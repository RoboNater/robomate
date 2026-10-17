"""Machine-local hub registry with bounded advisory locking.

One entry per hub, keyed by `hub_id`, live or stopped (spec §4 "Machine
registry"). A stopped hub keeps its entry so its name stays taken among the
hubs of its repository; an entry whose state is gone is pruned.
"""

from __future__ import annotations

import http.client
import io
import json
import os
import socket
import ssl
import sys
import tempfile
import time
import urllib.parse
from collections.abc import Iterator, Mapping
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .sharing import SHARING_RETRY_DELAYS_S, retry_sharing_violation


class RegistryError(RuntimeError):
    """The registry could not be read or updated safely."""


def registry_path(environ: Mapping[str, str] | None = None) -> Path:
    env = os.environ if environ is None else environ
    if sys.platform == "win32":
        base = Path(env.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local")
    else:
        configured = Path(env.get("XDG_STATE_HOME", ""))
        base = configured if configured.is_absolute() else Path.home() / ".local" / "state"
    return base / "robomate" / "hubs.json"


# Overrides the operator credential's location, for tests (#128).
OPERATOR_TOKEN_FILE_ENV = "ROBOMATE_OPERATOR_TOKEN_FILE"


def operator_token_path(environ: Mapping[str, str] | None = None) -> Path:
    """Where the operator credential lives: beside `hubs.json`, never in a repository.

    Agents work inside repositories and read `.robomate/` through discovery, so
    the one credential they must not hold is kept in the machine's registry
    directory instead (#128).
    """

    env = os.environ if environ is None else environ
    configured = env.get(OPERATOR_TOKEN_FILE_ENV, "").strip()
    if configured:
        return Path(configured).expanduser()
    return registry_path(env).with_name("operator-token")


@contextmanager
def _locked(path: Path) -> Iterator[None]:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    lock = path.with_name("registry.lock")
    deadline = time.monotonic() + 5
    while True:
        try:
            fd = os.open(lock, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            os.close(fd)
            break
        except FileExistsError:
            try:
                if time.time() - lock.stat().st_mtime > 30:
                    lock.unlink(missing_ok=True)
                    continue
            except FileNotFoundError:
                continue
            if time.monotonic() >= deadline:
                raise RegistryError(f"timed out waiting for registry lock {lock}") from None
            time.sleep(0.05)
    try:
        yield
    finally:
        lock.unlink(missing_ok=True)


# The registry lock serializes robomate's own readers and writers, but not a
# process that opens hubs.json without it: a scanner (antivirus, indexer), a
# tool, or a test. On Windows that makes a read or os.replace fail with a
# transient sharing violation, as #103 and #114 found for hub.json (#180).
# Retry briefly with the same delays; a file that stays locked still fails.
_RETRY_DELAYS_S = SHARING_RETRY_DELAYS_S


def _read(path: Path) -> list[dict[str, Any]]:
    try:
        data = json.loads(
            retry_sharing_violation(lambda: path.read_text(encoding="utf-8"), _RETRY_DELAYS_S)
        )
    except FileNotFoundError:
        return []
    except (OSError, ValueError) as exc:
        raise RegistryError(f"cannot read registry {path}: {exc}") from exc
    if not isinstance(data, list) or not all(isinstance(item, dict) for item in data):
        raise RegistryError(f"invalid registry {path}")
    return data


def _write(path: Path, entries: list[dict[str, Any]]) -> None:
    fd, temporary = tempfile.mkstemp(prefix=".hubs-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(entries, stream, indent=2, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        retry_sharing_violation(lambda: os.replace(temporary, path), _RETRY_DELAYS_S)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def process_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if sys.platform == "win32":
        return _windows_process_alive(pid)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _windows_process_alive(pid: int) -> bool:
    """Probe a Windows process handle without delivering a console event."""

    # An if, not an assert: inside a function mypy narrows sys.platform
    # only on an if, and the narrowing is what types ctypes.WinDLL below.
    if sys.platform != "win32":
        raise OSError("Windows only")

    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    open_process = kernel32.OpenProcess
    open_process.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    open_process.restype = wintypes.HANDLE
    wait_for_single = kernel32.WaitForSingleObject
    wait_for_single.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    wait_for_single.restype = wintypes.DWORD
    close_handle = kernel32.CloseHandle
    close_handle.argtypes = [wintypes.HANDLE]
    close_handle.restype = wintypes.BOOL
    handle = open_process(0x00100000, False, pid)  # SYNCHRONIZE
    if not handle:
        # Access denied means a process exists; other failures (notably an
        # invalid PID) mean it does not. The health check still verifies ID.
        return int(ctypes.get_last_error()) == 5
    try:
        # Zero timeout is a nonblocking state check. A signaled process has
        # exited; WAIT_TIMEOUT (or an indeterminate failure) is treated alive.
        return int(wait_for_single(handle, 0)) != 0  # WAIT_OBJECT_0
    finally:
        close_handle(handle)


# /healthz budgets (#230). A hub busy on a loaded host can take seconds to
# answer: at 0.5 s a live hub was reported unreachable before the longer RPC
# timeout (#207) could help. A refused connection still fails at once (about
# 2 s on Windows, which retries a refused loopback connect), and an answer
# from the wrong hub is refused as soon as it arrives, so only a silent or
# trickling endpoint waits out the budget. When the recorded pid is visible
# the hub is probably busy rather than gone, so it gets as long as an RPC would.
HEALTHZ_TIMEOUT_S = 5.0
HEALTHZ_LIVE_PID_TIMEOUT_S = 15.0
# A /healthz answer is a few dozen bytes; anything this large is not a hub.
_HEALTHZ_MAX_BYTES = 64 * 1024


@dataclass(frozen=True)
class HealthCheck:
    """What one /healthz probe found: the hub_id that answered, or why none did."""

    hub_id: str | None
    # Why no hub_id came back, phrased to follow "<hub> at <url>": None when
    # one did, else "unreachable" (refused, reset, no route), "did not answer
    # /healthz within N s", or "answered /healthz ... without a hub_id".
    failure: str | None = None
    timed_out: bool = False

    def describe(self) -> str:
        """The failure as a predicate: "is unreachable", "did not answer ..."."""

        return f"is {self.failure}" if self.failure == "unreachable" else str(self.failure)


class _Received:
    """A finished response's bytes, in the shape http.client parses from."""

    def __init__(self, data: bytes) -> None:
        self.data = data

    def makefile(self, *_args: object, **_kwargs: object) -> io.BytesIO:
        return io.BytesIO(self.data)


def _get_within(url: str, deadline: float) -> tuple[int, bytes]:
    """GET `url` and return its status and body, all of it before `deadline`.

    urllib's timeout bounds each socket operation, so a response that keeps
    sending a byte at a time never times out (#266 r1-1). Here every connect
    and recv waits only for what is left of one deadline, and the response is
    parsed once the server has closed the connection. Raises TimeoutError
    when the deadline passes first, OSError for a refused or reset
    connection, and ValueError for anything that is not an HTTP response.
    """

    parts = urllib.parse.urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise ValueError(f"not an http URL: {url}")
    port = parts.port or (443 if parts.scheme == "https" else 80)

    def remaining() -> float:
        left = deadline - time.monotonic()
        if left <= 0:
            raise TimeoutError
        return left

    context = ssl.create_default_context() if parts.scheme == "https" else None
    sock = socket.create_connection((parts.hostname, port), timeout=remaining())
    try:
        if context is not None:
            # The handshake gets only what connecting left of the budget
            # (#266 r2-1); CPython's do_handshake keeps to one deadline across
            # its reads and writes.
            tls = context.wrap_socket(
                sock, server_hostname=parts.hostname, do_handshake_on_connect=False
            )
            sock = tls
            tls.settimeout(remaining())
            tls.do_handshake()
        host = parts.netloc.rpartition("@")[2]
        sock.settimeout(remaining())
        sock.sendall(
            f"GET {parts.path or '/'} HTTP/1.1\r\nHost: {host}\r\n"
            "Accept: application/json\r\nConnection: close\r\n\r\n".encode("ascii")
        )
        received = bytearray()
        while True:
            sock.settimeout(remaining())
            chunk = sock.recv(4096)
            if not chunk:
                break
            received += chunk
            if len(received) > _HEALTHZ_MAX_BYTES:
                raise ValueError("response too large")
    finally:
        sock.close()
    response = http.client.HTTPResponse(_Received(bytes(received)))  # type: ignore[arg-type]
    try:
        response.begin()
        return response.status, response.read()
    except (http.client.HTTPException, OSError) as exc:
        raise ValueError(f"malformed response: {exc}") from exc


def check_healthz(url: str, timeout_s: float = HEALTHZ_TIMEOUT_S) -> HealthCheck:
    """Ask the hub at `url` for its hub_id, waiting at most `timeout_s` in all.

    The budget covers connecting, the headers and the whole body (#266 r1-1);
    only resolving a host name falls outside it, and hub URLs are loopback
    addresses.
    """

    try:
        status, body = _get_within(f"{url.rstrip('/')}/healthz", time.monotonic() + timeout_s)
    except TimeoutError:
        return HealthCheck(None, f"did not answer /healthz within {timeout_s:g} s", True)
    except ValueError:
        return HealthCheck(None, "answered /healthz without a hub_id")
    except OSError:
        return HealthCheck(None, "unreachable")
    if status != 200:
        return HealthCheck(None, f"answered /healthz with HTTP {status}, without a hub_id")
    try:
        data = json.loads(body)
    except ValueError:
        return HealthCheck(None, "answered /healthz without a hub_id")
    hub_id = data.get("hub_id") if isinstance(data, dict) else None
    if not hub_id:
        return HealthCheck(None, "answered /healthz without a hub_id")
    return HealthCheck(str(hub_id))


def healthz_hub_id(url: str, timeout_s: float = HEALTHZ_TIMEOUT_S) -> str | None:
    """The hub_id the hub at `url` reports on /healthz, or None when nothing answers."""

    return check_healthz(url, timeout_s).hub_id


def hub_healthy(url: str, hub_id: str, timeout_s: float = HEALTHZ_TIMEOUT_S) -> bool:
    return bool(url) and healthz_hub_id(url, timeout_s) == hub_id


def _stale(entry: Mapping[str, Any]) -> bool:
    """Whether an entry's state is gone, or now belongs to another hub."""

    from .discovery import DiscoveryError, hub_checkout, read_hub_json

    checkout = hub_checkout(entry)
    if checkout is None:
        return True
    try:
        info = read_hub_json(checkout)
    except DiscoveryError:
        return False
    return info is None or info.get("hub_id") != entry.get("hub_id")


# hub_entries probes every entry with a visible pid at once, each on its own
# thread, outside the registry lock. A silent entry neither holds up registry
# writers (whose lock wait is 5 s) nor delays the others, and no entry waits
# in a queue behind silent ones (#266 r1-2). The probes share one deadline, a
# live-pid budget away, which check_healthz keeps to in full, so the listing
# waits at most that long. An entry whose pid is not visible is not live and
# is never probed, so there are only as many threads as running hubs.


def _live(entry: Mapping[str, Any], deadline: float) -> bool:
    remaining = deadline - time.monotonic()
    return remaining > 0 and hub_healthy(
        str(entry.get("url") or ""), str(entry.get("hub_id") or ""), remaining
    )


def hub_entries(environ: Mapping[str, str] | None = None) -> list[dict[str, Any]]:
    """Every registered hub, each with a computed `live` flag; prunes stale entries."""

    path = registry_path(environ)
    with _locked(path):
        entries = _read(path)
    running = [
        index for index, item in enumerate(entries) if process_alive(int(item.get("pid") or 0))
    ]
    deadline = time.monotonic() + HEALTHZ_LIVE_PID_TIMEOUT_S
    live: set[int] = set()
    if running:
        with ThreadPoolExecutor(max_workers=len(running)) as pool:
            answers = list(pool.map(lambda index: _live(entries[index], deadline), running))
        live = {index for index, ok in zip(running, answers, strict=True) if ok}
    checked = [(item, index in live) for index, item in enumerate(entries)]
    gone = [item for item, live in checked if not live and _stale(item)]
    if gone:
        with _locked(path):
            # Only entries unchanged since the probe go: one re-registered or
            # updated meanwhile is newer than what was checked.
            current = _read(path)
            kept = [item for item in current if item not in gone]
            if len(kept) != len(current):
                _write(path, kept)
    return [item | {"live": live} for item, live in checked if item not in gone]


def live_entries(environ: Mapping[str, str] | None = None) -> list[dict[str, Any]]:
    return [item for item in hub_entries(environ) if item["live"]]


def register(entry: Mapping[str, Any], environ: Mapping[str, str] | None = None) -> None:
    """Record or update one hub by `hub_id`, never evicting another.

    A name another hub of the same git common directory holds is refused:
    `up` never picks a different name silently (spec §4 "Hub name").
    """

    path = registry_path(environ)
    with _locked(path):
        entries = _read(path)
        name, common = entry.get("name"), entry.get("git_common_dir")
        for item in entries:
            if (
                name
                and item.get("hub_id") != entry["hub_id"]
                and item.get("name") == name
                and item.get("git_common_dir") == common
                and not _stale(item)
            ):
                raise RegistryError(
                    f"hub name {name!r} is taken by hub {item.get('hub_id')} of checkout "
                    f"{item.get('checkout')} in the same repository; choose another with "
                    "robomate up --name"
                )
        entries = [item for item in entries if item.get("hub_id") != entry["hub_id"]]
        entries.append(dict(entry))
        _write(path, entries)


def mark_stopped(
    hub_id: str, environ: Mapping[str, str] | None = None, *, pid: int | None = None
) -> None:
    """Record that a hub stopped, keeping its entry and so its name."""

    path = registry_path(environ)
    with _locked(path):
        entries = _read(path)
        updated = [
            item | {"pid": None}
            if item.get("hub_id") == hub_id and (pid is None or item.get("pid") == pid)
            else item
            for item in entries
        ]
        if updated != entries:
            _write(path, updated)


def forget(hub_id: str, environ: Mapping[str, str] | None = None) -> dict[str, Any] | None:
    """Remove one hub's registry entry, freeing its name and port (#159).

    Hub state and run reports are untouched; only the machine registry entry
    goes. The next `up` in that checkout re-registers the hub and takes the
    first free port. Returns the removed entry, or None when absent.
    """

    path = registry_path(environ)
    with _locked(path):
        entries = _read(path)
        removed = [item for item in entries if item.get("hub_id") == hub_id]
        if not removed:
            return None
        _write(path, [item for item in entries if item.get("hub_id") != hub_id])
        return removed[0]
