"""Structured, append-only telemetry for worker endurance runs.

A `tool_call` finish record also measures both boundaries a call crosses (#78):
the MCP boundary (`mcp_request_bytes`, `mcp_result_bytes` — what enters and
leaves the model's context) and the A2A boundary (`http_request_bytes`,
`http_response_bytes`, `http_status`, `http_retries` — what crossed the wire to
the hub). Sizes only; no payload text is copied into the log.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Mapping
from contextlib import suppress
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from time import monotonic
from typing import Any
from uuid import uuid4

import httpx
import pydantic_core

logger = logging.getLogger(__name__)


def _timestamp() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


@dataclass(slots=True)
class HttpIO:
    """The hub exchanges one MCP tool call made, summed.

    Every completed attempt counts, a retried 502/503/504 included: its bodies
    crossed the wire. `retries` counts the attempts that were retried, whether
    after such a response or after a transport error that left no response.
    """

    requests: int = 0
    request_bytes: int = 0
    response_bytes: int = 0
    status: int | None = None
    retries: int = 0


# Set for the duration of one tool call. The heartbeat task never sees it, so
# its exchanges are not charged to whichever call happens to be in flight.
current_http_io: ContextVar[HttpIO | None] = ContextVar("current_http_io", default=None)


def count_http_exchange(response: httpx.Response, *, retried: bool = False) -> None:
    """Charge one completed attempt to the tool call in progress, if any."""

    io = current_http_io.get()
    if io is None:
        return
    io.requests += 1
    io.retries += int(retried)
    with suppress(httpx.RequestNotRead):
        io.request_bytes += len(response.request.content)
    try:
        io.response_bytes += len(response.content)
    except httpx.ResponseNotRead:
        # A held stream is left once its data event arrives: what was read.
        io.response_bytes += response.num_bytes_downloaded
    io.status = response.status_code


def count_http_retry() -> None:
    """Charge a retry after a transport error, which leaves no response to count."""

    io = current_http_io.get()
    if io is not None:
        io.retries += 1


def _json_bytes(value: Any, *, indent: int | None = None) -> int:
    return len(pydantic_core.to_json(value, fallback=str, indent=indent))


def mcp_result_bytes(result: Any) -> int:
    """Size of the text content FastMCP makes of `result` for the model."""

    if isinstance(result, str):
        return len(result.encode("utf-8"))
    return _json_bytes(result, indent=2)


class TelemetryLog:
    """Write one bounded JSON object per line without ever touching stdout.

    The file is opened for each event so a thin supervisor can restart the MCP
    process and keep appending to the same run log. Telemetry failures are
    reported on stderr and never break the worker protocol.
    """

    def __init__(
        self,
        path: Path | None,
        *,
        agent: str,
        worker_instance_id: str,
        session_fields: Mapping[str, Any] | None = None,
    ) -> None:
        self.path = path
        self.agent = agent
        self.worker_instance_id = worker_instance_id
        self.session_id = uuid4().hex
        if path is not None:
            try:
                path.parent.mkdir(parents=True, exist_ok=True)
            except OSError:
                logger.exception("Could not create worker telemetry directory %s", path.parent)
                self.path = None
                return
            self.emit("session_started", **dict(session_fields or {}))

    def emit(self, event: str, **fields: Any) -> None:
        if self.path is None:
            return
        record = {
            "timestamp": _timestamp(),
            "event": event,
            "agent": self.agent,
            "worker_instance_id": self.worker_instance_id,
            "session_id": self.session_id,
            **fields,
        }
        try:
            with self.path.open("a", encoding="utf-8") as stream:
                stream.write(json.dumps(record, sort_keys=True, default=str) + "\n")
        except OSError:
            logger.exception("Could not append worker telemetry to %s", self.path)

    def start_tool(self, tool: str, *, task_id: str | None = None) -> tuple[str, float]:
        call_id = uuid4().hex
        fields = {"phase": "start", "tool": tool, "call_id": call_id}
        if task_id is not None:
            fields["task_id"] = task_id[:128]
        self.emit("tool_call", **fields)
        return call_id, monotonic()

    def finish_tool(
        self,
        tool: str,
        call_id: str,
        started: float,
        *,
        result: Any | None = None,
        error: BaseException | None = None,
        task_id: str | None = None,
        arguments: Mapping[str, Any] | None = None,
        http: HttpIO | None = None,
    ) -> None:
        if self.path is None:
            return
        common: dict[str, Any] = {
            "tool": tool,
            "call_id": call_id,
            "duration_s": round(monotonic() - started, 3),
        }
        if arguments is not None:
            common["mcp_request_bytes"] = _json_bytes(dict(arguments))
        if http is not None:
            common["http_requests"] = http.requests
            common["http_request_bytes"] = http.request_bytes
            common["http_response_bytes"] = http.response_bytes
            common["http_status"] = http.status
            common["http_retries"] = http.retries
        result_task_id = result.get("task_id") if isinstance(result, dict) else None
        logged_task_id = task_id if task_id is not None else result_task_id
        if isinstance(logged_task_id, str):
            common["task_id"] = logged_task_id[:128]
        if error is not None:
            self.emit(
                "tool_call",
                phase="error",
                error_type=type(error).__name__,
                error=str(error)[:500],
                **common,
            )
            return
        self.emit(
            "tool_call",
            phase="success",
            outcome=_tool_outcome(result),
            mcp_result_bytes=mcp_result_bytes(result),
            **common,
        )


def _tool_outcome(result: Any) -> str:
    """Summarize a tool result without copying external text into telemetry."""

    if not isinstance(result, dict):
        return "success"
    for marker in ("timeout", "release", "task_ended", "ok"):
        if result.get(marker) is True:
            return marker
    if result.get("task_id") and result.get("role"):
        return "assignment"
    if "reply" in result:
        return "reply"
    status = result.get("status")
    return str(status) if status else "success"
