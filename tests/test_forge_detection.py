"""Unit tests for forge detection (spec §10)."""

import subprocess
from pathlib import Path

import pytest
from agent_hub_common.discovery import (
    detect_forge,
    extract_origin_host,
    resolve_repository,
)


def test_extract_origin_host() -> None:
    cases = [
        ("git@github.com:RoboNater/robomate.git", "github.com"),
        ("https://github.com/RoboNater/robomate.git", "github.com"),
        ("git@gitlab.com:gitlab-org/gitlab.git", "gitlab.com"),
        ("https://gitlab.com/gitlab-org/gitlab.git", "gitlab.com"),
        ("git@gitlab-box.local:RoboNater/robomate-glab-sandbox.git", "gitlab-box.local"),
        ("https://gitlab-box.local/RoboNater/robomate-glab-sandbox.git", "gitlab-box.local"),
        ("https://gitlab.corp.net:8443/team/project.git", "gitlab.corp.net"),
        ("ssh://git@my-gitlab.org:2222/org/proj.git", "my-gitlab.org"),
        ("/var/git/local.git", None),
    ]
    for origin, expected in cases:
        assert extract_origin_host(origin) == expected


def test_detect_forge_well_known_hosts() -> None:
    assert detect_forge("git@github.com:example/repo.git") == "github"
    assert detect_forge("https://github.com/example/repo.git") == "github"
    assert detect_forge("git@gitlab.com:example/repo.git") == "gitlab"
    assert detect_forge("https://gitlab.com/example/repo.git") == "gitlab"


def test_detect_forge_via_config_toml(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    config = repo / ".robomate" / "config.toml"
    config.parent.mkdir()
    config.write_text(
        """[forge]
gitlab_hosts = ["gitlab-internal.corp.com", "gl.local"]
github_hosts = ["github-internal.corp.com"]
""",
        encoding="utf-8",
    )

    assert detect_forge("git@gitlab-internal.corp.com:team/repo.git", root=repo) == "gitlab"
    assert detect_forge("https://gl.local/team/repo.git", root=repo) == "gitlab"
    assert detect_forge("git@github-internal.corp.com:team/repo.git", root=repo) == "github"
    assert detect_forge("git@unknown-host.net:team/repo.git", root=repo) == "unknown"


def test_detect_forge_via_glab_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config_dir = tmp_path / ".config" / "glab-cli"
    config_dir.mkdir(parents=True)
    (config_dir / "config.yml").write_text(
        """hosts:
    gitlab.com:
        token: xxx
    gitlab-box.local:
        token: yyy
""",
        encoding="utf-8",
    )
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / ".config"))

    assert detect_forge("git@gitlab-box.local:RoboNater/robomate-glab-sandbox.git") == "gitlab"
    assert detect_forge("git@unconfigured.local:repo.git") == "unknown"


def test_resolve_repository_detects_gitlab_forge(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-b", "main"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "--allow-empty", "-m", "init"], cwd=repo, check=True)
    subprocess.run(
        ["git", "remote", "add", "origin", "git@gitlab.com:group/project.git"],
        cwd=repo,
        check=True,
    )
    subprocess.run(
        ["git", "symbolic-ref", "refs/remotes/origin/HEAD", "refs/remotes/origin/main"],
        cwd=repo,
        check=True,
    )

    info = resolve_repository(repo)
    assert info.forge == "gitlab"
    assert info.default_branch == "main"
