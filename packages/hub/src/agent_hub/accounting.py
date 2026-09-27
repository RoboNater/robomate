"""Per-call byte accounting persisted by the HTTP hub (#78).

Every A2A request a worker makes and every MCP row the bridge ships is
recorded as byte counts, never as payload text: issue bodies, questions and
results are untrusted, and the accounting must not become a second place they
land. The labels a record carries — actor, tool, outcome, task id — are either
drawn from closed sets or validated against the hub's own identifiers.

Off by default for the standalone app; `robomate up` enables it by default.
Each call becomes a row in `call_log`, so a per-agent tally is a
`GROUP BY actor` that survives a hub restart; `HUB_CALL_LOG_JSONL`
additionally appends the same record to a file.
Nothing here writes to stdout (#7), and a failure to record is logged to
stderr rather than breaking the call it describes.
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import AsyncIterable, AsyncIterator, Callable
from contextvars import ContextVar
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from agent_hub_common import MetaKeys
from agent_hub_common.clock import utcnow_iso

from .database import database

logger = logging.getLogger(__name__)

UNKNOWN_ACTOR = "unknown"

# Hub identifiers are uuid4 hex; anything else is not one of ours and is
# dropped rather than copied into the accounting.
_ID_RE = re.compile(r"^[0-9a-f]{32}$")


def hub_id(value: Any) -> str | None:
    """Return `value` if it is a hub-generated id, else None."""

    return value if isinstance(value, str) and _ID_RE.fullmatch(value) else None


@dataclass(slots=True)
class CallRecord:
    """One call's accounting: sizes and closed-set labels only."""

    boundary: str
    actor: str
    tool: str
    outcome: str
    bytes_in: int
    bytes_out: int
    started: str
    finished: str
    status: int | None = None
    content_bytes: int | None = None
    repeat_bytes: int | None = None
    task_id: str | None = None


@dataclass(slots=True)
class CallAccounting:
    """Persist call records to `call_log` and, optionally, a JSONL stream."""

    database_path: Path
    enabled: bool = False
    jsonl_path: Path | None = None

    def record(self, record: CallRecord) -> bool:
        if not self.enabled:
            return False
        fields = asdict(record)
        persisted = False
        try:
            with database(self.database_path) as connection:
                # One workflow per database (§3); calls before it exists have none.
                row = connection.execute(
                    "SELECT id FROM workflow ORDER BY created LIMIT 1"
                ).fetchone()
                fields["workflow_id"] = None if row is None else row["id"]
                columns = ", ".join(fields)
                placeholders = ", ".join(f":{name}" for name in fields)
                connection.execute(
                    f"INSERT INTO call_log ({columns}) VALUES ({placeholders})", fields
                )
                persisted = True
        except Exception:
            logger.exception("Could not record %s call %s", record.boundary, record.tool)
            fields.setdefault("workflow_id", None)
        if self.jsonl_path is None:
            return persisted
        try:
            self.jsonl_path.parent.mkdir(parents=True, exist_ok=True)
            with self.jsonl_path.open("a", encoding="utf-8") as stream:
                stream.write(json.dumps(fields, sort_keys=True) + "\n")
        except OSError:
            logger.exception("Could not append call accounting to %s", self.jsonl_path)
        return persisted


# -- A2A boundary -------------------------------------------------------------


@dataclass(slots=True)
class A2ACall:
    """What the protocol learns about the caller while serving one request."""

    tool: str
    started: str = field(default_factory=utcnow_iso)
    actor: str = UNKNOWN_ACTOR
    task_id: str | None = None
    outcome: str | None = None


_current_a2a: ContextVar[A2ACall | None] = ContextVar("current_a2a_call", default=None)


def begin_a2a(tool: str) -> A2ACall:
    call = A2ACall(tool=tool)
    _current_a2a.set(call)
    return call


def note_a2a(
    *, actor: str | None = None, task_id: str | None = None, outcome: str | None = None
) -> None:
    """Label the request being served; a no-op when accounting is off.

    `actor` must be a registered agent's name and `outcome` a closed-set label;
    the callers in `protocol` pass only those.
    """

    call = _current_a2a.get()
    if call is None:
        return
    if actor is not None:
        call.actor = actor
    if task_id is not None:
        call.task_id = hub_id(task_id)
    if outcome is not None:
        call.outcome = outcome


def a2a_tool(payload: Any) -> str:
    """Name the worker intent a JSON-RPC body carries, from a closed set.

    The names are worker-mcp's tool names, so hub and worker logs join on them.
    """

    if not isinstance(payload, dict):
        return "invalid"
    method = payload.get("method")
    if method in ("tasks/get", "tasks/cancel"):
        return str(method)
    params = payload.get("params")
    message = params.get("message") if isinstance(params, dict) else None
    if method not in ("message/send", "message/stream") or not isinstance(message, dict):
        return "invalid"
    metadata = message.get("metadata")
    kind = metadata.get(MetaKeys.KIND) if isinstance(metadata, dict) else None
    if method == "message/stream":
        return "ask_alice" if message.get("taskId") is not None else "await_assignment"
    if message.get("taskId") is None:
        return "heartbeat" if kind == "heartbeat" else "check_in"
    return "submit_result" if kind == "result" else "report_progress"


async def counted_stream(
    body: AsyncIterable[str | bytes | memoryview], on_done: Callable[[int], None]
) -> AsyncIterator[str | bytes | memoryview]:
    """Pass a streaming body through, reporting its size once it ends."""

    sent = 0
    try:
        async for chunk in body:
            sent += len(chunk.encode("utf-8")) if isinstance(chunk, str) else len(chunk)
            yield chunk
    finally:
        on_done(sent)
