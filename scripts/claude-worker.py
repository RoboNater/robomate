#!/usr/bin/env python3
"""Run a headless Claude Code worker, resuming it until Alice releases it (#115, #158).

``claude -p`` exits when the model ends its turn. A worker that ends its turn
before ``await_assignment`` returned ``release: true`` (to wait for CI, say)
would otherwise stay gone until the hub declares it lost and fails its task.
This launcher starts the conversation under a session ID it chose, and each
time ``claude`` exits before the worker's telemetry shows a release, it resumes
that same conversation with a fixed prompt, backing off between resumes (#158).
worker-mcp detaches from the hub as it stops, so the resumed worker's new
worker-mcp can check in at once and keep the open task.

Resumes run in series: consecutive quick exits double the wait from
``--resume-delay-s`` up to ``--resume-max-delay-s`` (default 30 min), then
repeat at the cap. A run that lasts at least ``--resume-series-reset-s``
(default 5 min) starts a new series with the short delay again. Resumes stop
after ``--resume-total-s`` (default 12 h) of launcher elapsed time, or after
``--max-resumes`` when given (by default no count limit; 0 disables resumes).

Each launch, wait and exit is appended to ``--sessions`` as one JSON line: that
is where an operator finds the conversation ID for a manual ``claude --resume``.

``prepare-run.py`` writes the call into a Claude Code worker's start script.
It runs this file with the run's own interpreter, without ``uv run``, so the
worker's shell inherits no virtualenv; hence the standard library only.
Everything after the options is the ``claude`` command, without ``-p``::

    python scripts/claude-worker.py --agent bob \\
        --telemetry RUN/bob-telemetry.jsonl --sessions RUN/bob-sessions.jsonl \\
        --prompt 'Read RUN/bob.prompt.md and follow the instructions in it' \\
        claude --model opus --permission-mode auto --strict-mcp-config ...
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import time
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

DEFAULT_MAX_RESUMES: int | None = None
DEFAULT_RESUME_DELAY_S = 5.0
DEFAULT_RESUME_MAX_DELAY_S = 30 * 60.0
DEFAULT_RESUME_TOTAL_S = 12 * 60 * 60.0
DEFAULT_RESUME_SERIES_RESET_S = 5 * 60.0

CONTINUE_PROMPT = (
    "Your previous turn ended before Alice released you, so the harness exited; "
    "this launcher has resumed the same conversation. Your hub tools now run in a "
    "new worker-mcp process: call check_in once, then carry on where you stopped. "
    "If you still hold a task, finish it and submit its result; if the hub says the "
    "task was failed or canceled, drop it. Then call await_assignment again. Do not "
    "end your turn until await_assignment returns release: true, and wait for CI or "
    "any other work in the foreground."
)


def telemetry_size(path: Path) -> int:
    try:
        return path.stat().st_size
    except FileNotFoundError:
        return 0


def released(path: Path, offset: int) -> bool:
    """Whether worker telemetry written past ``offset`` records a release."""
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


def log_session(path: Path, **fields: object) -> None:
    record = {"timestamp": datetime.now(UTC).isoformat(timespec="seconds"), **fields}
    with path.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(record, sort_keys=True) + "\n")


def backoff_delay(initial_s: float, cap_s: float, failures_in_series: int) -> float:
    """The wait before the next resume: doubling from ``initial_s`` to ``cap_s`` (#158)."""
    if not initial_s > 0:
        return 0.0
    if failures_in_series >= 30:
        return cap_s
    return min(initial_s * (2.0**failures_in_series), cap_s)


def retry_at_iso(delay_s: float) -> str:
    return (datetime.now(UTC) + timedelta(seconds=max(0.0, delay_s))).isoformat(timespec="seconds")


def parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--agent", required=True)
    parser.add_argument(
        "--telemetry", type=Path, required=True, help="the worker's HUB_TELEMETRY_LOG"
    )
    parser.add_argument(
        "--sessions",
        type=Path,
        required=True,
        help="JSONL file each launch, wait and exit is appended to",
    )
    parser.add_argument("--prompt", required=True, help="the first turn's -p prompt")
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
        help="stop automatic resumes after this much launcher elapsed time in seconds "
        f"(default {DEFAULT_RESUME_TOTAL_S:g}, about 12 hours)",
    )
    parser.add_argument(
        "--resume-series-reset-s",
        type=float,
        default=DEFAULT_RESUME_SERIES_RESET_S,
        help="a run lasting this long starts a new backoff series at the short delay "
        f"(default {DEFAULT_RESUME_SERIES_RESET_S:g})",
    )
    parser.add_argument(
        "command", nargs=argparse.REMAINDER, help="the claude command and its flags, without -p"
    )
    args = parser.parse_args(argv)
    if args.command[:1] == ["--"]:
        args.command = args.command[1:]
    if not args.command:
        parser.error("the claude command is required after the options")
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
    return args


def run(args: argparse.Namespace) -> int:
    # `which` finds an npm `claude.cmd` shim on Windows, which a bare name does not.
    command = [shutil.which(args.command[0]) or args.command[0], *args.command[1:]]
    session_id = str(uuid.uuid4())
    start = telemetry_size(args.telemetry)
    turn = ["--session-id", session_id, "-p", args.prompt]
    launcher_start = time.monotonic()
    resumes = 0
    failures_in_series = 0
    while True:
        action = "resume" if resumes else "start"
        log_session(
            args.sessions, agent=args.agent, event=action, session_id=session_id, resumes=resumes
        )
        print(f"{args.agent}: claude {action}, conversation {session_id}", file=sys.stderr)
        run_start = time.monotonic()
        exit_code = subprocess.run([*command, *turn], check=False).returncode
        run_duration = time.monotonic() - run_start
        done = released(args.telemetry, start)
        log_session(
            args.sessions,
            agent=args.agent,
            event="exit",
            session_id=session_id,
            resumes=resumes,
            exit_code=exit_code,
            released=done,
            run_duration_s=round(run_duration, 3),
        )
        if done:
            print(f"{args.agent}: released by Alice after {resumes} resume(s)", file=sys.stderr)
            return 0
        if run_duration >= args.resume_series_reset_s and (failures_in_series or resumes):
            failures_in_series = 0
            log_session(
                args.sessions,
                agent=args.agent,
                event="series_reset",
                session_id=session_id,
                resumes=resumes,
                run_duration_s=round(run_duration, 3),
            )
            print(
                f"{args.agent}: ran {run_duration:.0f} s, starting a new backoff series "
                f"at {args.resume_delay_s:g} s",
                file=sys.stderr,
            )
        if args.max_resumes is not None and resumes >= args.max_resumes:
            log_session(
                args.sessions,
                agent=args.agent,
                event="stop",
                reason="resumes_spent",
                session_id=session_id,
                resumes=resumes,
            )
            print(
                f"{args.agent}: claude exited before release (code {exit_code}) and the "
                f"{args.max_resumes} resume(s) allowed are spent; resume conversation "
                f"{session_id} by hand (docs/development/agent-recovery.md)",
                file=sys.stderr,
            )
            return 1
        delay = backoff_delay(args.resume_delay_s, args.resume_max_delay_s, failures_in_series)
        elapsed = time.monotonic() - launcher_start
        if elapsed >= args.resume_total_s or elapsed + delay > args.resume_total_s:
            log_session(
                args.sessions,
                agent=args.agent,
                event="stop",
                reason="time_budget_spent",
                session_id=session_id,
                resumes=resumes,
                elapsed_s=round(elapsed, 3),
            )
            print(
                f"{args.agent}: claude exited before release (code {exit_code}) and the "
                f"resume time budget of {args.resume_total_s:g} s is spent after "
                f"{resumes} resume(s); resume conversation {session_id} by hand "
                "(docs/development/agent-recovery.md)",
                file=sys.stderr,
            )
            return 1
        resumes += 1
        failures_in_series += 1
        attempt_at = retry_at_iso(delay)
        log_session(
            args.sessions,
            agent=args.agent,
            event="wait",
            session_id=session_id,
            resumes=resumes,
            delay_s=delay,
            next_attempt=attempt_at,
        )
        count = f"{resumes}/{args.max_resumes}" if args.max_resumes is not None else f"{resumes}"
        print(
            f"{args.agent}: claude exited before release (code {exit_code}); resuming "
            f"({count}) in {delay:g} s at {attempt_at}",
            file=sys.stderr,
        )
        # A long wait is a plain sleep: SIGINT raises KeyboardInterrupt at once,
        # which main() turns into exit 130 without launching again.
        time.sleep(delay)
        turn = ["--resume", session_id, "-p", CONTINUE_PROMPT]


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        return run(args)
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
