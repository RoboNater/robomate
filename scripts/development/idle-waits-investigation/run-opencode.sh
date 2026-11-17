#!/usr/bin/env bash
# run-opencode.sh TRIAL HUB PORT TIMEOUT_MS|default (worker NAME T | orch NAME T...)
# OpenCode run trial. Config isolation: XDG_CONFIG_HOME is the trial's own directory, so the
# operator's global opencode.jsonc and its static robomate entry are not read. The login in
# XDG_DATA_HOME is. OC_MODEL picks the model.
. "$(dirname "$0")/common.sh"
trial=$1; hub=$2; port=$3; tt=$4
shift 4
trial_dir "$trial"
mkdir -p xdg-config/opencode work
if [ "$tt" = default ]; then tline=""; else tline=", \"timeout\": $tt"; fi
cat > xdg-config/opencode/opencode.json <<J
{"\$schema": "https://opencode.ai/config.json", "snapshot": false,
 "mcp": {"robomate": {"type": "local",
   "command": ["uv", "run", "--locked", "--project", "$SMOKE_CLONE", "robomate", "mcp", "--harness", "opencode"],
   "environment": {"ROBOMATE_HUB_URL": "http://127.0.0.1:$port",
                   "ROBOMATE_TOKEN_FILE": "$(hub_token "$hub")",
                   "SCRATCH_PROGRESS_LOG": "$d/progress.log",
                   "HUB_TELEMETRY_LOG": "$d/bridge-telemetry.jsonl"},
   "enabled": true$tline}}}
J
"$HERE/prompt.sh" "$@" > prompt.txt
echo "start $(date -u +%FT%TZ) timeout_ms=$tt" > timing.txt
XDG_CONFIG_HOME=$d/xdg-config opencode run --auto --dir "$d/work" \
  -m "${OC_MODEL:-opencode-go/deepseek-v4.1-flash}" --format json --print-logs "$(cat prompt.txt)" \
  < /dev/null > out.jsonl 2> err.txt
echo "exit $? $(date -u +%FT%TZ)" >> timing.txt
