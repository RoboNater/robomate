#!/usr/bin/env bash
# Invoke the configurable helper from any working directory.
set -euo pipefail
robomate_root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)"
uv sync --locked --all-packages --project "${robomate_root}"
exec uv run --locked --project "${robomate_root}" python \
    "${robomate_root}/scripts/prep-standard-run-area.py" "$@"
