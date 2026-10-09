"""The orchestrator launcher resumes Alice's conversation until her workflow is done (#146).

A fake harness stands in for each CLI: it logs the arguments, standard input
and environment it was started with, prints its conversation ID the way that
harness does, and exits. A scripted reader stands in for the hub, except in the
HTTP test, which checks the reads themselves are bearer-only.
"""

from __future__ import annotations

import importlib.util
import json
import os
import signal
import subprocess
import sys
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts/alice-launcher.py"
SPEC = importlib.util.spec_from_file_location("alice_launcher", SCRIPT)
assert SPEC and SPEC.loader
LAUNCHER = importlib.util.module_from_spec(SPEC)
sys.modules["alice_launcher"] = LAUNCHER
SPEC.loader.exec_module(LAUNCHER)

HUB = "hub-1"
WORKFLOW = "wf-1"
CODEX_ID = "019a2b3c-4d5e-6f70-8192-a3b4c5d6e7f8"
IDS = {
    "claude-code": None,
    "codex": CODEX_ID,
    "opencode": "ses_7Fa9b2C",
    "antigravity": "conv-1234abcd",
}
# What each harness prints that carries its conversation ID.
ID_LINES = {
    "codex": f"session id: {CODEX_ID}",
    "opencode": json.dumps({"type": "step_start", "sessionID": IDS["opencode"]}),
    "antigravity": json.dumps({"type": "init", "conversation_id": IDS["antigravity"]}),
}

FAKE_HARNESS = """\
import json, os, sys, time
argv = sys.argv[1:]
stdin = sys.stdin.read() if argv and argv[-1] == "-" else None
record = {
    "argv": argv,
    "stdin": stdin,
    "env": {k: os.environ.get(k) for k in ("FAKE_MARK", "CODEX_HOME", "OPENCODE_CONFIG")},
}
with open(os.environ["FAKE_LOG"], "a", encoding="utf-8") as log:
    log.write(json.dumps(record) + "\\n")
line = os.environ.get("FAKE_ID_LINE")
if line:
    print("starting up")
    print(line, flush=True)
time.sleep(float(os.environ.get("FAKE_SLEEP_S", "0")))
sys.exit(int(os.environ.get("FAKE_EXIT", "0")))
"""


class ScriptedReader:
    """Answers each hub read from a script; the last entry repeats."""

    def __init__(self, *script: Any) -> None:
        self.script = list(script)
        self.reads = 0

    def read(self) -> tuple[str, dict[str, Any] | None]:
        self.reads += 1
        step = self.script.pop(0) if len(self.script) > 1 else self.script[0]
        if isinstance(step, BaseException):
            raise step
        if callable(step):
            return step()  # type: ignore[no-any-return]
        return step  # type: ignore[no-any-return]


def wf(status: str, workflow: str = WORKFLOW, hub: str = HUB) -> tuple[str, dict[str, Any]]:
    return hub, {"id": workflow, "status": status}


@pytest.fixture
def run_dir(tmp_path: Path) -> Path:
    # Spaces in every path the launcher passes on (#146).
    directory = tmp_path / "my run dir"
    directory.mkdir()
    (directory / "alice.prompt.md").write_text("Kickoff: initialize the workflow.\n")
    (directory / "resume-alice.prompt.md").write_text("Manual resume: reconcile.\n")
    (directory / "token").write_text("secret-token-value\n")
    return directory


@pytest.fixture
def fake(run_dir: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    script = run_dir / "fake harness.py"
    script.write_text(FAKE_HARNESS, encoding="utf-8")
    monkeypatch.setenv("FAKE_LOG", str(run_dir / "launches.jsonl"))
    monkeypatch.setenv("FAKE_MARK", "kept")
    monkeypatch.setenv("CODEX_HOME", str(run_dir / "codex home"))
    monkeypatch.setenv("OPENCODE_CONFIG", str(run_dir / "alice.opencode.json"))
    monkeypatch.delenv("FAKE_ID_LINE", raising=False)
    return script


def use_harness(harness: str, monkeypatch: pytest.MonkeyPatch) -> None:
    if harness in ID_LINES:
        monkeypatch.setenv("FAKE_ID_LINE", ID_LINES[harness])


def args(run_dir: Path, fake: Path, harness: str, *extra: str) -> list[str]:
    return [
        "--harness",
        harness,
        "--hub-url",
        "http://127.0.0.1:9",
        "--token-file",
        str(run_dir / "token"),
        "--sessions",
        str(run_dir / "alice-sessions.jsonl"),
        "--resume-delay-s",
        "0",
        "--read-retry-delay-s",
        "0",
        *extra,
        "--",
        sys.executable,
        str(fake),
        "--model",
        "m-1",
        "--flag",
    ]


def start_args(run_dir: Path, fake: Path, harness: str, *extra: str) -> list[str]:
    return args(run_dir, fake, harness, "--prompt", str(run_dir / "alice.prompt.md"), *extra)


def launches(run_dir: Path) -> list[dict[str, Any]]:
    path = run_dir / "launches.jsonl"
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines()]


def sessions(run_dir: Path) -> list[dict[str, Any]]:
    return [
        json.loads(line) for line in (run_dir / "alice-sessions.jsonl").read_text().splitlines()
    ]


def events(run_dir: Path) -> list[str]:
    return [record["event"] for record in sessions(run_dir)]


@pytest.mark.parametrize("harness", ["claude-code", "codex", "opencode", "antigravity"])
def test_each_harness_resumes_its_own_conversation_until_done(
    run_dir: Path, fake: Path, monkeypatch: pytest.MonkeyPatch, harness: str
) -> None:
    use_harness(harness, monkeypatch)
    reader = ScriptedReader(wf("active"), wf("active"), wf("escalated"), wf("done"))

    assert LAUNCHER.main(start_args(run_dir, fake, harness), reader) == 0

    runs = launches(run_dir)
    assert len(runs) == 3
    for run in runs:
        # Model, flags and environment survive every resume.
        assert run["argv"][:3] == ["--model", "m-1", "--flag"]
        assert run["env"] == {
            "FAKE_MARK": "kept",
            "CODEX_HOME": str(run_dir / "codex home"),
            "OPENCODE_CONFIG": str(run_dir / "alice.opencode.json"),
        }
    kickoff = f"Read {run_dir / 'alice.prompt.md'} and follow the instructions in it"
    first, *resumed = (run["argv"][3:] for run in runs)
    log = sessions(run_dir)
    conversation = log[-1]["conversation_id"]
    if harness == "claude-code":
        assert first[:2] == ["--session-id", conversation] and first[2:] == ["-p", kickoff]
        expected = ["--resume", conversation, "-p", LAUNCHER.CONTINUE_PROMPT]
    elif harness == "codex":
        assert first == ["-"] and runs[0]["stdin"] == "Kickoff: initialize the workflow.\n"
        expected = ["resume", conversation, "-"]
        assert [run["stdin"] for run in runs[1:]] == [LAUNCHER.CONTINUE_PROMPT] * 2
    elif harness == "opencode":
        assert first == ["--format", "json", kickoff]
        expected = ["--format", "json", "--session", conversation, LAUNCHER.CONTINUE_PROMPT]
    else:
        assert first == ["--output-format", "stream-json", "-p", kickoff]
        expected = [
            "--conversation",
            conversation,
            "--output-format",
            "stream-json",
            "-p",
            LAUNCHER.CONTINUE_PROMPT,
        ]
    assert resumed == [expected, expected]
    if IDS[harness] is not None:
        assert conversation == IDS[harness]
    assert events(run_dir) == [
        "start",
        *(["conversation"] if harness != "claude-code" else []),
        "exit",
        "resume",
        "exit",
        "resume",
        "exit",
        "stop",
    ]
    assert log[-1]["reason"] == "done"
    assert {r["hub_id"] for r in log[1:]} == {HUB} and log[-1]["workflow_id"] == WORKFLOW
    text = (run_dir / "alice-sessions.jsonl").read_text()
    assert "secret-token-value" not in text and str(fake) not in text


def test_the_continuation_carries_no_state_or_decision() -> None:
    prompt = LAUNCHER.CONTINUE_PROMPT
    assert "get_state first" in prompt and "\n" not in prompt
    assert "no operator decision, approval, or state" in prompt
    for word in ("approved", "answer is", "snapshot", "delivery_id", "task_id"):
        assert word not in prompt


def test_resumes_are_bounded_and_exhaustion_exits_nonzero(
    run_dir: Path, fake: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    reader = ScriptedReader(wf("active"))
    code = LAUNCHER.main(start_args(run_dir, fake, "claude-code", "--max-resumes", "2"), reader)
    assert code == LAUNCHER.EXIT_RESUMES_SPENT
    assert len(launches(run_dir)) == 3
    assert sessions(run_dir)[-1]["reason"] == "resumes_spent"
    err = capsys.readouterr().err
    assert "robomate status --snapshot" in err and "resume-alice" in err


def test_zero_max_resumes_disables_automatic_resumes(run_dir: Path, fake: Path) -> None:
    reader = ScriptedReader(wf("active"))
    code = LAUNCHER.main(start_args(run_dir, fake, "claude-code", "--max-resumes", "0"), reader)
    assert code == LAUNCHER.EXIT_RESUMES_SPENT and len(launches(run_dir)) == 1


def test_a_paused_workflow_is_not_restarted(
    run_dir: Path, fake: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    reader = ScriptedReader(wf("active"), wf("paused"))
    assert LAUNCHER.main(start_args(run_dir, fake, "claude-code"), reader) == LAUNCHER.EXIT_PAUSED
    assert len(launches(run_dir)) == 1
    assert sessions(run_dir)[-1]["reason"] == "paused"
    assert "not restarting Alice while it is" in capsys.readouterr().err


def test_an_exit_before_initialization_never_replays_the_kickoff(run_dir: Path, fake: Path) -> None:
    reader = ScriptedReader((HUB, None))
    code = LAUNCHER.main(start_args(run_dir, fake, "claude-code"), reader)
    assert code == LAUNCHER.EXIT_CANNOT_RESUME
    assert len(launches(run_dir)) == 1
    assert sessions(run_dir)[-1]["reason"] == "no_workflow"


def test_no_conversation_id_means_no_guessed_resume(
    run_dir: Path, fake: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FAKE_ID_LINE", "no identity here")
    reader = ScriptedReader(wf("active"))
    code = LAUNCHER.main(start_args(run_dir, fake, "opencode"), reader)
    assert code == LAUNCHER.EXIT_CANNOT_RESUME
    assert len(launches(run_dir)) == 1
    assert sessions(run_dir)[-1]["reason"] == "no_conversation"


@pytest.mark.parametrize(
    "failure",
    [
        OSError("connection refused"),
        ValueError("malformed hub.status response"),
        wf("active", hub="hub-2"),
        wf("active", workflow="wf-2"),
        (HUB, None),
    ],
    ids=["unavailable", "malformed", "other hub", "other workflow", "workflow vanished"],
)
def test_unreadable_or_different_hub_state_is_retried_then_stops(
    run_dir: Path, fake: Path, failure: Any
) -> None:
    reader = ScriptedReader(wf("active"), failure)
    code = LAUNCHER.main(start_args(run_dir, fake, "claude-code", "--read-retries", "3"), reader)
    assert code == LAUNCHER.EXIT_HUB_UNREADABLE
    assert reader.reads == 1 + 3
    assert len(launches(run_dir)) == 1
    assert sessions(run_dir)[-1]["reason"] == "hub_unreadable"


def test_a_transient_read_failure_recovers(run_dir: Path, fake: Path) -> None:
    reader = ScriptedReader(wf("active"), OSError("blip"), wf("done"))
    assert LAUNCHER.main(start_args(run_dir, fake, "claude-code"), reader) == 0
    assert len(launches(run_dir)) == 1


def test_an_unreachable_hub_before_the_first_launch_launches_nothing(
    run_dir: Path, fake: Path
) -> None:
    reader = ScriptedReader(OSError("down"))
    code = LAUNCHER.main(start_args(run_dir, fake, "claude-code", "--read-retries", "2"), reader)
    assert code == LAUNCHER.EXIT_HUB_UNREADABLE and launches(run_dir) == []


@pytest.mark.parametrize("harness", ["claude-code", "codex", "opencode", "antigravity"])
def test_a_manual_resume_uses_the_operator_prompt_once_then_the_continuation(
    run_dir: Path, fake: Path, monkeypatch: pytest.MonkeyPatch, harness: str
) -> None:
    use_harness(harness, monkeypatch)
    reader = ScriptedReader(wf("paused"), wf("active"), wf("done"))
    extra = ["--resume-session", "conv-given", "--resume-prompt"]
    code = LAUNCHER.main(
        args(run_dir, fake, harness, *extra, str(run_dir / "resume-alice.prompt.md")), reader
    )

    assert code == 0
    runs = launches(run_dir)
    assert len(runs) == 2
    manual = f"Read {run_dir / 'resume-alice.prompt.md'} and follow the instructions in it"
    joined = [" ".join(run["argv"]) for run in runs]
    assert all("conv-given" in line for line in joined)
    if harness == "codex":
        assert runs[0]["stdin"] == "Manual resume: reconcile.\n"
        assert runs[1]["stdin"] == LAUNCHER.CONTINUE_PROMPT
    else:
        assert runs[0]["argv"][-1] == manual
        assert runs[1]["argv"][-1] == LAUNCHER.CONTINUE_PROMPT
    # The given conversation wins over anything the harness prints.
    assert {r["conversation_id"] for r in sessions(run_dir)} == {"conv-given"}
    assert events(run_dir)[0] == "manual_resume"


def test_a_manual_resume_of_a_done_or_missing_workflow_launches_nothing(
    run_dir: Path, fake: Path
) -> None:
    extra = ["--resume-session", "c-1", "--resume-prompt", str(run_dir / "resume-alice.prompt.md")]
    assert (
        LAUNCHER.main(args(run_dir, fake, "claude-code", *extra), ScriptedReader(wf("done"))) == 0
    )
    code = LAUNCHER.main(args(run_dir, fake, "claude-code", *extra), ScriptedReader((HUB, None)))
    assert code == LAUNCHER.EXIT_CANNOT_RESUME
    assert launches(run_dir) == []


@pytest.mark.parametrize(
    "extra",
    [
        ["--max-resumes", "-1"],
        ["--resume-delay-s", "-1"],
        ["--read-retries", "0"],
        ["--resume-session", "c-1"],
        ["--harness-typo"],
    ],
)
def test_invalid_options_are_refused(run_dir: Path, fake: Path, extra: list[str]) -> None:
    with pytest.raises(SystemExit) as raised:
        LAUNCHER.parse_args(start_args(run_dir, fake, "claude-code", *extra))
    assert raised.value.code == 2


def test_a_prompt_is_required_for_a_start(run_dir: Path, fake: Path) -> None:
    with pytest.raises(SystemExit):
        LAUNCHER.parse_args(args(run_dir, fake, "claude-code"))


def test_an_interruption_stops_the_child_and_launches_nothing_more(
    run_dir: Path, fake: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FAKE_SLEEP_S", "30")
    reader = ScriptedReader(wf("active"))
    launcher = LAUNCHER.Launcher(
        LAUNCHER.parse_args(start_args(run_dir, fake, "claude-code")), reader
    )

    def interrupt() -> None:
        for _ in range(200):
            if launcher.child is not None and launches(run_dir):
                break
            time.sleep(0.05)
        launcher.interrupt()

    threading.Thread(target=interrupt).start()
    started = time.monotonic()
    with pytest.raises(LAUNCHER.Stop) as stopped:
        launcher.run()
    assert stopped.value.code == LAUNCHER.EXIT_INTERRUPTED
    assert time.monotonic() - started < 20
    assert len(launches(run_dir)) == 1
    assert reader.reads == 1  # no hub read after the interruption


@pytest.mark.skipif(sys.platform == "win32", reason="SIGTERM delivery is POSIX-only")
def test_sigterm_to_the_launcher_process_stops_its_harness(
    run_dir: Path, fake: Path, tmp_path: Path
) -> None:
    with hub_server() as (url, _):
        env = {
            **os.environ,
            "FAKE_LOG": str(run_dir / "launches.jsonl"),
            "FAKE_SLEEP_S": "60",
        }
        argv = start_args(run_dir, fake, "claude-code")
        argv[argv.index("--hub-url") + 1] = url
        process = subprocess.Popen(
            [sys.executable, str(SCRIPT), *argv],
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
        )
        try:
            for _ in range(200):
                if launches(run_dir):
                    break
                time.sleep(0.05)
            process.send_signal(signal.SIGTERM)
            assert process.wait(timeout=20) == LAUNCHER.EXIT_INTERRUPTED
        finally:
            if process.poll() is None:
                process.kill()
                process.wait()
    assert len(launches(run_dir)) == 1
    assert sessions(run_dir)[-1]["reason"] == "interrupted"


# -- the reads themselves ------------------------------------------------------------


class _Hub(BaseHTTPRequestHandler):
    seen: list[dict[str, Any]] = []
    status = "done"

    def do_POST(self) -> None:  # noqa: N802 - the stdlib's name
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        self.seen.append({"method": body["method"], "headers": dict(self.headers)})
        if body["method"] == "hub.info":
            result: dict[str, Any] = {"hub_id": HUB, "url": "x"}
        else:
            result = {"workflow": {"id": WORKFLOW, "status": self.status, "headline": "h"}}
        payload = json.dumps({"jsonrpc": "2.0", "id": 1, "result": result}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, *_: Any) -> None:
        pass


@contextmanager
def hub_server() -> Iterator[tuple[str, list[dict[str, Any]]]]:
    _Hub.seen = []
    _Hub.status = "active"
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Hub)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}", _Hub.seen
    finally:
        server.shutdown()
        server.server_close()


def test_hub_reads_are_bearer_only_and_never_open_a_session(run_dir: Path) -> None:
    with hub_server() as (url, seen):
        reader = LAUNCHER.HubReader(url, run_dir / "token")
        assert reader.read() == (HUB, {"id": WORKFLOW, "status": "active"})
    assert [call["method"] for call in seen] == ["hub.info", "hub.status"]
    for call in seen:
        headers = {key.lower(): value for key, value in call["headers"].items()}
        assert headers["authorization"] == "Bearer secret-token-value"
        assert not any(key.startswith("x-robomate") for key in headers)
