# OpenCode and AntiGravity Harness Investigation & Early Support Status Report

**Issue:** [#49](https://github.com/RoboNater/robomate/issues/49)  
**Target Milestones:** Early support in `scripts/prepare-run.py` ahead of MVP Spec §13 **M2** (`robomate install`, harness profiles) and **M3** (`robomate certify`, worker loop).  
**Branch:** `issue-49-opencode-antigravity-support`  
**Status:** Investigation complete; implementing early support in `runtimes/`, `scripts/run_common.py`, `scripts/prepare-run.py`, and `tests/test_prepare_run.py`.

---

## 1. Executive Summary

Both **OpenCode** (`opencode` v1.18.32) and **Google AntiGravity CLI** (`agy` v1.2.7) are installed and authenticated on the test host and have been empirically verified against Robomate's MCP server entry points (`hub` and `worker-mcp`).

Key findings that directly shape both early `scripts/prepare-run.py` support and upcoming M2/M3 design:

1. **OpenCode (`opencode` v1.18.32)**
   - **Run-local config isolation:** Supported natively via `OPENCODE_CONFIG=/path/to/configs/<agent>.opencode.json` (`"$schema": "https://opencode.ai/config.json"`). Authentication (`~/.local/share/opencode/auth.json`) remains intact in the user profile and does not need to be copied or linked.
   - **MCP server schema:** Configured under `"mcp": { "hub": { "type": "local", "command": ["uv", "run", ...], "environment": { ... }, "enabled": true, "timeout": 330000 } }`. The 330,000 ms timeout covers `await_assignment` / `wait_for_event` holds up to `HUB_MAX_WAIT_S` (300 s) plus transport margin.
   - **CLI flag difference between `opencode run` (auto-start) and `opencode` TUI (`--no-auto-start`):**
     - Both accept `-m` / `--model <provider/model>` and `--auto`.
     - **Critical finding:** `--variant <effort>` is accepted **only** by `opencode run`. Passing `--variant` to top-level interactive `opencode` (`--no-auto-start`) causes yargs to fail immediately with exit code 1 (analogous to `codex exec`'s `--skip-git-repo-check` caught in #45 / #48). Therefore, `--variant` must be emitted only when `auto_start=True`.
   - **Non-interactive stdin behavior & Windows `.cmd` shim:**
     - On Windows, `opencode` is installed as an npm `.cmd` shim (`opencode.cmd`), which routes `argv` through `cmd.exe` and mangles multi-line arguments. Passing a single-line prompt instruction (`"Read <prompt> and follow the instructions in it"`) with `"external_directory": "allow"` in the rendered `.opencode.json` avoids multi-line `argv` mangling across Bash, Git Bash, and PowerShell.
     - When `stdin` is a non-TTY pipe, `opencode run` reads `stdin` until EOF before starting the turn.

2. **AntiGravity CLI (`agy` v1.2.7)**
   - **Binary & version:** Invoked as `agy`; `agy --version` prints `1.2.7` (`words[0]` in `parse_harness_version`).
   - **CLI flags:**
     - Model selection: `--model <model>`
     - Reasoning effort: `--effort <low|medium|high>`
     - Workspace / prompt directory access: `--add-dir <dir>` (repeatable)
     - Unattended tool approval: `--dangerously-skip-permissions`
     - Non-interactive (auto-start) vs interactive (`--no-auto-start`): `-p <instruction>` runs in print mode (`--print-timeout` defaults to `0`, waiting until the turn completes); omitting `-p` opens the interactive TUI while accepting the same `--model`, `--effort`, `--add-dir`, and `--dangerously-skip-permissions` flags.
     - Streaming supervisor readiness (for M3): `agy` supports `--input-format stream-json --output-format stream-json` in print mode, reading one NDJSON message per line from `stdin` and running a turn for each.
   - **Run-local MCP config isolation (`agy_home`):**
     - `agy` does **not** have a `--mcp-config <path>` CLI flag. It loads global MCP servers from `~/.gemini/config/mcp_config.json` and plugin MCP servers from `plugins/<plugin_name>/mcp_config.json` (which automatically namespaces server names as `<plugin>_<server>`).
     - Furthermore, writing config files inside worker clones is prohibited by Robomate's clone hygiene invariant (`test_no_token_or_clone_path_inside_clones`).
     - **Verified isolation pattern:** Create a run-local home directory `configs/<agent>-agy` containing `.gemini/config/mcp_config.json` (mode `0600`) and link `~/.gemini/antigravity-cli/jetski_state.pbtxt` (and `settings.json` if present) via symlink or hardlink (`os.link` fallback on unprivileged Windows, identical to `codex_home()`). Launch `agy` with `HOME=<agy-home> USERPROFILE=<agy-home>` (POSIX/Git Bash) or `$env:HOME` + `$env:USERPROFILE` (PowerShell) so Go's `os.UserHomeDir()` resolves to the isolated home on both POSIX (`HOME`) and Windows (`USERPROFILE`) without touching the user's global `~/.gemini/config/mcp_config.json`.
   - **MCP tool invocation semantics:**
     - In `agy` 1.2.7, MCP servers configured in `.gemini/config/mcp_config.json` have their tool schemas written to `.gemini/antigravity-cli/mcp/<server>/<tool>.json` and are invoked by the agent through `call_mcp_tool(ServerName="hub", ToolName="<tool>", Arguments=...)`.
     - Prompts that name the logical MCP server (`hub`) and tool names (`get_state`, `check_in`, `await_assignment`, etc.) work out of the box.

---

## 2. Empirical Probe Results

| Check | OpenCode (`opencode 1.18.32`) | AntiGravity (`agy 1.2.7`) |
|---|---|---|
| `<cli> --version` output | `1.18.32` | `1.2.7` |
| Run-local config mechanism | `OPENCODE_CONFIG=<configs>/<agent>.opencode.json` | `HOME` + `USERPROFILE` -> `<configs>/<agent>-agy` with `.gemini/config/mcp_config.json` |
| Credential reuse without copying | Uses `~/.local/share/opencode/auth.json` directly | Symlink / hardlink `.gemini/antigravity-cli/jetski_state.pbtxt` (and `settings.json`) |
| `<cli> mcp list` with isolated config | `✓ hub connected` | `hub stdio enabled uv run --locked --directory ...` |
| Live MCP `get_state` tool call | Verified (`opencode run -m opencode/big-pickle --auto` -> `⚙ hub_get_state` -> `null`) | Verified (`agy -p ... --dangerously-skip-permissions` -> `call_mcp_tool(ServerName="hub", ToolName="get_state")` -> `null`) |
| Long-poll MCP timeout setting | `"timeout": 330000` (milliseconds) | `"timeoutSeconds": 330` (seconds) |
| Skill discovery for Alice | `.claude/skills/alice-orchestrator` + `.agents/skills/alice-orchestrator` + prompt fallback | `<alice-agy-home>/.gemini/config/skills/alice-orchestrator` + `.agents/skills/alice-orchestrator` + prompt fallback |

---

## 3. Implementation Plan & Status

| Step | Description | Status |
|---|---|---|
| 1 | Open GitHub issues for OpenCode/AntiGravity (#49) and GitLab (#50) | Done |
| 2 | Empirical CLI probing of `opencode` and `agy` on Windows | Done |
| 3 | Create initial status report in `docs/harness-investigation-opencode-antigravity.md` | Done |
| 4 | Add `runtimes/opencode.json` and `runtimes/antigravity.mcp.json` templates and document in `runtimes/README.md` | In progress |
| 5 | Add `opencode` and `antigravity` (`agy`) support to `scripts/run_common.py` and `scripts/prepare-run.py` | Pending |
| 6 | Add unit and integration tests in `tests/test_prepare_run.py` and run full CI validation | Pending |
| 7 | Run `scripts/prepare-run.py` end-to-end with `opencode` and `antigravity` topologies and update status report | Pending |
| 8 | Open PR referencing `Closes #49` | Pending |

---

## 4. Implications for MVP Milestones M2 and M3

1. **M2 (`robomate install` & harness profiles, `docs/mvp-spec.md` §3.1, §9.2):**
   - `agy` global MCP installation targets `~/.gemini/config/mcp_config.json` (or `agy mcp add`), and global skills target `~/.gemini/config/skills/`.
   - `opencode` global MCP installation targets `~/.config/opencode/opencode.json` (or project `opencode.json`), and skills target `~/.config/opencode/skills/` or `.agents/skills/`.
   - Wait timeout profile: Unlike Claude Code (which backgrounds MCP tool calls at ~120 s and requires `wait_seconds=100`), both `opencode` (`timeout: 330000`) and `agy` (`timeoutSeconds: 330`) support full 300 s hub long-polls when their MCP server timeout is set to 330 s.
2. **M3 (`robomate certify` & worker supervision, `docs/mvp-spec.md` §13):**
   - `opencode` single-shot `opencode run` works for interactive/manual runs, while unattended multi-turn worker supervision can use `opencode serve` + `opencode run --attach <url> --session <id>` (as proven in `scripts/poc/step6_launch.py`).
   - `agy` single-shot `agy -p` works for single-turn print runs, and `agy -p --input-format stream-json --output-format stream-json` provides a persistent multi-turn NDJSON streaming supervisor transport similar to Claude Code's `supervise-claude-code.sh`.
