import asyncio
import json
from dataclasses import replace
from pathlib import Path

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
from conftest import BASE_URL, TOKEN
from worker_mcp.client import WorkerHubClient, WorkerProtocolError
from worker_mcp.config import WorkerSettings


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

        await asyncio.sleep(0.04)

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
        await asyncio.sleep(0.05)

    records = [
        json.loads(line) for line in telemetry_path.read_text(encoding="utf-8").splitlines()
    ]
    [started] = [record for record in records if record["event"] == "session_started"]
    heartbeats = [record for record in records if record["event"] == "heartbeat"]
    assert started["hub_url"] == BASE_URL
    assert heartbeats
    assert all(record["hub_url"] == BASE_URL for record in heartbeats)
    # The in-process transport reports a loopback peer, as a WSL worker would.
    bob = hub_store.agent_by_name("bob")
    assert bob is not None
    assert (bob.checkin_remote_addr, bob.last_remote_addr) == ("127.0.0.1", "127.0.0.1")


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
        "harness_version": "0.154.0",
        "provider": "openai",
        "model": "example-codex-model",
        "model_source": "env",
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
        await asyncio.sleep(0.02)
        hub_store.assign_task("bob", "implementer", "Fix issue #42", "Write code and tests")

    assignment_task = asyncio.create_task(worker.await_assignment(timeout_s=2.0))
    await asyncio.gather(assign(), assignment_task)

    assignment = assignment_task.result()
    assert assignment["role"] == "implementer"
    assert "Write code and tests" in assignment["instructions"]
    assert assignment["task_id"] != ""

    # Release
    hub_store.release_agent("bob")
    release_res = await worker.await_assignment(timeout_s=2.0)
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
        for _ in range(100):
            current = hub_store.get_task(task.id)
            if current is not None and current.state == TaskState.INPUT_REQUIRED:
                break
            await asyncio.sleep(0.01)
        event = hub_store.next_event()
        assert event is not None and event.kind is EventKind.WORKER_QUESTION
        hub_store.reply(
            task.id,
            "Use SQLite for persistence.",
            message_id=event.payload["message_id"],
        )

    ask_task = asyncio.create_task(worker.ask_alice(task.id, "Which database?", timeout_s=2.0))
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
    retry_res = await worker.ask_alice(task.id, "Second question?", timeout_s=2.0)
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


async def test_ask_alice_manual_termination_override(
    client: httpx.AsyncClient,
    worker_settings: WorkerSettings,
    hub_store: HubStore,
) -> None:
    worker = WorkerHubClient(worker_settings, http_client=client)
    await worker.check_in()
    task = hub_store.assign_task("bob", "implementer", "Task 1", "Instructions")

    async def cancel_task() -> None:
        for _ in range(100):
            current = hub_store.get_task(task.id)
            if current is not None and current.state == TaskState.INPUT_REQUIRED:
                break
            await asyncio.sleep(0.01)
        hub_store.set_task_state(task.id, TaskState.CANCELED, "Task aborted by user")

    ask_task = asyncio.create_task(worker.ask_alice(task.id, "How to proceed?", timeout_s=2.0))
    await asyncio.gather(cancel_task(), ask_task)

    res = ask_task.result()
    assert res.get("task_ended") is True
    assert res.get("state") == "canceled"
    assert "aborted" in str(res.get("note"))
    assert task.id not in worker._pending_questions


async def test_ask_alice_cancellation_without_result_summary(
    client: httpx.AsyncClient,
    worker_settings: WorkerSettings,
    hub_store: HubStore,
) -> None:
    worker = WorkerHubClient(worker_settings, http_client=client)
    await worker.check_in()
    task = hub_store.assign_task("bob", "implementer", "Task 1", "Instructions")

    async def cancel_task() -> None:
        for _ in range(100):
            current = hub_store.get_task(task.id)
            if current is not None and current.state == TaskState.INPUT_REQUIRED:
                break
            await asyncio.sleep(0.01)
        hub_store.cancel_task(task.id)

    ask_task = asyncio.create_task(worker.ask_alice(task.id, "How to proceed?", timeout_s=2.0))
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


async def test_retry_on_503(
    worker_settings: WorkerSettings, tmp_path: Path
) -> None:
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
    task = hub_store.assign_task("bob", "reviewer", "Review PR", "Review it")

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
