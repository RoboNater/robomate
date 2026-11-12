# Configure the robomate MCP entry in each agent harness

This prompt and `scripts/check-mcp-tools.py` are interim: `robomate install`
(§9.1) and harness profiles replace them in M3; remove both then.

You are setting up this machine so AI coding-agent harnesses can join a robomate hub. robomate (github.com/RoboNater/robomate) is a local hub that a small team of agents joins through an MCP bridge, `robomate mcp`. Each harness needs one **static, user-level** MCP server entry named `robomate`, which runs that bridge from a fixed robomate checkout. Configure it for each of these that is installed: **Claude Code**, **OpenAI Codex CLI**, **OpenCode** and **Google Antigravity** (`agy`).

## Input

ROBOMATE_CHECKOUT = `$HOME/lw/w535-robomate/active-install`

(Operator: set this for the machine before sending. On native Windows, give an absolute path with forward slashes, e.g. `C:/Users/me/robomate`.)

Resolve the path to an absolute one yourself, since harness configs generally don't expand `$HOME` or `~`. If it doesn't exist, or isn't a robomate checkout (it should contain `pyproject.toml`, `uv.lock` and `packages/cli`), stop and ask.

## The entry

For harness `<h>` (one of `claude-code`, `codex`, `opencode`, `antigravity`), the server command is:

```
uv run --locked --project <ROBOMATE_CHECKOUT> robomate mcp --harness <h>
```

That is, the command `uv` with the remaining words as its argument list.

Rules:

- Name it exactly `robomate`, at **user (global) scope**, not project scope.
- **No token, no hub URL, no identity and no paths other than `--project`.** Don't set `ROBOMATE_HUB_URL`, `ROBOMATE_TOKEN*`, `AGENT_NAME`, `HUB_WORKSPACE` or any `HUB_*` variable. The bridge finds the hub from the agent's worktree. robomate's launcher supplies identity on each launch: for Codex it adds `-c mcp_servers.robomate.env.<KEY>=…` overrides, which must merge into this entry.
- No `--role` and no `--name`. The agent declares both when it calls `join`.
- Don't limit the tool list (no `enabled_tools`-style allow-list) unless the harness requires one. The same entry serves orchestrators and workers.
- Older robomate docs and the `runtimes/*` templates show per-agent entries with a hub URL, token file and identity. Those belong to the older prepared-run route. Don't copy their `env` blocks.

Settings that unattended runs need. Harness config formats change, so confirm the current location and key names in each harness's official documentation (search the web) for the installed version. If a key has been renamed, use the current name and say so in your report.

| Harness | User-level config (typical; confirm) | Settings beyond the command |
|---|---|---|
| Claude Code | `claude mcp add --scope user …` (stored in `~/.claude.json`) | None. |
| Codex CLI | `~/.codex/config.toml` (`%USERPROFILE%\.codex\config.toml` on Windows; `$CODEX_HOME/config.toml` if `CODEX_HOME` is set), table `[mcp_servers.robomate]` | `command`, `args`, `startup_timeout_sec = 120`, `tool_timeout_sec = 330` (a bridge call can legitimately hold for about 315 s). Approve every bridge tool, because unattended runs can't answer approval prompts: one `[mcp_servers.robomate.tools.<tool>]` table with `approval_mode = "approve"` for each tool name from step 4. |
| OpenCode | Global config: `~/.config/opencode/opencode.json` or `opencode.jsonc` (confirm the Windows location), key `mcp.robomate` | `"type": "local"`, `"command": ["uv", "run", …]` (the whole command as one array), `"enabled": true`, `"timeout": 330000` (milliseconds). |
| Antigravity (`agy`) | The user-level MCP config `agy` reads. robomate's launcher has used `<home>/.gemini/config/mcp_config.json`; confirm against current docs. Key `mcpServers.robomate` | `command`, `args`, `"timeoutSeconds": 330`. If `agy` requires an `enabledTools` allow-list, list every tool from step 4. |

OpenCode 1.19.5 converts the documented V2 `mcp.servers` layout to the flat
form and drops its object timeout; use the flat form above and revisit when
OpenCode follows the V2 documentation. Codex documentation has moved to
learn.chatgpt.com; its keys remain unchanged, and a running app-server daemon
may need a restart to load changes. Antigravity uses the path above; its
`timeoutSeconds` covers both connection and tool calls, and `enabledTools` is
optional. `codex mcp add` cannot set timeouts or approvals, and `agy mcp add`
cannot set a timeout.

## Steps

1. **Check for active runs.** Ask the operator, unless they've already said, whether any robomate run is in progress on this machine. A user-level entry is loaded by every session of that harness, including a running agent's next resume. OpenCode also merges its global config into the `OPENCODE_CONFIG` file that older prepared runs pass. If a run is active, stop and confirm before changing anything.
2. **Take inventory.** Work out the environment: Linux, macOS, native Windows or WSL. Treat WSL as a separate machine here. Configure only the harnesses installed inside it, with a Linux path to a checkout on the WSL filesystem. Never share one checkout's `.venv` between Windows and WSL. For each harness, record whether its CLI (`claude`, `codex`, `opencode`, `agy`) is on `PATH`, and its `--version`. Skip absent harnesses and report them; don't install anything. Confirm `uv` is on `PATH`.
3. **Prepare the checkout.** In ROBOMATE_CHECKOUT, run `uv sync --locked --all-packages` so the bridge starts quickly on first launch. Don't edit, pull or switch branches in the checkout. If `--locked` fails, stop and report it. Never relock or hand-edit `uv.lock`.
4. **List the bridge's tools.** From ROBOMATE_CHECKOUT, run:

   ```
   uv run --locked python scripts/check-mcp-tools.py --list
   ```

   It starts the bridge in a temporary directory with any hub selection removed from its environment, prints the server info and tool list as JSON, and exits. It contacts no hub. If it fails, stop and report its output.

   Expect the tool list to start with `join` and `whoami`, and to include worker tools (such as `await_assignment` and `submit_result`) and orchestrator tools (such as `assign_task` and `wait_for_event`). Use the list the probe actually returns.
5. **For each installed harness:**
   1. Find its user-level config, confirming the location and format in the official docs for the installed version.
   2. Back up the config file before changing it: copy it to `<file>.bak-robomate-<YYYYMMDD-HHMMSS>` next to the original. If the file doesn't exist yet, note that instead.
   3. If a `robomate` server already exists at any scope, show it. Leave exactly one user-level `robomate` entry, matching this spec. Report any project-scope `robomate` entries, but leave them alone. Don't touch other MCP servers, settings, comments or formatting: OpenCode configs may be JSONC with comments, so preserve them. Use the harness's own CLI (`claude mcp add` / `claude mcp remove`, `codex mcp add`) when it can express every setting. Otherwise, edit the file minimally.
   4. Verify. The file still parses. The harness's own listing shows the entry: `claude mcp get robomate`, `codex mcp list`, `opencode mcp list`, or the `agy` equivalent. Where a listing reports connection health, the server connects. A bridge outside a robomate worktree starts fine and only fails at `join`.
   5. For Codex, also check that the launcher's override style merges into the entry. Run `codex -c 'mcp_servers.robomate.env.HUB_HARNESS="codex"' mcp list` (adjust the quoting for PowerShell). It must succeed. Without a base entry, it fails with `invalid transport in mcp_servers.robomate`.
6. **Save the tool baseline.** Run:

   ```
   uv run --locked python scripts/check-mcp-tools.py --save
   ```

   It records this checkout's tool list as the configured baseline. It also checks Codex's config, if there is one: every tool has an approval, no approval is left for a tool the bridge no longer serves, and the entry's `--project` names ROBOMATE_CHECKOUT. It must end with `OK:` and exit 0. If it lists a problem, fix it as in step 5 and run it again.
7. **Don't:**
   - start a robomate hub, run `robomate up` or `robomate submit`, or call `join`;
   - start an agent session with a prompt;
   - read any `*.robomate/` state or token directory;
   - add global auto-approval permissions beyond Codex's per-tool approvals for `robomate`;
   - commit anything.

## Report

Give a table with one row per harness: installed version, config file, action (added, replaced, unchanged or skipped), backup path, and verification result. Then give:

- the probed server version, tool count and tool list;
- any deviation from this spec that the current docs required;
- the check script's path, and the exact command the operator runs after a `git pull`: step 6's command without `--save`, written out in full for this machine's shell;
- anything that needs the operator.

## For the operator, after setup (no action now)

- Run `scripts/start-run.py` from ROBOMATE_CHECKOUT itself, so the hub and the bridges run the same robomate code.
- Update ROBOMATE_CHECKOUT only when no robomate run on this machine is active. Every agent launch and resume starts a fresh bridge from it, and a hub doesn't detect a bridge from a different robomate version.
- After a `git pull` of ROBOMATE_CHECKOUT, with no run active, run the check command from the report. It starts the bridge, which also syncs the checkout's `.venv`.
  - `OK` (exit 0): the tool list is unchanged, so there's nothing to do.
  - `RE-RUN` (exit 1): Codex's per-tool approvals, and any Antigravity allow-list, must change to match new or removed tools. Rerun this prompt. It saves a new baseline at the end.
  - Exit 2: the bridge didn't start, or there's no baseline. Read the output.
- The check watches tool names only. If this prompt itself changes, for example because a harness needs a new setting, rerun it anyway.
