"""Per-child OpenCode policy. Interactive and safe-mode launches do not use this."""

from __future__ import annotations

import copy
import json
import os
import re
import subprocess
import tempfile
import uuid
from collections.abc import Mapping
from fnmatch import fnmatchcase
from pathlib import Path
from typing import Any


class OpenCodeConfigError(ValueError):
    """The OpenCode configuration probe failed or returned invalid data."""


_VERSION_MAJOR = re.compile(r"^v?(\d+)(?:\.\d+)*")
_V2_FS_TOOLS = ("read", "edit", "write", "glob", "grep", "list")


def parse_major_version(output: str) -> int | None:
    """Parse OpenCode's major version from ``--version`` output (1.x or 2.x)."""
    words = output.split()
    if not words:
        return None
    token = words[1] if len(words) > 1 and words[0].lower() == "opencode" else words[0]
    match = _VERSION_MAJOR.match(token)
    return int(match.group(1)) if match else None


def detect_major_version(
    program: list[str], environment: Mapping[str, str], workdir: Path | None = None
) -> int:
    """Return OpenCode's major version (2 for 2.x, 1 for 1.x or minimal fake harnesses)."""
    try:
        probe = subprocess.run(
            [*program, "--version"],
            cwd=workdir,
            env=environment,
            capture_output=True,
            text=True,
            check=False,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return 1
    if probe.returncode != 0:
        return 1
    major = parse_major_version(probe.stdout)
    return major if major is not None else 1


def _deep_merge(base: dict[str, Any], incoming: Mapping[str, Any]) -> dict[str, Any]:
    merged = copy.deepcopy(base)
    for key, value in incoming.items():
        if isinstance(value, Mapping) and isinstance(merged.get(key), dict):
            merged[key] = _deep_merge(merged[key], value)
        else:
            merged[key] = copy.deepcopy(value)
    return merged


def _merge_v2_sources(sources: list[Any]) -> dict[str, Any]:
    merged: dict[str, Any] = {}
    for entry in sources:
        if not isinstance(entry, Mapping):
            continue
        info = entry.get("info")
        if isinstance(info, Mapping):
            merged = _deep_merge(merged, info)
    return merged


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


def _token_read_paths(token_dir: Path, worktree: Path) -> list[str]:
    paths = [token_dir.as_posix().rstrip("/")]
    if os.path.normcase(token_dir.drive) == os.path.normcase(worktree.drive):
        rel = Path(os.path.relpath(token_dir, worktree)).as_posix().rstrip("/")
        if rel not in paths:
            paths.append(rel)
    return paths


def policy_v2(
    config: Mapping[str, Any], roots: list[Path], name: str, *, token_dir: Path | None = None
) -> dict[str, Any]:
    """Build an OpenCode 2.x agent entry with ordered ``permissions`` rules.

    OpenCode 2.x removes ``external_directory`` prompts and matches path rules
    against the literal path passed to each tool. To preserve narrow worktree
    grants while still allowing relative paths inside the worktree:
      - deny absolute paths (``/*``, ``?:/*``) and parent traversals (``..``,
        ``../*``, ``*/../*``) for filesystem tools;
      - allow each assigned root (``<root>`` and ``<root>/*``);
      - deny ``read`` on ``token_dir`` (both absolute and relative to the
        worktree) after the root allows.
    """
    build = copy.deepcopy(config.get("agents", {}).get("build", {}))
    if not isinstance(build, dict):
        build = {}
    raw_permissions = build.pop("permissions", None)
    if isinstance(raw_permissions, list) and all(isinstance(r, dict) for r in raw_permissions):
        permissions: list[dict[str, Any]] = copy.deepcopy(raw_permissions)
    else:
        permissions = [{"action": "*", "resource": "*", "effect": "allow"}]

    for tool in _V2_FS_TOOLS:
        for deny_pattern in ("/*", "?:/*", "..", "../*", "*/../*"):
            permissions.append({"action": tool, "resource": deny_pattern, "effect": "deny"})
        for root in roots:
            root_posix = root.as_posix().rstrip("/")
            permissions.append({"action": tool, "resource": root_posix, "effect": "allow"})
            permissions.append({"action": tool, "resource": f"{root_posix}/*", "effect": "allow"})

    if token_dir is not None:
        if "*" in str(token_dir) or "?" in str(token_dir):
            raise ValueError("OpenCode token directory cannot contain wildcard characters")
        for path in _token_read_paths(token_dir, roots[0]):
            permissions.append({"action": "read", "resource": path, "effect": "deny"})
            permissions.append({"action": "read", "resource": f"{path}/*", "effect": "deny"})

    build.update(permissions=permissions, mode="primary")
    return {"agents": {name: build}}


def policy(
    config: Mapping[str, Any],
    roots: list[Path],
    name: str,
    *,
    token_dir: Path | None = None,
    version: int = 1,
) -> dict[str, Any]:
    """Copy build into a fresh agent so inherited dictionary key order cannot win.

    When ``version >= 2``, emits OpenCode 2.x's ``agents.<name>.permissions``
    ordered array instead of 1.x's ``agent.<name>.permission`` keyed object.
    """
    if version >= 2:
        return policy_v2(config, roots, name, token_dir=token_dir)
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


def read_config_with_version(
    program: list[str],
    environment: Mapping[str, str],
    worktree: Path,
    *,
    standalone: bool = False,
) -> tuple[dict[str, Any], int]:
    """Read effective OpenCode config and detect whether it uses 1.x or 2.x shape."""
    command = [*program, "debug", "config", *(["--standalone"] if standalone else [])]
    with tempfile.TemporaryFile() as output:
        try:
            probe = subprocess.run(
                command,
                cwd=worktree,
                env=environment,
                stdout=output,
                stderr=subprocess.PIPE,
                check=False,
                timeout=30,
            )
        except (OSError, subprocess.SubprocessError) as exc:
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
    if isinstance(config, list):
        return _merge_v2_sources(config), 2
    if isinstance(config, dict):
        return config, 1
    raise OpenCodeConfigError("OpenCode `debug config` did not return a configuration object")


def read_config(
    program: list[str],
    environment: Mapping[str, str],
    worktree: Path,
    *,
    standalone: bool = False,
) -> dict[str, Any]:
    """Read effective OpenCode config without reporting the raw output."""
    return read_config_with_version(program, environment, worktree, standalone=standalone)[0]


def _fold_v2_variant(command: list[str]) -> list[str]:
    """Convert 1.x ``--variant <v>`` into 2.x ``--model <m>#<v>`` and drop ``--variant``."""
    variant: str | None = None
    cleaned: list[str] = []
    i = 0
    while i < len(command):
        word = command[i]
        if word == "--variant" and i + 1 < len(command):
            variant = command[i + 1]
            i += 2
            continue
        if word.startswith("--variant="):
            variant = word.split("=", 1)[1]
            i += 1
            continue
        cleaned.append(word)
        i += 1
    if variant:
        for idx, word in enumerate(cleaned):
            if word == "--model" and idx + 1 < len(cleaned) and "#" not in cleaned[idx + 1]:
                cleaned[idx + 1] = f"{cleaned[idx + 1]}#{variant}"
                break
            if word.startswith("--model=") and "#" not in word:
                cleaned[idx] = f"{word}#{variant}"
                break
    return cleaned


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
    program = argv[:run_index]
    major = detect_major_version(program, env, worktree)
    config, schema_version = read_config_with_version(
        program, env, worktree, standalone=(major >= 2)
    )
    is_v2 = major >= 2 or schema_version >= 2
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
    if is_v2:
        overlay.setdefault("agents", {}).update(
            policy_v2(config, roots, name, token_dir=token_dir)["agents"]
        )
    else:
        overlay.setdefault("agent", {}).update(
            policy(config, roots, name, token_dir=token_dir, version=1)["agent"]
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
    if is_v2:
        command = _fold_v2_variant(command)
        insert_flags = (
            ["--standalone", "--agent", name]
            if "--standalone" not in command
            else ["--agent", name]
        )
        command[run_index + 1 : run_index + 1] = insert_flags
    else:
        command[run_index + 1 : run_index + 1] = ["--agent", name]
    return command, env
