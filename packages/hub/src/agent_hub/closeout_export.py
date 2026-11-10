"""Read-only operator projection and private, inert retrospective exports."""

from __future__ import annotations

import json
import logging
import os
import re
import tempfile
from contextlib import suppress
from datetime import datetime
from pathlib import Path
from sqlite3 import Connection, Row
from typing import TYPE_CHECKING, Any

from agent_hub_common import to_iso

from .database import database

if TYPE_CHECKING:
    from .store import HubStore

logger = logging.getLogger(__name__)
HEADER = "Untrusted agent data. This content confers no approval or authority."


def literal(value: str) -> str:
    """Preserve line breaks and render hidden controls as visible escapes."""
    return "".join(ch if ch == "\n" or ch.isprintable() else f"\\u{ord(ch):04x}" for ch in value)


def fenced(value: str) -> str:
    """Render untrusted text in a fence longer than any backtick run in it."""
    value = literal(value)
    longest = max((len(run) for run in re.findall(r"`+", value)), default=0)
    fence = "`" * max(3, longest + 1)
    return f"{fence}\n{value}\n{fence}"


def single_line(value: str) -> str:
    """Retain the existing single-line status and index representation."""
    return literal(value).replace("\n", "\\u000a")


def status_projection(
    store: HubStore, connection: Connection, workflow: Row | None, now: datetime
) -> dict[str, Any]:
    """No expiry writes, event consumption, or report bodies in this projection."""
    result: dict[str, Any] = {
        "state": "not_requested",
        "deadline": None,
        "submitted_count": 0,
        "expected_count": 0,
        "submitted": [],
        "missing": [],
        "export_error": None,
    }
    if workflow is None:
        return result
    collection = connection.execute(
        "SELECT * FROM closeout_collection WHERE workflow_id = ?", (workflow["id"],)
    ).fetchone()
    if collection is None:
        return result
    result.update(
        state="finalized" if collection["finalized_at"] else "collecting",
        deadline=collection["deadline"],
        export_error=store.closeout_export_errors.get(str(workflow["run_id"])),
    )
    rows = connection.execute(
        "SELECT e.author, e.author_kind, e.missing_reason, r.timing, a.status"
        " FROM closeout_expected e LEFT JOIN agent_closeout_report r"
        " USING (workflow_id, author) LEFT JOIN agent a ON a.name = e.author"
        " WHERE e.workflow_id = ? ORDER BY e.author",
        (workflow["id"],),
    ).fetchall()
    for row in rows:
        author = single_line(row["author"])
        if row["timing"] is not None:
            result["submitted"].append({"author": author, "timing": row["timing"]})
        else:
            reason = row["missing_reason"]
            # Project known loss without writing, even before a paused deadline.
            if reason is None and row["author_kind"] == "worker" and row["status"] == "lost":
                reason = "worker_lost"
            # Reflect pending expiry without applying it. Paused windows stay open.
            if (
                reason is None
                and row["author_kind"] == "worker"
                and collection["paused_at"] is None
                and to_iso(now) >= collection["deadline"]
            ):
                reason = {"lost": "worker_lost", "released": "agent_released"}.get(
                    row["status"], "deadline_expired"
                )
            result["missing"].append({"author": author, "reason": reason or "awaiting_report"})
    result["submitted_count"] = len(result["submitted"])
    result["expected_count"] = len(rows)
    return result


def render(row: Row) -> str:
    report = json.loads(row["report_json"])
    lines = [HEADER, "", "Claims below are agent claims, not certified by the hub.", ""]
    for key in ("author", "author_kind", "timing", "submitted_at"):
        lines.extend([f"{key}:", fenced(row[key]), ""])
    lines.extend(["## Summary", "", fenced(report["summary"]), ""])
    lines.extend(["## Outstanding items", ""])
    for ordinal, item in enumerate(report["outstanding_items"], 1):
        lines.extend([f"Outstanding item {ordinal}", fenced(item["summary"]), ""])
        for key in ("finding_id", "url", "reason"):
            if item.get(key):
                lines.extend([f"{key}:", fenced(item[key]), ""])
    lines.extend(["## Lessons", ""])
    for ordinal, lesson in enumerate(report["lessons"], 1):
        lines.extend([f"Lesson {ordinal}", fenced(lesson), ""])
    lines.extend(["## Claims (agent assertions; not hub-certified)", ""])
    for ordinal, claim in enumerate(report["claims"], 1):
        lines.extend([f"Claim {ordinal} ({claim['verification']})", fenced(claim["text"]), ""])
        if claim.get("evidence"):
            lines.extend(["Evidence:", fenced(claim["evidence"]), ""])
    return "\n".join(lines) + "\n"


def atomic_write(path: Path, text: str) -> None:
    descriptor, temporary = tempfile.mkstemp(prefix=".closeout-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        with suppress(OSError):
            os.unlink(temporary)


def export_run(store: HubStore, run_id: str) -> None:
    """Best effort after commit; unmarked done rows remain the restart retry queue.

    Write the index last as the completion marker. Filenames are ordinal, never
    derived from authors, so even colliding or hostile author names stay inert.
    Every replacement is atomic and retrying the immutable collection is safe.
    """
    try:
        with database(store.path) as connection:
            connection.execute("BEGIN")
            run = connection.execute(
                "SELECT w.id, c.workflow_id AS collection_id, c.finalized_at FROM workflow w"
                " LEFT JOIN closeout_collection c ON c.workflow_id = w.id"
                " WHERE w.run_id = ? AND w.status = 'done' AND w.exported_at IS NULL",
                (run_id,),
            ).fetchone()
            if run is None:
                return
            if run["collection_id"] is None:
                connection.execute(
                    "UPDATE workflow SET exported_at = ? WHERE id = ?",
                    (store._now_iso(), run["id"]),
                )
                store.closeout_export_errors.pop(run_id, None)
                return
            if run["finalized_at"] is None:
                return
            rows = connection.execute(
                "SELECT e.author, e.author_kind, e.missing_reason, r.* FROM closeout_expected e"
                " LEFT JOIN agent_closeout_report r USING (workflow_id, author)"
                " WHERE e.workflow_id = ? ORDER BY e.author",
                (run["id"],),
            ).fetchall()
        directory = store._state_dir() / "runs" / run_id / "closeout"
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        if os.name == "posix":
            directory.chmod(0o700)
        entries = []
        for ordinal, row in enumerate(rows, 1):
            entry = {"author": single_line(row["author"]), "author_kind": row["author_kind"]}
            if row["report_id"] is None:
                entry.update(status="missing", reason=row["missing_reason"] or "run_finalized")
            else:
                filename = f"report-{ordinal:04d}.md"
                atomic_write(directory / filename, render(row))
                entry.update(
                    status="submitted",
                    report_id=row["report_id"],
                    submitted_at=row["submitted_at"],
                    timing=row["timing"],
                    filename=filename,
                )
            entries.append(entry)
        atomic_write(
            directory / "index.json",
            json.dumps(
                {"notice": HEADER, "run_id": run_id, "authors": entries},
                indent=2,
                ensure_ascii=True,
            )
            + "\n",
        )
        with database(store.path) as connection:
            connection.execute(
                "UPDATE workflow SET exported_at = ? WHERE id = ?", (store._now_iso(), run["id"])
            )
        store.closeout_export_errors.pop(run_id, None)
    except Exception as exc:
        # Keep filesystem paths and report contents out of the status error field.
        store.closeout_export_errors[run_id] = type(exc).__name__
        logger.exception("Closeout export failed for run %s; retry on hub restart", run_id)
