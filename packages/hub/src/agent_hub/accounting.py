"""Per-call byte accounting on the hub's two boundaries (#78).

Every A2A request a worker makes and every MCP message Alice exchanges is
recorded as byte counts, never as payload text: issue bodies, questions and
results are untrusted, and the accounting must not become a second place they
land. The labels a record carries — actor, tool, outcome, task id — are either
drawn from closed sets or validated against the hub's own identifiers.

Off by default. When `HUB_CALL_ACCOUNTING` is on, each call becomes a row in
`call_log`, so a per-agent tally is a `GROUP BY actor` that survives a hub
restart; `HUB_CALL_LOG_JSONL` additionally appends the same record to a file.
Nothing here writes to stdout (#7), and a failure to record is logged to
stderr rather than breaking the call it describes.
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import AsyncIterable, AsyncIterator, Callable, Mapping
from contextvars import ContextVar
from dataclasses import asdict, dataclass, field
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any

from agent_hub_common import MetaKeys
from agent_hub_common.clock import utcnow_iso

from .database import database

logger = logging.getLogger(__name__)

UNKNOWN_ACTOR = "unknown"
ALICE = "alice"

# Hub identifiers are uuid4 hex; anything else is not one of ours and is
# dropped rather than copied into the accounting.
_ID_RE = re.compile(r"^[0-9a-f]{32}$")

# MCP methods other than `tools/call` are recorded under their own name when
# they are one of these, since `tools/list` is the per-session schema cost.
MCP_METHODS = frozenset({"initialize", "tools/list", "ping", "resources/list", "prompts/list"})


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

    def record(self, record: CallRecord) -> None:
        if not self.enabled:
            return
        fields = asdict(record)
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
        except Exception:
            logger.exception("Could not record %s call %s", record.boundary, record.tool)
            fields.setdefault("workflow_id", None)
        if self.jsonl_path is None:
            return
        try:
            self.jsonl_path.parent.mkdir(parents=True, exist_ok=True)
            with self.jsonl_path.open("a", encoding="utf-8") as stream:
                stream.write(json.dumps(fields, sort_keys=True) + "\n")
        except OSError:
            logger.exception("Could not append call accounting to %s", self.jsonl_path)


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


# -- MCP boundary -------------------------------------------------------------


@dataclass(slots=True)
class _PendingMcp:
    tool: str
    bytes_in: int
    started: str
    task_id: str | None


@dataclass(slots=True)
class McpAccounting:
    """Pair Alice's JSON-RPC requests with the responses the hub writes back.

    Measured on the stdio framing itself, so `bytes_in`/`bytes_out` are exactly
    what crossed the pipe. `content_bytes` is the text content a harness puts
    into the model's context, and `repeat_bytes` how much of it repeats the
    previous result of the same tool, line for line. The previous text is held
    in memory only for that comparison and never recorded.
    """

    accounting: CallAccounting
    tools: frozenset[str]
    _pending: dict[str | int, _PendingMcp] = field(default_factory=dict)
    _previous: dict[str, list[str]] = field(default_factory=dict)

    def observe_request(self, payload: Mapping[str, Any], size: int) -> None:
        request_id = payload.get("id")
        method = payload.get("method")
        if not isinstance(request_id, str | int) or not isinstance(method, str):
            return
        params = payload.get("params")
        params = params if isinstance(params, Mapping) else {}
        task_id = None
        if method == "tools/call":
            name = params.get("name")
            tool = name if isinstance(name, str) and name in self.tools else "unknown"
            arguments = params.get("arguments")
            if isinstance(arguments, Mapping):
                task_id = hub_id(arguments.get("task_id"))
        else:
            tool = method if method in MCP_METHODS else "other"
        self._pending[request_id] = _PendingMcp(tool, size, utcnow_iso(), task_id)

    def observe_response(self, payload: Mapping[str, Any], size: int) -> None:
        request_id = payload.get("id")
        if not isinstance(request_id, str | int) or "method" in payload:
            return
        pending = self._pending.pop(request_id, None)
        if pending is None:
            return
        result = payload.get("result")
        outcome = "error"
        content_bytes = repeat_bytes = None
        task_id = pending.task_id
        if isinstance(result, Mapping):
            structured = result.get("structuredContent")
            outcome = _mcp_outcome(pending.tool, result, structured)
            if pending.tool == "assign_task" and isinstance(structured, Mapping):
                task_id = hub_id(structured.get("id")) or task_id
            text = _content_text(result)
            if text is not None:
                content_bytes = len(text.encode("utf-8"))
                repeat_bytes = self._repeat(pending.tool, text)
        self.accounting.record(
            CallRecord(
                boundary="mcp",
                actor=ALICE,
                tool=pending.tool,
                outcome=outcome,
                bytes_in=pending.bytes_in,
                bytes_out=size,
                started=pending.started,
                finished=utcnow_iso(),
                content_bytes=content_bytes,
                repeat_bytes=repeat_bytes,
                task_id=task_id,
            )
        )

    def _repeat(self, tool: str, text: str) -> int:
        lines = text.splitlines(keepends=True)
        previous = self._previous.get(tool)
        self._previous[tool] = lines
        if previous is None:
            return 0
        matcher = SequenceMatcher(None, previous, lines, autojunk=False)
        return sum(
            len("".join(lines[block.b : block.b + block.size]).encode("utf-8"))
            for block in matcher.get_matching_blocks()
        )


def _content_text(result: Mapping[str, Any]) -> str | None:
    content = result.get("content")
    if not isinstance(content, list):
        return None
    texts = [
        block["text"]
        for block in content
        if isinstance(block, Mapping) and isinstance(block.get("text"), str)
    ]
    return "".join(texts) if texts else None


def _mcp_outcome(tool: str, result: Mapping[str, Any], structured: Any) -> str:
    if result.get("isError") is True:
        return "error"
    if tool == "wait_for_event" and isinstance(structured, Mapping):
        return "null_event" if structured.get("event") is None else "event"
    return "ok"
