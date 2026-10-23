import asyncio
import logging
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


def test_expire_event_leases_redelivers_without_touching_worker_lease(store: HubStore) -> None:
    store.check_in("bob", CLAUDE)
    task = store.assign_task("bob", "implementer", "Fix", "Do it")
    before = store.get_task(task.id)
    first = store.lease_next_event()
    assert first is not None

    assert store.expire_event_leases("not-a-delivery") == 0
    assert store.lease_next_event() is None
    assert store.expire_event_leases() == 1
    redelivered = store.lease_next_event()

    assert redelivered is not None and redelivered.id == first.id
    assert redelivered.delivery_attempts == 2
    assert redelivered.delivery_id != first.delivery_id
    assert store.get_task(task.id) == before
    assert not store.ack_event(first.delivery_id)
    assert store.ack_event(redelivered.delivery_id)


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
        if "FROM task" in sql and not changed:
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
        "standing_role": "worker",
        "harness": "claude-code",
        "harness_source": "unknown",
        "harness_version": "2.1.268",
        "provider": "anthropic",
        "model": "claude-opus-5",
        "model_source": "env",
        "effort": "unknown",
        "effort_source": "unknown",
        "declared_model": "unknown",
        "model_mismatch": False,
        "capabilities": ["python"],
        "workspace_id": None,
        "context_id": agent.context_id,
        "worker_instance_id": "bob-1",
        "takeover": False,
        "reattached_task_id": None,
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
        "standing_role": "worker",
        "status": "idle",
        "context_id": "",
        "last_seen": "",
        "worker_instance_id": "",
        "last_heartbeat": "",
        "last_progress_at": None,
        "current_task_id": None,
        "harness": "claude-code",
        "harness_source": "unknown",
        "harness_version": "2.1.268",
        "provider": "anthropic",
        "model": "claude-opus-5",
        "model_source": "env",
        "effort": "unknown",
        "effort_source": "unknown",
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


@pytest.mark.parametrize("role", ["reviewer", "rebase"])
def test_a_review_or_rebase_assignment_is_bound_to_the_head_it_was_given(
    store: HubStore, role: str
) -> None:
    store.check_in("bob")

    task = store.assign_task("bob", role, f"{role} #7", "Work on it", pr_head_sha=HEAD.upper())

    assert task.pr_head_sha == HEAD
    assert store.get_state()["tasks"][0]["pr_head_sha"] == HEAD
    metadata = store.task_history(task.id)[0].parts[0]["metadata"]
    assert metadata[MetaKeys.PR_HEAD_SHA] == HEAD


@pytest.mark.parametrize("role", ["reviewer", "rebase"])
def test_a_review_or_rebase_assignment_without_pr_head_sha_is_refused(
    store: HubStore, role: str
) -> None:
    store.check_in("bob")

    with pytest.raises(ValueError, match=rf"pr_head_sha is required for {role} tasks"):
        store.assign_task("bob", role, f"{role} #7", "Work on it")

    assert store.tasks() == []
    agent = store.agent_by_name("bob")
    assert agent is not None and agent.status is AgentStatus.IDLE


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
    assert waited.already_held is False
    # #326: the same worker calling again gets its held task back as a
    # redelivery instead of timing out.
    redelivered = await store.await_assignment(agent.context_id, 0.05)
    assert redelivered is not None and not isinstance(redelivered, Released)
    assert redelivered.id == waited.id
    assert redelivered.already_held is True
    assert redelivered.state is TaskState.WORKING


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
    # #264: WSL wall-clock resync can outpace a negative one-second cutoff.
    # Model silence explicitly; wall-clock rollback has its own regression.
    clock = FakeClock()
    store.clock = clock
    store.check_in("bob")
    task_id = assign(store)
    clock.advance(seconds=2)
    store.sweep(lost_after_s=1)

    agent = store.agent_by_name("bob")
    task = store.get_task(task_id)
    assert agent is not None and agent.status is AgentStatus.LOST
    assert task is not None and task.state is TaskState.FAILED
    assert task.result is not None and task.result["reason"] == "worker_lost"
    assert store.sweep(lost_after_s=1) == []


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
    store.monotonic_clock = lambda: clock.now.timestamp()
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


def test_a_detached_instance_lets_its_successor_keep_the_open_task(
    store: HubStore,
) -> None:
    # A headless worker whose harness exits mid-task is resumed in seconds; its
    # new worker-mcp must be able to check in before the task is failed (#115).
    clock = FakeClock()
    store.clock = clock
    first = store.check_in("bob", worker_instance_id="bob-1")
    task_id = assign(store)

    assert store.detach("bob", "bob-1")
    clock.advance(seconds=30)
    restarted = store.check_in("bob", worker_instance_id="bob-2")

    assert restarted.context_id == first.context_id
    assert restarted.status is AgentStatus.BUSY
    assert restarted.current_task_id == task_id
    assert store.sweep(lost_after_s=180) == []
    store.submit_result(task_id, "bob", TaskState.COMPLETED, "done")
    task = store.get_task(task_id)
    assert task is not None and task.state is TaskState.COMPLETED


def test_a_detached_worker_that_never_returns_is_still_lost(store: HubStore) -> None:
    clock = FakeClock()
    store.clock = clock
    store.check_in("bob", worker_instance_id="bob-1")
    task_id = assign(store)
    assert store.detach("bob", "bob-1")

    # Detaching neither refreshes liveness nor stops the lost-window clock.
    clock.advance(seconds=181)
    assert [event.kind for event in store.sweep(lost_after_s=180)] == [EventKind.AGENT_LOST]
    task = store.get_task(task_id)
    assert task is not None and task.state is TaskState.FAILED


def test_only_the_live_instance_can_detach(store: HubStore) -> None:
    store.check_in("bob", worker_instance_id="bob-1")
    task_id = assign(store)

    # A stale or foreign instance cannot free the slot of a running worker.
    assert not store.detach("bob", "bob-0")
    with pytest.raises(DuplicateAgentError, match="live worker instance"):
        store.check_in("bob", worker_instance_id="bob-2")

    store.submit_result(task_id, "bob", TaskState.COMPLETED, "done")
    store.release_agent("bob")
    assert not store.detach("bob", "bob-1")
    released = store.agent_by_name("bob")
    assert released is not None and released.worker_instance_id == "bob-1"


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

    with pytest.raises(ConflictError, match="queue a different run") as error:
        restarted.initialize_workflow(
            "Address issue #5", {"max_task_lease_min": 20, "merge_method": "merge"}
        )
    assert "HUB_STATE_DIR" not in str(error.value)


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
        store.set_workflow_status(WorkflowStatus.DONE, "Done", actor="alice", session=None)

    assert store.get_state()["workflow"] is None
    store.initialize_workflow("Address issue #5", {"max_task_lease_min": 10})
    assert store.assign_task("bob", "implementer", "Task", "Work").workflow_id


def test_timestamps_stay_comparable_against_stored_leases(store: HubStore) -> None:
    # Leases are compared lexically in SQL, so both sides must share a format.
    assert to_iso(utcnow()) < iso_after(60)


def _resume(store: HubStore, summary: str) -> str:
    """Answer every open question, leave escalation, and return the rationale."""

    for question in store.open_operator_questions():
        store.answer_operator_question(question["question_id"], "yes")
    store.set_workflow_status(WorkflowStatus.ACTIVE, summary, actor="alice", session=None)
    with database(store.path) as connection:
        row = connection.execute("SELECT rationale FROM decision ORDER BY id DESC").fetchone()
    return str(row["rationale"])


def test_a_resume_names_its_own_questions_whatever_the_clock_does(store: HubStore) -> None:
    """#132 r1-1: questions are tied to the resume that ends them, not to timestamps."""

    clock = FakeClock()
    store.clock = clock
    # Two whole cycles within one millisecond.
    store.ask_user("First?", None, actor="alice", session=None)
    assert _resume(store, "One") == "Workflow status set to active; operator answered questions 1"
    store.ask_user("Second?", None, actor="alice", session=None)
    assert _resume(store, "Two") == "Workflow status set to active; operator answered questions 2"

    # The clock steps back between two questions of one escalation.
    store.ask_user("Third?", None, actor="alice", session=None)
    clock.advance(seconds=-2)
    store.ask_user("Fourth?", None, actor="alice", session=None)
    assert _resume(store, "Three") == (
        "Workflow status set to active; operator answered questions 3, 4"
    )


def test_decision_text_cannot_move_which_questions_a_resume_names(store: HubStore) -> None:
    """#132 r1-1: a logged decision that reads like an escalation changes nothing."""

    store.ask_user("Merge?", None, actor="alice", session=None)
    store.log_decision("Spoof", "Workflow status set to escalated", actor="alice", session=None)
    assert _resume(store, "Go") == "Workflow status set to active; operator answered questions 1"


def _task_wait_question(store: HubStore) -> tuple[str, int]:
    store.check_in("bob")
    task_id = assign(store)
    question_id = store.ask_user(
        "Keep waiting or reassign?",
        None,
        actor="alice",
        session="s1",
        waiting_on_task_id=task_id,
    )
    return task_id, question_id


def _complete_waiting_task(store: HubStore, task_id: str) -> int:
    store.submit_result(task_id, "bob", TaskState.COMPLETED, "Done")
    with database(store.path) as connection:
        return int(connection.execute("SELECT MAX(id) FROM event").fetchone()[0])


def test_withdrawal_retains_evidence_and_resumes_without_authority(store: HubStore) -> None:
    # Completion and question may have identical timestamps; event ids order them.
    store.clock = FakeClock()
    task_id, question_id = _task_wait_question(store)
    event_id = _complete_waiting_task(store, task_id)
    store.withdraw_question(
        question_id, "The task completed", event_id, actor="alice", session="s1"
    )
    assert store.open_operator_questions() == []
    assert store.status_summary()["operator_questions"] == 0
    answer = store.operator_answer(question_id)
    assert answer["status"] == "withdrawn"
    assert answer["answer"] is None and answer["answered"] is None
    assert answer["settling_event_id"] == event_id
    with pytest.raises(ConflictError, match="already closed"):
        store.withdraw_question(question_id, "Again", event_id, actor="alice", session="s1")
    with pytest.raises(ConflictError, match="withdrawn"):
        store.answer_operator_question(question_id, "reassign")
    with pytest.raises(ConflictError, match="escalated"):
        store.refuse_while_escalated("check_merge_gate")
    store.set_workflow_status(WorkflowStatus.ACTIVE, "Review result", actor="alice", session="s1")
    store.refuse_while_escalated("check_merge_gate")
    with database(store.path) as connection:
        row = connection.execute("SELECT * FROM operator_question").fetchone()
        assert (row["withdrawn_by"], row["withdrawn_session"], row["withdrawal_reason"]) == (
            "alice",
            "s1",
            "The task completed",
        )
        assert row["withdrawn"] and row["resumed_by"]
        decision = connection.execute(
            "SELECT rationale FROM decision WHERE id = ?", (row["resumed_by"],)
        ).fetchone()
        assert decision[0] == "Workflow status set to active; orchestrator withdrew questions 1"
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM event WHERE kind = 'user_answered'"
            ).fetchone()[0]
            == 0
        )


@pytest.mark.parametrize(
    "bad_evidence", ["missing", "old", "wrong_task", "wrong_kind", "unfinished"]
)
def test_withdrawal_rejects_unsettling_events(store: HubStore, bad_evidence: str) -> None:
    store.check_in("bob")
    task_id = assign(store)
    old = store.append_event(EventKind.TASK_COMPLETED, {"task_id": task_id}).id
    question_id = store.ask_user(
        "Wait?", None, actor="alice", session="s1", waiting_on_task_id=task_id
    )
    event_id = old
    if bad_evidence == "missing":
        event_id = 999
    elif bad_evidence != "old":
        kind = EventKind.TASK_FAILED if bad_evidence == "wrong_kind" else EventKind.TASK_COMPLETED
        event_id = store.append_event(
            kind, {"task_id": "another" if bad_evidence == "wrong_task" else task_id}
        ).id
    if bad_evidence != "unfinished":
        _complete_waiting_task(store, task_id)
    with pytest.raises(ConflictError):
        store.withdraw_question(question_id, "Moot", event_id, actor="alice", session="s1")
    assert store.operator_answer(question_id)["status"] == "unanswered"
    with database(store.path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM decision").fetchone()[0] == 1


@pytest.mark.parametrize("actor,session", [("alice", "s2"), ("other", "s1")])
def test_only_the_asking_session_can_withdraw(store: HubStore, actor: str, session: str) -> None:
    task_id, question_id = _task_wait_question(store)
    event_id = _complete_waiting_task(store, task_id)
    with pytest.raises(ConflictError, match="asking actor and session"):
        store.withdraw_question(question_id, "Moot", event_id, actor=actor, session=session)


def test_withdrawal_does_not_clear_an_authority_question(store: HubStore) -> None:
    task_id, question_id = _task_wait_question(store)
    authority_id = store.ask_user("Skip CI?", None, actor="alice", session="s1")
    event_id = _complete_waiting_task(store, task_id)
    with pytest.raises(ConflictError, match="only task-wait"):
        store.withdraw_question(authority_id, "Task done", event_id, actor="alice", session="s1")
    store.withdraw_question(question_id, "Moot", event_id, actor="alice", session="s1")
    with pytest.raises(ConflictError, match="open operator questions 2"):
        store.set_workflow_status(WorkflowStatus.ACTIVE, "Go", actor="alice", session="s1")
    store.answer_operator_question(authority_id, "No, run CI")
    store.set_workflow_status(WorkflowStatus.ACTIVE, "Run CI", actor="alice", session="s1")
    with database(store.path) as connection:
        rationale = connection.execute(
            "SELECT rationale FROM decision ORDER BY id DESC"
        ).fetchone()[0]
    assert (
        rationale == "Workflow status set to active; operator answered questions 2;"
        " orchestrator withdrew questions 1"
    )


def test_withdrawal_rejects_closed_questions_and_invalid_reasons(store: HubStore) -> None:
    task_id, question_id = _task_wait_question(store)
    event_id = _complete_waiting_task(store, task_id)
    with pytest.raises(ValueError, match="empty"):
        store.withdraw_question(question_id, " ", event_id, actor="alice", session="s1")
    store.answer_operator_question(question_id, "wait")
    with pytest.raises(ConflictError, match="already closed"):
        store.withdraw_question(question_id, "Moot", event_id, actor="alice", session="s1")


def test_task_wait_binding_requires_an_open_task(store: HubStore) -> None:
    store.check_in("bob")
    task_id = assign(store)
    _complete_waiting_task(store, task_id)
    for task in (task_id, "missing"):
        with pytest.raises((ConflictError, NotFoundError)):
            store.ask_user("Wait?", None, actor="alice", session="s1", waiting_on_task_id=task)
    assert store.open_operator_questions() == []
    assert store.get_state()["workflow"]["status"] == "active"


class PauseClocks(FakeClock):
    """Independently advance wall and monotonic time without sleeping."""

    def __init__(self) -> None:
        super().__init__()
        self.mono = 100.0

    def monotonic(self) -> float:
        return self.mono

    def step(self, seconds: float, *, monotonic_seconds: float | None = None) -> None:
        self.advance(seconds=seconds)
        self.mono += seconds if monotonic_seconds is None else monotonic_seconds


@pytest.fixture
def pause_store(store: HubStore) -> tuple[HubStore, PauseClocks]:
    clocks = PauseClocks()
    store.clock = clocks
    store.monotonic_clock = clocks.monotonic
    store.check_in("bob", worker_instance_id="bob-1")
    assert store.sweep(180) == []  # Establish the consecutive-sweep baseline.
    return store, clocks


def test_pause_is_durable_and_grants_one_grace_window(
    pause_store: tuple[HubStore, PauseClocks], caplog: pytest.LogCaptureFixture
) -> None:
    store, clocks = pause_store
    task = store.assign_task("bob", "implementer", "Fix", "Work", lease_min=1)
    delivery = store.next_event(lease_s=60)
    assert delivery is not None
    clocks.step(4 * 3600, monotonic_seconds=10)

    with caplog.at_level(logging.WARNING):
        events = store.sweep(180)
    assert [e.kind for e in events] == [EventKind.HOST_PAUSED]
    assert events[0].payload["clock_jump_s"] == 4 * 3600 - 10
    assert events[0].payload["grace_s"] == 180
    assert "Host pause/clock jump detected" in caplog.text
    current_task = store.get_task(task.id)
    current_agent = store.agent_by_name("bob")
    assert current_task is not None and current_task.lease_expires == to_iso(
        clocks.now + timedelta(seconds=50)
    )
    assert current_agent is not None and current_agent.status is AgentStatus.BUSY
    with database(store.path) as connection:
        assert (
            connection.execute("SELECT COUNT(*) FROM event WHERE kind = 'host_paused'").fetchone()[
                0
            ]
            == 1
        )
    assert store.sweep(180) == []
    pause_delivery = store.next_event()
    assert pause_delivery is not None and pause_delivery.id == events[0].id
    assert store.next_event() is None  # Original delivery still belongs to Alice.
    assert store._earliest_delivery_expires_s() == 180

    clocks.step(179)
    assert store.sweep(180) == []
    assert store.next_event() is None
    clocks.step(1)
    expired = store.sweep(180)
    assert [e.kind for e in expired] == [EventKind.LEASE_EXPIRED, EventKind.AGENT_LOST]
    failed = store.get_task(task.id)
    assert failed is not None and failed.state is TaskState.FAILED
    redelivery = store.next_event()
    assert redelivery is not None and redelivery.id == delivery.id
    assert redelivery.delivery_id != delivery.delivery_id
    assert store.sweep(180) == []


@pytest.mark.parametrize("monotonic_seconds", [10, 600])
def test_heartbeat_during_grace_keeps_worker_and_task_live(
    pause_store: tuple[HubStore, PauseClocks], monotonic_seconds: float
) -> None:
    store, clocks = pause_store
    task = store.assign_task("bob", "implementer", "Fix", "Work", lease_min=1)
    clocks.step(600, monotonic_seconds=monotonic_seconds)
    store.sweep(180)
    for _ in range(6):
        clocks.step(30)
        assert store.heartbeat("bob", "bob-1", task.id)
        assert store.sweep(180) == []
    current_agent = store.agent_by_name("bob")
    current_task = store.get_task(task.id)
    assert current_agent is not None and current_agent.status is AgentStatus.BUSY
    assert current_task is not None and current_task.state is TaskState.SUBMITTED
    assert current_task.lease_expires == to_iso(clocks.now + timedelta(minutes=1))
    clocks.step(60)
    assert [e.kind for e in store.sweep(180)] == [EventKind.LEASE_EXPIRED]
    clocks.step(120)
    assert [e.kind for e in store.sweep(180)] == [EventKind.AGENT_LOST]


def test_pause_offset_preserves_the_active_renewal_cap_across_store_restart(
    pause_store: tuple[HubStore, PauseClocks],
) -> None:
    store, clocks = pause_store
    task = store.assign_task("bob", "implementer", "Fix", "Work", lease_min=1)
    clocks.step(4 * 3600, monotonic_seconds=10)
    store.sweep(180)
    restarted = HubStore(store.path, clock=clocks, monotonic_clock=clocks.monotonic)
    for _ in range(6):
        clocks.step(30)
        assert restarted.heartbeat("bob", "bob-1", task.id, max_task_lease_min=4)
        assert store.sweep(180) == []
    current = store.get_task(task.id)
    assert current is not None
    # Of the four-minute active cap, ten seconds passed before detection and
    # 180 since wake. Only 50 seconds remain, despite the four-hour wall jump.
    assert current.lease_expires == to_iso(clocks.now + timedelta(seconds=50))
    clocks.step(50)
    assert [e.kind for e in store.sweep(180)] == [EventKind.LEASE_EXPIRED]


def test_pause_does_not_extend_already_expired_or_newly_created_tasks(
    pause_store: tuple[HubStore, PauseClocks],
) -> None:
    store, clocks = pause_store
    expired = store.assign_task("bob", "implementer", "Old", "Work", lease_min=-1)
    store.check_in("charlie", worker_instance_id="charlie-1")
    clocks.step(3600, monotonic_seconds=10)
    fresh = store.assign_task("charlie", "implementer", "New", "Work")
    event = store.sweep(180)[0]
    assert event.payload["shifted_task_leases"] == 0
    for before in (expired, fresh):
        after = store.get_task(before.id)
        assert after is not None and after.lease_expires == before.lease_expires


def test_pause_event_and_lease_shift_roll_back_together(
    pause_store: tuple[HubStore, PauseClocks], monkeypatch: pytest.MonkeyPatch
) -> None:
    store, clocks = pause_store
    task = store.assign_task("bob", "implementer", "Fix", "Work", lease_min=1)
    clocks.step(3600, monotonic_seconds=10)
    original = HubStore._add_event

    def fail_pause(self: HubStore, connection: Connection, kind: EventKind, payload: object) -> int:
        if kind is EventKind.HOST_PAUSED:
            raise RuntimeError("injected pause event write failure")
        raise AssertionError("unexpected event")

    monkeypatch.setattr(HubStore, "_add_event", fail_pause)
    with pytest.raises(RuntimeError, match="injected pause event write failure"):
        store.sweep(180)
    current = store.get_task(task.id)
    assert current is not None and current.lease_expires == task.lease_expires
    with database(store.path) as connection:
        assert (
            connection.execute(
                "SELECT lease_clock_shift_s FROM task WHERE id = ?", (task.id,)
            ).fetchone()[0]
            == 0
        )
        assert (
            connection.execute("SELECT COUNT(*) FROM event WHERE kind = 'host_paused'").fetchone()[
                0
            ]
            == 0
        )
    monkeypatch.setattr(HubStore, "_add_event", original)
    event = store.sweep(180)[0]
    assert event.kind is EventKind.HOST_PAUSED
    assert event.payload["shifted_task_leases"] == 1


@pytest.mark.parametrize("monotonic_seconds,reason", [(10, "clock_jump"), (3600, "sweep_gap")])
def test_inbox_read_before_wake_sweep_does_not_redeliver(
    pause_store: tuple[HubStore, PauseClocks], monotonic_seconds: float, reason: str
) -> None:
    store, clocks = pause_store
    first = store.next_event(lease_s=60)
    assert first is not None
    clocks.step(3600, monotonic_seconds=monotonic_seconds)
    resumed = store.next_event()
    assert resumed is not None and resumed.kind is EventKind.HOST_PAUSED
    assert resumed.payload["reason"] == reason
    assert store.sweep(180) == []
    assert store.next_event() is None
    assert store.heartbeat("bob", "bob-1", None)


def test_normal_sweep_steps_and_small_clock_corrections_do_not_grant_grace(
    pause_store: tuple[HubStore, PauseClocks],
) -> None:
    store, clocks = pause_store
    for _ in range(17):
        clocks.step(10)
        assert store.sweep(180) == []
    clocks.step(10.5, monotonic_seconds=10)
    assert [e.kind for e in store.sweep(180)] == [EventKind.AGENT_LOST]


def test_repeated_pause_restarts_monotonic_grace(
    pause_store: tuple[HubStore, PauseClocks],
) -> None:
    store, clocks = pause_store
    clocks.step(3600, monotonic_seconds=10)
    assert [e.kind for e in store.sweep(180)] == [EventKind.HOST_PAUSED]
    clocks.step(100)
    assert store.sweep(180) == []
    clocks.step(3600, monotonic_seconds=10)
    assert [e.kind for e in store.sweep(180)] == [EventKind.HOST_PAUSED]
    clocks.step(179)
    assert store.sweep(180) == []
    clocks.step(1)
    assert [e.kind for e in store.sweep(180)] == [EventKind.AGENT_LOST]


@pytest.mark.parametrize("bad_name", ["Feature.B", "state", "token", "con", "-bob", ""])
def test_check_in_validates_agent_names_like_hub_names(store: HubStore, bad_name: str) -> None:
    with pytest.raises(ValueError, match="agent name"):
        store.check_in(bad_name)


def test_standing_roles_enforce_compatible_task_assignments(store: HubStore) -> None:
    store.initialize_workflow()
    store.check_in("bob", standing_role="implementer", worker_instance_id="bob-1")
    store.check_in("charlie", standing_role="reviewer", worker_instance_id="charlie-1")
    store.check_in("dan", standing_role="worker", worker_instance_id="dan-1")

    with pytest.raises(ValueError, match=r"standing role '?implementer'?"):
        store.assign_task("bob", "reviewer", "Review", "Look", pr_head_sha=HEAD)
    with pytest.raises(ValueError, match=r"standing role '?reviewer'?"):
        store.assign_task("charlie", "implementer", "Impl", "Build")
    with pytest.raises(ValueError, match=r"standing role '?reviewer'?"):
        store.assign_task("charlie", "rebase", "Rebase", "Rebase branch", pr_head_sha=HEAD)

    impl_task = store.assign_task("bob", "implementer", "Impl", "Build")
    store.submit_result(impl_task.id, "bob", TaskState.COMPLETED, "done")
    rebase_task = store.assign_task("bob", "rebase", "Rebase", "Rebase branch", pr_head_sha=HEAD)
    store.submit_result(rebase_task.id, "bob", TaskState.COMPLETED, "done")
    rev_task = store.assign_task("charlie", "reviewer", "Review", "Look", pr_head_sha=HEAD)
    store.submit_result(rev_task.id, "charlie", TaskState.COMPLETED, "done")

    # Standing role 'worker' accepts every task role.
    for task_role in ("implementer", "reviewer", "rebase"):
        head = None if task_role == "implementer" else HEAD
        task = store.assign_task("dan", task_role, f"Task {task_role}", "Do work", pr_head_sha=head)
        store.submit_result(task.id, "dan", TaskState.COMPLETED, "done")


def test_takeover_supersedes_old_instance_and_reattaches_live_task(store: HubStore) -> None:
    store.initialize_workflow()
    store.check_in("bob", CLAUDE, standing_role="implementer", worker_instance_id="bob-1")
    _ = store.next_event()
    task = store.assign_task(
        "bob", "implementer", "IMPLEMENT for #238", "Implement join", pr_head_sha=HEAD
    )
    assert task.state is TaskState.SUBMITTED

    # Without takeover, a second live instance gets 409 DuplicateAgentError.
    with pytest.raises(DuplicateAgentError, match=r"(?i)takeover=true"):
        store.check_in("bob", CLAUDE, standing_role="implementer", worker_instance_id="bob-2")

    # With takeover=True, the new instance supersedes bob-1 and reattaches the held task.
    agent = store.check_in(
        "bob",
        CLAUDE,
        standing_role="implementer",
        worker_instance_id="bob-2",
        takeover=True,
    )
    assert agent.worker_instance_id == "bob-2"
    assert agent.status is AgentStatus.BUSY
    assert agent.current_task_id == task.id
    reattached = agent.reattached_task
    assert reattached is not None
    assert reattached["task_id"] == task.id
    assert reattached["role"] == "implementer"
    assert reattached["title"] == "IMPLEMENT for #238"
    assert reattached["instructions"] == "Implement join"
    assert reattached["pr_head_sha"] == HEAD
    assert reattached["state"] == "working"

    # The task is still working with the same task ID, and no AGENT_LOST event was queued.
    current_task = store.get_task(task.id)
    assert current_task is not None and current_task.state is TaskState.WORKING
    event = store.next_event()
    assert event is not None and event.kind is EventKind.AGENT_CHECKED_IN
    assert event.payload["takeover"] is True
    assert event.payload["reattached_task_id"] == task.id
    assert store.next_event() is None

    # The old instance's heartbeats are ignored; the new instance's heartbeats are accepted.
    assert store.heartbeat("bob", "bob-1", task.id) is False
    assert store.heartbeat("bob", "bob-2", task.id) is True


def _run_id_format(run_id: str) -> bool:
    import re

    return bool(re.fullmatch(r"\d{8}_[0-9a-f]{8}", run_id))


def test_run_identity_has_unique_run_ids_and_statement(tmp_path: Path) -> None:
    """M2 Step 3: every workflow gets a `<YYYYMMDD>_<8 hex>` run_id and statement."""

    from agent_hub.database import database as db_conn

    path = tmp_path / "run-id.db"
    initialize_database(path)
    store = HubStore(path)
    first = store.initialize_workflow("First run")
    state = store.get_state()
    assert state["workflow"] is not None
    first_run_id = state["workflow"]["run_id"]
    assert _run_id_format(first_run_id)
    assert state["workflow"]["statement_sha256"] is not None
    assert state["run_id"] == first_run_id
    with db_conn(path) as connection:
        row = connection.execute(
            "SELECT run_id, statement_sha256 FROM workflow WHERE id = ?", (first,)
        ).fetchone()
        assert row["run_id"] == first_run_id
        assert row["statement_sha256"] is not None
        srow = connection.execute(
            "SELECT sha256, size, source FROM statement WHERE sha256 = ?",
            (row["statement_sha256"],),
        ).fetchone()
        assert srow is not None and srow["sha256"] == row["statement_sha256"]
    # Second run after done gets a new run_id.
    store.set_workflow_status(WorkflowStatus.DONE, "Done", actor="alice", session=None)
    second = store.start_workflow("Second run")
    assert second != first
    second_state = store.get_state()
    assert second_state["workflow"] is not None
    second_run_id = second_state["workflow"]["run_id"]
    assert _run_id_format(second_run_id) and second_run_id != first_run_id


def test_sequential_queue_second_starts_after_first_done(tmp_path: Path) -> None:
    """Two statements queued; the second starts only after the first is done."""

    path = tmp_path / "queue.db"
    initialize_database(path)
    store = HubStore(path)
    first = store.start_workflow("First statement")
    assert store.get_state()["workflow"]["id"] == first
    assert store.get_state()["workflow"]["status"] == "active"
    second = store.start_workflow("Second statement")
    assert second != first
    state = store.get_state()
    # Live stays first; second is queued.
    assert state["workflow"]["id"] == first
    assert len(state["queue"]) == 1 and state["queue"][0]["workflow_id"] == second
    # A third while live + queued exist is refused with conflict.
    with pytest.raises(ConflictError):
        store.start_workflow("Third statement")
    # Completing the first activates the second.
    store.set_workflow_status(WorkflowStatus.DONE, "Done", actor="alice", session=None)
    after = store.get_state()
    assert after["workflow"] is not None and after["workflow"]["id"] == second
    assert after["workflow"]["status"] == "active"
    assert after["queue"] == []
    assert after["previous_run"] is not None
    assert after["previous_run"]["run_id"] != after["workflow"]["run_id"]


def test_backward_clock_previous_run_uses_insertion_order(tmp_path: Path) -> None:
    """#209: run ordering is durable insertion order, not wall-clock `created`.

    Deterministic reproduction from the issue: First at T, Second at T-1s
    (backward step), Third at T+1s. `previous_run` must be Second, and that
    order survives a hub restart (rowid is durable).
    """

    path = tmp_path / "backward-clock.db"
    initialize_database(path)
    clock = FakeClock()
    store = HubStore(path, clock=clock)
    store.start_workflow("First")
    first_run_id = store.get_state()["run_id"]
    store.set_workflow_status(WorkflowStatus.DONE, "Done", actor="alice", session=None)
    # Backward step: Second is created with an earlier wall-clock time.
    clock.now -= timedelta(seconds=1)
    store.start_workflow("Second")
    second_run_id = store.get_state()["run_id"]
    assert second_run_id != first_run_id
    store.set_workflow_status(WorkflowStatus.DONE, "Done", actor="alice", session=None)
    # The most recent done run is Second by insertion, not First by timestamp.
    state = store.get_state()
    assert state["workflow"] is not None and state["workflow"]["run_id"] == second_run_id
    assert state["previous_run"] is not None
    assert state["previous_run"]["run_id"] == first_run_id
    assert state["previous_run"]["goal"] == "First"
    clock.advance(seconds=2)
    store.start_workflow("Third")
    live = store.get_state()
    assert live["workflow"] is not None and live["workflow"]["goal"] == "Third"
    assert live["previous_run"] is not None
    assert live["previous_run"]["run_id"] == second_run_id
    assert live["previous_run"]["goal"] == "Second"
    # Restart preserves insertion order: still Second, not First.
    restarted = HubStore(path, clock=clock)
    after = restarted.get_state()
    assert after["workflow"] is not None and after["workflow"]["goal"] == "Third"
    assert after["previous_run"] is not None
    assert after["previous_run"]["run_id"] == second_run_id
    assert after["previous_run"]["goal"] == "Second"


def test_backward_clock_task_order_uses_insertion_order(tmp_path: Path) -> None:
    """#209: task orderings use insertion order when `created` steps backwards."""

    path = tmp_path / "backward-clock-tasks.db"
    initialize_database(path)
    clock = FakeClock()
    store = HubStore(path, clock=clock)
    store.initialize_workflow("Task order run")
    store.check_in("bob")
    store.check_in("charlie")
    first = store.assign_task("bob", "implementer", "First task", "Do first")
    clock.now -= timedelta(seconds=1)
    second = store.assign_task("charlie", "implementer", "Second task", "Do second")
    tasks = store.tasks()
    assert [task.id for task in tasks] == [first.id, second.id]
    state = store.get_state()
    assert [task["id"] for task in state["tasks"]] == [first.id, second.id]


def test_backward_clock_live_queued_order_uses_insertion_order(tmp_path: Path) -> None:
    """#209 (r1-4): live stays live when a queued run has an earlier timestamp."""

    path = tmp_path / "backward-clock-queue.db"
    initialize_database(path)
    clock = FakeClock()
    store = HubStore(path, clock=clock)
    first = store.start_workflow("Live run")
    assert store.get_state()["workflow"]["id"] == first
    # Backward step: the queued run is created with an earlier wall-clock time.
    clock.now -= timedelta(seconds=1)
    second = store.start_workflow("Queued run")
    state = store.get_state()
    assert state["workflow"] is not None and state["workflow"]["id"] == first
    assert len(state["queue"]) == 1 and state["queue"][0]["workflow_id"] == second
    # Completing the live run activates the queued one by insertion order.
    store.set_workflow_status(WorkflowStatus.DONE, "Done", actor="alice", session=None)
    after = store.get_state()
    assert after["workflow"] is not None and after["workflow"]["id"] == second
    assert after["previous_run"] is not None
    assert after["previous_run"]["workflow_id"] == first


def test_initialize_workflow_confirms_active_and_starts_new_after_done(
    tmp_path: Path,
) -> None:
    """initialize_workflow with the same goal confirms; after done it starts new."""

    path = tmp_path / "init-alias.db"
    initialize_database(path)
    store = HubStore(path)
    first = store.initialize_workflow("Same goal")
    assert store.initialize_workflow("Same goal") == first
    first_run_id = store.get_state()["workflow"]["run_id"]
    store.set_workflow_status(WorkflowStatus.DONE, "Done", actor="alice", session=None)
    second = store.initialize_workflow("New goal")
    assert second != first
    assert store.get_state()["workflow"]["run_id"] != first_run_id


def test_initialize_workflow_mismatch_names_expected_sha(tmp_path: Path) -> None:
    """#296 item 4: a mismatched initialize_workflow names the expected sha."""

    import hashlib

    path = tmp_path / "guard.db"
    initialize_database(path)
    store = HubStore(path)
    goal = "Queued goal for #296"
    expected_sha = hashlib.sha256(goal.encode()).hexdigest()
    store.start_workflow(goal, source="prepare-run", force_queued=True)
    with pytest.raises(ConflictError, match=f"Expected statement_sha256={expected_sha}"):
        store.initialize_workflow("A different goal")
    # Starting the queued run yields the matching sha.
    store.start_workflow()
    live = store.get_state()["workflow"]
    assert live is not None and live["statement_sha256"] == expected_sha


def test_manifest_written_at_start_and_completed_at_done(tmp_path: Path) -> None:
    """Manifest is written at start and completed at done; restart keeps valid JSON."""

    import json

    path = tmp_path / "hub.db"
    initialize_database(path)
    store = HubStore(path)
    workflow_id = store.start_workflow("Manifest run")
    run_id = store.get_state()["workflow"]["run_id"]
    manifest_path = tmp_path / "runs" / run_id / "manifest.json"
    assert manifest_path.is_file()
    first = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert first["run_id"] == run_id and first["workflow_id"] == workflow_id
    assert first["status"] in ("active", "queued")
    assert first["completed"] is None
    assert first["agents"] == []
    # Restart: a new store on the same path sees the same run and valid JSON.
    restarted = HubStore(path)
    assert restarted.get_state()["workflow"]["run_id"] == run_id
    assert json.loads(manifest_path.read_text(encoding="utf-8"))["run_id"] == run_id
    # Give the run a task, an agent and a PR result so the end fields populate.
    restarted.check_in("bob", AgentProfile(harness="codex", model="gpt-6-sol"))
    task = restarted.assign_task("bob", "implementer", "Build", "Do work")
    restarted.submit_result(
        task.id,
        "bob",
        {
            "outcome": "completed",
            "summary": "done",
            "pr_url": "https://github.com/octo/sandbox/pull/7",
            "head_sha": "a" * 40,
        },
    )
    restarted.set_workflow_status(WorkflowStatus.DONE, "Done", actor="alice", session=None)
    completed = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert completed["status"] == "done" and completed["run_id"] == run_id
    assert completed["completed"] is not None
    assert completed["pr_url"] == "https://github.com/octo/sandbox/pull/7"
    assert completed["approved_sha"] is None
    # merged_sha stays null until the hub records a merge commit (r1-2).
    assert completed["merged_sha"] is None
    assert any(agent["name"] == "bob" for agent in completed["agents"])


def test_on_done_release_vs_standby(tmp_path: Path) -> None:
    """on_done release frees workers when the queue is empty; standby keeps them."""

    for on_done, expect_released in (("release", True), ("standby", False)):
        path = tmp_path / f"on-done-{on_done}.db"
        initialize_database(path)
        store = HubStore(path)
        store.initialize_workflow("Policy run", {"on_done": on_done})
        store.check_in("bob")
        bob = store.agent_by_name("bob")
        assert bob is not None
        assert bob.status is AgentStatus.IDLE
        store.set_workflow_status(WorkflowStatus.DONE, "Done", actor="alice", session=None)
        bob = store.agent_by_name("bob")
        assert bob is not None
        if expect_released:
            assert bob.status is AgentStatus.RELEASED
        else:
            assert bob.status is AgentStatus.IDLE
    # With a queued run waiting, even release keeps workers for the next run.
    path = tmp_path / "on-done-queued.db"
    initialize_database(path)
    store = HubStore(path)
    store.initialize_workflow("First", {"on_done": "release"})
    store.check_in("bob")
    store.start_workflow("Second")
    store.set_workflow_status(WorkflowStatus.DONE, "Done", actor="alice", session=None)
    bob = store.agent_by_name("bob")
    assert bob is not None
    assert bob.status is AgentStatus.IDLE
    assert store.get_state()["workflow"]["goal"] == "Second"


def test_assignments_carry_run_id(tmp_path: Path) -> None:
    """Assignments carry the run_id so agents can cite it."""

    path = tmp_path / "assign-run-id.db"
    initialize_database(path)
    store = HubStore(path)
    store.initialize_workflow("Run with task")
    store.check_in("bob")
    task = store.assign_task("bob", "implementer", "Build", "Do work")
    assert task.run_id is not None
    assert task.run_id == store.get_state()["workflow"]["run_id"]
    assert task.statement_sha256 == store.get_state()["workflow"]["statement_sha256"]


def test_new_run_clears_stale_releases(tmp_path: Path) -> None:
    """A new live run stops returning releases to returning workers (r1-1)."""

    path = tmp_path / "stale-release.db"
    initialize_database(path)
    store = HubStore(path)
    store.initialize_workflow("Run one")
    store.check_in("bob", worker_instance_id="bob-1")
    # Default on_done=release with an empty queue releases bob at done.
    store.set_workflow_status(WorkflowStatus.DONE, "Done", actor="alice", session=None)
    bob = store.agent_by_name("bob")
    assert bob is not None
    assert bob.status is AgentStatus.RELEASED
    # A new run becomes live; the stale release must not stick.
    store.initialize_workflow("Run two")
    store.check_in("bob", worker_instance_id="bob-2")
    agent = store.agent_by_name("bob")
    assert agent is not None and agent.status is AgentStatus.IDLE
    task = store.assign_task("bob", "implementer", "Build", "Do work")
    claimed = store._claim_assignment("bob")
    assert claimed is not None and not isinstance(claimed, Released)
    assert claimed.id == task.id


def test_get_statement_returns_bytes_verbatim_and_unknown_is_not_found(tmp_path: Path) -> None:
    """M2 Step 4: stored bytes/SHA equal file's; CRLF preserved; unknown is NotFound."""

    from agent_hub.database import initialize_database
    from agent_hub.store import HubStore, NotFoundError

    path = tmp_path / "stmt.db"
    initialize_database(path)
    store = HubStore(path)
    text = "Fix the typo in README.\r\n"
    store.start_workflow(text, source="submit:file")
    state = store.get_state()
    sha = state["workflow"]["statement_sha256"]
    import hashlib

    assert sha == hashlib.sha256(text.encode("utf-8")).hexdigest()
    got = store.get_statement(sha)
    assert got["statement"] == text
    assert got["sha256"] == sha
    assert got["size"] == len(text.encode("utf-8"))
    with pytest.raises(NotFoundError):
        store.get_statement("0" * 64)


def test_store_statement_over_cap_is_refused(tmp_path: Path) -> None:
    """M2 Step 4: statement over 12 KiB cap refuses with cap in message."""

    from agent_hub.database import initialize_database
    from agent_hub.store import HubStore, PayloadTooLargeError
    from agent_hub_common import MAX_STATEMENT_BYTES

    path = tmp_path / "cap.db"
    initialize_database(path)
    store = HubStore(path)
    big = "x" * (MAX_STATEMENT_BYTES + 1)
    with pytest.raises(PayloadTooLargeError) as exc_info:
        store.start_workflow(big, source="submit:file")
    assert str(MAX_STATEMENT_BYTES) in str(exc_info.value)
    assert "Shorten" in str(exc_info.value) or "shorten" in str(exc_info.value).lower()


def test_chat_path_rejects_unresolved_allow_no_ci_auto(tmp_path: Path) -> None:
    """r1-2: chat initialize/start must not store unresolved 'auto' (merge reads bool)."""

    from agent_hub.database import initialize_database
    from agent_hub.store import HubStore, InvalidPolicyError

    path = tmp_path / "auto.db"
    initialize_database(path)
    store = HubStore(path)
    with pytest.raises(InvalidPolicyError, match="auto"):
        store.initialize_workflow("goal text", {"allow_no_ci": "auto"})
    with pytest.raises(InvalidPolicyError, match="auto"):
        store.start_workflow("statement text", {"allow_no_ci": "auto"})
    # Explicit bools still store.
    wid = store.start_workflow("statement text", {"allow_no_ci": True})
    assert wid


@pytest.mark.parametrize("entry", ["initialize", "start", "promote"])
def test_stale_release_requires_check_in_on_each_live_run_path(tmp_path: Path, entry: str) -> None:
    clock = FakeClock()
    store = HubStore(tmp_path / "release.db", clock=clock)
    initialize_database(store.path)
    store.initialize_workflow("First", {"on_done": "release"})
    store.check_in("bob", worker_instance_id="old")
    store.set_workflow_status(WorkflowStatus.DONE, "Done", actor="alice", session=None)
    if entry == "initialize":
        store.initialize_workflow("Second")
    elif entry == "start":
        store.start_workflow("Second")
    else:
        # The old released worker remains absent while other workers run.
        store.start_workflow("Intervening")
        # Exercise a released row at the promotion itself, as when Alice
        # releases an absent worker while the intervening run is active.
        store.release_agent("bob")
        store.check_in("charlie")
        store.start_workflow("Second")
        store.set_workflow_status(WorkflowStatus.DONE, "Done", actor="alice", session=None)
        charlie = store.agent_by_name("charlie")
        assert charlie is not None and charlie.status is AgentStatus.IDLE
        store.assign_task("charlie", "implementer", "Build", "Do work")
    bob = next(a for a in store.get_state()["agents"] if a["name"] == "bob")
    assert bob["status"] == "lost"
    bob_status = next(a for a in store.status_summary()["agents"] if a["name"] == "bob")
    assert bob_status["status"] == "lost" and not bob_status["alive"]
    with pytest.raises(ConflictError, match="must check in again before it can be given work"):
        store.assign_task("bob", "implementer", "Build", "Do work")
    clock.advance(seconds=61)
    if entry == "promote":
        charlie = store.agent_by_name("charlie")
        assert charlie is not None
        store.heartbeat("charlie", charlie.worker_instance_id, None)
    store.sweep(lost_after_s=60)
    with database(store.path) as connection:
        assert (
            connection.execute("SELECT count(*) FROM event WHERE kind = 'agent_lost'").fetchone()[0]
            == 0
        )
        assert (
            connection.execute("SELECT count(*) FROM task WHERE state = 'failed'").fetchone()[0]
            == 0
        )
    store.check_in("bob", worker_instance_id="new")
    bob = store.agent_by_name("bob")
    assert bob is not None and bob.status is AgentStatus.IDLE
    task = store.assign_task("bob", "implementer", "Build", "Do work")
    claimed = store._claim_assignment("bob")
    assert claimed is not None and not isinstance(claimed, Released)
    assert claimed.id == task.id


@pytest.mark.parametrize("queued", [False, True])
def test_joined_workers_remain_assignable_between_runs(tmp_path: Path, queued: bool) -> None:
    path = tmp_path / "joined.db"
    initialize_database(path)
    store = HubStore(path)
    store.initialize_workflow("First", {"on_done": "release" if queued else "standby"})
    store.check_in("bob", worker_instance_id="same")
    if queued:
        store.start_workflow("Second")
    store.set_workflow_status(WorkflowStatus.DONE, "Done", actor="alice", session=None)
    if not queued:
        store.start_workflow("Second")
    bob = store.agent_by_name("bob")
    assert bob is not None and bob.status is AgentStatus.IDLE
    task = store.assign_task("bob", "implementer", "Build", "Do work")
    claimed = store._claim_assignment("bob")
    assert claimed is not None and not isinstance(claimed, Released)
    assert claimed.id == task.id


async def test_held_working_task_is_redelivered_with_flag(store: HubStore) -> None:
    """#326: a worker that lost track gets its held task back, not a timeout."""

    agent = store.check_in("bob")
    task_id = assign(store)
    claimed = await store.await_assignment(agent.context_id, 2.0)
    assert claimed is not None and not isinstance(claimed, Released)
    assert claimed.id == task_id
    assert claimed.already_held is False
    before = store.get_task(task_id)
    assert before is not None

    redelivered = await store.await_assignment(agent.context_id, 0.05)
    assert redelivered is not None and not isinstance(redelivered, Released)
    assert redelivered.id == task_id
    assert redelivered.already_held is True
    assert redelivered.state is TaskState.WORKING
    # No state or lease change on redelivery: the heartbeat still owns renewal.
    after = store.get_task(task_id)
    assert after is not None
    assert after.state is TaskState.WORKING
    assert after.lease_expires == before.lease_expires


async def test_held_input_required_task_is_redelivered(store: HubStore) -> None:
    """#326: a parked question is also an open held task worth returning."""

    agent = store.check_in("bob")
    task_id = assign(store)
    claimed = await store.await_assignment(agent.context_id, 2.0)
    assert claimed is not None
    assert not isinstance(claimed, Released)
    assert claimed.id == task_id
    store.open_question(task_id, "bob", "Which base?", "q-1")

    redelivered = await store.await_assignment(agent.context_id, 0.05)
    assert redelivered is not None and not isinstance(redelivered, Released)
    assert redelivered.id == task_id
    assert redelivered.already_held is True
    assert redelivered.state is TaskState.INPUT_REQUIRED


async def test_released_worker_gets_release_despite_held_task(store: HubStore) -> None:
    """#326: release outranks redelivery."""

    agent = store.check_in("bob")
    task_id = assign(store)
    claimed = await store.await_assignment(agent.context_id, 2.0)
    assert claimed is not None
    assert not isinstance(claimed, Released)
    assert claimed.id == task_id
    store.release_agent("bob")

    assert await store.await_assignment(agent.context_id, 0.05) == Released(agent="bob")


def _complete_then_hold(store: HubStore, first_title: str = "First") -> tuple[str, str]:
    """Complete one task, then hold a second: the #326 lost-track shape."""

    store.check_in("bob")
    first_id = store.assign_task("bob", "implementer", first_title, "Do first").id
    store._claim_assignment("bob")
    store.submit_result(first_id, "bob", TaskState.COMPLETED, "first done")
    second_id = store.assign_task("bob", "implementer", "Second", "Do second").id
    store._claim_assignment("bob")
    return first_id, second_id


def test_report_progress_on_finished_names_held_task(store: HubStore) -> None:
    first_id, second_id = _complete_then_hold(store)
    with pytest.raises(ConflictError) as exc_info:
        store.record_progress(first_id, "bob", "late note")
    message = str(exc_info.value)
    assert "already completed" in message
    assert second_id in message
    assert "Second" in message
    assert "use that task_id" in message


def test_report_progress_on_finished_without_held_points_to_await(store: HubStore) -> None:
    store.check_in("bob")
    task_id = assign(store)
    store._claim_assignment("bob")
    store.submit_result(task_id, "bob", TaskState.COMPLETED, "done")
    with pytest.raises(ConflictError) as exc_info:
        store.record_progress(task_id, "bob", "late note")
    message = str(exc_info.value)
    assert "already completed" in message
    assert "hold no open task" in message
    assert "await_assignment" in message


def test_submit_result_on_finished_names_held_task(store: HubStore) -> None:
    first_id, second_id = _complete_then_hold(store)
    with pytest.raises(ConflictError) as exc_info:
        store.submit_result(first_id, "bob", TaskState.COMPLETED, "again")
    message = str(exc_info.value)
    assert "already completed" in message
    assert second_id in message
    assert "use that task_id" in message


def test_submit_result_on_finished_without_held_points_to_await(store: HubStore) -> None:
    store.check_in("bob")
    task_id = assign(store)
    store._claim_assignment("bob")
    store.submit_result(task_id, "bob", TaskState.COMPLETED, "done")
    with pytest.raises(ConflictError) as exc_info:
        store.submit_result(task_id, "bob", TaskState.COMPLETED, "again")
    message = str(exc_info.value)
    assert "hold no open task" in message
    assert "await_assignment" in message


def test_ask_alice_on_finished_names_held_task(store: HubStore) -> None:
    first_id, second_id = _complete_then_hold(store)
    with pytest.raises(ConflictError) as exc_info:
        store.open_question(first_id, "bob", "late question?", "q-9")
    message = str(exc_info.value)
    assert "already completed" in message
    assert second_id in message
    assert "use that task_id" in message


def test_ask_alice_on_finished_without_held_points_to_await(store: HubStore) -> None:
    store.check_in("bob")
    task_id = assign(store)
    store._claim_assignment("bob")
    store.submit_result(task_id, "bob", TaskState.COMPLETED, "done")
    with pytest.raises(ConflictError) as exc_info:
        store.open_question(task_id, "bob", "late question?", "q-9")
    assert "await_assignment" in str(exc_info.value)


def test_finished_task_error_names_only_the_callers_task(store: HubStore) -> None:
    """#326: never list other workers' tasks in a finished-task error."""

    store.check_in("bob")
    store.check_in("charlie")
    bob_task = store.assign_task("bob", "implementer", "Bob work", "Do it").id
    store._claim_assignment("bob")
    store.submit_result(bob_task, "bob", TaskState.COMPLETED, "bob done")
    charlie_task = store.assign_task("charlie", "implementer", "Charlie work", "Do it").id
    store._claim_assignment("charlie")
    with pytest.raises(ConflictError) as exc_info:
        store.record_progress(bob_task, "bob", "late note")
    message = str(exc_info.value)
    assert "await_assignment" in message
    assert charlie_task not in message
    assert "Charlie work" not in message
