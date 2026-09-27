# robomate MVP — Spec & Implementation Plan

Design of record for **robomate** (`RoboNater/robomate`), the successor to the robo-agents
proof of concept. The repository is seeded with the PoC files; [`docs/poc-spec.md`](poc-spec.md)
is frozen as the PoC's historical design, and everything it established stays in force unless
this document changes it. Bare issue numbers such as #8 refer to robomate issues; issues in
`RoboNater/robo-agents`, where the PoC was built, are written robo-agents #N, except in
Appendix A, which lists robo-agents issues and says which of them carry over.
[`docs/robo-agents-issues.md`](robo-agents-issues.md) maps each carried issue to its robomate
number. PoC lessons are in [`docs/poc-lessons.md`](poc-lessons.md), exported from
robo-agents #57; the PoC's README is [`docs/poc-readme.md`](poc-readme.md).

**Naming.** Repository, Python distribution, CLI, MCP server name, and skill are all
`robomate`. Tools appear to agents as `robomate` tools (e.g. `mcp__robomate__join` in Claude
Code). robomate ships no short alias; operators may add their own (e.g. `alias rb=robomate`).

---

## 1. Goals / non-goals

**MVP success statement**
> The MVP succeeds when the operator can, on a GitHub repository and on a self-hosted
> GitLab project, start one hub with one command, bring up an orchestrator and two workers
> on any certified harnesses with a one-line "join" prompt each, submit a statement of work,
> and receive a merged PR/MR whose leftover review findings are each filed, noted, or
> dropped with a recorded reason — with no generated
> launch commands, no hand-edited per-run configs, no human prompts to workers after join,
> and recovery from a restart of the hub or any single agent.

**Goals — the three pillars**
1. **Ease of use.** `robomate up` in a repo, agents join by name and role, `robomate submit`
   the work. robomate provisions workspaces, serves instructions, and reports status.
2. **Harness breadth.** Any MCP-capable harness can take any role with any model and effort
   setting. One static install per harness, at user or project scope. Support is measured by
   a conformance test, not asserted.
3. **GitLab.** GitHub and GitLab (including a self-hosted instance) with the same workflow
   invariants: SHA-bound review and merge, stale-base detection, CI gate.

**Carried-over quality goals** (issues from PoC use that bite daily): runs never end `done`
unmerged, and leftover findings are dispositioned without the operator chatting with agents;
restarts and laptop standby don't strand a run; token usage per run is measured and reduced.

**Non-goals (MVP)**
- Multiple users or teams; per-agent credentials; cloud-hosted harnesses (Devin, RooMote).
- Concurrent workflows in one hub (runs are sequential; a queue is in scope).
- Hub-mediated forge operations. Agents run `gh`/`glab` themselves (§10).
- Dashboard, epics/boards, assignment from a UI, a PM/lead agent (§16).
- Non-PR deliverables (robo-agents #102).

**Principles** (PoC principles plus what PoC use taught)
1. **The hub waits; agents are disposable.** Durable state lives in the hub. Every
   assignment is self-contained (references + statement of work hash), so an agent session
   can be restarted or cleared between tasks at no loss.
2. **Instructions come from the hub.** Tool descriptions, the `join` response, and served
   guides. No role depends on a harness-specific skill mechanism.
3. **Agents drive the forge CLIs.** They can read `--help`, react to errors, and iterate.
   The hub reads forge facts for the merge gate; it does not perform forge writes.
4. **The hub checks facts; agents make judgment calls and record them.** A close-out rule
   either is checked in code (merge state) or is forced into a typed, validated result (every
   leftover finding dispositioned) — never left to prose
   ([`docs/poc-lessons.md`](poc-lessons.md): "what no step verifies, no one does").
5. **The operator's checkout is never touched.** Every agent, including the orchestrator,
   works in its own worktree.
6. **Evidence belongs to its run** ([`docs/poc-lessons.md`](poc-lessons.md)). Runs have
   identity and a manifest by default.
7. **Nothing enters an agent's context that it doesn't need.** Payload size is a design
   constraint, not a later optimization.

---

## 2. The MVP experience

```sh
uv tool install git+https://github.com/RoboNater/robomate    # once per machine
robomate install claude-code --scope user                   # once per harness
cd ~/src/my-repo && robomate up                             # starts this repo's hub
# In each harness session, one line:
#   "Join robomate as alice, orchestrator."
#   "Join robomate as bob, implementer."
#   "Join robomate as charlie, reviewer."
robomate submit sow.md     # or: robomate submit --issue 42 ; further submits queue up
robomate status            # repo, hub URL, agents, phase, PR/MR, gate, open questions
robomate report            # last run: merged SHA, each finding and where it went, steps
robomate inbox             # questions for you; answer with: robomate answer 3 "yes, file it"
robomate log --timeline    # drill down when something looks wrong
```

- `robomate up` detects the repo root, origin, forge, and default branch; runs preflight
  (forge CLI auth, CI presence, allowed merge method); prints the hub URL and the three
  join lines.
- For CLI harnesses, `robomate workspace bob` (or `robomate up --agents bob,charlie`) creates the agent's
  worktree and prints the path to start the harness in. IDE harnesses open that folder.
- The statement of work can also be pasted to the orchestrator in chat; `robomate submit` is
  preferred because it is stored byte-for-byte (§7.1).

---

## 3. Architecture

```
 operator terminal                  per-repo hub (robomate up)
 ┌───────────────┐  HTTP (local)  ┌───────────────────────────────────────────┐
 │ robomate      │───────────────▶│ hub daemon: HTTP A2A (workers)            │
 │ status/submit │                │           + JSON-RPC (orchestrator, CLI)  │
 │ inbox/report  │                │ SQLite state in <repo>/.robomate/         │
 └───────────────┘                │ worktree manager · merge-gate adapters    │
                                  └───────────▲───────────────▲───────────────┘
                                              │ HTTP          │ HTTP
 any harness ─ stdio MCP ─▶ robomate mcp ─────┘               │
 (alice)                    bridge: heartbeat, discovery      │
 any harness ─ stdio MCP ─▶ robomate mcp ─────────────────────┘  (bob, charlie, …)
```

**Changes from the PoC**

| PoC | MVP |
|---|---|
| Hub runs inside Alice's stdio MCP process | Hub is a standalone process started by `robomate up`; the orchestrator is an ordinary client |
| `worker-mcp` for workers; Alice speaks MCP to the hub directly | One bridge, `robomate mcp`, for every role; it discovers the hub, carries the heartbeat, and relays tool calls over HTTP |
| Identity, workspace, token baked into per-agent env and config | Static MCP entry; identity and role given at `join`; token found by discovery |
| Full clone per worker, created by `prepare-run.py` | Worktree per agent, created by the hub on demand; clone fallback for other hosts/OS |
| Alice's goal and policy in her kickoff prompt | Statement of work submitted to the hub, stored verbatim with its SHA-256 (#9) |
| Alice instructions in a Claude Code skill | `guides/orchestrator.md` served by the hub; any harness can orchestrate |
| One workflow per hub state dir, fresh state per run | Persistent per-repo hub; sequential workflows with history and a queue |
| Close-out rules in skill prose; operator reads agents' close-out reports and asks follow-ups in chat | Hub-built close-out task with a typed result, a rendered run report, and a `done` check on merge state (§7.3) |

The worker A2A protocol (§4.1 of the PoC spec) and the typed result schemas (§4.4) are
unchanged. Wire `hub.schema_version` stays 1 unless a typed body changes shape.

---

## 4. Local layout, discovery, multiple hubs

**Per-repo state** — `<repo>/.robomate/`, added to `.git/info/exclude` (never to a
tracked `.gitignore`):

| Path | Contents |
|---|---|
| `hub.json` | `repo_root`, `origin`, `forge`, `default_branch`, `url`, `port`, `pid`, `started_at`, `robomate_version`, `hub_id` |
| `hub.db` | SQLite state (schema continues from PoC v12; reserve versions per the roadmap rule) |
| `token` | per-hub bearer token, mode 0600 |
| `up.lock` | OS-held per-repo lock while a hub starts or runs; prevents concurrent `up` |
| `config.toml` | optional operator settings: worktree root, agent defaults, policy defaults, forge hosts, setup command |
| `runs/<run-id>/` | run manifest and exported evidence (§11) |

`hub_id` is retained across restarts and returned by the public `/healthz` route
for live-hub checks. `hub.info` and `hub.shutdown` are authenticated `/rpc`
operator methods.

**Machine registry** — `$XDG_STATE_HOME/robomate/hubs.json` (Windows:
`%LOCALAPPDATA%\robomate\hubs.json`): one entry per running hub; stale entries pruned by
pid and `/healthz`. `robomate ls` lists them: repo, URL, agents joined, phase.

**Port** — first free port from 8420 upward on first `robomate up`, then reused on restart so
remote agents keep working; `--port` pins it.

**Process** — foreground by default (visible log, Ctrl-C stops it, same on Windows and
Linux). `robomate down` stops it from another terminal. Background/service mode is post-MVP.

**`robomate mcp` hub discovery**, in order:
1. `ROBOMATE_HUB_URL` (+ `ROBOMATE_TOKEN` or `ROBOMATE_TOKEN_FILE`) — explicit, for remote agents.
2. From the working directory: `git rev-parse --git-common-dir` → owning repo root →
   `.robomate/hub.json`. Works from the main checkout and from every worktree of it.
3. If exactly one hub is registered on the machine, that hub.
4. Otherwise `join` fails with the list from the registry and asks for `hub=<repo name or path>`.

In M1, before `join` exists, `robomate mcp` resolves the hub on its first tool call. An
ambiguous discovery returns the registered-hub list as a tool error.

**Identify-the-hub** — `robomate status` (from any directory in the repo or its worktrees) and the
`whoami` tool both report repo, origin, forge, URL, and the agents joined, so neither the
operator nor an agent can confuse two hubs.

**Remote agents** (e.g. hub in WSL, agent on Windows — proven in PoC Step 7): `robomate up --bind
0.0.0.0 --public-url http://<addr>:<port>`; the remote side sets `ROBOMATE_HUB_URL` and a token
file, and uses the clone fallback workspace (§6). Supported, not the primary path.

---

## 5. Identity, roles, and join

**`join(name, role, model?, effort?, harness?, hub?, takeover=false)`** — the one tool an
unjoined session can call besides `whoami`. Its description is the install-free instruction:
*"Call when the user asks you to join robomate. Use the name and role the user gives."*

- **Standing roles:** `orchestrator`, `implementer`, `reviewer`, `worker` (any task role).
  Task roles are `implementer`, `reviewer`, `rebase`, and `closeout` (new, §7.3). An
  `implementer` accepts implementer, rebase, and closeout tasks; a `reviewer` accepts reviewer
  tasks; `worker` accepts any. The hub refuses an incompatible `assign_task`.
- **One orchestrator per hub.** A second orchestrator join is refused unless `takeover`.
- **M1 before `join`:** `robomate mcp --role orchestrator|worker [--name NAME]` selects the
  bridge mode and name; a worker may instead use `AGENT_NAME`, and the orchestrator defaults to
  `alice`. The orchestrator bridge sends a session ID with its `/rpc` calls and a heartbeat
  every 30 s. The first call from a new session makes it current: calls from older sessions
  are refused, and outstanding event delivery leases expire for immediate redelivery.
  Worker tasks, leases, and heartbeats are unaffected. This is the reduced M1 form of
  orchestrator `takeover`; M2 replaces the flags with `join`.
- **Names** are unique per hub. Joining a name whose instance is alive returns 409 unless
  `takeover=true`, which supersedes the old instance at once (its heartbeats are ignored).
  A takeover of an agent holding a live task **reattaches** the task to the new instance and
  returns it in the join response, instead of failing it as `worker_lost`.
- **Profile** (observational, as in PoC §3): `harness`, `harness_version`, `provider`,
  `model`, `effort`, each with a source (`declared` / `install` / `unknown`), plus
  `capabilities`, `host`, `os`, `workspace_id`. `harness` is written by `robomate install` into the
  static MCP entry (`robomate mcp --harness opencode`); `model` and `effort` are declared by the
  agent or pinned by the operator in `config.toml`. Unknown stays `unknown`.
- **Role policy** (PoC `role_policy`) is still available and still defaults to
  `reviewer_harness_differs: true`; the orchestrator checks the joined pair against it at PLAN
  and escalates on violation. Any harness/model/effort may hold any role; guidelines on what
  works accumulate in `docs/harnesses.md`.

**Join response** (compact; the only long part is the guide on first join):
- identity confirmed; hub, repo, forge, default branch;
- `workspace_path`, `workspace_id`, and whether the bridge's working directory is inside it;
- the standing role's guide (worker etiquette, or the orchestrator guide), with its hash;
- for a worker: any reattached task; the next call to make (`await_assignment`);
- for the orchestrator: queue and workflow summary, unacked deliveries, the resume procedure
  if a workflow is active.

**Liveness and listening** (shown by `robomate status`):
- `alive` / `lost` — from the bridge's background heartbeat, as in the PoC (§4.3).
- `listening` — a hold (`await_assignment`, `wait_for_event`, `ask_orchestrator`) is in
  flight or ended less than `max_hold_s + 30 s` ago.
- `working` — has a task and is not holding.
- **`not listening`** — alive, no task, no hold for longer than that: the harness ended its
  turn. `robomate status` flags it for a nudge ("continue"). This is the expected failure mode of
  IDE harnesses and the signal a supervisor would act on.

---

## 6. Workspaces

**Policy**
- Every joined agent, orchestrator included, gets a **git worktree** named after it. The
  operator's own checkout is never modified except for `.robomate/` and worktree
  registration under `.git/`.
- **Location:** `<parent of repo>/<repo>.robomate/<name>/` by default (outside the repo, so IDE
  indexers, test discovery, and file search in the main checkout never see agents' copies).
  `config.toml: worktree_root` overrides.
- **Created** at `join` (or earlier by `robomate workspace <name>` / `robomate up --agents …`) with
  `git worktree add --detach <path> origin/<default>`. Reused across runs for the same name,
  so environment setup (venv, node_modules) is paid once.
- **Branches:** implementers create `robomate/<name>/<work-label>`; nobody else checks out a work
  branch. Reviewers and the orchestrator use **detached HEAD** at the assigned SHA — git will
  not check out one branch in two worktrees, and a detached review of the exact SHA is what
  the review invariant wants anyway.
- **New task hygiene:** the guide starts each task from a fresh `origin/<default>` (or the
  assigned SHA); an agent that finds uncommitted changes reports `blocked`, never discards them.
- **Setup:** `config.toml: setup = "uv sync"` is returned in the join response; the agent
  runs it in its worktree.
- **Cleanup:** `robomate clean` removes worktrees of released agents only when they have no
  uncommitted changes and no unpushed commits; it never deletes work (PoC rule).
- **Clone fallback:** an agent whose bridge cannot see the worktree path (another host, or
  Windows vs WSL — a worktree's `.git` file holds an absolute path from one OS) uses its own
  clone, identified as in the PoC (`HUB_WORKSPACE`, `workspace_id`). `join` detects this and
  says so.

**Worktree risks and mitigations**

| Risk | Mitigation |
|---|---|
| A branch can be checked out in only one worktree | Only the implementer checks out work branches; others detach at SHAs |
| Worktree commits write into the main repo's `.git` (objects, `worktrees/<name>`); sandboxed harnesses (Codex `workspace-write`) may block that — the #11 shape, now in every run | `robomate certify` includes a commit-and-push test from the worktree; harness notes record the grant needed (e.g. `--add-dir <repo>/.git`) |
| Shared refs and config: one agent's `git config`, hooks, `gc` affect all | Guides forbid changing git config; per-worktree config via `extensions.worktreeConfig` only if needed |
| Local branch-name collisions | `robomate/<name>/…` namespace |
| `gh pr merge --delete-branch` tries to delete the local branch, which may be checked out in the implementer's worktree | Verify in M2; default: orchestrator deletes the remote branch only and `robomate clean` prunes local branches after WRAP-UP |
| Submodules and LFS need per-worktree init | Setup command; certification on a repo that uses them before claiming support |
| Cross-OS paths (WSL ↔ Windows) | Clone fallback |

Verdict: worktrees are the right default for same-host agents; the main real risk is sandbox
permissions, which certification catches per harness.

---

## 7. Work intake and the workflow lifecycle

### 7.1 Statement of work
- `robomate submit <file>` / `robomate submit --issue N` / `robomate submit -` (stdin). Stored verbatim with its
  SHA-256 (#9), size-capped with a clear error (#10). `--issue N` renders the standard
  one-issue statement.
- `start_workflow(statement?, policy?)` (orchestrator tool) starts the next queued statement;
  with `statement` it creates one from chat. The hub stores exactly what it received.
- Assignments carry `statement_sha256`; workers read it with `get_statement(sha)` when they
  need it (#8, #9). Nothing paraphrases the operator's text on its way to a worker.

### 7.2 Workflow states and sequencing
- Statuses: `queued → active → (paused | escalated) → done | abandoned`. One `active` at a
  time. `abandoned` is set only by the operator (`robomate abandon [--reason]`): the explicit exit
  for a run that should end without delivering. It bypasses the `done` check, keeps
  worktrees, and is recorded in the run report.
- **One run = one PR/MR** (locked). Sequential runs come from the queue.
- **Carried context between runs** is explicit and small: the previous run's summary
  (PR/MR, merged SHA, follow-ups filed) is available in `get_state`; nothing else carries over.
- Policy additions (to PoC `WorkflowPolicy`):
  - `deliver: merge | pr` (default `merge`) — #14.
  - `closeout: true | false` (default `true`) — run the close-out task (§7.3).
  - `on_done: release | standby` (default `release`). Standby keeps workers joined for the
    next queued run; idle holds cost tokens, so `release` is the default when the queue is empty.
  - `forge` — detected, not normally set.
- Operator policy defaults live in `config.toml`; `robomate submit --policy k=v` overrides per run.

### 7.3 Close-out (#14, #15, #16)
Automates the operator's PoC routine — skim the agents' close-out reports, ask the implementer
about anything non-trivial, have it file issues or add to a nits list, confirm the merge —
using mechanisms that already exist (a task role like `rebase`, a typed result with a
validator, the merge gate). The one judgment call, issue vs. nit vs. drop, stays with the
implementer, as it did in the PoC; the difference is that it is always asked for and always
recorded.

**Repo settings** (`config.toml`, all optional):

| Key | Meaning |
|---|---|
| `nits_issue` | a standing issue where trivial items are added as a comment; without it, `nit` is not an allowed action |
| `followup_label` | label applied to every follow-up issue (default `robomate-followup`), so the operator triages them in the forge |
| `closeout_steps` | list of steps passed verbatim to every close-out task, e.g. `"Update the roadmap issue with current status"` (#16: nothing about roadmaps is hard-coded) |

**The close-out task.** After MERGE (or after approval, with `deliver: pr`), the orchestrator
assigns the run's implementer a `closeout` task (another implementer if that one is lost).
The hub appends to the orchestrator's instructions, and stores on the task:
- **pending findings** — every non-blocking finding from this workflow's reviewer results
  whose ID no later implementer result lists in `resolved_finding_ids`. This is a set
  difference over typed fields (finding IDs are `r<round>-<n>`); disputed findings stay pending.
- **steps** — `closeout_steps`, verbatim and indexed.
- `nits_issue`, `followup_label`, and the PR/MR URL.

The orchestrator may add items of its own as free text. Out-of-scope observations by the
reviewer arrive as non-blocking findings (reviewer guide), so they flow through automatically.

**`CloseoutResult`** (new typed body; additive, so wire `hub.schema_version` stays 1):

```
outcome: completed | blocked | failed
items:   [{source: "<finding id>" | "implementer" | "orchestrator",
           summary, action: issue | nit | none, url?, reason?}]
steps:   [{index, done, url?, note?}]
summary
```

Validation (a `completed` result that fails any rule is rejected with a clear error and the
task stays open):
- every pending finding ID appears in `items` exactly once;
- `issue` and `nit` require `url`; `nit` requires `nits_issue` to be configured;
  `none` requires `reason`;
- every step index appears exactly once; `done: false` requires `note`.

Follow-up issues carry `followup_label` and reference the PR/MR and the finding ID.

**The `done` check.** `set_workflow_status(done)` is refused, naming what is missing, unless:
1. `check_merge_gate` reports the PR/MR `merged` (`deliver: merge`) or `open` (`deliver: pr`); and
2. a `closeout` task for this workflow is `completed` (unless `closeout: false`).

No other close-out rule is enforced in code for the MVP. Steps reported `done: false` do not
block; they are highlighted in the report.

**The run report.** At `done` or `abandoned` the hub renders
`.robomate/runs/<id>/report.md` from typed data only: statement of work (link and SHA),
agents and harnesses, rounds, approved and merged SHAs, each finding with its disposition and
link, close-out steps, and every `none` with its reason. `robomate report [run]` prints it;
`robomate status` shows its headline. The operator's remaining touch is reading the report when they
choose and triaging `followup_label` issues in the forge.

### 7.4 Asking the operator
`ask_user(question, options?)` queues a question; `robomate inbox` lists it, `robomate answer` answers it,
and the orchestrator receives a `user_answered` event. It is for escalations (the PoC rails),
not routine close-out. An orchestrator in an interactive session may still simply ask in chat;
the inbox makes a headless orchestrator possible and is the channel a future dashboard uses.

### 7.5 Resume (#18, reduced; #19)
- **Hub restart:** `robomate up` reuses port and state. Bridges reconnect with backoff under the same
  instance ID; an interrupted hold is re-issued. Agents see at most a retried call.
- **Agent restart:** join with `takeover` → task reattached (§5).
- **Orchestrator restart:** join as orchestrator → re-briefing in the join response; the guide's
  "On resume" procedure (PoC skill) reconciles with the forge before acting.
- **Host standby (#19):** the sweeper detects a wall-clock jump against the monotonic clock and
  grants a grace window before declaring anyone lost.
- **Tests:** the existing crash-point harness at component level, plus one live kill/restart of
  each component in the MVP acceptance runs.

---

## 8. Tool surface (served by `robomate mcp`)

One static tool list for every session; the hub enforces role. Tool schemas are counted in
the token budget (§11); list-changed per-role surfaces are an open decision (§15).

| Tool | Who | Change from PoC |
|---|---|---|
| `join`, `whoami` | any | **new** |
| `await_assignment`, `get_role_guide`, `report_progress` | worker | unchanged semantics; hold capped by the harness profile |
| `submit_result` | worker | accepts `CloseoutResult` for `closeout` tasks (§7.3) |
| `ask_orchestrator` | worker | renamed from `ask_alice` |
| `get_statement(sha)` | worker, orchestrator | **new** (#9) |
| `start_workflow` | orchestrator | **new**; replaces `initialize_workflow` |
| `get_state` | orchestrator | adds run summary, queue, close-out status, listening state; compact by default |
| `wait_for_event` | orchestrator | new event kinds `work_submitted`, `user_answered` |
| `assign_task` | orchestrator | adds `statement_sha256` and role `closeout` (hub appends pending findings and steps); refuses incompatible standing role |
| `reply`, `set_task_state`, `release_agent`, `log_decision` | orchestrator | unchanged |
| `set_workflow_status` | orchestrator | `done` refused unless merged/open per `deliver` and close-out completed |
| `check_merge_gate` | orchestrator | forge-dispatched (§10); same report shape |
| `ask_user` | orchestrator | **new** |

The orchestrator operations move from in-process MCP to an authenticated HTTP JSON-RPC route
on the hub; the worker A2A route is unchanged. `robomate status/submit/inbox/answer` use the same
route with an operator identity; `robomate abandon` and `robomate report` are operator-only.
The first operator methods on this route are `hub.info` and `hub.shutdown` (M1 Step 2),
followed by `hub.heartbeat` for orchestrator session liveness (M1 Step 4).
From M1 Step 3 it also serves the orchestrator operations, named after today's tools, with
the tool arguments as `params` (an object) and the tool's dict as `result`. Errors carry a
stable `code` and the original message: -32602 invalid params (including argument
validation), -32001 not found, -32002 conflict, -32003 payload too large, -32004 merge gate
unavailable, -32603 anything else.

---

## 9. Harness support

### 9.1 Install
`robomate install <harness> [--scope user|project]` writes one static MCP server entry —
`robomate mcp --harness <harness>` — into that harness's config at the chosen scope. **No token, no
identity, no paths**, so a project-scope config can be committed safely; a committed one is
also present in every worktree automatically. `--scope user` is the recommended default.
Optional `--with-rules` adds a one-line rule/instructions file for harnesses that don't act on
tool descriptions alone. `robomate install --list` shows harnesses, tiers, and scopes.

### 9.2 Harness profiles
Kept in robomate (versioned with it): `max_hold_s` (under the harness's tool-call limit — 100 s for
Claude Code, which backgrounds calls at 120 s; ~280 s for Codex, whose observed limit is 300 s),
config locations per scope, known grants (sandbox, auto-approve), whether a continuation
supervisor exists, and notes. The hub clamps every hold to the joined agent's profile.

### 9.3 Certification — `robomate certify <harness>`
Generalizes the Step 4B endurance gate (robo-agents #30; #13). `robomate certify` starts a throwaway hub on a
scratch repo and prints a join line; the operator starts the harness and pastes it; `robomate`
then drives a scripted orchestrator:
- ≥ 3 assignment cycles over ≥ 30 min with no operator message;
- hold timeouts and retries; an `ask_orchestrator` timeout and reply;
- a task longer than `HUB_LOST_AFTER_S` with liveness intact;
- commit and push from the worktree (catches sandbox grants);
- `gh`/`glab` reachable and authenticated;
- a clean release.

Report: `~/.local/state/robomate/certify/<harness>-<version>-<date>.md` with pass/fail per
check, attempts, tool errors, and interventions
([`docs/poc-lessons.md`](poc-lessons.md): record every attempt, classify failures).
Results are summarized in `docs/harnesses.md`.

### 9.4 Tiers

| Tier | Harnesses | Gate |
|---|---|---|
| MVP | Claude Code, Codex, OpenCode, AntiGravity | certified |
| MVP if certified | Cline | IDE turn-ending is the risk; "not listening" + nudge is the fallback |
| Post-MVP | Devin, RooMote | cloud-hosted: need an internet-reachable hub, per-agent auth, TLS |

Continuation: CLI harnesses that end turns get a thin, policy-free supervisor where one exists
(PoC `supervise-claude-code.sh`); others rely on "not listening" detection and a nudge.

---

## 10. Forge support — GitHub and GitLab

- **Detection** at `robomate up`: `github.com` → GitHub; a host in `config.toml: forge.gitlab_hosts`
  or known to `glab` → GitLab; otherwise `--forge`. Recorded in `hub.json` and the workflow policy.
- **Agents use the CLIs** (`gh`, `glab`) with their own authentication, as in the PoC.
- **Merge gate** (`check_merge_gate`, read-only facts): a `ForgeGate` interface with
  `GitHubGate` (the PoC implementation) and `GitLabGate`, which reads through `glab api` so the
  hub needs no forge token of its own. It maps GitLab pipelines, `detailed_merge_status`, and
  diverged-commit counts to the same report shape, following
  [the GitLab feasibility study](feasibility-and-impact-of-supporting-gitlab-centric-workflows.md) §4.
- **Merge:** the orchestrator merges with the forge CLI, bound to the approved SHA
  (`gh pr merge --match-head-commit …`; `glab mr merge --sha …` per the study §5).
- **Unsupported on GitLab (preflight refuses or warns):** merge trains, auto-merge, server-side
  automatic rebase — each would break the client-verified, SHA-bound merge.
- **Guides:** one guide per role, forge-neutral ("open a change request with your forge CLI"),
  composed at serve time with a short forge appendix (`guides/forge/github.md`,
  `guides/forge/gitlab.md`) holding only the invariant-critical commands. No N×M guide copies.
- **Self-hosted:** base URL from origin; custom CA via the environment the CLIs already honor;
  the shared-account approval rule (PoC #37) applies unchanged.
- **Acceptance:** the operator's self-hosted GitLab with a CI runner, plus the GitHub sandbox.

---

## 11. Observability and token budget

- `robomate status`: one screen — repo, URL, forge, agents (role, harness, model, alive/listening),
  workflow phase and round, PR/MR and last gate result, close-out status, inbox count.
- `robomate log [--timeline] [--follow]`: interleaved events, decisions, and results using closed-set
  labels, never printing untrusted payload text (#20). This is the drill-down and the future
  dashboard's feed.
- `robomate report [run]`: the rendered run report (§7.3).
- Summaries come from typed results and events, not from agent prose.
- **Run identity:** every workflow gets a run ID and a manifest in `.robomate/runs/<id>/`
  (robomate version, git revision, harness profiles, policy, statement SHA)
  ([`docs/poc-lessons.md`](poc-lessons.md)).
- **Accounting on by default** (#7); `call_log` retention bounded (#23), since hubs now live
  for days.
- **Token levers (#22):** longer holds where the harness allows; minimal timeout payloads;
  compact `get_state`; guides returned as "unchanged (hash)" when the bridge has already
  delivered them this session (with `force` to re-fetch); smaller tool schemas;
  `on_done: release`. Baseline first, then a target.

---

## 12. Security (MVP threat model)

Single operator. Loopback bind by default. One bearer token per hub in `.robomate/token`,
read by `robomate mcp` through discovery, never written into harness configs. All token holders are
trusted not to impersonate each other; identity and model metadata are self-declared (PoC §1).
Agents share the operator's forge identity; the hub's typed reviewer verdict remains the
approval record (PoC #37). Remote-agent mode requires a trusted network path.

---

## 13. Implementation plan

Each milestone leaves robomate usable for real work, and from M1 on robomate is built with
itself.

| # | Milestone | Deliverable | Done when |
|---|---|---|---|
| M0 | Seed robomate | `RoboNater/robomate` created with the PoC files and this spec; `poc-spec.md` frozen; PoC acceptance scripts archived (Appendix B); robo-agents #57 exported to `docs/poc-lessons.md`; Python distribution renamed `robomate`; roadmap issue, a minor-nits issue, and one issue per carried item (Appendix A) opened; robo-agents archived with a pointer to robomate | CI green in robomate; the roadmap lists M1–M6 with the carried issues attached; robo-agents is read-only |
| M1 | Standalone hub + `robomate` CLI | `robomate up/down/status/ls`, `.robomate/` layout, registry, port reuse; orchestrator JSON-RPC route; `robomate mcp` bridge for all roles (existing tool names) with heartbeat and discovery; accounting on (#7); fix flaky tests (#6) | A real run completes with the hub from `robomate up`, and the orchestrator's session is restarted mid-run without disturbing workers |
| M2 | Join, worktrees, work intake | `join`/`whoami`, standing roles, takeover + task reattach; worktree manager (§6); `robomate submit`, queue, `get_statement` (#8, #9); `guides/orchestrator.md` served; `robomate workspace`, `robomate clean` | On Claude Code + Codex: `robomate up`, three one-line joins, `robomate submit`, merged PR — with no `prepare-run.py`, no generated configs, no pasted launch prompts |
| M3 | Harness breadth | `robomate install` (both scopes), harness profiles, `robomate certify`, `docs/harnesses.md`; supervisor/nudge path | OpenCode and AntiGravity certified; Cline attempted and recorded; one real run with a non-Claude orchestrator |
| M4 | Close-out and resilience | `closeout` task role, `CloseoutResult` + validator, `done` check, run report + `robomate report`, `robomate abandon`, repo settings (`nits_issue`, `followup_label`, `closeout_steps`); `ask_user` inbox with `robomate inbox/answer`; resume (§7.5, #18 reduced, #19); `robomate log --timeline` (#20) | Scripted tests: `done` refused with an unmerged PR and with no completed close-out; a `CloseoutResult` missing a pending finding, a URL, a `none` reason, or a step is rejected; the report lists every finding with its disposition; kill/restart of the hub, a worker, and the orchestrator each resume without duplicate actions; simulated standby loses no one. One real run on robomate itself ends with its follow-ups filed and its roadmap issue updated, with no operator chat |
| M5 | GitLab | Forge detection, `GitLabGate` via `glab api`, forge appendix guides, preflight for unsupported settings | A multi-round run with a changes-requested round and a stale-base rebase merges on the self-hosted GitLab |
| M6 | Token budget + MVP acceptance | Baseline from accounting; #22 levers; retention (#23) | Two acceptance runs (GitHub, GitLab), mixed certified harnesses, each with a changes-requested round, one component restart, and two queued statements; per-run bytes by role reported against the baseline; setup from `robomate up` to first assignment timed |

**Parallelism:** M5's gate adapter and forge detection can start after M1 and run alongside
M2–M3. M4 depends on M2. M3's certification needs M2's join and worktrees.

---

## 14. Decisions (locked)

| Decision | Choice |
|---|---|
| Unit of work | One run = one PR/MR; sequential runs via queue; carried context = previous run summary only |
| Hub scope | One hub per repo, own port; registry + `robomate ls`/`robomate status` to tell them apart |
| Forge operations | Agents run `gh`/`glab` directly; hub gate reads facts only; orchestrator merges bound to the approved SHA |
| Workspaces | Worktree per agent (orchestrator included), outside the repo; clone fallback for other host/OS |
| Harness/model/role | Any certified harness, any model and effort, any role; tiers from `robomate certify` |
| Forges | GitHub and GitLab (incl. self-hosted) |
| Instruction delivery | Tool descriptions, join response, served guides; no harness-specific skill required |
| Close-out | Hub-built `closeout` task with a validated typed result; `done` requires merge state per `deliver` and a completed close-out; run report replaces reading agent chat; `robomate abandon` is the operator's no-delivery exit. No general obligation ledger in the MVP |
| Orchestrator mode | Interactive or headless; both supported via the inbox |
| Users | Single operator; per-hub shared token; loopback default |
| Name | `robomate` for repository, distribution, CLI, MCP server, and skill; no shipped short alias |

---

## 15. Open decisions

| Decision | Options | Settle by |
|---|---|---|
| Tool surface | One static list (simple) vs per-role list via `tools/list_changed` (fewer schema tokens; harness support varies) | M3, from measured schema bytes |
| Worktree root default | `<parent>/<repo>.robomate/` vs user state dir | M2 |
| `gh`/`glab` merge vs local branch in a worktree | Remote-only branch delete + `robomate clean`, or merge from outside any checkout | M2 |
| GitLab gate transport | `glab api` (recommended) vs direct REST with a hub-held token | M5 |
| Certification bar | cycles / duration / pass rate over N attempts | M3 |
| Default `reviewer_harness_differs` | keep `true` or relax to `false` for flexibility | M2 |

---

## 16. Later (post-MVP)

- **Forge pass-through tool:** the hub provides an authenticated `gh`/`glab` wrapper that
  passes all arguments through and returns full output — central auth and audit without
  giving up agent-driven discovery.
- **One local router:** a single `robomate` service on one port hosting several repo hubs by path or
  ID; the MVP registry already provides discovery.
- **Dashboard:** runs, agents, inbox, and timeline; then epics/stories backed by forge issues,
  milestones, and labels (not a separate ticket store), with assignment to available agents.
- **Lead / PM agent** (robo-agents #44): sequencing, reservations, collision detection, status
  and recommendations.
- Multiple users; per-agent credentials; TLS; cloud harnesses (Devin, RooMote).
- Concurrent workflows per hub; multiple reviewers per run; non-PR deliverables
  (robo-agents #102).
- A general obligation ledger (robo-agents #100): owed work beyond findings and configured
  steps, with operator-approved waivers — if the close-out task proves insufficient.
- Multi-machine hardening (robo-agents #127); log-analysis tooling (robo-agents #115);
  call-accounting reconciliation (robo-agents #96); background/service mode.

---

## Appendix A — robo-agents open issues: what carries into robomate

Issues are not migrated wholesale. "Carry" means open a new robomate issue under the
milestone shown, linking the robo-agents original for history.

| robo-agents issue | In robomate |
|---|---|
| #2 Roadmap | Replaced by a new roadmap issue (M0) |
| #57 Lessons learned | Exported to `docs/poc-lessons.md` (M0); cited throughout this spec |
| #110 Minor nits | Replaced by a new minor-nits issue, configured as robomate's `nits_issue` |
| #148 Flaky async tests | Carry → M1 |
| #119 Accounting on by default | Carry → M1 |
| #113 Short kickoff prompts | Carry → M2 (core) |
| #143 Statement of work verbatim | Carry → M2 (core) |
| #106 Work-file size cap | Folded into M2 as the `robomate submit` cap |
| #84 Codex hides tools with extra `--add-dir` | Carry → M3 re-test; worktrees likely make the main `.git` grant necessary |
| #112 Wait defaults ≤ 100 s | Carry, reshaped → M3: holds per harness profile, each tested against its limit |
| #61 Endurance leftover rows | Folded into `robomate certify` (M3): fail fast on leftover state |
| #114 `done` with PR unmerged | Carry → M4 (`done` check) |
| #91 Close-out drops unaddressed findings | Carry → M4 (close-out task) |
| #117 Roadmap obligations | Carry → M4 (`closeout_steps`) |
| #107 One PR per run / work label | Carry → decide in M4; the `done` check already binds a run to one PR/MR |
| #31 Resume reconciliation | Carry → M4, reduced per §7.5 |
| #131 Standby/resume | Carry → M4 |
| #121 Timeline | Carry → M4 as `robomate log --timeline` |
| #124 GitLab plan | Carry → M5, updated to this spec (`glab api` gate, forge appendix guides); the feasibility study comes with the seed files |
| #92 Reduce MCP context cost | Carry → M6 |
| #94 `call_log` retention | Carry → M6 |
| #100 Owed work beyond findings | Partly covered by M4; the general ledger is in §16 |
| #103, #105, #108, #135 | Not carried: superseded by `robomate up`, join, `robomate submit`, and the clone fallback (M2) |
| #82 Skill namespacing / project install | Not carried: superseded by `robomate install` (M3) |
| #99 Real measurement run | Not carried: M1–M6 dogfooding with accounting on replaces it |
| #87, #88, #89, #90 Step 6 verifier items | Not carried: PoC archive |
| #17 `CancellableStdout` parse | Not carried: likely moot once the hub no longer serves stdio |
| #44, #96, #102, #115, #127 | Not carried now; listed in §16, re-filed when picked up |
| #97 `X-Hub-Agent` self-claim | Not carried: bridge sessions are bound to a join; re-file if the gap remains after M2 |

## Appendix B — PoC artifacts

| Artifact | Fate |
|---|---|
| `docs/poc-spec.md`, PoC evidence, plans for Steps 6/7 | Frozen; historical record |
| `scripts/step5*`, `step6*`, `step7*`, `launch-step*`, `verify-step*`, `run-step6-disturbances.py`, their tests | Move to `scripts/poc/`; not maintained; tests moved out of the default CI run |
| `scripts/prepare-run.py`, `bootstrap-workspace.*` | Kept until M2 covers their topologies (incl. remote workers), then removed |
| `scripts/mock-alice.py`, `mock-worker.py` | Kept; become the engine of `robomate certify` and the scripted E2E tests |
| `scripts/supervise-claude-code.sh` | Kept; referenced from the Claude Code harness profile |
| `scripts/hub-report.py` | Folded into `robomate log` / `robomate status` |
| `skills/alice-orchestrator` | Content moves to `guides/orchestrator.md` (M2); an optional thin `robomate` skill only points the agent at `join` |
| `skills/alice-relay`, `docs/notes/relay-trial-*` | Archived |
| `skills/worker`, `prompts/*.md` | Replaced by join responses (M2) |
| `docs/user-guide.md` | Rewritten around `robomate` (M2, M3) |
| `packages/worker_mcp` | Becomes `robomate mcp` |
