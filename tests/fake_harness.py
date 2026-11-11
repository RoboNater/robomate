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
    ``LIVENESS_S``, keeps sweeping after the deadline so a child that hangs
    again after a resume is also killed, and cleans up only the fake children
    this test started (recorded in its per-test pids file). Liveness is queried
    with a native Windows STILL_ACTIVE check, never ``os.kill(pid, 0)`` (which
    is CTRL_C_EVENT on Windows);
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
from time import monotonic as _monotonic
from typing import Any

import pytest
from timing import LIVENESS_S, POLL_INTERVAL_S

# A small, explicit resume count: enough for every supervision scenario (a
# healthy run resumes at most twice), but finite, so a broken fixture cannot
# spin through the launcher's unlimited production count.
SUPERVISED_MAX_RESUMES = 4
# The launcher also enforces a wall-clock resume budget; bound it by the same
# LIVENESS_S hang bound rather than inheriting the ~12-hour production value.
SUPERVISED_TOTAL_S = LIVENESS_S

# Set by the per-test ``fake_supervision`` fixture; the fake harness appends
# ``<pid>\t<its own path>\t<creation time>`` for ownership checks.
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


# On Windows signal.CTRL_C_EVENT == 0, so os.kill(pid, 0) is NOT an existence
# check: Python routes it to GenerateConsoleCtrlEvent, which can send Ctrl-C to
# every process on the console (breaking CI and interrupting unrelated work).
# Liveness there must be queried natively instead (#464 r2-1).
_IS_WINDOWS = os.name == "nt"
_STILL_ACTIVE = 259
_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000

# Prepended to every executable fake child. An empty timestamp means the
# Windows API was unavailable and cleanup falls back to the recorded PID.
CHILD_PID_RECORD = r"""
import os, sys
_created = ""
if os.name == "nt":
    try:
        import ctypes
        from ctypes import wintypes
        _kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        _kernel.GetCurrentProcess.restype = wintypes.HANDLE
        _kernel.GetProcessTimes.argtypes = ([wintypes.HANDLE]
                                             + [ctypes.POINTER(wintypes.FILETIME)] * 4)
        _times = [wintypes.FILETIME() for _ in range(4)]
        if _kernel.GetProcessTimes(_kernel.GetCurrentProcess(), *(ctypes.byref(t) for t in _times)):
            _created = str((_times[0].dwHighDateTime << 32) | _times[0].dwLowDateTime)
    except Exception:  # Unavailable creation time must not prevent cleanup.
        pass
_pids = os.environ.get("FAKE_PIDS")
if _pids:
    with open(_pids, "a", encoding="utf-8") as _stream:
        _stream.write(f"{os.getpid()}\t{sys.argv[0]}\t{_created}\n")
"""


def _windows_creation_time(pid: int) -> int | None:
    """Return a process creation FILETIME, or None when unavailable."""
    import ctypes
    from ctypes import wintypes

    try:
        kernel = getattr(ctypes, "WinDLL")("kernel32", use_last_error=True)  # noqa: B009
        kernel.OpenProcess.argtypes = [ctypes.c_uint32, ctypes.c_int, ctypes.c_uint32]
        kernel.OpenProcess.restype = ctypes.c_void_p
        kernel.GetProcessTimes.argtypes = [ctypes.c_void_p] + [
            ctypes.POINTER(wintypes.FILETIME)
        ] * 4
        kernel.CloseHandle.argtypes = [ctypes.c_void_p]
        handle = kernel.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not handle:
            return None
        try:
            times = [wintypes.FILETIME() for _ in range(4)]
            if not kernel.GetProcessTimes(handle, *(ctypes.byref(t) for t in times)):
                return None
            return (times[0].dwHighDateTime << 32) | times[0].dwLowDateTime
        finally:
            kernel.CloseHandle(handle)
    except Exception:  # Native query failures must leave cleanup operational.
        return None


def _windows_alive(pid: int) -> bool:
    """Windows liveness: OpenProcess + GetExitCodeProcess == STILL_ACTIVE.

    Never calls os.kill(pid, 0). Only called when _IS_WINDOWS is true. The
    OpenProcess signature mirrors the production agent_recovery.process_exists,
    which is exercised on the project's Windows CI.
    """
    import ctypes

    # ctypes.WinDLL is Windows-only, so resolve it by name: mypy on POSIX does
    # not see the attribute, and a type: ignore would be unused on Windows.
    kernel = getattr(ctypes, "WinDLL")("kernel32", use_last_error=True)  # noqa: B009
    kernel.OpenProcess.argtypes = [ctypes.c_uint32, ctypes.c_int, ctypes.c_uint32]
    kernel.OpenProcess.restype = ctypes.c_void_p
    kernel.GetExitCodeProcess.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint32)]
    kernel.GetExitCodeProcess.restype = ctypes.c_int
    kernel.CloseHandle.argtypes = [ctypes.c_void_p]
    handle = kernel.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return False
    try:
        code = ctypes.c_uint32()
        if not kernel.GetExitCodeProcess(handle, ctypes.byref(code)):
            return False
        return code.value == _STILL_ACTIVE
    finally:
        kernel.CloseHandle(handle)


def process_alive(pid: int) -> bool:
    """Liveness probe that never sends a console Ctrl-C on Windows."""
    if _IS_WINDOWS:
        return _windows_alive(pid)
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def terminate_own(pid: int) -> None:
    """Terminate one confirmed own child.

    On Windows SIGTERM is TerminateProcess for that specific pid; on POSIX
    SIGKILL is used. Neither is CTRL_C_EVENT/0, so no console or unrelated
    process is ever interrupted.
    """
    sig = signal.SIGTERM if _IS_WINDOWS else getattr(signal, "SIGKILL", signal.SIGTERM)
    with contextlib.suppress(OSError):
        os.kill(pid, sig)


def live_own_pids(pids: Path) -> list[int]:
    """Recorded fake children of this test that are still running."""
    if not pids.is_file():
        return []
    alive: list[int] = []
    for line in pids.read_text(encoding="utf-8").splitlines():
        parts = line.split("\t")
        if len(parts) < 2 or not parts[1]:
            continue
        pid_text, token = parts[:2]
        created = parts[2] if len(parts) == 3 else ""
        try:
            pid = int(pid_text)
        except ValueError:
            continue
        if pid <= 0 or not process_alive(pid):
            continue
        if _still_ours(pid, token, created):
            alive.append(pid)
    return alive


def _still_ours(pid: int, token: str, created: str = "") -> bool:
    """Best-effort check that ``pid`` is still one of this test's fake children.

    Where /proc exists (Linux) the child's command line names its fake-harness
    script, and an unreadable cmdline means the process is gone or its pid was
    reused: not ours to kill. Windows compares creation times when available;
    macOS and unavailable Windows timestamps fall back to the recorded PID.
    """
    if _IS_WINDOWS:
        try:
            expected = int(created)
        except ValueError:
            return True
        actual = _windows_creation_time(pid)
        return actual is None or actual == expected
    if not Path("/proc").is_dir():
        return True
    try:
        return token.encode() in Path(f"/proc/{pid}/cmdline").read_bytes()
    except OSError:
        return False


def kill_own(pids: Path) -> None:
    """Terminate only the fake children this test started."""
    for pid in live_own_pids(pids):
        terminate_own(pid)


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
        # The hang bound. A regression shortens it on this instance; the sweep
        # reads it fresh each poll so the change takes effect mid-run.
        self.liveness_s = LIVENESS_S
        self._stop = threading.Event()
        self._watchdog = threading.Thread(target=self._sweep, daemon=True)

    def start(self) -> None:
        self._watchdog.start()

    def _sweep(self) -> None:
        """Sweep own children once the LIVENESS_S deadline passes.

        A single one-shot kill is not enough (#464 r1-1): a child that hangs
        again after an automatic resume would be unbounded, because max_resumes
        is only checked after an exit. Keep sweeping at the poll interval until
        close(), so every later hung child is killed promptly and the finite
        resume count ends the launcher's loop.
        """
        started = _monotonic()
        while not self._stop.is_set():
            if _monotonic() - started >= self.liveness_s:
                kill_own(self.pids)
            if self._stop.wait(POLL_INTERVAL_S):
                return

    def close(self) -> None:
        self._stop.set()
        self._watchdog.join(timeout=POLL_INTERVAL_S * 4)
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


@pytest.fixture(autouse=True)
def fake_supervision(
    tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch
) -> Iterator[Supervision]:
    """One shared autouse fixture, re-exported by alias in the three launcher
    test modules (an import would trip ruff F401/F811 on pytest fixture params).

    The pids file lives outside the test's tmp_path: files() checks scan
    tmp_path as the hub checkout root and would otherwise see it.
    """
    pids = tmp_path_factory.mktemp("fake-harness-pids") / "pids"
    monkeypatch.setenv(PIDS_ENV, str(pids))
    with supervision(pids) as sup:
        yield sup
