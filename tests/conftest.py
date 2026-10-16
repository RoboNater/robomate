"""Fixtures shared by the hub's HTTP and state tests."""

import json
import random
import re
import socket
import subprocess
import sys
import threading
from collections.abc import AsyncIterator, Callable, Iterator
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from time import monotonic
from typing import Any, cast
from uuid import uuid4

import httpx
import pytest
import pytest_asyncio
from agent_hub import create_app
from agent_hub.database import initialize_database
from agent_hub.store import HubStore
from agent_hub_common import SCHEMA_VERSION, HubSettings, MetaKeys
from agent_hub_common.discovery import token_file, write_hub_json
from fastapi import FastAPI

TOKEN = "test-token"
BASE_URL = "http://hub.test"
# The orchestrator session the tests' client stands in as: every orchestrator
# operation on /rpc must name its caller (#128).
SESSION = "5f0c6f0e-8f3c-4d57-9d0a-0b8b1c1e2f3a"
CALLER_HEADERS = {"X-Robomate-Actor": "alice", "X-Robomate-Session": SESSION}

# Waits are cut to fractions of a second: the tests exercise the hold, not the
# production deadline.
TEST_WAIT_S = 0.2
TEST_MAX_WAIT_S = 1.0


class MonotonicClock:
    """Wall-clock time based on `time.monotonic()`, with optional test advances.

    A test that sleeps past a lease, or waits for a heartbeat stamp to move,
    measures the sleep on the monotonic clock, while the store stamps on the
    wall clock. A host that steps its wall clock back — WSL2 resyncs by seconds
    (#120, #95) — leaves the lease live or the stamp unmoved after the sleep;
    one clock for both keeps the sleep meaning what it says.
    """

    def __init__(self) -> None:
        self.start = datetime.now(UTC)
        self.started = monotonic()

    def __call__(self) -> datetime:
        return self.start + timedelta(seconds=monotonic() - self.started)

    def advance(self, seconds: float) -> None:
        """Move this clock forward without waiting for real time."""

        self.start += timedelta(seconds=seconds)


@pytest.fixture
def settings(tmp_path: Path) -> HubSettings:
    return HubSettings(
        host="127.0.0.1",
        port=8420,
        public_url="http://hub.example:8420",
        state_dir=tmp_path,
        database_path=tmp_path / "hub.db",
        token=TOKEN,
        token_file=tmp_path / "token",
        guides_dir=tmp_path / "guides",
        default_wait_s=TEST_WAIT_S,
        max_wait_s=TEST_MAX_WAIT_S,
        lost_after_s=60.0,
        sweep_interval_s=3600.0,
    )


@pytest.fixture
def store(settings: HubSettings) -> HubStore:
    initialize_database(settings.database_path)
    hub_store = HubStore(path=settings.database_path)
    hub_store.initialize_workflow()
    return hub_store


@pytest.fixture
def app(settings: HubSettings) -> FastAPI:
    return create_app(settings)


@pytest.fixture
def hub_store(app: FastAPI) -> HubStore:
    """The store the running app writes to — the tests stand in for Alice."""

    store = cast(HubStore, app.state.store)
    initialize_database(store.path)
    store.initialize_workflow()
    return store


@pytest_asyncio.fixture
async def client(app: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url=BASE_URL,
            headers={"Authorization": f"Bearer {TOKEN}", **CALLER_HEADERS},
        ) as connected,
    ):
        yield connected


def rpc(method: str, params: dict[str, Any], request_id: int = 1) -> dict[str, Any]:
    """Build one JSON-RPC request body."""

    return {"jsonrpc": "2.0", "id": request_id, "method": method, "params": params}


def message(
    text: str,
    *,
    context_id: str | None = None,
    task_id: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build the `params` of a message/send or message/stream request."""

    body: dict[str, Any] = {
        "messageId": f"m-{text[:8]}",
        "role": "user",
        "parts": [{"kind": "text", "text": text}],
    }
    if context_id is not None:
        body["contextId"] = context_id
    if task_id is not None:
        body["taskId"] = task_id
    resolved_metadata = dict(metadata or {})
    if context_id is not None or text.upper() == "READY":
        resolved_metadata.setdefault(MetaKeys.WORKER_INSTANCE_ID, "test-worker-instance")
    if resolved_metadata:
        body["metadata"] = resolved_metadata
    return {"message": body}


def sse_results(response: httpx.Response) -> list[Any]:
    """Return the JSON-RPC results carried by an SSE response body."""

    return [
        json.loads(line.removeprefix("data: "))["result"]
        for line in response.text.splitlines()
        if line.startswith("data: ")
    ]


async def check_in(
    client: httpx.AsyncClient,
    name: str,
    capabilities: list[str] | None = None,
    worker_instance_id: str = "test-worker-instance",
) -> str:
    """Register a worker and return the context id it must use from then on."""

    response = await client.post(
        "/a2a",
        json=rpc(
            "message/send",
            message(
                "READY",
                metadata={
                    MetaKeys.AGENT: name,
                    MetaKeys.CAPABILITIES: capabilities or ["python"],
                    MetaKeys.HARNESS: "claude-code",
                    MetaKeys.SCHEMA_VERSION: SCHEMA_VERSION,
                    MetaKeys.OPERATION_ID: uuid4().hex,
                    MetaKeys.WORKER_INSTANCE_ID: worker_instance_id,
                },
            ),
        ),
    )
    response.raise_for_status()
    return str(response.json()["result"]["contextId"])


@pytest.fixture
def healthz() -> Iterator[Callable[[str], str]]:
    """Start loopback servers whose /healthz reports a given hub_id; return their URLs.

    Stands in for a hub, or for another process that took its address (#147).
    """

    servers: list[ThreadingHTTPServer] = []

    def serve(hub_id: str) -> str:
        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:
                body = json.dumps({"ok": True, "hub_id": hub_id}).encode()
                self.send_response(200 if self.path == "/healthz" else 404)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *_args: object) -> None:
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        servers.append(server)
        return f"http://127.0.0.1:{server.server_address[1]}"

    try:
        yield serve
    finally:
        for server in servers:
            server.shutdown()
            server.server_close()


def closed_port_url() -> str:
    """A loopback URL nothing listens on: a stopped hub's recorded address."""

    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return f"http://127.0.0.1:{probe.getsockname()[1]}"


# Port blocks for tests that start a real hub (#198, #204, #217, #229).
#
# Each such test hands `robomate up` a private block through
# ROBOMATE_BASE_PORT, so the first-free scan does not share 842x with the
# operator's hubs or another suite on the same host. The block is probed and
# released before `up` binds it, so it must also lie outside the host's
# ephemeral range: in it, an outgoing connection can take a port in between.

# The documented defaults, used when the host's range cannot be read.
DEFAULT_EPHEMERAL_RANGES = {
    "linux": (32768, 60999),
    "win32": (49152, 65535),
    "darwin": (49152, 65535),
}
# Ports around robomate's production default, DEFAULT_BASE_PORT 8420.
PRODUCTION_PORTS = (8400, 8499)
# Below this, well-known services cluster and POSIX ports under 1024 need root.
TEST_PORT_FLOOR = 10000
# Room for the most hubs one test starts (four), with spare for skipped ports.
PORT_BLOCK_SIZE = 16


def _valid_range(start: int, end: int) -> tuple[int, int] | None:
    return (start, end) if 1 <= start <= end <= 65535 else None


def parse_ip_local_port_range(text: str) -> tuple[int, int] | None:
    """`/proc/sys/net/ipv4/ip_local_port_range`: "<first> <last>"."""

    numbers = [int(word) for word in text.split() if word.isdigit()]
    return _valid_range(*numbers) if len(numbers) == 2 else None


def parse_netsh_dynamic_ports(text: str) -> tuple[int, int] | None:
    """`netsh int ipv4 show dynamicport tcp`: start port, then number of ports.

    The labels are localised, so only the order of the "<label> : <number>"
    lines is relied on.
    """

    numbers = [
        int(match.group(1))
        for line in text.splitlines()
        if (match := re.search(r":\s*(\d+)\s*$", line))
    ]
    if len(numbers) != 2 or numbers[1] < 1:
        return None
    start, count = numbers
    return _valid_range(start, start + count - 1)


def parse_sysctl_port_range(text: str) -> tuple[int, int] | None:
    """`sysctl -n net.inet.ip.portrange.first net.inet.ip.portrange.last` (macOS)."""

    return parse_ip_local_port_range(text)


def _read_text(path: str) -> str:
    return Path(path).read_text(encoding="utf-8")


def _run_text(argv: list[str]) -> str:
    return subprocess.run(argv, check=True, capture_output=True, text=True, timeout=10).stdout


def ephemeral_port_range(
    platform: str = sys.platform,
    read_text: Callable[[str], str] = _read_text,
    run_text: Callable[[list[str]], str] = _run_text,
) -> tuple[int, int]:
    """The host's ephemeral (dynamic) TCP port range, or the platform default.

    Only reads: a test never changes the host's range.
    """

    parsed: tuple[int, int] | None = None
    try:
        if platform == "linux":
            parsed = parse_ip_local_port_range(read_text("/proc/sys/net/ipv4/ip_local_port_range"))
        elif platform == "win32":
            parsed = parse_netsh_dynamic_ports(
                run_text(["netsh", "int", "ipv4", "show", "dynamicport", "tcp"])
            )
        elif platform == "darwin":
            parsed = parse_sysctl_port_range(
                run_text(
                    ["sysctl", "-n", "net.inet.ip.portrange.first", "net.inet.ip.portrange.last"]
                )
            )
    except (OSError, subprocess.SubprocessError, UnicodeDecodeError):
        parsed = None
    return parsed or DEFAULT_EPHEMERAL_RANGES.get(platform, DEFAULT_EPHEMERAL_RANGES["linux"])


def _subtract(spans: list[tuple[int, int]], cut: tuple[int, int]) -> list[tuple[int, int]]:
    low, high = cut
    kept = []
    for start, end in spans:
        if start < low:
            kept.append((start, min(end, low - 1)))
        if end > high:
            kept.append((max(start, high + 1), end))
    return kept


def port_block_spans(
    ephemeral: tuple[int, int], count: int = PORT_BLOCK_SIZE
) -> tuple[list[tuple[int, int]], bool]:
    """Spans of usable block bases, and whether they lie outside `ephemeral`.

    Each span is (first base, last base), inclusive. A block always avoids
    the production ports. When the ephemeral range leaves no room for a
    block (a host configured for 1024-65535), the bases fall back to the
    whole test range and the second value is False: see reserve_port_block.
    """

    spans = _subtract([(TEST_PORT_FLOOR, 65535)], PRODUCTION_PORTS)
    outside = _subtract(spans, ephemeral)
    for candidate, is_outside in ((outside, True), (spans, False)):
        bases = [(start, end - count + 1) for start, end in candidate if end - start + 1 >= count]
        if bases:
            return bases, is_outside
    raise AssertionError(f"no room for a {count}-port block")


def _probe_block(base: int, count: int) -> None:
    """Bind and release every port of the block; raise the first OSError."""

    held: list[socket.socket] = []
    try:
        for port in range(base, base + count):
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            held.append(sock)
            sock.bind(("127.0.0.1", port))
    finally:
        for sock in held:
            sock.close()


def reserve_port_block(
    count: int = PORT_BLOCK_SIZE,
    *,
    ephemeral: tuple[int, int] | None = None,
    probe: Callable[[int, int], None] = _probe_block,
    attempts: int = 100,
) -> int:
    """A base port whose `count` consecutive ports were all free just now.

    The base is drawn at random from outside the host's ephemeral range and
    away from 842x, then every port of the block is bound and released. A
    port in use, forbidden (WinError 10013 / EACCES) or in a Windows excluded
    range fails the probe, and another random base is tried. `bind(0)` is
    never used: on Windows it counts up from the bottom of the dynamic range,
    which can sit in the low thousands for a long time (#217).

    This is not race-free. Releasing the block before `robomate up` binds it
    leaves a gap in which another process can take a port. Outside the
    ephemeral range only an explicit bind can do that, and the random base
    makes it unlikely. A host whose ephemeral range covers every candidate
    (1024-65535) gets a block inside it, where an outgoing connection can
    also take a port in the gap: the race behind #207 (#229).
    """

    spans, _ = port_block_spans(ephemeral or ephemeral_port_range(), count)
    sizes = [last - first + 1 for first, last in spans]
    last_error: OSError | None = None
    for _ in range(attempts):
        index = random.randrange(sum(sizes))
        for (first, _last), size in zip(spans, sizes, strict=True):
            if index < size:
                base = first + index
                break
            index -= size
        try:
            probe(base, count)
        except OSError as exc:
            last_error = exc
            continue
        return base
    raise AssertionError(
        f"could not reserve a free {count}-port block in {attempts} attempts; "
        f"last probe failure: {last_error}"
    )


def record_local_hub(checkout: Path, url: str, hub_id: str) -> Path:
    """A git checkout whose hub state records a hub at `url` with `hub_id`."""

    checkout.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q", str(checkout)], check=True, capture_output=True)
    write_hub_json(checkout, {"url": url, "hub_id": hub_id, "name": checkout.name, "pid": None})
    token_file(checkout).write_text("local-token\n", encoding="utf-8")
    return checkout
