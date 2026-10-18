"""Injected-runner startup checks and all merge compatibility matrix cells."""

import argparse
import json
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pytest
import robomate.cli as cli
from agent_hub.forge_preflight import gitlab_policy_checks, gitlab_preflight
from agent_hub.gitlab_gate import (
    GitLabGate,
    GitLabProject,
    GlabResult,
    check_merge_compatibility,
    is_definite_ci_absence,
)
from agent_hub_common.discovery import Repository, read_hub_json

ORIGIN = "git@host:group/project.git"
BASE: dict[str, Any] = {
    "id": 1,
    "web_url": "https://host/group/project",
    "permissions": {"group_access": {"access_level": 30}},
    "auto_devops_enabled": False,
    "only_allow_merge_if_pipeline_succeeds": True,
}


class Runner:
    def __init__(self, project: dict[str, Any] | None = None, failure: str = "") -> None:
        self.project = BASE.copy() if project is None else project
        self.failure = failure
        self.calls: list[list[str]] = []

    async def __call__(self, args: Sequence[str]) -> GlabResult:
        self.calls.append(list(args))
        if self.failure == "missing":
            raise OSError("missing glab")
        if args == ["version"]:
            return GlabResult(0, "glab version 1.36.0 (build)", "")
        assert args[:3] == ["api", "--hostname", "host"]
        if args[-1] == "user":
            data = {"message": "401 Unauthorized"} if self.failure == "auth" else {"username": "u"}
        else:
            assert args[-1] == "projects/group%2Fproject"
            data = (
                {"message": "404 Project Not Found"} if self.failure == "project" else self.project
            )
        return GlabResult(0, json.dumps(data), "")


@pytest.mark.parametrize("project_method", ["merge", "rebase_merge", "ff"])
@pytest.mark.parametrize("squash", ["default_on", "default_off", "always", "never"])
@pytest.mark.parametrize("policy", ["merge", "squash", "rebase"])
def test_matrix(project_method: str, squash: str, policy: str) -> None:
    errors = check_merge_compatibility(
        {"merge_method": project_method, "squash_option": squash},
        policy,
    )
    refused = (
        policy == "rebase"
        or (squash == "always" and policy == "merge")
        or (squash == "never" and policy == "squash")
    )
    assert bool(errors) == refused


@pytest.mark.parametrize(
    "project,policy", [({}, "merge"), ({"merge_method": "other"}, "squash"), (BASE, "other")]
)
def test_unknown_matrix_settings_refuse(project: dict[str, Any], policy: str) -> None:
    assert check_merge_compatibility(project, policy)


async def test_baseline_and_case_insensitive_web_url() -> None:
    runner = Runner({**BASE, "web_url": "https://HOST/Group/PROJECT"})
    checks = await gitlab_preflight(ORIGIN, runner=runner)
    assert len(checks) == 6
    assert all(c.status == "pass" for c in checks)
    assert len(runner.calls) == 3


@pytest.mark.parametrize("failure", ["missing", "auth", "project"])
async def test_unavailable_facts_warn(failure: str) -> None:
    checks = await gitlab_preflight(ORIGIN, runner=Runner(failure=failure))
    assert len(checks) == 6
    assert any(c.status == "warn" for c in checks)
    assert all(c.status != "refuse" for c in checks)


@pytest.mark.parametrize(
    "setting",
    [
        "automatic_rebase_enabled",
        "merge_trains_enabled",
        "merge_train_enforcement",
        "merge_pipelines_enabled",
    ],
)
async def test_definite_setting_refuses(setting: str) -> None:
    checks = await gitlab_preflight(ORIGIN, runner=Runner({**BASE, setting: True}))
    refusal = next(c for c in checks if c.status == "refuse")
    assert setting in refusal.detail and "Disable" in refusal.detail


async def test_web_url_collision_refuses() -> None:
    checks = await gitlab_preflight(
        ORIGIN,
        runner=Runner(
            {
                **BASE,
                "web_url": "https://host/gitlab/group/project",
                "automatic_rebase_enabled": True,
                "auto_devops_enabled": True,
            },
        ),
    )
    refusal = next(c for c in checks if c.status == "refuse")
    assert refusal.name == "Project readable"
    assert "relative URL root" in refusal.detail and "issues/80" in refusal.detail
    assert "https://host/gitlab/group/project" in refusal.detail
    assert "https://host/group/project" in refusal.detail
    assert all(c.status == "warn" and "verified origin project" in c.detail for c in checks[3:])


async def test_advisories() -> None:
    checks = await gitlab_preflight(
        ORIGIN,
        runner=Runner(
            {**BASE, "auto_devops_enabled": True, "only_allow_merge_if_pipeline_succeeds": False},
        ),
    )
    assert [(c.name, c.status) for c in checks[-2:]] == [
        ("Auto DevOps", "warn"),
        ("Pipelines must succeed", "note"),
    ]


@pytest.mark.parametrize("level", [None, 20, 30, 40])
async def test_developer_access(level: int | None) -> None:
    checks = await gitlab_preflight(
        ORIGIN,
        runner=Runner(
            {**BASE, "permissions": {"project_access": {"access_level": level}}},
        ),
    )
    assert checks[2].status == ("pass" if level is not None and level >= 30 else "warn")


@pytest.mark.parametrize(
    "forge,override,failure,setting",
    [
        ("github", None, "", ""),
        ("unknown", None, "", ""),
        ("unknown", "github", "", ""),
        ("github", "gitlab", "", ""),
        ("gitlab", None, "missing", ""),
        ("gitlab", None, "auth", ""),
        ("gitlab", None, "project", ""),
        *[
            ("gitlab", None, "", s)
            for s in (
                "automatic_rebase_enabled",
                "merge_trains_enabled",
                "merge_train_enforcement",
                "merge_pipelines_enabled",
                "web_url",
            )
        ],
    ],
)
async def test_up_integration(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    forge: str,
    override: str | None,
    failure: str,
    setting: str,
) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    repo = Repository(root, root / ".git", ORIGIN, "main", forge)
    monkeypatch.setattr(cli, "resolve_repository", lambda *_a, **_kw: repo)
    # Never the machine's registry: no reads, no writes.
    monkeypatch.setattr(cli, "register", lambda *_a: None)
    monkeypatch.setattr(cli, "hub_entries", lambda *_a: [])
    monkeypatch.setattr(cli, "mark_stopped", lambda *_a, **_kw: None)
    monkeypatch.setattr(cli, "is_linked_worktree", lambda *_a: False)
    data = {**BASE, setting: True} if setting else BASE.copy()
    if setting == "web_url":
        data[setting] = "https://host/gitlab/group/project"
    runner = Runner(data, failure)

    async def preflight(origin: str) -> Any:
        return await gitlab_preflight(origin, runner=runner)

    served = False

    async def serve(_settings: Any, _sockets: Any, _info: Any, started: Any) -> None:
        nonlocal served
        served = True
        started()

    monkeypatch.setattr(cli, "gitlab_preflight", preflight)
    monkeypatch.setattr(cli, "serve_http", serve)
    args = argparse.Namespace(
        forge=override,
        name=None,
        port=None,
        bind="127.0.0.1",
        public_url=None,
        no_call_accounting=False,
    )
    if setting:
        with pytest.raises(RuntimeError, match="preflight refused"):
            await cli._up(args)
        assert not served and read_hub_json(root) is None
        assert setting in capsys.readouterr().out
    else:
        await cli._up(args)
        assert served
        info = read_hub_json(root)
        assert info is not None and info["forge"] == (override or forge)
    if (override or forge) in ("github", "unknown"):
        assert not runner.calls


@pytest.mark.parametrize("version", ["glab version 1.35.0", "garbage", "glab version 2.0.0"])
async def test_version_warning(version: str) -> None:
    delegate = Runner()

    async def runner(args: Sequence[str]) -> GlabResult:
        if args == ["version"]:
            return GlabResult(0, version, "")
        return await delegate(args)

    checks = await gitlab_preflight(ORIGIN, runner=runner)
    assert checks[0].status == ("pass" if "2.0.0" in version else "warn")


@pytest.mark.parametrize(
    "stdout,stderr", [("{}", "(HTTP 403)"), ("not json", ""), ("[]", ""), ('{"message":"403"}', "")]
)
async def test_unreadable_project_responses_warn(stdout: str, stderr: str) -> None:
    delegate = Runner()

    async def runner(args: Sequence[str]) -> GlabResult:
        if args[-1].startswith("projects/"):
            return GlabResult(0, stdout, stderr)
        return await delegate(args)

    checks = await gitlab_preflight(ORIGIN, runner=runner)
    assert checks[2].status == "warn"
    assert all(c.status != "refuse" for c in checks)


@pytest.mark.parametrize(
    "origin", ["http://host/group/project.git", "https://host:8443/group/project.git"]
)
async def test_unsupported_origin_refuses_without_call(origin: str) -> None:
    runner = Runner()
    checks = await gitlab_preflight(origin, runner=runner)
    assert checks[0].status == "refuse"
    assert not runner.calls


async def test_relative_root_collision() -> None:
    calls: list[list[str]] = []

    async def runner(args: Sequence[str]) -> GlabResult:
        calls.append(list(args))
        if args == ["version"]:
            return GlabResult(0, "glab version 1.36.0", "")
        if args[-1] == "user":
            return GlabResult(0, '{"username":"u"}', "")
        assert args[-1] == "projects/gitlab%2Fgroup%2Fproject"
        return GlabResult(
            0,
            json.dumps(
                {
                    **BASE,
                    "web_url": "https://host/gitlab/gitlab/group/project",
                }
            ),
            "",
        )

    checks = await gitlab_preflight("https://host/gitlab/group/project.git", runner=runner)
    assert checks[2].status == "refuse"
    assert "issues/80" in checks[2].detail
    assert len(calls) == 3


# --- Shared policy-dependent checks (#96, #93) ---

POLICY_BASE: dict[str, Any] = {
    **BASE,
    "default_branch": "main",
    "merge_method": "merge",
    "squash_option": "default_off",
    "ci_config_path": None,
}


class PolicyRunner:
    """Runner for gitlab_policy_checks: project + optional CI file."""

    def __init__(
        self,
        project: dict[str, Any] | None = None,
        *,
        ci: GlabResult | None = None,
        project_failure: str = "",
    ) -> None:
        self.project = POLICY_BASE.copy() if project is None else project
        self.ci = ci
        self.project_failure = project_failure
        self.calls: list[list[str]] = []

    async def __call__(self, args: Sequence[str]) -> GlabResult:
        self.calls.append(list(args))
        assert args[:3] == ["api", "--hostname", "host"]
        endpoint = args[-1]
        if endpoint == "projects/group%2Fproject":
            if self.project_failure == "missing":
                raise OSError("missing glab")
            if self.project_failure == "unreadable":
                return GlabResult(1, "", "500 Internal Server Error")
            return GlabResult(0, json.dumps(self.project), "")
        assert "repository/files" in endpoint, f"unexpected call: {args}"
        if self.ci is not None:
            return self.ci
        return GlabResult(0, json.dumps({"file_name": ".gitlab-ci.yml"}), "")

    def file_calls(self) -> list[list[str]]:
        return [c for c in self.calls if "repository/files" in " ".join(c)]


@pytest.mark.parametrize("allow", ["true", "false"])
async def test_policy_explicit_skips_ci_probe(allow: str) -> None:
    runner = PolicyRunner()
    checks = await gitlab_policy_checks(
        ORIGIN, merge_method="squash", allow_no_ci=allow, runner=runner
    )
    assert runner.file_calls() == []
    by_name = {c.name: c for c in checks}
    assert by_name["Developer access"].status == "pass"
    assert by_name["Merge compatibility"].status == "pass"
    assert "allows squash" in by_name["Merge compatibility"].detail
    assert by_name["CI configuration"].status == "note"
    assert f"explicit allow_no_ci={allow}" in by_name["CI configuration"].detail


async def test_policy_auto_non404_refuses_naming_check() -> None:
    runner = PolicyRunner(ci=GlabResult(1, "500 Internal Server Error", "500"))
    checks = await gitlab_policy_checks(
        ORIGIN, merge_method="squash", allow_no_ci="auto", runner=runner
    )
    refusal = next(c for c in checks if c.status == "refuse")
    assert refusal.name == "CI configuration"
    assert "CI configuration" in refusal.detail or "cannot check CI configuration" in refusal.detail
    assert "explicit --allow-no-ci" in refusal.detail


@pytest.mark.parametrize(
    "origin",
    [
        "git@host:group/release-404.git",
        "git@host404:group/project.git",
    ],
)
async def test_policy_404_in_args_is_not_no_ci(origin: str) -> None:
    """A 404 only in the host/path/ref must not read as absent (#93 r2-2)."""

    project_path = "group/release-404" if "release-404" in origin else "group/project"
    host = "host404" if "host404" in origin else "host"

    async def runner(args: Sequence[str]) -> GlabResult:
        assert args[1] == "--hostname"
        if args[-1] == f"projects/{project_path.replace('/', '%2F')}":
            data = {
                **POLICY_BASE,
                "web_url": f"https://{host}/{project_path}",
            }
            return GlabResult(0, json.dumps(data), "")
        assert "repository/files" in args[-1]
        # Non-404 failure whose command line contains 404 (host/path/ref),
        # but whose bodies do not contain a definite 404.
        return GlabResult(1, "500 Internal Server Error", "500")

    checks = await gitlab_policy_checks(
        origin, merge_method="squash", allow_no_ci="auto", runner=runner
    )
    refusal = next(c for c in checks if c.status == "refuse")
    assert refusal.name == "CI configuration"
    assert "cannot check CI configuration" in refusal.detail


def test_is_definite_ci_absence_ignores_command_line() -> None:
    assert is_definite_ci_absence('{"message":"404 File Not Found"}', "") is True
    assert is_definite_ci_absence("", "(HTTP 404)") is True
    assert is_definite_ci_absence("500", "500") is False
    # A 404 only in the args (host/path/ref) is passed as bodies without the
    # definite markers, so it must not count.
    assert is_definite_ci_absence("500 Internal Server Error", "500") is False


@pytest.mark.parametrize(
    "ci_result,project_patch,expected",
    [
        (GlabResult(0, '{"file_name": ".gitlab-ci.yml"}', ""), {}, True),
        (GlabResult(0, '{"message": "404 File Not Found"}', ""), {}, False),
        (GlabResult(1, "", "(HTTP 404)"), {}, False),
        (
            GlabResult(0, '{"message": "404 File Not Found"}', ""),
            {"ci_config_path": "custom.yml"},
            True,
        ),
        (
            GlabResult(1, "", "(HTTP 404)"),
            {"auto_devops_enabled": True},
            True,
        ),
    ],
)
async def test_ci_rule_parity_gate_and_preflight(
    ci_result: GlabResult, project_patch: dict[str, Any], expected: bool
) -> None:
    """The gate (MR head) and the preflight (default branch) agree (#96 A)."""

    project_data = {**POLICY_BASE, **project_patch}
    origin = ORIGIN
    project = GitLabProject.from_origin(origin)

    async def preflight_runner(args: Sequence[str]) -> GlabResult:
        if args[-1] == "projects/group%2Fproject":
            return GlabResult(0, json.dumps(project_data), "")
        assert "repository/files" in args[-1]
        return ci_result

    checks = await gitlab_policy_checks(
        origin, merge_method="squash", allow_no_ci="auto", runner=preflight_runner
    )
    ci_check = next(c for c in checks if c.name == "CI configuration")
    assert ci_check.status != "refuse"
    preflight_has_ci = "has CI configured" in ci_check.detail and "has no CI" not in ci_check.detail
    assert preflight_has_ci is expected

    # Gate side: same file result, same project data, MR head ref.
    ref = project.parse_mr_url("https://host/group/project/-/merge_requests/1")

    async def gate_runner(args: Sequence[str]) -> GlabResult:
        text = " ".join(args)
        if "repository/files" in text:
            return ci_result
        if "projects/group%2Fproject" in text:
            return GlabResult(0, json.dumps(project_data), "")
        raise AssertionError(f"unexpected gate call: {args}")

    gate = GitLabGate(project, runner=gate_runner)
    assert await gate._has_ci_config(ref, "main") is expected


@pytest.mark.parametrize(
    "ci_result",
    [
        GlabResult(1, "500 Internal Server Error", "500"),
        GlabResult(1, "", "glab: 500 Internal Server Error (HTTP 500)"),
    ],
)
async def test_ci_rule_parity_non404_refuses_both(ci_result: GlabResult) -> None:
    project = GitLabProject.from_origin(ORIGIN)

    async def preflight_runner(args: Sequence[str]) -> GlabResult:
        if args[-1] == "projects/group%2Fproject":
            return GlabResult(0, json.dumps(POLICY_BASE), "")
        return ci_result

    checks = await gitlab_policy_checks(
        ORIGIN, merge_method="squash", allow_no_ci="auto", runner=preflight_runner
    )
    assert any(c.status == "refuse" and c.name == "CI configuration" for c in checks)

    ref = project.parse_mr_url("https://host/group/project/-/merge_requests/1")

    async def gate_runner(args: Sequence[str]) -> GlabResult:
        text = " ".join(args)
        if "repository/files" in text:
            return ci_result
        return GlabResult(0, json.dumps(POLICY_BASE), "")

    gate = GitLabGate(project, runner=gate_runner)
    with pytest.raises(Exception, match="failed|500"):
        await gate._has_ci_config(ref, "main")
