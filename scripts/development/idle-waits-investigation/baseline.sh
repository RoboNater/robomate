#!/usr/bin/env bash
# baseline.sh HUB PORT NAME ROLE TOOL TIMEOUT... — harness-free holds, one after another.
# START_WORKFLOW=1 and TAKEOVER=1 are passed through to baseline_hold.py.
. "$(dirname "$0")/common.sh"
hub=$1; port=$2; name=$3; role=$4; tool=$5
shift 5
export ROBOMATE_HUB_URL=http://127.0.0.1:$port
ROBOMATE_TOKEN_FILE=$(hub_token "$hub")
export ROBOMATE_TOKEN_FILE
cd "$SMOKE/trials" || exit 1
for t in "$@"; do
  echo "== $(date -u +%FT%TZ) $name $tool $t"
  uv run --locked --project "$SMOKE_CLONE" python "$HERE/baseline_hold.py" "$name" "$role" "$tool" "$t" 2>&1 \
    | grep -v "^INFO"
done
