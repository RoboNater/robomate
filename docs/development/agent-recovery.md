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
(5), or Ctrl-C/SIGTERM (130). On interruption it stops only its own harness
child. It checks status again after a backoff before launching a successor.

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
only be selected from this run's log. A missing hub/workflow pin requires
independent verification and `--hub-id HUB_ID --workflow-id WORKFLOW_ID`.
Explicit pins must agree with saved pins and the live snapshot. A changed or
unreachable hub, absent workflow, missing configuration, changed workspace,
completed workflow, or released worker refuses launch. Restore the original
files/address/state, or ask Alice/operator whether separate fresh work is
needed. Never initialize another workflow to make recovery pass.

Start and resume launchers share an OS lock for the lifetime of the process.
An existing launcher, a recorded predecessor PID still present without an
exit, or an attached worker bridge refuses launch. Alice also refuses while
her old RPC session has a live hold or a heartbeat less than 60 seconds old.
Wait for detach/expiry, or have the operator inspect and stop its orphan bridge;
then retry. No option bypasses these guards. Workers never stop predecessors.
When no exit was recorded (including interactive runs), the operator must
verify both old harness and launcher are stopped and add `--confirm-stopped`.
That confirmation supplies no timestamp and cannot bypass a known attachment.

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
