# Agent recovery: join takeover and the launcher

Use this guide after a harness or hub interruption. Agent recovery uses the
same workspace and conversation, and rejoins with `takeover=true` when that
conversation already joined. The optional launcher supplies guarded `resume`
and `ask` commands.

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
the run; extra harness words go after `--harness-args`.

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

The launcher supervises the exact conversation
ID (the one it chose for Claude Code, or the one the harness prints), bearer
reads of `hub.info` and `hub.status` pinned to the hub and the workflow it
first sees, and #158 backoff (`--resume-delay-s`, `--resume-max-delay-s`,
`--resume-total-s`, `--resume-series-reset-s`, `--max-resumes`; the defaults
are shared with `start-run.py`).
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
a manual resume does. A refused takeover or an unbound bridge
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
join line over a conversation it does not know. The recovery guards require an exact saved
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

Set `stall_after_min` in the run TOML's `[policy]`, in the configuration
layers, or for one submission with
`robomate submit statement.md --policy stall_after_min=30`.

An optional `stall_action_after_min` threshold defaults to `0` (disabled).
Enable it with a finite number greater than `stall_after_min` in the same
configuration or with `--policy stall_action_after_min=60`. Each reason
must independently reach the second threshold.

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
A held `await_assignment`, `ask_orchestrator` or `wait_for_event` is never a stall,
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

## Stopping a resumed OpenCode run

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

The CLI up/down test covers hub identity and port reuse.
`tests/test_orchestrator_bridge.py` covers an active task, pending question
retry and event redelivery. `tests/test_agent_launcher.py` covers the
launcher, including guarded resume, `ask`, interactive launch and doctor.
The live doctor evidence covers the operator's separate tooling checkout,
recovering a stopped hub and a remote-worker resume.
[M7 operator acceptance](../evidence/m7-20261127.md#guidance-for-248)
