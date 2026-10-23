#!/usr/bin/env python3
"""Inside the eval sandbox: relay an MCP stdio session to the services broker.

The ``uv`` shim runs this in place of ``uv run ... robomate mcp``. The first
line sent is the request (the uv arguments, the robomate environment and the
working directory); after that, bytes are copied both ways unchanged.
"""

from __future__ import annotations

import json
import os
import selectors
import socket
import sys

ENV_KEYS = ("ROBOMATE_HUB_URL", "ROBOMATE_TOKEN_FILE", "AGENT_NAME", "PYTHONUTF8")


def main(args: list[str]) -> int:
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.connect(os.environ["EVAL_MCP_SOCKET"])
    env = {k: v for k, v in os.environ.items() if k in ENV_KEYS or k.startswith("HUB_")}
    request = {"args": args, "env": env, "cwd": os.getcwd()}
    sock.sendall(json.dumps(request).encode() + b"\n")
    selector = selectors.DefaultSelector()
    selector.register(sock, selectors.EVENT_READ)
    selector.register(sys.stdin.fileno(), selectors.EVENT_READ)
    while True:
        for key, _ in selector.select():
            if key.fileobj is sock:
                data = sock.recv(65536)
                if not data:
                    return 0
                os.write(sys.stdout.fileno(), data)
            else:
                data = os.read(sys.stdin.fileno(), 65536)
                if not data:
                    selector.unregister(key.fileobj)
                    sock.shutdown(socket.SHUT_WR)
                else:
                    sock.sendall(data)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
