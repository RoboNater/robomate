import pytest
from agent_hub_common.models import (
    IMPLEMENTER_RESULT_SCHEMA,
    REBASE_RESULT_SCHEMA,
    REVIEWER_RESULT_SCHEMA,
    SCHEMA_VERSION,
    Finding,
    ImplementerOutcome,
    ImplementerResult,
    RebaseResult,
    ReviewerResult,
    ReviewerVerdict,
    TestResult,
)
from pydantic import ValidationError

VALID_SHA = "0123456789abcdef0123456789abcdef01234567"
VALID_SHA_2 = "abcdef0123456789abcdef0123456789abcdef01"


def test_schema_version_is_one() -> None:
    assert SCHEMA_VERSION == 1


def test_valid_implementer_result_completed() -> None:
    res = ImplementerResult(
        outcome=ImplementerOutcome.COMPLETED,
        pr_url="https://github.com/org/repo/pull/1",
        head_sha=VALID_SHA,
        commits=[f"{VALID_SHA} feat: do something"],
        tests=[TestResult(command="pytest", status="passed")],
        summary="Done successfully",
    )
    assert res.outcome == ImplementerOutcome.COMPLETED
    assert res.pr_url == "https://github.com/org/repo/pull/1"
    assert res.head_sha == VALID_SHA
    assert len(res.commits) == 1
    assert len(res.tests) == 1
    assert res.summary == "Done successfully"


def test_implementer_result_completed_requires_pr_url_and_head_sha() -> None:
    with pytest.raises(ValidationError, match="pr_url"):
        ImplementerResult(
            outcome=ImplementerOutcome.COMPLETED,
            pr_url=None,
            head_sha=VALID_SHA,
            summary="Done",
        )

    with pytest.raises(ValidationError, match="pr_url"):
        ImplementerResult(
            outcome=ImplementerOutcome.COMPLETED,
            pr_url="",
            head_sha=VALID_SHA,
            summary="Done",
        )

    with pytest.raises(ValidationError, match="head_sha"):
        ImplementerResult(
            outcome=ImplementerOutcome.COMPLETED,
            pr_url="https://github.com/org/repo/pull/1",
            head_sha=None,
            summary="Done",
        )

    with pytest.raises(ValidationError):
        ImplementerResult(
            outcome=ImplementerOutcome.COMPLETED,
            pr_url="https://github.com/org/repo/pull/1",
            head_sha="",
            summary="Done",
        )


@pytest.mark.parametrize(
    "invalid_sha",
    [
        "abc1234",  # too short (7 chars)
        "0123456789",  # too short (10 chars)
        "g" * 40,  # non-hex char
        "0123456789abcdef0123456789abcdef0123456g",  # non-hex char at end
        "0" * 41,  # too long (41 chars)
        "0" * 39,  # too short (39 chars)
        "   0123456789abcdef0123456789abcdef01234567   ",  # whitespace
    ],
)
def test_sha_pattern_rejections(invalid_sha: str) -> None:
    # ImplementerResult rejects invalid head_sha
    with pytest.raises(ValidationError):
        ImplementerResult(
            outcome=ImplementerOutcome.COMPLETED,
            pr_url="https://github.com/org/repo/pull/1",
            head_sha=invalid_sha,
            summary="Done",
        )

    # ReviewerResult rejects invalid reviewed_head_sha
    with pytest.raises(ValidationError):
        ReviewerResult(
            verdict=ReviewerVerdict.APPROVED,
            reviewed_head_sha=invalid_sha,
            blocking_findings=[],
            summary="LGTM",
        )


def test_implementer_result_blocked_or_failed_does_not_require_pr_url() -> None:
    blocked = ImplementerResult(
        outcome=ImplementerOutcome.BLOCKED,
        blocker="Need credentials",
        summary="Blocked on credentials",
    )
    assert blocked.outcome == ImplementerOutcome.BLOCKED
    assert blocked.pr_url is None
    assert blocked.head_sha is None

    failed = ImplementerResult(
        outcome=ImplementerOutcome.FAILED,
        summary="Build failed",
    )
    assert failed.outcome == ImplementerOutcome.FAILED


def test_valid_reviewer_result_approved() -> None:
    res = ReviewerResult(
        verdict=ReviewerVerdict.APPROVED,
        pr_url="https://github.com/org/repo/pull/1",
        review_url="https://github.com/org/repo/pull/1#review-1",
        reviewed_head_sha=VALID_SHA,
        blocking_findings=[],
        nonblocking_findings=[Finding(id="r1-1", text="Minor nit")],
        summary="LGTM",
    )
    assert res.verdict == ReviewerVerdict.APPROVED
    assert res.reviewed_head_sha == VALID_SHA
    assert len(res.blocking_findings) == 0
    assert len(res.nonblocking_findings) == 1


def test_reviewer_result_approved_requires_reviewed_head_sha_and_empty_blocking_findings() -> None:
    with pytest.raises(ValidationError, match="reviewed_head_sha"):
        ReviewerResult(
            verdict=ReviewerVerdict.APPROVED,
            reviewed_head_sha=None,
            blocking_findings=[],
            summary="LGTM",
        )

    with pytest.raises(ValidationError, match="blocking_findings to be empty"):
        ReviewerResult(
            verdict=ReviewerVerdict.APPROVED,
            reviewed_head_sha=VALID_SHA,
            blocking_findings=[Finding(id="r1-1", text="Must fix this")],
            summary="LGTM",
        )


def test_reviewer_result_changes_requested() -> None:
    res = ReviewerResult(
        verdict=ReviewerVerdict.CHANGES_REQUESTED,
        pr_url="https://github.com/org/repo/pull/1",
        reviewed_head_sha=VALID_SHA,
        blocking_findings=[Finding(id="r1-1", text="Null pointer exception on line 10")],
        summary="Changes requested",
    )
    assert res.verdict == ReviewerVerdict.CHANGES_REQUESTED
    assert len(res.blocking_findings) == 1


def test_finding_id_pattern_validation() -> None:
    Finding(id="r1-1", text="A finding")
    Finding(id="r2-15", text="A finding")

    with pytest.raises(ValidationError):
        Finding(id="finding-1", text="Invalid id")

    with pytest.raises(ValidationError):
        Finding(id="r1", text="Invalid id")


def test_exported_json_schemas() -> None:
    assert IMPLEMENTER_RESULT_SCHEMA["type"] == "object"
    assert "outcome" in IMPLEMENTER_RESULT_SCHEMA["properties"]
    assert REVIEWER_RESULT_SCHEMA["type"] == "object"
    assert "verdict" in REVIEWER_RESULT_SCHEMA["properties"]
    assert {"outcome", "head_sha", "conflict_files", "resolution_summary"} <= set(
        REBASE_RESULT_SCHEMA["properties"]
    )


def test_a_clean_rebase_reports_its_new_head_and_no_conflicts() -> None:
    res = RebaseResult(outcome=ImplementerOutcome.COMPLETED, head_sha=VALID_SHA, summary="Rebased")

    assert res.conflict_files == []
    assert res.resolution_summary is None


def test_a_completed_rebase_requires_its_head_sha() -> None:
    with pytest.raises(ValidationError, match="head_sha"):
        RebaseResult(outcome=ImplementerOutcome.COMPLETED, summary="Rebased")


@pytest.mark.parametrize("resolution", [None, "", "   "])
def test_resolved_conflicts_must_say_how_they_were_resolved(resolution: str | None) -> None:
    with pytest.raises(ValidationError, match="resolution_summary"):
        RebaseResult(
            outcome=ImplementerOutcome.COMPLETED,
            head_sha=VALID_SHA,
            conflict_files=["packages/hub/src/agent_hub/database.py"],
            resolution_summary=resolution,
            summary="Rebased",
        )

    res = RebaseResult(
        outcome=ImplementerOutcome.COMPLETED,
        head_sha=VALID_SHA,
        conflict_files=["packages/hub/src/agent_hub/database.py"],
        resolution_summary="Kept both migrations; schema version from Reservations.",
        summary="Rebased",
    )
    assert res.conflict_files == ["packages/hub/src/agent_hub/database.py"]


def test_a_blocked_rebase_needs_no_head() -> None:
    res = RebaseResult(
        outcome=ImplementerOutcome.BLOCKED,
        blocker="main rewrote the module this PR changes",
        summary="Cannot rebase without a design decision",
    )

    assert res.head_sha is None
