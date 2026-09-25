#!/usr/bin/env bash
set -euo pipefail

repo_root=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
if [[ $# -ne 1 ]]; then
  echo "usage: $0 RUN_DIR" >&2
  exit 2
fi
run_dir=$(realpath "$1")
manifest="$run_dir/run.json"
state_dir="$run_dir/state"
token_file="$run_dir/token"
runtime_dir="$run_dir/alice-runtime"
mcp_config="$run_dir/alice.mcp.json"

for required in "$manifest" "$token_file"; do
  if [[ ! -f "$required" ]]; then
    echo "missing prepared run file: $required" >&2
    exit 1
  fi
done
if curl --silent --fail http://127.0.0.1:8420/healthz >/dev/null 2>&1; then
  echo "port 8420 already has a hub; stop only that checkout's listener before continuing" >&2
  exit 1
fi

mkdir -p "$runtime_dir/.claude/skills"
ln -sfn "$repo_root/skills/alice-orchestrator" \
  "$runtime_dir/.claude/skills/alice-orchestrator"
token=$(<"$token_file")
python3 - "$mcp_config" "$repo_root" "$state_dir" "$token" <<'PY'
import json
import sys

destination, repo_root, state_dir, token = sys.argv[1:]
config = {
    "mcpServers": {
        "hub": {
            "command": "uv",
            "args": ["run", "--locked", "--project", repo_root, "hub"],
            "env": {
                "HUB_STATE_DIR": state_dir,
                "HUB_TOKEN": token,
                "HUB_PUBLIC_URL": "http://127.0.0.1:8420",
                "HUB_GUIDES_DIR": f"{repo_root}/guides",
                "HUB_EVENT_LEASE_S": "5"
            }
        }
    }
}
with open(destination, "w", encoding="utf-8") as stream:
    json.dump(config, stream, indent=2)
    stream.write("\n")
PY
chmod 600 "$mcp_config"
prompt=$(UV_CACHE_DIR=${UV_CACHE_DIR:-/tmp/robo-step5-uv-cache} \
  uv run --locked --project "$repo_root" python "$repo_root/scripts/mock-worker.py" \
  --manifest "$manifest" --alice-prompt)

cd "$runtime_dir"
exec claude \
  --model "${CLAUDE_MODEL:-sonnet}" \
  --mcp-config "$mcp_config" \
  --strict-mcp-config \
  --permission-mode acceptEdits \
  --allowedTools 'Bash(gh *),mcp__hub__*' \
  "$prompt"
