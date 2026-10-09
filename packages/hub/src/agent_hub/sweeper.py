"""The background pass that notices what stopped happening.

Nothing else in the hub is driven by the passage of time: a lease only matters
once it is overdue, and a worker that has gone quiet sends nothing to react to.
This loop turns both into events on Alice's inbox, and keeps the stall
episodes of agents whose bridge is alive but whose harness has gone quiet
(#144).
"""

from __future__ import annotations

import asyncio
import logging
from contextlib import suppress

from .store import HubStore

logger = logging.getLogger(__name__)


async def run_sweeper(store: HubStore, interval_s: float, lost_after_s: float) -> None:
    """Sweep on a fixed interval until cancelled."""

    while True:
        await asyncio.sleep(interval_s)
        try:
            events = store.sweep(lost_after_s)
        except Exception:
            # A sweep failure must not take the loop down with it; the next
            # pass sees the same overdue rows and reports them then.
            logger.exception("Sweep failed")
            events = []
        try:
            # Separate from the lease and liveness pass, so neither can stop
            # the other (#144).
            events += store.sweep_stalls()
        except Exception:
            logger.exception("Stall sweep failed")
        for event in events:
            logger.info("Sweeper queued %s: %s", event.kind.value, event.payload)


def start_sweeper(store: HubStore, interval_s: float, lost_after_s: float) -> asyncio.Task[None]:
    """Start the sweep loop as a background task."""

    return asyncio.create_task(run_sweeper(store, interval_s, lost_after_s), name="hub-sweeper")


async def stop_sweeper(task: asyncio.Task[None]) -> None:
    """Cancel the sweep loop and wait for it to unwind."""

    task.cancel()
    with suppress(asyncio.CancelledError):
        await task
