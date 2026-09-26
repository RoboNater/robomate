import asyncio
from dataclasses import replace

import httpx
from agent_hub import create_app
from agent_hub.store import HubStore
from agent_hub_common import HubSettings
from conftest import BASE_URL
from fastapi import FastAPI
from fastapi.testclient import TestClient


def test_app_initializes_state_and_serves_agent_card(settings: HubSettings) -> None:
    with TestClient(create_app(settings)) as client:
        response = client.get("/.well-known/agent-card.json")
        health = client.get("/healthz")

    assert response.status_code == 200
    card = response.json()
    assert card["name"] == "Agent Comms Hub"
    assert card["url"] == "http://hub.example:8420/a2a"
    assert card["capabilities"]["streaming"] is True
    assert card["securitySchemes"]["bearerAuth"]["scheme"] == "bearer"
    assert health.json()["status"] == "ok"
    assert isinstance(health.json()["hub_id"], str)
    assert settings.database_path.exists()


def test_startup_provisions_a_token_file_when_none_is_injected(
    settings: HubSettings,
) -> None:
    provisioned = replace(settings, token=None)

    with TestClient(create_app(provisioned)) as client:
        client.get("/healthz")

    assert provisioned.token_file.exists()


def test_operator_rpc_requires_token_and_exposes_identity(settings: HubSettings) -> None:
    stopped = []
    info = {"hub_id": "test-hub", "repo_root": "/repo", "robomate_version": "0.1.0"}
    app = create_app(settings, hub_info=info, shutdown=lambda: stopped.append(True))
    with TestClient(app) as client:
        assert client.get("/healthz").json() == {"status": "ok", "hub_id": "test-hub"}
        request = {"jsonrpc": "2.0", "id": 1, "method": "hub.info"}
        assert client.post("/rpc", json=request).status_code == 401
        headers = {"Authorization": f"Bearer {settings.token}"}
        response = client.post("/rpc", json=request, headers=headers)
        assert response.json()["result"] == info
        request["method"] = "hub.shutdown"
        assert client.post("/rpc", json=request, headers=headers).json()["result"] == {
            "stopping": True
        }
    assert stopped == [True]


async def test_the_sweeper_runs_only_for_the_lifetime_of_the_app(app: FastAPI) -> None:
    def sweeper_running() -> bool:
        return "hub-sweeper" in {task.get_name() for task in asyncio.all_tasks()}

    async with app.router.lifespan_context(app):
        assert sweeper_running()

    assert not sweeper_running()


async def test_the_routes_and_alices_tools_share_one_store(
    app: FastAPI, hub_store: HubStore, settings: HubSettings
) -> None:
    # Step 3 hangs Alice's MCP tools off this same object: a worker's check-in
    # over HTTP has to be visible to her without a second connection.
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=BASE_URL) as client,
    ):
        await client.get("/healthz")

    assert hub_store.path == settings.database_path
