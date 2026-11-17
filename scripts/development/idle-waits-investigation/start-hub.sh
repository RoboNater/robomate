#!/usr/bin/env bash
# start-hub.sh NAME PORT — start one smoke hub, detached. NAME robomate-long is the scratch
# clone itself; any other NAME is a linked worktree of it, created on first use.
. "$(dirname "$0")/common.sh"
name=$1
port=$2
if [ "$name" != robomate-long ] && [ ! -d "$SMOKE/$name" ]; then
  git -C "$SMOKE_CLONE" worktree add -q "$SMOKE/$name" -b "smoke-$name" HEAD
fi
cd "$SMOKE/$name" || exit 1
setsid nohup uv run --locked --project "$SMOKE_CLONE" robomate up --port "$port" \
  >> "$SMOKE/logs/hub-$name.log" 2>&1 &
for _ in $(seq 30); do
  grep -q "running at" "$SMOKE/logs/hub-$name.log" 2>/dev/null && break
  sleep 1
done
grep "running at" "$SMOKE/logs/hub-$name.log" | tail -n 1
