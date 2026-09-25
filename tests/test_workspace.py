"""Exercise real full clones and workspace conflicts without network access."""

import json
import os
import subprocess
from pathlib import Path
from typing import Any, cast

import httpx
import pytest
from agent_hub.store import DuplicateAgentError, HubStore
from agent_hub_common import AgentProfile, ConfigurationError, MetaKeys
from agent_hub_common.workspace import IDENTITY_FILE
from conftest import message, rpc
from worker_mcp.config import WorkerSettings

ROOT = Path(__file__).resolve().parents[1]


def command(*args: str) -> str:
    return subprocess.run(args, check=True, capture_output=True, text=True).stdout.strip()


@pytest.fixture
def repository(tmp_path: Path) -> Path:
    origin = tmp_path / "origin"
    command("git", "init", "--initial-branch=main", str(origin))
    command(
        "git",
        "-C",
        str(origin),
        "-c",
        "user.name=Test",
        "-c",
        "user.email=test@example.com",
        "commit",
        "--allow-empty",
        "-m",
        "baseline",
    )
    return origin


def bootstrap(repository: Path, destination: Path, agent: str = "bob") -> dict[str, str]:
    return cast(
        dict[str, str],
        json.loads(
            command(
                str(ROOT / "scripts/bootstrap-workspace.sh"),
                agent,
                str(destination),
                str(repository),
            )
        ),
    )


def test_bootstrap_persistence_and_isolation(repository: Path, tmp_path: Path) -> None:
    bob, charlie = tmp_path / "bob", tmp_path / "charlie"
    first = bootstrap(repository, bob)
    second = bootstrap(repository, charlie, "charlie")
    assert first["workspace_id"] != second["workspace_id"]
    assert bootstrap(repository, bob) == first
    identity = bob / ".git" / IDENTITY_FILE
    if os.name != "nt":
        assert identity.stat().st_mode & 0o777 == 0o600
    assert command("git", "-C", str(bob), "ls-files") == ""
    (bob / "uncommitted-marker").write_text("isolated")
    assert not (charlie / "uncommitted-marker").exists()
    result = subprocess.run(
        [str(ROOT / "scripts/bootstrap-workspace.sh"), "bob", str(bob), str(repository)],
        capture_output=True,
        text=True,
    )
    assert result.returncode != 0 and "dirty" in result.stderr
    assert (bob / "uncommitted-marker").read_text() == "isolated"


def test_workspace_configuration(repository: Path, tmp_path: Path) -> None:
    bob = tmp_path / "bob"
    identity = bootstrap(repository, bob)
    env = {
        "HUB_URL": "http://hub",
        "HUB_TOKEN": "test",
        "AGENT_NAME": "bob",
        "HUB_WORKSPACE": str(bob),
    }
    settings = WorkerSettings.from_env(env)
    assert settings.workspace == bob
    assert settings.profile.workspace_id == identity["workspace_id"]
    for raw in ("relative", "", str(tmp_path / "missing"), str(bob / ".." / "bob")):
        with pytest.raises(ConfigurationError, match="HUB_WORKSPACE"):
            WorkerSettings.from_env(env | {"HUB_WORKSPACE": raw})
    with pytest.raises(ConfigurationError, match="agent/path"):
        WorkerSettings.from_env(env | {"AGENT_NAME": "charlie"})
    (bob / ".git" / IDENTITY_FILE).write_text('{"workspace_id":"bad"}')
    with pytest.raises(ConfigurationError, match="64 lowercase"):
        WorkerSettings.from_env(env)
    (bob / ".git" / IDENTITY_FILE).write_text(json.dumps({"workspace_id": int("1" * 64)}))
    with pytest.raises(ConfigurationError, match="64 lowercase"):
        WorkerSettings.from_env(env)
    (bob / ".git" / IDENTITY_FILE).unlink()
    with pytest.raises(ConfigurationError, match="bootstrap-workspace"):
        WorkerSettings.from_env(env)


@pytest.mark.parametrize("status", ["lost", "released"])
def test_workspace_readmission_and_stale_heartbeat(store: HubStore, status: str) -> None:
    from agent_hub.database import database

    profile = AgentProfile(workspace_id="a" * 64)
    store.check_in("bob", profile, worker_instance_id="old")
    with pytest.raises(DuplicateAgentError, match="workspace"):
        store.check_in("charlie", profile, worker_instance_id="other")
    with pytest.raises(DuplicateAgentError, match="live worker"):
        store.check_in("bob", profile, worker_instance_id="new")
    with database(store.path) as connection:
        connection.execute("UPDATE agent SET status = ? WHERE name = 'bob'", (status,))
    store.check_in("charlie", profile, worker_instance_id="other")
    if status == "lost":
        assert not store.heartbeat("bob", "old", None)
    with pytest.raises(DuplicateAgentError, match="workspace"):
        store.check_in("bob", profile, worker_instance_id="new")
    store.release_agent("charlie")
    store.check_in("bob", profile, worker_instance_id="new")
    assert not store.heartbeat("bob", "old", None)


async def test_workspace_wire_conflict_and_replay(
    client: httpx.AsyncClient, hub_store: HubStore
) -> None:
    metadata: dict[str, Any] = {
        MetaKeys.AGENT: "bob",
        MetaKeys.WORKSPACE_ID: "a" * 64,
        MetaKeys.SCHEMA_VERSION: 1,
        MetaKeys.OPERATION_ID: "first",
        MetaKeys.WORKER_INSTANCE_ID: "bob-process",
    }
    payload = rpc("message/send", message("READY", metadata=metadata))
    first = await client.post("/a2a", json=payload)
    assert first.status_code == 200
    replay = await client.post("/a2a", json=payload)
    assert replay.json() == first.json()
    metadata.update(
        {
            MetaKeys.AGENT: "charlie",
            MetaKeys.OPERATION_ID: "second",
            MetaKeys.WORKER_INSTANCE_ID: "charlie-process",
        }
    )
    conflict = await client.post(
        "/a2a", json=rpc("message/send", message("READY", metadata=metadata))
    )
    assert conflict.status_code == 409
    assert "workspace " + "a" * 64 in conflict.json()["error"]["message"]


def test_busy_workspace_is_occupied(store: HubStore) -> None:
    profile = AgentProfile(workspace_id="b" * 64)
    store.check_in("bob", profile, worker_instance_id="first")
    store.assign_task("bob", "implementer", "work", "independent edits")
    with pytest.raises(DuplicateAgentError, match="workspace"):
        store.check_in("charlie", profile, worker_instance_id="second")


async def test_worker_reports_persisted_identity(
    client: httpx.AsyncClient, hub_store: HubStore, repository: Path, tmp_path: Path
) -> None:
    from worker_mcp.client import WorkerHubClient

    destination = tmp_path / "bob"
    persisted = bootstrap(repository, destination)
    settings = WorkerSettings.from_env(
        {
            "HUB_URL": "http://hub.test",
            "HUB_TOKEN": "test-token",
            "AGENT_NAME": "bob",
            "HUB_WORKSPACE": str(destination),
        }
    )
    worker = WorkerHubClient(settings, http_client=client)
    await worker.check_in()
    registered = hub_store.agent_by_name("bob")
    assert registered is not None and registered.workspace_id == persisted["workspace_id"]


def test_bootstrap_missing_identity_error(repository: Path, tmp_path: Path) -> None:
    destination = tmp_path / "interrupted"
    subprocess.run(["git", "clone", str(repository), str(destination)], check=True)
    result = subprocess.run(
        [str(ROOT / "scripts/bootstrap-workspace.sh"), "bob", str(destination), str(repository)],
        capture_output=True,
        text=True,
    )
    assert result.returncode != 0
    assert "missing robo-agents-workspace.json" in result.stderr
    assert "remove the incomplete clone" in result.stderr
