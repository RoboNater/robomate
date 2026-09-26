"""End-to-end operator CLI tests in a disposable Git checkout."""

import errno
import json
import os
import socket
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

import pytest
from agent_hub_common.discovery import read_hub_json, write_hub_json
from robomate.cli import _bind

CLI = str(Path(sys.executable).with_name("robomate"))


@pytest.fixture
def repository(tmp_path: Path) -> tuple[Path, dict[str, str]]:
    root = tmp_path / "repo"
    root.mkdir()
    subprocess.run(["git", "init", "-b", "main"], cwd=root, check=True, capture_output=True)
    subprocess.run(
        ["git", "remote", "add", "origin", "git@github.com:example/repo.git"],
        cwd=root, check=True,
    )
    subprocess.run(
        ["git", "symbolic-ref", "refs/remotes/origin/HEAD", "refs/remotes/origin/main"],
        cwd=root, check=True,
    )
    env = {**os.environ, "XDG_STATE_HOME": str(tmp_path / "xdg")}
    return root, env


def start(root: Path, env: dict[str, str], *args: str) -> subprocess.Popen[str]:
    return subprocess.Popen(
        [CLI, "up", *args], cwd=root, env=env, text=True,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )


def await_hub(root: Path, process: subprocess.Popen[str]) -> dict[str, object]:
    for _ in range(100):
        data = read_hub_json(root)
        if data and data.get("pid") == process.pid:
            return data
        if process.poll() is not None:
            assert process.stderr is not None
            raise AssertionError(f"hub exited early: {process.stderr.read()}")
        time.sleep(0.05)
    raise AssertionError("hub did not start")


def stop(root: Path, env: dict[str, str], process: subprocess.Popen[str]) -> None:
    try:
        result = subprocess.run([CLI, "down"], cwd=root, env=env, text=True,
                                capture_output=True, timeout=10)
        assert result.returncode == 0, result.stderr
        assert process.wait(timeout=10) == 0
    finally:
        if process.poll() is None:
            process.terminate()
            process.wait(timeout=10)


def test_up_down_reuse_and_duplicate(repository: tuple[Path, dict[str, str]]) -> None:
    root, env = repository
    first = start(root, env)
    try:
        info = await_hub(root, first)
        assert info["hub_id"]
        if sys.platform != "win32":
            assert (root / ".robomate").stat().st_mode & 0o777 == 0o700
            assert (root / ".robomate/token").stat().st_mode & 0o777 == 0o600
        assert "/.robomate/" in (root / ".git/info/exclude").read_text()
        with urllib.request.urlopen(f"{info['url']}/healthz") as response:
            assert json.load(response)["hub_id"] == info["hub_id"]
        second = subprocess.run([CLI, "up"], cwd=root, env=env, text=True,
                                capture_output=True, timeout=10)
        assert second.returncode == 1
        assert "already running" in second.stderr
        stop(root, env, first)
        stopped = read_hub_json(root)
        assert stopped is not None and stopped["port"] == info["port"]
        assert stopped["pid"] is None and stopped["started_at"] is None
        restarted = start(root, env)
        try:
            again = await_hub(root, restarted)
            assert (again["port"], again["hub_id"]) == (info["port"], info["hub_id"])
        finally:
            stop(root, env, restarted)
    finally:
        if first.poll() is None:
            first.terminate()
            first.wait(timeout=10)


def test_recorded_busy_port_is_refused(repository: tuple[Path, dict[str, str]]) -> None:
    root, env = repository
    with socket.socket() as occupied:
        occupied.bind(("127.0.0.1", 0))
        occupied.listen()
        port = occupied.getsockname()[1]
        write_hub_json(root, {"port": port, "hub_id": "old"})
        result = subprocess.run([CLI, "up"], cwd=root, env=env, text=True,
                                capture_output=True, timeout=10)
    assert result.returncode == 1
    assert str(port) in result.stderr and "--port" in result.stderr


def test_stale_hub_metadata_is_reused(repository: tuple[Path, dict[str, str]]) -> None:
    root, env = repository
    with socket.socket() as available:
        available.bind(("127.0.0.1", 0))
        port = available.getsockname()[1]
    write_hub_json(root, {"port": port, "hub_id": "persistent-id", "pid": 999999})
    process = start(root, env)
    try:
        info = await_hub(root, process)
        assert (info["port"], info["hub_id"]) == (port, "persistent-id")
    finally:
        stop(root, env, process)


def test_first_free_port_is_selected(monkeypatch: pytest.MonkeyPatch) -> None:
    attempted = []

    class FakeSocket:
        def setsockopt(self, *_args: object) -> None:
            pass

        def bind(self, address: tuple[str, int]) -> None:
            attempted.append(address[1])
            if address[1] == 8420:
                raise OSError(errno.EADDRINUSE, "occupied")

        def listen(self) -> None:
            pass

        def close(self) -> None:
            pass

    monkeypatch.setattr("robomate.cli.socket.socket", lambda *_args: FakeSocket())
    _, port = _bind("127.0.0.1", None)
    assert (attempted, port) == ([8420, 8421], 8421)


def test_concurrent_up_only_starts_one_hub(repository: tuple[Path, dict[str, str]]) -> None:
    root, env = repository
    processes = [start(root, env), start(root, env)]
    try:
        for _ in range(200):
            states = [process.poll() is None for process in processes]
            if sum(states) == 1 and read_hub_json(root):
                break
            time.sleep(0.05)
        else:
            raise AssertionError("expected exactly one live hub")
        winner = next(process for process in processes if process.poll() is None)
        loser = next(process for process in processes if process.poll() is not None)
        assert await_hub(root, winner)["pid"] == winner.pid
        assert loser.stderr is not None
        assert "already" in loser.stderr.read()
        stop(root, env, winner)
    finally:
        for process in processes:
            if process.poll() is None:
                process.terminate()
                process.wait(timeout=10)


def test_down_reports_auth_failure(repository: tuple[Path, dict[str, str]]) -> None:
    root, env = repository
    process = start(root, env)
    try:
        info = await_hub(root, process)
        wrong = {**env, "ROBOMATE_HUB_URL": str(info["url"]), "ROBOMATE_TOKEN": "wrong"}
        response = subprocess.run(
            [CLI, "down"], cwd=root, env=wrong, capture_output=True, text=True, timeout=10
        )
        assert response.returncode == 1
        assert "HTTP 401" in response.stderr
        assert "unreachable" not in response.stderr
        stop(root, env, process)
    finally:
        if process.poll() is None:
            process.terminate()
            process.wait(timeout=10)
