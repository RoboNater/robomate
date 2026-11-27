"""Typed run evidence, export lifecycle and operator run selection."""

import json
import os
from pathlib import Path

import pytest
from agent_hub import create_app
from agent_hub.closeout_export import fenced
from agent_hub.database import database, initialize_database
from agent_hub.store import HubStore, NotFoundError
from agent_hub_common import (
    AgentProfile,
    Finding,
    HubSettings,
    ImplementerOutcome,
    ImplementerResult,
    RebaseResult,
    ResolvedConfig,
    ReviewerResult,
    ReviewerVerdict,
    TaskState,
)
from delivery_support import HEAD, MERGED, PR, finish_run
from fastapi import FastAPI

EARLY_HEAD = "a" * 40
REBASED_HEAD = "3" * 40
HOSTILE = "before\n``````\n<img src=x onerror=alert(1)>\n[link](javascript:alert(1))\x1b[2J\u202e"
REVIEW_ONE = f"{PR}#discussion_r17"
REVIEW_TWO = f"{PR}#pullrequestreview-18"


def report_file(store: HubStore, run_id: str) -> Path:
    return store._state_dir() / "runs" / run_id / "report.md"


def finding_section(markdown: str, finding_id: str) -> str:
    return markdown.split(f"### {finding_id}\n", 1)[1].split("\n##", 1)[0]


def test_typed_snapshot_keeps_every_finding_and_step(
    app: FastAPI, monkeypatch: pytest.MonkeyPatch
) -> None:
    store: HubStore = app.state.store
    initialize_database(store.path)
    store.issue_url_base = "https://github.com/octo/sandbox/issues/"
    workflow_id = store.start_workflow("Address issue `octo/sandbox#19`.", source="submit:issue")
    started = store.get_workflow(workflow_id)
    run_id, sha = started["run_id"], started["statement_sha256"]
    store.join_orchestrator("alice", "session", AgentProfile(harness="claude-code"))
    store.check_in("bob", profile=AgentProfile(harness="codex", model="worker-model"))
    store.check_in("charlie", profile=AgentProfile(harness="opencode", provider="review-provider"))
    config = ResolvedConfig(
        nits_issue=4, closeout_steps=("First recorded step", "Second recorded step")
    )
    monkeypatch.setattr("agent_hub_common.load_config_layers", lambda **kwargs: config)

    first = store.assign_task("charlie", "reviewer", "First review", "", pr_head_sha=EARLY_HEAD)
    store.submit_result(
        first.id,
        "charlie",
        ReviewerResult(
            verdict=ReviewerVerdict.CHANGES_REQUESTED,
            pr_url=PR,
            reviewed_head_sha=EARLY_HEAD,
            review_url=REVIEW_ONE,
            blocking_findings=[
                Finding(id="r1-1", text=HOSTILE),
                Finding(id="r1-2", text="Blocking with no later disposition"),
            ],
            nonblocking_findings=[
                Finding(id="r1-3", text="Nonblocking fixed during implementation"),
                Finding(id="r1-4", text="Disputed nonblocking finding"),
            ],
            summary="Review prose stays private",
        ),
    )
    work = store.assign_task("bob", "implementer", "Address", "Private instructions")
    store.submit_result(
        work.id,
        "bob",
        ImplementerResult(
            outcome=ImplementerOutcome.COMPLETED,
            pr_url=PR,
            head_sha=HEAD,
            summary="Address findings",
            resolved_finding_ids=["r1-1", "r1-3"],
            disputed_finding_ids=["r1-4"],
        ),
    )
    second = store.assign_task("charlie", "reviewer", "Second review", "", pr_head_sha=HEAD)
    store.submit_result(
        second.id,
        "charlie",
        ReviewerResult(
            verdict=ReviewerVerdict.APPROVED,
            pr_url=PR,
            reviewed_head_sha=HEAD,
            review_url=REVIEW_TWO,
            nonblocking_findings=[
                Finding(id="r1-4", text="Disputed ID seen again"),
                Finding(id="r2-1", text="Follow-up issue required"),
                Finding(id="r2-2", text="Nit issue destination"),
            ],
            summary="Approve HEAD",
        ),
    )
    close = store.assign_task("bob", "closeout", "Close out", "")
    draft = store.read_run_report()
    assert draft.startswith("# Draft run report\n")
    assert not report_file(store, run_id).exists()
    assert "Unresolved findings · 4" in draft
    assert "Incomplete closeout steps · 2" in draft
    assert "Finding status · unresolved" in finding_section(draft, "r2-1")
    assert "Implementer response · disputed" in finding_section(draft, "r1-4")

    issue_url = "https://github.com/octo/sandbox/issues/20"
    nit_url = "https://github.com/octo/sandbox/issues/4#issuecomment-9"
    step_url = f"{PR}#issuecomment-12"
    store.submit_result(
        close.id,
        "bob",
        {
            "outcome": "completed",
            "summary": "Closeout body stays private",
            "items": [
                {
                    "source": "r1-4",
                    "action": "none",
                    "summary": "Declined",
                    "reason": "Out of scope",
                },
                {"source": "r2-1", "action": "issue", "summary": "Filed", "url": issue_url},
                {"source": "r2-2", "action": "nit", "summary": "Noted", "url": nit_url},
                {
                    "source": "orchestrator",
                    "action": "none",
                    "summary": "Separate observation",
                    "reason": "Documented already",
                },
            ],
            "steps": [
                {"index": 0, "done": True, "url": step_url},
                {"index": 1, "done": False, "note": "Deferred to the operator"},
            ],
        },
    )
    finish_run(store, "Done", actor="alice", session="session")
    final = store.read_run_report()
    assert final.startswith("# Final run report\n")
    assert final == report_file(store, run_id).read_text(encoding="utf-8")
    assert "Retrospective collection not requested." in final
    assert "Source\n\n" + fenced("submit:issue") in final
    assert "Link\n\n" + fenced("https://github.com/octo/sandbox/issues/19") in final
    assert "SHA-256\n\n" + fenced(sha) in final
    assert "Approved head SHA\n\n" + fenced(HEAD) in final
    assert "Merged SHA\n\n" + fenced(MERGED) in final
    assert "PR/MR\n\n" + fenced(PR) in final
    assert '"deliver": "merge"' in final
    assert all(
        fenced(value) in final for value in ("claude-code", "worker-model", "review-provider")
    )
    assert final.index(REVIEW_ONE) < final.index(REVIEW_TWO)
    assert fenced(HOSTILE) in finding_section(final, "r1-1")
    assert "\x1b" not in final and "\u202e" not in final
    assert "\\u001b[2J" in final and "\\u202e" in final
    for fid in ("r1-1", "r1-3"):
        assert "Finding status · resolved by implementer" in finding_section(final, fid)
        assert fenced(work.id) in finding_section(final, fid)
    assert "Finding status · unresolved" in finding_section(final, "r1-2")
    declined = finding_section(final, "r1-4")
    assert "Disposition · none" in declined and fenced("Out of scope") in declined
    assert (
        declined.index("Disputed nonblocking finding")
        < declined.index("Implementer response · disputed")
        < declined.index("Disputed ID seen again")
        < declined.index("Disposition · none")
    )
    assert "Disposition · issue" in finding_section(final, "r2-1")
    assert issue_url in finding_section(final, "r2-1")
    assert "Disposition · nit" in finding_section(final, "r2-2")
    assert nit_url in finding_section(final, "r2-2")
    assert fenced("Documented already") in final
    assert "Step 0 · done" in final and step_url in final
    assert "Step 1 · INCOMPLETE" in final and fenced("Deferred to the operator") in final
    assert fenced("Second recorded step") in final

    status = store.status_summary()
    assert status["run"]["delivery_state"] == "merged"
    assert status["run"]["unresolved_findings"] == 1
    assert status["run"]["incomplete_closeout_steps"] == 1
    assert "Delivery · merged" in final
    assert "Unresolved findings · 1" in final and "Incomplete closeout steps · 1" in final
    for text in (HOSTILE, issue_url, "Out of scope", "Closeout body stays private"):
        assert text not in json.dumps(status)
    assert "Private instructions" not in final


def test_merge_record_comment_and_run_report_read_the_same_delivery_facts(
    store: HubStore,
) -> None:
    """#707/#452: the comment's delivery facts equal the report's, including a clean rebase."""

    store.check_in("charlie")
    store.check_in("bob")
    review = store.assign_task("charlie", "reviewer", "Review", "", pr_head_sha=HEAD)
    store.submit_result(
        review.id,
        "charlie",
        ReviewerResult(
            verdict=ReviewerVerdict.APPROVED,
            pr_url=PR,
            reviewed_head_sha=HEAD,
            review_url=REVIEW_ONE,
            summary="Approve",
        ),
    )
    rebase = store.assign_task("bob", "rebase", "Rebase", "", pr_head_sha=HEAD)
    store.submit_result(
        rebase.id,
        "bob",
        RebaseResult(
            outcome=ImplementerOutcome.COMPLETED,
            pr_url=PR,
            head_sha=REBASED_HEAD,
            conflict_files=[],
            summary="Clean rebase",
        ),
    )
    finish_run(store, "Done", actor="alice", session=None)

    state = store.get_state()
    final_approval = [
        task["result"]
        for task in state["tasks"]
        if task["role"] == "reviewer"
        and task["result"] is not None
        and task["result"].get("verdict") == ReviewerVerdict.APPROVED
    ][-1]
    facts = {
        "PR/MR": state["workflow"]["pr_url"],
        "Approved head SHA": state["workflow"]["approved_sha"],
        "Merged SHA": state["workflow"]["delivery_merged_sha"],
        "Reviewed head": final_approval["reviewed_head_sha"],
        "Review URL": final_approval["review_url"],
    }
    assert facts["Reviewed head"] == HEAD
    assert facts["Approved head SHA"] == REBASED_HEAD
    assert facts["Reviewed head"] != facts["Approved head SHA"]
    comment = (
        "Merge record from the orchestrator: "
        f"reviewed head {facts['Reviewed head']} ({facts['Review URL']}); "
        f"approved head {facts['Approved head SHA']} after a clean rebase; "
        f"merged {facts['Merged SHA']} in {facts['PR/MR']}."
    )
    report = store.read_run_report()
    for label, value in facts.items():
        assert value is not None
        assert value in comment
        assert f"{label}\n\n{fenced(value)}" in report, f"report disagrees on {label}"


@pytest.mark.parametrize("outcome", ["blocked", "failed"])
def test_draft_keeps_failed_or_blocked_closeout_until_retry(
    hub_store: HubStore, monkeypatch: pytest.MonkeyPatch, outcome: str
) -> None:
    store = hub_store
    store.check_in("bob")
    config = ResolvedConfig(closeout_steps=("Replaced checklist",))
    monkeypatch.setattr("agent_hub_common.load_config_layers", lambda **kwargs: config)
    failed = store.assign_task("bob", "closeout", "First attempt", "")
    store.submit_result(
        failed.id,
        "bob",
        {"outcome": outcome, "summary": "Retry", "blocker": "Temporary failure"},
    )
    previous = store.read_run_report()
    assert f"Closeout attempt · {outcome}" in previous
    assert fenced("Retry") in previous and fenced("Temporary failure") in previous
    config = ResolvedConfig(closeout_steps=("Current step A", "Current step B"))
    store.assign_task("bob", "closeout", "Second attempt", "")

    draft = store.read_run_report()
    assert fenced("Current step A") in draft and fenced("Current step B") in draft
    assert "Replaced checklist" not in draft and "Temporary failure" not in draft
    assert store.status_summary()["run"]["incomplete_closeout_steps"] == 2


def test_default_and_explicit_selection_with_live_and_queued(store: HubStore) -> None:
    first = store.get_state()["run_id"]
    finish_run(store, "First done", actor="alice", session=None)
    assert fenced(first) in store.read_run_report()
    queued = store.get_workflow(store.start_workflow("Queued", force_queued=True))["run_id"]
    assert fenced(first) in store.read_run_report()
    assert "# Draft run report" in store.read_run_report(queued)
    store.start_workflow()
    successor = store.get_workflow(store.start_workflow("Next queued"))["run_id"]
    assert fenced(queued) in store.read_run_report()
    assert fenced(successor) not in store.read_run_report()
    assert fenced(first) in store.read_run_report(first)
    assert not report_file(store, queued).exists() and not report_file(store, successor).exists()
    with pytest.raises(NotFoundError, match="unknown run"):
        store.read_run_report("20260101_cafebabe")


@pytest.mark.parametrize("value", ["../report.md", "20260101_aaaa0000/..", r"a\b", "", "2026\n"])
def test_invalid_run_id_is_refused(store: HubStore, value: str) -> None:
    with pytest.raises(ValueError, match="invalid run ID"):
        store.read_run_report(value)


def test_legacy_completed_run_projects_missing_facts_without_reexport(store: HubStore) -> None:
    run_id = store.get_state()["run_id"]
    store.check_in("charlie")
    task = store.assign_task(
        "charlie", "reviewer", "Review", "Never derive facts from this prose", pr_head_sha=HEAD
    )
    store.submit_result(task.id, "charlie", TaskState.COMPLETED, "Plain legacy result")
    with database(store.path) as connection:
        connection.execute(
            "UPDATE workflow SET status = 'done', statement_sha256 = NULL, exported_at = created"
            " WHERE run_id = ?",
            (run_id,),
        )
    store.retry_closeout_exports()
    markdown = store.read_run_report(run_id)
    assert markdown.startswith("# Final run report\n")
    assert "Delivery · not_recorded" in markdown
    assert "Typed review result not recorded." in markdown
    assert "Completed typed closeout result not recorded." in markdown
    assert "SHA-256\n\nnot recorded" in markdown
    assert "Plain legacy result" not in markdown and "Never derive facts" not in markdown
    assert "PR/MR\n\nnot recorded" in markdown
    assert not report_file(store, run_id).exists()


def test_disabled_closeout_and_pr_only_delivery(tmp_path: Path) -> None:
    store = HubStore(tmp_path / "hub.db")
    initialize_database(store.path)
    with pytest.raises(NotFoundError, match="no current or completed run"):
        store.read_run_report()
    workflow_id = store.start_workflow("Work", policy={"closeout": False, "deliver": "pr"})
    target = report_file(store, store.get_workflow(workflow_id)["run_id"])
    finish_run(store, "Delivered open PR", actor="alice", session=None)
    report = target.read_text(encoding="utf-8")
    assert "Delivery · pr_open" in report
    assert "Typed closeout disabled by run policy." in report
    assert "Retrospective collection not requested." in report
    assert "Merged SHA\n\nnot recorded" in report
    assert "Incomplete closeout steps · 0" in report
    assert not (target.parent / "closeout").exists()
    assert store.status_summary()["run"]["delivery_state"] == "pr_open"


async def test_interrupted_report_export_retries_on_restart_and_honors_deletion(
    app: FastAPI,
    hub_store: HubStore,
    settings: HubSettings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_id = hub_store.get_state()["run_id"]
    report = report_file(hub_store, run_id)
    replace = os.replace

    def interrupt(src: str, dest: Path) -> None:
        if dest == report:
            raise PermissionError("interrupt report replacement")
        replace(src, dest)

    with monkeypatch.context() as patch:
        patch.setattr(os, "replace", interrupt)
        finish_run(hub_store, "Done", actor="alice", session=None)
    assert not report.exists()
    assert not list(report.parent.glob(".closeout-*"))
    assert hub_store.status_summary()["workflow"]["status"] == "done"
    assert hub_store.status_summary()["closeout"]["export_error"] == "PermissionError"
    assert hub_store.status_summary()["closeout_export_errors"] == [
        {"run_id": run_id, "error": "PermissionError"},
    ]
    restarted = create_app(settings)
    async with restarted.router.lifespan_context(restarted):
        store = restarted.state.store
        assert report.read_text(encoding="utf-8") == store.read_run_report(run_id)
        assert store.status_summary()["closeout_export_errors"] == []
        if os.name == "posix":
            assert report.stat().st_mode & 0o777 == 0o600
            assert report.parent.stat().st_mode & 0o777 == 0o700

    original = report.read_bytes()
    report.unlink()
    restarted = create_app(settings)
    async with restarted.router.lifespan_context(restarted):
        assert not report.exists()
        assert restarted.state.store.read_run_report(run_id).encode() == original
