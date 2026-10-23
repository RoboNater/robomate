#!/usr/bin/env python3
"""SSH ProxyCommand inside the eval sandbox: tunnel host:port through the egress proxy.

ssh -o ProxyCommand='python3 connect.py %h %p' git@gitlab-box.local
"""

from __future__ import annotations

import os
import selectors
import socket
import sys


def main(host: str, port: str) -> int:
    proxy = os.environ.get("EVAL_PROXY_PORT", "3128")
    sock = socket.create_connection(("127.0.0.1", int(proxy)))
    sock.sendall(f"CONNECT {host}:{port} HTTP/1.1\r\nHost: {host}:{port}\r\n\r\n".encode())
    head = b""
    while b"\r\n\r\n" not in head:
        chunk = sock.recv(4096)
        if not chunk:
            break
        head += chunk
    status = head.split(b"\r\n", 1)[0]
    if b" 200 " not in status + b" ":
        sys.stderr.write(f"egress proxy refused {host}:{port}: {status.decode('latin-1')}\n")
        return 1
    rest = head.split(b"\r\n\r\n", 1)[1]
    out = sys.stdout.buffer
    if rest:
        out.write(rest)
        out.flush()
    selector = selectors.DefaultSelector()
    selector.register(sock, selectors.EVENT_READ)
    selector.register(sys.stdin.fileno(), selectors.EVENT_READ)
    while True:
        for key, _ in selector.select():
            if key.fileobj is sock:
                data = sock.recv(65536)
                if not data:
                    return 0
                out.write(data)
                out.flush()
            else:
                data = os.read(sys.stdin.fileno(), 65536)
                if not data:
                    selector.unregister(key.fileobj)
                    sock.shutdown(socket.SHUT_WR)
                else:
                    sock.sendall(data)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1], sys.argv[2]))
