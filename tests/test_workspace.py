"""Exercise real full clones and workspace conflicts without network access."""

import json
import os
import subprocess
import sys
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
# Windows cannot execute the bash wrapper; it runs the same entry point directly.
BOOTSTRAP = (
    [sys.executable, str(ROOT / "scripts/bootstrap-workspace.py")]
    if os.name == "nt"
    else [str(ROOT / "scripts/bootstrap-workspace.sh")]
)


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
                *BOOTSTRAP,
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
        [*BOOTSTRAP, "bob", str(bob), str(repository)],
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


def test_workspace_under_a_non_ascii_path_is_accepted(tmp_path: Path) -> None:
    # Git prints paths as UTF-8, whatever the locale (#166).
    origin = tmp_path / "café" / "origin"
    origin.mkdir(parents=True)
    command("git", "init", "--initial-branch=main", str(origin))
    bob = tmp_path / "café" / "bob é"
    identity = bootstrap(origin, bob)
    settings = WorkerSettings.from_env(
        {
            "HUB_URL": "http://hub",
            "HUB_TOKEN": "test",
            "AGENT_NAME": "bob",
            "HUB_WORKSPACE": str(bob),
        }
    )
    assert settings.workspace == bob
    assert identity["path"] == str(bob)
    assert settings.profile.workspace_id == identity["workspace_id"]


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
        [*BOOTSTRAP, "bob", str(destination), str(repository)],
        capture_output=True,
        text=True,
    )
    assert result.returncode != 0
    assert "missing robo-agents-workspace.json" in result.stderr
    assert "remove the incomplete clone" in result.stderr


def _make_repo(tmp_path: Path, name: str = "repo") -> Path:
    root = tmp_path / name
    root.mkdir(parents=True)
    command("git", "init", "-b", "main", str(root))
    command("git", "-C", str(root), "remote", "add", "origin", "git@github.com:example/repo.git")
    command(
        "git",
        "-C",
        str(root),
        "symbolic-ref",
        "refs/remotes/origin/HEAD",
        "refs/remotes/origin/main",
    )
    command(
        "git",
        "-C",
        str(root),
        "-c",
        "user.name=T",
        "-c",
        "user.email=t@example.com",
        "commit",
        "--allow-empty",
        "-m",
        "start",
    )
    command("git", "-C", str(root), "update-ref", "refs/remotes/origin/main", "HEAD")
    return root


async def test_hub_workspace_creates_ahead_of_join_and_join_reuses_it(tmp_path: Path) -> None:
    """M2 Step 7b (#314): `hub.workspace` creates ahead of join with binding/lock."""

    import uuid

    from agent_hub import create_app
    from agent_hub.database import initialize_database
    from agent_hub.worktrees import worktree_lock_reason
    from agent_hub_common import HubSettings
    from agent_hub_common.config_layers import load_config_layers
    from agent_hub_common.discovery import (
        checkout_git_dir,
        claim_worktree_root,
        read_binding,
        resolve_repository,
    )
    from conftest import BASE_URL, TOKEN

    root = _make_repo(tmp_path / "ws7b")
    repo = resolve_repository(root)
    layered = load_config_layers(git_common_dir=repo.git_common_dir)
    assert layered.worktree_root is not None
    claim_worktree_root(layered.worktree_root, repo.git_common_dir)
    state = layered.worktree_root / "repo" / "state"
    state.mkdir(parents=True, exist_ok=True)
    settings = HubSettings(
        host="127.0.0.1",
        port=8420,
        public_url="http://hub.test:8420",
        state_dir=state,
        database_path=state / "hub.db",
        token=TOKEN,
        token_file=layered.worktree_root / "repo" / "token" / "token",
        guides_dir=tmp_path / "guides",
        default_wait_s=0.2,
        max_wait_s=1.0,
        lost_after_s=60.0,
        sweep_interval_s=3600.0,
    )
    settings.guides_dir.mkdir(parents=True, exist_ok=True)
    (settings.guides_dir / "worker.md").write_text("# W\n", encoding="utf-8")
    (settings.guides_dir / "implementer.md").write_text("# I\n", encoding="utf-8")
    (settings.guides_dir / "orchestrator.md").write_text("# O\n", encoding="utf-8")
    (settings.guides_dir / "forge").mkdir(parents=True, exist_ok=True)
    (settings.guides_dir / "forge" / "github.md").write_text("# G\n", encoding="utf-8")
    (settings.guides_dir / "forge" / "orchestrator-github.md").write_text(
        "# OG\n", encoding="utf-8"
    )
    hub_info: dict[str, Any] = {
        "hub_id": f"hub-ws7b-{uuid.uuid4().hex[:8]}",
        "name": "repo",
        "checkout": str(repo.root),
        "repo_root": str(repo.root),
        "git_common_dir": str(repo.git_common_dir),
        "origin": repo.origin,
        "forge": repo.forge,
        "default_branch": repo.default_branch,
        "url": settings.public_url,
        "port": 8420,
    }
    app = create_app(settings, hub_info=hub_info)
    store = app.state.store
    initialize_database(store.path)
    store.initialize_workflow()

    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url=BASE_URL,
            headers={"Authorization": f"Bearer {TOKEN}"},
        ) as bare,
    ):
        from agent_hub.rpc import RpcDispatcher

        dispatcher = app.state.rpc
        assert isinstance(dispatcher, RpcDispatcher)
        # Lifespan clears a pre-set token (no operator file), so set it after
        # the hub starts, like test_rpc does.
        dispatcher.operator_token = "op-secret"
        # Operator-only: without the credential it is refused.
        refused = await bare.post(
            "/rpc",
            json=rpc("hub.workspace", {"agent": "bob"}),
        )
        assert refused.status_code == 200
        body = refused.json()
        assert "error" in body and body["error"]["code"] == -32005

        # With the credential it creates the worktree and prints its path shape.
        created = await bare.post(
            "/rpc",
            json=rpc("hub.workspace", {"agent": "bob"}),
            headers={"X-Robomate-Operator": "op-secret"},
        )
        assert created.status_code == 200, created.text
        result = created.json()["result"]
        expected = str(layered.worktree_root / "repo" / "bob")
        assert result["path"] == expected
        assert result["agent"] == "bob"
        assert Path(expected).is_dir()
        binding = read_binding(checkout_git_dir(Path(expected)))
        assert binding is not None and binding.role == "agent" and binding.agent == "bob"
        listing = command(
            "git",
            f"--git-dir={repo.git_common_dir}",
            "worktree",
            "list",
            "--porcelain",
        )
        assert f"worktree {Path(expected).as_posix()}" in listing
        assert worktree_lock_reason("repo") in listing

        # A later join reuses the same worktree and identity.
        join_meta: dict[str, Any] = {
            MetaKeys.AGENT: "bob",
            MetaKeys.STANDING_ROLE: "implementer",
            MetaKeys.TAKEOVER: False,
            MetaKeys.CAPABILITIES: ["python"],
            MetaKeys.SCHEMA_VERSION: 1,
            MetaKeys.OPERATION_ID: uuid.uuid4().hex,
            MetaKeys.WORKER_INSTANCE_ID: "bob-pre",
            MetaKeys.WORKSPACE_EXPLICIT: False,
            MetaKeys.WORKSPACE_PATH: str(Path.cwd()),
        }
        joined = await bare.post(
            "/a2a", json=rpc("message/send", message("READY", metadata=join_meta))
        )
        assert joined.status_code == 200, joined.text
        meta = joined.json()["result"].get("metadata") or {}
        assert meta.get(MetaKeys.WORKSPACE_PATH) == expected
        assert meta.get(MetaKeys.WORKSPACE_KIND) == "worktree"
        assert meta.get(MetaKeys.WORKSPACE_ID) == result["workspace_id"]


async def test_join_returns_setup_and_applies_pins(tmp_path: Path) -> None:
    """M2 Step 7b (#314): join carries `setup` and operator model/effort pins."""

    import uuid

    from agent_hub import create_app
    from agent_hub.database import initialize_database
    from agent_hub_common import HubSettings
    from agent_hub_common.config_layers import load_config_layers
    from agent_hub_common.discovery import claim_worktree_root, resolve_repository
    from conftest import BASE_URL, TOKEN

    root = _make_repo(tmp_path / "pins7b")
    repo = resolve_repository(root)
    repo_cfg = repo.git_common_dir / "robomate" / "config.toml"
    repo_cfg.parent.mkdir(parents=True, exist_ok=True)
    repo_cfg.write_text(
        'setup = "uv sync --locked"\n[agents.bob]\nmodel = "pinned-model"\neffort = "high"\n',
        encoding="utf-8",
    )
    layered = load_config_layers(git_common_dir=repo.git_common_dir)
    assert layered.setup == "uv sync --locked"
    assert layered.worktree_root is not None
    claim_worktree_root(layered.worktree_root, repo.git_common_dir)
    state = layered.worktree_root / "repo" / "state"
    state.mkdir(parents=True, exist_ok=True)
    settings = HubSettings(
        host="127.0.0.1",
        port=8420,
        public_url="http://hub.test:8420",
        state_dir=state,
        database_path=state / "hub.db",
        token=TOKEN,
        token_file=layered.worktree_root / "repo" / "token" / "token",
        guides_dir=tmp_path / "guides2",
        default_wait_s=0.2,
        max_wait_s=1.0,
        lost_after_s=60.0,
        sweep_interval_s=3600.0,
    )
    settings.guides_dir.mkdir(parents=True, exist_ok=True)
    (settings.guides_dir / "worker.md").write_text("# W\n", encoding="utf-8")
    (settings.guides_dir / "implementer.md").write_text("# I\n", encoding="utf-8")
    (settings.guides_dir / "orchestrator.md").write_text("# O\n", encoding="utf-8")
    (settings.guides_dir / "forge").mkdir(parents=True, exist_ok=True)
    (settings.guides_dir / "forge" / "github.md").write_text("# G\n", encoding="utf-8")
    (settings.guides_dir / "forge" / "orchestrator-github.md").write_text(
        "# OG\n", encoding="utf-8"
    )
    hub_info: dict[str, Any] = {
        "hub_id": f"hub-pins-{uuid.uuid4().hex[:8]}",
        "name": "repo",
        "checkout": str(repo.root),
        "repo_root": str(repo.root),
        "git_common_dir": str(repo.git_common_dir),
        "origin": repo.origin,
        "forge": repo.forge,
        "default_branch": repo.default_branch,
        "url": settings.public_url,
        "port": 8420,
    }
    app = create_app(settings, hub_info=hub_info)
    store = app.state.store
    initialize_database(store.path)
    store.initialize_workflow()

    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url=BASE_URL,
            headers={"Authorization": f"Bearer {TOKEN}"},
        ) as bare,
    ):
        join_meta: dict[str, Any] = {
            MetaKeys.AGENT: "bob",
            MetaKeys.STANDING_ROLE: "implementer",
            MetaKeys.TAKEOVER: False,
            MetaKeys.CAPABILITIES: ["python"],
            MetaKeys.SCHEMA_VERSION: 1,
            MetaKeys.OPERATION_ID: uuid.uuid4().hex,
            MetaKeys.WORKER_INSTANCE_ID: "bob-pins",
            MetaKeys.WORKSPACE_EXPLICIT: False,
            MetaKeys.WORKSPACE_PATH: str(Path.cwd()),
        }
        joined = await bare.post(
            "/a2a", json=rpc("message/send", message("READY", metadata=join_meta))
        )
        assert joined.status_code == 200, joined.text
        meta = joined.json()["result"].get("metadata") or {}
        assert meta.get(MetaKeys.SETUP) == "uv sync --locked"
        assert meta.get(MetaKeys.MODEL) == "pinned-model"
        assert meta.get(MetaKeys.EFFORT) == "high"
        agent = store.agent_by_name("bob")
        assert agent is not None
        assert agent.model == "pinned-model"
        assert agent.effort == "high"
        assert agent.model_source.value == "env"
        assert agent.effort_source.value == "env"
