"""Timestamped startup stage markers on stderr (#372).

`robomate up` turns them on, so a startup that stalls says on stderr which
stage it last reached: a test's `hub did not start` diagnostics and an
operator's terminal both show it. They stay off in every other process, so
the hub's lifespan can mark its stages without printing in in-process tests
or under the `hub` entry point.
"""

from __future__ import annotations

import sys
import time
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from datetime import UTC, datetime

PREFIX = "robomate up: stage "

_started: float | None = None


@contextmanager
def stages() -> Iterator[None]:
    """Print stage markers while active, with elapsed time counted from entry."""

    global _started
    _started = time.monotonic()
    try:
        yield
    finally:
        _started = None


def stage(name: str) -> None:
    """Write `name` with the wall time and the seconds since `stages` began, if active.

    One flushed line per stage, cheap enough to stay on. A marker never fails
    startup: a missing or closed stderr only loses it.
    """

    if _started is None or sys.stderr is None:
        return
    now = datetime.now(UTC).isoformat(timespec="milliseconds")
    with suppress(OSError, ValueError):
        print(
            f"{PREFIX}{name} at {now} (+{time.monotonic() - _started:.3f} s)",
            file=sys.stderr,
            flush=True,
        )
