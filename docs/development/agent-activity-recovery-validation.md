# Agent activity and recovery: live acceptance

The operator's live checks for #144 (stall detection) and #146 (Alice's
launcher and the before-snapshot), run at the candidate commit after the
automated suite passes. Each check proves something the tests cannot: a real
harness frozen with its bridge alive, a real harness killed and resumed in the
same conversation, and a snapshot read beside a live Alice.

Only the operator runs these. A worker or orchestrator in the implementation
run never freezes, kills or starts an agent or a hub for them; Alice asks for
them with `ask_user` and waits.

## What the tests already cover

`uv run --locked pytest tests/test_activity.py tests/test_alice_launcher.py`
and the `stall`, `snapshot` and `alice_scripts` tests in `tests/test_cli.py`
and `tests/test_prepare_run.py` cover, with a fake clock, fake harnesses and
stub hubs: silence with a fresh heartbeat, holds and repeated hold timeouts,
stale task progress and unconsumed events, one event per episode, restarts,
paused and done workflows, each harness's launch and resume arguments, the
continuation prompt, retries, logs, hub failures and interruption, and the
snapshot against `get_state`. They are adapter tests: they show the launcher
builds and runs each harness's command, not that a given harness version
behaves as assumed. The live checks below are where that is shown.

## Setup

Use `RoboNater/robo-agents-sandbox`, a fresh scratch directory, its own
registry, operator token and port, and the candidate commit. Keep the host
awake for the whole check (standby is #19).

```sh
export ROBOMATE=/absolute/path/to/robomate        # checked out at the candidate SHA
export ACC="$(mktemp -d)"                          # everything for these checks
export RUN="$ACC/run"
mkdir -p "$RUN"
# Every robomate command for these checks goes through this function, so the
# machine's registry and operator token are never touched.
rm8() {
  env -u ROBOMATE_HUB_URL -u ROBOMATE_TOKEN -u ROBOMATE_TOKEN_FILE \
    XDG_STATE_HOME="$ACC/xdg" ROBOMATE_OPERATOR_TOKEN_FILE="$ACC/operator-token" \
    uv run --project "$ROBOMATE" robomate "$@"
}
git -C "$ROBOMATE" rev-parse HEAD                  # record: candidate SHA
git clone git@github.com:RoboNater/robo-agents-sandbox.git "$RUN/hub-target"
(cd "$RUN/hub-target" && rm8 up --port 8471)       # its own terminal; keep it open
```

Seed a sandbox issue for the run (any small change with a test), then prepare
the run with a short threshold. For the exit check, Alice runs on OpenCode:

```sh
cd "$ROBOMATE"
uv run --locked python scripts/prepare-run.py \
  --hub-repo "$RUN/hub-target" --repository RoboNater/robo-agents-sandbox \
  --run-dir "$RUN" --issue N --account RoboNater \
  --stall-after-min 2 --alice-harness opencode --alice-model PROVIDER/MODEL
```

Start `start-alice.sh`, then each worker, in their own terminals (`.ps1` on
Windows). The launcher prints Alice's harness PID; `alice-sessions.jsonl` has it
as `pid` on each `start`/`resume` line. A worker's harness PID is the `claude`,
`codex`, `opencode` or `agy` process under its start script
(`ps -o pid,ppid,args --forest`). Never signal a `robomate mcp` bridge.

Observation commands, all read-only:

```sh
(cd "$RUN/hub-target" && rm8 status)                     # STALLED lines and evidence
(cd "$RUN/hub-target" && rm8 status --json) | python -m json.tool
uv run --locked python "$ROBOMATE/scripts/hub-report.py" --state-dir "$RUN/hub-target/.robomate"
# One query over the hub database, read-only:
q() { python - "$RUN/hub-target/.robomate/hub.db" "$1" <<'EOF'
import sqlite3, sys
db = sqlite3.connect(f"file:{sys.argv[1]}?mode=ro", uri=True)
for row in db.execute(sys.argv[2]):
    print(row)
EOF
}
```

## 1. A frozen harness with a live bridge reads STALLED

Freeze only the harness process, never its bridge (POSIX `kill -STOP`; on
Windows, Sysinternals `pssuspend.exe PID` and `pssuspend.exe -r PID`).

1. Once Alice has assigned the implementation, freeze her harness:
   `kill -STOP "$ALICE_PID"`. Note the UTC time. Leave the workers running, so
   a deliverable event (a progress note or the result) queues for her.
2. Within the threshold plus one sweep (2 minutes plus 10 s; plus up to 130 s
   if she was inside a `wait_for_event` hold when frozen), `rm8 status` shows
   `STALLED — no hub call for …; event K (…) queued … undelivered; bridge
   heartbeat …s ago` under the orchestrator line, with the heartbeat age under
   a minute. The report lists an open orchestrator episode.
3. `kill -CONT "$ALICE_PID"`. After her next hub call she consumes the event,
   and within one sweep the STALLED line is gone; the report shows the episode
   `cleared (recovered)`.
4. Repeat for one worker during its leased task: freeze its harness, observe
   `bob: … STALLED` with `no hub call` and `task … no progress`, then
   `q "SELECT id, ts FROM event WHERE kind = 'agent_stalled'"` shows exactly
   one row for the episode, however many sweeps pass. Continue it and observe
   the warning clear after its next progress note or call.
5. Negative control: while Alice is legitimately waiting on an operator
   question (an `ask_user` she raised; leave it unanswered for longer than the
   threshold) or on a worker, `rm8 status` shows no STALLED line for her, and
   idle workers in `await_assignment` show none either.

Stalls never change lifecycle: check `rm8 status` still shows the task open,
its worker `busy`, and the workflow status unchanged throughout.

## 2. Alice's harness exits and the launcher resumes her

1. Mid-workflow (a worker busy, Alice holding `wait_for_event`), kill only her
   harness child, never the launcher: `kill -TERM "$ALICE_PID"` (Windows:
   `Stop-Process -Id PID`). Note the UTC time.
2. Within `--resume-delay-s` (5 s) the launcher logs `exit` then `resume` with
   the same `conversation_id` (`tail -3 "$RUN/alice-sessions.jsonl"`).
3. A new RPC session supersedes the old one:
   `q "SELECT ts, actor, session FROM rpc_audit WHERE method = 'session.supersede'"`.
4. Her first substantive call after the takeover is `get_state`:
   `q "SELECT started, tool FROM call_log WHERE actor = 'alice' AND started > 'KILL_TIME' ORDER BY started LIMIT 3"`
   (call accounting is on under `robomate up`).
5. The run finishes with no manual resume: the launcher logs `stop` with
   `reason: done`, and the report shows one implementation task per round, one
   review per head, one merge, and no repeated operator question.

This does not prove #134's uninterrupted escalation hold; that is a separate
check.

## 3. The before-snapshot changes nothing

While a task is open and, ideally, an event is delivered but not yet acked:

```sh
q "SELECT COUNT(*) FROM rpc_audit WHERE method = 'session.supersede'"   # before
q "SELECT id, delivery_id, delivery_attempts FROM event WHERE state = 'delivered'"
(cd "$RUN/hub-target" && rm8 status --snapshot --stopped-at "$(date -u +%FT%TZ)")
q "SELECT COUNT(*) FROM rpc_audit WHERE method = 'session.supersede'"   # after: same
q "SELECT id, delivery_id, delivery_attempts FROM event WHERE state = 'delivered'"  # same
```

Compare the snapshot's open tasks and deliveries with Alice's next `get_state`
in her transcript and with `q "SELECT id, assignee, role, state FROM task WHERE
state IN ('submitted', 'working', 'input-required')"`. The snapshot's Alice
session must still be the current one in `rm8 status`.

## Cleanup

Stop only what these checks started: each agent's terminal (Ctrl-C stops a
launcher and its harness), then the hub with
`(cd "$RUN/hub-target" && rm8 down)`, and confirm `rm8 ls` lists nothing. Close
or delete the sandbox issue, branch and PR as the sandbox's practice requires.
Keep `$RUN` until the evidence is written, then remove `$ACC`.

## Evidence

The first run, at f08340c, passed all three checks:
[agent-activity-recovery-20261009T082101.md](../evidence/agent-activity-recovery-20261009T082101.md).

Record the results in `docs/evidence/agent-activity-recovery-<run-id>.md`:

```markdown
# Agent activity and recovery: live acceptance <run-id>

- Candidate SHA: <40-hex>          Operator: <name>          Date (UTC): <date>
- Host / OS: <...>                 Harness versions: <alice/bob/charlie: harness version, model>
- Hub: <hub ID>, port <port>, state dir <path>; workflow <ID>; stall_after_min <n>
- Sandbox: issue <URL>, PR <URL>, merge SHA <40-hex>

| Check | Actor (operator / agent / script) | Commands | Observed (times UTC, IDs, ages) | Result |
| --- | --- | --- | --- | --- |
| 1 Alice frozen → STALLED with fresh heartbeat and backlog event | | | | pass / fail / not run |
| 1 Alice continued → cleared | | | | |
| 1 Worker frozen → STALLED, one agent_stalled event | | | | |
| 1 Negative control (operator-answer or worker wait) | | | | |
| 2 OpenCode Alice killed → same conversation resumed | | | | |
| 2 session.supersede row; get_state first; run done without manual resume | | | | |
| 3 Snapshot matches state; no supersede, no delivery change | | | | |

Cleanup: <processes stopped, hub stopped, `rm8 ls` empty, sandbox cleaned>
Limitations / not run: <each skipped check and why; skipped is not a pass>
```

Exclude tokens and operator credentials. Distinguish what the operator did
(freeze, kill, answer) from what agents and scripts did.
