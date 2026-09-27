"""Observe MCP framing in the orchestrator bridge and prepare compact call rows."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from typing import Any

from agent_hub.accounting import CallRecord, hub_id
from agent_hub_common.clock import utcnow_iso

MCP_METHODS = frozenset({"initialize", "tools/list", "ping", "resources/list", "prompts/list"})


# -- MCP boundary -------------------------------------------------------------


@dataclass(slots=True)
class _PendingMcp:
    tool: str
    bytes_in: int
    started: str
    task_id: str | None


@dataclass(slots=True)
class McpAccounting:
    """Pair Alice's JSON-RPC requests with bridge responses to the harness.

    Measured on the stdio framing itself, so `bytes_in`/`bytes_out` are exactly
    what crossed the pipe. `content_bytes` is the text content a harness puts
    into the model's context, and `repeat_bytes` how much of it repeats the
    previous result of the same tool, line for line. The previous text is held
    in memory only for that comparison and never recorded.
    """

    record: Callable[[CallRecord], object]
    tools: frozenset[str]
    actor: str = "alice"
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
        self.record(
            CallRecord(
                boundary="mcp",
                actor=self.actor,
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
