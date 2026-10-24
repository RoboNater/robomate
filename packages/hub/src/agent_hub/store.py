"""Durable hub state and the blocking reads layered on top of it.

Every worker intent in spec §4.1 and every Alice action in §4.2 that a worker
can observe lands here. The A2A wire format stays in `protocol`; this module
deals only in records and in the wakeups that release a held request.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import subprocess
import tempfile
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import AbstractContextManager, contextmanager, nullcontext, suppress
from dataclasses import asdict, dataclass, field, replace
from datetime import datetime, timedelta
from pathlib import Path
from sqlite3 import Connection, Row
from time import monotonic
from typing import Any, TypeVar
from uuid import uuid4

from agent_hub_common import (
    DEFAULT_EVENT_LEASE_S,
    MAX_MESSAGE_PART_BYTES,
    MAX_STATEMENT_BYTES,
    MAX_TYPED_RESULT_BYTES,
    SHA_HEX_40_RE,
    UNKNOWN,
    WORKER_STANDING_ROLES,
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
    StandingRole,
    TaskResult,
    TaskRole,
    TaskState,
    WorkflowPolicy,
    WorkflowStatus,
    standing_role_accepts_task,
    to_iso,
    utcnow,
    validate_agent_name,
)
from pydantic import ValidationError

from .activity import (
    ACTIVE,
    BRIDGE_CONNECTED_S,
    CONNECTED,
    DISCONNECTED,
    EVENT_BACKLOG,
    FINISHED,
    HOLDING,
    IDLE,
    IDLE_AFTER_S,
    NO_HUB_CALL,
    NO_TASK_PROGRESS,
    NOT_SEEN,
    PAUSED,
    Activity,
    ActivityTracker,
)
from .database import database
from .merge_gate import GateReport, PrState
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
# Ignore small wall/monotonic differences from sampling and clock correction.
CLOCK_JUMP_TOLERANCE_S = 5.0

TERMINAL_STATES = (TaskState.COMPLETED, TaskState.FAILED, TaskState.CANCELED)
OPEN_STATES = (TaskState.SUBMITTED, TaskState.WORKING, TaskState.INPUT_REQUIRED)
# A hub runs one live workflow at a time; `queued` waits behind it (M2 Step 3).
LIVE_STATUSES = (WorkflowStatus.ACTIVE, WorkflowStatus.PAUSED, WorkflowStatus.ESCALATED)
LIVE_STATUS_VALUES = tuple(status.value for status in LIVE_STATUSES)


def _live_placeholders() -> str:
    return ", ".join("?" * len(LIVE_STATUSES))


def _new_run_id(now: datetime) -> str:
    """A new run ID: `<YYYYMMDD>_<8 hex>`, unique per hub (mvp-spec §7.2)."""

    return f"{now.strftime('%Y%m%d')}_{uuid4().hex[:8]}"


def _statement_sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _robomate_version() -> str:
    """The installed robomate version for run manifests (r1-3)."""

    try:
        from importlib.metadata import PackageNotFoundError, version

        for dist in ("robomate-cli", "agent-hub", "robomate"):
            try:
                return version(dist)
            except PackageNotFoundError:
                continue
    except Exception:
        pass
    return UNKNOWN


_GIT_REVISION: str | None = None


def _git_revision() -> str:
    """The robomate checkout's revision for run manifests; `unknown` when unreadable.

    Computed once per process (r1-4): manifest writes must not shell out on
    every call, and a non-checkout install must not attribute an unrelated
    repository's HEAD to robomate.
    """

    global _GIT_REVISION
    if _GIT_REVISION is not None:
        return _GIT_REVISION
    try:
        repo_root = Path(__file__).resolve().parents[4]
        # Only a robomate checkout has both markers; an installed wheel's
        # ancestor could be any directory.
        if (repo_root / ".git").exists() and (repo_root / "pyproject.toml").is_file():
            completed = subprocess.run(
                ["git", "rev-parse", "HEAD"],
                cwd=repo_root,
                capture_output=True,
                text=True,
                timeout=5,
            )
            revision = completed.stdout.strip()
            if len(revision) == 40 and all(ch in "0123456789abcdef" for ch in revision.lower()):
                _GIT_REVISION = revision.lower()
                return _GIT_REVISION
    except Exception:
        pass
    _GIT_REVISION = UNKNOWN
    return _GIT_REVISION


def _read_hub_json_for_manifest(state_dir: Path) -> dict[str, Any]:
    """Best-effort hub identity for manifests; missing files read as unknown."""

    try:
        raw = (state_dir / "hub.json").read_text(encoding="utf-8")
        value = json.loads(raw)
        if isinstance(value, dict):
            return value
    except Exception:
        pass
    return {}


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
    # Where the agent works (M2 Step 7a, #244): the worktree path with kind
    # `worktree`, or an explicit `HUB_WORKSPACE` clone path with kind `clone`.
    # NULL for rows written before v25.
    workspace_path: str | None = None
    workspace_kind: str | None = None
    # What the runtime reported, kept even when a configured `model` won (#77).
    declared_model: str = UNKNOWN
    # The peer address the hub saw at check-in and most recently (#126). An
    # observation, never identity: None when the hub did not see one.
    checkin_remote_addr: str | None = None
    last_remote_addr: str | None = None
    standing_role: StandingRole = StandingRole.WORKER
    harness_source: ModelSource = ModelSource.UNKNOWN
    effort: str = UNKNOWN
    effort_source: ModelSource = ModelSource.UNKNOWN
    reattached_task: dict[str, Any] | None = field(default=None, compare=False)


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
    # The run this task belongs to (M2 Step 3, #240); None only for rows read
    # without their workflow join.
    run_id: str | None = None
    # The statement this run was started from, if any.
    statement_sha256: str | None = None
    # The run's branch `robomate/<hub>/<agent>/<label>` (M2 Step 7c, #315);
    # None until the first implementer assignment names it.
    branch: str | None = None
    # Transient redelivery marker (#326): True only when `await_assignment`
    # returns a task the caller already holds. Never persisted; `_task()`
    # always reads False and `get_state()` omits it. Additive metadata, so
    # wire `hub.schema_version` stays 1 (mvp-spec §3).
    already_held: bool = field(default=False, compare=False)


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
    keys = set(row.keys()) if hasattr(row, "keys") else set()
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
        workspace_path=str(row["workspace_path"])
        if "workspace_path" in keys and row["workspace_path"] is not None
        else None,
        workspace_kind=str(row["workspace_kind"])
        if "workspace_kind" in keys and row["workspace_kind"] is not None
        else None,
        declared_model=row["declared_model"] or UNKNOWN,
        checkin_remote_addr=row["checkin_remote_addr"],
        last_remote_addr=row["last_remote_addr"],
        standing_role=StandingRole(row["standing_role"]),
        harness_source=ModelSource(row["harness_source"]),
        effort=row["effort"],
        effort_source=ModelSource(row["effort_source"]),
    )


def _model_mismatch(model_source: ModelSource, model: str, declared_model: str) -> bool:
    """True when the operator's configured model and the runtime's own claim differ.

    Only a configured model can disagree with a declaration: a declared one is
    the same string by construction, and a runtime that reported nothing is not
    contradicting anyone. Recorded, never enforced (§3), and pairing still reads
    `model`.
    """

    return model_source is ModelSource.ENV and declared_model not in (UNKNOWN, model)


def _profile_fields(
    profile: AgentProfile, standing_role: StandingRole = StandingRole.WORKER
) -> dict[str, Any]:
    """The profile as an event payload: what Alice pairs workers on (§5)."""

    return {
        "standing_role": standing_role.value,
        "harness": profile.harness,
        "harness_source": profile.harness_source.value,
        "harness_version": profile.harness_version,
        "provider": profile.provider,
        "model": profile.model,
        "model_source": profile.model_source.value,
        "declared_model": profile.declared_model,
        "effort": profile.effort,
        "effort_source": profile.effort_source.value,
        "model_mismatch": _model_mismatch(
            profile.model_source, profile.model, profile.declared_model
        ),
        "capabilities": list(profile.capabilities),
        "workspace_id": profile.workspace_id,
    }


def _task(row: Row) -> TaskRecord:
    keys = set(row.keys()) if hasattr(row, "keys") else set()
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
        run_id=str(row["run_id"]) if "run_id" in keys and row["run_id"] is not None else None,
        statement_sha256=str(row["statement_sha256"])
        if "statement_sha256" in keys and row["statement_sha256"] is not None
        else None,
        branch=str(row["branch"]) if "branch" in keys and row["branch"] is not None else None,
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
    # `auto` is resolved to bool by `hub.submit` before storage (M2 Step 4, r1-2);
    # the chat path must not store it unresolved, since the merge rule reads a bool.
    if validated.allow_no_ci == "auto":
        raise InvalidPolicyError(
            "invalid workflow policy: allow_no_ci 'auto' must be resolved to a bool "
            "via `robomate submit` (pass an explicit bool from chat)"
        )
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
    monotonic_clock: Callable[[], float] = monotonic
    # Durable state directory for runs/ and hub.json (mvp-spec §11, r1-8).
    # Defaults to the database's parent, which is the state directory for
    # every layout except the `hub` entry point with a custom HUB_DB_PATH.
    state_dir: Path | None = None
    # Branch naming context (M2 Step 7c, #315): the hub's name for
    # `robomate/<hub>/<agent>/<label>`, and the git common directory whose
    # refs decide whether a label is already taken. None for hubs without a
    # repository context (test hubs, the `hub` entry point): no branch is
    # named there; the first implementer assignment with a hub name records it
    # and reuses it afterwards, with no ref check when the common dir is None.
    hub_name: str | None = None
    git_common_dir: Path | None = None
    _sweep_sample: tuple[datetime, float] | None = field(default=None, init=False)
    _sweep_lost_after_s: float | None = field(default=None, init=False)
    _resume_grace_until: float = field(default=0.0, init=False)

    def _now(self) -> datetime:
        return self.clock()

    def _now_iso(self) -> str:
        return to_iso(self._now())

    def _state_dir(self) -> Path:
        return self.state_dir if self.state_dir is not None else self.path.parent

    # -- workflow -----------------------------------------------------------

    def _live_row(self, connection: Connection) -> Row | None:
        """The single live workflow, if any (M2 Step 3, #240)."""

        # Durable insertion order (#209): wall-clock `created` can step
        # backwards on WSL2, so run order is `rowid`, never a timestamp.
        row: Row | None = connection.execute(
            f"SELECT * FROM workflow WHERE status IN ({_live_placeholders()})"
            " ORDER BY rowid LIMIT 1",
            tuple(status.value for status in LIVE_STATUSES),
        ).fetchone()
        return row

    def _queued_rows(self, connection: Connection) -> list[Row]:
        # Durable insertion order (#209): see _live_row.
        return list(
            connection.execute(
                "SELECT * FROM workflow WHERE status = ? ORDER BY rowid",
                (WorkflowStatus.QUEUED.value,),
            ).fetchall()
        )

    def _most_recent_row(self, connection: Connection) -> Row | None:
        # Durable insertion order (#209): see _live_row.
        row: Row | None = connection.execute(
            "SELECT * FROM workflow ORDER BY rowid DESC LIMIT 1"
        ).fetchone()
        return row

    def _most_recent_done_row(self, connection: Connection) -> Row | None:
        # Durable insertion order (#209): see _live_row.
        row: Row | None = connection.execute(
            "SELECT * FROM workflow WHERE status = ? ORDER BY rowid DESC LIMIT 1",
            (WorkflowStatus.DONE.value,),
        ).fetchone()
        return row

    def _unique_run_id(self, connection: Connection, now: datetime) -> str:
        """A `<YYYYMMDD>_<8 hex>` run ID not used by any workflow on this hub."""

        for _ in range(100):
            candidate = _new_run_id(now)
            if (
                connection.execute(
                    "SELECT 1 FROM workflow WHERE run_id = ?", (candidate,)
                ).fetchone()
                is None
            ):
                return candidate
        # Practically unreachable; the date plus 32 bits makes a collision
        # vanishingly unlikely, but a bounded loop must not spin forever.
        raise ConflictError("could not allocate a unique run_id; retry start_workflow")

    def _store_statement(
        self, connection: Connection, text: str, source: str, now_iso: str
    ) -> tuple[str, bytes]:
        """Store a statement of work verbatim and return its SHA-256 and bytes."""

        data = text.encode("utf-8")
        if len(data) > MAX_STATEMENT_BYTES:
            raise PayloadTooLargeError(
                f"statement is {len(data)} bytes; maximum is {MAX_STATEMENT_BYTES} bytes "
                "so it fits in one 16 KiB message part with its envelope. Shorten the "
                "statement, or put the detail in an issue and submit that instead."
            )
        sha256 = _statement_sha256(data)
        connection.execute(
            "INSERT OR IGNORE INTO statement (sha256, bytes, size, source, created)"
            " VALUES (?, ?, ?, ?, ?)",
            (sha256, data, len(data), source, now_iso),
        )
        return sha256, data

    def get_statement(self, sha256: str) -> dict[str, Any]:
        """Return a stored statement of work verbatim by its SHA-256 (M2 Step 4, #241).

        Bytes are stored verbatim (CRLF and trailing newline preserved) and
        returned unchanged as text. Unknown SHA raises `NotFoundError`
        (mapped to -32001 on `/rpc`).
        """

        canonical = sha256.strip().lower()
        if len(canonical) != 64 or any(ch not in "0123456789abcdef" for ch in canonical):
            raise ValueError("sha256 must be a 64-character hex digest")
        with database(self.path) as connection:
            row = connection.execute(
                "SELECT sha256, bytes, size, source, created FROM statement WHERE sha256 = ?",
                (canonical,),
            ).fetchone()
            if row is None:
                raise NotFoundError(f"unknown statement: {sha256}")
            raw = bytes(row["bytes"])
            try:
                text = raw.decode("utf-8")
            except UnicodeDecodeError as exc:
                raise ValueError("stored statement is not valid UTF-8") from exc
            return {
                "sha256": str(row["sha256"]),
                "statement": text,
                "size": int(row["size"]),
                "source": str(row["source"]),
                "created": str(row["created"]),
            }

    def _clear_stale_releases(self, connection: Connection) -> None:
        """Make a newly live run take returning workers (r1-1).

        `on_done: release` (and Alice's explicit `release_agent`) leaves workers
        `released` when a run ends. `_readmitted` keeps that sticky across
        `check_in`, so without clearing, the next run's returning workers would
        get `release: true` from their first `await_assignment`. A new live run
        clears it to `lost` (unavailable) without a loss event or task failure.
        Only a fresh check-in makes the worker idle again. Free the instance
        slot so a returning bridge can check in without takeover (#279).
        """

        connection.execute(
            "UPDATE agent SET status = ?, worker_instance_id = ''"
            " WHERE status = ? AND standing_role != 'orchestrator'",
            (AgentStatus.LOST.value, AgentStatus.RELEASED.value),
        )

    def initialize_workflow(
        self, goal: str = DEFAULT_GOAL, policy: Mapping[str, Any] | None = None
    ) -> str:
        """Create the workflow once, or confirm the durable initial prompt.

        Goal and policy are immutable workflow inputs. A restarted Alice repeats
        this call with the same values; a different prompt must not silently
        replace rails that existing tasks were created under. After a run is
        `done`, the same call with a new goal starts a new run with a new
        `run_id` (M2 Step 3, #240); `start_workflow` queues instead of
        conflicting while a run is live.
        """

        requested_policy = _validated_workflow_policy(policy)
        encoded_policy = json.dumps(requested_policy, sort_keys=True)
        now = self._now()
        now_iso = to_iso(now)
        with database(self.path) as connection:
            connection.execute("BEGIN IMMEDIATE")
            live = self._live_row(connection)
            if live is not None:
                stored_policy = _json_object(live["policy_json"]) or {}
                if live["goal"] != goal or stored_policy != requested_policy:
                    expected_sha = live["statement_sha256"] or _statement_sha256(
                        str(live["goal"]).encode("utf-8")
                    )
                    raise ConflictError(
                        "workflow is already initialized with "
                        f"status={live['status']!r}, goal={live['goal']!r}, and a different goal "
                        "or policy; use get_state to resume it, or queue a different run with "
                        "start_workflow(statement) and start it after this one is done. "
                        f"Expected statement_sha256={expected_sha} "
                        "(run.json work.goal_sha256; read it with get_statement)"
                    )
                return str(live["id"])
            queued = self._queued_rows(connection)
            if queued:
                oldest = queued[0]
                stored_policy = _json_object(oldest["policy_json"]) or {}
                if oldest["goal"] == goal and stored_policy == requested_policy:
                    connection.execute(
                        "UPDATE workflow SET status = ? WHERE id = ?",
                        (WorkflowStatus.ACTIVE.value, oldest["id"]),
                    )
                    self._clear_stale_releases(connection)
                    workflow_id = str(oldest["id"])
                else:
                    expected_sha = oldest["statement_sha256"] or _statement_sha256(
                        str(oldest["goal"]).encode("utf-8")
                    )
                    raise ConflictError(
                        f"workflow {oldest['run_id']} is queued with goal={oldest['goal']!r}; "
                        "call start_workflow() with no statement to start it, or "
                        "initialize_workflow with its exact goal and policy to confirm it. "
                        f"Expected statement_sha256={expected_sha} "
                        "(run.json work.goal_sha256; read it with get_statement)"
                    )
            else:
                workflow_id = uuid4().hex
                run_id = self._unique_run_id(connection, now)
                statement_sha256, _ = self._store_statement(
                    connection, goal, "initialize_workflow", now_iso
                )
                connection.execute(
                    "INSERT INTO workflow (id, run_id, goal, status, policy_json,"
                    " statement_sha256, created) VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (
                        workflow_id,
                        run_id,
                        goal,
                        WorkflowStatus.ACTIVE.value,
                        encoded_policy,
                        statement_sha256,
                        now_iso,
                    ),
                )
                self._clear_stale_releases(connection)
            # Read the row for the manifest before committing: the file must
            # describe exactly the workflow the database just stored.
            row = connection.execute(
                "SELECT * FROM workflow WHERE id = ?", (workflow_id,)
            ).fetchone()
            assert row is not None
        self._write_manifest(row)
        return workflow_id

    def start_workflow(
        self,
        statement: str | None = None,
        policy: Mapping[str, Any] | None = None,
        *,
        source: str = "chat",
        force_queued: bool = False,
        emit_work_submitted: bool = False,
    ) -> str:
        """Start the oldest queued run, or create one from chat (mvp-spec §7.1).

        With no `statement`, the oldest `queued` workflow becomes `active`. With
        a `statement`, a new workflow is created: `active` when no run is live
        and nothing is queued, otherwise `queued` behind the live run. Only one
        live (`active`, `paused`, `escalated`) run and one `queued` run may
        exist; a further `start_workflow` while both exist is refused.

        `force_queued` (for `hub.submit`, M2 Step 4) queues even when idle:
        the orchestrator starts it with a later `start_workflow()` call.
        `emit_work_submitted` appends `work_submitted` in the same transaction
        (r1-5), so a queued run never lands without its event.
        """

        requested_policy = _validated_workflow_policy(policy)
        encoded_policy = json.dumps(requested_policy, sort_keys=True)
        now = self._now()
        now_iso = to_iso(now)
        with database(self.path) as connection:
            connection.execute("BEGIN IMMEDIATE")
            live = self._live_row(connection)
            queued = self._queued_rows(connection)
            if statement is None:
                if live is not None:
                    raise ConflictError(
                        f"workflow {live['run_id']} is {live['status']}; "
                        "wait for it to finish before starting the queued run, "
                        "or call start_workflow(statement) to queue behind it"
                    )
                if not queued:
                    raise ConflictError(
                        "no queued workflow to start; call start_workflow(statement, policy) "
                        "with a statement of work to create one"
                    )
                oldest = queued[0]
                connection.execute(
                    "UPDATE workflow SET status = ? WHERE id = ?",
                    (WorkflowStatus.ACTIVE.value, oldest["id"]),
                )
                self._clear_stale_releases(connection)
                workflow_id = str(oldest["id"])
                row = connection.execute(
                    "SELECT * FROM workflow WHERE id = ?", (workflow_id,)
                ).fetchone()
                assert row is not None
            else:
                if not statement.strip():
                    raise ValueError("statement must not be empty")
                statement_sha256, _ = self._store_statement(connection, statement, source, now_iso)
                goal = statement
                if live is not None:
                    if queued:
                        raise ConflictError(
                            f"workflow {live['run_id']} is {live['status']} and workflow "
                            f"{queued[0]['run_id']} is already queued; only one queued run "
                            "is kept (use get_state to see the queue)"
                        )
                    status = WorkflowStatus.QUEUED.value
                else:
                    if queued:
                        raise ConflictError(
                            f"workflow {queued[0]['run_id']} is queued; call start_workflow() "
                            "with no statement to start it before queueing another run"
                        )
                    status = (
                        WorkflowStatus.QUEUED.value if force_queued else WorkflowStatus.ACTIVE.value
                    )
                workflow_id = uuid4().hex
                run_id = self._unique_run_id(connection, now)
                connection.execute(
                    "INSERT INTO workflow (id, run_id, goal, status, policy_json,"
                    " statement_sha256, created) VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (
                        workflow_id,
                        run_id,
                        goal,
                        status,
                        encoded_policy,
                        statement_sha256,
                        now_iso,
                    ),
                )
                if status == WorkflowStatus.ACTIVE.value:
                    self._clear_stale_releases(connection)
                if emit_work_submitted:
                    if statement is None:
                        raise ValueError("emit_work_submitted needs a statement")
                    self._add_event(
                        connection,
                        EventKind.WORK_SUBMITTED,
                        {
                            "workflow_id": workflow_id,
                            "run_id": run_id,
                            "statement_sha256": statement_sha256,
                            "source": source,
                        },
                    )
                row = connection.execute(
                    "SELECT * FROM workflow WHERE id = ?", (workflow_id,)
                ).fetchone()
                assert row is not None
        self._write_manifest(row)
        if emit_work_submitted:
            self.signals.notify(EVENT_KEY)
        return workflow_id

    def _require_workflow(self, connection: Connection, operation: str) -> str:
        row = self._live_row(connection)
        if row is None:
            raise ConflictError(
                f"workflow is not initialized; call initialize_workflow(goal, policy) "
                f"or start_workflow(statement, policy) before {operation}"
            )
        return str(row["id"])

    def _require_live_or_recent(self, connection: Connection) -> Row | None:
        """The live workflow, else the most recent one, for read-only views."""

        live = self._live_row(connection)
        if live is not None:
            return live
        return self._most_recent_row(connection)

    def _workflow_pr_summary(
        self, connection: Connection, workflow_id: str
    ) -> dict[str, Any | None]:
        """PR/MR facts from typed task results and forge-backed gate evidence."""

        pr_url: str | None = None
        approved_sha: str | None = None
        # Durable insertion order (#209): wall-clock `created` can step backwards.
        for row in connection.execute(
            "SELECT result_json FROM task WHERE workflow_id = ?"
            " AND result_json IS NOT NULL ORDER BY rowid",
            (workflow_id,),
        ).fetchall():
            result = _json_object(row["result_json"]) or {}
            if isinstance(result.get("pr_url"), str) and result["pr_url"]:
                pr_url = result["pr_url"]
            # Reviewer approval binds the approved head.
            if result.get("verdict") == "approved" and isinstance(
                result.get("reviewed_head_sha"), str
            ):
                approved_sha = result["reviewed_head_sha"]
        # Bind evidence to this run AND its reported PR, never another PR's
        # reading or a task's head SHA. Errors/open readings do not erase a
        # successful post-merge observation (#281).
        reported_url = pr_url.strip().rstrip("/").casefold() if pr_url else None
        merged = next(
            (
                row
                for row in connection.execute(
                    "SELECT pr_url, merged_sha FROM gate_reading WHERE workflow_id = ?"
                    " AND pr_state = 'merged' AND merged_sha IS NOT NULL AND error_code IS NULL"
                    " ORDER BY id DESC",
                    (workflow_id,),
                )
                # Task results retain the supplied URL; gates trim whitespace
                # and, on GitHub, a trailing slash. Both forges route project
                # names case-insensitively. Compare equivalent spellings.
                if row["pr_url"].strip().rstrip("/").casefold() == reported_url
            ),
            None,
        )
        return {
            "pr_url": pr_url,
            "approved_sha": approved_sha,
            "merged_sha": str(merged["merged_sha"]) if merged else None,
        }

    def _previous_run_summary(
        self, connection: Connection, exclude_id: str | None = None
    ) -> dict[str, Any] | None:
        """The previous run's summary (run ID, PR/MR, merged SHA), nothing else."""

        if exclude_id is None:
            row = self._most_recent_done_row(connection)
        else:
            # Durable insertion order (#209): wall-clock `created` can step backwards.
            row = connection.execute(
                "SELECT * FROM workflow WHERE status = ? AND id != ? ORDER BY rowid DESC LIMIT 1",
                (WorkflowStatus.DONE.value, exclude_id),
            ).fetchone()
        if row is None:
            return None
        summary: dict[str, Any] = {
            "run_id": str(row["run_id"]),
            "workflow_id": str(row["id"]),
            "status": str(row["status"]),
            "goal": str(row["goal"]),
        }
        summary.update(self._workflow_pr_summary(connection, str(row["id"])))
        return summary

    def _queued_summaries(self, connection: Connection) -> list[dict[str, Any]]:
        """Queued workflows, oldest first, for `get_state` and `status`."""

        return [
            {
                "workflow_id": str(row["id"]),
                "run_id": str(row["run_id"]),
                "goal": str(row["goal"]),
                "statement_sha256": row["statement_sha256"],
                "created": str(row["created"]),
            }
            for row in self._queued_rows(connection)
        ]

    def _write_manifest(self, workflow_row: Row) -> Path:
        """Write `runs/<run_id>/manifest.json` atomically for a workflow row.

        The manifest is written at start (active or queued) and rewritten at
        `done`: robomate version and git revision, hub identity, statement
        SHA-256 and source, policy, agent profiles that held a task, and at the
        end the status, PR/MR URL and approved/merged SHAs. A hub restart
        between start and done leaves valid JSON because the write is atomic.
        """

        workflow_id = str(workflow_row["id"])
        run_id = str(workflow_row["run_id"])
        state_dir = self._state_dir()
        runs_dir = state_dir / "runs" / run_id
        runs_dir.mkdir(parents=True, exist_ok=True)
        with database(self.path) as connection:
            policy = _json_object(str(workflow_row["policy_json"])) or {}
            statement_sha256 = workflow_row["statement_sha256"]
            statement_source: str | None = None
            statement_size: int | None = None
            if statement_sha256 is not None:
                prow = connection.execute(
                    "SELECT source, size FROM statement WHERE sha256 = ?",
                    (str(statement_sha256),),
                ).fetchone()
                if prow is not None:
                    statement_source = str(prow["source"])
                    statement_size = int(prow["size"])
            pr_summary = self._workflow_pr_summary(connection, workflow_id)
            # Profiles of every agent that held a task in this run.
            agents: list[dict[str, Any]] = []
            assignees = [
                str(r["assignee"])
                for r in connection.execute(
                    "SELECT DISTINCT assignee FROM task WHERE workflow_id = ?"
                    " AND assignee IS NOT NULL",
                    (workflow_id,),
                ).fetchall()
            ]
            for name in sorted(assignees):
                arow = connection.execute("SELECT * FROM agent WHERE name = ?", (name,)).fetchone()
                if arow is None:
                    agents.append({"name": name})
                    continue
                agent = _agent(arow)
                agents.append(
                    {
                        "name": agent.name,
                        "standing_role": agent.standing_role.value,
                        "harness": agent.harness,
                        "harness_version": agent.harness_version,
                        "provider": agent.provider,
                        "model": agent.model,
                        "model_source": agent.model_source.value,
                        "declared_model": agent.declared_model,
                        "effort": agent.effort,
                        "effort_source": agent.effort_source.value,
                        "capabilities": list(agent.capabilities),
                        "workspace_id": agent.workspace_id,
                    }
                )
        hub_info = _read_hub_json_for_manifest(state_dir)
        manifest = {
            "run_id": run_id,
            "workflow_id": workflow_id,
            "goal": str(workflow_row["goal"]),
            "status": str(workflow_row["status"]),
            "created": str(workflow_row["created"]),
            "completed": None
            if str(workflow_row["status"]) != WorkflowStatus.DONE.value
            else to_iso(self._now()),
            "statement_sha256": statement_sha256,
            "statement_source": statement_source,
            "statement_size": statement_size,
            "policy": policy,
            "hub": {
                "hub_id": hub_info.get("hub_id"),
                "name": hub_info.get("name"),
                "checkout": hub_info.get("checkout") or hub_info.get("repo_root"),
                "origin": hub_info.get("origin"),
                "forge": hub_info.get("forge"),
                "default_branch": hub_info.get("default_branch"),
                "url": hub_info.get("url"),
            },
            "robomate_version": _robomate_version(),
            "git_revision": _git_revision(),
            "agents": agents,
            "pr_url": pr_summary["pr_url"],
            "approved_sha": pr_summary["approved_sha"],
            "merged_sha": pr_summary["merged_sha"],
        }
        # Atomic: write beside the target, then replace, so a crash never
        # leaves a half-written manifest behind.
        descriptor, temporary = tempfile.mkstemp(prefix=".manifest-", dir=runs_dir, text=True)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                json.dump(manifest, stream, indent=2, sort_keys=True)
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, runs_dir / "manifest.json")
        except BaseException:
            with suppress(OSError):
                os.unlink(temporary)
            raise
        return runs_dir / "manifest.json"

    def get_state(self) -> dict[str, Any]:
        """Return compact state without task instructions or transcripts."""
        now = self._now()
        with database(self.path) as connection:
            # SELECT alone does not start a transaction in sqlite3's legacy mode.
            connection.execute("BEGIN")
            live = self._live_row(connection)
            queue = self._queued_summaries(connection)
            workflow: dict[str, Any] | None
            tasks: list[dict[str, Any]]
            previous_run: dict[str, Any] | None
            if live is not None:
                live_dict = dict(live)
                live_dict["policy"] = json.loads(live_dict.pop("policy_json"))
                workflow = live_dict
                tasks = []
                for row in connection.execute(
                    "SELECT task.*, workflow.run_id, workflow.statement_sha256, workflow.branch"
                    " FROM task JOIN workflow ON task.workflow_id = workflow.id"
                    # Durable insertion order (#209): `created` can step backwards.
                    " WHERE task.workflow_id = ? ORDER BY task.rowid",
                    (str(live["id"]),),
                ).fetchall():
                    summary = asdict(_task(row))
                    summary.pop("instructions")
                    # Transient redelivery marker (#326): never persisted.
                    summary.pop("already_held", None)
                    tasks.append(summary)
                previous_run = self._previous_run_summary(connection)
            elif queue:
                # Queued runs wait behind no live run: no active workflow, but
                # the queue and the previous summary say what is next.
                workflow = None
                tasks = []
                previous_run = self._previous_run_summary(connection)
            else:
                # No live and nothing queued: show the most recent done run as
                # the workflow, so a completed run still reports its status;
                # the previous summary is the one before it.
                recent = self._most_recent_done_row(connection)
                if recent is None:
                    workflow = None
                    tasks = []
                    previous_run = None
                else:
                    recent_dict = dict(recent)
                    recent_dict["policy"] = json.loads(recent_dict.pop("policy_json"))
                    workflow = recent_dict
                    tasks = []
                    for row in connection.execute(
                        "SELECT task.*, workflow.run_id, workflow.statement_sha256, workflow.branch"
                        " FROM task JOIN workflow ON task.workflow_id = workflow.id"
                        # Durable insertion order (#209): `created` can step backwards.
                        " WHERE task.workflow_id = ? ORDER BY task.rowid",
                        (str(recent["id"]),),
                    ).fetchall():
                        summary = asdict(_task(row))
                        summary.pop("instructions")
                        # Transient redelivery marker (#326): never persisted.
                        summary.pop("already_held", None)
                        tasks.append(summary)
                    previous_run = self._previous_run_summary(
                        connection, exclude_id=str(recent["id"])
                    )
            agents = []
            for row in connection.execute("SELECT * FROM agent ORDER BY name").fetchall():
                agent = _agent(row)
                summary = asdict(agent)
                summary.pop("reattached_task", None)
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
        result: dict[str, Any] = {
            "workflow": workflow,
            "agents": agents,
            "tasks": tasks,
            "queued_events": queued_count,
            "unacked_delivered": unacked_delivered,
            "queue": queue,
            "previous_run": previous_run,
        }
        # The run identity is what operators, file names and evidence use; the
        # workflow ID stays what tasks and events reference.
        if workflow is not None:
            result["run_id"] = workflow.get("run_id")
        return result

    def status_summary(self) -> dict[str, Any]:
        """Return the small operator view without instructions or result bodies.

        `activity` beside each agent, and `orchestrator_activity`, are the
        stall assessment (#144): read from the same transaction, additive to
        the fields older consumers know.
        """
        now = self._now()
        with database(self.path) as connection:
            connection.execute("BEGIN")
            workflow = self._require_live_or_recent(connection)
            queue = self._queued_summaries(connection)
            assessment = self._assess(connection, now)
            agents = [
                {
                    "name": row["name"],
                    "standing_role": row["standing_role"],
                    "harness": row["harness"],
                    "harness_source": row["harness_source"],
                    "model": row["model"],
                    "effort": row["effort"],
                    "effort_source": row["effort_source"],
                    "status": row["status"],
                    "alive": row["status"] != AgentStatus.LOST.value,
                    "current_task": row["current_task_id"],
                    "stalled": assessment["agents"].get(row["name"], assessment["orchestrator"])[
                        "stalled"
                    ],
                    "activity": assessment["agents"].get(row["name"], assessment["orchestrator"]),
                }
                for row in connection.execute(
                    "SELECT name, standing_role, harness, harness_source, model, effort,"
                    " effort_source, status, current_task_id FROM agent ORDER BY name"
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
                    # Additive for #299 hand recovery: open tasks span
                    # workflows, so recovery needs each task's membership.
                    # Wire `hub.schema_version` stays 1.
                    "workflow_id": row["workflow_id"],
                }
                for row in connection.execute(
                    "SELECT id, role, assignee, state, pr_head_sha, workflow_id FROM task"
                    " WHERE state IN ('submitted', 'working', 'input-required')"
                    # Durable insertion order (#209): `created` can step backwards.
                    " ORDER BY rowid"
                )
            ]
            # Bind a review or rebase to the PR whose reported head matches
            # its assigned head. A workflow may have reported several PRs.
            # Durable insertion order (#209): `created` can step backwards.
            if tasks and workflow is not None:
                results = connection.execute(
                    "SELECT result_json FROM task WHERE workflow_id = ?"
                    " AND result_json IS NOT NULL ORDER BY rowid DESC",
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
                    "SELECT COUNT(*) AS n FROM operator_question"
                    " WHERE answered IS NULL AND withdrawn IS NULL"
                ).fetchone()["n"]
            )
        return {
            "workflow": None
            if workflow is None
            else {
                "id": workflow["id"],
                "run_id": workflow["run_id"],
                "status": workflow["status"],
                # Additive for #223 hand recovery: creation time lets a
                # relaunched agent verify this workflow existed while its
                # conversation ran. Wire `hub.schema_version` stays 1.
                "created": str(workflow["created"]),
                "headline": next(
                    (line.strip() for line in workflow["goal"].splitlines() if line.strip()),
                    "",
                )[:160],
                "statement_sha256": workflow["statement_sha256"],
            },
            "run_id": None if workflow is None else workflow["run_id"],
            "queue_length": len(queue),
            "queue": queue,
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
            workflow = self._require_live_or_recent(connection)
            tasks = [
                {
                    "id": row["id"],
                    "assignee": row["assignee"],
                    "role": row["role"],
                    "state": row["state"],
                    "lease_expires": row["lease_expires"],
                    # Additive for #299 (see status_summary).
                    "workflow_id": row["workflow_id"],
                }
                for row in connection.execute(
                    f"SELECT * FROM task WHERE state IN ({_placeholders(OPEN_STATES)})"
                    # Durable insertion order (#209): `created` can step backwards.
                    " ORDER BY rowid",
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
            else {
                "id": workflow["id"],
                "run_id": workflow["run_id"],
                "status": workflow["status"],
                # Additive for #223 hand recovery (see status_summary):
                # wire `hub.schema_version` stays 1.
                "created": str(workflow["created"]),
            },
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
        (#132); the decision row then distinguishes answers from withdrawals.

        Setting a run to `done` completes its manifest, starts the oldest
        queued run when one waits, and applies `on_done`: `release` releases
        workers when the queue is empty, `standby` keeps them joined (M2 Step 3).
        """
        if status == WorkflowStatus.ESCALATED:
            raise ValueError(
                "set_workflow_status cannot escalate; call ask_user, which escalates "
                "with the question the operator answers"
            )
        if status == WorkflowStatus.QUEUED:
            raise ValueError("set_workflow_status cannot queue; call start_workflow instead")
        done_row: Row | None = None
        activated_row: Row | None = None
        with database(self.path) as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                workflow_id = self._require_workflow(connection, "set_workflow_status")
            except ConflictError:
                # No live run: either a replayed `done` (no-op when the most
                # recent run already has that status) or a reopen of the most
                # recent run (e.g. DONE -> ACTIVE in activity tests).
                recent = self._most_recent_row(connection)
                if recent is None:
                    raise
                if recent["status"] == status.value:
                    return
                workflow_id = str(recent["id"])
            row = connection.execute(
                "SELECT * FROM workflow WHERE id = ?", (workflow_id,)
            ).fetchone()
            assert row is not None
            if row["status"] == status.value:
                return
            rationale = f"Workflow status set to {status.value}"
            resumed: list[int] = []
            if row["status"] == WorkflowStatus.ESCALATED.value:
                open_ids = self._open_question_ids(connection, workflow_id)
                if open_ids:
                    raise ConflictError(
                        f"workflow is escalated with open operator questions {_ids(open_ids)}; "
                        "wait for their user_answered events before changing its status"
                    )
                # Every question is closed, so those this decision has not
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
                    answered = [
                        int(r["id"])
                        for r in connection.execute(
                            "SELECT id FROM operator_question WHERE workflow_id = ?"
                            " AND resumed_by IS NULL AND answered IS NOT NULL ORDER BY id",
                            (workflow_id,),
                        )
                    ]
                    withdrawn = [
                        question_id for question_id in resumed if question_id not in answered
                    ]
                    if answered:
                        rationale += f"; operator answered questions {_ids(answered)}"
                    if withdrawn:
                        rationale += f"; orchestrator withdrew questions {_ids(withdrawn)}"
            connection.execute(
                "UPDATE workflow SET status = ? WHERE id = ?", (status.value, workflow_id)
            )
            if row["status"] == WorkflowStatus.PAUSED.value:
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
            if status == WorkflowStatus.DONE:
                done_row = connection.execute(
                    "SELECT * FROM workflow WHERE id = ?", (workflow_id,)
                ).fetchone()
                assert done_row is not None
                queued = self._queued_rows(connection)
                if queued:
                    oldest = queued[0]
                    connection.execute(
                        "UPDATE workflow SET status = ? WHERE id = ?",
                        (WorkflowStatus.ACTIVE.value, oldest["id"]),
                    )
                    self._clear_stale_releases(connection)
                    activated_row = connection.execute(
                        "SELECT * FROM workflow WHERE id = ?", (oldest["id"],)
                    ).fetchone()
                else:
                    policy = _json_object(str(done_row["policy_json"])) or {}
                    on_done = policy.get("on_done", "release")
                    if on_done != "standby":
                        connection.execute(
                            "UPDATE agent SET status = ?"
                            " WHERE status IN (?, ?)"
                            " AND standing_role != 'orchestrator'",
                            (
                                AgentStatus.RELEASED.value,
                                AgentStatus.IDLE.value,
                                AgentStatus.BUSY.value,
                            ),
                        )
        # Manifests are files, not rows: write them after the transaction
        # commits, so a crash between the two cannot leave a half-written
        # database row behind a completed manifest.
        if done_row is not None:
            self._write_manifest(done_row)
        if activated_row is not None:
            self._write_manifest(activated_row)
            self.signals.notify(EVENT_KEY)
        if status == WorkflowStatus.DONE and activated_row is None:
            # A released worker's NEXT returns the release marker.
            for agent_row in self.agents():
                if agent_row.status == AgentStatus.RELEASED:
                    self.signals.notify(context_key(agent_row.context_id))

    def refuse_while_escalated(self, operation: str) -> None:
        """Refuse an operation that must wait for the operator (#132)."""

        with database(self.path) as connection:
            row = self._live_row(connection)
            if row is not None:
                self._refuse_while_escalated(connection, str(row["id"]), operation)

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
            "SELECT id FROM operator_question WHERE workflow_id = ?"
            " AND answered IS NULL AND withdrawn IS NULL"
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
        self,
        question: str,
        options: Sequence[str] | None,
        *,
        actor: str,
        session: str | None,
        waiting_on_task_id: str | None = None,
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
            if waiting_on_task_id is not None:
                task = self._require_open_task(connection, waiting_on_task_id)
                if task.workflow_id != workflow_id:
                    raise ConflictError("task belongs to another workflow")
            event_boundary = connection.execute(
                "SELECT COALESCE(MAX(id), 0) FROM event"
            ).fetchone()[0]
            now = self._now_iso()
            cursor = connection.execute(
                "INSERT INTO operator_question"
                " (workflow_id, asked, actor, session, question, options_json,"
                " waiting_on_task_id, asked_after_event_id)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    workflow_id,
                    now,
                    actor,
                    session,
                    question,
                    None if options is None else json.dumps(list(options)),
                    waiting_on_task_id,
                    event_boundary,
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

    def withdraw_question(
        self, question_id: int, reason: str, event_id: int, *, actor: str, session: str | None
    ) -> dict[str, Any]:
        """Close a moot task-wait question without creating operator authority (#171)."""

        if not reason.strip():
            raise ValueError("reason must not be empty")
        if _json_size_bytes(reason) > MAX_MESSAGE_PART_BYTES:
            raise PayloadTooLargeError(f"reason exceeds maximum {MAX_MESSAGE_PART_BYTES} bytes")
        with database(self.path) as connection:
            connection.execute("BEGIN IMMEDIATE")
            question = connection.execute(
                "SELECT * FROM operator_question WHERE id = ?", (question_id,)
            ).fetchone()
            if question is None:
                raise NotFoundError(f"unknown operator question: {question_id}")
            if question["actor"] != actor or question["session"] != session:
                raise ConflictError("only the asking actor and session may withdraw a question")
            if question["answered"] is not None or question["withdrawn"] is not None:
                raise ConflictError(f"operator question {question_id} is already closed")
            task_id = question["waiting_on_task_id"]
            if task_id is None:
                raise ConflictError(
                    "only task-wait questions can be withdrawn; wait for the operator"
                )
            event = connection.execute("SELECT * FROM event WHERE id = ?", (event_id,)).fetchone()
            if (
                event is None
                or event_id <= question["asked_after_event_id"]
                or event["kind"] != EventKind.TASK_COMPLETED.value
                or json.loads(event["payload_json"]).get("task_id") != task_id
            ):
                raise ConflictError(
                    "withdrawal requires a later task_completed event for the waiting task"
                )
            task = self._require_task(connection, task_id)
            if task.workflow_id != question["workflow_id"] or task.state != TaskState.COMPLETED:
                raise ConflictError("the waiting task has not completed")
            now = self._now_iso()
            connection.execute(
                "UPDATE operator_question SET withdrawn = ?, withdrawn_by = ?,"
                " withdrawn_session = ?, withdrawal_reason = ?, settling_event_id = ? WHERE id = ?",
                (now, actor, session, reason, event_id, question_id),
            )
            connection.execute(
                "INSERT INTO decision (ts, summary, rationale, actor, session)"
                " VALUES (?, ?, ?, ?, ?)",
                (
                    now,
                    f"Withdrew operator question {question_id}",
                    f"Task {task_id} completed in event {event_id}; {reason}",
                    actor,
                    session,
                ),
            )
        return {"question_id": question_id, "withdrawn": now, "event_id": event_id}

    def open_operator_questions(self) -> list[dict[str, Any]]:
        """The questions still waiting for the operator, oldest first."""

        with database(self.path) as connection:
            rows = connection.execute(
                "SELECT id, asked, actor, question, options_json FROM operator_question"
                " WHERE answered IS NULL AND withdrawn IS NULL ORDER BY id"
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
        """One question's answer or unanswered/withdrawn status; withdrawal grants no authority."""

        with database(self.path) as connection:
            row = connection.execute(
                "SELECT id, question, answer, answered, withdrawn, withdrawal_reason,"
                " settling_event_id FROM operator_question WHERE id = ?",
                (question_id,),
            ).fetchone()
        if row is None:
            raise NotFoundError(f"unknown operator question: {question_id}")
        result = {
            "question_id": row["id"],
            "question": row["question"],
            "status": "withdrawn"
            if row["withdrawn"] is not None
            else ("unanswered" if row["answered"] is None else "answered"),
            "answer": row["answer"],
            "answered": row["answered"],
        }
        if row["withdrawn"] is not None:
            result.update(
                withdrawn=row["withdrawn"],
                withdrawal_reason=row["withdrawal_reason"],
                settling_event_id=row["settling_event_id"],
            )
        return result

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
                "SELECT answered, withdrawn FROM operator_question WHERE id = ?", (question_id,)
            ).fetchone()
            if row is None:
                raise NotFoundError(f"unknown operator question: {question_id}")
            if row["answered"] is not None:
                raise ConflictError(f"operator question {question_id} is already answered")
            if row["withdrawn"] is not None:
                raise ConflictError(f"operator question {question_id} is withdrawn")
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
            workflow = self._require_live_or_recent(connection)
            cursor = connection.execute(
                "INSERT INTO gate_reading (ts, workflow_id, pr_url, expected_head_sha,"
                " current_head_sha, pr_state, head_matches, ci, mergeable, merge_state_status,"
                " base_ref, base_sha, main_sha, base_behind_main, elapsed_s, checks_json,"
                " error_code, merged_sha) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,"
                " ?, ?)",
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
                    report.merged_sha if report and report.pr_state is PrState.MERGED else None,
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
        standing_role: StandingRole | str = StandingRole.WORKER,
        takeover: bool = False,
        worker_instance_id: str | None = None,
        operation_id: str | None = None,
        payload_hash: str | None = None,
        response_builder: Callable[[AgentRecord], str] | None = None,
        remote_addr: str | None = None,
        workspace_path: str | None = None,
        workspace_kind: str | None = None,
    ) -> Any:
        """Register a worker, or re-admit a returning one on its own context.

        The profile replaces whatever was recorded before, field by field: a
        returning worker may be a different harness or model under the same
        name, and a stale value would mislead role selection. `remote_addr`,
        the peer the request arrived from, likewise replaces both recorded
        addresses; a replayed check-in records nothing. `workspace_path` and
        `workspace_kind` (`worktree` or `clone`, M2 Step 7a #244) replace the
        recorded workspace location; None leaves a legacy row without one.
        """

        name = validate_agent_name(name)
        try:
            role_obj = (
                StandingRole(standing_role) if isinstance(standing_role, str) else standing_role
            )
        except ValueError:
            role_obj = None
        if role_obj not in WORKER_STANDING_ROLES:
            allowed = ", ".join(r.value for r in WORKER_STANDING_ROLES)
            raise ValueError(f"standing_role must be one of {allowed} (got {standing_role!r})")
        if workspace_kind not in (None, "worktree", "clone"):
            raise ValueError(
                f"workspace_kind must be 'worktree' or 'clone' (got {workspace_kind!r})"
            )
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
            workspace_path,
            workspace_kind,
            profile.declared_model,
            remote_addr,
            remote_addr,
            role_obj.value,
            profile.harness_source.value,
            profile.effort,
            profile.effort_source.value,
        )
        reattached: dict[str, Any] | None = None
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
                    " workspace_path, workspace_kind,"
                    " declared_model, checkin_remote_addr, last_remote_addr,"
                    " standing_role, harness_source, effort, effort_source)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
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
                if row["standing_role"] == StandingRole.ORCHESTRATOR.value:
                    raise DuplicateAgentError(f"agent {name} is already registered as orchestrator")
                previous = AgentStatus(row["status"])
                previous_instance = str(row["worker_instance_id"])
                if (
                    not takeover
                    and previous in (AgentStatus.IDLE, AgentStatus.BUSY)
                    and previous_instance
                ):
                    raise DuplicateAgentError(
                        f"agent {name} already has a live worker instance; "
                        "ask the user before calling join(..., takeover=True) to supersede it"
                    )
                # A returning worker keeps its context id so Alice reads one
                # unbroken thread per agent across restarts.
                context_id = row["context_id"]
                current = self._open_task_id(connection, row["current_task_id"])
                if takeover and current is not None:
                    open_task = self._require_task(connection, current)
                    if open_task.state is TaskState.SUBMITTED:
                        self._set_state(connection, current, TaskState.WORKING)
                        open_task = self._require_task(connection, current)
                    reattached = {
                        "task_id": open_task.id,
                        "role": open_task.role,
                        "title": open_task.title,
                        "instructions": open_task.instructions,
                        "state": open_task.state.value,
                        "lease_expires": open_task.lease_expires,
                        "pr_head_sha": open_task.pr_head_sha,
                    }
                connection.execute(
                    "UPDATE agent SET status = ?, last_seen = ?, worker_instance_id = ?,"
                    " last_heartbeat = ?, last_progress_at = ?, current_task_id = ?,"
                    " capabilities_json = ?, harness = ?, harness_version = ?, provider = ?,"
                    " model = ?, model_source = ?, workspace_id = ?,"
                    " workspace_path = ?, workspace_kind = ?, declared_model = ?,"
                    " checkin_remote_addr = ?, last_remote_addr = ?,"
                    " standing_role = ?, harness_source = ?, effort = ?, effort_source = ?"
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
            profile_payload = _profile_fields(profile, role_obj)
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
                | {
                    "context_id": context_id,
                    "worker_instance_id": instance_id,
                    "takeover": takeover,
                    "reattached_task_id": reattached["task_id"] if reattached else None,
                },
            )
            agent = replace(self._require_agent(connection, name), reattached_task=reattached)
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
            clock_shift = float(
                connection.execute(
                    "SELECT lease_clock_shift_s FROM task WHERE id = ?", (task.id,)
                ).fetchone()["lease_clock_shift_s"]
            )
            cap = _parse_timestamp(task.created) + timedelta(
                minutes=cap_minutes, seconds=clock_shift
            )
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

        if role in (TaskRole.REVIEWER, TaskRole.REBASE) and pr_head_sha is None:
            raise ValueError(f"pr_head_sha is required for {role} tasks")
        if pr_head_sha is not None and not SHA_HEX_40_RE.fullmatch(pr_head_sha):
            raise ValueError("pr_head_sha must be a 40-character hex commit SHA")
        head = None if pr_head_sha is None else pr_head_sha.lower()
        now_moment = self._now()
        now = to_iso(now_moment)
        with database(self.path) as connection:
            workflow_id = self._require_workflow(connection, "assign_task")
            workflow_row = connection.execute(
                "SELECT run_id, statement_sha256, branch, goal FROM workflow WHERE id = ?",
                (workflow_id,),
            ).fetchone()
            assert workflow_row is not None
            run_id = str(workflow_row["run_id"])
            statement_sha256 = workflow_row["statement_sha256"]
            # Named branches (M2 Step 7c, #315): the hub names
            # `robomate/<hub>/<agent>/<label>` in the run's first implementer
            # assignment and later tasks of the run reuse it.
            branch: str | None = None
            try:
                existing_branch = workflow_row["branch"]
            except (KeyError, IndexError):
                existing_branch = None
            if existing_branch is not None:
                branch = str(existing_branch)
            elif role == TaskRole.IMPLEMENTER.value and self.hub_name is not None:
                from .branches import (
                    BranchError,
                    branch_name,
                    derive_branch_label,
                    ensure_branch_available,
                )

                label = derive_branch_label(str(workflow_row["goal"] or ""))
                try:
                    candidate = branch_name(self.hub_name, agent, label)
                    ensure_branch_available(self.git_common_dir, candidate)
                except BranchError as exc:
                    raise ConflictError(str(exc)) from exc
                connection.execute(
                    "UPDATE workflow SET branch = ? WHERE id = ?",
                    (candidate, workflow_id),
                )
                branch = candidate
            assignment_metadata: dict[str, Any] = {
                MetaKeys.KIND: "assignment",
                MetaKeys.ROLE: role,
                MetaKeys.TITLE: title,
                MetaKeys.RUN_ID: run_id,
            }
            if statement_sha256 is not None:
                assignment_metadata[MetaKeys.STATEMENT_SHA256] = str(statement_sha256)
            if branch is not None:
                assignment_metadata[MetaKeys.BRANCH] = branch
            if head is not None:
                assignment_metadata[MetaKeys.PR_HEAD_SHA] = head
            lease_cap_min = self._max_task_lease_min(connection, workflow_id)
            effective_lease_min = min(lease_min, lease_cap_min)
            if source_event_id is not None:
                source = connection.execute(
                    "SELECT id FROM event WHERE id = ?", (source_event_id,)
                ).fetchone()
                if source is None:
                    raise NotFoundError(f"unknown source event: {source_event_id}")
                existing = connection.execute(
                    "SELECT task.*, workflow.run_id, workflow.statement_sha256, workflow.branch"
                    " FROM task LEFT JOIN workflow ON task.workflow_id = workflow.id"
                    " WHERE task.source_event_id = ?",
                    (source_event_id,),
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
            if not standing_role_accepts_task(record.standing_role, role):
                raise ValueError(
                    f"agent {agent} has standing role {record.standing_role.value} "
                    f"and cannot accept {role} tasks"
                )
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
            row = connection.execute(
                "SELECT task.*, workflow.run_id, workflow.statement_sha256, workflow.branch"
                " FROM task LEFT JOIN workflow ON task.workflow_id = workflow.id"
                " WHERE task.id = ?",
                (task_id,),
            ).fetchone()
        return None if row is None else _task(row)

    def task_context_id(self, task_id: str) -> str:
        """Return the context of the agent a task is assigned to, if any."""

        with database(self.path) as connection:
            return self._task_context_id(connection, self._require_task(connection, task_id))

    def tasks(self) -> list[TaskRecord]:
        with database(self.path) as connection:
            rows = connection.execute(
                "SELECT task.*, workflow.run_id, workflow.statement_sha256, workflow.branch"
                " FROM task LEFT JOIN workflow ON task.workflow_id = workflow.id"
                # Durable insertion order (#209): `created` can step backwards.
                " ORDER BY task.rowid"
            ).fetchall()
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

            task = self._require_open_task_for_worker(connection, task_id, agent)
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
            task = self._require_open_task_for_worker(connection, task_id, agent)
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

            task = self._require_open_task_for_worker(connection, task_id, agent)
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
        row = connection.execute(
            "SELECT task.*, workflow.run_id, workflow.statement_sha256, workflow.branch"
            " FROM task LEFT JOIN workflow ON task.workflow_id = workflow.id"
            " WHERE task.id = ?",
            (task_id,),
        ).fetchone()
        if row is None:
            raise NotFoundError(f"unknown task: {task_id}")
        return _task(row)

    def _require_open_task(self, connection: Connection, task_id: str) -> TaskRecord:
        task = self._require_task(connection, task_id)
        if task.state in TERMINAL_STATES:
            raise ConflictError(f"task {task_id} is already {task.state.value}")
        return task

    def _require_open_task_for_worker(
        self, connection: Connection, task_id: str, agent_name: str
    ) -> TaskRecord:
        """Like `_require_open_task` but names what the caller still holds (#326).

        Only the caller's own `current_task_id` is ever named: no counts or
        lists of other workers' tasks.
        """

        task = self._require_task(connection, task_id)
        if task.state not in TERMINAL_STATES:
            return task
        base = f"task {task_id} is already {task.state.value}"
        row = connection.execute(
            "SELECT current_task_id FROM agent WHERE name = ?", (agent_name,)
        ).fetchone()
        held_id = self._open_task_id(connection, row["current_task_id"] if row else None)
        if held_id is None or held_id == task_id:
            raise ConflictError(f"{base}. You hold no open task; call await_assignment")
        held = self._require_task(connection, held_id)
        raise ConflictError(
            f'{base}. You hold open task {held.id} ("{held.title}"); use that task_id'
        )

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
        # An inbox read can win the scheduling race with the first wake sweep.
        self._detect_pause(now_moment, self.monotonic_clock())
        grace = self._resume_grace_remaining() > 0
        now = to_iso(now_moment)
        expires = to_iso(now_moment + timedelta(seconds=effective_lease_s))
        delivery_id = uuid4().hex
        with database(self.path) as connection:
            row = connection.execute(
                """
                SELECT * FROM event
                WHERE state = 'queued'
                   OR (state = 'delivered' AND delivery_expires <= ? AND ? = 0)
                ORDER BY id ASC
                LIMIT 1
                """,
                (now, int(grace)),
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
        self._detect_pause(now_moment, self.monotonic_clock())
        with database(self.path) as connection:
            row = connection.execute(
                "SELECT MIN(delivery_expires) AS min_exp FROM event WHERE state = 'delivered'"
            ).fetchone()
            if row is None or row["min_exp"] is None:
                return None
            min_exp = _parse_timestamp(row["min_exp"])
            delta = (min_exp - now_moment).total_seconds()
            return max(0.01, delta, self._resume_grace_remaining())

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
            # Durable insertion order (#209): `created` can step backwards.
            row = connection.execute(
                "SELECT * FROM task WHERE assignee = ? AND state = ? ORDER BY rowid LIMIT 1",
                (name, TaskState.SUBMITTED.value),
            ).fetchone()
            if row is not None:
                self._set_state(connection, row["id"], TaskState.WORKING)
                return self._require_task(connection, row["id"])
            # No new work: a worker that lost track of its open task waits
            # forever without this (#326). Return the task it already holds,
            # marked as a redelivery, instead of timing out. No state or lease
            # change: the heartbeat still owns renewal and `worker_hold`
            # still exempts the surrounding wait.
            held_id = self._open_task_id(connection, agent.current_task_id)
            if held_id is None:
                return None
            held = self._require_task(connection, held_id)
            if held.assignee != name:
                return None
            if held.state not in (
                TaskState.WORKING,
                TaskState.INPUT_REQUIRED,
                TaskState.SUBMITTED,
            ):
                return None
            if held.state is TaskState.SUBMITTED:
                # Assigned but never claimed (e.g. a crash between assign and
                # claim): claim it now as new work, not a redelivery.
                self._set_state(connection, held.id, TaskState.WORKING)
                return self._require_task(connection, held.id)
            return replace(held, already_held=True)

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

    def _resume_grace_remaining(self) -> float:
        return max(0.0, self._resume_grace_until - self.monotonic_clock())

    def _detect_pause(self, now: datetime, mono: float) -> EventRecord | None:
        """Compare with the last sweep; record a discontinuity before any expiry.

        WSL pauses leave monotonic time behind wall time. On hosts whose
        monotonic clock counts sleep, a sweep gap longer than the loss window
        is also suspect. The latter can be scheduler starvation; granting the
        same bounded recovery window is safer than losing every worker.
        No baseline survives a process restart (restart reconciliation is #18).
        """

        previous = self._sweep_sample
        lost_after_s = self._sweep_lost_after_s
        if previous is None or lost_after_s is None or lost_after_s <= 0:
            return None
        wall_elapsed = (now - previous[0]).total_seconds()
        mono_elapsed = mono - previous[1]
        jump = wall_elapsed - mono_elapsed
        clock_jump = jump > CLOCK_JUMP_TOLERANCE_S
        sweep_gap = wall_elapsed > lost_after_s and mono_elapsed > lost_after_s
        if not clock_jump and not sweep_gap:
            return None
        pause_s = jump if clock_jump else wall_elapsed
        # For a wall jump, matching monotonic progress was active time. A
        # long gap on both clocks has no such measurement: conservatively
        # treat that gap as paused, bounded by each task's age.
        pause_start = now - timedelta(seconds=pause_s)
        with database(self.path) as connection:
            connection.execute("BEGIN IMMEDIATE")
            rows = connection.execute(
                "SELECT id, created, lease_expires FROM task"
                f" WHERE state IN ({_placeholders(OPEN_STATES)})"
                " AND lease_expires > ?",
                (*(state.value for state in OPEN_STATES), to_iso(pause_start)),
            ).fetchall()
            shifted = 0
            for row in rows:
                created = _parse_timestamp(row["created"])
                # A task created after the clock jumped never saw the pause.
                if clock_jump and created > pause_start:
                    continue
                shift_s = min(pause_s, max(0.0, (now - created).total_seconds()))
                if shift_s <= 0:
                    continue
                expires = _parse_timestamp(row["lease_expires"]) + timedelta(seconds=shift_s)
                connection.execute(
                    "UPDATE task SET lease_expires = ?,"
                    " lease_clock_shift_s = lease_clock_shift_s + ?, updated = ? WHERE id = ?",
                    (to_iso(expires), shift_s, to_iso(now), row["id"]),
                )
                shifted += 1
            payload = {
                "reason": "clock_jump" if clock_jump else "sweep_gap",
                "previous_sweep_at": to_iso(previous[0]),
                "detected_at": to_iso(now),
                "wall_elapsed_s": wall_elapsed,
                "monotonic_elapsed_s": mono_elapsed,
                "clock_jump_s": jump,
                "grace_s": lost_after_s,
                "lease_shift_s": pause_s,
                "shifted_task_leases": shifted,
            }
            event_id = self._add_event(connection, EventKind.HOST_PAUSED, payload)
            event = _event(
                connection.execute("SELECT * FROM event WHERE id = ?", (event_id,)).fetchone()
            )
        # Publish the baseline only after the durable event commits, so a
        # failed write is retried instead of silently losing the diagnostic.
        self._sweep_sample = (now, mono)
        self._resume_grace_until = mono + lost_after_s
        self.signals.notify(EVENT_KEY)
        logger.warning("Host pause/clock jump detected: %s", event.payload)
        return event

    def sweep(self, lost_after_s: float) -> list[EventRecord]:
        """Expire overdue leases and declare silent agents lost.

        Returns the events it queued, so the caller can log what changed.
        """

        now = self._now()
        mono = self.monotonic_clock()
        self._sweep_lost_after_s = lost_after_s
        pause = self._detect_pause(now, mono)
        self._sweep_sample = (now, mono)
        if self._resume_grace_remaining() > 0:
            return [] if pause is None else [pause]
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
            f"SELECT * FROM agent WHERE status IN ({_placeholders(live)}) "
            "AND last_heartbeat <= ? AND standing_role != 'orchestrator'",
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

    def _is_current_instance(self, name: str, instance: str) -> bool:
        """Whether `instance` is the durable, live instance for `name` right now.

        Checked before every tracker change, so a replayed check-in or a held
        stream from a superseded instance cannot replace the current one's
        record (#144).
        """

        with database(self.path) as connection:
            row = connection.execute(
                "SELECT worker_instance_id, status FROM agent WHERE name = ?", (name,)
            ).fetchone()
        return (
            row is not None
            and bool(instance)
            and row["worker_instance_id"] == instance
            and row["status"] != AgentStatus.LOST.value
        )

    def note_worker_call(self, name: str, instance: str, kind: str) -> None:
        """A substantive call by the worker instance that is current for `name`."""

        if self._is_current_instance(name, instance):
            self.activity.worker_call(name, instance, kind, self._now())

    def worker_hold(
        self, name: str, instance: str, kind: str, timeout_s: float
    ) -> AbstractContextManager[None]:
        """Exempt a worker from the silence check while one of its calls is held.

        A hold from an instance that is no longer current records nothing.
        """

        if not self._is_current_instance(name, instance):
            return nullcontext()
        return self.activity.worker_hold(name, instance, kind, timeout_s, self._now)

    def join_orchestrator(
        self,
        name: str,
        session: str,
        profile: AgentProfile,
        *,
        workspace_path: str | None = None,
        workspace_kind: str | None = None,
    ) -> None:
        """Record the joined role/profile in the existing agent columns, without a worker event.

        Orchestrator liveness and stalls use the separate session tracker, not
        the worker heartbeat/assignment lifecycle. `workspace_path`/`kind`
        (M2 Step 7a, #244) record the orchestrator's worktree (`worktree`) or
        explicit clone (`clone`); None leaves a legacy row without one.
        """
        name = validate_agent_name(name)
        if workspace_kind not in (None, "worktree", "clone"):
            raise ValueError(
                f"workspace_kind must be 'worktree' or 'clone' (got {workspace_kind!r})"
            )
        with database(self.path) as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT * FROM agent WHERE name = ?", (name,)).fetchone()
            if row is not None and row["standing_role"] != StandingRole.ORCHESTRATOR.value:
                raise DuplicateAgentError(f"agent {name} is already registered as a worker")
            if profile.workspace_id is not None:
                owner = connection.execute(
                    "SELECT name FROM agent WHERE workspace_id = ? AND name != ? "
                    "AND standing_role != 'orchestrator' AND status IN ('idle', 'busy')",
                    (profile.workspace_id, name),
                ).fetchone()
                if owner is not None:
                    raise DuplicateAgentError(
                        f"workspace is occupied by live agent {owner['name']}"
                    )
            connection.execute(
                "UPDATE agent SET status = 'released' WHERE standing_role = 'orchestrator' "
                "AND name != ?",
                (name,),
            )
            values = {
                "status": "idle",
                "worker_instance_id": session,
                "last_seen": self._now_iso(),
                "last_heartbeat": self._now_iso(),
                "standing_role": StandingRole.ORCHESTRATOR.value,
                "capabilities_json": json.dumps(list(profile.capabilities)),
                **{
                    key: value.value if isinstance(value, ModelSource) else value
                    for key, value in asdict(profile).items()
                    if key != "capabilities"
                },
                "workspace_path": workspace_path,
                "workspace_kind": workspace_kind,
            }
            if row is None:
                values = {"name": name, "context_id": uuid4().hex, **values}
                connection.execute(
                    f"INSERT INTO agent ({', '.join(values)}) "
                    f"VALUES ({_placeholders(tuple(values))})",
                    tuple(values.values()),
                )
            else:
                connection.execute(
                    f"UPDATE agent SET {', '.join(f'{key} = ?' for key in values)} WHERE name = ?",
                    (*values.values(), name),
                )

    def note_orchestrator_session(self, actor: str, session: str) -> None:
        """The session `/rpc` just accepted as the current orchestrator."""

        self.activity.orchestrator_accepted(actor, session, self._now())

    def note_orchestrator_heartbeat(self, session: str) -> None:
        self.activity.orchestrator_heartbeat(session, self._now())
        with database(self.path) as connection:
            connection.execute(
                "UPDATE agent SET last_seen = ?, last_heartbeat = ? "
                "WHERE standing_role = 'orchestrator' AND worker_instance_id = ?",
                (self._now_iso(), self._now_iso(), session),
            )

    @contextmanager
    def orchestrator_call(self, session: str, kind: str, timeout_s: float) -> Iterator[None]:
        """Count one orchestrator operation as activity, and hold while it runs.

        Its start and its return are also persisted as her last call (#95).
        """

        try:
            with self.activity.orchestrator_call(session, kind, timeout_s, self._now):
                self._persist_orchestrator_call(session)
                yield
        finally:
            self._persist_orchestrator_call(session)

    def _persist_orchestrator_call(self, session: str) -> None:
        """Keep the current session's last call in `orchestrator_last_call` (#95).

        Display only, so a failed write is logged rather than failing a call
        the orchestrator has already made.
        """

        tracker = self.activity
        record = tracker.orchestrator()
        if (
            record is None
            or session != tracker.orchestrator_session
            or tracker.orchestrator_actor is None
            or record.last_call is None
            or record.last_call_kind is None
        ):
            return
        try:
            with database(self.path) as connection:
                connection.execute(
                    "INSERT INTO orchestrator_last_call (id, actor, session, kind, ts)"
                    " VALUES (1, ?, ?, ?, ?) ON CONFLICT (id) DO UPDATE SET"
                    " actor = excluded.actor, session = excluded.session,"
                    " kind = excluded.kind, ts = excluded.ts",
                    (
                        tracker.orchestrator_actor,
                        session,
                        record.last_call_kind,
                        to_iso(record.last_call),
                    ),
                )
        except Exception:
            logger.exception("Could not persist the orchestrator's last call")

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
        row = self._require_live_or_recent(connection)
        if row is None:
            # Workers may check in before Alice initializes the workflow; they
            # are listed, never assessed.
            return {
                "workflow_id": None,
                "workflow_status": None,
                "stall_after_min": None,
                "orchestrator": None,
                "agents": {
                    r["name"]: {
                        "stalled": False,
                        "reasons": [],
                        "suppressed": "no_workflow",
                        "instance": r["worker_instance_id"] or None,
                        "heartbeat_age_s": _age_seconds(r["last_heartbeat"], now),
                        "task_id": None,
                        "progress_age_s": None,
                    }
                    for r in connection.execute(
                        "SELECT name, worker_instance_id, last_heartbeat FROM agent ORDER BY name"
                    )
                },
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
        if suppressed is None and self._resume_grace_remaining() > 0:
            suppressed = "host_pause_grace"
        orchestrator = self._assess_orchestrator(connection, now, threshold, status, suppressed)
        agents = {
            agent.name: self._assess_worker(connection, agent, now, threshold, suppressed)
            for agent in (
                _agent(r)
                for r in connection.execute(
                    "SELECT * FROM agent WHERE standing_role != 'orchestrator' ORDER BY name"
                )
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
        status: WorkflowStatus,
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
        last_call = self._orchestrator_last_call(connection, record)
        if last_call["last_call_restored"]:
            # Silence still runs from this hub's start; only the display
            # learns what she last did before the restart.
            fields["last_call"] = last_call["kind"]
            fields["last_call_age_s"] = _age_seconds(last_call["last_call_at"], now)
        # Bridge liveness: anything this process heard from her session,
        # heartbeats included. Activity is the last call alone.
        heard = [
            moment
            for moment in (heartbeat, None if record is None else record.last_call)
            if moment is not None
        ]
        bridge_age = None if not heard else max(0.0, (now - max(heard)).total_seconds())
        if fields["holding"] is not None or (
            bridge_age is not None and bridge_age <= BRIDGE_CONNECTED_S
        ):
            bridge = CONNECTED
        elif record is None:
            bridge = NOT_SEEN
        else:
            bridge = DISCONNECTED
        call_age = fields["last_call_age_s"]
        if status == WorkflowStatus.DONE:
            state = FINISHED
        elif status == WorkflowStatus.PAUSED:
            state = PAUSED
        elif fields["holding"] is not None:
            state = HOLDING
        elif call_age is not None and call_age <= IDLE_AFTER_S:
            state = ACTIVE
        elif call_age is None and record is None:
            state = NOT_SEEN
        else:
            state = IDLE
        return {
            "actor": tracker.orchestrator_actor,
            "session": tracker.orchestrator_session,
            "stalled": bool(reasons),
            "reasons": reasons,
            "suppressed": suppressed,
            **fields,
            "heartbeat_age_s": None if heartbeat is None else _age_seconds(to_iso(heartbeat), now),
            # Additive since #95.
            "state": state,
            "bridge": bridge,
            "bridge_age_s": None if bridge_age is None else round(bridge_age, 1),
            "last_call_at": last_call["last_call_at"],
            "last_call_actor": last_call["actor"],
            "last_call_restored": last_call["last_call_restored"],
            "unattempted_events": len(rows),
            "stale_events": [
                {"id": row["id"], "kind": row["kind"], "age_s": _age_seconds(row["ts"], now)}
                for row in backlog[:STALL_EVENT_SAMPLE]
            ],
        }

    def _orchestrator_last_call(
        self, connection: Connection, record: Activity | None
    ) -> dict[str, Any]:
        """Her last call from this process's memory, else as a previous one persisted it."""

        if record is not None and record.last_call is not None:
            return {
                "actor": self.activity.orchestrator_actor,
                "kind": record.last_call_kind,
                "last_call_at": to_iso(record.last_call),
                "last_call_restored": False,
            }
        row = connection.execute(
            "SELECT actor, kind, ts FROM orchestrator_last_call WHERE id = 1"
        ).fetchone()
        if row is None:
            return {"actor": None, "kind": None, "last_call_at": None, "last_call_restored": False}
        return {
            "actor": row["actor"],
            "kind": row["kind"],
            "last_call_at": row["ts"],
            "last_call_restored": True,
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
        self._detect_pause(now, self.monotonic_clock())
        # Freeze episodes as well as new diagnostics during wake recovery.
        if self._resume_grace_remaining() > 0:
            return []
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
