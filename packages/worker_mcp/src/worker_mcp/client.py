"""A2A HTTP client for worker MCP tools."""

from __future__ import annotations

import asyncio
import json
import logging
import re
from contextlib import suppress
from dataclasses import asdict, replace
from typing import Any
from uuid import uuid4

import httpx
from agent_hub_common import (
    SCHEMA_VERSION,
    UNKNOWN,
    AgentProfile,
    ImplementerResult,
    MetaKeys,
    ModelSource,
    RebaseResult,
    ReviewerResult,
)

from .config import WorkerSettings
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

    async def __aenter__(self) -> WorkerHubClient:
        if self._client is None:
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

    async def heartbeat(self) -> bool:
        """Send one timer heartbeat; stale instances receive an ignored ack."""

        if self.context_id is None:
            raise RuntimeError("Worker has not checked in yet; call check_in first")
        metadata: dict[str, Any] = {
            MetaKeys.KIND: "heartbeat",
            MetaKeys.AGENT: self.settings.agent_name,
            MetaKeys.WORKER_INSTANCE_ID: self.worker_instance_id,
            MetaKeys.SCHEMA_VERSION: SCHEMA_VERSION,
        }
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
        headers = {"Authorization": f"Bearer {self.settings.token}"}
        # Names the caller of the identity-less guide route for the hub's
        # call accounting (#78); the hub counts it only for a registered agent.
        if self.settings.agent_name.isascii():
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
        timeout: float | None = None,
    ) -> httpx.Response:
        client = self._ensure_client()
        attempts = 0
        while True:
            try:
                response = await client.request(
                    method,
                    path,
                    json=json_body,
                    timeout=timeout,
                    headers=self._headers(),
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

    async def _post_rpc(self, method: str, params: dict[str, Any]) -> Any:
        payload = {
            "jsonrpc": "2.0",
            "id": uuid4().hex,
            "method": method,
            "params": params,
        }
        response = await self._request_with_retry("POST", "/a2a", json_body=payload)
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
                err.get("message", "Unknown JSON-RPC error")
                if isinstance(err, dict)
                else str(err)
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

    def profile(
        self, capabilities: list[str] | None = None, model: str | None = None
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
        declared_model = (model or "").strip()
        if declared_model == "":
            declared_model = UNKNOWN
        if configured.model_source is ModelSource.UNKNOWN and declared_model != UNKNOWN:
            return replace(
                configured,
                capabilities=tuple(merged),
                model=declared_model,
                model_source=ModelSource.DECLARED,
                declared_model=declared_model,
            )
        return replace(configured, capabilities=tuple(merged), declared_model=declared_model)

    async def check_in(
        self,
        capabilities: list[str] | None = None,
        model: str | None = None,
        operation_id: str | None = None,
    ) -> dict[str, Any]:
        """Register the worker with the hub and store the returned contextId."""
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
            MetaKeys.HARNESS_VERSION: profile.harness_version,
            MetaKeys.PROVIDER: profile.provider,
            MetaKeys.MODEL: profile.model,
            MetaKeys.MODEL_SOURCE: profile.model_source.value,
            MetaKeys.DECLARED_MODEL: profile.declared_model,
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
        self.current_task_id = None
        self._pending_checkin = None
        return {
            "status": "registered",
            "agent": self.settings.agent_name,
            "context_id": self.context_id,
            "profile": asdict(profile) | {"capabilities": list(profile.capabilities)},
        }

    async def get_role_guide(self, role: str) -> str:
        """Fetch role guidance markdown from GET /guides/{role}.md (no local cache)."""
        clean_role = role.strip()
        if not ROLE_SLUG_RE.fullmatch(clean_role):
            raise ValueError(
                f"Role must be a slug matching [a-z][a-z0-9-]*, got {role!r}"
            )
        response = await self._request_with_retry("GET", f"/guides/{clean_role}.md")
        if response.status_code == 404:
            raise FileNotFoundError(f"Role guide for {clean_role!r} not found (404)")
        response.raise_for_status()
        return response.text

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
        assignment = {
            "task_id": str(task_id) if task_id else "",
            "role": str(role),
            "title": str(metadata.get(MetaKeys.TITLE, "")),
            "instructions": instructions,
        }
        pr_head_sha = metadata.get(MetaKeys.PR_HEAD_SHA)
        if isinstance(pr_head_sha, str) and pr_head_sha:
            assignment["pr_head_sha"] = pr_head_sha
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
        overridden = (
            msg_metadata.get(MetaKeys.KIND) == "state_override"
            or state in ("canceled", "failed", "completed")
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
