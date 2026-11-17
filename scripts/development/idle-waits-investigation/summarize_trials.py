#!/usr/bin/env python3
"""One line per hub wait in each trial directory: arguments, elapsed time, outcome (#497).

usage: summarize_trials.py TRIAL_DIR...

Reads the harness event stream each run-*.sh script saved as out.jsonl.
"""

from __future__ import annotations

import json
import pathlib
import sys
from collections.abc import Iterator
from typing import Any

WAITS = ("await_assignment", "wait_for_event")
Row = tuple[str, Any, Any, str]


def claude_rows(events: list[dict[str, Any]]) -> Iterator[Row]:
    started: dict[str, tuple[str, Any]] = {}
    heartbeat: dict[str, int] = {}
    for event in events:
        kind = event.get("type")
        if kind == "tool_progress":
            heartbeat[event["parent_tool_use_id"]] = event["elapsed_time_seconds"]
        elif kind in ("assistant", "user") and "message" in event:
            for part in event["message"].get("content", []):
                if not isinstance(part, dict):
                    continue
                if part["type"] == "tool_use" and part["name"].endswith(WAITS):
                    started[part["id"]] = (part["name"].split("__")[-1], part["input"])
                elif part["type"] == "tool_result" and part.get("tool_use_id") in started:
                    name, args = started.pop(part["tool_use_id"])
                    elapsed = f">={heartbeat.get(part['tool_use_id'], 0)} (30 s heartbeats)"
                    flag = "ERROR " if part.get("is_error") else ""
                    yield name, args, elapsed, flag + str(part.get("content"))


def codex_rows(events: list[dict[str, Any]]) -> Iterator[Row]:
    for event in events:
        item = event.get("item") or {}
        if event.get("type") != "item.completed" or item.get("type") != "mcp_tool_call":
            continue
        if item["tool"] in WAITS:
            outcome = item.get("error") or item.get("result")
            yield item["tool"], item["arguments"], "see timing", f"{item['status']} {outcome}"


def opencode_rows(events: list[dict[str, Any]]) -> Iterator[Row]:
    for event in events:
        if event.get("type") != "tool_use" or not event["part"]["tool"].endswith(WAITS):
            continue
        state = event["part"]["state"]
        times = state.get("time", {})
        elapsed = round((times.get("end", 0) - times.get("start", 0)) / 1000, 1)
        outcome = state.get("output") or state.get("error")
        name = event["part"]["tool"].split("_", 1)[1]
        yield name, state.get("input"), elapsed, f"{state['status']} {outcome}"


def antigravity_rows(events: list[dict[str, Any]]) -> Iterator[Row]:
    for event in events:
        if event.get("event") == "step_update":
            step = event["step_update"]
            info = step.get("tool_info", {})
            params = info.get("parameters") or {}
            if params.get("ToolName") in WAITS and step.get("state") != "ACTIVE":
                elapsed = round(step.get("duration_seconds") or 0, 1)
                outcome = info.get("output") or step.get("error") or ""
                yield (
                    params["ToolName"],
                    params.get("Arguments"),
                    elapsed,
                    f"{step['state']} {outcome}",
                )
        elif event.get("event") == "result":
            result = event["result"]
            if "timed out" in str(result.get("response")) or result.get("status") != "SUCCESS":
                elapsed = round(result.get("duration_seconds") or 0, 1)
                yield (
                    "(final reply)",
                    "",
                    elapsed,
                    f"{result.get('status')} {result.get('response')}",
                )


def main() -> None:
    for trial in sys.argv[1:]:
        directory = pathlib.Path(trial)
        timing = " | ".join((directory / "timing.txt").read_text().split("\n")[:2])
        print(f"## {directory.name}  [{timing}]")
        events = []
        stream = directory / "out.jsonl"
        if not stream.exists():  # the interactive trial keeps pane.log instead
            continue
        for line in stream.read_text(errors="replace").splitlines():
            try:
                events.append(json.loads(line))
            except ValueError:
                continue
        for rows in (claude_rows, codex_rows, opencode_rows, antigravity_rows):
            for name, args, elapsed, outcome in rows(events):
                text = " ".join(outcome.split())[:300]
                print(f"   {name} {json.dumps(args)} elapsed_s={elapsed} -> {text}")


main()
