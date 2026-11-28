"""The legacy hub command is HTTP-only and survives MCP stdin EOF."""

import io
import json
import os
import re
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
from agent_hub_common.logging_setup import clear_hub_logging
from conftest import reserve_port_block
from timing import LIVENESS_S, wait_until_sync


def test_hub_entry_point_escapes_unencodable_output(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # A state directory of its own: the shared setup (#652) writes the hub's
    # log file under the settings this test hands to main().
    settings = replace(HubSettings.from_env({"XDG_STATE_HOME": str(tmp_path)}), port=0)
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
        try:
            main.main()
        finally:
            # The in-process entry point leaves its handlers on the test's
            # root logger; remove them before the next test logs anything.
            clear_hub_logging()
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
            MetaKeys.STANDING_ROLE: "worker",
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


# Hub log level and persistent log file (#652).


def _launch_hub_entry(state: Path, extra: dict[str, str]) -> tuple[subprocess.Popen[bytes], int]:
    """Start `python -m agent_hub.main` with isolated state and a free port.

    Ambient `HUB_*` is stripped so this run's own level, file and timings are
    the only ones the hub sees.
    """

    port = reserve_port_block(1)
    operator_file = state.parent / "operator-token"
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
            "HUB_DB_PATH": str(state / "hub.db"),
            "HUB_HOST": "127.0.0.1",
            "HUB_PORT": str(port),
            "ROBOMATE_OPERATOR_TOKEN_FILE": str(operator_file),
            **extra,
        },
    )
    return process, port


def _await_healthz(port: int, process: subprocess.Popen[bytes]) -> None:
    """Wait until the hub answers; a hang bound, not a budget (#372)."""

    deadline = time.monotonic() + LIVENESS_S
    while True:
        try:
            with urllib.request.urlopen(
                f"http://127.0.0.1:{port}/healthz", timeout=0.2
            ) as response:
                assert json.load(response)["status"] == "ok"
            return
        except OSError:
            assert process.poll() is None and time.monotonic() < deadline, (
                process.stderr.read().decode()
                if process.poll() is not None and process.stderr
                else "timed out"
            )
            time.sleep(0.02)


def _read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return ""


def _stop_entry(process: subprocess.Popen[bytes]) -> str:
    """Stop the hub and return everything it wrote to stderr."""

    if process.poll() is None:
        process.terminate()
        process.wait(timeout=LIVENESS_S)
    assert process.stderr is not None
    stderr = process.stderr.read().decode("utf-8", errors="replace")
    for stream in (process.stdout, process.stderr):
        if stream is not None:
            stream.close()
    return stderr


def test_hub_entry_point_writes_the_default_log_file(tmp_path: Path) -> None:
    """#652: the persistent log holds the same startup records as stderr, privately."""

    state = tmp_path / "state"
    process, port = _launch_hub_entry(state, {})
    try:
        _await_healthz(port, process)
        log_file = state / "logs" / "hub.log"
        database = state / "hub.db"
        wait_until_sync(
            lambda: f"SQLite database ready at {database}" in _read_text(log_file),
            "the hub's log file holds its startup lines",
        )
        content = _read_text(log_file)
        assert re.search(
            rf"^\d{{4}}-\d\d-\d\d \d\d:\d\d:\d\d,\d{{3}} "
            rf"INFO:agent_hub\.app:SQLite database ready at {re.escape(str(database))}",
            content,
            re.MULTILINE,
        )
        if sys.platform != "win32":
            assert log_file.stat().st_mode & 0o777 == 0o600
            assert log_file.parent.stat().st_mode & 0o777 == 0o700
        # The same records reach stderr in the pre-#652 format.
        stderr = _stop_entry(process)
        assert f"INFO:agent_hub.app:SQLite database ready at {database}" in stderr
    finally:
        if process.poll() is None:
            process.terminate()
            process.wait(timeout=LIVENESS_S)
        for stream in (process.stdout, process.stderr):
            if stream is not None:
                stream.close()


def test_hub_entry_point_honours_hub_log_file_and_debug_level(tmp_path: Path) -> None:
    """#652: HUB_LOG_FILE redirects the file; DEBUG makes the sweeper's line visible."""

    state = tmp_path / "state"
    custom = tmp_path / "custom" / "operator-hub.log"
    process, port = _launch_hub_entry(
        state,
        {
            "HUB_TOKEN": "test-token",
            "HUB_LOG_LEVEL": "DEBUG",
            "HUB_LOG_FILE": str(custom),
            "HUB_SWEEP_INTERVAL_S": "0.1",
            "HUB_LOST_AFTER_S": "1",
        },
    )
    try:
        _await_healthz(port, process)
        # Join an agent with no heartbeat behind it: within HUB_LOST_AFTER_S
        # the sweeper declares it lost and logs the queued event at DEBUG.
        body = {
            "message": {
                "messageId": "m-1",
                "role": "user",
                "parts": [{"kind": "text", "text": "READY"}],
                "metadata": {
                    MetaKeys.STANDING_ROLE: "worker",
                    MetaKeys.AGENT: "bob",
                    MetaKeys.SCHEMA_VERSION: SCHEMA_VERSION,
                    MetaKeys.OPERATION_ID: "op-1",
                    MetaKeys.WORKER_INSTANCE_ID: "bob-1",
                },
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
            },
        )
        with urllib.request.urlopen(request, timeout=LIVENESS_S) as response:
            assert "result" in json.load(response)
        wait_until_sync(
            lambda: "Sweeper queued" in _read_text(custom),
            "HUB_LOG_LEVEL=DEBUG writes the sweeper's per-event line to HUB_LOG_FILE",
        )
        # The configured file replaced the default location.
        assert not (state / "logs" / "hub.log").exists()
    finally:
        if process.poll() is None:
            process.terminate()
            process.wait(timeout=LIVENESS_S)
        for stream in (process.stdout, process.stderr):
            if stream is not None:
                stream.close()


def test_hub_entry_point_log_level_warning_hides_info_and_disable_skips_the_file(
    tmp_path: Path,
) -> None:
    """#652: WARNING drops the INFO startup lines; HUB_LOG_FILE=- writes no file."""

    state = tmp_path / "state"
    process, port = _launch_hub_entry(state, {"HUB_LOG_LEVEL": "WARNING", "HUB_LOG_FILE": "-"})
    try:
        # healthz answers only after the lifespan startup that logs the INFO
        # lines, so by now they would exist if the level allowed them.
        _await_healthz(port, process)
        stderr = _stop_entry(process)
        assert "INFO:agent_hub.app:" not in stderr
        assert not (state / "logs" / "hub.log").exists()
    finally:
        if process.poll() is None:
            process.terminate()
            process.wait(timeout=LIVENESS_S)
        for stream in (process.stdout, process.stderr):
            if stream is not None:
                stream.close()


def test_hub_entry_point_formats_configuration_warnings(tmp_path: Path) -> None:
    """#652 r1-3: a warning raised inside `from_env` is formatted, not lastResort."""

    home = tmp_path / "home"
    home.mkdir()
    operator_file = tmp_path / "operator-token"
    operator_file.write_text("operator-secret\n")
    operator_file.chmod(0o600)
    port = reserve_port_block(1)
    process = subprocess.Popen(
        [sys.executable, "-m", "agent_hub.main"],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env={
            **{k: v for k, v in os.environ.items() if not k.startswith("HUB_")},
            # No HUB_STATE_DIR: the relative XDG value is refused and falls
            # back to $HOME/.local/state/agent-hub, warning from inside
            # from_env before any handler existed (#652 r1-3).
            "XDG_STATE_HOME": "relative-state",
            "HOME": str(home),
            "USERPROFILE": str(home),
            "HUB_HOST": "127.0.0.1",
            "HUB_PORT": str(port),
            "ROBOMATE_OPERATOR_TOKEN_FILE": str(operator_file),
        },
    )
    try:
        _await_healthz(port, process)
        stderr = _stop_entry(process)
        assert "WARNING:agent_hub_common.config:Ignoring relative XDG_STATE_HOME" in stderr
        # The state it fell back to is the isolated home, not the real one.
        assert (home / ".local" / "state" / "agent-hub" / "logs" / "hub.log").is_file()
    finally:
        if process.poll() is None:
            process.terminate()
            process.wait(timeout=LIVENESS_S)
        for stream in (process.stdout, process.stderr):
            if stream is not None:
                stream.close()
