"""End-to-end operator CLI tests in a disposable Git checkout."""

import errno
import json
import os
import queue
import re
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
import robomate.cli as cli
from agent_hub.database import database, initialize_database
from agent_hub.store import HubStore
from agent_hub_common import AgentProfile
from agent_hub_common.discovery import read_hub_json, write_hub_json
from agent_hub_common.registry import process_alive, register, registry_path
from robomate.cli import _bind

CLI = str(Path(sys.executable).with_name("robomate.exe" if sys.platform == "win32" else "robomate"))


def init_checkout(root: Path) -> None:
    root.mkdir(parents=True)
    subprocess.run(["git", "init", "-b", "main"], cwd=root, check=True, capture_output=True)
    subprocess.run(
        ["git", "remote", "add", "origin", "git@github.com:example/repo.git"],
        cwd=root,
        check=True,
    )
    subprocess.run(
        ["git", "symbolic-ref", "refs/remotes/origin/HEAD", "refs/remotes/origin/main"],
        cwd=root,
        check=True,
    )


@pytest.fixture
def repository(tmp_path: Path) -> tuple[Path, dict[str, str]]:
    root = tmp_path / "repo"
    init_checkout(root)
    env = {
        **os.environ,
        "XDG_STATE_HOME": str(tmp_path / "xdg"),
        # registry_path() uses %LOCALAPPDATA% on Windows, so isolate it too;
        # otherwise the tests read and prune the operator's real hub registry.
        "LOCALAPPDATA": str(tmp_path / "localappdata"),
    }
    return root, env


def start(root: Path, env: dict[str, str], *args: str) -> subprocess.Popen[str]:
    """Start `robomate up` with hub output sent to files, not pipes.

    Nothing reads the hub's output while it runs, so pipes both leak handles
    (#121) and risk blocking the hub once the pipe buffer fills. Files remove
    both risks; use _hub_stdout/_hub_stderr to read them after exit.
    """
    xdg = env.get("XDG_STATE_HOME")
    parent = Path(xdg).parent if xdg else Path(tempfile.gettempdir())
    log_dir = Path(tempfile.mkdtemp(prefix="hub-logs-", dir=parent if parent.is_dir() else None))
    stdout_path = log_dir / "stdout.log"
    stderr_path = log_dir / "stderr.log"
    with open(stdout_path, "w") as out, open(stderr_path, "w") as err:
        process = subprocess.Popen(
            [CLI, "up", *args],
            cwd=root,
            env=env,
            text=True,
            stdout=out,
            stderr=err,
        )
    process._stdout_log = stdout_path  # type: ignore[attr-defined]
    process._stderr_log = stderr_path  # type: ignore[attr-defined]
    return process


def _hub_stdout(process: subprocess.Popen[str]) -> str:
    path = getattr(process, "_stdout_log", None)
    if path is not None:
        return Path(path).read_text(errors="replace")
    assert process.stdout is not None
    return process.stdout.read()


def _hub_stderr(process: subprocess.Popen[str]) -> str:
    path = getattr(process, "_stderr_log", None)
    if path is not None:
        return Path(path).read_text(errors="replace")
    assert process.stderr is not None
    return process.stderr.read()


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
            raise AssertionError(f"hub exited early: {_hub_stderr(process)}")
        time.sleep(0.05)
    raise AssertionError("hub did not start")


def stop(root: Path, env: dict[str, str], process: subprocess.Popen[str]) -> None:
    try:
        result = subprocess.run(
            [CLI, "down"], cwd=root, env=env, text=True, capture_output=True, timeout=10
        )
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
        second = subprocess.run(
            [CLI, "up"], cwd=root, env=env, text=True, capture_output=True, timeout=10
        )
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


def _commit_and_add_worktrees(root: Path, *paths: Path) -> None:
    subprocess.run(
        ["git", "-c", "user.name=T", "-c", "user.email=t@example.com", "commit"]
        + ["--allow-empty", "-m", "start"],
        cwd=root,
        check=True,
        capture_output=True,
    )
    for path in paths:
        subprocess.run(
            ["git", "worktree", "add", "-b", path.name, str(path)],
            cwd=root,
            check=True,
            capture_output=True,
        )


def _cli(cwd: Path, env: dict[str, str], *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [CLI, *args], cwd=cwd, env=env, text=True, capture_output=True, timeout=30
    )


def test_two_hubs_in_one_repository(
    repository: tuple[Path, dict[str, str]], tmp_path: Path
) -> None:
    """#147 Tests 1, 2, 3 and 5: worktree hubs side by side, selected, never attached."""

    root, env = repository
    wt_a, wt_b = tmp_path / "wt-a", tmp_path / "wt-b"
    _commit_and_add_worktrees(root, wt_a, wt_b)
    hub_a, hub_b = start(wt_a, env), start(wt_b, env)
    try:
        a, b = await_hub(wt_a, hub_a), await_hub(wt_b, hub_b)
        assert (a["name"], b["name"]) == ("wt-a", "wt-b")
        assert a["port"] != b["port"] and a["hub_id"] != b["hub_id"]
        for checkout, info in ((wt_a, a), (wt_b, b)):
            assert (info["checkout"], info["repo_root"]) == (str(checkout), str(checkout))
            assert info["git_common_dir"] == str(root / ".git")
            assert (checkout / ".robomate" / "token").is_file()
        assert not (root / ".robomate").exists()
        for _ in range(200):
            printed = _hub_stdout(hub_a)
            if "ROBOMATE_TOKEN_FILE=" in printed:
                break
            time.sleep(0.05)
        assert f"Hub wt-a running at {a['url']}" in printed
        assert f"Hub state: {wt_a / '.robomate'}" in printed
        assert "`git worktree remove` deletes it" in printed

        listing = _cli(root, env, "ls")
        assert listing.returncode == 0, listing.stderr
        for checkout, info in ((wt_a, a), (wt_b, b)):
            line = next(x for x in listing.stdout.splitlines() if x.startswith(f"{info['name']} "))
            assert "live" in line and str(info["url"]) in line
            assert f"checkout {checkout}" in line and f"repository {root}" in line

        # The main checkout owns no hub: both commands refuse and list both.
        for command in (["down"], ["status"]):
            refused = _cli(root, env, *command)
            assert refused.returncode == 1
            assert "wt-a" in refused.stderr and "wt-b" in refused.stderr
        assert hub_a.poll() is None and hub_b.poll() is None
        selected = _cli(root, env, "status", "--hub", "wt-b")
        assert selected.returncode == 0, selected.stderr
        assert f"Checkout: {wt_b}" in selected.stdout

        stop(wt_a, env, hub_a)
        running = _cli(wt_b, env, "status")
        assert running.returncode == 0 and f"Hub: wt-b  {b['url']}" in running.stdout
        listing = _cli(root, env, "ls", "--json")
        rows = {row["name"]: row for row in json.loads(listing.stdout)}
        assert (rows["wt-a"]["live"], rows["wt-b"]["live"]) == (False, True)
        assert rows["wt-a"]["state_dir"] == str(wt_a / ".robomate")

        renamed = _cli(wt_a, env, "up", "--name", "other")
        assert renamed.returncode == 1 and "fixed once recorded" in renamed.stderr
        hub_a = start(wt_a, env)
        again = await_hub(wt_a, hub_a)
        assert (again["hub_id"], again["name"], again["port"]) == (a["hub_id"], "wt-a", a["port"])
    finally:
        for checkout, process in ((wt_a, hub_a), (wt_b, hub_b)):
            if process.poll() is None:
                stop(checkout, env, process)


def test_hub_names_are_validated_and_unique_per_repository(
    repository: tuple[Path, dict[str, str]], tmp_path: Path
) -> None:
    root, env = repository
    first, second = tmp_path / "a" / "feature", tmp_path / "b" / "feature"
    _commit_and_add_worktrees(root, first)
    subprocess.run(
        ["git", "worktree", "add", "-b", "feature-b", str(second)],
        cwd=root,
        check=True,
        capture_output=True,
    )
    invalid = _cli(second, env, "up", "--name", "Feature.B")
    assert invalid.returncode == 1 and "invalid hub name" in invalid.stderr
    process = start(first, env)
    try:
        await_hub(first, process)
        taken = _cli(second, env, "up")
        assert taken.returncode == 1
        assert "'feature' is taken" in taken.stderr and "--name" in taken.stderr
        assert read_hub_json(second) is None
    finally:
        stop(first, env, process)
    # Stopped, the hub keeps its name.
    assert _cli(second, env, "up").returncode == 1
    named = start(second, env, "--name", "feature-b")
    try:
        assert await_hub(second, named)["name"] == "feature-b"
    finally:
        stop(second, env, named)


def test_bare_repository_hub_runs_in_a_linked_worktree(
    repository: tuple[Path, dict[str, str]], tmp_path: Path
) -> None:
    """#147 Test 4, on an unmodified `git clone --bare`: it has no origin/HEAD."""

    root, env = repository
    _commit_and_add_worktrees(root)
    bare = tmp_path / "r" / ".bare"
    subprocess.run(
        ["git", "clone", "--bare", str(root), str(bare)], check=True, capture_output=True
    )
    main, feature = tmp_path / "r" / "main", tmp_path / "r" / "feature"
    for args in (
        ["worktree", "add", str(main), "main"],
        ["worktree", "add", "-b", "feature", str(feature)],
    ):
        subprocess.run(["git", *args], cwd=bare, check=True, capture_output=True)
    refused = _cli(bare, env, "up", "--forge", "github")
    assert refused.returncode == 1 and "bare repository" in refused.stderr
    # The worktree on another branch still records the repository's default branch.
    processes = {
        main: start(main, env, "--forge", "github"),
        feature: start(feature, env, "--forge", "github"),
    }
    try:
        for checkout, process in processes.items():
            info = await_hub(checkout, process)
            assert (info["name"], info["git_common_dir"]) == (checkout.name, str(bare))
            assert info["default_branch"] == "main"
        assert "/.robomate/" in (bare / "info" / "exclude").read_text()
    finally:
        for checkout, process in processes.items():
            if process.poll() is None:
                stop(checkout, env, process)


def test_legacy_hub_keeps_its_state_and_gets_a_name(
    repository: tuple[Path, dict[str, str]],
) -> None:
    """#147 Test 6: hub.json and a registry entry as written before checkout scope."""

    root, env = repository
    with socket.socket() as available:
        available.bind(("127.0.0.1", 0))
        port = available.getsockname()[1]
    legacy = {
        "repo_root": str(root),
        "origin": "git@github.com:example/repo.git",
        "forge": "github",
        "default_branch": "main",
        "url": f"http://127.0.0.1:{port}",
        "port": port,
        "pid": None,
        "started_at": None,
        "robomate_version": "0.1.0",
        "hub_id": "legacy-id",
    }
    write_hub_json(root, legacy)
    (root / ".robomate" / "token").write_text("legacy-token\n")
    (root / ".robomate" / "token").chmod(0o600)
    register({k: legacy[k] for k in ("repo_root", "url", "hub_id")} | {"pid": 999999}, env)
    status = _cli(root, env, "status", "--json")
    assert status.returncode == 1 and json.loads(status.stdout)["repo_root"] == str(root)
    process = start(root, env)
    try:
        info = await_hub(root, process)
        assert (info["hub_id"], info["port"], info["name"]) == ("legacy-id", port, "repo")
        assert (root / ".robomate" / "token").read_text() == "legacy-token\n"
        entries = json.loads(registry_path(env).read_text())
        assert [(e["hub_id"], e["name"]) for e in entries] == [("legacy-id", "repo")]
    finally:
        stop(root, env, process)


def _shutdown_with_bearer_only(url: str, token: str) -> dict[str, Any]:
    request = urllib.request.Request(
        f"{url}/rpc",
        data=json.dumps({"jsonrpc": "2.0", "id": 1, "method": "hub.shutdown"}).encode(),
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
    )
    with urllib.request.urlopen(request, timeout=10) as response:
        body: dict[str, Any] = json.load(response)
    return body


def test_up_creates_the_operator_token_beside_the_registry(
    repository: tuple[Path, dict[str, str]],
) -> None:
    """#128: agents hold the bearer token; only the operator holds this one."""

    root, env = repository
    operator_file = registry_path(env).with_name("operator-token")
    process = start(root, env)
    try:
        info = await_hub(root, process)
        operator_token = operator_file.read_text(encoding="utf-8").strip()
        assert len(operator_token) >= 32
        if sys.platform != "win32":
            assert operator_file.stat().st_mode & 0o777 == 0o600
        bearer = (root / ".robomate/token").read_text(encoding="utf-8").strip()
        refused = _shutdown_with_bearer_only(str(info["url"]), bearer)
        assert refused["error"]["code"] == -32005
        assert process.poll() is None
        stop(root, env, process)
        # Once stopped: on Windows a running hub holds up.lock unreadable.
        for path in root.rglob("*"):
            if path.is_file() and ".git" not in path.parts:
                assert operator_token not in path.read_text(errors="replace"), path
        assert process.poll() is not None
        printed = _hub_stdout(process) + _hub_stderr(process)
        assert operator_token not in printed and str(operator_file) not in printed
        assert "operator-token" not in printed
        # A restart reuses the credential rather than minting a new one.
        restarted = start(root, env)
        try:
            await_hub(root, restarted)
            assert operator_file.read_text(encoding="utf-8").strip() == operator_token
        finally:
            stop(root, env, restarted)
    finally:
        if process.poll() is None:
            process.terminate()
            process.wait(timeout=10)


def test_operator_token_file_override_is_honoured_but_never_in_the_repository(
    repository: tuple[Path, dict[str, str]], tmp_path: Path
) -> None:
    root, env = repository
    elsewhere = tmp_path / "elsewhere" / "operator-token"
    moved = {**env, "ROBOMATE_OPERATOR_TOKEN_FILE": str(elsewhere)}
    process = start(root, moved)
    try:
        await_hub(root, process)
        assert elsewhere.exists()
        assert not registry_path(env).with_name("operator-token").exists()
        stop(root, moved, process)
    finally:
        if process.poll() is None:
            process.terminate()
            process.wait(timeout=10)

    for inside in (root / ".robomate" / "operator-token", root / "operator-token"):
        result = subprocess.run(
            [CLI, "up"],
            cwd=root,
            env={**env, "ROBOMATE_OPERATOR_TOKEN_FILE": str(inside)},
            text=True,
            capture_output=True,
            timeout=10,
        )
        assert result.returncode == 1
        assert "must not be inside the repository" in result.stderr
        assert not inside.exists()


def test_isolated_smoke_hub_leaves_the_machine_state_untouched(
    repository: tuple[Path, dict[str, str]], tmp_path: Path
) -> None:
    """A worker's smoke run under the guide's limits writes nothing outside them (#145)."""

    _, machine = repository
    # The worker's MCP environment names the run's hub.
    machine = {
        **machine,
        "ROBOMATE_HUB_URL": "http://127.0.0.1:9",
        "ROBOMATE_TOKEN": "run-hub-token",
        "ROBOMATE_TOKEN_FILE": str(tmp_path / "run-hub-token"),
    }
    smoke = tmp_path / "smoke"
    root = smoke / "repo"
    init_checkout(root)
    env = {
        key: value
        for key, value in machine.items()
        if key not in {"ROBOMATE_HUB_URL", "ROBOMATE_TOKEN", "ROBOMATE_TOKEN_FILE"}
    }
    env.update(
        {
            "HUB_STATE_DIR": str(smoke),
            "ROBOMATE_OPERATOR_TOKEN_FILE": str(smoke / "operator-token"),
            "XDG_STATE_HOME": str(smoke / "state"),
            "LOCALAPPDATA": str(smoke / "state"),
        }
    )
    process = start(root, env)
    try:
        info = await_hub(root, process)
        registry = registry_path(env)
        assert registry.is_relative_to(smoke)
        # hub.json is written just before the registry entry.
        for _ in range(200):
            if registry.exists():
                break
            time.sleep(0.05)
        assert [entry["hub_id"] for entry in json.loads(registry.read_text())] == [info["hub_id"]]
        assert (smoke / "operator-token").exists()
        stop(root, env, process)
    finally:
        if process.poll() is None:
            process.terminate()
            process.wait(timeout=10)

    assert not registry_path(machine).parent.exists()
    assert not (tmp_path / "run-hub-token").exists()
    # Only the fixture's unused checkout lies outside the smoke directory.
    assert {path.name for path in tmp_path.iterdir()} == {"repo", "smoke"}


def test_recorded_busy_port_is_refused(repository: tuple[Path, dict[str, str]]) -> None:
    root, env = repository
    with socket.socket() as occupied:
        occupied.bind(("127.0.0.1", 0))
        occupied.listen()
        port = occupied.getsockname()[1]
        write_hub_json(root, {"port": port, "hub_id": "old"})
        result = subprocess.run(
            [CLI, "up"], cwd=root, env=env, text=True, capture_output=True, timeout=10
        )
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
        assert "already" in _hub_stderr(loser)
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
    subprocess.run(
        [
            "git",
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.com",
            "commit",
            "--allow-empty",
            "-m",
            "start",
        ],
        cwd=root,
        check=True,
        capture_output=True,
    )
    worktree = tmp_path / "linked"
    subprocess.run(
        ["git", "worktree", "add", "-b", "linked", str(worktree)],
        cwd=root,
        check=True,
        capture_output=True,
    )
    process = start(root, env)
    try:
        info = await_hub(root, process)
        # The linked worktree owns no hub, so it reaches the main checkout's
        # hub only when selected (#147).
        refused = subprocess.run(
            [CLI, "status"], cwd=worktree, env=env, text=True, capture_output=True, timeout=10
        )
        assert refused.returncode == 1
        assert f"no hub is recorded for checkout {worktree}" in refused.stderr
        assert "--hub" in refused.stderr and str(info["url"]) in refused.stderr
        for cwd, selector in (
            (nested, []),
            (worktree, ["--hub", "repo"]),
            (tmp_path, ["--hub", str(root)]),
        ):
            result = subprocess.run(
                [CLI, "status", *selector],
                cwd=cwd,
                env=env,
                text=True,
                capture_output=True,
                timeout=10,
            )
            assert result.returncode == 0, result.stderr
            assert "Hub: repo  " in result.stdout and str(info["url"]) in result.stdout
            assert f"Checkout: {root}" in result.stdout
            assert f"Repository: {root}" in result.stdout
            assert f"State: {root / '.robomate'}" in result.stdout
            assert "Workflow: none" in result.stdout
            result = subprocess.run(
                [CLI, "status", "--json", *selector],
                cwd=cwd,
                env=env,
                text=True,
                capture_output=True,
                timeout=10,
            )
            status = json.loads(result.stdout)
            assert (status["repo_root"], status["checkout"]) == (str(root), str(root))
            assert (status["name"], status["git_common_dir"]) == ("repo", str(root / ".git"))
        stop(root, env, process)
        stopped = subprocess.run(
            [CLI, "status"], cwd=nested, env=env, text=True, capture_output=True, timeout=10
        )
        assert stopped.returncode != 0
        assert f"Hub repo ({root}): not running" in stopped.stdout
        assert str(info["url"]) in stopped.stdout
        assert str(info["port"]) in stopped.stdout
    finally:
        if process.poll() is None:
            process.terminate()
            process.wait(timeout=10)


def test_status_renders_seeded_workflow_and_agents(repository: tuple[Path, dict[str, str]]) -> None:
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
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
                "X-Robomate-Actor": "alice",
                "X-Robomate-Session": "5f0c6f0e-8f3c-4d57-9d0a-0b8b1c1e2f3a",
            },
        )
        with urllib.request.urlopen(request, timeout=10) as response:
            assert json.load(response)["result"] == {"ok": True}
        result = subprocess.run(
            [CLI, "status"], cwd=root, env=env, text=True, capture_output=True, timeout=10
        )
        assert result.returncode == 0, result.stderr
        assert "Workflow: active — Build issue #43" in result.stdout
        assert re.search(
            r"Orchestrator: alice  session \S+  last seen \d+s ago\n", result.stdout
        ), result.stdout
        assert f"bob: codex / gpt-6-sol  alive  task {task.id}" in result.stdout
        assert "dave: unknown / unknown  released" in result.stdout
        assert f"{task.id}: implementer  bob  input-required" in result.stdout
        assert "Pending questions: 1" in result.stdout
        assert "secret instructions" not in result.stdout
        assert "secret question" not in result.stdout
    finally:
        stop(root, env, process)


def _rpc_as_alice(url: str, token: str, method: str) -> dict[str, Any]:
    request = urllib.request.Request(
        f"{url}/rpc",
        data=json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": {}}).encode(),
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "X-Robomate-Actor": "alice",
            "X-Robomate-Session": "5f0c6f0e-8f3c-4d57-9d0a-0b8b1c1e2f3a",
        },
    )
    with urllib.request.urlopen(request, timeout=10) as response:
        body: dict[str, Any] = json.load(response)
    return body


def test_status_shows_stalled_agents_with_evidence(repository: tuple[Path, dict[str, str]]) -> None:
    """#144: a fresh bridge heartbeat with no hub call reads STALLED, with its evidence."""

    root, env = repository
    directory = root / ".robomate"
    directory.mkdir()
    database_path = directory / "hub.db"
    initialize_database(database_path)
    hub_store = HubStore(database_path)
    # About 60 ms, so the test need not wait minutes for the threshold.
    hub_store.initialize_workflow("Build issue #43", {"stall_after_min": 0.001})
    hub_store.check_in("bob", AgentProfile(harness="codex", model="gpt-6-sol"))
    task = hub_store.assign_task("bob", "implementer", "Build", "secret instructions")
    process = start(root, env)
    try:
        info = await_hub(root, process)
        token = (directory / "token").read_text().strip()
        assert _rpc_as_alice(str(info["url"]), token, "hub.heartbeat")["result"] == {"ok": True}
        time.sleep(0.2)
        result = subprocess.run(
            [CLI, "status"], cwd=root, env=env, text=True, capture_output=True, timeout=10
        )
        assert result.returncode == 0, result.stderr
        out = result.stdout
        assert re.search(
            r"Orchestrator: alice .*\n  STALLED — no hub call for \d+s \(none seen\);"
            r" event 1 \(agent_checked_in\) queued \d+s undelivered; bridge heartbeat \d+s ago;"
            r" threshold 0s\n",
            out,
        ), out
        assert f"bob: codex / gpt-6-sol  STALLED  task {task.id}\n" in out
        assert re.search(
            rf"    no hub call for \d+s \(none seen\); task {task.id} no progress for \d+s;"
            r" bridge heartbeat \d+s ago; threshold 0s\n",
            out,
        ), out
        assert cli.STALL_NOTE in out
        assert "secret instructions" not in out

        status = json.loads(
            subprocess.run(
                [CLI, "status", "--json"],
                cwd=root,
                env=env,
                text=True,
                capture_output=True,
                timeout=10,
            ).stdout
        )
        (bob,) = status["agents"]
        assert bob["stalled"] is True and bob["status"] == "busy" and bob["alive"] is True
        assert bob["activity"]["reasons"] == ["no_hub_call", "no_task_progress"]
        assert status["orchestrator_activity"]["reasons"] == ["no_hub_call", "event_backlog"]
        assert status["orchestrator_activity"]["stale_events"][0]["id"] == 1
        assert status["stall_after_min"] == 0.001
        # Reading status changed nothing: no session took over, no event moved.
        with database(database_path) as connection:
            assert connection.execute("SELECT COUNT(*) FROM rpc_audit").fetchone()[0] == 0
            assert tuple(
                connection.execute(
                    "SELECT state, delivery_attempts FROM event WHERE id = 1"
                ).fetchone()
            ) == ("queued", 0)
    finally:
        stop(root, env, process)


def test_status_snapshot_matches_get_state_and_changes_nothing(
    repository: tuple[Path, dict[str, str]],
) -> None:
    """#146: the before-snapshot, from bearer-only reads, without superseding Alice."""

    root, env = repository
    directory = root / ".robomate"
    directory.mkdir()
    database_path = directory / "hub.db"
    initialize_database(database_path)
    hub_store = HubStore(database_path)
    workflow_id = hub_store.initialize_workflow("Build issue #43")
    hub_store.check_in("bob", AgentProfile(harness="codex", model="gpt-6-sol"))
    task = hub_store.assign_task("bob", "implementer", "Build", "secret instructions")
    delivered = hub_store.lease_next_event()
    assert delivered is not None
    process = start(root, env)
    try:
        info = await_hub(root, process)
        url = str(info["url"])
        token = (directory / "token").read_text().strip()
        state = _rpc_as_alice(url, token, "get_state")["result"]

        def robomate(*args: str) -> subprocess.CompletedProcess[str]:
            return subprocess.run(
                [CLI, *args], cwd=root, env=env, text=True, capture_output=True, timeout=10
            )

        result = robomate(
            "status", "--snapshot", "--json", "--stopped-at", "2026-10-08T14:00:00+02:00"
        )
        assert result.returncode == 0, result.stderr
        snapshot = json.loads(result.stdout)
        assert snapshot["hub_id"] == info["hub_id"]
        assert snapshot["stopped_at"] == "2026-10-08T12:00:00Z"
        assert snapshot["workflow"] == {"id": workflow_id, "status": "active"}
        assert snapshot["orchestrator"]["session"] == "5f0c6f0e-8f3c-4d57-9d0a-0b8b1c1e2f3a"
        open_tasks = [t for t in state["tasks"] if t["state"] not in TERMINAL]
        assert [(t["id"], t["assignee"], t["role"], t["state"]) for t in snapshot["tasks"]] == [
            (t["id"], t["assignee"], t["role"], t["state"]) for t in open_tasks
        ]
        assert [
            (
                d["event_id"],
                d["delivery_id"],
                d["attempt"],
                d["delivered_at"],
                d["delivery_expires"],
            )
            for d in snapshot["deliveries"]
        ] == [
            (
                e["id"],
                e["delivery_id"],
                e["delivery_attempts"],
                e["delivered_at"],
                e["delivery_expires"],
            )
            for e in state["unacked_delivered"]
        ]
        assert snapshot["deliveries"][0]["delivery_id"] == delivered.delivery_id

        text = robomate("status", "--snapshot")
        assert text.returncode == 0, text.stderr
        assert (
            text.stdout
            == cli.render_snapshot(
                json.loads(robomate("status", "--snapshot", "--json").stdout)
                | {"taken_at": _taken_at(text.stdout)}
            )
            + "\n"
        )
        assert "- Old process stop time: not supplied\n" in text.stdout
        assert f"  - task {task.id}: owner bob, role implementer, state submitted\n" in text.stdout
        assert "secret instructions" not in text.stdout

        bad = robomate("status", "--snapshot", "--stopped-at", "yesterday")
        assert bad.returncode == 1 and "ISO 8601" in bad.stderr
        lone = robomate("status", "--stopped-at", "2026-10-08T12:00:00Z")
        assert lone.returncode == 1 and "goes with --snapshot" in lone.stderr

        # Nothing moved: the same delivery, no takeover row, the same session.
        with database(database_path) as connection:
            assert (
                connection.execute(
                    "SELECT COUNT(*) FROM rpc_audit WHERE method = 'session.supersede'"
                ).fetchone()[0]
                == 0
            )
        after = _rpc_as_alice(url, token, "get_state")["result"]
        assert after["unacked_delivered"] == state["unacked_delivered"]
        assert after["tasks"] == state["tasks"]
    finally:
        stop(root, env, process)


TERMINAL = ("completed", "failed", "canceled")


def _taken_at(text: str) -> str:
    match = re.search(r"- Snapshot time: (\S+) ", text)
    assert match, text
    return match.group(1)


def test_inbox_answer_and_status_serve_the_operator(
    repository: tuple[Path, dict[str, str]], tmp_path: Path
) -> None:
    """#130: the operator lists open questions, answers them once, and status counts them."""

    root, env = repository
    directory = root / ".robomate"
    directory.mkdir()
    database_path = directory / "hub.db"
    initialize_database(database_path)
    hub_store = HubStore(database_path)
    hub_store.initialize_workflow("Build issue #43")
    hub_store.ask_user("Merge or wait?\x1b[2J", ["merge", "wait"], actor="alice", session=None)
    hub_store.ask_user("Which follow-up label?", None, actor="alice", session=None)

    def robomate(*args: str, environ: dict[str, str] = env) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [CLI, *args], cwd=root, env=environ, text=True, capture_output=True, timeout=10
        )

    process = start(root, env)
    try:
        await_hub(root, process)
        status = robomate("status")
        assert status.returncode == 0, status.stderr
        assert "Operator questions: 2  (see robomate inbox)" in status.stdout

        inbox = robomate("inbox")
        assert inbox.returncode == 0, inbox.stderr
        assert re.search(r"\[1\] asked \S+ \(\d+s ago\) by alice\n", inbox.stdout), inbox.stdout
        assert "    Merge or wait?\\x1b[2J\n" in inbox.stdout
        assert "\x1b" not in inbox.stdout
        assert "    --option 2: wait\n" in inbox.stdout
        assert "[2] asked" in inbox.stdout and "    Which follow-up label?\n" in inbox.stdout
        listed = json.loads(robomate("inbox", "--json").stdout)
        assert [q["question_id"] for q in listed] == [1, 2]

        # Without the operator credential the hub is never asked.
        no_token = {**env, "ROBOMATE_OPERATOR_TOKEN_FILE": str(tmp_path / "absent")}
        refused = robomate("answer", "1", "merge", environ=no_token)
        assert refused.returncode == 1 and "operator token" in refused.stderr

        for args, message in (
            (("answer", "1"), "either an answer text or --option N"),
            (("answer", "1", "merge", "--option", "1"), "either an answer text or --option N"),
            (("answer", "1", "--option", "3"), "has no option 3; choose 1 to 2"),
            (("answer", "2", "--option", "1"), "has no option 1; answer with text"),
            (("answer", "9", "--option", "1"), "question 9 is not open"),
            (("answer", "9", "yes"), "unknown operator question: 9"),
        ):
            result = robomate(*args)
            assert result.returncode == 1 and message in result.stderr, (args, result.stderr)

        answered = robomate("answer", "1", "--option", "2")
        assert answered.returncode == 0, answered.stderr
        assert answered.stdout == "Answered question 1: wait\n"
        answered = robomate("answer", "2", "needs-triage")
        assert answered.returncode == 0, answered.stderr
        again = robomate("answer", "2", "other")
        assert again.returncode == 1 and "already answered" in again.stderr

        assert robomate("inbox").stdout == "No open questions.\n"
        assert "Operator questions: 0\n" in robomate("status").stdout
        assert hub_store.operator_answer(1)["answer"] == "wait"
        assert hub_store.operator_answer(2)["answer"] == "needs-triage"
        with database(database_path) as connection:
            rows = connection.execute(
                "SELECT payload_json FROM event WHERE kind = 'user_answered' ORDER BY id"
            ).fetchall()
        assert [json.loads(row[0]) for row in rows] == [
            {"question_id": 1, "answer": "wait"},
            {"question_id": 2, "answer": "needs-triage"},
        ]
    finally:
        stop(root, env, process)


def test_status_reports_auth_failure_without_calling_hub_stopped(
    repository: tuple[Path, dict[str, str]],
) -> None:
    root, env = repository
    process = start(root, env)
    try:
        await_hub(root, process)
        token_file = root / ".robomate/token"
        original = token_file.read_text()
        try:
            token_file.write_text("incorrect-token")
            result = subprocess.run(
                [CLI, "status"], cwd=root, env=env, text=True, capture_output=True, timeout=10
            )
            assert result.returncode == 1
            assert "HTTP 401" in result.stderr
            assert "not running" not in result.stdout
        finally:
            token_file.write_text(original)
    finally:
        stop(root, env, process)


def test_status_never_attaches_to_the_only_hub_but_takes_an_explicit_url(
    repository: tuple[Path, dict[str, str]], tmp_path: Path
) -> None:
    root, env = repository
    other = tmp_path / "worker-clone"
    other.mkdir()
    subprocess.run(["git", "init", "-b", "main"], cwd=other, check=True, capture_output=True)
    process = start(root, env)
    try:
        info = await_hub(root, process)
        for command in (["status"], ["status", "--snapshot"], ["inbox"], ["down"]):
            refused = subprocess.run(
                [CLI, *command], cwd=other, env=env, text=True, capture_output=True, timeout=10
            )
            assert refused.returncode == 1, command
            assert "no hub is recorded" in refused.stderr and str(root) in refused.stderr
        assert process.poll() is None
        explicit_env = {
            **env,
            "ROBOMATE_HUB_URL": str(info["url"]),
            "ROBOMATE_TOKEN_FILE": str(root / ".robomate/token"),
        }
        explicit = subprocess.run(
            [CLI, "status"],
            cwd=tmp_path,
            env=explicit_env,
            text=True,
            capture_output=True,
            timeout=10,
        )
        assert explicit.returncode == 0, explicit.stderr
        assert str(root) in explicit.stdout and str(info["url"]) in explicit.stdout
    finally:
        stop(root, env, process)


def test_status_with_unreachable_explicit_url_does_not_claim_current_repo(
    repository: tuple[Path, dict[str, str]],
) -> None:
    root, env = repository
    explicit_env = {**env, "ROBOMATE_HUB_URL": "http://127.0.0.1:1", "ROBOMATE_TOKEN": "test-token"}
    result = subprocess.run(
        [CLI, "status", "--json"],
        cwd=root,
        env=explicit_env,
        text=True,
        capture_output=True,
        timeout=10,
    )
    assert result.returncode == 1
    assert json.loads(result.stdout) == {
        "running": False,
        "repo_root": None,
        "url": "http://127.0.0.1:1",
        "port": 1,
    }
    human = subprocess.run(
        [CLI, "status"], cwd=root, env=explicit_env, text=True, capture_output=True, timeout=10
    )
    assert human.returncode == 1
    assert human.stdout.startswith("Hub: not running")
    assert str(root) not in human.stdout


def test_status_uses_matching_hub_health_when_pid_is_not_visible(
    repository: tuple[Path, dict[str, str]],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    root, _ = repository
    state = root / ".robomate"
    state.mkdir()
    (state / "token").write_text("test-token")
    write_hub_json(
        root, {"url": "http://127.0.0.1:8420", "port": 8420, "pid": 1234, "hub_id": "same-hub"}
    )
    monkeypatch.chdir(root)
    monkeypatch.setattr(cli, "process_alive", lambda _pid: False)
    monkeypatch.setattr(cli, "hub_healthy", lambda _url, hub_id: hub_id == "same-hub")

    def status(_url: str, _token: str, method: str) -> dict[str, Any]:
        assert method == "hub.status"
        return {"running": True, "hub_id": "same-hub"}

    monkeypatch.setattr(cli, "_rpc", status)
    cli._status(True, None)
    assert json.loads(capsys.readouterr().out) == {"running": True, "hub_id": "same-hub"}


def test_status_reports_unreachable_with_visible_recorded_pid(
    repository: tuple[Path, dict[str, str]],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    root, _ = repository
    state = root / ".robomate"
    state.mkdir()
    write_hub_json(
        root, {"url": "http://127.0.0.1:8420", "port": 8420, "pid": 1234, "hub_id": "same-hub"}
    )
    monkeypatch.chdir(root)
    monkeypatch.setattr(cli, "process_alive", lambda _pid: True)
    monkeypatch.setattr(cli, "hub_healthy", lambda _url, _hub_id: False)
    with pytest.raises(SystemExit, match="1"):
        cli._status(True, None)
    assert json.loads(capsys.readouterr().out) == {
        "running": False,
        "repo_root": str(root),
        "name": None,
        "checkout": str(root),
        "state_dir": str(root / ".robomate"),
        "url": "http://127.0.0.1:8420",
        "port": 8420,
        "reason": "unreachable; recorded pid 1234 is visible",
    }


def test_ls_lists_two_live_hubs_and_prunes_stale_entry(
    repository: tuple[Path, dict[str, str]], tmp_path: Path
) -> None:
    root, env = repository
    other = tmp_path / "other"
    other.mkdir()
    subprocess.run(["git", "init", "-b", "main"], cwd=other, check=True, capture_output=True)
    subprocess.run(
        ["git", "remote", "add", "origin", "git@github.com:example/other.git"],
        cwd=other,
        check=True,
    )
    subprocess.run(
        ["git", "symbolic-ref", "refs/remotes/origin/HEAD", "refs/remotes/origin/main"],
        cwd=other,
        check=True,
    )
    first = start(root, env)
    second = start(other, env)
    try:
        first_info = await_hub(root, first)
        second_info = await_hub(other, second)
        register(
            {"repo_root": "/stale", "url": "http://127.0.0.1:1", "hub_id": "stale", "pid": 999999},
            env,
        )
        result = subprocess.run(
            [CLI, "ls"], cwd=root, env=env, text=True, capture_output=True, timeout=10
        )
        assert result.returncode == 0, result.stderr
        lines = result.stdout.splitlines()
        assert len(lines) == 2
        assert any(str(root) in line and str(first_info["url"]) in line for line in lines)
        assert any(str(other) in line and str(second_info["url"]) in line for line in lines)
        assert all("agents 0" in line and "workflow none" in line for line in lines)
        assert "stale" not in registry_path(env).read_text()
        result = subprocess.run(
            [CLI, "ls", "--json"], cwd=root, env=env, text=True, capture_output=True, timeout=10
        )
        assert len(json.loads(result.stdout)) == 2
    finally:
        for repo, process in ((root, first), (other, second)):
            if process.poll() is None:
                stop(repo, env, process)


def test_worker_mcp_first_call_needs_no_further_stdin(
    repository: tuple[Path, dict[str, str]], tmp_path: Path
) -> None:
    """The first worker tool call validates HUB_WORKSPACE with git (#65).

    On Windows a git child that inherits the MCP stdin pipe blocks until the
    harness writes again, so check_in hung until a timeout or shutdown. After
    sending check_in this test writes nothing more and requires the answer.
    """
    root, env = repository
    workspace = (tmp_path / "charlie").resolve()
    workspace.mkdir()
    origin = "git@github.com:example/repo.git"
    subprocess.run(["git", "init", "-b", "main"], cwd=workspace, check=True, capture_output=True)
    subprocess.run(["git", "remote", "add", "origin", origin], cwd=workspace, check=True)
    identity = workspace / ".git" / "robo-agents-workspace.json"
    identity.write_text(
        json.dumps(
            {
                "agent": "charlie",
                "path": str(workspace),
                "repository": origin,
                "workspace_id": "c" * 64,
            }
        )
    )
    identity.chmod(0o600)
    with socket.socket() as available:
        available.bind(("127.0.0.1", 0))
        port = available.getsockname()[1]
    hub = start(root, env, "--port", str(port))
    worker: subprocess.Popen[bytes] | None = None
    try:
        info = await_hub(root, hub)
        worker_env = {
            **env,
            "ROBOMATE_HUB_URL": str(info["url"]),
            "ROBOMATE_TOKEN_FILE": str(root / ".robomate" / "token"),
            "AGENT_NAME": "charlie",
            "HUB_HARNESS": "claude-code",
            "HUB_MODEL": "opus",
            "HUB_PROVIDER": "anthropic",
            "HUB_WORKSPACE": str(workspace),
        }
        worker = subprocess.Popen(
            [CLI, "mcp", "--role", "worker"],
            cwd=workspace,
            env=worker_env,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )
        stdin, stdout = worker.stdin, worker.stdout
        assert stdin is not None and stdout is not None
        responses: queue.Queue[dict[str, Any]] = queue.Queue()

        def read() -> None:
            try:
                for line in stdout:
                    responses.put(json.loads(line))
            except ValueError:
                pass  # stdout closed during shutdown

        reader = threading.Thread(target=read, daemon=True)
        reader.start()
        for payload in (
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-06-18",
                    "capabilities": {},
                    "clientInfo": {"name": "test", "version": "0"},
                },
            },
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
            {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "tools/call",
                "params": {"name": "check_in", "arguments": {}},
            },
        ):
            stdin.write((json.dumps(payload) + "\n").encode())
            stdin.flush()
        deadline = time.monotonic() + 30
        while True:
            try:
                response = responses.get(timeout=max(0.0, deadline - time.monotonic()))
            except queue.Empty:
                raise AssertionError(
                    "check_in got no answer without a further stdin write"
                ) from None
            if response.get("id") == 2:
                break
        result = response["result"]
        assert not result.get("isError"), result
        assert json.loads(result["content"][0]["text"])["status"] == "registered"
        stdin.close()
        worker.wait(timeout=10)
        stdout.close()
        reader.join(timeout=10)
        stop(root, env, hub)
    finally:
        if worker is not None:
            if worker.poll() is None:
                worker.kill()
                worker.wait(timeout=10)
            if worker.stdin is not None and not worker.stdin.closed:
                worker.stdin.close()
            if worker.stdout is not None and not worker.stdout.closed:
                worker.stdout.close()
        if hub.poll() is None:
            hub.terminate()
            hub.wait(timeout=10)


@pytest.mark.parametrize(
    ("seconds", "text"),
    [
        (-3, "0s"),
        (42, "42s"),
        (125, "2m 5s"),
        (4 * 3600 + 61, "4h 1m"),
    ],
)
def test_status_age_is_compact(seconds: int, text: str) -> None:
    assert cli._age(timedelta(seconds=seconds)) == text


@pytest.mark.parametrize("origin", ["https://[::1/a/b.git", "https://[gitlab-box.local]/a/b.git"])
def test_up_malformed_origin_warns_and_serves(
    repository: tuple[Path, dict[str, str]],
    origin: str,
) -> None:
    root, env = repository
    subprocess.run(["git", "remote", "set-url", "origin", origin], cwd=root, check=True)
    process = start(root, env)
    try:
        info = await_hub(root, process)
        assert info["forge"] == "unknown"
        stop(root, env, process)
        assert "forge is unknown" in _hub_stdout(process)
    finally:
        if process.poll() is None:
            process.terminate()
            process.wait(timeout=10)


def test_up_forge_flag_overrides_detection(repository: tuple[Path, dict[str, str]]) -> None:
    root, env = repository
    subprocess.run(
        ["git", "remote", "set-url", "origin", "git@gitlab.com:group/project.git"],
        cwd=root,
        check=True,
    )
    process = start(root, env, "--forge", "github")
    try:
        assert await_hub(root, process)["forge"] == "github"
        stop(root, env, process)
    finally:
        if process.poll() is None:
            process.terminate()
            process.wait(timeout=10)
