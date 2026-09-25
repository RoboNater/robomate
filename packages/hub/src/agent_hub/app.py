"""FastAPI application: A2A discovery, the worker protocol, and role guides."""

from __future__ import annotations

import json
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from agent_hub_common import HubSettings, load_or_create_token
from agent_hub_common.clock import utcnow_iso
from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.responses import StreamingResponse

from .accounting import (
    A2ACall,
    CallAccounting,
    CallRecord,
    a2a_tool,
    begin_a2a,
    counted_stream,
)
from .card import build_agent_card
from .database import initialize_database
from .guides import guide_response
from .protocol import A2AProtocol, parse_error_response
from .security import require_bearer
from .signals import Signals
from .store import HubStore
from .sweeper import start_sweeper, stop_sweeper

logger = logging.getLogger(__name__)

# Sent by worker-mcp on every request; only the guide route reads it (#78).
AGENT_HEADER = "X-Hub-Agent"


def create_app(settings: HubSettings | None = None) -> FastAPI:
    """Create a configured hub application without starting a server."""

    resolved = settings or HubSettings.from_env()
    card = build_agent_card(resolved.public_url)
    store = HubStore(
        path=resolved.database_path,
        signals=Signals(),
        default_event_lease_s=resolved.event_lease_s,
    )
    protocol = A2AProtocol(store=store, settings=resolved)
    accounting = CallAccounting(
        resolved.database_path,
        enabled=resolved.call_accounting,
        jsonl_path=resolved.call_log_jsonl,
    )

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        initialize_database(resolved.database_path)
        app.state.bearer_token = load_or_create_token(resolved.token, resolved.token_file)
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
    app.state.store = store
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
        return {"status": "ok"}

    @app.get("/guides/{role}.md", include_in_schema=False, dependencies=[Depends(require_bearer)])
    async def role_guide(role: str, request: Request) -> Response:
        if not accounting.enabled:
            return guide_response(resolved.guides_dir, role)
        call = begin_a2a("get_role_guide")
        # The guide route carries no A2A identity. worker-mcp names itself in a
        # header, which is counted only when it is a registered agent's name.
        claimed = request.headers.get(AGENT_HEADER, "")
        if claimed and store.agent_by_name(claimed) is not None:
            call.actor = claimed
        try:
            response = guide_response(resolved.guides_dir, role)
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
