"""rbm-bench re-review scoring: confirmed references and judge-response validation."""

from __future__ import annotations

import importlib.util
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "rbm_bench_score", ROOT / "scripts/rbm-bench/score.py"
)
assert SPEC is not None and SPEC.loader is not None
SCORE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(SCORE)


def finding(ident: str, severity: str = "blocking", resolved: bool = True) -> dict[str, Any]:
    return {"id": ident, "severity": severity, "resolved": resolved, "text": f"{ident} text"}


def case(*found: dict[str, Any]) -> dict[str, Any]:
    return {"id": "review-1-r1", "truth": {"verdict": "changes requested", "findings": list(found)}}


def verdict(*matches: tuple[str, str], extra: list[dict[str, str]] | None = None) -> dict[str, Any]:
    return {
        "reference": [{"id": ident, "match": match} for ident, match in matches],
        "extra": extra or [],
    }


def test_unresolved_findings_are_not_reference() -> None:
    references = SCORE.reference_findings(case(finding("r1-1"), finding("r1-2", resolved=False)))
    assert [f["id"] for f in references] == ["r1-1"]


def test_uncurated_severity_of_a_confirmed_finding_is_refused() -> None:
    with pytest.raises(ValueError, match="r1-2 is uncurated"):
        SCORE.reference_findings(case(finding("r1-1"), finding("r1-2", severity="unknown")))
    # An unconfirmed finding is not reference, so its severity does not matter.
    SCORE.reference_findings(case(finding("r1-1"), finding("r1-2", "unknown", resolved=False)))


def test_a_judge_response_missing_a_reference_is_not_scorable() -> None:
    problems = SCORE.judge_problems(verdict(("r1-1", "yes")), ["r1-1", "r1-2"])
    assert problems == ["missing reference ids ['r1-2']"]


@pytest.mark.parametrize(
    ("response", "problem"),
    [
        (
            verdict(("r1-1", "yes"), ("r1-1", "yes"), ("r1-2", "no")),
            "duplicate reference id 'r1-1'",
        ),
        (verdict(("r1-1", "yes"), ("r1-2", "no"), ("r9-9", "yes")), "unknown reference id 'r9-9'"),
        (verdict(("r1-1", "maybe"), ("r1-2", "no")), "invalid match 'maybe' for r1-1"),
        (
            verdict(("r1-1", "yes"), ("r1-2", "no"), extra=[{"class": "great"}]),
            "extra entries must each have a class of valid, nit or false_positive",
        ),
        ({"extra": []}, "reference is not a list"),
    ],
)
def test_invalid_judge_responses_are_named(response: dict[str, Any], problem: str) -> None:
    assert problem in SCORE.judge_problems(response, ["r1-1", "r1-2"])


def test_recall_counts_every_blocking_reference() -> None:
    references = [finding("r1-1"), finding("r1-2"), finding("r1-3", "nonblocking")]
    response = verdict(("r1-1", "yes"), ("r1-2", "partial"), ("r1-3", "yes"))
    assert SCORE.blocking_matches(response, references) == (1.5, 2)


def test_score_review_retries_an_invalid_judge_response(monkeypatch: pytest.MonkeyPatch) -> None:
    responses = [verdict(("r1-1", "yes")), verdict(("r1-1", "yes"), ("r1-2", "no"))]
    monkeypatch.setattr(SCORE, "candidate_review", lambda results: "Reviewer agent charlie")
    monkeypatch.setattr(SCORE, "judge", lambda prompt, spec: responses.pop(0))
    score = SCORE.score_review(case(finding("r1-1"), finding("r1-2")), Path("."), "x:y")
    assert score["status"] == "scored"
    assert (score["blocking_caught"], score["blocking_total"], score["recall_blocking"]) == (
        1.0,
        2,
        0.5,
    )
    assert responses == []


def test_score_review_reports_a_judge_that_stays_invalid(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []

    def judge(prompt: str, spec: str) -> dict[str, Any]:
        calls.append(prompt)
        return verdict(("r1-1", "yes"))

    monkeypatch.setattr(SCORE, "candidate_review", lambda results: "Reviewer agent charlie")
    monkeypatch.setattr(SCORE, "judge", judge)
    score = SCORE.score_review(
        case(finding("r1-1"), finding("r1-2"), finding("r1-3", resolved=False)), Path("."), "x:y"
    )
    assert score["status"] == "judge_invalid"
    assert score["problems"] == ["missing reference ids ['r1-2']"]
    assert "recall_blocking" not in score
    assert len(calls) == SCORE.JUDGE_ATTEMPTS
    # The unconfirmed finding is never shown to the judge as reference.
    assert "[r1-3]" not in calls[0]
