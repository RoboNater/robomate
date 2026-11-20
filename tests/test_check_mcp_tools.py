import importlib.util
import json
import os
import subprocess
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
def user_home(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    home = tmp_path / "home"
    for name in ("HOME", "USERPROFILE"):
        monkeypatch.setenv(name, str(home))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(home / ".config"))
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    return home


@pytest.fixture
def setup(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, user_home: Path) -> Path:
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    (checkout / "pyproject.toml").touch()
    (checkout / "uv.lock").touch()
    write_skill(checkout / "skills/worker", b"# Worker\nJoin the hub.\n")
    write_skill(checkout / "skills/alice-orchestrator", b"# Alice\n")
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex"))
    for name in ("XDG_STATE_HOME", "LOCALAPPDATA"):
        monkeypatch.setenv(name, str(user_home / "state"))
    monkeypatch.setattr(check.shutil, "which", lambda _: None)
    monkeypatch.setattr(check, "json_config_paths", lambda: [], raising=False)
    monkeypatch.setattr(check, "probe", lambda *_: ({"name": "bridge", "version": "1"}, ["a", "b"]))
    return checkout


def write_skill(path: Path, content: bytes) -> Path:
    path.mkdir(parents=True)
    skill = path / "SKILL.md"
    skill.write_bytes(content)
    return skill


def link_skill(path: Path, target: Path) -> None:
    path.parent.mkdir(parents=True)
    if os.name == "nt":
        result = subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(path), str(target)], capture_output=True, text=True
        )
        if result.returncode:
            pytest.skip(f"could not create a directory junction: {result.stderr or result.stdout}")
    else:
        try:
            path.symlink_to(target, target_is_directory=True)
        except OSError as exc:
            pytest.skip(f"could not create a directory symlink: {exc}")


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
        f'[mcp_servers.robomate]\ntool_timeout_sec = 1900\nargs = ["--project", "{project}"]\n'
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
        "[mcp_servers.robomate]\ntool_timeout_sec = 1900\n"
        f'args = ["--project", "{link.as_posix()}"]\n'
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


@pytest.mark.parametrize(
    ("harness", "mode", "timeout", "bad"),
    [
        ("codex", "unknown", 330, True),
        ("opencode", "unknown", 330, True),
        ("antigravity", "unknown", 1900, False),
        ("claude-code", "print", 1515, False),
        ("claude-code", "interactive", 330, False),
    ],
)
def test_static_timeout_profile(harness: str, mode: str, timeout: int, bad: bool) -> None:
    assert bool(check.check_timeout(harness, timeout, mode)) is bad


@pytest.mark.parametrize(
    ("harness", "field", "scale"),
    [
        ("opencode", "timeout", 1000),
        ("antigravity", "timeoutSeconds", 1),
    ],
)
def test_other_static_clients_and_advertisement_are_audited(
    setup: Path,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    harness: str,
    field: str,
    scale: int,
) -> None:
    path = tmp_path / "client.json"
    env_key = "environment" if harness == "opencode" else "env"
    section = "mcp" if harness == "opencode" else "mcpServers"
    path.write_text(
        json.dumps(
            {
                section: {
                    "robomate": {
                        field: 330 * scale,
                        env_key: {"HUB_MCP_TIMEOUT_S": "1900"},
                    }
                }
            }
        )
    )
    monkeypatch.setattr(check, "json_config_paths", lambda: [(harness, path)], raising=False)
    assert run(monkeypatch, setup, "--save") == 1
    out = capsys.readouterr().out
    assert harness in out and "profile + HTTP margin" in out
    assert "HUB_MCP_TIMEOUT_S" in out and "330" in out


def test_codex_advertised_timeout_cannot_exceed_real_timeout(
    setup: Path,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    config = tmp_path / "codex/config.toml"
    config.parent.mkdir()
    config.write_text(
        "[mcp_servers.robomate]\ntool_timeout_sec = 330\n"
        f'args = ["--project", "{setup.as_posix()}"]\n'
        'env = {HUB_MCP_TIMEOUT_S = "1900"}\n'
        '[mcp_servers.robomate.tools.a]\napproval_mode = "approve"\n'
        '[mcp_servers.robomate.tools.b]\napproval_mode = "approve"\n'
    )
    assert run(monkeypatch, setup, "--save") == 1
    assert "HUB_MCP_TIMEOUT_S" in capsys.readouterr().out


def test_valid_jsonc_keeps_quoted_comment_markers_and_converts_milliseconds(
    setup: Path,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    path = tmp_path / "opencode.jsonc"
    text = """{
      // Client timeout is in milliseconds.
      "mcp": {"robomate": {
        "timeout": 1900000, /* long hold budget */
        "environment": {
          "HUB_MCP_TIMEOUT_S": "1900",
          "URL": "https://example.test/a//b/*literal*/",
        },
      },},
    }"""
    path.write_text(text)
    monkeypatch.setattr(check, "json_config_paths", lambda: [("opencode", path)])
    parsed = check.jsonc_object(text)
    assert parsed["mcp"]["robomate"]["environment"]["URL"] == "https://example.test/a//b/*literal*/"
    assert run(monkeypatch, setup, "--save") == 0


@pytest.mark.parametrize(
    ("entry", "bad"),
    [
        ({"timeoutSeconds": 1900, "env": {"HUB_MCP_TIMEOUT_S": "1900"}}, False),
        ({"timeoutSeconds": 0, "env": {"HUB_MCP_TIMEOUT_S": "1900"}}, True),
        ({"timeoutSeconds": -1, "env": {"HUB_MCP_TIMEOUT_S": "1900"}}, False),
        ({"timeoutSeconds": 1900, "env": {"HUB_MCP_TIMEOUT_S": "invalid"}}, True),
        ({"timeoutSeconds": 1900, "env": []}, True),
    ],
)
def test_antigravity_timeout_defaults_and_unlimited_budget(
    entry: dict[str, Any], bad: bool
) -> None:
    assert bool(check.check_client_timeout("antigravity", entry)) is bad


def test_invalid_json_config_is_reported_instead_of_crashing(
    setup: Path,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    path = tmp_path / "client.json"
    path.write_text('{"mcp": {"robomate": "invalid"}}')
    monkeypatch.setattr(check, "json_config_paths", lambda: [("opencode", path)])
    assert run(monkeypatch, setup, "--save") == 1
    assert "robomate entry must be an object" in capsys.readouterr().out


def test_static_json_config_locations(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(check.Path, "home", classmethod(lambda cls: tmp_path))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    selected = tmp_path / "selected.jsonc"
    monkeypatch.setenv("OPENCODE_CONFIG", str(selected))
    assert check.json_config_paths() == [
        ("opencode", tmp_path / "xdg/opencode/opencode.json"),
        ("opencode", tmp_path / "xdg/opencode/opencode.jsonc"),
        ("opencode", selected),
        ("antigravity", tmp_path / ".gemini/config/mcp_config.json"),
    ]
    monkeypatch.setenv("XDG_CONFIG_HOME", "relative")
    assert check.json_config_paths()[0] == ("opencode", tmp_path / ".config/opencode/opencode.json")


@pytest.mark.parametrize("target_kind", ["checkout", "other-checkout", "wrong-skill", "broken"])
def test_skill_directory_links(
    setup: Path,
    user_home: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    target_kind: str,
) -> None:
    assert run(monkeypatch, setup, "--save") == 0
    capsys.readouterr()
    original = setup / "skills/worker"
    target = original
    if target_kind == "wrong-skill":
        target = setup / "skills/alice-orchestrator"
    elif target_kind != "checkout":
        # A live link to the wrong checkout fails even with identical bytes.
        target = setup.with_name("checkout-other") / "skills/worker"
        write_skill(target, (original / "SKILL.md").read_bytes())
    installed = user_home / ".claude/skills/worker"
    link_skill(installed, target)
    if target_kind == "broken":
        (target / "SKILL.md").unlink()
        target.rmdir()

    assert run(monkeypatch, setup) == (0 if target_kind == "checkout" else 1)
    findings = [line for line in capsys.readouterr().out.splitlines() if str(installed) in line]
    assert len(findings) == 1
    assert "Claude Code" in findings[0]
    assert "OpenCode" in findings[0]
    assert {
        "checkout": "OK",
        "other-checkout": f"expected {original}",
        "wrong-skill": f"expected {original}",
        "broken": "broken link",
    }[target_kind] in findings[0]


@pytest.mark.parametrize(
    ("content", "status"),
    [(b"# Worker\nJoin the hub.\n", 0), (b"# Worker\r\nJoin the hub.\r\n", 0), (b"# Old\n", 1)],
)
def test_skill_copies(
    setup: Path,
    user_home: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    content: bytes,
    status: int,
) -> None:
    assert run(monkeypatch, setup, "--save") == 0
    capsys.readouterr()
    installed = write_skill(user_home / ".agents/skills/worker", content)

    assert run(monkeypatch, setup) == status
    findings = [line for line in capsys.readouterr().out.splitlines() if str(installed) in line]
    assert len(findings) == 1
    assert "Codex" in findings[0]
    assert "OpenCode" in findings[0]
    assert ("OK" if status == 0 else "differs from") in findings[0]
    assert installed.read_bytes() == content


def test_archived_skill_fails_even_without_a_baseline(
    setup: Path,
    user_home: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    installed = write_skill(user_home / ".config/opencode/skills/alice-relay", b"# Archived\n")
    assert run(monkeypatch, setup) == 1
    out = capsys.readouterr().out
    assert f"{installed.parent}" in out
    assert "not installable" in out


def test_missing_installable_skill_is_reported_without_failing(
    setup: Path,
    user_home: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert run(monkeypatch, setup, "--save") == 0
    capsys.readouterr()
    assert run(monkeypatch, setup) == 0
    out = capsys.readouterr().out
    assert f"INFO: Antigravity skill {user_home / '.gemini/config/skills/worker'}" in out
    assert "absent" in out
