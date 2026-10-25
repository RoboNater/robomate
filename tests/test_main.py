"""The legacy hub command is HTTP-only and survives MCP stdin EOF."""

import io
import json
import os
import signal
import socket
import sqlite3
import subprocess
import sys
import time
import urllib.request
from collections.abc import Callable, Mapping
from dataclasses import replace
from pathlib import Path
from typing import Any, cast

import pytest
import uvicorn
from agent_hub import main
from agent_hub.rpc_errors import OPERATOR_REQUIRED
from agent_hub_common import SCHEMA_VERSION, HubSettings, MetaKeys
from conftest import reserve_port_block
from timing import LIVENESS_S


def test_hub_entry_point_escapes_unencodable_output(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = replace(HubSettings.from_env({}), port=0)
    monkeypatch.setattr(HubSettings, "from_env", lambda: settings)

    async def serve(
        settings: HubSettings,
        sockets: list[socket.socket],
        hub_info: Mapping[str, Any],
        on_started: Callable[[], None],
    ) -> None:
        print("drivér → ā", flush=True)

    monkeypatch.setattr(main, "serve_http", serve)
    with (
        io.BytesIO() as buffer,
        io.TextIOWrapper(buffer, encoding="cp1252", newline="\n") as stdout,
    ):
        monkeypatch.setattr(sys, "stdout", stdout)
        main.main()
        assert stdout.encoding == "cp1252"
        assert buffer.getvalue() == b"driv\xe9r \\u2192 \\u0101\n"


def _request(
    url: str,
    token: str,
    method: str,
    params: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
    timeout: float = LIVENESS_S,
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
    with urllib.request.urlopen(request, timeout=timeout) as response:
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
    port = reserve_port_block(1)
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
        deadline = time.monotonic() + LIVENESS_S  # a hang bound, not a budget (#372)
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
        with urllib.request.urlopen(request, timeout=LIVENESS_S) as response:
            assert "result" in json.load(response)
        rpc_url = f"http://127.0.0.1:{port}/rpc"
        refused = _request(rpc_url, "test-token", "hub.shutdown")
        assert refused["error"]["code"] == OPERATOR_REQUIRED
        operator = {"X-Robomate-Operator": "operator-secret"}
        assert _request(rpc_url, "test-token", "hub.shutdown", headers=operator)["result"] == {
            "stopping": True
        }
        process.wait(timeout=LIVENESS_S)
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
            process.wait(timeout=LIVENESS_S)
        for stream in (process.stdout, process.stderr):
            if stream is not None:
                stream.close()


def test_hub_entry_point_keeps_its_token_in_a_directory_of_its_own(tmp_path: Path) -> None:
    """A relocated hub's token is in `token/`, apart from `state/` (spec §4, §12)."""

    port = reserve_port_block(1)
    namespace = tmp_path / "ns"
    state, token_file = namespace / "state", namespace / "token" / "token"
    operator_file = tmp_path / "operator-token"
    operator_file.write_text("operator-secret\n")
    operator_file.chmod(0o600)
    process = subprocess.Popen(
        [sys.executable, "-m", "agent_hub.main"],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env={
            **{k: v for k, v in os.environ.items() if not k.startswith("HUB_")},
            "HUB_STATE_DIR": str(state),
            "HUB_TOKEN_FILE": str(token_file),
            "HUB_HOST": "127.0.0.1",
            "HUB_PORT": str(port),
            "ROBOMATE_OPERATOR_TOKEN_FILE": str(operator_file),
        },
    )
    try:
        deadline = time.monotonic() + LIVENESS_S  # a hang bound, not a budget (#372)
        while not token_file.is_file() or not token_file.read_text().strip():
            assert process.poll() is None and time.monotonic() < deadline, (
                process.stderr.read().decode() if process.stderr else "timed out"
            )
            time.sleep(0.02)
        token = token_file.read_text().strip()
        rpc_url = f"http://127.0.0.1:{port}/rpc"
        while True:
            try:
                refused = _request(rpc_url, token, "hub.shutdown")
                break
            except OSError:
                assert process.poll() is None and time.monotonic() < deadline, "timed out"
                time.sleep(0.02)
        # The bearer token read from its own directory authenticates; it is not the operator's.
        assert refused["error"]["code"] == OPERATOR_REQUIRED
        assert not (state / "token").exists()
        if sys.platform != "win32":
            assert token_file.stat().st_mode & 0o777 == 0o600
            assert token_file.parent.stat().st_mode & 0o777 == 0o700
        operator = {"X-Robomate-Operator": "operator-secret"}
        _request(rpc_url, token, "hub.shutdown", headers=operator)
        process.wait(timeout=LIVENESS_S)
    finally:
        if process.poll() is None:
            process.terminate()
            process.wait(timeout=LIVENESS_S)
        for stream in (process.stdout, process.stderr):
            if stream is not None:
                stream.close()
