"""Worker supervision for the joined launcher (#165, #247).

Standard library only. Before each launch, bearer-only hub status is checked:
cleanly detached predecessors can be replaced at once; attached predecessors
must become lost first (which fails their task). No hub mutation or ownership
takeover is performed here.
"""

from __future__ import annotations

import argparse
import time
from pathlib import Path
from typing import Any

import launcher_common as common


class WorkerReader(common.HubReader):
    def __init__(self, url: str, token_file: Path, agent: str) -> None:
        super().__init__(url, token_file)
        self.agent = agent

    def call(self, method: str) -> dict[str, Any]:
        result = super().call(method)
        if method == "hub.status":
            agents = result.get("agents")
            if not isinstance(agents, list):
                raise ValueError("hub.status has no agents list")
            matches = [a for a in agents if isinstance(a, dict) and a.get("name") == self.agent]
            if len(matches) > 1:
                raise ValueError("hub.status has duplicate agent rows")
            agent = matches[0] if matches else None
            if agent is not None and (
                agent.get("status") not in ("idle", "busy", "lost", "released")
                or not isinstance(agent.get("alive"), bool)
                or not isinstance(agent.get("activity"), dict)
                or "instance" not in agent["activity"]
            ):
                raise ValueError("hub.status has a malformed agent")
            self.current_agent = agent
        return result

    def read(self) -> tuple[str, dict[str, Any] | None]:
        hub, workflow = super().read()
        return hub, None if workflow is None else workflow | {"agent": self.current_agent}


class WorkerLauncher(common.Launcher):
    def __init__(self, args: argparse.Namespace, reader: common.HubReader | None = None) -> None:
        super().__init__(args, reader or WorkerReader(args.hub_url, args.token_file, args.agent))
        self.agent = args.agent
        self.ever_joined = False

    def released_locally(self) -> bool:
        raise NotImplementedError

    def finished(self, workflow: dict[str, Any] | None, resumes: int) -> bool:
        agent = workflow.get("agent") if workflow else None
        reason = None
        if self.released_locally() or (agent is not None and agent["status"] == "released"):
            reason = "released"
        elif workflow is not None and workflow["status"] == "done":
            reason = "done"
        if reason:
            self.log("stop", reason=reason, resumes=resumes)
            self.announce_stop(reason, workflow)
            return True
        return False

    def manual_resume_hint(self) -> str:
        raise NotImplementedError

    def own_join_evidence(self, agent: dict[str, Any] | None) -> bool:
        raise NotImplementedError

    def resumable(self, exit_code: int, resumes: int) -> str:
        # A release in local telemetry remains decisive when the hub is unavailable.
        if self.released_locally():
            self.finished(None, resumes)
            return "done"
        workflow = self.read_hub()
        if self.finished(workflow, resumes):
            return "done"
        if workflow is not None and workflow["status"] == "paused":
            raise common.Stop(
                common.EXIT_PAUSED,
                "paused",
                "workflow paused; wait until the operator resumes it. " + self.manual_resume_hint(),
            )
        agent = workflow.get("agent") if workflow else getattr(self.reader, "current_agent", None)
        self.ever_joined |= self.own_join_evidence(agent)
        if not self.ever_joined:
            raise common.Stop(
                common.EXIT_NEVER_JOINED,
                "never_joined",
                f"{self.agent} never joined the hub; inspect the harness log "
                f"and {self.args.sessions}.",
            )
        # Reuse the common stop checks without doing a second status read.
        if workflow is None:
            raise common.Stop(
                common.EXIT_CANNOT_RESUME,
                "no_workflow",
                "no workflow to resume. " + self.manual_resume_hint(),
            )
        if self.conversation is None:
            raise common.Stop(
                common.EXIT_CANNOT_RESUME,
                "no_conversation",
                "no exact conversation ID to resume. " + self.manual_resume_hint(),
            )
        if self.args.max_resumes is not None and resumes >= self.args.max_resumes:
            raise common.Stop(
                common.EXIT_RESUMES_SPENT,
                "resumes_spent",
                "automatic resumes spent. " + self.manual_resume_hint(),
            )
        return "resume"

    def before_launch(self, resumes: int) -> bool:
        deadline = time.monotonic() + self.args.predecessor_wait_s
        waiting = False
        while True:
            workflow = self.read_hub()
            if self.finished(workflow, resumes):
                return False
            # Workers may join before Alice starts a new workflow.
            # A resume still needs the existing workflow, checked by resumable().
            if workflow is None and (resumes or self.args.resume_session is not None):
                raise common.Stop(
                    common.EXIT_CANNOT_RESUME,
                    "no_workflow",
                    "no workflow to resume. " + self.manual_resume_hint(),
                )
            if (
                workflow is not None
                and workflow["status"] == "paused"
                and not (resumes == 0 and self.args.recover and self.args.force)
            ):
                raise common.Stop(
                    common.EXIT_PAUSED,
                    "paused",
                    "workflow paused; wait until the operator resumes it. "
                    + self.manual_resume_hint(),
                )
            agent = (
                workflow.get("agent")
                if workflow
                else (self.reader.current_agent if isinstance(self.reader, WorkerReader) else None)
            )
            self.ever_joined |= self.own_join_evidence(agent)
            if self.resume_deadline is not None and time.monotonic() >= self.resume_deadline:
                raise common.Stop(
                    common.EXIT_RESUMES_SPENT,
                    "time_budget_spent",
                    "resume time budget spent. " + self.manual_resume_hint(),
                )
            if agent is None or not agent["alive"] or agent["activity"]["instance"] is None:
                return True
            if not waiting:
                self.log("predecessor_wait", resumes=resumes)
                waiting = True
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise common.Stop(
                    common.EXIT_CANNOT_RESUME,
                    "predecessor_alive",
                    f"{self.agent}'s predecessor is still attached; operator must stop its "
                    "orphan bridge. " + self.manual_resume_hint(),
                )
            if self.resume_deadline is not None:
                remaining = min(remaining, self.resume_deadline - time.monotonic())
            self.sleep(min(self.args.predecessor_poll_s, max(0.0, remaining)))
