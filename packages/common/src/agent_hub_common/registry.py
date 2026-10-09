"""Machine-local hub registry with bounded advisory locking.

One entry per hub, keyed by `hub_id`, live or stopped (spec §4 "Machine
registry"). A stopped hub keeps its entry so its name stays taken among the
hubs of its repository; an entry whose state is gone is pruned.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import time
import urllib.error
import urllib.request
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from pathlib import Path
from typing import Any


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


def _read(path: Path) -> list[dict[str, Any]]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
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
        os.replace(temporary, path)
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


def healthz_hub_id(url: str) -> str | None:
    """The hub_id the hub at `url` reports on /healthz, or None when nothing answers."""

    try:
        with urllib.request.urlopen(f"{url.rstrip('/')}/healthz", timeout=0.5) as response:
            data = json.load(response)
    except (OSError, ValueError, urllib.error.URLError):
        return None
    hub_id = data.get("hub_id") if isinstance(data, dict) else None
    return str(hub_id) if hub_id else None


def hub_healthy(url: str, hub_id: str) -> bool:
    return bool(url) and healthz_hub_id(url) == hub_id


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


def _live(entry: Mapping[str, Any]) -> bool:
    return process_alive(int(entry.get("pid") or 0)) and hub_healthy(
        str(entry.get("url") or ""), str(entry.get("hub_id") or "")
    )


def hub_entries(environ: Mapping[str, str] | None = None) -> list[dict[str, Any]]:
    """Every registered hub, each with a computed `live` flag; prunes stale entries."""

    path = registry_path(environ)
    with _locked(path):
        entries = _read(path)
        checked = [(item, _live(item)) for item in entries]
        kept = [(item, live) for item, live in checked if live or not _stale(item)]
        if len(kept) != len(entries):
            _write(path, [item for item, _ in kept])
    return [item | {"live": live} for item, live in kept]


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
