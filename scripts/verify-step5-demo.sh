#!/usr/bin/env bash
set -euo pipefail

repo_root=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
if [[ $# -ne 1 ]]; then
  echo "usage: $0 RUN_DIR" >&2
  exit 2
fi
run_dir=$(realpath "$1")
UV_CACHE_DIR=${UV_CACHE_DIR:-/tmp/robo-step5-uv-cache} \
  uv run --locked --project "$repo_root" python "$repo_root/scripts/mock-worker.py" \
  --manifest "$run_dir/run.json" \
  --verify \
  --hub-db "$run_dir/state/hub.db" \
  --evidence "$run_dir/evidence.json"
