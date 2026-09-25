from typing import Any
from uuid import uuid4

import httpx
import pytest
from agent_hub.database import database
from agent_hub.store import HubStore
from agent_hub_common import (
    SCHEMA_VERSION,
    AgentProfile,
    AgentStatus,
    EventKind,
    Finding,
    ImplementerOutcome,
    ImplementerResult,
    MetaKeys,
    ReviewerResult,
    ReviewerVerdict,
    TaskState,
    TestResult,
)


def _rpc(
    method: str,
    params: dict[str, Any],
    req_id: str | None = None,
) -> dict[str, Any]:
    message = params.get("message")
    if isinstance(message, dict):
        metadata = message.setdefault("metadata", {})
        if isinstance(metadata, dict):
            metadata.setdefault(MetaKeys.WORKER_INSTANCE_ID, "test-worker-instance")
    return {
        "jsonrpc": "2.0",
        "id": req_id or uuid4().hex,
        "method": method,
        "params": params,
    }


async def test_check_in_idempotency_and_conflict(
    client: httpx.AsyncClient, hub_store: HubStore
) -> None:
    op_id = "op-checkin-test-1"
    payload = _rpc(
        "message/send",
        {
            "message": {
                "messageId": "msg-1",
                "role": "user",
                "parts": [{"kind": "text", "text": "READY"}],
                "metadata": {
                    MetaKeys.AGENT: "bob",
                    MetaKeys.CAPABILITIES: ["python"],
                    MetaKeys.HARNESS: "claude-code",
                    MetaKeys.SCHEMA_VERSION: SCHEMA_VERSION,
                    MetaKeys.OPERATION_ID: op_id,
                },
            }
        },
    )

    # First call: succeeds
    resp1 = await client.post("/a2a", json=payload)
    assert resp1.status_code == 200
    res1 = resp1.json()["result"]
    assert res1.get("contextId") is not None

    # Verify 1 event in database
    with database(hub_store.path) as connection:
        events = connection.execute(
            "SELECT count(*) as c FROM event WHERE kind = ?",
            (EventKind.AGENT_CHECKED_IN.value,),
        ).fetchone()
        assert events["c"] == 1

    # Replay identical call: should return cached result without duplicate events
    resp2 = await client.post("/a2a", json=payload)
    assert resp2.status_code == 200
    res2 = resp2.json()["result"]
    assert res2 == res1

    with database(hub_store.path) as connection:
        events = connection.execute(
            "SELECT count(*) as c FROM event WHERE kind = ?",
            (EventKind.AGENT_CHECKED_IN.value,),
        ).fetchone()
        assert events["c"] == 1

    # Conflicting call: same operation_id but different capabilities
    conflicting_payload = _rpc(
        "message/send",
        {
            "message": {
                "messageId": "msg-2",
                "role": "user",
                "parts": [{"kind": "text", "text": "READY"}],
                "metadata": {
                    MetaKeys.AGENT: "bob",
                    MetaKeys.CAPABILITIES: ["go", "rust"],
                    MetaKeys.HARNESS: "claude-code",
                    MetaKeys.SCHEMA_VERSION: SCHEMA_VERSION,
                    MetaKeys.OPERATION_ID: op_id,
                },
            }
        },
    )
    resp3 = await client.post("/a2a", json=conflicting_payload)
    assert resp3.status_code == 409
    err3 = resp3.json()["error"]
    assert err3["code"] == -32600
    assert "already executed" in err3["message"]


async def test_progress_idempotency_and_conflict(
    client: httpx.AsyncClient, hub_store: HubStore
) -> None:
    # Check in bob
    hub_store.check_in(
        "bob",
        AgentProfile(harness="claude-code", capabilities=("python",)),
        worker_instance_id="test-worker-instance",
    )
    agent = hub_store.agent_by_name("bob")
    assert agent is not None
    task = hub_store.assign_task("bob", "implementer", "Task 1", "Instructions")

    op_id = "op-progress-test-1"
    payload = _rpc(
        "message/send",
        {
            "message": {
                "messageId": "msg-p1",
                "taskId": task.id,
                "contextId": agent.context_id,
                "role": "user",
                "parts": [{"kind": "text", "text": "Starting tests"}],
                "metadata": {
                    MetaKeys.KIND: "progress",
                    MetaKeys.SCHEMA_VERSION: SCHEMA_VERSION,
                    MetaKeys.OPERATION_ID: op_id,
                },
            }
        },
    )

    # First call: succeeds
    resp1 = await client.post("/a2a", json=payload)
    assert resp1.status_code == 200

    # Verify event count in database
    with database(hub_store.path) as connection:
        events = connection.execute(
            "SELECT count(*) as c FROM event WHERE kind = ?",
            (EventKind.TASK_PROGRESS.value,),
        ).fetchone()
        assert events["c"] == 1

    # Replay identical call: returns cached result, no extra event
    resp2 = await client.post("/a2a", json=payload)
    assert resp2.status_code == 200

    with database(hub_store.path) as connection:
        events = connection.execute(
            "SELECT count(*) as c FROM event WHERE kind = ?",
            (EventKind.TASK_PROGRESS.value,),
        ).fetchone()
        assert events["c"] == 1

    # Conflicting call: same operation_id but different note
    conflicting_payload = _rpc(
        "message/send",
        {
            "message": {
                "messageId": "msg-p2",
                "taskId": task.id,
                "contextId": agent.context_id,
                "role": "user",
                "parts": [{"kind": "text", "text": "Different progress note"}],
                "metadata": {
                    MetaKeys.KIND: "progress",
                    MetaKeys.SCHEMA_VERSION: SCHEMA_VERSION,
                    MetaKeys.OPERATION_ID: op_id,
                },
            }
        },
    )
    resp3 = await client.post("/a2a", json=conflicting_payload)
    assert resp3.status_code == 409
    err3 = resp3.json()["error"]
    assert err3["code"] == -32600
    assert "already executed" in err3["message"]


async def test_result_idempotency_and_conflict(
    client: httpx.AsyncClient, hub_store: HubStore
) -> None:
    hub_store.check_in(
        "bob",
        AgentProfile(harness="claude-code", capabilities=("python",)),
        worker_instance_id="test-worker-instance",
    )
    agent = hub_store.agent_by_name("bob")
    assert agent is not None
    task = hub_store.assign_task("bob", "implementer", "Task 1", "Instructions")

    op_id = "op-result-test-1"
    result_dict = {
        "outcome": "completed",
        "summary": "Done!",
        "pr_url": "https://github.com/org/repo/pull/1",
        "head_sha": "0123456789abcdef0123456789abcdef01234567",
    }
    payload = _rpc(
        "message/send",
        {
            "message": {
                "messageId": "msg-r1",
                "taskId": task.id,
                "contextId": agent.context_id,
                "role": "user",
                "parts": [{"kind": "text", "text": "Done!"}],
                "metadata": {
                    MetaKeys.KIND: "result",
                    MetaKeys.SCHEMA_VERSION: SCHEMA_VERSION,
                    MetaKeys.OPERATION_ID: op_id,
                    MetaKeys.RESULT: result_dict,
                },
            }
        },
    )

    # First call: succeeds
    resp1 = await client.post("/a2a", json=payload)
    assert resp1.status_code == 200

    stored = hub_store.get_task(task.id)
    assert stored is not None and stored.state == TaskState.COMPLETED

    # Verify event count
    with database(hub_store.path) as connection:
        events = connection.execute(
            "SELECT count(*) as c FROM event WHERE kind = ?",
            (EventKind.TASK_COMPLETED.value,),
        ).fetchone()
        assert events["c"] == 1

    # Replay identical call: returns cached result, no duplicate event
    resp2 = await client.post("/a2a", json=payload)
    assert resp2.status_code == 200

    with database(hub_store.path) as connection:
        events = connection.execute(
            "SELECT count(*) as c FROM event WHERE kind = ?",
            (EventKind.TASK_COMPLETED.value,),
        ).fetchone()
        assert events["c"] == 1

    # Conflicting call: same operation_id but different summary
    conflicting_result = dict(result_dict, summary="Different summary")
    conflicting_payload = _rpc(
        "message/send",
        {
            "message": {
                "messageId": "msg-r2",
                "taskId": task.id,
                "contextId": agent.context_id,
                "role": "user",
                "parts": [{"kind": "text", "text": "Different summary"}],
                "metadata": {
                    MetaKeys.KIND: "result",
                    MetaKeys.SCHEMA_VERSION: SCHEMA_VERSION,
                    MetaKeys.OPERATION_ID: op_id,
                    MetaKeys.RESULT: conflicting_result,
                },
            }
        },
    )
    resp3 = await client.post("/a2a", json=conflicting_payload)
    assert resp3.status_code == 409
    err3 = resp3.json()["error"]
    assert err3["code"] == -32600
    assert "already executed" in err3["message"]


async def test_validation_failure_leaves_task_working(
    client: httpx.AsyncClient, hub_store: HubStore
) -> None:
    # 1. Reviewer validation failures
    hub_store.check_in(
        "charlie",
        AgentProfile(harness="codex", capabilities=("python",)),
        worker_instance_id="test-worker-instance",
    )
    agent = hub_store.agent_by_name("charlie")
    assert agent is not None
    rev_task = hub_store.assign_task("charlie", "reviewer", "Review PR", "Review instructions")

    # Approved without reviewed_head_sha -> HTTP 400
    bad_reviewer_1 = {
        "verdict": "approved",
        "summary": "Looks good without sha",
    }
    resp = await client.post(
        "/a2a",
        json=_rpc(
            "message/send",
            {
                "message": {
                    "messageId": uuid4().hex,
                    "taskId": rev_task.id,
                    "contextId": agent.context_id,
                    "role": "user",
                    "parts": [{"kind": "text", "text": "approved"}],
                    "metadata": {
                        MetaKeys.KIND: "result",
                        MetaKeys.SCHEMA_VERSION: SCHEMA_VERSION,
                        MetaKeys.OPERATION_ID: uuid4().hex,
                        MetaKeys.RESULT: bad_reviewer_1,
                    },
                }
            },
        ),
    )
    assert resp.status_code == 400
    assert "requires reviewed_head_sha" in resp.json()["error"]["message"]

    # Task remains open (not completed or failed)
    task_after = hub_store.get_task(rev_task.id)
    assert task_after is not None and task_after.state not in (
        TaskState.COMPLETED,
        TaskState.FAILED,
    )

    # Approved with blocking findings -> HTTP 400
    bad_reviewer_2 = {
        "verdict": "approved",
        "summary": "Approved with blocker",
        "reviewed_head_sha": "0123456789abcdef0123456789abcdef01234567",
        "blocking_findings": [
            {
                "id": "r1-1",
                "text": "Critical bug in main loop",
            }
        ],
    }
    resp = await client.post(
        "/a2a",
        json=_rpc(
            "message/send",
            {
                "message": {
                    "messageId": uuid4().hex,
                    "taskId": rev_task.id,
                    "contextId": agent.context_id,
                    "role": "user",
                    "parts": [{"kind": "text", "text": "approved"}],
                    "metadata": {
                        MetaKeys.KIND: "result",
                        MetaKeys.SCHEMA_VERSION: SCHEMA_VERSION,
                        MetaKeys.OPERATION_ID: uuid4().hex,
                        MetaKeys.RESULT: bad_reviewer_2,
                    },
                }
            },
        ),
    )
    assert resp.status_code == 400
    assert "blocking_findings to be empty" in resp.json()["error"]["message"]

    # 2. Implementer validation failure
    hub_store.check_in(
        "bob",
        AgentProfile(harness="claude-code", capabilities=("python",)),
        worker_instance_id="test-worker-instance",
    )
    bob_agent = hub_store.agent_by_name("bob")
    assert bob_agent is not None
    imp_task = hub_store.assign_task("bob", "implementer", "Fix bug", "Fix instructions")

    bad_implementer = {
        "outcome": "completed",
        "summary": "Completed without PR URL",
    }
    resp = await client.post(
        "/a2a",
        json=_rpc(
            "message/send",
            {
                "message": {
                    "messageId": uuid4().hex,
                    "taskId": imp_task.id,
                    "contextId": bob_agent.context_id,
                    "role": "user",
                    "parts": [{"kind": "text", "text": "done"}],
                    "metadata": {
                        MetaKeys.KIND: "result",
                        MetaKeys.SCHEMA_VERSION: SCHEMA_VERSION,
                        MetaKeys.OPERATION_ID: uuid4().hex,
                        MetaKeys.RESULT: bad_implementer,
                    },
                }
            },
        ),
    )
    assert resp.status_code == 400
    assert "requires pr_url" in resp.json()["error"]["message"]

    # Correct implementer submission succeeds
    good_implementer = {
        "outcome": "completed",
        "summary": "Completed with PR",
        "pr_url": "https://github.com/org/repo/pull/42",
        "head_sha": "0123456789abcdef0123456789abcdef01234567",
    }
    resp = await client.post(
        "/a2a",
        json=_rpc(
            "message/send",
            {
                "message": {
                    "messageId": uuid4().hex,
                    "taskId": imp_task.id,
                    "contextId": bob_agent.context_id,
                    "role": "user",
                    "parts": [{"kind": "text", "text": "done"}],
                    "metadata": {
                        MetaKeys.KIND: "result",
                        MetaKeys.SCHEMA_VERSION: SCHEMA_VERSION,
                        MetaKeys.OPERATION_ID: uuid4().hex,
                        MetaKeys.RESULT: good_implementer,
                    },
                }
            },
        ),
    )
    assert resp.status_code == 200
    task_after = hub_store.get_task(imp_task.id)
    assert task_after is not None and task_after.state == TaskState.COMPLETED


@pytest.mark.parametrize(
    "bad_version",
    [
        2,
        -1,
        0,
        "1",
        True,
        False,
        None,
    ],
)
async def test_schema_version_rejections(client: httpx.AsyncClient, bad_version: Any) -> None:
    # Check that invalid schema_version (when provided) is rejected with 400
    if bad_version is None:
        # None or omitted is valid for backwards compatibility
        return

    payload = _rpc(
        "message/send",
        {
            "message": {
                "messageId": uuid4().hex,
                "role": "user",
                "parts": [{"kind": "text", "text": "READY"}],
                "metadata": {
                    MetaKeys.AGENT: "bob",
                    MetaKeys.CAPABILITIES: ["python"],
                    MetaKeys.SCHEMA_VERSION: bad_version,
                    MetaKeys.OPERATION_ID: uuid4().hex,
                },
            }
        },
    )
    resp = await client.post("/a2a", json=payload)
    assert resp.status_code == 400
    err = resp.json()["error"]
    assert err["code"] == -32602
    assert "unsupported schema_version" in err["message"]


async def test_typed_result_roundtrip_a2a_to_get_state(
    client: httpx.AsyncClient, hub_store: HubStore
) -> None:
    hub_store.check_in(
        "bob",
        AgentProfile(harness="claude-code", capabilities=("python",)),
        worker_instance_id="test-worker-instance",
    )
    agent = hub_store.agent_by_name("bob")
    assert agent is not None
    task = hub_store.assign_task("bob", "implementer", "Task Implementer", "Inst")

    imp_result = ImplementerResult(
        outcome=ImplementerOutcome.COMPLETED,
        summary="All green",
        pr_url="https://github.com/org/repo/pull/77",
        head_sha="0123456789abcdef0123456789abcdef01234567",
        commits=["0123456789abcdef0123456789abcdef01234567"],
        tests=[
            TestResult(command="pytest tests/test_models.py", status="passed"),
            TestResult(command="pytest tests/test_protocol.py", status="passed"),
        ],
        resolved_finding_ids=["r1-1", "r1-2"],
    )

    resp = await client.post(
        "/a2a",
        json=_rpc(
            "message/send",
            {
                "message": {
                    "messageId": uuid4().hex,
                    "taskId": task.id,
                    "contextId": agent.context_id,
                    "role": "user",
                    "parts": [{"kind": "text", "text": "All green"}],
                    "metadata": {
                        MetaKeys.KIND: "result",
                        MetaKeys.SCHEMA_VERSION: SCHEMA_VERSION,
                        MetaKeys.OPERATION_ID: uuid4().hex,
                        MetaKeys.RESULT: imp_result.model_dump(mode="json"),
                    },
                }
            },
        ),
    )
    assert resp.status_code == 200

    # 1. Verify in database
    task_row = hub_store.get_task(task.id)
    assert task_row is not None
    assert task_row.result is not None
    assert task_row.result["pr_url"] == "https://github.com/org/repo/pull/77"
    assert task_row.result["resolved_finding_ids"] == ["r1-1", "r1-2"]
    assert len(task_row.result["tests"]) == 2
    assert task_row.result["tests"][0]["command"] == "pytest tests/test_models.py"

    # 2. Verify in get_state
    state = hub_store.get_state()
    tasks = state.get("tasks", [])
    matching = [t for t in tasks if t["id"] == task.id]
    assert len(matching) == 1
    assert matching[0]["result"]["head_sha"] == "0123456789abcdef0123456789abcdef01234567"
    assert matching[0]["result"]["resolved_finding_ids"] == ["r1-1", "r1-2"]

    # 3. Test ReviewerResult with findings
    hub_store.check_in(
        "charlie",
        AgentProfile(harness="codex", capabilities=("python",)),
        worker_instance_id="test-worker-instance",
    )
    charlie_agent = hub_store.agent_by_name("charlie")
    assert charlie_agent is not None
    rev_task = hub_store.assign_task("charlie", "reviewer", "Task Review", "Inst")

    rev_result = ReviewerResult(
        verdict=ReviewerVerdict.CHANGES_REQUESTED,
        summary="Changes needed in parser",
        blocking_findings=[
            Finding(
                id="r2-1",
                text="Unchecked None dereference in parser.py:42",
            ),
        ],
        nonblocking_findings=[
            Finding(
                id="r2-2",
                text="Typo in README.md",
            ),
        ],
    )

    resp = await client.post(
        "/a2a",
        json=_rpc(
            "message/send",
            {
                "message": {
                    "messageId": uuid4().hex,
                    "taskId": rev_task.id,
                    "contextId": charlie_agent.context_id,
                    "role": "user",
                    "parts": [{"kind": "text", "text": "Changes requested"}],
                    "metadata": {
                        MetaKeys.KIND: "result",
                        MetaKeys.SCHEMA_VERSION: SCHEMA_VERSION,
                        MetaKeys.OPERATION_ID: uuid4().hex,
                        MetaKeys.RESULT: rev_result.model_dump(mode="json"),
                    },
                }
            },
        ),
    )
    assert resp.status_code == 200

    rev_row = hub_store.get_task(rev_task.id)
    assert rev_row is not None
    assert rev_row.result is not None
    assert rev_row.result["verdict"] == "changes_requested"
    assert len(rev_row.result["blocking_findings"]) == 1
    assert rev_row.result["blocking_findings"][0]["id"] == "r2-1"
    assert len(rev_row.result["nonblocking_findings"]) == 1
    assert rev_row.result["nonblocking_findings"][0]["id"] == "r2-2"


async def test_missing_schema_version_and_operation_id_rejected_with_400(
    client: httpx.AsyncClient, hub_store: HubStore
) -> None:
    # 1. Check-in missing schema_version
    resp = await client.post(
        "/a2a",
        json=_rpc(
            "message/send",
            {
                "message": {
                    "messageId": uuid4().hex,
                    "role": "user",
                    "parts": [{"kind": "text", "text": "READY"}],
                    "metadata": {
                        MetaKeys.AGENT: "bob",
                        MetaKeys.CAPABILITIES: ["python"],
                        MetaKeys.OPERATION_ID: uuid4().hex,
                    },
                }
            },
        ),
    )
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == -32602
    assert "schema_version is required" in resp.json()["error"]["message"]

    # 2. Check-in missing operation_id
    resp = await client.post(
        "/a2a",
        json=_rpc(
            "message/send",
            {
                "message": {
                    "messageId": uuid4().hex,
                    "role": "user",
                    "parts": [{"kind": "text", "text": "READY"}],
                    "metadata": {
                        MetaKeys.AGENT: "bob",
                        MetaKeys.CAPABILITIES: ["python"],
                        MetaKeys.SCHEMA_VERSION: SCHEMA_VERSION,
                    },
                }
            },
        ),
    )
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == -32602
    assert "operation_id is required" in resp.json()["error"]["message"]

    # Now valid check-in
    hub_store.check_in(
        "bob", AgentProfile(capabilities=("python",)), worker_instance_id="test-worker-instance"
    )
    agent = hub_store.agent_by_name("bob")
    assert agent is not None
    task = hub_store.assign_task("bob", "implementer", "T1", "Inst")

    # 3. Progress missing schema_version
    resp = await client.post(
        "/a2a",
        json=_rpc(
            "message/send",
            {
                "message": {
                    "messageId": uuid4().hex,
                    "taskId": task.id,
                    "contextId": agent.context_id,
                    "role": "user",
                    "parts": [{"kind": "text", "text": "working"}],
                    "metadata": {
                        MetaKeys.KIND: "progress",
                        MetaKeys.OPERATION_ID: uuid4().hex,
                    },
                }
            },
        ),
    )
    assert resp.status_code == 400
    assert "schema_version is required" in resp.json()["error"]["message"]

    # 4. Progress missing operation_id
    resp = await client.post(
        "/a2a",
        json=_rpc(
            "message/send",
            {
                "message": {
                    "messageId": uuid4().hex,
                    "taskId": task.id,
                    "contextId": agent.context_id,
                    "role": "user",
                    "parts": [{"kind": "text", "text": "working"}],
                    "metadata": {
                        MetaKeys.KIND: "progress",
                        MetaKeys.SCHEMA_VERSION: SCHEMA_VERSION,
                    },
                }
            },
        ),
    )
    assert resp.status_code == 400
    assert "operation_id is required" in resp.json()["error"]["message"]

    # 5. Result missing schema_version
    resp = await client.post(
        "/a2a",
        json=_rpc(
            "message/send",
            {
                "message": {
                    "messageId": uuid4().hex,
                    "taskId": task.id,
                    "contextId": agent.context_id,
                    "role": "user",
                    "parts": [{"kind": "text", "text": "done"}],
                    "metadata": {
                        MetaKeys.KIND: "result",
                        MetaKeys.OPERATION_ID: uuid4().hex,
                        MetaKeys.RESULT: {
                            "outcome": "completed",
                            "summary": "done",
                            "pr_url": "https://github.com/org/repo/pull/1",
                            "head_sha": "0123456789abcdef0123456789abcdef01234567",
                        },
                    },
                }
            },
        ),
    )
    assert resp.status_code == 400
    assert "schema_version is required" in resp.json()["error"]["message"]

    # 6. Result missing operation_id
    resp = await client.post(
        "/a2a",
        json=_rpc(
            "message/send",
            {
                "message": {
                    "messageId": uuid4().hex,
                    "taskId": task.id,
                    "contextId": agent.context_id,
                    "role": "user",
                    "parts": [{"kind": "text", "text": "done"}],
                    "metadata": {
                        MetaKeys.KIND: "result",
                        MetaKeys.SCHEMA_VERSION: SCHEMA_VERSION,
                        MetaKeys.RESULT: {
                            "outcome": "completed",
                            "summary": "done",
                            "pr_url": "https://github.com/org/repo/pull/1",
                            "head_sha": "0123456789abcdef0123456789abcdef01234567",
                        },
                    },
                }
            },
        ),
    )
    assert resp.status_code == 400
    assert "operation_id is required" in resp.json()["error"]["message"]

    # 7. Result missing hub.result (with legacy hub.status) -> HTTP 400 (no fallback)
    resp = await client.post(
        "/a2a",
        json=_rpc(
            "message/send",
            {
                "message": {
                    "messageId": uuid4().hex,
                    "taskId": task.id,
                    "contextId": agent.context_id,
                    "role": "user",
                    "parts": [{"kind": "text", "text": "done"}],
                    "metadata": {
                        MetaKeys.KIND: "result",
                        MetaKeys.SCHEMA_VERSION: SCHEMA_VERSION,
                        MetaKeys.OPERATION_ID: uuid4().hex,
                        MetaKeys.STATUS: "completed",
                    },
                }
            },
        ),
    )
    assert resp.status_code == 400
    assert "result is required" in resp.json()["error"]["message"]


async def test_atomic_rollback_on_failure(client: httpx.AsyncClient, hub_store: HubStore) -> None:
    # Check-in failure rollback: response_builder throws exception
    op_id = "op-rollback-checkin-1"
    payload_hash = "hash-rollback-1"

    def fail_builder(*args: Any) -> Any:
        raise RuntimeError("Simulated builder failure")

    with pytest.raises(RuntimeError, match="Simulated builder failure"):
        hub_store.check_in(
            "dave",
            AgentProfile(capabilities=("python",)),
            operation_id=op_id,
            payload_hash=payload_hash,
            response_builder=fail_builder,
        )

    # Verify atomic rollback: no agent, no message, no event, no operation record
    assert hub_store.agent_by_name("dave") is None
    with database(hub_store.path) as conn:
        assert (
            conn.execute("SELECT count(*) as c FROM agent WHERE name = 'dave'").fetchone()["c"] == 0
        )
        assert (
            conn.execute("SELECT count(*) as c FROM message WHERE sender = 'dave'").fetchone()["c"]
            == 0
        )
        assert (
            conn.execute(
                "SELECT count(*) as c FROM operation WHERE operation_id = ?", (op_id,)
            ).fetchone()["c"]
            == 0
        )

    # Submit result failure rollback
    hub_store.check_in("dave", AgentProfile(capabilities=("python",)))
    dave = hub_store.agent_by_name("dave")
    assert dave is not None
    task = hub_store.assign_task("dave", "implementer", "T1", "Inst")

    res = ImplementerResult(
        outcome=ImplementerOutcome.COMPLETED,
        summary="Done",
        pr_url="https://github.com/org/repo/pull/1",
        head_sha="0123456789abcdef0123456789abcdef01234567",
    )
    res_op_id = "op-rollback-res-1"

    def fail_result_builder(*args: Any) -> Any:
        raise RuntimeError("Simulated result failure")

    with pytest.raises(RuntimeError, match="Simulated result failure"):
        hub_store.submit_result(
            task.id,
            "dave",
            res,
            operation_id=res_op_id,
            payload_hash="res-hash-1",
            response_builder=fail_result_builder,
        )

    # Verify task remains SUBMITTED (not transitioned to COMPLETED) and no operation record exists
    task_after = hub_store.get_task(task.id)
    assert task_after is not None and task_after.state == TaskState.SUBMITTED
    with database(hub_store.path) as conn:
        assert (
            conn.execute(
                "SELECT count(*) as c FROM operation WHERE operation_id = ?", (res_op_id,)
            ).fetchone()["c"]
            == 0
        )


async def test_worker_client_ambiguous_timeout_and_id_reuse(
    client: httpx.AsyncClient, hub_store: HubStore
) -> None:
    from unittest.mock import patch

    from worker_mcp.client import WorkerHubClient
    from worker_mcp.config import WorkerSettings

    settings = WorkerSettings(
        agent_name="bob",
        hub_url="http://hub.test",
        token="test-token",
        profile=AgentProfile(harness="claude-code"),
        max_retries=2,
        backoff_factor_s=0.01,
    )

    worker = WorkerHubClient(settings, http_client=client)

    # 1. Test ambiguous timeout in _request_with_retry automatically retrying same op_id
    original_request = client.request
    call_count = 0
    captured_payloads: list[dict[str, Any]] = []

    async def mock_request_with_timeout(*args: Any, **kwargs: Any) -> httpx.Response:
        nonlocal call_count
        call_count += 1
        json_body = kwargs.get("json")
        if json_body:
            captured_payloads.append(json_body)
        if call_count == 1:
            raise httpx.ReadTimeout("Read timed out")
        return await original_request(*args, **kwargs)

    with patch.object(client, "request", side_effect=mock_request_with_timeout):
        reg = await worker.check_in()
        assert reg["status"] == "registered"

    # Verify 2 attempts were made with identical messageId and hub.operation_id
    assert call_count == 2
    assert len(captured_payloads) == 2
    meta1 = captured_payloads[0]["params"]["message"]["metadata"]
    meta2 = captured_payloads[1]["params"]["message"]["metadata"]
    assert meta1[MetaKeys.OPERATION_ID] == meta2[MetaKeys.OPERATION_ID]

    # 2. Test pending progress operation_id retention across caller-level retries
    task = hub_store.assign_task("bob", "implementer", "T1", "Inst")

    call_count_prog = 0
    captured_progress_payloads: list[dict[str, Any]] = []

    async def mock_progress_failure(*args: Any, **kwargs: Any) -> httpx.Response:
        nonlocal call_count_prog
        call_count_prog += 1
        json_body = kwargs.get("json")
        if json_body:
            captured_progress_payloads.append(json_body)
        if call_count_prog <= 3:  # Fail all internal transport retries
            raise httpx.WriteTimeout("Write timed out")
        return await original_request(*args, **kwargs)

    with (
        patch.object(client, "request", side_effect=mock_progress_failure),
        pytest.raises(httpx.WriteTimeout),
    ):
        await worker.report_progress(task.id, "Step 1 progress")

    # Second caller-level attempt: should reuse pending operation_id
    call_count_prog_2 = 0

    async def mock_progress_success(*args: Any, **kwargs: Any) -> httpx.Response:
        nonlocal call_count_prog_2
        call_count_prog_2 += 1
        json_body = kwargs.get("json")
        if json_body:
            captured_progress_payloads.append(json_body)
        return await original_request(*args, **kwargs)

    with patch.object(client, "request", side_effect=mock_progress_success):
        prog_res = await worker.report_progress(task.id, "Step 1 progress")
        assert prog_res["ok"] is True

    # Check that operation_id was retained across the two caller calls
    first_attempt_meta = captured_progress_payloads[0]["params"]["message"]["metadata"]
    second_attempt_meta = captured_progress_payloads[-1]["params"]["message"]["metadata"]
    assert first_attempt_meta[MetaKeys.OPERATION_ID] == second_attempt_meta[MetaKeys.OPERATION_ID]


async def test_operation_table_created_column(
    client: httpx.AsyncClient, hub_store: HubStore
) -> None:
    op_id = "op-created-col-1"
    payload = _rpc(
        "message/send",
        {
            "message": {
                "messageId": uuid4().hex,
                "role": "user",
                "parts": [{"kind": "text", "text": "READY"}],
                "metadata": {
                    MetaKeys.AGENT: "eve",
                    MetaKeys.CAPABILITIES: ["python"],
                    MetaKeys.SCHEMA_VERSION: SCHEMA_VERSION,
                    MetaKeys.OPERATION_ID: op_id,
                },
            }
        },
    )
    resp = await client.post("/a2a", json=payload)
    assert resp.status_code == 200

    with database(hub_store.path) as conn:
        row = conn.execute("SELECT * FROM operation WHERE operation_id = ?", (op_id,)).fetchone()
        assert row is not None
        assert row["created"] is not None
        assert "T" in row["created"]  # Valid ISO timestamp


async def test_worker_client_clears_pending_ids_after_success(
    client: httpx.AsyncClient, hub_store: HubStore
) -> None:
    from worker_mcp.client import WorkerHubClient
    from worker_mcp.config import WorkerSettings

    settings = WorkerSettings(
        agent_name="alice-worker",
        hub_url="http://hub.test",
        token="test-token",
        profile=AgentProfile(harness="claude-code"),
    )
    worker = WorkerHubClient(settings, http_client=client)

    # 1. First check-in
    reg1 = await worker.check_in(["python"])
    assert reg1["status"] == "registered"
    assert worker._pending_checkin is None

    agent1 = hub_store.agent_by_name("alice-worker")
    assert agent1 is not None and agent1.status == AgentStatus.IDLE

    # 2. Assign task and test two consecutive identical progress reports
    task = hub_store.assign_task("alice-worker", "implementer", "Task 1", "Instructions")

    prog1 = await worker.report_progress(task.id, "still working")
    assert prog1["ok"] is True
    assert worker._pending_progress.get(task.id) is None

    prog2 = await worker.report_progress(task.id, "still working")
    assert prog2["ok"] is True
    assert worker._pending_progress.get(task.id) is None

    # Verify that two distinct task_progress events and operations were created
    with database(hub_store.path) as conn:
        events = conn.execute(
            "SELECT * FROM event WHERE kind = ? ORDER BY id ASC",
            (EventKind.TASK_PROGRESS.value,),
        ).fetchall()
        assert len(events) == 2

        ops = conn.execute(
            "SELECT operation_id FROM operation WHERE actor = ?",
            ("alice-worker",),
        ).fetchall()
        # 1 check-in + 2 progress = 3 distinct operations
        assert len(ops) == 3
        assert len(set(row["operation_id"] for row in ops)) == 3

    # 3. Complete the task
    res = ImplementerResult(
        outcome=ImplementerOutcome.COMPLETED,
        summary="Done",
        pr_url="https://github.com/org/repo/pull/1",
        head_sha="0123456789abcdef0123456789abcdef01234567",
    )
    submit_res = await worker.submit_result(task.id, res)
    assert submit_res["status"] == "completed"
    assert worker._pending_results.get(task.id) is not None
    assert worker._pending_progress.get(task.id) is None

    # 4. Re-admission test: simulate heartbeat timeout marking worker LOST
    hub_store.sweep(lost_after_s=-1)
    agent_lost = hub_store.agent_by_name("alice-worker")
    assert agent_lost is not None and agent_lost.status == AgentStatus.LOST

    # Lost worker cannot be assigned tasks
    with pytest.raises(Exception, match="not idle"):
        hub_store.assign_task("alice-worker", "implementer", "Task 2", "Inst 2")

    # Calling check_in again on the same client must generate a new operation_id
    # and re-admit the worker from LOST to IDLE
    reg2 = await worker.check_in(["python"])
    assert reg2["status"] == "registered"
    assert worker._pending_checkin is None

    agent_reopened = hub_store.agent_by_name("alice-worker")
    assert agent_reopened is not None
    assert agent_reopened.status == AgentStatus.IDLE

    # Worker can now be assigned work again
    task2 = hub_store.assign_task("alice-worker", "implementer", "Task 2", "Inst 2")
    assert task2 is not None

    # 5. Check-in after release: must use distinct op_id and reflect RELEASED
    hub_store.release_agent("alice-worker")
    agent_released = hub_store.agent_by_name("alice-worker")
    assert agent_released is not None and agent_released.status == AgentStatus.RELEASED

    reg3 = await worker.check_in(["python"])
    assert reg3["status"] == "registered"
    assert worker._pending_checkin is None

    # Total operations recorded for alice-worker should be 6 distinct IDs
    # (check-in 1, prog 1, prog 2, submit_result 1, check-in 2 re-admission,
    # check-in 3 post-release)
    with database(hub_store.path) as conn:
        ops_after = conn.execute(
            "SELECT operation_id FROM operation WHERE actor = ?",
            ("alice-worker",),
        ).fetchall()
        assert len(ops_after) == 6
        assert len(set(row["operation_id"] for row in ops_after)) == 6
