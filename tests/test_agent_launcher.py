"""The joined agents' launcher: start, resume, ask and Denise's doctor entry (#247).

The fake harness of the prepared launchers' tests stands in for each CLI here
too, extended to record its working directory and to stay running until a
test kills it. The hub is a reader whose answers each test scripts; its
`read()` is the real bearer-only parser. The hub's checkout is a git checkout
with legacy state, so `--hub <checkout>` finds the state directory and
`sessions/` locally, and its URL answers nothing: no test needs HTTP.
"""

from __future__ import annotations

import _thread
import argparse
import importlib.util
import io
import json
import os
import shlex
import signal
import subprocess
import sys
import threading
import traceback
from collections.abc import Callable
from contextlib import redirect_stdout
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import fake_harness
import pytest
from agent_hub_common.launcher_observations import permission_diagnostic
from fake_harness import (
    SUPERVISED_MAX_RESUMES,
    attempts,
    bounded,
    exit_codes,
    live_own_pids,
)
from test_alice_launcher import ID_LINES, IDS, LAUNCHER, ROOT
from timing import LIVENESS_S, wait_until_sync

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
RECOVERY = importlib.import_module("agent_recovery")

HUB = "hub-1"
RUN = "20261109_abcdef12"
WORKFLOW = "wf-1"
HARNESSES = ("claude-code", "codex", "opencode", "antigravity", "devin")


@pytest.fixture(autouse=True)
def stub_sandbox_preflight(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(CLI, "sandbox_preflight", lambda harness: None)


def test_sandbox_preflight_runs_required_probe(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.undo()
    monkeypatch.setattr(CLI.sys, "platform", "linux")
    calls: list[list[str]] = []

    def run(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(CLI.subprocess, "run", run)
    CLI.sandbox_preflight("claude-code")
    CLI.sandbox_preflight("codex")
    CLI.sandbox_preflight("opencode")
    assert [call[-1] for call in calls] == ["--claude", "--codex"]


def test_sandbox_preflight_refuses_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.undo()
    monkeypatch.setattr(CLI.sys, "platform", "linux")
    monkeypatch.setattr(
        CLI.subprocess, "run", lambda *a, **kw: subprocess.CompletedProcess(a, 1, "", "denied")
    )
    with pytest.raises(CLI.Usage, match="denied.*linux-sandbox-grants"):
        CLI.sandbox_preflight("codex")


def test_headless_policy_replaces_inherited_external_rules(tmp_path: Path) -> None:
    from opencode_headless import policy  # type: ignore[import-not-found]

    root = tmp_path / "allowed"
    inherited: dict[str, Any] = {
        "agent": {
            "build": {
                "variant": "high",
                "permission": {
                    "external_directory": {"/broader/*": "allow"},
                    "*": "allow",
                    "bash": {"rm *": "deny"},
                },
            }
        },
        "mcp": {"robomate": {"enabled": True}},
    }
    agent = policy(inherited, [root], "unique")["agent"]["unique"]
    assert agent["variant"] == "high"
    assert list(agent["permission"])[-1] == "external_directory"
    assert agent["permission"]["bash"] == {"rm *": "deny"}
    assert agent["permission"]["external_directory"] == {
        "*": "deny",
        root.as_posix() + "/*": "allow",
    }
    assert "/broader/*" in inherited["agent"]["build"]["permission"]["external_directory"]


def test_opencode_roots_use_git_common_dir_without_broadening_missing_metadata(
    tmp_path: Path,
) -> None:
    from opencode_headless import allowed_roots

    checkout = tmp_path / "checkout"
    checkout.mkdir()
    git(checkout, "init", "-q")
    git(
        checkout,
        "-c",
        "user.name=Test",
        "-c",
        "user.email=test@example.com",
        "commit",
        "--allow-empty",
        "-qm",
        "base",
    )
    worktree = tmp_path / "worktree"
    git(checkout, "worktree", "add", "--detach", str(worktree))
    lane_tmp = tmp_path / "lane" / "tmp"
    roots = allowed_roots(worktree, checkout, lane_tmp)
    assert roots == [
        worktree.resolve(),
        checkout.resolve(),
        (checkout / ".git").resolve(),
        lane_tmp.resolve(),
    ]
    assert allowed_roots(worktree, None, None) == [
        worktree.resolve(),
        (checkout / ".git").resolve(),
    ]
    assert tmp_path not in roots
    assert allowed_roots(worktree, worktree, worktree).count(worktree.resolve()) == 1


def test_opencode_start_and_resume_preserve_config_with_distinct_policies(
    area: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    use_harness("opencode", monkeypatch)
    inherited: dict[str, Any] = {
        "agent": {
            "build": {
                "variant": "high",
                "permission": {
                    "external_directory": {"/too-broad/*": "allow"},
                    "*": "allow",
                },
            }
        },
        "mcp": {"custom": {"enabled": False}},
        "plugin": ["file:///existing.mjs"],
    }
    monkeypatch.setenv("OPENCODE_CONFIG_CONTENT", json.dumps(inherited))
    monkeypatch.setenv("FAKE_OPENCODE_CONFIG", json.dumps(inherited))
    lane_tmp = area["root"] / "assigned tmp"
    detached = {"workflow": workflow(), "agents": [agent_row()]}
    released = {"workflow": workflow(), "agents": [agent_row("released")]}
    hub = FakeHub(after_launches(area, detached, detached, released))
    assert (
        invoke(
            start_words(area, "bob", "implementer", "opencode", "--lane-tmp-dir", str(lane_tmp)),
            programs=programs(area),
            reader=hub,
            workspace=workspace_of(area),
        )
        == 0
    )
    names = []
    for launch in launches(area):
        argv = launch["argv"]
        assert "--auto" not in argv
        name = argv[argv.index("--agent") + 1]
        names.append(name)
        content = json.loads(launch["env"]["OPENCODE_CONFIG_CONTENT"])
        assert content["mcp"] == inherited["mcp"]
        assert "file:///existing.mjs" in content["plugin"]
        assert any(p.endswith("/permission-events.mjs") for p in content["plugin"])
        assert content["agent"][name]["variant"] == "high"
        rules = content["agent"][name]["permission"]["external_directory"]
        assert rules["*"] == "deny" and "/too-broad/*" not in rules
        assert rules[area["worktree"].as_posix() + "/*"] == "allow"
        assert rules[area["checkout"].as_posix() + "/*"] == "allow"
        assert rules[lane_tmp.as_posix() + "/*"] == "allow"
        assert launch["cwd"] == launch["env"]["PWD"] == str(area["worktree"])
    assert len(names) == len(set(names)) == 2
    saved = CLI.saved_identity(rows(area)[0], "opencode", "", "")
    assert saved.lane_tmp == lane_tmp


@pytest.mark.parametrize("failure", ["invalid", "exit"])
def test_opencode_probe_reports_configuration_failure(
    area: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    failure: str,
) -> None:
    use_harness("opencode", monkeypatch)
    monkeypatch.setenv(
        "FAKE_OPENCODE_INVALID" if failure == "invalid" else "FAKE_OPENCODE_EXIT",
        "{bad" if failure == "invalid" else "1",
    )
    hub = FakeHub(after_launches(area, {"workflow": workflow(), "agents": [agent_row()]}))
    assert (
        invoke(
            start_words(area, "bob", "implementer", "opencode"),
            programs=programs(area),
            reader=hub,
            workspace=workspace_of(area),
        )
        == LAUNCHER.EXIT_CANNOT_RESUME
    )
    error = capsys.readouterr().err
    expected = "returned invalid JSON (4 bytes)" if failure == "invalid" else "could not be read"
    assert expected in error
    assert "configuration failure" in error
    assert "hub reachability" not in error


@pytest.mark.parametrize("system_cache", [False, True])
def test_ambient_uv_cache_never_grants_its_parent(
    area: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
    system_cache: bool,
) -> None:
    import tempfile

    cache = (
        Path(tempfile.gettempdir()) if system_cache else area["root"] / "unrelated"
    ) / "uv-cache"
    monkeypatch.setenv("UV_CACHE_DIR", str(cache))
    hub = FakeHub(
        after_launches(
            area,
            {"workflow": workflow(), "agents": [agent_row()]},
            {"workflow": workflow(), "agents": [agent_row("released")]},
        )
    )
    assert (
        invoke(
            start_words(area, "bob", "implementer", "opencode"),
            programs=programs(area),
            reader=hub,
            workspace=workspace_of(area),
        )
        == 0
    )
    launch = launches(area)[0]
    content = json.loads(launch["env"]["OPENCODE_CONFIG_CONTENT"])
    name = launch["argv"][launch["argv"].index("--agent") + 1]
    rules = content["agent"][name]["permission"]["external_directory"]
    assert cache.parent.resolve().as_posix() + "/*" not in rules


@pytest.mark.parametrize("matching_metadata", [False, True])
def test_prepared_temporary_root_requires_matching_run_metadata(
    area: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
    matching_metadata: bool,
) -> None:
    area["sessions"].mkdir(parents=True)
    metadata = {
        "run_dir": str(area["sessions"] if matching_metadata else area["root"]),
        "hub_repo": str(area["checkout"]),
        "state_dir": str(area["state"]),
    }
    (area["sessions"] / "run.json").write_text(json.dumps(metadata))
    monkeypatch.setenv("UV_CACHE_DIR", str(area["root"] / "unrelated" / "uv-cache"))
    hub = FakeHub(
        after_launches(
            area,
            {"workflow": workflow(), "agents": [agent_row()]},
            {"workflow": workflow(), "agents": [agent_row("released")]},
        )
    )
    assert (
        invoke(
            start_words(area, "bob", "implementer", "opencode"),
            programs=programs(area),
            reader=hub,
            workspace=workspace_of(area),
        )
        == 0
    )
    launch = launches(area)[0]
    content = json.loads(launch["env"]["OPENCODE_CONFIG_CONTENT"])
    name = launch["argv"][launch["argv"].index("--agent") + 1]
    rules = content["agent"][name]["permission"]["external_directory"]
    assert ((area["sessions"] / "tmp" / "bob").as_posix() + "/*" in rules) == matching_metadata
    assert (area["root"] / "unrelated").as_posix() + "/*" not in rules


def test_permission_trace_is_incremental_and_matches_session_request(tmp_path: Path) -> None:
    from permission_trace import PermissionTrace  # type: ignore[import-not-found]

    path = tmp_path / "trace"
    path.touch()
    observer = PermissionTrace(path, "launch")

    def append(**fields: Any) -> None:
        with path.open("a") as stream:
            stream.write(
                json.dumps({"launch_id": "launch", "session": "s", "request": "r", **fields}) + "\n"
            )

    append(type="permission.asked", permission="external_directory", patterns=["/path/\x1b[31m*"])
    assert observer.read() and len(observer.pending) == 1
    append(type="permission.replied", session="other")
    append(type="permission.replied", request="other")
    append(type="permission.replied", launch_id="old")
    with path.open("a") as stream:
        stream.write('malformed\n{"type":')
    assert not observer.read() and len(observer.pending) == 1
    with path.open("a") as stream:
        stream.write("null}\n")
    append(type="permission.replied")
    assert observer.read() and not observer.pending
    append(type="permission.asked", permission="external_directory", patterns=["/path/*"])
    assert observer.read() and observer.pending
    path.write_text("")
    assert observer.read() and not observer.pending
    append(type="permission.asked", launch_id="stale", permission="external_directory", patterns=[])
    assert not observer.read()


@pytest.mark.parametrize("probe", ["config", "git"])
def test_opencode_probe_timeouts_have_clear_fail_closed_messages(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    probe: str,
) -> None:
    from opencode_headless import OpenCodeConfigError, allowed_roots, prepare

    def timeout(command: list[str], **kwargs: Any) -> Any:
        raise subprocess.TimeoutExpired(command, kwargs["timeout"])

    monkeypatch.setattr(subprocess, "run", timeout)
    expected = "OpenCode effective configuration" if probe == "config" else "Git common-directory"
    with pytest.raises(
        OpenCodeConfigError if probe == "config" else ValueError,
        match=expected + " probe failed or timed out",
    ):
        if probe == "config":
            prepare(["opencode", "run"], {}, tmp_path, tmp_path / "journal")
        else:
            allowed_roots(tmp_path, None, None)


def test_permission_publisher_throttles_and_recovers_transient_replace_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from agent_hub_common import launcher_observations as observations

    now = [0.0]
    errors: list[str] = []
    publisher = observations.ObservationPublisher(
        tmp_path, "bob", errors.append, clock=lambda: now[0]
    )
    pending = {"pending": [{"request": "r"}]}
    publisher.publish({"pending": []})
    assert not list(tmp_path.iterdir())
    original = os.replace
    attempts = []

    def locked(source: str, target: Path) -> None:
        attempts.append(now[0])
        raise PermissionError("sharing violation")

    monkeypatch.setattr(os, "replace", locked)
    for tick in (0.0, 0.2, 0.4, 1.0, 1.2, 2.0):
        now[0] = tick
        publisher.publish(pending)
    assert attempts == [0.0, 1.0, 2.0]
    assert len(errors) == 1
    assert not list(tmp_path.iterdir())  # temporary replace files are removed on failure
    monkeypatch.setattr(os, "replace", original)
    now[0] = 3.0
    publisher.publish(pending)
    snapshot = tmp_path / "bob.permission.json"
    assert json.loads(snapshot.read_text())["pending"] == pending["pending"]
    before = snapshot.read_bytes()
    now[0] = 3.2
    publisher.publish(pending)
    assert snapshot.read_bytes() == before
    publisher.publish({"pending": []})
    assert json.loads(snapshot.read_text())["pending"] == []
    before = snapshot.read_bytes()
    now[0] = 9.0
    publisher.publish({"pending": []})
    assert snapshot.read_bytes() == before
    publisher.close()
    assert "pending" not in json.loads(snapshot.read_text())


@pytest.mark.parametrize("supported", [False, True])
def test_claude_headless_permission_prompts_capability(
    area: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
    supported: bool,
) -> None:
    monkeypatch.setenv("FAKE_CLAUDE_HELP", "--permission-prompts <mode>" if supported else "")
    hub = FakeHub(
        after_launches(
            area,
            {"workflow": workflow(), "agents": [agent_row()]},
            {"workflow": workflow(), "agents": [agent_row("released")]},
        )
    )
    assert (
        invoke(
            start_words(
                area,
                "bob",
                "implementer",
                "claude-code",
                "--harness-version",
                "2.1.325" if supported else "2.1.100",
            ),
            programs=programs(area),
            reader=hub,
            workspace=workspace_of(area),
        )
        == 0
    )
    argv = launches(area)[0]["argv"]
    assert ("--permission-prompts" in argv) == supported
    if supported:
        assert argv[argv.index("--permission-prompts") + 1] == "none"
    assert argv[argv.index("--permission-mode") + 1] == "auto"


def test_hanging_opencode_reports_pending_before_exit(
    area: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    use_harness("opencode", monkeypatch)
    monkeypatch.setenv("FAKE_PLAN", '["hang"]')
    monkeypatch.setenv("FAKE_PERMISSION_ASK", "1")
    detached = {"workflow": workflow(), "agents": [agent_row()]}
    released = {"workflow": workflow(), "agents": [agent_row("released")]}
    hub = FakeHub(after_launches(area, detached, released))
    errors: list[str] = []
    from agent_hub_common.discovery import HubEndpoint
    from robomate import cli as status_cli

    status = {
        "hub_id": HUB,
        "workflow": workflow(headline="work"),
        "repo_root": str(area["checkout"]),
        "url": "http://hub.test",
        "origin": "o/r",
        "forge": "github",
        "default_branch": "main",
        "orchestrator": None,
        "agents": [
            {
                "name": "bob",
                "harness": "opencode",
                "model": "test",
                "status": "busy",
                "alive": True,
                "stalled": True,
                "activity": {},
                "current_task": "task",
            }
        ],
        "tasks": [],
        "pending_questions": [],
    }
    monkeypatch.setattr(
        status_cli, "find_hub", lambda *a, **kw: HubEndpoint("http://hub.test", "t")
    )
    monkeypatch.setattr(status_cli, "_rpc", lambda *a, **kw: status)
    monkeypatch.setattr(
        status_cli, "_hub_location", lambda *a: (area["state"], area["state"] / "token")
    )

    def inspect_and_kill() -> None:
        pid_file = area["harness_dir"] / "pid"
        pid = None
        try:
            wait_until_sync(pid_file.exists, "the fake harness starts")
            pid = int(wait_until_sync(lambda: pid_file.read_text(), "its pid"))
            diagnostic = wait_until_sync(
                lambda: permission_diagnostic(area["sessions"], "bob", HUB, WORKFLOW),
                "the live permission diagnostic",
            )
            assert (
                diagnostic == "opencode permission ask pending: external_directory /outside/path/*"
            )
            output = io.StringIO()
            with redirect_stdout(output):
                status_cli._status(False, None)
            assert f"STALLED — {diagnostic}" in output.getvalue()
            assert any(
                row["event"] == "permission_observation" and row["pending"] for row in rows(area)
            )
            trace_path = next(area["sessions"].glob("permission-*.jsonl"))
            launch_id = trace_path.stem.removeprefix("permission-")

            def reply(session: str, request: str) -> None:
                with trace_path.open("a") as stream:
                    stream.write(
                        json.dumps(
                            {
                                "launch_id": launch_id,
                                "type": "permission.replied",
                                "session": session,
                                "request": request,
                            }
                        )
                        + "\n"
                    )

            snapshot = area["sessions"] / "bob.permission.json"

            def observed() -> float:
                # The publisher replaces this file atomically; Windows can deny
                # a read that lands during the replace (the viewer retries).
                try:
                    return float(json.loads(snapshot.read_text())["observed_at"])
                except OSError:
                    return 0.0

            before = wait_until_sync(observed, "the permission snapshot is readable")
            reply("unrelated", "per_1")
            reply("ses_7Fa9b2C", "unrelated")
            wait_until_sync(
                lambda: observed() > before,
                "the observer reads unrelated replies",
            )
            assert permission_diagnostic(area["sessions"], "bob", HUB, WORKFLOW) == diagnostic
            reply("ses_7Fa9b2C", "per_1")
            wait_until_sync(
                lambda: not permission_diagnostic(area["sessions"], "bob", HUB, WORKFLOW),
                "the matching reply clears the diagnostic",
            )
        except BaseException:
            errors.append(traceback.format_exc())
        finally:
            if pid is not None:
                try:
                    os.kill(pid, signal.SIGTERM)
                except ProcessLookupError:
                    pass  # POSIX: the harness already exited.
                except OSError:
                    errors.append(traceback.format_exc())

    inspector = threading.Thread(target=inspect_and_kill)
    inspector.start()
    try:
        assert (
            invoke(
                start_words(area, "bob", "implementer", "opencode"),
                programs=programs(area),
                reader=hub,
                workspace=workspace_of(area),
            )
            == 0
        )
    finally:
        inspector.join()
    assert not errors, "\n\n".join(errors)
    assert permission_diagnostic(area["sessions"], "bob", HUB, WORKFLOW) is None
    assert not list(area["sessions"].glob("permission-*.jsonl"))


FAKE_HARNESS = (
    """\
import json, os, stat, sys, time
"""
    + fake_harness.CHILD_PID_RECORD
    + """\
argv = sys.argv[1:]
if argv == ["--help"]:
    print(os.environ.get("FAKE_CLAUDE_HELP", ""))
    sys.exit(0)
if argv == ["debug", "config"]:
    if os.environ.get("FAKE_OPENCODE_EXIT"):
        sys.exit(1)
    raw = os.environ.get("FAKE_OPENCODE_INVALID")
    if raw is None:
        config = json.loads(os.environ.get("FAKE_OPENCODE_CONFIG", "{}"))
        config["provider"] = {"catalog": "x" * 100_000}
        raw = json.dumps(config)
    data = raw.encode("utf-8")
    if stat.S_ISFIFO(os.fstat(1).st_mode):
        data = data[:65_536]
    sys.stdout.buffer.write(data)
    sys.exit(0)
if "mcp" in argv and "list" in argv and "--json" in argv:
    entries = json.loads(os.environ.get("FAKE_MCP_SERVERS", "[]"))
    if any("enabled=false" in word for word in argv):
        for entry in entries:
            entry["enabled"] = False
    print(json.dumps(entries))
    sys.exit(0)
stdin = sys.stdin.read() if argv and argv[-1] == "-" else None
keys = ("FAKE_MARK", "OPENCODE_CONFIG_CONTENT", "TMPDIR", "ROBOMATE_HUB_URL", "ROBOMATE_TOKEN",
        "ROBOMATE_TOKEN_FILE", "ROBOMATE_HUB_ID", "CODEX_HOME", "CLAUDE_CONFIG_DIR", "HUB_HARNESS",
        "HUB_HARNESS_VERSION", "HUB_PROVIDER", "HUB_MODEL", "HUB_EFFORT", "UV_CACHE_DIR",
        "PYTEST_XDIST_AUTO_NUM_WORKERS", "HUB_TELEMETRY_LOG", "PWD", "VIRTUAL_ENV", "PATH")
record = {"argv": argv, "stdin": stdin, "cwd": os.getcwd(),
          "env": {k: os.environ.get(k) for k in keys}}
log = os.environ["FAKE_LOG"]
count = 0
if os.path.exists(log):
    with open(log, encoding="utf-8") as stream:
        count = len(stream.readlines())
with open(log, "a", encoding="utf-8") as stream:
    stream.write(json.dumps(record) + "\\n")
bridge_join = os.environ.get("FAKE_BRIDGE_JOIN")
telemetry = os.environ.get("HUB_TELEMETRY_LOG")
if bridge_join and telemetry:
    if bridge_join in ("success-then-takeover-refused", "success-then-no-hub"):
        bridge_join = "success" if count == 0 else bridge_join.removeprefix("success-then-")
    rows = [{"event": "session_started"},
            {"event": "tool_call", "tool": "join", "phase": "start"}]
    if bridge_join == "success":
        rows.append({"event": "tool_call", "tool": "join", "phase": "success"})
    elif bridge_join == "takeover-refused":
        rows.append({"event": "tool_call", "tool": "join", "phase": "error",
                     "error": "takeover refused: workspace: elsewhere; retry"})
    elif bridge_join == "no-hub":
        rows.append({"event": "tool_call", "tool": "join", "phase": "error",
                     "error": "this robomate bridge has no hub: unbound"})
    with open(telemetry, "a", encoding="utf-8") as stream:
        stream.write("".join(json.dumps(row) + "\\n" for row in rows))
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
    if os.environ.get("FAKE_PERMISSION_ASK"):
        with open(os.environ["ROBOMATE_PERMISSION_EVENTS"], "a", encoding="utf-8") as stream:
            row = {"launch_id": os.environ["ROBOMATE_PERMISSION_LAUNCH"],
                   "type": "permission.asked", "session": "ses_7Fa9b2C",
                   "request": "per_1", "permission": "external_directory",
                   "patterns": ["/outside/path/*"]}
            stream.write(json.dumps(row) + "\\n")
    with open(os.environ["FAKE_PID"], "w", encoding="utf-8") as stream:
        stream.write(str(os.getpid()))
    time.sleep(600)
sys.exit(int(step.split(":")[1]))
"""
)


def git(cwd: Path, *words: str) -> str:
    return subprocess.run(
        ["git", "-C", str(cwd), *words], check=True, capture_output=True, text=True
    ).stdout.strip()


@pytest.fixture
def area(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Path]:
    """A hub checkout with legacy state, an agent worktree, and the fake harness outside both."""

    monkeypatch.setattr(CLI, "check_mcp_entry", lambda harness: None)

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


class FakeOrchestratorHub(LAUNCHER.HubReader):  # type: ignore[misc,name-defined]
    """Use the production orchestrator reader with scripted bearer responses."""

    def __init__(self, answer: Callable[[str], dict[str, Any]]) -> None:
        super().__init__("http://127.0.0.1:9", Path("unused"))
        self.answer = answer

    def call(self, method: str) -> dict[str, Any]:
        return self.answer(method)


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
    # The first child's bridge records its join, so its resume is a takeover.
    monkeypatch.setenv("FAKE_BRIDGE_JOIN", "success")
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
        # Every harness's bridge is pinned to the launcher's hub by id (#511).
        assert run["env"]["ROBOMATE_HUB_ID"] == HUB
        # Every harness gets a bridge telemetry log in the hub's sessions/ (#512).
        assert run["env"]["HUB_TELEMETRY_LOG"] == str(
            (area["sessions"] / f"{agent}-bridge.jsonl").resolve()
        )
        # No rendered config, prompt file or MCP config path is passed.
        assert not any("--mcp-config" in word or "prompt.md" in word for word in run["argv"])
    resumed = second["stdin"] if harness == "codex" else second["argv"][-1]
    assert resumed == CLI.continuation(agent, role, joined=True)
    assert f'join(name="{agent}", role="{role}", takeover=true)' in resumed
    conversation = IDS[harness] or rows(area, agent)[0]["conversation_id"]
    assert conversation in second["argv"]
    # Nothing outside the hub's sessions/ was written or changed.
    after = files(area["root"], area["harness_dir"])
    changed = {path for path in after if before.get(path) != after[path]}
    assert changed and all(Path(path).parent == area["sessions"] for path in changed)
    events = [row["event"] for row in rows(area, agent)]
    assert events[0] == "start" and "resume" in events and events[-1] == "stop"
    assert events.count("joined") == 1
    launch_rows = [row for row in rows(area, agent) if row["event"] in ("start", "resume")]
    assert all(row["run_id"] == RUN and row["role"] == role for row in launch_rows)
    assert launch_rows[0]["workdir"] == str(area["worktree"].resolve())
    # Launch rows already record the hub id the child is pinned to (#511).
    assert all(row["hub_id"] == HUB for row in launch_rows)


def test_start_and_resume_name_the_worktree_and_drop_the_launcher_venv(
    area: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """#486: opencode takes its project from PWD, so a start elsewhere must not leak it."""

    use_harness("opencode", monkeypatch)
    checkout = area["root"] / "prep checkout"
    launcher_venv = checkout / ".venv"
    venv_bin = launcher_venv / ("Scripts" if os.name == "nt" else "bin")
    venv_bin.mkdir(parents=True)
    operator_bin = area["root"] / "operator bin"
    operator_bin.mkdir()
    monkeypatch.setenv("PWD", str(checkout))
    monkeypatch.setenv("VIRTUAL_ENV", str(launcher_venv))
    monkeypatch.setenv(
        "PATH", os.pathsep.join([str(venv_bin), str(operator_bin), os.environ.get("PATH", "")])
    )
    monkeypatch.setenv("FAKE_PLAN", json.dumps(["exit:1", "exit:0"]))
    active = {"workflow": workflow(), "agents": [agent_row()]}
    released = {"workflow": workflow(), "agents": [agent_row("released", context="c2")]}
    hub = FakeHub(after_launches(area, active, active, released))

    code = invoke(
        start_words(area, "bob", "implementer", "opencode"),
        programs=programs(area),
        reader=hub,
        workspace=workspace_of(area),
    )

    assert code == 0
    start, resumed = launches(area)
    for run in (start, resumed):
        # The harness starts in the worktree, and its own idea of PWD agrees.
        assert Path(run["cwd"]).resolve() == area["worktree"].resolve()
        assert run["env"]["PWD"] == str(area["worktree"].resolve())
        # The launcher's uv project venv is not handed on; the operator's PATH is.
        assert run["env"]["VIRTUAL_ENV"] is None
        entries = [Path(entry).resolve() for entry in run["env"]["PATH"].split(os.pathsep)]
        assert venv_bin.resolve() not in entries
        assert operator_bin.resolve() in entries


def test_grants_are_flags_for_the_measured_harnesses(
    area: dict[str, Path], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
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

    monkeypatch.setattr(sys, "platform", "linux")
    settings = CLI.grants("claude-code", area["checkout"], area["state"] / "token")
    sandbox = json.loads(settings[1])["sandbox"]
    assert settings[0] == "--settings"
    assert sandbox["failIfUnavailable"] is True
    assert sandbox["filesystem"]["denyRead"] == [str(area["state"])]
    assert sandbox["network"]["allowedDomains"] == ["github.com", "api.github.com"]
    monkeypatch.setattr(sys, "platform", "win32")
    windows = json.loads(CLI.grants("claude-code", area["checkout"], area["state"] / "token")[1])[
        "sandbox"
    ]
    assert not windows.get("failIfUnavailable", False)
    assert windows["enabled"] is True
    assert windows["filesystem"] == sandbox["filesystem"]
    assert windows["network"] == sandbox["network"]
    assert CLI.grants("opencode", linked, None) == []


@pytest.mark.parametrize("main", [CLI.main, CLI.common.main], ids=["agent", "legacy"])
def test_launcher_main_configures_output_streams(
    monkeypatch: pytest.MonkeyPatch, main: Callable[..., int]
) -> None:
    stdout = io.TextIOWrapper(io.BytesIO(), encoding="cp1252", errors="strict")
    stderr = io.TextIOWrapper(io.BytesIO(), encoding="cp1252", errors="strict")
    with monkeypatch.context() as patch:
        patch.setattr(sys, "stdout", stdout)
        patch.setattr(sys, "stderr", stderr)
        with pytest.raises(SystemExit) as exc:
            main(["--help"])
        assert exc.value.code == 0
        assert stdout.encoding == stderr.encoding == "utf-8"
        assert stdout.errors == stderr.errors == "backslashreplace"
    stdout.close()
    stderr.close()


def test_relay_survives_unencodable_output(monkeypatch: pytest.MonkeyPatch) -> None:
    launcher = CLI.common.Launcher.__new__(CLI.common.Launcher)
    launcher.harness = "codex"
    launcher.conversation = None
    launcher.log = lambda *args, **kwargs: None
    output = io.BytesIO()
    stream = io.TextIOWrapper(output, encoding="cp1252", errors="strict")
    monkeypatch.setattr(sys, "stdout", stream)
    launcher.relay(io.StringIO("bad ∧\n" + ID_LINES["codex"] + "\nlater\n"), [])
    stream.flush()
    assert b"later" in output.getvalue()
    assert launcher.conversation == IDS["codex"]


@pytest.mark.parametrize("platform", ["win32", "linux"])
def test_windows_sandbox_warning_once_per_start_and_manual_resume(
    area: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    platform: str,
) -> None:
    monkeypatch.setattr(sys, "platform", platform)
    monkeypatch.setenv("FAKE_PLAN", json.dumps(["exit:1", "exit:0"]))
    monkeypatch.setenv("FAKE_BRIDGE_JOIN", "success")
    active = {"workflow": workflow(), "agents": [agent_row(context="c1")]}
    released = {"workflow": workflow(), "agents": [agent_row("released", context="c2")]}
    hub = FakeHub(after_launches(area, active, active, released))
    assert (
        invoke(
            start_words(area, "bob", "implementer", "claude-code"),
            programs=programs(area),
            reader=hub,
            workspace=workspace_of(area),
        )
        == 0
    )
    output = capsys.readouterr()
    assert output.err.count("warning: bob (claude-code) on Windows") == (platform == "win32")
    assert "warning: bob" not in output.out

    (area["harness_dir"] / "launches.jsonl").unlink()
    monkeypatch.setenv("FAKE_PLAN", json.dumps(["exit:0"]))
    now = datetime.now(UTC)
    conversation = "0b8a51c4-6a7e-4b6f-9c55-0c0ffee00003"
    saved_log(area, conversation, now - timedelta(minutes=10), now - timedelta(minutes=1))
    live = workflow(created=(now - timedelta(minutes=5)).isoformat())
    status = after_launches(
        area,
        {"workflow": live, "agents": [agent_row()]},
        {"workflow": live, "agents": [agent_row("released", context="c2")]},
    )

    def answer(method: str) -> dict[str, Any]:
        if method == "hub.snapshot":
            return {"hub_id": HUB, "workflow": live, "tasks": [], "taken_at": now.isoformat()}
        if method == "hub.questions":
            return {"questions": []}
        return status(method)

    assert (
        invoke(
            [
                "resume",
                "bob",
                "--hub",
                str(area["checkout"]),
                "--read-retry-delay-s",
                "0",
                "--resume-session",
                conversation,
            ],
            programs=programs(area),
            reader=FakeHub(answer),
        )
        == 0
    )
    output = capsys.readouterr()
    assert output.err.count("warning: bob (claude-code) on Windows") == (platform == "win32")
    assert "warning: bob" not in output.out


def test_kill_mid_task_resumes_the_same_conversation_with_backoff(
    area: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FAKE_PLAN", json.dumps(["hang", "exit:0"]))
    # The killed child's bridge had already recorded its join (#512).
    monkeypatch.setenv("FAKE_BRIDGE_JOIN", "success")
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
    assert second["argv"][-1] == CLI.continuation("bob", "implementer", joined=True)
    log = rows(area)
    assert [row["event"] for row in log] == [
        "start",
        "joined",
        "exit",
        "wait",
        "resume",
        "exit",
        "stop",
    ]
    assert log[2]["exit_code"] != 0
    assert log[3]["delay_s"] == 0.05
    assert log[-1]["reason"] == "released"
    assert all(row["run_id"] == RUN and row["conversation_id"] == conversation for row in log)


def interrupt_when_running(area: dict[str, Path], how: str) -> threading.Thread:
    """Interrupt the launcher as a terminal Ctrl-C would (#534)."""

    def interrupt() -> None:
        pid_file = area["harness_dir"] / "pid"
        pid = int(
            wait_until_sync(
                lambda: pid_file.read_text(encoding="utf-8") if pid_file.exists() else "",
                "the fake harness hangs mid-task",
            )
        )
        if how == "sigint":
            os.kill(os.getpid(), signal.SIGINT)
        elif how == "sighup":
            # SIGHUP is POSIX-only; Windows mypy has no attribute for it.
            name = "SIGHUP"
            os.kill(os.getpid(), getattr(signal, name))
        else:
            # A pending KeyboardInterrupt, then the terminal's SIGINT to the child.
            _thread.interrupt_main()
            os.kill(pid, signal.SIGINT)

    thread = threading.Thread(target=interrupt)
    thread.start()
    return thread


@pytest.mark.parametrize("how", ["sigint", "sighup", "keyboard"])
def test_ctrl_c_keeps_the_join_and_exit_rows_before_stopping(
    area: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
    how: str,
) -> None:
    """#534: Ctrl-C, SIGHUP and the fallback write exit after the join scan."""

    if how in ("sigint", "sighup") and os.name == "nt":
        pytest.skip("os.kill(self, SIGINT/SIGHUP) terminates the whole process on Windows")

    monkeypatch.setenv("FAKE_BRIDGE_JOIN", "success")
    monkeypatch.setenv("FAKE_PLAN", json.dumps(["hang"]))
    active = {"workflow": workflow(), "agents": [agent_row()]}

    interrupt = interrupt_when_running(area, how)
    code = invoke(
        start_words(area, "bob", "implementer", "claude-code"),
        programs=programs(area),
        reader=FakeHub(after_launches(area, active)),
        workspace=workspace_of(area),
    )
    interrupt.join()

    assert code == LAUNCHER.EXIT_INTERRUPTED
    log = rows(area)
    assert [row["event"] for row in log] == ["start", "joined", "exit", "stop"]
    assert log[-1]["reason"] == "interrupted"
    conversation = log[0]["conversation_id"]
    assert log[1]["conversation_id"] == conversation
    assert log[2]["exit_code"] != 0

    # The recorded exit and conversation get past exit_unknown without the flag.
    active = {"workflow": workflow(), "agents": [agent_row()]}
    released = {"workflow": workflow(), "agents": [agent_row("released", context="c2")]}
    hub = snapshotting(FakeHub(after_launches(area, active, active, released)))
    assert (
        invoke(
            [
                "resume",
                "bob",
                "--hub",
                str(area["checkout"]),
                "--read-retry-delay-s",
                "0",
                "--resume-session",
                conversation,
            ],
            programs=programs(area),
            reader=hub,
        )
        == 0
    )
    assert len(launches(area)) == 2
    assert any(row["event"] == "manual_resume" for row in rows(area))


def test_keyboard_fallback_records_exit_after_a_joined_row(tmp_path: Path) -> None:
    """#534 Windows CI: the interrupt can land after scan_join, before the exit row."""

    log = tmp_path / "bob.jsonl"
    base = {
        "agent": "bob",
        "harness": "claude-code",
        "role": "implementer",
        "conversation_id": IDS["claude-code"],
        "hub_id": HUB,
        "workflow_id": WORKFLOW,
        "run_id": RUN,
    }
    log.write_text(
        "".join(
            json.dumps(row) + "\n"
            for row in (
                base | {"event": "start", "timestamp": "2026-11-18T00:00:00+00:00"},
                base | {"event": "joined", "timestamp": "2026-11-18T00:00:01+00:00"},
            )
        ),
        encoding="utf-8",
    )
    launcher = LAUNCHER.Launcher.__new__(LAUNCHER.Launcher)
    launcher.args = argparse.Namespace(sessions=log)
    launcher.agent = "bob"
    launcher.harness = "claude-code"
    launcher.conversation = IDS["claude-code"]
    launcher.hub_id = HUB
    launcher.workflow_id = WORKFLOW
    launcher.child = None
    launcher.child_exit_at = "2026-11-18T00:00:02+00:00"
    launcher.last_exit_code = 1
    launcher.pending_launch = None
    launcher.resumes = 0

    launcher.interrupted_fallback()

    values = [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]
    assert [row["event"] for row in values] == ["start", "joined", "exit"]
    assert values[-1]["exit_code"] == 1


def test_keyboard_fallback_records_a_launch_lost_to_the_interrupt(tmp_path: Path) -> None:
    """#534 Windows CI: the interrupt can land between Popen and the launch row."""

    log = tmp_path / "bob.jsonl"
    launcher = LAUNCHER.Launcher.__new__(LAUNCHER.Launcher)
    launcher.args = argparse.Namespace(sessions=log)
    launcher.agent = "bob"
    launcher.harness = "claude-code"
    launcher.conversation = IDS["claude-code"]
    launcher.hub_id = HUB
    launcher.workflow_id = WORKFLOW
    launcher.child = None
    launcher.child_exit_at = "2026-11-18T00:00:01+00:00"
    launcher.last_exit_code = 1
    launcher.pending_launch = (
        "start",
        {"resumes": 0, "pid": 4242, "launcher_pid": os.getpid(), "process": "python"},
    )
    launcher.resumes = 0

    launcher.interrupted_fallback()

    values = [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]
    assert [row["event"] for row in values] == ["start", "exit"]
    assert values[0]["pid"] == 4242
    assert values[-1]["exit_code"] == 1


def test_automatic_resume_without_a_recorded_join_sends_the_plain_join_line(
    area: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A conversation whose first join never completed must not be told to take over (#512)."""

    monkeypatch.delenv("FAKE_BRIDGE_JOIN", raising=False)
    monkeypatch.setenv("FAKE_PLAN", json.dumps(["exit:1", "exit:0"]))
    active = {"workflow": workflow(), "agents": [agent_row(context="c1")]}
    released = {"workflow": workflow(), "agents": [agent_row("released", context="c2")]}
    hub = FakeHub(after_launches(area, active, active, released))

    code = invoke(
        start_words(area, "bob", "implementer", "claude-code"),
        programs=programs(area),
        reader=hub,
        workspace=workspace_of(area),
    )

    assert code == 0
    _, resumed = launches(area)
    text = resumed["argv"][-1]
    assert text == CLI.continuation("bob", "implementer", joined=False)
    assert "Join robomate as bob, implementer." in text
    assert "takeover=true" not in text
    assert "task you still hold" not in text and "reattached_task" not in text
    assert "joined" not in [row["event"] for row in rows(area)]


def test_a_recorded_join_keeps_the_bounded_takeover_continuation(
    area: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The takeover text now names only this seat and stops on a refusal (#512)."""

    monkeypatch.setenv("FAKE_BRIDGE_JOIN", "success")
    monkeypatch.setenv("FAKE_PLAN", json.dumps(["exit:1", "exit:0"]))
    active = {"workflow": workflow(), "agents": [agent_row(context="c1")]}
    released = {"workflow": workflow(), "agents": [agent_row("released", context="c2")]}
    hub = FakeHub(after_launches(area, active, active, released))

    assert (
        invoke(
            start_words(area, "bob", "implementer", "claude-code"),
            programs=programs(area),
            reader=hub,
            workspace=workspace_of(area),
        )
        == 0
    )

    _, resumed = launches(area)
    text = resumed["argv"][-1]
    assert text == CLI.continuation("bob", "implementer", joined=True)
    assert 'join(name="bob", role="implementer", takeover=true)' in text
    assert "If the join response has a reattached_task, finish it" in text
    assert "refused takeover" in text and "report blocked" in text
    assert "never take over a seat you did not hold" in text
    assert "Its reattached_task is the task you still hold" not in text
    joined = [row for row in rows(area) if row["event"] == "joined"]
    conversation = rows(area)[0]["conversation_id"]
    assert [row["conversation_id"] for row in joined] == [conversation]


@pytest.mark.parametrize(
    ("bridge", "event"),
    [("success-then-takeover-refused", "takeover_refused"), ("success-then-no-hub", "no_hub")],
)
def test_a_refused_resume_join_stops_without_retaking_the_seat(
    area: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    bridge: str,
    event: str,
) -> None:
    """A refused takeover or an unbound bridge is terminal, not another resume (#512).

    The stop keeps the child's exit row, so a later manual resume passes the
    recorded-exit guard without --confirm-stopped (r1-1 on #512).
    """

    monkeypatch.setenv("FAKE_BRIDGE_JOIN", bridge)
    monkeypatch.setenv("FAKE_PLAN", json.dumps(["exit:1", "exit:0", "exit:0"]))
    active = {"workflow": workflow(), "agents": [agent_row(context="c1")]}
    released = {"workflow": workflow(), "agents": [agent_row("released", context="c2")]}
    hub = FakeHub(after_launches(area, active, active, active, released))

    code = invoke(
        start_words(area, "bob", "implementer", "claude-code"),
        programs=programs(area),
        reader=hub,
        workspace=workspace_of(area),
    )

    assert code == LAUNCHER.EXIT_CANNOT_RESUME
    # The operator gets a remedy, not the agent's instruction (r1-2 on #512).
    stop_text = capsys.readouterr().err
    assert "Check `robomate status`" in stop_text
    assert "agent-launcher.py resume bob" in stop_text
    assert "report blocked" not in stop_text
    assert len(launches(area)) == 2  # the start and one takeover attempt
    log = rows(area)
    events = [row["event"] for row in log]
    assert events.count("joined") == 1 and event in events
    # One wait and one takeover attempt, then the stop: no further resume.
    assert events.count("wait") == 1 and events.count("resume") == 1
    assert events[-2:] == [event, "stop"]
    last_launch = max(i for i, name in enumerate(events) if name == "resume")
    exit_after = events.index("exit", last_launch)
    assert exit_after < events.index(event)

    # The recorded exit lets a manual resume proceed without --confirm-stopped.
    monkeypatch.setenv("FAKE_BRIDGE_JOIN", "success")
    conversation = log[0]["conversation_id"]
    resume = ["resume", "bob", "--hub", str(area["checkout"]), "--read-retry-delay-s", "0"]
    assert (
        invoke(
            [*resume, "--resume-session", conversation],
            programs=programs(area),
            reader=snapshotting(hub),
        )
        == 0
    )
    assert len(launches(area)) == 3
    assert any(row["event"] == "manual_resume" for row in rows(area))


def test_telemetry_scan_ignores_partial_and_malformed_rows(tmp_path: Path) -> None:
    """Only complete JSON objects after the offset count (#512, like agent_worker.released)."""

    path = tmp_path / "bob-bridge.jsonl"
    start = {"event": "tool_call", "tool": "join", "phase": "start"}
    success = {"event": "tool_call", "tool": "join", "phase": "success"}
    path.write_bytes(
        b'{"event": "session_started"}\nnot json\nnull\n'
        + json.dumps(start).encode()
        + b"\n"
        + json.dumps(success).encode()
        + b'\n{"event": "tool_call", "tool": "join",'
    )
    assert CLI.telemetry_rows(path, 0) == [{"event": "session_started"}, start, success]

    offset = path.stat().st_size
    later = {"event": "tool_call", "tool": "join", "phase": "error", "error": "x"}
    with path.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(later) + "\n")
    assert CLI.telemetry_rows(path, offset) == [later]
    assert CLI.telemetry_rows(tmp_path / "missing.jsonl", 0) == []


def test_saved_join_matches_only_the_resumed_conversation() -> None:
    rows: list[dict[str, Any]] = [
        {"event": "start", "conversation_id": "conversation-a"},
        {"event": "joined", "conversation_id": "conversation-a"},
        {"event": "joined", "conversation_id": "conversation-b"},
    ]
    assert CLI.saved_join(rows, "conversation-a")
    assert CLI.saved_join(rows, "conversation-b")
    assert not CLI.saved_join(rows, "conversation-c")
    assert not CLI.saved_join(rows, None)
    assert not CLI.saved_join([{"event": "joined"}], "conversation-a")


def test_orchestrator_resume_matches_its_recorded_join(
    area: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FAKE_BRIDGE_JOIN", "success")
    monkeypatch.setenv("FAKE_PLAN", json.dumps(["exit:1", "exit:0"]))
    active = {"workflow": workflow(), "agents": [], "orchestrator": {"name": "alice"}}
    done = {"workflow": workflow("done"), "agents": []}
    hub = FakeOrchestratorHub(after_launches(area, active, active, done))

    assert (
        invoke(
            start_words(area, "alice", "orchestrator", "claude-code"),
            programs=programs(area),
            reader=hub,
            workspace=workspace_of(area),
        )
        == 0
    )

    text = launches(area)[1]["argv"][-1]
    assert text == CLI.continuation("alice", "orchestrator", joined=True)
    assert 'join(name="alice", role="orchestrator", takeover=true)' in text
    assert "Then call get_state" in text and "On resume" in text


def test_orchestrator_that_never_joins_stops(
    area: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("FAKE_BRIDGE_JOIN", raising=False)
    monkeypatch.setenv("FAKE_PLAN", json.dumps(["exit:0"]))
    queued: dict[str, Any] = {"workflow": workflow("queued"), "agents": [], "orchestrator": None}
    code = invoke(
        start_words(area, "alice", "orchestrator", "claude-code"),
        programs=programs(area),
        reader=FakeOrchestratorHub(after_launches(area, queued)),
        workspace=workspace_of(area),
    )
    assert code == LAUNCHER.EXIT_NEVER_JOINED
    assert len(launches(area)) == 1
    rows = [
        json.loads(line) for line in (area["sessions"] / "alice.jsonl").read_text().splitlines()
    ]
    assert rows[-1]["reason"] == "never_joined"
    assert not any(row["event"] == "wait" for row in rows)


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
    area: dict[str, Path],
    conversation: str,
    started: datetime,
    exited: datetime,
    **launch: object,
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
            **launch,
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
    # No joined row was recorded for this conversation, so the resume asks for
    # the plain join line rather than a takeover (#512).
    text = CLI.continuation("bob", "implementer", joined=False)
    assert launch["argv"][-4:] == ["--resume", conversation, "-p", text]
    assert "Join robomate as bob, implementer." in text
    assert "takeover=true" not in text and "task you still hold" not in text
    assert Path(launch["cwd"]).resolve() == area["worktree"].resolve()
    log = rows(area)
    recovery = next(row for row in log if row["event"] == "recovery")
    assert recovery["workflow_id"] == WORKFLOW
    manual = next(row for row in log if row["event"] == "manual_resume")
    assert manual["run_id"] == RUN and manual["workflow_id"] == WORKFLOW
    # The agent is never pointed at a file in the hub's state; the record stays there.
    assert not any("resume.md" in word for word in launch["argv"])
    assert "Before-snapshot" in (area["sessions"] / "bob-resume.md").read_text(encoding="utf-8")


@pytest.mark.parametrize("recorded", ["the-resumed", "another", None])
def test_manual_resume_join_text_matches_the_recorded_conversation(
    area: dict[str, Path], monkeypatch: pytest.MonkeyPatch, recorded: str | None
) -> None:
    """Only a join recorded for this exact conversation makes a manual resume a takeover (#512)."""

    conversation = "0b8a51c4-6a7e-4b6f-9c55-0c0ffee00003"
    now = datetime.now(UTC)
    saved_log(area, conversation, now - timedelta(minutes=10), now - timedelta(minutes=1))
    if recorded is not None:
        row = {
            "agent": "bob",
            "harness": "claude-code",
            "role": "implementer",
            "event": "joined",
            "timestamp": (now - timedelta(minutes=2)).isoformat(),
            "conversation_id": conversation
            if recorded == "the-resumed"
            else "another-conversation",
            "hub_id": HUB,
            "workflow_id": None,
            "run_id": None,
        }
        with (area["sessions"] / "bob.jsonl").open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(row) + "\n")
    joined = recorded == "the-resumed"
    live = workflow(created=(now - timedelta(minutes=5)).isoformat())
    status = after_launches(
        area,
        {"workflow": live, "agents": [agent_row()]},
        {"workflow": live, "agents": [agent_row("released", context="c2")]},
    )

    def answer(method: str) -> dict[str, Any]:
        if method == "hub.snapshot":
            return {"hub_id": HUB, "workflow": live, "tasks": [], "taken_at": now.isoformat()}
        if method == "hub.questions":
            return {"questions": []}
        return status(method)

    code = invoke(
        [
            "resume",
            "bob",
            "--hub",
            str(area["checkout"]),
            "--read-retry-delay-s",
            "0",
            "--resume-session",
            conversation,
        ],
        programs=programs(area),
        reader=FakeHub(answer),
    )

    assert code == 0
    (launch,) = launches(area)
    text = CLI.continuation("bob", "implementer", joined=joined)
    assert launch["argv"][-4:] == ["--resume", conversation, "-p", text]
    assert ('join(name="bob", role="implementer", takeover=true)' in text) == joined
    record = (area["sessions"] / "bob-resume.md").read_text(encoding="utf-8")
    assert f"Sent to the agent: {text}" in record


IDENTITY = ("HUB_HARNESS", "HUB_HARNESS_VERSION", "HUB_PROVIDER", "HUB_MODEL", "HUB_EFFORT")


@pytest.mark.parametrize(
    ("harness", "provider"), [("claude-code", "anthropic"), ("codex", "openai")]
)
def test_start_exports_identity_uv_cache_and_workers_to_every_launch(
    area: dict[str, Path], monkeypatch: pytest.MonkeyPatch, harness: str, provider: str
) -> None:
    """#402 and #285 carried into the joined launcher, which start-run.py uses (#268)."""

    use_harness(harness, monkeypatch)
    monkeypatch.setenv("HUB_MODEL", "an-ambient-claim")
    monkeypatch.setenv("FAKE_PLAN", json.dumps(["exit:1", "exit:0"]))
    active = {"workflow": workflow(), "agents": [agent_row(context="c1")]}
    final = {"workflow": workflow(), "agents": [agent_row("released", context="c2")]}
    hub = FakeHub(after_launches(area, active, active, final))
    cache = area["root"] / "run" / "tmp" / "bob" / "uv-cache"

    code = invoke(
        start_words(
            area,
            "bob",
            "implementer",
            harness,
            "--model",
            "m-1",
            "--effort",
            "high",
            "--harness-version",
            "9.9.9",
            "--uv-cache-dir",
            str(cache),
            "--pytest-workers",
            "3",
        ),
        programs=programs(area),
        reader=hub,
        workspace=workspace_of(area),
    )

    assert code == 0
    expected = {
        "HUB_HARNESS": harness,
        "HUB_HARNESS_VERSION": "9.9.9",
        "HUB_PROVIDER": provider,
        # The launcher's setting, never an ambient claim.
        "HUB_MODEL": "m-1",
        "HUB_EFFORT": "high",
    }
    telemetry = str((area["sessions"] / "bob-bridge.jsonl").resolve())
    for launch in launches(area):
        assert {key: launch["env"][key] for key in IDENTITY} == expected
        assert launch["env"]["UV_CACHE_DIR"] == str(cache.resolve())
        assert launch["env"]["PYTEST_XDIST_AUTO_NUM_WORKERS"] == "3"
        # Every harness's child env carries the launcher's hub-id pin (#511) and
        # its bridge telemetry log (#512).
        assert launch["env"]["ROBOMATE_HUB_ID"] == HUB
        assert launch["env"]["HUB_TELEMETRY_LOG"] == telemetry
        forwarded = [word for word in launch["argv"] if word.startswith("mcp_servers.robomate")]
        if harness == "codex":
            # Codex forwards nothing to an MCP server unless the entry names it,
            # so the pin and the telemetry log are named on argv too (#511, #512).
            assert forwarded == [
                'mcp_servers.robomate.env.HUB_HARNESS_MODE="headless"',
            ] + [
                f"mcp_servers.robomate.env.{key}={json.dumps(value)}"
                for key, value in expected.items()
            ] + [
                f"mcp_servers.robomate.env.HUB_TELEMETRY_LOG={json.dumps(telemetry)}",
                f"mcp_servers.robomate.env.ROBOMATE_HUB_ID={json.dumps(HUB)}",
            ]
            roots = next(
                word for word in launch["argv"] if word.startswith("sandbox_workspace_write.wr")
            )
            assert json.loads(roots.split("=", 1)[1])[-1] == str(cache.resolve())
        else:
            assert forwarded == []
            settings = launch["argv"][launch["argv"].index("--settings") + 1]
            assert json.loads(settings)["sandbox"]["filesystem"]["allowWrite"] == [
                str(cache.resolve())
            ]
    assert cache.is_dir()
    if os.name != "nt":
        assert cache.stat().st_mode & 0o777 == 0o700
    launch_rows = [row for row in rows(area) if row["event"] in ("start", "resume")]
    assert len(launch_rows) == 2
    for row in launch_rows:
        assert row["harness_version"] == "9.9.9"
        assert row["uv_cache_dir"] == str(cache.resolve())
        assert row["pytest_workers"] == 3


def setup_command(code: str) -> str:
    words = [sys.executable, "-c", code]
    return subprocess.list2cmdline(words) if os.name == "nt" else shlex.join(words)


def test_workspace_setup_runs_per_start_and_failure_is_isolated(
    area: dict[str, Path], monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    cache = area["root"] / "tmp" / "bob" / "uv-cache"
    command = setup_command(
        "import json, os; from pathlib import Path; "
        "Path('setup.json').write_text(json.dumps([os.getcwd(), os.environ['UV_CACHE_DIR'], "
        "os.environ['TMPDIR']]))"
    )
    active = {"workflow": workflow(), "agents": []}

    def run(agent: str, *options: str) -> int:
        released = {"workflow": workflow(), "agents": [agent_row("released", name=agent)]}
        previous = len(launches(area))

        def answer(method: str) -> dict[str, Any]:
            if method == "hub.info":
                return {"hub_id": HUB}
            return active if len(launches(area)) == previous else released

        return invoke(
            start_words(area, agent, "implementer", "claude-code", *options),
            programs=programs(area),
            reader=FakeHub(answer, agent),
            workspace=workspace_of(area),
        )

    assert (
        run(
            "bob",
            "--workspace-setup",
            command,
            "--uv-cache-dir",
            str(cache),
            "--lane-tmp-dir",
            str(cache.parent),
        )
        == 0
    )
    assert json.loads((area["worktree"] / "setup.json").read_text()) == [
        str(area["worktree"]),
        str(cache),
        str(cache.parent),
    ]
    (area["worktree"] / "setup.json").unlink()
    assert run("charlie") == 0
    assert not (area["worktree"] / "setup.json").exists()
    before = len(launches(area))
    failing = setup_command("import sys; print('setup broke'); sys.exit(7)")
    assert run("denise", "--workspace-setup", failing) != 0
    assert "setup broke" in capsys.readouterr().err
    assert len(launches(area)) == before


@pytest.mark.parametrize(("option", "expected"), [([], "7"), (["--pytest-workers", "0"], None)])
def test_pytest_workers_zero_clears_an_inherited_budget(
    area: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
    option: list[str],
    expected: str | None,
) -> None:
    monkeypatch.setenv("PYTEST_XDIST_AUTO_NUM_WORKERS", "7")
    monkeypatch.setenv("FAKE_PLAN", json.dumps(["exit:0"]))
    joined = {"workflow": workflow(), "agents": [agent_row("released", context="c1")]}
    hub = FakeHub(after_launches(area, {"workflow": workflow(), "agents": []}, joined))

    code = invoke(
        start_words(area, "bob", "implementer", "claude-code", *option),
        programs=programs(area),
        reader=hub,
        workspace=workspace_of(area),
    )

    assert code == 0
    (launch,) = launches(area)
    assert launch["env"]["PYTEST_XDIST_AUTO_NUM_WORKERS"] == expected
    # Without --harness-version the bridge reports the version as unknown, never a guess.
    assert launch["env"]["HUB_HARNESS_VERSION"] == ""
    assert rows(area)[0]["uv_cache_dir"] is None


def test_negative_pytest_workers_is_refused(area: dict[str, Path]) -> None:
    code = invoke(
        start_words(area, "bob", "implementer", "claude-code", "--pytest-workers", "-1"),
        programs=programs(area),
        reader=FakeHub(lambda method: pytest.fail(f"read the hub: {method}")),
        workspace=workspace_of(area),
    )

    assert code == 2
    assert launches(area) == []


@pytest.mark.parametrize("environment_exists", [True, False])
@pytest.mark.parametrize("interactive", [True, False])
def test_resume_exports_the_saved_identity_uv_cache_and_workers_again(
    area: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
    environment_exists: bool,
    interactive: bool,
) -> None:
    """A guarded resume of a start-run agent keeps #402's identity and #285's cache (#268)."""

    conversation = "0b8a51c4-6a7e-4b6f-9c55-0c0ffee00002"
    now = datetime.now(UTC)
    cache = area["root"] / "run" / "tmp" / "bob" / "uv-cache"
    if environment_exists:
        (area["worktree"] / ".venv").mkdir()
    setup = setup_command("from pathlib import Path; Path('setup-ran').touch()")
    saved_log(
        area,
        conversation,
        now - timedelta(minutes=10),
        now - timedelta(minutes=1),
        model="claude-sonnet-5-5",
        effort="max",
        harness_version="2.1.999",
        uv_cache_dir=str(cache),
        pytest_workers=2,
        workspace_setup=setup,
    )
    live = workflow(created=(now - timedelta(minutes=5)).isoformat())
    status = after_launches(
        area,
        {"workflow": live, "agents": [agent_row()]},
        {"workflow": live, "agents": [agent_row("released", context="c2")]},
    )

    def answer(method: str) -> dict[str, Any]:
        if method == "hub.snapshot":
            return {"hub_id": HUB, "workflow": live, "tasks": [], "taken_at": now.isoformat()}
        if method == "hub.questions":
            return {"questions": []}
        return status(method)

    code = invoke(
        ["resume", "bob", "--hub", str(area["checkout"]), "--read-retry-delay-s", "0"]
        + (["--interactive"] if interactive else []),
        programs=programs(area),
        reader=FakeHub(answer),
    )

    assert code == 0
    assert (area["worktree"] / "setup-ran").exists() is (not environment_exists)
    (launch,) = launches(area)
    assert {key: launch["env"][key] for key in IDENTITY} == {
        "HUB_HARNESS": "claude-code",
        "HUB_HARNESS_VERSION": "2.1.999",
        "HUB_PROVIDER": "anthropic",
        "HUB_MODEL": "claude-sonnet-5-5",
        "HUB_EFFORT": "max",
    }
    assert launch["env"]["UV_CACHE_DIR"] == str(cache)
    assert launch["env"]["PYTEST_XDIST_AUTO_NUM_WORKERS"] == "2"
    assert cache.is_dir()
    if os.name != "nt":
        assert cache.stat().st_mode & 0o777 == 0o700
    manual = next(row for row in rows(area) if row["event"] == "manual_resume")
    assert manual["uv_cache_dir"] == str(cache) and manual["pytest_workers"] == 2


@pytest.mark.parametrize("safe", [False, True])
def test_interactive_start_does_not_resume_but_records_the_session(
    area: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
    safe: bool,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("FAKE_PLAN", json.dumps(["exit:3"]))
    hub = FakeHub(after_launches(area, {"workflow": workflow(), "agents": []}))
    setup = setup_command("from pathlib import Path; Path('interactive-setup').touch()")
    words = start_words(
        area, "bob", "implementer", "codex", "--interactive", "--workspace-setup", setup
    )
    words += ["--safe-mode"] if safe else []

    code = invoke(
        [*words, "--harness-args", "--extra", "x y"],
        programs=programs(area),
        reader=hub,
        workspace=workspace_of(area),
    )

    assert code == 3
    assert (area["worktree"] / "interactive-setup").exists()
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
    assert launch["env"]["PWD"] == str(area["worktree"].resolve())
    assert launch["env"]["VIRTUAL_ENV"] is None


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


def test_remote_codex_argv_carries_the_pin_and_selection_without_a_token_value(
    area: dict[str, Path], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A remote Codex bridge needs the pin, URL and token-file path on argv (#511)."""

    monkeypatch.setenv("FAKE_PLAN", json.dumps(["exit:0"]))
    monkeypatch.setenv("ROBOMATE_HUB_URL", "http://ambient:1")
    sessions = tmp_path / "remote sessions"
    token = area["state"] / "token"
    released = {"workflow": workflow(), "agents": [agent_row("released", context="c2")]}
    hub = FakeHub(after_launches(area, {"workflow": workflow(), "agents": []}, released))

    def no_workspace(agent: str, selector: str | None) -> Path:
        raise AssertionError("a remote host has no local robomate workspace")

    remote = ["--hub-url", "http://hub.example:8420/", "--token-file", str(token)]
    remote += ["--sessions-dir", str(sessions), "--read-retry-delay-s", "0"]
    words = ["start", "bob", "--role", "implementer", "--harness", "codex", *remote]
    words += ["--workdir", str(area["worktree"]), "--predecessor-poll-s", "0.01"]
    words += ["--resume-delay-s", "0"]
    assert invoke(words, programs=programs(area), reader=hub, workspace=no_workspace) == 0

    (launch,) = launches(area)
    argv = launch["argv"]
    forwarded = [word for word in argv if word.startswith("mcp_servers.robomate.env.")]
    assert f"mcp_servers.robomate.env.ROBOMATE_HUB_ID={json.dumps(HUB)}" in forwarded
    assert (
        f"mcp_servers.robomate.env.ROBOMATE_HUB_URL={json.dumps('http://hub.example:8420')}"
        in forwarded
    )
    assert (
        f"mcp_servers.robomate.env.ROBOMATE_TOKEN_FILE={json.dumps(str(token.resolve()))}"
        in forwarded
    )
    # No argv element ever contains the token value, only the token-file path.
    assert all("secret-token-value" not in word for word in argv)
    assert launch["env"]["ROBOMATE_HUB_ID"] == HUB
    assert launch["env"]["ROBOMATE_HUB_URL"] == "http://hub.example:8420"


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
    prompt = f"Read {run['denise'] / 'startup.md'} and follow the instructions in it"
    assert Path(launch["cwd"]).resolve() == (run["denise"] / "workspace").resolve()
    assert launch["argv"][-1] == prompt
    assert not any("Join robomate" in word or "--mcp-config" in word for word in launch["argv"])
    assert not any(word in launch["argv"] for word in ("-p", "--approve-for-me"))
    if harness == "codex":
        assert "approval_policy=on-request" in launch["argv"]
    else:
        assert "--strict-mcp-config" in launch["argv"]
    granted = [
        launch["argv"][i + 1] for i, word in enumerate(launch["argv"]) if word == "--add-dir"
    ]
    assert str(run["target"].resolve()) in granted and str(run["run_dir"].resolve()) in granted
    # Every grant stays a directory and the prompt stays positional: Claude
    # Code's variadic --add-dir otherwise consumes a trailing prompt (#513).
    assert prompt not in granted and "--" not in granted
    if harness == "claude":
        assert launch["argv"][-2] == "--"
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
@pytest.mark.parametrize("clean_login", [False, True])
@pytest.mark.parametrize("ambient", [False, True])
def test_doctor_login_and_no_inherited_hub_selection(
    area: dict[str, Path],
    run: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
    harness: str,
    clean_login: bool,
    ambient: bool,
    capsys: pytest.CaptureFixture[str],
) -> None:
    for name, value in {
        "ROBOMATE_HUB_URL": "http://another-hub:1",
        "ROBOMATE_TOKEN": "ambient-token",
        "ROBOMATE_TOKEN_FILE": str(area["root"] / "ambient token"),
        "CODEX_HOME": str(area["root"] / "team codex home"),
        "CLAUDE_CONFIG_DIR": str(area["root"] / "team claude config"),
    }.items():
        monkeypatch.setenv(name, value)

    if not ambient:
        for key in ("CODEX_HOME", "CLAUDE_CONFIG_DIR"):
            monkeypatch.delenv(key)
    monkeypatch.setenv("FAKE_MCP_SERVERS", json.dumps([{"name": "robomate", "enabled": True}]))
    words = ["doctor", "--hub", str(area["checkout"]), "--run", RUN, "--harness", harness]
    if clean_login:
        words += ["--clean-login"]
    assert invoke(words, programs=programs(area), reader=no_hub()) == 0

    (launch,) = launches(area)
    env = launch["env"]
    assert env["PWD"] == str(Path(launch["cwd"]).resolve())
    assert env["VIRTUAL_ENV"] is None
    assert env["ROBOMATE_HUB_URL"] is None and env["ROBOMATE_TOKEN"] is None
    assert env["ROBOMATE_TOKEN_FILE"] is None
    profile = run["denise"] / "configs" / ("codex" if harness == "codex" else "claude")
    variable = "CODEX_HOME" if harness == "codex" else "CLAUDE_CONFIG_DIR"
    context = json.loads((run["denise"] / "context.json").read_text(encoding="utf-8"))
    if clean_login:
        assert env[variable] == str(profile) and profile.is_dir()
        assert context["doctor"]["profile"] == str(profile)
        if harness == "codex":
            config = (profile / "config.toml").read_text(encoding="utf-8")
            assert "[mcp_servers" not in config and "approval" not in config.split("#")[0]
        else:
            assert "/login" in capsys.readouterr().out
    else:
        assert "profile" not in context["doctor"]
        assert not profile.exists()
    for key in ("CODEX_HOME", "CLAUDE_CONFIG_DIR"):
        if clean_login and key == variable:
            continue
        expected = (
            str(area["root"] / ("team codex home" if key == "CODEX_HOME" else "team claude config"))
            if ambient
            else None
        )
        assert env[key] == expected
    argv = launch["argv"]
    if harness == "codex":
        assert "mcp_servers.robomate.enabled=false" in argv
        assert "approval_policy=on-request" in argv and "sandbox_mode=workspace-write" in argv
    else:
        assert "--strict-mcp-config" in argv and "--mcp-config" not in argv
        assert argv[argv.index("--permission-mode") + 1] == "default"


@pytest.mark.parametrize("failure", ["enabled", "bad-json", "bad-shape", "exit", "timeout"])
def test_doctor_refuses_unverified_codex_mcp(
    area: dict[str, Path],
    run: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
    failure: str,
    capsys: pytest.CaptureFixture[str],
) -> None:
    calls = []
    real_run = subprocess.run

    def listing(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        if "mcp" not in argv:
            return real_run(argv, **kwargs)
        calls.append(argv)
        if failure == "exit":
            raise subprocess.CalledProcessError(1, argv, stderr="secret transport")
        if failure == "timeout":
            raise subprocess.TimeoutExpired(argv, 30)
        body = json.dumps([{"name": "robomate", "enabled": True}])
        if len(calls) > 1:
            body = {"bad-json": "secret transport", "bad-shape": "{}"}.get(failure, body)
        return subprocess.CompletedProcess(argv, 0, stdout=body)

    monkeypatch.setattr(CLI.subprocess, "run", listing)
    words = ["doctor", "--hub", str(area["checkout"]), "--run", RUN, "--harness", "codex"]
    if failure == "exit":
        words += ["--harness-args", "session-only-argument"]
    assert (
        invoke(
            words,
            programs=programs(area),
            reader=no_hub(),
        )
        == 2
    )
    assert launches(area) == []
    error = capsys.readouterr().err
    assert "--clean-login" in error and "secret transport" not in error
    if failure != "enabled":
        assert "configuration probe failed" in error and "harness arguments" in error
    if failure == "exit":
        assert "session-only-argument" in calls[0]


def test_doctor_codex_disables_special_names_with_same_launch_settings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = []
    entries = [{"name": name, "enabled": True} for name in ("robomate", "with.dot")]

    def listing(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        calls.append((argv, kwargs))
        return subprocess.CompletedProcess(
            argv,
            0,
            stdout=json.dumps(
                entries if len(calls) == 1 else [entry | {"enabled": False} for entry in entries]
            ),
        )

    monkeypatch.setattr(CLI.subprocess, "run", listing)
    argv = ["codex", "-C", "/work", "-c", "approval_policy=on-request"]
    env = {"CODEX_HOME": "/operator"}
    flags = CLI.doctor_codex_mcp_flags(argv, environment=env, cwd="/work")
    assert flags == [
        "-c",
        'mcp_servers={"robomate"={enabled=false},"with.dot"={enabled=false}}',
    ]
    assert calls[0][0] == [*argv, "mcp", "list", "--json"]
    assert calls[1][0] == [*argv, *flags, "mcp", "list", "--json"]
    assert all(kwargs["env"] == env and kwargs["cwd"] == "/work" for _, kwargs in calls)


# -- a broken fake child (#464) --------------------------------------------------------


# An immediately failing child that records its pid (so cleanup is checkable)
# and prints a diagnostic, but dies before writing its launch marker.
IMMEDIATE_FAIL_CHILD = (
    """\
import os, sys
"""
    + fake_harness.CHILD_PID_RECORD
    + """\
print("fake child failed before its launch marker", file=sys.stderr, flush=True)
sys.exit(7)
"""
)

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


def test_a_resume_stop_names_the_full_runnable_ask_command(
    area: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
    fake_supervision: Any,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """#491: the hint after a resume names the hub and runs from any directory."""

    area["fake"].write_text(IMMEDIATE_FAIL_CHILD, encoding="utf-8")
    conversation = "0b8a51c4-6a7e-4b6f-9c55-0c0ffee00002"
    now = datetime.now(UTC)
    saved_log(area, conversation, now - timedelta(minutes=10), now - timedelta(minutes=1))
    live = workflow(created=(now - timedelta(minutes=5)).isoformat())
    hub = snapshotting(FakeHub(after_launches(area, {"workflow": live, "agents": [agent_row()]})))

    code = invoke(
        ["resume", "bob", "--hub", str(area["checkout"]), "--resume-delay-s", "0"],
        programs=programs(area),
        reader=hub,
    )

    assert code == LAUNCHER.EXIT_RESUMES_SPENT
    script = ROOT / "scripts/agent-launcher.py"
    if os.name == "nt":
        expected = (
            f"& 'uv' 'run' '--locked' '--project' '{ROOT}' 'python' '{script}' "
            f"'ask' 'bob' '--hub' '{area['checkout']}'"
        )
    else:
        expected = (
            f"uv run --locked --project {ROOT} python {script} ask bob --hub '{area['checkout']}'"
        )
    err = capsys.readouterr().err
    assert f"To only ask bob questions: {expected}" in err
    assert "To only ask bob questions: agent-launcher.py ask bob" not in err


# A child that hangs on every attempt and never writes its launch marker. The
# watchdog must keep sweeping across automatic resumes, not fire once (#464
# r1-1); otherwise the resumed child hangs with no watchdog left.
HANG_FAKE_CHILD = (
    """\
import os, sys, time
"""
    + fake_harness.CHILD_PID_RECORD
    + """\
print("fake child hanging before its launch marker", file=sys.stderr, flush=True)
time.sleep(10**6)
"""
)


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


# -- stop by hand (#534) -----------------------------------------------------------

# A real launcher process, with the production signal handling and supervision,
# whose hub reader is scripted in-process so no HTTP is needed. The subprocess
# is the launcher PID that `stop` finds in the lock and the launch rows.
BACKGROUND_LAUNCHER = """\
import importlib.util
import json
import os
import sys

spec = importlib.util.spec_from_file_location("agent_launcher_cli", sys.argv[1])
cli = importlib.util.module_from_spec(spec)
sys.modules["agent_launcher_cli"] = cli
spec.loader.exec_module(cli)
config = json.loads(sys.argv[2])


class Reader:
    def read(self):
        return (
            config["hub_id"],
            {
                "id": config["workflow_id"],
                "status": "active",
                "run_id": config["run_id"],
            },
        )


cli.sandbox_preflight = lambda harness: None
cli.check_mcp_entry = lambda harness: None
# The Windows venv python.exe is a trampoline, not the fake harness process.
# Stopping that wrapper alone leaves its interpreter running. Use the base
# interpreter directly so the launch row identifies the fake harness itself.
python = os.path.join(sys.base_prefix, "python.exe") if os.name == "nt" else sys.executable
programs = {name: [python, config["fake"]] for name in cli.PROGRAMS}
raise SystemExit(cli.main(config["words"], programs=programs, reader=Reader()))
"""


def spawn_background_launcher(
    area: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> subprocess.Popen[str]:
    """Run the real launcher in a subprocess, hanging its fake harness (#534)."""

    monkeypatch.setenv("FAKE_PLAN", json.dumps(["hang"]))
    helper = area["root"] / "background launcher.py"
    helper.write_text(BACKGROUND_LAUNCHER, encoding="utf-8")
    config = {
        "fake": str(area["fake"]),
        "hub_id": HUB,
        "workflow_id": WORKFLOW,
        "run_id": RUN,
        "words": [
            "start",
            "bob",
            "--role",
            "implementer",
            "--harness",
            "claude-code",
            "--hub",
            str(area["checkout"]),
            "--workdir",
            str(area["worktree"]),
            "--max-resumes",
            "0",
            "--resume-delay-s",
            "0",
            "--read-retry-delay-s",
            "0",
        ],
    }
    return subprocess.Popen(
        [sys.executable, str(helper), str(ROOT / "scripts/agent-launcher.py"), json.dumps(config)],
        cwd=area["root"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
    )


def wait_for_launch(area: dict[str, Path]) -> dict[str, Any]:
    """The recorded start row, once the background launcher has written it."""

    def started() -> dict[str, Any] | None:
        path = area["sessions"] / "bob.jsonl"
        try:
            values = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
        except (OSError, ValueError):
            return None
        return values[0] if values and values[0].get("event") == "start" else None

    row = wait_until_sync(started, "the background launcher records its start")
    assert row is not None
    return row


def stop_words(area: dict[str, Path]) -> list[str]:
    return ["stop", "bob", "--hub", str(area["checkout"])]


def test_stop_stops_an_attached_launcher_and_resume_needs_no_confirmation(
    area: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
    fake_supervision: Any,
) -> None:
    """#534: stop signals the recorded launcher PID and waits for its stop row."""

    process = spawn_background_launcher(area, monkeypatch)
    try:
        start = wait_for_launch(area)
        launcher_pid = start["launcher_pid"]
        # uv's Windows venv python.exe is a trampoline: the recorded launcher
        # PID is the real interpreter, not the Popen handle (#534 Windows CI).
        assert launcher_pid != start["pid"]
        assert RECOVERY.process_alive(launcher_pid)
        # The held lock body names only its current holder (#534 r1-2). Skip
        # the locked first byte, which Windows denies to another process.
        lock = area["sessions"] / "bob.lock"
        with lock.open("rb") as stream:
            stream.seek(1)
            assert stream.read() == f"launcher_pid {launcher_pid}\n".encode()

        assert CLI.main(stop_words(area), programs=programs(area)) == 0
        process.wait(timeout=LIVENESS_S)
        err = process.stderr.read() if process.stderr is not None else ""
        assert f"launcher pid {launcher_pid}" in err

        events = [row["event"] for row in rows(area)]
        if os.name == "nt":
            # TerminateProcess cannot run the launcher's handler: stop records
            # the orphaned harness's exit itself.
            assert events == ["start", "exit"]
            assert rows(area)[-1]["stopped_by"] == "agent-launcher stop"
        else:
            assert process.returncode == LAUNCHER.EXIT_INTERRUPTED
            assert events == ["start", "exit", "stop"]
            assert rows(area)[-1]["reason"] == "interrupted"
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=LIVENESS_S)

    # The recorded exit lets the next resume proceed without --confirm-stopped.
    active = {"workflow": workflow(), "agents": [agent_row()]}
    released = {"workflow": workflow(), "agents": [agent_row("released", context="c2")]}
    hub = snapshotting(FakeHub(after_launches(area, active, active, released)))
    conversation = rows(area)[0]["conversation_id"]
    assert (
        invoke(
            [
                "resume",
                "bob",
                "--hub",
                str(area["checkout"]),
                "--read-retry-delay-s",
                "0",
                "--resume-session",
                conversation,
            ],
            programs=programs(area),
            reader=hub,
        )
        == 0
    )
    assert len(launches(area)) == 2
    assert live_own_pids(fake_supervision.pids) == []


def test_stop_terminates_an_orphaned_harness_and_records_its_exit(
    area: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
    fake_supervision: Any,
) -> None:
    """#534: no launcher attached, but the recorded harness PID still runs."""

    process = spawn_background_launcher(area, monkeypatch)
    harness_pid = 0
    try:
        start = wait_for_launch(area)
        harness_pid = start["pid"]
        pid_file = area["harness_dir"] / "pid"
        wait_until_sync(pid_file.exists, "the fake harness reports its pid")
        fake_pid = wait_until_sync(lambda: pid_file.read_text() or None, "its pid is written")
        assert fake_pid is not None
        assert int(fake_pid) == harness_pid
        process.kill()
        process.wait(timeout=LIVENESS_S)
        assert wait_until_sync(
            lambda: RECOVERY.process_alive(harness_pid),
            "the orphaned fake harness still runs",
        )

        assert CLI.main(stop_words(area), programs=programs(area)) == 0
        assert wait_until_sync(
            lambda: not RECOVERY.process_alive(harness_pid),
            "the recorded harness is terminated",
        )
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=LIVENESS_S)

    stopped = [row for row in rows(area) if row.get("event") == "exit"]
    assert len(stopped) == 1
    assert stopped[0]["pid"] == harness_pid
    assert stopped[0]["stopped_by"] == "agent-launcher stop"
    assert live_own_pids(fake_supervision.pids) == []


def test_stop_leaves_a_reused_harness_pid_alone(
    area: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """#534: a live recorded PID whose process name changed is not ours to stop."""

    process = spawn_background_launcher(area, monkeypatch)
    try:
        start = wait_for_launch(area)
        harness_pid = start["pid"]
        process.kill()
        process.wait(timeout=LIVENESS_S)
        monkeypatch.setattr(CLI, "process_name", lambda pid: "unrelated.exe")

        assert CLI.main(stop_words(area), programs=programs(area)) == LAUNCHER.EXIT_CANNOT_RESUME
        err = capsys.readouterr().err
        assert "may have been reused" in err and "Left it alone" in err
        assert not any(row.get("event") == "exit" for row in rows(area))
        assert RECOVERY.process_alive(harness_pid)
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=LIVENESS_S)


def test_stop_with_nothing_running_changes_nothing(
    area: dict[str, Path], capsys: pytest.CaptureFixture[str]
) -> None:
    """#534: no launcher and no recorded harness means a report, not a write."""

    area["sessions"].mkdir(parents=True)
    before = files(area["root"], area["harness_dir"])
    assert CLI.main(stop_words(area), programs=programs(area)) == 0
    assert "no launch is recorded" in capsys.readouterr().err
    assert files(area["root"], area["harness_dir"]) == before

    now = datetime.now(UTC)
    saved_log(
        area,
        "0b8a51c4-6a7e-4b6f-9c55-0c0ffee00004",
        now - timedelta(minutes=2),
        now - timedelta(minutes=1),
    )
    before = files(area["root"], area["harness_dir"])
    content = (area["sessions"] / "bob.jsonl").read_bytes()
    assert CLI.main(stop_words(area), programs=programs(area)) == 0
    assert "has its exit row" in capsys.readouterr().err
    assert (area["sessions"] / "bob.jsonl").read_bytes() == content
    assert files(area["root"], area["harness_dir"]) == before


def test_stop_does_not_clean_up_under_an_unknown_lock_holder(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """#534 r2-1: a held lock with no readable holder is not the dead launcher's."""

    sessions = tmp_path / "sessions"
    sessions.mkdir()
    start = {
        "agent": "bob",
        "harness": "claude-code",
        "role": "implementer",
        "event": "start",
        "timestamp": datetime.now(UTC).isoformat(),
        "conversation_id": "0b8a51c4-6a7e-4b6f-9c55-0c0ffee00005",
        "hub_id": HUB,
        "workflow_id": WORKFLOW,
        "run_id": RUN,
        "pid": 4242,
        "launcher_pid": 999999,
        "process": "python",
    }
    log = sessions / "bob.jsonl"
    log.write_text(json.dumps(start) + "\n", encoding="utf-8")
    monkeypatch.setattr(CLI, "launcher_attached", lambda path: True)
    monkeypatch.setattr(CLI, "lock_holder_pid", lambda path: None)
    monkeypatch.setattr(CLI, "process_alive", lambda pid: False)
    monkeypatch.setattr(CLI, "LOCK_SETTLE_S", 0.01)

    code = CLI.main(["stop", "bob", "--sessions-dir", str(sessions)])

    assert code == LAUNCHER.EXIT_CANNOT_RESUME
    assert "lock is still held" in capsys.readouterr().err
    values = [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]
    assert not any(row.get("event") == "exit" for row in values)


def test_stop_without_a_process_identity_leaves_the_pid_alone(
    area: dict[str, Path], capsys: pytest.CaptureFixture[str]
) -> None:
    """#534: an older launch row's live PID is unknown, not a match."""

    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    try:
        area["sessions"].mkdir(parents=True)
        start = {
            "agent": "bob",
            "harness": "claude-code",
            "role": "implementer",
            "event": "start",
            "timestamp": datetime.now(UTC).isoformat(),
            "conversation_id": IDS["claude-code"],
            "hub_id": HUB,
            "workflow_id": WORKFLOW,
            "run_id": RUN,
            "pid": child.pid,
            "workdir": str(area["worktree"]),
        }
        (area["sessions"] / "bob.jsonl").write_text(json.dumps(start) + "\n", encoding="utf-8")

        assert CLI.main(stop_words(area), programs=programs(area)) == LAUNCHER.EXIT_CANNOT_RESUME
        err = capsys.readouterr().err
        assert "recorded no process identity" in err
        assert not any(row.get("event") == "exit" for row in rows(area))
        assert RECOVERY.process_alive(child.pid)
    finally:
        child.kill()
        child.wait(timeout=LIVENESS_S)


# -- Windows-safe liveness (#464 r2-1) --------------------------------------------


def test_watchdog_ignores_patched_monotonic(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fake_supervision: Any
) -> None:
    """A healthy recorded child survives a clock patch after supervision starts (#475)."""
    import time

    child = tmp_path / "healthy.py"
    child.write_text(fake_harness.CHILD_PID_RECORD + "\nimport time\ntime.sleep(0.3)\n")
    monkeypatch.setattr(time, "monotonic", lambda: 1000.0)
    process = subprocess.Popen([sys.executable, str(child)])
    assert process.wait() == 0


def test_windows_pid_creation_guards_cleanup(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A reused PID is excluded; unavailable timestamps retain the fallback (#473)."""
    pids = tmp_path / "pids"
    pids.write_text(
        "42\tfake.py\t123\n43\tfake.py\t123\n44\tfake.py\tbad\n"
        "45\tfake.py\t123\ninvalid\n0\tfake.py\t123\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(fake_harness, "_IS_WINDOWS", True)
    monkeypatch.setattr(fake_harness, "_windows_alive", lambda pid: True)
    monkeypatch.setattr(
        fake_harness, "_windows_creation_time", lambda pid: {42: 123, 43: 124}.get(pid)
    )
    assert fake_harness.live_own_pids(pids) == [42, 44, 45]


@pytest.mark.skipif(os.name != "nt", reason="requires Windows process creation times")
def test_windows_child_records_creation_time(tmp_path: Path, fake_supervision: Any) -> None:
    child = tmp_path / "child.py"
    child.write_text(fake_harness.CHILD_PID_RECORD + "\ninput()\n", encoding="utf-8")
    process = subprocess.Popen([sys.executable, str(child)], stdin=subprocess.PIPE)
    try:
        wait_until_sync(
            lambda: (
                fake_supervision.pids.is_file()
                and fake_supervision.pids.read_text(encoding="utf-8").endswith("\n")
            ),
            "child PID record",
        )
        parts = fake_supervision.pids.read_text(encoding="utf-8").strip().split("\t")
        recorded_pid = int(parts[0])
        assert int(parts[2]) == fake_harness._windows_creation_time(recorded_pid)
        assert fake_harness.live_own_pids(fake_supervision.pids) == [recorded_pid]
    finally:
        process.communicate(input=b"\n")


def test_windows_creation_time_query_failure_falls_back(monkeypatch: pytest.MonkeyPatch) -> None:
    import ctypes

    def fail(*args: Any, **kwargs: Any) -> Any:
        raise ctypes.ArgumentError("unavailable")

    monkeypatch.setattr(ctypes, "WinDLL", fail, raising=False)
    assert fake_harness._windows_creation_time(42) is None


def test_windows_liveness_uses_still_active_query(monkeypatch: pytest.MonkeyPatch) -> None:
    """Liveness is OpenProcess + GetExitCodeProcess == STILL_ACTIVE, never
    os.kill(pid, 0) (CTRL_C_EVENT on Windows). Exercise the ctypes path on
    Linux with a stand-in kernel32."""
    import ctypes
    from types import SimpleNamespace

    import fake_harness

    calls: list[tuple[Any, ...]] = []

    class Fn:
        def __init__(self, result: Any) -> None:
            self.result = result

        def __call__(self, *args: Any) -> Any:
            calls.append(args)
            return self.result(*args) if callable(self.result) else self.result

    def kernel(handle: Any, code: int, exit_ok: bool = True) -> Any:
        def get_exit(_handle: Any, pcode: Any) -> int:
            pcode._obj.value = code
            return int(exit_ok)

        return SimpleNamespace(
            OpenProcess=Fn(lambda access, inherit, pid: handle),
            GetExitCodeProcess=Fn(get_exit),
            CloseHandle=Fn(lambda _handle: 1),
        )

    monkeypatch.setattr(ctypes, "WinDLL", lambda *_a, **_k: kernel(1, 259), raising=False)
    assert fake_harness._windows_alive(4242) is True
    assert (0x1000, False, 4242) in calls

    calls.clear()
    monkeypatch.setattr(ctypes, "WinDLL", lambda *_a, **_k: kernel(1, 0), raising=False)
    assert fake_harness._windows_alive(4242) is False  # exited (code 0)

    calls.clear()
    monkeypatch.setattr(ctypes, "WinDLL", lambda *_a, **_k: kernel(0, 259), raising=False)
    assert fake_harness._windows_alive(4242) is False  # OpenProcess failed


def test_windows_supervision_never_sends_console_ctrl_c(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The Windows path must never call os.kill(pid, 0) and must terminate only
    with a specific-pid SIGTERM, so no console or unrelated process is hit."""
    import fake_harness

    pids = tmp_path / "pids"
    pids.write_text("4242\tC:\\fake.py\n", encoding="utf-8")
    kills: list[tuple[int, int]] = []
    monkeypatch.setattr(fake_harness, "_IS_WINDOWS", True)
    monkeypatch.setattr(fake_harness, "_windows_alive", lambda pid: True)
    monkeypatch.setattr("os.kill", lambda pid, sig: kills.append((pid, sig)))

    assert fake_harness.live_own_pids(pids) == [4242]
    fake_harness.kill_own(pids)

    assert all(sig != 0 for _, sig in kills), "os.kill(pid, 0) would Ctrl-C the console"
    assert (4242, signal.SIGTERM) in kills


def test_start_refuses_missing_user_mcp_entry(
    area: dict[str, Path], monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from mcp_preflight import check  # type: ignore[import-not-found]

    monkeypatch.setattr(CLI, "check_mcp_entry", check)
    missing = area["root"] / "empty-codex"
    missing.mkdir()
    monkeypatch.setenv("CODEX_HOME", str(missing))
    rc = CLI.main(
        [
            "start",
            "bob",
            "--role",
            "implementer",
            "--harness",
            "codex",
            "--hub",
            str(area["checkout"]),
        ],
        programs=programs(area),
    )
    assert rc == 2
    err = capsys.readouterr().err
    assert "codex" in err and "[mcp_servers.robomate]" in err and "user-level" in err
    assert not (area["harness_dir"] / "launches.jsonl").exists()
    assert not area["sessions"].exists()
