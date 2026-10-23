"""Shared per-launch controls for generated interactive scripts (#335)."""

from __future__ import annotations

import argparse
import shlex
import shutil

ARGUMENT_SLOT = "__ROBOMATE_HARNESS_ARGS__"


def split_arguments(words: list[str]) -> tuple[list[str], list[str]]:
    """Only the first marker is ours; every following word belongs to the harness."""
    if "--harness-args" not in words:
        return words, []
    index = words.index("--harness-args")
    return words[:index], words[index + 1 :]


def add_commands(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--shell-command", required=True)
    # Optional for scripts generated before #335.
    parser.add_argument("--safe-shell-command")


def add_launch_controls(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--safe-mode", action="store_true", help="restore prompting defaults for this launch"
    )
    parser.add_argument(
        "--harness-args",
        nargs=argparse.REMAINDER,
        metavar="WORD",
        help="pass every following word verbatim to the harness; "
        "put wrapper controls and IDs before this marker",
    )


def quote_powershell(word: str) -> str:
    """Escape every PowerShell single-quote delimiter inside a literal string."""
    escaped = "".join(
        character * 2 if character in "'\u2018\u2019\u201a\u201b" else character
        for character in word
    )
    return "'" + escaped + "'"


def shell_argv(command: str, powershell: bool) -> list[str]:
    if powershell:
        shell = shutil.which("pwsh") or shutil.which("powershell")
        if shell is None:
            raise OSError("PowerShell is unavailable")
        return [shell, "-NoProfile", "-Command", command + "\nexit $LASTEXITCODE"]
    # Recovery imports split_arguments from this module. Resolve only at launch
    # time so both paths share its existing native-Windows/WSL guard.
    from agent_recovery import bash_executable

    return [bash_executable(), "-c", command]


def launch_command(
    command: str,
    safe_command: str | None,
    safe_mode: bool,
    harness_args: list[str],
    powershell: bool,
) -> list[str]:
    """Quote each opaque extra word exactly once for the generated inner shell.

    Replace the slot before execution, never evaluate the extra words as code.
    Templates and their permission variants are pinned by preparation.
    """
    if safe_mode:
        if safe_command is None:
            raise ValueError("Regenerate this script to use --safe-mode")
        command = safe_command
    if harness_args and ARGUMENT_SLOT not in command:
        raise ValueError("Regenerate this script to use --harness-args")

    def quote(word: str) -> str:
        return quote_powershell(word) if powershell else shlex.quote(word)

    command = command.replace(ARGUMENT_SLOT, " ".join(map(quote, harness_args)))
    mode = "safe-mode (prompting defaults)" if safe_mode else "run permissions"
    print(f"Interactive launch: {mode}; harness arguments: {harness_args!r}", flush=True)
    return shell_argv(command, powershell)
