"""Fixtures shared by the hub's HTTP and state tests."""

import json
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
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
from fastapi import FastAPI

TOKEN = "test-token"
BASE_URL = "http://hub.test"

# Waits are cut to fractions of a second: the tests exercise the hold, not the
# production deadline.
TEST_WAIT_S = 0.2
TEST_MAX_WAIT_S = 1.0


class MonotonicClock:
    """Wall-clock time that advances only as `time.monotonic()` does.

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
            headers={"Authorization": f"Bearer {TOKEN}"},
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
