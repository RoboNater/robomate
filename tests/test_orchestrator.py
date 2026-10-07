"""OrchestratorOps: one implementation behind the stdio tools and `/rpc`."""

import inspect
import json
import sqlite3
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager, closing
from dataclasses import replace
from datetime import UTC, datetime
from itertools import count
from pathlib import Path
from typing import Any, cast
from uuid import UUID

import httpx
import pytest
from agent_hub.mcp import create_mcp
from agent_hub.merge_gate import Check, CiStatus, GateReport, Mergeable, MergeGate, PrState
from agent_hub.orchestrator import OPERATIONS, OrchestratorOps
from agent_hub.store import MAX_CHECK_NAME_CHARS, HubStore
from agent_hub_common import MAX_MESSAGE_PART_BYTES, AgentProfile, TaskState
from conftest import rpc
from fastapi import FastAPI
from mcp import ClientSession
from mcp.shared.memory import create_connected_server_and_client_session

FIXTURE = Path(__file__).parent / "fixtures" / "orchestrator-tools.json"
PR = "https://github.com/octo/sandbox/pull/7"
HEAD = "0123456789abcdef0123456789abcdef01234567"
NOW = datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC)


async def test_recording_caps_check_names_without_changing_response(store: HubStore) -> None:
    name = "x" * (MAX_CHECK_NAME_CHARS + 100)

    class LongNameGate(StubGate):
        async def check(self, pr_url: str, expected_head_sha: str) -> GateReport:
            report = await super().check(pr_url, expected_head_sha)
            return replace(report, checks=[Check(name, "pass", "https://example.com")])

    result = await OrchestratorOps(store, LongNameGate()).check_merge_gate(PR, HEAD)
    assert result["checks"][0]["name"] == name
    with sqlite3.connect(store.path) as connection:
        [checks] = connection.execute("SELECT checks_json FROM gate_reading").fetchone()
    assert json.loads(checks) == [{"name": name[:MAX_CHECK_NAME_CHARS], "bucket": "pass"}]


@pytest.mark.parametrize(
    ("error", "code"), [(ValueError("bad URL"), -32602), (RuntimeError("unexpected"), -32603)]
)
async def test_other_gate_errors_are_recorded(
    store: HubStore,
    error: Exception,
    code: int,
) -> None:
    class BrokenGate(StubGate):
        async def check(self, pr_url: str, expected_head_sha: str) -> GateReport:
            raise error

    with pytest.raises(type(error)) as raised:
        await OrchestratorOps(store, BrokenGate()).check_merge_gate(PR, HEAD)
    assert raised.value is error
    with sqlite3.connect(store.path) as connection:
        [recorded] = connection.execute("SELECT error_code FROM gate_reading").fetchone()
    assert recorded == code


class StubGate:
    """A merge gate that answers without `gh`, identically on every call."""

    async def check(self, pr_url: str, expected_head_sha: str) -> GateReport:
        return GateReport(
            pr_url=pr_url,
            pr_state=PrState.OPEN,
            expected_head_sha=expected_head_sha,
            current_head_sha=expected_head_sha,
            head_matches=True,
            ci=CiStatus.PASS,
            checks=[],
            base_ref="main",
            base_sha=HEAD,
            main_sha=HEAD,
            base_behind_main=False,
            mergeable=Mergeable.CLEAN,
            merge_state_status="CLEAN",
        )


async def test_the_tool_list_is_unchanged_by_the_move(tmp_path: Path) -> None:
    # Tool schemas are part of Alice's token budget (§11): the move to
    # OrchestratorOps must not change a byte of what tools/list serves.
    tools = await create_mcp(HubStore(tmp_path / "hub.db")).list_tools()

    served = [tool.model_dump(mode="json", by_alias=True, exclude_none=True) for tool in tools]

    assert served == json.loads(FIXTURE.read_text(encoding="utf-8"))


def test_operations_are_exactly_the_public_methods() -> None:
    public = {
        name
        for name, member in inspect.getmembers(OrchestratorOps, inspect.iscoroutinefunction)
        if not name.startswith("_")
    }

    assert public == set(OPERATIONS)
    assert len(OPERATIONS) == 11


# -- parity: the same operation over stdio MCP and over /rpc -----------------


def checked_in(store: HubStore, name: str) -> int:
    """Register a worker and lease its check-in event; return the event id."""

    store.check_in(name, AgentProfile(capabilities=("python",)))
    event = store.lease_next_event()
    assert event is not None
    return event.id


async def working_task(store: HubStore, name: str) -> str:
    event_id = checked_in(store, name)
    agent = store.agent_by_name(name)
    assert agent is not None
    task = store.assign_task(
        name, "implementer", "Fix", "Do it", 30, None, source_event_id=event_id
    )
    assert await store.await_assignment(agent.context_id, 0) is not None
    return task.id


async def get_state(store: HubStore) -> dict[str, Any]:
    await working_task(store, "bob")
    return {}


async def initialize_workflow(store: HubStore) -> dict[str, Any]:
    return {"goal": store.get_state()["workflow"]["goal"]}


async def initialize_workflow_conflict(store: HubStore) -> dict[str, Any]:
    return {"goal": "A different goal"}


async def wait_for_event(store: HubStore) -> dict[str, Any]:
    store.check_in("bob", AgentProfile())
    return {"timeout_s": 0}


async def wait_for_event_ack(store: HubStore) -> dict[str, Any]:
    store.check_in("bob", AgentProfile())
    event = store.lease_next_event()
    assert event is not None
    return {"timeout_s": 0, "ack": event.delivery_id}


async def assign_task(store: HubStore) -> dict[str, Any]:
    event_id = checked_in(store, "bob")
    return {
        "agent": "bob",
        "role": "reviewer",
        "title": "Review",
        "instructions": "Read it",
        "event_id": event_id,
        "lease_min": 15,
        "pr_head_sha": HEAD,
    }


async def assign_task_unknown_agent(store: HubStore) -> dict[str, Any]:
    return {**await assign_task(store), "agent": "nobody"}


async def assign_task_too_large(store: HubStore) -> dict[str, Any]:
    args = await assign_task(store)
    return {**args, "instructions": "x" * MAX_MESSAGE_PART_BYTES}


async def check_merge_gate(store: HubStore) -> dict[str, Any]:
    return {"pr_url": PR, "expected_head_sha": HEAD}


async def reply(store: HubStore) -> dict[str, Any]:
    task_id = await working_task(store, "bob")
    message_id = store.open_question(task_id, "bob", "Which?", "q1")
    return {"task_id": task_id, "text": "This one", "message_id": message_id}


async def reply_unknown_task(store: HubStore) -> dict[str, Any]:
    return {"task_id": "missing", "text": "hi", "message_id": 1}


async def set_task_state(store: HubStore) -> dict[str, Any]:
    return {"task_id": await working_task(store, "bob"), "state": "canceled", "note": "Stop"}


async def set_task_state_terminal(store: HubStore) -> dict[str, Any]:
    args = await set_task_state(store)
    store.set_task_state(args["task_id"], TaskState.FAILED, "Earlier")
    return args


async def release_agent(store: HubStore) -> dict[str, Any]:
    checked_in(store, "bob")
    return {"agent": "bob"}


async def release_agent_unknown(store: HubStore) -> dict[str, Any]:
    return {"agent": "nobody"}


async def set_workflow_status(store: HubStore) -> dict[str, Any]:
    return {"status": "paused", "summary": "Waiting on the operator"}


async def log_decision(store: HubStore) -> dict[str, Any]:
    return {"summary": "Decision", "rationale": "Because", "key": "k1"}


async def ask_user(store: HubStore) -> dict[str, Any]:
    return {"question": "Merge or wait?", "options": ["merge", "wait"]}


async def ask_user_too_large(store: HubStore) -> dict[str, Any]:
    return {"question": "x" * MAX_MESSAGE_PART_BYTES}


Prepare = Callable[[HubStore], Any]
OK, FAILS = False, True
# (operation, prepare, whether it fails): every operation succeeds at least
# once, and the failures check that /rpc carries the store's error text.
CASES: list[tuple[str, Prepare, bool]] = [
    ("get_state", get_state, OK),
    ("initialize_workflow", initialize_workflow, OK),
    ("initialize_workflow", initialize_workflow_conflict, FAILS),
    ("wait_for_event", wait_for_event, OK),
    ("wait_for_event", wait_for_event_ack, OK),
    ("assign_task", assign_task, OK),
    ("assign_task", assign_task_unknown_agent, FAILS),
    ("assign_task", assign_task_too_large, FAILS),
    ("check_merge_gate", check_merge_gate, OK),
    ("reply", reply, OK),
    ("reply", reply_unknown_task, FAILS),
    ("set_task_state", set_task_state, OK),
    ("set_task_state", set_task_state_terminal, FAILS),
    ("release_agent", release_agent, OK),
    ("release_agent", release_agent_unknown, FAILS),
    ("set_workflow_status", set_workflow_status, OK),
    ("log_decision", log_decision, OK),
    ("ask_user", ask_user, OK),
    ("ask_user", ask_user_too_large, FAILS),
]


def test_the_parity_cases_cover_every_operation() -> None:
    assert {name for name, _, fails in CASES if not fails} == set(OPERATIONS)


@pytest.fixture
def deterministic(hub_store: HubStore, monkeypatch: pytest.MonkeyPatch) -> Callable[[], None]:
    """Fix the store's clock and ids; the returned callable restarts the ids."""

    counter = count(1)

    def restart() -> None:
        nonlocal counter
        counter = count(1)

    monkeypatch.setattr("agent_hub.store.uuid4", lambda: UUID(int=next(counter)))
    hub_store.clock = lambda: NOW
    return restart


@asynccontextmanager
async def alice_mcp(app: FastAPI, hub_store: HubStore) -> AsyncIterator[ClientSession]:
    """The in-process stdio tools, over the app's own store and gate.

    Not a fixture: the session's task group must exit in the task it entered.
    """

    gate = cast(MergeGate, StubGate())
    cast(OrchestratorOps, app.state.orchestrator).gate = gate
    server = create_mcp(hub_store, gate=gate)
    async with create_connected_server_and_client_session(server._mcp_server) as session:
        yield session


def snapshot(path: Path) -> sqlite3.Connection:
    copy = sqlite3.connect(":memory:")
    with closing(sqlite3.connect(path)) as source:
        source.backup(copy)
    return copy


def restore(copy: sqlite3.Connection, path: Path) -> None:
    with closing(sqlite3.connect(path)) as target:
        copy.backup(target)


@pytest.mark.parametrize(
    ("operation", "prepare", "fails"), CASES, ids=[prepare.__name__ for _, prepare, _ in CASES]
)
async def test_rpc_and_mcp_return_the_same_result(
    operation: str,
    prepare: Prepare,
    fails: bool,
    app: FastAPI,
    hub_store: HubStore,
    client: httpx.AsyncClient,
    deterministic: Callable[[], None],
) -> None:
    # Both transports run the operation from the same database state with the
    # same clock and ids, so any difference is the transport's.
    args = await prepare(hub_store)

    with closing(snapshot(hub_store.path)) as before:
        deterministic()
        async with alice_mcp(app, hub_store) as session:
            over_mcp = await session.call_tool(operation, args)
        restore(before, hub_store.path)
    deterministic()
    over_rpc = (await client.post("/rpc", json=rpc(operation, args))).json()

    assert over_mcp.isError is fails
    if fails:
        text = over_mcp.content[0].text  # type: ignore[union-attr]
        assert text == f"Error executing tool {operation}: {over_rpc['error']['message']}"
    else:
        assert over_rpc["result"] == over_mcp.structuredContent
