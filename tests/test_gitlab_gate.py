"""Unit tests for GitLabGate adapter using recorded and mocked glab api fixtures."""

import json
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pytest
from agent_hub.gitlab_gate import (
    GitLabGate,
    GitLabGateError,
    GitLabProject,
    GlabResult,
    UnboundGitLabGate,
    check_unsupported_project_settings,
    classify_gitlab_status,
    map_detailed_merge_status,
)
from agent_hub.merge_gate import CiStatus, Mergeable, MergeGateError, PrState

FIXTURES_DIR = Path(__file__).parent / "fixtures" / "gitlab"

ORIGIN = "git@gitlab-box.local:RoboNater/robomate-glab-sandbox.git"
PROJECT = GitLabProject.from_origin(ORIGIN)
WEB = "https://gitlab-box.local/RoboNater/robomate-glab-sandbox"
MR_URL = f"{WEB}/-/merge_requests/1"
HEAD_SHA = "a7f1f5e2fb871446bd8ea71ce3deb7963ff2d751"
BASE_SHA = "0b56cda79d90ec1fbcc09c09d6d73486d2060621"
MAIN_SHA = "84388471501e9d0b03b0914d00c1cb457a55d89d"
# Sandbox `main`, recorded 2026-10-01: pipelines 5 (push) and 6 (api), one `test` job each.
SANDBOX_MAIN_SHA = "08726cbdadc5ff198a13d7a346b24c678967e3ee"


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

    def calls_to(self, pattern: str) -> list[list[str]]:
        return [call for call in self.calls if pattern in " ".join(call)]


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.now += seconds


def make_gate(
    runner: FakeGlab, project: GitLabProject = PROJECT, **kwargs: float
) -> tuple[GitLabGate, FakeClock]:
    clock = FakeClock()
    options: dict[str, float] = {"poll_interval_s": 5.0} | kwargs
    return (
        GitLabGate(project, runner=runner, clock=clock, sleep=clock.sleep, **options),
        clock,
    )


def ok(data: Any) -> GlabResult:
    return GlabResult(0, data if isinstance(data, str) else json.dumps(data), "")


def pipeline(pipeline_id: int, status: str, sha: str = HEAD_SHA) -> dict[str, Any]:
    return {
        "id": pipeline_id,
        "sha": sha,
        "status": status,
        "web_url": f"{WEB}/-/pipelines/{pipeline_id}",
    }


def job(
    name: str, status: str, pipeline_id: int | None, *, allow_failure: bool = False
) -> dict[str, Any]:
    return {
        "name": name,
        "status": status,
        "pipeline_id": pipeline_id,
        "allow_failure": allow_failure,
        "target_url": None,
    }


def mr(head_pipeline: dict[str, Any] | None = None, **fields: Any) -> dict[str, Any]:
    data: dict[str, Any] = json.loads(load_fixture("mr_clean.json"))
    data["head_pipeline"] = head_pipeline
    return data | fields


def routes(
    mr_data: dict[str, Any] | str,
    pipelines: list[Any] | list[GlabResult],
    statuses: list[Any] | list[GlabResult],
    extra: dict[str, GlabResult] | None = None,
) -> FakeGlab:
    def responses(items: list[Any]) -> GlabResult | list[GlabResult]:
        if items and all(isinstance(item, GlabResult) for item in items):
            return list(items)
        return ok(items)

    return FakeGlab(
        {
            "merge_requests/1?include_diverged_commits_count=true": ok(mr_data),
            "repository/branches/main": ok(load_fixture("branch_main.json")),
            "pipelines?sha=": responses(pipelines),
            "statuses": responses(statuses),
            **(extra or {}),
        }
    )


# --- Origin binding ---


@pytest.mark.parametrize(
    ("origin", "host", "path"),
    [
        (ORIGIN, "gitlab-box.local", "RoboNater/robomate-glab-sandbox"),
        ("gitlab-box.local:RoboNater/robomate-glab-sandbox", "gitlab-box.local",
         "RoboNater/robomate-glab-sandbox"),
        ("ssh://git@gitlab-box.local:2222/RoboNater/robomate-glab-sandbox.git",
         "gitlab-box.local", "RoboNater/robomate-glab-sandbox"),
        ("https://GitLab-Box.local/RoboNater/robomate-glab-sandbox.git", "gitlab-box.local",
         "RoboNater/robomate-glab-sandbox"),
        ("https://oauth2:secret@gitlab-box.local:443/group/sub/project/", "gitlab-box.local",
         "group/sub/project"),
    ],
)
def test_origin_binds_host_and_project(origin: str, host: str, path: str) -> None:
    project = GitLabProject.from_origin(origin)

    assert (project.host, project.path) == (host, path)
    assert project.web_url == f"https://{host}/{path}"


@pytest.mark.parametrize(
    "origin",
    [
        "http://gitlab-box.local/RoboNater/robomate-glab-sandbox.git",
        "https://gitlab-box.local:8443/RoboNater/robomate-glab-sandbox.git",
        "https://oauth2:secret@gitlab-box.local:99999/RoboNater/robomate-glab-sandbox.git",
        "git@gitlab-box.local:robomate-glab-sandbox.git",
        "git@gitlab-box.local:RoboNater/../robomate-glab-sandbox.git",
        "git@gitlab-box.local:/RoboNater/robomate-glab-sandbox.git",
        "https://gitlab-box.local/RoboNater/robomate-glab-sandbox.git?x=1",
        "https://[::1]/RoboNater/robomate-glab-sandbox.git",
        "/srv/git/robomate-glab-sandbox.git",
        "",
    ],
)
def test_origin_without_a_supported_project_is_refused(origin: str) -> None:
    with pytest.raises(GitLabGateError) as refused:
        GitLabProject.from_origin(origin)

    assert "secret" not in str(refused.value)


# --- MR URL hardening ---


@pytest.mark.parametrize(
    "url",
    [
        "https://gitlab.com/RoboNater/robomate-glab-sandbox/-/merge_requests/1",
        "https://gitlab-box.local.example/RoboNater/robomate-glab-sandbox/-/merge_requests/1",
        "https://evil.gitlab-box.local/RoboNater/robomate-glab-sandbox/-/merge_requests/1",
        "https://user@gitlab-box.local/RoboNater/robomate-glab-sandbox/-/merge_requests/1",
        "https://user:pw@gitlab-box.local/RoboNater/robomate-glab-sandbox/-/merge_requests/1",
        "https://gitlab-box.local@evil.example/RoboNater/robomate-glab-sandbox/-/merge_requests/1",
        "https://[::1]/RoboNater/robomate-glab-sandbox/-/merge_requests/1",
        "https://[fe80::1]:443/RoboNater/robomate-glab-sandbox/-/merge_requests/1",
        "https://gitlab-box.local:0/RoboNater/robomate-glab-sandbox/-/merge_requests/1",
        "https://gitlab-box.local:99999/RoboNater/robomate-glab-sandbox/-/merge_requests/1",
        "https://gitlab-box.local:https/RoboNater/robomate-glab-sandbox/-/merge_requests/1",
        "https://gitlab-box.local:8443/RoboNater/robomate-glab-sandbox/-/merge_requests/1",
        "https://gitlab-box.local:0443/RoboNater/robomate-glab-sandbox/-/merge_requests/1",
        "https://gitlab-box.local:/RoboNater/robomate-glab-sandbox/-/merge_requests/1",
        "http://gitlab-box.local/RoboNater/robomate-glab-sandbox/-/merge_requests/1",
        "HTTPS://gitlab-box.local/RoboNater/robomate-glab-sandbox/-/merge_requests/1",
        "//gitlab-box.local/RoboNater/robomate-glab-sandbox/-/merge_requests/1",
        f"{MR_URL}?view=parallel",
        f"{MR_URL}?",
        f"{MR_URL}#note_1",
        "https://gitlab-box.local/RoboNater/x/../robomate-glab-sandbox/-/merge_requests/1",
        "https://gitlab-box.local/RoboNater/./robomate-glab-sandbox/-/merge_requests/1",
        "https://gitlab-box.local/RoboNater/robomate-glab-sandbox/./-/merge_requests/1",
        "https://gitlab-box.local/RoboNater/robomate-glab-sandbox/-/merge_requests/../1",
        "https://gitlab-box.local/RoboNater%2Frobomate-glab-sandbox/-/merge_requests/1",
        "https://gitlab-box.local/RoboNater%2frobomate-glab-sandbox/-/merge_requests/1",
        "https://gitlab-box.local/RoboNater/robomate-glab-sandbox%2F-/merge_requests/1",
        "https://gitlab-box.local/RoboNater/robomate-glab-sandbox/-/merge_requests/%31",
        "https://gitlab-box.local/RoboNater\\robomate-glab-sandbox/-/merge_requests/1",
        "https://gitlab-box.local/RoboNater//robomate-glab-sandbox/-/merge_requests/1",
        f"{MR_URL}/diffs",
        f"{MR_URL}/",
        f"{MR_URL}.json",
        "https://gitlab-box.local/RoboNater/robomate-glab-scratch/-/merge_requests/1",
        "https://gitlab-box.local/RoboNater/robomate-glab/-/merge_requests/1",
        "https://gitlab-box.local/RoboNater/robomate-glab-sandbox2/-/merge_requests/1",
        "https://gitlab-box.local/robomate-glab-sandbox/-/merge_requests/1",
        "https://gitlab-box.local/RoboNater/robomate-glab-sandbox/sub/-/merge_requests/1",
        "https://gitlab-box.local/Other/RoboNater/robomate-glab-sandbox/-/merge_requests/1",
        "https://gitlab-box.local/RoboNater/robomate-glab-sandbox/merge_requests/1",
        "https://gitlab-box.local/RoboNater/robomate-glab-sandbox/-/merge_requests/0",
        "https://gitlab-box.local/RoboNater/robomate-glab-sandbox/-/merge_requests/01",
        "https://gitlab-box.local/RoboNater/robomate-glab-sandbox/-/merge_requests/1a",
        "https://gitlab-box.local/RoboNater/robomate-glab-sandbox/-/merge_requests/",
        "https://gitlab-box.local/RoboNater/robomate-glab-sandbox/-/pipelines/1",
        "https://gitlab-box.local/RoboNater/robomate-glab-sandbox/-/issues/1",
        "https://gitlab-box.local/RoboNater/robomate-glab sandbox/-/merge_requests/1",
        "https://gitlab-box.local/RoboNater/robomate-glab-sandbox/-/merge_requests/1\n--paginate",
        "https://gitlab-box.local/RoboKater/robomate-glab-sandbox/-/merge_requests/1",
        "https://gitlab-box.local/-/merge_requests/1",
        "",
    ],
)
async def test_url_outside_the_bound_project_is_refused_before_any_glab_call(
    url: str,
) -> None:
    runner = FakeGlab({})
    gate, _ = make_gate(runner)

    with pytest.raises(MergeGateError, match="refusing merge request URL"):
        await gate.check(url, HEAD_SHA)

    assert runner.calls == []


@pytest.mark.parametrize(
    ("origin", "url"),
    [
        (ORIGIN, "https://GITLAB-BOX.LOCAL/RoboNater/robomate-glab-sandbox/-/merge_requests/1"),
        (ORIGIN, "https://gitlab-box.local:443/RoboNater/robomate-glab-sandbox/-/merge_requests/1"),
        (ORIGIN, "https://gitlab-box.local/robonater/Robomate-Glab-Sandbox/-/merge_requests/1"),
        (ORIGIN, f"  {MR_URL}\n"),
        (
            "git@gitlab-box.local:group/sub/project.git",
            "https://gitlab-box.local/group/sub/project/-/merge_requests/1",
        ),
    ],
)
async def test_accepted_urls_read_the_origin_project_only(origin: str, url: str) -> None:
    project = GitLabProject.from_origin(origin)
    runner = routes(mr(pipeline(10, "success")), [pipeline(10, "success")], [])
    gate, _ = make_gate(runner, project)

    report = await gate.check(url, HEAD_SHA)

    assert report.ci == CiStatus.PASS
    assert report.pr_url == url.strip()
    encoded = project.path.replace("/", "%2F")
    for call in runner.calls:
        assert call[0] == "api"
        assert call[1].startswith(f"projects/{encoded}/")
        assert "--hostname=gitlab-box.local" in call


def test_mr_ref_names_the_iid() -> None:
    ref = PROJECT.parse_mr_url(f"{WEB}/-/merge_requests/42")

    assert (ref.iid, ref.project) == (42, PROJECT)
    assert ref.api("merge_requests/42") == [
        "api",
        "projects/RoboNater%2Frobomate-glab-sandbox/merge_requests/42",
        "--hostname=gitlab-box.local",
    ]


async def test_unbound_gate_fails_closed() -> None:
    gate = UnboundGitLabGate("the origin is not an SSH or HTTPS remote URL")

    with pytest.raises(MergeGateError, match="unavailable: the origin is not"):
        await gate.check(MR_URL, HEAD_SHA)


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


async def test_clean_green_mr_passes() -> None:
    runner = routes(
        load_fixture("mr_clean.json"),
        [pipeline(10, "success")],
        [job("test", "success", 10)] + json.loads(load_fixture("commit_statuses.json")),
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
    assert [(c.name, c.bucket) for c in report.checks] == [
        ("pipeline:10", "pass"),
        ("pipeline:10/test", "pass"),
        ("external/security-scan", "pass"),
    ]
    assert report.checks[1].link == f"{WEB}/-/pipelines/10"
    assert report.checks[2].link == "https://sec.example.com/scan/1"


async def test_pipeline_and_status_queries_enumerate_the_head_explicitly() -> None:
    runner = routes(mr(pipeline(10, "success")), [pipeline(10, "success")], [])
    gate, _ = make_gate(runner)

    await gate.check(MR_URL, HEAD_SHA)

    (pipelines_call,) = runner.calls_to("pipelines?sha=")
    assert pipelines_call == [
        "api",
        f"projects/RoboNater%2Frobomate-glab-sandbox/pipelines?sha={HEAD_SHA}&per_page=100",
        "--hostname=gitlab-box.local",
        "--paginate",
    ]
    (statuses_call,) = runner.calls_to("statuses")
    assert statuses_call == [
        "api",
        f"projects/RoboNater%2Frobomate-glab-sandbox/repository/commits/{HEAD_SHA}"
        "/statuses?per_page=100",
        "--hostname=gitlab-box.local",
        "--paginate",
    ]
    assert not any("all=" in arg for call in runner.calls for arg in call)
    assert not runner.calls_to("/jobs")


async def test_branch_and_mr_pipelines_for_one_sha_both_green_pass() -> None:
    runner = routes(
        mr(pipeline(11, "success")),
        [pipeline(12, "success"), pipeline(11, "success")],
        [job("test", "success", 11), job("test", "success", 12)],
    )
    gate, _ = make_gate(runner)

    report = await gate.check(MR_URL, HEAD_SHA)

    assert report.ci == CiStatus.PASS
    assert {c.name for c in report.checks} == {
        "pipeline:11", "pipeline:12", "pipeline:11/test", "pipeline:12/test"
    }


@pytest.mark.parametrize("head_pipeline_id", [11, 12])
@pytest.mark.parametrize("reverse", [False, True])
async def test_same_named_job_failing_in_either_pipeline_fails(
    head_pipeline_id: int, reverse: bool
) -> None:
    # Pipeline statuses stay green so only the (name, pipeline_id) key can catch it.
    statuses = [job("test", "failed", 11), job("test", "success", 12)]
    pipelines = [pipeline(11, "success"), pipeline(12, "success")]
    if reverse:
        statuses.reverse()
        pipelines.reverse()
    runner = routes(mr(pipeline(head_pipeline_id, "success")), pipelines, statuses)
    gate, clock = make_gate(runner)

    report = await gate.check(MR_URL, HEAD_SHA)

    assert report.ci == CiStatus.FAIL
    assert ("pipeline:11/test", "fail") in [(c.name, c.bucket) for c in report.checks]
    assert clock.now == 0.0


async def test_canceled_pipeline_reports_cancelled_and_polls() -> None:
    runner = routes(
        mr(pipeline(10, "canceled")),
        [pipeline(10, "canceled")],
        [job("test", "canceled", 10)],
    )
    gate, clock = make_gate(runner, poll_timeout_s=10.0)

    report = await gate.check(MR_URL, HEAD_SHA)

    assert report.ci == CiStatus.CANCELLED
    assert clock.now == 10.0


async def test_listed_pipeline_without_statuses_yet_is_pending() -> None:
    runner = routes(mr(None), [pipeline(10, "created")], [])
    gate, _ = make_gate(runner, poll_timeout_s=0.0)

    report = await gate.check(MR_URL, HEAD_SHA)

    assert report.ci == CiStatus.PENDING
    assert [(c.name, c.bucket) for c in report.checks] == [("pipeline:10", "pending")]


async def test_older_failed_pipeline_is_not_superseded_by_a_newer_green_one() -> None:
    runner = routes(
        mr(pipeline(14, "success")),
        [pipeline(14, "success"), pipeline(13, "failed")],
        [job("test", "success", 14), job("test", "failed", 13)],
    )
    gate, _ = make_gate(runner)

    report = await gate.check(MR_URL, HEAD_SHA)

    assert report.ci == CiStatus.FAIL


async def test_job_retried_in_place_reads_only_its_latest_attempt() -> None:
    # GitLab's default `all=false` returns only the retry (live check in #72's PR).
    runner = routes(
        mr(pipeline(8, "success")),
        [pipeline(8, "success")],
        [job("test", "success", 8)],
    )
    gate, _ = make_gate(runner)

    report = await gate.check(MR_URL, HEAD_SHA)

    assert report.ci == CiStatus.PASS


@pytest.mark.parametrize("branch_pipeline", [False, True])
async def test_merged_results_head_pipeline_gives_no_checks(branch_pipeline: bool) -> None:
    merge_commit = "f" * 40
    pipelines = [pipeline(20, "success")] if branch_pipeline else []
    statuses = [job("test", "success", 20)] if branch_pipeline else []
    runner = routes(
        mr(pipeline(21, "success", sha=merge_commit)),
        pipelines,
        statuses,
        {"files/.gitlab-ci.yml": ok({"file_name": ".gitlab-ci.yml"})},
    )
    gate, clock = make_gate(runner, poll_interval_s=1.0, poll_timeout_s=2.0)

    report = await gate.check(MR_URL, HEAD_SHA)

    assert report.ci == CiStatus.NO_CHECKS
    assert "pipeline:21" not in {c.name for c in report.checks}
    assert clock.now == 2.0


async def test_head_pipeline_missing_from_the_list_still_counts() -> None:
    runner = routes(mr(pipeline(10, "running")), [], [])
    gate, _ = make_gate(runner, poll_timeout_s=0.0)

    report = await gate.check(MR_URL, HEAD_SHA)

    assert report.ci == CiStatus.PENDING
    assert [c.name for c in report.checks] == ["pipeline:10"]


async def test_recorded_sandbox_main_pipelines_pass() -> None:
    head_pipeline = json.loads(load_fixture("sandbox_08726cb_pipelines.json"))[0]
    runner = routes(
        mr(head_pipeline, sha=SANDBOX_MAIN_SHA),
        [ok(load_fixture("sandbox_08726cb_pipelines.json"))],
        [ok(load_fixture("sandbox_08726cb_statuses.json"))],
    )
    gate, _ = make_gate(runner)

    report = await gate.check(MR_URL, SANDBOX_MAIN_SHA)

    assert report.ci == CiStatus.PASS
    assert [(c.name, c.bucket) for c in report.checks] == [
        ("pipeline:6", "pass"),
        ("pipeline:5", "pass"),
        ("pipeline:5/test", "pass"),
        ("pipeline:6/test", "pass"),
    ]
    assert report.checks[2].link == f"{WEB}/-/pipelines/5"


async def test_stale_mr_detects_diverged_commits_and_stops_immediately() -> None:
    stale_url = f"{WEB}/-/merge_requests/2"
    runner = FakeGlab(
        {
            "merge_requests/2?include_diverged_commits_count=true": ok(
                load_fixture("mr_stale.json")
            ),
            "repository/branches/main": ok(load_fixture("branch_main.json")),
            "pipelines?sha=": ok([]),
            "statuses": ok([job("test", "success", 11)]),
        }
    )
    gate, clock = make_gate(runner)

    report = await gate.check(stale_url, "ae902885739d1d768567744f2fb75838417d7908")

    assert report.base_behind_main is True
    assert report.ci == CiStatus.PASS
    assert report.elapsed_s == 0.0
    assert clock.now == 0.0


async def test_conflicting_mr_stops_immediately() -> None:
    conflict_url = f"{WEB}/-/merge_requests/3"
    runner = FakeGlab(
        {
            "merge_requests/3?include_diverged_commits_count=true": ok(
                load_fixture("mr_conflict.json")
            ),
            "repository/branches/main": ok(load_fixture("branch_main.json")),
            "pipelines?sha=": ok([]),
            "statuses": ok([]),
            "files/.gitlab-ci.yml": ok("404 File Not Found"),
            "api projects/RoboNater%2Frobomate-glab-sandbox --": ok(
                load_fixture("project_clean.json")
            ),
        }
    )
    gate, clock = make_gate(runner)

    report = await gate.check(conflict_url, "232ee469f8e43bc329bc3a4383a42fb876d838c9")

    assert report.mergeable == Mergeable.CONFLICTING
    assert report.base_behind_main is True
    assert report.ci == CiStatus.NO_WORKFLOWS
    assert report.elapsed_s == 0.0
    assert clock.now == 0.0


async def test_merged_mr_reports_state_merged() -> None:
    runner = routes(load_fixture("mr_merged.json"), [pipeline(10, "success")], [])
    gate, clock = make_gate(runner)

    report = await gate.check(MR_URL, HEAD_SHA)

    assert report.pr_state == PrState.MERGED
    assert report.elapsed_s == 0.0


async def test_head_mismatch_stops_immediately() -> None:
    runner = routes(load_fixture("mr_clean.json"), [pipeline(10, "success")], [])
    gate, clock = make_gate(runner)

    report = await gate.check(MR_URL, "ffffffffffffffffffffffffffffffffffffffff")

    assert report.head_matches is False
    assert report.elapsed_s == 0.0
    assert clock.now == 0.0


async def test_pending_ci_polls_until_settled() -> None:
    runner = routes(
        mr(pipeline(10, "running")),
        [ok([pipeline(10, "running")]), ok([pipeline(10, "success")])],
        [ok([job("test", "running", 10)]), ok([job("test", "success", 10)])],
    )
    gate, clock = make_gate(runner)

    report = await gate.check(MR_URL, HEAD_SHA)

    # The listed pipeline wins over the MR's copy of head_pipeline, still "running".
    assert report.ci == CiStatus.PASS
    assert clock.now == 5.0
    assert report.elapsed_s == 5.0


async def test_failed_ci_stops_immediately() -> None:
    runner = routes(
        mr(pipeline(10, "failed")), [pipeline(10, "failed")], [job("test", "failed", 10)]
    )
    gate, clock = make_gate(runner)

    report = await gate.check(MR_URL, HEAD_SHA)

    assert report.ci == CiStatus.FAIL
    assert clock.now == 0.0


async def test_no_ci_workflows_classified_correctly() -> None:
    runner = routes(
        mr(None),
        [],
        [],
        {
            "files/.gitlab-ci.yml": GlabResult(
                0, '{"message": "404 File Not Found"}', "glab: 404 File Not Found (HTTP 404)"
            ),
            "api projects/RoboNater%2Frobomate-glab-sandbox --": ok(
                load_fixture("project_clean.json")
            ),
        },
    )
    gate, clock = make_gate(runner)

    report = await gate.check(MR_URL, HEAD_SHA)

    assert report.ci == CiStatus.NO_WORKFLOWS
    assert report.elapsed_s == 0.0


async def test_error_handling_invalid_sha_reported() -> None:
    runner = FakeGlab(
        {
            "merge_requests/1?include_diverged_commits_count=true": ok(
                mr(None, sha="not-a-valid-sha")
            ),
        }
    )
    gate, clock = make_gate(runner)

    with pytest.raises(GitLabGateError, match="not a commit SHA"):
        await gate.check(MR_URL, HEAD_SHA)


async def test_jobs_with_allow_failure_and_manual_pass() -> None:
    runner = routes(
        mr(pipeline(10, "success")),
        [pipeline(10, "success")],
        [
            job("lint", "success", 10),
            job("flaky_test", "failed", 10, allow_failure=True),
            job("deploy_staging", "manual", 10, allow_failure=True),
        ],
    )
    gate, _ = make_gate(runner)

    report = await gate.check(MR_URL, HEAD_SHA)

    assert report.ci == CiStatus.PASS
    buckets = {c.name: c.bucket for c in report.checks}
    assert buckets == {
        "pipeline:10": "pass",
        "pipeline:10/lint": "pass",
        "pipeline:10/flaky_test": "skipping",
        "pipeline:10/deploy_staging": "skipping",
    }


async def test_external_status_without_a_pipeline_is_its_own_check() -> None:
    runner = routes(
        mr(pipeline(10, "success")),
        [pipeline(10, "success")],
        [job("test", "success", 10), job("test", "failed", None)],
    )
    gate, _ = make_gate(runner)

    report = await gate.check(MR_URL, HEAD_SHA)

    assert report.ci == CiStatus.FAIL
    assert ("test", "fail") in [(c.name, c.bucket) for c in report.checks]


async def test_paginate_concatenated_json_arrays() -> None:
    # glab api --paginate concatenates arrays back to back
    concatenated_pages = json.dumps([job("job1", "success", 10)]) + json.dumps(
        [job("job2", "success", 10)]
    )
    runner = routes(
        mr(pipeline(10, "success")), [pipeline(10, "success")], [ok(concatenated_pages)]
    )
    gate, _ = make_gate(runner)

    report = await gate.check(MR_URL, HEAD_SHA)

    assert report.ci == CiStatus.PASS
    assert [c.name for c in report.checks] == [
        "pipeline:10", "pipeline:10/job1", "pipeline:10/job2"
    ]


async def test_json_list_fails_closed_on_http_error() -> None:
    error = GlabResult(
        0, '{"message": "500 Internal Server Error"}', "glab: 500 Internal Server Error (HTTP 500)"
    )
    runner = routes(mr(pipeline(10, "success")), [error], [])
    gate, _ = make_gate(runner)

    with pytest.raises(GitLabGateError, match="HTTP 500"):
        await gate.check(MR_URL, HEAD_SHA)


async def test_has_ci_config_fails_closed_on_500() -> None:
    runner = routes(
        mr(None),
        [],
        [],
        {
            "files/.gitlab-ci.yml": GlabResult(
                0,
                '{"message": "500 Internal Server Error"}',
                "glab: 500 Internal Server Error (HTTP 500)",
            ),
        },
    )
    gate, _ = make_gate(runner)

    with pytest.raises(GitLabGateError, match="HTTP 500"):
        await gate.check(MR_URL, HEAD_SHA)


async def test_paginate_rejects_non_list_element() -> None:
    runner = routes(
        mr(pipeline(10, "success")),
        [ok('[{"id": 1, "status": "success"}]{"message": "error"}')],
        [],
    )
    gate, _ = make_gate(runner)

    with pytest.raises(GitLabGateError, match="printed invalid JSON"):
        await gate.check(MR_URL, HEAD_SHA)
