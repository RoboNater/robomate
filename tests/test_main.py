"""The legacy hub command is HTTP-only and survives MCP stdin EOF."""

import json
import os
import signal
import socket
import sqlite3
import subprocess
import sys
import time
import urllib.request
from pathlib import Path
from typing import Any, cast

import uvicorn
from agent_hub import main
from agent_hub.rpc_errors import OPERATOR_REQUIRED
from agent_hub_common import SCHEMA_VERSION, MetaKeys


def _request(
    url: str,
    token: str,
    method: str,
    params: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
) -> dict[str, Any]:
    request = urllib.request.Request(
        url,
        data=json.dumps(
            {"jsonrpc": "2.0", "id": 1, "method": method, "params": params or {}}
        ).encode(),
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            **(headers or {}),
        },
    )
    with urllib.request.urlopen(request, timeout=2) as response:
        return cast(dict[str, Any], json.load(response))


def test_signal_capture_degrades_off_main_thread() -> None:
    import threading

    errors: list[BaseException] = []
    server = main.HubServer(uvicorn.Config("unused:app"))

    def enter() -> None:
        try:
            with server.capture_signals():
                pass
        except BaseException as exc:
            errors.append(exc)

    thread = threading.Thread(target=enter)
    thread.start()
    thread.join()
    assert errors == []


def test_second_sigint_forces_exit() -> None:
    server = main.HubServer(uvicorn.Config("unused:app"))
    with server.capture_signals():
        handler = signal.getsignal(signal.SIGINT)
        assert callable(handler)
        handler(signal.SIGINT, None)
        assert server.should_exit and not server.force_exit
        handler(signal.SIGINT, None)
        assert server.force_exit


def test_hub_entry_point_is_http_only_and_records_socket_peer(tmp_path: Path) -> None:
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    operator_file = tmp_path / "operator-token"
    operator_file.write_text("operator-secret\n")
    operator_file.chmod(0o600)
    process = subprocess.Popen(
        [sys.executable, "-m", "agent_hub.main"],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env={
            **os.environ,
            "HUB_STATE_DIR": str(tmp_path),
            "HUB_DB_PATH": str(tmp_path / "hub.db"),
            "HUB_TOKEN": "test-token",
            "HUB_HOST": "127.0.0.1",
            "HUB_PORT": str(port),
            "ROBOMATE_OPERATOR_TOKEN_FILE": str(operator_file),
        },
    )
    try:
        deadline = time.monotonic() + 10
        while True:
            try:
                with urllib.request.urlopen(f"http://127.0.0.1:{port}/healthz", timeout=0.2) as r:
                    assert json.load(r)["status"] == "ok"
                break
            except OSError:
                assert process.poll() is None and time.monotonic() < deadline, (
                    process.stderr.read().decode()
                    if process.poll() is not None and process.stderr
                    else "timed out"
                )
                time.sleep(0.02)
        metadata = {
            MetaKeys.AGENT: "bob",
            MetaKeys.SCHEMA_VERSION: SCHEMA_VERSION,
            MetaKeys.OPERATION_ID: "op-1",
            MetaKeys.WORKER_INSTANCE_ID: "bob-1",
        }
        body = {
            "message": {
                "messageId": "m-1",
                "role": "user",
                "parts": [{"kind": "text", "text": "READY"}],
                "metadata": metadata,
            }
        }
        request = urllib.request.Request(
            f"http://127.0.0.1:{port}/a2a",
            data=json.dumps(
                {"jsonrpc": "2.0", "id": 1, "method": "message/send", "params": body}
            ).encode(),
            headers={
                "Authorization": "Bearer test-token",
                "Content-Type": "application/json",
                "X-Forwarded-For": "203.0.113.7",
            },
        )
        with urllib.request.urlopen(request, timeout=2) as response:
            assert "result" in json.load(response)
        rpc_url = f"http://127.0.0.1:{port}/rpc"
        refused = _request(rpc_url, "test-token", "hub.shutdown")
        assert refused["error"]["code"] == OPERATOR_REQUIRED
        operator = {"X-Robomate-Operator": "operator-secret"}
        assert _request(rpc_url, "test-token", "hub.shutdown", headers=operator)["result"] == {
            "stopping": True
        }
        process.wait(timeout=5)
        assert process.returncode == 0
        assert process.stdout is not None and process.stdout.read() == b""
        with sqlite3.connect(tmp_path / "hub.db") as connection:
            row = connection.execute(
                "SELECT checkin_remote_addr, last_remote_addr FROM agent WHERE name = 'bob'"
            ).fetchone()
        assert row == ("127.0.0.1", "127.0.0.1")
    finally:
        if process.poll() is None:
            process.terminate()
            process.wait(timeout=5)
        for stream in (process.stdout, process.stderr):
            if stream is not None:
                stream.close()
