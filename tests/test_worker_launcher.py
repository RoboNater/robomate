"""All worker harnesses recover exits, including an attached predecessor (#165)."""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from typing import Any

import pytest
import test_alice_launcher as alice_tests
from test_alice_launcher import (
    ID_LINES,
    IDS,
    ROOT,
    ScriptedReader,
    args,
    launches,
    sessions,
    use_harness,
)

run_dir = alice_tests.run_dir
fake = alice_tests.fake

SPEC = importlib.util.spec_from_file_location(
    "worker_launcher", ROOT / "scripts/worker-launcher.py"
)
assert SPEC and SPEC.loader
WORKER = importlib.util.module_from_spec(SPEC)
sys.modules["worker_launcher"] = WORKER
SPEC.loader.exec_module(WORKER)


def wf(status: str = "active", instance: str | None = None, agent_status: str = "busy") -> Any:
    return "hub-1", {
        "id": "wf-1",
        "status": status,
        "agent": {
            "status": agent_status,
            "alive": agent_status != "lost",
            "current_task": "task-held",
            "activity": {"instance": instance},
        },
    }


def worker_args(directory: Path, script: Path, harness: str, *extra: str) -> list[str]:
    return args(
        directory,
        script,
        harness,
        "--agent",
        "bob",
        "--telemetry",
        str(directory / "bob-telemetry.jsonl"),
        "--predecessor-poll-s",
        "0.01",
        *extra,
    )


@pytest.mark.parametrize("harness", list(IDS))
@pytest.mark.parametrize("detaches", [True, False], ids=["detaches", "lost-predecessor"])
def test_holding_a_task_resumes_the_exact_conversation(
    run_dir: Path, fake: Path, monkeypatch: pytest.MonkeyPatch, harness: str, detaches: bool
) -> None:
    use_harness(harness, monkeypatch)
    # First exit still has the task. The second process records a release.
    fake.write_text(
        fake.read_text().replace(
            'sys.exit(int(os.environ.get("FAKE_EXIT", "0")))',
            """
with open(os.environ["FAKE_LOG"], encoding="utf-8") as log:
    count = len(log.readlines())
if count == 2:
    with open(os.environ["FAKE_TELEMETRY"], "a", encoding="utf-8") as stream:
        stream.write(json.dumps({"event": "tool_call", "phase": "success",
            "tool": "await_assignment", "outcome": "release"}) + "\\n")
""",
        )
    )
    monkeypatch.setenv("FAKE_TELEMETRY", str(run_dir / "bob-telemetry.jsonl"))
    predecessor = wf() if detaches else wf(instance="old-instance")
    reader = ScriptedReader(
        wf(), wf(), predecessor, predecessor, predecessor, wf(agent_status="lost")
    )
    assert (
        WORKER.main(
            worker_args(run_dir, fake, harness, "--prompt", str(run_dir / "alice.prompt.md")),
            reader,
        )
        == 0
    )
    runs = launches(run_dir)
    assert len(runs) == 2
    conversation = sessions(run_dir)[-1]["conversation_id"]
    assert conversation in runs[1]["argv"]
    if harness != "claude-code":
        assert conversation == IDS[harness]
    prompt = runs[1]["stdin"] if harness == "codex" else runs[1]["argv"][-1]
    assert prompt == WORKER.CONTINUE_PROMPT
    assert "failed or was canceled" in prompt and "await_assignment" in prompt
    records = sessions(run_dir)
    assert [r["event"] for r in records].count("exit") == 2
    assert records[-1]["reason"] == "released"
    assert any(r["event"] == "predecessor_wait" for r in records) != detaches


@pytest.mark.parametrize("harness", list(IDS))
@pytest.mark.parametrize("stop", ["released", "done", "paused", "limit", "attached", "no-id"])
def test_stop_conditions_never_launch_a_successor(
    run_dir: Path, fake: Path, monkeypatch: pytest.MonkeyPatch, harness: str, stop: str
) -> None:
    use_harness(harness, monkeypatch)
    script = [wf(), wf()]
    expected = WORKER.common.EXIT_CANNOT_RESUME
    if stop == "released":
        script += [wf(agent_status="released")]
        expected = 0
    elif stop in ("done", "paused"):
        script += [wf(status=stop)]
        expected = 0 if stop == "done" else WORKER.common.EXIT_PAUSED
    elif stop == "limit":
        script += [wf()]
        expected = WORKER.common.EXIT_RESUMES_SPENT
    elif stop == "attached":
        script += [wf(instance="orphan")]
    else:
        if harness == "claude-code":
            pytest.skip("Claude's conversation ID is chosen before launch")
        monkeypatch.setenv("FAKE_ID_LINE", "no conversation ID")
        script += [wf()]
    extra = ["--prompt", str(run_dir / "alice.prompt.md"), "--predecessor-wait-s", "0"]
    if stop == "limit":
        extra += ["--max-resumes", "0"]
    assert (
        WORKER.main(worker_args(run_dir, fake, harness, *extra), ScriptedReader(*script))
        == expected
    )
    assert len(launches(run_dir)) == 1


@pytest.mark.parametrize("harness", list(IDS))
def test_manual_resume_uses_manual_prompt_then_fixed_continuation(
    run_dir: Path, fake: Path, monkeypatch: pytest.MonkeyPatch, harness: str
) -> None:
    use_harness(harness, monkeypatch)
    reader = ScriptedReader(wf(), wf(), wf(), wf(), wf(), wf(status="done"))
    assert (
        WORKER.main(
            worker_args(
                run_dir,
                fake,
                harness,
                "--resume-session",
                "conv-given",
                "--resume-prompt",
                str(run_dir / "resume-alice.prompt.md"),
            ),
            reader,
        )
        == 0
    )
    runs = launches(run_dir)
    assert len(runs) == 2 and all("conv-given" in r["argv"] for r in runs)
    text = runs[1]["stdin"] if harness == "codex" else runs[1]["argv"][-1]
    assert text == WORKER.CONTINUE_PROMPT


def test_opencode_ignores_the_robomate_session_id() -> None:
    assert WORKER.common.conversation_in("opencode", '{"session_id": "robomate-id"}') is None
    assert WORKER.common.conversation_in("opencode", ID_LINES["opencode"]) == IDS["opencode"]


def test_stale_release_does_not_stop_a_new_launcher(run_dir: Path) -> None:
    telemetry = run_dir / "bob-telemetry.jsonl"
    telemetry.write_text(
        json.dumps(
            {
                "event": "tool_call",
                "phase": "success",
                "tool": "await_assignment",
                "outcome": "release",
            }
        )
        + "\n"
    )
    assert WORKER.released(telemetry, 0)
    assert not WORKER.released(telemetry, WORKER.telemetry_size(telemetry))


def test_a_worker_can_start_before_alice_initializes_the_workflow(
    run_dir: Path, fake: Path
) -> None:
    reader = ScriptedReader(("hub-1", None), ("hub-1", None), wf(status="done"))
    assert (
        WORKER.main(
            worker_args(run_dir, fake, "claude-code", "--prompt", str(run_dir / "alice.prompt.md")),
            reader,
        )
        == 0
    )
    assert len(launches(run_dir)) == 1


@pytest.mark.parametrize("harness", list(IDS))
@pytest.mark.parametrize("stop", ["done", "released", "paused", "other-hub"])
def test_stop_while_waiting_for_an_attached_predecessor(
    run_dir: Path, fake: Path, monkeypatch: pytest.MonkeyPatch, harness: str, stop: str
) -> None:
    use_harness(harness, monkeypatch)
    if stop == "other-hub":
        hub, state = wf()
        terminal = "other-hub", state
        expected = WORKER.common.EXIT_HUB_UNREADABLE
    elif stop == "released":
        terminal = wf(agent_status="released")
        expected = 0
    else:
        terminal = wf(status=stop)
        expected = 0 if stop == "done" else WORKER.common.EXIT_PAUSED
    reader = ScriptedReader(
        wf(), wf(), wf(instance="old"), wf(instance="old"), wf(instance="old"), terminal
    )
    argv = worker_args(
        run_dir, fake, harness, "--prompt", str(run_dir / "alice.prompt.md"), "--read-retries", "1"
    )
    assert WORKER.main(argv, reader) == expected
    assert len(launches(run_dir)) == 1
    assert any(r["event"] == "predecessor_wait" for r in sessions(run_dir))


@pytest.mark.parametrize("harness", list(IDS))
def test_worker_backoff_doubles_and_caps(
    run_dir: Path, fake: Path, monkeypatch: pytest.MonkeyPatch, harness: str
) -> None:
    use_harness(harness, monkeypatch)
    launcher = WORKER.WorkerLauncher(
        WORKER.parse_args(
            worker_args(
                run_dir,
                fake,
                harness,
                "--prompt",
                str(run_dir / "alice.prompt.md"),
                "--max-resumes",
                "4",
                "--resume-delay-s",
                "5",
                "--resume-max-delay-s",
                "12",
            )
        ),
        ScriptedReader(wf()),
    )
    delays: list[float] = []
    launcher.sleep = delays.append
    with pytest.raises(WORKER.common.Stop) as stopped:
        launcher.run()
    assert stopped.value.reason == "resumes_spent"
    assert delays == [5, 10, 12, 12]
    assert len(launches(run_dir)) == 5


def test_predecessor_wait_cannot_outlast_the_resume_budget(
    run_dir: Path, fake: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    now = [1000.0]
    monkeypatch.setattr(WORKER.common.time, "monotonic", lambda: now[0])
    launcher = WORKER.WorkerLauncher(
        WORKER.parse_args(
            worker_args(
                run_dir,
                fake,
                "claude-code",
                "--prompt",
                str(run_dir / "alice.prompt.md"),
                "--resume-total-s",
                "1",
                "--predecessor-poll-s",
                "2",
            )
        ),
        ScriptedReader(wf(), wf(), wf(instance="orphan")),
    )
    launcher.sleep = lambda seconds: now.__setitem__(0, now[0] + seconds)
    with pytest.raises(WORKER.common.Stop) as stopped:
        launcher.run()
    assert stopped.value.reason == "time_budget_spent"
    assert len(launches(run_dir)) == 1 and now[0] == 1001.0


def test_worker_reader_retains_detached_and_attached_state(
    run_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    agent: dict[str, Any] = {
        "name": "bob",
        "status": "busy",
        "alive": True,
        "activity": {"instance": "old-instance"},
    }
    calls: list[str] = []

    def call(self: Any, method: str) -> dict[str, Any]:
        calls.append(method)
        if method == "hub.info":
            return {"hub_id": "hub-1"}
        return {"workflow": {"id": "wf-1", "status": "active"}, "agents": [agent]}

    monkeypatch.setattr(WORKER.common.HubReader, "call", call)
    reader = WORKER.WorkerReader("http://stub", run_dir / "token", "bob")
    assert reader.read()[1]["agent"]["activity"]["instance"] == "old-instance"
    agent["activity"] = {"instance": None}
    assert reader.read()[1]["agent"]["activity"]["instance"] is None
    assert calls == ["hub.info", "hub.status"] * 2
    del agent["activity"]["instance"]
    with pytest.raises(ValueError, match="malformed agent"):
        reader.read()


@pytest.mark.parametrize("harness", list(IDS))
def test_worker_recovers_a_hub_restart_longer_than_five_reads(
    run_dir: Path, fake: Path, monkeypatch: pytest.MonkeyPatch, harness: str
) -> None:
    use_harness(harness, monkeypatch)
    reader = ScriptedReader(*([OSError("hub restarting")] * 8), wf(), wf(), wf(status="done"))
    assert (
        WORKER.main(
            worker_args(
                run_dir,
                fake,
                harness,
                "--prompt",
                str(run_dir / "alice.prompt.md"),
                "--read-retry-delay-s",
                "0",
            ),
            reader,
        )
        == 0
    )
    assert reader.reads == 11 and len(launches(run_dir)) == 1
