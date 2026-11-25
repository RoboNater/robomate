"""Doctor preparation is local, credential-free and separate from team identity."""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("denise", ROOT / "scripts/denise.py")
assert SPEC and SPEC.loader
DENISE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(DENISE)

AGENTS = {
    "alice": {"harness": "claude-code", "model": "alice-model", "effort": "alice-effort"},
    "bob": {"harness": "codex", "model": "bob-model", "effort": "bob-effort"},
    "charlie": {"harness": "claude", "model": "charlie-model", "effort": "charlie-effort"},
}


@pytest.mark.parametrize("source", ["alice", "bob", "charlie"])
def test_inherits_final_agent_settings(source: str) -> None:
    resolved = DENISE.resolve_settings(AGENTS, settings_from=source)
    assert resolved["settings_from"] == source
    assert resolved["model"] == AGENTS[source]["model"]
    assert resolved["effort"] == AGENTS[source]["effort"]
    assert resolved["status"] == "ready"
    assert DENISE.resolve_settings(AGENTS) == DENISE.resolve_settings(AGENTS, settings_from="bob")


def test_overrides_and_harness_defaults_without_value_allowlists() -> None:
    resolved = DENISE.resolve_settings(AGENTS, harness="CLAUDE")
    assert (resolved["harness"], resolved["model"], resolved["effort"]) == ("claude-code", "", "")
    same = DENISE.resolve_settings(AGENTS, harness="Codex")
    assert (same["model"], same["effort"]) == ("bob-model", "bob-effort")
    explicit = DENISE.resolve_settings(AGENTS, model='future model "x"', effort="future effort")
    assert explicit["model"] == 'future model "x"' and explicit["effort"] == "future effort"
    none = DENISE.resolve_settings(AGENTS, settings_from="none", harness="codex")
    assert none["model"] == none["effort"] == ""


@pytest.mark.parametrize("harness", ["devin", "opencode", "antigravity"])
@pytest.mark.parametrize("source", ["alice", "bob", "charlie"])
def test_unsupported_inheritance_skips_even_with_explicit_selector(
    harness: str, source: str
) -> None:
    agents = {**AGENTS, source: {"harness": harness, "model": "future-model", "effort": ""}}
    resolved = DENISE.resolve_settings(agents, settings_from=source)
    assert resolved["status"] == "skipped"
    assert harness in resolved["skip_reason"]
    assert "--denise-harness" in resolved["skip_reason"]
    with pytest.raises(ValueError, match="explicit --denise-harness"):
        DENISE.resolve_settings(agents, settings_from=source, harness=harness)
    assert (
        DENISE.resolve_settings(agents, settings_from=source, harness="codex")["status"] == "ready"
    )


@pytest.mark.parametrize("harness", ["devin", "opencode"])
def test_effort_adapter_capability_is_separate_from_harness_support(
    harness: str,
) -> None:
    agents = {**AGENTS, "bob": {"harness": harness, "model": "", "effort": "new-value"}}
    inherited = DENISE.resolve_settings(agents)
    assert inherited["effort"] == ""
    assert inherited["status"] == "skipped"
    assert f"inherited harness {harness} is not yet supported" in inherited["skip_reason"]
    assert "inherited effort 'new-value'" in inherited["effort_note"]
    assert f"{harness} default effort" in inherited["effort_note"]

    joined = DENISE.resolve_settings(agents, joined=True)
    assert joined["status"] == ("ready" if harness == "opencode" else "skipped")
    assert joined["effort"] == ""
    with pytest.raises(ValueError, match="explicit effort is unsupported"):
        DENISE.resolve_settings(agents, effort="another-new-value", joined=True)


def test_joined_antigravity_inherits_model_and_effort() -> None:
    agents = {**AGENTS, "bob": {"harness": "agy", "model": "test-model", "effort": "high"}}
    settings = DENISE.resolve_settings(agents, joined=True)
    assert settings["status"] == "ready" and settings["harness"] == "antigravity"
    assert (settings["model"], settings["effort"]) == ("test-model", "high")


@pytest.mark.parametrize(
    "kwargs",
    [
        {"settings_from": "worker"},
        {"settings_from": "none"},
        {"harness": "unknown"},
        {"settings_from": False},
        {"model": 4},
        {"effort": []},
        {"harness": None},
    ],
)
def test_invalid_structure_is_an_early_error(kwargs: dict[str, Any]) -> None:
    with pytest.raises(ValueError):
        DENISE.resolve_settings(AGENTS, **kwargs)


def git(path: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(path), *args],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def manifest(tmp_path: Path) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "run_dir": str(tmp_path / "run 'with quotes'"),
        "hub_id": "selected-hub",
        "hub_repo": str(tmp_path / "target"),
        "state_dir": str(tmp_path / "target-state"),
        "repository": "https://user:SECRET@example.test/owner/project.git?token=SECRET#SECRET",
        "workspaces": {"bob": {"path": str(tmp_path / "worker")}},
    }


def test_legacy_context_and_renderer_never_read_secrets_or_contact_hub(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    saved = manifest(tmp_path)
    config = tmp_path / "original.toml"
    config.write_text('token = "SECRET"')
    monkeypatch.setenv("ROBOMATE_TOKEN", "SECRET")
    monkeypatch.setenv("HUB_TOKEN", "SECRET")
    root = tmp_path / "separate tooling"
    root.mkdir()
    settings = DENISE.resolve_settings(AGENTS)
    metadata = DENISE.render(saved, root, settings, original_config=config)
    assert "script" not in metadata
    assert metadata["startup_status"] == "pending-step-2"
    assert metadata["tooling_commit"] is None
    context = json.loads(Path(metadata["context"]).read_text())
    assert context["run"]["hub_id"] == "selected-hub"
    assert context["target"]["origin"] == "https://example.test/owner/project.git"
    assert context["original_config"]["path"] == str(config)
    assert context["hub"]["checkout"]["access"] == "missing"
    assert context["hub"]["git_common_dir"]["access"] == "unknown"
    assert context["agents"]["bob"]["instructions"]["access"] == "missing"
    assert context["agents"]["charlie"]["checkout"]["access"] == "unknown"
    assert "SECRET" not in json.dumps(context)
    assert "SECRET" not in Path(metadata["startup"]).read_text()
    assert not (tmp_path / "target").exists()
    assert not (tmp_path / "target-state").exists()
    assert not list(Path(saved["run_dir"]).glob("start-denise*"))
    for key in ("reports", "configs", "workdir", "tmp"):
        assert Path(metadata[key]).is_dir() and not list(Path(metadata[key]).iterdir())
    assert {key: value for key, value in saved.items() if key == "denise"} == {}
    roots = context["access_inventory"]["roots"]
    assert {item["path"] for item in roots if item["mode"] == "write"} == {
        saved["run_dir"],
        saved["hub_repo"],
        saved["state_dir"],
        str(Path(saved["run_dir"]) / "alice-runtime"),
        saved["workspaces"]["bob"]["path"],
    }
    assert all(item["mode"] == "read" for item in roots if item["path"] == str(root))
    assert all(item["path"] != str(tmp_path) for item in roots)


@pytest.mark.parametrize("bare", [False, True])
def test_inventory_resolves_git_common_directories_in_worktrees_and_bare_layouts(
    tmp_path: Path,
    bare: bool,
) -> None:
    target = tmp_path / "target with spaces 'quoted'"
    target.mkdir()
    git(target, "init", "-b", "main")
    git(
        target,
        "-c",
        "user.name=Test",
        "-c",
        "user.email=t@example.test",
        "commit",
        "--allow-empty",
        "-m",
        "baseline",
    )
    common = target / ".git"
    if bare:
        common = tmp_path / "bare.git"
        git(tmp_path, "clone", "--bare", str(target), str(common))
    worker = tmp_path / "linked 'worker'"
    git(common if bare else target, "worktree", "add", "-b", "worker", str(worker))
    assert (worker / ".git").is_file()
    saved = manifest(tmp_path)
    saved["hub_repo"] = str(common if bare else target)
    saved["workspaces"]["bob"]["path"] = str(worker)
    (worker / "AGENTS.md").write_text("Target instructions.\n")
    context = DENISE.build_context(saved, ROOT, DENISE.resolve_settings(AGENTS))
    assert context["agents"]["bob"]["git_common_dir"]["path"] == str(common.resolve())
    assert context["hub"]["git_common_dir"]["path"] == str(common.resolve())
    assert context["agents"]["bob"]["instructions"]["access"] == "present"
    roots = context["access_inventory"]["roots"]
    assert len([item for item in roots if item["path"] == str(common.resolve())]) == 1
    assert context["tooling"]["root"]["path"] != context["hub"]["checkout"]["path"]


@pytest.mark.parametrize("bare", [False, True])
def test_plain_directories_never_grant_an_enclosing_repository(
    tmp_path: Path,
    bare: bool,
) -> None:
    enclosing = tmp_path / "unrelated parent"
    enclosing.mkdir()
    git(enclosing, "init", *(["--bare"] if bare else []))
    saved = manifest(enclosing)
    runtime = Path(saved["run_dir"]) / "alice-runtime"
    runtime.mkdir(parents=True)
    Path(saved["hub_repo"]).mkdir()
    Path(saved["workspaces"]["bob"]["path"]).mkdir()

    context = DENISE.build_context(saved, ROOT, DENISE.resolve_settings(AGENTS))

    for indexed in (context["agents"]["alice"], context["hub"], context["agents"]["bob"]):
        assert indexed["checkout"]["access"] == "present"
        assert indexed["git_common_dir"]["path"] is None
    write_roots = {
        item["path"] for item in context["access_inventory"]["roots"] if item["mode"] == "write"
    }
    assert str(enclosing) not in write_roots
    assert str(enclosing / ".git") not in write_roots
    assert str(runtime) in write_roots


def test_symlink_paths_resolve_to_their_actual_roots(tmp_path: Path) -> None:
    target = tmp_path / "actual"
    target.mkdir()
    git(target, "init")
    alias = tmp_path / "alias"
    try:
        alias.symlink_to(target, target_is_directory=True)
    except OSError as exc:
        if getattr(exc, "winerror", None) == 1314:
            pytest.skip("Windows Developer Mode or symlink privilege required")
        raise
    saved = manifest(tmp_path)
    saved["workspaces"]["bob"]["path"] = str(alias)
    context = DENISE.build_context(saved, ROOT, DENISE.resolve_settings(AGENTS))
    assert context["agents"]["bob"]["checkout"]["path"] == str(target)
    assert context["agents"]["bob"]["git_common_dir"]["path"] == str(target / ".git")
    assert str(alias) not in {item["path"] for item in context["access_inventory"]["roots"]}


def test_remote_paths_stay_on_their_host_and_are_never_probed(tmp_path: Path) -> None:
    saved = manifest(tmp_path)
    saved["network"] = {"remote_worker": "bob", "hub_url": "http://hub.example:8420"}
    saved["workspaces"]["bob"] = {"host": "worker.example", "path": "Q:/remote 'work'"}
    saved["launch"] = {"agents": {"bob": {"sessions": "Q:/run/bob-sessions.jsonl"}}}
    context = DENISE.build_context(saved, ROOT, DENISE.resolve_settings(AGENTS))
    bob = context["agents"]["bob"]
    assert bob["workspace"] == {
        "host": "worker.example",
        "path": "Q:/remote 'work'",
        "access": "unavailable",
    }
    assert bob["sessions"]["access"] == "unavailable"
    assert "worker host" in bob["resume_advice"]
    assert "Q:" not in json.dumps(context["access_inventory"])
    del saved["workspaces"]["bob"]
    unknown = DENISE.build_context(saved, ROOT, DENISE.resolve_settings(AGENTS))["agents"]["bob"]
    assert unknown["workspace"]["path"] is None
    assert unknown["workspace"]["host"] == "unknown remote host"


def test_saved_new_manifest_is_readable_with_no_live_paths(tmp_path: Path) -> None:
    saved = manifest(tmp_path)
    saved["denise"] = {"obsolete": "missing metadata is optional"}
    saved["launch"] = {"agents": {"bob": {"script": str(tmp_path / "stale.sh")}}}
    saved["configs"] = {"bob": str(tmp_path / "gone.json")}
    context = DENISE.build_context(saved, ROOT, DENISE.resolve_settings(AGENTS))
    assert context["agents"]["bob"]["scripts"]["script"]["access"] == "missing"
    assert context["agents"]["bob"]["config"]["access"] == "missing"
    assert context["agents"]["bob"]["sessions"]["access"] == "unknown"
    assert context["captured_at"] and "local preparation" in context["provenance"]


def test_path_references_do_not_assume_remote_windows_paths_are_local(tmp_path: Path) -> None:
    # This also runs natively on Windows; the index preserves the host's spelling.
    ref = DENISE.reference("Z:/other-host/run", "other-host", local=False)
    assert ref["path"] == "Z:/other-host/run" and ref["access"] == "unavailable"
    assert os.path.isabs(DENISE.reference(tmp_path, "local")["path"])


def test_joined_context_indexes_agents_from_session_logs_and_persisted_logs(
    tmp_path: Path,
) -> None:
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    git(checkout, "init", "-q")
    state = tmp_path / "state"
    run_id = "20261121_ec4eaf28"
    run_dir = state / "runs" / run_id
    run_dir.mkdir(parents=True)
    sessions = state / "sessions"
    sessions.mkdir(parents=True)
    logs_dir = tmp_path / "lane" / "logs"
    logs_dir.mkdir(parents=True)
    worktrees = {name: tmp_path / "wt" / name for name in ("alice", "bob", "charlie")}
    for wt in worktrees.values():
        wt.mkdir(parents=True)

    # Active-run manifest has no agents yet; session logs provide all three including Alice.
    saved = {
        "run_id": run_id,
        "workflow_id": "wf-active",
        "status": "active",
        "hub": {
            "hub_id": "hub-1",
            "name": "p1023-i592",
            "checkout": str(checkout),
            "origin": "https://github.com/example/repo.git",
            "forge": "github",
            "url": "http://127.0.0.1:8420",
        },
        "agents": [],
    }
    session_rows = {
        "alice": {
            "event": "start",
            "agent": "alice",
            "role": "orchestrator",
            "harness": "claude-code",
            "model": "claude-opus-5-5",
            "effort": "high",
            "workdir": str(worktrees["alice"]),
            "launcher_pid": 1001,
            "run_id": run_id,
            "workflow_id": "wf-active",
        },
        "bob": {
            "event": "start",
            "agent": "bob",
            "role": "implementer",
            "harness": "codex",
            "model": "gpt-5",
            "effort": "medium",
            "workdir": str(worktrees["bob"]),
            "launcher_pid": 1002,
            "run_id": run_id,
            "workflow_id": "wf-active",
        },
        "charlie": {
            "event": "resume",
            "agent": "charlie",
            "role": "reviewer",
            "harness": "claude-code",
            "model": "claude-opus-5-5",
            "effort": "medium",
            "workdir": str(worktrees["charlie"]),
            "launcher_pid": 1003,
            "run_id": run_id,
            "workflow_id": "wf-active",
        },
    }
    for name, row in session_rows.items():
        (sessions / f"{name}.jsonl").write_text(
            json.dumps({"event": "start", "agent": name, "run_id": "earlier-run", "harness": "old"})
            + "\n"
            + json.dumps(row)
            + "\n",
            encoding="utf-8",
        )
    # Only Alice and Bob have bridge telemetry on disk; Charlie's should appear as missing.
    (sessions / "alice-bridge.jsonl").write_text('{"event":"joined"}\n', encoding="utf-8")
    (sessions / "bob-bridge.jsonl").write_text('{"event":"joined"}\n', encoding="utf-8")
    # Unrelated earlier-run agent (scoped or unscoped) should not pollute this run.
    (sessions / "dana.jsonl").write_text(
        json.dumps({"event": "start", "agent": "dana", "run_id": "earlier-run", "harness": "codex"})
        + "\n",
        encoding="utf-8",
    )
    (sessions / "dave.jsonl").write_text(
        json.dumps({"event": "start", "agent": "dave", "role": "worker", "harness": "codex"})
        + "\n",
        encoding="utf-8",
    )
    # Persisted log directory with hub and agent launcher logs.
    for filename in ("hub.log", "alice.log", "bob.log", "charlie.log"):
        (logs_dir / filename).write_text("log line\n", encoding="utf-8")
    DENISE.write_joined_settings(
        run_dir,
        {
            "settings_from": "charlie",
            "harness": "claude-code",
            "model": "claude-opus-5-5",
            "effort": "medium",
            "status": "ready",
            "skip_reason": "",
        },
        clean_login=False,
        logs_dir=logs_dir,
    )

    context = DENISE.build_joined_context(
        saved,
        run_dir=run_dir,
        state_dir=state,
        sessions_dir=sessions,
        hub_checkout=checkout,
        target=checkout,
        target_common=DENISE.git_common_dir(checkout),
        tooling_root=ROOT,
        settings={"harness": "claude-code", "model": "claude-opus-5-5", "effort": "medium"},
        remote_workers={},
    )

    by_name = {agent["name"]: agent for agent in context["agents"]}
    assert set(by_name) == {"alice", "bob", "charlie"}
    assert by_name["alice"]["role"] == "orchestrator"
    assert by_name["alice"]["harness"] == "claude-code"
    assert by_name["alice"]["model"] == "claude-opus-5-5"
    assert by_name["alice"]["effort"] == "high"
    assert by_name["alice"]["launcher_pid"] == 1001
    assert by_name["alice"]["workspace"]["path"] == str(worktrees["alice"].resolve())
    assert by_name["alice"]["workspace"]["access"] == "present"
    assert by_name["bob"]["role"] == "implementer" and by_name["bob"]["harness"] == "codex"
    assert by_name["charlie"]["role"] == "reviewer"
    assert by_name["charlie"]["harness"] == "claude-code"
    assert by_name["alice"]["launcher_log"] == {
        "host": context["host"],
        "path": str((logs_dir / "alice.log").resolve()),
        "access": "present",
    }
    assert by_name["alice"]["sessions"]["access"] == "present"
    assert by_name["alice"]["bridge_telemetry"]["access"] == "present"
    assert by_name["charlie"]["bridge_telemetry"]["access"] == "missing"
    for name in ("alice", "bob", "charlie"):
        log_paths = {item["path"]: item["access"] for item in by_name[name]["logs"]}
        assert log_paths[str((logs_dir / f"{name}.log").resolve())] == "present"
        assert log_paths[str((sessions / f"{name}.jsonl").resolve())] == "present"
        assert str((sessions / f"{name}-bridge.jsonl").resolve()) in log_paths

    hub_log_paths = {item["path"]: item["access"] for item in context["hub"]["logs"]}
    assert hub_log_paths[str((logs_dir / "hub.log").resolve())] == "present"
    assert str((state / "hub.log").resolve()) not in hub_log_paths
    assert context["run"]["logs_dir"]["path"] == str(logs_dir.resolve())

    # If <state>/hub.log genuinely exists, it is included.
    (state / "hub.log").write_text("legacy hub log\n", encoding="utf-8")
    context_with_legacy = DENISE.build_joined_context(
        saved,
        run_dir=run_dir,
        state_dir=state,
        sessions_dir=sessions,
        hub_checkout=checkout,
        target=checkout,
        target_common=DENISE.git_common_dir(checkout),
        tooling_root=ROOT,
        settings={"harness": "claude-code", "model": "", "effort": ""},
        remote_workers={},
    )
    legacy_paths = {item["path"]: item["access"] for item in context_with_legacy["hub"]["logs"]}
    assert legacy_paths[str((state / "hub.log").resolve())] == "present"


def test_resolve_doctor_settings_precedence_and_skip_remedy() -> None:
    persisted = {
        "settings_from": "charlie",
        "harness": "claude-code",
        "model": "claude-opus-5-5",
        "effort": "medium",
        "clean_login": True,
        "status": "ready",
        "skip_reason": "",
    }
    inherited, used = DENISE.resolve_doctor_settings(persisted)
    assert (inherited["harness"], inherited["model"], inherited["effort"]) == (
        "claude-code",
        "claude-opus-5-5",
        "medium",
    )
    assert inherited["clean_login"] is True
    assert inherited["settings_from"] == "charlie"
    assert set(used) == {
        "harness=claude-code",
        "model=claude-opus-5-5",
        "effort=medium",
        "clean_login=true",
    }

    switched, used_switched = DENISE.resolve_doctor_settings(
        persisted, harness="codex", clean_login=False
    )
    assert (switched["harness"], switched["model"], switched["effort"]) == ("codex", "", "")
    assert switched["clean_login"] is False
    assert used_switched == []

    skipped = {
        "settings_from": "bob",
        "harness": "devin",
        "model": "",
        "effort": "",
        "clean_login": False,
        "status": "skipped",
        "skip_reason": "inherited harness devin is not yet supported for Denise",
    }
    with pytest.raises(ValueError, match="devin.*--harness"):
        DENISE.resolve_doctor_settings(skipped)
    overridden, _ = DENISE.resolve_doctor_settings(skipped, harness="codex")
    assert overridden["harness"] == "codex" and overridden["status"] == "ready"

    with pytest.raises(ValueError, match="--harness"):
        DENISE.resolve_doctor_settings(None)


def test_unscoped_session_row_kept_only_for_known_agents_and_marked_unscoped(
    tmp_path: Path,
) -> None:
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    git(checkout, "init", "-q")
    state = tmp_path / "state"
    run_id = "20261125_unscoped"
    run_dir = state / "runs" / run_id
    run_dir.mkdir(parents=True)
    sessions = state / "sessions"
    sessions.mkdir(parents=True)
    logs_dir = tmp_path / "logs"
    logs_dir.mkdir()
    (logs_dir / "bob.log").write_text("bob launcher\n", encoding="utf-8")
    (sessions / "bob.jsonl").write_text(
        json.dumps({"event": "start", "agent": "bob", "role": "implementer", "harness": "codex"})
        + "\n",
        encoding="utf-8",
    )
    (sessions / "dave.jsonl").write_text(
        json.dumps({"event": "start", "agent": "dave", "role": "worker", "harness": "codex"})
        + "\n",
        encoding="utf-8",
    )
    context = DENISE.build_joined_context(
        {"run_id": run_id, "status": "active", "hub": {"checkout": str(checkout)}, "agents": []},
        run_dir=run_dir,
        state_dir=state,
        sessions_dir=sessions,
        hub_checkout=checkout,
        target=checkout,
        target_common=DENISE.git_common_dir(checkout),
        tooling_root=ROOT,
        settings={"harness": "codex", "model": "", "effort": ""},
        remote_workers={},
        logs_dir=logs_dir,
    )
    by_name = {agent["name"]: agent for agent in context["agents"]}
    assert set(by_name) == {"bob"}
    assert by_name["bob"]["harness"] == "codex"
    assert by_name["bob"]["session_scope"] == "unscoped"


def test_write_joined_settings_cleans_up_temp_file_on_dump_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_dir = tmp_path / "run"

    def boom(*_args: object, **_kwargs: object) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(DENISE.json, "dump", boom)
    with pytest.raises(OSError, match="disk full"):
        DENISE.write_joined_settings(run_dir, {"harness": "codex"})
    assert list((run_dir / "denise").iterdir()) == []
