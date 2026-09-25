import asyncio
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from sqlite3 import Connection

import pytest
from agent_hub import store as store_module
from agent_hub.database import database, initialize_database
from agent_hub.store import (
    ConflictError,
    DuplicateAgentError,
    HubStore,
    InvalidPolicyError,
    NotFoundError,
    Released,
)
from agent_hub_common import (
    AgentProfile,
    AgentStatus,
    EventKind,
    ImplementerOutcome,
    MetaKeys,
    ModelSource,
    RebaseResult,
    TaskState,
    WorkflowStatus,
    iso_after,
    to_iso,
    utcnow,
)

CLAUDE = AgentProfile(
    harness="claude-code",
    harness_version="2.1.268",
    provider="anthropic",
    model="claude-opus-5",
    model_source=ModelSource.ENV,
    capabilities=("python",),
)


class FakeClock:
    def __init__(self) -> None:
        self.now = datetime(2026, 9, 11, 12, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.now

    def advance(self, **kwargs: float) -> None:
        self.now += timedelta(**kwargs)


def assign(store: HubStore, agent: str = "bob", role: str = "implementer") -> str:
    return store.assign_task(agent, role, "Fix #1", "Open a PR", lease_min=30).id


def test_state_uses_one_read_snapshot(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "hub.db"
    initialize_database(path)
    store = HubStore(path)
    store.initialize_workflow()
    store.check_in("bob")
    task = store.assign_task("bob", "implementer", "Fix", "Instructions")
    changed = False

    def between_queries(sql: str) -> None:
        nonlocal changed
        if "SELECT * FROM task" in sql and not changed:
            changed = True
            with database(path) as writer:
                writer.execute("UPDATE task SET state = 'completed' WHERE id = ?", (task.id,))
                writer.execute("UPDATE agent SET status = 'idle', current_task_id = NULL")

    @contextmanager
    def traced_database(path: Path) -> Iterator[Connection]:
        with database(path) as connection:
            connection.set_trace_callback(between_queries)
            yield connection

    monkeypatch.setattr(store_module, "database", traced_database)
    state = store.get_state()
    assert changed
    assert state["tasks"][0]["state"] == "submitted"
    assert state["agents"][0]["status"] == "busy"
    assert state["agents"][0]["current_task_id"] == task.id


def test_check_in_registers_an_agent_and_queues_the_event(store: HubStore) -> None:
    agent = store.check_in("bob", CLAUDE, worker_instance_id="bob-1")

    event = store.next_event()

    assert agent.status is AgentStatus.IDLE
    assert agent.context_id
    assert agent.capabilities == ["python"]
    assert event is not None
    assert event.kind is EventKind.AGENT_CHECKED_IN
    assert event.payload == {
        "agent": "bob",
        "harness": "claude-code",
        "harness_version": "2.1.268",
        "provider": "anthropic",
        "model": "claude-opus-5",
        "model_source": "env",
        "declared_model": "unknown",
        "model_mismatch": False,
        "capabilities": ["python"],
        "workspace_id": None,
        "context_id": agent.context_id,
        "worker_instance_id": "bob-1",
    }


def test_check_in_records_a_declared_model_that_disagrees_with_the_configured_one(
    store: HubStore,
) -> None:
    profile = replace(CLAUDE, declared_model="claude-sonnet-5")

    agent = store.check_in("bob", profile)
    event = store.next_event()

    # Resolution is untouched: the configured value stays authoritative (§3).
    assert (agent.model, agent.model_source) == ("claude-opus-5", ModelSource.ENV)
    assert agent.declared_model == "claude-sonnet-5"
    assert event is not None
    assert event.payload["model"] == "claude-opus-5"
    assert event.payload["declared_model"] == "claude-sonnet-5"
    assert event.payload["model_mismatch"] is True
    [state] = store.get_state()["agents"]
    assert (state["model"], state["declared_model"]) == ("claude-opus-5", "claude-sonnet-5")
    assert state["model_mismatch"] is True


@pytest.mark.parametrize(
    ("profile", "declared_model"),
    [
        # The runtime agrees with the operator.
        (replace(CLAUDE, declared_model="claude-opus-5"), "claude-opus-5"),
        # The runtime reported nothing, which contradicts no one.
        (CLAUDE, "unknown"),
        # No configured model: the declared one is `model`, so it cannot differ.
        (
            replace(
                CLAUDE,
                model_source=ModelSource.DECLARED,
                declared_model="claude-opus-5",
            ),
            "claude-opus-5",
        ),
    ],
)
def test_only_a_configured_model_the_runtime_contradicts_is_a_mismatch(
    store: HubStore, profile: AgentProfile, declared_model: str
) -> None:
    store.check_in("bob", profile)

    event = store.next_event()
    [state] = store.get_state()["agents"]

    assert event is not None
    assert event.payload["declared_model"] == state["declared_model"] == declared_model
    assert event.payload["model_mismatch"] is state["model_mismatch"] is False


def test_a_returning_worker_replaces_the_declared_model_with_its_new_one(
    store: HubStore,
) -> None:
    store.check_in("bob", replace(CLAUDE, declared_model="claude-sonnet-5"))
    store.release_agent("bob")

    store.check_in("bob", CLAUDE)

    agent = store.agent_by_name("bob")
    assert agent is not None and agent.declared_model == "unknown"


def test_get_state_shows_each_workers_profile(store: HubStore) -> None:
    store.check_in("bob", CLAUDE)
    store.check_in("charlie", AgentProfile(harness="codex"))

    agents = {agent["name"]: agent for agent in store.get_state()["agents"]}
    assert agents["bob"].pop("heartbeat_age_s") >= 0
    assert agents["bob"].pop("progress_age_s") is None

    assert agents["bob"] | {
        "context_id": "",
        "last_seen": "",
        "last_heartbeat": "",
        "worker_instance_id": "",
    } == {
        "name": "bob",
        "status": "idle",
        "context_id": "",
        "last_seen": "",
        "worker_instance_id": "",
        "last_heartbeat": "",
        "last_progress_at": None,
        "current_task_id": None,
        "harness": "claude-code",
        "harness_version": "2.1.268",
        "provider": "anthropic",
        "model": "claude-opus-5",
        "model_source": "env",
        "capabilities": ["python"],
        "workspace_id": None,
        "declared_model": "unknown",
        # A direct store call has no connection, so nothing was observed.
        "checkin_remote_addr": None,
        "last_remote_addr": None,
        "model_mismatch": False,
    }
    # Whatever charlie's launcher left unset reads `unknown`, not a guess.
    charlie = agents["charlie"]
    assert charlie["harness"] == "codex"
    assert charlie["harness_version"] == charlie["provider"] == charlie["model"] == "unknown"
    assert charlie["model_source"] == charlie["declared_model"] == "unknown"
    assert charlie["capabilities"] == []


def test_a_second_live_instance_cannot_claim_an_idle_agent_name(store: HubStore) -> None:
    first = store.check_in("bob", CLAUDE, worker_instance_id="bob-1")
    task_id = assign(store)
    store.submit_result(task_id, "bob", TaskState.COMPLETED, "done")

    with pytest.raises(DuplicateAgentError, match="live worker instance"):
        store.check_in("bob", worker_instance_id="bob-2")

    assert store.agent_by_name("bob") == first


def test_a_second_live_instance_cannot_take_over_an_open_task(store: HubStore) -> None:
    store.check_in("bob", worker_instance_id="bob-1")
    task_id = assign(store)

    with pytest.raises(DuplicateAgentError, match="live worker instance"):
        store.check_in("bob", worker_instance_id="bob-2")

    current = store.agent_by_name("bob")
    assert current is not None and current.current_task_id == task_id


def test_assignment_marks_the_agent_busy_and_records_the_instructions(store: HubStore) -> None:
    store.check_in("bob")

    task = store.assign_task("bob", "implementer", "Fix #1", "Open a PR")

    agent = store.agent_by_name("bob")
    assert task.state is TaskState.SUBMITTED
    assert task.lease_expires is not None
    assert agent is not None
    assert agent.status is AgentStatus.BUSY
    assert agent.current_task_id == task.id
    assert [part["text"] for part in store.task_history(task.id)[0].parts] == ["Open a PR"]


HEAD = "0123456789abcdef0123456789abcdef01234567"


def test_a_review_assignment_is_bound_to_the_head_it_was_given(store: HubStore) -> None:
    store.check_in("bob")

    task = store.assign_task("bob", "reviewer", "Review #7", "Review it", pr_head_sha=HEAD.upper())

    assert task.pr_head_sha == HEAD
    assert store.get_state()["tasks"][0]["pr_head_sha"] == HEAD
    metadata = store.task_history(task.id)[0].parts[0]["metadata"]
    assert metadata[MetaKeys.PR_HEAD_SHA] == HEAD


def test_an_unbound_assignment_carries_no_head(store: HubStore) -> None:
    store.check_in("bob")

    task = store.assign_task("bob", "implementer", "Fix #1", "Open a PR")

    assert task.pr_head_sha is None
    assert MetaKeys.PR_HEAD_SHA not in store.task_history(task.id)[0].parts[0]["metadata"]


@pytest.mark.parametrize("sha", ["abc123", HEAD + "\n", "g" * 40, ""])
def test_a_malformed_head_sha_is_refused_before_anything_is_assigned(
    store: HubStore, sha: str
) -> None:
    store.check_in("bob")

    with pytest.raises(ValueError, match="pr_head_sha"):
        store.assign_task("bob", "reviewer", "Review", "Review it", pr_head_sha=sha)

    assert store.tasks() == []
    agent = store.agent_by_name("bob")
    assert agent is not None and agent.status is AgentStatus.IDLE


@pytest.mark.parametrize(
    ("outcome", "state", "kind"),
    [
        (ImplementerOutcome.COMPLETED, TaskState.COMPLETED, EventKind.TASK_COMPLETED),
        (ImplementerOutcome.BLOCKED, TaskState.FAILED, EventKind.TASK_FAILED),
    ],
)
def test_a_rebase_result_is_stored_whole_and_reported_to_alice(
    store: HubStore, outcome: ImplementerOutcome, state: TaskState, kind: EventKind
) -> None:
    store.check_in("bob")
    task_id = store.assign_task("bob", "rebase", "Rebase", "Rebase it", pr_head_sha=HEAD).id
    while store.next_event():
        pass
    result = RebaseResult(
        outcome=outcome,
        head_sha=HEAD if outcome is ImplementerOutcome.COMPLETED else None,
        conflict_files=["README.md"],
        resolution_summary="Took main's wording, kept the new section.",
        blocker=None if outcome is ImplementerOutcome.COMPLETED else "needs a decision",
        summary="Rebased onto main",
    )

    finished = store.submit_result(task_id, "bob", result)

    event = store.next_event()
    assert finished.state is state
    assert finished.result is not None
    assert finished.result["conflict_files"] == ["README.md"]
    assert event is not None and event.kind is kind
    assert event.payload["result"]["resolution_summary"].startswith("Took main's")


def test_a_second_assignment_to_a_busy_agent_is_refused(store: HubStore) -> None:
    store.check_in("bob")
    assign(store)

    with pytest.raises(ConflictError, match="already holds"):
        assign(store)


def test_a_released_agent_takes_no_further_assignment(store: HubStore) -> None:
    store.check_in("bob")
    store.release_agent("bob")

    with pytest.raises(ConflictError, match="released"):
        assign(store)


def test_assigning_to_an_unknown_agent_is_refused(store: HubStore) -> None:
    with pytest.raises(NotFoundError):
        assign(store)


async def test_waiting_worker_gets_the_task_and_claims_it(store: HubStore) -> None:
    agent = store.check_in("bob")

    async def alice() -> None:
        await asyncio.sleep(0.01)
        assign(store)

    waited, _ = await asyncio.gather(store.await_assignment(agent.context_id, 2.0), alice())

    assert not isinstance(waited, Released)
    assert waited is not None
    # The task is claimed as it is handed over, so a second waiter cannot take it.
    assert waited.state is TaskState.WORKING
    assert await store.await_assignment(agent.context_id, 0.05) is None


async def test_waiting_worker_is_released_while_it_waits(store: HubStore) -> None:
    agent = store.check_in("bob")

    async def alice() -> None:
        await asyncio.sleep(0.01)
        store.release_agent("bob")

    waited, _ = await asyncio.gather(store.await_assignment(agent.context_id, 2.0), alice())

    assert waited == Released(agent="bob")


async def test_assignment_wait_times_out_without_work(store: HubStore) -> None:
    agent = store.check_in("bob")

    assert await store.await_assignment(agent.context_id, 0.05) is None


async def test_assignment_wait_rejects_an_unknown_context(store: HubStore) -> None:
    with pytest.raises(NotFoundError):
        await store.await_assignment("nope", 0.05)


def test_progress_is_recorded_as_an_event(store: HubStore) -> None:
    store.check_in("bob")
    task_id = assign(store)
    store.next_event()

    store.record_progress(task_id, "bob", "branch pushed")

    event = store.next_event()
    assert event is not None
    assert event.kind is EventKind.TASK_PROGRESS
    assert event.payload == {"task_id": task_id, "agent": "bob", "note": "branch pushed"}


async def test_a_question_parks_the_task_until_alice_replies(store: HubStore) -> None:
    store.check_in("bob")
    task_id = assign(store)

    question_id = store.open_question(task_id, "bob", "Which base branch?", "q-1")
    parked = store.get_task(task_id)

    async def alice() -> None:
        await asyncio.sleep(0.01)
        store.reply(task_id, "main", message_id=question_id)

    reply, _ = await asyncio.gather(store.await_reply(task_id, question_id, 2.0), alice())

    resumed = store.get_task(task_id)
    assert parked is not None and parked.state is TaskState.INPUT_REQUIRED
    assert reply is not None
    assert [part["text"] for part in reply.parts] == ["main"]
    assert resumed is not None and resumed.state is TaskState.WORKING


async def test_an_unanswered_question_times_out(store: HubStore) -> None:
    store.check_in("bob")
    task_id = assign(store)
    question_id = store.open_question(task_id, "bob", "Which base branch?", "q-1")

    assert await store.await_reply(task_id, question_id, 0.05) is None
    # The question stays parked, so calling again resumes the same wait.
    parked = store.get_task(task_id)
    assert parked is not None and parked.state is TaskState.INPUT_REQUIRED


async def test_a_reply_that_lands_between_attempts_reaches_the_retry(
    store: HubStore,
) -> None:
    """ "Call again" is only safe if a retry can still see the answer it missed."""

    store.check_in("bob")
    task_id = assign(store)
    first = store.open_question(task_id, "bob", "Which base branch?", "q-1")
    assert await store.await_reply(task_id, first, 0.05) is None

    # Alice answers in the gap between the timeout and the worker calling again.
    store.reply(task_id, "main", message_id=first)
    retry = store.open_question(task_id, "bob", "Which base branch?", "q-1")
    reply = await store.await_reply(task_id, retry, 0.05)

    assert retry == first
    assert reply is not None
    assert [part["text"] for part in reply.parts] == ["main"]


def test_a_retried_question_does_not_ask_alice_twice(store: HubStore) -> None:
    store.check_in("bob")
    task_id = assign(store)
    while store.next_event():
        pass

    store.open_question(task_id, "bob", "Which base branch?", "q-1")
    store.open_question(task_id, "bob", "Which base branch?", "q-1")

    kinds = [event.kind for event in iter(store.next_event, None)]
    assert kinds == [EventKind.WORKER_QUESTION]
    assert len(store.task_history(task_id)) == 2


def test_a_different_question_is_a_new_question(store: HubStore) -> None:
    store.check_in("bob")
    task_id = assign(store)

    first = store.open_question(task_id, "bob", "Which base branch?", "q-1")
    second = store.open_question(task_id, "bob", "Squash or merge?", "q-2")

    assert second != first


@pytest.mark.parametrize(
    ("status", "kind"),
    [
        (TaskState.COMPLETED, EventKind.TASK_COMPLETED),
        (TaskState.FAILED, EventKind.TASK_FAILED),
    ],
)
def test_a_result_ends_the_task_and_frees_the_worker(
    store: HubStore, status: TaskState, kind: EventKind
) -> None:
    store.check_in("bob")
    task_id = assign(store)
    while store.next_event():
        pass

    finished = store.submit_result(
        task_id, "bob", status, "PR open", [{"name": "pr", "url": "http://pr/1"}]
    )

    agent = store.agent_by_name("bob")
    event = store.next_event()
    assert finished.state is status
    assert finished.lease_expires is None
    assert finished.result is not None
    assert finished.result["artifacts"] == [{"name": "pr", "url": "http://pr/1"}]
    assert agent is not None
    assert agent.status is AgentStatus.IDLE
    assert agent.current_task_id is None
    assert event is not None and event.kind is kind


def test_a_result_must_be_terminal_and_can_only_be_reported_once(store: HubStore) -> None:
    store.check_in("bob")
    task_id = assign(store)

    with pytest.raises(ConflictError, match="completed or failed"):
        store.submit_result(task_id, "bob", TaskState.WORKING, "still going")

    store.submit_result(task_id, "bob", TaskState.COMPLETED, "done")
    with pytest.raises(ConflictError, match="already completed"):
        store.submit_result(task_id, "bob", TaskState.COMPLETED, "done twice")


def test_cancelling_frees_the_worker_and_only_works_once(store: HubStore) -> None:
    store.check_in("bob")
    task_id = assign(store)

    canceled = store.cancel_task(task_id)

    agent = store.agent_by_name("bob")
    assert canceled.state is TaskState.CANCELED
    assert agent is not None and agent.status is AgentStatus.IDLE
    with pytest.raises(ConflictError):
        store.cancel_task(task_id)


def test_events_are_consumed_once_and_in_order(store: HubStore) -> None:
    store.check_in("bob")
    task_id = assign(store)
    store.record_progress(task_id, "bob", "one")

    kinds = [event.kind for event in iter(store.next_event, None)]

    assert kinds == [EventKind.AGENT_CHECKED_IN, EventKind.TASK_PROGRESS]
    assert store.pending_events() == 0
    assert store.next_event() is None


async def test_waiting_for_an_event_returns_as_soon_as_one_is_queued(store: HubStore) -> None:
    async def worker() -> None:
        await asyncio.sleep(0.01)
        store.check_in("bob")

    event, _ = await asyncio.gather(store.wait_for_event(2.0), worker())

    assert event is not None and event.kind is EventKind.AGENT_CHECKED_IN


async def test_waiting_for_an_event_times_out_on_an_empty_inbox(store: HubStore) -> None:
    assert await store.wait_for_event(0.05) is None


def test_history_can_be_trimmed_to_the_most_recent_messages(store: HubStore) -> None:
    store.check_in("bob")
    task_id = assign(store)
    store.record_progress(task_id, "bob", "one")
    store.record_progress(task_id, "bob", "two")

    assert len(store.task_history(task_id)) == 3
    assert [part["text"] for part in store.task_history(task_id, 1)[0].parts] == ["two"]
    assert store.task_history(task_id, 0) == []


def test_an_overdue_lease_is_reported_exactly_once(store: HubStore) -> None:
    store.check_in("bob")
    task_id = store.assign_task("bob", "implementer", "Fix #1", "Open a PR", lease_min=-1).id

    first = store.sweep(lost_after_s=3600)
    second = store.sweep(lost_after_s=3600)

    assert [event.kind for event in first] == [EventKind.LEASE_EXPIRED]
    assert first[0].payload["task_id"] == task_id
    assert second == []
    # The task itself is untouched: whether to extend or reassign is Alice's call.
    task = store.get_task(task_id)
    assert task is not None and task.state is TaskState.SUBMITTED


def test_a_silent_worker_is_lost_and_its_task_fails(store: HubStore) -> None:
    store.check_in("bob")
    task_id = assign(store)
    store.sweep(lost_after_s=-1)

    agent = store.agent_by_name("bob")
    task = store.get_task(task_id)
    assert agent is not None and agent.status is AgentStatus.LOST
    assert task is not None and task.state is TaskState.FAILED
    assert task.result is not None and task.result["reason"] == "worker_lost"
    assert store.sweep(lost_after_s=-1) == []


def test_a_release_survives_the_worker_restarting(store: HubStore) -> None:
    # Alice releases at wrap-up; a worker that comes back afterwards has to be
    # told to stop rather than left polling for work nobody will assign.
    store.check_in("bob")
    store.release_agent("bob")

    returned = store.check_in("bob", AgentProfile(capabilities=("python",)))

    assert returned.status is AgentStatus.RELEASED
    assert returned.capabilities == ["python"]


def test_checking_in_readmits_a_lost_worker(store: HubStore) -> None:
    store.check_in("bob")
    store.sweep(lost_after_s=-1)

    returned = store.check_in("bob")

    assert returned.status is AgentStatus.IDLE


def test_a_lost_worker_is_given_no_work_until_it_checks_in_again(store: HubStore) -> None:
    store.check_in("bob")
    store.sweep(lost_after_s=-1)

    with pytest.raises(ConflictError, match="not idle"):
        assign(store)

    store.check_in("bob")
    assert store.get_task(assign(store)) is not None


def test_a_released_worker_is_never_declared_lost(store: HubStore) -> None:
    store.check_in("bob")
    store.release_agent("bob")

    assert store.sweep(lost_after_s=-1) == []


def test_a_worker_that_keeps_heartbeating_stays_live(store: HubStore) -> None:
    worker = store.check_in("bob")
    assert store.heartbeat("bob", worker.worker_instance_id, None, 120)

    assert store.sweep(lost_after_s=60) == []


def test_heartbeats_keep_a_worker_alive_during_twenty_minutes_without_llm_calls(
    store: HubStore,
) -> None:
    clock = FakeClock()
    store.clock = clock
    worker = store.check_in("bob", worker_instance_id="bob-1")
    task = store.assign_task("bob", "implementer", "Long tests", "Run them", lease_min=5)

    for _ in range(20):
        clock.advance(minutes=1)
        assert store.heartbeat("bob", "bob-1", task.id, max_task_lease_min=30)
        assert store.sweep(lost_after_s=180) == []

    current = store.agent_by_name("bob")
    active_task = store.get_task(task.id)
    assert current is not None and current.status is AgentStatus.BUSY
    assert active_task is not None and active_task.state is TaskState.SUBMITTED
    assert current.worker_instance_id == worker.worker_instance_id


def test_stopped_worker_is_lost_once_and_restart_supersedes_the_instance(
    store: HubStore,
) -> None:
    clock = FakeClock()
    store.clock = clock
    first = store.check_in("bob", worker_instance_id="bob-1")
    task = store.assign_task("bob", "implementer", "Task", "Work")
    while store.next_event():
        pass

    clock.advance(seconds=181)
    first_sweep = store.sweep(lost_after_s=180)
    assert [event.kind for event in first_sweep] == [EventKind.AGENT_LOST]
    assert store.sweep(lost_after_s=180) == []
    failed = store.get_task(task.id)
    assert failed is not None and failed.result is not None
    assert failed.result["reason"] == "worker_lost"

    restarted = store.check_in("bob", worker_instance_id="bob-2")
    assert restarted.context_id == first.context_id
    assert restarted.worker_instance_id == "bob-2"
    assert restarted.status is AgentStatus.IDLE
    assert not store.heartbeat("bob", "bob-1", None, max_task_lease_min=120)
    assert [event.kind for event in iter(store.next_event, None)] == [
        EventKind.AGENT_LOST,
        EventKind.AGENT_CHECKED_IN,
    ]


def test_superseded_heartbeat_does_not_renew_or_revive(store: HubStore) -> None:
    clock = FakeClock()
    store.clock = clock
    store.check_in("bob", worker_instance_id="bob-1")
    clock.advance(seconds=181)
    store.sweep(lost_after_s=180)
    store.check_in("bob", worker_instance_id="bob-2")
    task = store.assign_task("bob", "implementer", "Task", "Work", lease_min=5)
    before = store.agent_by_name("bob")
    original_expiry = task.lease_expires

    clock.advance(minutes=1)
    assert not store.heartbeat("bob", "bob-1", task.id, max_task_lease_min=30)

    after = store.agent_by_name("bob")
    unchanged_task = store.get_task(task.id)
    assert before is not None and after is not None
    assert after.status is AgentStatus.BUSY
    assert after.last_heartbeat == before.last_heartbeat
    assert unchanged_task is not None and unchanged_task.lease_expires == original_expiry


def test_a_wall_clock_stepped_back_never_moves_liveness_or_the_lease_back(
    store: HubStore,
) -> None:
    # WSL2 steps its wall clock back by seconds when it resyncs (#120).
    clock = FakeClock()
    store.clock = clock
    store.check_in("bob", worker_instance_id="bob-1")
    task = store.assign_task("bob", "implementer", "Task", "Work", lease_min=5)
    clock.advance(minutes=1)
    assert store.heartbeat("bob", "bob-1", task.id, max_task_lease_min=30)
    before = store.agent_by_name("bob")
    renewed = store.get_task(task.id)

    clock.advance(seconds=-2)
    assert store.heartbeat("bob", "bob-1", task.id, max_task_lease_min=30)

    after = store.agent_by_name("bob")
    unchanged = store.get_task(task.id)
    assert before is not None and after is not None
    assert after.last_heartbeat == before.last_heartbeat
    assert after.last_seen == before.last_seen
    assert renewed is not None and unchanged is not None
    assert unchanged.lease_expires == renewed.lease_expires


def test_heartbeat_lease_renewal_stops_at_cap_and_expires_once(tmp_path: Path) -> None:
    clock = FakeClock()
    path = tmp_path / "lease-cap.db"
    initialize_database(path)
    store = HubStore(path, clock=clock)
    store.initialize_workflow(policy={"max_task_lease_min": 10})
    store.check_in("bob", worker_instance_id="bob-1")
    task = store.assign_task("bob", "implementer", "Task", "Work", lease_min=3)
    while store.next_event():
        pass

    for _ in range(4):
        clock.advance(minutes=2)
        assert store.heartbeat("bob", "bob-1", task.id)

    renewed = store.get_task(task.id)
    assert renewed is not None
    assert renewed.lease_expires == to_iso(datetime(2026, 9, 11, 12, 10, tzinfo=UTC))

    clock.advance(minutes=2)
    assert store.heartbeat("bob", "bob-1", task.id)
    first = store.sweep(lost_after_s=180)
    second = store.sweep(lost_after_s=180)
    assert [event.kind for event in first] == [EventKind.LEASE_EXPIRED]
    assert second == []


def test_initial_lease_is_also_bounded_by_the_workflow_cap(tmp_path: Path) -> None:
    clock = FakeClock()
    path = tmp_path / "initial-lease-cap.db"
    initialize_database(path)
    store = HubStore(path, clock=clock)
    store.initialize_workflow(policy={"max_task_lease_min": 10})
    store.check_in("bob", worker_instance_id="bob-1")

    task = store.assign_task("bob", "implementer", "Task", "Work", lease_min=30)

    assert task.lease_expires == to_iso(clock.now + timedelta(minutes=10))
    assert task.lease_duration_s == 10 * 60


def test_state_reports_heartbeat_and_progress_ages_separately(store: HubStore) -> None:
    clock = FakeClock()
    store.clock = clock
    worker = store.check_in("bob", worker_instance_id="bob-1")
    task_id = assign(store)
    initial_heartbeat = worker.last_heartbeat

    clock.advance(seconds=30)
    store.record_progress(task_id, "bob", "tests started")
    progressed = store.agent_by_name("bob")
    assert progressed is not None and progressed.last_heartbeat == initial_heartbeat
    clock.advance(seconds=20)
    store.heartbeat("bob", "bob-1", task_id, max_task_lease_min=120)
    clock.advance(seconds=10)

    [agent] = store.get_state()["agents"]
    assert agent["heartbeat_age_s"] == 10
    assert agent["progress_age_s"] == 30


def test_the_single_workflow_is_initialized_once(store: HubStore) -> None:
    first = store.initialize_workflow()
    store.check_in("bob")
    task_id = assign(store)

    task = store.get_task(task_id)
    assert store.initialize_workflow() == first
    assert task is not None and task.workflow_id == first


def test_initial_workflow_policy_is_immutable_durable_and_used_by_rails(
    tmp_path: Path,
) -> None:
    path = tmp_path / "policy.db"
    initialize_database(path)
    clock = FakeClock()
    first_store = HubStore(path, clock=clock)
    policy = {"max_task_lease_min": 10, "merge_method": "merge"}

    workflow_id = first_store.initialize_workflow("Address issue #5", policy)
    restarted = HubStore(path, clock=clock)

    assert restarted.initialize_workflow("Address issue #5", policy) == workflow_id
    assert restarted.get_state()["workflow"]["policy"] == policy
    restarted.check_in("bob", worker_instance_id="bob-1")
    task = restarted.assign_task("bob", "implementer", "Task", "Work", lease_min=30)
    assert task.lease_expires == to_iso(clock.now + timedelta(minutes=10))

    with pytest.raises(ConflictError, match="fresh HUB_STATE_DIR"):
        restarted.initialize_workflow(
            "Address issue #5", {"max_task_lease_min": 20, "merge_method": "merge"}
        )


@pytest.mark.parametrize(
    ("policy", "field"),
    [
        ({"max_task_lease_min": "10"}, "max_task_lease_min"),
        ({"max_review_round": 5}, "max_review_round"),
        ({"merge_method": "octopus"}, "merge_method"),
        ({"role_policy": {"reviewer_harnes_differs": True}}, "reviewer_harnes_differs"),
    ],
)
def test_invalid_initial_policy_is_rejected_before_insert(
    tmp_path: Path, policy: dict[str, object], field: str
) -> None:
    path = tmp_path / "invalid-policy.db"
    initialize_database(path)
    store = HubStore(path)

    with pytest.raises(InvalidPolicyError, match=field):
        store.initialize_workflow("Address issue #5", policy)

    assert store.get_state()["workflow"] is None


def test_assignment_and_status_require_explicit_workflow_initialization(
    tmp_path: Path,
) -> None:
    path = tmp_path / "uninitialized.db"
    initialize_database(path)
    store = HubStore(path)
    store.check_in("bob")

    with pytest.raises(ConflictError, match="initialize_workflow.*assign_task"):
        store.assign_task("bob", "implementer", "Task", "Work")
    with pytest.raises(ConflictError, match="initialize_workflow.*set_workflow_status"):
        store.set_workflow_status(WorkflowStatus.DONE, "Done")

    assert store.get_state()["workflow"] is None
    store.initialize_workflow("Address issue #5", {"max_task_lease_min": 10})
    assert store.assign_task("bob", "implementer", "Task", "Work").workflow_id


def test_timestamps_stay_comparable_against_stored_leases(store: HubStore) -> None:
    # Leases are compared lexically in SQL, so both sides must share a format.
    assert to_iso(utcnow()) < iso_after(60)
