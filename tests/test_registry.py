"""Registry lock, keying by hub_id, name claims, and stale entry pruning."""

import ctypes
import os
import socket
import ssl
import sys
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from agent_hub_common import registry
from agent_hub_common.discovery import legacy_state_dir, write_hub_json
from agent_hub_common.registry import (
    RegistryError,
    forget,
    hub_entries,
    live_entries,
    mark_stopped,
    operator_token_path,
    register,
    registry_path,
)
from conftest import closed_port_url


def _entry(tmp_path: Path, hub_id: str, name: str, common: str = "/repo/.git") -> dict[str, object]:
    checkout = tmp_path / hub_id
    write_hub_json(legacy_state_dir(checkout), {"hub_id": hub_id, "name": name})
    return {
        "hub_id": hub_id,
        "name": name,
        "checkout": str(checkout),
        "git_common_dir": common,
        "url": f"http://{hub_id}",
        "pid": 123,
    }


@pytest.fixture
def env(tmp_path: Path) -> dict[str, str]:
    # registry_path() uses %LOCALAPPDATA% on Windows; isolate both locations
    # so hub_entries() never reads or prunes the operator's real registry.
    return {"XDG_STATE_HOME": str(tmp_path), "LOCALAPPDATA": str(tmp_path / "localappdata")}


def test_registry_keeps_stopped_hubs_and_prunes_gone_state(
    tmp_path: Path, env: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("agent_hub_common.registry.process_alive", lambda pid: pid == 123)
    monkeypatch.setattr(
        "agent_hub_common.registry.hub_healthy", lambda url, hub_id, *_: hub_id == "live"
    )
    register(_entry(tmp_path, "live", "a"), env)
    register(_entry(tmp_path, "stopped", "b"), env)
    register({"hub_id": "gone", "repo_root": "/repo/c", "url": "http://c", "pid": 456}, env)
    assert [x["hub_id"] for x in live_entries(env)] == ["live"]
    assert [(x["hub_id"], x["live"]) for x in hub_entries(env)] == [
        ("live", True),
        ("stopped", False),
    ]
    assert "gone" not in registry_path(env).read_text()
    assert not registry_path(env).with_name("registry.lock").exists()
    mark_stopped("live", env, pid=999)
    assert [x["hub_id"] for x in live_entries(env)] == ["live"]
    mark_stopped("live", env, pid=123)
    assert live_entries(env) == []
    assert [x["pid"] for x in hub_entries(env)] == [None, 123]


def test_registering_never_evicts_a_sibling_and_names_are_claimed(
    tmp_path: Path, env: dict[str, str]
) -> None:
    register(_entry(tmp_path, "a", "wt-a"), env)
    register(_entry(tmp_path, "b", "wt-b"), env)
    # The same checkout path, as the old repo_root keying would have evicted.
    register(_entry(tmp_path, "a", "wt-a") | {"url": "http://moved"}, env)
    assert [(x["hub_id"], x["url"]) for x in hub_entries(env)] == [
        ("b", "http://b"),
        ("a", "http://moved"),
    ]
    with pytest.raises(RegistryError, match="'wt-a' is taken by hub a"):
        register(_entry(tmp_path, "c", "wt-a"), env)
    # Another repository may use the same name.
    register(_entry(tmp_path, "d", "wt-a", common="/other/.git"), env)
    assert {x["hub_id"] for x in hub_entries(env)} == {"a", "b", "d"}


def test_a_name_held_by_a_hub_whose_state_is_gone_is_free(
    tmp_path: Path, env: dict[str, str]
) -> None:
    register(_entry(tmp_path, "a", "wt-a"), env)
    (tmp_path / "a" / ".robomate" / "hub.json").unlink()
    register(_entry(tmp_path, "c", "wt-a"), env)
    assert [x["hub_id"] for x in hub_entries(env)] == ["c"]


def test_forget_removes_the_entry_and_frees_the_name(
    tmp_path: Path, env: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """#159: forgetting drops the registry entry only; hub.json stays."""

    monkeypatch.setattr("agent_hub_common.registry.process_alive", lambda pid: False)
    monkeypatch.setattr("agent_hub_common.registry.hub_healthy", lambda url, hub_id, *_: False)
    register(_entry(tmp_path, "a", "wt-a"), env)
    register(_entry(tmp_path, "b", "wt-b"), env)
    removed = forget("a", env)
    assert removed is not None and removed["hub_id"] == "a"
    assert [x["hub_id"] for x in hub_entries(env)] == ["b"]
    # hub.json (the hub's state) is untouched: only the registry entry goes.
    assert (tmp_path / "a" / ".robomate" / "hub.json").is_file()
    # The freed name can be claimed by a sibling hub of the same repository.
    register(_entry(tmp_path, "c", "wt-a"), env)
    assert {x["hub_id"] for x in hub_entries(env)} == {"b", "c"}
    assert forget("missing", env) is None


def test_windows_liveness_never_signals_a_process(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[int] = []
    monkeypatch.setattr(sys, "platform", "win32")

    def fake_windows_probe(pid: int) -> bool:
        seen.append(pid)
        return pid == 123

    monkeypatch.setattr(registry, "_windows_process_alive", fake_windows_probe)

    def forbidden_kill(pid: int, signal: int) -> None:
        raise AssertionError("os.kill must not run on Windows")

    monkeypatch.setattr(os, "kill", forbidden_kill)
    assert registry.process_alive(123)
    assert not registry.process_alive(456)
    assert not registry.process_alive(0)
    assert seen == [123, 456]


def test_windows_handle_probe_distinguishes_running_and_exited(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    closed: list[int] = []

    class FakeFunction:
        def __init__(self, action: object) -> None:
            self.action = action

        def __call__(self, *args: object) -> object:
            assert callable(self.action)
            return self.action(*args)

    class FakeKernel:
        OpenProcess = FakeFunction(lambda access, inherit, pid: 0 if pid == 30 else pid)
        WaitForSingleObject = FakeFunction(lambda handle, timeout: 258 if handle == 10 else 0)
        CloseHandle = FakeFunction(lambda handle: closed.append(int(handle)))

    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(ctypes, "WinDLL", lambda *args, **kwargs: FakeKernel(), raising=False)
    monkeypatch.setattr(ctypes, "get_last_error", lambda: 5, raising=False)
    assert registry._windows_process_alive(10)
    assert not registry._windows_process_alive(20)
    assert registry._windows_process_alive(30)  # Access denied is conservative.
    assert closed == [10, 20]


def test_operator_token_lives_beside_the_registry_unless_overridden(tmp_path: Path) -> None:
    env = {"XDG_STATE_HOME": str(tmp_path / "xdg"), "LOCALAPPDATA": str(tmp_path / "local")}

    assert operator_token_path(env) == registry_path(env).with_name("operator-token")
    moved = {**env, "ROBOMATE_OPERATOR_TOKEN_FILE": str(tmp_path / "op")}
    assert operator_token_path(moved) == tmp_path / "op"
    assert operator_token_path({**env, "ROBOMATE_OPERATOR_TOKEN_FILE": " "}) == (
        registry_path(env).with_name("operator-token")
    )


@pytest.mark.parametrize("denials", [2, 3])
def test_registry_read_retries_transient_permission_error(
    tmp_path: Path, env: dict[str, str], monkeypatch: pytest.MonkeyPatch, denials: int
) -> None:
    # Two retries are allowed: two denials recover, a third is reported (#180).
    register(_entry(tmp_path, "a", "wt-a"), env)
    monkeypatch.setattr("agent_hub_common.registry._RETRY_DELAYS_S", (0.0, 0.0))
    path = registry_path(env)
    real_read_text = Path.read_text
    attempts = 0

    def flaky_read_text(self: Path, *args: Any, **kwargs: Any) -> str:
        nonlocal attempts
        if self == path:
            attempts += 1
            if attempts <= denials:
                raise PermissionError(13, "Permission denied", str(self))
        return real_read_text(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", flaky_read_text)
    if denials < 3:
        assert [x["hub_id"] for x in registry._read(path)] == ["a"]
    else:
        with pytest.raises(RegistryError, match="Permission denied"):
            registry._read(path)
    assert attempts == min(denials + 1, 3)


@pytest.mark.parametrize("denials", [2, 3])
def test_registry_write_retries_transient_permission_error(
    tmp_path: Path, env: dict[str, str], monkeypatch: pytest.MonkeyPatch, denials: int
) -> None:
    # Two retries are allowed: two denials recover, a third is reported (#180).
    register(_entry(tmp_path, "a", "wt-a"), env)
    second = _entry(tmp_path, "b", "wt-b")  # writes hub.json, so before the patch
    monkeypatch.setattr("agent_hub_common.registry._RETRY_DELAYS_S", (0.0, 0.0))
    path = registry_path(env)
    real_replace = os.replace
    attempts = 0

    def flaky_replace(src: Any, dst: Any) -> None:
        nonlocal attempts
        attempts += 1
        if attempts <= denials:
            raise PermissionError(13, "Permission denied", str(dst))
        real_replace(src, dst)

    monkeypatch.setattr("agent_hub_common.registry.os.replace", flaky_replace)
    if denials < 3:
        register(second, env)
        assert [x["hub_id"] for x in registry._read(path)] == ["a", "b"]
    else:
        with pytest.raises(PermissionError, match="Permission denied"):
            register(second, env)
        assert [x["hub_id"] for x in registry._read(path)] == ["a"]
    assert attempts == min(denials + 1, 3)
    # Neither the temporary file nor the lock is left behind.
    assert sorted(p.name for p in path.parent.iterdir()) == ["hubs.json"]


def test_registry_read_survives_windows_sharing_violation(
    tmp_path: Path, env: dict[str, str]
) -> None:
    # The #180 race: os.replace (or a scanner) holds hubs.json with DELETE
    # access, and Python's open() lacks FILE_SHARE_DELETE, so a read fails
    # with EACCES until that handle closes.
    if sys.platform != "win32":  # in the body, so mypy skips the rest off Windows
        pytest.skip("Windows share-mode semantics")
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateFileW.restype = wintypes.HANDLE
    kernel32.CreateFileW.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.LPVOID,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HANDLE,
    ]
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    delete, share_all, open_existing = 0x00010000, 0x7, 3

    register(_entry(tmp_path, "a", "wt-a"), env)
    path = registry_path(env)
    handle = kernel32.CreateFileW(str(path), delete, share_all, None, open_existing, 0, None)
    assert handle != wintypes.HANDLE(-1).value, ctypes.get_last_error()
    try:
        with pytest.raises(PermissionError):
            path.read_text(encoding="utf-8")
    except BaseException:
        kernel32.CloseHandle(handle)
        raise
    timer = threading.Timer(0.05, kernel32.CloseHandle, (handle,))
    timer.start()
    try:
        assert [x["hub_id"] for x in registry._read(path)] == ["a"]
    finally:
        timer.join()


# /healthz budgets (#230). Each fixture server is owned by its fixture, which
# shuts it down after the test.

# Wall-clock ceilings for "fails fast" and "stays bounded": far above the
# expected time, so a loaded host passes, but well below what unbounded or
# serial waits would take. Windows takes about 2 s to report a refused
# loopback connection.
FAST_FAILURE_S = 8.0


def test_a_hub_slower_than_the_old_budget_is_healthy(healthz: Callable[[str, float], str]) -> None:
    url = healthz("slow-hub", 1.0)
    assert registry.check_healthz(url) == registry.HealthCheck("slow-hub")
    assert registry.hub_healthy(url, "slow-hub")


def test_a_wrong_hub_id_is_refused_at_once(healthz: Callable[[str], str]) -> None:
    url = healthz("other-hub")
    started = time.monotonic()
    assert registry.check_healthz(url, 60.0) == registry.HealthCheck("other-hub")
    assert not registry.hub_healthy(url, "expected-hub", 60.0)
    assert time.monotonic() - started < FAST_FAILURE_S


def test_a_silent_endpoint_fails_within_its_budget(silent_url: Callable[[], str]) -> None:
    url = silent_url()
    started = time.monotonic()
    health = registry.check_healthz(url, 0.5)
    assert time.monotonic() - started < FAST_FAILURE_S
    assert health == registry.HealthCheck(None, "did not answer /healthz within 0.5 s", True)
    assert health.describe() == "did not answer /healthz within 0.5 s"
    assert not registry.hub_healthy(url, "any-hub", 0.5)


def test_a_refused_port_fails_fast_as_unreachable() -> None:
    started = time.monotonic()
    health = registry.check_healthz(closed_port_url(), 60.0)
    assert time.monotonic() - started < FAST_FAILURE_S
    assert health == registry.HealthCheck(None, "unreachable")
    assert health.describe() == "is unreachable"


def test_a_trickling_answer_is_cut_off_at_the_budget(
    trickle_url: Callable[[str, float], str],
) -> None:
    """#266 r1-1: the budget covers the whole response, not each read."""

    # A valid answer, but one byte every 50 ms: about 20 s in all.
    url = trickle_url("trickle-hub", 0.05)
    started = time.monotonic()
    health = registry.check_healthz(url, 0.5)
    elapsed = time.monotonic() - started
    assert health == registry.HealthCheck(None, "did not answer /healthz within 0.5 s", True)
    assert elapsed < 0.5 + FAST_FAILURE_S / 4
    # Given the time, the same server's answer is accepted.
    assert registry.check_healthz(trickle_url("trickle-hub", 0.0), 10.0) == registry.HealthCheck(
        "trickle-hub"
    )


def test_a_tls_handshake_gets_only_what_connecting_left(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#266 r2-1 / #264: assert the remaining budget at the TLS socket boundary.

    A real silent peer returned unreachable rather than timeout once under
    Windows load. This regression checks our timeout assignment with a virtual
    clock; the silent/trickling endpoint tests still exercise real IO deadlines.
    """
    clock = 100.0
    raw_socket = object()

    class TLS:
        timeout = 0.0
        closed = False

        def settimeout(self, seconds: float) -> None:
            self.timeout = seconds

        def do_handshake(self) -> None:
            nonlocal clock
            clock += self.timeout
            raise TimeoutError("silent TLS peer")

        def close(self) -> None:
            self.closed = True

    tls = TLS()

    class Context:
        def wrap_socket(
            self, sock: object, *, server_hostname: str, do_handshake_on_connect: bool
        ) -> TLS:
            assert sock is raw_socket
            assert server_hostname == "hub.test"
            assert not do_handshake_on_connect
            return tls

    def slow_connect(address: tuple[str, int], *, timeout: float) -> object:
        nonlocal clock
        assert address == ("hub.test", 443)
        assert timeout == pytest.approx(1.0)
        clock += 0.9
        return raw_socket

    monkeypatch.setattr(time, "monotonic", lambda: clock)
    monkeypatch.setattr(socket, "create_connection", slow_connect)
    monkeypatch.setattr(ssl, "create_default_context", Context)
    health = registry.check_healthz("https://hub.test", 1.0)
    assert health == registry.HealthCheck(None, "did not answer /healthz within 1 s", True)
    assert tls.timeout == pytest.approx(0.1)
    assert clock == pytest.approx(101.0)
    assert tls.closed


def test_hub_entries_probes_silent_entries_together_and_outside_the_lock(
    tmp_path: Path,
    env: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
    silent_url: Callable[[], str],
    healthz: Callable[[str, float], str],
    trickle_url: Callable[[str, float], str],
) -> None:
    """Many silent entries share one budget, and registry writers never wait on them.

    More entries than the old 16-probe cap, with the healthy one registered
    last: queued behind silent probes, it was listed as stopped without ever
    being asked (#266 r1-2).
    """

    budget = 3.0
    monkeypatch.setattr(registry, "HEALTHZ_LIVE_PID_TIMEOUT_S", budget)
    monkeypatch.setattr(registry, "process_alive", lambda pid: True)
    silent = [f"silent-{index}" for index in range(19)]
    for hub_id in silent:
        register(_entry(tmp_path, hub_id, hub_id) | {"url": silent_url()}, env)
    silent.append("trickle")
    # Keeps sending past the budget, a byte at a time (#266 r1-1).
    register(_entry(tmp_path, "trickle", "trickle") | {"url": trickle_url("trickle", 0.05)}, env)
    register(_entry(tmp_path, "slow", "slow") | {"url": healthz("slow", 1.0)}, env)
    listing: list[list[dict[str, Any]]] = []
    started = time.monotonic()
    lister = threading.Thread(target=lambda: listing.append(hub_entries(env)))
    lister.start()
    try:
        time.sleep(0.3)
        # The lister is mid-probe; a write still gets the lock at once.
        register(_entry(tmp_path, "late", "late") | {"url": closed_port_url()}, env)
        assert time.monotonic() - started < budget
    finally:
        lister.join(timeout=60)
    elapsed = time.monotonic() - started
    # One budget in all: serial probes would take 20 * 3 s, and the trickling
    # answer alone would take about 20 s to arrive.
    assert budget <= elapsed < 2 * budget
    assert {(x["hub_id"], x["live"]) for x in listing[0]} == {
        *((hub_id, False) for hub_id in silent),
        ("slow", True),
    }
    # The probe kept every entry, and did not drop the one written meanwhile.
    assert {x["hub_id"] for x in hub_entries(env)} >= {*silent, "slow", "late"}


@pytest.mark.parametrize("platform", ["win32", "linux"])
def test_lock_creation_permission_error_is_busy_only_on_windows(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, platform: str
) -> None:
    path = tmp_path / "hubs.json"
    real_open = os.open
    attempts = 0

    def flaky_open(*args: Any, **kwargs: Any) -> int:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise PermissionError("delete pending")
        return real_open(*args, **kwargs)

    monkeypatch.setattr(sys, "platform", platform)
    monkeypatch.setattr(os, "open", flaky_open)
    if platform == "win32":
        with registry._locked(path):
            assert path.with_name("registry.lock").exists()
        assert attempts == 2
    else:
        with pytest.raises(PermissionError, match="delete pending"), registry._locked(path):
            pytest.fail("must not acquire")
        assert attempts == 1
    assert not path.with_name("registry.lock").exists()


@pytest.mark.parametrize("failure", ["create", "stale_unlink", "stale_stat"])
def test_lock_sharing_errors_obey_deadline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    path = tmp_path / "hubs.json"
    lock = path.with_name("registry.lock")
    if failure != "create":
        lock.touch()
        os.utime(lock, (time.time() - 60,) * 2)
    clock = 0.0

    def advance(seconds: float) -> None:
        nonlocal clock
        clock += seconds

    def denied(*args: Any, **kwargs: Any) -> Any:
        raise PermissionError("sharing violation")

    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(time, "monotonic", lambda: clock)
    monkeypatch.setattr(time, "sleep", advance)
    if failure == "create":
        monkeypatch.setattr(os, "open", denied)
    elif failure == "stale_unlink":
        monkeypatch.setattr(Path, "unlink", denied)
    else:
        real_stat = Path.stat

        def denied_lock_stat(self: Path, *args: Any, **kwargs: Any) -> os.stat_result:
            if self == lock:
                raise PermissionError("sharing violation")
            return real_stat(self, *args, **kwargs)

        monkeypatch.setattr(Path, "stat", denied_lock_stat)
    with (
        pytest.raises(RegistryError, match="last failure: access or sharing error"),
        registry._locked(path),
    ):
        pytest.fail("must time out")
    assert 5 <= clock < 5.1


def test_lock_cleanup_permission_error_preserves_completed_operation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(registry, "_RETRY_DELAYS_S", (0.0, 0.0))
    attempts = 0

    def denied(*args: Any, **kwargs: Any) -> None:
        nonlocal attempts
        attempts += 1
        raise PermissionError("sharing violation")

    monkeypatch.setattr(Path, "unlink", denied)
    with registry._locked(tmp_path / "hubs.json"):
        pass
    assert attempts == 3


def test_stale_registry_lock_is_reclaimed(tmp_path: Path) -> None:
    lock = tmp_path / "registry.lock"
    lock.touch()
    os.utime(lock, (time.time() - 31,) * 2)
    with registry._locked(tmp_path / "hubs.json"):
        assert lock.stat().st_mtime > time.time() - 5
    assert not lock.exists()
