#!/usr/bin/env python3
"""Things that arrive in the middle of a 600 s hold (#497), through real bridges.

usage: interact_arrivals.py   (ROBOMATE_HUB_URL / ROBOMATE_TOKEN_FILE name a smoke hub)

Each trigger fires 20 s into the hold: assign_task, a worker joining (an event for the
orchestrator), release_agent, and request_closeout_reports.
"""

from __future__ import annotations

import asyncio
from contextlib import AsyncExitStack

from bridge_client import call, log, open_bridge

HOLD_S = 600
DELAY_S = 20


async def drain(orchestrator) -> dict | None:
    """Ack queued events; return the last one seen."""

    last = None
    args: dict[str, object] = {"timeout_s": 1}
    while True:
        body = await call(orchestrator, "alice", "wait_for_event", **args)
        if not body or body["event"] is None:
            return last
        last = body["event"]
        args["ack"] = last["delivery_id"]


async def main() -> None:
    async with AsyncExitStack() as stack:
        alice = await open_bridge(stack)
        bob = await open_bridge(stack)
        carol = await open_bridge(stack)
        dave = await open_bridge(stack)
        await call(alice, "alice", "join", name="alice", role="orchestrator", takeover=True)
        await call(
            alice,
            "alice",
            "start_workflow",
            statement="Smoke workflow for #497 interaction checks.",
            policy={"max_wall_minutes": 600},
        )
        state = await call(alice, "alice", "get_state")
        run_id = state["workflow"]["run_id"]
        await call(bob, "bob", "join", name="bob", role="implementer", takeover=True)
        await call(dave, "dave", "join", name="dave", role="implementer", takeover=True)
        event = await drain(alice)

        log("=== assign_task arrives mid-hold")
        hold = asyncio.create_task(call(bob, "bob", "await_assignment", timeout_s=HOLD_S))
        await asyncio.sleep(DELAY_S)
        await call(
            alice,
            "alice",
            "assign_task",
            agent="bob",
            role="implementer",
            title="smoke",
            instructions="Smoke task; do nothing.",
            event_id=event["id"],
        )
        await hold

        log("=== an event arrives mid-hold (carol joins)")
        hold = asyncio.create_task(call(alice, "alice", "wait_for_event", timeout_s=HOLD_S))
        await asyncio.sleep(DELAY_S)
        await call(carol, "carol", "join", name="carol", role="reviewer", takeover=True)
        await hold

        log("=== release_agent arrives mid-hold")
        hold = asyncio.create_task(call(dave, "dave", "await_assignment", timeout_s=HOLD_S))
        await asyncio.sleep(DELAY_S)
        await call(alice, "alice", "release_agent", agent="dave")
        await hold

        log("=== request_closeout_reports arrives mid-hold")
        hold = asyncio.create_task(call(carol, "carol", "await_assignment", timeout_s=HOLD_S))
        await asyncio.sleep(DELAY_S)
        request = asyncio.create_task(
            call(alice, "alice", "request_closeout_reports", run_id=run_id, timeout_s=30)
        )
        await hold
        await request


asyncio.run(main())
