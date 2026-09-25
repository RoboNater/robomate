import json
import re
from pathlib import Path
from typing import Any

import pytest
from a2a.types import (
    CancelTaskRequest,
    GetTaskRequest,
    JSONRPCSuccessResponse,
    Message,
    SendMessageRequest,
    SendStreamingMessageRequest,
    Task,
)
from agent_hub.database import initialize_database
from agent_hub.protocol import A2AProtocol
from agent_hub.store import HubStore
from agent_hub_common import AgentProfile, HubSettings, MetaKeys

WIRE_DIR = Path(__file__).parent / "wire"


def _collect_metadata_keys(obj: Any) -> list[str]:
    keys: list[str] = []
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k == "metadata" and isinstance(v, dict):
                keys.extend(v.keys())
            keys.extend(_collect_metadata_keys(v))
    elif isinstance(obj, list):
        for item in obj:
            keys.extend(_collect_metadata_keys(item))
    return keys


@pytest.mark.parametrize(
    "fixture_name",
    [
        "message_send.json",
        "heartbeat.json",
        "message_stream.json",
        "tasks_get.json",
        "tasks_cancel.json",
    ],
)
def test_wire_fixtures_round_trip_a2a_sdk_models(fixture_name: str) -> None:
    fixture_path = WIRE_DIR / fixture_name
    assert fixture_path.exists(), f"Fixture {fixture_name} missing"
    data = json.loads(fixture_path.read_text(encoding="utf-8"))

    method = data["method"]

    # Validate request against a2a-sdk model and assert wire shape round-trips
    validated_req: (
        SendMessageRequest
        | SendStreamingMessageRequest
        | GetTaskRequest
        | CancelTaskRequest
    )
    if method == "message/send":
        validated_req = SendMessageRequest.model_validate(data["request"])
    elif method == "message/stream":
        validated_req = SendStreamingMessageRequest.model_validate(data["request"])
    elif method == "tasks/get":
        validated_req = GetTaskRequest.model_validate(data["request"])
    elif method == "tasks/cancel":
        validated_req = CancelTaskRequest.model_validate(data["request"])
    else:
        raise ValueError(f"Unknown method: {method}")

    dumped_req = validated_req.model_dump(mode="json", exclude_none=True, by_alias=True)
    assert dumped_req == data["request"]

    # Validate response against a2a-sdk model and assert wire shape round-trips
    validated_resp = JSONRPCSuccessResponse.model_validate(data["response"])
    if method == "message/send":
        msg_result = Message.model_validate(data["response"]["result"])
        dumped_result = msg_result.model_dump(
            mode="json", exclude_none=True, by_alias=True
        )
        assert dumped_result == data["response"]["result"]
    else:
        task_result = Task.model_validate(data["response"]["result"])
        dumped_result = task_result.model_dump(
            mode="json", exclude_none=True, by_alias=True
        )
        assert dumped_result == data["response"]["result"]
    dumped_resp = validated_resp.model_dump(mode="json", exclude_none=True, by_alias=True)
    assert dumped_resp == data["response"]


def test_no_unprefixed_hub_keys_in_fixtures() -> None:
    for fixture_path in WIRE_DIR.glob("*.json"):
        data = json.loads(fixture_path.read_text(encoding="utf-8"))
        keys = _collect_metadata_keys(data)
        assert keys, f"Expected metadata keys in {fixture_path.name}"
        for key in keys:
            assert key.startswith("hub."), f"Found unprefixed key {key!r} in {fixture_path.name}"


def test_no_unprefixed_hub_keys_in_packages_code() -> None:
    import ast

    packages_dir = Path(__file__).resolve().parents[1] / "packages"
    meta_get_re = re.compile(r'metadata\.get\(\s*["\']([^"\']+)["\']')
    meta_sub_re = re.compile(r'metadata\[\s*["\']([^"\']+)["\']\s*\]')

    for py_file in packages_dir.rglob("*.py"):
        text = py_file.read_text(encoding="utf-8")
        for match in meta_get_re.finditer(text):
            key = match.group(1)
            assert key.startswith("hub."), (
                f"Unprefixed metadata.get key {key!r} in {py_file}"
            )
        for match in meta_sub_re.finditer(text):
            key = match.group(1)
            assert key.startswith("hub."), (
                f"Unprefixed metadata subscript key {key!r} in {py_file}"
            )

        tree = ast.parse(text, filename=str(py_file))
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.keyword)
                and node.arg == "metadata"
                and isinstance(node.value, ast.Dict)
            ):
                for key_node in node.value.keys:
                    if isinstance(key_node, ast.Constant) and isinstance(key_node.value, str):
                        assert key_node.value.startswith("hub."), (
                            f"{py_file}: Unprefixed key {key_node.value!r} in metadata literal"
                        )
            elif isinstance(node, ast.Assign) and isinstance(node.value, ast.Dict):
                for target in node.targets:
                    if isinstance(target, ast.Name) and target.id == "metadata":
                        for key_node in node.value.keys:
                            if isinstance(key_node, ast.Constant) and isinstance(
                                key_node.value, str
                            ):
                                msg = f"{py_file}: Unprefixed key {key_node.value!r} in metadata"
                                assert key_node.value.startswith("hub."), msg
            elif isinstance(node, ast.Call):
                func = node.func
                is_text_part = (
                    (isinstance(func, ast.Name) and func.id == "text_part")
                    or (isinstance(func, ast.Attribute) and func.attr == "text_part")
                )
                if is_text_part:
                    for kw in node.keywords:
                        msg = f"{py_file}:{node.lineno}: text_part called with kwarg {kw.arg!r}"
                        assert kw.arg == "metadata", msg


async def test_wire_fixtures_dispatch(tmp_path: Path) -> None:
    db_path = tmp_path / "wire_hub.db"
    initialize_database(db_path)
    settings = HubSettings(
        host="127.0.0.1",
        port=8420,
        public_url="http://hub.example:8420",
        state_dir=tmp_path,
        database_path=db_path,
        token="wire-token",
        token_file=tmp_path / "token",
        guides_dir=tmp_path / "guides",
    )
    store = HubStore(db_path)
    store.initialize_workflow()
    protocol = A2AProtocol(store, settings)

    # 1. message/send check-in fixture
    send_fixture = json.loads((WIRE_DIR / "message_send.json").read_text(encoding="utf-8"))
    resp = await protocol.dispatch(send_fixture["request"])
    resp_data = json.loads(bytes(resp.body).decode("utf-8"))
    assert resp_data["id"] == send_fixture["request"]["id"]
    assert resp_data["result"]["kind"] == "message"
    assert resp_data["result"]["metadata"][MetaKeys.AGENT] == "bob"
    assert resp_data["result"]["metadata"][MetaKeys.KIND] == "check_in_ack"
    assert resp_data["result"]["metadata"][MetaKeys.STATUS] == "idle"
    bob = store.agent_by_name("bob")
    assert bob is not None
    assert (bob.harness, bob.harness_version) == ("claude-code", "2.1.268")
    assert bob.provider == "anthropic"
    assert (bob.model, bob.model_source, bob.capabilities) == ("claude-opus-5", "env", ["python"])

    # Assign task so tasks/get and tasks/cancel work
    task = store.assign_task("bob", "implementer", "Fix #1", "Fix issue #1")
    store.record_progress(task.id, "bob", "branch pushed")

    # 2. tasks/get fixture
    get_fixture = json.loads((WIRE_DIR / "tasks_get.json").read_text(encoding="utf-8"))
    get_req = dict(get_fixture["request"])
    get_req["params"] = {"id": task.id, "historyLength": 10}
    resp_get = await protocol.dispatch(get_req)
    get_data = json.loads(bytes(resp_get.body).decode("utf-8"))
    assert get_data["result"]["id"] == task.id
    assert get_data["result"]["kind"] == "task"
    assert get_data["result"]["metadata"][MetaKeys.ROLE] == "implementer"
    assert get_data["result"]["metadata"][MetaKeys.ASSIGNEE] == "bob"

    # 3. tasks/cancel fixture
    cancel_fixture = json.loads((WIRE_DIR / "tasks_cancel.json").read_text(encoding="utf-8"))
    cancel_req = dict(cancel_fixture["request"])
    cancel_req["params"] = {"id": task.id}
    resp_cancel = await protocol.dispatch(cancel_req)
    cancel_data = json.loads(bytes(resp_cancel.body).decode("utf-8"))
    assert cancel_data["result"]["id"] == task.id
    assert cancel_data["result"]["status"]["state"] == "canceled"
    assert cancel_data["result"]["metadata"][MetaKeys.ASSIGNEE] == "bob"


async def test_tasks_get_populated_history_has_only_prefixed_metadata(
    tmp_path: Path,
) -> None:
    """Regression test for Issue #22 finding: part-metadata must be namespaced to hub.*.

    Verifies that all history messages (assignment, progress, question, reply)
    and legacy persisted rows in the database return only hub.* namespaced
    metadata keys at all levels (message, part, task).
    """
    from agent_hub.database import database

    db_path = tmp_path / "populated_hub.db"
    initialize_database(db_path)
    settings = HubSettings(
        host="127.0.0.1",
        port=8420,
        public_url="http://hub.example:8420",
        state_dir=tmp_path,
        database_path=db_path,
        token="wire-token",
        token_file=tmp_path / "token",
        guides_dir=tmp_path / "guides",
    )
    store = HubStore(db_path)
    store.initialize_workflow()
    protocol = A2AProtocol(store, settings)

    store.check_in("bob", AgentProfile(harness="claude-code", capabilities=("python",)))
    task = store.assign_task("bob", "implementer", "Fix issue #22", "Do the work")
    store.record_progress(task.id, "bob", "working on fix")
    q_id = store.open_question(task.id, "bob", "Which approach?", sent_as="q-001")
    assert q_id > 0
    store.reply(task.id, "Use MetaKeys constants", message_id=q_id)

    # Directly insert legacy transcript rows with raw, unprefixed keys to test
    # backward compatibility for existing persisted databases.
    with database(db_path) as conn:
        legacy_parts = json.dumps(
            [
                {
                    "kind": "text",
                    "text": "legacy progress note",
                    "metadata": {"kind": "progress", "role": "implementer"},
                }
            ]
        )
        conn.execute(
            "INSERT INTO message (task_id, context_id, sender, direction, parts_json, ts)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (task.id, "ctx-bob", "bob", "to_alice", legacy_parts, "2026-09-01T12:00:00Z"),
        )
        # Old question format persisted prior to issue #22 using message_id for correlation
        legacy_q_parts = json.dumps(
            [
                {
                    "kind": "text",
                    "text": "old question text",
                    "metadata": {"kind": "question", "message_id": "legacy-q-999"},
                }
            ]
        )
        cursor = conn.execute(
            "INSERT INTO message (task_id, context_id, sender, direction, parts_json, ts)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (task.id, "ctx-bob", "bob", "to_alice", legacy_q_parts, "2026-09-01T12:01:00Z"),
        )
        legacy_q_row_id = cursor.lastrowid

    # Verify that a retry of the legacy question recognizes and reuses the existing row
    events_before = store.pending_events()
    history_len_before = len(store.task_history(task.id))
    reused_id = store.open_question(task.id, "bob", "old question text", sent_as="legacy-q-999")
    assert reused_id == legacy_q_row_id
    assert (
        store.pending_events() == events_before
    ), "Retried question must not queue duplicate event"
    assert len(store.task_history(task.id)) == history_len_before, "No new row should be added"

    # Dispatch tasks/get with full history
    get_req = {
        "jsonrpc": "2.0",
        "id": 99,
        "method": "tasks/get",
        "params": {"id": task.id, "historyLength": 10},
    }
    resp = await protocol.dispatch(get_req)
    resp_data = json.loads(bytes(resp.body).decode("utf-8"))

    # Validate response matches a2a-sdk Task model
    JSONRPCSuccessResponse.model_validate(resp_data)
    task_result = Task.model_validate(resp_data["result"])
    assert task_result.history is not None
    assert len(task_result.history) >= 6

    # Check all metadata keys recursively across entire response
    all_keys = _collect_metadata_keys(resp_data)
    assert all_keys, "Expected metadata keys in tasks/get response"
    unprefixed = [k for k in all_keys if not k.startswith("hub.")]
    assert not unprefixed, f"Found unprefixed metadata keys: {unprefixed}"

    # Specifically check part metadata on each history message
    history = resp_data["result"]["history"]
    part_metas = [
        part["metadata"]
        for msg in history
        for part in msg.get("parts", [])
        if "metadata" in part
    ]
    assert len(part_metas) >= 6, "Expected part metadata on history messages"
    for pmeta in part_metas:
        assert isinstance(pmeta, dict)
        for key in pmeta:
            assert key.startswith("hub."), f"Found unprefixed part metadata key {key!r}"

    # Verify that the legacy progress row's metadata was normalized to hub.*
    legacy_msg = next(
        msg
        for msg in history
        if any(p.get("text") == "legacy progress note" for p in msg.get("parts", []))
    )
    legacy_part = legacy_msg["parts"][0]
    assert legacy_part["metadata"] == {
        "hub.kind": "progress",
        "hub.role": "implementer",
    }

    # Verify that the legacy question row's message_id was normalized to hub.retry_as_message_id
    legacy_q_msg = next(
        msg
        for msg in history
        if any(p.get("text") == "old question text" for p in msg.get("parts", []))
    )
    legacy_q_part = legacy_q_msg["parts"][0]
    assert legacy_q_part["metadata"] == {
        "hub.kind": "question",
        "hub.retry_as_message_id": "legacy-q-999",
    }
