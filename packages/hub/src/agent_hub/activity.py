"""What each agent is doing on the hub, apart from whether its bridge is alive (#144).

A bridge heartbeat says only that the MCP bridge process is running; a harness
whose model is rate-limited, hung or finished keeps it running all the same.
This module keeps the other half in memory: when each agent last made a
substantive hub call, and which of its calls are still being held. The sweeper
and `hub.status` combine that with durable task, message and event rows into a
stall assessment, which sits beside the agent's lifecycle status rather than
replacing it.

What counts:

- Substantive: a worker's check-in, NEXT poll, progress, question and result,
  and every orchestrator operation on `/rpc`. Heartbeats, accounting uploads,
  launcher polling and the operator's status, snapshot and question reads do
  not.
- Attributed to the current worker instance or orchestrator RPC session only.
  A superseded instance's late return can neither refresh nor clear the
  current one's record.
- A hold (NEXT, a worker question, `wait_for_event`, any orchestrator call in
  flight) exempts its agent from the silence check until it returns, errors,
  is cancelled, or passes its deadline plus `HOLD_GRACE_S`.

Nothing here is persisted. A restarted hub starts every silence clock at its
own start and knows no holds until the agents call again, so it never reports
a hold it did not see; a stall is therefore reported at the earliest one
threshold after a restart. Stall episodes, which the operator report reads,
are durable in `stall_episode`.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from itertools import count

# How long past its deadline a hold still exempts its agent. A held response
# whose client vanished is cleaned up when the server notices, which may be a
# little after the deadline; past this, the hold is ignored.
HOLD_GRACE_S = 30.0

# Reasons, stable strings that status JSON, events and the report carry.
NO_HUB_CALL = "no_hub_call"
NO_TASK_PROGRESS = "no_task_progress"
EVENT_BACKLOG = "event_backlog"


@dataclass(slots=True)
class Hold:
    kind: str
    started: datetime
    deadline: datetime


@dataclass(slots=True)
class Activity:
    """One worker instance's or orchestrator session's recent hub calls."""

    instance: str
    last_call: datetime | None = None
    last_call_kind: str | None = None
    holds: dict[int, Hold] = field(default_factory=dict)
    heartbeat: datetime | None = None

    def call(self, kind: str, now: datetime) -> None:
        if self.last_call is None or now >= self.last_call:
            self.last_call = now
            self.last_call_kind = kind

    def active_hold(self, now: datetime) -> Hold | None:
        """The oldest hold still within its deadline, if any."""

        live = [hold for hold in self.holds.values() if now <= hold.deadline]
        return min(live, key=lambda hold: hold.started) if live else None


class ActivityTracker:
    """In-memory activity per worker name and per orchestrator session."""

    def __init__(self) -> None:
        self.started: datetime | None = None
        # When the workflow last left `paused`, as this process saw it.
        self.resumed_at: datetime | None = None
        # Whether a stall sweep has reconciled the episodes a previous hub
        # process left open.
        self.reconciled = False
        self.orchestrator_actor: str | None = None
        self.orchestrator_session: str | None = None
        self._workers: dict[str, Activity] = {}
        self._sessions: dict[str, Activity] = {}
        self._ids = count(1)

    def mark_started(self, now: datetime) -> None:
        if self.started is None:
            self.started = now

    def note_resumed(self, now: datetime) -> None:
        self.resumed_at = now

    # -- workers -------------------------------------------------------------

    def _worker(self, name: str, instance: str) -> Activity:
        record = self._workers.get(name)
        if record is None or record.instance != instance:
            # A new instance starts with a clean record; the old one's holds
            # belong to a process the hub no longer talks to.
            record = Activity(instance)
            self._workers[name] = record
        return record

    def worker_call(self, name: str, instance: str, kind: str, now: datetime) -> None:
        self.mark_started(now)
        self._worker(name, instance).call(kind, now)

    def worker(self, name: str, instance: str) -> Activity | None:
        record = self._workers.get(name)
        return record if record is not None and record.instance == instance else None

    @contextmanager
    def worker_hold(
        self, name: str, instance: str, kind: str, timeout_s: float, clock: Callable[[], datetime]
    ) -> Iterator[None]:
        now = clock()
        self.worker_call(name, instance, kind, now)
        record = self._worker(name, instance)
        hold_id = next(self._ids)
        record.holds[hold_id] = Hold(kind, now, _deadline(now, timeout_s))
        try:
            yield
        finally:
            record.holds.pop(hold_id, None)
            # Returning from a hold is activity too, so a wait/timeout/retry
            # cycle never ages; but only for the instance that is still current.
            if self.worker(name, instance) is record:
                record.call(kind, clock())

    # -- orchestrator ----------------------------------------------------------

    def orchestrator_accepted(self, actor: str, session: str, now: datetime) -> None:
        """Record the session `/rpc` accepted as current; drop superseded ones."""

        self.mark_started(now)
        if session != self.orchestrator_session:
            self._sessions = {key: value for key, value in self._sessions.items() if key == session}
            self.orchestrator_session = session
        self.orchestrator_actor = actor
        self._sessions.setdefault(session, Activity(session))

    def orchestrator(self) -> Activity | None:
        if self.orchestrator_session is None:
            return None
        return self._sessions.get(self.orchestrator_session)

    def orchestrator_heartbeat(self, session: str, now: datetime) -> None:
        record = self._sessions.get(session)
        if record is not None and session == self.orchestrator_session:
            record.heartbeat = now

    @contextmanager
    def orchestrator_call(
        self, session: str, kind: str, timeout_s: float, clock: Callable[[], datetime]
    ) -> Iterator[None]:
        """Hold for one orchestrator operation, from its start until it returns."""

        now = clock()
        record = self._sessions.get(session)
        if record is None or session != self.orchestrator_session:
            yield
            return
        record.call(kind, now)
        hold_id = next(self._ids)
        record.holds[hold_id] = Hold(kind, now, _deadline(now, timeout_s))
        try:
            yield
        finally:
            record.holds.pop(hold_id, None)
            if session == self.orchestrator_session and self._sessions.get(session) is record:
                record.call(kind, clock())

    def silence_anchor(self, record: Activity | None, now: datetime) -> datetime:
        """The moment silence is measured from: the latest of call, start and resume."""

        candidates = [moment for moment in (self.started, self.resumed_at) if moment is not None]
        if record is not None and record.last_call is not None:
            candidates.append(record.last_call)
        return max(candidates) if candidates else now


def _deadline(now: datetime, timeout_s: float) -> datetime:
    return now + timedelta(seconds=max(0.0, timeout_s) + HOLD_GRACE_S)
