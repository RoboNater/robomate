"""The `/rpc` JSON-RPC route: orchestrator operations and `hub.*` operator methods (§8)."""

from __future__ import annotations

import inspect
import logging
import math
import uuid
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from fastapi import BackgroundTasks
from pydantic import ValidationError, validate_call
from pydantic_core import to_jsonable_python

from .accounting import CallAccounting, CallRecord, hub_id
from .merge_gate import MergeGateError
from .orchestrator import OPERATIONS, OrchestratorOps
from .store import ConflictError, InvalidPolicyError, NotFoundError, PayloadTooLargeError

logger = logging.getLogger(__name__)

# Self-declared by the orchestrator bridge (§12 threat model): they identify a
# session for the one-orchestrator rule and accounting, never authorize.
ACTOR_HEADER = "X-Robomate-Actor"
SESSION_HEADER = "X-Robomate-Session"
MAX_ACTOR_LENGTH = 128

# JSON-RPC 2.0 codes, then this route's server errors. The codes are stable;
# the message is the original error text, so the bridge can hand Alice the
# same tool error she saw over stdio.
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
INTERNAL_ERROR = -32603
NOT_FOUND = -32001
CONFLICT = -32002
PAYLOAD_TOO_LARGE = -32003
MERGE_GATE_UNAVAILABLE = -32004


class RpcError(Exception):
    def __init__(self, code: int, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True, slots=True)
class OrchestratorSession:
    """The latest orchestrator session seen on `/rpc`; held in memory only."""

    actor: str
    session: str
    last_seen: datetime


def parse_caller(headers: Mapping[str, str]) -> tuple[str, str] | None:
    """Return (actor, session) from the headers, or None when neither is sent."""

    actor = headers.get(ACTOR_HEADER)
    session = headers.get(SESSION_HEADER)
    if actor is None and session is None:
        return None
    if actor is None or session is None:
        raise RpcError(INVALID_REQUEST, f"{ACTOR_HEADER} and {SESSION_HEADER} go together")
    actor = actor.strip()
    if not actor or len(actor) > MAX_ACTOR_LENGTH:
        raise RpcError(
            INVALID_REQUEST, f"{ACTOR_HEADER} must be 1 to {MAX_ACTOR_LENGTH} characters"
        )
    try:
        parsed = uuid.UUID(session)
    except ValueError:
        raise RpcError(INVALID_REQUEST, f"{SESSION_HEADER} must be a UUID") from None
    return actor, str(parsed)


def _error_code(exc: Exception) -> int:
    if isinstance(exc, ValidationError | InvalidPolicyError | ValueError):
        return INVALID_PARAMS
    if isinstance(exc, NotFoundError):
        return NOT_FOUND
    if isinstance(exc, ConflictError):
        return CONFLICT
    if isinstance(exc, PayloadTooLargeError):
        return PAYLOAD_TOO_LARGE
    if isinstance(exc, MergeGateError):
        return MERGE_GATE_UNAVAILABLE
    return INTERNAL_ERROR


class RpcDispatcher:
    """Validate and route one JSON-RPC request body."""

    def __init__(
        self,
        ops: OrchestratorOps,
        *,
        hub_info: Mapping[str, Any] | None = None,
        shutdown: Callable[[], None] | None = None,
        accounting: CallAccounting | None = None,
    ) -> None:
        self.ops = ops
        self.hub_info = hub_info
        self.shutdown = shutdown
        self.accounting = accounting
        self.orchestrator: OrchestratorSession | None = None
        self._superseded_sessions: set[str] = set()
        self._operations: dict[str, Callable[..., Awaitable[Any]]] = {
            name: validate_call(getattr(ops, name)) for name in OPERATIONS
        }
        self._params = {
            name: frozenset(inspect.signature(getattr(ops, name)).parameters)
            for name in OPERATIONS
        }

    async def dispatch(
        self, payload: Any, headers: Mapping[str, str], background: BackgroundTasks
    ) -> dict[str, Any]:
        request_id = payload.get("id") if isinstance(payload, dict) else None
        try:
            result = await self._dispatch(payload, headers, background)
        except RpcError as exc:
            return _error(request_id, exc.code, exc.message)
        except Exception as exc:
            code = _error_code(exc)
            if code == INTERNAL_ERROR:
                logger.exception("Unhandled error serving /rpc")
            return _error(request_id, code, str(exc))
        return {"jsonrpc": "2.0", "id": request_id, "result": to_jsonable_python(result)}

    async def _dispatch(
        self, payload: Any, headers: Mapping[str, str], background: BackgroundTasks
    ) -> Any:
        if (
            not isinstance(payload, dict)
            or payload.get("jsonrpc") != "2.0"
            or not isinstance(payload.get("method"), str)
            or "id" not in payload
            or not _valid_id(payload["id"])
        ):
            raise RpcError(INVALID_REQUEST, "Invalid Request")
        caller = parse_caller(headers)
        method = payload["method"]
        if method == "hub.info" and self.hub_info is not None:
            return dict(self.hub_info)
        if method == "hub.status" and self.hub_info is not None:
            params = payload.get("params", {})
            if not isinstance(params, dict) or params:
                raise RpcError(INVALID_PARAMS, "hub.status takes no params")
            summary = self.ops.store.status_summary()
            session = self.orchestrator
            return {
                "repo_root": self.hub_info["repo_root"],
                "origin": self.hub_info["origin"],
                "forge": self.hub_info["forge"],
                "url": self.hub_info["url"],
                "default_branch": self.hub_info["default_branch"],
                "orchestrator": None if session is None else {
                    "name": session.actor, "session": session.session,
                    "last_seen": session.last_seen.isoformat(),
                },
                **summary,
            }
        if method == "hub.shutdown" and self.shutdown is not None:
            background.add_task(self.shutdown)
            return {"stopping": True}
        if method == "hub.heartbeat":
            if caller is None:
                raise RpcError(INVALID_REQUEST, "orchestrator session headers are required")
            params = payload.get("params", {})
            if not isinstance(params, dict) or params:
                raise RpcError(INVALID_PARAMS, "hub.heartbeat takes no params")
            self._accept_session(*caller)
            return {"ok": True}
        if method == "hub.record_calls":
            if caller is None:
                raise RpcError(INVALID_REQUEST, "orchestrator session headers are required")
            params = payload.get("params")
            rows = params.get("calls") if isinstance(params, dict) else None
            if not isinstance(rows, list) or len(rows) > 32:
                raise RpcError(INVALID_PARAMS, "calls must be a list of at most 32 rows")
            records = []
            rejected = 0
            for row in rows:
                try:
                    records.append(self._call_record(row, caller[0]))
                except RpcError as exc:
                    if exc.code != INVALID_PARAMS:
                        raise
                    rejected += 1
            # These rows describe completed calls. A bridge superseded during
            # restart still needs to flush them, but accounting must never
            # change which orchestrator session owns future operations.
            if self.accounting is None or not self.accounting.enabled:
                return {"recorded": 0, "rejected": rejected, "disabled": True}
            recorded = sum(self.accounting.record(record) for record in records)
            return {"recorded": recorded, "rejected": rejected}
        operation = self._operations.get(method)
        if operation is None:
            raise RpcError(METHOD_NOT_FOUND, "Method not found")
        # Omitted params mean no arguments; an explicit null is not an object.
        params = payload.get("params", {})
        if not isinstance(params, dict):
            raise RpcError(INVALID_PARAMS, "params must be an object")
        unexpected = sorted(params.keys() - self._params[method])
        if unexpected:
            raise RpcError(INVALID_PARAMS, f"unexpected params for {method}: {unexpected}")
        if caller is not None:
            self._accept_session(*caller)
        result = await operation(**params)
        if (
            caller is not None and self.orchestrator is not None
            and caller[1] != self.orchestrator.session
        ):
            if method == "wait_for_event" and isinstance(result, dict):
                event = result.get("event")
                if isinstance(event, dict) and isinstance(event.get("delivery_id"), str):
                    self.ops.store.expire_event_leases(event["delivery_id"])
            raise RpcError(CONFLICT, "superseded by a newer orchestrator session")
        return result

    @staticmethod
    def _call_record(row: Any, actor: str) -> CallRecord:
        if not isinstance(row, dict) or set(row) != {
            "boundary", "actor", "tool", "outcome", "bytes_in", "bytes_out",
            "started", "finished", "status", "content_bytes", "repeat_bytes", "task_id",
        }:
            raise RpcError(INVALID_PARAMS, "invalid call record")
        if row["boundary"] != "mcp" or row["actor"] != actor:
            raise RpcError(INVALID_PARAMS, "call record actor or boundary mismatch")
        if row["tool"] not in (*OPERATIONS, "initialize", "tools/list", "ping",
                               "resources/list", "prompts/list", "unknown", "other"):
            raise RpcError(INVALID_PARAMS, "invalid call tool")
        if row["outcome"] not in ("ok", "error", "null_event", "event"):
            raise RpcError(INVALID_PARAMS, "invalid call outcome")
        for key in ("bytes_in", "bytes_out", "content_bytes", "repeat_bytes"):
            value = row[key]
            if (key in ("bytes_in", "bytes_out") and value is None) or (
                value is not None and (type(value) is not int or value < 0)
            ):
                raise RpcError(INVALID_PARAMS, f"invalid {key}")
        if row["status"] is not None or (
            row["task_id"] is not None and hub_id(row["task_id"]) is None
        ):
            raise RpcError(INVALID_PARAMS, "invalid call status or task_id")
        for key in ("tool", "outcome", "started", "finished"):
            if not isinstance(row[key], str) or len(row[key]) > 128:
                raise RpcError(INVALID_PARAMS, f"invalid {key}")
        try:
            started = datetime.fromisoformat(row["started"].replace("Z", "+00:00"))
            finished = datetime.fromisoformat(row["finished"].replace("Z", "+00:00"))
        except ValueError:
            raise RpcError(INVALID_PARAMS, "invalid call timestamps") from None
        if started.tzinfo is None or finished.tzinfo is None:
            raise RpcError(INVALID_PARAMS, "invalid call timestamps")
        if row["repeat_bytes"] is not None and (
            row["content_bytes"] is None or row["repeat_bytes"] > row["content_bytes"]
        ):
            raise RpcError(INVALID_PARAMS, "invalid repeat_bytes")
        return CallRecord(**row)

    def _accept_session(self, actor: str, session: str) -> None:
        if session in self._superseded_sessions:
            raise RpcError(CONFLICT, "superseded by a newer orchestrator session")
        current = self.orchestrator
        if current is not None and current.session != session:
            self._superseded_sessions.add(current.session)
            self.ops.store.expire_event_leases()
        self.orchestrator = OrchestratorSession(actor, session, self.ops.store.clock())


def _valid_id(request_id: Any) -> bool:
    """JSON-RPC 2.0 ids are a string, a number, or null.

    Python's parser reads `1e400` as inf and accepts `NaN`; neither can be
    echoed as JSON, so such an id is as unusable as an object would be.
    """

    if isinstance(request_id, float):
        return math.isfinite(request_id)
    return request_id is None or (
        isinstance(request_id, str | int) and not isinstance(request_id, bool)
    )


def _error(request_id: Any, code: int, message: str) -> dict[str, Any]:
    if not _valid_id(request_id):
        request_id = None
    return {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}}
