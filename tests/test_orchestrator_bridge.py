"""The bridge preserves tool schemas and survives an orchestrator restart."""

import asyncio
import json
import os
import sqlite3
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, cast
from unittest.mock import AsyncMock

import httpx
import pytest
from agent_hub.database import database
from agent_hub.store import HubStore
from agent_hub_common.discovery import DiscoveryError, read_hub_json
from conftest import BASE_URL, TOKEN
from fastapi import FastAPI
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.server.fastmcp.exceptions import ToolError
from worker_mcp.client import WorkerHubClient
from worker_mcp.config import WorkerSettings
from worker_mcp.orchestrator import (
    OrchestratorBridge,
    create_orchestrator_mcp,
)
from worker_mcp.tools import create_worker_mcp


async def _call(server: Any, name: str, **arguments: Any) -> dict[str, Any]:
    result = await server.call_tool(name, arguments)
    assert isinstance(result, tuple)
    return cast(dict[str, Any], result[1])


async def test_tool_list_matches_legacy_fixture() -> None:
    bridge = OrchestratorBridge()
    tools = await create_orchestrator_mcp(bridge).list_tools()
    actual = [tool.model_dump(exclude_none=True, by_alias=True) for tool in tools]
    fixture = Path(__file__).parent / "fixtures/orchestrator-tools.json"
    assert actual == json.loads(fixture.read_text(encoding="utf-8"))
    assert bridge._client is None  # Listing tools does not discover or connect.


async def test_discovery_failure_is_a_tool_error(monkeypatch: pytest.MonkeyPatch) -> None:
    def missing(_cwd: Path) -> None:
        raise DiscoveryError("cannot choose a hub; Registered hubs: repo-a, repo-b")

    monkeypatch.setattr("worker_mcp.orchestrator.discover", missing)
    server = create_orchestrator_mcp(OrchestratorBridge())
    await server.list_tools()
    with pytest.raises(ToolError, match="Registered hubs: repo-a, repo-b"):
        await _call(server, "get_state")


@pytest.mark.parametrize(("key", "retries"), [(None, False), ("decision-key", True)])
async def test_log_decision_retries_only_with_an_idempotency_key(
    key: str | None, retries: bool
) -> None:
    requests = 0

    def respond(request: httpx.Request) -> httpx.Response:
        nonlocal requests
        requests += 1
        if requests == 1:
            raise httpx.ConnectError("connection lost", request=request)
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": "x", "result": {"id": 1}})

    bridge = OrchestratorBridge()
    bridge._client = httpx.AsyncClient(
        transport=httpx.MockTransport(respond), base_url=BASE_URL
    )
    try:
        if retries:
            assert await bridge.call(
                "log_decision", {"summary": "s", "rationale": "r", "key": key}
            ) == {"id": 1}
            assert requests == 2
        else:
            with pytest.raises(httpx.ConnectError):
                await bridge.call("log_decision", {"summary": "s", "rationale": "r"})
            assert requests == 1
    finally:
        await bridge.close()


async def test_superseded_heartbeat_stops_without_repeating_error_logs(
    monkeypatch: pytest.MonkeyPatch
) -> None:
    bridge = OrchestratorBridge()
    requests = 0

    def respond(_request: httpx.Request) -> httpx.Response:
        nonlocal requests
        requests += 1
        return httpx.Response(200, json={
            "jsonrpc": "2.0", "id": "x",
            "error": {"code": -32002, "message": "superseded by a newer orchestrator session"},
        })

    bridge._client = httpx.AsyncClient(
        transport=httpx.MockTransport(respond), base_url=BASE_URL
    )
    monkeypatch.setattr("worker_mcp.orchestrator.asyncio.sleep", AsyncMock())
    try:
        await bridge._heartbeat_loop()
        assert requests == 1
    finally:
        await bridge.close()


async def test_worker_task_survives_orchestrator_session_restart(
    app: FastAPI, hub_store: HubStore
) -> None:
    async with app.router.lifespan_context(app):
        async def orchestrator() -> tuple[OrchestratorBridge, Any]:
            bridge = OrchestratorBridge()
            bridge._client = httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url=BASE_URL,
                headers={
                    "Authorization": f"Bearer {TOKEN}",
                    "X-Robomate-Actor": "alice",
                    "X-Robomate-Session": bridge.session,
                },
            )
            return bridge, create_orchestrator_mcp(bridge)

        old_bridge, old = await orchestrator()
        worker_http = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url=BASE_URL,
            headers={"Authorization": f"Bearer {TOKEN}"},
        )
        worker_client = WorkerHubClient(
            WorkerSettings(hub_url=BASE_URL, token=TOKEN, agent_name="bob"), worker_http
        )
        async with worker_client:
            worker = create_worker_mcp(worker_client, server_name="robomate")
            try:
                assert (await _call(old, "get_state"))["workflow"] is not None
                await _call(worker, "check_in")
                checked_in = (await _call(old, "wait_for_event", timeout_s=0))["event"]
                assert checked_in["kind"] == "agent_checked_in"
                task = await _call(
                    old, "assign_task", agent="bob", role="implementer", title="Fix",
                    instructions="Do it", event_id=checked_in["id"],
                )
                assigned = await _call(worker, "await_assignment", timeout_s=0)
                assert assigned["task_id"] == task["id"]
                assert await worker_client.heartbeat()
                pending = asyncio.create_task(
                    _call(worker, "ask_alice", task_id=task["id"], question="Which?",
                          timeout_s=5)
                )
                question = (await _call(
                    old, "wait_for_event", timeout_s=2,
                    ack=checked_in["delivery_id"],
                ))["event"]
                assert question["kind"] == "worker_question"
                before = hub_store.get_task(task["id"])
                assert before is not None

                new_bridge, new = await orchestrator()
                try:
                    redelivered = (await _call(new, "wait_for_event", timeout_s=0))["event"]
                    assert redelivered["id"] == question["id"]
                    assert redelivered["delivery_attempts"] == 2
                    assert redelivered["delivery_id"] != question["delivery_id"]
                    with pytest.raises(ToolError, match="superseded by a newer orchestrator"):
                        await _call(old, "get_state")
                    after = hub_store.get_task(task["id"])
                    assert after is not None and after.state == before.state
                    assert after.lease_expires == before.lease_expires
                    assert await worker_client.heartbeat()
                    await _call(
                        new, "reply", task_id=task["id"], text="This one",
                        message_id=question["payload"]["message_id"],
                    )
                    assert (await pending)["reply"] == "This one"
                    with database(hub_store.path) as connection:
                        kinds = [row[0] for row in connection.execute("SELECT kind FROM event")]
                    assert "agent_lost" not in kinds and "lease_expired" not in kinds
                finally:
                    await new_bridge.close()
            finally:
                await old_bridge.close()


async def test_stdio_bridges_drive_one_task_against_a_live_hub(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-b", "main"], cwd=repo, check=True, capture_output=True)
    subprocess.run(
        ["git", "remote", "add", "origin", "git@github.com:example/repo.git"],
        cwd=repo, check=True,
    )
    subprocess.run(
        ["git", "symbolic-ref", "refs/remotes/origin/HEAD", "refs/remotes/origin/main"],
        cwd=repo, check=True,
    )
    command = str(Path(sys.executable).with_name("robomate"))
    env = {**os.environ, "XDG_STATE_HOME": str(tmp_path / "xdg")}
    hub = subprocess.Popen(
        [command, "up"], cwd=repo, env=env, text=True,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )
    try:
        deadline = time.monotonic() + 10
        while True:
            info = read_hub_json(repo)
            if info is not None and info.get("pid") == hub.pid:
                break
            if hub.poll() is not None or time.monotonic() >= deadline:
                raise AssertionError("hub did not start")
            await asyncio.sleep(0.05)
        bridge_env = {
            **env,
            "ROBOMATE_HUB_URL": str(info["url"]),
            "ROBOMATE_TOKEN_FILE": str(repo / ".robomate/token"),
        }
        orchestrator = StdioServerParameters(
            command=command, args=["mcp", "--role", "orchestrator"], env=bridge_env,
        )
        worker = StdioServerParameters(
            command=command, args=["mcp", "--role", "worker", "--name", "bob"],
            env=bridge_env,
        )
        async with (
            stdio_client(orchestrator) as (alice_read, alice_write),
            ClientSession(alice_read, alice_write) as alice,
            stdio_client(worker) as (bob_read, bob_write),
            ClientSession(bob_read, bob_write) as bob,
        ):
            await alice.initialize()
            await bob.initialize()
            assert len((await alice.list_tools()).tools) == 10
            assert len((await bob.list_tools()).tools) == 6
            created = await alice.call_tool("initialize_workflow", {"goal": "One task"})
            assert created.structuredContent is not None
            checked = await bob.call_tool("check_in")
            assert checked.structuredContent is not None
            event = await alice.call_tool("wait_for_event", {"timeout_s": 0})
            assert event.structuredContent is not None
            checkin = event.structuredContent["event"]
            assigned = await alice.call_tool("assign_task", {
                "agent": "bob", "role": "implementer", "title": "Fix",
                "instructions": "Do it", "event_id": checkin["id"],
            })
            assert assigned.structuredContent is not None
            received = await bob.call_tool("await_assignment", {"timeout_s": 0})
            assert received.structuredContent is not None
            assert received.structuredContent["task_id"] == assigned.structuredContent["id"]
            pending = asyncio.create_task(bob.call_tool("ask_alice", {
                "task_id": assigned.structuredContent["id"], "question": "Which?",
                "timeout_s": 5,
            }))
            question = await alice.call_tool("wait_for_event", {
                "timeout_s": 2, "ack": checkin["delivery_id"],
            })
            assert question.structuredContent is not None
            first_delivery = question.structuredContent["event"]
            assert first_delivery["kind"] == "worker_question"
            # Start a second bridge while the old one still has the event lease.
            async with (
                stdio_client(orchestrator) as (new_read, new_write),
                ClientSession(new_read, new_write) as resumed,
            ):
                await resumed.initialize()
                repeated = await resumed.call_tool("wait_for_event", {"timeout_s": 0})
                assert repeated.structuredContent is not None
                second_delivery = repeated.structuredContent["event"]
                assert second_delivery["id"] == first_delivery["id"]
                assert second_delivery["delivery_attempts"] == 2
                assert (await alice.call_tool("get_state")).isError
                answered = await resumed.call_tool("reply", {
                    "task_id": assigned.structuredContent["id"], "text": "This one",
                    "message_id": first_delivery["payload"]["message_id"],
                })
                assert not answered.isError
            reply = await pending
            assert reply.structuredContent is not None
            assert reply.structuredContent["reply"] == "This one"
        with sqlite3.connect(repo / ".robomate/hub.db") as connection:
            rows = connection.execute(
                "SELECT tool, content_bytes FROM call_log "
                "WHERE boundary = 'mcp' AND actor = 'alice'"
            ).fetchall()
        assert rows
        assert any(tool == "initialize_workflow" and content_bytes > 0
                   for tool, content_bytes in rows)
    finally:
        if hub.poll() is None:
            stopped = subprocess.run(
                [command, "down"], cwd=repo, env=env, text=True,
                capture_output=True, timeout=10,
            )
            assert stopped.returncode == 0, stopped.stderr
            hub.wait(timeout=10)
        for stream in (hub.stdout, hub.stderr):
            if stream is not None:
                stream.close()
