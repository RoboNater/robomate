"""Registry lock, keying by hub_id, name claims, and stale entry pruning."""

import ctypes
import os
import sys
from pathlib import Path

import pytest
from agent_hub_common import registry
from agent_hub_common.discovery import write_hub_json
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


def _entry(tmp_path: Path, hub_id: str, name: str, common: str = "/repo/.git") -> dict[str, object]:
    checkout = tmp_path / hub_id
    write_hub_json(checkout, {"hub_id": hub_id, "name": name})
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
        "agent_hub_common.registry.hub_healthy", lambda url, hub_id: hub_id == "live"
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
    monkeypatch.setattr("agent_hub_common.registry.hub_healthy", lambda url, hub_id: False)
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
