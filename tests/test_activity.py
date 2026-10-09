"""Agent activity and stall detection (#144): the tracker, the assessment, the sweep."""

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest
from agent_hub.activity import EVENT_BACKLOG, NO_HUB_CALL, NO_TASK_PROGRESS, ActivityTracker
from agent_hub.database import database, initialize_database
from agent_hub.protocol import A2AProtocol
from agent_hub.store import HubStore
from agent_hub_common import (
    AgentStatus,
    EventKind,
    HubSettings,
    MetaKeys,
    TaskState,
    WorkflowStatus,
)
from conftest import SESSION, check_in, message, rpc

SESSION_2 = "0b9d2f4e-1c3a-4e5f-8a7b-6c5d4e3f2a1b"


class FakeClock:
    def __init__(self) -> None:
        self.now = datetime(2026, 10, 8, 12, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.now

    def advance(self, **kwargs: float) -> None:
        self.now += timedelta(**kwargs)


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


def make_store(path: Path, clock: FakeClock, policy: dict[str, Any] | None = None) -> HubStore:
    initialize_database(path)
    store = HubStore(path, clock=clock)
    store.activity.mark_started(clock())
    store.initialize_workflow(policy=policy)
    return store


@pytest.fixture
def hub(tmp_path: Path, clock: FakeClock) -> HubStore:
    return make_store(tmp_path / "hub.db", clock)


def worker(store: HubStore, name: str = "bob") -> str:
    """Check a worker in as the protocol layer does; return its instance."""

    agent = store.check_in(name)
    store.note_worker_call(name, agent.worker_instance_id, "check_in")
    return str(agent.worker_instance_id)


def heartbeat(store: HubStore, name: str, instance: str) -> None:
    assert store.heartbeat(name, instance, None)


def assessed(store: HubStore, name: str) -> dict[str, Any]:
    return dict(store.assess_activity()["agents"][name])


def alice(store: HubStore) -> dict[str, Any]:
    return dict(store.assess_activity()["orchestrator"])


def orchestrate(store: HubStore, method: str = "get_state", session: str = SESSION) -> None:
    store.note_orchestrator_session("alice", session)
    with store.orchestrator_call(session, method, 75):
        pass


# -- workers ---------------------------------------------------------------------


def test_fresh_heartbeats_do_not_hide_a_silent_worker(hub: HubStore, clock: FakeClock) -> None:
    instance = worker(hub)
    for _ in range(21):
        clock.advance(minutes=1)
        heartbeat(hub, "bob", instance)

    state = assessed(hub, "bob")

    assert state["stalled"] and state["reasons"] == [NO_HUB_CALL]
    assert state["heartbeat_age_s"] == 0
    assert state["silence_s"] == 21 * 60
    assert state["threshold_s"] == 20 * 60
    assert hub.agent_by_name("bob").status is AgentStatus.IDLE  # type: ignore[union-attr]


def test_a_worker_held_in_await_assignment_is_not_stalled(hub: HubStore, clock: FakeClock) -> None:
    instance = worker(hub)
    with hub.worker_hold("bob", instance, "await_assignment", 100):
        clock.advance(seconds=100)
        state = assessed(hub, "bob")
    assert not state["stalled"] and state["holding"] == "await_assignment"


def test_repeated_hold_timeouts_never_age_into_a_stall(hub: HubStore, clock: FakeClock) -> None:
    instance = worker(hub)
    for _ in range(40):
        with hub.worker_hold("bob", instance, "await_assignment", 100):
            clock.advance(seconds=100)
        clock.advance(seconds=1)
        assert not assessed(hub, "bob")["stalled"]


def test_a_hold_past_its_deadline_stops_exempting(hub: HubStore, clock: FakeClock) -> None:
    instance = worker(hub)
    with hub.worker_hold("bob", instance, "await_assignment", 100):
        clock.advance(minutes=25)
        state = assessed(hub, "bob")
    assert state["stalled"] and state["holding"] is None


@pytest.mark.parametrize("error", [RuntimeError, KeyboardInterrupt])
def test_an_error_or_cancellation_clears_the_hold(
    hub: HubStore, clock: FakeClock, error: type[BaseException]
) -> None:
    instance = worker(hub)
    with pytest.raises(error), hub.worker_hold("bob", instance, "ask_alice", 100):
        raise error()
    record = hub.activity.worker("bob", instance)
    assert record is not None and record.holds == {}
    clock.advance(minutes=21)
    assert assessed(hub, "bob")["reasons"] == [NO_HUB_CALL]


async def test_cancelling_a_held_wait_clears_the_hold(hub: HubStore) -> None:
    import asyncio

    instance = worker(hub)
    agent = hub.agent_by_name("bob")
    assert agent is not None

    async def wait() -> None:
        with hub.worker_hold("bob", instance, "await_assignment", 100):
            await hub.await_assignment(agent.context_id, 100)

    task = asyncio.create_task(wait())
    await asyncio.sleep(0.05)
    assert hub.activity.worker("bob", instance).holds  # type: ignore[union-attr]
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert hub.activity.worker("bob", instance).holds == {}  # type: ignore[union-attr]


def test_a_superseded_instance_neither_refreshes_nor_clears_the_current_one(
    hub: HubStore, clock: FakeClock
) -> None:
    old = worker(hub)
    hold = hub.worker_hold("bob", old, "await_assignment", 100)
    hold.__enter__()
    # The old process stops; a new instance takes over after the lost window.
    clock.advance(minutes=4)
    hub.sweep(lost_after_s=180)
    new = worker(hub)
    assert new != old
    clock.advance(minutes=21)
    # The old instance's held call finally returns, long after it was replaced.
    hold.__exit__(None, None, None)

    state = assessed(hub, "bob")
    assert state["instance"] == new
    assert state["reasons"] == [NO_HUB_CALL]


def test_task_progress_is_measured_from_the_assignment(hub: HubStore, clock: FakeClock) -> None:
    instance = worker(hub)
    # An old task's timestamps must not make a fresh assignment look stale.
    first = hub.assign_task("bob", "implementer", "Old", "Do it")
    hub.cancel_task(first.id)
    clock.advance(minutes=30)
    task = hub.assign_task("bob", "implementer", "Fix", "Do it")
    with hub.worker_hold("bob", instance, "await_assignment", 100):
        pass

    clock.advance(minutes=19)
    hub.note_worker_call("bob", instance, "progress")
    assert assessed(hub, "bob")["progress_age_s"] == 19 * 60
    clock.advance(minutes=2)
    # A hub call alone is not task progress.
    hub.note_worker_call("bob", instance, "other")
    state = assessed(hub, "bob")
    assert state["reasons"] == [NO_TASK_PROGRESS] and state["task_id"] == task.id

    hub.record_progress(task.id, "bob", "tests pass")
    assert not assessed(hub, "bob")["stalled"]


def test_a_worker_waiting_for_alices_answer_is_not_stalled(hub: HubStore, clock: FakeClock) -> None:
    instance = worker(hub)
    task = hub.assign_task("bob", "implementer", "Fix", "Do it")
    hub.open_question(task.id, "bob", "Which branch?", "m-1")
    for _ in range(30):
        with hub.worker_hold("bob", instance, "ask_alice", 100):
            clock.advance(seconds=100)
    state = assessed(hub, "bob")
    assert not state["stalled"] and state["progress_age_s"] is None


def test_released_and_lost_workers_are_never_stalled(hub: HubStore, clock: FakeClock) -> None:
    worker(hub, "bob")
    worker(hub, "charlie")
    hub.release_agent("bob")
    clock.advance(minutes=30)
    heartbeat(hub, "charlie", hub.agent_by_name("charlie").worker_instance_id)  # type: ignore[union-attr]
    hub.sweep(lost_after_s=180)  # bob is released, so never lost
    hub.check_in("dave")
    clock.advance(minutes=4)
    hub.sweep(lost_after_s=180)

    agents = hub.assess_activity()["agents"]
    assert agents["bob"]["suppressed"] == "released" and not agents["bob"]["stalled"]
    assert agents["dave"]["suppressed"] == "lost" and not agents["dave"]["stalled"]


def test_the_policy_threshold_is_honoured(tmp_path: Path, clock: FakeClock) -> None:
    store = make_store(tmp_path / "hub.db", clock, {"stall_after_min": 2})
    worker(store)
    clock.advance(seconds=119)
    assert not assessed(store, "bob")["stalled"]
    clock.advance(seconds=1)
    state = assessed(store, "bob")
    assert state["stalled"] and state["threshold_s"] == 120
    assert store.assess_activity()["stall_after_min"] == 2


def test_the_threshold_defaults_to_twenty_minutes(hub: HubStore) -> None:
    assert hub.assess_activity()["stall_after_min"] == 20


# -- orchestrator ------------------------------------------------------------------


def test_alice_silent_with_a_fresh_heartbeat_is_stalled(hub: HubStore, clock: FakeClock) -> None:
    orchestrate(hub)
    for _ in range(42):
        clock.advance(seconds=30)
        hub.note_orchestrator_heartbeat(SESSION)
    state = alice(hub)
    assert state["stalled"] and state["reasons"] == [NO_HUB_CALL]
    assert state["heartbeat_age_s"] == 0 and state["session"] == SESSION
    assert state["last_call"] == "get_state"


def test_alice_holding_wait_for_event_is_not_stalled_even_while_escalated(
    hub: HubStore, clock: FakeClock
) -> None:
    orchestrate(hub)
    hub.ask_user("Merge?", None, actor="alice", session=SESSION)
    for _ in range(30):
        with hub.orchestrator_call(SESSION, "wait_for_event", 100):
            clock.advance(seconds=100)
        clock.advance(seconds=2)
        assert not alice(hub)["stalled"]
    assert hub.get_state()["workflow"]["status"] == WorkflowStatus.ESCALATED.value


def test_an_open_operator_question_is_no_blanket_exemption(hub: HubStore, clock: FakeClock) -> None:
    orchestrate(hub)
    hub.ask_user("Merge?", None, actor="alice", session=SESSION)
    clock.advance(minutes=21)
    assert alice(hub)["reasons"] == [NO_HUB_CALL]


def test_an_unconsumed_event_is_backlog_whatever_else_alice_does(
    hub: HubStore, clock: FakeClock
) -> None:
    orchestrate(hub)
    event = hub.append_event(EventKind.TASK_COMPLETED, {"task_id": "t"})
    clock.advance(minutes=21)
    # Unrelated calls keep her silence short but cannot reset the event's age.
    orchestrate(hub, "log_decision")
    state = alice(hub)
    assert state["reasons"] == [EVENT_BACKLOG]
    assert state["stale_events"] == [{"id": event.id, "kind": "task_completed", "age_s": 21 * 60}]
    # Once a delivery has been attempted it is no longer unconsumed backlog.
    leased = hub.lease_next_event()
    assert leased is not None and leased.id == event.id
    assert not alice(hub)["stalled"]


def test_alice_with_no_session_is_measured_from_hub_start(hub: HubStore, clock: FakeClock) -> None:
    clock.advance(minutes=20)
    state = alice(hub)
    assert state["reasons"] == [NO_HUB_CALL] and state["session"] is None


def test_a_superseded_session_does_not_refresh_alice(hub: HubStore, clock: FakeClock) -> None:
    orchestrate(hub, session=SESSION)
    old_call = hub.orchestrator_call(SESSION, "wait_for_event", 100)
    old_call.__enter__()
    orchestrate(hub, session=SESSION_2)
    clock.advance(minutes=21)
    old_call.__exit__(None, None, None)
    hub.note_orchestrator_heartbeat(SESSION)  # a late heartbeat from the old bridge
    with hub.orchestrator_call(SESSION, "get_state", 75):
        pass
    state = alice(hub)
    assert state["session"] == SESSION_2
    assert state["reasons"] == [NO_HUB_CALL] and state["heartbeat_age_s"] is None


# -- workflow states -----------------------------------------------------------------


def test_done_workflows_report_no_stalls(hub: HubStore, clock: FakeClock) -> None:
    worker(hub)
    hub.append_event(EventKind.TASK_COMPLETED, {"task_id": "t"})
    hub.set_workflow_status(WorkflowStatus.DONE, "Merged", actor="alice", session=SESSION)
    clock.advance(hours=3)
    report = hub.assess_activity()
    assert report["orchestrator"]["suppressed"] == "workflow_done"
    assert not report["orchestrator"]["stalled"]
    assert report["agents"]["bob"] == report["agents"]["bob"] | {
        "stalled": False,
        "suppressed": "workflow_done",
    }


def test_paused_workflows_suspend_and_resume_restarts_the_clocks(
    hub: HubStore, clock: FakeClock
) -> None:
    instance = worker(hub)
    hub.assign_task("bob", "implementer", "Fix", "Do it")
    hub.set_workflow_status(WorkflowStatus.PAUSED, "Hold", actor="alice", session=SESSION)
    clock.advance(hours=2)
    paused = hub.assess_activity()
    assert paused["agents"]["bob"]["suppressed"] == "workflow_paused"
    assert not paused["agents"]["bob"]["stalled"] and not paused["orchestrator"]["stalled"]
    assert hub.sweep_stalls() == []

    hub.set_workflow_status(WorkflowStatus.ACTIVE, "Go", actor="alice", session=SESSION)
    clock.advance(minutes=19)
    resumed = hub.assess_activity()["agents"]["bob"]
    assert not resumed["stalled"] and resumed["progress_age_s"] == 19 * 60
    clock.advance(minutes=1)
    hub.note_worker_call("bob", instance, "other")
    assert assessed(hub, "bob")["reasons"] == [NO_TASK_PROGRESS]


# -- sweep and episodes ----------------------------------------------------------------


def worker_episodes(store: HubStore, *, open_only: bool = False) -> list[dict[str, Any]]:
    return [e for e in store.stall_episodes(open_only=open_only) if e["role"] == "worker"]


def lifecycle(store: HubStore) -> tuple[Any, ...]:
    state = store.get_state()
    return (
        state["workflow"]["status"],
        [(agent["name"], agent["status"], agent["current_task_id"]) for agent in state["agents"]],
        [(task["id"], task["state"], task["lease_expires"]) for task in state["tasks"]],
    )


def test_one_event_per_worker_episode_then_recovery_and_a_second_episode(
    hub: HubStore, clock: FakeClock
) -> None:
    instance = worker(hub)
    task = hub.assign_task("bob", "implementer", "Fix", "Do it", lease_min=600)
    hub.sweep_stalls()
    clock.advance(minutes=21)
    heartbeat(hub, "bob", instance)
    before = lifecycle(hub)

    first = hub.sweep_stalls()
    assert [event.kind for event in first] == [EventKind.AGENT_STALLED]
    payload = first[0].payload
    assert payload["agent"] == "bob" and payload["task_id"] == task.id
    assert payload["reasons"] == [NO_HUB_CALL, NO_TASK_PROGRESS]
    assert payload["heartbeat_age_s"] == 0
    for _ in range(5):
        clock.advance(seconds=10)
        assert hub.sweep_stalls() == []
    assert lifecycle(hub) == before

    hub.record_progress(task.id, "bob", "still going")
    hub.note_worker_call("bob", instance, "progress")
    assert hub.sweep_stalls() == []
    episodes = worker_episodes(hub)
    assert len(episodes) == 1 and episodes[0]["cleared_reason"] == "recovered"
    assert episodes[0]["event_id"] == first[0].id

    clock.advance(minutes=21)
    second = hub.sweep_stalls()
    assert [event.kind for event in second] == [EventKind.AGENT_STALLED]
    assert second[0].payload["episode_id"] != payload["episode_id"]
    assert [episode["cleared"] is None for episode in worker_episodes(hub)] == [False, True]


def test_an_alice_stall_is_recorded_but_never_queued_to_her(
    hub: HubStore, clock: FakeClock
) -> None:
    orchestrate(hub)
    clock.advance(minutes=21)
    assert hub.sweep_stalls() == []
    (episode,) = hub.stall_episodes(open_only=True)
    assert episode["role"] == "orchestrator" and episode["actor"] == "alice"
    assert episode["reasons"] == [NO_HUB_CALL] and episode["event_id"] is None
    assert hub.pending_events() == 0

    orchestrate(hub, "wait_for_event")
    hub.sweep_stalls()
    assert hub.stall_episodes(open_only=True) == []


def test_episodes_close_when_a_worker_is_released_or_the_workflow_ends(
    hub: HubStore, clock: FakeClock
) -> None:
    worker(hub, "bob")
    worker(hub, "charlie")
    hub.sweep_stalls()
    clock.advance(minutes=21)
    assert len(hub.sweep_stalls()) == 2
    hub.release_agent("bob")
    hub.sweep_stalls()
    reasons = {e["actor"]: e["cleared_reason"] for e in hub.stall_episodes()}
    assert reasons == {"orchestrator": None, "bob": "released", "charlie": None}
    hub.set_workflow_status(WorkflowStatus.DONE, "Done", actor="alice", session=SESSION)
    hub.sweep_stalls()
    reasons = {e["actor"]: e["cleared_reason"] for e in hub.stall_episodes()}
    assert reasons == {
        "orchestrator": "workflow_done",
        "bob": "released",
        "charlie": "workflow_done",
    }


def test_a_restarted_hub_invents_no_holds_and_keeps_a_continuing_episode(
    tmp_path: Path, clock: FakeClock
) -> None:
    path = tmp_path / "hub.db"
    first = make_store(path, clock)
    instance = worker(first)
    task = first.assign_task("bob", "implementer", "Fix", "Do it", lease_min=600)
    hold = first.worker_hold("bob", instance, "await_assignment", 10_000)
    hold.__enter__()  # still held when the hub process dies
    first.sweep_stalls()
    clock.advance(minutes=21)
    # The hold exempts silence but not stale task progress.
    assert len(first.sweep_stalls()) == 1
    assert [e["reasons"] for e in worker_episodes(first, open_only=True)] == [[NO_TASK_PROGRESS]]

    restarted = HubStore(path, clock=clock)
    restarted.activity.mark_started(clock())
    state = assessed(restarted, "bob")
    assert state["holding"] is None and state["silence_s"] == 0
    assert state["reasons"] == [NO_TASK_PROGRESS] and state["task_id"] == task.id
    # The same episode continues: no second event for Alice.
    assert restarted.sweep_stalls() == []
    (episode,) = worker_episodes(restarted)
    assert episode["cleared"] is None


def test_a_restart_closes_episodes_that_no_longer_hold(tmp_path: Path, clock: FakeClock) -> None:
    path = tmp_path / "hub.db"
    first = make_store(path, clock)
    worker(first)
    first.sweep_stalls()
    clock.advance(minutes=21)
    assert len(first.sweep_stalls()) == 1

    restarted = HubStore(path, clock=clock)
    restarted.activity.mark_started(clock())
    assert restarted.sweep_stalls() == []
    assert {e["cleared_reason"] for e in restarted.stall_episodes()} == {"hub_restarted"}


def test_no_workflow_means_no_assessment(tmp_path: Path, clock: FakeClock) -> None:
    initialize_database(tmp_path / "hub.db")
    store = HubStore(tmp_path / "hub.db", clock=clock)
    clock.advance(hours=1)
    assert store.assess_activity()["orchestrator"] is None
    assert store.sweep_stalls() == []


def test_the_tracker_alone_needs_no_database() -> None:
    tracker = ActivityTracker()
    now = datetime(2026, 10, 8, tzinfo=UTC)
    tracker.worker_call("bob", "i-1", "check_in", now)
    tracker.worker_call("bob", "i-2", "check_in", now + timedelta(seconds=5))
    assert tracker.worker("bob", "i-1") is None
    assert tracker.worker("bob", "i-2").last_call == now + timedelta(seconds=5)  # type: ignore[union-attr]


# -- over the wire, with call accounting off -------------------------------------------


async def test_wire_calls_count_and_heartbeats_and_reads_do_not(
    client: httpx.AsyncClient, hub_store: HubStore
) -> None:
    context_id = await check_in(client, "bob")
    record = hub_store.activity.worker("bob", "test-worker-instance")
    assert record is not None and record.last_call_kind == "check_in"
    called = record.last_call

    response = await client.post(
        "/a2a",
        json=rpc(
            "message/send",
            message(
                "HEARTBEAT",
                metadata={
                    MetaKeys.KIND: "heartbeat",
                    MetaKeys.AGENT: "bob",
                    MetaKeys.SCHEMA_VERSION: 1,
                    MetaKeys.WORKER_INSTANCE_ID: "test-worker-instance",
                },
            ),
        ),
    )
    response.raise_for_status()
    for method in ("hub.status", "hub.questions", "hub.heartbeat", "hub.info"):
        await client.post("/rpc", json=rpc(method, {}))
    assert record.last_call == called and record.last_call_kind == "check_in"
    orchestrator = hub_store.activity.orchestrator()
    assert orchestrator is not None and orchestrator.last_call is None
    assert orchestrator.heartbeat is not None

    response = await client.post(
        "/a2a",
        json=rpc(
            "message/stream",
            message("NEXT", context_id=context_id, metadata={MetaKeys.TIMEOUT_S: 0.05}),
        ),
    )
    response.raise_for_status()
    assert record.last_call_kind == "await_assignment" and record.holds == {}

    body = (await client.post("/rpc", json=rpc("get_state", {}))).json()
    assert "result" in body
    assert orchestrator.last_call_kind == "get_state" and orchestrator.holds == {}
    with database(hub_store.path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM call_log").fetchone()[0] == 0


async def test_status_json_carries_the_assessment_additively(
    client: httpx.AsyncClient, hub_store: HubStore
) -> None:
    await check_in(client, "bob")
    hub_store.assign_task("bob", "implementer", "Fix", "Do it")
    status = hub_store.status_summary()
    (bob,) = status["agents"]
    assert set(bob) >= {"name", "harness", "model", "status", "alive", "current_task"}
    assert bob["stalled"] is False and bob["activity"]["reasons"] == []
    assert status["stall_after_min"] == 20
    assert status["orchestrator_activity"]["stalled"] is False
    assert status["workflow"]["id"] == hub_store.get_state()["workflow"]["id"]
    assert hub_store.get_task(bob["current_task"]).state is TaskState.SUBMITTED  # type: ignore[union-attr]


# -- review r1 -------------------------------------------------------------------------


async def test_a_replayed_check_in_of_a_superseded_instance_keeps_the_current_hold(
    tmp_path: Path, clock: FakeClock, settings: HubSettings
) -> None:
    """r1-1: the cached replay names the old instance; it must not touch the new one."""

    store = make_store(tmp_path / "hub.db", clock)
    protocol = A2AProtocol(store, settings)

    def ready(instance: str, operation: str) -> dict[str, Any]:
        metadata: dict[str, Any] = {
            MetaKeys.AGENT: "bob",
            MetaKeys.SCHEMA_VERSION: 1,
            MetaKeys.OPERATION_ID: operation,
            MetaKeys.WORKER_INSTANCE_ID: instance,
        }
        return rpc("message/send", message("READY", metadata=metadata))

    old = ready("old", "old-op")
    assert (await protocol.dispatch(old)).status_code == 200
    clock.advance(minutes=4)
    store.sweep(lost_after_s=180)
    assert (await protocol.dispatch(ready("new", "new-op"))).status_code == 200
    with store.worker_hold("bob", "new", "await_assignment", 100):
        clock.advance(seconds=30)
        assert (await protocol.dispatch(old)).status_code == 200  # the cached replay
        assert store.agent_by_name("bob").worker_instance_id == "new"  # type: ignore[union-attr]
        state = assessed(store, "bob")
        assert state["holding"] == "await_assignment" and state["last_call"] == "await_assignment"
        # A held stream from the old instance records nothing either.
        with store.worker_hold("bob", "old", "await_assignment", 100):
            assert assessed(store, "bob")["holding"] == "await_assignment"
        store.note_worker_call("bob", "old", "progress")
    record = store.activity.worker("bob", "new")
    assert record is not None and record.last_call_kind == "await_assignment"


def test_status_lists_workers_that_checked_in_before_the_workflow(
    tmp_path: Path, clock: FakeClock
) -> None:
    """r1-2: no workflow yet means no assessment, not a broken status read."""

    initialize_database(tmp_path / "hub.db")
    store = HubStore(tmp_path / "hub.db", clock=clock)
    store.check_in("bob")
    clock.advance(hours=1)
    status = store.status_summary()
    assert status["workflow"] is None and status["orchestrator_activity"] is None
    (bob,) = status["agents"]
    assert bob["name"] == "bob" and bob["stalled"] is False
    assert bob["activity"]["suppressed"] == "no_workflow"
    assert store.sweep_stalls() == [] and store.stall_episodes() == []
