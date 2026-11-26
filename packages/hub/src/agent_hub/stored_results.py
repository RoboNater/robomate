"""Read typed task history, including results written before ID uniqueness checks."""

from __future__ import annotations

from collections.abc import Iterator
from sqlite3 import Connection, Row

from agent_hub_common import (
    CloseoutResult,
    ImplementerResult,
    RebaseResult,
    ReviewerResult,
    TaskResult,
    TaskRole,
)
from pydantic import ValidationError, model_validator


class StoredReviewerResult(ReviewerResult):
    @model_validator(mode="after")
    def validate_finding_ids_unique(self) -> StoredReviewerResult:
        return self


class StoredImplementerResult(ImplementerResult):
    @model_validator(mode="after")
    def validate_finding_ids_unique(self) -> StoredImplementerResult:
        return self


_RESULT_MODELS: dict[TaskRole, type[TaskResult]] = {
    TaskRole.REVIEWER: StoredReviewerResult,
    TaskRole.IMPLEMENTER: StoredImplementerResult,
    TaskRole.REBASE: RebaseResult,
    TaskRole.CLOSEOUT: CloseoutResult,
}


def read_result(row: Row) -> TaskResult | None:
    model = _RESULT_MODELS.get(row["role"])
    if model is None or row["result_json"] is None:
        return None
    try:
        return model.model_validate_json(row["result_json"])
    except ValidationError:
        # Legacy raw status and malformed results supply no typed evidence.
        return None


def task_history(connection: Connection, workflow_id: str) -> Iterator[Row]:
    # Completion event order, with a durable fallback for legacy results.
    return iter(
        connection.execute(
            "SELECT task.rowid AS ordinal, task.id, task.role, task.state, task.assignee,"
            " task.pr_head_sha, task.result_json, task.closeout_context_json FROM task "
            "LEFT JOIN event ON event.kind IN ('task_completed', 'task_failed') "
            "AND json_extract(event.payload_json, '$.task_id') = task.id "
            "WHERE task.workflow_id = ? ORDER BY event.id, task.rowid",
            (workflow_id,),
        )
    )
