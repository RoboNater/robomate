"""Delivery evidence for lifecycle fixtures that exercise other hub behavior."""

import json
from uuid import uuid4

from agent_hub.database import database
from agent_hub.merge_gate import CiStatus, GateReport, Mergeable, PrState
from agent_hub.store import HubStore
from agent_hub_common import WorkflowStatus

PR = "https://github.com/octo/sandbox/pull/7"
HEAD = "0123456789abcdef0123456789abcdef01234567"
MERGED = "2" * 40


class DeliveredForge:
    """A forge whose chosen PR is in the fixture's final delivery state."""

    def __init__(self, state: PrState = PrState.MERGED) -> None:
        self.state = state

    def reading(self, pr_url: str, expected_head_sha: str) -> GateReport:
        return GateReport(
            pr_url,
            self.state,
            expected_head_sha,
            expected_head_sha,
            True,
            CiStatus.PASS,
            [],
            "main",
            HEAD,
            HEAD,
            False,
            Mergeable.CLEAN,
            "clean",
            merged_sha=MERGED if self.state == PrState.MERGED else None,
        )

    async def check(self, pr_url: str, expected_head_sha: str, *, wait: bool = True) -> GateReport:
        return self.reading(pr_url, expected_head_sha)


def seed_delivery(store: HubStore, *, closeout: bool = True) -> str:
    """Set up persisted evidence without adding agents or queued events."""

    run_id = store.get_state()["run_id"]
    with database(store.path) as connection:
        row = connection.execute(
            "SELECT id, created FROM workflow WHERE run_id = ?", (run_id,)
        ).fetchone()
        connection.execute(
            "UPDATE workflow SET pr_url = COALESCE(pr_url, ?),"
            " approved_sha = COALESCE(approved_sha, ?) WHERE id = ?",
            (PR, HEAD, row["id"]),
        )
        if (
            closeout
            and not connection.execute(
                "SELECT 1 FROM task WHERE workflow_id = ? "
                "AND role = 'closeout' AND state = 'completed'",
                (row["id"],),
            ).fetchone()
        ):
            result = {"outcome": "completed", "summary": "Closed out", "items": [], "steps": []}
            connection.execute(
                "INSERT INTO task (id, workflow_id, role, title, instructions,"
                " state, result_json, created, updated)"
                " VALUES (?, ?, 'closeout', 'Close out', '', 'completed', ?, ?, ?)",
                (uuid4().hex, row["id"], json.dumps(result), row["created"], row["created"]),
            )
    return str(run_id)


def finish_run(store: HubStore, summary: str, *, actor: str, session: str | None) -> None:
    run_id = seed_delivery(store)
    done = store.prepare_done(run_id)
    if done is not None:
        state = PrState.OPEN if done.policy.deliver == "pr" else PrState.MERGED
        store.set_workflow_status(
            WorkflowStatus.DONE,
            summary,
            actor=actor,
            session=session,
            done=done,
            report=DeliveredForge(state).reading(done.pr_url, done.approved_sha),
        )
