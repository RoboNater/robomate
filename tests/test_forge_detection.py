"""Unit tests for forge detection (spec §10)."""

import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest
from agent_hub_common.discovery import (
    DiscoveryError,
    detect_forge,
    extract_origin_host,
    resolve_repository,
)


@pytest.fixture(autouse=True)
def isolate_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Ensure all tests run with an isolated XDG_CONFIG_HOME and fail on unmocked subprocesses."""
    empty_config = tmp_path / "empty_xdg"
    empty_config.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(empty_config))


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


def test_detect_forge_ignores_repo_root_config_toml(tmp_path: Path) -> None:
    """Only .robomate/config.toml is inspected; a root config.toml is ignored."""
    repo = tmp_path / "repo"
    repo.mkdir()
    root_config = repo / "config.toml"
    root_config.write_text(
        """[forge]
gitlab_hosts = ["gitlab-ignored.corp.com"]
""",
        encoding="utf-8",
    )

    assert detect_forge("git@gitlab-ignored.corp.com:team/repo.git", root=repo) == "unknown"


def test_detect_forge_malformed_config_toml_raises_discovery_error(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    config = repo / ".robomate" / "config.toml"
    config.parent.mkdir()
    config.write_text("invalid toml content [[[", encoding="utf-8")

    with pytest.raises(DiscoveryError, match="invalid configuration in"):
        detect_forge("git@some-host.com:team/repo.git", root=repo)


def test_detect_forge_via_glab_config_exact_host_match(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_dir = tmp_path / "xdg" / "glab-cli"
    config_dir.mkdir(parents=True)
    (config_dir / "config.yml").write_text(
        """hosts:
    gitlab.com:
        token: xxx
    gitlab.example.com:
        token: yyy
""",
        encoding="utf-8",
    )
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))

    # Exact match works
    assert detect_forge("git@gitlab.example.com:team/repo.git") == "gitlab"

    # Substrings must NOT match
    assert detect_forge("git@example.com:team/repo.git") == "unknown"
    assert detect_forge("git@lab.example.com:team/repo.git") == "unknown"


def test_detect_forge_via_gh_hosts_exact_host_match(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_dir = tmp_path / "xdg" / "gh"
    config_dir.mkdir(parents=True)
    (config_dir / "hosts.yml").write_text(
        """github.com:
    user: test
    oauth_token: xxx
ghe.corp.net:
    user: test
    oauth_token: yyy
""",
        encoding="utf-8",
    )
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))

    # Exact match works
    assert detect_forge("https://ghe.corp.net/team/repo.git") == "github"

    # Substring must NOT match
    assert detect_forge("https://hub.com/team/repo.git") == "unknown"
    assert detect_forge("https://corp.net/team/repo.git") == "unknown"


def test_detect_forge_cli_probes_not_run_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    """When probe_cli=False (the default), subprocess.run is never invoked."""
    called = False

    def fake_run(*args: Any, **kwargs: Any) -> Any:
        nonlocal called
        called = True
        raise AssertionError("subprocess.run should not be called when probe_cli=False")

    monkeypatch.setattr(subprocess, "run", fake_run)
    monkeypatch.setattr(shutil, "which", lambda cmd: "/usr/bin/" + cmd)

    result = detect_forge("git@unknown-server.net:team/repo.git", probe_cli=False)
    assert result == "unknown"
    assert called is False


def test_detect_forge_cli_probe_glab_success(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_which(cmd: str) -> str | None:
        return f"/usr/bin/{cmd}" if cmd == "glab" else None

    def fake_run(args: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        if "glab" in args and "--hostname=gl.internal.net" in args:
            return subprocess.CompletedProcess(
                args, 0, stdout="Logged in to gl.internal.net", stderr=""
            )
        return subprocess.CompletedProcess(args, 1, stdout="", stderr="error")

    monkeypatch.setattr(shutil, "which", fake_which)
    monkeypatch.setattr(subprocess, "run", fake_run)

    result = detect_forge("git@gl.internal.net:team/repo.git", probe_cli=True)
    assert result == "gitlab"


def test_detect_forge_cli_probe_gh_success(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_which(cmd: str) -> str | None:
        return f"/usr/bin/{cmd}" if cmd == "gh" else None

    def fake_run(args: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        if "gh" in args and "--hostname=gh.internal.net" in args:
            return subprocess.CompletedProcess(
                args, 0, stdout="Logged in to gh.internal.net", stderr=""
            )
        return subprocess.CompletedProcess(args, 1, stdout="", stderr="error")

    monkeypatch.setattr(shutil, "which", fake_which)
    monkeypatch.setattr(subprocess, "run", fake_run)

    result = detect_forge("git@gh.internal.net:team/repo.git", probe_cli=True)
    assert result == "github"


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


@pytest.mark.parametrize("origin", ["https://[::1/a/b.git", "https://[gitlab-box.local]/a/b.git"])
def test_malformed_bracketed_host(origin: str) -> None:
    assert extract_origin_host(origin) is None
    assert detect_forge(origin, probe_cli=True) == "unknown"
