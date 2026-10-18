"""Alice's §4.2 operations, independent of the transport that carries them.

The stdio MCP server (`agent_hub.mcp`) and the `/rpc` route (`agent_hub.rpc`)
both call these methods. Their names, signatures and docstrings, which are the
tools' schemas, live in `agent_hub_common.orchestrator_tools` (#47).
"""

from contextvars import ContextVar
from dataclasses import asdict, dataclass
from time import monotonic
from typing import Any, Literal

from agent_hub_common import TaskState, WorkflowPolicy, WorkflowStatus
from agent_hub_common.orchestrator_tools import (
    Lease,
    OrchestratorTools,
    PositiveId,
    Sha,
    Timeout,
)

from .merge_gate import ForgeGate, MergeGate
from .rpc_errors import error_code
from .store import HubStore


@dataclass(frozen=True, slots=True)
class Caller:
    """Who an operation runs for, as recorded on the rows it writes (#128)."""

    actor: str
    session: str | None


# The operator acts through the operator channel, never in a session.
OPERATOR = Caller("operator", None)

# Set by the transport around each call. A context variable rather than an
# argument, because each method's signature is its tool's schema.
CALLER: ContextVar[Caller | None] = ContextVar("robomate_caller", default=None)


class OrchestratorOps(OrchestratorTools):
    """The orchestrator operations over one store and merge gate."""

    def __init__(
        self, store: HubStore, gate: ForgeGate | None = None, *, caller: Caller | None = None
    ) -> None:
        self.store = store
        self.gate: ForgeGate = gate if gate is not None else MergeGate()
        # Used only when the transport sets no CALLER: the in-process server,
        # which has a single caller.
        self.caller = caller

    def _caller(self) -> Caller:
        caller = CALLER.get() or self.caller
        if caller is None:
            raise RuntimeError("no caller to attribute this operation to")
        return caller

    async def get_state(self) -> dict[str, Any]:
        return self.store.get_state()

    async def get_statement(self, sha256: str) -> dict[str, Any]:
        return self.store.get_statement(sha256)

    async def initialize_workflow(
        self, goal: str, policy: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        workflow_id = self.store.initialize_workflow(goal, policy)
        state = self.store.get_state()
        workflow = state["workflow"]
        if workflow is None:
            # The call queued behind a live run; report the queued run.
            queued = [q for q in state["queue"] if q["workflow_id"] == workflow_id]
            if queued:
                validated = WorkflowPolicy.model_validate(dict(policy or {})).model_dump(
                    mode="json", exclude_unset=True
                )
                return {
                    "id": workflow_id,
                    "run_id": queued[0]["run_id"],
                    "goal": queued[0]["goal"],
                    "policy": validated,
                    "status": "queued",
                }
            raise RuntimeError("initialize_workflow returned an unknown workflow")
        return {
            "id": workflow_id,
            "run_id": workflow.get("run_id"),
            "goal": workflow["goal"],
            "policy": workflow["policy"],
        }

    async def start_workflow(
        self, statement: str | None = None, policy: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        workflow_id = self.store.start_workflow(statement, policy)
        validated = WorkflowPolicy.model_validate(dict(policy or {})).model_dump(
            mode="json", exclude_unset=True
        )
        state = self.store.get_state()
        workflow = state["workflow"]
        if workflow is not None and workflow["id"] == workflow_id:
            return {
                "id": workflow_id,
                "run_id": workflow.get("run_id"),
                "goal": workflow["goal"],
                "policy": workflow["policy"],
                "status": workflow["status"],
            }
        queued = [q for q in state["queue"] if q["workflow_id"] == workflow_id]
        if queued:
            return {
                "id": workflow_id,
                "run_id": queued[0]["run_id"],
                "goal": queued[0]["goal"],
                "policy": validated,
                "status": "queued",
            }
        previous = state.get("previous_run") or {}
        if previous.get("workflow_id") == workflow_id:
            return {
                "id": workflow_id,
                "run_id": previous.get("run_id"),
                "goal": previous.get("goal"),
                "policy": validated,
                "status": previous.get("status"),
            }
        raise RuntimeError("start_workflow returned an unknown workflow")

    async def wait_for_event(
        self, timeout_s: Timeout = 100, ack: str | None = None
    ) -> dict[str, Any]:
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
        self.store.refuse_while_escalated("check_merge_gate")
        started = monotonic()
        try:
            report = await self.gate.check(pr_url, expected_head_sha)
        except Exception as exc:
            self.store.record_gate_reading(
                pr_url,
                expected_head_sha,
                error_code=error_code(exc),
                elapsed_s=round(monotonic() - started, 3),
            )
            raise
        self.store.record_gate_reading(
            pr_url,
            expected_head_sha,
            report=report,
            elapsed_s=report.elapsed_s,
        )
        return asdict(report)

    async def reply(self, task_id: str, text: str, message_id: int) -> dict[str, bool]:
        applied = self.store.reply(task_id, text, message_id=message_id)
        return {"ok": True, "applied": applied}

    async def set_task_state(
        self, task_id: str, state: Literal["canceled", "failed"], note: str
    ) -> dict[str, Any]:
        return asdict(self.store.set_task_state(task_id, TaskState(state), note))

    async def release_agent(self, agent: str) -> dict[str, Any]:
        return asdict(self.store.release_agent(agent))

    async def set_workflow_status(self, status: WorkflowStatus, summary: str) -> dict[str, bool]:
        caller = self._caller()
        self.store.set_workflow_status(status, summary, actor=caller.actor, session=caller.session)
        return {"ok": True}

    async def log_decision(
        self, summary: str, rationale: str, key: str | None = None
    ) -> dict[str, int]:
        caller = self._caller()
        return {
            "id": self.store.log_decision(
                summary, rationale, key=key, actor=caller.actor, session=caller.session
            )
        }

    async def ask_user(
        self,
        question: str,
        options: list[str] | None = None,
        waiting_on_task_id: str | None = None,
    ) -> dict[str, int]:
        caller = self._caller()
        question_id = self.store.ask_user(
            question,
            options,
            actor=caller.actor,
            session=caller.session,
            waiting_on_task_id=waiting_on_task_id,
        )
        return {"question_id": question_id}

    async def withdraw_question(
        self,
        question_id: PositiveId,
        reason: str,
        event_id: PositiveId,
    ) -> dict[str, Any]:
        caller = self._caller()
        return self.store.withdraw_question(
            question_id, reason, event_id, actor=caller.actor, session=caller.session
        )
