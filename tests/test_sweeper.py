import asyncio
from datetime import UTC, datetime, timedelta
from typing import cast

from agent_hub.store import EventRecord, HubStore
from agent_hub.sweeper import start_sweeper, stop_sweeper
from agent_hub_common import EventKind


async def drain(store: HubStore) -> None:
    while store.next_event():
        pass


async def test_the_loop_reports_an_overdue_lease_without_being_asked(
    store: HubStore,
) -> None:
    store.check_in("bob")
    store.assign_task("bob", "implementer", "Fix #1", "Open a PR", lease_min=-1)
    await drain(store)

    sweeper = start_sweeper(store, interval_s=0.01, lost_after_s=3600)
    try:
        event = await store.wait_for_event(2.0)
    finally:
        await stop_sweeper(sweeper)

    assert event is not None and event.kind is EventKind.LEASE_EXPIRED


class FlakyStore:
    """A store whose first sweep fails, standing in for a locked database."""

    def __init__(self) -> None:
        self.calls: list[float] = []

    def sweep(self, lost_after_s: float) -> list[EventRecord]:
        self.calls.append(lost_after_s)
        if len(self.calls) == 1:
            raise RuntimeError("database is locked")
        return []


async def test_a_failing_sweep_does_not_stop_the_loop() -> None:
    flaky = FlakyStore()

    sweeper = start_sweeper(cast(HubStore, flaky), interval_s=0.01, lost_after_s=3600)
    try:
        while len(flaky.calls) < 3:
            await asyncio.sleep(0.01)
    finally:
        await stop_sweeper(sweeper)

    assert flaky.calls[:3] == [3600, 3600, 3600]


async def test_stopping_the_loop_leaves_no_task_running(store: HubStore) -> None:
    sweeper = start_sweeper(store, interval_s=0.01, lost_after_s=3600)

    await stop_sweeper(sweeper)

    assert sweeper.cancelled()


async def test_wake_loop_reports_pause_and_suppresses_expiry_and_stalls(store: HubStore) -> None:
    wall = [datetime(2026, 10, 12, tzinfo=UTC)]
    mono = [100.0]
    store.clock = lambda: wall[0]
    store.monotonic_clock = lambda: mono[0]
    agent = store.check_in("bob", worker_instance_id="bob-1")
    store.note_worker_call("bob", "bob-1", "check_in")
    task = store.assign_task("bob", "implementer", "Fix", "Work", lease_min=1)
    await drain(store)
    assert store.sweep(180) == []
    wall[0] += timedelta(hours=4)
    mono[0] += 10

    sweeper = start_sweeper(store, interval_s=0.01, lost_after_s=180)
    try:
        # Waiting polls the inbox, so detection can precede the loop itself.
        event = await store.wait_for_event(2.0)
        await asyncio.sleep(0.05)
    finally:
        await stop_sweeper(sweeper)

    assert event is not None and event.kind is EventKind.HOST_PAUSED
    current = store.agent_by_name(agent.name)
    assert current is not None and current.status.value == "busy"
    current_task = store.get_task(task.id)
    assert current_task is not None and current_task.lease_expires == task.lease_expires
    assert store.stall_episodes() == []
    assert store.next_event() is None
