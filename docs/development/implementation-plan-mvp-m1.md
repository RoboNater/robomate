# Implementation plan — MVP Milestone M1: standalone hub + `robomate` CLI

Status: proposed. Tracking issue [#26](https://github.com/RoboNater/robomate/issues/26);
roadmap [#2](https://github.com/RoboNater/robomate/issues/2), section M1.

This plan turns [`docs/mvp-spec.md`](../mvp-spec.md) §13 row M1 into a sequence of
PR-sized steps. It is a plan, not a design of record: where it settles something the spec
leaves open, the step that implements it also updates the spec (see
[Spec deltas](#spec-deltas)). §-numbers refer to `docs/mvp-spec.md` unless marked
"PoC §".

## What M1 has to deliver

From §13:

> **Deliverable:** `robomate up/down/status/ls`, `.robomate/` layout, registry, port reuse;
> orchestrator JSON-RPC route; `robomate mcp` bridge for all roles (existing tool names) with
> heartbeat and discovery; accounting on (#7); fix flaky tests (#6).
>
> **Done when:** A real run completes with the hub from `robomate up`, and the orchestrator's
> session is restarted mid-run without disturbing workers.

The M1 issues on the roadmap are:

- [#6](https://github.com/RoboNater/robomate/issues/6): intermittent async test failures
  under full-suite load.
- [#7](https://github.com/RoboNater/robomate/issues/7): turn call accounting on so
  `hub-report` has Alice's bytes and the hub-side wire bytes.

Not in M1, even though later milestones depend on it: `join`/`whoami`, standing roles,
takeover, worktrees, `robomate submit`, the queue, and `get_statement` (all M2). Also
`robomate install` and harness profiles (M3); close-out, the inbox, full resume and standby
handling, and `robomate log` (M4); forge detection beyond GitHub (M5); and `call_log`
retention (#23, M6). M1 keeps the existing tool names (`check_in`, `ask_alice`,
`initialize_workflow`, …) and the PoC's full-clone worker workspaces.

## Facts from the current code that shape the plan

1. **The hub lives inside Alice's MCP process.** `agent_hub.main.run_hub` runs uvicorn and
   the stdio MCP server (`agent_hub.mcp.run_mcp`) as sibling tasks, and when either one
   exits it shuts the other down. So an orchestrator restart today *is* a hub restart. It
   takes the worker A2A endpoint with it, and that is the thing the M1 done-when forbids.
2. **Alice's tools are functions closed over the store.** `agent_hub.mcp.create_mcp` defines
   ten FastMCP tools whose bodies are one or two lines over `HubStore` and `MergeGate`. They
   are easy to lift into a transport-neutral operations object.
3. **Workers already have most of a bridge.** `worker_mcp` is an HTTP client with a
   background heartbeat, retries with backoff, idempotent operation IDs, and telemetry. Its
   configuration is `HUB_URL`, `HUB_TOKEN`, and `AGENT_NAME` from the environment
   (`worker_mcp.config.WorkerSettings.from_env`).
4. **Settings are environment-only.** `HubSettings.from_env` anchors state to `HUB_STATE_DIR`,
   and the token file, database, and guides are all absolute paths. `robomate up` can reuse
   all of that validation by building an environment overlay. It does not need a second
   configuration path.
5. **Accounting is off by default, and Alice's side is observed on the hub's stdio.**
   `HubSettings.call_accounting` defaults to `False`. `McpAccounting` measures Alice's MCP
   bytes by watching the hub's own stdin and stdout (`CancellableStdin` and
   `CancellableStdout`). When the hub stops serving stdio, that measurement has to move to
   the process that does serve it: the bridge.
6. **`call_log.boundary` is `CHECK (boundary IN ('a2a', 'mcp'))`** (schema v12). A new
   boundary value would need schema v13. The plan avoids needing one (see
   [Accounting](#accounting-7)).
7. **Event leases outlive a dead orchestrator.** `wait_for_event` leases an event for
   `HUB_EVENT_LEASE_S` (default 600 s), and nothing ties the lease to a session. An
   orchestrator killed mid-lease comes back to find that event invisible for up to ten
   minutes.
8. **Several scripts launch the hub as an MCP stdio child:** `scripts/prepare-run.py` (via
   the rendered `alice.mcp.json`), `scripts/mock-alice.py --mcp`, and
   `scripts/measure-call-bytes.py`. Each has to move to the new topology in the same PR that
   removes stdio from the hub.
9. **Distribution layout.** The root `pyproject.toml` is `robomate` with
   `[tool.uv] package = false`, so it cannot declare a console script. The workspace members
   are `agent-hub`, `agent-hub-common`, and `worker-mcp`.

## Design

### Topology after M1

```
 operator terminal (in the target repo)
 $ uv run --project <robomate checkout> robomate up      # foreground; Ctrl-C stops it
        │ writes <repo>/.robomate/{hub.json,hub.db,token}; registers in hubs.json
        ▼
 ┌─────────────────── hub (HTTP only) ───────────────────┐
 │ POST /a2a   worker A2A (unchanged)                    │
 │ POST /rpc   JSON-RPC: orchestrator ops + hub.* ops    │
 │ GET  /guides/{role}.md, /healthz, agent card          │
 └──────────▲───────────────────────────▲────────────────┘
            │ /rpc                      │ /a2a
 alice's harness ─ stdio ─▶ robomate mcp --role orchestrator
 bob's harness   ─ stdio ─▶ robomate mcp --role worker     (full clone, as in the PoC)
```

### CLI packaging

- A new workspace member, `packages/cli/`: distribution `robomate-cli`, import package
  `robomate`, console script `robomate = robomate.cli:main`. It depends on the other three
  members, and the root project depends on it. `uv run robomate …` then works from a
  checkout, and `uv run --project <checkout> robomate up` works from any target repo.
  `--project`, unlike `--directory`, keeps the working directory, and `up` needs that to
  find the repo.
- Subcommands: `up`, `down`, `status`, `ls`, `mcp`. Arguments are parsed with `argparse`, as
  the scripts already do. No new dependencies.
- Discovery, `hub.json`, and registry code lives in `agent_hub_common`
  (`discovery.py`, `registry.py`), because both the CLI and the bridge (`worker_mcp`) need
  it. `agent_hub_common` still depends on neither of them.
- No package renames in M1. `agent_hub` and `worker_mcp` keep their import names. Appendix
  B's "`packages/worker_mcp` becomes `robomate mcp`" is met by the subcommand, not by a
  rename.
- `uv tool install git+…` (§2) is **not** an M1 requirement. It needs the root to become a
  package and the guides to be packaged, and both belong with M2 (served orchestrator guide)
  and M3 (`robomate install`).

### `.robomate/` layout and `hub.json` (§4)

`robomate up` resolves the repo once, from the working directory, and passes absolute paths
on:

- **Repo root:** `git rev-parse --path-format=absolute --git-common-dir`, then its parent,
  so that `up` run inside a worktree still finds the owning repo (§4 step 2).
- **Origin:** `git remote get-url origin`.
- **Default branch:** `git symbolic-ref --short refs/remotes/origin/HEAD`. If that is unset,
  `up` fails and tells the operator to run `git remote set-head origin --auto`, rather than
  guessing `main`.
- **Forge:** `github` when the origin host is `github.com`, otherwise `unknown`. Full
  detection is M5.
- `.robomate/` is created with mode 0700. `/.robomate/` is appended once, idempotently, to
  `<git-common-dir>/info/exclude` and never to a tracked `.gitignore`.
- The hub is started with the overlay `HUB_STATE_DIR=<repo>/.robomate`, `HUB_HOST`,
  `HUB_PORT`, `HUB_PUBLIC_URL`, and `HUB_CALL_ACCOUNTING=1` (unless `--no-call-accounting`),
  applied on top of `os.environ` and passed to `HubSettings.from_env`. `hub.db` and `token`
  fall out of the existing defaults.
- `hub.json` is written atomically (temporary file, then `os.replace`) after the socket is
  bound. It holds the §4 fields `repo_root`, `origin`, `forge`, `default_branch`, `url`,
  `port`, `pid`, `started_at`, and the version field, plus **`hub_id`** (a random ID minted
  on the first `up` and kept across restarts). On a clean exit, `pid` and `started_at` are
  cleared and `port` is kept for reuse.
- `config.toml` and `runs/` are not created in M1. Their first readers are M2 (`setup`,
  `worktree_root`) and M4 (the run report).

The AGENTS.md invariant "Nothing reads the working directory" still holds for the hub
process. Only the `robomate` CLI reads it, once at startup, to find the repo. The CLI PR
updates AGENTS.md to say so.

### Port selection and reuse (§4)

- `--port N` pins the port. Otherwise `up` uses `hub.json`'s `port` if one is recorded, or the
  first free port from 8420 upward if not.
- A recorded port that is busy is an error, not a silent move. The message names the port
  and suggests `--port`, because remote agents depend on it staying put.
- To avoid a check-then-bind race, `up` binds the listening socket itself and hands it to
  uvicorn with `Server.serve(sockets=[sock])`.
- `--bind` and `--public-url` map to `HUB_HOST` and `HUB_PUBLIC_URL`, so the existing
  wildcard-bind check still applies (§4 remote agents).

### One hub per repo, and `robomate down`

- `up` refuses to start when `hub.json` names a live hub: its `pid` is alive and `/healthz`
  at its `url` returns the same `hub_id`. It prints that hub's URL instead.
- `/healthz` (public, PoC §4.1) returns `{"status": "ok", "hub_id": …}`. It does not return
  the repo path, which would be the only unauthenticated disclosure.
- `robomate down` finds the hub by discovery and calls `hub.shutdown` on `/rpc` with the
  token. The same call works on Windows, which has no `SIGTERM`. The hub stops the way it
  does on Ctrl-C. If the hub cannot be reached and its pid is still alive, `down` says so and
  prints the pid. It does not kill processes itself.

### Machine registry (§4)

- The registry is `$XDG_STATE_HOME/robomate/hubs.json`
  (default `~/.local/state/robomate/hubs.json`; on Windows,
  `%LOCALAPPDATA%\robomate\hubs.json`). Each entry records `hub_id`, `repo_root`, `url`,
  `pid`, and `started_at`.
- Writes take an advisory lock (`registry.lock`, created with `O_EXCL` and a bounded retry)
  and then replace the file atomically.
- `up` registers the hub and a clean exit deregisters it. Every reader prunes entries whose
  pid is dead or whose `/healthz` `hub_id` does not match.
- `robomate ls` lists repo, URL, agent count, and workflow status for each live hub. It reads
  each hub's token from `<repo_root>/.robomate/token`; a single operator owns them all
  (§12).

### Discovery (§4) — shared by `robomate mcp`, `status`, and `down`

The rules are tried in order:

1. `ROBOMATE_HUB_URL` plus `ROBOMATE_TOKEN` or `ROBOMATE_TOKEN_FILE`.
2. The working directory's git common dir, then the owning repo root, then
   `.robomate/hub.json` and `.robomate/token`.
3. Exactly one live hub in the registry.
4. Otherwise an error that lists the registered hubs.

In M1 there is no `join` to carry `hub=<repo>`, so the bridge resolves the hub on its first
tool call and returns the rule-4 list as the tool error. Resolving lazily also means that a
harness starting before `robomate up` does not fail to load the MCP server.

A PoC full-clone worker has its own repo root with no `.robomate/`, so it finds the hub by
rule 3. With two hubs running, it needs `ROBOMATE_HUB_URL` (rule 1). M2's worktrees make
rule 2 the normal case.

### Orchestrator JSON-RPC route (§8)

- `POST /rpc` requires the bearer token and takes JSON-RPC 2.0, with one request per body and
  no batches.
- **Orchestrator methods** are named exactly after today's tools: `get_state`,
  `initialize_workflow`, `wait_for_event`, `assign_task`, `check_merge_gate`, `reply`,
  `set_task_state`, `release_agent`, `set_workflow_status`, `log_decision`. Their params
  match the tool arguments and their results are the dicts the tools return today.
- **Operator methods:** `hub.info` (the `hub.json` facts plus the version), `hub.status`
  (the compact summary for `robomate status`), `hub.shutdown`, `hub.heartbeat` (orchestrator
  session liveness, below), and `hub.record_calls` (bridge-measured MCP accounting rows,
  below).
- **One implementation.** The tool bodies move from `agent_hub/mcp.py` into an
  `OrchestratorOps` class (`agent_hub/orchestrator.py`). `/rpc` dispatches to it. Argument
  validation keeps the constraints the FastMCP signatures enforce today (`Timeout` ≤ 120 s,
  `Lease`, the 40-hex `Sha`), using the same pydantic `Annotated` types.
- **Errors.** Store and validation errors become JSON-RPC errors with a stable `code` and the
  original message. The bridge turns them back into MCP tool errors, so Alice sees the same
  text as before.
- **Holds.** `wait_for_event` holds the HTTP request for up to `timeout_s` (≤ 120 s, as now).
  The bridge's client timeout is `timeout_s` plus a margin. A response lost to a transport
  error is safe: the event stays leased and is redelivered (see the next section), and
  `assign_task` (keyed by `event_id`), `reply` (`message_id`), and `log_decision` (`key`)
  are already idempotent. The bridge retries only transport failures that happen before a
  response starts, and only for `get_state`, `wait_for_event`, `check_merge_gate`, and the
  idempotent mutations above.
- **Actor.** The bridge sends `X-Robomate-Actor: <name>` and `X-Robomate-Session: <uuid>`.
  They are self-declared, like everything else under the §12 threat model. They feed
  accounting and the session rule below, not authorization.

### Orchestrator session and restart (the M1 done-when)

- The orchestrator bridge mints a session ID at startup and sends `hub.heartbeat` every
  30 s, the same period as the worker default. The hub keeps
  `{actor, session, last_seen}` **in memory** for `robomate status`. No schema change.
- **One orchestrator per hub (§5, reduced).** The first `/rpc` orchestrator call from a new
  session makes it current. Calls from the older session are then refused with a "superseded
  by a newer orchestrator session" error, so two Alices cannot interleave. This is the
  minimal form of M2's `takeover`.
- **A new session releases stale event leases.** When a session becomes current, every
  outstanding event delivery lease expires at once. The restarted orchestrator's first
  `wait_for_event` therefore gets the in-flight event back, instead of waiting up to 600 s
  (fact 7). This uses the existing re-delivery path (`delivery_attempts` + 1, new
  `delivery_id`), and `ack` of an old delivery keeps working (#50 semantics).
- **Workers are untouched.** Their tasks, leases, heartbeats, and holds are all on `/a2a`,
  and none of them involves the orchestrator's connection. An `ask_alice` pending across the
  restart is still in `get_state` and is answered by the new session. The resume procedure
  in the orchestrator skill already covers reconciling with the forge.

### `robomate mcp` bridge (§3, §8)

- `robomate mcp --role orchestrator|worker [--name NAME] [--harness H]`. In M1 the role and
  name come from flags, or from `AGENT_NAME` (worker) with a default of `alice`
  (orchestrator), because `join` does not exist yet. Profile fields keep coming from the
  existing `HUB_HARNESS`/`HUB_MODEL`/… environment (`profile_from_env`).
- **Worker mode** runs today's `worker_mcp` server unchanged. Only the settings source
  changes: URL and token come from discovery instead of `HUB_URL` and `HUB_TOKEN`.
  `HUB_WORKSPACE` and its identity check are kept. The `worker-mcp` entry point stays
  working, for rendered configs that still use it, until M2 replaces the configs.
- **Orchestrator mode** is a new FastMCP server (`worker_mcp/orchestrator.py`) with the same
  ten tools, **the same names, descriptions, and argument schemas** as `agent_hub/mcp.py`
  today. Each tool is a thin `/rpc` call. A golden test compares its `list_tools()` output
  with a fixture captured from the current hub before the move. Tool-schema bytes are part
  of the token budget (§11), and M1 must not change them by accident.
- The MCP server name becomes `robomate` in both modes (spec, "Naming"). The config key in
  rendered harness configs (which sets the `mcp__<key>__` prefix) changes from `hub` to `robomate` in
  the same PR, with `docs/user-guide.md`, which refers to `mcp__hub__…`, updated to match.
- The bridge's stdout is MCP framing only, and it logs to stderr (AGENTS.md invariant).

### Accounting (#7)

- **Hub side:** `robomate up` turns accounting on by default (`--no-call-accounting` to opt
  out), so `/a2a` and `/guides` rows, including worker heartbeats, are recorded as they are
  today.
- **Alice's MCP bytes:** `McpAccounting` (request/response observation, `content_bytes`,
  `repeat_bytes`) moves from the hub's stdio to the orchestrator bridge's stdio streams. The
  bridge sends finished `CallRecord`s to `hub.record_calls` in small batches, in the
  background and best-effort (dropped rows are counted and logged to stderr). The hub writes
  them as `boundary = 'mcp'`, `actor = <orchestrator name>`, exactly the shape
  `scripts/hub-report.py` reads now. No `call_log` schema change and no wire change.
- The raw `/rpc` HTTP bytes are **not** recorded in M1, because that would need a new
  `boundary` value (schema v13). If M6 wants them, it reserves v13 on the roadmap first.
- `HUB_CALL_LOG_JSONL` is still honoured by the hub for rows it records itself.
- `scripts/prepare-run.py` no longer starts a hub (Step 5), so #7's proposed
  "`HUB_CALL_ACCOUNTING=1` in prepare-run's hub env" becomes "on by default in `robomate up`".
  #7's reviewer commands are carried over, with `--state-dir <repo>/.robomate`.
- A long-lived hub with accounting on grows `call_log` without bound until #23 (M6).
  Recording is an append-only insert of a few hundred rows per run.

## Spec deltas

Each item is settled here and written into `docs/mvp-spec.md` by the step that implements it:

| Item | Settled as | Step |
|---|---|---|
| `hub.json` version field (§4 says `ra_version`) | `robomate_version`; §4 table updated | 2 |
| `hub_id` in `hub.json` and `/healthz` | Added; used for pruning and one-hub-per-repo | 2 |
| Operator methods on the orchestrator route (§8) | `hub.info`, `hub.status`, `hub.shutdown`, `hub.heartbeat`, `hub.record_calls` | 2, 3, 4 |
| Orchestrator restart before `join` exists | Newest session wins; stale event leases released | 4 |
| Bridge role before `join` exists | `--role`/`--name` flags; replaced by `join` in M2 | 4 |
| Where Alice's MCP bytes are measured | In the bridge, reported to the hub as `mcp` rows | 5 |

No DB schema version and no wire `hub.schema_version` change is expected in M1. A step that
turns out to need one stops and reserves the value on roadmap #2 before editing (AGENTS.md).

## Steps

Each step is one issue and one PR, reviewable on its own, and leaves `main` usable. A PoC-style
run (prepare-run plus the stdio hub) keeps working until Step 5 switches it over in one PR.

```
Step 1 (#6) ──────────────────────────────────────────────┐
Step 2 up/down/registry ──▶ Step 3 /rpc ops ──▶ Step 4 bridge ──▶ Step 5 cutover (#7) ──▶ Step 7 acceptance
                                   └──────────▶ Step 6 status/ls ──────────────────────────┘
```

Step 1 is independent and goes first, so that every later PR's CI result can be trusted.
Step 6 can run in parallel with Steps 4–5.

### Step 1 — Deterministic async tests (#6)

**Scope.** Reproduce both failures, find their cause, and make the tests deterministic.
Test code first; production code only if the cause is a real bug, explained in the PR.

**Approach.**
- Reproduce under load before choosing a fix. Run the full suite in a loop
  (`for i in $(seq 20); do uv run --locked pytest -x -q || break; done`) while another
  full-suite loop or a CPU hog runs next to it. Then run the two named tests alone under the
  same load. Record the attempts and failure rates in the PR (poc-lessons: record every
  attempt).
- Hypotheses to check, not assume:
  - `test_mock_alice_backends_recover_from_every_crash_point[direct-after_reply-…]` uses a
    10 ms event lease and a 30 ms sleep. `MAX(delivery_attempts) >= 2` depends on
    wall-clock scheduling. A candidate fix is to assert re-delivery through explicit lease
    expiry (the store's clock is injectable as `HubStore.clock`), rather than through sleep
    timing.
  - `test_worker_client_clears_pending_ids_after_success`: `sweep(lost_after_s=-1)` sets
    the cutoff to `now + 1 s`, but `heartbeat()` never moves `last_seen` backwards
    (`MAX(last_seen, ?)`, #120). A wall-clock step backwards of more than 1 s (WSL2
    resync) between the last stamp and the sweep leaves `last_seen` past the cutoff. A
    candidate fix is to drive the sweep from a controlled clock rather than a negative
    threshold.

**Tests.** The fixed tests pass 50 of 50 in a loop under the same load that reproduced them.
The full suite stays green.

```sh
uv run --locked pytest "tests/test_mock_alice.py::test_mock_alice_backends_recover_from_every_crash_point" \
    tests/test_idempotency.py::test_worker_client_clears_pending_ids_after_success
for i in $(seq 50); do uv run --locked pytest -q -p no:cacheprovider \
    tests/test_mock_alice.py tests/test_idempotency.py || exit 1; done
uv run --locked pytest
```

**Size:** small (tests plus possibly a one-line store fix).

### Step 2 — `robomate` CLI skeleton: `up`, `down`, `.robomate/`, registry, port reuse

**Scope.**
- `packages/cli/` (see [CLI packaging](#cli-packaging)), with root `pyproject.toml` and
  `uv.lock` updated through uv.
- `agent_hub_common/discovery.py` covers repo resolution, `hub.json` read/write, and the
  discovery rules. `agent_hub_common/registry.py` covers the registry file, its lock, and
  pruning.
- `robomate up`: resolve the repo, create `.robomate/`, update `info/exclude`, load the
  token, choose and bind the port, check for an existing hub, start the **HTTP-only** hub
  (a new `agent_hub.main.serve_http(settings, sockets)`), write `hub.json`, register,
  deregister on exit, and turn accounting on by default. `up` prints the URL and the
  bridge config lines to stdout; this process has no MCP. The existing `hub` entry point
  (HTTP plus stdio) is **left unchanged** in this step.
- `/rpc` with only `hub.info` and `hub.shutdown`, behind the bearer token.
- `/healthz` gains `hub_id`.
- `robomate down`.
- AGENTS.md: the working-directory invariant (CLI only) and "Stop any hub you start"
  (use `robomate down`).

**Tests** (`tests/test_discovery.py`, `tests/test_registry.py`, `tests/test_cli.py`,
additions to `tests/test_app.py`). Repo resolution runs from the main checkout and from a
`git worktree`. The exclude line is written once. The port is chosen first-free and then
reused. A recorded busy port is refused. A second `up` is refused while the first is live.
A stale `hub.json` or registry entry is pruned. `hub.json` is atomic, and its token file is
mode 0600. `down` stops the hub over `/rpc`, and `/rpc` without a token is 401.

```sh
uv run --locked pytest tests/test_cli.py tests/test_discovery.py tests/test_registry.py tests/test_app.py
# manual, in a scratch clone of any GitHub repo:
cd /tmp/scratch-repo && uv run --project <robomate checkout> robomate up &
cat .robomate/hub.json; grep robomate .git/info/exclude; curl -s localhost:8420/healthz
uv run --project <robomate checkout> robomate down   # hub exits; hub.json keeps port, pid cleared
uv run --project <robomate checkout> robomate up     # same port again
```

**Size:** medium. This is the largest new surface in M1 but touches no existing behaviour.

### Step 3 — Orchestrator operations on `/rpc`

**Scope.**
- `agent_hub/orchestrator.py`: `OrchestratorOps` with the ten operations. `agent_hub/mcp.py`
  becomes a thin FastMCP wrapper over it, so the stdio hub, still used by prepare-run, runs
  the same code.
- The `/rpc` dispatcher adds the orchestrator methods: param validation with the existing
  constraint types, error mapping, and held `wait_for_event`.
- `X-Robomate-Actor` and `X-Robomate-Session` are parsed and stored for the session rule
  (enforced in Step 4, once there is a client that sends them).
- **Before** the move, capture `create_mcp(...).list_tools()` into
  `tests/fixtures/orchestrator-tools.json` for Step 4's golden test.

**Tests** (`tests/test_orchestrator.py`, `tests/test_rpc.py`). A parametrized parity test
drives every operation over `/rpc` and over the in-process MCP tools against the same
`hub_store` and compares the results. It also covers the `wait_for_event` hold and its
timeout, `ack` of an expired delivery, validation errors (a bad SHA, a timeout over 120 s),
an unknown method, and a missing token.

```sh
uv run --locked pytest tests/test_orchestrator.py tests/test_rpc.py tests/test_mcp.py
uv run --locked python scripts/mock-alice.py --help   # unchanged entry point still loads
```

**Size:** small to medium, mostly a mechanical move plus a dispatcher.

### Step 4 — `robomate mcp` bridge (orchestrator and worker modes)

**Scope.**
- `robomate mcp --role …`, with lazy discovery (Step 2's rules), in both modes as designed
  above.
- `worker_mcp/orchestrator.py`: the orchestrator-mode FastMCP server, plus the session ID,
  `hub.heartbeat`, and the retry policy.
- Hub side: the in-memory session table, the "newest session wins" rule, releasing stale
  event leases on a session change (a store method such as
  `HubStore.expire_event_leases()`), and a `hub.heartbeat` handler.
- Worker mode: `WorkerSettings` gains a constructor from a discovered hub, and
  `WorkerSettings.from_env` still works.
- `hub.status` is **not** in this step (Step 6).

**Tests** (`tests/test_orchestrator_bridge.py`, `tests/test_worker_config.py`,
`tests/test_store.py`).
- Golden: the bridge's `list_tools()` equals Step 3's fixture.
- An end-to-end in-process test: a hub on a free port, an orchestrator bridge and a worker
  bridge as MCP clients, `mock-alice`'s one-task drive over the orchestrator bridge.
- **Restart test (the done-when in miniature):** with a worker holding an assigned task and
  a pending `ask_alice`, kill the orchestrator bridge in the middle of a leased
  `wait_for_event` and start a new one. The worker's task state, lease, and heartbeat are
  unaffected, with no `agent_lost` or `lease_expired` event. The new session's first
  `wait_for_event` returns the in-flight event with `delivery_attempts` = 2. The old
  session's next call is refused.
- Discovery failure surfaces as a tool error that lists the hubs.

```sh
uv run --locked pytest tests/test_orchestrator_bridge.py tests/test_worker_config.py tests/test_store.py
# manual: hub from Step 2, then in the same repo
uv run --project <robomate checkout> robomate mcp --role orchestrator   # speaks MCP on stdio
```

**Size:** medium.

### Step 5 — Cut over: hub is HTTP-only; accounting moves to the bridge (#7)

**Scope.**
- Remove stdio MCP from the hub. `agent_hub.main` keeps `serve_http` only.
  `CancellableStdin`, `CancellableStdout`, and `McpConnection` are deleted or moved into the
  bridge if its accounting needs them. The `hub` entry point either becomes HTTP-only
  (for scripts) or is removed; the PR decides and says which.
- `McpAccounting` goes into the orchestrator bridge, with `hub.record_calls` on the hub
  (see [Accounting](#accounting-7)).
- `scripts/prepare-run.py`: stop rendering a hub. Take the hub from an existing
  `robomate up` (`--hub-repo PATH`, reading `.robomate/hub.json` and the token path). Render
  Alice as `robomate mcp --role orchestrator` and the workers as `robomate mcp --role worker`,
  with `ROBOMATE_HUB_URL` and `ROBOMATE_TOKEN_FILE`, under the config key `robomate`.
  Networked mode (`--hub-host`/`--remote-worker`) maps onto `robomate up --bind/--public-url`.
  Update the fixtures in `tests/fixtures/prepare-run-default/`.
- `scripts/mock-alice.py --mcp` and `scripts/measure-call-bytes.py`: start the hub over HTTP
  and talk to it through the orchestrator bridge.
- `runtimes/*`, `prompts/alice.md`, `skills/alice-orchestrator/SKILL.md` (state directory and
  resume wording), `docs/user-guide.md` (quickstart via `robomate up`, `mcp__robomate__…`
  names, accounting on by default), and the AGENTS.md "stdout belongs to MCP" invariant
  (now: the bridge).

**Tests.** `tests/test_main.py` is rewritten for HTTP-only startup and shutdown.
`tests/test_prepare_run.py` checks the rendered bridge configs. `tests/test_accounting.py`
checks that bridge-shipped rows land as `mcp`/`alice` with `content_bytes`.
`tests/test_hub_report.py` checks that Alice's row is non-zero with no "call_log is empty"
note.

```sh
uv run --locked pytest
uv run --locked python scripts/measure-call-bytes.py /absolute/scratch-run-dir
uv run --locked python scripts/mock-alice.py --mcp --agent bob --harness claude-code   # against robomate up + a mock worker
uv run --locked python scripts/prepare-run.py --hub-repo /abs/target --repository … --run-dir /abs/run --issue N
uv run --locked python scripts/hub-report.py --state-dir /abs/target/.robomate
#   -> alice row shows non-zero MCP bytes; no "call_log is empty" note   (#7)
```

**Size:** medium to large, mostly deletions and script ports. If review load is too high, split
it into 5a (bridge accounting and #7) and 5b (stdio removal and script ports). 5a can land
first, because the stdio hub is still there to compare against.

### Step 6 — `robomate status` and `robomate ls`

**Scope.**
- `hub.status` on `/rpc`: repo, origin, forge, URL, and default branch. For each agent: name,
  harness, model, alive or lost, and current task. The orchestrator's name, session, and
  last seen. Workflow status and goal headline. Open tasks, with role, assignee, state, and
  PR/head where known. Pending questions count. Compact by default, `--json` for scripts.
- `robomate status` works from any directory in the repo or its worktrees. When the hub is not
  running it prints "not running", with `hub.json`'s recorded URL and port, and exits non-zero.
- `robomate ls` lists the live hubs from the registry (pruned), one line each.
- "Listening / not listening" (§5) needs M2's hold tracking and is left out. Inbox and
  close-out lines are M4.

**Tests** (`tests/test_cli.py`, `tests/test_rpc.py`): status output against a seeded
`hub_store`, not running, two hubs in `ls`, and stale-entry pruning.

```sh
uv run --locked pytest tests/test_cli.py tests/test_rpc.py
cd /tmp/scratch-repo && uv run --project <robomate checkout> robomate status
uv run --project <robomate checkout> robomate ls
```

**Size:** small to medium.

### Step 7 — M1 acceptance run

**Scope.** Meet the §13 done-when on robomate itself (from M1 on, robomate is built with
itself).

- The operator runs `robomate up` in a robomate checkout. Workers are prepared with Step 5's
  prepare-run. Alice, Bob, and Charlie take one real robomate issue (for example a
  Minor-nits item from #4, or a small M2 precursor) to a merged PR.
- **While a worker holds a task**, the operator kills Alice's harness session and starts it
  again, with the same config and the resume procedure. The run then continues to merge.
- Evidence goes in `docs/evidence/m1-<run-id>.md`, using only the facts that follow.
  - No `agent_lost` or `lease_expired` event during the restart window.
  - No worker task changed state or owner because of the restart.
  - Worker telemetry shows no failed calls during the restart window.
  - The in-flight event was re-delivered (`delivery_attempts`).
  - The merged SHA equals the approved head.
  - `hub-report` shows Alice's MCP bytes and the worker wire bytes (#7).
  - `robomate status` output before and after the restart.
- Record every attempt, including failed ones (poc-lessons).

**Tests.** The run itself. The PR adds only the evidence document and any small fixes the run
exposed. Fixes of more than a few lines get their own issue.

## Risks

| Risk | Mitigation |
|---|---|
| Moving Alice's tools changes their schemas, and so her token cost or behaviour | Step 3 captures a golden fixture before the move; Step 4 asserts equality |
| Stale event leases after an orchestrator crash delay resume by up to 600 s | A new session releases them (Step 4), tested explicitly |
| Two orchestrator sessions interleave (an old harness still running) | The newest session wins; the old one's calls are refused |
| A busy recorded port on restart (another process took 8420) | Refuse with a message; `--port` overrides; never move silently |
| Registry corruption from concurrent `up`s | Lock file and atomic replace; readers prune and tolerate a missing or invalid file by rebuilding it |
| A full-clone worker finds the wrong hub when several are running | Rule 3 fails with a list rather than guessing; `ROBOMATE_HUB_URL` overrides |
| Step 5's breadth (hub stdio removal plus four scripts) | Optional 5a/5b split; `uv run --locked pytest` and every touched script run once (AGENTS.md) |
| Windows: no `SIGTERM`, a different registry path | `down` goes over `/rpc`; `%LOCALAPPDATA%` path; the Ctrl-C path is tested on the Windows host used in PoC Step 7 before Step 7 |
| Bridge-reported accounting rows are self-declared | Same trust model as every other token holder (§12); rows never carry payload text |

## Roadmap bookkeeping

- The M1 "Milestone work" line on #2 links this plan. One issue per step is opened when
  the step is picked up. Step 1 is #6. #7 closes with Step 5.
- **Reservations:** none expected (no DB schema bump, no wire `hub.schema_version` change).
  A step that finds otherwise reserves on #2 first.
