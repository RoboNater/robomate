"""Start and stop a checkout's hub, and answer its operator questions."""

from __future__ import annotations

import argparse
import asyncio
import errno
import io
import json
import logging
import os
import socket
import sys
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, NoReturn

import anyio
from agent_hub.forge_preflight import gitlab_preflight
from agent_hub.main import serve_http
from agent_hub_common import HubSettings, load_or_create_token, read_token_file, reserve_stdout
from agent_hub_common.discovery import (
    DiscoveryError,
    HubEndpoint,
    LocalHub,
    Repository,
    derive_hub_name,
    ensure_excluded,
    find_hub,
    hub_checkout,
    is_linked_worktree,
    local_hub,
    read_hub_json,
    repository_dir,
    resolve_checkout,
    resolve_repository,
    select_hub,
    state_dir,
    token_file,
    validate_hub_name,
    verify_local_hub,
    write_hub_json,
)
from agent_hub_common.registry import (
    RegistryError,
    hub_entries,
    hub_healthy,
    live_entries,
    mark_stopped,
    operator_token_path,
    process_alive,
    register,
)
from agent_hub_common.registry import (
    forget as forget_entry,
)
from worker_mcp.main import run_worker_bridge, serve_mcp
from worker_mcp.orchestrator import OrchestratorBridge, create_orchestrator_mcp

VERSION = "0.1.0"


def _bind(
    host: str, port: int | None, avoid: frozenset[int] = frozenset()
) -> tuple[socket.socket, int]:
    """Bind the pinned or saved port, or the first free one from 8420 not in `avoid`."""

    family = socket.AF_INET6 if ":" in host else socket.AF_INET
    ports = [port] if port is not None else (p for p in range(8420, 65536) if p not in avoid)
    for candidate in ports:
        assert candidate is not None
        sock = socket.socket(family, socket.SOCK_STREAM)
        if os.name != "nt":
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind((host, candidate))
            sock.listen()
            return sock, candidate
        except OSError as exc:
            sock.close()
            if port is not None or exc.errno not in (errno.EADDRINUSE, 10048):
                raise RuntimeError(
                    f"port {candidate} is unavailable; choose --port: {exc}"
                ) from exc
    raise RuntimeError("no free port from 8420 upward")


def _rpc(
    url: str,
    token: str,
    method: str,
    *,
    operator_token: str | None = None,
    params: dict[str, Any] | None = None,
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
    with urllib.request.urlopen(request, timeout=2) as response:
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


@contextmanager
def _hub_lock(checkout: Path) -> Iterator[None]:
    """Hold this hub's OS file lock for its lifetime, including startup.

    The lock is per hub, in its state directory: it never blocks a sibling hub.
    """

    fd = os.open(state_dir(checkout) / "up.lock", os.O_RDWR | os.O_CREAT, 0o600)
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
            info = read_hub_json(checkout) or {}
            url = info.get("url")
            message = f"hub already running at {url}" if url else "hub already starting"
            raise RuntimeError(message) from exc
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
    repo = resolve_repository(Path.cwd(), probe_cli=args.forge is None)
    if args.forge:
        repo = replace(repo, forge=args.forge)
    if args.name is not None:
        validate_hub_name(args.name)
    directory = state_dir(repo.root)
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(directory, 0o700)
    with _hub_lock(repo.root):
        await _run_up(args, repo, directory)


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


async def _run_up(args: argparse.Namespace, repo: Repository, directory: Path) -> None:
    ensure_excluded(repo.git_common_dir)
    old = read_hub_json(repo.root) or {}
    old_pid = int(old.get("pid") or 0)
    if (
        old_pid
        and process_alive(old_pid)
        and hub_healthy(str(old.get("url") or ""), str(old.get("hub_id") or ""))
    ):
        raise RuntimeError(f"hub already running at {old['url']}")
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
        if released_port is None:
            raise RuntimeError(
                f"this hub is named {recorded!r}; a hub's name is fixed once recorded, "
                "because agent worktrees and branches carry it"
            )
        # A forgotten hub released its registry name: an explicit --name may
        # rename it at its next up, since the old name may since have been
        # taken by a sibling hub.
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
    entry = identity | {"state_dir": str(directory)}
    if is_linked_worktree(repo.root, repo.git_common_dir):
        print(
            f"Warning: this hub's state is in {directory}, inside a linked worktree; "
            "`git worktree remove` deletes it, with the hub's history and run reports, "
            "until hub state moves out of the checkout (M2).",
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
    # Record the identity, then claim the name among the repository's hubs
    # before anything starts. The registry prunes an entry without hub.json,
    # so the file comes first, and is restored if the name is refused.
    # `released_port` was read above from the forget marker in hub.json: while
    # it is set the startup entry claims no port, so the released port is
    # reusable by a sibling hub at once.
    write_hub_json(repo.root, old | identity)
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
            write_hub_json(repo.root, old)
        else:
            (directory / "hub.json").unlink(missing_ok=True)
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
            "HUB_TOKEN_FILE": str(token_file(repo.root)),
            "HUB_CALL_ACCOUNTING": "0" if args.no_call_accounting else "1",
        }
    )
    overlay.pop("HUB_TOKEN", None)
    if args.public_url:
        overlay["HUB_PUBLIC_URL"] = args.public_url
    try:
        settings = HubSettings.from_env(overlay)
        load_or_create_token(settings.token, settings.token_file)
        _provision_operator_token(settings, repo)
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
            write_hub_json(repo.root, info)
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
                current = read_hub_json(repo.root)
                if (
                    current
                    and current.get("hub_id") == info["hub_id"]
                    and current.get("pid") == os.getpid()
                ):
                    current["pid"] = None
                    current["started_at"] = None
                    write_hub_json(repo.root, current)
            finally:
                mark_stopped(hub_id, pid=os.getpid())
    finally:
        sock.close()


def _provision_operator_token(settings: HubSettings, repo: Repository) -> None:
    """Create the operator credential beside the machine registry (#128).

    Agents read the repository, its checkouts and the hub's state, so a path
    into any of them is refused rather than handing them the one credential
    they must not hold. The token and its path are never printed.
    """

    path = settings.operator_token_file
    if path is None:
        return
    resolved = path.resolve()
    for place in (repo.root, state_dir(repo.root), repository_dir(repo.git_common_dir)):
        if resolved.is_relative_to(place.resolve()):
            raise RuntimeError(
                f"the operator token file must not be inside the repository or the "
                f"hub's state ({place})"
            )
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
        if process_alive(pid):
            raise RuntimeError(
                f"{label} at {endpoint.url} is unreachable; pid {pid} is still alive"
            ) from exc
        raise RuntimeError(f"{label} at {endpoint.url} is not running") from exc
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
        checkout = hub_checkout(entry)
        try:
            if checkout is None:
                raise RuntimeError("registry entry names no checkout")
            token = token_file(checkout).read_text(encoding="utf-8").strip()
            result = _rpc(url, token, "hub.shutdown", operator_token=operator_token)
            if not result.get("stopping"):
                raise RuntimeError("hub did not acknowledge shutdown")
            outcomes.append({"hub_id": hub_id, "name": name, "url": url, "stopped": True})
            if not as_json:
                print(f"Stopping hub {name} at {url}")
        except (OSError, urllib.error.URLError, urllib.error.HTTPError, RuntimeError) as exc:
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

    Hub state, including run reports, is kept: only the machine registry
    entry goes. A forgotten hub's next `up` takes the first free port and
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
        hub_id = str(entry.get("hub_id"))
        name = entry.get("name") or "(unnamed)"
        entry_checkout = hub_checkout(entry)
        removed = forget_entry(hub_id)
        if removed is None:
            outcomes.append({"hub_id": hub_id, "name": name, "forgotten": False})
            if not as_json:
                print(f"Hub {name} is already forgotten.")
            continue
        port = removed.get("port") or _entry_port(removed)
        state = str(
            removed.get("state_dir") or (state_dir(entry_checkout) if entry_checkout else "?")
        )
        # Release the saved port: record it as released so the next up takes
        # the first free port and says so. The last URL and port stay in
        # hub.json so `status` still reports the stopped hub; only pid and
        # started_at are cleared. Anything else — hub_id, name, checkout,
        # origin — stays, as does hub.db.
        if entry_checkout is not None:
            current = read_hub_json(entry_checkout)
            if current is not None and current.get("hub_id") == hub_id:
                current["pid"] = None
                current["started_at"] = None
                try:
                    current["released_port"] = int(port) if port is not None else None
                except (TypeError, ValueError):
                    current["released_port"] = None
                if current.get("released_port") is None:
                    current.pop("released_port", None)
                write_hub_json(entry_checkout, current)
        outcomes.append(
            {"hub_id": hub_id, "name": name, "forgotten": True, "port": port, "state_dir": state}
        )
        if not as_json:
            print(
                f"Forgot hub {name} (hub_id {hub_id}): released its name"
                + (f" and port {port}" if port else "")
                + f"; state kept at {state}"
            )
    if as_json:
        print(json.dumps(outcomes))


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


def _state_location(local: LocalHub | None, checkout: Path) -> str:
    if local is not None:
        return str(state_dir(local.checkout))
    # Selected by URL: the state is only known when that checkout is on this machine.
    state = state_dir(checkout)
    return str(state) if (state / "hub.json").is_file() else "unknown (hub selected by URL)"


def _status(as_json: bool, selector: str | None) -> None:
    """Show the selected or discovered hub, retaining local metadata for stopped hubs."""

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
                "state_dir": str(state_dir(local.checkout)),
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
        # A worker in another PID namespace cannot see the hub process. The
        # matching HTTP hub ID is the authoritative live check in that case.
        if not hub_healthy(url, local.hub_id):
            if process_alive(pid):
                stopped(url, port, f"unreachable; recorded pid {pid} is visible")
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
    if as_json:
        print(json.dumps(status))
        return
    # name, checkout and git_common_dir are absent from a hub older than #147.
    name = status.get("name") or (local.name if local else None)
    checkout = Path(status.get("checkout") or status["repo_root"])
    common = status.get("git_common_dir")
    print(f"Hub: {name or '(unnamed)'}  {status['url']}")
    print(f"Checkout: {checkout}")
    print(f"Repository: {repository_dir(Path(common)) if common else checkout}")
    print(f"State: {_state_location(local, checkout)}")
    print(f"Origin: {status['origin']}")
    print(f"Forge: {status['forge']}  Default branch: {status['default_branch']}")
    workflow = status["workflow"]
    if workflow:
        print(f"Workflow: {workflow['status']} — {workflow['headline'][:120]}")
    else:
        print("Workflow: none")
    orchestrator = status["orchestrator"]
    if orchestrator:
        # An age, not the hub's UTC timestamp, which read as hours stale in
        # other zones (#65). Alice is only seen per call, so gaps of about two
        # minutes while she holds wait_for_event are normal.
        seen = datetime.fromisoformat(orchestrator["last_seen"])
        print(
            f"Orchestrator: {orchestrator['name']}  session {orchestrator['session']}"
            f"  last seen {_age(datetime.now(UTC) - seen)} ago"
        )
    else:
        print("Orchestrator: none")
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


def _escape_unencodable_output() -> None:
    # Agent text can hold characters a Windows code page cannot encode once
    # stdout is redirected; escape them rather than fail mid-listing.
    if isinstance(sys.stdout, io.TextIOWrapper):
        sys.stdout.reconfigure(errors="backslashreplace")


def _open_questions(url: str, token: str) -> list[dict[str, Any]]:
    questions = _rpc(url, token, "hub.questions").get("questions")
    if not isinstance(questions, list):
        raise RuntimeError("invalid hub.questions response")
    return questions


def _inbox(as_json: bool, selector: str | None) -> None:
    """List the questions the orchestrator is waiting on the operator to answer."""

    _escape_unencodable_output()
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
    _escape_unencodable_output()
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
                token = token_file(checkout).read_text(encoding="utf-8").strip()
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
                "state_dir": str(entry.get("state_dir") or state_dir(checkout)),
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
    if args.role == "worker":
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
    for command in (down, status, inbox, answer):
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
    mcp.add_argument("--role", choices=("orchestrator", "worker"), required=True)
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
