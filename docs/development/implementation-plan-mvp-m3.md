# Implementation plan — MVP Milestone M3: harness breadth

**Status:** Proposed in [#505](https://github.com/RoboNater/robomate/issues/505).
Inventory checked on `origin/main` at `3247b55`. The steps apply the spec
changes alongside their implementations.

## What M3 has to deliver

The [MVP spec §13](../mvp-spec.md#13-implementation-plan) milestone row:

> | # | Milestone | Deliverable | Done when |
> |---|---|---|---|
> | M3 | Harness breadth | `robomate install` (both scopes), harness profiles, `robomate certify`, `docs/harnesses.md`; supervisor/nudge path; per-harness record of the bridge's read grant (§12) | OpenCode and AntiGravity certified; Cline attempted and recorded; Devin CLI certified, or attempted and recorded; Devin Desktop (Devin Local) attempted via `join` and recorded; one real run with a non-Claude orchestrator |

### Inventory on main

“Done” identifies a shipped slice with code and test evidence, or a verified
run where the requirement is itself a run. The inventory keeps partial
deliverables open even when their underlying mechanisms ship.

| §13 requirement or supporting slice | State | Evidence on main | Remainder |
|---|---|---|---|
| Static, unjoined MCP entry | Done | #238 / PR #259 and #239 / PR #267. `robomate mcp --harness` in `cli.py`, `worker_mcp/main.py`, and `test_unjoined_mcp_join_whoami_and_hub_selector` in `test_cli.py`. #485 / PR #535 checks user-entry presence in `mcp_preflight.py`, covered by `test_start_run.py`. | Reuse the entry and presence check in Step 4. |
| `robomate install`, user and project, `--list`, `--with-rules` | Partial | #489 / PR #490 provides a [manual setup prompt](../mcp-setup-prompt.md), `check-mcp-tools.py`, and `test_check_mcp_tools.py`. Prepared renderers in `run_common.py` and `runtimes/` still write per-run entries. | Step 4 owns both static scopes, listing, optional rules and migration. |
| Skill installation and audit (#505 addition) | Partial | #608 / PR #609 documents linking and audits user-level skills in `check-mcp-tools.py` and its tests. The tested audit detects stale copies and broken or wrong links. `prepare-run.py` also links Alice's skill to run-local profiles. | Step 5 installs and audits both scopes, carries over the interim audit and retires its entry point. |
| Native Windows `uv tool install` (#181 inheritance) | Partial | #181 / PR #187 excludes pywin32 from workspace installs and provides `packages/win32_compat`. `test_win32_compat.py` checks clean dependency state and job-object cleanup. The [compat README](../../packages/win32_compat/README.md#limits) limits that exclusion to workspace installs. | Step 2 validates the actual §2 tool environment and upgrade. |
| Shared hub clamp and long-wait transport | Done | #559 / PR #566. Workers use `HubSettings.bounded_wait` from the A2A timeout parser, and the RPC activity tracker and orchestrator wait use the same effective clamp. `test_protocol.py::test_a_requested_timeout_is_clamped_to_the_configured_ceiling`, `test_rpc.py::test_long_inbox_hold_uses_hub_clamp_for_activity_and_wait`, and `test_worker_client.py` cover the ceiling and restart. | Preserve this clamp in Step 1. |
| Harness profiles and joined-agent `max_hold_s` | Partial | #497 / PR #557 measured the layers. #559 / PR #566 ships `agent_hub_common/waits.py` and `test_measured_wait_profiles_and_precedence` in `test_worker_config.py`. The bridge selects a default. Explicit timeouts still go through a common hub ceiling. | Step 1 resolves and enforces a per-join ceiling and records the profile fields. Step 8 checks the shipped values on live clients. |
| `robomate certify` and its report | Missing | The PoC #30 endurance gate survives as `scripts/mock-alice.py --endurance` with tests in `test_mock_alice.py`. It has no operator-facing `certify` entry, automatic isolated setup or §9.3 report. #13 identifies the stale-row gap. | Step 7 brings the relevant driver and checks into the CLI. |
| `docs/harnesses.md` | Missing | The [OpenCode and AntiGravity investigation](../harness-investigation-opencode-antigravity.md) (#49 / PR #52), [Devin CLI investigation](../harness-investigation-devin-cli.md) (#224 / PR #234) and user-guide grant table contain separate observations. | Step 7 creates the index. Steps 8–9 add versioned outcomes and evidence. |
| Launcher adapters for start, resume and ask | Done | #247 / PR #463, #487 / PR #543 and #491 / PR #552. `agent-launcher.py` has headless supervision, interactive start/resume and explicit ask support or refusal. `test_agent_launcher.py` exercises exact-session resume, interactive session capture, ask with MCP off and unsupported ask. `test_start_run.py` runs a fake team through done. | Keep the shipped paths and document the limits in Step 6. |
| Supervisor command and nudge (§§5, 9.4) | Partial | `scripts/supervise-claude-code.sh` is the earlier print-mode supervisor. `agent-launcher.py` is its optional joined-run successor. `agent_hub/activity.py`, `robomate status` and `test_activity.py` distinguish bridge liveness from holds and task progress. #396 documents a real interactive stall. | Step 6 adds the operator-facing “not listening” and nudge path and resolves #465. |
| Per-harness bridge read grant (§12) | Partial | #316 / PR #394 and #395 document Linux grants. #438 / #498, PR #550, cover Ubuntu sandbox setup. #488 / PR #626 and #517 / PR #521 leave launcher controls and limits in the [user guide](../user-guide.md#hub-token-controls-by-harness) and grant cases in `test_agent_launcher.py`. | Steps 7–9 record and verify bridge and agent access separately, for each tested host and launch mode. |
| OpenCode certified | Partial | #49 / PR #52 exercised 1.x MCP. #517 / PR #521 and #497 / PR #557 add 1.x permission and hold evidence. [M5 acceptance](../evidence/m5-20261004_operator.md) used real OpenCode Alice. | Step 3 migrates the supported setup to 2.x. Step 8 certifies the exact 2.x build and settings. |
| AntiGravity certified | Partial | #49 / PR #52 exercised `agy` 1.2.7, including MCP and prepared rendering. #497 / PR #557 has longer hold trials. | Step 8 runs the full gate. #480 must be fixed or shown inapplicable before claiming native Windows SSH support. |
| Cline attempted and recorded | Missing | [§9.4](../mvp-spec.md#94-tiers) lists conditional support and the IDE risk. No Cline adapter, test or recorded attempt was found on main. | Step 9 attempts a real IDE join, continuation and certification where it can proceed. |
| Devin CLI certified, or attempted and recorded | Partial | #224 / PR #234 shipped a prototype and investigative config/flag probes. `agent-launcher.py` and its tests cover Devin launch and exact export/resume plumbing. The investigation's live model call was blocked before the join. | Step 9 uses #235 on the joined topology and records an actual attempt. |
| Devin Desktop (Devin Local) attempted via `join` and recorded | Missing | #225 defines the target and §9.1 names the shared CLI user entry. The Devin investigation records discovery and config observations. | Step 9 opens Devin Local, pastes the join line and records the result separately from CLI. |
| One real run with a non-Claude orchestrator | Done | The [operator-driven M5 evidence](../evidence/m5-20261004_operator.md) records OpenCode Alice, two merged MRs, review/CI, read-back and release on the prepared topology. [#269's joined evidence](https://github.com/RoboNater/robomate/issues/269#issuecomment-7067958951) records OpenCode Alice on the static setup route through [merged PR #619](https://github.com/RoboNater/robomate/pull/619). | Step 9 verifies the new install, profile and continuation paths together. |

## Facts from the current code that shape the plan

- `robomate mcp` is the CLI entry in `packages/cli/src/robomate/cli.py`. The
  unjoined bridge serves one catalog and defers hub discovery until a tool
  needs it. `--harness` supplies install provenance. The launched bridge's
  working directory is the agent's workspace, which discovery uses for the
  binding. `uv run --project` chooses the code and dependencies independently
  of that working directory. `start-run.py` and `agent-launcher.py` use
  `mcp_preflight.py` for the interim static-entry presence check.
- `run_common.py` renders the older per-agent JSON, TOML and environment
  blocks for `prepare-run.py`. `runtimes/README.md` describes both run paths.
  The successor launcher uses static entries and per-launch flags or
  environment, including Codex's explicit MCP environment overrides. Denise's
  `doctor` is a local tool with MCP disabled and is outside team setup.
- The wait resolver selects 100 s for unknown or old entries, 100 s for
  interactive Claude, 1500 s for Claude print, and 1800 s for Codex, OpenCode
  and AntiGravity when the advertised client budget permits. It accepts an
  operator default override. The hub validates requests and applies the
  shared cap. The observed client limits and the final shipping client
  settings have distinct evidence. [#497 findings](idle-waits-investigation.md)
  and [#567 live check](https://github.com/RoboNater/robomate/issues/567).
- The PoC driver and telemetry verifier already correlate tasks, long-task
  liveness and question timing. A successful join must also be attributable
  to the current attempt, which a leftover live-looking row can defeat.
  [#13](https://github.com/RoboNater/robomate/issues/13) and the
  [PoC lessons](../poc-lessons.md#lessons-learned-from-pr-60).
- The root workspace is currently configured with `package = false` and
  `robomate-cli` owns the console entry point. The workspace excludes
  pywin32. These facts leave a packaging gap in the Git-source tool install
  described in §2. [#181](https://github.com/RoboNater/robomate/issues/181)
  and [roadmap #2](https://github.com/RoboNater/robomate/issues/2).
- A non-Claude run counts when run evidence identifies the real orchestrator
  harness, correlates a submitted issue or statement and typed worker results
  with a reviewed head and forge merge, reads back the merge, and shows
  terminal workflow and release. Report code revision, client versions, host,
  launch mode and interventions. The M5 operator record meets the original
  clause. Its earlier [scripted run](../evidence/m5-20261002_e8b76429.md)
  proves forge mechanics. The M2 live evidence adds the joined topology.
  The M3 run will cover the new installer and the certified client build.

## Design

### Harness profiles and hold limits

Use the measured values and conditions in the [#497 findings](idle-waits-investigation.md#proposed-hold-profiles)
and the shipped #559 resolver as the starting point. Give the CLI, bridge and
hub one version-controlled profile table keyed by harness and mode. Record
supported client range, `max_hold_s`, config paths and units per scope,
approval controls, bridge and agent grants, continuation support and known
limits. Keep “supported” and “certified” distinct. Samples or documented
client defaults justify a provisional setting. Only the evidence record
establishes a certified tuple.

Bind the hold ceiling at authenticated join to the orchestrator session or
worker instance. Take the lower of the profile maximum, actual client budget
less the HTTP margin, and the operator's hub ceiling. Reject invalid
advertisements and requests. A bridge may advertise a lower budget. It may
claim a long mode only for the matching harness with install or launcher
provenance and a configured timeout budget. A missing advertisement or
unknown client uses the compatible short fallback. Adopt a new ceiling on
takeover and fall back safely if the hub restarts without the negotiated
value. Have the existing reconnect/heartbeat path re-establish a long
ceiling for that same joined instance before its next long hold.

Apply the bound when the timeout is omitted or explicit, to worker NEXT and
question waits and the orchestrator inbox, using the same effective budget
for activity deadlines and wait dispatch. Keep wakeups, event leases,
heartbeat and wall deadlines independent. Close-out collection and merge
gate have their own limits. An operator bridge default can shorten an omitted
wait, subject to the bound. Old guides' “all waits ≤100 s” regression in
[#12](https://github.com/RoboNater/robomate/issues/12) has already become
profile/default coverage in #559. Extend that coverage to explicit waits
and the per-instance clamp. Align §8 and §9.2 with the shipped values
from #497 and #559. The live gate tests final
settings, using any qualifying results from #567 for the same version,
mode and settings.

### Static installation and upgrades

Use `robomate install <harness> [--scope user|project] [--with-rules]`
as in [§9.1](../mvp-spec.md#91-install), with user scope by default and one
static server named `robomate`. Both scopes store an argv command for
`robomate mcp --harness <harness>` resolved on the harness's PATH. Keep
the entry portable, including for linked worktrees. Config may include
non-identifying client settings derived from the profile, such as
`HUB_MCP_TIMEOUT_S`. Put no secrets, operator/hub selection, per-agent
identity, machine paths or global tool permission changes in the entry.
`--with-rules` adds a scoped one-line robomate rule directing the
harness to use the configured join tool when asked. Write a named rule
file where the client supports it, or a delimited managed paragraph in
its instruction file if required. Omit it by default.

`--scope project` selects where config is written. The target project is
the current Git project root, including the root of a linked worktree.
The existing `uv run --locked --project <ROBOMATE_CHECKOUT>` selects the
*robomate source*, even when the agent works in an unrelated repository.
Keep that override out of shareable config. For checkout-backed use,
expose the selected checkout's `robomate` on the harness PATH through
its synced environment or a local launcher that forwards to the above
`uv run` command without changing cwd. Accept `install --project <path>`
as an explicit source assertion. Resolve it, check the bridge launched
from the effective PATH reports that installation, and print the PATH
remedy on a mismatch. Do the same check for a migrated path-pinned legacy
entry before replacing it. Prove these directions work from another
checkout and across Windows/WSL's different native environments. The
normal tool-install path resolves its tool executable on PATH. Treat
an unresolvable executable or shadowing scope as an install failure
and report the cause.

Edit only the named server, its own approvals and the optional managed
rule. Prefer a client command when it can express every setting. For
file updates, preserve unknown settings, comments and line endings in
JSONC/TOML, back up once per change, validate before replacing, and leave
the prior file recoverable on failure. Check higher-priority overrides
without editing them implicitly. Missing config can be created. Unsupported
scope or version gets an explicit status and leaves files intact.

| Harness | User MCP config | Project MCP config | Client settings and rules |
|---|---|---|---|
| Claude Code | `claude mcp add --scope user`, normally `~/.claude.json`, respect `CLAUDE_CONFIG_DIR`. | `.mcp.json` via `--scope project`. | Use the supported approval for this named project server. Advertise the configured budget per #559. Keep 100 s interactive behavior and enable the measured print profile when the launcher supplies the mode. Optional Claude rule in `.claude/rules/` or the user equivalent. [MCP scopes](https://code.claude.com/docs/en/mcp) |
| Codex CLI | `${CODEX_HOME:-~/.codex}/config.toml`. | Trusted `.codex/config.toml`. | `[mcp_servers.robomate]`, measured profile's client budget, currently `startup_timeout_sec = 120`, `tool_timeout_sec = 1900` and matching bridge advertisement. Generate `tools.<name>.approval_mode = "approve"` for exactly the probed catalog. Verify startup and launcher `-c mcp_servers.robomate.env.…` merges. Optional managed `AGENTS.md` instruction. [Config scopes](https://learn.chatgpt.com/docs/config-file/config-basic) and [MCP settings](https://learn.chatgpt.com/docs/extend/mcp) |
| OpenCode | `opencode.json` or `opencode.jsonc` in `$XDG_CONFIG_HOME/opencode` (when absolute), otherwise `~/.config/opencode`. | `opencode.json[c]` at the project root, with `OPENCODE_CONFIG` or inline overrides audited separately. | 1.x uses flat `mcp.robomate` and milliseconds. Use Step 3's verified 2.x schema, catalog/execution timeouts and tool exposure settings. Keep optional instructions in `AGENTS.md` at project scope or its configured user equivalent. [OpenCode config](https://opencode.ai/docs/config/) |
| AntiGravity CLI | `~/.gemini/config/mcp_config.json`. | `.agents/mcp_config.json` where the installed CLI supports it. | `mcpServers.robomate`, current client timeout `timeoutSeconds: 1900`, matching budget, and a complete `enabledTools` only where required. Approve this server's tools as needed and measure the result. Optional rule under `.agents/rules/` or the corresponding user rule directory. [MCP](https://antigravity.google/docs/cli/mcp/) and [customization discovery](https://antigravity.google/docs/customization-discovery/?tab=cli) |
| Devin CLI and Devin Local | `~/.config/devin/mcp_config.json` on POSIX and `%APPDATA%/devin/mcp_config.json` on Windows. §9.1 gives both clients the same user entry. | `.devin/mcp_config.json` for CLI. Check local overrides and use the user entry for the Desktop attempt. | `mcpServers.robomate`. Check the named tool permission and workspace trust in the selected mode. Keep the short fallback until measured. Optional named rule under `.devin/rules/` or the native user rules root. [MCP config](https://docs.devin.ai/cli/extensibility/mcp/configuration) and [Devin Local](https://docs.devin.ai/desktop/devin-local/overview) |
| Cline | For the IDE, locate `cline_mcp_settings.json` with its MCP settings UI on the extension host. | Verify the chosen IDE build's project MCP loader before offering automated project scope. Where user-only, report that limit and use its user entry for the attempt. | Follow that build's per-server timeout and per-tool approvals (`autoApprove` where supported). Record the grants and reload behavior. Offer a scoped rule only where the IDE supports it. [Cline MCP](https://docs.cline.bot/mcp/adding-and-configuring-servers) |

`install --list` is read-only and reports harness version and tier, supported
scopes, effective entry and configuration shadowing, bridge source identity,
timeout and approval state, and skill state after Step 5. Reinstall is the
upgrade operation. Start a fresh bridge, or reload a harness daemon that
caches the old catalog, after updating outside live workflows. Detect stale
paths, tooling source/revision, catalog/schema, timeout budget and scoped
approval drift. Reuse the MCP initialization and `tools/list` probe without
joining a hub. Catalog freshness includes argument schema changes with
unchanged names. Probe launch failures must be diagnosable.
The audit records a fresh installation as current, a detected mismatch
as stale, and any unprobeable effective entry as unverified. Return a
failing result for stale or unverified settings on a harness selected
for readiness or certification. A general listing can still report
clients that are absent or have never been configured.

[#485](https://github.com/RoboNater/robomate/issues/485) owns the pre-launch
presence check. Have it consume install's local discovery after Step 4.
[#504](https://github.com/RoboNater/robomate/issues/504) owns the independently
scheduled live bridge/hub compatibility check, including remote and hand
joins, and its mismatch guidance. Use its local catalog comparison in the
installer when available and carry its same-name schema test forward.
Local install auditing compares the effective entry, catalog and skills
with the selected source before join. Step 5 waits for #504's interim
checker work before removing that checker.

### Skill installation

Maintain one install manifest of current skills (`worker`,
`alice-orchestrator`) and retired names (`alice-relay`, Appendix B).
Checkout-backed entries link directly into the selected source checkout's
`skills/` directory. For a standalone tool install, ship the same skill
tree from the bridge's build checkout in the installed artifact and link
to that stable installed source tree. An editable checkout install uses
the actual checkout directories. Resolve both from the executable used
by the entry. This preserves the one-command machine install in §2 and
avoids an unrelated clone or copies with independent versions.

Install the current names into the primary directory of the selected
scope. Check other roots in discovery order for existing robomate names.
Deduplicate shared paths before changing them. A shared destination such
as `~/.claude/skills` can affect OpenCode and Claude Code. If an existing
managed link is for another build that is effective for the other harness,
show both sources and require the operator to select a common source or
fix the conflicting scope. Cross-scope checks report shadows and keep
scope changes explicit.

| Harness | User skill roots | Project skill roots |
|---|---|---|
| Claude Code | `~/.claude/skills`, or `$CLAUDE_CONFIG_DIR/skills`. | `.claude/skills`. |
| Codex | `~/.agents/skills`. Check `$CODEX_HOME/skills` (default `~/.codex/skills`) and effective roots surfaced by the client. | Root `.agents/skills`. Audit effective ancestor and nested roots between the launch directory and the checkout root. [Codex skills](https://learn.chatgpt.com/docs/build-skills) |
| OpenCode | `$XDG_CONFIG_HOME/opencode/skills` or `~/.config/opencode/skills`. Also check `~/.agents/skills`, `~/.claude/skills`, and legacy singular roots recognized by the supported version. | `.opencode/skills` with discovery checks for `.claude/skills`, `.agents/skills`, effective ancestor paths and legacy singular roots. Verify the active 2.x discovery rules in Step 3. [OpenCode skills](https://opencode.ai/docs/skills/) |
| AntiGravity | `~/.gemini/config/skills`. Also check `~/.gemini/antigravity-cli/skills` and `~/.gemini/skills` for CLI. | `.agents/skills`. [Customization discovery](https://antigravity.google/docs/customization-discovery/?tab=cli) |
| Devin | `~/.config/devin/skills` or `%APPDATA%/devin/skills`, and any shared agent roots the selected client reports. Check Desktop's effective user root separately. | `.devin/skills`, and audit `.agents/skills` where discovered. Probe both modes before claiming linked skill discovery. [Devin CLI](https://devin.ai/cli) and [Devin Local](https://docs.devin.ai/desktop/devin-local/overview) |
| Cline | `~/.cline/skills` where the selected IDE supports that root. Audit the shared agent roots and extension-host home it reads. | `.cline/skills` or the documented primary root of the selected build. Audit `.agents/skills` and legacy `.clinerules/skills` where read. [Cline skills](https://github.com/cline/cline/blob/main/docs/customization/skills.mdx) |

Use a directory junction on native Windows and a symlink on POSIX. If
link creation fails because of filesystem support or permissions, report
the cause and copy the canonical tree. Treat a matching fallback copy as
current. Detect mismatched contents, wrong checkout, broken link, missing
skill or retired entry during the same read-only audit used by
`install --list` and `certify`. Check the canonical path and the contents
of every file in each managed skill directory. Certification
requires the selected harness to discover the expected copies or links.
The primary entry's validity cannot make a stale effective skill pass.

Validate sources before changes. Move stale and retired entries into a
unique dated backup outside every discovery root, moving a link or
junction itself. Restore backups if replacement fails. Preserve
unrelated skills. Only the portable MCP config and optional portable rule
are intended for commits. Keep project-scope links and copies local and
have the installer detect tracked conflicts and exclude only its
managed destinations from local Git staging. Install those local
destinations in each worktree where project scope is used, or use the
user install across worktrees. The prepared-run per-run links under
`alice-runtime` stay until [M2 Step 11](implementation-plan-mvp-m2.md#step-11--retire-the-prepare-run-path)
retires that path. [#608](https://github.com/RoboNater/robomate/issues/608)
provides the incumbent audit and migration evidence. Appendix A's
robo-agents #82 stays superseded by this two-scope installer.

### Bridge grants and sandboxes

Record the exact version, host, client mode and configuration source.
Distinguish an MCP process outside the harness sandbox from one inside
it. A bridge inside needs the relocated hub's token-only directory, or
the single legacy token file where file-only grants work. A
directory-only legacy case needs relocation or an explicitly recorded
trusted-only run (§12). No grant should include the state or operator
credential. Use the worktree and the necessary git-common subpaths for
writes. Preserve #488's requirement that joined agents use their
configured MCP tools, including when the bridge is unavailable. The
certification harness never asks the agent for the real token path or
contents.

| Harness | Starting evidence | What the certification record must distinguish |
|---|---|---|
| Claude Code | Linux `denyRead` blocks sandboxed tool access, and the stdio bridge can still read. #395 measures narrower write/cache needs. #438 and #498 show why Ubuntu AppArmor/Bubblewrap setup matters. | Tool shell versus operator shell, bridge success under the deny, git/forge/cache checks and platform-specific sandbox availability. Reuse the host verification helper where relevant. |
| Codex | Linux `workspace-write` roots establish write grants. Path-specific read deny is not established for that launcher setup. | Actual read policy and token boundary for the version/profile in use, old `--add-dir` visibility and Windows write tests as separate properties. |
| OpenCode | 1.x launcher has narrow tool-level permissions. Shell-level enforcement has limits. | Repeat for Step 3's 2.x route, including configured read denial and shell limits. Distinguish interactive permissions from managed headless policy and document any unmeasured Windows behavior. |
| AntiGravity | Headless launcher uses the run's broad permission mode. Client supports configured read rules. | Verify actual mode and rule enforcement, and git push using the intended credentials, separately from MCP connectivity. Mark guide-only controls explicitly. |
| Devin CLI and Local, Cline | Devin CLI has a prototype and default permissions. Desktop/IDE grant and bridge behavior need live checks. | Record both successful connection and tool restrictions, or exactly where the attempt stopped, using IDE/host evidence for these clients. |

Use operator-owned disposable canaries under the same policy as the
token, state and operator paths. Prove the bridge can join and stays
alive, then have an ordinary harness tool exercise the canaries without
touching any live credential. Check permitted Git writes and forge auth
through that same harness. Record enforced, guide-only, or unmeasured
per boundary. A claimed deny that fails the probe, a required bridge
read that fails, or an unexpected grant fails the check. Where enforcement
is unavailable, scope any pass to the actual trusted-only configuration
and make the limit visible. A probe in a sandbox-disabled shell does not
establish a sandbox property. Reuse
[#395](https://github.com/RoboNater/robomate/issues/395),
[#438](https://github.com/RoboNater/robomate/issues/438),
[#498](https://github.com/RoboNater/robomate/issues/498) and
[#488](https://github.com/RoboNater/robomate/issues/488) evidence.

### Supervisor and nudge

Take [#465's per-workflow option](https://github.com/RoboNater/robomate/issues/465).
Keep `scripts/agent-launcher.py` optional and policy-free. The joined
CLI supervisor covers the workflow through terminal state and release.
The hub can keep its existing standby and queue behavior. A fresh launch
for the next workflow starts a fresh supervision context, so an old
run's worker or session log cannot authorize a new resume. Keep the
existing `resume`, `stop`, `ask` and M7 `doctor` operator entry points
in place. The PoC Claude supervisor remains a reference. A packaged
`robomate supervisor` command and persistent standby supervision across
workflows can wait until the optional launcher needs a distribution or
lifecycle change.

| Harness | Interactive start and resume | Post-run `ask` |
|---|---|---|
| Claude Code | Startup prompt and exact UUID. | Existing strict MCP-off config, fork where supported. |
| Codex | Startup prompt. Resume exact ID, with capture or operator-supplied ID. | Existing server-disable override, fork where supported. |
| OpenCode | Startup prompt and exact session. Preserve Step 3's 2.x settings. | Existing concept of a server-disable override, with 2.x proof of effective disable and fork where supported. |
| AntiGravity and Devin CLI | Launch and print the line for the operator, resume only with captured or supplied exact conversation ID. | Keep the explicit refusal until a tested per-launch switch can disable the effective entry without changing shared config. Operator can reopen the recorded conversation with the server disabled in a separate profile. |
| Cline and Devin Local | Hand-start in the worktree and paste the join or continuation prompt, using the original IDE conversation. | No launcher adapter. |

Use current hub hold, task and bridge activity for a “not listening”
status when a joined agent is live, has no active wait or task, and has
been idle past the configured threshold. Keep “stalled with open work”
distinct. Surface the name, last activity and operator continuation
hint in `robomate status`. In a running interactive session the remedy
is a “Continue.” nudge in that conversation. For an exited CLI use the
existing exact-session resume path with takeover only when that
conversation joined. Treat diagnostic state as a prompt for the operator.
It grants no workflow or role authority.

[#396](https://github.com/RoboNater/robomate/issues/396) still owns the
interactive guide reformulation and any broader notification work with
its stall follow-up. Share the status signal and reuse #346 evidence.
[#487](https://github.com/RoboNater/robomate/issues/487) and
[#329](https://github.com/RoboNater/robomate/issues/329) landed bounded
never-joined recovery and repeated non-retryable provider-error handling
(PRs #543 and #579). Include regressions in Step 6. Leave the series reset
budget in [#169](https://github.com/RoboNater/robomate/issues/169) and the
reused-hub row defect in [#546](https://github.com/RoboNater/robomate/issues/546)
on their own fixes. Regenerate live reproduction evidence if either blocks
the focused checks. [#263](https://github.com/RoboNater/robomate/issues/263)
and #520 own model-switch authorization and attribution. M3 keeps exact
recorded identity on resumes and certifies fixed model settings.
[#501](https://github.com/RoboNater/robomate/issues/501) is post-MVP.

### Certification and tiers

Use the [§9.3](../mvp-spec.md#93-certification--robomate-certify-harness)
scripted orchestrator, the operator-started real harness and a disposable
repo and hub. Start with a local installation audit. Use a fresh namespace
per attempt and fail fast on an initialized or reused hub/state
destination. Record each attempt's run and live join instance from that
attempt, fresh tasks and expected worktree. Reusing a harness account is
fine. Reusing a previous run's join, row or telemetry cannot satisfy the
gate. This addresses [#13](https://github.com/RoboNater/robomate/issues/13).
The CLI selects the profile's default mode when omitted, or accepts
`--mode` for a supported mode. Each command invocation runs one attempt
and adds it to that tuple's batch. For Devin, `--mode desktop` uses the
shared install with the Local frontend and produces a distinct report.

Settle §15's bar at **three valid attempts, all three passing**, on the
same versioned configuration. Each attempt must meet §9.3 independently,
with at least three completed assignment cycles over at least 30 minutes,
without an operator message after the initial join line. Exercise timeout
and retry, one worker question that times out and is then answered by the
scripted orchestrator, a task longer than configured `HUB_LOST_AFTER_S`
with live heartbeats, commit and push from the linked worktree, authenticated
`gh` and `glab` access, and clean release. Include an idle hold at the
published profile limit. Let early failures finish promptly and account
for them. Record setup failures separately. A failed counted attempt
means the batch fails. After diagnosis and correction, start a new
three-attempt batch and preserve the earlier results. Report raw
numerator and denominator, tool failures, harness or provider errors,
operator interventions, cleanup and any reason a probe is unverified.
Nudging an IDE explains a failure and provides continuation evidence.

Certify the exact harness version, OS and execution mode with robomate
build/revision, model/provider, timeout and permission configuration and
forge transport recorded. Published support applies only to the tested
tuple. A new harness version, operating mode or effective grant profile
needs the same gate before claiming certification. A fresh compatible
robomate release can reference prior evidence with the tested revision
visible. Re-run affected tuples when the bridge, wait behavior,
permission policy or tool/skill loading changes. `docs/harnesses.md`
indexes passed, failed/attempted and unverified tuples. Retain earlier
evidence and report withdrawn support explicitly. Follow the tiers in
[§9.4](../mvp-spec.md#94-tiers), including conditional Cline and Devin and
post-MVP Devin Cloud and RooMote.

| Item | Disposition |
|---|---|
| OpenCode 2.x | [#518](https://github.com/RoboNater/robomate/issues/518) is a prerequisite bug and compatibility migration, Step 3. [#325](https://github.com/RoboNater/robomate/issues/325) is superseded. Check its installer, background service and Code Mode questions. [#330](https://github.com/RoboNater/robomate/issues/330) remains the M7 Denise repair for poisoned 1.x conversations. Document fresh 2.x sessions and no guarantee of resuming old ones. |
| AntiGravity on native Windows | [#480](https://github.com/RoboNater/robomate/issues/480) stays a Track 1 Lane 4 bug. The reported home override is on the prepared route. A native SSH claim needs a passing test on the supported joined launcher, with the separate bug fixed where it reproduces. Linux or WSL certification can proceed with Windows SSH coverage explicitly limited. |
| Devin CLI and Local | [#235](https://github.com/RoboNater/robomate/issues/235) supplies the CLI live checklist. Carry it to the joined topology and use hub-collected report evidence where the original work expected prepared closeout files. Test Local separately through `join`. Record unmeasured timeouts, prompt/approval limits and interactive nudge outcome. Only passing tuples join the conditional tier. |
| Cline | Attempt the real IDE path, including enough time to observe turn-ending or one completed interval, then record continuation/nudge and whether the full gate was possible. A configuration observation alone is insufficient for “attempted”. |
| Codex on native Windows | Reproduce [#11](https://github.com/RoboNater/robomate/issues/11) on the currently supported version with and without the extra narrow grant. [#271](https://github.com/RoboNater/robomate/issues/271) and [#232](https://github.com/RoboNater/robomate/issues/232) are independent Track 1 Windows bugs, including TEMP/cache and closeout/report paths where applicable. If a required join or write still fails, keep that tuple uncertified pending the corresponding fix and record the limit. A Linux pass stays valid for its tuple. |

### Tool catalog

Keep one static list for M3, including before join. At `3247b55`, a
hub-free stdio probe of `robomate mcp --harness codex` initialized MCP
and issued `tools/list`. The response was 24,097 UTF-8 bytes including
JSON-RPC framing. Summing compact encodings of each `inputSchema` gives
12,165 bytes. Grouping client-side from the same capture by current hub
roles, with `join`, `whoami`, `get_statement` and
`submit_closeout_report` shared, gives:

| Catalog | Tools | Compact UTF-8 array bytes | Sum of input-schema bytes |
|---|---:|---:|---:|
| Static | 27 | 24,052 | 12,165 |
| Worker projection | 12 | 13,960 | 8,191 |
| Orchestrator projection | 19 | 12,921 | 5,079 |

These are uncompressed MCP payload bytes. Model token cost requires
client-side measurement.
The role projections retain M2's compatibility aliases where used.
Effective savings depend on initial discovery, client caching and
whether each harness updates its model context after
`notifications/tools/list_changed`. Defer a catalog change to
[M6 #22](https://github.com/RoboNater/robomate/issues/22), which owns the
context-cost measurement. Repeat with the post-M2 catalog, compare
representative harness accounting and notification support, and preserve
the pre-join surface and hub enforcement. Record the disposition in §15
when the static installer ships. The earlier
[PoC measurement](../evidence/call-bytes-20260921032414.md) counted a
different catalog and is historical context.

### Milestone order

§13 orders M2 and M7 in parallel, followed by M3, M4 and M6. The
[M2 plan](implementation-plan-mvp-m2.md#step-11--retire-the-prepare-run-path)
and [M7 plan](implementation-plan-mvp-m7-denise.md) keep Denise available
through retirement. [#269](https://github.com/RoboNater/robomate/issues/269)
was still open when #505 was filed and has since closed with operator
evidence. It is a satisfied prerequisite. [#248](https://github.com/RoboNater/robomate/issues/248)
still waits for M7 and the successor handoff. [#249](https://github.com/RoboNater/robomate/issues/249)
proves that joined launches work without prepared configs. M3's
compatibility, package and profile work can be staged against shipped
join/worktree entry points. Preserve the interim route until its
replacement and M2 retirement handoff are proven. Require #248 and #249
before taking final M3 evidence on the shipped topology.

The [M4 plan](implementation-plan-mvp-m4.md#order-launcher-handoff-and-neighboring-issues)
already permits independent Steps 2 and 6 now, with Steps 3–7 following
its own dependencies and prepared-path handoff. M4 Step 8 (#454) waits
for M3, M2 acceptance and #18, as its plan says. Flag overlapping launcher
or evidence edits during triage, and take unresolved timing changes
against §13 to the operator.

## Spec deltas

| Spec | Delta | Applies in |
|---|---|---|
| §8 and §9.2 | Replace the interim universal default wording with measured defaults, per-role request validation, per-join `max_hold_s` and the old-client fallback. | Step 1 |
| §2 | Document the verified Git-source tool install, Windows dependency exclusion, and upgrade command. If the spelling must change, get the operator's decision before changing the documented setup. | Step 2 |
| §9.1 | Document the portable command, source selection, supported client scopes and local install audit. | Step 4 |
| §8 and §15 | Retain a static catalog for M3, and move any per-role list change and client accounting to M6 #22. | Step 4 |
| §9.1 and Appendix B | Install the entry and link the skills to the same source, back up retired names, and document the local nature of project skill links. | Step 5 |
| §5, §9.4 and Appendix B | Keep the optional launcher script and per-workflow lifecycle. Document the operator nudge, exact-session recovery and current adapters. | Step 6 |
| §9.3, §12, §15 and Appendix A | Define the certification bar, evidence, install and read-grant checks and attempt identity. Carry #13 into the certifier. | Step 7 |
| §13 | Current milestone order and done-when remain in force. Record exact certification results in the living harness index. | Steps 8–9 |

## Steps

Each step is one issue and one independently reviewable PR that keeps
`main` usable. Step numbers identify dependencies and stable pickup
headings. A step that names a new test file adds it before running the
review command. Run the repository's normal validation and each changed
CLI or script entry point as well.

```text
1 Profiles ────────┐
2 Tool install ────┼─► 4 Static install ─► 5 Skills ─────┐
3 OpenCode 2.x ─────┘                                    ├─► 7 Certify ─► 8 Required results ─► 9 Acceptance
       └──────────────────────► 6 Supervision ──────────┘
```

| Edge | Reason |
|---|---|
| 1 → 4 | The installer writes settings from the profile and reports the effective budget. |
| 2 → 4 | The installed command and source identity are the entry's stable target. |
| 3 → 4 | Install the verified OpenCode 2.x schema and discovery contract. |
| 4 → 5 | Skills and config share the same effective source, scope and audit. |
| 3 → 6 | The launcher matrix and hub-off ask depend on verified 2.x behavior. |
| 5 → 7 | Certify must check the actual effective entry and linked skills. |
| 6 → 7 | Certify records continuation, including interactive evidence and clean release. |
| 7 → 8 | Use the shipped driver, reports and harness index for required certification. |
| 8 → 9 | Use certified partners and the recorded supported settings in the real-run evidence. |

Steps 1, 2 and #518 can start in parallel on the joined foundation,
within the operator's milestone schedule. After Step 3, Step 6 can run
in parallel with Steps 4–5, coordinating any changes to launcher preflight.
Step 5 also waits for #504's interim schema comparison to land, or for
triage to move that part to the shared audit before retiring the checker.
Step 7 can build on the existing isolated hub and mock test fixtures.
Both final steps need #248, #249 and the M7 handoff. Optional Cline and
Devin setup probes can run in parallel with Step 8 once Step 7 ships,
while Step 9 integrates the resulting evidence. Use #567's qualifying
runs for the exact shipping settings and use the open Windows and Devin
issues for their own triaged work.

| Step | Suggested lane | Operator interaction | DB migration | Wire `hub.schema_version` change |
|---|---|---|---|---|
| 1 | 3, large model | None | No | No (additive join metadata, §3) |
| 2 | 4, native Windows | Isolated native installation, upgrade and cleanup | No | No |
| 3 | 4, special host | OpenCode host upgrade between runs and live smoke | No | No |
| 4 | 3, large model | None for scripted checks | No | No |
| 5 | 4, native Windows | Native junction checks in isolated profiles | No | No |
| 6 | 2, medium model | None for scripted checks | No | No |
| 7 | 3, large model | None for scripted checks | No | No |
| 8 | 4, live hosts | Operator starts every certification harness | No | No |
| 9 | 4, IDE and live hosts | Operator starts the IDEs and the real team | No | No |

If a step discovers a breaking wire change or a need for additional
database persistence, stop that part and resolve it with Alice. Claim
the required counter on [#409](https://github.com/RoboNater/robomate/issues/409)
before writing the code that uses it. Close out only that claim after
merge, following #408 and the [shared-body procedure](editing-shared-bodies.md).
The plan and all no-counter steps need no claim.

### Step 1 — Harness profiles and per-join hold caps

**Scope and size.** Medium. Extend the shipped wait resolver into the
common versioned profile table and enforce the bound on the existing
worker and orchestrator hold dispatch. Advertise validated mode and
budget in additive join metadata, report the effective profile, and
keep it in hub session/instance state. Cover safe re-registration or
reconnect after restart. Compatibility for older clients means a
conservative bound. The same value drives real waits and activity.

**Dependencies and operation.** Uses #497 findings (PR #557) and #559
(PR #566), both shipped. Lane 3, no operator interaction. Keep the
existing default path and both aliases through M2 retirement. Coordinate
the additive fields with #504's join metadata changes.

**Review.** Extend explicit/omitted and low/high/old-client vectors for
worker NEXT, worker question and orchestrator event waits. Test timeout
validation, per-join selection, a profile change on takeover, a stale
instance, and restart/reconnect. Existing wakeup, stall and guide tests
remain in force. Use frozen clocks for hub deadlines.

```sh
uv run --locked pytest tests/test_worker_config.py tests/test_protocol.py tests/test_rpc.py tests/test_activity.py tests/test_worker_client.py
uv run --locked python scripts/sync-worker-prompt.py --check
```

### Step 2 — Ship the tool install on Windows and POSIX

**Scope and size.** Medium to large. Make §2's tool install produce a
runnable `robomate` from a pinned source without a workspace checkout
on PATH. Package the console entry and its runtime packages and include
the canonical skill assets for Step 5. Use installed runtime resources
as the source for packaged installs. Preserve the locked workspace
workflow. Validate a package or dependency layout that actually
excludes pywin32 from the tool environment. The workspace exclusion
alone supplies no such evidence. Keep the compatibility package where
it supplies the job-object functions. If the tool resolver requires a
different dependency arrangement, use the repository license policy
and document the supported install before marking it shipped.

**Dependencies and operation.** Independent of the profile and launcher
steps. Lane 4. Operator or a native Windows lane runs fresh install and
upgrade in disposable tool directories. Use separate POSIX and Windows
environments, Python binaries and caches when proving the routes.

**Review.** Run the following in native PowerShell from the reviewed
repository. This step adds an installed-Python test route to
`test_win32_compat.py` so the tests check the exact tool interpreter,
including subprocesses that use MCP's stdio client to start a server
and grandchild. Check no pywin32 distribution or files
(`adodbapi`, `win32`, `win32com`, `pywin32_system32`), normal shutdown
and abrupt client-death cleanup, and the package origin of
`pywintypes`, `win32api`, `win32con` and `win32job` if using the
shim. Preserve command results for both clean install and an upgrade
from the previous supported source into the reviewed one.

```powershell
$toolScratch = Join-Path ([System.IO.Path]::GetTempPath()) ([guid]::NewGuid().ToString())
$env:UV_TOOL_DIR = Join-Path $toolScratch 'tools'
$env:UV_TOOL_BIN_DIR = Join-Path $toolScratch 'bin'
$reviewed = (git rev-parse HEAD).Trim()
uv tool install "git+https://github.com/RoboNater/robomate@$reviewed"
& (Join-Path $env:UV_TOOL_BIN_DIR 'robomate.exe') --help
$env:ROBOMATE_TEST_TOOL_PYTHON = Join-Path $env:UV_TOOL_DIR 'robomate/Scripts/python.exe'
uv run --locked pytest tests/test_win32_compat.py
```

Repeat the same checks after `uv tool install --force` to the reviewed
source from the previous supported source in another isolated tool
environment. Where this is the first supported release, rehearse the
upgrade from a separately built earlier candidate and record the two
tested refs. On POSIX repeat the Git-source tool install using fresh
`UV_TOOL_DIR` and `UV_TOOL_BIN_DIR`, run `robomate --help` and
`robomate mcp --help` from outside the repository, and include a stdio
initialize and catalog test from the installed tool. Also run
`uv run --locked python scripts/dependency-licenses.py --check`.

### Step 3 — Migrate OpenCode to 2.x

**Scope and size.** Large. Reuse [#518](https://github.com/RoboNater/robomate/issues/518)
for this step. Pin an exact 2.x trial version. Verify installer and shim
detection, config and skill discovery, catalog and execution timeout
units, mode/effort and snapshot settings, session ID capture, resume,
and the shared daemon's startup, restart and per-session config. Check
the findings in superseded #325. Keep the 1.x route for pre-existing
sessions during the transition and make 2.x the certification target.
Do the host upgrade between runs with config and session data backed up.
Record a tested rollback procedure and the result of opening an old
session. Support new 2.x sessions even if old-session compatibility
remains unverified.

Redesign PR #521's permission adapter using 2.x's ordered rules, tool
exposure and path controls. Preserve narrow worktree grants, agent
token-path denial where supported, explicit safe-mode behavior and
headless refusal of unresolved approval, with actionable diagnostics.
Prove the configured rules in both TUI and headless clients. Verify
MCP removal for `ask` against the effective merged config. Prefer
direct tool exposure for the one-catalog M3 route. Only claim Code Mode
support if discovery, permissions and hold behavior work there too.
Document 2.x's supported config forms and leave an interim setup that
works before Step 4.

**Dependencies and operation.** Uses the shipped 1.x route and #517 /
PR #521. Lane 4, with operator-led upgrade, smoke and rollback
preparation. Linux or WSL first. Record native Windows shim and prompt
routing results separately.

**Review.** Exercise both schema versions, fail-closed headless
permission behavior, exact session capture, effective hub-off ask
and timeout settings. In the isolated operator smoke, start 2.x as
Alice and as a worker, join, deliver an assignment, record progress,
end one harness and verify automatic resume of the same conversation.
Exercise interactive resume and `ask` with tools disabled, and verify a
headless permission request neither hangs nor silently passes. Record
the exact version, config, session identity, process stops and residual
limits. Run the installer/config/ask matrix, including the recorded
native Windows differences where that host is available.

```sh
uv run --locked pytest tests/test_agent_launcher.py tests/test_start_run.py tests/test_check_mcp_tools.py
opencode --version
opencode mcp list
```

For the operator smoke, point `M3_TARGET` at the isolated target
checkout and use the [M2 smoke isolation](implementation-plan-mvp-m2.md#isolated-smoke-recipe).
Run the start commands in separate terminals. Queue an agreed test
statement from the target checkout. After recording automatic resume,
stop Bob's launcher before opening his interactive conversation.
Close that session before running `ask`. Stop all smoke processes on
completion.

```sh
uv run --locked python scripts/agent-launcher.py start alice --hub "$M3_TARGET" --role orchestrator --harness opencode
uv run --locked python scripts/agent-launcher.py start bob --hub "$M3_TARGET" --role implementer --harness opencode
uv run --locked python scripts/agent-launcher.py start charlie --hub "$M3_TARGET" --role reviewer --harness claude-code
uv run --locked python scripts/agent-launcher.py resume bob --hub "$M3_TARGET" --interactive
uv run --locked python scripts/agent-launcher.py ask bob --hub "$M3_TARGET"
```

### Step 4 — Install the static MCP entry

**Scope and size.** Large. Implement both scopes, `--list` and
`--with-rules` with versioned config adapters, the portable argv entry
and checkout source assertion described above. Keep unrelated config
and stricter managed policy intact. Make reinstall idempotent, inspect
the effective server and tool catalog without a hub, and have
`mcp_preflight.py` reuse that discovery for its existing presence check.
Static setup and documentation remain usable alongside the interim
prompt and checker through Step 5. Carry forward the catalog
measurement and defer dynamic lists as in the spec delta.

**Dependencies and operation.** Steps 1, 2 and 3, and the shipped #485
presence check. Lane 3, no operator interaction for fixture-based
review. Use per-harness schemas from the design and report unsupported
scopes on conditional harness versions.

**Review.** Verify first install and idempotence, selected scopes, path
and shadowing diagnostics, version detection, backup/rollback,
managed-section preservation and optional rules. Check that profile
timeout units and Codex approvals match actual bridge tools. Use
fixture homes, an isolated checkout and an MCP stdio probe. Assert that
the portable entry works when launched from a different worktree and
that a conflicting active entry produces remediation. Verify launch
preflight keeps its missing-entry refusal and skips Denise.

```sh
uv run --locked pytest tests/test_install.py tests/test_cli.py tests/test_check_mcp_tools.py tests/test_start_run.py tests/test_agent_launcher.py
uv run --locked robomate install --help
uv run --locked robomate install --list
```

### Step 5 — Link and audit the installed skills

**Scope and size.** Medium. Implement links, fallbacks, backups and
retired-name handling per [Skill installation](#skill-installation).
Keep one manifest. Extend the installer audit, listing and stale exit
status, including all effective roots and alternate paths shared by
multiple clients. Retain a no-write audit API for certification. Update
§9.1, the user guide and the joined setup in `runtimes/README.md`.
Move the relevant #489 and #608 checks into `install`, then remove
the interim prompt and `check-mcp-tools.py` and update their live links.
Keep prepared launch links and verification until #248 removes them.

**Dependencies and operation.** Step 4 and the catalog-check handoff
from #504. Lane 4, with native Windows junction verification and POSIX
symlink coverage. Use isolated user and project roots on both hosts.

**Review.** Both scopes create links into the same source the bridge
loads. Test reinstall with no duplicate edits, fallback copy and
validation of every installed skill file, backup of stale copies,
retired names, wrong and broken links, shared roots, and restore on
injected link/copy failure. Assert `--list` and the audit identify a
mismatched copy, other-checkout link, broken link, missing skill and
retired skill separately. Verify project links remain local and that
prepared-run links still work where that route exists.

```sh
uv run --locked pytest tests/test_install.py tests/test_cli.py tests/test_start_run.py tests/test_agent_launcher.py tests/test_doc_links.py
uv run --locked robomate install --list
```

While the prepared route is present, also run
`uv run --locked pytest tests/test_prepare_run.py` on both hosts.

### Step 6 — Complete the per-workflow supervisor and nudge path

**Scope and size.** Small to medium. Implement the #465 decision,
finish the operator status and continuation guidance, and document
the adapters and hub-off ask limits. Derive the status hint from
existing hub status/activity fields. Retain the hub's standby behavior
and the launcher script's exact-session recovery. Keep the queued,
terminal and released cases distinct and guard against adopting old
supervision state on the next launch. The spec delta records the
decision on the subcommand and standby. Respect the M7 operator
recovery handoff.

**Dependencies and operation.** Step 3 and shipped #247, #487, #491
and #329. Lane 2, no operator interaction for the fake-harness checks.
Reuse #346 observations and route actual harness evidence to Steps 8–9.
Triage can resolve #465 when this implementation and the evidence
listed in that issue are recorded.

**Review.** A live bridge with no task or hold gets a nudge hint only
after the configured idle threshold. A healthy hold,
active/stalled task, released agent and disconnected bridge have
distinct output. Test CLI resume after a known join, never-joined
startup failure, stop and exact-session recovery, second workflow
without inherited supervision, and supported/unsupported hub-off
ask paths. Preserve the no-MCP doctor test.

```sh
uv run --locked pytest tests/test_activity.py tests/test_cli.py tests/test_agent_launcher.py tests/test_start_run.py tests/test_worker_launcher.py
uv run --locked python scripts/agent-launcher.py --help
```

### Step 7 — Implement the scripted certifier

**Scope and size.** Large. Add the operator CLI and text report for
§9.3, using the reusable mock endurance and joined-path logic from
`mock-alice.py` and `mock-worker.py`. Exercise the real bridge entry,
current join, and linked worktree path. Start only an isolated hub and
refuse a reused attempt namespace before consuming any agent or task
rows. Drive tasks, question/retry, activity checks, idle hold, release,
forge and permission canaries. Put an authority-free join line and a
clear setup failure or failed gate in the report. Close the scratch
hub before returning on success, failure or interruption. Save
`certify/<harness>-<version>-<date>.md` under robomate's resolved
native user state (normally `~/.local/state/robomate/` on POSIX), with
unique attempt IDs and safe append or a new file for a new batch.

Create `docs/harnesses.md` with the exact version/mode/grant and tier
columns, including guide-only and unverified markers and links to
evidence. Reference the single grant record from the user guide.
Use the install audit from Step 5. Retain any mock CLI used by other
scripts and make its stale-state precondition agree with the certifier
where it still runs the same endurance gate.

**Dependencies and operation.** Steps 5 and 6. Lane 3. Scripted
validation uses fake harness and forge fixtures and needs no operator
to start a real harness. Actual certification in Steps 8–9 always
requires an operator-started harness.

**Review.** Use short, fixture-driven attempts with an injected clock.
Test stale row/telemetry rejection even with matching names, failed
skill audit, expected real join identity, task liveness through the
lost threshold, question timeout and redelivery, grant check failures,
forge authentication/commit/push, partial batches and result counting.
Test clean release and cleanup on early failure and interrupt. Verify
report attribution and omission of secret values and raw transcripts.
Confirm the production command enforces the §9.3 duration.

```sh
uv run --locked pytest tests/test_certify.py tests/test_mock_alice.py tests/test_mock_worker.py tests/test_cli.py tests/test_doc_links.py
uv run --locked robomate certify --help
```

### Step 8 — Certify the required CLI harnesses

**Scope and size.** Small evidence PR, with substantial live host
time. Certify OpenCode 2.x and AntiGravity. Confirm the Claude Code
and Codex MVP tier on their exact supported configurations too. Record
separate mode/host rows and precise configuration. Three valid passing
attempts on the same tuple are required for a certification claim.
Re-use #567 live hold results where they match the recorded shipping
tuple. Run the native Windows Codex #11 probe, and include the #271 /
#232 write checks before any Windows pass claim. AntiGravity native
Windows SSH coverage follows the #480 disposition above.

**Dependencies and operation.** Step 7, M7 handoff and #248–#249.
Lane 4. Operator starts each harness, configures test credentials,
pastes the join line and records the attempt. Use isolated native
profiles and a disposable forge repository. Read the GitLab operator
requirements in [#69](https://github.com/RoboNater/robomate/issues/69)
before running its authenticated checks.

**Review.** After the scripted checks, the operator starts each command
from the designated certification environment and starts its harness
on the intended host when prompted. Repeat per the certification bar.
For each client run `robomate install --list` and its native version
command in that environment. Attach sanitized reports to the evidence PR.

```sh
uv run --locked pytest tests/test_certify.py tests/test_doc_links.py
robomate certify claude-code
robomate certify codex
robomate certify opencode
robomate certify antigravity
```

| Harness | Operator prerequisites |
|---|---|
| All | Exact reviewed robomate build, Git/uv/Python, disposable repo and push permission, isolated tool/client state, authenticated `gh` and `glab` for their test hosts, and a verified approved network path if the harness is remote. |
| Claude Code | Authenticated account and model, Linux/WSL host with working sandbox if claiming it. Use the supported print supervisor or interactive start for the mode tested. |
| Codex | Authenticated CLI, trusted workspace and scoped approvals. Linux/WSL for the baseline. Native PowerShell, Windows-native CLI and writable TEMP/cache for #11, #271 and #232. |
| OpenCode | Step 3's exact 2.x build, provider login and fixed model, CLI/config settings and documented daemon state. Use Linux/WSL unless separately including a native Windows tuple. |
| AntiGravity | Authenticated `agy`, model and required grants on Linux/WSL. For a Windows SSH claim use native PowerShell and an authenticated SSH remote in the intended host profile. |

Add one file under `docs/evidence/m3-certify-<harness>-<version>.md` per
version, with sections for OS/mode, commands, tuple, outcomes, why any
attempt was excluded, cleanup and report excerpts. Add a suffix for
additional tuples where needed. Cross-link from the harness index.
Use the evidence structure of [M5 Step 5](implementation-plan-mvp-m5-gitlab.md#step-5--m5-acceptance-run-on-gitlab-boxlocal),
which distinguishes scripted and operator-driven runs.

### Step 9 — Record conditional harness attempts and the real non-Claude run

**Scope and size.** Small evidence PR, with multiple operator sessions.
Attempt Cline and Devin Local via `join` from the actual IDE worktree.
Run #235's Devin CLI checks on the joined path or record the first
observable blocker and the attempted commands. Check tool approval,
long wait where viable, session identity and recovery, Windows quoting,
grants and write/forge checks as applicable. For a functioning CLI,
follow #235 with a real Devin worker and a Devin Alice on separate
runs, and report their joined-path results. Record the first live
attempt separately from the full three-attempt certification claim.
Describe every failure, including early stop and operator intervention.
Physical absence of a usable host or account remains a prerequisite gap.

Run an ordinary reviewed change to completion with an OpenCode 2.x
or other certified non-Claude orchestrator and real companion agents
using the shipped static entries and skills. Use `robomate up`, one-line
joins and `robomate submit`. Record tool and skill load, selected
versions, topology, identity, submitted statement digest, review and
merge-gate evidence, forge read-back and final worker release. Report
restarts and operator actions if any. The earlier M5 run supports
the clause independently, and this run verifies the M3 topology.

**Dependencies and operation.** Step 8, M7 handoff and #248–#249.
Lane 4. Operator starts the real team and the live attempts, opens the
IDE worktrees and pastes every join line. Use the shared disposable
forge setup from Step 8. Cline needs the selected VS Code extension
version, provider login and settings from the extension host. Devin CLI
needs a Cognition account and an enabled model, plus Win/WSL targets
for the supported #235 matrix. Devin Local needs a native supported
desktop installation and an account with Local enabled. Use its
CLI-shared user entry on that host. Record the exact extension/client
version, including OS and architecture.

**Review.** Run the scripted commands first. Then start the real clients
on the scoped entries and save the observable join, continuation,
where the attempt stopped, and any passes. After starting the real
team in the disposable target, submit the agreed sandbox statement
and capture the final status and forge read-back. The operator
tears down the target hub with `robomate down`.

```sh
uv run --locked pytest tests/test_certify.py tests/test_doc_links.py
robomate install --list
robomate certify cline
robomate certify devin
robomate certify devin --mode desktop
```

For Devin Local, paste the printed join line in the Local chat.
For the real run, put the authorized scratch statement at the absolute
path in `M3_STATEMENT` and run from the disposable target checkout.
After `up`, create or open each agent's worktree and give the three
real harnesses their one-line joins before submitting.

```sh
robomate up --agents alice,bob,charlie
robomate submit "$M3_STATEMENT"
robomate status --json
robomate down
```

Keep `m3-certify-cline-<version>.md`, `m3-certify-devin-<version>.md`
and `m3-certify-devin-local-<version>.md` as distinct evidence under
`docs/evidence/`. Add `docs/evidence/m3-non-claude-<run-id>.md` for
the real run. Link the outcomes from `docs/harnesses.md`. A scripted
team or an MCP listing alone cannot stand in for a live attempt.

## Risks

| Risk | How the plan handles it |
|---|---|
| Harness overrides hide a correct config, change a timeout or load an old skill. | Read the effective config and probe the client. Audit shared and alternate skill roots and run Step 8 against exact shipping settings. |
| User and project scopes select different bridge versions on one machine. | Report the effective source and pending shadowing, retain portable entries, verify both skill provenance and the #504 live compatibility boundary. |
| An OpenCode daemon or schema upgrade changes visibility, prompts or grants. | Execute the migration between runs, prove both modes and exact conversation recovery, and keep a documented 1.x rollback path. |
| Warm rows or incomplete Windows sandbox probes create a false pass. | Require an attributable current attempt, use fresh disposable state and measure both permitted and denied actions in the actual harness. |
| Packaging reintroduces the banned payload on the machine tool path. | Check distributions, files, import origins and child cleanup through the installed interpreter on a native host, including upgrade. |
| Step 9 requires access to multiple products and special hosts. | Schedule operator windows during Step 7, reuse independently scheduled evidence where the tuple matches, and retain attempted/unverified states with reasons. |

## Roadmap bookkeeping

After this plan merges, triage opens one short issue per step with
the `M3 Harness breadth` milestone, the `track-2-mvp` label and a
stable heading link. Reuse #518 on its existing Track 1 Lane 4
placement with a reference to Step 3, and link the independent work
from its dependent steps. Record the cross-track relationships in #2.
The plan supplies step details when a pickup issue is abbreviated.

Replace the unplanned milestone placeholder with the plan and step
headings. Carry the #181 inheritance into Step 2, link #12 to Step 1's
coverage and #13 to Step 7, and link #465's outcome to Step 6. Link #235
and the Windows follow-ups to the relevant live evidence steps. Leave
roadmap, open issue text, reservations and the owner's §13 order to
their existing owners. Update milestone completion only when the
reviewed evidence meets the quoted done-when.
