import asyncio
import json
import os
import sqlite3
import sys
from collections.abc import Sequence
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from agent_hub.database import initialize_database
from agent_hub.gitlab_gate import run_glab
from agent_hub.mcp import create_mcp
from agent_hub.merge_gate import (
    Check,
    CiStatus,
    GhResult,
    GitHubProject,
    Mergeable,
    MergeGate,
    MergeGateError,
    PrState,
    PullRequestRef,
    UnboundGitHubGate,
    classify_checks,
    run_gh,
)
from agent_hub.orchestrator import OrchestratorOps
from agent_hub.store import HubStore
from agent_hub.sweeper import CI_WATCH_INTERVAL_S, poll_ci_watches
from agent_hub_common import EventKind, WorkflowPolicy, to_iso
from conftest import MonotonicClock
from delivery_support import finish_run
from timing import LIVENESS_S, FrozenClock

PR = "https://github.com/octo/sandbox/pull/7"
HEAD = "0123456789abcdef0123456789abcdef01234567"
PUSHED = "1111111111111111111111111111111111111111"
MAIN = "2222222222222222222222222222222222222222"
OLD_MAIN = "3333333333333333333333333333333333333333"


async def test_orchestrator_records_github_reading_once_per_call(store: HubStore) -> None:
    gh = FakeGh(checks=[checks("pending"), checks("pass")])
    merge_gate, _ = gate(gh)
    ops = OrchestratorOps(store, merge_gate)
    first = await ops.check_merge_gate(PR + "/", HEAD.upper())
    second = await ops.check_merge_gate(PR, HEAD)
    # Internal polls do not create additional readings, and no call accounting
    # needs to be enabled to record the final facts.
    assert gh.count("view") == 3
    with sqlite3.connect(store.path) as connection:
        connection.row_factory = sqlite3.Row
        rows = connection.execute("SELECT * FROM gate_reading ORDER BY id").fetchall()
        assert connection.execute("SELECT count(*) FROM call_log").fetchone()[0] == 0
    assert len(rows) == 2
    for row, result in zip(rows, (first, second), strict=True):
        assert row["ts"]
        assert row["workflow_id"] == store.get_state()["workflow"]["id"]
        assert row["error_code"] is None
        for key, value in result.items():
            if key == "checks":
                assert json.loads(row["checks_json"]) == [
                    {"name": c["name"], "bucket": c["bucket"]} for c in value
                ]
            else:
                assert row[key] == value
    assert rows[0]["elapsed_s"] == 10.0


async def test_github_failure_is_recorded_and_still_raised(store: HubStore) -> None:
    merge_gate, _ = gate(FakeGh(view=GhResult(1, "", "HTTP 500 secret external text")))
    with pytest.raises(MergeGateError, match="HTTP 500"):
        await OrchestratorOps(store, merge_gate).check_merge_gate(PR, HEAD)
    with sqlite3.connect(store.path) as connection:
        connection.row_factory = sqlite3.Row
        [row] = connection.execute("SELECT * FROM gate_reading").fetchall()
    assert row["error_code"] == -32004
    assert row["pr_url"] == PR and row["expected_head_sha"] == HEAD
    assert row["current_head_sha"] is None and row["ci"] is None
    assert row["elapsed_s"] >= 0
    assert json.loads(row["checks_json"]) == []
    assert "secret external text" not in str(dict(row))


def view(
    head: str = HEAD,
    mergeable: str = "MERGEABLE",
    state: str = "CLEAN",
    base: str = "main",
    pr_state: str = "OPEN",
) -> GhResult:
    body = {
        "state": pr_state,
        "headRefOid": head,
        "baseRefName": base,
        "mergeable": mergeable,
        "mergeStateStatus": state,
    }
    return GhResult(0, json.dumps(body), "")


def checks(*buckets: str) -> GhResult:
    body = [
        {"name": f"check-{i}", "bucket": bucket, "link": f"https://ci.example/{i}"}
        for i, bucket in enumerate(buckets)
    ]
    return GhResult(0, json.dumps(body), "")


# What gh 2.96.0 prints for a head no workflow run has reported on yet.
NO_CHECKS = GhResult(1, "", "no checks reported on the 'issue-7' branch\n")


def workflows(count: int) -> GhResult:
    return GhResult(0, f"{count}\n", "")


def compare(behind: int = 0, merge_base: str = MAIN, tip: str = MAIN) -> GhResult:
    body = {"behind_by": behind, "base_commit": tip, "merge_base_commit": merge_base}
    return GhResult(0, json.dumps(body), "")


BEHIND = compare(behind=3, merge_base=OLD_MAIN)
# A current, clean, green PR whose repository has CI.
DEFAULT_VIEW, DEFAULT_CHECKS = view(), checks("pass")
DEFAULT_WORKFLOWS, DEFAULT_COMPARE = workflows(1), compare()


class FakeGh:
    """Serve fixture output per gh call; a list is consumed one poll at a time."""

    def __init__(
        self,
        *,
        view: GhResult | list[GhResult] | None = None,
        checks: GhResult | list[GhResult] | None = None,
        workflows: GhResult | list[GhResult] | None = None,
        compare: GhResult | list[GhResult] | None = None,
    ) -> None:
        self.responses = {
            "view": view or DEFAULT_VIEW,
            "checks": checks or DEFAULT_CHECKS,
            "workflows": workflows or DEFAULT_WORKFLOWS,
            "compare": compare or DEFAULT_COMPARE,
        }
        self.calls: list[list[str]] = []

    def count(self, kind: str) -> int:
        return sum(1 for args in self.calls if _kind(args) == kind)

    async def __call__(self, args: Sequence[str]) -> GhResult:
        self.calls.append(list(args))
        response = self.responses[_kind(args)]
        if isinstance(response, list):
            return response.pop(0) if len(response) > 1 else response[0]
        return response


def _kind(args: Sequence[str]) -> str:
    if args[:2] == ["pr", "view"]:
        return "view"
    if args[:2] == ["pr", "checks"]:
        return "checks"
    if args[0] == "api" and args[2].endswith("/actions/workflows"):
        return "workflows"
    if args[0] == "api" and "/compare/" in args[2]:
        return "compare"
    raise AssertionError(f"unexpected gh call: {args}")


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.now += seconds


def gate(gh: FakeGh) -> tuple[MergeGate, FakeClock]:
    clock = FakeClock()
    return MergeGate(runner=gh, clock=clock, sleep=clock.sleep), clock


async def test_pending_gate_watch_emits_one_event_when_ci_settles(
    store: HubStore, frozen_clock: FrozenClock
) -> None:
    gh = FakeGh(checks=checks("pending"))
    merge_gate = MergeGate(runner=gh, poll_timeout_s=0)
    ops = OrchestratorOps(store, merge_gate)
    pending = await ops.check_merge_gate(PR, HEAD)

    assert pending["ci"] == "pending"
    [watch] = store.active_ci_watches()
    assert (watch.pr_url, watch.head_sha) == (PR, HEAD)
    assert store.next_event() is None

    gh.responses["checks"] = checks("pass")
    frozen_clock.advance(CI_WATCH_INTERVAL_S)
    await poll_ci_watches(store, merge_gate)
    event = store.next_event()
    assert event is not None and event.kind is EventKind.CI_SETTLED
    assert event.payload == {
        "workflow_id": store.get_state()["workflow"]["id"],
        "pr_url": PR,
        "head_sha": HEAD,
        "ci": "pass",
        "checks": [{"name": "check-0", "bucket": "pass", "link": "https://ci.example/0"}],
    }
    assert store.active_ci_watches() == []
    await ops.check_merge_gate(PR, HEAD)
    await poll_ci_watches(store, merge_gate)
    assert store.next_event() is None


async def test_replacing_watch_drops_in_flight_old_head_result(
    store: HubStore, frozen_clock: FrozenClock
) -> None:
    gh = FakeGh(checks=checks("pending"))
    merge_gate = MergeGate(runner=gh, poll_timeout_s=0)
    ops = OrchestratorOps(store, merge_gate)
    await ops.check_merge_gate(PR, HEAD)
    [old_watch] = store.active_ci_watches()
    gh.responses["checks"] = checks("pass")
    old_reading = await merge_gate.check(PR, HEAD, wait=False)

    gh.responses.update(view=view(PUSHED), checks=checks("pending"))
    await ops.check_merge_gate(PR, PUSHED)
    store.record_ci_watch_poll(old_watch, old_reading)

    [watch] = store.active_ci_watches()
    assert watch.head_sha == PUSHED
    assert store.next_event() is None
    gh.responses["checks"] = checks("fail")
    frozen_clock.advance(CI_WATCH_INTERVAL_S)
    await poll_ci_watches(store, merge_gate)
    event = store.next_event()
    assert event is not None and event.kind is EventKind.CI_SETTLED
    assert (event.payload["head_sha"], event.payload["ci"]) == (PUSHED, "fail")
    assert store.next_event() is None


async def test_watch_discovers_pushed_head_before_reporting_settlement(
    store: HubStore, frozen_clock: FrozenClock
) -> None:
    gh = FakeGh(checks=checks("pending"))
    merge_gate = MergeGate(runner=gh, poll_timeout_s=0)
    await OrchestratorOps(store, merge_gate).check_merge_gate(PR, HEAD)
    gh.responses["view"] = view(PUSHED)

    frozen_clock.advance(CI_WATCH_INTERVAL_S)
    await poll_ci_watches(store, merge_gate)
    [watch] = store.active_ci_watches()
    assert watch.head_sha == PUSHED
    assert store.next_event() is None

    gh.responses["checks"] = checks("pass")
    frozen_clock.advance(CI_WATCH_INTERVAL_S)
    await poll_ci_watches(store, merge_gate)
    event = store.next_event()
    assert event is not None and event.kind is EventKind.CI_SETTLED
    assert event.payload["head_sha"] == PUSHED
    assert store.next_event() is None


async def test_initial_unknown_mergeability_emits_already_settled_ci_once(
    store: HubStore,
) -> None:
    gh = FakeGh(view=view(mergeable="UNKNOWN", state="UNKNOWN"))
    merge_gate = MergeGate(runner=gh, poll_timeout_s=0)
    ops = OrchestratorOps(store, merge_gate)
    await ops.check_merge_gate(PR, HEAD)

    event = store.next_event()
    assert event is not None and event.kind is EventKind.CI_SETTLED
    assert event.payload["ci"] == "pass"
    assert len(store.active_ci_watches()) == 1

    await ops.check_merge_gate(PR, HEAD)
    gh.responses["view"] = view()
    await poll_ci_watches(store, merge_gate)
    assert store.active_ci_watches() == []
    assert store.next_event() is None


@pytest.mark.parametrize("terminal_state", ["CLOSED", "MERGED"])
async def test_watch_ends_when_change_request_closes(
    store: HubStore, frozen_clock: FrozenClock, terminal_state: str
) -> None:
    gh = FakeGh(checks=checks("pending"))
    merge_gate = MergeGate(runner=gh, poll_timeout_s=0)
    await OrchestratorOps(store, merge_gate).check_merge_gate(PR, HEAD)

    gh.responses.update(view=view(pr_state=terminal_state), checks=checks("pass"))
    frozen_clock.advance(CI_WATCH_INTERVAL_S)
    await poll_ci_watches(store, merge_gate)
    assert store.active_ci_watches() == []
    assert store.next_event() is None


async def test_expired_ci_watches_wake_inbox_once_without_a_forge_read(
    store: HubStore, frozen_clock: FrozenClock
) -> None:
    gh = FakeGh(checks=checks("pending"))
    merge_gate = MergeGate(runner=gh, poll_timeout_s=0)
    ops = OrchestratorOps(store, merge_gate)
    await ops.check_merge_gate(PR, HEAD)
    await ops.check_merge_gate(PR.replace("/7", "/8"), HEAD)
    first, second = store.active_ci_watches()

    assert first.deadline is not None and first.deadline == second.deadline
    frozen_clock.advance((first.deadline - frozen_clock()).total_seconds())
    await poll_ci_watches(store, merge_gate)
    event = store.next_event()
    assert event is not None and event.kind is EventKind.CI_WATCH_EXPIRED
    assert event.payload["head_sha"] == HEAD
    assert gh.count("view") == 2
    assert store.active_ci_watches() == []
    assert store.next_event() is None


@pytest.mark.parametrize(
    ("next_pr", "next_head"),
    [(PR, HEAD), (PR, PUSHED), (PR.replace("/7", "/8"), PUSHED)],
    ids=["same-head", "new-head", "another-pr"],
)
async def test_gate_after_wall_expiry_rearms_without_repeating_expiry(
    store: HubStore, frozen_clock: FrozenClock, next_pr: str, next_head: str
) -> None:
    gh = FakeGh(checks=checks("pending"))
    merge_gate = MergeGate(runner=gh, poll_timeout_s=0)
    frozen_clock.advance(WorkflowPolicy().max_wall_minutes * 60 + 1)
    await OrchestratorOps(store, merge_gate).check_merge_gate(PR, HEAD)
    expired = store.next_event()
    assert expired is not None and expired.kind is EventKind.CI_WATCH_EXPIRED
    assert store.active_ci_watches() == []

    # Resumption can happen after a restart. The expiry's scope is the run.
    restarted = HubStore(store.path, detect_host_pauses=False)
    frozen_clock.install(restarted)
    gh.responses["view"] = view(next_head)
    await OrchestratorOps(restarted, merge_gate).check_merge_gate(next_pr, next_head)
    watches = restarted.active_ci_watches()
    assert len(watches) == 1
    assert watches[0].deadline is None
    assert restarted.next_event() is None

    gh.responses["checks"] = checks("pass")
    frozen_clock.advance(CI_WATCH_INTERVAL_S)
    await poll_ci_watches(restarted, merge_gate)
    event = restarted.next_event()
    assert event is not None and event.kind is EventKind.CI_SETTLED
    assert (event.payload["pr_url"], event.payload["head_sha"]) == (next_pr, next_head)
    assert restarted.next_event() is None


@pytest.mark.parametrize("activation", ["auto", "start"])
async def test_ci_watch_wall_time_starts_when_queued_work_activates(
    store: HubStore, frozen_clock: FrozenClock, activation: str
) -> None:
    policy = {"max_wall_minutes": 1}
    if activation == "auto":
        store.start_workflow("Queued work", policy)
        frozen_clock.advance(120)
        finish_run(store, "previous run done", actor="alice", session=None)
    else:
        finish_run(store, "previous run done", actor="alice", session=None)
        store.start_workflow("Queued work", policy, force_queued=True)
        frozen_clock.advance(120)
        store.start_workflow()

    gh = FakeGh(checks=checks("pending"))
    merge_gate = MergeGate(runner=gh, poll_timeout_s=0)
    await OrchestratorOps(store, merge_gate).check_merge_gate(PR, HEAD)
    watches = store.active_ci_watches()
    assert len(watches) == 1
    assert watches[0].deadline is not None
    assert to_iso(watches[0].deadline) == to_iso(frozen_clock() + timedelta(minutes=1))
    assert store.next_event() is None


async def test_done_workflow_discards_watch_and_late_poll(store: HubStore) -> None:
    gh = FakeGh(checks=checks("pending"))
    merge_gate = MergeGate(runner=gh, poll_timeout_s=0)
    ops = OrchestratorOps(store, merge_gate)
    await ops.check_merge_gate(PR, HEAD)
    [watch] = store.active_ci_watches()

    finish_run(store, "done", actor="alice", session=None)
    gh.responses["checks"] = checks("pass")
    report = await merge_gate.check(PR, HEAD, wait=False)
    store.record_ci_watch_poll(watch, report)
    assert store.active_ci_watches() == []
    assert store.next_event() is None


# -- the four #41 fixture cases ----------------------------------------------


async def test_a_current_clean_green_pr_passes_every_field_at_once() -> None:
    gh = FakeGh()
    merge_gate, clock = gate(gh)

    report = await merge_gate.check(PR, HEAD)

    assert report.head_matches and report.current_head_sha == HEAD
    assert report.ci is CiStatus.PASS
    assert report.base_behind_main is False
    assert (report.base_ref, report.base_sha, report.main_sha) == ("main", MAIN, MAIN)
    assert report.mergeable is Mergeable.CLEAN
    assert report.elapsed_s == 0 and clock.now == 0
    assert gh.count("view") == 1


async def test_a_base_behind_main_is_reported_even_when_it_merges_cleanly() -> None:
    # The #23 trial: approved, green, no conflict — and still not what a
    # reviewer read, because main moved underneath it.
    gh = FakeGh(compare=BEHIND)
    merge_gate, _ = gate(gh)

    report = await merge_gate.check(PR, HEAD)

    assert report.base_behind_main is True
    assert (report.base_sha, report.main_sha) == (OLD_MAIN, MAIN)
    assert report.mergeable is Mergeable.CLEAN
    assert report.ci is CiStatus.PASS


async def test_a_base_behind_main_that_conflicts_is_reported_as_conflicting() -> None:
    gh = FakeGh(view=view(mergeable="CONFLICTING", state="DIRTY"), compare=BEHIND)
    merge_gate, _ = gate(gh)

    report = await merge_gate.check(PR, HEAD)

    assert report.base_behind_main is True
    assert report.mergeable is Mergeable.CONFLICTING
    assert report.merge_state_status == "DIRTY"


async def test_mergeability_github_is_still_computing_is_returned_as_unknown() -> None:
    gh = FakeGh(view=view(mergeable="UNKNOWN", state="UNKNOWN"))
    merge_gate, clock = gate(gh)

    report = await merge_gate.check(PR, HEAD)

    assert report.mergeable is Mergeable.UNKNOWN
    assert report.elapsed_s == 60 and clock.now == 60


async def test_mergeability_is_re_read_until_github_has_computed_it() -> None:
    gh = FakeGh(view=[view(mergeable="UNKNOWN", state="UNKNOWN"), view()])
    merge_gate, _ = gate(gh)

    report = await merge_gate.check(PR, HEAD)

    assert report.mergeable is Mergeable.CLEAN
    assert report.elapsed_s == 10


@pytest.mark.parametrize(
    ("mergeable", "state", "expected"),
    [
        ("MERGEABLE", "CLEAN", Mergeable.CLEAN),
        # Mergeable means no conflict; branch protection and CI are other fields.
        ("MERGEABLE", "BLOCKED", Mergeable.CLEAN),
        ("MERGEABLE", "BEHIND", Mergeable.CLEAN),
        ("CONFLICTING", "DIRTY", Mergeable.CONFLICTING),
        ("UNKNOWN", "DIRTY", Mergeable.CONFLICTING),
        ("UNKNOWN", "UNKNOWN", Mergeable.UNKNOWN),
    ],
)
async def test_mergeable_is_read_from_both_github_fields(
    mergeable: str, state: str, expected: Mergeable
) -> None:
    merge_gate, _ = gate(FakeGh(view=view(mergeable=mergeable, state=state)))
    merge_gate.poll_timeout_s = 0

    assert (await merge_gate.check(PR, HEAD)).mergeable is expected


# -- the #27 CI outcomes and head binding ------------------------------------


@pytest.mark.parametrize(
    ("fixture", "count", "expected", "waited"),
    [
        (checks("pass", "skipping"), 1, CiStatus.PASS, 0),
        (checks("pass", "fail", "pending"), 1, CiStatus.FAIL, 0),
        (checks("pass", "pending"), 1, CiStatus.PENDING, 60),
        (checks("pass", "cancel"), 1, CiStatus.CANCELLED, 60),
        (NO_CHECKS, 1, CiStatus.NO_CHECKS, 60),
        (NO_CHECKS, 0, CiStatus.NO_WORKFLOWS, 0),
    ],
    ids=["pass", "fail", "pending", "cancelled", "no_checks", "no_workflows"],
)
async def test_each_ci_outcome(
    fixture: GhResult, count: int, expected: CiStatus, waited: float
) -> None:
    gh = FakeGh(checks=fixture, workflows=workflows(count))
    merge_gate, _ = gate(gh)

    report = await merge_gate.check(PR, HEAD)

    assert report.ci is expected
    assert report.elapsed_s == waited
    assert gh.count("workflows") == (gh.count("checks") if fixture is NO_CHECKS else 0)


@pytest.mark.parametrize("fixture", [NO_CHECKS, checks("cancel")], ids=["no_checks", "cancelled"])
async def test_polling_stops_at_sixty_seconds(fixture: GhResult) -> None:
    gh = FakeGh(checks=fixture)
    merge_gate, clock = gate(gh)

    await merge_gate.check(PR, HEAD)

    assert clock.now == 60
    # Reads at 0, 10, ... 60 s: one every `--watch` interval, then give up.
    assert gh.count("checks") == 7


async def test_a_superseding_run_replaces_a_cancelled_one() -> None:
    gh = FakeGh(checks=[checks("cancel"), NO_CHECKS, checks("pending"), checks("pass")])
    merge_gate, _ = gate(gh)

    report = await merge_gate.check(PR, HEAD)

    assert report.ci is CiStatus.PASS
    assert report.elapsed_s == 30
    assert [c.bucket for c in report.checks] == ["pass"]


@pytest.mark.parametrize(
    ("reported", "expected"),
    [("MERGED", PrState.MERGED), ("CLOSED", PrState.CLOSED), ("LOCKED", PrState.CLOSED)],
)
async def test_a_pr_that_is_no_longer_open_is_reported_without_waiting(
    reported: str, expected: PrState
) -> None:
    """A merged PR answers a redelivered MERGE event: do not merge it twice (#50).

    GitHub also leaves `mergeable` UNKNOWN on such a PR forever, so waiting on
    it would burn the whole polling window for nothing.
    """

    gh = FakeGh(view=view(mergeable="UNKNOWN", state="UNKNOWN", pr_state=reported))
    merge_gate, clock = gate(gh)

    report = await merge_gate.check(PR, HEAD)

    assert report.pr_state is expected
    assert clock.now == 0


async def test_an_open_pr_is_reported_as_open() -> None:
    report = await gate(FakeGh())[0].check(PR, HEAD)

    assert report.pr_state is PrState.OPEN


async def test_a_head_that_moved_after_approval_fails_without_waiting() -> None:
    gh = FakeGh(view=view(head=PUSHED), checks=checks("pending"))
    merge_gate, clock = gate(gh)

    report = await merge_gate.check(PR, HEAD)

    assert report.head_matches is False
    assert report.current_head_sha == PUSHED
    assert clock.now == 0


async def test_the_expected_head_matches_regardless_of_case() -> None:
    merge_gate, _ = gate(FakeGh(view=view(head=HEAD.upper())))

    report = await merge_gate.check(PR, HEAD)

    assert report.head_matches and report.current_head_sha == HEAD


async def test_a_stale_base_or_conflict_needs_no_ci_wait() -> None:
    for gh in (
        FakeGh(checks=checks("pending"), compare=BEHIND),
        FakeGh(checks=checks("pending"), view=view(mergeable="CONFLICTING", state="DIRTY")),
    ):
        merge_gate, clock = gate(gh)
        report = await merge_gate.check(PR, HEAD)
        assert report.ci is CiStatus.PENDING
        assert clock.now == 0


async def test_a_base_that_moves_while_ci_runs_is_seen_by_the_last_read() -> None:
    gh = FakeGh(checks=[checks("pending"), checks("pass")], compare=[compare(), BEHIND])
    merge_gate, _ = gate(gh)

    report = await merge_gate.check(PR, HEAD)

    assert report.ci is CiStatus.PASS
    assert report.base_behind_main is True


def test_check_buckets_reduce_to_one_verdict() -> None:
    def ci(*buckets: str) -> CiStatus | None:
        return classify_checks([Check(name=b, bucket=b, link="") for b in buckets])

    assert ci() is None
    assert ci("skipping") is CiStatus.PASS
    assert ci("fail", "cancel") is CiStatus.FAIL
    assert ci("cancel", "pending") is CiStatus.CANCELLED
    # A bucket this code does not know is never read as green.
    assert ci("pass", "neutral") is CiStatus.PENDING


async def test_a_check_list_printed_with_a_failing_exit_is_still_read() -> None:
    printed = checks("fail")
    gh = FakeGh(checks=GhResult(1, printed.stdout, ""))
    merge_gate, _ = gate(gh)

    assert (await merge_gate.check(PR, HEAD)).ci is CiStatus.FAIL


# -- calls, inputs and failures ----------------------------------------------


async def test_gh_is_called_with_the_pr_url_and_a_bounded_compare() -> None:
    gh = FakeGh(view=view(base="release/1.x"), checks=NO_CHECKS, workflows=workflows(0))
    merge_gate, _ = gate(gh)

    await merge_gate.check(PR + "/", HEAD)

    assert gh.calls[0] == [
        "pr",
        "view",
        PR,
        "--json",
        "state,headRefOid,baseRefName,mergeable,mergeStateStatus,mergeCommit",
    ]
    assert gh.calls[1] == ["pr", "checks", PR, "--json", "name,bucket,link"]
    assert gh.calls[2][:3] == [
        "api",
        "--hostname=github.com",
        "repos/octo/sandbox/actions/workflows",
    ]
    assert gh.calls[3][:3] == [
        "api",
        "--hostname=github.com",
        f"repos/octo/sandbox/compare/release/1.x...{HEAD}",
    ]
    # The compare response lists every commit and file; only three fields are read.
    assert gh.calls[3][3] == "--jq"


@pytest.mark.parametrize(
    "url",
    [
        "--repo=evil",
        "https://github.com/octo/sandbox/issues/7",
        "https://github.com/octo/sandbox/pull/7/files",
        "https://github.com/octo/sandbox/pull/0",
        "http://github.com/octo/sandbox/pull/7",
        PR + "\n--flag",
        "https://github.com/octo/../pull/7",
        "https://github.com/octo/./pull/7",
        "https://github.com/-octo/sandbox/pull/7",
        "https://-github.com/octo/sandbox/pull/7",
    ],
)
async def test_only_a_pr_url_reaches_gh(url: str) -> None:
    gh = FakeGh()
    merge_gate, _ = gate(gh)

    with pytest.raises(ValueError, match="pull request URL"):
        await merge_gate.check(url, HEAD)

    assert gh.calls == []


@pytest.mark.parametrize("sha", ["abc", HEAD + "\n", "z" * 40])
async def test_a_malformed_expected_head_is_refused(sha: str) -> None:
    gh = FakeGh()
    merge_gate, _ = gate(gh)

    with pytest.raises(ValueError, match="expected_head_sha"):
        await merge_gate.check(PR, sha)

    assert gh.calls == []


def test_a_pr_ref_names_its_repository() -> None:
    ref = PullRequestRef.parse("https://ghe.example.com/team/repo.name/pull/12/")

    assert (ref.host, ref.owner, ref.repo) == ("ghe.example.com", "team", "repo.name")
    assert ref.number == 12
    assert PullRequestRef.parse("https://github.com/octo/.github/pull/1").repo == ".github"
    assert ref.url == "https://ghe.example.com/team/repo.name/pull/12"


# -- GitHub origin binding (#77) ----------------------------------------------

ORIGIN = "git@github.com:octo/sandbox.git"
PROJECT = GitHubProject.from_origin(ORIGIN)


def bound_gate(gh: FakeGh, project: GitHubProject = PROJECT) -> tuple[MergeGate, FakeClock]:
    clock = FakeClock()
    return MergeGate(runner=gh, clock=clock, sleep=clock.sleep, project=project), clock


@pytest.mark.parametrize(
    ("origin", "host", "owner", "repo"),
    [
        (ORIGIN, "github.com", "octo", "sandbox"),
        ("github.com:octo/sandbox", "github.com", "octo", "sandbox"),
        ("ssh://git@github.com:22/octo/sandbox.git", "github.com", "octo", "sandbox"),
        ("https://GitHub.COM/octo/sandbox.git", "github.com", "octo", "sandbox"),
        (
            "https://oauth2:secret@github.com:443/octo/sandbox/",
            "github.com",
            "octo",
            "sandbox",
        ),
        (
            "git@ghe.example.com:team/repo.name.git",
            "ghe.example.com",
            "team",
            "repo.name",
        ),
    ],
)
def test_github_origin_binds_host_and_repository(
    origin: str, host: str, owner: str, repo: str
) -> None:
    project = GitHubProject.from_origin(origin)

    assert (project.host, project.owner, project.repo) == (host, owner, repo)
    assert project.web_url == f"https://{host}/{owner}/{repo}"


@pytest.mark.parametrize(
    "origin",
    [
        "http://github.com/octo/sandbox.git",
        "https://github.com:8443/octo/sandbox.git",
        "https://oauth2:secret@github.com:99999/octo/sandbox.git",
        "git@github.com:sandbox.git",
        "git@github.com:octo/../sandbox.git",
        "git@github.com:/octo/sandbox.git",
        "https://github.com/octo/sandbox.git?x=1",
        "https://[::1]/octo/sandbox.git",
        "https://[::1/octo/sandbox.git",
        "ssh://git@[github.com]/octo/sandbox.git",
        "ssh://git@github.com:port/octo/sandbox.git",
        "/srv/git/sandbox.git",
        "",
    ],
)
def test_github_origin_without_a_supported_repository_is_refused(origin: str) -> None:
    with pytest.raises(MergeGateError) as refused:
        GitHubProject.from_origin(origin)

    assert "secret" not in str(refused.value)


@pytest.mark.parametrize(
    "url",
    [
        "https://github.com.other/octo/sandbox/pull/7",
        "https://evil.github.com/octo/sandbox/pull/7",
        "https://user@github.com/octo/sandbox/pull/7",
        "https://user:pw@github.com/octo/sandbox/pull/7",
        "https://github.com@evil.example/octo/sandbox/pull/7",
        "https://[::1]/octo/sandbox/pull/7",
        "https://github.com:0/octo/sandbox/pull/7",
        "https://github.com:99999/octo/sandbox/pull/7",
        "https://github.com:https/octo/sandbox/pull/7",
        "https://github.com:8443/octo/sandbox/pull/7",
        "https://github.com:0443/octo/sandbox/pull/7",
        "https://github.com:/octo/sandbox/pull/7",
        "http://github.com/octo/sandbox/pull/7",
        "HTTPS://github.com/octo/sandbox/pull/7",
        "//github.com/octo/sandbox/pull/7",
        f"{PR}?view=parallel",
        f"{PR}?",
        f"{PR}#issuecomment-1",
        "https://github.com/octo/x/../sandbox/pull/7",
        "https://github.com/octo/./sandbox/pull/7",
        "https://github.com/octo/sandbox/./pull/7",
        "https://github.com/octo/sandbox/pull/../7",
        "https://github.com/octo%2Fsandbox/pull/7",
        "https://github.com/octo/sandbox%2F/pull/7",
        "https://github.com/octo/sandbox/pull/%37",
        "https://github.com/octo\\sandbox/pull/7",
        "https://github.com/octo//sandbox/pull/7",
        f"{PR}/diffs",
        f"{PR}.json",
        "https://github.com/octo/sandbox2/pull/7",
        "https://github.com/octo/sand/pull/7",
        "https://github.com/other/sandbox/pull/7",
        "https://github.com/octo/sandbox/pull/0",
        "https://github.com/octo/sandbox/pull/01",
        "https://github.com/octo/sandbox/pull/1a",
        "https://github.com/octo/sandbox/pull/",
        "https://github.com/octo/sandbox/issues/7",
        "https://github.com/octo/sandbox/pull/7\n--paginate",
        "https://github.com/octo/sandbox /pull/7",
        "",
    ],
)
async def test_url_outside_the_bound_repository_is_refused_before_any_gh_call(
    url: str,
) -> None:
    gh = FakeGh()
    merge_gate, _ = bound_gate(gh)

    with pytest.raises(MergeGateError, match="refusing pull request URL"):
        await merge_gate.check(url, HEAD)

    assert gh.calls == []


@pytest.mark.parametrize(
    ("origin", "url", "number"),
    [
        (ORIGIN, "https://GITHUB.COM/octo/sandbox/pull/7", 7),
        (ORIGIN, "https://github.com:443/octo/sandbox/pull/7", 7),
        (ORIGIN, "https://github.com/OCTO/SANDBOX/pull/7", 7),
        (ORIGIN, f"  {PR}\n", 7),
        (
            "git@ghe.example.com:team/repo.name.git",
            "https://ghe.example.com/team/repo.name/pull/12",
            12,
        ),
    ],
)
async def test_accepted_urls_read_the_origin_repository_only(
    origin: str, url: str, number: int
) -> None:
    project = GitHubProject.from_origin(origin)
    gh = FakeGh()
    merge_gate, _ = bound_gate(gh, project)

    report = await merge_gate.check(url, HEAD)

    canonical = f"{project.web_url}/pull/{number}"
    assert report.ci == CiStatus.PASS
    assert report.pr_url == canonical
    for call in gh.calls:
        if call[0] == "api":
            assert call[1] == f"--hostname={project.host}"
            assert f"repos/{project.owner}/{project.repo}/" in call[2]
        elif call[:2] == ["pr", "view"] or call[:2] == ["pr", "checks"]:
            assert call[2] == canonical


async def test_unbound_github_gate_fails_closed() -> None:
    gate = UnboundGitHubGate("the origin is not an SSH or HTTPS remote URL")

    with pytest.raises(MergeGateError, match="unavailable: the origin is not"):
        await gate.check(PR, HEAD)


@pytest.mark.parametrize(
    "fake",
    [
        FakeGh(view=GhResult(1, "", "GraphQL: Could not resolve to a PullRequest\n")),
        FakeGh(view=GhResult(0, "not json", "")),
        FakeGh(view=GhResult(0, "{}", "")),
        FakeGh(view=view(head="HEAD~1")),
        FakeGh(checks=GhResult(1, "", "HTTP 401: Bad credentials\n")),
        FakeGh(checks=NO_CHECKS, workflows=GhResult(1, "", "HTTP 404: Not Found\n")),
        FakeGh(compare=GhResult(0, json.dumps({"base_commit": MAIN}), "")),
    ],
    ids=[
        "view-fails",
        "view-not-json",
        "view-missing-fields",
        "view-head-not-a-sha",
        "checks-fail",
        "workflows-fail",
        "compare-missing-behind",
    ],
)
async def test_a_gate_that_cannot_be_read_raises_rather_than_reporting(fake: FakeGh) -> None:
    merge_gate, _ = gate(fake)

    with pytest.raises(MergeGateError):
        await merge_gate.check(PR, HEAD)


async def test_a_gh_failure_names_the_call_and_its_error() -> None:
    merge_gate, _ = gate(FakeGh(checks=GhResult(1, "", "HTTP 401: Bad credentials\n")))

    with pytest.raises(MergeGateError, match=r"gh pr checks failed: HTTP 401: Bad credentials"):
        await merge_gate.check(PR, HEAD)


# -- the real runner ---------------------------------------------------------


@pytest.mark.parametrize("run", [run_gh, run_glab])
async def test_cancelled_gate_query_kills_and_reaps_its_cli(
    monkeypatch: pytest.MonkeyPatch, run: Any
) -> None:
    started = asyncio.Event()
    killed = asyncio.Event()
    reaped = asyncio.Event()

    class Process:
        async def communicate(self) -> tuple[bytes, bytes]:
            if not started.is_set():
                started.set()
                await asyncio.Event().wait()
            assert killed.is_set()
            reaped.set()
            return b"", b""

        def kill(self) -> None:
            killed.set()

    async def spawn(*args: Any, **kwargs: Any) -> Process:
        return Process()

    monkeypatch.setattr("shutil.which", lambda name: name)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    query = asyncio.create_task(run(["--version"]))
    try:
        await asyncio.wait_for(started.wait(), LIVENESS_S)
        query.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(query, LIVENESS_S)
        assert reaped.is_set()
    finally:
        query.cancel()
        await asyncio.gather(query, return_exceptions=True)


@pytest.mark.skipif(os.name == "nt", reason="the stand-in gh is a POSIX script")
async def test_run_gh_keeps_mcp_stdin_and_stdout_out_of_reach(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = tmp_path / "gh"
    fake.write_text(
        f"#!{sys.executable}\n"
        "import json, os, sys\n"
        "data = sys.stdin.read()\n"
        "print(json.dumps({'args': sys.argv[1:], 'stdin': data,"
        " 'prompt': os.environ.get('GH_PROMPT_DISABLED')}))\n"
        "print('to stderr', file=sys.stderr)\n"
        "sys.exit(3)\n",
        encoding="utf-8",
    )
    fake.chmod(0o755)
    monkeypatch.setenv("PATH", str(tmp_path))

    result = await run_gh(["pr", "view", PR])

    printed = json.loads(result.stdout)
    assert printed == {"args": ["pr", "view", PR], "stdin": "", "prompt": "1"}
    assert result.stderr.strip() == "to stderr"
    assert result.returncode == 3


async def test_run_gh_without_gh_on_path_is_a_gate_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PATH", str(tmp_path))

    with pytest.raises(MergeGateError, match="not on PATH"):
        await run_gh(["--version"])


# -- the MCP tool ------------------------------------------------------------


async def test_the_mcp_tool_reports_the_gate_as_plain_json(tmp_path: Path) -> None:
    path = tmp_path / "hub.db"
    initialize_database(path)
    merge_gate, _ = gate(FakeGh(compare=BEHIND))
    server = create_mcp(HubStore(path), gate=merge_gate)

    result = await server.call_tool("check_merge_gate", {"pr_url": PR, "expected_head_sha": HEAD})

    assert isinstance(result, tuple)
    report: dict[str, Any] = result[1]
    assert json.loads(json.dumps(report)) == report
    assert report["ci"] == "pass"
    assert report["mergeable"] == "clean"
    assert report["base_behind_main"] is True
    assert report["pr_state"] == "open"
    assert report["checks"] == [
        {"name": "check-0", "bucket": "pass", "link": "https://ci.example/0"}
    ]
    with pytest.raises(Exception, match="validation error"):
        await server.call_tool("check_merge_gate", {"pr_url": PR, "expected_head_sha": "abc"})


async def test_merged_commit_comes_from_github_not_pr_head() -> None:
    # GitHub exposes the target commit for each method through mergeCommit;
    # a rebase merge reports the last rebased commit, not the old PR head.
    body = json.loads(view(pr_state="MERGED").stdout)
    body["mergeCommit"] = {"oid": PUSHED.upper()}
    gh = FakeGh(view=GhResult(0, json.dumps(body), ""))
    merge_gate, _ = gate(gh)
    report = await merge_gate.check(PR, HEAD)
    assert report.merged_sha == PUSHED != HEAD
    assert "mergeCommit" in gh.calls[0][-1]


async def test_open_github_pr_does_not_report_merge_commit() -> None:
    body = json.loads(view().stdout)
    body["mergeCommit"] = {"oid": PUSHED}
    merge_gate, _ = gate(FakeGh(view=GhResult(0, json.dumps(body), "")))
    assert (await merge_gate.check(PR, HEAD)).merged_sha is None


@pytest.mark.parametrize("merged", [True, False])
@pytest.mark.parametrize("reported_pr_url", [PR, f"  {PR}/\n", PR.upper()])
async def test_post_merge_gate_populates_completed_run_and_manifest(
    tmp_path: Path, merged: bool, reported_pr_url: str
) -> None:
    from agent_hub.database import initialize_database
    from agent_hub.orchestrator import Caller
    from agent_hub_common import WorkflowStatus

    store = HubStore(tmp_path / "hub.db")
    initialize_database(store.path)
    store.start_workflow("Deliver", {"closeout": False, "deliver": "merge" if merged else "pr"})

    # Anchor to the first run, then advance explicitly between runs. Real
    # wall time can step backwards on WSL2 (#209); this tests merge evidence,
    # not the separate production run-ordering policy.
    clock = MonotonicClock()
    clock.start = datetime.fromisoformat(store.get_state()["workflow"]["created"])
    store.clock = clock
    store.register_worker("bob")
    task = store.assign_task("bob", "implementer", "Build", "Do work")
    store.submit_result(
        task.id,
        "bob",
        {
            "outcome": "completed",
            "summary": "built",
            "pr_url": reported_pr_url,
            "head_sha": HEAD,
        },
    )
    store.register_worker("charlie")
    review = store.assign_task("charlie", "reviewer", "Review", "Review", pr_head_sha=HEAD)
    store.submit_result(
        review.id,
        "charlie",
        {
            "verdict": "approved",
            "summary": "approved",
            "pr_url": reported_pr_url,
            "reviewed_head_sha": HEAD,
            "review_url": PR + "#issuecomment-1",
        },
    )
    run_id = store.get_state()["run_id"]
    body = json.loads(view(pr_state="MERGED" if merged else "OPEN").stdout)
    body["mergeCommit"] = {"oid": PUSHED} if merged else None
    merge_gate, _ = gate(FakeGh(view=GhResult(0, json.dumps(body), "")))
    ops = OrchestratorOps(store, merge_gate, caller=Caller("alice", None))
    reading = await ops.check_merge_gate(PR.replace("octo", "OcTo") + "/", HEAD)
    assert reading["merged_sha"] == (PUSHED if merged else None)
    # Later failed readings and unrelated merged PRs must not erase or leak
    # into the successful evidence for this run's reported PR.
    store.record_gate_reading(PR, HEAD, error_code=-32004, elapsed_s=0)
    other_body = dict(body, state="MERGED", mergeCommit={"oid": MAIN})
    other_gate, _ = gate(FakeGh(view=GhResult(0, json.dumps(other_body), "")))
    await OrchestratorOps(store, other_gate).check_merge_gate(PR.replace("/7", "/8"), HEAD)
    # Read through the gate's canonical URL at finalization as well.
    final_gate, _ = gate(FakeGh(view=GhResult(0, json.dumps(body), "")))
    await OrchestratorOps(store, final_gate, caller=Caller("alice", None)).set_workflow_status(
        WorkflowStatus.DONE, "Closed out", run_id
    )
    # Restart, then advance to the next run: both durable summaries retain it.
    store = HubStore(store.path, clock=clock)
    clock.advance(1)
    store.start_workflow("Next run", {"deliver": "pr"})
    previous = store.get_state()["previous_run"]
    assert previous["run_id"] == run_id and previous["approved_sha"] == HEAD
    assert previous["merged_sha"] == (PUSHED if merged else None)
    manifest = json.loads((store.path.parent / "runs" / run_id / "manifest.json").read_text())
    assert manifest["merged_sha"] == previous["merged_sha"]
    assert manifest["approved_sha"] == HEAD
    # A subsequent run without a merge cannot inherit the previous commit.
    finish_run(store, "No merge", actor="alice", session=None)
    clock.advance(1)
    store.start_workflow("Third run")
    assert store.get_state()["previous_run"]["merged_sha"] is None
