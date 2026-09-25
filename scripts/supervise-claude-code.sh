#!/usr/bin/env bash
# Keep one Claude Code streaming process (and its worker-mcp child) alive while
# reprompting premature end-turns. Assignment policy remains entirely in Alice.
set -euo pipefail

: "${CLAUDE_MCP_CONFIG:?set CLAUDE_MCP_CONFIG to an absolute MCP config path}"
: "${HUB_TELEMETRY_LOG:?set HUB_TELEMETRY_LOG to the worker JSONL telemetry path}"

case "$CLAUDE_MCP_CONFIG" in
  /*) ;;
  *) echo "CLAUDE_MCP_CONFIG must be an absolute path" >&2; exit 2 ;;
esac
case "$HUB_TELEMETRY_LOG" in
  /*) ;;
  *) echo "HUB_TELEMETRY_LOG must be an absolute path" >&2; exit 2 ;;
esac

claude_bin=${CLAUDE_BIN:-claude}
model=${CLAUDE_MODEL:-sonnet}
reprompt_delay_s=${CLAUDE_REPROMPT_DELAY_S:-1}
max_reprompts=${CLAUDE_MAX_REPROMPTS:-100}
tools='Bash,TaskOutput,mcp__hub__check_in,mcp__hub__await_assignment,mcp__hub__ask_alice,mcp__hub__submit_result'
allowed_tools='Bash(sleep *),TaskOutput,mcp__hub__check_in,mcp__hub__await_assignment,mcp__hub__ask_alice,mcp__hub__submit_result'
telemetry_start_bytes=0
if [[ -f "$HUB_TELEMETRY_LOG" ]]; then
  telemetry_start_bytes=$(wc -c < "$HUB_TELEMETRY_LOG")
fi

initial_prompt='You are an unattended worker. Use only the hub MCP tools, literal sleep commands, and TaskOutput. Do not inspect or modify repository files. Call check_in, then loop on await_assignment with timeout_s 20 and immediately retry every timeout. Follow each assignment exactly, retry an identical ask_alice question after timeout, submit exactly one result, and return to await_assignment. Do not end before release.'
continue_prompt='Continue the unattended worker loop now. If the current assignment has unfinished waiting or work, finish it and submit exactly one result before awaiting more work. Preserve path/URL text literally. Do not end before release.'

# Step 6 supplies repository tools and the checked-in worker prompt. Defaults
# retain the endurance harness's original transport-only exercise.
tools=${CLAUDE_WORKER_TOOLS:-$tools}
allowed_tools=${CLAUDE_WORKER_ALLOWED_TOOLS:-$allowed_tools}
if [[ -n ${CLAUDE_WORKER_PROMPT_FILE:-} ]]; then
  initial_prompt=$(<"$CLAUDE_WORKER_PROMPT_FILE")
fi

json_escape() {
  # Hand-rolled JSON string escaping for stream-json stdin. Prompts rendered
  # from a Windows (CRLF) checkout carry carriage returns, so \r must be
  # escaped like the other JSON short escapes; a raw control byte corrupts
  # the framing and Claude exits with a streaming-input SyntaxError.
  local value=$1
  value=${value//\\/\\\\}
  value=${value//\"/\\\"}
  value=${value//$'\b'/\\b}
  value=${value//$'\f'/\\f}
  value=${value//$'\n'/\\n}
  value=${value//$'\r'/\\r}
  value=${value//$'\t'/\\t}
  printf '%s' "$value"
}

send_message() {
  local escaped
  escaped=$(json_escape "$1")
  printf '{"type":"user","message":{"role":"user","content":[{"type":"text","text":"%s"}]}}\n' \
    "$escaped" >&"$write_fd"
}

released() {
  [[ -f "$HUB_TELEMETRY_LOG" ]] \
    && tail -c "+$((telemetry_start_bytes + 1))" "$HUB_TELEMETRY_LOG" \
      | grep '"outcome": "release"' >/dev/null
}

coproc CLAUDE_PROC {
  # `=` value forms: Claude 2.1.278 on Windows mis-parses space-separated
  # --mcp-config/--tools/--allowedTools in -p mode (the value is ignored and
  # the following args are swallowed); `=` works on every platform.
  "$claude_bin" -p \
    --model="$model" \
    --input-format=stream-json \
    --output-format=stream-json \
    --verbose \
    --strict-mcp-config \
    --mcp-config="$CLAUDE_MCP_CONFIG" \
    --permission-mode=dontAsk \
    --permission-prompts=none \
    --tools="$tools" \
    --allowedTools="$allowed_tools"
}
claude_pid=$CLAUDE_PROC_PID
read_fd=${CLAUDE_PROC[0]}
write_fd=${CLAUDE_PROC[1]}

cleanup() {
  exec {write_fd}>&- 2>/dev/null || true
  if kill -0 "$claude_pid" 2>/dev/null; then
    kill "$claude_pid" 2>/dev/null || true
  fi
}
trap cleanup EXIT INT TERM

send_message "$initial_prompt"
reprompts=0
while IFS= read -r -u "$read_fd" line; do
  printf '%s\n' "$line"
  if [[ $line =~ \"type\"[[:space:]]*:[[:space:]]*\"result\" ]]; then
    if released; then
      echo "Claude release observed after $reprompts supervisor reprompt(s)" >&2
      exec {write_fd}>&-
      wait "$claude_pid"
      trap - EXIT INT TERM
      exit 0
    fi
    if (( reprompts >= max_reprompts )); then
      echo "Claude ended $reprompts turns without receiving release" >&2
      exit 1
    fi
    sleep "$reprompt_delay_s"
    echo "Claude ended before release; sending supervisor reprompt $((reprompts + 1))" >&2
    send_message "$continue_prompt"
    ((reprompts += 1))
  fi
done

wait "$claude_pid" || true
if released; then
  echo "Claude release observed after $reprompts supervisor reprompt(s)" >&2
  trap - EXIT INT TERM
  exit 0
fi
echo "Claude process exited before the worker received release" >&2
exit 1
