import asyncio
import importlib.util
import logging
from contextlib import suppress
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest
from agent_hub.database import database, initialize_database
from agent_hub.mcp import create_mcp
from agent_hub.store import ConflictError, EventRecord, HubStore, IdempotencyConflictError
from agent_hub_common import (
    AgentProfile,
    AgentStatus,
    EventKind,
    EventState,
    HubSettings,
    TaskState,
    WorkflowStatus,
)
from conftest import BASE_URL, TOKEN, MonotonicClock
from worker_mcp.client import WorkerHubClient
from worker_mcp.config import WorkerSettings

script_path = Path(__file__).resolve().parents[1] / "scripts" / "mock-alice.py"
spec = importlib.util.spec_from_file_location("mock_alice", script_path)
assert spec is not None and spec.loader is not None
mock_alice = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mock_alice)

# Every recovery test here turns on one thing: a lease expiring while Alice is
# gone. Both numbers live here so the next retune is one edit rather than nine,
# and so a test that sleeps is visibly a timing-sensitive one.
EVENT_LEASE_S = 1.0
PAST_LEASE_S = EVENT_LEASE_S + 0.05


@pytest.fixture
def settings(settings: HubSettings) -> HubSettings:
    """conftest's settings, retuned for the crash-and-recover holds.

    Overriding the fixture rather than building a second `HubSettings` means
    `app`, `hub_store` and `client` all come from conftest unchanged — and a
    test that drives the app never ends up talking to a second `HubStore` whose
    `Signals` nothing notifies (#53).
    """

    return replace(
        settings,
        default_wait_s=0.5,
        max_wait_s=5.0,
        event_lease_s=EVENT_LEASE_S,
    )


class FakeClock:
    """A clock the test advances by hand, so a lease expires without sleeping."""

    def __init__(self) -> None:
        self.now = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += timedelta(seconds=seconds)


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def timed_store(settings: HubSettings, clock: FakeClock) -> HubStore:
    """A standalone store on a hand-advanced clock, for the lease arithmetic."""

    initialize_database(settings.database_path)
    store = HubStore(settings.database_path, clock=clock)
    store.initialize_workflow()
    return store


@pytest.fixture
def worker() -> WorkerSettings:
    return WorkerSettings(
        hub_url=BASE_URL,
        token=TOKEN,
        agent_name="bob",
        profile=AgentProfile(harness="claude-code"),
        default_wait_s=0.5,
        max_retries=2,
        backoff_factor_s=0.01,
    )


async def run_worker_lifecycle(client: WorkerHubClient, *, ask: bool = False) -> None:
    """Check in, take one task through to done, and wait to be released.

    Every crash test needs a worker that keeps going while Alice dies and
    restarts; `ask` adds the question the reply-crash cases turn on.
    """

    await client.check_in(["python"])
    while True:
        assignment = await client.await_assignment(timeout_s=5.0)
        if not assignment.get("timeout"):
            break
    task_id = assignment["task_id"]
    if ask:
        while True:
            answer = await client.ask_alice(task_id, "Should I proceed?", timeout_s=5.0)
            if not answer.get("timeout"):
                break
        assert "Approved" in answer.get("reply", "")
    else:
        await client.report_progress(task_id, "Working...")
    await client.submit_result(
        task_id,
        "completed",
        "Work done",
        artifacts=[{"name": "pr", "url": "https://github.com/repo/pull/1"}],
    )
    while True:
        released = await client.await_assignment(timeout_s=5.0)
        if not released.get("timeout"):
            break
    assert released == {"release": True}


def test_event_delivery_and_implicit_ack_store(store: HubStore) -> None:

    # 1. Enqueue event
    event = store.append_event(EventKind.AGENT_CHECKED_IN, {"agent": "bob"})
    assert event.state == EventState.QUEUED
    assert event.delivery_id is None
    assert event.delivery_attempts == 0
    assert store.pending_events() == 1

    state = store.get_state()
    assert state["queued_events"] == 1
    assert len(state["unacked_delivered"]) == 0

    # 2. Lease event
    leased = store.lease_next_event(lease_s=5.0)
    assert leased is not None
    assert leased.id == event.id
    assert leased.state == EventState.DELIVERED
    assert leased.delivery_attempts == 1
    assert leased.delivery_id is not None
    assert leased.delivered_at is not None
    assert leased.delivery_expires is not None
    assert store.pending_events() == 0

    state = store.get_state()
    assert state["queued_events"] == 0
    assert len(state["unacked_delivered"]) == 1
    assert state["unacked_delivered"][0]["delivery_id"] == leased.delivery_id

    # Cannot re-lease while active
    assert store.lease_next_event() is None

    # 3. Ack event
    acked = store.ack_event(leased.delivery_id)
    assert acked is True

    with database(store.path) as conn:
        row = conn.execute("SELECT * FROM event WHERE id = ?", (event.id,)).fetchone()
        assert row["state"] == "acked"
        assert row["acked_at"] is not None

    state = store.get_state()
    assert state["queued_events"] == 0
    assert len(state["unacked_delivered"]) == 0


def test_event_redelivery_after_lease_expiry(timed_store: HubStore, clock: FakeClock) -> None:
    store = timed_store

    store.append_event(EventKind.AGENT_CHECKED_IN, {"agent": "alice"})

    # First lease with 5s duration
    first_delivery = store.lease_next_event(lease_s=5.0)
    assert first_delivery is not None
    assert first_delivery.delivery_attempts == 1
    old_delivery_id = first_delivery.delivery_id

    # Immediate second lease returns nothing
    assert store.lease_next_event() is None

    # Advance clock past lease expiry
    clock.advance(6.0)

    # Redelivered!
    second_delivery = store.lease_next_event(lease_s=10.0)
    assert second_delivery is not None
    assert second_delivery.id == first_delivery.id
    assert second_delivery.delivery_attempts == 2
    assert second_delivery.delivery_id != old_delivery_id

    # Ack new delivery
    assert store.ack_event(second_delivery.delivery_id) is True
    # Can no longer lease
    assert store.lease_next_event() is None


def test_ack_with_a_wrong_delivery_id_is_ignored(timed_store: HubStore) -> None:
    store = timed_store

    store.append_event(EventKind.AGENT_CHECKED_IN, {"agent": "bob"})
    leased = store.lease_next_event(lease_s=5.0)
    assert leased is not None

    assert store.ack_event("bogus-delivery-id") is False
    with database(store.path) as conn:
        row = conn.execute("SELECT * FROM event WHERE id = ?", (leased.id,)).fetchone()
        assert row["state"] == "delivered"

    assert store.ack_event(leased.delivery_id) is True


def test_a_late_ack_still_acks_an_event_nobody_re_leased(
    timed_store: HubStore, clock: FakeClock, caplog: pytest.LogCaptureFixture
) -> None:
    """#50: the delivery_id proves the caller holds the latest delivery.

    An action can outlive its lease — the §5 MERGE window waits on CI — and
    redoing it afterwards is worse than acking late.
    """

    store = timed_store

    store.append_event(EventKind.TASK_COMPLETED, {"agent": "bob"})
    leased = store.lease_next_event(lease_s=5.0)
    assert leased is not None
    clock.advance(6.0)

    with caplog.at_level(logging.WARNING, logger="agent_hub.store"):
        assert store.ack_event(leased.delivery_id) is True

    with database(store.path) as conn:
        row = conn.execute("SELECT * FROM event WHERE id = ?", (leased.id,)).fetchone()
        assert row["state"] == "acked"
    # No redelivery: the work was finished, just slowly.
    assert store.lease_next_event() is None
    assert "Late ack" in caplog.text and "1.0 s past" in caplog.text


def test_an_ack_from_a_superseded_delivery_is_still_ignored(
    timed_store: HubStore, clock: FakeClock
) -> None:
    """A re-lease rotates delivery_id, so the stale holder cannot ack the new one."""

    store = timed_store

    store.append_event(EventKind.AGENT_CHECKED_IN, {"agent": "bob"})
    leased = store.lease_next_event(lease_s=5.0)
    assert leased is not None
    clock.advance(6.0)
    re_leased = store.lease_next_event(lease_s=5.0)
    assert re_leased is not None
    assert re_leased.delivery_attempts == 2
    assert re_leased.delivery_id != leased.delivery_id

    assert store.ack_event(leased.delivery_id) is False
    with database(store.path) as conn:
        row = conn.execute("SELECT * FROM event WHERE id = ?", (leased.id,)).fetchone()
        assert row["state"] == "delivered"

    # The current delivery is still the one that can ack it.
    assert store.ack_event(re_leased.delivery_id) is True


def test_fifo_ordering_expired_before_newer(timed_store: HubStore, clock: FakeClock) -> None:
    store = timed_store

    # Enqueue 1 and 2
    e1 = store.append_event(EventKind.AGENT_CHECKED_IN, {"agent": "bob"})
    e2 = store.append_event(EventKind.TASK_PROGRESS, {"note": "step 1"})

    # Lease e1 with 5s expiry
    l1 = store.lease_next_event(lease_s=5.0)
    assert l1 is not None and l1.id == e1.id

    # Advance clock past e1 expiry
    clock.advance(6.0)

    # Enqueue e3
    e3 = store.append_event(EventKind.TASK_PROGRESS, {"note": "step 2"})

    # Next lease must be e1 (expired, lowest id), then e2 (queued), then e3 (queued)
    rl1 = store.lease_next_event(lease_s=5.0)
    assert rl1 is not None and rl1.id == e1.id
    assert rl1.delivery_attempts == 2
    assert store.ack_event(rl1.delivery_id) is True

    l2 = store.lease_next_event(lease_s=5.0)
    assert l2 is not None and l2.id == e2.id
    assert store.ack_event(l2.delivery_id) is True

    l3 = store.lease_next_event(lease_s=5.0)
    assert l3 is not None and l3.id == e3.id
    assert store.ack_event(l3.delivery_id) is True

    assert store.lease_next_event() is None


def test_events_survive_hub_restart(timed_store: HubStore, clock: FakeClock) -> None:
    # Two stores on one database is the point here: it is what a hub restart
    # looks like, and the second must pick up what the first left leased.
    store1 = timed_store
    e1 = store1.append_event(EventKind.AGENT_CHECKED_IN, {"agent": "bob"})
    store1.append_event(EventKind.TASK_PROGRESS, {"note": "started"})
    l1 = store1.lease_next_event(lease_s=5.0)
    assert l1 is not None and l1.id == e1.id

    # Hub crashes / restarts: Advance time and Store 2 opens the same db
    clock.advance(6.0)
    store2 = HubStore(store1.path, clock=clock)

    state = store2.get_state()
    assert state["queued_events"] == 1
    assert len(state["unacked_delivered"]) == 1

    # Store 2 leases next event -> gets expired e1 with attempt 2
    l1_re = store2.lease_next_event(lease_s=5.0)
    assert l1_re is not None and l1_re.id == e1.id
    assert l1_re.delivery_attempts == 2
    assert store2.ack_event(l1_re.delivery_id) is True

    # Next event is the queued task_progress
    l2 = store2.lease_next_event(lease_s=5.0)
    assert l2 is not None and l2.kind == EventKind.TASK_PROGRESS
    assert store2.ack_event(l2.delivery_id) is True


def test_state_guards_idempotency(store: HubStore) -> None:

    # 1. log_decision deduplication on key
    d1 = store.log_decision("Summary A", "Rationale A", key="key-1")
    d2 = store.log_decision("Summary A", "Rationale A", key="key-1")
    assert d1 == d2
    with database(store.path) as conn:
        count = conn.execute("SELECT COUNT(*) AS n FROM decision WHERE key = 'key-1'").fetchone()[
            "n"
        ]
        assert count == 1

    # 2. set_workflow_status duplicate audit entries
    store.set_workflow_status(WorkflowStatus.PAUSED, "Paused work")
    store.set_workflow_status(WorkflowStatus.PAUSED, "Paused work again")
    with database(store.path) as conn:
        audit_rows = conn.execute(
            "SELECT * FROM decision WHERE rationale LIKE '%Workflow status set to paused%'"
        ).fetchall()
        assert len(audit_rows) == 1

    # 3. assign_task guard when agent is busy
    store.check_in("worker-1", AgentProfile())
    task = store.assign_task("worker-1", "implementer", "Task 1", "Instructions")
    with pytest.raises(ConflictError, match="already holds task"):
        store.assign_task("worker-1", "reviewer", "Task 2", "Instructions")

    # 4. reply guard when task not in INPUT_REQUIRED
    # task is in WORKING state; reply should return False and not write messages
    with database(store.path) as conn:
        before_count = conn.execute("SELECT COUNT(*) AS n FROM message").fetchone()["n"]
    applied = store.reply(task.id, "Late answer", message_id=0)
    assert applied is False
    with database(store.path) as conn:
        after_count = conn.execute("SELECT COUNT(*) AS n FROM message").fetchone()["n"]
    assert after_count == before_count

    # Ask question -> task enters INPUT_REQUIRED
    q_msg_id = store.open_question(task.id, "worker-1", "Need clarification", sent_as="msg-1")
    # First reply applies
    assert store.reply(task.id, "Here is the answer", message_id=q_msg_id) is True
    # Duplicate reply with same message_id returns False
    assert store.reply(task.id, "Duplicate answer", message_id=q_msg_id) is False

    # Terminal task safely no-ops with False
    store.set_task_state(task.id, TaskState.CANCELED, "Canceled")
    assert store.reply(task.id, "Late answer on canceled task", message_id=0) is False
    task2 = store.assign_task("worker-1", "implementer", "Task 2", "Instructions")
    store.set_task_state(task2.id, TaskState.FAILED, "Failed")
    assert store.reply(task2.id, "Late answer on failed task", message_id=0) is False

    # 5. release_agent guard when agent is already released
    store.release_agent("worker-1")
    agent = store.agent_by_name("worker-1")
    assert agent is not None and agent.status == AgentStatus.RELEASED
    # Second release is a safe no-op
    store.release_agent("worker-1")
    agent_second = store.agent_by_name("worker-1")
    assert agent_second is not None and agent_second.status == AgentStatus.RELEASED


def test_assignment_replay_is_bound_to_its_source_event(store: HubStore) -> None:
    store.check_in("bob", AgentProfile(harness="claude-code"))
    source = store.lease_next_event()
    assert source is not None and source.kind is EventKind.AGENT_CHECKED_IN

    original = store.assign_task(
        "bob",
        "implementer",
        "IMPLEMENT for RoboNater/example#1",
        "Do it",
        source_event_id=source.id,
    )
    store.submit_result(original.id, "bob", TaskState.COMPLETED, "Done")

    replay = store.assign_task(
        "bob",
        "implementer",
        "IMPLEMENT for RoboNater/example#1",
        "Do it",
        source_event_id=source.id,
    )
    assert replay.id == original.id
    assert len(store.tasks()) == 1

    with pytest.raises(IdempotencyConflictError, match="different payload"):
        store.assign_task(
            "bob",
            "implementer",
            "A different task",
            "Do something else",
            source_event_id=source.id,
        )


async def test_mcp_wait_for_event_and_implicit_ack(store: HubStore) -> None:
    server = create_mcp(store)

    async def call(name: str, **args: Any) -> Any:
        result = await server.call_tool(name, args)
        assert isinstance(result, tuple)
        return result[1]

    # Enqueue event
    store.append_event(EventKind.AGENT_CHECKED_IN, {"agent": "bob"})

    # Wait for event via MCP
    res1 = await call("wait_for_event", timeout_s=1.0)
    event1 = res1.get("event")
    assert event1 is not None
    assert event1["kind"] == "agent_checked_in"
    assert event1["delivery_attempts"] == 1
    deliv_id = event1["delivery_id"]
    assert deliv_id is not None

    # Check get_state via MCP
    state1 = await call("get_state")
    assert state1["queued_events"] == 0
    assert len(state1["unacked_delivered"]) == 1
    assert state1["unacked_delivered"][0]["delivery_id"] == deliv_id

    # Wait for next event while acking the previous delivery
    res2 = await call("wait_for_event", timeout_s=0.1, ack=deliv_id)
    assert res2.get("event") is None

    state2 = await call("get_state")
    assert len(state2["unacked_delivered"]) == 0


@pytest.mark.parametrize(
    ("crash_at", "asks"),
    [
        ("after_action", False),
        ("delivery", False),
        ("before_ack", False),
        ("after_reply", True),
    ],
)
async def test_alice_crashes_and_a_second_session_finishes_the_task(
    crash_at: str,
    asks: bool,
    client: httpx.AsyncClient,
    hub_store: HubStore,
    worker: WorkerSettings,
) -> None:
    """Alice dies at each point in handling an event; the work still completes.

    Whatever she had done before dying, the unacked event is redelivered to the
    next session, which must reach the same end state without doing anything
    twice — one task, one assignment decision, one reply.
    """

    hub_store.clock = MonotonicClock()
    worker_client = WorkerHubClient(worker, http_client=client)
    worker_task = asyncio.create_task(run_worker_lifecycle(worker_client, ask=asks))

    with pytest.raises(mock_alice.AliceCrashError):
        await mock_alice.drive_one_task(
            store=hub_store,
            expected_agent="bob",
            expected_harness="claude-code",
            timeout_s=5.0,
            crash_at=crash_at,
        )

    # Nobody acked, so the lease has to lapse before the event comes back.
    await asyncio.sleep(PAST_LEASE_S)

    result = await mock_alice.drive_one_task(
        store=hub_store,
        expected_agent="bob",
        expected_harness="claude-code",
        timeout_s=5.0,
    )
    await worker_task

    assert result is not None
    assert hub_store.get_state()["workflow"]["status"] == WorkflowStatus.DONE.value

    with database(hub_store.path) as conn:
        tasks = conn.execute("SELECT * FROM task").fetchall()
        assert len(tasks) == 1

        redelivered = conn.execute(
            "SELECT * FROM event WHERE kind = ?",
            (EventKind.WORKER_QUESTION.value if asks else EventKind.AGENT_CHECKED_IN.value,),
        ).fetchone()
        assert redelivered is not None
        assert redelivered["delivery_attempts"] == 2
        assert redelivered["state"] == "acked"

        assignments = conn.execute(
            "SELECT * FROM decision WHERE key LIKE 'event:%:assign'"
        ).fetchall()
        assert len(assignments) == 1

        if asks:
            # The question was answered once, by the session that crashed; the
            # redelivery must not put a second answer on the wire.
            replies = conn.execute(
                "SELECT * FROM message WHERE direction = 'from_alice'"
                " AND parts_json LIKE '%\"hub.kind\": \"reply\"%'"
            ).fetchall()
            assert len(replies) == 1
            from_alice = conn.execute(
                "SELECT * FROM message WHERE direction = 'from_alice'"
            ).fetchall()
            assert len(from_alice) == 2


# --- the redelivery property -------------------------------------------------
#
# #25's guards were specified per tool ("`reply` to a task not `input-required`
# → no-op"), so they were implemented and tested per tool, and the cases that do
# not fit that shape slipped through. The invariant they exist for is one
# sentence — replaying a delivered event must not change state twice — and the
# harness below states it once. A new event kind joins by adding a parameter.

# Every state a replay could duplicate: a task, a message on the wire, an
# audit decision, or a workflow transition.
Fingerprint = tuple[tuple[str, int], ...]


def _fingerprint(store: HubStore) -> Fingerprint:
    with database(store.path) as connection:
        counts = tuple(
            (table, connection.execute(f"SELECT COUNT(*) AS n FROM {table}").fetchone()["n"])
            for table in ("task", "message", "decision")
        )
    # No workflow exists until the first assignment, and "none" is as much a
    # status as any other: a replay must not conjure one either.
    workflow = store.get_state()["workflow"]
    status = "none" if workflow is None else workflow["status"]
    return (*counts, (f"workflow_status:{status}", 1))


def _drain(store: HubStore) -> None:
    """Ack everything queued so far, so the test leases only the event it set up."""

    while (event := store.lease_next_event()) is not None:
        assert store.ack_event(event.delivery_id) is True


def _alice_acts(store: HubStore, event: EventRecord) -> None:
    """The mutation §5 has Alice make for this event kind.

    Deliberately without Alice's own recovery discipline — no reading
    `get_state` first to notice she already assigned, no checkpoint key beyond
    the one derived from the event id, which a redelivery preserves. What is
    under test is what the hub's guards allow, not what a careful caller
    manages to avoid asking for.
    """

    payload = event.payload
    if event.kind is EventKind.AGENT_CHECKED_IN:
        # Refusing a second task for a busy worker is the guard working.
        with suppress(ConflictError):
            store.assign_task(
                payload["agent"],
                "implementer",
                "Fix it",
                "Please fix it.",
                source_event_id=event.id,
            )
    elif event.kind is EventKind.WORKER_QUESTION:
        store.reply(payload["task_id"], "Approved.", message_id=payload["message_id"])
    elif event.kind in (EventKind.TASK_COMPLETED, EventKind.TASK_FAILED):
        store.release_agent(payload["agent"])
        store.set_workflow_status(WorkflowStatus.DONE, f"Task {payload['task_id']} finished")
    elif event.kind is EventKind.TASK_PROGRESS:
        pass  # Alice reads progress; she writes nothing on it.
    else:
        # `lease_expired` and `agent_lost` have no hub-side mutation yet; the
        # audit entry is the whole action, and its key is the only guard.
        store.log_decision(
            f"Handled {event.kind.value}",
            "Recovery path (§5)",
            key=f"event:{event.id}:{event.kind.value}",
        )


def _prepare(store: HubStore, clock: FakeClock, scenario: str) -> EventKind:
    """Leave exactly one unacked event queued, and say which kind it is."""

    store.check_in("bob", AgentProfile(harness="claude-code"))
    if scenario.startswith("agent_checked_in"):
        return EventKind.AGENT_CHECKED_IN

    _drain(store)
    if scenario == "agent_lost":
        clock.advance(600.0)
        store.sweep(lost_after_s=60.0)
        return EventKind.AGENT_LOST

    task = store.assign_task("bob", "implementer", "Fix it", "Please fix it.", lease_min=1)
    if scenario == "lease_expired":
        clock.advance(120.0)
        # A ceiling no heartbeat can breach, so only the lease expires here.
        store.sweep(lost_after_s=1_000_000.0)
        return EventKind.LEASE_EXPIRED
    if scenario == "task_progress":
        store.record_progress(task.id, "bob", "Working...")
        return EventKind.TASK_PROGRESS
    if scenario.startswith("worker_question"):
        store.open_question(task.id, "bob", "Proceed?", sent_as="q-1")
        return EventKind.WORKER_QUESTION
    if scenario == "task_completed":
        store.submit_result(task.id, "bob", TaskState.COMPLETED, "Done")
        return EventKind.TASK_COMPLETED
    if scenario == "task_failed":
        store.submit_result(task.id, "bob", TaskState.FAILED, "Broken")
        return EventKind.TASK_FAILED
    raise AssertionError(f"no setup for scenario {scenario!r}")


def _interleaves(store: HubStore, scenario: str) -> None:
    """What the worker does in the gap while Alice is dead."""

    if scenario == "agent_checked_in_after_completion":
        # The task Alice just assigned finishes, so the worker is idle again by
        # the time her check-in event comes back — #51's second gap.
        task = store.tasks()[-1]
        store.submit_result(task.id, "bob", TaskState.COMPLETED, "Done")
    elif scenario == "worker_question_then_another":
        # The interleaving that produced the wrong answer during #49's review:
        # a newer question is open when the older one is redelivered.
        task = store.tasks()[-1]
        store.open_question(task.id, "bob", "And this one?", sent_as="q-2")


@pytest.mark.parametrize(
    "scenario",
    [
        "agent_checked_in",
        "agent_checked_in_after_completion",
        "task_progress",
        "worker_question",
        "worker_question_then_another",
        "task_completed",
        "task_failed",
        "lease_expired",
        "agent_lost",
    ],
)
def test_replaying_a_delivered_event_changes_nothing_twice(
    timed_store: HubStore, clock: FakeClock, scenario: str
) -> None:
    store = timed_store
    expected_kind = _prepare(store, clock, scenario)

    delivered = store.lease_next_event(lease_s=EVENT_LEASE_S)
    assert delivered is not None
    assert delivered.kind is expected_kind

    _alice_acts(store, delivered)
    _interleaves(store, scenario)
    settled = _fingerprint(store)

    # Alice dies without acking, and the lease lapses.
    clock.advance(EVENT_LEASE_S + 1.0)
    redelivered = store.lease_next_event(lease_s=EVENT_LEASE_S)
    assert redelivered is not None
    assert redelivered.id == delivered.id
    assert redelivered.delivery_attempts == 2

    _alice_acts(store, redelivered)

    assert _fingerprint(store) == settled


def test_reply_requires_and_guards_the_newest_question_id(store: HubStore) -> None:
    """A redelivered answer to q1 can never become the answer to q2 (#51)."""

    store.check_in("bob", AgentProfile(harness="claude-code"))
    task = store.assign_task("bob", "implementer", "Fix it", "Please fix it.")

    q1 = store.open_question(task.id, "bob", "Ship it as drafted?", sent_as="q-1")
    assert store.reply(task.id, "answer-to-q1", message_id=q1) is True
    q2 = store.open_question(task.id, "bob", "And rename the module?", sent_as="q-2")

    with pytest.raises(TypeError, match="message_id"):
        store.reply(task.id, "answer-to-q1")  # type: ignore[call-arg]
    assert store.reply(task.id, "answer-to-q1", message_id=q1) is False

    assert store.pending_reply(task.id, q2) is None
