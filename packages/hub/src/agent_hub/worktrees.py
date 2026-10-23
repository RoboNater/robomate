"""Generate agent worktrees at join (M2 Step 7a, #244; mvp-spec §6).

The hub owns worktree creation: at join it runs
`git worktree add --detach <root>/<hub>/<agent>/ origin/<default>`, writes the
agent binding into the worktree's own git directory, and locks the worktree
with the hub's reason. A join that finds its worktree already there reuses it.
The orchestrator gets one the same way. An explicit `HUB_WORKSPACE` creates no
worktree; that path stays a full clone (Step 9 keeps the identity file).
"""

from __future__ import annotations

import secrets
import subprocess
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from agent_hub_common.discovery import (
    Binding,
    DiscoveryError,
    check_worktree_root,
    checkout_git_dir,
    claim_worktree_root,
    read_binding,
    validate_agent_name,
    write_binding,
)


class WorktreeError(RuntimeError):
    """A worktree cannot be created, reused, or locked for this join."""


def worktree_lock_reason(hub_name: str) -> str:
    """The `git worktree lock --reason` the hub holds its agent worktrees with."""

    return f"robomate hub {hub_name}"


def agent_worktree_path(worktree_root: Path, hub_name: str, agent_name: str) -> Path:
    """Where an agent's worktree lives: `<root>/<hub>/<agent>/` (spec §6)."""

    return worktree_root / hub_name / agent_name


def _git(common: Path, *args: str, cwd: Path | None = None) -> str:
    try:
        completed = subprocess.run(
            ["git", f"--git-dir={common}", *args],
            cwd=cwd or Path.cwd(),
            stdin=subprocess.DEVNULL,
            capture_output=True,
            timeout=30,
        )
    except subprocess.TimeoutExpired as exc:
        raise WorktreeError(f"git {' '.join(args)} timed out; check the repository") from exc
    except OSError as exc:
        raise WorktreeError(f"cannot run git {' '.join(args)}: {exc}") from exc
    if completed.returncode != 0:
        detail = completed.stderr.decode("utf-8", errors="replace").strip()
        raise WorktreeError(
            f"git {' '.join(args)} failed: {detail or f'exit {completed.returncode}'}"
        )
    try:
        return completed.stdout.decode("utf-8").strip()
    except UnicodeDecodeError as exc:
        raise WorktreeError(f"cannot decode git {' '.join(args)} output: {exc}") from exc


def _worktree_paths(common: Path) -> dict[str, dict[str, str]]:
    """Parse `git worktree list --porcelain` into {path: {branch, detached, locked}}."""

    listing = _git(common, "worktree", "list", "--porcelain")
    entries: dict[str, dict[str, str]] = {}
    current: dict[str, str] | None = None
    current_path: str | None = None
    for line in listing.splitlines():
        if line.startswith("worktree "):
            if current_path is not None and current is not None:
                entries[current_path] = current
            current_path = line.removeprefix("worktree ").strip()
            current = {}
        elif current is not None and current_path is not None:
            if line.startswith("branch "):
                current["branch"] = line.removeprefix("branch ").strip()
            elif line == "detached":
                current["detached"] = "true"
            elif line.startswith("locked"):
                current["locked"] = line.removeprefix("locked").strip()
    if current_path is not None and current is not None:
        entries[current_path] = current
    return entries


def is_locked_worktree(common: Path, path: Path) -> tuple[bool, str]:
    """Whether `path` is a locked worktree of this repository, and its reason."""

    try:
        entries = _worktree_paths(common)
    except WorktreeError:
        return False, ""
    info = entries.get(str(path))
    if info is None:
        # Resolve symlinks/case: compare resolved paths.
        resolved = str(path.resolve()) if path.exists() else str(path)
        for candidate, details in entries.items():
            try:
                if str(Path(candidate).resolve()) == resolved:
                    info = details
                    break
            except OSError:
                continue
    if info is None:
        return False, ""
    reason = info.get("locked", "")
    return ("locked" in info, reason)


class WorktreeManager:
    """Create and reuse `<root>/<hub>/<agent>/` worktrees for one hub."""

    def __init__(
        self,
        *,
        hub_id: str,
        hub_name: str,
        checkout: Path,
        git_common_dir: Path,
        default_branch: str,
        worktree_root: Path,
        state_dir: Path,
        token_file: Path,
    ) -> None:
        self.hub_id = hub_id
        self.hub_name = hub_name
        self.checkout = checkout
        self.common = git_common_dir
        self.default_branch = default_branch
        self.root = worktree_root
        self.state_dir = state_dir
        self.token_file = token_file

    @classmethod
    def from_hub_info(
        cls,
        hub_info: Mapping[str, Any] | None,
        *,
        state_dir: Path,
        token_file: Path,
        worktree_root: Path | None,
    ) -> WorktreeManager | None:
        """Build a manager when the hub knows its repository, else None.

        Test hubs created without `hub_info` (no git context) get None and keep
        the pre-worktree behaviour: the bridge's path is echoed with no
        worktree created.
        """

        if hub_info is None or worktree_root is None:
            return None
        try:
            hub_id = str(hub_info.get("hub_id") or "")
            hub_name = str(hub_info.get("name") or hub_info.get("hub_name") or "")
            checkout_raw = hub_info.get("checkout") or hub_info.get("repo_root")
            common_raw = hub_info.get("git_common_dir")
            default_branch = str(hub_info.get("default_branch") or "")
            if not hub_id or not hub_name or not checkout_raw or not common_raw:
                return None
            if not default_branch:
                return None
            return cls(
                hub_id=hub_id,
                hub_name=hub_name,
                checkout=Path(str(checkout_raw)),
                git_common_dir=Path(str(common_raw)),
                default_branch=default_branch,
                worktree_root=worktree_root,
                state_dir=state_dir,
                token_file=token_file,
            )
        except (TypeError, ValueError):
            return None

    def ensure(self, agent_name: str) -> tuple[Path, str]:
        """Create or reuse an agent's worktree; return (path, workspace_id).

        Writes the agent binding into the worktree's own git directory and
        locks the worktree with the hub's reason. Raises `WorktreeError` when
        the name is reserved, the root belongs to another repository, or git
        refuses.
        """

        try:
            validate_agent_name(agent_name)
        except ValueError as exc:
            raise WorktreeError(str(exc)) from exc
        try:
            check_worktree_root(self.root, self.common)
        except DiscoveryError as exc:
            raise WorktreeError(str(exc)) from exc
        # The root records its repository on first creation; a missing or empty
        # root is claimed here so a join never runs against an unowned one.
        try:
            claim_worktree_root(self.root, self.common)
        except DiscoveryError as exc:
            raise WorktreeError(str(exc)) from exc
        path = agent_worktree_path(self.root, self.hub_name, agent_name)
        if path.exists():
            return self._reuse(path, agent_name)
        return self._create(path, agent_name)

    def _reuse(self, path: Path, agent_name: str) -> tuple[Path, str]:
        try:
            git_dir = checkout_git_dir(path)
        except DiscoveryError as exc:
            raise WorktreeError(
                f"agent path {path} exists but is not a worktree of this repository; "
                "move it away before joining"
            ) from exc
        binding = read_binding(git_dir)
        if (
            binding is None
            or binding.role != "agent"
            or binding.hub_id != self.hub_id
            or binding.agent != agent_name
        ):
            raise WorktreeError(
                f"agent path {path} exists but is not bound to hub {self.hub_name} "
                f"as agent {agent_name!r}; move it away before joining"
            )
        workspace_id = binding.workspace_id
        if workspace_id is None:
            # Bindings written by hand in Step 6 tests carry no workspace_id;
            # mint one now so the reused worktree still has an identity.
            workspace_id = secrets.token_hex(32)
            write_binding(
                git_dir,
                Binding(
                    "agent",
                    self.hub_id,
                    self.hub_name,
                    self.checkout,
                    self.state_dir,
                    self.token_file,
                    agent_name,
                    workspace_id,
                ),
            )
        self._lock(path)
        return path, workspace_id

    def _create(self, path: Path, agent_name: str) -> tuple[Path, str]:
        start = f"origin/{self.default_branch}"
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            _git(self.common, "worktree", "add", "--detach", str(path), start)
        except WorktreeError as exc:
            raise WorktreeError(
                f"cannot create worktree {path} at {start}: {exc}; "
                "the hub needs the default branch fetched as origin/<default>"
            ) from exc
        try:
            git_dir = checkout_git_dir(path)
        except DiscoveryError as exc:
            raise WorktreeError(f"created {path} but cannot find its git directory: {exc}") from exc
        workspace_id = secrets.token_hex(32)
        write_binding(
            git_dir,
            Binding(
                "agent",
                self.hub_id,
                self.hub_name,
                self.checkout,
                self.state_dir,
                self.token_file,
                agent_name,
                workspace_id,
            ),
        )
        self._lock(path)
        return path, workspace_id

    def _lock(self, path: Path) -> None:
        locked, reason = is_locked_worktree(self.common, path)
        if locked and reason == worktree_lock_reason(self.hub_name):
            return
        try:
            _git(
                self.common,
                "worktree",
                "lock",
                "--reason",
                worktree_lock_reason(self.hub_name),
                str(path),
            )
        except WorktreeError as exc:
            raise WorktreeError(f"cannot lock worktree {path}: {exc}") from exc
