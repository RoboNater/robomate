"""Policy-independent GitLab startup checks; unavailable facts only warn."""

from __future__ import annotations

import asyncio
import json
import re
from dataclasses import dataclass
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
    readable = data is not None and isinstance(data.get("id"), int)
    if readable and data is not None:
        web_url = data.get("web_url")
        if isinstance(web_url, str) and web_url.lower() != project.web_url.lower():
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
            permissions = data.get("permissions")
            levels = []
            if isinstance(permissions, dict):
                for key in ("project_access", "group_access"):
                    access = permissions.get(key)
                    if isinstance(access, dict) and isinstance(access.get("access_level"), int):
                        levels.append(access["access_level"])
            developer = bool(levels and max(levels) >= 30)
            verified = developer and isinstance(web_url, str)
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
    if (
        not isinstance(proj_data, dict)
        or not isinstance(proj_data.get("id"), int)
        or "message" in proj_data
    ):
        return [
            PreflightCheck(
                "Project readable",
                "refuse",
                f"cannot read project settings for {slug} on {host}; "
                "check glab auth and repository name",
            )
        ]

    web_url = proj_data.get("web_url")
    if isinstance(web_url, str) and web_url.lower() != project.web_url.lower():
        return [
            PreflightCheck(
                "Project readable",
                "refuse",
                f"project web_url {web_url!r} differs from expected {project.web_url!r}",
            )
        ]

    permissions = proj_data.get("permissions")
    levels: list[int] = []
    if isinstance(permissions, dict):
        for key in ("project_access", "group_access"):
            access = permissions.get(key)
            if isinstance(access, dict) and isinstance(access.get("access_level"), int):
                levels.append(access["access_level"])
    checks: list[PreflightCheck] = []
    if not (levels and max(levels) >= 30):
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
