#!/usr/bin/env python3
"""Count how Codex made hub waits in session rollout files (#497).

usage: codex_rollout.py ROLLOUT_JSONL...

Codex can call an MCP tool from an `exec` script, which yields back to the model after
`yield_time_ms`; the model then polls with `wait`. Each poll is a model call. For every
rollout this prints the model, the model calls, the hub waits made through `exec`, the
`wait` polls that followed one, the yield pragmas used, and the direct MCP calls.
"""

from __future__ import annotations

import collections
import json
import re
import sys

HUB_WAITS = ("await_assignment", "wait_for_event", "ask_orchestrator", "ask_alice")
PRAGMA = re.compile(r'yield_time_ms"\s*:\s*(\d+)')


def summarize(path: str) -> str | None:
    cwd = model = None
    model_calls = 0
    polls: collections.Counter[str] = collections.Counter()
    yields: collections.Counter[str] = collections.Counter()
    direct: collections.Counter[str] = collections.Counter()
    hub_waits = 0
    current: str | None = None
    with open(path) as handle:
        for line in handle:
            try:
                record = json.loads(line)
            except ValueError:
                continue
            payload = record.get("payload") or {}
            kind = payload.get("type")
            if record.get("type") == "session_meta":
                cwd = payload.get("cwd")
            elif record.get("type") == "turn_context":
                model = payload.get("model") or model
            if kind == "token_count":
                model_calls += 1
            elif kind == "custom_tool_call" and payload.get("name") == "exec":
                source = str(payload.get("input"))
                current = next((w for w in HUB_WAITS if f"mcp__robomate__{w}" in source), None)
                if current:
                    hub_waits += 1
                    pragma = PRAGMA.search(source)
                    yields[pragma.group(1) if pragma else "default"] += 1
            elif kind == "function_call":
                name = str(payload.get("name"))
                if name == "wait" and current:
                    polls[current] += 1
                elif name.endswith(HUB_WAITS):
                    direct[name] += 1
    if not hub_waits and not direct:
        return None
    return (
        f"{path}\n  cwd={cwd} model={model} model_calls={model_calls}"
        f" exec_hub_waits={hub_waits} polls={dict(polls)} yields={dict(yields)}"
        f" direct={dict(direct)}"
    )


for rollout in sys.argv[1:]:
    summary = summarize(rollout)
    if summary:
        print(summary)
