#!/usr/bin/env python3
"""What `await_assignment()` holds for when `timeout_s` is omitted (#497).

usage: omitted_timeout.py   (ROBOMATE_HUB_URL / ROBOMATE_TOKEN_FILE name a smoke hub)

One bridge runs with no HUB_DEFAULT_WAIT_S, one with HUB_DEFAULT_WAIT_S=40.
"""

from __future__ import annotations

import asyncio
from contextlib import AsyncExitStack

from bridge_client import bridge_env, call, open_bridge


async def one(name: str, extra: dict[str, str]) -> None:
    async with AsyncExitStack() as stack:
        session = await open_bridge(stack, bridge_env(extra))
        await call(session, name, "join", name=name, role="implementer", takeover=True)
        await call(session, f"{name} {extra}", "await_assignment")


async def main() -> None:
    await asyncio.gather(one("omit-a", {}), one("omit-b", {"HUB_DEFAULT_WAIT_S": "40"}))


asyncio.run(main())
