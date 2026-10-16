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
def test_effort_adapter_capability_is_separate_from_effort_value(harness: str) -> None:
    agents = {**AGENTS, "bob": {"harness": harness, "model": "", "effort": "new-value"}}
    inherited = DENISE.resolve_settings(agents)
    assert inherited["effort"] == "new-value"
    assert inherited["status"] == "skipped"
    assert "no interactive representation" in inherited["skip_reason"]
    with pytest.raises(ValueError, match="--denise-effort is unsupported"):
        DENISE.resolve_settings(agents, effort="another-new-value")


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
