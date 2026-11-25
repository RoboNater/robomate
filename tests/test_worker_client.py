import asyncio
import json
import subprocess
from collections.abc import AsyncIterator, Callable
from dataclasses import replace
from pathlib import Path
from typing import Any, cast

import httpx
import pytest
from agent_hub.store import HubStore
from agent_hub_common import (
    AgentProfile,
    EventKind,
    HubSettings,
    ImplementerOutcome,
    ImplementerResult,
    ModelSource,
    RebaseResult,
    ReviewerResult,
    ReviewerVerdict,
    TaskState,
)
from agent_hub_common.discovery import (
    Binding,
    DiscoveryError,
    checkout_git_dir,
    write_binding,
    write_hub_json,
)
from agent_hub_common.registry import register
from conftest import BASE_URL, TOKEN, closed_port_url
from timing import LIVENESS_S, FrozenClock, wait_until
from worker_mcp.client import WorkerHubClient, WorkerProtocolError
from worker_mcp.config import WorkerSettings

# #264 r1-1: allow timer/request/telemetry scheduling on a loaded host;
# each wait returns as soon as a heartbeat is observed.
HEARTBEAT_WAIT_TIMEOUT_S = 10.0


@pytest.fixture
def worker_settings() -> WorkerSettings:
    return WorkerSettings(
        hub_url=BASE_URL,
        token=TOKEN,
        agent_name="bob",
        profile=AgentProfile(harness="claude-code"),
        default_wait_s=0.2,
        max_retries=2,
        backoff_factor_s=0.01,
    )


async def test_worker_check_in_and_state(
    client: httpx.AsyncClient, worker_settings: WorkerSettings, hub_store: HubStore
) -> None:
    worker = WorkerHubClient(worker_settings, http_client=client)
    res = await worker.check_in(["python", "testing"])

    assert res["status"] == "registered"
    assert res["agent"] == "bob"
    assert worker.context_id is not None
    assert hub_store.agent_by_name("bob") is not None


def test_worker_instance_id_is_generated_once_per_client_startup(
    worker_settings: WorkerSettings,
) -> None:
    first = WorkerHubClient(worker_settings)
    second = WorkerHubClient(worker_settings)

    assert first.worker_instance_id
    assert second.worker_instance_id
    assert first.worker_instance_id != second.worker_instance_id


# #368: tests the client background timer advancing the hub store's heartbeat stamp
# across real time (asyncio.sleep).
@pytest.mark.real_clock
async def test_background_heartbeat_runs_without_llm_tool_calls(
    client: httpx.AsyncClient,
    worker_settings: WorkerSettings,
    hub_store: HubStore,
) -> None:
    configured = replace(worker_settings, heartbeat_s=0.01)
    async with WorkerHubClient(configured, http_client=client) as worker:
        await worker.check_in()
        task = hub_store.assign_task("bob", "implementer", "Long tests", "Run them")
        assignment = await worker.await_assignment(timeout_s=0.2)
        assert assignment["task_id"] == task.id
        before = hub_store.agent_by_name("bob")
        assert before is not None

        # #264 r1-1: wait for evidence, rather than a 40 ms scheduler window.
        async with asyncio.timeout(HEARTBEAT_WAIT_TIMEOUT_S):
            while True:
                after = hub_store.agent_by_name("bob")
                assert after is not None
                if after.last_heartbeat > before.last_heartbeat:
                    break
                await asyncio.sleep(0.01)

        after = hub_store.agent_by_name("bob")
        assert after is not None and after.last_heartbeat > before.last_heartbeat
        assert worker.current_task_id == task.id
        assert f"worker-heartbeat-{configured.agent_name}" in {
            running.get_name() for running in asyncio.all_tasks()
        }

    assert f"worker-heartbeat-{configured.agent_name}" not in {
        running.get_name() for running in asyncio.all_tasks()
    }


async def test_telemetry_records_the_hub_url_at_startup_and_on_each_heartbeat(
    client: httpx.AsyncClient,
    worker_settings: WorkerSettings,
    hub_store: HubStore,
    tmp_path: Path,
) -> None:
    """#126: the URL the worker dialed, to set beside the address the hub saw."""

    telemetry_path = tmp_path / "bob.jsonl"
    configured = replace(worker_settings, heartbeat_s=0.01, telemetry_log=telemetry_path)
    async with WorkerHubClient(configured, http_client=client) as worker:
        await worker.check_in()
        # The same scheduler race as the background-heartbeat test (#264).
        # Observe the emitted record, so the request and telemetry write finish.
        async with asyncio.timeout(HEARTBEAT_WAIT_TIMEOUT_S):
            while True:
                records = [
                    json.loads(line)
                    for line in telemetry_path.read_text(encoding="utf-8").splitlines()
                ]
                if any(record.get("event") == "heartbeat" for record in records):
                    break
                await asyncio.sleep(0.01)

    records = [json.loads(line) for line in telemetry_path.read_text(encoding="utf-8").splitlines()]
    [started] = [record for record in records if record["event"] == "session_started"]
    heartbeats = [record for record in records if record["event"] == "heartbeat"]
    assert started["hub_url"] == BASE_URL
    assert heartbeats
    assert all(record["hub_url"] == BASE_URL for record in heartbeats)
    # The in-process transport reports a loopback peer, as a WSL worker would.
    bob = hub_store.agent_by_name("bob")
    assert bob is not None
    assert (bob.checkin_remote_addr, bob.last_remote_addr) == ("127.0.0.1", "127.0.0.1")


async def test_a_restarted_worker_checks_in_at_once_and_finishes_its_task(
    client: httpx.AsyncClient,
    worker_settings: WorkerSettings,
    hub_store: HubStore,
    tmp_path: Path,
) -> None:
    """#115: a resumed headless harness starts a new worker-mcp mid-task."""

    telemetry_path = tmp_path / "bob.jsonl"
    configured = replace(worker_settings, telemetry_log=telemetry_path)
    async with WorkerHubClient(configured, http_client=client) as first:
        await first.check_in()
        task = hub_store.assign_task("bob", "implementer", "Task 1", "Do it")
        assert (await first.await_assignment(timeout_s=0.2))["task_id"] == task.id

    events = [
        json.loads(line)["event"]
        for line in telemetry_path.read_text(encoding="utf-8").splitlines()
    ]
    assert events[-2:] == ["detach", "session_stopped"]

    async with WorkerHubClient(configured, http_client=client) as second:
        assert (await second.check_in())["status"] == "registered"
        res = await second.submit_result(
            task.id,
            ImplementerResult(
                outcome=ImplementerOutcome.COMPLETED,
                summary="Finished after a restart",
                pr_url="https://github.com/org/repo/pull/1",
                head_sha="0123456789abcdef0123456789abcdef01234567",
            ),
        )
    assert res["status"] == "completed"
    stored = hub_store.get_task(task.id)
    assert stored is not None and stored.state is TaskState.COMPLETED


async def test_check_in_reports_the_launcher_profile_to_get_state(
    client: httpx.AsyncClient, worker_settings: WorkerSettings, hub_store: HubStore
) -> None:
    configured = AgentProfile(
        harness="codex",
        harness_version="0.154.0",
        provider="openai",
        model="example-codex-model",
        model_source=ModelSource.ENV,
        capabilities=("python",),
    )
    worker = WorkerHubClient(replace(worker_settings, profile=configured), http_client=client)

    # The launcher's model wins over the agent's own belief about itself, but
    # that belief is recorded beside it rather than discarded (#77).
    res = await worker.check_in(["gh", "python"], model="something-else")

    expected = {
        "harness": "codex",
        "harness_source": "env",
        "harness_version": "0.154.0",
        "provider": "openai",
        "model": "example-codex-model",
        "model_source": "env",
        "effort": "unknown",
        "effort_source": "unknown",
        "capabilities": ["python", "gh"],
        "workspace_id": None,
        "declared_model": "something-else",
    }
    [agent] = hub_store.get_state()["agents"]
    assert {key: agent[key] for key in expected} == expected
    assert agent["model_mismatch"] is True
    assert res["profile"] == expected
    event = hub_store.next_event()
    assert event is not None and event.kind is EventKind.AGENT_CHECKED_IN
    assert event.payload["model"] == "example-codex-model"
    assert event.payload["declared_model"] == "something-else"
    assert event.payload["model_mismatch"] is True


@pytest.mark.parametrize(
    ("declared", "model", "source", "recorded"),
    [
        ("claude-opus-5", "claude-opus-5", "declared", "claude-opus-5"),
        (" ", "unknown", "unknown", "unknown"),
        ("unknown", "unknown", "unknown", "unknown"),
        (None, "unknown", "unknown", "unknown"),
    ],
)
async def test_a_declared_model_is_used_only_when_the_launcher_names_none(
    client: httpx.AsyncClient,
    worker_settings: WorkerSettings,
    hub_store: HubStore,
    declared: str | None,
    model: str,
    source: str,
    recorded: str,
) -> None:
    worker = WorkerHubClient(worker_settings, http_client=client)

    await worker.check_in(model=declared)

    [agent] = hub_store.get_state()["agents"]
    assert (agent["harness"], agent["model"], agent["model_source"]) == (
        "claude-code",
        model,
        source,
    )
    assert agent["provider"] == agent["harness_version"] == "unknown"
    # Unset HUB_MODEL: the declared model agrees with `model`; a runtime that
    # reported nothing stays `unknown`, never guessed.
    assert agent["declared_model"] == recorded
    assert agent["model_mismatch"] is False


async def test_get_role_guide_no_cache(
    client: httpx.AsyncClient,
    worker_settings: WorkerSettings,
    settings: HubSettings,
) -> None:
    settings.guides_dir.mkdir(parents=True, exist_ok=True)
    guide_file = settings.guides_dir / "implementer.md"
    guide_file.write_text("# Implementer Guide\nDo the work.", encoding="utf-8")
    (settings.guides_dir / "forge").mkdir(parents=True, exist_ok=True)
    (settings.guides_dir / "forge" / "github.md").write_text("# Appendix\n", encoding="utf-8")

    worker = WorkerHubClient(worker_settings, http_client=client)

    # Invalid slug rejection
    with pytest.raises(ValueError, match="Role must be a slug"):
        await worker.get_role_guide("Implementer")
    with pytest.raises(ValueError, match="Role must be a slug"):
        await worker.get_role_guide("../traversal")

    # Unknown guide -> 404 / FileNotFoundError
    with pytest.raises(FileNotFoundError, match="not found"):
        await worker.get_role_guide("reviewer")

    # Fetch guide
    content = await worker.get_role_guide("implementer")
    assert "Implementer Guide" in content

    # Modify guide file and verify no local cache (fetches freshly each time)
    guide_file.write_text("# Updated Guide\nNew instructions.", encoding="utf-8")
    updated = await worker.get_role_guide("implementer")
    assert "Updated Guide" in updated


async def test_await_assignment_and_release(
    client: httpx.AsyncClient,
    worker_settings: WorkerSettings,
    hub_store: HubStore,
) -> None:
    worker = WorkerHubClient(worker_settings, http_client=client)

    # Calling before check-in raises error
    with pytest.raises(RuntimeError, match="call check_in first"):
        await worker.await_assignment()

    await worker.check_in()

    # Timeout when no assignment pending
    timeout_res = await worker.await_assignment(timeout_s=0.05)
    assert timeout_res == {"timeout": True}

    # Assignment wake-up
    async def assign() -> None:
        await wait_until(
            lambda: bool(
                (w := hub_store.activity.worker("bob", worker.worker_instance_id)) and w.holds
            ),
            "worker to hold on await_assignment",
        )
        hub_store.assign_task("bob", "implementer", "Fix issue #42", "Write code and tests")

    assignment_task = asyncio.create_task(worker.await_assignment(timeout_s=LIVENESS_S))
    await asyncio.gather(assign(), assignment_task)

    assignment = assignment_task.result()
    assert assignment["role"] == "implementer"
    assert "Write code and tests" in assignment["instructions"]
    assert assignment["task_id"] != ""

    # Release
    hub_store.release_agent("bob")
    release_res = await worker.await_assignment(timeout_s=LIVENESS_S)
    assert release_res == {"release": True}


async def test_report_progress_and_submit_result(
    client: httpx.AsyncClient,
    worker_settings: WorkerSettings,
    hub_store: HubStore,
) -> None:
    worker = WorkerHubClient(worker_settings, http_client=client)
    await worker.check_in()
    task = hub_store.assign_task("bob", "implementer", "Task 1", "Do it")

    # Progress
    prog_res = await worker.report_progress(task.id, "50% done")
    assert prog_res == {"ok": True, "note": "50% done"}
    event = hub_store.next_event()
    while event and event.payload.get("task_id") != task.id:
        event = hub_store.next_event()
    assert event is not None and event.payload.get("note") == "50% done"

    # Submit result
    with pytest.raises(ValueError, match="status must be 'completed' or 'failed'"):
        await worker.submit_result(task.id, "unknown", "bad status")

    res = await worker.submit_result(
        task.id,
        "completed",
        "Finished successfully",
        artifacts=[{"name": "pr", "url": "https://github.com/pr/1"}],
    )
    assert res["status"] == "completed"
    assert res["task_id"] == task.id
    stored_task = hub_store.get_task(task.id)
    assert stored_task is not None and stored_task.state == TaskState.COMPLETED


@pytest.mark.hub_settings(max_wait_s=LIVENESS_S)
async def test_ask_alice_reply_and_retry_correlation(
    client: httpx.AsyncClient,
    worker_settings: WorkerSettings,
    hub_store: HubStore,
) -> None:
    worker = WorkerHubClient(worker_settings, http_client=client)
    await worker.check_in()
    task = hub_store.assign_task("bob", "implementer", "Task 1", "Instructions")
    while hub_store.next_event() is not None:
        pass

    # 1. Ask question with answer
    async def answer() -> None:
        await wait_until(
            lambda: (
                (t := hub_store.get_task(task.id)) is not None
                and t.state == TaskState.INPUT_REQUIRED
            ),
            "task to reach INPUT_REQUIRED",
        )
        event = hub_store.next_event()
        assert event is not None and event.kind is EventKind.WORKER_QUESTION
        hub_store.reply(
            task.id,
            "Use SQLite for persistence.",
            message_id=event.payload["message_id"],
        )

    ask_task = asyncio.create_task(
        worker.ask_alice(task.id, "Which database?", timeout_s=LIVENESS_S)
    )
    await asyncio.gather(answer(), ask_task)
    reply = ask_task.result()
    assert reply == {"reply": "Use SQLite for persistence."}

    # 2. Ask question that times out
    timeout_res = await worker.ask_alice(task.id, "Second question?", timeout_s=0.05)
    assert timeout_res == {"timeout": True}
    assert task.id in worker._pending_questions
    saved_msg_id = worker._pending_questions[task.id][1]
    assert saved_msg_id != ""

    # Alice answers while worker is retrying in the gap
    event = hub_store.next_event()
    assert event is not None and event.kind is EventKind.WORKER_QUESTION
    hub_store.reply(
        task.id,
        "The answer given in the gap.",
        message_id=event.payload["message_id"],
    )

    # 3. Retry uses the saved message_id and picks up the answer
    retry_res = await worker.ask_alice(task.id, "Second question?", timeout_s=LIVENESS_S)
    assert retry_res == {"reply": "The answer given in the gap."}
    assert task.id not in worker._pending_questions

    # 4. Asking a different question after timeout generates a new message_id
    timeout_diff_1 = await worker.ask_alice(task.id, "Question A?", timeout_s=0.05)
    assert timeout_diff_1 == {"timeout": True}
    msg_id_a = worker._pending_questions[task.id][1]

    timeout_diff_2 = await worker.ask_alice(task.id, "Question B?", timeout_s=0.05)
    assert timeout_diff_2 == {"timeout": True}
    msg_id_b = worker._pending_questions[task.id][1]
    assert msg_id_b != msg_id_a


@pytest.mark.hub_settings(max_wait_s=LIVENESS_S)
async def test_ask_alice_manual_termination_override(
    client: httpx.AsyncClient,
    worker_settings: WorkerSettings,
    hub_store: HubStore,
) -> None:
    worker = WorkerHubClient(worker_settings, http_client=client)
    await worker.check_in()
    task = hub_store.assign_task("bob", "implementer", "Task 1", "Instructions")

    async def cancel_task() -> None:
        await wait_until(
            lambda: (
                (t := hub_store.get_task(task.id)) is not None
                and t.state == TaskState.INPUT_REQUIRED
            ),
            "task to reach INPUT_REQUIRED",
        )
        hub_store.set_task_state(task.id, TaskState.CANCELED, "Task aborted by user")

    ask_task = asyncio.create_task(
        worker.ask_alice(task.id, "How to proceed?", timeout_s=LIVENESS_S)
    )
    await asyncio.gather(cancel_task(), ask_task)

    res = ask_task.result()
    assert res.get("task_ended") is True
    assert res.get("state") == "canceled"
    assert "aborted" in str(res.get("note"))
    assert task.id not in worker._pending_questions


@pytest.mark.hub_settings(max_wait_s=LIVENESS_S)
async def test_ask_alice_cancellation_without_result_summary(
    client: httpx.AsyncClient,
    worker_settings: WorkerSettings,
    hub_store: HubStore,
) -> None:
    worker = WorkerHubClient(worker_settings, http_client=client)
    await worker.check_in()
    task = hub_store.assign_task("bob", "implementer", "Task 1", "Instructions")

    async def cancel_task() -> None:
        await wait_until(
            lambda: (
                (t := hub_store.get_task(task.id)) is not None
                and t.state == TaskState.INPUT_REQUIRED
            ),
            "task to reach INPUT_REQUIRED",
        )
        hub_store.cancel_task(task.id)

    ask_task = asyncio.create_task(
        worker.ask_alice(task.id, "How to proceed?", timeout_s=LIVENESS_S)
    )
    await asyncio.gather(cancel_task(), ask_task)

    res = ask_task.result()
    assert res.get("task_ended") is True
    assert res.get("state") == "canceled"
    assert res.get("note") == "canceled"
    assert task.id not in worker._pending_questions


async def test_stream_rpc_json_error_raises_protocol_error(
    client: httpx.AsyncClient,
    worker_settings: WorkerSettings,
    hub_store: HubStore,
) -> None:
    worker = WorkerHubClient(worker_settings, http_client=client)
    # 1. await_assignment with unknown contextId raises WorkerProtocolError
    worker.context_id = "invalid-context-id"
    with pytest.raises(WorkerProtocolError) as exc_info:
        await worker.await_assignment(timeout_s=0.5)
    assert "unknown context" in exc_info.value.message.lower()

    # 2. ask_alice on a task not assigned to bob raises WorkerProtocolError
    await worker.check_in()
    hub_store.check_in("charlie")
    charlie_task = hub_store.assign_task("charlie", "implementer", "Task Charlie", "Inst")
    with pytest.raises(WorkerProtocolError) as exc_info:
        await worker.ask_alice(charlie_task.id, "Question?", timeout_s=0.5)
    assert "not assigned to bob" in exc_info.value.message.lower()


@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize(
    ("error", "message"),
    [
        (httpx.ReadError, "ReadError (hub connection dropped)"),
        (httpx.ReadTimeout, "ReadTimeout (hub transport timeout)"),
    ],
)
async def test_empty_transport_error_names_type(
    worker_settings: WorkerSettings, stream: bool, error: type[httpx.TransportError], message: str
) -> None:
    def fail(request: httpx.Request) -> httpx.Response:
        raise error("", request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(fail), base_url=BASE_URL) as client:
        worker = WorkerHubClient(replace(worker_settings, max_retries=0), http_client=client)
        with pytest.raises(error) as exc:
            if stream:
                await worker._stream_rpc("test", {}, 1)
            else:
                await worker._request_with_retry("GET", "/")
        assert str(exc.value) == message


async def test_retry_on_503(worker_settings: WorkerSettings, tmp_path: Path) -> None:
    attempts = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        if attempts < 2:
            return httpx.Response(503, text="Service Unavailable")
        return httpx.Response(200, json={"status": "ok"})

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(
        transport=transport, base_url=worker_settings.hub_url
    ) as mock_client:
        telemetry_path = tmp_path / "retry.jsonl"
        configured = replace(worker_settings, telemetry_log=telemetry_path)
        worker = WorkerHubClient(configured, http_client=mock_client)
        resp = await worker._request_with_retry("GET", "/healthz")
        assert resp.status_code == 200
        assert attempts == 2
        records = [
            json.loads(line) for line in telemetry_path.read_text(encoding="utf-8").splitlines()
        ]
        retries = [record for record in records if record.get("event") == "retry"]
        assert len(retries) == 1
        assert retries[0]["attempt"] == 1
        assert retries[0]["reason"] == "http_503"


async def test_worker_submit_typed_implementer_result(
    client: httpx.AsyncClient,
    worker_settings: WorkerSettings,
    hub_store: HubStore,
) -> None:
    worker = WorkerHubClient(worker_settings, http_client=client)
    await worker.check_in()
    task = hub_store.assign_task("bob", "implementer", "Task 1", "Do it")

    res = await worker.submit_result(
        task.id,
        ImplementerResult(
            outcome=ImplementerOutcome.COMPLETED,
            summary="All tests pass",
            pr_url="https://github.com/org/repo/pull/1",
            head_sha="0123456789abcdef0123456789abcdef01234567",
        ),
    )
    assert res["status"] == "completed"
    assert res["task_id"] == task.id
    assert res["result"]["pr_url"] == "https://github.com/org/repo/pull/1"

    stored_task = hub_store.get_task(task.id)
    assert stored_task is not None and stored_task.state == TaskState.COMPLETED
    assert stored_task.result is not None
    assert stored_task.result.get("pr_url") == "https://github.com/org/repo/pull/1"


async def test_worker_submit_typed_reviewer_result(
    client: httpx.AsyncClient,
    worker_settings: WorkerSettings,
    hub_store: HubStore,
) -> None:
    worker = WorkerHubClient(worker_settings, http_client=client)
    await worker.check_in()
    task = hub_store.assign_task(
        "bob",
        "reviewer",
        "Review PR",
        "Review it",
        pr_head_sha="0123456789abcdef0123456789abcdef01234567",
    )

    res = await worker.submit_result(
        task.id,
        ReviewerResult(
            verdict=ReviewerVerdict.APPROVED,
            summary="Looks great!",
            reviewed_head_sha="0123456789abcdef0123456789abcdef01234567",
        ),
    )
    assert res["status"] == "completed"
    assert res["result"]["verdict"] == "approved"

    stored_task = hub_store.get_task(task.id)
    assert stored_task is not None and stored_task.state == TaskState.COMPLETED
    assert stored_task.result is not None
    assert stored_task.result.get("reviewed_head_sha") == (
        "0123456789abcdef0123456789abcdef01234567"
    )


async def test_worker_rebase_assignment_and_result(
    client: httpx.AsyncClient,
    worker_settings: WorkerSettings,
    hub_store: HubStore,
) -> None:
    head = "0123456789abcdef0123456789abcdef01234567"
    rebased = "abcdef0123456789abcdef0123456789abcdef01"
    worker = WorkerHubClient(worker_settings, http_client=client)
    await worker.check_in()
    hub_store.assign_task("bob", "rebase", "Rebase #7", "Bring it up to date", pr_head_sha=head)

    assignment = await worker.await_assignment(timeout_s=1.0)
    res = await worker.submit_result(
        assignment["task_id"],
        RebaseResult(
            outcome=ImplementerOutcome.COMPLETED,
            head_sha=rebased,
            conflict_files=["README.md"],
            resolution_summary="Kept both paragraphs.",
            summary="Rebased onto main",
        ),
    )

    assert assignment["role"] == "rebase"
    assert assignment["pr_head_sha"] == head
    assert res["status"] == "completed"
    stored = hub_store.get_task(assignment["task_id"])
    assert stored is not None and stored.result is not None
    assert stored.result["head_sha"] == rebased
    assert stored.result["conflict_files"] == ["README.md"]


async def test_worker_submit_result_validation_failure_leaves_task_working(
    client: httpx.AsyncClient,
    worker_settings: WorkerSettings,
    hub_store: HubStore,
) -> None:
    worker = WorkerHubClient(worker_settings, http_client=client)
    await worker.check_in()
    task = hub_store.assign_task("bob", "implementer", "Task 1", "Do it")
    assignment = await worker.await_assignment()
    assert assignment["task_id"] == task.id

    # Invalid result: outcome=completed but missing pr_url and head_sha
    invalid_payload = {
        "outcome": "completed",
        "summary": "Finished without PR",
    }

    with pytest.raises(WorkerProtocolError) as exc_info:
        await worker.submit_result(task.id, invalid_payload)

    assert "requires pr_url" in exc_info.value.message
    # Task remains in working state
    stored_task = hub_store.get_task(task.id)
    assert stored_task is not None and stored_task.state == TaskState.WORKING

    # Correct and retry: should succeed
    valid_payload = {
        "outcome": "completed",
        "summary": "Finished with PR",
        "pr_url": "https://github.com/org/repo/pull/42",
        "head_sha": "0123456789abcdef0123456789abcdef01234567",
    }
    res = await worker.submit_result(task.id, valid_payload)
    assert res["status"] == "completed"

    stored_task = hub_store.get_task(task.id)
    assert stored_task is not None and stored_task.state == TaskState.COMPLETED


async def test_worker_idempotent_submit_result_and_conflict(
    client: httpx.AsyncClient,
    worker_settings: WorkerSettings,
    hub_store: HubStore,
) -> None:
    worker = WorkerHubClient(worker_settings, http_client=client)
    await worker.check_in()
    task = hub_store.assign_task("bob", "implementer", "Task 1", "Do it")

    result = {
        "outcome": "completed",
        "summary": "First try",
        "pr_url": "https://github.com/org/repo/pull/10",
        "head_sha": "0123456789abcdef0123456789abcdef01234567",
    }

    # First submission
    res1 = await worker.submit_result(task.id, result)
    assert res1["status"] == "completed"

    # Replay identical submission: should reuse operation_id and return cleanly
    res2 = await worker.submit_result(task.id, result)
    assert res2["status"] == "completed"

    # Conflicting submission with same operation_id explicitly passed
    pending_op_id = worker._pending_results[task.id][1]
    with pytest.raises(WorkerProtocolError) as exc_info:
        await worker.submit_result(
            task.id,
            {"outcome": "failed", "summary": "Changed my mind"},
            operation_id=pending_op_id,
        )
    assert exc_info.value.code == -32600
    msg = exc_info.value.message.lower()
    assert "conflict" in msg or "already executed" in msg


async def test_worker_join_whoami_and_takeover_over_a2a(
    client: httpx.AsyncClient,
    hub_store: HubStore,
    settings: HubSettings,
) -> None:
    settings.guides_dir.mkdir(parents=True, exist_ok=True)
    (settings.guides_dir / "worker.md").write_text("# Worker Guide\n", encoding="utf-8")
    (settings.guides_dir / "implementer.md").write_text("# Implementer Guide\n", encoding="utf-8")
    (settings.guides_dir / "forge").mkdir(parents=True, exist_ok=True)
    (settings.guides_dir / "forge" / "github.md").write_text("# Appendix\n", encoding="utf-8")

    unjoined = WorkerSettings.unjoined(
        profile=AgentProfile(harness="claude-code", harness_source=ModelSource.INSTALL),
        environ={"ROBOMATE_HUB_URL": BASE_URL, "ROBOMATE_TOKEN": TOKEN},
        default_wait_s=0.2,
    )
    async with WorkerHubClient(unjoined, http_client=client) as first:
        before = await first.whoami()
        assert before["joined"] is False
        assert before["hub"] is not None
        assert before["hub"]["name"] == "hub"
        assert before["hub"]["url"] == settings.public_url

        join_res = await first.join(
            role="implementer",
            name="bob",
            model="claude-opus-4-7",
            effort="xhigh",
            capabilities=["python", "git"],
        )
        assert join_res["status"] == "registered"
        assert join_res["agent"] == "bob"
        assert join_res["role"] == "implementer"
        assert join_res["hub"]["name"] == "hub"
        assert "workspace_path" in join_res
        assert join_res["cwd_inside_workspace"] is True
        assert join_res["profile"]["harness"] == "claude-code"
        assert join_res["profile"]["harness_source"] == "install"
        assert join_res["profile"]["model"] == "claude-opus-4-7"
        assert join_res["profile"]["model_source"] == "declared"
        assert join_res["profile"]["effort"] == "xhigh"
        assert join_res["profile"]["effort_source"] == "declared"

        task = hub_store.assign_task("bob", "implementer", "Fix #238", "Implement join")
        after = await first.whoami()
        assert after["joined"] is True
        assert after["name"] == "hub"
        assert after["agent_name"] == "bob"
        assert after["role"] == "implementer"
        assert after["hub"] == settings.public_url
        assert after["status"] == "active"
        assert after["current_task"] == {
            "task_id": task.id,
            "role": "implementer",
            "title": "Fix #238",
            "state": "submitted",
        }
        assert after["last_seen"] is not None

        # Without takeover=True, a second live session under the same name is refused
        async with WorkerHubClient(unjoined, http_client=client) as duplicate:
            with pytest.raises(WorkerProtocolError, match=r"(?i)takeover=true"):
                await duplicate.join(role="implementer", name="bob")

        # With takeover=True, the new session supersedes the first and reattaches the task
        async with WorkerHubClient(unjoined, http_client=client) as replacement:
            takeover_res = await replacement.join(
                role="implementer",
                name="bob",
                takeover=True,
            )
            assert takeover_res["reattached_task"] is not None
            assert takeover_res["reattached_task"]["task_id"] == task.id
            assert takeover_res["reattached_task"]["role"] == "implementer"
            assert takeover_res["reattached_task"]["title"] == "Fix #238"
            assert takeover_res["reattached_task"]["state"] == "working"
            assert replacement.current_task_id == task.id

            # Old session's await_assignment now fails with superseded instance error
            with pytest.raises(WorkerProtocolError, match="superseded"):
                await first.await_assignment(timeout_s=0.1)

            # Replacement can finish the task directly
            done = await replacement.submit_result(
                task.id,
                ImplementerResult(
                    outcome=ImplementerOutcome.COMPLETED,
                    summary="Finished after takeover",
                    pr_url="https://github.com/org/repo/pull/238",
                    head_sha="0123456789abcdef0123456789abcdef01234567",
                ),
            )
            assert done["status"] == "completed"


async def test_join_declared_model_no_mismatch(
    client: httpx.AsyncClient,
    hub_store: HubStore,
    settings: HubSettings,
) -> None:
    """#87: join with declared exact ID and no HUB_MODEL gives declared source and no mismatch."""
    settings.guides_dir.mkdir(parents=True, exist_ok=True)
    (settings.guides_dir / "worker.md").write_text("# Worker Guide\n", encoding="utf-8")
    (settings.guides_dir / "implementer.md").write_text("# Implementer Guide\n", encoding="utf-8")
    (settings.guides_dir / "forge").mkdir(parents=True, exist_ok=True)
    (settings.guides_dir / "forge" / "github.md").write_text("# Appendix\n", encoding="utf-8")

    unjoined = WorkerSettings.unjoined(
        profile=AgentProfile(harness="claude-code", harness_source=ModelSource.INSTALL),
        environ={"ROBOMATE_HUB_URL": BASE_URL, "ROBOMATE_TOKEN": TOKEN},
        default_wait_s=0.2,
    )
    async with WorkerHubClient(unjoined, http_client=client) as worker:
        res = await worker.join(
            role="implementer",
            name="bob",
            model="claude-opus-5-5",
        )
        assert res["profile"]["model"] == "claude-opus-5-5"
        assert res["profile"]["model_source"] == "declared"
        agent = hub_store.agent_by_name("bob")
        assert agent is not None
        assert agent.model == "claude-opus-5-5"
        assert agent.model_source == ModelSource.DECLARED
        assert agent.declared_model == "claude-opus-5-5"
        state = hub_store.get_state()
        agent_summary = next(a for a in state["agents"] if a["name"] == "bob")
        assert agent_summary["model_mismatch"] is False


async def test_await_assignment_redelivers_held_task_with_flag(
    client: httpx.AsyncClient,
    worker_settings: WorkerSettings,
    hub_store: HubStore,
) -> None:
    """#326: second await_assignment returns the held task, flagged."""

    worker = WorkerHubClient(worker_settings, http_client=client)
    await worker.check_in()
    task = hub_store.assign_task("bob", "implementer", "Task 1", "Do it")

    first = await worker.await_assignment(timeout_s=1.0)
    assert first["task_id"] == task.id
    assert "already_held" not in first

    second = await worker.await_assignment(timeout_s=0.2)
    assert second["task_id"] == task.id
    assert second["already_held"] is True


async def test_finished_task_errors_name_held_task(
    client: httpx.AsyncClient,
    worker_settings: WorkerSettings,
    hub_store: HubStore,
) -> None:
    """#326: progress/result on a finished task name the held open task."""

    worker = WorkerHubClient(worker_settings, http_client=client)
    await worker.check_in()
    first = hub_store.assign_task("bob", "implementer", "First", "Do first")
    first_claim = await worker.await_assignment(timeout_s=1.0)
    assert first_claim["task_id"] == first.id
    await worker.submit_result(
        first.id,
        {
            "outcome": "completed",
            "summary": "done",
            "pr_url": "https://github.com/org/repo/pull/1",
            "head_sha": "0123456789abcdef0123456789abcdef01234567",
        },
    )
    second = hub_store.assign_task("bob", "implementer", "Second", "Do second")
    held = await worker.await_assignment(timeout_s=1.0)
    assert held["task_id"] == second.id

    with pytest.raises(WorkerProtocolError) as prog_info:
        await worker.report_progress(first.id, "late note")
    assert second.id in prog_info.value.message
    assert "use that task_id" in prog_info.value.message

    with pytest.raises(WorkerProtocolError) as res_info:
        await worker.submit_result(first.id, {"outcome": "failed", "summary": "again"})
    assert second.id in res_info.value.message

    with pytest.raises(WorkerProtocolError) as ask_info:
        await worker.ask_alice(first.id, "late question?", timeout_s=0.2)
    assert second.id in ask_info.value.message


async def test_finished_task_errors_without_held_point_to_await(
    client: httpx.AsyncClient,
    worker_settings: WorkerSettings,
    hub_store: HubStore,
) -> None:
    """#326: with no held task, finished-task errors direct to await_assignment."""

    worker = WorkerHubClient(worker_settings, http_client=client)
    await worker.check_in()
    task = hub_store.assign_task("bob", "implementer", "Task 1", "Do it")
    claimed = await worker.await_assignment(timeout_s=1.0)
    assert claimed["task_id"] == task.id
    await worker.submit_result(
        task.id,
        {
            "outcome": "completed",
            "summary": "done",
            "pr_url": "https://github.com/org/repo/pull/1",
            "head_sha": "0123456789abcdef0123456789abcdef01234567",
        },
    )
    with pytest.raises(WorkerProtocolError) as exc_info:
        await worker.report_progress(task.id, "late note")
    assert "hold no open task" in exc_info.value.message
    assert "await_assignment" in exc_info.value.message


async def test_worker_await_assignment_surfaces_branch(
    client: httpx.AsyncClient,
    worker_settings: WorkerSettings,
    hub_store: HubStore,
) -> None:
    """M2 Step 7c r1-1: an assignment carrying hub.branch returns branch."""

    from agent_hub.database import database

    hub_store.hub_name = "testhub"
    state = hub_store.get_state()
    workflow_id = state["workflow"]["id"]
    with database(hub_store.path) as connection:
        connection.execute(
            "UPDATE workflow SET goal = ? WHERE id = ?",
            ("Address issue `o/r#99`, merge it.", workflow_id),
        )
    worker = WorkerHubClient(worker_settings, http_client=client)
    await worker.check_in()
    task = hub_store.assign_task("bob", "implementer", "Build", "Do it")
    assert task.branch == "robomate/testhub/bob/99"
    assignment = await worker.await_assignment(timeout_s=1.0)
    assert assignment["task_id"] == task.id
    assert assignment["branch"] == "robomate/testhub/bob/99"


async def test_repeat_reminder_is_compact_and_bridge_restart_recovers_full_text(
    client: httpx.AsyncClient, worker_settings: WorkerSettings, hub_store: HubStore
) -> None:
    worker = WorkerHubClient(worker_settings, http_client=client)
    await worker.check_in()
    task = hub_store.assign_task("bob", "implementer", "Recovery", "Full task text")
    fresh = await worker.await_assignment(timeout_s=0)
    recovery = await worker.await_assignment(timeout_s=0)
    assert recovery == {**fresh, "already_held": True}
    reminder = await worker.await_assignment(timeout_s=0)
    assert reminder == {"task_id": task.id, "title": task.title, "already_held": True}
    assert await worker.await_assignment(timeout_s=0, include_instructions=True) == recovery
    assert await worker.await_assignment(timeout_s=0) == reminder
    restarted = WorkerHubClient(worker_settings, http_client=client)
    restarted.context_id = worker.context_id
    restarted.worker_instance_id = worker.worker_instance_id
    assert await restarted.await_assignment(timeout_s=0) == recovery


# -- #511: a launcher-pinned bridge fails closed and honours ROBOMATE_HUB_ID -----


def _register_live_hub(
    tmp_path: Path,
    hub_id: str,
    name: str,
    env: dict[str, str],
    healthz: Callable[[str], str],
) -> str:
    """Register a hub whose /healthz answers its id and whose token is the hub's."""

    url = healthz(hub_id)
    state = tmp_path / name / "state"
    state.mkdir(parents=True)
    write_hub_json(state, {"hub_id": hub_id, "name": name, "url": url, "pid": None})
    token = tmp_path / name / "token"
    token.write_text(TOKEN, encoding="utf-8")
    checkout = tmp_path / name / "checkout"
    checkout.mkdir()
    register(
        {
            "hub_id": hub_id,
            "name": name,
            "checkout": str(checkout),
            "git_common_dir": str(checkout / ".git"),
            "state_dir": str(state),
            "token_file": str(token),
            "url": url,
            "pid": None,
        },
        env,
    )
    return url


def _register_down_hub(tmp_path: Path, hub_id: str, name: str, env: dict[str, str]) -> None:
    """Register a hub whose /healthz URL refuses connections (hub is down)."""

    url = closed_port_url()
    state = tmp_path / name / "state"
    state.mkdir(parents=True)
    write_hub_json(state, {"hub_id": hub_id, "name": name, "url": url, "pid": None})
    token = tmp_path / name / "token"
    token.write_text(TOKEN, encoding="utf-8")
    checkout = tmp_path / name / "checkout"
    checkout.mkdir()
    register(
        {
            "hub_id": hub_id,
            "name": name,
            "checkout": str(checkout),
            "git_common_dir": str(checkout / ".git"),
            "state_dir": str(state),
            "token_file": str(token),
            "url": url,
            "pid": None,
        },
        env,
    )


def _register_bound_down_hub(tmp_path: Path, hub_id: str, name: str) -> Path:
    """A checkout bound to a hub whose /healthz URL refuses connections."""

    checkout = tmp_path / name / "checkout"
    checkout.mkdir(parents=True)
    subprocess.run(["git", "init", "-q", str(checkout)], check=True, capture_output=True)
    state = tmp_path / name / "state"
    state.mkdir(parents=True)
    token = tmp_path / name / "token"
    token.write_text(TOKEN, encoding="utf-8")
    write_hub_json(state, {"hub_id": hub_id, "name": name, "url": closed_port_url(), "pid": None})
    write_binding(
        checkout_git_dir(checkout),
        Binding("owner", hub_id, name, checkout, state, token),
    )
    return checkout


def _join_guides(settings: HubSettings) -> None:
    settings.guides_dir.mkdir(parents=True, exist_ok=True)
    (settings.guides_dir / "worker.md").write_text("# Worker Guide\n", encoding="utf-8")
    (settings.guides_dir / "implementer.md").write_text("# Implementer Guide\n", encoding="utf-8")
    (settings.guides_dir / "forge").mkdir(parents=True, exist_ok=True)
    (settings.guides_dir / "forge" / "github.md").write_text("# Appendix\n", encoding="utf-8")


async def test_unbound_bridge_fails_closed_without_listing_hubs(
    tmp_path: Path, healthz: Callable[[str], str]
) -> None:
    """join and whoami fail closed and leak no hub URL, id or hub= hint (#511)."""

    env = {"XDG_STATE_HOME": str(tmp_path / "xdg"), "LOCALAPPDATA": str(tmp_path / "local")}
    _register_live_hub(tmp_path, "hub-a", "alpha", env, healthz)
    _register_live_hub(tmp_path, "hub-b", "beta", env, healthz)
    unbound = tmp_path / "unbound"
    unbound.mkdir()
    worker = WorkerHubClient(WorkerSettings.unjoined(cwd=unbound, environ=env))

    with pytest.raises(DiscoveryError) as joined:
        await worker.join("bob", "implementer")
    message = str(joined.value)
    assert "this robomate bridge has no hub" in message
    assert "report blocked" in message
    assert "hub=" not in message
    assert "http://" not in message
    assert "alpha" not in message and "beta" not in message
    assert "hub-a" not in message and "hub-b" not in message

    result = await worker.whoami()
    assert result["joined"] is False
    error = result["discovery_error"]
    assert "this robomate bridge has no hub" in error
    assert "report blocked" in error
    assert "http://" not in error
    assert "hub-a" not in error and "hub-b" not in error


async def test_hub_id_pin_reaches_the_hub_from_an_unrelated_checkout(
    tmp_path: Path,
    healthz: Callable[[str], str],
    client: httpx.AsyncClient,
    hub_store: HubStore,
    settings: HubSettings,
) -> None:
    """ROBOMATE_HUB_ID reaches the pinned hub even with no worktree binding (#511)."""

    _join_guides(settings)
    env = {"XDG_STATE_HOME": str(tmp_path / "xdg"), "LOCALAPPDATA": str(tmp_path / "local")}
    _register_live_hub(tmp_path, "hub-a", "alpha", env, healthz)
    url_b = _register_live_hub(tmp_path, "hub-b", "beta", env, healthz)
    unrelated = tmp_path / "unrelated"
    unrelated.mkdir()
    pinned = env | {"ROBOMATE_HUB_ID": "hub-b"}
    worker = WorkerHubClient(
        WorkerSettings.unjoined(cwd=unrelated, environ=pinned), http_client=client
    )
    result = await worker.join("bob", "implementer")
    assert result["status"] == "registered"
    assert worker.settings.hub_url == url_b
    assert worker.settings.token == TOKEN


async def test_unbound_bridge_refuses_agent_chosen_hub_with_the_fail_closed_text(
    tmp_path: Path, healthz: Callable[[str], str]
) -> None:
    """join(hub=...) cannot pick a registered hub for an unbound bridge (#526)."""

    env = {"XDG_STATE_HOME": str(tmp_path / "xdg"), "LOCALAPPDATA": str(tmp_path / "local")}
    _register_live_hub(tmp_path, "hub-a", "alpha", env, healthz)
    _register_live_hub(tmp_path, "hub-b", "beta", env, healthz)
    unbound = tmp_path / "unbound"
    unbound.mkdir()
    checkout_b = tmp_path / "beta" / "checkout"

    for selector in ("alpha", "hub-b", str(checkout_b)):
        worker = WorkerHubClient(WorkerSettings.unjoined(cwd=unbound, environ=env))
        with pytest.raises(DiscoveryError) as refused:
            await worker.join("bob", "implementer", hub=selector)
        message = str(refused.value)
        assert "this robomate bridge has no hub" in message
        assert "report blocked" in message
        assert "hub=" not in message
        assert "http://" not in message
        assert "alpha" not in message and "beta" not in message
        assert "hub-a" not in message and "hub-b" not in message
        assert str(checkout_b) not in message


@pytest.mark.parametrize("selector", ["beta", "hub-b"])
async def test_operator_opt_in_lets_an_unbound_bridge_join_the_named_hub(
    selector: str,
    tmp_path: Path,
    healthz: Callable[[str], str],
    client: httpx.AsyncClient,
    hub_store: HubStore,
    settings: HubSettings,
) -> None:
    """`robomate mcp --allow-hub-selection` keeps the operator's route (#526)."""

    _join_guides(settings)
    env = {"XDG_STATE_HOME": str(tmp_path / "xdg"), "LOCALAPPDATA": str(tmp_path / "local")}
    _register_live_hub(tmp_path, "hub-a", "alpha", env, healthz)
    url_b = _register_live_hub(tmp_path, "hub-b", "beta", env, healthz)
    unbound = tmp_path / "unbound"
    unbound.mkdir()
    worker = WorkerHubClient(
        WorkerSettings.unjoined(cwd=unbound, environ=env, allow_hub_selection=True),
        http_client=client,
    )
    result = await worker.join("bob", "implementer", hub=selector)
    assert result["status"] == "registered"
    assert worker.settings.hub_url == url_b


async def test_join_hub_override_is_refused_for_a_different_pinned_hub(
    tmp_path: Path, healthz: Callable[[str], str]
) -> None:
    """An agent's join(hub=...) cannot redirect a launcher-pinned bridge (#511)."""

    env = {"XDG_STATE_HOME": str(tmp_path / "xdg"), "LOCALAPPDATA": str(tmp_path / "local")}
    _register_live_hub(tmp_path, "hub-a", "alpha", env, healthz)
    _register_live_hub(tmp_path, "hub-b", "beta", env, healthz)
    pinned = env | {"ROBOMATE_HUB_ID": "hub-b"}
    worker = WorkerHubClient(WorkerSettings.unjoined(environ=pinned, allow_hub_selection=True))
    with pytest.raises(DiscoveryError) as refused:
        await worker.join("bob", "implementer", hub="hub-a")
    assert "names a different hub" in str(refused.value)
    assert "report blocked" in str(refused.value)


async def test_join_hub_override_matching_the_pin_is_accepted(
    tmp_path: Path,
    healthz: Callable[[str], str],
    client: httpx.AsyncClient,
    hub_store: HubStore,
    settings: HubSettings,
) -> None:
    _join_guides(settings)
    env = {"XDG_STATE_HOME": str(tmp_path / "xdg"), "LOCALAPPDATA": str(tmp_path / "local")}
    _register_live_hub(tmp_path, "hub-a", "alpha", env, healthz)
    url_b = _register_live_hub(tmp_path, "hub-b", "beta", env, healthz)
    pinned = env | {"ROBOMATE_HUB_ID": "hub-b"}
    worker = WorkerHubClient(WorkerSettings.unjoined(environ=pinned), http_client=client)
    result = await worker.join("bob", "implementer", hub="hub-b")
    assert result["status"] == "registered"
    assert worker.settings.hub_url == url_b


# -- review r1-1: only the genuinely unbound case fails closed ------------------


def _assert_cause_surfaces(message: str, diagnostic: str) -> None:
    """A pinned/bound failure keeps its real cause and the stable phrase is absent."""

    assert diagnostic in message
    # #512's launcher matches this phrase to stop resuming; a restarting hub
    # must not be reported as an unbound bridge (review r1-1, #511).
    assert "this robomate bridge has no hub" not in message
    assert "report blocked" not in message


async def test_pinned_hub_down_surfaces_the_cause(tmp_path: Path) -> None:
    """A launcher-pinned hub that is down is diagnosed, not 'no hub' (r1-1)."""

    env = {"XDG_STATE_HOME": str(tmp_path / "xdg"), "LOCALAPPDATA": str(tmp_path / "local")}
    _register_down_hub(tmp_path, "pin-id", "pinned", env)
    unbound = tmp_path / "unbound"
    unbound.mkdir()
    pinned = env | {"ROBOMATE_HUB_ID": "pin-id"}
    worker = WorkerHubClient(WorkerSettings.unjoined(cwd=unbound, environ=pinned))
    with pytest.raises(DiscoveryError) as joined:
        await worker.join("bob", "implementer")
    _assert_cause_surfaces(str(joined.value), "is not running")
    result = await worker.whoami()
    _assert_cause_surfaces(result["discovery_error"], "is not running")


async def test_bound_checkout_hub_down_surfaces_the_cause(tmp_path: Path) -> None:
    """A bound checkout whose hub is down keeps verify_local_hub's diagnostic (r1-1)."""

    checkout = _register_bound_down_hub(tmp_path, "bound-id", "bound")
    worker = WorkerHubClient(WorkerSettings.unjoined(cwd=checkout, environ={}))
    with pytest.raises(DiscoveryError) as joined:
        await worker.join("bob", "implementer")
    _assert_cause_surfaces(str(joined.value), "is not running")
    result = await worker.whoami()
    _assert_cause_surfaces(result["discovery_error"], "is not running")


async def test_unknown_pin_surfaces_the_cause(tmp_path: Path) -> None:
    """An unknown ROBOMATE_HUB_ID reports 'no registered hub', not 'no hub' (r1-1)."""

    env = {"XDG_STATE_HOME": str(tmp_path / "xdg"), "LOCALAPPDATA": str(tmp_path / "local")}
    unbound = tmp_path / "unbound"
    unbound.mkdir()
    pinned = env | {"ROBOMATE_HUB_ID": "missing-id"}
    worker = WorkerHubClient(WorkerSettings.unjoined(cwd=unbound, environ=pinned))
    with pytest.raises(DiscoveryError) as joined:
        await worker.join("bob", "implementer")
    _assert_cause_surfaces(str(joined.value), "no registered hub has that name or hub_id")
    result = await worker.whoami()
    _assert_cause_surfaces(result["discovery_error"], "no registered hub has that name or hub_id")


async def test_restart_during_long_stream_errors_without_replaying_hold_and_bridge_recovers(
    worker_settings: WorkerSettings,
) -> None:
    class Interrupted(httpx.AsyncByteStream):
        async def __aiter__(self) -> AsyncIterator[bytes]:
            yield b": held\n\n"
            raise httpx.ReadError("hub restarted")

    calls = []

    def respond(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        if len(calls) == 1:
            return httpx.Response(
                200, headers={"content-type": "text/event-stream"}, stream=Interrupted()
            )
        return httpx.Response(200, json={"result": {"recovered": True}})

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(respond), base_url=BASE_URL
    ) as client:
        worker = WorkerHubClient(worker_settings, http_client=client)
        with pytest.raises(httpx.ReadError, match="hub restarted"):
            await worker._stream_rpc("message/stream", {}, 1800)
        assert len(calls) == 1
        assert calls[0].extensions["timeout"]["read"] == 1815
        assert await worker._stream_rpc("message/stream", {}, 1800) == {"recovered": True}


async def test_real_http_hub_restart_interrupts_long_holds_and_same_bridges_recover(
    settings: HubSettings, worker_settings: WorkerSettings, frozen_clock: FrozenClock
) -> None:
    import uvicorn
    from agent_hub import create_app
    from agent_hub.signals import EVENT_KEY, context_key
    from conftest import reserve_port_block
    from worker_mcp.orchestrator import OrchestratorBridge

    port = reserve_port_block(1)
    url = f"http://127.0.0.1:{port}"
    config = replace(settings, host="127.0.0.1", port=port, public_url=url, max_wait_s=1800)
    servers = []
    running = []

    async def start() -> HubStore:
        app = create_app(config)
        frozen_clock.install(app.state.store)
        server = uvicorn.Server(
            uvicorn.Config(
                app, host=config.host, port=port, log_level="critical", timeout_graceful_shutdown=0
            )
        )
        servers.append(server)
        running.append(asyncio.create_task(server.serve()))
        await wait_until(lambda: server.started, "isolated HTTP hub starts")
        return cast(HubStore, app.state.store)

    try:
        store = await start()
        async with (
            WorkerHubClient(replace(worker_settings, hub_url=url)) as worker,
            httpx.AsyncClient(
                base_url=url, headers={"Authorization": f"Bearer {settings.token}"}
            ) as http,
        ):
            bridge = OrchestratorBridge(http_client=http)
            try:
                await worker.check_in()
                await bridge.call("get_state", {})
                while (event := store.lease_next_event()) is not None:
                    store.ack_event(event.delivery_id)
                worker_hold = asyncio.create_task(worker.await_assignment(timeout_s=1800))
                alice_hold = asyncio.create_task(bridge.call("wait_for_event", {"timeout_s": 1800}))
                await wait_until(
                    lambda: store.signals.waiting(context_key(str(worker.context_id))),
                    "worker HTTP hold",
                )
                await wait_until(lambda: store.signals.waiting(EVENT_KEY), "Alice HTTP hold")
                servers[0].should_exit = True
                await asyncio.wait_for(running[0], LIVENESS_S)
                for hold in (worker_hold, alice_hold):
                    await wait_until(hold.done, "restart returns a prompt transport error")
                    with pytest.raises(httpx.HTTPError):
                        await hold
                await start()
                assert await worker.await_assignment(timeout_s=0) == {"timeout": True}
                assert "agents" in await bridge.call("get_state", {})
            finally:
                await bridge.close()
    finally:
        for server, task in zip(servers, running, strict=True):
            server.should_exit = True
            await asyncio.wait_for(task, LIVENESS_S)


@pytest.mark.hub_settings(max_wait_s=1800)
@pytest.mark.parametrize("reply_wait", [False, True])
async def test_superseded_held_stream_returns_protocol_error(
    client: httpx.AsyncClient,
    worker_settings: WorkerSettings,
    hub_store: HubStore,
    reply_wait: bool,
) -> None:
    from agent_hub.signals import context_key, task_key
    from worker_mcp.client import WorkerProtocolError

    worker = WorkerHubClient(worker_settings, http_client=client)
    await worker.check_in()
    if reply_wait:
        task = hub_store.assign_task("bob", "implementer", "Build", "Build")
        await worker.await_assignment(timeout_s=0)
        pending = asyncio.create_task(worker.ask_alice(task.id, "Which base?", timeout_s=1800))
        key = task_key(task.id)
    else:
        pending = asyncio.create_task(worker.await_assignment(timeout_s=1800))
        key = context_key(str(worker.context_id))
    await wait_until(lambda: hub_store.signals.waiting(key), "old instance stream")
    hub_store.check_in("bob", worker_instance_id="replacement", takeover=True)
    try:
        await wait_until(pending.done, "superseded stream rejected")
        with pytest.raises(WorkerProtocolError, match="superseded"):
            await pending
    finally:
        pending.cancel()


async def test_replaced_instance_cannot_claim_work_when_stream_body_starts_late(
    hub_store: HubStore,
    settings: HubSettings,
) -> None:
    from agent_hub.protocol import A2AProtocol
    from agent_hub.store import TaskRecord

    old = hub_store.check_in("bob", worker_instance_id="old")
    protocol = A2AProtocol(store=hub_store, settings=settings)
    # StreamingResponse starts the body after sending its headers; takeover can
    # land between request authentication and that first generator iteration.
    stream = protocol._assignment_stream("request", old, 1800)
    replacement = hub_store.check_in("bob", worker_instance_id="new", takeover=True)
    task = hub_store.assign_task("bob", "implementer", "Build", "Build")
    chunks = [chunk async for chunk in stream]
    body = json.loads(chunks[0].decode().removeprefix("data: ").strip())
    assert "superseded" in body["error"]["message"]
    unchanged = hub_store.get_task(task.id)
    assert unchanged is not None and unchanged.state is TaskState.SUBMITTED
    claimed = await hub_store.await_assignment(replacement.context_id, 0)
    assert isinstance(claimed, TaskRecord) and claimed.id == task.id


async def test_assignment_metadata_contract(
    client: httpx.AsyncClient,
    worker_settings: WorkerSettings,
    hub_store: HubStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Classify hub metadata and carry every worker field through the real path."""
    from agent_hub import protocol
    from agent_hub.database import database
    from agent_hub_common.constants import (
        INTERNAL_ASSIGNMENT_KEYS,
        WORKER_ASSIGNMENT_FIELDS,
        MetaKeys,
    )

    classified = set(WORKER_ASSIGNMENT_FIELDS) | INTERNAL_ASSIGNMENT_KEYS
    assert not set(WORKER_ASSIGNMENT_FIELDS) & INTERNAL_ASSIGNMENT_KEYS

    def check_classified(metadata: dict[str, Any]) -> None:
        assert {key for key in metadata if key.startswith("hub.")} <= classified

    rendered: list[Any] = []
    original = protocol._task_object

    def capture(*args: Any, **kwargs: Any) -> Any:
        task_object = original(*args, **kwargs)
        rendered.append(task_object)
        return task_object

    monkeypatch.setattr(protocol, "_task_object", capture)
    worker = WorkerHubClient(worker_settings, http_client=client)
    await worker.check_in()
    workflow_id = hub_store.get_state()["workflow"]["id"]
    head = "a" * 40
    branch = "robomate/testhub/bob/99"
    context = {"obligations": ["example"]}
    with database(hub_store.path) as connection:
        statement, _ = hub_store._store_statement(connection, "Work", "test", "2026-01-01")
        connection.execute(
            "UPDATE workflow SET statement_sha256 = ?, branch = ? WHERE id = ?",
            (statement, branch, workflow_id),
        )
    closeout = hub_store.assign_task("bob", "closeout", "Close out", "Finish")
    with database(hub_store.path) as connection:
        row = connection.execute(
            "SELECT parts_json FROM message WHERE task_id = ? AND direction = 'from_alice'",
            (closeout.id,),
        ).fetchone()
        closeout_metadata = json.loads(row["parts_json"])[0]["metadata"]
        check_classified(closeout_metadata)
        assert closeout_metadata[MetaKeys.CLOSEOUT_CONTEXT] == closeout.closeout_context
    closeout_assignment = await worker.await_assignment(timeout_s=LIVENESS_S)
    assert closeout_assignment["closeout_context"] == closeout.closeout_context
    hub_store.cancel_task(closeout.id)

    task = hub_store.assign_task("bob", "reviewer", "Review", "Read it", pr_head_sha=head)
    with database(hub_store.path) as connection:
        row = connection.execute(
            "SELECT parts_json FROM message WHERE task_id = ? AND direction = 'from_alice'",
            (task.id,),
        ).fetchone()
        stored_metadata = json.loads(row["parts_json"])[0]["metadata"]
        check_classified(stored_metadata)
        connection.execute(
            "UPDATE task SET closeout_context_json = ? WHERE id = ?",
            (json.dumps(context), task.id),
        )

    await worker.await_assignment(timeout_s=LIVENESS_S)
    assignment = await worker.await_assignment(timeout_s=LIVENESS_S)
    obj = rendered[-1]
    metadata = obj.metadata
    assert metadata is not None
    check_classified(metadata)
    assert obj.status.message is not None
    check_classified(obj.status.message.metadata or {})
    expected = {
        MetaKeys.ROLE: "reviewer",
        MetaKeys.TITLE: "Review",
        MetaKeys.PR_HEAD_SHA: head,
        MetaKeys.RUN_ID: task.run_id,
        MetaKeys.STATEMENT_SHA256: statement,
        MetaKeys.BRANCH: branch,
        MetaKeys.ALREADY_HELD: True,
        MetaKeys.CLOSEOUT_CONTEXT: context,
    }
    assert set(expected) == set(WORKER_ASSIGNMENT_FIELDS)
    for key, value in expected.items():
        assert metadata[key] == value
        assert assignment[WORKER_ASSIGNMENT_FIELDS[key]] == value
    assert assignment["task_id"] == task.id
    assert assignment["instructions"] == "Read it"
