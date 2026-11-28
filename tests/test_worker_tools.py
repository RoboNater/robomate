import ast
import hashlib
import json
from collections import Counter
from collections.abc import Callable
from datetime import timedelta
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import httpx
import pytest
from agent_hub.store import HubStore
from agent_hub_common import TaskState
from agent_hub_common.orchestrator_tools import OPERATIONS
from conftest import BASE_URL, TOKEN
from fastapi import FastAPI
from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.exceptions import ToolError
from mcp.shared.memory import create_connected_server_and_client_session
from mcp.types import TextContent
from timing import LIVENESS_S
from worker_mcp.client import WorkerHubClient
from worker_mcp.config import WorkerSettings
from worker_mcp.tools import create_worker_mcp

EXPECTED_TOOLS = {
    "submit_closeout_report",
    "join",
    "whoami",
    "get_role_guide",
    "await_assignment",
    "report_progress",
    "ask_orchestrator",
    "submit_result",
    "get_operator_answer",
    "get_statement",
}


async def test_combined_mcp_registers_every_tool_once(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    registrations: list[str] = []
    add_tool = FastMCP.add_tool

    def record_tool(server: FastMCP, fn: Callable[..., Any], **kwargs: Any) -> None:
        registrations.append(kwargs.get("name") or fn.__name__)
        add_tool(server, fn, **kwargs)

    monkeypatch.setattr(FastMCP, "add_tool", record_tool)
    client = WorkerHubClient(WorkerSettings.unjoined(environ={}))
    server = create_worker_mcp(client, include_orchestrator=True)

    # A new overlapping name must get an explicit role-dispatching implementation.
    assert EXPECTED_TOOLS.intersection(OPERATIONS) == {"submit_closeout_report", "get_statement"}
    expected = EXPECTED_TOOLS.union(OPERATIONS)
    assert Counter(registrations) == Counter(dict.fromkeys(expected, 1))
    assert {tool.name for tool in await server.list_tools()} == expected
    assert "Tool already exists" not in caplog.text


@pytest.mark.hub_settings(guides_dir=Path(__file__).resolve().parents[1] / "guides")
async def test_combined_mcp_get_statement_for_orchestrator_and_worker(
    app: FastAPI, tmp_path: Path
) -> None:
    statement = "Read caf\u00e9 requirements.\r\nKeep this trailing newline.\r\n"
    raw = statement.encode("utf-8")
    sha256 = hashlib.sha256(raw).hexdigest()

    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url=BASE_URL,
            headers={"Authorization": f"Bearer {TOKEN}"},
        ) as http,
    ):
        alice = WorkerHubClient(WorkerSettings.unjoined(cwd=tmp_path, environ={}), http)
        bob = WorkerHubClient(WorkerSettings.unjoined(cwd=tmp_path, environ={}), http)
        try:
            orchestrator = create_worker_mcp(alice, include_orchestrator=True)
            worker = create_worker_mcp(bob, include_orchestrator=True)
            await orchestrator.call_tool("join", {"name": "alice", "role": "orchestrator"})
            await orchestrator.call_tool("start_workflow", {"statement": statement})
            await worker.call_tool("join", {"name": "bob", "role": "implementer"})

            for server in (orchestrator, worker):
                result = await server.call_tool("get_statement", {"sha256": sha256})
                assert isinstance(result, tuple)
                stored = result[1]
                assert stored["sha256"] == sha256
                assert stored["statement"].encode("utf-8") == raw
                assert stored["size"] == len(raw)
                with pytest.raises(ToolError, match="unknown statement"):
                    await server.call_tool("get_statement", {"sha256": "0" * 64})
        finally:
            await alice.close()
            await bob.close()


async def test_worker_mcp_tools_list_and_dispatch() -> None:
    settings = WorkerSettings(
        hub_url="http://hub.example",
        token="tok",
        agent_name="bob",
    )
    client = WorkerHubClient(settings)
    client._joined = True
    client.join = AsyncMock(  # type: ignore[method-assign]
        return_value={"status": "registered", "agent": "bob", "role": "implementer"}
    )
    client.whoami = AsyncMock(  # type: ignore[method-assign]
        return_value={"joined": True, "name": "bob", "role": "implementer"}
    )
    client.get_role_guide = AsyncMock(return_value="# Guide\nContent")  # type: ignore[method-assign]
    client.await_assignment = AsyncMock(  # type: ignore[method-assign]
        return_value={"task_id": "t1", "role": "implementer", "instructions": "Do it"}
    )
    client.report_progress = AsyncMock(return_value={"ok": True, "note": "working"})  # type: ignore[method-assign]
    client.ask_orchestrator = AsyncMock(return_value={"reply": "yes"})  # type: ignore[method-assign]
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


async def test_mcp_unbound_bridge_fails_closed_without_leaking_hubs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The MCP tool layer replaces a hub-listing discovery error with a blocked hint (#511)."""

    from agent_hub_common.discovery import NoHubError

    def listing_error(
        cwd: Path, environ: Any, *, selector: Any = None, list_hubs: bool = True
    ) -> Any:
        # The genuinely unbound case is the one error the bridge rewrites (#511).
        raise NoHubError(
            "no hub is recorded for checkout /unbound. Live hubs:\n"
            "  alpha  http://alpha:8420  hub_id hub-a\n"
            "  beta   http://beta:8420   hub_id hub-b"
        )

    monkeypatch.setattr("worker_mcp.client.discover", listing_error)
    monkeypatch.setattr("worker_mcp.client.find_hub", listing_error)
    client = WorkerHubClient(WorkerSettings.unjoined(cwd=Path("/unbound"), environ={}))
    server = create_worker_mcp(client)

    with pytest.raises(ToolError) as joined:
        await server.call_tool("join", {"name": "bob", "role": "implementer"})
    message = str(joined.value)
    assert "this robomate bridge has no hub" in message
    assert "report blocked" in message
    assert "hub=" not in message
    assert "hub-a" not in message and "http://alpha" not in message

    # #526: an agent-chosen hub cannot select from the registry either.
    with pytest.raises(ToolError) as selected:
        await server.call_tool("join", {"name": "bob", "role": "implementer", "hub": "alpha"})
    selected_message = str(selected.value)
    assert "this robomate bridge has no hub" in selected_message
    assert "report blocked" in selected_message
    assert "alpha" not in selected_message
    assert "hub-a" not in selected_message
    assert "http://alpha" not in selected_message

    who = await server.call_tool("whoami", {})
    assert isinstance(who, tuple)
    assert who[1]["joined"] is False
    assert "this robomate bridge has no hub" in who[1]["discovery_error"]
    assert "hub-a" not in who[1]["discovery_error"]


async def test_unjoined_bridge_refuses_other_tools_and_names_join() -> None:
    client = WorkerHubClient(WorkerSettings.unjoined())
    server = create_worker_mcp(client)

    whoami_res = await server.call_tool("whoami", {})
    assert isinstance(whoami_res, tuple)
    assert whoami_res[1]["joined"] is False

    guarded_calls: list[tuple[str, dict[str, Any]]] = [
        ("get_role_guide", {"role": "implementer"}),
        ("await_assignment", {}),
        ("report_progress", {"task_id": "t1", "note": "step"}),
        ("ask_orchestrator", {"task_id": "t1", "question": "q?"}),
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
    client._joined = True
    client.await_assignment = AsyncMock(return_value={"timeout": True})  # type: ignore[method-assign]
    server = create_worker_mcp(client)

    result = await server.call_tool(
        "await_assignment", {"timeout_s": 0.1, "include_instructions": True}
    )

    client.await_assignment.assert_awaited_once_with(0.1, include_instructions=True)
    assert isinstance(result, tuple)
    records = [json.loads(line) for line in telemetry_path.read_text(encoding="utf-8").splitlines()]
    calls = [record for record in records if record.get("event") == "tool_call"]
    assert [(record["phase"], record.get("outcome")) for record in calls] == [
        ("start", None),
        ("success", "timeout"),
    ]


SHA = "0123456789abcdef0123456789abcdef01234567"


@pytest.mark.parametrize(
    "body",
    [
        pytest.param(
            {"outcome": "completed", "head_sha": SHA, "summary": "Rebased"},
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
            id="rebase-that-also-names-its-pr",
        ),
        pytest.param(
            {
                "outcome": "completed",
                "pr_url": "https://github.com/org/repo/pull/1",
                "head_sha": SHA,
                "summary": "Done",
            },
            id="implementer",
        ),
        pytest.param(
            {
                "outcome": "completed",
                "pr_url": "https://github.com/org/repo/pull/1",
                "head_sha": SHA,
                "commits": [SHA[:7]],
                "resolved_finding_ids": ["r1-1"],
                "summary": "Done",
            },
            id="invalid-implementer-that-fits-rebase-schema",
        ),
        pytest.param(
            {"verdict": "approved", "reviewed_head_sha": SHA, "summary": "Approved"},
            id="reviewer",
        ),
        pytest.param(
            {"outcome": "completed", "items": [], "steps": [], "summary": "Closed out"},
            id="closeout",
        ),
    ],
)
async def test_submit_result_passes_the_supplied_body_through(body: dict[str, Any]) -> None:
    settings = WorkerSettings(hub_url="http://hub.example", token="t", agent_name="b")
    client = WorkerHubClient(settings)
    client._joined = True
    client.submit_result = AsyncMock(return_value={"status": "completed"})  # type: ignore[method-assign]
    server = create_worker_mcp(client)

    await server.call_tool("submit_result", {"task_id": "t1", "result": body})

    client.submit_result.assert_awaited_once_with("t1", body)


@pytest.mark.hub_settings(guides_dir=Path(__file__).resolve().parents[1] / "guides")
async def test_submit_result_rejects_short_commit_and_accepts_retry(
    client: httpx.AsyncClient, hub_store: HubStore
) -> None:
    worker = WorkerHubClient(WorkerSettings.unjoined(environ={}), client)
    server = create_worker_mcp(worker)
    try:
        async with create_connected_server_and_client_session(
            server, read_timeout_seconds=timedelta(seconds=LIVENESS_S)
        ) as session:
            joined = await session.call_tool("join", {"name": "bob", "role": "implementer"})
            assert not joined.isError, joined.content
            task = hub_store.assign_task("bob", "implementer", "Address review", "Fix r1-1")
            assignment = await session.call_tool("await_assignment", {"timeout_s": 0})
            assert assignment.structuredContent is not None
            assert assignment.structuredContent["task_id"] == task.id
            result = {
                "outcome": "completed",
                "summary": "Addressed review",
                "pr_url": "https://github.com/org/repo/pull/1",
                "head_sha": SHA,
                "commits": [SHA[:7]],
                "resolved_finding_ids": ["r1-1"],
                "disputed_finding_ids": ["r1-2"],
            }

            rejected = await session.call_tool(
                "submit_result", {"task_id": task.id, "result": result}
            )
            assert rejected.isError, rejected.structuredContent
            error = rejected.content[0]
            assert isinstance(error, TextContent)
            assert "commits.0" in error.text
            still_open = hub_store.get_task(task.id)
            assert still_open is not None and still_open.state is TaskState.WORKING
            assert still_open.result is None

            result["commits"] = [SHA]
            accepted = await session.call_tool(
                "submit_result", {"task_id": task.id, "result": result}
            )
            assert not accepted.isError, accepted.content
            assert accepted.structuredContent is not None
            assert accepted.structuredContent["result"] == result
            stored = hub_store.get_task(task.id)
            assert stored is not None and stored.state is TaskState.COMPLETED
            assert stored.result is not None
            assert {key: stored.result[key] for key in result} == result
    finally:
        await worker.close()


async def test_submit_result_telemetry_identifies_task_on_error(tmp_path: Path) -> None:
    telemetry_path = tmp_path / "worker.jsonl"
    settings = WorkerSettings(
        hub_url="http://hub.example",
        token="t",
        agent_name="bob",
        telemetry_log=telemetry_path,
    )
    client = WorkerHubClient(settings)
    client._joined = True
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


async def test_submit_result_schema_carries_commits_pattern() -> None:
    """#371: the worker MCP submit_result schema exposes the commits pattern."""
    settings = WorkerSettings(hub_url="http://hub.example", token="t", agent_name="b")
    server = create_worker_mcp(WorkerHubClient(settings))
    tools = await server.list_tools()
    tool = next(t for t in tools if t.name == "submit_result")
    schema = tool.inputSchema
    commits = schema["$defs"]["ImplementerResult"]["properties"]["commits"]
    assert commits["items"]["pattern"] == r"^[0-9a-fA-F]{40}$"


@pytest.mark.hub_settings(guides_dir=Path(__file__).resolve().parents[1] / "guides")
async def test_orchestrator_can_refetch_complete_guide_in_pages(
    app: FastAPI, tmp_path: Path
) -> None:
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url=BASE_URL,
            headers={"Authorization": f"Bearer {TOKEN}"},
        ) as http,
    ):
        alice = WorkerHubClient(WorkerSettings.unjoined(cwd=tmp_path, environ={}), http)
        try:
            server = create_worker_mcp(alice, include_orchestrator=True)
            joined = await server.call_tool("join", {"name": "alice", "role": "orchestrator"})
            assert isinstance(joined, tuple)
            original = (await http.get("/guides/orchestrator.md")).text
            assert "get_role_guide" in joined[1]["guide"]
            for phrase in ("get_state", "start_workflow()", "Do not end your turn"):
                assert phrase in joined[1]["guide"]
            assert len(json.dumps(joined[1]).encode()) < 40000
            parts = []
            for page in range(1, 100):
                result = await server.call_tool(
                    "get_role_guide", {"role": "orchestrator", "page": page}
                )
                assert isinstance(result, tuple)
                header, content = result[1]["result"].split("\n", 1)
                assert len(json.dumps(result[1]).encode()) < 40000
                parts.append(content)
                if page == int(header.split("/")[1]):
                    break
            assert "".join(parts) == original
            assert len(parts) <= 4
            assert "# GitHub Orchestrator Appendix" in original
            with pytest.raises(ToolError, match="only fetch its own guide"):
                await server.call_tool("get_role_guide", {"role": "implementer", "page": 1})
        finally:
            await alice.close()


@pytest.mark.hub_settings(guides_dir=Path(__file__).resolve().parents[1] / "guides")
@pytest.mark.parametrize("role", ["orchestrator", "worker", "implementer", "reviewer"])
async def test_join_guide_can_be_retrieved_separately(
    app: FastAPI, tmp_path: Path, role: str
) -> None:
    """Join may exceed client limits; the guide must remain retrievable separately."""
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url=BASE_URL,
            headers={"Authorization": f"Bearer {TOKEN}"},
        ) as http,
    ):
        client = WorkerHubClient(WorkerSettings.unjoined(cwd=tmp_path, environ={}), http)
        try:
            server = create_worker_mcp(client, include_orchestrator=True)
            joined = await server.call_tool(
                "join", {"name": "alice" if role == "orchestrator" else "bob", "role": role}
            )
            assert isinstance(joined, tuple)
            # 40 KB leaves headroom below supported harness tool-output limits (#632).
            assert len(json.dumps(joined[1]).encode()) < 40000
            if role == "orchestrator":
                result = await server.call_tool("get_role_guide", {"role": role, "page": 1})
                assert isinstance(result, tuple)
                assert result[1]["result"].startswith("Page 1/")
            else:
                result = await server.call_tool("get_role_guide", {"role": "worker"})
                assert isinstance(result, tuple)
                assert result[1]["result"] == joined[1]["guide"]
                role_result = await server.call_tool("get_role_guide", {"role": role})
                assert isinstance(role_result, tuple)
                assert "get_role_guide" in joined[1]["role_guide"]
                title = "Worker etiquette" if role == "worker" else role.title()
                assert role_result[1]["result"].startswith(f"# {title}\n")
                assert "# GitHub Forge Appendix" in role_result[1]["result"]
                assert joined[1]["next_call"] == "await_assignment"
                launcher = ast.parse(
                    (Path(__file__).resolve().parents[1] / "scripts/launcher_common.py").read_text(
                        encoding="utf-8"
                    )
                )
                rule = next(
                    ast.literal_eval(node.value)
                    for node in launcher.body
                    if isinstance(node, ast.Assign)
                    and any(
                        isinstance(target, ast.Name) and target.id == "WORKER_TURN_RULE"
                        for target in node.targets
                    )
                )
                assert joined[1]["turn_instruction"] == "Call await_assignment next. " + rule
                assert "Every reply must contain a tool call" in rule
                assert "release: true" in rule
                assert list(joined[1]).index("turn_instruction") < list(joined[1]).index("guide")
                again = await server.call_tool(
                    "join", {"name": "bob", "role": role, "takeover": True}
                )
                assert isinstance(again, tuple)
                assert again[1]["next_call"] == "await_assignment"
                assert again[1]["turn_instruction"] == joined[1]["turn_instruction"]
                assert list(again[1]).index("turn_instruction") < list(again[1]).index("guide")
                assert len(json.dumps(again[1]).encode()) < 40000
                with pytest.raises(ToolError, match="not found"):
                    await server.call_tool("get_role_guide", {"role": "denise"})
        finally:
            await client.close()


def test_guide_pages_bound_json_escaped_unicode() -> None:
    from worker_mcp.guide_pages import guide_page

    guide = '😀"\\\n' * 6000
    parts = []
    for page in range(1, 100):
        result = guide_page(guide, page)
        assert len(json.dumps({"result": result}).encode()) < 20000
        header, content = result.split("\n", 1)
        parts.append(content)
        if page == int(header.split("/")[1]):
            break
    assert "".join(parts) == guide
    with pytest.raises(ValueError, match="at least 1"):
        guide_page(guide, 0)
    with pytest.raises(ValueError, match="exceeds"):
        guide_page(guide, 100)
