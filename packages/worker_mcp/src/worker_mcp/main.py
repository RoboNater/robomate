"""Console entry point for worker-mcp."""

from __future__ import annotations

import asyncio
import json
import logging
import sys
from collections.abc import AsyncIterator
from contextlib import suppress
from io import TextIOWrapper
from pathlib import Path
from typing import TextIO

import anyio
from agent_hub_common import reserve_stdout
from agent_hub_common.discovery import discover
from mcp.server.fastmcp import FastMCP
from mcp.server.stdio import stdio_server

from .accounting import McpAccounting
from .client import WorkerHubClient
from .config import WorkerSettings
from .tools import create_worker_mcp


class _ObservedInput(anyio.AsyncFile[str]):
    def __init__(self, source: anyio.AsyncFile[str], observer: McpAccounting) -> None:
        self.source = source
        self.observer = observer

    async def __aiter__(self) -> AsyncIterator[str]:
        async for line in self.source:
            try:
                payload = json.loads(line)
            except ValueError:
                payload = None
            if isinstance(payload, dict):
                self.observer.observe_request(payload, len(line.encode("utf-8")))
            yield line


class _ObservedOutput(anyio.AsyncFile[str]):
    def __init__(self, source: anyio.AsyncFile[str], observer: McpAccounting) -> None:
        self.source = source
        self.observer = observer

    async def write(self, value: str) -> int:
        written = await self.source.write(value)
        try:
            payload = json.loads(value)
        except ValueError:
            payload = None
        if isinstance(payload, dict):
            self.observer.observe_response(payload, len(value.encode("utf-8")))
        return written

    async def flush(self) -> None:
        await self.source.flush()


async def serve_mcp(
    server: FastMCP, stdout: TextIO, accounting: McpAccounting | None = None
) -> None:
    """Run one MCP server with stdout reserved for protocol framing."""

    buffer = getattr(stdout, "buffer", None)
    stdout_stream = (
        anyio.wrap_file(TextIOWrapper(buffer, encoding="utf-8"))
        if buffer is not None
        else anyio.wrap_file(stdout)
    )
    stdin_buffer = getattr(sys.stdin, "buffer", None)
    stdin_stream = (
        anyio.wrap_file(TextIOWrapper(stdin_buffer, encoding="utf-8", errors="replace"))
        if stdin_buffer is not None
        else anyio.wrap_file(sys.stdin)
    )
    if accounting is not None:
        stdin_stream = _ObservedInput(stdin_stream, accounting)
        stdout_stream = _ObservedOutput(stdout_stream, accounting)
    async with stdio_server(stdin=stdin_stream, stdout=stdout_stream) as (read, write):
        await server._mcp_server.run(
            read, write, server._mcp_server.create_initialization_options()
        )


async def run_worker(stdout: TextIO) -> None:
    settings = WorkerSettings.from_env()
    async with WorkerHubClient(settings) as client:
        await serve_mcp(create_worker_mcp(client), stdout)


async def run_worker_bridge(
    stdout: TextIO, *, name: str | None = None, harness: str | None = None
) -> None:
    """Serve worker tools; discover the hub only when a tool is first called."""

    client: WorkerHubClient | None = None
    init_lock = asyncio.Lock()

    async def get_client() -> WorkerHubClient:
        nonlocal client
        async with init_lock:
            if client is None:
                endpoint = discover(Path.cwd())
                settings = WorkerSettings.from_discovered(
                    endpoint, agent_name=name, harness=harness
                )
                created = WorkerHubClient(settings)
                await created.__aenter__()
                client = created
        return client

    try:
        await serve_mcp(create_worker_mcp(get_client, server_name="robomate"), stdout)
    finally:
        if client is not None:
            await client.close()


def main() -> None:
    # Ensure stdout is reserved exclusively for MCP framing; all logging and print goes to stderr.
    logging.basicConfig(
        level=logging.INFO,
        stream=sys.stderr,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    with reserve_stdout() as protocol_stdout, suppress(KeyboardInterrupt):
        anyio.run(run_worker, protocol_stdout)


if __name__ == "__main__":
    main()
