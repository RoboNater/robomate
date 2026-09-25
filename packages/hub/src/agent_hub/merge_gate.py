"""The merge gate Alice evaluates immediately before merging (spec §4.2, §5 MERGE).

Deciding whether a PR may merge takes several `gh` calls and some easily
misread output: `gh pr checks` exits 1 both when checks fail and when there are
none, a cancelled run is neither a pass nor a failure, and a PR whose base has
moved on can be approved, green and still wrong to merge. This module does that
reading once, in code, and reports facts. What to do about each one — merge,
re-review, rebase, escalate — stays policy in Alice's skill.

Every `gh` call runs with stdin closed and stdout captured: the hub's own stdin
and stdout carry MCP framing (#7), and a child that inherited either would
corrupt it.
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
from enum import StrEnum
from time import monotonic
from typing import Any
from urllib.parse import quote

from agent_hub_common import SHA_HEX_40_RE

POLL_TIMEOUT_S = 60.0
# Matches `gh pr checks --watch`'s own default `--interval`.
POLL_INTERVAL_S = 10.0
GH_TIMEOUT_S = 30.0
_STDERR_LIMIT = 500

# Each part lands in a `gh api` path, so none may be a dot segment or open
# with a hyphen.
PR_URL_RE = re.compile(
    r"https://(?P<host>[A-Za-z0-9][A-Za-z0-9.-]*)"
    r"/(?P<owner>[A-Za-z0-9][A-Za-z0-9-]*)"
    r"/(?P<repo>(?!\.\.?/)[A-Za-z0-9_.][A-Za-z0-9_.-]*)"
    r"/pull/(?P<number>[1-9][0-9]*)/?"
)


class CiStatus(StrEnum):
    """The CI verdict over the PR's current head (§5 MERGE)."""

    PASS = "pass"
    FAIL = "fail"
    PENDING = "pending"
    CANCELLED = "cancelled"
    NO_CHECKS = "no_checks"
    NO_WORKFLOWS = "no_workflows"


class PrState(StrEnum):
    """Whether the PR is still open, and so still mergeable at all."""

    OPEN = "open"
    MERGED = "merged"
    CLOSED = "closed"


class Mergeable(StrEnum):
    """Whether the PR's head merges into its base without textual conflicts."""

    CLEAN = "clean"
    CONFLICTING = "conflicting"
    # GitHub computes mergeability lazily; the skill treats this as pending.
    UNKNOWN = "unknown"


# States that can resolve by themselves, so the gate waits on them.
_POLLED_CI = (CiStatus.PENDING, CiStatus.CANCELLED, CiStatus.NO_CHECKS)


class MergeGateError(RuntimeError):
    """The gate could not be evaluated; that is never permission to merge."""


@dataclass(frozen=True, slots=True)
class GhResult:
    returncode: int
    stdout: str
    stderr: str


GhRunner = Callable[[Sequence[str]], Awaitable[GhResult]]


async def run_gh(args: Sequence[str]) -> GhResult:
    """Run `gh` without letting it touch the hub's MCP stdin or stdout."""

    executable = shutil.which("gh")
    if executable is None:
        raise MergeGateError("the gh CLI is not on PATH")
    # Never prompt: there is no terminal, and stdin belongs to MCP.
    env = os.environ | {"GH_PROMPT_DISABLED": "1", "GH_NO_UPDATE_NOTIFIER": "1"}
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
        raise MergeGateError(f"{_describe(args)} timed out after {GH_TIMEOUT_S:g} s") from None
    return GhResult(
        returncode=process.returncode if process.returncode is not None else -1,
        stdout=stdout.decode("utf-8", errors="replace"),
        stderr=stderr.decode("utf-8", errors="replace"),
    )


@dataclass(frozen=True, slots=True)
class PullRequestRef:
    url: str
    host: str
    owner: str
    repo: str
    number: int

    @classmethod
    def parse(cls, url: str) -> PullRequestRef:
        """Accept only a PR URL, so nothing reaches `gh` that it reads as a flag."""

        text = url.strip()
        match = PR_URL_RE.fullmatch(text)
        if match is None:
            raise ValueError(f"not a pull request URL: {url!r}")
        return cls(
            url=text.rstrip("/"),
            host=match["host"],
            owner=match["owner"],
            repo=match["repo"],
            number=int(match["number"]),
        )

    def api(self, path: str) -> list[str]:
        return ["api", f"--hostname={self.host}", f"repos/{self.owner}/{self.repo}/{path}"]


@dataclass(frozen=True, slots=True)
class Check:
    name: str
    bucket: str
    link: str


@dataclass(frozen=True, slots=True)
class GateReport:
    """What the gate saw. Merge only when the §5 MERGE invariant holds on it."""

    pr_url: str
    pr_state: PrState
    expected_head_sha: str
    current_head_sha: str
    head_matches: bool
    ci: CiStatus
    checks: list[Check]
    # The PR's base branch, the commit the PR's head is based on, and that
    # branch's current tip. The names follow the spec, where the base is main.
    base_ref: str
    base_sha: str
    main_sha: str
    base_behind_main: bool
    mergeable: Mergeable
    merge_state_status: str
    elapsed_s: float = 0.0


def classify_checks(checks: Sequence[Check]) -> CiStatus | None:
    """Reduce check buckets to one verdict; None when no checks are reported.

    A failure is decisive. A cancelled run says nothing about the code and is
    reported ahead of pending, because only a superseding run clears it.
    Anything but `pass` or `skipping` keeps the gate from reading green.
    """

    if not checks:
        return None
    buckets = {check.bucket for check in checks}
    if "fail" in buckets:
        return CiStatus.FAIL
    if "cancel" in buckets:
        return CiStatus.CANCELLED
    if buckets <= {"pass", "skipping"}:
        return CiStatus.PASS
    return CiStatus.PENDING


def _pr_state(state: str) -> PrState:
    """Map GitHub's PR state; anything unrecognised is treated as closed."""

    if state == "OPEN":
        return PrState.OPEN
    return PrState.MERGED if state == "MERGED" else PrState.CLOSED


def _mergeable(mergeable: str, merge_state_status: str) -> Mergeable:
    if mergeable == "CONFLICTING" or merge_state_status == "DIRTY":
        return Mergeable.CONFLICTING
    if mergeable == "MERGEABLE":
        return Mergeable.CLEAN
    return Mergeable.UNKNOWN


def _settled(report: GateReport) -> bool:
    """True when waiting cannot change whether the gate passes."""

    if (
        report.pr_state is not PrState.OPEN
        or not report.head_matches
        or report.base_behind_main
        or report.mergeable is Mergeable.CONFLICTING
    ):
        return True
    return report.ci not in _POLLED_CI and report.mergeable is not Mergeable.UNKNOWN


@dataclass(slots=True)
class MergeGate:
    """Evaluate `check_merge_gate` (§4.2) through an injectable `gh` runner."""

    runner: GhRunner = run_gh
    poll_timeout_s: float = POLL_TIMEOUT_S
    poll_interval_s: float = POLL_INTERVAL_S
    clock: Callable[[], float] = monotonic
    sleep: Callable[[float], Awaitable[Any]] = field(default=asyncio.sleep)

    async def check(self, pr_url: str, expected_head_sha: str) -> GateReport:
        """Report head, CI, base freshness and mergeability for a PR.

        While CI is pending, cancelled or not yet reported, or GitHub has not
        finished computing mergeability, the gate re-reads everything every
        `poll_interval_s` for up to `poll_timeout_s`. It returns at once when
        the answer cannot improve by waiting: the PR is no longer open, the
        head has moved, the base is behind, or the PR conflicts.
        """

        if not SHA_HEX_40_RE.fullmatch(expected_head_sha):
            raise ValueError("expected_head_sha must be a 40-character hex commit SHA")
        ref = PullRequestRef.parse(pr_url)
        expected = expected_head_sha.lower()
        start = self.clock()
        deadline = start + self.poll_timeout_s
        while True:
            report = await self._snapshot(ref, expected)
            remaining = deadline - self.clock()
            if _settled(report) or remaining <= 0:
                return replace(report, elapsed_s=round(self.clock() - start, 3))
            await self.sleep(min(self.poll_interval_s, remaining))

    async def _snapshot(self, ref: PullRequestRef, expected: str) -> GateReport:
        view = await self._json(
            [
                "pr",
                "view",
                ref.url,
                "--json",
                "state,headRefOid,baseRefName,mergeable,mergeStateStatus",
            ]
        )
        head = _string(view, "headRefOid").lower()
        if not SHA_HEX_40_RE.fullmatch(head):
            raise MergeGateError("gh pr view reported a head that is not a commit SHA")
        base_ref = _string(view, "baseRefName")
        merge_state_status = _string(view, "mergeStateStatus")
        checks = await self._checks(ref)
        ci = classify_checks(checks)
        if ci is None:
            # No checks yet is a race right after a push; no workflows at all
            # is a misconfigured repository, and waiting will not fix it.
            workflows = await self._workflow_count(ref)
            ci = CiStatus.NO_CHECKS if workflows else CiStatus.NO_WORKFLOWS
        compare = await self._json(
            ref.api(f"compare/{quote(base_ref, safe='/')}...{head}")
            + [
                "--jq",
                "{behind_by: .behind_by, base_commit: .base_commit.sha,"
                " merge_base_commit: .merge_base_commit.sha}",
            ]
        )
        behind_by = compare.get("behind_by")
        if not isinstance(behind_by, int) or isinstance(behind_by, bool):
            raise MergeGateError("gh api compare did not report behind_by")
        return GateReport(
            pr_url=ref.url,
            pr_state=_pr_state(_string(view, "state")),
            expected_head_sha=expected,
            current_head_sha=head,
            head_matches=head == expected,
            ci=ci,
            checks=checks,
            base_ref=base_ref,
            base_sha=_string(compare, "merge_base_commit").lower(),
            main_sha=_string(compare, "base_commit").lower(),
            base_behind_main=behind_by > 0,
            mergeable=_mergeable(_string(view, "mergeable"), merge_state_status),
            merge_state_status=merge_state_status,
        )

    async def _checks(self, ref: PullRequestRef) -> list[Check]:
        args = ["pr", "checks", ref.url, "--json", "name,bucket,link"]
        result = await self.runner(args)
        text = result.stdout.strip()
        # Parse before trusting the exit code: gh exits non-zero for states
        # that still print a complete check list.
        if text:
            try:
                data: Any = json.loads(text)
            except json.JSONDecodeError:
                data = None
            if isinstance(data, list):
                return [_check(item) for item in data]
        elif result.returncode == 0:
            return []
        if "no checks reported" in result.stderr:
            return []
        raise _failure(args, result)

    async def _workflow_count(self, ref: PullRequestRef) -> int:
        args = ref.api("actions/workflows") + ["--jq", ".total_count"]
        result = await self.runner(args)
        try:
            count: Any = json.loads(result.stdout) if result.returncode == 0 else None
        except json.JSONDecodeError:
            count = None
        if not isinstance(count, int) or isinstance(count, bool):
            raise _failure(args, result)
        return count

    async def _json(self, args: list[str]) -> dict[str, Any]:
        result = await self.runner(args)
        if result.returncode != 0:
            raise _failure(args, result)
        try:
            data: Any = json.loads(result.stdout)
        except json.JSONDecodeError as exc:
            raise MergeGateError(f"{_describe(args)} printed invalid JSON") from exc
        if not isinstance(data, dict):
            raise MergeGateError(f"{_describe(args)} did not print a JSON object")
        return data


def _string(data: dict[str, Any], key: str) -> str:
    value = data.get(key)
    if not isinstance(value, str):
        raise MergeGateError(f"gh output is missing {key}")
    return value


def _check(item: Any) -> Check:
    if not isinstance(item, dict):
        raise MergeGateError("gh pr checks printed a check that is not an object")
    return Check(
        name=str(item.get("name", "")),
        bucket=str(item.get("bucket", "")),
        link=str(item.get("link", "")),
    )


def _describe(args: Sequence[str]) -> str:
    """Name a call for an error: `gh pr view`, or `gh api <path>`."""

    if args[:1] == ["api"] and len(args) >= 3:
        return f"gh api {args[2]}"
    return "gh " + " ".join(args[:2])


def _failure(args: Sequence[str], result: GhResult) -> MergeGateError:
    detail = result.stderr.strip()[:_STDERR_LIMIT] or f"exit {result.returncode}"
    return MergeGateError(f"{_describe(args)} failed: {detail}")
