"""The GitLab merge gate evaluating read-only MR facts through glab (spec §10).

Deciding whether a GitLab Merge Request may merge requires querying `glab api`
for MR details, diverged commit counts, pipeline jobs, commit status checks,
and target branch tips. This module reads those facts and produces a standard
GateReport conforming to ForgeGate.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import subprocess
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field, replace
from time import monotonic
from typing import Any
from urllib.parse import quote

from agent_hub_common import SHA_HEX_40_RE

from .merge_gate import (
    POLL_INTERVAL_S,
    POLL_TIMEOUT_S,
    Check,
    CiStatus,
    GateReport,
    Mergeable,
    MergeGateError,
    PrState,
    _settled,
    classify_checks,
)

GLAB_TIMEOUT_S = 30.0
_STDERR_LIMIT = 500
HTTP_ERROR_RE = re.compile(r"\(HTTP\s+([45]\d\d)\)")

MR_URL_RE = re.compile(
    r"https?://(?P<host>[A-Za-z0-9][A-Za-z0-9.-]*(?::[0-9]+)?)"
    r"/(?P<project>(?!\.\.?/)[A-Za-z0-9_.][A-Za-z0-9_./-]*?)"
    r"/(?:-/)?merge_requests/(?P<iid>[1-9][0-9]*)/?"
)


class GitLabGateError(MergeGateError):
    """The GitLab gate could not be evaluated; that is never permission to merge."""


@dataclass(frozen=True, slots=True)
class GlabResult:
    returncode: int
    stdout: str
    stderr: str


GlabRunner = Callable[[Sequence[str]], Awaitable[GlabResult]]


async def run_glab(args: Sequence[str]) -> GlabResult:
    """Run `glab` without letting it touch the hub's MCP stdin or stdout."""

    executable = shutil.which("glab")
    if executable is None:
        raise GitLabGateError("the glab CLI is not on PATH")
    env = os.environ | {"NO_COLOR": "1", "GLAB_NO_UPDATE_NOTIFIER": "1"}
    process = await asyncio.create_subprocess_exec(
        executable,
        *args,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
    )
    try:
        stdout, stderr = await asyncio.wait_for(process.communicate(), GLAB_TIMEOUT_S)
    except TimeoutError:
        process.kill()
        await process.wait()
        raise GitLabGateError(
            f"{_describe(args)} timed out after {GLAB_TIMEOUT_S:g} s"
        ) from None
    return GlabResult(
        returncode=process.returncode if process.returncode is not None else -1,
        stdout=stdout.decode("utf-8", errors="replace"),
        stderr=stderr.decode("utf-8", errors="replace"),
    )


@dataclass(frozen=True, slots=True)
class MergeRequestRef:
    url: str
    host: str
    project: str
    iid: int

    @property
    def hostname(self) -> str:
        return self.host.split(":")[0]

    @classmethod
    def parse(cls, url: str) -> MergeRequestRef:
        """Parse a GitLab MR URL into its host, project path, and IID."""

        text = url.strip()
        match = MR_URL_RE.fullmatch(text)
        if match is None:
            raise ValueError(f"not a GitLab merge request URL: {url!r}")
        return cls(
            url=text.rstrip("/"),
            host=match["host"],
            project=match["project"].strip("/"),
            iid=int(match["iid"]),
        )

    def api(self, path: str, *, paginate: bool = False) -> list[str]:
        encoded_project = quote(self.project, safe="")
        endpoint = (
            f"projects/{encoded_project}/{path.lstrip('/')}"
            if path
            else f"projects/{encoded_project}"
        )
        args = ["api", endpoint, f"--hostname={self.hostname}"]
        if paginate:
            args.append("--paginate")
        return args


def classify_gitlab_status(status: str, *, allow_failure: bool = False) -> str:
    """Map GitLab pipeline/job statuses to Check buckets."""

    s = status.lower().strip()
    if s in ("success", "passed"):
        return "pass"
    if allow_failure:
        return "skipping"
    if s in ("failed",):
        return "fail"
    if s in ("canceled", "canceling", "cancelled"):
        return "cancel"
    if s in ("skipped",):
        return "skipping"
    if s in ("manual",):
        # Required manual actions (allow_failure=False) block merge without intervention
        return "fail"
    return "pending"


def map_detailed_merge_status(status: str, has_conflicts: bool) -> tuple[Mergeable, str]:
    """Map GitLab's 24 detailed_merge_status states to Mergeable enum and status string."""

    s = status.lower().strip()
    if s in ("conflict", "need_rebase") or has_conflicts:
        return Mergeable.CONFLICTING, s or "conflict"
    if s in (
        "checking",
        "unchecked",
        "approvals_syncing",
        "preparing",
    ):
        return Mergeable.UNKNOWN, s
    if s in (
        "mergeable",
        "discussions_not_resolved",
        "not_approved",
        "requested_changes",
        "draft_status",
        "blocked_status",
        "commits_status",
        "ci_still_running",
        "ci_must_pass",
        "status_checks_must_pass",
        "jira_association_missing",
    ):
        return Mergeable.CLEAN, s
    if s == "not_open":
        return Mergeable.UNKNOWN, "not_open"
    if has_conflicts:
        return Mergeable.CONFLICTING, s or "conflict"
    return Mergeable.UNKNOWN, s


def check_unsupported_project_settings(project_data: dict[str, Any]) -> list[str]:
    """Inspect GitLab project settings for unsupported options (spec §10)."""

    errors: list[str] = []
    if project_data.get("automatic_rebase_enabled") is True:
        errors.append(
            "GitLab project has automatic server-side rebase enabled "
            "(automatic_rebase_enabled=true), which rewrites commit SHAs at merge time "
            "and violates the SHA-bound merge invariant."
        )
    if (
        project_data.get("merge_trains_enabled") is True
        or project_data.get("merge_train_enforcement") is True
    ):
        errors.append(
            "GitLab project has merge trains enabled, which queues merges asynchronously "
            "and violates the SHA-bound merge invariant."
        )
    return errors


@dataclass(slots=True)
class GitLabGate:
    """Evaluate check_merge_gate (§10) for GitLab MRs through an injectable `glab` runner."""

    runner: GlabRunner = run_glab
    poll_timeout_s: float = POLL_TIMEOUT_S
    poll_interval_s: float = POLL_INTERVAL_S
    clock: Callable[[], float] = monotonic
    sleep: Callable[[float], Awaitable[Any]] = field(default=asyncio.sleep)

    async def check(self, pr_url: str, expected_head_sha: str) -> GateReport:
        """Report head, CI, base freshness and mergeability for a GitLab MR."""

        if not SHA_HEX_40_RE.fullmatch(expected_head_sha):
            raise ValueError("expected_head_sha must be a 40-character hex commit SHA")

        ref = MergeRequestRef.parse(pr_url)
        expected = expected_head_sha.lower()
        start = self.clock()
        deadline = start + self.poll_timeout_s

        while True:
            report = await self._snapshot(ref, expected)
            remaining = deadline - self.clock()
            if _settled(report) or remaining <= 0:
                return replace(report, elapsed_s=round(self.clock() - start, 3))
            await self.sleep(min(self.poll_interval_s, remaining))

    async def _snapshot(self, ref: MergeRequestRef, expected: str) -> GateReport:
        mr_data = await self._json(
            ref.api(f"merge_requests/{ref.iid}?include_diverged_commits_count=true")
        )

        raw_state = str(mr_data.get("state", "")).lower()
        pr_state = (
            PrState.OPEN
            if raw_state == "opened"
            else (PrState.MERGED if raw_state == "merged" else PrState.CLOSED)
        )

        head = str(mr_data.get("sha", "")).lower()
        if not SHA_HEX_40_RE.fullmatch(head):
            raise GitLabGateError(
                f"glab api reported an MR head that is not a commit SHA: {head!r}"
            )

        base_ref = str(mr_data.get("target_branch", "main"))
        detailed_merge_status = str(mr_data.get("detailed_merge_status") or "")
        has_conflicts = bool(mr_data.get("has_conflicts", False))
        mergeable, merge_state_status = map_detailed_merge_status(
            detailed_merge_status, has_conflicts
        )

        branch_data = await self._json(
            ref.api(f"repository/branches/{quote(base_ref, safe='')}")
        )
        commit_data = branch_data.get("commit")
        if not isinstance(commit_data, dict) or "id" not in commit_data:
            raise GitLabGateError("glab api branch query did not return commit.id")
        main_sha = str(commit_data["id"]).lower()

        diff_refs = mr_data.get("diff_refs")
        if isinstance(diff_refs, dict) and diff_refs.get("base_sha"):
            base_sha = str(diff_refs["base_sha"]).lower()
        else:
            base_sha = main_sha

        diverged_count = mr_data.get("diverged_commits_count")
        if isinstance(diverged_count, int):
            base_behind_main = diverged_count > 0
        else:
            base_behind_main = base_sha != main_sha

        checks: list[Check] = []
        seen_names: set[str] = set()
        stale_pipeline = False
        head_pipeline = mr_data.get("head_pipeline")
        if isinstance(head_pipeline, dict) and "id" in head_pipeline:
            pipeline_sha = str(head_pipeline.get("sha") or "").lower()
            if pipeline_sha and pipeline_sha != head:
                # The MR head has moved beyond this pipeline; don't read stale jobs.
                stale_pipeline = True
            else:
                pipeline_id = head_pipeline["id"]
                pipeline_status = str(head_pipeline.get("status", ""))
                pipeline_url = str(head_pipeline.get("web_url", ""))
                jobs_data = await self._json_list(
                    ref.api(f"pipelines/{pipeline_id}/jobs", paginate=True)
                )
                if jobs_data:
                    for job in jobs_data:
                        if isinstance(job, dict):
                            name = str(job.get("name", ""))
                            allow_failure = bool(job.get("allow_failure", False))
                            checks.append(
                                Check(
                                    name=name,
                                    bucket=classify_gitlab_status(
                                        str(job.get("status", "")),
                                        allow_failure=allow_failure,
                                    ),
                                    link=str(job.get("web_url", "")),
                                )
                            )
                            if name:
                                seen_names.add(name)
                else:
                    checks.append(
                        Check(
                            name=f"pipeline:{pipeline_id}",
                            bucket=classify_gitlab_status(pipeline_status),
                            link=pipeline_url,
                        )
                    )

        statuses = await self._json_list(
            ref.api(f"repository/commits/{head}/statuses", paginate=True)
        )
        for st in statuses:
            if isinstance(st, dict):
                name = str(st.get("name", ""))
                if name and name in seen_names:
                    # Avoid double-counting pipeline jobs returned by commits/:sha/statuses
                    continue
                allow_failure = bool(st.get("allow_failure", False))
                checks.append(
                    Check(
                        name=name,
                        bucket=classify_gitlab_status(
                            str(st.get("status", "")),
                            allow_failure=allow_failure,
                        ),
                        link=str(st.get("target_url", "") or st.get("web_url", "")),
                    )
                )
                if name:
                    seen_names.add(name)

        if stale_pipeline and not checks:
            ci = CiStatus.NO_CHECKS
        else:
            classified = classify_checks(checks)
            if classified is not None:
                ci = classified
            elif stale_pipeline:
                ci = CiStatus.NO_CHECKS
            else:
                has_ci = await self._has_ci_config(ref, head)
                ci = CiStatus.NO_CHECKS if has_ci else CiStatus.NO_WORKFLOWS

        return GateReport(
            pr_url=ref.url,
            pr_state=pr_state,
            expected_head_sha=expected,
            current_head_sha=head,
            head_matches=head == expected,
            ci=ci,
            checks=checks,
            base_ref=base_ref,
            base_sha=base_sha,
            main_sha=main_sha,
            base_behind_main=base_behind_main,
            mergeable=mergeable,
            merge_state_status=merge_state_status or detailed_merge_status,
        )

    async def _has_ci_config(self, ref: MergeRequestRef, head: str) -> bool:
        """Check if CI configuration affirmatively exists for the project/commit."""

        file_args = ref.api(f"repository/files/.gitlab-ci.yml?ref={quote(head, safe='')}")
        result = await self.runner(file_args)
        if (
            result.returncode == 0
            and not HTTP_ERROR_RE.search(result.stderr)
            and "404 File Not Found" not in result.stdout
        ):
            return True

        # Affirmative 404 (file absent)
        is_404 = "(HTTP 404)" in result.stderr or "404 File Not Found" in result.stdout
        if not is_404:
            raise _failure(file_args, result)

        proj_args = ref.api("")
        proj_data = await self._json(proj_args)
        return bool(
            proj_data.get("ci_config_path")
            or proj_data.get("auto_devops_enabled") is True
        )

    async def _json(self, args: Sequence[str]) -> dict[str, Any]:
        result = await self.runner(args)
        _check_result(args, result)
        text = result.stdout.strip()
        try:
            data: Any = json.loads(text)
        except json.JSONDecodeError as exc:
            raise GitLabGateError(f"{_describe(args)} printed invalid JSON") from exc
        if not isinstance(data, dict):
            raise GitLabGateError(f"{_describe(args)} did not print a JSON object")
        if "message" in data and any(
            err in str(data["message"]) for err in ("404", "401", "403")
        ):
            raise _failure(args, result)
        if "error" in data:
            raise _failure(args, result)
        return data

    async def _json_list(self, args: Sequence[str]) -> list[Any]:
        result = await self.runner(args)
        _check_result(args, result)
        text = result.stdout.strip()
        if not text:
            return []
        try:
            data: Any = json.loads(text)
        except json.JSONDecodeError as exc:
            raise GitLabGateError(f"{_describe(args)} printed invalid JSON") from exc
        if not isinstance(data, list):
            raise _failure(args, result)
        return data


def _check_result(args: Sequence[str], result: GlabResult) -> None:
    if result.returncode != 0 or HTTP_ERROR_RE.search(result.stderr):
        raise _failure(args, result)


def _describe(args: Sequence[str]) -> str:
    """Format a command invocation for errors."""

    if len(args) >= 2 and args[0] == "api":
        return f"glab api {args[1]}"
    return "glab " + " ".join(args[:2])


def _failure(args: Sequence[str], result: GlabResult) -> GitLabGateError:
    detail = (
        result.stderr.strip()[:_STDERR_LIMIT]
        or result.stdout.strip()[:_STDERR_LIMIT]
        or f"exit {result.returncode}"
    )
    return GitLabGateError(f"{_describe(args)} failed: {detail}")
