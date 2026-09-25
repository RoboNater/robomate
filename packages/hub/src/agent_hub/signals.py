"""In-process wakeups for the hub's held requests.

The hub holds a worker's request open until Alice acts. Alice's tools run in
the same process (spec §2: one process for hub and Alice's MCP server), so a
write can hand the waiter its wakeup directly instead of the waiter polling
SQLite on a timer.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from threading import Lock


def context_key(context_id: str) -> str:
    """Wakeup key for anything addressed to one agent's A2A context."""

    return f"context:{context_id}"


def task_key(task_id: str) -> str:
    """Wakeup key for anything addressed to one task."""

    return f"task:{task_id}"


EVENT_KEY = "events"
"""Wakeup key for Alice's inbox."""


@dataclass(frozen=True, slots=True)
class _Waiter:
    """One held request, bound to the loop its event belongs to."""

    loop: asyncio.AbstractEventLoop
    event: asyncio.Event

    def wake(self) -> None:
        # A writer may be a synchronous caller on another thread, and
        # asyncio.Event.set is not thread-safe. Hopping through the owning loop
        # costs nothing when the writer is already on it.
        try:
            running = asyncio.get_running_loop()
        except RuntimeError:
            running = None
        if running is self.loop:
            self.event.set()
        else:
            with suppress(RuntimeError):
                self.loop.call_soon_threadsafe(self.event.set)


class Signals:
    """A registry of waiters, keyed by what they are waiting for."""

    def __init__(self) -> None:
        self._waiters: dict[str, set[_Waiter]] = {}
        self._lock = Lock()

    @contextmanager
    def subscribe(self, key: str) -> Iterator[asyncio.Event]:
        """Register a waiter for `key` and unregister it on the way out.

        Subscribe before reading state: a notification that lands between the
        read and the wait then sets the flag rather than being lost.
        """

        waiter = _Waiter(loop=asyncio.get_running_loop(), event=asyncio.Event())
        with self._lock:
            self._waiters.setdefault(key, set()).add(waiter)
        try:
            yield waiter.event
        finally:
            with self._lock:
                waiters = self._waiters.get(key)
                if waiters is not None:
                    waiters.discard(waiter)
                    if not waiters:
                        del self._waiters[key]

    def notify(self, key: str) -> None:
        """Wake every waiter registered for `key`. No-op when there are none."""

        with self._lock:
            waiters = list(self._waiters.get(key, ()))
        for waiter in waiters:
            waiter.wake()

    def waiting(self, key: str) -> int:
        """Return how many waiters `key` currently has (diagnostics, tests)."""

        with self._lock:
            return len(self._waiters.get(key, ()))
