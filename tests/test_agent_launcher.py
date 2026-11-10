"""The joined agents' launcher: start, resume, ask and Denise's doctor entry (#247).

The fake harness of the prepared launchers' tests stands in for each CLI here
too, extended to record its working directory and to stay running until a
test kills it. The hub is a reader whose answers each test scripts; its
`read()` is the real bearer-only parser. The hub's checkout is a git checkout
with legacy state, so `--hub <checkout>` finds the state directory and
`sessions/` locally, and its URL answers nothing: no test needs HTTP.
"""

from __future__ import annotations

import importlib.util
import json
import os
import signal
import subprocess
import sys
import threading
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import fake_harness
import pytest
from fake_harness import (
    SUPERVISED_MAX_RESUMES,
    attempts,
    bounded,
    exit_codes,
    live_own_pids,
)
from test_alice_launcher import ID_LINES, IDS, LAUNCHER, ROOT
from timing import wait_until_sync

# One shared autouse fixture (fake_harness.py), re-exported here by alias so
# ruff does not treat the pytest fixture as an unused import (#464 r1-3).
fake_supervision = fake_harness.fake_supervision

SPEC = importlib.util.spec_from_file_location(
    "agent_launcher_cli", ROOT / "scripts/agent-launcher.py"
)
assert SPEC and SPEC.loader
CLI = importlib.util.module_from_spec(SPEC)
sys.modules["agent_launcher_cli"] = CLI
SPEC.loader.exec_module(CLI)
WORKER = sys.modules["agent_worker"]

HUB = "hub-1"
RUN = "20261109_abcdef12"
WORKFLOW = "wf-1"
HARNESSES = ("claude-code", "codex", "opencode", "antigravity", "devin")

FAKE_HARNESS = """\
import json, os, sys, time
_pids = os.environ.get("FAKE_PIDS")
if _pids:
    with open(_pids, "a", encoding="utf-8") as _stream:
        _stream.write(f"{os.getpid()}\\t{sys.argv[0]}\\n")
argv = sys.argv[1:]
stdin = sys.stdin.read() if argv and argv[-1] == "-" else None
keys = ("FAKE_MARK", "OPENCODE_CONFIG_CONTENT", "TMPDIR", "ROBOMATE_HUB_URL", "ROBOMATE_TOKEN",
        "ROBOMATE_TOKEN_FILE", "CODEX_HOME", "CLAUDE_CONFIG_DIR")
record = {"argv": argv, "stdin": stdin, "cwd": os.getcwd(),
          "env": {k: os.environ.get(k) for k in keys}}
log = os.environ["FAKE_LOG"]
count = 0
if os.path.exists(log):
    with open(log, encoding="utf-8") as stream:
        count = len(stream.readlines())
with open(log, "a", encoding="utf-8") as stream:
    stream.write(json.dumps(record) + "\\n")
export = os.environ.get("FAKE_EXPORT_ID")
if export and "--export" in argv:
    with open(argv[argv.index("--export") + 1], "w", encoding="utf-8") as stream:
        json.dump({"schema_version": "ATIF-v1.2", "session_id": export, "steps": []}, stream)
rollout = os.environ.get("FAKE_CODEX_ROLLOUT")
if rollout:
    # Codex's own session store: rollout-<time>-<id>.jsonl, working directory first.
    store = os.path.join(os.environ["CODEX_HOME"], "sessions", "2026", "11", "09")
    os.makedirs(store, exist_ok=True)
    name = os.path.join(store, "rollout-2026-11-09T00-00-00-" + rollout + ".jsonl")
    with open(name, "w", encoding="utf-8") as stream:
        named = os.environ.get("FAKE_CODEX_META_ID", rollout)
        meta = {"type": "session_meta", "payload": {"id": named, "cwd": os.getcwd()}}
        stream.write(json.dumps(meta) + "\\n")
appended = os.environ.get("FAKE_CODEX_APPEND")
if appended:
    # An earlier conversation's rollout grows while this harness runs.
    with open(appended, "a", encoding="utf-8") as stream:
        stream.write(json.dumps({"type": "event_msg"}) + "\\n")
line = os.environ.get("FAKE_ID_LINE")
if line:
    print(line, flush=True)
plan = json.loads(os.environ.get("FAKE_PLAN", "[]"))
step = plan[count] if count < len(plan) else "exit:0"
if step == "hang":
    with open(os.environ["FAKE_PID"], "w", encoding="utf-8") as stream:
        stream.write(str(os.getpid()))
    time.sleep(600)
sys.exit(int(step.split(":")[1]))
"""


def git(cwd: Path, *words: str) -> str:
    return subprocess.run(
        ["git", "-C", str(cwd), *words], check=True, capture_output=True, text=True
    ).stdout.strip()


@pytest.fixture
def area(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Path]:
    """A hub checkout with legacy state, an agent worktree, and the fake harness outside both."""

    for name in (
        "ROBOMATE_HUB_URL",
        "ROBOMATE_TOKEN",
        "ROBOMATE_TOKEN_FILE",
        "FAKE_ID_LINE",
        "FAKE_CODEX_ROLLOUT",
        "FAKE_CODEX_META_ID",
        "FAKE_CODEX_APPEND",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.delenv("FAKE_EXPORT_ID", raising=False)
    monkeypatch.delenv("FAKE_PLAN", raising=False)
    checkout = tmp_path / "hub checkout"
    checkout.mkdir()
    git(checkout, "init", "-q")
    git(checkout, "remote", "add", "origin", "https://github.com/example/target.git")
    state = checkout / ".robomate"
    state.mkdir()
    (state / "hub.json").write_text(
        json.dumps({"hub_id": HUB, "name": "hub", "url": "http://127.0.0.1:9"}), encoding="utf-8"
    )
    (state / "token").write_text("secret-token-value\n", encoding="utf-8")
    worktree = tmp_path / "bob worktree"
    worktree.mkdir()
    harness_dir = tmp_path / "fake harness"
    harness_dir.mkdir()
    fake = harness_dir / "fake harness.py"
    fake.write_text(FAKE_HARNESS, encoding="utf-8")
    monkeypatch.setenv("FAKE_LOG", str(harness_dir / "launches.jsonl"))
    monkeypatch.setenv("FAKE_PID", str(harness_dir / "pid"))
    monkeypatch.setenv("FAKE_MARK", "kept")
    return {
        "root": tmp_path,
        "checkout": checkout,
        "state": state,
        "sessions": state / "sessions",
        "worktree": worktree,
        "fake": fake,
        "harness_dir": harness_dir,
    }


def programs(area: dict[str, Path]) -> dict[str, list[str]]:
    return {name: [sys.executable, str(area["fake"])] for name in HARNESSES}


def use_harness(harness: str, monkeypatch: pytest.MonkeyPatch) -> None:
    if harness in ID_LINES:
        monkeypatch.setenv("FAKE_ID_LINE", ID_LINES[harness])
    elif harness == "devin":
        monkeypatch.setenv("FAKE_EXPORT_ID", str(IDS["devin"]))


def launches(area: dict[str, Path]) -> list[dict[str, Any]]:
    path = area["harness_dir"] / "launches.jsonl"
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def rows(area: dict[str, Path], agent: str = "bob") -> list[dict[str, Any]]:
    path = area["sessions"] / f"{agent}.jsonl"
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def workflow(status: str = "active", **extra: Any) -> dict[str, Any]:
    return {"id": WORKFLOW, "run_id": RUN, "status": status, **extra}


def agent_row(status: str = "busy", context: str = "c1", **extra: Any) -> dict[str, Any]:
    return {
        "name": "bob",
        "status": status,
        "alive": False,
        "activity": {"instance": None},
        "context_id": context,
        **extra,
    }


class FakeHub(WORKER.WorkerReader):  # type: ignore[misc,name-defined]
    """Answers the bearer-only RPCs from ``answer(method)``; the real `read()` parses them."""

    def __init__(self, answer: Callable[[str], dict[str, Any]], agent: str = "bob") -> None:
        super().__init__("http://127.0.0.1:9", Path("unused"), agent)
        self.answer = answer
        self.methods: list[str] = []

    def call(self, method: str) -> dict[str, Any]:
        self.methods.append(method)
        result = self.answer(method)
        if method == "hub.status":
            agents = result.setdefault("agents", [])
            self.current_agent = next((a for a in agents if a.get("name") == self.agent), None)
        return result


def after_launches(
    area: dict[str, Path], *phases: dict[str, Any]
) -> Callable[[str], dict[str, Any]]:
    """Hub status by how many harness launches have happened: phase N after N launches."""

    def answer(method: str) -> dict[str, Any]:
        if method == "hub.info":
            return {"hub_id": HUB}
        phase = phases[min(len(launches(area)), len(phases) - 1)]
        copied: dict[str, Any] = json.loads(json.dumps(phase))
        return copied

    return answer


def files(root: Path, skip: Path) -> dict[str, float]:
    return {
        str(path): path.stat().st_mtime_ns
        for path in root.rglob("*")
        if path.is_file() and skip not in path.parents and ".git" not in path.parts
    }


def start_words(
    area: dict[str, Path], agent: str, role: str, harness: str, *extra: str
) -> list[str]:
    # A broken fake child must not loop through the launcher's unlimited
    # production resume count / ~12-hour budget (#464). `bounded` keeps the
    # production defaults unchanged and gives every scenario a finite, explicit
    # count. `invoke` below also bounds the inline start/resume word lists.
    return bounded(
        [
            "start",
            agent,
            "--role",
            role,
            "--harness",
            harness,
            "--hub",
            str(area["checkout"]),
            "--resume-delay-s",
            "0",
            "--read-retry-delay-s",
            "0",
            "--predecessor-poll-s",
            "0.01",
            *extra,
        ],
        total=True,
    )


def invoke(words: list[str], **kwargs: Any) -> int:
    """Bound every supervised start/resume command; leave ask/doctor untouched."""
    if words and words[0] in ("start", "resume"):
        words = bounded(words, total=True)
    return int(CLI.main(words, **kwargs))


def workspace_of(
    area: dict[str, Path], calls: list[str] | None = None
) -> Callable[[str, str | None], Path]:
    def workspace(agent: str, selector: str | None) -> Path:
        if calls is not None:
            calls.append(agent)
        assert selector == str(area["checkout"])
        return area["worktree"]

    return workspace


@pytest.mark.parametrize("harness", HARNESSES)
@pytest.mark.parametrize("role", ["orchestrator", "implementer"])
def test_start_sends_the_join_line_and_resumes_with_takeover(
    area: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
    harness: str,
    role: str,
    fake_supervision: Any,
) -> None:
    use_harness(harness, monkeypatch)
    monkeypatch.setenv("ROBOMATE_HUB_URL", "http://another-hub:1")
    monkeypatch.setenv("ROBOMATE_TOKEN", "ambient-token")
    agent = "alice" if role == "orchestrator" else "bob"
    monkeypatch.setenv("FAKE_PLAN", json.dumps(["exit:1", "exit:0"]))
    active = {"workflow": workflow(), "agents": [agent_row(context="c1")]}
    final = (
        {"workflow": workflow("done"), "agents": []}
        if role == "orchestrator"
        else {"workflow": workflow(), "agents": [agent_row("released", context="c2")]}
    )
    hub = FakeHub(after_launches(area, active, active, final), agent)
    before = files(area["root"], area["harness_dir"])
    calls: list[str] = []

    code = invoke(
        start_words(area, agent, role, harness, "--model", "m-1"),
        programs=programs(area),
        reader=hub,
        workspace=workspace_of(area, calls),
    )

    # Wire the diagnostic guard into a real supervision scenario (#464 r1-2):
    # a fixture that dies before its marker fails clearly here too, not only in
    # the dedicated regressions. A passing scenario has all its markers.
    fake_supervision.check(
        expect=2,
        markers=len(launches(area)),
        sessions=(area["sessions"] / f"{agent}.jsonl",),
        output="",
    )

    assert code == 0
    assert calls == [agent]
    first, second = launches(area)
    line = f"Join robomate as {agent}, {role}."
    sent = first["stdin"] if harness == "codex" else first["argv"][-1]
    assert sent == line
    for run in (first, second):
        assert Path(run["cwd"]).resolve() == area["worktree"].resolve()
        assert run["argv"][run["argv"].index("--model") : run["argv"].index("--model") + 2] == [
            "--model",
            "m-1",
        ]
        assert run["env"]["FAKE_MARK"] == "kept"
        # The worktree's binding names a local hub; an ambient selection is cleared.
        assert run["env"]["ROBOMATE_HUB_URL"] is None and run["env"]["ROBOMATE_TOKEN"] is None
        # No rendered config, prompt file or MCP config path is passed.
        assert not any("--mcp-config" in word or "prompt.md" in word for word in run["argv"])
    resumed = second["stdin"] if harness == "codex" else second["argv"][-1]
    assert f'join(name="{agent}", role="{role}", takeover=true)' in resumed
    conversation = IDS[harness] or rows(area, agent)[0]["conversation_id"]
    assert conversation in second["argv"]
    # Nothing outside the hub's sessions/ was written or changed.
    after = files(area["root"], area["harness_dir"])
    changed = {path for path in after if before.get(path) != after[path]}
    assert changed and all(Path(path).parent == area["sessions"] for path in changed)
    events = [row["event"] for row in rows(area, agent)]
    assert events[0] == "start" and "resume" in events and events[-1] == "stop"
    launch_rows = [row for row in rows(area, agent) if row["event"] in ("start", "resume")]
    assert all(row["run_id"] == RUN and row["role"] == role for row in launch_rows)
    assert launch_rows[0]["workdir"] == str(area["worktree"].resolve())


def test_grants_are_flags_for_the_measured_harnesses(area: dict[str, Path], tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "-q")
    git(
        repo,
        "-c",
        "user.name=t",
        "-c",
        "user.email=t@example.com",
        "commit",
        "-q",
        "--allow-empty",
        "-m",
        "c",
    )
    linked = tmp_path / "linked bob"
    git(repo, "worktree", "add", "-q", "--detach", str(linked))
    common = Path(git(linked, "rev-parse", "--path-format=absolute", "--git-common-dir"))
    own = git(linked, "rev-parse", "--absolute-git-dir")

    words = CLI.grants("codex", linked, area["state"] / "token")
    roots = json.loads(words[-1].split("=", 1)[1])
    assert words[:4] == [
        "-c",
        "sandbox_mode=workspace-write",
        "-c",
        "sandbox_workspace_write.network_access=true",
    ]
    assert roots[:4] == [own, *(str(common / leaf) for leaf in ("objects", "refs", "logs"))]
    assert str(common) not in roots

    settings = CLI.grants("claude-code", area["checkout"], area["state"] / "token")
    sandbox = json.loads(settings[1])["sandbox"]
    assert settings[0] == "--settings"
    assert sandbox["filesystem"]["denyRead"] == [str(area["state"])]
    assert sandbox["network"]["allowedDomains"] == ["github.com", "api.github.com"]
    assert CLI.grants("opencode", linked, None) == []


def test_kill_mid_task_resumes_the_same_conversation_with_backoff(
    area: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FAKE_PLAN", json.dumps(["hang", "exit:0"]))
    # The killed harness's bridge is still attached: the successor's join with
    # takeover supersedes it and keeps the task, so the launcher does not wait.
    orphan = {"alive": True, "activity": {"instance": "orphan-bridge"}}
    busy = {"workflow": workflow(), "agents": [agent_row(context="c1") | orphan]}
    released = {"workflow": workflow(), "agents": [agent_row("released", context="c2")]}
    detached = {"workflow": workflow(), "agents": [agent_row(context="c1")]}
    hub = FakeHub(after_launches(area, detached, busy, released))

    def kill() -> None:
        pid_file = area["harness_dir"] / "pid"
        wait_until_sync(pid_file.exists, "the fake harness hangs mid-task")
        pid = int(wait_until_sync(lambda: pid_file.read_text(encoding="utf-8"), "its pid"))
        os.kill(pid, getattr(signal, "SIGKILL", signal.SIGTERM))

    killer = threading.Thread(target=kill)
    killer.start()
    code = invoke(
        start_words(area, "bob", "implementer", "claude-code")
        + ["--resume-delay-s", "0.05", "--predecessor-wait-s", "0"],
        programs=programs(area),
        reader=hub,
        workspace=workspace_of(area),
    )
    killer.join()

    assert code == 0
    first, second = launches(area)
    conversation = first["argv"][first["argv"].index("--session-id") + 1]
    assert second["argv"][-4:-2] == ["--resume", conversation]
    assert second["argv"][-1] == CLI.continuation("bob", "implementer")
    log = rows(area)
    assert [row["event"] for row in log] == ["start", "exit", "wait", "resume", "exit", "stop"]
    assert log[1]["exit_code"] != 0
    assert log[2]["delay_s"] == 0.05
    assert log[-1]["reason"] == "released"
    assert all(row["run_id"] == RUN and row["conversation_id"] == conversation for row in log)


def test_an_earlier_release_and_finished_run_do_not_stop_a_new_start(
    area: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FAKE_PLAN", json.dumps(["exit:0"]))
    earlier = {"workflow": workflow("done"), "agents": [agent_row("released", context="old")]}
    joined = {"workflow": workflow(), "agents": [agent_row("released", context="new")]}
    hub = FakeHub(after_launches(area, earlier, joined))

    code = invoke(
        start_words(area, "bob", "implementer", "claude-code"),
        programs=programs(area),
        reader=hub,
        workspace=workspace_of(area),
    )

    assert code == 0
    assert len(launches(area)) == 1
    log = rows(area)
    assert log[0]["workflow_id"] is None  # the finished run was never pinned
    assert log[-1]["reason"] == "released" and log[-1]["run_id"] == RUN


@pytest.mark.parametrize("content", [None, ""])
def test_resume_with_an_empty_log_refuses(area: dict[str, Path], content: str | None) -> None:
    if content is not None:
        area["sessions"].mkdir()
        (area["sessions"] / "bob.jsonl").write_text(content, encoding="utf-8")
    hub = FakeHub(lambda method: pytest.fail(f"read the hub: {method}"))

    code = invoke(
        ["resume", "bob", "--hub", str(area["checkout"])], programs=programs(area), reader=hub
    )

    assert code == LAUNCHER.EXIT_CANNOT_RESUME
    assert launches(area) == []
    assert hub.methods == []


def saved_log(
    area: dict[str, Path], conversation: str, started: datetime, exited: datetime
) -> None:
    area["sessions"].mkdir(exist_ok=True)
    common = {
        "agent": "bob",
        "harness": "claude-code",
        "role": "implementer",
        "conversation_id": conversation,
        "hub_id": HUB,
        "workflow_id": None,
        "run_id": None,
    }
    lines = [
        common
        | {
            "event": "start",
            "timestamp": started.isoformat(),
            "pid": 999999,
            "model": "",
            "effort": "",
            "workdir": str(area["worktree"]),
            "interactive": False,
        },
        common | {"event": "exit", "timestamp": exited.isoformat(), "exit_code": 1},
    ]
    (area["sessions"] / "bob.jsonl").write_text(
        "".join(json.dumps(line) + "\n" for line in lines), encoding="utf-8"
    )


def test_resume_of_a_log_from_before_the_workflow_takes_the_run_from_the_hub(
    area: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    conversation = "0b8a51c4-6a7e-4b6f-9c55-0c0ffee00001"
    now = datetime.now(UTC)
    saved_log(area, conversation, now - timedelta(minutes=10), now - timedelta(minutes=1))
    created = (now - timedelta(minutes=5)).isoformat()
    live = workflow(created=created)
    resumed = {"workflow": live, "agents": [agent_row()]}
    released = {"workflow": live, "agents": [agent_row("released", context="c2")]}
    status = after_launches(area, resumed, released)

    def answer(method: str) -> dict[str, Any]:
        if method == "hub.snapshot":
            return {"hub_id": HUB, "workflow": live, "tasks": [], "taken_at": now.isoformat()}
        if method == "hub.questions":
            return {"questions": []}
        return status(method)

    code = invoke(
        ["resume", "bob", "--hub", str(area["checkout"]), "--read-retry-delay-s", "0"],
        programs=programs(area),
        reader=FakeHub(answer),
    )

    assert code == 0
    (launch,) = launches(area)
    assert launch["argv"][-4:] == [
        "--resume",
        conversation,
        "-p",
        CLI.continuation("bob", "implementer"),
    ]
    assert Path(launch["cwd"]).resolve() == area["worktree"].resolve()
    log = rows(area)
    recovery = next(row for row in log if row["event"] == "recovery")
    assert recovery["workflow_id"] == WORKFLOW
    manual = next(row for row in log if row["event"] == "manual_resume")
    assert manual["run_id"] == RUN and manual["workflow_id"] == WORKFLOW
    # The agent is never pointed at a file in the hub's state; the record stays there.
    assert not any("resume.md" in word for word in launch["argv"])
    assert "Before-snapshot" in (area["sessions"] / "bob-resume.md").read_text(encoding="utf-8")


@pytest.mark.parametrize("safe", [False, True])
def test_interactive_start_does_not_resume_but_records_the_session(
    area: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
    safe: bool,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("FAKE_PLAN", json.dumps(["exit:3"]))
    hub = FakeHub(after_launches(area, {"workflow": workflow(), "agents": []}))
    words = start_words(area, "bob", "implementer", "codex", "--interactive")
    words += ["--safe-mode"] if safe else []

    code = invoke(
        [*words, "--harness-args", "--extra", "x y"],
        programs=programs(area),
        reader=hub,
        workspace=workspace_of(area),
    )

    assert code == 3
    (launch,) = launches(area)
    assert launch["argv"][-3:] == ["--extra", "x y", "Join robomate as bob, implementer."]
    assert ("--approve-for-me" in launch["argv"]) is not safe
    assert (launch["argv"][2:4] == ["-a", "on-request"]) is safe
    assert ("safe-mode" in capsys.readouterr().out) is safe
    log = rows(area)
    assert [row["event"] for row in log] == ["start", "exit"]
    assert all(row["interactive"] and row["run_id"] == RUN for row in log)
    assert log[1]["exit_code"] == 3


@pytest.mark.parametrize("harness", ["claude-code", "codex", "opencode"])
def test_ask_opens_the_saved_conversation_without_hub_tools(
    area: dict[str, Path], harness: str
) -> None:
    conversation = IDS[harness] or "0b8a51c4-6a7e-4b6f-9c55-0c0ffee00001"
    area["sessions"].mkdir()
    row = {
        "agent": "bob",
        "harness": harness,
        "role": "reviewer",
        "event": "start",
        "timestamp": datetime.now(UTC).isoformat(),
        "conversation_id": conversation,
        "workdir": str(area["worktree"]),
    }
    log = area["sessions"] / "bob.jsonl"
    log.write_text(json.dumps(row) + "\n", encoding="utf-8")
    before = log.read_bytes()

    code = invoke(["ask", "bob", "--hub", str(area["checkout"])], programs=programs(area))

    assert code == 0
    (launch,) = launches(area)
    assert conversation in launch["argv"]
    if harness == "claude-code":
        assert "--strict-mcp-config" in launch["argv"] and "--mcp-config" not in launch["argv"]
    elif harness == "codex":
        assert "mcp_servers.robomate.enabled=false" in launch["argv"]
    else:
        content = json.loads(launch["env"]["OPENCODE_CONFIG_CONTENT"])
        assert content["mcp"] == {"robomate": {"enabled": False}}
    assert log.read_bytes() == before


@pytest.mark.parametrize("harness", ["antigravity", "devin"])
def test_ask_refuses_a_harness_whose_hub_tools_cannot_be_switched_off(
    area: dict[str, Path], harness: str
) -> None:
    area["sessions"].mkdir()
    row = {
        "agent": "bob",
        "harness": harness,
        "role": "reviewer",
        "event": "start",
        "timestamp": datetime.now(UTC).isoformat(),
        "conversation_id": IDS[harness],
        "workdir": str(area["worktree"]),
    }
    (area["sessions"] / "bob.jsonl").write_text(json.dumps(row) + "\n", encoding="utf-8")

    assert invoke(["ask", "bob", "--hub", str(area["checkout"])], programs=programs(area)) == 2
    assert launches(area) == []


@pytest.mark.parametrize("interactive", [False, True])
def test_a_remote_host_hands_its_hub_selection_to_the_joining_harness(
    area: dict[str, Path], tmp_path: Path, monkeypatch: pytest.MonkeyPatch, interactive: bool
) -> None:
    """A clone has no hub binding: the bridge finds the hub only from the forwarded selection."""

    monkeypatch.setenv("FAKE_PLAN", json.dumps(["exit:1", "exit:0"]))
    monkeypatch.setenv("ROBOMATE_HUB_URL", "http://another-hub:1")
    monkeypatch.setenv("ROBOMATE_TOKEN", "ambient-token")
    monkeypatch.setenv("ROBOMATE_TOKEN_FILE", str(tmp_path / "ambient token"))
    sessions = tmp_path / "remote sessions"
    token = area["state"] / "token"
    active = {"workflow": workflow(), "agents": [agent_row()]}
    released = {"workflow": workflow(), "agents": [agent_row("released", context="c2")]}
    hub = FakeHub(after_launches(area, active, active, released))

    def no_workspace(agent: str, selector: str | None) -> Path:
        raise AssertionError("a remote host has no local robomate workspace")

    remote = ["--hub-url", "http://hub.example:8420/", "--token-file", str(token)]
    remote += ["--sessions-dir", str(sessions), "--read-retry-delay-s", "0"]
    words = ["start", "bob", "--role", "implementer", "--harness", "claude-code", *remote]
    words += ["--workdir", str(area["worktree"]), "--predecessor-poll-s", "0.01"]
    words += ["--resume-delay-s", "0"]
    code = invoke(
        words + (["--interactive"] if interactive else []),
        programs=programs(area),
        reader=hub,
        workspace=no_workspace,
    )

    assert code == (1 if interactive else 0)
    if interactive:
        # The guarded resume on that host hands on the same selection.
        assert (
            invoke(["resume", "bob", *remote], programs=programs(area), reader=snapshotting(hub))
            == 0
        )
    assert len(launches(area)) == 2
    for run in launches(area):
        assert run["env"]["ROBOMATE_HUB_URL"] == "http://hub.example:8420"
        assert run["env"]["ROBOMATE_TOKEN_FILE"] == str(token.resolve())
        # An ambient token would take precedence over the file in the bridge.
        assert run["env"]["ROBOMATE_TOKEN"] is None
    assert (sessions / "bob.jsonl").is_file()
    assert not area["sessions"].exists()


def test_interactive_codex_start_captures_its_conversation_for_resume_and_ask(
    area: dict[str, Path], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    conversation = "019a2b3c-4d5e-6f70-8192-a3b4c5d6e7f9"
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex home"))
    monkeypatch.setenv("FAKE_CODEX_ROLLOUT", conversation)
    monkeypatch.setenv("FAKE_PLAN", json.dumps(["exit:1"]))
    active = {"workflow": workflow(), "agents": [agent_row()]}
    released = {"workflow": workflow(), "agents": [agent_row("released", context="c2")]}
    hub = FakeHub(after_launches(area, active, active, released))
    words = start_words(area, "bob", "implementer", "codex", "--interactive")

    assert invoke(words, programs=programs(area), reader=hub, workspace=workspace_of(area)) == 1

    log = rows(area)
    assert [row["event"] for row in log] == ["start", "conversation", "exit"]
    assert log[1]["conversation_id"] == conversation
    assert log[1]["conversation_source"] == "codex session store"
    monkeypatch.delenv("FAKE_CODEX_ROLLOUT")
    resume = ["resume", "bob", "--hub", str(area["checkout"]), "--read-retry-delay-s", "0"]
    assert invoke(resume, programs=programs(area), reader=snapshotting(hub)) == 0
    assert launches(area)[1]["argv"][-3:] == ["resume", conversation, "-"]
    assert invoke(["ask", "bob", "--hub", str(area["checkout"])], programs=programs(area)) == 0
    assert launches(area)[2]["argv"][-1] == conversation


@pytest.mark.parametrize("case", ["earlier", "appended", "none", "foreign"])
def test_interactive_codex_never_takes_a_session_it_did_not_create(
    area: dict[str, Path],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    case: str,
) -> None:
    """Only a rollout this launch created, naming its ID and worktree, is captured (r2-1)."""

    home = tmp_path / "codex home"
    monkeypatch.setenv("CODEX_HOME", str(home))
    monkeypatch.setenv("FAKE_PLAN", json.dumps(["exit:0"]))
    store = home / "sessions" / "2026" / "11" / "08"
    store.mkdir(parents=True)
    earlier_id = "019a2b3c-4d5e-6f70-8192-a3b4c5d6e7f0"
    earlier = store / f"rollout-2026-11-08T23-59-59-{earlier_id}.jsonl"
    if case in ("earlier", "appended"):
        # A recent conversation in the same worktree, just modified.
        meta = {"id": earlier_id, "cwd": str(area["worktree"].resolve())}
        earlier.write_text(
            json.dumps({"type": "session_meta", "payload": meta}) + "\n", encoding="utf-8"
        )
    if case == "appended":
        monkeypatch.setenv("FAKE_CODEX_APPEND", str(earlier))
    if case == "foreign":
        # A new rollout whose metadata names another session.
        monkeypatch.setenv("FAKE_CODEX_ROLLOUT", "019a2b3c-4d5e-6f70-8192-a3b4c5d6e7f1")
        monkeypatch.setenv("FAKE_CODEX_META_ID", earlier_id)
    hub = FakeHub(after_launches(area, {"workflow": workflow(), "agents": [agent_row()]}))
    words = start_words(area, "bob", "implementer", "codex", "--interactive")

    assert invoke(words, programs=programs(area), reader=hub, workspace=workspace_of(area)) == 0

    assert [row["event"] for row in rows(area)] == ["start", "exit"]
    assert all(row["conversation_id"] is None for row in rows(area))
    assert "no conversation ID was captured" in capsys.readouterr().err
    resume = ["resume", "bob", "--hub", str(area["checkout"]), "--read-retry-delay-s", "0"]
    assert invoke(resume, programs=programs(area), reader=snapshotting(hub)) == 4
    assert len(launches(area)) == 1


def snapshotting(hub: Any) -> Any:
    """The same scripted hub, also answering the recovery reads."""

    status: Callable[[str], dict[str, Any]] = hub.answer

    def answer(method: str) -> dict[str, Any]:
        if method == "hub.snapshot":
            live = status("hub.status")["workflow"]
            return {
                "hub_id": HUB,
                "workflow": live,
                "tasks": [],
                "taken_at": "2026-11-09T00:00:00Z",
            }
        if method == "hub.questions":
            return {"questions": []}
        return status(method)

    return FakeHub(answer)


@pytest.mark.parametrize("harness", ["opencode", "antigravity", "devin"])
def test_interactive_start_without_capture_warns_and_takes_the_operators_exact_id(
    area: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    harness: str,
) -> None:
    monkeypatch.setenv("FAKE_PLAN", json.dumps(["exit:0", "exit:0"]))
    active = {"workflow": workflow(), "agents": [agent_row()]}
    released = {"workflow": workflow(), "agents": [agent_row("released", context="c2")]}
    hub = FakeHub(after_launches(area, active, active, released))
    words = start_words(area, "bob", "reviewer", harness, "--interactive")

    assert invoke(words, programs=programs(area), reader=hub, workspace=workspace_of(area)) == 0

    out = capsys.readouterr()
    assert "does not tell an interactive launcher its conversation ID" in out.out
    assert "--resume-session ID" in out.out and "no conversation ID was captured" in out.err
    resume = ["resume", "bob", "--hub", str(area["checkout"]), "--read-retry-delay-s", "0"]
    # Without an ID, recovery refuses rather than guess one.
    assert invoke(resume, programs=programs(area), reader=snapshotting(hub)) == 4
    assert len(launches(area)) == 1
    exact = str(IDS[harness])
    assert invoke([*resume, "--resume-session", "not an id"], programs=programs(area)) == 2
    assert (
        invoke(
            [*resume, "--resume-session", exact], programs=programs(area), reader=snapshotting(hub)
        )
        == 0
    )
    assert exact in launches(area)[1]["argv"]
    recorded = [row for row in rows(area) if row["event"] == "conversation"]
    assert [(row["conversation_id"], row["conversation_source"]) for row in recorded] == [
        (exact, "operator")
    ]


# -- Denise's doctor entry -------------------------------------------------------------


@pytest.fixture
def run(area: dict[str, Path]) -> dict[str, Path]:
    target = area["root"] / "target repo"
    target.mkdir()
    git(target, "init", "-q")
    run_dir = area["state"] / "runs" / RUN
    run_dir.mkdir(parents=True)
    manifest = {
        "run_id": RUN,
        "workflow_id": WORKFLOW,
        "status": "active",
        "hub": {
            "hub_id": HUB,
            "name": "hub",
            "checkout": str(area["checkout"]),
            "origin": "https://user:secret@github.com/example/target.git",
            "forge": "github",
            "url": "http://127.0.0.1:9",
        },
        "agents": [{"name": "bob", "standing_role": "implementer", "harness": "codex"}],
    }
    (run_dir / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    area["sessions"].mkdir()
    (area["sessions"] / "bob.jsonl").write_text('{"agent": "bob"}\n', encoding="utf-8")
    return {"target": target, "run_dir": run_dir, "denise": run_dir / "denise"}


def no_hub() -> Any:
    return FakeHub(lambda method: pytest.fail(f"the doctor read the hub: {method}"))


@pytest.mark.parametrize("harness", ["claude", "codex"])
def test_doctor_starts_with_the_hub_down_and_no_join_mcp_or_supervision(
    area: dict[str, Path], run: dict[str, Path], harness: str
) -> None:
    team_log = (area["sessions"] / "bob.jsonl").read_bytes()
    before = files(area["root"], area["harness_dir"])

    code = invoke(
        [
            "doctor",
            "--hub",
            str(area["checkout"]),
            "--run",
            RUN,
            "--harness",
            harness,
            "--target",
            str(run["target"]),
        ],
        programs=programs(area),
        reader=no_hub(),
    )

    assert code == 0
    (launch,) = launches(area)
    assert Path(launch["cwd"]).resolve() == (run["denise"] / "workspace").resolve()
    assert (
        launch["argv"][-1]
        == f"Read {run['denise'] / 'startup.md'} and follow the instructions in it"
    )
    assert not any("Join robomate" in word or "--mcp-config" in word for word in launch["argv"])
    assert not any(
        word in launch["argv"] for word in ("-p", "--permission-mode", "--approve-for-me")
    )
    if harness == "codex":
        assert "mcp_servers={}" in launch["argv"]
    else:
        assert "--strict-mcp-config" in launch["argv"]
    granted = [
        launch["argv"][i + 1] for i, word in enumerate(launch["argv"]) if word == "--add-dir"
    ]
    assert str(run["target"].resolve()) in granted and str(run["run_dir"].resolve()) in granted
    # Everything the doctor wrote is under the selected run's denise/; team logs are untouched.
    after = files(area["root"], area["harness_dir"])
    changed = {path for path in after if before.get(path) != after[path]}
    assert changed and all(
        run["denise"].resolve() in Path(path).resolve().parents for path in changed
    )
    assert (area["sessions"] / "bob.jsonl").read_bytes() == team_log
    assert launch["env"]["TMPDIR"] == str(run["denise"] / "tmp")


def test_doctor_context_keeps_robomate_and_target_apart(
    area: dict[str, Path], run: dict[str, Path]
) -> None:
    assert (
        invoke(
            [
                "doctor",
                "--hub",
                str(area["checkout"]),
                "--run",
                RUN,
                "--harness",
                "codex",
                "--target",
                str(run["target"]),
            ],
            programs=programs(area),
            reader=no_hub(),
        )
        == 0
    )

    context = json.loads((run["denise"] / "context.json").read_text(encoding="utf-8"))
    assert context["tooling"]["root"]["path"] == str(ROOT.resolve())
    assert context["target"]["checkout"]["path"] == str(run["target"].resolve())
    assert context["target"]["origin"] == "https://github.com/example/target.git"
    assert context["run"]["run_id"] == RUN and context["run"]["hub_id"] == HUB
    modes = {root["path"]: root["mode"] for root in context["access_inventory"]["roots"]}
    assert modes[str(ROOT.resolve())] == "read"
    assert modes[str(run["target"].resolve())] == "write"
    assert "secret" not in (run["denise"] / "context.json").read_text(encoding="utf-8")


def test_doctor_keeps_earlier_reports(area: dict[str, Path], run: dict[str, Path]) -> None:
    words = ["doctor", "--hub", str(area["checkout"]), "--run", RUN, "--harness", "codex"]
    assert invoke(words, programs=programs(area), reader=no_hub()) == 0
    (first,) = (run["denise"] / "reports").iterdir()
    first.write_text("# Earlier findings\n", encoding="utf-8")

    assert invoke(words, programs=programs(area), reader=no_hub()) == 0

    reports = sorted((run["denise"] / "reports").iterdir())
    assert len(reports) == 2 and first in reports
    assert first.read_text(encoding="utf-8") == "# Earlier findings\n"


@pytest.mark.parametrize("selected", [None, "20990101_00000000", "../runs"])
def test_doctor_refuses_an_unknown_run_without_creating_one(
    area: dict[str, Path],
    run: dict[str, Path],
    selected: str | None,
    capsys: pytest.CaptureFixture[str],
) -> None:
    words = ["doctor", "--hub", str(area["checkout"]), "--harness", "codex"]
    words += ["--run", selected] if selected else []

    assert invoke(words, programs=programs(area), reader=no_hub()) == 2

    assert launches(area) == []
    assert RUN in capsys.readouterr().err
    assert sorted(p.name for p in (area["state"] / "runs").iterdir()) == [RUN]
    assert not run["denise"].exists()


def test_doctor_supports_only_its_ready_harnesses(
    area: dict[str, Path], run: dict[str, Path]
) -> None:
    with pytest.raises(SystemExit) as refused:
        invoke(["doctor", "--hub", str(area["checkout"]), "--run", RUN, "--harness", "opencode"])
    assert refused.value.code == 2
    assert launches(area) == []


@pytest.mark.parametrize("harness", ["claude", "codex"])
def test_doctor_uses_its_own_profile_and_no_inherited_hub_selection(
    area: dict[str, Path], run: dict[str, Path], monkeypatch: pytest.MonkeyPatch, harness: str
) -> None:
    for name, value in {
        "ROBOMATE_HUB_URL": "http://another-hub:1",
        "ROBOMATE_TOKEN": "ambient-token",
        "ROBOMATE_TOKEN_FILE": str(area["root"] / "ambient token"),
        "CODEX_HOME": str(area["root"] / "team codex home"),
        "CLAUDE_CONFIG_DIR": str(area["root"] / "team claude config"),
    }.items():
        monkeypatch.setenv(name, value)

    words = ["doctor", "--hub", str(area["checkout"]), "--run", RUN, "--harness", harness]
    assert invoke(words, programs=programs(area), reader=no_hub()) == 0

    (launch,) = launches(area)
    env = launch["env"]
    assert env["ROBOMATE_HUB_URL"] is None and env["ROBOMATE_TOKEN"] is None
    assert env["ROBOMATE_TOKEN_FILE"] is None
    profile = run["denise"] / "configs" / ("codex" if harness == "codex" else "claude")
    variable, other = (
        ("CODEX_HOME", "CLAUDE_CONFIG_DIR")
        if harness == "codex"
        else ("CLAUDE_CONFIG_DIR", "CODEX_HOME")
    )
    assert env[variable] == str(profile) and profile.is_dir()
    if harness == "codex":
        config = (profile / "config.toml").read_text(encoding="utf-8")
        assert "[mcp_servers" not in config and "approval" not in config.split("#")[0]
    context = json.loads((run["denise"] / "context.json").read_text(encoding="utf-8"))
    assert context["doctor"]["profile"] == str(profile)
    assert env[other] == str(
        area["root"] / ("team claude config" if other == "CLAUDE_CONFIG_DIR" else "team codex home")
    )


# -- a broken fake child (#464) --------------------------------------------------------


# An immediately failing child that records its pid (so cleanup is checkable)
# and prints a diagnostic, but dies before writing its launch marker.
IMMEDIATE_FAIL_CHILD = """\
import os, sys
_pids = os.environ.get("FAKE_PIDS")
if _pids:
    with open(_pids, "a", encoding="utf-8") as _stream:
        _stream.write(f"{os.getpid()}\\t{sys.argv[0]}\\n")
print("fake child failed before its launch marker", file=sys.stderr, flush=True)
sys.exit(7)
"""

# The real incident (#464): a malformed script (nested Python/heredoc escaping).
# Python cannot even compile it, so no child code runs, no marker is written and
# the interpreter's SyntaxError is the diagnostic evidence.
SYNTAX_ERROR_CHILD = """\
def broken(:
    pass
"""

BROKEN_FAKE_CHILDREN = {
    "immediate-fail": (IMMEDIATE_FAIL_CHILD, "fake child failed before its launch marker"),
    "syntax-error": (SYNTAX_ERROR_CHILD, "SyntaxError"),
}


@pytest.mark.parametrize("case", list(BROKEN_FAKE_CHILDREN))
def test_broken_fake_child_fails_with_finite_attempts_and_diagnostics(
    area: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
    fake_supervision: Any,
    capfd: pytest.CaptureFixture[str],
    case: str,
) -> None:
    """#464: a child that dies before its marker fails clearly, and never loops."""
    child, evidence = BROKEN_FAKE_CHILDREN[case]
    area["fake"].write_text(child, encoding="utf-8")
    monkeypatch.setenv("FAKE_PLAN", json.dumps(["exit:0"]))
    # A single scripted phase stays active/busy however many launches happen,
    # so only the finite resume count can end the supervision.
    active = {"workflow": workflow(), "agents": [agent_row()]}
    hub = FakeHub(after_launches(area, active))

    code = invoke(
        start_words(area, "bob", "implementer", "claude-code"),
        programs=programs(area),
        reader=hub,
        workspace=workspace_of(area),
    )
    output = capfd.readouterr().out
    sessions = (area["sessions"] / "bob.jsonl",)

    # One start plus the bounded resumes, then EXIT_RESUMES_SPENT: counted as a
    # finite attempt count, never asserted as a short wall-clock duration.
    assert code == LAUNCHER.EXIT_RESUMES_SPENT
    assert attempts(*sessions) == SUPERVISED_MAX_RESUMES + 1
    assert launches(area) == []
    codes = exit_codes(*sessions)
    assert len(codes) == SUPERVISED_MAX_RESUMES + 1 and all(code != 0 for code in codes)

    # The failure is clear and preserves the child's exit code and output.
    with pytest.raises(AssertionError) as raised:
        fake_supervision.check(
            expect=1, markers=len(launches(area)), sessions=sessions, output=output
        )
    message = str(raised.value)
    assert "before writing its launch marker" in message
    assert evidence in message

    # Cleanup leaves only this test's own children, all of them gone.
    assert live_own_pids(fake_supervision.pids) == []


# A child that hangs on every attempt and never writes its launch marker. The
# watchdog must keep sweeping across automatic resumes, not fire once (#464
# r1-1); otherwise the resumed child hangs with no watchdog left.
HANG_FAKE_CHILD = """\
import os, sys, time
_pids = os.environ.get("FAKE_PIDS")
if _pids:
    with open(_pids, "a", encoding="utf-8") as _stream:
        _stream.write(f"{os.getpid()}\\t{sys.argv[0]}\\n")
print("fake child hanging before its launch marker", file=sys.stderr, flush=True)
time.sleep(10**6)
"""


def test_hung_child_on_every_attempt_is_bounded_and_cleaned_up(
    area: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
    fake_supervision: Any,
) -> None:
    """#464 r1-1: the LIVENESS_S watchdog keeps sweeping across resumes."""
    # Shorten this instance's hang bound so the test is fast; the sweep reads
    # liveness_s fresh each poll, so the change takes effect mid-run.
    fake_supervision.liveness_s = 0.3
    area["fake"].write_text(HANG_FAKE_CHILD, encoding="utf-8")
    monkeypatch.setenv("FAKE_PLAN", json.dumps(["exit:0"]))
    active = {"workflow": workflow(), "agents": [agent_row()]}
    hub = FakeHub(after_launches(area, active))

    code = invoke(
        start_words(area, "bob", "implementer", "claude-code"),
        programs=programs(area),
        reader=hub,
        workspace=workspace_of(area),
    )
    sessions = (area["sessions"] / "bob.jsonl",)

    # Termination by the finite resume count, with no external timeout: every
    # hung child (including the resumed ones) was swept by the watchdog. Count
    # attempts and exit codes, never assert a short wall-clock duration.
    assert code == LAUNCHER.EXIT_RESUMES_SPENT
    assert attempts(*sessions) == SUPERVISED_MAX_RESUMES + 1
    assert launches(area) == []
    codes = exit_codes(*sessions)
    assert len(codes) == SUPERVISED_MAX_RESUMES + 1 and all(code != 0 for code in codes)

    # Cleanup swept only this test's own children; none remain alive.
    assert live_own_pids(fake_supervision.pids) == []
