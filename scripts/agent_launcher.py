"""Shared headless launcher mechanics for Alice and workers (#165).

Standard library only: exact per-harness start/resume and conversation IDs,
pinned hub reads, child supervision, session logs and #158 backoff.
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, TextIO

DEFAULT_MAX_RESUMES: int | None = None
DEFAULT_RESUME_DELAY_S = 5.0
DEFAULT_RESUME_MAX_DELAY_S = 30 * 60.0
DEFAULT_RESUME_TOTAL_S = 12 * 60 * 60.0
DEFAULT_RESUME_SERIES_RESET_S = 5 * 60.0
DEFAULT_READ_RETRIES = 5
# Workers tolerate a hub restart without exhausting the short Alice read window (#165 r1-1).
DEFAULT_WORKER_READ_RETRIES = 60
DEFAULT_READ_RETRY_DELAY_S = 2.0
HUB_TIMEOUT_S = 10.0
# How long, after the harness exits, its remaining output may take to arrive.
RELAY_GRACE_S = 0.5
# How long a terminated harness child has to exit before it is killed.
CHILD_KILL_AFTER_S = 10.0

EXIT_DONE = 0
EXIT_RESUMES_SPENT = 1
EXIT_HUB_UNREADABLE = 3
EXIT_CANNOT_RESUME = 4
EXIT_PAUSED = 5
EXIT_INTERRUPTED = 130

HARNESSES = ("claude-code", "codex", "opencode", "antigravity")

# The one prompt every automatic resume sends (#146). Fixed text: it names no
# state, snapshot, operator decision or approval, so it cannot be mistaken for
# one (#131).
CONTINUE_PROMPT = (
    "Your previous harness process exited before the workflow was done, and the "
    "launcher has resumed this same conversation. This message is a fixed "
    "continuation: it carries no operator decision, approval, or state. Call "
    "get_state first, then reconcile as the alice-orchestrator skill's On resume "
    "section describes. Keep an escalated or paused workflow as it is: escalation "
    "ends only through the operator's user_answered event. Do not initialize the "
    "workflow again and do not repeat completed work; continue the existing "
    "assignment. Do not end your turn until the workflow is done."
)

_CODEX_ID = re.compile(r"session id:\s*([0-9a-fA-F][0-9a-fA-F-]{35})\b")
_CODEX_THREAD = re.compile(r'"thread_id"\s*:\s*"([0-9a-fA-F-]{36})"')
_OPENCODE_ID = re.compile(r'"sessionI[Dd]"\s*:\s*"(ses_[A-Za-z0-9]+)"')
_AGY_ID = re.compile(
    r'"(?:conversation_?[iI][dD]|conversationID|session_?[iI][dD]|sessionID)"\s*:\s*"([^"\s]{6,128})"'
)


class Stop(Exception):
    """End the launcher with an exit code and a reason for the operator."""

    def __init__(self, code: int, reason: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.reason = reason


@dataclass(frozen=True, slots=True)
class Launch:
    argv: list[str]
    stdin: str | None


def instruction(path: Path) -> str:
    """A one-line pointer to a prompt file, safe through a Windows npm .cmd shim."""

    return f"Read {path} and follow the instructions in it"


def start_launch(harness: str, base: list[str], prompt: Path, session_id: str) -> Launch:
    """The first launch, with the kickoff prompt."""

    if harness == "claude-code":
        return Launch([*base, "--session-id", session_id, "-p", instruction(prompt)], None)
    if harness == "codex":
        return Launch([*base, "-"], prompt.read_text(encoding="utf-8"))
    if harness == "opencode":
        return Launch([*base, "--format", "json", instruction(prompt)], None)
    return Launch([*base, "--output-format", "stream-json", "-p", instruction(prompt)], None)


def resume_launch(harness: str, base: list[str], conversation: str, text: str) -> Launch:
    """A resume of ``conversation``; ``text`` is a one-line instruction or the continuation."""

    if harness == "claude-code":
        return Launch([*base, "--resume", conversation, "-p", text], None)
    if harness == "codex":
        return Launch([*base, "resume", conversation, "-"], text)
    if harness == "opencode":
        return Launch([*base, "--format", "json", "--session", conversation, text], None)
    return Launch(
        [*base, "--conversation", conversation, "--output-format", "stream-json", "-p", text],
        None,
    )


def conversation_in(harness: str, line: str) -> str | None:
    """The conversation ID a harness printed on ``line``, if any."""

    if harness == "codex":
        match = _CODEX_ID.search(line) or _CODEX_THREAD.search(line)
    elif harness == "opencode":
        match = _OPENCODE_ID.search(line)
    elif harness == "antigravity":
        match = _AGY_ID.search(line)
    else:
        return None
    return match.group(1) if match else None


def log_session(path: Path, **fields: object) -> None:
    record = {"timestamp": datetime.now(UTC).isoformat(timespec="seconds"), **fields}
    with path.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(record, sort_keys=True) + "\n")


def say(message: str) -> None:
    print(f"agent-launcher: {message}", file=sys.stderr, flush=True)


def backoff_delay(initial_s: float, cap_s: float, failures_in_series: int) -> float:
    """The wait before the next resume: doubling from ``initial_s`` to ``cap_s`` (#158).

    Shared by Alice and all worker harnesses; standard library only.
    """
    if not initial_s > 0:
        return 0.0
    if failures_in_series >= 30:
        return cap_s
    return min(initial_s * (2.0**failures_in_series), cap_s)


def retry_at_iso(delay_s: float) -> str:
    return (datetime.now(UTC) + timedelta(seconds=max(0.0, delay_s))).isoformat(timespec="seconds")


# -- hub reads ------------------------------------------------------------------


class HubReader:
    """Bearer-only reads of the run hub; never an orchestrator session."""

    def __init__(self, url: str, token_file: Path) -> None:
        self.url = url.rstrip("/")
        self.token_file = token_file

    def call(self, method: str) -> dict[str, Any]:
        token = self.token_file.read_text(encoding="utf-8").strip()
        request = urllib.request.Request(
            f"{self.url}/rpc",
            data=json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": {}}).encode(),
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        )
        with urllib.request.urlopen(request, timeout=HUB_TIMEOUT_S) as response:
            body = json.load(response)
        if not isinstance(body, dict) or not isinstance(body.get("result"), dict):
            raise ValueError(f"malformed {method} response")
        result: dict[str, Any] = body["result"]
        return result

    def read(self) -> tuple[str, dict[str, Any] | None]:
        """The hub ID and the workflow (``id``, ``status``), or None before initialization."""

        info = self.call("hub.info")
        hub_id = info.get("hub_id")
        if not isinstance(hub_id, str) or not hub_id:
            raise ValueError("hub.info names no hub_id")
        status = self.call("hub.status")
        workflow = status.get("workflow")
        if workflow is None:
            return hub_id, None
        if (
            not isinstance(workflow, dict)
            or not isinstance(workflow.get("id"), str)
            or workflow.get("status") not in ("active", "paused", "escalated", "done")
        ):
            raise ValueError("hub.status has a malformed workflow")
        return hub_id, {"id": workflow["id"], "status": workflow["status"]}


class Launcher:
    agent = "alice"
    continue_prompt = CONTINUE_PROMPT

    def __init__(self, args: argparse.Namespace, reader: HubReader | None = None) -> None:
        self.args = args
        self.reader = reader or HubReader(args.hub_url, args.token_file)
        self.harness: str = args.harness
        self.base = [shutil.which(args.command[0]) or args.command[0], *args.command[1:]]
        self.hub_id: str | None = None
        self.workflow_id: str | None = None
        self.conversation: str | None = None
        self.child: subprocess.Popen[str] | None = None
        self.child_exit_at: str | None = None
        self.interrupted = False
        self.resume_deadline: float | None = None

    def log(self, event: str, **fields: object) -> None:
        log_session(
            self.args.sessions,
            agent=self.agent,
            harness=self.harness,
            event=event,
            conversation_id=self.conversation,
            hub_id=self.hub_id,
            workflow_id=self.workflow_id,
            **fields,
        )

    # -- reading the hub ----------------------------------------------------------

    def read_hub(self) -> dict[str, Any] | None:
        """Read the pinned hub and workflow, retrying boundedly; stop on failure."""

        problem = ""
        for attempt in range(1, self.args.read_retries + 1):
            if self.interrupted:
                raise Stop(EXIT_INTERRUPTED, "interrupted", "interrupted")
            try:
                hub_id, workflow = self.reader.read()
            except (OSError, urllib.error.URLError, ValueError) as exc:
                problem = f"hub unreadable: {exc}"
            else:
                if self.hub_id is not None and hub_id != self.hub_id:
                    problem = f"hub {hub_id} is not the pinned hub {self.hub_id}"
                elif (
                    self.workflow_id is not None
                    and workflow is not None
                    and workflow["id"] != self.workflow_id
                ):
                    problem = (
                        f"workflow {workflow['id']} is not the pinned workflow {self.workflow_id}"
                    )
                elif self.workflow_id is not None and workflow is None:
                    problem = f"the pinned workflow {self.workflow_id} has disappeared"
                else:
                    self.hub_id = hub_id
                    if workflow is not None:
                        self.workflow_id = workflow["id"]
                    return workflow
            say(f"read {attempt}/{self.args.read_retries} failed: {problem}")
            if attempt < self.args.read_retries:
                self.sleep(self.args.read_retry_delay_s)
        raise Stop(
            EXIT_HUB_UNREADABLE,
            "hub_unreadable",
            f"{problem}. Not guessing the workflow's state and not launching again. Check "
            f"`robomate status` in the target repository, then resume by hand with "
            f"resume-{self.agent} and conversation {self.conversation or '(unknown)'}.",
        )

    def sleep(self, seconds: float) -> None:
        deadline = time.monotonic() + seconds
        while not self.interrupted and time.monotonic() < deadline:
            time.sleep(min(0.1, max(0.0, deadline - time.monotonic())))
        if self.interrupted:
            raise Stop(EXIT_INTERRUPTED, "interrupted", "interrupted")

    # -- running the harness ------------------------------------------------------

    def run_child(self, launch: Launch, action: str, resumes: int) -> int:
        """Run one harness process, relaying its output and watching for its conversation ID.

        The child's PID is logged with the launch, so an operator can signal
        that one process (#144's stall check, #146's exit check) and no other.
        """

        if self.interrupted:
            raise Stop(EXIT_INTERRUPTED, "interrupted", "interrupted")

        self.child = subprocess.Popen(
            launch.argv,
            stdin=subprocess.PIPE if launch.stdin is not None else None,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
        )
        if self.interrupted:
            # A signal that landed just before this launch.
            self.stop_child(wait=True)
        self.log(action, resumes=resumes, pid=self.child.pid)
        say(
            f"{action} ({self.harness}) pid {self.child.pid},"
            f" conversation {self.conversation or 'pending'}"
        )
        if launch.stdin is not None:
            threading.Thread(
                target=_feed, args=(self.child.stdin, launch.stdin), daemon=True
            ).start()
        # The output is relayed on its own thread and the child's exit is
        # awaited here, so a descendant that inherited the pipe (a bridge, a
        # shell) cannot hold recovery up after the harness itself exits.
        relay = threading.Thread(target=self.relay, args=(self.child.stdout,), daemon=True)
        relay.start()
        code = self.child.wait()
        self.child_exit_at = datetime.now(UTC).isoformat(timespec="seconds")
        relay.join(RELAY_GRACE_S)
        self.child = None
        return code

    def relay(self, stream: TextIO | None) -> None:
        """Copy the harness's output through, noting its conversation ID once."""

        if stream is None:
            return
        for line in stream:
            sys.stdout.write(line)
            sys.stdout.flush()
            if self.conversation is None:
                found = conversation_in(self.harness, line)
                if found is not None:
                    self.conversation = found
                    self.log("conversation")
                    say(f"conversation {found}")

    def stop_child(self, *, wait: bool) -> None:
        """Terminate this launcher's own harness child, killing it after 10 s.

        From a signal handler `wait` is False: the main thread may be inside
        that child's `wait()` already, which a second `wait()` would deadlock;
        it returns once the child exits, and a timer kills a child that
        ignores the terminate.
        """

        child = self.child
        if child is None or child.poll() is not None:
            return
        child.terminate()
        if not wait:
            timer = threading.Timer(CHILD_KILL_AFTER_S, _kill_if_running, args=(child,))
            timer.daemon = True
            timer.start()
            return
        try:
            child.wait(timeout=CHILD_KILL_AFTER_S)
        except subprocess.TimeoutExpired:
            child.kill()
            child.wait()

    def interrupt(self, *_: object) -> None:
        """SIGTERM: stop the child and launch nothing more."""

        self.interrupted = True
        self.stop_child(wait=False)

    def resumable(self, exit_code: int, resumes: int) -> str:
        """Read the pinned hub and return "done" or "resume"; stop for anything else."""

        workflow = self.read_hub()
        if workflow is None:
            raise Stop(
                EXIT_CANNOT_RESUME,
                "no_workflow",
                f"the harness exited (code {exit_code}) before initializing the workflow. "
                "Not replaying the kickoff prompt. Inspect the harness output, then rerun "
                "start-alice if nothing was done, or resume by hand.",
            )
        status = workflow["status"]
        if status == "done":
            self.log("stop", reason="done", workflow_status=status, resumes=resumes)
            say(f"workflow done after {resumes} automatic resume(s)")
            return "done"
        if self.conversation is None:
            raise Stop(
                EXIT_CANNOT_RESUME,
                "no_conversation",
                f"the harness exited (code {exit_code}) without printing its conversation "
                "ID, so there is nothing exact to resume. Find it in the harness's session "
                "list and resume by hand with resume-alice.",
            )
        if status == "paused":
            raise Stop(
                EXIT_PAUSED,
                "paused",
                f"the workflow is paused; not restarting Alice while it is. Resume by hand "
                f"with resume-{self.agent} {self.conversation} when the operator is ready.",
            )
        if self.args.max_resumes is not None and resumes >= self.args.max_resumes:
            raise Stop(
                EXIT_RESUMES_SPENT,
                "resumes_spent",
                f"the harness exited (code {exit_code}) with the workflow {status} and the "
                f"{self.args.max_resumes} automatic resume(s) are spent. Run "
                f"resume-{self.agent} with no arguments to capture the snapshot and "
                "prepare recovery automatically; follow its prerequisite recommendations. "
                "(docs/development/agent-recovery.md).",
            )
        return "resume"

    def before_launch(self, resumes: int) -> bool:
        """Return False if a worker was released while waiting for its predecessor."""
        return True

    def run(self) -> int:
        if self.args.recover:
            from agent_recovery import prepare

            self.conversation, _ = prepare(self.args, self.reader, self.agent)
            self.args.resume_session = self.conversation
            # Saved pins survive a new launcher process, including its first read.
            from agent_recovery import pin, records

            saved = records(self.args.sessions, self.agent, self.harness)
            saved = [r for r in saved if r.get("conversation_id") in (None, self.conversation)]
            self.hub_id = pin(saved, "hub_id", self.args.hub_id)
            self.workflow_id = pin(saved, "workflow_id", self.args.workflow_id)
        manual = self.args.resume_session is not None
        workflow = self.read_hub()
        if manual:
            if workflow is None:
                raise Stop(
                    EXIT_CANNOT_RESUME,
                    "no_workflow",
                    "the hub has no workflow, so there is nothing to resume; run start-alice "
                    "for a new workflow",
                )
            if workflow["status"] == "done":
                self.log("stop", reason="done", workflow_status="done", resumes=0)
                say("the workflow is already done; nothing to resume")
                return EXIT_DONE
            if self.args.recover and workflow["status"] == "paused" and not self.args.force:
                raise Stop(
                    EXIT_PAUSED,
                    "paused",
                    "Workflow paused during recovery. Have the "
                    "operator resume it, or retry with --force to reconcile "
                    "while preserving pause.",
                )
            self.conversation = self.args.resume_session.strip()
            # The operator's manual resume prompt, once; Codex reads it on stdin.
            text = (
                self.args.resume_prompt.read_text(encoding="utf-8")
                if self.harness == "codex"
                else instruction(self.args.resume_prompt)
            )
            launch = resume_launch(self.harness, self.base, self.conversation, text)
            action = "manual_resume"
        else:
            if self.harness == "claude-code":
                self.conversation = str(uuid.uuid4())
            launch = start_launch(
                self.harness, self.base, self.args.prompt, self.conversation or ""
            )
            action = "start"
        resumes = 0
        failures_in_series = 0
        # The budget anchor: the first exit of the current series. Healthy
        # running time before it never spends the budget, and a series reset
        # refills it (r1-1: anchoring at launcher start stopped long-lived
        # launchers cold).
        series_start: float | None = None
        while True:
            if not self.before_launch(resumes):
                return EXIT_DONE
            run_start = time.monotonic()
            exit_code = self.run_child(launch, action, resumes)
            run_duration = time.monotonic() - run_start
            self.log("exit", resumes=resumes, exit_code=exit_code, exited_at=self.child_exit_at)
            if self.interrupted:
                raise Stop(EXIT_INTERRUPTED, "interrupted", "interrupted")
            if self.resumable(exit_code, resumes) == "done":
                return EXIT_DONE
            if run_duration >= self.args.resume_series_reset_s and (failures_in_series or resumes):
                failures_in_series = 0
                series_start = time.monotonic()
                self.log(
                    "series_reset",
                    resumes=resumes,
                    run_duration_s=round(run_duration, 3),
                )
                say(
                    f"ran {run_duration:.0f} s, starting a new backoff series "
                    f"at {self.args.resume_delay_s:g} s"
                )
            delay = backoff_delay(
                self.args.resume_delay_s, self.args.resume_max_delay_s, failures_in_series
            )
            if series_start is None:
                series_start = time.monotonic()
            self.resume_deadline = series_start + self.args.resume_total_s
            elapsed = time.monotonic() - series_start
            if elapsed >= self.args.resume_total_s or elapsed + delay > self.args.resume_total_s:
                self.log(
                    "stop",
                    reason="time_budget_spent",
                    resumes=resumes,
                    elapsed_s=round(elapsed, 3),
                )
                raise Stop(
                    EXIT_RESUMES_SPENT,
                    "time_budget_spent",
                    f"the harness exited (code {exit_code}) and the resume time budget "
                    f"of {self.args.resume_total_s:g} s is spent after {resumes} "
                    f"automatic resume(s). Run resume-{self.agent} with no arguments to "
                    "capture the snapshot and prepare recovery automatically; "
                    "follow its prerequisite recommendations. "
                    "(docs/development/agent-recovery.md).",
                )
            resumes += 1
            failures_in_series += 1
            attempt_at = retry_at_iso(delay)
            self.log(
                "wait",
                resumes=resumes,
                delay_s=delay,
                next_attempt=attempt_at,
            )
            count = (
                f"{resumes}/{self.args.max_resumes}"
                if self.args.max_resumes is not None
                else f"{resumes}"
            )
            say(
                f"harness exited (code {exit_code}); resuming"
                f" ({count}) in {delay:g} s at {attempt_at}"
            )
            # The existing sleep() raises Stop(130) when interrupted, so a
            # long backoff wait stops the launcher promptly on Ctrl-C/SIGTERM.
            self.sleep(delay)
            # The workflow may have been paused or finished during the delay,
            # or the hub replaced: decide again just before launching.
            if self.resumable(exit_code, resumes - 1) == "done":
                return EXIT_DONE
            assert self.conversation is not None
            launch = resume_launch(self.harness, self.base, self.conversation, self.continue_prompt)
            action = "resume"


def _kill_if_running(child: subprocess.Popen[str]) -> None:
    if child.poll() is None:
        child.kill()


def _feed(stream: TextIO | None, text: str) -> None:
    if stream is None:
        return
    try:
        stream.write(text)
        stream.close()
    except (BrokenPipeError, OSError, ValueError):
        pass


def parse_args(argv: list[str] | None, *, worker: bool = False) -> argparse.Namespace:
    from agent_recovery import add_options, split_controls

    argv, controls = split_controls(argv)

    description = (
        "Resume a headless worker on any supported harness until release."
        if worker
        else "Run a headless Alice, resuming the same conversation until her workflow is done."
    )
    parser = argparse.ArgumentParser(description=description)
    add_options(parser)
    if worker:
        parser.add_argument("--agent", required=True)
        parser.add_argument("--telemetry", type=Path, required=True)
        parser.add_argument("--predecessor-wait-s", type=float, default=300.0)
        parser.add_argument("--predecessor-poll-s", type=float, default=2.0)
    parser.add_argument("--harness", required=True, choices=HARNESSES)
    parser.add_argument("--hub-url", required=True, help="the run hub's URL, never discovered")
    parser.add_argument("--token-file", type=Path, required=True, help="the hub bearer token")
    parser.add_argument("--sessions", type=Path, required=True, help="the JSONL launch log")
    parser.add_argument("--prompt", type=Path, help="the kickoff prompt file (first launch)")
    parser.add_argument("--resume-session", help="resume this conversation ID by hand")
    parser.add_argument(
        "--resume-prompt", type=Path, help="with --resume-session: the manual resume prompt"
    )
    parser.add_argument(
        "--max-resumes",
        type=int,
        default=DEFAULT_MAX_RESUMES,
        help="stop after this many automatic resumes (default: no count limit, "
        "--resume-total-s governs; 0 disables automatic resumes)",
    )
    parser.add_argument(
        "--resume-delay-s",
        type=float,
        default=DEFAULT_RESUME_DELAY_S,
        help="first wait between automatic resumes in seconds "
        f"(default {DEFAULT_RESUME_DELAY_S:g}); doubles until --resume-max-delay-s",
    )
    parser.add_argument(
        "--resume-max-delay-s",
        type=float,
        default=DEFAULT_RESUME_MAX_DELAY_S,
        help="cap for the doubling wait in seconds "
        f"(default {DEFAULT_RESUME_MAX_DELAY_S:g}, about 30 minutes)",
    )
    parser.add_argument(
        "--resume-total-s",
        type=float,
        default=DEFAULT_RESUME_TOTAL_S,
        help="stop automatic resumes after this much time in seconds since the first "
        f"exit of the current series (default {DEFAULT_RESUME_TOTAL_S:g}, about 12 hours)",
    )
    parser.add_argument(
        "--resume-series-reset-s",
        type=float,
        default=DEFAULT_RESUME_SERIES_RESET_S,
        help="a run lasting this long starts a new backoff series at the short delay "
        f"(default {DEFAULT_RESUME_SERIES_RESET_S:g})",
    )
    parser.add_argument(
        "--read-retries",
        type=int,
        default=DEFAULT_WORKER_READ_RETRIES if worker else DEFAULT_READ_RETRIES,
        help="bounded hub read attempts (default: 60 for workers, 5 for Alice)",
    )
    parser.add_argument("--read-retry-delay-s", type=float, default=DEFAULT_READ_RETRY_DELAY_S)
    parser.add_argument("command", nargs=argparse.REMAINDER, help="the harness command, after --")
    args = parser.parse_args(argv)
    for key, value in controls.items():
        setattr(args, key, value)
    if args.command[:1] == ["--"]:
        args.command = args.command[1:]
    if not args.command:
        parser.error("the harness command is required after --")
    if args.max_resumes is not None and args.max_resumes < 0:
        parser.error("--max-resumes must not be negative (0 disables automatic resumes)")
    if not args.resume_delay_s >= 0:
        parser.error("--resume-delay-s must not be negative")
    if not args.resume_max_delay_s >= 0:
        parser.error("--resume-max-delay-s must not be negative")
    if not args.resume_total_s >= 0:
        parser.error("--resume-total-s must not be negative")
    if not args.resume_series_reset_s >= 0:
        parser.error("--resume-series-reset-s must not be negative")
    if args.read_retries < 1:
        parser.error("--read-retries must be at least 1")
    if not args.read_retry_delay_s >= 0:
        parser.error("--read-retry-delay-s must not be negative")
    if args.recover and args.resume_prompt is None:
        parser.error("--recover needs --resume-prompt")
    if args.resume_session is not None:
        if not args.resume_session.strip():
            parser.error("--resume-session must name a conversation ID")
        if args.resume_prompt is None:
            parser.error("--resume-session needs --resume-prompt")
    elif args.prompt is None and not args.recover:
        parser.error("--prompt is required unless --resume-session is given")
    if worker:
        if not args.predecessor_wait_s >= 0:
            parser.error("--predecessor-wait-s must not be negative")
        if not args.predecessor_poll_s > 0:
            parser.error("--predecessor-poll-s must be positive")
    return args


def main(
    argv: list[str] | None = None,
    reader: HubReader | None = None,
    *,
    launcher_type: type[Launcher] = Launcher,
    worker: bool = False,
) -> int:
    args = parse_args(argv, worker=worker)
    launcher = launcher_type(args, reader)
    previous = signal.signal(signal.SIGTERM, launcher.interrupt)
    try:
        from agent_recovery import agent_lock

        with agent_lock(args.sessions):
            try:
                return launcher.run()
            except KeyboardInterrupt:
                launcher.interrupted = True
                launcher.stop_child(wait=True)
                return _stopped(launcher, Stop(EXIT_INTERRUPTED, "interrupted", "interrupted"))
            except Stop as stop:
                return _stopped(launcher, stop)
    except KeyboardInterrupt:
        launcher.interrupted = True
        launcher.stop_child(wait=True)
        return _stopped(launcher, Stop(EXIT_INTERRUPTED, "interrupted", "interrupted"))
    except Stop as stop:
        say(str(stop))
        return stop.code
    except (OSError, ValueError, urllib.error.URLError) as exc:
        say(
            f"Recovery/launch unavailable: {exc}. Restore the original run files and hub "
            "reachability, then retry the resume script; do not replay kickoff."
        )
        return EXIT_HUB_UNREADABLE
    finally:
        launcher.stop_child(wait=True)
        signal.signal(signal.SIGTERM, previous)


def _stopped(launcher: Launcher, stop: Stop) -> int:
    launcher.log("stop", reason=stop.reason, exit_code=stop.code)
    if stop.code == EXIT_INTERRUPTED:
        say("interrupted; stopped the harness this launcher started and launched nothing more")
    else:
        say(str(stop))
    return stop.code
