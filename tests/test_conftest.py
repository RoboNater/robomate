"""Tests for the shared fixtures themselves."""

import os
import subprocess
import sys
from pathlib import Path

from conftest import LAUNCHER_ENV
from timing import LIVENESS_S

ROOT = Path(__file__).resolve().parents[1]

_INNER_TEST = """\
import os

import pytest

# The launcher-set variables #492 requires the fixture to strip. Kept here as a
# literal so dropping one from conftest's list fails the nested run.
REQUIRED = (
    "AGENT_NAME",
    "AGENT_RUNTIME",
    "HUB_CAPABILITIES",
    "HUB_CALL_LOG_JSONL",
    "HUB_EFFORT",
    "HUB_HARNESS",
    "HUB_HARNESS_VERSION",
    "HUB_MODEL",
    "HUB_PROVIDER",
    "HUB_TELEMETRY_LOG",
    "ROBOMATE_HUB_ID",
    "ROBOMATE_LAUNCH_HUB_CHECKOUT",
)


def test_launcher_identity_does_not_reach_the_code_under_test() -> None:
    names = os.environ["ROBO_LAUNCHER_ENV"].split(",")
    assert set(REQUIRED) <= set(names), "the outer test did not set every required variable"
    leaked = sorted(name for name in names if name in os.environ)
    assert not leaked, f"launcher identity reached the test: {leaked}"


def test_a_test_can_still_set_its_own_profile(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HUB_HARNESS", "claude-code")
    assert os.environ["HUB_HARNESS"] == "claude-code"
"""


def test_isolated_machine_state_strips_launcher_identity(tmp_path: Path) -> None:
    """#492: an agent lane's launcher environment must not reach the suite.

    The fixture strips the variables a launcher exports to its agent: the
    declared profile (``HUB_HARNESS`` and friends) and agent name, the pinned
    hub id and launch checkout, telemetry sinks and the hub selection. Without
    that, a full suite run inside a worker lane fails discovery-dependent
    tests and reports the launcher's harness where a test configures its own.

    To make the regression host-independent, the check runs in a *nested*
    pytest: its invocation directory carries no identity, the outer test sets
    every variable in the process environment, and the repo's conftest is
    copied in so the real fixture (present or removed) governs the inner run.
    The inner test reads the copied fixture's behaviour, not this one, and
    fails in any outer checkout when the strip list is removed.
    """
    inner = tmp_path / "inner"
    inner.mkdir()
    tests_dir = Path(__file__).resolve().parent
    for name in ("conftest.py", "timing.py"):
        (inner / name).write_text((tests_dir / name).read_text(encoding="utf-8"), encoding="utf-8")
    # Use the repo's real pytest config for the inner run (#127/#123 warnings
    # policy, marker registration, asyncio defaults), not a minimal ini, so it
    # is configured exactly like the main suite (the #466 lesson).
    (inner / "pyproject.toml").write_text(
        (ROOT / "pyproject.toml").read_text(encoding="utf-8"), encoding="utf-8"
    )
    (inner / "test_inner.py").write_text(_INNER_TEST, encoding="utf-8")

    # A fresh master: drop the outer run's xdist/current-test markers so the
    # nested pytest does not try to rejoin it, and give it its own basetemp.
    env = {k: v for k, v in os.environ.items() if not k.startswith("PYTEST_")}
    env.pop("PYTHONPATH", None)
    env |= {name: f"launched-{name.lower()}" for name in LAUNCHER_ENV}
    env["ROBO_LAUNCHER_ENV"] = ",".join(LAUNCHER_ENV)
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "-q",
            "-p",
            "no:cacheprovider",
            "-c",
            str(inner / "pyproject.toml"),
            "--basetemp",
            str(inner / "run"),
            str(inner / "test_inner.py"),
        ],
        cwd=inner,
        env=env,
        capture_output=True,
        text=True,
        timeout=3 * LIVENESS_S,
    )
    assert result.returncode == 0, (
        f"nested test saw a launcher environment the fixture did not strip "
        f"(rc={result.returncode}):\n{result.stdout}\n{result.stderr}"
    )
