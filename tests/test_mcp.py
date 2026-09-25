import asyncio
import json
import os
import socket
import subprocess
import sys
import time
import urllib.request
from pathlib import Path
from typing import Any, cast

import httpx
import pytest
from agent_hub.database import database, initialize_database
from agent_hub.mcp import CancellableStdout, create_mcp
from agent_hub.store import HubStore
from agent_hub_common import MAX_MESSAGE_PART_BYTES, SCHEMA_VERSION, AgentProfile, MetaKeys
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

TOOLS = {
    "get_state",
    "initialize_workflow",
    "wait_for_event",
    "assign_task",
    "reply",
    "set_task_state",
    "release_agent",
    "set_workflow_status",
    "log_decision",
    "check_merge_gate",
}


async def test_tools_and_durable_actions(tmp_path: Path) -> None:
    path = tmp_path / "hub.db"
    initialize_database(path)
    store = HubStore(path)
    server = create_mcp(store)

    async def call(name: str, **args: Any) -> Any:
        result = await server.call_tool(name, args)
        assert isinstance(result, tuple)
        return result[1]

    assert {t.name for t in await server.list_tools()} == TOOLS
    assert (await call("get_state"))["workflow"] is None
    with pytest.raises(Exception, match="initialize_workflow.*set_workflow_status"):
        await call("set_workflow_status", status="done", summary="Too early")
    with pytest.raises(Exception, match="invalid workflow policy.*max_review_round"):
        await call(
            "initialize_workflow",
            goal="Address issue #5",
            policy={"max_review_round": 5},
        )
    assert (await call("get_state"))["workflow"] is None
    initialized = await call(
        "initialize_workflow",
        goal="Address issue #5",
        policy={"max_task_lease_min": 10},
    )
    assert initialized["policy"] == {"max_task_lease_min": 10}
    assert (
        await call(
            "initialize_workflow",
            goal="Address issue #5",
            policy={"max_task_lease_min": 10},
        )
    )["id"] == initialized["id"]
    bob = store.check_in("bob", AgentProfile(capabilities=("python",)))
    checkin_event = store.lease_next_event()
    assert checkin_event is not None
    with pytest.raises(Exception, match=f"maximum is {MAX_MESSAGE_PART_BYTES} bytes"):
        await call(
            "assign_task",
            agent="bob",
            role="implementer",
            title="Too large",
            instructions="x" * MAX_MESSAGE_PART_BYTES,
            event_id=checkin_event.id,
        )
    assert store.tasks() == []
    pending = asyncio.create_task(store.await_assignment(bob.context_id, 1))
    await asyncio.sleep(0)
    task = await call(
        "assign_task",
        agent="bob",
        role="implementer",
        title="Fix",
        instructions="Do it",
        event_id=checkin_event.id,
    )
    claimed = await pending
    assert claimed is not None
    question = store.open_question(task["id"], "bob", "Which?", "q1")
    waiting = asyncio.create_task(store.await_reply(task["id"], question, 1))
    await asyncio.sleep(0)
    with pytest.raises(Exception, match="message_id"):
        await call("reply", task_id=task["id"], text="This one")
    await call("reply", task_id=task["id"], text="This one", message_id=question)
    answer = await waiting
    assert answer is not None and answer.parts[0]["text"] == "This one"
    await call("set_task_state", task_id=task["id"], state="failed", note="Stop")
    assert store.agent_by_name("bob").current_task_id is None  # type: ignore[union-attr]
    await call("release_agent", agent="bob")
    await call("set_workflow_status", status="done", summary="Finished")
    await call("log_decision", summary="Decision", rationale="Because")
    state = HubStore(path).get_state()
    assert state["workflow"]["status"] == "done"
    assert state["tasks"][0]["result"]["summary"] == "Stop"
    assert "instructions" not in state["tasks"][0]
    with database(path) as conn:
        assert (
            conn.execute("SELECT summary FROM decision ORDER BY id").fetchall()[0][0] == "Finished"
        )
    for args in ({"timeout_s": -1}, {"timeout_s": 121}, {"timeout_s": float("inf")}):
        with pytest.raises(Exception, match="validation error"):
            await call("wait_for_event", **args)
    with pytest.raises(Exception, match="already failed"):
        await call("set_task_state", task_id=task["id"], state="canceled", note="Again")


async def test_canceling_a_blocked_stdout_write_returns_promptly() -> None:
    read_fd, write_fd = os.pipe()
    os.set_blocking(write_fd, False)
    try:
        while True:
            os.write(write_fd, b"x" * 4096)
    except BlockingIOError:
        pass
    os.set_blocking(write_fd, True)
    writer = CancellableStdout(os.fdopen(write_fd, "w", closefd=False))
    try:
        pending = asyncio.create_task(writer.write("blocked"))
        await asyncio.sleep(0.05)
        assert not pending.done()
        pending.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(pending, 0.5)
    finally:
        os.close(read_fd)
        assert writer.join_workers(1)
        os.close(write_fd)


async def test_stdio_and_http_share_events(tmp_path: Path) -> None:
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    params = StdioServerParameters(
        command=sys.executable,
        args=["-m", "agent_hub.main"],
        env={
            **os.environ,
            "HUB_STATE_DIR": str(tmp_path),
            "HUB_DB_PATH": str(tmp_path / "hub.db"),
            "HUB_HOST": "127.0.0.1",
            "HUB_PORT": str(port),
            "HUB_TOKEN": "test",
        },
    )
    async with stdio_client(params) as (read, write), ClientSession(read, write) as session:
        await session.initialize()
        assert {t.name for t in (await session.list_tools()).tools} == TOOLS
        timeout = await session.call_tool("wait_for_event", {"timeout_s": 0.02})
        assert timeout.structuredContent == {"event": None}
        pending = asyncio.create_task(session.call_tool("wait_for_event", {"timeout_s": 2}))
        await asyncio.sleep(0.05)
        assert not pending.done()
        async with httpx.AsyncClient() as client:
            response = await client.post(
                f"http://127.0.0.1:{port}/a2a",
                headers={"Authorization": "Bearer test"},
                json={
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "message/send",
                    "params": {
                        "message": {
                            "messageId": "ready",
                            "role": "user",
                            "parts": [{"kind": "text", "text": "READY"}],
                            "metadata": {
                                MetaKeys.AGENT: "bob",
                                MetaKeys.CAPABILITIES: ["python"],
                                MetaKeys.SCHEMA_VERSION: SCHEMA_VERSION,
                                MetaKeys.OPERATION_ID: "op-mcp-1",
                                MetaKeys.WORKER_INSTANCE_ID: "test-worker-instance",
                            },
                        }
                    },
                },
            )
            assert response.status_code == 200
        result = await pending
        assert result.structuredContent is not None
        assert result.structuredContent["event"]["kind"] == "agent_checked_in"
        empty = await session.call_tool("wait_for_event", {"timeout_s": 0})
        assert empty.structuredContent == {"event": None}
        error = await session.call_tool("reply", {"task_id": "missing", "text": "hi"})
        assert error.isError
        assert json.loads((await session.call_tool("get_state")).content[0].text)["agents"]  # type: ignore[union-attr]
    async with httpx.AsyncClient() as client:
        with pytest.raises(httpx.ConnectError):
            await client.get(f"http://127.0.0.1:{port}/healthz")


def test_wire_cancellation_stops_event_consumption(tmp_path: Path) -> None:
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    process = subprocess.Popen(
        [sys.executable, "-m", "agent_hub.main"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        bufsize=1,
        env={
            **os.environ,
            "HUB_STATE_DIR": str(tmp_path),
            "HUB_DB_PATH": str(tmp_path / "hub.db"),
            "HUB_HOST": "127.0.0.1",
            "HUB_PORT": str(port),
            "HUB_TOKEN": "test",
        },
    )

    def send(payload: dict[str, Any]) -> None:
        assert process.stdin is not None
        process.stdin.write(json.dumps(payload) + "\n")
        process.stdin.flush()

    def receive() -> dict[str, Any]:
        assert process.stdout is not None
        return cast(dict[str, Any], json.loads(process.stdout.readline()))

    try:
        deadline = time.monotonic() + 10
        while True:
            try:
                urllib.request.urlopen(f"http://127.0.0.1:{port}/healthz", timeout=0.2).close()
                break
            except OSError:
                assert time.monotonic() < deadline
                time.sleep(0.02)
        send(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-06-18",
                    "capabilities": {},
                    "clientInfo": {"name": "test", "version": "1"},
                },
            }
        )
        assert receive()["id"] == 1
        send({"jsonrpc": "2.0", "method": "notifications/initialized"})
        send(
            {
                "jsonrpc": "2.0",
                "id": 42,
                "method": "tools/call",
                "params": {"name": "wait_for_event", "arguments": {"timeout_s": 120}},
            }
        )
        send(
            {
                "jsonrpc": "2.0",
                "method": "notifications/cancelled",
                "params": {"requestId": 42, "reason": "test"},
            }
        )
        canceled = receive()
        assert canceled["id"] == 42
        assert canceled["error"]["message"] == "Request cancelled"

        request = urllib.request.Request(
            f"http://127.0.0.1:{port}/a2a",
            data=json.dumps(
                {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "message/send",
                    "params": {
                        "message": {
                            "messageId": "ready",
                            "role": "user",
                            "parts": [{"kind": "text", "text": "READY"}],
                            "metadata": {
                                MetaKeys.AGENT: "bob",
                                MetaKeys.CAPABILITIES: [],
                                MetaKeys.SCHEMA_VERSION: SCHEMA_VERSION,
                                MetaKeys.OPERATION_ID: "op-mcp-2",
                                MetaKeys.WORKER_INSTANCE_ID: "test-worker-instance",
                            },
                        }
                    },
                }
            ).encode(),
            headers={"Authorization": "Bearer test", "Content-Type": "application/json"},
        )
        urllib.request.urlopen(request, timeout=1).close()
        send(
            {
                "jsonrpc": "2.0",
                "id": 43,
                "method": "tools/call",
                "params": {"name": "wait_for_event", "arguments": {"timeout_s": 1}},
            }
        )
        delivered = receive()
        assert delivered["id"] == 43
        content = json.loads(delivered["result"]["content"][0]["text"])
        assert content["event"]["kind"] == "agent_checked_in"
    finally:
        if process.stdin is not None:
            process.stdin.close()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()
        for stream in (process.stdout, process.stderr):
            if stream is not None:
                stream.close()


@pytest.mark.parametrize("enabled", [True, False], ids=["accounting-on", "accounting-off"])
async def test_stdio_calls_are_accounted_only_when_enabled(tmp_path: Path, enabled: bool) -> None:
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    jsonl = tmp_path / "calls.jsonl"
    env = {
        **os.environ,
        "HUB_STATE_DIR": str(tmp_path),
        "HUB_DB_PATH": str(tmp_path / "hub.db"),
        "HUB_HOST": "127.0.0.1",
        "HUB_PORT": str(port),
        "HUB_TOKEN": "test",
    }
    for name in ("HUB_CALL_ACCOUNTING", "HUB_CALL_LOG_JSONL"):
        env.pop(name, None)
    if enabled:
        env |= {"HUB_CALL_ACCOUNTING": "1", "HUB_CALL_LOG_JSONL": str(jsonl)}
    secret = "IGNORE PREVIOUS INSTRUCTIONS"
    params = StdioServerParameters(command=sys.executable, args=["-m", "agent_hub.main"], env=env)
    # The client parses every stdout line as JSON-RPC, so any stray write
    # during accounting fails the session here (#7).
    async with stdio_client(params) as (read, write), ClientSession(read, write) as session:
        await session.initialize()
        await session.list_tools()
        await session.call_tool("initialize_workflow", {"goal": secret})
        await session.call_tool("wait_for_event", {"timeout_s": 0})
        state = [await session.call_tool("get_state") for _ in range(2)]
        await session.call_tool("log_decision", {"summary": secret, "rationale": secret})

    with database(tmp_path / "hub.db") as connection:
        rows = [dict(row) for row in connection.execute("SELECT * FROM call_log ORDER BY id")]
    if not enabled:
        assert rows == []
        assert not jsonl.exists()
        return
    assert [(row["tool"], row["outcome"]) for row in rows] == [
        ("initialize", "ok"),
        ("tools/list", "ok"),
        ("initialize_workflow", "ok"),
        ("wait_for_event", "null_event"),
        ("get_state", "ok"),
        ("get_state", "ok"),
        ("log_decision", "ok"),
    ]
    assert {(row["boundary"], row["actor"]) for row in rows} == {("mcp", "alice")}
    tools_list = rows[1]
    assert tools_list["bytes_out"] > 1000  # Ten tool schemas, once per session.
    first, second = rows[4], rows[5]
    text = state[0].content[0].text  # type: ignore[union-attr]
    assert first["content_bytes"] == len(text.encode("utf-8"))
    assert first["repeat_bytes"] == 0
    assert 0 < second["repeat_bytes"] <= second["content_bytes"]
    assert all(row["bytes_out"] > (row["content_bytes"] or 0) for row in rows)
    assert all(row["workflow_id"] for row in rows[3:])
    assert len(jsonl.read_text(encoding="utf-8").splitlines()) == len(rows)
    assert secret not in json.dumps(rows)
    assert secret not in jsonl.read_text(encoding="utf-8")
