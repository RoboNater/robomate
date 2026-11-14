"""Local-only, short-lived projection of a launcher's permission journal."""

from __future__ import annotations

import json
import os
import tempfile
import time
from pathlib import Path
from typing import Any

from .registry import process_alive


def write_observation(directory: Path, agent: str, observation: dict[str, Any]) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=".permission-", dir=directory)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump({**observation, "observed_at": time.time()}, stream)
        os.replace(temporary, directory / f"{agent}.permission.json")
    finally:
        Path(temporary).unlink(missing_ok=True)


def permission_diagnostic(
    directory: Path | None, agent: str, hub_id: str, workflow_id: str | None
) -> str | None:
    if directory is None:
        return None
    try:
        with (directory / f"{agent}.permission.json").open("rb") as stream:
            data = stream.read(65537)
        if len(data) > 65536:
            return None
        row = json.loads(data)
        if not isinstance(row, dict) or row.get("hub_id") != hub_id:
            return None
        if row.get("workflow_id") != workflow_id or not row.get("launch_id"):
            return None
        age = time.time() - float(row.get("observed_at", 0))
        if not 0 <= age <= 5:
            return None
        if not process_alive(int(row.get("pid", 0))):
            return None
        # The projection is only an index. The journal establishes which child
        # is current, including replacements and exit before lease expiry.
        with Path(row["journal"]).open("rb") as stream:
            stream.seek(0, os.SEEK_END)
            end = stream.tell()
            stream.seek(max(0, end - 262144))
            lines = stream.read(262144).splitlines()
        current = None
        for line in reversed(lines):
            try:
                entry = json.loads(line)
            except (ValueError, UnicodeError):
                continue
            if isinstance(entry, dict) and entry.get("event") in {
                "start",
                "resume",
                "manual_resume",
                "exit",
                "stop",
            }:
                current = entry
                break
        if not current or current.get("launch_id") != row["launch_id"]:
            return None
        if current.get("event") in {"exit", "stop"}:
            return None
        for pending in row.get("pending", []):
            if pending.get("session") != row.get("session"):
                continue
            permission = pending["permission"]
            patterns = pending["patterns"]
            text = f"opencode permission ask pending: {permission} {' '.join(patterns)}"
            return "".join(c for c in text if c.isprintable())[:4096]
    except (OSError, ValueError, TypeError, KeyError, AttributeError):
        pass
    return None
