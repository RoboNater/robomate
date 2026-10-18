"""Worker MCP tools (§4.3) exposed to LLM runtimes."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from functools import wraps
from types import MethodType
from typing import Annotated, Any, TypeVar

from agent_hub_common import ImplementerResult, RebaseResult, ReviewerResult, StandingRole
from agent_hub_common.orchestrator_tools import OPERATIONS, OrchestratorTools
from mcp.server.fastmcp import FastMCP
from pydantic import Field

from .client import WorkerHubClient
from .telemetry import HttpIO, current_http_io

Timeout = Annotated[float, Field(ge=0, le=300, allow_inf_nan=False)]
T = TypeVar("T")


def create_worker_mcp(
    client: WorkerHubClient | Callable[[], Awaitable[WorkerHubClient]],
    *,
    server_name: str = "worker-mcp",
    include_orchestrator: bool = False,
) -> FastMCP:
    server = FastMCP(
        server_name,
        instructions=(
            "Worker-side MCP tools for coordinating with the orchestrator through the hub. "
            "Follow the workflow: join -> await_assignment -> do work -> submit_result."
        ),
    )

    async def resolve() -> WorkerHubClient:
        return client if isinstance(client, WorkerHubClient) else await client()

    async def invoke(
        resolved: WorkerHubClient,
        tool: str,
        call: Awaitable[T],
        arguments: Mapping[str, Any],
        *,
        task_id: str | None = None,
    ) -> T:
        call_id, started = resolved.telemetry.start_tool(tool, task_id=task_id)
        http = HttpIO()
        token = current_http_io.set(http)
        try:
            result = await call
        except BaseException as exc:
            resolved.telemetry.finish_tool(
                tool, call_id, started, error=exc, task_id=task_id, arguments=arguments, http=http
            )
            raise
        finally:
            current_http_io.reset(token)
        resolved.telemetry.finish_tool(
            tool, call_id, started, result=result, task_id=task_id, arguments=arguments, http=http
        )
        return result

    def require_joined(resolved: WorkerHubClient, *, allow_preconfigured: bool = True) -> None:
        if resolved.standing_role is StandingRole.ORCHESTRATOR:
            raise RuntimeError("this tool requires a worker role")
        if resolved.is_joined:
            return
        if allow_preconfigured and resolved.settings.agent_name:
            return
        raise RuntimeError(
            "Worker has not joined or checked in yet; "
            "call join(name=..., role=...) or check_in first"
        )

    @server.tool(
        description=(
            "Call when the user asks you to join robomate. Use the name and role the user gives."
        )
    )
    async def join(
        name: str,
        role: str,
        model: str | None = None,
        effort: str | None = None,
        harness: str | None = None,
        hub: str | None = None,
        takeover: bool = False,
        capabilities: list[str] | None = None,
    ) -> dict[str, Any]:
        """Join the hub under the given agent name and standing role."""
        resolved = await resolve()
        return await invoke(
            resolved,
            "join",
            resolved.join(
                name,
                role,
                model=model,
                effort=effort,
                harness=harness,
                hub=hub,
                takeover=takeover,
                capabilities=capabilities,
            ),
            {
                "name": name,
                "role": role,
                "model": model,
                "effort": effort,
                "harness": harness,
                "hub": hub,
                "takeover": takeover,
                "capabilities": capabilities,
            },
        )

    @server.tool()
    async def whoami() -> dict[str, Any]:
        """Return the connected hub's identity and this bridge's join state."""
        resolved = await resolve()
        return await invoke(resolved, "whoami", resolved.whoami(), {})

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
        resolved = await resolve()
        require_joined(resolved, allow_preconfigured=True)
        return await invoke(
            resolved,
            "check_in",
            resolved.check_in(capabilities, model),
            {"capabilities": capabilities, "model": model},
        )

    @server.tool()
    async def get_role_guide(role: str) -> str:
        """Fetch instructions for the assigned role (e.g. 'implementer', 'reviewer')."""
        resolved = await resolve()
        require_joined(resolved, allow_preconfigured=True)
        return await invoke(
            resolved, "get_role_guide", resolved.get_role_guide(role), {"role": role}
        )

    @server.tool()
    async def await_assignment(timeout_s: Timeout | None = None) -> dict[str, Any]:
        """Poll the hub for the next task assignment.

        Returns {task_id, role, instructions}, {release: true}, or {timeout: true}.
        A review or rebase assignment also carries pr_head_sha, the head it is bound to.
        On timeout, call again.
        timeout_s: Optional wait timeout in seconds (defaults to HUB_DEFAULT_WAIT_S if omitted).
        """
        resolved = await resolve()
        require_joined(resolved)
        return await invoke(
            resolved,
            "await_assignment",
            resolved.await_assignment(timeout_s),
            {"timeout_s": timeout_s},
        )

    @server.tool()
    async def report_progress(task_id: str, note: str) -> dict[str, Any]:
        """Send a non-blocking progress update note to Alice."""
        resolved = await resolve()
        require_joined(resolved)
        return await invoke(
            resolved,
            "report_progress",
            resolved.report_progress(task_id, note),
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
        resolved = await resolve()
        require_joined(resolved)
        return await invoke(
            resolved,
            "ask_alice",
            resolved.ask_alice(task_id, question, timeout_s),
            {"task_id": task_id, "question": question, "timeout_s": timeout_s},
            task_id=task_id,
        )

    @server.tool()
    async def ask_orchestrator(
        task_id: str, question: str, timeout_s: Timeout | None = None
    ) -> dict[str, Any]:
        """Ask the orchestrator a clarifying question and hold for a response.

        Returns {reply: text}, {timeout: true}, or {task_ended: true, state, note}.
        On timeout, call ask_orchestrator again to continue waiting.
        """
        resolved = await resolve()
        require_joined(resolved)
        return await invoke(
            resolved,
            "ask_orchestrator",
            resolved.ask_orchestrator(task_id, question, timeout_s),
            {"task_id": task_id, "question": question, "timeout_s": timeout_s},
            task_id=task_id,
        )

    @server.tool()
    async def get_operator_answer(question_id: int) -> dict[str, Any]:
        """Read an operator question and its answer straight from the hub.

        Returns {question_id, question, status, answer, answered}; status is
        answered, unanswered or withdrawn. Only answered confirms an operator
        decision; withdrawn grants no authority.
        """
        resolved = await resolve()
        require_joined(resolved, allow_preconfigured=True)
        return await invoke(
            resolved,
            "get_operator_answer",
            resolved.get_operator_answer(question_id),
            {"question_id": question_id},
        )

    @server.tool()
    async def get_statement(sha256: str) -> dict[str, Any]:
        """Read a submitted statement of work verbatim by its SHA-256.

        Returns {sha256, statement, size, source, created}; bytes are stored
        verbatim (CRLF and trailing newline preserved). Unknown SHA is -32001.
        Assignments carry statement_sha256; call this when you need the text.
        """
        resolved = await resolve()
        require_joined(resolved, allow_preconfigured=True)
        return await invoke(
            resolved,
            "get_statement",
            resolved.get_statement(sha256),
            {"sha256": sha256},
        )

    @server.tool()
    async def submit_result(
        task_id: str,
        # RebaseResult last: a body that fits several models validates as the
        # first, and the hub re-validates against the task's role either way.
        result: ImplementerResult | ReviewerResult | RebaseResult,
    ) -> dict[str, Any]:
        """Submit the final result for a task, validated against the role's schema."""
        resolved = await resolve()
        require_joined(resolved)
        return await invoke(
            resolved,
            "submit_result",
            resolved.submit_result(task_id, result),
            {"task_id": task_id, "result": result},
            task_id=task_id,
        )

    if include_orchestrator:

        def forward_operation(name: str) -> Any:
            @wraps(getattr(OrchestratorTools, name))
            async def forward(_self: Any, **kwargs: Any) -> dict[str, Any]:
                resolved = await resolve()
                if resolved.orchestrator is None or not resolved.orchestrator.joined:
                    raise RuntimeError("call join(name=..., role='orchestrator') first")
                return await resolved.orchestrator.call(name, kwargs)

            return forward

        for operation in OPERATIONS:
            server.add_tool(MethodType(forward_operation(operation), server))
    return server
