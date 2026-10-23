"""A2A HTTP client for worker MCP tools."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
from collections.abc import Mapping
from contextlib import suppress
from dataclasses import asdict, replace
from pathlib import Path
from typing import Any
from uuid import uuid4

import httpx
from agent_hub_common import (
    AGENT_HEADER,
    SCHEMA_VERSION,
    UNKNOWN,
    AgentProfile,
    ImplementerResult,
    MetaKeys,
    ModelSource,
    RebaseResult,
    ReviewerResult,
    StandingRole,
    validate_agent_name,
)
from agent_hub_common.discovery import discover
from agent_hub_common.workspace import canonical_workspace, read_identity

from .config import WorkerSettings
from .orchestrator import OrchestratorBridge
from .telemetry import TelemetryLog, count_http_exchange, count_http_retry

logger = logging.getLogger(__name__)

ROLE_SLUG_RE = re.compile(r"^[a-z][a-z0-9-]*$")
RETRYABLE_STATUS_CODES = frozenset({502, 503, 504})


class WorkerProtocolError(Exception):
    """Raised when the hub returns a JSON-RPC error or protocol failure."""

    def __init__(self, code: int | None, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


class WorkerHubClient:
    """HTTP client communicating with the agent-hub over A2A and guide routes."""

    def __init__(
        self,
        settings: WorkerSettings,
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        self.settings = settings
        self._external_client = http_client
        self._client: httpx.AsyncClient | None = http_client
        self.context_id: str | None = None
        self.standing_role: StandingRole | None = None
        self._joined: bool = False
        self.orchestrator: OrchestratorBridge | None = None
        self.worker_instance_id = uuid4().hex
        self.telemetry = TelemetryLog(
            settings.telemetry_log,
            agent=settings.agent_name,
            worker_instance_id=self.worker_instance_id,
            session_fields={
                # The address this worker dialed, to set beside the peer
                # address the hub recorded for it (#126).
                "hub_url": settings.hub_url,
                "harness": settings.profile.harness,
                "harness_version": settings.profile.harness_version,
                "provider": settings.profile.provider,
                "model": settings.profile.model,
                "model_source": settings.profile.model_source.value,
                "heartbeat_s": settings.heartbeat_s,
                "max_retries": settings.max_retries,
            },
        )
        self.current_task_id: str | None = None
        self._heartbeat_task: asyncio.Task[None] | None = None
        # Active pending question (question_text, message_id) per task (§4.1)
        self._pending_questions: dict[str, tuple[str, str]] = {}
        # Active pending result (result_dict, operation_id) per task (§4.1)
        self._pending_results: dict[str, tuple[dict[str, Any], str]] = {}
        # Active pending check-in (profile, operation_id)
        self._pending_checkin: tuple[AgentProfile, str] | None = None
        # Active pending progress (note, operation_id) per task (§4.1)
        self._pending_progress: dict[str, tuple[str, str]] = {}

    @property
    def is_joined(self) -> bool:
        return self._joined or bool(self.context_id)

    async def __aenter__(self) -> WorkerHubClient:
        if self._client is None and self.settings.hub_url:
            self._client = httpx.AsyncClient(
                base_url=self.settings.hub_url,
                headers={"Authorization": f"Bearer {self.settings.token}"},
            )
        self.start_heartbeat()
        return self

    async def __aexit__(self, *args: object) -> None:
        await self.close()

    async def close(self) -> None:
        await self.stop_heartbeat()
        if self.orchestrator is not None:
            await self.orchestrator.close()
        await self.detach()
        if self._external_client is None and self._client is not None:
            await self._client.aclose()
            self._client = None
        self.telemetry.emit("session_stopped")

    def start_heartbeat(self) -> None:
        """Start the process-level heartbeat timer once."""

        if self._heartbeat_task is None or self._heartbeat_task.done():
            self._heartbeat_task = asyncio.create_task(
                self._heartbeat_loop(), name=f"worker-heartbeat-{self.settings.agent_name}"
            )

    async def stop_heartbeat(self) -> None:
        task = self._heartbeat_task
        self._heartbeat_task = None
        if task is None:
            return
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task

    async def _heartbeat_loop(self) -> None:
        while True:
            await asyncio.sleep(self.settings.heartbeat_s)
            if self.context_id is None:
                continue
            try:
                accepted = await self.heartbeat()
                self.telemetry.emit(
                    "heartbeat",
                    phase="success",
                    accepted=accepted,
                    current_task_id=self.current_task_id,
                    hub_url=self.settings.hub_url,
                )
            except Exception as exc:
                # The normal request retry policy has already been exhausted;
                # keep worker MCP alive so the next timer tick can recover.
                self.telemetry.emit(
                    "heartbeat",
                    phase="error",
                    error_type=type(exc).__name__,
                    error=str(exc)[:500],
                    current_task_id=self.current_task_id,
                    hub_url=self.settings.hub_url,
                )
                logger.exception("Background heartbeat failed")

    async def detach(self) -> None:
        """Tell the hub this instance is stopping, so a successor can check in at once.

        Best effort: the hub keeps the agent's status and task, and still
        declares it lost if no successor checks in within its lost window (#115).
        """

        if self.context_id is None:
            return
        try:
            accepted = await self.heartbeat(detach=True)
        except Exception as exc:
            self.telemetry.emit("detach", phase="error", error_type=type(exc).__name__)
            logger.warning("Could not detach from the hub: %s", exc)
            return
        self.telemetry.emit("detach", phase="success", accepted=accepted)

    async def heartbeat(self, *, detach: bool = False) -> bool:
        """Send one timer heartbeat; stale instances receive an ignored ack.

        With ``detach`` it is this instance's last message instead (#115).
        """

        if self.context_id is None:
            raise RuntimeError("Worker has not checked in yet; call check_in first")
        metadata: dict[str, Any] = {
            MetaKeys.KIND: "heartbeat",
            MetaKeys.AGENT: self.settings.agent_name,
            MetaKeys.WORKER_INSTANCE_ID: self.worker_instance_id,
            MetaKeys.SCHEMA_VERSION: SCHEMA_VERSION,
        }
        if detach:
            metadata[MetaKeys.DETACH] = True
        if self.current_task_id is not None:
            metadata[MetaKeys.CURRENT_TASK_ID] = self.current_task_id
        result = await self._post_rpc(
            "message/send",
            {
                "message": {
                    "messageId": uuid4().hex,
                    "contextId": self.context_id,
                    "role": "user",
                    "parts": [{"kind": "text", "text": "HEARTBEAT"}],
                    "metadata": metadata,
                }
            },
        )
        if not isinstance(result, dict):
            raise WorkerProtocolError(None, "heartbeat response was not a dict")
        return (result.get("metadata") or {}).get(MetaKeys.ACCEPTED) is True

    def _headers(self) -> dict[str, str]:
        headers: dict[str, str] = {}
        if self.settings.token:
            headers["Authorization"] = f"Bearer {self.settings.token}"
        # Names the caller of the identity-less guide route for the hub's
        # call accounting (#78); the hub counts it only for a registered agent.
        if self.settings.agent_name.isascii() and self.settings.agent_name:
            headers["X-Hub-Agent"] = self.settings.agent_name
        return headers

    def _ensure_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                base_url=self.settings.hub_url,
                headers={"Authorization": f"Bearer {self.settings.token}"},
            )
        return self._client

    async def _request_with_retry(
        self,
        method: str,
        path: str,
        *,
        json_body: dict[str, Any] | None = None,
        headers: Mapping[str, str] | None = None,
        timeout: float | None = None,
    ) -> httpx.Response:
        client = self._ensure_client()
        attempts = 0
        req_headers = {**self._headers(), **dict(headers or {})}
        while True:
            try:
                response = await client.request(
                    method,
                    path,
                    json=json_body,
                    timeout=timeout,
                    headers=req_headers,
                )
                retryable = (
                    response.status_code in RETRYABLE_STATUS_CODES
                    and attempts < self.settings.max_retries
                )
                if retryable:
                    attempts += 1
                    count_http_exchange(response, retried=True)
                    delay = self.settings.backoff_factor_s * (2 ** (attempts - 1))
                    self.telemetry.emit(
                        "retry",
                        operation=f"{method} {path}",
                        attempt=attempts,
                        max_retries=self.settings.max_retries,
                        reason=f"http_{response.status_code}",
                        delay_s=delay,
                    )
                    logger.warning(
                        "Hub returned %s on %s %s; retrying in %.2fs (attempt %d/%d)",
                        response.status_code,
                        method,
                        path,
                        delay,
                        attempts,
                        self.settings.max_retries,
                    )
                    await asyncio.sleep(delay)
                    continue
                count_http_exchange(response)
                return response
            except (httpx.TransportError, httpx.TimeoutException) as exc:
                if attempts < self.settings.max_retries:
                    attempts += 1
                    count_http_retry()
                    delay = self.settings.backoff_factor_s * (2 ** (attempts - 1))
                    self.telemetry.emit(
                        "retry",
                        operation=f"{method} {path}",
                        attempt=attempts,
                        max_retries=self.settings.max_retries,
                        reason=type(exc).__name__,
                        delay_s=delay,
                    )
                    logger.warning(
                        "Transport or timeout error (%s) on %s %s; retrying in %.2fs "
                        "(attempt %d/%d)",
                        exc,
                        method,
                        path,
                        delay,
                        attempts,
                        self.settings.max_retries,
                    )
                    await asyncio.sleep(delay)
                    continue
                raise

    async def _post_rpc(self, method: str, params: dict[str, Any], *, path: str = "/a2a") -> Any:
        payload = {
            "jsonrpc": "2.0",
            "id": uuid4().hex,
            "method": method,
            "params": params,
        }
        response = await self._request_with_retry("POST", path, json_body=payload)
        if response.status_code >= 400:
            try:
                data = response.json()
                if isinstance(data, dict) and "error" in data:
                    err = data["error"]
                    code = err.get("code") if isinstance(err, dict) else None
                    msg = (
                        err.get("message", "Unknown JSON-RPC error")
                        if isinstance(err, dict)
                        else str(err)
                    )
                    raise WorkerProtocolError(code, msg)
            except (json.JSONDecodeError, ValueError):
                pass
            response.raise_for_status()
        data = response.json()
        if not isinstance(data, dict):
            raise WorkerProtocolError(None, "Hub response was not a JSON object")
        if "error" in data:
            err = data["error"]
            code = err.get("code") if isinstance(err, dict) else None
            msg = (
                err.get("message", "Unknown JSON-RPC error") if isinstance(err, dict) else str(err)
            )
            raise WorkerProtocolError(code, msg)
        return data.get("result")

    async def _stream_rpc(
        self,
        method: str,
        params: dict[str, Any],
        hold_timeout_s: float,
    ) -> Any:
        client = self._ensure_client()
        payload = {
            "jsonrpc": "2.0",
            "id": uuid4().hex,
            "method": method,
            "params": params,
        }
        # Client read timeout must comfortably exceed the requested server hold timeout
        client_timeout = hold_timeout_s + 15.0
        attempts = 0
        while True:
            try:
                async with client.stream(
                    "POST",
                    "/a2a",
                    json=payload,
                    timeout=client_timeout,
                    headers=self._headers(),
                ) as response:
                    retryable = (
                        response.status_code in RETRYABLE_STATUS_CODES
                        and attempts < self.settings.max_retries
                    )
                    if retryable:
                        attempts += 1
                        # Read the retried body so its bytes count too.
                        await response.aread()
                        count_http_exchange(response, retried=True)
                        delay = self.settings.backoff_factor_s * (2 ** (attempts - 1))
                        self.telemetry.emit(
                            "retry",
                            operation=method,
                            attempt=attempts,
                            max_retries=self.settings.max_retries,
                            reason=f"http_{response.status_code}",
                            delay_s=delay,
                        )
                        logger.warning(
                            "Hub returned %s on stream; retrying in %.2fs (attempt %d/%d)",
                            response.status_code,
                            delay,
                            attempts,
                            self.settings.max_retries,
                        )
                        await asyncio.sleep(delay)
                        continue
                    # Counted however the body ends: a result, an error or a
                    # closed stream all cost the bytes read so far.
                    try:
                        if response.status_code >= 400:
                            content = await response.aread()
                            try:
                                data = json.loads(content)
                                if isinstance(data, dict) and "error" in data:
                                    err = data["error"]
                                    code = err.get("code") if isinstance(err, dict) else None
                                    msg = (
                                        err.get("message", "Unknown JSON-RPC error")
                                        if isinstance(err, dict)
                                        else str(err)
                                    )
                                    raise WorkerProtocolError(code, msg)
                            except (json.JSONDecodeError, ValueError):
                                pass
                            response.raise_for_status()
                        content_type = response.headers.get("content-type", "")
                        if "text/event-stream" not in content_type:
                            content = await response.aread()
                            try:
                                data = json.loads(content)
                            except Exception as exc:
                                raw = content.decode("utf-8", errors="replace")[:200]
                                raise WorkerProtocolError(
                                    None,
                                    f"Hub returned non-SSE response: {raw}",
                                ) from exc
                            if isinstance(data, dict) and "error" in data:
                                err = data["error"]
                                code = err.get("code") if isinstance(err, dict) else None
                                msg = (
                                    err.get("message", "Unknown JSON-RPC error")
                                    if isinstance(err, dict)
                                    else str(err)
                                )
                                raise WorkerProtocolError(code, msg)
                            if isinstance(data, dict) and "result" in data:
                                return data["result"]
                            raise WorkerProtocolError(
                                None, f"Hub returned unexpected non-streaming response: {data}"
                            )

                        async for line in response.aiter_lines():
                            line = line.strip()
                            if line.startswith("data:"):
                                raw = line.removeprefix("data:").strip()
                                if not raw:
                                    continue
                                data = json.loads(raw)
                                if not isinstance(data, dict):
                                    raise WorkerProtocolError(
                                        None, "SSE chunk was not a JSON object"
                                    )
                                if "error" in data:
                                    err = data["error"]
                                    code = err.get("code") if isinstance(err, dict) else None
                                    msg = (
                                        err.get("message", "Unknown JSON-RPC error")
                                        if isinstance(err, dict)
                                        else str(err)
                                    )
                                    raise WorkerProtocolError(code, msg)
                                return data.get("result")
                        raise WorkerProtocolError(
                            None, "SSE stream closed without delivering a data event"
                        )
                    finally:
                        count_http_exchange(response)
            except (httpx.TransportError, httpx.TimeoutException) as exc:
                if attempts < self.settings.max_retries:
                    attempts += 1
                    count_http_retry()
                    delay = self.settings.backoff_factor_s * (2 ** (attempts - 1))
                    self.telemetry.emit(
                        "retry",
                        operation=method,
                        attempt=attempts,
                        max_retries=self.settings.max_retries,
                        reason=type(exc).__name__,
                        delay_s=delay,
                    )
                    logger.warning(
                        "Transport or timeout error (%s) on stream; retrying in %.2fs "
                        "(attempt %d/%d)",
                        exc,
                        delay,
                        attempts,
                        self.settings.max_retries,
                    )
                    await asyncio.sleep(delay)
                    continue
                raise

    async def _bind_hub(self, hub: str | None = None) -> None:
        if hub is not None or (not self.settings.hub_url and self._external_client is None):
            cwd = self.settings.cwd or Path.cwd()
            endpoint = discover(cwd, self.settings.environ, selector=hub)
            if (
                self._external_client is None
                and self._client is not None
                and (self.settings.hub_url != endpoint.url or self.settings.token != endpoint.token)
            ):
                await self._client.aclose()
                self._client = None
            self.settings = replace(self.settings, hub_url=endpoint.url, token=endpoint.token)
        elif not self.settings.hub_url and self._external_client is not None:
            self.settings = replace(
                self.settings, hub_url=str(self._external_client.base_url).rstrip("/")
            )

    def profile(
        self,
        capabilities: list[str] | None = None,
        model: str | None = None,
        *,
        effort: str | None = None,
        harness: str | None = None,
    ) -> AgentProfile:
        """Combine the launcher's profile with what the agent declares.

        Declared capabilities add to the configured ones. A declared model is
        used as `model` only when the launcher names none: the launcher is the
        operator's statement of what runs, the agent's is its own belief (§3).
        It is reported as `declared_model` either way, so a stale `HUB_MODEL`
        leaves a trace instead of silently winning (#77).
        """

        configured = self.settings.profile
        declared_caps = (item.strip() for item in capabilities or [])
        merged = dict.fromkeys([*configured.capabilities, *(c for c in declared_caps if c)])
        declared_model = (model or "").strip() or UNKNOWN
        if configured.model_source is ModelSource.UNKNOWN and declared_model != UNKNOWN:
            resolved_model = declared_model
            resolved_model_source = ModelSource.DECLARED
        else:
            resolved_model = configured.model
            resolved_model_source = configured.model_source

        declared_effort = (effort or "").strip() or UNKNOWN
        if configured.effort_source is ModelSource.UNKNOWN and declared_effort != UNKNOWN:
            resolved_effort = declared_effort
            resolved_effort_source = ModelSource.DECLARED
        else:
            resolved_effort = configured.effort
            resolved_effort_source = configured.effort_source

        declared_harness = (harness or "").strip() or UNKNOWN
        resolved_harness_source: ModelSource
        if configured.harness != UNKNOWN:
            resolved_harness = configured.harness
            resolved_harness_source = (
                configured.harness_source
                if configured.harness_source is not ModelSource.UNKNOWN
                else ModelSource.ENV
            )
        elif declared_harness != UNKNOWN:
            resolved_harness = declared_harness
            resolved_harness_source = ModelSource.DECLARED
        else:
            resolved_harness = UNKNOWN
            resolved_harness_source = ModelSource.UNKNOWN

        return replace(
            configured,
            capabilities=tuple(merged),
            harness=resolved_harness,
            harness_source=resolved_harness_source,
            model=resolved_model,
            model_source=resolved_model_source,
            declared_model=declared_model,
            effort=resolved_effort,
            effort_source=resolved_effort_source,
        )

    async def check_in(
        self,
        capabilities: list[str] | None = None,
        model: str | None = None,
        operation_id: str | None = None,
    ) -> dict[str, Any]:
        """Register the worker with the hub and store the returned contextId."""
        await self._bind_hub()
        profile = self.profile(capabilities, model)
        if operation_id is not None:
            op_id = operation_id
        else:
            if self._pending_checkin is not None and self._pending_checkin[0] == profile:
                op_id = self._pending_checkin[1]
            else:
                op_id = uuid4().hex
                self._pending_checkin = (profile, op_id)

        metadata: dict[str, Any] = {
            MetaKeys.AGENT: self.settings.agent_name,
            MetaKeys.CAPABILITIES: list(profile.capabilities),
            MetaKeys.HARNESS: profile.harness,
            MetaKeys.HARNESS_SOURCE: profile.harness_source.value,
            MetaKeys.HARNESS_VERSION: profile.harness_version,
            MetaKeys.PROVIDER: profile.provider,
            MetaKeys.MODEL: profile.model,
            MetaKeys.MODEL_SOURCE: profile.model_source.value,
            MetaKeys.DECLARED_MODEL: profile.declared_model,
            MetaKeys.EFFORT: profile.effort,
            MetaKeys.EFFORT_SOURCE: profile.effort_source.value,
            MetaKeys.SCHEMA_VERSION: SCHEMA_VERSION,
            MetaKeys.OPERATION_ID: op_id,
            MetaKeys.WORKER_INSTANCE_ID: self.worker_instance_id,
        }
        if profile.workspace_id is not None:
            metadata[MetaKeys.WORKSPACE_ID] = profile.workspace_id

        params = {
            "message": {
                "messageId": uuid4().hex,
                "role": "user",
                "parts": [{"kind": "text", "text": "READY"}],
                "metadata": metadata,
            }
        }
        result = await self._post_rpc("message/send", params)
        if not isinstance(result, dict):
            raise WorkerProtocolError(None, "check_in response was not a dict")
        context_id = result.get("contextId") or (result.get("metadata") or {}).get("contextId")
        if not context_id or not isinstance(context_id, str):
            raise WorkerProtocolError(None, "check_in response did not contain contextId")
        self.context_id = context_id
        self.standing_role = StandingRole.WORKER
        self._joined = True
        self.current_task_id = None
        self._pending_checkin = None
        return {
            "status": "registered",
            "agent": self.settings.agent_name,
            "context_id": self.context_id,
            "profile": {
                "harness": profile.harness,
                "harness_source": profile.harness_source.value,
                "harness_version": profile.harness_version,
                "provider": profile.provider,
                "model": profile.model,
                "model_source": profile.model_source.value,
                "effort": profile.effort,
                "effort_source": profile.effort_source.value,
                "declared_model": profile.declared_model,
                "capabilities": list(profile.capabilities),
                "workspace_id": profile.workspace_id,
            },
        }

    async def join(
        self,
        name: str,
        role: str,
        *,
        model: str | None = None,
        effort: str | None = None,
        harness: str | None = None,
        hub: str | None = None,
        takeover: bool = False,
        capabilities: list[str] | None = None,
        operation_id: str | None = None,
    ) -> dict[str, Any]:
        """Register this worker by name and standing role and fetch its role guide (§5)."""

        clean_name = validate_agent_name(name.strip())
        clean_role = role.strip().lower()
        try:
            standing_role = StandingRole(clean_role)
        except ValueError as exc:
            allowed = ", ".join(r.value for r in StandingRole)
            raise ValueError(f"role must be one of {allowed} (got {role!r})") from exc
        if self.is_joined and (
            self.settings.agent_name != clean_name or self.standing_role != standing_role
        ):
            raise ValueError("this bridge has already joined under another name or role")

        await self._bind_hub(hub)
        profile = self.profile(capabilities, model, effort=effort, harness=harness)

        bridge_cwd = (self.settings.cwd or Path.cwd()).resolve()
        env = self.settings.environ or {}
        explicit_raw_env = (env.get("HUB_WORKSPACE") or "").strip()
        # Standalone `run_worker` builds settings with `from_env()`, which
        # records the validated clone in `settings.workspace` but leaves
        # `environ` unset (r1-2 on #244). Honor that path too, so an explicit
        # clone never falls through to worktree creation.
        configured_workspace = self.settings.workspace
        if explicit_raw_env:
            explicit_raw = explicit_raw_env
            workspace_explicit = True
        elif configured_workspace is not None:
            explicit_raw = str(configured_workspace)
            workspace_explicit = True
        else:
            explicit_raw = ""
            workspace_explicit = False
        if workspace_explicit:
            try:
                canonical_path = canonical_workspace(explicit_raw).resolve()
            except Exception:
                # An invalid explicit workspace is the hub's to refuse (r2-2
                # on #244), not a bridge crash: send the raw path with no
                # identity and let the hub name it. Clear any stale ID so a
                # configured clone for another agent cannot authorize this join.
                resolved_workspace_path = bridge_cwd
                reported_workspace_path = explicit_raw
                profile = replace(profile, workspace_id=None)
            else:
                resolved_workspace_path = canonical_path
                reported_workspace_path = str(canonical_path)
                # Revalidate for the name passed to each join (r1-1 on #244):
                # a profile ID configured for another agent (e.g. via
                # `from_discovered` with AGENT_NAME=bob joining as charlie)
                # must not survive. A failure clears to None so the hub
                # refuses the explicit mismatch.
                try:
                    identity = read_identity(canonical_path, clean_name)
                    profile = replace(profile, workspace_id=identity["workspace_id"])
                except Exception:
                    profile = replace(profile, workspace_id=None)
        else:
            resolved_workspace_path = bridge_cwd
            reported_workspace_path = str(bridge_cwd)
            if profile.workspace_id is None:
                try:
                    identity = read_identity(resolved_workspace_path, clean_name)
                    profile = replace(profile, workspace_id=identity["workspace_id"])
                except Exception:
                    pass

        if standing_role is StandingRole.ORCHESTRATOR:
            if self.orchestrator is None:
                self.orchestrator = OrchestratorBridge(
                    clean_name,
                    cwd=bridge_cwd,
                    http_client=self._ensure_client(),
                )
            self.orchestrator.name = clean_name
            result = await self.orchestrator.register(
                profile,
                takeover=takeover,
                workspace_path=reported_workspace_path,
                workspace_explicit=workspace_explicit,
            )
            self.settings = replace(self.settings, agent_name=clean_name)
            self.standing_role = standing_role
            self._joined = True
            hub_path = result.get("workspace_path") or reported_workspace_path
            try:
                cwd_inside = bridge_cwd == Path(
                    str(hub_path)
                ).resolve() or bridge_cwd.is_relative_to(Path(str(hub_path)).resolve())
            except Exception:
                cwd_inside = False
            pinned_profile = dict(asdict(profile))
            for _key in ("model", "model_source", "effort", "effort_source"):
                if isinstance(result.get(_key), str) and result.get(_key):
                    pinned_profile[_key] = result.get(_key)
            if isinstance(result.get("workspace_id"), str):
                pinned_profile["workspace_id"] = result.get("workspace_id")
            return result | {
                "workspace_path": str(hub_path),
                "workspace_id": result.get("workspace_id", profile.workspace_id),
                "cwd_inside_workspace": cwd_inside,
                "profile": pinned_profile,
            }

        self.settings = replace(self.settings, agent_name=clean_name)
        self.telemetry.agent = clean_name
        op_id = operation_id or uuid4().hex
        metadata: dict[str, Any] = {
            MetaKeys.AGENT: clean_name,
            MetaKeys.STANDING_ROLE: standing_role.value,
            MetaKeys.TAKEOVER: takeover,
            MetaKeys.CAPABILITIES: list(profile.capabilities),
            MetaKeys.HARNESS: profile.harness,
            MetaKeys.HARNESS_SOURCE: profile.harness_source.value,
            MetaKeys.HARNESS_VERSION: profile.harness_version,
            MetaKeys.PROVIDER: profile.provider,
            MetaKeys.MODEL: profile.model,
            MetaKeys.MODEL_SOURCE: profile.model_source.value,
            MetaKeys.DECLARED_MODEL: profile.declared_model,
            MetaKeys.EFFORT: profile.effort,
            MetaKeys.EFFORT_SOURCE: profile.effort_source.value,
            MetaKeys.SCHEMA_VERSION: SCHEMA_VERSION,
            MetaKeys.OPERATION_ID: op_id,
            MetaKeys.WORKER_INSTANCE_ID: self.worker_instance_id,
            MetaKeys.WORKSPACE_EXPLICIT: workspace_explicit,
            MetaKeys.WORKSPACE_PATH: reported_workspace_path,
        }
        if profile.workspace_id is not None:
            metadata[MetaKeys.WORKSPACE_ID] = profile.workspace_id

        params = {
            "message": {
                "messageId": uuid4().hex,
                "role": "user",
                "parts": [{"kind": "text", "text": "READY"}],
                "metadata": metadata,
            }
        }
        result = await self._post_rpc("message/send", params)
        if not isinstance(result, dict):
            raise WorkerProtocolError(None, "join response was not a dict")
        res_meta = result.get("metadata") or {}
        context_id = result.get("contextId") or res_meta.get("contextId")
        if not context_id or not isinstance(context_id, str):
            raise WorkerProtocolError(None, "join response did not contain contextId")
        self.context_id = context_id
        self.standing_role = standing_role
        self._joined = True
        reattached_task = res_meta.get(MetaKeys.REATTACHED_TASK)
        if isinstance(reattached_task, dict) and reattached_task.get("task_id"):
            self.current_task_id = str(reattached_task["task_id"])
        else:
            reattached_task = None
            self.current_task_id = None
        self._pending_checkin = None

        worker_guide = await self.get_role_guide("worker")
        worker_guide_sha256 = hashlib.sha256(worker_guide.encode("utf-8")).hexdigest()
        role_guide = await self.get_role_guide(standing_role.value)

        who = await self._request_with_retry("GET", "/whoami")
        who.raise_for_status()
        who_data = who.json()
        if not isinstance(who_data, dict):
            raise WorkerProtocolError(None, "GET /whoami did not return a JSON object")
        hub_summary = {
            "hub_id": who_data.get("hub_id"),
            "name": who_data.get("hub_name") or who_data.get("name"),
            "repository": who_data.get("repository"),
            "checkout": who_data.get("checkout"),
            "url": who_data.get("url") or who_data.get("hub") or self.settings.hub_url,
            "port": who_data.get("port"),
            "forge": who_data.get("forge"),
            "default_branch": who_data.get("default_branch"),
        }

        hub_workspace_path = res_meta.get(MetaKeys.WORKSPACE_PATH)
        hub_workspace_id = res_meta.get(MetaKeys.WORKSPACE_ID)
        hub_workspace_kind = res_meta.get(MetaKeys.WORKSPACE_KIND)
        # M2 Step 7b (#314): the join response carries `setup` and the applied
        # model/effort pins from the configuration layers.
        hub_setup_raw = res_meta.get(MetaKeys.SETUP)
        hub_setup = str(hub_setup_raw) if isinstance(hub_setup_raw, str) and hub_setup_raw else None
        hub_model_raw = res_meta.get(MetaKeys.MODEL)
        hub_model_source_raw = res_meta.get(MetaKeys.MODEL_SOURCE)
        hub_effort_raw = res_meta.get(MetaKeys.EFFORT)
        hub_effort_source_raw = res_meta.get(MetaKeys.EFFORT_SOURCE)
        final_model = (
            str(hub_model_raw)
            if isinstance(hub_model_raw, str) and hub_model_raw
            else profile.model
        )
        final_model_source = (
            str(hub_model_source_raw)
            if isinstance(hub_model_source_raw, str) and hub_model_source_raw
            else profile.model_source.value
        )
        final_effort = (
            str(hub_effort_raw)
            if isinstance(hub_effort_raw, str) and hub_effort_raw
            else profile.effort
        )
        final_effort_source = (
            str(hub_effort_source_raw)
            if isinstance(hub_effort_source_raw, str) and hub_effort_source_raw
            else profile.effort_source.value
        )
        final_workspace_path = (
            str(hub_workspace_path)
            if isinstance(hub_workspace_path, str) and hub_workspace_path
            else str(resolved_workspace_path)
        )
        final_workspace_id = (
            str(hub_workspace_id)
            if isinstance(hub_workspace_id, str) and hub_workspace_id
            else profile.workspace_id
        )
        try:
            final_cwd_inside = bridge_cwd == Path(final_workspace_path).resolve() or (
                bridge_cwd.is_relative_to(Path(final_workspace_path).resolve())
            )
        except Exception:
            final_cwd_inside = False
        profile_dict = {
            "harness": profile.harness,
            "harness_source": profile.harness_source.value,
            "harness_version": profile.harness_version,
            "provider": profile.provider,
            "model": final_model,
            "model_source": final_model_source,
            "effort": final_effort,
            "effort_source": final_effort_source,
            "declared_model": profile.declared_model,
            "capabilities": list(profile.capabilities),
            "workspace_id": final_workspace_id,
        }

        return {
            "status": "registered",
            "agent": clean_name,
            "role": standing_role.value,
            "standing_role": standing_role.value,
            "context_id": self.context_id,
            "worker_instance_id": self.worker_instance_id,
            "hub": hub_summary,
            "workspace_path": final_workspace_path,
            "workspace_id": final_workspace_id,
            "workspace_kind": hub_workspace_kind,
            "cwd_inside_workspace": final_cwd_inside,
            "setup": hub_setup,
            "profile": profile_dict,
            "role_guide": role_guide,
            "guide": worker_guide,
            "guide_sha256": worker_guide_sha256,
            "reattached_task": reattached_task,
            "next_call": "await_assignment",
        }

    async def whoami(self) -> dict[str, Any]:
        """Return the connected hub's identity and this bridge's join state (§5)."""

        if not self.is_joined:
            hub_summary: dict[str, Any] | None = None
            try:
                await self._bind_hub()
                response = await self._request_with_retry("GET", "/whoami")
                if response.status_code == 200:
                    who_data = response.json()
                    if isinstance(who_data, dict):
                        hub_summary = {
                            "hub_id": who_data.get("hub_id"),
                            "name": who_data.get("hub_name") or who_data.get("name"),
                            "repository": who_data.get("repository"),
                            "checkout": who_data.get("checkout"),
                            "url": (
                                who_data.get("url") or who_data.get("hub") or self.settings.hub_url
                            ),
                            "port": who_data.get("port"),
                            "forge": who_data.get("forge"),
                            "default_branch": who_data.get("default_branch"),
                        }
            except Exception as exc:
                return {"joined": False, "hub": None, "discovery_error": str(exc)}
            return {"joined": False, "hub": hub_summary}

        await self._bind_hub()
        headers = {AGENT_HEADER: self.settings.agent_name} if self.settings.agent_name else None
        response = await self._request_with_retry("GET", "/whoami", headers=headers)
        response.raise_for_status()
        data = response.json()
        if not isinstance(data, dict):
            raise WorkerProtocolError(None, "whoami response was not a dict")
        return {
            **data,
            "joined": self.is_joined,
            "agent_name": self.settings.agent_name or None,
            "standing_role": self.standing_role.value if self.standing_role is not None else None,
            "context_id": self.context_id,
            "worker_instance_id": self.worker_instance_id,
        }

    async def get_role_guide(self, role: str) -> str:
        """Fetch role guidance markdown from GET /guides/{role}.md (no local cache)."""
        clean_role = role.strip()
        if not ROLE_SLUG_RE.fullmatch(clean_role):
            raise ValueError(f"Role must be a slug matching [a-z][a-z0-9-]*, got {role!r}")
        response = await self._request_with_retry("GET", f"/guides/{clean_role}.md")
        if response.status_code == 404:
            raise FileNotFoundError(f"Role guide for {clean_role!r} not found (404)")
        response.raise_for_status()
        return response.text

    async def get_operator_answer(self, question_id: int) -> dict[str, Any]:
        """Read one operator question and its answer from the hub's `/rpc` (#129).

        A read with the bearer token alone, so it needs no check-in and is safe
        to retry.
        """
        result = await self._post_rpc(
            "hub.operator_answer", {"question_id": question_id}, path="/rpc"
        )
        if not isinstance(result, dict):
            raise WorkerProtocolError(None, "hub.operator_answer response was not a dict")
        return result

    async def get_statement(self, sha256: str) -> dict[str, Any]:
        """Read a submitted statement of work verbatim by its SHA-256 (M2 Step 4, #241).

        A read with the bearer token alone, like `get_operator_answer`: the
        bytes are stored verbatim (CRLF and trailing newline preserved) and
        returned unchanged. Unknown SHA raises with code -32001.
        """
        result = await self._post_rpc("hub.get_statement", {"sha256": sha256}, path="/rpc")
        if not isinstance(result, dict):
            raise WorkerProtocolError(None, "hub.get_statement response was not a dict")
        return result

    async def await_assignment(self, timeout_s: float | None = None) -> dict[str, Any]:
        """Poll the hub for the next task assignment, holding until assigned or released."""
        if not self.context_id:
            raise RuntimeError("Worker has not checked in yet; call check_in first")
        hold_s = timeout_s if timeout_s is not None else self.settings.default_wait_s
        params = {
            "message": {
                "messageId": uuid4().hex,
                "contextId": self.context_id,
                "role": "user",
                "parts": [{"kind": "text", "text": "NEXT"}],
                "metadata": {
                    MetaKeys.TIMEOUT_S: hold_s,
                    MetaKeys.WORKER_INSTANCE_ID: self.worker_instance_id,
                },
            }
        }
        result = await self._stream_rpc("message/stream", params, hold_s)
        if not isinstance(result, dict):
            return {"timeout": True}
        metadata = result.get("metadata") or {}
        if metadata.get(MetaKeys.RELEASE) is True:
            return {"release": True}
        if metadata.get(MetaKeys.TIMEOUT) is True:
            return {"timeout": True}

        # Assignment received (A2A Task object)
        task_id = result.get("id")
        role = metadata.get(MetaKeys.ROLE, "")
        status_msg = (result.get("status") or {}).get("message") or {}
        parts = status_msg.get("parts") or []
        instructions = ""
        for part in parts:
            if isinstance(part, dict) and "text" in part:
                instructions += part["text"]
            elif (
                isinstance(part, dict)
                and isinstance(part.get("root"), dict)
                and "text" in part["root"]
            ):
                instructions += part["root"]["text"]

        self.current_task_id = str(task_id) if task_id else None
        assignment: dict[str, Any] = {
            "task_id": str(task_id) if task_id else "",
            "role": str(role),
            "title": str(metadata.get(MetaKeys.TITLE, "")),
            "instructions": instructions,
        }
        pr_head_sha = metadata.get(MetaKeys.PR_HEAD_SHA)
        if isinstance(pr_head_sha, str) and pr_head_sha:
            assignment["pr_head_sha"] = pr_head_sha
        # Run identity travels with the assignment so the worker can cite it
        # and fetch the statement verbatim with `get_statement` (M2 Step 4, #9).
        run_id = metadata.get(MetaKeys.RUN_ID)
        if isinstance(run_id, str) and run_id:
            assignment["run_id"] = run_id
        statement_sha256 = metadata.get(MetaKeys.STATEMENT_SHA256)
        if isinstance(statement_sha256, str) and statement_sha256:
            assignment["statement_sha256"] = statement_sha256
        # Redelivery marker (#326): the hub returns the task the caller
        # already holds instead of timing out, flagged so the worker knows
        # it is not new work. Additive metadata; wire version stays 1.
        if metadata.get(MetaKeys.ALREADY_HELD) is True:
            assignment["already_held"] = True
        else:
            status_metadata = status_msg.get("metadata") or {}
            if status_metadata.get(MetaKeys.ALREADY_HELD) is True:
                assignment["already_held"] = True
        return assignment

    async def report_progress(
        self,
        task_id: str,
        note: str,
        operation_id: str | None = None,
    ) -> dict[str, Any]:
        """Send a progress note to Alice."""
        if not self.context_id:
            raise RuntimeError("Worker has not checked in yet; call check_in first")
        if operation_id is not None:
            op_id = operation_id
        else:
            pending = self._pending_progress.get(task_id)
            if pending is not None and pending[0] == note:
                op_id = pending[1]
            else:
                op_id = uuid4().hex
                self._pending_progress[task_id] = (note, op_id)
        params = {
            "message": {
                "messageId": uuid4().hex,
                "taskId": task_id,
                "contextId": self.context_id,
                "role": "user",
                "parts": [{"kind": "text", "text": note}],
                "metadata": {
                    MetaKeys.KIND: "progress",
                    MetaKeys.SCHEMA_VERSION: SCHEMA_VERSION,
                    MetaKeys.OPERATION_ID: op_id,
                    MetaKeys.WORKER_INSTANCE_ID: self.worker_instance_id,
                },
            }
        }
        await self._post_rpc("message/send", params)
        self._pending_progress.pop(task_id, None)
        return {"ok": True, "note": note}

    async def ask_alice(
        self,
        task_id: str,
        question: str,
        timeout_s: float | None = None,
    ) -> dict[str, Any]:
        """Ask Alice a question, holding until answered, overridden, or timed out.

        Retries of the same question on a task reuse the original messageId (§4.1)
        so answers given in gaps are not lost. Asking a different question generates
        a new messageId.
        """
        if not self.context_id:
            raise RuntimeError("Worker has not checked in yet; call check_in first")
        hold_s = timeout_s if timeout_s is not None else self.settings.default_wait_s

        # Re-use messageId if retrying the same question on this task
        pending = self._pending_questions.get(task_id)
        if pending is not None and pending[0] == question:
            message_id = pending[1]
        else:
            message_id = uuid4().hex
            self._pending_questions[task_id] = (question, message_id)

        params = {
            "message": {
                "messageId": message_id,
                "taskId": task_id,
                "contextId": self.context_id,
                "role": "user",
                "parts": [{"kind": "text", "text": question}],
                "metadata": {
                    MetaKeys.KIND: "question",
                    MetaKeys.TIMEOUT_S: hold_s,
                    MetaKeys.SCHEMA_VERSION: SCHEMA_VERSION,
                    MetaKeys.WORKER_INSTANCE_ID: self.worker_instance_id,
                },
            }
        }
        result = await self._stream_rpc("message/stream", params, hold_s)
        if not isinstance(result, dict):
            return {"timeout": True}

        # Check for manual termination during question (§4.1)
        status = result.get("status") or {}
        state = status.get("state")
        status_msg = status.get("message") or {}
        msg_metadata = status_msg.get("metadata") or {}
        overridden = msg_metadata.get(MetaKeys.KIND) == "state_override" or state in (
            "canceled",
            "failed",
            "completed",
        )
        if overridden:
            self._pending_questions.pop(task_id, None)
            self.current_task_id = None
            task_result = (result.get("metadata") or {}).get(MetaKeys.RESULT)
            note = task_result.get("summary") if isinstance(task_result, dict) else None
            if not note:
                parts = status_msg.get("parts") or []
                note = parts[0].get("text") if parts and isinstance(parts[0], dict) else state
            return {
                "task_ended": True,
                "state": state,
                "note": note,
            }

        # Check for hold timeout (§4.1)
        res_metadata = result.get("metadata") or {}
        if res_metadata.get(MetaKeys.TIMEOUT) is True:
            # Preserve retry_as_message_id if provided by hub
            retry_id = res_metadata.get(MetaKeys.RETRY_AS_MESSAGE_ID, message_id)
            self._pending_questions[task_id] = (question, retry_id)
            return {"timeout": True}

        # Normal reply from Alice
        self._pending_questions.pop(task_id, None)
        parts = result.get("parts") or []
        reply_text = ""
        for part in parts:
            if isinstance(part, dict) and "text" in part:
                reply_text += part["text"]
            elif (
                isinstance(part, dict)
                and isinstance(part.get("root"), dict)
                and "text" in part["root"]
            ):
                reply_text += part["root"]["text"]

        return {"reply": reply_text}

    async def ask_orchestrator(
        self,
        task_id: str,
        question: str,
        timeout_s: float | None = None,
    ) -> dict[str, Any]:
        """Ask the orchestrator a question, holding until answered, overridden, or timed out."""
        return await self.ask_alice(task_id, question, timeout_s)

    async def submit_result(
        self,
        task_id: str,
        result: ImplementerResult | ReviewerResult | RebaseResult | dict[str, Any] | str,
        summary: str | None = None,
        artifacts: list[Any] | None = None,
        operation_id: str | None = None,
    ) -> dict[str, Any]:
        """Report final typed result (or status/summary) for a task."""
        if not self.context_id:
            raise RuntimeError("Worker has not checked in yet; call check_in first")

        self._pending_questions.pop(task_id, None)

        if isinstance(result, (ImplementerResult, ReviewerResult, RebaseResult)):
            result_dict = result.model_dump(mode="json")
            summary_text = result.summary
        elif isinstance(result, dict):
            result_dict = result
            summary_text = str(result.get("summary", ""))
        elif isinstance(result, str):
            clean_status = result.strip().lower()
            if clean_status not in ("completed", "failed"):
                raise ValueError(f"status must be 'completed' or 'failed', got {result!r}")
            summary_text = summary or ""
            result_dict = {
                "outcome": clean_status,
                "summary": summary_text,
            }
            if artifacts:
                for art in artifacts:
                    if isinstance(art, dict) and "url" in art:
                        result_dict["pr_url"] = art["url"]
                        result_dict["head_sha"] = "0" * 40
        else:
            raise TypeError(
                "result must be ImplementerResult, ReviewerResult, RebaseResult, dict, or str, "
                f"got {type(result)}"
            )

        outcome = result_dict.get("outcome")
        verdict = result_dict.get("verdict")
        if outcome == "completed" or verdict in ("approved", "changes_requested"):
            terminal_status = "completed"
        elif outcome in ("blocked", "failed") or verdict == "failed":
            terminal_status = "failed"
        else:
            terminal_status = (
                "completed" if isinstance(result, str) and result == "completed" else "failed"
            )

        if operation_id is not None:
            op_id = operation_id
        else:
            pending = self._pending_results.get(task_id)
            if pending is not None and pending[0] == result_dict:
                op_id = pending[1]
            else:
                op_id = uuid4().hex
                self._pending_results[task_id] = (result_dict, op_id)

        metadata: dict[str, Any] = {
            MetaKeys.KIND: "result",
            MetaKeys.STATUS: terminal_status,
            MetaKeys.SCHEMA_VERSION: SCHEMA_VERSION,
            MetaKeys.OPERATION_ID: op_id,
            MetaKeys.RESULT: result_dict,
            MetaKeys.ARTIFACTS: artifacts or [],
            MetaKeys.WORKER_INSTANCE_ID: self.worker_instance_id,
        }

        params = {
            "message": {
                "messageId": uuid4().hex,
                "taskId": task_id,
                "contextId": self.context_id,
                "role": "user",
                "parts": [{"kind": "text", "text": summary_text}],
                "metadata": metadata,
            }
        }
        await self._post_rpc("message/send", params)
        self._pending_progress.pop(task_id, None)
        self.current_task_id = None
        return {
            "status": terminal_status,
            "task_id": task_id,
            "summary": summary_text,
            "result": result_dict,
        }
