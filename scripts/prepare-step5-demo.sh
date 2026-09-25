#!/usr/bin/env bash
set -euo pipefail

repo_root=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
if [[ $# -lt 1 || $# -gt 3 ]]; then
  echo "usage: $0 RUN_DIR [RUN_ID] [--allow-repeat]" >&2
  exit 2
fi
run_dir=$(realpath -m "$1")
run_id=${2:-}
repeat_arg=${3:-}
if [[ "$run_id" == "--allow-repeat" ]]; then
  repeat_arg=$run_id
  run_id=""
fi
if [[ -n "$repeat_arg" && "$repeat_arg" != "--allow-repeat" ]]; then
  echo "third argument must be --allow-repeat" >&2
  exit 2
fi
manifest="$run_dir/run.json"
scenario="$repo_root/scenarios/step5c-untrusted.json"

mkdir -p "$run_dir/state"
if [[ -e "$run_dir/token" ]]; then
  echo "refusing to overwrite existing run token: $run_dir/token" >&2
  exit 1
fi
openssl rand -hex 32 >"$run_dir/token"
chmod 600 "$run_dir/token"

seed_args=(
  uv run --locked --project "$repo_root" python "$repo_root/scripts/mock-worker.py"
  --scenario "$scenario" --manifest "$manifest" --seed
)
if [[ -n "$run_id" ]]; then
  seed_args+=(--run-id "$run_id")
fi
if [[ -n "$repeat_arg" ]]; then
  seed_args+=(--allow-repeat)
fi
UV_CACHE_DIR=${UV_CACHE_DIR:-/tmp/robo-step5-uv-cache} "${seed_args[@]}"

echo "Prepared Step 5C run in $run_dir" >&2
echo "Next: scripts/launch-step5-alice.sh $run_dir" >&2
echo "Then, from a second terminal: scripts/launch-step5-workers.sh $run_dir" >&2
