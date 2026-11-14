"""Per-child OpenCode policy. Interactive and safe-mode launches do not use this."""

from __future__ import annotations

import copy
import json
import subprocess
import uuid
from collections.abc import Mapping
from pathlib import Path
from typing import Any


def allowed_roots(worktree: Path, hub: Path | None, lane_tmp: Path | None) -> list[Path]:
    """Use only known roots; a missing metadata field never grants its parent."""
    if any(root is not None and not root.is_absolute() for root in (worktree, hub, lane_tmp)):
        raise ValueError("OpenCode permission roots require absolute metadata paths")
    roots = [worktree.resolve()]
    if hub is not None:
        roots.append(hub.resolve())
    result = subprocess.run(
        ["git", "-C", str(worktree), "rev-parse", "--path-format=absolute", "--git-common-dir"],
        capture_output=True,
        text=True,
        check=False,
        timeout=10,
    )
    if result.returncode == 0 and Path(result.stdout.strip()).is_absolute():
        roots.append(Path(result.stdout.strip()).resolve())
    if lane_tmp is not None:
        roots.append(lane_tmp.resolve())
    if any("*" in str(root) or "?" in str(root) for root in roots):
        raise ValueError("OpenCode permission roots cannot contain wildcard characters")
    return list(dict.fromkeys(roots))


def policy(config: Mapping[str, Any], roots: list[Path], name: str) -> dict[str, Any]:
    """Copy build into a fresh agent so inherited dictionary key order cannot win.

    Global permissions still precede this agent. Other build settings and tool
    denials survive; external_directory is deliberately the last permission.
    OpenCode additionally permits its own tool-output directory.
    """
    build = copy.deepcopy(config.get("agent", {}).get("build", {}))
    permissions = build.pop("permission", {})
    if isinstance(permissions, str):
        permissions = {"*": permissions}
    permissions = dict(permissions)
    permissions.pop("external_directory", None)
    permissions["external_directory"] = {
        "*": "deny",
        **{root.as_posix().rstrip("/") + "/*": "allow" for root in roots},
    }
    build.update(permission=permissions, mode="primary")
    return {"agent": {name: build}}


def prepare(
    argv: list[str], environment: Mapping[str, str], worktree: Path, sessions: Path
) -> tuple[list[str], dict[str, str]]:
    """Read effective configuration without changing user files, then pin our agent."""
    env = dict(environment)
    # Preserve wrappers used by operators (and fake harnesses in tests).
    run_index = argv.index("run")
    probe = subprocess.run(
        [*argv[:run_index], "debug", "config"],
        cwd=worktree,
        env=env,
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )
    if probe.returncode:
        raise ValueError("OpenCode effective configuration could not be read (debug config)")
    config = json.loads(probe.stdout)
    hub = env.get("ROBOMATE_LAUNCH_HUB_CHECKOUT")
    cache = env.get("UV_CACHE_DIR")
    lane_tmp = Path(cache).parent if cache and Path(cache).name == "uv-cache" else None
    # Prepared runs already have a local metadata file beside their journal.
    manifest = sessions.parent / "run.json"
    if not hub and manifest.is_file():
        metadata = json.loads(manifest.read_text(encoding="utf-8"))
        hub = metadata.get("hub_repo")
        if metadata.get("state_dir"):
            env["ROBOMATE_PERMISSION_STATUS_DIR"] = str(Path(metadata["state_dir"]) / "sessions")
    roots = allowed_roots(worktree, Path(hub) if hub else None, lane_tmp)
    name = "robomate_headless_" + uuid.uuid4().hex
    overlay = json.loads(env.get("OPENCODE_CONFIG_CONTENT", "{}"))
    overlay.setdefault("agent", {}).update(policy(config, roots, name)["agent"])
    plugins = list(overlay.get("plugin", []))
    plugin = Path(__file__).with_name("permission-events.mjs").resolve().as_uri()
    if plugin not in plugins:
        plugins.append(plugin)
    overlay["plugin"] = plugins
    env["OPENCODE_CONFIG_CONTENT"] = json.dumps(overlay)
    command = [
        word for word in argv if word != "--auto" and not word.startswith(("--auto=", "--agent="))
    ]
    if "--agent" in command:
        index = command.index("--agent")
        del command[index : index + 2]
    command[run_index + 1 : run_index + 1] = ["--agent", name]
    return command, env
