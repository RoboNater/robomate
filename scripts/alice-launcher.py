#!/usr/bin/env python3
"""Run a headless Alice, resuming the same conversation until her workflow is done (#146).

A headless orchestrator (``claude -p``, ``codex exec``, ``opencode run``,
``agy -p``) exits when the model ends its turn, and some models end it early
whatever the skill says. Nothing else restarts her, so the workflow sits idle.
This launcher is Alice's counterpart of ``claude-worker.py`` (#115):

1. It launches the harness once with the kickoff prompt (``--prompt``), or, for
   a manual resume, resumes ``--resume-session`` with ``--resume-prompt``.
2. It records the exact conversation ID: one it chose (Claude Code
   ``--session-id``) or the one the harness prints (Codex ``session id:``,
   OpenCode ``sessionID``, AntiGravity's stream-json conversation ID). It never
   resumes an unspecified "latest" conversation.
3. When the harness exits, it reads the run hub with bearer-only ``hub.info``
   and ``hub.status``, pinned to the hub and workflow it first saw. It never
   opens an orchestrator session, reads ``.robomate/``, or changes hub state.
4. A ``done`` workflow ends it successfully. ``active`` or ``escalated`` resumes
   the same conversation after ``--resume-delay-s``, at most ``--max-resumes``
   times, with one fixed continuation prompt that carries no state and no
   operator decision. ``paused`` stops it until the operator resumes by hand.

It stops, without launching again, when it cannot tell what happened: the hub
stays unreadable or reports another hub or workflow after ``--read-retries``
reads, the workflow was never initialized, or the conversation ID never
appeared. It does not watch a harness that keeps running; a stalled harness is
#144's warning, and stopping it stays with the operator.

Each launch, exit, conversation ID and stop reason is appended to
``--sessions`` as one JSON line, without the token or any command line.
Ctrl-C or SIGTERM stops the current harness child, launches nothing more, and
exits 130.

``prepare-run.py`` writes the call into ``start-alice`` and ``resume-alice``.
It runs this file with the run's own interpreter, so the standard library only.
Everything after ``--`` is the harness command without its prompt or session
arguments, which this launcher adds::

    python scripts/alice-launcher.py --harness opencode \\
        --hub-url http://127.0.0.1:8420 --token-file /state/token \\
        --sessions RUN/alice-sessions.jsonl --prompt RUN/alice.prompt.md \\
        -- opencode run --auto --title 'alice my-run'

Exit codes: 0 done; 1 resumes spent; 2 usage; 3 hub unreadable or a different
hub or workflow; 4 nothing safe to resume (no workflow, no conversation ID);
5 paused; 130 interrupted.
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
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, TextIO

DEFAULT_MAX_RESUMES = 5
DEFAULT_RESUME_DELAY_S = 5.0
DEFAULT_READ_RETRIES = 5
DEFAULT_READ_RETRY_DELAY_S = 2.0
HUB_TIMEOUT_S = 10.0

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
    print(f"alice-launcher: {message}", file=sys.stderr, flush=True)


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
    def __init__(self, args: argparse.Namespace, reader: HubReader | None = None) -> None:
        self.args = args
        self.reader = reader or HubReader(args.hub_url, args.token_file)
        self.harness: str = args.harness
        self.base = [shutil.which(args.command[0]) or args.command[0], *args.command[1:]]
        self.hub_id: str | None = None
        self.workflow_id: str | None = None
        self.conversation: str | None = None
        self.child: subprocess.Popen[str] | None = None
        self.interrupted = False

    def log(self, event: str, **fields: object) -> None:
        log_session(
            self.args.sessions,
            agent="alice",
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
            f"resume-alice and conversation {self.conversation or '(unknown)'}.",
        )

    def sleep(self, seconds: float) -> None:
        deadline = time.monotonic() + seconds
        while not self.interrupted and time.monotonic() < deadline:
            time.sleep(min(0.1, max(0.0, deadline - time.monotonic())))
        if self.interrupted:
            raise Stop(EXIT_INTERRUPTED, "interrupted", "interrupted")

    # -- running the harness ------------------------------------------------------

    def run_child(self, launch: Launch) -> int:
        """Run one harness process, relaying its output and watching for its conversation ID."""

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
        if launch.stdin is not None:
            threading.Thread(
                target=_feed, args=(self.child.stdin, launch.stdin), daemon=True
            ).start()
        assert self.child.stdout is not None
        for line in self.child.stdout:
            sys.stdout.write(line)
            sys.stdout.flush()
            if self.conversation is None:
                found = conversation_in(self.harness, line)
                if found is not None:
                    self.conversation = found
                    self.log("conversation")
                    say(f"conversation {found}")
        code = self.child.wait()
        self.child = None
        return code

    def stop_child(self) -> None:
        child = self.child
        if child is None or child.poll() is not None:
            return
        child.terminate()
        try:
            child.wait(timeout=10)
        except subprocess.TimeoutExpired:
            child.kill()
            child.wait()

    def interrupt(self, *_: object) -> None:
        self.interrupted = True
        self.stop_child()

    def run(self) -> int:
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
        while True:
            self.log(action, resumes=resumes)
            say(f"{action} ({self.harness}), conversation {self.conversation or 'pending'}")
            exit_code = self.run_child(launch)
            self.log("exit", resumes=resumes, exit_code=exit_code)
            if self.interrupted:
                raise Stop(EXIT_INTERRUPTED, "interrupted", "interrupted")
            workflow = self.read_hub()
            status = None if workflow is None else workflow["status"]
            if workflow is None:
                raise Stop(
                    EXIT_CANNOT_RESUME,
                    "no_workflow",
                    f"the harness exited (code {exit_code}) before initializing the workflow. "
                    "Not replaying the kickoff prompt. Inspect the harness output, then rerun "
                    "start-alice if nothing was done, or resume by hand.",
                )
            if status == "done":
                self.log("stop", reason="done", workflow_status=status, resumes=resumes)
                say(f"workflow done after {resumes} automatic resume(s)")
                return EXIT_DONE
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
                    f"with resume-alice {self.conversation} when the operator is ready.",
                )
            if resumes >= self.args.max_resumes:
                raise Stop(
                    EXIT_RESUMES_SPENT,
                    "resumes_spent",
                    f"the harness exited (code {exit_code}) with the workflow {status} and the "
                    f"{self.args.max_resumes} automatic resume(s) are spent. Take a "
                    "before-snapshot with `robomate status --snapshot` in the target "
                    f"repository, then resume by hand with resume-alice {self.conversation} "
                    "(docs/development/agent-recovery.md).",
                )
            resumes += 1
            say(
                f"harness exited (code {exit_code}) with the workflow {status}; resuming "
                f"({resumes}/{self.args.max_resumes}) in {self.args.resume_delay_s:g} s"
            )
            self.sleep(self.args.resume_delay_s)
            launch = resume_launch(self.harness, self.base, self.conversation, CONTINUE_PROMPT)
            action = "resume"


def _feed(stream: TextIO | None, text: str) -> None:
    if stream is None:
        return
    try:
        stream.write(text)
        stream.close()
    except (BrokenPipeError, OSError, ValueError):
        pass


def parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    parser.add_argument("--harness", required=True, choices=HARNESSES)
    parser.add_argument("--hub-url", required=True, help="the run hub's URL, never discovered")
    parser.add_argument("--token-file", type=Path, required=True, help="the hub bearer token")
    parser.add_argument("--sessions", type=Path, required=True, help="the JSONL launch log")
    parser.add_argument("--prompt", type=Path, help="the kickoff prompt file (first launch)")
    parser.add_argument("--resume-session", help="resume this conversation ID by hand")
    parser.add_argument(
        "--resume-prompt", type=Path, help="with --resume-session: the manual resume prompt"
    )
    parser.add_argument("--max-resumes", type=int, default=DEFAULT_MAX_RESUMES)
    parser.add_argument("--resume-delay-s", type=float, default=DEFAULT_RESUME_DELAY_S)
    parser.add_argument("--read-retries", type=int, default=DEFAULT_READ_RETRIES)
    parser.add_argument("--read-retry-delay-s", type=float, default=DEFAULT_READ_RETRY_DELAY_S)
    parser.add_argument("command", nargs=argparse.REMAINDER, help="the harness command, after --")
    args = parser.parse_args(argv)
    if args.command[:1] == ["--"]:
        args.command = args.command[1:]
    if not args.command:
        parser.error("the harness command is required after --")
    if args.max_resumes < 0:
        parser.error("--max-resumes must not be negative (0 disables automatic resumes)")
    if not args.resume_delay_s >= 0:
        parser.error("--resume-delay-s must not be negative")
    if args.read_retries < 1:
        parser.error("--read-retries must be at least 1")
    if not args.read_retry_delay_s >= 0:
        parser.error("--read-retry-delay-s must not be negative")
    if args.resume_session is not None:
        if not args.resume_session.strip():
            parser.error("--resume-session must name a conversation ID")
        if args.resume_prompt is None:
            parser.error("--resume-session needs --resume-prompt")
    elif args.prompt is None:
        parser.error("--prompt is required unless --resume-session is given")
    return args


def main(argv: list[str] | None = None, reader: HubReader | None = None) -> int:
    args = parse_args(argv)
    launcher = Launcher(args, reader)
    previous = signal.signal(signal.SIGTERM, launcher.interrupt)
    try:
        return launcher.run()
    except KeyboardInterrupt:
        launcher.interrupt()
        return _stopped(launcher, Stop(EXIT_INTERRUPTED, "interrupted", "interrupted"))
    except Stop as stop:
        return _stopped(launcher, stop)
    finally:
        signal.signal(signal.SIGTERM, previous)


def _stopped(launcher: Launcher, stop: Stop) -> int:
    launcher.log("stop", reason=stop.reason, exit_code=stop.code)
    if stop.code == EXIT_INTERRUPTED:
        say("interrupted; stopped the harness this launcher started and launched nothing more")
    else:
        say(str(stop))
    return stop.code


if __name__ == "__main__":
    sys.exit(main())
