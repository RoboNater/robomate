"""Operator cleanup exercises real git worktrees in disposable repositories."""

import json
import os
import shutil
import subprocess
import sys
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
import robomate.clean as cleanup
import robomate.cli as cli
from agent_hub_common.discovery import (
    Binding,
    Repository,
    checkout_git_dir,
    resolve_repository,
    write_binding,
)
from agent_hub_common.registry import register
from test_worktrees import _git, make_manager, make_repo

MERGED_ON_FORGE = cleanup.merged_on_forge


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
    # This fixture's example origin is deliberately not a real forge project.
    # Tests for the CLI's forge adapter below supply explicit JSON responses.
    monkeypatch.setattr(cleanup, "merged_on_forge", lambda repo, branch, head: False)
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

    def refuse_remove(repo: Repository, *args: str, cwd: Path | None = None) -> str:
        if args[:2] == ("worktree", "remove"):
            raise RuntimeError("removal refused")
        return original(repo, *args, cwd=cwd)

    monkeypatch.setattr(cleanup, "git", refuse_remove)
    with pytest.raises(SystemExit) as exc:
        clean(monkeypatch)
    assert exc.value.code == 1 and path.is_dir()
    assert "locked robomate hub repo" in _git(cleanup_repo, "worktree", "list", "--porcelain")


@pytest.mark.parametrize("selector", ["repo", "hub-repo", "checkout"])
def test_orphan_can_be_selected_from_outside_a_checkout(
    cleanup_repo: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    selector: str,
) -> None:
    manager, _, common = make_manager(cleanup_repo, tmp_path)
    path, _ = manager.ensure("bob")
    register(
        {
            "hub_id": manager.hub_id,
            "name": "repo",
            "checkout": str(cleanup_repo),
            "git_common_dir": str(common),
            "state_dir": str(manager.state_dir),
            "pid": None,
        }
    )
    shutil.rmtree(manager.state_dir)
    monkeypatch.chdir(tmp_path)
    clean(monkeypatch, "--hub", str(cleanup_repo) if selector == "checkout" else selector)
    assert not path.exists() and not manager.state_dir.exists()


def test_orphan_with_live_registered_process_is_refused(
    cleanup_repo: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager, _, common = make_manager(cleanup_repo, tmp_path)
    path, _ = manager.ensure("bob")
    register(
        {
            "hub_id": manager.hub_id,
            "name": "repo",
            "checkout": str(cleanup_repo),
            "git_common_dir": str(common),
            "state_dir": str(manager.state_dir),
            "pid": os.getpid(),
        }
    )
    shutil.rmtree(manager.state_dir)
    with pytest.raises(SystemExit) as exc:
        clean(monkeypatch)
    assert exc.value.code == 1 and path.is_dir()


def test_clean_never_chooses_between_same_named_orphans(
    cleanup_repo: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager, _, common = make_manager(cleanup_repo, tmp_path)
    path, _ = manager.ensure("bob")
    # The registry can have the same name in different repositories. Even if
    # only this checkout's binding can be read, --hub must not silently pick it.
    for hub_id, git_common in ((manager.hub_id, common), ("other-id", tmp_path / "elsewhere")):
        register(
            {
                "hub_id": hub_id,
                "name": "repo",
                "checkout": str(cleanup_repo),
                "git_common_dir": str(git_common),
                "state_dir": str(manager.state_dir),
                "pid": None,
            }
        )
    shutil.rmtree(manager.state_dir)
    with pytest.raises(SystemExit) as exc:
        clean(monkeypatch, "--hub", "repo")
    assert exc.value.code == 1 and path.is_dir()


@pytest.mark.parametrize("detached", [False, True])
def test_squash_merged_exact_head_is_removed_even_without_remote_tracking_ref(
    cleanup_repo: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    detached: bool,
) -> None:
    manager, _, _ = make_manager(cleanup_repo, tmp_path)
    path, _ = manager.ensure("bob")
    branch = "robomate/repo/bob/squashed"
    _git(path, "switch", "--no-track", "-c", branch)
    (path / "feature").write_text("feature", encoding="utf-8")
    _git(path, "add", "feature")
    _git(path, "-c", "user.name=T", "-c", "user.email=t@example.com", "commit", "-m", "feature")
    head = _git(path, "rev-parse", "HEAD")
    _git(cleanup_repo, "merge", "--squash", branch)
    _git(
        cleanup_repo,
        "-c",
        "user.name=T",
        "-c",
        "user.email=t@example.com",
        "commit",
        "-m",
        "squash",
    )
    _git(cleanup_repo, "update-ref", "refs/remotes/origin/main", "HEAD")
    if detached:
        _git(path, "switch", "--detach", head)
    monkeypatch.setattr(
        cleanup, "merged_on_forge", lambda repo, candidate, tip: candidate == branch and tip == head
    )
    clean(monkeypatch)
    assert not path.exists()
    assert branch not in _git(cleanup_repo, "branch", "--format=%(refname:short)").splitlines()


def test_commit_beyond_merged_pr_head_is_unpublished_and_kept(
    cleanup_repo: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager, _, _ = make_manager(cleanup_repo, tmp_path)
    path, _ = manager.ensure("bob")
    branch = "robomate/repo/bob/newer"
    _git(path, "switch", "--no-track", "-c", branch)
    merged_head = _git(path, "rev-parse", "HEAD")
    _git(
        path,
        "-c",
        "user.name=T",
        "-c",
        "user.email=t@example.com",
        "commit",
        "--allow-empty",
        "-m",
        "new unpublished work",
    )
    monkeypatch.setattr(
        cleanup,
        "merged_on_forge",
        lambda repo, candidate, tip: candidate == branch and tip == merged_head,
    )
    with pytest.raises(SystemExit) as exc:
        clean(monkeypatch)
    assert exc.value.code == 1 and path.is_dir()
    assert branch in _git(cleanup_repo, "branch", "--format=%(refname:short)").splitlines()


@pytest.mark.parametrize("forge", ["github", "gitlab"])
@pytest.mark.parametrize(
    "case",
    [
        "exact",
        "head",
        "branch",
        "base",
        "state",
        "ambiguous",
        "missing-merge",
        "unfetched-merge",
        "invalid-json",
        "cli-fails",
    ],
)
def test_forge_proof_requires_exact_branch_head_base_and_reachable_merge(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    forge: str,
    case: str,
) -> None:
    root = make_repo(tmp_path)
    repo = replace(resolve_repository(root), forge=forge)
    if forge == "gitlab":
        repo = replace(repo, origin="git@gitlab.example:team/repo.git")
    branch = "robomate/repo/bob/squashed"
    head = "a" * 40
    merge_sha = _git(root, "rev-parse", "HEAD")
    if forge == "github":
        row: dict[str, Any] = {
            "state": "MERGED",
            "headRefName": branch,
            "headRefOid": head,
            "baseRefName": "main",
            "mergeCommit": {"oid": merge_sha},
        }
        keys = {
            "head": "headRefOid",
            "branch": "headRefName",
            "base": "baseRefName",
            "state": "state",
        }
        merge_key = "mergeCommit"
    else:
        row = {
            "state": "merged",
            "source_branch": branch,
            "sha": head,
            "target_branch": "main",
            "merge_commit_sha": merge_sha,
        }
        keys = {"head": "sha", "branch": "source_branch", "base": "target_branch", "state": "state"}
        merge_key = "merge_commit_sha"
    if case in keys:
        row[keys[case]] = "wrong"
    if case == "missing-merge":
        row.pop(merge_key)
    if case == "unfetched-merge":
        row[merge_key] = {"oid": "b" * 40} if forge == "github" else "b" * 40
    payload = json.dumps([row, row] if case == "ambiguous" else [row])
    if case == "invalid-json":
        payload = "not JSON"
    run = subprocess.run

    def fake_run(command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        if command[0] not in ("gh", "glab"):
            return run(command, **kwargs)
        if forge == "github":
            assert command[:3] == ["gh", "pr", "list"]
            assert "https://github.com/example/repo" in command
            assert command[command.index("--head") + 1] == branch
        else:
            assert command[:4] == ["glab", "api", "--hostname", "gitlab.example"]
            assert "projects/team%2Frepo/merge_requests?" in command[4]
            assert "source_branch=robomate%2Frepo%2Fbob%2Fsquashed" in command[4]
        return subprocess.CompletedProcess(
            command, 1 if case == "cli-fails" else 0, stdout=payload, stderr=""
        )

    monkeypatch.setattr(subprocess, "run", fake_run)
    assert MERGED_ON_FORGE(repo, branch, head) is (case == "exact")


def test_branch_pruning_uses_full_refs_even_when_a_tag_has_the_same_name(
    cleanup_repo: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    branch = "robomate/repo/bob/merged"
    unrelated = f"heads/{branch}"
    _git(cleanup_repo, "branch", branch)
    _git(cleanup_repo, "branch", unrelated)
    _git(cleanup_repo, "tag", branch)
    clean(monkeypatch)
    refs = _git(cleanup_repo, "for-each-ref", "--format=%(refname)").splitlines()
    assert f"refs/heads/{branch}" not in refs
    assert f"refs/heads/{unrelated}" in refs
    assert f"refs/tags/{branch}" in refs


def test_branch_advance_after_merge_proof_is_never_deleted(
    cleanup_repo: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    branch = "robomate/repo/bob/merged"
    _git(cleanup_repo, "branch", branch)
    tree = _git(cleanup_repo, "rev-parse", "HEAD^{tree}")
    newer = _git(
        cleanup_repo,
        "-c",
        "user.name=T",
        "-c",
        "user.email=t@example.com",
        "commit-tree",
        tree,
        "-p",
        "HEAD",
        "-m",
        "new unpushed work",
    )
    original = cleanup.git

    def advance_before_delete(repo: Repository, *args: str, cwd: Path | None = None) -> str:
        if args[:2] == ("update-ref", "-d"):
            _git(cleanup_repo, "update-ref", f"refs/heads/{branch}", newer)
        return original(repo, *args, cwd=cwd)

    monkeypatch.setattr(cleanup, "git", advance_before_delete)
    with pytest.raises(SystemExit) as exc:
        clean(monkeypatch)
    assert exc.value.code == 1
    assert _git(cleanup_repo, "rev-parse", f"refs/heads/{branch}") == newer
