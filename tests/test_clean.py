"""Operator cleanup exercises real git worktrees in disposable repositories."""

import shutil
import sys
from pathlib import Path

import pytest
import robomate.cli as cli
from agent_hub_common.discovery import Binding, checkout_git_dir, write_binding
from test_worktrees import _git, make_manager, make_repo


@pytest.fixture
def cleanup_repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = make_repo(tmp_path)
    manager, _, _ = make_manager(root, tmp_path)
    write_binding(
        checkout_git_dir(root),
        Binding(
            "owner", manager.hub_id, manager.hub_name, root, manager.state_dir, manager.token_file
        ),
    )
    operator = tmp_path / "operator"
    operator.write_text("operator-token", encoding="utf-8")
    operator.chmod(0o600)
    monkeypatch.setenv("ROBOMATE_OPERATOR_TOKEN_FILE", str(operator))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "xdg-state"))
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "local-app-data"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg-config"))
    monkeypatch.setenv("APPDATA", str(tmp_path / "app-data"))
    for key in ("ROBOMATE_HUB_URL", "ROBOMATE_TOKEN", "ROBOMATE_TOKEN_FILE"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.chdir(root)
    return root


def clean(monkeypatch: pytest.MonkeyPatch, *args: str) -> None:
    monkeypatch.setattr(sys, "argv", ["robomate", "clean", *args])
    cli.main()


def test_clean_removes_only_selected_agents_and_merged_branches_keeps_state(
    cleanup_repo: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    root = cleanup_repo
    manager, _, common = make_manager(root, tmp_path)
    path, _ = manager.ensure("bob")
    other, _, _ = make_manager(root, tmp_path, hub="other")
    other_path, _ = other.ensure("bob")
    branch = "robomate/repo/bob/done"
    _git(path, "switch", "--no-track", "-c", branch)
    for name in ("robomate/other/bob/done", "operator-branch"):
        _git(root, "branch", name)
    unfinished = "robomate/repo/bob/unfinished"
    _git(root, "branch", unfinished)
    _git(root, "switch", unfinished)
    _git(
        root,
        "-c",
        "user.name=T",
        "-c",
        "user.email=t@example.com",
        "commit",
        "--allow-empty",
        "-m",
        "unfinished",
    )
    _git(root, "switch", "main")
    report = manager.state_dir / "runs" / "report.md"
    report.parent.mkdir()
    report.write_bytes(b"history\n")
    manager.token_file.write_bytes(b"bearer\n")
    clean(monkeypatch)
    assert not path.exists() and other_path.is_dir() and root.is_dir()
    branches = _git(root, "for-each-ref", "--format=%(refname:short)", "refs/heads")
    assert branch not in branches.splitlines()
    assert unfinished in branches.splitlines()
    assert "robomate/other/bob/done" in branches.splitlines()
    assert "operator-branch" in branches.splitlines()
    assert report.read_bytes() == b"history\n"
    assert manager.token_file.read_bytes() == b"bearer\n"
    assert "Removed agent worktree" in capsys.readouterr().out
    assert "robomate hub other" in _git(root, "worktree", "list", "--porcelain")
    assert common.is_dir()


@pytest.mark.parametrize("change", ["untracked", "staged", "unstaged", "unpushed", "detached"])
def test_clean_refuses_work_and_keeps_its_lock(
    cleanup_repo: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    change: str,
) -> None:
    manager, _, _ = make_manager(cleanup_repo, tmp_path)
    path, _ = manager.ensure("bob")
    if change in ("unpushed", "detached"):
        if change == "unpushed":
            _git(path, "switch", "-c", "robomate/repo/bob/wip")
        _git(
            path,
            "-c",
            "user.name=T",
            "-c",
            "user.email=t@example.com",
            "commit",
            "--allow-empty",
            "-m",
            "unpublished",
        )
    else:
        (path / "work.txt").write_text("keep", encoding="utf-8")
        if change != "untracked":
            _git(path, "add", "work.txt")
        if change == "unstaged":
            _git(
                path,
                "-c",
                "user.name=T",
                "-c",
                "user.email=t@example.com",
                "commit",
                "-m",
                "published",
            )
            _git(path, "update-ref", "refs/remotes/origin/work", "HEAD")
            (path / "work.txt").write_text("changed", encoding="utf-8")
    with pytest.raises(SystemExit) as exc:
        clean(monkeypatch)
    assert exc.value.code == 1 and path.is_dir()
    output = capsys.readouterr()
    assert (
        "unpushed commits" if change in ("unpushed", "detached") else "uncommitted changes"
    ) in output.err
    assert "locked robomate hub repo" in _git(cleanup_repo, "worktree", "list", "--porcelain")


@pytest.mark.parametrize("selector", [None, "repo", "hub-repo", "checkout"])
def test_clean_reports_orphans_when_state_has_been_removed(
    cleanup_repo: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    selector: str | None,
) -> None:
    manager, _, _ = make_manager(cleanup_repo, tmp_path)
    path, _ = manager.ensure("bob")
    shutil.rmtree(manager.state_dir)
    arguments = (
        []
        if selector is None
        else ["--hub", str(cleanup_repo) if selector == "checkout" else selector]
    )
    clean(monkeypatch, *arguments)
    assert not path.exists() and not manager.state_dir.exists()
    assert "Orphaned agent worktree" in capsys.readouterr().out


def test_clean_requires_operator_credential(
    cleanup_repo: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager, _, _ = make_manager(cleanup_repo, tmp_path)
    path, _ = manager.ensure("bob")
    monkeypatch.setenv("ROBOMATE_OPERATOR_TOKEN_FILE", str(tmp_path / "missing"))
    with pytest.raises(SystemExit) as exc:
        clean(monkeypatch)
    assert exc.value.code == 1 and path.is_dir()


def test_clean_refuses_live_or_starting_hub(
    cleanup_repo: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager, _, _ = make_manager(cleanup_repo, tmp_path)
    path, _ = manager.ensure("bob")
    with cli._hub_lock(manager.state_dir), pytest.raises(SystemExit) as exc:
        clean(monkeypatch)
    assert exc.value.code == 1 and path.is_dir()


def test_clean_keeps_worktree_with_foreign_lock(
    cleanup_repo: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager, _, _ = make_manager(cleanup_repo, tmp_path)
    path, _ = manager.ensure("bob")
    _git(cleanup_repo, "worktree", "unlock", str(path))
    _git(cleanup_repo, "worktree", "lock", "--reason", "operator hold", str(path))
    with pytest.raises(SystemExit) as exc:
        clean(monkeypatch)
    assert exc.value.code == 1 and path.is_dir()
    assert "locked operator hold" in _git(cleanup_repo, "worktree", "list", "--porcelain")


def test_clean_removes_published_worktree_but_retains_unmerged_branch(
    cleanup_repo: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager, _, _ = make_manager(cleanup_repo, tmp_path)
    path, _ = manager.ensure("bob")
    branch = "robomate/repo/bob/published"
    _git(path, "switch", "-c", branch)
    _git(
        path,
        "-c",
        "user.name=T",
        "-c",
        "user.email=t@example.com",
        "commit",
        "--allow-empty",
        "-m",
        "published",
    )
    _git(path, "update-ref", f"refs/remotes/origin/{branch}", "HEAD")
    clean(monkeypatch)
    assert not path.exists()
    assert branch in _git(cleanup_repo, "branch", "--format=%(refname:short)").splitlines()


def test_orphan_with_uncommitted_work_is_reported_and_kept(
    cleanup_repo: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    manager, _, _ = make_manager(cleanup_repo, tmp_path)
    path, _ = manager.ensure("bob")
    (path / "untracked.txt").write_text("work", encoding="utf-8")
    shutil.rmtree(manager.state_dir)
    with pytest.raises(SystemExit) as exc:
        clean(monkeypatch)
    assert exc.value.code == 1 and path.is_dir()
    assert not manager.state_dir.exists()
    assert "Orphaned agent worktree" in capsys.readouterr().out


def test_failed_git_removal_restores_the_hub_lock(
    cleanup_repo: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import robomate.clean as cleanup

    manager, _, _ = make_manager(cleanup_repo, tmp_path)
    path, _ = manager.ensure("bob")
    original = cleanup.git

    def refuse_remove(repo: cleanup.Repository, *args: str, cwd: Path | None = None) -> str:
        if args[:2] == ("worktree", "remove"):
            raise RuntimeError("removal refused")
        return original(repo, *args, cwd=cwd)

    monkeypatch.setattr(cleanup, "git", refuse_remove)
    with pytest.raises(SystemExit) as exc:
        clean(monkeypatch)
    assert exc.value.code == 1 and path.is_dir()
    assert "locked robomate hub repo" in _git(cleanup_repo, "worktree", "list", "--porcelain")
