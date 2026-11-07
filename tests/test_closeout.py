"""#215 acceptance scenario: real bridge/A2A clients, pytest-owned isolated hub.

Run: uv run --locked pytest tests/test_closeout.py -v
No live run state, agents, ports or external processes are used.
"""

import asyncio
import json
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest
from agent_hub import create_app
from agent_hub.database import database
from agent_hub.store import ConflictError, HubStore, NotFoundError, PayloadTooLargeError
from agent_hub_common import (
    AgentCloseoutReport,
    Finding,
    HubSettings,
    ReviewerResult,
    ReviewerVerdict,
    TaskState,
    WorkflowStatus,
)
from conftest import BASE_URL, TOKEN
from fastapi import FastAPI
from pydantic import ValidationError
from timing import LIVENESS_S, FrozenClock, wait_until
from worker_mcp.client import WorkerHubClient, WorkerProtocolError
from worker_mcp.config import WorkerSettings
from worker_mcp.orchestrator import OrchestratorBridge, create_orchestrator_mcp
from worker_mcp.tools import create_worker_mcp


def report(summary: str = "Implemented the assigned work.") -> dict[str, Any]:
    return {"summary": summary, "outstanding_items": [], "lessons": [], "claims": []}


async def call(server: Any, name: str, **args: Any) -> dict[str, Any]:
    result = await server.call_tool(name, args)
    assert isinstance(result, tuple)
    return dict(result[1])


def bridge_for(app: FastAPI) -> OrchestratorBridge:
    bridge = OrchestratorBridge()
    bridge._client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url=BASE_URL,
        headers={"Authorization": f"Bearer {TOKEN}"},
    )
    return bridge


def holding(store: HubStore, name: str) -> bool:
    """Whether `name`'s await_assignment hold is parked in the hub."""

    agent = store.agent_by_name(name)
    record = None if agent is None else store.activity.worker(name, agent.worker_instance_id)
    return record is not None and any(
        hold.kind == "await_assignment" for hold in record.holds.values()
    )


# The closeout window. It runs on the frozen clock, so it ends only when the
# test advances past it; its length is part of the scenario, not a budget.
WINDOW_S = 100


@pytest.mark.parametrize("crash", [False, True])
# Holds are bounded by LIVENESS_S, so lift the test hub's 1 s ceiling (#367).
@pytest.mark.hub_settings(max_wait_s=LIVENESS_S)
async def test_isolated_collection_scenario(
    app: FastAPI,
    hub_store: HubStore,
    settings: HubSettings,
    frozen_clock: FrozenClock,
    crash: bool,
) -> None:
    """All three reports, then one crash; closeout ordering and restart retention.

    Every hub deadline runs on `frozen_clock`; real time only bounds the waits
    (#367, docs/development/testing-time-and-waits.md).
    """
    async with app.router.lifespan_context(app):
        bridge = bridge_for(app)
        alice = create_orchestrator_mcp(bridge)
        workers: list[WorkerHubClient] = []
        servers = []
        try:
            for name in ("bob", "charlie"):
                http = httpx.AsyncClient(
                    transport=httpx.ASGITransport(app=app),
                    base_url=BASE_URL,
                    headers={"Authorization": f"Bearer {TOKEN}"},
                )
                worker = WorkerHubClient(
                    WorkerSettings(hub_url=BASE_URL, token=TOKEN, agent_name=name), http
                )
                workers.append(worker)
                server = create_worker_mcp(worker)
                servers.append(server)
                await call(server, "check_in")
                task = hub_store.assign_task(name, "implementer", "Work", "Do the work")
                assert (await call(server, "await_assignment", timeout_s=0))["task_id"] == task.id
                hub_store.submit_result(task.id, name, TaskState.COMPLETED, "Work completed")
            run_id = hub_store.get_state()["workflow"]["run_id"]
            pending = asyncio.create_task(
                call(servers[0], "await_assignment", timeout_s=LIVENESS_S)
            )
            # The request must wake a parked hold, not race its first poll.
            await wait_until(lambda: holding(hub_store, "bob"), "bob's hold is parked")
            requested = await call(
                alice, "request_closeout_reports", run_id=run_id, timeout_s=WINDOW_S
            )
            delivery = await pending
            assert delivery["closeout_requested"] is True and delivery["run_id"] == run_id
            assert "submit_closeout_report" in delivery["instructions"]
            assert "CloseoutResult" in delivery["instructions"]
            assert requested["deadline"] == delivery["deadline"]
            # Open work wins even while a report request is outstanding.
            work = hub_store.assign_task("charlie", "implementer", "Finish", "Finish")
            assert (await call(servers[1], "await_assignment", timeout_s=0))["task_id"] == work.id
            hub_store.submit_result(work.id, "charlie", TaskState.COMPLETED, "Finished")
            assert (await call(servers[1], "await_assignment", timeout_s=0))["closeout_requested"]
            # Worker report precedes the implementer's later closeout task.
            receipt = await call(
                servers[0], "submit_closeout_report", run_id=run_id, report=report()
            )
            assert receipt["author"] == "bob"
            assert (
                await call(servers[0], "submit_closeout_report", run_id=run_id, report=report())
                == receipt
            )
            assert await call(servers[0], "await_assignment", timeout_s=0) == {"timeout": True}
            if not crash:
                await call(
                    servers[1], "submit_closeout_report", run_id=run_id, report=report("Reviewed.")
                )
            else:
                # A crashed bridge sends no report. Advance past the window
                # and the loss grace; bob heartbeats and charlie, silent, is
                # lost. The first sweep has no baseline, so no host pause.
                frozen_clock.advance(max(WINDOW_S, settings.lost_after_s) + 1)
                await workers[0].heartbeat()
                hub_store.sweep(settings.lost_after_s)
            closeout = hub_store.assign_task("bob", "closeout", "Close out", "Route observations")
            if closeout:
                assert (await call(servers[0], "await_assignment", timeout_s=0))[
                    "role"
                ] == "closeout"
                hub_store.submit_result(
                    closeout.id, "bob", TaskState.COMPLETED, "Dispositions recorded"
                )
            await call(
                alice, "submit_closeout_report", run_id=run_id, report=report("Orchestrated.")
            )
            collected = await call(alice, "get_closeout_reports", run_id=run_id)
            bob = next(r for r in collected["reports"] if r["author"] == "bob")
            assert bob["timing"] == "collected_before_closeout"
            assert collected["reports"][-1]["author_kind"] == "orchestrator"
            if crash:
                assert collected["missing"] == [{"author": "charlie", "reason": "worker_lost"}]
            else:
                assert collected["missing"] == []
            # No report gate: done and release proceed even with the missing report.
            await call(alice, "set_workflow_status", status="done", summary="Completed")
            if not crash:
                assert (await call(servers[0], "await_assignment", timeout_s=0))["release"]
            with pytest.raises(Exception, match="finalized|released|lost"):
                await call(servers[0], "submit_closeout_report", run_id=run_id, report=report())
            before_restart = await call(alice, "get_closeout_reports", run_id=run_id)
            assert before_restart["finalized"]
        finally:
            for worker in workers:
                await worker.close()
                if worker._external_client:
                    await worker._external_client.aclose()
            await bridge.close()
    restarted = create_app(settings)
    frozen_clock.install(restarted.state.store)
    async with restarted.router.lifespan_context(restarted):
        new_bridge = bridge_for(restarted)
        try:
            restored = await new_bridge.call("get_closeout_reports", {"run_id": run_id})
            assert restored == before_restart
        finally:
            await new_bridge.close()


def prepare(store: HubStore, timeout_s: float = 100) -> str:
    store.check_in("bob", worker_instance_id="bob-instance")
    task = store.assign_task("bob", "implementer", "Build", "Build")
    store.submit_result(task.id, "bob", TaskState.COMPLETED, "Built")
    run_id = str(store.get_state()["workflow"]["run_id"])
    store.request_closeout_reports(run_id, "alice", "alice-session", timeout_s)
    return run_id


def test_strict_models_and_evidence() -> None:
    AgentCloseoutReport.model_validate(report())
    for invalid in (
        report(""),
        report(" "),
        report() | {"author": "charlie"},
        report() | {"claims": [{"text": "Passed", "verification": "verified"}]},
        report() | {"outstanding_items": [{"summary": "Later"}]},
        report() | {"outstanding_items": [{"summary": "Later", "url": "javascript:bad"}]},
        report() | {"lessons": [1]},
    ):
        with pytest.raises(ValidationError):
            AgentCloseoutReport.model_validate(invalid)
    AgentCloseoutReport.model_validate(
        report()
        | {
            "claims": [
                {"text": "Passed", "verification": "verified", "evidence": "pytest: exit 0"}
            ],
            "outstanding_items": [{"summary": "Later", "reason": "Needs operator triage"}],
        }
    )


def test_retry_membership_limits_and_secrets(store: HubStore) -> None:
    run_id = prepare(store)
    store.known_credentials = ("a-known-credential",)

    def submit(body: dict[str, Any]) -> dict[str, Any]:
        return store.submit_closeout_report(run_id, "bob", "bob-instance", "worker", body)

    for invalid in (
        report("a-known-credential"),
        report() | {"summary": 1},
        report()
        | {"outstanding_items": [{"summary": "x", "reason": "later", "finding_id": "r1-9"}]},
    ):
        with pytest.raises(ValueError) as error:
            submit(invalid)
        assert "a-known-credential" not in str(error.value)
    with pytest.raises(PayloadTooLargeError):
        submit(report() | {"lessons": ["x" * 8000] * 5})
    with pytest.raises(NotFoundError):
        store.submit_closeout_report("wrong-run", "bob", "bob-instance", "worker", report())
    for author, session, kind in (
        ("charlie", "x", "worker"),
        ("alice", "x", "worker"),
        ("bob", "wrong", "worker"),
    ):
        with pytest.raises(ConflictError):
            store.submit_closeout_report(run_id, author, session, kind, report())
    receipt = submit(report())
    assert submit(report()) == receipt
    with pytest.raises(ConflictError, match="different final"):
        submit(report("Changed"))
    with database(store.path) as connection:
        assert connection.execute("SELECT count(*) FROM agent_closeout_report").fetchone()[0] == 1
        assert (
            connection.execute(
                "SELECT count(*) FROM event WHERE kind = 'closeout_report_submitted'"
            ).fetchone()[0]
            == 1
        )
    store.release_agent("bob")
    with pytest.raises(ConflictError, match="released"):
        submit(report())


def test_pause_expiry_restart_and_missing(store: HubStore) -> None:
    run_id = prepare(store, 10)
    first = store.get_closeout_reports(run_id)
    now = store._now()
    store.set_workflow_status(WorkflowStatus.PAUSED, "Pause", actor="alice", session=None)
    store.clock = lambda: now + timedelta(seconds=300)
    assert store.get_closeout_reports(run_id)["missing"][0]["reason"] == "awaiting_report"
    restarted = HubStore(store.path, clock=store.clock)
    restarted.set_workflow_status(WorkflowStatus.ACTIVE, "Resume", actor="alice", session=None)
    resumed = restarted.get_closeout_reports(run_id)
    assert resumed["deadline"] > first["deadline"]
    restarted.clock = lambda: now + timedelta(seconds=311)
    assert (
        next(
            m["reason"]
            for m in restarted.get_closeout_reports(run_id)["missing"]
            if m["author"] == "bob"
        )
        == "deadline_expired"
    )
    with pytest.raises(ConflictError, match="window"):
        restarted.submit_closeout_report(run_id, "bob", "bob-instance", "worker", report())
    # Repeated requests cannot extend the bounded collection window.
    assert (
        restarted.request_closeout_reports(run_id, "alice", "new-session", 100)["deadline"]
        == resumed["deadline"]
    )
    # Alice can finish her report after worker collection expires, before done.
    restarted.submit_closeout_report(run_id, "alice", "new-session", "orchestrator", report())


def test_submission_serializes_against_finalization(store: HubStore) -> None:
    run_id = prepare(store)

    def submit() -> str:
        try:
            store.submit_closeout_report(run_id, "bob", "bob-instance", "worker", report())
            return "accepted"
        except ConflictError:
            return "finalized"

    with ThreadPoolExecutor(max_workers=2) as pool:
        future = pool.submit(submit)
        store.set_workflow_status(WorkflowStatus.DONE, "Done", actor="alice", session=None)
        outcome = future.result()
    result = store.get_closeout_reports(run_id)
    assert result["finalized"]
    assert ("bob" in result["submitted"]) == (outcome == "accepted")
    with pytest.raises(ConflictError):
        store.submit_closeout_report(run_id, "bob", "bob-instance", "worker", report())


async def test_request_survives_restart_and_a2a_splits_parts(
    app: FastAPI,
    hub_store: HubStore,
    client: httpx.AsyncClient,
) -> None:
    worker = WorkerHubClient(
        WorkerSettings(hub_url=BASE_URL, token=TOKEN, agent_name="bob"), client
    )
    await worker.check_in()
    task = hub_store.assign_task("bob", "implementer", "Build", "Build")
    hub_store.submit_result(task.id, "bob", TaskState.COMPLETED, "Built")
    run_id = hub_store.get_state()["workflow"]["run_id"]
    hub_store.request_closeout_reports(run_id, "alice", "alice-session")
    # The persisted request is visible without remembering the original prompt.
    reloaded = HubStore(hub_store.path)
    pending = await reloaded.await_assignment(str(worker.context_id), 0)
    assert isinstance(pending, dict) and pending["closeout_requested"]
    # >16 KiB encoded report: must travel in multiple bounded parts.
    large = report() | {"lessons": ["報告 😀 " * 500] * 3}
    assert 16384 < len(json.dumps(large, ensure_ascii=False).encode()) < 32768
    receipt = await worker.submit_closeout_report(run_id, large)
    assert hub_store.get_closeout_reports(run_id)["reports"][0]["report"] == large
    assert receipt["author"] == "bob"
    with pytest.raises(WorkerProtocolError):
        await worker.submit_closeout_report(run_id, report(TOKEN))
    await worker.close()


def test_unassigned_lost_participant_and_sequential_runs(store: HubStore) -> None:
    store.check_in("charlie", worker_instance_id="charlie-instance")
    now = store._now()
    store.clock = lambda: now + timedelta(seconds=2)
    store.sweep(1)
    run_id = prepare(store)
    assert {m["author"] for m in store.get_closeout_reports(run_id)["missing"]} == {
        "alice",
        "bob",
        "charlie",
    }
    store.submit_closeout_report(run_id, "bob", "bob-instance", "worker", report())
    store.set_workflow_status(WorkflowStatus.DONE, "Done", actor="alice", session=None)
    before = store.get_closeout_reports(run_id)
    store.start_workflow("Next")
    next_run = prepare(store)
    assert next_run != run_id
    assert {m["author"] for m in store.get_closeout_reports(next_run)["missing"]} == {
        "alice",
        "bob",
    }
    assert store.get_closeout_reports(run_id) == before
    with pytest.raises(ConflictError, match="finalized"):
        store.submit_closeout_report(run_id, "bob", "bob-instance", "worker", report())


def test_host_pause_shifts_deadline_before_expiry(store: HubStore) -> None:
    store.detect_host_pauses = True
    now = store._now()
    mono = store.monotonic_clock()
    store.clock = lambda: now
    store.monotonic_clock = lambda: mono
    run_id = prepare(store)
    store.sweep(60)
    first = store.get_closeout_reports(run_id)["deadline"]
    store.clock = lambda: now + timedelta(seconds=601)
    store.monotonic_clock = lambda: mono + 1
    shifted = store.get_closeout_reports(run_id)
    assert shifted["deadline"] > first
    assert all(m["reason"] == "awaiting_report" for m in shifted["missing"])
    store.submit_closeout_report(run_id, "bob", "bob-instance", "worker", report())


async def test_closeout_safe_errors_and_part_limits(
    client: httpx.AsyncClient,
    hub_store: HubStore,
    app: FastAPI,
    caplog: pytest.LogCaptureFixture,
) -> None:
    worker = WorkerHubClient(
        WorkerSettings(hub_url=BASE_URL, token=TOKEN, agent_name="bob"), client
    )
    await worker.check_in()
    run_id = hub_store.get_state()["workflow"]["run_id"]
    hub_store.request_closeout_reports(run_id, "alice", "session")
    secret = 'credential-"-with-escape'
    hub_store.known_credentials += (secret,)
    try:
        for bad in (report(TOKEN), report(secret), report() | {"author": "charlie"}):
            with pytest.raises(WorkerProtocolError) as error:
                await worker.submit_closeout_report(run_id, bad)
            assert TOKEN not in str(error.value) and secret not in str(error.value)
        with pytest.raises(WorkerProtocolError, match="message part"):
            await worker._post_rpc(
                "message/send",
                {
                    "message": {
                        "messageId": "large-part",
                        "contextId": worker.context_id,
                        "role": "user",
                        "parts": [{"kind": "text", "text": "x" * 16384}],
                        "metadata": {
                            "hub.kind": "closeout_report",
                            "hub.schema_version": 1,
                            "hub.worker_instance_id": worker.worker_instance_id,
                        },
                    }
                },
            )
        alice = bridge_for(app)
        try:
            for bad_rpc in (report(TOKEN), {"claims": [TOKEN]}, TOKEN):
                with pytest.raises(RuntimeError) as rpc_error:
                    await alice.call(
                        "submit_closeout_report", {"run_id": run_id, "report": bad_rpc}
                    )
                assert TOKEN not in str(rpc_error.value)
        finally:
            await alice.close()
        assert hub_store.get_closeout_reports(run_id)["reports"] == []
        assert secret not in caplog.text and TOKEN not in caplog.text
    finally:
        await worker.close()


async def test_accounted_shared_worker_submission(
    app: FastAPI,
    client: httpx.AsyncClient,
    hub_store: HubStore,
) -> None:
    app.state.accounting.enabled = True
    worker = WorkerHubClient(
        WorkerSettings(hub_url=BASE_URL, token=TOKEN, agent_name="bob"), client
    )
    server = create_worker_mcp(worker, include_orchestrator=True)
    await call(server, "check_in")
    run_id = hub_store.get_state()["workflow"]["run_id"]
    alice = bridge_for(app)
    try:
        await alice.call("request_closeout_reports", {"run_id": run_id})
        receipt = await call(server, "submit_closeout_report", run_id=run_id, report=report())
        assert receipt["author"] == "bob"
        # The stdio observer obtains its allowlist from the actual tool list.
        # These rows exercise the same hub allowlist used by its background flush.
        observer = await alice.accounting(create_orchestrator_mcp(alice))
        observer.observe_request(
            {
                "id": 1,
                "method": "tools/call",
                "params": {
                    "name": "submit_closeout_report",
                    "arguments": {"run_id": run_id, "report": report()},
                },
            },
            100,
        )
        result = await alice.call("submit_closeout_report", {"run_id": run_id, "report": report()})
        observer.observe_response(
            {"id": 1, "result": {"structuredContent": result, "content": []}}, 80
        )
        await alice.flush_accounting()
        with database(hub_store.path) as connection:
            rows = connection.execute(
                "SELECT boundary, actor, tool, bytes_in, bytes_out FROM call_log"
                " WHERE tool = 'submit_closeout_report'"
            ).fetchall()
        assert {(r["boundary"], r["actor"]) for r in rows} == {("a2a", "bob"), ("mcp", "alice")}
        assert all(r["bytes_in"] > 0 and r["bytes_out"] > 0 for r in rows)
    finally:
        await worker.close()
        await alice.close()


def test_verified_claims_and_finding_references(store: HubStore) -> None:
    store.check_in("bob", worker_instance_id="bob-instance")
    store.check_in("charlie", worker_instance_id="charlie-instance")
    task = store.assign_task("charlie", "reviewer", "Review", "Review", pr_head_sha="a" * 40)
    store.submit_result(
        task.id,
        "charlie",
        ReviewerResult(
            verdict=ReviewerVerdict.APPROVED,
            summary="Reviewed",
            reviewed_head_sha="a" * 40,
            nonblocking_findings=[Finding(id="r1-1", text="Follow up")],
        ),
    )
    run_id = store.get_state()["workflow"]["run_id"]
    store.request_closeout_reports(run_id, "alice", "session")
    body = report() | {
        "outstanding_items": [
            {
                "summary": "Follow up",
                "finding_id": "r1-1",
                "url": "https://github.com/RoboNater/robomate/issues/216",
            }
        ],
        "claims": [
            {"text": "Tests passed", "verification": "verified", "evidence": "pytest: exit 0"},
            {"text": "May help future runs", "verification": "unverified"},
        ],
    }
    receipt = store.submit_closeout_report(run_id, "bob", "bob-instance", "worker", body)
    now = store._now()
    store.clock = lambda: now + timedelta(seconds=301)
    assert store.submit_closeout_report(run_id, "bob", "bob-instance", "worker", body) == receipt
    stored = store.get_closeout_reports(run_id)["reports"][0]["report"]
    assert stored["claims"][0]["verification"] == "verified"
    assert stored["claims"][1]["verification"] == "unverified"
    store.release_agent("charlie")
    assert (
        next(
            m["reason"]
            for m in store.get_closeout_reports(run_id)["missing"]
            if m["author"] == "charlie"
        )
        == "deadline_expired"
    )


def test_early_release_records_missing_without_waiting(store: HubStore) -> None:
    run_id = prepare(store)
    store.release_agent("bob")
    assert (
        next(
            m["reason"]
            for m in store.get_closeout_reports(run_id)["missing"]
            if m["author"] == "bob"
        )
        == "agent_released"
    )
    with pytest.raises(ConflictError, match="released"):
        store.submit_closeout_report(run_id, "bob", "bob-instance", "worker", report())
    store.set_workflow_status(WorkflowStatus.DONE, "Done", actor="alice", session=None)
    assert store.get_closeout_reports(run_id)["finalized"]


def _deadline_delta_s(store: HubStore, run_id: str) -> float:
    """Return (deadline - requested_at) in seconds for #386 window assertions."""

    from datetime import datetime

    from agent_hub.database import database

    with database(store.path) as connection:
        row = connection.execute(
            "SELECT requested_at, deadline FROM closeout_collection"
            " WHERE workflow_id = (SELECT id FROM workflow WHERE run_id = ?)",
            (run_id,),
        ).fetchone()
    assert row is not None
    requested = datetime.fromisoformat(row["requested_at"].replace("Z", "+00:00"))
    deadline = datetime.fromisoformat(row["deadline"].replace("Z", "+00:00"))
    return (deadline - requested).total_seconds()


def test_closeout_default_uses_policy_300_and_explicit_overrides(
    tmp_path: Path, frozen_clock: FrozenClock
) -> None:
    """#386: omitted timeout uses run closeout_wait_s (default 300); explicit wins."""

    from agent_hub.database import initialize_database

    def fresh(policy: dict[str, Any] | None) -> tuple[HubStore, str]:
        path = Path(tmp_path) / f"{len(list(tmp_path.iterdir()))}.db"
        initialize_database(path)
        hub_store = HubStore(path)
        frozen_clock.install(hub_store)
        if policy is None:
            hub_store.initialize_workflow()
        else:
            hub_store.initialize_workflow("Goal", policy)
        hub_store.check_in("bob", worker_instance_id="bob-instance")
        task = hub_store.assign_task("bob", "implementer", "Build", "Build")
        hub_store.submit_result(task.id, "bob", TaskState.COMPLETED, "Built")
        run_id = str(hub_store.get_state()["workflow"]["run_id"])
        return hub_store, run_id

    # Stored policy without the key reads as the 300 s default.
    default_store, default_run = fresh(None)
    assert default_store.get_state()["workflow"]["policy"] == {}
    default_store.request_closeout_reports(default_run, "alice", "alice-session")
    assert _deadline_delta_s(default_store, default_run) == pytest.approx(300, abs=1)

    # --policy closeout_wait_s=600 gives a 600 s deadline.
    policy_store, policy_run = fresh({"closeout_wait_s": 600})
    policy_store.request_closeout_reports(policy_run, "alice", "alice-session")
    assert _deadline_delta_s(policy_store, policy_run) == pytest.approx(600, abs=1)

    # An explicit timeout overrides the policy on first request.
    override_store, override_run = fresh({"closeout_wait_s": 600})
    override_store.request_closeout_reports(override_run, "alice", "alice-session", 20)
    assert _deadline_delta_s(override_store, override_run) == pytest.approx(20, abs=1)

    # Repeated requests keep the original deadline.
    first = policy_store.get_closeout_reports(policy_run)["deadline"]
    assert (
        policy_store.request_closeout_reports(policy_run, "alice", "other-session", 100)["deadline"]
        == first
    )

    # The default window expires only after 300 s on the frozen clock.
    frozen_clock.install(default_store)
    frozen_clock.advance(299)
    assert all(
        m["reason"] == "awaiting_report"
        for m in default_store.get_closeout_reports(default_run)["missing"]
    )
    frozen_clock.advance(2)
    assert (
        next(
            m["reason"]
            for m in default_store.get_closeout_reports(default_run)["missing"]
            if m["author"] == "bob"
        )
        == "deadline_expired"
    )


def test_closeout_policy_and_argument_bounds_schema_and_hub(tmp_path: Path) -> None:
    """#386: policy and argument accept 1800, reject 0/negative/non-finite/>1800."""

    from agent_hub.database import initialize_database
    from agent_hub_common import WorkflowPolicy
    from agent_hub_common.orchestrator_tools import CloseoutTimeout
    from pydantic import TypeAdapter

    adapter = TypeAdapter(CloseoutTimeout)
    assert adapter.validate_python(1800) == 1800
    assert WorkflowPolicy.model_validate({"closeout_wait_s": 1800}).closeout_wait_s == 1800
    assert WorkflowPolicy().closeout_wait_s == 300

    for bad in (0, -1, 1801, 1800.5, float("inf"), float("-inf"), float("nan"), True):
        with pytest.raises(ValidationError):
            WorkflowPolicy.model_validate({"closeout_wait_s": bad})
    for bad in (0, -1, 1801, 1800.5, float("inf"), float("-inf"), float("nan")):
        with pytest.raises(ValidationError):
            adapter.validate_python(bad)

    for bad in (0, -1, 1801, 1800.5, float("inf"), float("-inf"), float("nan"), True):
        path = Path(tmp_path) / f"hub-{str(bad)}.db"
        initialize_database(path)
        hub_store = HubStore(path)
        hub_store.initialize_workflow()
        run_id = str(hub_store.get_state()["workflow"]["run_id"])
        with pytest.raises(ValueError, match="greater than zero and at most 1800"):
            hub_store.request_closeout_reports(run_id, "alice", "sess", bad)

    path = Path(tmp_path) / "hub-1800.db"
    initialize_database(path)
    hub_store = HubStore(path)
    hub_store.initialize_workflow()
    run_id = str(hub_store.get_state()["workflow"]["run_id"])
    hub_store.request_closeout_reports(run_id, "alice", "sess", 1800)
    assert _deadline_delta_s(hub_store, run_id) == pytest.approx(1800, abs=1)


def test_closeout_pause_with_longer_window_uses_advance(
    store: HubStore, frozen_clock: FrozenClock
) -> None:
    """#386: workflow pause suspends expiry and host pause shifts the 300 s window."""

    # Workflow pause: 300 s window survives a 400 s pause, then shifts on resume.
    run_id = prepare(store, 300)
    first = store.get_closeout_reports(run_id)["deadline"]
    store.set_workflow_status(WorkflowStatus.PAUSED, "Pause", actor="alice", session=None)
    frozen_clock.advance(400)
    assert all(
        m["reason"] == "awaiting_report" for m in store.get_closeout_reports(run_id)["missing"]
    )
    store.set_workflow_status(WorkflowStatus.ACTIVE, "Resume", actor="alice", session=None)
    resumed = store.get_closeout_reports(run_id)["deadline"]
    assert resumed > first
    frozen_clock.advance(299)
    assert all(
        m["reason"] == "awaiting_report" for m in store.get_closeout_reports(run_id)["missing"]
    )
    frozen_clock.advance(2)
    assert (
        next(
            m["reason"]
            for m in store.get_closeout_reports(run_id)["missing"]
            if m["author"] == "bob"
        )
        == "deadline_expired"
    )


def test_closeout_host_pause_shifts_longer_window(
    tmp_path: Path, frozen_clock: FrozenClock
) -> None:
    """#386: a detected host pause shifts the 300 s closeout deadline."""

    from agent_hub.database import initialize_database

    path = Path(tmp_path) / "hub.db"
    initialize_database(path)
    hub_store = HubStore(path, detect_host_pauses=True)
    frozen_clock.install(hub_store)
    hub_store.initialize_workflow()
    hub_store.check_in("bob", worker_instance_id="bob-instance")
    task = hub_store.assign_task("bob", "implementer", "Build", "Build")
    hub_store.submit_result(task.id, "bob", TaskState.COMPLETED, "Built")
    run_id = str(hub_store.get_state()["workflow"]["run_id"])
    hub_store.request_closeout_reports(run_id, "alice", "alice-session")
    first = hub_store.get_closeout_reports(run_id)["deadline"]
    hub_store.sweep(60)
    frozen_clock.advance(601)
    shifted = hub_store.get_closeout_reports(run_id)
    assert shifted["deadline"] > first
    assert all(m["reason"] == "awaiting_report" for m in shifted["missing"])


def test_closeout_submit_policy_override_600() -> None:
    """#386: robomate submit --policy closeout_wait_s=600 resolves to a 600 s policy."""

    from agent_hub.submit import _resolve_policy
    from agent_hub_common import WorkflowPolicy
    from robomate.cli import _parse_policy_override

    key, value = _parse_policy_override("closeout_wait_s=600")
    assert (key, value) == ("closeout_wait_s", 600)
    merged = _resolve_policy(WorkflowPolicy(), {key: value})
    assert merged["closeout_wait_s"] == 600

    key, value = _parse_policy_override("policy.closeout_wait_s=600")
    assert _resolve_policy(WorkflowPolicy(), {key: value})["closeout_wait_s"] == 600

    with pytest.raises(ValueError):
        _resolve_policy(WorkflowPolicy(), {"closeout_wait_s": 0})
    with pytest.raises(ValueError):
        _resolve_policy(WorkflowPolicy(), {"closeout_wait_s": 1801})


async def test_closeout_mcp_omitted_timeout_uses_policy_300(
    app: FastAPI,
    hub_store: HubStore,
    frozen_clock: FrozenClock,
) -> None:
    """#386 r2-2: MCP request_closeout_reports without timeout_s yields the 300 s default."""

    async with app.router.lifespan_context(app):
        bridge = bridge_for(app)
        alice = create_orchestrator_mcp(bridge)
        try:
            hub_store.check_in("bob", worker_instance_id="bob-instance")
            task = hub_store.assign_task("bob", "implementer", "Build", "Build")
            hub_store.submit_result(task.id, "bob", TaskState.COMPLETED, "Built")
            run_id = str(hub_store.get_state()["workflow"]["run_id"])
            result = await call(alice, "request_closeout_reports", run_id=run_id)
            assert _deadline_delta_s(hub_store, run_id) == pytest.approx(300, abs=1)
            assert result["deadline"] == hub_store.get_closeout_reports(run_id)["deadline"]
        finally:
            await bridge.close()


def test_closeout_stored_out_of_range_policy_refused(tmp_path: Path) -> None:
    """#386 r2-2: a stored out-of-range closeout_wait_s is refused, not applied."""

    from agent_hub.database import database, initialize_database

    for bad in (0, -5, 1801, 9999, True):
        path = Path(tmp_path) / f"bad-policy-{bad}.db"
        initialize_database(path)
        hub_store = HubStore(path)
        hub_store.initialize_workflow()
        run_id = str(hub_store.get_state()["workflow"]["run_id"])
        with database(hub_store.path) as connection:
            connection.execute(
                "UPDATE workflow SET policy_json = ? WHERE run_id = ?",
                (json.dumps({"closeout_wait_s": bad}), run_id),
            )
        with pytest.raises(ValueError, match="greater than zero and at most 1800"):
            hub_store.request_closeout_reports(run_id, "alice", "sess")


@pytest.mark.parametrize("paused", [False, True])
@pytest.mark.parametrize("expired", [False, True])
async def test_loss_rejoin_clears_reason_and_preserves_window(
    store: HubStore, frozen_clock: FrozenClock, paused: bool, expired: bool
) -> None:
    run_id = prepare(store)
    if paused:
        store.set_workflow_status(WorkflowStatus.PAUSED, "Pause", actor="alice", session=None)
    frozen_clock.advance(2)
    store.sweep(1)
    assert {m["author"]: m["reason"] for m in store.get_closeout_reports(run_id)["missing"]}[
        "bob"
    ] == "worker_lost"
    if expired:
        frozen_clock.advance(101)
    returned = store.check_in("bob", worker_instance_id="bob-returned")
    reason = "deadline_expired" if expired and not paused else "awaiting_report"
    assert {m["author"]: m["reason"] for m in store.status_summary()["closeout"]["missing"]}[
        "bob"
    ] == reason
    assert {m["author"]: m["reason"] for m in store.get_closeout_reports(run_id)["missing"]}[
        "bob"
    ] == reason
    if reason == "awaiting_report":
        pending = await store.await_assignment(returned.context_id, 0)
        assert isinstance(pending, dict) and pending["closeout_requested"]
        store.submit_closeout_report(run_id, "bob", "bob-returned", "worker", report())
    else:
        with pytest.raises(ConflictError, match="window"):
            store.submit_closeout_report(run_id, "bob", "bob-returned", "worker", report())


def test_collection_of_already_lost_worker_and_submitted_report_precedence(
    store: HubStore, frozen_clock: FrozenClock
) -> None:
    store.check_in("charlie", worker_instance_id="charlie-instance")
    frozen_clock.advance(2)
    store.sweep(1)
    run_id = prepare(store)
    assert {m["author"]: m["reason"] for m in store.get_closeout_reports(run_id)["missing"]}[
        "charlie"
    ] == "worker_lost"
    store.submit_closeout_report(run_id, "bob", "bob-instance", "worker", report())
    # Lose bob too without a sweep gap: an accepted report remains submitted.
    frozen_clock.advance(1)
    store.sweep(1)
    assert store.get_closeout_reports(run_id)["submitted"] == ["bob"]
    assert store.status_summary()["closeout"]["submitted_count"] == 1
