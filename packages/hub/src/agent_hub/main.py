"""Console entry point for the hub service."""

import asyncio
import logging
import os
import signal
import sys
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from copy import deepcopy
from typing import TextIO

import uvicorn
from agent_hub_common import HubSettings
from agent_hub_common import reserve_stdout as reserve_stdout

from .app import create_app
from .mcp import run_mcp

MCP_SHUTDOWN_TIMEOUT_S = 1.0


class HubServer(uvicorn.Server):
    """Handle signals without Uvicorn re-raising them before MCP cleanup."""

    @contextmanager
    def capture_signals(self) -> Iterator[None]:
        if threading.current_thread() is not threading.main_thread():
            yield
            return

        def stop(signum: int, frame: object) -> None:
            if self.should_exit and signum == signal.SIGINT:
                self.force_exit = True
            self.should_exit = True

        previous = {sig: signal.signal(sig, stop) for sig in (signal.SIGINT, signal.SIGTERM)}
        try:
            yield
        finally:
            for sig, handler in previous.items():
                signal.signal(sig, handler)

    async def main_loop(self) -> None:
        try:
            await super().main_loop()
        except BaseException:
            # Uvicorn does not run shutdown when its main loop raises.
            await self.shutdown()
            raise


async def run_hub(settings: HubSettings, stdout: TextIO) -> bool:
    """Own both transports; either one's exit shuts down its sibling."""
    app = create_app(settings)
    log_config = deepcopy(uvicorn.config.LOGGING_CONFIG)
    for handler in log_config["handlers"].values():
        handler["stream"] = "ext://sys.stderr"
    server = HubServer(
        uvicorn.Config(
            app,
            host=settings.host,
            port=settings.port,
            log_config=log_config,
            access_log=False,
            timeout_graceful_shutdown=2,
            # uvicorn otherwise rewrites the peer from X-Forwarded-For when
            # the connection is from 127.0.0.1, which would let any loopback
            # worker choose the address the hub records for it (#126).
            proxy_headers=False,
        )
    )
    http = asyncio.create_task(server.serve())
    mcp: asyncio.Task[bool] | None = None
    uninitialized_eof = False
    http_failure: BaseException | None = None
    try:
        while not server.started:
            if http.done():
                await http
                raise RuntimeError("HTTP server stopped before startup")
            await asyncio.sleep(0.01)
        mcp = asyncio.create_task(run_mcp(app.state.store, stdout, app.state.accounting))
        done, _ = await asyncio.wait((http, mcp), return_when=asyncio.FIRST_COMPLETED)
        for task in done:
            await task
        if mcp in done:
            if mcp.result():
                logging.getLogger(__name__).info("MCP client disconnected; shutting down HTTP")
            else:
                uninitialized_eof = True
                logging.getLogger(__name__).error(
                    "MCP stdin closed before initialization; shutting down HTTP. "
                    "Detached launches with stdin at EOF cannot serve."
                )
    finally:
        server.should_exit = True
        http_result = (await asyncio.gather(http, return_exceptions=True))[0]
        if isinstance(http_result, BaseException):
            http_failure = http_result
        if mcp is not None:
            mcp.cancel()
            _, pending = await asyncio.wait({mcp}, timeout=MCP_SHUTDOWN_TIMEOUT_S)
            if pending:
                logging.getLogger(__name__).critical(
                    "MCP transport did not stop; forcing process exit"
                )
                os._exit(1)
    if http_failure is not None:
        raise http_failure
    return not uninitialized_eof


def main() -> None:
    with reserve_stdout() as stdout:
        logging.basicConfig(level=logging.INFO, stream=sys.stderr)
        if not asyncio.run(run_hub(HubSettings.from_env(), stdout)):
            raise SystemExit(1)


if __name__ == "__main__":
    main()
