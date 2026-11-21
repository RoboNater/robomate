"""Tests for the shared fixtures themselves."""

import os
import subprocess
import sys
from pathlib import Path

from conftest import (
    LAUNCHER_ENV,
    LAUNCHER_KEPT,
    collect_launcher_env_keys,
    launcher_env_stripped,
)
from timing import LIVENESS_S

ROOT = Path(__file__).resolve().parents[1]


def _collected() -> tuple[dict[str, set[str]], set[str]]:
    produced = collect_launcher_env_keys()
    names: set[str] = set()
    for builder_names in produced.values():
        names |= builder_names
    return produced, names


def test_launcher_env_builders_are_classified() -> None:
    """#531: every name a launcher env-builder produces is stripped or kept.

    ``LAUNCHER_ENV`` alone cannot catch a launcher starting to export a new
    variable (#492). Call each builder with dummy arguments and require every
    produced name to land in exactly one of the fixture's strip list (or its
    ``ROBOMATE_TOKEN*`` prefix) or the explicit keep-list.
    """

    produced, names = _collected()
    failures: list[str] = []
    for name in sorted(names):
        builders = sorted(
            builder for builder, builder_names in produced.items() if name in builder_names
        )
        stripped = name in LAUNCHER_ENV or name.startswith("ROBOMATE_TOKEN")
        kept = name in LAUNCHER_KEPT
        if stripped and not kept:
            continue
        if kept and not stripped:
            continue
        where = "both LAUNCHER_ENV and LAUNCHER_KEPT" if stripped else "neither"
        failures.append(
            f"{name} (from {', '.join(builders)}) is in {where}: add it to "
            f"LAUNCHER_ENV in tests/conftest.py or to LAUNCHER_KEPT with a reason"
        )
    assert not failures, "\n".join(failures)


def _inner_test(required: tuple[str, ...]) -> str:
    return f"""\
import os

import pytest

# Launcher-set variables the fixture must strip (#492, #531). Built from the
# collected stripped set so this list cannot disagree with the builders.
REQUIRED = {required!r}


def test_launcher_identity_does_not_reach_the_code_under_test() -> None:
    names = os.environ["ROBO_LAUNCHER_ENV"].split(",")
    assert set(REQUIRED) <= set(names), "the outer test did not set every required variable"
    # Check the launcher's sentinel value, not just the name: the fixture may
    # replace a value with its own isolated directory (XDG_CONFIG_HOME, #592),
    # which is not a leak.
    leaked = sorted(
        name for name in names if os.environ.get(name) == f"launched-{{name.lower()}}"
    )
    assert not leaked, f"launcher identity reached the test: {{leaked}}"


def test_a_test_can_still_set_its_own_profile(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HUB_HARNESS", "claude-code")
    assert os.environ["HUB_HARNESS"] == "claude-code"
"""


def test_isolated_machine_state_strips_launcher_identity(tmp_path: Path) -> None:
    """#492: an agent lane's launcher environment must not reach the suite.

    The fixture strips the variables a launcher exports to its agent: the
    declared profile (``HUB_HARNESS`` and friends) and agent name, the explicit
    workspace, the pinned hub id and launch checkout, telemetry sinks and the
    hub selection. Without that, a full suite run inside a worker lane fails
    discovery-dependent tests and reports the launcher's harness where a test
    configures its own.

    To make the regression host-independent, the check runs in a *nested*
    pytest: its invocation directory carries no identity, the outer test sets
    every variable in the process environment, and the repo's conftest is
    copied in so the real fixture (present or removed) governs the inner run.
    The inner test reads the copied fixture's behaviour, not this one, and
    fails in any outer checkout when the strip list is removed.

    ``REQUIRED`` is the collected stripped set (#531), not a separate literal,
    so dropping a name from ``LAUNCHER_ENV`` fails both this test and the
    classification test rather than leaving the two lists to disagree.

    The inner check compares the launcher's sentinel value rather than just the
    name, so a variable the fixture deliberately replaces with its own isolated
    directory (``XDG_CONFIG_HOME``, #592) does not count as a leak.
    """
    _, collected = _collected()
    required = tuple(sorted(launcher_env_stripped(collected)))
    assert required, "no launcher env-builder names were collected"

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
    (inner / "test_inner.py").write_text(_inner_test(required), encoding="utf-8")

    # A fresh master: drop the outer run's xdist/current-test markers so the
    # nested pytest does not try to rejoin it, and give it its own basetemp.
    env = {k: v for k, v in os.environ.items() if not k.startswith("PYTEST_")}
    env.pop("PYTHONPATH", None)
    # Set every strip-list name plus the collected set: the former proves the
    # fixture honours LAUNCHER_ENV, the latter the collected stripped names.
    to_set = sorted(set(LAUNCHER_ENV) | set(required))
    env |= {name: f"launched-{name.lower()}" for name in to_set}
    env["ROBO_LAUNCHER_ENV"] = ",".join(to_set)
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
