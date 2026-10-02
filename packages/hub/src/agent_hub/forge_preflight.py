"""Policy-independent GitLab startup checks; unavailable facts only warn."""

from __future__ import annotations

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
    check_unsupported_project_settings,
    run_glab,
)


@dataclass(frozen=True)
class PreflightCheck:
    name: str
    status: Literal["pass", "warn", "note", "refuse"]
    detail: str


async def gitlab_preflight(
    origin: str, *, runner: GlabRunner = run_glab,
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
    checks.append(PreflightCheck(
        "glab version", "pass" if supported else "warn",
        "glab >= 1.36.0" if supported else "glab missing, unreadable, or older than 1.36.0",
    ))
    user = await api("user")
    authenticated = user is not None and isinstance(user.get("username"), str)
    checks.append(PreflightCheck(
        "Authenticated", "pass" if authenticated else "warn",
        f"origin host {project.host}" if authenticated
        else "cannot verify origin-host authentication",
    ))
    data = await api(f"projects/{quote(project.path, safe='')}")
    readable = data is not None and isinstance(data.get("id"), int)
    if readable and data is not None:
        web_url = data.get("web_url")
        if isinstance(web_url, str) and web_url.lower() != project.web_url.lower():
            checks.append(PreflightCheck(
                "Project readable", "refuse",
                f"project web_url {web_url!r} differs from expected {project.web_url!r}; "
                "a relative URL root or project mismatch would make the gate report on "
                "the wrong project. See https://github.com/RoboNater/robomate/issues/80",
            ))
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
            checks.append(PreflightCheck(
                "Project readable", "pass" if verified else "warn",
                "web_url matches; developer access or more" if verified else
                "project readable, but web_url or developer access cannot be verified",
            ))
    else:
        data = None
        checks.append(PreflightCheck("Project readable", "warn", "cannot read origin project"))

    if data is None:
        for name in ("Unsupported settings", "Auto DevOps", "Pipelines must succeed"):
            checks.append(PreflightCheck(
                name, "warn", "cannot check without verified origin project settings",
            ))
        return checks
    errors = check_unsupported_project_settings(data)
    checks.append(PreflightCheck(
        "Unsupported settings", "refuse" if errors else "pass",
        " ".join(errors) + " Disable these settings in project Settings > Merge requests."
        if errors else "no definite unsupported settings reported by the project API",
    ))
    auto = data.get("auto_devops_enabled")
    checks.append(PreflightCheck(
        "Auto DevOps", "pass" if auto is False else "warn",
        "off" if auto is False else "on or unknown; when on, the gate cannot report no_workflows",
    ))
    pipelines = data.get("only_allow_merge_if_pipeline_succeeds")
    checks.append(PreflightCheck(
        "Pipelines must succeed", "pass" if pipelines is True else "note",
        "on" if pipelines is True else "off or unknown; enable for defense in depth",
    ))
    return checks
