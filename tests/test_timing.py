"""The shared time and wait tools in tests/timing.py (#367)."""

import asyncio
import sqlite3
from datetime import timedelta
from time import monotonic

import pytest
from agent_hub import database as hub_database
from agent_hub.store import CLOCK_JUMP_TOLERANCE_S, HubStore
from agent_hub_common import EventKind
from fastapi import FastAPI
from timing import (
    DB_LATENCY_ENV,
    LIVENESS_S,
    FrozenClock,
    db_latency,
    db_latency_s,
    wait_until,
    wait_until_sync,
)

LOST_AFTER_S = 60.0


def test_frozen_clock_stands_still_and_advance_moves_both_clocks(
    store: HubStore, frozen_clock: FrozenClock
) -> None:
    wall, mono = store._now(), store.monotonic_clock()
    assert (store._now(), store.monotonic_clock()) == (wall, mono)
    frozen_clock.advance(30)
    assert store._now() - wall == timedelta(seconds=30)
    # Approximate: the clock adds to the real monotonic reading at its creation,
    # and `(start + 30) - start` is not exactly 30.0 for every float start (#375).
    assert store.monotonic_clock() - mono == pytest.approx(30)
    with pytest.raises(ValueError):
        frozen_clock.advance(-1)


def test_frozen_clock_installs_on_the_apps_store(
    app: FastAPI, hub_store: HubStore, frozen_clock: FrozenClock
) -> None:
    assert app.state.store is hub_store
    assert hub_store.clock is frozen_clock
    assert hub_store.monotonic_clock == frozen_clock.monotonic


def test_large_advance_records_no_host_pause(store: HubStore, frozen_clock: FrozenClock) -> None:
    store.sweep(LOST_AFTER_S)
    # Well past the jump tolerance, inside the sweep's loss window.
    advance_s = CLOCK_JUMP_TOLERANCE_S * 6
    assert advance_s < LOST_AFTER_S
    frozen_clock.advance(advance_s)
    assert store.sweep(LOST_AFTER_S) == []
    # The same advance on the wall clock alone looks like a host pause.
    moved = frozen_clock()
    store.clock = lambda: moved + timedelta(seconds=advance_s)
    assert [event.kind for event in store.sweep(LOST_AFTER_S)] == [EventKind.HOST_PAUSED]


async def test_holds_time_out_in_real_time_on_a_frozen_clock(
    store: HubStore, frozen_clock: FrozenClock
) -> None:
    agent = store.check_in("bob", worker_instance_id="bob-instance")
    started = monotonic()
    assert await asyncio.wait_for(store.await_assignment(agent.context_id, 0.2), LIVENESS_S) is None
    assert 0.2 <= monotonic() - started < LIVENESS_S
    assert frozen_clock.elapsed == 0


async def test_wait_until_returns_the_condition_or_fails_naming_it() -> None:
    assert await wait_until(lambda: 7, "a value is ready") == 7
    started = monotonic()
    with pytest.raises(pytest.fail.Exception, match="0.2 s waiting until the flag is set"):
        await wait_until(lambda: False, "the flag is set", timeout_s=0.2)
    assert 0.2 <= monotonic() - started < LIVENESS_S


def test_wait_until_sync_fails_naming_the_condition() -> None:
    assert wait_until_sync(lambda: "ok", "a value is ready") == "ok"
    started = monotonic()
    with pytest.raises(pytest.fail.Exception, match="0.2 s waiting until the file exists"):
        wait_until_sync(lambda: None, "the file exists", timeout_s=0.2)
    assert 0.2 <= monotonic() - started < LIVENESS_S


def test_db_latency_probe_is_on_only_when_set() -> None:
    # Without the setting the suite runs the hub's own connect, so timing is unchanged.
    assert (hub_database.connect.__name__ == "slow_connect") == (db_latency_s() > 0)


def test_db_latency_setting_parses(monkeypatch: pytest.MonkeyPatch) -> None:
    for text, expected in (("", 0.0), (" ", 0.0), ("0", 0.0), ("50", 0.05), ("100", 0.1)):
        monkeypatch.setenv(DB_LATENCY_ENV, text)
        assert db_latency_s() == expected
    monkeypatch.delenv(DB_LATENCY_ENV)
    assert db_latency_s() == 0.0
    for text in ("fast", "-5", "nan"):
        monkeypatch.setenv(DB_LATENCY_ENV, text)
        with pytest.raises(pytest.UsageError):
            db_latency_s()


def test_db_latency_sleeps_before_each_transaction(
    store: HubStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Count the sleeps rather than time them: a real-time lower bound is itself
    # flaky (Windows timer granularity). Start from an unpatched connect,
    # whatever the session's probe setting.
    monkeypatch.setattr(hub_database, "connect", sqlite3.connect)
    slept: list[float] = []
    monkeypatch.setattr("time.sleep", slept.append)

    def transaction() -> None:
        with hub_database.database(store.path):
            pass

    with db_latency(0):
        assert hub_database.connect is sqlite3.connect
        transaction()
    assert slept == []
    with db_latency(0.05):
        assert hub_database.connect is not sqlite3.connect
        transaction()
        transaction()
    assert slept == [0.05, 0.05]
    assert hub_database.connect is sqlite3.connect
