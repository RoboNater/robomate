"""Machine-local live hub registry with bounded advisory locking."""

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
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def hub_healthy(url: str, hub_id: str) -> bool:
    try:
        with urllib.request.urlopen(f"{url.rstrip('/')}/healthz", timeout=0.5) as response:
            data = json.load(response)
        return isinstance(data, dict) and data.get("hub_id") == hub_id
    except (OSError, ValueError, urllib.error.URLError):
        return False


def live_entries(environ: Mapping[str, str] | None = None) -> list[dict[str, Any]]:
    path = registry_path(environ)
    with _locked(path):
        entries = _read(path)
        live = [
            item for item in entries
            if process_alive(int(item.get("pid") or 0))
            and hub_healthy(str(item.get("url") or ""), str(item.get("hub_id") or ""))
        ]
        if live != entries:
            _write(path, live)
        return live


def register(entry: Mapping[str, Any], environ: Mapping[str, str] | None = None) -> None:
    path = registry_path(environ)
    with _locked(path):
        entries = _read(path)
        entries = [item for item in entries if item.get("repo_root") != entry["repo_root"]]
        entries.append(dict(entry))
        _write(path, entries)


def deregister(
    hub_id: str, environ: Mapping[str, str] | None = None, *, pid: int | None = None
) -> None:
    path = registry_path(environ)
    with _locked(path):
        entries = _read(path)
        remaining = [
            item for item in entries
            if item.get("hub_id") != hub_id or (pid is not None and item.get("pid") != pid)
        ]
        if remaining != entries:
            _write(path, remaining)
