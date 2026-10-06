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

If the hub itself stopped, run `uv run --locked robomate up` from its dedicated
target clone and verify the same hub ID and database. A hub started by
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
| Bob or Charlie with active task | An auto-started Claude Code worker's launcher resumes it by itself; act only once it reports its resumes spent. Otherwise preserve branch, uncommitted edits, pending question/result, task ID, and PR head, and resume the worker's saved conversation in the same workspace before the hub declares it lost (below). Failing that, relaunch with the same worker identity; Alice reconciles task state and decides whether the same worker can finish or a fresh task is needed. Never silently overwrite a result or assign another worker to the same branch. |
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

<!-- Headless turn end, launcher and detach: #115. -->

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

`prepare-run.py` runs every auto-started Claude Code worker under
`scripts/claude-worker.py`. It starts `claude -p` with a `--session-id` it
picks. Whenever `claude` exits before the worker's telemetry records
`release: true`, it resumes that conversation with a fixed continue prompt, at
most five times. Every launch and exit is logged to
`RUN_DIR/<worker>-sessions.jsonl` (`launch.agents.<worker>.sessions` in
`run.json`). When its resumes are spent it exits 1 and prints the conversation
ID to resume by hand. Other harnesses and Alice have no launcher: resume them
by hand as below.

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
| Claude Code | A UUID. For a worker under `scripts/claude-worker.py`, take `session_id` from `RUN_DIR/<worker>-sessions.jsonl`. Otherwise it is the file name of `~/.claude/projects/<dir>/<uuid>.jsonl`, where `<dir>` is the working directory with every character other than a letter or digit replaced by `-`; pick by modification time. |
| Codex | A UUID from the saved session or the CLI output; the exact `codex exec resume` form is in [M1 restart and recovery](m1-restart-recovery.md). |
| OpenCode | A `ses_...` ID. `opencode session list`, run from the working directory, lists them; every title reads `New session - <ISO time>`, so match by time, and `opencode export <id>` prints a transcript to confirm (#94). |

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
  opencode run --auto --model MODEL --variant EFFORT --session SES_ID \
  "Read $RUN_DIR/resume-alice.prompt.md and follow it"
```

Never use `claude --continue` or `opencode --continue`: each picks the most
recent conversation, which may be another agent's. A resumed `opencode run`
can idle after Alice's final message with its bridge still connected; stop it
once the workflow is `done` (#94).

The disposable CLI up/down test covers hub identity and port reuse;
`tests/test_orchestrator_bridge.py::test_worker_task_survives_orchestrator_session_restart`
covers active task, pending question retry, and event redelivery;
`tests/test_mock_alice.py` covers simulated delivery/ack crash paths;
`tests/test_worker_client.py::test_a_restarted_worker_checks_in_at_once_and_finishes_its_task`
covers the detach, and `tests/test_claude_worker.py` the launcher's resumes. These
fixtures do not prove a real operator kill, Codex conversation resumption,
cross-host worker relaunch, or PR merge sequencing. [M2 join/takeover](../mvp-spec.md)
must define safe new-session ownership and reassignment before this becomes
automatic. [#18](https://github.com/RoboNater/robomate/issues/18) should
reconcile pending questions, results, and uncommitted work on resume;
[#19](https://github.com/RoboNater/robomate/issues/19) must distinguish host
standby from worker loss. Acceptance for those issues should include idle and
active crash fixtures and a live cross-host resume with durable identity,
single ownership, and no duplicate PR action.
