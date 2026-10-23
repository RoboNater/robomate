#!/usr/bin/env python3
"""Inside the eval sandbox: forward loopback TCP ports to Unix sockets, then run a command.

    forward.py 3128=/run/jail/sock/egress.sock -- claude -p ...

The sandbox has its own network namespace with only loopback, so the egress
proxy is reached on 127.0.0.1:3128 and relayed to the services socket bound in
from outside. Exits with the command's status; standard library only.
"""

from __future__ import annotations

import asyncio
import contextlib
import signal
import sys


async def pipe(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    try:
        while data := await reader.read(65536):
            writer.write(data)
            await writer.drain()
    except (ConnectionError, OSError):
        pass
    finally:
        with contextlib.suppress(OSError, RuntimeError):
            if writer.can_write_eof():
                writer.write_eof()


def handler(socket_path: str):  # type: ignore[no-untyped-def]
    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            up_reader, up_writer = await asyncio.open_unix_connection(socket_path)
        except OSError:
            writer.close()
            return
        await asyncio.gather(pipe(reader, up_writer), pipe(up_reader, writer))
        for stream in (writer, up_writer):
            stream.close()

    return handle


async def main(argv: list[str]) -> int:
    split = argv.index("--")
    forwards, command = argv[:split], argv[split + 1 :]
    for spec in forwards:
        port, _, socket_path = spec.partition("=")
        await asyncio.start_server(handler(socket_path), "127.0.0.1", int(port))
    process = await asyncio.create_subprocess_exec(*command)
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
        loop.add_signal_handler(sig, process.send_signal, sig)
    return await process.wait()


if __name__ == "__main__":
    sys.exit(asyncio.run(main(sys.argv[1:])))
