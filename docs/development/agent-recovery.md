# Alice, Bob, and Charlie recovery

Use this guide after an ordinary harness or hub interruption. The deliberate
M1 Step 7 kill and evidence protocol is in [M1 restart and recovery](m1-restart-recovery.md).
Before acting, read `robomate status --json`, Alice's durable `get_state`,
`RUN_DIR/run.json`, and the affected worker's `git status --short --branch`.
The manifest names the state directory, generated config, scripts, and exact
worker workspaces. Verify the old harness process is gone using its recorded
PID and terminal exit before relaunch; two live Alice sessions race, and only
the newest may use RPC. Preserve the generated configuration, `.robomate/`
database/token/metadata, and all worker workspaces. Do not rerun `prepare-run.py`
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
(5), or Ctrl-C/SIGTERM (130). On interruption it stops only its own harness
child. It checks status again after a backoff before launching a successor.

When automatic attempts are spent, preserve the workspace and pending task,
question/result, verify the old harness is gone, and take the operator's
before-snapshot. Fill in `RUN_DIR/resume-<name>.prompt.md`, then run:

```sh
"$RUN_DIR/resume-bob.sh" CONVERSATION_ID       # or resume-charlie.sh
# Windows: & "$RUN_DIR/resume-bob.ps1" CONVERSATION_ID
```

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

### Agent and harness coverage after #165

All auto-started pairs below have kickoff and hand-resume turn-end rules,
automatic backoff, and a generated `resume-<name>.sh`/`.ps1` for hand recovery.
`--no-auto-start` remains interactive and has no automatic launcher.

| Agent | Harness | Automatic launcher | Conversation ID location | Hand resume |
| --- | --- | --- | --- | --- |
| Alice | Claude Code | `alice-launcher.py` | `alice-sessions.jsonl`, `conversation_id` (UUID) | `resume-alice ID` |
| Alice | Codex | `alice-launcher.py` | `alice-sessions.jsonl`, `conversation_id` (UUID) | `resume-alice ID` |
| Alice | OpenCode | `alice-launcher.py` | `alice-sessions.jsonl`, `conversation_id` (`ses_…`) | `resume-alice ID` |
| Alice | AntiGravity | `alice-launcher.py` | `alice-sessions.jsonl`, `conversation_id` (stream-json ID) | `resume-alice ID` |
| Bob | Claude Code | `worker-launcher.py` | `bob-sessions.jsonl`, `conversation_id` (UUID) | `resume-bob ID` |
| Bob | Codex | `worker-launcher.py` | `bob-sessions.jsonl`, `conversation_id` (UUID) | `resume-bob ID` |
| Bob | OpenCode | `worker-launcher.py` | `bob-sessions.jsonl`, `conversation_id` (`ses_…`) | `resume-bob ID` |
| Bob | AntiGravity | `worker-launcher.py` | `bob-sessions.jsonl`, `conversation_id` (stream-json ID) | `resume-bob ID` |
| Charlie | Claude Code | `worker-launcher.py` | `charlie-sessions.jsonl`, `conversation_id` (UUID) | `resume-charlie ID` |
| Charlie | Codex | `worker-launcher.py` | `charlie-sessions.jsonl`, `conversation_id` (UUID) | `resume-charlie ID` |
| Charlie | OpenCode | `worker-launcher.py` | `charlie-sessions.jsonl`, `conversation_id` (`ses_…`) | `resume-charlie ID` |
| Charlie | AntiGravity | `worker-launcher.py` | `charlie-sessions.jsonl`, `conversation_id` (stream-json ID) | `resume-charlie ID` |

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
  call `get_state` first, reconcile with the skill, keep escalation and pause,
  do not repeat work. That prompt carries no state and no operator decision.
  The wait doubles from `--resume-delay-s` (default 5 s) up to
  `--resume-max-delay-s` (default 1800 s, about 30 minutes), then repeats at
  the cap; a run lasting `--resume-series-reset-s` (default 300 s) starts a new
  series at the short delay. Resumes stop after `--resume-total-s` (default
  43200 s, about 12 hours) since the first exit of the current series, or after
  `--max-resumes` when given (by default no count limit; 0 disables them),
  then exits 1 with these manual steps. Each wait is logged as a `wait` line
  with `delay_s` and `next_attempt`, and stderr says when the next resume
  goes. Healthy running time before that first exit never spends the budget,
  and a series reset refills it. The hub is re-read after the wait, before
  every resume, so a pause, `done`, or a different hub or workflow during a
  long wait still stops the launcher.
- stops without launching again, and says why, when the workflow is `paused`
  (exit 5: resume by hand when you are ready), when Alice exited before
  initializing the workflow or before printing her conversation ID (exit 4: it
  never replays the kickoff or guesses an ID), or when the hub stays unreadable
  or names another hub or workflow after `--read-retries` reads (exit 3: it
  never guesses `done`).
- on Ctrl-C or SIGTERM, stops only the harness process it started, launches
  nothing more, and exits 130. A long backoff wait is interruptible: Ctrl-C
  stops the launcher promptly.

`prepare-run.py --max-resumes N --resume-delay-s S --resume-max-delay-s MAX
--resume-total-s TOTAL --resume-series-reset-s RESET` sets the backoff for the
run, passing the same settings to supervised workers and to Alice, and
`run.json` records them under `launch.agents.alice` and each supervised
worker's `launch.agents.<worker>`. Every launch, wait, exit, conversation ID
and stop reason is one line in `alice-sessions.jsonl`; the token and command
lines are never logged.

When the launcher has stopped, take a before-snapshot and resume by hand:

```sh
cd /absolute/path/to/my-run/hub-target          # the target clone running the hub
uv run --project /absolute/path/to/robomate robomate status --snapshot \
  --stopped-at 2026-10-08T12:34:56Z               # when you saw the old process stop
"$RUN_DIR/resume-alice.sh" CONVERSATION_ID        # resume-alice.ps1 on Windows
```

Paste the snapshot into `RUN_DIR/resume-alice.prompt.md` where it asks, first.
`resume-alice` runs the same launcher with `--resume-session`: its first launch
resumes the given conversation with that manual prompt, and later exits get a
fresh budget of `--max-resumes` automatic resumes with the fixed continuation.
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
(default 20 minutes; `prepare-run.py --stall-after-min`):

- it made no substantive hub call and holds none (`no_hub_call`);
- a worker's assigned task has had no message, progress, question or result
  since its assignment (`no_task_progress`);
- an event for Alice has never been delivered (`event_backlog`).

Each line names the ages, the bridge heartbeat's age, and the task or event
IDs. `robomate status --json` carries the same under each agent's `activity`
and `orchestrator_activity`; `scripts/hub-report.py` lists every stall episode.
A held `await_assignment`, `ask_alice` or `wait_for_event` is never a stall,
however long the waiting goes on; an open operator question is not by itself
an exemption, because Alice should be holding `wait_for_event` for its answer.
A long non-hub command (a test suite, a CI watch) can also trigger the
warning: it is not proof of a provider fault. `lost` is different: the
heartbeat itself stopped (`HUB_LOST_AFTER_S`, 180 s). A stall changes nothing:
no task fails, nothing is reassigned, and no process is stopped. A worker's
stall reaches Alice as one `agent_stalled` event per episode; Alice's own
stall reaches only you, through status and the report. Done workflows and
released or lost agents are never reported; a paused workflow suspends the
check, and resuming it restarts every clock.

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

`prepare-run.py` generates that resume as `RUN_DIR/resume-<agent>.sh` (`.ps1`
on Windows) with its matching `resume-<agent>.prompt.md`, for every agent on
its configured harness; the generated resume prompt carries the same
reconciliation checklist, with `<...>` placeholders for the operator's
before-snapshot. Prefer the generated script over a hand-built command: it
repeats the start script's directory, environment, and model/effort flags, but
resumes the saved conversation instead of sending the kickoff prompt. It takes
the conversation/session ID as its only argument:

```sh
./resume-alice.sh SES_ID        # or resume-alice.ps1 on Windows
```

Fill in the resume prompt first: for Alice, paste the output of
`robomate status --snapshot --stopped-at <UTC time>` from the target clone
(hub ID, workflow, her RPC session, open tasks, unacknowledged deliveries);
for a worker, the `<...>` placeholders. Every generated resume prompt already
names the real run directory. Verify the old harness process is gone before
resuming.

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
resumes and predecessor waits. `tests/test_prepare_run.py` executes generated
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
