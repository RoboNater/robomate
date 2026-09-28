# OpenCode and AntiGravity Harness Investigation & Early Support Status Report

**Issue:** [#49](https://github.com/RoboNater/robomate/issues/49)  
**Target Milestones:** Early support in `scripts/prepare-run.py` ahead of MVP Spec §13 **M2** (`robomate install`, harness profiles) and **M3** (`robomate certify`, worker loop).  
**Branch:** `issue-49-opencode-antigravity-support`  
**Status:** Complete; PR [#52](https://github.com/RoboNater/robomate/pull/52) rebased onto post-[#51](https://github.com/RoboNater/robomate/pull/51) `main` and updated per review feedback.

---

## 1. Executive Summary

Both **OpenCode** (`opencode` v1.18.32) and **Google AntiGravity CLI** (`agy` v1.2.7) are installed and authenticated on the test host and have been empirically verified against Robomate's MCP bridge (`robomate mcp --role worker|orchestrator`).

Key findings that directly shape both early `scripts/prepare-run.py` support and upcoming M2/M3 design:

1. **OpenCode (`opencode` v1.18.32)**
   - **Run-local config isolation:** Supported natively via `OPENCODE_CONFIG=/path/to/configs/<agent>.opencode.json` (`"$schema": "https://opencode.ai/config.json"`). Authentication (`~/.local/share/opencode/auth.json`) remains intact in the user profile and does not need to be copied or linked.
   - **MCP server schema:** Configured under `"mcp": { "robomate": { "type": "local", "command": ["uv", "run", "--locked", "--project", "<root>", "robomate", "mcp", "--role", "worker|orchestrator"], "environment": { "ROBOMATE_HUB_URL": "...", "ROBOMATE_TOKEN_FILE": "...", ... }, "enabled": true, "timeout": 330000 } }`. The 330,000 ms timeout covers `await_assignment` / `wait_for_event` holds up to `HUB_MAX_WAIT_S` (300 s) plus transport margin.
   - **CLI flag difference between `opencode run` (auto-start) and `opencode` TUI (`--no-auto-start`):**
     - Both accept `-m` / `--model <provider/model>` and `--auto`.
     - **Critical finding:** `--variant <effort>` is accepted **only** by `opencode run`. Passing `--variant` to top-level interactive `opencode` (`--no-auto-start`) causes yargs to fail immediately with exit code 1 (analogous to `codex exec`'s `--skip-git-repo-check` caught in #45 / #48). `prepare-run.py` therefore rejects combining `--<agent>-effort` with `--no-auto-start` for an OpenCode agent up front with an actionable `ValueError`.
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
   - **Run-local MCP config & home isolation (`agy_home`):**
     - `agy` does **not** have a `--mcp-config <path>` CLI flag. It loads global MCP servers from `~/.gemini/config/mcp_config.json` and plugin MCP servers from `plugins/<plugin_name>/mcp_config.json` (which automatically namespaces server names as `<plugin>_<server>`).
     - Furthermore, writing config files inside worker clones is prohibited by Robomate's clone hygiene invariant (`test_no_token_or_clone_path_inside_clones`).
     - **Verified isolation pattern:** Create a run-local home directory `configs/<agent>-agy` containing `.gemini/config/mcp_config.json` (mode `0600`) and link `~/.gemini/antigravity-cli/antigravity-oauth-token` (Linux OAuth token, mode `0600`), `jetski_state.pbtxt` (Windows state), `settings.json`, and `~/.git-credentials` (for HTTPS `credential.helper store` users) via `link_credential()` (symlink or hardlink fallback on unprivileged Windows, shared with `codex_home()`).
     - **Operator git/gh/uv config without linking dot-directories:** Instead of symlinking `.ssh` or `.config` into `configs/<agent>-agy` (where `zip -r` on the run directory would follow symlinks and capture SSH keys or `gh` tokens), `antigravity_launch()` pins `GIT_CONFIG_GLOBAL` (and on POSIX `XDG_CONFIG_HOME` / `XDG_CACHE_HOME` / `XDG_DATA_HOME`; on Windows `XDG_*_HOME` are left unset so `gh` and `uv` keep their `%APPDATA%` / `%LOCALAPPDATA%` paths) to the operator's real home before overriding `HOME` and `USERPROFILE` for `agy`.
     - **PowerShell caller-session hygiene:** On Windows, `start-<agent>.ps1` saves `$oldHome = $env:HOME; $oldProfile = $env:USERPROFILE; $oldGitConfig = $env:GIT_CONFIG_GLOBAL`, sets `$env:GIT_CONFIG_GLOBAL` to `$oldHome\.gitconfig` (if `$oldHome` was set) or `$env:USERPROFILE\.gitconfig` when unset, and restores all three in a `try { ... } finally { ... }` block.
   - **MCP tool invocation semantics:**
     - In `agy` 1.2.7, MCP servers configured in `.gemini/config/mcp_config.json` have their tool schemas written to `.gemini/antigravity-cli/mcp/<server>/<tool>.json` and are invoked by the agent through `call_mcp_tool(ServerName="robomate", ToolName="<tool>", Arguments=...)`.
     - Prompts that name the logical MCP server (`robomate`) and tool names (`get_state`, `check_in`, `await_assignment`, etc.) work out of the box.

3. **Model-Maker Lineage (`HUB_PROVIDER` / `resolve_provider`)**
   - Per `docs/poc-spec.md` §2.4 and `docs/mvp-spec.md` §5.2, `provider` records model-maker lineage (e.g. `anthropic`, `openai`, `google`, `alibaba`, `zhipu`, `moonshot`, `deepseek`, `xai`, `mistral`, `meta`, `minimax`) so `reviewer_provider_differs` compares who trained the models rather than which harness or routing gateway (`opencode/`, `openrouter/`) served them.
   - `resolve_provider()` checks in order: (1) explicit `--<agent>-provider`, (2) the `<maker>` segment of `openrouter/<maker>/...` (mapped through `MAKER_ALIASES` or preserved as written), (3) known model families on the model slug (`MODEL_FAMILIES`), (4) `<maker>/<model>` via `MAKER_ALIASES`, (5) single-vendor harness defaults (`claude-code` -> `anthropic`, `codex` -> `openai`, `gemini` -> `google`), and (6) `"unknown"` (never fabricated; surfaced in `report["checks"]["provider"]` when a worker resolves to `"unknown"`).

---

## 2. Empirical Probe Results

| Check | OpenCode (`opencode 1.18.32`) | AntiGravity (`agy 1.2.7`) |
|---|---|---|
| `<cli> --version` output | `1.18.32` | `1.2.7` |
| Run-local config mechanism | `OPENCODE_CONFIG=<configs>/<agent>.opencode.json` | `HOME` + `USERPROFILE` -> `<configs>/<agent>-agy` with `.gemini/config/mcp_config.json` |
| Credential & dotfile reuse without copying | Uses `~/.local/share/opencode/auth.json` directly | Links `antigravity-oauth-token`, `jetski_state.pbtxt`, `settings.json`, `.git-credentials`; pins `GIT_CONFIG_GLOBAL` (and POSIX `XDG_*_HOME`) to real home |
| `<cli> mcp list` with isolated config | `✓ robomate connected` | `robomate stdio enabled uv run --locked --project ... robomate mcp --role ...` |
| Live MCP `get_state` tool call | Verified (`opencode run -m opencode/big-pickle --auto` -> `⚙ robomate_get_state` -> `null`) | Verified (`agy -p ... --dangerously-skip-permissions` -> `call_mcp_tool(ServerName="robomate", ToolName="get_state")` -> `null`) |
| Long-poll MCP timeout setting | `"timeout": 330000` (milliseconds) | `"timeoutSeconds": 330` (seconds) |
| Skill discovery for Alice | `.claude/skills/alice-orchestrator` + `.agents/skills/alice-orchestrator` + prompt fallback | `<alice-agy-home>/.gemini/config/skills/alice-orchestrator` + `.agents/skills/alice-orchestrator` + prompt fallback |

---

## 3. Implementation Plan & Status

| Step | Description | Status |
|---|---|---|
| 1 | Open GitHub issues for OpenCode/AntiGravity (#49) and GitLab (#50) | Done |
| 2 | Empirical CLI probing of `opencode` and `agy` on Windows and Linux | Done |
| 3 | Create and maintain status report in `docs/harness-investigation-opencode-antigravity.md` | Done |
| 4 | Add `runtimes/opencode.json` and `runtimes/antigravity.mcp.json` templates and document in `runtimes/README.md` | Done |
| 5 | Add `opencode` and `antigravity` (`agy`) support to `scripts/run_common.py` and `scripts/prepare-run.py` | Done |
| 6 | Add unit and integration tests in `tests/test_prepare_run.py` and run full CI validation | Done |
| 7 | Run `scripts/prepare-run.py` end-to-end with `opencode` and `antigravity` topologies and update status report | Done |
| 8 | Open PR [#52](https://github.com/RoboNater/robomate/pull/52) referencing `Closes #49` and address review feedback | Done |

---

## 4. Implications for MVP Milestones M2 and M3

1. **M2 (`robomate install` & harness profiles, `docs/mvp-spec.md` §3.1, §9.2):**
   - `agy` global MCP installation targets `~/.gemini/config/mcp_config.json` (or `agy mcp add`), and global skills target `~/.gemini/config/skills/`.
   - `opencode` global MCP installation targets `~/.config/opencode/opencode.json` (or project `opencode.json`), and skills target `~/.config/opencode/skills/` or `.agents/skills/`.
   - Wait timeout profile: Unlike Claude Code (which backgrounds MCP tool calls at ~120 s and requires `wait_seconds=100`), both `opencode` (`timeout: 330000`) and `agy` (`timeoutSeconds: 330`) support full 300 s hub long-polls when their MCP server timeout is set to 330 s.
2. **M3 (`robomate certify` & worker supervision, `docs/mvp-spec.md` §13):**
   - `opencode` single-shot `opencode run` works for interactive/manual runs, while unattended multi-turn worker supervision can use `opencode serve` + `opencode run --attach <url> --session <id>` (as proven in `scripts/poc/step6_launch.py`).
   - `agy` single-shot `agy -p` works for single-turn print runs, and `agy -p --input-format stream-json --output-format stream-json` provides a persistent multi-turn NDJSON streaming supervisor transport similar to Claude Code's `supervise-claude-code.sh`.
