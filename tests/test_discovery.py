"""Repository discovery and atomic local metadata."""

import json
import subprocess
from pathlib import Path

import pytest
from agent_hub_common.discovery import (
    DiscoveryError,
    HubEndpoint,
    discover,
    ensure_excluded,
    read_hub_json,
    repo_root,
    resolve_repository,
    write_hub_json,
)


def git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


@pytest.fixture
def repository(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    git(root, "init", "-b", "main")
    git(root, "config", "user.name", "Test")
    git(root, "config", "user.email", "test@example.com")
    git(root, "commit", "--allow-empty", "-m", "initial")
    git(root, "remote", "add", "origin", "git@github.com:example/repo.git")
    git(root, "symbolic-ref", "refs/remotes/origin/HEAD", "refs/remotes/origin/main")
    return root


def test_repo_resolution_main_and_worktree(repository: Path, tmp_path: Path) -> None:
    worktree = tmp_path / "linked"
    git(repository, "worktree", "add", "-b", "linked", str(worktree))
    for cwd in (repository, worktree):
        assert repo_root(cwd) == (repository, repository / ".git")
        info = resolve_repository(cwd)
        assert (info.root, info.default_branch, info.forge) == (repository, "main", "github")


def test_exclude_once_and_atomic_hub_json(
    repository: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    common = repository / ".git"
    ensure_excluded(common)
    ensure_excluded(common)
    assert (common / "info/exclude").read_text().splitlines().count("/.robomate/") == 1
    write_hub_json(repository, {"port": 8420})
    assert read_hub_json(repository) == {"port": 8420}

    def fail_replace(*args: object) -> None:
        raise OSError("injected replace failure")

    monkeypatch.setattr("agent_hub_common.discovery.os.replace", fail_replace)
    with pytest.raises(OSError, match="injected"):
        write_hub_json(repository, {"port": 8421})
    assert json.loads((repository / ".robomate/hub.json").read_text()) == {"port": 8420}
    assert sorted(p.name for p in (repository / ".robomate").iterdir()) == ["hub.json"]


def test_discovery_prefers_explicit_then_repo(repository: Path) -> None:
    write_hub_json(repository, {"url": "http://repo:8420", "hub_id": "repo-id"})
    (repository / ".robomate/token").write_text("repo-token\n")
    assert discover(repository, {}) == HubEndpoint("http://repo:8420", "repo-token")
    assert discover(repository, {
        "ROBOMATE_HUB_URL": "http://explicit:8421/", "ROBOMATE_TOKEN": "explicit-token"
    }) == HubEndpoint("http://explicit:8421", "explicit-token")


def test_discovery_uses_sole_registry_hub_or_lists_choices(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "hub"
    (root / ".robomate").mkdir(parents=True)
    (root / ".robomate/token").write_text("registry-token\n")
    one = {"repo_root": str(root), "url": "http://registry:8420", "hub_id": "one"}
    monkeypatch.setattr("agent_hub_common.registry.live_entries", lambda env: [one])
    assert discover(tmp_path, {}) == HubEndpoint("http://registry:8420", "registry-token")
    monkeypatch.setattr("agent_hub_common.registry.live_entries", lambda env: [one, {
        "repo_root": "/other", "url": "http://other:8421", "hub_id": "two"
    }])
    with pytest.raises(DiscoveryError, match="Registered hubs:.*hub.*other"):
        discover(tmp_path, {})
