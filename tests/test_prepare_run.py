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
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("prepare_run", ROOT / "scripts/prepare-run.py")
assert SPEC and SPEC.loader
PREPARE_RUN = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(PREPARE_RUN)
RUN_COMMON = sys.modules["run_common"]


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


def prepare(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, **kwargs: Any
) -> tuple[Path, dict[str, Any]]:
    source = origin(tmp_path)
    target = hub_repo(tmp_path, kwargs.pop("test_hub_url", "http://127.0.0.1:8521"))
    monkeypatch.setattr(PREPARE_RUN, "probe_versions",
                        lambda _: ({"claude": "2.1", "codex": "0.1"},
                                   {"claude-code": "2.1", "codex": "0.1"}))
    monkeypatch.setattr(PREPARE_RUN, "hub_healthy", lambda *_: True)
    monkeypatch.setattr(PREPARE_RUN, "codex_login_status", lambda _: "logged in")
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
    assert "--skip-git-repo-check" not in expected


def test_launch_lines_omit_unset_model_and_effort(tmp_path: Path) -> None:
    run_dir = (tmp_path / "run").resolve()
    alice = PREPARE_RUN.launch_lines(
        "codex", run_dir / "alice-runtime", run_dir / "configs" / "alice-codex",
        run_dir / "alice.prompt.md", None,
    )
    assert "--add-dir" not in alice[1] and "--model" not in alice[1] and " -c " not in alice[1]
    # Alice's runtime is not a clone, so Codex must skip its git-repo check (#45).
    assert "-C . --skip-git-repo-check --approve-for-me" in alice[1]
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


def test_work_file_becomes_the_goal_and_manifest_entry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    statement = "Address `acme/app#7` and `acme/app#9` in one pull request.\n"
    work_file = tmp_path / "sow.md"
    work_file.write_text(statement)
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
        script = (run_dir / f"start-{name}.sh").read_text()
        assert "prompt.md" not in script and " -p " not in script and "exec" not in script
    alice_script = (run_dir / "start-alice.sh").read_text()
    assert "codex -C . --approve-for-me" in alice_script
    assert "--skip-git-repo-check" not in alice_script


def test_codex_alice_exec_uses_skip_git_repo_check(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_dir, _ = prepare(tmp_path, monkeypatch, alice_harness="codex",
                         alice_model="gpt-6-luna", alice_effort="xhigh")
    script = (run_dir / "start-alice.sh").read_text()
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
    with pytest.raises(SystemExit, match="expected claude or codex"):
        PREPARE_RUN.main()
    assert not (tmp_path / "run").exists()


def test_remote_worker_config_carries_windows_paths_and_a_private_token(
    tmp_path: Path,
) -> None:
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
    assert written.stat().st_mode & 0o077 == 0


def test_worker_only_rejects_loose_missing_token_or_loopback_url(
    tmp_path: Path,
) -> None:
    run_dir = tmp_path / "worker-run"
    token = tmp_path / "token"
    token.write_text("secret\n")
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
    assert not (run_dir / "start-charlie.sh").exists()
    assert (run_dir / "start-bob.sh").exists()


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
