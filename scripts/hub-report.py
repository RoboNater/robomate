#!/usr/bin/env python3
"""Report what one workflow (one issue) cost and where its time went (#79).

Reads a run's hub state and the worker telemetry beside it, and prints
per-agent, per-task and per-workflow figures:

    uv run --locked python scripts/hub-report.py --state-dir /path/to/my-run/hub-state
    uv run --locked python scripts/hub-report.py --state-dir ... --format json
    uv run --locked python scripts/hub-report.py --state-dir ... --format md

The database is opened read-only through a SQLite URI, inside one read
transaction, so the report is safe to run while the hub is still serving the
workflow; an unfinished run reports what has happened so far. Like any reader
of a WAL database, it may leave SQLite's `hub.db-shm` and an empty `hub.db-wal`
beside `hub.db`, which the hub's next connection reuses and removes; `hub.db`
itself is never written.

Figures are serialized message-body bytes (#78), never tokens: the hub cannot
see a harness's token accounting. Payload text — task instructions, messages,
results, event payloads, decision rationales, telemetry errors — is never
printed; only sizes, counts, closed-set labels and validated identifiers. The
free-text labels Alice writes herself (the workflow goal, task titles, and
decision keys and summaries) are printed flattened to one line and truncated,
and `--no-labels` leaves them out as well.

Worker telemetry defaults to every `*-telemetry.jsonl` and `*.telemetry.jsonl`
beside the state directory, which is where `prepare-run.py` and `step6.py` put
it; `--telemetry` names files explicitly.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import re
import sqlite3
import sys
import unicodedata
from collections import defaultdict
from collections.abc import Iterable, Iterator, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path, PurePath
from typing import Any

from agent_hub_common import WorkflowPolicy
from pydantic import ValidationError

UNITS = (
    "Figures are serialized message-body bytes (hub call_log and worker telemetry, #78), "
    "not tokens and not billed cost: HTTP headers, TLS/TCP overhead and harness framing "
    "are excluded, and the byte-to-token ratio differs per provider."
)

ALICE = "alice"
TERMINAL_STATES = frozenset({"completed", "failed", "canceled"})
# Blocking holds; one that returns with nothing is time spent waiting.
HOLD_TOOLS = frozenset({"await_assignment", "wait_for_event"})
EMPTY_OUTCOMES = frozenset({"timeout", "null_event"})
SHA_RE = re.compile(r"^[0-9a-fA-F]{40}$")
PR_URL_RE = re.compile(r"^https://[A-Za-z0-9.-]+/[A-Za-z0-9-]+/[A-Za-z0-9_.-]+/pull/[1-9][0-9]*$")
# A worker name, tool name or outcome as the hub and worker-mcp write them.
LABEL_RE = re.compile(r"^[A-Za-z0-9_./:-]{1,64}$")
MERGED_SHA_RE = re.compile(
    r"merge(?:d|_commit|\s+commit)?(?:[\s_-]*(?:sha|oid|commit|as|at|to))*[\s:=`'\"(]*"
    r"([0-9a-f]{40})\b",
    re.IGNORECASE,
)
GATE_FIELDS = ("pr_state", "head_matches", "ci", "mergeable", "base_behind_main")
GATE_SHA_FIELDS = ("expected_head_sha", "current_head_sha")
LABEL_LIMIT = 120


def log(message: str) -> None:
    print(message, file=sys.stderr, flush=True)


# -- small helpers ------------------------------------------------------------


def parse_ts(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        moment = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return moment if moment.tzinfo is not None else moment.replace(tzinfo=UTC)


def iso(moment: datetime | None) -> str | None:
    if moment is None:
        return None
    return moment.astimezone(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def seconds(start: datetime | None, end: datetime | None) -> float | None:
    if start is None or end is None:
        return None
    return round(max(0.0, (end - start).total_seconds()), 3)


def label(value: Any, limit: int = LABEL_LIMIT) -> str | None:
    """One line of Alice-authored text, with control and format characters gone.

    Category C covers newlines, escapes and the bidi overrides that could make a
    terminal show something other than what is stored.
    """

    if not isinstance(value, str):
        return None
    text = "".join(" " if unicodedata.category(ch).startswith("C") else ch for ch in value)
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 3] + "..."


def closed(value: Any) -> str | None:
    """A closed-set label (name, tool, outcome), or None for anything else."""

    return value if isinstance(value, str) and LABEL_RE.fullmatch(value) else None


def sha(value: Any) -> str | None:
    return value.lower() if isinstance(value, str) and SHA_RE.fullmatch(value) else None


def pr_url(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    text = value.strip().rstrip("/")
    return text if PR_URL_RE.fullmatch(text) else None


def json_object(raw: Any) -> dict[str, Any] | None:
    if not isinstance(raw, str):
        return None
    try:
        value = json.loads(raw)
    except ValueError:
        return None
    return value if isinstance(value, dict) else None


def number(value: Any) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


def count(value: Any) -> int:
    return len(value) if isinstance(value, list) else 0


def embedded_objects(text: str) -> Iterator[dict[str, Any]]:
    """Every top-level JSON object found in free text, in order."""

    decoder = json.JSONDecoder()
    index = text.find("{")
    while index != -1:
        try:
            value, end = decoder.raw_decode(text, index)
        except ValueError:
            index = text.find("{", index + 1)
            continue
        if isinstance(value, dict):
            yield value
        index = text.find("{", end)


Interval = tuple[datetime, datetime]


def union(intervals: Iterable[Interval]) -> list[Interval]:
    merged: list[Interval] = []
    for start, end in sorted(i for i in intervals if i[1] > i[0]):
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    return merged


def clip(intervals: Iterable[Interval], lower: datetime, upper: datetime) -> list[Interval]:
    return union((max(s, lower), min(e, upper)) for s, e in intervals)


def subtract(intervals: list[Interval], removed: list[Interval]) -> list[Interval]:
    result: list[Interval] = []
    for start, end in intervals:
        pieces = [(start, end)]
        for r_start, r_end in removed:
            next_pieces = []
            for p_start, p_end in pieces:
                if r_end <= p_start or r_start >= p_end:
                    next_pieces.append((p_start, p_end))
                    continue
                if r_start > p_start:
                    next_pieces.append((p_start, r_start))
                if r_end < p_end:
                    next_pieces.append((r_end, p_end))
            pieces = next_pieces
        result.extend(pieces)
    return union(result)


def span(intervals: Iterable[Interval]) -> float:
    return round(sum((e - s).total_seconds() for s, e in intervals), 3)


def within(moment: datetime, intervals: Iterable[Interval]) -> bool:
    return any(start <= moment <= end for start, end in intervals)


# -- reading ------------------------------------------------------------------


def database_uri(path: PurePath) -> str:
    """A read-only SQLite URI for an absolute path, POSIX or Windows.

    `as_uri` percent-escapes what a URI cannot carry literally (`#`, `?`, `%`,
    spaces) and spells a drive path `file:///C:/...`, which SQLite accepts.
    """

    return path.as_uri() + "?mode=ro"


def read_database(path: Path) -> dict[str, Any]:
    """One consistent read-only snapshot of the tables the report needs.

    `mode=ro` refuses every write, and the URI comes from `Path.as_uri`, which
    spells a Windows drive path (`file:///C:/...`) as well as a POSIX one.
    """

    with contextlib.closing(sqlite3.connect(database_uri(path.resolve()), uri=True)) as connection:
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout = 5000")
        connection.execute("BEGIN")
        try:
            tables = {
                row["name"]
                for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
            }
            snapshot: dict[str, Any] = {
                "schema_version": connection.execute("PRAGMA user_version").fetchone()[0],
            }
            for table in ("workflow", "agent", "task", "message", "event", "decision", "call_log"):
                snapshot[table] = (
                    [dict(row) for row in connection.execute(f"SELECT * FROM {table}")]
                    if table in tables
                    else []
                )
            snapshot["has_call_log"] = "call_log" in tables
        finally:
            connection.execute("ROLLBACK")
    return snapshot


@dataclass(slots=True)
class Call:
    """One hub call an agent's model made, reduced to sizes and labels."""

    tool: str
    outcome: str
    start: datetime | None
    end: datetime | None
    mcp_sent: int = 0
    mcp_received: int = 0
    content: int = 0
    wire_sent: int = 0
    wire_received: int = 0
    retries: int = 0


@dataclass(slots=True)
class Telemetry:
    calls: dict[str, list[Call]] = field(default_factory=lambda: defaultdict(list))
    heartbeats: dict[str, int] = field(default_factory=lambda: defaultdict(int))
    assigned: dict[str, datetime] = field(default_factory=dict)
    released: dict[str, datetime] = field(default_factory=dict)
    skipped_lines: int = 0


def read_telemetry(paths: Iterable[Path]) -> Telemetry:
    """Worker tool calls from each `HUB_TELEMETRY_LOG`; unreadable lines are counted."""

    telemetry = Telemetry()
    pending: dict[tuple[str, str], tuple[str, datetime | None]] = {}
    for path in paths:
        with path.open(encoding="utf-8", errors="replace") as stream:
            for line in stream:
                record = json_object(line)
                agent = closed(record.get("agent")) if record else None
                if record is None or agent is None:
                    telemetry.skipped_lines += bool(line.strip())
                    continue
                moment = parse_ts(record.get("timestamp"))
                if record.get("event") == "heartbeat":
                    telemetry.heartbeats[agent] += 1
                    continue
                if record.get("event") != "tool_call":
                    continue
                tool = closed(record.get("tool")) or "unknown"
                call_id = str(record.get("call_id"))
                if record.get("phase") == "start":
                    pending[(agent, call_id)] = (tool, moment)
                    continue
                pending.pop((agent, call_id), None)
                duration = record.get("duration_s")
                start = moment
                if moment is not None and isinstance(duration, int | float):
                    start = datetime.fromtimestamp(moment.timestamp() - float(duration), UTC)
                outcome = (
                    "error"
                    if record.get("phase") == "error"
                    else closed(record.get("outcome")) or "success"
                )
                telemetry.calls[agent].append(
                    Call(
                        tool=tool,
                        outcome=outcome,
                        start=start,
                        end=moment,
                        mcp_sent=number(record.get("mcp_request_bytes")),
                        mcp_received=number(record.get("mcp_result_bytes")),
                        content=number(record.get("mcp_result_bytes")),
                        wire_sent=number(record.get("http_request_bytes")),
                        wire_received=number(record.get("http_response_bytes")),
                        retries=number(record.get("http_retries")),
                    )
                )
                task_id = record.get("task_id")
                if outcome == "assignment" and isinstance(task_id, str) and moment is not None:
                    telemetry.assigned.setdefault(task_id, moment)
                if outcome == "release" and moment is not None:
                    telemetry.released[agent] = moment
    # A call still in flight has a start and no finish; it counts, untimed.
    for (agent, _), (tool, moment) in pending.items():
        telemetry.calls[agent].append(Call(tool=tool, outcome="in_flight", start=moment, end=None))
    return telemetry


def default_telemetry(state_dir: Path) -> list[Path]:
    run_dir = state_dir.parent
    found = {*run_dir.glob("*-telemetry.jsonl"), *run_dir.glob("*.telemetry.jsonl")}
    return sorted(path for path in found if path.is_file())


def manifest_workers(state_dir: Path) -> list[str]:
    """Worker names a `prepare-run.py` manifest expects, whether or not they came."""

    try:
        manifest = json.loads((state_dir.parent / "run.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    harnesses = manifest.get("harnesses") if isinstance(manifest, dict) else None
    if not isinstance(harnesses, dict):
        return []
    return [name for name in harnesses if closed(name)]


# -- figures ------------------------------------------------------------------


def message_kind(parts_json: Any) -> str | None:
    try:
        parts = json.loads(parts_json)
    except (TypeError, ValueError):
        return None
    for part in parts if isinstance(parts, list) else []:
        metadata = part.get("metadata") if isinstance(part, dict) else None
        if isinstance(metadata, dict):
            kind = metadata.get("hub.kind", metadata.get("kind"))
            if isinstance(kind, str):
                return kind
    return None


def task_figures(
    snapshot: Mapping[str, Any], telemetry: Telemetry, now: datetime
) -> list[dict[str, Any]]:
    kinds: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    for message in snapshot["message"]:
        if message["task_id"] and message["direction"] == "to_alice":
            kind = message_kind(message["parts_json"])
            if kind in ("question", "progress"):
                kinds[message["task_id"]][kind] += 1
    tasks = []
    for row in sorted(snapshot["task"], key=lambda item: (item["created"], item["id"])):
        result = json_object(row["result_json"]) or {}
        terminal = row["state"] in TERMINAL_STATES
        created = parse_ts(row["created"])
        assigned = telemetry.assigned.get(row["id"])
        finished = parse_ts(row["updated"]) if terminal else None
        # A typed result names its outcome or verdict; Alice's manual cancel or
        # fail stores only `status`, and a plain cancel stores no result at all.
        outcome = (
            closed(result.get("outcome"))
            or closed(result.get("verdict"))
            or closed(result.get("status"))
            or (closed(row["state"]) if terminal else None)
        )
        tasks.append(
            {
                "id": row["id"],
                "role": closed(row["role"]),
                "assignee": closed(row["assignee"]),
                "title": label(row["title"]),
                "state": closed(row["state"]),
                "lease_duration_s": row["lease_duration_s"],
                "created": row["created"],
                "picked_up": iso(assigned),
                "finished": iso(finished),
                "wall_s": seconds(created, finished if terminal else now),
                "open": not terminal,
                "outcome": outcome,
                "pr_url": pr_url(result.get("pr_url")),
                "pr_head_sha": sha(row["pr_head_sha"]),
                "head_sha": sha(result.get("head_sha")) or sha(result.get("reviewed_head_sha")),
                "blocking_findings": count(result.get("blocking_findings")),
                "nonblocking_findings": count(result.get("nonblocking_findings")),
                "questions": kinds[row["id"]]["question"],
                "progress_notes": kinds[row["id"]]["progress"],
                "result_bytes": len(row["result_json"].encode("utf-8"))
                if row["result_json"]
                else 0,
            }
        )
    return tasks


def call_log_calls(
    rows: Iterable[Mapping[str, Any]],
) -> tuple[dict[str, list[Call]], dict[str, int]]:
    calls: dict[str, list[Call]] = defaultdict(list)
    heartbeats: dict[str, int] = defaultdict(int)
    for row in rows:
        actor = closed(row["actor"]) or "unknown"
        tool = closed(row["tool"]) or "unknown"
        if tool == "heartbeat":
            heartbeats[actor] += 1
        mcp = row["boundary"] == "mcp"
        calls[actor].append(
            Call(
                tool=tool,
                outcome=closed(row["outcome"]) or "unknown",
                start=parse_ts(row["started"]),
                end=parse_ts(row["finished"]),
                mcp_sent=number(row["bytes_in"]) if mcp else 0,
                mcp_received=number(row["bytes_out"]) if mcp else 0,
                content=number(row["content_bytes"]) if mcp else 0,
                wire_sent=0 if mcp else number(row["bytes_in"]),
                wire_received=0 if mcp else number(row["bytes_out"]),
            )
        )
    return calls, heartbeats


def agent_figures(
    snapshot: Mapping[str, Any],
    telemetry: Telemetry,
    tasks: list[dict[str, Any]],
    expected: Iterable[str],
    now: datetime,
) -> list[dict[str, Any]]:
    logged, logged_heartbeats = call_log_calls(snapshot["call_log"])
    rows = {row["name"]: row for row in snapshot["agent"]}
    names = [ALICE] + sorted(
        ({*rows, *expected, *telemetry.calls, *telemetry.heartbeats, *logged} - {ALICE}),
    )
    workflow = snapshot["workflow"][0] if snapshot["workflow"] else None
    agents = []
    for name in names:
        row = rows.get(name)
        hub_calls = logged.get(name, [])
        if name == ALICE:
            calls, source = hub_calls, "call_log" if hub_calls else "none"
        elif telemetry.calls.get(name):
            calls, source = telemetry.calls[name], "telemetry"
        else:
            calls = [call for call in hub_calls if call.tool != "heartbeat"]
            source = "call_log" if hub_calls else "none"
        # The wire as the hub saw it (heartbeats included) when it was measuring;
        # otherwise worker-mcp's own count, which leaves heartbeats out.
        wire_rows = [call for call in hub_calls if call.wire_sent or call.wire_received]
        if wire_rows:
            wire_source = "call_log"
        else:
            wire_rows = calls if source == "telemetry" else []
            wire_source = "telemetry" if wire_rows else "none"

        active_windows: list[Interval] = []
        for task in tasks:
            if task["assignee"] != name:
                continue
            start = parse_ts(task["picked_up"]) or parse_ts(task["created"])
            end = parse_ts(task["finished"]) or now
            if start is not None:
                active_windows.append((start, end))
        active_windows = union(active_windows)

        timed = [call for call in calls if call.start is not None]
        if name == ALICE:
            first = min((c.start for c in timed if c.start), default=None)
        else:
            first = min(
                (c.start for c in timed if c.tool == "check_in" and c.start),
                default=min((c.start for c in timed if c.start), default=None),
            )
        released = telemetry.released.get(name)
        if released is None:
            released = max((c.end for c in calls if c.outcome == "release" and c.end), default=None)
        ends = [c.end for c in calls if c.end] + [c.start for c in timed if c.start]
        last = released or max(ends, default=None)
        if released is None and row is not None and row["status"] in ("idle", "busy"):
            # Still connected: the clock runs to now, not to the last recorded call.
            last = max(last, now) if last else None

        waiting_holds = [
            (c.start, c.end)
            for c in calls
            if c.tool in HOLD_TOOLS and c.outcome in EMPTY_OUTCOMES and c.start and c.end
        ]
        active_calls = waiting_calls = 0
        for call in calls:
            if call.tool in HOLD_TOOLS and call.outcome in EMPTY_OUTCOMES:
                waiting_calls += 1
            elif name == ALICE or (call.start and within(call.start, active_windows)):
                active_calls += 1

        total_s = turn_s = waiting_s = idle_s = None
        if first is not None and last is not None and last >= first:
            total_s = span([(first, last)])
            waiting = clip(waiting_holds, first, last)
            if name == ALICE:
                turn = subtract([(first, last)], waiting)
            else:
                turn = clip(active_windows, first, last)
                waiting = subtract(waiting, turn)
            turn_s, waiting_s = span(turn), span(waiting)
            idle_s = round(max(0.0, total_s - turn_s - waiting_s), 3)

        heartbeats = telemetry.heartbeats.get(name, 0) or logged_heartbeats.get(name, 0)
        agents.append(
            {
                "name": name,
                "checked_in": name == ALICE or row is not None,
                "status": (
                    closed(row["status"])
                    if row
                    else (closed(workflow["status"]) if name == ALICE and workflow else None)
                ),
                "harness": closed(row["harness"]) if row else None,
                "provider": closed(row["provider"]) if row else None,
                "model": closed(row["model"]) if row else None,
                "calls_source": source,
                "wire_source": wire_source,
                "calls": {
                    "total": len(calls),
                    "active": active_calls,
                    "waiting": waiting_calls,
                    "other": len(calls) - active_calls - waiting_calls,
                },
                "heartbeats": heartbeats,
                "timeouts": sum(c.outcome in EMPTY_OUTCOMES for c in calls),
                "retries": sum(c.retries for c in calls),
                "errors": sum(c.outcome == "error" for c in calls),
                "bytes": {
                    "mcp": {
                        "sent": sum(c.mcp_sent for c in calls),
                        "received": sum(c.mcp_received for c in calls),
                        "context": sum(c.content for c in calls),
                    },
                    "a2a": {
                        "sent": sum(c.wire_sent for c in wire_rows),
                        "received": sum(c.wire_received for c in wire_rows),
                    },
                },
                "first_seen": iso(first),
                "last_seen": iso(last),
                "released": iso(released),
                "time_s": {
                    "total": total_s,
                    "turn": turn_s,
                    "waiting": waiting_s,
                    "idle": idle_s,
                },
            }
        )
    return agents


def gate_readings(decisions: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    readings = []
    for decision in decisions:
        for value in embedded_objects(decision["rationale"] or ""):
            if "head_matches" not in value or "ci" not in value:
                continue
            reading: dict[str, Any] = {"decision_id": decision["id"], "ts": decision["ts"]}
            for key in GATE_FIELDS:
                item = value.get(key)
                reading[key] = item if isinstance(item, bool) else closed(item)
            for key in GATE_SHA_FIELDS:
                reading[key] = sha(value.get(key))
            reading["pr_url"] = pr_url(value.get("pr_url"))
            readings.append(reading)
    return readings


def merged_sha(decisions: Iterable[Mapping[str, Any]]) -> dict[str, Any] | None:
    """The merge commit Alice logged after merging, if she logged one."""

    found = None
    for decision in decisions:
        text = f"{decision['summary'] or ''}\n{decision['rationale'] or ''}"
        candidate = None
        for value in embedded_objects(text):
            for key in ("merged_sha", "merge_commit_sha", "merge_commit", "mergeCommit"):
                item = value.get(key)
                item = item.get("oid") if isinstance(item, dict) else item
                candidate = sha(item) or candidate
        if candidate is None:
            match = MERGED_SHA_RE.search(text)
            candidate = sha(match.group(1)) if match else None
        if candidate is not None:
            found = {"sha": candidate, "decision_id": decision["id"], "ts": decision["ts"]}
    return found


def workflow_figures(
    snapshot: Mapping[str, Any], tasks: list[dict[str, Any]], now: datetime
) -> dict[str, Any] | None:
    if not snapshot["workflow"]:
        return None
    row = snapshot["workflow"][0]
    stored = json_object(row["policy_json"]) or {}
    policy, defaulted = effective_policy(stored)
    decisions = sorted(snapshot["decision"], key=lambda item: item["id"])
    created = parse_ts(row["created"])
    end = now
    if row["status"] == "done":
        closing = [
            parse_ts(d["ts"]) for d in decisions if d["rationale"] == "Workflow status set to done"
        ]
        end = next((moment for moment in reversed(closing) if moment), now)
    elapsed = seconds(created, end)
    max_wall = policy.get("max_wall_minutes")
    reviews = [task for task in tasks if task["role"] == "reviewer"]
    rounds = sum(
        1
        for task in reviews
        if task["outcome"] == "changes_requested" and task["blocking_findings"] > 0
    )
    calls = [c for c in snapshot["call_log"] if c["tool"] == "check_merge_gate"]
    return {
        "id": row["id"],
        "goal": label(row["goal"], 200),
        "goal_bytes": len(row["goal"].encode("utf-8")),
        "status": closed(row["status"]),
        "created": row["created"],
        "policy": policy,
        "policy_defaults": defaulted,
        "elapsed_s": elapsed,
        "max_wall_minutes": max_wall,
        "wall_used_fraction": (
            round(elapsed / (float(max_wall) * 60), 3)
            if elapsed is not None and isinstance(max_wall, int | float) and max_wall > 0
            else None
        ),
        "review_tasks": len(reviews),
        "review_rounds_used": rounds,
        "max_review_rounds": policy.get("max_review_rounds"),
        "merge_gate_calls": len(calls),
        "merge_gate_readings": gate_readings(decisions),
        "merged": merged_sha(decisions),
        "decisions": [
            {
                "id": d["id"],
                "ts": d["ts"],
                "key": label(d["key"], 80),
                "summary": label(d["summary"]),
                "rationale_bytes": len((d["rationale"] or "").encode("utf-8")),
            }
            for d in decisions
        ],
        "events": dict(sorted(_count(e["kind"] for e in snapshot["event"]).items())),
        "messages": len(snapshot["message"]),
    }


def effective_policy(stored: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
    """The policy the hub enforces, and which keys came from §5 defaults.

    The hub stores only the keys Alice supplied, so a limit she left out is the
    `WorkflowPolicy` default, not absent. A stored policy that no longer
    validates is reported as stored.
    """

    try:
        policy = WorkflowPolicy.model_validate(stored).model_dump(mode="json")
    except ValidationError:
        return stored, []
    return policy, sorted(set(policy) - set(stored))


def _count(values: Iterable[Any]) -> dict[str, int]:
    counts: dict[str, int] = defaultdict(int)
    for value in values:
        counts[closed(value) or "unknown"] += 1
    return dict(counts)


def totals(agents: list[dict[str, Any]]) -> dict[str, Any]:
    def add(path: tuple[str, ...]) -> Any:
        values = []
        for agent in agents:
            value: Any = agent
            for key in path:
                value = value[key]
            values.append(value)
        present = [value for value in values if value is not None]
        return round(sum(present), 3) if present else None

    mcp = add(("bytes", "mcp", "sent")) + add(("bytes", "mcp", "received"))
    a2a = add(("bytes", "a2a", "sent")) + add(("bytes", "a2a", "received"))
    return {
        "bytes": {
            "mcp": {
                "sent": add(("bytes", "mcp", "sent")),
                "received": add(("bytes", "mcp", "received")),
                "context": add(("bytes", "mcp", "context")),
            },
            "a2a": {
                "sent": add(("bytes", "a2a", "sent")),
                "received": add(("bytes", "a2a", "received")),
            },
            "mcp_total": mcp,
            "a2a_total": a2a,
        },
        "calls": {key: add(("calls", key)) for key in ("total", "active", "waiting", "other")},
        "heartbeats": add(("heartbeats",)),
        "timeouts": add(("timeouts",)),
        "retries": add(("retries",)),
        "errors": add(("errors",)),
        "time_s": {key: add(("time_s", key)) for key in ("total", "turn", "waiting", "idle")},
    }


def build_report(
    state_dir: Path,
    *,
    telemetry_paths: list[Path] | None = None,
    labels: bool = True,
    now: datetime | None = None,
) -> dict[str, Any]:
    now = now or datetime.now(UTC)
    database_path = state_dir / "hub.db"
    snapshot = read_database(database_path)
    paths = default_telemetry(state_dir) if telemetry_paths is None else telemetry_paths
    telemetry = read_telemetry(paths)
    tasks = task_figures(snapshot, telemetry, now)
    agents = agent_figures(snapshot, telemetry, tasks, manifest_workers(state_dir), now)
    workflow = workflow_figures(snapshot, tasks, now)
    notes = []
    if not snapshot["has_call_log"] or not snapshot["call_log"]:
        notes.append(
            "call_log is empty: the hub ran without HUB_CALL_ACCOUNTING=1, so Alice's MCP "
            "bytes are not measured and worker wire bytes come from telemetry (no heartbeats)"
        )
    if not paths:
        notes.append("no worker telemetry found; pass --telemetry for worker call figures")
    if telemetry.skipped_lines:
        notes.append(f"{telemetry.skipped_lines} unreadable telemetry line(s) skipped")
    in_progress = workflow is None or workflow["status"] != "done"
    if in_progress:
        notes.append("workflow is not done: open figures run to the report time")
    if not labels:
        if workflow is not None:
            workflow["goal"] = None
            for decision in workflow["decisions"]:
                decision["summary"] = decision["key"] = None
        for task in tasks:
            task["title"] = None
    return {
        "report": {
            "generated_at": iso(now),
            "state_dir": str(state_dir),
            "schema_version": snapshot["schema_version"],
            "telemetry": [str(path) for path in paths],
            "units": UNITS,
            "labels": labels,
            "in_progress": in_progress,
            "notes": notes,
        },
        "workflow": workflow,
        "agents": agents,
        "tasks": tasks,
        "totals": totals(agents),
    }


# -- rendering ----------------------------------------------------------------


def duration(value: Any) -> str:
    if value is None:
        return "-"
    total = int(round(float(value)))
    hours, rest = divmod(total, 3600)
    minutes, secs = divmod(rest, 60)
    return f"{hours}h{minutes:02d}m{secs:02d}s" if hours else f"{minutes}m{secs:02d}s"


def show(value: Any) -> str:
    if value is None:
        return "-"
    if isinstance(value, bool):
        return "yes" if value else "no"
    return str(value)


def agent_rows(report: Mapping[str, Any]) -> tuple[list[str], list[list[str]]]:
    header = [
        "agent", "status", "calls", "active", "waiting", "other", "timeouts", "retries",
        "MCP sent", "MCP recv", "A2A sent", "A2A recv", "turn", "waiting", "idle", "total",
    ]  # fmt: skip
    rows = []
    for agent in [*report["agents"], {"name": "TOTAL", **report["totals"]}]:
        status = agent.get("status")
        if agent["name"] != "TOTAL" and not agent.get("checked_in"):
            status = "never checked in"
        rows.append(
            [
                agent["name"],
                show(status) if agent["name"] != "TOTAL" else "",
                show(agent["calls"]["total"]),
                show(agent["calls"]["active"]),
                show(agent["calls"]["waiting"]),
                show(agent["calls"]["other"]),
                show(agent["timeouts"]),
                show(agent["retries"]),
                show(agent["bytes"]["mcp"]["sent"]),
                show(agent["bytes"]["mcp"]["received"]),
                show(agent["bytes"]["a2a"]["sent"]),
                show(agent["bytes"]["a2a"]["received"]),
                duration(agent["time_s"]["turn"]),
                duration(agent["time_s"]["waiting"]),
                duration(agent["time_s"]["idle"]),
                duration(agent["time_s"]["total"]),
            ]
        )
    return header, rows


def task_rows(report: Mapping[str, Any]) -> tuple[list[str], list[list[str]]]:
    header = [
        "task", "role", "assignee", "state", "outcome", "lease", "wall", "head", "questions",
        "progress", "title",
    ]  # fmt: skip
    rows = [
        [
            task["id"][:8],
            show(task["role"]),
            show(task["assignee"]),
            show(task["state"]),
            show(task["outcome"]),
            duration(task["lease_duration_s"]),
            duration(task["wall_s"]) + (" (open)" if task["open"] else ""),
            (task["head_sha"] or task["pr_head_sha"] or "-")[:12],
            show(task["questions"]),
            show(task["progress_notes"]),
            show(task["title"]),
        ]
        for task in report["tasks"]
    ]
    return header, rows


def plain_table(header: list[str], rows: list[list[str]]) -> list[str]:
    widths = [max(len(row[i]) for row in [header, *rows]) for i in range(len(header))]
    lines = ["  ".join(cell.ljust(width) for cell, width in zip(header, widths, strict=True))]
    lines.append("  ".join("-" * width for width in widths))
    for row in rows:
        lines.append("  ".join(cell.ljust(width) for cell, width in zip(row, widths, strict=True)))
    return [line.rstrip() for line in lines]


def md_cell(value: str) -> str:
    """Keep a value inert in Markdown: no table breaks, code spans or raw HTML."""

    for old, new in (("\\", "\\\\"), ("|", "\\|"), ("`", "'"), ("<", "&lt;"), (">", "&gt;")):
        value = value.replace(old, new)
    return value


def md_table(header: list[str], rows: list[list[str]]) -> list[str]:
    lines = ["| " + " | ".join(header) + " |", "|" + "---|" * len(header)]
    lines += ["| " + " | ".join(md_cell(cell) for cell in row) + " |" for row in rows]
    return lines


def headline(report: Mapping[str, Any]) -> str:
    total = report["totals"]
    mcp, a2a = total["bytes"]["mcp_total"], total["bytes"]["a2a_total"]
    time = total["time_s"]
    return (
        f"{mcp + a2a} body bytes (MCP {mcp}, A2A {a2a}) over {total['calls']['total']} hub calls "
        f"({total['calls']['active']} active, {total['calls']['waiting']} waiting), "
        f"{total['timeouts']} timeouts, {total['retries']} retries; "
        f"agent time {duration(time['total'])} "
        f"(turn {duration(time['turn'])}, waiting {duration(time['waiting'])})"
    )


def workflow_lines(workflow: Mapping[str, Any] | None) -> list[str]:
    if workflow is None:
        return ["No workflow initialized yet."]
    fraction = workflow["wall_used_fraction"]
    merged = workflow["merged"]
    lines = [
        f"id: {workflow['id']}   status: {show(workflow['status'])}   "
        f"created: {workflow['created']}",
        f"goal: {show(workflow['goal'])} ({workflow['goal_bytes']} bytes)",
        "policy: "
        + json.dumps(workflow["policy"], sort_keys=True)
        + (
            f" (defaults: {', '.join(workflow['policy_defaults'])})"
            if workflow["policy_defaults"]
            else ""
        ),
        f"elapsed: {duration(workflow['elapsed_s'])} of max_wall_minutes "
        f"{show(workflow['max_wall_minutes'])}"
        + (f" ({fraction:.0%})" if fraction is not None else ""),
        f"review rounds used: {workflow['review_rounds_used']} of max_review_rounds "
        f"{show(workflow['max_review_rounds'])} ({workflow['review_tasks']} review task(s))",
        f"merge gate: {workflow['merge_gate_calls']} call(s) in call_log, "
        f"{len(workflow['merge_gate_readings'])} reading(s) logged",
    ]
    for reading in workflow["merge_gate_readings"]:
        facts = ", ".join(f"{key}={show(reading[key])}" for key in GATE_FIELDS)
        head = reading["current_head_sha"] or reading["expected_head_sha"]
        lines.append(
            f"  - decision {reading['decision_id']} {reading['ts']}: {facts}, head={show(head)}"
        )
    lines.append(
        f"merged sha: {merged['sha']} (decision {merged['decision_id']})"
        if merged
        else "merged sha: -"
    )
    events = ", ".join(f"{kind}={count}" for kind, count in workflow["events"].items())
    lines.append(f"events: {events or '-'}   messages: {workflow['messages']}")
    lines.append(f"decisions: {len(workflow['decisions'])}")
    for decision in workflow["decisions"]:
        key = f" [{decision['key']}]" if decision["key"] else ""
        lines.append(
            f"  - {decision['id']} {decision['ts']}{key} {show(decision['summary'])} "
            f"(rationale {decision['rationale_bytes']} bytes)"
        )
    return lines


def render_text(report: Mapping[str, Any], markdown: bool = False) -> str:
    meta = report["report"]
    table = md_table if markdown else plain_table
    heading = (lambda text: f"### {text}") if markdown else (lambda text: f"== {text} ==")
    title = "Hub run report" + (" (in progress)" if meta["in_progress"] else "")
    lines = [f"## {title}" if markdown else title, ""]
    lines.append(("> " if markdown else "") + UNITS)
    lines.append("")
    lines.append(f"Generated {meta['generated_at']} from {meta['state_dir']} "
                 f"(schema v{meta['schema_version']}); telemetry: "
                 f"{', '.join(meta['telemetry']) or 'none'}")  # fmt: skip
    for note in meta["notes"]:
        lines.append(f"- note: {note}")
    lines += ["", ("**Total:** " if markdown else "Total: ") + headline(report), ""]
    lines += [heading("Workflow"), ""]
    for line in workflow_lines(report["workflow"]):
        if markdown:
            # Nested items keep their indent; top-level lines become bullets.
            line = md_cell(line) if line.startswith("  - ") else "- " + md_cell(line)
        lines.append(line)
    lines += ["", heading("Agents"), ""]
    lines += table(*agent_rows(report))
    lines += [
        "",
        "calls: model-initiated hub tool calls (heartbeats excluded); active = made while "
        "holding an assigned task, waiting = a hold that returned timeout/null event. "
        "MCP = tool arguments/results at the model's context boundary; A2A = HTTP bodies "
        "between worker-mcp and the hub.",
        "",
        heading("Tasks"),
        "",
    ]
    lines += table(*task_rows(report)) if report["tasks"] else ["No tasks yet."]
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--state-dir", required=True, type=Path, help="the run's HUB_STATE_DIR")
    parser.add_argument("--format", choices=("text", "json", "md"), default="text")
    parser.add_argument(
        "--telemetry",
        action="append",
        type=Path,
        help="a worker HUB_TELEMETRY_LOG (repeatable); default: *-telemetry.jsonl beside it",
    )
    parser.add_argument(
        "--no-labels",
        action="store_true",
        help="also omit Alice-authored labels (goal, task titles, decision keys and summaries)",
    )
    args = parser.parse_args(argv)
    # A Windows console may not encode every character a label can hold.
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(errors="backslashreplace")
    state_dir = args.state_dir.expanduser()
    if not (state_dir / "hub.db").is_file():
        log(f"no hub.db in {state_dir}")
        return 2
    missing = [path for path in args.telemetry or [] if not path.is_file()]
    if missing:
        log(f"no telemetry file at {missing[0]}")
        return 2
    try:
        report = build_report(state_dir, telemetry_paths=args.telemetry, labels=not args.no_labels)
    except (OSError, sqlite3.Error) as exc:
        log(f"could not read hub state: {exc}")
        return 1
    if args.format == "json":
        sys.stdout.write(json.dumps(report, indent=2, sort_keys=True) + "\n")
    else:
        sys.stdout.write(render_text(report, markdown=args.format == "md"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
