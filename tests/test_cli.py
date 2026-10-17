"""End-to-end operator CLI tests in a disposable Git checkout."""

import errno
import io
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
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import timedelta
from pathlib import Path
from typing import Any, cast

import anyio
import conftest
import pytest
import robomate.cli as cli
from agent_hub.database import database, initialize_database
from agent_hub.store import HubStore
from agent_hub_common import AgentProfile, TaskState, WorkflowStatus
from agent_hub_common.discovery import HubEndpoint, read_hub_json, write_hub_json
from agent_hub_common.registry import process_alive, register, registry_path
from conftest import reserve_port_block
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
        # The first-free scan runs in this test's own block, not from 8420,
        # so hubs and test suites elsewhere on the host can't move it (#204).
        "ROBOMATE_BASE_PORT": str(reserve_port_block()),
    }
    return root, env


def start(root: Path, env: dict[str, str], *args: str) -> subprocess.Popen[str]:
    """Start `robomate up` with hub output sent to files, not pipes.

    Nothing reads the hub's output while it runs, so pipes both leak handles
    (#121) and risk blocking the hub once the pipe buffer fills. Files remove
    both risks; use _hub_stdout/_hub_stderr to read them after exit.
    """
    return _start_logged(root, env, [CLI, "up", *args])


def _start_logged(root: Path, env: dict[str, str], argv: list[str]) -> subprocess.Popen[str]:
    xdg = env.get("XDG_STATE_HOME")
    parent = Path(xdg).parent if xdg else Path(tempfile.gettempdir())
    log_dir = Path(tempfile.mkdtemp(prefix="hub-logs-", dir=parent if parent.is_dir() else None))
    stdout_path = log_dir / "stdout.log"
    stderr_path = log_dir / "stderr.log"
    with open(stdout_path, "w") as out, open(stderr_path, "w") as err:
        process = subprocess.Popen(
            argv,
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


# `robomate up` takes about 3 s to record itself on a native Windows desktop,
# mostly importing robomate.cli. Loaded hosted Windows runners have run past
# the earlier 10 s budget with the hub still alive and nothing on stderr
# (#197), so allow 30 s before calling it a failure.
HUB_START_TIMEOUT_S = 30.0


def _startup_diagnostics(root: Path, process: subprocess.Popen[str], elapsed: float) -> str:
    """What a startup failure needs: process state, hub.json and the hub's output."""

    returncode = process.poll()
    state = "still running" if returncode is None else f"exited with {returncode}"
    return (
        f"after {elapsed:.1f}s the `robomate up` process (pid {process.pid}) is {state}\n"
        f"hub.json: {read_hub_json(root)!r}\n"
        f"--- hub stdout ---\n{_hub_stdout(process)}\n"
        f"--- hub stderr ---\n{_hub_stderr(process)}"
    )


def await_hub(
    root: Path, process: subprocess.Popen[str], timeout_s: float = HUB_START_TIMEOUT_S
) -> dict[str, object]:
    started = time.monotonic()
    while (elapsed := time.monotonic() - started) < timeout_s:
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
            raise AssertionError(
                f"hub exited early\n{_startup_diagnostics(root, process, elapsed)}"
            )
        time.sleep(0.05)
    raise AssertionError(f"hub did not start\n{_startup_diagnostics(root, process, elapsed)}")


def await_registry(
    env: dict[str, str],
    matches: Callable[[list[dict[str, Any]]], bool],
    timeout_s: float = 10.0,
) -> list[dict[str, Any]]:
    """Poll hubs.json until its entries satisfy `matches`, and return them (#180).

    `robomate up` writes hub.json before it registers in hubs.json, so
    await_hub can return first. A read that meets `robomate`'s os.replace of
    hubs.json fails on Windows with a sharing violation (EACCES) that clears
    within milliseconds; retry it like any other not-yet state.
    """

    path = registry_path(env)
    deadline = time.monotonic() + timeout_s
    while True:
        entries: list[dict[str, Any]] | None
        try:
            entries = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            entries = []
        except PermissionError:
            if sys.platform != "win32":
                raise
            entries = None
        if entries is not None and matches(entries):
            return entries
        if time.monotonic() >= deadline:
            raise AssertionError(
                f"{path} did not reach the expected entries; last read {entries!r}"
            )
        time.sleep(0.05)


@contextmanager
def _cleanup_step(pending: BaseException | None, what: str) -> Iterator[None]:
    """Raise a cleanup failure, or note it on the failure already propagating."""

    try:
        yield
    except Exception as failure:
        if pending is None:
            raise
        pending.add_note(f"cleanup {what} also failed: {failure!r}")


def stop(root: Path, env: dict[str, str], process: subprocess.Popen[str]) -> None:
    """Stop the hub with `robomate down`, asserting it shuts down cleanly.

    Called from a `finally` while a test failure propagates, it still stops
    the hub but must not replace that failure: a failed `down` (as after a
    hub that never started, #197) or a failed fallback termination becomes a
    note on the original exception.
    """

    pending = sys.exception()
    try:
        with _cleanup_step(pending, f"`robomate down` in {root}"):
            result = subprocess.run(
                [CLI, "down"], cwd=root, env=env, text=True, capture_output=True, timeout=30
            )
            assert result.returncode == 0, result.stderr
            assert process.wait(timeout=10) == 0
    finally:
        with _cleanup_step(pending, f"terminating pid {process.pid}"):
            if process.poll() is None:
                process.terminate()
                process.wait(timeout=10)


# Stands in for a hub that is alive but never records itself in hub.json.
_SILENT_HUB = (
    "import sys, time; print('hub stdout so far', flush=True); "
    "print('hub stderr so far', file=sys.stderr, flush=True); time.sleep(60)"
)


def test_await_hub_timeout_reports_the_hub_output(
    repository: tuple[Path, dict[str, str]],
) -> None:
    root, env = repository
    process = _start_logged(root, env, [sys.executable, "-c", _SILENT_HUB])
    try:
        deadline = time.monotonic() + 10
        while "so far" not in _hub_stderr(process) and time.monotonic() < deadline:
            time.sleep(0.05)
        with pytest.raises(AssertionError, match="hub did not start") as caught:
            await_hub(root, process, timeout_s=0.2)
        message = str(caught.value)
        assert f"(pid {process.pid}) is still running" in message
        assert "hub.json: None" in message
        assert "hub stdout so far" in message and "hub stderr so far" in message
    finally:
        process.terminate()
        process.wait(timeout=10)


def _failed_down(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
    """`robomate down` for a hub that never recorded itself, without the CLI's import time."""

    return subprocess.CompletedProcess(argv, 1, "", "robomate: no hub is recorded for checkout")


class _UnstoppableHub:
    """A hub process that survives terminate(): every wait times out."""

    pid = 0

    def poll(self) -> int | None:
        return None

    def terminate(self) -> None:
        pass

    def wait(self, timeout: float | None = None) -> int:
        raise subprocess.TimeoutExpired("robomate up", timeout or 0)


def test_stop_during_a_failure_keeps_that_failure(
    repository: tuple[Path, dict[str, str]], monkeypatch: pytest.MonkeyPatch
) -> None:
    root, env = repository
    monkeypatch.setattr(subprocess, "run", _failed_down)
    process = _start_logged(root, env, [sys.executable, "-c", _SILENT_HUB])
    with pytest.raises(AssertionError) as caught:
        try:
            raise AssertionError("hub did not start")
        finally:
            stop(root, env, process)
    assert caught.value.args == ("hub did not start",)
    assert process.poll() is not None
    notes = getattr(caught.value, "__notes__", [])
    assert len(notes) == 1 and "no hub is recorded" in notes[0]
    # Without a failure in flight, the same failed `down` is the test's failure.
    process = _start_logged(root, env, [sys.executable, "-c", _SILENT_HUB])
    with pytest.raises(AssertionError, match="no hub is recorded"):
        stop(root, env, process)
    assert process.poll() is not None


def test_stop_during_a_failure_keeps_it_when_termination_fails(
    repository: tuple[Path, dict[str, str]], monkeypatch: pytest.MonkeyPatch
) -> None:
    root, env = repository
    monkeypatch.setattr(subprocess, "run", _failed_down)
    process = cast(subprocess.Popen[str], _UnstoppableHub())
    with pytest.raises(AssertionError) as caught:
        try:
            raise AssertionError("hub did not start")
        finally:
            stop(root, env, process)
    assert caught.value.args == ("hub did not start",)
    down, termination = caught.value.__notes__
    assert "`robomate down`" in down and "no hub is recorded" in down
    assert "terminating pid 0" in termination and "TimeoutExpired" in termination
    # Without a failure in flight, the failed termination is not hidden.
    with pytest.raises(subprocess.TimeoutExpired):
        stop(root, env, process)


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


def test_up_redirected_output_escapes_unencodable_checkout(
    repository: tuple[Path, dict[str, str]],
) -> None:
    root, env = repository
    root = root.parent / "drivér → ā"
    init_checkout(root)
    # Force the redirected Windows code page on every platform so CI also
    # exercises this regression when its Python defaults to UTF-8.
    env = {**env, "PYTHONIOENCODING": "cp1252:strict"}
    port = reserve_port_block(1)
    process = start(root, env, "--port", str(port))
    try:
        info = await_hub(root, process)
        # Startup metadata is written before the path output; wait for the last
        # flushed line to establish that startup printing has finished.
        output_path = Path(process._stdout_log)  # type: ignore[attr-defined]
        for _ in range(200):
            output = output_path.read_bytes().decode("cp1252")
            if "ROBOMATE_TOKEN_FILE=" in output:
                break
            assert process.poll() is None, _hub_stderr(process)
            time.sleep(0.05)
        else:
            raise AssertionError("startup output did not finish")
        expected = str(root).encode("cp1252", errors="backslashreplace").decode("cp1252")
        assert f"Hub checkout: {expected}" in output
        assert "\\u2192" in output and "\\u0101" in output
        with urllib.request.urlopen(f"{info['url']}/healthz", timeout=2) as response:
            assert json.load(response)["status"] == "ok"
    finally:
        stop(root, env, process)


def test_mcp_does_not_reconfigure_protocol_stdout(monkeypatch: pytest.MonkeyPatch) -> None:
    with io.BytesIO() as buffer, io.TextIOWrapper(buffer, encoding="cp1252") as stdout:
        monkeypatch.setattr(sys, "stdout", stdout)
        monkeypatch.setattr(sys, "argv", ["robomate", "mcp", "--role", "orchestrator"])

        def run(function: object, args: object, protocol_stdout: object) -> None:
            assert protocol_stdout is stdout
            assert stdout.encoding == "cp1252" and stdout.errors == "strict"

        monkeypatch.setattr(anyio, "run", run)
        cli.main()
        assert stdout.errors == "strict"
        assert buffer.getvalue() == b""


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
    port = reserve_port_block(1)
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
        expected = [("legacy-id", "repo")]
        entries = await_registry(
            env, lambda found: [(e["hub_id"], e.get("name")) for e in found] == expected
        )
        assert [(e["hub_id"], e["name"]) for e in entries] == expected
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
        expected = [info["hub_id"]]
        entries = await_registry(env, lambda found: [e["hub_id"] for e in found] == expected)
        assert [entry["hub_id"] for entry in entries] == expected
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


def test_recorded_busy_port_with_unsafe_operator_token_file_prefers_token_diagnostic(
    repository: tuple[Path, dict[str, str]],
) -> None:
    """#192: an unsafe token path wins over an occupied saved or requested port."""

    root, env = repository
    with socket.socket() as occupied:
        occupied.bind(("127.0.0.1", 0))
        occupied.listen()
        port = occupied.getsockname()[1]
        write_hub_json(root, {"port": port, "hub_id": "old"})
        # A valid token location still reports the occupied port.
        busy_saved = subprocess.run(
            [CLI, "up"], cwd=root, env=env, text=True, capture_output=True, timeout=10
        )
        assert busy_saved.returncode == 1
        assert str(port) in busy_saved.stderr and "--port" in busy_saved.stderr
        busy_explicit = subprocess.run(
            [CLI, "up", "--port", str(port)],
            cwd=root,
            env=env,
            text=True,
            capture_output=True,
            timeout=10,
        )
        assert busy_explicit.returncode == 1
        assert str(port) in busy_explicit.stderr and "--port" in busy_explicit.stderr
        # An unsafe path inside the repository or its hub state is refused with
        # the token-path diagnostic even though the saved port is occupied, and
        # no operator credential is created there.
        for inside in (root / "operator-token", root / ".robomate" / "operator-token"):
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
            assert str(inside) not in result.stdout + result.stderr
        # The same holds for an explicitly requested occupied port.
        explicit_unsafe = subprocess.run(
            [CLI, "up", "--port", str(port)],
            cwd=root,
            env={**env, "ROBOMATE_OPERATOR_TOKEN_FILE": str(root / "operator-token")},
            text=True,
            capture_output=True,
            timeout=10,
        )
        assert explicit_unsafe.returncode == 1
        assert "must not be inside the repository" in explicit_unsafe.stderr
        assert not (root / "operator-token").exists()
        assert str(root / "operator-token") not in explicit_unsafe.stdout + explicit_unsafe.stderr


def test_stale_hub_metadata_is_reused(repository: tuple[Path, dict[str, str]]) -> None:
    root, env = repository
    port = reserve_port_block(1)
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
    monkeypatch.delenv("ROBOMATE_BASE_PORT", raising=False)
    _, port = _bind("127.0.0.1", None)
    assert (attempted, port) == ([8420, 8421], 8421)


def test_first_free_base_port_env_sets_scan_base(monkeypatch: pytest.MonkeyPatch) -> None:
    """#198: ROBOMATE_BASE_PORT moves the first-free scan; default stays 8420."""

    monkeypatch.delenv("ROBOMATE_BASE_PORT", raising=False)
    assert cli._first_free_base() == 8420
    monkeypatch.setenv("ROBOMATE_BASE_PORT", "35000")
    assert cli._first_free_base() == 35000
    monkeypatch.setenv("ROBOMATE_BASE_PORT", "not-a-port")
    with pytest.raises(RuntimeError, match="ROBOMATE_BASE_PORT must be an integer"):
        cli._first_free_base()
    monkeypatch.setenv("ROBOMATE_BASE_PORT", "0")
    with pytest.raises(RuntimeError, match="ROBOMATE_BASE_PORT must be between"):
        cli._first_free_base()


def test_first_free_port_scan_starts_at_base(monkeypatch: pytest.MonkeyPatch) -> None:
    attempted: list[int] = []

    class FakeSocket:
        def setsockopt(self, *_args: object) -> None:
            pass

        def bind(self, address: tuple[str, int]) -> None:
            attempted.append(address[1])

        def listen(self) -> None:
            pass

        def close(self) -> None:
            pass

    monkeypatch.setattr("robomate.cli.socket.socket", lambda *_args: FakeSocket())
    monkeypatch.setenv("ROBOMATE_BASE_PORT", "35000")
    _, port = _bind("127.0.0.1", None)
    assert (attempted, port) == ([35000], 35000)


class _ScriptedSocket:
    """A socket whose bind outcome per port comes from `fail`, recording each port."""

    def __init__(
        self, attempted: list[int], fail: Callable[[int], OSError | None] = lambda _: None
    ) -> None:
        self.attempted, self.fail = attempted, fail

    def setsockopt(self, *_args: object) -> None:
        pass

    def bind(self, address: tuple[str, int]) -> None:
        self.attempted.append(address[1])
        if (error := self.fail(address[1])) is not None:
            raise error

    def listen(self) -> None:
        pass

    def close(self) -> None:
        pass


def _forbidden() -> OSError:
    """What Windows raises for WinError 10013; POSIX ignores the winerror slot."""

    return PermissionError(errno.EACCES, "forbidden", None, 10013)


def test_bind_first_free_scan_skips_forbidden_ports(monkeypatch: pytest.MonkeyPatch) -> None:
    """#229: the unpinned scan moves past 10013/EACCES as it does past a port in use."""

    attempted: list[int] = []
    errors = {35000: _forbidden(), 35001: OSError(errno.EADDRINUSE, "occupied")}
    monkeypatch.setattr(
        "robomate.cli.socket.socket", lambda *_args: _ScriptedSocket(attempted, errors.get)
    )
    monkeypatch.setenv("ROBOMATE_BASE_PORT", "35000")
    _, port = _bind("127.0.0.1", None)
    assert (attempted, port) == ([35000, 35001, 35002], 35002)


def test_bind_pinned_or_saved_port_still_fails_on_forbidden(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#229: a pinned or saved port is never moved, so 10013 fails as before."""

    attempted: list[int] = []
    monkeypatch.setattr(
        "robomate.cli.socket.socket",
        lambda *_args: _ScriptedSocket(attempted, lambda _: _forbidden()),
    )
    with pytest.raises(RuntimeError, match=r"^port 35000 is unavailable; choose --port: "):
        _bind("127.0.0.1", 35000)
    assert attempted == [35000]


def test_bind_first_free_scan_stops_on_other_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    attempted: list[int] = []
    monkeypatch.setattr(
        "robomate.cli.socket.socket",
        lambda *_args: _ScriptedSocket(
            attempted, lambda _: OSError(errno.EADDRNOTAVAIL, "no such address")
        ),
    )
    monkeypatch.setenv("ROBOMATE_BASE_PORT", "35000")
    with pytest.raises(RuntimeError, match="port 35000 is unavailable; choose --port"):
        _bind("127.0.0.1", None)
    assert attempted == [35000]


NETSH_DYNAMIC_PORTS = {
    "en": "\nProtocol tcp Dynamic Port Range\n---------------------------------\n"
    "Start Port      : {start}\nNumber of Ports : {count}\n\n",
    "de": "\nProtokoll tcp Dynamischer Portbereich\n---------------------------------\n"
    "Startport       : {start}\nAnzahl Ports    : {count}\n\n",
}


@pytest.mark.parametrize("language", sorted(NETSH_DYNAMIC_PORTS))
@pytest.mark.parametrize(
    ("start", "count", "expected"),
    [(49152, 16384, (49152, 65535)), (1024, 64511, (1024, 65534)), (1024, 64512, (1024, 65535))],
)
def test_base_port_block_parses_windows_dynamic_range(
    monkeypatch: pytest.MonkeyPatch,
    language: str,
    start: int,
    count: int,
    expected: tuple[int, int],
) -> None:
    output = NETSH_DYNAMIC_PORTS[language].format(start=start, count=count)
    monkeypatch.setattr(
        "subprocess.run",
        lambda argv, **_kwargs: subprocess.CompletedProcess(argv, 0, stdout=output),
    )
    assert conftest.ephemeral_port_range("win32") == expected


def _unreadable() -> tuple[int, int] | None:
    raise OSError("unreadable")


@pytest.mark.parametrize(
    ("platform", "reader", "ephemeral", "outside"),
    [
        ("linux", lambda: (32768, 60999), (32768, 60999), True),
        ("linux", _unreadable, (32768, 60999), True),
        ("linux", lambda: None, (32768, 60999), True),
        ("win32", lambda: (49152, 65535), (49152, 65535), True),
        ("win32", lambda: (1024, 65535), (1024, 65535), False),
        ("win32", lambda: (1024, 65534), (1024, 65534), False),
        ("win32", _unreadable, (49152, 65535), True),
        ("win32", lambda: (70000, 80000), (49152, 65535), True),
        ("darwin", lambda: (49152, 65535), (49152, 65535), True),
        ("darwin", _unreadable, (49152, 65535), True),
    ],
)
def test_base_port_block_avoids_host_ephemeral_range(
    monkeypatch: pytest.MonkeyPatch,
    platform: str,
    reader: Callable[[], tuple[int, int] | None],
    ephemeral: tuple[int, int],
    outside: bool,
) -> None:
    """#217/#229: blocks clear the host's range when any fit, and never reach 842x."""

    monkeypatch.setitem(conftest.EPHEMERAL_RANGE_READERS, platform, reader)
    assert conftest.ephemeral_port_range(platform) == ephemeral
    low, high = ephemeral
    bases = conftest.port_block_bases(16, ephemeral)
    assert bases and min(bases) >= conftest.LOWEST_TEST_PORT > 8499
    assert max(bases) + 15 <= 65535
    assert all(base + 15 < low or base > high for base in bases) is outside

    attempted: list[int] = []
    monkeypatch.setattr("socket.socket", lambda *_args: _ScriptedSocket(attempted))
    base = conftest.reserve_port_block(16, platform=platform)
    assert base in bases and attempted == list(range(base, base + 16))


@pytest.mark.parametrize("count", [1, 8, 16])
def test_base_port_block_falls_back_when_little_lies_outside(count: int) -> None:
    """A range ending at 65534 leaves base 65535 outside; one base is no room to draw from.

    Two concurrent suites each pinning one port both drew 65535 there.
    """

    bases = conftest.port_block_bases(count, (1024, 65534))
    assert bases == list(range(conftest.LOWEST_TEST_PORT, 65536 - count + 1))


def test_base_port_block_never_relies_on_bind_zero(monkeypatch: pytest.MonkeyPatch) -> None:
    """#217: a host whose bind(0) only answers low ports still gets a block."""

    class LowEphemeralSocket(_ScriptedSocket):
        def getsockname(self) -> tuple[str, int]:
            return ("127.0.0.1", 8446)

    monkeypatch.setitem(conftest.EPHEMERAL_RANGE_READERS, "win32", lambda: (1024, 65534))
    attempted: list[int] = []
    monkeypatch.setattr("socket.socket", lambda *_args: LowEphemeralSocket(attempted))
    base = conftest.reserve_port_block(8, platform="win32")
    assert base >= conftest.LOWEST_TEST_PORT
    assert attempted == list(range(base, base + 8))


class _Draws:
    """Stands in for random.SystemRandom: hands out the given bases in order."""

    def __init__(self, bases: list[int]) -> None:
        self.bases = iter(bases)

    def choice(self, options: list[int]) -> int:
        base = next(self.bases)
        assert base in options
        return base


def test_base_port_block_retries_in_use_and_forbidden_ports(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setitem(conftest.EPHEMERAL_RANGE_READERS, "linux", lambda: (32768, 60999))
    monkeypatch.setattr("random.SystemRandom", lambda: _Draws([20000, 21000, 22000]))
    errors = {20003: OSError(errno.EADDRINUSE, "occupied"), 21005: _forbidden()}
    attempted: list[int] = []
    monkeypatch.setattr("socket.socket", lambda *_args: _ScriptedSocket(attempted, errors.get))
    assert conftest.reserve_port_block(8, platform="linux") == 22000
    assert attempted == [*range(20000, 20004), *range(21000, 21006), *range(22000, 22008)]


def test_base_port_block_fails_clearly_only_after_its_attempts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setitem(conftest.EPHEMERAL_RANGE_READERS, "linux", lambda: (32768, 60999))
    attempted: list[int] = []
    monkeypatch.setattr(
        "socket.socket",
        lambda *_args: _ScriptedSocket(attempted, lambda _: _forbidden()),
    )
    with pytest.raises(
        RuntimeError, match=r"^no 8 consecutive free loopback ports after 5 random blocks in "
    ):
        conftest.reserve_port_block(8, platform="linux", attempts=5)
    assert len(attempted) == 5


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


def test_down_tells_a_slow_hub_from_an_unreachable_one(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#207: a shutdown RPC that times out is not reported as an unreachable hub."""

    monkeypatch.setattr(cli, "read_token_file", lambda _path, _label: "operator-token")
    with socket.create_server(("127.0.0.1", 0)) as silent:
        url = f"http://127.0.0.1:{silent.getsockname()[1]}"
    monkeypatch.setattr(cli, "_connect", lambda _selector: (HubEndpoint(url, "t"), None))
    # Nothing listens there now. Windows takes about 2 s to report a refused
    # connection, so this runs with the real timeout.
    with pytest.raises(RuntimeError, match="is not running$"):
        cli._down(None)
    monkeypatch.setattr(cli, "_RPC_TIMEOUT_S", 0.2)
    with socket.create_server(("127.0.0.1", 0)) as silent:
        # Accepts (into the backlog) but never answers, like a hub too busy to
        # reply. The patched _connect reads `url` at call time.
        url = f"http://127.0.0.1:{silent.getsockname()[1]}"
        with pytest.raises(RuntimeError, match=r"did not answer shutdown within 0\.2 s$"):
            cli._down(None)


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
        # A heartbeat alone is bridge liveness, never activity (#95).
        assert (
            "Orchestrator: alice  idle  no hub call seen  (bridge connected, 1 open task)\n"
            "  session 5f0c6f0e-8f3c-4d57-9d0a-0b8b1c1e2f3a\n"
        ) in result.stdout, result.stdout
        assert "last seen" not in result.stdout
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


def test_status_after_a_restart_shows_alice_finished_with_her_persisted_last_call(
    repository: tuple[Path, dict[str, str]],
) -> None:
    """#95: a done run's idle Alice reads finished, not freshly seen, across a restart."""

    root, env = repository
    directory = root / ".robomate"
    directory.mkdir()
    database_path = directory / "hub.db"
    initialize_database(database_path)
    # The previous hub process: Alice's close-out was her last call.
    previous = HubStore(database_path)
    previous.initialize_workflow("Build issue #95")
    previous.note_orchestrator_session("alice", "5f0c6f0e-8f3c-4d57-9d0a-0b8b1c1e2f3a")
    with previous.orchestrator_call(
        "5f0c6f0e-8f3c-4d57-9d0a-0b8b1c1e2f3a", "set_workflow_status", 75
    ):
        previous.set_workflow_status(
            WorkflowStatus.DONE,
            "Merged",
            actor="alice",
            session="5f0c6f0e-8f3c-4d57-9d0a-0b8b1c1e2f3a",
        )
    process = start(root, env)
    try:
        info = await_hub(root, process)
        token = (directory / "token").read_text().strip()

        def status_text() -> str:
            result = subprocess.run(
                [CLI, "status"], cwd=root, env=env, text=True, capture_output=True, timeout=10
            )
            assert result.returncode == 0, result.stderr
            return result.stdout

        out = status_text()
        assert re.search(
            r"Orchestrator: alice  finished  last call \d+s ago"
            r" \(set_workflow_status, before hub restart\)"
            r"  \(bridge not seen by this hub, workflow done\)\n"
            r"  session none seen by this hub process\n",
            out,
        ), out
        # Her bridge still heartbeats: connected, still finished.
        assert _rpc_as_alice(str(info["url"]), token, "hub.heartbeat")["result"] == {"ok": True}
        out = status_text()
        assert re.search(
            r"Orchestrator: alice  finished  last call \d+s ago"
            r" \(set_workflow_status, before hub restart\)"
            r"  \(bridge connected, workflow done\)\n"
            r"  session 5f0c6f0e-8f3c-4d57-9d0a-0b8b1c1e2f3a\n",
            out,
        ), out
        assert "last seen" not in out and "STALLED" not in out
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
        # Additive: the legacy session block is still there.
        assert status["orchestrator"]["last_seen"]
        alice = status["orchestrator_activity"]
        assert (alice["state"], alice["bridge"], alice["last_call_restored"]) == (
            "finished",
            "connected",
            True,
        )
    finally:
        stop(root, env, process)


def _activity(**fields: Any) -> dict[str, Any]:
    return {
        "state": "active",
        "holding": None,
        "hold_age_s": None,
        "last_call": "get_state",
        "last_call_age_s": 5.0,
        "last_call_actor": "alice",
        "last_call_restored": False,
        "bridge": "connected",
        "bridge_age_s": 5.0,
        "actor": "alice",
    } | fields


def _with_alice(activity: dict[str, Any] | None, **status: Any) -> dict[str, Any]:
    return {
        "orchestrator": {"name": "alice", "session": "s-1", "last_seen": "2026-10-08T12:00:00Z"},
        "orchestrator_activity": activity,
        "workflow": {"status": "active"},
        "tasks": [],
    } | status


@pytest.mark.parametrize(
    ("status", "line"),
    [
        (
            _with_alice(_activity()),
            "Orchestrator: alice  active  last call 5s ago (get_state)  (bridge connected)",
        ),
        (
            _with_alice(_activity(state="holding", holding="wait_for_event", hold_age_s=40.0)),
            "Orchestrator: alice  waiting in wait_for_event 40s"
            "  last call 5s ago (get_state)  (bridge connected)",
        ),
        (
            _with_alice(
                _activity(state="idle", last_call="assign_task", last_call_age_s=1320.0),
                tasks=[{"id": "t1"}, {"id": "t2"}],
            ),
            "Orchestrator: alice  idle  last call 22m 0s ago (assign_task)"
            "  (bridge connected, 2 open tasks)",
        ),
        (
            _with_alice(
                _activity(
                    state="finished", last_call="set_workflow_status", last_call_age_s=1320.0
                ),
                workflow={"status": "done"},
            ),
            "Orchestrator: alice  finished  last call 22m 0s ago (set_workflow_status)"
            "  (bridge connected, workflow done)",
        ),
        (
            _with_alice(
                _activity(state="idle", bridge="disconnected", bridge_age_s=600.0),
                workflow={"status": "escalated"},
            ),
            "Orchestrator: alice  idle  last call 5s ago (get_state)"
            "  (bridge disconnected, last heard 10m 0s ago, workflow escalated)",
        ),
    ],
)
def test_the_orchestrator_line_separates_her_activity_from_bridge_liveness(
    status: dict[str, Any], line: str
) -> None:
    assert cli._orchestrator_lines(status) == [line, "  session s-1"]


def test_the_orchestrator_line_falls_back_for_a_hub_older_than_95() -> None:
    status = _with_alice({"stalled": False})
    (line,) = cli._orchestrator_lines(status)
    assert re.fullmatch(r"Orchestrator: alice  session s-1  last seen [\dhms ]+ ago", line), line
    assert cli._orchestrator_lines(_with_alice(None, orchestrator=None)) == ["Orchestrator: none"]


def test_no_orchestrator_at_all_reads_none() -> None:
    status = _with_alice(
        _activity(
            state="not_seen",
            last_call=None,
            last_call_age_s=None,
            last_call_actor=None,
            actor=None,
            bridge="not_seen",
        ),
        orchestrator=None,
    )
    assert cli._orchestrator_lines(status) == ["Orchestrator: none"]


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
            r"Orchestrator: alice .*\n  session \S+\n"
            r"  STALLED — no hub call for \d+s \(none seen\);"
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

        # #171: a moot task-wait question disappears without an operator answer.
        hub_store.check_in("bob")
        hub_store.set_workflow_status(
            WorkflowStatus.ACTIVE, "Continue", actor="alice", session="s1"
        )
        task = hub_store.assign_task("bob", "implementer", "Fix", "Do it")
        question_id = hub_store.ask_user(
            "Still waiting?", None, actor="alice", session="s1", waiting_on_task_id=task.id
        )
        assert [q["question_id"] for q in json.loads(robomate("inbox", "--json").stdout)] == [
            question_id
        ]
        hub_store.submit_result(task.id, "bob", TaskState.COMPLETED, "Done")
        with database(database_path) as connection:
            event_id = connection.execute("SELECT MAX(id) FROM event").fetchone()[0]
        hub_store.withdraw_question(
            question_id, "Task completed", event_id, actor="alice", session="s1"
        )
        assert robomate("inbox").stdout == "No open questions.\n"
        assert json.loads(robomate("inbox", "--json").stdout) == []
        assert json.loads(robomate("status", "--json").stdout)["operator_questions"] == 0
        assert "Operator questions: 0\n" in robomate("status").stdout
        late = robomate("answer", str(question_id), "reassign")
        assert late.returncode == 1 and "withdrawn" in late.stderr
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
        entries = await_registry(env, lambda found: all(e["hub_id"] != "stale" for e in found))
        assert "stale" not in [e["hub_id"] for e in entries]
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
    port = reserve_port_block(1)
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


def test_forget_releases_name_and_port_and_next_up_takes_first_free(
    repository: tuple[Path, dict[str, str]], tmp_path: Path
) -> None:
    """#159: forget a stopped hub; ls drops it, state stays, next up takes first free."""

    root, env = repository
    wt_a, wt_b, wt_c = tmp_path / "wt-a", tmp_path / "wt-b", tmp_path / "wt-c"
    _commit_and_add_worktrees(root, wt_a, wt_b, wt_c)
    hub_a, hub_b = start(wt_a, env), start(wt_b, env)
    try:
        a, b = await_hub(wt_a, hub_a), await_hub(wt_b, hub_b)
        assert (a["name"], b["name"]) == ("wt-a", "wt-b")
        assert a["port"] != b["port"]

        # A live hub is never forgotten.
        refused = _cli(wt_a, env, "forget", "wt-a")
        assert refused.returncode == 1 and "stop it with robomate down first" in refused.stderr

        stop(wt_a, env, hub_a)
        rows = {row["name"]: row for row in json.loads(_cli(root, env, "ls", "--json").stdout)}
        assert (rows["wt-a"]["live"], rows["wt-b"]["live"]) == (False, True)

        forgotten = _cli(root, env, "forget", "wt-a")
        assert forgotten.returncode == 0, forgotten.stderr
        assert "Forgot hub wt-a" in forgotten.stdout and "state kept" in forgotten.stdout
        # State and run reports are kept; only the registry entry goes.
        assert (wt_a / ".robomate" / "hub.json").is_file()
        assert (wt_a / ".robomate" / "hub.db").is_file()
        rows = {row["name"]: row for row in json.loads(_cli(root, env, "ls", "--json").stdout)}
        assert "wt-a" not in rows and rows["wt-b"]["live"] is True

        # The freed port goes to the next hub that starts (it is the
        # smallest free port: the sibling's port is still held live, and the
        # scan runs on an isolated block per #198).
        hub_c = start(wt_c, env)
        try:
            c = await_hub(wt_c, hub_c)
            assert int(str(c["port"])) == int(str(a["port"]))
            # The forgotten hub's restart takes the next first-free port and says so.
            hub_a = start(wt_a, env)
            again = await_hub(wt_a, hub_a)
            assert again["hub_id"] == a["hub_id"] and again["name"] == "wt-a"
            assert int(str(again["port"])) not in (int(str(b["port"])), int(str(c["port"])))
            for _ in range(200):
                out = _hub_stdout(hub_a)
                if "previously used port" in out:
                    break
                time.sleep(0.05)
            assert f"previously used port {a['port']}" in out
            assert "port was released" in out and str(again["port"]) in out
        finally:
            if hub_c.poll() is None:
                stop(wt_c, env, hub_c)
            if hub_a.poll() is None:
                stop(wt_a, env, hub_a)
    finally:
        for checkout, process in ((wt_a, hub_a), (wt_b, hub_b)):
            if process.poll() is None:
                stop(checkout, env, process)


def test_forget_all_only_forgets_stopped_hubs(
    repository: tuple[Path, dict[str, str]], tmp_path: Path
) -> None:
    """#159: forget --all drops every stopped hub and keeps live ones and state."""

    root, env = repository
    wt_a, wt_b = tmp_path / "wt-a", tmp_path / "wt-b"
    _commit_and_add_worktrees(root, wt_a, wt_b)
    hub_a, hub_b = start(wt_a, env), start(wt_b, env)
    try:
        await_hub(wt_a, hub_a)
        await_hub(wt_b, hub_b)
        stop(wt_a, env, hub_a)
        result = _cli(root, env, "forget", "--all")
        assert result.returncode == 0, result.stderr
        assert "Forgot hub wt-a" in result.stdout
        rows = {row["name"]: row for row in json.loads(_cli(root, env, "ls", "--json").stdout)}
        assert list(rows) == ["wt-b"] and rows["wt-b"]["live"] is True
        assert (wt_a / ".robomate" / "hub.db").is_file()
        again = _cli(root, env, "forget", "--all")
        assert again.returncode == 0 and "No stopped hubs to forget." in again.stdout
        # r1-5: the released port is still the first free one, so the restart
        # reuses it and says so; the marker is then cleared, so the next
        # restart is silent.
        stopped_info = read_hub_json(wt_a)
        assert stopped_info is not None
        assert stopped_info.get("released_port") == stopped_info.get("port")
        restarted = start(wt_a, env)
        try:
            back = await_hub(wt_a, restarted)
            assert back["port"] == stopped_info["port"]
            for _ in range(200):
                out = _hub_stdout(restarted)
                if "first free port" in out:
                    break
                time.sleep(0.05)
            assert "was released; it was still the first free port" in out
            cleared = read_hub_json(wt_a)
            assert cleared is not None and cleared.get("released_port") is None
        finally:
            stop(wt_a, env, restarted)
        second = start(wt_a, env)
        try:
            assert (await_hub(wt_a, second))["port"] == stopped_info["port"]
            stop(wt_a, env, second)
            out = _hub_stdout(second)
            assert "previously used port" not in out and "was released" not in out
        finally:
            if second.poll() is None:
                second.terminate()
                second.wait(timeout=10)
    finally:
        for checkout, process in ((wt_a, hub_a), (wt_b, hub_b)):
            if process.poll() is None:
                stop(checkout, env, process)


def test_down_all_stops_every_live_hub(
    repository: tuple[Path, dict[str, str]], tmp_path: Path
) -> None:
    """#159: one command stops all live hubs; ls then shows none live."""

    root, env = repository
    wt_a, wt_b = tmp_path / "wt-a", tmp_path / "wt-b"
    _commit_and_add_worktrees(root, wt_a, wt_b)
    hub_a, hub_b = start(wt_a, env), start(wt_b, env)
    try:
        a, b = await_hub(wt_a, hub_a), await_hub(wt_b, hub_b)
        assert hub_a.poll() is None and hub_b.poll() is None
        result = _cli(root, env, "down", "--all")
        assert result.returncode == 0, result.stderr
        assert f"Stopping hub wt-a at {a['url']}" in result.stdout
        assert f"Stopping hub wt-b at {b['url']}" in result.stdout
        assert hub_a.wait(timeout=10) == 0
        assert hub_b.wait(timeout=10) == 0
        rows = json.loads(_cli(root, env, "ls", "--json").stdout)
        assert len(rows) == 2 and all(not row["live"] for row in rows)
        assert (wt_a / ".robomate" / "hub.db").is_file()
        assert (wt_b / ".robomate" / "hub.db").is_file()
        idle = _cli(root, env, "down", "--all")
        assert idle.returncode == 0 and "No live hubs." in idle.stdout
    finally:
        for process in (hub_a, hub_b):
            if process.poll() is None:
                process.terminate()
                process.wait(timeout=10)


def test_forgotten_hub_can_be_renamed_and_pinned(
    repository: tuple[Path, dict[str, str]], tmp_path: Path
) -> None:
    """#159 r1-3/r1-4/r2-2: stolen name forces rename; --port pins with marker present."""

    root, env = repository
    wt_a, wt_c = tmp_path / "wt-a", tmp_path / "wt-c"
    _commit_and_add_worktrees(root, wt_a, wt_c)
    hub_a = start(wt_a, env)
    hub_c: subprocess.Popen[str] | None = None
    try:
        a = await_hub(wt_a, hub_a)
        stop(wt_a, env, hub_a)
        assert _cli(root, env, "forget", "wt-a").returncode == 0
        # A sibling takes the freed name.
        hub_c = start(wt_c, env, "--name", "wt-a")
        assert await_hub(wt_c, hub_c)["name"] == "wt-a"
        # A plain up is refused naming the holder; an explicit --name renames,
        # and the forgotten port is released to first-free with a message.
        refused = _cli(wt_a, env, "up")
        assert refused.returncode == 1 and "'wt-a' is taken" in refused.stderr
        hub_a = start(wt_a, env, "--name", "wt-a2")
        renamed = await_hub(wt_a, hub_a)
        assert renamed["name"] == "wt-a2"
        for _ in range(200):
            out = _hub_stdout(hub_a)
            if "previously used port" in out:
                break
            time.sleep(0.05)
        assert f"previously used port {a['port']}" in out
        stop(wt_a, env, hub_a)
        # r2-2: forget again, then pin with --port while the marker is present:
        # it binds the pinned port, prints no release message, clears the marker.
        assert _cli(root, env, "forget", "wt-a2").returncode == 0
        marked = read_hub_json(wt_a)
        assert marked is not None and marked.get("released_port") is not None
        pinned = reserve_port_block(1)
        hub_a = start(wt_a, env, "--port", str(pinned))
        assert await_hub(wt_a, hub_a)["port"] == pinned
        stop(wt_a, env, hub_a)
        out = _hub_stdout(hub_a)
        assert "previously used port" not in out and "was released" not in out
        cleared = read_hub_json(wt_a)
        assert cleared is not None and cleared.get("released_port") is None
    finally:
        for checkout, process in ((wt_a, hub_a), (wt_c, hub_c)):
            if process is not None and process.poll() is None:
                stop(checkout, env, process)


def test_down_all_partial_failure_stops_the_rest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """#159 r1-2: one malformed shutdown response is reported, not fatal to the loop."""

    operator_file = tmp_path / "operator-token"
    operator_file.write_text("operator-token")
    operator_file.chmod(0o600)
    monkeypatch.setattr(cli, "operator_token_path", lambda: operator_file)
    checkouts = []
    for name in ("one", "two"):
        checkout = tmp_path / name
        (checkout / ".robomate").mkdir(parents=True)
        (checkout / ".robomate" / "token").write_text("bearer-token")
        checkouts.append(checkout)
    entries = [
        {
            "hub_id": f"id-{name}",
            "name": name,
            "checkout": str(checkout),
            "url": f"http://127.0.0.1:{port}",
        }
        for name, checkout, port in zip(("one", "two"), checkouts, (8400, 8401), strict=True)
    ]
    monkeypatch.setattr(cli, "live_entries", lambda: entries)
    calls: list[str] = []

    def fake_rpc(
        url: str,
        token: str,
        method: str,
        *,
        operator_token: str | None = None,
        params: dict[str, object] | None = None,
    ) -> dict[str, object]:
        calls.append(url)
        assert method == "hub.shutdown"
        assert operator_token == "operator-token" and token == "bearer-token"
        if url.endswith(":8400"):
            raise ValueError("invalid JSON in response")
        return {"stopping": True}

    monkeypatch.setattr(cli, "_rpc", fake_rpc)
    with pytest.raises(RuntimeError, match="failed to stop 1 of 2"):
        cli._down_all(False)
    assert calls == [entry["url"] for entry in entries]
    out = capsys.readouterr().out
    assert "Failed to stop hub one" in out and "Stopping hub two" in out


def test_down_json_requires_all(repository: tuple[Path, dict[str, str]]) -> None:
    """#159 r1-7: down --json without --all is refused instead of ignored."""

    root, env = repository
    result = _cli(root, env, "down", "--json")
    assert result.returncode == 1 and "--json goes with --all" in result.stderr


def test_forget_one_lock_contention_skips_under_all(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """#159 r2-1: a held _hub_lock is a skip under --all, with an accurate message."""

    from agent_hub_common.discovery import write_hub_json as _write_hub_json

    checkout = tmp_path / "wt-a"
    (checkout / ".robomate").mkdir(parents=True)
    _write_hub_json(
        checkout,
        {
            "hub_id": "hid-a",
            "name": "wt-a",
            "url": "http://127.0.0.1:8420",
            "port": 8420,
            "pid": None,
        },
    )
    entry = {
        "hub_id": "hid-a",
        "name": "wt-a",
        "checkout": str(checkout),
        "url": "http://127.0.0.1:8420",
        "port": 8420,
        "live": False,
    }

    def locked(_checkout: Path) -> object:
        raise RuntimeError("hub already running at http://127.0.0.1:8420")

    monkeypatch.setattr(cli, "_hub_lock", locked)

    # The holder is live: --all skips it naming liveness; explicit mode raises.
    monkeypatch.setattr(cli, "hub_entries", lambda: [entry | {"live": True}])
    outcome = cli._forget_one(entry, forget_all=True, as_json=False)
    assert outcome == {
        "hub_id": "hid-a",
        "name": "wt-a",
        "forgotten": False,
        "reason": "hub wt-a is live at http://127.0.0.1:8420; stop it with robomate down first",
    }
    assert "Skipping hub wt-a is live" in capsys.readouterr().out
    with pytest.raises(RuntimeError, match="is live"):
        cli._forget_one(entry, forget_all=False, as_json=False)

    # The holder is only starting: the message says so, and --all still skips.
    monkeypatch.setattr(cli, "hub_entries", lambda: [entry | {"live": False}])
    outcome = cli._forget_one(entry, forget_all=True, as_json=True)
    assert outcome == {
        "hub_id": "hid-a",
        "name": "wt-a",
        "forgotten": False,
        "reason": "hub wt-a is starting; wait for robomate up to finish and try again",
    }
    with pytest.raises(RuntimeError, match="is starting"):
        cli._forget_one(entry, forget_all=False, as_json=False)


def test_unjoined_mcp_join_whoami_and_hub_selector(
    repository: tuple[Path, dict[str, str]], tmp_path: Path
) -> None:
    """#238: `robomate mcp` starts unjoined, guards tools, and binds via `join`."""

    root, env = repository
    outside = tmp_path / "outside"
    outside.mkdir()
    hub = start(root, env)
    worker: subprocess.Popen[bytes] | None = None
    try:
        info = await_hub(root, hub)
        await_registry(env, lambda found: any(e.get("hub_id") == info["hub_id"] for e in found))
        worker_env = {
            key: value
            for key, value in env.items()
            if key
            not in {"ROBOMATE_HUB_URL", "ROBOMATE_TOKEN", "ROBOMATE_TOKEN_FILE", "AGENT_NAME"}
        }
        worker = subprocess.Popen(
            [CLI, "mcp", "--harness", "claude-code"],
            cwd=outside,
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
                pass

        reader = threading.Thread(target=read, daemon=True)
        reader.start()

        def send(payload: dict[str, Any]) -> None:
            stdin.write((json.dumps(payload) + "\n").encode())
            stdin.flush()

        def wait_id(req_id: int) -> dict[str, Any]:
            deadline = time.monotonic() + 30
            while True:
                try:
                    response = responses.get(timeout=max(0.0, deadline - time.monotonic()))
                except queue.Empty:
                    raise AssertionError(f"request {req_id} timed out") from None
                if response.get("id") == req_id:
                    return response

        send(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-06-18",
                    "capabilities": {},
                    "clientInfo": {"name": "test", "version": "0"},
                },
            }
        )
        send({"jsonrpc": "2.0", "method": "notifications/initialized"})
        wait_id(1)

        # 1. whoami before join
        send(
            {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "tools/call",
                "params": {"name": "whoami", "arguments": {}},
            }
        )
        whoami_before = wait_id(2)["result"]
        assert not whoami_before.get("isError"), whoami_before
        before_body = json.loads(whoami_before["content"][0]["text"])
        assert before_body["joined"] is False and before_body["hub"] is None
        assert str(info["name"]) in before_body["discovery_error"]
        assert "not inside a git repository" in before_body["discovery_error"]

        # 2. await_assignment before join fails naming join
        send(
            {
                "jsonrpc": "2.0",
                "id": 3,
                "method": "tools/call",
                "params": {"name": "await_assignment", "arguments": {}},
            }
        )
        guarded = wait_id(3)["result"]
        assert guarded.get("isError") is True
        assert "join" in guarded["content"][0]["text"]

        # 3. join from outside any checkout with unknown hub selector lists live hubs
        send(
            {
                "jsonrpc": "2.0",
                "id": 4,
                "method": "tools/call",
                "params": {
                    "name": "join",
                    "arguments": {"role": "implementer", "name": "bob", "hub": "no-such-hub"},
                },
            }
        )
        bad_hub = wait_id(4)["result"]
        assert bad_hub.get("isError") is True
        assert str(info["name"]) in bad_hub["content"][0]["text"]

        # 4. join with hub=<name> succeeds and records install harness source
        send(
            {
                "jsonrpc": "2.0",
                "id": 5,
                "method": "tools/call",
                "params": {
                    "name": "join",
                    "arguments": {
                        "role": "implementer",
                        "name": "bob",
                        "hub": str(info["name"]),
                        "model": "claude-opus-4-7",
                        "effort": "high",
                    },
                },
            }
        )
        joined = wait_id(5)["result"]
        assert not joined.get("isError"), joined
        joined_body = json.loads(joined["content"][0]["text"])
        assert joined_body["status"] == "registered"
        assert joined_body["agent"] == "bob"
        assert joined_body["role"] == "implementer"
        assert joined_body["profile"]["harness"] == "claude-code"
        assert joined_body["profile"]["harness_source"] == "install"
        assert joined_body["profile"]["model"] == "claude-opus-4-7"
        assert joined_body["profile"]["effort"] == "high"
        assert joined_body["hub"]["name"] == info["name"]
        assert "workspace_path" in joined_body

        # 5. whoami after join reflects hub state
        send(
            {
                "jsonrpc": "2.0",
                "id": 6,
                "method": "tools/call",
                "params": {"name": "whoami", "arguments": {}},
            }
        )
        whoami_after = wait_id(6)["result"]
        assert not whoami_after.get("isError"), whoami_after
        after_body = json.loads(whoami_after["content"][0]["text"])
        assert after_body["joined"] is True
        assert after_body["name"] == info["name"]
        assert after_body["agent_name"] == "bob"
        assert after_body["role"] == "implementer"
        assert after_body["hub"] == info["url"]

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
