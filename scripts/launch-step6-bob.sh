#!/usr/bin/env bash
set -euo pipefail
repo_root=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
if [[ $# -ne 1 || $1 != /* ]]; then
  echo "usage: $0 ABSOLUTE_RUN_DIR" >&2; exit 2
fi
run_dir=$1
readarray -t settings < <(python3 - "$run_dir/run.json" <<'PY'
import json, sys
m = json.load(open(sys.argv[1]))
assert m['repository'] == 'RoboNater/robo-agents-sandbox' and m.get('issue')
assert not m.get('network'), 'networked run: launch bob with scripts/step6_launch.py bob RUN_DIR'
print(m['workspaces']['bob']['path'])
print(m['models']['bob'])
PY
)
export HUB_WORKSPACE=${settings[0]}
export CLAUDE_MODEL=${settings[1]}
export CLAUDE_MCP_CONFIG="$run_dir/bob.mcp.json"
export HUB_TELEMETRY_LOG="$run_dir/bob.telemetry.jsonl"
export CLAUDE_WORKER_PROMPT_FILE="$run_dir/bob.prompt.md"
export CLAUDE_WORKER_TOOLS='Bash,Read,Edit,Write,TaskOutput,mcp__hub__check_in,mcp__hub__get_role_guide,mcp__hub__await_assignment,mcp__hub__report_progress,mcp__hub__ask_alice,mcp__hub__submit_result'
export CLAUDE_WORKER_ALLOWED_TOOLS='Read,Edit,Write,TaskOutput,Bash(git *),Bash(gh *),Bash(python3 *),mcp__hub__*'
cd "$HUB_WORKSPACE"
exec "$repo_root/scripts/supervise-claude-code.sh"
