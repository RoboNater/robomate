"""The Claude Code worker launcher resumes a worker that exits before release (#115)."""

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


def launch(tmp_path: Path, fake: Path, max_resumes: int = 2) -> int:
    return int(
        CLAUDE_WORKER.main([
            "--agent", "bob",
            "--telemetry", str(tmp_path / "bob-telemetry.jsonl"),
            "--sessions", str(tmp_path / "bob-sessions.jsonl"),
            "--prompt", "Read bob.prompt.md and follow the instructions in it",
            "--max-resumes", str(max_resumes),
            "--resume-delay-s", "0",
            sys.executable, str(fake), "--model", "opus",
        ])
    )


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
        "--model", "opus", "--session-id", session_id,
        "-p", "Read bob.prompt.md and follow the instructions in it",
    ]
    assert second == [
        "--model", "opus", "--resume", session_id, "-p", CLAUDE_WORKER.CONTINUE_PROMPT,
    ]
    sessions = jsonl(tmp_path / "bob-sessions.jsonl")
    assert [(r["event"], r["resumes"], r.get("released")) for r in sessions] == [
        ("start", 0, None), ("exit", 0, False), ("resume", 1, None), ("exit", 1, True),
    ]
    assert {r["session_id"] for r in sessions} == {session_id}
    assert {r["agent"] for r in sessions} == {"bob"}
    assert sessions[1]["exit_code"] == 3


def test_resumes_stop_at_the_limit(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fake = fake_harness(tmp_path, monkeypatch, release_on="")

    assert launch(tmp_path, fake, max_resumes=2) == 1

    launches = jsonl(tmp_path / "argv.jsonl")
    assert ["--resume" in argv for argv in launches] == [False, True, True]
    assert jsonl(tmp_path / "bob-sessions.jsonl")[-1] | {"timestamp": None} == {
        "timestamp": None, "agent": "bob", "event": "exit",
        "session_id": launches[0][launches[0].index("--session-id") + 1],
        "resumes": 2, "exit_code": 3, "released": False,
    }


def test_a_released_worker_is_not_resumed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = fake_harness(tmp_path, monkeypatch, release_on="start")

    assert launch(tmp_path, fake) == 0

    assert len(jsonl(tmp_path / "argv.jsonl")) == 1


def test_the_claude_command_is_required() -> None:
    with pytest.raises(SystemExit):
        CLAUDE_WORKER.parse_args(["--agent", "bob", "--telemetry", "t", "--sessions", "s",
                                  "--prompt", "p"])
    # A `--` before the command is accepted and dropped.
    args = CLAUDE_WORKER.parse_args(["--agent", "bob", "--telemetry", "t", "--sessions", "s",
                                     "--prompt", "p", "--", "claude", "--prompt", "x"])
    assert args.command == ["claude", "--prompt", "x"]
