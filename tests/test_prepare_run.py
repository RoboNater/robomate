"""A prepared run attaches all agents to an existing HTTP hub."""

from __future__ import annotations

import errno
import hashlib
import importlib.util
import json
import os
import shlex
import shutil
import signal
import subprocess
import sys
import threading
import time
import tomllib
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any

import pytest
from agent_hub.database import initialize_database
from agent_hub.store import HubStore
from agent_hub_common.discovery import state_dir, token_file

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("prepare_run", ROOT / "scripts/prepare-run.py")
assert SPEC and SPEC.loader
PREPARE_RUN = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(PREPARE_RUN)
RUN_COMMON = sys.modules["run_common"]


class WindowsLinkError(OSError):
    """A Windows error that remains typed on POSIX test runners."""

    winerror: int


_LAUNCHER_SPEC = importlib.util.spec_from_file_location(
    "alice_launcher", ROOT / "scripts/alice-launcher.py"
)
assert _LAUNCHER_SPEC and _LAUNCHER_SPEC.loader
ALICE_LAUNCHER = importlib.util.module_from_spec(_LAUNCHER_SPEC)
# Dataclasses resolve their module through sys.modules while the class is built.
sys.modules.setdefault("alice_launcher", ALICE_LAUNCHER)
_LAUNCHER_SPEC.loader.exec_module(ALICE_LAUNCHER)

# A local run's start scripts are PowerShell on a Windows host, bash elsewhere.
SCRIPT_SUFFIX = "ps1" if os.name == "nt" else "sh"


@pytest.fixture(autouse=True)
def isolated_credential_homes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """#179: every preparation test must stay away from operator credentials."""
    for variable in ("CODEX_HOME", "AGY_HOME"):
        home = tmp_path / variable.lower()
        home.mkdir()
        monkeypatch.setenv(variable, str(home))


bash_launch_lines = pytest.mark.skipif(
    os.name == "nt",
    reason="a Windows host renders PowerShell launch lines "
    "(test_launch_lines_windows_powershell); these check the bash rendering",
)


def origin(tmp_path: Path) -> Path:
    path = tmp_path / "origin"
    subprocess.run(["git", "init", "-b", "main", str(path)], check=True, capture_output=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(path),
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
    return path


def hub_repo(tmp_path: Path, url: str = "http://127.0.0.1:8521") -> Path:
    repo = tmp_path / "target"
    state = state_dir(repo)
    state.mkdir(parents=True)
    token = token_file(repo)
    token.write_text("test-token\n")
    token.chmod(0o600)
    (state / "hub.json").write_text(
        json.dumps(
            {
                "pid": os.getpid(),
                "url": url,
                "port": PREPARE_RUN.url_port(url, "test"),
                "hub_id": "abc",
            }
        )
    )
    return repo


def running_hub(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    url: str = "http://127.0.0.1:8521",
) -> Path:
    target = hub_repo(tmp_path, url)
    monkeypatch.setattr(
        PREPARE_RUN,
        "probe_versions",
        lambda _: (
            {
                "claude": "2.1",
                "codex": "0.1",
                "opencode": "1.18.32",
                "agy": "1.2.7",
                "devin": "devin 3000.11.3 (9c803229faa4)",
            },
            {
                "claude-code": "2.1",
                "codex": "0.1",
                "opencode": "1.18.32",
                "antigravity": "1.2.7",
                "devin": "3000.11.3",
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


def test_a_linked_worktree_hub_repo_uses_that_worktrees_hub(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#147 Test 7: the worktree's token and workflow check, not the main checkout's."""

    source = origin(tmp_path)
    main = running_hub(tmp_path, monkeypatch)
    for args in (
        ["init", "-b", "main"],
        ["-c", "user.name=T", "-c", "user.email=t@example.com", "commit", "--allow-empty"]
        + ["-m", "start"],
        ["worktree", "add", "-b", "wt", str(tmp_path / "wt")],
    ):
        subprocess.run(["git", *args], cwd=main, check=True, capture_output=True)
    worktree = tmp_path / "wt"
    state_dir(worktree).mkdir()
    token_file(worktree).write_text("worktree-token\n")
    token_file(worktree).chmod(0o600)
    url = "http://127.0.0.1:8522"
    (state_dir(worktree) / "hub.json").write_text(
        json.dumps({"pid": os.getpid(), "url": url, "port": 8522, "hub_id": "wt"})
    )
    # The main checkout's hub is busy with another workflow; that is not this run's hub.
    initialize_database(state_dir(main) / "hub.db")
    HubStore(state_dir(main) / "hub.db").initialize_workflow("another goal", {})
    run_dir = tmp_path / "run"
    manifest = PREPARE_RUN.prepare(
        str(source), run_dir, issue=42, account="tester", hub_repo=worktree
    )
    assert (manifest["hub_repo"], manifest["state_dir"]) == (
        str(worktree),
        str(state_dir(worktree)),
    )
    alice = json.loads((run_dir / "configs/alice.mcp.json").read_text())["mcpServers"]
    assert alice["robomate"]["env"]["ROBOMATE_HUB_URL"] == url
    assert alice["robomate"]["env"]["ROBOMATE_TOKEN_FILE"] == str(token_file(worktree))


def test_configs_use_bridge_and_existing_hub(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
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


@pytest.mark.parametrize("auto_start", [False, True])
def test_doctor_context_is_separate_from_team_profiles(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    auto_start: bool,
) -> None:
    source_config = tmp_path / "settings.toml"
    source_config.write_text("# original config\n")
    run_dir, manifest = prepare(
        tmp_path,
        monkeypatch,
        bob_model="future model",
        bob_effort="future effort",
        source_config=source_config,
        auto_start=auto_start,
    )
    doctor = manifest["denise"]
    assert doctor["status"] == "ready" and doctor["startup_status"] == "pending-step-2"
    assert (doctor["settings_from"], doctor["model"], doctor["effort"]) == (
        "bob",
        "future model",
        "future effort",
    )
    assert set(manifest["launch"]["agents"]) == {"alice", "bob", "charlie"}
    assert set(manifest["workspaces"]) == {"bob", "charlie"}
    assert not list(run_dir.glob("start-denise*"))
    context = json.loads(Path(doctor["context"]).read_text())
    assert context["tooling"]["root"]["path"] == str(ROOT)
    assert context["hub"]["checkout"]["path"] == manifest["hub_repo"]
    assert context["original_config"]["path"] == str(source_config)
    assert context["agents"]["alice"]["config"]["access"] == "present"
    assert context["agents"]["bob"]["workspace_id"] == manifest["workspaces"]["bob"]["workspace_id"]
    assert context["agents"]["bob"]["scripts"]["resume_script"]["access"] == "present"
    assert "test-token" not in Path(doctor["context"]).read_text()
    assert not list(Path(doctor["configs"]).iterdir())


@pytest.mark.parametrize("harness", ["devin", "opencode", "antigravity"])
@pytest.mark.parametrize("explicit_selector", [False, True])
def test_unsupported_inherited_doctor_does_not_break_team_preparation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    harness: str,
    explicit_selector: bool,
) -> None:
    options = {"denise_settings_from": "bob"} if explicit_selector else {}
    run_dir, manifest = prepare(tmp_path, monkeypatch, bob_harness=harness, **options)
    assert manifest["denise"]["status"] == "skipped"
    assert "--denise-harness" in manifest["denise"]["skip_reason"]
    assert "Warning: skipping Denise" in capsys.readouterr().err
    assert "script" not in manifest["denise"]
    assert not list(run_dir.glob("start-denise*"))
    assert Path(manifest["launch"]["agents"]["bob"]["script"]).exists()


def test_unsupported_explicit_doctor_fails_before_hub_or_repo_access(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def unexpected(*args: Any) -> None:
        pytest.fail("doctor settings must be checked before accessing a hub")

    monkeypatch.setattr(PREPARE_RUN, "read_hub_json", unexpected)
    with pytest.raises(ValueError, match="explicit --denise-harness"):
        PREPARE_RUN.prepare("repository", tmp_path / "new-run", denise_harness="devin")
    assert not (tmp_path / "new-run").exists()


@pytest.mark.parametrize("legacy", [False, True])
def test_existing_manifests_do_not_regenerate_or_retrofit_doctor_artifacts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    legacy: bool,
) -> None:
    run_dir, manifest = prepare(tmp_path, monkeypatch)
    context_path = Path(manifest["denise"]["context"])
    previous = context_path.read_bytes()
    if legacy:
        del manifest["denise"]
        context_path.unlink()
    (run_dir / "run.json").write_text(json.dumps(manifest))
    again = PREPARE_RUN.prepare(
        manifest["repository"],
        run_dir,
        issue=42,
        account="tester",
        hub_repo=Path(manifest["hub_repo"]),
        denise_harness="codex",
    )
    if legacy:
        assert "denise" not in again and not context_path.exists()
    else:
        assert again["denise"] == manifest["denise"]
        assert context_path.read_bytes() == previous


async def test_codex_alice_uses_orchestrator_bridge(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from worker_mcp.orchestrator import OrchestratorBridge, create_orchestrator_mcp

    run_dir, _ = prepare(tmp_path, monkeypatch, alice_harness="codex")
    config = tomllib.loads((run_dir / "configs/alice-codex/config.toml").read_text())
    server = config["mcp_servers"]["robomate"]
    assert server["args"][-3:] == ["mcp", "--role", "orchestrator"]
    assert set(server["enabled_tools"]) == set(PREPARE_RUN.ALICE_TOOLS)
    served = {
        tool.name for tool in await create_orchestrator_mcp(OrchestratorBridge()).list_tools()
    }
    assert "join" in served
    assert set(server["enabled_tools"]) == served
    assert server["tools"]["join"]["approval_mode"] == "approve"


def test_missing_or_different_hub_is_rejected(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = origin(tmp_path)
    with pytest.raises(ValueError, match="--hub-repo is required"):
        PREPARE_RUN.prepare(str(source), tmp_path / "run")
    target = hub_repo(tmp_path)
    monkeypatch.setattr(PREPARE_RUN, "hub_healthy", lambda *_: True)
    with pytest.raises(ValueError, match="differs from the running hub"):
        PREPARE_RUN.prepare(
            str(source), tmp_path / "run", hub_repo=target, hub_url="http://127.0.0.1:9999"
        )
    monkeypatch.setattr(PREPARE_RUN, "hub_healthy", lambda *_: False)
    with pytest.raises(ValueError, match="not responding"):
        PREPARE_RUN.prepare(str(source), tmp_path / "run", hub_repo=target)


def test_rerun_preserves_clones_and_external_token(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_dir, first = prepare(tmp_path, monkeypatch)
    token_path = tmp_path / "target/.robomate/token"
    original_token = token_path.read_text()
    second = PREPARE_RUN.prepare(
        str(tmp_path / "origin"),
        run_dir,
        issue=42,
        account="tester",
        hub_repo=tmp_path / "target",
    )
    assert first["workspaces"] == second["workspaces"]
    assert token_path.read_text() == original_token
    assert not (run_dir / "hub-state").exists()
    for name in ("bob", "charlie"):
        assert not (run_dir / name / "configs").exists()
        assert not (run_dir / name / ".mcp.json").exists()


@pytest.mark.parametrize("harness", ["claude-code", "codex", "opencode", "antigravity", "devin"])
def test_closeout_reports_leave_custom_clones_clean_on_rerun(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, harness: str
) -> None:
    bob_dir = tmp_path / "custom-bob"
    charlie_dir = tmp_path / "custom-charlie"
    run_dir, manifest = prepare(
        tmp_path,
        monkeypatch,
        alice_harness=harness,
        bob_harness=harness,
        charlie_harness=harness,
        bob_dir=bob_dir,
        charlie_dir=charlie_dir,
    )
    for name in ("alice", "bob", "charlie"):
        report = run_dir / "closeout" / f"{name}.md"
        prompt = (run_dir / f"{name}.prompt.md").read_text(encoding="utf-8")
        assert f"`{report.as_posix()}`" in prompt
        report.write_text(f"{name}: summary, outstanding items, lessons\n", encoding="utf-8")
        if harness == "codex":
            home = PREPARE_RUN.codex_home_name(name)
            config = tomllib.loads((run_dir / "configs" / home / "config.toml").read_text())
            assert config["sandbox_workspace_write"]["writable_roots"] == [
                (run_dir / "closeout").as_posix()
            ]
    for clone in (bob_dir, charlie_dir):
        status = subprocess.run(
            ["git", "-C", str(clone), "status", "--porcelain"],
            check=True,
            capture_output=True,
            text=True,
        )
        assert status.stdout == ""
    second = PREPARE_RUN.prepare(
        str(tmp_path / "origin"),
        run_dir,
        issue=42,
        account="tester",
        hub_repo=tmp_path / "target",
        alice_harness=harness,
        bob_harness=harness,
        charlie_harness=harness,
        bob_dir=bob_dir,
        charlie_dir=charlie_dir,
    )
    assert second["workspaces"] == manifest["workspaces"]
    assert (run_dir / "closeout" / "bob.md").read_text().startswith("bob:")


@pytest.mark.parametrize("parent", ["target", "worker"])
def test_run_directory_cannot_put_reports_inside_a_configured_clone(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, parent: str
) -> None:
    source = origin(tmp_path)
    target = running_hub(tmp_path, monkeypatch)
    run_dir = tmp_path / parent / "run"
    with pytest.raises(ValueError, match="outside the hub checkout and worker clones"):
        PREPARE_RUN.prepare(str(source), run_dir, hub_repo=target, bob_dir=tmp_path / "worker")
    assert not run_dir.exists()


def test_worker_only_rejects_run_directory_inside_its_clone(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="outside the worker clone"):
        PREPARE_RUN.prepare_worker(
            "bob",
            "test-org/test-repo",
            tmp_path / "worker" / "run",
            "http://192.0.2.10:8420",
            tmp_path / "token",
            "claude-code",
            worker_dir=tmp_path / "worker",
        )


@pytest.mark.skipif(os.name == "nt", reason="symlink creation may need Windows privileges")
def test_closeout_directory_cannot_redirect_reports_into_a_clone(tmp_path: Path) -> None:
    run_dir = tmp_path / "run"
    clone = tmp_path / "clone"
    run_dir.mkdir()
    clone.mkdir()
    (run_dir / "closeout").symlink_to(clone, target_is_directory=True)
    with pytest.raises(ValueError, match="must not be a symlink"):
        PREPARE_RUN.prepare_closeout_dir(run_dir)
    assert list(clone.iterdir()) == []


def test_existing_hub_workflow_requires_the_same_run_and_goal(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
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
    assert "fresh checkout of the target (a new linked worktree or clone)" in message
    assert not (tmp_path / "run2").exists()
    assert database_path.stat().st_mtime_ns == modified

    with pytest.raises(ValueError, match="original run directory and goal"):
        PREPARE_RUN.prepare(str(source), run_dir, issue=43, hub_repo=target)
    with pytest.raises(ValueError, match="policy differs.*original preparation options"):
        PREPARE_RUN.prepare(str(source), run_dir, issue=42, hub_repo=target, merge_method="merge")
    resumed = PREPARE_RUN.prepare(str(source), run_dir, issue=42, hub_repo=target)
    assert resumed["work"]["goal"] == goal


def test_hub_with_all_done_workflows_accepts_a_new_run(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """M2 Step 3: `check_hub_workflow` accepts a hub whose workflows are all done."""

    from agent_hub_common import WorkflowStatus

    source = origin(tmp_path)
    target = running_hub(tmp_path, monkeypatch)
    database_path = target / ".robomate/hub.db"
    initialize_database(database_path)
    run_dir = tmp_path / "run1"
    first = PREPARE_RUN.prepare(str(source), run_dir, issue=42, hub_repo=target)
    store = HubStore(database_path)
    store.initialize_workflow(first["work"]["goal"], first["policy"])
    store.set_workflow_status(WorkflowStatus.DONE, "Done", actor="alice", session=None)
    # A new issue on the same hub with all workflows done prepares cleanly.
    second = PREPARE_RUN.prepare(str(source), tmp_path / "run2", issue=43, hub_repo=target)
    assert second["work"]["goal"] != first["work"]["goal"]


def test_worker_only_uses_token_file_not_bearer_value(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = origin(tmp_path)
    target = hub_repo(tmp_path, "http://192.0.2.1:8521")
    monkeypatch.setattr(
        PREPARE_RUN, "probe_versions", lambda _: ({"claude": "2.1"}, {"claude-code": "2.1"})
    )
    run_dir = tmp_path / "remote"
    PREPARE_RUN.prepare_worker(
        "bob",
        str(source),
        run_dir,
        "http://192.0.2.1:8521",
        target / ".robomate/token",
        "claude-code",
    )
    config = json.loads((run_dir / "configs/bob.mcp.json").read_text())
    env = config["mcpServers"]["robomate"]["env"]
    assert env["ROBOMATE_TOKEN_FILE"] == str(target / ".robomate/token")
    assert "HUB_TOKEN" not in env


def test_script_help_loads() -> None:
    result = subprocess.run(
        [sys.executable, str(ROOT / "scripts/prepare-run.py"), "--help"],
        capture_output=True,
        text=True,
        check=True,
    )
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
        "claude-code",
        run_dir / "bob",
        config,
        prompt,
        run_dir / "bob" / ".git",
        "opus[1m]",
        "high",
        auto_start,
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
        f"{base} -p 'Read {prompt} and follow the instructions in it'" if auto_start else base
    )
    assert lines == [f"cd {run_dir / 'charlie'}", expected]


def test_launch_lines_omit_unset_model_and_effort(tmp_path: Path) -> None:
    run_dir = (tmp_path / "run").resolve()
    alice = PREPARE_RUN.launch_lines(
        "codex",
        run_dir / "alice-runtime",
        run_dir / "configs" / "alice-codex",
        run_dir / "alice.prompt.md",
        None,
    )
    # PowerShell sets CODEX_HOME on a line of its own.
    command = alice[-1]
    assert "--add-dir" not in command and "--model" not in command and " -c " not in command
    # Alice's runtime is not a clone, so Codex must skip its git-repo check (#45).
    assert "-C . --skip-git-repo-check --approve-for-me" in command
    claude = PREPARE_RUN.launch_lines(
        "claude-code",
        run_dir / "alice-runtime",
        run_dir / "configs" / "alice.mcp.json",
        run_dir / "alice.prompt.md",
        None,
    )
    assert claude[1].startswith("claude --permission-mode auto --strict-mcp-config")


def test_launch_lines_windows_powershell(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(os, "name", "nt")
    run_dir = (tmp_path / "run").resolve()
    prompt = run_dir / "charlie.prompt.md"
    lines = PREPARE_RUN.launch_lines(
        "codex",
        run_dir / "charlie",
        run_dir / "configs" / "codex",
        prompt,
        run_dir / "charlie" / ".git",
        "gpt-6-sol",
        "high",
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
        "--model",
        "'opus[1m]'",
        "--effort",
        "max",
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
    result = subprocess.run(["bash"], input=script, text=True, capture_output=True, cwd=tmp_path)
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
    kind: str,
    kwargs: dict[str, object],
    check: str,
    roadmap: str | None,
    expected_roadmap: str | None,
    has_roadmap_instructions: bool,
) -> None:
    """Each {--issue, --work-file, neither} x {no roadmap, N, #N, OWNER/REPO#N} goal (#34)."""
    slug = "test-org/test-repo"
    repository = "git@github.com:test-org/test-repo.git"
    goal = PREPARE_RUN.render_goal(
        slug,
        repository,
        kwargs.get("issue"),
        kwargs.get("work"),
        roadmap,
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
        "bob",
        "codex",
        WINDOWS_RUN,
        WINDOWS_RUN / "bob",
        "gpt-6-sol",
        "high",
        token_file="C:/private/token",
    )
    assert lines[:4] == [
        "cd /c/Users/Bob/runs/step7/bob",
        "export TMPDIR=/c/Users/Bob/runs/step7/tmp/bob",
        "export TEMP=C:/Users/Bob/runs/step7/tmp/bob",
        "export TMP=C:/Users/Bob/runs/step7/tmp/bob",
    ]
    command = lines[-1]
    assert "CODEX_HOME=C:/Users/Bob/runs/step7/configs/bob-codex" in command
    assert "worker-launcher.py" in command and "--token-file C:/private/token" in command
    assert "--prompt C:/Users/Bob/runs/step7/bob.prompt.md" in command
    assert "-- codex exec -C . --add-dir C:/Users/Bob/runs/step7/bob/.git" in command
    assert "--approve-for-me --model gpt-6-sol" in command
    assert "< /c/" not in command  # native Python opens the prompt file now
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
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    statement = "Address `acme/app#7` and `acme/app#9` in one pull request.\n"
    work_file = tmp_path / "sow.md"
    work_file.write_bytes(statement.encode())
    run_dir, manifest = prepare(tmp_path, monkeypatch, issue=None, work_file=work_file)
    goal = manifest["work"]["goal"]
    assert goal.startswith(statement.strip()) and "no roadmap edit" in goal
    assert manifest["issue"] is None
    assert manifest["work"] == {
        "goal": goal,
        "path": str(work_file.resolve()),
        "sha256": hashlib.sha256(statement.encode()).hexdigest(),
        "roadmap": None,
    }
    assert json.loads((run_dir / "run.json").read_text())["work"] == manifest["work"]


def test_issue_goal_defaults_to_no_roadmap_edit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_dir, manifest = prepare(tmp_path, monkeypatch)
    goal = manifest["work"]["goal"]
    assert goal == (
        f"Address issue `{tmp_path / 'origin'}#42`, merge its pull request, "
        "and close out with no roadmap edit; record the merge only in the "
        "workflow summary."
    )
    assert goal in (run_dir / "alice.prompt.md").read_text()
    assert manifest["work"]["roadmap"] is None


def test_prepare_records_the_rendered_roadmap_in_run_json(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_dir, manifest = prepare(tmp_path, monkeypatch, roadmap="2")
    assert manifest["work"]["roadmap"] == f"{tmp_path / 'origin'}#2"
    assert f"({tmp_path / 'origin'}#2)" in manifest["work"]["goal"]
    assert json.loads((run_dir / "run.json").read_text())["work"] == manifest["work"]


def test_work_file_with_roadmap_appends_bob_instructions(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    work_file = tmp_path / "sow.md"
    work_file.write_text("Address two issues together.\n")
    _, manifest = prepare(tmp_path, monkeypatch, issue=None, work_file=work_file, roadmap="5")
    assert manifest["work"]["roadmap"] == f"{tmp_path / 'origin'}#5"
    assert "should make a decision on what roadmap" in manifest["work"]["goal"]


def test_work_file_with_issue_fails_actionably(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    work_file = tmp_path / "sow.md"
    work_file.write_text("Do the work.\n")
    with pytest.raises(ValueError, match="mutually exclusive"):
        prepare(tmp_path, monkeypatch, work_file=work_file)
    assert not (tmp_path / "run").exists()


@pytest.mark.parametrize(
    "content,expected", [(None, "does not exist"), ("", "is empty"), (" \n\t", "is empty")]
)
def test_missing_or_empty_work_file_fails_actionably(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    content: str | None,
    expected: str,
) -> None:
    work_file = tmp_path / "sow.md"
    if content is not None:
        work_file.write_text(content)
    with pytest.raises(ValueError, match=expected):
        prepare(tmp_path, monkeypatch, issue=None, work_file=work_file)
    assert not (tmp_path / "run").exists()


def test_fails_when_gh_unauthenticated(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = hub_repo(tmp_path)
    monkeypatch.setattr(PREPARE_RUN, "hub_healthy", lambda *_: True)
    monkeypatch.setattr(
        PREPARE_RUN,
        "probe_versions",
        lambda _: ({"claude": "2", "codex": "1"}, {"claude-code": "2", "codex": "1"}),
    )
    monkeypatch.setattr(
        PREPARE_RUN,
        "check_gh_auth",
        lambda: (_ for _ in ()).throw(ValueError("gh is not authenticated")),
    )
    with pytest.raises(ValueError, match="authenticated"):
        PREPARE_RUN.prepare("test-org/test-repo", (tmp_path / "run").resolve(), hub_repo=target)


def test_fails_when_runtime_missing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = origin(tmp_path)
    target = hub_repo(tmp_path)
    monkeypatch.setattr(PREPARE_RUN, "hub_healthy", lambda *_: True)
    monkeypatch.setattr(
        PREPARE_RUN,
        "probe_versions",
        lambda _: (_ for _ in ()).throw(ValueError("requires the 'codex' CLI on PATH")),
    )
    with pytest.raises(ValueError, match="requires the 'codex' CLI on PATH"):
        PREPARE_RUN.prepare(str(source), (tmp_path / "run").resolve(), hub_repo=target)


def test_no_token_or_clone_path_inside_clones(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
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


def test_no_operator_token_or_its_path_in_the_run(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """#128: the launch files and output carry neither the credential nor where it is."""

    operator_file = tmp_path / "registry" / "operator-token"
    operator_file.parent.mkdir()
    operator_file.write_text("operator-secret-value\n")
    operator_file.chmod(0o600)
    monkeypatch.setenv("ROBOMATE_OPERATOR_TOKEN_FILE", str(operator_file))
    run_dir, _ = prepare(tmp_path, monkeypatch)
    printed = capsys.readouterr()
    needles = ("operator-secret-value", str(operator_file), "operator-token")
    for text in (printed.out, printed.err):
        assert not any(needle in text for needle in needles)
    files = [path for path in run_dir.rglob("*") if path.is_file() and ".git" not in path.parts]
    assert any(path.name.startswith("start-") for path in files)
    for path in files:
        text = path.read_text(errors="replace")
        assert not any(needle in text for needle in needles), path


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
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    source = origin(tmp_path)
    target = hub_repo(tmp_path, "http://127.0.0.1:8420")
    monkeypatch.setattr(PREPARE_RUN, "hub_healthy", lambda *_: True)
    monkeypatch.setattr(
        PREPARE_RUN,
        "probe_versions",
        lambda _: (
            {"claude": "2.1.277 (Claude Code)", "codex": "codex-cli 0.154.0"},
            {"claude-code": "2.1.277", "codex": "0.154.0"},
        ),
    )
    monkeypatch.setattr(PREPARE_RUN, "codex_login_status", lambda _: "Logged in using ChatGPT")
    run_dir = tmp_path / "run"
    manifest = PREPARE_RUN.prepare(
        str(source), run_dir, issue=42, account="testuser", hub_repo=target
    )
    masks = {
        # The interpreter running the tests, often under $ROOT: mask it first.
        sys.executable: "$PYTHON",
        str(run_dir): "$RUN_DIR",
        str(source): "$ORIGIN",
        str(target): "$HUB_REPO",
        str(ROOT): "$ROOT",
        manifest["denise"]["tooling_commit"]: "$TOOLING_COMMIT",
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
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_dir, manifest = prepare(
        tmp_path, monkeypatch, alice_harness="codex", charlie_model="gpt-6-sol", auto_start=False
    )
    assert manifest["launch"]["auto_start"] is False
    for name in ("alice", "bob", "charlie"):
        script = (run_dir / f"start-{name}.{SCRIPT_SUFFIX}").read_text()
        assert "prompt.md" not in script and " -p " not in script and "exec" not in script
    alice_script = (run_dir / f"start-alice.{SCRIPT_SUFFIX}").read_text()
    assert "codex -C . --approve-for-me" in alice_script
    assert "--skip-git-repo-check" not in alice_script


def test_codex_alice_exec_uses_skip_git_repo_check(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_dir, _ = prepare(
        tmp_path, monkeypatch, alice_harness="codex", alice_model="gpt-6-luna", alice_effort="xhigh"
    )
    script = (run_dir / f"start-alice.{SCRIPT_SUFFIX}").read_text()
    assert "codex exec -C . --skip-git-repo-check --approve-for-me" in script
    home = run_dir / "configs/alice-codex"
    config = tomllib.loads((home / "config.toml").read_text())
    assert set(config["mcp_servers"]["robomate"]["enabled_tools"]) == set(PREPARE_RUN.ALICE_TOOLS)


@pytest.mark.parametrize("value", ["gemini", "claude-desktop", ""])
def test_unsupported_harness_names_fail_before_any_artifact(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    value: str,
) -> None:
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "prepare-run.py",
            "--repository",
            "test-org/test-repo",
            "--run-dir",
            str(tmp_path / "run"),
            "--alice-harness",
            value,
        ],
    )
    with pytest.raises(SystemExit, match="expected claude, codex, opencode, antigravity, or devin"):
        PREPARE_RUN.main()
    assert not (tmp_path / "run").exists()


def test_remote_worker_config_carries_windows_paths_and_a_private_token(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AGY_HOME", str(tmp_path / "no-agy-login"))
    root = PureWindowsPath("C:/work/robomate")
    run = PureWindowsPath("C:/Users/Bob/runs/step7")
    out_dir = tmp_path / "bundle"
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
        out_dir,
    )
    written = out_dir / "configs/bob.mcp.json"
    assert "`C:/Users/Bob/runs/step7/closeout/bob.md`" in (out_dir / "bob.prompt.md").read_text()
    assert (out_dir / "closeout").is_dir()
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
        "bob",
        "opencode",
        "1.18.32",
        "anthropic",
        "opencode/claude-sonnet-4-6",
        "",
        "C:/private/token",
        "http://172.26.115.68:8420",
        root,
        run,
        run / "bob",
        oc_out,
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
        "bob",
        "antigravity",
        "1.2.7",
        "google",
        "gemini-3.1-pro-high",
        "",
        "C:/private/token",
        "http://172.26.115.68:8420",
        root,
        run,
        run / "bob",
        agy_out,
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
        "USERPROFILE=C:/Users/Bob/runs/step7/configs/bob-agy "
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
            PREPARE_RUN.prepare_worker(
                "bob", "o/r", run_dir, "http://192.0.2.10:8420", token, "claude-code"
            )
    token.chmod(0o600)
    with pytest.raises(ValueError, match="cannot be loopback"):
        PREPARE_RUN.prepare_worker(
            "bob", "o/r", run_dir, "http://127.0.0.1:8420", token, "claude-code"
        )
    with pytest.raises(ValueError, match="does not exist"):
        PREPARE_RUN.prepare_worker(
            "bob", "o/r", run_dir, "http://192.0.2.10:8420", tmp_path / "missing", "claude-code"
        )
    assert not run_dir.exists()


def test_worker_only_refuses_hub_flags(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    token = tmp_path / "token"
    token.write_text("secret\n")
    token.chmod(0o600)
    for extra in (["--issue", "42"], ["--roadmap", "2"], ["--hub-port", "8521"]):
        monkeypatch.setattr(
            sys,
            "argv",
            [
                "prepare-run.py",
                "--repository",
                "o/r",
                "--run-dir",
                str(tmp_path / "run"),
                "--worker-only",
                "bob",
                "--token-file",
                str(token),
                *extra,
            ],
        )
        with pytest.raises(SystemExit):
            PREPARE_RUN.main()
    assert not (tmp_path / "run").exists()


@pytest.mark.parametrize(
    "protocol,expected",
    [
        ("https", "https://github.com/test-org/test-repo.git"),
        ("ssh", "git@github.com:test-org/test-repo.git"),
        ("", "https://github.com/test-org/test-repo.git"),
    ],
)
def test_slug_clone_url_honors_gh_protocol(
    monkeypatch: pytest.MonkeyPatch,
    protocol: str,
    expected: str,
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
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    remote_url = "http://192.0.2.10:8420"
    run_dir, manifest = prepare(
        tmp_path,
        monkeypatch,
        remote_worker="bob",
        test_hub_url=remote_url,
        bob_model="claude-opus-5-5",
    )
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
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    run_dir, _ = prepare(
        tmp_path,
        monkeypatch,
        remote_worker="charlie",
        test_hub_url="http://192.0.2.10:8420",
        charlie_effort="high",
        auto_start=False,
    )
    output = capsys.readouterr().out
    command = next(line for line in output.splitlines() if "--worker-only charlie" in line)
    assert "--charlie-effort 'high'" in command and command.endswith("--no-auto-start")
    assert not (run_dir / f"start-charlie.{SCRIPT_SUFFIX}").exists()
    assert (run_dir / f"start-bob.{SCRIPT_SUFFIX}").exists()


def test_rerun_is_idempotent_and_preserves_clone_token_identity(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = origin(tmp_path)
    target = hub_repo(tmp_path)
    monkeypatch.setattr(PREPARE_RUN, "hub_healthy", lambda *_: True)
    monkeypatch.setattr(
        PREPARE_RUN,
        "probe_versions",
        lambda _: ({"claude": "2", "codex": "1"}, {"claude-code": "2", "codex": "1"}),
    )
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
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
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
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
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


def test_fresh_run_produces_every_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    run_dir, manifest = prepare(tmp_path, monkeypatch, allow_no_ci="auto")
    assert (
        manifest["workspaces"]["bob"]["workspace_id"]
        != (manifest["workspaces"]["charlie"]["workspace_id"])
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
        "check_in",
        "get_role_guide",
        "await_assignment",
        "report_progress",
        "ask_alice",
        "submit_result",
        "get_operator_answer",
        "get_statement",
    }
    for name in ("alice", "bob", "charlie"):
        assert (run_dir / f"{name}.prompt.md").exists()
        assert (run_dir / f"start-{name}.{SCRIPT_SUFFIX}").exists()
    assert (run_dir / "alice-runtime/.claude/skills/alice-orchestrator").exists()
    report = json.loads(capsys.readouterr().out.split("\nStart scripts")[0])
    assert set(report["configs"]) == {"alice", "bob", "charlie"}


def test_all_claude_run_renders_start_scripts_prompts_and_manifest(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    source = origin(tmp_path)
    target = running_hub(tmp_path, monkeypatch)
    run_dir = tmp_path / "run"
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "prepare-run.py",
            "--repository",
            str(source),
            "--run-dir",
            str(run_dir),
            "--hub-repo",
            str(target),
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
    report = json.loads(capsys.readouterr().out.split("\nStart scripts")[0])
    manifest = json.loads((run_dir / "run.json").read_text())
    assert manifest["harnesses"] == {"bob": "claude-code", "charlie": "claude-code"}
    expected = {
        "alice": ("sonnet", "high"),
        "bob": ("claude-opus-5-5", "max"),
        "charlie": ("", "medium"),
    }
    for name, (model, effort) in expected.items():
        script = run_dir / f"start-{name}.{SCRIPT_SUFFIX}"
        assert report["start_scripts"][name] == str(script)
        assert manifest["launch"]["agents"][name]["model"] == model
        assert manifest["launch"]["agents"][name]["effort"] == effort
        assert os.access(script, os.X_OK)
        command = script.read_text()
        assert "--permission-mode auto --strict-mcp-config" in command
        if name == "alice":
            # The launcher hands Alice her prompt and resumes her (#146).
            assert "alice-launcher.py" in command
            assert f"--prompt {shlex.quote(str(run_dir / 'alice.prompt.md'))}" in command
        else:
            assert f"--prompt {shlex.quote(str(run_dir / f'{name}.prompt.md'))}" in command
        assert (
            f"`{(run_dir / 'closeout' / f'{name}.md').as_posix()}`"
            in (run_dir / f"{name}.prompt.md").read_text()
        )
    bob_env = json.loads((run_dir / "configs/bob.mcp.json").read_text())["mcpServers"]["robomate"][
        "env"
    ]
    assert bob_env["HUB_MODEL"] == "claude-opus-5-5"


def test_rendered_prompts_forbid_ending_the_turn_to_wait(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A headless worker that ends its turn to wait for CI exits and loses its task (#115).
    run_dir, _ = prepare(tmp_path, monkeypatch, alice_harness="claude-code")
    bob = " ".join((run_dir / "bob.prompt.md").read_text(encoding="utf-8").split())
    charlie = " ".join((run_dir / "charlie.prompt.md").read_text(encoding="utf-8").split())
    alice = " ".join((run_dir / "alice.prompt.md").read_text(encoding="utf-8").split())
    guide_rule = (
        "Your turn ends only after `await_assignment` returns `release: true`. "
        "A headless runtime (`claude -p`, `codex exec`, `opencode run`, `agy -p`) exits"
    )
    # Both the guide and the kickoff turn-end reminder reach every worker harness.
    assert guide_rule in bob and guide_rule in charlie
    claude_rule = "Do not end your turn until `await_assignment` returns `release: true`."
    assert claude_rule in bob and claude_rule in charlie
    assert "do not use `run_in_background` for a wait" in bob
    assert "Do not end your turn until the workflow is `done`." in alice
    # #122: the guide's detached-run recipe reaches every worker, in both shells, and
    # the Claude note points a Claude worker at it.
    for recipe in (
        # The directory is an argument, not program text: it may contain spaces.
        """echo $? > "$1/rc"' sh "$D" >/dev/null 2>&1 &)""",
        "`$LASTEXITCODE > '$D/rc'\"",
        'for i in $(seq 1 18); do [ -f "$D/rc" ] && break; sleep 5; done',
    ):
        assert recipe in bob and recipe in charlie
    pointer = "use the detached-run recipe in the Waiting section above"
    assert pointer in bob and pointer not in charlie and pointer not in alice


def test_remote_claude_worker_runs_the_launcher_from_git_bash() -> None:
    lines = PREPARE_RUN.worker_launch(
        "bob",
        "claude-code",
        WINDOWS_RUN,
        WINDOWS_RUN / "bob",
        root=PureWindowsPath("C:/src/robomate"),
        python="C:\\src\\robomate\\.venv\\Scripts\\python.exe",
        token_file="C:/private/token",
    )
    # Git Bash runs the interpreter; python and claude read Windows paths.
    command = lines[-1]
    assert (
        "/c/src/robomate/.venv/Scripts/python.exe C:/src/robomate/scripts/worker-launcher.py"
        in command
    )
    assert "--harness claude-code" in command and "--token-file C:/private/token" in command
    assert "--agent bob --telemetry C:/Users/Bob/runs/step7/bob-telemetry.jsonl" in command
    assert "--prompt C:/Users/Bob/runs/step7/bob.prompt.md -- claude" in command
    assert "--mcp-config C:/Users/Bob/runs/step7/configs/bob.mcp.json" in command


def test_same_harness_pair_renders_both_claude_configs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_dir, manifest = prepare(
        tmp_path,
        monkeypatch,
        bob_harness="claude-code",
        charlie_harness="claude-code",
        bob_model="claude-sonnet-5",
        charlie_model="claude-sonnet-5",
    )
    assert manifest["policy"]["role_policy"]["reviewer_harness_differs"] is False
    assert (run_dir / "configs/charlie.mcp.json").exists()
    charlie = json.loads((run_dir / "configs/charlie.mcp.json").read_text())
    assert charlie["mcpServers"]["robomate"]["env"]["AGENT_NAME"] == "charlie"


def test_bare_slug_expands_via_gh_protocol(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = running_hub(tmp_path, monkeypatch)
    seen: list[tuple[str, str]] = []

    def fake_bootstrap(agent: str, destination: Path, repository: str) -> dict[str, str]:
        seen.append((agent, repository))
        return {"path": str(destination), "workspace_id": ("a" if agent == "bob" else "b") * 64}

    monkeypatch.setattr(PREPARE_RUN, "bootstrap_clone", fake_bootstrap)
    for protocol, expected in (
        ("https", "https://github.com/test-org/test-repo.git"),
        ("ssh", "git@github.com:test-org/test-repo.git"),
    ):
        monkeypatch.setattr(RUN_COMMON, "run", lambda *_args, value=protocol: value)
        manifest = PREPARE_RUN.prepare(
            "test-org/test-repo",
            tmp_path / f"{protocol}-run",
            hub_repo=target,
            skip_github_checks=True,
        )
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
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    run_dir, _ = prepare(tmp_path, monkeypatch, bob_harness="codex", charlie_harness="codex")
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
        ("opencode", "opencode/space-bunny-free", None, "unknown"),
        ("opencode", "opencode/nemotron-3-ultra-free", None, "nvidia"),
        ("opencode", "opencode/nemotron-3.5-lightning-free", None, "nvidia"),
        ("opencode", "opencode/mimo-v2.6-flash-free", None, "xiaomi"),
        ("opencode", "opencode/longcat-2.5-preview-free", None, "meituan"),
        ("opencode", "opencode/ling-3.0-flash-fin-free", None, "inclusionai"),
        ("opencode", "opencode/lingma-x", None, "unknown"),
        ("opencode", "opencode/muse-spark-1.2", None, "meta"),
        ("opencode", "opencode/muse-spark-1.3", None, "meta"),
        ("opencode", "opencode/muse-spark-1.3-contributor-free", None, "meta"),
        ("opencode", "", None, "unknown"),
        ("antigravity", "gemini-3.1-pro-high", None, "google"),
        ("antigravity", "claude-sonnet-4-6", None, "anthropic"),
        ("antigravity", "gpt-oss-120b-medium", None, "openai"),
        ("antigravity", "", None, "unknown"),
        ("opencode", "opencode/big-pickle", "stealth-lab", "stealth-lab"),
        ("antigravity", "claude-sonnet-4-6", "custom-vendor", "custom-vendor"),
        # Devin routes several makers' models and its own SWE models (#224).
        ("devin", "swe-1.6-slow", None, "unknown"),
        ("devin", "", None, "unknown"),
        ("devin", "claude-opus-4.6", None, "anthropic"),
        ("devin", "codex", None, "openai"),
        ("devin", "swe-1.6-slow", "cognition", "cognition"),
    ],
)
def test_resolve_provider_records_model_maker_lineage(
    harness: str,
    model: str,
    provider: str | None,
    expected: str,
) -> None:
    assert RUN_COMMON.resolve_provider(harness, model, provider) == expected


def test_agy_home_links_cli_auth_without_operator_dotdirs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_user_home = tmp_path / "user-home"
    cli_dir = fake_user_home / ".gemini" / "antigravity-cli"
    cli_dir.mkdir(parents=True)
    (cli_dir / "antigravity-oauth-token").write_text('{"access_token":"linux"}\n')
    (cli_dir / "jetski_state.pbtxt").write_text("oauth_token: 'win'\n")
    (cli_dir / "settings.json").write_text('{"theme":"dark"}\n')
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


@pytest.mark.parametrize(
    ("winerror", "message"),
    [(1314, "missing symlink privilege"), (1142, "hard-link limit reached"), (17, "cross-volume")],
)
def test_link_credential_fallback_and_accurate_errors(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    winerror: int,
    message: str,
) -> None:
    source = tmp_path / "source-token"
    source.write_text("secret\n")
    target = tmp_path / "dest" / "token"
    target.parent.mkdir()

    def fail_link(*_args: Any, **_kwargs: Any) -> None:
        exc = WindowsLinkError("forced link failure")
        exc.winerror = winerror
        raise exc

    monkeypatch.setattr(Path, "symlink_to", fail_link)
    monkeypatch.setattr(os, "link", fail_link)
    with pytest.raises(ValueError, match=message) as error:
        RUN_COMMON.link_credential(source, target, "AGY_HOME")
    assert f"WinError {winerror}" in str(error.value)
    assert "--allow-credential-copy" in str(error.value)
    assert not target.exists()
    exposure = RUN_COMMON.link_credential(source, target, "AGY_HOME", allow_copy=True)
    assert exposure["method"] == "copy"
    assert message in exposure["hardlink_error"]
    assert target.read_text() == source.read_text()
    assert not target.samefile(source)
    if os.name != "nt":
        assert target.stat().st_mode & 0o777 == 0o600


def test_copy_failure_preserves_both_link_causes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "source"
    source.write_text("temporary test credential")
    target = tmp_path / "target"

    def fail_symlink(*args: Any, **kwargs: Any) -> None:
        exc = WindowsLinkError("privilege unavailable")
        exc.winerror = 1314
        raise exc

    def fail_hardlink(*args: Any, **kwargs: Any) -> None:
        exc = WindowsLinkError("too many links")
        exc.winerror = 1142
        raise exc

    def fail_copy(*args: Any, **kwargs: Any) -> None:
        raise PermissionError(13, "copy access denied")

    monkeypatch.setattr(Path, "symlink_to", fail_symlink)
    monkeypatch.setattr(os, "link", fail_hardlink)
    monkeypatch.setattr(shutil, "copyfileobj", fail_copy)
    with pytest.raises(ValueError) as error:
        RUN_COMMON.link_credential(source, target, "CODEX_HOME", allow_copy=True)
    assert "missing symlink privilege" in str(error.value)
    assert "hard-link limit reached" in str(error.value)
    assert "copy access denied" in str(error.value)
    assert not target.exists()


@pytest.mark.parametrize("code", [errno.EMLINK, errno.EXDEV, errno.EACCES])
def test_posix_link_diagnostics(code: int) -> None:
    expected = {
        errno.EMLINK: "hard-link limit reached",
        errno.EXDEV: "drive holding CODEX_HOME",
        errno.EACCES: "check source and destination permissions",
    }
    detail = RUN_COMMON.credential_error(OSError(code, "synthetic failure"), "CODEX_HOME")
    assert expected[code] in detail
    assert f"errno {code}" in detail


def test_hardlink_exposure_and_repreparation_preserve_login(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "auth.json"
    source.write_text("synthetic login")
    target = tmp_path / "run-auth.json"

    def fail_symlink(*args: Any, **kwargs: Any) -> None:
        exc = WindowsLinkError("no symlink privilege")
        exc.winerror = 1314
        raise exc

    monkeypatch.setattr(Path, "symlink_to", fail_symlink)
    exposure = RUN_COMMON.link_credential(source, target, "CODEX_HOME")
    assert exposure["method"] == "hardlink"
    assert "missing symlink privilege" in exposure["symlink_error"]
    assert target.samefile(source)
    assert RUN_COMMON.link_credential(source, target, "CODEX_HOME")["method"] == "hardlink"
    target.unlink()
    target.write_text("refreshed run login")
    assert RUN_COMMON.link_credential(source, target, "CODEX_HOME")["method"] == "copy"
    assert target.read_text() == "refreshed run login"


def test_preparation_with_no_links_reports_copies(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Native Windows acceptance uses only synthetic credentials and a fixture hub."""
    (Path(os.environ["CODEX_HOME"]) / "auth.json").write_text("test credential")
    agy = Path(os.environ["AGY_HOME"])
    cli = agy / ".gemini/antigravity-cli"
    cli.mkdir(parents=True)
    for filename in ("antigravity-oauth-token", "jetski_state.pbtxt", "settings.json"):
        (cli / filename).write_text("test credential")
    (agy / ".git-credentials").write_text("test credential")

    def fail_symlink(*args: Any, **kwargs: Any) -> None:
        exc = WindowsLinkError("forced missing privilege")
        exc.winerror = 1314
        raise exc

    def fail_hardlink(*args: Any, **kwargs: Any) -> None:
        exc = WindowsLinkError("forced link limit")
        exc.winerror = 1142
        raise exc

    monkeypatch.setattr(Path, "symlink_to", fail_symlink)
    monkeypatch.setattr(os, "link", fail_hardlink)
    _, manifest = prepare(
        tmp_path,
        monkeypatch,
        alice_harness="codex",
        bob_harness="antigravity",
        allow_credential_copy=True,
    )
    exposures = manifest["credential_exposure"]
    assert len(exposures) == 6
    assert {entry["method"] for entry in exposures.values()} == {"copy"}
    output = capsys.readouterr().out
    assert '"credential_exposure"' in output and '"method": "copy"' in output
    assert "test credential" not in output


async def test_opencode_and_antigravity_harnesses_render_configs_skills_and_scripts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from worker_mcp.orchestrator import OrchestratorBridge, create_orchestrator_mcp

    fake_agy_home = tmp_path / "user-home"
    cli_dir = fake_agy_home / ".gemini" / "antigravity-cli"
    cli_dir.mkdir(parents=True)
    (cli_dir / "antigravity-oauth-token").write_text('{"access_token":"fake"}\n')
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

    # AntiGravity Alice has an isolated home with linked CLI auth, skill, and all hub tools.
    alice_home = run_dir / "configs" / "alice-agy"
    assert (alice_home / ".gemini" / "antigravity-cli" / "antigravity-oauth-token").exists()
    assert not (alice_home / ".ssh").exists()
    alice_skill = Path("skills") / "alice-orchestrator" / "SKILL.md"
    assert (alice_home / ".gemini" / "config" / alice_skill).exists()
    assert (run_dir / "alice-runtime" / ".agents" / alice_skill).exists()
    alice_mcp = json.loads(RUN_COMMON.agy_mcp_path(alice_home).read_text(encoding="utf-8"))[
        "mcpServers"
    ]["robomate"]
    assert alice_mcp["args"][-3:] == ["mcp", "--role", "orchestrator"]
    assert alice_mcp["env"]["ROBOMATE_HUB_URL"] == "http://127.0.0.1:8521"
    assert alice_mcp["env"]["ROBOMATE_TOKEN_FILE"] == str(tmp_path / "target/.robomate/token")
    served = {
        tool.name for tool in await create_orchestrator_mcp(OrchestratorBridge()).list_tools()
    }
    assert "join" in served
    assert set(alice_mcp["enabledTools"]) == served
    assert alice_mcp["timeoutSeconds"] == 330

    # OpenCode Bob uses the robomate worker bridge with a 330000ms timeout and external_directory.
    bob_oc = json.loads((run_dir / "configs" / "bob.opencode.json").read_text(encoding="utf-8"))
    assert bob_oc["model"] == "opencode/claude-sonnet-4-6"
    bob_mcp = bob_oc["mcp"]["robomate"]
    assert bob_mcp["command"][-3:] == ["mcp", "--role", "worker"]
    assert bob_mcp["timeout"] == 330000
    assert bob_mcp["environment"]["ROBOMATE_HUB_URL"] == "http://127.0.0.1:8521"
    assert bob_mcp["environment"]["ROBOMATE_TOKEN_FILE"] == str(tmp_path / "target/.robomate/token")
    assert bob_mcp["environment"]["HUB_HARNESS"] == "opencode"
    assert bob_mcp["environment"]["HUB_HARNESS_VERSION"] == "1.18.32"
    assert bob_mcp["environment"]["HUB_PROVIDER"] == "anthropic"
    assert bob_oc["permission"]["external_directory"] == "allow"

    # AntiGravity Charlie has an isolated home and all 6 worker tools enabled on robomate.
    charlie_home = run_dir / "configs" / "charlie-agy"
    assert (charlie_home / ".gemini" / "antigravity-cli" / "antigravity-oauth-token").exists()
    charlie_mcp = json.loads(RUN_COMMON.agy_mcp_path(charlie_home).read_text(encoding="utf-8"))[
        "mcpServers"
    ]["robomate"]
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
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
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


def test_worker_only_backoff_options_reach_script_and_manifest(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """r1-2: a remote worker's launcher is configurable, not fixed at defaults."""
    source = origin(tmp_path)
    token = tmp_path / "token"
    token.write_text("secret\n")
    token.chmod(0o600)
    monkeypatch.setattr(
        PREPARE_RUN, "probe_versions", lambda _: ({"claude": "2.1"}, {"claude-code": "2.1"})
    )
    worker_run = (tmp_path / "worker-run").resolve()
    manifest = PREPARE_RUN.prepare_worker(
        "bob",
        str(source),
        worker_run,
        "http://192.0.2.10:8420",
        token,
        "claude-code",
        max_resumes=7,
        resume_delay_s=1.5,
        resume_max_delay_s=60,
        resume_total_s=3600,
        resume_series_reset_s=30,
    )
    start = (worker_run / "start-bob.sh").read_text()
    for flag in (
        "--max-resumes 7",
        "--resume-delay-s 1.5",
        "--resume-max-delay-s 60",
        "--resume-total-s 3600",
        "--resume-series-reset-s 30",
    ):
        assert flag in start
    agent = manifest["launch"]["agents"]["bob"]
    assert (
        f"`{(worker_run / 'closeout/bob.md').as_posix()}`"
        in (worker_run / "bob.prompt.md").read_text()
    )
    assert (worker_run / "closeout").is_dir()
    assert agent["max_resumes"] == 7
    assert agent["resume_delay_s"] == 1.5
    assert agent["resume_max_delay_s"] == 60
    assert agent["resume_total_s"] == 3600
    assert agent["resume_series_reset_s"] == 30


def test_opencode_effort_with_no_auto_start_is_rejected_before_run_dir(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
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
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tmp_path = tmp_path / "spaced directory"
    tmp_path.mkdir()
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
            assert lines[2] == f"export TMPDIR={PREPARE_RUN.shell_word(str(agent_tmp))}"
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
        line.strip() for line in script.splitlines() if line.strip() and not line.startswith("#")
    ]
    assert lines[0] == "set -e"
    assert lines[1].startswith("cd ")
    bash_tmp = PREPARE_RUN.git_bash_path(bob_tmp) if host == "nt" else str(bob_tmp)
    assert lines[2] == f"export TMPDIR={PREPARE_RUN.shell_word(bash_tmp)}"

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
        RUN_COMMON.parse_github_slug("git@gitlab.com:group/subgroup/project.git", forge="gitlab")
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
    ) -> Callable[..., tuple[str, str]]:
        def runner(*args: str, **kwargs: Any) -> tuple[str, str]:
            calls.append(list(args))
            assert args[0] != "gh", f"gh command unexpectedly invoked: {args}"
            if args[0] != "glab":
                passthrough: tuple[str, str] = RUN_COMMON.run_output(*args, **kwargs)
                return passthrough
            if list(args) == ["glab", "version"]:
                return ("glab version 1.36.0 (build)", "")
            if list(args[1:4]) == ["api", "--hostname", "gitlab-box.local"]:
                endpoint = args[4]
                if endpoint == "user":
                    if user_data is not None:
                        return (json.dumps(user_data), "")
                    return (json.dumps({"username": "testuser"}), "")
                if endpoint.startswith(
                    "projects/RoboNater%2Frobomate-glab-sandbox/repository/files"
                ):
                    if ci_error is not None:
                        raise ci_error
                    if not ci_found:
                        raise subprocess.CalledProcessError(
                            1, list(args), output="404 File Not Found", stderr="(HTTP 404)"
                        )
                    return (json.dumps({"file_name": ".gitlab-ci.yml"}), "")
                if endpoint == "projects/RoboNater%2Frobomate-glab-sandbox":
                    if project_data is not None:
                        return (json.dumps(project_data), "")
                    return (json.dumps(GL_BASE_PROJECT), "")
            raise AssertionError(f"unexpected call: {args}")

        return runner

    # 1. Auth failure
    monkeypatch.setattr(
        PREPARE_RUN, "run_output", make_runner(user_data={"message": "401 Unauthorized"})
    )
    with pytest.raises(ValueError, match="glab is not authenticated"):
        PREPARE_RUN.prepare(GL_ORIGIN, tmp_path / "run1", hub_repo=target)

    # 2. Project unreadable
    monkeypatch.setattr(
        PREPARE_RUN, "run_output", make_runner(project_data={"message": "404 Project Not Found"})
    )
    with pytest.raises(ValueError, match="cannot read project settings"):
        PREPARE_RUN.prepare(GL_ORIGIN, tmp_path / "run2", hub_repo=target)

    # 3. Web URL mismatch
    bad_web = {**GL_BASE_PROJECT, "web_url": "https://other-box.local/RoboNater/robomate"}
    monkeypatch.setattr(PREPARE_RUN, "run_output", make_runner(project_data=bad_web))
    with pytest.raises(ValueError, match="differs from expected"):
        PREPARE_RUN.prepare(GL_ORIGIN, tmp_path / "run3", hub_repo=target)

    # 4. Developer access missing (access_level 20 < 30)
    low_perm = {
        **GL_BASE_PROJECT,
        "permissions": {"project_access": {"access_level": 20}},
    }
    monkeypatch.setattr(PREPARE_RUN, "run_output", make_runner(project_data=low_perm))
    with pytest.raises(ValueError, match="requires developer access"):
        PREPARE_RUN.prepare(GL_ORIGIN, tmp_path / "run4", hub_repo=target)

    # 5. Incompatible merge method
    incompatible = {
        **GL_BASE_PROJECT,
        "merge_method": "merge",
        "squash_option": "always",
    }
    monkeypatch.setattr(PREPARE_RUN, "run_output", make_runner(project_data=incompatible))
    with pytest.raises(ValueError, match="does not allow the 'merge' merge method"):
        PREPARE_RUN.prepare(GL_ORIGIN, tmp_path / "run5", hub_repo=target, merge_method="merge")

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
    monkeypatch.setattr(PREPARE_RUN, "run_output", make_runner(ci_found=False))
    run_dir6 = tmp_path / "run6"
    manifest6 = PREPARE_RUN.prepare(
        GL_ORIGIN, run_dir6, hub_repo=target, issue=12, account="testuser"
    )
    assert manifest6["policy"]["allow_no_ci"] is True
    assert manifest6["forge"] == "gitlab"

    # 6b. CI presence - network/5xx error refuses instead of failing open
    monkeypatch.setattr(
        PREPARE_RUN,
        "run_output",
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
    monkeypatch.setattr(PREPARE_RUN, "run_output", make_runner(ci_found=True))
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


def gitlab_hub_repo_no_origin(tmp_path: Path, url: str = "http://127.0.0.1:8521") -> Path:
    repo = tmp_path / "gl-target-no-origin"
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
            }
        )
    )
    return repo


def test_gitlab_no_origin_refuses_with_remedy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = gitlab_hub_repo_no_origin(tmp_path)
    monkeypatch.setattr(PREPARE_RUN, "hub_healthy", lambda *_: True)
    monkeypatch.setattr(
        PREPARE_RUN,
        "probe_versions",
        lambda _: ({"claude": "2", "codex": "1"}, {"claude-code": "2", "codex": "1"}),
    )
    with pytest.raises(ValueError, match="restart hub with robomate up which records origin"):
        PREPARE_RUN.prepare(GL_ORIGIN, tmp_path / "run-no-origin", hub_repo=target)


@pytest.mark.parametrize(
    "hub_origin,repository",
    [
        (
            "https://gitlab-box.local/RoboNater/robomate-glab-sandbox.git",
            "git@gitlab-box.local:RoboNater/robomate-glab-sandbox.git",
        ),
        (
            "git@gitlab-box.local:RoboNater/robomate-glab-sandbox.git",
            "https://gitlab-box.local/RoboNater/robomate-glab-sandbox.git",
        ),
    ],
)
def test_gitlab_origin_match_incl_ssh_https(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, hub_origin: str, repository: str
) -> None:
    target = gitlab_hub_repo(tmp_path, origin_url=hub_origin)
    monkeypatch.setattr(PREPARE_RUN, "hub_healthy", lambda *_: True)
    monkeypatch.setattr(
        PREPARE_RUN,
        "probe_versions",
        lambda _: ({"claude": "2", "codex": "1"}, {"claude-code": "2", "codex": "1"}),
    )
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

    def runner(*args: str, **kwargs: Any) -> tuple[str, str]:
        assert args[0] != "gh", f"gh command unexpectedly invoked: {args}"
        if args[0] != "glab":
            passthrough: tuple[str, str] = RUN_COMMON.run_output(*args, **kwargs)
            return passthrough
        if list(args) == ["glab", "version"]:
            return ("glab version 1.36.0 (build)", "")
        assert list(args[1:4]) == ["api", "--hostname", "gitlab-box.local"]
        endpoint = args[4]
        if endpoint == "user":
            return (json.dumps({"username": "testuser"}), "")
        if "repository/files" in endpoint:
            return (json.dumps({"file_name": ".gitlab-ci.yml"}), "")
        assert endpoint == "projects/RoboNater%2Frobomate-glab-sandbox"
        return (json.dumps(GL_BASE_PROJECT), "")

    monkeypatch.setattr(PREPARE_RUN, "run_output", runner)
    manifest = PREPARE_RUN.prepare(repository, tmp_path / "run-match", hub_repo=target)
    assert manifest["forge"] == "gitlab"
    assert manifest["policy"]["allow_no_ci"] is False


@pytest.mark.parametrize("project_method", ["merge", "rebase_merge", "ff"])
@pytest.mark.parametrize("squash", ["default_on", "default_off", "always", "never"])
@pytest.mark.parametrize("policy", ["merge", "squash", "rebase"])
def test_gitlab_merge_matrix_same_as_before(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    project_method: str,
    squash: str,
    policy: str,
) -> None:
    from agent_hub.gitlab_gate import check_merge_compatibility

    target = gitlab_hub_repo(tmp_path)
    monkeypatch.setattr(PREPARE_RUN, "hub_healthy", lambda *_: True)
    monkeypatch.setattr(
        PREPARE_RUN,
        "probe_versions",
        lambda _: ({"claude": "2", "codex": "1"}, {"claude-code": "2", "codex": "1"}),
    )
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
    project_data = {
        **GL_BASE_PROJECT,
        "merge_method": project_method,
        "squash_option": squash,
    }

    def runner(*args: str, **kwargs: Any) -> tuple[str, str]:
        assert args[0] != "gh", f"gh command unexpectedly invoked: {args}"
        if args[0] != "glab":
            passthrough: tuple[str, str] = RUN_COMMON.run_output(*args, **kwargs)
            return passthrough
        if list(args) == ["glab", "version"]:
            return ("glab version 1.36.0 (build)", "")
        assert list(args[1:4]) == ["api", "--hostname", "gitlab-box.local"]
        endpoint = args[4]
        if endpoint == "user":
            return (json.dumps({"username": "testuser"}), "")
        if "repository/files" in endpoint:
            return (json.dumps({"file_name": ".gitlab-ci.yml"}), "")
        assert endpoint == "projects/RoboNater%2Frobomate-glab-sandbox"
        return (json.dumps(project_data), "")

    monkeypatch.setattr(PREPARE_RUN, "run_output", runner)
    should_refuse = bool(check_merge_compatibility(project_data, policy))
    if should_refuse:
        with pytest.raises(ValueError, match="does not allow the"):
            PREPARE_RUN.prepare(
                GL_ORIGIN, tmp_path / "run-matrix", hub_repo=target, merge_method=policy
            )
    else:
        manifest = PREPARE_RUN.prepare(
            GL_ORIGIN, tmp_path / "run-matrix", hub_repo=target, merge_method=policy
        )
        assert manifest["policy"]["merge_method"] == policy


@pytest.mark.parametrize("allow", ["true", "false"])
def test_gitlab_explicit_allow_no_ci_makes_no_ci_call(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, allow: str
) -> None:
    target = gitlab_hub_repo(tmp_path)
    monkeypatch.setattr(PREPARE_RUN, "hub_healthy", lambda *_: True)
    monkeypatch.setattr(
        PREPARE_RUN,
        "probe_versions",
        lambda _: ({"claude": "2", "codex": "1"}, {"claude-code": "2", "codex": "1"}),
    )
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
    calls: list[list[str]] = []

    def runner(*args: str, **kwargs: Any) -> tuple[str, str]:
        calls.append(list(args))
        assert args[0] != "gh", f"gh command unexpectedly invoked: {args}"
        if args[0] != "glab":
            passthrough: tuple[str, str] = RUN_COMMON.run_output(*args, **kwargs)
            return passthrough
        if list(args) == ["glab", "version"]:
            return ("glab version 1.36.0 (build)", "")
        assert list(args[1:4]) == ["api", "--hostname", "gitlab-box.local"]
        endpoint = args[4]
        if endpoint == "user":
            return (json.dumps({"username": "testuser"}), "")
        if "repository/files" in endpoint:
            raise AssertionError("CI file must not be probed with explicit allow_no_ci")
        assert endpoint == "projects/RoboNater%2Frobomate-glab-sandbox"
        return (json.dumps(GL_BASE_PROJECT), "")

    monkeypatch.setattr(PREPARE_RUN, "run_output", runner)
    manifest = PREPARE_RUN.prepare(
        GL_ORIGIN, tmp_path / f"run-explicit-{allow}", hub_repo=target, allow_no_ci=allow
    )
    assert manifest["policy"]["allow_no_ci"] is (allow == "true")
    assert not [c for c in calls if "repository/files" in " ".join(c)]


@pytest.mark.parametrize(
    "setting",
    [
        "automatic_rebase_enabled",
        "merge_trains_enabled",
        "merge_train_enforcement",
        "merge_pipelines_enabled",
    ],
)
def test_gitlab_unsupported_settings_refuse(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, setting: str
) -> None:
    """Base `Unsupported settings: refuse` must refuse prepare-run too (r1-1)."""

    target = gitlab_hub_repo(tmp_path)
    monkeypatch.setattr(PREPARE_RUN, "hub_healthy", lambda *_: True)
    monkeypatch.setattr(
        PREPARE_RUN,
        "probe_versions",
        lambda _: ({"claude": "2", "codex": "1"}, {"claude-code": "2", "codex": "1"}),
    )
    project_data = {**GL_BASE_PROJECT, setting: True}

    def runner(*args: str, **kwargs: Any) -> tuple[str, str]:
        assert args[0] != "gh", f"gh command unexpectedly invoked: {args}"
        if args[0] != "glab":
            passthrough: tuple[str, str] = RUN_COMMON.run_output(*args, **kwargs)
            return passthrough
        if list(args) == ["glab", "version"]:
            return ("glab version 1.36.0 (build)", "")
        assert list(args[1:4]) == ["api", "--hostname", "gitlab-box.local"]
        endpoint = args[4]
        if endpoint == "user":
            return (json.dumps({"username": "testuser"}), "")
        assert endpoint == "projects/RoboNater%2Frobomate-glab-sandbox"
        return (json.dumps(project_data), "")

    monkeypatch.setattr(PREPARE_RUN, "run_output", runner)
    with pytest.raises(ValueError, match="Disable"):
        PREPARE_RUN.prepare(GL_ORIGIN, tmp_path / f"run-unsupported-{setting}", hub_repo=target)


def test_opencode_title_is_stable_and_names_the_agent() -> None:
    """The OpenCode session title depends only on the agent and run directory (#94)."""
    assert PREPARE_RUN.opencode_title("alice", PurePosixPath("/srv/runs/step7")) == "alice step7"
    assert PREPARE_RUN.opencode_title("bob", PureWindowsPath("C:/runs/step7")) == "bob step7"


@pytest.mark.parametrize("powershell", [False, True])
def test_opencode_launch_sets_title_only_with_auto_start(powershell: bool) -> None:
    command = " ".join(
        PREPARE_RUN.opencode_launch(
            "cfg", "prompt", ["--model", "m"], True, "alice my-run", powershell
        )
    )
    assert "--title" in command and "alice my-run" in command
    untitled = " ".join(
        PREPARE_RUN.opencode_launch("cfg", "prompt", ["--model", "m"], True, None, powershell)
    )
    assert "--title" not in untitled
    manual = " ".join(
        PREPARE_RUN.opencode_launch("cfg", "prompt", [], False, "alice my-run", powershell)
    )
    assert "--title" not in manual and "--session" not in manual


@pytest.mark.parametrize(
    "harness,marker",
    [
        ("claude-code", "--resume"),
        ("codex", "resume"),
        ("opencode", "--session"),
        ("antigravity", "--conversation"),
    ],
)
def test_resume_lines_resume_each_harness_by_id(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, harness: str, marker: str
) -> None:
    """Every harness's resume script takes the saved ID and never reuses the kickoff (#94)."""
    monkeypatch.setattr(os, "name", "posix")
    run_dir = (tmp_path / "my run").resolve()
    lines = PREPARE_RUN.resume_lines(
        "bob",
        harness,
        run_dir / "bob",
        run_dir / "configs" / "bob",
        run_dir / "resume-bob.prompt.md",
        run_dir / "bob" / ".git",
        "model-x",
        "high",
        True,
        tmp_dir=run_dir / "tmp" / "bob",
        title="bob my run",
        token_file=run_dir / "token",
    )
    text = "\n".join(lines)
    assert "--recover" in text and '"$@"' in text
    assert "set -- --resume-session" in text
    assert "bob.prompt.md" not in text.replace("resume-bob.prompt.md", "")
    if harness == "opencode":
        assert "--title" in text and "bob my run" in text
    else:
        assert "--title" not in text


def test_resume_lines_windows_powershell(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(os, "name", "nt")
    run_dir = (tmp_path / "run").resolve()
    lines = PREPARE_RUN.resume_lines(
        "alice",
        "opencode",
        run_dir / "alice-runtime",
        run_dir / "configs" / "alice.opencode.json",
        run_dir / "resume-alice.prompt.md",
        None,
        "opencode/gemini-3.8-flash",
        "high",
        True,
        tmp_dir=run_dir / "tmp" / "alice",
        title="alice run",
        token_file=run_dir / "token",
    )
    text = "\n".join(lines)
    assert "$env:OPENCODE_CONFIG =" in text
    assert "--recover" in text and "@ResumeArgs" in text
    assert "--title 'alice run'" in text
    assert "exit $LASTEXITCODE" in text
    assert "alice.prompt.md" not in text.replace("resume-alice.prompt.md", "")


def test_worker_resume_uses_git_bash_spellings() -> None:
    run = PureWindowsPath("C:/Users/Bob/runs/step7")
    lines = PREPARE_RUN.worker_resume(
        "bob",
        "opencode",
        run,
        run / "bob",
        "m",
        "high",
        title="bob step7",
        token_file="C:/private/token",
    )
    assert lines[0] == "cd /c/Users/Bob/runs/step7/bob"
    assert any("--recover" in line and '"$@"' in line and "--title" in line for line in lines)
    assert any("resume-bob.prompt.md" in line for line in lines)


def test_resume_prompts_use_role_specific_first_hub_call() -> None:
    """#239 r2-2: Alice replaces her bridge before state reads; workers check in."""
    from agent_hub_common.orchestrator_tools import OPERATIONS

    assert "check_in" not in OPERATIONS
    assert "check_in" in RUN_COMMON.TOOLS
    alice = PREPARE_RUN.render_resume_prompt("alice", PurePosixPath("/runs/r"))
    assert alice.index("`join`") < alice.index("`get_state`")
    assert 'role="orchestrator"' in alice and "`takeover=true`" in alice
    assert "returned orchestrator guide's On resume" in alice
    assert "grants no workflow" in alice and "call `check_in`" not in alice
    for name in ("bob", "charlie"):
        assert "call `check_in`" in PREPARE_RUN.render_resume_prompt(name, PurePosixPath("/runs/r"))


def test_opencode_run_records_title_and_resume_artifacts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """run.json records the OpenCode title and resume files, and scripts use them (#94)."""
    run_dir, manifest = prepare(
        tmp_path, monkeypatch, alice_harness="opencode", bob_harness="opencode"
    )
    slug = run_dir.name
    for name in ("alice", "bob"):
        agent = manifest["launch"]["agents"][name]
        assert agent["title"] == f"{name} {slug}"
        assert agent["resume_script"] == str(run_dir / f"resume-{name}.{SCRIPT_SUFFIX}")
        assert agent["resume_prompt"] == str(run_dir / f"resume-{name}.prompt.md")
        assert (run_dir / f"resume-{name}.{SCRIPT_SUFFIX}").exists()
        assert (run_dir / f"resume-{name}.prompt.md").exists()
        start = (run_dir / f"start-{name}.{SCRIPT_SUFFIX}").read_text(encoding="utf-8")
        assert "--title" in start and f"{name} {slug}" in start
        resume = (run_dir / f"resume-{name}.{SCRIPT_SUFFIX}").read_text(encoding="utf-8")
        # Alice's launcher adds `--session` itself, from its --resume-session (#146).
        assert "--recover" in resume
        assert f"resume-{name}.prompt.md" in resume
        kickoff = f"Read {run_dir / f'{name}.prompt.md'} and follow the instructions in it"
        assert kickoff not in resume
    charlie = manifest["launch"]["agents"]["charlie"]
    assert "title" not in charlie
    assert (run_dir / f"resume-charlie.{SCRIPT_SUFFIX}").exists()
    alice_resume = (run_dir / "resume-alice.prompt.md").read_text(encoding="utf-8")
    assert "initialize_workflow" in alice_resume and "start-alice.sh" in alice_resume
    bob_resume = (run_dir / "resume-bob.prompt.md").read_text(encoding="utf-8")
    assert "check_in" in bob_resume and "start-bob.sh" in bob_resume


# -- Alice's launcher (#146) ---------------------------------------------------------

# A stand-in for each harness CLI: it logs its arguments, standard input and
# the environment the start script set, prints its conversation ID the way the
# real CLI does, and exits as an early-exiting headless Alice would.
FAKE_ALICE = """\
import json, os, sys
argv = sys.argv[1:]
stdin = sys.stdin.read() if argv and argv[-1] == "-" else None
env = {k: os.environ.get(k) for k in ("CODEX_HOME", "OPENCODE_CONFIG", "HOME", "TMPDIR", "TEMP")}
with open(os.environ["FAKE_ARGV_LOG"], "a", encoding="utf-8") as log:
    log.write(json.dumps({"argv": argv, "stdin": stdin, "env": env, "cwd": os.getcwd()}) + "\\n")
if "--export" in argv:
    # Devin's ATIF export after each turn carries its session ID (#224).
    with open(argv[argv.index("--export") + 1], "w", encoding="utf-8") as stream:
        json.dump({"schema_version": "ATIF-v1.2", "session_id": "brisk-otter"}, stream)
print(os.environ.get("FAKE_ID_LINE", ""), flush=True)
"""
FAKE_ID_LINES = {
    "claude-code": "",
    "codex": "session id: 019a2b3c-4d5e-6f70-8192-a3b4c5d6e7f8",
    "opencode": json.dumps({"type": "step_start", "sessionID": "ses_7Fa9b2C"}),
    "antigravity": json.dumps({"type": "init", "conversation_id": "conv-1234abcd"}),
    "devin": "",
}
CLI_NAMES = {
    "claude-code": "claude",
    "codex": "codex",
    "opencode": "opencode",
    "antigravity": "agy",
    "devin": "devin",
}


class _StubHub(BaseHTTPRequestHandler):
    """hub.info and hub.status as the run hub answers them, statuses in order."""

    statuses: list[str] = []
    calls: list[tuple[str, str | None]] = []
    current_status = "active"

    def do_POST(self) -> None:  # noqa: N802 - the stdlib's name
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        self.calls.append((body["method"], self.headers.get("X-Robomate-Session")))
        if body["method"] == "hub.info":
            result: dict[str, Any] = {"hub_id": "abc"}
        elif body["method"] == "hub.questions":
            result = {"questions": []}
        elif body["method"] == "hub.snapshot":
            result = {
                "hub_id": "abc",
                "taken_at": "2026-10-10T00:00:00Z",
                "workflow": {"id": "wf-stub", "status": self.current_status},
                "tasks": [],
                "deliveries": [],
                "queued_events": 0,
            }
        else:
            status = self.statuses.pop(0) if len(self.statuses) > 1 else self.statuses[0]
            type(self).current_status = status
            result = {
                "workflow": {"id": "wf-stub", "status": status, "headline": "h"},
                "agents": [],
                "orchestrator_activity": {"holding": None, "heartbeat_age_s": None},
            }
        payload = json.dumps({"jsonrpc": "2.0", "id": 1, "result": result}).encode()
        self.send_response(200)
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, *_: Any) -> None:
        pass


@contextmanager
def stub_hub(*statuses: str) -> Iterator[str]:
    _StubHub.statuses = list(statuses)
    _StubHub.calls = []
    server = ThreadingHTTPServer(("127.0.0.1", 0), _StubHub)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        server.server_close()


def run_script(script: Path, bin_dir: Path, env: dict[str, str], *args: str) -> str:
    if os.name == "nt":
        shell = shutil.which("pwsh") or shutil.which("powershell")
        assert shell is not None
        command = [shell, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass"]
        command += ["-File", str(script), *args]
    else:
        command = ["bash", str(script), *args]
    completed = subprocess.run(
        command, env=env, capture_output=True, text=True, timeout=120, check=False
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
    return completed.stderr


def fake_cli(bin_dir: Path, harness: str) -> None:
    bin_dir.mkdir(exist_ok=True)
    fake = bin_dir / "fake_alice.py"
    fake.write_text(FAKE_ALICE, encoding="utf-8")
    name = CLI_NAMES[harness]
    if os.name == "nt":
        (bin_dir / f"{name}.cmd").write_text(f'@"{sys.executable}" "{fake}" %*\r\n')
    else:
        cli = bin_dir / name
        cli.write_text(f"#!{sys.executable}\n{FAKE_ALICE}", encoding="utf-8")
        cli.chmod(0o755)


@pytest.mark.parametrize("harness", ["claude-code", "codex", "opencode", "antigravity", "devin"])
@pytest.mark.parametrize("agent", ["alice", "bob", "charlie"])
def test_start_and_resume_scripts_run_the_launcher_to_done(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, harness: str, agent: str
) -> None:
    """The generated scripts, run by their own shell, with each harness stood in (#146)."""

    base = tmp_path / "with space"
    base.mkdir()
    bin_dir = base / "bin"
    fake_cli(bin_dir, harness)
    env = os.environ | {
        "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
        "FAKE_ARGV_LOG": str(base / "argv.jsonl"),
        "FAKE_ID_LINE": FAKE_ID_LINES[harness],
    }
    # start-alice reads before launching, after its first exit and again
    # after the resume delay, then after its second exit; resume-alice reads
    # before its launch and after it.
    statuses = (
        ["active"] * 3 + ["done", "escalated", "escalated", "done"]
        if agent == "alice"
        else ["active"] * 5 + ["done", "escalated", "escalated", "escalated", "done"]
    )
    with stub_hub(*statuses) as url:
        run_dir, manifest = prepare(
            base,
            monkeypatch,
            test_hub_url=url,
            **{
                f"{agent}_harness": harness,
                f"{agent}_model": "model-x",
                f"{agent}_effort": "high" if harness not in ("opencode", "devin") else "",
            },
            max_resumes=3,
            resume_delay_s=0,
        )
        assert manifest["launch"]["agents"][agent]["max_resumes"] == 3
        run_script(run_dir / f"start-{agent}.{SCRIPT_SUFFIX}", bin_dir, env)
        started = [json.loads(line) for line in (base / "argv.jsonl").read_text().splitlines()]
        (base / "argv.jsonl").unlink()
        # #209: exercises a real, just-written exit record. A backward wall-clock
        # step that makes it look 1-2s future-dated is tolerated by
        # agent_recovery.timestamp() for the recorded harness exit.
        run_script(run_dir / f"resume-{agent}.{SCRIPT_SUFFIX}", bin_dir, env)
        resumed = [json.loads(line) for line in (base / "argv.jsonl").read_text().splitlines()]
        # Bearer-only reads: the launcher never presents an orchestrator session.
        assert {session for _, session in _StubHub.calls} == {None}
    assert len(started) == 2
    records = [
        json.loads(line) for line in (run_dir / f"{agent}-sessions.jsonl").read_text().splitlines()
    ]
    stops = [record for record in records if record["event"] == "stop"]
    assert [stop["reason"] for stop in stops] == ["done", "done"]
    conversation = stops[0]["conversation_id"]
    assert conversation
    configs = run_dir / "configs"
    for run in [*started, *resumed]:
        # Model, working directory, temporary directory and isolated config
        # are the same on every launch.
        assert "model-x" in run["argv"]
        assert (
            Path(run["cwd"]).resolve()
            == ((run_dir / "alice-runtime") if agent == "alice" else run_dir / agent).resolve()
        )
        tmp = run["env"]["TEMP" if os.name == "nt" else "TMPDIR"]
        assert Path(tmp) == run_dir / "tmp" / agent
        if harness == "codex":
            assert Path(run["env"]["CODEX_HOME"]) == configs / PREPARE_RUN.codex_home_name(agent)
        elif harness == "opencode":
            assert Path(run["env"]["OPENCODE_CONFIG"]) == configs / f"{agent}.opencode.json"
        elif harness == "antigravity":
            assert Path(run["env"]["HOME"]) == configs / f"{agent}-agy"
        elif harness == "devin":
            # Its server is the local scope of the directory it starts in.
            local = json.loads((Path(run["cwd"]) / ".devin/mcp_config.local.json").read_text())
            role = "orchestrator" if agent == "alice" else "worker"
            assert local["mcpServers"]["robomate"]["args"][-2:] == ["--role", role]
            assert run["argv"][run["argv"].index("--permission-mode") + 1] == "dangerous"
            export = Path(run["argv"][run["argv"].index("--export") + 1])
            assert export == run_dir / f"{agent}-devin-export.json"
    if harness == "devin":
        assert conversation == "brisk-otter"
    for script in (f"start-{agent}", f"resume-{agent}"):
        body = (run_dir / f"{script}.{SCRIPT_SUFFIX}").read_text()
        assert ("alice-launcher.py" if agent == "alice" else "worker-launcher.py") in body
    prompt_body = (run_dir / f"resume-{agent}.prompt.md").read_text()
    if agent == "alice":
        assert "workflow is `done`" in prompt_body and "`wait_for_event`" in prompt_body
    else:
        assert "Every reply contains a tool call" in prompt_body
        assert "`await_assignment` returns `release: true`" in prompt_body
        assert "Do not end your turn until" in (run_dir / f"{agent}.prompt.md").read_text()
    assert conversation in started[1]["argv"]
    continuation = started[1]["stdin"] if harness == "codex" else started[1]["argv"][-1]
    if agent == "alice":
        assert continuation == ALICE_LAUNCHER.CONTINUE_PROMPT
    else:
        assert "call check_in once" in continuation
        assert "await_assignment returns release: true" in continuation
    kickoff = started[0]["stdin"] if harness == "codex" else started[0]["argv"][-1]
    assert kickoff != continuation and "RUN_DIR" not in kickoff
    # resume-alice resumes the conversation it is given, with the manual prompt.
    assert len(resumed) == 1 and conversation in resumed[0]["argv"]
    assert "Before-snapshot (facts" in prompt_body
    assert "recorded harness exit" in prompt_body
    manual = resumed[0]["stdin"] if harness == "codex" else resumed[0]["argv"][-1]
    if harness == "codex":
        assert manual.startswith(f"# Resume {'Alice' if agent == 'alice' else agent}")
    else:
        assert (
            manual
            == f"Read {run_dir / f'resume-{agent}.prompt.md'} and follow the instructions in it"
        )


def test_no_auto_start_keeps_alice_interactive_without_the_launcher(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_dir, manifest = prepare(tmp_path, monkeypatch, auto_start=False)
    for script in ("start-alice", "resume-alice"):
        assert "alice-launcher.py" not in (run_dir / f"{script}.{SCRIPT_SUFFIX}").read_text()
    assert "sessions" not in manifest["launch"]["agents"]["alice"]


@pytest.mark.parametrize("harness", ["claude-code", "codex", "opencode", "antigravity", "devin"])
@pytest.mark.parametrize("agent", ["alice", "bob", "charlie"])
def test_interactive_recovery_preserves_cli_with_explicit_missing_log_fallback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    harness: str,
    agent: str,
) -> None:
    """Run the actual guarded interactive wrapper on Linux/native PowerShell CI."""
    bin_dir = tmp_path / "bin"
    fake_cli(bin_dir, harness)
    env = os.environ | {
        "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
        "FAKE_ARGV_LOG": str(tmp_path / "argv.jsonl"),
    }
    conversation = {
        "claude-code": "019a2b3c-4d5e-6f70-8192-a3b4c5d6e7f8",
        "codex": "019a2b3c-4d5e-6f70-8192-a3b4c5d6e7f8",
        "opencode": "ses_7Fa9b2C",
        "antigravity": "conv-1234abcd",
        "devin": "brisk-otter",
    }[harness]
    with stub_hub("active") as url:
        run_dir, _ = prepare(
            tmp_path,
            monkeypatch,
            test_hub_url=url,
            auto_start=False,
            **{f"{agent}_harness": harness, f"{agent}_model": "model-x"},
        )
        output = run_script(
            run_dir / f"resume-{agent}.{SCRIPT_SUFFIX}",
            bin_dir,
            env,
            conversation,
            "--workflow-id",
            "wf-stub",
            "--confirm-stopped",
            "--stopped-at",
            "2026-10-08T12:34:56Z",
        )
    run = json.loads((tmp_path / "argv.jsonl").read_text().strip())
    assert conversation in run["argv"] and "model-x" in run["argv"]
    assert "-p" not in run["argv"] and "exec" not in run["argv"]
    assert "Interactive recovery" in output
    prompt = (run_dir / f"resume-{agent}.prompt.md").read_text()
    assert "operator-supplied" in prompt and "Before-snapshot" in prompt


@pytest.mark.parametrize("remote", [False, True], ids=["local", "remote-bash"])
@pytest.mark.parametrize("force", [False, True], ids=["refusal", "override"])
def test_recovery_controls_refuse_pause_or_reconcile_with_override(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    remote: bool,
    force: bool,
) -> None:
    bin_dir = tmp_path / "bin"
    fake_cli(bin_dir, "codex")
    env = os.environ | {
        "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
        "FAKE_ARGV_LOG": str(tmp_path / "argv.jsonl"),
    }
    with stub_hub("paused") as url:
        run_dir, _ = prepare(tmp_path, monkeypatch, test_hub_url=url, bob_harness="codex")
        controls = [
            "--resume-session",
            "019a2b3c-4d5e-6f70-8192-a3b4c5d6e7f8",
            "--hub-id",
            "abc",
            "--workflow-id",
            "wf-stub",
            "--confirm-stopped",
        ]
        if force:
            controls.append("--force")
        script = run_dir / f"resume-bob.{SCRIPT_SUFFIX}"
        if remote:
            lines = PREPARE_RUN.worker_resume(
                "bob",
                "codex",
                run_dir,
                run_dir / "bob",
                hub_url=url,
                token_file=str(run_dir / "token"),
            )
            (run_dir / "token").write_text("stub-token\n")
            script = run_dir / "remote-resume-bob.sh"
            PREPARE_RUN.write_script(script, PREPARE_RUN.resume_script(lines))
        shell = (
            [importlib.import_module("agent_recovery").bash_executable()]
            if remote or os.name != "nt"
            else [
                shutil.which("pwsh") or shutil.which("powershell") or "powershell",
                "-NoProfile",
                "-File",
            ]
        )
        script_arg = PREPARE_RUN.git_bash_path(script) if remote else str(script)
        completed = subprocess.run(
            [*shell, script_arg, *controls], env=env, text=True, capture_output=True, timeout=30
        )
        # Force runs one reconciliation turn; automatic continuation then respects pause.
        assert completed.returncode == 5, completed.stdout + completed.stderr
        assert "paused" in completed.stderr.lower() and "operator" in completed.stderr
        assert (tmp_path / "argv.jsonl").exists() == force
        assert "--force" in completed.stderr or "Advisory override" in completed.stderr


def test_stall_and_resume_options_reach_policy_and_scripts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_dir, manifest = prepare(
        tmp_path,
        monkeypatch,
        stall_after_min=2,
        max_resumes=0,
        resume_delay_s=1.5,
        resume_max_delay_s=60,
        resume_total_s=3600,
        resume_series_reset_s=30,
    )
    assert manifest["policy"]["stall_after_min"] == 2
    assert '"stall_after_min": 2' in (run_dir / "alice.prompt.md").read_text()
    start = (run_dir / f"start-alice.{SCRIPT_SUFFIX}").read_text()
    assert "--max-resumes 0" in start
    assert "--resume-delay-s 1.5" in start
    assert "--resume-max-delay-s 60" in start
    assert "--resume-total-s 3600" in start
    assert "--resume-series-reset-s 30" in start
    alice = manifest["launch"]["agents"]["alice"]
    assert alice["max_resumes"] == 0
    assert alice["resume_delay_s"] == 1.5
    assert alice["resume_max_delay_s"] == 60
    assert alice["resume_total_s"] == 3600
    assert alice["resume_series_reset_s"] == 30
    # Supervised workers get the same backoff (#158).
    bob_start = (run_dir / f"start-bob.{SCRIPT_SUFFIX}").read_text()
    assert "--max-resumes 0" in bob_start
    assert "--resume-delay-s 1.5" in bob_start
    bob = manifest["launch"]["agents"]["bob"]
    assert bob["resume_max_delay_s"] == 60
    assert bob["resume_total_s"] == 3600
    assert bob["resume_series_reset_s"] == 30


@pytest.mark.parametrize(
    ("option", "value", "message"),
    [
        ("stall_after_min", 0, "--stall-after-min"),
        ("stall_after_min", float("inf"), "--stall-after-min"),
        ("max_resumes", -1, "--max-resumes"),
        ("resume_delay_s", -0.5, "--resume-delay-s"),
        ("resume_max_delay_s", -0.5, "--resume-max-delay-s"),
        ("resume_total_s", -0.5, "--resume-total-s"),
        ("resume_series_reset_s", -0.5, "--resume-series-reset-s"),
    ],
)
def test_invalid_stall_and_resume_options_are_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, option: str, value: float, message: str
) -> None:
    with pytest.raises(ValueError, match=message):
        prepare(tmp_path, monkeypatch, **{option: value})


def test_every_resume_prompt_names_the_real_run_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    base = tmp_path / "dir with spaces"
    base.mkdir()
    run_dir, _ = prepare(base, monkeypatch)
    for name in ("alice", "bob", "charlie"):
        text = (run_dir / f"resume-{name}.prompt.md").read_text(encoding="utf-8")
        assert "RUN_DIR" not in text
        assert str(run_dir / "run.json") in text
    remote = PREPARE_RUN.render_resume_prompt("bob", PureWindowsPath("C:/runs/my run"))
    assert "RUN_DIR" not in remote and "C:\\runs\\my run\\run.json" in remote


def test_resume_templates_need_no_operator_paste_or_time_guess() -> None:
    for name in ("alice", "bob", "charlie"):
        template = PREPARE_RUN.render_resume_prompt(name, PurePosixPath("/r"))
        assert "automatically" in template and "stop time may be unknown" in template
        assert "<UTC TIME>" not in template and "<TASK ID>" not in template
        assert "The operator pastes" not in template


@pytest.mark.parametrize("harness", ["claude-code", "codex", "opencode", "antigravity"])
def test_supervised_local_windows_paths_keep_native_spelling(harness: str) -> None:
    """Exercise Windows path rendering even when the test host is POSIX (#165)."""
    prompt = WINDOWS_RUN / "bob.prompt.md"
    temp = WINDOWS_RUN / "tmp" / "bob"
    lines = PREPARE_RUN.agent_launch_lines(
        harness,
        WINDOWS_RUN / "bob",
        PREPARE_RUN.launch_config_path(harness, WINDOWS_RUN / "configs", "bob"),
        prompt,
        "http://hub.example:8420",
        WINDOWS_RUN / "token",
        tmp_dir=temp,
        worker="bob",
        git_dir=WINDOWS_RUN / "bob" / ".git",
        root=PureWindowsPath("C:/src/robomate"),
        powershell=True,
    )
    text = "\n".join(lines)
    assert f"--prompt {PREPARE_RUN.shell_word(str(prompt), powershell=True)}" in text
    assert f"$env:TEMP = {PREPARE_RUN.shell_word(str(temp), powershell=True)}" in text
    assert f"$env:TMP = {PREPARE_RUN.shell_word(str(temp), powershell=True)}" in text
    assert "C:/Users/Bob" not in text


@pytest.mark.parametrize("harness", CLI_NAMES)
@pytest.mark.parametrize("powershell", [False, True])
@pytest.mark.parametrize("worker_only", [False, True])
def test_ask_rendering(harness: str, powershell: bool, worker_only: bool) -> None:
    """All four adapters keep interactive forms across local and remote shells."""
    run = (
        PureWindowsPath("C:/runs/questions with space") if powershell else PurePosixPath("/runs/q")
    )
    body = "\n".join(
        PREPARE_RUN.ask_lines(
            "bob",
            harness,
            run,
            run / "bob",
            "model-x",
            "" if harness == "devin" else "high",
            powershell=powershell,
            git_bash=worker_only and not powershell,
        )
    )
    assert "agent-ask.py" in body
    assert "bob-sessions.jsonl" in body
    assert "model-x" in body and ("high" in body or harness == "devin")
    for absent in (
        "agent-recovery.py",
        "launcher.py",
        "--permission-mode",
        "--approve-for-me",
        "--dangerously-skip-permissions",
        ".prompt.md",
        "--mcp-config",
    ):
        assert absent not in body
    assert "TEMP" in body if powershell else "TMPDIR" in body
    if harness == "claude-code":
        assert "--strict-mcp-config" in body and "--resume" in body and "--fork-session" in body
    elif harness == "codex":
        assert "codex resume" in body and "codex fork" in body
        assert "mcp_servers.robomate.enabled=false" in body and "CODEX_HOME" in body
    elif harness == "opencode":
        assert "OPENCODE_CONFIG" in body and "OPENCODE_CONFIG_CONTENT" in body
        assert '"enabled": false' in body and '"variant": "high"' in body
        assert "--session" in body and "--fork" in body and "--auto" not in body
    elif harness == "devin":
        # agent-ask.py disables the local-scope server; Devin has no fork option.
        assert "devin --model model-x --resume" in body and ".devin" in body
        assert "--fork-command" not in body and "--export" not in body
    else:
        assert "USERPROFILE" in body and "HOME" in body and "--conversation" in body
        assert "--fork-command" not in body


@pytest.mark.parametrize("harness", CLI_NAMES)
def test_ask_scripts_generated_for_every_local_agent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, harness: str
) -> None:
    run_dir, manifest = prepare(
        tmp_path,
        monkeypatch,
        **{f"{agent}_harness": harness for agent in ("alice", "bob", "charlie")},
    )
    for agent in ("alice", "bob", "charlie"):
        path = run_dir / f"ask-{agent}.{SCRIPT_SUFFIX}"
        assert path.is_file()
        assert manifest["launch"]["agents"][agent]["ask_script"] == str(path)
        assert "agent-ask.py" in path.read_text()


ASK_FAKE = """import json, os, sys, time
from pathlib import Path
keys = ("CODEX_HOME", "OPENCODE_CONFIG", "OPENCODE_CONFIG_CONTENT",
        "HOME", "USERPROFILE", "TMPDIR", "TEMP", "TMP")
with open(os.environ["ASK_ARGV_LOG"], "a", encoding="utf-8") as f:
    config = Path(os.environ.get("HOME", ".")) / ".gemini/config/mcp_config.json"
    local = Path(".devin/mcp_config.local.json")
    mcp = None
    if config.is_file() and "--conversation" in sys.argv:
        mcp = json.loads(config.read_text())
    elif local.is_file() and "--resume" in sys.argv:
        mcp = json.loads(local.read_text())
    f.write(json.dumps({"argv": sys.argv[1:], "cwd": os.getcwd(), "mcp": mcp,
                        "env": {k: os.environ.get(k) for k in keys}}) + "\\n")
time.sleep(float(os.environ.get("ASK_SLEEP_S", "0")))
sys.exit(int(os.environ.get("ASK_EXIT_CODE", "0")))
"""


def ask_fake(tmp_path: Path, harness: str) -> dict[str, str]:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    fake = bin_dir / "fake.py"
    fake.write_text(ASK_FAKE)
    name = CLI_NAMES[harness]
    if os.name == "nt":
        (bin_dir / f"{name}.cmd").write_text(f'@"{sys.executable}" "{fake}" %*\r\n')
    else:
        cli = bin_dir / name
        cli.write_text(f"#!{sys.executable}\n{ASK_FAKE}")
        cli.chmod(0o700)
    return os.environ | {
        "PATH": str(bin_dir) + os.pathsep + os.environ["PATH"],
        "ASK_ARGV_LOG": str(tmp_path / "asked.jsonl"),
        "ROBOMATE_HUB_URL": "http://127.0.0.1:1",
        "ROBOMATE_TOKEN_FILE": str(tmp_path / "nonexistent-token"),
    }


def execute_ask(script: Path, env: dict[str, str], *args: str) -> subprocess.CompletedProcess[str]:
    if script.suffix == ".ps1":
        shell = shutil.which("pwsh") or shutil.which("powershell")
        assert shell
        command = [shell, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(script)]
    else:
        command = ["bash", str(script)]
    return subprocess.run(command + list(args), env=env, capture_output=True, text=True, timeout=30)


@pytest.mark.parametrize("harness", CLI_NAMES)
@pytest.mark.parametrize("worker_only", [False, True])
def test_ask_executes_without_hub_and_preserves_run_log(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, harness: str, worker_only: bool
) -> None:
    env = ask_fake(tmp_path, harness)
    effort = "" if harness == "devin" else "high"
    suppressed = harness in ("antigravity", "devin")
    if worker_only:
        run_dir = tmp_path / "remote run"
        run_dir.mkdir()
        workdir = run_dir / "bob"
        workdir.mkdir()
        bundle = PREPARE_RUN.render_worker_bundle(
            "bob",
            harness,
            "1.2",
            "provider",
            "model-x",
            "",
            "absent-token",
            "http://127.0.0.1:1",
            ROOT,
            run_dir,
            workdir,
            run_dir,
            effort=effort,
        )
        script = run_dir / ("ask-bob.ps1" if os.name == "nt" else "ask-bob.sh")
        assert bundle["ask_script"] == (run_dir / "ask-bob.sh").as_posix()
    else:
        run_dir, _ = prepare(
            tmp_path, monkeypatch, bob_harness=harness, bob_model="model-x", bob_effort=effort
        )
        workdir = run_dir / "bob"
        script = run_dir / f"ask-bob.{SCRIPT_SUFFIX}"
    config = Path(PREPARE_RUN.launch_config_path(harness, run_dir / "configs", "bob", workdir))
    mcp = config / (
        "mcp_config.local.json" if harness == "devin" else ".gemini/config/mcp_config.json"
    )
    original_mcp = mcp.read_bytes() if suppressed else None
    ids = (
        ["ses_old123", "ses_new456"]
        if harness == "opencode"
        else ["11111111-1111-1111-1111-111111111111", "22222222-2222-2222-2222-222222222222"]
    )
    sessions = run_dir / "bob-sessions.jsonl"
    before = "".join(
        json.dumps({"agent": "bob", "harness": harness, "conversation_id": id_}) + "\n"
        for id_ in ids
    )
    sessions.write_text(before)
    result = execute_ask(script, env)
    assert result.returncode == 0, result.stdout + result.stderr
    assert ids[0] in result.stdout and ids[-1] in result.stdout
    assert "hub tools are unavailable" in result.stdout and str(workdir) in result.stdout
    result = execute_ask(script, env, ids[0])
    assert result.returncode == 0, result.stdout + result.stderr
    result = execute_ask(script, env, ids[0], "--fork")
    assert (result.returncode == 2) if suppressed else (result.returncode == 0)
    if suppressed:
        assert "--fork is unavailable" in result.stderr
    result = execute_ask(script, env, "--last")
    assert result.returncode != 0
    result = execute_ask(script, env, "bad id")
    assert result.returncode == 2 and "Invalid conversation ID" in result.stderr
    assert sessions.read_text() == before
    recorded = [json.loads(line) for line in Path(env["ASK_ARGV_LOG"]).read_text().splitlines()]
    assert len(recorded) == (2 if suppressed else 3)
    assert ids[1] in recorded[0]["argv"] and ids[0] in recorded[1]["argv"]
    for run in recorded:
        assert Path(run["cwd"]).resolve() == workdir.resolve()
        assert "model-x" in run["argv"]
        assert Path(run["env"]["TEMP" if os.name == "nt" else "TMPDIR"]) == run_dir / "tmp/bob"
        for absent in (
            "-p",
            "exec",
            "run",
            "--auto",
            "--permission-mode",
            "--approve-for-me",
            "--dangerously-skip-permissions",
            "--mcp-config",
        ):
            assert absent not in run["argv"]
        if harness == "codex":
            assert Path(run["env"]["CODEX_HOME"]) == config
            assert "mcp_servers.robomate.enabled=false" in run["argv"]
        elif harness == "opencode":
            assert Path(run["env"]["OPENCODE_CONFIG"]) == config
            inline = json.loads(run["env"]["OPENCODE_CONFIG_CONTENT"])
            assert inline["mcp"]["robomate"]["enabled"] is False
            assert inline["agent"]["build"]["variant"] == "high"
        elif harness == "antigravity":
            assert Path(run["env"]["HOME"]) == config
            assert Path(run["env"]["USERPROFILE"]) == config
            assert run["mcp"] == {"mcpServers": {}}
        elif harness == "devin":
            # A disabled entry, not an empty map: Devin merges scopes (#224 r1-1).
            assert run["mcp"]["mcpServers"]["robomate"]["disabled"] is True
    if suppressed:
        assert mcp.read_bytes() == original_mcp
        result = execute_ask(script, env | {"ASK_EXIT_CODE": "7"})
        assert result.returncode == 7
        assert mcp.read_bytes() == original_mcp
        assert not mcp.with_name(mcp.name + ".ask-backup").exists()
    if not suppressed:
        assert (
            "fork"
            if harness == "codex"
            else "--fork-session"
            if harness == "claude-code"
            else "--fork"
        ) in recorded[-1]["argv"]


@pytest.mark.parametrize("auto_start", [False, True])
@pytest.mark.parametrize("worker_only", [False, True])
@pytest.mark.parametrize("reason", ["identity", "conversation", "hub_unreadable", "arguments"])
def test_ask_hint_ends_every_resume_refusal(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    auto_start: bool,
    worker_only: bool,
    reason: str,
) -> None:
    if worker_only and os.name == "nt":
        pytest.skip("worker resume is a Git Bash script; tested by Bash on POSIX")
    env = ask_fake(tmp_path, "claude-code")
    if worker_only:
        run_dir = tmp_path / "worker"
        run_dir.mkdir()
        workdir = run_dir / "bob"
        workdir.mkdir()
        PREPARE_RUN.render_worker_bundle(
            "bob",
            "claude-code",
            "1",
            "anthropic",
            "",
            "",
            str(tmp_path / "token"),
            "http://127.0.0.1:1",
            ROOT,
            run_dir,
            workdir,
            run_dir,
            auto_start=auto_start,
        )
        script = run_dir / "resume-bob.sh"
    else:
        run_dir, _ = prepare(
            tmp_path,
            monkeypatch,
            auto_start=auto_start,
            test_hub_url="http://127.0.0.1:1",
            bob_harness="claude-code",
        )
        script = run_dir / f"resume-bob.{SCRIPT_SUFFIX}"
    (tmp_path / "token").write_text("fake-token")
    row = {
        "agent": "bob",
        "harness": "claude-code",
        "timestamp": "2026-10-10T00:00:00Z",
        "conversation_id": "11111111-1111-1111-1111-111111111111",
        "event": "exit",
        "hub_id": "abc",
        "workflow_id": "wf-test",
    }
    if reason == "identity":
        del row["workflow_id"]
    elif reason == "conversation":
        row["conversation_id"] = "bad id"
    (run_dir / "bob-sessions.jsonl").write_text(json.dumps(row) + "\n")
    result = execute_ask(
        script, env, *(["--not-a-recovery-option"] if reason == "arguments" else [])
    )
    assert result.returncode != 0, result.stdout + result.stderr
    last = result.stderr.splitlines()[-1]
    assert str(run_dir / "ask-bob.sh") in last
    assert last.endswith("(.ps1 on Windows); it doesn't need the hub.")
    assert not Path(env["ASK_ARGV_LOG"]).exists()


def test_ask_devin_masks_an_inherited_robomate_server(tmp_path: Path) -> None:
    """#224 r1-1: Devin merges local, project and user scope by server name.

    An empty local map left a project- or user-scope ``robomate`` live (shown
    with ``devin mcp get robomate`` on 3000.11.3); a disabled local entry of
    the same name masks it, and the original bytes come back afterwards.
    """
    spec = importlib.util.spec_from_file_location("agent_ask", ROOT / "scripts/agent-ask.py")
    assert spec and spec.loader
    ask = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(ask)
    scope = tmp_path / ".devin"
    scope.mkdir()
    inherited = {"robomate": {"command": "another-hub-bridge", "args": []}}
    (scope / "mcp_config.json").write_text(json.dumps({"mcpServers": inherited}))
    local = scope / "mcp_config.local.json"
    original = b'{"mcpServers": {"robomate": {"command": "uv", "env": {"A": "b"}}}}\n'
    local.write_bytes(original)
    name, replacement = ask.SUPPRESSED_MCP["devin"]
    assert name == local.name
    with ask.suppress_mcp(local, replacement):
        servers = json.loads(local.read_text())["mcpServers"]
        # Devin resolves a server name from the most specific scope that has it.
        effective = inherited | servers
        assert effective["robomate"]["disabled"] is True
        assert effective["robomate"]["command"] != "another-hub-bridge"
    assert local.read_bytes() == original
    assert not local.with_name(local.name + ".ask-backup").exists()


def test_ask_antigravity_restores_mcp_on_interrupt_and_refuses_concurrent_swap(
    tmp_path: Path,
) -> None:
    spec = importlib.util.spec_from_file_location("agent_ask", ROOT / "scripts/agent-ask.py")
    assert spec and spec.loader
    ask = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(ask)
    config = tmp_path / ".gemini/config/mcp_config.json"
    config.parent.mkdir(parents=True)
    original = b'{"mcpServers": {"robomate": {"command": "must-not-run"}}}\n'
    config.write_bytes(original)
    with pytest.raises(KeyboardInterrupt), ask.suppress_mcp(config):
        assert json.loads(config.read_bytes()) == {"mcpServers": {}}
        with (
            pytest.raises(ValueError, match="An ask may still be running"),
            ask.suppress_mcp(config),
        ):
            pytest.fail("concurrent ask must not replace the saved configuration")
        ask.interrupted(15, None)
    assert config.read_bytes() == original
    assert not config.with_name(config.name + ".ask-backup").exists()


@pytest.mark.parametrize("harness", CLI_NAMES)
def test_ask_explicit_id_works_without_session_log_or_recovery_identity(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    harness: str,
) -> None:
    env = ask_fake(tmp_path, harness)
    run_dir, _ = prepare(tmp_path, monkeypatch, bob_harness=harness)
    script = run_dir / f"ask-bob.{SCRIPT_SUFFIX}"
    result = execute_ask(script, env)
    assert result.returncode == 2 and "No saved conversation ID" in result.stderr
    (run_dir / "bob-sessions.jsonl").write_text("not-json; deliberately unusable by recovery")
    selected = (
        "ses_explicit123" if harness == "opencode" else "11111111-1111-1111-1111-111111111111"
    )
    result = execute_ask(script, env, selected)
    assert result.returncode == 0, result.stdout + result.stderr
    assert selected in result.stdout
    assert (run_dir / "bob-sessions.jsonl").read_text().startswith("not-json")


def test_ask_does_not_launch_if_workdir_is_missing(tmp_path: Path) -> None:
    env = ask_fake(tmp_path, "claude-code")
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    script = run_dir / f"ask-bob.{SCRIPT_SUFFIX}"
    PREPARE_RUN.write_script(
        script,
        PREPARE_RUN.start_script(
            PREPARE_RUN.ask_lines(
                "bob", "claude-code", run_dir, run_dir / "missing", powershell=os.name == "nt"
            ),
            os.name == "nt",
        ),
    )
    result = execute_ask(script, env, "11111111-1111-1111-1111-111111111111")
    assert result.returncode != 0
    assert not Path(env["ASK_ARGV_LOG"]).exists()


# Devin writes its config into the real clone, so it is covered by
# test_devin_worker_only_bundle_writes_the_clone_scope instead (#224).
@pytest.mark.parametrize("harness", [name for name in CLI_NAMES if name != "devin"])
def test_ask_worker_bundle_writes_bash_and_powershell_for_windows(
    tmp_path: Path,
    harness: str,
) -> None:
    run_dir = PureWindowsPath("C:/runs/remote questions")
    PREPARE_RUN.render_worker_bundle(
        "bob",
        harness,
        "1",
        "provider",
        "model-x",
        "",
        "C:/private/token",
        "http://127.0.0.1:1",
        PureWindowsPath("C:/robomate"),
        run_dir,
        run_dir / "bob",
        tmp_path,
        effort="high",
    )
    bash = (tmp_path / "ask-bob.sh").read_text()
    ps = (tmp_path / "ask-bob.ps1").read_text()
    argv = shlex.split("\n".join(bash.splitlines()[3:]))
    command = argv[argv.index("--shell-command") + 1]
    assert "cd '/c/runs/remote questions/bob'" in command
    assert "--powershell" not in bash
    assert "--powershell" in ps
    assert "C:/runs/remote questions/bob-sessions.jsonl" in bash
    assert "C:\\runs\\remote questions\\bob-sessions.jsonl" in ps


def test_ask_git_bash_spells_windows_interpreter_path() -> None:
    run_dir = PureWindowsPath("C:/runs/questions")
    body = "\n".join(
        PREPARE_RUN.ask_lines(
            "bob",
            "codex",
            run_dir,
            run_dir / "bob",
            git_bash=True,
            python="C:/Program Files/Python/python.exe",
        )
    )
    assert "exec '/c/Program Files/Python/python.exe'" in body


@pytest.mark.skipif(
    os.name == "nt", reason="POSIX signal delivery; Windows console close is documented"
)
@pytest.mark.parametrize("signal_name", ["SIGINT", "SIGTERM", "SIGHUP"])
def test_ask_antigravity_restores_mcp_after_process_signal(
    tmp_path: Path, signal_name: str
) -> None:
    env = ask_fake(tmp_path, "antigravity") | {"ASK_SLEEP_S": "5"}
    run_dir = tmp_path / "run"
    workdir = run_dir / "bob"
    workdir.mkdir(parents=True)
    PREPARE_RUN.render_worker_bundle(
        "bob",
        "antigravity",
        "1",
        "provider",
        "model-x",
        "",
        "absent-token",
        "http://127.0.0.1:1",
        ROOT,
        run_dir,
        workdir,
        run_dir,
    )
    config = run_dir / "configs/bob-agy/.gemini/config/mcp_config.json"
    original = config.read_bytes()
    script = run_dir / "ask-bob.sh"
    child = subprocess.Popen(
        # A detached/nohup parent can ignore these signals. The helper must
        # install its own handlers rather than depend on inherited defaults.
        [
            "bash",
            "-c",
            'trap "" INT HUP; exec bash "$@"',
            "ask-test",
            str(script),
            "11111111-1111-1111-1111-111111111111",
        ],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        deadline = time.monotonic() + 10
        while not Path(env["ASK_ARGV_LOG"]).exists() and time.monotonic() < deadline:
            assert child.poll() is None
            time.sleep(0.02)
        assert Path(env["ASK_ARGV_LOG"]).exists(), "fake harness never started"
        assert json.loads(config.read_bytes()) == {"mcpServers": {}}
        child.send_signal(getattr(signal, signal_name))
        stdout, stderr = child.communicate(timeout=10)
        assert child.returncode == 130, stdout + stderr
        assert config.read_bytes() == original
        assert not config.with_name(config.name + ".ask-backup").exists()
    finally:
        if child.poll() is None:
            child.kill()
        child.communicate(timeout=10)


@pytest.mark.parametrize("contents", ['{"agent": "bob', '{"agent": "other"}'])
def test_ask_malformed_log_names_file_and_explicit_id_escape(tmp_path: Path, contents: str) -> None:
    env = ask_fake(tmp_path, "claude-code")
    sessions = tmp_path / "bob-sessions.jsonl"
    sessions.write_text(contents)
    script = tmp_path / f"ask-bob.{SCRIPT_SUFFIX}"
    PREPARE_RUN.write_script(
        script,
        PREPARE_RUN.start_script(
            PREPARE_RUN.ask_lines(
                "bob", "claude-code", tmp_path, tmp_path, powershell=os.name == "nt"
            ),
            os.name == "nt",
        ),
    )
    result = execute_ask(script, env)
    assert result.returncode == 2
    assert str(sessions) in result.stderr and "explicit ID" in result.stderr
    assert "Unterminated string" not in result.stderr
    result = execute_ask(script, env, "11111111-1111-1111-1111-111111111111")
    assert result.returncode == 0, result.stdout + result.stderr
    assert sessions.read_text() == contents


def test_devin_version_is_the_word_after_its_name() -> None:
    """#224: ``devin --version`` prints ``devin 3000.11.3 (9c803229faa4)``."""
    output = "devin 3000.11.3 (9c803229faa4)"
    assert RUN_COMMON.parse_harness_version("devin", output) == "3000.11.3"


def test_devin_rejects_an_effort_it_has_no_flag_for() -> None:
    with pytest.raises(ValueError, match="devin has no reasoning-effort flag"):
        PREPARE_RUN.model_flags("devin", "swe-1.6-slow", "high")
    assert PREPARE_RUN.model_flags("devin", "swe-1.6-slow", "") == ["--model", "swe-1.6-slow"]


def test_devin_run_keeps_each_agents_server_in_its_own_local_scope(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#224: no MCP-config flag, so each agent's ``.devin/`` holds its server."""
    run_dir, manifest = prepare(
        tmp_path,
        monkeypatch,
        alice_harness="devin",
        bob_harness="devin",
        charlie_harness="devin",
        bob_model="swe-1.6-slow",
    )
    assert manifest["providers"]["bob"] == "unknown"
    alice_scope = run_dir / "alice-runtime" / ".devin"
    alice = json.loads((alice_scope / "mcp_config.local.json").read_text())
    assert alice["mcpServers"]["robomate"]["args"][-3:] == ["mcp", "--role", "orchestrator"]
    assert (alice_scope / "skills" / "alice-orchestrator" / "SKILL.md").exists()
    for name in ("bob", "charlie"):
        clone = Path(manifest["workspaces"][name]["path"])
        scope = clone / ".devin"
        server = json.loads((scope / "mcp_config.local.json").read_text())["mcpServers"]
        assert set(server) == {"robomate"}
        assert server["robomate"]["args"][-3:] == ["mcp", "--role", "worker"]
        assert server["robomate"]["env"]["HUB_HARNESS"] == "devin"
        assert server["robomate"]["env"]["HUB_HARNESS_VERSION"] == "3000.11.3"
        assert server["robomate"]["env"]["AGENT_NAME"] == name
        permissions = json.loads((scope / "config.local.json").read_text())
        assert permissions == {"permissions": {"allow": ["mcp__robomate__*"]}}
        assert manifest["configs"][name] == str(scope / "mcp_config.local.json")
        exclude = (clone / ".git" / "info" / "exclude").read_text().splitlines()
        assert exclude.count(".devin/mcp_config.local.json") == 1
        assert exclude.count(".devin/config.local.json") == 1
        status = subprocess.run(
            ["git", "-C", str(clone), "status", "--porcelain"],
            check=True,
            capture_output=True,
            text=True,
        )
        assert status.stdout == ""
        start = (run_dir / f"start-{name}.{SCRIPT_SUFFIX}").read_text()
        assert "worker-launcher.py" in start
        assert "--permission-mode dangerous --respect-workspace-trust=false --export" in start
        assert " -c " not in start and "--continue" not in start
        prompt = (run_dir / f"{name}.prompt.md").read_text()
        assert "Use a 100 second wait time" in prompt and "`devin -p`" in prompt
    # A rerun appends no second exclude line.
    PREPARE_RUN.prepare(
        str(tmp_path / "origin"),
        run_dir,
        issue=42,
        account="tester",
        hub_repo=tmp_path / "target",
        alice_harness="devin",
        bob_harness="devin",
        charlie_harness="devin",
        bob_model="swe-1.6-slow",
    )
    clone = Path(manifest["workspaces"]["bob"]["path"])
    exclude = (clone / ".git" / "info" / "exclude").read_text().splitlines()
    assert exclude.count(".devin/mcp_config.local.json") == 1


@pytest.mark.parametrize("auto_start", [True, False])
def test_devin_launch_and_resume_lines(tmp_path: Path, auto_start: bool) -> None:
    run_dir = (tmp_path / "run").resolve()
    workdir = run_dir / "bob"
    prompt = run_dir / "bob.prompt.md"
    config = workdir / ".devin"
    lines = PREPARE_RUN.launch_lines(
        "devin", workdir, config, prompt, workdir / ".git", "swe-1.6-slow", "", auto_start
    )
    command = lines[-1]
    assert command.startswith("devin --model swe-1.6-slow")
    if auto_start:
        assert "--permission-mode dangerous --respect-workspace-trust=false -p" in command
        assert f"Read {prompt} and follow the instructions in it" in command
    else:
        assert command == "devin --model swe-1.6-slow"
    resume = PREPARE_RUN.devin_resume(
        str(run_dir / "resume-bob.prompt.md"), ["--model", "swe-1.6-slow"], auto_start, "ID"
    )
    assert "--resume ID" in resume[0] and ("-p" in resume[0].split()) == auto_start


def test_devin_worker_only_bundle_writes_the_clone_scope(tmp_path: Path) -> None:
    clone = tmp_path / "bob"
    subprocess.run(["git", "init", "-b", "main", str(clone)], check=True, capture_output=True)
    out = tmp_path / "run"
    bundle = PREPARE_RUN.render_worker_bundle(
        "bob",
        "devin",
        "3000.11.3",
        "unknown",
        "swe-1.6-slow",
        "",
        "C:/private/token",
        "http://172.26.115.68:8420",
        PurePosixPath("/opt/robomate"),
        PurePosixPath(out.as_posix()),
        PurePosixPath(clone.as_posix()),
        out,
    )
    assert bundle["config"] == f"{clone.as_posix()}/.devin/mcp_config.local.json"
    server = json.loads((clone / ".devin/mcp_config.local.json").read_text())
    assert server["mcpServers"]["robomate"]["env"]["ROBOMATE_TOKEN_FILE"] == "C:/private/token"
    if os.name != "nt":
        assert (clone / ".devin/mcp_config.local.json").stat().st_mode & 0o077 == 0
    script = (out / "start-bob.sh").read_text(encoding="utf-8")
    assert "worker-launcher.py" in script and "devin --model swe-1.6-slow" in script
    resume = (out / "resume-bob.sh").read_text(encoding="utf-8")
    assert "--recover" in resume


@pytest.mark.parametrize("workers", [0, 2])
@pytest.mark.parametrize("auto_start", [True, False])
def test_pytest_worker_budget_reaches_local_start_and_resume_scripts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, workers: int, auto_start: bool
) -> None:
    run_dir, manifest = prepare(
        tmp_path, monkeypatch, pytest_workers=workers, auto_start=auto_start
    )
    assert manifest["pytest_workers"] == workers
    if os.name == "nt":
        expected = (
            f"$env:PYTEST_XDIST_AUTO_NUM_WORKERS = '{workers}'"
            if workers
            else "$env:PYTEST_XDIST_AUTO_NUM_WORKERS = $null"
        )
    else:
        expected = (
            f"export PYTEST_XDIST_AUTO_NUM_WORKERS={workers}"
            if workers
            else "unset PYTEST_XDIST_AUTO_NUM_WORKERS"
        )
    for name in ("alice", "bob", "charlie"):
        for kind in ("start", "resume"):
            text = (run_dir / f"{kind}-{name}.{SCRIPT_SUFFIX}").read_text(encoding="utf-8")
            assert expected in text.splitlines()


@pytest.mark.parametrize("windows", [True, False])
@pytest.mark.parametrize("auto_start", [True, False])
def test_pytest_worker_budget_reaches_remote_git_bash_scripts(
    tmp_path: Path, windows: bool, auto_start: bool
) -> None:
    run = PureWindowsPath("C:/my run") if windows else PurePosixPath("/srv/my run")
    root = run / "robomate"
    PREPARE_RUN.render_worker_bundle(
        "bob",
        "claude-code",
        "2.1",
        "anthropic",
        "",
        "",
        "test-token-file",
        "http://192.0.2.1:8420",
        root,
        run,
        run / "bob",
        tmp_path,
        auto_start=auto_start,
        pytest_workers=2,
    )
    for kind in ("start", "resume"):
        text = (tmp_path / f"{kind}-bob.sh").read_text(encoding="utf-8")
        assert "export PYTEST_XDIST_AUTO_NUM_WORKERS=2" in text.splitlines()
    command = PREPARE_RUN.worker_only_command(
        "bob",
        "o/r",
        "http://192.0.2.1:8420",
        tmp_path / "token",
        "claude-code",
        "",
        None,
        "",
        pytest_workers=2,
    )
    assert "--pytest-workers 2" in command


def test_negative_pytest_worker_count_fails_before_preparation(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="pytest-workers"):
        PREPARE_RUN.prepare("o/r", tmp_path, pytest_workers=-1)
    with pytest.raises(ValueError, match="pytest-workers"):
        PREPARE_RUN.prepare_worker(
            "bob",
            "o/r",
            tmp_path,
            "http://192.0.2.1:8420",
            tmp_path / "token",
            "claude-code",
            pytest_workers=-1,
        )
