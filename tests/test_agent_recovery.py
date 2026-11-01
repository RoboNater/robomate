"""Manual recovery refuses guesses/duplicates and generates factual prompts (#173)."""

from __future__ import annotations

import argparse
import importlib
import io
import json
import os
import subprocess
import sys
from copy import deepcopy
from pathlib import Path
from typing import Any

import pytest
from agent_hub.store import HubStore
from test_alice_launcher import CODEX_ID, LAUNCHER

RECOVERY = importlib.import_module("agent_recovery")


class RecoveryReader:
    def __init__(self) -> None:
        self.calls: list[str] = []
        self.state: dict[str, Any] = {
            "hub.info": {"hub_id": "hub-1"},
            "hub.status": {
                "workflow": {"id": "wf-1", "status": "active"},
                "agents": [],
                "orchestrator_activity": {"holding": None, "heartbeat_age_s": None},
            },
            "hub.snapshot": {
                "hub_id": "hub-1",
                "taken_at": "2026-10-09T12:00:00Z",
                "workflow": {"id": "wf-1", "status": "active"},
                "tasks": [
                    {"id": "task-1", "assignee": "bob", "role": "reviewer", "workflow_id": "wf-1"}
                ],
                "deliveries": [{"event_id": 7, "delivery_id": "d", "attempt": 1}],
                "queued_events": 2,
            },
            "hub.questions": {"questions": []},
        }

    def call(self, method: str) -> dict[str, Any]:
        self.calls.append(method)
        return deepcopy(self.state[method])

    def workflow(self, status: str) -> None:
        for method in ("hub.status", "hub.snapshot"):
            self.state[method]["workflow"]["status"] = status


@pytest.fixture
def recovery_args(tmp_path: Path) -> argparse.Namespace:
    args = argparse.Namespace(
        sessions=tmp_path / "bob-sessions.jsonl",
        harness="codex",
        resume_session=None,
        hub_id=None,
        workflow_id=None,
        confirm_stopped=False,
        stopped_at=None,
        force=False,
        resume_prompt=tmp_path / "resume-bob.prompt.md",
    )
    args.resume_prompt.write_text("Reconcile; check_in first.\n", encoding="utf-8")
    save_rows(args, [row("start"), row("exit")])
    return args


def row(event: str, **changes: Any) -> dict[str, Any]:
    return {
        "agent": "bob",
        "harness": "codex",
        "event": event,
        "conversation_id": CODEX_ID,
        "hub_id": "hub-1",
        "workflow_id": "wf-1",
        "timestamp": "2026-10-08T11:00:00Z",
        **changes,
    }


def save_rows(args: argparse.Namespace, rows: list[dict[str, Any]]) -> None:
    args.sessions.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")


@pytest.mark.parametrize("target_key", ["roadmap", "reservations"])
def test_resume_loads_old_and_new_reservations_metadata_without_rewriting_goal(
    recovery_args: argparse.Namespace, target_key: str
) -> None:
    """The rename must not change a prepared run's durable statement (#408)."""
    goal = "Address acme/app#42; update roadmap acme/app#2 after merge."
    manifest = {
        "schema_version": 1,
        "hub_id": "hub-1",
        "work": {"goal": goal, target_key: "acme/app#2"},
        "launch": {"agents": {"bob": {"harness": "codex"}}},
    }
    path = recovery_args.sessions.parent / "run.json"
    path.write_text(json.dumps(manifest), encoding="utf-8")
    before = path.read_bytes()
    conversation, prompt = RECOVERY.prepare(recovery_args, RecoveryReader(), "bob")
    assert conversation == CODEX_ID
    assert "task-1" in prompt
    assert path.read_bytes() == before


def test_zero_argument_preparation_captures_distinct_times_and_full_state(
    recovery_args: argparse.Namespace,
) -> None:
    reader = RecoveryReader()
    save_rows(recovery_args, [row("start"), row("exit", exited_at="2026-10-08T10:59:59Z")])
    conversation, text = RECOVERY.prepare(recovery_args, reader, "bob")
    assert conversation == CODEX_ID
    assert "Snapshot time: 2026-10-09T12:00:00Z" in text
    assert "Old process stop time: 2026-10-08T10:59:59+00:00 (recorded harness exit)" in text
    assert "task-1" in text and '"delivery_id": "d"' in text
    assert reader.calls == ["hub.info", "hub.status", "hub.snapshot", "hub.questions"]
    assert recovery_args.resume_prompt.read_text() == text
    # A second snapshot replaces, rather than appends, the previous facts.
    reader.state["hub.snapshot"]["taken_at"] = "2026-10-09T13:00:00Z"
    _, second = RECOVERY.prepare(recovery_args, reader, "bob")
    assert "2026-10-09T12:00:00Z" not in second
    assert second.count("Before-snapshot") == 1


@pytest.mark.parametrize(
    "harness,conversation",
    [
        ("claude-code", CODEX_ID),
        ("codex", CODEX_ID),
        ("opencode", "ses_ABC123"),
        ("antigravity", "conv-1234abcd"),
        ("devin", "brisk-otter"),
    ],
)
@pytest.mark.parametrize("agent", ["alice", "bob", "charlie"])
def test_all_pairs_recover_the_same_identity(
    recovery_args: argparse.Namespace,
    harness: str,
    conversation: str,
    agent: str,
) -> None:
    recovery_args.harness = harness
    save_rows(
        recovery_args,
        [
            row("start", agent=agent, harness=harness, conversation_id=conversation),
            row("exit", agent=agent, harness=harness, conversation_id=conversation),
        ],
    )
    assert RECOVERY.prepare(recovery_args, RecoveryReader(), agent)[0] == conversation


@pytest.mark.parametrize(
    "case",
    [
        "missing-id",
        "ambiguous-id",
        "wrong-conversation",
        "malformed-id",
        "corrupt-log",
        "wrong-agent",
        "no-hub-pin",
        "no-workflow-pin",
        "conflicting-pin",
        "other-hub",
        "other-snapshot-hub",
        "other-workflow",
        "disappeared",
        "done",
        "released",
        "attached-worker",
        "attached-alice",
        "holding-alice",
        "missing-attachment",
        "state-changed",
        "local-process",
        "exit-unknown",
    ],
)
def test_essential_refusals_are_never_overridden(
    recovery_args: argparse.Namespace,
    case: str,
) -> None:
    reader = RecoveryReader()
    agent = "bob"
    rows = [row("start"), row("exit")]
    recovery_args.force = True
    if case == "missing-id":
        rows = [row("start", conversation_id=None), row("exit", conversation_id=None)]
    elif case == "ambiguous-id":
        rows += [row("exit", conversation_id="119a2b3c-4d5e-6f70-8192-a3b4c5d6e7f8")]
    elif case in ("wrong-conversation", "malformed-id"):
        recovery_args.resume_session = (
            "119a2b3c-4d5e-6f70-8192-a3b4c5d6e7f8" if case == "wrong-conversation" else "--last"
        )
    elif case == "wrong-agent":
        rows[0]["agent"] = "charlie"
    elif case in ("no-hub-pin", "no-workflow-pin"):
        key = "hub_id" if case == "no-hub-pin" else "workflow_id"
        for r in rows:
            r[key] = None
    elif case == "conflicting-pin":
        rows[0]["hub_id"] = "other"
    elif case == "other-hub":
        reader.state["hub.info"]["hub_id"] = "other"
    elif case == "other-snapshot-hub":
        reader.state["hub.snapshot"]["hub_id"] = "other"
    elif case == "other-workflow":
        reader.state["hub.snapshot"]["workflow"]["id"] = "other"
    elif case == "disappeared":
        reader.state["hub.snapshot"]["workflow"] = None
    elif case == "done":
        reader.workflow("done")
    elif case in ("released", "attached-worker", "missing-attachment"):
        reader.state["hub.status"]["agents"] = [
            {
                "name": "bob",
                "status": "released" if case == "released" else "busy",
                "alive": True,
                "activity": {} if case == "missing-attachment" else {"instance": "old"},
            }
        ]
    elif case in ("attached-alice", "holding-alice"):
        agent = "alice"
        for r in rows:
            r["agent"] = agent
        reader.state["hub.status"]["orchestrator_activity"] = {
            "heartbeat_age_s": 1 if case == "attached-alice" else 1000,
            "holding": "wait_for_event" if case == "holding-alice" else None,
        }
    elif case == "state-changed":
        reader.state["hub.status"]["workflow"]["status"] = "paused"
    elif case in ("local-process", "exit-unknown"):
        rows = [row("start", pid=os.getpid())]
        recovery_args.confirm_stopped = case == "local-process"
    save_rows(recovery_args, rows)
    if case == "corrupt-log":
        recovery_args.sessions.write_text('{"event":', encoding="utf-8")
    before = recovery_args.resume_prompt.read_text()
    with pytest.raises(LAUNCHER.Stop):
        RECOVERY.prepare(recovery_args, reader, agent)
    assert recovery_args.resume_prompt.read_text() == before


def test_missing_metadata_explicit_controls_and_unknown_stop_time(
    recovery_args: argparse.Namespace,
) -> None:
    recovery_args.sessions.unlink()
    recovery_args.resume_session = CODEX_ID
    recovery_args.hub_id = "hub-1"
    recovery_args.workflow_id = "wf-1"
    recovery_args.confirm_stopped = True
    _, text = RECOVERY.prepare(recovery_args, RecoveryReader(), "bob")
    assert "unknown (not recorded)" in text and "recorded harness exit" not in text
    recovery_args.stopped_at = "2026-10-08T11:00:00-04:00"
    _, text = RECOVERY.prepare(recovery_args, RecoveryReader(), "bob")
    assert "2026-10-08T15:00:00+00:00 (operator-supplied)" in text


def test_new_start_without_id_never_reuses_an_older_conversation(
    recovery_args: argparse.Namespace,
) -> None:
    save_rows(
        recovery_args,
        [
            row("start"),
            row("exit"),
            row("start", conversation_id=None),
            row("exit", conversation_id=None),
        ],
    )
    recovery_args.force = True
    with pytest.raises(LAUNCHER.Stop, match="earlier start"):
        RECOVERY.prepare(recovery_args, RecoveryReader(), "bob")


@pytest.mark.parametrize("value", ["yesterday", "2026-10-08T11:00:00", "9999-01-01T00:00:00Z"])
def test_invalid_stop_time_never_launches(recovery_args: argparse.Namespace, value: str) -> None:
    recovery_args.stopped_at = value
    with pytest.raises(LAUNCHER.Stop, match="stop time|Stop time"):
        RECOVERY.prepare(recovery_args, RecoveryReader(), "bob")


def test_recorded_exit_allows_small_future_but_operator_stopped_at_stays_strict(
    recovery_args: argparse.Namespace,
) -> None:
    """#209: a backward step makes the launcher's own exit look 1-2s future."""

    from datetime import UTC, datetime, timedelta

    # Compute `now` fresh per case (r1-3): a single captured time goes stale
    # across `prepare()` calls on a loaded runner and the 1s case stops
    # being future-dated.
    for ahead_s in (1, 2):
        future = (datetime.now(UTC) + timedelta(seconds=ahead_s)).isoformat()
        save_rows(recovery_args, [row("start"), row("exit", exited_at=future)])
        _, text = RECOVERY.prepare(recovery_args, RecoveryReader(), "bob")
        assert "recorded harness exit" in text
        assert "clamped from future" in text
    # A large future recorded exit is still refused, naming its source.
    far_future = (datetime.now(UTC) + timedelta(seconds=60)).isoformat()
    save_rows(recovery_args, [row("start"), row("exit", exited_at=far_future)])
    with pytest.raises(LAUNCHER.Stop, match="Recorded harness exit"):
        RECOVERY.prepare(recovery_args, RecoveryReader(), "bob")
    # Even a small future operator-supplied --stopped-at is refused.
    recovery_args.stopped_at = (datetime.now(UTC) + timedelta(seconds=2)).isoformat()
    save_rows(recovery_args, [row("start"), row("exit")])
    with pytest.raises(LAUNCHER.Stop, match="Operator-supplied"):
        RECOVERY.prepare(recovery_args, RecoveryReader(), "bob")


@pytest.mark.parametrize("agent", ["alice", "bob", "charlie"])
@pytest.mark.parametrize("advisory", ["paused", "question"])
def test_advisories_preserve_state_and_alice_can_handle_pending_questions(
    recovery_args: argparse.Namespace,
    agent: str,
    advisory: str,
) -> None:
    reader = RecoveryReader()
    save_rows(recovery_args, [row("start", agent=agent), row("exit", agent=agent)])
    if advisory == "paused":
        reader.workflow("paused")
    else:
        reader.workflow("escalated")
        reader.state["hub.questions"]["questions"] = [{"question_id": 17, "question": "decision?"}]
    before = deepcopy(reader.state)
    if advisory == "paused" or agent != "alice":
        with pytest.raises(LAUNCHER.Stop, match="--force"):
            RECOVERY.prepare(recovery_args, reader, agent)
        recovery_args.force = True
    _, text = RECOVERY.prepare(recovery_args, reader, agent)
    assert "never answer a question or change workflow status" in text
    assert reader.state == before


def test_concurrent_launcher_lock_cannot_be_overridden(recovery_args: argparse.Namespace) -> None:
    with (
        RECOVERY.agent_lock(recovery_args.sessions),
        pytest.raises(LAUNCHER.Stop, match="launcher is still running"),
        RECOVERY.agent_lock(recovery_args.sessions),
    ):
        pytest.fail("duplicate acquired the lock")
    with RECOVERY.agent_lock(recovery_args.sessions):
        pass


@pytest.mark.parametrize(
    "control",
    [
        ["--hub-url", "http://other"],
        ["--harness", "opencode"],
        ["--sessions", "other.jsonl"],
        ["--", "other-command"],
        ["--recovery-command", "--hub-url", "http://other"],
        ["--recovery-command", "--", "evil"],
        ["--recovery-command"],
    ],
)
@pytest.mark.parametrize("interactive", [False, True])
def test_generated_controls_cannot_replace_the_pinned_run_or_command(
    control: list[str], interactive: bool
) -> None:
    with pytest.raises(SystemExit) as error:
        RECOVERY.split_controls(
            [
                "--harness",
                "codex",
                *(["--shell-command", "codex resume pinned-id"] if interactive else []),
                "--recovery-options",
                *control,
                "--recovery-command",
                *([] if interactive else ["--", "codex", "exec"]),
            ],
            interactive=interactive,
        )
    assert error.value.code == 2


def test_live_pid_recommendation_never_tells_operator_to_stop_unrelated_process(
    recovery_args: argparse.Namespace,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    save_rows(recovery_args, [row("start", pid=12345)])
    recovery_args.force = True
    recovery_args.confirm_stopped = True
    monkeypatch.setattr(RECOVERY, "process_exists", lambda pid: True)
    monkeypatch.setattr(RECOVERY, "process_name", lambda pid: "unrelated.exe")
    reader = RecoveryReader()
    with pytest.raises(LAUNCHER.Stop) as error:
        RECOVERY.prepare(recovery_args, reader, "bob")
    text = str(error.value)
    assert "unrelated.exe" in text and "PIDs can be reused" in text
    assert "stop it only if it is the old harness" in text
    assert "preserve that process" in text and "move the stale sessions log aside" in text
    assert "--resume-session, --hub-id, --workflow-id and --confirm-stopped" in text
    assert reader.calls == []


@pytest.mark.parametrize("windows", [False, True])
def test_process_name_is_display_only_and_queries_the_requested_pid(
    monkeypatch: pytest.MonkeyPatch, windows: bool
) -> None:
    def query(command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        assert kwargs == {"capture_output": True, "text": True, "timeout": 2}
        if windows:
            assert command == ["tasklist", "/FI", "PID eq 123", "/FO", "CSV", "/NH"]
            output = '"other.exe","124","Console","1","1 K"\n"python.exe","123","Console","1","1 K"'
        else:
            assert command == ["ps", "-p", "123", "-o", "comm="]
            output = "python\n"
        return subprocess.CompletedProcess(command, 0, output, "")

    monkeypatch.setattr(RECOVERY.subprocess, "run", query)
    assert RECOVERY.process_name(123, windows=windows) == ("python.exe" if windows else "python")


def test_process_name_failure_does_not_disable_duplicate_guard(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def unavailable(*args: Any, **kwargs: Any) -> None:
        raise OSError("process tool unavailable")

    monkeypatch.setattr(RECOVERY.subprocess, "run", unavailable)
    assert RECOVERY.process_name(123).startswith("unavailable;")


def test_process_name_can_query_own_process() -> None:
    assert not RECOVERY.process_name(os.getpid()).startswith("unavailable;")


@pytest.mark.parametrize(
    "case", ["configuration", "manifest-harness", "manifest-workspace", "manifest-hub"]
)
def test_original_run_metadata_is_essential(recovery_args: argparse.Namespace, case: str) -> None:
    manifest: dict[str, Any] = {}
    if case == "configuration":
        recovery_args.recovery_config = recovery_args.sessions.parent / "missing-home"
    elif case == "manifest-harness":
        manifest = {"launch": {"agents": {"bob": {"harness": "opencode"}}}}
    elif case == "manifest-workspace":
        manifest = {"workspaces": {"bob": {"path": str(recovery_args.sessions.parent)}}}
    else:
        manifest = {"hub_id": "other"}
    (recovery_args.sessions.parent / "run.json").write_text(json.dumps(manifest))
    recovery_args.force = True
    with pytest.raises(LAUNCHER.Stop):
        RECOVERY.prepare(recovery_args, RecoveryReader(), "bob")


@pytest.mark.parametrize("agent", ["alice", "bob"])
def test_real_isolated_hub_pending_question_schema_and_recommendation(
    recovery_args: argparse.Namespace,
    hub_store: HubStore,
    agent: str,
) -> None:
    question = hub_store.ask_user("Wait or proceed?", None, actor="alice", session="test-session")
    reader = RecoveryReader()
    reader.state["hub.status"] = hub_store.status_summary()
    reader.state["hub.snapshot"] = hub_store.snapshot() | {"hub_id": "hub-1"}
    reader.state["hub.questions"] = {"questions": hub_store.open_operator_questions()}
    workflow_id = reader.state["hub.snapshot"]["workflow"]["id"]
    save_rows(
        recovery_args,
        [
            row("start", agent=agent, workflow_id=workflow_id),
            row("exit", agent=agent, workflow_id=workflow_id),
        ],
    )
    if agent == "bob":
        with pytest.raises(LAUNCHER.Stop, match=f"question\\(s\\) {question}.*robomate answer"):
            RECOVERY.prepare(recovery_args, reader, agent)
        recovery_args.force = True
    _, prompt = RECOVERY.prepare(recovery_args, reader, agent)
    assert f'"question_id": {question}' in prompt
    assert hub_store.open_operator_questions()[0]["question_id"] == question
    assert hub_store.status_summary()["workflow"]["status"] == "escalated"


def test_unavailable_hub_refuses_recovery_with_restore_recommendation(
    recovery_args: argparse.Namespace,
    capsys: pytest.CaptureFixture[str],
) -> None:
    class OfflineReader(RecoveryReader):
        def call(self, method: str) -> dict[str, Any]:
            raise ConnectionError("hub unavailable")

    save_rows(recovery_args, [row("start", agent="alice"), row("exit", agent="alice")])
    before = recovery_args.resume_prompt.read_text()
    code = LAUNCHER.main(
        [
            "--recover",
            "--harness",
            "codex",
            "--hub-url",
            "http://unavailable",
            "--token-file",
            str(recovery_args.sessions.parent / "token"),
            "--sessions",
            str(recovery_args.sessions),
            "--resume-prompt",
            str(recovery_args.resume_prompt),
            "--",
            "must-never-launch-this-harness",
        ],
        OfflineReader(),
    )
    assert code == LAUNCHER.EXIT_HUB_UNREADABLE
    error = capsys.readouterr().err
    assert "hub unavailable" in error and "Restore" in error and "hub reachability" in error
    assert recovery_args.resume_prompt.read_text() == before


def test_budget_stop_timestamp_is_never_used_as_process_exit(
    recovery_args: argparse.Namespace,
) -> None:
    save_rows(recovery_args, [row("manual_resume"), row("stop", reason="resumes_spent")])
    recovery_args.confirm_stopped = True
    _, prompt = RECOVERY.prepare(recovery_args, RecoveryReader(), "bob")
    assert "unknown (not recorded)" in prompt


@pytest.mark.parametrize("harness", ["codex", "antigravity", "devin"])
def test_existing_home_without_mcp_configuration_refuses_recovery(
    recovery_args: argparse.Namespace,
    harness: str,
) -> None:
    recovery_args.harness = harness
    recovery_args.recovery_config = recovery_args.sessions.parent / "empty-home"
    recovery_args.recovery_config.mkdir()
    recovery_args.force = True
    with pytest.raises(LAUNCHER.Stop, match="Restore that run's configuration"):
        RECOVERY.prepare(recovery_args, RecoveryReader(), "bob")


def test_windows_shell_selection_uses_git_bash_instead_of_wsl(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    git = tmp_path / "Git/cmd/git.exe"
    git.parent.mkdir(parents=True)
    git.touch()
    bash = tmp_path / "Git/bin/bash.exe"
    bash.parent.mkdir(parents=True)
    bash.touch()
    wsl = tmp_path / "Windows/System32/bash.exe"
    monkeypatch.setattr(RECOVERY.shutil, "which", lambda name: str(git if name == "git" else wsl))
    assert RECOVERY.bash_executable(windows=True) == str(bash)
    bash.unlink()
    with pytest.raises(OSError, match="Git Bash is unavailable"):
        RECOVERY.bash_executable(windows=True)


@pytest.mark.parametrize(
    "harness,config_file",
    [("antigravity", ".gemini/config/mcp_config.json"), ("devin", "mcp_config.local.json")],
)
def test_ask_backup_blocks_recovery_before_hub_reads(
    recovery_args: argparse.Namespace, harness: str, config_file: str
) -> None:
    recovery_args.harness = harness
    home = recovery_args.sessions.parent / "config-home"
    config = home / config_file
    config.parent.mkdir(parents=True)
    config.write_text('{"mcpServers": {}}')
    backup = config.with_name(config.name + ".ask-backup")
    original = '{"mcpServers": {"robomate": {"command": "must-not-run"}}}'
    backup.write_text(original)
    recovery_args.recovery_config = home
    recovery_args.force = True
    reader = RecoveryReader()
    with pytest.raises(LAUNCHER.Stop) as refused:
        RECOVERY.prepare(recovery_args, reader, "bob")
    assert refused.value.reason == "config"
    assert str(backup) in str(refused.value)
    assert "restore" in str(refused.value)
    assert reader.calls == []
    assert backup.read_text() == original
    assert json.loads(config.read_text()) == {"mcpServers": {}}


@pytest.mark.parametrize(
    "value,exact",
    [
        ("brisk-otter", True),
        ("abc12345", True),
        ("-c", False),
        ("--continue", False),
        ("a b", False),
        ("x", False),
    ],
)
def test_devin_session_ids_are_names_never_flags(value: str, exact: bool) -> None:
    """#224: documented IDs are short names or hex prefixes; never ``-c``."""
    assert RECOVERY.exact_id("devin", value) is exact


def _null_row(event: str, **changes: Any) -> dict[str, Any]:
    base = row(event, **changes)
    base["workflow_id"] = None
    return base


def _hub_with_created(
    reader: RecoveryReader, created: str, run_id: str = "run-1"
) -> RecoveryReader:
    for method in ("hub.status", "hub.snapshot"):
        workflow = reader.state[method]["workflow"]
        assert isinstance(workflow, dict)
        workflow["created"] = created
        workflow["run_id"] = run_id
    reader.state["hub.status"]["queue"] = []
    reader.state["hub.status"]["queue_length"] = 0
    reader.state["hub.status"]["tasks"] = []
    return reader


def test_null_workflow_log_recovers_from_hub_and_pins_later_rows(
    recovery_args: argparse.Namespace,
) -> None:
    """#223: a never-relaunched worker takes its workflow ID from the hub."""

    save_rows(
        recovery_args,
        [
            _null_row("start", timestamp="2026-10-08T11:00:00Z"),
            _null_row("exit", timestamp="2026-10-08T11:05:00Z"),
        ],
    )
    reader = _hub_with_created(RecoveryReader(), "2026-10-08T11:02:00Z")
    conversation, text = RECOVERY.prepare(recovery_args, reader, "bob")
    assert conversation == CODEX_ID
    assert "wf-1" in text and "taken from hub" in text
    logged = [json.loads(line) for line in recovery_args.sessions.read_text().splitlines()]
    assert logged[-1]["event"] == "recovery" and logged[-1]["workflow_id"] == "wf-1"
    # A relaunched launcher now pins the derived ID from the log alone.
    second_reader = _hub_with_created(RecoveryReader(), "2026-10-08T11:02:00Z")
    again, second_text = RECOVERY.prepare(recovery_args, second_reader, "bob")
    assert again == CODEX_ID
    assert "taken from hub" not in second_text


def test_null_log_refuses_when_created_outside_conversation_window(
    recovery_args: argparse.Namespace,
) -> None:
    """#223 condition 3: earlier done or later workflows must not be picked."""

    for created, reason in [
        ("2026-10-08T10:50:00Z", "before start"),
        ("2026-10-08T11:15:00Z", "after last exit"),
    ]:
        save_rows(
            recovery_args,
            [
                _null_row("start", timestamp="2026-10-08T11:00:00Z"),
                _null_row("exit", timestamp="2026-10-08T11:05:00Z"),
            ],
        )
        reader = _hub_with_created(RecoveryReader(), created)
        recovery_args.force = True
        with pytest.raises(LAUNCHER.Stop, match="condition 3"):
            RECOVERY.prepare(recovery_args, reader, "bob")
        assert reason in ("before start", "after last exit")


def test_null_log_refuses_on_hub_mismatch_and_missing_workflow(
    recovery_args: argparse.Namespace,
) -> None:
    """#223/#299 conditions 1-2: same hub, live workflow; disagreement or non-live refused."""

    save_rows(
        recovery_args,
        [
            _null_row("start", timestamp="2026-10-08T11:00:00Z"),
            _null_row("exit", timestamp="2026-10-08T11:05:00Z"),
        ],
    )
    other_hub = _hub_with_created(RecoveryReader(), "2026-10-08T11:02:00Z")
    other_hub.state["hub.info"]["hub_id"] = "other"
    with pytest.raises(LAUNCHER.Stop, match="condition 1"):
        RECOVERY.prepare(recovery_args, other_hub, "bob")
    no_workflow = _hub_with_created(RecoveryReader(), "2026-10-08T11:02:00Z")
    no_workflow.state["hub.status"]["workflow"] = None
    no_workflow.state["hub.snapshot"]["workflow"] = None
    with pytest.raises(LAUNCHER.Stop, match="condition 2"):
        RECOVERY.prepare(recovery_args, no_workflow, "bob")
    disagreed_id = _hub_with_created(RecoveryReader(), "2026-10-08T11:02:00Z")
    disagreed_id.state["hub.snapshot"]["workflow"]["id"] = "wf-other"
    with pytest.raises(LAUNCHER.Stop, match="condition 2"):
        RECOVERY.prepare(recovery_args, disagreed_id, "bob")
    disagreed_run = _hub_with_created(RecoveryReader(), "2026-10-08T11:02:00Z")
    disagreed_run.state["hub.snapshot"]["workflow"]["run_id"] = "run-other"
    with pytest.raises(LAUNCHER.Stop, match="condition 2"):
        RECOVERY.prepare(recovery_args, disagreed_run, "bob")
    non_live = _hub_with_created(RecoveryReader(), "2026-10-08T11:02:00Z")
    non_live.workflow("done")
    with pytest.raises(LAUNCHER.Stop, match="condition 2"):
        RECOVERY.prepare(recovery_args, non_live, "bob")
    save_rows(
        recovery_args,
        [
            _null_row("start", timestamp="2026-10-08T11:00:00Z", run_id="run-old"),
            _null_row("exit", timestamp="2026-10-08T11:05:00Z", run_id="run-old"),
        ],
    )
    with pytest.raises(LAUNCHER.Stop, match="condition 2"):
        RECOVERY.prepare(
            recovery_args, _hub_with_created(RecoveryReader(), "2026-10-08T11:02:00Z"), "bob"
        )
    save_rows(
        recovery_args,
        [
            _null_row("start", timestamp="2026-10-08T11:00:00Z"),
            _null_row("exit", timestamp="2026-10-08T11:05:00Z"),
        ],
    )
    recovery_args.workflow_id = "other"
    with pytest.raises(LAUNCHER.Stop, match="Conflicting|differs"):
        RECOVERY.prepare(
            recovery_args, _hub_with_created(RecoveryReader(), "2026-10-08T11:02:00Z"), "bob"
        )


def test_done_live_and_queued_workflows_pick_live_one(
    recovery_args: argparse.Namespace,
    hub_store: HubStore,
) -> None:
    """#299 item 3: an earlier done workflow, a live one and a queued one picks the live one."""

    from agent_hub_common import WorkflowStatus

    hub_store.set_workflow_status(WorkflowStatus.DONE, "done", actor="alice", session="s-1")
    live_id = hub_store.initialize_workflow("Live workflow goal", {})
    queued_id = hub_store.start_workflow(
        "Queued workflow goal", {}, source="cli", force_queued=True
    )
    assert live_id != queued_id
    reader = RecoveryReader()
    reader.state["hub.status"] = hub_store.status_summary()
    reader.state["hub.snapshot"] = hub_store.snapshot() | {"hub_id": "hub-1"}
    assert reader.state["hub.status"]["queue_length"] == 1
    created = reader.state["hub.status"]["workflow"]["created"]
    save_rows(
        recovery_args,
        [
            _null_row("start", timestamp=created),
            _null_row("exit", timestamp=created),
        ],
    )
    conversation, text = RECOVERY.prepare(recovery_args, reader, "bob")
    assert conversation == CODEX_ID
    assert f"Workflow: {live_id} (active) (taken from hub" in text
    assert f"Workflow: {queued_id}" not in text
    logged = [json.loads(line) for line in recovery_args.sessions.read_text().splitlines()]
    assert logged[-1]["event"] == "recovery" and logged[-1]["workflow_id"] == live_id


def test_alice_hub_evidence_uses_orchestrator_activity_session(
    recovery_args: argparse.Namespace,
    hub_store: HubStore,
) -> None:
    """#299 item 1: Alice's session lives in status['orchestrator_activity']['session']."""

    hub_store.note_orchestrator_session("alice", "alice-session-1")
    reader = RecoveryReader()
    status = hub_store.status_summary()
    status["orchestrator_activity"]["holding"] = None
    status["orchestrator_activity"]["heartbeat_age_s"] = None
    snapshot = hub_store.snapshot() | {"hub_id": "hub-1"}
    assert "orchestrator" not in status and "orchestrator" not in snapshot
    assert status["orchestrator_activity"]["session"] == "alice-session-1"
    status["workflow"]["created"] = "2026-10-08T10:58:00Z"
    snapshot["workflow"]["created"] = "2026-10-08T10:58:00Z"
    reader.state["hub.status"] = status
    reader.state["hub.snapshot"] = snapshot
    save_rows(
        recovery_args,
        [
            _null_row("start", agent="alice", timestamp="2026-10-08T11:00:00Z"),
            _null_row("exit", agent="alice", timestamp="2026-10-08T11:05:00Z"),
        ],
    )
    _, text = RECOVERY.prepare(recovery_args, reader, "alice")
    assert "orchestrator session on the hub workflow" in text
    assert "backward-clock tolerance" in text
    assert "on timing alone" not in text


def test_trailing_resume_without_exit_leaves_conversation_window_open(
    recovery_args: argparse.Namespace,
) -> None:
    """#299 item 2: start, exit, wait, resume with no exit leaves the window open."""

    save_rows(
        recovery_args,
        [
            _null_row("start", timestamp="2026-10-08T11:00:00Z"),
            _null_row("exit", timestamp="2026-10-08T11:05:00Z"),
            _null_row("wait", timestamp="2026-10-08T11:05:00Z"),
            _null_row("resume", timestamp="2026-10-08T11:06:00Z"),
        ],
    )
    recovery_args.confirm_stopped = True
    reader = _hub_with_created(RecoveryReader(), "2026-10-08T11:12:00Z")
    _, text = RECOVERY.prepare(recovery_args, reader, "bob")
    assert "wf-1" in text and "taken from hub" in text
    assert "backward-clock tolerance" not in text


def test_stale_agent_row_from_earlier_workflow_is_not_evidence(
    recovery_args: argparse.Namespace,
) -> None:
    """#299 item 4: a worker row from an earlier workflow does not count as evidence."""

    rows = [
        _null_row("start", timestamp="2026-10-08T11:00:00Z"),
        _null_row("exit", timestamp="2026-10-08T11:05:00Z"),
    ]
    stale_row = {
        "name": "bob",
        "standing_role": "implementer",
        "status": "idle",
        "alive": False,
        "activity": {"instance": None},
    }
    for agent_entry in (
        stale_row,
        stale_row | {"checked_in": "2026-10-08T09:00:00Z"},
    ):
        save_rows(recovery_args, rows)
        reader = _hub_with_created(RecoveryReader(), "2026-10-08T10:58:00Z")
        reader.state["hub.status"]["agents"] = [agent_entry]
        reader.state["hub.snapshot"]["tasks"] = []
        with pytest.raises(LAUNCHER.Stop, match="condition 3"):
            RECOVERY.prepare(recovery_args, reader, "bob")

    save_rows(recovery_args, rows)
    fresh = _hub_with_created(RecoveryReader(), "2026-10-08T10:58:00Z")
    fresh.state["hub.status"]["agents"] = [stale_row | {"checked_in": "2026-10-08T11:00:00Z"}]
    fresh.state["hub.snapshot"]["tasks"] = []
    _, text = RECOVERY.prepare(recovery_args, fresh, "bob")
    assert "agent row for bob on the hub workflow" in text
    assert "backward-clock tolerance" in text


def test_alice_log_with_late_workflow_id_uses_saved_pin(
    recovery_args: argparse.Namespace,
) -> None:
    """#223: Alice's first saved ID at a later stop row needs no hub fallback."""

    save_rows(
        recovery_args,
        [
            row("start", agent="alice", workflow_id=None, timestamp="2026-10-08T11:00:00Z"),
            row("exit", agent="alice", workflow_id=None, timestamp="2026-10-08T11:05:00Z"),
            row("stop", agent="alice", timestamp="2026-10-08T11:06:00Z"),
        ],
    )
    _, text = RECOVERY.prepare(recovery_args, RecoveryReader(), "alice")
    assert "taken from hub" not in text


@pytest.mark.parametrize(
    ("end", "created"),
    [
        ("2026-10-08T11:05:00Z", "2026-10-08T10:58:00Z"),
        # #398: a step during the launch leaves its end before its start.
        ("2026-10-08T10:59:58Z", "2026-10-08T11:00:00Z"),
    ],
    ids=["created-before-start", "end-before-start"],
)
def test_backward_clock_tolerated_only_with_hub_evidence(
    recovery_args: argparse.Namespace,
    end: str,
    created: str,
) -> None:
    """#223/#209: prefer hub-side evidence over a tight timestamp comparison."""

    save_rows(
        recovery_args,
        [
            _null_row("start", timestamp="2026-10-08T11:00:00Z"),
            _null_row("exit", timestamp=end),
        ],
    )
    skewed = _hub_with_created(RecoveryReader(), created)
    _, text = RECOVERY.prepare(recovery_args, skewed, "bob")
    assert "backward-clock tolerance" in text and "taken from hub" in text
    save_rows(
        recovery_args,
        [
            _null_row("start", timestamp="2026-10-08T11:00:00Z"),
            _null_row("exit", timestamp=end),
        ],
    )
    lonely = _hub_with_created(RecoveryReader(), created)
    lonely.state["hub.status"]["agents"] = []
    lonely.state["hub.snapshot"]["tasks"] = []
    with pytest.raises(LAUNCHER.Stop, match="condition 3"):
        RECOVERY.prepare(recovery_args, lonely, "bob")


def test_interrupted_stop_without_exit_recovers_with_confirm(
    recovery_args: argparse.Namespace,
) -> None:
    """#223: Ctrl-C stop/interrupted without exit still takes the hub ID with --confirm-stopped."""

    save_rows(
        recovery_args,
        [
            _null_row("start", timestamp="2026-10-08T11:00:00Z"),
            _null_row("stop", timestamp="2026-10-08T11:04:00Z", reason="interrupted"),
        ],
    )
    with pytest.raises(LAUNCHER.Stop, match="--confirm-stopped"):
        RECOVERY.prepare(
            recovery_args, _hub_with_created(RecoveryReader(), "2026-10-08T11:02:00Z"), "bob"
        )
    recovery_args.confirm_stopped = True
    _, text = RECOVERY.prepare(
        recovery_args, _hub_with_created(RecoveryReader(), "2026-10-08T11:02:00Z"), "bob"
    )
    assert "taken from hub" in text


def test_hub_status_and_snapshot_expose_created_without_version_bumps(
    hub_store: HubStore,
) -> None:
    """#223: status and snapshot expose additive created field while wire version stays 1."""

    from agent_hub_common import SCHEMA_VERSION as WIRE_VERSION

    assert WIRE_VERSION == 1
    status = hub_store.status_summary()
    snapshot = hub_store.snapshot()
    assert status["workflow"] is not None and snapshot["workflow"] is not None
    assert isinstance(status["workflow"]["created"], str)
    assert isinstance(snapshot["workflow"]["created"], str)
    assert status["workflow"]["created"] == snapshot["workflow"]["created"]


def test_launcher_exit_row_pins_workflow_seen_during_run(tmp_path: Path) -> None:
    """#223 cheap launcher improvement: exit records a workflow seen mid-run."""

    class MidRunReader:
        def read(self) -> tuple[str, dict[str, Any] | None]:
            return "hub-1", {"id": "wf-1", "status": "active"}

    args = argparse.Namespace(read_retries=1, read_retry_delay_s=0.0)
    launcher = LAUNCHER.Launcher.__new__(LAUNCHER.Launcher)
    launcher.args = args
    launcher.reader = MidRunReader()
    launcher.hub_id = "hub-1"
    launcher.workflow_id = None
    launcher.interrupted = False
    launcher.refresh_workflow_id_best_effort()
    assert launcher.workflow_id == "wf-1"


def test_interactive_launch_keeps_windows_git_bash_guard(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    interactive = importlib.import_module("agent_interactive")
    git = tmp_path / "Git/cmd/git.exe"
    git.parent.mkdir(parents=True)
    git.touch()
    bash = tmp_path / "Git/bin/bash.exe"
    bash.parent.mkdir(parents=True)
    bash.touch()
    wsl = tmp_path / "Windows/System32/bash.exe"
    monkeypatch.setattr(RECOVERY.shutil, "which", lambda name: str(git if name == "git" else wsl))
    resolver = RECOVERY.bash_executable
    monkeypatch.setattr(RECOVERY, "bash_executable", lambda: resolver(windows=True))
    argv = interactive.launch_command("harness", None, False, [], False)
    assert argv == [str(bash), "-c", "harness"]
    bash.unlink()
    with pytest.raises(OSError, match="Git Bash is unavailable"):
        interactive.launch_command("harness", None, False, [], False)


def test_interactive_powershell_quotes_all_single_quote_delimiters(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    interactive = importlib.import_module("agent_interactive")
    monkeypatch.setattr(interactive.shutil, "which", lambda _: "pwsh")
    word = "don't ‘x’ ‚x‛"
    argv = interactive.launch_command(
        "harness __ROBOMATE_HARNESS_ARGS__", None, False, [word], True
    )
    assert argv[-1] == "harness 'don''t ‘‘x’’ ‚‚x‛‛'\nexit $LASTEXITCODE"


def test_interactive_unicode_arguments_do_not_depend_on_console_encoding(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    interactive = importlib.import_module("agent_interactive")
    monkeypatch.setattr(interactive.shutil, "which", lambda _: "pwsh")
    output = io.BytesIO()
    console = io.TextIOWrapper(output, encoding="ascii")
    monkeypatch.setattr(sys, "stdout", console)
    interactive.launch_command("harness __ROBOMATE_HARNESS_ARGS__", None, False, ["don’t ‛"], True)
    console.flush()
    assert b"\\u2019" in output.getvalue() and b"\\u201b" in output.getvalue()


def test_windows_shell_selection_prefers_msys_over_git_native_launcher(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    git = tmp_path / "Git/cmd/git.exe"
    launcher = tmp_path / "Git/bin/bash.exe"
    msys = tmp_path / "Git/usr/bin/bash.exe"
    for executable in (git, launcher, msys):
        executable.parent.mkdir(parents=True, exist_ok=True)
        executable.touch()
    monkeypatch.setattr(
        RECOVERY.shutil, "which", lambda name: str(git if name == "git" else launcher)
    )
    assert RECOVERY.bash_executable(windows=True) == str(msys)
    interactive = importlib.import_module("agent_interactive")
    resolver = RECOVERY.bash_executable
    monkeypatch.setattr(RECOVERY, "bash_executable", lambda: resolver(windows=True))
    assert interactive.launch_command("harness", None, False, [], False)[0] == str(msys)


def _detached(status: dict[str, Any], agent: str) -> dict[str, Any]:
    """The predecessor has exited: its bridge no longer holds an instance."""

    for entry in status["agents"]:
        if entry["name"] == agent:
            entry["activity"]["instance"] = None
    return status


def _skewed(status: dict[str, Any], snapshot: dict[str, Any]) -> list[dict[str, Any]]:
    """Workflow created 2 minutes before the log's start: only the 300 s tolerance admits it.

    Only `created` is moved (log times cannot lie in the future); the tasks and
    agents stay as the real HubStore payloads report them.
    """

    for payload in (status, snapshot):
        payload["workflow"]["created"] = "2026-10-08T10:58:00Z"
    return [
        _null_row("start", timestamp="2026-10-08T11:00:00Z"),
        _null_row("exit", timestamp="2026-10-08T11:05:00Z"),
    ]


def test_stale_task_from_earlier_workflow_is_not_evidence(
    recovery_args: argparse.Namespace,
    hub_store: HubStore,
) -> None:
    """#299 r1-1: an open task left on a done workflow never vouches for the live one."""

    from agent_hub_common import WorkflowStatus

    hub_store.check_in("bob")
    stale = hub_store.assign_task("bob", "implementer", "Old", "old work")
    hub_store.set_workflow_status(WorkflowStatus.DONE, "done", actor="alice", session="s-1")
    live_id = hub_store.initialize_workflow("Live workflow goal", {})
    assert stale.workflow_id != live_id
    status = _detached(hub_store.status_summary(), "bob")
    snapshot = hub_store.snapshot() | {"hub_id": "hub-1"}
    # Both real payloads list the stale open task, naming its own workflow.
    assert [(t["id"], t["workflow_id"]) for t in status["tasks"]] == [(stale.id, stale.workflow_id)]
    assert [(t["id"], t["workflow_id"]) for t in snapshot["tasks"]] == [
        (stale.id, stale.workflow_id)
    ]
    reader = RecoveryReader()
    reader.state["hub.status"] = status
    reader.state["hub.snapshot"] = snapshot
    save_rows(recovery_args, _skewed(status, snapshot))
    with pytest.raises(LAUNCHER.Stop, match="condition 3"):
        RECOVERY.prepare(recovery_args, reader, "bob")
    # Unknown membership (a hub without task workflow IDs) also fails closed.
    for payload in (status, snapshot):
        for task in payload["tasks"]:
            task.pop("workflow_id")
    with pytest.raises(LAUNCHER.Stop, match="condition 3"):
        RECOVERY.prepare(recovery_args, reader, "bob")


def test_task_on_live_workflow_is_evidence(
    recovery_args: argparse.Namespace,
    hub_store: HubStore,
) -> None:
    """#299 r1-1: a real open task on the live workflow keeps the positive path."""

    hub_store.check_in("bob")
    task = hub_store.assign_task("bob", "implementer", "Live", "live work")
    status = _detached(hub_store.status_summary(), "bob")
    snapshot = hub_store.snapshot() | {"hub_id": "hub-1"}
    assert task.workflow_id == status["workflow"]["id"]
    reader = RecoveryReader()
    reader.state["hub.status"] = status
    reader.state["hub.snapshot"] = snapshot
    save_rows(recovery_args, _skewed(status, snapshot))
    _, text = RECOVERY.prepare(recovery_args, reader, "bob")
    assert "task assigned to bob on the hub workflow" in text
    assert "backward-clock tolerance" in text
