"""Manual recovery refuses guesses/duplicates and generates factual prompts (#173)."""

from __future__ import annotations

import argparse
import importlib
import json
import os
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
                "tasks": [{"id": "task-1", "assignee": "bob", "role": "reviewer"}],
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
    ],
)
def test_generated_controls_cannot_replace_the_pinned_run_or_command(control: list[str]) -> None:
    with pytest.raises(SystemExit) as error:
        RECOVERY.split_controls(
            [
                "--harness",
                "codex",
                "--recovery-options",
                *control,
                "--recovery-command",
                "--",
                "codex",
                "exec",
            ]
        )
    assert error.value.code == 2


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
