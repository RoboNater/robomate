#!/usr/bin/env python3
"""`robomate down` in the middle of a hold, then the same bridges after `robomate up` (#497).

usage: interact_hub_down.py HUB PORT   (IDLE_WAITS_SCRATCH's env.sh sourced; HUB is the hub's
directory name under $SMOKE; ROBOMATE_HUB_URL / ROBOMATE_TOKEN_FILE name the same hub)
"""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
from contextlib import AsyncExitStack

from bridge_client import call, log, open_bridge

hub, port = sys.argv[1], sys.argv[2]
smoke, clone = os.environ["SMOKE"], os.environ["SMOKE_CLONE"]
ROBOMATE = ["uv", "run", "--locked", "--project", clone, "robomate"]


def robomate(*args: str) -> str:
    done = subprocess.run(
        [*ROBOMATE, *args], cwd=f"{smoke}/{hub}", capture_output=True, text=True, check=False
    )
    return f"rc={done.returncode} {(done.stdout + done.stderr)[-300:].strip()}"


def robomate_up() -> None:
    with open(f"{smoke}/logs/hub-{hub}.log", "a") as hub_log:
        subprocess.Popen(
            [*ROBOMATE, "up", "--port", port],
            cwd=f"{smoke}/{hub}",
            stdout=hub_log,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )


async def main() -> None:
    async with AsyncExitStack() as stack:
        alice = await open_bridge(stack)
        carol = await open_bridge(stack)
        await call(alice, "alice", "join", name="alice", role="orchestrator", takeover=True)
        await call(carol, "carol", "join", name="carol", role="reviewer", takeover=True)
        args: dict[str, object] = {"timeout_s": 1}
        while (body := await call(alice, "alice", "wait_for_event", **args)) and body["event"]:
            args["ack"] = body["event"]["delivery_id"]

        log("=== robomate down 40 s into 600 s holds")
        holds = [
            asyncio.create_task(call(carol, "carol", "await_assignment", timeout_s=600)),
            asyncio.create_task(call(alice, "alice", "wait_for_event", timeout_s=600)),
        ]
        await asyncio.sleep(40)
        log("robomate down:", await asyncio.to_thread(robomate, "down"))
        await asyncio.gather(*holds)

        log("=== calls while the hub is down")
        await call(carol, "carol", "await_assignment", timeout_s=600)
        await call(alice, "alice", "wait_for_event", timeout_s=600)

        log("=== robomate up on the same port; the same bridges call again")
        await asyncio.sleep(10)  # `down` returns before the old process has exited
        robomate_up()
        await asyncio.sleep(10)
        log("status:", await asyncio.to_thread(robomate, "status"))
        await asyncio.gather(
            call(carol, "carol", "await_assignment", timeout_s=30),
            call(alice, "alice", "wait_for_event", timeout_s=30),
        )


asyncio.run(main())
