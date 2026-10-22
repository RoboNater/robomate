# Implementation plan — MVP Milestone M2: join, worktrees, work intake

Status: proposed. Tracking issue [#226](https://github.com/RoboNater/robomate/issues/226);
roadmap [#2](https://github.com/RoboNater/robomate/issues/2), section M2. Step issues:
[#238](https://github.com/RoboNater/robomate/issues/238)–[#249](https://github.com/RoboNater/robomate/issues/249),
linked under each step heading.

This plan turns [`docs/mvp-spec.md`](../mvp-spec.md) §13 row M2 into a sequence of
PR-sized steps. It is a plan, not a design of record: where it settles something the spec
leaves open, the step that implements it also updates the spec (see
[Spec deltas](#spec-deltas)). §-numbers refer to `docs/mvp-spec.md` unless marked
"PoC §". The [M1 plan](implementation-plan-mvp-m1.md) and the
[M5 plan](implementation-plan-mvp-m5-gitlab.md) are cited as "M1 plan" and "M5 plan".

M2 runs after M1, M5 and the pre-M2 checkout-scope step (#147, PR #157), in that order
(§13 Order). It starts from that topology: a per-checkout hub from `robomate up` with its
state in the checkout's `.robomate/`, `robomate mcp` bridges in `--role` mode, and runs
prepared by `scripts/prepare-run.py`.

M7 Denise proceeds in parallel on the prepared-run path. Only Step 11 (#248) waits for
M7 completion and the doctor handoff described below; the other M2 implementation steps,
including Step 10 (#247), can proceed independently (spec §13; #174 review decision).

## What M2 has to deliver

From §13:

> **Deliverable:** `join`/`whoami`, standing roles, takeover + task reattach; hub state and
> token moved to `<worktree_root>/<hub>/` with checkout bindings (§4, §12); worktree
> manager (§6): generated worktrees per `(hub, agent)` for every agent, locks,
> `robomate/<hub>/<agent>/<label>` branches; configuration layers (§4); `robomate submit`,
> queue, `get_statement` (#8, #9); `guides/orchestrator.md` served; `robomate workspace`,
> `robomate clean`, and hub selection for each; the agent boundaries and operator-only
> steps of §12
>
> **Done when:** On Claude Code + Codex: `robomate up`, three one-line joins,
> `robomate submit`, merged PR — with no `prepare-run.py`, no generated team configs, no
> pasted launch prompts; Denise's separate context/profile/report files remain (§5.1).
> The same again from a second worktree's hub while the first runs

Inputs:

- [#54](https://github.com/RoboNater/robomate/issues/54) (decision in §4, §6, §14): a hub
  belongs to the checkout that ran `up`; state and agent worktrees live outside every
  checkout, under the worktree root.
- [#147](https://github.com/RoboNater/robomate/issues/147) / PR #157: hub names,
  `up.lock`, per-hub registry entries and ports, discovery without silent attach,
  `--hub`, and legacy hubs keeping their state. State is still in `.robomate/`, reached
  through one resolver.
- [#159](https://github.com/RoboNater/robomate/issues/159) / PR #161: `robomate forget` and
  `robomate down --all`.
- [#145](https://github.com/RoboNater/robomate/issues/145) / PR #150: the isolated smoke
  hub for robomate's own entry points (§4). M2 adds its configuration-layer and
  worktree-root overrides (§13 Checkout scope).
- The carried M2 issues [#8](https://github.com/RoboNater/robomate/issues/8),
  [#9](https://github.com/RoboNater/robomate/issues/9) and
  [#10](https://github.com/RoboNater/robomate/issues/10), and
  [#90](https://github.com/RoboNater/robomate/issues/90),
  [#93](https://github.com/RoboNater/robomate/issues/93) and
  [#96](https://github.com/RoboNater/robomate/issues/96), which name M2 as their landing
  point.
- The two inheritance lines on #2: from M5 (M5 plan, "The run path before M2") and from
  #181 (pywin32 kept out of the `uv tool install` path).
- M7's optional interactive Denise (#174), built alongside M2 and retained through the
  Step 10/11 launcher handoff before retirement.
- What the run path grew after M1, which M2 has to replace or keep: headless launchers and
  backoff (#146, #165, #158), generated guarded recovery (#173), ask scripts (#221), remote
  worker-only bundles, isolated harness homes and credential links (#179, #184), closeout
  report paths (#97 / PR #218), host standby (#19), and the Devin CLI prototype (#224).

Not in M2: `robomate install`, harness profiles, `robomate certify`, `docs/harnesses.md`,
the supervisor/nudge path and the per-harness grant record (M3); the `closeout` task,
`CloseoutResult`, the `done` check, `robomate report`, `robomate abandon`, `nits_issue`
and `followup_label` handling, and `robomate log --timeline` (M4); `call_log` retention
(M6). M2 defines the run identity those build on (#215, #216).

## Facts from the current code that shape the plan

1. **A hub holds one workflow for its whole life.** `HubStore.initialize_workflow` reads
   `SELECT … FROM workflow ORDER BY created LIMIT 1` and creates a row only when there is
   none. Every later call must repeat the same goal and policy, or it is refused with "run
   robomate up in a fresh checkout … for a different workflow". So today each run needs a
   fresh checkout and hub (#53), and `prepare-run.py`'s `check_hub_workflow` reads
   `hub.db` directly to refuse a reused one. There is no queue, no `queued` status
   (`WorkflowStatus` is `active`, `paused`, `done`, `escalated`), and no run ID anywhere
   in `packages/`: the run IDs in `docs/evidence/` were minted by scripts.
2. **Identity comes from the launcher, not from the agent.** `robomate mcp` requires
   `--role orchestrator|worker` and takes `--name`; a worker may use `AGENT_NAME`. A
   worker's `check_in` reports a profile whose harness, model and capabilities come from
   `HUB_*` variables that `prepare-run.py` writes into each rendered MCP config. The
   orchestrator defaults to `alice`, and the newest session wins (§5, M1 form of
   takeover). The `agent` table is keyed by `name` and has no standing role.
3. **The worker workspace check refuses a worktree.** `agent_hub_common.workspace.
   canonical_workspace` requires `<path>/.git` to be a directory and the common directory
   to be `<path>/.git` ("must be a full clone, not a worktree"), and `read_identity` reads
   `robo-agents-workspace.json` from that `.git`. A generated worktree's `.git` is a file.
4. **One resolver for state, with a few side readers.** `discovery.state_dir(checkout)`
   returns `checkout/.robomate` and `token_file()` its `token`, and the CLI goes through
   them. Two readers bypass the hub: `detect_forge` reads `[forge]` from
   `.robomate/config.toml` (the only configuration file today), and `prepare-run.py`
   opens `hub.db` read-only through `state_dir`. `HubSettings.from_env` falls back to
   `~/.local/state/agent-hub` for the `hub` entry point.
5. **Guides are served from the robomate checkout.** `_default_guides_dir` finds
   `guides/` four directories above `agent_hub_common`, so the guides work under
   `uv run --project <robomate checkout>` but not from an installed wheel. The guide route
   composes the hub forge's appendix by default (M5 Step 3). There is no
   `guides/orchestrator.md`: Alice's instructions are `skills/alice-orchestrator/SKILL.md`
   (31.6 KB), plus `prompts/alice.md` (2.5 KB) and the goal and policy pasted into her
   prompt. Both embed a policy block that `tests/test_runtime_content.py` requires to equal
   `WorkflowPolicy().model_dump()` (M5 plan fact 8). `prompts/worker.md` (10.2 KB) mirrors
   `guides/worker.md` (10.0 KB), and a test compares them (#8).
6. **The skill merges with `--delete-branch`.** Its GitHub command is
   `gh pr merge <pr_url> --<merge_method> --delete-branch --match-head-commit <approved head>`,
   and its GitLab command passes `--remove-source-branch`. Under worktrees the implementer's
   work branch is checked out in another worktree of the same repository, which is the §6
   risk §15 asks M2 to settle.
7. **`prepare-run.py` is 3,510 lines** and depends on `run_common.py` (680),
   `agent_launcher.py` (778), `agent_recovery.py` (515), `agent-ask.py` (186) and
   `agent_sessions.py`. For each agent it renders an MCP config carrying
   `ROBOMATE_HUB_URL`, `ROBOMATE_TOKEN_FILE`, `AGENT_NAME`, `HUB_WORKSPACE` and the
   profile; isolated harness homes (`CODEX_HOME`, AntiGravity's) with linked or copied
   credentials; per-harness grants (Codex `writable_roots`, Claude Code and AntiGravity
   `--add-dir`, OpenCode `external_directory`); `start-`, `resume-` and `ask-<agent>`
   scripts in Bash and PowerShell; a `--worker-only` bundle for a remote host; the prompt
   files; `RUN_DIR/closeout/` and `RUN_DIR/tmp/<agent>`; and `run.json`. Five harnesses
   are supported, Devin as a prototype (#224, live checks in #235).
8. **Launchers are already policy-free.** `agent_launcher.Launcher` starts a harness with
   a prompt file, captures the conversation ID, resumes the same conversation with a fixed
   continuation text and #158 backoff, and logs every start and exit to
   `<agent>-sessions.jsonl`. It reads the hub only to decide whether a resume is still
   wanted. What ties it to `prepare-run.py` is its inputs: the prompt file, the rendered
   config paths and the run directory. A row's `workflow_id` is null until a workflow
   exists, which is #223.
9. **The token budget already counts.** `prompts/worker.md` plus the served
   `guides/worker.md` is about 20 KB of duplicate instructions per worker start, and the
   orchestrator skill alone is larger than a 16 KiB message part. The join response that
   replaces them (§5) carries a guide once per session with its hash.
10. **Counters.** DB schema v20 shipped for #95, and #2's Reservations block names the next
    free value. Wire `hub.schema_version` is 1. #215 (M4) also needs a DB version, reserved
    at implementation time.

## Design

### Size and order of M2

M2 stays one milestone named M2, delivered in fifteen steps. The order puts a usable
increment first: `join` lands on today's `.robomate/` layout (Steps 1–2), and
sequential runs with `robomate submit` (Steps 3–4) work with or without it. Only then does
state move (Step 6) and the worktree manager start creating agent workspaces (Steps 7a–7d).
`prepare-run.py` keeps working until Step 11 retires it, so robomate is built with itself
throughout.

| §13 M2 deliverable | Step |
|---|---|
| `join`/`whoami`, standing roles, takeover + task reattach | 1 (workers), 2 (orchestrator) |
| `guides/orchestrator.md` served | 2 |
| Run identity, sequential workflows, queue | 3 |
| `robomate submit`, `get_statement` (#8, #9) | 4 |
| Configuration layers (§4) | 5 |
| State and token in `<worktree_root>/<hub>/`, bindings (§4, §12) | 6 |
| Worktree manager at join: worktrees, bindings, locks, reuse | 7a |
| Worktree creation before join: `robomate workspace`, `up --agents`, `setup`/pins | 7b |
| Branches: `robomate/<hub>/<agent>/<label>`, reuse, workflow record; named-branch guide lines | 7c |
| §12 agent boundaries, §6 hygiene, scratch; Linux grants for two harnesses | 7d |
| `robomate clean`; branch deletion after merge (§15) | 8 |
| Clone fallback for remote agents | 9 |
| Headless agents without generated configs (optional launcher) | 10 |
| No `prepare-run.py`; §12 operator-only steps in the served guides | 11 |
| Done-when | 12 |

### `join` and `whoami` (§5)

The spec settles the tool, its arguments, the standing roles, the one-orchestrator rule,
takeover and reattach, and the join response (§5). The plan adds what the code needs.

- **The bridge starts unjoined.** `robomate mcp` loses its required `--role`: without
  `--role` it serves the static tool list (§8) and accepts only `join` and `whoami` until a
  join succeeds. With `--role` and `--name` (or `AGENT_NAME`) it behaves as today, so
  prepared runs keep working until Step 11 removes the flags. `--harness` (§9.1) is
  accepted from Step 1 and recorded as the profile's `install` source.
- **Workers join over A2A, the orchestrator over `/rpc`.** A worker's `join` is the
  existing check-in extended with `role`, `takeover`, `model`, `effort` and `harness`;
  workers still never reach `/rpc` (§12). The orchestrator's `join` is a new `/rpc`
  operation carrying the M1 session headers, and the first call of a new session still
  makes it current. `check_in` stays as an alias of a worker `join` with the role
  `worker` until Step 11.
- **Hub selection at join.** Discovery is unchanged (§4): explicit URL and token, then the
  bridge's working directory through git. `join(hub=…)` is the third route, a name, a
  `hub_id`, or a checkout path, resolved like the CLI's `--hub` against the machine
  registry. It exists for harnesses that start MCP servers outside the agent's folder,
  which is the IDE case below.
- **Takeover.** A live name returns 409 unless `takeover=true`. Takeover records a new
  `worker_instance_id`, so the old instance's heartbeats are ignored, and a held task is
  reattached and returned in the join response, not failed as `worker_lost`.
- **Role compatibility.** `assign_task` refuses an incompatible standing role (§5) with
  -32602 naming the agent's role.
- **Profile.** `join(model=…, effort=…)` is the `declared` source and `--harness` the
  `install` source. A `config.toml` pin (Step 5) is the operator's source and must be an
  exact model ID. Without a launcher pinning `HUB_MODEL` to an alias, #87's false
  positive cannot arise on the join path; Step 1 closes #87 if its tests show that, and
  otherwise moves it to M3 with the harness profiles.
- **`ask_alice` becomes `ask_orchestrator`** (§8), with `ask_alice` kept as an alias until
  Step 11. The A2A message is unchanged.
- **Workspace before Step 7a.** Until the worktree manager exists, the join response's
  `workspace_path` is the bridge's working directory as today (a prepared clone with its
  identity file), and `workspace_id` comes from that identity. Step 7a changes the source,
  not the field.

`whoami` returns the joined identity and the identify-the-hub fields (§4), and works
before join, when it reports what discovery would select.

### `guides/orchestrator.md` and the orchestrator's join (Step 2)

- The orchestrator skill's content moves to `guides/orchestrator.md`, written
  forge-neutrally like the role guides (M5 Step 3). Its merge and merged-state read-back
  commands go to an orchestrator-only appendix per forge, `guides/forge/orchestrator-github.md`
  and `guides/forge/orchestrator-gitlab.md`, composed by the hub's forge. Worker
  appendices stay merge-free, and the M5 test that no served worker guide contains a merge
  command stays green (M5 plan, "What M2 inherits").
- The orchestrator's join response carries the guide and its hash, the hub's forge, the
  compact `get_state` summary (which gains the queue and `run_id` in Step 3, so the join
  response needs no change then), unacknowledged deliveries, and, with a workflow active,
  the resume procedure (§5, §7.5). On a rejoin in the same bridge session the guide is
  returned as "unchanged (hash)" (§11 lever).
- The skill becomes a thin pointer to `join` (Appendix B). `prompts/alice.md` keeps working
  for prepared runs until Step 11.
- Step 2 reports the served orchestrator guide's bytes, before and after, as M5 Step 3 did
  for the role guides. Trimming it is M6 (#22).

### Run identity and history (Step 3)

This is what M4's close-out reports (#215, #216) and run report (§7.3) build on.

- **Run ID.** Each workflow gets a `run_id` when it is created: `<UTC date YYYYMMDD>_<8 hex>`,
  the form the evidence documents already use (`m5-20261002_e8b76429`). It is unique per
  hub and stored on the workflow row next to the existing `id`, which stays the key every
  table references. The run ID is what operators, file names and evidence use; the
  workflow ID stays what tasks and events reference.
- **Sequential workflows.** A hub keeps every workflow. Statuses gain `queued` (§7.2); at
  most one is `active`, `paused` or `escalated` at a time. `start_workflow(statement?,
  policy?)` starts the oldest queued workflow, or creates one from chat (§7.1).
  `initialize_workflow` stays as an alias until Step 11, with its create-or-confirm
  behaviour scoped to the current workflow: when the previous one is `done`, it starts a
  new one. That alone removes the fresh-checkout-per-run rule (#53) for prepared runs.
  `abandoned` and `robomate abandon` are M4 (§13).
- **Carried context.** `get_state` returns the previous run's summary (run ID, PR/MR,
  merged SHA) and nothing else from earlier runs (§7.2).
- **`on_done`** (§7.2): `release` (default) releases workers when a run ends with the queue
  empty; `standby` keeps them joined for the next queued run. It is the one §7.2 policy
  addition M2 needs; `deliver` and `closeout` are M4.
- **`runs/<run_id>/`** in the hub's state directory (§4, §11). `manifest.json` is written
  when the run starts and rewritten atomically at its end: robomate version and git
  revision, `hub_id` and name, origin, forge, default branch, statement SHA-256 and source,
  policy, and the profile of each agent that held a task. At the end it adds the status,
  PR/MR URL, approved and merged SHAs. `report.md` is M4's.
- **Attachments.** Rows that belong to a run carry its `run_id` where they are written
  outside the hub: the launcher's session log (Step 10) and, from M4, per-agent closeout
  reports (#215), exported to `runs/<run_id>/closeout/<author>.md` (as
  [agent-closeout-reports.md](agent-closeout-reports.md) proposes). Assignments and
  `get_state` return the `run_id`, so an agent can cite it.

### Work intake: `robomate submit` and `get_statement` (Step 4)

- `robomate submit <file>`, `submit --issue N` and `submit -` (§7.1) call an operator
  `hub.submit` method with the operator credential (§8). The hub stores the bytes verbatim
  with their SHA-256, queues a workflow, and emits `work_submitted`. `--issue N` renders
  the standard one-issue statement, which is then stored like any other.
  `--policy k=v` overrides the configuration-layer defaults (Step 5) for that run.
- **Size cap (#10).** Workers read the statement with `get_statement(sha)` over A2A, so the
  cap is set so that one statement fits in one 16 KiB message part with its envelope. Step
  4 picks the number, justifies it, and states it in the error, which also says what to do
  instead (shorten it, or put the detail in an issue and submit that).
- **Assignments carry `statement_sha256`** (§7.1, §8). The orchestrator guide tells Alice
  to reference scope, not restate it (#9). Nothing paraphrases the operator's text on its
  way to a worker.
- **Preflight at submit** (M5 inheritance; §2). The policy is known at submit, so submit
  runs the policy-dependent forge checks: CI presence for `allow_no_ci = auto` and, on
  GitLab, `check_merge_compatibility` against `merge_method`. Both call the hub's own
  functions (`forge_preflight`, `GitLabGate`'s CI check), with no second copy. That is
  #96's request; #93's two bugs are in the copy that goes away, and their cases become
  tests of the shared code: an explicit `allow_no_ci` skips the CI probe, and only a 404
  status means "no CI file". A hub whose `hub.json` has no origin (possible only from the
  `hub` entry point or an old `hub.json`, since `up` refuses an empty origin) refuses
  submit, because the gate cannot be bound (#96, item 2).

### Configuration layers (Step 5)

The spec settles the files, their order, and which keys each layer may set (§4). The plan
adds:

- One reader in `agent_hub_common` (`config_layers.py`) used by the CLI and the hub, with
  the §4 key-to-layer table enforced: a key set in a layer that may not hold it is an
  error naming the file and key, never ignored.
- `forge.*` moves from `.robomate/config.toml` to the repository and user layers. A legacy
  hub's `.robomate/config.toml` `[forge]` table is still honoured, with a warning naming the
  repository-level file to move it to, until the hub is relocated (Step 6). Nothing is
  rewritten for the operator.
- Policy defaults and agent defaults (model and effort pins) come from the layers, and so
  does `setup`. `submit` applies the policy defaults (Step 4); the join response returns
  `setup` and applies the agent pins once worktrees exist (Step 7b, §6). `nits_issue`,
  `followup_label` and `closeout_steps` are parsed and validated but have no reader until
  M4.
- The isolated smoke hub adds `XDG_CONFIG_HOME` (Windows `APPDATA`) to the variables it
  points into its temporary directory (§4, #145). `guides/worker.md` changes with it.

### State location, token and bindings (Step 6)

The spec settles the layout (§4, §6, §12): new hubs keep state in
`<worktree_root>/<hub>/state/` and the token in `<worktree_root>/<hub>/token/token`;
legacy hubs run in place; bindings are `robomate-binding.json` in each checkout's own git
directory. The plan adds:

- **All of it goes through the resolver.** `state_dir()` and `token_file()` take the
  binding or the registry entry instead of a checkout path. Every other reader (fact 4)
  moves onto them in the same PR; a test fails if any module outside `discovery.py`
  names `.robomate`.
- **The worktree root records its repository.** At creation the root gets
  `robomate-root.json` with the git common directory; a hub of another repository refuses
  that root and asks for a repository-level `worktree_root` (§4).
- **The relocation command is `robomate relocate`** (§4 Migration names no command). It
  runs on a stopped hub, selected like any other, with the operator credential. It moves
  state and token together, rewrites the owner and agent bindings and the registry entry,
  and leaves `.robomate/` with a one-line pointer file instead of the moved files. It is
  refused while the hub is live or any agent worktree of the hub is locked by a live hub.
- `up` in a checkout bound as an agent is refused, naming the hub (§4). The linked-worktree
  warning about `git worktree remove` is dropped for relocated hubs and kept for legacy
  ones.

### The worktree manager (Steps 7a-7d)

The spec settles the location, creation command, reuse per `(hub, agent)`, locking, branch
names, detached reviewers and the orchestrator, new-task hygiene and setup (§6). The plan
adds (each bullet names the sub-step that implements it):

- **The hub creates worktrees (Step 7a).** The hub knows the git common directory from `hub_info`,
  so at join it runs `git worktree add --detach <root>/<hub>/<agent>/ origin/<default>`,
  writes the agent binding, and locks the worktree with
  `git worktree lock --reason "robomate hub <name>"`. A join that finds its worktree already
  there reuses it. `robomate workspace <name>` and `robomate up --agents a,b,c` do the
  same ahead of the join through an operator-only `hub.workspace` method (Step 7b) and
  print each path.
- **Workspace identity for worktrees (Step 7a; clones keep the identity file, Step 9).** The full-clone check (fact 3) gains a worktree form: the
  identity lives in the agent binding in the worktree's own git directory, and
  `workspace_id` is the binding's. Prepared runs keep their clones: a join with an
  explicit `HUB_WORKSPACE` creates no worktree and keeps its full-clone check. An
  explicitly configured workspace with no matching identity is refused; the lenient
  path stays only for the bridge-cwd fallback (r2-2 on #244).
- **Branches (Step 7c).** The hub names the branch in a run's first implementer assignment,
  `robomate/<hub>/<agent>/<label>`, with `<label>` derived from the statement (issue number
  or a slug) and validated like a name. It is refused if the ref exists locally or on
  origin, and later tasks of the run reuse it (§6). The workflow row records it. The
  guide lines for the named branch are part of Step 7c.
- **Agent boundaries (§12) (Step 7d)** go into the served guides in this step: never read the hub's
  state or token directory, start every task from fresh `origin/<default>` or the
  assigned SHA, report `blocked` on uncommitted changes, retry a `cannot lock ref` after a
  short wait and never delete a `.lock` file, never change git config. The bridge's
  `whoami` names the token file it reads; the guide says that the bridge, not the agent,
  reads it. Scratch stays outside the worktree: validation scratch, temporary checkouts
  and validation TEMP belong in the harness's own temporary directory, never in the
  worktree or `closeout/`.
- **Sandbox grants for M2's two harnesses (Step 7d).** Grants name the worktree and the git common
  directory, never `<checkout>/.git` or the hub's namespace (§6, §12). Step 7d records,
  on Linux, what Claude Code and Codex need to commit and push from a generated worktree.
  Because worktrees are reused and the root is stable, these grants are static per
  repository, written once into the harness's user configuration, not per run. The other
  harnesses, and Codex on Windows (#11), are M3.

### Merge and branch deletion (§15, Step 8)

Decision: **the orchestrator merges with the forge CLI from its own detached worktree and
deletes only the remote branch; `robomate clean` prunes local branches.** This is the
default §6's risk table already names.

- GitHub: `gh pr merge <url> --<method> --match-head-commit <approved head>` without
  `--delete-branch`, then `git push origin --delete <branch>`. `--delete-branch` also
  deletes the local branch, which is checked out in the implementer's worktree, so git
  refuses, and `gh` would report a failure after a merge that succeeded. A run must not
  read a merged PR as a failed merge.
- GitLab: `glab mr merge … --remove-source-branch` is unchanged; GitLab deletes the source
  branch on the server and touches no local ref.
- The alternative, merging from outside any checkout with `gh -R`, still leaves the local
  branch for cleanup and adds nothing.
- Step 8 verifies the `--delete-branch` failure live on the GitHub sandbox before changing
  the guide, and records the output in its PR. If `gh` turns out to skip the local delete
  silently, the decision stands, because the remote-only command is correct either way.

`robomate clean` (§6) acts on one selected hub with the operator credential: it unlocks and
removes the hub's worktrees without `--force`, refuses any with uncommitted changes or
unpushed commits, prunes merged `robomate/<hub>/…` branches, and reports orphaned
worktrees found through `git worktree list --porcelain` and their bindings. It never
removes hub state.

### Reviewer pairing default (§15, Step 2)

Decision: **keep `reviewer_harness_differs: true`.** An independent reviewer harness is the
point of the role policy (§5), and M2's own done-when (Claude Code and Codex) satisfies
it. From Step 5 an operator who wants otherwise sets
`role_policy.reviewer_harness_differs = false` once in a configuration layer, or per run
with `robomate submit --policy`, so the safer default costs one line. M3's certification
runs, with more harnesses, are where evidence for relaxing it would come from; this plan
does not ask for it. The §15 row moves to §14.

### What replaces the prepare-run run path

The done-when excludes `prepare-run.py`, generated team configs and pasted launch prompts. Each
capability of today's run path is replaced, kept as an optional launcher outside the core,
or retired:

| Today (fact 7) | After M2 | Step |
|---|---|---|
| Per-agent rendered MCP configs with URL, token file, name, workspace, profile | One static entry per harness, `robomate mcp --harness <h>` (§9.1), written once by hand in M2 and by `robomate install` in M3; identity at `join`; token found through the binding | 1, 6, 11 |
| Pasted prompt files (`alice.prompt.md`, `bob.prompt.md`) | The one-line join; guides in the join response; the statement from `get_statement` | 1, 2, 4 |
| Bootstrapped full clones per worker | Generated worktrees per `(hub, agent)` | 7a |
| Headless agents under the Alice and worker launchers, restart and backoff | **Kept, optional, outside the core**: the launcher starts a harness in the agent's worktree with the join line as its prompt, and keeps #158 backoff | 10 |
| Generated guarded recovery (`resume-<agent>`) | `join(takeover=true)` reattaches the task in the hub; the launcher's `resume <agent>` resumes the saved conversation with today's guards, taking the run from the hub (#223) | 1, 10 |
| Generated `ask-<agent>` scripts (#221) | The launcher's `ask <agent>`, reading the same session log | 10 |
| Remote worker-only bundles | Clone fallback: `robomate workspace <name> --clone` on the remote host, with an explicit URL and token file (§4, §6) | 9 |
| Isolated team harness homes, credential links and copies (#179, #184) | **Retired for the joined team.** They existed because a rendered config carried per-run identity and URL. A static entry holds neither, so the team harness uses the operator's own home and login. Denise's isolated no-MCP profile is preserved separately below | 11 |
| Per-harness sandbox grants rendered per run (Codex `writable_roots`, `--add-dir`; #11) | Static per-repository grants on the worktree and git common directory, recorded for Claude Code and Codex in M2 and for every harness in M3; the launcher passes the same grants as flags | 7d, 10 |
| `RUN_DIR/closeout/<agent>.md` report paths (#97) | **Retired** with the run directory. Hub collection (#215, M4) replaces it; until then an operator who needs the files keeps using prepared runs, which work until Step 11 | 11 |
| `RUN_DIR/tmp/<agent>` (#81, #232) | The harness's own temporary directory, outside the worktree | 7d |
| `run.json` | `runs/<run_id>/manifest.json` in hub state | 3 |
| GitHub and GitLab preflight in `prepare-run.py` | Policy-independent checks at `up` (M5 Step 2); policy-dependent checks at `submit` | 4 |
| Team Bash and PowerShell wrappers | Not needed: `robomate` and the launcher are Python and run the same on Windows; Denise's optional replacement must be demonstrated before retiring her wrappers | 10, 11 |
| `prep-standard-run*` (#107) | Retired: `robomate up` in a checkout plus `robomate submit` | 11 |
| M7 `start-denise.sh` / `.ps1`, isolated doctor profile and `RUN_DIR/denise/` | Optional doctor entry in the successor launcher, outside join/supervision. Use the state resolver and locally readable `runs/<run_id>/manifest.json` for explicit hub/run selection, robomate/target references and effective settings; keep context, isolated no-MCP config, scoped repair grants and local reports in that run's `denise/` directory. The guide stays local at `docs/denise/guide.md`, outside served `guides/`. Startup and diagnosis work without HTTP/MCP; no new credential or privileged API | 10 integration, 11 proven handoff before removal |

The doctor entry is separate from `start <agent>` and never calls `workspace`, `join` or
an agent's supervision loop to start Denise. It reads saved local metadata when the hub is
down; absent/stale metadata yields a concrete remedy rather than creating a run. M7 keeps
its prepared-run first slice while M2 is built. Step 10 provides the launcher integration
as the doctor implementation becomes available, without waiting for all of M7; Step 11
completes and verifies the replacement before deleting its generators. Doctor context,
profile and report files are an intentional exception to the team's no-generated-config
path. Existing consultation reports remain at their recorded paths; adoption must retain
or explicitly reference them, never delete or silently move a live consultation.

**Headless supervision: the launcher survives without generated configs; `robomate`
gains no launch command in M2.** Reasons:

- Launching agent harnesses is an operator-only action (§12), and how to start each
  harness headless is harness knowledge. The spec puts that knowledge in harness profiles
  and the supervisor path in M3 (§9.2, §9.4, §13 M3 row).
- The core experience is that the operator starts a harness in a folder and types one line
  (§2). Headless supervision is one way to start it, not part of the hub.
- The launcher already holds no policy (fact 8). Replacing its inputs, a prompt file and
  rendered paths, with the agent's name, role, harness and the worktree that
  `robomate workspace` returns is a contained change.

M3 decides, with the harness profiles, whether the launcher becomes a `robomate`
subcommand. Until then it is `scripts/agent-launcher.py`, one entry point for every
agent:

```sh
uv run --project <robomate checkout> python scripts/agent-launcher.py start bob \
    --role implementer --harness claude-code [--model M] [--effort E] [--interactive]
uv run --project <robomate checkout> python scripts/agent-launcher.py resume bob
uv run --project <robomate checkout> python scripts/agent-launcher.py ask bob
```

It finds the worktree with `robomate workspace bob`, sends `Join robomate as bob,
implementer.` as the prompt, passes the harness's grants as flags (no files), and logs each
start and exit, with `run_id`, to `sessions/<agent>.jsonl` in the hub's state directory.
That is the operator's data, which the agent never reads. A remote host logs to a
directory given with `--sessions-dir`. `--interactive` is #222's request, carried here
instead of into `prepare-run.py`. The Devin adapter (#224) moves with the launcher; its
live validation stays in #235.

### Interim run-path issues

| Issue | Disposition | Why |
|---|---|---|
| #10 work-file size cap | **Carry into Step 4**, as the `submit` cap | §7.1, Appendix A |
| #90 `--worker-only` placeholders and hub probe | **Carry into Step 9**: `workspace --clone` rejects unexpanded values and probes `/healthz` and the agent card before writing | Same host-side check, new command |
| #93 GitLab CI probe bugs | **Close with Step 4**; its two cases become tests of the shared code | The buggy copy is removed |
| #96 duplicated GitLab preflight, hub without origin | **Carry into Step 4** | Submit calls the hub's functions; no origin refuses |
| #219 junction closeout directories | **Close when Step 11 lands**; not worth doing before M2 | Hardening of an operator-owned path that M2 removes |
| #220 live closeout write check | **Keep as an evidence run until Step 11**, then close | It checks every prepared run until the run directory goes |
| #221 `ask-<agent>` | Done (PR #233); **carried into Step 10** as `ask <agent>` | Same session log, no generated script |
| #222 `--interactive` | **Carry into Step 10**; don't build it into `prepare-run.py` | The start scripts it changes are retired in Step 11 |
| #223 recovery `workflow_id` | **Do before M2** (Track 1 next, per #2); Step 10 keeps the behaviour | It fixes recovery of runs being prepared today |
| #188 `archive-run.py` drops `hub.db` | **Do the `hub.db` part before M2**; the script goes in Step 11 | Following the guide loses a run's main record today; M2 state lives outside the run directory and `clean` never removes it |
| #189 credential copies | **Close when Step 11 lands** | Isolated homes and copies are retired |
| #190 Codex `auth.json` refresh | **Close when Step 11 lands**, unless M3 isolates harness homes again | The core no longer links logins |
| #224 Devin CLI in `prepare-run.py` | Done (PR #234); adapter moves in Step 10; live checks stay in #235 (M3) | |
| #232 validation scratch and per-agent TEMP | Track 1 while prepared runs continue; under M2 the guides keep scratch out of the worktree (Step 7d); Codex Windows TEMP is M3 | |
| #87 model alias mismatch | **Carry into Step 1** (see [join](#join-and-whoami-5)) | |
| #8 short kickoff prompts | **Closes with Step 2** (join line plus served guides; worker half in Step 1) | |
| #9 statement verbatim | **Closes with Step 4** | |

### Inheritance lines on #2

- **From M5.** Forge-neutral guides composed at serve time: already in place (M5 Step 3);
  Step 2 adds `guides/orchestrator.md` the same way, with the orchestrator-only appendix.
  `join` reports the hub's forge: Steps 1–2. The GitLab preflight moves to
  `robomate submit`: Step 4. M2's tests keep `tests/test_gitlab_gate.py`,
  `tests/test_guides.py` and the M5 dispatch tests green, and M2's done-when stays
  GitHub-only; M6 covers GitLab end to end (§13).
- **From M7.** Add an inheritance line after M5 in the M2 section, naming Step 10 (#247)
  and Step 11 (#248). Carry optional Denise startup, locally readable context, local
  guide, isolated no-MCP profile, repair access and local reports into the successor
  launcher as above. Only retirement Step 11 waits for completed M7 and demonstrated
  replacement behavior; other M2 implementation proceeds in parallel.
- **From #181.** M2 does **not** ship the §2 `uv tool install` path. Its done-when runs
  from `uv run --project <robomate checkout>`, and installing needs the root to become a
  package and the guides to become package data (fact 5), which belong with
  `robomate install` in M3 (M1 plan, "CLI packaging"). The #181 line says it moves with the
  install work, so on merge of this plan it moves to M3 unchanged.

### Joining from an IDE

`robomate install` and certification are M3, but the `join` design has to hold for an IDE
harness before then. In M2:

- The operator writes one static MCP entry by hand, with no token, identity or paths
  (§9.1). For Devin Desktop's Devin Local agent that is the user-scope
  `mcp_config.json`, which the Devin CLI reads too (§9.1, §9.2; #224/#225). For Cline it
  is its MCP settings file. The entry is
  `uv run --project <robomate checkout> robomate mcp --harness devin` (or `cline`).
- The operator runs `robomate workspace dave`, opens that folder in the IDE, and types
  `Join robomate as dave, reviewer.`
- An IDE may start its MCP servers in another directory than the open folder, so working
  directory discovery may find no binding. The join response says whether the bridge's
  working directory is inside the workspace (§5). When discovery fails, the error lists
  the live hubs, and the agent joins again with `hub=<name>`, which is why `hub=` is in
  the design. The agent's file tools then act in the open folder, which is the worktree.
- An IDE on Windows with the hub in WSL uses the clone fallback (Step 9).

Step 1 tests `join(hub=…)` from a working directory outside any checkout. Step 12 records
one IDE join live, as evidence and not part of the done-when.

### Migration

- **Legacy hubs** (state in `.robomate/`) keep their state, `hub_id`, name, port and
  token, and run in place. From Step 6, `up` writes their owner binding with `.robomate/`
  as the state location and `.robomate/token` as the token file, and agent bindings record
  the same (§4 Migration). `robomate relocate` moves them when the operator chooses.
  `hub.db` migrates forward in place at the first `up` of each schema step, as every
  version has since PoC v12.
- **The legacy workflow.** At the run-identity migration (Step 3) every existing workflow
  gets a `run_id` from its `created` date and the first 8 hex of its `id`. A hub whose one
  workflow is `done` can then start a new one.
- **Existing run directories** are not migrated. Their hubs' state stays in
  `<run>/hub-target/.robomate/` and stays readable by `scripts/hub-report.py --state-dir`,
  since schema migrations only move forward. Their generated start, resume and ask scripts
  call the old launchers by path, which Step 10 leaves in place, so they work until Step 11
  removes them; after that the operator finishes such a run with the harness's own resume.
- **An in-flight prepared run across an upgrade.** Steps 1–10 keep `check_in`,
  `ask_alice`, `initialize_workflow` and `robomate mcp --role` as aliases, so a prepared
  run started before a step keeps working after its hub restarts on the new code. Step 11
  removes them; its PR says so, and an operator finishes in-flight prepared runs before
  upgrading past it. Explicit `ROBOMATE_HUB_URL` and token-file bridges keep the first rank
  in discovery throughout, so a prepared worker never starts following a binding.

## Spec deltas

Each item is settled here and written into `docs/mvp-spec.md` by the step that implements
it. This PR changes no spec text.

| Item | Settled as | Step |
|---|---|---|
| Unjoined bridge, `--role` compatibility, `join(hub=)` route (§4, §5, §8) | Static list; only `join`/`whoami` before join; `hub=` resolved like `--hub`; aliases until Step 11 | 1 |
| Worker join transport (§5, §8, §12) | Extended A2A check-in; workers never use `/rpc` | 1 |
| Model sources (§5) | `config.toml` pins are exact IDs; #87 closed or moved to M3 | 1 |
| Orchestrator guide and appendix (§5, §10, Appendix B) | `guides/orchestrator.md`; `guides/forge/orchestrator-<forge>.md`; skill becomes a pointer | 2 |
| Default `reviewer_harness_differs` (§15 → §14) | Kept `true`; relaxed per layer or per run | 2 |
| Run ID and manifest (§7.2, §11) | `<YYYYMMDD>_<8 hex>`; `queued`; `runs/<run_id>/manifest.json` fields above; `on_done` | 3 |
| Submit cap (§7.1) | Fits one 16 KiB part with envelope; the number set and justified in Step 4 | 4 |
| Preflight at submit (§2, §10) | CI presence and merge compatibility from the hub's functions; no origin refuses | 4 |
| Legacy `[forge]` in `.robomate/config.toml` (§4) | Honoured with a warning until relocation | 5 |
| Relocation command (§4 Migration) | `robomate relocate`, stopped hub, operator credential | 6 |
| Worktree root ownership record (§4) | `robomate-root.json` with the git common directory | 6 |
| Worktree creation path (§6, §12) | The hub at join (Step 7a); `hub.workspace` is operator-only (Step 7b) | 7a, 7b |
| Workspace identity, prepared clones, r2-2 refusal (§6) | Worktree binding identity; explicit `HUB_WORKSPACE` creates no worktree; explicit mismatch refused | 7a |
| Branch label (§6) | Derived from the statement, validated like a name, recorded on the workflow | 7c |
| Merge vs local branch (§15 → §6, §14) | Remote-only delete; no `--delete-branch`; `clean` prunes local branches | 8 |
| Launcher and supervision (§9.4, Appendix B) | Optional `scripts/agent-launcher.py`; supervisor command decided in M3 | 10 |
| `uv tool install` (§2, §13) | Moves to M3 with `robomate install` | 11 |
| Appendix B fates | `prepare-run.py`, `bootstrap-workspace.*`, `prep-standard-run*`, `prompts/`, `skills/worker`, `runtimes/` templates, `archive-run.py` removed | 11 |

## Reservations

The plan claims **no schema version number**. Four steps each need one new DB version,
and each takes it the way M5 and AGENTS.md require: when the step issue is picked up, it
reserves the then-next value on #2's Reservations block and only then writes the number
into code or text. The steps below say "the step's reserved DB version" for that reason.

| Counter | Step | Change |
|---|---|---|
| DB schema, one new version | 1 | `agent.standing_role` (`orchestrator`, `implementer`, `reviewer`, `worker`) and the join profile sources. Step 2 records the orchestrator's joined name and role with these columns |
| DB schema, one new version | 3 | `workflow.run_id` (unique), `workflow.statement_sha256`, `queued` in the status check, a `statement` table (SHA-256 key, bytes, size, source, created) |
| DB schema, one new version | 7a | Agent workspace path and kind (`worktree` or `clone`) |
| DB schema, one new version | 7c | The run's branch on the workflow |
| Wire `hub.schema_version` | — | No change: `join` is a new operation, check-in and assignment responses gain fields additively, and no typed result changes shape (§3, §7.3) |

Steps 2, 4–6, 7b, 7d and 8–11 expect no DB change. A step that finds it needs one reserves the
then-next value on #2 before editing (AGENTS.md). #215 (M4) does the same.

**Landing order.** Migrations are linear, so DB steps merge in the order of their
reserved values. Steps 7a and 7c merge after Steps 1 and 3 anyway (see [Steps](#steps)); only
Steps 1 and 3 can be ready in either order. Steps 7a and 7c each reserve their own
version on #2 at pickup, in that order: 7a first, then 7c. If the step with the higher value is ready to
merge first, the two swap their values on #2, and rename them in their branches, before
either merges.

## Steps

Each step is one issue and one PR, reviewable on its own, and leaves `main` usable.
Prepared runs keep working through Step 10. Every step that changes a hub or CLI entry
point comes with pytest tests that start and stop their own hub (#145); a worker's manual
smoke commands run inside the [isolated smoke recipe](#isolated-smoke-recipe).

```
Start together   [1] worker join        [3] run identity + queue      [5] config layers
                   │                      │                             │
Next             [2] orchestrator join + guide ◀── 1
                 [4] submit + get_statement    ◀── 3, 5
                 [6] state + bindings          ◀── 5
                   │
Then             [7a] worktrees at join         ◀── 1, 3, 6
                   │
Next             [7b] create before join ◀── 7a    [7c] branches ◀── 7a    [7d] boundaries + grants ◀── 7a
                   │
In parallel      [8] clean + merge ◀── 7a, 7c    [9] clone fallback ◀── 7a, 7b    [10] launcher ◀── 7a, 7b
                   │
Then             [11] retire prepare-run       ◀── 2, 4, 8, 9, 10
                   │
Last             [12] acceptance               ◀── all
```

Read `[k] … ◀── a, b` as "Step k merges only after Steps a and b". The table gives the
reason for each edge.

| Step | Needs (must have landed) | Why |
|---|---|---|
| 1, 3, 5 | — | Each is tested on today's code alone |
| 2 | 1 | The orchestrator joins with Step 1's bridge and columns |
| 4 | 3, 5 | Statements and the queue (3); `submit` applies the layers' policy defaults and its test checks that (5) |
| 6 | 5 | `worktree_root` comes from the layers |
| 7a | 1, 3, 6 | Worktrees are made at `join` (1); its migration follows Step 3's (3); worktrees live in the hub namespace (6) |
| 7b | 7a | It creates workspaces ahead of the join in generated workspaces |
| 7c | 7a | The branch label comes from the stored statement (3, through 7a), and its migration follows 7a's |
| 7d | 7a | Boundaries and grants apply to generated workspaces |
| 8 | 7a, 7c | It cleans worktrees (7a) and prunes merged `robomate/<hub>/…` branches (7c) |
| 9 | 7a, 7b | It clones beside generated workspaces; clones keep the identity file that 7a defines; `workspace --clone` extends 7b's `robomate workspace` |
| 10 | 7a, 7b | It starts agents in generated workspaces (7a) created ahead of the join (7b); Step 10 also logs `run_id` (3, through 7a) |
| 11 | 2, 4, 8, 9, 10; completed M7 and its launcher handoff | Every replacement, including optional Denise, exists and is verified before the old path goes |
| 12 | all | |

Parallel: Steps 1, 3 and 5 can start and land in any order, except that Steps 1 and 3
merge in the order of their reserved DB versions ([Reservations](#reservations)), and Steps 7a
and 7c merge in the order of theirs. Step 2
can run alongside Steps 3–6, Step 4 alongside Steps 6–7a, and Steps 8, 9 and 10 alongside
each other. Steps 7b, 7c and 7d can be prepared in parallel once 7a lands, but each
merges only after its prerequisites. A step whose prerequisite is still open may be prepared in parallel, but it
merges only after the prerequisite. Its tests need the prerequisite's code.

Step 11 also waits for completed M7 and its proven doctor replacement, as the table
specifies; this is the only M2 implementation step gated on M7 completion.

**Single-lane order.** Track 2 runs in one lane (#2), which takes the steps in this order:
**1, 2, 3, 5, 4, 6, 7a, 7b, 7c, 7d, 8, 9, 10, 11, 12.** Number order is not a valid sequence, because
Step 4 needs Step 5; this order satisfies every prerequisite in the table and reserves the
DB versions of Steps 1, 3, 7a and 7c in that order. The order does not lift the M7 gate: the
lane holds at Step 11 until M7 is complete, Step 11 proves the doctor handoff before it
removes anything, and Step 12 follows Step 11.

### Isolated smoke recipe

A step's manual smoke commands run inside this block, never on their own. It satisfies
`guides/worker.md`'s isolated-hub rules (#145, §4): every location is under one `mktemp -d`
directory, the explicit hub variables are unset, the hub belongs to a temporary clone,
the directory change happens in the foreground, the block waits until the hub answers and
checks that `status` names this clone, and the hub it started is stopped however the block
ends. `rb` is shorthand for the checkout under test.

```sh
T=$(mktemp -d); echo "$T"
git clone -q <robomate origin URL> "$T/repo"
(
  set -eu
  cd "$T/repo" || exit 1   # foreground: every command below runs in the temporary clone
  export HUB_STATE_DIR="$T/hub-state" XDG_STATE_HOME="$T/xdg-state" \
         XDG_CONFIG_HOME="$T/xdg-config" ROBOMATE_OPERATOR_TOKEN_FILE="$T/operator-token"
  unset ROBOMATE_HUB_URL ROBOMATE_TOKEN ROBOMATE_TOKEN_FILE
  rb() { uv run --project <robomate checkout under test> robomate "$@"; }
  rb up --port <free port, not the run hub's> > "$T/up.log" 2>&1 &
  trap 'rb down; wait' EXIT   # stops the hub this clone owns, however the block ends
  for i in $(seq 30); do rb status > /dev/null 2>&1 && break; sleep 1; done
  rb status --json | python3 -c 'import json, os, sys
sys.exit(os.path.realpath(json.load(sys.stdin)["checkout"]) != os.path.realpath(os.getcwd()))' \
    || { echo "no hub, or status names another hub; stopping" >&2; exit 1; }
  # the step's smoke commands go here
)
```

`rb down` in the temporary clone can reach only the hub that clone owns, since discovery
never attaches silently (§4). If `up` failed, it refuses instead. The checks end the block
with an explicit `exit 1` rather than relying on `set -e`, which some harness shells run
with errexit suppressed. `XDG_CONFIG_HOME` matters from Step 5 on; setting it earlier is
harmless.

### Step 1 — Worker `join`, `whoami`, standing roles, takeover

Issue: [#238](https://github.com/RoboNater/robomate/issues/238).

**Scope.**
- `robomate mcp` starts unjoined without `--role`; `join` and `whoami` for workers, with
  `hub=` resolution (see [join](#join-and-whoami-5)). `--role`/`--name`/`AGENT_NAME` and
  `check_in` stay as aliases. `--harness` is accepted and recorded.
- Standing roles `implementer`, `reviewer`, `worker` stored on the agent row (the step's
  reserved DB version);
  `assign_task` refuses an incompatible role.
- Name validation as for hub names (§5). 409 on a live name; `takeover=true` supersedes the
  old instance and reattaches its task.
- The join response for a worker: identity, hub (`hub_id`, name), repo, forge, default
  branch, `workspace_path`/`workspace_id` and whether the bridge's working directory is
  inside it, the worker guide and its hash, any reattached task, and the next call.
- `ask_orchestrator`, with `ask_alice` as an alias. #87 per the join design.
- `scripts/mock-worker.py` gains `--join ROLE`, which joins through `robomate mcp` instead
  of checking in (the mocks become `robomate certify`'s engine, Appendix B).
- §4, §5, §8 deltas; `guides/worker.md`, `prompts/worker.md` (kept in step by its test) and
  `skills/worker/SKILL.md` describe join, and `check_in` for prepared runs until Step 11.

**Tests** (`tests/test_cli.py`, `tests/test_worker_tools.py`, `tests/test_worker_client.py`,
`tests/test_store.py`, `tests/test_rpc.py`, `tests/test_database.py`,
`tests/test_mock_worker.py`).
- An unjoined bridge refuses every tool but `join` and `whoami`, naming `join`.
- Join, then the same name from a second bridge: 409; with `takeover`, the first bridge's
  heartbeats are ignored and its held task comes back in the second join's response, still
  `working`, with the same task ID and no `worker_lost` event.
- `assign_task` of a reviewer task to an `implementer` is refused with -32602; `worker`
  accepts every task role.
- `join(hub=<name>)` from a temporary directory outside any checkout reaches that hub; an
  unknown name lists the live hubs.
- A database at the previous version migrates to the step's version with existing agents
  as `worker`.
- The prepared-run path: a bridge started with `--role worker --name bob` still checks in
  and takes a task.
- Wait defaults stay at or below 100 s (#12, taken into this step because it rewrites the
  text that holds them). One named constant, with a comment citing #12 and
  robo-agents #109, bounds `worker_mcp.config.DEFAULT_WAIT_S`, the `wait_for_event`
  default as the orchestrator tool's input schema exposes it, and every `timeout_s=` in
  `guides/worker.md` and `prompts/worker.md` (`await_assignment`, `ask_orchestrator` and
  the `ask_alice` alias). Step 2 extends the same test to `guides/orchestrator.md`. Setting
  any of them to 120 makes the test fail, naming the place. M3 replaces the constant with
  per-harness limits (§9.2), which is what keeps #12 open.

```sh
uv run --locked pytest tests/test_cli.py tests/test_worker_tools.py tests/test_worker_client.py \
    tests/test_store.py tests/test_rpc.py tests/test_database.py tests/test_mock_worker.py \
    tests/test_runtime_content.py tests/test_worker_config.py
# inside the isolated smoke recipe:
uv run --project <checkout> python <checkout>/scripts/mock-worker.py --agent bob --join implementer
#   -> prints the join response: identity, hub name, forge, worker guide hash
```

**Size:** medium.

### Step 2 — Orchestrator `join` and `guides/orchestrator.md`

Issue: [#239](https://github.com/RoboNater/robomate/issues/239).

**Scope.**
- Orchestrator `join` over `/rpc` with the session headers; one orchestrator per hub;
  `takeover` replaces the M1 newest-session rule's implicit form, keeping its refusal of
  old sessions and its lease release.
- `guides/orchestrator.md` from the skill, forge-neutral; orchestrator-only appendices
  `guides/forge/orchestrator-github.md` and `orchestrator-gitlab.md` with the merge and
  read-back commands (still `--delete-branch` until Step 8). The guide route serves
  `orchestrator.md` with the orchestrator appendix.
- The orchestrator join response (see [the guide](#guidesorchestratormd-and-the-orchestrators-join-step-2)),
  with "unchanged (hash)" on a rejoin.
- `skills/alice-orchestrator/SKILL.md` becomes a pointer to `join`; `prompts/alice.md`
  stays for prepared runs; the policy-block tests follow. `scripts/mock-alice.py` gains
  `--join`.
- §5, §10, §14/§15, Appendix B deltas; the `reviewer_harness_differs` decision recorded.

**Tests** (`tests/test_guides.py`, `tests/test_runtime_content.py`, `tests/test_rpc.py`,
`tests/test_orchestrator_bridge.py`, `tests/test_mock_alice.py`).
- A second orchestrator join is refused; with `takeover`, the old session's calls are
  refused and its event leases released.
- `guides/orchestrator.md` on a `gitlab` hub carries the GitLab orchestrator appendix, on a
  `github` hub the GitHub one; no worker guide contains a merge command.
- Step 1's wait-limit test (#12) also covers every `timeout_s=` in `guides/orchestrator.md`.
- The join response with an active workflow contains the resume procedure and the
  unacknowledged deliveries; a rejoin returns the hash only.
- The PR reports the served orchestrator bytes, before (skill plus prompt) and after.

```sh
uv run --locked pytest tests/test_guides.py tests/test_runtime_content.py tests/test_rpc.py \
    tests/test_orchestrator_bridge.py tests/test_mock_alice.py
# inside the isolated smoke recipe:
uv run --project <checkout> python <checkout>/scripts/mock-alice.py --join
#   -> prints the orchestrator join response with the guide hash
```

**Size:** medium. Mostly prose, but every orchestrator reads it.

### Step 3 — Run identity, sequential workflows, queue

Issue: [#240](https://github.com/RoboNater/robomate/issues/240).

**Scope.**
- The step's reserved DB version: `run_id`, `statement_sha256`, `queued`, the `statement`
  table (see
  [Run identity](#run-identity-and-history-step-3)). Legacy workflows get a `run_id`.
- `start_workflow(statement?, policy?)`; `initialize_workflow` as an alias scoped to the
  current workflow. One live workflow at a time.
- `get_state` carries `run_id`, the queue and the previous run's summary; assignments carry
  `run_id`. Policy `on_done`. The policy blocks that `tests/test_runtime_content.py` pins
  (`prompts/alice.md`, and the skill or `guides/orchestrator.md`) follow `WorkflowPolicy`.
- `runs/<run_id>/manifest.json` at start and end, written atomically.
- `robomate status` shows the run ID and queue length. `prepare-run.py`'s
  `check_hub_workflow` accepts a hub whose workflows are all `done`.
- §7.1, §7.2, §11 deltas.

**Tests** (`tests/test_store.py`, `tests/test_rpc.py`, `tests/test_database.py`,
`tests/test_cli.py`, `tests/test_prepare_run.py`).
- Two statements queued; the second starts only after the first is `done`; a third
  `start_workflow` while one is active is refused with -32002.
- `initialize_workflow` with the same goal confirms the active workflow; after `done` it
  starts a new one with a new `run_id`.
- A database at the previous version with one `done` workflow migrates, gets a `run_id` from its
  `created` date, and accepts a new workflow.
- The manifest is written at start and completed at `done`, and a hub restart in between
  leaves it valid JSON.
- `on_done: release` with an empty queue releases workers; `standby` keeps them.

```sh
uv run --locked pytest tests/test_store.py tests/test_rpc.py tests/test_database.py \
    tests/test_cli.py tests/test_prepare_run.py
```

**Size:** medium to large.

### Step 4 — `robomate submit`, `get_statement`, preflight at submit

Issue: [#241](https://github.com/RoboNater/robomate/issues/241).

**Scope.**
- `robomate submit <file> | --issue N | -`, `--policy k=v`, through an operator-only
  `hub.submit` method; `work_submitted` event; the size cap (#10) with an actionable error.
- `get_statement(sha)` for workers (A2A) and the orchestrator; assignments carry
  `statement_sha256`. The orchestrator's instructions tell Alice to reference scope, not
  restate it (#9): `guides/orchestrator.md` if Step 2 has landed, otherwise the skill,
  whose content Step 2 then carries over.
- Policy-dependent forge checks at submit, from the hub's functions; #96's origin rule;
  #93's cases as tests of the shared code. `prepare-run.py`'s duplicate GitLab checks call
  the shared functions until Step 11.
- `robomate status` shows queued statements. §2, §7.1, §8, §10 deltas.

**Tests** (`tests/test_cli.py`, `tests/test_rpc.py`, `tests/test_worker_tools.py`,
`tests/test_forge_preflight.py`, `tests/test_prepare_run.py`).
- The stored bytes and SHA-256 equal the file's, including CRLF and a trailing newline;
  `get_statement` returns them unchanged; an unknown SHA is -32001.
- A statement over the cap is refused through `main()` with the cap in the message, and
  nothing is queued.
- `submit` without the operator credential is refused with -32005.
- GitLab: each matrix cell at submit; `allow_no_ci=true|false` runs no CI probe; a non-404
  failure with `auto` refuses and names the check; a 404 only in the argument list is not
  read as "no CI file". GitHub runs no `glab`.
- A hub whose `hub.json` has no origin refuses submit.
- `robomate submit` without `--policy` takes the repository layer's `max_review_rounds`;
  with `--policy max_review_rounds=5` it takes 5.

```sh
uv run --locked pytest tests/test_cli.py tests/test_rpc.py tests/test_worker_tools.py \
    tests/test_forge_preflight.py tests/test_prepare_run.py
# inside the isolated smoke recipe:
printf 'Fix the typo in README.\r\n' > "$T/sow.md"
rb submit "$T/sow.md"
rb status               # one queued statement and its SHA-256
sha256sum "$T/sow.md"   # equals the stored SHA-256
```

**Size:** medium.

### Step 5 — Configuration layers

Issue: [#242](https://github.com/RoboNater/robomate/issues/242).

**Scope.**
- `agent_hub_common.config_layers`: the three files, first match wins, and the §4
  key-to-layer table enforced with errors that name the file and key.
- `forge.*` from the repository and user layers; the legacy `[forge]` table honoured with a
  warning. The resolved policy defaults, agent defaults and `setup` are returned by the
  reader; their consumers come later: `submit` applies the policy defaults (Step 4), and
  the join response carries `setup` and applies the agent pins (Step 7b). The M4 keys are
  parsed and validated only.
- The isolated smoke hub sets `XDG_CONFIG_HOME`/`APPDATA` (§4); `guides/worker.md` and
  AGENTS.md follow.
- §4 deltas.

**Tests** (`tests/test_config_layers.py`, new; `tests/test_forge_detection.py`,
`tests/test_cli.py`).
- Precedence for each key class, on Linux and Windows paths; `worktree_root` in a hub
  `config.toml` is an error; `nits_issue` in the user layer is an error.
- A legacy `.robomate/config.toml` `[forge]` still selects GitLab and warns.
- The reader resolves `max_review_rounds` from the repository layer over the user layer,
  and a hub `config.toml` over both.

```sh
uv run --locked pytest tests/test_config_layers.py tests/test_forge_detection.py tests/test_cli.py
```

**Size:** small to medium.

### Step 6 — State and token in `<worktree_root>/<hub>/`, bindings, `robomate relocate`

Issue: [#243](https://github.com/RoboNater/robomate/issues/243).

**Scope.**
- The resolver takes a binding or registry entry; new hubs' state in
  `<worktree_root>/<hub>/state/`, token in `<worktree_root>/<hub>/token/token`; the
  `robomate-root.json` ownership record.
- Owner bindings at `up`; legacy hubs run in place with a binding. Discovery rule 2
  follows the binding; a missing or stopped bound hub is an error naming it (§4).
- `robomate relocate`; `up` refused in an agent-bound checkout. Step 6 defines the binding
  format for both roles and writes owner bindings; Step 7a writes agent bindings, so Step 6's
  tests write an agent binding by hand.
- Every reader of `.robomate/` (fact 4) on the resolver; `prepare-run.py`'s `hub.db` read
  and `scripts/hub-report.py` take the state location from `robomate status --json`.
- §4, §12 deltas.

**Tests** (`tests/test_discovery.py`, `tests/test_cli.py`, `tests/test_main.py`).
- A new hub from a linked worktree keeps its state after `git worktree remove` of that
  worktree: `robomate ls` still lists it, `robomate status --hub <name>` reaches its state,
  and `robomate status` in the main checkout does not attach to it (no silent attach).
- Token directory mode 0700, token 0600; `state/` holds no token.
- A legacy hub starts in place with the same `hub_id`, port and token, and its binding
  names `.robomate/`. After `relocate`, the same `hub_id` serves from the new location,
  bindings point there, and the old token file is gone.
- A root created by repository A is refused for repository B with the same `<repo>` name.
- No module outside `discovery.py` names `.robomate`.

```sh
uv run --locked pytest tests/test_discovery.py tests/test_cli.py tests/test_main.py
# inside the isolated smoke recipe:
rb status                   # State: $T/repo.robomate/<hub>/state
ls -ld "$T/repo.robomate"/*/token   # drwx------ (0700)
```

**Size:** medium to large.

### Step 7a — Worktrees at join

Issue: [#244](https://github.com/RoboNater/robomate/issues/244).

**Scope.**
- Worktree creation at join: the hub runs `git worktree add --detach <root>/<hub>/<agent>/`
  at join, writes the agent binding, locks with the hub's reason, and reuses per
  `(hub, agent)`; the orchestrator gets one too (§1 principle 5).
- Workspace identity for worktrees (fact 3); the step's reserved DB version records the
  workspace path and kind (`worktree` or `clone`).
- `up` refused in an agent worktree naming the hub; `state` and `token` refused as agent
  names.
- Prepared runs keep their clones: a join with an explicit `HUB_WORKSPACE` creates no
  worktree and keeps its full-clone check.
- Decide r2-2 on #244: refuse a join whose explicitly configured workspace has no matching
  identity, keeping the lenient path only for the bridge-cwd fallback.
- §6, §12 deltas for worktree creation at join.

**Tests** (`tests/test_worktrees.py`, new; `tests/test_cli.py`,
`tests/test_rpc.py`).
- Join creates `<root>/<hub>/<agent>/` detached at `origin/<default>`, locked with the
  hub's reason, with an agent binding; a second join reuses it.
- `up` in that worktree is refused naming the hub; `git worktree remove` of it is refused
  while locked.
- Names `state` and `token` are refused as agent names.
- Two hubs of one repository create worktrees without collision.
- The orchestrator's join creates its own worktree the same way.
- A join with an explicit `HUB_WORKSPACE` creates no worktree and keeps its full-clone
  check.
- A join with an explicitly configured workspace and no matching identity is refused;
  the bridge-cwd fallback stays lenient.

```sh
uv run --locked pytest tests/test_worktrees.py tests/test_cli.py tests/test_rpc.py
# inside the isolated smoke recipe:
git worktree list --porcelain   # bob's worktree, locked with the hub's reason
```

**Size:** medium.

### Step 7b — Creating worktrees before join

Issue: [#314](https://github.com/RoboNater/robomate/issues/314).

**Scope.**
- Worktree creation before join through `robomate workspace <name>` and
  `robomate up --agents …` (operator-only `hub.workspace`); each prints its path.
- The join response returns `setup` and applies the agent pins from the layers (Step 5).
  They sit here, not in 7a, to keep 7a small: until 7b lands, worktrees created at join
  get no `setup`, which is harmless while prepared runs remain the working path,
  through Step 10.
- §6, §12 deltas for creation before join.

**Tests** (`tests/test_workspace.py`, `tests/test_cli.py`, `tests/test_rpc.py`).
- `robomate workspace bob` without the operator credential is refused.
- `robomate workspace bob` and `robomate up --agents …` create the worktree ahead of the
  join, with an agent binding and the hub's lock; a later join reuses it.

```sh
uv run --locked pytest tests/test_workspace.py tests/test_cli.py tests/test_rpc.py
# inside the isolated smoke recipe:
rb workspace bob                # prints $T/repo.robomate/<hub>/bob
```

**Size:** small to medium.

### Step 7c — Branches

Issue: [#315](https://github.com/RoboNater/robomate/issues/315).

**Scope.**
- The hub names the branch in a run's first implementer assignment,
  `robomate/<hub>/<agent>/<label>`, with `<label>` derived from the statement (issue number
  or a slug) and validated like a name. It is refused if the ref exists locally or on
  origin, and later tasks of the run reuse it; the workflow row records it (the step's
  reserved DB version).
- The guide lines for the named branch.
- §6 deltas for branch naming.

**Tests** (`tests/test_worktrees.py`, `tests/test_rpc.py`).
- The first implementer assignment names the branch; an existing local or remote ref is
  refused; a later task in the run reuses it.
- The workflow row records the run's branch.
- Two hubs of one repository name branches without collision.

```sh
uv run --locked pytest tests/test_worktrees.py tests/test_rpc.py
```

**Size:** small to medium.

### Step 7d — Agent boundaries and grants

Issue: [#316](https://github.com/RoboNater/robomate/issues/316).

**Scope.**
- §12 agent boundaries and §6 hygiene in the served guides: never read the hub's state or
  token directory, start every task from fresh `origin/<default>` or the assigned SHA,
  report `blocked` on uncommitted changes, retry a `cannot lock ref` after a short wait and
  never delete a `.lock` file, never change git config. The bridge's `whoami` names the
  token file it reads; the guide says that the bridge, not the agent, reads it. Scratch
  stays outside the worktree.
- The Linux grants Claude Code and Codex need to commit and push from a worktree,
  recorded in `docs/user-guide.md`. Measured in the harnesses' real sandboxes, so this
  step needs the operator.
- §6, §12 deltas for boundaries, hygiene and grants.

**Tests** (`tests/test_guides.py`).
- The served worker guide states the boundaries and scratch rule; the bridge's `whoami`
  names the token file.
- `docs/user-guide.md` records the Linux grants for Claude Code and Codex.

```sh
uv run --locked pytest tests/test_guides.py
```

**Size:** small to medium.

### Step 8 — `robomate clean` and branch deletion after merge

Issue: [#245](https://github.com/RoboNater/robomate/issues/245).

**Scope.**
- `robomate clean [--hub …]` per §6, operator-only, never removing state or `runs/`.
- The §15 decision in the orchestrator appendices: GitHub merge without `--delete-branch`,
  then a remote-only delete. A live check on the GitHub sandbox of what `--delete-branch`
  does with the branch checked out in another worktree, recorded in the PR.
- §6, §14, §15 deltas.

**Tests** (`tests/test_clean.py`, new; `tests/test_guides.py`).
- `clean` removes clean worktrees, refuses one with uncommitted changes and one with
  unpushed commits, prunes merged `robomate/<hub>/…` branches only, and leaves state and
  `runs/` untouched.
- An orphaned worktree (hub state removed by hand in the test) is reported and removed on
  the same terms.
- The served GitHub orchestrator appendix has no `--delete-branch`.

```sh
uv run --locked pytest tests/test_clean.py tests/test_guides.py
# live, on the GitHub sandbox, with a scratch PR whose branch is checked out in a second worktree:
gh pr merge <scratch PR> --squash --delete-branch --match-head-commit <head>   # record the output
```

**Size:** small to medium.

### Step 9 — Clone fallback for remote agents

Issue: [#246](https://github.com/RoboNater/robomate/issues/246).

**Scope.**
- `robomate workspace <name> --clone <path> --hub-url URL --token-file F` on the agent's
  host: rejects unexpanded variables and placeholders, probes `/healthz` and the agent card
  before writing anything (#90), clones origin, and writes the clone's identity.
- `join` detects a clone and says so (§6). The remote bridge uses the explicit URL and
  token file; the static entry stays token-free.
- `docs/user-guide.md` remote-agent section; §4, §6 deltas.

**Tests** (`tests/test_cli.py`, `tests/test_worker_config.py`, `tests/test_workspace.py`).
- `--hub-url 'http://$HUB_IP:8420'`, `%HUB%`, `<addr>` and `{x}` are refused naming the flag;
  an unreachable hub refuses before any file is written; an agent card whose URL differs
  from `<hub-url>/a2a` refuses.
- A clone joins and the response says `clone`.

```sh
uv run --locked pytest tests/test_cli.py tests/test_worker_config.py tests/test_workspace.py
# live, operator: hub in WSL with --bind/--public-url, then on native Windows (PowerShell):
uv run --project <checkout> robomate workspace bob --clone C:\agents\bob --hub-url http://<addr>:<port> --token-file <file>
```

**Size:** small to medium.

### Step 10 — Headless agents without generated configs

Issue: [#247](https://github.com/RoboNater/robomate/issues/247).

**Scope.**
- `scripts/agent-launcher.py start|resume|ask <agent>`, the successor of `alice-launcher.py`,
  `worker-launcher.py`, `agent-recovery.py` and `agent-ask.py`, which stay for prepared
  runs until Step 11. It shares their harness adapters, as in
  [What replaces the prepare-run run path](#what-replaces-the-prepare-run-run-path): join
  line as the prompt, worktree from `robomate workspace`, grants as flags, backoff,
  session log with `run_id` in the hub's `sessions/`, guarded resume taking the run from
  the hub (#223), `--interactive` (#222), `--sessions-dir` on a remote host. All five
  harness adapters move with it.
- Provide the separate optional Denise entry described in the replacement table as M7
  becomes available. Read selected hub/run metadata locally, preserve her isolated
  no-MCP profile, local guide, access inventory and reports, and keep hub-down startup.
  This does not gate Step 10 on M7 completion; Step 11 finishes and validates the handoff.
- `docs/development/agent-recovery.md` rewritten for join takeover and the launcher.
- §9.4, Appendix B deltas.

**Tests** (`tests/test_agent_launcher.py`, new, with the fake harnesses that
`tests/test_alice_launcher.py`, `tests/test_worker_launcher.py` and
`tests/test_agent_recovery.py` use today; those stay green until Step 11).
- Each team harness's start command contains the join line and no file path of a rendered
  config; no file is written outside `sessions/`.
- For the doctor integration, fake-harness tests exercise startup with HTTP unavailable,
  explicit hub/run selection, separate robomate/target paths, no join/MCP/supervision,
  scoped grants and retained reports. Doctor files stay under the selected run's
  `denise/`, separate from team session logs. Complete these cases before Step 11 removal.
- Kill the fake harness mid-task: the launcher resumes the same conversation with the
  continuation text and backoff; the session log rows carry the hub's `run_id`.
- `resume` with an empty log refuses; with a log from before the workflow started it takes
  the run from the hub (#223).
- `--interactive` starts without automatic resume and still records the session.
- The Windows CI job runs the same tests.

```sh
uv run --locked pytest tests/test_agent_launcher.py tests/test_alice_launcher.py \
    tests/test_worker_launcher.py tests/test_agent_recovery.py
```

**Size:** medium to large.

### Step 11 — Retire the prepare-run path

Issue: [#248](https://github.com/RoboNater/robomate/issues/248).

**Scope.**
- Requires completed M7 and a proven doctor replacement from Steps 10/11 (#247/#248),
  in addition to the M2 step dependencies. Exercise the successor doctor entry on Bash
  and native PowerShell, including a stopped hub, separate target repository, effective
  repair grants and retained reports. Preserve the local guide and isolated doctor
  profile; do not remove their only generator/entry point. No other M2 implementation
  step waits for M7 completion.
- Remove `scripts/prepare-run.py`, `bootstrap-workspace.*`, `prep-standard-run*`,
  `archive-run.py`, the old launchers, the rendering half of `run_common.py`, `prompts/`,
  `skills/worker`, and the `runtimes/` templates (Appendix B), with their tests and
  fixtures. Keep `mock-alice.py` and `mock-worker.py` (M3 certification) on `join`.
- Remove the aliases: `check_in`, `ask_alice`, `initialize_workflow`, `robomate mcp --role`.
- Rewrite `docs/user-guide.md` around `robomate up`, the static MCP entries for Claude Code
  and Codex, `robomate workspace`, join lines, `submit`, `status`, `clean`, remote agents
  and the launcher (Appendix B: "M2, M3"). The §12 operator-only steps, now with
  Step 7b `workspace` and `clean`, go into the served worker and orchestrator guides.
- Close #189, #190, #219, #220 and #222 if not closed earlier, with a pointer to the PR.
- §2, §13, Appendix B deltas.

**Tests.** The full suite on Linux and native Windows, and:

Run the doctor integration cases from Step 10 and M7's live acceptance through the
replacement entry point before removing the old wrappers. Record the commands and
evidence for both platforms; fake-harness tests alone do not prove real access grants.

```sh
git grep -n 'prepare-run\|check_in\|ask_alice\|initialize_workflow' -- ':!docs/historical' ':!docs/evidence' ':!docs/development/implementation-plan-mvp-m*.md'
#   -> no hits outside historical records
uv run --locked pytest
```

**Size:** large, mostly deletions. Split documentation from removal if review needs it.

### Step 12 — M2 acceptance run

Issue: [#249](https://github.com/RoboNater/robomate/issues/249).

**Operator prerequisites.**
- Claude Code and Codex installed and logged in on the hub host (WSL or Linux), each with
  the static robomate entry from the user guide, written once, and the Step 7d grants.
- `gh` authenticated with the shared account; two unrelated robomate issues small enough
  for one PR each (for example items from #4), one per hub.
- A robomate checkout at the commit under test, and a linked worktree of it for the second
  hub (`git worktree add ../robomate-m2b`). No run directory exists for either run.
- Agents' names and roles: `alice`/orchestrator and `bob`/implementer on Claude Code,
  `charlie`/reviewer on Codex, in both hubs (names are per hub).

**Scope.** Meet the §13 done-when on robomate itself.
- Hub A: `robomate up` in the main checkout; `robomate up --agents alice,bob,charlie`, or a
  first join, creates the worktrees. The operator starts each harness in its worktree and
  gives it its one-line join, typed in an interactive session or passed as the Step 10
  launcher's prompt; the evidence says which. `robomate submit --issue <N>`. The run ends
  with a merged PR.
- Hub B: while hub A's workflow is `active`, the same in the linked worktree with the
  second issue, to a merged PR. `robomate ls` shows both hubs live with distinct names,
  state and ports.
- After both merge: `robomate clean` on each hub.
- Optional, not part of the done-when: one IDE join (Devin Desktop's Devin Local or
  Cline) per [Joining from an IDE](#joining-from-an-ide), as a fourth agent or a
  replacement reviewer on hub B, recorded with its outcome either way.
- Evidence goes in `docs/evidence/m2-<run-id>.md`, modelled on M5 Step 5, with hub A's
  run ID in the name, using only these facts:
  - The robomate commit, `claude --version`, `codex --version`, `gh --version`.
  - Both `robomate up` outputs and `robomate ls` with both hubs live.
  - The three join lines per hub as given, and from each join response the identity, hub
    name, forge and `workspace_path`.
  - `git worktree list --porcelain` showing each agent's worktree, locked, under the
    hub's namespace; each implementer branch `robomate/<hub>/bob/<label>`.
  - Each statement's SHA-256 from `robomate status` against `sha256sum` of the rendered
    statement, and each `get_statement` call in the hub's call accounting.
  - No `prepare-run.py`, no generated config, no pasted prompt: the commands the operator
    ran, in order, and a listing showing no run directory.
  - Overlap: each workflow's `active` interval from `runs/<run_id>/manifest.json` and the
    hub's events, with the intervals intersecting.
  - Every `check_merge_gate` reading behind each merge decision; the approved head equal
    to the PR head accepted at merge; the squash merge commit recorded separately; the
    remote branch deleted and the local branch pruned by `clean`.
  - Bytes by role from `hub-report`, against a prepared run's as the before figure.
  - Every attempt, including failed ones (poc-lessons).

**Tests.** The run itself. The PR adds only the evidence document and any small fixes the
run exposed. Fixes of more than a few lines get their own issue.

## Risks

| Risk | Mitigation |
|---|---|
| A step breaks prepared runs, which robomate is built with until Step 11 | Aliases and `--role` kept through Step 10; each step's tests include the prepared-run path; Step 11 is the only removal |
| Two DB steps in flight at once take migration numbers out of order | Reservations by step; swap on #2 before merging (Reservations) |
| An IDE starts MCP servers outside the open folder, so discovery finds no binding | `join(hub=)`, and the join response says where the bridge runs; one IDE join recorded in Step 12 |
| Codex's sandbox blocks writes to the git common directory from a worktree (#11 shape) | Step 7d records the Linux grants for Claude Code and Codex; static per repository; Windows Codex and other harnesses in M3 |
| `gh pr merge --delete-branch` reports failure after a successful merge | Remote-only delete (Step 8), verified live |
| The orchestrator guide in the join response costs tokens | Once per session with a hash; bytes reported in Step 2; trimming is M6 |
| A crashed hub leaves locked worktrees | The lock reason names the hub; `clean` and `git worktree list --porcelain` report them; `unlock` stays an operator action |
| Closeout report files disappear with the run directory before #215 lands | Prepared runs keep them until Step 11; #215 can start once Step 3 lands |
| Moving state loses a legacy hub's history | Nothing moves without `robomate relocate`; tests start a legacy hub in place |
| Windows and remote topologies lose their path before the replacement is proven | Step 11 needs Steps 9 and 10; both run their tests on the Windows CI job, and Step 9 has a live WSL-to-Windows check |
| A worker's smoke hub reaches the run's hub through a binding | Isolated smoke rules gain `XDG_CONFIG_HOME` (Step 5); explicit-URL variables unset; a temporary clone, never the agent worktree, where `up` refuses (Step 6) |

## Roadmap bookkeeping

- On merge of this plan, #2's M2 line "Milestone work per mvp-spec §13 (issue to be
  written)" becomes a checked line linking this plan (#226), in the M5 form, and one
  unchecked line per step issue follows, #238–#249, each with the GitHub milestone
  "M2 Join, worktrees, work intake" and its Tests section.
- **Dispositions on #2:** #8 closes with Steps 1–2 and #9 with Step 4; #10 is carried into
  Step 4; #90 into Step 9. The "Inherit from M5" line is met by Steps 1, 2 and 4. The
  "Inherit from #181" line moves to M3 with `robomate install` (above). #93 and #96 are
  added under M2 with Step 4.
- **Reservations:** none made by this plan. Steps 1, 3, 7a and 7c each reserve one DB version
  on #2 when they are picked up ([Reservations](#reservations)). Wire `hub.schema_version`
  stays 1.
- The interim issues outside the M2 section (#188, #189, #190, #219, #220, #222, #223,
  #232) keep their tracks; their issues get a comment with the disposition above when the
  step that settles them lands.
