"""The operator-channel driver's isolation, audit check and evidence (#133).

The sandbox run itself needs GitHub; these tests cover what it must never get
wrong locally: its hub stays inside its temporary directory, the operator's
real registry and credential are left byte-identical, `robomate ls` in the
normal environment does not list it, and it is stopped on every exit path.
"""

import importlib.util
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
from agent_hub_common.registry import operator_token_path, registry_path

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "operator_channel_driver", ROOT / "scripts/operator-channel-driver.py"
)
assert SPEC is not None and SPEC.loader is not None
driver = importlib.util.module_from_spec(SPEC)
# Its dataclasses look their module up while the class is being built.
sys.modules[SPEC.name] = driver
SPEC.loader.exec_module(driver)


def init_checkout(root: Path) -> None:
    root.mkdir(parents=True)
    for args in (
        ["init", "-b", "main"],
        ["remote", "add", "origin", "https://github.com/example/sandbox.git"],
        ["symbolic-ref", "refs/remotes/origin/HEAD", "refs/remotes/origin/main"],
    ):
        subprocess.run(["git", *args], cwd=root, check=True, capture_output=True)


@pytest.fixture
def normal(tmp_path: Path) -> dict[str, str]:
    """A decoy of the operator's normal environment, with a run hub selected.

    It holds a registry and an operator token where the real ones would be, so
    the test can prove the driver leaves them exactly as they were.
    """

    decoy = tmp_path / "decoy"
    env = {
        key: value for key, value in os.environ.items() if not key.startswith(("ROBOMATE_", "HUB_"))
    }
    env |= {
        "XDG_STATE_HOME": str(decoy / "state"),
        "LOCALAPPDATA": str(decoy / "state"),
        # What a worker's MCP environment carries for the run's hub.
        "ROBOMATE_HUB_URL": "http://127.0.0.1:9",
        "ROBOMATE_TOKEN": "run-hub-token",
        "ROBOMATE_TOKEN_FILE": str(decoy / "run-hub-token"),
        "HUB_URL": "http://127.0.0.1:9",
        "HUB_TOKEN": "run-hub-token",
    }
    registry = registry_path(env)
    registry.parent.mkdir(parents=True)
    registry.write_text("[]\n", encoding="utf-8")
    token = operator_token_path(env)
    token.write_text("decoy-operator-token\n", encoding="utf-8")
    token.chmod(0o600)
    return env


def snapshot(env: dict[str, str]) -> dict[Path, tuple[bytes, int]]:
    paths = (registry_path(env), operator_token_path(env))
    return {path: (path.read_bytes(), path.stat().st_mtime_ns) for path in paths}


def test_isolated_env_drops_selectors_and_points_state_into_temp(tmp_path: Path) -> None:
    base = {
        "PATH": "/bin",
        "ROBOMATE_HUB_URL": "http://127.0.0.1:9",
        "ROBOMATE_TOKEN": "t",
        "ROBOMATE_TOKEN_FILE": "/t",
        "ROBOMATE_OPERATOR_TOKEN_FILE": "/real/operator-token",
        "HUB_URL": "http://127.0.0.1:9",
        "HUB_TOKEN": "t",
        "AGENT_NAME": "bob",
        "XDG_STATE_HOME": "/real/state",
    }

    env = driver.isolated_env(base, tmp_path)

    assert env["PATH"] == "/bin"
    for key in ("ROBOMATE_HUB_URL", "ROBOMATE_TOKEN", "ROBOMATE_TOKEN_FILE", "HUB_URL"):
        assert key not in env
    assert "HUB_TOKEN" not in env and "AGENT_NAME" not in env
    for key in ("XDG_STATE_HOME", "LOCALAPPDATA", "ROBOMATE_OPERATOR_TOKEN_FILE", "HUB_STATE_DIR"):
        assert Path(env[key]).is_relative_to(tmp_path)
    assert registry_path(env).is_relative_to(tmp_path)
    assert operator_token_path(env).is_relative_to(tmp_path)


def test_driver_hub_leaves_the_machine_state_untouched(
    tmp_path: Path, normal: dict[str, str], capsys: pytest.CaptureFixture[str]
) -> None:
    before = snapshot(normal)
    machine = driver.MachineState.capture(normal)
    temp = tmp_path / "driver"
    temp.mkdir()
    clone = temp / "sandbox"
    init_checkout(clone)
    port = driver.free_port()
    hub = driver.IsolatedHub(clone, temp, driver.isolated_env(normal, temp), port)

    with hub:
        endpoint = hub.start()
        assert endpoint.url == f"http://127.0.0.1:{port}"
        assert hub.facts.registered
        assert Path(hub.facts.registry).is_relative_to(temp)
        assert Path(hub.facts.state_dir).is_relative_to(temp)
        # robomate commands reach this hub by explicit URL, not the run hub.
        inbox = hub.run("inbox", "--json")
        assert inbox.returncode == 0, inbox.stderr
        assert inbox.stdout.strip() == "[]"
        # `robomate ls` in the normal environment, during the run.
        assert driver.operator_ls(normal, hub.facts, tmp_path) == {
            "hubs_listed": 0,
            "driver_hub_listed": False,
        }
        # The operator sees the result on the terminal, not only in the evidence.
        assert "Operator `robomate ls`: 0 hubs listed; driver's hub listed: False" in (
            capsys.readouterr().out
        )

    assert hub.facts.stopped
    assert hub.facts.registry_stopped
    assert "`robomate down` exited 0" in hub.facts.stop_detail
    assert "no longer answers" in hub.facts.stop_detail
    assert snapshot(normal) == before
    assert driver.MachineState.capture(normal) == machine
    assert not (tmp_path / "decoy" / "run-hub-token").exists()
    assert {path.name for path in tmp_path.iterdir()} == {"decoy", "driver"}


def test_driver_hub_is_stopped_when_a_step_fails(tmp_path: Path, normal: dict[str, str]) -> None:
    clone = tmp_path / "sandbox"
    init_checkout(clone)
    hub = driver.IsolatedHub(
        clone, tmp_path, driver.isolated_env(normal, tmp_path), driver.free_port()
    )

    with pytest.raises(driver.DriverError, match="step failed"), hub:
        hub.start()
        raise driver.DriverError("step failed")

    assert hub.facts.stopped
    assert hub.facts.registry_stopped
    assert hub.process is None


@pytest.mark.skipif(
    sys.platform == "win32" or getattr(os, "geteuid")() == 0,  # noqa: B009 (mypy on Windows)
    reason="needs POSIX permissions that bind root",
)
def test_machine_state_is_compared_without_reading_the_files(tmp_path: Path) -> None:
    token = tmp_path / "operator-token"
    token.write_text("secret\n", encoding="utf-8")
    token.chmod(0)

    state = driver.stat_only(token)

    assert state.exists and state.size == 7
    assert driver.stat_only(tmp_path / "absent") == driver.FileStat(str(tmp_path / "absent"), False)


def audit_rows() -> list[dict[str, Any]]:
    alice, session = driver.ORCHESTRATOR, "0b6f8a8e-7f61-4d1f-9a43-7d5f5bb0f9a1"
    rows = [
        ("initialize_workflow", "ok", alice, session),
        ("ask_user", "ok", alice, session),
        ("set_workflow_status", "error -32002", alice, session),
        ("assign_task", "error -32002", alice, session),
        ("check_merge_gate", "error -32002", alice, session),
        ("hub.answer", "error -32005", None, None),
        ("hub.answer", "ok", "operator", None),
        ("log_decision", "ok", alice, session),
        ("set_workflow_status", "ok", alice, session),
        ("check_merge_gate", "ok", alice, session),
    ]
    return [
        {"id": i, "ts": "t", "method": m, "outcome": o, "actor": a, "session": s}
        for i, (m, o, a, s) in enumerate(rows, start=1)
    ]


DECISIONS = [
    {"id": 1, "ts": "t", "summary": "Asked", "rationale": "r", "key": None, "actor": "a"},
]
QUESTIONS = [
    {
        "id": 1,
        "asked": "t",
        "actor": driver.ORCHESTRATOR,
        "session": "s",
        "answer": "yes",
        "answered": "t",
        "answered_by": "operator",
        "resumed_by": 3,
    }
]


def test_audit_check_accepts_attributed_rows() -> None:
    assert driver.audit_problems(audit_rows(), DECISIONS, QUESTIONS) == []


def test_audit_check_names_each_unattributed_change() -> None:
    rows = audit_rows()
    rows[7]["actor"] = None
    rows[6]["session"] = "0b6f8a8e-7f61-4d1f-9a43-7d5f5bb0f9a1"
    decisions = [dict(DECISIONS[0], actor=None)]
    questions = [dict(QUESTIONS[0], answered_by=driver.ORCHESTRATOR, resumed_by=None)]

    problems = driver.audit_problems(rows[:-1], decisions, questions)

    assert problems == [
        "rpc_audit row 8 (log_decision) applied without an actor",
        "decision row 1 names no actor",
        f"no rpc_audit row check_merge_gate ok by {driver.ORCHESTRATOR}",
        "rpc_audit row 7 gives the operator a session",
        "operator question 1 misattributed",
        "operator question 1 was never resumed",
    ]


def test_evidence_credits_the_script_in_its_first_line_and_every_row(tmp_path: Path) -> None:
    record = driver.Record(
        run_id="20261008000000_abcdef12",
        repository=driver.SANDBOX,
        started="2026-10-08T00:00:00+00:00",
        robomate_commit="0" * 40,
        hub=driver.HubFacts(clone=str(tmp_path), temp_dir=str(tmp_path), port=8999),
        audit=audit_rows(),
        decisions=[dict(row, session=None) for row in DECISIONS],
        questions=QUESTIONS,
        facts={"pr_url": "https://github.com/RoboNater/robo-agents-sandbox/pull/1"},
    )
    record.machine_before = record.machine_after = driver.MachineState(
        driver.FileStat("hubs.json", False), driver.FileStat("operator-token", False)
    )
    for number in range(1, 11):
        record.step(number, f"step {number}", driver.script("orchestrator"), "a", hub="h")

    text = driver.render_evidence(record)
    lines = text.splitlines()

    assert lines[0].startswith(
        f"**Scripted run: every action below was performed by `{driver.SCRIPT}`, not by "
        "agents and not by the operator.**"
    )
    rows = [line for line in lines if line.startswith("| ") and not line.startswith("| #")]
    data = [row for row in rows if not row.startswith(("| id ", "| Fact ", "| File "))]
    assert data
    for row in data:
        cells = [cell.strip() for cell in row.strip("|").split("|")]
        assert any(cell.startswith("script") for cell in cells), row
    assert f"- Platform: `{record.platform}`" in text
    assert "--operator-ls" in text
    assert "Result: **FAILED**" in text  # the hub never started, so it never stopped
