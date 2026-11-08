"""Operator cleanup of one hub's bound agent worktrees (spec §6)."""

from __future__ import annotations

import json
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote

from agent_hub.gitlab_gate import GitLabProject
from agent_hub.merge_gate import GitHubProject
from agent_hub_common.discovery import (
    Binding,
    DiscoveryError,
    LocalHub,
    Repository,
    checkout_git_dir,
    find_hub,
    hub_checkout,
    read_binding,
    resolve_repository,
)
from agent_hub_common.registry import recorded_entries

_SHA = re.compile(r"[0-9a-f]{40}")


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


def merged_on_forge(repo: Repository, branch: str, head: str) -> bool:
    """Prove an exact branch/head was merged into the fetched default branch.

    Query only the repository's configured origin. An absent CLI, failed read,
    unexpected JSON or ambiguous match never authorizes deletion.
    """

    try:
        if repo.forge == "github":
            project = GitHubProject.from_origin(repo.origin)
            command = [
                "gh",
                "pr",
                "list",
                "--repo",
                project.web_url,
                "--state",
                "merged",
                "--head",
                branch,
                "--base",
                repo.default_branch,
                "--limit",
                "100",
                "--json",
                "state,headRefName,headRefOid,baseRefName,mergeCommit",
            ]
        elif repo.forge == "gitlab":
            gitlab = GitLabProject.from_origin(repo.origin)
            command = [
                "glab",
                "api",
                "--hostname",
                gitlab.host,
                f"projects/{quote(gitlab.path, safe='')}/merge_requests?state=merged"
                f"&scope=all&source_branch={quote(branch, safe='')}"
                f"&target_branch={quote(repo.default_branch, safe='')}&per_page=100",
            ]
        else:
            return False
        result = subprocess.run(
            command,
            cwd=repo.root,
            capture_output=True,
            text=True,
            stdin=subprocess.DEVNULL,
            timeout=30,
        )
        if result.returncode:
            return False
        data = json.loads(result.stdout)
        if not isinstance(data, list):
            return False
        matches = []
        for row in data:
            if not isinstance(row, dict):
                return False
            if repo.forge == "github":
                merge = row.get("mergeCommit")
                merge_sha = merge.get("oid") if isinstance(merge, dict) else None
                exact = (
                    row.get("state") == "MERGED"
                    and row.get("headRefName") == branch
                    and row.get("headRefOid") == head
                    and row.get("baseRefName") == repo.default_branch
                )
            else:
                merge_sha = row.get("merge_commit_sha") or row.get("squash_commit_sha")
                exact = (
                    row.get("state") == "merged"
                    and row.get("source_branch") == branch
                    and row.get("sha") == head
                    and row.get("target_branch") == repo.default_branch
                )
            if exact and isinstance(merge_sha, str) and _SHA.fullmatch(merge_sha):
                matches.append(merge_sha)
        if len(matches) != 1:
            return False
        # The forge's merge must still be present in the locally fetched base;
        # a force-reset default branch or stale fetch cannot discard work.
        git(repo, "merge-base", "--is-ancestor", matches[0], f"origin/{repo.default_branch}")
        return True
    except (OSError, RuntimeError, ValueError, subprocess.TimeoutExpired):
        return False


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

    # Discovery can prune registry entries whose state vanished. Retain their
    # repository/identity information long enough to locate the orphan bindings.
    entries = recorded_entries()
    try:
        hub = find_hub(cwd, selector=selector)
    except DiscoveryError:
        matches = (
            (
                [e for e in entries if e.get("hub_id") == selector]
                or [e for e in entries if e.get("name") == selector]
            )
            if selector
            else []
        )
        # Orphan recovery must not resolve an ambiguous registry selection
        # by silently choosing the repository containing the caller.
        if len(matches) > 1:
            raise
        selected_checkout = hub_checkout(matches[0]) if matches else None
        if selected_checkout is None and selector and any(c in selector for c in "/\\.~:"):
            selected_checkout = Path(selector).expanduser()
        repo = resolve_repository(selected_checkout or cwd)
        bound = read_binding(checkout_git_dir(repo.root))
        candidates = [item.binding for item in agent_worktrees(repo)]
        if bound is not None:
            candidates.append(bound)
        if selector:
            selected_path = Path(selector).expanduser().resolve()
            candidates = [
                b
                for b in candidates
                if (matches and b.hub_id == matches[0].get("hub_id"))
                or (
                    not matches
                    and (selector in (b.name, b.hub_id) or b.checkout.resolve() == selected_path)
                )
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
    """Remove safe worktrees and proven merged branches; never force worktree removal.

    Unpublished commits include detached HEADs. Remote refs must already have
    been fetched by the operator; stale/missing refs conservatively keep work.
    """

    failures = []
    prefix = f"robomate/{hub.name}/" if hub.name else ""
    branches = (
        dict(
            line.removeprefix("refs/heads/").split(" ", 1)
            for line in git(
                repo,
                "for-each-ref",
                "--format=%(refname) %(objectname)",
                f"refs/heads/{prefix}",
            ).splitlines()
        )
        if prefix
        else {}
    )
    ancestry_merged = (
        {
            ref.removeprefix("refs/heads/")
            for ref in git(
                repo,
                "for-each-ref",
                f"--merged=origin/{repo.default_branch}",
                "--format=%(refname)",
                f"refs/heads/{prefix}",
            ).splitlines()
        }
        if prefix
        else set()
    )
    proven = {
        branch: head
        for branch, head in branches.items()
        if branch in ancestry_merged or merged_on_forge(repo, branch, head)
    }
    for branch in branches.keys() - proven.keys():
        print(f"Retained branch {branch}: no exact merge proof (unmerged or forge unavailable)")
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
            head = git(repo, "rev-parse", "HEAD", cwd=path).strip()
            if (
                head not in proven.values()
                and git(repo, "rev-list", "HEAD", "--not", "--remotes", cwd=path).strip()
            ):
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
    checked_out = set(
        field.removeprefix("branch refs/heads/")
        for field in git(repo, "worktree", "list", "--porcelain", "-z").split("\0")
        if field.startswith("branch refs/heads/")
    )
    for branch, head in proven.items():
        try:
            if branch in checked_out:
                raise RuntimeError("branch is still checked out in a worktree")
            # A compare-and-delete protects against ref changes after the proof.
            # Unlike branch -D, this never deletes an unverified newer tip.
            git(repo, "update-ref", "-d", f"refs/heads/{branch}", head)
            print(f"Pruned merged branch: {branch}")
        except RuntimeError as exc:
            failures.append(f"{branch}: {exc}")
            print(f"Refused branch {branch}: {exc}")
    if failures:
        raise RuntimeError("cleanup incomplete; refused work was kept: " + "; ".join(failures))
