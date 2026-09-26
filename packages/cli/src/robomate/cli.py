"""Start and stop the per-repository hub."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import socket
import sys
import urllib.error
import urllib.request
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from agent_hub.main import serve_http
from agent_hub_common import HubSettings, load_or_create_token
from agent_hub_common.discovery import (
    DiscoveryError,
    discover,
    ensure_excluded,
    read_hub_json,
    resolve_repository,
    state_dir,
    write_hub_json,
)
from agent_hub_common.registry import deregister, hub_healthy, process_alive, register

VERSION = "0.1.0"


def _bind(host: str, port: int | None) -> tuple[socket.socket, int]:
    family = socket.AF_INET6 if ":" in host else socket.AF_INET
    ports = [port] if port is not None else range(8420, 65536)
    for candidate in ports:
        assert candidate is not None
        sock = socket.socket(family, socket.SOCK_STREAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind((host, candidate))
            sock.listen()
            return sock, candidate
        except OSError as exc:
            sock.close()
            if port is not None or exc.errno not in (98, 10048):
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


async def _up(args: argparse.Namespace) -> None:
    repo = resolve_repository(Path.cwd())
    directory = state_dir(repo.root)
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(directory, 0o700)
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
    except (OSError, urllib.error.URLError) as exc:
        try:
            repo = resolve_repository(Path.cwd())
            info = read_hub_json(repo.root) or {}
            pid = int(info.get("pid") or 0)
        except DiscoveryError:
            pid = 0
        if process_alive(pid):
            raise RuntimeError(
                f"hub at {endpoint.url} is unreachable; pid {pid} is still alive"
            ) from exc
        raise RuntimeError(f"hub at {endpoint.url} is not running") from exc
    print(f"Stopping hub at {endpoint.url}: {result['stopping']}")


def main() -> None:
    parser = argparse.ArgumentParser(prog="robomate")
    commands = parser.add_subparsers(dest="command", required=True)
    up = commands.add_parser("up", help="run this repository's hub in the foreground")
    up.add_argument("--bind", default="127.0.0.1")
    up.add_argument("--public-url")
    up.add_argument("--port", type=int)
    up.add_argument("--no-call-accounting", action="store_true")
    commands.add_parser("down", help="stop the discovered hub")
    args = parser.parse_args()
    try:
        if args.command == "up":
            asyncio.run(_up(args))
        else:
            _down()
    except (DiscoveryError, RuntimeError, ValueError, OSError) as exc:
        print(f"robomate: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc
