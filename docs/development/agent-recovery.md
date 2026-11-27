# Agent recovery: join takeover and the launcher

Use this guide after an ordinary harness or hub interruption. The deliberate
M1 Step 7 kill and evidence protocol is in [M1 restart and recovery](m1-restart-recovery.md).
There are two kinds of run until M2 Step 11 removes the second:

- **Joined agents** (M2): the operator starts a harness in the agent's worktree
  and it joins with one line, by hand or under the optional
  `scripts/agent-launcher.py`. Recovery is `join` with `takeover=true` and the
  launcher's `resume` and `ask`. See [Joined agents](#joined-agents).
- **Prepared runs**: `prepare-run.py` generated configs and `start-*`,
  `resume-*` and `ask-*` scripts in a run directory. They keep working until
  Step 11; see [Prepared runs](#prepared-runs-until-step-11).

[An agent that stops working but keeps running](#an-agent-that-stops-working-but-keeps-running)
applies to both.

For interactive diagnosis on the hub host, use [Consulting Denise about a
run](../user-guide.md#consulting-denise-about-a-run) and the
[local Denise guide](../denise/guide.md). Denise can open the saved run while
the hub is down. Recovery uses the guarded commands below after the operator
requests it.

## Joined agents

<!-- Join takeover: §5, §7.5. Launcher: M2 Step 10, #247. -->

### Join takeover

A joined agent recovers by joining again under the same name with
`takeover=true` (§5). The new session supersedes the old one at once (its
heartbeats are ignored), and a worker holding a live task gets the task
reattached and returned in the join response, instead of failing it as
`worker_lost`. An orchestrator's takeover fences the old session and releases
its event leases for redelivery; the guide's On resume section reconciles with
the forge before acting. Takeover grants no workflow authority: an escalated
or paused workflow stays as it is.
After joining, the orchestrator calls `get_state` and starts a queued run with
`start_workflow()` without a statement; she keeps the turn going until the
workflow is done unless a required hub tool is unavailable.

By hand, stop the old harness, start the same harness in the same worktree
(`robomate workspace <agent>` prints it) on the saved conversation if you have
it, and ask it to `join` with its name, role and `takeover=true`. Without the
saved conversation, a fresh conversation can join with takeover too: it gets
the task back, but not the context the old conversation held.

### Starting under the launcher

`scripts/agent-launcher.py` is optional and outside the core (§9.4). Run it
from the hub's checkout, or name the hub with `--hub <name | hub_id | checkout>`:

```sh
uv run --project <robomate checkout> python scripts/agent-launcher.py start alice \
    --role orchestrator --harness claude-code
uv run --project <robomate checkout> python scripts/agent-launcher.py start bob \
    --role implementer --harness codex --model MODEL --effort EFFORT [--allow-unsandboxed]
```

`start` creates or reuses the worktree with `robomate workspace <agent>` (the
operator credential), starts the harness there with a first prompt naming the
join, next step, and turn rule, and writes nothing
but `sessions/<agent>.jsonl` (with its `.lock`, and Devin's export) in the
hub's state directory. The harness finds the hub through its static
`robomate mcp` entry and the worktree's binding: there is no rendered config,
prompt file or run directory. An inherited `ROBOMATE_HUB_URL` or
`ROBOMATE_TOKEN*` would override that binding, so the launcher clears them
for every harness it starts. The sandbox grants measured in the
[user guide](../user-guide.md#linux-sandbox-grants) are passed as flags:
Claude Code's sandbox (`--settings` with the forge hosts and a read-deny on
the hub's token directory) and Codex's `workspace-write` with network and the
linked worktree's own git roots plus the uv cache. Other harnesses get no
grants until M3 measures them (§9.2). On Linux, `start` runs the sandbox
preflight for Claude Code and Codex and refuses to launch on failure; passing
`--allow-unsandboxed` overrides a failure for that one agent only, still runs
the verifier, prints a loud warning that the agent will not run with proper
sandboxing and may not work at all (Claude Code keeps `failIfUnavailable: true`
and may stop at startup), and records `sandbox_check: "overridden"` and
`sandbox_check_failure` on its launch rows. The run's permission flags are those of
the prepared runs; extra harness words go after `--harness-args`.

The session log is the operator's data, which agents never read. Each launch,
exit, conversation ID, wait and stop is one JSON line carrying the agent,
role, harness, conversation ID, hub ID, workflow ID and the hub's `run_id`;
launch rows add the model, effort and worktree that `resume` and `ask` reuse,
the launcher's own PID (`launcher_pid`), and the harness process name it saw
at launch (`process`). The launcher prints that PID with every launch, for
example `start (claude-code) pid N, conversation pending; launcher pid M`.
Starting an agent on a different harness keeps the earlier harness's log
beside it as `<agent>.<n>.jsonl`.

### When the harness exits

The launcher supervises as the prepared launchers do: the exact conversation
ID (the one it chose for Claude Code, or the one the harness prints), bearer
reads of `hub.info` and `hub.status` pinned to the hub and the workflow it
first sees, and #158 backoff (`--resume-delay-s`, `--resume-max-delay-s`,
`--resume-total-s`, `--resume-series-reset-s`, `--max-resumes`; the defaults
of [Alice exits before the workflow is done](#alice-exits-before-the-workflow-is-done)).
A run that was already `done` when the launcher started is an earlier run and
is never pinned; a worker released in an earlier run is not treated as
released until its new session is.

Every automatic resume sends one fixed continuation, which carries no state
or decision. Its join instruction matches the conversation (#512): one with
no recorded join is told to call the plain join line, while one that joined is
told to call `join` with its name, role and `takeover=true` for its own seat
only, and to report blocked if that takeover is refused. Then an orchestrator
calls `get_state` and follows On resume, and a worker finishes a reattached
task if there is one or returns to `await_assignment`. The launcher learns
which from the bridge's own `sessions/<agent>-bridge.jsonl` after each child.
Because takeover supersedes a bridge the exited harness left behind and keeps
its task, an automatic resume does not wait for that bridge to become lost, as
the prepared worker launcher does. A refused takeover or an unbound bridge
ends the launcher (exit 4) instead of trying the seat again. The launcher
stops on `done` or the worker's release (0), spent resumes (1), an unreadable
or different hub or workflow (3), nothing safe to resume (4), a pause (5) or
Ctrl-C/SIGINT, SIGHUP or SIGTERM (130), and then names
`agent-launcher.py resume <agent>`. On POSIX a terminal Ctrl-C runs the same
handler as SIGTERM, so the loop records the child's `exit` and the bridge's
`joined` row before the `stop`; the next `resume` therefore needs no
`--confirm-stopped` (#534). Windows keeps the `KeyboardInterrupt` fallback,
which records the child's `exit` row before the stop.

### Per-workflow supervision and the next run

The launcher supervises one workflow (#465, M3 Step 6). It pins the workflow it
first sees, follows it through `done` or a worker's release, then stops with a
message naming a fresh `start` for the next run. `on_done: standby` and a
queued run keep the hub's agent rows joined, but they do not extend
supervision: start each agent again when the next run is active. A fresh
`start` always begins a new conversation and sends only the join line, so an
earlier run's conversation, task or kickoff is never adopted. If the hub moves
to the successor workflow while a child still runs (the pinned run finished and
a queued run was activated), the launcher stops for the pinned workflow and
names the successor instead of adopting it. If run A's child is still live and
holding when the hub activates B, the hub can hand B's work to that same
conversation under standby; stop A's launcher (`agent-launcher.py stop
<agent>`) before starting the fresh B launcher, so supervision and the
conversation both belong to the run being worked (the launcher's lock would
refuse a second concurrent launcher anyway). The `robomate status` done line
and the queue reminder say to stop an attached launcher first. `resume`
continues only the pinned conversation and workflow; a different hub or
workflow is refused before any launch, and `--confirm-stopped` or `--force`
never bypass those identity checks. `robomate status` lists a queued run with
the same per-workflow reminder and prints the "not listening" nudge described
under
[an agent that stops working](#an-agent-that-stops-working-but-keeps-running).

### Resuming by hand

```sh
uv run --project <robomate checkout> python scripts/agent-launcher.py resume bob [--allow-unsandboxed]
```

`resume` takes the harness, role, model, effort and worktree from the newest
launch row; with no saved session it refuses (exit 4) and never replays the
join line over a conversation it does not know. It applies the guards of
[Manual recovery controls](#manual-recovery-controls) unchanged: an exact saved
conversation ID, the same hub, a recorded exit or `--confirm-stopped`, no
surviving recorded PID, a detached predecessor, and pause and open-question
advisories that `--force` overrides. A log written before the workflow
existed takes the workflow from the hub only with #223's evidence. The same
options apply: `--resume-session`, `--hub-id`, `--workflow-id`,
`--stopped-at`, `--confirm-stopped`, `--force`, plus `--allow-unsandboxed` on
Linux. A sandbox override is never inherited from the saved start row: `resume`
re-runs the sandbox preflight for Claude Code and Codex and refuses on failure
unless `--allow-unsandboxed` is passed again explicitly. What recovery captured is
kept for the operator in `sessions/<agent>-resume.md`; the agent receives
only the fixed continuation, never a path into the hub's state. After the
first launch, supervision continues with a fresh backoff budget.

### Stopping by hand

```sh
uv run --project <robomate checkout> python scripts/agent-launcher.py stop bob
```

`stop` is local process control with the same `--hub` / `--sessions-dir`
selection as `resume` and `ask`; it never calls the hub and changes no
workflow or agent state. When a launcher is attached (its OS lock is held),
`stop` sends it SIGTERM, waits boundedly for the launcher's `stop` row, and
reports the rows written. On POSIX the launcher exits 130 through its normal
interrupt path, so the log gains the child's `exit` before the `stop`. When
no launcher is attached but the newest launch has no `exit` after it and its
recorded harness PID still runs, `stop` checks the live process name against
the one recorded at launch, terminates that PID (SIGKILL after the usual
grace on POSIX), and writes an `exit` row marked
`"stopped_by": "agent-launcher stop"`. Each launch row records the offset
where that child's bridge telemetry starts, so `stop` reads only the rows its
own launch appended: when they contain a successful `join` tool call it writes
the launch's `joined` row before the `exit` row, and a following resume keeps
the bounded takeover continuation (#571). An older launch row without that
offset gets no `joined` row, because its telemetry cannot be told apart from
another launch's; the resume then sends the plain join line. When neither is
running it reports that and changes nothing. A PID whose name changed, or an
older launch row with no recorded process identity, may have been reused:
`stop` leaves it alone (exit 4) and says so rather than guessing; the process
name is the check recovery already applies, and #176 tracks a stronger
creation identity.

After `stop` or Ctrl-C, a following `resume` passes the recorded-exit guard
without `--confirm-stopped`. Keep `--confirm-stopped` for real unknowns: a
SIGKILLed launcher with no stop afterwards, a host crash where the harness is
gone too, or a log written before this change. On Windows the launcher cannot
receive a console-safe per-process SIGTERM, so `stop` terminates it and then
terminates its orphaned harness with the same reuse check, recording the
`exit` row on the launcher's behalf. A stop never changes hub state and never
replaces the stopped supervisor: `start` again for the next workflow, and use
`resume` only inside the same one.

### Asking an agent

```sh
uv run --project <robomate checkout> python scripts/agent-launcher.py ask bob [ID] [--fork]
```

`ask` opens the newest saved conversation (or the given exact ID)
interactively in its worktree with its hub tools switched off, and needs no
hub and changes no run file: Claude Code with `--strict-mcp-config` and no
MCP config, Codex with `mcp_servers.robomate.enabled=false`, OpenCode through
`OPENCODE_CONFIG_CONTENT`. AntiGravity and Devin have no tested per-launch
switch for it, so `ask` refuses them rather than leave the hub tools live;
reopen the conversation by hand with the robomate server absent from a
separate profile (see the [adapter matrix](#adapter-matrix)).

### Interactive starts and resumes

`--interactive` (#222) on `start` or `resume` runs the harness in the
terminal with no automatic resume, and still records its start and exit in
the session log. Interactive launches use the run's permission flags;
`--safe-mode` restores the harness's prompting defaults, and the launcher
prints the mode and extra words first (#335). Claude Code, Codex and OpenCode
get the join line as their initial prompt; for AntiGravity and Devin the
launcher prints it for you to type. An interactive `resume` applies the same
guards, opens the conversation, and prints the continuation to send.

An interactive harness owns the terminal, so its output cannot be read for a
conversation ID. The ID is exact for Claude Code, which takes the one the
launcher chooses. For Codex, the launcher looks after the session in Codex's
own session store (`$CODEX_HOME/sessions`) for the single rollout that the
session created: files that existed before the launch never count, even if
appended to meanwhile, and the rollout's `session_meta` line must name the
same ID and this worktree. It records that ID as a `conversation` row.
Elsewhere, and whenever that lookup finds no single candidate, the launcher
says so before and after the session and names the remedy: find the exact ID
in the harness's own session list (`codex resume`, `opencode session list`)
and pass it to `resume <agent> --resume-session ID`, which records it with
`conversation_source: operator` before the usual guards, or to
`ask <agent> ID`. Recovery refuses to guess an ID, and never uses `--last` or
`--continue`.

### Adapter matrix

The joined launcher's contract per harness (#465, M3 Step 6). The `ADAPTERS`
table in `agent-launcher.py` is the checked source; `test_agent_launcher.py`
compares it with each code path. An interactive start for Claude Code, Codex or
OpenCode carries the join line as its first prompt, and a continuation on
resume. For AntiGravity and Devin the launcher prints the line for you to type.

| Harness | Interactive start / resume | Exact-ID capture, or fallback | Hub-free `ask` |
|---|---|---|---|
| Claude Code | Join or continuation is the first prompt; resume opens the exact UUID | The launcher chooses the exact UUID for a new conversation | Supported: `--strict-mcp-config` with no MCP config |
| Codex | Join or continuation is the first prompt; resume opens the exact ID | This launch's new rollout under `CODEX_HOME/sessions`; otherwise the operator's exact ID | Supported: `-c mcp_servers.robomate.enabled=false` |
| OpenCode | Join or continuation is the first prompt; resume opens the exact session | No automatic capture; supply the ID from `opencode session list` | Supported: the robomate server is disabled in `OPENCODE_CONFIG_CONTENT` (2.x proof; 1.x `mcp.robomate` off) |
| AntiGravity | Start prints the join line; resume opens `--conversation ID` | No automatic capture; supply the ID from the harness's session list | **Refused:** no tested per-launch switch disables the effective robomate MCP entry. Reopen the conversation by hand with that server absent from a separate profile/home. |
| Devin CLI | Start prints the join line; resume opens `--resume ID` | Interactive has no automatic capture; a headless launch reads its ATIF export (`--export`) | **Refused:** same limit and separate-profile remedy as AntiGravity. |
| Cline, Devin Local | No launcher adapter: hand-start in the worktree and paste the join or continuation | Use the original IDE conversation | No launcher adapter |

Without automatic capture, `resume <agent> --resume-session ID` records the
operator's exact ID with `conversation_source: operator` before the usual
guards, and `ask <agent> ID` opens it without the hub. Never use `--last` or
`--continue`: they may pick another agent's conversation.

### Remote hosts

For Denise, run `agent-launcher.py doctor` on the hub host and add
`--remote-worker AGENT=HOST` for each known worker host. The consultation marks
remote session and process evidence unavailable there. Use the worker-side
credentials and session directory below to perform guarded resume on the
verified worker host.

On a host without the hub, create the clone with
`robomate workspace <agent> --clone <path> --hub-url <url> --token-file <file>`
(§6), then pass the same hub details, a local sessions directory and the clone:

```sh
uv run --project <robomate checkout> python scripts/agent-launcher.py start bob \
    --role implementer --harness claude-code --hub-url <url> --token-file <file> \
    --sessions-dir <dir> --workdir <clone>
```

`resume` and `ask` on that host take `--hub-url`, `--token-file` and
`--sessions-dir` (`ask` needs only `--sessions-dir`). A clone has no hub
binding, so the launcher hands its bridge the same selection: every harness
it starts or resumes there gets `ROBOMATE_HUB_URL` and `ROBOMATE_TOKEN_FILE`
from these options, and no inherited `ROBOMATE_TOKEN`, which would otherwise
take precedence over the file.

### Denise's doctor entry

```sh
uv run --locked python scripts/agent-launcher.py doctor \
    [--hub /path/to/hub-checkout] [--run RUN_ID] [--harness codex]
```

`--hub`, `--run` and `--harness` are all optional:

- **Hub and run:** when started inside `<state>/runs/<run_id>/` (including
  `denise/workspace`), both the hub (`hub.json`) and run are inferred from that
  path and conflicting `--hub` or `--run` flags are refused. Otherwise `--hub`
  defaults to the current git checkout, and `--run` defaults to the hub's only
  run or its only unfinished run (printing the choice and reason, or listing
  runs when ambiguous).
- **Harness, model, effort and clean login:** explicit flags override the
  effective settings saved in `runs/<run_id>/denise/settings.json` (switching
  `--harness` drops inherited model and effort); if no settings are saved,
  `--harness` is required.

The doctor reads the hub's state location, `runs/<run_id>/manifest.json`, and
local session logs and opens an interactive consultation in `denise/workspace`.
Each consultation writes `context.json` and `startup.md` under
`denise/consultations/<id>/` and a matching `denise/reports/<id>.md`.
Use `--target` for a separate affected checkout. See the
[doctor harness table](../user-guide.md#consulting-denise-about-a-run) for
login and directory access and the [local guide](../denise/guide.md) for
MCP isolation and decisions during recovery.

### Limits

- Supervision is per workflow (#465, M3 Step 6). `on_done: standby` and a queued
  run keep hub rows, but each next run needs a fresh `start`; an earlier run's
  conversation is never reused as its supervisor. There is no persistent
  cross-run supervision and no `robomate supervisor` subcommand in the MVP
  (§9.4).
- Interactive capture and hub-free `ask` follow the [adapter
  matrix](#adapter-matrix); AntiGravity and Devin keep an explicit ask refusal
  until a tested per-launch MCP-off switch exists.
- Grants for OpenCode, AntiGravity and Devin, and all grants on Windows and
  macOS, are not measured; M3's `robomate certify` measures them.
- Codex `-c` values reach a Windows npm `.cmd` shim through `cmd.exe`, which
  may alter quoted words such as the writable-roots list (#287).
- Fake-harness tests cover the launcher; live kills on each harness and the
  doctor's real grants are operator evidence.

## Prepared runs (until Step 11)

Use this guide after an ordinary harness or hub interruption. The deliberate
M1 Step 7 kill and evidence protocol is in [M1 restart and recovery](m1-restart-recovery.md).
Before acting, read `robomate status --json`, Alice's durable `get_state`,
`RUN_DIR/run.json`, and the affected worker's `git status --short --branch`.
The manifest names the state directory, generated config, scripts, and exact
worker workspaces. Verify the old harness process is gone using its recorded
PID and terminal exit before relaunch; two live Alice sessions race, and only
the newest may use RPC. Preserve the generated configuration, the hub's state and token
(database, token, metadata; `robomate status --json` reports where), and all worker workspaces. Do not rerun `prepare-run.py`
or initialize a new workflow in this state directory.

If the hub itself stopped, run `uv run --locked robomate up` from its own
checkout (`hub_repo` in `run.json`), and verify the same hub name, hub ID and
database. Another checkout of the same repository has a hub of its own. A hub started by
`robomate up` is stopped with `robomate down`; leave hubs from other checkouts
alone. If a worker's sandbox cannot see the hub PID or write the registry
lock, use its configured `ROBOMATE_HUB_URL` and `ROBOMATE_TOKEN_FILE` to run
`robomate status --json`; keep the token private. A matching health hub ID and
authenticated status establish reachability. If HTTP fails, check from the
hub host before declaring the hub stopped. Record the harness conversation ID
(see [Resuming a harness conversation](#resuming-a-harness-conversation)),
hub ID, workflow ID, Alice RPC session ID, and task ID as separate fields.

| Interrupted agent/state | Operator and Alice action |
| --- | --- |
| Alice idle, no delivered event | Resume Alice's saved conversation with the same config and an operator prompt to read durable state. Reconcile tasks, PR heads, and checks before new assignments. There is no redelivery proof to claim. |
| Alice active, delivered event | Confirm the old process is gone, then resume. New Alice reads state and redelivery before ack. If the event was already acked, continue ordinary recovery and mark a Step 7 checkpoint failed. |
| Bob or Charlie idle | Relaunch that worker from its generated script/config and workspace. Check in once for the new runtime, then await assignment. Do not assign duplicate work merely because a harness exited. |
| Bob or Charlie with active task | Every auto-started worker's launcher resumes it by itself; act only once it reports its resumes spent. Otherwise preserve branch, uncommitted edits, pending question/result, task ID, and PR head, and resume the worker's saved conversation in the same workspace before the hub declares it lost (below). Failing that, relaunch with the same worker identity; Alice reconciles task state and decides whether the same worker can finish or a fresh task is needed. Never silently overwrite a result or assign another worker to the same branch. |
| Pending `ask_alice` question | Worker retries the **same text on the same task** after a normal timeout, preserving message correlation. Alice replies to the durable question once. A canceled/failed/completed task response is terminal, not an answer. |
| Result submitted before crash | Alice reads the durable typed result and GitHub PR state. Do not submit it again or infer merge from a process exit. |
| Uncommitted worker changes | Inspect `git status`, local diff, and remote branch separately; retain the workspace until the owner decides to commit, discard, or hand off. A new clone cannot reconstruct uncommitted work. |

Alice chooses resume, reassign, or escalate only after comparing durable task
state, GitHub head, and local workspace state. A terminal task cannot be
resumed by merely restarting the worker: assign a fresh task if the same work
is still needed. If the prior harness cannot restore its saved conversation,
or a different worker must take over uncommitted work, escalate with the saved
state and Git status. M1 has no automatic inbox, queue, or takeover contract.

## A worker that exits mid-task

<!-- Headless turn end, launcher and detach: #115. Resume backoff: #158. -->

A headless harness (`claude -p`, `codex exec`, `opencode run`, `agy -p`) exits
when its model ends its turn, even mid-task. When `worker-mcp` stops cleanly it
detaches from the hub: the hub frees that instance and leaves the agent's
status, open task and last heartbeat alone. A new `worker-mcp` for the same
worker can then check in at once and keep the task. If none checks in within
`HUB_LOST_AFTER_S` (180 s by default) of the last heartbeat, the hub declares
the agent lost and fails the task, as before. A `worker-mcp` killed outright
does not detach, so its successor's check-in is refused as a duplicate until
the hub declares the old one lost. The grace is the existing lost window, not
a new timer. It bridges only a resume of the same worker in the same
workspace; takeover by another session waits for M2 join (#18), and host
standby is #19.

`prepare-run.py` now runs every auto-started worker, local or remote, under
`scripts/worker-launcher.py` (#165). Both worker start and hand-resume scripts
use it. Alice and workers share the standard-library `scripts/agent_launcher.py`
for harness commands, exact conversation-ID capture, child supervision, logs
and #158 backoff. Only the first start sends the kickoff prompt. Each automatic
resume sends a fixed continuation with no operator decision or state: check in
once, reconcile the held task, drop a failed/canceled task, and return to
`await_assignment`. Every reply must include a tool call until `release: true`.
Kickoff and hand-resume prompts carry the turn-end rule on all four harnesses;
Alice's hand-resume prompt also says to hold `wait_for_event` while escalated.

The chosen recovery approach is **launcher-side waiting**, rather than relying
on every harness to stop its MCP servers cleanly. Before every worker launch,
the launcher reads bearer-only `hub.info` and `hub.status`, pins the hub and
workflow IDs, and checks the worker's `activity.instance` and `alive` fields.
A missing agent or a detached instance (`activity.instance: null`) can check
in immediately. An attached predecessor must first become lost (`alive: false`)
under the existing hub lost window; its held task then fails and Alice must
assign retry work. The launcher polls every 2 seconds for up to 300 seconds
(`--predecessor-poll-s`, `--predecessor-wait-s`). It also checks for release,
workflow completion and pause while waiting. It changes no hub state and
never takes ownership from a live bridge.

The predecessor wait is independent of `HUB_LOST_AFTER_S`. If the operator
raises the hub's lost window, set `--predecessor-wait-s` above that window
with margin for the sweeper and status reads. `prepare-run.py` does not expose
this tuning: add the launcher flag before `--` in both generated worker start
and resume scripts. For example, a 600-second lost window needs a wait longer
than 600 seconds, such as `--predecessor-wait-s 660`.

Workers retry unreadable hub status up to 60 times, 2 seconds apart by default
(about two minutes for an immediately refused connection), so a brief hub
restart does not immediately end automatic recovery. Alice keeps her 5-read
default. Both accept `--read-retries` and `--read-retry-delay-s`; sustained
outages still stop with exit 3. HTTP request timeouts also count as waiting,
so a server that accepts a connection but fails to respond can take longer.

If a harness leaves an orphan MCP bridge **still heartbeating**, the worker
never becomes lost. At the wait limit the launcher exits 4 with
`predecessor_alive`. The operator must confirm the old harness is gone, stop
its orphan bridge, and use the generated hand-resume script. A clean detach
continues to preserve an open task when the successor checks in before the
lost window expires; waiting for loss cannot preserve that task. These two
paths are covered with fake harnesses and stub status. Real OpenCode and
AntiGravity mid-task kill checks remain operator validation: workers may not
start or stop agents. A harness that hangs without exiting remains #144's
stall case, outside launcher supervision.

Resumes back off from `--resume-delay-s` (default 5 seconds), doubling to
`--resume-max-delay-s` (1800 seconds), then repeating at the cap. A harness run
lasting `--resume-series-reset-s` (300 seconds) resets the series. The budget
starts at its first exit, not while it is running healthily, and lasts
`--resume-total-s` (43200 seconds); `--max-resumes` optionally caps the count
(0 disables resumes). Each launch, exit, conversation ID, wait and stop is
logged to `RUN_DIR/<name>-sessions.jsonl`; `run.json` records the path and
backoff settings. Waits include `delay_s` and `next_attempt`. The launcher stops
on release or workflow `done` (exit 0), spent resumes (1), unreadable or changed
hub/workflow (3), missing workflow/ID or an attached predecessor (4), pause
(5), a repeated non-retryable provider error (7), a long-run failure loop (8),
or Ctrl-C/SIGHUP/SIGTERM (130). On interruption it stops only its own
harness child and records its exit row before stopping (#534). It checks
status again after a backoff before launching a successor.

A run that lasts `--resume-series-reset-s` and then exits non-zero counts
toward a stuck-loop signal (#169); the first one counts even though the
launcher writes no `series_reset` row for it, so the thresholds count these
runs, not reset rows (6 runs from a fresh start is 5 reset rows). A worker that
lasts `--resume-series-reset-s` and then fails on every cycle
refills the time budget and would otherwise be resumed forever. After
`--warn-series-resets` (default 3) such runs in a row, the launcher logs a
`series_reset_warning` line and prints a warning on stderr; after
`--max-series-resets` (default 6) it stops with reason `series_resets_spent` and
exit 8, printing the error and the `resume-<name>` instructions. A run shorter
than the reset interval, or a long run that exits 0, clears the count, so
ordinary crashes and a worker making progress in long runs keep today's
backoff and series-reset behaviour. `--max-series-resets 0` keeps the warning
only, `--warn-series-resets 0` keeps the cap only, and `--max-resumes` still
wins when it is reached first.

When automatic attempts are spent, preserve the workspace and pending task,
question/result, verify the old harness is gone, then run the generated script
on that agent's host with no arguments:

```sh
"$RUN_DIR/resume-bob.sh"                      # or resume-charlie.sh
# Windows: & "$RUN_DIR/resume-bob.ps1"
```

The script discovers the exact conversation ID in `<name>-sessions.jsonl`,
checks the saved run identity, captures a current read-only hub snapshot, and
writes `resume-<name>.prompt.md` automatically. See
[Manual recovery controls](#manual-recovery-controls) for recommendations and
fallbacks when prerequisites are missing.

The generated resume repeats the start environment, config, working directory,
model and effort. It starts the exact saved conversation with the manual prompt
once, then resumes automatically with the fixed continuation and a fresh
backoff budget. Never rerun `start-<name>` for recovery. Runs prepared before
#165 retain their old scripts; this change does not rewrite a running manifest.
Their existing direct hand-resume scripts still work, but old Claude worker
start scripts refer to the replaced `claude-worker.py`. To adopt supervision
without rerunning preparation, the operator can adapt the existing command to
`worker-launcher.py` with `--agent`, `--harness`, `--hub-url`, `--token-file`,
`--telemetry`, `--sessions`, `--resume-session` and `--resume-prompt`, then `--`
and the unchanged harness flags (omit its old prompt/session flags).

### Agent and harness coverage after #173

All auto-started pairs below have kickoff and hand-resume turn-end rules,
automatic backoff, and a generated `resume-<name>.sh`/`.ps1` for hand recovery.
`--no-auto-start` remains interactive and has no automatic launcher.

| Agent | Harness | Automatic launcher | Conversation ID location | Hand resume |
| --- | --- | --- | --- | --- |
| Alice | Claude Code | `alice-launcher.py` | `alice-sessions.jsonl`, `conversation_id` (UUID) | `resume-alice` |
| Alice | Codex | `alice-launcher.py` | `alice-sessions.jsonl`, `conversation_id` (UUID) | `resume-alice` |
| Alice | OpenCode | `alice-launcher.py` | `alice-sessions.jsonl`, `conversation_id` (`ses_…`) | `resume-alice` |
| Alice | AntiGravity | `alice-launcher.py` | `alice-sessions.jsonl`, `conversation_id` (stream-json ID) | `resume-alice` |
| Bob | Claude Code | `worker-launcher.py` | `bob-sessions.jsonl`, `conversation_id` (UUID) | `resume-bob` |
| Bob | Codex | `worker-launcher.py` | `bob-sessions.jsonl`, `conversation_id` (UUID) | `resume-bob` |
| Bob | OpenCode | `worker-launcher.py` | `bob-sessions.jsonl`, `conversation_id` (`ses_…`) | `resume-bob` |
| Bob | AntiGravity | `worker-launcher.py` | `bob-sessions.jsonl`, `conversation_id` (stream-json ID) | `resume-bob` |
| Charlie | Claude Code | `worker-launcher.py` | `charlie-sessions.jsonl`, `conversation_id` (UUID) | `resume-charlie` |
| Charlie | Codex | `worker-launcher.py` | `charlie-sessions.jsonl`, `conversation_id` (UUID) | `resume-charlie` |
| Charlie | OpenCode | `worker-launcher.py` | `charlie-sessions.jsonl`, `conversation_id` (`ses_…`) | `resume-charlie` |
| Charlie | AntiGravity | `worker-launcher.py` | `charlie-sessions.jsonl`, `conversation_id` (stream-json ID) | `resume-charlie` |

These files live in the run directory on the host running that agent; remote
workers use their worker-host run directory. IDs are distinct from the hub ID,
workflow ID, task ID and Alice RPC session ID. OpenCode's `ses_…` is never the
robomate session ID. A separate nudge loop is no longer needed for exited
harnesses; the bounded launcher provides it.

## Alice exits before the workflow is done

<!-- Orchestrator launcher: #146. Resume backoff: #158. -->

An auto-started Alice runs under `scripts/alice-launcher.py`, written into
`start-alice` and `resume-alice` (`.sh`, or `.ps1` on Windows) for Claude Code,
Codex, OpenCode and AntiGravity. `--no-auto-start` keeps her interactive and
unsupervised. The launcher:

- records her exact conversation ID in `RUN_DIR/alice-sessions.jsonl`: the
  `--session-id` it chose for Claude Code, or the one the harness prints
  (Codex `session id:`, OpenCode `sessionID` from `--format json`,
  AntiGravity's conversation ID from `--output-format stream-json`). It never
  resumes a "latest" conversation.
- after each exit, reads the run hub named by `--hub-url` and `--token-file`
  with bearer-only `hub.info` and `hub.status`. It pins the hub ID and workflow
  ID it first sees and never opens an orchestrator session, so it cannot
  supersede Alice, and it never reads `.robomate/`.
- on `done`, exits 0 without another launch. On `active` or `escalated`, backs
  off and resumes the same conversation with one fixed continuation prompt:
  call `join` with the configured name, `role="orchestrator"` and `takeover=true`
  first to replace the prior Alice bridge, then call `get_state` and reconcile
  with the returned orchestrator guide. Keep escalation and pause; do not repeat
  work. The manual `resume-alice` prompt uses the same join/read order. These
  prompts carry no state or operator decision, and session replacement grants
  no workflow authority.
  The wait doubles from `--resume-delay-s` (default 5 s) up to
  `--resume-max-delay-s` (default 1800 s, about 30 minutes), then repeats at
  the cap; a run lasting `--resume-series-reset-s` (default 300 s) starts a new
  series at the short delay. Resumes stop after `--resume-total-s` (default
  43200 s, about 12 hours) since the first exit of the current series, or after
  `--max-resumes` when given (by default no count limit; 0 disables them),
  then exits 1 with these manual steps. Each wait is logged as a `wait` line
  with `delay_s` and `next_attempt`, and stderr says when the next resume
  goes. Healthy running time before that first exit never spends the budget,
  and a series reset refills it. A long run that then fails on every cycle also
  counts toward #169's stuck-loop signal, by run: the first long failing run
  counts even though it writes no `series_reset` row, so `--max-series-resets`
  counts runs, not reset rows. After `--warn-series-resets` (default
  3) in a row the launcher logs `series_reset_warning` and warns on stderr,
  and after `--max-series-resets` (default 6) it stops with reason
  `series_resets_spent` and exit 8, printing the manual resume steps. A
  shorter run or a long run that exits 0 clears the count, so ordinary crashes
  and healthy long runs keep resuming; 0 disables either signal. The hub is
  re-read after the wait, before
  every resume, so a pause, `done`, or a different hub or workflow during a
  long wait still stops the launcher.
- stops without launching again, and says why, when the workflow is `paused`
  (exit 5: resume by hand when you are ready), when Alice exited before
  initializing the workflow or before printing her conversation ID (exit 4: it
  never replays the kickoff or guesses an ID), when the hub stays unreadable
  or names another hub or workflow after `--read-retries` reads (exit 3: it
  never guesses `done`), when the same non-retryable provider error repeats
  (exit 7), or when long failing runs reach `--max-series-resets` (exit 8).
- on Ctrl-C, SIGHUP or SIGTERM, stops only the harness process it started,
  launches nothing more, and exits 130. The child's `exit` row is written
  before the stop, so a later resume needs no `--confirm-stopped`; a long
  backoff wait is interruptible too.

`prepare-run.py --max-resumes N --resume-delay-s S --resume-max-delay-s MAX
--resume-total-s TOTAL --resume-series-reset-s RESET` sets the backoff for the
run, passing the same settings to supervised workers and to Alice, and
`run.json` records them under `launch.agents.alice` and each supervised
worker's `launch.agents.<worker>`. The #169 warning and cap keep their defaults
through preparation; to change them, add `--warn-series-resets N` and
`--max-series-resets N` before `--` in the generated start and resume scripts
(0 disables either). Every launch, wait, exit, conversation ID
and stop reason is one line in `alice-sessions.jsonl`; the token and command
lines are never logged.

When the launcher has stopped, resume the saved conversation:

```sh
"$RUN_DIR/resume-alice.sh"                    # resume-alice.ps1 on Windows
```

The script captures the before-snapshot and constructs her prompt automatically.
Alice can resume while an operator question is pending, so she can receive its
answer and reconcile. Escalation stays in effect until the actual operator
answer arrives.

`resume-alice` runs the same launcher with `--recover`: its first launch resumes
the saved conversation with the generated factual prompt. Later exits use a
fresh automatic resume budget and the fixed continuation.
It refuses to launch when the workflow is already done. Only `start-alice`
sends the kickoff prompt; never rerun it to resume.

The launcher watches only for an exit. A harness that keeps running without
working is #144's stall warning below, and stopping it stays with you.

## An agent that stops working but keeps running

<!-- Stall detection: #144. -->

The bridge heartbeat proves only that `robomate mcp` is running. A harness
whose model is rate-limited or hung, or that ended its turn without exiting,
keeps it running. The hub therefore tracks substantive hub calls, task
progress and held calls separately, and `robomate status` marks an agent
`STALLED` when, for longer than the workflow policy's `stall_after_min`
(default 20 minutes):

- it made no substantive hub call and holds none (`no_hub_call`);
- a worker's assigned task has had no message, progress, question or result
  since its assignment (`no_task_progress`);
- an event for Alice has never been delivered (`event_backlog`).

Set `stall_after_min = 30` in the standard-run TOML, or pass
`scripts/prep-standard-run.sh --config run-config.toml --stall-after-min 30`
(PowerShell: `scripts/prep-standard-run.ps1` with the same arguments). CLI
values override the file; positive fractional minutes are accepted. The helper
forwards this to the existing `prepare-run.py --stall-after-min` option and
records it in the run policy and Alice's prompt. For the M2 submission path,
set `stall_after_min` in the user, repository or hub configuration layer, or
override it per run with `robomate submit statement.md --policy stall_after_min=30`.
The #247/#268 launcher and setup successors must retain these policy settings.

An optional second threshold, `stall_action_after_min`, defaults to `0`
(disabled). Enable it with a finite number greater than `stall_after_min`,
through the same TOML/configuration layers or `--stall-action-after-min` in
both preparation scripts; `robomate submit --policy stall_action_after_min=60`
sets it per submitted run. Both thresholds measure absolute minutes of eligible
inactivity. For example, `stall_after_min = 20` and `stall_action_after_min = 60`
report a diagnostic at 20 minutes and ask the operator at 60 minutes, even if
the first sweep was late. Each reason must independently reach the second
threshold; the age of the episode alone cannot trigger it.

The second threshold opens one durable operator question per worker episode,
escalates the workflow, and sends Alice a `stall_action` event with the question,
worker instance, task, reasons and ages. Alice checks PR comments, worker state
and the manual recovery guidance. This never stops or restarts a process,
changes task ownership, shortens a lease or grants authority. When the worker
recovers (`cleared_reason: recovered`), the hub withdraws its own unanswered
question and notifies Alice again; it preserves operator answers and unrelated
questions. Loss, release, removal, replacement and workflow changes leave the
question open for the operator because they do not confirm recovery. Withdrawal grants no
authority and does not automatically resume the workflow. Alice follows the
normal escalation resolution checks. Episode/question/event writes are atomic
and the durable episode link prevents duplicate actions across sweeps/restarts.

Each line names the ages, the bridge heartbeat's age, and the task or event
IDs. `robomate status --json` carries the same under each agent's `activity`
and `orchestrator_activity`; `scripts/hub-report.py` lists every stall episode.

`status` also shows an agent's last hub call, last progress
(`HH:MM:SS (N m ago)`) and the path of its harness session log
(`sessions/<agent>.jsonl` in the hub's state directory) below its line while
the agent is still healthy, and the headline's `--activity` view condenses one
line per agent for `watch`. Use the log path to read the harness's own log
before acting; a long non-hub command still explains a silence without being a
provider fault, and the stall evidence keeps its own wording.

Before `STALLED`, a live agent with no task, no hold and no substantive hub
call for the hub's `idle_after_s` (120 s) is **not listening**: its harness
ended the turn. `robomate status` prints that agent's last hub call and the
remedy: send `Continue.` in the live session, or, when its CLI already exited,
resume the exact saved conversation with `agent-launcher.py resume <agent>`
(takeover only for a conversation with a recorded join). A healthy hold, an
open task, a released agent, a lost bridge, a paused or escalated workflow and
a done or queued run print their own line instead, so a worker stalled with
open work keeps its stall evidence rather than showing a nudge. The hint is an
operator prompt; it grants no task or workflow authority and changes no state.
A held `await_assignment`, `ask_alice` or `wait_for_event` is never a stall,
however long the waiting goes on; an open operator question is not by itself
an exemption, because Alice should be holding `wait_for_event` for its answer.
A long non-hub command (a test suite, a CI watch) can also trigger the
warning: it is not proof of a provider fault. `lost` is different: the
heartbeat itself stopped (`HUB_LOST_AFTER_S`, 180 s). The first stall changes nothing:
no task fails, nothing is reassigned, and no process is stopped. A worker's
stall reaches Alice as one `agent_stalled` event per episode; Alice's own
stall reaches only you, through status and the report. Done workflows and
released or lost agents are never reported; a paused workflow suspends the
check, and resuming it restarts every clock.

The orchestrator line keeps Alice's activity and her bridge apart (#95), for
example `Orchestrator: alice  finished  last call 22m 0s ago
(set_workflow_status)  (bridge connected, workflow done)`. Her state is
`finished` once the workflow is done, which is her end-of-run state, as
`released` is a worker's; `paused` while it is paused; `waiting in <call>`
while a call of hers is held; `active` within two minutes of her last call;
otherwise `idle`, with the open-task count when there is work. The bridge is
`connected` when the hub heard from it, heartbeats included, within 90 s. A
heartbeat never counts as a call, so a finished run's idle Alice no longer
looks freshly seen. Her last call is kept in the database, so after a hub
restart status still shows it, marked `before hub restart`; it only informs
the display, and the stall check still measures silence from the new hub's
start. `--json` adds `state`, `bridge`, `bridge_age_s`, `last_call_at`,
`last_call_actor` and `last_call_restored` to `orchestrator_activity` and
leaves `orchestrator.last_seen`, which is bridge liveness, as it was.

The threshold relates to the other limits like this: `HUB_LOST_AFTER_S` (180 s)
catches a bridge that stopped; `stall_after_min` catches a live bridge whose
harness stopped working; `max_task_lease_min` (120 minutes) caps one task
however busy its worker is. Calls and holds are held in memory: a restarted
hub knows no holds and measures silence and event backlog from its own start,
so it reports `no_hub_call` or `event_backlog` no sooner than one threshold
after a restart. Task progress is durable (the task's messages), so a task
already stale reports `no_task_progress` at once; an episode that still holds
continues without a second event.

For a stalled agent, read its harness's own log before acting. **OpenCode on a
provider rate limit** neither retries nor exits: `opencode run` idles with its
last assistant message unfinished, and its log
(`~/.local/share/opencode/log/opencode.log`) records `AI_APICallError: Rate
limit exceeded`. A resume can hit the same limit at once (#144's run
`la005-issue-132`). Wait for the limit to clear, stop that `opencode` process,
and resume its conversation; under the launcher, stopping the agent's `opencode`
child triggers automatic recovery, subject to predecessor and backoff limits.

## Resuming a harness conversation

To keep an agent's context, resume its saved conversation rather than rerun
its start script. A start script sends the original kickoff prompt: Alice
would try to initialize a new workflow, and a worker would start a fresh
conversation that knows nothing of its task. Run the
resume from the agent's working directory, the `cd` line of its start script,
with the script's environment lines: `CODEX_HOME` or `OPENCODE_CONFIG`, and
`TMPDIR` (`TEMP` and `TMP` on Windows). Use the same model and effort flags. Give
the agent a new prompt that says what happened and what it must reconcile.
Write it to a file such as `RUN_DIR/resume-bob.prompt.md` and pass the one-line
instruction `Read <file> and follow it`, as the start scripts do. A resumed
worker runs a new `worker-mcp`, so the prompt should tell it to call
`check_in` once before anything else.

| Harness | Conversation ID and where to find it |
| --- | --- |
| Claude Code | A UUID. For any supervised agent, take `conversation_id` from `RUN_DIR/<name>-sessions.jsonl`. Legacy #115 worker logs use `session_id`. Otherwise it is the file name of `~/.claude/projects/<dir>/<uuid>.jsonl`, where `<dir>` is the working directory with every character other than a letter or digit replaced by `-`; pick by modification time. |
| Codex | A UUID from `RUN_DIR/<name>-sessions.jsonl`, CLI `session id:`/JSON `thread_id`, or the configured `CODEX_HOME` session store; the exact `codex exec resume` form is in [M1 restart and recovery](m1-restart-recovery.md). |
| Alice (any harness) | Under `scripts/alice-launcher.py`, the `conversation_id` in `RUN_DIR/alice-sessions.jsonl`. |
| OpenCode | A `ses_...` ID. `prepare-run.py` starts every OpenCode agent with `--title "<agent> <run-slug>"` and records that title in `run.json` `launch.agents.<agent>.title`, so run `opencode session list --format json` from the working directory and match the title; `opencode export <id>` prints a transcript to confirm. The session database is `~/.local/share/opencode/opencode.db` on the agent host; never substitute a robomate session ID. Runs prepared before the title change all read `New session - <ISO time>`, so match by time instead (#94). |
| AntiGravity | `conversation_id` from the launcher sessions log, captured from `--output-format stream-json`; if missing, inspect the captured stream output for the conversation/session ID. Never guess a latest conversation. |

Claude Code takes the flags from the start script's `claude` command, with
`--resume` in place of `--session-id` and a new `-p`:

```sh
cd "$WORKSPACE"                      # the start script's cd line
export TMPDIR="$RUN_DIR/tmp/bob"     # Windows: TEMP and TMP, as the script sets them
claude --model MODEL --effort EFFORT --permission-mode auto --strict-mcp-config \
  --mcp-config "$RUN_DIR/configs/bob.mcp.json" --add-dir "$RUN_DIR" \
  --resume SESSION_UUID -p "Read $RUN_DIR/resume-bob.prompt.md and follow it"
```

OpenCode, shown for Alice (#94):

```sh
cd "$RUN_DIR/alice-runtime"
OPENCODE_CONFIG="$RUN_DIR/configs/alice.opencode.json" TMPDIR="$RUN_DIR/tmp/alice" \
  opencode run --auto --model MODEL --variant EFFORT --title "alice <run-slug>" \
    --session SES_ID \
  "Read $RUN_DIR/resume-alice.prompt.md and follow it"
```

Prefer `RUN_DIR/resume-<agent>.sh` (`.ps1` on Windows) over a hand-built
command: it preserves directory, environment, configuration/home, model and
effort, discovers the saved conversation, and captures its recovery prompt.
Do not rerun preparation or the kickoff script to recover an existing run.
For runs created before #173, retain their original configuration and use the
manual command above with a reviewed before-snapshot, or adapt their launcher
command to `--recover --resume-prompt PATH` with the same run/session paths.

### Manual recovery controls

The ordinary command is `"$RUN_DIR/resume-alice.sh"`, `resume-bob.sh`, or
`resume-charlie.sh`, with no arguments. Optional controls are forwarded to the
shared recovery helper (the same options work after a PowerShell script path):

```sh
"$RUN_DIR/resume-bob.sh" --resume-session EXACT_ID
"$RUN_DIR/resume-bob.sh" EXACT_ID             # positional shorthand retained
"$RUN_DIR/resume-alice.sh" --stopped-at 2026-10-08T12:34:56Z
"$RUN_DIR/resume-alice.sh" --force            # reconcile while keeping a pause
"$RUN_DIR/resume-bob.sh" --force              # reconcile despite an open operator question
```

Essential checks cannot be overridden. A missing, ambiguous, invalid, or
conflicting conversation ID requires inspection of the harness's session list
in the original home/configuration, and an exact explicit ID. Known IDs may
only be selected from this run's log. When no workflow ID is saved and none
is supplied, recovery takes it from the run hub (#223) only when all of these
hold: (1) the same hub ID, (2) exactly one candidate workflow matched by
run_id where available (an earlier done or queued workflow is never picked),
(3) the workflow's `created` time falls after the conversation's first start
and before its last exit/stop (open when no exit was recorded), and (4)
hub-side evidence where available (a worker's agent row or assigned task, or
Alice's orchestrator session; timing alone still accepts but says so). The
prompt and output name the ID and say it came from the hub, and the relaunched
launcher pins it in later rows. Any failed condition keeps the refusal and
names the condition. Otherwise a missing hub/workflow pin still requires
independent verification and `--hub-id HUB_ID --workflow-id WORKFLOW_ID`.
Explicit pins must agree with saved pins and the live snapshot. A changed or
unreachable hub, absent workflow, missing configuration, changed workspace,
completed workflow, or released worker refuses launch. Restore the original
files/address/state, or ask Alice/operator whether separate fresh work is
needed. Never initialize another workflow to make recovery pass.

Timestamps tolerate a backward wall clock (#209): creation within 300 seconds
outside the strict conversation window still accepts when hub-side evidence
names the agent, and the prompt notes the tolerance. Launchers also pin a
workflow that appears while the child runs onto the exit row when the hub is
readable, narrowing the window that needs the hub path.

Start and resume launchers share an OS lock for the lifetime of the process.
An existing launcher, a recorded predecessor PID still present without an
exit, or an attached worker bridge refuses launch. Alice also refuses while
her old RPC session has a live hold or a heartbeat less than 60 seconds old.
A clean bridge shutdown detaches its session, ends its held calls and clears
its heartbeat immediately. A dropped HTTP connection ends the affected call;
the last heartbeat can still keep the guard active until it ages out. If the
hub cannot see the disconnect, the hold expires after its deadline plus 30
seconds. Wait for detach or expiry, or have the operator inspect and stop its
orphan bridge, then retry. No option bypasses these guards. Workers never stop
predecessors.
For a launcher started by `agent-launcher.py`, stop it with `stop <agent>` or
Ctrl-C; both record the child's `exit`, so the next resume needs no
`--confirm-stopped` (#534). When no exit was recorded (an older log, a
SIGKILLed launcher with no stop afterwards, or a host crash/interactive run
where the harness is also gone), the operator must verify both old harness and
launcher are stopped and add `--confirm-stopped`. That confirmation supplies no
timestamp and cannot bypass a known attachment.

A recorded PID may have been reused after a reboot or a hard launcher exit.
The refusal shows the current process name when available; that name alone
does not prove identity. Check its executable and start time, and stop it only
if it is the old harness. If it is unrelated and both old harness and launcher
are confirmed stopped, leave it alone, preserve the stale sessions log by
moving it aside, and use the explicit fallback below with the verified original
conversation, hub and workflow IDs. The launcher lock and hub attachment checks
still apply.

Paused workflows and workers with open operator questions are advisory waits:
the script names the question IDs and recommends `robomate answer`, or asks
the operator to resume the paused workflow. `--force` permits reconciliation
while preserving pause/escalation. It never answers questions, changes workflow
status, grants authority, bypasses identity checks, or launches an attached
predecessor. Alice's pending operator questions are allowed without `--force`;
a worker's own pending `ask_alice` question can also be retried normally.

The snapshot's `taken_at` records capture time. Process stop time comes from
the recorded harness exit (`exited_at`, or an older exit record's timestamp),
never a budget/stop record or the snapshot time. A timezone-qualified
`--stopped-at` can supply known timing; future or malformed times refuse.
Otherwise an unrecorded stop is explicitly **unknown**. Recovery appends the
snapshot/stop metadata to the sessions log, and replaces the previous captured
facts in the prompt. Hub text is marked as data, never instructions or approval.

If logs are lost/corrupt, restore them where possible. After inspection, move a
corrupt log aside rather than guessing from a partial record. With no saved
metadata, the explicit fallback is:

```sh
"$RUN_DIR/resume-bob.sh" --resume-session EXACT_ID --hub-id HUB_ID \
  --workflow-id WORKFLOW_ID --confirm-stopped
```

If the prompt or configuration is missing, restore the original run artifacts.
If the manifest is absent, existing scripts and valid session pins can still
recover; no new configuration is created. If scripts are also missing, retain
the original model/config/workspace and use the documented per-harness resume
forms after independent verification and a current snapshot. Do not replay
kickoff or use a different conversation.

`--no-auto-start` keeps interactive mode. The generated script applies the same
guards and writes the prompt, then opens the original interactive CLI. Because
interactive starts have no session capture, normally supply the explicit
fallback above and read the printed prompt path in the resumed conversation
before action. All four harnesses keep their original permission/model flags.
Local scripts use Bash or native PowerShell; remote worker bundles use their
host's Bash (Git Bash on Windows), native interpreter, token path, and workspace.

Reviewer-runnable commands (fake CLIs and isolated HTTP stubs; no run hub):

```sh
uv run --locked pytest tests/test_prepare_run.py::test_start_and_resume_scripts_run_the_launcher_to_done -q
uv run --locked pytest tests/test_agent_recovery.py -q
uv run --locked pytest tests/test_prepare_run.py -k 'interactive_recovery or recovery_controls or remote_resume' -q
uv run --locked pytest tests/test_alice_launcher.py tests/test_worker_launcher.py -q
```

The first executes zero-argument generated resumes for all 12 supervised
agent/harness pairs and verifies configuration, workspace, model, effort and
exact conversation. The second demonstrates identity/attachment refusals,
recommendations, explicit fallback/timing controls, timestamps and permitted
advisory overrides. The third exercises optional controls and interactive and
remote wrappers. CI runs these on Linux and native Windows; path-rendering
checks additionally cover remote Windows Git Bash. Real harness crash/resume,
remote-host connectivity and orphan-bridge kill checks remain operator live
validation; these tests do not start or stop any run agent.

Never use `claude --continue` or `opencode --continue`: each picks the most
recent conversation, which may be another agent's.

### Stopping a resumed OpenCode run

A resumed `opencode run` may never exit on its own after the agent's final
message, even with the workflow `done` and no pending tool call: the process
idles indefinitely while still holding its MCP bridge, whose heartbeat keeps
`robomate status` showing a live orchestrator. The suspected cause is an
upstream `opencode run` lifecycle bug (the session loop failing to treat the
final response as terminal, as in publicly reported `opencode run` hangs after
the last tool call) — that mechanism is unconfirmed. What is established is
only that the workflow state in the database is already final, so on current
evidence this is an idle harness process, not a hub defect. Once `get_state`
(or `robomate status --json`) shows the workflow `done` and the agent was
released, stop the leftover `opencode run` with Ctrl-C or a targeted signal
and confirm the process exits.
If it recurs, record the `opencode --version`, model/provider, and a
`--print-logs --log-level DEBUG` capture for an upstream report; do not leave
the idle process running as if the workflow were still active (#94).

The disposable CLI up/down test covers hub identity and port reuse;
`tests/test_orchestrator_bridge.py::test_worker_task_survives_orchestrator_session_restart`
covers active task, pending question retry, and event redelivery;
`tests/test_mock_alice.py` covers simulated delivery/ack crash paths;
`tests/test_worker_client.py::test_a_restarted_worker_checks_in_at_once_and_finishes_its_task`
covers the detach, and `tests/test_worker_launcher.py` the four-harness worker
resumes and predecessor waits. `tests/test_agent_launcher.py` covers the joined
launcher: the join line and grants on every harness, a mid-task kill resumed
with takeover, guarded `resume`, `ask`, `--interactive` and the doctor entry. `tests/test_prepare_run.py` executes generated
start and resume scripts for every agent/harness pair. These
fixtures do not prove a real operator kill, Codex conversation resumption,
cross-host worker relaunch, or PR merge sequencing. [M2 join/takeover](../mvp-spec.md)
must define safe new-session ownership and reassignment before this becomes
automatic. [#18](https://github.com/RoboNater/robomate/issues/18) should
reconcile pending questions, results, and uncommitted work on resume;
[#19](https://github.com/RoboNater/robomate/issues/19) must distinguish host
standby from worker loss. Acceptance for those issues should include idle and
active crash fixtures and a live cross-host resume with durable identity,
single ownership, and no duplicate PR action.
