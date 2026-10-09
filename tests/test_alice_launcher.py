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
    # Reads: before the first launch, then after each exit and again after
    # each resume delay.
    reader = ScriptedReader(
        wf("active"), wf("active"), wf("active"), wf("escalated"), wf("escalated"), wf("done")
    )

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
        "wait",
        "resume",
        "exit",
        "wait",
        "resume",
        "exit",
        "stop",
    ]
    assert log[-1]["reason"] == "done"
    assert all(isinstance(r["pid"], int) for r in log if r["event"] in ("start", "resume"))
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
    reader = ScriptedReader(wf("paused"), wf("active"), wf("active"), wf("done"))
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
        ["--resume-max-delay-s", "-1"],
        ["--resume-total-s", "-1"],
        ["--resume-series-reset-s", "-1"],
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


# -- review r1 -------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("after_delay", "code", "reason"),
    [
        (wf("paused"), LAUNCHER.EXIT_PAUSED, "paused"),
        (wf("done"), LAUNCHER.EXIT_DONE, "done"),
        (wf("active", hub="hub-2"), LAUNCHER.EXIT_HUB_UNREADABLE, "hub_unreadable"),
    ],
    ids=["paused", "done", "other hub"],
)
def test_the_hub_is_read_again_after_the_delay_before_any_resume(
    run_dir: Path, fake: Path, after_delay: Any, code: int, reason: str
) -> None:
    """r1-3: a pause (or done, or another hub) during the delay prevents the launch."""

    reader = ScriptedReader(wf("active"), wf("active"), after_delay)
    launcher = LAUNCHER.Launcher(
        LAUNCHER.parse_args(start_args(run_dir, fake, "claude-code", "--read-retries", "1")),
        reader,
    )
    delays: list[float] = []
    launcher.sleep = delays.append
    try:
        result = launcher.run()
    except LAUNCHER.Stop as stop:
        result = stop.code
        assert stop.reason == reason
    assert result == code
    assert delays == [0.0]
    assert len(launches(run_dir)) == 1


DESCENDANT_HARNESS = """\
import json, os, subprocess, sys
with open(os.environ["FAKE_LOG"], "a", encoding="utf-8") as log:
    log.write(json.dumps({"argv": sys.argv[1:], "stdin": None, "env": {}}) + "\\n")
# A descendant inherits stdout and outlives the harness, as a bridge or a
# shell can; the harness itself exits at once.
hold = "import time; time.sleep(float(%r))" % os.environ["HOLD_S"]
subprocess.Popen([sys.executable, "-c", hold])
print("harness exiting now", flush=True)
"""


def test_a_descendant_holding_the_pipe_does_not_delay_recovery(
    run_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """r1-4: the child's exit is awaited directly, not through output EOF."""

    script = run_dir / "descendant harness.py"
    script.write_text(DESCENDANT_HARNESS, encoding="utf-8")
    monkeypatch.setenv("FAKE_LOG", str(run_dir / "launches.jsonl"))
    monkeypatch.setenv("HOLD_S", "15")
    reader = ScriptedReader(wf("active"), wf("done"))
    started = time.monotonic()
    assert LAUNCHER.main(start_args(run_dir, script, "claude-code"), reader) == 0
    elapsed = time.monotonic() - started
    assert elapsed < LAUNCHER.RELAY_GRACE_S + 5, f"waited {elapsed:.1f}s for the descendant"
    assert len(launches(run_dir)) == 1


def test_a_conversation_id_printed_just_before_exit_is_still_captured(
    run_dir: Path, fake: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    use_harness("opencode", monkeypatch)
    reader = ScriptedReader(wf("active"), wf("active"), wf("active"), wf("done"))
    assert LAUNCHER.main(start_args(run_dir, fake, "opencode"), reader) == 0
    runs = launches(run_dir)
    assert len(runs) == 2 and IDS["opencode"] in runs[1]["argv"]


# -- backoff (#158) ----------------------------------------------------------------


class FakeClock:
    """A controllable ``time.monotonic`` for the launcher's budget and series clocks."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.now = 1000.0
        monkeypatch.setattr(LAUNCHER.time, "monotonic", self.monotonic)

    def monotonic(self) -> float:
        return self.now


def test_backoff_delay_doubles_then_repeats_at_the_cap() -> None:
    backoff = LAUNCHER.backoff_delay
    assert [backoff(5.0, 20.0, n) for n in range(5)] == [5.0, 10.0, 20.0, 20.0, 20.0]
    assert backoff(5.0, 1800.0, 8) == 1280.0
    assert backoff(5.0, 1800.0, 9) == 1800.0
    assert backoff(5.0, 1800.0, 40) == 1800.0
    assert backoff(0.0, 1800.0, 3) == 0.0


def test_waits_double_until_the_cap_with_visible_retries(
    run_dir: Path, fake: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    reader = ScriptedReader(wf("active"))
    launcher = LAUNCHER.Launcher(
        LAUNCHER.parse_args(
            start_args(
                run_dir,
                fake,
                "claude-code",
                "--max-resumes",
                "4",
                "--resume-delay-s",
                "5",
                "--resume-max-delay-s",
                "12",
            )
        ),
        reader,
    )
    delays: list[float] = []
    launcher.sleep = delays.append
    with pytest.raises(LAUNCHER.Stop) as stopped:
        launcher.run()
    assert stopped.value.code == LAUNCHER.EXIT_RESUMES_SPENT
    assert stopped.value.reason == "resumes_spent"
    assert delays == [5.0, 10.0, 12.0, 12.0]
    waits = [r for r in sessions(run_dir) if r["event"] == "wait"]
    assert [w["delay_s"] for w in waits] == [5.0, 10.0, 12.0, 12.0]
    assert all(w["next_attempt"] for w in waits)
    err = capsys.readouterr().err
    assert "in 10 s at" in err and "resuming (2/4)" in err


def test_a_further_wait_past_the_time_budget_stops_instead(
    run_dir: Path, fake: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    clock = FakeClock(monkeypatch)
    reader = ScriptedReader(wf("active"))
    launcher = LAUNCHER.Launcher(
        LAUNCHER.parse_args(
            start_args(
                run_dir,
                fake,
                "claude-code",
                "--resume-delay-s",
                "5",
                "--resume-total-s",
                "12",
            )
        ),
        reader,
    )
    elapsed: list[float] = []

    def sleep(seconds: float) -> None:
        elapsed.append(seconds)
        clock.now += seconds

    launcher.sleep = sleep
    with pytest.raises(LAUNCHER.Stop) as stopped:
        launcher.run()
    assert stopped.value.code == LAUNCHER.EXIT_RESUMES_SPENT
    assert stopped.value.reason == "time_budget_spent"
    # The first wait (5 s) fits; the doubled second (10 s) would exceed it.
    assert elapsed == [5.0]
    records = sessions(run_dir)
    assert records[-1]["reason"] == "time_budget_spent"
    assert len(launches(run_dir)) == 2


def test_a_long_run_starts_a_new_series_at_the_short_delay(
    run_dir: Path, fake: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    clock = FakeClock(monkeypatch)
    reader = ScriptedReader(wf("active"))
    launcher = LAUNCHER.Launcher(
        LAUNCHER.parse_args(
            start_args(
                run_dir,
                fake,
                "claude-code",
                "--max-resumes",
                "3",
                "--resume-delay-s",
                "5",
                "--resume-series-reset-s",
                "30",
            )
        ),
        reader,
    )
    real_run_child = launcher.run_child

    def run_child(launch: Any, action: str, resumes: int) -> Any:
        code = real_run_child(launch, action, resumes)
        if resumes == 1:
            # The second harness run lasts 60 s of launcher time; the fake
            # clock is frozen while the child runs, so advance it here.
            clock.now += 60.0
        return code

    delays: list[float] = []
    launcher.run_child = run_child
    launcher.sleep = delays.append
    with pytest.raises(LAUNCHER.Stop) as stopped:
        launcher.run()
    assert stopped.value.reason == "resumes_spent"
    records = sessions(run_dir)
    assert [r["event"] for r in records if r["event"] == "series_reset"]
    # 5 s, then the long run resets the series back to 5 s, then 10 s.
    assert delays == [5.0, 5.0, 10.0]
    assert [r["delay_s"] for r in records if r["event"] == "wait"] == [5.0, 5.0, 10.0]


def test_an_interrupted_backoff_wait_stops_without_another_launch(
    run_dir: Path, fake: Path
) -> None:
    reader = ScriptedReader(wf("active"))
    launcher = LAUNCHER.Launcher(
        LAUNCHER.parse_args(start_args(run_dir, fake, "claude-code", "--resume-delay-s", "1800")),
        reader,
    )

    def sleep(_: float) -> None:
        launcher.interrupted = True
        raise LAUNCHER.Stop(LAUNCHER.EXIT_INTERRUPTED, "interrupted", "interrupted")

    launcher.sleep = sleep
    with pytest.raises(LAUNCHER.Stop) as stopped:
        launcher.run()
    assert stopped.value.code == LAUNCHER.EXIT_INTERRUPTED
    assert len(launches(run_dir)) == 1
    records = sessions(run_dir)
    assert records[-1]["event"] == "wait"
    assert not [r for r in records if r["event"] == "stop"]
