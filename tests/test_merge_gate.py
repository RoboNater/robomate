import json
import os
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pytest
from agent_hub.database import initialize_database
from agent_hub.mcp import create_mcp
from agent_hub.merge_gate import (
    Check,
    CiStatus,
    GhResult,
    Mergeable,
    MergeGate,
    MergeGateError,
    PrState,
    PullRequestRef,
    classify_checks,
    run_gh,
)
from agent_hub.store import HubStore

PR = "https://github.com/octo/sandbox/pull/7"
HEAD = "0123456789abcdef0123456789abcdef01234567"
PUSHED = "1111111111111111111111111111111111111111"
MAIN = "2222222222222222222222222222222222222222"
OLD_MAIN = "3333333333333333333333333333333333333333"


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
        "state,headRefOid,baseRefName,mergeable,mergeStateStatus",
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
    ids=["view-fails", "view-not-json", "view-missing-fields", "view-head-not-a-sha",
         "checks-fail", "workflows-fail", "compare-missing-behind"],
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
    assert report["checks"] == [{"name": "check-0", "bucket": "pass", "link": "https://ci.example/0"}]
    with pytest.raises(Exception, match="validation error"):
        await server.call_tool("check_merge_gate", {"pr_url": PR, "expected_head_sha": "abc"})
