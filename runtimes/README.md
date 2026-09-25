# Worker Runtime Configurations

This directory contains template configurations for worker agent runtimes (Claude Code, OpenAI Codex CLI, Gemini CLI) to connect to the agent hub via `worker-mcp`.

## Template Files

- `claude-code.mcp.json` — For Claude Code (e.g., Bob).
- `codex.config.toml` — For Codex CLI (e.g., Charlie).
- `gemini.settings.json` — For Gemini CLI.

## Platform Setup & Absolute Paths

All templates use `uv run --directory /path/to/agent-hub worker-mcp` so worker runtimes can launch the MCP server from any working directory or sandbox repo.

Before using a template:
1. Replace `/path/to/agent-hub` with the absolute path to the `agent-hub` workspace checkout:
   - **Linux / macOS**: `/home/user/path/to/robo-agents`
   - **Windows**: Prefer forward slashes (e.g., `C:/work/robo-agents`). A backslash must be escaped as `\\` in JSON (e.g., `C:\\work\\robo-agents`): a single unescaped `\` either fails to parse (`\w` is an `Invalid \escape`) or silently corrupts the value (`\n` and `\r` become newline/carriage-return control characters).
2. Replace `http://alice-host:8420` with your hub's public address (e.g., `http://127.0.0.1:8420` for local runs).
3. Replace `HUB_TOKEN` with the shared bearer token (from `$HUB_STATE_DIR/token` or `.env`).
4. Ensure `AGENT_NAME` names the assigned worker.
5. Fill in the identity profile (spec §3), which Alice's role policy pairs workers on:

   | Variable | Meaning | Template value |
   |---|---|---|
   | `HUB_HARNESS` | Agent harness running the worker | `claude-code` / `codex` / `gemini` |
   | `HUB_HARNESS_VERSION` | Harness version, e.g. from `claude --version` | empty |
   | `HUB_PROVIDER` | Model provider | `anthropic` / `openai` / `google` |
   | `HUB_MODEL` | Exact model ID, when the launcher pins one | empty |
   | `HUB_CAPABILITIES` | Comma-separated capabilities matched against `implementer_capabilities` / `reviewer_capabilities` | empty |

   An empty or unset variable is reported as `unknown` (capabilities as none), never guessed.
   Change the provider if the harness is pointed elsewhere (e.g. Claude Code on Bedrock).
   When `HUB_MODEL` is empty the agent may declare its own model through `check_in(model=...)`,
   recorded with `model_source: declared`; a value set here wins and is recorded as `env`.
   `AGENT_RUNTIME`, the Step 4 name for `HUB_HARNESS`, is still honoured when `HUB_HARNESS` is unset.

For endurance runs, replace the empty `HUB_TELEMETRY_LOG` in the runtime's MCP
configuration with an absolute path. `worker-mcp` appends JSON Lines records
for MCP tool calls and outcomes, errors, HTTP retry attempts, and timer
heartbeats. Reusing the path across a supervised restart is intentional: each
process has a distinct `session_id` and `worker_instance_id`.
The endurance verifier selects only the checked-in worker instance, and the
supervisor considers only records appended after it launches, so older records
at the same path cannot satisfy the current run.

Claude Code print mode may end a turn while work is still pending. For an
endurance run, keep one streaming process and its `worker-mcp` child alive with
the policy-free supervisor (Alice still owns every assignment and decision):

```sh
CLAUDE_MCP_CONFIG=/absolute/path/claude-code.mcp.json \
HUB_TELEMETRY_LOG=/absolute/path/endurance-worker.jsonl \
scripts/supervise-claude-code.sh
```

The `HUB_TELEMETRY_LOG` above and the value in `claude-code.mcp.json` must name
the same file.

## Step 5 runtime prompts

Before launch, expose the checked-in skills to Claude Code by copying or
symlinking each required directory into `.claude/skills/` in that runtime's
workspace (or into `~/.claude/skills/` for a user-wide installation). Step 5C's
launch scripts will automate this. For example, Alice needs
`skills/alice-orchestrator/` installed as
`.claude/skills/alice-orchestrator/`, while a Claude worker needs
`skills/worker/` installed as `.claude/skills/worker/`.

Start Alice with [`prompts/alice.md`](../prompts/alice.md), filling in every
repository, issue, account, close-out, and policy value; Claude Code then loads
the [`alice-orchestrator`](../skills/alice-orchestrator/SKILL.md) skill named
there. Start a Claude worker with the thin
[`worker`](../skills/worker/SKILL.md) skill.
For runtimes without skills, use [`prompts/worker.md`](../prompts/worker.md),
which inlines the same [`guides/worker.md`](../guides/worker.md) etiquette and
still fetches the assigned role guide from the hub for every task.

This launcher is specific to the Step 4B endurance scenario, not the general
Step 5 worker launcher. It pre-approves only literal `sleep` commands, the
blocking background-task wait that Claude Code requires for long sleeps, and
the four worker coordination tools. It sends a continuation message after a
premature end-turn, and exits only after telemetry records the hub's release
response.

## Repository workspace contract (Step 6 / #28)

Bootstrap each worker separately:

```sh
scripts/bootstrap-workspace.sh bob /absolute/bob-sandbox git@github.com:RoboNater/robo-agents-sandbox.git
scripts/bootstrap-workspace.sh charlie /absolute/charlie-sandbox git@github.com:RoboNater/robo-agents-sandbox.git
# Or cross-platform via Python:
# uv run --locked python scripts/bootstrap-workspace.py bob /absolute/bob-sandbox git@github.com:RoboNater/robo-agents-sandbox.git
```

The JSON stdout contains `workspace_id`, `agent`, `path`, and `repository`.
Each full, non-shallow clone has its own `.git/robo-agents-workspace.json`,
created exclusively with mode 0600 and a cryptographically random 256-bit ID.
Reruns preserve the ID and all commits; they never fetch, reset, or delete an
existing clone. Dirty clones, different origins, absent identities, and
agent/path mismatches fail with an actionable error. A moved clone requires
manual reconciliation, not automatic identity regeneration.

Replace the Bob and Charlie templates' distinct `HUB_WORKSPACE` placeholders
with their canonical absolute clone roots. Start the LLM in that clone with
`cd` (Claude) or `-C` (Codex). `uv --directory` selects the coordination code
for the MCP child; it does not set the LLM's shell workspace. Worker settings
validate clone topology, origin, identity ownership and permissions before
reporting the persisted ID. Non-repository tests/endurance may omit
`HUB_WORKSPACE`; explicitly setting it empty is an error.

Step 6 launchers render private run-local configurations and expose all six
worker tools. Bob uses the existing transport-only supervisor with the worker
prompt and repository tools. Its continuation prompt remains fixed and owns
no workflow decisions. Charlie uses `--approve-for-me`, which selects
workspace-write in Codex 0.154.0, plus `--add-dir <charlie-clone>/.git` so fetch
and checkout can update only his own Git metadata. His launch prompt requires
the trusted review-check helper to run tests and audit the actual assigned head
in that persisted clone. Charlie uses a run-local `CODEX_HOME` and authentication
symlink; global configuration is never edited. See
[`docs/step6-acceptance.md`](../docs/step6-acceptance.md) for the automated sandbox
demo and [`docs/user-guide.md`](../docs/user-guide.md) for user-facing setup on
your own repositories.
