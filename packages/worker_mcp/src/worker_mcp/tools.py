"""Worker MCP tools (§4.3) exposed to LLM runtimes."""

from __future__ import annotations

from collections.abc import Awaitable, Mapping
from typing import Annotated, Any, TypeVar

from agent_hub_common import ImplementerResult, RebaseResult, ReviewerResult
from mcp.server.fastmcp import FastMCP
from pydantic import Field

from .client import WorkerHubClient
from .telemetry import HttpIO, current_http_io

Timeout = Annotated[float, Field(ge=0, le=300, allow_inf_nan=False)]
T = TypeVar("T")


def create_worker_mcp(client: WorkerHubClient) -> FastMCP:
    server = FastMCP(
        "worker-mcp",
        instructions=(
            "Worker-side MCP tools for coordinating with Alice through the hub. "
            "Follow the workflow: check_in -> await_assignment -> get_role_guide -> "
            "do work -> submit_result."
        ),
    )

    async def invoke(
        tool: str,
        call: Awaitable[T],
        arguments: Mapping[str, Any],
        *,
        task_id: str | None = None,
    ) -> T:
        call_id, started = client.telemetry.start_tool(tool, task_id=task_id)
        http = HttpIO()
        token = current_http_io.set(http)
        try:
            result = await call
        except BaseException as exc:
            client.telemetry.finish_tool(
                tool, call_id, started, error=exc, task_id=task_id, arguments=arguments, http=http
            )
            raise
        finally:
            current_http_io.reset(token)
        client.telemetry.finish_tool(
            tool, call_id, started, result=result, task_id=task_id, arguments=arguments, http=http
        )
        return result

    @server.tool()
    async def check_in(
        capabilities: list[str] | None = None, model: str | None = None
    ) -> dict[str, Any]:
        """One-time registration with the hub, reporting this worker's identity profile.

        Harness, provider and configured capabilities come from the launcher.
        capabilities: optional extra capabilities to declare.
        model: your exact model ID if you know it; ignored when the launcher
        already names the model. Omit rather than guess.
        """
        return await invoke(
            "check_in",
            client.check_in(capabilities, model),
            {"capabilities": capabilities, "model": model},
        )

    @server.tool()
    async def get_role_guide(role: str) -> str:
        """Fetch instructions for the assigned role (e.g. 'implementer', 'reviewer')."""
        return await invoke("get_role_guide", client.get_role_guide(role), {"role": role})

    @server.tool()
    async def await_assignment(timeout_s: Timeout | None = None) -> dict[str, Any]:
        """Poll the hub for the next task assignment.

        Returns {task_id, role, instructions}, {release: true}, or {timeout: true}.
        A review or rebase assignment also carries pr_head_sha, the head it is bound to.
        On timeout, call again.
        timeout_s: Optional wait timeout in seconds (defaults to HUB_DEFAULT_WAIT_S if omitted).
        """
        return await invoke(
            "await_assignment", client.await_assignment(timeout_s), {"timeout_s": timeout_s}
        )

    @server.tool()
    async def report_progress(task_id: str, note: str) -> dict[str, Any]:
        """Send a non-blocking progress update note to Alice."""
        return await invoke(
            "report_progress",
            client.report_progress(task_id, note),
            {"task_id": task_id, "note": note},
            task_id=task_id,
        )

    @server.tool()
    async def ask_alice(
        task_id: str, question: str, timeout_s: Timeout | None = None
    ) -> dict[str, Any]:
        """Ask Alice a clarifying question and hold for her response.

        Returns {reply: text}, {timeout: true}, or {task_ended: true, state, note}.
        On timeout, call ask_alice again to continue waiting; retries resume the pending question.
        timeout_s: Optional wait timeout in seconds (defaults to HUB_DEFAULT_WAIT_S if omitted).
        """
        return await invoke(
            "ask_alice",
            client.ask_alice(task_id, question, timeout_s),
            {"task_id": task_id, "question": question, "timeout_s": timeout_s},
            task_id=task_id,
        )

    @server.tool()
    async def submit_result(
        task_id: str,
        # RebaseResult last: a body that fits several models validates as the
        # first, and the hub re-validates against the task's role either way.
        result: ImplementerResult | ReviewerResult | RebaseResult,
    ) -> dict[str, Any]:
        """Submit the final result for a task, validated against the role's schema."""
        return await invoke(
            "submit_result",
            client.submit_result(task_id, result),
            {"task_id": task_id, "result": result},
            task_id=task_id,
        )

    return server
