#!/usr/bin/env bash
set -euo pipefail

repo_root=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
if [[ $# -lt 1 || $# -gt 2 ]]; then
  echo "usage: $0 RUN_DIR [CRASH_POINT]" >&2
  exit 2
fi
run_dir=$(realpath "$1")
manifest="$run_dir/run.json"
token_file="$run_dir/token"
crash_at=${2:-}

for required in "$manifest" "$token_file"; do
  if [[ ! -f "$required" ]]; then
    echo "missing prepared run file: $required" >&2
    exit 1
  fi
done
for _ in $(seq 1 60); do
  if curl --silent --fail http://127.0.0.1:8420/healthz >/dev/null; then
    break
  fi
  sleep 0.5
done
if ! curl --silent --fail http://127.0.0.1:8420/healthz >/dev/null; then
  echo "Alice's hub did not become ready on port 8420" >&2
  exit 1
fi

worker_args=(
  uv run --locked --project "$repo_root" python "$repo_root/scripts/mock-worker.py"
  --manifest "$manifest" --token "$(<"$token_file")" --timeout 120
)
if [[ -n "$crash_at" ]]; then
  worker_args+=(--crash-at "$crash_at")
fi
UV_CACHE_DIR=${UV_CACHE_DIR:-/tmp/robo-step5-uv-cache} "${worker_args[@]}"
