from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Annotated, Any, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

# A profile field the adapter was not told is recorded as this, never guessed
# (spec §4.3), so Alice can tell "not reported" apart from a real value.
UNKNOWN = "unknown"


class WorkflowStatus(StrEnum):
    ACTIVE = "active"
    PAUSED = "paused"
    DONE = "done"
    ESCALATED = "escalated"


PositiveNumber = Annotated[int | float, Field(gt=0, allow_inf_nan=False)]


class RolePolicy(BaseModel):
    """The worker-pairing rails accepted in workflow policy (§5)."""

    model_config = ConfigDict(extra="forbid", strict=True)

    reviewer_harness_differs: bool = True
    reviewer_provider_differs: bool = False
    implementer_capabilities: list[str] = Field(default_factory=list)
    reviewer_capabilities: list[str] = Field(default_factory=list)


class WorkflowPolicy(BaseModel):
    """Validated create-once policy; omitted fields retain the §5 defaults."""

    model_config = ConfigDict(extra="forbid", strict=True)

    max_review_rounds: int = Field(default=3, gt=0)
    merge_method: Literal["merge", "squash", "rebase"] = "squash"
    allow_no_ci: bool = False
    role_policy: RolePolicy = Field(default_factory=RolePolicy)
    pairing_wait_s: PositiveNumber = 120
    max_wall_minutes: PositiveNumber = 120
    max_task_lease_min: PositiveNumber = 120


class AgentStatus(StrEnum):
    IDLE = "idle"
    BUSY = "busy"
    RELEASED = "released"
    LOST = "lost"


class TaskState(StrEnum):
    SUBMITTED = "submitted"
    WORKING = "working"
    INPUT_REQUIRED = "input-required"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELED = "canceled"


class EventKind(StrEnum):
    AGENT_CHECKED_IN = "agent_checked_in"
    TASK_PROGRESS = "task_progress"
    TASK_COMPLETED = "task_completed"
    TASK_FAILED = "task_failed"
    WORKER_QUESTION = "worker_question"
    LEASE_EXPIRED = "lease_expired"
    AGENT_LOST = "agent_lost"


class TaskRole(StrEnum):
    """Roles with a typed result body (§4.4); the guide for each is `<role>.md`."""

    IMPLEMENTER = "implementer"
    REVIEWER = "reviewer"
    REBASE = "rebase"


class EventState(StrEnum):
    QUEUED = "queued"
    DELIVERED = "delivered"
    ACKED = "acked"


SCHEMA_VERSION: int = 1

SHA_HEX_40_RE = re.compile(r"^[0-9a-fA-F]{40}$")


class ImplementerOutcome(StrEnum):
    COMPLETED = "completed"
    BLOCKED = "blocked"
    FAILED = "failed"


class ReviewerVerdict(StrEnum):
    APPROVED = "approved"
    CHANGES_REQUESTED = "changes_requested"
    BLOCKED = "blocked"
    FAILED = "failed"


class TestResult(BaseModel):
    __test__ = False
    command: str
    status: str


class Finding(BaseModel):
    id: str = Field(pattern=r"^r\d+-\d+$")
    text: str


class ImplementerResult(BaseModel):
    outcome: ImplementerOutcome
    pr_url: str | None = None
    head_sha: str | None = Field(default=None, pattern=r"^[0-9a-fA-F]{40}$")
    commits: list[str] = Field(default_factory=list)
    tests: list[TestResult] = Field(default_factory=list)
    blocker: str | None = None
    resolved_finding_ids: list[str] | None = None
    disputed_finding_ids: list[str] | None = None
    summary: str

    @model_validator(mode="after")
    def validate_completed(self) -> Self:
        if self.outcome == ImplementerOutcome.COMPLETED:
            if not self.pr_url or not self.pr_url.strip():
                raise ValueError("completed implementer result requires pr_url")
            if not self.head_sha or not SHA_HEX_40_RE.match(self.head_sha):
                raise ValueError("completed implementer result requires head_sha")
        return self


class ReviewerResult(BaseModel):
    verdict: ReviewerVerdict
    pr_url: str | None = None
    review_url: str | None = None
    reviewed_head_sha: str | None = Field(default=None, pattern=r"^[0-9a-fA-F]{40}$")
    blocking_findings: list[Finding] = Field(default_factory=list)
    nonblocking_findings: list[Finding] = Field(default_factory=list)
    tests: list[TestResult] | None = None
    summary: str

    @model_validator(mode="after")
    def validate_approved(self) -> Self:
        if self.verdict == ReviewerVerdict.APPROVED:
            if not self.reviewed_head_sha or not SHA_HEX_40_RE.match(self.reviewed_head_sha):
                raise ValueError("approved reviewer result requires reviewed_head_sha")
            if len(self.blocking_findings) > 0:
                raise ValueError("approved reviewer result requires blocking_findings to be empty")
        return self


class RebaseResult(BaseModel):
    """Bringing an approved PR up to date with its base branch (spec §5 REBASE).

    `conflict_files` decides what follows: empty sends the new head straight
    to MERGE, anything else sends it back to review, because a resolved
    conflict is code no reviewer has read. An empty list is therefore a claim,
    not a default — it must mean git merged every file without a hand edit and
    nothing else needed changing to make the result correct.
    """

    outcome: ImplementerOutcome
    pr_url: str | None = None
    head_sha: str | None = Field(default=None, pattern=r"^[0-9a-fA-F]{40}$")
    conflict_files: list[str] = Field(default_factory=list)
    resolution_summary: str | None = None
    tests: list[TestResult] = Field(default_factory=list)
    blocker: str | None = None
    summary: str

    @model_validator(mode="after")
    def validate_completed(self) -> Self:
        if self.outcome == ImplementerOutcome.COMPLETED:
            if not self.head_sha or not SHA_HEX_40_RE.match(self.head_sha):
                raise ValueError("completed rebase result requires head_sha")
            if self.conflict_files and not (self.resolution_summary or "").strip():
                raise ValueError("rebase result with conflict_files requires resolution_summary")
        return self


TaskResult = ImplementerResult | ReviewerResult | RebaseResult

IMPLEMENTER_RESULT_SCHEMA: dict[str, Any] = ImplementerResult.model_json_schema()
REVIEWER_RESULT_SCHEMA: dict[str, Any] = ReviewerResult.model_json_schema()
REBASE_RESULT_SCHEMA: dict[str, Any] = RebaseResult.model_json_schema()


class ModelSource(StrEnum):
    """Where a worker's `model` came from (spec §3).

    `env` is the operator's launcher configuration (`HUB_MODEL`); `declared` is
    the agent naming its own model at check-in, used only when the launcher
    names none. Neither is attested (§1 non-goals).
    """

    DECLARED = "declared"
    ENV = "env"
    UNKNOWN = UNKNOWN


@dataclass(frozen=True, slots=True)
class AgentProfile:
    """A worker's self-reported identity, which role selection evaluates (§5).

    Observational only: the hub records what the worker says and verifies none
    of it. `workspace_id` stays None until the worker reports one (#28).
    `declared_model` is what the runtime itself reported, kept beside `model`
    because a configured `HUB_MODEL` wins there and would otherwise hide it (#77).
    """

    harness: str = UNKNOWN
    harness_version: str = UNKNOWN
    provider: str = UNKNOWN
    model: str = UNKNOWN
    model_source: ModelSource = ModelSource.UNKNOWN
    capabilities: tuple[str, ...] = field(default_factory=tuple)
    workspace_id: str | None = None
    declared_model: str = UNKNOWN
