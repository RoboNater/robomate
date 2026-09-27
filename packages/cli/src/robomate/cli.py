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
import urllib.request
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import anyio
from agent_hub.main import serve_http
from agent_hub_common import HubSettings, load_or_create_token, reserve_stdout
from agent_hub_common.discovery import (
    DiscoveryError,
    Repository,
    discover,
    ensure_excluded,
    read_hub_json,
    resolve_repository,
    state_dir,
    write_hub_json,
)
from agent_hub_common.registry import deregister, hub_healthy, process_alive, register
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
            if os.name == "nt":
                import msvcrt

                if os.fstat(fd).st_size == 0:
                    os.write(fd, b"0")
                os.lseek(fd, 0, os.SEEK_SET)
                msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)  # type: ignore[attr-defined]
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
                if os.name == "nt":
                    os.lseek(fd, 0, os.SEEK_SET)
                    msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)  # type: ignore[attr-defined]
                else:
                    fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)


async def _up(args: argparse.Namespace) -> None:
    repo = resolve_repository(Path.cwd())
    directory = state_dir(repo.root)
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(directory, 0o700)
    with _repo_lock(directory):
        await _run_up(args, repo, directory)


async def _run_up(args: argparse.Namespace, repo: Repository, directory: Path) -> None:
    ensure_excluded(repo.git_common_dir)
    old = read_hub_json(repo.root) or {}
    old_pid = int(old.get("pid") or 0)
    if old_pid and process_alive(old_pid) and hub_healthy(
        str(old.get("url") or ""), str(old.get("hub_id") or "")
    ):
        raise RuntimeError(f"hub already running at {old['url']}")
    requested_port = args.port if args.port is not None else old.get("port")
    sock, port = _bind(args.bind, int(requested_port) if requested_port is not None else None)
    overlay = dict(os.environ)
    overlay.update({
        "HUB_STATE_DIR": str(directory), "HUB_HOST": args.bind, "HUB_PORT": str(port),
        "HUB_DB_PATH": str(directory / "hub.db"),
        "HUB_TOKEN_FILE": str(directory / "token"),
        "HUB_CALL_ACCOUNTING": "0" if args.no_call_accounting else "1",
    })
    overlay.pop("HUB_TOKEN", None)
    if args.public_url:
        overlay["HUB_PUBLIC_URL"] = args.public_url
    try:
        settings = HubSettings.from_env(overlay)
        load_or_create_token(settings.token, settings.token_file)
        info: dict[str, Any] = {
            "repo_root": str(repo.root), "origin": repo.origin, "forge": repo.forge,
            "default_branch": repo.default_branch, "url": settings.public_url, "port": port,
            "pid": os.getpid(), "started_at": datetime.now(UTC).isoformat(),
            "robomate_version": VERSION, "hub_id": old.get("hub_id") or uuid.uuid4().hex,
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
            current = read_hub_json(repo.root)
            if (
                current and current.get("hub_id") == info["hub_id"]
                and current.get("pid") == os.getpid()
            ):
                current["pid"] = None
                current["started_at"] = None
                write_hub_json(repo.root, current)
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


async def _mcp(args: argparse.Namespace, stdout: Any) -> None:
    if args.role == "worker":
        await run_worker_bridge(stdout, name=args.name, harness=args.harness)
    else:
        bridge = OrchestratorBridge(args.name or "alice")
        try:
            await serve_mcp(create_orchestrator_mcp(bridge), stdout)
        finally:
            await bridge.close()


def main() -> None:
    parser = argparse.ArgumentParser(prog="robomate")
    commands = parser.add_subparsers(dest="command", required=True)
    up = commands.add_parser("up", help="run this repository's hub in the foreground")
    up.add_argument("--bind", default="127.0.0.1")
    up.add_argument("--public-url")
    up.add_argument("--port", type=int)
    up.add_argument("--no-call-accounting", action="store_true")
    commands.add_parser("down", help="stop the discovered hub")
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
        else:
            logging.basicConfig(level=logging.INFO, stream=sys.stderr)
            with reserve_stdout() as protocol_stdout:
                anyio.run(_mcp, args, protocol_stdout)
    except (DiscoveryError, RuntimeError, ValueError, OSError) as exc:
        print(f"robomate: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc
