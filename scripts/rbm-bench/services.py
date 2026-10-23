#!/usr/bin/env python3
"""Per-run services for sandboxed eval agents: an egress proxy and an MCP broker.

Agents run inside `jail.py`'s bubblewrap sandbox with no network of their own.
This process, outside the sandbox, listens on two Unix sockets in
``<run>/jail/sock/``:

- ``egress.sock``: an HTTP CONNECT proxy. Only allowlisted ``host:port``
  destinations are tunnelled; every request, allowed or denied, is logged to
  ``<run>/jail/egress.jsonl``. Plain (non-CONNECT) HTTP is refused.
- ``mcp.sock``: the robomate MCP broker. The sandbox's ``uv`` shim connects
  here instead of running ``robomate mcp`` itself, so the robomate tooling
  checkout (current code, newer than the case under test) is never visible
  inside the sandbox. The broker spawns only ``robomate mcp --role
  worker|orchestrator`` from the tooling checkout, with the run's hub URL and
  token file, and relays bytes both ways.

    uv run --locked python scripts/rbm-bench/services.py --run-dir RUN \\
        --hub-url http://127.0.0.1:8421 --token-file RUN/hub.robomate/x/token/token
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import os
import signal
import sys
from datetime import UTC, datetime
from pathlib import Path

TOOLING = Path(__file__).resolve().parents[2]

#: Destinations an eval agent may reach. ``*.example.com`` matches subdomains only.
DEFAULT_ALLOW = (
    # Anthropic (Claude Code API, OAuth refresh, feature flags).
    "api.anthropic.com:443",
    "console.anthropic.com:443",
    "platform.claude.com:443",
    "statsig.anthropic.com:443",
    # OpenAI (Codex with API key or ChatGPT login).
    "api.openai.com:443",
    "auth.openai.com:443",
    "chatgpt.com:443",
    # OpenCode Zen/Go and its model catalogue.
    "opencode.ai:443",
    "models.dev:443",
    "models.opencode.ai:443",
    # Python packages for `uv sync` in the clones.
    "pypi.org:443",
    "files.pythonhosted.org:443",
    # The eval forge.
    "gitlab-box.local:443",
    "gitlab-box.local:22",
)

MCP_ROLES = ("worker", "orchestrator")
MCP_ENV_KEYS = frozenset({"ROBOMATE_HUB_URL", "ROBOMATE_TOKEN_FILE", "AGENT_NAME", "PYTHONUTF8"})
MCP_ENV_PREFIXES = ("HUB_",)
CHUNK = 64 * 1024


def now() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds")


def allowed(host: str, port: int, allow: tuple[str, ...]) -> bool:
    host = host.lower().rstrip(".")
    for entry in allow:
        pattern, _, entry_port = entry.rpartition(":")
        if int(entry_port) != port:
            continue
        if pattern.startswith("*."):
            if host.endswith(pattern[1:]):
                return True
        elif host == pattern:
            return True
    return False


async def pipe(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    try:
        while data := await reader.read(CHUNK):
            writer.write(data)
            await writer.drain()
    except (ConnectionError, OSError):
        pass
    finally:
        with contextlib.suppress(OSError, RuntimeError):
            if writer.can_write_eof():
                writer.write_eof()


async def splice(
    a: tuple[asyncio.StreamReader, asyncio.StreamWriter],
    b: tuple[asyncio.StreamReader, asyncio.StreamWriter],
) -> None:
    await asyncio.gather(pipe(a[0], b[1]), pipe(b[0], a[1]))
    for _, writer in (a, b):
        writer.close()
        with contextlib.suppress(OSError, ConnectionError):
            await writer.wait_closed()


class Services:
    def __init__(
        self,
        run_dir: Path,
        hub_url: str,
        token_file: Path,
        allow: tuple[str, ...],
        tooling: Path,
    ) -> None:
        self.jail = run_dir / "jail"
        self.sock = self.jail / "sock"
        self.hub_url = hub_url.rstrip("/")
        self.token_file = token_file
        self.allow = allow
        self.tooling = tooling
        self.log_path = self.jail / "egress.jsonl"
        self.mcp_logs = self.jail / "mcp-logs"
        self.mcp_count = 0

    def log(self, **fields: object) -> None:
        with self.log_path.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps({"time": now(), **fields}, sort_keys=True) + "\n")

    # -- egress proxy ------------------------------------------------------------

    async def egress(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 30)
        except (asyncio.IncompleteReadError, asyncio.LimitOverrunError, TimeoutError):
            writer.close()
            return
        request = head.split(b"\r\n", 1)[0].decode("latin-1")
        method, _, rest = request.partition(" ")
        target = rest.rsplit(" ", 1)[0]
        if method.upper() != "CONNECT":
            self.log(decision="deny", method=method, target=target[:300], reason="not CONNECT")
            writer.write(b"HTTP/1.1 403 Forbidden\r\nContent-Length: 0\r\n\r\n")
            await writer.drain()
            writer.close()
            return
        host, _, port_text = target.rpartition(":")
        host = host.strip("[]")
        try:
            port = int(port_text)
        except ValueError:
            port = -1
        if not allowed(host, port, self.allow):
            self.log(decision="deny", method="CONNECT", host=host, port=port)
            writer.write(b"HTTP/1.1 403 Forbidden\r\nContent-Length: 0\r\n\r\n")
            await writer.drain()
            writer.close()
            return
        try:
            upstream = await asyncio.wait_for(asyncio.open_connection(host, port), 30)
        except (OSError, TimeoutError) as exc:
            self.log(decision="error", host=host, port=port, error=str(exc)[:200])
            writer.write(b"HTTP/1.1 502 Bad Gateway\r\nContent-Length: 0\r\n\r\n")
            await writer.drain()
            writer.close()
            return
        self.log(decision="allow", host=host, port=port)
        writer.write(b"HTTP/1.1 200 Connection Established\r\n\r\n")
        await writer.drain()
        await splice((reader, writer), upstream)

    # -- MCP broker --------------------------------------------------------------

    def mcp_command(self, request: dict[str, object]) -> tuple[list[str], dict[str, str], str]:
        args = request.get("args")
        expected_prefix = ["run", "--locked", "--project", str(self.tooling), "robomate", "mcp"]
        if (
            not isinstance(args, list)
            or args[:6] != expected_prefix
            or len(args) != 8
            or args[6] != "--role"
            or args[7] not in MCP_ROLES
        ):
            raise ValueError(f"refused MCP command {args!r}")
        env_in = request.get("env")
        if not isinstance(env_in, dict):
            raise ValueError("MCP request has no env")
        env = {
            key: str(value)
            for key, value in env_in.items()
            if key in MCP_ENV_KEYS or key.startswith(MCP_ENV_PREFIXES)
        }
        if env.get("ROBOMATE_HUB_URL", "").rstrip("/") != self.hub_url:
            raise ValueError(f"MCP hub URL {env.get('ROBOMATE_HUB_URL')!r} is not this run's")
        if env.get("ROBOMATE_TOKEN_FILE") != str(self.token_file):
            raise ValueError("MCP token file is not this run's")
        cwd = request.get("cwd")
        if not isinstance(cwd, str) or not Path(cwd).is_dir():
            cwd = str(self.jail)
        base = {
            key: value
            for key, value in os.environ.items()
            if key in ("PATH", "HOME", "LANG", "LC_ALL", "TMPDIR", "USER", "LOGNAME", "TERM")
            or key.startswith(("XDG_", "UV_"))
        }
        return ["uv", *args], {**base, **env}, cwd

    async def mcp(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        self.mcp_count += 1
        number = self.mcp_count
        try:
            line = await asyncio.wait_for(reader.readline(), 30)
            command, env, cwd = self.mcp_command(json.loads(line))
        except (ValueError, TimeoutError, asyncio.IncompleteReadError) as exc:
            self.log(kind="mcp", decision="deny", error=str(exc)[:300])
            writer.close()
            return
        agent = env.get("AGENT_NAME", "alice")
        log_file = self.mcp_logs / f"{number:03d}-{agent}.log"
        self.log(kind="mcp", decision="spawn", agent=agent, role=command[-1], log=str(log_file))
        with log_file.open("ab") as stderr:
            process = await asyncio.create_subprocess_exec(
                *command,
                cwd=cwd,
                env=env,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=stderr,
            )
        assert process.stdin is not None and process.stdout is not None

        async def to_child() -> None:
            await pipe(reader, process.stdin)  # type: ignore[arg-type]
            with contextlib.suppress(OSError):
                process.stdin.close()  # type: ignore[union-attr]

        await asyncio.gather(to_child(), pipe(process.stdout, writer))
        with contextlib.suppress(ProcessLookupError):
            process.terminate()
        try:
            code = await asyncio.wait_for(process.wait(), 10)
        except TimeoutError:
            process.kill()
            code = await process.wait()
        self.log(kind="mcp", decision="exit", agent=agent, code=code)
        writer.close()

    async def serve(self) -> None:
        self.sock.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.mcp_logs.mkdir(parents=True, exist_ok=True, mode=0o700)
        servers = []
        for name, handler in (("egress.sock", self.egress), ("mcp.sock", self.mcp)):
            path = self.sock / name
            path.unlink(missing_ok=True)
            servers.append(await asyncio.start_unix_server(handler, path=str(path)))
        (self.jail / "services.pid").write_text(f"{os.getpid()}\n", encoding="utf-8")
        self.log(kind="services", decision="start", allow=list(self.allow), pid=os.getpid())
        print(f"eval services listening in {self.sock}", file=sys.stderr, flush=True)
        stop = asyncio.Event()
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, stop.set)
        await stop.wait()
        for server in servers:
            server.close()
        (self.jail / "services.pid").unlink(missing_ok=True)
        self.log(kind="services", decision="stop")


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument("--hub-url", required=True)
    parser.add_argument("--token-file", type=Path, required=True)
    parser.add_argument(
        "--allow",
        action="append",
        default=[],
        metavar="HOST:PORT",
        help="extra allowed destination (repeatable; *.domain matches subdomains)",
    )
    parser.add_argument("--tooling", type=Path, default=TOOLING)
    args = parser.parse_args(argv)
    services = Services(
        args.run_dir.resolve(),
        args.hub_url,
        args.token_file.resolve(),
        (*DEFAULT_ALLOW, *args.allow),
        args.tooling.resolve(),
    )
    asyncio.run(services.serve())


if __name__ == "__main__":
    main()
