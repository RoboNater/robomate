"""Move a stopped hub's state and token into its worktree root (spec §4 "Migration").

`robomate relocate` calls this under the hub's `up.lock`, so the hub is not
running. The move is copy, switch, delete: the copy is made and checked while
the old location is still authoritative, the bindings and the registry entry
are rewritten next, and only then are the old files removed. A failure before
the switch leaves the old location in place and removes the copy.
"""

from __future__ import annotations

import os
import shutil
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass, replace
from pathlib import Path

from .discovery import (
    BINDING_FILENAME,
    Binding,
    DiscoveryError,
    HubPlacement,
    LocalHub,
    check_worktree_root,
    checkout_git_dir,
    create_hub_dirs,
    hub_bindings,
    hub_paths,
    namespace_free,
    read_binding,
    write_binding,
)
from .registry import update_location

# Left in an emptied state directory instead of the files that moved.
POINTER_FILENAME = "relocated"
# The lock `robomate up` holds is the caller's, and open: it is recreated, not moved.
_NOT_MOVED = frozenset({"up.lock"})


@dataclass(frozen=True)
class Relocation:
    hub_id: str
    name: str
    old_state_dir: Path
    old_token_file: Path
    state_dir: Path
    token_file: Path
    bindings: tuple[Path, ...]
    """The git directories whose binding now names the new location."""


def relocation_targets(hub: LocalHub, worktree_root: Path) -> tuple[str, Path, Path]:
    """The hub's name and where `relocate` would put its state and token."""

    if not hub.name:
        raise DiscoveryError(
            f"{hub.label()} has no name yet; run robomate up in {hub.checkout} once, "
            "which records it, then relocate"
        )
    state, token = hub_paths(worktree_root, hub.name)
    return hub.name, state, token


def _refuse_hub_forge(state_dir: Path, repository_file: Path) -> None:
    """A relocated hub's config may not hold [forge] (spec §4), and nothing is rewritten."""

    config = state_dir / "config.toml"
    try:
        table = tomllib.loads(config.read_text(encoding="utf-8")).get("forge")
    except FileNotFoundError:
        return
    except (OSError, ValueError) as exc:
        raise DiscoveryError(f"cannot read {config}: {exc}") from exc
    if table is not None:
        raise DiscoveryError(
            f"{config} holds a [forge] table, which only a legacy hub's config may keep. "
            f"Move it to {repository_file} (the repository-level file), then relocate"
        )


def _copy(source: Path, destination: Path) -> None:
    if source.is_dir():
        shutil.copytree(source, destination, dirs_exist_ok=True)
        for path in source.rglob("*"):
            if path.is_file() and (destination / path.relative_to(source)).stat().st_size != (
                path.stat().st_size
            ):
                raise OSError(f"the copy of {path} is not the same size")
    else:
        shutil.copy2(source, destination)
        if destination.stat().st_size != source.stat().st_size:
            raise OSError(f"the copy of {source} is not the same size")


def _copy_token(source: Path, destination: Path) -> None:
    descriptor = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "wb") as stream:
        stream.write(source.read_bytes())
    os.chmod(destination, 0o600)


def relocate_hub(
    hub: LocalHub,
    git_common_dir: Path,
    worktree_root: Path,
    repository_file: Path,
    environ: Mapping[str, str] | None = None,
) -> Relocation:
    """Move a stopped hub's state and token to `<worktree_root>/<hub>/` and rebind everything.

    The same `hub_id` serves from the new location: the owner binding and every
    agent binding of the hub, in any checkout of the repository, and the
    registry entry now name it, and the old token file is gone. The old state
    directory keeps a one-line pointer file in place of what moved.
    """

    name, state, token = relocation_targets(hub, worktree_root)
    old_state, old_token = hub.state_dir, hub.token_file
    if state == old_state:
        raise DiscoveryError(f"{hub.label()} already keeps its state in {state}")
    _refuse_hub_forge(old_state, repository_file)
    check_worktree_root(worktree_root, git_common_dir)
    namespace = worktree_root / name
    if not namespace_free(namespace):
        raise DiscoveryError(
            f"cannot relocate {hub.label()}: {namespace} already exists under the worktree root"
        )
    created = not namespace.exists()
    placement = HubPlacement(state, token, True, name, worktree_root, git_common_dir)
    try:
        create_hub_dirs(placement)
        moved = [
            path
            for path in sorted(old_state.iterdir())
            if path.name not in _NOT_MOVED and path != old_token and path.name != POINTER_FILENAME
        ]
        for path in moved:
            _copy(path, state / path.name)
        _copy_token(old_token, token)
    except BaseException:
        if created:
            shutil.rmtree(namespace, ignore_errors=True)
        raise

    # The switch: the bindings, then the registry entry. A failure part-way
    # puts the earlier bindings back and removes the copy; the old files were
    # never touched.
    written: list[tuple[Path, Binding | None]] = []
    try:
        found = hub_bindings(git_common_dir, hub.hub_id)
        owner_git_dir = None
        if hub.checkout.is_dir():
            try:
                owner_git_dir = checkout_git_dir(hub.checkout)
            except DiscoveryError:
                owner_git_dir = None
        if owner_git_dir is not None and all(g != owner_git_dir for g, _ in found):
            found.append(
                (owner_git_dir, Binding("owner", hub.hub_id, name, hub.checkout, state, token))
            )
        for git_dir, binding in found:
            written.append((git_dir, read_binding(git_dir)))
            write_binding(git_dir, replace(binding, name=name, state_dir=state, token_file=token))
        update_location(hub.hub_id, state, token, environ)
    except BaseException:
        for git_dir, previous in written:
            if previous is not None:
                write_binding(git_dir, previous)
            else:
                (git_dir / BINDING_FILENAME).unlink(missing_ok=True)
        if created:
            shutil.rmtree(namespace, ignore_errors=True)
        raise

    for path in moved:
        if path.is_dir():
            shutil.rmtree(path, ignore_errors=True)
        else:
            path.unlink(missing_ok=True)
    old_token.unlink(missing_ok=True)
    (old_state / POINTER_FILENAME).write_text(
        f"This hub's state moved to {state}. See `robomate status`.\n", encoding="utf-8"
    )
    return Relocation(
        hub.hub_id,
        name,
        old_state,
        old_token,
        state,
        token,
        tuple(git_dir for git_dir, _ in found),
    )
