import asyncio
from dataclasses import replace
from pathlib import Path
from typing import cast

import httpx
import pytest
from agent_hub import create_app
from agent_hub.database import database
from agent_hub.gitlab_gate import GitLabGate, GitLabProject, UnboundGitLabGate
from agent_hub.merge_gate import MergeGate
from agent_hub.orchestrator import OrchestratorOps
from agent_hub.rpc_errors import OPERATOR_REQUIRED
from agent_hub.store import HubStore
from agent_hub_common import HubSettings, load_or_create_token
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


def test_operator_rpc_requires_token_and_exposes_identity(
    settings: HubSettings, tmp_path: Path
) -> None:
    stopped = []
    info = {"hub_id": "test-hub", "repo_root": "/repo", "robomate_version": "0.1.0"}
    operator_file = tmp_path / "registry" / "operator-token"
    load_or_create_token(None, operator_file)
    operator = {"X-Robomate-Operator": operator_file.read_text().strip()}
    settings = replace(settings, operator_token_file=operator_file)
    app = create_app(settings, hub_info=info, shutdown=lambda: stopped.append(True))
    with TestClient(app) as client:
        assert client.get("/healthz").json() == {"status": "ok", "hub_id": "test-hub"}
        request = {"jsonrpc": "2.0", "id": 1, "method": "hub.info"}
        assert client.post("/rpc", json=request).status_code == 401
        headers = {"Authorization": f"Bearer {settings.token}"}
        response = client.post("/rpc", json=request, headers=headers)
        assert response.json()["result"] == info
        for request_id in (None, 1.5):  # Valid, if discouraged, JSON-RPC ids.
            body = client.post("/rpc", json={**request, "id": request_id}, headers=headers).json()
            assert body == {"jsonrpc": "2.0", "id": request_id, "result": info}
        request["method"] = "hub.shutdown"
        # The bearer token alone, which every agent holds, is not enough (#128).
        for wrong in ({}, {"X-Robomate-Operator": "guess"}):
            body = client.post("/rpc", json=request, headers={**headers, **wrong}).json()
            assert body["error"]["code"] == OPERATOR_REQUIRED
        assert stopped == []
        response = client.post("/rpc", json=request, headers={**headers, **operator})
        assert response.json()["result"] == {"stopping": True}
    assert stopped == [True]
    with database(settings.database_path) as connection:
        rows = connection.execute(
            "SELECT actor, session, method, outcome FROM rpc_audit ORDER BY id"
        ).fetchall()
    assert [tuple(row) for row in rows] == [
        (None, None, "hub.shutdown", f"error {OPERATOR_REQUIRED}"),
        (None, None, "hub.shutdown", f"error {OPERATOR_REQUIRED}"),
        ("operator", None, "hub.shutdown", "ok"),
    ]


def test_without_an_operator_token_file_operator_methods_are_refused(
    settings: HubSettings, tmp_path: Path
) -> None:
    stopped = []
    missing = replace(settings, operator_token_file=tmp_path / "absent" / "operator-token")
    app = create_app(missing, hub_info={"hub_id": "h"}, shutdown=lambda: stopped.append(True))
    request = {"jsonrpc": "2.0", "id": 1, "method": "hub.shutdown"}
    headers = {"Authorization": f"Bearer {settings.token}", "X-Robomate-Operator": ""}
    with TestClient(app) as client:
        body = client.post("/rpc", json=request, headers=headers).json()
    assert body["error"]["code"] == OPERATOR_REQUIRED
    assert stopped == []
    assert not missing.operator_token_file.exists()  # type: ignore[union-attr]


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


SANDBOX_ORIGIN = "git@gitlab-box.local:RoboNater/robomate-glab-sandbox.git"


def gate_of(app: FastAPI) -> object:
    return cast(OrchestratorOps, app.state.orchestrator).gate


@pytest.mark.parametrize(
    "hub_info",
    [
        None,
        {"hub_id": "h", "origin": "git@github.com:octo/sandbox.git", "forge": "github"},
        {"hub_id": "h", "origin": "git@git.example:octo/sandbox.git", "forge": "unknown"},
        {"hub_id": "h", "origin": SANDBOX_ORIGIN},
    ],
)
def test_the_github_gate_serves_every_hub_not_detected_as_gitlab(
    settings: HubSettings, hub_info: dict[str, str] | None
) -> None:
    assert type(gate_of(create_app(settings, hub_info=hub_info))) is MergeGate


def test_a_gitlab_hub_binds_its_gate_to_the_origin_project_on_every_start(
    settings: HubSettings,
) -> None:
    info = {"hub_id": "h", "origin": SANDBOX_ORIGIN, "forge": "gitlab"}

    first, restarted = (gate_of(create_app(settings, hub_info=info)) for _ in range(2))

    assert isinstance(first, GitLabGate) and isinstance(restarted, GitLabGate)
    assert (
        first.project
        == restarted.project
        == GitLabProject("gitlab-box.local", ("RoboNater", "robomate-glab-sandbox"))
    )


@pytest.mark.parametrize(
    "origin",
    [
        "http://gitlab-box.local/a/b.git",
        "https://[::1/a/b.git",
        "https://[gitlab-box.local]/a/b.git",
        "ssh://git@gitlab-box.local:port/a/b.git",
    ],
)
def test_a_gitlab_hub_without_a_supported_origin_gets_a_gate_that_fails_closed(
    settings: HubSettings, origin: str
) -> None:
    info = {"hub_id": "h", "origin": origin, "forge": "gitlab"}

    assert isinstance(gate_of(create_app(settings, hub_info=info)), UnboundGitLabGate)
