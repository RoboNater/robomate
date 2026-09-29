# M1 Step 7 acceptance — run `16-issue-44-attempt-2`

Implementation agent bob on behalf of RoboNater

This document records the deliberate orchestrator restart in the live
[#44 acceptance run](https://github.com/RoboNater/robomate/issues/44). The
operator kept the hub and both worker bridges running while Alice's old
harness stopped and a new RPC session connected. The observations below come
from two `robomate status --json` captures, consistent SQLite backups, and
copies of worker telemetry
taken outside the checkout. The raw captures, harness transcripts, configs,
tokens, and telemetry are private and are not included in this PR.

The bounded restart window used here begins with the before capture at
**2026-09-29 18:01:14.146 UTC** and ends with the after capture at
**18:03:48.259 UTC**. Assertions about worker calls and loss events below
apply to that window; later workflow activity requires separate verification.

## Before and after

The table reproduces selected status fields; repository paths and goal text
are omitted from the published extract.

| Observation | Before | After |
|---|---|---|
| Hub ID | `31e2b4fbd0e14016b4ac69311e9185f7` | Same |
| Workflow ID and status | `8531bbfe85b34caa8d951b3fe6b2965f`, active | Same |
| Alice RPC session | `6d4768e4-2349-44d7-85b5-12d58b583e19` | `b06a28a8-c0a5-42fd-873a-f6ca62647c3d` |
| Bob task | `5b564eaace1a4c8fb22653037fcbb201`, owned by bob, working | Same ID, owner, and state |
| Workers in `robomate status --json` | Bob busy/alive on that task; Charlie idle/alive | Same |
| Pending questions | 0 | 0 |

The task row in both database backups also has the same `updated` timestamp
(`2026-09-29T17:59:42.083Z`) and lease expiry
(`2026-09-29T19:57:05.846Z`). The workflow stayed active. The operator's
stop record verifies that the old Alice processes were absent at
**18:01:49.570886 UTC**. It does not independently record the exact signal,
exit time, or exit status, so this document does not assert them. The captures
record saved Codex conversation ID `01a0ee4e-80a0-7b50-903c-aeda67bfd5ae`
as the sole pre-restart candidate, and the resume prompt names it. They do
not independently verify that the resumed harness used that same conversation.

## Held event and redelivery

Alice left Bob's `task_progress` event **5** delivered and unacknowledged in
the old session. The before backup records delivery attempt **1**, delivery
ID `eec7472d2f8640af8023e9666a812665`, delivered at
`17:59:46.019Z`, expiring at `18:09:46.019Z`, and no `acked_at`.

The after backup records the **same event ID** at attempt **2**, with new
delivery ID `02c98b4854f64f8faff6d77262ab5bb7`, delivered at
`18:02:39.636Z` and still unacknowledged. The new delivery occurred
**7 minutes 6.383 seconds before** the old delivery's expiry. Alice's new
session made an MCP `get_state` call at `18:02:31.315Z` and received the
`wait_for_event` result at `18:02:39.659Z`. These timestamps establish
redelivery after session replacement, within the old lease rather than after
normal expiry.

## Worker continuity in the bounded window

The after backup contains no `agent_lost` or `lease_expired` events; its five
event rows are the same IDs and kinds present before the restart. Bob's task
row has no state, owner, or update-time change. Both workers remained alive in
the status snapshots.

| Worker | Accepted heartbeat telemetry within the window | Completed tool calls within the window | Failed worker calls |
|---|---:|---|---:|
| Bob | 5 | None | 0 |
| Charlie | 5 | Two `await_assignment` calls, each HTTP 200 and a normal long-poll timeout | 0 |

The hub's `call_log` confirms the ten heartbeats as `ok`/HTTP 200 and both
Charlie's long polls as `timeout`/HTTP 200. The telemetry has no error or
retry phase in this window. Normal assignment long-poll timeouts are not
failed calls.

## Accounting at the after capture

I ran `scripts/hub-report.py`'s `build_report` over the immutable after
backup and the matching Bob and Charlie telemetry, with labels suppressed.
The accounting sources are Alice's MCP `call_log`, worker tool telemetry,
and the hub's A2A `call_log` for wire bytes (including heartbeats). These are
cumulative figures at **18:03:48.259 UTC**, while the workflow was active;
they are not final whole-run totals. Units are serialized message-body bytes,
not model tokens.

| Agent | MCP bytes sent / received | A2A wire bytes sent / received |
|---|---:|---:|
| Alice | 29,836 / 64,200 | 0 / 0 |
| Bob | 1,365 / 7,991 | 9,708 / 13,413 |
| Charlie | 123 / 499 | 7,624 / 6,155 |

The same report on the before backup showed Alice MCP
**25,784 / 48,931**, Bob A2A wire **7,453 / 11,828**, and Charlie A2A wire
**4,936 / 3,910**. The after database has schema version 12; this run
introduces no DB schema or wire `hub.schema_version` change.

## Attempts and merge evidence

The supplied checkpoint artifacts document no earlier failed checkpoint
attempt within run `16-issue-44-attempt-2`. The separate
[2026-09-28 #44 attempt](https://github.com/RoboNater/robomate/issues/62)
did not prove redelivery or reach a PR: its candidate event was first delivered
after Alice had exited, and Bob later submitted blocked results. It is a
failed historical acceptance run, not evidence for this checkpoint. The
checkpoint above establishes held-event redelivery in this run; the same
Codex-conversation detail remains unverified by these captures.

At the time of these captures, no PR head had been approved or merged. After
review and merge, the postmerge PR comment must compare the approved head to
the PR head accepted by the merge gate and record the distinct squash merge
commit SHA. This premerge document makes no claim about those future values.
