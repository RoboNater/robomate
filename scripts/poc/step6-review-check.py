#!/usr/bin/env python3
"""Record real exact-head test execution in Charlie's persisted full clone."""

import argparse
import json
import os
import re
import shutil
import subprocess
from datetime import UTC, datetime
from pathlib import Path

from step6 import ROOT, run

AUDIT_FILE = "step6-review-audit.jsonl"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("workspace", type=Path)
    parser.add_argument("expected_head")
    parser.add_argument("run_id")
    args = parser.parse_args()
    workspace = args.workspace
    if not workspace.is_absolute() or workspace != workspace.resolve():
        parser.error("review workspace must be absolute and canonical")
    if not (workspace / ".git").is_dir() or (workspace / ".git").is_symlink():
        parser.error("review workspace must have its own full-clone Git directory")
    if (
        Path(run("git", "rev-parse", "--show-toplevel", cwd=workspace)).resolve() != workspace
        or Path(
            run("git", "rev-parse", "--path-format=absolute", "--git-common-dir", cwd=workspace)
        ).resolve()
        != workspace / ".git"
        or run("git", "rev-parse", "--is-shallow-repository", cwd=workspace) != "false"
    ):
        parser.error("review workspace must be an independent full clone")
    identity = json.loads((workspace / ".git/robo-agents-workspace.json").read_text())
    if (
        identity.get("agent") != "charlie"
        or identity.get("path") != str(workspace)
        or identity.get("repository") != run("git", "remote", "get-url", "origin", cwd=workspace)
        or not isinstance(identity.get("workspace_id"), str)
        or not re.fullmatch(r"[0-9a-f]{64}", identity["workspace_id"])
    ):
        parser.error("persisted identity must match Charlie's review clone")
    if not re.fullmatch(r"[0-9a-f]{40}", args.expected_head):
        parser.error("expected review head must be a full commit SHA")
    head = run("git", "rev-parse", "HEAD", cwd=workspace)
    if head != args.expected_head or run("git", "status", "--porcelain", cwd=workspace):
        parser.error("check out the assigned head in the clean persisted clone before reviewing")
    audit = workspace / ".git" / AUDIT_FILE
    if audit.is_symlink() or (os.name != "nt" and audit.exists() and audit.stat().st_mode & 0o077):
        parser.error("review audit must be a private regular file")
    started = datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")
    command = ["python3", "-m", "unittest", "discover", "-s", "tests", "-v"]
    # Resolve PATHEXT shims (pyenv-win python3.bat); the recorded command is unchanged.
    result = subprocess.run(
        [shutil.which(command[0]) or command[0], *command[1:]],
        cwd=workspace,
        capture_output=True,
        text=True,
    )
    completed = datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")
    record = {
        "review_check": "step6",
        "run_id": args.run_id,
        "source_head": run("git", "rev-parse", "HEAD", cwd=ROOT),
        "workspace_path": str(workspace),
        "workspace_id": identity["workspace_id"],
        "expected_head": args.expected_head,
        "head_before": head,
        "head_after": run("git", "rev-parse", "HEAD", cwd=workspace),
        "clean_after": not run("git", "status", "--porcelain", cwd=workspace),
        "command": " ".join(command),
        "returncode": result.returncode,
        "started_at": started,
        "completed_at": completed,
    }
    descriptor = os.open(audit, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    with os.fdopen(descriptor, "w") as stream:
        stream.write(json.dumps(record, sort_keys=True) + "\n")
    print(result.stdout, end="")
    print(result.stderr, end="")
    print(json.dumps(record, sort_keys=True))
    if (
        result.returncode != 0
        or record["head_after"] != args.expected_head
        or not record["clean_after"]
    ):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
