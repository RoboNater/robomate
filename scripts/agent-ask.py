#!/usr/bin/env python3
"""Open a saved conversation interactively without the hub or recovery guards."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager, nullcontext
from pathlib import Path

from agent_sessions import exact_id


def conversation(sessions: Path, agent: str, harness: str, supplied: str | None) -> str:
    """Select the last recorded exact ID, without changing any run files."""
    if supplied is not None:
        if not exact_id(harness, supplied):
            raise ValueError("Invalid conversation ID; supply an exact harness ID, never -c/--last")
        return supplied
    try:
        lines = sessions.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        lines = []
    ids: list[str] = []
    for line in lines:
        try:
            row = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ValueError(
                f"Session log {sessions} is malformed; supply an explicit ID to bypass discovery"
            ) from exc
        if not isinstance(row, dict) or row.get("agent") != agent or row.get("harness") != harness:
            raise ValueError(
                f"Session log {sessions} is malformed or belongs to another agent; "
                "supply an explicit ID to bypass discovery"
            )
        value = row.get("conversation_id")
        if value is None:
            continue
        if not isinstance(value, str) or not exact_id(harness, value):
            raise ValueError(f"Invalid saved conversation ID in {sessions}")
        # A conversation may appear repeatedly or again after a newer start.
        if value in ids:
            ids.remove(value)
        ids.append(value)
    if not ids:
        raise ValueError(f"No saved conversation ID in {sessions}; supply an explicit ID")
    if len(ids) > 1:
        print("Other recorded conversations: " + ", ".join(ids[:-1]), flush=True)
    return ids[-1]


@contextmanager
def suppress_mcp(home: Path) -> Iterator[None]:
    """Temporarily suppress AntiGravity's run-home MCP, then restore exact bytes.

    Operator decision, #221: keep the original HOME and conversation store.
    Exclusive backup creation prevents two asks from overwriting the saved config.
    A hard-kill backup is deliberately retained for explicit restoration.
    """
    config = home / ".gemini/config/mcp_config.json"
    backup = config.with_name(config.name + ".ask-backup")
    try:
        fd = os.open(backup, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError as exc:
        raise ValueError(
            f"An ask may still be running, or was killed. After it exits, restore {backup} "
            f"to {config} before asking again"
        ) from exc
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(config.read_bytes())
    except BaseException:
        backup.unlink()
        raise
    temporary: Path | None = None
    try:
        fd, name = tempfile.mkstemp(dir=config.parent, prefix=".ask-mcp-")
        temporary = Path(name)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump({"mcpServers": {}}, stream)
        os.replace(temporary, config)
        yield
    finally:
        os.replace(backup, config)
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def interrupted(signum: int, frame: object) -> None:
    """Unwind config cleanup on termination, terminal hangup, and ordinary Ctrl-C."""
    raise KeyboardInterrupt


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--agent", required=True)
    parser.add_argument("--harness", required=True)
    parser.add_argument("--sessions", required=True, type=Path)
    parser.add_argument("--workdir", required=True)
    parser.add_argument("--shell-command", required=True)
    parser.add_argument("--fork-command")
    parser.add_argument("--powershell", action="store_true")
    parser.add_argument("--config", required=True, type=Path)
    # User options are separated from the generated, pinned command.
    words = sys.argv[1:]
    boundary = words.index("--") if "--" in words else len(words)
    args = parser.parse_args(words[:boundary])
    options = argparse.ArgumentParser(description="Ask this agent questions; no hub is needed")
    options.add_argument("conversation_id", nargs="?")
    options.add_argument("--fork", action="store_true", help="ask in a new conversation")
    controls = options.parse_args(words[boundary + 1 :])
    try:
        selected = conversation(args.sessions, args.agent, args.harness, controls.conversation_id)
        command = args.shell_command
        if controls.fork:
            if not args.fork_command:
                raise ValueError("AntiGravity has no CLI fork option; --fork is unavailable")
            command = args.fork_command
        print(
            f"Ask {args.agent} ({args.harness}): conversation {selected}; "
            f"working directory {args.workdir}; hub tools are unavailable.",
            flush=True,
        )
        env = os.environ | {"ROBOMATE_ASK_SESSION_ID": selected}
        if args.powershell:
            shell = shutil.which("pwsh") or shutil.which("powershell")
            if shell is None:
                raise OSError("PowerShell is unavailable")
            argv = [shell, "-NoProfile", "-Command", command + "\nexit $LASTEXITCODE"]
        else:
            shell = shutil.which("bash")
            if shell is None:
                raise OSError("Bash is unavailable")
            argv = [shell, "-c", command]
        suppression = suppress_mcp(args.config) if args.harness == "antigravity" else nullcontext()
        signals = [signal.SIGINT, signal.SIGTERM]
        if hasattr(signal, "SIGHUP"):
            signals.append(signal.SIGHUP)
        previous = {sig: signal.signal(sig, interrupted) for sig in signals}
        try:
            with suppression:
                return subprocess.call(argv, env=env)
        except KeyboardInterrupt:
            return 130
        finally:
            for sig, handler in previous.items():
                signal.signal(sig, handler)
    except (OSError, ValueError) as exc:
        print(f"Cannot ask {args.agent}: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
