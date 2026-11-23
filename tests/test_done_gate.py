"""Final delivery is based on fresh forge data and the run's typed records."""

import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, cast

import pytest
from agent_hub.database import database, initialize_database
from agent_hub.merge_gate import CiStatus, GateReport, Mergeable, MergeGateError, PrState
from agent_hub.orchestrator import Caller, OrchestratorOps
from agent_hub.store import ConflictError, HubStore
from agent_hub_common import TaskState, WorkflowPolicy, WorkflowStatus
from timing import FrozenClock

HEAD = "a" * 40
NEXT = "b" * 40
MERGED = "c" * 40


@pytest.fixture
def store(tmp_path: Path, frozen_clock: FrozenClock) -> HubStore:
    path = tmp_path / "hub.db"
    initialize_database(path)
    result = HubStore(path)
    frozen_clock.install(result)
    return result


@pytest.fixture(
    params=[
        "https://github.com/octo/sandbox/pull/7",
        "https://gitlab.example/octo/nested/sandbox/-/merge_requests/7",
    ],
    ids=["github", "gitlab"],
)
def pr_url(request: pytest.FixtureRequest) -> str:
    return str(request.param)


@dataclass
class FakeGate:
    report: GateReport
    calls: int = 0
    during_read: Callable[[], None] | None = None
    unavailable: bool = False

    async def check(self, pr_url: str, expected_head_sha: str) -> GateReport:
        self.calls += 1
        if self.during_read:
            self.during_read()
        if self.unavailable:
            raise MergeGateError("forge unavailable")
        return self.report


def reading(pr_url: str, *, head: str = HEAD, state: PrState = PrState.MERGED) -> GateReport:
    return GateReport(
        pr_url,
        state,
        head,
        head,
        True,
        CiStatus.PASS,
        [],
        "main",
        NEXT,
        NEXT,
        False,
        Mergeable.CLEAN,
        "clean",
        merged_sha=MERGED if state == PrState.MERGED else None,
    )


def approved_run(
    store: HubStore,
    pr_url: str,
    policy: dict[str, object] | None = None,
) -> tuple[str, str]:
    wid = store.start_workflow("Open a PR and complete the change", policy)
    store.check_in("bob")
    store.check_in("charlie")
    implementation(store, pr_url)
    approve(store, pr_url)
    return wid, store.get_workflow(wid)["run_id"]


def implementation(store: HubStore, url: str, head: str = HEAD) -> None:
    task = store.assign_task("bob", "implementer", "Implement", "Implement the change")
    store.submit_result(
        task.id,
        "bob",
        {
            "outcome": "completed",
            "pr_url": url,
            "head_sha": head,
            "summary": "Pushed",
        },
    )


def approve(store: HubStore, url: str, head: str = HEAD) -> None:
    task = store.assign_task("charlie", "reviewer", "Review", "Review the change", pr_head_sha=head)
    store.submit_result(
        task.id,
        "charlie",
        {
            "verdict": "approved",
            "pr_url": url,
            "reviewed_head_sha": head,
            "summary": "Approved",
        },
    )


def close_out(store: HubStore, outcome: str = "completed") -> None:
    task = store.assign_task("bob", "closeout", "CLOSE-OUT", "Close out")
    if outcome == "legacy":
        store.submit_result(task.id, "bob", TaskState.COMPLETED)
    else:
        store.submit_result(
            task.id,
            "bob",
            {
                "outcome": outcome,
                "summary": "Closed out",
                "items": [],
                "steps": [],
                **({"blocker": "Forge unavailable"} if outcome == "failed" else {}),
            },
        )


def done(store: HubStore, gate: FakeGate, run_id: str) -> Awaitable[dict[str, bool]]:
    return OrchestratorOps(store, gate, caller=Caller("alice", None)).set_workflow_status(
        WorkflowStatus.DONE, "Delivered", run_id
    )


def manifest(store: HubStore, run_id: str) -> dict[str, Any]:
    path = store.path.parent / "runs" / run_id / "manifest.json"
    return cast(dict[str, Any], json.loads(path.read_text(encoding="utf-8")))


async def test_default_done_reads_merge_and_preserves_both_shas(
    store: HubStore, pr_url: str
) -> None:
    wid, run_id = approved_run(store, pr_url)
    close_out(store)
    gate = FakeGate(reading(pr_url))
    # The operator read the merge directly on the forge. No gate reading has
    # been recorded by Alice (#553).
    with database(store.path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM gate_reading").fetchone()[0] == 0

    assert await done(store, gate, run_id) == {"ok": True}

    assert gate.calls == 1
    assert store.get_workflow(wid)["status"] == "done"
    assert manifest(store, run_id)["approved_sha"] == HEAD
    assert manifest(store, run_id)["merged_sha"] == MERGED


@pytest.mark.parametrize("state", [PrState.OPEN, PrState.CLOSED])
async def test_default_refuses_unmerged_even_with_cached_merge(
    store: HubStore, pr_url: str, state: PrState
) -> None:
    wid, run_id = approved_run(store, pr_url, {"closeout": False})
    store.record_gate_reading(pr_url, HEAD, report=reading(pr_url), elapsed_s=0)
    gate = FakeGate(reading(pr_url, state=state))
    with pytest.raises(ConflictError, match="requires a merged PR/MR"):
        await done(store, gate, run_id)
    assert gate.calls == 1
    assert store.get_workflow(wid)["status"] == "active"


@pytest.mark.parametrize("outcome", ["missing", "failed", "legacy"])
async def test_done_requires_typed_closeout(store: HubStore, pr_url: str, outcome: str) -> None:
    _, run_id = approved_run(store, pr_url)
    if outcome != "missing":
        close_out(store, outcome)
    gate = FakeGate(reading(pr_url))
    with pytest.raises(ConflictError, match="completed typed CloseoutResult"):
        await done(store, gate, run_id)


@pytest.mark.parametrize("failure", ["url", "expected", "actual", "flag", "missing_sha", "forge"])
async def test_done_fails_closed(store: HubStore, pr_url: str, failure: str | None) -> None:
    _, run_id = approved_run(store, pr_url, {"closeout": False})
    changes: dict[Any, dict[str, Any]] = {
        "url": {"pr_url": pr_url[:-1] + "8"},
        "expected": {"expected_head_sha": NEXT},
        "actual": {"current_head_sha": NEXT},
        "flag": {"head_matches": False},
        "missing_sha": {"merged_sha": None},
        "forge": {},
    }
    gate = FakeGate(replace(reading(pr_url), **changes[failure]), unavailable=failure == "forge")
    with pytest.raises((ConflictError, MergeGateError)):
        await done(store, gate, run_id)
    assert store.get_workflow(store.get_state()["workflow"]["id"])["status"] == "active"


@pytest.mark.parametrize("failure", ["merged", "closed", "ci", "no_ci", "stale", "conflict", None])
async def test_pr_delivery_requires_open_approved_clean_head(
    store: HubStore, pr_url: str, failure: str | None
) -> None:
    wid, run_id = approved_run(store, pr_url, {"deliver": "pr", "closeout": False})
    store = HubStore(store.path, clock=store.clock)
    assert store.get_state()["workflow"]["policy"]["deliver"] == "pr"
    changes: dict[Any, dict[str, Any]] = {
        "merged": {"pr_state": PrState.MERGED, "merged_sha": MERGED},
        "closed": {"pr_state": PrState.CLOSED},
        "ci": {"ci": CiStatus.FAIL},
        "no_ci": {"ci": CiStatus.NO_WORKFLOWS},
        "stale": {"base_behind_main": True},
        "conflict": {"mergeable": Mergeable.CONFLICTING},
        None: {},
    }
    gate = FakeGate(replace(reading(pr_url, state=PrState.OPEN), **changes[failure]))
    if failure:
        with pytest.raises(ConflictError, match="deliver=pr"):
            await done(store, gate, run_id)
        assert store.get_workflow(wid)["status"] == "active"
    else:
        await done(store, gate, run_id)
        assert manifest(store, run_id)["merged_sha"] is None


async def test_pr_delivery_honors_explicit_no_ci_policy(store: HubStore, pr_url: str) -> None:
    _, run_id = approved_run(
        store,
        pr_url,
        {
            "deliver": "pr",
            "closeout": False,
            "allow_no_ci": True,
        },
    )
    gate = FakeGate(replace(reading(pr_url, state=PrState.OPEN), ci=CiStatus.NO_WORKFLOWS))
    assert await done(store, gate, run_id) == {"ok": True}


@pytest.mark.parametrize(
    "role,agent", [("implementer", "bob"), ("reviewer", "charlie"), ("rebase", "bob")]
)
def test_conflicting_pr_does_not_finish_task(
    store: HubStore, pr_url: str, role: str, agent: str
) -> None:
    approved_run(store, pr_url)
    task = store.assign_task(agent, role, "More work", "Use the run PR", pr_head_sha=HEAD)
    result = {
        "summary": "Published",
        "pr_url": pr_url[:-1] + "8",
        **(
            {"verdict": "approved", "reviewed_head_sha": HEAD}
            if role == "reviewer"
            else {"outcome": "completed", "head_sha": NEXT}
        ),
    }
    with pytest.raises(ConflictError, match="different PR/MR"):
        store.submit_result(task.id, agent, result)
    worker = store.agent_by_name(agent)
    assert worker is not None and worker.current_task_id == task.id


async def test_done_rechecks_after_concurrent_new_approval(store: HubStore, pr_url: str) -> None:
    wid, run_id = approved_run(store, pr_url, {"closeout": False})

    def move_head() -> None:
        implementation(store, pr_url, NEXT)
        approve(store, pr_url, NEXT)

    with pytest.raises(ConflictError, match="binding changed"):
        await done(store, FakeGate(reading(pr_url), during_read=move_head), run_id)
    assert store.get_workflow(wid)["status"] == "active"
    with database(store.path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM gate_reading").fetchone()[0] == 0


async def test_done_replay_cannot_advance_queued_run(store: HubStore, pr_url: str) -> None:
    _, run_id = approved_run(store, pr_url, {"closeout": False})
    queued_id = store.start_workflow("Next run")
    gate = FakeGate(reading(pr_url))
    await done(store, gate, run_id)
    assert store.get_state()["workflow"]["id"] == queued_id
    assert await done(store, gate, run_id) == {"ok": True}
    assert store.get_workflow(queued_id)["status"] == "active"
    assert gate.calls == 1


async def test_clean_rebase_carries_approval_to_delivered_head(
    store: HubStore, pr_url: str
) -> None:
    _, run_id = approved_run(store, pr_url, {"closeout": False})
    task = store.assign_task("bob", "rebase", "Rebase", "Update base", pr_head_sha=HEAD)
    store.submit_result(
        task.id,
        "bob",
        {
            "pr_url": pr_url,
            "outcome": "completed",
            "head_sha": NEXT,
            "summary": "Clean rebase",
        },
    )
    await done(store, FakeGate(reading(pr_url, head=NEXT)), run_id)
    assert manifest(store, run_id)["approved_sha"] == NEXT


def test_wrong_closeout_role_and_post_merge_implementer_refused(
    store: HubStore, pr_url: str
) -> None:
    approved_run(store, pr_url)
    with pytest.raises(ConflictError, match='requires role="closeout"'):
        store.assign_task("bob", "implementer", "CLOSE-OUT for PR", "Close out")
    accepted = store.assign_task("bob", "implementer", "Follow up", "Continue work")
    store.set_task_state(accepted.id, TaskState.CANCELED, "No changes needed")
    store.record_gate_reading(pr_url, HEAD, report=reading(pr_url), elapsed_s=0)
    with pytest.raises(ConflictError, match=f'{MERGED}.*role="closeout"'):
        store.assign_task("bob", "implementer", "Follow up", "Close out")
    close_out(store)


def test_delivery_defaults_to_merge() -> None:
    assert WorkflowPolicy().deliver == "merge"
    with pytest.raises(ValueError, match="deliver"):
        WorkflowPolicy.model_validate({"deliver": "skip"})
