"""Background loops for leases, agent liveness, and pending forge checks."""

from __future__ import annotations

import asyncio
import logging
from contextlib import suppress

from .merge_gate import ForgeGate
from .store import CiWatch, HubStore

logger = logging.getLogger(__name__)
CI_WATCH_INTERVAL_S = 30.0


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
            # Per-event detail; `robomate up` shows INFO startup lines only (#377).
            logger.debug("Sweeper queued %s: %s", event.kind.value, event.payload)


def start_sweeper(store: HubStore, interval_s: float, lost_after_s: float) -> asyncio.Task[None]:
    """Start the sweep loop as a background task."""

    return asyncio.create_task(run_sweeper(store, interval_s, lost_after_s), name="hub-sweeper")


async def stop_sweeper(task: asyncio.Task[None]) -> None:
    """Cancel the sweep loop and wait for it to unwind."""

    task.cancel()
    with suppress(asyncio.CancelledError):
        await task


async def poll_ci_watches(store: HubStore, gate: ForgeGate) -> None:
    """Read each forge once, without delaying lease and liveness sweeps."""

    async def poll(watch: CiWatch) -> None:
        report = None
        if store.clock() < watch.deadline:
            try:
                async with asyncio.timeout(CI_WATCH_INTERVAL_S):
                    report = await gate.check(watch.pr_url, watch.head_sha, wait=False)
            except Exception as exc:
                # The next pass retries; a failed read is not a CI verdict.
                logger.warning("CI watch %s forge read failed: %s", watch.id, type(exc).__name__)
        store.record_ci_watch_poll(watch, report)

    async with asyncio.TaskGroup() as tasks:
        for watch in store.active_ci_watches():
            tasks.create_task(poll(watch))


async def run_ci_watcher(store: HubStore, gate: ForgeGate) -> None:
    """Resume persisted watches immediately, then poll on a fixed cadence."""
    loop = asyncio.get_running_loop()
    while True:
        started = loop.time()
        try:
            await poll_ci_watches(store, gate)
        except Exception:
            logger.exception("CI watch sweep failed")
        await asyncio.sleep(max(0, started + CI_WATCH_INTERVAL_S - loop.time()))
