"""Fixtures shared by the hub's HTTP and state tests."""

import json
import os
import random
import re
import socket
import subprocess
import sys
import threading
from collections.abc import AsyncIterator, Callable, Iterator
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from time import monotonic, sleep
from typing import Any, cast
from uuid import uuid4

import httpx
import pytest
import pytest_asyncio
from agent_hub import create_app
from agent_hub.database import initialize_database
from agent_hub.store import HubStore
from agent_hub_common import SCHEMA_VERSION, HubSettings, MetaKeys
from agent_hub_common.discovery import legacy_state_dir, write_hub_json
from fastapi import FastAPI
from timing import FrozenClock, db_latency, db_latency_s

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


def pytest_report_header() -> str:
    latency_s = db_latency_s()
    return f"db latency probe: {latency_s * 1000:g} ms" if latency_s else "db latency probe: off"


@pytest.fixture(autouse=True, scope="session")
def db_latency_probe() -> Iterator[None]:
    """The slow-host probe, on only when ROBOMATE_TEST_DB_LATENCY_MS is set (#367)."""

    with db_latency(db_latency_s()):
        yield


@pytest.fixture(autouse=True)
def isolated_machine_state(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """#264: inherited worker credentials must never select the run's hub/state.

    Every xdist process has its own monkeypatch state and each test its own
    directory. Tests needing explicit discovery overrides set them afterwards.
    The default operator credential follows the isolated registry.
    """
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "xdg"))
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "localappdata"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg-config"))
    monkeypatch.setenv("APPDATA", str(tmp_path / "appdata"))
    for name in list(os.environ):
        if name in ("ROBOMATE_HUB_URL", "ROBOMATE_OPERATOR_TOKEN_FILE") or name.startswith(
            "ROBOMATE_TOKEN"
        ):
            monkeypatch.delenv(name)


@pytest.fixture
def settings(tmp_path: Path, request: pytest.FixtureRequest) -> HubSettings:
    """Test hub settings; `@pytest.mark.hub_settings(field=value)` overrides fields."""

    resolved = HubSettings(
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
        detect_host_pauses=False,
    )
    marker = request.node.get_closest_marker("hub_settings")
    return replace(resolved, **marker.kwargs) if marker else resolved


@pytest.fixture
def store(settings: HubSettings, request: pytest.FixtureRequest) -> HubStore:
    initialize_database(settings.database_path)
    hub_store = HubStore(
        path=settings.database_path,
        detect_host_pauses=settings.detect_host_pauses,
    )
    hub_store.initialize_workflow()
    if not request.node.get_closest_marker("real_clock"):
        clock = request.getfixturevalue("frozen_clock")
        clock.install(hub_store)
    return hub_store


@pytest.fixture
def app(settings: HubSettings, request: pytest.FixtureRequest) -> FastAPI:
    application = create_app(settings)
    if not request.node.get_closest_marker("real_clock"):
        clock = request.getfixturevalue("frozen_clock")
        clock.install(cast(HubStore, application.state.store))
    return application


@pytest.fixture
def hub_store(app: FastAPI) -> HubStore:
    """The store the running app writes to — the tests stand in for Alice."""

    store = cast(HubStore, app.state.store)
    initialize_database(store.path)
    store.initialize_workflow()
    return store


@pytest.fixture
def frozen_clock(request: pytest.FixtureRequest) -> FrozenClock:
    """Frozen clock, installed by default on `store` and `app` / `hub_store`.

    Opt out for tests that explicitly verify real-time behaviors with
    `@pytest.mark.real_clock`. Hub deadlines then move only by
    `frozen_clock.advance(seconds)`. Install it on any other store the test
    builds, such as a restarted app's, with `frozen_clock.install(...)`. See
    docs/development/testing-time-and-waits.md.
    """

    return FrozenClock()


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
    `delay_s` holds each answer back, like a hub busy on a loaded host (#230).
    """

    servers: list[ThreadingHTTPServer] = []

    def serve(hub_id: str, delay_s: float = 0.0) -> str:
        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:
                sleep(delay_s)
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


@pytest.fixture
def silent_url() -> Iterator[Callable[[], str]]:
    """Open loopback listeners that never accept or answer; return their URLs.

    A connection completes into the backlog and then hears nothing, like a
    hub too busy to reply, or a process that holds its port and stays mute.
    """

    listeners: list[socket.socket] = []

    def listen() -> str:
        listener = socket.create_server(("127.0.0.1", 0), backlog=16)
        listeners.append(listener)
        return f"http://127.0.0.1:{listener.getsockname()[1]}"

    try:
        yield listen
    finally:
        for listener in listeners:
            listener.close()


@pytest.fixture
def trickle_url() -> Iterator[Callable[[str, float], str]]:
    """Serve a valid /healthz answer one byte every `interval_s`; return the URLs.

    Each read makes progress, so a per-read timeout never fires: only a total
    deadline stops the client (#266 r1-1). Stopped and closed after the test.
    """

    stop = threading.Event()
    listeners: list[socket.socket] = []

    def serve(listener: socket.socket, hub_id: str, interval_s: float) -> None:
        body = json.dumps({"hub_id": hub_id}).encode() + b" " * 400
        head = (
            "HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n"
            f"Content-Length: {len(body)}\r\nConnection: close\r\n\r\n"
        ).encode()
        while not stop.is_set():
            try:
                connection, _ = listener.accept()
            except OSError:
                return
            with connection:
                try:
                    connection.recv(4096)
                    for byte in head + body:
                        if stop.wait(interval_s):
                            break
                        connection.sendall(bytes([byte]))
                except OSError:
                    pass

    def trickle(hub_id: str, interval_s: float) -> str:
        listener = socket.create_server(("127.0.0.1", 0))
        listeners.append(listener)
        threading.Thread(target=serve, args=(listener, hub_id, interval_s), daemon=True).start()
        return f"http://127.0.0.1:{listener.getsockname()[1]}"

    try:
        yield trickle
    finally:
        stop.set()
        for listener in listeners:
            listener.close()


def closed_port_url() -> str:
    """A loopback URL nothing listens on: a stopped hub's recorded address."""

    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return f"http://127.0.0.1:{probe.getsockname()[1]}"


# Port blocks for hub-starting tests (#198, #204, #217, #229).

# The platform's default ephemeral (dynamic) TCP range, inclusive, used when the
# host's own setting cannot be read.
DEFAULT_EPHEMERAL_RANGES: dict[str, tuple[int, int]] = {
    "linux": (32768, 60999),
    "win32": (49152, 65535),
    "darwin": (49152, 65535),
}
FALLBACK_EPHEMERAL_RANGE = (32768, 65535)
# Test blocks start here: clear of privileged ports, well-known services and
# production `robomate up`, which scans from 8420, so run hubs and sibling
# worktree hubs live in 842x.
LOWEST_TEST_PORT = 10000
# Fewer bases than this outside the ephemeral range are too few to draw from.
MIN_OUTSIDE_BASES = 1000


def _linux_ephemeral_range() -> tuple[int, int] | None:
    text = Path("/proc/sys/net/ipv4/ip_local_port_range").read_text(encoding="ascii")
    low, high = (int(field) for field in text.split())
    return low, high


def _windows_ephemeral_range() -> tuple[int, int] | None:
    """Parse `netsh int ipv4 show dynamicport tcp`.

    The labels are localised, so take the first two `<label> : <number>`
    lines in order: start port, then number of ports.
    """

    output = subprocess.run(
        ["netsh", "int", "ipv4", "show", "dynamicport", "tcp"],
        capture_output=True,
        text=True,
        timeout=10,
        check=True,
    ).stdout
    numbers = [int(found) for found in re.findall(r"^[^:\n]*:\s*(\d+)\s*$", output, re.M)]
    if len(numbers) < 2:
        return None
    start, count = numbers[:2]
    return start, start + count - 1


def _macos_ephemeral_range() -> tuple[int, int] | None:
    output = subprocess.run(
        ["sysctl", "-n", "net.inet.ip.portrange.first", "net.inet.ip.portrange.last"],
        capture_output=True,
        text=True,
        timeout=10,
        check=True,
    ).stdout
    low, high = (int(field) for field in output.split())
    return low, high


EPHEMERAL_RANGE_READERS: dict[str, Callable[[], tuple[int, int] | None]] = {
    "linux": _linux_ephemeral_range,
    "win32": _windows_ephemeral_range,
    "darwin": _macos_ephemeral_range,
}


def ephemeral_port_range(platform: str = sys.platform) -> tuple[int, int]:
    """The host's ephemeral TCP port range, inclusive; read only, never changed.

    Falls back to the platform default when the setting can't be read or
    makes no sense.
    """

    default = DEFAULT_EPHEMERAL_RANGES.get(platform, FALLBACK_EPHEMERAL_RANGE)
    reader = EPHEMERAL_RANGE_READERS.get(platform)
    if reader is None:
        return default
    try:
        found = reader()
    except (OSError, ValueError, subprocess.SubprocessError):
        return default
    if found is None or not 1 <= found[0] <= found[1] <= 65535:
        return default
    return found


def port_block_bases(count: int, ephemeral: tuple[int, int]) -> list[int]:
    """Bases of `count`-port blocks from LOWEST_TEST_PORT up, outside `ephemeral` if room.

    A host whose ephemeral range covers (nearly) everything from
    LOWEST_TEST_PORT up, such as Windows set to 1024-65534, leaves fewer than
    MIN_OUTSIDE_BASES bases outside it. Concurrent suites drawing from so few
    would collide (all on 65535 for one port), so then every base from
    LOWEST_TEST_PORT up qualifies, inside the range.
    """

    low, high = ephemeral
    usable = range(LOWEST_TEST_PORT, 65536 - count + 1)
    outside = [base for base in usable if base + count - 1 < low or base > high]
    return outside if len(outside) >= MIN_OUTSIDE_BASES else list(usable)


def reserve_port_block(
    count: int = 16, *, platform: str = sys.platform, attempts: int = 100
) -> int:
    """Base of `count` consecutive free loopback ports for a test's own hubs.

    Give the base to `ROBOMATE_BASE_PORT` so `robomate up`'s first-free scan
    runs there and not from 8420, or use it as a pinned `--port`. The block is
    drawn at random (from the OS's random source, so two suites seeded alike
    still draw apart) from 10000 up, outside the host's ephemeral range,
    then every port in it is probe-bound. Any bind failure (in use, WinError
    10013, a Windows excluded range) rejects the block and draws another;
    after `attempts` blocks it raises RuntimeError. It never relies on
    `bind(0)`, whose result follows the host's allocator (#217).

    Not race-free: the probe releases the block before `robomate up` binds it.
    Outside the ephemeral range only an explicit bind can take a port in that
    gap, such as a concurrent suite drawing an overlapping block, which the
    random base makes unlikely. On a host whose ephemeral range leaves too
    little room (Windows set to 1024-65534 or 1024-65535; see
    port_block_bases) the block falls back inside that range, where
    an outgoing connection can also take a port, as in #207. The first-free
    scan skips a port taken that way, but a test that pins a port, restarts a
    hub on its saved port or expects a particular port then fails; rerun it.
    """

    bases = port_block_bases(count, ephemeral_port_range(platform))
    draw = random.SystemRandom()
    for _ in range(attempts):
        base = draw.choice(bases)
        held: list[socket.socket] = []
        try:
            for port in range(base, base + count):
                probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                held.append(probe)
                probe.bind(("127.0.0.1", port))
            return base
        except OSError:
            continue
        finally:
            for probe in held:
                probe.close()
    raise RuntimeError(
        f"no {count} consecutive free loopback ports after {attempts} random blocks "
        f"in {bases[0]}-{bases[-1] + count - 1}"
    )


def record_local_hub(checkout: Path, url: str, hub_id: str) -> Path:
    """A git checkout whose hub state records a hub at `url` with `hub_id`."""

    checkout.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q", str(checkout)], check=True, capture_output=True)
    state = legacy_state_dir(checkout)
    write_hub_json(state, {"url": url, "hub_id": hub_id, "name": checkout.name, "pid": None})
    (state / "token").write_text("local-token\n", encoding="utf-8")
    return checkout
