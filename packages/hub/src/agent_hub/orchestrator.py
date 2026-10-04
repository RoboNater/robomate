"""Alice's §4.2 operations, independent of the transport that carries them.

The stdio MCP server (`agent_hub.mcp`) and the `/rpc` route (`agent_hub.rpc`)
both call these methods. Each method's name, signature and docstring are its
tool's name, argument schema and description, so edits here change what Alice
sees and what her tool list costs (tests/fixtures/orchestrator-tools.json).
"""

from dataclasses import asdict
from time import monotonic
from typing import Annotated, Any, Literal

from agent_hub_common import TaskState, WorkflowStatus
from pydantic import Field

from .merge_gate import ForgeGate, MergeGate
from .rpc_errors import error_code
from .store import HubStore

Timeout = Annotated[float, Field(ge=0, le=120, allow_inf_nan=False)]
Lease = Annotated[float, Field(gt=0, le=525600, allow_inf_nan=False)]
Sha = Annotated[str, Field(pattern=r"^[0-9a-fA-F]{40}$")]

# Tool names in their listed order, which the tool list's bytes depend on.
OPERATIONS = (
    "get_state",
    "initialize_workflow",
    "wait_for_event",
    "assign_task",
    "check_merge_gate",
    "reply",
    "set_task_state",
    "release_agent",
    "set_workflow_status",
    "log_decision",
)


class OrchestratorOps:
    """The ten orchestrator operations over one store and merge gate."""

    def __init__(self, store: HubStore, gate: ForgeGate | None = None) -> None:
        self.store = store
        self.gate: ForgeGate = gate if gate is not None else MergeGate()

    async def get_state(self) -> dict[str, Any]:
        """Read the workflow, agents and compact task summaries."""
        return self.store.get_state()

    async def initialize_workflow(
        self, goal: str, policy: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        """Persist the initial prompt's goal and policy, or confirm them on restart.

        This must be Alice's first mutating call. Goal and policy are immutable;
        use get_state to resume the stored workflow after a restart.
        """

        workflow_id = self.store.initialize_workflow(goal, policy)
        workflow = self.store.get_state()["workflow"]
        return {"id": workflow_id, "goal": workflow["goal"], "policy": workflow["policy"]}

    async def wait_for_event(
        self, timeout_s: Timeout = 100, ack: str | None = None
    ) -> dict[str, Any]:
        """Wait for and lease the oldest event. On event=null, call again.

        ack: the delivery_id of the event just handled. It still acks after
        the delivery lease has expired, so a long action — merging, say — is
        not redone once it has finished.
        """
        event = await self.store.wait_for_event(timeout_s=timeout_s, ack=ack)
        return {"event": None if event is None else asdict(event)}

    async def assign_task(
        self,
        agent: str,
        role: str,
        title: str,
        instructions: str,
        event_id: int,
        lease_min: Lease = 30,
        pr_head_sha: Sha | None = None,
    ) -> dict[str, Any]:
        """Assign work to an idle worker and wake its pending NEXT.

        event_id: the durable event whose handling causes this assignment.
        role: implementer, reviewer or rebase — the guide the worker fetches.
        pr_head_sha: the PR head a review or rebase is bound to.
        """
        return asdict(
            self.store.assign_task(
                agent,
                role,
                title,
                instructions,
                lease_min,
                pr_head_sha,
                source_event_id=event_id,
            )
        )

    async def check_merge_gate(self, pr_url: str, expected_head_sha: Sha) -> dict[str, Any]:
        """Read the PR's head, CI, base freshness and mergeability before merging.

        expected_head_sha: the approved head — the reviewer's reviewed_head_sha,
        or the head_sha of a rebase that reported no conflict_files.
        Waits up to 60 s while CI or mergeability is still settling. Call it
        immediately before merging; earlier results are advisory.
        """
        started = monotonic()
        try:
            report = await self.gate.check(pr_url, expected_head_sha)
        except Exception as exc:
            self.store.record_gate_reading(
                pr_url, expected_head_sha, error_code=error_code(exc),
                elapsed_s=round(monotonic() - started, 3),
            )
            raise
        self.store.record_gate_reading(
            pr_url, expected_head_sha, report=report, elapsed_s=report.elapsed_s,
        )
        return asdict(report)

    async def reply(self, task_id: str, text: str, message_id: int) -> dict[str, bool]:
        """Answer the named worker question and return its task to working."""
        applied = self.store.reply(task_id, text, message_id=message_id)
        return {"ok": True, "applied": applied}

    async def set_task_state(
        self, task_id: str, state: Literal["canceled", "failed"], note: str
    ) -> dict[str, Any]:
        """Cancel or fail an open task, retaining the note and freeing its worker."""
        return asdict(self.store.set_task_state(task_id, TaskState(state), note))

    async def release_agent(self, agent: str) -> dict[str, Any]:
        """Release a worker; its next NEXT returns the release marker."""
        return asdict(self.store.release_agent(agent))

    async def set_workflow_status(self, status: WorkflowStatus, summary: str) -> dict[str, bool]:
        """Set active/paused/done/escalated and save the summary in the audit log."""
        self.store.set_workflow_status(status, summary)
        return {"ok": True}

    async def log_decision(
        self, summary: str, rationale: str, key: str | None = None
    ) -> dict[str, int]:
        """Append a durable audit entry explaining Alice's decision."""
        return {"id": self.store.log_decision(summary, rationale, key=key)}
