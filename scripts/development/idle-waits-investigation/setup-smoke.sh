#!/usr/bin/env bash
# setup-smoke.sh SCRATCH_DIR [PATCH...] — build the isolated smoke area for the #497 hold trials.
#
# Clones this checkout into SCRATCH_DIR/robomate-long, applies patches/raise-timeout-bounds.patch
# plus any PATCH named (file names under patches/), commits them in the scratch clone only, syncs
# it, and writes SCRATCH_DIR/env.sh. SCRATCH_DIR must not exist. Nothing here is pushed.
set -euo pipefail
here=$(cd "$(dirname "$0")" && pwd)
repo=$(git -C "$here" rev-parse --show-toplevel)
scratch=$(realpath -m "$1")
shift
[ ! -e "$scratch" ] || { echo "$scratch exists" >&2; exit 1; }
mkdir -p "$scratch"/{state,xdg-state,xdg-config,logs,trials}
clone=$scratch/robomate-long
git clone -q "$repo" "$clone"
cd "$clone"
# `robomate up` needs an origin with origin/HEAD set (guides/worker.md, isolated smoke run).
git remote set-url origin https://github.com/RoboNater/robomate.git
git update-ref refs/remotes/origin/main HEAD
git symbolic-ref refs/remotes/origin/HEAD refs/remotes/origin/main
max_wait=7200
if [ "${IDLE_WAITS_FINAL_SETTINGS:-0}" = 1 ]; then
  # #559 acceptance: exercise the committed code and shipping ceiling, no scratch patches.
  max_wait=1800
else
  for patch in raise-timeout-bounds.patch "$@"; do
    git apply "$here/patches/$patch"
  done
  git -c user.name=scratch -c user.email=scratch@example.invalid commit -qam "SCRATCH ONLY (#497 trials)"
fi
uv sync --locked --all-packages > "$scratch/logs/uv-sync.log" 2>&1
cat > "$scratch/env.sh" <<ENV
# Isolated smoke environment for the #497 hold trials. Sourced through common.sh.
export SMOKE=$scratch
export SMOKE_CLONE=$clone
export HUB_STATE_DIR=$scratch/state
export ROBOMATE_OPERATOR_TOKEN_FILE=$scratch/state/operator-token
export XDG_STATE_HOME=$scratch/xdg-state
export XDG_CONFIG_HOME=$scratch/xdg-config
export HUB_MAX_WAIT_S=$max_wait
unset ROBOMATE_HUB_URL ROBOMATE_TOKEN ROBOMATE_TOKEN_FILE ROBOMATE_HUB_ID AGENT_NAME
ENV
echo "scratch ready: export IDLE_WAITS_SCRATCH=$scratch"
