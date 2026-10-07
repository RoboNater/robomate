"""Registry lock, replacement, and stale entry pruning."""

import ctypes
import os
import sys
from pathlib import Path

import pytest
from agent_hub_common import registry
from agent_hub_common.registry import (
    deregister,
    live_entries,
    operator_token_path,
    register,
    registry_path,
)


def test_registry_register_prune_and_deregister(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # registry_path() uses %LOCALAPPDATA% on Windows; isolate both locations
    # so live_entries() never reads or prunes the operator's real registry.
    env = {"XDG_STATE_HOME": str(tmp_path), "LOCALAPPDATA": str(tmp_path / "localappdata")}
    monkeypatch.setattr("agent_hub_common.registry.process_alive", lambda pid: pid == 123)
    monkeypatch.setattr(
        "agent_hub_common.registry.hub_healthy", lambda url, hub_id: hub_id == "live"
    )
    register({"hub_id": "live", "repo_root": "/repo/a", "url": "http://a", "pid": 123}, env)
    register({"hub_id": "stale", "repo_root": "/repo/b", "url": "http://b", "pid": 456}, env)
    assert [x["hub_id"] for x in live_entries(env)] == ["live"]
    assert "stale" not in registry_path(env).read_text()
    assert not registry_path(env).with_name("registry.lock").exists()
    deregister("live", env)
    assert live_entries(env) == []


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
