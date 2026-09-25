#!/usr/bin/env python3
"""Measure hub call bytes by replaying a published Step 6 call sequence (#78).

This is a scripted substitute for a Step 6 acceptance run, not one: no LLM
runs, and nothing touches GitHub. It starts the real hub over stdio MCP with
`HUB_CALL_ACCOUNTING=1` and two real worker-mcp processes with
`HUB_TELEMETRY_LOG`, then plays Alice's, Bob's and Charlie's hub tool calls in
the order a Step 6 tool-audit recorded them, with the arguments it recorded.
Only the ids a live hub mints (task ids, event ids, delivery acks) are
substituted.

Timing is not replayed. A recorded `wait_for_event` that timed out is replayed
with `timeout_s=1`, and one that returned an event with its recorded timeout, so
it returns once the scripted workers produce that event. Workers hold
`await_assignment` for `--worker-hold-s` seconds and heartbeat every
`--heartbeat-s`, so a short replay still measures both. `check_merge_gate`
needs GitHub and is skipped.

The report is generated from `call_log` and the worker telemetry, never typed
by hand:

    uv run --locked python scripts/measure-call-bytes.py /absolute/run-dir
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import secrets
import socket
import sqlite3
import sys
import time
import urllib.request
from collections import Counter, defaultdict
from contextlib import AsyncExitStack
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_AUDIT = ROOT / "docs/evidence/step6-20260918191713_f99578f4.tool-audit.json"
PREFIX = "mcp__hub__"
HARNESS = {"bob": ("claude-code", "anthropic"), "charlie": ("codex", "openai")}
# Waits that returned within this margin of their timeout are counted as timeouts.
TIMEOUT_MARGIN_S = 0.5


def log(message: str) -> None:
    print(message, file=sys.stderr, flush=True)


def load_calls(audit: Path) -> dict[str, list[dict[str, Any]]]:
    """Each agent's hub tool calls, in order, with Codex's doubled records merged."""

    raw = json.loads(audit.read_text(encoding="utf-8"))
    calls: dict[str, list[dict[str, Any]]] = {}
    for agent, records in raw.items():
        hub = [r for r in records if r.get("name", "").startswith(PREFIX)]
        merged: list[dict[str, Any]] = []
        for record in hub:
            # Codex transcripts carry each call twice with no timestamp.
            if (
                merged
                and "timestamp" not in record
                and record["name"] == merged[-1]["name"]
                and record["input"] == merged[-1]["input"]
                and not merged[-1].get("_paired")
            ):
                merged[-1]["_paired"] = True
                continue
            merged.append(dict(record))
        calls[agent] = [
            {"tool": r["name"].removeprefix(PREFIX), **{k: r[k] for k in r if k != "name"}}
            for r in merged
        ]
    return calls


def _duration(record: dict[str, Any]) -> float | None:
    if not record.get("timestamp") or not record.get("completed_at"):
        return None
    start = datetime.fromisoformat(record["timestamp"].replace("Z", "+00:00"))
    end = datetime.fromisoformat(record["completed_at"].replace("Z", "+00:00"))
    return (end - start).total_seconds()


def timed_out(record: dict[str, Any]) -> bool | None:
    duration = _duration(record)
    if duration is None:
        return None
    return duration >= float(record["input"].get("timeout_s", 120)) - TIMEOUT_MARGIN_S


def free_port() -> int:
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


def structured(result: Any) -> Any:
    if result.structuredContent is not None:
        return result.structuredContent
    return json.loads(result.content[0].text) if result.content else None


async def alice(
    session: ClientSession, calls: list[dict[str, Any]], skipped: Counter[str]
) -> None:
    delivery: str | None = None
    event_id: int | None = None
    for call in calls:
        tool, args = call["tool"], dict(call["input"])
        if tool == "check_merge_gate":
            skipped[tool] += 1
            continue
        if tool == "wait_for_event":
            args["timeout_s"] = 1 if timed_out(call) else args.get("timeout_s", 120)
            if "ack" in args:
                args["ack"] = delivery
                if delivery is None:
                    del args["ack"]
        if tool == "assign_task":
            args["event_id"] = event_id
        result = await session.call_tool(tool, args)
        if result.isError:
            log(f"alice {tool}: error")
            continue
        if tool == "wait_for_event":
            event = (structured(result) or {}).get("event")
            if event is not None:
                delivery, event_id = event["delivery_id"], event["id"]
        log(f"alice {tool}")


def work_blocks(
    calls: list[dict[str, Any]],
) -> tuple[dict[str, Any], list[list[dict[str, Any]]]]:
    """The check-in, then the calls a worker made after each assignment."""

    check_in = next(c for c in calls if c["tool"] == "check_in")
    blocks: list[list[dict[str, Any]]] = []
    current: list[dict[str, Any]] | None = None
    for call in calls:
        if call["tool"] == "await_assignment":
            if current:
                blocks.append(current)
            current = []
        elif call["tool"] != "check_in" and current is not None:
            current.append(call)
    if current:
        blocks.append(current)
    return check_in, blocks


async def worker(
    name: str, session: ClientSession, calls: list[dict[str, Any]], hold_s: float
) -> None:
    check_in, blocks = work_blocks(calls)
    await session.call_tool("check_in", dict(check_in["input"]))
    done = 0
    while True:
        assignment = structured(
            await session.call_tool("await_assignment", {"timeout_s": hold_s})
        )
        if assignment.get("release"):
            log(f"{name} released")
            return
        if assignment.get("timeout"):
            continue
        task_id = assignment["task_id"]
        block = blocks[done] if done < len(blocks) else []
        done += 1
        log(f"{name} assignment {done} ({assignment['role']})")
        for call in block:
            args = dict(call["input"])
            if "task_id" in args:
                args["task_id"] = task_id
            result = await session.call_tool(call["tool"], args)
            if result.isError:
                log(f"{name} {call['tool']}: error")


async def replay(
    run_dir: Path, calls: dict[str, list[dict[str, Any]]], hold_s: float, heartbeat_s: float
) -> dict[str, Any]:
    port, token = free_port(), secrets.token_hex(16)
    state = run_dir / "state"
    base_env = {k: v for k, v in os.environ.items() if not k.startswith("HUB_")}
    hub_env = base_env | {
        "HUB_STATE_DIR": str(state),
        "HUB_HOST": "127.0.0.1",
        "HUB_PORT": str(port),
        "HUB_TOKEN": token,
        "HUB_CALL_ACCOUNTING": "1",
        "HUB_CALL_LOG_JSONL": str(run_dir / "hub-calls.jsonl"),
    }
    skipped: Counter[str] = Counter()
    started = time.monotonic()
    async with AsyncExitStack() as stack:
        hub = StdioServerParameters(
            command=sys.executable, args=["-m", "agent_hub.main"], env=hub_env
        )
        read, write = await stack.enter_async_context(stdio_client(hub))
        alice_session = await stack.enter_async_context(ClientSession(read, write))
        await alice_session.initialize()
        await alice_session.list_tools()
        deadline = time.monotonic() + 20
        while True:
            try:
                urllib.request.urlopen(f"http://127.0.0.1:{port}/healthz", timeout=0.5).close()
                break
            except OSError:
                if time.monotonic() > deadline:
                    raise
                await asyncio.sleep(0.1)
        sessions = {}
        for name, (harness, provider) in HARNESS.items():
            env = base_env | {
                "HUB_URL": f"http://127.0.0.1:{port}",
                "HUB_TOKEN": token,
                "AGENT_NAME": name,
                "HUB_HARNESS": harness,
                "HUB_PROVIDER": provider,
                "HUB_TELEMETRY_LOG": str(run_dir / f"{name}-telemetry.jsonl"),
                "HUB_HEARTBEAT_S": str(heartbeat_s),
            }
            params = StdioServerParameters(
                command=sys.executable, args=["-m", "worker_mcp.main"], env=env
            )
            wread, wwrite = await stack.enter_async_context(stdio_client(params))
            session = await stack.enter_async_context(ClientSession(wread, wwrite))
            await session.initialize()
            await session.list_tools()
            sessions[name] = session
        await asyncio.wait_for(
            asyncio.gather(
                alice(alice_session, calls["alice"], skipped),
                *(worker(n, s, calls[n], hold_s) for n, s in sessions.items()),
            ),
            timeout=900,
        )
    return {
        "database": state / "hub.db",
        "skipped": dict(skipped),
        "wall_s": round(time.monotonic() - started, 1),
        "worker_hold_s": hold_s,
        "heartbeat_s": heartbeat_s,
    }


def report(
    run_dir: Path, audit: Path, calls: dict[str, Any], run: dict[str, Any]
) -> dict[str, Any]:
    with sqlite3.connect(run["database"]) as connection:
        connection.row_factory = sqlite3.Row
        rows = [dict(r) for r in connection.execute("SELECT * FROM call_log ORDER BY id")]
        per_actor = [
            dict(r)
            for r in connection.execute(
                "SELECT actor, COUNT(*) AS calls, SUM(bytes_in) AS bytes_in,"
                " SUM(bytes_out) AS bytes_out FROM call_log GROUP BY actor ORDER BY actor"
            )
        ]
    per_tool: dict[tuple[str, str, str], dict[str, int | None]] = defaultdict(
        lambda: {"calls": 0, "bytes_in": 0, "bytes_out": 0, "content_bytes": None}
    )
    for row in rows:
        entry = per_tool[(row["boundary"], row["actor"], row["tool"])]
        for key in ("bytes_in", "bytes_out"):
            entry[key] = (entry[key] or 0) + row[key]
        entry["calls"] = (entry["calls"] or 0) + 1
        if row["content_bytes"] is not None:
            entry["content_bytes"] = (entry["content_bytes"] or 0) + row["content_bytes"]

    waits = [r for r in rows if r["tool"] == "wait_for_event"]
    nulls = [r for r in waits if r["outcome"] == "null_event"]
    states = [r for r in rows if r["tool"] == "get_state"]
    recorded_waits = [c for c in calls["alice"] if c["tool"] == "wait_for_event"]
    recorded_nulls = [c for c in recorded_waits if timed_out(c)]

    workers: dict[str, dict[str, dict[str, int]]] = {}
    for name in HARNESS:
        tally: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
        path = run_dir / f"{name}-telemetry.jsonl"
        for line in path.read_text(encoding="utf-8").splitlines():
            record = json.loads(line)
            if record.get("event") != "tool_call" or record.get("phase") == "start":
                continue
            entry = tally[record["tool"]]
            entry["calls"] += 1
            for key in (
                "mcp_request_bytes",
                "mcp_result_bytes",
                "http_request_bytes",
                "http_response_bytes",
                "http_retries",
            ):
                entry[key] += record.get(key) or 0
        workers[name] = {tool: dict(values) for tool, values in sorted(tally.items())}

    return {
        "kind": "scripted substitute, not a Step 6 acceptance run",
        "call_sequence_source": str(audit.relative_to(ROOT)),
        "generated": datetime.now(UTC).isoformat(timespec="seconds"),
        "replay_wall_s": run["wall_s"],
        "worker_hold_s": run["worker_hold_s"],
        "worker_heartbeat_s": run["heartbeat_s"],
        "skipped_calls": run["skipped"],
        "per_actor": per_actor,
        "per_tool": [
            {"boundary": b, "actor": a, "tool": t, **v}
            for (b, a, t), v in sorted(per_tool.items())
        ],
        "wait_for_event": {
            "calls": len(waits),
            "null_event": len(nulls),
            "null_share_calls": round(len(nulls) / len(waits), 3) if waits else None,
            "bytes_out": sum(r["bytes_out"] for r in waits),
            "null_bytes_out": sum(r["bytes_out"] for r in nulls),
            "null_bytes_each": sorted({r["bytes_out"] for r in nulls}),
            "recorded_run_calls": len(recorded_waits),
            "recorded_run_timeouts": len(recorded_nulls),
        },
        "get_state": [
            {
                "call": i + 1,
                "bytes_out": r["bytes_out"],
                "content_bytes": r["content_bytes"],
                "repeat_bytes": r["repeat_bytes"],
            }
            for i, r in enumerate(states)
        ],
        "workers": workers,
    }


def markdown(data: dict[str, Any], stem: str) -> str:
    waits = data["wait_for_event"]
    lines = [
        f"# Call byte accounting: {stem}",
        "",
        "**A scripted substitute, not a Step 6 acceptance run.** Generated by",
        "`scripts/measure-call-bytes.py` from `call_log` and worker telemetry; no number",
        "here was typed by hand.",
        "",
        "The real hub ran over stdio MCP with `HUB_CALL_ACCOUNTING=1`, and two real",
        "worker-mcp processes spoke A2A to it with `HUB_TELEMETRY_LOG`. A script played",
        "each agent's hub tool calls in the order, and with the arguments, recorded in",
        f"`{data['call_sequence_source']}`. Only ids the live hub mints were substituted.",
        "No LLM ran and nothing touched GitHub.",
        "",
        "Timing was not replayed. A recorded `wait_for_event` that timed out was replayed",
        "with `timeout_s=1`; one that returned an event kept its recorded timeout and",
        "returned when the scripted workers produced the event. Workers held",
        f"`await_assignment` for {data['worker_hold_s']} s rather than 120 s and heartbeat"
        f" every {data['worker_heartbeat_s']} s rather than 30 s, so their timeout and",
        "heartbeat counts reflect the script's pacing, not a real run's; the per-call",
        f"sizes do not depend on it. The replay took {data['replay_wall_s']} s. Skipped",
        f"(needs GitHub): {data['skipped_calls'] or 'none'}.",
        "",
        "## Per agent (`SELECT actor, ... FROM call_log GROUP BY actor`)",
        "",
        "| actor | calls | bytes in | bytes out |",
        "|---|---:|---:|---:|",
    ]
    lines += [
        f"| {r['actor']} | {r['calls']} | {r['bytes_in']} | {r['bytes_out']} |"
        for r in data["per_actor"]
    ]
    lines += [
        "",
        "## Bytes per tool, hub side",
        "",
        "`mcp` rows are Alice's stdio JSON-RPC framing; `content` is the text content",
        "a harness puts into her context. `a2a` rows are the workers' HTTP bodies.",
        "",
        "| boundary | actor | tool | calls | bytes in | bytes out | content | out/call |",
        "|---|---|---|---:|---:|---:|---:|---:|",
    ]
    for r in data["per_tool"]:
        content = "–" if r["content_bytes"] is None else r["content_bytes"]
        lines.append(
            f"| {r['boundary']} | {r['actor']} | `{r['tool']}` | {r['calls']} | {r['bytes_in']}"
            f" | {r['bytes_out']} | {content} | {r['bytes_out'] // r['calls']} |"
        )
    lines += [
        "",
        "## `wait_for_event` and `{\"event\": null}`",
        "",
        f"- Replay: {waits['null_event']} of {waits['calls']} results were"
        f" `{{\"event\": null}}` (share {waits['null_share_calls']}), "
        f"{waits['null_bytes_out']} of {waits['bytes_out']} bytes out. Each null result is"
        f" {', '.join(map(str, waits['null_bytes_each'])) or '-'} bytes on the wire.",
        f"- Recorded run, from its durations rather than from accounting: "
        f"{waits['recorded_run_timeouts']} of {waits['recorded_run_calls']} of Alice's"
        " waits lasted at least their `timeout_s`, i.e. returned `{\"event\": null}`.",
        "",
        "## How much `get_state` repeats itself",
        "",
        "`repeat` is how many bytes of a result's text content repeat the previous",
        "`get_state` result, line for line.",
        "",
        "| call | bytes out | content | repeat | repeat share |",
        "|---:|---:|---:|---:|---:|",
    ]
    for r in data["get_state"]:
        share = round(r["repeat_bytes"] / r["content_bytes"], 3) if r["content_bytes"] else ""
        lines.append(
            f"| {r['call']} | {r['bytes_out']} | {r['content_bytes']} | {r['repeat_bytes']}"
            f" | {share} |"
        )
    lines += [
        "",
        "## Worker MCP boundary (worker-mcp telemetry)",
        "",
        "| agent | tool | calls | MCP request | MCP result | HTTP request | HTTP response |",
        "|---|---|---:|---:|---:|---:|---:|",
    ]
    for agent, tools in data["workers"].items():
        for tool, v in tools.items():
            lines.append(
                f"| {agent} | `{tool}` | {v['calls']} | {v.get('mcp_request_bytes', 0)}"
                f" | {v.get('mcp_result_bytes', 0)} | {v.get('http_request_bytes', 0)}"
                f" | {v.get('http_response_bytes', 0)} |"
            )
    return "\n".join(lines) + "\n"


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawTextHelpFormatter
    )
    parser.add_argument("run_dir", type=Path, help="absolute, new or empty directory")
    parser.add_argument("--audit", type=Path, default=DEFAULT_AUDIT)
    parser.add_argument("--evidence-dir", type=Path, default=ROOT / "docs/evidence")
    parser.add_argument("--worker-hold-s", type=float, default=2.0)
    parser.add_argument("--heartbeat-s", type=float, default=5.0)
    args = parser.parse_args()
    run_dir: Path = args.run_dir
    if not run_dir.is_absolute():
        parser.error("run_dir must be absolute")
    if run_dir.exists() and any(run_dir.iterdir()):
        parser.error(f"{run_dir} is not empty; use a fresh directory per measurement")
    run_dir.mkdir(parents=True, exist_ok=True)

    audit = args.audit.resolve()
    calls = load_calls(audit)
    run = asyncio.run(replay(run_dir, calls, args.worker_hold_s, args.heartbeat_s))
    data = report(run_dir, audit, calls, run)
    stem = "call-bytes-" + datetime.now(UTC).strftime("%Y%m%d%H%M%S")
    args.evidence_dir.mkdir(parents=True, exist_ok=True)
    (args.evidence_dir / f"{stem}.json").write_text(json.dumps(data, indent=2) + "\n")
    (args.evidence_dir / f"{stem}.md").write_text(markdown(data, stem))
    log(f"wrote {args.evidence_dir / stem}.md and .json")


if __name__ == "__main__":
    main()
