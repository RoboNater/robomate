"""Unit tests for GitLabGate adapter using recorded and mocked glab api fixtures."""

import json
from collections.abc import Sequence
from pathlib import Path

import pytest
from agent_hub.gitlab_gate import (
    GitLabGate,
    GitLabGateError,
    GlabResult,
    MergeRequestRef,
    check_unsupported_project_settings,
    classify_gitlab_status,
    map_detailed_merge_status,
)
from agent_hub.merge_gate import CiStatus, Mergeable, PrState

FIXTURES_DIR = Path(__file__).parent / "fixtures" / "gitlab"

MR_URL = "https://gitlab-box.local/RoboNater/robomate-glab-sandbox/-/merge_requests/1"
HEAD_SHA = "a7f1f5e2fb871446bd8ea71ce3deb7963ff2d751"
BASE_SHA = "0b56cda79d90ec1fbcc09c09d6d73486d2060621"
MAIN_SHA = "84388471501e9d0b03b0914d00c1cb457a55d89d"


def load_fixture(name: str) -> str:
    return (FIXTURES_DIR / name).read_text(encoding="utf-8")


class FakeGlab:
    """Mock runner that serves fixture responses keyed by API path pattern."""

    def __init__(self, routes: dict[str, GlabResult | list[GlabResult]]) -> None:
        self.routes = routes
        self.calls: list[list[str]] = []

    async def __call__(self, args: Sequence[str]) -> GlabResult:
        self.calls.append(list(args))
        cmd = " ".join(args)
        for pattern, response in self.routes.items():
            if pattern in cmd:
                if isinstance(response, list):
                    return response.pop(0) if len(response) > 1 else response[0]
                return response
        raise AssertionError(f"Unexpected glab call: {args}")


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.now += seconds


def make_gate(runner: FakeGlab) -> tuple[GitLabGate, FakeClock]:
    clock = FakeClock()
    return (
        GitLabGate(runner=runner, clock=clock, sleep=clock.sleep, poll_interval_s=5.0),
        clock,
    )


# --- URL Parsing Tests ---


def test_mr_url_parsing_various_formats() -> None:
    cases = [
        (
            "https://gitlab-box.local/RoboNater/robomate-glab-sandbox/-/merge_requests/1",
            "gitlab-box.local",
            "RoboNater/robomate-glab-sandbox",
            1,
        ),
        (
            "https://gitlab.com/group/subgroup/project/-/merge_requests/42",
            "gitlab.com",
            "group/subgroup/project",
            42,
        ),
        (
            "https://gitlab.example.com:8443/team/project/-/merge_requests/99",
            "gitlab.example.com:8443",
            "team/project",
            99,
        ),
        (
            "https://gitlab.example.com/team/project/merge_requests/12",
            "gitlab.example.com",
            "team/project",
            12,
        ),
    ]
    for url, host, project, iid in cases:
        ref = MergeRequestRef.parse(url)
        assert ref.host == host
        assert ref.project == project
        assert ref.iid == iid
        assert ref.hostname == host.split(":")[0]


def test_invalid_mr_url_raises_value_error() -> None:
    with pytest.raises(ValueError, match="not a GitLab merge request URL"):
        MergeRequestRef.parse("https://gitlab.com/not/a/merge/request")


# --- Status & Preflight Mapping Tests ---


def test_classify_gitlab_status() -> None:
    assert classify_gitlab_status("success") == "pass"
    assert classify_gitlab_status("failed") == "fail"
    assert classify_gitlab_status("failed", allow_failure=True) == "skipping"
    assert classify_gitlab_status("canceled") == "cancel"
    assert classify_gitlab_status("skipped") == "skipping"
    assert classify_gitlab_status("manual") == "fail"
    assert classify_gitlab_status("manual", allow_failure=True) == "skipping"
    assert classify_gitlab_status("running") == "pending"
    assert classify_gitlab_status("pending") == "pending"


def test_map_detailed_merge_status() -> None:
    assert map_detailed_merge_status("mergeable", False) == (Mergeable.CLEAN, "mergeable")
    assert map_detailed_merge_status("conflict", True) == (Mergeable.CONFLICTING, "conflict")
    assert map_detailed_merge_status("need_rebase", False) == (Mergeable.CONFLICTING, "need_rebase")
    assert map_detailed_merge_status("preparing", False) == (Mergeable.UNKNOWN, "preparing")
    assert map_detailed_merge_status("discussions_not_resolved", False) == (
        Mergeable.CLEAN,
        "discussions_not_resolved",
    )
    assert map_detailed_merge_status("not_approved", False) == (
        Mergeable.CLEAN,
        "not_approved",
    )
    assert map_detailed_merge_status("draft_status", False) == (
        Mergeable.CLEAN,
        "draft_status",
    )
    assert map_detailed_merge_status("blocked_status", False) == (
        Mergeable.CLEAN,
        "blocked_status",
    )


def test_preflight_checks_unsupported_settings() -> None:
    clean = json.loads(load_fixture("project_clean.json"))
    assert check_unsupported_project_settings(clean) == []

    unsupported = json.loads(load_fixture("project_unsupported.json"))
    errors = check_unsupported_project_settings(unsupported)
    assert len(errors) == 2
    assert any("automatic_rebase_enabled" in err for err in errors)
    assert any("merge trains" in err for err in errors)


# --- Gate Check Tests ---


@pytest.mark.asyncio
async def test_clean_green_mr_passes() -> None:
    runner = FakeGlab(
        {
            "merge_requests/1?include_diverged_commits_count=true": GlabResult(
                0, load_fixture("mr_clean.json"), ""
            ),
            "repository/branches/main": GlabResult(
                0, load_fixture("branch_main.json"), ""
            ),
            "pipelines/10/jobs": GlabResult(
                0, load_fixture("pipeline_jobs_success.json"), ""
            ),
            "statuses": GlabResult(0, load_fixture("commit_statuses.json"), ""),
        }
    )
    gate, clock = make_gate(runner)

    report = await gate.check(MR_URL, HEAD_SHA)

    assert report.pr_state == PrState.OPEN
    assert report.head_matches is True
    assert report.current_head_sha == HEAD_SHA
    assert report.ci == CiStatus.PASS
    assert report.base_behind_main is False
    assert report.mergeable == Mergeable.CLEAN
    assert report.merge_state_status == "mergeable"
    assert report.elapsed_s == 0.0
    assert clock.now == 0.0

    jobs_calls = [c for c in runner.calls if "pipelines/10/jobs" in " ".join(c)]
    assert len(jobs_calls) == 1
    assert "--paginate" in jobs_calls[0]
    status_calls = [c for c in runner.calls if "statuses" in " ".join(c)]
    assert len(status_calls) == 1
    assert "--paginate" in status_calls[0]


@pytest.mark.asyncio
async def test_stale_mr_detects_diverged_commits_and_stops_immediately() -> None:
    stale_url = (
        "https://gitlab-box.local/RoboNater/robomate-glab-sandbox/-/merge_requests/2"
    )
    runner = FakeGlab(
        {
            "merge_requests/2?include_diverged_commits_count=true": GlabResult(
                0, load_fixture("mr_stale.json"), ""
            ),
            "repository/branches/main": GlabResult(
                0, load_fixture("branch_main.json"), ""
            ),
            "pipelines/11/jobs": GlabResult(
                0, load_fixture("pipeline_jobs_success.json"), ""
            ),
            "statuses": GlabResult(0, "[]", ""),
        }
    )
    gate, clock = make_gate(runner)

    report = await gate.check(stale_url, "ae902885739d1d768567744f2fb75838417d7908")

    assert report.base_behind_main is True
    assert report.ci == CiStatus.PASS
    assert report.elapsed_s == 0.0
    assert clock.now == 0.0


@pytest.mark.asyncio
async def test_conflicting_mr_stops_immediately() -> None:
    conflict_url = (
        "https://gitlab-box.local/RoboNater/robomate-glab-sandbox/-/merge_requests/3"
    )
    runner = FakeGlab(
        {
            "merge_requests/3?include_diverged_commits_count=true": GlabResult(
                0, load_fixture("mr_conflict.json"), ""
            ),
            "repository/branches/main": GlabResult(
                0, load_fixture("branch_main.json"), ""
            ),
            "statuses": GlabResult(0, "[]", ""),
            "files/.gitlab-ci.yml": GlabResult(0, "404 File Not Found", ""),
            "api projects/RoboNater%2Frobomate-glab-sandbox --": GlabResult(
                0, load_fixture("project_clean.json"), ""
            ),
        }
    )
    gate, clock = make_gate(runner)

    report = await gate.check(conflict_url, "232ee469f8e43bc329bc3a4383a42fb876d838c9")

    assert report.mergeable == Mergeable.CONFLICTING
    assert report.base_behind_main is True
    assert report.elapsed_s == 0.0
    assert clock.now == 0.0


@pytest.mark.asyncio
async def test_merged_mr_reports_state_merged() -> None:
    runner = FakeGlab(
        {
            "merge_requests/1?include_diverged_commits_count=true": GlabResult(
                0, load_fixture("mr_merged.json"), ""
            ),
            "repository/branches/main": GlabResult(
                0, load_fixture("branch_main.json"), ""
            ),
            "pipelines/10/jobs": GlabResult(
                0, load_fixture("pipeline_jobs_success.json"), ""
            ),
            "statuses": GlabResult(0, "[]", ""),
        }
    )
    gate, clock = make_gate(runner)

    report = await gate.check(MR_URL, HEAD_SHA)

    assert report.pr_state == PrState.MERGED
    assert report.elapsed_s == 0.0


@pytest.mark.asyncio
async def test_head_mismatch_stops_immediately() -> None:
    runner = FakeGlab(
        {
            "merge_requests/1?include_diverged_commits_count=true": GlabResult(
                0, load_fixture("mr_clean.json"), ""
            ),
            "repository/branches/main": GlabResult(
                0, load_fixture("branch_main.json"), ""
            ),
            "pipelines/10/jobs": GlabResult(
                0, load_fixture("pipeline_jobs_success.json"), ""
            ),
            "statuses": GlabResult(0, "[]", ""),
        }
    )
    gate, clock = make_gate(runner)

    report = await gate.check(MR_URL, "ffffffffffffffffffffffffffffffffffffffff")

    assert report.head_matches is False
    assert report.elapsed_s == 0.0
    assert clock.now == 0.0


@pytest.mark.asyncio
async def test_pending_ci_polls_until_settled() -> None:
    runner = FakeGlab(
        {
            "merge_requests/1?include_diverged_commits_count=true": GlabResult(
                0, load_fixture("mr_clean.json"), ""
            ),
            "repository/branches/main": GlabResult(
                0, load_fixture("branch_main.json"), ""
            ),
            "pipelines/10/jobs": [
                GlabResult(0, load_fixture("pipeline_jobs_pending.json"), ""),
                GlabResult(0, load_fixture("pipeline_jobs_success.json"), ""),
            ],
            "statuses": GlabResult(0, "[]", ""),
        }
    )
    gate, clock = make_gate(runner)

    report = await gate.check(MR_URL, HEAD_SHA)

    assert report.ci == CiStatus.PASS
    assert clock.now == 5.0
    assert report.elapsed_s == 5.0


@pytest.mark.asyncio
async def test_failed_ci_stops_immediately() -> None:
    runner = FakeGlab(
        {
            "merge_requests/1?include_diverged_commits_count=true": GlabResult(
                0, load_fixture("mr_clean.json"), ""
            ),
            "repository/branches/main": GlabResult(
                0, load_fixture("branch_main.json"), ""
            ),
            "pipelines/10/jobs": GlabResult(
                0, load_fixture("pipeline_jobs_failed.json"), ""
            ),
            "statuses": GlabResult(0, "[]", ""),
        }
    )
    gate, clock = make_gate(runner)

    report = await gate.check(MR_URL, HEAD_SHA)

    assert report.ci == CiStatus.FAIL
    assert clock.now == 0.0


@pytest.mark.asyncio
async def test_no_ci_workflows_classified_correctly() -> None:
    mr_no_pipeline = json.loads(load_fixture("mr_clean.json"))
    mr_no_pipeline["head_pipeline"] = None

    runner = FakeGlab(
        {
            "merge_requests/1?include_diverged_commits_count=true": GlabResult(
                0, json.dumps(mr_no_pipeline), ""
            ),
            "repository/branches/main": GlabResult(
                0, load_fixture("branch_main.json"), ""
            ),
            "statuses": GlabResult(0, "[]", ""),
            "files/.gitlab-ci.yml": GlabResult(
                0, '{"message": "404 File Not Found"}', "glab: 404 File Not Found (HTTP 404)"
            ),
            "api projects/RoboNater%2Frobomate-glab-sandbox --": GlabResult(
                0, load_fixture("project_clean.json"), ""
            ),
        }
    )
    gate, clock = make_gate(runner)

    report = await gate.check(MR_URL, HEAD_SHA)

    assert report.ci == CiStatus.NO_WORKFLOWS
    assert report.elapsed_s == 0.0


@pytest.mark.asyncio
async def test_error_handling_invalid_sha_reported() -> None:
    mr_bad_sha = json.loads(load_fixture("mr_clean.json"))
    mr_bad_sha["sha"] = "not-a-valid-sha"

    runner = FakeGlab(
        {
            "merge_requests/1?include_diverged_commits_count=true": GlabResult(
                0, json.dumps(mr_bad_sha), ""
            ),
        }
    )
    gate, clock = make_gate(runner)

    with pytest.raises(GitLabGateError, match="not a commit SHA"):
        await gate.check(MR_URL, HEAD_SHA)


@pytest.mark.asyncio
async def test_jobs_with_allow_failure_and_manual_pass() -> None:
    jobs = [
        {
            "id": 1,
            "name": "lint",
            "status": "success",
            "allow_failure": False,
            "web_url": "https://gitlab-box.local/jobs/1",
        },
        {
            "id": 2,
            "name": "flaky_test",
            "status": "failed",
            "allow_failure": True,
            "web_url": "https://gitlab-box.local/jobs/2",
        },
        {
            "id": 3,
            "name": "deploy_staging",
            "status": "manual",
            "allow_failure": True,
            "web_url": "https://gitlab-box.local/jobs/3",
        },
    ]
    runner = FakeGlab(
        {
            "merge_requests/1?include_diverged_commits_count=true": GlabResult(
                0, load_fixture("mr_clean.json"), ""
            ),
            "repository/branches/main": GlabResult(
                0, load_fixture("branch_main.json"), ""
            ),
            "pipelines/10/jobs": GlabResult(0, json.dumps(jobs), ""),
            "statuses": GlabResult(0, "[]", ""),
        }
    )
    gate, _ = make_gate(runner)

    report = await gate.check(MR_URL, HEAD_SHA)

    assert report.ci == CiStatus.PASS
    assert len(report.checks) == 3
    buckets = {c.name: c.bucket for c in report.checks}
    assert buckets["lint"] == "pass"
    assert buckets["flaky_test"] == "skipping"
    assert buckets["deploy_staging"] == "skipping"


@pytest.mark.asyncio
async def test_deduplicate_pipeline_jobs_and_commit_statuses() -> None:
    jobs = [
        {
            "id": 1,
            "name": "test_job",
            "status": "success",
            "allow_failure": False,
            "web_url": "https://gitlab-box.local/jobs/1",
        },
    ]
    # Commit statuses returns the pipeline job again plus an external check
    statuses = [
        {
            "id": 1,
            "name": "test_job",
            "status": "success",
            "allow_failure": False,
            "target_url": "https://gitlab-box.local/jobs/1",
        },
        {
            "id": 2,
            "name": "external_ci",
            "status": "success",
            "allow_failure": False,
            "target_url": "https://ci.external.com/build/2",
        },
    ]
    runner = FakeGlab(
        {
            "merge_requests/1?include_diverged_commits_count=true": GlabResult(
                0, load_fixture("mr_clean.json"), ""
            ),
            "repository/branches/main": GlabResult(
                0, load_fixture("branch_main.json"), ""
            ),
            "pipelines/10/jobs": GlabResult(0, json.dumps(jobs), ""),
            "statuses": GlabResult(0, json.dumps(statuses), ""),
        }
    )
    gate, _ = make_gate(runner)

    report = await gate.check(MR_URL, HEAD_SHA)

    assert report.ci == CiStatus.PASS
    assert len(report.checks) == 2
    check_names = [c.name for c in report.checks]
    assert check_names == ["test_job", "external_ci"]


@pytest.mark.asyncio
async def test_stale_pipeline_sha_reports_no_checks() -> None:
    mr_data = json.loads(load_fixture("mr_clean.json"))
    # Set head_pipeline.sha to an older commit
    mr_data["head_pipeline"]["sha"] = "0000000000000000000000000000000000000000"

    runner = FakeGlab(
        {
            "merge_requests/1?include_diverged_commits_count=true": GlabResult(
                0, json.dumps(mr_data), ""
            ),
            "repository/branches/main": GlabResult(
                0, load_fixture("branch_main.json"), ""
            ),
            "statuses": GlabResult(0, "[]", ""),
            "files/.gitlab-ci.yml": GlabResult(0, '{"file_name": ".gitlab-ci.yml"}', ""),
        }
    )
    # Use small timeout to observe poll timeout when NO_CHECKS
    clock = FakeClock()
    gate = GitLabGate(
        runner=runner,
        clock=clock,
        sleep=clock.sleep,
        poll_interval_s=1.0,
        poll_timeout_s=2.0,
    )

    report = await gate.check(MR_URL, HEAD_SHA)

    assert report.ci == CiStatus.NO_CHECKS


@pytest.mark.asyncio
async def test_json_list_fails_closed_on_http_error() -> None:
    runner = FakeGlab(
        {
            "merge_requests/1?include_diverged_commits_count=true": GlabResult(
                0, load_fixture("mr_clean.json"), ""
            ),
            "repository/branches/main": GlabResult(
                0, load_fixture("branch_main.json"), ""
            ),
            "pipelines/10/jobs": GlabResult(
                0,
                '{"message": "500 Internal Server Error"}',
                "glab: 500 Internal Server Error (HTTP 500)",
            ),
            "statuses": GlabResult(0, "[]", ""),
        }
    )
    gate, _ = make_gate(runner)

    with pytest.raises(GitLabGateError, match="HTTP 500"):
        await gate.check(MR_URL, HEAD_SHA)


@pytest.mark.asyncio
async def test_has_ci_config_fails_closed_on_500() -> None:
    mr_no_pipeline = json.loads(load_fixture("mr_clean.json"))
    mr_no_pipeline["head_pipeline"] = None

    runner = FakeGlab(
        {
            "merge_requests/1?include_diverged_commits_count=true": GlabResult(
                0, json.dumps(mr_no_pipeline), ""
            ),
            "repository/branches/main": GlabResult(
                0, load_fixture("branch_main.json"), ""
            ),
            "statuses": GlabResult(0, "[]", ""),
            "files/.gitlab-ci.yml": GlabResult(
                0,
                '{"message": "500 Internal Server Error"}',
                "glab: 500 Internal Server Error (HTTP 500)",
            ),
        }
    )
    gate, _ = make_gate(runner)

    with pytest.raises(GitLabGateError, match="HTTP 500"):
        await gate.check(MR_URL, HEAD_SHA)
