"""Policy-independent GitLab startup checks; unavailable facts only warn.

Also policy-dependent GitLab checks shared by `prepare-run.py` and
`robomate submit`, and GitHub checks for `robomate submit` (M2 Step 4, #241;
prepare-run keeps its own GitHub checks until Step 11).
"""

from __future__ import annotations

import asyncio
import json
import re
import shutil
import subprocess
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from typing import Any, Literal
from urllib.parse import quote

from .gitlab_gate import (
    HTTP_ERROR_RE,
    GitLabGateError,
    GitLabProject,
    GlabRunner,
    check_merge_compatibility,
    check_unsupported_project_settings,
    is_definite_ci_absence,
    run_glab,
)


@dataclass(frozen=True)
class PreflightCheck:
    name: str
    status: Literal["pass", "warn", "note", "refuse"]
    detail: str


@dataclass(frozen=True)
class ProjectFacts:
    """One parsed GitLab project response, shared by both check suites (r1-5).

    `is_valid` means the response names a project (`id` int, no `message`);
    `web_url_matches` compares `web_url` case-insensitively to the origin's;
    `access_levels` are the `project_access`/`group_access` levels found.
    Callers map these typed facts to `pass`/`warn`/`refuse` without parsing
    detail text (r2-1); `robomate up` keeps its warn-only details unchanged.
    """

    is_valid: bool
    web_url: str | None
    web_url_matches: bool
    access_levels: tuple[int, ...] = field(default_factory=tuple)

    @property
    def has_developer_access(self) -> bool:
        return bool(self.access_levels and max(self.access_levels) >= 30)


def project_facts(data: Any, project: GitLabProject) -> ProjectFacts:
    """Parse one project API response into typed facts (r1-5)."""

    if not isinstance(data, dict) or not isinstance(data.get("id"), int) or "message" in data:
        return ProjectFacts(is_valid=False, web_url=None, web_url_matches=False)
    web_url = data.get("web_url")
    web_url_str = web_url if isinstance(web_url, str) else None
    matches = isinstance(web_url, str) and web_url.lower() == project.web_url.lower()
    levels: list[int] = []
    permissions = data.get("permissions")
    if isinstance(permissions, dict):
        for key in ("project_access", "group_access"):
            access = permissions.get(key)
            if isinstance(access, dict) and isinstance(access.get("access_level"), int):
                levels.append(int(access["access_level"]))
    return ProjectFacts(
        is_valid=True,
        web_url=web_url_str,
        web_url_matches=bool(matches),
        access_levels=tuple(levels),
    )


async def gitlab_preflight(
    origin: str,
    *,
    runner: GlabRunner = run_glab,
) -> list[PreflightCheck]:
    """Check the origin project without making forge reachability a startup requirement."""
    checks: list[PreflightCheck] = []
    try:
        project = GitLabProject.from_origin(origin)
    except GitLabGateError as exc:
        return [PreflightCheck("Project readable", "refuse", str(exc))]

    async def invoke(args: list[str]) -> str | None:
        try:
            result = await runner(args)
        except (GitLabGateError, OSError, TimeoutError):
            return None
        if result.returncode or HTTP_ERROR_RE.search(result.stderr):
            return None
        return result.stdout

    async def api(path: str) -> dict[str, Any] | None:
        output = await invoke(["api", "--hostname", project.host, path])
        try:
            data = json.loads(output) if output is not None else None
        except ValueError:
            return None
        return data if isinstance(data, dict) and "message" not in data else None

    version = await invoke(["version"])
    match = re.search(r"\b(\d+)\.(\d+)\.(\d+)\b", version or "")
    supported = bool(match and tuple(map(int, match.groups())) >= (1, 36, 0))
    checks.append(
        PreflightCheck(
            "glab version",
            "pass" if supported else "warn",
            "glab >= 1.36.0" if supported else "glab missing, unreadable, or older than 1.36.0",
        )
    )
    user = await api("user")
    authenticated = user is not None and isinstance(user.get("username"), str)
    checks.append(
        PreflightCheck(
            "Authenticated",
            "pass" if authenticated else "warn",
            f"origin host {project.host}"
            if authenticated
            else "cannot verify origin-host authentication",
        )
    )
    data = await api(f"projects/{quote(project.path, safe='')}")
    facts = project_facts(data, project)
    if facts.is_valid:
        web_url = facts.web_url
        if not facts.web_url_matches:
            checks.append(
                PreflightCheck(
                    "Project readable",
                    "refuse",
                    f"project web_url {web_url!r} differs from expected {project.web_url!r}; "
                    "a relative URL root or project mismatch would make the gate report on "
                    "the wrong project. See https://github.com/RoboNater/robomate/issues/80",
                )
            )
            data = None
        else:
            verified = facts.has_developer_access and isinstance(web_url, str)
            checks.append(
                PreflightCheck(
                    "Project readable",
                    "pass" if verified else "warn",
                    "web_url matches; developer access or more"
                    if verified
                    else "project readable, but web_url or developer access cannot be verified",
                )
            )
    else:
        data = None
        checks.append(PreflightCheck("Project readable", "warn", "cannot read origin project"))

    if data is None:
        for name in ("Unsupported settings", "Auto DevOps", "Pipelines must succeed"):
            checks.append(
                PreflightCheck(
                    name,
                    "warn",
                    "cannot check without verified origin project settings",
                )
            )
        return checks
    errors = check_unsupported_project_settings(data)
    checks.append(
        PreflightCheck(
            "Unsupported settings",
            "refuse" if errors else "pass",
            " ".join(errors) + " Disable these settings in project Settings > Merge requests."
            if errors
            else "no definite unsupported settings reported by the project API",
        )
    )
    auto = data.get("auto_devops_enabled")
    checks.append(
        PreflightCheck(
            "Auto DevOps",
            "pass" if auto is False else "warn",
            "off"
            if auto is False
            else "on or unknown; when on, the gate cannot report no_workflows",
        )
    )
    pipelines = data.get("only_allow_merge_if_pipeline_succeeds")
    checks.append(
        PreflightCheck(
            "Pipelines must succeed",
            "pass" if pipelines is True else "note",
            "on" if pipelines is True else "off or unknown; enable for defense in depth",
        )
    )
    return checks


async def gitlab_policy_checks(
    origin: str,
    *,
    merge_method: str,
    allow_no_ci: str,
    runner: GlabRunner = run_glab,
) -> list[PreflightCheck]:
    """Policy-dependent GitLab checks: access, merge compatibility, CI presence (#96, #93).

    Shared by ``prepare-run.py`` now and ``robomate submit`` next (M2 Step 4,
    #241): the caller enforces ``refuse`` strictly, while ``robomate up``
    keeps its warn-only ``gitlab_preflight`` unchanged.

    - ``allow_no_ci`` explicit (``true``/``false``) runs no CI-file call at all
      (#93 r2-1).
    - ``auto`` probes ``.gitlab-ci.yml`` on the default branch with the same
      definite-404 rule as ``GitLabGate._has_ci_config`` (only ``(HTTP 404)``
      in stderr or ``404 File Not Found`` in stdout means absent; the command
      line never counts, so a host/path/ref containing ``404`` is not "no CI",
      #93 r2-2). A non-404 failure under ``auto`` refuses and names the check,
      with a remedy that works (an explicit ``--allow-no-ci``).
    - The gate checks the MR head ref; this checks the default branch ref, so
      the ref is a parameter of the shared rule.
    - ``CI configuration`` is typed data, not text to parse: ``pass`` means
      the file (or an override) is present, ``note`` means absent (``auto``)
      or skipped (explicit), ``refuse`` means the probe itself failed.
    """

    if allow_no_ci not in ("auto", "true", "false"):
        return [
            PreflightCheck(
                "CI configuration",
                "refuse",
                f"unknown allow_no_ci={allow_no_ci!r}; want one of auto, true, false",
            )
        ]
    try:
        project = GitLabProject.from_origin(origin)
    except GitLabGateError as exc:
        return [PreflightCheck("Project readable", "refuse", str(exc))]
    slug = project.path
    host = project.host

    try:
        result = await runner(["api", "--hostname", host, f"projects/{quote(slug, safe='')}"])
    except (GitLabGateError, OSError, TimeoutError) as exc:
        return [
            PreflightCheck(
                "Project readable",
                "refuse",
                f"cannot read project settings for {slug} on {host}; "
                f"check glab auth and repository name: {exc}",
            )
        ]
    if result.returncode or HTTP_ERROR_RE.search(result.stderr):
        return [
            PreflightCheck(
                "Project readable",
                "refuse",
                f"cannot read project settings for {slug} on {host}; "
                "check glab auth and repository name",
            )
        ]
    try:
        proj_data: Any = json.loads(result.stdout)
    except ValueError:
        return [
            PreflightCheck(
                "Project readable",
                "refuse",
                f"cannot read project settings for {slug} on {host}; "
                "check glab auth and repository name",
            )
        ]
    facts = project_facts(proj_data, project)
    if not facts.is_valid:
        return [
            PreflightCheck(
                "Project readable",
                "refuse",
                f"cannot read project settings for {slug} on {host}; "
                "check glab auth and repository name",
            )
        ]

    if not facts.web_url_matches:
        return [
            PreflightCheck(
                "Project readable",
                "refuse",
                f"project web_url {facts.web_url!r} differs from expected {project.web_url!r}",
            )
        ]

    checks: list[PreflightCheck] = []
    if not facts.has_developer_access:
        permissions = proj_data.get("permissions") if isinstance(proj_data, dict) else None
        return [
            PreflightCheck(
                "Developer access",
                "refuse",
                f"repository {slug} requires developer access or more "
                f"(permissions {permissions!r}); check repo permissions",
            )
        ]
    checks.append(
        PreflightCheck(
            "Developer access",
            "pass",
            "web_url matches; developer access or more",
        )
    )

    errors = check_merge_compatibility(proj_data, merge_method)
    if errors:
        return [
            *checks,
            PreflightCheck(
                "Merge compatibility",
                "refuse",
                f"repository {slug} does not allow the {merge_method!r} merge method: "
                f"{' '.join(errors)}",
            ),
        ]
    checks.append(PreflightCheck("Merge compatibility", "pass", f"{slug} allows {merge_method}"))

    if allow_no_ci in ("true", "false"):
        checks.append(
            PreflightCheck(
                "CI configuration",
                "note",
                f"{slug} CI check skipped (explicit allow_no_ci={allow_no_ci})",
            )
        )
        return checks

    default_branch = str(proj_data.get("default_branch") or "main")
    file_path = (
        f"projects/{quote(slug, safe='')}/repository/files/"
        f"{quote('.gitlab-ci.yml', safe='')}?ref={quote(default_branch, safe='')}"
    )
    try:
        file_result = await runner(["api", "--hostname", host, file_path])
    except (GitLabGateError, OSError, TimeoutError) as exc:
        return [
            *checks,
            PreflightCheck(
                "CI configuration",
                "refuse",
                f"cannot check CI configuration for {slug} on {host}: {exc}; "
                "check network/auth or pass explicit --allow-no-ci true|false",
            ),
        ]
    if (
        file_result.returncode == 0
        and not HTTP_ERROR_RE.search(file_result.stderr)
        and "404 File Not Found" not in file_result.stdout
    ):
        has_ci = True
    elif not is_definite_ci_absence(file_result.stdout, file_result.stderr):
        detail = (
            file_result.stderr.strip()
            or file_result.stdout.strip()
            or f"exit {file_result.returncode}"
        )[:500]
        return [
            *checks,
            PreflightCheck(
                "CI configuration",
                "refuse",
                f"cannot check CI configuration for {slug} on {host}: {detail}; "
                "check network/auth or pass explicit --allow-no-ci true|false",
            ),
        ]
    else:
        has_ci = bool(
            proj_data.get("ci_config_path") or proj_data.get("auto_devops_enabled") is True
        )

    if has_ci:
        checks.append(
            PreflightCheck(
                "CI configuration",
                "pass",
                f"{slug} has CI configured (allow_no_ci=false)",
            )
        )
    else:
        checks.append(
            PreflightCheck(
                "CI configuration",
                "note",
                f"{slug} has no CI configured (allow_no_ci=true)",
            )
        )
    return checks


def gitlab_preflight_sync(origin: str, *, runner: GlabRunner) -> list[PreflightCheck]:
    """Sync wrapper for the policy-independent base checks: no second implementation."""

    return asyncio.run(gitlab_preflight(origin, runner=runner))


def gitlab_policy_checks_sync(
    origin: str,
    *,
    merge_method: str,
    allow_no_ci: str,
    runner: GlabRunner,
) -> list[PreflightCheck]:
    """Sync wrapper for ``prepare-run.py``: no second implementation."""

    return asyncio.run(
        gitlab_policy_checks(
            origin, merge_method=merge_method, allow_no_ci=allow_no_ci, runner=runner
        )
    )


GH_TIMEOUT_S = 30.0


@dataclass(frozen=True, slots=True)
class GhResult:
    returncode: int
    stdout: str
    stderr: str


GhRunner = Callable[[Sequence[str]], Awaitable[GhResult]]


async def run_gh(args: Sequence[str]) -> GhResult:
    """Run `gh` without letting it touch the hub's MCP stdin or stdout (M2 Step 4).

    Mirrors `gitlab_gate.run_glab` (including its timeout) so submit's GitHub
    checks see the same failures as the merge gate would, not prepare-run's
    untimed adapter.
    """

    import os

    executable = shutil.which("gh")
    if executable is None:
        raise OSError("the gh CLI is not on PATH")
    env = os.environ | {"NO_COLOR": "1", "GH_NO_UPDATE_NOTIFIER": "1"}
    process = await asyncio.create_subprocess_exec(
        executable,
        *args,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
    )
    try:
        stdout, stderr = await asyncio.wait_for(process.communicate(), GH_TIMEOUT_S)
    except TimeoutError:
        process.kill()
        await process.wait()
        raise TimeoutError(f"gh {' '.join(args[:2])} timed out after {GH_TIMEOUT_S:g} s") from None
    return GhResult(
        returncode=process.returncode if process.returncode is not None else -1,
        stdout=stdout.decode("utf-8", errors="replace"),
        stderr=stderr.decode("utf-8", errors="replace"),
    )


def parse_github_slug(origin: str) -> str | None:
    """Return `owner/repo` for a GitHub origin, else None for local paths.

    Hub origins are full remotes (SSH or HTTPS), never bare slugs; a local
    path (no remote) has no slug and skips forge checks, like prepare-run.
    """

    text = origin.strip()
    if text.startswith("git@github.com:"):
        slug = text.removeprefix("git@github.com:").removesuffix(".git")
    elif "github.com/" in text:
        slug = text.split("github.com/", 1)[1].removesuffix(".git").lstrip("/")
    else:
        return None
    parts = slug.strip("/").split("/")
    if len(parts) != 2 or not all(parts):
        return None
    return f"{parts[0]}/{parts[1]}"


async def github_policy_checks(
    origin: str,
    *,
    merge_method: str,
    allow_no_ci: str,
    runner: GhRunner = run_gh,
) -> list[PreflightCheck]:
    """Policy-dependent GitHub checks for `robomate submit` (M2 Step 4, #241).

    Shared hub functions (no second copy in the CLI): repository access, merge
    compatibility and CI presence. Explicit `allow_no_ci` `true`/`false` runs
    no workflow probe; `auto` lists workflows and refuses on failure naming
    the check. A local origin (no slug) returns no checks. GitHub runs no glab.
    `CI configuration` is typed like the GitLab suite: `pass` means present,
    `note` means absent (`auto`) or skipped (explicit).
    """

    if allow_no_ci not in ("auto", "true", "false"):
        return [
            PreflightCheck(
                "CI configuration",
                "refuse",
                f"unknown allow_no_ci={allow_no_ci!r}; want one of auto, true, false",
            )
        ]
    slug = parse_github_slug(origin)
    if slug is None:
        return []
    try:
        result = await runner(
            [
                "repo",
                "view",
                slug,
                "--json",
                "viewerPermission,squashMergeAllowed,mergeCommitAllowed,rebaseMergeAllowed",
            ]
        )
    except (OSError, TimeoutError) as exc:
        return [
            PreflightCheck(
                "Repository readable",
                "refuse",
                f"cannot read repository settings for {slug}; check `gh auth status` "
                f"and the repository name: {exc}",
            )
        ]
    if result.returncode:
        detail = (result.stderr.strip() or result.stdout.strip() or f"exit {result.returncode}")[
            :500
        ]
        return [
            PreflightCheck(
                "Repository readable",
                "refuse",
                f"cannot read repository settings for {slug}; check `gh auth status` "
                f"and the repository name: {detail}",
            )
        ]
    try:
        settings: Any = json.loads(result.stdout)
    except ValueError:
        return [
            PreflightCheck(
                "Repository readable",
                "refuse",
                f"cannot read repository settings for {slug}; check `gh auth status` "
                "and the repository name",
            )
        ]
    if not isinstance(settings, dict):
        return [
            PreflightCheck(
                "Repository readable",
                "refuse",
                f"cannot read repository settings for {slug}; check `gh auth status` "
                "and the repository name",
            )
        ]
    if settings.get("viewerPermission") not in ("ADMIN", "MAINTAIN", "WRITE"):
        return [
            PreflightCheck(
                "Repository access",
                "refuse",
                f"repository {slug} requires push access "
                f"(viewerPermission {settings.get('viewerPermission')!r}); "
                "check `gh auth status` and repo permissions",
            )
        ]
    checks: list[PreflightCheck] = [
        PreflightCheck("Repository access", "pass", f"{slug} allows push access")
    ]
    allowed = {
        "squash": settings.get("squashMergeAllowed"),
        "merge": settings.get("mergeCommitAllowed"),
        "rebase": settings.get("rebaseMergeAllowed"),
    }
    if merge_method not in ("squash", "merge", "rebase"):
        return [
            *checks,
            PreflightCheck(
                "Merge compatibility",
                "refuse",
                f"unknown merge_method={merge_method!r}; want one of squash, merge, rebase",
            ),
        ]
    if not allowed.get(merge_method):
        return [
            *checks,
            PreflightCheck(
                "Merge compatibility",
                "refuse",
                f"repository {slug} does not allow the {merge_method!r} merge method; "
                "enable it in repository settings or pass another merge_method",
            ),
        ]
    checks.append(PreflightCheck("Merge compatibility", "pass", f"{slug} allows {merge_method}"))
    if allow_no_ci in ("true", "false"):
        checks.append(
            PreflightCheck(
                "CI configuration",
                "note",
                f"{slug} CI check skipped (explicit allow_no_ci={allow_no_ci})",
            )
        )
        return checks
    try:
        wf_result = await runner(["api", f"repos/{slug}/actions/workflows"])
    except (OSError, TimeoutError) as exc:
        return [
            *checks,
            PreflightCheck(
                "CI configuration",
                "refuse",
                f"cannot check CI configuration for {slug}: {exc}; "
                "check network/auth or pass explicit allow_no_ci true|false",
            ),
        ]
    if wf_result.returncode:
        detail = (
            wf_result.stderr.strip() or wf_result.stdout.strip() or f"exit {wf_result.returncode}"
        )[:500]
        return [
            *checks,
            PreflightCheck(
                "CI configuration",
                "refuse",
                f"cannot check CI configuration for {slug}: {detail}; "
                "check network/auth or pass explicit allow_no_ci true|false",
            ),
        ]
    try:
        payload: Any = json.loads(wf_result.stdout)
    except ValueError:
        return [
            *checks,
            PreflightCheck(
                "CI configuration",
                "refuse",
                f"cannot check CI configuration for {slug}: invalid JSON; "
                "check network/auth or pass explicit allow_no_ci true|false",
            ),
        ]
    workflows = payload.get("workflows", []) if isinstance(payload, dict) else []
    has_workflows = bool(workflows)
    if has_workflows:
        checks.append(
            PreflightCheck(
                "CI configuration",
                "pass",
                f"{slug} has workflows (allow_no_ci=false)",
            )
        )
    else:
        checks.append(
            PreflightCheck(
                "CI configuration",
                "note",
                f"{slug} has no workflows (allow_no_ci=true)",
            )
        )
    return checks
