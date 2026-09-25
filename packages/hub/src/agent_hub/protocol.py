"""The A2A JSON-RPC surface workers speak (spec §4.1).

Workers are A2A clients, so every pull-model intent has to ride on a standard
method. `message/send` carries the intents that return immediately; the two
that wait — get an assignment, ask Alice a question — use `message/stream`, and
the hub holds the SSE response open until the answer exists or the bounded
deadline passes. `tasks/get` and `tasks/cancel` are there for debugging.
"""

from __future__ import annotations

import hashlib
import json
import logging
from collections.abc import AsyncIterator, Mapping
from dataclasses import dataclass
from typing import Any
from uuid import uuid4

from a2a.types import (
    Artifact,
    CancelTaskRequest,
    GetTaskRequest,
    InternalError,
    InvalidParamsError,
    InvalidRequestError,
    JSONParseError,
    JSONRPCError,
    JSONRPCErrorResponse,
    JSONRPCSuccessResponse,
    MessageSendParams,
    MethodNotFoundError,
    Part,
    Role,
    SendMessageRequest,
    SendStreamingMessageRequest,
    Task,
    TaskIdParams,
    TaskNotCancelableError,
    TaskNotFoundError,
    TaskQueryParams,
    TaskStatus,
    TextPart,
    UnsupportedOperationError,
)
from a2a.types import Message as A2AMessage
from a2a.types import TaskState as A2ATaskState
from agent_hub_common import (
    MAX_MESSAGE_PART_BYTES,
    MAX_TYPED_RESULT_BYTES,
    UNKNOWN,
    AgentProfile,
    HubSettings,
    ImplementerResult,
    MetaKeys,
    ModelSource,
    RebaseResult,
    ReviewerResult,
    TaskResult,
    TaskRole,
    TaskState,
)
from fastapi import Response
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import ValidationError

from .accounting import note_a2a
from .store import (
    AgentRecord,
    ConflictError,
    DuplicateAgentError,
    HubStore,
    IdempotencyConflictError,
    MessageRecord,
    NotFoundError,
    PayloadTooLargeError,
    Released,
    TaskRecord,
    _json_size_bytes,
    _normalize_part,
)

logger = logging.getLogger(__name__)

CHECK_IN_TEXT = "READY"
NEXT_TEXT = "NEXT"
SSE_MEDIA_TYPE = "text/event-stream"
# Proxies that buffer would defeat the point of holding the response open.
SSE_HEADERS = {"Cache-Control": "no-cache", "X-Accel-Buffering": "no"}

# The typed body each role's result is validated against (§4.4). A task whose
# role has no entry is validated by the shape of what the worker sent.
RESULT_MODELS: dict[TaskRole, type[TaskResult]] = {
    TaskRole.IMPLEMENTER: ImplementerResult,
    TaskRole.REVIEWER: ReviewerResult,
    TaskRole.REBASE: RebaseResult,
}

SUPPORTED_METHODS = frozenset({"message/send", "message/stream", "tasks/get", "tasks/cancel"})

RequestId = str | int | None
A2AErrorModel = (
    JSONRPCError
    | JSONParseError
    | InvalidParamsError
    | InvalidRequestError
    | MethodNotFoundError
    | TaskNotFoundError
    | TaskNotCancelableError
    | UnsupportedOperationError
    | InternalError
)


class ProtocolError(Exception):
    """A refusal that must be reported as a JSON-RPC error, not an exception."""

    def __init__(self, error: A2AErrorModel) -> None:
        super().__init__(error.message)
        self.error = error


class MissingRequiredFieldError(ProtocolError):
    """Raised when a required mutation field is missing."""

    def __init__(self, message: str) -> None:
        super().__init__(InvalidParamsError(message=message))


class ResultValidationError(ProtocolError):
    """Raised when a typed task result fails validation."""

    def __init__(self, message: str) -> None:
        super().__init__(InvalidParamsError(message=message))


class UnsupportedSchemaVersionError(ProtocolError):
    """Raised when an unsupported hub.schema_version is encountered."""

    def __init__(self, version: Any) -> None:
        super().__init__(InvalidParamsError(message=f"unsupported schema_version: {version!r}"))


def _invalid(message: str) -> ProtocolError:
    return ProtocolError(InvalidParamsError(message=message))


def _check_schema_version(metadata: Mapping[str, Any]) -> None:
    if MetaKeys.SCHEMA_VERSION in metadata:
        version = metadata[MetaKeys.SCHEMA_VERSION]
        if not isinstance(version, int) or isinstance(version, bool) or version != 1:
            raise UnsupportedSchemaVersionError(version)


def _require_schema_version(metadata: Mapping[str, Any]) -> None:
    if MetaKeys.SCHEMA_VERSION not in metadata:
        raise UnsupportedSchemaVersionError("metadata.hub.schema_version is required")
    version = metadata[MetaKeys.SCHEMA_VERSION]
    if not isinstance(version, int) or isinstance(version, bool) or version != 1:
        raise UnsupportedSchemaVersionError(version)


def _require_operation_id(metadata: Mapping[str, Any]) -> str:
    if MetaKeys.OPERATION_ID not in metadata:
        raise MissingRequiredFieldError(f"metadata.{MetaKeys.OPERATION_ID} is required")
    operation_id = metadata[MetaKeys.OPERATION_ID]
    if not isinstance(operation_id, str) or not operation_id.strip():
        raise MissingRequiredFieldError(
            f"metadata.{MetaKeys.OPERATION_ID} must be a non-empty string"
        )
    return operation_id.strip()


def _require_worker_instance_id(metadata: Mapping[str, Any]) -> str:
    value = metadata.get(MetaKeys.WORKER_INSTANCE_ID)
    if not isinstance(value, str) or not value.strip():
        raise MissingRequiredFieldError(
            f"metadata.{MetaKeys.WORKER_INSTANCE_ID} must be a non-empty string"
        )
    return value.strip()


def _require_result(metadata: Mapping[str, Any]) -> dict[str, Any]:
    if MetaKeys.RESULT not in metadata or metadata[MetaKeys.RESULT] is None:
        raise ResultValidationError(f"metadata.{MetaKeys.RESULT} is required")
    raw_result = metadata[MetaKeys.RESULT]
    if not isinstance(raw_result, dict):
        raise ResultValidationError(f"metadata.{MetaKeys.RESULT} must be an object")
    return dict(raw_result)


def _hash_payload(payload: dict[str, Any]) -> str:
    serialized = json.dumps(payload, sort_keys=True)
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def _error_response(
    request_id: RequestId,
    error: A2AErrorModel,
    status_code: int = 200,
) -> JSONResponse:
    body = JSONRPCErrorResponse(id=request_id, error=error)
    return JSONResponse(body.model_dump(mode="json", exclude_none=True), status_code=status_code)


def parse_error_response() -> JSONResponse:
    """Report a body that is not JSON at all, per JSON-RPC."""

    return _error_response(None, JSONParseError())


def _success_body(request_id: RequestId, result: Task | A2AMessage) -> dict[str, Any]:
    body = JSONRPCSuccessResponse(id=request_id, result=result)
    return body.model_dump(mode="json", exclude_none=True)


def _sse(chunk: Mapping[str, Any]) -> bytes:
    return f"data: {json.dumps(chunk)}\n\n".encode()


def _text(message: A2AMessage) -> str:
    """Join the message's text parts; non-text parts carry no worker intent."""

    return "".join(
        part.root.text for part in message.parts if isinstance(part.root, TextPart)
    ).strip()


def _metadata(message: A2AMessage) -> dict[str, Any]:
    return dict(message.metadata or {})


def _check_message_part_sizes(message: A2AMessage) -> None:
    """Reject every oversized A2A part, including intents not persisted."""

    for index, part in enumerate(message.parts, start=1):
        value = part.root.model_dump(mode="json", by_alias=True, exclude_none=True)
        part_size = _json_size_bytes(value)
        if part_size > MAX_MESSAGE_PART_BYTES:
            raise PayloadTooLargeError(
                f"message part {index} is {part_size} bytes; maximum is "
                f"{MAX_MESSAGE_PART_BYTES} bytes. Store work product in GitHub and "
                "send a URL or compact reference instead."
            )


def _string_list(value: Any, field: str) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise _invalid(f"metadata.{field} must be a list of strings")
    return [str(item) for item in value]


def _profile_text(metadata: Mapping[str, Any], key: MetaKeys) -> str | None:
    """Read one self-reported profile string; absent, blank or `unknown` is None."""

    value = metadata.get(key)
    if value is None:
        return None
    if not isinstance(value, str):
        raise _invalid(f"metadata.{key} must be a string")
    text = value.strip()
    return None if text in ("", UNKNOWN) else text


def _profile(metadata: Mapping[str, Any]) -> AgentProfile:
    """Read the identity profile a check-in carries (§4.3).

    Whatever the worker does not report is recorded as `unknown`. A model with
    no stated source is refused rather than assigned one, and a source with no
    model is dropped, so `model_source` always describes the recorded model.
    `hub.declared_model` is recorded beside it as reported and never feeds
    `model` (#77): a check-in from a worker that predates it reads `unknown`.
    """

    model = _profile_text(metadata, MetaKeys.MODEL)
    raw_source = _profile_text(metadata, MetaKeys.MODEL_SOURCE) or UNKNOWN
    try:
        source = ModelSource(raw_source)
    except ValueError as exc:
        allowed = ", ".join(item.value for item in ModelSource)
        raise _invalid(f"metadata.{MetaKeys.MODEL_SOURCE} must be one of {allowed}") from exc
    if model is None:
        source = ModelSource.UNKNOWN
    elif source is ModelSource.UNKNOWN:
        raise _invalid(
            f"metadata.{MetaKeys.MODEL} needs metadata.{MetaKeys.MODEL_SOURCE} "
            f"({ModelSource.ENV.value} or {ModelSource.DECLARED.value})"
        )
    capabilities = _string_list(metadata.get(MetaKeys.CAPABILITIES), MetaKeys.CAPABILITIES)
    return AgentProfile(
        harness=_profile_text(metadata, MetaKeys.HARNESS) or UNKNOWN,
        harness_version=_profile_text(metadata, MetaKeys.HARNESS_VERSION) or UNKNOWN,
        provider=_profile_text(metadata, MetaKeys.PROVIDER) or UNKNOWN,
        model=model or UNKNOWN,
        model_source=source,
        capabilities=tuple(dict.fromkeys(item.strip() for item in capabilities if item.strip())),
        workspace_id=_profile_text(metadata, MetaKeys.WORKSPACE_ID),
        declared_model=_profile_text(metadata, MetaKeys.DECLARED_MODEL) or UNKNOWN,
    )


def _agent_message(
    text: str,
    *,
    context_id: str | None = None,
    task_id: str | None = None,
    metadata: Mapping[str, Any] | None = None,
) -> A2AMessage:
    """Build a message from the hub back to a worker."""

    return A2AMessage(
        message_id=uuid4().hex,
        role=Role.agent,
        parts=[Part(root=TextPart(text=text))],
        context_id=context_id,
        task_id=task_id,
        metadata=dict(metadata) if metadata else None,
    )


def _stored_message(record: MessageRecord) -> A2AMessage:
    return A2AMessage(
        message_id=str(record.id),
        role=Role.user if record.direction == "to_alice" else Role.agent,
        parts=[Part.model_validate(_normalize_part(part)) for part in record.parts],
        context_id=record.context_id,
        task_id=record.task_id,
        metadata={MetaKeys.SENDER: record.sender, MetaKeys.TS: record.ts},
    )


def _artifacts(record: TaskRecord) -> list[Artifact] | None:
    """Expose a worker's reported artifacts — PR URL, SHAs — on the task."""

    result = record.result or {}
    artifacts = []
    reported = result.get("artifacts")
    if isinstance(reported, list):
        for item in reported:
            payload = item if isinstance(item, dict) else {"value": item}
            name = payload.get("name")
            artifacts.append(
                Artifact(
                    artifact_id=uuid4().hex,
                    name=str(name) if isinstance(name, str) else None,
                    parts=[Part(root=TextPart(text=json.dumps(payload, sort_keys=True)))],
                )
            )
    for field in ("pr_url", "head_sha", "reviewed_head_sha", "review_url"):
        val = result.get(field)
        if isinstance(val, str) and val:
            artifacts.append(
                Artifact(
                    artifact_id=uuid4().hex,
                    name=field,
                    parts=[Part(root=TextPart(text=val))],
                )
            )
    return artifacts or None


def _task_object(
    record: TaskRecord,
    context_id: str,
    history: list[A2AMessage] | None = None,
    status_message: A2AMessage | None = None,
) -> Task:
    """Render a stored task as the A2A Task a worker or debugger receives."""

    bound_head: dict[str, Any] = (
        {} if record.pr_head_sha is None else {MetaKeys.PR_HEAD_SHA: record.pr_head_sha}
    )
    if status_message is None:
        status_message = _agent_message(
            record.instructions,
            context_id=context_id,
            task_id=record.id,
            metadata={
                MetaKeys.KIND: "assignment",
                MetaKeys.ROLE: record.role,
                MetaKeys.TITLE: record.title,
            }
            | bound_head,
        )
    return Task(
        id=record.id,
        context_id=context_id,
        status=TaskStatus(
            state=A2ATaskState(record.state.value),
            timestamp=record.updated,
            message=status_message,
        ),
        history=history,
        artifacts=_artifacts(record),
        metadata={
            MetaKeys.ROLE: record.role,
            MetaKeys.TITLE: record.title,
            MetaKeys.ASSIGNEE: record.assignee,
            MetaKeys.LEASE_EXPIRES: record.lease_expires,
            MetaKeys.RESULT: record.result,
        }
        | bound_head,
    )


@dataclass(frozen=True, slots=True)
class A2AProtocol:
    """Maps A2A calls onto hub state. Holds no state of its own."""

    store: HubStore
    settings: HubSettings

    async def dispatch(self, payload: Any, remote_addr: str | None = None) -> Response:
        """Route one JSON-RPC request, turning refusals into error responses.

        remote_addr: the transport peer the request arrived from, recorded on
        check-in and heartbeat as an observation (#126). The caller takes it
        from the connection, never from anything the worker sent.
        """

        if not isinstance(payload, dict):
            return _error_response(None, InvalidRequestError())
        request_id = payload.get("id")
        request_id = request_id if isinstance(request_id, str | int) else None
        method = payload.get("method")
        if not isinstance(method, str):
            return _error_response(request_id, InvalidRequestError())
        if method not in SUPPORTED_METHODS:
            return _error_response(request_id, MethodNotFoundError())

        try:
            return await self._dispatch(method, payload, request_id, remote_addr)
        except (
            ResultValidationError,
            UnsupportedSchemaVersionError,
            MissingRequiredFieldError,
            PayloadTooLargeError,
        ) as exc:
            error = (
                exc.error
                if isinstance(exc, ProtocolError)
                else InvalidParamsError(message=str(exc))
            )
            return _error_response(request_id, error, status_code=400)
        except (DuplicateAgentError, IdempotencyConflictError) as exc:
            return _error_response(
                request_id, InvalidRequestError(message=str(exc)), status_code=409
            )
        except ProtocolError as exc:
            return _error_response(request_id, exc.error)
        except NotFoundError as exc:
            return _error_response(request_id, InvalidParamsError(message=str(exc)))
        except ConflictError as exc:
            return _error_response(request_id, InvalidRequestError(message=str(exc)))
        except Exception:
            logger.exception("Unhandled error serving %s", method)
            return _error_response(request_id, InternalError())

    async def _dispatch(
        self, method: str, payload: Any, request_id: RequestId, remote_addr: str | None
    ) -> Response:
        if method == "message/send":
            params = _validate(SendMessageRequest, payload).params
            _check_message_part_sizes(params.message)
            return JSONResponse(_success_body(request_id, self._send(params, remote_addr)))
        if method == "message/stream":
            params = _validate(SendStreamingMessageRequest, payload).params
            _check_message_part_sizes(params.message)
            return self._stream(request_id, params)
        if method == "tasks/get":
            query = _validate(GetTaskRequest, payload).params
            return JSONResponse(_success_body(request_id, self._get_task(query)))
        cancel = _validate(CancelTaskRequest, payload).params
        return JSONResponse(_success_body(request_id, self._cancel_task(cancel)))

    # -- message/send -------------------------------------------------------

    def _send(self, params: MessageSendParams, remote_addr: str | None) -> Task | A2AMessage:
        message = params.message
        metadata = _metadata(message)
        if message.task_id is None:
            if metadata.get(MetaKeys.KIND) == "heartbeat":
                return self._heartbeat(message, metadata, remote_addr)
            return self._check_in(message, metadata, remote_addr)

        agent = self._resolve_agent(message, metadata)
        task = self._owned_task(message.task_id, agent)
        kind = metadata.get(MetaKeys.KIND, "progress")
        if kind == "result":
            return self._result(task, agent, _text(message), metadata)
        if kind == "progress":
            _require_schema_version(metadata)
            operation_id = _require_operation_id(metadata)
            note = _text(message)
            payload_hash = _hash_payload({
                "intent": "progress",
                "task_id": task.id,
                "note": note,
            })
            resp_ack = _agent_message(
                "noted",
                context_id=agent.context_id,
                task_id=task.id,
                metadata={MetaKeys.KIND: "progress_ack"},
            )
            resp_json, _ = self.store.record_progress(
                task.id,
                agent.name,
                note,
                operation_id=operation_id,
                payload_hash=payload_hash,
                response_builder=lambda: json.dumps(
                    resp_ack.model_dump(mode="json", exclude_none=True)
                ),
            )
            return A2AMessage.model_validate(json.loads(resp_json))
        raise _invalid(f"metadata.{MetaKeys.KIND} {kind!r} is not a message/send intent on a task")

    def _check_in(
        self, message: A2AMessage, metadata: dict[str, Any], remote_addr: str | None
    ) -> A2AMessage:
        if _text(message).upper() != CHECK_IN_TEXT:
            raise _invalid(f"a message with no taskId must be the {CHECK_IN_TEXT} check-in")
        _require_schema_version(metadata)
        operation_id = _require_operation_id(metadata)
        worker_instance_id = _require_worker_instance_id(metadata)
        name = metadata.get(MetaKeys.AGENT)
        if not isinstance(name, str) or not name.strip():
            raise _invalid(f"check-in requires metadata.{MetaKeys.AGENT}")
        agent_name = name.strip()
        profile = _profile(metadata)

        payload_hash = _hash_payload({
            "intent": "check_in",
            "agent": agent_name,
            "worker_instance_id": worker_instance_id,
            "capabilities": sorted(profile.capabilities),
            "harness": profile.harness,
            "harness_version": profile.harness_version,
            "provider": profile.provider,
            "model": profile.model,
            "model_source": profile.model_source.value,
            "declared_model": profile.declared_model,
            "workspace_id": profile.workspace_id,
        })

        resp_json, _ = self.store.check_in(
            agent_name,
            profile,
            worker_instance_id=worker_instance_id,
            operation_id=operation_id,
            payload_hash=payload_hash,
            response_builder=lambda agent: json.dumps(
                _agent_message(
                    "REGISTERED",
                    context_id=agent.context_id,
                    metadata={
                        MetaKeys.KIND: "check_in_ack",
                        MetaKeys.AGENT: agent.name,
                        MetaKeys.STATUS: agent.status.value,
                        MetaKeys.WORKER_INSTANCE_ID: agent.worker_instance_id,
                    },
                ).model_dump(mode="json", exclude_none=True)
            ),
            remote_addr=remote_addr,
        )
        note_a2a(actor=agent_name)
        return A2AMessage.model_validate(json.loads(resp_json))

    def _heartbeat(
        self, message: A2AMessage, metadata: dict[str, Any], remote_addr: str | None
    ) -> A2AMessage:
        _require_schema_version(metadata)
        name = metadata.get(MetaKeys.AGENT)
        if not isinstance(name, str) or not name.strip():
            raise _invalid(f"heartbeat requires metadata.{MetaKeys.AGENT}")
        instance_id = _require_worker_instance_id(metadata)
        current_task_id = metadata.get(MetaKeys.CURRENT_TASK_ID)
        if current_task_id is not None and (
            not isinstance(current_task_id, str) or not current_task_id.strip()
        ):
            raise _invalid(
                f"metadata.{MetaKeys.CURRENT_TASK_ID} must be a non-empty string when present"
            )
        agent_name = name.strip()
        accepted = self.store.heartbeat(
            agent_name,
            instance_id,
            None if current_task_id is None else current_task_id.strip(),
            remote_addr=remote_addr,
        )
        agent = self.store.agent_by_name(agent_name)
        if agent is not None:
            note_a2a(actor=agent.name)
        return _agent_message(
            "HEARTBEAT",
            context_id=None if agent is None else agent.context_id,
            metadata={
                MetaKeys.KIND: "heartbeat_ack",
                MetaKeys.AGENT: agent_name,
                MetaKeys.ACCEPTED: accepted,
            },
        )

    def _result(
        self,
        task: TaskRecord,
        agent: AgentRecord,
        summary: str,
        metadata: dict[str, Any],
    ) -> Task:
        _require_schema_version(metadata)
        operation_id = _require_operation_id(metadata)
        result_dict = _require_result(metadata)
        result_size = _json_size_bytes(result_dict)
        if result_size > MAX_TYPED_RESULT_BYTES:
            raise PayloadTooLargeError(
                f"typed result is {result_size} bytes; maximum is "
                f"{MAX_TYPED_RESULT_BYTES} bytes. Store work product in GitHub and "
                "submit only references and compact metadata."
            )

        if not result_dict.get("summary") and summary:
            result_dict["summary"] = summary

        typed_result: TaskResult
        try:
            if task.role in RESULT_MODELS:
                typed_result = RESULT_MODELS[TaskRole(task.role)].model_validate(result_dict)
            elif "outcome" in result_dict:
                typed_result = ImplementerResult.model_validate(result_dict)
            elif "verdict" in result_dict:
                typed_result = ReviewerResult.model_validate(result_dict)
            else:
                raise ValueError("result must specify 'outcome' or 'verdict'")
        except ValidationError as exc:
            first_err = exc.errors()[0]
            msg = first_err.get("msg", str(exc))
            loc = ".".join(str(part) for part in first_err.get("loc", []))
            raise ResultValidationError(f"{loc}: {msg}" if loc else msg) from exc
        except ValueError as exc:
            raise ResultValidationError(str(exc)) from exc

        payload_hash = _hash_payload({
            "intent": "result",
            "task_id": task.id,
            "result": typed_result.model_dump(mode="json"),
        })

        resp_json, _ = self.store.submit_result(
            task.id,
            agent.name,
            typed_result,
            operation_id=operation_id,
            payload_hash=payload_hash,
            response_builder=lambda finished: json.dumps(
                _task_object(finished, agent.context_id).model_dump(
                    mode="json", exclude_none=True
                )
            ),
        )
        return Task.model_validate(json.loads(resp_json))

    # -- message/stream -----------------------------------------------------

    def _stream(self, request_id: RequestId, params: MessageSendParams) -> Response:
        message = params.message
        metadata = _metadata(message)
        timeout_s = self._timeout(metadata)

        if message.task_id is not None:
            if metadata.get(MetaKeys.KIND) != "question":
                raise _invalid(
                    f"a streaming call on a task must be metadata.{MetaKeys.KIND}=question"
                )
            agent = self._resolve_agent(message, metadata)
            task = self._owned_task(message.task_id, agent)
            question = _text(message)
            if not question:
                raise _invalid("a question needs text")
            # The task is parked and Alice is notified before the response body
            # opens, so a client that disconnects still leaves the question with
            # her rather than losing it with the stream.
            question_id = self.store.open_question(
                task.id, agent.name, question, message.message_id
            )
            return self._streaming(
                self._reply_stream(
                    request_id, task, agent, question_id, timeout_s, message.message_id
                )
            )

        if _text(message).upper() != NEXT_TEXT:
            raise _invalid(f"a streaming call with no taskId must be the {NEXT_TEXT} poll")
        agent = self._resolve_agent(message, metadata)
        return self._streaming(self._assignment_stream(request_id, agent, timeout_s))

    def _streaming(self, body: AsyncIterator[bytes]) -> StreamingResponse:
        return StreamingResponse(body, media_type=SSE_MEDIA_TYPE, headers=SSE_HEADERS)

    async def _assignment_stream(
        self, request_id: RequestId, agent: AgentRecord, timeout_s: float
    ) -> AsyncIterator[bytes]:
        outcome = await self.store.await_assignment(agent.context_id, timeout_s)
        if isinstance(outcome, Released):
            note_a2a(outcome="release")
            yield _sse(
                _success_body(
                    request_id,
                    _agent_message(
                        "RELEASED",
                        context_id=agent.context_id,
                        metadata={MetaKeys.KIND: "release", MetaKeys.RELEASE: True},
                    ),
                )
            )
            return
        if outcome is None:
            note_a2a(outcome="timeout")
            yield _sse(_success_body(request_id, self._timeout_message(agent.context_id)))
            return
        note_a2a(outcome="assignment", task_id=outcome.id)
        yield _sse(_success_body(request_id, _task_object(outcome, agent.context_id)))

    async def _reply_stream(
        self,
        request_id: RequestId,
        task: TaskRecord,
        agent: AgentRecord,
        question_id: int,
        timeout_s: float,
        sent_as: str,
    ) -> AsyncIterator[bytes]:
        reply = await self.store.await_reply(task.id, question_id, timeout_s)
        current = self.store.get_task(task.id)
        if current is not None and current.state in (
            TaskState.CANCELED,
            TaskState.FAILED,
            TaskState.COMPLETED,
        ):
            note_a2a(outcome="task_ended")
            note = str((current.result or {}).get("summary", current.state.value))
            status_message = _agent_message(
                note,
                context_id=agent.context_id,
                task_id=current.id,
                metadata={MetaKeys.KIND: "state_override", MetaKeys.STATE: current.state.value},
            )
            yield _sse(
                _success_body(
                    request_id,
                    _task_object(current, agent.context_id, status_message=status_message),
                )
            )
            return
        if reply is None:
            note_a2a(outcome="timeout")
            yield _sse(
                _success_body(
                    request_id,
                    self._timeout_message(agent.context_id, task.id, sent_as),
                )
            )
            return
        note_a2a(outcome="reply")
        yield _sse(_success_body(request_id, _stored_message(reply)))

    def _timeout_message(
        self, context_id: str, task_id: str | None = None, sent_as: str | None = None
    ) -> A2AMessage:
        """Tell the worker the hold elapsed; the guide says to call again.

        A retried question has to carry the message id it was first asked
        under, so the marker names it rather than leaving the caller to
        remember: an answer Alice gave in the gap is only reachable through the
        original question.
        """

        metadata: dict[str, Any] = {MetaKeys.KIND: "timeout", MetaKeys.TIMEOUT: True}
        if sent_as is not None:
            metadata[MetaKeys.RETRY_AS_MESSAGE_ID] = sent_as
        return _agent_message(
            "TIMEOUT",
            context_id=context_id,
            task_id=task_id,
            metadata=metadata,
        )

    def _timeout(self, metadata: Mapping[str, Any]) -> float:
        requested = metadata.get(MetaKeys.TIMEOUT_S)
        if requested is None:
            return self.settings.bounded_wait(None)
        if not isinstance(requested, int | float) or isinstance(requested, bool):
            raise _invalid(f"metadata.{MetaKeys.TIMEOUT_S} must be a number of seconds")
        return self.settings.bounded_wait(float(requested))

    # -- tasks/get and tasks/cancel ----------------------------------------

    def _get_task(self, params: TaskQueryParams) -> Task:
        record = self.store.get_task(params.id)
        if record is None:
            raise ProtocolError(TaskNotFoundError())
        history = [
            _stored_message(message)
            for message in self.store.task_history(params.id, params.history_length)
        ]
        return _task_object(record, self.store.task_context_id(record.id), history)

    def _cancel_task(self, params: TaskIdParams) -> Task:
        record = self.store.get_task(params.id)
        if record is None:
            raise ProtocolError(TaskNotFoundError())
        try:
            canceled = self.store.cancel_task(params.id)
        except ConflictError as exc:
            raise ProtocolError(TaskNotCancelableError(message=str(exc))) from exc
        return _task_object(canceled, self.store.task_context_id(canceled.id))

    # -- shared lookups -----------------------------------------------------

    def _resolve_agent(self, message: A2AMessage, metadata: Mapping[str, Any]) -> AgentRecord:
        """Identify the caller by its context id, falling back to its name."""

        if message.context_id:
            agent = self.store.agent_by_context(message.context_id)
            if agent is None:
                raise _invalid(f"unknown contextId {message.context_id!r}; check in first")
            note_a2a(actor=agent.name)
            return self._require_current_instance(agent, metadata)
        name = metadata.get(MetaKeys.AGENT)
        if isinstance(name, str) and name.strip():
            agent = self.store.agent_by_name(name.strip())
            if agent is None:
                raise _invalid(f"unknown agent {name!r}; check in first")
            note_a2a(actor=agent.name)
            return self._require_current_instance(agent, metadata)
        raise _invalid(f"the message needs a contextId or metadata.{MetaKeys.AGENT}")

    def _require_current_instance(
        self, agent: AgentRecord, metadata: Mapping[str, Any]
    ) -> AgentRecord:
        instance_id = _require_worker_instance_id(metadata)
        if instance_id != agent.worker_instance_id:
            raise _invalid(f"worker instance for agent {agent.name} has been superseded")
        if agent.status.value == "lost":
            raise _invalid(f"agent {agent.name} is lost; check in with a new worker instance")
        return agent

    def _owned_task(self, task_id: str, agent: AgentRecord) -> TaskRecord:
        task = self.store.get_task(task_id)
        if task is None:
            raise ProtocolError(TaskNotFoundError())
        if task.assignee != agent.name:
            raise _invalid(f"task {task_id} is not assigned to {agent.name}")
        note_a2a(task_id=task.id)
        return task


def _validate(model: type[Any], payload: Any) -> Any:
    try:
        return model.model_validate(payload)
    except ValidationError as exc:
        raise ProtocolError(InvalidParamsError(message=_first_error(exc))) from exc


def _first_error(exc: ValidationError) -> str:
    first = exc.errors()[0]
    location = ".".join(str(part) for part in first["loc"])
    return f"{location}: {first['msg']}" if location else first["msg"]
