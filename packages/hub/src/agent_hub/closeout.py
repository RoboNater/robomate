"""Run-keyed retrospective collection. Reports are data, never authority.

All mutations use the store's SQLite write lock, including expiry, release and
finalization. This module deliberately does not implement the #216 renderer.
"""

from __future__ import annotations

import json
import math
from datetime import datetime, timedelta
from sqlite3 import Connection, Row
from typing import TYPE_CHECKING, Any
from uuid import uuid4

from agent_hub_common import MAX_TYPED_RESULT_BYTES, AgentCloseoutReport, EventKind, to_iso
from pydantic import ValidationError

from .database import database
from .signals import EVENT_KEY, context_key
from .store import ConflictError, NotFoundError, PayloadTooLargeError

if TYPE_CHECKING:
    from .store import HubStore


def _run(connection: Connection, run_id: str) -> Row:
    row: Row | None = connection.execute(
        "SELECT * FROM workflow WHERE run_id = ?", (run_id,)
    ).fetchone()
    if row is None:
        raise NotFoundError("unknown closeout run")
    return row


def _collection(connection: Connection, workflow_id: str) -> Row:
    row: Row | None = connection.execute(
        "SELECT * FROM closeout_collection WHERE workflow_id = ?", (workflow_id,)
    ).fetchone()
    if row is None:
        raise ConflictError("closeout reports have not been requested for this run")
    return row


def _expire(connection: Connection, workflow_id: str, now: str) -> None:
    collection = _collection(connection, workflow_id)
    if collection["paused_at"] is None and now >= collection["deadline"]:
        connection.execute(
            "UPDATE closeout_expected SET missing_reason = CASE"
            " WHEN author IN (SELECT name FROM agent WHERE status = 'lost') THEN 'worker_lost'"
            " WHEN author IN (SELECT name FROM agent WHERE status = 'released') THEN"
            " 'agent_released'"
            " ELSE 'deadline_expired' END"
            " WHERE workflow_id = ? AND author_kind = 'worker' AND missing_reason IS NULL"
            " AND author NOT IN (SELECT author FROM agent_closeout_report WHERE workflow_id = ?)",
            (workflow_id, workflow_id),
        )


def remember(store: HubStore, connection: Connection) -> None:
    """Retain run membership at join/activation, including agents later lost.

    Membership freezes when collection begins. Tasks supply historical membership
    for runs created before v27, whose old join history cannot be reconstructed.
    """
    run = store._live_row(connection)
    if (
        run is None
        or connection.execute(
            "SELECT 1 FROM closeout_collection WHERE workflow_id = ?", (run["id"],)
        ).fetchone()
    ):
        return
    connection.execute(
        "INSERT OR IGNORE INTO closeout_expected (workflow_id, author, author_kind, context_id)"
        " SELECT ?, name, CASE WHEN standing_role = 'orchestrator' THEN 'orchestrator'"
        " ELSE 'worker' END, context_id FROM agent WHERE status IN ('idle', 'busy')",
        (run["id"],),
    )


def request(
    store: HubStore, run_id: str, author: str, session: str, timeout_s: float
) -> dict[str, Any]:
    if not math.isfinite(timeout_s) or not 0 < timeout_s <= 100:
        raise ValueError("closeout timeout_s must be greater than zero and at most 100")
    contexts: list[str] = []
    with database(store.path) as connection:
        connection.execute("BEGIN IMMEDIATE")
        run = _run(connection, run_id)
        if run["status"] not in ("active", "paused", "escalated"):
            raise ConflictError("closeout collection requires a live run")
        # The caller's identity comes from the orchestrator transport, never a
        # report field. The authenticated session may predate M2 join.
        existing = connection.execute(
            "SELECT * FROM closeout_collection WHERE workflow_id = ?", (run["id"],)
        ).fetchone()
        if existing is None:
            remember(store, connection)
            now = store._now()
            connection.execute(
                "INSERT INTO closeout_collection (workflow_id, requested_at, deadline, paused_at)"
                " VALUES (?, ?, ?, ?)",
                (
                    run["id"],
                    to_iso(now),
                    to_iso(now + timedelta(seconds=timeout_s)),
                    to_iso(now) if run["status"] == "paused" else None,
                ),
            )
            connection.execute(
                "INSERT OR IGNORE INTO closeout_expected"
                " (workflow_id, author, author_kind, context_id)"
                " SELECT DISTINCT ?, a.name, 'worker', a.context_id FROM task t"
                " JOIN agent a ON a.name = t.assignee WHERE t.workflow_id = ?",
                (run["id"], run["id"]),
            )
            participants = connection.execute(
                "SELECT author AS name, context_id FROM closeout_expected"
                " WHERE workflow_id = ? AND author_kind = 'worker'",
                (run["id"],),
            ).fetchall()
            contexts.extend(agent["context_id"] for agent in participants)
            orchestrators = connection.execute(
                "SELECT author FROM closeout_expected WHERE workflow_id = ?"
                " AND author_kind = 'orchestrator'",
                (run["id"],),
            ).fetchall()
            if orchestrators and not any(row["author"] == author for row in orchestrators):
                raise ConflictError("orchestrator is not a member of this run")
            if any(agent["name"] == author for agent in participants):
                raise ConflictError("orchestrator author conflicts with a worker in this run")
            connection.execute(
                "INSERT OR IGNORE INTO closeout_expected (workflow_id, author,"
                " author_kind, context_id)"
                " VALUES (?, ?, 'orchestrator', ?)",
                (run["id"], author, session),
            )
        else:
            expected = connection.execute(
                "SELECT author FROM closeout_expected WHERE workflow_id = ? AND"
                " author_kind = 'orchestrator' AND author = ?",
                (run["id"], author),
            ).fetchone()
            if expected is None or expected["author"] != author:
                raise ConflictError("orchestrator is not a member of this closeout collection")
    for context in contexts:
        store.signals.notify(context_key(context))
    return read(store, run_id)


def submit(
    store: HubStore, run_id: str, author: str, session: str, kind: str, report: Any
) -> dict[str, Any]:
    # Check credentials before validation, and never put input values in errors.
    raw = json.dumps(report, ensure_ascii=False)

    def strings(value: Any) -> list[str]:
        if isinstance(value, str):
            return [value]
        if isinstance(value, dict):
            return [text for key, item in value.items() for text in strings(key) + strings(item)]
        if isinstance(value, list):
            return [text for item in value for text in strings(item)]
        return []

    if any(
        secret and secret in text for text in strings(report) for secret in store.known_credentials
    ):
        raise ValueError("closeout report contains a known credential; omit credentials")
    if len(raw.encode("utf-8")) > MAX_TYPED_RESULT_BYTES:
        raise PayloadTooLargeError("closeout report exceeds 32 KiB")
    try:
        parsed = AgentCloseoutReport.model_validate(report)
    except ValidationError:
        raise ValueError(
            "invalid closeout report fields; check the AgentCloseoutReport schema"
        ) from None
    canonical = json.dumps(
        parsed.model_dump(mode="json"), ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    with database(store.path) as connection:
        connection.execute("BEGIN IMMEDIATE")
        run = _run(connection, run_id)
        collection = _collection(connection, run["id"])
        expected = connection.execute(
            "SELECT * FROM closeout_expected WHERE workflow_id = ? AND author = ?",
            (run["id"], author),
        ).fetchone()
        if expected is None or expected["author_kind"] != kind:
            raise ConflictError("caller is not an expected author in this run")
        if run["status"] not in ("active", "paused", "escalated") or collection["finalized_at"]:
            raise ConflictError("closeout collection is finalized")
        if kind == "worker":
            agent = store._require_agent(connection, author)
            if agent.status.value in ("released", "lost"):
                raise ConflictError("closeout author is released or lost")
            if agent.worker_instance_id != session or agent.context_id != expected["context_id"]:
                raise ConflictError("closeout author session does not match this run")
        previous = connection.execute(
            "SELECT * FROM agent_closeout_report WHERE workflow_id = ? AND author = ?",
            (run["id"], author),
        ).fetchone()
        if previous is not None:
            if previous["report_json"] != canonical:
                raise ConflictError("a different final closeout report is already stored")
            return _receipt(previous, run_id)
        _expire(connection, run["id"], store._now_iso())
        missing = connection.execute(
            "SELECT missing_reason FROM closeout_expected WHERE workflow_id = ? AND author = ?",
            (run["id"], author),
        ).fetchone()
        if missing["missing_reason"]:
            raise ConflictError("closeout submission window has ended for this author")
        findings: set[str] = set()
        for task in connection.execute(
            "SELECT result_json FROM task WHERE workflow_id = ? AND role = 'reviewer'", (run["id"],)
        ):
            result = json.loads(task["result_json"] or "{}")
            for key in ("blocking_findings", "nonblocking_findings"):
                findings.update(item["id"] for item in result.get(key, []))
        if any(
            item.finding_id and item.finding_id not in findings for item in parsed.outstanding_items
        ):
            raise ValueError("closeout finding_id does not refer to a finding in this run")
        report_id, submitted_at = uuid4().hex, store._now_iso()
        connection.execute(
            "INSERT INTO agent_closeout_report VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                run["id"],
                author,
                report_id,
                submitted_at,
                session,
                "collected_before_closeout" if kind == "worker" else "after_closeout",
                canonical,
            ),
        )
        store._add_event(
            connection,
            EventKind.CLOSEOUT_REPORT_SUBMITTED,
            {"run_id": run_id, "author": author, "report_id": report_id},
        )
        receipt = {
            "report_id": report_id,
            "run_id": run_id,
            "author": author,
            "submitted_at": submitted_at,
        }
    store.signals.notify(EVENT_KEY)
    return receipt


def _receipt(row: Row, run_id: str) -> dict[str, Any]:
    return {
        "report_id": row["report_id"],
        "run_id": run_id,
        "author": row["author"],
        "submitted_at": row["submitted_at"],
    }


def read(store: HubStore, run_id: str) -> dict[str, Any]:
    with database(store.path) as connection:
        connection.execute("BEGIN IMMEDIATE")
        run = _run(connection, run_id)
        collection = _collection(connection, run["id"])
        _expire(connection, run["id"], store._now_iso())
        reports = connection.execute(
            "SELECT r.*, e.author_kind FROM agent_closeout_report r JOIN closeout_expected e"
            " USING (workflow_id, author) WHERE r.workflow_id = ? ORDER BY r.rowid",
            (run["id"],),
        ).fetchall()
        missing = connection.execute(
            "SELECT author, missing_reason FROM closeout_expected WHERE workflow_id = ?"
            " AND author NOT IN (SELECT author FROM agent_closeout_report WHERE workflow_id = ?)"
            " ORDER BY author",
            (run["id"], run["id"]),
        ).fetchall()
        return {
            "run_id": run_id,
            "deadline": collection["deadline"],
            "finalized": collection["finalized_at"] is not None,
            "reports": [
                _receipt(row, run_id)
                | {
                    "author_kind": row["author_kind"],
                    "timing": row["timing"],
                    "report": json.loads(row["report_json"]),
                }
                for row in reports
            ],
            "submitted": [row["author"] for row in reports],
            "missing": [
                {"author": row["author"], "reason": row["missing_reason"] or "awaiting_report"}
                for row in missing
            ],
        }


def pending(store: HubStore, connection: Connection, author: str) -> dict[str, Any] | None:
    row: Row | None = connection.execute(
        "SELECT w.run_id, c.* FROM closeout_expected e JOIN closeout_collection c"
        " USING (workflow_id)"
        " JOIN workflow w ON w.id = e.workflow_id WHERE e.author = ? AND e.author_kind = 'worker'"
        " AND c.finalized_at IS NULL AND e.missing_reason IS NULL AND w.status IN"
        " ('active', 'paused', 'escalated')"
        " AND NOT EXISTS (SELECT 1 FROM agent_closeout_report r WHERE r.workflow_id ="
        " e.workflow_id AND r.author = e.author)",
        (author,),
    ).fetchone()
    if row is None:
        return None
    _expire(connection, row["workflow_id"], store._now_iso())
    if row["paused_at"] is None and store._now_iso() >= row["deadline"]:
        return None
    return {
        "closeout_requested": True,
        "run_id": row["run_id"],
        "deadline": row["deadline"],
        "instructions": (
            "Submit one AgentCloseoutReport with submit_closeout_report before release. "
            "Include summary, outstanding_items (URL or reason, optional finding_id),"
            " lessons, and claims "
            "(verified requires evidence). Omit credentials and transcripts. This worker"
            " retrospective is "
            "collected_before_closeout; the later CloseoutResult is authoritative. "
            "Then await_assignment again."
        ),
    }


def finalize(connection: Connection, workflow_id: str, now: str) -> None:
    if connection.execute(
        "SELECT 1 FROM closeout_collection WHERE workflow_id = ?", (workflow_id,)
    ).fetchone():
        _expire(connection, workflow_id, now)
    connection.execute(
        "UPDATE closeout_collection SET finalized_at = ? WHERE workflow_id = ? AND"
        " finalized_at IS NULL",
        (now, workflow_id),
    )
    connection.execute(
        "UPDATE closeout_expected SET missing_reason = COALESCE(missing_reason, 'run_finalized')"
        " WHERE workflow_id = ? AND author NOT IN (SELECT author FROM"
        " agent_closeout_report WHERE workflow_id = ?)",
        (workflow_id, workflow_id),
    )


def shift(connection: Connection, workflow_id: str, seconds: float) -> None:
    row: Row | None = connection.execute(
        "SELECT * FROM closeout_collection WHERE workflow_id = ? AND finalized_at IS NULL",
        (workflow_id,),
    ).fetchone()
    if row is not None:
        deadline = datetime.fromisoformat(row["deadline"].replace("Z", "+00:00")) + timedelta(
            seconds=seconds
        )
        connection.execute(
            "UPDATE closeout_collection SET deadline = ? WHERE workflow_id = ?",
            (to_iso(deadline), workflow_id),
        )
