"""Registry lock, replacement, and stale entry pruning."""

from pathlib import Path

import pytest
from agent_hub_common.registry import deregister, live_entries, register, registry_path


def test_registry_register_prune_and_deregister(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env = {"XDG_STATE_HOME": str(tmp_path)}
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
