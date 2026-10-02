"""A prepared run attaches all agents to an existing HTTP hub."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import shlex
import subprocess
import sys
import tomllib
from collections.abc import Callable
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any

import pytest
from agent_hub.database import initialize_database
from agent_hub.store import HubStore

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("prepare_run", ROOT / "scripts/prepare-run.py")
assert SPEC and SPEC.loader
PREPARE_RUN = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(PREPARE_RUN)
RUN_COMMON = sys.modules["run_common"]

# A local run's start scripts are PowerShell on a Windows host, bash elsewhere.
SCRIPT_SUFFIX = "ps1" if os.name == "nt" else "sh"
bash_launch_lines = pytest.mark.skipif(
    os.name == "nt",
    reason="a Windows host renders PowerShell launch lines "
    "(test_launch_lines_windows_powershell); these check the bash rendering",
)


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
                                                  "port": PREPARE_RUN.url_port(url, "test"),
                                                  "hub_id": "abc"}))
    return repo


def running_hub(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    url: str = "http://127.0.0.1:8521",
) -> Path:
    target = hub_repo(tmp_path, url)
    monkeypatch.setattr(
        PREPARE_RUN,
        "probe_versions",
        lambda _: (
            {"claude": "2.1", "codex": "0.1", "opencode": "1.18.32", "agy": "1.2.7"},
            {
                "claude-code": "2.1",
                "codex": "0.1",
                "opencode": "1.18.32",
                "antigravity": "1.2.7",
            },
        ),
    )
    monkeypatch.setattr(PREPARE_RUN, "hub_healthy", lambda *_: True)
    monkeypatch.setattr(PREPARE_RUN, "codex_login_status", lambda _: "logged in")
    return target


def prepare(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, **kwargs: Any
) -> tuple[Path, dict[str, Any]]:
    source = origin(tmp_path)
    target = running_hub(tmp_path, monkeypatch, kwargs.pop("test_hub_url", "http://127.0.0.1:8521"))
    run_dir = tmp_path / "run"
    kwargs.setdefault("issue", 42)
    kwargs.setdefault("account", "tester")
    manifest = PREPARE_RUN.prepare(str(source), run_dir, hub_repo=target, **kwargs)
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


def test_existing_hub_workflow_requires_the_same_run_and_goal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = origin(tmp_path)
    target = running_hub(tmp_path, monkeypatch)
    database_path = target / ".robomate/hub.db"
    initialize_database(database_path)
    run_dir = tmp_path / "run1"
    first = PREPARE_RUN.prepare(str(source), run_dir, issue=42, hub_repo=target)
    goal = first["work"]["goal"]
    HubStore(database_path).initialize_workflow(goal, first["policy"])
    modified = database_path.stat().st_mtime_ns

    with pytest.raises(ValueError) as error:
        PREPARE_RUN.prepare(str(source), tmp_path / "run2", issue=43, hub_repo=target)
    message = str(error.value)
    assert "status='active'" in message and repr(goal) in message
    assert "fresh dedicated target clone" in message and "robomate down" in message
    assert not (tmp_path / "run2").exists()
    assert database_path.stat().st_mtime_ns == modified

    with pytest.raises(ValueError, match="original run directory and goal"):
        PREPARE_RUN.prepare(str(source), run_dir, issue=43, hub_repo=target)
    with pytest.raises(ValueError, match="policy differs.*original preparation options"):
        PREPARE_RUN.prepare(str(source), run_dir, issue=42, hub_repo=target,
                            merge_method="merge")
    resumed = PREPARE_RUN.prepare(str(source), run_dir, issue=42, hub_repo=target)
    assert resumed["work"]["goal"] == goal


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


@bash_launch_lines
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


@bash_launch_lines
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
    assert "--skip-git-repo-check" not in expected


@bash_launch_lines
@pytest.mark.parametrize("auto_start", [True, False])
def test_opencode_launch_lines_carry_model_variant_and_prompt(
    tmp_path: Path, auto_start: bool
) -> None:
    run_dir = (tmp_path / "run").resolve()
    config = run_dir / "configs" / "charlie.opencode.json"
    prompt = run_dir / "charlie.prompt.md"
    if not auto_start:
        with pytest.raises(ValueError, match="opencode only accepts --variant"):
            PREPARE_RUN.launch_lines(
                "opencode",
                run_dir / "charlie",
                config,
                prompt,
                run_dir / "charlie" / ".git",
                "opencode/gemini-3.8-flash",
                "high",
                auto_start,
            )
    lines = PREPARE_RUN.launch_lines(
        "opencode",
        run_dir / "charlie",
        config,
        prompt,
        run_dir / "charlie" / ".git",
        "opencode/gemini-3.8-flash",
        "high" if auto_start else "",
        auto_start,
    )
    if auto_start:
        expected = (
            f"OPENCODE_CONFIG={config} opencode run --auto "
            "--model opencode/gemini-3.8-flash --variant high "
            f"'Read {prompt} and follow the instructions in it'"
        )
    else:
        expected = f"OPENCODE_CONFIG={config} opencode --auto --model opencode/gemini-3.8-flash"
    assert lines == [f"cd {run_dir / 'charlie'}", expected]


@bash_launch_lines
@pytest.mark.parametrize("auto_start", [True, False])
def test_antigravity_launch_lines_isolate_home_and_carry_model_effort(
    tmp_path: Path, auto_start: bool
) -> None:
    run_dir = (tmp_path / "run").resolve()
    home = run_dir / "configs" / "charlie-agy"
    prompt = run_dir / "charlie.prompt.md"
    lines = PREPARE_RUN.launch_lines(
        "antigravity",
        run_dir / "charlie",
        home,
        prompt,
        run_dir / "charlie" / ".git",
        "gemini-3.1-pro-high",
        "high",
        auto_start,
    )
    env_prefix = (
        'GIT_CONFIG_GLOBAL="${GIT_CONFIG_GLOBAL:-$HOME/.gitconfig}" '
        'XDG_CONFIG_HOME="${XDG_CONFIG_HOME:-$HOME/.config}" '
        'XDG_CACHE_HOME="${XDG_CACHE_HOME:-$HOME/.cache}" '
        'XDG_DATA_HOME="${XDG_DATA_HOME:-$HOME/.local/share}" '
        f"HOME={home} USERPROFILE={home}"
    )
    base = (
        f"{env_prefix} agy --model gemini-3.1-pro-high --effort high "
        f"--dangerously-skip-permissions --add-dir {run_dir}"
    )
    expected = (
        f"{base} -p 'Read {prompt} and follow the instructions in it'"
        if auto_start
        else base
    )
    assert lines == [f"cd {run_dir / 'charlie'}", expected]


def test_launch_lines_omit_unset_model_and_effort(tmp_path: Path) -> None:
    run_dir = (tmp_path / "run").resolve()
    alice = PREPARE_RUN.launch_lines(
        "codex", run_dir / "alice-runtime", run_dir / "configs" / "alice-codex",
        run_dir / "alice.prompt.md", None,
    )
    # PowerShell sets CODEX_HOME on a line of its own.
    command = alice[-1]
    assert "--add-dir" not in command and "--model" not in command and " -c " not in command
    # Alice's runtime is not a clone, so Codex must skip its git-repo check (#45).
    assert "-C . --skip-git-repo-check --approve-for-me" in command
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
    oc_lines = PREPARE_RUN.launch_lines(
        "opencode",
        run_dir / "charlie",
        run_dir / "configs" / "charlie.opencode.json",
        prompt,
        run_dir / "charlie" / ".git",
        "opencode/gemini-3.8-flash",
        "high",
    )
    assert "$env:OPENCODE_CONFIG =" in oc_lines[1]
    assert "opencode run --auto --model 'opencode/gemini-3.8-flash' --variant high" in oc_lines[2]
    agy_home = run_dir / "configs" / "charlie-agy"
    agy_lines = PREPARE_RUN.launch_lines(
        "antigravity",
        run_dir / "charlie",
        agy_home,
        prompt,
        run_dir / "charlie" / ".git",
        "gemini-3.1-pro-high",
        "high",
    )
    assert agy_lines[1] == (
        "$oldHome = $env:HOME; $oldProfile = $env:USERPROFILE; "
        "$oldGitConfig = $env:GIT_CONFIG_GLOBAL"
    )
    assert agy_lines[2].startswith(
        "try { "
        "if (-not $env:GIT_CONFIG_GLOBAL) { $env:GIT_CONFIG_GLOBAL = "
        'if ($oldHome) { "$oldHome\\.gitconfig" } '
        'else { "$env:USERPROFILE\\.gitconfig" } }; '
        f"$env:HOME = '{agy_home}'; $env:USERPROFILE = '{agy_home}'; "
        "agy --model gemini-3.1-pro-high --effort high --dangerously-skip-permissions"
    )
    assert agy_lines[2].endswith(
        "finally { $env:HOME = $oldHome; $env:USERPROFILE = $oldProfile; "
        "$env:GIT_CONFIG_GLOBAL = $oldGitConfig }"
    )


@bash_launch_lines
@pytest.mark.parametrize("auto_start", [True, False])
@pytest.mark.parametrize("harness", ["claude-code", "codex", "opencode", "antigravity"])
def test_start_scripts_take_metacharacter_paths_literally(
    tmp_path: Path, harness: str, auto_start: bool
) -> None:
    # $HOME, a backquote and quotes would expand or break inside double quotes.
    run_dir = (tmp_path / "my run $HOME `x` \"q\" 'a'").resolve()
    workdir = run_dir / "bob"
    workdir.mkdir(parents=True)
    config_names = {
        "claude-code": "bob.mcp.json",
        "codex": "bob-codex",
        "opencode": "bob.opencode.json",
        "antigravity": "bob-agy",
    }
    config = run_dir / "configs" / config_names[harness]
    prompt = run_dir / "bob.prompt.md"
    prompt.write_text("prompt\n", encoding="utf-8")
    effort = "" if (harness == "opencode" and not auto_start) else "high"
    lines = PREPARE_RUN.launch_lines(
        harness, workdir, config, prompt, workdir / ".git", "opus[1m]", effort, auto_start
    )
    assert lines[0] == f"cd {shlex.quote(str(workdir))}"
    words = shlex.split(lines[1])
    if harness == "codex":
        assert words[0] == f"CODEX_HOME={config}"
        assert words[words.index("--add-dir") + 1] == str(workdir / ".git")
        if auto_start:
            assert words[-3:] == ["-", "<", str(prompt)]
    elif harness == "opencode":
        assert words[0] == f"OPENCODE_CONFIG={config}"
        if auto_start:
            assert words[-1] == f"Read {prompt} and follow the instructions in it"
    elif harness == "antigravity":
        assert words[:6] == [
            "GIT_CONFIG_GLOBAL=${GIT_CONFIG_GLOBAL:-$HOME/.gitconfig}",
            "XDG_CONFIG_HOME=${XDG_CONFIG_HOME:-$HOME/.config}",
            "XDG_CACHE_HOME=${XDG_CACHE_HOME:-$HOME/.cache}",
            "XDG_DATA_HOME=${XDG_DATA_HOME:-$HOME/.local/share}",
            f"HOME={config}",
            f"USERPROFILE={config}",
        ]
        assert words[words.index("--add-dir") + 1] == str(run_dir)
        if auto_start:
            assert words[-1] == f"Read {prompt} and follow the instructions in it"
    else:
        assert words[words.index("--mcp-config") + 1] == str(config)
        assert words[words.index("--add-dir") + 1] == str(run_dir)
        if auto_start:
            assert words[-1] == f"Read {prompt} and follow the instructions in it"
    # Run the script with the agent CLI stubbed: it must reach the real
    # directory, and Codex's stdin redirect the real prompt file.
    stub = "claude() { pwd; }; codex() { pwd; }; opencode() { pwd; }; agy() { pwd; }"
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
    # The hub host is WSL, so its token path is POSIX whatever runs the test.
    assert PREPARE_RUN.remote_token_path(PurePosixPath("/home/me/target/.robomate/token")) == (
        "\\\\wsl.localhost\\Ubuntu-24.04\\home\\me\\target\\.robomate\\token"
    )
    monkeypatch.delenv("WSL_DISTRO_NAME")
    assert PREPARE_RUN.remote_token_path(PurePosixPath("/srv/run/token")) == "/srv/run/token"


def test_remote_codex_launch_uses_git_bash_only_where_the_shell_reads_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    lines = PREPARE_RUN.worker_launch(
        "bob", "codex", WINDOWS_RUN, WINDOWS_RUN / "bob", "gpt-6-sol", "high"
    )
    assert lines == [
        "cd /c/Users/Bob/runs/step7/bob",
        "export TMPDIR=/c/Users/Bob/runs/step7/tmp/bob",
        "export TEMP=C:/Users/Bob/runs/step7/tmp/bob",
        "export TMP=C:/Users/Bob/runs/step7/tmp/bob",
        "CODEX_HOME=C:/Users/Bob/runs/step7/configs/bob-codex codex exec "
        "-C . --add-dir C:/Users/Bob/runs/step7/bob/.git --approve-for-me "
        "--model gpt-6-sol -c 'model_reasoning_effort=\"high\"' - "
        "< /c/Users/Bob/runs/step7/bob.prompt.md",
    ]
    posix = PurePosixPath("/srv/run")
    # A POSIX worker host renders its own lines; worker_launch reads the host.
    with monkeypatch.context() as patch:
        patch.setattr(os, "name", "posix")
        posix_lines = PREPARE_RUN.worker_launch(
            "charlie", "claude-code", posix, posix / "charlie", auto_start=False
        )
    assert posix_lines == [
        "cd /srv/run/charlie",
        "export TMPDIR=/srv/run/tmp/charlie",
        "claude --strict-mcp-config --mcp-config /srv/run/configs/charlie.mcp.json "
        "--add-dir /srv/run",
    ]


def test_git_bash_path_only_rewrites_windows_drives() -> None:
    assert PREPARE_RUN.git_bash_path(PureWindowsPath("D:/Runs/x")) == "/d/Runs/x"
    assert PREPARE_RUN.git_bash_path(PurePosixPath("/srv/run")) == "/srv/run"


def test_work_file_becomes_the_goal_and_manifest_entry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    statement = "Address `acme/app#7` and `acme/app#9` in one pull request.\n"
    work_file = tmp_path / "sow.md"
    work_file.write_bytes(statement.encode())
    run_dir, manifest = prepare(tmp_path, monkeypatch, issue=None, work_file=work_file)
    goal = manifest["work"]["goal"]
    assert goal.startswith(statement.strip()) and "no roadmap edit" in goal
    assert manifest["issue"] is None
    assert manifest["work"] == {
        "goal": goal, "path": str(work_file.resolve()),
        "sha256": hashlib.sha256(statement.encode()).hexdigest(), "roadmap": None,
    }
    assert json.loads((run_dir / "run.json").read_text())["work"] == manifest["work"]


def test_issue_goal_defaults_to_no_roadmap_edit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_dir, manifest = prepare(tmp_path, monkeypatch)
    goal = manifest["work"]["goal"]
    assert goal == (f"Address issue `{tmp_path / 'origin'}#42`, merge its pull request, "
                    "and close out with no roadmap edit; record the merge only in the "
                    "workflow summary.")
    assert goal in (run_dir / "alice.prompt.md").read_text()
    assert manifest["work"]["roadmap"] is None


def test_prepare_records_the_rendered_roadmap_in_run_json(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_dir, manifest = prepare(tmp_path, monkeypatch, roadmap="2")
    assert manifest["work"]["roadmap"] == f"{tmp_path / 'origin'}#2"
    assert f"({tmp_path / 'origin'}#2)" in manifest["work"]["goal"]
    assert json.loads((run_dir / "run.json").read_text())["work"] == manifest["work"]


def test_work_file_with_roadmap_appends_bob_instructions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    work_file = tmp_path / "sow.md"
    work_file.write_text("Address two issues together.\n")
    _, manifest = prepare(tmp_path, monkeypatch, issue=None, work_file=work_file,
                          roadmap="5")
    assert manifest["work"]["roadmap"] == f"{tmp_path / 'origin'}#5"
    assert "should make a decision on what roadmap" in manifest["work"]["goal"]


def test_work_file_with_issue_fails_actionably(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    work_file = tmp_path / "sow.md"
    work_file.write_text("Do the work.\n")
    with pytest.raises(ValueError, match="mutually exclusive"):
        prepare(tmp_path, monkeypatch, work_file=work_file)
    assert not (tmp_path / "run").exists()


@pytest.mark.parametrize("content,expected", [(None, "does not exist"),
                                                ("", "is empty"), (" \n\t", "is empty")])
def test_missing_or_empty_work_file_fails_actionably(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, content: str | None, expected: str,
) -> None:
    work_file = tmp_path / "sow.md"
    if content is not None:
        work_file.write_text(content)
    with pytest.raises(ValueError, match=expected):
        prepare(tmp_path, monkeypatch, issue=None, work_file=work_file)
    assert not (tmp_path / "run").exists()


def test_fails_when_gh_unauthenticated(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = hub_repo(tmp_path)
    monkeypatch.setattr(PREPARE_RUN, "hub_healthy", lambda *_: True)
    monkeypatch.setattr(PREPARE_RUN, "probe_versions",
                        lambda _: ({"claude": "2", "codex": "1"},
                                   {"claude-code": "2", "codex": "1"}))
    monkeypatch.setattr(PREPARE_RUN, "check_gh_auth",
                        lambda: (_ for _ in ()).throw(ValueError("gh is not authenticated")))
    with pytest.raises(ValueError, match="authenticated"):
        PREPARE_RUN.prepare("test-org/test-repo", (tmp_path / "run").resolve(),
                            hub_repo=target)


def test_fails_when_runtime_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = origin(tmp_path)
    target = hub_repo(tmp_path)
    monkeypatch.setattr(PREPARE_RUN, "hub_healthy", lambda *_: True)
    monkeypatch.setattr(PREPARE_RUN, "probe_versions",
                        lambda _: (_ for _ in ()).throw(
                            ValueError("requires the 'codex' CLI on PATH")))
    with pytest.raises(ValueError, match="requires the 'codex' CLI on PATH"):
        PREPARE_RUN.prepare(str(source), (tmp_path / "run").resolve(),
                            hub_repo=target)


def test_no_token_or_clone_path_inside_clones(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_dir, manifest = prepare(tmp_path, monkeypatch)
    token = "test-token"
    for name, other in (("bob", "charlie"), ("charlie", "bob")):
        clone = Path(manifest["workspaces"][name]["path"])
        other_path = manifest["workspaces"][other]["path"]
        for path in clone.rglob("*"):
            if not path.is_file() or ".git" in path.parts:
                continue
            text = path.read_text(errors="replace")
            assert token not in text and other_path not in text
        assert not (clone / "bob.mcp.json").exists()
        assert not (clone / "charlie.prompt.md").exists()
        assert not (clone / "token").exists()
    assert (run_dir / "configs/bob.mcp.json").exists()


GOLDEN = ROOT / "tests/fixtures/prepare-run-default"
GOLDEN_FILES = {
    "alice.mcp.json": "configs/alice.mcp.json",
    "bob.mcp.json": "configs/bob.mcp.json",
    "codex.config.toml": "configs/codex/config.toml",
    "run.json": "run.json",
    "start-alice.sh": "start-alice.sh",
    "start-bob.sh": "start-bob.sh",
    "start-charlie.sh": "start-charlie.sh",
}


@pytest.mark.skipif(
    os.name == "nt",
    reason="the golden files are a POSIX host's bash start scripts and path spellings",
)
def test_default_rendering_matches_the_golden_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    source = origin(tmp_path)
    target = hub_repo(tmp_path, "http://127.0.0.1:8420")
    monkeypatch.setattr(PREPARE_RUN, "hub_healthy", lambda *_: True)
    monkeypatch.setattr(PREPARE_RUN, "probe_versions",
                        lambda _: ({"claude": "2.1.277 (Claude Code)",
                                    "codex": "codex-cli 0.154.0"},
                                   {"claude-code": "2.1.277", "codex": "0.154.0"}))
    monkeypatch.setattr(PREPARE_RUN, "codex_login_status",
                        lambda _: "Logged in using ChatGPT")
    run_dir = tmp_path / "run"
    manifest = PREPARE_RUN.prepare(str(source), run_dir, issue=42,
                                   account="testuser", hub_repo=target)
    masks = {
        str(run_dir): "$RUN_DIR", str(source): "$ORIGIN",
        str(target): "$HUB_REPO", str(ROOT): "$ROOT",
        manifest["workspaces"]["bob"]["workspace_id"]: "$BOB_ID",
        manifest["workspaces"]["charlie"]["workspace_id"]: "$CHARLIE_ID",
    }
    rendered = {name: (run_dir / path).read_text() for name, path in GOLDEN_FILES.items()}
    rendered["stdout.txt"] = capsys.readouterr().out
    for name, content in rendered.items():
        for value, mask in masks.items():
            content = content.replace(value, mask)
        assert content == (GOLDEN / name).read_text(), name


def test_no_auto_start_opens_each_agent_without_its_prompt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_dir, manifest = prepare(tmp_path, monkeypatch, alice_harness="codex",
                                charlie_model="gpt-6-sol", auto_start=False)
    assert manifest["launch"]["auto_start"] is False
    for name in ("alice", "bob", "charlie"):
        script = (run_dir / f"start-{name}.{SCRIPT_SUFFIX}").read_text()
        assert "prompt.md" not in script and " -p " not in script and "exec" not in script
    alice_script = (run_dir / f"start-alice.{SCRIPT_SUFFIX}").read_text()
    assert "codex -C . --approve-for-me" in alice_script
    assert "--skip-git-repo-check" not in alice_script


def test_codex_alice_exec_uses_skip_git_repo_check(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_dir, _ = prepare(tmp_path, monkeypatch, alice_harness="codex",
                         alice_model="gpt-6-luna", alice_effort="xhigh")
    script = (run_dir / f"start-alice.{SCRIPT_SUFFIX}").read_text()
    assert "codex exec -C . --skip-git-repo-check --approve-for-me" in script
    home = run_dir / "configs/alice-codex"
    config = tomllib.loads((home / "config.toml").read_text())
    assert set(config["mcp_servers"]["robomate"]["enabled_tools"]) == set(
        PREPARE_RUN.ALICE_TOOLS
    )


@pytest.mark.parametrize("value", ["gemini", "claude-desktop", ""])
def test_unsupported_harness_names_fail_before_any_artifact(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, value: str,
) -> None:
    monkeypatch.setattr(sys, "argv", ["prepare-run.py", "--repository", "test-org/test-repo",
                                  "--run-dir", str(tmp_path / "run"),
                                  "--alice-harness", value])
    with pytest.raises(SystemExit, match="expected claude, codex, opencode, or antigravity"):
        PREPARE_RUN.main()
    assert not (tmp_path / "run").exists()


def test_remote_worker_config_carries_windows_paths_and_a_private_token(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AGY_HOME", str(tmp_path / "no-agy-login"))
    root = PureWindowsPath("C:/work/robomate")
    run = PureWindowsPath("C:/Users/Bob/runs/step7")
    out_dir = tmp_path / "bundle"
    PREPARE_RUN.render_worker_bundle(
        "bob", "claude-code", "2.1.277", "anthropic", "", "",
        "C:/private/token", "http://172.26.115.68:8420", root, run,
        run / "bob", out_dir,
    )
    written = out_dir / "configs/bob.mcp.json"
    text = written.read_text()
    assert "\\\\" not in text
    bridge = json.loads(text)["mcpServers"]["robomate"]
    assert bridge["args"][-3:] == ["mcp", "--role", "worker"]
    assert bridge["env"]["ROBOMATE_TOKEN_FILE"] == "C:/private/token"
    assert bridge["env"]["HUB_WORKSPACE"] == "C:/Users/Bob/runs/step7/bob"
    assert "HUB_TOKEN" not in bridge["env"]
    if os.name != "nt":
        assert written.stat().st_mode & 0o077 == 0

    oc_out = tmp_path / "oc-bundle"
    oc_bundle = PREPARE_RUN.render_worker_bundle(
        "bob", "opencode", "1.18.32", "anthropic", "opencode/claude-sonnet-4-6", "",
        "C:/private/token", "http://172.26.115.68:8420", root, run,
        run / "bob", oc_out,
    )
    assert oc_bundle["config"] == "C:/Users/Bob/runs/step7/configs/bob.opencode.json"
    oc_written = oc_out / "configs/bob.opencode.json"
    oc_mcp = json.loads(oc_written.read_text())["mcp"]["robomate"]
    assert oc_mcp["command"][-3:] == ["mcp", "--role", "worker"]
    assert oc_mcp["environment"]["ROBOMATE_TOKEN_FILE"] == "C:/private/token"
    assert oc_mcp["environment"]["HUB_PROVIDER"] == "anthropic"
    if os.name != "nt":
        assert oc_written.stat().st_mode & 0o077 == 0

    agy_out = tmp_path / "agy-bundle"
    agy_bundle = PREPARE_RUN.render_worker_bundle(
        "bob", "antigravity", "1.2.7", "google", "gemini-3.1-pro-high", "",
        "C:/private/token", "http://172.26.115.68:8420", root, run,
        run / "bob", agy_out,
    )
    assert agy_bundle["config"] == (
        "C:/Users/Bob/runs/step7/configs/bob-agy/.gemini/config/mcp_config.json"
    )
    agy_written = agy_out / "configs/bob-agy/.gemini/config/mcp_config.json"
    agy_mcp = json.loads(agy_written.read_text())["mcpServers"]["robomate"]
    assert agy_mcp["args"][-3:] == ["mcp", "--role", "worker"]
    assert agy_mcp["env"]["ROBOMATE_TOKEN_FILE"] == "C:/private/token"
    assert agy_mcp["env"]["HUB_PROVIDER"] == "google"
    if os.name != "nt":
        assert agy_written.stat().st_mode & 0o077 == 0
    agy_script = (agy_out / "start-bob.sh").read_text(encoding="utf-8")
    assert (
        'GIT_CONFIG_GLOBAL="${GIT_CONFIG_GLOBAL:-$HOME/.gitconfig}" '
        "HOME=C:/Users/Bob/runs/step7/configs/bob-agy "
        "USERPROFILE=C:/Users/Bob/runs/step7/configs/bob-agy agy"
    ) in agy_script
    assert "XDG_CONFIG_HOME" not in agy_script


def test_worker_only_rejects_loose_missing_token_or_loopback_url(
    tmp_path: Path,
) -> None:
    run_dir = tmp_path / "worker-run"
    token = tmp_path / "token"
    token.write_text("secret\n")
    if os.name != "nt":
        # Windows mode bits say nothing; the token inherits its directory's ACL.
        token.chmod(0o644)
        with pytest.raises(ValueError, match="owner-only"):
            PREPARE_RUN.prepare_worker("bob", "o/r", run_dir,
                                       "http://192.0.2.10:8420", token, "claude-code")
    token.chmod(0o600)
    with pytest.raises(ValueError, match="cannot be loopback"):
        PREPARE_RUN.prepare_worker("bob", "o/r", run_dir,
                                   "http://127.0.0.1:8420", token, "claude-code")
    with pytest.raises(ValueError, match="does not exist"):
        PREPARE_RUN.prepare_worker("bob", "o/r", run_dir,
                                   "http://192.0.2.10:8420", tmp_path / "missing", "claude-code")
    assert not run_dir.exists()


def test_worker_only_refuses_hub_flags(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    token = tmp_path / "token"
    token.write_text("secret\n")
    token.chmod(0o600)
    for extra in (["--issue", "42"], ["--roadmap", "2"], ["--hub-port", "8521"]):
        monkeypatch.setattr(sys, "argv", ["prepare-run.py", "--repository", "o/r",
                                      "--run-dir", str(tmp_path / "run"), "--worker-only",
                                      "bob", "--token-file", str(token), *extra])
        with pytest.raises(SystemExit):
            PREPARE_RUN.main()
    assert not (tmp_path / "run").exists()


@pytest.mark.parametrize("protocol,expected", [
    ("https", "https://github.com/test-org/test-repo.git"),
    ("ssh", "git@github.com:test-org/test-repo.git"),
    ("", "https://github.com/test-org/test-repo.git"),
])
def test_slug_clone_url_honors_gh_protocol(
    monkeypatch: pytest.MonkeyPatch, protocol: str, expected: str,
) -> None:
    monkeypatch.setattr(RUN_COMMON, "run", lambda *_: protocol)
    assert RUN_COMMON.slug_clone_url("test-org/test-repo") == expected
    assert PREPARE_RUN.clone_source("test-org/test-repo", "test-org/test-repo") == expected


def test_slug_clone_url_falls_back_without_gh(monkeypatch: pytest.MonkeyPatch) -> None:
    def missing(*_args: Any) -> str:
        raise OSError("no gh on PATH")

    monkeypatch.setattr(RUN_COMMON, "run", missing)
    assert RUN_COMMON.slug_clone_url("test-org/test-repo") == (
        "https://github.com/test-org/test-repo.git"
    )


def test_remote_worker_is_left_to_its_host(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    remote_url = "http://192.0.2.10:8420"
    run_dir, manifest = prepare(tmp_path, monkeypatch, remote_worker="bob",
                                test_hub_url=remote_url, bob_model="claude-opus-5-5")
    assert not (run_dir / "bob").exists()
    assert not (run_dir / "configs/bob.mcp.json").exists()
    assert not (run_dir / "bob.prompt.md").exists()
    assert set(manifest["workspaces"]) == {"charlie"}
    charlie = tomllib.loads((run_dir / "configs/codex/config.toml").read_text())
    assert charlie["mcp_servers"]["robomate"]["env"]["ROBOMATE_HUB_URL"] == remote_url
    output = capsys.readouterr().out
    command = next(line for line in output.splitlines() if "--worker-only bob" in line)
    assert f"--hub-url '{remote_url}'" in command
    expected_token_path = PREPARE_RUN.remote_token_path(tmp_path / "target/.robomate/token")
    assert f"--token-file '{expected_token_path}'" in command
    assert "--bob-model 'claude-opus-5-5'" in command
    assert "test-token" not in output


def test_remote_worker_command_forwards_effort_and_auto_start(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    run_dir, _ = prepare(tmp_path, monkeypatch, remote_worker="charlie",
                         test_hub_url="http://192.0.2.10:8420", charlie_effort="high",
                         auto_start=False)
    output = capsys.readouterr().out
    command = next(line for line in output.splitlines() if "--worker-only charlie" in line)
    assert "--charlie-effort 'high'" in command and command.endswith("--no-auto-start")
    assert not (run_dir / f"start-charlie.{SCRIPT_SUFFIX}").exists()
    assert (run_dir / f"start-bob.{SCRIPT_SUFFIX}").exists()


def test_rerun_is_idempotent_and_preserves_clone_token_identity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = origin(tmp_path)
    target = hub_repo(tmp_path)
    monkeypatch.setattr(PREPARE_RUN, "hub_healthy", lambda *_: True)
    monkeypatch.setattr(PREPARE_RUN, "probe_versions",
                        lambda _: ({"claude": "2", "codex": "1"},
                                   {"claude-code": "2", "codex": "1"}))
    monkeypatch.setattr(PREPARE_RUN, "codex_login_status", lambda _: "logged in")
    run_dir = tmp_path / "run"
    first = PREPARE_RUN.prepare(str(source), run_dir, hub_repo=target)
    token = (target / ".robomate/token").read_text()
    identity = (run_dir / "bob/.git/robo-agents-workspace.json").read_text()
    marker = run_dir / "bob/uncommitted-marker"
    marker.write_text("preserve")
    with pytest.raises(ValueError, match="dirty"):
        PREPARE_RUN.prepare(str(source), run_dir, hub_repo=target)
    assert (target / ".robomate/token").read_text() == token
    marker.unlink()
    second = PREPARE_RUN.prepare(str(source), run_dir, hub_repo=target)
    assert second["workspaces"] == first["workspaces"]
    assert (run_dir / "bob/.git/robo-agents-workspace.json").read_text() == identity
    assert (target / ".robomate/token").read_text() == token


def test_bootstrap_failure_message_keeps_tail_without_traceback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    stderr = (
        "Traceback (most recent call last):\n"
        '  File "bootstrap-workspace.py", line 1, in <module>\n'
        "    subprocess.run(...)\n"
        "Cloning into 'x'...\n"
        "fatal: could not read Username for 'https://github.com': No such device\n"
    )

    def runner(*args: Any, **kwargs: Any) -> str:
        raise subprocess.CalledProcessError(1, list(args), output="", stderr=stderr)

    monkeypatch.setattr(RUN_COMMON, "run", runner)
    with pytest.raises(ValueError, match="could not read Username") as exc:
        PREPARE_RUN.bootstrap_clone("bob", tmp_path / "bob", "test-org/test-repo")
    assert "Traceback" not in str(exc.value)
    assert "subprocess.py" not in str(exc.value)


def test_main_reports_actionable_error_without_traceback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(sys, "argv", [
        "prepare-run.py", "--repository", "test-org/test-repo",
        "--run-dir", str((tmp_path / "run").resolve()), "--bob", "nosuch",
    ])
    with pytest.raises(SystemExit) as exc:
        PREPARE_RUN.main()
    assert "prepare-run: error:" in str(exc.value.code)
    assert "not yet supported" in str(exc.value.code)


def test_fresh_run_produces_every_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    run_dir, manifest = prepare(tmp_path, monkeypatch, allow_no_ci="auto")
    assert manifest["workspaces"]["bob"]["workspace_id"] != (
        manifest["workspaces"]["charlie"]["workspace_id"]
    )
    assert manifest["clone_repository"] == str(tmp_path / "origin")
    assert manifest["policy"]["allow_no_ci"] is True
    assert manifest["policy"]["merge_method"] == "squash"
    bob = json.loads((run_dir / "configs/bob.mcp.json").read_text())
    bob_env = bob["mcpServers"]["robomate"]["env"]
    assert bob_env["AGENT_NAME"] == "bob"
    assert bob_env["HUB_HARNESS"] == "claude-code"
    assert bob_env["HUB_PROVIDER"] == "anthropic"
    assert bob_env["HUB_WORKSPACE"] == manifest["workspaces"]["bob"]["path"]
    codex = tomllib.loads((run_dir / "configs/codex/config.toml").read_text())
    assert set(codex["mcp_servers"]["robomate"]["enabled_tools"]) == {
        "check_in", "get_role_guide", "await_assignment", "report_progress",
        "ask_alice", "submit_result",
    }
    for name in ("alice", "bob", "charlie"):
        assert (run_dir / f"{name}.prompt.md").exists()
        assert (run_dir / f"start-{name}.{SCRIPT_SUFFIX}").exists()
    assert (run_dir / "alice-runtime/.claude/skills/alice-orchestrator").exists()
    report = json.loads(capsys.readouterr().out.split("\nStart scripts")[0])
    assert set(report["configs"]) == {"alice", "bob", "charlie"}


def test_all_claude_run_renders_start_scripts_prompts_and_manifest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    source = origin(tmp_path)
    target = running_hub(tmp_path, monkeypatch)
    run_dir = tmp_path / "run"
    monkeypatch.setattr(sys, "argv", [
        "prepare-run.py", "--repository", str(source), "--run-dir", str(run_dir),
        "--hub-repo", str(target), "--issue", "42", "--alice-harness", "Claude",
        "--alice-model", "sonnet", "--alice-effort", "high",
        "--bob-harness", "CLAUDE", "--bob-model", "claude-opus-5-5",
        "--bob-effort", "max", "--charlie-harness", "claude",
        "--charlie-effort", "medium",
    ])
    PREPARE_RUN.main()
    report = json.loads(capsys.readouterr().out.split("\nStart scripts")[0])
    manifest = json.loads((run_dir / "run.json").read_text())
    assert manifest["harnesses"] == {"bob": "claude-code", "charlie": "claude-code"}
    expected = {"alice": ("sonnet", "high"), "bob": ("claude-opus-5-5", "max"),
                "charlie": ("", "medium")}
    for name, (model, effort) in expected.items():
        script = run_dir / f"start-{name}.{SCRIPT_SUFFIX}"
        assert report["start_scripts"][name] == str(script)
        assert manifest["launch"]["agents"][name]["model"] == model
        assert manifest["launch"]["agents"][name]["effort"] == effort
        assert os.access(script, os.X_OK)
        command = script.read_text()
        assert "--permission-mode auto --strict-mcp-config" in command
        assert f"Read {run_dir / f'{name}.prompt.md'} and follow" in command
        assert f"`closeout-report-{name}.md`" in (run_dir / f"{name}.prompt.md").read_text()
    bob_env = json.loads((run_dir / "configs/bob.mcp.json").read_text())[
        "mcpServers"]["robomate"]["env"]
    assert bob_env["HUB_MODEL"] == "claude-opus-5-5"


def test_same_harness_pair_renders_both_claude_configs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_dir, manifest = prepare(tmp_path, monkeypatch, bob_harness="claude-code",
                                charlie_harness="claude-code", bob_model="claude-sonnet-5",
                                charlie_model="claude-sonnet-5")
    assert manifest["policy"]["role_policy"]["reviewer_harness_differs"] is False
    assert (run_dir / "configs/charlie.mcp.json").exists()
    charlie = json.loads((run_dir / "configs/charlie.mcp.json").read_text())
    assert charlie["mcpServers"]["robomate"]["env"]["AGENT_NAME"] == "charlie"


def test_bare_slug_expands_via_gh_protocol(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = running_hub(tmp_path, monkeypatch)
    seen: list[tuple[str, str]] = []

    def fake_bootstrap(agent: str, destination: Path, repository: str) -> dict[str, str]:
        seen.append((agent, repository))
        return {"path": str(destination), "workspace_id": ("a" if agent == "bob" else "b") * 64}

    monkeypatch.setattr(PREPARE_RUN, "bootstrap_clone", fake_bootstrap)
    for protocol, expected in (("https", "https://github.com/test-org/test-repo.git"),
                               ("ssh", "git@github.com:test-org/test-repo.git")):
        monkeypatch.setattr(RUN_COMMON, "run", lambda *_args, value=protocol: value)
        manifest = PREPARE_RUN.prepare("test-org/test-repo", tmp_path / f"{protocol}-run",
                                       hub_repo=target, skip_github_checks=True)
        assert manifest["clone_repository"] == expected
        prompt = (tmp_path / f"{protocol}-run" / "alice.prompt.md").read_text(encoding="utf-8")
        assert "Forge: github (host: github.com, project: test-org/test-repo)." in prompt
    assert seen == [
        ("bob", "https://github.com/test-org/test-repo.git"),
        ("charlie", "https://github.com/test-org/test-repo.git"),
        ("bob", "git@github.com:test-org/test-repo.git"),
        ("charlie", "git@github.com:test-org/test-repo.git"),
    ]


def test_both_codex_workers_report_each_login(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    run_dir, _ = prepare(tmp_path, monkeypatch, bob_harness="codex",
                         charlie_harness="codex")
    report = json.loads(capsys.readouterr().out.split("\nStart scripts")[0])
    assert set(report["checks"]["codex_auth"]) == {"bob", "charlie"}
    assert (run_dir / "configs/bob-codex/config.toml").exists()


@pytest.mark.parametrize(
    "harness,model,provider,expected",
    [
        ("claude-code", "", None, "anthropic"),
        ("claude-code", "sonnet", None, "anthropic"),
        ("claude-code", "glm-5.3", None, "zhipu"),
        ("codex", "", None, "openai"),
        ("gemini", "", None, "google"),
        ("opencode", "opencode/claude-sonnet-4-6", None, "anthropic"),
        ("opencode", "openrouter/~anthropic/claude-sonnet-4-6", None, "anthropic"),
        ("opencode", "openrouter/qwen/qwen3-coder", None, "alibaba"),
        ("opencode", "alibaba/kimi-k2.5", None, "moonshot"),
        ("opencode", "opencode/glm-5.1", None, "zhipu"),
        ("opencode", "opencode/kimi-k2.5", None, "moonshot"),
        ("opencode", "opencode/minimax-m2.5", None, "minimax"),
        ("opencode", "opencode/gemini-3.1-pro", None, "google"),
        ("opencode", "opencode/gpt-5.4", None, "openai"),
        ("opencode", "opencode/o3", None, "openai"),
        ("opencode", "openrouter/deepseek/deepseek-r1", None, "deepseek"),
        ("opencode", "openrouter/x-ai/grok-4", None, "xai"),
        ("opencode", "openrouter/meta-llama/llama-4-maverick", None, "meta"),
        ("opencode", "openrouter/mistralai/codestral", None, "mistral"),
        ("opencode", "openrouter/eleutherai/gpt-neox-20b", None, "eleutherai"),
        ("opencode", "openrouter/allenai/olmo-2-32b", None, "allenai"),
        ("opencode", "openrouter/bytedance-seed/seed-coder", None, "bytedance"),
        ("opencode", "openrouter/openrouter/auto", None, "unknown"),
        ("opencode", "openrouter/stealth/space-bunny-alpha", None, "unknown"),
        ("opencode", "opencode/big-pickle", None, "unknown"),
        ("opencode", "", None, "unknown"),
        ("antigravity", "gemini-3.1-pro-high", None, "google"),
        ("antigravity", "claude-sonnet-4-6", None, "anthropic"),
        ("antigravity", "gpt-oss-120b-medium", None, "openai"),
        ("antigravity", "", None, "unknown"),
        ("opencode", "opencode/big-pickle", "stealth-lab", "stealth-lab"),
        ("antigravity", "claude-sonnet-4-6", "custom-vendor", "custom-vendor"),
    ],
)
def test_resolve_provider_records_model_maker_lineage(
    harness: str, model: str, provider: str | None, expected: str,
) -> None:
    assert RUN_COMMON.resolve_provider(harness, model, provider) == expected


def test_agy_home_links_cli_auth_without_operator_dotdirs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_user_home = tmp_path / "user-home"
    cli_dir = fake_user_home / ".gemini" / "antigravity-cli"
    cli_dir.mkdir(parents=True)
    (cli_dir / "antigravity-oauth-token").write_text("{\"access_token\":\"linux\"}\n")
    (cli_dir / "jetski_state.pbtxt").write_text("oauth_token: 'win'\n")
    (cli_dir / "settings.json").write_text("{\"theme\":\"dark\"}\n")
    (fake_user_home / ".gitconfig").write_text("[user]\n\tname = Tester\n")
    (fake_user_home / ".git-credentials").write_text("https://user:pass@github.com\n")
    for dirname in (".ssh", ".config", ".cache", ".local"):
        d = fake_user_home / dirname
        d.mkdir()
        (d / "marker.txt").write_text(dirname)
    monkeypatch.setenv("AGY_HOME", str(fake_user_home))

    configs = tmp_path / "configs"
    configs.mkdir()
    isolated = RUN_COMMON.agy_home(configs, "bob-agy")
    isolated_cli = isolated / ".gemini" / "antigravity-cli"
    for filename in ("antigravity-oauth-token", "jetski_state.pbtxt", "settings.json"):
        assert (isolated_cli / filename).read_text() == (cli_dir / filename).read_text()
    assert (isolated / ".git-credentials").read_text() == "https://user:pass@github.com\n"
    for untouched in (".gitconfig", ".ssh", ".config", ".cache", ".local"):
        assert not (isolated / untouched).exists()
        assert not (isolated / untouched).is_symlink()
    assert PREPARE_RUN.agy_login_status(isolated) == "logged in (antigravity-oauth-token)"

    empty_home = tmp_path / "empty-home"
    empty_home.mkdir()
    monkeypatch.setenv("AGY_HOME", str(empty_home))
    unauthed = RUN_COMMON.agy_home(configs, "charlie-agy")
    assert PREPARE_RUN.agy_login_status(unauthed).startswith("not logged in")


def test_link_credential_reports_cross_volume_failure_actionably(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "source-token"
    source.write_text("secret\n")
    target = tmp_path / "dest" / "token"
    target.parent.mkdir()

    def fail_link(*_args: Any, **_kwargs: Any) -> None:
        raise OSError("cross-device link")

    monkeypatch.setattr(Path, "symlink_to", fail_link)
    monkeypatch.setattr(os, "link", fail_link)
    with pytest.raises(ValueError, match="place RUN_DIR on the drive holding AGY_HOME"):
        RUN_COMMON.link_credential(source, target, "AGY_HOME")


async def test_opencode_and_antigravity_harnesses_render_configs_skills_and_scripts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    from agent_hub.mcp import create_mcp

    fake_agy_home = tmp_path / "user-home"
    cli_dir = fake_agy_home / ".gemini" / "antigravity-cli"
    cli_dir.mkdir(parents=True)
    (cli_dir / "antigravity-oauth-token").write_text("{\"access_token\":\"fake\"}\n")
    (fake_agy_home / ".ssh").mkdir()
    monkeypatch.setenv("AGY_HOME", str(fake_agy_home))

    run_dir, manifest = prepare(
        tmp_path,
        monkeypatch,
        alice_harness="AGY",
        alice_model="gemini-3.1-pro-high",
        alice_effort="high",
        bob_harness="OpenCode",
        bob_model="opencode/claude-sonnet-4-6",
        bob_effort="high",
        charlie_harness="antigravity",
        charlie_model="gemini-3.1-pro-high",
        charlie_effort="medium",
    )
    assert manifest["harnesses"] == {"bob": "opencode", "charlie": "antigravity"}
    assert manifest["providers"] == {"bob": "anthropic", "charlie": "google"}
    assert manifest["policy"]["role_policy"]["reviewer_harness_differs"] is True

    # AntiGravity Alice has an isolated home with linked CLI auth, skill, and all 10 tools.
    alice_home = run_dir / "configs" / "alice-agy"
    assert (alice_home / ".gemini" / "antigravity-cli" / "antigravity-oauth-token").exists()
    assert not (alice_home / ".ssh").exists()
    alice_skill = Path("skills") / "alice-orchestrator" / "SKILL.md"
    assert (alice_home / ".gemini" / "config" / alice_skill).exists()
    assert (run_dir / "alice-runtime" / ".agents" / alice_skill).exists()
    alice_mcp = json.loads(
        RUN_COMMON.agy_mcp_path(alice_home).read_text(encoding="utf-8")
    )["mcpServers"]["robomate"]
    assert alice_mcp["args"][-3:] == ["mcp", "--role", "orchestrator"]
    assert alice_mcp["env"]["ROBOMATE_HUB_URL"] == "http://127.0.0.1:8521"
    assert alice_mcp["env"]["ROBOMATE_TOKEN_FILE"] == str(tmp_path / "target/.robomate/token")
    initialize_database(tmp_path / "hub.db")
    served = {tool.name for tool in await create_mcp(HubStore(tmp_path / "hub.db")).list_tools()}
    assert set(alice_mcp["enabledTools"]) == served
    assert alice_mcp["timeoutSeconds"] == 330

    # OpenCode Bob uses the robomate worker bridge with a 330000ms timeout and external_directory.
    bob_oc = json.loads((run_dir / "configs" / "bob.opencode.json").read_text(encoding="utf-8"))
    assert bob_oc["model"] == "opencode/claude-sonnet-4-6"
    bob_mcp = bob_oc["mcp"]["robomate"]
    assert bob_mcp["command"][-3:] == ["mcp", "--role", "worker"]
    assert bob_mcp["timeout"] == 330000
    assert bob_mcp["environment"]["ROBOMATE_HUB_URL"] == "http://127.0.0.1:8521"
    assert bob_mcp["environment"]["ROBOMATE_TOKEN_FILE"] == str(
        tmp_path / "target/.robomate/token"
    )
    assert bob_mcp["environment"]["HUB_HARNESS"] == "opencode"
    assert bob_mcp["environment"]["HUB_HARNESS_VERSION"] == "1.18.32"
    assert bob_mcp["environment"]["HUB_PROVIDER"] == "anthropic"
    assert bob_oc["permission"]["external_directory"] == "allow"

    # AntiGravity Charlie has an isolated home and all 6 worker tools enabled on robomate.
    charlie_home = run_dir / "configs" / "charlie-agy"
    assert (charlie_home / ".gemini" / "antigravity-cli" / "antigravity-oauth-token").exists()
    charlie_mcp = json.loads(
        RUN_COMMON.agy_mcp_path(charlie_home).read_text(encoding="utf-8")
    )["mcpServers"]["robomate"]
    assert charlie_mcp["args"][-3:] == ["mcp", "--role", "worker"]
    assert set(charlie_mcp["enabledTools"]) == set(RUN_COMMON.TOOLS)
    assert charlie_mcp["env"]["HUB_HARNESS"] == "antigravity"
    assert charlie_mcp["env"]["HUB_HARNESS_VERSION"] == "1.2.7"
    assert charlie_mcp["env"]["HUB_PROVIDER"] == "google"

    alice_prompt = (run_dir / "alice.prompt.md").read_text(encoding="utf-8")
    assert "AntiGravity runtime note" in alice_prompt
    assert "Hub tools are the MCP server `robomate`." in alice_prompt
    report = json.loads(capsys.readouterr().out.split("\nStart scripts")[0])
    assert report["checks"]["codex_auth"] == {"codex": "no codex worker in this topology"}
    assert report["checks"]["agy_auth"] == {
        "alice": "alice-agy: logged in (antigravity-oauth-token)",
        "charlie": "charlie-agy: logged in (antigravity-oauth-token)",
    }
    assert "provider" not in report["checks"]


def test_unknown_worker_provider_surfaces_in_preflight_checks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("AGY_HOME", str(tmp_path / "no-agy-login"))
    _, manifest = prepare(
        tmp_path,
        monkeypatch,
        bob_harness="opencode",
        bob_model="opencode/big-pickle",
        charlie_harness="antigravity",
    )
    assert manifest["providers"] == {"bob": "unknown", "charlie": "unknown"}
    report = json.loads(capsys.readouterr().out.split("\nStart scripts")[0])
    assert report["checks"]["provider"] == {
        "bob": "unknown: pass --bob-model or --bob-provider to record the model maker",
        "charlie": "unknown: pass --charlie-model or --charlie-provider to record the model maker",
    }

    source = tmp_path / "origin"
    token = tmp_path / "token"
    token.write_text("secret\n")
    token.chmod(0o600)
    worker_run = (tmp_path / "worker-run").resolve()
    PREPARE_RUN.prepare_worker(
        "bob", str(source), worker_run, "http://192.0.2.10:8420", token, "opencode"
    )
    worker_report = json.loads(capsys.readouterr().out.split("\n\n")[0])
    assert worker_report["checks"]["provider"] == {
        "bob": "unknown: pass --bob-model or --bob-provider to record the model maker"
    }


def test_opencode_effort_with_no_auto_start_is_rejected_before_run_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    with pytest.raises(ValueError, match="opencode only accepts --variant"):
        prepare(
            tmp_path,
            monkeypatch,
            bob_harness="opencode",
            bob_effort="high",
            auto_start=False,
        )
    assert not (tmp_path / "run").exists()
    token = tmp_path / "token"
    token.write_text("secret\n")
    token.chmod(0o600)
    worker_run = tmp_path / "worker-run"
    with pytest.raises(ValueError, match="opencode only accepts --variant"):
        PREPARE_RUN.prepare_worker(
            "bob",
            "o/r",
            worker_run,
            "http://192.0.2.10:8420",
            token,
            "opencode",
            effort="high",
            auto_start=False,
        )
    assert not worker_run.exists()


def test_start_scripts_set_per_agent_temp_dir_and_create_directories(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    host = os.name
    # 1. Local start scripts: bash on POSIX, PowerShell on a Windows host
    run_dir, manifest = prepare(tmp_path, monkeypatch)
    tmp_parent = run_dir / "tmp"
    assert tmp_parent.is_dir()
    if os.name != "nt":
        assert tmp_parent.stat().st_mode & 0o777 == 0o700

    for name in ("alice", "bob", "charlie"):
        agent_tmp = tmp_parent / name
        assert agent_tmp.is_dir()
        if os.name != "nt":
            assert agent_tmp.stat().st_mode & 0o777 == 0o700
        script = (run_dir / f"start-{name}.{SCRIPT_SUFFIX}").read_text()
        lines = [
            line.strip()
            for line in script.splitlines()
            if line.strip() and not line.startswith("#")
        ]
        assert lines[1].startswith("cd ")
        if host == "nt":
            tmp_quoted = PREPARE_RUN.shell_word(str(agent_tmp), powershell=True)
            assert lines[0] == '$ErrorActionPreference = "Stop"'
            assert lines[2:4] == [f"$env:TEMP = {tmp_quoted}", f"$env:TMP = {tmp_quoted}"]
        else:
            assert lines[0] == "set -e"
            assert lines[2] == f"export TMPDIR={agent_tmp}"
        assert len(lines) >= 4

    # 2. Local PowerShell launch lines and start script
    monkeypatch.setattr(os, "name", "nt")
    for name, harness in (("alice", "claude-code"), ("bob", "claude-code"), ("charlie", "codex")):
        workdir = run_dir / name
        config = run_dir / "configs" / f"{name}.mcp.json"
        prompt = run_dir / f"{name}.prompt.md"
        agent_tmp = tmp_parent / name
        ps_lines = PREPARE_RUN.launch_lines(
            harness,
            workdir,
            config,
            prompt,
            None if name == "alice" else workdir / ".git",
            tmp_dir=agent_tmp,
        )
        script = PREPARE_RUN.start_script(ps_lines, powershell=True)
        lines = [
            line.strip()
            for line in script.splitlines()
            if line.strip() and not line.startswith("#")
        ]
        assert lines[0] == '$ErrorActionPreference = "Stop"'
        assert lines[1].startswith("cd ")
        tmp_quoted = PREPARE_RUN.shell_word(str(agent_tmp), powershell=True)
        assert lines[2] == f"$env:TEMP = {tmp_quoted}"
        assert lines[3] == f"$env:TMP = {tmp_quoted}"
        assert len(lines) >= 5

    # 3. Remote worker-only output on this host (Git Bash spellings on Windows)
    monkeypatch.setattr(os, "name", host)
    worker_run = tmp_path / "worker_run"
    token = tmp_path / "target/.robomate/token"
    PREPARE_RUN.prepare_worker(
        "bob",
        str(tmp_path / "origin"),
        worker_run,
        "http://192.0.2.10:8420",
        token,
        "claude-code",
    )
    bob_tmp = worker_run / "tmp" / "bob"
    assert bob_tmp.is_dir()
    if os.name != "nt":
        assert bob_tmp.stat().st_mode & 0o777 == 0o700
    script = (worker_run / "start-bob.sh").read_text()
    lines = [
        line.strip()
        for line in script.splitlines()
        if line.strip() and not line.startswith("#")
    ]
    assert lines[0] == "set -e"
    assert lines[1].startswith("cd ")
    bash_tmp = PREPARE_RUN.git_bash_path(bob_tmp) if host == "nt" else str(bob_tmp)
    assert lines[2] == f"export TMPDIR={bash_tmp}"

    # 4. Remote worker-only output on Windows host
    win_out = tmp_path / "win_bundle"
    root = PureWindowsPath("C:/work/robomate")
    run = PureWindowsPath("C:/Users/Bob/runs/step7")
    PREPARE_RUN.render_worker_bundle(
        "bob",
        "claude-code",
        "2.1.277",
        "anthropic",
        "",
        "",
        "C:/private/token",
        "http://172.26.115.68:8420",
        root,
        run,
        run / "bob",
        win_out,
    )
    win_bob_tmp = win_out / "tmp" / "bob"
    assert win_bob_tmp.is_dir()
    if os.name != "nt":
        assert win_bob_tmp.stat().st_mode & 0o777 == 0o700
    win_script = (win_out / "start-bob.sh").read_text()
    win_lines = [
        line.strip()
        for line in win_script.splitlines()
        if line.strip() and not line.startswith("#")
    ]
    assert win_lines[0] == "set -e"
    assert win_lines[1] == "cd /c/Users/Bob/runs/step7/bob"
    assert win_lines[2] == "export TMPDIR=/c/Users/Bob/runs/step7/tmp/bob"
    assert win_lines[3] == "export TEMP=C:/Users/Bob/runs/step7/tmp/bob"
    assert win_lines[4] == "export TMP=C:/Users/Bob/runs/step7/tmp/bob"


GL_ORIGIN = "git@gitlab-box.local:RoboNater/robomate-glab-sandbox.git"
GL_BASE_PROJECT: dict[str, Any] = {
    "id": 42,
    "default_branch": "main",
    "web_url": "https://gitlab-box.local/RoboNater/robomate-glab-sandbox",
    "permissions": {"project_access": {"access_level": 40}},
    "merge_method": "merge",
    "squash_option": "default_off",
    "auto_devops_enabled": False,
    "ci_config_path": None,
}


def gitlab_hub_repo(
    tmp_path: Path,
    origin_url: str = GL_ORIGIN,
    url: str = "http://127.0.0.1:8521",
) -> Path:
    repo = tmp_path / "gl-target"
    state = repo / ".robomate"
    state.mkdir(parents=True, exist_ok=True)
    token = state / "token"
    token.write_text("test-token\n")
    token.chmod(0o600)
    (state / "hub.json").write_text(
        json.dumps(
            {
                "pid": os.getpid(),
                "url": url,
                "port": PREPARE_RUN.url_port(url, "test"),
                "hub_id": "abc",
                "forge": "gitlab",
                "origin": origin_url,
            }
        )
    )
    return repo


def test_gitlab_requires_full_origin_url(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    target = gitlab_hub_repo(tmp_path)
    monkeypatch.setattr(PREPARE_RUN, "hub_healthy", lambda *_: True)
    monkeypatch.setattr(
        PREPARE_RUN,
        "probe_versions",
        lambda _: ({"claude": "2", "codex": "1"}, {"claude-code": "2", "codex": "1"}),
    )
    run_dir = tmp_path / "run"

    # Bare slug rejected
    with pytest.raises(ValueError, match="must be the full origin URL"):
        PREPARE_RUN.prepare("RoboNater/robomate-glab-sandbox", run_dir, hub_repo=target)

    # Local path rejected
    with pytest.raises(ValueError, match="must be the full origin URL"):
        PREPARE_RUN.prepare(str(tmp_path / "local"), run_dir, hub_repo=target)


def test_gitlab_rejects_origin_differing_from_hub(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = gitlab_hub_repo(tmp_path)
    monkeypatch.setattr(PREPARE_RUN, "hub_healthy", lambda *_: True)
    monkeypatch.setattr(
        PREPARE_RUN,
        "probe_versions",
        lambda _: ({"claude": "2", "codex": "1"}, {"claude-code": "2", "codex": "1"}),
    )
    run_dir = tmp_path / "run"
    with pytest.raises(ValueError, match="differs from the running hub origin"):
        PREPARE_RUN.prepare(
            "git@gitlab-box.local:OtherGroup/other-project.git", run_dir, hub_repo=target
        )


def test_gitlab_slug_parsing() -> None:
    assert (
        RUN_COMMON.parse_github_slug(
            "git@gitlab.com:group/subgroup/project.git", forge="gitlab"
        )
        == "group/subgroup/project"
    )
    assert (
        RUN_COMMON.parse_github_slug(
            "https://gitlab-box.local/RoboNater/robomate-glab-sandbox.git",
            forge="gitlab",
        )
        == "RoboNater/robomate-glab-sandbox"
    )
    assert RUN_COMMON.parse_github_slug("/local/path", forge="gitlab") is None


def test_gitlab_preflight_checks(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    target = gitlab_hub_repo(tmp_path)
    monkeypatch.setattr(PREPARE_RUN, "hub_healthy", lambda *_: True)
    monkeypatch.setattr(
        PREPARE_RUN,
        "probe_versions",
        lambda _: ({"claude": "2", "codex": "1"}, {"claude-code": "2", "codex": "1"}),
    )

    calls: list[list[str]] = []

    def make_runner(
        user_data: dict[str, Any] | None = None,
        project_data: dict[str, Any] | None = None,
        ci_found: bool = True,
        ci_error: Exception | None = None,
    ) -> Callable[..., str]:
        def runner(*args: str, **kwargs: Any) -> str:
            calls.append(list(args))
            assert args[0] != "gh", f"gh command unexpectedly invoked: {args}"
            assert args[0] == "glab"
            if list(args[1:4]) == ["api", "--hostname", "gitlab-box.local"]:
                endpoint = args[4]
                if endpoint == "user":
                    if user_data is not None:
                        return json.dumps(user_data)
                    return json.dumps({"username": "testuser"})
                if endpoint.startswith(
                    "projects/RoboNater%2Frobomate-glab-sandbox/repository/files"
                ):
                    if ci_error is not None:
                        raise ci_error
                    if not ci_found:
                        raise subprocess.CalledProcessError(
                            1, list(args), output="404 File Not Found", stderr="(HTTP 404)"
                        )
                    return json.dumps({"file_name": ".gitlab-ci.yml"})
                if endpoint == "projects/RoboNater%2Frobomate-glab-sandbox":
                    if project_data is not None:
                        return json.dumps(project_data)
                    return json.dumps(GL_BASE_PROJECT)
            raise AssertionError(f"unexpected call: {args}")

        return runner

    # 1. Auth failure
    monkeypatch.setattr(
        PREPARE_RUN, "run", make_runner(user_data={"message": "401 Unauthorized"})
    )
    with pytest.raises(ValueError, match="glab is not authenticated"):
        PREPARE_RUN.prepare(GL_ORIGIN, tmp_path / "run1", hub_repo=target)

    # 2. Project unreadable
    monkeypatch.setattr(
        PREPARE_RUN, "run", make_runner(project_data={"message": "404 Project Not Found"})
    )
    with pytest.raises(ValueError, match="cannot read project settings"):
        PREPARE_RUN.prepare(GL_ORIGIN, tmp_path / "run2", hub_repo=target)

    # 3. Web URL mismatch
    bad_web = {**GL_BASE_PROJECT, "web_url": "https://other-box.local/RoboNater/robomate"}
    monkeypatch.setattr(PREPARE_RUN, "run", make_runner(project_data=bad_web))
    with pytest.raises(ValueError, match="differs from expected"):
        PREPARE_RUN.prepare(GL_ORIGIN, tmp_path / "run3", hub_repo=target)

    # 4. Developer access missing (access_level 20 < 30)
    low_perm = {
        **GL_BASE_PROJECT,
        "permissions": {"project_access": {"access_level": 20}},
    }
    monkeypatch.setattr(PREPARE_RUN, "run", make_runner(project_data=low_perm))
    with pytest.raises(ValueError, match="requires developer access"):
        PREPARE_RUN.prepare(GL_ORIGIN, tmp_path / "run4", hub_repo=target)

    # 5. Incompatible merge method
    incompatible = {
        **GL_BASE_PROJECT,
        "merge_method": "merge",
        "squash_option": "always",
    }
    monkeypatch.setattr(PREPARE_RUN, "run", make_runner(project_data=incompatible))
    with pytest.raises(ValueError, match="does not allow the 'merge' merge method"):
        PREPARE_RUN.prepare(
            GL_ORIGIN, tmp_path / "run5", hub_repo=target, merge_method="merge"
        )

    # 6. CI presence - no CI file, auto_devops false -> allow_no_ci=True
    monkeypatch.setattr(
        PREPARE_RUN,
        "bootstrap_clone",
        lambda name, path, clone_from: {
            "agent": name,
            "path": str(path),
            "repository": clone_from,
            "workspace_id": f"w-{name}",
        },
    )
    monkeypatch.setattr(PREPARE_RUN, "run", make_runner(ci_found=False))
    run_dir6 = tmp_path / "run6"
    manifest6 = PREPARE_RUN.prepare(
        GL_ORIGIN, run_dir6, hub_repo=target, issue=12, account="testuser"
    )
    assert manifest6["policy"]["allow_no_ci"] is True
    assert manifest6["forge"] == "gitlab"

    # 6b. CI presence - network/5xx error refuses instead of failing open
    monkeypatch.setattr(
        PREPARE_RUN,
        "run",
        make_runner(
            ci_error=subprocess.CalledProcessError(
                1, ["glab"], output="500 Internal Server Error", stderr="500"
            )
        ),
    )
    with pytest.raises(ValueError, match="cannot check CI configuration"):
        PREPARE_RUN.prepare(GL_ORIGIN, tmp_path / "run6b", hub_repo=target)

    # 7. Happy path with CI present -> allow_no_ci=False
    calls.clear()
    monkeypatch.setattr(PREPARE_RUN, "run", make_runner(ci_found=True))
    run_dir7 = tmp_path / "run7"
    manifest7 = PREPARE_RUN.prepare(
        GL_ORIGIN, run_dir7, hub_repo=target, issue=12, account="testuser"
    )
    assert manifest7["policy"]["allow_no_ci"] is False
    assert manifest7["forge"] == "gitlab"
    assert all(c[0] != "gh" for c in calls)

    alice_prompt = (run_dir7 / "alice.prompt.md").read_text(encoding="utf-8")
    assert (
        "Forge: gitlab (host: gitlab-box.local, project: RoboNater/robomate-glab-sandbox)."
        in alice_prompt
    )
    assert "Forge comment identity account: `testuser`." in alice_prompt
    assert "merge its merge request" in alice_prompt
