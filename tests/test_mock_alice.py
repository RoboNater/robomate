import asyncio
import importlib.util
import json
import sys
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import anyio
import httpx
import pytest
from agent_hub import create_app
from agent_hub.database import database, initialize_database
from agent_hub.mcp import create_mcp
from agent_hub.store import HubStore, TaskRecord
from agent_hub_common import AgentProfile, HubSettings, TaskState, WorkflowStatus
from conftest import BASE_URL, TOKEN, MonotonicClock
from mcp import ClientSession
from worker_mcp.client import WorkerHubClient
from worker_mcp.config import WorkerSettings

script_path = Path(__file__).resolve().parents[1] / "scripts" / "mock-alice.py"
spec = importlib.util.spec_from_file_location("mock_alice", script_path)
assert spec is not None and spec.loader is not None
mock_alice = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mock_alice)


@asynccontextmanager
async def alice_backend(kind: str, store: HubStore) -> AsyncIterator[Any]:
    """Yield either adapter while keeping the MCP transport lifecycle out of tests."""

    if kind == "direct":
        yield mock_alice.DirectStoreBackend(store)
        return

    mcp_server = create_mcp(store)
    client_send, server_receive = anyio.create_memory_object_stream(10)
    server_send, client_receive = anyio.create_memory_object_stream(10)
    async with anyio.create_task_group() as task_group:
        task_group.start_soon(
            mcp_server._mcp_server.run,
            server_receive,
            server_send,
            mcp_server._mcp_server.create_initialization_options(),
        )
        async with ClientSession(client_receive, client_send) as session:
            await session.initialize()
            yield mock_alice.McpBackend(session)
        task_group.cancel_scope.cancel()


def test_verify_endurance_telemetry_requires_timeouts_heartbeat_and_release(
    tmp_path: Path,
) -> None:
    path = tmp_path / "worker.jsonl"
    records = [
        {
            "timestamp": "2026-09-12T00:00:00Z",
            "event": "session_started",
            "heartbeat_s": 30,
            "worker_instance_id": "current-worker",
        },
        {
            "timestamp": "2026-09-12T00:00:01Z",
            "event": "tool_call",
            "phase": "success",
            "tool": "await_assignment",
            "outcome": "timeout",
            "worker_instance_id": "current-worker",
        },
        {
            "timestamp": "2026-09-12T00:00:02Z",
            "event": "tool_call",
            "phase": "success",
            "tool": "ask_alice",
            "outcome": "timeout",
            "worker_instance_id": "current-worker",
        },
        {
            "timestamp": "2026-09-12T00:00:10Z",
            "event": "tool_call",
            "phase": "success",
            "tool": "await_assignment",
            "outcome": "assignment",
            "worker_instance_id": "current-worker",
        },
        *[
            {
                "timestamp": f"2026-09-12T00:{stamp}Z",
                "event": "heartbeat",
                "phase": "success",
                "accepted": True,
                "current_task_id": "long-task",
                "worker_instance_id": "current-worker",
            }
            for stamp in ("00:40", "01:10", "01:40", "02:10", "02:40", "03:10", "03:40")
        ],
        {
            "timestamp": "2026-09-12T00:04:10Z",
            "event": "tool_call",
            "phase": "start",
            "tool": "submit_result",
            "worker_instance_id": "current-worker",
        },
        {
            "timestamp": "2026-09-12T00:04:11Z",
            "event": "tool_call",
            "phase": "success",
            "tool": "await_assignment",
            "outcome": "release",
            "worker_instance_id": "current-worker",
        },
        {
            "timestamp": "2026-09-11T23:00:00Z",
            "event": "heartbeat",
            "phase": "success",
            "accepted": True,
            "current_task_id": "long-task",
            "worker_instance_id": "stale-worker",
        },
        {
            "timestamp": "2026-09-11T23:00:01Z",
            "event": "tool_call",
            "phase": "success",
            "tool": "await_assignment",
            "outcome": "release",
            "worker_instance_id": "stale-worker",
        },
    ]
    path.write_text("".join(json.dumps(record) + "\n" for record in records), encoding="utf-8")

    result = mock_alice.verify_endurance_telemetry(
        path, "long-task", "current-worker", lost_after_s=180
    )

    assert result["assignment_timeouts"] == 1
    assert result["question_timeouts"] == 1
    assert result["long_task_heartbeats"] == 7
    assert result["long_work_tool_gap_s"] == 240
    assert result["max_heartbeat_gap_s"] == 30
    assert result["releases"] == 1


def test_verify_endurance_telemetry_rejects_stale_worker_evidence(tmp_path: Path) -> None:
    path = tmp_path / "worker.jsonl"
    records = [
        {
            "timestamp": "2026-09-12T00:00:00Z",
            "event": "session_started",
            "heartbeat_s": 30,
            "worker_instance_id": "current-worker",
        },
        *[
            {
                "timestamp": f"2026-09-12T00:{stamp}Z",
                "event": "heartbeat",
                "phase": "success",
                "accepted": True,
                "current_task_id": "long-task",
                "worker_instance_id": "current-worker",
            }
            for stamp in ("00:30", "01:00", "01:30", "02:00", "02:30", "03:00", "03:30")
        ],
        {
            "timestamp": "2026-09-12T00:00:10Z",
            "event": "tool_call",
            "phase": "success",
            "tool": "await_assignment",
            "outcome": "assignment",
            "worker_instance_id": "current-worker",
        },
        {
            "timestamp": "2026-09-12T00:04:00Z",
            "event": "tool_call",
            "phase": "start",
            "tool": "submit_result",
            "worker_instance_id": "current-worker",
        },
        *[
            {
                "timestamp": f"2026-09-11T23:00:0{index}Z",
                "event": "tool_call",
                "phase": "success",
                "tool": tool,
                "outcome": outcome,
                "worker_instance_id": "stale-worker",
            }
            for index, (tool, outcome) in enumerate(
                (
                    ("await_assignment", "timeout"),
                    ("ask_alice", "timeout"),
                    ("await_assignment", "release"),
                )
            )
        ],
    ]
    path.write_text("".join(json.dumps(record) + "\n" for record in records), encoding="utf-8")

    with pytest.raises(RuntimeError, match="no await_assignment timeout"):
        mock_alice.verify_endurance_telemetry(
            path, "long-task", "current-worker", lost_after_s=180
        )


def test_verify_endurance_telemetry_rejects_only_approximate_long_work(
    tmp_path: Path,
) -> None:
    path = tmp_path / "worker.jsonl"
    base = {
        "worker_instance_id": "current-worker",
        "event": "tool_call",
        "phase": "success",
    }
    records = [
        {
            "timestamp": "2026-09-12T00:00:00Z",
            "event": "session_started",
            "heartbeat_s": 30,
            "worker_instance_id": "current-worker",
        },
        {
            **base,
            "timestamp": "2026-09-12T00:00:01Z",
            "tool": "await_assignment",
            "outcome": "timeout",
        },
        {
            **base,
            "timestamp": "2026-09-12T00:00:02Z",
            "tool": "ask_alice",
            "outcome": "timeout",
        },
        {
            "timestamp": "2026-09-12T00:00:30Z",
            "event": "heartbeat",
            "phase": "success",
            "accepted": True,
            "current_task_id": "long-task",
            "worker_instance_id": "current-worker",
        },
        *[
            {
                **base,
                "timestamp": f"2026-09-12T00:0{minute}:00Z",
                "tool": "await_assignment",
                "outcome": "timeout",
            }
            for minute in range(1, 4)
        ],
        {
            **base,
            "timestamp": "2026-09-12T00:04:00Z",
            "tool": "await_assignment",
            "outcome": "release",
        },
    ]
    path.write_text("".join(json.dumps(record) + "\n" for record in records), encoding="utf-8")

    with pytest.raises(RuntimeError, match="no heartbeat-covered gap"):
        mock_alice.verify_endurance_telemetry(
            path, "long-task", "current-worker", lost_after_s=180
        )


@pytest.mark.parametrize(
    ("backend_kind", "agent_name", "harness"),
    [
        (backend_kind, agent_name, harness)
        for backend_kind in ("direct", "mcp")
        for agent_name, harness in (("bob", "claude-code"), ("charlie", "codex"))
    ],
)
async def test_mock_alice_drives_worker_through_full_task(
    backend_kind: str,
    agent_name: str,
    harness: str,
    tmp_path: Path,
) -> None:
    db_path = tmp_path / f"hub_{agent_name}.db"
    initialize_database(db_path)
    settings = HubSettings(
        host="127.0.0.1",
        port=8420,
        public_url="http://hub.example:8420",
        state_dir=tmp_path,
        database_path=db_path,
        token=TOKEN,
        token_file=tmp_path / "token",
        guides_dir=tmp_path / "guides",
        default_wait_s=0.5,
        max_wait_s=1.0,
        lost_after_s=60.0,
        sweep_interval_s=3600.0,
    )
    app = create_app(settings)
    store = app.state.store

    worker_settings = WorkerSettings(
        hub_url=BASE_URL,
        token=TOKEN,
        agent_name=agent_name,
        profile=AgentProfile(harness=harness),
        default_wait_s=0.5,
        max_retries=2,
        backoff_factor_s=0.01,
    )

    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url=BASE_URL,
            headers={"Authorization": f"Bearer {TOKEN}"},
        ) as http_client,
    ):
        worker = WorkerHubClient(worker_settings, http_client=http_client)

        async def run_worker() -> None:
            # 1. Check in
            await worker.check_in(["python"])

            # 2. Wait for assignment
            assignment = await worker.await_assignment(timeout_s=2.0)
            assert assignment.get("task_id")
            task_id = assignment["task_id"]
            assert assignment["role"] == "implementer"

            # 3. Report progress
            prog = await worker.report_progress(task_id, "Working on it...")
            assert prog["ok"] is True

            # 4. Ask a question and receive answer
            q_res = await worker.ask_alice(task_id, "Confirm design?", timeout_s=2.0)
            assert "Approved" in q_res.get("reply", "")

            # 5. Submit result
            res = await worker.submit_result(
                task_id,
                "completed",
                "Feature implemented and tested",
                artifacts=[{"name": "pr", "url": "https://github.com/repo/pull/1"}],
            )
            assert res["status"] == "completed"

            # 6. Await assignment again -> should receive release
            rel = await worker.await_assignment(timeout_s=2.0)
            assert rel == {"release": True}

        async with alice_backend(backend_kind, store) as backend:
            alice_task = asyncio.create_task(
                mock_alice.drive_one_task_with_backend(
                    backend=backend,
                    expected_agent=agent_name,
                    role="implementer",
                    title="Test Issue",
                    instructions="Please fix the issue.",
                    timeout_s=5.0,
                    expected_harness=harness,
                    policy={"max_task_lease_min": 45},
                )
            )
            worker_task = asyncio.create_task(run_worker())

            alice_res, _ = await asyncio.gather(alice_task, worker_task)

        assert alice_res["task_id"] is not None
        assert store.get_state()["workflow"]["status"] == WorkflowStatus.DONE.value
        assert store.get_state()["workflow"]["policy"] == {"max_task_lease_min": 45}
        with database(store.path) as connection:
            decisions = connection.execute("SELECT * FROM decision ORDER BY id").fetchall()
            assert any("Assigned task" in d["summary"] for d in decisions)


# The scaled endurance scenario keeps the production shape — one work interval
# outlives lost-after while heartbeats keep the worker live — in seconds, not
# minutes. Hub sweeper, worker heartbeat and mock Alice share one event loop, so
# a stall of the loop under full-suite load delays the heartbeat until the
# sweep that follows it; lost-after must dwarf both the heartbeat interval and
# any plausible stall, or the sweep declares the worker lost (#95).
ENDURANCE_HEARTBEAT_S = 0.05
ENDURANCE_LOST_AFTER_S = 2.0
ENDURANCE_LONG_WORK_S = 2.5


async def test_mock_alice_drives_scaled_endurance_scenario(tmp_path: Path) -> None:
    assert ENDURANCE_LONG_WORK_S > ENDURANCE_LOST_AFTER_S >= 40 * ENDURANCE_HEARTBEAT_S
    db_path = tmp_path / "endurance.db"
    initialize_database(db_path)
    settings = HubSettings(
        host="127.0.0.1",
        port=8420,
        public_url=BASE_URL,
        state_dir=tmp_path,
        database_path=db_path,
        token=TOKEN,
        token_file=tmp_path / "token",
        guides_dir=tmp_path / "guides",
        default_wait_s=0.1,
        max_wait_s=1.0,
        lost_after_s=ENDURANCE_LOST_AFTER_S,
        sweep_interval_s=0.02,
    )
    app = create_app(settings)
    # Mock Alice checks the heartbeat stamp moved across a monotonic sleep; a
    # wall clock stepped back by more than that sleep would hold it still.
    app.state.store.clock = MonotonicClock()
    worker_settings = WorkerSettings(
        hub_url=BASE_URL,
        token=TOKEN,
        agent_name="bob",
        profile=AgentProfile(harness="codex"),
        default_wait_s=0.1,
        heartbeat_s=ENDURANCE_HEARTBEAT_S,
        max_retries=2,
        backoff_factor_s=0.01,
    )
    observed = {"assignment_timeouts": 0, "question_timeouts": 0, "cycles": 0}

    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url=BASE_URL,
            headers={"Authorization": f"Bearer {TOKEN}"},
        ) as http_client,
    ):

        async def run_worker() -> None:
            async with WorkerHubClient(worker_settings, http_client=http_client) as worker:
                await worker.check_in()
                while True:
                    assignment = await worker.await_assignment(timeout_s=0.1)
                    if assignment.get("timeout"):
                        observed["assignment_timeouts"] += 1
                        continue
                    if assignment.get("release"):
                        return

                    observed["cycles"] += 1
                    task_id = assignment["task_id"]
                    if observed["cycles"] == 1:
                        reply = await worker.ask_alice(
                            task_id, mock_alice.ENDURANCE_QUESTION, timeout_s=0.1
                        )
                        if reply.get("timeout"):
                            observed["question_timeouts"] += 1
                            reply = await worker.ask_alice(
                                task_id, mock_alice.ENDURANCE_QUESTION, timeout_s=1.0
                            )
                        assert "Approved" in reply.get("reply", "")
                    elif observed["cycles"] == 2:
                        await asyncio.sleep(ENDURANCE_LONG_WORK_S)

                    await worker.submit_result(
                        task_id,
                        {
                            "outcome": "completed",
                            "summary": f"Completed cycle {observed['cycles']}",
                            "pr_url": "https://github.com/RoboNater/robo-agents/pull/30",
                            "head_sha": "0123456789abcdef0123456789abcdef01234567",
                        },
                    )

        alice_task = asyncio.create_task(
            mock_alice.drive_endurance(
                app.state.store,
                "bob",
                expected_harness="codex",
                cycles=3,
                min_elapsed_s=1.5,
                assignment_delay_s=0.2,
                cycle_gap_s=0.1,
                worker_hold_s=0.1,
                question_hold_s=0.1,
                question_reply_delay_s=0.2,
                long_work_s=ENDURANCE_LONG_WORK_S,
                lost_after_s=ENDURANCE_LOST_AFTER_S,
                checkin_timeout_s=2.0,
            )
        )
        result, _ = await asyncio.gather(alice_task, run_worker())

    assert result["cycles"] == 3
    assert result["elapsed_s"] >= 1.5
    assert result["long_work_interval_s"] >= ENDURANCE_LONG_WORK_S
    assert result["heartbeat_advanced"] is True
    assert result["row_counts"] == {
        "assignments": 3,
        "questions": 1,
        "replies": 1,
        "results": 3,
    }
    assert observed["assignment_timeouts"] >= 1
    assert observed["question_timeouts"] == 1
    assert observed["cycles"] == 3


def test_endurance_gives_claude_code_a_supported_blocking_wait() -> None:
    action = mock_alice._long_work_action("claude-code", 210.0)

    assert "run_in_background=true" in action
    assert "TaskOutput exactly once" in action
    assert "Do not use `wait`" in action


async def test_mock_alice_rejects_unexpected_harness(tmp_path: Path) -> None:
    db_path = tmp_path / "hub_mismatch.db"
    initialize_database(db_path)
    store = HubStore(db_path)

    # Bob checks in as claude-code
    store.check_in("bob", AgentProfile(harness="claude-code"))

    # Mock Alice expects codex
    match_msg = "Worker 'bob' checked in with harness 'claude-code', expected 'codex'"
    with pytest.raises(ValueError, match=match_msg):
        await mock_alice.drive_one_task(
            store=store,
            expected_agent="bob",
            timeout_s=1.0,
            expected_harness="codex",
        )


def test_mock_alice_main_cli_parses_arguments(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    db_path = tmp_path / "cli_test.db"
    initialize_database(db_path)

    called = False

    async def fake_drive_one_task(
        store: object, expected_agent: str, **kwargs: object
    ) -> dict[str, str]:
        nonlocal called
        called = True
        assert expected_agent == "charlie"
        assert kwargs.get("expected_harness") == "codex"
        return {"status": "ok"}

    monkeypatch.setattr(mock_alice, "drive_one_task", fake_drive_one_task)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "mock-alice.py",
            "--db",
            str(db_path),
            "--agent",
            "charlie",
            "--runtime",
            "codex",
            "--timeout",
            "10",
        ],
    )

    mock_alice.main()
    assert called is True


def test_endurance_cli_requires_a_telemetry_log(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(sys, "argv", ["mock-alice.py", "--endurance"])

    with pytest.raises(SystemExit, match="2"):
        mock_alice.main()

    assert "--telemetry-log is required for an endurance run" in capsys.readouterr().err


@pytest.mark.parametrize("backend_kind", ["direct", "mcp"])
async def test_mock_alice_agent_already_checked_in_event_consumed(
    backend_kind: str, tmp_path: Path
) -> None:
    db_path = tmp_path / "already_checked_in.db"
    initialize_database(db_path)
    store = HubStore(db_path)

    # Bob checks in as codex
    store.check_in("bob", AgentProfile(harness="codex"))

    # Consume the check_in event so wait_for_event returns None
    event = await store.wait_for_event(timeout_s=0.01)
    assert event is not None and event.kind.value == "agent_checked_in"

    # Worker completes task once assigned
    async def finish_task() -> None:
        for _ in range(100):
            t = store.get_state()["tasks"]
            if t:
                task_id = t[0]["id"]
                store.submit_result(
                    task_id=task_id,
                    agent="bob",
                    status=TaskState.COMPLETED,
                    summary="Done",
                )
                break
            await asyncio.sleep(0.01)

    async with alice_backend(backend_kind, store) as backend:
        drive_fut = asyncio.create_task(
            mock_alice.drive_one_task_with_backend(
                backend=backend,
                expected_agent="bob",
                timeout_s=2.0,
                expected_harness="codex",
            )
        )
        finish_fut = asyncio.create_task(finish_task())
        res, _ = await asyncio.gather(drive_fut, finish_fut)
    assert res.get("summary") == "Done"


async def test_mock_alice_agent_already_checked_in_mismatch(tmp_path: Path) -> None:
    db_path = tmp_path / "already_mismatch.db"
    initialize_database(db_path)
    store = HubStore(db_path)

    # Bob checks in as codex
    store.check_in("bob", AgentProfile(harness="codex"))

    # Consume the check_in event
    await store.wait_for_event(timeout_s=0.01)

    match_msg = "Worker 'bob' checked in with harness 'codex', expected 'claude-code'"
    with pytest.raises(ValueError, match=match_msg):
        await mock_alice.drive_one_task(
            store=store,
            expected_agent="bob",
            timeout_s=1.0,
            expected_harness="claude-code",
        )


async def test_mock_alice_call_helper_error_handling() -> None:
    from unittest.mock import AsyncMock, MagicMock

    session = MagicMock()

    # Success call
    mock_success = MagicMock()
    mock_success.isError = False
    mock_success.structuredContent = {"ok": True}
    mock_success.content = None
    session.call_tool = AsyncMock(return_value=mock_success)

    data = await mock_alice._call(session, "get_state")
    assert data == {"ok": True}

    # Error call
    mock_error = MagicMock()
    mock_error.isError = True
    text_content = MagicMock()
    text_content.text = "Field required: summary"
    mock_error.content = [text_content]
    session.call_tool = AsyncMock(return_value=mock_error)

    with pytest.raises(RuntimeError, match="Tool log_decision failed: Field required: summary"):
        await mock_alice._call(session, "log_decision", {"decision": "wrong"})


def test_mock_alice_parse_cmd(monkeypatch: pytest.MonkeyPatch) -> None:
    # On Windows: parses paths with spaces inside quotes without stripping backslashes
    monkeypatch.setattr(sys, "platform", "win32")
    cmd_win = r'"C:\Program Files\Python312\python.exe" -m agent_hub.main'
    parts = mock_alice._parse_cmd(cmd_win)
    assert parts == [r"C:\Program Files\Python312\python.exe", "-m", "agent_hub.main"]

    # On POSIX: standard shlex.split
    monkeypatch.setattr(sys, "platform", "linux")
    cmd_posix = "/usr/bin/python3 -m agent_hub.main"
    assert mock_alice._parse_cmd(cmd_posix) == ["/usr/bin/python3", "-m", "agent_hub.main"]


def test_mock_alice_main_cli_mcp_flag(monkeypatch: pytest.MonkeyPatch) -> None:
    called_mcp = False

    def fake_run(coro: object) -> dict[str, str]:
        nonlocal called_mcp
        called_mcp = True
        if hasattr(coro, "close"):
            coro.close()
        return {"status": "ok"}

    monkeypatch.setattr(mock_alice.asyncio, "run", fake_run)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "mock-alice.py",
            "--mcp",
            "--agent",
            "bob",
            "--harness",
            "claude-code",
        ],
    )
    mock_alice.main()
    assert called_mcp is True


@pytest.mark.parametrize(
    ("backend_kind", "crash_at", "match"),
    [
        (backend_kind, crash_at, match)
        for backend_kind in ("direct", "mcp")
        for crash_at, match in (
            ("delivery", "crash at delivery"),
            ("after_action", "crash after action"),
            ("before_ack", "crash before ack"),
            ("after_reply", "crash after reply"),
        )
    ],
)
async def test_mock_alice_backends_recover_from_every_crash_point(
    backend_kind: str, crash_at: str, match: str, tmp_path: Path
) -> None:
    db_path = tmp_path / f"hub_crash_{backend_kind}_{crash_at}.db"
    initialize_database(db_path)
    store = HubStore(db_path, default_event_lease_s=0.01)

    async def run_worker() -> None:
        bob = store.check_in("bob", AgentProfile(harness="claude-code"))
        task = await store.await_assignment(bob.context_id, timeout_s=4.0)
        assert isinstance(task, TaskRecord)
        question_id = store.open_question(task.id, "bob", "Proceed?", "q-1")
        reply = await store.await_reply(task.id, question_id, timeout_s=4.0)
        assert reply is not None
        store.submit_result(
            task.id,
            "bob",
            TaskState.COMPLETED,
            "Recovered and finished",
        )
        release = await store.await_assignment(bob.context_id, timeout_s=4.0)
        assert release is not None

    async with alice_backend(backend_kind, store) as backend:
        worker_fut = asyncio.create_task(run_worker())
        try:
            with pytest.raises(mock_alice.AliceCrashError, match=match):
                await mock_alice.drive_one_task_with_backend(
                    backend=backend,
                    expected_agent="bob",
                    role="implementer",
                    timeout_s=5.0,
                    expected_harness="claude-code",
                    crash_at=crash_at,
                )
            await asyncio.sleep(0.03)
            result = await mock_alice.drive_one_task_with_backend(
                backend=backend,
                expected_agent="bob",
                role="implementer",
                timeout_s=5.0,
                expected_harness="claude-code",
            )
            await worker_fut
        finally:
            if not worker_fut.done():
                worker_fut.cancel()

    assert result.get("summary") == "Recovered and finished"
    assert len(store.tasks()) == 1
    assert store.get_state()["workflow"]["status"] == WorkflowStatus.DONE.value
    with database(store.path) as connection:
        assert connection.execute(
            "SELECT MAX(delivery_attempts) FROM event"
        ).fetchone()[0] >= 2
