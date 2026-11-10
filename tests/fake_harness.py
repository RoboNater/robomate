"""Bound fake-harness launcher scenarios so a broken child cannot loop through
the production resume budget (#464).

A malformed or immediately failing fake child that exits before writing its
launch marker must fail the test clearly, with its exit code and output kept as
evidence, instead of turning the launcher's automatic resume loop into a long
or rapid spin. Supervision scenarios therefore:

  * pass their launch words through :func:`bounded`, which gives the launcher a
    finite, explicit ``--max-resumes`` (and, for the successor launcher, a
    finite ``--resume-total-s``) instead of the unlimited production count and
    ~12-hour budget;
  * run under :func:`supervision`, which bounds a genuinely hung child with
    ``LIVENESS_S`` and cleans up only the fake children this test started
    (recorded in its per-test pids file);
  * use :class:`Supervision` to count launch attempts and raise a diagnostic
    failure when a child dies before its marker.

Production backoff, usage-limit waiting and resume defaults are untouched.
"""

from __future__ import annotations

import contextlib
import json
import os
import signal
import threading
from collections.abc import Iterable, Iterator
from pathlib import Path
from typing import Any

from timing import LIVENESS_S

# A small, explicit resume count: enough for every supervision scenario (a
# healthy run resumes at most twice), but finite, so a broken fixture cannot
# spin through the launcher's unlimited production count.
SUPERVISED_MAX_RESUMES = 4
# The launcher also enforces a wall-clock resume budget; bound it by the same
# LIVENESS_S hang bound rather than inheriting the ~12-hour production value.
SUPERVISED_TOTAL_S = LIVENESS_S

# Set by the per-test ``fake_supervision`` fixture; the fake harness appends
# ``<pid>\t<its own path>`` here so cleanup can find only this test's children.
PIDS_ENV = "FAKE_PIDS"


def bounded(words: list[str], *, total: bool = False) -> list[str]:
    """Add finite resume bounds to a start/resume command, unless it set its own.

    Launcher words may carry a harness command after a bare ``--`` (the prepared
    launchers) or after ``--harness-args`` (the joined launcher). Bounds must
    land before that separator so they parse as launcher options, not harness
    words. Already-present bounds in the launcher portion are respected.

    ``total`` also bounds the wall-clock resume budget with ``LIVENESS_S``. The
    prepared Alice/worker launcher tests omit it: a mocked 1800 s backoff there
    would trip a small budget before the intended interruption. Their finite
    ``--max-resumes`` count already ends any broken-child loop.
    """
    result = list(words)
    cut = len(result)
    for marker in ("--", "--harness-args"):
        if marker in result:
            cut = min(cut, result.index(marker))
    limits = [("--max-resumes", str(SUPERVISED_MAX_RESUMES))]
    if total:
        limits.append(("--resume-total-s", str(SUPERVISED_TOTAL_S)))
    additions: list[str] = []
    for flag, value in limits:
        if flag not in result[:cut]:
            additions += [flag, value]
    return [*result[:cut], *additions, *result[cut:]]


def live_own_pids(pids: Path) -> list[int]:
    """Recorded fake children of this test that are still running."""
    if not pids.is_file():
        return []
    alive: list[int] = []
    for line in pids.read_text(encoding="utf-8").splitlines():
        pid_text, _, token = line.partition("\t")
        try:
            pid = int(pid_text)
        except ValueError:
            continue
        try:
            os.kill(pid, 0)
        except OSError:
            continue
        if _still_ours(pid, token):
            alive.append(pid)
    return alive


def _still_ours(pid: int, token: str) -> bool:
    """Best-effort check that ``pid`` is still one of this test's fake children.

    On POSIX the child's command line names its fake-harness script. Windows
    has no /proc, so a recorded pid is trusted: these are short-lived children
    the launcher started for this test alone.
    """
    try:
        return token.encode() in Path(f"/proc/{pid}/cmdline").read_bytes()
    except OSError:
        return True


def kill_own(pids: Path) -> None:
    """Terminate only the fake children this test started."""
    for pid in live_own_pids(pids):
        with contextlib.suppress(OSError):
            os.kill(pid, getattr(signal, "SIGKILL", signal.SIGTERM))


def _rows(path: Path) -> list[dict[str, Any]]:
    if not path.is_file():
        return []
    rows: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            value = json.loads(line)
        except ValueError:
            continue
        if isinstance(value, dict):
            rows.append(value)
    return rows


def attempts(*sessions: Path) -> int:
    """Count launcher attempts (start/resume rows), excluding interactive rows."""
    return sum(
        1
        for path in sessions
        for row in _rows(path)
        if row.get("event") in ("start", "resume", "manual_resume")
        and row.get("interactive") is not True
    )


def exit_codes(*sessions: Path) -> list[int]:
    """The recorded exit code of every child this test started."""
    return [
        row["exit_code"]
        for path in sessions
        for row in _rows(path)
        if row.get("event") == "exit" and isinstance(row.get("exit_code"), int)
    ]


def diagnose(*, expect: int, markers: int, tries: int, codes: list[int], output: str) -> str:
    """A clear failure carrying the child's exit/output evidence."""
    return (
        f"fake child failed before writing its launch marker: expected {expect} "
        f"launch marker(s), got {markers} over {tries} attempt(s); child exit "
        f"codes {codes}. Captured child output:\n{output}"
    )


class Supervision:
    """The per-test watchdog, cleanup and diagnostic check."""

    def __init__(self, pids: Path) -> None:
        self.pids = pids
        self._watchdog = threading.Timer(LIVENESS_S, kill_own, args=(pids,))
        self._watchdog.daemon = True

    def start(self) -> None:
        self._watchdog.start()

    def close(self) -> None:
        self._watchdog.cancel()
        kill_own(self.pids)

    def check(self, *, expect: int, markers: int, sessions: Iterable[Path], output: str) -> None:
        """Fail clearly when a child died before its expected launch marker."""
        if markers >= expect:
            return
        paths = tuple(sessions)
        raise AssertionError(
            diagnose(
                expect=expect,
                markers=markers,
                tries=attempts(*paths),
                codes=exit_codes(*paths),
                output=output,
            )
        )


@contextlib.contextmanager
def supervision(pids: Path) -> Iterator[Supervision]:
    """Bound a hung child by LIVENESS_S and clean up only this test's children."""
    sup = Supervision(pids)
    sup.start()
    try:
        yield sup
    finally:
        sup.close()
