#!/usr/bin/env python3
"""Guarded interactive recovery; generated scripts preserve their original CLI."""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import urllib.error
from pathlib import Path

from agent_launcher import HARNESSES, HubReader, Stop, log_session, say
from agent_recovery import add_options, agent_lock, prepare, split_controls


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    add_options(parser)
    parser.add_argument("--agent", required=True)
    parser.add_argument("--harness", choices=HARNESSES, required=True)
    parser.add_argument("--hub-url", required=True)
    parser.add_argument("--token-file", type=Path, required=True)
    parser.add_argument("--sessions", type=Path, required=True)
    parser.add_argument("--resume-prompt", type=Path, required=True)
    parser.add_argument("--resume-session")
    parser.add_argument("--powershell", action="store_true")
    parser.add_argument("--shell-command", required=True)
    argv, controls = split_controls(None)
    args = parser.parse_args(argv)
    for key, value in controls.items():
        setattr(args, key, value)
    try:
        with agent_lock(args.sessions):
            conversation, _ = prepare(args, HubReader(args.hub_url, args.token_file), args.agent)
            say(
                f"Interactive recovery: read {args.resume_prompt} in the resumed conversation "
                "before taking action. This launch keeps interactive mode."
            )
            if args.powershell:
                shell = shutil.which("pwsh") or shutil.which("powershell")
                if shell is None:
                    raise OSError("PowerShell unavailable; restore the original run environment")
                command = [shell, "-NoProfile", "-Command", args.shell_command]
            else:
                command = ["bash", "-c", args.shell_command]
            child = subprocess.Popen(
                command, env=os.environ | {"ROBOMATE_RESUME_SESSION_ID": conversation}
            )
            fields = {"agent": args.agent, "harness": args.harness, "conversation_id": conversation}
            log_session(args.sessions, event="manual_resume", pid=child.pid, **fields)
            code = child.wait()
            log_session(args.sessions, event="exit", exit_code=code, **fields)
            return code
    except Stop as exc:
        say(str(exc))
        return exc.code
    except (OSError, ValueError, urllib.error.URLError) as exc:
        say(
            f"Recovery unavailable: {exc}. "
            "Restore the original run files/hub reachability and retry."
        )
        return 3


if __name__ == "__main__":
    sys.exit(main())
