import httpx
import pytest
from agent_hub.security import require_bearer
from agent_hub_common import SCHEMA_VERSION, MetaKeys
from conftest import BASE_URL, TOKEN, message, rpc
from fastapi import FastAPI
from fastapi.routing import APIRoute

PUBLIC_ROUTES = ["/.well-known/agent-card.json", "/healthz"]


async def anonymous(app: FastAPI) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=BASE_URL)


@pytest.mark.parametrize(
    "headers",
    [
        pytest.param({}, id="no-header"),
        pytest.param({"Authorization": f"Bearer {TOKEN}-wrong"}, id="wrong-token"),
        pytest.param({"Authorization": TOKEN}, id="no-scheme"),
        pytest.param({"Authorization": f"Basic {TOKEN}"}, id="wrong-scheme"),
        pytest.param({"Authorization": "Bearer "}, id="empty-token"),
    ],
)
async def test_the_a2a_route_refuses_anything_but_the_shared_token(
    app: FastAPI, headers: dict[str, str]
) -> None:
    async with app.router.lifespan_context(app), await anonymous(app) as client:
        response = await client.post(
            "/a2a",
            json=rpc("message/send", message("READY", metadata={MetaKeys.AGENT: "bob"})),
            headers=headers,
        )

    assert response.status_code == 401
    assert response.headers["www-authenticate"] == "Bearer"


async def test_the_token_is_accepted_in_any_case_of_the_scheme(app: FastAPI) -> None:
    async with app.router.lifespan_context(app), await anonymous(app) as client:
        response = await client.post(
            "/a2a",
            json=rpc(
                "message/send",
                message(
                    "READY",
                    metadata={
                        MetaKeys.AGENT: "bob",
                        MetaKeys.SCHEMA_VERSION: SCHEMA_VERSION,
                        MetaKeys.OPERATION_ID: "op-sec-1",
                    },
                ),
            ),
            headers={"Authorization": f"bearer {TOKEN}"},
        )

    assert response.status_code == 200


@pytest.mark.parametrize("route", PUBLIC_ROUTES)
async def test_discovery_and_health_stay_public(app: FastAPI, route: str) -> None:
    # A worker needs the card before it holds anything, and health checks run
    # before a token exists (§4.1).
    async with app.router.lifespan_context(app), await anonymous(app) as client:
        response = await client.get(route)

    assert response.status_code == 200


def test_no_route_reaches_state_without_the_token(app: FastAPI) -> None:
    """The public surface is a fixed list, not a property of what exists today."""

    unprotected = {
        route.path
        for route in app.routes
        if isinstance(route, APIRoute)
        and require_bearer not in {dependency.call for dependency in route.dependant.dependencies}
    }

    assert unprotected == set(PUBLIC_ROUTES)
    assert {route.path for route in app.routes if isinstance(route, APIRoute)} == {
        *PUBLIC_ROUTES,
        "/a2a",
        "/guides/{role}.md",
    }
