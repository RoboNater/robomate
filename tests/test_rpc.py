"""The `/rpc` route: JSON-RPC envelope, validation, error codes, holds, and callers."""

import asyncio
import time
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from typing import Any, cast

import httpx
import pytest
from agent_hub import create_app
from agent_hub.database import database
from agent_hub.gitlab_gate import GitLabGate, GlabResult
from agent_hub.merge_gate import GhResult, MergeGate, MergeGateError
from agent_hub.orchestrator import OrchestratorOps
from agent_hub.rpc import (
    ACTOR_HEADER,
    CONFLICT,
    INTERNAL_ERROR,
    INVALID_PARAMS,
    INVALID_REQUEST,
    MERGE_GATE_UNAVAILABLE,
    METHOD_NOT_FOUND,
    NOT_FOUND,
    OPERATOR_HEADER,
    OPERATOR_REQUIRED,
    PAYLOAD_TOO_LARGE,
    SESSION_HEADER,
    SUPERSEDE_METHOD,
    RpcDispatcher,
    RpcError,
    require_operator,
)
from agent_hub.store import HubStore
from agent_hub_common import MAX_MESSAGE_PART_BYTES, AgentProfile, EventKind, HubSettings
from conftest import BASE_URL, CALLER_HEADERS, SESSION, TOKEN, MonotonicClock, check_in, rpc
from fastapi import FastAPI

PR = "https://github.com/octo/sandbox/pull/7"
HEAD = "0123456789abcdef0123456789abcdef01234567"


async def call(
    client: httpx.AsyncClient, method: str, headers: dict[str, str] | None = None, **params: Any
) -> dict[str, Any]:
    response = await client.post("/rpc", json=rpc(method, params), headers=headers)
    assert response.status_code == 200
    return cast(dict[str, Any], response.json())


async def bare_call(
    app: FastAPI, method: str, headers: dict[str, str] | None = None, **params: Any
) -> dict[str, Any]:
    """Call with the bearer token and only the given headers, not the session's."""

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url=BASE_URL,
        headers={"Authorization": f"Bearer {TOKEN}"},
    ) as bare:
        response = await bare.post("/rpc", json=rpc(method, params), headers=headers)
    assert response.status_code == 200
    return cast(dict[str, Any], response.json())


def error_of(body: dict[str, Any]) -> tuple[int, str]:
    assert "result" not in body, body
    return body["error"]["code"], body["error"]["message"]


async def test_rpc_requires_the_bearer_token(app: FastAPI) -> None:
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=BASE_URL) as bare,
    ):
        assert (await bare.post("/rpc", json=rpc("get_state", {}))).status_code == 401
        wrong = {"Authorization": "Bearer wrong"}
        response = await bare.post("/rpc", json=rpc("get_state", {}), headers=wrong)
        assert response.status_code == 401
        good = {"Authorization": f"Bearer {TOKEN}", **CALLER_HEADERS}
        response = await bare.post("/rpc", json=rpc("get_state", {}), headers=good)
        assert response.json()["result"]["workflow"] is None


async def test_hub_status_is_compact_and_reports_open_work(
    app: FastAPI, client: httpx.AsyncClient, hub_store: HubStore
) -> None:
    dispatcher = cast(RpcDispatcher, app.state.rpc)
    dispatcher.hub_info = {
        "repo_root": "/repo",
        "origin": "git@github.com:example/repo.git",
        "forge": "github",
        "url": "http://hub.test",
        "default_branch": "main",
    }
    hub_store.check_in("bob", AgentProfile(harness="codex", model="gpt-6-sol"))
    task = hub_store.assign_task("bob", "implementer", "Build", "secret instructions")
    hub_store.open_question(task.id, "bob", "question text", "q1")
    headers = {ACTOR_HEADER: "alice", SESSION_HEADER: SESSION}
    await call(client, "hub.heartbeat", headers)
    result = (await call(client, "hub.status"))["result"]
    assert {
        key: result[key] for key in ("repo_root", "origin", "forge", "url", "default_branch")
    } == dispatcher.hub_info
    assert result["workflow"]["status"] == "active"
    assert result["orchestrator"]["name"] == "alice"
    assert result["orchestrator"]["session"] == SESSION
    assert result["orchestrator"]["last_seen"]
    assert result["agents"] == [
        {
            "name": "bob",
            "harness": "codex",
            "model": "gpt-6-sol",
            "status": "busy",
            "alive": True,
            "current_task": task.id,
        }
    ]
    assert result["tasks"] == [
        {
            "id": task.id,
            "role": "implementer",
            "assignee": "bob",
            "state": "input-required",
            "pr_url": None,
            "head_sha": None,
        }
    ]
    assert result["pending_questions"] == 1
    assert result["operator_questions"] == 0
    hub_store.ask_user("Operator question text", None, actor="alice", session=SESSION)
    assert (await call(client, "hub.status"))["result"]["operator_questions"] == 1
    assert "secret instructions" not in str(result)
    assert "question text" not in str(result)
    assert error_of(await call(client, "hub.status", unexpected=True)) == (
        INVALID_PARAMS,
        "hub.status takes no params",
    )


async def test_hub_status_shows_known_review_pr_and_head(
    app: FastAPI, client: httpx.AsyncClient, hub_store: HubStore
) -> None:
    cast(RpcDispatcher, app.state.rpc).hub_info = {
        "repo_root": "/repo",
        "origin": "git@github.com:example/repo.git",
        "forge": "github",
        "url": "http://hub.test",
        "default_branch": "main",
    }
    hub_store.check_in("bob", AgentProfile())
    hub_store.check_in("dave", AgentProfile())
    implement = hub_store.assign_task("bob", "implementer", "Build", "build")
    hub_store.submit_result(
        implement.id,
        "bob",
        {
            "outcome": "completed",
            "summary": "done",
            "pr_url": PR,
            "head_sha": HEAD,
        },
    )
    other_pr = "https://github.com/octo/sandbox/pull/8"
    other_head = "b" * 40
    second = hub_store.assign_task("dave", "implementer", "Build other", "build")
    hub_store.submit_result(
        second.id,
        "dave",
        {
            "outcome": "completed",
            "summary": "done",
            "pr_url": other_pr,
            "head_sha": other_head,
        },
    )
    prior_review = hub_store.assign_task(
        "dave", "reviewer", "Prior review", "review", pr_head_sha=other_head
    )
    hub_store.submit_result(
        prior_review.id,
        "dave",
        {
            "verdict": "changes_requested",
            "summary": "needs changes",
            "pr_url": other_pr,
            "reviewed_head_sha": other_head,
        },
    )
    review = hub_store.assign_task("bob", "reviewer", "Review", "review", pr_head_sha=HEAD)
    unknown = hub_store.assign_task(
        "dave", "reviewer", "Review unknown", "review", pr_head_sha="c" * 40
    )
    result = (await call(client, "hub.status"))["result"]
    assert result["tasks"] == [
        {
            "id": review.id,
            "role": "reviewer",
            "assignee": "bob",
            "state": "submitted",
            "pr_url": PR,
            "head_sha": HEAD,
        },
        {
            "id": unknown.id,
            "role": "reviewer",
            "assignee": "dave",
            "state": "submitted",
            "pr_url": None,
            "head_sha": "c" * 40,
        },
    ]


async def test_bridge_call_rows_are_recorded_as_mcp_for_the_session_actor(
    app: FastAPI,
    hub_store: HubStore,
) -> None:
    app.state.accounting.enabled = True
    headers = {"Authorization": f"Bearer {TOKEN}", ACTOR_HEADER: "alice", SESSION_HEADER: SESSION}
    row = {
        "boundary": "mcp",
        "actor": "alice",
        "tool": "get_state",
        "outcome": "ok",
        "bytes_in": 55,
        "bytes_out": 120,
        "started": "2026-01-01T00:00:00Z",
        "finished": "2026-01-01T00:00:01Z",
        "status": None,
        "content_bytes": 100,
        "repeat_bytes": 0,
        "task_id": None,
    }
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url=BASE_URL, headers=headers
        ) as client,
    ):
        assert (await call(client, "hub.record_calls", calls=[row]))["result"] == {
            "recorded": 1,
            "rejected": 0,
        }
        bad = {**row, "actor": "bob"}
        assert (await call(client, "hub.record_calls", calls=[bad, row]))["result"] == {
            "recorded": 1,
            "rejected": 1,
        }
        backwards = {**row, "started": "2026-01-01T00:00:02Z"}
        assert (await call(client, "hub.record_calls", calls=[backwards]))["result"] == {
            "recorded": 1,
            "rejected": 0,
        }
        assert (
            error_of(
                await call(client, "hub.record_calls", headers={SESSION_HEADER: ""}, calls=[row])
            )[0]
            == INVALID_REQUEST
        )
    with database(hub_store.path) as connection:
        rows = connection.execute("SELECT boundary, actor, content_bytes FROM call_log").fetchall()
    assert [tuple(row) for row in rows] == [("mcp", "alice", 100)] * 3


async def test_disabled_hub_identifies_accounting_as_off(
    client: httpx.AsyncClient,
) -> None:
    row = {
        "boundary": "mcp",
        "actor": "alice",
        "tool": "get_state",
        "outcome": "ok",
        "bytes_in": 55,
        "bytes_out": 120,
        "started": "2026-01-01T00:00:00Z",
        "finished": "2026-01-01T00:00:01Z",
        "status": None,
        "content_bytes": 100,
        "repeat_bytes": 0,
        "task_id": None,
    }
    result = await call(
        client,
        "hub.record_calls",
        headers={ACTOR_HEADER: "alice", SESSION_HEADER: SESSION},
        calls=[row],
    )
    assert result["result"] == {"recorded": 0, "rejected": 0, "disabled": True}


async def test_superseded_bridge_can_flush_accounting_without_reclaiming_session(
    app: FastAPI,
    client: httpx.AsyncClient,
    hub_store: HubStore,
) -> None:
    app.state.accounting.enabled = True
    old = {ACTOR_HEADER: "alice", SESSION_HEADER: SESSION}
    newer_session = "0d6f4b5e-1a2b-4c3d-8e9f-a0b1c2d3e4f5"
    new = {ACTOR_HEADER: "alice", SESSION_HEADER: newer_session}
    assert "result" in await call(client, "get_state", old)
    assert "result" in await call(client, "get_state", new)
    row = {
        "boundary": "mcp",
        "actor": "alice",
        "tool": "get_state",
        "outcome": "ok",
        "bytes_in": 55,
        "bytes_out": 120,
        "started": "2026-01-01T00:00:00Z",
        "finished": "2026-01-01T00:00:01Z",
        "status": None,
        "content_bytes": 100,
        "repeat_bytes": 0,
        "task_id": None,
    }
    assert (await call(client, "hub.record_calls", old, calls=[row]))["result"] == {
        "recorded": 1,
        "rejected": 0,
    }
    dispatcher = cast(RpcDispatcher, app.state.rpc)
    assert dispatcher.orchestrator is not None
    assert dispatcher.orchestrator.session == newer_session
    assert error_of(await call(client, "get_state", old))[0] == CONFLICT
    with database(hub_store.path) as connection:
        count = connection.execute(
            "SELECT COUNT(*) FROM call_log WHERE boundary = 'mcp'"
        ).fetchone()
    assert count is not None and count[0] == 1


async def test_unknown_methods_and_malformed_requests_are_refused(
    client: httpx.AsyncClient, hub_store: HubStore
) -> None:
    body = await call(client, "no_such_method")
    assert body["id"] == 1
    assert error_of(body) == (METHOD_NOT_FOUND, "Method not found")
    # Operator methods exist only on a hub started by `robomate up`.
    assert error_of(await call(client, "hub.info"))[0] == METHOD_NOT_FOUND

    for payload in (
        [rpc("get_state", {})],  # no batches
        {"jsonrpc": "1.0", "id": 1, "method": "get_state"},
        {"jsonrpc": "2.0", "method": "get_state"},  # notifications have no reply
        {"jsonrpc": "2.0", "id": 1, "method": 7},
    ):
        response = await client.post("/rpc", json=payload)
        assert error_of(response.json()) == (INVALID_REQUEST, "Invalid Request")
    response = await client.post("/rpc", content=b"{not json")
    assert error_of(response.json()) == (INVALID_REQUEST, "Invalid Request")

    for params in ([1], None, "x"):
        # r1-1: an explicit null is not "no params"; wait_for_event would hold 100 s.
        for method in ("get_state", "wait_for_event"):
            request = {"jsonrpc": "2.0", "id": 2, "method": method, "params": params}
            assert error_of((await client.post("/rpc", json=request)).json()) == (
                INVALID_PARAMS,
                "params must be an object",
            )
    omitted = {"jsonrpc": "2.0", "id": "a", "method": "get_state"}
    body = (await client.post("/rpc", json=omitted)).json()
    assert body["id"] == "a" and body["result"]["workflow"]["status"] == "active"


async def test_every_json_rpc_id_is_echoed_and_other_ids_are_refused(
    client: httpx.AsyncClient, hub_store: HubStore
) -> None:
    # JSON-RPC 2.0 allows a string, a number, or null (the last two discouraged).
    for request_id in ("a", 7, 1.5, None):
        request = {"jsonrpc": "2.0", "id": request_id, "method": "get_state"}
        body = (await client.post("/rpc", json=request)).json()
        assert body["id"] == request_id and "result" in body, body
        request["method"] = "no_such_method"
        body = (await client.post("/rpc", json=request)).json()
        assert body["id"] == request_id and body["error"]["code"] == METHOD_NOT_FOUND
    for invalid_id in (True, {"a": 1}, [1]):
        invalid = {"jsonrpc": "2.0", "id": invalid_id, "method": "get_state"}
        body = (await client.post("/rpc", json=invalid)).json()
        assert body["id"] is None
        assert error_of(body) == (INVALID_REQUEST, "Invalid Request")
    # r2-1: numbers Python parses as non-finite, sent raw since json= refuses them.
    for raw_id in ("1e400", "-1e400", "NaN", "Infinity"):
        raw = f'{{"jsonrpc": "2.0", "id": {raw_id}, "method": "get_state"}}'
        response = await client.post(
            "/rpc", content=raw, headers={"Content-Type": "application/json"}
        )
        assert response.status_code == 200
        body = response.json()
        assert body["id"] is None
        assert error_of(body) == (INVALID_REQUEST, "Invalid Request")


@pytest.mark.parametrize(
    ("method", "params", "field"),
    [
        ("check_merge_gate", {"pr_url": PR, "expected_head_sha": "abc"}, "expected_head_sha"),
        ("check_merge_gate", {"pr_url": PR, "expected_head_sha": HEAD + "0"}, "expected_head_sha"),
        (
            "assign_task",
            {
                "agent": "bob",
                "role": "reviewer",
                "title": "Review",
                "instructions": "Read it",
                "event_id": 1,
                "pr_head_sha": "not-a-sha",
            },
            "pr_head_sha",
        ),
        (
            "assign_task",
            {
                "agent": "bob",
                "role": "r",
                "title": "t",
                "instructions": "i",
                "event_id": 1,
                "lease_min": 0,
            },
            "lease_min",
        ),
        ("wait_for_event", {"timeout_s": 121}, "timeout_s"),
        ("wait_for_event", {"timeout_s": -1}, "timeout_s"),
        ("set_task_state", {"task_id": "t", "state": "done", "note": "n"}, "state"),
        ("set_workflow_status", {"status": "finished", "summary": "s"}, "status"),
        ("reply", {"task_id": "t", "text": "hi"}, "message_id"),
    ],
)
async def test_params_are_validated_with_the_tool_constraints(
    client: httpx.AsyncClient, hub_store: HubStore, method: str, params: dict[str, Any], field: str
) -> None:
    code, text = error_of(await call(client, method, **params))

    assert code == INVALID_PARAMS
    assert "validation error" in text
    assert f"\n{field}\n" in text
    assert hub_store.tasks() == []


async def test_params_the_operation_does_not_take_are_refused(
    client: httpx.AsyncClient, hub_store: HubStore
) -> None:
    assert error_of(await call(client, "get_state", verbose=True)) == (
        INVALID_PARAMS,
        "unexpected params for get_state: ['verbose']",
    )
    # A bound method's own name for its instance is not a parameter either.
    assert error_of(await call(client, "release_agent", agent="bob", self=1)) == (
        INVALID_PARAMS,
        "unexpected params for release_agent: ['self']",
    )


class FailingGate:
    def __init__(self, error: Exception) -> None:
        self.error = error

    async def check(self, pr_url: str, expected_head_sha: str) -> None:
        raise self.error


async def test_errors_map_to_stable_codes_with_the_original_message(
    app: FastAPI, client: httpx.AsyncClient, hub_store: HubStore
) -> None:
    assert error_of(await call(client, "release_agent", agent="nobody")) == (
        NOT_FOUND,
        "unknown agent: nobody",
    )
    code, text = error_of(await call(client, "initialize_workflow", goal="Something else"))
    assert code == CONFLICT and text.startswith("workflow is already initialized")
    code, text = error_of(
        await call(client, "initialize_workflow", goal="g", policy={"max_review_round": 5})
    )
    assert code == INVALID_PARAMS and "invalid workflow policy" in text

    hub_store.check_in("bob", AgentProfile())
    event = hub_store.lease_next_event()
    assert event is not None
    code, text = error_of(
        await call(
            client,
            "assign_task",
            agent="bob",
            role="implementer",
            title="Big",
            instructions="x" * MAX_MESSAGE_PART_BYTES,
            event_id=event.id,
        )
    )
    assert code == PAYLOAD_TOO_LARGE and f"maximum is {MAX_MESSAGE_PART_BYTES} bytes" in text

    ops = cast(OrchestratorOps, app.state.orchestrator)
    ops.gate = cast(MergeGate, FailingGate(MergeGateError("the gh CLI is not on PATH")))
    assert error_of(await call(client, "check_merge_gate", pr_url=PR, expected_head_sha=HEAD)) == (
        MERGE_GATE_UNAVAILABLE,
        "the gh CLI is not on PATH",
    )
    ops.gate = cast(MergeGate, FailingGate(RuntimeError("unexpected")))
    assert error_of(await call(client, "check_merge_gate", pr_url=PR, expected_head_sha=HEAD)) == (
        INTERNAL_ERROR,
        "unexpected",
    )


SANDBOX_ORIGIN = "git@gitlab-box.local:RoboNater/robomate-glab-sandbox.git"
MR = "https://gitlab-box.local/RoboNater/robomate-glab-sandbox/-/merge_requests/4"


@asynccontextmanager
async def serving(app: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url=BASE_URL,
            headers={"Authorization": f"Bearer {TOKEN}", **CALLER_HEADERS},
        ) as connected,
    ):
        yield connected


class RecordingCli:
    """Stands in for gh or glab: records each call and fails it."""

    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    async def gh(self, args: Sequence[str]) -> GhResult:
        self.calls.append(list(args))
        return GhResult(1, "", "recorded")

    async def glab(self, args: Sequence[str]) -> GlabResult:
        self.calls.append(list(args))
        return GlabResult(1, "", "recorded")


@pytest.mark.parametrize(
    ("hub_info", "url", "first_call"),
    [
        (
            {"hub_id": "h", "origin": SANDBOX_ORIGIN, "forge": "gitlab"},
            MR,
            [
                "api",
                "projects/RoboNater%2Frobomate-glab-sandbox/merge_requests/4"
                "?include_diverged_commits_count=true",
                "--hostname=gitlab-box.local",
            ],
        ),
        (
            {"hub_id": "h", "origin": "git@github.com:octo/sandbox.git", "forge": "github"},
            PR,
            ["pr", "view", PR, "--json", "state,headRefOid,baseRefName,mergeable,mergeStateStatus"],
        ),
        (
            None,
            PR,
            ["pr", "view", PR, "--json", "state,headRefOid,baseRefName,mergeable,mergeStateStatus"],
        ),
    ],
)
async def test_check_merge_gate_is_dispatched_by_the_hubs_forge(
    settings: HubSettings,
    hub_info: dict[str, str] | None,
    url: str,
    first_call: list[str],
) -> None:
    app = create_app(settings, hub_info=hub_info)
    gate = cast(OrchestratorOps, app.state.orchestrator).gate
    cli = RecordingCli()
    if isinstance(gate, GitLabGate):
        gate.runner = cli.glab
    else:
        assert isinstance(gate, MergeGate)
        gate.runner = cli.gh

    async with serving(app) as client:
        code, text = error_of(
            await call(client, "check_merge_gate", pr_url=url, expected_head_sha=HEAD)
        )

    assert code == MERGE_GATE_UNAVAILABLE and "recorded" in text
    assert cli.calls == [first_call]


@pytest.mark.parametrize(
    "url",
    [
        PR,
        "https://gitlab.com/RoboNater/robomate-glab-sandbox/-/merge_requests/4",
        "https://gitlab-box.local/RoboNater/robomate-glab-scratch/-/merge_requests/4",
        "https://gitlab-box.local:8443/RoboNater/robomate-glab-sandbox/-/merge_requests/4",
        "http://gitlab-box.local/RoboNater/robomate-glab-sandbox/-/merge_requests/4",
    ],
)
async def test_a_gitlab_hub_refuses_another_projects_url_without_calling_glab(
    settings: HubSettings, url: str
) -> None:
    app = create_app(
        settings, hub_info={"hub_id": "h", "origin": SANDBOX_ORIGIN, "forge": "gitlab"}
    )
    gate = cast(GitLabGate, cast(OrchestratorOps, app.state.orchestrator).gate)
    cli = RecordingCli()
    gate.runner = cli.glab

    async with serving(app) as client:
        code, text = error_of(
            await call(client, "check_merge_gate", pr_url=url, expected_head_sha=HEAD)
        )

    assert code == MERGE_GATE_UNAVAILABLE
    assert text.startswith("refusing merge request URL")
    assert cli.calls == []


async def test_a_gitlab_hub_with_an_unsupported_origin_fails_the_gate_closed(
    settings: HubSettings,
) -> None:
    info = {"hub_id": "h", "origin": "https://gitlab-box.local:8443/a/b.git", "forge": "gitlab"}

    async with serving(create_app(settings, hub_info=info)) as client:
        code, text = error_of(
            await call(client, "check_merge_gate", pr_url=MR, expected_head_sha=HEAD)
        )

    assert code == MERGE_GATE_UNAVAILABLE
    assert text.startswith("the GitLab merge gate is unavailable: GitLab on HTTPS port 8443")


async def test_wait_for_event_holds_the_request_until_an_event_arrives(
    client: httpx.AsyncClient, hub_store: HubStore
) -> None:
    held = asyncio.create_task(call(client, "wait_for_event", timeout_s=5))
    await asyncio.sleep(0.1)
    assert not held.done()

    await check_in(client, "bob")

    body = await asyncio.wait_for(held, 2)
    event = body["result"]["event"]
    assert event["kind"] == "agent_checked_in"
    assert event["payload"]["agent"] == "bob"
    assert event["delivery_attempts"] == 1
    assert hub_store.get_state()["unacked_delivered"][0]["delivery_id"] == event["delivery_id"]


async def test_wait_for_event_times_out_with_a_null_event(client: httpx.AsyncClient) -> None:
    started = time.monotonic()

    body = await call(client, "wait_for_event", timeout_s=0.2)

    assert body["result"] == {"event": None}
    assert time.monotonic() - started >= 0.2


async def test_ack_of_an_expired_delivery_still_acks_it(
    client: httpx.AsyncClient, hub_store: HubStore, settings: HubSettings
) -> None:
    # #50: an action that outlives the delivery lease is not redone.
    clock = MonotonicClock()
    hub_store.clock = clock
    await check_in(client, "bob")
    delivered = (await call(client, "wait_for_event", timeout_s=0))["result"]["event"]
    clock.advance(settings.event_lease_s + 1)

    body = await call(client, "wait_for_event", timeout_s=0, ack=delivered["delivery_id"])

    assert body["result"] == {"event": None}
    state = hub_store.get_state()
    assert state["unacked_delivered"] == [] and state["queued_events"] == 0


async def test_an_expired_delivery_is_redelivered_without_an_ack(
    client: httpx.AsyncClient, hub_store: HubStore, settings: HubSettings
) -> None:
    clock = MonotonicClock()
    hub_store.clock = clock
    await check_in(client, "bob")
    first = (await call(client, "wait_for_event", timeout_s=0))["result"]["event"]
    clock.advance(settings.event_lease_s + 1)

    again = (await call(client, "wait_for_event", timeout_s=0))["result"]["event"]

    assert again["id"] == first["id"]
    assert again["delivery_attempts"] == 2
    assert again["delivery_id"] != first["delivery_id"]


async def test_the_callers_session_is_recorded_for_orchestrator_calls(
    app: FastAPI, client: httpx.AsyncClient, hub_store: HubStore
) -> None:
    dispatcher = cast(RpcDispatcher, app.state.rpc)
    assert dispatcher.orchestrator is None

    headers = {ACTOR_HEADER: "alice", SESSION_HEADER: SESSION.upper()}
    await call(client, "get_state", headers=headers)
    first = dispatcher.orchestrator
    assert first is not None
    assert (first.actor, first.session) == ("alice", SESSION)
    assert first.last_seen.tzinfo is not None

    newer = "0d6f4b5e-1a2b-4c3d-8e9f-a0b1c2d3e4f5"
    await call(
        client,
        "log_decision",
        {ACTOR_HEADER: "alice", SESSION_HEADER: newer},
        summary="s",
        rationale="r",
    )
    assert dispatcher.orchestrator is not None
    assert dispatcher.orchestrator.session == newer


async def test_heartbeat_fences_old_session_and_releases_delivery(
    app: FastAPI, client: httpx.AsyncClient, hub_store: HubStore
) -> None:
    old = {ACTOR_HEADER: "alice", SESSION_HEADER: SESSION}
    new = {
        ACTOR_HEADER: "alice",
        SESSION_HEADER: "0d6f4b5e-1a2b-4c3d-8e9f-a0b1c2d3e4f5",
    }
    hub_store.check_in("bob", AgentProfile())
    first = (await call(client, "wait_for_event", old, timeout_s=0))["result"]["event"]
    assert first is not None
    assert (await call(client, "hub.heartbeat", old))["result"] == {"ok": True}
    assert hub_store.lease_next_event() is None

    assert (await call(client, "hub.heartbeat", new))["result"] == {"ok": True}
    again = (await call(client, "wait_for_event", new, timeout_s=0))["result"]["event"]
    assert again["id"] == first["id"] and again["delivery_attempts"] == 2
    assert error_of(await call(client, "get_state", old)) == (
        CONFLICT,
        "superseded by a newer orchestrator session",
    )
    assert error_of(await call(client, "hub.heartbeat", old)) == (
        CONFLICT,
        "superseded by a newer orchestrator session",
    )
    assert error_of(await bare_call(app, "hub.heartbeat"))[0] == INVALID_REQUEST
    assert error_of(await call(client, "hub.heartbeat", new, unexpected=True)) == (
        INVALID_PARAMS,
        "hub.heartbeat takes no params",
    )


async def test_superseded_held_call_releases_event_it_received_after_takeover(
    app: FastAPI, client: httpx.AsyncClient, hub_store: HubStore
) -> None:
    dispatcher = cast(RpcDispatcher, app.state.rpc)
    started = asyncio.Event()
    resume = asyncio.Event()
    original = dispatcher._operations["wait_for_event"]

    async def delayed(**params: Any) -> Any:
        started.set()
        await resume.wait()
        return await original(**params)

    dispatcher._operations["wait_for_event"] = delayed
    old = {ACTOR_HEADER: "alice", SESSION_HEADER: SESSION}
    new = {
        ACTOR_HEADER: "alice",
        SESSION_HEADER: "0d6f4b5e-1a2b-4c3d-8e9f-a0b1c2d3e4f5",
    }
    held = asyncio.create_task(call(client, "wait_for_event", old, timeout_s=0))
    await asyncio.wait_for(started.wait(), 1)
    assert "result" in await call(client, "get_state", new)
    hub_store.check_in("bob", AgentProfile())
    resume.set()
    assert error_of(await held) == (CONFLICT, "superseded by a newer orchestrator session")
    repeated = (await call(client, "wait_for_event", new, timeout_s=0))["result"]["event"]
    assert repeated["kind"] == "agent_checked_in"
    assert repeated["delivery_attempts"] == 2


async def test_malformed_caller_headers_are_refused(
    app: FastAPI, client: httpx.AsyncClient, hub_store: HubStore
) -> None:
    for headers in (
        {ACTOR_HEADER: "alice"},
        {SESSION_HEADER: SESSION},
        {ACTOR_HEADER: " ", SESSION_HEADER: SESSION},
        {ACTOR_HEADER: "a" * 129, SESSION_HEADER: SESSION},
        {ACTOR_HEADER: "alice", SESSION_HEADER: "not-a-uuid"},
    ):
        body = await bare_call(app, "log_decision", headers, summary="s", rationale="r")
        assert error_of(body)[0] == INVALID_REQUEST
    assert cast(RpcDispatcher, app.state.rpc).orchestrator is None
    with database(hub_store.path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM decision").fetchone()[0] == 0


def audit_rows(store: HubStore) -> list[tuple[Any, ...]]:
    with database(store.path) as connection:
        rows = connection.execute(
            "SELECT actor, session, method, outcome FROM rpc_audit ORDER BY id"
        ).fetchall()
    return [tuple(row) for row in rows]


async def test_orchestrator_operations_require_the_caller_headers(
    app: FastAPI, client: httpx.AsyncClient, hub_store: HubStore
) -> None:
    """#128: the bearer token alone, which every agent holds, names no caller."""

    dispatcher = cast(RpcDispatcher, app.state.rpc)
    dispatcher.hub_info = {
        "repo_root": "/repo",
        "origin": "o",
        "forge": "github",
        "url": BASE_URL,
        "default_branch": "main",
    }
    for method, params in (
        ("get_state", {}),
        ("wait_for_event", {"timeout_s": 0}),
        ("log_decision", {"summary": "s", "rationale": "r"}),
        ("set_workflow_status", {"status": "escalated", "summary": "s"}),
    ):
        code, text = error_of(await bare_call(app, method, **params))
        assert code == INVALID_REQUEST and ACTOR_HEADER in text, method
    assert dispatcher.orchestrator is None
    assert hub_store.get_state()["workflow"]["status"] == "active"
    # Read-only operator views stay open to the bearer token.
    assert "result" in await bare_call(app, "hub.info")
    assert "result" in await bare_call(app, "hub.status")

    logged = await call(client, "log_decision", summary="s", rationale="r")
    assert "result" in logged
    await call(client, "get_state")
    await call(client, "wait_for_event", timeout_s=0)

    refused = f"error {INVALID_REQUEST}"
    assert audit_rows(hub_store) == [
        (None, None, "log_decision", refused),
        (None, None, "set_workflow_status", refused),
        ("alice", SESSION, "log_decision", "ok"),
    ]


async def test_decisions_record_the_caller(client: httpx.AsyncClient, hub_store: HubStore) -> None:
    await call(client, "set_workflow_status", status="paused", summary="Hold")
    await call(client, "log_decision", summary="Plan", rationale="Why", key="k")

    with database(hub_store.path) as connection:
        rows = connection.execute(
            "SELECT summary, actor, session FROM decision ORDER BY id"
        ).fetchall()
    assert [tuple(row) for row in rows] == [("Hold", "alice", SESSION), ("Plan", "alice", SESSION)]
    assert [row[2:] for row in audit_rows(hub_store)] == [
        ("set_workflow_status", "ok"),
        ("log_decision", "ok"),
    ]


async def test_a_session_takeover_leaves_an_audit_row(
    client: httpx.AsyncClient, hub_store: HubStore
) -> None:
    newer = "0d6f4b5e-1a2b-4c3d-8e9f-a0b1c2d3e4f5"
    await call(client, "get_state")
    await call(client, "hub.heartbeat", {ACTOR_HEADER: "mallory", SESSION_HEADER: newer})
    assert error_of(await call(client, "log_decision", summary="s", rationale="r"))[0] == CONFLICT

    assert audit_rows(hub_store) == [
        ("mallory", newer, SUPERSEDE_METHOD, "ok"),
        ("alice", SESSION, "log_decision", f"error {CONFLICT}"),
    ]
    with database(hub_store.path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM decision").fetchone()[0] == 0


def test_require_operator_accepts_only_the_operator_token() -> None:
    require_operator({OPERATOR_HEADER: " secret "}, "secret")
    for headers, expected in (
        ({}, "secret"),
        ({OPERATOR_HEADER: "wrong"}, "secret"),
        ({OPERATOR_HEADER: "secret"}, None),
    ):
        with pytest.raises(RpcError) as raised:
            require_operator(headers, expected)
        assert raised.value.code == OPERATOR_REQUIRED


async def test_ask_user_holds_the_question_and_escalates_once(
    app: FastAPI, client: httpx.AsyncClient, hub_store: HubStore
) -> None:
    """#129: each question is kept, and every ask leaves a decision naming the asker."""

    first = await call(client, "ask_user", question="Merge or wait?", options=["merge", "wait"])
    second = await call(client, "ask_user", question="And the follow-up?")
    assert first["result"] == {"question_id": 1}
    assert second["result"] == {"question_id": 2}
    assert hub_store.get_state()["workflow"]["status"] == "escalated"
    with database(hub_store.path) as connection:
        decisions = connection.execute(
            "SELECT summary, rationale, actor, session FROM decision ORDER BY id"
        ).fetchall()
        asked = connection.execute(
            "SELECT actor, session, answer, answered, answered_by FROM operator_question"
        ).fetchall()
    assert [tuple(row) for row in decisions] == [
        ("Asked the operator question 1", "Workflow status set to escalated", "alice", SESSION),
        (
            "Asked the operator question 2",
            "Workflow already escalated; question added",
            "alice",
            SESSION,
        ),
    ]
    assert [tuple(row) for row in asked] == [("alice", SESSION, None, None, None)] * 2
    assert [row[2:] for row in audit_rows(hub_store)] == [("ask_user", "ok"), ("ask_user", "ok")]

    # Read with the bearer token alone: no caller headers, and no audit rows.
    listed = (await bare_call(app, "hub.questions"))["result"]["questions"]
    assert [(q["question_id"], q["question"], q["options"], q["actor"]) for q in listed] == [
        (1, "Merge or wait?", ["merge", "wait"], "alice"),
        (2, "And the follow-up?", None, "alice"),
    ]
    assert (await bare_call(app, "hub.operator_answer", question_id=1))["result"] == {
        "question_id": 1,
        "question": "Merge or wait?",
        "status": "unanswered",
        "answer": None,
        "answered": None,
    }
    assert len(audit_rows(hub_store)) == 2


OPERATOR_TOKEN = "operator-secret-for-tests"
OPERATOR = {OPERATOR_HEADER: OPERATOR_TOKEN}


async def test_hub_answer_needs_the_operator_and_answers_once(
    app: FastAPI, client: httpx.AsyncClient, hub_store: HubStore
) -> None:
    """#130: only the operator answers, once, and the orchestrator hears of it."""

    cast(RpcDispatcher, app.state.rpc).operator_token = OPERATOR_TOKEN
    await call(client, "ask_user", question="Merge?")

    # The bearer token every agent holds is not enough, nor are an
    # orchestrator's caller headers or a wrong operator token.
    for headers in (None, CALLER_HEADERS, {OPERATOR_HEADER: "wrong"}):
        body = await bare_call(app, "hub.answer", headers, question_id=1, answer="yes")
        assert error_of(body)[0] == OPERATOR_REQUIRED
    assert (await bare_call(app, "hub.operator_answer", question_id=1))["result"][
        "status"
    ] == "unanswered"

    answered = await bare_call(app, "hub.answer", OPERATOR, question_id=1, answer="yes")
    assert answered["result"]["question_id"] == 1
    read = (await bare_call(app, "hub.operator_answer", question_id=1))["result"]
    assert read == {
        "question_id": 1,
        "question": "Merge?",
        "status": "answered",
        "answer": "yes",
        "answered": answered["result"]["answered"],
    }
    with database(hub_store.path) as connection:
        row = connection.execute(
            "SELECT answer, answered, answered_by FROM operator_question WHERE id = 1"
        ).fetchone()
    assert tuple(row) == ("yes", read["answered"], "operator")
    assert (await bare_call(app, "hub.questions"))["result"] == {"questions": []}

    event = (await call(client, "wait_for_event", timeout_s=0))["result"]["event"]
    assert event["kind"] == EventKind.USER_ANSWERED.value
    assert event["payload"] == {"question_id": 1, "answer": "yes"}

    again = await bare_call(app, "hub.answer", OPERATOR, question_id=1, answer="no")
    assert error_of(again) == (CONFLICT, "operator question 1 is already answered")
    assert (await bare_call(app, "hub.operator_answer", question_id=1))["result"]["answer"] == "yes"
    assert [row[:3] for row in audit_rows(hub_store)][-5:] == [
        (None, None, "hub.answer"),
        ("alice", SESSION, "hub.answer"),
        (None, None, "hub.answer"),
        ("operator", None, "hub.answer"),
        ("operator", None, "hub.answer"),
    ]
    assert [row[3] for row in audit_rows(hub_store)][-2:] == ["ok", f"error {CONFLICT}"]


async def test_hub_answer_refuses_bad_params_unknown_ids_and_no_operator_token(
    app: FastAPI, client: httpx.AsyncClient
) -> None:
    body = await bare_call(app, "hub.answer", OPERATOR, question_id=1, answer="yes")
    assert error_of(body) == (OPERATOR_REQUIRED, "this hub has no operator credential loaded")

    cast(RpcDispatcher, app.state.rpc).operator_token = OPERATOR_TOKEN
    await call(client, "ask_user", question="Merge?")
    unknown = await bare_call(app, "hub.answer", OPERATOR, question_id=9, answer="yes")
    assert error_of(unknown) == (NOT_FOUND, "unknown operator question: 9")
    for params in (
        {},
        {"question_id": 1},
        {"question_id": "1", "answer": "yes"},
        {"question_id": True, "answer": "yes"},
        {"question_id": 1, "answer": 3},
        {"question_id": 1, "answer": "yes", "x": 2},
        {"question_id": 1, "answer": "  "},
    ):
        body = await bare_call(app, "hub.answer", OPERATOR, **params)
        assert error_of(body)[0] == INVALID_PARAMS, params
    oversized = "x" * (MAX_MESSAGE_PART_BYTES + 1)
    body = await bare_call(app, "hub.answer", OPERATOR, question_id=1, answer=oversized)
    assert error_of(body)[0] == PAYLOAD_TOO_LARGE


async def test_operator_question_reads_refuse_bad_params_and_unknown_ids(
    app: FastAPI, client: httpx.AsyncClient
) -> None:
    assert error_of(await bare_call(app, "hub.operator_answer", question_id=9)) == (
        NOT_FOUND,
        "unknown operator question: 9",
    )
    for params in ({}, {"question_id": "1"}, {"question_id": True}, {"question_id": 1, "x": 2}):
        body = await bare_call(app, "hub.operator_answer", **params)
        assert error_of(body)[0] == INVALID_PARAMS
    assert error_of(await bare_call(app, "hub.questions", x=1))[0] == INVALID_PARAMS


async def test_ask_user_requires_the_caller_headers(
    app: FastAPI, client: httpx.AsyncClient, hub_store: HubStore
) -> None:
    body = await bare_call(app, "ask_user", question="Merge?")
    assert error_of(body)[0] == INVALID_REQUEST
    with database(hub_store.path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM operator_question").fetchone()[0] == 0


async def test_wait_for_event_delivers_user_answered(
    client: httpx.AsyncClient, hub_store: HubStore
) -> None:
    hub_store.append_event(EventKind.USER_ANSWERED, {"question_id": 1, "answer": "merge"})

    event = (await call(client, "wait_for_event", timeout_s=1))["result"]["event"]
    assert event["kind"] == "user_answered"
    assert event["payload"] == {"question_id": 1, "answer": "merge"}


async def test_escalation_is_left_only_once_the_operator_has_answered(
    app: FastAPI, client: httpx.AsyncClient, hub_store: HubStore
) -> None:
    """#132: only ask_user escalates, and the operator's answers end it."""

    cast(RpcDispatcher, app.state.rpc).operator_token = OPERATOR_TOKEN
    hub_store.check_in("bob", AgentProfile())
    assign: dict[str, Any] = {
        "agent": "bob",
        "role": "implementer",
        "title": "Build",
        "instructions": "Do it",
        "event_id": 1,
    }
    gate = {"pr_url": PR, "expected_head_sha": HEAD}

    body = await call(client, "set_workflow_status", status="escalated", summary="Help")
    code, text = error_of(body)
    assert code == INVALID_PARAMS and "ask_user" in text
    assert hub_store.get_state()["workflow"]["status"] == "active"

    await call(client, "ask_user", question="Merge?")
    await call(client, "ask_user", question="And then?")
    for status in ("active", "done"):
        body = await call(client, "set_workflow_status", status=status, summary="Go on")
        assert error_of(body) == (
            CONFLICT,
            "workflow is escalated with open operator questions 1, 2; "
            "wait for their user_answered events before changing its status",
        )
    for method, params in (("assign_task", assign), ("check_merge_gate", gate)):
        assert error_of(await call(client, method, **params)) == (
            CONFLICT,
            f"{method} is refused while the workflow is escalated: "
            "operator questions 1, 2 are open",
        )
    assert hub_store.tasks() == []

    await bare_call(app, "hub.answer", OPERATOR, question_id=1, answer="yes")
    body = await call(client, "set_workflow_status", status="active", summary="Go on")
    assert error_of(body)[0] == CONFLICT and "questions 2;" in error_of(body)[1]
    await bare_call(app, "hub.answer", OPERATOR, question_id=2, answer="ship it")
    assert (await call(client, "set_workflow_status", status="active", summary="Go on"))[
        "result"
    ] == {"ok": True}
    assert "result" in await call(client, "assign_task", **assign)

    # A later escalation names only its own questions.
    await call(client, "ask_user", question="Again?")
    await bare_call(app, "hub.answer", OPERATOR, question_id=3, answer="no")
    await call(client, "set_workflow_status", status="paused", summary="Hold")
    with database(hub_store.path) as connection:
        rows = connection.execute(
            "SELECT summary, rationale, actor, session FROM decision"
            " WHERE rationale LIKE 'Workflow status set to %' ORDER BY id"
        ).fetchall()
    assert [tuple(row) for row in rows] == [
        ("Asked the operator question 1", "Workflow status set to escalated", "alice", SESSION),
        (
            "Go on",
            "Workflow status set to active; operator answered questions 1, 2",
            "alice",
            SESSION,
        ),
        ("Asked the operator question 3", "Workflow status set to escalated", "alice", SESSION),
        (
            "Hold",
            "Workflow status set to paused; operator answered questions 3",
            "alice",
            SESSION,
        ),
    ]


async def test_a_legacy_escalation_without_questions_can_be_resumed(
    client: httpx.AsyncClient, hub_store: HubStore
) -> None:
    """#132: a workflow escalated by set_workflow_status before the guard is not stuck."""

    with database(hub_store.path) as connection:
        connection.execute("UPDATE workflow SET status = 'escalated'")
    body = await call(client, "check_merge_gate", pr_url=PR, expected_head_sha=HEAD)
    assert error_of(body) == (
        CONFLICT,
        "check_merge_gate is refused while the workflow is escalated: "
        "no operator question is open; resume with set_workflow_status(active)",
    )

    await call(client, "set_workflow_status", status="active", summary="Resume")
    assert hub_store.get_state()["workflow"]["status"] == "active"
    with database(hub_store.path) as connection:
        row = connection.execute("SELECT summary, rationale, actor FROM decision").fetchone()
    assert tuple(row) == ("Resume", "Workflow status set to active", "alice")
