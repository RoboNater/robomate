#!/usr/bin/env bash
set -euo pipefail
if [[ $# -ne 1 || $1 != /* ]]; then
  echo "usage: $0 ABSOLUTE_RUN_DIR" >&2; exit 2
fi
run_dir=$1
readarray -t settings < <(python3 - "$run_dir/run.json" <<'PY'
import json, sys, re
from pathlib import Path
m = json.load(open(sys.argv[1]))
assert m['repository'] == 'RoboNater/robo-agents-sandbox' and m.get('issue')
print(m['models']['alice'])
print(m['alice_session_id'])
project = re.sub(r'[^A-Za-z0-9]', '-', str(Path(m['run_dir']) / 'alice-runtime'))
config_root = Path(m.get('claude_config_dir', str(Path.home() / '.claude')))
transcript = config_root / 'projects' / project / (m['alice_session_id'] + '.jsonl')
print('resume' if transcript.is_file() else 'new')
print(config_root)
print('custom' if m.get('claude_config_dir_is_custom', config_root != Path.home() / '.claude') else 'default')
print(m.get('network', {}).get('hub_port', 8420))  # a networked (Step 7) run names its port
PY
)
if curl --silent --fail "http://127.0.0.1:${settings[5]}/healthz" >/dev/null 2>&1; then
  echo "port ${settings[5]} occupied; leave other checkout listeners alone" >&2; exit 1
fi
if [[ ${settings[4]} == custom ]]; then
  export CLAUDE_CONFIG_DIR=${settings[3]}
else
  # Setting even the default directory relocates Claude's main .claude.json.
  unset CLAUDE_CONFIG_DIR
fi
session_flags=(--session-id "${settings[1]}")
if [[ ${settings[2]} == resume ]]; then
  session_flags=(--resume "${settings[1]}")
fi
cd "$run_dir/alice-runtime"
exec claude --model "${settings[0]}" "${session_flags[@]}" \
  --strict-mcp-config --mcp-config "$run_dir/alice.mcp.json" \
  --permission-mode acceptEdits --allowedTools 'Bash(gh *),mcp__hub__*' \
  -- "$(<"$run_dir/alice.prompt.md")"
