#!/usr/bin/env python3
"""Harness-free baseline for #497: join through the bridge and hold one wait.

usage: baseline_hold.py NAME ROLE TOOL TIMEOUT_S   (ROLE: implementer | orchestrator)

START_WORKFLOW=1 makes an orchestrator start a smoke workflow first; TAKEOVER=1 joins with
takeover. `wait_for_event` acks and waits again until one call returns event=null.
"""

from __future__ import annotations

import asyncio
import os
import sys
from contextlib import AsyncExitStack

from bridge_client import call, open_bridge


async def main() -> None:
    name, role, tool, timeout_s = sys.argv[1], sys.argv[2], sys.argv[3], float(sys.argv[4])
    async with AsyncExitStack() as stack:
        session = await open_bridge(stack)
        await call(
            session,
            name,
            "join",
            name=name,
            role=role,
            takeover=bool(os.environ.get("TAKEOVER")),
        )
        if role == "orchestrator" and os.environ.get("START_WORKFLOW"):
            await call(
                session,
                name,
                "start_workflow",
                statement="Smoke workflow for #497 hold trials. No work is to be done.",
                policy={"max_wall_minutes": 600},
            )
        args: dict[str, object] = {"timeout_s": timeout_s}
        while True:
            body = await call(session, name, tool, **args)
            if tool != "wait_for_event" or not body or body.get("event") is None:
                break
            args["ack"] = body["event"]["delivery_id"]


asyncio.run(main())
