"""Project typed, run-scoped evidence for the operator report and status headline."""

from __future__ import annotations

import json
import re
from contextlib import suppress
from dataclasses import dataclass, field
from sqlite3 import Connection, Row
from typing import TYPE_CHECKING, Literal, TypedDict

from agent_hub_common import (
    UNKNOWN,
    CloseoutContext,
    CloseoutResult,
    ImplementerResult,
    ReviewerResult,
    TaskResult,
    TaskRole,
    TaskState,
    WorkflowPolicy,
    WorkflowStatus,
)
from agent_hub_common.models import CloseoutItem
from pydantic import ValidationError

from .closeout_export import HEADER, fenced
from .stored_results import read_result, task_history

if TYPE_CHECKING:
    from .store import AgentRecord, HubStore

# The migration for pre-v22 workflows can append a decimal collision suffix.
_RUN_ID = re.compile(r"[0-9]{8}_[0-9a-f]{8}[0-9]*")


def validate_run_id(run_id: object) -> str:
    if not isinstance(run_id, str) or _RUN_ID.fullmatch(run_id) is None:
        raise ValueError("invalid run ID; expected YYYYMMDD_ and eight lowercase hex digits")
    return run_id


@dataclass(frozen=True, slots=True)
class TaskEvidence:
    ordinal: int
    id: str
    role: str
    state: str
    assignee: str | None
    head_sha: str | None
    result: TaskResult | None
    context: CloseoutContext | None

    @classmethod
    def from_row(cls, row: Row) -> TaskEvidence:
        context = None
        if row["closeout_context_json"] is not None:
            with suppress(ValidationError):
                context = CloseoutContext.model_validate_json(row["closeout_context_json"])
        return cls(
            row["ordinal"],
            row["id"],
            row["role"],
            row["state"],
            row["assignee"],
            row["pr_head_sha"],
            read_result(row),
            context,
        )


FindingState = Literal[
    "unresolved", "resolved by implementer", "disputed, unresolved", "disposition recorded"
]


@dataclass(slots=True)
class FindingHistory:
    observations: list[tuple[TaskEvidence, str, str]] = field(default_factory=list)
    responses: list[tuple[TaskEvidence, str]] = field(default_factory=list)
    dispositions: list[CloseoutItem] = field(default_factory=list)
    state: FindingState = "unresolved"


DeliveryState = Literal["pending", "merged", "pr_open", "not_recorded"]


class ReportHeadline(TypedDict):
    delivery_state: DeliveryState
    unresolved_findings: int
    incomplete_closeout_steps: int | None


@dataclass(slots=True)
class RunEvidence:
    headline: ReportHeadline
    policy: WorkflowPolicy | None
    reviews: list[TaskEvidence]
    findings: dict[str, FindingHistory]
    closeout_task: TaskEvidence | None
    closeout_result: CloseoutResult | None
    other_items: list[CloseoutItem]


def read_evidence(connection: Connection, workflow: Row) -> RunEvidence:
    """Read one workflow's results in completion order within the caller's snapshot."""
    try:
        policy = WorkflowPolicy.model_validate_json(workflow["policy_json"])
    except ValidationError:
        policy = None
    history = [TaskEvidence.from_row(row) for row in task_history(connection, workflow["id"])]
    closeout_tasks = [task for task in history if task.role == TaskRole.CLOSEOUT]
    closeout_task = max(closeout_tasks, key=lambda task: task.ordinal, default=None)
    closeout_result = None
    for task in closeout_tasks:
        if (
            isinstance(task.result, CloseoutResult)
            and task.state == TaskState.COMPLETED
            and task.result.outcome == "completed"
        ):
            closeout_task, closeout_result = task, task.result

    reviews = []
    other_items = []
    findings: dict[str, FindingHistory] = {}
    for task in history:
        result = task.result
        if task.role == TaskRole.REVIEWER:
            reviews.append(task)
        if isinstance(result, ReviewerResult):
            for severity, group in (
                ("blocking", result.blocking_findings),
                ("nonblocking", result.nonblocking_findings),
            ):
                for finding in group:
                    entry = findings.setdefault(finding.id, FindingHistory())
                    entry.observations.append((task, severity, finding.text))
                    entry.state = "unresolved"
        elif isinstance(result, ImplementerResult):
            resolved = set(result.resolved_finding_ids or [])
            disputed = set(result.disputed_finding_ids or [])
            for finding_id in sorted((resolved | disputed) & findings.keys()):
                entry = findings[finding_id]
                if finding_id in disputed:
                    entry.state = "disputed, unresolved"
                    detail = (
                        "resolved and disputed in the same result"
                        if finding_id in resolved
                        else "disputed"
                    )
                else:
                    entry.state = "resolved by implementer"
                    detail = "resolved"
                entry.responses.append((task, detail))
        elif task is closeout_task and closeout_result is not None:
            for item in closeout_result.items:
                if item.source in findings:
                    entry = findings[item.source]
                    entry.dispositions.append(item)
                    entry.state = "disposition recorded"
                else:
                    other_items.append(item)

    incomplete = None
    if closeout_result is not None:
        incomplete = sum(not step.done for step in closeout_result.steps)
        if closeout_task is not None and closeout_task.context is not None:
            recorded = {step.index for step in closeout_result.steps}
            incomplete += sum(step.index not in recorded for step in closeout_task.context.steps)
    elif policy is not None and not policy.closeout:
        incomplete = 0
    elif closeout_task is not None and closeout_task.context is not None:
        incomplete = len(closeout_task.context.steps)
    delivery: DeliveryState
    if workflow["status"] != WorkflowStatus.DONE:
        delivery = "pending"
    elif workflow["delivery_verified_at"] is None:
        delivery = "not_recorded"
    elif workflow["delivery_merged_sha"] is not None:
        delivery = "merged"
    else:
        delivery = "pr_open"
    return RunEvidence(
        {
            "delivery_state": delivery,
            "unresolved_findings": sum(
                entry.state in ("unresolved", "disputed, unresolved") for entry in findings.values()
            ),
            "incomplete_closeout_steps": incomplete,
        },
        policy,
        sorted(reviews, key=lambda task: task.ordinal),
        findings,
        closeout_task,
        closeout_result,
        other_items,
    )


@dataclass(frozen=True, slots=True)
class RunReport:
    run_id: str
    status: str
    created: str
    statement_sha256: str | None
    statement_source: str | None
    statement_link: str | None
    pr_url: str | None
    approved_sha: str | None
    merged_sha: str | None
    agents: list[tuple[str, AgentRecord | None]]
    collection_requested: bool
    evidence: RunEvidence


def snapshot(store: HubStore, connection: Connection, workflow: Row) -> RunReport:
    """Load only this workflow's facts, without reading exports or querying a forge."""
    from .store import ConflictError, DeliveryBinding, _agent, _delivery_binding, _recorded_merge

    workflow_id = workflow["id"]
    evidence = read_evidence(connection, workflow)
    source, issue = store._statement_reference(connection, workflow["statement_sha256"])
    statement_link = _statement_link(store, source, issue)
    try:
        binding = _delivery_binding(connection, workflow)
    except ConflictError:
        # Conflicting legacy results still have their own review and work references.
        binding = DeliveryBinding()
    merged_sha = workflow["delivery_merged_sha"]
    if workflow["delivery_verified_at"] is None and binding.pr_url is not None:
        merged_sha = _recorded_merge(connection, workflow_id, binding.pr_url)
    agents = [
        (row["participant"], _agent(row) if row["name"] is not None else None)
        for row in connection.execute(
            "SELECT participant, agent.* FROM ("
            " SELECT author AS participant FROM closeout_expected WHERE workflow_id = ?"
            " UNION SELECT assignee FROM task WHERE workflow_id = ? AND assignee IS NOT NULL"
            ") LEFT JOIN agent ON agent.name = participant ORDER BY participant",
            (workflow_id, workflow_id),
        )
    ]
    collection_requested = (
        connection.execute(
            "SELECT 1 FROM closeout_collection WHERE workflow_id = ?", (workflow_id,)
        ).fetchone()
        is not None
    )
    return RunReport(
        validate_run_id(workflow["run_id"]),
        workflow["status"],
        workflow["created"],
        workflow["statement_sha256"],
        source,
        statement_link,
        binding.pr_url,
        binding.approved_sha,
        merged_sha,
        agents,
        collection_requested,
        evidence,
    )


def _statement_link(store: HubStore, source: str | None, issue: str | None) -> str | None:
    if source is not None and source.startswith("https://"):
        return source
    # A generated issue statement names an issue of the selected repository.
    if source == "submit:issue" and store.issue_url_base is not None and issue is not None:
        return f"{store.issue_url_base}{issue}"
    return None


class ReportMarkdown:
    def __init__(self, title: str) -> None:
        self.lines = [title, "", HEADER, ""]

    def section(self, title: str) -> None:
        self.lines.extend([title, ""])

    def value(self, label: str, value: str | None) -> None:
        self.lines.extend([label, "", "not recorded" if value is None else fenced(value), ""])

    def field(self, label: str, value: str | int | None) -> None:
        # Only controlled labels and values use this inline representation.
        self.lines.extend([f"{label} · {'not recorded' if value is None else value}", ""])

    def text(self) -> str:
        return "\n".join(self.lines)


def render(report: RunReport) -> str:
    evidence = report.evidence
    phase = "Final" if report.status == WorkflowStatus.DONE else "Draft"
    out = ReportMarkdown(f"# {phase} run report")
    out.value("Run ID", report.run_id)
    out.value("Workflow status", report.status)
    out.value("Created", report.created)
    out.field("Delivery", evidence.headline["delivery_state"])
    out.field("Unresolved findings", evidence.headline["unresolved_findings"])
    out.field("Incomplete closeout steps", evidence.headline["incomplete_closeout_steps"])
    out.value("PR/MR", report.pr_url)
    out.value("Approved head SHA", report.approved_sha)
    out.value("Merged SHA", report.merged_sha)
    out.value("Policy", evidence.policy.model_dump_json(indent=2) if evidence.policy else None)

    out.section("## Statement")
    out.value("Source", report.statement_source)
    out.value("Link", report.statement_link)
    out.value("SHA-256", report.statement_sha256)

    out.section("## Agent profiles")
    out.section("Latest stored profiles for agents associated with this run.")
    if not report.agents:
        out.section("Agent profiles not recorded.")
    for name, agent in report.agents:
        out.value("Agent", name)
        if agent is None:
            out.section("Profile not recorded.")
            continue
        for label, value in (
            ("Standing role", agent.standing_role),
            ("Harness", agent.harness),
            ("Harness source", agent.harness_source),
            ("Harness version", agent.harness_version),
            ("Provider", agent.provider),
            ("Model", agent.model),
            ("Model source", agent.model_source),
            ("Declared model", agent.declared_model),
            ("Effort", agent.effort),
            ("Effort source", agent.effort_source),
        ):
            out.value(label, None if value == UNKNOWN else value)
        out.value("Capabilities", json.dumps(agent.capabilities, ensure_ascii=False))

    out.section("## Review rounds")
    if not evidence.reviews:
        out.section("Review rounds not recorded.")
    for index, task in enumerate(evidence.reviews, 1):
        out.section(f"### Review {index}")
        out.value("Task ID", task.id)
        out.value("Reviewer", task.assignee)
        out.value("Assigned head", task.head_sha)
        result = task.result
        if isinstance(result, ReviewerResult):
            out.field("Verdict", result.verdict)
            out.value("Reviewed head", result.reviewed_head_sha)
            out.value("Reviewed PR/MR", result.pr_url)
            out.value("Review URL", result.review_url)
        else:
            out.value("Task state", task.state)
            out.section("Typed review result not recorded.")

    out.section("## Findings")
    if not evidence.findings:
        out.section("No typed findings recorded.")
    for finding_id, entry in evidence.findings.items():
        # Finding IDs are validated by the typed reviewer result.
        out.section(f"### {finding_id}")
        out.field("Finding status", entry.state)
        blocking = {task.id for task, severity, _ in entry.observations if severity == "blocking"}
        if any(
            task.id in blocking and severity == "nonblocking"
            for task, severity, _ in entry.observations
        ):
            out.section("A review recorded this ID as both blocking and nonblocking.")
        for task, severity, text in entry.observations:
            out.value(f"{severity.capitalize()} finding", text)
            out.value("Review task ID", task.id)
            if isinstance(task.result, ReviewerResult) and task.result.review_url is not None:
                out.value("Review URL", task.result.review_url)
        for task, detail in entry.responses:
            out.field("Implementer response", detail)
            out.value("Implementer task ID", task.id)
            if isinstance(task.result, ImplementerResult):
                out.value("Response head SHA", task.result.head_sha)
                out.value("Response PR/MR", task.result.pr_url)
        for item in entry.dispositions:
            _render_disposition(out, item)

    _render_closeout(out, report)
    return out.text()


def _render_closeout(out: ReportMarkdown, report: RunReport) -> None:
    evidence = report.evidence
    out.section("## Closeout")
    if evidence.policy is not None and not evidence.policy.closeout:
        out.section("Typed closeout disabled by run policy.")
    if not report.collection_requested:
        out.section("Retrospective collection not requested.")
    else:
        out.section("Retrospective collection requested.")
    task, result = evidence.closeout_task, evidence.closeout_result
    if result is None:
        out.section("Completed typed closeout result not recorded.")
    else:
        out.value("Closeout task ID", task.id if task else None)
        out.value("Closeout summary", result.summary)
        for item in evidence.other_items:
            out.value("Disposition source", item.source)
            _render_disposition(out, item)

    out.section("### Steps")
    expected = (
        {step.index: step.text for step in task.context.steps} if task and task.context else {}
    )
    recorded = {step.index: step for step in result.steps} if result else {}
    if not expected and not recorded:
        out.section("No closeout steps recorded.")
    for index in sorted(expected.keys() | recorded.keys()):
        step = recorded.get(index)
        state = "not recorded" if step is None else "done" if step.done else "INCOMPLETE"
        out.field(f"Step {index}", state)
        out.value("Step description", expected.get(index))
        if step is not None and step.url is not None:
            out.value("Step URL", step.url)
        if step is not None and step.note is not None:
            out.value("Step note", step.note)


def _render_disposition(out: ReportMarkdown, item: CloseoutItem) -> None:
    out.field("Disposition", item.action)
    out.value("Disposition summary", item.summary)
    if item.url is not None:
        out.value("Disposition URL", item.url)
    if item.reason is not None:
        out.value("Disposition reason", item.reason)
