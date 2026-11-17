#!/usr/bin/env bash
# stop-hubs.sh — `robomate down` for every smoke hub of this scratch area, and stop the poller.
. "$(dirname "$0")/common.sh"
pkill -f "status-poll.sh $SMOKE\$" 2>/dev/null
for dir in $(hub_dirs); do
  (cd "$dir" && uv run --locked --project "$SMOKE_CLONE" robomate down 2>&1 | tail -n 1)
done
