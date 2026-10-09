"""Durable hub state and the blocking reads layered on top of it.

Every worker intent in spec §4.1 and every Alice action in §4.2 that a worker
can observe lands here. The A2A wire format stays in `protocol`; this module
deals only in records and in the wakeups that release a held request.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Callable, Mapping, Sequence
from contextlib import AbstractContextManager, suppress
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from sqlite3 import Connection, Row
from time import monotonic
from typing import Any, TypeVar
from uuid import uuid4

from agent_hub_common import (
    DEFAULT_EVENT_LEASE_S,
    MAX_MESSAGE_PART_BYTES,
    MAX_TYPED_RESULT_BYTES,
    SHA_HEX_40_RE,
    UNKNOWN,
    AgentProfile,
    AgentStatus,
    EventKind,
    EventState,
    ImplementerOutcome,
    ImplementerResult,
    MetaKeys,
    ModelSource,
    RebaseResult,
    ReviewerResult,
    ReviewerVerdict,
    TaskResult,
    TaskState,
    WorkflowPolicy,
    WorkflowStatus,
    to_iso,
    utcnow,
)
from pydantic import ValidationError

from .activity import (
    EVENT_BACKLOG,
    NO_HUB_CALL,
    NO_TASK_PROGRESS,
    Activity,
    ActivityTracker,
)
from .database import database
from .merge_gate import GateReport
from .signals import EVENT_KEY, Signals, context_key, task_key

logger = logging.getLogger(__name__)

DEFAULT_GOAL = "Drive the assigned GitHub issue to a merged pull request."
DEFAULT_LEASE_MIN = 30.0
DEFAULT_MAX_TASK_LEASE_MIN = 120.0
DEFAULT_STALL_AFTER_MIN = 20.0
# How many of the oldest unconsumed events a stall assessment names.
STALL_EVENT_SAMPLE = 5
DEFAULT_PROFILE = AgentProfile()
LOST_REASON = "worker_lost"
MAX_CHECK_NAME_CHARS = 256

TERMINAL_STATES = (TaskState.COMPLETED, TaskState.FAILED, TaskState.CANCELED)
OPEN_STATES = (TaskState.SUBMITTED, TaskState.WORKING, TaskState.INPUT_REQUIRED)

T = TypeVar("T")


class StoreError(RuntimeError):
    """Base class for refusals that the protocol layer maps onto A2A errors."""


class NotFoundError(StoreError):
    """Raised when an addressed agent or task does not exist."""


class ConflictError(StoreError):
    """Raised when an operation contradicts the current state."""


class IdempotencyConflictError(ConflictError):
    """Raised when an operation is retried with a conflicting payload."""


class DuplicateAgentError(ConflictError):
    """Raised when a second live worker claims an existing agent name."""


class PayloadTooLargeError(StoreError):
    """Raised when transcript or typed-result data exceeds the contract cap."""


class InvalidPolicyError(StoreError):
    """Raised before an invalid create-once workflow policy is persisted."""


@dataclass(frozen=True, slots=True)
class AgentRecord:
    name: str
    capabilities: list[str]
    status: AgentStatus
    context_id: str
    last_seen: str
    worker_instance_id: str
    last_heartbeat: str
    last_progress_at: str | None
    current_task_id: str | None
    # The identity profile (§3), flat so `get_state` shows it per agent.
    harness: str = UNKNOWN
    harness_version: str = UNKNOWN
    provider: str = UNKNOWN
    model: str = UNKNOWN
    model_source: ModelSource = ModelSource.UNKNOWN
    workspace_id: str | None = None
    # What the runtime reported, kept even when a configured `model` won (#77).
    declared_model: str = UNKNOWN
    # The peer address the hub saw at check-in and most recently (#126). An
    # observation, never identity: None when the hub did not see one.
    checkin_remote_addr: str | None = None
    last_remote_addr: str | None = None


@dataclass(frozen=True, slots=True)
class TaskRecord:
    id: str
    workflow_id: str
    assignee: str | None
    role: str
    title: str
    instructions: str
    state: TaskState
    lease_expires: str | None
    lease_duration_s: float
    result: dict[str, Any] | None
    created: str
    updated: str
    # The PR head a review or rebase is bound to (#27, #41); None when unbound.
    pr_head_sha: str | None = None
    # The durable event whose handling created this task (#51); direct tasks are unbound.
    source_event_id: int | None = None


@dataclass(frozen=True, slots=True)
class MessageRecord:
    id: int
    task_id: str | None
    context_id: str
    sender: str
    direction: str
    parts: list[dict[str, Any]]
    ts: str


@dataclass(frozen=True, slots=True)
class EventRecord:
    id: int
    kind: EventKind
    payload: dict[str, Any]
    ts: str
    state: EventState = EventState.QUEUED
    delivery_id: str | None = None
    delivery_attempts: int = 0
    delivered_at: str | None = None
    delivery_expires: str | None = None
    acked_at: str | None = None


@dataclass(frozen=True, slots=True)
class Released:
    """Sentinel returned instead of a task when Alice has released the agent."""

    agent: str


def _json_object(raw: str | None) -> dict[str, Any] | None:
    if raw is None:
        return None
    loaded: Any = json.loads(raw)
    return loaded if isinstance(loaded, dict) else None


def _ids(ids: Sequence[int]) -> str:
    return ", ".join(str(i) for i in ids)


def _json_size_bytes(value: Any) -> int:
    """Return the compact UTF-8 JSON size used by the wire payload caps."""

    encoded = json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return len(encoded)


def _agent(row: Row) -> AgentRecord:
    capabilities: Any = json.loads(row["capabilities_json"])
    return AgentRecord(
        name=row["name"],
        capabilities=[str(item) for item in capabilities] if isinstance(capabilities, list) else [],
        status=AgentStatus(row["status"]),
        context_id=row["context_id"],
        last_seen=row["last_seen"],
        worker_instance_id=row["worker_instance_id"],
        last_heartbeat=row["last_heartbeat"],
        last_progress_at=row["last_progress_at"],
        current_task_id=row["current_task_id"],
        harness=row["harness"],
        harness_version=row["harness_version"],
        provider=row["provider"],
        model=row["model"],
        model_source=ModelSource(row["model_source"]),
        workspace_id=row["workspace_id"],
        declared_model=row["declared_model"] or UNKNOWN,
        checkin_remote_addr=row["checkin_remote_addr"],
        last_remote_addr=row["last_remote_addr"],
    )


def _model_mismatch(model_source: ModelSource, model: str, declared_model: str) -> bool:
    """True when the operator's configured model and the runtime's own claim differ.

    Only a configured model can disagree with a declaration: a declared one is
    the same string by construction, and a runtime that reported nothing is not
    contradicting anyone. Recorded, never enforced (§3), and pairing still reads
    `model`.
    """

    return model_source is ModelSource.ENV and declared_model not in (UNKNOWN, model)


def _profile_fields(profile: AgentProfile) -> dict[str, Any]:
    """The profile as an event payload: what Alice pairs workers on (§5)."""

    return {
        "harness": profile.harness,
        "harness_version": profile.harness_version,
        "provider": profile.provider,
        "model": profile.model,
        "model_source": profile.model_source.value,
        "declared_model": profile.declared_model,
        "model_mismatch": _model_mismatch(
            profile.model_source, profile.model, profile.declared_model
        ),
        "capabilities": list(profile.capabilities),
        "workspace_id": profile.workspace_id,
    }


def _task(row: Row) -> TaskRecord:
    return TaskRecord(
        id=row["id"],
        workflow_id=row["workflow_id"],
        assignee=row["assignee"],
        role=row["role"],
        title=row["title"],
        instructions=row["instructions"],
        state=TaskState(row["state"]),
        lease_expires=row["lease_expires"],
        lease_duration_s=float(row["lease_duration_s"]),
        result=_json_object(row["result_json"]),
        created=row["created"],
        updated=row["updated"],
        pr_head_sha=row["pr_head_sha"],
        source_event_id=row["source_event_id"],
    )


_LEGACY_KEY_MAP: dict[str, str] = {
    "kind": MetaKeys.KIND.value,
    "agent": MetaKeys.AGENT.value,
    "capabilities": MetaKeys.CAPABILITIES.value,
    "status": MetaKeys.STATUS.value,
    "timeout": MetaKeys.TIMEOUT.value,
    "timeout_s": MetaKeys.TIMEOUT_S.value,
    "retry_as_message_id": MetaKeys.RETRY_AS_MESSAGE_ID.value,
    "message_id": MetaKeys.RETRY_AS_MESSAGE_ID.value,
    "release": MetaKeys.RELEASE.value,
    "result": MetaKeys.RESULT.value,
    "role": MetaKeys.ROLE.value,
    "title": MetaKeys.TITLE.value,
    "assignee": MetaKeys.ASSIGNEE.value,
    "lease_expires": MetaKeys.LEASE_EXPIRES.value,
    "artifacts": MetaKeys.ARTIFACTS.value,
    "state": MetaKeys.STATE.value,
    "sender": MetaKeys.SENDER.value,
    "ts": MetaKeys.TS.value,
    "worker_instance_id": MetaKeys.WORKER_INSTANCE_ID.value,
    "current_task_id": MetaKeys.CURRENT_TASK_ID.value,
}


def _normalize_metadata(metadata: Mapping[str, Any]) -> dict[str, Any]:
    """Normalize legacy unprefixed metadata keys to the hub.* namespace."""
    normalized: dict[str, Any] = {}
    for k, v in metadata.items():
        if k in _LEGACY_KEY_MAP:
            normalized[_LEGACY_KEY_MAP[k]] = v
        elif not k.startswith("hub."):
            normalized[f"hub.{k}"] = v
        else:
            normalized[k] = v
    return normalized


def _normalize_part(part: dict[str, Any]) -> dict[str, Any]:
    """Ensure part metadata uses the hub.* namespace."""
    metadata = part.get("metadata")
    if not isinstance(metadata, dict):
        return part
    new_part = dict(part)
    new_part["metadata"] = _normalize_metadata(metadata)
    return new_part


def _message(row: Row) -> MessageRecord:
    parts: Any = json.loads(row["parts_json"])
    return MessageRecord(
        id=row["id"],
        task_id=row["task_id"],
        context_id=row["context_id"],
        sender=row["sender"],
        direction=row["direction"],
        parts=[_normalize_part(part) for part in parts if isinstance(part, dict)]
        if isinstance(parts, list)
        else [],
        ts=row["ts"],
    )


def _event(row: Row) -> EventRecord:
    return EventRecord(
        id=row["id"],
        kind=EventKind(row["kind"]),
        payload=_json_object(row["payload_json"]) or {},
        ts=row["ts"],
        state=EventState(row["state"]),
        delivery_id=row["delivery_id"],
        delivery_attempts=row["delivery_attempts"],
        delivered_at=row["delivered_at"],
        delivery_expires=row["delivery_expires"],
        acked_at=row["acked_at"],
    )


def text_part(text: str, metadata: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """Build the A2A text part shape the transcript stores."""

    part: dict[str, Any] = {"kind": "text", "text": text}
    if metadata:
        part["metadata"] = _normalize_metadata(metadata)
    return part


def _validated_workflow_policy(policy: Mapping[str, Any] | None) -> dict[str, Any]:
    """Validate only supplied rails and retain their compact stored shape."""

    try:
        validated = WorkflowPolicy.model_validate(dict(policy or {}))
    except ValidationError as exc:
        details = "; ".join(
            f"{'.'.join(str(item) for item in error['loc'])}: {error['msg']}"
            for error in exc.errors()
        )
        raise InvalidPolicyError(
            f"invalid workflow policy: {details}. Correct the policy and retry "
            "initialize_workflow before assigning work."
        ) from exc
    return validated.model_dump(mode="json", exclude_unset=True)


def _result_transcript_part(summary: str, status: TaskState) -> dict[str, Any]:
    """Keep internal transcript echoes within the part cap without shrinking results."""

    metadata: dict[str, Any] = {
        MetaKeys.KIND.value: "result",
        MetaKeys.STATUS.value: status.value,
    }
    part = text_part(summary, metadata=metadata)
    if _json_size_bytes(part) <= MAX_MESSAGE_PART_BYTES:
        return part
    return text_part(
        "Typed result recorded; full summary is retained in the task result.",
        metadata=metadata,
    )


@dataclass(slots=True)
class HubStore:
    """SQLite-backed hub state, plus the waits that hold a worker's request."""

    path: Path
    signals: Signals = field(default_factory=Signals)
    clock: Callable[[], datetime] = utcnow
    default_event_lease_s: float = DEFAULT_EVENT_LEASE_S
    # Hub calls and holds per agent, in memory only (#144).
    activity: ActivityTracker = field(default_factory=ActivityTracker)

    def _now(self) -> datetime:
        return self.clock()

    def _now_iso(self) -> str:
        return to_iso(self._now())

    # -- workflow -----------------------------------------------------------

    def initialize_workflow(
        self, goal: str = DEFAULT_GOAL, policy: Mapping[str, Any] | None = None
    ) -> str:
        """Create the workflow once, or confirm the durable initial prompt.

        Goal and policy are immutable workflow inputs. A restarted Alice repeats
        this call with the same values; a different prompt must not silently
        replace rails that existing tasks were created under.
        """

        requested_policy = _validated_workflow_policy(policy)
        encoded_policy = json.dumps(requested_policy, sort_keys=True)
        with database(self.path) as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT id, goal, status, policy_json FROM workflow ORDER BY created LIMIT 1"
            ).fetchone()
            if row is None:
                workflow_id = uuid4().hex
                connection.execute(
                    "INSERT INTO workflow (id, goal, status, policy_json, created) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (
                        workflow_id,
                        goal,
                        WorkflowStatus.ACTIVE.value,
                        encoded_policy,
                        self._now_iso(),
                    ),
                )
                return workflow_id
            stored_policy = _json_object(row["policy_json"]) or {}
            if row["goal"] != goal or stored_policy != requested_policy:
                raise ConflictError(
                    "workflow is already initialized with "
                    f"status={row['status']!r}, goal={row['goal']!r}, and a different goal "
                    "or policy; use get_state to resume it, or run robomate up "
                    "in a fresh dedicated target clone for a different workflow"
                )
            return str(row["id"])

    def _require_workflow(self, connection: Connection, operation: str) -> str:
        row = connection.execute("SELECT id FROM workflow ORDER BY created LIMIT 1").fetchone()
        if row is None:
            raise ConflictError(
                f"workflow is not initialized; call initialize_workflow(goal, policy) "
                f"before {operation}"
            )
        return str(row["id"])

    def get_state(self) -> dict[str, Any]:
        """Return compact state without task instructions or transcripts."""
        now = self._now()
        with database(self.path) as connection:
            # SELECT alone does not start a transaction in sqlite3's legacy mode.
            connection.execute("BEGIN")
            row = connection.execute("SELECT * FROM workflow ORDER BY created LIMIT 1").fetchone()
            workflow = None if row is None else dict(row)
            if workflow is not None:
                workflow["policy"] = json.loads(workflow.pop("policy_json"))
            tasks = []
            for row in connection.execute("SELECT * FROM task ORDER BY created").fetchall():
                summary = asdict(_task(row))
                summary.pop("instructions")
                tasks.append(summary)
            agents = []
            for row in connection.execute("SELECT * FROM agent ORDER BY name").fetchall():
                agent = _agent(row)
                summary = asdict(agent)
                summary["model_mismatch"] = _model_mismatch(
                    agent.model_source, agent.model, agent.declared_model
                )
                summary["heartbeat_age_s"] = _age_seconds(summary["last_heartbeat"], now)
                summary["progress_age_s"] = _age_seconds(summary["last_progress_at"], now)
                agents.append(summary)
            queued_count = int(
                connection.execute(
                    "SELECT COUNT(*) AS n FROM event WHERE state = 'queued'"
                ).fetchone()["n"]
            )
            unacked_rows = connection.execute(
                "SELECT * FROM event WHERE state = 'delivered' ORDER BY id ASC"
            ).fetchall()
            unacked_delivered = [asdict(_event(r)) for r in unacked_rows]
        return {
            "workflow": workflow,
            "agents": agents,
            "tasks": tasks,
            "queued_events": queued_count,
            "unacked_delivered": unacked_delivered,
        }

    def status_summary(self) -> dict[str, Any]:
        """Return the small operator view without instructions or result bodies.

        `activity` beside each agent, and `orchestrator_activity`, are the
        stall assessment (#144): read from the same transaction, additive to
        the fields older consumers know.
        """
        now = self._now()
        with database(self.path) as connection:
            connection.execute("BEGIN")
            workflow = connection.execute(
                "SELECT id, status, goal FROM workflow ORDER BY created LIMIT 1"
            ).fetchone()
            assessment = self._assess(connection, now)
            agents = [
                {
                    "name": row["name"],
                    "harness": row["harness"],
                    "model": row["model"],
                    "status": row["status"],
                    "alive": row["status"] != AgentStatus.LOST.value,
                    "current_task": row["current_task_id"],
                    "stalled": assessment["agents"][row["name"]]["stalled"],
                    "activity": assessment["agents"][row["name"]],
                }
                for row in connection.execute(
                    "SELECT name, harness, model, status, current_task_id FROM agent ORDER BY name"
                )
            ]
            tasks = [
                {
                    "id": row["id"],
                    "role": row["role"],
                    "assignee": row["assignee"],
                    "state": row["state"],
                    "pr_url": None,
                    "head_sha": row["pr_head_sha"],
                }
                for row in connection.execute(
                    "SELECT id, role, assignee, state, pr_head_sha FROM task"
                    " WHERE state IN ('submitted', 'working', 'input-required')"
                    " ORDER BY created"
                )
            ]
            # Bind a review or rebase to the PR whose reported head matches
            # its assigned head. A workflow may have reported several PRs.
            if tasks and workflow is not None:
                results = connection.execute(
                    "SELECT result_json FROM task WHERE workflow_id = ?"
                    " AND result_json IS NOT NULL ORDER BY created DESC",
                    (workflow["id"],),
                )
                pr_by_head: dict[str, str] = {}
                for row in results:
                    result = _json_object(row["result_json"])
                    if result and isinstance(result.get("pr_url"), str):
                        for key in ("head_sha", "reviewed_head_sha"):
                            head = result.get(key)
                            if isinstance(head, str):
                                pr_by_head.setdefault(head.lower(), result["pr_url"])
                for task in tasks:
                    if task["role"] in ("reviewer", "rebase") and task["head_sha"]:
                        task["pr_url"] = pr_by_head.get(task["head_sha"].lower())
            pending_questions = sum(
                task["state"] == TaskState.INPUT_REQUIRED.value for task in tasks
            )
            operator_questions = int(
                connection.execute(
                    "SELECT COUNT(*) AS n FROM operator_question WHERE answered IS NULL"
                ).fetchone()["n"]
            )
        return {
            "workflow": None
            if workflow is None
            else {
                "id": workflow["id"],
                "status": workflow["status"],
                "headline": next(
                    (line.strip() for line in workflow["goal"].splitlines() if line.strip()),
                    "",
                )[:160],
            },
            "agents": agents,
            "tasks": tasks,
            "pending_questions": pending_questions,
            # Open `ask_user` questions, which `robomate inbox` lists (#130).
            "operator_questions": operator_questions,
            "stall_after_min": assessment["stall_after_min"],
            "orchestrator_activity": assessment["orchestrator"],
            "assessed_at": to_iso(now),
        }

    def snapshot(self) -> dict[str, Any]:
        """The operator's before-snapshot for a manual resume (#146); a pure read.

        One read transaction, so tasks and deliveries are consistent with each
        other. Nothing is leased, acknowledged or renewed, and no session is
        touched: taking it changes nothing Alice or a worker can observe.
        """

        now = self._now()
        with database(self.path) as connection:
            connection.execute("BEGIN")
            workflow = connection.execute(
                "SELECT id, status FROM workflow ORDER BY created LIMIT 1"
            ).fetchone()
            tasks = [
                {
                    "id": row["id"],
                    "assignee": row["assignee"],
                    "role": row["role"],
                    "state": row["state"],
                    "lease_expires": row["lease_expires"],
                }
                for row in connection.execute(
                    f"SELECT * FROM task WHERE state IN ({_placeholders(OPEN_STATES)})"
                    " ORDER BY created",
                    tuple(state.value for state in OPEN_STATES),
                )
            ]
            deliveries = [
                {
                    "event_id": row["id"],
                    "kind": row["kind"],
                    "delivery_id": row["delivery_id"],
                    "attempt": row["delivery_attempts"],
                    "delivered_at": row["delivered_at"],
                    "delivery_expires": row["delivery_expires"],
                }
                for row in connection.execute(
                    "SELECT * FROM event WHERE state = 'delivered' ORDER BY id"
                )
            ]
            queued = int(
                connection.execute(
                    "SELECT COUNT(*) AS n FROM event WHERE state = 'queued'"
                ).fetchone()["n"]
            )
        return {
            "taken_at": to_iso(now),
            "workflow": None
            if workflow is None
            else {"id": workflow["id"], "status": workflow["status"]},
            "tasks": tasks,
            "deliveries": deliveries,
            "queued_events": queued,
        }

    def set_workflow_status(
        self, status: WorkflowStatus, summary: str, *, actor: str, session: str | None
    ) -> None:
        """Persist status and its explanation atomically in the audit log.

        actor and session name the caller (#128): the orchestrator's, or
        `operator` with no session. Only `ask_user` escalates, and the
        workflow leaves escalation only once no operator question is open
        (#132); the decision row then names the questions answered.
        """
        if status == WorkflowStatus.ESCALATED:
            raise ValueError(
                "set_workflow_status cannot escalate; call ask_user, which escalates "
                "with the question the operator answers"
            )
        with database(self.path) as connection:
            connection.execute("BEGIN IMMEDIATE")
            workflow_id = self._require_workflow(connection, "set_workflow_status")
            row = connection.execute(
                "SELECT status FROM workflow WHERE id = ?", (workflow_id,)
            ).fetchone()
            if row is not None and row["status"] == status.value:
                return
            rationale = f"Workflow status set to {status.value}"
            resumed: list[int] = []
            if row is not None and row["status"] == WorkflowStatus.ESCALATED.value:
                open_ids = self._open_question_ids(connection, workflow_id)
                if open_ids:
                    raise ConflictError(
                        f"workflow is escalated with open operator questions {_ids(open_ids)}; "
                        "wait for their user_answered events before changing its status"
                    )
                # Every question is answered, so those this decision has not
                # yet ended are the ones this escalation asked.
                resumed = [
                    int(r["id"])
                    for r in connection.execute(
                        "SELECT id FROM operator_question"
                        " WHERE workflow_id = ? AND resumed_by IS NULL ORDER BY id",
                        (workflow_id,),
                    )
                ]
                if resumed:
                    rationale += f"; operator answered questions {_ids(resumed)}"
            connection.execute(
                "UPDATE workflow SET status = ? WHERE id = ?", (status.value, workflow_id)
            )
            if row is not None and row["status"] == WorkflowStatus.PAUSED.value:
                # Stall clocks restart when a pause ends (#144).
                self.activity.note_resumed(self._now())
            cursor = connection.execute(
                "INSERT INTO decision (ts, summary, rationale, actor, session)"
                " VALUES (?, ?, ?, ?, ?)",
                (self._now_iso(), summary, rationale, actor, session),
            )
            connection.executemany(
                "UPDATE operator_question SET resumed_by = ? WHERE id = ?",
                [(cursor.lastrowid, question_id) for question_id in resumed],
            )

    def refuse_while_escalated(self, operation: str) -> None:
        """Refuse an operation that must wait for the operator (#132)."""

        with database(self.path) as connection:
            row = connection.execute("SELECT id FROM workflow ORDER BY created LIMIT 1").fetchone()
            if row is not None:
                self._refuse_while_escalated(connection, row["id"], operation)

    def _refuse_while_escalated(
        self, connection: Connection, workflow_id: str, operation: str
    ) -> None:
        row = connection.execute(
            "SELECT status FROM workflow WHERE id = ?", (workflow_id,)
        ).fetchone()
        if row is None or row["status"] != WorkflowStatus.ESCALATED.value:
            return
        open_ids = self._open_question_ids(connection, workflow_id)
        if open_ids:
            waiting = f"operator questions {_ids(open_ids)} are open"
        else:
            waiting = "no operator question is open; resume with set_workflow_status(active)"
        raise ConflictError(f"{operation} is refused while the workflow is escalated: {waiting}")

    def _open_question_ids(self, connection: Connection, workflow_id: str) -> list[int]:
        rows = connection.execute(
            "SELECT id FROM operator_question WHERE workflow_id = ? AND answered IS NULL"
            " ORDER BY id",
            (workflow_id,),
        )
        return [int(row["id"]) for row in rows]

    def log_decision(
        self,
        summary: str,
        rationale: str,
        key: str | None = None,
        *,
        actor: str,
        session: str | None,
    ) -> int:
        with database(self.path) as connection:
            if key is not None:
                row = connection.execute("SELECT id FROM decision WHERE key = ?", (key,)).fetchone()
                if row is not None:
                    return int(row["id"])
            cursor = connection.execute(
                "INSERT INTO decision (ts, summary, rationale, key, actor, session)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                (self._now_iso(), summary, rationale, key, actor, session),
            )
            return int(cursor.lastrowid or 0)

    # -- operator questions (#129) -------------------------------------------

    def ask_user(
        self, question: str, options: Sequence[str] | None, *, actor: str, session: str | None
    ) -> int:
        """Hold a question for the operator and escalate the workflow.

        The question, the status change and its decision row commit together,
        so the workflow is never escalated over a question the hub did not
        keep. A question asked while already escalated is added beside the
        open ones, and still leaves a decision row naming who asked it.
        """

        for label, value in (("question", question), ("options", options)):
            size = _json_size_bytes(value)
            if size > MAX_MESSAGE_PART_BYTES:
                raise PayloadTooLargeError(
                    f"{label} is {size} bytes; maximum is {MAX_MESSAGE_PART_BYTES} bytes. "
                    "Link to the forge for detail instead."
                )
        with database(self.path) as connection:
            connection.execute("BEGIN IMMEDIATE")
            workflow_id = self._require_workflow(connection, "ask_user")
            now = self._now_iso()
            cursor = connection.execute(
                "INSERT INTO operator_question"
                " (workflow_id, asked, actor, session, question, options_json)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                (
                    workflow_id,
                    now,
                    actor,
                    session,
                    question,
                    None if options is None else json.dumps(list(options)),
                ),
            )
            question_id = int(cursor.lastrowid or 0)
            row = connection.execute(
                "SELECT status FROM workflow WHERE id = ?", (workflow_id,)
            ).fetchone()
            if row["status"] == WorkflowStatus.ESCALATED.value:
                rationale = "Workflow already escalated; question added"
            else:
                connection.execute(
                    "UPDATE workflow SET status = ? WHERE id = ?",
                    (WorkflowStatus.ESCALATED.value, workflow_id),
                )
                rationale = f"Workflow status set to {WorkflowStatus.ESCALATED.value}"
            connection.execute(
                "INSERT INTO decision (ts, summary, rationale, actor, session)"
                " VALUES (?, ?, ?, ?, ?)",
                (now, f"Asked the operator question {question_id}", rationale, actor, session),
            )
        return question_id

    def open_operator_questions(self) -> list[dict[str, Any]]:
        """The questions still waiting for the operator, oldest first."""

        with database(self.path) as connection:
            rows = connection.execute(
                "SELECT id, asked, actor, question, options_json FROM operator_question"
                " WHERE answered IS NULL ORDER BY id"
            ).fetchall()
        return [
            {
                "question_id": row["id"],
                "asked": row["asked"],
                "actor": row["actor"],
                "question": row["question"],
                "options": None if row["options_json"] is None else json.loads(row["options_json"]),
            }
            for row in rows
        ]

    def operator_answer(self, question_id: int) -> dict[str, Any]:
        """One question with its answer, or `unanswered` while it waits."""

        with database(self.path) as connection:
            row = connection.execute(
                "SELECT id, question, answer, answered FROM operator_question WHERE id = ?",
                (question_id,),
            ).fetchone()
        if row is None:
            raise NotFoundError(f"unknown operator question: {question_id}")
        return {
            "question_id": row["id"],
            "question": row["question"],
            "status": "unanswered" if row["answered"] is None else "answered",
            "answer": row["answer"],
            "answered": row["answered"],
        }

    def answer_operator_question(self, question_id: int, answer: str) -> dict[str, Any]:
        """Record the operator's answer and queue `user_answered` for the orchestrator (#130).

        The answer, its time and actor, and the event commit together, so the
        orchestrator hears of exactly the answer the hub kept. A question is
        answered once: a second answer is refused rather than replacing a
        decision the orchestrator may already have acted on.
        """

        if not answer.strip():
            raise ValueError("answer must not be empty")
        size = _json_size_bytes(answer)
        if size > MAX_MESSAGE_PART_BYTES:
            raise PayloadTooLargeError(
                f"answer is {size} bytes; maximum is {MAX_MESSAGE_PART_BYTES} bytes."
            )
        with database(self.path) as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT answered FROM operator_question WHERE id = ?", (question_id,)
            ).fetchone()
            if row is None:
                raise NotFoundError(f"unknown operator question: {question_id}")
            if row["answered"] is not None:
                raise ConflictError(f"operator question {question_id} is already answered")
            now = self._now_iso()
            connection.execute(
                "UPDATE operator_question SET answer = ?, answered = ?, answered_by = ?"
                " WHERE id = ?",
                (answer, now, "operator", question_id),
            )
            self._add_event(
                connection,
                EventKind.USER_ANSWERED,
                {"question_id": question_id, "answer": answer},
            )
        self.signals.notify(EVENT_KEY)
        return {"question_id": question_id, "answered": now}

    def record_rpc_audit(
        self, actor: str | None, session: str | None, method: str, outcome: str
    ) -> None:
        """Append one `/rpc` audit row (#128): who called what, and how it ended.

        Never params or payload text, as with call accounting. actor and
        session are None when the caller did not identify itself.
        """
        with database(self.path) as connection:
            connection.execute(
                "INSERT INTO rpc_audit (ts, actor, session, method, outcome)"
                " VALUES (?, ?, ?, ?, ?)",
                (self._now_iso(), actor, session, method[:128], outcome),
            )

    def record_gate_reading(
        self,
        pr_url: str,
        expected_head_sha: str,
        *,
        report: GateReport | None = None,
        error_code: int | None = None,
        elapsed_s: float,
    ) -> int:
        """Append one gate result, including failures, without external error text.

        A gate can be called before workflow initialization; its workflow ID is
        then NULL. Check names are external labels, capped here and sanitized by
        display consumers. Links are not needed to reconstruct the CI reading.
        """
        checks = (
            []
            if report is None
            else [
                {"name": check.name[:MAX_CHECK_NAME_CHARS], "bucket": check.bucket}
                for check in report.checks
            ]
        )
        with database(self.path) as connection:
            workflow = connection.execute(
                "SELECT id FROM workflow ORDER BY created LIMIT 1"
            ).fetchone()
            cursor = connection.execute(
                "INSERT INTO gate_reading (ts, workflow_id, pr_url, expected_head_sha,"
                " current_head_sha, pr_state, head_matches, ci, mergeable, merge_state_status,"
                " base_ref, base_sha, main_sha, base_behind_main, elapsed_s, checks_json,"
                " error_code) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    self._now_iso(),
                    workflow["id"] if workflow else None,
                    report.pr_url if report else pr_url,
                    report.expected_head_sha if report else expected_head_sha,
                    report.current_head_sha if report else None,
                    report.pr_state.value if report else None,
                    report.head_matches if report else None,
                    report.ci.value if report else None,
                    report.mergeable.value if report else None,
                    report.merge_state_status if report else None,
                    report.base_ref if report else None,
                    report.base_sha if report else None,
                    report.main_sha if report else None,
                    report.base_behind_main if report else None,
                    elapsed_s,
                    json.dumps(checks),
                    error_code,
                ),
            )
            return int(cursor.lastrowid or 0)

    def set_task_state(self, task_id: str, state: TaskState, note: str) -> TaskRecord:
        """Cancel or fail open work; terminal work requires a new assignment."""
        if state not in (TaskState.CANCELED, TaskState.FAILED):
            raise ConflictError("manual overrides accept only canceled or failed")
        with database(self.path) as connection:
            task = self._require_open_task(connection, task_id)
            context_id = self._task_context_id(connection, task)
            self._add_message(
                connection,
                task_id=task_id,
                context_id=context_id,
                sender="alice",
                direction="from_alice",
                parts=[text_part(note, metadata={MetaKeys.KIND: "state_override"})],
            )
            self._finish(connection, task_id, state, {"status": state.value, "summary": note})
            result = self._require_task(connection, task_id)
        self.signals.notify(task_key(task_id))
        self.signals.notify(context_key(context_id))
        return result

    # -- agents -------------------------------------------------------------

    def check_in(
        self,
        name: str,
        profile: AgentProfile = DEFAULT_PROFILE,
        *,
        worker_instance_id: str | None = None,
        operation_id: str | None = None,
        payload_hash: str | None = None,
        response_builder: Callable[[AgentRecord], str] | None = None,
        remote_addr: str | None = None,
    ) -> Any:
        """Register a worker, or re-admit a returning one on its own context.

        The profile replaces whatever was recorded before, field by field: a
        returning worker may be a different harness or model under the same
        name, and a stale value would mislead role selection. `remote_addr`,
        the peer the request arrived from, likewise replaces both recorded
        addresses; a replayed check-in records nothing.
        """

        now = self._now_iso()
        instance_id = worker_instance_id or uuid4().hex
        profile_values = (
            json.dumps(list(profile.capabilities)),
            profile.harness,
            profile.harness_version,
            profile.provider,
            profile.model,
            profile.model_source.value,
            profile.workspace_id,
            profile.declared_model,
            remote_addr,
            remote_addr,
        )
        with database(self.path) as connection:
            connection.execute("BEGIN IMMEDIATE")
            if operation_id is not None and payload_hash is not None:
                row = connection.execute(
                    "SELECT payload_hash, response_json FROM operation "
                    "WHERE actor = ? AND operation_id = ?",
                    (name, operation_id),
                ).fetchone()
                if row is not None:
                    if row["payload_hash"] != payload_hash:
                        raise IdempotencyConflictError(
                            f"operation {operation_id!r} already executed with different payload"
                        )
                    return (row["response_json"], False)

            if profile.workspace_id is not None:
                owner = connection.execute(
                    "SELECT name FROM agent WHERE workspace_id = ? "
                    "AND status IN (?, ?) AND name != ?",
                    (
                        profile.workspace_id,
                        AgentStatus.IDLE.value,
                        AgentStatus.BUSY.value,
                        name,
                    ),
                ).fetchone()
                if owner is not None:
                    raise DuplicateAgentError(
                        f"workspace {profile.workspace_id} is occupied "
                        f"by live agent {owner['name']}"
                    )

            row = connection.execute("SELECT * FROM agent WHERE name = ?", (name,)).fetchone()
            if row is None:
                context_id = uuid4().hex
                connection.execute(
                    "INSERT INTO agent (name, status, context_id, last_seen, worker_instance_id,"
                    " last_heartbeat, capabilities_json,"
                    " harness, harness_version, provider, model, model_source, workspace_id,"
                    " declared_model, checkin_remote_addr, last_remote_addr)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        name,
                        AgentStatus.IDLE.value,
                        context_id,
                        now,
                        instance_id,
                        now,
                        *profile_values,
                    ),
                )
            else:
                previous = AgentStatus(row["status"])
                previous_instance = str(row["worker_instance_id"])
                if previous in (AgentStatus.IDLE, AgentStatus.BUSY) and previous_instance:
                    raise DuplicateAgentError(f"agent {name} already has a live worker instance")
                # A returning worker keeps its context id so Alice reads one
                # unbroken thread per agent across restarts.
                context_id = row["context_id"]
                current = self._open_task_id(connection, row["current_task_id"])
                connection.execute(
                    "UPDATE agent SET status = ?, last_seen = ?, worker_instance_id = ?,"
                    " last_heartbeat = ?, last_progress_at = ?, current_task_id = ?,"
                    " capabilities_json = ?, harness = ?, harness_version = ?, provider = ?,"
                    " model = ?, model_source = ?, workspace_id = ?, declared_model = ?,"
                    " checkin_remote_addr = ?, last_remote_addr = ?"
                    " WHERE name = ?",
                    (
                        _readmitted(previous, current).value,
                        now,
                        instance_id,
                        now,
                        None if previous is AgentStatus.LOST else row["last_progress_at"],
                        current,
                        *profile_values,
                        name,
                    ),
                )
            profile_payload = _profile_fields(profile)
            self._add_message(
                connection,
                task_id=None,
                context_id=context_id,
                sender=name,
                direction="to_alice",
                parts=[
                    text_part(
                        "READY",
                        metadata={MetaKeys.KIND: "check_in"}
                        | {f"hub.{key}": value for key, value in profile_payload.items()},
                    )
                ],
            )
            self._add_event(
                connection,
                EventKind.AGENT_CHECKED_IN,
                {"agent": name}
                | profile_payload
                | {"context_id": context_id, "worker_instance_id": instance_id},
            )
            agent = self._require_agent(connection, name)
            res: tuple[Any, bool]
            if (
                operation_id is not None
                and payload_hash is not None
                and response_builder is not None
            ):
                resp_json = response_builder(agent)
                connection.execute(
                    "INSERT INTO operation ("
                    "actor, operation_id, payload_hash, response_json, created"
                    ") VALUES (?, ?, ?, ?, ?)",
                    (name, operation_id, payload_hash, resp_json, self._now_iso()),
                )
                res = (resp_json, True)
            else:
                res = (agent, True)

        self.signals.notify(EVENT_KEY)
        if operation_id is None:
            return agent
        return res

    def _open_task_id(self, connection: Connection, task_id: str | None) -> str | None:
        """Return `task_id` only while that task is still open."""

        if task_id is None:
            return None
        row = connection.execute("SELECT state FROM task WHERE id = ?", (task_id,)).fetchone()
        if row is None or TaskState(row["state"]) in TERMINAL_STATES:
            return None
        return task_id

    def agent_by_name(self, name: str) -> AgentRecord | None:
        with database(self.path) as connection:
            row = connection.execute("SELECT * FROM agent WHERE name = ?", (name,)).fetchone()
        return None if row is None else _agent(row)

    def agent_by_context(self, context_id: str) -> AgentRecord | None:
        with database(self.path) as connection:
            row = connection.execute(
                "SELECT * FROM agent WHERE context_id = ?", (context_id,)
            ).fetchone()
        return None if row is None else _agent(row)

    def agents(self) -> list[AgentRecord]:
        with database(self.path) as connection:
            rows = connection.execute("SELECT * FROM agent ORDER BY name").fetchall()
        return [_agent(row) for row in rows]

    def heartbeat(
        self,
        name: str,
        worker_instance_id: str,
        current_task_id: str | None,
        max_task_lease_min: float | None = None,
        *,
        remote_addr: str | None = None,
    ) -> bool:
        """Record a timer heartbeat and renew the matching task's bounded lease.

        A stale process is deliberately given a successful no-op path: it must
        not revive an agent or lease after a newer instance supersedes it, nor
        overwrite the live instance's `last_remote_addr`. An accepted heartbeat
        with no `remote_addr` leaves the recorded one in place.

        The wall clock can step backwards (WSL2 resyncs by seconds, #120), so
        the stamp is taken once the write lock is held and never moves either
        liveness stamp, or the lease, back behind a value already stored.
        """

        with database(self.path) as connection:
            connection.execute("BEGIN IMMEDIATE")
            now = self._now()
            now_iso = to_iso(now)
            agent = self._require_agent(connection, name)
            if agent.worker_instance_id != worker_instance_id or agent.status is AgentStatus.LOST:
                return False
            connection.execute(
                "UPDATE agent SET last_heartbeat = MAX(last_heartbeat, ?),"
                " last_seen = MAX(last_seen, ?),"
                " last_remote_addr = coalesce(?, last_remote_addr) WHERE name = ?",
                (now_iso, now_iso, remote_addr, name),
            )
            if not current_task_id or agent.current_task_id != current_task_id:
                return True
            task = self._require_task(connection, current_task_id)
            if task.assignee != name or task.state in TERMINAL_STATES or task.lease_expires is None:
                return True
            current_expiry = _parse_timestamp(task.lease_expires)
            cap_minutes = max_task_lease_min or self._max_task_lease_min(
                connection, task.workflow_id
            )
            cap = _parse_timestamp(task.created) + timedelta(minutes=cap_minutes)
            # An already-expired lease stays expired even if the sweeper has not
            # observed it yet. The next pass will emit its one durable event.
            if current_expiry <= now:
                return True
            if cap <= now:
                connection.execute(
                    "UPDATE task SET lease_expires = ?, updated = ? WHERE id = ?",
                    (to_iso(cap), now_iso, task.id),
                )
                return True
            renewed = min(max(now + timedelta(seconds=task.lease_duration_s), current_expiry), cap)
            if renewed != current_expiry:
                connection.execute(
                    "UPDATE task SET lease_expires = ?, updated = ? WHERE id = ?",
                    (to_iso(renewed), now_iso, task.id),
                )
        return True

    def _max_task_lease_min(self, connection: Connection, workflow_id: str) -> float:
        """Read the workflow rail that bounds heartbeat lease renewal."""

        row = connection.execute(
            "SELECT policy_json FROM workflow WHERE id = ?", (workflow_id,)
        ).fetchone()
        if row is None:
            return DEFAULT_MAX_TASK_LEASE_MIN
        policy = _json_object(row["policy_json"]) or {}
        value = policy.get("max_task_lease_min", DEFAULT_MAX_TASK_LEASE_MIN)
        if isinstance(value, int | float) and not isinstance(value, bool) and value > 0:
            return float(value)
        return DEFAULT_MAX_TASK_LEASE_MIN

    def release_agent(self, name: str) -> AgentRecord:
        """Mark an agent released so its next assignment wait returns release."""

        with database(self.path) as connection:
            agent = self._require_agent(connection, name)
            if agent.status == AgentStatus.RELEASED:
                return agent
            connection.execute(
                "UPDATE agent SET status = ? WHERE name = ?",
                (AgentStatus.RELEASED.value, name),
            )
            released = self._require_agent(connection, name)
        self.signals.notify(context_key(agent.context_id))
        return released

    def detach(self, name: str, worker_instance_id: str) -> bool:
        """Free a cleanly stopped instance's slot so its successor can check in at once.

        Only the live instance itself can detach, so this never displaces a
        running worker. Status, current task and last heartbeat are left as
        they are: a successor that checks in keeps the open task, and the
        sweeper still declares the agent lost, failing that task, if none
        arrives within the lost window (#115).
        """

        with database(self.path) as connection:
            connection.execute("BEGIN IMMEDIATE")
            agent = self._require_agent(connection, name)
            if agent.worker_instance_id != worker_instance_id or agent.status not in (
                AgentStatus.IDLE,
                AgentStatus.BUSY,
            ):
                return False
            connection.execute("UPDATE agent SET worker_instance_id = '' WHERE name = ?", (name,))
            return True

    def _require_agent(self, connection: Connection, name: str) -> AgentRecord:
        row = connection.execute("SELECT * FROM agent WHERE name = ?", (name,)).fetchone()
        if row is None:
            raise NotFoundError(f"unknown agent: {name}")
        return _agent(row)

    # -- tasks --------------------------------------------------------------

    def assign_task(
        self,
        agent: str,
        role: str,
        title: str,
        instructions: str,
        lease_min: float = DEFAULT_LEASE_MIN,
        pr_head_sha: str | None = None,
        source_event_id: int | None = None,
    ) -> TaskRecord:
        """Create a task for an idle agent and unblock its pending wait.

        `pr_head_sha` binds a review or rebase to the PR head it was given, so
        the verdict can be checked against what the PR holds at merge time.
        `source_event_id` makes Alice's event-driven call idempotent: an exact
        replay returns the original task even after it has become terminal.
        """

        if pr_head_sha is not None and not SHA_HEX_40_RE.fullmatch(pr_head_sha):
            raise ValueError("pr_head_sha must be a 40-character hex commit SHA")
        head = None if pr_head_sha is None else pr_head_sha.lower()
        assignment_metadata: dict[str, Any] = {
            MetaKeys.KIND: "assignment",
            MetaKeys.ROLE: role,
            MetaKeys.TITLE: title,
        }
        if head is not None:
            assignment_metadata[MetaKeys.PR_HEAD_SHA] = head
        now_moment = self._now()
        now = to_iso(now_moment)
        with database(self.path) as connection:
            workflow_id = self._require_workflow(connection, "assign_task")
            lease_cap_min = self._max_task_lease_min(connection, workflow_id)
            effective_lease_min = min(lease_min, lease_cap_min)
            if source_event_id is not None:
                source = connection.execute(
                    "SELECT id FROM event WHERE id = ?", (source_event_id,)
                ).fetchone()
                if source is None:
                    raise NotFoundError(f"unknown source event: {source_event_id}")
                existing = connection.execute(
                    "SELECT * FROM task WHERE source_event_id = ?", (source_event_id,)
                ).fetchone()
                if existing is not None:
                    task = _task(existing)
                    requested = (
                        workflow_id,
                        agent,
                        role,
                        title,
                        instructions,
                        effective_lease_min * 60,
                        head,
                    )
                    stored = (
                        task.workflow_id,
                        task.assignee,
                        task.role,
                        task.title,
                        task.instructions,
                        task.lease_duration_s,
                        task.pr_head_sha,
                    )
                    if requested == stored:
                        return task
                    raise IdempotencyConflictError(
                        f"source event {source_event_id} already assigned task {task.id} "
                        "with a different payload"
                    )
            self._refuse_while_escalated(connection, workflow_id, "assign_task")
            record = self._require_agent(connection, agent)
            if record.status is not AgentStatus.IDLE:
                held = self._open_task_id(connection, record.current_task_id)
                if held is not None:
                    raise ConflictError(f"agent {agent} already holds task {held}")
                # A released or lost worker is not there to claim the task, and
                # the sweeper will not report it again. Check-in is the only
                # re-admission: it is the one call that proves a worker is back.
                raise ConflictError(
                    f"agent {agent} is {record.status.value}, not idle; "
                    "it must check in again before it can be given work"
                )
            task_id = uuid4().hex
            connection.execute(
                "INSERT INTO task (id, workflow_id, assignee, role, title, instructions, state,"
                " lease_expires, lease_duration_s, created, updated, pr_head_sha, source_event_id)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    task_id,
                    workflow_id,
                    agent,
                    role,
                    title,
                    instructions,
                    TaskState.SUBMITTED.value,
                    to_iso(now_moment + timedelta(minutes=effective_lease_min)),
                    effective_lease_min * 60,
                    now,
                    now,
                    head,
                    source_event_id,
                ),
            )
            connection.execute(
                "UPDATE agent SET status = ?, current_task_id = ? WHERE name = ?",
                (AgentStatus.BUSY.value, task_id, agent),
            )
            self._add_message(
                connection,
                task_id=task_id,
                context_id=record.context_id,
                sender="alice",
                direction="from_alice",
                parts=[text_part(instructions, metadata=assignment_metadata)],
            )
            task = self._require_task(connection, task_id)
        self.signals.notify(context_key(record.context_id))
        return task

    def get_task(self, task_id: str) -> TaskRecord | None:
        with database(self.path) as connection:
            row = connection.execute("SELECT * FROM task WHERE id = ?", (task_id,)).fetchone()
        return None if row is None else _task(row)

    def task_context_id(self, task_id: str) -> str:
        """Return the context of the agent a task is assigned to, if any."""

        with database(self.path) as connection:
            return self._task_context_id(connection, self._require_task(connection, task_id))

    def tasks(self) -> list[TaskRecord]:
        with database(self.path) as connection:
            rows = connection.execute("SELECT * FROM task ORDER BY created").fetchall()
        return [_task(row) for row in rows]

    def task_history(self, task_id: str, limit: int | None = None) -> list[MessageRecord]:
        """Return a task's transcript, newest-last, optionally trimmed."""

        with database(self.path) as connection:
            rows = connection.execute(
                "SELECT * FROM message WHERE task_id = ? ORDER BY id", (task_id,)
            ).fetchall()
        history = [_message(row) for row in rows]
        if limit is not None and limit >= 0:
            history = history[-limit:] if limit else []
        return history

    def record_progress(
        self,
        task_id: str,
        agent: str,
        note: str,
        *,
        operation_id: str | None = None,
        payload_hash: str | None = None,
        response_builder: Callable[[], str] | None = None,
    ) -> Any:
        """Record a fire-and-forget progress note and queue it for Alice."""

        with database(self.path) as connection:
            connection.execute("BEGIN IMMEDIATE")
            if operation_id is not None and payload_hash is not None:
                row = connection.execute(
                    "SELECT payload_hash, response_json FROM operation "
                    "WHERE actor = ? AND operation_id = ?",
                    (agent, operation_id),
                ).fetchone()
                if row is not None:
                    if row["payload_hash"] != payload_hash:
                        raise IdempotencyConflictError(
                            f"operation {operation_id!r} already executed with different payload"
                        )
                    return (row["response_json"], False)

            task = self._require_open_task(connection, task_id)
            record = self._require_agent(connection, agent)
            self._add_message(
                connection,
                task_id=task.id,
                context_id=record.context_id,
                sender=agent,
                direction="to_alice",
                parts=[text_part(note, metadata={MetaKeys.KIND: "progress"})],
            )
            self._add_event(
                connection,
                EventKind.TASK_PROGRESS,
                {"task_id": task.id, "agent": agent, "note": note},
            )
            self._record_progress_at(connection, agent)
            res: tuple[Any, bool]
            if (
                operation_id is not None
                and payload_hash is not None
                and response_builder is not None
            ):
                resp_json = response_builder()
                connection.execute(
                    "INSERT INTO operation ("
                    "actor, operation_id, payload_hash, response_json, created"
                    ") VALUES (?, ?, ?, ?, ?)",
                    (agent, operation_id, payload_hash, resp_json, self._now_iso()),
                )
                res = (resp_json, True)
            else:
                res = (None, True)

        self.signals.notify(EVENT_KEY)
        if operation_id is None:
            return None
        return res

    def open_question(self, task_id: str, agent: str, question: str, sent_as: str) -> int:
        """Park a task on `input-required` and return the question's row id.

        `sent_as` is the caller's own message id, and a retry after a timeout
        carries the one it first asked under. That is what makes "call again"
        safe: Alice may have answered in the gap between attempts, and her
        answer is older than a second question would be, so a retry that opened
        a new question could never see it. A recognised retry resumes the
        original instead — no second question, no duplicate event for Alice.
        """

        with database(self.path) as connection:
            task = self._require_open_task(connection, task_id)
            record = self._require_agent(connection, agent)
            asked = self._asked_question(connection, task.id, sent_as)
            if asked is not None:
                return asked
            message_id = self._add_message(
                connection,
                task_id=task.id,
                context_id=record.context_id,
                sender=agent,
                direction="to_alice",
                parts=[
                    text_part(
                        question,
                        metadata={
                            MetaKeys.KIND: "question",
                            MetaKeys.RETRY_AS_MESSAGE_ID: sent_as,
                        },
                    )
                ],
            )
            self._set_state(connection, task.id, TaskState.INPUT_REQUIRED)
            self._add_event(
                connection,
                EventKind.WORKER_QUESTION,
                {
                    "task_id": task.id,
                    "agent": agent,
                    "question": question,
                    "message_id": message_id,
                },
            )
        self.signals.notify(EVENT_KEY)
        return message_id

    def _asked_question(self, connection: Connection, task_id: str, sent_as: str) -> int | None:
        """Return the row id of a question already asked under `sent_as`."""

        if not sent_as:
            return None
        rows = connection.execute(
            "SELECT id, parts_json FROM message WHERE task_id = ? AND direction = 'to_alice'"
            " ORDER BY id",
            (task_id,),
        ).fetchall()
        for row in rows:
            for part in json.loads(row["parts_json"]):
                if not isinstance(part, dict):
                    continue
                metadata = _normalize_part(part).get("metadata") or {}
                if (
                    metadata.get(MetaKeys.KIND) == "question"
                    and metadata.get(MetaKeys.RETRY_AS_MESSAGE_ID) == sent_as
                ):
                    return int(row["id"])
        return None

    def reply(self, task_id: str, text: str, message_id: int) -> bool:
        """Answer a worker question and put the task back to `working`.

        Returns True if the reply was applied, or False if skipped due to state guards.
        """

        with database(self.path) as connection:
            task = self._require_task(connection, task_id)
            if task.state != TaskState.INPUT_REQUIRED:
                return False
            questions = connection.execute(
                "SELECT id, parts_json FROM message"
                " WHERE task_id = ? AND direction = 'to_alice' ORDER BY id",
                (task.id,),
            ).fetchall()
            newest_question_id = next(
                (
                    int(row["id"])
                    for row in reversed(questions)
                    if any(
                        (_normalize_part(part).get("metadata") or {}).get(MetaKeys.KIND)
                        == "question"
                        for part in json.loads(row["parts_json"])
                        if isinstance(part, dict)
                    )
                ),
                None,
            )
            if newest_question_id != message_id:
                return False
            prior = connection.execute(
                """
                SELECT id FROM message
                 WHERE task_id = ?
                   AND id > ?
                   AND direction = 'from_alice'
                 ORDER BY id LIMIT 1
                """,
                (task.id, message_id),
            ).fetchone()
            if prior is not None:
                return False

            context_id = self._task_context_id(connection, task)
            self._add_message(
                connection,
                task_id=task.id,
                context_id=context_id,
                sender="alice",
                direction="from_alice",
                parts=[text_part(text, metadata={MetaKeys.KIND: "reply"})],
            )
            self._set_state(connection, task.id, TaskState.WORKING)
        self.signals.notify(task_key(task_id))
        return True

    def pending_reply(self, task_id: str, after_message_id: int) -> MessageRecord | None:
        """Return Alice's first reply on this task after the given message."""

        with database(self.path) as connection:
            row = connection.execute(
                "SELECT * FROM message WHERE task_id = ? AND direction = 'from_alice'"
                " AND id > ? ORDER BY id LIMIT 1",
                (task_id, after_message_id),
            ).fetchone()
        return None if row is None else _message(row)

    def get_operation(self, actor: str, operation_id: str) -> Row | None:
        """Look up an existing operation by actor and operation_id."""
        with database(self.path) as connection:
            row: Row | None = connection.execute(
                "SELECT * FROM operation WHERE actor = ? AND operation_id = ?",
                (actor, operation_id),
            ).fetchone()
            return row

    def record_operation(
        self,
        actor: str,
        operation_id: str,
        payload_hash: str,
        response_json: str,
    ) -> None:
        """Record an operation result for idempotency deduplication."""
        with database(self.path) as connection:
            connection.execute(
                "INSERT INTO operation (actor, operation_id, payload_hash, response_json, created)"
                " VALUES (?, ?, ?, ?, ?)",
                (actor, operation_id, payload_hash, response_json, self._now_iso()),
            )

    def submit_result(
        self,
        task_id: str,
        agent: str,
        result: TaskResult | Mapping[str, Any] | TaskState | None = None,
        summary: str = "",
        artifacts: Sequence[Mapping[str, Any]] = (),
        *,
        status: TaskState | None = None,
        operation_id: str | None = None,
        payload_hash: str | None = None,
        response_builder: Callable[[TaskRecord], str] | None = None,
    ) -> Any:
        """Drive a task to a terminal state and free its worker."""

        payload: dict[str, Any]
        result_summary: str
        terminal_status: TaskState

        actual_result = result if result is not None else status
        if actual_result is None:
            raise ConflictError("submit_result requires result or status")
        if isinstance(actual_result, Mapping):
            raw_result_size = _json_size_bytes(dict(actual_result))
            if raw_result_size > MAX_TYPED_RESULT_BYTES:
                raise PayloadTooLargeError(
                    f"typed result is {raw_result_size} bytes; maximum is "
                    f"{MAX_TYPED_RESULT_BYTES} bytes. Store work product in GitHub and "
                    "submit only references and compact metadata."
                )

        if isinstance(actual_result, (ImplementerResult, RebaseResult)):
            terminal_status = (
                TaskState.COMPLETED
                if actual_result.outcome == ImplementerOutcome.COMPLETED
                else TaskState.FAILED
            )
            payload = actual_result.model_dump(mode="json")
            result_summary = actual_result.summary
        elif isinstance(actual_result, ReviewerResult):
            approved_or_changes = (
                ReviewerVerdict.APPROVED,
                ReviewerVerdict.CHANGES_REQUESTED,
            )
            terminal_status = (
                TaskState.COMPLETED
                if actual_result.verdict in approved_or_changes
                else TaskState.FAILED
            )
            payload = actual_result.model_dump(mode="json")
            result_summary = actual_result.summary
        elif isinstance(actual_result, Mapping):
            if "outcome" in actual_result:
                parsed_impl = ImplementerResult.model_validate(actual_result)
                terminal_status = (
                    TaskState.COMPLETED
                    if parsed_impl.outcome == ImplementerOutcome.COMPLETED
                    else TaskState.FAILED
                )
                payload = parsed_impl.model_dump(mode="json")
                result_summary = parsed_impl.summary
            elif "verdict" in actual_result:
                parsed_rev = ReviewerResult.model_validate(actual_result)
                approved_or_changes = (
                    ReviewerVerdict.APPROVED,
                    ReviewerVerdict.CHANGES_REQUESTED,
                )
                terminal_status = (
                    TaskState.COMPLETED
                    if parsed_rev.verdict in approved_or_changes
                    else TaskState.FAILED
                )
                payload = parsed_rev.model_dump(mode="json")
                result_summary = parsed_rev.summary
            else:
                raise ConflictError("result mapping must contain 'outcome' or 'verdict'")
        elif isinstance(actual_result, TaskState):
            if actual_result not in (TaskState.COMPLETED, TaskState.FAILED):
                msg = f"a result must be completed or failed, got {actual_result.value}"
                raise ConflictError(msg)
            terminal_status = actual_result
            result_summary = summary
            payload = {
                "status": terminal_status.value,
                "summary": summary,
                "artifacts": [dict(artifact) for artifact in artifacts],
            }
        else:
            raise ConflictError(f"unsupported result type: {type(actual_result)}")

        result_size = _json_size_bytes(payload)
        if result_size > MAX_TYPED_RESULT_BYTES:
            raise PayloadTooLargeError(
                f"typed result is {result_size} bytes; maximum is "
                f"{MAX_TYPED_RESULT_BYTES} bytes. Store work product in GitHub and "
                "submit only references and compact metadata."
            )

        with database(self.path) as connection:
            connection.execute("BEGIN IMMEDIATE")
            if operation_id is not None and payload_hash is not None:
                row = connection.execute(
                    "SELECT payload_hash, response_json FROM operation "
                    "WHERE actor = ? AND operation_id = ?",
                    (agent, operation_id),
                ).fetchone()
                if row is not None:
                    if row["payload_hash"] != payload_hash:
                        raise IdempotencyConflictError(
                            f"operation {operation_id!r} already executed with different payload"
                        )
                    return (row["response_json"], False)

            task = self._require_open_task(connection, task_id)
            record = self._require_agent(connection, agent)
            self._add_message(
                connection,
                task_id=task.id,
                context_id=record.context_id,
                sender=agent,
                direction="to_alice",
                parts=[_result_transcript_part(result_summary, terminal_status)],
            )
            self._finish(connection, task.id, terminal_status, payload)
            event_payload: dict[str, Any] = {
                "task_id": task.id,
                "agent": agent,
                "summary": result_summary,
            }
            event_payload.update(payload)
            event_payload["result"] = payload
            self._add_event(
                connection,
                EventKind.TASK_COMPLETED
                if terminal_status is TaskState.COMPLETED
                else EventKind.TASK_FAILED,
                event_payload,
            )
            finished = self._require_task(connection, task.id)
            res: tuple[Any, bool]
            if (
                operation_id is not None
                and payload_hash is not None
                and response_builder is not None
            ):
                resp_json = response_builder(finished)
                connection.execute(
                    "INSERT INTO operation ("
                    "actor, operation_id, payload_hash, response_json, created"
                    ") VALUES (?, ?, ?, ?, ?)",
                    (agent, operation_id, payload_hash, resp_json, self._now_iso()),
                )
                res = (resp_json, True)
            else:
                res = (finished, True)

        self.signals.notify(EVENT_KEY)
        if operation_id is None:
            return finished
        return res

    def cancel_task(self, task_id: str) -> TaskRecord:
        """Cancel an open task and release whoever was holding it."""

        with database(self.path) as connection:
            row = connection.execute("SELECT * FROM task WHERE id = ?", (task_id,)).fetchone()
            if row is None:
                raise NotFoundError(f"unknown task: {task_id}")
            task = _task(row)
            if task.state in TERMINAL_STATES:
                raise ConflictError(f"task {task_id} is already {task.state.value}")
            context_id = self._task_context_id(connection, task)
            self._finish(connection, task.id, TaskState.CANCELED, None)
            canceled = self._require_task(connection, task.id)
        self.signals.notify(task_key(task_id))
        if context_id:
            self.signals.notify(context_key(context_id))
        return canceled

    def _require_task(self, connection: Connection, task_id: str) -> TaskRecord:
        row = connection.execute("SELECT * FROM task WHERE id = ?", (task_id,)).fetchone()
        if row is None:
            raise NotFoundError(f"unknown task: {task_id}")
        return _task(row)

    def _require_open_task(self, connection: Connection, task_id: str) -> TaskRecord:
        task = self._require_task(connection, task_id)
        if task.state in TERMINAL_STATES:
            raise ConflictError(f"task {task_id} is already {task.state.value}")
        return task

    def _task_context_id(self, connection: Connection, task: TaskRecord) -> str:
        if task.assignee is None:
            return ""
        return self._require_agent(connection, task.assignee).context_id

    def _set_state(self, connection: Connection, task_id: str, state: TaskState) -> None:
        connection.execute(
            "UPDATE task SET state = ?, updated = ? WHERE id = ?",
            (state.value, self._now_iso(), task_id),
        )

    def _finish(
        self,
        connection: Connection,
        task_id: str,
        state: TaskState,
        payload: dict[str, Any] | None,
    ) -> None:
        """Apply a terminal state: clear the lease and free the assignee."""

        connection.execute(
            "UPDATE task SET state = ?, updated = ?, lease_expires = NULL, result_json = ?"
            " WHERE id = ?",
            (
                state.value,
                self._now_iso(),
                None if payload is None else json.dumps(payload),
                task_id,
            ),
        )
        connection.execute(
            "UPDATE agent SET status = CASE status WHEN ? THEN ? ELSE status END,"
            " current_task_id = NULL WHERE current_task_id = ?",
            (AgentStatus.BUSY.value, AgentStatus.IDLE.value, task_id),
        )

    def _record_progress_at(self, connection: Connection, name: str) -> None:
        """Update progress recency without treating an LLM call as liveness."""

        connection.execute(
            "UPDATE agent SET last_progress_at = ? WHERE name = ?",
            (self._now_iso(), name),
        )

    # -- transcript and events ---------------------------------------------

    def _add_message(
        self,
        connection: Connection,
        *,
        task_id: str | None,
        context_id: str,
        sender: str,
        direction: str,
        parts: list[dict[str, Any]],
    ) -> int:
        for index, part in enumerate(parts, start=1):
            part_size = _json_size_bytes(part)
            if part_size > MAX_MESSAGE_PART_BYTES:
                raise PayloadTooLargeError(
                    f"message part {index} is {part_size} bytes; maximum is "
                    f"{MAX_MESSAGE_PART_BYTES} bytes. Store work product in GitHub and "
                    "send a URL or compact reference instead."
                )
        cursor = connection.execute(
            "INSERT INTO message (task_id, context_id, sender, direction, parts_json, ts)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (task_id, context_id, sender, direction, json.dumps(parts), self._now_iso()),
        )
        return int(cursor.lastrowid or 0)

    def _add_event(
        self, connection: Connection, kind: EventKind, payload: Mapping[str, Any]
    ) -> int:
        cursor = connection.execute(
            "INSERT INTO event (kind, payload_json, state, ts) VALUES (?, ?, 'queued', ?)",
            (kind.value, json.dumps(dict(payload)), self._now_iso()),
        )
        return int(cursor.lastrowid or 0)

    def append_event(self, kind: EventKind, payload: Mapping[str, Any]) -> EventRecord:
        """Queue an event for Alice and wake her inbox."""

        with database(self.path) as connection:
            event_id = self._add_event(connection, kind, payload)
            row = connection.execute("SELECT * FROM event WHERE id = ?", (event_id,)).fetchone()
            event = _event(row)
        self.signals.notify(EVENT_KEY)
        return event

    def ack_event(self, delivery_id: str | None) -> bool:
        """Acknowledge an event delivery; an unknown or superseded id is ignored.

        A matching `delivery_id` is itself the proof that the caller holds the
        newest delivery, because every lease mints a new one. So the ack counts
        even after the delivery lease has expired (#50): an action that outlives
        `HUB_EVENT_LEASE_S` — the §5 MERGE window, where the gate waits on CI
        and `gh pr merge` follows, is the long one — would otherwise be
        redelivered and redone after it had already completed. A late ack is
        logged, so a lease that is chronically too short stays visible.
        """

        if not delivery_id:
            return False
        now_moment = self._now()
        now = to_iso(now_moment)
        with database(self.path) as connection:
            row = connection.execute(
                "SELECT id, delivery_expires, delivery_attempts FROM event"
                " WHERE delivery_id = ? AND state = 'delivered'",
                (delivery_id,),
            ).fetchone()
            if row is None:
                return False
            connection.execute(
                "UPDATE event SET state = 'acked', acked_at = ? WHERE delivery_id = ?"
                " AND state = 'delivered'",
                (now, delivery_id),
            )
        expires = row["delivery_expires"]
        if expires is not None and expires <= now:
            logger.warning(
                "Late ack for event %s: %.1f s past its delivery lease, attempt %s. "
                "Raise HUB_EVENT_LEASE_S if this repeats.",
                row["id"],
                (now_moment - _parse_timestamp(expires)).total_seconds(),
                row["delivery_attempts"],
            )
        return True

    def lease_next_event(self, lease_s: float | None = None) -> EventRecord | None:
        """Lease the oldest queued or expired-delivered event, returning it with delivery_id."""

        effective_lease_s = self.default_event_lease_s if lease_s is None else lease_s
        now_moment = self._now()
        now = to_iso(now_moment)
        expires = to_iso(now_moment + timedelta(seconds=effective_lease_s))
        delivery_id = uuid4().hex
        with database(self.path) as connection:
            row = connection.execute(
                """
                SELECT * FROM event
                WHERE state = 'queued'
                   OR (state = 'delivered' AND delivery_expires <= ?)
                ORDER BY id ASC
                LIMIT 1
                """,
                (now,),
            ).fetchone()
            if row is None:
                return None
            connection.execute(
                """
                UPDATE event
                SET state = 'delivered',
                    delivery_id = ?,
                    delivery_attempts = delivery_attempts + 1,
                    delivered_at = ?,
                    delivery_expires = ?
                WHERE id = ?
                """,
                (delivery_id, now, expires, row["id"]),
            )
            updated_row = connection.execute(
                "SELECT * FROM event WHERE id = ?", (row["id"],)
            ).fetchone()
            return _event(updated_row)

    def expire_event_leases(self, delivery_id: str | None = None) -> int:
        """Make outstanding event deliveries eligible for immediate redelivery.

        A delivery ID limits cleanup to an in-flight call from a superseded
        orchestrator. Without one, a new session releases every stale lease.
        """

        expired_at = to_iso(self._now() - timedelta(microseconds=1))
        with database(self.path) as connection:
            if delivery_id is None:
                cursor = connection.execute(
                    "UPDATE event SET delivery_expires = ? WHERE state = 'delivered'",
                    (expired_at,),
                )
            else:
                cursor = connection.execute(
                    "UPDATE event SET delivery_expires = ?"
                    " WHERE state = 'delivered' AND delivery_id = ?",
                    (expired_at, delivery_id),
                )
            count = cursor.rowcount
        if count:
            self.signals.notify(EVENT_KEY)
        return count

    def next_event(self, lease_s: float | None = None) -> EventRecord | None:
        """Lease the oldest eligible event, if any."""

        return self.lease_next_event(lease_s)

    def pending_events(self) -> int:
        with database(self.path) as connection:
            row = connection.execute(
                "SELECT COUNT(*) AS n FROM event WHERE state = 'queued'"
            ).fetchone()
        return int(row["n"])

    # -- waits --------------------------------------------------------------

    def _earliest_delivery_expires_s(self) -> float | None:
        now_moment = self._now()
        with database(self.path) as connection:
            row = connection.execute(
                "SELECT MIN(delivery_expires) AS min_exp FROM event WHERE state = 'delivered'"
            ).fetchone()
            if row is None or row["min_exp"] is None:
                return None
            min_exp = _parse_timestamp(row["min_exp"])
            delta = (min_exp - now_moment).total_seconds()
            return max(0.01, delta)

    async def _wait_for(
        self,
        key: str,
        poll: Callable[[], T | None],
        timeout_s: float,
        next_timeout: Callable[[], float | None] | None = None,
    ) -> T | None:
        """Poll under a subscription until `poll` yields or the deadline passes."""

        deadline = monotonic() + timeout_s
        with self.signals.subscribe(key) as woken:
            while True:
                # Clearing before the read means a notification racing the read
                # is still pending when the wait begins, instead of being lost.
                woken.clear()
                found = poll()
                if found is not None:
                    return found
                remaining = deadline - monotonic()
                if remaining <= 0:
                    return None
                wait_step = remaining
                if next_timeout is not None:
                    dynamic_timeout = next_timeout()
                    if dynamic_timeout is not None:
                        wait_step = min(remaining, dynamic_timeout)
                with suppress(TimeoutError):
                    await asyncio.wait_for(woken.wait(), wait_step)

    async def await_assignment(
        self, context_id: str, timeout_s: float
    ) -> TaskRecord | Released | None:
        """Hold until this agent has a task or is released; None on timeout."""

        agent = self.agent_by_context(context_id)
        if agent is None:
            raise NotFoundError(f"unknown context: {context_id}")
        return await self._wait_for(
            context_key(context_id),
            lambda: self._claim_assignment(agent.name),
            timeout_s,
        )

    def _claim_assignment(self, name: str) -> TaskRecord | Released | None:
        with database(self.path) as connection:
            agent = self._require_agent(connection, name)
            if agent.status is AgentStatus.RELEASED:
                return Released(agent=name)
            row = connection.execute(
                "SELECT * FROM task WHERE assignee = ? AND state = ? ORDER BY created LIMIT 1",
                (name, TaskState.SUBMITTED.value),
            ).fetchone()
            if row is None:
                return None
            self._set_state(connection, row["id"], TaskState.WORKING)
            return self._require_task(connection, row["id"])

    async def await_reply(
        self, task_id: str, after_message_id: int, timeout_s: float
    ) -> MessageRecord | None:
        """Hold until Alice answers the question at `after_message_id`."""

        return await self._wait_for(
            task_key(task_id),
            lambda: self.pending_reply(task_id, after_message_id),
            timeout_s,
        )

    async def wait_for_event(
        self,
        timeout_s: float,
        ack: str | None = None,
        lease_s: float | None = None,
    ) -> EventRecord | None:
        """Hold until Alice's inbox has an event, leasing it and acking prior delivery."""

        if ack is not None:
            self.ack_event(ack)
        return await self._wait_for(
            EVENT_KEY,
            lambda: self.lease_next_event(lease_s),
            timeout_s,
            next_timeout=self._earliest_delivery_expires_s,
        )

    # -- sweeper ------------------------------------------------------------

    def sweep(self, lost_after_s: float) -> list[EventRecord]:
        """Expire overdue leases and declare silent agents lost.

        Returns the events it queued, so the caller can log what changed.
        """

        now = self._now()
        emitted: list[int] = []
        with database(self.path) as connection:
            emitted += self._expire_leases(connection, to_iso(now))
            cutoff = to_iso(now - timedelta(seconds=lost_after_s))
            emitted += self._lose_agents(connection, cutoff)
            if not emitted:
                return []
            events = [
                _event(row)
                for row in connection.execute(
                    f"SELECT * FROM event WHERE id IN ({_placeholders(emitted)}) ORDER BY id",
                    emitted,
                ).fetchall()
            ]
        self.signals.notify(EVENT_KEY)
        return events

    def _expire_leases(self, connection: Connection, now: str) -> list[int]:
        rows = connection.execute(
            "SELECT id, assignee, lease_expires FROM task"
            f" WHERE state IN ({_placeholders(OPEN_STATES)})"
            " AND lease_expires IS NOT NULL AND lease_expires <= ?",
            (*(state.value for state in OPEN_STATES), now),
        ).fetchall()
        emitted = []
        for row in rows:
            # The lease is cleared as it is reported, so one overdue task
            # produces one event however often the sweeper runs. Alice decides
            # whether to extend, reassign or fail it.
            connection.execute(
                "UPDATE task SET lease_expires = NULL, updated = ? WHERE id = ?",
                (self._now_iso(), row["id"]),
            )
            emitted.append(
                self._add_event(
                    connection,
                    EventKind.LEASE_EXPIRED,
                    {
                        "task_id": row["id"],
                        "agent": row["assignee"],
                        "lease_expires": row["lease_expires"],
                    },
                )
            )
        return emitted

    def _lose_agents(self, connection: Connection, cutoff: str) -> list[int]:
        live = (AgentStatus.IDLE, AgentStatus.BUSY)
        rows = connection.execute(
            f"SELECT * FROM agent WHERE status IN ({_placeholders(live)}) AND last_heartbeat <= ?",
            (*(status.value for status in live), cutoff),
        ).fetchall()
        emitted = []
        for row in rows:
            agent = _agent(row)
            connection.execute(
                "UPDATE agent SET status = ? WHERE name = ?",
                (AgentStatus.LOST.value, agent.name),
            )
            task_id = self._open_task_id(connection, agent.current_task_id)
            if task_id is not None:
                # Re-queue the work as failed rather than silently stranding it;
                # reassignment is Alice's call (§4.3).
                self._finish(
                    connection,
                    task_id,
                    TaskState.FAILED,
                    {
                        "status": TaskState.FAILED.value,
                        "summary": f"worker {agent.name} stopped heartbeating",
                        "reason": LOST_REASON,
                        "artifacts": [],
                    },
                )
            emitted.append(
                self._add_event(
                    connection,
                    EventKind.AGENT_LOST,
                    {
                        "agent": agent.name,
                        "task_id": task_id,
                        "last_heartbeat": agent.last_heartbeat,
                    },
                )
            )
        return emitted

    # -- activity and stalls (#144) -------------------------------------------

    def note_worker_call(self, name: str, instance: str, kind: str) -> None:
        """A substantive call by the worker instance that is current for `name`."""

        self.activity.worker_call(name, instance, kind, self._now())

    def worker_hold(
        self, name: str, instance: str, kind: str, timeout_s: float
    ) -> AbstractContextManager[None]:
        """Exempt a worker from the silence check while one of its calls is held."""

        return self.activity.worker_hold(name, instance, kind, timeout_s, self._now)

    def note_orchestrator_session(self, actor: str, session: str) -> None:
        """The session `/rpc` just accepted as the current orchestrator."""

        self.activity.orchestrator_accepted(actor, session, self._now())

    def note_orchestrator_heartbeat(self, session: str) -> None:
        self.activity.orchestrator_heartbeat(session, self._now())

    def orchestrator_call(
        self, session: str, kind: str, timeout_s: float
    ) -> AbstractContextManager[None]:
        """Count one orchestrator operation as activity, and hold while it runs."""

        return self.activity.orchestrator_call(session, kind, timeout_s, self._now)

    def _stall_after_min(self, policy: Mapping[str, Any]) -> float:
        value = policy.get("stall_after_min", DEFAULT_STALL_AFTER_MIN)
        if isinstance(value, int | float) and not isinstance(value, bool) and value > 0:
            return float(value)
        return DEFAULT_STALL_AFTER_MIN

    def assess_activity(self) -> dict[str, Any]:
        """The current stall assessment for Alice and every worker; a pure read."""

        now = self._now()
        with database(self.path) as connection:
            connection.execute("BEGIN")
            return self._assess(connection, now)

    def _assess(self, connection: Connection, now: datetime) -> dict[str, Any]:
        """Combine in-memory activity with durable rows into one assessment.

        A stall is an observation beside the lifecycle status, never a change
        to it. Each reason has its own evidence, so a call made for some other
        reason cannot hide an event nobody has consumed, and a hold exempts
        only the silence check, never stale task progress.
        """

        tracker = self.activity
        tracker.mark_started(now)
        row = connection.execute(
            "SELECT id, status, policy_json FROM workflow ORDER BY created LIMIT 1"
        ).fetchone()
        if row is None:
            return {
                "workflow_id": None,
                "workflow_status": None,
                "stall_after_min": None,
                "orchestrator": None,
                "agents": {},
            }
        status = WorkflowStatus(row["status"])
        threshold_min = self._stall_after_min(_json_object(row["policy_json"]) or {})
        threshold = timedelta(minutes=threshold_min)
        # Done ends the assessment; paused suspends it until the workflow
        # resumes, when every silence and progress clock restarts.
        suppressed = {
            WorkflowStatus.DONE: "workflow_done",
            WorkflowStatus.PAUSED: "workflow_paused",
        }.get(status)
        orchestrator = self._assess_orchestrator(connection, now, threshold, suppressed)
        agents = {
            agent.name: self._assess_worker(connection, agent, now, threshold, suppressed)
            for agent in (
                _agent(r) for r in connection.execute("SELECT * FROM agent ORDER BY name")
            )
        }
        return {
            "workflow_id": row["id"],
            "workflow_status": status.value,
            "stall_after_min": threshold_min,
            "orchestrator": orchestrator,
            "agents": agents,
        }

    def _activity_fields(
        self, record: Activity | None, now: datetime, threshold: timedelta
    ) -> tuple[dict[str, Any], bool]:
        """Evidence shared by Alice and workers, and whether silence crossed the threshold."""

        tracker = self.activity
        hold = None if record is None else record.active_hold(now)
        anchor = tracker.silence_anchor(record, now)
        silence = max(timedelta(0), now - anchor)
        last_call = None if record is None else record.last_call
        fields = {
            "threshold_s": threshold.total_seconds(),
            "silence_s": round(silence.total_seconds(), 1),
            "last_call": None if record is None else record.last_call_kind,
            "last_call_age_s": None if last_call is None else _age_seconds(to_iso(last_call), now),
            "holding": None if hold is None else hold.kind,
            "hold_age_s": None
            if hold is None
            else round(max(0.0, (now - hold.started).total_seconds()), 1),
        }
        return fields, hold is None and silence >= threshold

    def _assess_orchestrator(
        self,
        connection: Connection,
        now: datetime,
        threshold: timedelta,
        suppressed: str | None,
    ) -> dict[str, Any]:
        tracker = self.activity
        record = tracker.orchestrator()
        fields, silent = self._activity_fields(record, now, threshold)
        # An event nobody has even tried to deliver ages from when it was
        # queued, or from when this hub could first have delivered it.
        floor = max(
            [moment for moment in (tracker.started, tracker.resumed_at) if moment is not None],
            default=now,
        )
        rows = connection.execute(
            "SELECT id, kind, ts FROM event WHERE state = 'queued' AND delivery_attempts = 0"
            " ORDER BY id"
        ).fetchall()
        backlog = [
            row for row in rows if now - max(_parse_timestamp(row["ts"]), floor) >= threshold
        ]
        reasons = []
        if silent:
            reasons.append(NO_HUB_CALL)
        if backlog:
            reasons.append(EVENT_BACKLOG)
        if suppressed is not None:
            reasons = []
        heartbeat = None if record is None else record.heartbeat
        return {
            "actor": tracker.orchestrator_actor,
            "session": tracker.orchestrator_session,
            "stalled": bool(reasons),
            "reasons": reasons,
            "suppressed": suppressed,
            **fields,
            "heartbeat_age_s": None if heartbeat is None else _age_seconds(to_iso(heartbeat), now),
            "unattempted_events": len(rows),
            "stale_events": [
                {"id": row["id"], "kind": row["kind"], "age_s": _age_seconds(row["ts"], now)}
                for row in backlog[:STALL_EVENT_SAMPLE]
            ],
        }

    def _assess_worker(
        self,
        connection: Connection,
        agent: AgentRecord,
        now: datetime,
        threshold: timedelta,
        suppressed: str | None,
    ) -> dict[str, Any]:
        heartbeat_age = _age_seconds(agent.last_heartbeat, now)
        base: dict[str, Any] = {
            "stalled": False,
            "reasons": [],
            "instance": agent.worker_instance_id or None,
            "heartbeat_age_s": heartbeat_age,
            "task_id": None,
            "progress_age_s": None,
        }
        if agent.status in (AgentStatus.RELEASED, AgentStatus.LOST):
            # Lifecycle already says what happened; a released worker is
            # finished and a lost one has no heartbeat (#19).
            return base | {"suppressed": agent.status.value}
        if not agent.worker_instance_id:
            # Detached (#115): its successor has not checked in yet, and the
            # sweeper declares it lost if none does.
            return base | {"suppressed": "detached"}
        record = self.activity.worker(agent.name, agent.worker_instance_id)
        fields, silent = self._activity_fields(record, now, threshold)
        reasons = [NO_HUB_CALL] if silent else []
        task_id = self._open_task_id(connection, agent.current_task_id)
        progress_age = None
        if task_id is not None:
            task = self._require_task(connection, task_id)
            if task.state in (TaskState.SUBMITTED, TaskState.WORKING):
                # Progress is any message on the task, starting with the
                # assignment itself: report_progress, a question, a reply or
                # the result. A worker answering a question is Alice's wait.
                latest = connection.execute(
                    "SELECT MAX(ts) AS ts FROM message WHERE task_id = ?", (task_id,)
                ).fetchone()["ts"]
                anchors = [_parse_timestamp(latest or task.created)]
                if self.activity.resumed_at is not None:
                    anchors.append(self.activity.resumed_at)
                progress = max(timedelta(0), now - max(anchors))
                progress_age = round(progress.total_seconds(), 1)
                if progress >= threshold:
                    reasons.append(NO_TASK_PROGRESS)
        if suppressed is not None:
            reasons = []
        return base | {
            "stalled": bool(reasons),
            "reasons": reasons,
            "suppressed": suppressed,
            **fields,
            "task_id": task_id,
            "progress_age_s": progress_age,
        }

    def sweep_stalls(self) -> list[EventRecord]:
        """Open, refresh and close stall episodes; queue one event per worker episode.

        Changes nothing about any agent, task or workflow (#144): a stall is
        diagnostic. A worker's episode starts with one `agent_stalled` event
        for Alice; an orchestrator stall can only reach the operator, so it is
        recorded and shown, never queued to the stalled orchestrator itself.
        An episode stays open while any reason holds and closes once none does,
        so a recovered agent can start a new one.
        """

        now = self._now()
        now_iso = to_iso(now)
        first = not self.activity.reconciled
        emitted: list[int] = []
        with database(self.path) as connection:
            connection.execute("BEGIN IMMEDIATE")
            report = self._assess(connection, now)
            stalled: dict[tuple[str, str | None], dict[str, Any]] = {}
            orchestrator = report["orchestrator"]
            if orchestrator is not None and orchestrator["stalled"]:
                stalled[("orchestrator", None)] = orchestrator | {
                    "actor": orchestrator["actor"] or "orchestrator",
                    "instance": orchestrator["session"],
                    "task_id": None,
                }
            for name, assessment in report["agents"].items():
                if assessment["stalled"]:
                    stalled[(name, assessment["instance"])] = assessment | {"actor": name}
            open_rows = connection.execute(
                "SELECT * FROM stall_episode WHERE cleared IS NULL ORDER BY id"
            ).fetchall()
            seen: set[tuple[str, str | None]] = set()
            for row in open_rows:
                key: tuple[str, str | None] = (
                    ("orchestrator", None)
                    if row["role"] == "orchestrator"
                    else (row["actor"], row["instance"])
                )
                current = stalled.get(key)
                if current is not None and key not in seen:
                    seen.add(key)
                    connection.execute(
                        "UPDATE stall_episode SET assessed = ?, reasons_json = ?,"
                        " evidence_json = ?, task_id = ?, instance = ? WHERE id = ?",
                        (
                            now_iso,
                            json.dumps(current["reasons"]),
                            json.dumps(_stall_evidence(current)),
                            current["task_id"],
                            current["instance"],
                            row["id"],
                        ),
                    )
                    continue
                connection.execute(
                    "UPDATE stall_episode SET cleared = ?, cleared_reason = ? WHERE id = ?",
                    (now_iso, _cleared_reason(row, report, first), row["id"]),
                )
            for key, current in stalled.items():
                if key in seen:
                    continue
                role = "orchestrator" if key == ("orchestrator", None) else "worker"
                cursor = connection.execute(
                    "INSERT INTO stall_episode (workflow_id, role, actor, instance, task_id,"
                    " started, assessed, reasons_json, evidence_json)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        report["workflow_id"],
                        role,
                        current["actor"],
                        current["instance"],
                        current["task_id"],
                        now_iso,
                        now_iso,
                        json.dumps(current["reasons"]),
                        json.dumps(_stall_evidence(current)),
                    ),
                )
                episode_id = int(cursor.lastrowid or 0)
                if role != "worker":
                    continue
                event_id = self._add_event(
                    connection,
                    EventKind.AGENT_STALLED,
                    {
                        "agent": current["actor"],
                        "task_id": current["task_id"],
                        "episode_id": episode_id,
                        "reasons": current["reasons"],
                    }
                    | _stall_evidence(current),
                )
                connection.execute(
                    "UPDATE stall_episode SET event_id = ? WHERE id = ?", (event_id, episode_id)
                )
                emitted.append(event_id)
            events = [
                _event(row)
                for row in connection.execute(
                    f"SELECT * FROM event WHERE id IN ({_placeholders(emitted)}) ORDER BY id",
                    emitted,
                ).fetchall()
            ]
        self.activity.reconciled = True
        if events:
            self.signals.notify(EVENT_KEY)
        return events

    def stall_episodes(self, *, open_only: bool = False) -> list[dict[str, Any]]:
        """Recorded stall episodes, oldest first."""

        query = "SELECT * FROM stall_episode"
        if open_only:
            query += " WHERE cleared IS NULL"
        with database(self.path) as connection:
            rows = connection.execute(query + " ORDER BY id").fetchall()
        return [_stall_episode(row) for row in rows]


def _readmitted(previous: AgentStatus, open_task: str | None) -> AgentStatus:
    """Status for a worker that has just re-announced itself with READY.

    A release outlives the connection it was issued on. Alice ends the workflow
    by releasing her workers, so one that restarts afterwards has to be told to
    stop; resetting it to idle would leave it polling for work nobody is left to
    assign. Every other prior status — including `lost` — is what check-in
    exists to clear.
    """

    if previous is AgentStatus.RELEASED:
        return AgentStatus.RELEASED
    return AgentStatus.BUSY if open_task else AgentStatus.IDLE


# Evidence copied into an episode and its event: ages and IDs, never text.
_EVIDENCE_KEYS = (
    "threshold_s",
    "silence_s",
    "last_call",
    "last_call_age_s",
    "holding",
    "heartbeat_age_s",
    "task_id",
    "progress_age_s",
    "session",
    "unattempted_events",
    "stale_events",
)


def _stall_evidence(assessment: Mapping[str, Any]) -> dict[str, Any]:
    return {key: assessment[key] for key in _EVIDENCE_KEYS if key in assessment}


def _cleared_reason(row: Row, report: Mapping[str, Any], first_sweep: bool) -> str:
    """Why an open episode no longer holds, for the report."""

    if first_sweep:
        return "hub_restarted"
    status = report["workflow_status"]
    if status == WorkflowStatus.DONE.value:
        return "workflow_done"
    if status == WorkflowStatus.PAUSED.value:
        return "workflow_paused"
    if row["role"] == "worker":
        assessment = report["agents"].get(row["actor"])
        if assessment is None:
            return "agent_removed"
        if assessment.get("suppressed") in ("released", "lost", "detached"):
            return str(assessment["suppressed"])
        if assessment.get("instance") != row["instance"]:
            return "superseded"
    return "recovered"


def _stall_episode(row: Row) -> dict[str, Any]:
    return {
        "id": row["id"],
        "workflow_id": row["workflow_id"],
        "role": row["role"],
        "actor": row["actor"],
        "instance": row["instance"],
        "task_id": row["task_id"],
        "started": row["started"],
        "assessed": row["assessed"],
        "cleared": row["cleared"],
        "cleared_reason": row["cleared_reason"],
        "reasons": json.loads(row["reasons_json"]),
        "evidence": json.loads(row["evidence_json"]),
        "event_id": row["event_id"],
    }


def _placeholders(values: Sequence[object]) -> str:
    return ", ".join("?" * len(values))


def _parse_timestamp(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _age_seconds(value: str | None, now: datetime) -> float | None:
    if not value:
        return None
    return max(0.0, (now - _parse_timestamp(value)).total_seconds())
