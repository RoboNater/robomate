"""M2 Step 7a (#244): worktrees created at join, reused per (hub, agent).

The hub runs `git worktree add --detach <root>/<hub>/<agent>/ origin/<default>`
at join, writes the agent binding into the worktree's own git directory, and
locks the worktree with the hub's reason. A second join reuses it. The
orchestrator gets one the same way. An explicit `HUB_WORKSPACE` creates no
worktree and keeps its full-clone check; an explicit workspace with no matching
identity is refused, while the bridge-cwd fallback stays lenient (r2-2).
"""

from __future__ import annotations

import json
import subprocess
import uuid
from pathlib import Path
from typing import Any

import httpx
import pytest
from agent_hub import create_app
from agent_hub.database import SCHEMA_VERSION, database, initialize_database
from agent_hub.store import HubStore
from agent_hub.worktrees import (
    WorktreeError,
    WorktreeManager,
    worktree_lock_reason,
)
from agent_hub_common import (
    SCHEMA_VERSION as WIRE_SCHEMA_VERSION,
)
from agent_hub_common import (
    AgentProfile,
    HubSettings,
    MetaKeys,
)
from agent_hub_common.discovery import (
    checkout_git_dir,
    plan_hub_placement,
    read_binding,
    resolve_repository,
)
from conftest import BASE_URL, CALLER_HEADERS, TOKEN, message, rpc


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True, text=True
    ).stdout.strip()


def make_repo(tmp_path: Path, name: str = "repo") -> Path:
    """A git repo with a commit and an origin/<default> ref for worktree adds."""

    root = tmp_path / name
    root.mkdir(parents=True)
    _git(root, "init", "-b", "main")
    _git(root, "remote", "add", "origin", "git@github.com:example/repo.git")
    _git(root, "symbolic-ref", "refs/remotes/origin/HEAD", "refs/remotes/origin/main")
    _git(
        root,
        "-c",
        "user.name=T",
        "-c",
        "user.email=t@example.com",
        "commit",
        "--allow-empty",
        "-m",
        "start",
    )
    _git(root, "update-ref", "refs/remotes/origin/main", "HEAD")
    return root


def make_manager(
    root: Path, tmp_path: Path, hub: str = "repo", agent: str = "bob"
) -> tuple[WorktreeManager, Path, Path]:
    repo = resolve_repository(root)
    from agent_hub_common.config_layers import load_config_layers
    from agent_hub_common.discovery import claim_worktree_root

    layered = load_config_layers(git_common_dir=repo.git_common_dir)
    assert layered.worktree_root is not None
    claim_worktree_root(layered.worktree_root, repo.git_common_dir)
    state, token = (
        layered.worktree_root / hub / "state",
        layered.worktree_root / hub / "token" / "token",
    )
    state.mkdir(parents=True, exist_ok=True)
    token.parent.mkdir(parents=True, exist_ok=True)
    manager = WorktreeManager(
        hub_id=f"hub-{hub}",
        hub_name=hub,
        checkout=repo.root,
        git_common_dir=repo.git_common_dir,
        default_branch=repo.default_branch,
        worktree_root=layered.worktree_root,
        state_dir=state,
        token_file=token,
    )
    return manager, layered.worktree_root, repo.git_common_dir


def test_join_creates_detached_locked_worktree_with_binding_and_reuses(
    tmp_path: Path,
) -> None:
    """Plan Step 7a: create at origin/<default>, lock, bind, reuse per (hub, agent)."""

    root = make_repo(tmp_path)
    manager, worktree_root, common = make_manager(root, tmp_path)
    path, workspace_id = manager.ensure("bob")
    assert path == worktree_root / "repo" / "bob"
    assert path.is_dir()
    assert len(workspace_id) == 64 and all(c in "0123456789abcdef" for c in workspace_id)
    # Detached at origin/main, locked with the hub's reason.
    listing = subprocess.run(
        ["git", f"--git-dir={common}", "worktree", "list", "--porcelain"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    # `git worktree list --porcelain` prints forward slashes even on Windows,
    # while `str(Path)` uses backslashes there.
    assert f"worktree {path.as_posix()}" in listing
    assert "detached" in listing
    assert worktree_lock_reason("repo") in listing
    # Agent binding in the worktree's own git directory, with the workspace id.
    binding = read_binding(checkout_git_dir(path))
    assert binding is not None
    assert binding.role == "agent" and binding.agent == "bob"
    assert binding.hub_id == "hub-repo" and binding.workspace_id == workspace_id
    # A second join reuses the same worktree and identity.
    again_path, again_id = manager.ensure("bob")
    assert (again_path, again_id) == (path, workspace_id)


def test_up_in_agent_worktree_is_refused_naming_the_hub(tmp_path: Path) -> None:
    """Spec §4: `up` runs in the owning checkout, never in an agent's worktree."""

    root = make_repo(tmp_path)
    manager, _, _ = make_manager(root, tmp_path)
    path, _ = manager.ensure("bob")
    repo = resolve_repository(path)
    with pytest.raises(Exception, match="hub repo.*agent 'bob'|agent.*bob"):
        plan_hub_placement(repo, None)


def test_state_and_token_are_refused_as_agent_names(tmp_path: Path) -> None:
    root = make_repo(tmp_path)
    manager, _, _ = make_manager(root, tmp_path)
    for reserved in ("state", "token"):
        with pytest.raises(WorktreeError, match="reserved"):
            manager.ensure(reserved)


def test_two_hubs_of_one_repository_create_worktrees_without_collision(
    tmp_path: Path,
) -> None:
    root = make_repo(tmp_path)
    first, worktree_root, _ = make_manager(root, tmp_path, hub="repo")
    second, _, _ = make_manager(root, tmp_path, hub="other")
    # Same agent name under two hub namespaces: distinct paths, both locked.
    first_path, first_id = first.ensure("bob")
    second_path, second_id = second.ensure("bob")
    assert first_path != second_path
    assert first_path == worktree_root / "repo" / "bob"
    assert second_path == worktree_root / "other" / "bob"
    assert first_id != second_id
    assert first_path.is_dir() and second_path.is_dir()


def _hub_app_with_repo(
    tmp_path: Path, hub: str = "repo"
) -> tuple[Any, HubStore, Path, Path, dict[str, Any]]:
    """An app whose hub knows its repository, so joins create worktrees."""

    root = make_repo(tmp_path / "app" / hub)
    repo = resolve_repository(root)
    from agent_hub_common.config_layers import load_config_layers
    from agent_hub_common.discovery import claim_worktree_root

    layered = load_config_layers(git_common_dir=repo.git_common_dir)
    assert layered.worktree_root is not None
    claim_worktree_root(layered.worktree_root, repo.git_common_dir)
    state = layered.worktree_root / hub / "state"
    state.mkdir(parents=True, exist_ok=True)
    settings = HubSettings(
        host="127.0.0.1",
        port=8420,
        public_url="http://hub.example:8420",
        state_dir=state,
        database_path=state / "hub.db",
        token=TOKEN,
        token_file=layered.worktree_root / hub / "token" / "token",
        guides_dir=tmp_path / "guides",
        default_wait_s=0.2,
        max_wait_s=1.0,
        lost_after_s=60.0,
        sweep_interval_s=3600.0,
    )
    settings.guides_dir.mkdir(parents=True, exist_ok=True)
    (settings.guides_dir / "worker.md").write_text("# Worker\n", encoding="utf-8")
    (settings.guides_dir / "implementer.md").write_text("# Impl\n", encoding="utf-8")
    (settings.guides_dir / "orchestrator.md").write_text("# Orch\n", encoding="utf-8")
    (settings.guides_dir / "forge").mkdir(parents=True, exist_ok=True)
    (settings.guides_dir / "forge" / "github.md").write_text("# GH\n", encoding="utf-8")
    (settings.guides_dir / "forge" / "orchestrator-github.md").write_text(
        "# Orch GH\n", encoding="utf-8"
    )
    hub_info: dict[str, Any] = {
        "hub_id": f"hub-{hub}-{uuid.uuid4().hex[:8]}",
        "name": hub,
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
    store: HubStore = app.state.store
    initialize_database(store.path)
    store.initialize_workflow()
    return app, store, layered.worktree_root, repo.git_common_dir, hub_info


async def _a2a_join(
    app: Any, name: str, role: str, *, explicit: bool = False, path: str | None = None
) -> tuple[dict[str, Any], HubStore]:
    store: HubStore = app.state.store
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url=BASE_URL,
            headers={"Authorization": f"Bearer {TOKEN}"},
        ) as client,
    ):
        metadata: dict[str, Any] = {
            MetaKeys.AGENT: name,
            MetaKeys.STANDING_ROLE: role,
            MetaKeys.TAKEOVER: False,
            MetaKeys.CAPABILITIES: ["python"],
            MetaKeys.SCHEMA_VERSION: 1,
            MetaKeys.OPERATION_ID: uuid.uuid4().hex,
            MetaKeys.WORKER_INSTANCE_ID: f"{name}-instance",
            MetaKeys.WORKSPACE_EXPLICIT: explicit,
            MetaKeys.WORKSPACE_PATH: path or str(Path.cwd()),
        }
        payload = rpc("message/send", message("READY", metadata=metadata))
        response = await client.post("/a2a", json=payload)
        assert response.status_code == 200, response.text
        result = response.json()["result"]
        return result, store


async def test_worker_join_creates_worktree_and_records_path_and_kind(
    tmp_path: Path,
) -> None:
    app, store, worktree_root, _, hub_info = _hub_app_with_repo(tmp_path)
    result, _ = await _a2a_join(app, "bob", "implementer")
    meta = result.get("metadata") or {}
    workspace_path = meta.get(MetaKeys.WORKSPACE_PATH)
    workspace_kind = meta.get(MetaKeys.WORKSPACE_KIND)
    workspace_id = meta.get(MetaKeys.WORKSPACE_ID)
    assert workspace_kind == "worktree"
    assert workspace_path == str(worktree_root / hub_info["name"] / "bob")
    assert isinstance(workspace_id, str) and len(workspace_id) == 64
    assert Path(str(workspace_path)).is_dir()
    agent = store.agent_by_name("bob")
    assert agent is not None
    assert (agent.workspace_path, agent.workspace_kind) == (workspace_path, "worktree")
    assert agent.workspace_id == workspace_id
    # The porcelain listing shows the worktree locked with the hub's reason.
    common = hub_info["git_common_dir"]
    listing = subprocess.run(
        ["git", f"--git-dir={common}", "worktree", "list", "--porcelain"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    # `git worktree list --porcelain` prints forward slashes even on Windows.
    assert f"worktree {Path(str(workspace_path)).as_posix()}" in listing
    assert worktree_lock_reason(hub_info["name"]) in listing


async def test_second_join_reuses_the_worktree(tmp_path: Path) -> None:
    app, store, worktree_root, _, hub_info = _hub_app_with_repo(tmp_path)
    first, _ = await _a2a_join(app, "bob", "implementer")
    first_meta = first.get("metadata") or {}
    # A live name without takeover is refused; with takeover it reattaches.

    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url=BASE_URL,
            headers={"Authorization": f"Bearer {TOKEN}"},
        ) as client,
    ):
        metadata: dict[str, Any] = {
            MetaKeys.AGENT: "bob",
            MetaKeys.STANDING_ROLE: "implementer",
            MetaKeys.TAKEOVER: True,
            MetaKeys.CAPABILITIES: ["python"],
            MetaKeys.SCHEMA_VERSION: 1,
            MetaKeys.OPERATION_ID: uuid.uuid4().hex,
            MetaKeys.WORKER_INSTANCE_ID: "bob-second",
            MetaKeys.WORKSPACE_EXPLICIT: False,
            MetaKeys.WORKSPACE_PATH: str(Path.cwd()),
        }
        payload = rpc("message/send", message("READY", metadata=metadata))
        response = await client.post("/a2a", json=payload)
        assert response.status_code == 200, response.text
        second_meta = response.json()["result"].get("metadata") or {}
    assert second_meta.get(MetaKeys.WORKSPACE_PATH) == first_meta.get(MetaKeys.WORKSPACE_PATH)
    assert second_meta.get(MetaKeys.WORKSPACE_ID) == first_meta.get(MetaKeys.WORKSPACE_ID)


async def test_orchestrator_join_creates_its_own_worktree(tmp_path: Path) -> None:
    app, store, worktree_root, _, hub_info = _hub_app_with_repo(tmp_path)

    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url=BASE_URL,
            headers={
                "Authorization": f"Bearer {TOKEN}",
                **CALLER_HEADERS,
            },
        ) as client,
    ):
        payload = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "join",
            "params": {
                "name": "alice",
                "role": "orchestrator",
                "takeover": False,
                "profile": {},
                "workspace_path": str(Path.cwd()),
                "workspace_explicit": False,
            },
        }
        response = await client.post("/rpc", json=payload)
        assert response.status_code == 200, response.text
        result = response.json()["result"]
    assert result["workspace_kind"] == "worktree"
    assert result["workspace_path"] == str(worktree_root / hub_info["name"] / "alice")
    assert isinstance(result["workspace_id"], str) and len(result["workspace_id"]) == 64
    agent = store.agent_by_name("alice")
    assert agent is not None
    assert agent.standing_role.value == "orchestrator"
    assert (agent.workspace_path, agent.workspace_kind) == (
        result["workspace_path"],
        "worktree",
    )


async def test_explicit_workspace_creates_no_worktree_and_keeps_clone(
    tmp_path: Path,
) -> None:
    """An explicit HUB_WORKSPACE join records a clone and creates no worktree."""

    import sys

    app, store, worktree_root, _, hub_info = _hub_app_with_repo(tmp_path)
    origin = tmp_path / "origin"
    subprocess.run(["git", "init", "--initial-branch=main", str(origin)], check=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(origin),
            "-c",
            "user.name=T",
            "-c",
            "user.email=t@e.com",
            "commit",
            "--allow-empty",
            "-m",
            "base",
        ],
        check=True,
        capture_output=True,
    )
    destination = tmp_path / "bob-clone"
    root = Path(__file__).resolve().parents[1]
    bootstrap = (
        [sys.executable, str(root / "scripts/bootstrap-workspace.py")]
        if __import__("os").name == "nt"
        else [str(root / "scripts/bootstrap-workspace.sh")]
    )
    out = subprocess.run(
        [*bootstrap, "bob", str(destination), str(origin)],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    identity = json.loads(out)
    # Join with the bootstrap identity so the hub records the clone.

    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url=BASE_URL,
            headers={"Authorization": f"Bearer {TOKEN}"},
        ) as client,
    ):
        metadata: dict[str, Any] = {
            MetaKeys.AGENT: "bob2",
            MetaKeys.STANDING_ROLE: "implementer",
            MetaKeys.TAKEOVER: False,
            MetaKeys.CAPABILITIES: ["python"],
            MetaKeys.SCHEMA_VERSION: 1,
            MetaKeys.OPERATION_ID: uuid.uuid4().hex,
            MetaKeys.WORKER_INSTANCE_ID: "bob2-instance",
            MetaKeys.WORKSPACE_ID: identity["workspace_id"],
            MetaKeys.WORKSPACE_EXPLICIT: True,
            MetaKeys.WORKSPACE_PATH: str(destination),
        }
        payload = rpc("message/send", message("READY", metadata=metadata))
        response = await client.post("/a2a", json=payload)
        assert response.status_code == 200, response.text
        meta = response.json()["result"].get("metadata") or {}
    assert meta.get(MetaKeys.WORKSPACE_KIND) == "clone"
    assert meta.get(MetaKeys.WORKSPACE_PATH) == str(destination)
    agent = store.agent_by_name("bob2")
    assert agent is not None
    assert (agent.workspace_path, agent.workspace_kind) == (str(destination), "clone")
    # No worktree was created for the explicit clone.
    assert not (worktree_root / hub_info["name"] / "bob2").exists()


async def test_explicit_mismatch_is_refused_while_fallback_stays_lenient(
    tmp_path: Path,
) -> None:
    app, _, _, _, _ = _hub_app_with_repo(tmp_path)

    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url=BASE_URL,
            headers={"Authorization": f"Bearer {TOKEN}"},
        ) as client,
    ):
        # Explicit with no identity: refused (r2-2).
        refused = await client.post(
            "/a2a",
            json=rpc(
                "message/send",
                message(
                    "READY",
                    metadata={
                        MetaKeys.AGENT: "bob",
                        MetaKeys.STANDING_ROLE: "implementer",
                        MetaKeys.TAKEOVER: False,
                        MetaKeys.SCHEMA_VERSION: 1,
                        MetaKeys.OPERATION_ID: uuid.uuid4().hex,
                        MetaKeys.WORKER_INSTANCE_ID: "bob-explicit",
                        MetaKeys.WORKSPACE_EXPLICIT: True,
                        MetaKeys.WORKSPACE_PATH: str(tmp_path / "missing"),
                    },
                ),
            ),
        )
        assert refused.status_code == 200, refused.text
        assert "error" in refused.json()
        assert "HUB_WORKSPACE" in refused.json()["error"]["message"]
        # Fallback with no identity: lenient, creates a worktree.
        ok = await client.post(
            "/a2a",
            json=rpc(
                "message/send",
                message(
                    "READY",
                    metadata={
                        MetaKeys.AGENT: "bob",
                        MetaKeys.STANDING_ROLE: "implementer",
                        MetaKeys.TAKEOVER: False,
                        MetaKeys.SCHEMA_VERSION: 1,
                        MetaKeys.OPERATION_ID: uuid.uuid4().hex,
                        MetaKeys.WORKER_INSTANCE_ID: "bob-fallback",
                        MetaKeys.WORKSPACE_EXPLICIT: False,
                        MetaKeys.WORKSPACE_PATH: str(Path.cwd()),
                    },
                ),
            ),
        )
        assert ok.status_code == 200, ok.text
        assert (ok.json()["result"].get("metadata") or {}).get(MetaKeys.WORKSPACE_KIND) == (
            "worktree"
        )


def test_db_records_workspace_path_and_kind_and_rejects_other_kinds(
    tmp_path: Path,
) -> None:
    path = tmp_path / "hub.db"
    initialize_database(path)
    store = HubStore(path)
    store.initialize_workflow()
    store.check_in(
        "bob",
        AgentProfile(),
        standing_role="implementer",
        worker_instance_id="i-1",
        workspace_path="/wt/repo/bob",
        workspace_kind="worktree",
    )
    agent = store.agent_by_name("bob")
    assert agent is not None
    assert (agent.workspace_path, agent.workspace_kind) == ("/wt/repo/bob", "worktree")
    with pytest.raises(Exception, match="workspace_kind|CHECK|constraint"):
        store.check_in(
            "charlie",
            AgentProfile(),
            standing_role="implementer",
            worker_instance_id="i-2",
            workspace_path="/wt/repo/charlie",
            workspace_kind="yurt",
        )


def test_migration_from_v24_leaves_workspace_path_and_kind_null(
    tmp_path: Path,
) -> None:
    import sqlite3

    fixture = Path(__file__).resolve().parent / "fixtures" / "schema_v24.sql"
    path = tmp_path / "v24.db"
    with sqlite3.connect(path) as connection:
        connection.executescript(fixture.read_text(encoding="utf-8"))
        connection.execute(
            "INSERT INTO agent (name, status, context_id, last_seen)"
            " VALUES ('bob', 'idle', 'ctx-bob', '2026-10-22T00:00:00Z')"
        )
    initialize_database(path)
    with database(path) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
        row = connection.execute(
            "SELECT workspace_path, workspace_kind FROM agent WHERE name = 'bob'"
        ).fetchone()
        assert (row["workspace_path"], row["workspace_kind"]) == (None, None)


def test_wire_schema_version_stays_1() -> None:
    assert WIRE_SCHEMA_VERSION == 1
    assert SCHEMA_VERSION == 26


async def test_explicit_join_revalidates_identity_for_join_name(tmp_path: Path) -> None:
    """r1-1: a clone configured for bob cannot authorize a join as charlie."""

    import sys

    from agent_hub_common.discovery import HubEndpoint
    from worker_mcp.client import WorkerHubClient, WorkerProtocolError
    from worker_mcp.config import WorkerSettings

    app, store, worktree_root, _, hub_info = _hub_app_with_repo(tmp_path)
    origin = tmp_path / "origin-r11"
    subprocess.run(["git", "init", "--initial-branch=main", str(origin)], check=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(origin),
            "-c",
            "user.name=T",
            "-c",
            "user.email=t@e.com",
            "commit",
            "--allow-empty",
            "-m",
            "base",
        ],
        check=True,
        capture_output=True,
    )
    destination = tmp_path / "bob-clone-r11"
    root = Path(__file__).resolve().parents[1]
    bootstrap = (
        [sys.executable, str(root / "scripts/bootstrap-workspace.py")]
        if __import__("os").name == "nt"
        else [str(root / "scripts/bootstrap-workspace.sh")]
    )
    subprocess.run(
        [*bootstrap, "bob", str(destination), str(origin)],
        check=True,
        capture_output=True,
    )
    env = {
        "ROBOMATE_HUB_URL": BASE_URL,
        "ROBOMATE_TOKEN": TOKEN,
        "AGENT_NAME": "bob",
        "HUB_WORKSPACE": str(destination),
    }
    settings = WorkerSettings.from_discovered(
        HubEndpoint(BASE_URL, TOKEN), agent_name="bob", environ=env
    )
    # The configured profile carries bob's clone ID before join runs.
    assert settings.profile.workspace_id is not None
    import httpx

    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url=BASE_URL,
            headers={"Authorization": f"Bearer {TOKEN}"},
        ) as http,
        WorkerHubClient(settings, http_client=http) as bridge,
    ):
        with pytest.raises(WorkerProtocolError, match="HUB_WORKSPACE"):
            await bridge.join(name="charlie", role="implementer")
    # No worktree was created for the refused explicit join.
    assert not (worktree_root / hub_info["name"] / "charlie").exists()
    assert store.agent_by_name("charlie") is None


async def test_from_env_settings_join_honors_explicit_workspace(tmp_path: Path) -> None:
    """r1-2: `from_env` workspace (environ=None) still joins as a clone."""

    import sys

    from worker_mcp.client import WorkerHubClient
    from worker_mcp.config import WorkerSettings

    app, store, worktree_root, _, hub_info = _hub_app_with_repo(tmp_path)
    origin = tmp_path / "origin-r12"
    subprocess.run(["git", "init", "--initial-branch=main", str(origin)], check=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(origin),
            "-c",
            "user.name=T",
            "-c",
            "user.email=t@e.com",
            "commit",
            "--allow-empty",
            "-m",
            "base",
        ],
        check=True,
        capture_output=True,
    )
    destination = tmp_path / "bob-clone-r12"
    root = Path(__file__).resolve().parents[1]
    bootstrap = (
        [sys.executable, str(root / "scripts/bootstrap-workspace.py")]
        if __import__("os").name == "nt"
        else [str(root / "scripts/bootstrap-workspace.sh")]
    )
    out = subprocess.run(
        [*bootstrap, "bob", str(destination), str(origin)],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    identity = json.loads(out)
    settings = WorkerSettings.from_env(
        {
            "HUB_URL": BASE_URL,
            "HUB_TOKEN": TOKEN,
            "AGENT_NAME": "bob",
            "HUB_WORKSPACE": str(destination),
        }
    )
    assert settings.workspace is not None and settings.environ is None
    assert settings.profile.workspace_id == identity["workspace_id"]
    import httpx

    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url=BASE_URL,
            headers={"Authorization": f"Bearer {TOKEN}"},
        ) as http,
        WorkerHubClient(settings, http_client=http) as bridge,
    ):
        res = await bridge.join(name="bob", role="implementer")
    assert res["workspace_kind"] == "clone"
    assert res["workspace_path"] == str(destination)
    assert res["workspace_id"] == identity["workspace_id"]
    assert not (worktree_root / hub_info["name"] / "bob").exists()
    agent = store.agent_by_name("bob")
    assert agent is not None
    assert (agent.workspace_path, agent.workspace_kind) == (str(destination), "clone")


# M2 Step 7c (#315): the hub names `robomate/<hub>/<agent>/<label>` in the
# run's first implementer assignment, refuses an existing local or origin ref,
# reuses it for later tasks, records it on the workflow, and namespaces two
# hubs of one repository without collision.


def test_branch_label_derivation_prefers_issue_number_and_slugifies() -> None:
    from agent_hub.branches import derive_branch_label

    assert derive_branch_label("Address issue `RoboNater/robomate#315`, merge it.") == "315"
    assert derive_branch_label("Fix login") == "fix-login"
    assert derive_branch_label("  ") != ""
    # Reserved words (hub/agent name rules) fall back to a SHA prefix, so the
    # hub never names a branch it would then refuse (r1-2).
    for reserved in ("Token", "con", "State"):
        label = derive_branch_label(reserved)
        assert label not in ("token", "con", "state")
        assert len(label) == 8


def _branch_store(
    tmp_path: Path, hub: str = "myhub", goal: str = "Address issue `o/r#42`"
) -> tuple[HubStore, Path]:
    repo = make_repo(tmp_path / f"branch-{hub}")
    _, _, common = make_manager(repo, tmp_path, hub=hub)
    path = tmp_path / f"{hub}.db"
    initialize_database(path)
    store = HubStore(path, hub_name=hub, git_common_dir=common)
    store.initialize_workflow(goal)
    store.check_in("bob", AgentProfile(), standing_role="implementer", worker_instance_id="i-bob")
    store.check_in("bob2", AgentProfile(), standing_role="implementer", worker_instance_id="i-bob2")
    return store, common


def test_first_implementer_assignment_names_branch_and_reuses(tmp_path: Path) -> None:
    store, _ = _branch_store(tmp_path, hub="myhub", goal="Address issue `o/r#42`, merge it.")
    first = store.assign_task("bob", "implementer", "Build", "build it")
    assert first.branch == "robomate/myhub/bob/42"
    assert first.run_id is not None
    state = store.get_state()
    assert state["workflow"]["branch"] == "robomate/myhub/bob/42"
    # Later tasks of the same run reuse the same branch, even for another agent.
    second = store.assign_task("bob2", "implementer", "Build more", "more")
    assert second.branch == first.branch
    assert store.get_state()["workflow"]["branch"] == first.branch


def test_reserved_word_statement_still_names_a_branch(tmp_path: Path) -> None:
    """r1-2: a reserved-word slug falls back instead of failing every assignment."""

    store, _ = _branch_store(tmp_path, hub="myhub", goal="Token")
    task = store.assign_task("bob", "implementer", "Build", "build it")
    assert task.branch is not None
    assert task.branch.startswith("robomate/myhub/bob/")
    assert task.branch.split("/")[-1] not in ("token", "con", "state")


def test_existing_local_ref_is_refused(tmp_path: Path) -> None:
    from agent_hub.store import ConflictError

    store, common = _branch_store(tmp_path, hub="myhub", goal="Address issue `o/r#43`")
    # Create the local branch ref the hub would name.
    subprocess.run(
        ["git", f"--git-dir={common}", "branch", "robomate/myhub/bob/43"],
        check=True,
        capture_output=True,
        cwd=common.parent if common.name == ".git" else Path.cwd(),
    )
    with pytest.raises(ConflictError, match="already exists locally"):
        store.assign_task("bob", "implementer", "Build", "build it")


def test_existing_origin_ref_is_refused(tmp_path: Path) -> None:
    from agent_hub.store import ConflictError

    store, common = _branch_store(tmp_path, hub="myhub", goal="Address issue `o/r#44`")
    cwd = common.parent if common.name == ".git" else Path.cwd()
    head = subprocess.run(
        ["git", "rev-parse", "HEAD"], check=True, capture_output=True, text=True, cwd=cwd
    ).stdout.strip()
    subprocess.run(
        [
            "git",
            f"--git-dir={common}",
            "update-ref",
            "refs/remotes/origin/robomate/myhub/bob/44",
            head,
        ],
        check=True,
        capture_output=True,
        cwd=cwd,
    )
    with pytest.raises(ConflictError, match="already exists on origin"):
        store.assign_task("bob", "implementer", "Build", "build it")


def test_two_hubs_name_branches_without_collision(tmp_path: Path) -> None:
    repo = make_repo(tmp_path / "two-hubs")
    _, _, common = make_manager(repo, tmp_path, hub="repo")
    first_path = tmp_path / "first.db"
    second_path = tmp_path / "second.db"
    for p in (first_path, second_path):
        initialize_database(p)
    first = HubStore(first_path, hub_name="repo", git_common_dir=common)
    second = HubStore(second_path, hub_name="other", git_common_dir=common)
    for s in (first, second):
        s.initialize_workflow("Address issue `o/r#45`, merge it.")
        s.check_in("bob", AgentProfile(), standing_role="implementer", worker_instance_id="i-b")
    first_task = first.assign_task("bob", "implementer", "Build", "build")
    second_task = second.assign_task("bob", "implementer", "Build", "build")
    assert first_task.branch == "robomate/repo/bob/45"
    assert second_task.branch == "robomate/other/bob/45"
    assert first_task.branch != second_task.branch


def test_migration_from_v25_leaves_branch_null(tmp_path: Path) -> None:
    import sqlite3

    fixture = Path(__file__).resolve().parent / "fixtures" / "schema_v25.sql"
    path = tmp_path / "v25.db"
    with sqlite3.connect(path) as connection:
        connection.executescript(fixture.read_text(encoding="utf-8"))
        connection.execute(
            "INSERT INTO workflow (id, run_id, goal, status, created)"
            " VALUES ('wf', '20261022_deadbeef', 'Goal', 'active', '2026-10-22T00:00:00Z')"
        )
    initialize_database(path)
    with database(path) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
        row = connection.execute("SELECT branch FROM workflow WHERE id = 'wf'").fetchone()
        assert row["branch"] is None
