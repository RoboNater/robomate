import json
from pathlib import Path

import httpx
import pytest
from worker_mcp.client import WorkerHubClient
from worker_mcp.config import WorkerSettings
from worker_mcp.telemetry import HttpIO, TelemetryLog, current_http_io, mcp_result_bytes


def _records(path: Path) -> list[dict[str, object]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def test_telemetry_appends_bounded_structured_events(tmp_path: Path) -> None:
    path = tmp_path / "nested" / "worker.jsonl"
    telemetry = TelemetryLog(
        path,
        agent="bob",
        worker_instance_id="worker-1",
        session_fields={"harness": "codex", "model": "gpt-test"},
    )

    call_id, started = telemetry.start_tool("await_assignment")
    telemetry.finish_tool(
        "await_assignment", call_id, started, result={"timeout": True, "instructions": "secret"}
    )
    telemetry.emit(
        "retry",
        operation="message/stream",
        attempt=1,
        max_retries=3,
        reason="ReadTimeout",
        delay_s=0.5,
    )

    records = _records(path)
    assert [record["event"] for record in records] == [
        "session_started",
        "tool_call",
        "tool_call",
        "retry",
    ]
    assert records[0]["harness"] == "codex"
    assert records[0]["model"] == "gpt-test"
    assert records[2]["phase"] == "success"
    assert records[2]["outcome"] == "timeout"
    assert "instructions" not in records[2]
    assert all(record["timestamp"] for record in records)
    assert len({record["session_id"] for record in records}) == 1


def test_telemetry_records_tool_errors_without_raising(tmp_path: Path) -> None:
    path = tmp_path / "worker.jsonl"
    telemetry = TelemetryLog(path, agent="bob", worker_instance_id="worker-1")
    call_id, started = telemetry.start_tool("get_role_guide")
    telemetry.finish_tool(
        "get_role_guide",
        call_id,
        started,
        error=RuntimeError("guide unavailable"),
    )

    error = _records(path)[-1]
    assert error["phase"] == "error"
    assert error["error_type"] == "RuntimeError"
    assert error["error"] == "guide unavailable"


def test_telemetry_directory_failure_does_not_break_worker(tmp_path: Path) -> None:
    blocking_file = tmp_path / "not-a-directory"
    blocking_file.write_text("occupied", encoding="utf-8")

    telemetry = TelemetryLog(
        blocking_file / "worker.jsonl",
        agent="bob",
        worker_instance_id="worker-1",
    )

    assert telemetry.path is None


def test_finish_record_measures_both_boundaries(tmp_path: Path) -> None:
    path = tmp_path / "worker.jsonl"
    telemetry = TelemetryLog(path, agent="bob", worker_instance_id="worker-1")
    result = {"task_id": "t1", "role": "implementer", "instructions": "Ignore the rails ✓"}
    arguments = {"timeout_s": 120}

    call_id, started = telemetry.start_tool("await_assignment")
    telemetry.finish_tool(
        "await_assignment",
        call_id,
        started,
        result=result,
        arguments=arguments,
        http=HttpIO(requests=1, request_bytes=321, response_bytes=654, status=200, retries=2),
    )

    record = _records(path)[-1]
    # The text FastMCP hands the model, and the arguments it sent, byte for byte.
    text = json.dumps(result, indent=2, ensure_ascii=False)
    assert record["mcp_result_bytes"] == len(text.encode("utf-8"))
    assert record["mcp_request_bytes"] == len(json.dumps(arguments, separators=(",", ":")))
    assert record["mcp_result_bytes"] == mcp_result_bytes(result)
    assert {key: record[key] for key in record if key.startswith("http_")} == {
        "http_requests": 1,
        "http_request_bytes": 321,
        "http_response_bytes": 654,
        "http_status": 200,
        "http_retries": 2,
    }
    # Sizes only: the untrusted instructions never reach the log.
    assert "Ignore the rails" not in path.read_text(encoding="utf-8")


def test_a_string_result_is_measured_as_its_encoded_text(tmp_path: Path) -> None:
    path = tmp_path / "worker.jsonl"
    telemetry = TelemetryLog(path, agent="bob", worker_instance_id="worker-1")
    guide = "# Guide ✓\n"

    call_id, started = telemetry.start_tool("get_role_guide")
    telemetry.finish_tool("get_role_guide", call_id, started, result=guide)

    record = _records(path)[-1]
    assert record["mcp_result_bytes"] == len(guide.encode("utf-8"))
    assert "http_status" not in record


async def test_exchanges_are_charged_only_to_the_call_in_progress() -> None:
    attempts = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        if attempts == 2:
            return httpx.Response(503, content=b"busy")
        return httpx.Response(200, content=b"x" * 7)

    settings = WorkerSettings(
        hub_url="http://hub", token="t", agent_name="bob", backoff_factor_s=0.001
    )
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="http://hub"
    ) as http:
        worker = WorkerHubClient(settings, http_client=http)
        # No call in progress, as for the heartbeat task: nothing is charged.
        await worker._request_with_retry("POST", "/a2a", json_body={"a": 1})

        io = HttpIO()
        token = current_http_io.set(io)
        try:
            await worker._request_with_retry("POST", "/a2a", json_body={"a": 1})
        finally:
            current_http_io.reset(token)

    # Both attempts crossed the wire: 7 + 7 bytes sent, a 4-byte 503 and a 7-byte 200 back.
    assert io == HttpIO(requests=2, request_bytes=14, response_bytes=11, status=200, retries=1)


async def test_a_retried_stream_and_a_transport_retry_are_charged() -> None:
    sse = b'data: {"jsonrpc": "2.0", "id": "1", "result": {"ok": true}}\n\n'
    attempts = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise httpx.ConnectError("refused", request=request)
        if attempts == 2:
            return httpx.Response(503, content=b"busy")
        return httpx.Response(200, content=sse, headers={"content-type": "text/event-stream"})

    settings = WorkerSettings(
        hub_url="http://hub", token="t", agent_name="bob", backoff_factor_s=0.001
    )
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="http://hub"
    ) as http:
        worker = WorkerHubClient(settings, http_client=http)
        io = HttpIO()
        token = current_http_io.set(io)
        try:
            assert await worker._stream_rpc("message/stream", {}, 1) == {"ok": True}
        finally:
            current_http_io.reset(token)

    # The refused attempt left no response; the 503 and the stream both count.
    assert io.requests == 2
    assert io.retries == 2
    assert io.response_bytes == len(b"busy") + len(sse)
    assert io.status == 200


def test_nothing_is_written_when_telemetry_is_off(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    telemetry = TelemetryLog(None, agent="bob", worker_instance_id="worker-1")
    call_id, started = telemetry.start_tool("get_role_guide")
    telemetry.finish_tool("get_role_guide", call_id, started, result="guide", http=HttpIO())

    assert list(tmp_path.iterdir()) == []
    assert capsys.readouterr().out == ""
