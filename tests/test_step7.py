"""Step 7 networked topology (#140): addressing, preflight, launch, checks 1-5, export.

The helpers live in ``scripts/step7.py``; ``scripts/step6.py`` stays the entry
point, so these tests drive both through the module ``step6`` imports.
"""

import contextlib
import copy
import importlib.util
import json
import os
import re
import sqlite3
import subprocess
import sys
import tomllib
from pathlib import Path, PureWindowsPath
from typing import Any

import httpx
import pytest
import test_step6
from agent_hub_common import SCHEMA_VERSION, MetaKeys
from conftest import TOKEN, message, rpc
from fastapi import FastAPI
from worker_mcp.config import WorkerSettings

ROOT = Path(__file__).resolve().parents[1]
STEP6 = test_step6.STEP6
STEP7 = STEP6.step7
stamp = test_step6.stamp
# Step 6's fixtures, shared so the Step 7 verifier runs every Step 6 check too.
proof = test_step6.proof
fake_step6_cli = test_step6.fake_step6_cli

ETH0 = "172.26.115.68"
VETHERNET = "172.26.112.1"
PORT = 8431
WINDOWS_RUN = "C:/work/step7-run"
BOB_ID = "b" * 64
CHARLIE_ID = "c" * 64

LAUNCH_SPEC = importlib.util.spec_from_file_location(
    "step6_launch", ROOT / "scripts/step6_launch.py"
)
assert LAUNCH_SPEC and LAUNCH_SPEC.loader
LAUNCH = importlib.util.module_from_spec(LAUNCH_SPEC)
LAUNCH_SPEC.loader.exec_module(LAUNCH)


def git(*args: str, cwd: Path | None = None) -> str:
    return subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True, text=True
    ).stdout.strip()


def commit_origin(path: Path) -> Path:
    git("init", "--initial-branch=main", str(path))
    (path / "README.md").write_text("sandbox\n")
    git("add", "README.md", cwd=path)
    git(
        "-c",
        "user.name=Test",
        "-c",
        "user.email=test@example.com",
        "commit",
        "-m",
        "base",
        cwd=path,
    )
    return path


def network() -> dict[str, Any]:
    return {
        "hub_host": "0.0.0.0",
        "hub_port": PORT,
        "hub_url": f"http://{ETH0}:{PORT}",
        "public_url": f"http://{ETH0}:{PORT}",
        "charlie_url": f"http://127.0.0.1:{PORT}",
        "remote_worker": "bob",
        "wsl_eth0": ETH0,
        "wsl_distro": "Ubuntu-24.04",
        "lost_after_s": 180.0,
    }


def windows() -> dict[str, str]:
    return {
        "run_dir": WINDOWS_RUN,
        "checkout": "C:/work/robo-agents",
        "bash": STEP7.WINDOWS_BASH,
        "curl": STEP7.WINDOWS_CURL,
    }


def at(minute: int, second: int = 0) -> str:
    """A Windows-clock timestamp, unrelated to the hub's stamp() clock."""
    return f"2026-09-18T13:{minute:02d}:{second:02d}.000Z"


@pytest.fixture
def networked(proof: tuple[Any, ...]) -> tuple[Any, ...]:
    """Step 6's correlated proof, recast as a WSL-hub / Windows-Bob run."""
    manifest, snapshot, facts, traces = copy.deepcopy(proof)
    manifest["topology"] = "networked"
    manifest["network"] = network()
    manifest["windows"] = windows()
    manifest["workspaces"]["bob"].update(
        path="C:\\work\\step7-run\\bob",
        workspace_id=BOB_ID,
        host="windows",
        qualified_path="windows:C:/work/step7-run/bob",
    )
    manifest["workspaces"]["charlie"].update(
        workspace_id=CHARLIE_ID, host="wsl", qualified_path="wsl:/runs/charlie"
    )
    for record in facts["review_runs"]:
        record["workspace_id"] = CHARLIE_ID
    agents = {row["name"]: row for row in snapshot["agent"]}
    agents["bob"].update(
        workspace_id=BOB_ID,
        checkin_remote_addr=VETHERNET,
        last_remote_addr=VETHERNET,
        context_id="bob-context",
    )
    agents["charlie"].update(
        workspace_id=CHARLIE_ID, checkin_remote_addr="127.0.0.1", last_remote_addr="127.0.0.1"
    )
    snapshot["event"] = [
        {
            "kind": "agent_checked_in",
            "payload_json": json.dumps({"agent": name, "workspace_id": identity}),
        }
        for name, identity in (("bob", BOB_ID), ("charlie", CHARLIE_ID))
    ]
    bob_tasks = [task["id"] for task in snapshot["task"] if task["assignee"] == "bob"]
    telemetry: list[dict[str, Any]] = [
        {
            "event": "session_started",
            "timestamp": at(0),
            "hub_url": network()["public_url"],
            "worker_instance_id": "bob-instance",
        }
    ]
    for index, task_id in enumerate(bob_tasks):
        minute = 1 + 2 * index
        telemetry += [
            {
                "event": "tool_call",
                "tool": "await_assignment",
                "phase": "success",
                "outcome": "assignment",
                "task_id": task_id,
                "timestamp": at(minute),
            },
            {
                "event": "heartbeat",
                "phase": "success",
                "accepted": True,
                "current_task_id": task_id,
                "hub_url": network()["public_url"],
                "timestamp": at(minute, 30),
            },
            {
                "event": "tool_call",
                "tool": "submit_result",
                "phase": "success",
                "task_id": task_id,
                "timestamp": at(minute + 1),
            },
        ]
    facts["telemetry"]["bob"] = telemetry + facts["telemetry"]["bob"]
    sample = {
        "status": "busy",
        "current_task_id": bob_tasks[0],
        "last_remote_addr": VETHERNET,
        "worker_instance_id": "bob-instance",
        "context_id": "bob-context",
        "workspace_id": BOB_ID,
    }
    manifest["heartbeat_samples"] = [
        {**sample, "sampled_at": stamp(0), "last_heartbeat": stamp(0)},
        {**sample, "sampled_at": stamp(1), "last_heartbeat": "2026-09-18T12:00:00.900Z"},
    ]
    live = {**sample, "last_heartbeat": stamp(0)}
    manifest["duplicate_probe"] = {
        "at": "2026-09-18T12:00:00.500Z",
        "agent": STEP7.PROBE_AGENT,
        "workspace_id": BOB_ID,
        "status_code": 409,
        "refused": True,
        "message": f"workspace {BOB_ID} is occupied by live agent bob",
        "bob_before": live,
        "bob_after": dict(live),
    }
    facts["canaries"] = {
        name: {"present_in_own": True, "found_in_other": []} for name in ("bob", "charlie")
    }
    traces["bob"].append(
        {"name": "Read", "input": {"file_path": "C:\\work\\step7-run\\bob\\step6_x.py"}}
    )
    return manifest, snapshot, facts, traces


# Scenario and addressing.


def test_step7_scenario_is_the_step6_fixture_on_the_networked_topology() -> None:
    step6 = json.loads((ROOT / "scenarios/step6-localhost-untrusted.json").read_text())
    step7 = json.loads((ROOT / "scenarios/step7-networked-untrusted.json").read_text())
    assert STEP7.topology_of(step6) == "localhost"
    assert STEP7.topology_of(step7) == "networked"
    assert step7.pop("evidence_prefix") == "step7" and "evidence_prefix" not in step6
    assert step7.pop("topology") == "networked"
    assert step7.pop("title") != step6.pop("title")
    assert step7 == step6
    with pytest.raises(ValueError, match="topology"):
        STEP7.topology_of({"topology": "multi-machine"})


def test_eth0_address_is_read_and_must_be_a_private_nat_address() -> None:
    output = (
        "2: eth0    inet 172.26.115.68/20 brd 172.26.127.255 scope global eth0\\"
        "       valid_lft forever preferred_lft forever"
    )
    assert STEP7.parse_eth0(output) == ETH0
    for bad in ("", "2: eth0 inet 127.0.0.1/8 scope host", "2: eth0 inet 8.8.8.8/24 scope"):
        with pytest.raises(ValueError):
            STEP7.parse_eth0(bad)


def test_network_settings_take_the_run_port_never_8420() -> None:
    settings = STEP7.network_settings(ETH0, PORT, "Ubuntu-24.04")
    assert settings == network()
    assert STEP7.hub_env(settings) == {
        "HUB_HOST": "0.0.0.0",
        "HUB_PORT": str(PORT),
        "HUB_PUBLIC_URL": f"http://{ETH0}:{PORT}",
        "HUB_LOST_AFTER_S": "180.0",
    }
    manifest = {"topology": "networked", "network": settings}
    assert STEP7.local_hub(manifest, "http://127.0.0.1:8420") == f"http://127.0.0.1:{PORT}"
    assert STEP7.local_hub({}, "http://127.0.0.1:8420") == "http://127.0.0.1:8420"
    assert "8420" not in json.dumps(settings)
    with pytest.raises(ValueError, match="port"):
        STEP7.network_settings(ETH0, 0, "Ubuntu-24.04")


def test_windows_paths_are_qualified_by_host() -> None:
    assert STEP7.windows_path("C:\\work\\run", "--flag") == "C:/work/run"
    for bad in ("work/run", "C:", "C:/", "/mnt/c/work", "C:/work/../run"):
        with pytest.raises(ValueError, match="--flag"):
            STEP7.windows_path(bad, "--flag")
    assert STEP7.mount("C:\\work\\run\\bob") == "/mnt/c/work/run/bob"
    assert STEP7.mount("D:/x") == "/mnt/d/x"
    assert STEP7.qualified("windows", "C:\\work\\run\\bob") == "windows:C:/work/run/bob"
    assert STEP7.qualified("wsl", "/home/me/run/charlie") == "wsl:/home/me/run/charlie"
    bob = {"host": "windows", "path": "C:\\work\\run\\bob"}
    assert STEP7.local_path(bob) == Path("/mnt/c/work/run/bob")
    assert STEP7.local_path({"host": "wsl", "path": "/runs/c"}) == Path("/runs/c")


def test_windows_commands_run_in_git_bash_from_the_windows_checkout() -> None:
    command, cwd = STEP7.windows_command(
        windows(), ["uv", "run", "python", "x y.py"], {"GIT_CONFIG_COUNT": "1"}
    )
    assert command[:2] == [STEP7.WINDOWS_BASH, "-lc"]
    assert command[2] == (
        "cd /c/work/robo-agents || exit 97; export GIT_CONFIG_COUNT=1; exec uv run python 'x y.py'"
    )
    assert cwd == "/mnt/c/work/robo-agents"
    # WSL's Windows command line collapses doubled backslashes; refuse them all.
    with pytest.raises(ValueError, match="forward-slash"):
        STEP7.windows_command(windows(), ["cat", "\\\\wsl.localhost\\x\\token"])
    url = "git@github.com:RoboNater/robo-agents-sandbox.git"
    assert STEP7.windows_clone_source(url, "Ubuntu-24.04") == (url, {})
    source, exports = STEP7.windows_clone_source("/home/me/sandbox", "Ubuntu-24.04")
    assert source == "//wsl.localhost/Ubuntu-24.04/home/me/sandbox"
    assert exports["GIT_CONFIG_VALUE_0"] == source
    assert exports["GIT_CONFIG_VALUE_1"] == source + "/.git"


def networked_manifest(directory: Path) -> dict[str, Any]:
    return {
        "run_id": "unique",
        "run_dir": str(directory),
        "repository": STEP6.SANDBOX,
        "topology": "networked",
        "evidence_prefix": "step7",
        "network": network(),
        "windows": windows(),
        "issue": {"number": 9, "url": "https://github.com/" + STEP6.SANDBOX + "/issues/9"},
        "models": {"alice": "claude-sonnet-5", "bob": "claude-sonnet-5", "charlie": "gpt-5.6"},
        "alice_session_id": "00000000-0000-0000-0000-000000000000",
        "claude_config_dir": str(directory / "claude-config"),
        "claude_config_dir_is_custom": True,
    }


def test_step6_launchers_use_the_run_port(tmp_path: Path, fake_step6_cli: Path) -> None:
    directory = tmp_path / "run"
    (directory / "alice-runtime").mkdir(parents=True)
    STEP6.save(directory / "run.json", networked_manifest(directory))
    (directory / "alice.mcp.json").write_text("{}")
    (directory / "alice.prompt.md").write_text("prompt")
    probes = tmp_path / "curl.log"
    curl = fake_step6_cli / "curl"
    curl.write_text(f'#!/usr/bin/env bash\necho "$@" >> {probes}\nexit 1\n')
    alice = subprocess.run(
        [str(ROOT / "scripts/launch-step6-alice.sh"), str(directory)],
        capture_output=True,
        text=True,
    )
    assert alice.returncode == 0, alice.stderr
    assert probes.read_text().split()[-1] == f"http://127.0.0.1:{PORT}/healthz"
    curl.write_text("#!/usr/bin/env bash\nexit 0\n")  # the run's port is taken
    occupied = subprocess.run(
        [str(ROOT / "scripts/launch-step6-alice.sh"), str(directory)],
        capture_output=True,
        text=True,
    )
    assert occupied.returncode == 1 and f"port {PORT} occupied" in occupied.stderr
    bob = subprocess.run(
        [str(ROOT / "scripts/launch-step6-bob.sh"), str(directory)],
        capture_output=True,
        text=True,
    )
    assert bob.returncode != 0 and "step6_launch.py bob" in bob.stderr


def test_codex_alice_checks_the_run_port(monkeypatch: pytest.MonkeyPatch) -> None:
    seen = []

    def healthy(url: str) -> bool:
        seen.append(url)
        return True

    monkeypatch.setattr(LAUNCH, "hub_healthy", healthy)
    manifest = networked_manifest(Path("/runs/x")) | {"harnesses": {"alice": "codex"}}
    with pytest.raises(SystemExit, match=f"127.0.0.1:{PORT} occupied"):
        LAUNCH.alice(Path("/runs/x"), manifest, 1, 0)
    assert seen == [f"http://127.0.0.1:{PORT}"]


def test_networked_bob_launches_the_same_supervisor_on_windows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}

    def fake_run(command: list[str], cwd: str) -> subprocess.CompletedProcess[str]:
        captured.update(command=command, cwd=cwd)
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(LAUNCH.subprocess, "run", fake_run)
    LAUNCH.bob(Path("/runs/x"), networked_manifest(Path("/runs/x")), 3, 1.0)
    assert captured["cwd"] == "/mnt/c/work/robo-agents"
    assert captured["command"][:2] == [STEP7.WINDOWS_BASH, "-lc"]
    assert captured["command"][2].endswith(
        "exec uv run --locked python scripts/step6_launch.py bob C:/work/step7-run "
        "--max-turns 3 --delay 1.0"
    )


def test_worker_only_run_directory_launches_only_its_bob(
    tmp_path: Path, fake_step6_cli: Path
) -> None:
    directory = tmp_path / "windows-run"
    clone = directory / "bob"
    clone.mkdir(parents=True)
    worker = {
        "worker_only": "bob",
        "slug": STEP6.SANDBOX,
        "run_dir": str(directory),
        "workspaces": {"bob": {"path": str(clone)}},
        "models": {"bob": "claude-sonnet-5"},
    }
    STEP6.save(directory / "run.json", worker)
    (directory / "bob.prompt.md").write_text("worker prompt")
    assert LAUNCH.launch_manifest(directory, "bob") == worker
    with pytest.raises(SystemExit, match="only its own sandbox bob"):
        LAUNCH.launch_manifest(directory, "charlie")
    STEP6.save(directory / "run.json", worker | {"slug": "someone/else"})
    with pytest.raises(SystemExit, match="only its own sandbox bob"):
        LAUNCH.launch_manifest(directory, "bob")
    # A --local-repository dry run's clone has no GitHub slug.
    STEP6.save(directory / "run.json", worker | {"slug": None})
    assert LAUNCH.launch_manifest(directory, "bob")["slug"] is None
    # The fake claude echoes its argv and exits, so the supervisor stops before release.
    with pytest.raises(SystemExit, match="exited before release"):
        LAUNCH.bob(directory, worker, 0, 0)
    record = json.loads((directory / "bob.transcript.jsonl").read_text().splitlines()[0])
    assert record["cwd"] == str(clone)
    arguments = record["args"]
    assert arguments[arguments.index("--mcp-config") + 1] == str(directory / "configs/bob.mcp.json")
    assert STEP7.telemetry_path(directory, worker, "bob") == directory / "bob.telemetry.jsonl"
    manifest = networked_manifest(directory)
    assert STEP7.telemetry_path(directory, manifest, "bob") == Path(
        "/mnt/c/work/step7-run/bob-telemetry.jsonl"
    )


# Preflight.


@pytest.fixture
def preflight_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Every probe passes; each test breaks one."""
    tools = tmp_path / "windows"
    tools.mkdir()
    (tools / "bash.exe").write_text("")
    (tools / "curl.exe").write_text("")
    state: dict[str, Any] = {
        "head": "1" * 40,
        "dirty": False,
        "port_free": True,
        "health": {"status": "ok"},
        "card": {"url": f"http://{ETH0}:{PORT}/a2a"},
        "hub_started": True,
        "hub_env": None,
    }

    def windows_checkout(_: Any) -> tuple[str, bool]:
        return state["head"], state["dirty"]

    def curl(_: Any, url: str) -> Any:
        value = state["health"] if url.endswith("/healthz") else state["card"]
        if isinstance(value, Exception):
            raise value
        return value

    class Hub:
        def __init__(self, directory: Path, env: dict[str, str], url: str) -> None:
            state["hub_env"], state["hub_url"] = env, url

        def __enter__(self) -> None:
            if not state["hub_started"]:
                raise ValueError("preflight hub did not start")

        def __exit__(self, *args: object) -> None:
            return None

    monkeypatch.setattr(STEP7, "windows_checkout", windows_checkout)
    monkeypatch.setattr(STEP7, "port_free", lambda port: state["port_free"])
    monkeypatch.setattr(STEP7, "windows_curl", curl)
    monkeypatch.setattr(STEP7, "preflight_hub", Hub)
    manifest = {
        "topology": "networked",
        "coordination_head": "1" * 40,
        "network": network(),
        "windows": windows() | {"bash": str(tools / "bash.exe"), "curl": str(tools / "curl.exe")},
    }
    return {"state": state, "manifest": manifest, "tools": tools}


def test_preflight_passes_when_windows_reaches_the_advertised_hub(
    tmp_path: Path, preflight_env: dict[str, Any]
) -> None:
    result = STEP7.preflight(tmp_path, preflight_env["manifest"], {"HUB_PORT": str(PORT)})
    assert result["passed"], result
    assert preflight_env["state"]["hub_env"] == {"HUB_PORT": str(PORT)}
    assert preflight_env["state"]["hub_url"] == f"http://127.0.0.1:{PORT}"
    assert {row["check"] for row in result["checks"]} == {
        "wsl_distro",
        "windows_bash",
        "windows_curl",
        "windows_checkout",
        "hub_port_free",
        "windows_healthz",
        "windows_agent_card_url",
    }


@pytest.mark.parametrize(
    ("defect", "check"),
    [
        ("distro", "wsl_distro"),
        ("bash", "windows_bash"),
        ("curl", "windows_curl"),
        ("head", "windows_checkout"),
        ("dirty", "windows_checkout"),
        ("port", "hub_port_free"),
        ("hub", "preflight_error"),
        ("healthz", "windows_healthz"),
        ("card", "windows_agent_card_url"),
        ("loopback_card", "windows_agent_card_url"),
    ],
)
def test_preflight_fails_closed(
    tmp_path: Path, preflight_env: dict[str, Any], defect: str, check: str
) -> None:
    state, manifest = preflight_env["state"], preflight_env["manifest"]
    if defect == "distro":
        manifest["network"]["wsl_distro"] = ""
    elif defect in ("bash", "curl"):
        (preflight_env["tools"] / f"{defect}.exe").unlink()
    elif defect == "head":
        state["head"] = "2" * 40
    elif defect == "dirty":
        state["dirty"] = True
    elif defect == "port":
        state["port_free"] = False
    elif defect == "hub":
        state["hub_started"] = False
    elif defect == "healthz":
        state["health"] = ValueError("curl.exe exited 7")
    elif defect == "card":
        state["card"] = {"url": f"http://{ETH0}:9999/a2a"}
    else:
        state["card"] = {"url": f"http://127.0.0.1:{PORT}/a2a"}
    result = STEP7.preflight(tmp_path, manifest, {})
    assert not result["passed"]
    assert check in {row["check"] for row in result["checks"] if not row["passed"]}


def test_windows_checkout_counts_untracked_files_as_dirty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The clean-checkout preflight sees untracked files; ignored ones stay ignored (r1-2)."""
    commands: list[list[str]] = []
    status = {"stdout": ""}

    def windows_call(_: Any, args: list[str]) -> subprocess.CompletedProcess[str]:
        commands.append(args)
        stdout = "1" * 40 + "\n" if args[1] == "rev-parse" else status["stdout"]
        return subprocess.CompletedProcess(args, 0, stdout, "")

    monkeypatch.setattr(STEP7, "windows_call", windows_call)
    assert STEP7.windows_checkout(windows()) == ("1" * 40, False)
    assert commands[-1] == ["git", "status", "--porcelain"]
    status["stdout"] = "?? stray.py\n"
    assert STEP7.windows_checkout(windows()) == ("1" * 40, True)


def seed_fakes(monkeypatch: pytest.MonkeyPatch, calls: list[tuple[Any, ...]]) -> None:
    def runner(*args: Any, **kwargs: Any) -> str:
        calls.append(args)
        if args[:3] == ("claude", "auth", "status"):
            return '{"loggedIn":true}'
        if args[-1] == "--version":
            return {"claude": "2.1.281 (Claude Code)", "codex": "codex-cli 0.155.1"}.get(
                args[0], "gh version 2"
            )
        if "scripts/bootstrap-workspace.py" in " ".join(map(str, args)):
            name, destination = args[-3], Path(args[-2])
            (destination / ".git/info").mkdir(parents=True)
            return json.dumps({"agent": name, "path": str(destination), "workspace_id": name})
        return "1" * 40

    def github(*args: Any) -> dict[str, Any]:
        if args[0] == "repo":
            return {
                "viewerPermission": "ADMIN",
                "squashMergeAllowed": True,
                "mergeCommitAllowed": False,
                "rebaseMergeAllowed": False,
            }
        return {
            "workflows": [{"state": "active", "path": ".github/workflows/ci.yml", "name": "CI"}]
        }

    monkeypatch.setattr(STEP6, "run", runner)
    monkeypatch.setattr(STEP6, "gh", github)
    monkeypatch.setattr(STEP6, "green", lambda _: True)
    monkeypatch.setattr(STEP7, "read_eth0", lambda: ETH0)
    monkeypatch.setenv("WSL_DISTRO_NAME", "Ubuntu-24.04")


def test_seed_is_refused_while_any_preflight_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[tuple[Any, ...]] = []
    seed_fakes(monkeypatch, calls)
    failed = {"passed": False, "checks": [{"check": "windows_healthz", "passed": False}]}
    monkeypatch.setattr(STEP7, "preflight", lambda *args: failed)

    def never(*args: Any) -> None:
        raise AssertionError("Bob must not be rendered after a failed preflight")

    monkeypatch.setattr(STEP7, "prepare_bob", never)
    directory = tmp_path / "run"
    options = {"hub_port": PORT, **windows()}
    with pytest.raises(ValueError, match="preflight failed.*windows_healthz"):
        STEP6.prepare(
            directory, None, True, Path("scenarios/step7-networked-untrusted.json"), options
        )
    assert not any("issue" in args or "comment" in args for args in calls)
    assert json.loads((directory / "setup.json").read_text())["phase"] == "preparing"
    assert STEP6.load_manifest(directory)["preflight"] == failed
    with pytest.raises(ValueError, match="needs --hub-port"):
        STEP6.prepare(tmp_path / "other", None, False, None, options)
    with pytest.raises(ValueError, match="needs --hub-port"):
        STEP6.prepare(
            tmp_path / "third", None, False, Path("scenarios/step7-networked-untrusted.json")
        )


def test_networked_prepare_renders_hub_charlie_and_windows_bob(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fake_step6_cli: Path
) -> None:
    origin = commit_origin(tmp_path / "origin")
    drive = tmp_path / "c-drive"
    monkeypatch.setattr(
        STEP7, "mount", lambda path: str(drive / PureWindowsPath(path).as_posix()[3:])
    )
    monkeypatch.setattr(STEP7, "read_eth0", lambda: ETH0)
    monkeypatch.setenv("WSL_DISTRO_NAME", "Ubuntu-24.04")
    preflights: list[dict[str, str]] = []

    def preflight(directory: Path, manifest: dict[str, Any], env: dict[str, str]) -> Any:
        preflights.append(env)
        return {"passed": True, "checks": []}

    windows_calls: list[tuple[list[str], dict[str, str]]] = []

    def windows_call(
        options: dict[str, str], args: list[str], exports: dict[str, str] | None = None
    ) -> subprocess.CompletedProcess[str]:
        """Stand in for prepare-run --worker-only bob on Windows."""
        windows_calls.append((args, exports or {}))
        run_dir = Path(STEP7.mount(args[args.index("--run-dir") + 1]))
        clone = run_dir / "bob"
        git("clone", "--quiet", str(origin), str(clone))
        identity = {
            "agent": "bob",
            "path": "C:\\work\\step7-run\\bob",
            "repository": args[args.index("--repository") + 1],
            "workspace_id": BOB_ID,
        }
        (clone / ".git" / STEP7.IDENTITY_FILE).write_text(json.dumps(identity))
        STEP6.save(
            run_dir / "run.json",
            {
                "worker_only": "bob",
                "workspaces": {"bob": identity},
                "versions": {"claude": "2.1.281 (Claude Code)"},
            },
        )
        return subprocess.CompletedProcess(args, 0, "", "")

    monkeypatch.setattr(STEP7, "preflight", preflight)
    monkeypatch.setattr(STEP7, "windows_call", windows_call)
    directory = tmp_path / "run"
    STEP6.prepare(
        directory,
        str(origin),
        False,
        Path("scenarios/step7-networked-untrusted.json"),
        {"hub_port": PORT, **windows()},
    )
    manifest = STEP6.load_manifest(directory)
    assert manifest["topology"] == "networked"
    assert manifest["scenario"] == "scenarios/step7-networked-untrusted.json"
    assert manifest["evidence_prefix"] == "step7"
    assert manifest["implementation_branch"] == f"step7-{manifest['run_id']}/implement"
    assert manifest["network"] == network()
    assert manifest["windows"] == windows()
    bob = manifest["workspaces"]["bob"]
    assert bob["host"] == "windows" and bob["qualified_path"] == "windows:C:/work/step7-run/bob"
    charlie = manifest["workspaces"]["charlie"]
    assert charlie["qualified_path"] == "wsl:" + str(directory / "charlie")
    assert manifest["remote_versions"] == {"bob": "2.1.281 (Claude Code)"}
    assert STEP6.harness_version(manifest, "bob") == "2.1.281"
    # Bob is rendered only on Windows, by prepare-run --worker-only.
    (args, exports), *rest = windows_calls
    assert not rest
    assert args[args.index("--worker-only") + 1] == "bob"
    assert args[args.index("--run-dir") + 1] == WINDOWS_RUN
    assert args[args.index("--hub-url") + 1] == f"http://{ETH0}:{PORT}"
    assert args[args.index("--token-file") + 1] == "//wsl.localhost/Ubuntu-24.04" + str(
        directory / "token"
    )
    assert args[args.index("--repository") + 1] == f"//wsl.localhost/Ubuntu-24.04{origin}"
    assert args[args.index("--bob-model") + 1] == manifest["models"]["bob"]
    assert exports["GIT_CONFIG_KEY_0"] == "safe.directory"
    for name in ("bob", "bob.mcp.json", "bob.prompt.md"):
        assert not (directory / name).exists()
    hub = json.loads((directory / "alice.mcp.json").read_text())["mcpServers"]["hub"]["env"]
    assert preflights == [hub]
    assert hub["HUB_HOST"] == "0.0.0.0" and hub["HUB_PORT"] == str(PORT)
    assert hub["HUB_PUBLIC_URL"] == f"http://{ETH0}:{PORT}"
    codex = tomllib.loads((directory / "codex-home/config.toml").read_text())
    assert codex["mcp_servers"]["hub"]["env"]["HUB_URL"] == f"http://127.0.0.1:{PORT}"
    for env in (hub, codex["mcp_servers"]["hub"]["env"]):
        assert "8420" not in json.dumps({key: env[key] for key in env if key != "HUB_TOKEN"})
    # Canaries sit uncommitted and ignored in both clones; Bob has his worker skill.
    bob_clone = Path(STEP7.local_path(bob))
    for name, clone in (("bob", bob_clone), ("charlie", directory / "charlie")):
        canary = manifest["canaries"][name]
        assert (clone / canary["file"]).read_text().strip() == canary["token"]
        assert git("status", "--porcelain", cwd=clone) == ""
    assert (bob_clone / ".claude/skills/worker").is_dir()
    assert manifest["isolation_check"]["absent_in_charlie"] is True


# Driver observation and the duplicate-workspace probe.


def test_driver_samples_bob_and_probes_once_while_he_is_busy() -> None:
    manifest: dict[str, Any] = {}
    row = {"name": "bob", "status": "idle", "last_heartbeat": stamp(0)}
    snapshot: dict[str, Any] = {"agent": [row], "workflow": [{"status": "active"}]}
    probes: list[str] = []

    def probe(directory: Path, value: dict[str, Any]) -> dict[str, Any]:
        probes.append(row["status"])
        return {"status_code": 409}

    assert STEP7.observe(Path("/run"), manifest, snapshot, probe)
    assert not STEP7.observe(Path("/run"), manifest, snapshot, probe)
    row["status"] = "busy"
    assert STEP7.observe(Path("/run"), manifest, snapshot, probe)
    row["last_heartbeat"] = stamp(5)
    assert STEP7.observe(Path("/run"), manifest, snapshot, probe)
    assert probes == ["busy"]
    assert [sample["last_heartbeat"] for sample in manifest["heartbeat_samples"]] == [
        stamp(0),
        stamp(0),
        stamp(5),
    ]
    assert STEP7.observing(snapshot)
    row["status"] = "released"
    assert not STEP7.observing(snapshot)
    assert not STEP7.observing({"agent": [], "workflow": [{"status": "done"}]})
    assert not STEP7.observe(Path("/run"), {}, {"agent": []}, probe)


def test_networked_driver_keeps_sampling_after_the_disturbances(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    directory = tmp_path / "run"
    directory.mkdir()
    STEP6.save(directory / "run.json", networked_manifest(directory))
    passes: list[str] = []
    statuses = iter(["busy", "busy", "busy", "released"])

    def snapshot(_: Path) -> dict[str, Any]:
        return {"agent": [{"name": "bob", "status": next(statuses)}], "workflow": []}

    def disturb(_: Path) -> bool:
        passes.append("driver")
        return True

    monkeypatch.setattr(STEP6, "audit", snapshot)
    monkeypatch.setattr(STEP6, "driver_once", disturb)
    monkeypatch.setattr(STEP7, "probe_duplicate", lambda *args: {"status_code": 409})
    monkeypatch.setattr(STEP6.time, "sleep", lambda _: None)
    STEP6.drive(directory, 60)
    assert passes == ["driver"]  # disturbances end once; the driver never runs again
    assert len(STEP6.load_manifest(directory)["heartbeat_samples"]) == 2


def workspace_clone(path: Path, origin: Path, agent: str, workspace_id: str) -> Path:
    git("clone", "--quiet", str(origin), str(path))
    descriptor = os.open(path / ".git" / STEP7.IDENTITY_FILE, os.O_WRONLY | os.O_CREAT, 0o600)
    with os.fdopen(descriptor, "w") as stream:
        json.dump(
            {
                "agent": agent,
                "path": str(path),
                "repository": str(origin),
                "workspace_id": workspace_id,
            },
            stream,
        )
    return path


async def test_duplicate_identity_is_refused_with_409_while_bob_is_live(
    app: FastAPI, tmp_path: Path
) -> None:
    origin = commit_origin(tmp_path / "origin")
    directory = tmp_path / "run"
    driver = workspace_clone(directory / "driver", origin, "driver", "d" * 64)
    bob = workspace_clone(directory / "bob", origin, "bob", BOB_ID)
    manifest = {
        "network": network(),
        "workspaces": {
            "bob": {"path": str(bob), "host": "wsl"},
            "driver": {"path": str(driver)},
        },
    }
    clone, workspace_id = STEP7.duplicate_clone(directory, manifest)
    assert workspace_id == BOB_ID
    assert git("remote", "get-url", "origin", cwd=clone) == str(origin)
    settings = WorkerSettings.from_env(
        {
            "HUB_URL": "http://hub.test",
            "HUB_TOKEN": TOKEN,
            "AGENT_NAME": STEP7.PROBE_AGENT,
            "HUB_WORKSPACE": str(clone),
            "HUB_MAX_RETRIES": "0",
        }
    )
    assert settings.profile.workspace_id == BOB_ID
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        # Nobody holds the workspace: the same probe is accepted (the failing fixture).
        assert await STEP7.duplicate_check_in(settings, transport) == (200, None)
        app.state.store.release_agent(STEP7.PROBE_AGENT)
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://hub.test",
            headers={"Authorization": f"Bearer {TOKEN}"},
        ) as client:
            ready = message(
                "READY",
                metadata={
                    MetaKeys.AGENT: "bob",
                    MetaKeys.HARNESS: "claude-code",
                    MetaKeys.SCHEMA_VERSION: SCHEMA_VERSION,
                    MetaKeys.OPERATION_ID: "bob-check-in",
                    MetaKeys.WORKER_INSTANCE_ID: "bob-process",
                    MetaKeys.WORKSPACE_ID: BOB_ID,
                },
            )
            response = await client.post("/a2a", json=rpc("message/send", ready))
            assert response.status_code == 200
        status, refusal = await STEP7.duplicate_check_in(settings, transport)
    assert status == 409
    assert refusal == f"workspace {BOB_ID} is occupied by live agent bob"


def test_probe_record_brackets_the_check_in_with_bobs_hub_row(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    origin = commit_origin(tmp_path / "origin")
    directory = tmp_path / "run"
    (directory / "state").mkdir(parents=True)
    (directory / "token").write_text(TOKEN)
    columns = ", ".join(("name", *STEP7.SAMPLED))
    with contextlib.closing(sqlite3.connect(directory / "state/hub.db")) as db:
        db.execute(f"CREATE TABLE agent ({columns})")
        db.execute(
            "INSERT INTO agent VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            ("bob", "busy", "task", stamp(0), VETHERNET, "bob-instance", "ctx", BOB_ID),
        )
        db.commit()
    manifest = {
        "network": network(),
        "workspaces": {
            "bob": {"path": str(workspace_clone(directory / "bob", origin, "bob", BOB_ID))},
            "driver": {"path": str(workspace_clone(directory / "driver", origin, "d", "d" * 64))},
        },
    }
    seen: list[Any] = []

    async def check_in(settings: WorkerSettings, transport: Any = None) -> tuple[int, str]:
        seen.append(settings)
        return 409, f"workspace {BOB_ID} is occupied by live agent bob"

    monkeypatch.setattr(STEP7, "duplicate_check_in", check_in)
    record = STEP7.probe_duplicate(directory, manifest)
    assert seen[0].hub_url == f"http://127.0.0.1:{PORT}"
    assert seen[0].agent_name == STEP7.PROBE_AGENT
    assert seen[0].profile.workspace_id == BOB_ID
    assert record["status_code"] == 409 and record["refused"] is True
    assert record["bob_before"] == record["bob_after"]
    assert record["bob_before"]["status"] == "busy"
    assert record["bob_before"]["worker_instance_id"] == "bob-instance"


# Canaries.


def test_canaries_are_uncommitted_and_leaks_are_found(tmp_path: Path) -> None:
    origin = commit_origin(tmp_path / "origin")
    manifest: dict[str, Any] = {
        "run_id": "unique",
        "workspaces": {
            name: {
                "path": str(workspace_clone(tmp_path / name, origin, name, name * 8)),
                "host": "wsl",
            }
            for name in ("bob", "charlie")
        },
    }
    manifest["canaries"] = STEP7.place_canaries(manifest)
    assert STEP7.place_canaries(manifest) == manifest["canaries"]  # idempotent on retry
    for name in ("bob", "charlie"):
        assert git("status", "--porcelain", cwd=tmp_path / name) == ""
    clean = STEP7.canary_facts(manifest)
    assert clean == {
        name: {"present_in_own": True, "found_in_other": []} for name in ("bob", "charlie")
    }
    token = manifest["canaries"]["bob"]["token"]
    (tmp_path / "charlie/notes.txt").write_text("copied " + token)
    (tmp_path / "bob/.git/.step7-canary-charlie").write_text("")
    (tmp_path / "charlie/.step7-canary-charlie").unlink()
    leaked = STEP7.canary_facts(manifest)
    assert leaked["charlie"]["found_in_other"] == [".git/.step7-canary-charlie"]
    assert leaked["charlie"]["present_in_own"] is False
    assert leaked["bob"]["found_in_other"] == ["notes.txt"]


# Verifier checks 1-5.


def test_networked_proof_passes_every_step6_and_step7_check(
    networked: tuple[Any, ...], proof: tuple[Any, ...]
) -> None:
    evidence = STEP6.evaluate(*networked)
    assert evidence["passed"], evidence["failed_checks"]
    step7 = [name for name in evidence["checks"] if name.startswith("step7_")]
    assert len(step7) == 12
    # The localhost topology never runs the Step 7 checks.
    assert not any(name.startswith("step7_") for name in STEP6.evaluate(*proof)["checks"])


@pytest.mark.parametrize(
    ("defect", "check"),
    [
        ("bob_loopback", "step7_bob_remote_peer"),
        ("bob_wsl_address", "step7_bob_remote_peer"),
        ("bob_sampled_loopback", "step7_bob_remote_peer"),
        ("charlie_remote", "step7_charlie_loopback_peer"),
        ("bob_dialed_loopback", "step7_bob_dialed_public_url"),
        ("no_session_record", "step7_bob_dialed_public_url"),
        ("heartbeat_gap", "step7_heartbeat_gaps"),
        ("rejected_heartbeat", "step7_heartbeat_gaps"),
        ("no_assignment_record", "step7_heartbeat_gaps"),
        ("agent_lost", "step7_no_agent_lost"),
        ("stale_hub_heartbeat", "step7_hub_heartbeat_advanced"),
        ("shared_workspace", "step7_distinct_workspace_ids"),
        ("changed_workspace", "step7_stable_workspace_ids"),
        ("no_check_in_event", "step7_stable_workspace_ids"),
        ("duplicate_accepted", "step7_duplicate_refused"),
        ("no_probe", "step7_duplicate_refused"),
        ("probe_while_bob_away", "step7_duplicate_refused"),
        ("bob_replaced", "step7_duplicate_refused"),
        ("probe_registered", "step7_duplicate_refused"),
        ("bob_stopped_after_probe", "step7_duplicate_refused"),
        ("canary_leaked", "step7_canary_isolation"),
        ("canary_missing", "step7_canary_isolation"),
        ("charlie_reads_mount", "step7_charlie_no_cross_host_access"),
        ("charlie_reads_windows_spelling", "step7_charlie_no_cross_host_access"),
        ("charlie_cds_relative", "step7_charlie_no_cross_host_access"),
        ("charlie_edits_mount", "step7_charlie_no_cross_host_access"),
        ("bob_reads_unc", "step7_bob_no_cross_host_access"),
        ("bob_reads_wsl_path", "step7_bob_no_cross_host_access"),
    ],
)
def test_each_step7_check_fails_closed(networked: tuple[Any, ...], defect: str, check: str) -> None:
    manifest, snapshot, facts, traces = copy.deepcopy(networked)
    agents = {row["name"]: row for row in snapshot["agent"]}
    telemetry = facts["telemetry"]["bob"]
    probe = manifest["duplicate_probe"]
    if defect == "bob_loopback":
        agents["bob"]["last_remote_addr"] = "127.0.0.1"
    elif defect == "bob_wsl_address":
        agents["bob"]["checkin_remote_addr"] = ETH0
    elif defect == "bob_sampled_loopback":
        manifest["heartbeat_samples"][0]["last_remote_addr"] = "::1"
    elif defect == "charlie_remote":
        agents["charlie"]["checkin_remote_addr"] = VETHERNET
    elif defect == "bob_dialed_loopback":
        telemetry[2]["hub_url"] = f"http://127.0.0.1:{PORT}"
    elif defect == "no_session_record":
        del telemetry[0]
    elif defect == "heartbeat_gap":
        manifest["network"]["lost_after_s"] = 30.0  # the fixture's beats are 30 s apart
    elif defect == "rejected_heartbeat":
        for row in telemetry:
            if row.get("event") == "heartbeat":
                row["accepted"] = False
        manifest["network"]["lost_after_s"] = 45.0
    elif defect == "no_assignment_record":
        telemetry.remove(next(row for row in telemetry if row.get("tool") == "await_assignment"))
    elif defect == "agent_lost":
        snapshot["event"].append({"kind": "agent_lost", "payload_json": '{"agent": "charlie"}'})
    elif defect == "stale_hub_heartbeat":
        manifest["heartbeat_samples"][1]["last_heartbeat"] = stamp(0)
    elif defect == "shared_workspace":
        manifest["workspaces"]["charlie"]["workspace_id"] = BOB_ID
        agents["charlie"]["workspace_id"] = BOB_ID
    elif defect == "changed_workspace":
        snapshot["event"].append(
            {
                "kind": "agent_checked_in",
                "payload_json": json.dumps({"agent": "bob", "workspace_id": "e" * 64}),
            }
        )
    elif defect == "no_check_in_event":
        snapshot["event"] = snapshot["event"][1:]
    elif defect == "duplicate_accepted":
        probe.update(status_code=200, refused=False, message=None)
    elif defect == "no_probe":
        del manifest["duplicate_probe"]
    elif defect == "probe_while_bob_away":
        probe["bob_before"]["status"] = "lost"
    elif defect == "bob_replaced":
        probe["bob_after"]["worker_instance_id"] = "another-instance"
    elif defect == "probe_registered":
        snapshot["agent"].append({"name": STEP7.PROBE_AGENT, "status": "idle"})
    elif defect == "bob_stopped_after_probe":
        manifest["heartbeat_samples"][1]["sampled_at"] = stamp(0)
    elif defect == "canary_leaked":
        facts["canaries"]["bob"]["found_in_other"] = [".step7-canary-bob"]
    elif defect == "canary_missing":
        facts["canaries"]["charlie"]["present_in_own"] = False
    elif defect == "charlie_reads_mount":
        traces["charlie"].append(
            {"name": "Bash", "input": {"command": "cat /mnt/c/work/step7-run/bob/step6_x.py"}}
        )
    elif defect == "charlie_reads_windows_spelling":
        traces["charlie"].append(
            {"name": "Bash", "input": {"command": "type C:\\WORK\\step7-run\\bob\\x.py"}}
        )
    elif defect == "charlie_cds_relative":
        traces["charlie"].append(
            {
                "name": "Bash",
                "input": {"command": "cd ../../mnt/c/work/step7-run && cat bob/step6_x.py"},
            }
        )
    elif defect == "charlie_edits_mount":
        traces["charlie"].append(
            {"name": "Edit", "input": {"file_path": "../../mnt/c/work/step7-run/bob/x.py"}}
        )
    elif defect == "bob_reads_unc":
        traces["bob"].append(
            {
                "name": "Read",
                "input": {"file_path": "\\\\wsl.localhost\\Ubuntu-24.04\\runs\\charlie\\x.py"},
            }
        )
    else:
        traces["bob"].append({"name": "Bash", "input": {"command": "wsl cat /runs/charlie/x.py"}})
    evidence = STEP6.evaluate(manifest, snapshot, facts, traces)
    assert not evidence["passed"]
    assert check in evidence["failed_checks"], evidence["failed_checks"]


def test_workers_own_paths_are_not_cross_host_access(networked: tuple[Any, ...]) -> None:
    manifest, snapshot, facts, traces = copy.deepcopy(networked)
    traces["bob"] += [
        {"name": "Bash", "input": {"command": "cd /c/work/step7-run/bob && git status"}},
        {"name": "Bash", "input": {"command": "ls C:/work/step7-run/bobby"}},
    ]
    traces["charlie"] += [
        {"name": "Bash", "input": {"command": "cat /runs/charlie/x.py /mnt/c/work/other/bob"}},
        {"name": "Read", "input": {"file_path": "/runs/charlie/step6_x.py"}},
    ]
    evidence = STEP6.evaluate(manifest, snapshot, facts, traces)
    assert evidence["passed"], evidence["failed_checks"]


def test_heartbeat_gaps_are_measured_in_bobs_own_clock() -> None:
    records = [
        {
            "event": "tool_call",
            "tool": "await_assignment",
            "phase": "success",
            "task_id": "t",
            "timestamp": at(0),
        },
        {"event": "heartbeat", "phase": "success", "accepted": True, "timestamp": at(2)},
        {"event": "heartbeat", "phase": "error", "timestamp": at(4)},
        {
            "event": "tool_call",
            "tool": "submit_result",
            "phase": "success",
            "task_id": "t",
            "timestamp": at(5),
        },
    ]
    assert STEP7.heartbeat_gaps(records, ["t", "missing"]) == {"t": 180.0, "missing": None}


# Collection and export.


def test_collect_pulls_bobs_windows_transcript_and_telemetry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    drive = tmp_path / "c-drive"
    monkeypatch.setattr(
        STEP7, "mount", lambda path: str(drive / PureWindowsPath(path).as_posix()[3:])
    )
    windows_run = Path(STEP7.mount(WINDOWS_RUN))
    windows_run.mkdir(parents=True)
    (windows_run / "bob.transcript.jsonl").write_text('{"transcript": 1}\n')
    (windows_run / "bob-telemetry.jsonl").write_text('{"telemetry": 1}\n')
    directory = tmp_path / "run"
    directory.mkdir()
    STEP7.pull_windows(directory, {"windows": windows()})
    assert (directory / "bob.transcript.jsonl").read_text() == '{"transcript": 1}\n'
    assert (directory / "bob.telemetry.jsonl").read_text() == '{"telemetry": 1}\n'


def spellings(directory: Path) -> list[str]:
    """Every way the harness records the two run roots and a Windows profile."""
    unc = str(directory).replace("/", "\\")
    return [
        "C:/work/step7-run/bob/step6_x.py",
        "C:\\work\\step7-run\\bob\\step6_x.py",
        "c:\\WORK\\Step7-Run",
        "/c/work/step7-run/bob",
        "/mnt/c/work/step7-run/bob-telemetry.jsonl",
        "windows:C:/work/step7-run/bob",
        '"C:\\\\work\\\\step7-run\\\\bob"',
        str(directory / "charlie"),
        f"wsl:{directory}/charlie",
        f"\\\\wsl.localhost\\Ubuntu-24.04{unc}\\token",
        f"\\\\wsl$\\Ubuntu-24.04{unc}\\token",
        f"//wsl.localhost/Ubuntu-24.04{directory}/token",
        "C:\\Users\\nates\\.claude\\projects\\x.jsonl",
        "C:/Users/nates/AppData",
        "/c/Users/nates/.local/bin/uv",
        "/mnt/c/Users/nates/.claude",
        # A profile name with a space is masked whole, in every spelling (r1-1).
        "C:/Users/John Doe/AppData/Local",
        "C:\\Users\\John Doe\\.claude\\x.jsonl",
        "cd '/c/Users/John Doe/' && ls",
        '"/mnt/c/Users/John Doe"',
        # No separator after the name: masked through to the end of the string.
        "cat /c/Users/John Doe",
    ]


def export_run(directory: Path, prefix: str | None) -> dict[str, Any]:
    manifest = networked_manifest(directory)
    if prefix is None:
        del manifest["evidence_prefix"], manifest["topology"]
    STEP6.save(directory / "run.json", manifest)
    (directory / "token").write_text("private-token-" + "0" * 64)
    STEP6.save(directory / "evidence.json", {"passed": True, "run_id": "unique"})
    agents = [
        {"name": "bob", "checkin_remote_addr": VETHERNET, "last_remote_addr": VETHERNET},
        {"name": "charlie", "checkin_remote_addr": "127.0.0.1", "last_remote_addr": "127.0.0.1"},
    ]
    STEP6.save(
        directory / "hub-audit.json",
        {"workflow": [], "agent": agents, "task": [], "decision": [], "event": []},
    )
    STEP6.save(
        directory / "github-facts.json",
        {
            "head_commit": {"parents": []},
            "merge_commit": {"sha": "m", "parents": [], "commit": {"tree": {"sha": "t"}}},
            "telemetry": {"bob": [{"hub_url": f"http://{ETH0}:{PORT}"}]},
        },
    )
    traces = {
        "bob": [{"name": "Bash", "input": {"command": text}} for text in spellings(directory)]
    }
    STEP6.save(directory / "tool-audit.json", traces)
    return manifest


def test_networked_export_masks_both_run_roots_and_windows_profiles(tmp_path: Path) -> None:
    directory = tmp_path / "wsl-run"
    directory.mkdir()
    export_run(directory, "step7")
    destination = tmp_path / "exports"
    STEP6.export_evidence(directory, destination)
    names = sorted(path.name for path in destination.iterdir())
    assert names == [
        f"step7-unique.{label}.json"
        for label in ("evidence", "github-facts", "hub-audit", "manifest", "tool-audit")
    ]
    text = "\n".join(path.read_text() for path in destination.iterdir())
    for path in destination.iterdir():
        json.loads(path.read_text())
    flat = text.replace("\\", "/").lower()
    assert "work/step7-run" not in flat
    assert str(directory).lower() not in flat
    assert "nates" not in flat
    assert "john" not in flat and "doe" not in flat
    assert "private-token-" not in text
    assert "windows:/RUN" in text and "wsl:/RUN" in text
    assert "C:/Users/<user>/AppData" in text and "/c/Users/<user>/.local" in text
    # The private NAT addresses are check 1's evidence and stay.
    assert VETHERNET in text and ETH0 in text
    tool_audit = json.loads((destination / "step7-unique.tool-audit.json").read_text())
    commands = [call["input"]["command"] for call in tool_audit["bob"]]
    assert commands[0] == "windows:/RUN/bob/step6_x.py"
    assert commands[5] == "windows:/RUN/bob"
    assert commands[8] == "wsl:/RUN/charlie"
    assert commands[12] == "C:\\Users\\<user>\\.claude\\projects\\x.jsonl"
    assert commands[16:] == [
        "C:/Users/<user>/AppData/Local",
        "C:\\Users\\<user>\\.claude\\x.jsonl",
        "cd '/c/Users/<user>/' && ls",
        '"/mnt/c/Users/<user>"',
        "cat /c/Users/<user>",
    ]
    audit = json.loads((destination / "step7-unique.hub-audit.json").read_text())
    assert {row["name"]: row["last_remote_addr"] for row in audit["agent"]} == {
        "bob": VETHERNET,
        "charlie": "127.0.0.1",
    }


# Profile names of characters Windows allows in a name (r2-1), each marked Zq.
PROFILE_NAMES = [
    "Zq O'Connor",
    "Zqé Ñúñez",
    "Zq (x86) & Co;1,=+!@#$%^~`[]{}",
    "\U0001f600 Zq smile",
    "ZQJOHN~1",
]


@pytest.mark.parametrize("name", PROFILE_NAMES)
def test_networked_export_masks_every_valid_profile_name(tmp_path: Path, name: str) -> None:
    directory = tmp_path / "wsl-run"
    directory.mkdir()
    export_run(directory, "step7")
    paths = [
        f"{root}{separator.join(['Users', name, 'AppData'])}"
        for root, separator in (("C:/", "/"), ("C:\\", "\\"), ("/c/", "/"), ("/mnt/c/", "/"))
    ] + [f"C:\\Documents and Settings\\{name}\\x", f"cd '/c/Users/{name}' && ls"]
    texts = [
        *paths,
        *(json.dumps({"input": path}) for path in paths),  # JSON text inside a string
        *(json.dumps(path).replace("/", "\\/") for path in paths),  # with \/ escapes
    ]
    traces = json.loads((directory / "tool-audit.json").read_text())
    traces["bob"] = [{"name": "Bash", "input": {"command": text}} for text in texts]
    STEP6.save(directory / "tool-audit.json", traces)
    destination = tmp_path / "exports"
    STEP6.export_evidence(directory, destination)
    raw = (destination / "step7-unique.tool-audit.json").read_text()
    commands = [call["input"]["command"] for call in json.loads(raw)["bob"]]
    for text in (raw, *commands):
        assert "zq" not in text.lower()
        assert not any(character in text for character in "éÑúñ\U0001f600")
        assert not re.search(r"\\+u(00e9|00d1|00fa|00f1|d83d|de00)", text, re.IGNORECASE)
    assert commands[0] == "C:/Users/<user>/AppData"
    assert commands[1] == "C:\\Users\\<user>\\AppData"
    assert commands[4] == "C:\\Users\\<user>\\x"  # Documents and Settings, masked too
    assert commands[5] == "cd '/c/Users/<user>"
    for nested in commands[len(paths) :]:
        json.loads(nested)  # nested JSON text stays parseable after masking
    assert json.loads(commands[len(paths)]) == {"input": "C:/Users/<user>/AppData"}


def test_non_ascii_run_roots_are_masked_inside_nested_json() -> None:
    manifest = {"windows": {"run_dir": "C:/wörk/step7-rün"}}
    directory = Path("/home/zoë/run")
    document = {
        "windows": json.dumps({"path": "C:\\wörk\\step7-rün\\bob"}),
        "wsl": json.dumps({"path": "/home/zoë/run/charlie"}),
    }
    masked = STEP7.mask_document(document, directory, manifest)
    assert json.loads(masked["windows"]) == {"path": "windows:/RUN\\bob"}
    assert json.loads(masked["wsl"]) == {"path": "wsl:/RUN/charlie"}


@pytest.mark.parametrize("where", ["bob_transcript", "bob_telemetry", "windows_path"])
def test_networked_export_refuses_a_planted_token_and_writes_nothing(
    tmp_path: Path, where: str
) -> None:
    directory = tmp_path / "wsl-run"
    directory.mkdir()
    export_run(directory, "step7")
    token = (directory / "token").read_text()
    if where == "bob_telemetry":
        facts = json.loads((directory / "github-facts.json").read_text())
        facts["telemetry"]["bob"].append({"error": "Bearer " + token})
        STEP6.save(directory / "github-facts.json", facts)
    else:
        traces = json.loads((directory / "tool-audit.json").read_text())
        planted = token if where == "bob_transcript" else f"C:\\work\\step7-run\\{token}"
        traces["bob"].append({"name": "Bash", "input": {"command": planted}})
        STEP6.save(directory / "tool-audit.json", traces)
    destination = tmp_path / "exports"
    with pytest.raises(ValueError, match="credential detected"):
        STEP6.export_evidence(directory, destination)
    assert not destination.exists()


def test_localhost_export_keeps_the_step6_prefix(tmp_path: Path) -> None:
    directory = tmp_path / "wsl-run"
    directory.mkdir()
    export_run(directory, None)
    destination = tmp_path / "exports"
    STEP6.export_evidence(directory, destination)
    assert {path.name.split(".")[0] for path in destination.iterdir()} == {"step6-unique"}
    text = "\n".join(path.read_text() for path in destination.iterdir())
    assert "/RUN/charlie" in text and "windows:/RUN" not in text


def test_step7_helpers_import_without_the_workspace_packages() -> None:
    """prepare-step6-demo.sh runs step6.py (and so step7.py) under a bare python3."""
    code = (
        "import builtins, sys\n"
        "real = builtins.__import__\n"
        "def guard(name, *args, **kwargs):\n"
        "    if name.split('.')[0] in ('agent_hub', 'agent_hub_common', 'worker_mcp', 'httpx'):\n"
        "        raise ImportError(name)\n"
        "    return real(name, *args, **kwargs)\n"
        "builtins.__import__ = guard\n"
        f"sys.path.insert(0, {str(ROOT / 'scripts')!r})\n"
        "import step6, step7\n"
    )
    subprocess.run([sys.executable, "-c", code], check=True)
