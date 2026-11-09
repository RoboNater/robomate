"""Operator closeout views on pytest-owned isolated hubs (#356)."""

import json
import os
import re
from pathlib import Path
from typing import Any

import pytest
from agent_hub import create_app
from agent_hub.closeout_export import HEADER, fenced, literal
from agent_hub.database import database
from agent_hub.store import HubStore
from agent_hub_common import HubSettings, TaskState, WorkflowStatus
from fastapi import FastAPI
from timing import FrozenClock


def report(summary: str = "Completed work.") -> dict[str, Any]:
    return {"summary": summary, "outstanding_items": [], "lessons": [], "claims": []}


def prepare(store: HubStore) -> str:
    for name in ("bob", "charlie"):
        store.check_in(name, worker_instance_id=name + "-instance")
        task = store.assign_task(name, "implementer", "Work", "Work")
        store.submit_result(task.id, name, TaskState.COMPLETED, "Done")
    run_id = str(store.get_state()["workflow"]["run_id"])
    store.request_closeout_reports(run_id, "alice", "alice-session", 100)
    return run_id


def submit(store: HubStore, run_id: str, author: str, body: dict[str, Any]) -> None:
    agent = store.agent_by_name(author)
    assert agent is not None or author == "alice"
    session = "alice-session" if agent is None else agent.worker_instance_id
    store.submit_closeout_report(
        run_id, author, session, "orchestrator" if author == "alice" else "worker", body
    )


def done(store: HubStore) -> None:
    store.set_workflow_status(WorkflowStatus.DONE, "Done", actor="alice", session="alice-session")


def export_dir(store: HubStore, run_id: str) -> Path:
    return store._state_dir() / "runs" / run_id / "closeout"


@pytest.mark.parametrize("crash", [False, True])
async def test_export_status_restart_and_stopped_files(
    app: FastAPI,
    hub_store: HubStore,
    settings: HubSettings,
    frozen_clock: FrozenClock,
    crash: bool,
) -> None:
    async with app.router.lifespan_context(app):
        assert hub_store.status_summary()["closeout"]["state"] == "not_requested"
        run_id = prepare(hub_store)
        submit(hub_store, run_id, "bob", report("Built."))
        if crash:
            frozen_clock.advance(settings.lost_after_s + 101)
            bob = hub_store.agent_by_name("bob")
            assert bob is not None
            hub_store.heartbeat("bob", bob.worker_instance_id, None)
            hub_store.sweep(settings.lost_after_s)
        else:
            submit(hub_store, run_id, "charlie", report("Reviewed."))
        submit(hub_store, run_id, "alice", report("Orchestrated."))
        status = hub_store.status_summary()["closeout"]
        assert status["state"] == "collecting"
        assert status["submitted_count"] == (2 if crash else 3)
        assert status["expected_count"] == 3
        assert status["missing"] == (
            [{"author": "charlie", "reason": "worker_lost"}] if crash else []
        )
        assert {r["timing"] for r in status["submitted"]} == {
            "after_closeout",
            "collected_before_closeout",
        }
        assert "Built." not in json.dumps(status)
        stored = hub_store.get_closeout_reports(run_id)
        done(hub_store)
        final_status = hub_store.status_summary()["closeout"]
        assert final_status["state"] == "finalized"
        directory = export_dir(hub_store, run_id)
        index = json.loads((directory / "index.json").read_text())
        assert len(index["authors"]) == 3
        assert len(list(directory.glob("*.md"))) == len(stored["reports"])
        for saved in stored["reports"]:
            entry = next(e for e in index["authors"] if e["author"] == saved["author"])
            assert entry["report_id"] == saved["report_id"]
            assert entry["submitted_at"] == saved["submitted_at"]
            assert entry["timing"] == saved["timing"]
            content = (directory / entry["filename"]).read_text()
            assert HEADER in content
            assert fenced(saved["report"]["summary"]) in content
        if crash:
            assert next(e for e in index["authors"] if e["author"] == "charlie") == {
                "author": "charlie",
                "author_kind": "worker",
                "status": "missing",
                "reason": "worker_lost",
            }
        if os.name == "posix":
            assert directory.stat().st_mode & 0o777 == 0o700
            assert all(p.stat().st_mode & 0o777 == 0o600 for p in directory.iterdir())
    # Files can be read after the owning hub stops, with no DB or HTTP access.
    before = {p.name: p.read_bytes() for p in directory.iterdir()}
    restarted = create_app(settings)
    frozen_clock.install(restarted.state.store)
    async with restarted.router.lifespan_context(restarted):
        assert restarted.state.store.status_summary()["closeout"] == final_status
    assert {p.name: p.read_bytes() for p in directory.iterdir()} == before


def test_status_is_pure_even_after_deadline(store: HubStore, frozen_clock: FrozenClock) -> None:
    prepare(store)
    frozen_clock.advance(101)
    with database(store.path) as connection:
        before = list(connection.iterdump())
    status = store.status_summary()["closeout"]
    assert status["missing"] == [
        {"author": "alice", "reason": "awaiting_report"},
        {"author": "bob", "reason": "deadline_expired"},
        {"author": "charlie", "reason": "deadline_expired"},
    ]
    with database(store.path) as connection:
        assert list(connection.iterdump()) == before


async def test_export_failure_does_not_block_done_and_startup_retries(
    app: FastAPI,
    hub_store: HubStore,
    settings: HubSettings,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    from agent_hub import closeout_export

    async with app.router.lifespan_context(app):
        run_id = prepare(hub_store)
        submit(hub_store, run_id, "bob", report())
        with monkeypatch.context() as patch:

            def fail(*args: Any) -> None:
                raise PermissionError("test failure")

            patch.setattr(closeout_export, "atomic_write", fail)
            done(hub_store)
        assert hub_store.get_state()["workflow"]["status"] == "done"
        assert hub_store.status_summary()["closeout"]["export_error"] == "PermissionError"
        assert "Closeout export failed" in caplog.text
        assert not (export_dir(hub_store, run_id) / "index.json").exists()
    restarted = create_app(settings)
    async with restarted.router.lifespan_context(restarted):
        assert restarted.state.store.status_summary()["closeout"]["export_error"] is None
    assert (export_dir(hub_store, run_id) / "index.json").is_file()


def test_untrusted_content_and_colliding_unsafe_author_names(store: HubStore) -> None:
    run_id = prepare(store)
    hostile = (
        "\x1b[31m\x00\x85\u202e\u2028\u2029\n# heading <script>alert(1)</script> "
        "[link](javascript:evil) https://evil.test\n```\n~~~~\n``````\n"
        "0123456789abcdef0123456789abcdef01234567"
    )
    body = report(hostile)
    source_url = "https://evil.test/issues/1#<script>"
    body["outstanding_items"] = [
        {"summary": hostile, "reason": hostile},
        {"summary": hostile, "url": source_url},
    ]
    body["lessons"] = [hostile]
    body["claims"] = [
        {"text": hostile, "verification": "verified", "evidence": hostile},
        {"text": hostile, "verification": "unverified"},
    ]
    for name in ("bob", "charlie", "alice"):
        submit(store, run_id, name, body)
    # Normal API validates names; also cover legacy/corrupt imported author data.
    with database(store.path) as connection:
        connection.execute("PRAGMA defer_foreign_keys = ON")
        for old, new in (("bob", "../a/b\x1b\u202e"), ("charlie", "../a_b\x1b\u202e")):
            for table in ("closeout_expected", "agent_closeout_report"):
                connection.execute(f"UPDATE {table} SET author = ? WHERE author = ?", (new, old))
    done(store)
    directory = export_dir(store, run_id)
    index_text = (directory / "index.json").read_text()
    entries = json.loads(index_text)["authors"]
    assert len({e["filename"] for e in entries}) == 3
    for entry in entries:
        assert re.fullmatch(r"report-[0-9]+\.md", entry["filename"])
        content = (directory / entry["filename"]).read_text()
        assert not any(c in content for c in ("\x1b", "\x00", "\x85", "\u202e", "\u2028", "\u2029"))
        assert "not certified by the hub" in content
        assert "Claim 1 (verified)" in content and "Claim 2 (unverified)" in content
        # Track CommonMark fences and compare the blocks with intended values.
        blocks: list[str] = []
        outside: list[str] = []
        current: list[str] | None = None
        fence_len = 0
        for line in content.splitlines():
            if current is None:
                if re.fullmatch(r"`{3,}", line):
                    fence_len = len(line)
                    current = []
                else:
                    outside.append(line)
            elif re.fullmatch(r"`+ *", line) and len(line.rstrip()) >= fence_len:
                blocks.append("\n".join(current))
                current = None
            else:
                current.append(line)
        assert current is None
        assert all(
            marker not in "\n".join(outside)
            for marker in ("# heading", "<script>", "https://evil.test", "~~~~")
        )
        assert blocks.count(literal(hostile)) == 8
        assert source_url in blocks
        assert "\n# heading" in blocks[4]
        assert "0123456789abcdef0123456789abcdef01234567" in blocks[4]
    assert "\x1b" not in json.dumps(store.status_summary()["closeout"], ensure_ascii=False)
    assert not (directory.parent / "a").exists()


def test_scripted_closeout_real_processes(
    tmp_path: Path,
    frozen_clock: FrozenClock,
) -> None:
    """Real HTTP workers + stdio MCP Alice, one killed worker, stopped exports."""
    import subprocess
    import sys
    from contextlib import ExitStack

    import httpx
    from conftest import TOKEN, reserve_port_block
    from timing import LIVENESS_S, wait_until_sync

    root = Path(__file__).resolve().parents[1]
    state = tmp_path / "state"
    state.mkdir()
    port = reserve_port_block()
    url = f"http://127.0.0.1:{port}"
    env = {
        **os.environ,
        "ROBOMATE_HUB_URL": url,
        "ROBOMATE_TOKEN": TOKEN,
        "PYTHONIOENCODING": "utf-8",
    }
    processes: list[subprocess.Popen[str]] = []
    with ExitStack() as stack:

        def launch(label: str, args: list[str]) -> subprocess.Popen[str]:
            log = stack.enter_context((tmp_path / f"{label}.log").open("w", encoding="utf-8"))
            process = subprocess.Popen(
                args, env=env, cwd=tmp_path, stdout=log, stderr=subprocess.STDOUT, text=True
            )
            processes.append(process)
            return process

        def status() -> dict[str, Any]:
            result = subprocess.run(
                [sys.executable, "-m", "robomate.cli", "status", "--json"],
                env=env,
                cwd=tmp_path,
                capture_output=True,
                text=True,
                timeout=LIVENESS_S,
            )
            assert result.returncode == 0, result.stderr
            parsed: dict[str, Any] = json.loads(result.stdout)
            return parsed

        hub = launch(
            "hub",
            [
                sys.executable,
                str(root / "tests/fixtures/closeout_hub.py"),
                str(state),
                str(port),
                frozen_clock.start.isoformat(),
            ],
        )
        http = stack.enter_context(httpx.Client(base_url=url, timeout=LIVENESS_S))

        def ready() -> bool:
            try:
                return http.get("/healthz").status_code == 200
            except httpx.TransportError:
                return False

        try:
            wait_until_sync(ready, "isolated subprocess hub serves healthz")
            alice = launch(
                "alice",
                [
                    sys.executable,
                    str(root / "scripts/mock-alice.py"),
                    "--mcp",
                    "--closeout",
                    "--timeout",
                    str(LIVENESS_S),
                    "--agent",
                    "bob",
                    "--harness",
                    "codex",
                ],
            )
            wait_until_sync(lambda: status()["workflow"], "Alice starts workflow")
            casualty = launch(
                "charlie",
                [
                    sys.executable,
                    str(root / "scripts/mock-worker.py"),
                    "--hub-url",
                    url,
                    "--token",
                    TOKEN,
                    "--agent",
                    "charlie",
                    "--runtime",
                    "codex",
                    "--timeout",
                    str(LIVENESS_S),
                ],
            )
            wait_until_sync(
                lambda: any(a["name"] == "charlie" for a in status()["agents"]),
                "second worker joined the run",
            )
            casualty.kill()
            casualty.wait(timeout=LIVENESS_S)
            bob = launch(
                "bob",
                [
                    sys.executable,
                    str(root / "scripts/mock-worker.py"),
                    "--hub-url",
                    url,
                    "--token",
                    TOKEN,
                    "--agent",
                    "bob",
                    "--runtime",
                    "codex",
                    "--timeout",
                    str(LIVENESS_S),
                ],
            )
            before = wait_until_sync(
                lambda: s if (s := status())["closeout"]["submitted_count"] == 1 else None,
                "worker report collected while killed worker remains missing",
            )
            assert before is not None
            assert before["closeout"]["state"] == "collecting"
            assert before["closeout"]["expected_count"] == 3
            # Drive expiry and loss explicitly, through this test-only fixture.
            frozen_clock.advance(161)
            assert http.post("/test/advance", json={"seconds": frozen_clock.elapsed}).is_success
            wait_until_sync(lambda: alice.poll() is not None, "mock Alice completes closeout")
            assert alice.returncode == 0, (tmp_path / "alice.log").read_text()
            wait_until_sync(lambda: bob.poll() is not None, "mock worker receives release")
            assert bob.returncode == 0, (tmp_path / "bob.log").read_text()
            after = status()
            assert after["workflow"]["status"] == "done"
            assert after["closeout"]["state"] == "finalized"
            assert after["closeout"]["submitted_count"] == 2
            assert after["closeout"]["missing"] == [{"author": "charlie", "reason": "worker_lost"}]
            hub.terminate()
            hub.wait(timeout=LIVENESS_S)
            assert hub.poll() is not None
            run_id = after["run_id"]
            saved = HubStore(state / "hub.db").get_closeout_reports(run_id)
            directory = state / "runs" / run_id / "closeout"
            entries = json.loads((directory / "index.json").read_text())["authors"]
            assert len(entries) == 3
            for receipt in saved["reports"]:
                entry = next(e for e in entries if e["author"] == receipt["author"])
                assert entry["report_id"] == receipt["report_id"]
                assert entry["submitted_at"] == receipt["submitted_at"]
                assert (
                    fenced(receipt["report"]["summary"])
                    in (directory / entry["filename"]).read_text()
                )
            missing = next(e for e in entries if e["author"] == "charlie")
            assert missing["status"] == "missing" and missing["reason"] == "worker_lost"
        finally:
            for process in reversed(processes):
                if process.poll() is None:
                    process.terminate()
                process.wait(timeout=LIVENESS_S)


def test_atomic_replacement_failure_preserves_existing_export(
    store: HubStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_id = prepare(store)
    submit(store, run_id, "bob", report())
    done(store)
    directory = export_dir(store, run_id)
    original = {p.name: p.read_bytes() for p in directory.iterdir()}

    def fail_replace(*args: Any) -> None:
        raise PermissionError("test replacement failure")

    with monkeypatch.context() as patch:
        patch.setattr(os, "replace", fail_replace)
        store.retry_closeout_exports()
    assert store.status_summary()["closeout"]["export_error"] == "PermissionError"
    assert {p.name: p.read_bytes() for p in directory.iterdir()} == original
    store.retry_closeout_exports()
    assert store.status_summary()["closeout"]["export_error"] is None
    assert {p.name: p.read_bytes() for p in directory.iterdir()} == original


async def test_failed_export_stays_visible_after_queued_run_activation(
    app: FastAPI,
    hub_store: HubStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from agent_hub import closeout_export

    async with app.router.lifespan_context(app):
        failed_run = prepare(hub_store)
        submit(hub_store, failed_run, "bob", report())
        hub_store.start_workflow("Queued run")
        with monkeypatch.context() as patch:

            def fail(*args: Any) -> None:
                raise PermissionError("test export failure")

            patch.setattr(closeout_export, "atomic_write", fail)
            done(hub_store)
        status = hub_store.status_summary()
        assert status["run_id"] != failed_run
        assert status["workflow"]["status"] == "active"
        assert status["closeout"]["export_error"] is None
        assert status["closeout_export_errors"] == [
            {"run_id": failed_run, "error": "PermissionError"}
        ]
        with database(hub_store.path) as connection:
            before = list(connection.iterdump())
        assert (
            hub_store.status_summary()["closeout_export_errors"] == status["closeout_export_errors"]
        )
        with database(hub_store.path) as connection:
            assert list(connection.iterdump()) == before
        hub_store.retry_closeout_exports()
        assert hub_store.status_summary()["closeout_export_errors"] == []
        assert (export_dir(hub_store, failed_run) / "index.json").is_file()


@pytest.mark.parametrize("paused", [False, True])
@pytest.mark.parametrize("read_before_done", [False, True])
def test_predeadline_loss_status_read_and_early_done_export(
    store: HubStore, frozen_clock: FrozenClock, paused: bool, read_before_done: bool
) -> None:
    """Known loss ends waiting even while expiry is paused; status never writes."""
    run_id = prepare(store)
    submit(store, run_id, "bob", report())
    submit(store, run_id, "alice", report())
    if paused:
        store.set_workflow_status(WorkflowStatus.PAUSED, "Pause", actor="alice", session=None)
    frozen_clock.advance(2)
    store.heartbeat("bob", "bob-instance", None)
    store.sweep(1)
    expected = [{"author": "charlie", "reason": "worker_lost"}]
    with database(store.path) as connection:
        assert (
            connection.execute(
                "SELECT missing_reason FROM closeout_expected WHERE author = 'charlie'"
            ).fetchone()[0]
            == "worker_lost"
        )
        # A persisted collection from before #424 may not have stored the loss.
        # Both the pure projection and read/finalize must still agree.
        connection.execute(
            "UPDATE closeout_expected SET missing_reason = NULL WHERE author = 'charlie'"
        )
        before = list(connection.iterdump())
    assert store.status_summary()["closeout"]["missing"] == expected
    with database(store.path) as connection:
        assert list(connection.iterdump()) == before
    if read_before_done:
        assert store.get_closeout_reports(run_id)["missing"] == expected
    done(store)
    assert store.get_closeout_reports(run_id)["missing"] == expected
    assert store.status_summary()["closeout"]["missing"] == expected
    entries = json.loads((export_dir(store, run_id) / "index.json").read_text())["authors"]
    assert next(e for e in entries if e["author"] == "charlie")["reason"] == "worker_lost"
    store.check_in("charlie", worker_instance_id="charlie-returned")
    assert store.get_closeout_reports(run_id)["missing"] == expected
