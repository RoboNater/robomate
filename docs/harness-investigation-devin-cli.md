# Devin CLI Harness Investigation

**Issue:** [#224](https://github.com/RoboNater/robomate/issues/224)  
**Precedent:** [#49](https://github.com/RoboNater/robomate/issues/49) / PR #52 ([`harness-investigation-opencode-antigravity.md`](harness-investigation-opencode-antigravity.md))  
**Branch:** `issue-224-devin-cli`  
**Status:** Partial. The static questions are answered with evidence below.
The live questions are **open**: the investigating agent's own harness
refused to start a live Devin agent session (see [§0](#0-blocker)). By
operator decision (relayed by Alice on the #224 task), a **prototype** was
still added to the run path from the verified static findings and Devin's
documentation, with fake-harness tests ([Prototype](#prototype)). It is
unverified against a live Devin agent until the live run below is done.

## Hosts and versions

| Host | OS | Devin CLI |
| --- | --- | --- |
| Windows (native) | Windows 11 Home 10.0.26200 (build 26200.9457), Git Bash `MINGW64_NT-10.0-26200` | `devin 3000.11.3 (9c803229faa4)` at `%LOCALAPPDATA%\devin\cli\bin\devin.exe` |
| WSL2 on the same machine | Ubuntu 24.04.2 LTS, kernel `6.18.54.1-microsoft-standard-WSL2` | `devin 3000.11.92 (7f4e64980e22)` at `~/.local/bin/devin` |

Both installs are signed in on the free tier, which offers only
`swe-1.6-slow` (the default). No other model was selected.

The two hosts run different builds (`3000.11.3` versus `3000.11.92`). The
Windows build did not update itself during the investigation.

## 0. Blocker

The investigating worker ran under Claude Code in auto mode. Its permission
classifier refused:

- any live agent run, such as
  `devin --model swe-1.6-slow --respect-workspace-trust false -p "Reply with exactly the word PONG"`
  (reason given: "Create Unsafe Agents");
- any probe of Devin's sign-in state under an isolated config directory, such
  as `devin auth status`, and reads of the operator's
  `%APPDATA%\devin\config.json` (reason given: "Credential Exploration").

Help output, `devin mcp list`, `devin list --format json`, flag parsing
checked with `devin <flags> --version`, and hand-written config files in
scratch repositories were allowed. Everything marked
**verified** below comes from those. Everything marked **open** needs a live
agent run, done by the operator or by an agent whose harness allows it.

This is the same class of problem as the issue's own subject: an agent harness
whose default permission policy stops unattended work. It is recorded here as
evidence, not as a defect in Devin.

## 1. Per-agent MCP config without touching the operator's

**Verified (Windows, 3000.11.3):**

- `devin mcp add --help` names three scopes: `local`
  (`.devin/mcp_config.local.json`, "not committed", the default), `project`
  (`.devin/mcp_config.json`) and `user` (`~/.config/devin/mcp_config.json`).
- The global `--config <PATH>` flag overrides only the user `config.json`
  ("Override the default user config file (~/.config/devin/config.json)").
  It does not carry MCP servers: `devin --config <file> mcp list` printed
  "No MCP servers configured" for a file holding `mcpServers`, `mcp_servers`,
  `mcp.servers` or `mcpConfig.mcpServers`.
- An isolated user scope is **not reachable through the environment on
  Windows.** With a `devin/mcp_config.json` placed under each of the
  overridden directories, `devin mcp list` printed "No MCP servers configured"
  for `APPDATA=<scratch>`, `XDG_CONFIG_HOME=<scratch>` and `HOME=<scratch>`.
  `USERPROFILE=<scratch>` failed with `Error: Failed to determine config
  directory`. The CLI resolves its config directory through the Windows known
  folder, not the environment. Its sign-in file is in the same directory
  (`devin auth status` before the classifier refusals printed
  `C:\Users\<user>\AppData\Roaming\devin\credentials.toml`), shared with
  Devin Desktop.
- No `DEVIN_*` variable naming a config directory appears in the binary; the
  ones present cover model, permission mode, sandbox, harness and cloud
  sessions.
- **Local scope works outside git, and stops at a git root.** A
  `.devin/mcp_config.local.json` in a plain directory was listed from that
  directory and from a subdirectory. From a git repository nested in that
  directory, the parent's file was **not** listed, and the nested repository's
  own was. So an Alice config in `RUN_DIR/alice-runtime/.devin/` cannot leak
  into a worker clone.
- The [MCP configuration docs](https://docs.devin.ai/cli/extensibility/mcp/configuration.md)
  confirm the three files and the Windows user path
  (`%APPDATA%\devin\mcp_config.json`), say "the local config file is
  automatically excluded from git", and list the stdio fields as `command`,
  `args`, `env` and `disabled`: there is **no per-server timeout field**.
  Before v3000.3, servers lived under `mcpServers` in `config.json`; newer
  versions migrate them out, which is consistent with `--config` not
  carrying them.
- **Local scope keeps the clone clean.** In a scratch repository, a
  hand-written `.devin/mcp_config.local.json` was listed by `devin mcp list`,
  and that command **appended `.devin/mcp_config.local.json` to
  `.git/info/exclude`**. `git status --porcelain` stayed empty, and
  `git check-ignore -v` names `.git/info/exclude` as the reason. Rerunning
  preparation therefore would not trip the dirty-clone check (#97), provided
  preparation writes the same exclude line itself before Devin first runs.

**Open:**

- Whether an isolated `XDG_CONFIG_HOME` works on Linux, and whether the sign-in
  survives it (where Linux keeps the credentials was not checked; if they move
  with the config directory, a link or copy as in #189/#190 would be needed).
  The prototype does not need it: local scope works on both platforms.
- Whether `devin -p` loads the local scope from the clone it starts in.

**Prototype choice:** write the robomate server into the agent's working
directory at `.devin/mcp_config.local.json` (and its permission rule at
`.devin/config.local.json`), and add both paths to a clone's
`.git/info/exclude` at preparation rather than relying on Devin to do it. The file names the agent's token
*file* in the run directory, never the token, which satisfies the clone
hygiene test (`test_no_token_or_clone_path_inside_clones`). Never write the
user scope: on Windows it is the operator's own Devin and Devin Desktop
configuration.

## 2. The robomate MCP server

**Open.** `devin mcp list` accepts a stdio server with `command`, `args` and
`env`. Whether both `robomate mcp --role worker` and `--role orchestrator`
connect, and whether their tools run without a prompt, needs a live run.

**Permissions hazard (from #224's comment, PR #227 finding r1-1):** the
[Devin Local permissions documentation](https://docs.devin.ai/desktop/devin-local#permissions)
says the default configuration prompts before every MCP tool call. The CLI
help confirms the default is `--permission-mode auto`, which "auto-approves
read-only tools" only. A headless `--print` run cannot answer a prompt.

The [permissions reference](https://docs.devin.ai/cli/reference/permissions.md)
puts rules in `.devin/config.local.json` (above project and user config),
with `mcp__server__*` matching all of a server's tools; with no matching rule
the user is prompted. It does not say what print mode does with a prompt.

The candidate unattended setups, for the live run to choose between:

1. `--permission-mode dangerous` (or `DEVIN_PERMISSION_MODE=dangerous`):
   approves every tool, including shell and file writes. Simplest; the
   broadest grant.
2. `permissions.allow` rules such as `mcp__robomate__*` plus the shell and edit
   rules a worker needs. Narrower, but needs a config location the agent
   reads; given §1, that is the local scope or `--config`.

Whichever is chosen, record it in the run's report and in the future §9.2
harness profile as a **known grant/certification hazard**: a Devin agent
certified under one approval setup is not certified under another, and the
default setup cannot run unattended at all.

## 3. Hold limit

**Open.** No timeout option is shown by `devin mcp add --help` for stdio
servers. Until a live probe holds `await_assignment` / `wait_for_event` at
increasing durations, use Claude Code's conservative `max_hold_s` of 100 s.

## 4. Turn-ending in `--print` mode

**Partly verified from help.** `-p, --print` is "Print response and exit …
processes the prompt and exits". So a Devin agent in print mode ends when its
turn ends, as `claude -p` does, and the "never end your turn to wait" guidance
applies. **Open:** whether `swe-1.6-slow` follows that guidance through a long
task. The issue owner notes the model may not reason well; keep its prompts
short.

## 5. Session IDs

**Partly verified from help.**

- `-r, --resume [<SESSION_ID>]` resumes a given session (interactive picker
  when omitted). `-c, --continue` resumes the latest in the directory and must
  not be used (several agents can share a directory tree).
- `--model` with `--resume` switches the resumed session's model; without it
  the saved model is kept.
- `devin list --format json|csv` lists the sessions of the current directory
  (an empty repository printed `[]`). That is the likely capture point for
  `<agent>-sessions.jsonl` after a crash.
- `devin rm <id|name|prefix>` accepts names and ID prefixes.
- `devin acp --agent-type summarizer` documents
  `~/.local/share/devin/summaries/<session_id>.md`, so sessions have IDs under
  `$XDG_DATA_HOME/devin` on Linux.

**Open:** the exact ID format (for `exact_id` in `scripts/agent_recovery.py`),
whether `--print` prints it, whether one can be supplied up front (no flag for
that is shown), and whether `--resume <id>` restores the conversation in both
modes.

## 6. Sandbox and write access

**Partly verified from help.** `--sandbox` is a research preview using macOS
seatbelt or Linux bwrap+seccomp; with it, commands "can write only within the
workspace and granted `Write(...)` scopes". It has no Windows backend listed.
Writing `RUN_DIR/closeout/<agent>.md` outside the clone (#220) therefore needs
either no `--sandbox` or a `Write(...)` grant for the closeout directory.
`--respect-workspace-trust=false` is needed in print mode outside a trusted
folder ("Non-interactive (print) mode cannot show the trust prompt and fails
in an untrusted directory").

**Open:** commit and push from the clone, `gh`/`glab` network access, and the
closeout write under the chosen permission mode.

**Verified (flag parsing):** `--permission-mode bogus` is rejected with
"Valid options: normal (auto), accept-edits, dangerous (yolo, bypass),
autonomous (requires --sandbox)". `autonomous` needs `--sandbox`, which has no
Windows backend, so it is not an option for a cross-platform run.

## 7. Provider lineage

**Verified by reading `scripts/run_common.py`.** `resolve_provider` checks an
explicit `--<agent>-provider`, then OpenRouter routes, then model families,
then `<maker>/<model>`, then single-vendor harness defaults, then `unknown`.

Devin is multi-vendor (`--model` takes Claude, Codex and Cognition's own SWE
models), so it must **not** be added to `PROVIDERS`. With that:

- `--model claude-opus-4.6` resolves to `anthropic` and `--model codex…` to
  `openai` through the existing families.
- `swe-1.6-slow` matches no family and resolves to `unknown`. The base-model
  lineage of Cognition's SWE models isn't published with the CLI, so this
  report does not add a `swe-` family; the operator can name a maker with
  `--<agent>-provider`.
- Under Fusion (the binary's `DEVIN_HARNESS_LEAD_ONLY` /
  `DEVIN_HARNESS_SIDEKICK_ONLY` switches suggest a lead and a sidekick model),
  one agent's work comes from two models. Record `unknown` unless the operator
  gives `--<agent>-provider`, so `reviewer_provider_differs` never claims a
  difference it can't establish.

## 8. Windows

**Verified:** the native CLI is a single `devin.exe` (no npm `.cmd` shim, so no
`cmd.exe` argument mangling of the kind OpenCode has). Its config directory is
`%APPDATA%\devin` (Roaming), shared with Devin Desktop, and can't be
redirected by environment variables (§1).

**Open:** PowerShell quoting of a `--print` prompt, and a run with the hub in
WSL2 and on native Windows.

## Findings for M2 and M3

- **M2 `join` / M3 `robomate install devin --scope user`:** the user scope is
  `%APPDATA%\devin\mcp_config.json` on Windows (help text says
  `~/.config/devin/mcp_config.json`; Linux path to confirm). Devin Desktop's
  Devin Local agent reads the same file, so an install there reaches both.
  `devin mcp add --scope user robomate -e … -- uv run … robomate mcp …` is the
  documented way to write it.
- **Hold limit:** unknown; use 100 s until measured.
- **Turn-ending:** print mode exits at the end of the turn.
- **`robomate certify`:** must record the approval setup (§2) as part of what is
  certified.

## Prototype

Built on the verified findings above and the documented flags; every flag
spelling was checked to parse with `devin <flags> --version` on 3000.11.3.

| Piece | What it does |
| --- | --- |
| `--<agent>-harness devin` | Accepted by `prepare-run.py` and `prep-standard-run-area.py`. `--<agent>-effort` is refused: Devin has no effort flag. |
| MCP config | `RUN_DIR/alice-runtime/.devin/` for Alice (`--role orchestrator`), `<clone>/.devin/` for a worker (`--role worker`, same `env` block as the other harnesses): `mcp_config.local.json` plus `config.local.json` allowing `mcp__robomate__*`. Both paths are added to the clone's `info/exclude` before writing, so `git status --porcelain` stays empty and reruns add no second line. Alice's skill is linked at `.devin/skills/alice-orchestrator`, with the usual prompt fallback. |
| Headless launch | `devin [--model M] --permission-mode dangerous --respect-workspace-trust=false --export RUN_DIR/<agent>-devin-export.json -p "Read <prompt> and follow the instructions in it"` under `alice-launcher.py` / `worker-launcher.py`. |
| Resume | `… --resume <id> -p …` (automatic and `resume-<agent>`); never `-c`. |
| Session ID | Read after each exit from the top-level `session_id` of the `--export` file. Stdout is not searched: a relayed hub result can carry robomate's own `session_id`. A stale export is deleted before a fresh start, so it can't name the new session. If no ID is found the launcher stops with `no_conversation`, as for the other harnesses. |
| `exact_id` | `[A-Za-z0-9][A-Za-z0-9._-]{2,127}`, matching the documented `brisk-otter` and `abc12345` forms and refusing `-c`. |
| Recovery config check | `<workdir>/.devin/mcp_config.local.json` must exist. |
| Interactive (`--no-auto-start`) | `devin [--model M]`; the hub tools are allowed by `config.local.json`, everything else keeps Devin's own prompts. |
| Ask (`ask-<agent>`, #221) | `devin [--model M] --resume <id>` from the same working directory, with `agent-ask.py` swapping `.devin/mcp_config.local.json` for an empty server map and restoring it on exit, as for AntiGravity. No `--fork` (none documented). |
| Hold limit | Devin prompts say "Use a 100 second wait time for all hub waits", as for Claude Code, until §3 is measured. |
| Provider | `devin` is not in `PROVIDERS`; `swe-1.6-slow` resolves to `unknown` (§7). |

The assumptions and risks a live run must check, most consequential first:

1. `dangerous` lets `-p` call robomate tools without a prompt.
2. `--export` writes a JSON file whose top-level `session_id` is the ID
   `--resume` takes. If not, the launcher stops with `no_conversation` and
   the operator resumes by hand from `devin list --format json`.
3. `-p` loads the local scope of the directory it starts in.
4. A worker that runs `git clean -fdx` deletes its own `.devin/` config; rerun
   preparation to restore it.

## What a live run must still do

On each host, as the operator or an agent allowed to start one:

1. In a clone prepared with `--bob-harness devin`, run
   `devin --model swe-1.6-slow --permission-mode dangerous --respect-workspace-trust=false -p "Call the robomate tool get_role_guide with role implementer"`
   and confirm the call runs without a prompt. Repeat for Alice's
   `alice-runtime` with `get_state`, and once with only the
   `config.local.json` rule (no `dangerous`).
2. Hold `await_assignment` at 100, 150, 200, 280 s to find the timeout.
3. Add `--export <file>` to step 1 and compare the file's top-level
   `session_id` with stdout and `devin list --format json`; resume it with
   `--resume <id>` in print and interactive mode.
4. Commit, push and `gh pr view` from the clone, and write a file outside it.
5. Repeat step 1 from PowerShell with a multi-word prompt.
6. The issue's live acceptance: one sandbox run with a Devin worker, and one
   with a Devin Alice, alongside certified harnesses, reaching a merged PR
   with the Devin agent's report in `RUN_DIR/closeout/` and a clean clone.
   Record the version, model, observed hold limit and interventions here.
