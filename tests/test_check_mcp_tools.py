import importlib.util
import json
import sys
from pathlib import Path
from typing import Any

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts/check-mcp-tools.py"
spec = importlib.util.spec_from_file_location("check_mcp_tools", SCRIPT)
assert spec and spec.loader
check = importlib.util.module_from_spec(spec)
spec.loader.exec_module(check)


@pytest.fixture
def setup(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    (checkout / "pyproject.toml").touch()
    (checkout / "uv.lock").touch()
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setattr(check.shutil, "which", lambda _: None)
    monkeypatch.setattr(check, "probe", lambda *_: ({"name": "bridge", "version": "1"}, ["a", "b"]))
    return checkout


def run(monkeypatch: pytest.MonkeyPatch, checkout: Path, *args: str) -> int:
    monkeypatch.setattr(sys, "argv", [str(SCRIPT), str(checkout), *args])
    return int(check.main())


def test_baseline_and_changes(
    setup: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    assert run(monkeypatch, setup) == 2
    assert run(monkeypatch, setup, "--save") == 0
    assert run(monkeypatch, setup) == 0
    monkeypatch.setattr(check, "probe", lambda *_: ({}, ["b", "c"]))
    assert run(monkeypatch, setup) == 1
    out = capsys.readouterr().out
    assert "added since baseline: c" in out
    assert "removed since baseline: a" in out


@pytest.mark.parametrize(
    ("problem", "expected"),
    [
        ("missing", 'Codex: no approval_mode = "approve" for: b'),
        ("stale", "Codex: approvals for tools the bridge no longer serves: stale"),
        ("project", "Codex: entry runs --project /wrong, not "),
    ],
)
def test_codex_problems(
    setup: Path,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    problem: str,
    expected: str,
) -> None:
    config = tmp_path / "codex/config.toml"
    config.parent.mkdir()
    project = "/wrong" if problem == "project" else setup.as_posix()
    approvals = ["a"] if problem == "missing" else ["a", "b"]
    if problem == "stale":
        approvals.append("stale")
    config.write_text(
        f'[mcp_servers.robomate]\nargs = ["--project", "{project}"]\n'
        + "".join(
            f'[mcp_servers.robomate.tools.{name}]\napproval_mode = "approve"\n'
            for name in approvals
        )
    )
    assert run(monkeypatch, setup, "--save") == 1
    assert expected in capsys.readouterr().out


def test_missing_codex_entry(
    setup: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    config = tmp_path / "codex/config.toml"
    config.parent.mkdir()
    config.write_text("")
    assert run(monkeypatch, setup, "--save") == 0
    monkeypatch.setattr(check.shutil, "which", lambda _: "/bin/codex")
    assert run(monkeypatch, setup) == 1
    assert "no [mcp_servers.robomate]" in capsys.readouterr().out


def test_symlinked_project(setup: Path, tmp_path: Path) -> None:
    link = tmp_path / "link"
    try:
        link.symlink_to(setup, target_is_directory=True)
    except OSError:
        pytest.skip("symlinks unavailable")
    config = tmp_path / "codex/config.toml"
    config.parent.mkdir()
    config.write_text(
        f'[mcp_servers.robomate]\nargs = ["--project", "{link.as_posix()}"]\n'
        '[mcp_servers.robomate.tools.a]\napproval_mode = "approve"\n'
    )
    assert check.check_codex(setup, {"a"}) == ([], True)


def test_separate_checkouts_and_list(
    setup: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    other = tmp_path / "other"
    other.mkdir()
    (other / "pyproject.toml").touch()
    (other / "uv.lock").touch()
    assert run(monkeypatch, setup, "--save") == 0
    assert run(monkeypatch, other, "--save") == 0
    store = json.loads(check.snapshot_path().read_text())
    assert set(store["checkouts"]) == {str(setup), str(other)}
    capsys.readouterr()
    assert run(monkeypatch, setup, "--list") == 0
    result = json.loads(capsys.readouterr().out)
    assert result["server"]["name"] == "bridge"
    assert result["tools"] == ["a", "b"]


def test_real_bridge(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    monkeypatch.setenv("ROBOMATE_HUB_URL", "http://127.0.0.1:1")
    monkeypatch.setenv("ROBOMATE_TOKEN", "invalid")
    monkeypatch.setenv("ROBOMATE_TOKEN_FILE", "invalid")
    monkeypatch.setenv("ROBOMATE_TOKEN_EXTRA", "invalid")
    monkeypatch.setenv("PROBE_SENTINEL", "present")
    popen = check.subprocess.Popen
    observed: dict[str, Any] = {}

    def capture(*args: Any, **kwargs: Any) -> Any:
        observed.update(kwargs)
        observed["process"] = popen(*args, **kwargs)
        return observed["process"]

    monkeypatch.setattr(check.subprocess, "Popen", capture)
    monkeypatch.setattr(sys, "argv", [str(SCRIPT), "--list"])
    assert check.main() == 0
    result = json.loads(capsys.readouterr().out)
    env = observed["env"]
    assert env["PROBE_SENTINEL"] == "present"
    assert "ROBOMATE_HUB_URL" not in env
    assert not any(key.startswith("ROBOMATE_TOKEN") for key in env)
    assert observed["stderr"] != check.subprocess.PIPE
    assert observed["process"].returncode == 0
    assert {"join", "whoami", "await_assignment", "assign_task"} <= set(result["tools"])
