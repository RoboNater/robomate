"""Credential exclusions apply to regular files, hard links, copies and symlinks."""

import importlib.util
import os
import subprocess
import sys
import tarfile
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts/archive-run.py"
SPEC = importlib.util.spec_from_file_location("archive_run", SCRIPT)
assert SPEC and SPEC.loader
ARCHIVE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(ARCHIVE)


@pytest.mark.parametrize("method", ["copy", "hardlink", "symlink"])
def test_archive_excludes_credentials_and_preserves_claude_settings(
    tmp_path: Path,
    method: str,
) -> None:
    run = tmp_path / "run"
    run.mkdir()
    source = tmp_path / "test-secret"
    source.write_text("synthetic archive credential")
    for name in (
        "configs/codex/auth.json",
        "configs/bob-agy/.gemini/antigravity-cli/antigravity-oauth-token",
        "configs/bob-agy/.gemini/antigravity-cli/jetski_state.pbtxt",
        "configs/bob-agy/.gemini/antigravity-cli/settings.json",
        "configs/bob-agy/.git-credentials",
        "hub-target/.robomate/token",
    ):
        target = run / name
        target.parent.mkdir(parents=True, exist_ok=True)
        if method == "hardlink":
            os.link(source, target)
        elif method == "symlink":
            try:
                target.symlink_to(source)
            except OSError as exc:
                pytest.skip(f"host cannot create symlinks: {exc}")
        else:
            target.write_text(source.read_text())
    safe = run / "bob/.claude/settings.json"
    safe.parent.mkdir(parents=True)
    safe.write_text("safe settings")
    output = tmp_path / "archive.tar.gz"
    subprocess.run(
        [sys.executable, str(SCRIPT), "--run-dir", str(run), "--output", str(output)],
        check=True,
    )
    with tarfile.open(output) as archive:
        assert archive.getnames() == ["bob/.claude/settings.json"]
        stream = archive.extractfile(safe.relative_to(run).as_posix())
        assert stream is not None
        with stream:
            assert stream.read() == b"safe settings"


def test_archive_refuses_output_inside_run_and_existing_output(tmp_path: Path) -> None:
    run = tmp_path / "run"
    run.mkdir()
    with pytest.raises(ValueError, match="outside"):
        ARCHIVE.archive_run(run, run / "archive.tar.gz")
    output = tmp_path / "archive.tar.gz"
    output.write_text("keep me")
    with pytest.raises(FileExistsError):
        ARCHIVE.archive_run(run, output)
    assert output.read_text() == "keep me"


def test_archive_includes_hub_database_but_not_token(tmp_path: Path) -> None:
    run = tmp_path / "run"
    state = run / "hub-target" / ".robomate"
    state.mkdir(parents=True)
    for name, content in (
        ("hub.db", "synthetic hub database"),
        ("hub.db-wal", "synthetic wal"),
        ("hub.db-shm", "synthetic shm"),
        ("hub.json", '{"hub": "synthetic"}'),
        ("token", "synthetic bearer token"),
        ("operator-token", "synthetic operator token"),
    ):
        (state / name).write_text(content)
    output = tmp_path / "archive.tar.gz"
    subprocess.run(
        [sys.executable, str(SCRIPT), "--run-dir", str(run), "--output", str(output)],
        check=True,
    )
    with tarfile.open(output) as archive:
        assert sorted(archive.getnames()) == [
            "hub-target/.robomate/hub.db",
            "hub-target/.robomate/hub.db-shm",
            "hub-target/.robomate/hub.db-wal",
            "hub-target/.robomate/hub.json",
        ]


def test_main_reports_expected_failures_without_traceback(tmp_path: Path) -> None:
    run = tmp_path / "run"
    run.mkdir()
    inside = subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--run-dir",
            str(run),
            "--output",
            str(run / "archive.tar.gz"),
        ],
        capture_output=True,
        text=True,
    )
    assert inside.returncode != 0
    assert "archive-run: error:" in inside.stderr
    assert "Traceback" not in inside.stderr
    assert "Traceback" not in inside.stdout
    assert not (run / "archive.tar.gz").exists()

    output = tmp_path / "archive.tar.gz"
    output.write_text("keep me")
    existing = subprocess.run(
        [sys.executable, str(SCRIPT), "--run-dir", str(run), "--output", str(output)],
        capture_output=True,
        text=True,
    )
    assert existing.returncode != 0
    assert "archive-run: error:" in existing.stderr
    assert "Traceback" not in existing.stderr
    assert "Traceback" not in existing.stdout
    assert output.read_text() == "keep me"
