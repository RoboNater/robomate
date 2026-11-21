"""Per-child OpenCode policy. Interactive and safe-mode launches do not use this."""

from __future__ import annotations

import copy
import json
import os
import subprocess
import tempfile
import uuid
from collections.abc import Mapping
from fnmatch import fnmatchcase
from pathlib import Path
from typing import Any


class OpenCodeConfigError(ValueError):
    """The OpenCode configuration probe failed or returned invalid data."""


def allowed_roots(worktree: Path, hub: Path | None, lane_tmp: Path | None) -> list[Path]:
    """Use only known roots; a missing metadata field never grants its parent."""
    if any(root is not None and not root.is_absolute() for root in (worktree, hub, lane_tmp)):
        raise ValueError("OpenCode permission roots require absolute metadata paths")
    roots = [worktree.resolve()]
    if hub is not None:
        roots.append(hub.resolve())
    try:
        result = subprocess.run(
            ["git", "-C", str(worktree), "rev-parse", "--path-format=absolute", "--git-common-dir"],
            capture_output=True,
            text=True,
            check=False,
            timeout=10,
        )
    except subprocess.SubprocessError as exc:
        raise ValueError("Git common-directory probe failed or timed out") from exc
    if result.returncode == 0 and Path(result.stdout.strip()).is_absolute():
        roots.append(Path(result.stdout.strip()).resolve())
    if lane_tmp is not None:
        roots.append(lane_tmp.resolve())
    if any("*" in str(root) or "?" in str(root) for root in roots):
        raise ValueError("OpenCode permission roots cannot contain wildcard characters")
    return list(dict.fromkeys(roots))


def _deny_token_reads(permissions: dict[str, Any], token_dir: Path, worktree: Path) -> None:
    # Rebuild the effective read rules in their original order before moving
    # the read entry to the end. A later wildcard must still govern other reads.
    reads: dict[str, str] = {}
    for tool, rules in permissions.items():
        if "[" not in tool and fnmatchcase("read", os.path.normcase(tool)):
            for pattern, action in ({"*": rules} if isinstance(rules, str) else rules).items():
                reads.pop(pattern, None)
                reads[pattern] = action
    # OpenCode 1.x evaluates read paths relative to its project worktree.
    paths = [token_dir.as_posix()]
    if os.path.normcase(token_dir.drive) == os.path.normcase(worktree.drive):
        paths.append(Path(os.path.relpath(token_dir, worktree)).as_posix())
    for path in paths:
        for pattern in (path, path.rstrip("/") + "/*"):
            reads.pop(pattern, None)
            reads[pattern] = "deny"
    permissions.pop("read", None)
    permissions["read"] = reads


def policy(
    config: Mapping[str, Any], roots: list[Path], name: str, *, token_dir: Path | None = None
) -> dict[str, Any]:
    """Copy build into a fresh agent so inherited dictionary key order cannot win.

    Global permissions still precede this agent. Other build settings and tool
    denials survive; external_directory is deliberately the last permission.
    OpenCode additionally permits its own tool-output directory. Other built-in
    external allows (temporary tool paths and discovered skill directories) are
    intentionally denied unless they are inside one of the assigned roots.
    Native Windows OpenCode path matching still requires a live platform check.
    """
    build = copy.deepcopy(config.get("agent", {}).get("build", {}))
    permissions = build.pop("permission", {})
    if isinstance(permissions, str):
        permissions = {"*": permissions}
    permissions = dict(permissions)
    external = {"*": "deny", **{root.as_posix().rstrip("/") + "/*": "allow" for root in roots}}
    if token_dir is not None:
        if "*" in str(token_dir) or "?" in str(token_dir):
            raise ValueError("OpenCode token directory cannot contain wildcard characters")
        _deny_token_reads(permissions, token_dir, roots[0])
        pattern = token_dir.as_posix().rstrip("/") + "/*"
        external.pop(pattern, None)
        external[pattern] = "deny"
    permissions.pop("external_directory", None)
    permissions["external_directory"] = external
    build.update(permission=permissions, mode="primary")
    return {"agent": {name: build}}


def prepare(
    argv: list[str],
    environment: Mapping[str, str],
    worktree: Path,
    sessions: Path,
    *,
    agent: str | None = None,
    lane_tmp: Path | None = None,
    token_file: Path | None = None,
) -> tuple[list[str], dict[str, str]]:
    """Read effective configuration without changing user files, then pin our agent."""
    env = dict(environment)
    # Preserve wrappers used by operators (and fake harnesses in tests).
    run_index = argv.index("run")
    with tempfile.TemporaryFile() as output:
        try:
            probe = subprocess.run(
                [*argv[:run_index], "debug", "config"],
                cwd=worktree,
                env=env,
                stdout=output,
                stderr=subprocess.PIPE,
                check=False,
                timeout=30,
            )
        except subprocess.SubprocessError as exc:
            raise OpenCodeConfigError(
                "OpenCode effective configuration probe failed or timed out"
            ) from exc
        if probe.returncode:
            raise OpenCodeConfigError(
                "OpenCode effective configuration could not be read (debug config)"
            )
        output.seek(0)
        data = output.read()
    try:
        config = json.loads(data)
    except (ValueError, UnicodeDecodeError) as exc:
        raise OpenCodeConfigError(
            f"OpenCode `debug config` returned invalid JSON ({len(data)} bytes)"
        ) from exc
    hub = env.get("ROBOMATE_LAUNCH_HUB_CHECKOUT")
    # Only explicit launcher/run metadata establishes a temporary root. An
    # ambient UV_CACHE_DIR=/tmp/uv-cache must never grant all of /tmp.
    # Prepared runs already have a local metadata file beside their journal.
    manifest = sessions.parent / "run.json"
    env["ROBOMATE_PERMISSION_STATUS_DIR"] = str(sessions.parent)
    if manifest.is_file():
        metadata = json.loads(manifest.read_text(encoding="utf-8"))
        hub = metadata.get("hub_repo")
        lane_tmp = None
        run_dir = metadata.get("run_dir")
        if (
            agent
            and isinstance(run_dir, str)
            and Path(run_dir).is_absolute()
            and Path(run_dir).resolve() == sessions.parent.resolve()
        ):
            lane_tmp = Path(run_dir) / "tmp" / agent
        if metadata.get("state_dir"):
            env["ROBOMATE_PERMISSION_STATUS_DIR"] = str(Path(metadata["state_dir"]) / "sessions")
    roots = allowed_roots(worktree, Path(hub) if hub else None, lane_tmp)
    name = "robomate_headless_" + uuid.uuid4().hex
    overlay = json.loads(env.get("OPENCODE_CONFIG_CONTENT", "{}"))
    token_dir = token_file.resolve().parent if token_file is not None else None
    overlay.setdefault("agent", {}).update(
        policy(config, roots, name, token_dir=token_dir)["agent"]
    )
    plugins = list(overlay.get("plugin", []))
    plugin = Path(__file__).with_name("permission-events.mjs").resolve().as_uri()
    if plugin not in plugins:
        plugins.append(plugin)
    overlay["plugin"] = plugins
    env["OPENCODE_CONFIG_CONTENT"] = json.dumps(overlay)
    command = [
        word for word in argv if word != "--auto" and not word.startswith(("--auto=", "--agent="))
    ]
    while "--agent" in command:
        index = command.index("--agent")
        if index + 1 >= len(command):
            raise ValueError("OpenCode --agent requires a value")
        del command[index : index + 2]
    command[run_index + 1 : run_index + 1] = ["--agent", name]
    return command, env
