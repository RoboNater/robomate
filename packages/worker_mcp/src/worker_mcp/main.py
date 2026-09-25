"""Console entry point for worker-mcp."""

from __future__ import annotations

import logging
import sys
from contextlib import suppress
from io import TextIOWrapper
from typing import TextIO

import anyio
from agent_hub_common import reserve_stdout
from mcp.server.stdio import stdio_server

from .client import WorkerHubClient
from .config import WorkerSettings
from .tools import create_worker_mcp


async def run_worker(stdout: TextIO) -> None:
    settings = WorkerSettings.from_env()
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
    async with WorkerHubClient(settings) as client:
        server = create_worker_mcp(client)
        async with stdio_server(stdin=stdin_stream, stdout=stdout_stream) as (read, write):
            await server._mcp_server.run(
                read, write, server._mcp_server.create_initialization_options()
            )


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
