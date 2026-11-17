#!/usr/bin/env python3
"""State changes per agent from the status poller's log (#497).

usage: status_summary.py STATUS_JSONL [HUB...]

Prints a line each time an agent's status, liveness, stall state or hold changes.
"""

from __future__ import annotations

import json
import sys
from typing import Any


def main() -> None:
    path, hubs = sys.argv[1], set(sys.argv[2:])
    last: dict[tuple[str, str], Any] = {}
    with open(path) as handle:
        for line in handle:
            try:
                record = json.loads(line)
            except ValueError:
                continue
            hub = record["hub"]
            if hubs and hub not in hubs:
                continue
            status, clock = record["status"], record["t"][11:19]
            orchestrator = status.get("orchestrator_activity") or {}
            if orchestrator:
                value = (
                    orchestrator.get("state"),
                    orchestrator.get("bridge"),
                    orchestrator.get("stalled"),
                    tuple(orchestrator.get("reasons") or ()),
                    orchestrator.get("holding"),
                )
                if last.get((hub, "ORCH")) != value:
                    last[(hub, "ORCH")] = value
                    silence = orchestrator.get("silence_s")
                    print(clock, hub, "ORCH", value, "silence_s", silence)
            for agent in status["agents"]:
                activity = agent.get("activity") or {}
                value = (
                    agent["status"],
                    "alive" if agent["alive"] else "LOST",
                    agent["stalled"],
                    tuple(activity.get("reasons") or ()),
                    activity.get("suppressed"),
                    activity.get("holding"),
                )
                if last.get((hub, agent["name"])) != value:
                    last[(hub, agent["name"])] = value
                    print(
                        clock, hub, agent["name"], value, "hold_age_s", activity.get("hold_age_s")
                    )


main()
