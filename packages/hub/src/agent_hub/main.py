"""Console entry point for the hub service."""

import asyncio
import logging
import signal
import socket
import threading
import time
import uuid
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from copy import deepcopy
from typing import Any

import uvicorn
from agent_hub_common import HubSettings
from agent_hub_common.stdio import escape_unencodable_output

from .app import create_app

# How long `robomate up` waits for the child hub to bind and run startup
# before it fails fast instead of hanging (#301). The parent already holds
# the checkout lock and has written hub.json, so a silent child would leave
# stale state that `down` must then report.
_START_TIMEOUT_S = 30.0


async def serve_http(
    settings: HubSettings,
    sockets: list[socket.socket],
    hub_info: Mapping[str, Any],
    on_started: Callable[[], None],
) -> None:
    """Serve the HTTP-only hub."""

    log_config = deepcopy(uvicorn.config.LOGGING_CONFIG)
    for handler in log_config["handlers"].values():
        handler["stream"] = "ext://sys.stderr"
    server: HubServer
    app = create_app(
        settings, hub_info=hub_info, shutdown=lambda: setattr(server, "should_exit", True)
    )
    server = HubServer(
        uvicorn.Config(
            app,
            host=settings.host,
            port=settings.port,
            log_config=log_config,
            access_log=False,
            timeout_graceful_shutdown=2,
            proxy_headers=False,
        )
    )
    serving = asyncio.create_task(server.serve(sockets=sockets))
    try:
        deadline = time.monotonic() + _START_TIMEOUT_S
        while not server.started:
            if serving.done():
                await serving
                raise RuntimeError("HTTP server stopped before startup")
            if time.monotonic() >= deadline:
                serving.cancel()
                await asyncio.gather(serving, return_exceptions=True)
                raise RuntimeError(
                    f"hub did not start serving within {_START_TIMEOUT_S:g} s; "
                    "check stderr, hub.json, the database, the guides directory, "
                    "and whether the port is already bound (#301)"
                )
            await asyncio.sleep(0.01)
        on_started()
        await serving
    finally:
        server.should_exit = True
        await asyncio.gather(serving, return_exceptions=True)


class HubServer(uvicorn.Server):
    """Handle signals while allowing application cleanup."""

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


def main() -> None:
    escape_unencodable_output()
    logging.basicConfig(level=logging.INFO)
    settings = HubSettings.from_env()
    with socket.socket(socket.AF_INET6 if ":" in settings.host else socket.AF_INET) as sock:
        sock.bind((settings.host, settings.port))
        sock.listen()
        asyncio.run(serve_http(settings, [sock], {"hub_id": uuid.uuid4().hex}, lambda: None))


if __name__ == "__main__":
    main()
