"""Start and stop a checkout's hub, and answer its operator questions."""

from __future__ import annotations

import argparse
import asyncio
import errno
import hashlib
import json
import logging
import os
import socket
import sys
import tomllib
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
import uuid
from collections.abc import Iterator
from contextlib import ExitStack, contextmanager
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, NoReturn

import anyio
from agent_hub.forge_preflight import gitlab_preflight, parse_github_slug
from agent_hub.main import serve_http
from agent_hub_common import (
    MAX_STATEMENT_BYTES,
    HubSettings,
    load_config_layers,
    load_or_create_token,
    read_token_file,
    reserve_stdout,
)
from agent_hub_common.config_layers import repository_config_path
from agent_hub_common.discovery import (
    BINDING_FILENAME,
    Binding,
    DiscoveryError,
    HubEndpoint,
    HubPlacement,
    LocalHub,
    Repository,
    checkout_git_dir,
    create_hub_dirs,
    derive_hub_name,
    ensure_excluded,
    find_hub,
    hub_checkout,
    is_legacy_state_dir,
    is_linked_worktree,
    legacy_state_dir,
    local_hub,
    plan_hub_placement,
    read_binding,
    read_hub_json,
    repository_dir,
    resolve_checkout,
    resolve_repository,
    select_hub,
    stale_partial_text,
    state_dir,
    token_file,
    validate_hub_name,
    verify_local_hub,
    write_binding,
    write_hub_json,
)
from agent_hub_common.registry import (
    HEALTHZ_LIVE_PID_TIMEOUT_S,
    HEALTHZ_TIMEOUT_S,
    RegistryError,
    check_healthz,
    hub_entries,
    live_entries,
    mark_stopped,
    operator_token_path,
    process_alive,
    recorded_entries,
    register,
)
from agent_hub_common.registry import (
    forget as forget_entry,
)
from agent_hub_common.relocation import relocate_hub, relocation_targets
from agent_hub_common.stdio import escape_unencodable_output
from worker_mcp.main import run_worker_bridge, serve_mcp
from worker_mcp.orchestrator import OrchestratorBridge, create_orchestrator_mcp

VERSION = "0.1.0"


DEFAULT_BASE_PORT = 8420


def _first_free_base() -> int:
    """Base of the first-free port scan (#198).

    Production default is 8420. Tests set ROBOMATE_BASE_PORT to a reserved
    high port block so the scan does not share 842x with other hubs or test
    runs on the same host.
    """

    raw = os.environ.get("ROBOMATE_BASE_PORT", "").strip()
    if not raw:
        return DEFAULT_BASE_PORT
    try:
        base = int(raw)
    except ValueError as exc:
        raise RuntimeError(f"ROBOMATE_BASE_PORT must be an integer, got {raw!r}") from exc
    if not 1 <= base <= 65535:
        raise RuntimeError(f"ROBOMATE_BASE_PORT must be between 1 and 65535, got {raw!r}")
    return base


# Errors that make the first-free scan move on to the next port: the port is
# in use (EADDRINUSE, WSAEADDRINUSE 10048), or this process may not bind it
# (EACCES, WSAEACCES 10013: a Windows excluded range or an exclusive holder,
# or a privileged port on POSIX) (#229).
_SKIPPED_BIND_ERRORS = frozenset({errno.EADDRINUSE, 10048, errno.EACCES, 10013})


def _bind(
    host: str, port: int | None, avoid: frozenset[int] = frozenset()
) -> tuple[socket.socket, int]:
    """Bind the pinned or saved port, or the first free one from the base not in `avoid`.

    The first-free scan skips ports in use or forbidden to this process. A
    pinned or saved port is never moved: any bind failure stops startup, since
    remote agents are configured with that port (#229).
    """

    family = socket.AF_INET6 if ":" in host else socket.AF_INET
    base = DEFAULT_BASE_PORT if port is not None else _first_free_base()
    if port is not None:
        ports: Iterator[int] = iter([port])
    else:
        ports = (p for p in range(base, 65536) if p not in avoid)
    for candidate in ports:
        sock = socket.socket(family, socket.SOCK_STREAM)
        if os.name != "nt":
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind((host, candidate))
            sock.listen()
            return sock, candidate
        except OSError as exc:
            sock.close()
            if port is not None or exc.errno not in _SKIPPED_BIND_ERRORS:
                raise RuntimeError(
                    f"port {candidate} is unavailable; choose --port: {exc}"
                ) from exc
    raise RuntimeError(f"no free port from {base} upward")


# Long enough for a loaded host (a CI runner mid-suite) to answer: at 2 s,
# `down` and `answer` timed out against a live hub (#207).
_RPC_TIMEOUT_S = 15.0
# `hub.submit` runs forge preflight in-request: GitLab up to 3 base + 2 policy
# `glab` calls, GitHub up to 2 `gh` calls, each allowed 30 s (worst-case ~150 s).
# A 15 s client timeout would report failure while the hub goes on to queue the
# run and emit `work_submitted`, and a retry then fails -32002 (r1-1). Submit
# gets its own deadline above that worst case.
_SUBMIT_RPC_TIMEOUT_S = 180.0


def _timed_out(exc: BaseException) -> bool:
    """Whether a urllib failure is a timeout, not a refused or reset connection."""

    if isinstance(exc, urllib.error.URLError):
        return isinstance(exc.reason, TimeoutError)
    return isinstance(exc, TimeoutError)


def _rpc(
    url: str,
    token: str,
    method: str,
    *,
    operator_token: str | None = None,
    params: dict[str, Any] | None = None,
    timeout: float = _RPC_TIMEOUT_S,
) -> dict[str, Any]:
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    if operator_token is not None:
        headers["X-Robomate-Operator"] = operator_token
    request = urllib.request.Request(
        f"{url.rstrip('/')}/rpc",
        data=json.dumps(
            {"jsonrpc": "2.0", "id": 1, "method": method, "params": params or {}}
        ).encode(),
        headers=headers,
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        result: dict[str, Any] = json.load(response)
    if "error" in result:
        error = result["error"]
        if isinstance(error, dict) and isinstance(error.get("message"), str):
            raise RuntimeError(f"hub refused {method}: {error['message']} ({error.get('code')})")
        raise RuntimeError(str(error))
    value = result["result"]
    if not isinstance(value, dict):
        raise RuntimeError("invalid hub RPC response")
    return value


class HubLockBusy(RuntimeError):
    """Another process holds this hub's up.lock (it is running or starting)."""


@contextmanager
def _hub_lock(state: Path) -> Iterator[None]:
    """Hold this hub's OS file lock for its lifetime, including startup.

    The lock is per hub, in its state directory: it never blocks a sibling hub.

    Raises HubLockBusy when another process holds the lock (the hub is
    running or starting).
    """

    fd = os.open(state / "up.lock", os.O_RDWR | os.O_CREAT, 0o600)
    locked = False
    try:
        try:
            if sys.platform == "win32":
                import msvcrt

                if os.fstat(fd).st_size == 0:
                    os.write(fd, b"0")
                os.lseek(fd, 0, os.SEEK_SET)
                msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            locked = True
        except OSError as exc:
            info = read_hub_json(state) or {}
            url = info.get("url")
            message = f"hub already running at {url}" if url else "hub already starting"
            raise HubLockBusy(message) from exc
        yield
    finally:
        try:
            if locked:
                if sys.platform == "win32":
                    os.lseek(fd, 0, os.SEEK_SET)
                    msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
                else:
                    fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)


async def _up(args: argparse.Namespace) -> None:
    escape_unencodable_output()
    repo = resolve_repository(Path.cwd(), probe_cli=args.forge is None)
    if args.forge:
        repo = replace(repo, forge=args.forge)
    if args.name is not None:
        validate_hub_name(args.name)
    placement = plan_hub_placement(repo, args.name)
    create_hub_dirs(placement)
    with _hub_lock(placement.state_dir):
        await _run_up(args, repo, placement)


def _hub_name(requested: str | None, recorded: object, checkout: Path) -> str:
    """The recorded name, else the requested or derived one (spec §4 "Hub name")."""

    if recorded:
        if requested is not None and requested != recorded:
            raise RuntimeError(
                f"this hub is named {recorded!r}; a hub's name is fixed once recorded, "
                "because agent worktrees and branches carry it"
            )
        return str(recorded)
    return validate_hub_name(requested) if requested is not None else derive_hub_name(checkout)


def _entry_port(entry: dict[str, Any]) -> int | None:
    port = entry.get("port") or urllib.parse.urlparse(str(entry.get("url") or "")).port
    return int(port) if port else None


async def _run_up(args: argparse.Namespace, repo: Repository, placement: HubPlacement) -> None:
    directory = placement.state_dir
    layered_config = load_config_layers(
        hub_config=directory / "config.toml",
        git_common_dir=repo.git_common_dir,
    )
    for warning in layered_config.warnings:
        print(f"Warning: {warning}", flush=True)
    if placement.legacy:
        ensure_excluded(repo.git_common_dir)
    old = read_hub_json(directory) or {}
    old_pid = int(old.get("pid") or 0)
    if old_pid and old.get("url") and process_alive(old_pid):
        health = check_healthz(str(old["url"]), HEALTHZ_LIVE_PID_TIMEOUT_S)
        if health.hub_id is not None and health.hub_id == old.get("hub_id"):
            raise RuntimeError(f"hub already running at {old['url']}")
        # Silent, with its process alive: most likely the hub itself, too busy
        # to answer. Starting another over it would share its state (#230).
        if health.timed_out:
            raise RuntimeError(
                f"hub at {old['url']} {health.describe()}; recorded pid {old_pid} is alive. "
                "Wait for it, or stop it with robomate down"
            )
    hub_id = str(old.get("hub_id") or uuid.uuid4().hex)
    # A hub forgotten with `robomate forget` (#159) carries an explicit
    # `released_port` marker in hub.json: its port counts as released, so the
    # next up takes the first free port instead of reusing a saved one, and
    # says so once the new port is known. Registry absence alone means
    # nothing: a hub.json written without registering (or a lost registry)
    # keeps its saved port pinned, as before.
    released_port: int | None = None
    if old.get("hub_id") == hub_id and old.get("released_port") is not None:
        try:
            released_port = int(old["released_port"])
        except (TypeError, ValueError):
            released_port = None
    recorded = old.get("name")
    if recorded and args.name is not None and args.name != recorded:
        # A name is claimed only while its hub is registered. A hub whose
        # registry entry is gone (forgotten with `robomate forget`, #159) may
        # be renamed explicitly at its next up, since a sibling hub may have
        # taken the old name; otherwise the name stays fixed because agent
        # worktrees and branches carry it.
        if any(item.get("hub_id") == hub_id for item in hub_entries()):
            raise RuntimeError(
                f"this hub is named {recorded!r}; a hub's name is fixed once recorded, "
                "because agent worktrees and branches carry it"
            )
        name = validate_hub_name(args.name)
    else:
        name = _hub_name(args.name, old.get("name"), repo.root)
    identity = {
        "hub_id": hub_id,
        "name": name,
        "checkout": str(repo.root),
        # The same directory, for readers from before checkout scope (#147),
        # and for hub.info and hub.status callers such as the #133 driver.
        "repo_root": str(repo.root),
        "git_common_dir": str(repo.git_common_dir),
    }
    entry = identity | {
        "state_dir": str(directory),
        "token_file": str(placement.token_file),
    }
    if placement.legacy and is_linked_worktree(repo.root, repo.git_common_dir):
        print(
            f"Warning: this hub's state is in {directory}, inside a linked worktree; "
            "`git worktree remove` deletes it, with the hub's history and run reports. "
            "Move it out of the checkout with robomate relocate while the hub is stopped.",
            flush=True,
        )
    if repo.forge == "unknown":
        print(
            "Warning: forge is unknown; check_merge_gate will use the GitHub gate. "
            "Choose --forge github|gitlab to override.",
            flush=True,
        )
    elif repo.forge == "gitlab":
        checks = await gitlab_preflight(repo.origin)
        for check in checks:
            print(f"{check.status.upper()}: {check.name}: {check.detail}", flush=True)
        if any(check.status == "refuse" for check in checks):
            raise RuntimeError(
                "GitLab preflight refused startup; disable the named unsupported "
                "settings or correct the origin before running up again"
            )
    # The owner binding (spec §4) is this checkout's own record of the hub it
    # owns and where its state and token are; it holds no token. It is written
    # with the identity, before hub.json, so a sibling `up` of this checkout
    # that looks while this one starts finds the hub bound, and meets the
    # lock, not a "name taken" directory.
    git_dir = checkout_git_dir(repo.root)
    previous_binding = read_binding(git_dir)
    binding = Binding("owner", hub_id, name, repo.root, directory, placement.token_file)
    if previous_binding != binding:
        write_binding(git_dir, binding)
    # Record the identity, then claim the name among the repository's hubs
    # before anything starts. The registry prunes an entry without hub.json,
    # so the file comes first, and is restored if the name is refused (the
    # binding with it).
    # `released_port` was read above from the forget marker in hub.json: while
    # it is set the startup entry claims no port, so the released port is
    # reusable by a sibling hub at once.
    write_hub_json(directory, old | identity)
    try:
        register(
            entry
            | (
                {"url": None, "port": None, "pid": None}
                if released_port is not None and args.port is None
                else {"url": old.get("url"), "port": old.get("port"), "pid": None}
            )
        )
    except RegistryError:
        if old:
            write_hub_json(directory, old)
        else:
            (directory / "hub.json").unlink(missing_ok=True)
        if previous_binding is not None:
            write_binding(git_dir, previous_binding)
        else:
            (git_dir / BINDING_FILENAME).unlink(missing_ok=True)
        raise
    if args.port is not None:
        requested_port = args.port
        released_port = None
    elif released_port is not None:
        requested_port = None
    else:
        requested_port = old.get("port")
    taken = frozenset(
        port
        for item in hub_entries()
        if item.get("hub_id") != hub_id and (port := _entry_port(item)) is not None
    )
    # Validate the operator-token path before binding (#192): an unsafe path
    # inside the repository or hub state is refused with the token-path
    # diagnostic even when the saved or requested port is occupied. Binding
    # first would mask it with the port-unavailable diagnostic.
    _validate_operator_token_path(operator_token_path(), repo, placement)
    sock, port = _bind(
        args.bind, int(requested_port) if requested_port is not None else None, taken
    )
    overlay = dict(os.environ)
    overlay.update(
        {
            "HUB_STATE_DIR": str(directory),
            "HUB_HOST": args.bind,
            "HUB_PORT": str(port),
            "HUB_DB_PATH": str(directory / "hub.db"),
            "HUB_TOKEN_FILE": str(placement.token_file),
            "HUB_CALL_ACCOUNTING": "0" if args.no_call_accounting else "1",
        }
    )
    overlay.pop("HUB_TOKEN", None)
    if args.public_url:
        overlay["HUB_PUBLIC_URL"] = args.public_url
    try:
        settings = HubSettings.from_env(overlay)
        load_or_create_token(settings.token, settings.token_file)
        _provision_operator_token(settings, repo, placement)
        info: dict[str, Any] = {
            **identity,
            "origin": repo.origin,
            "forge": repo.forge,
            "default_branch": repo.default_branch,
            "url": settings.public_url,
            "port": port,
            "pid": os.getpid(),
            "started_at": datetime.now(UTC).isoformat(),
            "robomate_version": VERSION,
        }

        def started() -> None:
            write_hub_json(directory, info)
            register(entry | {"url": settings.public_url, "port": port, "pid": os.getpid()})
            print(f"Hub {name} running at {settings.public_url}", flush=True)
            if released_port is not None and released_port != port:
                print(
                    f"Hub {name} previously used port {released_port}; its port was "
                    f"released, so it now runs at port {port}. Remote agents "
                    "configured with the old port need the new one.",
                    flush=True,
                )
            elif released_port is not None:
                print(
                    f"Hub {name} port {port} was released; it was still the first "
                    "free port, so the hub reuses it.",
                    flush=True,
                )
            print(f"Hub checkout: {repo.root}", flush=True)
            print(f"Hub state: {directory}", flush=True)
            print(f"ROBOMATE_HUB_URL={settings.public_url}", flush=True)
            print(f"ROBOMATE_TOKEN_FILE={settings.token_file}", flush=True)

        try:
            await serve_http(settings, [sock], info, started)
        finally:
            # The shutdown write can hit a transient Windows sharing
            # violation while a reader holds hub.json (#114). It must never
            # skip mark_stopped, so the registry update runs even if the
            # read or write still fails after retries.
            try:
                current = read_hub_json(directory)
                if (
                    current
                    and current.get("hub_id") == info["hub_id"]
                    and current.get("pid") == os.getpid()
                ):
                    current["pid"] = None
                    current["started_at"] = None
                    write_hub_json(directory, current)
            finally:
                mark_stopped(hub_id, pid=os.getpid())
    finally:
        sock.close()


def _operator_token_off_limits(repo: Repository, placement: HubPlacement) -> tuple[Path, ...]:
    """Where agents read: the repository, the hub's state and token, and its namespace."""

    places = [
        repo.root,
        placement.state_dir,
        placement.token_file.parent,
        repository_dir(repo.git_common_dir),
    ]
    if not placement.legacy:
        # `<worktree_root>/<hub>/` also holds the hub's agent worktrees (spec §6).
        places.append(placement.state_dir.parent)
    return tuple(places)


def _validate_operator_token_path(
    path: Path | None, repo: Repository, placement: HubPlacement
) -> Path | None:
    """Refuse an operator-token path inside the repository or hub state (#128, #192).

    Pure validation without creating any credential, so `_run_up` can run it
    before binding the port. The token and its path are never printed.
    """

    if path is None:
        return None
    resolved = path.resolve()
    for place in _operator_token_off_limits(repo, placement):
        if resolved.is_relative_to(place.resolve()):
            raise RuntimeError(
                f"the operator token file must not be inside the repository or the "
                f"hub's state ({place})"
            )
    return resolved


def _provision_operator_token(
    settings: HubSettings, repo: Repository, placement: HubPlacement
) -> None:
    """Create the operator credential beside the machine registry (#128).

    Agents read the repository, its checkouts and the hub's state, so a path
    into any of them is refused rather than handing them the one credential
    they must not hold. The token and its path are never printed.
    """

    resolved = _validate_operator_token_path(settings.operator_token_file, repo, placement)
    if resolved is None:
        return
    resolved.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    load_or_create_token(None, resolved, label="operator token")


def _connect(selector: str | None) -> tuple[HubEndpoint, LocalHub | None]:
    """The selected or discovered hub's endpoint, and the local hub when one was found."""

    hub = find_hub(Path.cwd(), selector=selector)
    if isinstance(hub, LocalHub):
        verify_local_hub(hub)
        return hub.endpoint(), hub
    return hub, None


def _down(selector: str | None) -> None:
    endpoint, hub = _connect(selector)
    label = hub.label() if hub is not None else "hub"
    operator_token = read_token_file(operator_token_path(), "operator token")
    try:
        result = _rpc(endpoint.url, endpoint.token, "hub.shutdown", operator_token=operator_token)
    except urllib.error.HTTPError as exc:
        raise RuntimeError(
            f"{label} at {endpoint.url} rejected shutdown (HTTP {exc.code})"
        ) from exc
    except (OSError, urllib.error.URLError) as exc:
        pid = int(hub.info.get("pid") or 0) if hub is not None else 0
        # Name the stale state a hung or killed `up` left behind (#301).
        stale = ""
        if hub is not None:
            stale = (
                f"; stale state remains in {hub.state_dir / 'hub.json'} "
                f"(hub_id {hub.hub_id}, pid {pid or '-'}, "
                f"port {hub.info.get('port') or '-'})"
            )
        if _timed_out(exc):
            alive = f"; pid {pid} is still alive" if process_alive(pid) else ""
            raise RuntimeError(
                f"{label} at {endpoint.url} did not answer shutdown within "
                f"{_RPC_TIMEOUT_S:g} s{alive}{stale}"
            ) from exc
        if process_alive(pid):
            raise RuntimeError(
                f"{label} at {endpoint.url} is unreachable; pid {pid} is still alive{stale}"
            ) from exc
        raise RuntimeError(f"{label} at {endpoint.url} is not running{stale}") from exc
    if not result.get("stopping"):
        raise RuntimeError("hub did not acknowledge shutdown")
    print(f"Stopping {label} at {endpoint.url}")


def _down_all(as_json: bool) -> None:
    """Stop every live hub in the machine registry (operator-only, #159).

    Uses the operator credential, as `robomate down` does. Reports what was
    stopped and what failed; hub state and run reports are kept.
    """

    entries = live_entries()
    if not entries:
        if as_json:
            print(json.dumps([]))
        else:
            print("No live hubs.")
        return
    operator_token = read_token_file(operator_token_path(), "operator token")
    outcomes: list[dict[str, Any]] = []
    for entry in entries:
        name = entry.get("name") or "(unnamed)"
        url = str(entry.get("url") or "")
        hub_id = str(entry.get("hub_id") or "")
        try:
            token = token_file(entry).read_text(encoding="utf-8").strip()
            result = _rpc(url, token, "hub.shutdown", operator_token=operator_token)
            if not result.get("stopping"):
                raise RuntimeError("hub did not acknowledge shutdown")
            outcomes.append({"hub_id": hub_id, "name": name, "url": url, "stopped": True})
            if not as_json:
                print(f"Stopping hub {name} at {url}")
        except (
            OSError,
            ValueError,
            KeyError,
            urllib.error.URLError,
            urllib.error.HTTPError,
            RuntimeError,
        ) as exc:
            outcomes.append(
                {"hub_id": hub_id, "name": name, "url": url, "stopped": False, "error": str(exc)}
            )
            if not as_json:
                print(f"Failed to stop hub {name} at {url}: {exc}")
    if as_json:
        print(json.dumps(outcomes))
    elif outcomes and all(item["stopped"] for item in outcomes):
        print(f"Stopped {len(outcomes)} hub(s).")
    if any(not item["stopped"] for item in outcomes):
        failed = sum(1 for item in outcomes if not item["stopped"])
        raise RuntimeError(f"failed to stop {failed} of {len(outcomes)} hub(s)")


def _forget(selectors: list[str], forget_all: bool, as_json: bool) -> None:
    """Forget stopped hubs: drop their registry entries, freeing name and port (#159).

    Nothing is deleted: hub.db and run reports stay. hub.json keeps its
    identity and last address, records the released port, and clears pid and
    started_at. A forgotten hub's next `up` takes the first free port and
    says so. Live hubs are never forgotten; stop them first.
    """

    if forget_all and selectors:
        raise ValueError("give either hub names or --all, not both")
    if not forget_all and not selectors:
        checkout, _ = resolve_checkout(Path.cwd())
        hub = local_hub(checkout)
        if hub is None:
            raise DiscoveryError(f"no hub is recorded for checkout {checkout}")
        selectors = [hub.hub_id]
    entries = {str(item.get("hub_id")): item for item in hub_entries()}
    targets: list[dict[str, Any]] = []
    if forget_all:
        targets = [item for item in entries.values() if not item["live"]]
        if not targets:
            if as_json:
                print(json.dumps([]))
            else:
                print("No stopped hubs to forget.")
            return
    else:
        for selector in selectors:
            hub = select_hub(selector)
            entry = entries.get(hub.hub_id)
            if entry is None:
                raise DiscoveryError(
                    f"--hub {selector}: hub {hub.hub_id} is not registered; it is already forgotten"
                )
            if entry["live"]:
                raise RuntimeError(
                    f"hub {entry.get('name')} is live at {entry.get('url')}; "
                    "stop it with robomate down first"
                )
            targets.append(entry)
    outcomes: list[dict[str, Any]] = []
    for entry in targets:
        outcomes.append(_forget_one(entry, forget_all=forget_all, as_json=as_json))
    if as_json:
        print(json.dumps(outcomes))


def _forget_one(entry: dict[str, Any], *, forget_all: bool, as_json: bool) -> dict[str, Any]:
    """Forget one registry entry under its checkout's `_hub_lock` (r1-1).

    The lock serializes against a concurrent `up` in that checkout, which
    holds the same lock from startup through shutdown: either this wins and
    the later `up` sees the forget marker, or `up` wins and this fails
    instead of deleting its fresh entry. Liveness is re-checked after the
    lock is held, so a hub that started in between is refused, not removed.
    """

    hub_id = str(entry.get("hub_id"))
    name = entry.get("name") or "(unnamed)"
    if not entry.get("state_dir") and hub_checkout(entry) is None:
        raise RuntimeError(f"hub {name} names no state location; cannot forget it")
    entry_state = state_dir(entry)
    with ExitStack() as stack:
        try:
            stack.enter_context(_hub_lock(entry_state))
        except HubLockBusy as exc:
            # Lock contention (r2-1): a concurrent `up` holds the checkout
            # lock. Read fresh state to say whether that hub is live or still
            # starting. Under --all this is a skip, never an abort: the loop
            # continues and the JSON array is still printed.
            current = next(
                (item for item in hub_entries() if str(item.get("hub_id")) == hub_id), None
            )
            if current is not None and current["live"]:
                reason = (
                    f"hub {name} is live at {current.get('url')}; stop it with robomate down first"
                )
            else:
                reason = f"hub {name} is starting; wait for robomate up to finish and try again"
            if forget_all:
                if not as_json:
                    print(f"Skipping {reason}")
                return {"hub_id": hub_id, "name": name, "forgotten": False, "reason": reason}
            raise RuntimeError(reason) from exc
        fresh = {str(item.get("hub_id")): item for item in hub_entries()}
        current = fresh.get(hub_id)
        if current is None:
            if not as_json:
                print(f"Hub {name} is already forgotten.")
            return {"hub_id": hub_id, "name": name, "forgotten": False}
        if current["live"]:
            if forget_all:
                if not as_json:
                    print(f"Skipping live hub {name} at {current.get('url')}; stop it first")
                return {"hub_id": hub_id, "name": name, "forgotten": False}
            raise RuntimeError(
                f"hub {current.get('name')} is live at {current.get('url')}; "
                "stop it with robomate down first"
            )
        removed = forget_entry(hub_id)
        if removed is None:
            if not as_json:
                print(f"Hub {name} is already forgotten.")
            return {"hub_id": hub_id, "name": name, "forgotten": False}
        port = removed.get("port") or _entry_port(removed)
        state = str(removed.get("state_dir") or entry_state)
        # Release the saved port: record it as released so the next up takes
        # the first free port and says so. The last URL and port stay in
        # hub.json so `status` still reports the stopped hub; only pid and
        # started_at are cleared. Anything else — hub_id, name, checkout,
        # origin — stays, as does hub.db.
        stored = read_hub_json(entry_state)
        if stored is not None and stored.get("hub_id") == hub_id:
            stored["pid"] = None
            stored["started_at"] = None
            try:
                stored["released_port"] = int(port) if port is not None else None
            except (TypeError, ValueError):
                stored["released_port"] = None
            if stored.get("released_port") is None:
                stored.pop("released_port", None)
            write_hub_json(entry_state, stored)
        # A hub kept beside its worktrees still holds its name as the directory
        # <worktree_root>/<hub>/, which its state occupies (spec §4, operator
        # decision Q2 on #243): only a legacy hub's name is released.
        name_released = is_legacy_state_dir(Path(state))
        if not as_json:
            released = ["its name"] if name_released else []
            released += [f"port {port}"] if port else []
            print(
                f"Forgot hub {name} (hub_id {hub_id}): released "
                + (" and ".join(released) or "its registry entry")
                + f"; state kept at {state}"
                + (
                    ""
                    if name_released
                    else f"; the name stays taken by {Path(state).parent} until that state "
                    "is removed or relocated"
                )
            )
        return {
            "hub_id": hub_id,
            "name": name,
            "forgotten": True,
            "name_released": name_released,
            "port": port,
            "state_dir": state,
        }


def _age(elapsed: timedelta) -> str:
    seconds = max(0, int(elapsed.total_seconds()))
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m {seconds % 60}s"
    return f"{seconds // 3600}h {seconds % 3600 // 60}m"


STALL_NOTE = (
    "STALLED is an activity warning, not a lifecycle state: no substantive hub call, "
    "task progress or event consumption within the threshold, while the bridge may "
    "still heartbeat. A long non-hub command (tests, CI) can cause it; it does not "
    "prove a provider fault. 'lost' means the bridge heartbeat itself stopped."
)


def _seconds(value: object) -> timedelta | None:
    return timedelta(seconds=float(value)) if isinstance(value, int | float) else None


def _stall_evidence(activity: dict[str, Any]) -> str:
    """One line of evidence for a stalled agent: reasons, ages and IDs (#144)."""

    parts = []
    reasons = activity.get("reasons") or []
    silence = _seconds(activity.get("silence_s"))
    if "no_hub_call" in reasons and silence is not None:
        last = activity.get("last_call")
        parts.append(
            f"no hub call for {_age(silence)}" + (f" (last: {last})" if last else " (none seen)")
        )
    progress = _seconds(activity.get("progress_age_s"))
    if "no_task_progress" in reasons and progress is not None:
        parts.append(f"task {activity.get('task_id')} no progress for {_age(progress)}")
    if "event_backlog" in reasons:
        for event in activity.get("stale_events") or []:
            age = _seconds(event.get("age_s"))
            parts.append(
                f"event {event.get('id')} ({event.get('kind')}) queued"
                f" {_age(age) if age is not None else '?'} undelivered"
            )
    heartbeat = _seconds(activity.get("heartbeat_age_s"))
    parts.append(
        "no bridge heartbeat seen"
        if heartbeat is None
        else f"bridge heartbeat {_age(heartbeat)} ago"
    )
    threshold = _seconds(activity.get("threshold_s"))
    if threshold is not None:
        parts.append(f"threshold {_age(threshold)}")
    return "; ".join(parts)


_ORCHESTRATOR_STATE_LABELS = {
    "finished": "finished",
    "paused": "paused",
    "active": "active",
    "idle": "idle",
    "not_seen": "not seen by this hub",
}


def _orchestrator_lines(status: dict[str, Any]) -> list[str]:
    """The orchestrator's status lines: what she is doing apart from bridge liveness (#95).

    `last_seen` moves with every bridge heartbeat, so it says only that the
    bridge process is alive; a finished or idle Alice looked freshly seen.
    The activity's state and last call say what she is doing, and the bridge
    is reported beside them.
    """

    orchestrator = status["orchestrator"]
    activity = status.get("orchestrator_activity")
    if not isinstance(activity, dict) or "state" not in activity:
        # A hub older than #95. An age, not the hub's UTC timestamp, which
        # read as hours stale in other zones (#65).
        if not orchestrator:
            return ["Orchestrator: none"]
        seen = datetime.fromisoformat(orchestrator["last_seen"])
        return [
            f"Orchestrator: {orchestrator['name']}  session {orchestrator['session']}"
            f"  last seen {_age(datetime.now(UTC) - seen)} ago"
        ]
    name = (
        orchestrator["name"]
        if orchestrator
        else activity.get("last_call_actor") or activity.get("actor")
    )
    if not name:
        return ["Orchestrator: none"]
    state = activity["state"]
    hold = _seconds(activity.get("hold_age_s"))
    if state == "holding":
        label = f"waiting in {activity.get('holding')}" + (
            f" {_age(hold)}" if hold is not None else ""
        )
    else:
        label = _ORCHESTRATOR_STATE_LABELS.get(state, str(state))
    call_age = _seconds(activity.get("last_call_age_s"))
    if call_age is None:
        call = "no hub call seen"
    else:
        notes = [activity.get("last_call") or "?"]
        if activity.get("last_call_restored"):
            notes.append("before hub restart")
        call = f"last call {_age(call_age)} ago ({', '.join(notes)})"
    bridge = activity.get("bridge")
    bridge_age = _seconds(activity.get("bridge_age_s"))
    if bridge == "connected":
        context = ["bridge connected"]
    elif bridge == "disconnected":
        context = ["bridge disconnected"] + (
            [f"last heard {_age(bridge_age)} ago"] if bridge_age is not None else []
        )
    else:
        context = ["bridge not seen by this hub"]
    workflow = status.get("workflow")
    if workflow and workflow.get("status") != "active":
        context.append(f"workflow {workflow['status']}")
    open_tasks = len(status.get("tasks") or [])
    if state == "idle" and open_tasks:
        context.append(f"{open_tasks} open task{'s' if open_tasks != 1 else ''}")
    lines = [f"Orchestrator: {name}  {label}  {call}  ({', '.join(context)})"]
    session = orchestrator["session"] if orchestrator else None
    lines.append(f"  session {session or 'none seen by this hub process'}")
    return lines


def _stopped_at(value: str | None) -> str | None:
    """Normalize an operator-supplied stop time to UTC ISO 8601, or refuse it."""

    if value is None:
        return None
    try:
        moment = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        raise ValueError(
            f"--stopped-at {value!r} is not an ISO 8601 time, e.g. 2026-10-08T12:00:00Z"
        ) from None
    if moment.tzinfo is None:
        raise ValueError("--stopped-at needs a timezone, e.g. 2026-10-08T12:00:00Z")
    return moment.astimezone(UTC).isoformat().replace("+00:00", "Z")


def render_snapshot(snapshot: dict[str, Any]) -> str:
    """The before-snapshot block exactly as `resume-<agent>.prompt.md` expects it (#146)."""

    stopped = snapshot.get("stopped_at")
    workflow = snapshot.get("workflow")
    orchestrator = snapshot.get("orchestrator")
    lines = [
        "Operator before-snapshot",
        f"- Snapshot time: {snapshot['taken_at']} (when this was read, not a process stop time)",
        "- Old process stop time: "
        + (f"{stopped} (operator-supplied)" if stopped else "not supplied"),
        f"- Hub ID: {snapshot.get('hub_id') or 'unknown'}",
        "- Workflow: " + ("none" if not workflow else f"{workflow['id']} ({workflow['status']})"),
        "- Alice RPC session: "
        + (
            "none seen by this hub process"
            if not orchestrator
            else f"{orchestrator['session']} ({orchestrator['name']},"
            f" last seen {orchestrator['last_seen']})"
        ),
        f"- Open tasks: {len(snapshot['tasks'])}",
    ]
    for task in snapshot["tasks"]:
        lines.append(
            f"  - task {task['id']}: owner {task['assignee'] or '-'}, role {task['role']},"
            f" state {task['state']}"
        )
    lines.append(f"- Unacknowledged deliveries: {len(snapshot['deliveries'])}")
    for delivery in snapshot["deliveries"]:
        lines.append(
            f"  - event {delivery['event_id']} ({delivery['kind']}): delivery"
            f" {delivery['delivery_id']}, attempt {delivery['attempt']},"
            f" delivered_at {delivery['delivered_at']},"
            f" delivery_expires {delivery['delivery_expires']}"
        )
    lines.append(f"- Queued, never delivered events: {snapshot['queued_events']}")
    return "\n".join(lines)


def _snapshot(as_json: bool, stopped_at: str | None, selector: str | None) -> None:
    """Print the operator's before-snapshot from bearer-only reads (#146)."""

    stopped = _stopped_at(stopped_at)
    endpoint, _ = _connect(selector)
    snapshot = _rpc(endpoint.url, endpoint.token, "hub.snapshot") | {"stopped_at": stopped}
    if as_json:
        print(json.dumps(snapshot))
    else:
        print(render_snapshot(snapshot))


def _hub_location(
    local: LocalHub | None, checkout: Path | None, hub_id: object
) -> tuple[Path, Path] | None:
    """A hub's state directory and token file, as the resolver finds them.

    A hub selected by URL has no binding; its state is only known when this
    machine's registry or the checkout it reports holds it.
    """

    if local is not None:
        return local.state_dir, local.token_file
    for entry in recorded_entries():
        if hub_id and entry.get("hub_id") == hub_id:
            return state_dir(entry), token_file(entry)
    if checkout is not None:
        legacy = legacy_state_dir(checkout)
        if (legacy / "hub.json").is_file():
            return legacy, legacy / "token"
    return None


def _status(as_json: bool, selector: str | None) -> None:
    """Show the selected or discovered hub, retaining local metadata for stopped hubs."""

    escape_unencodable_output()

    hub = find_hub(Path.cwd(), selector=selector)
    local = hub if isinstance(hub, LocalHub) else None

    def stopped(url: str, port: object, reason: str | None = None) -> NoReturn:
        report: dict[str, Any] = {
            "running": False,
            "repo_root": str(local.checkout) if local else None,
            "url": url,
            "port": port,
        }
        if local is not None:
            report |= {
                "name": local.name,
                "checkout": str(local.checkout),
                "state_dir": str(local.state_dir),
                "token_file": str(local.token_file),
            }
        if reason is not None:
            report["reason"] = reason
        if as_json:
            print(json.dumps(report))
        else:
            who = "Hub"
            if local is not None:
                who = " ".join(filter(None, ("Hub", local.name, f"({local.checkout})")))
            print(f"{who}: {reason or 'not running'} (URL: {url}, port: {port})")
        raise SystemExit(1)

    if local is not None:
        url = local.url
        port = local.info.get("port")
        pid = int(local.info.get("pid") or 0)
        if not url:
            if (local.state_dir / "hub.json").is_file():
                # A startup that failed before serving left identity without
                # an address: report the stale partial record, not bare
                # "not running" (#301). A bound hub whose state directory
                # holds no hub.json yet is still starting: it reports bare
                # "not running" below, like `down` does.
                stopped(url, port, stale_partial_text(local))
            stopped(url, port)
        # A worker in another PID namespace cannot see the hub process. The
        # matching HTTP hub ID is the authoritative live check in that case.
        alive = process_alive(pid)
        health = check_healthz(url, HEALTHZ_LIVE_PID_TIMEOUT_S if alive else HEALTHZ_TIMEOUT_S)
        if health.hub_id != local.hub_id:
            if health.hub_id is not None:
                stopped(url, port, f"answers as hub {health.hub_id}, not {local.hub_id}")
            if alive:
                stopped(url, port, f"{health.failure}; recorded pid {pid} is visible")
            if health.timed_out:
                stopped(url, port, health.failure)
            stopped(url, port)
        endpoint = local.endpoint()
    else:
        assert isinstance(hub, HubEndpoint)
        endpoint = hub
        url = hub.url
        port = urllib.parse.urlparse(url).port
    try:
        status = _rpc(url, endpoint.token, "hub.status")
    except urllib.error.HTTPError as exc:
        raise RuntimeError(f"hub at {url} rejected status (HTTP {exc.code})") from exc
    except (OSError, urllib.error.URLError):
        stopped(url, port)
    reported = status.get("checkout") or status.get("repo_root")
    location = _hub_location(local, Path(reported) if reported else None, status.get("hub_id"))
    if as_json:
        # Operator tools take the hub's state from here (spec §4): prepare-run
        # reads hub.db and hub-report reads its state directory through it.
        print(
            json.dumps(
                status
                | {
                    "state_dir": str(location[0]) if location else None,
                    "token_file": str(location[1]) if location else None,
                }
            )
        )
        return
    # name, checkout and git_common_dir are absent from a hub older than #147.
    name = status.get("name") or (local.name if local else None)
    checkout = Path(status.get("checkout") or status["repo_root"])
    common = status.get("git_common_dir")
    print(f"Hub: {name or '(unnamed)'}  {status['url']}")
    print(f"Checkout: {checkout}")
    print(f"Repository: {repository_dir(Path(common)) if common else checkout}")
    print(f"State: {location[0] if location else 'unknown (hub selected by URL)'}")
    print(f"Origin: {status['origin']}")
    print(f"Forge: {status['forge']}  Default branch: {status['default_branch']}")
    workflow = status["workflow"]
    if workflow:
        run_id = workflow.get("run_id") or status.get("run_id") or "-"
        print(f"Workflow: {workflow['status']} — {workflow['headline'][:120]}")
        print(f"Run ID: {run_id}")
        if workflow.get("statement_sha256"):
            print(f"Statement: {workflow['statement_sha256']}")
    else:
        print("Workflow: none")
    # Queued runs (M2 Step 3): the sequential queue behind the live run.
    # Step 4 shows each queued statement and its SHA-256 (mvp-spec §7.1).
    queue_length = status.get("queue_length")
    queue = status.get("queue") or []
    if queue_length is None:
        queue_length = len(queue)
    print(f"Queue length: {queue_length}")
    for entry in queue:
        print(f"  queued {entry.get('run_id', '-')}: {entry.get('statement_sha256', '-')}")
    for line in _orchestrator_lines(status):
        print(line)
    # Absent from a hub older than #144, which may still be running.
    stalled = False
    alice = status.get("orchestrator_activity")
    if alice and alice.get("stalled"):
        stalled = True
        print(f"  STALLED — {_stall_evidence(alice)}")
    print(f"Agents: {len(status['agents'])}")
    for agent in status["agents"]:
        life = (
            "released" if agent["status"] == "released" else "alive" if agent["alive"] else "lost"
        )
        if agent.get("stalled"):
            stalled = True
            life = "STALLED"
        print(
            f"  {agent['name']}: {agent['harness']} / {agent['model']}  {life}"
            f"  task {agent['current_task'] or '-'}"
        )
        if agent.get("stalled"):
            print(f"    {_stall_evidence(agent['activity'])}")
    print(f"Open tasks: {len(status['tasks'])}")
    for task in status["tasks"]:
        print(
            f"  {task['id']}: {task['role']}  {task['assignee'] or '-'}  {task['state']}"
            f"  PR {task['pr_url'] or '-'}  head {task['head_sha'] or '-'}"
        )
    print(f"Pending questions: {status['pending_questions']}")
    # Absent from a hub older than #130, which may still be running.
    operator_questions = status.get("operator_questions")
    if operator_questions is not None:
        hint = "  (see robomate inbox)" if operator_questions else ""
        print(f"Operator questions: {operator_questions}{hint}")
    if stalled:
        print(STALL_NOTE)


def _printable(text: str) -> str:
    """Escape control characters in agent-written text before it reaches a terminal.

    Questions and options are untrusted (§5 rails); an escape sequence in one
    must not be able to rewrite what the operator sees.
    """

    return "".join(
        char
        if char in "\n\t" or not unicodedata.category(char).startswith("C")
        else char.encode("unicode_escape").decode("ascii")
        for char in text
    )


def _open_questions(url: str, token: str) -> list[dict[str, Any]]:
    questions = _rpc(url, token, "hub.questions").get("questions")
    if not isinstance(questions, list):
        raise RuntimeError("invalid hub.questions response")
    return questions


def _inbox(as_json: bool, selector: str | None) -> None:
    """List the questions the orchestrator is waiting on the operator to answer."""

    escape_unencodable_output()
    endpoint, _ = _connect(selector)
    questions = _open_questions(endpoint.url, endpoint.token)
    if as_json:
        print(json.dumps(questions))
        return
    if not questions:
        print("No open questions.")
        return
    now = datetime.now(UTC)
    for question in questions:
        asked = datetime.fromisoformat(question["asked"])
        print(
            f"[{question['question_id']}] asked {question['asked']}"
            f" ({_age(now - asked)} ago) by {_printable(question['actor'])}"
        )
        for line in _printable(question["question"]).splitlines() or [""]:
            print(f"    {line}")
        for number, option in enumerate(question["options"] or [], start=1):
            print(f"    --option {number}: {_printable(option)}")
    print('Answer with: robomate answer <id> "<text>"  or  robomate answer <id> --option N')


def _answer(question_id: int, text: str | None, option: int | None, selector: str | None) -> None:
    """Answer one open question with the operator credential (#130)."""

    if (text is None) == (option is None):
        raise ValueError("give either an answer text or --option N, exactly one")
    escape_unencodable_output()
    endpoint, _ = _connect(selector)
    operator_token = read_token_file(operator_token_path(), "operator token")
    if option is not None:
        question = next(
            (
                q
                for q in _open_questions(endpoint.url, endpoint.token)
                if q["question_id"] == question_id
            ),
            None,
        )
        if question is None:
            raise RuntimeError(f"question {question_id} is not open; see robomate inbox")
        options = question["options"] or []
        if not 1 <= option <= len(options):
            raise ValueError(
                f"question {question_id} has no option {option}"
                + (f"; choose 1 to {len(options)}" if options else "; answer with text")
            )
        text = options[option - 1]
    assert text is not None
    try:
        _rpc(
            endpoint.url,
            endpoint.token,
            "hub.answer",
            operator_token=operator_token,
            params={"question_id": question_id, "answer": text},
        )
    except urllib.error.HTTPError as exc:
        raise RuntimeError(f"hub at {endpoint.url} rejected the answer (HTTP {exc.code})") from exc
    print(f"Answered question {question_id}: {_printable(text)}")


def _parse_policy_override(spec: str) -> tuple[str, Any]:
    """Parse one `--policy k=v` override with TOML value typing (M2 Step 4).

    Values use TOML typing so `max_review_rounds=5` is int 5 and
    `allow_no_ci=true` is bool; a bare word TOML rejects (`auto`, `squash`)
    stays a string. Lists use TOML arrays (`k=["a","b"]`).
    """

    if "=" not in spec:
        raise ValueError(f"--policy must be k=v, got {spec!r}")
    key, _, raw = spec.partition("=")
    key = key.strip()
    raw = raw.strip()
    if not key or not raw:
        raise ValueError(f"--policy must be k=v with non-empty key and value, got {spec!r}")
    try:
        value: Any = tomllib.loads(f"x = {raw}")["x"]
    except tomllib.TOMLDecodeError:
        value = raw
    return key, value


def _read_submit_text(file: str | None, issue: int | None) -> tuple[str, str]:
    """Read the statement bytes verbatim, preserving CRLF and trailing newline.

    Returns (text, source) with source `file`, `issue` or `stdin`. File and
    stdin bytes must be UTF-8; `--issue` renders the standard one-issue
    statement (stored like any other). Empty statements are refused.
    """

    if file is not None and issue is not None:
        raise ValueError("--issue and a file are mutually exclusive; give one statement source")
    if issue is not None:
        if issue <= 0:
            raise ValueError("--issue must be a positive issue number")
        # Rendered after connecting (needs forge); placeholder replaced by caller.
        return "", "issue"
    if file is None or file == "-":
        try:
            data = sys.stdin.buffer.read()
        except OSError as exc:
            raise ValueError(f"cannot read statement from stdin: {exc}") from exc
        source = "stdin"
    else:
        path = Path(file)
        try:
            data = path.read_bytes()
        except FileNotFoundError as exc:
            raise ValueError(f"statement file {file} does not exist") from exc
        except OSError as exc:
            raise ValueError(f"statement file {file} is not readable: {exc}") from exc
        source = "file"
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError(f"statement must be a readable UTF-8 text file: {exc}") from exc
    if not text.strip():
        raise ValueError("statement must not be empty; write the statement of work first")
    return text, source


def _render_issue_statement(issue: int, origin: str, forge: str) -> str:
    """The standard one-issue statement, stored like any other (M2 Step 4)."""

    slug: str | None = None
    if forge == "gitlab":
        try:
            from agent_hub.gitlab_gate import GitLabProject

            slug = GitLabProject.from_origin(origin).path
        except Exception:
            slug = None
    else:
        slug = parse_github_slug(origin)
    target = f"{slug}#{issue}" if slug else f"#{issue}"
    change_term = "merge request" if forge == "gitlab" else "pull request"
    return f"Address issue `{target}`, merge its {change_term}, and close out."


def _submit(
    file: str | None, issue: int | None, policy: list[str] | None, selector: str | None
) -> None:
    """Submit a statement of work through operator-only `hub.submit` (M2 Step 4, #241)."""

    if file is not None and issue is not None:
        raise ValueError("--issue and a file are mutually exclusive; give one statement source")
    escape_unencodable_output()
    overrides: dict[str, Any] = {}
    for spec in policy or []:
        key, value = _parse_policy_override(spec)
        # Last wins for repeated keys; dotted keys nest in the hub.
        overrides[key] = value
    endpoint, _ = _connect(selector)
    operator_token = read_token_file(operator_token_path(), "operator token")
    # For `--issue`, the statement needs the hub's forge/origin for the slug
    # and change term; read them from `hub.status` before rendering.
    if issue is not None and file is None:
        try:
            status = _rpc(endpoint.url, endpoint.token, "hub.status")
        except urllib.error.HTTPError as exc:
            raise RuntimeError(f"hub at {endpoint.url} rejected status (HTTP {exc.code})") from exc
        origin = str(status.get("origin") or "")
        forge = str(status.get("forge") or "github")
        text = _render_issue_statement(issue, origin, forge)
        source = "issue"
    else:
        text, source = _read_submit_text(file, None)
    data = text.encode("utf-8")
    if len(data) > MAX_STATEMENT_BYTES:
        raise ValueError(
            f"statement is {len(data)} bytes; maximum is {MAX_STATEMENT_BYTES} bytes "
            "so it fits in one 16 KiB message part with its envelope. Shorten the "
            "statement, or put the detail in an issue and submit that instead."
        )
    try:
        result = _rpc(
            endpoint.url,
            endpoint.token,
            "hub.submit",
            operator_token=operator_token,
            params={"statement": text, "policy": overrides or {}, "source": source},
            timeout=_SUBMIT_RPC_TIMEOUT_S,
        )
    except urllib.error.HTTPError as exc:
        raise RuntimeError(f"hub at {endpoint.url} rejected submit (HTTP {exc.code})") from exc
    sha = result.get("statement_sha256") or hashlib.sha256(data).hexdigest()
    print(
        f"Submitted {source} statement {sha} as {result.get('status', 'queued')}"
        f" run {result.get('run_id', '-')}"
    )


def _relocate(selector: str | None) -> None:
    """Move a stopped hub's state and token beside its agent worktrees (spec §4 Migration).

    Operator-only (spec §12): it needs the operator credential. It moves state
    and token together, rewrites the hub's bindings and registry entry, and
    leaves a pointer file where the state was. The same hub_id serves from the
    new location at the next `robomate up`.
    """

    escape_unencodable_output()
    read_token_file(operator_token_path(), "operator token")
    hub = find_hub(Path.cwd(), selector=selector)
    if not isinstance(hub, LocalHub):
        raise DiscoveryError(
            "robomate relocate moves a hub on this machine; select it with --hub, "
            "not with ROBOMATE_HUB_URL"
        )
    # A hub from before checkout scope (#147) does not record its repository.
    common_raw = hub.info.get("git_common_dir")
    common = Path(str(common_raw)) if common_raw else resolve_checkout(hub.checkout)[1]
    root = load_config_layers(git_common_dir=common).worktree_root
    assert root is not None
    name, _, _ = relocation_targets(hub, root)
    if not hub.state_dir.is_dir():
        raise DiscoveryError(f"{hub.label()}: its state {hub.state_dir} is missing")
    pid = int(hub.info.get("pid") or 0)
    if (
        pid
        and process_alive(pid)
        and check_healthz(hub.url, HEALTHZ_LIVE_PID_TIMEOUT_S).hub_id == hub.hub_id
    ):
        raise RuntimeError(
            f"{hub.label()} is running at {hub.url}; stop it with robomate down first"
        )
    operator = operator_token_path().resolve()
    if operator.is_relative_to((root / name).resolve()):
        raise RuntimeError(
            f"the operator token file must not be inside the hub's namespace ({root / name})"
        )
    try:
        with _hub_lock(hub.state_dir):
            result = relocate_hub(hub, common, root, repository_config_path(common))
    except HubLockBusy:
        raise RuntimeError(
            f"{hub.label()} is running or starting; stop it with robomate down first"
        ) from None
    # The lock was open while the state moved, so it is removed once released.
    (result.old_state_dir / "up.lock").unlink(missing_ok=True)
    print(f"Relocated hub {result.name} (hub_id {result.hub_id})")
    print(f"State: {result.state_dir}")
    print(f"Token: {result.token_file}")
    print(f"Bindings rewritten: {len(result.bindings)}")
    print(f"Start it with robomate up in {hub.checkout}.")


def _ls(as_json: bool) -> None:
    """List every registered hub, live or stopped, with name, checkout and repository."""

    hubs = []
    for entry in hub_entries():
        checkout = hub_checkout(entry)
        assert checkout is not None  # hub_entries prunes entries without one
        common = entry.get("git_common_dir")
        url = str(entry.get("url") or "")
        agent_count, phase = None, None
        if entry["live"]:
            try:
                token = token_file(entry).read_text(encoding="utf-8").strip()
                status = _rpc(url, token, "hub.status")
                agent_count = len(status["agents"])
                phase = status["workflow"]["status"] if status["workflow"] else "none"
            except (OSError, urllib.error.URLError, RuntimeError, KeyError):
                phase = "unknown"
        hubs.append(
            {
                "hub_id": entry.get("hub_id"),
                "name": entry.get("name"),
                "live": entry["live"],
                "checkout": str(checkout),
                "repo_root": str(checkout),
                "repository": str(repository_dir(Path(common)) if common else checkout),
                "git_common_dir": common,
                "state_dir": str(state_dir(entry)),
                "token_file": str(token_file(entry)),
                "url": url,
                "agent_count": agent_count,
                "workflow_status": phase,
            }
        )
    if as_json:
        print(json.dumps(hubs))
    else:
        for hub in hubs:
            count = hub["agent_count"] if hub["agent_count"] is not None else "-"
            print(
                f"{hub['name'] or '(unnamed)'}  {'live' if hub['live'] else 'stopped'}"
                f"  {hub['url']}  checkout {hub['checkout']}  repository {hub['repository']}"
                f"  agents {count}  workflow {hub['workflow_status'] or '-'}"
            )


async def _mcp(args: argparse.Namespace, stdout: Any) -> None:
    if args.role in (None, "worker"):
        await run_worker_bridge(stdout, name=args.name, harness=args.harness)
    else:
        bridge = OrchestratorBridge(args.name or "alice")
        try:
            server = create_orchestrator_mcp(bridge)
            await serve_mcp(server, stdout, await bridge.accounting(server))
        finally:
            await bridge.close()


def main() -> None:
    parser = argparse.ArgumentParser(prog="robomate")
    commands = parser.add_subparsers(dest="command", required=True)
    up = commands.add_parser("up", help="run this checkout's hub in the foreground")
    up.add_argument(
        "--name",
        help="the hub's name at its first up (default: from the checkout's directory name)",
    )
    up.add_argument(
        "--forge",
        choices=("github", "gitlab"),
        help="override forge detection and record the choice in hub.json",
    )
    up.add_argument("--bind", default="127.0.0.1")
    up.add_argument("--public-url")
    up.add_argument("--port", type=int)
    up.add_argument("--no-call-accounting", action="store_true")
    down = commands.add_parser("down", help="stop this checkout's hub, or the one --hub names")
    down.add_argument(
        "--all",
        action="store_true",
        help="stop every live hub in the machine registry",
    )
    down.add_argument("--json", action="store_true", help="with --all: report as JSON")
    status = commands.add_parser("status", help="show this checkout's hub, or the one --hub names")
    status.add_argument("--json", action="store_true")
    status.add_argument(
        "--snapshot",
        action="store_true",
        help="print the operator before-snapshot a manual resume prompt asks for",
    )
    status.add_argument(
        "--stopped-at",
        metavar="UTC_TIME",
        help="with --snapshot: when the old harness process was seen to stop",
    )
    inbox = commands.add_parser("inbox", help="list the questions waiting for you")
    inbox.add_argument("--json", action="store_true")
    answer = commands.add_parser("answer", help="answer a question from robomate inbox")
    answer.add_argument("question_id", type=int, metavar="id")
    answer.add_argument("text", nargs="?", help="the answer; quote it")
    answer.add_argument("--option", type=int, metavar="N", help="answer with option N")
    submit = commands.add_parser("submit", help="submit a statement of work to this checkout's hub")
    sources = submit.add_mutually_exclusive_group()
    sources.add_argument(
        "file",
        nargs="?",
        metavar="FILE",
        help="statement file (use - for stdin); omit with --issue N",
    )
    sources.add_argument(
        "--issue", type=int, metavar="N", help="render the standard one-issue statement"
    )
    submit.add_argument(
        "--policy",
        action="append",
        default=[],
        metavar="k=v",
        help=(
            "override a policy default for this run (repeatable; TOML values,"
            " e.g. max_review_rounds=5, allow_no_ci=auto)"
        ),
    )
    relocate = commands.add_parser(
        "relocate",
        help="move a stopped hub's state and token out of its checkout, beside its worktrees",
    )
    for command in (down, status, inbox, answer, submit, relocate):
        command.add_argument(
            "--hub",
            metavar="NAME|HUB_ID|CHECKOUT",
            help="act on this hub instead of the one this checkout owns",
        )
    listing = commands.add_parser("ls", help="list the hubs on this machine")
    listing.add_argument("--json", action="store_true")
    forget = commands.add_parser(
        "forget",
        help="forget stopped hubs: release their names and ports, keeping their state",
    )
    forget.add_argument(
        "hubs",
        nargs="*",
        metavar="HUB",
        help="hub name, hub_id or checkout path; without --all, defaults to this checkout's hub",
    )
    forget.add_argument(
        "--all",
        action="store_true",
        help="forget every stopped hub in the machine registry",
    )
    forget.add_argument("--json", action="store_true")
    mcp = commands.add_parser("mcp", help="serve an agent's MCP tools over stdio")
    mcp.add_argument("--role", choices=("orchestrator", "worker"), default=None)
    mcp.add_argument("--name")
    mcp.add_argument("--harness")
    args = parser.parse_args()
    try:
        if args.command == "up":
            asyncio.run(_up(args))
        elif args.command == "down":
            if args.all:
                if args.hub:
                    raise ValueError("give either --hub or --all, not both")
                _down_all(args.json)
            else:
                if args.json:
                    raise ValueError("--json goes with --all")
                _down(args.hub)
        elif args.command == "status":
            if args.stopped_at is not None and not args.snapshot:
                raise ValueError("--stopped-at goes with --snapshot")
            if args.snapshot:
                _snapshot(args.json, args.stopped_at, args.hub)
            else:
                _status(args.json, args.hub)
        elif args.command == "inbox":
            _inbox(args.json, args.hub)
        elif args.command == "answer":
            _answer(args.question_id, args.text, args.option, args.hub)
        elif args.command == "submit":
            _submit(args.file, args.issue, args.policy, args.hub)
        elif args.command == "relocate":
            _relocate(args.hub)
        elif args.command == "ls":
            _ls(args.json)
        elif args.command == "forget":
            _forget(args.hubs, args.all, args.json)
        else:
            logging.basicConfig(level=logging.INFO, stream=sys.stderr)
            with reserve_stdout() as protocol_stdout:
                anyio.run(_mcp, args, protocol_stdout)
    except (DiscoveryError, RuntimeError, ValueError, OSError) as exc:
        print(f"robomate: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc


if __name__ == "__main__":
    main()
