"""rbm-bench case mining: the selected eval root and severity curation."""

from __future__ import annotations

import importlib.util
import subprocess
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "rbm_bench_cases", ROOT / "scripts/rbm-bench/cases.py"
)
assert SPEC is not None and SPEC.loader is not None
CASES = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(CASES)


def test_mirror_goes_to_the_selected_eval_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    commands: list[list[str]] = []
    monkeypatch.setattr(CASES.subprocess, "run", lambda command, **kwargs: commands.append(command))
    CASES.main(["--eval-root", str(tmp_path), "mirror"])
    assert commands == [
        [
            "git",
            "clone",
            "--mirror",
            f"https://github.com/{CASES.REPO}.git",
            str(tmp_path / "source.git"),
        ]
    ]
    assert not any(str(CASES.DEFAULT_EVAL_ROOT) in word for word in commands[0])


def test_git_reads_the_selected_eval_roots_mirror(tmp_path: Path) -> None:
    source = tmp_path / "source.git"
    subprocess.run(["git", "init", "-q", "-b", "main", str(source)], check=True)
    subprocess.run(
        ["git", "-C", str(source), "-c", "user.name=t", "-c", "user.email=t@t"]
        + ["commit", "-q", "--allow-empty", "-m", "base"],
        check=True,
    )
    head = subprocess.run(
        ["git", "-C", str(source), "rev-parse", "HEAD"], capture_output=True, text=True, check=True
    ).stdout.strip()
    assert CASES.git(tmp_path, "rev-parse", "main").strip() == head
    assert CASES.source_repo(tmp_path) == source


def found(ident: str, severity: str, resolved: bool = True) -> dict[str, Any]:
    return {"id": ident, "severity": severity, "resolved": resolved, "text": ident}


def test_a_resolved_finding_of_unclear_severity_needs_curation() -> None:
    with pytest.raises(SystemExit, match="severity unclear for resolved finding"):
        CASES.curated([found("r1-1", "unknown")], [])
    curated = CASES.curated([found("r1-1", "unknown")], ["r1-1=blocking"])
    assert curated[0]["severity"] == "blocking"


def test_an_unresolved_finding_needs_no_curation() -> None:
    assert CASES.curated([found("r2-1", "unknown", resolved=False)], []) == [
        found("r2-1", "unknown", resolved=False)
    ]


@pytest.mark.parametrize(
    ("override", "message"),
    [("r1-1=major", "expected ID=blocking"), ("r9-9=blocking", "did not raise")],
)
def test_bad_severity_overrides_are_refused(override: str, message: str) -> None:
    with pytest.raises(SystemExit, match=message):
        CASES.curated([found("r1-1", "blocking")], [override])
