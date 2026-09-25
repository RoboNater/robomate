#!/usr/bin/env bash
# Usage: bootstrap-workspace.sh AGENT ABSOLUTE_DESTINATION REPOSITORY_URL
set -euo pipefail
repo_root=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
exec uv run --locked --project "$repo_root" python "$repo_root/scripts/bootstrap-workspace.py" "$@"
