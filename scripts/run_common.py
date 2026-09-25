#!/usr/bin/env python3
"""Shared run-directory preparation helpers for Step 6 and user runs.

Step 6 (``scripts/step6.py``) and user runs (``scripts/prepare-run.py``) render
the same MCP configs, Codex home, and worker prompts. This module holds the
generic pieces so the two entry points cannot drift; sandbox seeding,
disturbances, and measurement stay in ``step6.py``.
"""

from __future__ import annotations

import json
import os
import secrets
import shutil
import subprocess
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]

#: The six worker-mcp coordination tools every worker config must approve.
TOOLS = [
    "check_in",
    "get_role_guide",
    "await_assignment",
    "report_progress",
    "ask_alice",
    "submit_result",
]

#: CLI invoked with ``--version`` to report each harness version.
VERSION_COMMANDS = {
    "claude-code": "claude",
    "codex": "codex",
    "opencode": "opencode",
    "gemini": "gemini",
}

#: Default model provider per harness. An empty value means the caller must supply one.
PROVIDERS = {
    "claude-code": "anthropic",
    "codex": "openai",
    "gemini": "google",
}


def executable(name: str) -> str:
    """Resolve PATHEXT shims (npm .cmd, pyenv .bat) that CreateProcess cannot find alone."""
    return shutil.which(name) or name


def run(*args: Any, cwd: Path | str | None = None, env: dict[str, str] | None = None) -> str:
    """Run a subprocess, returning stripped stdout; raises on failure."""
    merged = None if env is None else {**os.environ, **env}
    return subprocess.run(
        [executable(str(args[0])), *[str(arg) for arg in args[1:]]],
        cwd=cwd,
        env=merged,
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    ).stdout.strip()


def run_output(
    *args: Any, cwd: Path | str | None = None, env: dict[str, str] | None = None
) -> tuple[str, str]:
    """Run a subprocess, returning stripped (stdout, stderr); raises on failure.

    Some CLIs report status on stderr with exit code 0 (e.g. ``codex login
    status`` on Windows), so callers that need the human-readable status use
    this instead of :func:`run`.
    """
    merged = None if env is None else {**os.environ, **env}
    proc = subprocess.run(
        [executable(str(args[0])), *[str(arg) for arg in args[1:]]],
        cwd=cwd,
        env=merged,
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    return proc.stdout.strip(), proc.stderr.strip()


def save(path: Path, value: Any) -> None:
    """Atomic private checkpoint; all generated run artifacts remain untracked."""
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("w", encoding="utf-8") as stream:
        os.chmod(temporary, 0o600)
        json.dump(value, stream, indent=2, sort_keys=True)
        stream.write("\n")
    temporary.replace(path)


def write_private_text(path: Path, text: str) -> None:
    """Write a credential-bearing text file (e.g. Codex TOML) with mode 0600."""
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("w", encoding="utf-8") as stream:
        os.chmod(temporary, 0o600)
        stream.write(text)
    temporary.replace(path)


def link_or_copy(source: Path, destination: Path) -> None:
    """Expose a checked-in directory; unprivileged Windows accounts cannot symlink."""
    if destination.exists():
        return
    try:
        destination.symlink_to(source, target_is_directory=True)
    except OSError:
        shutil.copytree(source, destination)


def codex_home(directory: Path, name: str) -> Path:
    """Run-local CODEX_HOME that reuses login without copying it into a second file."""
    home = directory / name
    home.mkdir(exist_ok=True, mode=0o700)
    auth = Path(os.environ.get("CODEX_HOME", str(Path.home() / ".codex"))) / "auth.json"
    if auth.exists() and not (home / "auth.json").exists():
        try:
            (home / "auth.json").symlink_to(auth)
        except OSError:
            # Hard link: no extra copy of the credential and no symlink privilege.
            try:
                os.link(auth, home / "auth.json")
            except OSError as exc:
                raise ValueError(
                    f"cannot link {auth} into {home}; hard links need the same volume, "
                    "so place RUN_DIR on the drive holding CODEX_HOME"
                ) from exc
    return home


def codex_mcp(
    command: str, args: list[str], env: dict[str, str], tools: list[str], timeout: int
) -> str:
    """Render the ``[mcp_servers.hub]`` TOML section for a Codex home."""
    config = f'[mcp_servers.hub]\ncommand = "{command}"\n'
    config += "args = " + json.dumps(args) + "\n"
    config += f"startup_timeout_sec = 120\ntool_timeout_sec = {timeout}\n"
    config += "enabled_tools = " + json.dumps(tools) + "\n"
    config += (
        "env = { "
        + ", ".join(key + " = " + json.dumps(value) for key, value in env.items())
        + " }\n"
    )
    for tool in tools:
        config += f'\n[mcp_servers.hub.tools.{tool}]\napproval_mode = "approve"\n'
    return config


def codex_sandbox() -> str:
    """Sandbox stanza every Codex home needs for GitHub and hub network access."""
    config = 'sandbox_mode = "workspace-write"\n'
    if os.name == "nt":
        config += '[windows]\nsandbox = "unelevated"\n'
    return config + "[sandbox_workspace_write]\nnetwork_access = true\n"


def parse_harness_version(harness: str, output: str) -> str:
    """The bare version reported at check-in, from the recorded ``--version`` output."""
    words = output.split()
    if not words:
        raise ValueError(f"{harness} reported an empty --version")
    return words[-1] if harness == "codex" else words[0]


def probe_harness_version(harness: str) -> tuple[str, str]:
    """Run the harness CLI's ``--version``; fail with an actionable message if missing."""
    if harness not in VERSION_COMMANDS:
        raise ValueError(
            f"unknown harness {harness!r}; expected one of {sorted(VERSION_COMMANDS)}"
        )
    command = VERSION_COMMANDS[harness]
    try:
        output = run(command, "--version")
    except (OSError, subprocess.CalledProcessError) as exc:
        raise ValueError(
            f"harness {harness!r} requires the {command!r} CLI on PATH "
            f"({command} --version failed); install and authenticate it first"
        ) from exc
    return output, parse_harness_version(harness, output)


def ensure_token(token_path: Path) -> str:
    """Create or reuse the bearer token; never rewrite an existing one."""
    token_path.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
    if not token_path.exists():
        token_path.write_text(secrets.token_hex(32) + "\n", encoding="utf-8")
        os.chmod(token_path, 0o600)
    return token_path.read_text(encoding="utf-8").strip()


def bootstrap_clone(agent: str, destination: Path, repository: str) -> dict[str, Any]:
    """Bootstrap a worker clone via the existing entry point; never touch an existing clone."""
    try:
        raw = run(
            "uv",
            "run",
            "--locked",
            "--project",
            str(ROOT),
            "python",
            str(ROOT / "scripts/bootstrap-workspace.py"),
            agent,
            str(destination),
            repository,
        )
    except subprocess.CalledProcessError as exc:
        detail = ((exc.stderr or "") + "\n" + (exc.stdout or "")).strip()
        # The child (e.g. an uncaught git failure) may dump a full traceback;
        # drop its frame lines so only the actionable lines survive, no stack.
        nonempty = [line for line in detail.splitlines() if line.strip()]
        substantive = [
            line
            for line in nonempty
            if line != "Traceback (most recent call last):" and not line.startswith("  ")
        ]
        tail = "\n".join((substantive or nonempty)[-3:])
        raise ValueError(
            f"bootstrap failed for {agent} at {destination}"
            + (f": {tail}" if tail else "")
        ) from exc
    value = json.loads(raw)
    if not isinstance(value, dict):
        raise ValueError("bootstrap-workspace.py did not return an identity object")
    return value


def render_claude_mcp(env: dict[str, str], root: str | None = None) -> dict[str, Any]:
    """Render a Claude worker ``*.mcp.json`` from the checked-in template.

    ``root`` is the robo-agents checkout ``uv`` runs ``worker-mcp`` from, as
    the worker's host spells it; it defaults to this checkout.
    """
    template = json.loads((ROOT / "runtimes/claude-code.mcp.json").read_text(encoding="utf-8"))
    directory = str(ROOT) if root is None else root
    template["mcpServers"]["hub"].update(
        {"args": ["run", "--locked", "--directory", directory, "worker-mcp"], "env": env}
    )
    return template


def render_codex_config(env: dict[str, str], worker_args: list[str]) -> str:
    """Render a run-local Codex ``config.toml``; asserts template tool parity."""
    import tomllib

    reference = tomllib.loads(
        (ROOT / "runtimes/codex.config.toml").read_text(encoding="utf-8")
    )["mcp_servers"]["hub"]
    config = codex_sandbox() + codex_mcp("uv", worker_args, env, TOOLS, 330)
    if sorted(reference.get("tools", {})) != sorted(TOOLS):
        raise ValueError("runtimes/codex.config.toml tools drifted from the shared TOOLS list")
    return config


def render_worker_prompt(agent: str) -> str:
    """Render ``prompts/worker.md`` for one agent; refuse an unrendered placeholder."""
    instruction = (ROOT / "prompts/worker.md").read_text(encoding="utf-8").replace(
        "$AGENT_NAME", agent
    )
    if "$AGENT_NAME" in instruction:
        raise ValueError("worker prompt still contains $AGENT_NAME after rendering")
    return instruction


def parse_github_slug(repository: str) -> str | None:
    """Return ``owner/repo`` for GitHub URLs/SLUGs, else None for local paths."""
    text = repository.strip()
    if text.startswith("git@github.com:"):
        slug = text.removeprefix("git@github.com:").removesuffix(".git")
    elif "github.com/" in text:
        slug = text.split("github.com/", 1)[1].removesuffix(".git").lstrip("/")
    elif text.count("/") == 1 and "://" not in text and not Path(text).exists():
        slug = text.removesuffix(".git")
    else:
        return None
    parts = slug.strip("/").split("/")
    if len(parts) != 2 or not all(parts):
        return None
    return f"{parts[0]}/{parts[1]}"


def slug_clone_url(slug: str) -> str:
    """Clone URL for a bare ``owner/repo`` slug honoring gh's protocol.

    ``gh`` is already a hard dependency of the run preparation path, so ask it
    which protocol it uses for github.com: an ssh-configured ``gh`` has no
    https credential helper and cannot clone the https URL. Anything
    unexpected (no ``gh``, no configured protocol) falls back to https.
    """
    try:
        protocol = run("gh", "config", "get", "-h", "github.com", "git_protocol")
    except (OSError, subprocess.CalledProcessError):
        protocol = ""
    if protocol.strip().lower() == "ssh":
        return f"git@github.com:{slug}.git"
    return f"https://github.com/{slug}.git"


def clone_source(repository: str, slug: str | None) -> str:
    """Return a cloneable source for a repository argument.

    A bare ``owner/repo`` slug (or ``slug.git`` spelling) passes the ``gh``
    preflight checks but is not a valid ``git clone`` argument, so expand it
    via :func:`slug_clone_url`. Anything already URL-shaped (``://``,
    ``git@``) or an existing local path passes through for
    bootstrap-workspace.py to validate.
    """
    if slug is None:
        return repository
    text = repository.strip()
    if "://" in text or text.startswith("git@") or Path(text).exists():
        return repository
    return slug_clone_url(slug)
