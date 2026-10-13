# Host standby during a run (#19)

The hub compares wall time with monotonic time between sweeps. A forward
wall-clock discrepancy over 5 seconds records a `host_paused` inbox event.
When both clocks advance through sleep, a gap between sweeps greater than
`HUB_LOST_AFTER_S` also records the event (`reason: sweep_gap`). A long delay in
scheduling the sweeper receives the same treatment. This diagnostic does not
prove which mechanism caused the gap.

From detection the hub grants `HUB_LOST_AFTER_S` seconds of monotonic grace.
During grace, it accepts heartbeats, holds overdue task leases and event
redeliveries, and suppresses stall assessments. New queued events still reach
Alice. Existing stall episodes remain unchanged. A repeated pause restarts
grace. When grace ends, ordinary checks resume: a worker that heartbeats is
live; a silent worker is lost; an overdue task lease is reported to Alice and
an overdue event delivery becomes eligible for redelivery. Task leases that
were live before a forward jump are shifted by the wall/monotonic discrepancy.
Their durable renewal-cap anchor shifts by the same amount, so
`max_task_lease_min` still bounds active time. On a long gap with matching clocks,
the whole gap is conservatively excluded, bounded by each task's age. Already
expired leases and tasks created after a wall-clock jump are not extended.
The lease shifts and pause event commit together. Resumed heartbeats can renew
these leases normally, including after the original wall-clock cap has passed.
The pause event includes the two elapsed times, their difference, the grace
length, the lease shift and how many task leases were shifted;
the hub also logs that payload to stderr.

Stall ages still include the pause after grace ends; excluding it is tracked
in [#202](https://github.com/RoboNater/robomate/issues/202). Recovery when a
still-running bridge already receives rejected heartbeats is tracked in
[#203](https://github.com/RoboNater/robomate/issues/203). Alice's orchestrator
skill explains how to handle `host_paused` and inspect later loss/expiry.

Detection starts with the first sweep of this hub process. A restart has no
previous in-memory clock sample; crash/restart reconciliation remains #18.
Grace protects the hub's deadlines, but cannot prevent a harness from timing
out its own MCP call during standby. Keeping the host awake remains useful
for unattended runs. On Modern Standby laptops, display-off may also enter
standby; check the display and sleep settings for both AC and battery power.
AC-only settings do not cover loss of external power.

## Operator live acceptance

Run once with the hub in WSL2 and once with a native Windows hub. These are
operator checks on a Modern Standby host; deterministic tests simulate both
clock behaviours but do not establish actual hardware behaviour.

1. Start a fresh run with `robomate up`, launch a worker, and assign a task.
   Also check an idle worker with an already completed deliverable. Allow at
   least two normal sweep intervals before sleeping.
2. Let the host enter standby for more than `HUB_LOST_AFTER_S`, then wake it.
3. Inspect Alice's inbox for `host_paused` and the hub's stderr log. Record
   `reason`, `wall_elapsed_s`, `monotonic_elapsed_s`, `clock_jump_s` and
   `grace_s`. The worker's first resumed heartbeat should be accepted, and
   its task should remain assigned. Neither new stall episodes nor lease
   expiry/redelivery should occur during grace.
4. Confirm standby in PowerShell with
   `Get-WinEvent -FilterHashtable @{LogName='System'; Id=506,507}`.
5. Leave one worker heartbeating and stop a separate disposable worker through
   the operator's normal controls. After grace, the former should remain live
   and the latter should be lost once. Overdue task leases should be reported
   once; an unacknowledged overdue event should be redelivered.
6. Stop the test run with `robomate down` and retain the log and event evidence.

A worker performing an optional manual entry-point smoke check must instead
follow the isolated-hub limits in [the worker guide](../../guides/worker.md).
A worker never sleeps the run's host or starts/stops the run's hub or agents.
