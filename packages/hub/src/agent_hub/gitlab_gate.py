"""The GitLab merge gate evaluating read-only MR facts through glab (spec §10).

Deciding whether a GitLab Merge Request may merge requires querying `glab api`
for MR details, diverged commit counts, the pipelines and commit statuses for
the head, and target branch tips. This module reads those facts and produces a
standard GateReport conforming to ForgeGate. The gate is bound to the hub's own
project, parsed from `origin`; an MR URL anywhere else is refused before any
`glab` call.
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
from urllib.parse import quote, urlsplit

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

# GitLab's project path rules: letters, digits, `_`, `-` and `.`, never
# opening with `-`. The dot segments `.` and `..` are refused separately.
_SEGMENT_RE = re.compile(r"[A-Za-z0-9_.][A-Za-z0-9_.-]*")
_HOST_RE = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9.-]*[A-Za-z0-9])?")
_IID_RE = re.compile(r"[1-9][0-9]{0,17}")
_SCP_ORIGIN_RE = re.compile(r"(?:[^@/:]+@)?(?P<host>[^@/:]+):(?P<path>[^/].*)")
_URL_LIMIT = 200


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
        raise GitLabGateError(f"{_describe(args)} timed out after {GLAB_TIMEOUT_S:g} s") from None
    return GlabResult(
        returncode=process.returncode if process.returncode is not None else -1,
        stdout=stdout.decode("utf-8", errors="replace"),
        stderr=stderr.decode("utf-8", errors="replace"),
    )


def _project_segments(path: str) -> tuple[str, ...] | None:
    segments = tuple(path.split("/"))
    if len(segments) < 2 or any(
        segment in (".", "..") or not _SEGMENT_RE.fullmatch(segment) for segment in segments
    ):
        return None
    return segments


@dataclass(frozen=True, slots=True)
class GitLabProject:
    """The hub's own GitLab project: the only one its gate will read (§10, §12).

    The host and path come from the operator's `origin`, never from an MR URL,
    so a URL in a worker's result cannot point `glab` at another host or project.
    """

    host: str
    segments: tuple[str, ...]

    @property
    def path(self) -> str:
        return "/".join(self.segments)

    @property
    def web_url(self) -> str:
        return f"https://{self.host}/{self.path}"

    @classmethod
    def from_origin(cls, origin: str) -> GitLabProject:
        """Parse an SSH, scp-style or HTTPS origin into its host and project path.

        The web URL is assumed to be `https://<host>/<path>`. An origin that
        says otherwise — plain `http`, or HTTPS on a port other than 443 — is
        refused. One under a relative URL root cannot be told from a nested
        group; its API paths then name no project or a different one. The
        `up` preflight refuses the latter by checking the project web URL.
        """

        text = origin.strip()
        if "://" in text:
            try:
                # urlsplit rejects a malformed bracketed host; .port a bad port.
                parts = urlsplit(text)
                port = parts.port
                hostname = parts.hostname
            except ValueError:
                raise GitLabGateError("the origin's host or port is malformed") from None
            scheme = parts.scheme.lower()
            if scheme not in ("https", "ssh"):
                raise GitLabGateError(
                    f"the GitLab gate needs an https or ssh origin, not {scheme or 'none'}://"
                )
            if scheme == "https" and port not in (None, 443):
                raise GitLabGateError(
                    f"GitLab on HTTPS port {port} is not supported; only the default port"
                )
            host = hostname or ""
            path = parts.path.removeprefix("/")
            if parts.query or parts.fragment:
                path = ""
        elif match := _SCP_ORIGIN_RE.fullmatch(text):
            host, path = match["host"], match["path"]
        else:
            raise GitLabGateError("the origin is not an SSH or HTTPS remote URL")
        path = path.removesuffix("/").removesuffix(".git")
        segments = _project_segments(path)
        if not _HOST_RE.fullmatch(host) or segments is None:
            # Name the host only: an HTTPS origin's userinfo may hold a token.
            raise GitLabGateError(f"cannot read a GitLab project from the origin on host {host!r}")
        return cls(host=host.lower(), segments=segments)

    def parse_mr_url(self, url: str) -> MergeRequestRef:
        """Accept only a merge request of this project, so nothing else reaches `glab`.

        `https://<host>/<project>/-/merge_requests/<iid>` and nothing more: no
        userinfo, port other than 443, query, fragment, percent-encoding or dot
        segment. Surrounding whitespace is trimmed, as the GitHub gate does;
        any left inside is refused. The host and each path segment compare
        case-insensitively, as GitLab routes them; the API calls use this
        project's own spelling.
        """

        text = url.strip()

        def refuse(reason: str) -> GitLabGateError:
            shown = text if len(text) <= _URL_LIMIT else text[:_URL_LIMIT] + "..."
            return GitLabGateError(
                f"refusing merge request URL {shown!r}: {reason}; "
                f"this hub's GitLab project is {self.web_url}"
            )

        if not text.isascii() or not text.isprintable() or " " in text:
            raise refuse("it contains whitespace, control or non-ASCII characters")
        if not text.startswith("https://"):
            raise refuse("only https:// is accepted")
        if "?" in text or "#" in text:
            raise refuse("a query or fragment is not accepted")
        if "%" in text or "\\" in text:
            raise refuse("percent-encoding and backslashes are not accepted")
        authority, _, path = text.removeprefix("https://").partition("/")
        if "@" in authority:
            raise refuse("userinfo is not accepted")
        host, colon, port = authority.partition(":")
        if colon and port != "443":
            raise refuse(f"port {port!r} is not accepted; only the default HTTPS port")
        if not _HOST_RE.fullmatch(host):
            raise refuse("the host is malformed")
        if host.lower() != self.host:
            raise refuse(f"host {host!r} is not this hub's GitLab host")
        segments = path.split("/")
        if (
            len(segments) < 4
            or segments[-3:-1] != ["-", "merge_requests"]
            or not _IID_RE.fullmatch(segments[-1])
        ):
            raise refuse("the path must end at /-/merge_requests/<iid>")
        project = _project_segments("/".join(segments[:-3]))
        if project is None:
            raise refuse("the project path has an empty, dot or invalid segment")
        if [s.lower() for s in project] != [s.lower() for s in self.segments]:
            raise refuse("it is not this hub's project")
        return MergeRequestRef(url=text, project=self, iid=int(segments[-1]))

    def api(self, path: str, *, paginate: bool = False) -> list[str]:
        encoded_project = quote(self.path, safe="")
        endpoint = (
            f"projects/{encoded_project}/{path.lstrip('/')}"
            if path
            else f"projects/{encoded_project}"
        )
        args = ["api", endpoint, f"--hostname={self.host}"]
        if paginate:
            args.append("--paginate")
        return args


@dataclass(frozen=True, slots=True)
class MergeRequestRef:
    url: str
    project: GitLabProject
    iid: int

    def api(self, path: str, *, paginate: bool = False) -> list[str]:
        return self.project.api(path, paginate=paginate)


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
            "GitLab project has merge trains enabled "
            "(merge_trains_enabled or merge_train_enforcement), "
            "which queues merges asynchronously "
            "and violates the SHA-bound merge invariant."
        )
    if project_data.get("merge_pipelines_enabled") is True:
        errors.append(
            "GitLab project has merged-results pipelines enabled "
            "(merge_pipelines_enabled=true); their synthetic SHA is not the reviewed head."
        )
    return errors


def check_merge_compatibility(project: dict[str, Any], merge_method: str) -> list[str]:
    """Return refusals for a project's settings and the requested policy merge method."""
    if merge_method == "rebase":
        return ["Policy merge_method=rebase uses server-side rebase and violates SHA-bound merge."]
    if merge_method not in ("merge", "squash"):
        return [f"Unknown policy merge_method={merge_method!r}."]
    if project.get("merge_method") not in ("merge", "rebase_merge", "ff"):
        return ["Unknown project merge_method; cannot verify merge compatibility."]
    squash = project.get("squash_option")
    if squash not in ("default_on", "default_off", "always", "never"):
        return ["Unknown project squash_option; cannot verify merge compatibility."]
    if squash == "always" and merge_method == "merge":
        return ["Project squash_option=always conflicts with policy merge_method=merge."]
    if squash == "never" and merge_method == "squash":
        return ["Project squash_option=never conflicts with policy merge_method=squash."]
    return []


@dataclass(slots=True)
class GitLabGate:
    """Evaluate check_merge_gate (§10) for the hub's GitLab project through `glab`."""

    project: GitLabProject
    runner: GlabRunner = run_glab
    poll_timeout_s: float = POLL_TIMEOUT_S
    poll_interval_s: float = POLL_INTERVAL_S
    clock: Callable[[], float] = monotonic
    sleep: Callable[[float], Awaitable[Any]] = field(default=asyncio.sleep)

    async def check(self, pr_url: str, expected_head_sha: str) -> GateReport:
        """Report head, CI, base freshness and mergeability for a GitLab MR."""

        if not SHA_HEX_40_RE.fullmatch(expected_head_sha):
            raise ValueError("expected_head_sha must be a 40-character hex commit SHA")

        ref = self.project.parse_mr_url(pr_url)
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

        branch_data = await self._json(ref.api(f"repository/branches/{quote(base_ref, safe='')}"))
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

        head_pipeline = mr_data.get("head_pipeline")
        checks = await self._pipeline_checks(ref, head, head_pipeline)
        if (
            isinstance(head_pipeline, dict)
            and str(head_pipeline.get("sha") or head).lower() != head
        ):
            # A merged-results pipeline tests a synthetic merge commit, and a
            # head_pipeline left from before a push tests an older head;
            # neither is a verdict on this head (unsupported or settling).
            ci = CiStatus.NO_CHECKS
        else:
            classified = classify_checks(checks)
            if classified is not None:
                ci = classified
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

    async def _pipeline_checks(
        self, ref: MergeRequestRef, head: str, head_pipeline: Any
    ) -> list[Check]:
        """One check per pipeline GitLab ran for `head`, and one per job or status.

        Every pipeline for the SHA counts, not only the MR's head_pipeline: a
        project that runs both branch and MR pipelines must pass both. Each
        pipeline's own status is a check, so one whose jobs are not visible yet
        still holds the gate. The statuses query keeps its default `all=false`:
        GitLab then returns only the latest attempt of each job, so a job
        retried in place supersedes its earlier attempt, while a new pipeline
        for the same SHA supersedes nothing. A status is keyed by its name and
        pipeline, so a green `test` in one pipeline cannot hide a red one in
        another.
        """

        pipelines = await self._json_list(
            ref.api(f"pipelines?sha={head}&per_page=100", paginate=True)
        )
        if (
            isinstance(head_pipeline, dict)
            and "id" in head_pipeline
            and str(head_pipeline.get("sha") or "").lower() == head
        ):
            # GitLab names it on the MR; count it even if the list lags behind.
            pipelines.append(head_pipeline)
        checks: list[Check] = []
        links: dict[Any, str] = {}
        for pipeline in pipelines:
            if not isinstance(pipeline, dict) or pipeline.get("id") in links:
                continue
            pipeline_id = pipeline.get("id")
            links[pipeline_id] = str(pipeline.get("web_url") or "")
            checks.append(
                Check(
                    name=f"pipeline:{pipeline_id}",
                    bucket=classify_gitlab_status(str(pipeline.get("status", ""))),
                    link=links[pipeline_id],
                )
            )

        statuses = await self._json_list(
            ref.api(f"repository/commits/{head}/statuses?per_page=100", paginate=True)
        )
        for status in statuses:
            if not isinstance(status, dict):
                continue
            name = str(status.get("name", ""))
            pipeline_id = status.get("pipeline_id")
            checks.append(
                Check(
                    name=name if pipeline_id is None else f"pipeline:{pipeline_id}/{name}",
                    bucket=classify_gitlab_status(
                        str(status.get("status", "")),
                        allow_failure=bool(status.get("allow_failure", False)),
                    ),
                    link=str(status.get("target_url") or links.get(pipeline_id, "")),
                )
            )
        return checks

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
        return bool(proj_data.get("ci_config_path") or proj_data.get("auto_devops_enabled") is True)

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
        if "message" in data and any(err in str(data["message"]) for err in ("404", "401", "403")):
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
        if text.startswith("{"):
            raise _failure(args, result)
        try:
            return _decode_json_arrays(text)
        except (json.JSONDecodeError, ValueError) as exc:
            raise GitLabGateError(f"{_describe(args)} printed invalid JSON") from exc


@dataclass(frozen=True, slots=True)
class UnboundGitLabGate:
    """The gate of a GitLab hub whose origin names no supported project.

    The hub still serves; every gate call fails closed with the reason.
    """

    reason: str

    async def check(self, pr_url: str, expected_head_sha: str) -> GateReport:
        raise GitLabGateError(f"the GitLab merge gate is unavailable: {self.reason}")


def _decode_json_arrays(text: str) -> list[Any]:
    """Decode concatenated JSON arrays output by glab api --paginate ([...][...])."""
    decoder = json.JSONDecoder()
    pos = 0
    length = len(text)
    items: list[Any] = []
    while pos < length:
        while pos < length and text[pos].isspace():
            pos += 1
        if pos >= length:
            break
        val, end = decoder.raw_decode(text, idx=pos)
        if not isinstance(val, list):
            raise ValueError(f"Expected JSON array, got {type(val).__name__}")
        items.extend(val)
        pos = end
    return items


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
