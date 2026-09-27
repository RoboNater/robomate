"""In-process schema parity for the bridge; transport tests live in test_orchestrator_bridge."""

import asyncio
from pathlib import Path
from typing import Any

import pytest
from agent_hub.database import database, initialize_database
from agent_hub.mcp import create_mcp
from agent_hub.store import HubStore
from agent_hub_common import MAX_MESSAGE_PART_BYTES, AgentProfile

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
