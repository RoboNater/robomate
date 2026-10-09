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
1. **Ease of use.** `robomate up` in a checkout, agents join by name and role, `robomate submit`
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
5. **The operator's checkout is never touched.** That is the checkout that owns the hub
   (§4): the main checkout or a linked worktree. Every agent, including the orchestrator,
   works in its own generated worktree (§6).
6. **Evidence belongs to its run** ([`docs/poc-lessons.md`](poc-lessons.md)). Runs have
   identity and a manifest by default.
7. **Nothing enters an agent's context that it doesn't need.** Payload size is a design
   constraint, not a later optimization.

---

## 2. The MVP experience

```sh
uv tool install git+https://github.com/RoboNater/robomate    # once per machine
robomate install claude-code --scope user                   # once per harness
cd ~/src/my-repo && robomate up                             # starts this checkout's hub
# In each harness session, one line:
#   "Join robomate as alice, orchestrator."
#   "Join robomate as bob, implementer."
#   "Join robomate as charlie, reviewer."
robomate submit sow.md     # or: robomate submit --issue 42 ; further submits queue up
robomate status            # hub name, checkout, repo, URL, agents, phase, PR/MR, gate, open questions
robomate report            # last run: merged SHA, each finding and where it went, steps
robomate inbox             # questions for you; answer with: robomate answer 3 "yes, file it"
robomate log --timeline    # drill down when something looks wrong
```

- `robomate up` runs in a checkout: the main checkout or a linked worktree. The hub belongs
  to that checkout (§4). A second team working in the same repository at the same time
  starts its own hub from another worktree, e.g.
  `git worktree add ../my-repo-feature && cd ../my-repo-feature && robomate up`. The two
  hubs have separate state, tokens, ports, agents, and queues; they share what every
  worktree of a repository shares: git objects, refs, ordinary config, hooks, and the
  remote (§6).
- `robomate up` detects the checkout, origin, forge, and default branch; runs preflight
  before serving. `--forge github|gitlab` overrides detection and is recorded in `hub.json`.
  An unknown forge warns that `check_merge_gate` uses the GitHub gate. GitLab startup checks
  are listed in §10; CI presence and merge-policy compatibility are checked where the policy
  is known (the run preparation path, then `submit` in M2). Prints the hub name, its state
  location, the hub URL, and join lines.
- For CLI harnesses, `robomate workspace bob` (or `robomate up --agents bob,charlie`) creates the agent's
  worktree and prints the path to start the harness in. IDE harnesses open that folder.
- The statement of work can also be pasted to the orchestrator in chat; `robomate submit` is
  preferred because it is stored byte-for-byte (§7.1).

---

## 3. Architecture

```
 operator terminal                  per-checkout hub (robomate up)
 ┌───────────────┐  HTTP (local)  ┌───────────────────────────────────────────┐
 │ robomate      │───────────────▶│ hub daemon: HTTP A2A (workers)            │
 │ status/submit │                │           + JSON-RPC (orchestrator, CLI)  │
 │ inbox/report  │                │ SQLite state: <worktree_root>/<hub>/state │
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
| One workflow per hub state dir, fresh state per run | Persistent per-checkout hub; sequential workflows with history and a queue, per hub |
| Close-out rules in skill prose; operator reads agents' close-out reports and asks follow-ups in chat | Hub-built close-out task with a typed result, a rendered run report, and a `done` check on merge state (§7.3) |

The worker A2A protocol (§4.1 of the PoC spec) and the typed result schemas (§4.4) are
unchanged. Wire `hub.schema_version` stays 1 unless a typed body changes shape.

---

## 4. Local layout, discovery, multiple hubs

**Ownership.** A hub belongs to the **checkout** where `robomate up` runs: the main
checkout or a linked worktree, as `git rev-parse --show-toplevel` reports it from any
directory inside. Hubs are therefore scoped per checkout, not per repository (§14), and
one repository can have several hubs at once, one per checkout that ran `up`. The
repository is still the unit for git: every hub records the absolute
`git rev-parse --git-common-dir`, and git operations (worktrees, fetch, refs) go through
that shared common directory. A repository whose common directory is a **bare
repository** with linked worktrees is supported: `up` runs in one of its linked
worktrees. `up` in the bare directory itself, which has no working tree, is refused.

**Repository names.** Paths derived from the repository use `<parent>` and `<repo>`,
computed from the git common directory *D*: if *D*'s name starts with `.` (`.git`, or a
`.bare` beside the worktrees), use the directory that contains it instead. `<repo>` is
that directory's name with any trailing `.git` removed, and `<parent>` is its parent.
So `~/src/my-repo/.git`, `~/src/my-repo.git` (bare), and `~/src/my-repo/.bare` (bare)
all give `<parent>` = `~/src` and `<repo>` = `my-repo`.

**Hub name.** Each hub has a short **name**, unique among the hubs of its repository. It
appears in paths (§6), branch names, listings, and hub selection; `hub_id` stays the
identity key and never changes.
- **Derived** at the first `up` from the owning checkout's directory name: lowercased,
  each run of characters outside `a-z`, `0-9`, `-`, `_` replaced by `-`, cut to 40
  characters, then leading and trailing `-` and `_` dropped. A derived name that still
  fails validation is refused with a request for `--name`.
- **Validated**, derived or given, against `^[a-z0-9]([a-z0-9_-]{0,38}[a-z0-9])?$`, which
  keeps it safe as one path component on Windows and Linux and as one component of a git
  ref (no `.`, `/`, `..`, `@{`, or `.lock`). The Windows device names (`con`, `nul`, `aux`,
  `prn`, `com1`–`com9`, `lpt1`–`lpt9`) and the names `state` and `token`, which are taken in
  the hub's namespace (§6), are refused.
- **Overridden** with `robomate up --name <name>` at the first `up`. The name is then
  recorded in `hub.json` and fixed: a later `--name` that differs is refused, because
  agent worktrees and branches carry it.
- **Unique:** a name already used by another hub of the same git common directory, in the
  registry or (from M2) as a directory under the worktree root, is refused with a request
  for `--name`. `up` never picks a different name silently.

**Hub state** — one directory per hub: `hub.json`, `hub.db`, `up.lock`, `config.toml`,
`runs/`, and the token (§12):

| Path | Contents |
|---|---|
| `hub.json` | `hub_id`, `name`, `checkout` (owning checkout), `git_common_dir`, `origin`, `forge`, `default_branch`, `url`, `port`, `pid`, `started_at`, `robomate_version` |
| `hub.db` | SQLite state (schema continues from PoC v12; reserve versions per the roadmap rule) |
| `token` | per-hub bearer token, mode 0600; in its own directory from M2 (§12) |
| `up.lock` | OS-held per-hub lock while the hub starts or runs; prevents a concurrent `up` of the same hub, never of a sibling hub |
| `config.toml` | optional hub settings, the first configuration layer (below) |
| `runs/<run-id>/` | run manifest and exported evidence (§11) |

**State location.** Durable hub state lives **beside the hub's agent worktrees**, in
`<worktree_root>/<hub>/state/`, not in the checkout. `worktree_root` defaults to
`<parent>/<repo>.robomate/` and is a repository-level setting (below). The reason: hub state
in an ignored directory of the checkout, as `.robomate/` is, does not survive the checkout.
Ignored files don't make a worktree dirty, so `git worktree remove` on a linked worktree
deletes its `.robomate/` silently, and with it the history and run reports. A worktree root
outside every checkout also keeps a hub's state reachable when its owning worktree is gone,
which `robomate clean` needs to report orphans (§6).

Until M2 moves it (§13), state stays in the owning checkout's `.robomate/`, added to
`info/exclude` in the git common directory (never to a tracked `.gitignore`). Every
consumer, the CLI, the bridge, and the operator scripts, finds it through **one resolver**
from a hub to its state location, so the move is a change in one place. In that interval
`up` in a linked worktree warns that `git worktree remove` deletes the hub's state.

**Binding** (from M2). Once state can live outside the checkout, a checkout is bound to
its hub, relocated or legacy (Migration below), by an untracked record,
`robomate-binding.json`, in that checkout's own git directory (`git rev-parse --git-dir`:
`.git` in a main checkout, `<common>/worktrees/<id>` in a linked worktree). It is always
found through git, never by constructing a `.git/worktrees/<name>` path. It names the hub
(`hub_id`, name, state location, and the path of its token file) and the checkout's role
in it: `owner`, or `agent` with the agent's name. It holds **no token**, only where to
find it (§12). `up` writes the owner binding; the worktree manager writes agent bindings
(§6). `up` refuses in a checkout bound to a hub as an agent, and names that hub. A binding
lives and dies with its worktree: `git worktree remove` deletes it together with the
worktree's git directory.

**Migration.** A hub created before checkout scope has its state in the main checkout's
`.robomate/` (until now, `up` in a linked worktree also used the main checkout's state).
It keeps that state, its `hub_id`, port, and token, and keeps working; nothing is copied or
moved silently.
- **Name:** derived from the main checkout's directory name at the first `up` under this
  version, or given with `--name` then; recorded in `hub.json` like any other.
- **Binding (M2):** `up` in a checkout that holds legacy state runs the hub from it in
  place and writes the owner binding with `.robomate/` as the state location and
  `.robomate/token` as the token file; agent bindings record the same. The main checkout
  cannot be removed with `git worktree remove`, so state there is not exposed to the
  deletion above.
- **Relocation** to `<worktree_root>/<hub>/state/`, with the token to
  `<worktree_root>/<hub>/token/` (§12), is a separate, explicit operator command. It moves
  state and token together and rewrites the hub's bindings; until it runs, `.robomate/`
  and `.robomate/token` stay authoritative.

**Configuration layers.** Settings are read from three files, first match wins:
1. the hub's own `config.toml`, in its state directory;
2. a repository-level file shared by every checkout of the repository,
   `<git-common-dir>/robomate/config.toml` (untracked, like everything in the common
   directory);
3. the user's file, `$XDG_CONFIG_HOME/robomate/config.toml` (Windows:
   `%APPDATA%\robomate\config.toml`).

| Keys | Layers | Why |
|---|---|---|
| `worktree_root`, `forge.*` (e.g. `forge.gitlab_hosts`) | repository, user; a hub cannot override them | The worktree root holds the hub's own state, so the hub's config cannot choose it; the forge is a fact of the repository |
| `nits_issue`, `followup_label`, `closeout_steps` (§7.3), `setup` (§6) | repository; a hub may override | They describe the repository's process, so a new worktree's hub has them from its first start, with nothing copied. A hub may need a different setup or nits issue for its line of work |
| policy defaults (§7.2), agent defaults (model, effort pins) | hub, repository, user | Operator preferences; per-hub where a team is set up differently |

A user-level `worktree_root` is a parent directory; the repository's root is then
`<that>/<repo>.robomate/`. That alone does not keep two repositories apart: two
independent repositories with the same `<repo>` name derive the same root. So each
repository's worktree root records, when its first hub creates it, the git common
directory it belongs to, and a hub of any other repository refuses that root and asks for
a repository-level `worktree_root`. The same check applies to every root, default or
configured.

**Machine registry** — `$XDG_STATE_HOME/robomate/hubs.json` (Windows:
`%LOCALAPPDATA%\robomate\hubs.json`): one entry per hub, **keyed by `hub_id`**, recording
the hub's name, owning checkout, git common directory, state location, URL, and pid.
Registering one hub never evicts another, including a sibling hub of the same repository.
Liveness is checked by pid and `/healthz` (matching `hub_id`); an entry whose state
location is gone is pruned. `robomate ls` lists the hubs with their name, owning checkout,
repository, URL, live or stopped, agents joined, and phase. The operator credential,
`operator-token` (mode 0600), lives in the same directory and never in a repository or a
hub's state (#128). A stopped hub keeps its entry, and so its name and port, until the
operator releases them with `robomate forget` (#159): forgetting removes the registry
entry, freeing the name and the saved port for reuse, and deletes nothing. hub.db and
run reports are kept; hub.json keeps its identity and last address, records the
released port, and clears pid/started_at. `robomate forget --all` forgets every stopped
hub; a live hub is never forgotten (stop it first). If a sibling hub has since taken the
freed name, the forgotten hub's next `up` is refused naming the holder; an explicit
`robomate up --name <new>` renames it for that start. A rename starts a new
worktree/branch namespace: agent worktrees and `robomate/<old>/…` branches made under
the old name keep it. Removing hub state stays a separate, explicit operator action.

**Port** — per hub: the first free port from 8420 upward on the hub's first `up`, saved in
`hub.json` and reused on restart so remote agents keep working; `--port` pins it. A pinned
or saved port that is taken fails startup instead of moving the hub, so two hubs that ask
for the same port fail clearly. Allocation binds first; there is no repository-wide lock.
A hub forgotten with `robomate forget` has its port released: its next `up` ignores the
saved port, takes the first free port, and says that the port was released and names the
new one, since remote agents configured with the old port need the new one.

**Process** — foreground by default (visible log, Ctrl-C stops it, same on Windows and
Linux). `robomate down` stops it from another terminal. `robomate down --all` stops every
live hub in the machine registry with the operator credential and reports what it stopped;
hub state and run reports are kept. Background/service mode is post-MVP.

**Hub discovery** — the same order for `robomate mcp` and for every CLI command that acts
on a hub. **There is no silent attach**: a checkout never reaches a hub it was not given.
1. `ROBOMATE_HUB_URL` with `ROBOMATE_TOKEN` or `ROBOMATE_TOKEN_FILE` — explicit, for remote
   agents and the clone fallback. For the CLI, an explicit selector,
   `--hub <name | hub_id | checkout path>`, at the same rank; a name that matches hubs of
   several repositories fails with the choices.
2. From the working directory, through git: this checkout's binding (from M2), or the hub
   this checkout owns. Works from any directory inside the checkout.
3. Otherwise an error that lists the live hubs (name, owning checkout, repository, URL) and
   says how to choose one: `--hub` for the CLI, `ROBOMATE_HUB_URL` for a bridge, `hub=` at
   `join` (§5).

The PoC-era rule "if exactly one hub is registered on the machine, that hub" is removed,
for agents and for every CLI command. Under checkout scope it would let a command in one
checkout act on a sibling worktree's hub: in particular, `robomate down` never stops a hub
the operator did not select. A binding to a hub that is missing or stopped is an actionable
error naming that hub and its owning checkout; it never falls through to another hub.
Before a bound or selected local hub is used, its `hub_id` is checked against `/healthz`.

In M1, before `join` exists, `robomate mcp` resolves the hub on its first tool call. An
ambiguous or empty discovery returns the live-hub list as a tool error.

**Identify-the-hub** — `robomate status` and the `whoami` tool both report the hub name,
owning checkout, repository, origin, forge, URL, and the agents joined, so neither the
operator nor an agent can confuse two hubs, even two of one repository. `robomate status`
also shows the hub's state location, which operator tools take
(`scripts/hub-report.py --state-dir`).

**Remote agents** (e.g. hub in WSL, agent on Windows — proven in PoC Step 7): `robomate up --bind
0.0.0.0 --public-url http://<addr>:<port>`; the remote side sets `ROBOMATE_HUB_URL` and a token
file, and uses the clone fallback workspace (§6). Supported, not the primary path.

**Isolated smoke hubs.** A worker that changes robomate's own entry points runs them
against a hub of its own (#145). Such a hub falls outside the operator-only "start or stop
hubs" step (§12) only if it is isolated completely: every location it derives falls inside
one temporary directory, so it can touch nothing of the run's.
- **Registry and operator credential:** `XDG_STATE_HOME` (Windows: `LOCALAPPDATA`) and
  `ROBOMATE_OPERATOR_TOKEN_FILE` point into the temporary directory.
- **Configuration layers and worktree root** (M2): `XDG_CONFIG_HOME` (Windows: `APPDATA`)
  points into it; the repository-level file is in the clone's own common directory; the
  clone sits at `<tmp>/<repo>`, so the default root `<tmp>/<repo>.robomate/` is inside too.
- **Hub selection:** its commands run with `ROBOMATE_HUB_URL`, `ROBOMATE_TOKEN`, and
  `ROBOMATE_TOKEN_FILE` unset. An explicit URL comes first in discovery, so with one set a
  `status` or `down` in the temporary clone would act on that hub instead.
- **Checkout:** a temporary clone, never the agent's worktree, where `up` refuses (Binding
  above).

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
- **Names** are unique per hub, and validated like hub names (§4), since they appear in
  worktree paths and branch names (§6). Joining a name whose instance is alive returns 409 unless
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
- identity confirmed; hub (`hub_id` and name), repo, forge, default branch;
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
- `STALLED` (#144, before M2) — an activity assessment beside `alive`/`lost`: no substantive
  hub call and no hold, no task progress on leased work, or an orchestrator event never
  delivered, for the policy's `stall_after_min`. Shown with its evidence; a worker's episode
  queues one `agent_stalled` event; nothing is failed or restarted. Episodes are kept in
  `stall_episode` (DB schema v17).

---

## 6. Workspaces

**Policy**
- Every joined agent, orchestrator included, gets its own **generated git worktree**. The
  checkout that owns the hub (§4) is never an agent workspace, and robomate never modifies
  it beyond its binding record in its git directory, worktree registration in the git
  common directory, and, until M2 moves hub state, `.robomate/`.
- **Location:** `<worktree_root>/<hub>/<agent>/`, with `worktree_root` defaulting to
  `<parent>/<repo>.robomate/` (§4). The hub's namespace `<worktree_root>/<hub>/` also holds
  its `state/` and `token/` directories (§4, §12), which is why those two names are not
  valid agent names; a legacy hub run in place keeps both in `.robomate/` until it is
  relocated (§4 Migration). Outside every checkout, so IDE indexers, test discovery, and file
  search in the operator's checkout never see agents' copies. `worktree_root` is a
  repository-level setting (§4).
- **Created** at `join` (or earlier by `robomate workspace <name>` / `robomate up --agents …`) with
  `git worktree add --detach <path> origin/<default>`, then bound to the hub as that agent
  (§4). **Reused per `(hub, agent)`** across runs, so environment setup (venv,
  node_modules) is paid once and the path stays stable: harness resume finds a saved
  session by its working directory (Claude Code project directories, `opencode session
  list`; see [`development/agent-recovery.md`](development/agent-recovery.md)).
- **Locked while in use:** `git worktree lock --reason "robomate hub <name>"`, so
  `git worktree remove` and `git worktree prune` refuse an agent's worktree. robomate locks
  only the worktrees it creates; it never locks the operator's checkout, and git cannot
  lock a main checkout at all. Hub state lives outside every checkout for that reason (§4).
- **Branches:** implementers create `robomate/<hub>/<agent>/<label>`; nobody else checks out a work
  branch. The hub names the branch in a run's first implementer assignment and refuses it
  if that ref already exists, locally or on origin, so a label is unique per hub; later
  tasks of the same run reuse the branch. Reviewers and the orchestrator use **detached
  HEAD** at the assigned SHA — git will not check out one branch in two worktrees, and a
  detached review of the exact SHA is what the review invariant wants anyway.
- **New task hygiene:** the guide starts each task from a fresh `origin/<default>` (or the
  assigned SHA); an agent that finds uncommitted changes reports `blocked`, never discards them.
- **Setup:** the `setup` setting (§4, e.g. `setup = "uv sync"`) is returned in the join
  response; the agent runs it in its worktree.
- **Cleanup:** `robomate clean` acts on one selected hub (§4 discovery). It never deletes
  work (PoC rule):
  - unlocks and removes that hub's agent worktrees without `--force`, refusing any with
    uncommitted changes or unpushed commits;
  - prunes that hub's merged local `robomate/<hub>/…` branches;
  - reports **orphaned** agent worktrees, whose owning hub is gone, found through
    `git worktree list --porcelain` and their bindings, and removes them on the same terms;
  - never removes hub state, `runs/` included. That is a separate, explicit operator
    action, never part of agent cleanup.
- **Clone fallback:** an agent whose bridge cannot see the worktree path (another host, or
  Windows vs WSL — a worktree's `.git` file holds an absolute path from one OS) uses its own
  clone, identified as in the PoC (`HUB_WORKSPACE`, `workspace_id`), and connects by explicit
  URL and token (§4). `join` detects this and says so.

**Parallel hubs in one repository.** A worktree isolates working files and the index, not
all git metadata: objects, refs, ordinary config, hooks, and `info/exclude` stay shared.
Parallel hubs are independent otherwise: each has its own state, token, port, agents,
queue, and history, and there is no cross-hub scheduling. They share the remote target
branch, so SHA-bound review, the merge gate, and stale-base rebase still apply as
they do between any two PRs.

**Worktree risks and mitigations**

| Risk | Mitigation |
|---|---|
| A branch can be checked out in only one worktree | Only the implementer checks out work branches; others detach at SHAs |
| Worktree commits write into the shared git common directory (objects, refs, `worktrees/<id>`); sandboxed harnesses (Codex `workspace-write`) may block that — the #11 shape, now in every run | `robomate certify` includes a commit-and-push test from the worktree; harness notes record the grant needed (below) |
| Sandbox grants name the wrong place: `<checkout>/.git` is a file in a linked worktree, and a grant on the hub's namespace exposes its state | Grants name the agent's worktree and the actual git common directory (`git rev-parse --git-common-dir`), never `<checkout>/.git`, the hub's namespace, or its state directory. A harness that sandboxes its MCP servers' reads grants the bridge the token directory alone (§12); M3 records which grant each harness needs |
| Shared-ref lock contention: parallel agents (of one hub or of sibling hubs) fetching, committing, or pushing at once fail with `cannot lock ref` | Transient; the guides say retry the git command after a short wait, never delete a `.lock` file |
| Shared refs and config: one agent's `git config`, hooks, `gc` affect all | Guides forbid changing git config; per-worktree config via `extensions.worktreeConfig` only if needed |
| Local branch-name collisions, within a hub or between hubs | `robomate/<hub>/<agent>/…` namespace; an existing ref is refused, not reused |
| `git worktree remove` deletes ignored files without complaint, such as hub state in a `.robomate/` | Hub state lives outside every checkout (§4); agent worktrees are locked |
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
  time per hub. `abandoned` is set only by the operator (`robomate abandon [--reason]`): the explicit exit
  for a run that should end without delivering. It bypasses the `done` check, keeps
  worktrees, and is recorded in the run report.
- **One run = one PR/MR** (locked). Sequential runs come from the queue.
- **The queue and history are per hub.** `robomate submit` queues on the selected hub
  (§4 discovery). Parallel work in one repository uses parallel hubs, one per checkout;
  there is no cross-hub scheduling (§6).
- **Carried context between runs** is explicit and small: the previous run's summary
  (PR/MR, merged SHA, follow-ups filed) is available in `get_state`; nothing else carries over.
- Policy additions (to PoC `WorkflowPolicy`):
  - `deliver: merge | pr` (default `merge`) — #14.
  - `closeout: true | false` (default `true`) — run the close-out task (§7.3).
  - `on_done: release | standby` (default `release`). Standby keeps workers joined for the
    next queued run; idle holds cost tokens, so `release` is the default when the queue is empty.
  - the hub's forge (§4, §10) applies; it is not a policy field.
- Operator policy defaults come from the configuration layers (§4): the hub's `config.toml`,
  then the repository's, then the user's; `robomate submit --policy k=v` overrides per run.

### 7.3 Close-out (#14, #15, #16)
Automates the operator's PoC routine — skim the agents' close-out reports, ask the implementer
about anything non-trivial, have it file issues or add to a nits list, confirm the merge —
using mechanisms that already exist (a task role like `rebase`, a typed result with a
validator, the merge gate). The one judgment call, issue vs. nit vs. drop, stays with the
implementer, as it did in the PoC; the difference is that it is always asked for and always
recorded.

**Repo settings** (all optional): repository-level settings, read from the repository's
`config.toml` in the git common directory, which every checkout's hub shares; a hub's own
`config.toml` may override them (§4):

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

**The run report.** At `done` or `abandoned` the hub renders `runs/<id>/report.md` in its
state directory (§4) from typed data only: statement of work (link and SHA), agents and harnesses, rounds, approved and merged SHAs, each finding with its disposition and
link, close-out steps, and every `none` with its reason. `robomate report [run]` prints it;
`robomate status` shows its headline. The operator's remaining touch is reading the report when they
choose and triaging `followup_label` issues in the forge.

### 7.4 Asking the operator
An escalation is `ask_user(question, options?)`, answered with `robomate answer`. `ask_user`
queues the question and sets `escalated`; `robomate inbox` lists it; `robomate answer <id>
<text>` (or `--option N`) answers it once through the operator-only `hub.answer` (#130); and the
orchestrator receives a `user_answered` event. It is for escalations (the PoC rails), not
routine close-out. Asking in chat is no longer an escalation path: only an answer given with
`robomate answer` is an operator decision. The inbox makes a headless orchestrator possible and
is the channel a future dashboard uses.

### 7.5 Resume (#18, reduced; #19)
- **Hub restart:** `robomate up` in the owning checkout reuses the hub's state, `hub_id`, name,
  and port. Bridges reconnect with backoff under the same
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
| `get_operator_answer(question_id)` | worker | **new** (#129): reads `hub.operator_answer`, so a worker can check an operator decision it is told of |
| `get_statement(sha)` | worker, orchestrator | **new** (#9) |
| `start_workflow` | orchestrator | **new**; replaces `initialize_workflow` |
| `get_state` | orchestrator | adds run summary, queue, close-out status, listening state; compact by default |
| `wait_for_event` | orchestrator | new event kinds `work_submitted`, `user_answered` (payload `question_id`, `answer`; #129) |
| `assign_task` | orchestrator | adds `statement_sha256` and role `closeout` (hub appends pending findings and steps); refuses incompatible standing role; refused (-32002) while `escalated`, naming the open question ids (#132) |
| `reply`, `set_task_state`, `release_agent`, `log_decision` | orchestrator | unchanged |
| `set_workflow_status` | orchestrator | `done` refused unless merged/open per `deliver` and close-out completed; `escalated` refused (-32602), since only `ask_user` escalates; leaving `escalated` refused (-32002) while any operator question is open, and its decision row names the questions answered, each tied to it by `operator_question.resumed_by` (DB schema v16); with none open, a legacy escalation resumes (#132) |
| `check_merge_gate` | orchestrator | forge-dispatched by the hub's forge in `hub.json` (§10): `gitlab` → `GitLabGate`, anything else → GitHub; same report shape; refused (-32002) while `escalated`, naming the open question ids (#132) |
| `ask_user(question, options?)` | orchestrator | **new** (#129): holds the question, sets `escalated` with an attributed decision row, returns `question_id`; asking again while escalated adds a question |
| `robomate inbox [--json]` | operator (CLI, not MCP) | **new** (#130): lists open questions (id, time, asking actor, question, options) from `hub.questions` |
| `robomate answer <id> <text>` / `--option N` | operator (CLI, not MCP) | **new** (#130): answers one question through `hub.answer` with the operator credential and prints a confirmation; `robomate status` shows the open-question count |

The orchestrator operations move from in-process MCP to an authenticated HTTP JSON-RPC route
on the hub; the worker A2A route is unchanged. `robomate status/submit/inbox/answer` use the same
route with an operator identity; `robomate abandon` and `robomate report` are operator-only.
The first operator methods on this route are `hub.info` and `hub.shutdown` (M1 Step 2),
followed by `hub.heartbeat` for orchestrator session liveness (M1 Step 4).
From M1 Step 3 it also serves the orchestrator operations, named after today's tools, with
the tool arguments as `params` (an object) and the tool's dict as `result`. Errors carry a
stable `code` and the original message: -32602 invalid params (including argument
validation), -32001 not found, -32002 conflict, -32003 payload too large, -32004 merge gate
unavailable, -32005 operator credential required, -32603 anything else.
Every orchestrator operation carries `X-Robomate-Actor` and `X-Robomate-Session` and is refused
without them; `hub.info`, `hub.status`, and the read-only `hub.questions` (open operator
questions) and `hub.operator_answer(question_id)` (the question, its answer and answer time, or
`unanswered`; not found is -32001) need only the bearer token (#129), as does `hub.snapshot`,
the operator's before-snapshot for a manual resume (#146: hub ID, workflow, current session,
open tasks, unacknowledged deliveries; it accepts no session and leases nothing). Operator-only methods,
`hub.shutdown` first, also need `X-Robomate-Operator` with the operator token that `robomate up`
creates beside the machine registry (#128). `hub.answer(question_id, answer)` is one (#130): it
records the answer, its time and actor `operator`, and queues `user_answered`; an unknown id is
-32001 and a second answer to a question is -32002. Each call other than `get_state`, `wait_for_event`,
`hub.info`, `hub.status`, `hub.snapshot`, `hub.heartbeat`, `hub.record_calls`, `hub.questions` and
`hub.operator_answer` leaves an `rpc_audit` row (actor,
session, method, outcome; never params), as does each orchestrator session takeover.

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
  or known to `glab` → GitLab; otherwise `--forge`. Recorded in `hub.json`.
- **Agents use the CLIs** (`gh`, `glab`) with their own authentication, as in the PoC.
- **Merge gate** (`check_merge_gate`, read-only facts): a `ForgeGate` interface with
  `GitHubGate` (the PoC implementation) and `GitLabGate`, chosen at hub start from the forge
  `up` recorded in `hub.json` (`gitlab` → `GitLabGate`, anything else → GitHub; restarts choose
  the same). It maps GitLab pipelines, `detailed_merge_status`, and diverged-commit counts to
  the same report shape, following
  [the GitLab feasibility study](feasibility-and-impact-of-supporting-gitlab-centric-workflows.md) §4.
  - **Transport:** `glab api`, run with the environment of the operator who ran `robomate up`.
    The hub holds no forge token and makes no HTTP calls to the forge.
  - **Binding:** the gate reads only the hub's own project, whose host and path are parsed
    once from `origin`; `--hostname` and every API path come from there, never from the MR
    URL. A URL is accepted only as `https://<host>/<project>/-/merge_requests/<iid>`: no
    userinfo, query, fragment, percent-encoding, or dot segment, and a port only if absent or
    443; host and path segments compare case-insensitively. Anything else, including another
    host or project, is refused with -32004 before any `glab` call. An origin served over
    plain `http`, on another HTTPS port, or under a relative URL root is not supported; the
    gate then fails closed on every call.
  - **Pipelines:** every pipeline GitLab ran for the head SHA must pass, not only the MR's
    `head_pipeline`. The gate lists them with `pipelines?sha=<head>` (each pipeline's own
    status is a check) and reads `repository/commits/<head>/statuses` with `all` at its default,
    keyed by job name and pipeline, so a green job in one pipeline cannot hide a red one in
    another. Retrying a job or pipeline in place supersedes the earlier attempt; a new pipeline
    for the same SHA supersedes nothing, so an older failure still fails the gate. Branch
    pipelines are the supported configuration; a merged-results `head_pipeline`, whose SHA is
    not the head, reads as `no_checks`.
- **Merge:** the orchestrator merges with the forge CLI, bound to the approved SHA
  (`gh pr merge --match-head-commit …`; `glab mr merge --sha …` per the study §5).
  The orchestrator never uses auto-merge: a deferred merge could happen after its checks.
  The GitLab run path passes `--auto-merge=false` explicitly (M5 Step 4).
- **GitLab startup preflight:** prints one line per check: `glab` present and ≥ 1.36.0;
  authentication via `glab api --hostname <origin host> user`; project readable with developer
  access or more; unsupported settings; Auto DevOps; and "Pipelines must succeed".
  Unavailable CLI, auth, project, or version checks warn and allow startup, so restart does
  not depend on forge reachability. Auto DevOps on warns (the gate cannot report
  `no_workflows`); "Pipelines must succeed" off is a defense-in-depth note.
  The project's `web_url` must match `https://<origin host>/<origin path>` case-insensitively;
  a definite mismatch refuses startup because a relative URL root can select a different
  project ([#80](https://github.com/RoboNater/robomate/issues/80)).
- **Unsupported on GitLab:** startup refuses definite automatic server-side rebase,
  merge trains or merge-train enforcement, and merged-results pipelines, naming the settings
  to disable. Policy `merge_method=rebase` is also refused where the policy is known.
  These are permanent restrictions while the client-verified, SHA-bound merge invariant
  stands: they rewrite or test a different SHA. Project `ff` and `rebase_merge` (semi-linear)
  are supported, with client-side rebase to a fresh approved head; they are subject to
  squash compatibility. `check_merge_compatibility` implements that matrix for the run path:
  `default_on`/`default_off` allow policy `merge` and `squash`; `always` allows only `squash`;
  `never` allows only `merge`, for each of project `merge`, `rebase_merge`, and `ff`.
  The orchestrator supplies the squash flag explicitly. Live matrix verification and its
  run-path caller landed in M5 Step 4 (#75).
- **Guides:** one guide per role, forge-neutral ("open a change request with your forge CLI"),
  composed at serve time with a short forge appendix (`guides/forge/github.md`,
  `guides/forge/gitlab.md`) holding the forge CLI commands the roles need
  (change-request, review, and issue reads). No N×M guide copies.
  Composition is the default: the route appends the appendix for the hub's forge (`gitlab`
  gives `gitlab.md`, anything else `github.md`, matching gate selection), `?forge=` overrides
  it, and a missing appendix for the effective forge is a 404. Appendices are worker-only:
  they hold no merge commands, since no worker role merges.
- **GitLab review comments:** through the Notes REST API via `glab api`
  (`POST projects/<project>/merge_requests/<iid>/notes`). Top-level notes are non-resolvable,
  so they can never become a `discussions_not_resolved` merge blocker. A note response carries
  an `id` but no `web_url`, so the reviewer builds `ReviewerResult.review_url` as
  `<MR URL>#note_<id>` and reads the note back by that `id` before reporting it.
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
- **Run identity:** every workflow gets a run ID and a manifest in `runs/<id>/` in the hub's
  state directory (§4)
  (robomate version, git revision, harness profiles, policy, statement SHA)
  ([`docs/poc-lessons.md`](poc-lessons.md)).
- **Accounting on by default** (#7); `call_log` retention bounded (#23), since hubs now live
  for days.
- Alice's bridge measures MCP request, response, content, and repeat bytes on its
  stdio stream, then sends compact rows to `hub.record_calls`. The hub records
  them as `boundary=mcp` with the orchestrator actor. The HTTP hub records its
  own A2A and guide rows; raw `/rpc` bytes are not counted in M1. This uses
  the existing `call_log` schema and wire version. A superseded bridge may
  flush measurements of completed calls; this does not reclaim the active
  orchestrator session.
- **Token levers (#22):** longer holds where the harness allows; minimal timeout payloads;
  compact `get_state`; guides returned as "unchanged (hash)" when the bridge has already
  delivered them this session (with `force` to re-fetch); smaller tool schemas;
  `on_done: release`. Baseline first, then a target.

---

## 12. Security (MVP threat model)

Single operator. Loopback bind by default. Agents are untrusted as to role and authority.
The hub enforces roles, attributes every action, and accepts operator authority only
through the operator channel. Keeping the operator credential away from agents depends on
the harness sandbox; a harness run without one can read it, so such runs are trusted-only.
Identity and model metadata are self-declared (PoC §1).
Agents share the operator's forge identity; the hub's typed reviewer verdict remains the
approval record (PoC #37). The merge gate runs the forge CLI with that identity, so it reads
only the hub's own repository: a change-request URL, which arrives in untrusted worker
results, never chooses the host or project (§10). Remote-agent mode requires a trusted network path.

**Credentials.** The MVP model is one shared bearer token per hub plus the separate
operator credential beside the machine registry (§4, #128). Per-agent credentials stay a
non-goal (§1) and post-MVP (§16). Neither is ever written into a harness config.
- **Where the token lives:** until M2, `token` in the hub's state directory (`.robomate/`).
  From M2, a hub whose state is in `<worktree_root>/<hub>/state/` keeps its token in its
  own directory beside the state, `<worktree_root>/<hub>/token/token`, not in `state/`, so
  a harness that sandboxes its MCP servers' reads can be granted the token alone, never
  `hub.db` or `runs/`. A legacy hub run in place (§4 Migration) keeps `.robomate/token`
  until the operator relocates it.
- **How a bridge reads it:** an explicit `ROBOMATE_TOKEN` or `ROBOMATE_TOKEN_FILE` comes
  first (today set by `prepare-run.py`, and always for remote agents). Otherwise
  `robomate mcp` reads the token of the hub discovery selected (§4): until M2 from that
  checkout's `.robomate/token`; from M2 it follows the checkout's binding, which holds no
  token, to the token file the binding names, either path above, and reads it on the
  agent's behalf.
- **The token-only grant** is a read grant on the `token/` directory of a relocated hub.
  For a legacy hub it is a read grant on the single file `.robomate/token`, never on
  `.robomate/`. A harness that can grant only directories cannot isolate a legacy hub's
  token; with such a harness the operator relocates the hub first, or the run is
  trusted-only.
- **Which grant a harness needs:** whether each harness runs MCP servers inside its sandbox,
  and so which read grant the bridge needs, is recorded per harness in M3 and checked by
  `robomate certify` (§9.3).

**Operator-only actions.** The orchestrator routes these to `ask_user` (§7.4), never to a
worker, and a worker whose task needs one returns `blocked` naming it (#132):
- starting or stopping hubs (`robomate up`, `robomate down`, `robomate down --all`),
  forgetting hubs (`robomate forget`), and launching agent harnesses;
- acting or speaking for the operator, or accepting work on the operator's behalf;
- changing the default branch outside the PR, or forge or repository settings;
- creating and removing robomate worktrees (`robomate up --agents`, `robomate workspace`,
  `robomate clean`), and removing a hub's state.

Workers also never call the hub's `/rpc` route, change workflow status, or start or stop
agents (#132).

A worker's isolated smoke hub is not "starting a hub" in this sense, provided it meets every
condition in §4 (#145).

**Agent boundaries.**
- A worker never reads a hub's state directory (#132's "never read `.robomate/`"). The
  rule binds the agent and every tool it runs. It does not bind the agent's `robomate mcp`
  bridge, which reads the hub's token on the agent's behalf.
- Sandbox grants name the agent's worktree and the actual git common directory. They never
  name `<checkout>/.git`, which is a file in a linked worktree, and never the hub's
  namespace `<worktree_root>/<hub>/` or its state directory (§6).

---

## 13. Implementation plan

Each milestone leaves robomate usable for real work, and from M1 on robomate is built with
itself.

| # | Milestone | Deliverable | Done when |
|---|---|---|---|
| M0 | Seed robomate | `RoboNater/robomate` created with the PoC files and this spec; `poc-spec.md` frozen; PoC acceptance scripts archived (Appendix B); robo-agents #57 exported to `docs/poc-lessons.md`; Python distribution renamed `robomate`; roadmap issue, a minor-nits issue, and one issue per carried item (Appendix A) opened; robo-agents archived with a pointer to robomate | CI green in robomate; the roadmap lists M1–M6 with the carried issues attached; robo-agents is read-only |
| M1 | Standalone hub + `robomate` CLI | `robomate up/down/status/ls`, `.robomate/` layout, registry, port reuse; orchestrator JSON-RPC route; `robomate mcp` bridge for all roles (existing tool names) with heartbeat and discovery; accounting on (#7); fix flaky tests (#6) | A real run completes with the hub from `robomate up`, and the orchestrator's session is restarted mid-run without disturbing workers |
| Pre-M2 | Checkout-scoped hubs: identity, discovery, lifecycle | The checkout owns its hub, bare-repository layouts included; hub names; per-hub registry entries, `up.lock`, and port; discovery without silent attach and an explicit hub selector for the CLI; legacy hubs keep their state (§4). State stays in the checkout's `.robomate/`, reached through one resolver | Two hubs from two linked worktrees of one repository run at once with distinct names, state, and ports, and `robomate ls` shows both; stopping one leaves the other running; `down` and `status` in a checkout with no hub refuse and list both; a hub created before the change starts with its old state; the standard run path is unchanged |
| M2 | Join, worktrees, work intake | `join`/`whoami`, standing roles, takeover + task reattach; hub state and token moved to `<worktree_root>/<hub>/` with checkout bindings (§4, §12); worktree manager (§6): generated worktrees per `(hub, agent)` for every agent, locks, `robomate/<hub>/<agent>/<label>` branches; configuration layers (§4); `robomate submit`, queue, `get_statement` (#8, #9); `guides/orchestrator.md` served; `robomate workspace`, `robomate clean`, and hub selection for each; the agent boundaries and operator-only steps of §12 | On Claude Code + Codex: `robomate up`, three one-line joins, `robomate submit`, merged PR — with no `prepare-run.py`, no generated configs, no pasted launch prompts; the same again from a second worktree's hub while the first runs |
| M3 | Harness breadth | `robomate install` (both scopes), harness profiles, `robomate certify`, `docs/harnesses.md`; supervisor/nudge path; per-harness record of the bridge's read grant (§12) | OpenCode and AntiGravity certified; Cline attempted and recorded; one real run with a non-Claude orchestrator |
| M4 | Close-out and resilience | `closeout` task role, `CloseoutResult` + validator, `done` check, run report + `robomate report`, `robomate abandon`, repo settings (`nits_issue`, `followup_label`, `closeout_steps`); `ask_user` inbox with `robomate inbox/answer`; resume (§7.5, #18 reduced, #19); `robomate log --timeline` (#20) | Scripted tests: `done` refused with an unmerged PR and with no completed close-out; a `CloseoutResult` missing a pending finding, a URL, a `none` reason, or a step is rejected; the report lists every finding with its disposition; kill/restart of the hub, a worker, and the orchestrator each resume without duplicate actions; simulated standby loses no one. One real run on robomate itself ends with its follow-ups filed and its roadmap issue updated, with no operator chat |
| M5 | GitLab | Forge detection, `GitLabGate` via `glab api`, forge appendix guides, preflight for unsupported settings | A multi-round run with a changes-requested round and a stale-base rebase merges on the self-hosted GitLab |
| M6 | Token budget + MVP acceptance | Baseline from accounting; #22 levers; retention (#23) | Two acceptance runs (GitHub, GitLab), mixed certified harnesses, each with a changes-requested round, one component restart, and two queued statements; per-run bytes by role reported against the baseline; setup from `robomate up` to first assignment timed |

**Order:** M1, M5, pre-M2, M2, M3, M4, M6. The names stay M1–M6; only the order changed (#68).

**Checkout scope (#54)** is delivered in three steps, so that nothing ships which M2 would
have to undo:
- **Pre-M2 (#147):** ownership, hub names, discovery, per-hub lifecycle, and migration (§4) for
  state that is still in the checkout.
- **M2:** the state location and bindings (§4), generated and locked worktrees, branches,
  and cleanup (§6), configuration layers (§4), the token under bindings (§12), and the
  rest of §12's operator-only steps and agent boundaries. The smoke-hub rule (§4) is
  delivered by #145 for today's state (registry, operator credential, and explicit hub
  selection); M2 adds the configuration-layer and worktree-root overrides.
- **M3:** the per-harness record of the bridge's read grant (§12), checked by
  `robomate certify`.

**Landed early from M4:** the `ask_user` inbox with `robomate inbox` and `robomate answer`
(#129, #130), and the hub's escalation guard (#132), all after #99, in which a run was
escalated and resumed without an operator answer.

**Parallelism:** M5 goes first after M1. GitLab is an MVP goal (§1), its gate and forge
detection need neither `join` nor worktrees, and #50 already prototyped both. It runs on the
M1 topology, with runs prepared by `prepare-run.py`
([M5 plan](development/implementation-plan-mvp-m5-gitlab.md)). For M2 this means the role
guides are already forge-neutral and composed with a forge appendix at serve time.
`guides/orchestrator.md` follows the same pattern, `join` reports the hub's forge (§5), and
the GitLab preflight that `prepare-run.py` runs moves to `robomate submit` when M2 removes
the script. M3's certification checks `glab` as well as `gh` (§9.3), and needs M2's join and
worktrees. M4 depends on M2.

---

## 14. Decisions (locked)

| Decision | Choice |
|---|---|
| Unit of work | One run = one PR/MR; sequential runs via queue; carried context = previous run summary only |
| Hub scope | One hub per checkout (main checkout or linked worktree), own name, state, and port; registry + `robomate ls`/`robomate status` to tell them apart; no silent attach. Reason: a git worktree should be an isolated enough place for a team of agents to work, so parallel lines of work in one repository need separate worktrees, not full clones (#54) |
| Forge operations | Agents run `gh`/`glab` directly; hub gate reads facts only; orchestrator merges bound to the approved SHA |
| Workspaces | Generated worktree per agent (orchestrator included) at `<worktree_root>/<hub>/<agent>/`, outside every checkout, beside the hub's state; locked while in use; the owning checkout is never a workspace; clone fallback for other host/OS. Reason: state must survive `git worktree remove` of the owning checkout, and stable paths let harnesses resume sessions (#54) |
| Harness/model/role | Any certified harness, any model and effort, any role; tiers from `robomate certify` |
| Forges | GitHub and GitLab (incl. self-hosted) |
| Instruction delivery | Tool descriptions, join response, served guides; no harness-specific skill required |
| Close-out | Hub-built `closeout` task with a validated typed result; `done` requires merge state per `deliver` and a completed close-out; run report replaces reading agent chat; `robomate abandon` is the operator's no-delivery exit. No general obligation ledger in the MVP |
| Orchestrator mode | Interactive or headless; both supported via the inbox |
| Users | Single operator; per-hub shared token plus the operator credential; loopback default |
| Name | `robomate` for repository, distribution, CLI, MCP server, and skill; no shipped short alias |

---

## 15. Open decisions

| Decision | Options | Settle by |
|---|---|---|
| Tool surface | One static list (simple) vs per-role list via `tools/list_changed` (fewer schema tokens; harness support varies) | M3, from measured schema bytes |
| `gh`/`glab` merge vs local branch in a worktree | Remote-only branch delete + `robomate clean`, or merge from outside any checkout | M2 |
| Certification bar | cycles / duration / pass rate over N attempts | M3 |
| Default `reviewer_harness_differs` | keep `true` or relax to `false` for flexibility | M2 |

---

## 16. Later (post-MVP)

- **Forge pass-through tool:** the hub provides an authenticated `gh`/`glab` wrapper that
  passes all arguments through and returns full output — central auth and audit without
  giving up agent-driven discovery.
- **One local router:** a single `robomate` service on one port hosting several hubs by path or
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
| #84 Codex hides tools with extra `--add-dir` | Carry → M3 re-test; worktrees likely make the git common directory grant necessary |
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
| `skills/alice-relay`, `docs/historical/poc/notes/relay-trial-*` | Archived |
| `skills/worker`, `prompts/*.md` | Replaced by join responses (M2) |
| `docs/user-guide.md` | Rewritten around `robomate` (M2, M3) |
| `packages/worker_mcp` | Becomes `robomate mcp` |
