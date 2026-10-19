"""FastAPI application: A2A discovery, the worker protocol, role guides, and `/rpc`."""

from __future__ import annotations

import json
import logging
import uuid
from collections.abc import AsyncIterator, Callable, Mapping
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from agent_hub_common import (
    AgentStatus,
    HubSettings,
    TaskState,
    load_config_layers,
    load_or_create_token,
    read_token_file,
)
from agent_hub_common.accounting import CallRecord
from agent_hub_common.clock import utcnow_iso
from agent_hub_common.discovery import repository_dir
from fastapi import BackgroundTasks, Depends, FastAPI, HTTPException, Request, Response
from fastapi.responses import JSONResponse, StreamingResponse

from .accounting import (
    A2ACall,
    CallAccounting,
    a2a_tool,
    begin_a2a,
    counted_stream,
)
from .card import build_agent_card
from .database import initialize_database
from .gitlab_gate import GitLabGate, GitLabGateError, GitLabProject, UnboundGitLabGate
from .guides import guide_response
from .merge_gate import ForgeGate, GitHubProject, MergeGate, MergeGateError, UnboundGitHubGate
from .orchestrator import OrchestratorOps
from .protocol import A2AProtocol, parse_error_response
from .rpc import RpcDispatcher
from .security import require_bearer
from .signals import Signals
from .store import HubStore
from .sweeper import start_sweeper, stop_sweeper

logger = logging.getLogger(__name__)

# Sent by worker-mcp on every request; only the guide route reads it (#78).
AGENT_HEADER = "X-Hub-Agent"


def select_gate(hub_info: Mapping[str, Any] | None) -> ForgeGate:
    """Choose the merge gate from the forge `robomate up` detected (§8, §10).

    `gitlab` gets a GitLabGate bound to the origin's project; `github` gets a
    MergeGate bound to the origin's repository; anything else, and a hub
    without hub_info, gets the unbound GitHub gate. `up` detects the forge
    again on every start, so a restart chooses the same gate.
    """

    if hub_info is None:
        return MergeGate()
    if hub_info.get("forge") == "gitlab":
        try:
            project = GitLabProject.from_origin(str(hub_info.get("origin") or ""))
        except GitLabGateError as exc:
            logger.warning("check_merge_gate will fail closed: %s", exc)
            return UnboundGitLabGate(str(exc))
        logger.info("check_merge_gate reads GitLab project %s", project.web_url)
        return GitLabGate(project)
    if hub_info.get("forge") == "github":
        try:
            github_project = GitHubProject.from_origin(str(hub_info.get("origin") or ""))
        except MergeGateError as exc:
            logger.warning("check_merge_gate will fail closed: %s", exc)
            return UnboundGitHubGate(str(exc))
        logger.info("check_merge_gate reads GitHub repository %s", github_project.web_url)
        return MergeGate(project=github_project)
    return MergeGate()


def create_app(
    settings: HubSettings | None = None,
    *,
    hub_info: Mapping[str, Any] | None = None,
    shutdown: Callable[[], None] | None = None,
) -> FastAPI:
    """Create a configured hub application without starting a server."""

    resolved = settings or HubSettings.from_env()
    common_raw = hub_info.get("git_common_dir") if hub_info is not None else None
    common_dir = Path(str(common_raw)) if common_raw else None
    config_layers = load_config_layers(
        hub_config=resolved.state_dir / "config.toml",
        git_common_dir=common_dir,
    )
    hub_id = str(hub_info["hub_id"]) if hub_info is not None else uuid.uuid4().hex
    card = build_agent_card(resolved.public_url)
    store = HubStore(
        path=resolved.database_path,
        signals=Signals(),
        default_event_lease_s=resolved.event_lease_s,
        state_dir=resolved.state_dir,
    )
    protocol = A2AProtocol(store=store, settings=resolved)
    orchestrator = OrchestratorOps(store, select_gate(hub_info))
    accounting = CallAccounting(
        resolved.database_path,
        enabled=resolved.call_accounting,
        jsonl_path=resolved.call_log_jsonl,
    )
    dispatcher = RpcDispatcher(
        orchestrator,
        hub_info=hub_info,
        shutdown=shutdown,
        accounting=accounting,
        guides_dir=resolved.guides_dir,
    )

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        initialize_database(resolved.database_path)
        # Silence is measured from here at the earliest: a restarted hub knows
        # nothing of the calls and holds before it (#144).
        store.activity.mark_started(store.clock())
        app.state.bearer_token = load_or_create_token(resolved.token, resolved.token_file)
        # `robomate up` creates the operator token; the hub only reads it, so a
        # hub started some other way has no operator methods until it exists.
        operator_file = resolved.operator_token_file
        if operator_file is not None and operator_file.exists():
            dispatcher.operator_token = read_token_file(operator_file, "operator token")
            logger.info("Operator credential loaded")
        else:
            dispatcher.operator_token = None
            logger.info("No operator credential; operator-only methods are refused")
        # Log the resolved absolute path so a hub started against the wrong state
        # directory is visible at once, rather than as lost state later.
        logger.info("SQLite database ready at %s", resolved.database_path)
        if resolved.guides_dir.is_dir():
            logger.info("Serving role guides from %s", resolved.guides_dir)
        else:
            # Step 5 authors the guide content; until then, and whenever
            # HUB_GUIDES_DIR points somewhere else than intended, every
            # /guides/{role}.md is a 404 and this line is the only warning.
            logger.warning("Guides directory %s does not exist", resolved.guides_dir)
        if accounting.enabled:
            logger.info(
                "Call accounting on: call_log in %s%s",
                resolved.database_path,
                f", raw stream at {accounting.jsonl_path}" if accounting.jsonl_path else "",
            )
        if resolved.token is None:
            logger.info("Bearer token ready at %s", resolved.token_file)
        else:
            logger.info("Bearer token loaded from HUB_TOKEN")
        sweeper = start_sweeper(store, resolved.sweep_interval_s, resolved.lost_after_s)
        try:
            yield
        finally:
            await stop_sweeper(sweeper)

    app = FastAPI(
        title="Agent Comms Hub",
        version="0.1.0",
        lifespan=lifespan,
        # Every route is hand-written and excluded from the schema, so the
        # generated docs would describe nothing while widening the public
        # surface past the two routes §4.1 allows.
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    app.state.settings = resolved
    app.state.config_layers = config_layers
    app.state.store = store
    app.state.orchestrator = orchestrator
    app.state.rpc = dispatcher
    app.state.accounting = accounting

    def record_http(call: A2ACall, status: int, bytes_in: int, bytes_out: int) -> None:
        accounting.record(
            CallRecord(
                boundary="a2a",
                actor=call.actor,
                tool=call.tool,
                outcome=call.outcome or "ok",
                status=status,
                bytes_in=bytes_in,
                bytes_out=bytes_out,
                started=call.started,
                finished=utcnow_iso(),
                task_id=call.task_id,
            )
        )

    def measured(call: A2ACall | None, bytes_in: int, response: Response) -> Response:
        """Record `response` once its full size is known: a held stream at its end."""

        if call is None:
            return response
        if isinstance(response, StreamingResponse):
            status = response.status_code
            response.body_iterator = counted_stream(
                response.body_iterator,
                lambda sent: record_http(call, status, bytes_in, sent),
            )
            return response
        body = bytes(response.body)
        if call.outcome is None:
            try:
                parsed = json.loads(body)
            except ValueError:
                parsed = None
            call.outcome = "error" if isinstance(parsed, dict) and "error" in parsed else "ok"
        record_http(call, response.status_code, bytes_in, len(body))
        return response

    @app.get("/.well-known/agent-card.json", include_in_schema=False)
    async def agent_card() -> dict[str, object]:
        return card.model_dump(by_alias=True, exclude_none=True)

    @app.get("/healthz", include_in_schema=False)
    async def health() -> dict[str, str]:
        return {"status": "ok", "hub_id": hub_id}

    @app.get("/whoami", include_in_schema=False, dependencies=[Depends(require_bearer)])
    async def whoami(request: Request) -> Response:
        call = begin_a2a("whoami") if accounting.enabled else None
        claimed = request.headers.get(AGENT_HEADER, "")
        if call is not None and claimed and store.agent_by_name(claimed) is not None:
            call.actor = claimed
        info = dict(hub_info or {})
        hub_name = info.get("name") or info.get("hub_name") or "hub"
        checkout = info.get("checkout") or info.get("repo_root")
        common = info.get("git_common_dir")
        repository = (
            str(repository_dir(Path(str(common))))
            if common
            else (str(checkout) if checkout else None)
        )
        agents = [
            {
                "name": a.name,
                "standing_role": a.standing_role.value,
                "status": a.status.value,
                "alive": a.status is not AgentStatus.LOST,
                "harness": a.harness,
                "model": a.model,
            }
            for a in store.agents()
        ]
        agent = store.agent_by_name(claimed) if claimed else None
        current_task = None
        if agent and agent.current_task_id:
            task = store.get_task(agent.current_task_id)
            if task and task.state not in (
                TaskState.COMPLETED,
                TaskState.FAILED,
                TaskState.CANCELED,
            ):
                current_task = {
                    "task_id": task.id,
                    "role": task.role,
                    "title": task.title,
                    "state": task.state.value,
                }
        hub_url = info.get("url") or resolved.public_url
        payload = {
            "hub_id": hub_id,
            "name": hub_name,
            "hub_name": hub_name,
            "agent_name": agent.name if agent else None,
            "joined": agent is not None,
            "role": agent.standing_role.value if agent else None,
            "standing_role": agent.standing_role.value if agent else None,
            "hub": hub_url,
            "status": "active" if agent and agent.status is not AgentStatus.LOST else "idle",
            "current_task": current_task,
            "last_seen": agent.last_seen if agent else None,
            "checkout": checkout,
            "repo_root": info.get("repo_root") or checkout,
            "repository": repository,
            "git_common_dir": common,
            "origin": info.get("origin"),
            "forge": info.get("forge"),
            "default_branch": info.get("default_branch"),
            "url": hub_url,
            "port": info.get("port") or resolved.port,
            "agents": agents,
        }
        return measured(call, 0, JSONResponse(payload))

    @app.post("/rpc", include_in_schema=False, dependencies=[Depends(require_bearer)])
    async def rpc(request: Request, background: BackgroundTasks) -> Response:
        try:
            payload = await request.json()
        except ValueError:
            payload = None
        body = await dispatcher.dispatch(payload, request.headers, background)
        return JSONResponse(body, background=background)

    @app.get("/guides/{role}.md", include_in_schema=False, dependencies=[Depends(require_bearer)])
    async def role_guide(role: str, request: Request, forge: str | None = None) -> Response:
        # Composition is the default: the hub's forge picks the appendix
        # (`gitlab` gives `gitlab.md`, anything else `github.md`, matching
        # gate selection), and `?forge=` overrides it. A missing appendix for
        # the effective forge is a 404, never a guide without its commands.
        if forge:
            effective_forge: str | None = forge
        elif hub_info is not None and hub_info.get("forge") == "gitlab":
            effective_forge = "gitlab"
        else:
            effective_forge = "github"
        if not accounting.enabled:
            return guide_response(resolved.guides_dir, role, forge=effective_forge)
        call = begin_a2a("get_role_guide")
        # The guide route carries no A2A identity. worker-mcp names itself in a
        # header, which is counted only when it is a registered agent's name.
        claimed = request.headers.get(AGENT_HEADER, "")
        if claimed and store.agent_by_name(claimed) is not None:
            call.actor = claimed
        try:
            response = guide_response(resolved.guides_dir, role, forge=effective_forge)
        except HTTPException as exc:
            call.outcome = "not_found"
            record_http(call, exc.status_code, 0, len(json.dumps({"detail": exc.detail})))
            raise
        return measured(call, 0, response)

    @app.post("/a2a", include_in_schema=False, dependencies=[Depends(require_bearer)])
    async def a2a(request: Request) -> Response:
        call = begin_a2a("invalid") if accounting.enabled else None
        body = await request.body()
        try:
            payload: Any = json.loads(body)
        except ValueError:
            return measured(call, len(body), parse_error_response())
        if call is not None:
            call.tool = a2a_tool(payload)
        # The connection's peer, not a header such as X-Forwarded-For: a
        # worker can set any header it likes, but not the socket it dials from.
        peer = request.client.host if request.client is not None else None
        return measured(call, len(body), await protocol.dispatch(payload, peer))

    return app
