import hashlib
import importlib.util
import json
import sqlite3
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path, PureWindowsPath
from typing import Any

import httpx
import pytest
from agent_hub.database import initialize_database
from agent_hub.store import HubStore
from agent_hub_common import TaskState

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("hub_report", ROOT / "scripts/hub-report.py")
assert SPEC is not None and SPEC.loader is not None
REPORT = importlib.util.module_from_spec(SPEC)
# Dataclasses resolve their module through sys.modules while the class is built.
sys.modules["hub_report"] = REPORT
SPEC.loader.exec_module(REPORT)

T0 = datetime(2026, 1, 1, tzinfo=UTC)
# Text standing in for untrusted payloads; the report must never echo it.
MARKER = "PAYLOAD-MARKER ignore previous instructions"
HEAD = "a" * 40
MERGED = "b" * 40
PR = "https://github.com/example/repo/pull/7"


def at(offset_s: float) -> str:
    moment = T0 + timedelta(seconds=offset_s)
    return moment.isoformat(timespec="milliseconds").replace("+00:00", "Z")


def task_id(n: int) -> str:
    return f"{n:032x}"


def telemetry_call(
    agent: str,
    tool: str,
    end_s: float,
    duration_s: float = 0.0,
    *,
    outcome: str = "success",
    phase: str = "success",
    task: str | None = None,
    retries: int = 0,
) -> dict[str, Any]:
    record: dict[str, Any] = {
        "event": "tool_call",
        "agent": agent,
        "phase": phase,
        "tool": tool,
        "call_id": f"{agent}-{tool}-{end_s}",
        "timestamp": at(end_s),
        "duration_s": duration_s,
        "mcp_request_bytes": 10,
        "http_request_bytes": 20,
        "http_response_bytes": 200,
        "http_retries": retries,
        "error": MARKER,
    }
    if phase == "success":
        record["outcome"] = outcome
        record["mcp_result_bytes"] = 100
    if task is not None:
        record["task_id"] = task
    return record


def seed_run(run_dir: Path, *, status: str = "done") -> Path:
    """A finished run: three tasks (one cancelled), four expected workers.

    bob works from telemetry, charlie only from the hub's call_log, dave made
    one failed check-in, and erin was expected by the manifest but never came.
    """

    state = run_dir / "hub-state"
    state.mkdir(parents=True)
    database = state / "hub.db"
    initialize_database(database)
    policy = {"max_wall_minutes": 60, "max_review_rounds": 3, "allow_no_ci": False}
    connection = sqlite3.connect(database)
    with connection:
        connection.execute(
            "INSERT INTO workflow (id, goal, status, policy_json, created) VALUES (?, ?, ?, ?, ?)",
            ("f" * 32, "Address example/repo#7\nand merge it", status, json.dumps(policy), at(0)),
        )
        for name, harness in (("bob", "claude-code"), ("charlie", "codex")):
            connection.execute(
                "INSERT INTO agent (name, status, context_id, last_seen, harness) "
                "VALUES (?, 'released', ?, ?, ?)",
                (name, f"ctx-{name}", at(1560), harness),
            )
        tasks = [
            (1, "bob", "implementer", "completed", 60, 660,
             {"outcome": "completed", "head_sha": HEAD, "pr_url": PR, "summary": MARKER}),
            (2, "charlie", "reviewer", "completed", 720, 1200,
             {"verdict": "changes_requested", "reviewed_head_sha": HEAD,
              "blocking_findings": [{"id": "r1-1", "text": MARKER}], "summary": MARKER}),
            (3, "bob", "implementer", "canceled", 1260, 1500, None),
        ]  # fmt: skip
        for n, assignee, role, state_, created, updated, result in tasks:
            connection.execute(
                "INSERT INTO task (id, workflow_id, assignee, role, title, instructions, state,"
                " lease_duration_s, result_json, created, updated, pr_head_sha)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, 1800, ?, ?, ?, ?)",
                (
                    task_id(n),
                    "f" * 32,
                    assignee,
                    role,
                    f"{role.upper()} for #7\x1b[31m",
                    MARKER,
                    state_,
                    None if result is None else json.dumps(result),
                    at(created),
                    at(updated),
                    HEAD if role == "reviewer" else None,
                ),
            )
        messages = [
            (1, "bob", "to_alice", "progress"),
            (1, "bob", "to_alice", "progress"),
            (1, "bob", "to_alice", "question"),
            (1, "alice", "from_alice", "reply"),
            (2, "charlie", "to_alice", "progress"),
            (2, "alice", "from_alice", "assignment"),
        ]
        for n, sender, direction, kind in messages:
            parts = [{"kind": "text", "text": MARKER, "metadata": {"hub.kind": kind}}]
            connection.execute(
                "INSERT INTO message (task_id, context_id, sender, direction, parts_json, ts)"
                " VALUES (?, 'ctx', ?, ?, ?, ?)",
                (task_id(n), sender, direction, json.dumps(parts), at(100)),
            )
        for kind in ("agent_checked_in", "agent_checked_in", "task_completed"):
            connection.execute(
                "INSERT INTO event (kind, payload_json, state, ts) VALUES (?, ?, 'acked', ?)",
                (kind, json.dumps({"text": MARKER}), at(10)),
            )
        gate = {
            "pr_url": PR,
            "pr_state": "open",
            "expected_head_sha": HEAD,
            "current_head_sha": HEAD,
            "head_matches": True,
            "ci": "pass",
            "checks": [{"name": MARKER, "bucket": "pass", "link": ""}],
            "base_behind_main": False,
            "mergeable": "clean",
        }
        decisions = [
            (5, "plan", "Plan for #7", MARKER),
            (2000, "gate:final", "Final gate reading", f"{MARKER} {json.dumps(gate)}"),
            (2100, None, "Merged the PR", f"{MARKER}; merged sha {MERGED}"),
            (3000, None, "Done", "Workflow status set to done"),
        ]
        for ts, key, summary, rationale in decisions:
            connection.execute(
                "INSERT INTO decision (ts, summary, rationale, key) VALUES (?, ?, ?, ?)",
                (at(ts), summary, rationale, key),
            )
        call_log = [
            # Alice over MCP: one empty hold, one hold that returned an event.
            ("mcp", "alice", "initialize", "ok", 100, 200, None, 0, 0),
            ("mcp", "alice", "wait_for_event", "null_event", 50, 60, 20, 1000, 1100),
            ("mcp", "alice", "wait_for_event", "event", 50, 500, 400, 1100, 1150),
            ("mcp", "alice", "assign_task", "ok", 800, 300, 250, 1150, 1151),
            ("mcp", "alice", "check_merge_gate", "ok", 100, 900, 800, 2000, 2010),
            # bob's wire as the hub saw it, heartbeats included.
            ("a2a", "bob", "heartbeat", "ok", 50, 40, None, 100, 100),
            ("a2a", "bob", "heartbeat", "ok", 50, 40, None, 400, 400),
            ("a2a", "bob", "check_in", "ok", 600, 300, None, 30, 30),
            # charlie has no telemetry: the hub's record is all there is.
            ("a2a", "charlie", "check_in", "ok", 100, 10, None, 20, 20),
            ("a2a", "charlie", "await_assignment", "assignment", 100, 10, None, 20, 720),
            ("a2a", "charlie", "submit_result", "ok", 100, 10, None, 1200, 1200),
            ("a2a", "charlie", "await_assignment", "timeout", 100, 10, None, 1200, 1300),
            ("a2a", "charlie", "await_assignment", "release", 100, 10, None, 1300, 1560),
        ]
        for boundary, actor, tool, outcome, b_in, b_out, content, start, end in call_log:
            connection.execute(
                "INSERT INTO call_log (boundary, actor, tool, outcome, bytes_in, bytes_out,"
                " content_bytes, started, finished) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (boundary, actor, tool, outcome, b_in, b_out, content, at(start), at(end)),
            )
    connection.close()

    bob = [
        {"event": "session_started", "agent": "bob", "timestamp": at(0)},
        telemetry_call("bob", "check_in", 30, outcome="registered"),
        telemetry_call("bob", "await_assignment", 90, 60, outcome="assignment", task=task_id(1)),
        telemetry_call("bob", "get_role_guide", 100),
        {"event": "heartbeat", "agent": "bob", "timestamp": at(120)},
        telemetry_call("bob", "report_progress", 300, retries=1, task=task_id(1)),
        telemetry_call("bob", "ask_alice", 400, 10, outcome="reply", task=task_id(1)),
        {"event": "heartbeat", "agent": "bob", "timestamp": at(420)},
        telemetry_call("bob", "submit_result", 660, task=task_id(1)),
        telemetry_call("bob", "await_assignment", 760, 100, outcome="timeout"),
        telemetry_call("bob", "await_assignment", 860, 100, outcome="timeout"),
        telemetry_call("bob", "await_assignment", 1260, 400, outcome="assignment", task=task_id(3)),
        {"event": "heartbeat", "agent": "bob", "timestamp": at(1300)},
        telemetry_call("bob", "await_assignment", 1560, 50, outcome="release"),
    ]
    dave = [telemetry_call("dave", "check_in", 5, phase="error")]
    for name, records in (("bob", bob), ("dave", dave)):
        lines = [json.dumps(record) for record in records]
        (run_dir / f"{name}-telemetry.jsonl").write_text(
            "\n".join(lines) + "\nnot json\n", encoding="utf-8"
        )
    manifest = {"harnesses": {"bob": "claude-code", "charlie": "codex", "erin": "codex"}}
    (run_dir / "run.json").write_text(json.dumps(manifest), encoding="utf-8")
    return state


def by_name(report: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {agent["name"]: agent for agent in report["agents"]}


def test_a_seeded_run_produces_known_totals(tmp_path: Path) -> None:
    state = seed_run(tmp_path)

    report = REPORT.build_report(state, now=T0 + timedelta(hours=2))
    agents = by_name(report)

    assert list(agents) == ["alice", "bob", "charlie", "dave", "erin"]
    bob = agents["bob"]
    assert bob["calls_source"] == "telemetry" and bob["wire_source"] == "call_log"
    assert bob["calls"] == {"total": 10, "active": 4, "waiting": 2, "other": 4}
    assert bob["timeouts"] == 2 and bob["retries"] == 1 and bob["heartbeats"] == 3
    assert bob["bytes"]["mcp"] == {"sent": 100, "received": 1000, "context": 1000}
    assert bob["bytes"]["a2a"] == {"sent": 700, "received": 380}
    assert bob["time_s"] == {"total": 1530.0, "turn": 810.0, "waiting": 200.0, "idle": 520.0}
    assert bob["released"] == at(1560)

    charlie = agents["charlie"]
    assert charlie["calls_source"] == "call_log"
    assert charlie["calls"] == {"total": 5, "active": 1, "waiting": 1, "other": 3}
    assert charlie["bytes"]["a2a"] == {"sent": 500, "received": 50}
    assert charlie["time_s"] == {"total": 1540.0, "turn": 480.0, "waiting": 100.0, "idle": 960.0}

    alice = agents["alice"]
    assert alice["calls"] == {"total": 5, "active": 4, "waiting": 1, "other": 0}
    assert alice["bytes"]["mcp"] == {"sent": 1100, "received": 1960, "context": 1470}
    assert alice["time_s"] == {"total": 2010.0, "turn": 1910.0, "waiting": 100.0, "idle": 0.0}

    # dave made one failed check-in; erin was expected and never came.
    assert agents["dave"]["checked_in"] is False
    assert agents["dave"]["calls"]["total"] == 1 and agents["dave"]["errors"] == 1
    erin = agents["erin"]
    assert erin["checked_in"] is False and erin["calls"]["total"] == 0
    assert erin["time_s"]["total"] is None

    totals = report["totals"]
    assert totals["calls"]["total"] == 21
    assert totals["bytes"]["mcp_total"] == sum(
        a["bytes"]["mcp"]["sent"] + a["bytes"]["mcp"]["received"] for a in agents.values()
    )
    assert totals["bytes"]["a2a_total"] == 700 + 380 + 500 + 50 + 20 + 200
    assert totals["time_s"]["turn"] == 810.0 + 480.0 + 1910.0

    tasks = {task["id"]: task for task in report["tasks"]}
    first = tasks[task_id(1)]
    assert first["outcome"] == "completed" and first["head_sha"] == HEAD and first["pr_url"] == PR
    assert first["picked_up"] == at(90) and first["wall_s"] == 600.0
    assert first["questions"] == 1 and first["progress_notes"] == 2
    assert first["title"] == "IMPLEMENTER for #7 [31m"
    review = tasks[task_id(2)]
    assert review["outcome"] == "changes_requested" and review["blocking_findings"] == 1
    cancelled = tasks[task_id(3)]
    assert cancelled["state"] == "canceled" and cancelled["outcome"] == "canceled"
    assert cancelled["wall_s"] == 240.0 and cancelled["open"] is False

    workflow = report["workflow"]
    assert workflow["elapsed_s"] == 3000.0 and workflow["wall_used_fraction"] == 0.833
    assert workflow["review_rounds_used"] == 1 and workflow["max_review_rounds"] == 3
    assert workflow["merge_gate_calls"] == 1
    [reading] = workflow["merge_gate_readings"]
    assert reading["ci"] == "pass" and reading["head_matches"] is True
    assert reading["current_head_sha"] == HEAD
    assert workflow["merged"]["sha"] == MERGED
    assert [d["key"] for d in workflow["decisions"]] == ["plan", "gate:final", None, None]
    assert workflow["events"] == {"agent_checked_in": 2, "task_completed": 1}
    assert report["report"]["in_progress"] is False
    assert "2 unreadable telemetry line(s) skipped" in report["report"]["notes"]


def test_active_waiting_and_idle_sum_to_total_time(tmp_path: Path) -> None:
    state = seed_run(tmp_path)

    report = REPORT.build_report(state, now=T0 + timedelta(hours=2))

    for agent in report["agents"]:
        time = agent["time_s"]
        if time["total"] is None:
            continue
        assert time["turn"] + time["waiting"] + time["idle"] == pytest.approx(time["total"])
    # bob's two timed-out holds are waiting; everything between assignment and
    # result is active.
    bob = by_name(report)["bob"]
    assert bob["calls"]["waiting"] == 2 and bob["time_s"]["waiting"] == 200.0


def test_the_classifier_counts_a_timeout_during_a_task_as_active() -> None:
    telemetry = REPORT.Telemetry()
    telemetry.calls["bob"] = [
        REPORT.Call("check_in", "registered", T0, T0),
        REPORT.Call("await_assignment", "timeout", T0, T0 + timedelta(seconds=100)),
        REPORT.Call(
            "ask_alice", "timeout", T0 + timedelta(seconds=200), T0 + timedelta(seconds=300)
        ),
        REPORT.Call(
            "await_assignment", "release", T0 + timedelta(seconds=400), T0 + timedelta(seconds=400)
        ),
    ]
    telemetry.released["bob"] = T0 + timedelta(seconds=400)
    tasks = [{"assignee": "bob", "picked_up": at(150), "created": at(150), "finished": at(350)}]
    snapshot: dict[str, list[Any]] = {"agent": [], "workflow": [], "call_log": []}

    [_, bob] = REPORT.agent_figures(snapshot, telemetry, tasks, [], T0)

    assert bob["calls"] == {"total": 4, "active": 1, "waiting": 1, "other": 2}
    assert bob["timeouts"] == 2
    assert bob["time_s"] == {"total": 400.0, "turn": 200.0, "waiting": 100.0, "idle": 100.0}


def test_a_workflow_in_progress_reports_partial_figures(tmp_path: Path) -> None:
    state = tmp_path / "hub-state"
    state.mkdir()
    initialize_database(state / "hub.db")
    empty = REPORT.build_report(state, telemetry_paths=[])
    assert empty["workflow"] is None and empty["tasks"] == []
    assert empty["report"]["in_progress"] is True
    REPORT.render_text(empty)

    store = HubStore(path=state / "hub.db")
    store.initialize_workflow(policy={"max_wall_minutes": 60})
    store.check_in("bob")
    task = store.assign_task("bob", "implementer", "IMPLEMENT for #7", MARKER)
    now = datetime.now(UTC) + timedelta(minutes=5)

    report = REPORT.build_report(state, telemetry_paths=[], now=now)

    [row] = report["tasks"]
    assert row["id"] == task.id and row["open"] is True and row["outcome"] is None
    assert row["wall_s"] is not None and row["wall_s"] >= 300
    assert report["workflow"]["status"] == "active"
    assert report["workflow"]["elapsed_s"] >= 300
    assert report["workflow"]["merged"] is None
    assert "(open)" in REPORT.render_text(report)
    assert "(in progress)" in REPORT.render_text(report, markdown=True)


def test_limits_left_out_of_the_stored_policy_report_their_defaults(tmp_path: Path) -> None:
    state = tmp_path / "hub-state"
    state.mkdir()
    initialize_database(state / "hub.db")
    store = HubStore(path=state / "hub.db")
    # Only what Alice supplies is stored; the hub enforces the §5 defaults.
    store.initialize_workflow()

    workflow = REPORT.build_report(state, telemetry_paths=[])["workflow"]

    assert workflow["max_wall_minutes"] == 120 and workflow["max_review_rounds"] == 3
    assert workflow["wall_used_fraction"] is not None
    assert {"max_wall_minutes", "max_review_rounds"} <= set(workflow["policy_defaults"])
    text = REPORT.render_text(REPORT.build_report(state, telemetry_paths=[]))
    assert "of max_wall_minutes 120" in text and "of max_review_rounds 3" in text


def test_tasks_ended_by_alice_report_their_terminal_outcome(tmp_path: Path) -> None:
    state = tmp_path / "hub-state"
    state.mkdir()
    initialize_database(state / "hub.db")
    store = HubStore(path=state / "hub.db")
    store.initialize_workflow()
    store.check_in("bob")
    outcomes = {}
    for override in (TaskState.FAILED, TaskState.CANCELED, None):
        task = store.assign_task("bob", "implementer", f"IMPLEMENT {override}", MARKER)
        if override is None:
            store.cancel_task(task.id)
        else:
            store.set_task_state(task.id, override, MARKER)
        outcomes[task.id] = "canceled" if override is None else override.value

    report = REPORT.build_report(state, telemetry_paths=[])

    assert {task["id"]: task["outcome"] for task in report["tasks"]} == outcomes
    assert MARKER not in REPORT.render_text(report)


async def test_a_live_hub_database_is_read_without_being_modified(
    tmp_path: Path, hub_store: HubStore, client: httpx.AsyncClient, capsys: Any
) -> None:
    hub_store.log_decision("Plan", MARKER, key="plan")
    hub_store.check_in("bob")
    hub_store.assign_task("bob", "implementer", "IMPLEMENT", MARKER)
    telemetry = tmp_path / "bob-telemetry.jsonl"
    telemetry.write_text(json.dumps(telemetry_call("bob", "check_in", 0)) + "\n")
    # A second connection holding the WAL open, as a serving hub's would.
    holder = sqlite3.connect(hub_store.path)
    holder.execute("PRAGMA journal_mode = WAL")
    holder.execute("SELECT count(*) FROM task").fetchone()

    def fingerprint() -> tuple[list[str], dict[str, str]]:
        dump = list(holder.iterdump())
        files = {
            path.name: hashlib.sha256(path.read_bytes()).hexdigest()
            for path in sorted(tmp_path.iterdir())
            if path.is_file()
        }
        return dump, files

    before = fingerprint()
    try:
        status = REPORT.main(["--state-dir", str(tmp_path), "--telemetry", str(telemetry)])
        assert status == 0
        assert fingerprint() == before
        # And the connection itself refuses writes.
        with pytest.raises(sqlite3.OperationalError):
            ro = sqlite3.connect(REPORT.database_uri(hub_store.path.resolve()), uri=True)
            ro.execute("INSERT INTO decision (ts, summary, rationale) VALUES ('', '', '')")
    finally:
        holder.close()
    assert MARKER not in capsys.readouterr().out
    assert (await client.get("/.well-known/agent-card.json")).status_code == 200


def test_json_output_parses_and_matches_the_text_form(tmp_path: Path, capsys: Any) -> None:
    state = seed_run(tmp_path)

    assert REPORT.main(["--state-dir", str(state), "--format", "json"]) == 0
    report = json.loads(capsys.readouterr().out)
    assert REPORT.main(["--state-dir", str(state)]) == 0
    text = capsys.readouterr().out
    assert REPORT.main(["--state-dir", str(state), "--format", "md"]) == 0
    markdown = capsys.readouterr().out

    # The generation time differs between runs; everything else is the same data.
    rebuilt = REPORT.build_report(state, now=REPORT.parse_ts(report["report"]["generated_at"]))
    assert json.loads(json.dumps(rebuilt)) == report
    assert REPORT.headline(report) in text and REPORT.headline(report) in markdown
    assert "bytes" in text.splitlines()[2] and "not tokens" in text.splitlines()[2]
    _, rows = REPORT.agent_rows(report)
    lines = text.splitlines()
    for row in rows:
        line = next(line for line in lines if line.startswith(row[0] + " "))
        # Status can hold spaces ("never checked in"); every figure after it cannot.
        assert line.split()[-14:] == row[2:]
    assert "| alice |" in markdown and "### Tasks" in markdown


def test_payload_text_is_never_printed(tmp_path: Path, capsys: Any) -> None:
    state = seed_run(tmp_path)

    for extra in ([], ["--format", "json"], ["--format", "md"], ["--no-labels"]):
        assert REPORT.main(["--state-dir", str(state), *extra]) == 0
        output = capsys.readouterr().out
        assert "PAYLOAD-MARKER" not in output
        assert "\x1b" not in output
    assert REPORT.main(["--state-dir", str(state), "--no-labels", "--format", "json"]) == 0
    report = json.loads(capsys.readouterr().out)
    assert {task["title"] for task in report["tasks"]} == {None}
    assert report["workflow"]["goal"] is None
    assert {(d["key"], d["summary"]) for d in report["workflow"]["decisions"]} == {(None, None)}


def test_state_paths_become_read_only_uris_on_windows_and_posix(tmp_path: Path) -> None:
    windows = PureWindowsPath("C:/my run/hub-state#1/hub.db")
    assert REPORT.database_uri(windows) == "file:///C:/my%20run/hub-state%231/hub.db?mode=ro"

    state = seed_run(tmp_path / "my run #1")
    report = REPORT.build_report(state)
    assert report["workflow"]["id"] == "f" * 32
