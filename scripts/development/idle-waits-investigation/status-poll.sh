#!/usr/bin/env bash
# status-poll.sh [SCRATCH] — append `robomate status --json` of every smoke hub to
# logs/status.jsonl every 20 s. The argument only labels the process for stop-hubs.sh.
. "$(dirname "$0")/common.sh"
while true; do
  for dir in $(hub_dirs); do
    out=$(cd "$dir" && uv run --locked --project "$SMOKE_CLONE" robomate status --json 2>/dev/null) || continue
    printf '{"t":"%s","hub":"%s","status":%s}\n' "$(date -u +%FT%TZ)" "$(basename "$dir")" "$out" \
      >> "$SMOKE/logs/status.jsonl"
  done
  sleep 20
done
