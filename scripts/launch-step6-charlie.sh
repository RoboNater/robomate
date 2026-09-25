#!/usr/bin/env bash
set -euo pipefail
if [[ $# -ne 1 || $1 != /* ]]; then
  echo "usage: $0 ABSOLUTE_RUN_DIR" >&2; exit 2
fi
run_dir=$1
readarray -t settings < <(python3 - "$run_dir/run.json" <<'PY'
import json, sys
m = json.load(open(sys.argv[1]))
assert m['repository'] == 'RoboNater/robo-agents-sandbox' and m.get('issue')
print(m['workspaces']['charlie']['path'])
print(m['models']['charlie'])
PY
)
export HUB_WORKSPACE=${settings[0]}
export CODEX_HOME="$run_dir/codex-home"
exec codex exec --ephemeral -C "$HUB_WORKSPACE" --add-dir "$HUB_WORKSPACE/.git" --approve-for-me \
  --model "${settings[1]}" --json - < "$run_dir/charlie.prompt.md"
