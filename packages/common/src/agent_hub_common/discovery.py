"""Repository and hub discovery for the operator CLI and future MCP bridge."""

from __future__ import annotations

import json
import os
import re
import subprocess
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlparse


class DiscoveryError(RuntimeError):
    """A repository or hub cannot be identified unambiguously."""


@dataclass(frozen=True)
class Repository:
    root: Path
    git_common_dir: Path
    origin: str
    default_branch: str
    forge: str


@dataclass(frozen=True)
class HubEndpoint:
    url: str
    token: str


def _git(cwd: Path, *args: str) -> str:
    try:
        return subprocess.check_output(
            ["git", *args], cwd=cwd, text=True, stderr=subprocess.PIPE
        ).strip()
    except (OSError, subprocess.CalledProcessError) as exc:
        raise DiscoveryError(f"cannot resolve repository with git {' '.join(args)}: {exc}") from exc


def repo_root(cwd: Path) -> tuple[Path, Path]:
    """Find the owning checkout even when cwd is inside a linked worktree."""

    common = Path(_git(cwd, "rev-parse", "--path-format=absolute", "--git-common-dir"))
    if not common.is_absolute():
        common = (cwd / common).resolve()
    common = common.resolve()
    if common.name != ".git":
        raise DiscoveryError(f"git common directory is not a checkout: {common}")
    return common.parent, common


def resolve_repository(cwd: Path) -> Repository:
    root, common = repo_root(cwd)
    origin = _git(root, "remote", "get-url", "origin")
    if not origin:
        raise DiscoveryError("origin remote is empty")
    try:
        head = _git(root, "symbolic-ref", "--short", "refs/remotes/origin/HEAD")
    except DiscoveryError as exc:
        raise DiscoveryError(
            "origin/HEAD is unset; run git remote set-head origin --auto"
        ) from exc
    if not head.startswith("origin/"):
        raise DiscoveryError(f"unexpected origin/HEAD: {head}")
    host = urlparse(origin).hostname or re.match(r"[^@]+@([^:]+):", origin)
    host = host.group(1) if isinstance(host, re.Match) else host
    return Repository(root, common, origin, head.removeprefix("origin/"),
                      "github" if host == "github.com" else "unknown")


def state_dir(root: Path) -> Path:
    return root / ".robomate"


def read_hub_json(root: Path) -> dict[str, Any] | None:
    path = state_dir(root) / "hub.json"
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as exc:
        raise DiscoveryError(f"cannot read {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise DiscoveryError(f"invalid hub metadata in {path}")
    return value


def write_hub_json(root: Path, value: Mapping[str, Any]) -> None:
    directory = state_dir(root)
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=".hub-", dir=directory)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(value, stream, indent=2, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, directory / "hub.json")
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def ensure_excluded(common: Path) -> None:
    path = common / "info" / "exclude"
    path.parent.mkdir(parents=True, exist_ok=True)
    existing = path.read_text(encoding="utf-8") if path.exists() else ""
    if "/.robomate/" not in existing.splitlines():
        with path.open("a", encoding="utf-8") as stream:
            if existing and not existing.endswith("\n"):
                stream.write("\n")
            stream.write("/.robomate/\n")


def discover(cwd: Path, environ: Mapping[str, str] | None = None) -> HubEndpoint:
    """Resolve explicit settings, this repo, or the sole live registry entry."""

    from .registry import live_entries

    env = os.environ if environ is None else environ
    explicit_url = env.get("ROBOMATE_HUB_URL", "").strip()
    if explicit_url:
        token = env.get("ROBOMATE_TOKEN", "").strip()
        token_file = env.get("ROBOMATE_TOKEN_FILE", "").strip()
        if not token and token_file:
            token = Path(token_file).expanduser().read_text(encoding="utf-8").strip()
        if not token:
            raise DiscoveryError("ROBOMATE_HUB_URL requires ROBOMATE_TOKEN or ROBOMATE_TOKEN_FILE")
        return HubEndpoint(explicit_url.rstrip("/"), token)
    try:
        root, _ = repo_root(cwd)
    except DiscoveryError:
        root = None
    if root is not None:
        data = read_hub_json(root)
        if data is not None and data.get("url"):
            token = (state_dir(root) / "token").read_text(encoding="utf-8").strip()
            return HubEndpoint(str(data["url"]), token)
    entries = live_entries(env)
    if len(entries) == 1:
        entry = entries[0]
        token = (state_dir(Path(entry["repo_root"])) / "token").read_text(
            encoding="utf-8"
        ).strip()
        return HubEndpoint(entry["url"], token)
    choices = ", ".join(f"{item['repo_root']} ({item['url']})" for item in entries)
    raise DiscoveryError(
        f"cannot choose a hub; set ROBOMATE_HUB_URL and ROBOMATE_TOKEN. "
        f"Registered hubs: {choices or 'none'}"
    )
