#!/usr/bin/env bash
# run-claude-code.sh TRIAL HUB PORT (worker NAME T | orch NAME T...) — Claude Code print-mode trial.
# CC_ENV="K=V ..." adds environment for the claude process (for example MCP_TOOL_TIMEOUT=7200000,
# CLAUDE_CODE_MCP_TOOL_IDLE_TIMEOUT=0). CC_SERVER_EXTRA=', "timeout": 7200000' adds keys to the
# MCP entry. Config isolation: --strict-mcp-config with the trial's own mcp.json.
. "$(dirname "$0")/common.sh"
trial=$1; hub=$2; port=$3
shift 3
trial_dir "$trial"
cat > mcp.json <<J
{"mcpServers": {"robomate": {"command": "uv",
  "args": ["run", "--locked", "--project", "$SMOKE_CLONE", "robomate", "mcp", "--harness", "claude-code"],
  "env": {"ROBOMATE_HUB_URL": "http://127.0.0.1:$port",
          "ROBOMATE_TOKEN_FILE": "$(hub_token "$hub")",
          "SCRATCH_PROGRESS_LOG": "$d/progress.log",
          "HUB_TELEMETRY_LOG": "$d/bridge-telemetry.jsonl"}$CC_SERVER_EXTRA}}}
J
"$HERE/prompt.sh" "$@" > prompt.txt
echo "start $(date -u +%FT%TZ) CC_ENV=$CC_ENV CC_SERVER_EXTRA=$CC_SERVER_EXTRA" > timing.txt
# shellcheck disable=SC2086
env -u CLAUDECODE -u CLAUDE_CODE_ENTRYPOINT $CC_ENV claude -p "$(cat prompt.txt)" \
  --model "${CC_MODEL:-claude-haiku-5-5}" --strict-mcp-config --mcp-config mcp.json \
  --allowedTools 'mcp__robomate__*' --output-format stream-json --verbose \
  < /dev/null > out.jsonl 2> err.txt
echo "exit $? $(date -u +%FT%TZ)" >> timing.txt
