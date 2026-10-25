"""Time and wait tools for tests (#367). The rules: docs/development/testing-time-and-waits.md.

- `FrozenClock` stands still until the test advances it, so a deadline the
  hub enforces (a lease, a closeout window, the loss grace) passes exactly when
  the test says, on any host. Install it with the `frozen_clock` fixture.
- `wait_until` waits for a condition, never a duration. Real time bounds it
  only to catch a hang, at `LIVENESS_S`.
- The slow-host probe adds latency before each hub SQLite transaction when
  `ROBOMATE_TEST_DB_LATENCY_MS` is set, to reproduce a loaded runner locally.
"""

import asyncio
import os
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from sqlite3 import Connection

import pytest
from agent_hub import database as hub_database
from agent_hub.store import HubStore

# Real time a condition wait may take before the test fails as hung. It is a
# hang detector, not a budget: no hub deadline depends on it, and a passing
# test never comes near it. A condition that passes in milliseconds locally
# can take seconds on a loaded Windows runner or under the probe at 100 ms; a
# minute is far beyond that, and short enough that a real hang fails the test
# well before the CI job's timeout. It costs time only when a test is already
# failing, so never shorten it to make a test faster.
LIVENESS_S = 60.0
# How often a condition wait re-reads its condition.
POLL_INTERVAL_S = 0.01

DB_LATENCY_ENV = "ROBOMATE_TEST_DB_LATENCY_MS"


class FrozenClock:
    """Wall and monotonic time that move only when the test calls `advance`.

    `HubStore` reads two clocks: `clock` (wall) stamps and expires leases and
    windows, and `monotonic_clock` measures host pauses against it
    (`_detect_pause`). Moving only the wall clock looks like a host pause once
    the gap passes `CLOCK_JUMP_TOLERANCE_S`, so both read one elapsed value.
    """

    def __init__(self, start: datetime | None = None) -> None:
        self.start = start if start is not None else datetime.now(UTC)
        self.monotonic_start = time.monotonic()
        self.elapsed = 0.0

    def __call__(self) -> datetime:
        return self.start + timedelta(seconds=self.elapsed)

    def monotonic(self) -> float:
        return self.monotonic_start + self.elapsed

    def advance(self, seconds: float) -> None:
        """Move both clocks forward together, without waiting for real time."""

        if seconds < 0:
            raise ValueError("a frozen clock only moves forward")
        self.elapsed += seconds

    def install(self, store: HubStore) -> None:
        """Drive both of `store`'s clocks from this one."""

        store.clock = self
        store.monotonic_clock = self.monotonic


async def wait_until[T](
    condition: Callable[[], T], what: str, *, timeout_s: float = LIVENESS_S
) -> T:
    """Return `condition()` once it is truthy; fail naming `what` after `timeout_s`.

    Yields to the event loop between reads, so in-process hub calls and holds
    started with `asyncio.create_task` keep running while it waits.
    """

    deadline = time.monotonic() + timeout_s
    while not (value := condition()):
        if time.monotonic() >= deadline:
            pytest.fail(f"timed out after {timeout_s:g} s waiting until {what}")
        await asyncio.sleep(POLL_INTERVAL_S)
    return value


def wait_until_sync[T](
    condition: Callable[[], T], what: str, *, timeout_s: float = LIVENESS_S
) -> T:
    """Blocking `wait_until`, for synchronous tests such as subprocess-hub ones."""

    deadline = time.monotonic() + timeout_s
    while not (value := condition()):
        if time.monotonic() >= deadline:
            pytest.fail(f"timed out after {timeout_s:g} s waiting until {what}")
        time.sleep(POLL_INTERVAL_S)
    return value


def db_latency_s() -> float:
    """The probe's latency from `ROBOMATE_TEST_DB_LATENCY_MS`, in seconds; 0 when off."""

    text = os.environ.get(DB_LATENCY_ENV, "").strip()
    if not text:
        return 0.0
    try:
        milliseconds = float(text)
    except ValueError:
        raise pytest.UsageError(f"{DB_LATENCY_ENV} must be a number of milliseconds") from None
    if not milliseconds >= 0:
        raise pytest.UsageError(f"{DB_LATENCY_ENV} must be zero or more")
    return milliseconds / 1000


@contextmanager
def db_latency(latency_s: float) -> Iterator[None]:
    """Sleep `latency_s` before each hub SQLite transaction while active.

    `agent_hub.database.database` opens one connection per transaction through
    the module's `connect`, so delaying `connect` delays every transaction the
    in-process hub runs. The sleep blocks the event loop, as a slow disk does,
    because the hub's SQLite calls are synchronous. A hub in a subprocess is
    out of reach.
    """

    if latency_s <= 0:
        yield
        return
    original = hub_database.connect

    def slow_connect(path: Path) -> Connection:
        time.sleep(latency_s)
        return original(path)

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(hub_database, "connect", slow_connect)
        yield
