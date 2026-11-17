#!/usr/bin/env bash
# run-codex.sh TRIAL HUB PORT TOOL_TIMEOUT_SEC|default (worker NAME T | orch NAME T...)
# Codex exec trial. Config isolation: CODEX_HOME is the trial's own directory, with only
# auth.json linked from the operator's Codex home. CODEX_MODEL picks the model (default
# gpt-6-luna). CODEX_EXTRA_TOML is appended to config.toml, for example
#   CODEX_EXTRA_TOML=$'[features.code_mode]\ndirect_only_tool_namespaces = ["mcp__robomate"]'
# CODEX_PROMPT_EXTRA is appended to the prompt, for example the yield pragma instruction.
. "$(dirname "$0")/common.sh"
trial=$1; hub=$2; port=$3; tt=$4
shift 4
trial_dir "$trial"
mkdir -p codex-home
ln -sf "${CODEX_HOME:-$HOME/.codex}/auth.json" codex-home/auth.json
{
  echo '[mcp_servers.robomate]'
  echo 'command = "uv"'
  echo "args = [\"run\", \"--locked\", \"--project\", \"$SMOKE_CLONE\", \"robomate\", \"mcp\", \"--harness\", \"codex\"]"
  echo 'startup_timeout_sec = 120'
  [ "$tt" = default ] || echo "tool_timeout_sec = $tt"
  echo "env = { ROBOMATE_HUB_URL = \"http://127.0.0.1:$port\", ROBOMATE_TOKEN_FILE = \"$(hub_token "$hub")\", SCRATCH_PROGRESS_LOG = \"$d/progress.log\", HUB_TELEMETRY_LOG = \"$d/bridge-telemetry.jsonl\" }"
  for tool in join whoami await_assignment wait_for_event start_workflow get_state; do
    printf '\n[mcp_servers.robomate.tools.%s]\napproval_mode = "approve"\n' $tool
  done
  [ -z "$CODEX_EXTRA_TOML" ] || printf '\n%s\n' "$CODEX_EXTRA_TOML"
} > codex-home/config.toml
"$HERE/prompt.sh" "$@" > prompt.txt
[ -z "$CODEX_PROMPT_EXTRA" ] || printf '%s\n' "$CODEX_PROMPT_EXTRA" >> prompt.txt
echo "start $(date -u +%FT%TZ) tool_timeout_sec=$tt model=${CODEX_MODEL:-gpt-6-luna}" > timing.txt
CODEX_HOME=$d/codex-home codex exec --skip-git-repo-check -m "${CODEX_MODEL:-gpt-6-luna}" \
  -s read-only --json "$(cat prompt.txt)" < /dev/null > out.jsonl 2> err.txt
echo "exit $? $(date -u +%FT%TZ)" >> timing.txt
