"""Persistent full-clone identities shared by bootstrap and worker configuration."""

from __future__ import annotations

import json
import os
import re
import subprocess
from pathlib import Path
from typing import Any

from .config import ConfigurationError

IDENTITY_FILE = "robo-agents-workspace.json"


def git(workspace: Path, *args: str) -> str:
    try:
        return subprocess.run(
            ["git", "-C", str(workspace), *args], check=True, capture_output=True, text=True
        ).stdout.strip()
    except (OSError, subprocess.CalledProcessError) as exc:
        raise ConfigurationError(
            "HUB_WORKSPACE must identify an accessible full Git clone"
        ) from exc


def canonical_workspace(raw: str) -> Path:
    path = Path(raw)
    if not path.is_absolute() or path != path.resolve():
        raise ConfigurationError("HUB_WORKSPACE must be an absolute canonical path")
    if not (path / ".git").is_dir():
        raise ConfigurationError(
            "HUB_WORKSPACE must be a full clone, not a worktree or missing path"
        )
    if Path(git(path, "rev-parse", "--show-toplevel")).resolve() != path:
        raise ConfigurationError("HUB_WORKSPACE must name the clone root")
    common = Path(git(path, "rev-parse", "--path-format=absolute", "--git-common-dir"))
    if common.resolve() != path / ".git":
        raise ConfigurationError("HUB_WORKSPACE must have its own Git directory")
    if git(path, "rev-parse", "--is-shallow-repository") != "false":
        raise ConfigurationError("HUB_WORKSPACE must be a full, non-shallow clone")
    return path


def read_identity(workspace: Path, agent: str) -> dict[str, Any]:
    identity_path = workspace / ".git" / IDENTITY_FILE
    try:
        if identity_path.is_symlink():
            raise ValueError("identity file must not be a symlink")
        if os.name != "nt" and identity_path.stat().st_mode & 0o077:
            raise ValueError("identity file must have owner-only permissions (0600)")
        value = json.loads(identity_path.read_text(encoding="utf-8"))
        if (
            not isinstance(value, dict)
            or not isinstance(value.get("workspace_id"), str)
            or not re.fullmatch(r"[0-9a-f]{64}", value["workspace_id"])
        ):
            raise ValueError("workspace_id must be 64 lowercase hexadecimal characters")
        if value.get("agent") != agent or value.get("path") != str(workspace):
            raise ValueError("identity agent/path does not match this workspace")
        if value.get("repository") != git(workspace, "remote", "get-url", "origin"):
            raise ValueError("identity repository does not match origin")
        return value
    except (OSError, ValueError) as exc:
        raise ConfigurationError(
            f"HUB_WORKSPACE identity invalid: {exc}; run scripts/bootstrap-workspace.sh"
        ) from exc
