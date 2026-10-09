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

from agent_hub_common import token_matches
from fastapi import BackgroundTasks
from pydantic import validate_call
from pydantic_core import to_jsonable_python

from .accounting import CallAccounting, CallRecord, hub_id
from .orchestrator import CALLER, OPERATIONS, Caller, OrchestratorOps
from .rpc_errors import CONFLICT as CONFLICT
from .rpc_errors import INTERNAL_ERROR as INTERNAL_ERROR
from .rpc_errors import INVALID_PARAMS as INVALID_PARAMS
from .rpc_errors import INVALID_REQUEST as INVALID_REQUEST
from .rpc_errors import MERGE_GATE_UNAVAILABLE as MERGE_GATE_UNAVAILABLE
from .rpc_errors import METHOD_NOT_FOUND as METHOD_NOT_FOUND
from .rpc_errors import NOT_FOUND as NOT_FOUND
from .rpc_errors import OPERATOR_REQUIRED as OPERATOR_REQUIRED
from .rpc_errors import PAYLOAD_TOO_LARGE as PAYLOAD_TOO_LARGE
from .rpc_errors import error_code

logger = logging.getLogger(__name__)

# Self-declared by the orchestrator bridge (§12 threat model): they identify a
# session for the one-orchestrator rule, accounting and attribution, never
# authorize. Every orchestrator operation must carry them (#128).
ACTOR_HEADER = "X-Robomate-Actor"
SESSION_HEADER = "X-Robomate-Session"
MAX_ACTOR_LENGTH = 128
# The operator credential, which agents do not hold (#128). It authorizes the
# operator-only methods on top of the bearer token every agent has.
OPERATOR_HEADER = "X-Robomate-Operator"

# Read-only or high-frequency methods, whose rows would bury the actions the
# audit exists to show (#128). Every other /rpc call leaves one rpc_audit row.
UNAUDITED_METHODS = frozenset(
    {
        "get_state",
        "wait_for_event",
        "hub.info",
        "hub.status",
        "hub.snapshot",
        "hub.heartbeat",
        "hub.record_calls",
        "hub.questions",
        "hub.operator_answer",
    }
)
# The audit row a takeover leaves, under the session that took over.
SUPERSEDE_METHOD = "session.supersede"


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


def require_operator(headers: Mapping[str, str], expected: str | None) -> None:
    """Refuse an operator-only call that does not carry the operator credential.

    The bearer token says only that the caller is on this hub, and every agent
    holds it; the operator token is what says the operator sent the call. A
    hub that loaded no operator token refuses every operator-only method.
    """

    if expected is None:
        raise RpcError(OPERATOR_REQUIRED, "this hub has no operator credential loaded")
    candidate = headers.get(OPERATOR_HEADER)
    if candidate is None or not token_matches(candidate.strip(), expected):
        raise RpcError(OPERATOR_REQUIRED, f"{OPERATOR_HEADER} with the operator token is required")


@dataclass(slots=True)
class _Audit:
    """What one call's rpc_audit row records, filled in as dispatch learns it."""

    method: str | None = None
    actor: str | None = None
    session: str | None = None


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
        # Set by the app's lifespan from the operator token file (#128).
        self.operator_token: str | None = None
        self.orchestrator: OrchestratorSession | None = None
        self._superseded_sessions: set[str] = set()
        self._operations: dict[str, Callable[..., Awaitable[Any]]] = {
            name: validate_call(getattr(ops, name)) for name in OPERATIONS
        }
        self._params = {
            name: frozenset(inspect.signature(getattr(ops, name)).parameters) for name in OPERATIONS
        }

    async def dispatch(
        self, payload: Any, headers: Mapping[str, str], background: BackgroundTasks
    ) -> dict[str, Any]:
        request_id = payload.get("id") if isinstance(payload, dict) else None
        audit = _Audit()
        try:
            result = await self._dispatch(payload, headers, background, audit)
        except RpcError as exc:
            self._audit(audit, f"error {exc.code}")
            return _error(request_id, exc.code, exc.message)
        except Exception as exc:
            code = error_code(exc)
            if code == INTERNAL_ERROR:
                logger.exception("Unhandled error serving /rpc")
            self._audit(audit, f"error {code}")
            return _error(request_id, code, str(exc))
        self._audit(audit, "ok")
        return {"jsonrpc": "2.0", "id": request_id, "result": to_jsonable_python(result)}

    def _audit(self, audit: _Audit, outcome: str) -> None:
        method = audit.method if audit.method is not None else "invalid"
        if method in UNAUDITED_METHODS:
            return
        self._record_audit(audit.actor, audit.session, method, outcome)

    def _record_audit(
        self, actor: str | None, session: str | None, method: str, outcome: str
    ) -> None:
        # The call itself has already happened, so a failed audit write is
        # logged loudly rather than turned into an error for an applied call.
        try:
            self.ops.store.record_rpc_audit(actor, session, method, outcome)
        except Exception:
            logger.exception("Could not record the rpc_audit row for %s", method)

    async def _dispatch(
        self, payload: Any, headers: Mapping[str, str], background: BackgroundTasks, audit: _Audit
    ) -> Any:
        if isinstance(payload, dict) and isinstance(payload.get("method"), str):
            audit.method = payload["method"]
        if (
            not isinstance(payload, dict)
            or payload.get("jsonrpc") != "2.0"
            or not isinstance(payload.get("method"), str)
            or "id" not in payload
            or not _valid_id(payload["id"])
        ):
            raise RpcError(INVALID_REQUEST, "Invalid Request")
        caller = parse_caller(headers)
        if caller is not None:
            audit.actor, audit.session = caller
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
                # Additive since checkout scope (#147); absent from older hub.json.
                "hub_id": self.hub_info.get("hub_id"),
                "name": self.hub_info.get("name"),
                "checkout": self.hub_info.get("checkout") or self.hub_info["repo_root"],
                "git_common_dir": self.hub_info.get("git_common_dir"),
                "origin": self.hub_info["origin"],
                "forge": self.hub_info["forge"],
                "url": self.hub_info["url"],
                "default_branch": self.hub_info["default_branch"],
                "orchestrator": None
                if session is None
                else {
                    "name": session.actor,
                    "session": session.session,
                    "last_seen": session.last_seen.isoformat(),
                },
                **summary,
            }
        # The operator's before-snapshot (#146): bearer-only and read-only. It
        # names the current session without accepting or superseding any.
        if method == "hub.snapshot":
            params = payload.get("params", {})
            if not isinstance(params, dict) or params:
                raise RpcError(INVALID_PARAMS, "hub.snapshot takes no params")
            snapshot = self.ops.store.snapshot()
            session = self.orchestrator
            return {
                "hub_id": None if self.hub_info is None else self.hub_info.get("hub_id"),
                "orchestrator": None
                if session is None
                else {
                    "name": session.actor,
                    "session": session.session,
                    "last_seen": session.last_seen.isoformat(),
                },
                **snapshot,
            }
        # Operator questions (#129) are readable with the bearer token alone,
        # so a worker can check an answer the orchestrator says it received.
        if method == "hub.questions":
            params = payload.get("params", {})
            if not isinstance(params, dict) or params:
                raise RpcError(INVALID_PARAMS, "hub.questions takes no params")
            return {"questions": self.ops.store.open_operator_questions()}
        if method == "hub.operator_answer":
            params = payload.get("params")
            question_id = params.get("question_id") if isinstance(params, dict) else None
            if (
                not isinstance(params, dict)
                or set(params) != {"question_id"}
                or type(question_id) is not int
            ):
                raise RpcError(INVALID_PARAMS, "hub.operator_answer takes an integer question_id")
            return self.ops.store.operator_answer(question_id)
        # Only the operator answers (#130): an agent holding the bearer token
        # must not be able to stand in for an operator decision.
        if method == "hub.answer":
            require_operator(headers, self.operator_token)
            audit.actor, audit.session = "operator", None
            params = payload.get("params")
            if (
                not isinstance(params, dict)
                or set(params) != {"question_id", "answer"}
                or type(params["question_id"]) is not int
                or not isinstance(params["answer"], str)
            ):
                raise RpcError(
                    INVALID_PARAMS, "hub.answer takes an integer question_id and a string answer"
                )
            return self.ops.store.answer_operator_question(params["question_id"], params["answer"])
        if method == "hub.shutdown" and self.shutdown is not None:
            require_operator(headers, self.operator_token)
            audit.actor, audit.session = "operator", None
            background.add_task(self.shutdown)
            return {"stopping": True}
        if method == "hub.heartbeat":
            if caller is None:
                raise RpcError(INVALID_REQUEST, "orchestrator session headers are required")
            params = payload.get("params", {})
            if not isinstance(params, dict) or params:
                raise RpcError(INVALID_PARAMS, "hub.heartbeat takes no params")
            self._accept_session(*caller)
            # Bridge liveness only: never activity (#144).
            self.ops.store.note_orchestrator_heartbeat(caller[1])
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
        if caller is None:
            raise RpcError(
                INVALID_REQUEST,
                f"{method} requires the {ACTOR_HEADER} and {SESSION_HEADER} headers",
            )
        self._accept_session(*caller)
        token = CALLER.set(Caller(*caller))
        try:
            with self.ops.store.orchestrator_call(caller[1], method, _hold_s(method, params)):
                result = await operation(**params)
        finally:
            CALLER.reset(token)
        if self.orchestrator is not None and caller[1] != self.orchestrator.session:
            if method == "wait_for_event" and isinstance(result, dict):
                event = result.get("event")
                if isinstance(event, dict) and isinstance(event.get("delivery_id"), str):
                    self.ops.store.expire_event_leases(event["delivery_id"])
            raise RpcError(CONFLICT, "superseded by a newer orchestrator session")
        return result

    @staticmethod
    def _call_record(row: Any, actor: str) -> CallRecord:
        if not isinstance(row, dict) or set(row) != {
            "boundary",
            "actor",
            "tool",
            "outcome",
            "bytes_in",
            "bytes_out",
            "started",
            "finished",
            "status",
            "content_bytes",
            "repeat_bytes",
            "task_id",
        }:
            raise RpcError(INVALID_PARAMS, "invalid call record")
        if row["boundary"] != "mcp" or row["actor"] != actor:
            raise RpcError(INVALID_PARAMS, "call record actor or boundary mismatch")
        if row["tool"] not in (
            *OPERATIONS,
            "initialize",
            "tools/list",
            "ping",
            "resources/list",
            "prompts/list",
            "unknown",
            "other",
        ):
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
            self._record_audit(actor, session, SUPERSEDE_METHOD, "ok")
        self.orchestrator = OrchestratorSession(actor, session, self.ops.store.clock())
        self.ops.store.note_orchestrator_session(actor, session)


def _hold_s(method: str, params: Mapping[str, Any]) -> float:
    """How long an orchestrator call may legitimately run before it stops exempting her.

    `wait_for_event` holds for its own timeout; the merge gate settles for up
    to a minute; nothing else should take longer than the bridge's 75 s.
    """

    if method == "wait_for_event":
        timeout = params.get("timeout_s", 100)
        if isinstance(timeout, int | float) and not isinstance(timeout, bool):
            return min(max(float(timeout), 0.0), 120.0)
        return 100.0
    return 75.0


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
