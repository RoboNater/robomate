"""Start and stop the per-repository hub."""

from __future__ import annotations

import argparse
import asyncio
import errno
import json
import logging
import os
import socket
import sys
import urllib.error
import urllib.parse
import urllib.request
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import anyio
from agent_hub.forge_preflight import gitlab_preflight
from agent_hub.main import serve_http
from agent_hub_common import HubSettings, load_or_create_token, reserve_stdout
from agent_hub_common.discovery import (
    DiscoveryError,
    Repository,
    discover,
    ensure_excluded,
    read_hub_json,
    repo_root,
    resolve_repository,
    state_dir,
    write_hub_json,
)
from agent_hub_common.registry import deregister, hub_healthy, live_entries, process_alive, register
from worker_mcp.main import run_worker_bridge, serve_mcp
from worker_mcp.orchestrator import OrchestratorBridge, create_orchestrator_mcp

VERSION = "0.1.0"


def _bind(host: str, port: int | None) -> tuple[socket.socket, int]:
    family = socket.AF_INET6 if ":" in host else socket.AF_INET
    ports = [port] if port is not None else range(8420, 65536)
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


def _rpc(url: str, token: str, method: str) -> dict[str, Any]:
    request = urllib.request.Request(
        f"{url.rstrip('/')}/rpc",
        data=json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": {}}).encode(),
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
    )
    with urllib.request.urlopen(request, timeout=2) as response:
        result: dict[str, Any] = json.load(response)
    if "error" in result:
        raise RuntimeError(str(result["error"]))
    value = result["result"]
    if not isinstance(value, dict):
        raise RuntimeError("invalid hub RPC response")
    return value


@contextmanager
def _repo_lock(directory: Path) -> Iterator[None]:
    """Hold an OS file lock for the hub lifetime, including startup."""

    fd = os.open(directory / "up.lock", os.O_RDWR | os.O_CREAT, 0o600)
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
            info = read_hub_json(directory.parent) or {}
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
    directory = state_dir(repo.root)
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(directory, 0o700)
    with _repo_lock(directory):
        await _run_up(args, repo, directory)


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
    requested_port = args.port if args.port is not None else old.get("port")
    sock, port = _bind(args.bind, int(requested_port) if requested_port is not None else None)
    overlay = dict(os.environ)
    overlay.update(
        {
            "HUB_STATE_DIR": str(directory),
            "HUB_HOST": args.bind,
            "HUB_PORT": str(port),
            "HUB_DB_PATH": str(directory / "hub.db"),
            "HUB_TOKEN_FILE": str(directory / "token"),
            "HUB_CALL_ACCOUNTING": "0" if args.no_call_accounting else "1",
        }
    )
    overlay.pop("HUB_TOKEN", None)
    if args.public_url:
        overlay["HUB_PUBLIC_URL"] = args.public_url
    try:
        settings = HubSettings.from_env(overlay)
        load_or_create_token(settings.token, settings.token_file)
        info: dict[str, Any] = {
            "repo_root": str(repo.root),
            "origin": repo.origin,
            "forge": repo.forge,
            "default_branch": repo.default_branch,
            "url": settings.public_url,
            "port": port,
            "pid": os.getpid(),
            "started_at": datetime.now(UTC).isoformat(),
            "robomate_version": VERSION,
            "hub_id": old.get("hub_id") or uuid.uuid4().hex,
        }

        def started() -> None:
            write_hub_json(repo.root, info)
            register(info)
            print(f"Hub running at {settings.public_url}", flush=True)
            print(f"ROBOMATE_HUB_URL={settings.public_url}", flush=True)
            print(f"ROBOMATE_TOKEN_FILE={settings.token_file}", flush=True)

        try:
            await serve_http(settings, [sock], info, started)
        finally:
            # The shutdown write can hit a transient Windows sharing
            # violation while a reader holds hub.json (#114). It must never
            # skip deregister, so the registry cleanup runs even if the
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
                deregister(str(info["hub_id"]), pid=os.getpid())
    finally:
        sock.close()


def _down() -> None:
    endpoint = discover(Path.cwd())
    try:
        result = _rpc(endpoint.url, endpoint.token, "hub.shutdown")
    except urllib.error.HTTPError as exc:
        raise RuntimeError(f"hub at {endpoint.url} rejected shutdown (HTTP {exc.code})") from exc
    except (OSError, urllib.error.URLError) as exc:
        pid = 0
        if not os.environ.get("ROBOMATE_HUB_URL"):
            try:
                repo = resolve_repository(Path.cwd())
                info = read_hub_json(repo.root) or {}
                pid = int(info.get("pid") or 0)
            except DiscoveryError:
                pass
        if process_alive(pid):
            raise RuntimeError(
                f"hub at {endpoint.url} is unreachable; pid {pid} is still alive"
            ) from exc
        raise RuntimeError(f"hub at {endpoint.url} is not running") from exc
    if not result.get("stopping"):
        raise RuntimeError("hub did not acknowledge shutdown")
    print(f"Stopping hub at {endpoint.url}")


def _age(elapsed: timedelta) -> str:
    seconds = max(0, int(elapsed.total_seconds()))
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m {seconds % 60}s"
    return f"{seconds // 3600}h {seconds % 3600 // 60}m"


def _status(as_json: bool) -> None:
    """Show the discovered hub, retaining local metadata for stopped hubs."""
    try:
        root, _ = repo_root(Path.cwd())
    except DiscoveryError:
        root = None
    info = read_hub_json(root) if root is not None else None
    explicit = bool(os.environ.get("ROBOMATE_HUB_URL", "").strip())

    def stopped(url: str, port: object, reason: str | None = None) -> None:
        known_root = root if not explicit else None
        stopped = {
            "running": False,
            "repo_root": str(known_root) if known_root else None,
            "url": url,
            "port": port,
        }
        if reason is not None:
            stopped["reason"] = reason
        if as_json:
            print(json.dumps(stopped))
        elif reason is not None:
            print(f"{known_root or 'Hub'}: {reason} (URL: {url}, port: {port})")
        else:
            print(f"{known_root or 'Hub'}: not running (URL: {url}, port: {port})")
        raise SystemExit(1)

    if info is not None and not explicit:
        url = str(info.get("url") or "unknown")
        port = info.get("port")
        pid = int(info.get("pid") or 0)
        # A worker in another PID namespace cannot see the hub process. The
        # matching HTTP hub ID is the authoritative live check in that case.
        if not hub_healthy(url, str(info.get("hub_id") or "")):
            if process_alive(pid):
                stopped(url, port, f"unreachable; recorded pid {pid} is visible")
            stopped(url, port)
        assert root is not None
        token = (state_dir(root) / "token").read_text(encoding="utf-8").strip()
    else:
        try:
            endpoint = discover(Path.cwd())
        except DiscoveryError as exc:
            if info is None and root is not None and not explicit:
                raise RuntimeError(f"not running: no hub recorded for {root}") from exc
            raise
        url, token = endpoint.url, endpoint.token
        port = urllib.parse.urlparse(url).port
    try:
        status = _rpc(url, token, "hub.status")
    except urllib.error.HTTPError as exc:
        raise RuntimeError(f"hub at {url} rejected status (HTTP {exc.code})") from exc
    except (OSError, urllib.error.URLError):
        stopped(url, port)
    if as_json:
        print(json.dumps(status))
        return
    print(f"Repository: {status['repo_root']}")
    print(f"Origin: {status['origin']}")
    print(f"Forge: {status['forge']}  Default branch: {status['default_branch']}")
    print(f"Hub: {status['url']}")
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
    print(f"Agents: {len(status['agents'])}")
    for agent in status["agents"]:
        life = (
            "released" if agent["status"] == "released" else "alive" if agent["alive"] else "lost"
        )
        print(
            f"  {agent['name']}: {agent['harness']} / {agent['model']}  {life}"
            f"  task {agent['current_task'] or '-'}"
        )
    print(f"Open tasks: {len(status['tasks'])}")
    for task in status["tasks"]:
        print(
            f"  {task['id']}: {task['role']}  {task['assignee'] or '-'}  {task['state']}"
            f"  PR {task['pr_url'] or '-'}  head {task['head_sha'] or '-'}"
        )
    print(f"Pending questions: {status['pending_questions']}")


def _ls(as_json: bool) -> None:
    hubs = []
    for entry in live_entries():
        repo = Path(str(entry["repo_root"]))
        url = str(entry["url"])
        try:
            token = (state_dir(repo) / "token").read_text(encoding="utf-8").strip()
            status = _rpc(url, token, "hub.status")
            agent_count = len(status["agents"])
            phase = status["workflow"]["status"] if status["workflow"] else "none"
        except (OSError, urllib.error.URLError, RuntimeError, KeyError):
            agent_count, phase = None, "unknown"
        hubs.append(
            {
                "repo_root": str(repo),
                "url": url,
                "agent_count": agent_count,
                "workflow_status": phase,
            }
        )
    if as_json:
        print(json.dumps(hubs))
    else:
        for hub in hubs:
            count = hub["agent_count"] if hub["agent_count"] is not None else "?"
            print(
                f"{hub['repo_root']}  {hub['url']}  agents {count}"
                f"  workflow {hub['workflow_status']}"
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
    up = commands.add_parser("up", help="run this repository's hub in the foreground")
    up.add_argument(
        "--forge",
        choices=("github", "gitlab"),
        help="override forge detection and record the choice in hub.json",
    )
    up.add_argument("--bind", default="127.0.0.1")
    up.add_argument("--public-url")
    up.add_argument("--port", type=int)
    up.add_argument("--no-call-accounting", action="store_true")
    commands.add_parser("down", help="stop the discovered hub")
    status = commands.add_parser("status", help="show this repository's hub state")
    status.add_argument("--json", action="store_true")
    listing = commands.add_parser("ls", help="list live hubs on this machine")
    listing.add_argument("--json", action="store_true")
    mcp = commands.add_parser("mcp", help="serve an agent's MCP tools over stdio")
    mcp.add_argument("--role", choices=("orchestrator", "worker"), required=True)
    mcp.add_argument("--name")
    mcp.add_argument("--harness")
    args = parser.parse_args()
    try:
        if args.command == "up":
            asyncio.run(_up(args))
        elif args.command == "down":
            _down()
        elif args.command == "status":
            _status(args.json)
        elif args.command == "ls":
            _ls(args.json)
        else:
            logging.basicConfig(level=logging.INFO, stream=sys.stderr)
            with reserve_stdout() as protocol_stdout:
                anyio.run(_mcp, args, protocol_stdout)
    except (DiscoveryError, RuntimeError, ValueError, OSError) as exc:
        print(f"robomate: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc
