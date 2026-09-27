"""A prepared run attaches all agents to an existing HTTP hub."""

from __future__ import annotations

import importlib.util
import json
import os
import shlex
import subprocess
import sys
import tomllib
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("prepare_run", ROOT / "scripts/prepare-run.py")
assert SPEC and SPEC.loader
PREPARE_RUN = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(PREPARE_RUN)


def origin(tmp_path: Path) -> Path:
    path = tmp_path / "origin"
    subprocess.run(["git", "init", "-b", "main", str(path)], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(path), "-c", "user.name=Test",
                    "-c", "user.email=test@example.com", "commit", "--allow-empty",
                    "-m", "baseline"], check=True, capture_output=True)
    return path


def hub_repo(tmp_path: Path, url: str = "http://127.0.0.1:8521") -> Path:
    repo = tmp_path / "target"
    state = repo / ".robomate"
    state.mkdir(parents=True)
    token = state / "token"
    token.write_text("test-token\n")
    token.chmod(0o600)
    (state / "hub.json").write_text(json.dumps({"pid": os.getpid(), "url": url,
                                                  "port": 8521, "hub_id": "abc"}))
    return repo


def prepare(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, **kwargs: Any
) -> tuple[Path, dict[str, Any]]:
    source = origin(tmp_path)
    target = hub_repo(tmp_path)
    monkeypatch.setattr(PREPARE_RUN, "probe_versions",
                        lambda _: ({"claude": "2.1", "codex": "0.1"},
                                   {"claude-code": "2.1", "codex": "0.1"}))
    monkeypatch.setattr(PREPARE_RUN, "hub_healthy", lambda *_: True)
    monkeypatch.setattr(PREPARE_RUN, "codex_login_status", lambda _: "logged in")
    run_dir = tmp_path / "run"
    manifest = PREPARE_RUN.prepare(str(source), run_dir, issue=42, account="tester",
                                   hub_repo=target, **kwargs)
    return run_dir, manifest


def test_configs_use_bridge_and_existing_hub(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_dir, manifest = prepare(tmp_path, monkeypatch)
    assert manifest["state_dir"] == str(tmp_path / "target/.robomate")
    assert not (run_dir / "hub-state").exists()
    alice = json.loads((run_dir / "configs/alice.mcp.json").read_text())["mcpServers"]
    bob = json.loads((run_dir / "configs/bob.mcp.json").read_text())["mcpServers"]
    assert set(alice) == set(bob) == {"robomate"}
    assert alice["robomate"]["args"][-3:] == ["mcp", "--role", "orchestrator"]
    assert bob["robomate"]["args"][-3:] == ["mcp", "--role", "worker"]
    for config in (alice["robomate"], bob["robomate"]):
        assert config["env"]["ROBOMATE_HUB_URL"] == "http://127.0.0.1:8521"
        assert config["env"]["ROBOMATE_TOKEN_FILE"] == str(tmp_path / "target/.robomate/token")
        assert "HUB_TOKEN" not in config["env"]
    codex = tomllib.loads((run_dir / "configs/codex/config.toml").read_text())
    assert set(codex["mcp_servers"]) == {"robomate"}
    assert codex["mcp_servers"]["robomate"]["args"][-3:] == ["mcp", "--role", "worker"]
    assert manifest["configs"]["bob"].endswith("bob.mcp.json")


def test_codex_alice_uses_orchestrator_bridge(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_dir, _ = prepare(tmp_path, monkeypatch, alice_harness="codex")
    config = tomllib.loads((run_dir / "configs/alice-codex/config.toml").read_text())
    server = config["mcp_servers"]["robomate"]
    assert server["args"][-3:] == ["mcp", "--role", "orchestrator"]
    assert set(server["enabled_tools"]) == set(PREPARE_RUN.ALICE_TOOLS)


def test_missing_or_different_hub_is_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = origin(tmp_path)
    with pytest.raises(ValueError, match="--hub-repo is required"):
        PREPARE_RUN.prepare(str(source), tmp_path / "run")
    target = hub_repo(tmp_path)
    monkeypatch.setattr(PREPARE_RUN, "hub_healthy", lambda *_: True)
    with pytest.raises(ValueError, match="differs from the running hub"):
        PREPARE_RUN.prepare(str(source), tmp_path / "run", hub_repo=target,
                            hub_url="http://127.0.0.1:9999")
    monkeypatch.setattr(PREPARE_RUN, "hub_healthy", lambda *_: False)
    with pytest.raises(ValueError, match="not responding"):
        PREPARE_RUN.prepare(str(source), tmp_path / "run", hub_repo=target)


def test_rerun_preserves_clones_and_external_token(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_dir, first = prepare(tmp_path, monkeypatch)
    token_path = tmp_path / "target/.robomate/token"
    original_token = token_path.read_text()
    second = PREPARE_RUN.prepare(
        str(tmp_path / "origin"), run_dir, issue=42, account="tester",
        hub_repo=tmp_path / "target",
    )
    assert first["workspaces"] == second["workspaces"]
    assert token_path.read_text() == original_token
    assert not (run_dir / "hub-state").exists()
    for name in ("bob", "charlie"):
        assert not (run_dir / name / "configs").exists()
        assert not (run_dir / name / ".mcp.json").exists()


def test_worker_only_uses_token_file_not_bearer_value(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = origin(tmp_path)
    target = hub_repo(tmp_path, "http://192.0.2.1:8521")
    monkeypatch.setattr(PREPARE_RUN, "probe_versions",
                        lambda _: ({"claude": "2.1"}, {"claude-code": "2.1"}))
    run_dir = tmp_path / "remote"
    PREPARE_RUN.prepare_worker("bob", str(source), run_dir,
                               "http://192.0.2.1:8521", target / ".robomate/token",
                               "claude-code")
    config = json.loads((run_dir / "configs/bob.mcp.json").read_text())
    env = config["mcpServers"]["robomate"]["env"]
    assert env["ROBOMATE_TOKEN_FILE"] == str(target / ".robomate/token")
    assert "HUB_TOKEN" not in env


def test_script_help_loads() -> None:
    result = subprocess.run([sys.executable, str(ROOT / "scripts/prepare-run.py"), "--help"],
                            capture_output=True, text=True, check=True)
    assert "--hub-repo" in result.stdout


# Harness, path, and goal behavior independent of the hub transport.
WINDOWS_RUN = PureWindowsPath("C:/Users/Bob/runs/step7")


@pytest.mark.parametrize("auto_start", [True, False])
def test_claude_launch_lines_carry_model_effort_and_prompt(
    tmp_path: Path, auto_start: bool
) -> None:
    run_dir = (tmp_path / "run").resolve()
    config = run_dir / "configs" / "bob.mcp.json"
    prompt = run_dir / "bob.prompt.md"
    lines = PREPARE_RUN.launch_lines(
        "claude-code", run_dir / "bob", config, prompt, run_dir / "bob" / ".git",
        "opus[1m]", "high", auto_start,
    )
    assert lines[0] == f"cd {run_dir / 'bob'}"
    command = lines[1]
    assert command.startswith("claude --model 'opus[1m]' --effort high ")
    assert f"--mcp-config {config} --add-dir {run_dir}" in command
    if auto_start:
        assert "--permission-mode auto" in command
        assert command.endswith(f"-p 'Read {prompt} and follow the instructions in it'")
    else:
        assert "--permission-mode" not in command and " -p " not in command
    assert len(lines) == 2


@pytest.mark.parametrize("auto_start", [True, False])
def test_codex_launch_lines_keep_the_session_and_carry_model_effort(
    tmp_path: Path, auto_start: bool
) -> None:
    run_dir = (tmp_path / "run").resolve()
    home = run_dir / "configs" / "codex"
    prompt = run_dir / "charlie.prompt.md"
    git_dir = run_dir / "charlie" / ".git"
    lines = PREPARE_RUN.launch_lines(
        "codex", run_dir / "charlie", home, prompt, git_dir, "gpt-6-sol", "high", auto_start
    )
    flags = (
        f"-C . --add-dir {git_dir} --approve-for-me --model gpt-6-sol "
        "-c 'model_reasoning_effort=\"high\"'"
    )
    if auto_start:
        expected = f"CODEX_HOME={home} codex exec {flags} - < {prompt}"
    else:
        expected = f"CODEX_HOME={home} codex {flags}"
    assert lines == [f"cd {run_dir / 'charlie'}", expected]
    assert "--ephemeral" not in expected


def test_launch_lines_omit_unset_model_and_effort(tmp_path: Path) -> None:
    run_dir = (tmp_path / "run").resolve()
    alice = PREPARE_RUN.launch_lines(
        "codex", run_dir / "alice-runtime", run_dir / "configs" / "alice-codex",
        run_dir / "alice.prompt.md", None,
    )
    assert "--add-dir" not in alice[1] and "--model" not in alice[1] and " -c " not in alice[1]
    claude = PREPARE_RUN.launch_lines(
        "claude-code", run_dir / "alice-runtime", run_dir / "configs" / "alice.mcp.json",
        run_dir / "alice.prompt.md", None,
    )
    assert claude[1].startswith("claude --permission-mode auto --strict-mcp-config")


def test_launch_lines_windows_powershell(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(os, "name", "nt")
    run_dir = (tmp_path / "run").resolve()
    prompt = run_dir / "charlie.prompt.md"
    lines = PREPARE_RUN.launch_lines(
        "codex", run_dir / "charlie", run_dir / "configs" / "codex", prompt,
        run_dir / "charlie" / ".git", "gpt-6-sol", "high",
    )
    text = "\n".join(lines)
    assert "$env:CODEX_HOME" in text
    assert f"Get-Content -Raw '{prompt}' | codex exec" in text
    assert "CODEX_HOME=" not in text.replace("$env:CODEX_HOME", "")
    # PowerShell expands $ inside double quotes too, so paths are single-quoted.
    assert '"' not in text
    # PowerShell strips embedded double quotes; -c falls back to the raw string.
    assert "-c 'model_reasoning_effort=high'" in text
    assert PREPARE_RUN.model_flags("claude-code", "opus[1m]", "max", powershell=True) == [
        "--model", "'opus[1m]'", "--effort", "max",
    ]


@pytest.mark.parametrize("auto_start", [True, False])
@pytest.mark.parametrize("harness", ["claude-code", "codex"])
def test_start_scripts_take_metacharacter_paths_literally(
    tmp_path: Path, harness: str, auto_start: bool
) -> None:
    # $HOME, a backquote and quotes would expand or break inside double quotes.
    run_dir = (tmp_path / "my run $HOME `x` \"q\" 'a'").resolve()
    workdir = run_dir / "bob"
    workdir.mkdir(parents=True)
    config = run_dir / "configs" / ("bob.mcp.json" if harness == "claude-code" else "bob-codex")
    prompt = run_dir / "bob.prompt.md"
    prompt.write_text("prompt\n", encoding="utf-8")
    lines = PREPARE_RUN.launch_lines(
        harness, workdir, config, prompt, workdir / ".git", "opus[1m]", "high", auto_start
    )
    assert lines[0] == f"cd {shlex.quote(str(workdir))}"
    words = shlex.split(lines[1])
    if harness == "codex":
        assert words[0] == f"CODEX_HOME={config}"
        assert words[words.index("--add-dir") + 1] == str(workdir / ".git")
        if auto_start:
            assert words[-3:] == ["-", "<", str(prompt)]
    else:
        assert words[words.index("--mcp-config") + 1] == str(config)
        assert words[words.index("--add-dir") + 1] == str(run_dir)
        if auto_start:
            assert words[-1] == f"Read {prompt} and follow the instructions in it"
    # Run the script with the agent CLI stubbed: it must reach the real
    # directory, and Codex's stdin redirect the real prompt file.
    stub = "claude() { pwd; }; codex() { pwd; }"
    script = PREPARE_RUN.start_script([stub, *lines])
    result = subprocess.run(
        ["bash"], input=script, text=True, capture_output=True, cwd=tmp_path
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == str(workdir)


@pytest.mark.parametrize(
    "repository,slug,expected",
    [
        (
            "git@github.com:test-org/test-repo.git",
            "test-org/test-repo",
            "git@github.com:test-org/test-repo.git",
        ),
        (
            "https://github.com/test-org/test-repo.git",
            "test-org/test-repo",
            "https://github.com/test-org/test-repo.git",
        ),
    ],
)
def test_clone_source_passes_through_urls(repository: str, slug: str, expected: str) -> None:
    assert PREPARE_RUN.clone_source(repository, slug) == expected


def test_clone_source_passes_through_local_paths(tmp_path: Path) -> None:
    local = tmp_path / "local"
    local.mkdir()
    assert PREPARE_RUN.clone_source(str(local), None) == str(local)


def test_codex_login_status_skips_warning_preamble(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        PREPARE_RUN,
        "run_output",
        lambda *args, **kwargs: ("", "WARNING: proceeding anyway\nLogged in using ChatGPT\n"),
    )
    assert PREPARE_RUN.codex_login_status(Path("/tmp/home")) == "Logged in using ChatGPT"


def test_roadmap_passes_non_bare_numbers_through_verbatim() -> None:
    """Only a bare N or #N resolves against the target repo (#34)."""
    slug = "test-org/test-repo"
    repository = "git@github.com:test-org/test-repo.git"
    assert PREPARE_RUN.resolve_roadmap("other-org/other-repo#7", slug, repository) == (
        "other-org/other-repo#7"
    )
    # Surrounding whitespace is not stripped: " 2 " is not a bare number.
    assert PREPARE_RUN.resolve_roadmap(" 2 ", slug, repository) == " 2 "
    assert PREPARE_RUN.resolve_roadmap("2", slug, repository) == "test-org/test-repo#2"
    with pytest.raises(ValueError, match="must not be empty"):
        PREPARE_RUN.resolve_roadmap("   ", slug, repository)


@pytest.mark.parametrize(
    "kind,kwargs,check",
    [
        ("issue", {"issue": 42}, "Address issue"),
        ("work", {"work": "Do the thing."}, "Do the thing."),
        ("neither", {}, "Address issue `<issue-owner>"),
    ],
)
@pytest.mark.parametrize(
    "roadmap,expected_roadmap,has_roadmap_instructions",
    [
        (None, None, False),
        ("2", "test-org/test-repo#2", True),
        ("#2", "test-org/test-repo#2", True),
        ("other-org/other-repo#7", "other-org/other-repo#7", True),
    ],
)
def test_render_goal_roadmap_combinations(
    kind: str, kwargs: dict[str, object], check: str,
    roadmap: str | None, expected_roadmap: str | None,
    has_roadmap_instructions: bool,
) -> None:
    """Each {--issue, --work-file, neither} x {no roadmap, N, #N, OWNER/REPO#N} goal (#34)."""
    slug = "test-org/test-repo"
    repository = "git@github.com:test-org/test-repo.git"
    goal = PREPARE_RUN.render_goal(
        slug, repository,
        kwargs.get("issue"), kwargs.get("work"), roadmap,
    )
    assert check in goal
    if not has_roadmap_instructions:
        assert "no roadmap edit" in goal
        assert "should make a decision on what roadmap" not in goal
        assert PREPARE_RUN.resolve_roadmap(roadmap, slug, repository) is None
    else:
        assert "no roadmap edit" not in goal
        assert "should make a decision on what roadmap" in goal
        assert f"({expected_roadmap})" in goal
        assert PREPARE_RUN.resolve_roadmap(roadmap, slug, repository) == expected_roadmap
    # Bare numbers render against the target repo; other values pass through verbatim.
    if roadmap in ("2", "#2"):
        assert "(test-org/test-repo#2)" in goal
    if roadmap == "other-org/other-repo#7":
        assert "(other-org/other-repo#7)" in goal


def test_url_port_falls_back_to_the_scheme_default() -> None:
    assert PREPARE_RUN.url_port("http://hub.example", "--hub-url") == 80
    assert PREPARE_RUN.url_port("https://hub.example/", "--hub-url") == 443
    assert PREPARE_RUN.url_port("http://[::1]:8521", "--hub-url") == 8521


def test_remote_token_path_spells_the_wsl_share(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("WSL_DISTRO_NAME", "Ubuntu-24.04")
    assert PREPARE_RUN.remote_token_path(Path("/home/me/target/.robomate/token")) == (
        "\\\\wsl.localhost\\Ubuntu-24.04\\home\\me\\target\\.robomate\\token"
    )
    monkeypatch.delenv("WSL_DISTRO_NAME")
    assert PREPARE_RUN.remote_token_path(Path("/srv/run/token")) == "/srv/run/token"


def test_remote_codex_launch_uses_git_bash_only_where_the_shell_reads_it() -> None:
    lines = PREPARE_RUN.worker_launch(
        "bob", "codex", WINDOWS_RUN, WINDOWS_RUN / "bob", "gpt-6-sol", "high"
    )
    assert lines == [
        "cd /c/Users/Bob/runs/step7/bob",
        "CODEX_HOME=C:/Users/Bob/runs/step7/configs/bob-codex codex exec "
        "-C . --add-dir C:/Users/Bob/runs/step7/bob/.git --approve-for-me "
        "--model gpt-6-sol -c 'model_reasoning_effort=\"high\"' - "
        "< /c/Users/Bob/runs/step7/bob.prompt.md",
    ]
    posix = PurePosixPath("/srv/run")
    assert PREPARE_RUN.worker_launch(
        "charlie", "claude-code", posix, posix / "charlie", auto_start=False
    ) == [
        "cd /srv/run/charlie",
        "claude --strict-mcp-config --mcp-config /srv/run/configs/charlie.mcp.json "
        "--add-dir /srv/run",
    ]


def test_git_bash_path_only_rewrites_windows_drives() -> None:
    assert PREPARE_RUN.git_bash_path(PureWindowsPath("D:/Runs/x")) == "/d/Runs/x"
    assert PREPARE_RUN.git_bash_path(PurePosixPath("/srv/run")) == "/srv/run"
