"""Local-only, short-lived projection of a launcher's permission journal."""

from __future__ import annotations

import json
import os
import tempfile
import time
from collections.abc import Callable
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


class ObservationPublisher:
    """Publish state changes and a one-second lease heartbeat while asks are pending."""

    def __init__(
        self,
        directory: Path,
        agent: str,
        report: Callable[[str], None],
        *,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.directory, self.agent, self.report, self.clock = directory, agent, report, clock
        self.pending: list[dict[str, Any]] = []
        self.deadline = 0.0
        self.failed = False
        self.attempted = False

    def _write(self, observation: dict[str, Any]) -> None:
        self.attempted = True
        try:
            write_observation(self.directory, self.agent, observation)
        except OSError as exc:
            if not self.failed:
                self.report(f"permission observation unavailable: {type(exc).__name__}")
            self.failed = True
        else:
            self.failed = False

    def publish(self, observation: dict[str, Any]) -> None:
        pending = observation.get("pending", [])
        changed = pending != self.pending
        now = self.clock()
        if not pending and not self.pending:
            return
        if not changed and now < self.deadline:
            return
        self.pending = list(pending)
        self.deadline = now + 1.0
        self._write(observation)

    def close(self) -> None:
        if self.attempted:
            self._write({})


def permission_diagnostic(
    directory: Path | None, agent: str, hub_id: str, workflow_id: str | None
) -> str | None:
    if directory is None or not agent or "/" in agent or "\\" in agent or agent in (".", ".."):
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
        journal_pending = None
        journal_seen = False
        for line in reversed(lines):
            try:
                entry = json.loads(line)
            except (ValueError, UnicodeError):
                continue
            if (
                isinstance(entry, dict)
                and entry.get("event") == "permission_observation"
                and entry.get("launch_id") == row["launch_id"]
                and not journal_seen
            ):
                journal_seen = True
                journal_pending = entry.get("pending")
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
        if journal_pending != row.get("pending"):
            # A transient projection replace failure must not resurrect an ask
            # which the journal has already resolved.
            return None
        for pending in row.get("pending", []):
            if pending.get("session") != row.get("session"):
                continue
            permission = pending["permission"]
            patterns = pending["patterns"]
            text = f"opencode permission ask pending: {permission} {' '.join(patterns)}"
            return "".join(c for c in text if c.isprintable())[:4096]
    except (OSError, ValueError, TypeError, KeyError, AttributeError, OverflowError):
        pass
    return None


def sandbox_override_diagnostic(
    directory: Path | None, agent: str, hub_id: str, workflow_id: str | None
) -> str | None:
    """Return a warning when ``agent``'s latest launch for this run overrode sandbox preflight."""

    if directory is None or not agent or "/" in agent or "\\" in agent or agent in (".", ".."):
        return None
    try:
        with (directory / f"{agent}.jsonl").open("rb") as stream:
            stream.seek(0, os.SEEK_END)
            end = stream.tell()
            stream.seek(max(0, end - 262144))
            lines = stream.read(262144).splitlines()
        seen_hub: str | None = None
        seen_workflow: str | None = None
        for line in reversed(lines):
            try:
                entry = json.loads(line)
            except (ValueError, UnicodeError):
                continue
            if not isinstance(entry, dict):
                continue
            event = entry.get("event")
            if event not in {"start", "resume", "manual_resume"}:
                if seen_hub is None and isinstance(entry.get("hub_id"), str):
                    seen_hub = entry["hub_id"]
                if seen_workflow is None and isinstance(entry.get("workflow_id"), str):
                    seen_workflow = entry["workflow_id"]
                continue
            row_hub = entry.get("hub_id") or seen_hub
            row_workflow = entry.get("workflow_id") or seen_workflow
            if hub_id and row_hub != hub_id:
                return None
            if workflow_id is not None and row_workflow != workflow_id:
                return None
            if entry.get("sandbox_check") != "overridden":
                return None
            failure = entry.get("sandbox_check_failure")
            detail = f": {failure.strip()}" if isinstance(failure, str) and failure.strip() else ""
            text = f"sandbox preflight overridden (running without proper sandboxing){detail}"
            return "".join(c for c in text if c.isprintable())[:4096]
    except (OSError, ValueError, TypeError, KeyError, AttributeError):
        pass
    return None
