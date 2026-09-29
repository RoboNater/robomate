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
hub host before declaring the hub stopped. Record the Codex conversation ID,
hub ID, workflow ID, Alice RPC session ID, and task ID as separate fields.

| Interrupted agent/state | Operator and Alice action |
| --- | --- |
| Alice idle, no delivered event | Resume the saved Codex conversation with the same config and an operator prompt to read durable state. Reconcile tasks, PR heads, and checks before new assignments. There is no redelivery proof to claim. |
| Alice active, delivered event | Confirm the old process is gone, then resume. New Alice reads state and redelivery before ack. If the event was already acked, continue ordinary recovery and mark a Step 7 checkpoint failed. |
| Bob or Charlie idle | Relaunch that worker from its generated script/config and workspace. Check in once for the new runtime, then await assignment. Do not assign duplicate work merely because a harness exited. |
| Bob or Charlie with active task | Preserve branch, uncommitted edits, pending question/result, task ID, and PR head. Relaunch in the same workspace with the same worker identity; Alice reconciles task state and decides whether the same worker can finish or a fresh task is needed. Never silently overwrite a result or assign another worker to the same branch. |
| Pending `ask_alice` question | Worker retries the **same text on the same task** after a normal timeout, preserving message correlation. Alice replies to the durable question once. A canceled/failed/completed task response is terminal, not an answer. |
| Result submitted before crash | Alice reads the durable typed result and GitHub PR state. Do not submit it again or infer merge from a process exit. |
| Uncommitted worker changes | Inspect `git status`, local diff, and remote branch separately; retain the workspace until the owner decides to commit, discard, or hand off. A new clone cannot reconstruct uncommitted work. |

Alice chooses resume, reassign, or escalate only after comparing durable task
state, GitHub head, and local workspace state. A terminal task cannot be
resumed by merely restarting the worker: assign a fresh task if the same work
is still needed. If the prior harness cannot restore its saved conversation,
or a different worker must take over uncommitted work, escalate with the saved
state and Git status. M1 has no automatic inbox, queue, or takeover contract.

The disposable CLI up/down test covers hub identity and port reuse;
`tests/test_orchestrator_bridge.py::test_worker_task_survives_orchestrator_session_restart`
covers active task, pending question retry, and event redelivery;
`tests/test_mock_alice.py` covers simulated delivery/ack crash paths. These
fixtures do not prove a real operator kill, Codex conversation resumption,
cross-host worker relaunch, or PR merge sequencing. [M2 join/takeover](../mvp-spec.md)
must define safe new-session ownership and reassignment before this becomes
automatic. [#18](https://github.com/RoboNater/robomate/issues/18) should
reconcile pending questions, results, and uncommitted work on resume;
[#19](https://github.com/RoboNater/robomate/issues/19) must distinguish host
standby from worker loss. Acceptance for those issues should include idle and
active crash fixtures and a live cross-host resume with durable identity,
single ownership, and no duplicate PR action.
