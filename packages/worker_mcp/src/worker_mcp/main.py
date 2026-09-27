"""Console entry point for worker-mcp."""

from __future__ import annotations

import asyncio
import logging
import sys
from contextlib import suppress
from io import TextIOWrapper
from pathlib import Path
from typing import TextIO

import anyio
from agent_hub_common import reserve_stdout
from agent_hub_common.discovery import discover
from mcp.server.fastmcp import FastMCP
from mcp.server.stdio import stdio_server

from .client import WorkerHubClient
from .config import WorkerSettings
from .tools import create_worker_mcp


async def serve_mcp(server: FastMCP, stdout: TextIO) -> None:
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
