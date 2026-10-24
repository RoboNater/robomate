import json
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import pytest
from agent_hub_common import ImplementerResult, RebaseResult
from mcp.server.fastmcp.exceptions import ToolError
from worker_mcp.client import WorkerHubClient
from worker_mcp.config import WorkerSettings
from worker_mcp.tools import create_worker_mcp

EXPECTED_TOOLS = {
    "submit_closeout_report",
    "join",
    "whoami",
    "check_in",
    "get_role_guide",
    "await_assignment",
    "report_progress",
    "ask_orchestrator",
    "ask_alice",
    "submit_result",
    "get_operator_answer",
    "get_statement",
}


async def test_worker_mcp_tools_list_and_dispatch() -> None:
    settings = WorkerSettings(
        hub_url="http://hub.example",
        token="tok",
        agent_name="bob",
    )
    client = WorkerHubClient(settings)
    client.join = AsyncMock(  # type: ignore[method-assign]
        return_value={"status": "registered", "agent": "bob", "role": "implementer"}
    )
    client.whoami = AsyncMock(  # type: ignore[method-assign]
        return_value={"joined": True, "name": "bob", "role": "implementer"}
    )
    client.check_in = AsyncMock(  # type: ignore[method-assign]
        return_value={"status": "registered", "agent": "bob", "context_id": "c1"}
    )
    client.get_role_guide = AsyncMock(return_value="# Guide\nContent")  # type: ignore[method-assign]
    client.await_assignment = AsyncMock(  # type: ignore[method-assign]
        return_value={"task_id": "t1", "role": "implementer", "instructions": "Do it"}
    )
    client.report_progress = AsyncMock(return_value={"ok": True, "note": "working"})  # type: ignore[method-assign]
    client.ask_orchestrator = AsyncMock(return_value={"reply": "yes"})  # type: ignore[method-assign]
    client.ask_alice = AsyncMock(return_value={"reply": "yes"})  # type: ignore[method-assign]
    client.submit_result = AsyncMock(return_value={"status": "completed", "task_id": "t1"})  # type: ignore[method-assign]
    client.get_operator_answer = AsyncMock(return_value={"question_id": 1, "status": "unanswered"})  # type: ignore[method-assign]
    client.get_statement = AsyncMock(  # type: ignore[method-assign]
        return_value={"sha256": "s" * 64, "statement": "Do it", "size": 5}
    )

    server = create_worker_mcp(client)
    tools = await server.list_tools()
    tool_by_name = {t.name: t for t in tools}
    assert set(tool_by_name) == EXPECTED_TOOLS
    assert (
        tool_by_name["join"].description
        == "Call when the user asks you to join robomate. Use the name and role the user gives."
    )

    async def call(tool_name: str, **args: Any) -> Any:
        result = await server.call_tool(tool_name, args)
        assert isinstance(result, tuple)
        return result[1]

    assert (await call("join", role="implementer", name="bob")) == {
        "status": "registered",
        "agent": "bob",
        "role": "implementer",
    }
    assert (await call("whoami")) == {
        "joined": True,
        "name": "bob",
        "role": "implementer",
    }
    assert (await call("check_in", capabilities=["python"])) == {
        "status": "registered",
        "agent": "bob",
        "context_id": "c1",
    }
    assert (await call("get_role_guide", role="implementer")) == {"result": "# Guide\nContent"}
    assert (await call("await_assignment", timeout_s=60.0)) == {
        "task_id": "t1",
        "role": "implementer",
        "instructions": "Do it",
    }
    assert (await call("report_progress", task_id="t1", note="working")) == {
        "ok": True,
        "note": "working",
    }
    assert (
        await call("ask_orchestrator", task_id="t1", question="is this ok?", timeout_s=30.0)
    ) == {"reply": "yes"}
    assert (await call("ask_alice", task_id="t1", question="is this ok?", timeout_s=30.0)) == {
        "reply": "yes",
    }
    assert (
        await call(
            "submit_result",
            task_id="t1",
            result={
                "outcome": "completed",
                "summary": "Done",
                "pr_url": "https://github.com/org/repo/pull/1",
                "head_sha": "0123456789abcdef0123456789abcdef01234567",
            },
        )
    ) == {"status": "completed", "task_id": "t1"}
    assert (await call("get_operator_answer", question_id=1)) == {
        "question_id": 1,
        "status": "unanswered",
    }
    assert (await call("get_statement", sha256="s" * 64)) == {
        "sha256": "s" * 64,
        "statement": "Do it",
        "size": 5,
    }


async def test_unjoined_bridge_refuses_other_tools_and_names_join() -> None:
    client = WorkerHubClient(WorkerSettings.unjoined())
    server = create_worker_mcp(client)

    whoami_res = await server.call_tool("whoami", {})
    assert isinstance(whoami_res, tuple)
    assert whoami_res[1]["joined"] is False

    guarded_calls: list[tuple[str, dict[str, Any]]] = [
        ("check_in", {}),
        ("get_role_guide", {"role": "implementer"}),
        ("await_assignment", {}),
        ("report_progress", {"task_id": "t1", "note": "step"}),
        ("ask_orchestrator", {"task_id": "t1", "question": "q?"}),
        ("ask_alice", {"task_id": "t1", "question": "q?"}),
        (
            "submit_result",
            {
                "task_id": "t1",
                "result": {
                    "outcome": "completed",
                    "summary": "Done",
                    "pr_url": "https://github.com/org/repo/pull/1",
                    "head_sha": "0123456789abcdef0123456789abcdef01234567",
                },
            },
        ),
        ("get_operator_answer", {"question_id": 1}),
        ("get_statement", {"sha256": "s" * 64}),
    ]
    for tool_name, args in guarded_calls:
        with pytest.raises(ToolError, match="join"):
            await server.call_tool(tool_name, args)


async def test_worker_mcp_tool_boundary_writes_telemetry(tmp_path: Path) -> None:
    telemetry_path = tmp_path / "worker.jsonl"
    settings = WorkerSettings(
        hub_url="http://hub.example",
        token="tok",
        agent_name="bob",
        telemetry_log=telemetry_path,
    )
    client = WorkerHubClient(settings)
    client.await_assignment = AsyncMock(return_value={"timeout": True})  # type: ignore[method-assign]
    server = create_worker_mcp(client)

    result = await server.call_tool("await_assignment", {"timeout_s": 0.1})

    assert isinstance(result, tuple)
    records = [json.loads(line) for line in telemetry_path.read_text(encoding="utf-8").splitlines()]
    calls = [record for record in records if record.get("event") == "tool_call"]
    assert [(record["phase"], record.get("outcome")) for record in calls] == [
        ("start", None),
        ("success", "timeout"),
    ]


SHA = "0123456789abcdef0123456789abcdef01234567"


@pytest.mark.parametrize(
    ("body", "expected"),
    [
        pytest.param(
            {"outcome": "completed", "head_sha": SHA, "summary": "Rebased"},
            RebaseResult,
            id="clean-rebase-without-pr-url",
        ),
        pytest.param(
            {
                "outcome": "completed",
                "pr_url": "https://github.com/org/repo/pull/1",
                "head_sha": SHA,
                "conflict_files": ["README.md"],
                "resolution_summary": "Kept both",
                "summary": "Rebased",
            },
            RebaseResult,
            id="rebase-that-also-names-its-pr",
        ),
        pytest.param(
            {
                "outcome": "completed",
                "pr_url": "https://github.com/org/repo/pull/1",
                "head_sha": SHA,
                "summary": "Done",
            },
            ImplementerResult,
            id="implementer",
        ),
    ],
)
async def test_submit_result_keeps_a_rebase_body_whole(
    body: dict[str, Any], expected: type[Any]
) -> None:
    settings = WorkerSettings(hub_url="http://hub.example", token="t", agent_name="b")
    client = WorkerHubClient(settings)
    client.submit_result = AsyncMock(return_value={"status": "completed"})  # type: ignore[method-assign]
    server = create_worker_mcp(client)

    await server.call_tool("submit_result", {"task_id": "t1", "result": body})

    submitted = client.submit_result.call_args.args[1]
    assert type(submitted) is expected
    assert submitted.model_dump(exclude_unset=True) == body


async def test_submit_result_telemetry_identifies_task_on_error(tmp_path: Path) -> None:
    telemetry_path = tmp_path / "worker.jsonl"
    settings = WorkerSettings(
        hub_url="http://hub.example",
        token="t",
        agent_name="bob",
        telemetry_log=telemetry_path,
    )
    client = WorkerHubClient(settings)
    client.submit_result = AsyncMock(side_effect=RuntimeError("already completed"))  # type: ignore[method-assign]
    server = create_worker_mcp(client)

    with pytest.raises(ToolError, match="already completed"):
        await server.call_tool(
            "submit_result",
            {
                "task_id": "wrong-task-id",
                "result": {
                    "outcome": "completed",
                    "pr_url": "https://github.com/org/repo/pull/1",
                    "head_sha": SHA,
                    "summary": "Done",
                },
            },
        )

    records = [json.loads(line) for line in telemetry_path.read_text(encoding="utf-8").splitlines()]
    calls = [record for record in records if record.get("event") == "tool_call"]
    assert [(record["phase"], record["task_id"]) for record in calls] == [
        ("start", "wrong-task-id"),
        ("error", "wrong-task-id"),
    ]
