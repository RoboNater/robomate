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
from agent_hub.database import initialize_database
from agent_hub.store import HubStore
from agent_hub_common import AgentProfile
from agent_hub_common.discovery import read_hub_json, write_hub_json
from agent_hub_common.registry import process_alive, register, registry_path
from robomate.cli import _bind

CLI = str(Path(sys.executable).with_name("robomate.exe" if sys.platform == "win32" else "robomate"))


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
    env = {
        **os.environ,
        "XDG_STATE_HOME": str(tmp_path / "xdg"),
        # registry_path() uses %LOCALAPPDATA% on Windows, so isolate it too;
        # otherwise the tests read and prune the operator's real hub registry.
        "LOCALAPPDATA": str(tmp_path / "localappdata"),
    }
    return root, env


def start(root: Path, env: dict[str, str], *args: str) -> subprocess.Popen[str]:
    return subprocess.Popen(
        [CLI, "up", *args], cwd=root, env=env, text=True,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )


def await_hub(root: Path, process: subprocess.Popen[str]) -> dict[str, object]:
    for _ in range(200):
        data = read_hub_json(root)
        pid = int((data or {}).get("pid") or 0)
        if data and pid:
            if pid == process.pid:
                return data
            if sys.platform == "win32" and process.poll() is None and process_alive(pid):
                # On Windows the console-script launcher spawns the hub in a
                # child python process, so hub.json records the server PID
                # rather than the launcher PID tracked by Popen.
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
        winner_info = await_hub(root, winner)
        if sys.platform != "win32":
            assert winner_info["pid"] == winner.pid
        else:
            winner_pid = winner_info.get("pid")
            assert isinstance(winner_pid, int) and process_alive(winner_pid)
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


def test_status_from_nested_directory_and_worktree(
    repository: tuple[Path, dict[str, str]], tmp_path: Path
) -> None:
    root, env = repository
    nested = root / "nested"
    nested.mkdir()
    subprocess.run(["git", "-c", "user.name=Test", "-c", "user.email=test@example.com",
                    "commit", "--allow-empty", "-m", "start"], cwd=root, check=True,
                   capture_output=True)
    worktree = tmp_path / "linked"
    subprocess.run(["git", "worktree", "add", "-b", "linked", str(worktree)],
                   cwd=root, check=True, capture_output=True)
    process = start(root, env)
    try:
        info = await_hub(root, process)
        for cwd in (nested, worktree):
            result = subprocess.run([CLI, "status"], cwd=cwd, env=env, text=True,
                                    capture_output=True, timeout=10)
            assert result.returncode == 0, result.stderr
            assert str(root) in result.stdout and str(info["url"]) in result.stdout
            assert "Workflow: none" in result.stdout
            result = subprocess.run([CLI, "status", "--json"], cwd=cwd, env=env,
                                    text=True, capture_output=True, timeout=10)
            assert json.loads(result.stdout)["repo_root"] == str(root)
        stop(root, env, process)
        stopped = subprocess.run([CLI, "status"], cwd=worktree, env=env, text=True,
                                 capture_output=True, timeout=10)
        assert stopped.returncode != 0
        assert "not running" in stopped.stdout
        assert str(info["url"]) in stopped.stdout
        assert str(info["port"]) in stopped.stdout
    finally:
        if process.poll() is None:
            process.terminate()
            process.wait(timeout=10)


def test_status_renders_seeded_workflow_and_agents(
    repository: tuple[Path, dict[str, str]]
) -> None:
    root, env = repository
    directory = root / ".robomate"
    directory.mkdir()
    database_path = directory / "hub.db"
    initialize_database(database_path)
    hub_store = HubStore(database_path)
    hub_store.initialize_workflow("\n\nBuild issue #43\nmore detail")
    hub_store.check_in("bob", AgentProfile(harness="codex", model="gpt-6-sol"))
    task = hub_store.assign_task("bob", "implementer", "Build", "secret instructions")
    hub_store.open_question(task.id, "bob", "secret question", "q1")
    hub_store.check_in("dave", AgentProfile())
    hub_store.release_agent("dave")
    process = start(root, env)
    try:
        info = await_hub(root, process)
        token = (directory / "token").read_text().strip()
        request = urllib.request.Request(
            f"{info['url']}/rpc",
            data=json.dumps({"jsonrpc": "2.0", "id": 1, "method": "hub.heartbeat"}).encode(),
            headers={"Authorization": f"Bearer {token}",
                     "Content-Type": "application/json",
                     "X-Robomate-Actor": "alice",
                     "X-Robomate-Session": "5f0c6f0e-8f3c-4d57-9d0a-0b8b1c1e2f3a"},
        )
        with urllib.request.urlopen(request, timeout=10) as response:
            assert json.load(response)["result"] == {"ok": True}
        result = subprocess.run([CLI, "status"], cwd=root, env=env, text=True,
                                capture_output=True, timeout=10)
        assert result.returncode == 0, result.stderr
        assert "Workflow: active — Build issue #43" in result.stdout
        assert "Orchestrator: alice" in result.stdout
        assert f"bob: codex / gpt-6-sol  alive  task {task.id}" in result.stdout
        assert "dave: unknown / unknown  released" in result.stdout
        assert f"{task.id}: implementer  bob  input-required" in result.stdout
        assert "Pending questions: 1" in result.stdout
        assert "secret instructions" not in result.stdout
        assert "secret question" not in result.stdout
    finally:
        stop(root, env, process)


def test_status_reports_auth_failure_without_calling_hub_stopped(
    repository: tuple[Path, dict[str, str]]
) -> None:
    root, env = repository
    process = start(root, env)
    try:
        await_hub(root, process)
        token_file = root / ".robomate/token"
        original = token_file.read_text()
        try:
            token_file.write_text("incorrect-token")
            result = subprocess.run([CLI, "status"], cwd=root, env=env, text=True,
                                    capture_output=True, timeout=10)
            assert result.returncode == 1
            assert "HTTP 401" in result.stderr
            assert "not running" not in result.stdout
        finally:
            token_file.write_text(original)
    finally:
        stop(root, env, process)


def test_status_uses_registry_fallback_and_explicit_url(
    repository: tuple[Path, dict[str, str]], tmp_path: Path
) -> None:
    root, env = repository
    other = tmp_path / "worker-clone"
    other.mkdir()
    subprocess.run(["git", "init", "-b", "main"], cwd=other, check=True,
                   capture_output=True)
    process = start(root, env)
    try:
        info = await_hub(root, process)
        fallback = subprocess.run([CLI, "status"], cwd=other, env=env, text=True,
                                  capture_output=True, timeout=10)
        assert fallback.returncode == 0, fallback.stderr
        assert str(root) in fallback.stdout and str(info["url"]) in fallback.stdout
        explicit_env = {**env, "ROBOMATE_HUB_URL": str(info["url"]),
                        "ROBOMATE_TOKEN_FILE": str(root / ".robomate/token")}
        explicit = subprocess.run([CLI, "status"], cwd=tmp_path, env=explicit_env,
                                  text=True, capture_output=True, timeout=10)
        assert explicit.returncode == 0, explicit.stderr
        assert str(root) in explicit.stdout and str(info["url"]) in explicit.stdout
    finally:
        stop(root, env, process)


def test_status_with_unreachable_explicit_url_does_not_claim_current_repo(
    repository: tuple[Path, dict[str, str]]
) -> None:
    root, env = repository
    explicit_env = {**env, "ROBOMATE_HUB_URL": "http://127.0.0.1:1",
                    "ROBOMATE_TOKEN": "test-token"}
    result = subprocess.run([CLI, "status", "--json"], cwd=root, env=explicit_env,
                            text=True, capture_output=True, timeout=10)
    assert result.returncode == 1
    assert json.loads(result.stdout) == {
        "running": False, "repo_root": None, "url": "http://127.0.0.1:1", "port": 1,
    }
    human = subprocess.run([CLI, "status"], cwd=root, env=explicit_env,
                           text=True, capture_output=True, timeout=10)
    assert human.returncode == 1
    assert human.stdout.startswith("Hub: not running")
    assert str(root) not in human.stdout


def test_ls_lists_two_live_hubs_and_prunes_stale_entry(
    repository: tuple[Path, dict[str, str]], tmp_path: Path
) -> None:
    root, env = repository
    other = tmp_path / "other"
    other.mkdir()
    subprocess.run(["git", "init", "-b", "main"], cwd=other, check=True,
                   capture_output=True)
    subprocess.run(["git", "remote", "add", "origin", "git@github.com:example/other.git"],
                   cwd=other, check=True)
    subprocess.run(["git", "symbolic-ref", "refs/remotes/origin/HEAD",
                    "refs/remotes/origin/main"], cwd=other, check=True)
    first = start(root, env)
    second = start(other, env)
    try:
        first_info = await_hub(root, first)
        second_info = await_hub(other, second)
        register({"repo_root": "/stale", "url": "http://127.0.0.1:1",
                  "hub_id": "stale", "pid": 999999}, env)
        result = subprocess.run([CLI, "ls"], cwd=root, env=env, text=True,
                                capture_output=True, timeout=10)
        assert result.returncode == 0, result.stderr
        lines = result.stdout.splitlines()
        assert len(lines) == 2
        assert any(str(root) in line and str(first_info["url"]) in line for line in lines)
        assert any(str(other) in line and str(second_info["url"]) in line for line in lines)
        assert all("agents 0" in line and "workflow none" in line for line in lines)
        assert "stale" not in registry_path(env).read_text()
        result = subprocess.run([CLI, "ls", "--json"], cwd=root, env=env, text=True,
                                capture_output=True, timeout=10)
        assert len(json.loads(result.stdout)) == 2
    finally:
        for repo, process in ((root, first), (other, second)):
            if process.poll() is None:
                stop(repo, env, process)
