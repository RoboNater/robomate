"""The Claude Code worker launcher resumes a worker that exits before release (#115, #158)."""

from __future__ import annotations

import importlib.util
import json
import sys
import uuid
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("claude_worker", ROOT / "scripts/claude-worker.py")
assert SPEC and SPEC.loader
CLAUDE_WORKER = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(CLAUDE_WORKER)

RELEASE = {
    "event": "tool_call",
    "phase": "success",
    "tool": "await_assignment",
    "outcome": "release",
}

# A stand-in for `claude -p`: it logs its argv, and when FAKE_RELEASE_ON names
# this launch ("start" or "resume") it records a release in the telemetry, as
# worker-mcp does when await_assignment returns release: true.
FAKE_HARNESS = """\
import json, os, sys
argv = sys.argv[1:]
with open(os.environ["FAKE_ARGV_LOG"], "a", encoding="utf-8") as log:
    log.write(json.dumps(argv) + "\\n")
launch = "resume" if "--resume" in argv else "start"
if launch in os.environ.get("FAKE_RELEASE_ON", "").split(","):
    with open(os.environ["FAKE_TELEMETRY"], "a", encoding="utf-8") as telemetry:
        telemetry.write(json.dumps(__RELEASE__) + "\\n")
sys.exit(3)
""".replace("__RELEASE__", repr(RELEASE))


def fake_harness(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, release_on: str) -> Path:
    fake = tmp_path / "fake_claude.py"
    fake.write_text(FAKE_HARNESS, encoding="utf-8")
    monkeypatch.setenv("FAKE_ARGV_LOG", str(tmp_path / "argv.jsonl"))
    monkeypatch.setenv("FAKE_TELEMETRY", str(tmp_path / "bob-telemetry.jsonl"))
    monkeypatch.setenv("FAKE_RELEASE_ON", release_on)
    return fake


def launch(
    tmp_path: Path,
    fake: Path,
    max_resumes: int | None = 2,
    extra: list[str] | None = None,
) -> int:
    argv = [
        "--agent",
        "bob",
        "--telemetry",
        str(tmp_path / "bob-telemetry.jsonl"),
        "--sessions",
        str(tmp_path / "bob-sessions.jsonl"),
        "--prompt",
        "Read bob.prompt.md and follow the instructions in it",
        "--resume-delay-s",
        "0",
        *(extra or []),
        sys.executable,
        str(fake),
        "--model",
        "opus",
    ]
    if max_resumes is not None:
        argv[argv.index("--resume-delay-s") : argv.index("--resume-delay-s")] = [
            "--max-resumes",
            str(max_resumes),
        ]
    return int(CLAUDE_WORKER.main(argv))


def jsonl(path: Path) -> list[Any]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def test_a_worker_that_exits_before_release_resumes_the_same_conversation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = fake_harness(tmp_path, monkeypatch, release_on="resume")
    # A release from an earlier launch must not count for this one.
    (tmp_path / "bob-telemetry.jsonl").write_text(json.dumps(RELEASE) + "\n", encoding="utf-8")

    assert launch(tmp_path, fake) == 0

    first, second = jsonl(tmp_path / "argv.jsonl")
    session_id = first[first.index("--session-id") + 1]
    assert str(uuid.UUID(session_id)) == session_id
    assert first == [
        "--model",
        "opus",
        "--session-id",
        session_id,
        "-p",
        "Read bob.prompt.md and follow the instructions in it",
    ]
    assert second == [
        "--model",
        "opus",
        "--resume",
        session_id,
        "-p",
        CLAUDE_WORKER.CONTINUE_PROMPT,
    ]
    sessions = jsonl(tmp_path / "bob-sessions.jsonl")
    assert [(r["event"], r["resumes"], r.get("released")) for r in sessions] == [
        ("start", 0, None),
        ("exit", 0, False),
        ("wait", 1, None),
        ("resume", 1, None),
        ("exit", 1, True),
    ]
    assert sessions[2]["delay_s"] == 0
    assert sessions[2]["next_attempt"]
    assert {r["session_id"] for r in sessions} == {session_id}
    assert {r["agent"] for r in sessions} == {"bob"}
    assert sessions[1]["exit_code"] == 3


def test_resumes_stop_at_the_limit(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fake = fake_harness(tmp_path, monkeypatch, release_on="")

    assert launch(tmp_path, fake, max_resumes=2) == 1

    launches = jsonl(tmp_path / "argv.jsonl")
    assert ["--resume" in argv for argv in launches] == [False, True, True]
    records = jsonl(tmp_path / "bob-sessions.jsonl")
    last_exit = [r for r in records if r["event"] == "exit"][-1]
    assert last_exit | {"timestamp": None} == {
        "timestamp": None,
        "agent": "bob",
        "event": "exit",
        "session_id": launches[0][launches[0].index("--session-id") + 1],
        "resumes": 2,
        "exit_code": 3,
        "released": False,
        "run_duration_s": last_exit["run_duration_s"],
    }
    assert records[-1]["event"] == "stop" and records[-1]["reason"] == "resumes_spent"


def test_a_released_worker_is_not_resumed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fake = fake_harness(tmp_path, monkeypatch, release_on="start")

    assert launch(tmp_path, fake) == 0

    assert len(jsonl(tmp_path / "argv.jsonl")) == 1


def test_the_claude_command_is_required() -> None:
    with pytest.raises(SystemExit):
        CLAUDE_WORKER.parse_args(
            ["--agent", "bob", "--telemetry", "t", "--sessions", "s", "--prompt", "p"]
        )
    # A `--` before the command is accepted and dropped.
    args = CLAUDE_WORKER.parse_args(
        [
            "--agent",
            "bob",
            "--telemetry",
            "t",
            "--sessions",
            "s",
            "--prompt",
            "p",
            "--",
            "claude",
            "--prompt",
            "x",
        ]
    )
    assert args.command == ["claude", "--prompt", "x"]


# -- backoff (#158) -------------------------------------------------------------


class FakeClock:
    """A controllable ``time.monotonic``/``time.sleep`` pair for the launcher."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.now = 1000.0
        self.sleeps: list[float] = []
        monkeypatch.setattr(CLAUDE_WORKER.time, "monotonic", self.monotonic)
        monkeypatch.setattr(CLAUDE_WORKER.time, "sleep", self.sleep)

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


def test_backoff_delay_doubles_then_repeats_at_the_cap() -> None:
    backoff = CLAUDE_WORKER.backoff_delay
    assert [backoff(5.0, 20.0, n) for n in range(5)] == [5.0, 10.0, 20.0, 20.0, 20.0]
    assert backoff(5.0, 1800.0, 8) == 1280.0
    assert backoff(5.0, 1800.0, 9) == 1800.0
    assert backoff(5.0, 1800.0, 40) == 1800.0
    assert backoff(0.0, 1800.0, 3) == 0.0


def test_waits_double_until_the_cap(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fake = fake_harness(tmp_path, monkeypatch, release_on="")
    clock = FakeClock(monkeypatch)

    assert (
        launch(
            tmp_path,
            fake,
            max_resumes=4,
            extra=["--resume-delay-s", "5", "--resume-max-delay-s", "12"],
        )
        == 1
    )

    assert clock.sleeps == [5.0, 10.0, 12.0, 12.0]
    waits = [r for r in jsonl(tmp_path / "bob-sessions.jsonl") if r["event"] == "wait"]
    assert [w["delay_s"] for w in waits] == [5.0, 10.0, 12.0, 12.0]
    assert all(w["next_attempt"] for w in waits)


def test_a_further_wait_past_the_time_budget_stops_instead(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = fake_harness(tmp_path, monkeypatch, release_on="")
    clock = FakeClock(monkeypatch)

    assert (
        launch(
            tmp_path,
            fake,
            max_resumes=None,
            extra=["--resume-delay-s", "5", "--resume-total-s", "12"],
        )
        == 1
    )

    # The first wait (5 s) fits in the 12 s budget; the doubled second (10 s)
    # would exceed it, so the launcher stops instead.
    assert clock.sleeps == [5.0]
    records = jsonl(tmp_path / "bob-sessions.jsonl")
    assert records[-1]["event"] == "stop"
    assert records[-1]["reason"] == "time_budget_spent"
    assert len(jsonl(tmp_path / "argv.jsonl")) == 2


def test_a_long_run_starts_a_new_series_at_the_short_delay(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = fake_harness(tmp_path, monkeypatch, release_on="")
    clock = FakeClock(monkeypatch)
    real_run = CLAUDE_WORKER.subprocess.run
    calls = 0

    def run(*args: Any, **kwargs: Any) -> Any:
        nonlocal calls
        calls += 1
        result = real_run(*args, **kwargs)
        if calls == 2:
            # The second harness run lasts 60 s of launcher time, after one
            # quick failure; the fake clock is frozen while the subprocess
            # runs, so advance it here.
            clock.now += 60.0
        return result

    monkeypatch.setattr(CLAUDE_WORKER.subprocess, "run", run)

    assert (
        launch(
            tmp_path,
            fake,
            max_resumes=3,
            extra=[
                "--resume-delay-s",
                "5",
                "--resume-series-reset-s",
                "30",
            ],
        )
        == 1
    )

    records = jsonl(tmp_path / "bob-sessions.jsonl")
    assert [r["event"] for r in records if r["event"] == "series_reset"]
    waits = [r for r in records if r["event"] == "wait"]
    # 5 s, then the long run resets the series back to 5 s, then 10 s.
    assert [w["delay_s"] for w in waits] == [5.0, 5.0, 10.0]


def test_sigint_during_a_long_wait_stops_without_another_launch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = fake_harness(tmp_path, monkeypatch, release_on="")

    def sleep(_: float) -> None:
        raise KeyboardInterrupt

    monkeypatch.setattr(CLAUDE_WORKER.time, "sleep", sleep)

    assert launch(tmp_path, fake, max_resumes=None) == 130
    assert len(jsonl(tmp_path / "argv.jsonl")) == 1
    records = jsonl(tmp_path / "bob-sessions.jsonl")
    assert records[-1]["event"] == "wait"
    assert not [r for r in records if r["event"] == "stop"]


def test_negative_backoff_options_are_refused() -> None:
    base = ["--agent", "bob", "--telemetry", "t", "--sessions", "s", "--prompt", "p"]
    for flag in (
        "--resume-max-delay-s",
        "--resume-total-s",
        "--resume-series-reset-s",
    ):
        with pytest.raises(SystemExit):
            CLAUDE_WORKER.parse_args([*base, flag, "-1", "claude"])
    with pytest.raises(SystemExit):
        CLAUDE_WORKER.parse_args([*base, "--max-resumes", "-2", "claude"])
