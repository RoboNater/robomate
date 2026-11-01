"""Read-only operator projection and private, inert retrospective exports."""

from __future__ import annotations

import json
import logging
import os
import string
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
    """Printable single-line text, including visible escapes for hidden controls."""
    return "".join(f"\\u{ord(ch):04x}" if not ch.isprintable() else ch for ch in value)


def markdown(value: str) -> str:
    """Escape all ASCII punctuation, including URL schemes and raw HTML."""
    return "".join("\\" + ch if ch in string.punctuation else ch for ch in literal(value))


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
        author = literal(row["author"])
        if row["timing"] is not None:
            result["submitted"].append({"author": author, "timing": row["timing"]})
        else:
            reason = row["missing_reason"]
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
        lines.append(f"{key}: {markdown(row[key])}")
    lines.extend(["", "## Summary", "", markdown(report["summary"])])
    lines.extend(["", "## Outstanding items", ""])
    for item in report["outstanding_items"]:
        lines.append("- " + markdown(item["summary"]))
        for key in ("finding_id", "url", "reason"):
            if item.get(key):
                lines.append(f"  {key}: {markdown(item[key])}")
    lines.extend(["", "## Lessons", ""])
    lines.extend("- " + markdown(lesson) for lesson in report["lessons"])
    lines.extend(["", "## Claims (agent assertions; not hub-certified)", ""])
    for claim in report["claims"]:
        lines.append(f"- {claim['verification']}: {markdown(claim['text'])}")
        if claim.get("evidence"):
            lines.append("  Evidence: " + markdown(claim["evidence"]))
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
    """Best effort after commit; finalized rows remain the restart retry queue.

    Write the index last as the completion marker. Filenames are ordinal, never
    derived from authors, so even colliding or hostile author names stay inert.
    Every replacement is atomic and retrying the immutable collection is safe.
    """
    try:
        with database(store.path) as connection:
            connection.execute("BEGIN")
            run = connection.execute(
                "SELECT w.id FROM workflow w JOIN closeout_collection c ON c.workflow_id = w.id"
                " WHERE w.run_id = ? AND w.status = 'done' AND c.finalized_at IS NOT NULL",
                (run_id,),
            ).fetchone()
            if run is None:
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
            entry = {"author": literal(row["author"]), "author_kind": row["author_kind"]}
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
        store.closeout_export_errors.pop(run_id, None)
    except Exception as exc:
        # Keep filesystem paths and report contents out of the status error field.
        store.closeout_export_errors[run_id] = type(exc).__name__
        logger.exception("Closeout export failed for run %s; retry on hub restart", run_id)
