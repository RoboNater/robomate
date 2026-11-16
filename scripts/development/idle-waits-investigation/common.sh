# Sourced by every script here. IDLE_WAITS_SCRATCH is the directory setup-smoke.sh made.
: "${IDLE_WAITS_SCRATCH:?set IDLE_WAITS_SCRATCH to the directory setup-smoke.sh made}"
# shellcheck source=/dev/null
. "$IDLE_WAITS_SCRATCH/env.sh"
HERE=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)

hub_token() { echo "$SMOKE/robomate-long.robomate/$1/token/token"; }

# trial_dir TRIAL: make and enter $SMOKE/trials/TRIAL, print its path in $d.
trial_dir() {
  d=$SMOKE/trials/$1
  mkdir -p "$d"
  cd "$d" || exit 1
}

# hub_dirs: the scratch clone and its hub worktrees. Agents that join get worktrees of their
# own under robomate-long.robomate/, which are not hubs.
hub_dirs() {
  git -C "$SMOKE_CLONE" worktree list --porcelain | sed -n 's/^worktree //p' | grep -v '\.robomate/'
}
