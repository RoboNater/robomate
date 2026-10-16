# Implementation plan — MVP M7: Denise, an interactive doctor agent

Status: proposed. Planning issue [#174](https://github.com/RoboNater/robomate/issues/174);
roadmap [#2](https://github.com/RoboNater/robomate/issues/2).

This plan turns [mvp-spec §5.1](../mvp-spec.md#51-denise-interactive-operator-assistant-m7)
and [§13](../mvp-spec.md#13-implementation-plan)
into PR-sized implementation steps, following the M1 and M5 plans. The spec is the design
of record. This issue delivers prose only; commands and files described as proposed below
are interfaces to implement, not features already shipped. Each implementation PR updates
the spec if it settles a remaining choice differently.

## Outcome and boundaries

M7 is the next milestone, before M2. Preparing a run through
`scripts/prep-standard-run.sh --config run-config.toml` (PowerShell: `.ps1`) generates a
Denise start script without starting her. When trouble occurs, the operator opens a new
terminal, runs `start-denise`, and gets an interactive assistant who knows which run to
inspect and where robomate and the target project's instructions live. The hub may be down.
The normal team has no dependency on Denise, her terminal, or her credentials.

Denise diagnoses, explains and performs requested recovery within a distinct operator
assistant role. She is not a fourth worker, a backup Alice, an always-on monitor, or a hub
workflow engine. M7 does not deliver #18's automatic reconciliation, replace #19's standby
protection or #95's activity reporting, or certify a new harness for ordinary team roles.
It uses [#173](https://github.com/RoboNater/robomate/issues/173)'s guarded resume path.
Future role instructions do not authorize current workers to recover the run's hub or
agents, inspect its state, or answer on the operator's behalf.

## Facts from current code

Baseline inspected: `70250d8e63100f966cc9d7c5a50337760ddeb22d` (PR #234).

| Area | Constraint and implication |
| --- | --- |
| `scripts/prep-standard-run-area.py` and the `.sh`/`.ps1` wrappers | The flat TOML schema rejects unknown keys, merges CLI over config over defaults, and forwards Alice/Bob/Charlie settings to `prepare-run.py`. Denise needs new accepted keys and forwarding; adding only a prompt cannot work. Config generation does not start or contact a hub. |
| `scripts/prepare-run.py` | Generates configs, prompts, start/resume/ask scripts and `run.json`. Team loops and role maps name Alice/Bob/Charlie explicitly. Do not append Denise to those loops: that would provision a bridge, supervision or task identity she must not have. |
| `run.json` | Schema 1 records `hub_id`, `hub_repo`, `state_dir`, `workspaces`, `configs`, `launch.agents`, and network metadata. `launch.agents.<name>.sessions` points to host-local JSONL when supervised. It does not yet record a separate doctor context or a reliable robomate source path. Add optional metadata without making old manifests unreadable. |
| Robomate source versus target | Standard preparation clones the target into `RUN_DIR/hub`, and invokes helpers from its own `ROOT`. That hub checkout can contain no robomate code. Record the actual tooling root separately; launching from the hub checkout is insufficient. |
| `scripts/agent_launcher.py`, `agent_recovery.py`, `agent_sessions.py` | These own session IDs, predecessor checks, locks, backoff and recovery prerequisites. #173 landed in PR #175. Reuse checks and supported resume scripts; do not implement a second takeover algorithm. #18's full reconciliation is still separate work. |
| `scripts/agent-ask.py`, `prepare-run.py:ask_lines` (#221, PR #233) | Hub-free interactive sessions and per-harness adapters already exist. Reuse command construction and isolation patterns, but start a fresh Denise session; do not resume Bob's conversation or copy worker settings wholesale. Interactive mode alone does not remove an inherited MCP server or unattended permissions. |
| `scripts/run_common.py` and runtime templates | Existing helpers render model/effort, isolated homes, credentials and sandbox roots. Current worker configs approve coordination tools and some launch paths skip permissions. Denise needs a distinct config; neither those approvals nor bypass flags should be inherited. Test actual Codex extra-directory write grants on native Windows. #11 concerns withheld MCP tools, which Denise does not use; that symptom is not a doctor acceptance check. |
| Discovery and authentication | `robomate status --json`, `status --snapshot` and `inbox --json` provide inspection; `down` and `answer` need the separate machine operator credential. Orchestrator RPC changes the current session; Denise must not instantiate that bridge or call orchestrator methods. Filesystem access does not authenticate a command. |
| Local and remote preparation | `prepare-run.py` supports remote worker bundles and records network/host paths. `prep-standard-run-area.py` exposes a smaller local interface. A local filesystem grant does not provide SSH access or map a Windows git worktree into WSL. Initial Denise runs on the hub host. |
| Observability | #95 separates Alice's activity from bridge liveness; #19 landed host-pause grace, with live checks still outstanding. Session logs, harness logs and hub logs may be absent or host-local. Preserve this uncertainty instead of treating an old heartbeat or a missing log as proof of death. |

## Design choices and interfaces

### Configuration and generated artifacts

Use the current flat key convention in standard-run TOML and equivalent hyphenated CLI
options in both preparation entry points. Proposed defaults:

```toml
denise_settings_from = "bob"   # alice | bob | charlie | none
denise_harness = ""           # empty: inherit selected agent's harness
denise_model = ""             # empty: inherit when using the same harness
denise_effort = ""            # empty: inherit when using the same harness
```

Resolution: CLI overrides TOML; then resolve against the selected agent's final settings.
If the harness is overridden to another harness, empty model/effort use that harness's
defaults, not a potentially invalid inherited model. With `settings_from = "none"`, an
explicit harness is required and empty model/effort use harness defaults. Validate
configuration structure and known option capabilities early, naming a remedy. Persist
effective values and the inheritance source so a later config edit cannot silently change
a generated consultation. Do not infer model identity from a vendor or claim an unknown
effort value.

Always generate the optional start script on supported combinations, regardless of team
`auto_start`. There is no Denise auto-start or automatic recovery loop. If an inherited
harness is not yet supported for Denise, prepare the team normally, skip `start-denise`,
and warn with a concrete supported `--denise-harness` override. Record the skipped state
and reason in the optional manifest object; do not point to a nonexistent script. "Explicit
harness" means a nonempty `denise_harness` CLI/TOML override, not merely choosing
`denise_settings_from`. Resolve and test the following defaults in Step 1:

| Input | Result |
| --- | --- |
| Empty `denise_harness`, with default or explicitly chosen `denise_settings_from` naming an agent whose harness lacks doctor support | Inherited: prepare the team, skip Denise with a warning and supported override remedy. An explicit source selector does not turn inheritance into an explicit harness override. |
| Nonempty `denise_harness` naming a harness without doctor support | Preparation error naming that override and supported alternatives. |
| Model/effort values, inherited or explicitly supplied | Forward through the existing adapters without a new model/effort allowlist or provider probe; the harness validates values when Denise is started. Never silently substitute a different value. |
| Nonempty explicit `denise_effort` where the interactive harness has no supported way to render an effort option | Preparation error naming the unsupported option. This is an adapter capability check, not validation of the effort value. |
| Inherited effort with no supported interactive representation | Keep team preparation working; skip the doctor profile with a warning and an override remedy. |
| Invalid selector/type, or `denise_settings_from = "none"` without the required harness | Configuration error, independent of the unsupported-inheritance rule. |

Initially implement interactive Claude Code and Codex on Bash/native PowerShell; then
cover OpenCode and AntiGravity before marking M7 complete. Devin remains explicitly
unsupported for Denise until its interactive configuration and access behavior are tested;
its existing team prototype and #235 do not establish doctor support. A team using Devin
can choose an explicit supported Denise harness.

Proposed files, all inside the selected run:

| File | Purpose |
| --- | --- |
| `start-denise.sh` / `start-denise.ps1` | Pin the run and tooling root, set a dedicated home/config, invoke an interactive harness, propagate its exit code. Native Windows also follows the existing Git Bash wrapper convention. |
| `denise/context.json` | Small, non-secret path/identity/settings index; separates host names and host-local paths. |
| `denise/startup.md` | Short generated briefing pointing to the context index and local guide. No log dumps, tokens, copied worker etiquette, or task-loop prompt. |
| `denise/workspace/`, `denise/configs/`, `denise/tmp/` | Stable consultation working directory, isolated harness configuration/home and temporary files. No target branch checkout needed. |
| `denise/reports/<UTC-time>-<unique-id>.md` | One consultation's diagnosis/actions/verification and outstanding items. Unique creation avoids overwriting an earlier report. |

Add a separate optional top-level `denise` object to `run.json`: context/startup/script,
workdir/config/report paths, effective settings, tooling root/version and local guide path.
Include a ready/skipped status and skip reason, with script paths only when generated.
Keep it outside `launch.agents`; status/accounting must not mistake Denise for a joined
agent. Store only paths to credentials where needed by existing tooling, never secret
contents. No DB or wire change is expected; before any future incompatible manifest change,
audit consumers and document migration. Existing runs are not rewritten by preparation.
The initial supported adoption path is a newly prepared run; an add-only retrofit helper
for old runs is an open choice below. Never rerun preparation to fix a live run.

### Context discovery and diagnosis without a hub

Preparation records robomate's real `ROOT` and version/commit separately from `hub_repo`.
The start helper checks the saved tooling root, manifest identity and local paths without
requiring HTTP or an MCP handshake. A relocated/missing tooling root needs an explicit
operator-supplied replacement validated as robomate; do not search arbitrary checkouts and
silently switch versions. A missing manifest yields a concrete restoration recommendation,
without inventing a run or starting a fresh workflow.

The context index identifies the selected run and expected hub ID, hub checkout/state,
target origin and instructions, Alice/Bob/Charlie workspaces and host locations, original
config if known plus generated configs, start/resume/ask scripts, sessions and available
logs, local recovery docs, robomate `AGENTS.md`, code and `docs/denise/guide.md`. Find target
instructions in the actual affected checkout and follow applicable nested instructions for
any edit. Do not assume a target `AGENTS.md` exists. Probe known log paths rather than
promising a single universal harness transcript location.

At launch, report stale/missing references and create a report skeleton. Denise reads more
on demand. Record observations with capture times and provenance: HTTP unavailable means
unreachable from this host, not stopped; snapshot time is not process exit time. When the
hub is down, inspect local metadata, workspace state and logs without initializing or
migrating the DB. Use a SQLite-consistent read-only inspection/backup if necessary; never
copy a live database alone without its WAL and claim it is a valid snapshot. Restore
reachability before operations whose existing guards require live hub state. Avoid raw DB
writes; no schema repair or migration engine belongs in M7.

### Filesystem access, authentication and confidentiality

Build a resolved access inventory for this run: generated run files/report directory,
selected hub checkout/state, relevant local agent workspaces and each actual git common
directory (`git -C <checkout> rev-parse --path-format=absolute --git-common-dir`). Resolve
linked worktrees, bare common directories and symlinks; `<worktree>/.git` may be a file.
Grant only needed roots, not a shared run parent or every checkout discovered on the host.
Robomate code/docs are read context, not an automatic write grant to modify the installation.
Remote paths remain host/path pairs, with access marked unavailable unless the operator
has provisioned a supported connection. Never guess a drive/WSL translation.

The generated profile permits selected-run repairs without repeatedly hitting filesystem
blocks, while retaining interactive decisions for consequential operations. Verify each
harness's real read/write behavior; a list of paths in a prompt is not sandbox enforcement.
If a harness cannot express scoped grants, document the trusted-only mode and its broader
effective reach. Do not quietly disable its sandbox or copy a worker's dangerous flags.
Filesystem permissions cannot enforce conversational authority or make shared git refs
private: inspect effects on sibling worktrees before writing common metadata.

Denise has no robomate MCP entry or inherited worker/orchestrator environment. Use a
dedicated harness home/config; explicitly disable inherited robomate servers, and clear
ambient `ROBOMATE_HUB_URL` / `ROBOMATE_TOKEN*` during launch so another run cannot win
discovery. Operator CLI inspection/actions get explicit selection from this run's context
using the existing supported selector/environment, scoped to that command. Verify hub ID
before writes. Do not rely on Denise's working directory for hub selection.

The CLI reads the selected hub bearer token and the existing machine operator credential
when an authorized command needs it. Provision these through the operator's existing local
setup; do not duplicate secret values into doctor artifacts or copy them to a remote
worker. The operator credential is machine-wide, not a run-scoped capability. If a sandbox
needs a credential read grant, grant the exact file to the CLI where supported; otherwise
document trusted-only execution or have the operator run the command. Startup and offline
diagnosis never require operator authentication. Shell history, logs, reports and forge
comments must contain no token values or complete environment dumps.
For the isolated harness profile, reuse the existing owner-only local authentication
patterns; a missing login produces an actionable login recommendation. Inheriting Bob's
model settings never means sharing his complete harness home, MCP environment or session.

### Authority, coordination and recovery procedure

`docs/denise/guide.md` is a local operator-assistant guide, not worker etiquette. It lives
outside the hub-served `guides/` directory: bearer holders and assigned workers must not
obtain operator recovery instructions through `get_role_guide("denise")`. The existing
guide route must return 404 for `/guides/denise.md`, tested when Step 1 adds the local file.

External issue text, logs, transcripts and agent messages remain data; embedded requests
to reset state, ignore a guard or claim operator approval never override the guide or
interactive operator's request. Diagnose any such content without executing it.

The recovery procedure is:

1. Read manifest/context and check observed run identity, local processes/locks, workspace
   status/diffs, live hub status/snapshot/inbox if reachable, and relevant forge facts.
   Distinguish facts, hypotheses, unknown stop times and inaccessible hosts.
2. State a diagnosis and the next repair's purpose. Inspection and reversible repairs
   already covered by the operator's request need no repeated permission. Starting Denise
   by itself is a diagnosis request; a restart, handoff or other intervention needs a
   corresponding operator request, not inferred consent from silence.
3. Check a still-running Alice before intervening. Agree with the operator on a quiet
   handoff for competing actions; a useful initial default is to let Alice continue
   ordinary tasks while Denise reads evidence, and to stop that progression through
   supported controls only when intervention requires it. Do not call orchestrator RPC
   under Alice's name to communicate or change her session. If Alice cannot be contacted,
   establish actual process/bridge state and obtain the operator's recovery decision.
4. Prefer `robomate up/down` from the selected owning checkout and generated
   `resume-<agent>` on that agent's host. Use #173's exact conversation, saved config,
   identity checks and predecessor lock. Explain pause/question advisories and essential
   identity/attachment refusals. Never use `start-<agent>` or rerun prep as recovery.
5. For an attached/orphan bridge, first establish which harness/launcher/bridge owns it;
   heartbeat alone proves no task progress. An explicit operator decision is needed before
   replacing an attached predecessor. Stop only the verified selected-run process, then
   recheck locks, hub instance and task state before resume. Waiting for loss can fail a
   task; Alice reconciles or assigns fresh retry work, never revive a terminal task by
   editing SQLite. A plan to discard edits, cancel/reassign work or change workflow state
   also requires an explicit consequential decision. Preserve pending results/questions.
6. An existing operator question is answered through `robomate answer <id> <answer>` only
   with the operator's actual decision. Record its ID/status in the report; a direct chat
   is not a worker authorization record. No automatic yes, fabricated answer, review/CI
   bypass or competing merge. If a needed state mutation has no supported operator path,
   explain it and leave it to the operator/Alice rather than impersonating Alice or
   inventing a privileged doctor API.
7. Verify the intended effect and preservation of identity/work, and return control to
   Alice. Update the local report with material decisions, actions, results and remaining
   limitations; point to evidence rather than copying logs. Preserve interrupted reports,
   and begin a fresh dated report on a later consultation. Reporting never requires a hub
   write, a worker result, an automatic forge post or retention cleanup.

### Topology and dependency strategy

| Path | Initial default and completion requirement |
| --- | --- |
| Local Linux/Bash and native Windows/PowerShell | First slice; separate robomate source and target repository, Claude Code/Codex. Both get automated coverage and live acceptance. |
| Windows Git Bash | Render using existing quoting/path conventions; execute generated wrappers with a fake harness on Windows and record live coverage separately. |
| Hub host with remote workers | Generate Denise on the hub host; include remote bundle/session paths as remote references. Local diagnosis/startup must work without contacting remote hosts. Operator may run guarded resume on the worker host or provide an existing trusted SSH connection; no automatic credential distribution or remote process control. |
| Remote-only doctor terminal | Deferred: not required for M7. Explain how to start Denise on the hub host instead. Do not claim local grants reach remote state. |
| OpenCode/AntiGravity | Follow the first slice with tested interactive configs and access grants on supported hosts. No MCP and no headless worker loop. Needed before M7 completion. |
| M2 replacement of preparation | Carry the context/guide/profile generation forward behind the new preparation/join experience; use the state resolver and recorded identity, not hard-coded `.robomate`. Denise remains optional and outside join. |

No dependency on future M2 join, state relocation or M3 certification. No new package is
expected. Changes needing a dependency follow `docs/dependency-licenses.md`. No counters
are reserved by #174: an implementation that needs a DB or wire schema change must reserve
on #2 before editing. An optional manifest field alone does not consume those counters.

## Spec deltas and remaining choices

This planning PR updates §1/§2 (optional help and instruction/checkout exceptions), §5.1
(role contract), §7.5/§8/§12 (recovery, tool and authority boundaries), §13 (M7 before M2),
and Appendix B (M2 migration preserves Denise). Existing worker guides remain in force.
The implementation steps add the local doctor guide, templates and documentation, without
changing current worker permissions. Record meaningful later changes in their PRs and
update the design of record; no additional scope-locking mechanism is needed.

| Choice to validate in implementation | Sensible initial default |
| --- | --- |
| Reusing interactive command builders versus a new helper module | Extract small shared argument/config builders from #221; keep doctor lifecycle outside `agent_launcher.py`. |
| Harness-specific scoped write grants, especially native Codex | Prefer explicit roots in the isolated config; test extra-directory reads/writes on native Windows before claiming isolation. #11's missing MCP tools are unrelated to Denise's no-MCP profile. Document any trusted-only harness/OS combination. |
| Adopting Denise for already-prepared runs | New runs first; consider an explicit add-only doctor renderer that reads existing metadata, never reruns team preparation or changes existing scripts. Not required for initial M7. |
| Conversation reuse | Fresh consultation with previous local reports as context. A Denise-only resume can follow if useful; never reuse a team conversation or overwrite its JSONL. |
| Report retention | Retain locally with the run; no automatic deletion, DB storage, or raw transcript collection. Future #23 retention policy can explicitly include reports. |
| Future privileged tool surface | None in M7. Use existing CLI; unsupported privileged actions stay with the operator rather than designing a new authentication layer. |

## PR-sized steps

One issue and one PR per step, opened when picked up; keep `main` usable after each. Step 1
precedes Step 2; Step 3 extends Step 2; Step 4 records acceptance after all three. The first
slice may document its supported matrix without claiming the whole milestone complete.

### Step 1 — Settings, context and access inventory

Add flat config/CLI options, resolution tests, optional manifest metadata and a doctor
renderer separated from team loops. Generate the local context, short startup template,
report directory and `docs/denise/guide.md` outside the served directory. Record the
source/target distinction and resolved git common directories. Cover spaces/quotes,
worktrees and bare layouts, missing instructions, stale paths, remote references, no secrets
and legacy manifests. Config-generation mode
must stay side-effect-free with respect to repositories/hubs. Do not advertise a working
start script until Step 2 supplies it. Test that an unsupported inherited harness (Devin,
and OpenCode/AntiGravity before Step 3) does not fail team preparation, records the skip
and remedy, and emits no broken doctor script. Cover both default and explicit
`denise_settings_from` selectors, nonempty explicit harness overrides, and the effort
capability cases above. Verify model/effort values are forwarded without a new allowlist.
Add a guide-route test proving `/guides/denise.md` and worker `get_role_guide("denise")`
cannot serve the local operator guide, while ordinary role guides remain available.

Reviewer commands (proposed test module introduced by this step):

```sh
uv run --locked pytest tests/test_prep_standard_run_area.py tests/test_prepare_run.py tests/test_denise.py tests/test_guides.py
uv run --locked python scripts/prep-standard-run-area.py --generate-default-config <fresh-config.toml>
uv run --locked python scripts/prepare-run.py --help
```

Verify default output and CLI override precedence without a live run. Add fixtures for
the context index and old/new manifests; assertions check behavior and identities rather
than coupling every test to rendered whitespace. No changes to Alice/Bob/Charlie task
profiles, start/resume/ask semantics, or authority.

### Step 2 — Interactive start on Claude Code and Codex

Generate functional Bash/PowerShell wrappers and isolated doctor profiles with no bridge,
team identity or unattended flags. Refresh local availability observations at launch,
create a unique report skeleton and propagate harness failures. Missing HTTP is diagnostic,
not a startup blocker. Bound missing-path/config errors with concrete remedies. Document
the actual grant behavior for each supported host, including Codex extra-directory write
grants on native Windows; Denise does not exercise #11's MCP-withholding symptom.
Cover generated scripts by executing them with fake harness executables, not just parsing
their text. Prove no HTTP is needed at startup, no worker check-in/orchestrator takeover
occurs, no team session file changes, and repeated consultations keep separate reports.
Carry forward Step 1's unsupported-inheritance tests through the emitted start-script
path. In this same first-runnable PR, clarify `AGENTS.md`'s role wording for Denise's
explicitly requested recovery (including leaving a restored run hub running), while
retaining joined workers' restrictions and the isolated-smoke-only exception.

```sh
uv run --locked pytest tests/test_denise.py tests/test_prepare_run.py tests/test_runtime_content.py
bash scripts/prep-standard-run.sh --config <sandbox-config.toml>
bash <run-dir>/start-denise.sh
```

On native Windows, run `pwsh -NoProfile -File scripts/prep-standard-run.ps1 --config
<sandbox-config.toml>`, then `pwsh -NoProfile -File <run-dir>/start-denise.ps1`. The prep
wrapper forwards arguments to Python and uses `--config`, not a PowerShell `-Config`
parameter. Preparation requires the selected sandbox hub running as usual; starting Denise
after stopping it must still work.
Automated tests use isolated test hubs/fake harnesses. Live CLI grants and interactive UX
are separate operator evidence, not inferred from fake-harness tests.

### Step 3 — Remaining interactive harnesses, remote context and docs

Add OpenCode/AntiGravity interactive doctor profiles; test the no-MCP configuration,
model/effort flags and effective access independently. Cover Windows Git Bash wrappers and
remote-worker metadata, honest unavailable evidence and a concrete worker-host resume
recommendation. Add usage/troubleshooting to `docs/user-guide.md`, recovery links in
`docs/development/agent-recovery.md`, `guides/README.md`'s pointer to the separate local
`docs/denise/guide.md` (never copied into the served directory), and
standard-run config/launcher help and fixtures. Document supported versus trusted-only or
deferred combinations, credential setup, reports, consequential decisions and M2 handoff.

```sh
uv run --locked pytest tests/test_denise.py tests/test_prepare_run.py tests/test_prep_standard_run_area.py tests/test_runtime_content.py tests/test_agent_recovery.py
```

Run Step 2's prep/start commands for each supported harness/host; on Windows also execute
the generated `start-denise.sh` under Git Bash. Representative remote-worker commands,
with the networked sandbox hub already started by the operator and the token file
provisioned through the existing trusted path:

```sh
# Hub host, from the robomate source checkout; --denise-harness is proposed by Step 1.
uv run --locked python scripts/prepare-run.py --repository <target-origin> \
    --hub-repo <hub-checkout> --run-dir <hub-run-dir> --issue <issue> \
    --account <account> --remote-worker bob --denise-harness codex
# Worker host, from its robomate source checkout; do not pass hub-only flags here.
uv run --locked python scripts/prepare-run.py --worker-only bob \
    --repository <target-origin> --run-dir <worker-run-dir> \
    --hub-url <reachable-hub-url> --token-file <existing-token-file>
# Hub host: remote evidence must remain explicitly attributed to the worker host.
bash <hub-run-dir>/start-denise.sh
```

Use corresponding native paths and `.ps1` wrappers on Windows. Confirm Denise labels
worker-host evidence correctly. Record exact host-specific commands in the implementation
PR. No real remote credential
provisioning, process termination or live operator action is delegated to a worker.

### Step 4 — Operator acceptance and evidence

Run the following scenarios on disposable runs, preserving artifacts and failures as well
as successes. Record exact preparation/start/recovery commands, robomate commit, OS/shell,
harness version/settings, original/final run and hub identities, decisions, report paths
and sanitized evidence in `docs/evidence/m7-<run-id>.md`. Do not tick M7 complete from unit
tests alone. Any substantive production defect gets its own scoped fix PR.

| Scenario and operator commands | Expected result and evidence |
| --- | --- |
| Separate target project: prepare with `bash scripts/prep-standard-run.sh --config <config>` or the documented `.ps1` equivalent; run `<run-dir>/start-denise.sh` / `.ps1` | Interactive consultation, correct target instructions and separate robomate source/docs; no hand-assembled briefing, no extra joined agent and no edits to team conversations. Repeat on Bash and native PowerShell, and record each harness's actual support. |
| Hub unavailable: operator uses `robomate down` in the selected hub checkout, then runs `start-denise`; later requests restoration with `robomate up` in that same checkout | Startup/diagnosis/report work without HTTP or MCP. Restore only when requested; verify the same hub ID/state/port and no fresh workflow. Distinguish a stopped hub from unreachable remote HTTP. Stop disposable hubs after the exercise. |
| Agent returned to shell: operator verifies exit, starts Denise, requests `<run-dir>/resume-bob.sh` (or corresponding agent/`.ps1`) on its host | Preserve workspace/config and exact conversation; verify live hub/task identity and existing pending result, not a replayed kickoff or a second active agent. Explain a completed/released task instead of resuming it. |
| Attached/orphan predecessor: keep a sandbox predecessor attached and request resume | Existing lock/attachment refusal is explained. Denise requests an explicit decision before terminating the identified orphan. After the operator handles it, recheck and use guarded resume. No kill by guessed PID or forced duplicate; document any task lost while waiting. |
| Pending question or pause: inspect with `robomate inbox --json` and `robomate status --json`; try relevant `resume-alice` and worker resume scripts | Explain role-specific advice; Alice may need to resume to handle the question. No automatic answer/status change. If the operator decides, use `robomate answer <id> <answer>` and verify answered status, then leave normal workflow progression to Alice. An advisory override never bypasses identity/attachment checks. |
| Preserved uncommitted work: make a known sandbox worker edit, save `git -C <workspace> status --short` and `git -C <workspace> diff`, interrupt the agent and recover through Denise | Same branch/workspace and edit after recovery; preserve IDs and pending questions/results. No reset, clean, new clone, DB deletion or prep replay. Report verification and any unresolved ambiguity. |
| Remote evidence unavailable: deny access to a worker host, then start Denise on the hub host | Useful local diagnosis; remote logs/status explicitly unknown, with worker-host commands recommended. No copied token or assumed path translation. Later operator-provided access does not silently change the run being operated on. |

## Validation and CI

Run the repository validation sequence on the planning change and each implementation PR:

```sh
uv sync --locked --all-packages --dev
uv run --locked ruff check .
uv run --locked ruff format --check .
uv run --locked mypy
uv run --locked pytest
uv run --locked python scripts/dependency-licenses.py --check
uv run --locked python scripts/dependency-licenses.py --update && git diff --exit-code docs/
```

For the final license-update drift check, first commit intended documentation edits so
`git diff --exit-code docs/` measures generated drift, not the planned prose change.
`uv run --locked pytest tests/test_doc_links.py` checks local Markdown file targets as part
of the full suite; additionally verify anchors and referenced source paths on this plan.
No executable entry point is changed by #174. Implementation PRs must execute every
changed generator/helper and emitted wrapper
at least once, including loop bodies and non-default branches, per `AGENTS.md`.

Extend the existing Linux/native Windows CI with fake-harness script execution and isolated
test hubs. CI must not use the operator's registry, credential, run `.robomate/`, agents or
real process cleanup. Missing identity/credentials, wrong hub, inherited environment,
unavailable HTTP, paths with spaces, read-only roots, failed launch, duplicate consultation
and report preservation all get deterministic coverage where introduced. Automated evidence
does not prove real interactive access grants, remote connectivity, orphan termination or
actual conversation restoration; record live checks and limitations explicitly.

## Roadmap decision

On opening this PR, propose the following in an attributed PR comment for adjudication;
make the live #2 edit only when Alice assigns post-merge close-out:

- Insert `M7 Denise: interactive doctor agent` immediately before M2, mark it the next
  milestone, link #174 and this plan, mark planning complete only with the merged PR, and
  leave implementation/acceptance unchecked. Open one issue per step as picked up rather
  than inventing reservations or checking off the milestone with this document.
- Rewrite the *Tracks and lanes* ordering sentence: "The milestone sections below give
  the MVP order for Track 2: M7 next, then M2, M3, M4 and M6; other tracks continue
  independently." Change Track 2's milestone list to "M7, then M2, M3, M4 and M6" and its
  `Next:` to "M7 Step 1 (issue opened when picked up), then
  the remaining M7 steps; M2 implementation steps wait for M7 completion. #226 M2 planning
  may continue independently." Set Track 3's `Next:` to "#222" after #174 closes; keep
  #174's completed planning on its historical track, and label new M7 step issues Track 2.
- Lane A (WSL), the existing Track 2 lane, runs M7 and then M2. M7 does not wait for optional
  lane C; lane C remains available for Track 3's #222. Preserve the other lane priorities.
- Add an M2 inheritance item preserving optional Denise startup, context, access, local
  guide and reports when generated team launchers are replaced.
- Preserve every unrelated completion status, issue ordering and counter reservation.
  This planning PR reserves no DB or wire counter and creates no implementation issues.

At close-out, snapshot the current live body, construct the replacement in a unique
temporary file, and stop on any failed match or command before `gh issue edit`. Anchor
edits to their own sections, reread the live issue and diff it against the snapshot; do
not replace a newer track summary with this plan's historical snapshot. Report the merged
PR and full merge SHA, and any review-adjudicated changes to the proposal.
