#!/usr/bin/env bash
# run-antigravity.sh TRIAL HUB PORT TIMEOUT_SECONDS|default (worker NAME T | orch NAME T...)
# Antigravity print-mode trial. Config isolation: HOME is the trial's own directory with only
# the agy login files linked (as run_common.agy_home does), so the operator's
# ~/.gemini/config/mcp_config.json is not read. AGY_MODEL picks the model.
. "$(dirname "$0")/common.sh"
trial=$1; hub=$2; port=$3; tt=$4
shift 4
trial_dir "$trial"
h=$d/home
mkdir -p "$h/.gemini/antigravity-cli" "$h/.gemini/config" work
real=$HOME
for f in antigravity-oauth-token jetski_state.pbtxt settings.json; do
  [ -e "$real/.gemini/antigravity-cli/$f" ] && ln -sf "$real/.gemini/antigravity-cli/$f" "$h/.gemini/antigravity-cli/$f"
done
if [ "$tt" = default ]; then tline=""; else tline=", \"timeoutSeconds\": $tt"; fi
cat > "$h/.gemini/config/mcp_config.json" <<J
{"mcpServers": {"robomate": {"command": "uv",
  "args": ["run", "--locked", "--project", "$SMOKE_CLONE", "robomate", "mcp", "--harness", "antigravity"],
  "env": {"ROBOMATE_HUB_URL": "http://127.0.0.1:$port",
          "ROBOMATE_TOKEN_FILE": "$(hub_token "$hub")",
          "SCRATCH_PROGRESS_LOG": "$d/progress.log",
          "HUB_TELEMETRY_LOG": "$d/bridge-telemetry.jsonl"}$tline}}}
J
"$HERE/prompt.sh" "$@" > prompt.txt
echo "start $(date -u +%FT%TZ) timeoutSeconds=$tt" > timing.txt
cd work || exit 1
HOME=$h USERPROFILE=$h GIT_CONFIG_GLOBAL=$real/.gitconfig XDG_CACHE_HOME=$real/.cache \
  XDG_DATA_HOME=$real/.local/share \
  "$(command -v agy)" --model "${AGY_MODEL:-gpt-oss-120b}" --dangerously-skip-permissions \
  --output-format stream-json --log-file "$d/agy-cli.log" -p "$(cat "$d/prompt.txt")" \
  < /dev/null > "$d/out.jsonl" 2> "$d/err.txt"
echo "exit $? $(date -u +%FT%TZ)" >> "$d/timing.txt"
