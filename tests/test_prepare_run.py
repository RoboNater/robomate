"""Prepare a whole user run directory in one command (#76)."""

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


def make_origin(tmp_path: Path) -> Path:
    origin = (tmp_path / "origin").resolve()
    subprocess.run(
        ["git", "init", "--initial-branch=main", str(origin)], check=True, capture_output=True
    )
    subprocess.run(
        [
            "git",
            "-C",
            str(origin),
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.com",
            "commit",
            "--allow-empty",
            "-m",
            "baseline",
        ],
        check=True,
        capture_output=True,
    )
    return origin


def fake_runner(
    monkeypatch: pytest.MonkeyPatch, *, gh_auth_ok: bool = True, git_protocol: str = "https"
) -> Any:
    original_run = PREPARE_RUN.run
    original_output = PREPARE_RUN.run_output

    def runner(*args: Any, **kwargs: Any) -> str:
        if args[:2] == ("claude", "--version"):
            return "2.1.277 (Claude Code)"
        if args[:2] == ("codex", "--version"):
            return "codex-cli 0.154.0"
        if args[:3] == ("gh", "auth", "status"):
            if not gh_auth_ok:
                raise subprocess.CalledProcessError(1, list(args))
            return ""
        if args[:3] == ("gh", "config", "get"):
            return git_protocol
        if args[:3] == ("gh", "repo", "view"):
            return json.dumps(
                {
                    "viewerPermission": "WRITE",
                    "squashMergeAllowed": True,
                    "mergeCommitAllowed": False,
                    "rebaseMergeAllowed": False,
                }
            )
        if args[:2] == ("gh", "api"):
            return json.dumps({"workflows": []})
        return str(original_run(*args, **kwargs))

    def output_runner(*args: Any, **kwargs: Any) -> tuple[str, str]:
        if args[:3] == ("codex", "login", "status"):
            # Codex CLI reports its status on stderr with exit 0 on Windows.
            return ("", "Logged in using ChatGPT")
        result = original_output(*args, **kwargs)
        return (str(result[0]), str(result[1]))

    monkeypatch.setattr(PREPARE_RUN, "run", runner)
    monkeypatch.setattr(PREPARE_RUN, "run_output", output_runner)
    # slug_clone_url resolves run() from run_common's namespace, not this module's.
    monkeypatch.setattr(RUN_COMMON, "run", runner)
    return runner


def printed_report(capsys: pytest.CaptureFixture[str]) -> dict[str, Any]:
    out = capsys.readouterr().out
    report = json.loads(out.split("\nStart scripts")[0])
    assert isinstance(report, dict)
    return report


def test_fresh_run_produces_every_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    origin = make_origin(tmp_path)
    fake_runner(monkeypatch)
    run_dir = (tmp_path / "run").resolve()
    manifest = PREPARE_RUN.prepare(
        str(origin), run_dir, issue=42, account="testuser", allow_no_ci="auto"
    )
    assert (
        manifest["workspaces"]["bob"]["workspace_id"]
        != manifest["workspaces"]["charlie"]["workspace_id"]
    )
    assert manifest["clone_repository"] == str(origin)
    assert manifest["policy"]["allow_no_ci"] is True  # local repo has no workflows
    assert manifest["policy"]["merge_method"] == "squash"

    configs = run_dir / "configs"
    bob_config = json.loads((configs / "bob.mcp.json").read_text(encoding="utf-8"))
    assert bob_config["mcpServers"]["hub"]["env"]["AGENT_NAME"] == "bob"
    assert bob_config["mcpServers"]["hub"]["env"]["HUB_HARNESS"] == "claude-code"
    assert bob_config["mcpServers"]["hub"]["env"]["HUB_HARNESS_VERSION"] == "2.1.277"
    assert bob_config["mcpServers"]["hub"]["env"]["HUB_PROVIDER"] == "anthropic"
    assert (
        bob_config["mcpServers"]["hub"]["env"]["HUB_WORKSPACE"]
        == manifest["workspaces"]["bob"]["path"]
    )
    codex_config = tomllib.loads((configs / "codex" / "config.toml").read_text(encoding="utf-8"))
    assert set(codex_config["mcp_servers"]) == {"hub"}
    assert codex_config["sandbox_mode"] == "workspace-write"
    assert codex_config["sandbox_workspace_write"]["network_access"] is True
    assert set(codex_config["mcp_servers"]["hub"]["enabled_tools"]) == {
        "check_in",
        "get_role_guide",
        "await_assignment",
        "report_progress",
        "ask_alice",
        "submit_result",
    }
    assert codex_config["mcp_servers"]["hub"]["env"]["AGENT_NAME"] == "charlie"
    assert codex_config["mcp_servers"]["hub"]["env"]["HUB_HARNESS_VERSION"] == "0.154.0"

    for name in ("bob", "charlie"):
        prompt = (run_dir / f"{name}.prompt.md").read_text(encoding="utf-8")
        assert "$AGENT_NAME" not in prompt
        assert name in prompt
    alice_prompt = (run_dir / "alice.prompt.md").read_text(encoding="utf-8")
    assert "42" in alice_prompt and "testuser" in alice_prompt
    assert "<account>" not in alice_prompt

    alice_config = json.loads((configs / "alice.mcp.json").read_text(encoding="utf-8"))
    assert alice_config["mcpServers"]["hub"]["env"]["HUB_STATE_DIR"] == str(
        run_dir / "hub-state"
    )

    # Alice's orchestrator skill is linked run-locally, next to the clones.
    skill = run_dir / "alice-runtime" / ".claude" / "skills" / "alice-orchestrator"
    assert skill.exists()

    # The stderr-form login status survives the report.
    report = printed_report(capsys)
    assert report["checks"]["codex_auth"] == {"charlie": "codex: Logged in using ChatGPT"}


def test_rerun_is_idempotent_and_preserves_clone_token_identity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    origin = make_origin(tmp_path)
    fake_runner(monkeypatch)
    run_dir = (tmp_path / "run").resolve()
    first = PREPARE_RUN.prepare(str(origin), run_dir)
    token = (run_dir / "hub-state" / "token").read_text(encoding="utf-8")
    bob_identity = (
        run_dir / "bob" / ".git" / "robo-agents-workspace.json"
    ).read_text(encoding="utf-8")
    charlie_identity = (
        run_dir / "charlie" / ".git" / "robo-agents-workspace.json"
    ).read_text(encoding="utf-8")
    marker = run_dir / "bob" / "uncommitted-marker"
    marker.write_text("preserve")
    # Rerun on a dirty clone must fail without touching anything.
    with pytest.raises(ValueError, match="dirty"):
        PREPARE_RUN.prepare(str(origin), run_dir)
    assert (run_dir / "hub-state" / "token").read_text(encoding="utf-8") == token
    marker.unlink()
    second = PREPARE_RUN.prepare(str(origin), run_dir)
    assert second["workspaces"] == first["workspaces"]
    assert (run_dir / "hub-state" / "token").read_text(encoding="utf-8") == token
    assert (
        run_dir / "bob" / ".git" / "robo-agents-workspace.json"
    ).read_text(encoding="utf-8") == bob_identity
    assert (
        (run_dir / "charlie" / ".git" / "robo-agents-workspace.json").read_text(
            encoding="utf-8"
        )
        == charlie_identity
    )


def test_fails_when_gh_unauthenticated(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fake_runner(monkeypatch, gh_auth_ok=False)
    with pytest.raises(ValueError, match="authenticated"):
        PREPARE_RUN.prepare("test-org/test-repo", (tmp_path / "run").resolve())


def test_fails_when_runtime_missing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fake_runner(monkeypatch)
    original = PREPARE_RUN.run

    def runner(*args: Any, **kwargs: Any) -> str:
        if args[:2] == ("codex", "--version"):
            raise OSError("no such file")
        return str(original(*args, **kwargs))

    monkeypatch.setattr(PREPARE_RUN, "run", runner)
    with pytest.raises(ValueError, match="requires the 'codex' CLI on PATH"):
        PREPARE_RUN.prepare(
            "test-org/test-repo",
            (tmp_path / "run").resolve(),
            skip_github_checks=True,
        )


def test_no_token_or_clone_path_inside_clones(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    origin = make_origin(tmp_path)
    fake_runner(monkeypatch)
    run_dir = (tmp_path / "run").resolve()
    manifest = PREPARE_RUN.prepare(str(origin), run_dir)
    token = (run_dir / "hub-state" / "token").read_text(encoding="utf-8").strip()
    bob_path = manifest["workspaces"]["bob"]["path"]
    charlie_path = manifest["workspaces"]["charlie"]["path"]
    for clone, other in ((bob_path, charlie_path), (charlie_path, bob_path)):
        for path in Path(clone).rglob("*"):
            if not path.is_file() or ".git" in path.parts:
                continue
            text = path.read_text(encoding="utf-8", errors="replace")
            assert token not in text, f"token leaked into {path}"
            assert other not in text, f"other clone path leaked into {path}"
        assert not (Path(clone) / "bob.mcp.json").exists()
        assert not (Path(clone) / "charlie.prompt.md").exists()
        assert not (Path(clone) / "token").exists()


def test_same_harness_pair_renders_both_claude_configs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    origin = make_origin(tmp_path)
    fake_runner(monkeypatch)
    run_dir = (tmp_path / "run").resolve()
    manifest = PREPARE_RUN.prepare(
        str(origin),
        run_dir,
        bob_harness="claude-code",
        charlie_harness="claude-code",
        bob_model="claude-sonnet-5",
        charlie_model="claude-sonnet-5",
    )
    assert manifest["policy"]["role_policy"]["reviewer_harness_differs"] is False
    assert (run_dir / "configs" / "charlie.mcp.json").exists()
    with pytest.raises(ValueError, match="not yet supported"):
        PREPARE_RUN.prepare(
            str(origin),
            (tmp_path / "other").resolve(),
            charlie_harness="opencode",
            skip_github_checks=True,
        )


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


def test_bare_slug_expands_via_gh_protocol(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake_runner(monkeypatch)
    seen: list[tuple[str, str]] = []
    ids = {"bob": "a" * 64, "charlie": "b" * 64}

    def fake_bootstrap(agent: str, destination: Path, repository: str) -> dict[str, str]:
        seen.append((agent, repository))
        return {"path": str(destination), "workspace_id": ids[agent]}

    monkeypatch.setattr(PREPARE_RUN, "bootstrap_clone", fake_bootstrap)
    run_dir = (tmp_path / "run").resolve()
    manifest = PREPARE_RUN.prepare(
        "test-org/test-repo", run_dir, skip_github_checks=True
    )
    assert manifest["clone_repository"] == "https://github.com/test-org/test-repo.git"
    assert seen == [
        ("bob", "https://github.com/test-org/test-repo.git"),
        ("charlie", "https://github.com/test-org/test-repo.git"),
    ]
    ssh_dir = (tmp_path / "ssh-run").resolve()
    fake_runner(monkeypatch, git_protocol="ssh")
    ssh_manifest = PREPARE_RUN.prepare(
        "test-org/test-repo", ssh_dir, skip_github_checks=True
    )
    assert ssh_manifest["clone_repository"] == "git@github.com:test-org/test-repo.git"


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


@pytest.mark.parametrize(
    "protocol,expected",
    [
        ("https", "https://github.com/test-org/test-repo.git"),
        ("ssh", "git@github.com:test-org/test-repo.git"),
        ("", "https://github.com/test-org/test-repo.git"),
    ],
)
def test_slug_clone_url_honors_gh_protocol(
    monkeypatch: pytest.MonkeyPatch, protocol: str, expected: str
) -> None:
    fake_runner(monkeypatch, git_protocol=protocol)
    assert RUN_COMMON.slug_clone_url("test-org/test-repo") == expected
    assert PREPARE_RUN.clone_source("test-org/test-repo", "test-org/test-repo") == expected


def test_slug_clone_url_falls_back_without_gh(monkeypatch: pytest.MonkeyPatch) -> None:
    def runner(*args: Any, **kwargs: Any) -> str:
        raise OSError("no gh on PATH")

    monkeypatch.setattr(RUN_COMMON, "run", runner)
    assert RUN_COMMON.slug_clone_url("test-org/test-repo") == (
        "https://github.com/test-org/test-repo.git"
    )


def test_codex_login_status_skips_warning_preamble(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        PREPARE_RUN,
        "run_output",
        lambda *args, **kwargs: ("", "WARNING: proceeding anyway\nLogged in using ChatGPT\n"),
    )
    assert PREPARE_RUN.codex_login_status(Path("/tmp/home")) == "Logged in using ChatGPT"


def test_bootstrap_failure_message_keeps_tail_without_traceback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
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


def test_failed_bootstrap_leaves_no_token(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake_runner(monkeypatch)

    def failing_bootstrap(agent: str, destination: Path, repository: str) -> dict[str, str]:
        raise ValueError("boom")

    monkeypatch.setattr(PREPARE_RUN, "bootstrap_clone", failing_bootstrap)
    run_dir = (tmp_path / "run").resolve()
    with pytest.raises(ValueError, match="boom"):
        PREPARE_RUN.prepare(
            str((tmp_path / "origin").resolve()), run_dir, skip_github_checks=True
        )
    assert not (run_dir / "hub-state" / "token").exists()


def test_both_codex_workers_report_each_login(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    origin = make_origin(tmp_path)
    fake_runner(monkeypatch)
    run_dir = (tmp_path / "run").resolve()
    PREPARE_RUN.prepare(
        str(origin), run_dir, bob_harness="codex", charlie_harness="codex"
    )
    report = printed_report(capsys)
    assert set(report["checks"]["codex_auth"]) == {"bob", "charlie"}


def test_main_reports_actionable_error_without_traceback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "prepare-run.py",
            "--repository",
            "test-org/test-repo",
            "--run-dir",
            str((tmp_path / "run").resolve()),
            "--bob",
            "nosuch",
        ],
    )
    with pytest.raises(SystemExit) as exc:
        PREPARE_RUN.main()
    assert "prepare-run: error:" in str(exc.value.code)
    assert "not yet supported" in str(exc.value.code)


STATEMENT = """# Land acme/app#7 and acme/app#9 together

Address `acme/app#7` and `acme/app#9` in one pull request.

Acceptance criteria:
- the parser accepts both forms
"""


def durable_goal(run_dir: Path) -> str:
    prompt = (run_dir / "alice.prompt.md").read_text(encoding="utf-8")
    return prompt.split("Goal: ", 1)[1].split("GitHub comment identity account:", 1)[0]


def test_work_file_becomes_the_goal_and_manifest_entry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    origin = make_origin(tmp_path)
    fake_runner(monkeypatch)
    work_file = tmp_path / "sow.md"
    work_file.write_text(STATEMENT, encoding="utf-8")
    run_dir = (tmp_path / "run").resolve()
    manifest = PREPARE_RUN.prepare(
        str(origin), run_dir, account="testuser", work_file=work_file
    )

    goal = durable_goal(run_dir)
    assert goal.startswith(STATEMENT.strip())
    assert "no roadmap edit" in goal
    assert "Address issue" not in goal
    assert "<issue" not in goal
    assert manifest["issue"] is None
    assert manifest["work"] == {
        "goal": goal.strip(),
        "path": str(work_file.resolve()),
        "sha256": hashlib.sha256(STATEMENT.encode("utf-8")).hexdigest(),
        "roadmap": None,
    }
    saved = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
    assert saved["work"] == manifest["work"]


ISSUE_GOAL_NO_ROADMAP = (
    "Address issue `{repo}#42`, merge its pull request, and close out with no roadmap "
    "edit; record the merge only in the workflow summary."
)


ISSUE_GOAL = (
    "Address issue `{repo}#42`, merge its pull request, and close out. The implementer bob "
    "should make a decision on what roadmap ({roadmap}) updates are necessary, if any, when "
    "they open the PR and include it as a PR comment so it can be reviewed. After the merge "
    "the implementer bob should update the roadmap per the adjudicated PR if necessary. "
    "Make sure you include that in bob's initial tasking."
)


def test_issue_goal_defaults_to_no_roadmap_edit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    origin = make_origin(tmp_path)
    fake_runner(monkeypatch)
    run_dir = (tmp_path / "run").resolve()
    manifest = PREPARE_RUN.prepare(str(origin), run_dir, issue=42, account="testuser")
    expected = ISSUE_GOAL_NO_ROADMAP.format(repo=origin)
    assert durable_goal(run_dir) == expected + "\n\n"
    assert manifest["issue"] == 42
    assert manifest["work"] == {
        "goal": expected,
        "path": None,
        "sha256": None,
        "roadmap": None,
    }
    assert PREPARE_RUN.render_goal(
        "RoboNater/robomate", "git@github.com:RoboNater/robomate.git", 42, None
    ) == ISSUE_GOAL_NO_ROADMAP.format(repo="RoboNater/robomate")


@pytest.mark.parametrize("roadmap", ["2", "#2"])
def test_issue_goal_renders_bare_roadmap_numbers_against_the_target(roadmap: str) -> None:
    goal = PREPARE_RUN.render_goal(
        "o/r", "git@github.com:o/r.git", 42, None, roadmap
    )
    assert goal == ISSUE_GOAL.format(repo="o/r", roadmap="o/r#2")


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


def test_prepare_records_the_rendered_roadmap_in_run_json(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    origin = make_origin(tmp_path)
    fake_runner(monkeypatch)
    run_dir = (tmp_path / "run").resolve()
    manifest = PREPARE_RUN.prepare(
        str(origin), run_dir, issue=42, account="testuser", roadmap="2"
    )
    assert manifest["work"]["roadmap"] == f"{origin}#2"
    assert f"({origin}#2)" in manifest["work"]["goal"]
    saved = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
    assert saved["work"] == manifest["work"]


def test_work_file_with_roadmap_appends_bob_instructions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    origin = make_origin(tmp_path)
    fake_runner(monkeypatch)
    work_file = tmp_path / "sow.md"
    work_file.write_text(STATEMENT, encoding="utf-8")
    run_dir = (tmp_path / "run").resolve()
    manifest = PREPARE_RUN.prepare(
        str(origin), run_dir, account="testuser", work_file=work_file, roadmap="5"
    )
    goal = durable_goal(run_dir)
    assert goal.startswith(STATEMENT.strip())
    assert "no roadmap edit" not in goal
    assert f"roadmap ({origin}#5)" in goal
    assert "should make a decision on what roadmap" in goal
    assert manifest["work"]["roadmap"] == f"{origin}#5"


def run_main(monkeypatch: pytest.MonkeyPatch, run_dir: Path, *extra: str) -> str:
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "prepare-run.py",
            "--repository",
            "test-org/test-repo",
            "--run-dir",
            str(run_dir),
            *extra,
        ],
    )
    with pytest.raises(SystemExit) as exc:
        PREPARE_RUN.main()
    return str(exc.value.code)


def test_work_file_with_issue_fails_actionably(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    work_file = tmp_path / "sow.md"
    work_file.write_text(STATEMENT, encoding="utf-8")
    run_dir = (tmp_path / "run").resolve()
    message = run_main(monkeypatch, run_dir, "--work-file", str(work_file), "--issue", "42")
    assert message.startswith("prepare-run: error:")
    assert "mutually exclusive" in message
    assert not run_dir.exists()


@pytest.mark.parametrize(
    "content,expected",
    [(None, "does not exist"), ("", "is empty"), ("  \n\t\n", "is empty")],
)
def test_missing_or_empty_work_file_fails_actionably(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, content: str | None, expected: str
) -> None:
    work_file = tmp_path / "sow.md"
    if content is not None:
        work_file.write_text(content, encoding="utf-8")
    run_dir = (tmp_path / "run").resolve()
    message = run_main(monkeypatch, run_dir, "--work-file", str(work_file))
    assert message.startswith("prepare-run: error:")
    assert expected in message
    assert not run_dir.exists()


GOLDEN = ROOT / "tests" / "fixtures" / "prepare-run-default"
#: Rendered outputs of a default (loopback) run, compared byte for byte (#125).
GOLDEN_FILES = {
    "alice.mcp.json": Path("configs/alice.mcp.json"),
    "bob.mcp.json": Path("configs/bob.mcp.json"),
    "codex.config.toml": Path("configs/codex/config.toml"),
    "run.json": Path("run.json"),
    "start-alice.sh": Path("start-alice.sh"),
    "start-bob.sh": Path("start-bob.sh"),
    "start-charlie.sh": Path("start-charlie.sh"),
}


def default_rendering(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> dict[str, str]:
    """A default run's rendered files and stdout, with run-specific values masked."""
    origin = make_origin(tmp_path)
    fake_runner(monkeypatch)
    run_dir = (tmp_path / "run").resolve()
    manifest = PREPARE_RUN.prepare(str(origin), run_dir, issue=42, account="testuser")
    token = (run_dir / "hub-state" / "token").read_text(encoding="utf-8").strip()
    masks = {
        str(run_dir): "$RUN_DIR",
        str(origin): "$ORIGIN",
        str(ROOT): "$ROOT",
        token: "$TOKEN",
        manifest["workspaces"]["bob"]["workspace_id"]: "$BOB_ID",
        manifest["workspaces"]["charlie"]["workspace_id"]: "$CHARLIE_ID",
    }
    rendered = {
        name: (run_dir / path).read_text(encoding="utf-8") for name, path in GOLDEN_FILES.items()
    }
    rendered["stdout.txt"] = capsys.readouterr().out
    for name, text in rendered.items():
        for value, mask in masks.items():
            text = text.replace(value, mask)
        rendered[name] = text
    return rendered


def test_default_rendering_matches_the_golden_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    rendered = default_rendering(tmp_path, monkeypatch, capsys)
    for name, text in rendered.items():
        assert text == (GOLDEN / name).read_text(encoding="utf-8"), name


WSL_URL = "http://172.26.115.68:8420"


def test_networked_run_renders_addresses_consistently(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    origin = make_origin(tmp_path)
    fake_runner(monkeypatch)
    run_dir = (tmp_path / "run").resolve()
    manifest = PREPARE_RUN.prepare(
        str(origin), run_dir, hub_host="0.0.0.0", hub_url=WSL_URL
    )
    configs = run_dir / "configs"
    hub_env = json.loads((configs / "alice.mcp.json").read_text(encoding="utf-8"))[
        "mcpServers"
    ]["hub"]["env"]
    assert hub_env["HUB_HOST"] == "0.0.0.0"
    assert hub_env["HUB_PUBLIC_URL"] == WSL_URL
    assert "HUB_PORT" not in hub_env  # 8420 is the hub's own default
    bob_env = json.loads((configs / "bob.mcp.json").read_text(encoding="utf-8"))[
        "mcpServers"
    ]["hub"]["env"]
    charlie_env = tomllib.loads((configs / "codex" / "config.toml").read_text(encoding="utf-8"))[
        "mcp_servers"
    ]["hub"]["env"]
    assert bob_env["HUB_URL"] == charlie_env["HUB_URL"] == WSL_URL
    assert manifest["network"] == {
        "hub_host": "0.0.0.0",
        "hub_port": 8420,
        "hub_url": WSL_URL,
        "public_url": WSL_URL,
        "remote_worker": None,
    }
    out = capsys.readouterr().out
    assert f"curl.exe -fsS {WSL_URL}/healthz" in out
    assert "changes whenever WSL restarts" in out
    report = json.loads(out.split("\nStart scripts")[0])
    assert report["network"] == manifest["network"]


def test_public_url_overrides_the_advertised_address_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    origin = make_origin(tmp_path)
    fake_runner(monkeypatch)
    run_dir = (tmp_path / "run").resolve()
    PREPARE_RUN.prepare(
        str(origin),
        run_dir,
        hub_host="0.0.0.0",
        hub_url=WSL_URL + "/",
        public_url="http://hub.example:8420",
    )
    configs = run_dir / "configs"
    hub_env = json.loads((configs / "alice.mcp.json").read_text(encoding="utf-8"))[
        "mcpServers"
    ]["hub"]["env"]
    assert hub_env["HUB_PUBLIC_URL"] == "http://hub.example:8420"
    bob_env = json.loads((configs / "bob.mcp.json").read_text(encoding="utf-8"))[
        "mcpServers"
    ]["hub"]["env"]
    assert bob_env["HUB_URL"] == WSL_URL


PORT_URL = "http://172.26.115.68:8521"


def rendered_envs(run_dir: Path) -> tuple[dict[str, str], dict[str, str], dict[str, str]]:
    """Alice's hub env and the bob and charlie worker envs of a default-topology run."""
    configs = run_dir / "configs"
    hub_env = json.loads((configs / "alice.mcp.json").read_text(encoding="utf-8"))[
        "mcpServers"
    ]["hub"]["env"]
    bob_env = json.loads((configs / "bob.mcp.json").read_text(encoding="utf-8"))[
        "mcpServers"
    ]["hub"]["env"]
    charlie_env = tomllib.loads((configs / "codex" / "config.toml").read_text(encoding="utf-8"))[
        "mcp_servers"
    ]["hub"]["env"]
    return hub_env, bob_env, charlie_env


def test_hub_url_port_becomes_the_bind_port(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    origin = make_origin(tmp_path)
    fake_runner(monkeypatch)
    run_dir = (tmp_path / "run").resolve()
    manifest = PREPARE_RUN.prepare(str(origin), run_dir, hub_host="0.0.0.0", hub_url=PORT_URL)
    hub_env, bob_env, charlie_env = rendered_envs(run_dir)
    assert hub_env["HUB_PORT"] == "8521"
    assert hub_env["HUB_PUBLIC_URL"] == bob_env["HUB_URL"] == charlie_env["HUB_URL"] == PORT_URL
    assert manifest["network"]["hub_port"] == 8521
    out = capsys.readouterr().out
    assert f"curl.exe -fsS {PORT_URL}/healthz" in out
    assert f"curl.exe -fsS {PORT_URL}/.well-known/agent-card.json" in out
    assert f"its url must be {PORT_URL}/a2a" in out
    assert ":8420" not in out
    assert "forward" not in out


def test_loopback_run_on_another_port_binds_that_port(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A second hub on the same machine, beside one that holds 8420.
    origin = make_origin(tmp_path)
    fake_runner(monkeypatch)
    run_dir = (tmp_path / "run").resolve()
    manifest = PREPARE_RUN.prepare(str(origin), run_dir, hub_url="http://127.0.0.1:8521")
    hub_env, bob_env, charlie_env = rendered_envs(run_dir)
    assert "HUB_HOST" not in hub_env
    assert hub_env["HUB_PORT"] == "8521"
    assert hub_env["HUB_PUBLIC_URL"] == bob_env["HUB_URL"] == charlie_env["HUB_URL"]
    assert bob_env["HUB_URL"] == "http://127.0.0.1:8521"
    assert manifest["network"]["hub_port"] == 8521


@pytest.mark.parametrize("hub_port,rendered", [(8600, "8600"), (8420, None)])
def test_explicit_hub_port_is_rendered_as_given(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    hub_port: int,
    rendered: str | None,
) -> None:
    origin = make_origin(tmp_path)
    fake_runner(monkeypatch)
    run_dir = (tmp_path / "run").resolve()
    manifest = PREPARE_RUN.prepare(
        str(origin), run_dir, hub_host="0.0.0.0", hub_url=PORT_URL, hub_port=hub_port
    )
    hub_env, bob_env, charlie_env = rendered_envs(run_dir)
    assert hub_env.get("HUB_PORT") == rendered
    # Workers still dial, and the card still advertises, the --hub-url port.
    assert hub_env["HUB_PUBLIC_URL"] == bob_env["HUB_URL"] == charlie_env["HUB_URL"] == PORT_URL
    assert manifest["network"]["hub_port"] == hub_port
    out = capsys.readouterr().out
    assert (
        f"The hub binds port {hub_port} but workers dial port 8521; "
        f"forward 8521 to {hub_port}"
    ) in out
    assert f"curl.exe -fsS {PORT_URL}/healthz" in out


def test_url_port_falls_back_to_the_scheme_default() -> None:
    assert PREPARE_RUN.url_port("http://hub.example", "--hub-url") == 80
    assert PREPARE_RUN.url_port("https://hub.example/", "--hub-url") == 443
    assert PREPARE_RUN.url_port("http://[::1]:8521", "--hub-url") == 8521


@pytest.mark.parametrize(
    "extra,expected",
    [
        (["--hub-port", "0"], "--hub-port must name a port between 1 and 65535"),
        (["--hub-port", "65536"], "--hub-port must name a port between 1 and 65535"),
        (["--hub-url", "http://127.0.0.1:0"], "--hub-url must name a port between 1 and 65535"),
        (["--hub-url", "http://127.0.0.1:70000"], "--hub-url has an invalid port"),
        (
            ["--hub-host", "0.0.0.0", "--hub-url", PORT_URL,
             "--public-url", "http://hub.example:99999"],
            "--public-url has an invalid port",
        ),
    ],
)
def test_out_of_range_ports_are_rejected_before_any_artifact(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, extra: list[str], expected: str
) -> None:
    fake_runner(monkeypatch)
    run_dir = (tmp_path / "run").resolve()
    message = run_main(monkeypatch, run_dir, "--skip-github-checks", *extra)
    assert message.startswith("prepare-run: error:")
    assert expected in message
    assert not run_dir.exists()


def test_non_integer_hub_port_is_a_usage_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_dir = (tmp_path / "run").resolve()
    assert run_main(monkeypatch, run_dir, "--hub-port", "http") == "2"
    assert not run_dir.exists()


@pytest.mark.parametrize(
    "kwargs,expected",
    [
        ({"hub_host": "0.0.0.0"}, "binds every interface"),
        ({"hub_host": "::"}, "binds every interface"),
        ({"hub_host": "[::]"}, "binds every interface"),
        ({"hub_host": "*"}, "binds every interface"),
        (
            {"hub_host": "0.0.0.0", "hub_url": WSL_URL, "public_url": "http://localhost:8420"},
            "binds every interface",
        ),
        (
            {"hub_host": "0.0.0.0", "hub_url": WSL_URL, "public_url": "http://0.0.0.0:8420"},
            "binds every interface",
        ),
        ({"hub_host": "[::1]", "hub_url": WSL_URL}, "only binds loopback"),
        ({"remote_worker": "bob"}, "cannot be loopback"),
        ({"hub_host": "0.0.0.0", "public_url": WSL_URL, "remote_worker": "bob"}, "cannot be"),
        ({"hub_url": WSL_URL}, "only binds loopback"),
        ({"hub_url": "172.26.115.68:8420"}, "http(s) URL"),
        (
            {"hub_host": "0.0.0.0", "hub_url": WSL_URL, "remote_worker": "bob",
             "bob_dir": Path("/elsewhere/bob")},
            "--worker-only bob",
        ),
    ],
)
def test_unreachable_network_topologies_are_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, kwargs: dict[str, Any], expected: str
) -> None:
    fake_runner(monkeypatch)
    run_dir = (tmp_path / "run").resolve()
    with pytest.raises(ValueError, match=expected.replace("(", r"\(").replace(")", r"\)")):
        PREPARE_RUN.prepare("test-org/test-repo", run_dir, skip_github_checks=True, **kwargs)
    assert not run_dir.exists()


def test_remote_worker_is_left_to_its_host(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    origin = make_origin(tmp_path)
    runner = fake_runner(monkeypatch)
    probed: list[tuple[Any, ...]] = []

    def recording(*args: Any, **kwargs: Any) -> str:
        probed.append(args)
        return str(runner(*args, **kwargs))

    monkeypatch.setattr(PREPARE_RUN, "run", recording)
    monkeypatch.delenv("WSL_DISTRO_NAME", raising=False)
    run_dir = (tmp_path / "run").resolve()
    manifest = PREPARE_RUN.prepare(
        str(origin),
        run_dir,
        hub_host="0.0.0.0",
        hub_url=WSL_URL,
        remote_worker="bob",
        bob_model="claude-opus-5-5",
    )
    # Bob's clone, config, prompt and harness version all belong to his host.
    assert not (run_dir / "bob").exists()
    assert not (run_dir / "configs" / "bob.mcp.json").exists()
    assert not (run_dir / "bob.prompt.md").exists()
    assert ("claude", "--version") not in probed
    assert set(manifest["workspaces"]) == {"charlie"}
    assert manifest["network"]["remote_worker"] == "bob"
    charlie_env = tomllib.loads(
        (run_dir / "configs" / "codex" / "config.toml").read_text(encoding="utf-8")
    )["mcp_servers"]["hub"]["env"]
    assert charlie_env["HUB_URL"] == WSL_URL
    out = capsys.readouterr().out
    assert "# bob runs on the worker host" in out
    command = next(line for line in out.splitlines() if "--worker-only bob" in line)
    assert f"--hub-url '{WSL_URL}'" in command
    assert "--hub-port" not in command
    assert f"--token-file '{run_dir / 'hub-state' / 'token'}'" in command
    assert "--bob-harness claude-code" in command
    assert "--bob-model 'claude-opus-5-5'" in command
    assert "--bob-effort" not in command and "--no-auto-start" not in command
    assert not (run_dir / "start-bob.sh").exists()
    token = (run_dir / "hub-state" / "token").read_text(encoding="utf-8").strip()
    assert token not in out


def test_remote_token_path_spells_the_wsl_share(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("WSL_DISTRO_NAME", "Ubuntu-24.04")
    assert PREPARE_RUN.remote_token_path(Path("/home/me/run/hub-state/token")) == (
        "\\\\wsl.localhost\\Ubuntu-24.04\\home\\me\\run\\hub-state\\token"
    )
    monkeypatch.delenv("WSL_DISTRO_NAME")
    assert PREPARE_RUN.remote_token_path(Path("/srv/run/token")) == "/srv/run/token"


WINDOWS_ROOT = PureWindowsPath("C:/work/robo-agents")
WINDOWS_RUN = PureWindowsPath("C:/Users/Bob/runs/step7")


def test_remote_worker_config_carries_windows_paths_and_a_private_token(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    out_dir = tmp_path / "bundle"
    bundle = PREPARE_RUN.render_worker_bundle(
        "bob",
        "claude-code",
        "2.1.277",
        "anthropic",
        "",
        "",
        "f" * 64,
        WSL_URL,
        WINDOWS_ROOT,
        WINDOWS_RUN,
        WINDOWS_RUN / "bob",
        out_dir,
    )
    written = out_dir / "configs" / "bob.mcp.json"
    text = written.read_text(encoding="utf-8")
    assert "\\\\" not in text  # forward slashes only (#75)
    hub = json.loads(text)["mcpServers"]["hub"]
    assert hub["args"] == ["run", "--locked", "--directory", "C:/work/robo-agents", "worker-mcp"]
    assert hub["env"]["HUB_URL"] == WSL_URL
    assert hub["env"]["HUB_WORKSPACE"] == "C:/Users/Bob/runs/step7/bob"
    assert hub["env"]["HUB_TELEMETRY_LOG"] == "C:/Users/Bob/runs/step7/bob-telemetry.jsonl"
    assert hub["env"]["HUB_TOKEN"] == "f" * 64
    assert written.stat().st_mode & 0o077 == 0
    assert bundle["config"] == "C:/Users/Bob/runs/step7/configs/bob.mcp.json"
    assert "$AGENT_NAME" not in (out_dir / "bob.prompt.md").read_text(encoding="utf-8")
    assert bundle["launch"] == [
        "cd /c/Users/Bob/runs/step7/bob",
        "claude --permission-mode auto --strict-mcp-config "
        "--mcp-config C:/Users/Bob/runs/step7/configs/bob.mcp.json "
        "--add-dir C:/Users/Bob/runs/step7 "
        "-p 'Read C:/Users/Bob/runs/step7/bob.prompt.md and follow the instructions in it'",
    ]
    assert bundle["script"] == "C:/Users/Bob/runs/step7/start-bob.sh"
    assert (out_dir / "start-bob.sh").read_text(encoding="utf-8") == (
        "#!/usr/bin/env bash\n# Generated by scripts/prepare-run.py\nset -e\n"
        + "\n".join(bundle["launch"])
        + "\n"
    )


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


@pytest.mark.parametrize("harness", ["claude-code", "codex"])
@pytest.mark.parametrize("probe_holds", [False, True])
def test_bundle_refuses_a_filesystem_that_ignores_chmod_leaving_no_token(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, harness: str, probe_holds: bool
) -> None:
    # A DrvFs mount without metadata (/mnt/c from WSL) accepts chmod and ignores
    # it. probe_holds=True lets only the permission probe keep its mode, so the
    # check after the real write is exercised too.
    real_chmod = os.chmod

    def chmod(path: Any, mode: int, *args: Any, **kwargs: Any) -> None:
        if probe_holds and "permission-probe" in str(path):
            real_chmod(path, mode, *args, **kwargs)

    monkeypatch.setattr(RUN_COMMON.os, "chmod", chmod)
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "no-codex-login"))
    out_dir = tmp_path / "bundle"
    token = "f" * 64
    previous = os.umask(0o022)
    try:
        with pytest.raises(ValueError, match="ignores POSIX permissions"):
            PREPARE_RUN.render_worker_bundle(
                "bob", harness, "2.1.277", "anthropic", "", "", token, WSL_URL,
                WINDOWS_ROOT, WINDOWS_RUN, WINDOWS_RUN / "bob", out_dir,
            )
    finally:
        os.umask(previous)
    files = [path for path in out_dir.rglob("*") if path.is_file()]
    assert all(token not in path.read_text(encoding="utf-8") for path in files), files
    assert not (out_dir / "configs" / "bob.mcp.json").exists()
    assert not (out_dir / "configs" / "bob-codex" / "config.toml").exists()


def hub_token_file(tmp_path: Path, mode: int = 0o600) -> tuple[Path, str]:
    state = tmp_path / "hub-state"
    state.mkdir(mode=0o700)
    token_file = state / "token"
    token_file.write_text("a" * 64 + "\n", encoding="utf-8")
    token_file.chmod(mode)
    return token_file, "a" * 64


def test_worker_only_renders_one_worker_from_the_hub_token(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    origin = make_origin(tmp_path)
    fake_runner(monkeypatch)
    token_file, token = hub_token_file(tmp_path)
    run_dir = (tmp_path / "worker-run").resolve()
    manifest = PREPARE_RUN.prepare_worker(
        "bob", str(origin), run_dir, WSL_URL, token_file, "claude-code"
    )
    identity = manifest["workspaces"]["bob"]
    written = run_dir / "configs" / "bob.mcp.json"
    hub = json.loads(written.read_text(encoding="utf-8"))["mcpServers"]["hub"]
    assert hub["env"]["HUB_URL"] == WSL_URL
    assert hub["env"]["HUB_TOKEN"] == token
    assert hub["env"]["HUB_WORKSPACE"] == identity["path"] == str(run_dir / "bob")
    assert hub["env"]["HUB_TELEMETRY_LOG"] == str(run_dir / "bob-telemetry.jsonl")
    assert hub["env"]["HUB_HARNESS_VERSION"] == "2.1.277"
    assert hub["args"][3] == ROOT.as_posix()
    assert written.stat().st_mode & 0o077 == 0
    assert (run_dir / "bob.prompt.md").exists()
    assert not (run_dir / "charlie").exists()
    # The token is only read: no token file is minted or copied on this host.
    assert [path for path in run_dir.rglob("token*")] == []
    for path in (run_dir / "bob").rglob("*"):
        if path.is_file() and ".git" not in path.parts:
            assert token not in path.read_text(encoding="utf-8", errors="replace")
    out = capsys.readouterr().out
    assert f"curl.exe -fsS {WSL_URL}/healthz" in out
    # The hub run may advertise a --public-url this host was never told.
    assert "its url must be the hub run's --public-url + /a2a" in out
    assert f"{WSL_URL}/a2a" not in out
    script = run_dir / "start-bob.sh"
    assert str(script) in out
    assert f"--strict-mcp-config --mcp-config {written}" in script.read_text(encoding="utf-8")
    assert manifest["launch"]["agents"]["bob"]["script"] == str(script)
    assert token not in out
    # A rerun reuses the clone and identity.
    again = PREPARE_RUN.prepare_worker(
        "bob", str(origin), run_dir, WSL_URL, token_file, "claude-code"
    )
    assert again["workspaces"] == manifest["workspaces"]


def test_worker_only_carries_a_non_default_port(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    origin = make_origin(tmp_path)
    fake_runner(monkeypatch)
    token_file, _ = hub_token_file(tmp_path)
    run_dir = (tmp_path / "worker-run").resolve()
    manifest = PREPARE_RUN.prepare_worker(
        "bob", str(origin), run_dir, PORT_URL, token_file, "claude-code"
    )
    hub = json.loads((run_dir / "configs" / "bob.mcp.json").read_text(encoding="utf-8"))
    assert hub["mcpServers"]["hub"]["env"]["HUB_URL"] == manifest["hub_url"] == PORT_URL
    out = capsys.readouterr().out
    assert f"curl.exe -fsS {PORT_URL}/healthz" in out
    assert f"curl.exe -fsS {PORT_URL}/.well-known/agent-card.json" in out
    assert ":8420" not in out


@pytest.mark.parametrize(
    "mode,url,expected",
    [
        (0o644, WSL_URL, "owner-only"),
        (0o600, "http://127.0.0.1:8420", "cannot be loopback"),
        (None, WSL_URL, "does not exist"),
    ],
)
def test_worker_only_rejects_loose_missing_token_or_loopback_url(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mode: int | None, url: str, expected: str
) -> None:
    fake_runner(monkeypatch)
    if mode is None:
        token_file = tmp_path / "missing-token"
    else:
        token_file, _ = hub_token_file(tmp_path, mode)
    run_dir = (tmp_path / "worker-run").resolve()
    with pytest.raises(ValueError, match=expected):
        PREPARE_RUN.prepare_worker(
            "bob", "test-org/test-repo", run_dir, url, token_file, "claude-code"
        )
    assert not run_dir.exists()


def test_worker_only_refuses_hub_flags(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    token_file, _ = hub_token_file(tmp_path)
    run_dir = (tmp_path / "run").resolve()
    for extra in (
        ["--worker-only", "bob", "--token-file", str(token_file), "--issue", "42"],
        ["--worker-only", "bob", "--token-file", str(token_file), "--roadmap", "2"],
        ["--worker-only", "bob", "--token-file", str(token_file), "--hub-port", "8521"],
        ["--worker-only", "bob"],
        ["--token-file", str(token_file)],
    ):
        monkeypatch.setattr(
            sys,
            "argv",
            ["prepare-run.py", "--repository", "test-org/test-repo", "--run-dir", str(run_dir)]
            + extra,
        )
        with pytest.raises(SystemExit) as exc:
            PREPARE_RUN.main()
        assert exc.value.code == 2
    assert not run_dir.exists()


def test_git_bash_path_only_rewrites_windows_drives() -> None:
    assert PREPARE_RUN.git_bash_path(PureWindowsPath("D:/Runs/x")) == "/d/Runs/x"
    assert PREPARE_RUN.git_bash_path(PurePosixPath("/srv/run")) == "/srv/run"


WAIT_TEXT = "Use a 100 second wait time for all hub waits including"


def test_all_claude_run_renders_start_scripts_prompts_and_manifest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    origin = make_origin(tmp_path)
    fake_runner(monkeypatch)
    run_dir = (tmp_path / "run").resolve()
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "prepare-run.py",
            "--repository",
            str(origin),
            "--run-dir",
            str(run_dir),
            "--issue",
            "42",
            "--alice-harness",
            "Claude",
            "--alice-model",
            "sonnet",
            "--alice-effort",
            "high",
            "--bob-harness",
            "CLAUDE",
            "--bob-model",
            "claude-opus-5-5",
            "--bob-effort",
            "max",
            "--charlie-harness",
            "claude",
            "--charlie-effort",
            "medium",
        ],
    )
    PREPARE_RUN.main()
    out = capsys.readouterr().out
    report = json.loads(out.split("\nStart scripts")[0])
    manifest = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
    assert manifest["harnesses"] == {"bob": "claude-code", "charlie": "claude-code"}
    assert manifest["launch"]["auto_start"] is True
    expected = {
        "alice": ("sonnet", "high", run_dir / "alice-runtime"),
        "bob": ("claude-opus-5-5", "max", run_dir / "bob"),
        "charlie": ("", "medium", run_dir / "charlie"),
    }
    for name, (model, effort, workdir) in expected.items():
        script = run_dir / f"start-{name}.sh"
        assert report["start_scripts"][name] == str(script) and str(script) in out
        assert manifest["launch"]["agents"][name] == {
            "harness": "claude-code",
            "model": model,
            "effort": effort,
            "script": str(script),
        }
        assert os.access(script, os.X_OK)
        lines = script.read_text(encoding="utf-8").splitlines()
        assert lines[:3] == [
            "#!/usr/bin/env bash",
            "# Generated by scripts/prepare-run.py",
            "set -e",
        ]
        assert lines[3] == f"cd {workdir}"
        flags = (f"--model {model} " if model else "") + f"--effort {effort} "
        assert lines[4] == (
            f"claude {flags}--permission-mode auto --strict-mcp-config "
            f"--mcp-config {run_dir / 'configs' / f'{name}.mcp.json'} --add-dir {run_dir} "
            f"-p 'Read {run_dir / f'{name}.prompt.md'} and follow the instructions in it'"
        )
        assert len(lines) == 5
        prompt = (run_dir / f"{name}.prompt.md").read_text(encoding="utf-8")
        assert WAIT_TEXT in prompt
        assert f"`closeout-report-{name}.md`" in prompt
        ending = "closeout report." if name == "alice" else "left uncommitted."
        assert prompt.rstrip().endswith(ending)
    bob_env = json.loads((run_dir / "configs" / "bob.mcp.json").read_text(encoding="utf-8"))
    assert bob_env["mcpServers"]["hub"]["env"]["HUB_MODEL"] == "claude-opus-5-5"
    assert durable_goal(run_dir) == ISSUE_GOAL_NO_ROADMAP.format(repo=origin) + "\n\n"
    alice_prompt = (run_dir / "alice.prompt.md").read_text(encoding="utf-8")
    assert "After you release bob and charlie" in alice_prompt
    assert "`wait_for_event`" in alice_prompt


async def test_codex_alice_gets_a_codex_home_with_every_hub_tool(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from agent_hub.database import initialize_database
    from agent_hub.mcp import create_mcp
    from agent_hub.store import HubStore

    origin = make_origin(tmp_path)
    fake_runner(monkeypatch)
    run_dir = (tmp_path / "run").resolve()
    manifest = PREPARE_RUN.prepare(
        str(origin),
        run_dir,
        issue=42,
        alice_harness="codex",
        alice_model="gpt-6-luna",
        alice_effort="xhigh",
    )
    home = run_dir / "configs" / "alice-codex"
    config = tomllib.loads((home / "config.toml").read_text(encoding="utf-8"))
    assert (home / "config.toml").stat().st_mode & 0o077 == 0
    assert (home / "skills" / "alice-orchestrator" / "SKILL.md").exists()
    assert not (run_dir / "configs" / "alice.mcp.json").exists()
    hub = config["mcp_servers"]["hub"]
    assert hub["args"] == ["run", "--locked", "--directory", str(ROOT), "hub"]
    assert hub["env"]["HUB_STATE_DIR"] == str(run_dir / "hub-state")
    initialize_database(tmp_path / "hub.db")
    served = {tool.name for tool in await create_mcp(HubStore(tmp_path / "hub.db")).list_tools()}
    assert set(hub["enabled_tools"]) == set(hub["tools"]) == served

    script = (run_dir / "start-alice.sh").read_text(encoding="utf-8")
    assert f"cd {run_dir / 'alice-runtime'}" in script
    assert (
        f"CODEX_HOME={home} codex exec -C . --approve-for-me --model gpt-6-luna "
        f"-c 'model_reasoning_effort=\"xhigh\"' - < {run_dir / 'alice.prompt.md'}"
    ) in script
    prompt = (run_dir / "alice.prompt.md").read_text(encoding="utf-8")
    assert "Codex runtime note" in prompt and WAIT_TEXT not in prompt
    assert WAIT_TEXT not in (run_dir / "charlie.prompt.md").read_text(encoding="utf-8")
    assert manifest["launch"]["agents"]["alice"]["harness"] == "codex"
    report = printed_report(capsys)
    assert report["configs"]["alice"] == str(home / "config.toml")
    assert report["checks"]["codex_auth"]["alice"] == "alice-codex: Logged in using ChatGPT"


def test_no_auto_start_opens_each_agent_without_its_prompt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    origin = make_origin(tmp_path)
    fake_runner(monkeypatch)
    run_dir = (tmp_path / "run").resolve()
    manifest = PREPARE_RUN.prepare(
        str(origin), run_dir, alice_harness="codex", charlie_model="gpt-6-sol", auto_start=False
    )
    assert manifest["launch"]["auto_start"] is False
    for name in ("alice", "bob", "charlie"):
        script = (run_dir / f"start-{name}.sh").read_text(encoding="utf-8")
        assert "prompt.md" not in script and " -p " not in script and "exec" not in script
        assert "--permission-mode" not in script
    assert (
        f"CODEX_HOME={run_dir / 'configs' / 'codex'} codex -C . "
        f"--add-dir {run_dir / 'charlie' / '.git'} --approve-for-me --model gpt-6-sol\n"
    ) in (run_dir / "start-charlie.sh").read_text(encoding="utf-8")
    assert "Auto-start is off" in capsys.readouterr().out


@pytest.mark.parametrize("value", ["gemini", "claude-desktop", ""])
def test_unsupported_harness_names_fail_before_any_artifact(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, value: str
) -> None:
    run_dir = (tmp_path / "run").resolve()
    message = run_main(monkeypatch, run_dir, "--alice-harness", value)
    assert message.startswith("prepare-run: error: --alice-harness")
    assert "expected claude or codex" in message
    assert not run_dir.exists()


def test_remote_worker_command_forwards_effort_and_auto_start(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    origin = make_origin(tmp_path)
    fake_runner(monkeypatch)
    run_dir = (tmp_path / "run").resolve()
    PREPARE_RUN.prepare(
        str(origin),
        run_dir,
        hub_host="0.0.0.0",
        hub_url=WSL_URL,
        remote_worker="charlie",
        charlie_effort="high",
        auto_start=False,
    )
    out = capsys.readouterr().out
    command = next(line for line in out.splitlines() if "--worker-only charlie" in line)
    assert "--charlie-harness codex" in command
    assert "--charlie-effort 'high'" in command and command.endswith("--no-auto-start")
    assert not (run_dir / "start-charlie.sh").exists()
    assert (run_dir / "start-bob.sh").exists()
