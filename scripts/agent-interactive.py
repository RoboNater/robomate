#!/usr/bin/env python3
"""Launch a generated interactive start with per-launch permission controls."""

from __future__ import annotations

import argparse
import subprocess
import sys

from agent_interactive import add_commands, add_launch_controls, launch_command, split_arguments


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--harness", required=True)
    parser.add_argument("--powershell", action="store_true")
    add_commands(parser)
    words = sys.argv[1:]
    boundary = words.index("--") if "--" in words else len(words)
    args = parser.parse_args(words[:boundary])
    controls = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    add_launch_controls(controls)
    options, extra = split_arguments(words[boundary + 1 :])
    selected = controls.parse_args(options)
    try:
        command = launch_command(
            args.shell_command, args.safe_shell_command, selected.safe_mode, extra, args.powershell
        )
        return subprocess.call(command)
    except (OSError, ValueError) as exc:
        print(f"Cannot start interactive {args.harness}: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
