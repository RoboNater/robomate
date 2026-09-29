# M1 Step 7 restart and recovery guide

This is the operator procedure for [#44](https://github.com/RoboNater/robomate/issues/44),
the live acceptance defined in [Step 7](implementation-plan-mvp-m1.md#step-7--m1-acceptance-run).
Ordinary interruptions use the separate [agent recovery guide](agent-recovery.md). Use a **new
acceptance attempt** after a failed checkpoint; never reinterpret an earlier run
as proof of redelivery. The hub stays up when Alice's harness is interrupted.

## Identify this run before acting

Use the absolute paths printed by `scripts/prepare-run.py`. In the examples,
replace the two paths and the issue number with this run's values:

```sh
HUB_REPO=/absolute/path/to/the/dedicated/target-clone
RUN_DIR=/absolute/path/to/the/run-directory
cd "$HUB_REPO"
uv run --locked robomate up                 # leave this terminal open
uv run --locked python scripts/prepare-run.py --hub-repo "$HUB_REPO" \
  --repository git@github.com:RoboNater/robomate.git \
  --run-dir "$RUN_DIR" --issue 44 --account YOUR_GITHUB_ACCOUNT \
  --alice-harness codex --alice-model MODEL --alice-effort EFFORT
```

Run the generated `start-alice.sh`, `start-bob.sh`, and `start-charlie.sh` in
separate foreground terminals (or the `.ps1` equivalents on Windows). Do not
rerun `prepare-run.py` to resume a run: preserve its manifest, configuration,
workspaces, and prompt. Keep the hub's `.robomate/` directory, especially
`hub.db`, `hub.json`, and `token`. `HUB_STATE_DIR` is anchored by the hub setup;
the generated run manifest names the actual state directory. The run directory
contains `manifest.json`, generated start scripts, prompts, isolated Codex
config, and worker telemetry. The worker clone paths are in
`manifest.json.workspaces`; do not assume they are adjacent to the hub clone:

```sh
python3 -c 'import json,sys; m=json.load(open(sys.argv[1])); print("state:",m["state_dir"]); print("workspaces:",m["workspaces"]); print("launch:",m["launch"])' "$RUN_DIR/manifest.json"
```

Keep raw database backups, token bearing configs, telemetry, harness transcripts,
and launch logs in a private directory **outside every Git checkout**. Publish
only reviewed, redacted observations and aggregate counts. Never print or copy
the token into an issue or PR. If worker sandbox PID visibility or its registry
lock differs from the hub host, set `ROBOMATE_HUB_URL` and
`ROBOMATE_TOKEN_FILE` to its configured values before `robomate status --json`;
the explicit URL path bypasses local registry discovery. A matching `/healthz`
hub ID plus authenticated `hub.status` proves that hub is reachable even when
its PID is invisible. A failed HTTP probe with a visible live PID means
*unreachable*; a stopped report after both probes cannot distinguish a dead
hub from network loss or PID namespace isolation without a hub-host check.

The Codex conversation ID (from its saved session or CLI output), the hub ID
(from `hub.json`/health), the workflow ID (`get_state.workflow.id`), the Alice
RPC session ID (`robomate status --json`), and the worker task ID are different
identifiers. Record all of them separately. A Codex process exit is only a
harness event; it does **not** complete the hub workflow.

## Deliberate Step 7 checkpoint

Alice and the operator coordinate one short window. The operator declares a
10-minute task hold, with a UTC deadline, to Bob and Charlie. Bob confirms his
assigned task ID and owner, continues heartbeats, and does not voluntarily
submit a blocked/completed result during the hold. If he needs `ask_alice`,
he uses `timeout_s=100` and retries the same question on normal timeouts; a
question may temporarily make the task `input-required`. Alice gives Bob the
deadline and confirms it in a durable decision or progress note. If the
checkpoint cannot be reached before the deadline, Alice aborts this attempt,
answers pending questions, and explicitly tells Bob whether to finish or
submit a blocked result. Renewing the hold needs a fresh explicit deadline;
silence is never an extension. A task that has become terminal cannot be used
for the restart proof; Alice must reassign a new task for a new attempt.

1. Alice calls `get_state` and confirms an active Bob task, its ID, owner, and
   state. She arranges one worker-generated event (for example a progress note
   or question), calls `wait_for_event`, and records its **event ID, delivery
   ID, `delivery_attempts=1`, kind, and Alice RPC session**. She must leave
   this delivery unacknowledged: no `ack` argument carrying its delivery ID,
   including on a later wait. `queued_events` is not sufficient.
2. Alice explicitly says **READY TO INTERRUPT**, the event/task identifiers,
   and the hold deadline. She remains alive in the harness while the operator
   captures the before snapshot. A noninteractive `codex exec` final answer
   ends its process; that is a failed readiness attempt, not the required
   operator kill. If the harness cannot stay running in this mode, resume its
   saved Codex conversation interactively with `codex resume
   --include-non-interactive` and the same `CODEX_HOME`/model/effort settings
   before establishing the checkpoint. Keep that foreground session open
   until the operator kills it.
3. The operator saves `robomate status --json` and a consistent SQLite backup
   before stopping Alice. The backup must show the event `state=delivered`,
   `delivery_attempts=1`, non-null `delivery_id`, and no `acked_at`. Capture
   UTC time, Alice process ID, worker telemetry offsets, and Bob/Charlie
   status. These files are private. One possible hub-host capture is:

   ```sh
   CAPTURE=/private/path/outside/git/m1-attempt-1
   mkdir -m 700 "$CAPTURE"
   cd "$HUB_REPO"
   uv run --locked robomate status --json > "$CAPTURE/before-status.json"
   python3 -c 'import sqlite3,sys; src=sqlite3.connect("file:"+sys.argv[1]+"?mode=ro",uri=True); dst=sqlite3.connect(sys.argv[2]); src.backup(dst); dst.close(); src.close()' "$HUB_REPO/.robomate/hub.db" "$CAPTURE/before.db"
   python3 -c 'import sqlite3,sys; c=sqlite3.connect(sys.argv[1]); print("workflow",c.execute("select id,status from workflow").fetchall()); print("tasks",c.execute("select id,assignee,state from task").fetchall()); print("events",c.execute("select id,kind,state,delivery_id,delivery_attempts,acked_at from event order by id").fetchall())' "$CAPTURE/before.db" > "$CAPTURE/before-rows.txt"
   ```

4. Stop **only Alice's foreground harness**, with Ctrl-C or a targeted TERM
   signal to its verified process ID. Record the signal, time, and exit status;
   confirm the old process is gone before relaunch. Leave hub and workers
   running. Do not send `robomate down` for this test. If the old Alice has
   already ended voluntarily, record a failed attempt and reset the checkpoint.
5. Resume the **same saved Alice Codex conversation** using the same
   `CODEX_HOME`, working directory, model, effort, and approval/sandbox setup
   as `start-alice.sh`, but supply a new follow-up prompt. Use its recorded
   Codex conversation UUID, not the hub session or workflow ID. For the
   generated noninteractive Codex setup on this CLI, the form is:

   ```sh
   cd "$RUN_DIR/alice-runtime"
   CODEX_HOME="$RUN_DIR/configs/alice-codex" \
     codex exec --approve-for-me resume --skip-git-repo-check --model MODEL \
       -c 'model_reasoning_effort="EFFORT"' CODEX_CONVERSATION_ID - \
       < "$RUN_DIR/resume-alice.prompt.md"
   ```

   Check the actual generated `start-alice.sh` for the `CODEX_HOME` directory,
   `--model` and effort flags and copy their **actual values** into this
   command. Do not substitute the example config path if it differs. For an
   interactive Codex launch, use the same `CODEX_HOME` and directory with
   `codex resume --include-non-interactive --approve-for-me -m MODEL -c
   'model_reasoning_effort="EFFORT"' CODEX_CONVERSATION_ID`, then submit the
   same prompt text. Omit `--include-non-interactive` if the saved session was
   already interactive. The `codex exec resume` and `codex resume` forms are
   supported by [OpenAI's noninteractive guide](https://learn.chatgpt.com/docs/non-interactive-mode)
   and [CLI reference](https://learn.chatgpt.com/docs/developer-commands?surface=cli).
   If this CLI/configuration cannot resume its exact conversation, stop the
   attempt and record that limitation; a fresh conversation needs an explicit
   reconciliation decision. Never use `--last` when several conversations
   could match.

   Write `resume-alice.prompt.md` privately under `RUN_DIR`, for example:

   > Resume the existing #44 workflow. Operator decision: continue the
   > documented Step 7 checkpoint. Before event ID N, delivery ID D, attempt
   > 1, Alice RPC session S; Bob task T was active under Bob. I stopped the old
   > Alice process at UTC TIME and verified it exited. Read `get_state` and
   > `wait_for_event` without acknowledging D. Verify the same hub, workflow,
   > and task; verify event N has attempt 2 and a new delivery ID. Only then
   > acknowledge it and continue. If any invariant differs, report the failed
   > attempt and ask for a new checkpoint. This prompt is the operator's
   > answer to the pending restart escalation; log that decision and restore
   > workflow active before continuing.

6. New Alice first reads durable state, then `wait_for_event` **without** an
   `ack` argument. Record the new Alice RPC session and the returned event.
   Success requires the same hub ID, workflow ID, Bob task ID and owner, and
   **same event ID at attempt 2 with a different delivery ID**, before any
   acknowledgement. A queued event changing from attempt 0 to 1 is first
   delivery, so it fails this check. After Alice says **REDELIVERED AND HELD**,
   capture after status/backup with the same commands (using `after` names),
   then let Alice acknowledge the new delivery
   and continue. Check worker telemetry and event table only for the bounded
   restart window: no failed worker calls, `agent_lost`, or `lease_expired`.
   Normal long-poll timeouts do not count as failed calls. Report any later
   loss separately in the whole-run evidence.

7. Record every failed attempt and reason. Alice completes implementation,
   independent review, current-head CI and merge gate, then squash merges if
   authorized. Evidence committed to the PR can state the approved PR head
   and the expected merge check. The **postmerge** comment and actual merge
   commit do not exist before merge, so the premerge document must not claim
   them. After merge, Alice reads the PR and records a postmerge comment or
   closeout artifact comparing the approved head to the PR head accepted at
   merge. A squash merge creates a **distinct merge commit**; record that SHA
   separately. If Step 7's old phrase “merged SHA equals approved head” is
   read literally, this is the explicit interpretation needed for a squash
   repository, not a claim that the two commit IDs match.

To stop a hub started by `robomate up` after the run, use `uv run --locked
robomate down` in `HUB_REPO` and verify its process exits. Never stop a hub
owned by another checkout.

For interruptions outside this deliberate checkpoint, use the separate
[Alice/Bob/Charlie recovery guide](agent-recovery.md).

## Disposition of the 2026-09-28 #44 incident

Established: six Alice sessions ended with escalation messages; the observed
before/after Alice RPC sessions differ, Bob's retry task had the same owner and
was working in those two snapshots, and worker calls/heartbeats succeeded in
their broad bracket. Event 14 was queued after Alice had exited, was delivered
once on the later session, and was acknowledged. All delivered events in the
final database had one attempt. Later `agent_lost` events occurred outside the
restart window; their cause was not investigated. No PR or live Step 7 proof
was produced. These facts come from the reviewed [#62 investigation](https://github.com/RoboNater/robomate/issues/62);
private raw captures stay local.

Contributing factors: there was no READY checkpoint, event 13 had been
acknowledged before Alice exited, Bob later submitted blocked results, and
the restart prompt lacked a verified delivered event and durable operator
decision. The hypothesis that PID namespace visibility made ordinary status
report the hub stopped is consistent with its old PID-first code path; the
original sandbox conditions were not fully reproduced. The scoped status
regression here proves the health-first behavior in a disposable fixture.
The separate focused bridge-test `KeyError: 'reply'` occurred after its
redelivery assertions, when a 5-second worker hold could return `timeout`
before the test's reply. Its original timing was not captured, so this is the
best-supported cause, not a proved scheduler trace. The regression now
forces timeout and retries the same question ID. Neither observation justifies
a hub event-redelivery defect or a waiver of #44.
