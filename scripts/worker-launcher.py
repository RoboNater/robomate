#!/usr/bin/env python3
"""Resume a headless worker on any supported harness until release (#115, #158, #165).

Uses agent_launcher.py for the same harness commands, conversation-ID capture,
child supervision and bounded backoff as Alice. Before each launch, bearer-only
hub status is checked: cleanly detached predecessors can be replaced at once;
attached predecessors must become lost first (which fails their task). An
orphan bridge that continues heartbeating stops recovery after 300 seconds,
rather than launching a duplicate. The operator stops that bridge and resumes
by hand. No hub mutation or ownership takeover is performed by this launcher.

Each launch and exit is recorded in <name>-sessions.jsonl. --resume-session
and --resume-prompt give hand resumes the same supervision and a fresh budget.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
import agent_launcher as common  # noqa: E402

CONTINUE_PROMPT = (
    "Your previous harness process exited before Alice released you. The launcher "
    "resumed this same conversation. This fixed continuation carries no operator "
    "decision, approval, or state. Your hub tools run in a new worker-mcp process: "
    "call check_in once. Reconcile your assignment with the hub: if you still hold "
    "a task, finish it and submit its result; if the task failed or was canceled, "
    "drop it and return to await_assignment. Every reply must contain a tool call "
    "until await_assignment returns release: true; an announcement of a step must "
    "come with its tool call. Wait for CI and other work in the foreground. Do not "
    "end your turn until await_assignment returns release: true."
)


def telemetry_size(path: Path) -> int:
    try:
        return path.stat().st_size
    except FileNotFoundError:
        return 0


def released(path: Path, offset: int) -> bool:
    """Ignore releases from earlier runs and malformed/incomplete telemetry lines."""
    try:
        with path.open("rb") as stream:
            stream.seek(offset)
            data = stream.read()
    except FileNotFoundError:
        return False
    for line in data.decode("utf-8", errors="replace").splitlines():
        try:
            record = json.loads(line)
        except ValueError:
            continue
        if (
            isinstance(record, dict)
            and record.get("event") == "tool_call"
            and record.get("phase") == "success"
            and record.get("tool") == "await_assignment"
            and record.get("outcome") == "release"
        ):
            return True
    return False


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
    continue_prompt = CONTINUE_PROMPT

    def __init__(self, args: argparse.Namespace, reader: common.HubReader | None = None) -> None:
        super().__init__(args, reader or WorkerReader(args.hub_url, args.token_file, args.agent))
        self.agent = args.agent
        self.telemetry_offset = telemetry_size(args.telemetry)

    def finished(self, workflow: dict[str, Any] | None, resumes: int) -> bool:
        agent = workflow.get("agent") if workflow else None
        reason = None
        if released(self.args.telemetry, self.telemetry_offset) or (
            agent is not None and agent["status"] == "released"
        ):
            reason = "released"
        elif workflow is not None and workflow["status"] == "done":
            reason = "done"
        if reason:
            self.log("stop", reason=reason, resumes=resumes)
            return True
        return False

    def manual_resume_hint(self) -> str:
        return (
            "Take a before-snapshot with `robomate status --snapshot` in the target "
            f"repository, fill in resume-{self.agent}.prompt.md, then resume by hand "
            f"with resume-{self.agent} {self.conversation or '(find the exact conversation ID)'}."
        )

    def resumable(self, exit_code: int, resumes: int) -> str:
        # A release in local telemetry remains decisive when the hub is unavailable.
        if released(self.args.telemetry, self.telemetry_offset):
            self.finished(None, resumes)
            return "done"
        workflow = self.read_hub()
        if self.finished(workflow, resumes):
            return "done"
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
        if workflow["status"] == "paused":
            raise common.Stop(
                common.EXIT_PAUSED,
                "paused",
                "workflow paused; wait until the operator resumes it. " + self.manual_resume_hint(),
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
            # Workers may check in before Alice initializes a new workflow.
            # A resume still needs the existing workflow, checked by resumable().
            if workflow is None and (resumes or self.args.resume_session is not None):
                raise common.Stop(
                    common.EXIT_CANNOT_RESUME,
                    "no_workflow",
                    "no workflow to resume. " + self.manual_resume_hint(),
                )
            if workflow is not None and workflow["status"] == "paused":
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


def parse_args(argv: list[str] | None) -> argparse.Namespace:
    return common.parse_args(argv, worker=True)


def main(argv: list[str] | None = None, reader: common.HubReader | None = None) -> int:
    return common.main(argv, reader, launcher_type=WorkerLauncher, worker=True)


if __name__ == "__main__":
    sys.exit(main())
