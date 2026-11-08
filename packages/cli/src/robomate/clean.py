"""Operator cleanup of one hub's bound agent worktrees (spec §6)."""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path

from agent_hub_common.discovery import (
    Binding,
    DiscoveryError,
    LocalHub,
    Repository,
    checkout_git_dir,
    find_hub,
    read_binding,
    resolve_repository,
)


def git(repo: Repository, *args: str, cwd: Path | None = None) -> str:
    """Run a bounded local git command, preserving porcelain separators."""

    command = (
        ["git", "-C", str(cwd), *args]
        if cwd
        else ["git", f"--git-dir={repo.git_common_dir}", *args]
    )
    try:
        result = subprocess.run(
            command, capture_output=True, text=True, stdin=subprocess.DEVNULL, timeout=30
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RuntimeError(f"git {' '.join(args)}: {exc}") from exc
    if result.returncode:
        raise RuntimeError(f"git {' '.join(args)}: {result.stderr.strip()}")
    return result.stdout


@dataclass(frozen=True)
class AgentWorktree:
    path: Path
    binding: Binding
    lock_reason: str | None


def agent_worktrees(repo: Repository) -> list[AgentWorktree]:
    """Discover linked worktrees through git and their own binding records."""

    result = []
    for record in git(repo, "worktree", "list", "--porcelain", "-z").split("\0\0"):
        fields = record.split("\0")
        if not fields[0].startswith("worktree "):
            continue
        path = Path(fields[0].removeprefix("worktree "))
        # A missing checkout cannot be inspected for unpublished work. Leave its
        # registration alone rather than using prune/force to bypass git's guard.
        if not path.is_dir() or "bare" in fields:
            continue
        git_dir = checkout_git_dir(path)
        # A full-clone fallback may also have an agent binding; it is never
        # a generated linked worktree and must not be removed by clean.
        if git_dir.resolve() == repo.git_common_dir.resolve():
            continue
        binding = read_binding(git_dir)
        if binding is None or binding.role != "agent":
            continue
        reason = next(
            (
                f.removeprefix("locked").lstrip()
                for f in fields
                if f == "locked" or f.startswith("locked ")
            ),
            None,
        )
        result.append(AgentWorktree(path, binding, reason))
    return result


def cleanup_target(cwd: Path, selector: str | None) -> tuple[Repository, LocalHub]:
    """Resolve locally, including a selected hub whose state was removed by hand."""

    try:
        hub = find_hub(cwd, selector=selector)
    except DiscoveryError:
        repo = resolve_repository(cwd)
        bound = read_binding(checkout_git_dir(repo.root))
        candidates = [item.binding for item in agent_worktrees(repo)]
        if bound is not None:
            candidates.append(bound)
        if selector:
            selected_path = Path(selector).expanduser().resolve()
            candidates = [
                b
                for b in candidates
                if selector in (b.name, b.hub_id) or b.checkout.resolve() == selected_path
            ]
        else:
            candidates = [b for b in candidates if bound is not None and b.hub_id == bound.hub_id]
        if not candidates or len({b.hub_id for b in candidates}) != 1:
            raise
        binding = candidates[0]
        if binding.state_dir.exists():
            raise
        hub = LocalHub(
            binding.checkout,
            {"hub_id": binding.hub_id, "name": binding.name},
            binding.state_dir,
            binding.token_file,
        )
        return repo, hub
    if not isinstance(hub, LocalHub):
        raise DiscoveryError("robomate clean requires a local hub; select it with --hub")
    return resolve_repository(hub.checkout), hub


def clean_worktrees(repo: Repository, hub: LocalHub) -> None:
    """Remove safe worktrees and ancestry-merged branches; never force deletion.

    Unpublished commits include detached HEADs. Remote refs must already have
    been fetched by the operator; stale/missing refs conservatively keep work.
    """

    failures = []
    for item in agent_worktrees(repo):
        if item.binding.hub_id != hub.hub_id:
            continue
        path = item.path
        orphan = not item.binding.state_dir.exists()
        if orphan:
            print(f"Orphaned agent worktree: {path} (hub {item.binding.name})")
        try:
            if path.resolve() == Path.cwd().resolve() or Path.cwd().resolve().is_relative_to(
                path.resolve()
            ):
                raise RuntimeError("cannot remove the current working directory")
            if any(
                place.resolve().is_relative_to(path.resolve())
                for place in (hub.state_dir, item.binding.state_dir, item.binding.token_file)
            ):
                raise RuntimeError("worktree contains hub state or credentials")
            if git(repo, "status", "--porcelain", "--untracked-files=all", cwd=path).strip():
                raise RuntimeError("uncommitted changes")
            if git(repo, "rev-list", "HEAD", "--not", "--remotes", cwd=path).strip():
                raise RuntimeError("unpushed commits (not reachable from a remote ref)")
            if item.lock_reason is not None:
                if item.lock_reason != f"robomate hub {item.binding.name}":
                    raise RuntimeError(f"worktree has another lock: {item.lock_reason}")
                git(repo, "worktree", "unlock", str(path))
            try:
                git(repo, "worktree", "remove", str(path))
            except RuntimeError:
                if item.lock_reason is not None:
                    git(repo, "worktree", "lock", "--reason", item.lock_reason, str(path))
                raise
            print(f"Removed agent worktree: {path}")
        except RuntimeError as exc:
            failures.append(f"{path}: {exc}")
            print(f"Refused {path}: {exc}")
    # --merged is deliberately ancestry-based: uncertain squash/rebase merges
    # are retained. Never infer integration merely from a missing remote ref.
    if hub.name:
        prefix = f"robomate/{hub.name}/"
        branches = git(
            repo,
            "for-each-ref",
            f"--merged=origin/{repo.default_branch}",
            "--format=%(refname:short)",
            f"refs/heads/{prefix}",
        ).splitlines()
        for branch in branches:
            if not branch.startswith(prefix):
                continue
            try:
                git(repo, "branch", "-d", "--", branch)
                print(f"Pruned merged branch: {branch}")
            except RuntimeError as exc:
                failures.append(f"{branch}: {exc}")
                print(f"Refused branch {branch}: {exc}")
    if failures:
        raise RuntimeError("cleanup incomplete; refused work was kept: " + "; ".join(failures))
