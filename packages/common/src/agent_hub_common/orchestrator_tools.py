"""Alice's §4.2 tool signatures, shared by the hub and the stdio bridge (#47).

Each method's name, signature and docstring are its tool's name, argument
schema and description, so edits here change what Alice sees and what her
tool list costs (tests/fixtures/orchestrator-tools.json). The hub implements
them in `agent_hub.orchestrator.OrchestratorOps`; worker-mcp forwards them to
the hub's `/rpc` route without importing the hub server.
"""

import inspect
from typing import Annotated, Any, Literal

from pydantic import Field

from .models import WorkflowStatus

Timeout = Annotated[float, Field(ge=0, le=120, allow_inf_nan=False)]
Lease = Annotated[float, Field(gt=0, le=525600, allow_inf_nan=False)]
Sha = Annotated[str, Field(pattern=r"^[0-9a-fA-F]{40}$")]
PositiveId = Annotated[int, Field(gt=0)]

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
    "ask_user",
    "withdraw_question",
)


class OrchestratorTools:
    """The orchestrator tools' contract; a subclass supplies the behaviour.

    A subclass overrides each operation with exactly this signature and
    inherits its docstring, so the description has one source.
    """

    def __init_subclass__(cls, **kwargs: Any) -> None:
        super().__init_subclass__(**kwargs)
        for name in OPERATIONS:
            method = cls.__dict__.get(name)
            if method is None:
                continue
            tool = getattr(OrchestratorTools, name)
            if inspect.signature(method) != inspect.signature(tool):
                raise TypeError(f"{cls.__qualname__}.{name} does not match its tool signature")
            method.__doc__ = tool.__doc__

    async def get_state(self) -> dict[str, Any]:
        """Read the workflow, agents and compact task summaries."""
        raise NotImplementedError

    async def initialize_workflow(
        self, goal: str, policy: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        """Persist the initial prompt's goal and policy, or confirm them on restart.

        This must be Alice's first mutating call. Goal and policy are immutable;
        use get_state to resume the stored workflow after a restart.
        """
        raise NotImplementedError

    async def wait_for_event(
        self, timeout_s: Timeout = 100, ack: str | None = None
    ) -> dict[str, Any]:
        """Wait for and lease the oldest event. On event=null, call again.

        ack: the delivery_id of the event just handled. It still acks after
        the delivery lease has expired, so a long action — merging, say — is
        not redone once it has finished.
        """
        raise NotImplementedError

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

        Refused while the workflow is escalated.

        event_id: the durable event whose handling causes this assignment.
        role: implementer, reviewer or rebase — the guide the worker fetches.
        pr_head_sha: the PR head a review or rebase is bound to.
        """
        raise NotImplementedError

    async def check_merge_gate(self, pr_url: str, expected_head_sha: Sha) -> dict[str, Any]:
        """Read the PR's head, CI, base freshness and mergeability before merging.

        expected_head_sha: the approved head — the reviewer's reviewed_head_sha,
        or the head_sha of a rebase that reported no conflict_files.
        Waits up to 60 s while CI or mergeability is still settling. Call it
        immediately before merging; earlier results are advisory. Refused while
        the workflow is escalated.
        """
        raise NotImplementedError

    async def reply(self, task_id: str, text: str, message_id: int) -> dict[str, bool]:
        """Answer the named worker question and return its task to working."""
        raise NotImplementedError

    async def set_task_state(
        self, task_id: str, state: Literal["canceled", "failed"], note: str
    ) -> dict[str, Any]:
        """Cancel or fail an open task, retaining the note and freeing its worker."""
        raise NotImplementedError

    async def release_agent(self, agent: str) -> dict[str, Any]:
        """Release a worker; its next NEXT returns the release marker."""
        raise NotImplementedError

    async def set_workflow_status(self, status: WorkflowStatus, summary: str) -> dict[str, bool]:
        """Set active/paused/done and save the summary in the audit log.

        Escalate with ask_user. Leaving escalated is refused while any
        operator question is open.
        """
        raise NotImplementedError

    async def log_decision(
        self, summary: str, rationale: str, key: str | None = None
    ) -> dict[str, int]:
        """Append a durable audit entry explaining Alice's decision."""
        raise NotImplementedError

    async def ask_user(
        self,
        question: str,
        options: list[str] | None = None,
        waiting_on_task_id: str | None = None,
    ) -> dict[str, int]:
        """Ask the operator a question and set the workflow to escalated.

        The answer arrives as a user_answered event naming the question_id.
        Ask again while escalated to add another question.
        Bind waiting_on_task_id only for a wait/cancel/reassign question about
        that unfinished task, never for restricted-action authorization.
        """
        raise NotImplementedError

    async def withdraw_question(
        self,
        question_id: PositiveId,
        reason: str,
        event_id: PositiveId,
    ) -> dict[str, Any]:
        """Withdraw your session's task-wait question after the task completes.

        Cite its later task_completed event and explain why no decision remains.
        Never withdraw a question still needing operator authority. This gives
        no operator answer; resume explicitly when no questions remain open.
        """
        raise NotImplementedError
