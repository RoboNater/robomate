"""Optional stall actions (#272), with time controlled by the shared fake clock."""

from collections.abc import Mapping
from datetime import timedelta
from pathlib import Path
from sqlite3 import Connection
from typing import Any

import pytest
from agent_hub.database import SCHEMA_VERSION, database, initialize_database
from agent_hub.store import EventRecord, HubStore
from agent_hub_common import AgentStatus, EventKind, TaskState, WorkflowPolicy
from pydantic import ValidationError
from timing import FrozenClock


def setup(tmp_path: Path, clock: FrozenClock, action: float = 3) -> tuple[HubStore, str]:
    clock.start = clock.start.replace(microsecond=0)
    path = tmp_path / "stall.db"
    initialize_database(path)
    store = HubStore(path, detect_host_pauses=False)
    clock.install(store)
    store.initialize_workflow(policy={"stall_after_min": 1, "stall_action_after_min": action})
    agent = store.check_in("bob")
    instance = str(agent.worker_instance_id)
    store.note_worker_call("bob", instance, "check_in")
    return store, instance


def advance(
    store: HubStore, clock: FrozenClock, instance: str, seconds: float = 60
) -> list[EventRecord]:
    clock.advance(seconds)
    assert store.heartbeat("bob", instance, None)
    return store.sweep_stalls()


def test_thresholds_and_durable_once_only_action(tmp_path: Path, frozen_clock: FrozenClock) -> None:
    store, instance = setup(tmp_path, frozen_clock)
    task = store.assign_task("bob", "implementer", "Fix", "Work")
    assert advance(store, frozen_clock, instance, 59) == []
    assert [e.kind for e in advance(store, frozen_clock, instance, 1)] == [EventKind.AGENT_STALLED]
    assert not store.open_operator_questions()
    assert advance(store, frozen_clock, instance, 119) == []
    events = advance(store, frozen_clock, instance, 1)
    assert [e.kind for e in events] == [EventKind.STALL_ACTION]
    payload = events[0].payload
    assert payload["action"] == "operator_question" and payload["instance"] == instance
    assert payload["task_id"] == task.id and payload["heartbeat_age_s"] == 0
    question_id = payload["question_id"]
    assert (
        next(e for e in store.stall_episodes() if e["role"] == "worker")["escalation_question_id"]
        == question_id
    )
    question = store.open_operator_questions()[0]
    assert question["question_id"] == question_id and "latest PR comments" in question["question"]
    assert store.get_state()["workflow"]["status"] == "escalated"
    agent = store.agent_by_name("bob")
    current = store.get_task(task.id)
    assert agent is not None and agent.status == AgentStatus.BUSY
    assert current is not None and current.state == TaskState.SUBMITTED
    assert current.lease_expires == task.lease_expires
    assert advance(store, frozen_clock, instance) == []
    restarted = HubStore(store.path, detect_host_pauses=False)
    frozen_clock.install(restarted)
    assert restarted.sweep_stalls() == []
    assert len(restarted.open_operator_questions()) == 1


def test_disabled_action_never_asks(tmp_path: Path, frozen_clock: FrozenClock) -> None:
    store, instance = setup(tmp_path, frozen_clock, 0)
    advance(store, frozen_clock, instance, 3600)
    assert store.stall_episodes() and not store.open_operator_questions()
    assert store.get_state()["workflow"]["status"] == "active"


def test_recovery_before_action_resets_inactivity(
    tmp_path: Path, frozen_clock: FrozenClock
) -> None:
    store, instance = setup(tmp_path, frozen_clock)
    advance(store, frozen_clock, instance, 179)
    store.note_worker_call("bob", instance, "get_task")
    assert store.sweep_stalls() == []
    assert not [e for e in store.stall_episodes(open_only=True) if e["role"] == "worker"]
    assert advance(store, frozen_clock, instance, 2) == []
    assert not store.open_operator_questions()


def test_task_progress_before_action_prevents_escalation(
    tmp_path: Path, frozen_clock: FrozenClock
) -> None:
    store, instance = setup(tmp_path, frozen_clock)
    task = store.assign_task("bob", "implementer", "Fix", "Work")
    advance(store, frozen_clock, instance, 179)
    store.note_worker_call("bob", instance, "report_progress")
    store.record_progress(task.id, "bob", "Tests running")
    assert store.sweep_stalls() == []
    assert advance(store, frozen_clock, instance, 2) == []
    assert not store.open_operator_questions()


def test_lost_worker_keeps_operator_question_open(
    tmp_path: Path, frozen_clock: FrozenClock
) -> None:
    store, instance = setup(tmp_path, frozen_clock)
    store.assign_task("bob", "implementer", "Fix", "Work")
    advance(store, frozen_clock, instance, 180)
    question_id = store.open_operator_questions()[0]["question_id"]
    # The bridge now stops too: loss is not evidence that the worker recovered.
    frozen_clock.advance(180)
    events = store.sweep(180)
    assert EventKind.AGENT_LOST in [e.kind for e in events]
    assert store.sweep_stalls() == []
    episode = next(e for e in store.stall_episodes() if e["role"] == "worker")
    assert episode["cleared_reason"] == "lost"
    assert [q["question_id"] for q in store.open_operator_questions()] == [question_id]
    assert store.operator_answer(question_id)["status"] == "unanswered"
    assert store.get_state()["workflow"]["status"] == "escalated"
    assert store.sweep_stalls() == []


@pytest.mark.parametrize("answer", [False, True])
def test_recovery_withdraws_only_unanswered_hub_question(
    tmp_path: Path, frozen_clock: FrozenClock, answer: bool
) -> None:
    store, instance = setup(tmp_path, frozen_clock)
    advance(store, frozen_clock, instance, 180)
    question_id = store.open_operator_questions()[0]["question_id"]
    unrelated = store.ask_user("Separate question", None, actor="alice", session="s")
    if answer:
        store.answer_operator_question(question_id, "Inspect first")
    store.note_worker_call("bob", instance, "get_task")
    events = store.sweep_stalls()
    assert [e.kind for e in events] == ([] if answer else [EventKind.STALL_ACTION])
    result = store.operator_answer(question_id)
    assert result["status"] == ("answered" if answer else "withdrawn")
    assert result["answer"] == ("Inspect first" if answer else None)
    assert [q["question_id"] for q in store.open_operator_questions()] == [unrelated]
    assert store.get_state()["workflow"]["status"] == "escalated"
    assert store.sweep_stalls() == []


def test_live_hold_exempts_silence_but_not_task_progress(
    tmp_path: Path, frozen_clock: FrozenClock
) -> None:
    store, instance = setup(tmp_path, frozen_clock)
    with store.worker_hold("bob", instance, "await_assignment", 600):
        assert advance(store, frozen_clock, instance, 180) == []
    assert not store.open_operator_questions()
    task = store.assign_task("bob", "implementer", "Fix", "Work")
    for _ in range(3):
        store.note_worker_call("bob", instance, "get_task")
        events = advance(store, frozen_clock, instance)
    assert store.open_operator_questions()
    event = [e for e in events if e.kind == EventKind.STALL_ACTION][0]
    assert event.payload["reasons"] == ["no_task_progress"]
    current = store.get_task(task.id)
    assert current is not None and current.state == TaskState.SUBMITTED


def test_worker_waiting_for_answer_is_exempt(tmp_path: Path, frozen_clock: FrozenClock) -> None:
    store, instance = setup(tmp_path, frozen_clock)
    task = store.assign_task("bob", "implementer", "Fix", "Work")
    store.open_question(task.id, "bob", "Need decision", "m1")
    with store.worker_hold("bob", instance, "ask_alice", 600):
        assert advance(store, frozen_clock, instance, 180) == []
    assert not store.open_operator_questions()


def test_action_uses_current_reason_age_not_episode_age(
    tmp_path: Path, frozen_clock: FrozenClock
) -> None:
    store, instance = setup(tmp_path, frozen_clock)
    advance(store, frozen_clock, instance, 179)
    store.note_worker_call("bob", instance, "get_task")
    store.assign_task("bob", "implementer", "Fix", "Work")
    # No sweep between old silence and new task: the new reason has not aged.
    assert advance(store, frozen_clock, instance, 2) == []
    assert not store.open_operator_questions()
    assert [e.kind for e in advance(store, frozen_clock, instance, 178)] == [
        EventKind.AGENT_STALLED,
        EventKind.STALL_ACTION,
    ]


@pytest.mark.parametrize("already_asked", [False, True])
def test_host_pause_freezes_action_until_grace_and_allows_recovery(
    tmp_path: Path, frozen_clock: FrozenClock, already_asked: bool
) -> None:
    store, instance = setup(tmp_path, frozen_clock)
    if already_asked:
        advance(store, frozen_clock, instance, 180)
    store.detect_host_pauses = True
    store.sweep(180)
    before = store.stall_episodes()
    # Deliberately simulate a host pause: wall time jumps ahead of monotonic time.
    frozen_clock.start += timedelta(hours=4)
    frozen_clock.advance(10)
    assert [e.kind for e in store.sweep(180)] == [EventKind.HOST_PAUSED]
    assert store.sweep_stalls() == [] and store.stall_episodes() == before
    frozen_clock.advance(179)
    store.note_worker_call("bob", instance, "get_task")
    assert store.heartbeat("bob", instance, None)
    assert store.sweep_stalls() == []
    frozen_clock.advance(1)
    events = store.sweep_stalls()
    assert [e.kind for e in events] == ([EventKind.STALL_ACTION] if already_asked else [])
    assert not store.open_operator_questions()


def test_action_transaction_rolls_back_without_partial_question(
    tmp_path: Path, frozen_clock: FrozenClock, monkeypatch: pytest.MonkeyPatch
) -> None:
    store, instance = setup(tmp_path, frozen_clock)
    advance(store, frozen_clock, instance, 60)
    original = HubStore._add_event

    def fail_action(
        self: HubStore, connection: Connection, kind: EventKind, payload: Mapping[str, Any]
    ) -> int:
        if kind == EventKind.STALL_ACTION:
            raise RuntimeError("event write failed")
        return original(self, connection, kind, payload)

    with monkeypatch.context() as patch:
        patch.setattr(HubStore, "_add_event", fail_action)
        with pytest.raises(RuntimeError, match="event write failed"):
            advance(store, frozen_clock, instance, 120)
    assert not store.open_operator_questions()
    assert store.get_state()["workflow"]["status"] == "active"
    assert [e.kind for e in store.sweep_stalls()] == [EventKind.STALL_ACTION]


@pytest.mark.parametrize("bad", [-1, 1, 0.5, True, float("inf"), float("nan")])
def test_invalid_second_threshold_rejected(bad: float) -> None:
    with pytest.raises(ValidationError):
        WorkflowPolicy(stall_after_min=1, stall_action_after_min=bad)


def test_schema_migrates_stall_action_link(tmp_path: Path) -> None:
    import sqlite3

    path = tmp_path / "v27.db"
    with sqlite3.connect(path) as connection:
        connection.executescript((Path(__file__).parent / "fixtures/schema_v27.sql").read_text())
        connection.execute(
            "INSERT INTO event (kind, payload_json, ts) VALUES ('agent_stalled', '{}', 't')"
        )
        connection.execute(
            "INSERT INTO stall_episode (role, actor, started, assessed, reasons_json,"
            " evidence_json, event_id) VALUES ('worker', 'bob', 't', 't', '[]', '{}', 1)"
        )
    initialize_database(path)
    initialize_database(path)
    store = HubStore(path)
    assert store.stall_episodes()[0]["escalation_question_id"] is None
    assert store.stall_episodes()[0]["event_id"] == 1
    store.append_event(EventKind.STALL_ACTION, {"action": "operator_question"})
    with database(path) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
        assert "escalation_question_id" in {
            r["name"] for r in connection.execute("PRAGMA table_info(stall_episode)")
        }
        assert connection.execute("SELECT COUNT(*) FROM event").fetchone()[0] == 2


async def test_recovery_diagnostics_in_stall_event_action_and_question(
    tmp_path: Path, frozen_clock: FrozenClock
) -> None:
    store, instance = setup(tmp_path, frozen_clock)
    agent = store.agent_by_name("bob")
    assert agent is not None
    task = store.assign_task("bob", "implementer", "Fix", "Work")
    await store.await_assignment(agent.context_id, 0)
    await store.await_assignment(agent.context_id, 0)
    await store.await_assignment(agent.context_id, 0)
    store.record_failed_task_call(agent)
    store.record_failed_task_call(agent)
    events = advance(store, frozen_clock, instance, 60)
    stalled = next(e for e in events if e.kind == EventKind.AGENT_STALLED)
    assert stalled.payload["redelivery_count"] == 2
    assert stalled.payload["failed_task_call_count"] == 2
    assert stalled.payload["last_redelivery_age_s"] == 60
    assert stalled.payload["last_failed_task_call_age_s"] == 60
    events = advance(store, frozen_clock, instance, 120)
    action = next(e for e in events if e.kind == EventKind.STALL_ACTION)
    assert action.payload["redelivery_count"] == 2
    assert action.payload["failed_task_call_count"] == 2
    question = store.open_operator_questions()[0]["question"]
    assert '"redelivery_count": 2' in question
    assert '"failed_task_call_count": 2' in question
    assert task.id in question
    held = store.get_task(task.id)
    assert held is not None and held.lease_expires == task.lease_expires
    frozen_clock.advance(1201)
    assert "failed_task_call_count" not in store.assess_activity()["agents"]["bob"]
    store.cancel_task(task.id)
    assert "redelivery_count" not in store.assess_activity()["agents"]["bob"]
