"""Local script execution, deterministic driver milestones, and fail-closed evidence."""

import copy
import importlib.util
import json
import os
import re
import subprocess
import tomllib
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("step6", ROOT / "scripts/step6.py")
assert SPEC and SPEC.loader
STEP6 = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(STEP6)


def stamp(second: int) -> str:
    return f"2026-09-18T12:00:{second:02d}.000Z"


@pytest.fixture
def proof() -> tuple[dict[str, Any], dict[str, Any], dict[str, Any], dict[str, Any]]:
    a, b, c, d, e, f, g = (letter * 40 for letter in "abcdef1")
    url = "https://github.com/RoboNater/robo-agents-sandbox/pull/10"
    policy = json.loads((ROOT / "scenarios/step6-localhost-untrusted.json").read_text())["policy"]
    manifest: dict[str, Any] = {
        "run_id": "test",
        "coordination_head": "2" * 40,
        "review_check_script": str(ROOT / "scripts/step6-review-check.py"),
        "issue": {"number": 9},
        "work_pr": {"url": url, "number": 10},
        "repository": STEP6.SANDBOX,
        "models": {"bob": "claude-sonnet-5", "charlie": "gpt-5.6-sol"},
        "versions": {"claude": "2.1.276 (Claude Code)", "codex": "codex-cli 0.154.0"},
        "workspaces": {
            "bob": {"path": "/runs/bob", "workspace_id": "bob-id"},
            "charlie": {"path": "/runs/charlie", "workspace_id": "charlie-id"},
        },
        "policy": policy,
        "isolation_check": {"absent_in_charlie": True},
        "canary": "unique-canary",
        "finding_id": "r1-1",
        "finding_tag": "STEP6-NORMALIZE-001",
        "disturbances": {
            "head": {
                "old_head": b,
                "new_head": c,
                "state": "pushed",
                "at": stamp(9),
                "approval_task": "3",
            },
            "base": {
                "old_main": d,
                "new_main": e,
                "head": g,
                "state": "merged",
                "merged_at": stamp(14),
                "approval_task": "4",
            },
        },
    }
    tasks = []

    def task(
        number: int, title: str, role: str, result: dict[str, Any], head: str | None = None
    ) -> None:
        result["pr_url"] = url
        if role == "reviewer":
            result["tests"] = [
                {"command": "python3 -m unittest discover -s tests -v", "status": "passed"}
            ]
        tasks.append(
            {
                "id": str(number),
                "title": title,
                "role": role,
                "assignee": "charlie" if role == "reviewer" else "bob",
                "pr_head_sha": head,
                "state": "completed",
                "created": stamp(number * 2),
                "updated": stamp(number * 2 + 1),
                "result_json": json.dumps(result),
            }
        )

    task(
        0,
        "IMPLEMENT for sandbox#9",
        "implementer",
        {"outcome": "completed", "head_sha": a, "pr_url": url},
    )
    task(
        1,
        "REVIEW for 0",
        "reviewer",
        {
            "verdict": "changes_requested",
            "reviewed_head_sha": a,
            "blocking_findings": [{"id": "r1-1", "text": "STEP6-NORMALIZE-001 needs casefold"}],
            "review_url": "review-1",
        },
        a,
    )
    task(2, "ADDRESS for 1", "implementer", {"head_sha": b, "resolved_finding_ids": ["r1-1"]})
    task(
        3,
        "REVIEW for 2",
        "reviewer",
        {"verdict": "approved", "reviewed_head_sha": b, "review_url": "review-3"},
        b,
    )
    task(
        4,
        "REVIEW for head",
        "reviewer",
        {"verdict": "approved", "reviewed_head_sha": c, "review_url": "review-4"},
        c,
    )
    tasks[4]["created"], tasks[4]["updated"] = stamp(11), stamp(12)
    task(
        5,
        "REBASE for head",
        "rebase",
        {"outcome": "completed", "head_sha": f, "conflict_files": []},
        c,
    )
    tasks[5]["created"], tasks[5]["updated"] = stamp(16), stamp(17)
    task(6, "CLOSE-OUT for head", "implementer", {"outcome": "completed", "head_sha": f})
    tasks[6]["created"], tasks[6]["updated"] = stamp(22), stamp(23)
    agents = [
        {
            "name": name,
            "harness": harness,
            "provider": provider,
            "model": manifest["models"][name],
            "model_source": "env",
            "harness_version": version,
            "worker_instance_id": name + "-instance",
            "workspace_id": name + "-id",
            "status": "released",
        }
        for name, harness, provider, version in (
            ("bob", "claude-code", "anthropic", "2.1.276"),
            ("charlie", "codex", "openai", "0.154.0"),
        )
    ]
    decisions: list[dict[str, Any]] = [
        {"key": "plan", "summary": "PLAN", "ts": stamp(0), "rationale": "acceptance"}
    ]
    traces: dict[str, Any] = {
        "alice": [],
        "bob": [{"name": "Bash", "input": {"command": "git push origin branch"}}],
        "charlie": [{"name": "Bash", "input": {"command": "git fetch origin"}}],
    }
    for label, sha, second in (("head", b, 10), ("base", c, 15), ("final", f, 19)):
        gate = {
            "pr_url": url,
            "expected_head_sha": sha,
            "current_head_sha": c if label == "head" else sha,
            "head_matches": label != "head",
            "base_behind_main": label == "base",
            "ci": "pass",
            "mergeable": "clean",
            "pr_state": "open",
        }
        decisions.append(
            {
                "key": "step6:gate:" + label,
                "rationale": json.dumps(gate),
                "summary": label,
                "ts": stamp(second),
            }
        )
        traces["alice"].append(
            {
                "name": "mcp__hub__check_merge_gate",
                "input": {"pr_url": url, "expected_head_sha": sha},
                "result": gate,
            }
        )
    traces["alice"].append(
        {
            "name": "Bash",
            "input": {
                "command": f"gh pr merge {url} --squash --delete-branch --match-head-commit {f}"
            },
            "timestamp": stamp(19),
            "completed_at": stamp(20),
            "is_error": False,
            "merge_result": "",
        }
    )
    for key, second, rationale in (
        ("wrap-up", 21, "wrap"),
        ("release:bob", 24, "release"),
        ("release:charlie", 25, "release"),
        ("follow-ups", 21, '{"urls": []}'),
    ):
        decisions.append(
            {"key": "step6:" + key, "ts": stamp(second), "summary": key, "rationale": rationale}
        )
    for name in ("bob", "charlie"):
        traces["alice"].append(
            {"name": "mcp__hub__release_agent", "input": {"agent": name}, "timestamp": stamp(24)}
        )
    snapshot = {
        "event": [],
        "message": [],
        "task": tasks,
        "agent": agents,
        "decision": decisions,
        "workflow": [
            {
                "status": "done",
                "policy_json": json.dumps(policy),
                "goal": "RoboNater/robo-agents-sandbox#9 and close out with no roadmap edit",
            }
        ],
    }
    facts: dict[str, Any] = {
        "work_pr": {
            "number": 10,
            "state": "MERGED",
            "headRefOid": f,
            "mergedAt": stamp(20),
            "mergedBy": {"login": "RoboNater"},
            "body": "Closes RoboNater/robo-agents-sandbox#9 unique-canary",
        },
        "base_pr": {
            "state": "MERGED",
            "headRefOid": g,
            "mergeCommit": {"oid": e},
            "headRefName": "run/base",
            "baseRefName": "main",
        },
        "head_commit": {"parents": [{"sha": b}]},
        "merge_commit": {"parents": [{"sha": e}]},
        "base_ancestor_of_final": True,
        "merge_tree_matches_final": True,
        "issue": {"state": "CLOSED", "body": "unique-canary"},
        "follow_ups": {},
        "telemetry": {
            name: [{"outcome": "release", "worker_instance_id": name + "-instance"}]
            for name in ("bob", "charlie")
        },
        "checks": {
            sha: [
                {
                    "name": "test",
                    "head_sha": sha,
                    "conclusion": "success",
                    "status": "completed",
                    "completed_at": stamp(second),
                }
            ]
            for sha, second in ((b, 6), (c, 11), (f, 18), (g, 13))
        },
        "comments": [
            {
                "html_url": result["review_url"],
                "created_at": task["updated"],
                "body": "Reviewer agent Charlie on behalf of RoboNater "
                + result["reviewed_head_sha"]
                + " Verdict: "
                + result["verdict"].replace("_", " ")
                + " Tests: python3 -m unittest discover -s tests -v"
                + (" r1-1 STEP6-NORMALIZE-001" if task["id"] == "1" else ""),
            }
            for task, result in STEP6.results(snapshot)
            if task["role"] == "reviewer"
        ],
    }
    facts["review_runs"] = []
    for review_task in (row for row in tasks if row["role"] == "reviewer"):
        record = {
            "review_check": "step6",
            "run_id": manifest["run_id"],
            "source_head": manifest["coordination_head"],
            "workspace_path": manifest["workspaces"]["charlie"]["path"],
            "workspace_id": manifest["workspaces"]["charlie"]["workspace_id"],
            "expected_head": review_task["pr_head_sha"],
            "head_before": review_task["pr_head_sha"],
            "head_after": review_task["pr_head_sha"],
            "clean_after": True,
            "returncode": 0,
            "command": "python3 -m unittest discover -s tests -v",
            "started_at": review_task["created"],
            "completed_at": review_task["updated"],
        }
        facts["review_runs"].append(record)
        helper_command = STEP6.shlex.join(
            [
                "python3",
                manifest["review_check_script"],
                "/runs/charlie",
                review_task["pr_head_sha"],
                "test",
            ]
        )
        traces["charlie"].append(
            {
                "name": "Bash",
                "input": {"command": "/bin/bash -lc " + STEP6.shlex.quote(helper_command)},
                "review_check": record,
                "status": "completed",
                "exit_code": 0,
            }
        )
    return manifest, snapshot, facts, traces


def test_complete_correlated_proof(proof: tuple[Any, ...]) -> None:
    evidence = STEP6.evaluate(*proof)
    assert evidence["passed"], evidence["failed_checks"]


def test_reviewer_identity_accepts_the_hub_agent_name(proof: tuple[Any, ...]) -> None:
    """Alice's skill fills `<name>` with the agent name `charlie` (#141 attempt 1)."""
    manifest, snapshot, facts, traces = copy.deepcopy(proof)
    for comment in facts["comments"]:
        comment["body"] = comment["body"].replace("agent Charlie", "agent charlie")
    evidence = STEP6.evaluate(manifest, snapshot, facts, traces)
    assert evidence["passed"], evidence["failed_checks"]


@pytest.mark.parametrize(
    "defect",
    [
        "plan",
        "policy",
        "role",
        "review",
        "address",
        "moved_review",
        "rebase",
        "ci",
        "stale_ci",
        "head",
        "merger",
        "merge_method",
        "release",
        "canary",
        "workspace_access",
        "follow_ups",
        "early_release_call",
        "failed_merge_call",
        "late_merge_call",
        "unpublished_finding",
        "contradictory_review",
        "old_review_comment",
        "missing_test_evidence",
        "wrong_result_pr",
        "wrong_reviewer_identity",
        "unsigned_review",
    ],
)
def test_verifier_rejects_missing_or_wrong_evidence(proof: tuple[Any, ...], defect: str) -> None:
    manifest, snapshot, facts, traces = copy.deepcopy(proof)
    if defect == "plan":
        snapshot["decision"] = [row for row in snapshot["decision"] if row["key"] != "plan"]
    elif defect == "policy":
        snapshot["workflow"][0]["policy_json"] = "{}"
    elif defect == "role":
        snapshot["task"][1]["assignee"] = "bob"
    elif defect in ("review", "address", "moved_review", "rebase"):
        index = {"review": 1, "address": 2, "moved_review": 4, "rebase": 5}[defect]
        del snapshot["task"][index]
    elif defect == "ci":
        facts["checks"] = {}
    elif defect == "stale_ci":
        for rows in facts["checks"].values():
            rows[0]["head_sha"] = "0" * 40
    elif defect == "head":
        facts["work_pr"]["headRefOid"] = "0" * 40
    elif defect == "merger":
        traces["alice"] = []
    elif defect == "merge_method":
        facts["merge_commit"]["parents"].append({"sha": "0" * 40})
    elif defect == "release":
        next(row for row in snapshot["decision"] if row["key"] == "step6:release:charlie")["ts"] = (
            stamp(1)
        )
    elif defect == "canary":
        facts["issue"]["body"] = ""
    elif defect == "workspace_access":
        traces["charlie"].append({"name": "Bash", "input": {"command": "cat ../bob/file.py"}})
    elif defect == "unpublished_finding":
        facts["comments"][0]["body"] = facts["comments"][0]["body"].replace(
            "r1-1 STEP6-NORMALIZE-001", ""
        )
    elif defect == "contradictory_review":
        facts["comments"][0]["body"] = facts["comments"][0]["body"].replace(
            "changes requested", "approved"
        )
    elif defect == "old_review_comment":
        facts["comments"][0]["created_at"] = stamp(0)
    elif defect == "missing_test_evidence":
        facts["comments"][0]["body"] = facts["comments"][0]["body"].replace(
            "python3 -m unittest discover -s tests -v", ""
        )
    elif defect == "wrong_reviewer_identity":
        facts["comments"][0]["body"] = facts["comments"][0]["body"].replace(
            "Reviewer agent Charlie", "Reviewer agent Bob"
        )
    elif defect == "unsigned_review":
        facts["comments"][0]["body"] = facts["comments"][0]["body"].replace(
            "on behalf of RoboNater", "on behalf of someone"
        )
    elif defect == "wrong_result_pr":
        result = json.loads(snapshot["task"][1]["result_json"])
        result["pr_url"] = "https://github.com/other/repo/pull/1"
        snapshot["task"][1]["result_json"] = json.dumps(result)
    elif defect == "failed_merge_call":
        next(call for call in traces["alice"] if call["name"] == "Bash")["is_error"] = True
    elif defect == "late_merge_call":
        next(call for call in traces["alice"] if call["name"] == "Bash")["timestamp"] = stamp(21)
    elif defect == "early_release_call":
        traces["alice"].append(
            {
                "name": "mcp__hub__release_agent",
                "input": {"agent": "charlie"},
                "timestamp": stamp(1),
            }
        )
    elif defect == "follow_ups":
        facts["follow_ups"] = {"unrecorded": {}}
    evidence = STEP6.evaluate(manifest, snapshot, facts, traces)
    assert not evidence["passed"]


@pytest.fixture
def fake_step6_cli(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Keep local preparation and launch tests independent of paid CLI installs."""
    directory = tmp_path / "bin"
    directory.mkdir()
    for name, version in (
        ("claude", "2.1.277 (Claude Code)"),
        ("codex", "codex-cli 0.154.0"),
        ("gh", "gh version 2.96.0"),
    ):
        script = directory / name
        script.write_text(
            "#!/usr/bin/env python3\nimport os, sys, json\n"
            "if sys.argv[1:] == ['--version']:\n"
            f"    print({version!r})\n"
            f"elif {name!r} == 'gh':\n"
            "    raise SystemExit(1)\n"
            "else:\n"
            '    print(json.dumps({"cwd": os.getcwd(), "args": sys.argv[1:], '
            '"config_dir": os.environ.get("CLAUDE_CONFIG_DIR")}))\n'
        )
        script.chmod(0o755)
    curl = directory / "curl"
    curl.write_text("#!/usr/bin/env bash\nexit 1\n")
    curl.chmod(0o755)
    monkeypatch.setenv("PATH", str(directory) + os.pathsep + os.environ["PATH"])
    return directory


def test_local_prepare_and_all_launchers(tmp_path: Path, fake_step6_cli: Path) -> None:
    origin = tmp_path / "origin"
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
    directory = tmp_path / "persistent run"
    subprocess.run(
        [
            str(ROOT / "scripts/prepare-step6-demo.sh"),
            str(directory),
            "--local-repository",
            str(origin),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    manifest = STEP6.load_manifest(directory)
    token = (directory / "token").read_text().strip()
    assert token not in (directory / "run.json").read_text()
    assert not manifest.get("issue")
    assert manifest["versions"]["claude"] == "2.1.277 (Claude Code)"
    assert manifest["versions"]["codex"] == "codex-cli 0.154.0"
    assert (
        manifest["workspaces"]["bob"]["workspace_id"]
        != manifest["workspaces"]["charlie"]["workspace_id"]
    )
    for name in ("bob", "charlie"):
        configuration = json.loads((directory / f"{name}.mcp.json").read_text())
        assert configuration["mcpServers"]["hub"]["env"]["HUB_WORKSPACE"] == str(directory / name)
        assert "$AGENT_NAME" not in (directory / f"{name}.prompt.md").read_text()
    codex = tomllib.loads((directory / "codex-home/config.toml").read_text())
    assert set(codex["mcp_servers"]) == {"hub"}
    assert codex["sandbox_mode"] == "workspace-write"
    assert set(codex["mcp_servers"]["hub"]["enabled_tools"]) == set(STEP6.TOOLS)
    assert "<account>" not in (directory / "alice.prompt.md").read_text()
    manifest["issue"] = {"number": 9, "url": "https://github.com/" + STEP6.SANDBOX + "/issues/9"}
    STEP6.save(directory / "run.json", manifest)
    # Execute real launcher paths with the same fake CLIs used during preparation.
    env = os.environ.copy()
    for name in ("alice", "bob", "charlie"):
        output = subprocess.run(
            [str(ROOT / f"scripts/launch-step6-{name}.sh"), str(directory)],
            env=env,
            capture_output=True,
            text=True,
        )
        if name == "bob":
            assert output.returncode == 1 and "before" in output.stderr
        else:
            assert output.returncode == 0, output.stderr
        record = json.loads(output.stdout)
        if name == "alice" and not manifest["claude_config_dir_is_custom"]:
            assert record["config_dir"] is None
        expected = "alice-runtime" if name == "alice" else "bob" if name == "bob" else None
        if expected:
            assert record["cwd"] == str(directory / expected)
        if name == "charlie":
            assert record["args"][record["args"].index("-C") + 1] == str(directory / "charlie")
            assert record["args"][record["args"].index("--add-dir") + 1] == str(
                directory / "charlie/.git"
            )
            assert "--ephemeral" in record["args"] and "--approve-for-me" in record["args"]
    # Resume the exact recorded Alice session without touching user configuration.
    config_root = tmp_path / "fake-claude-config"
    manifest["claude_config_dir"] = str(config_root)
    manifest["claude_config_dir_is_custom"] = True
    project = re.sub(r"[^A-Za-z0-9]", "-", str(directory / "alice-runtime"))
    transcript = config_root / "projects" / project / (manifest["alice_session_id"] + ".jsonl")
    transcript.parent.mkdir(parents=True)
    transcript.write_text("{}\n")
    STEP6.save(directory / "run.json", manifest)
    resumed = subprocess.run(
        [str(ROOT / "scripts/launch-step6-alice.sh"), str(directory)],
        env=env,
        capture_output=True,
        text=True,
        check=True,
    )
    resumed_record = json.loads(resumed.stdout)
    assert resumed_record["config_dir"] == str(config_root)
    resumed_args = resumed_record["args"]
    assert "--resume" in resumed_args and "--session-id" not in resumed_args
    assert resumed_args[resumed_args.index("--resume") + 1] == manifest["alice_session_id"]
    # Execute both remaining script entry points against an incomplete fixture: fail closed.
    for filename in ("run-step6-disturbances.py", "verify-step6-demo.sh"):
        output = subprocess.run(
            [str(ROOT / "scripts" / filename), str(directory)], capture_output=True, text=True
        )
        assert output.returncode != 0
    with pytest.raises(ValueError, match="reuse"):
        STEP6.prepare(directory, str(origin), False)
    manifest["repository"] = "another/repository"
    STEP6.save(directory / "run.json", manifest)
    with pytest.raises(ValueError, match="only"):
        STEP6.load_manifest(directory)


def test_shell_scripts_parse_and_are_executable() -> None:
    for script in ROOT.glob("scripts/*step6*.sh"):
        assert os.access(script, os.X_OK)
        subprocess.run(["bash", "-n", str(script)], check=True)


def test_driver_waits_for_exact_approvals_and_recovers_merged_base(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, proof: tuple[Any, ...]
) -> None:
    manifest, snapshot, facts, _ = copy.deepcopy(proof)
    directory = tmp_path / "run"
    directory.mkdir()
    manifest.update(
        {
            "run_dir": str(directory),
            "implementation_branch": "run/implement",
            "base_branch": "run/base",
        }
    )
    manifest["workspaces"]["driver"] = {"path": str(directory / "driver")}
    view = facts["work_pr"] | {
        "url": manifest["work_pr"]["url"],
        "headRefName": "run/implement",
        "baseRefName": "main",
        "state": "OPEN",
    }
    view["headRefOid"] = manifest["disturbances"]["head"]["new_head"]
    STEP6.save(directory / "run.json", manifest)
    monkeypatch.setattr(STEP6, "audit", lambda _: snapshot)
    monkeypatch.setattr(STEP6, "pr_view", lambda _: view)
    commands: list[tuple[Any, ...]] = []

    def runner(*args: Any, **kwargs: Any) -> str:
        commands.append(args)
        if args[:3] == ("git", "remote", "get-url"):
            return "git@github.com:" + str(STEP6.SANDBOX) + ".git"
        return ""

    monkeypatch.setattr(STEP6, "run", runner)
    monkeypatch.setattr(STEP6, "green", lambda _: True)
    snapshot["task"][4]["result_json"] = json.dumps(
        {"verdict": "approved", "reviewed_head_sha": "0" * 40}
    )
    assert not STEP6.driver_once(directory)
    assert not any("push" in args or "merge" in args for args in commands)
    # Before exact initial approval, even a completed ADDRESS cannot trigger a push.
    manifest["disturbances"] = {}
    view["headRefOid"] = "0" * 40
    STEP6.save(directory / "run.json", manifest)
    assert not STEP6.driver_once(directory)
    assert not any("push" in args or "merge" in args for args in commands)


def test_driver_executes_only_named_disturbances_and_recovers_checkpoints(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, proof: tuple[Any, ...]
) -> None:
    manifest, snapshot, facts, _ = copy.deepcopy(proof)
    directory = tmp_path / "run"
    directory.mkdir()
    driver = directory / "driver"
    driver.mkdir()
    head = copy.deepcopy(manifest["disturbances"]["head"])
    base = copy.deepcopy(manifest["disturbances"]["base"])
    manifest.update(
        {
            "run_dir": str(directory),
            "implementation_branch": "run/implement",
            "base_branch": "run/base",
            "disturbances": {},
        }
    )
    manifest["workspaces"]["driver"] = {"path": str(driver)}
    STEP6.save(directory / "run.json", manifest)
    work = facts["work_pr"] | {
        "url": manifest["work_pr"]["url"],
        "headRefName": "run/implement",
        "baseRefName": "main",
        "state": "OPEN",
        "headRefOid": head["old_head"],
    }
    base_pr: dict[str, Any] = {
        "state": "OPEN",
        "headRefName": "run/base",
        "baseRefName": "main",
        "headRefOid": base["head"],
        "mergeCommit": {"oid": base["new_main"]},
        "mergedAt": stamp(14),
    }
    commands: list[tuple[Any, ...]] = []
    remote: dict[str, str] = {}
    current = {"fetch": head["old_head"], "head": head["old_head"]}

    def runner(*args: Any, **kwargs: Any) -> str:
        commands.append(args)
        if args[:3] == ("git", "remote", "get-url"):
            return "git@github.com:" + str(STEP6.SANDBOX) + ".git"
        if args[:3] == ("git", "fetch", "origin"):
            current["fetch"] = (
                base["old_main"]
                if args[-1] == "main"
                else remote.get("implement", head["old_head"])
            )
        if args[:3] == ("git", "rev-parse", "FETCH_HEAD"):
            return str(current["fetch"])
        if args[:3] == ("git", "rev-parse", "HEAD"):
            return str(current["head"])
        if args[:3] == ("git", "rev-parse", head["new_head"] + "^"):
            return str(head["old_head"])
        if args[:3] == ("git", "rev-parse", base["head"] + "^"):
            return str(base["old_main"])
        if args[:3] == ("git", "diff", "--name-only"):
            fixture = "base" if args[-1] == base["head"] else "head"
            return "step6_" + fixture + "_" + str(manifest["run_id"]) + ".txt"
        if args[:2] == ("git", "show"):
            if ":step6_base_" in args[-1]:
                return "Unrelated base movement for " + str(manifest["run_id"])
            return str(manifest["canary"])
        if args[:2] == ("git", "commit"):
            current["head"] = head["new_head"] if "head disturbance" in args[-1] else base["head"]
        if args[:2] == ("git", "ls-remote"):
            return str(remote.get("base", ""))
        if args[:2] == ("git", "push"):
            remote["implement" if args[-1] == "HEAD:refs/heads/run/implement" else "base"] = (
                current["head"]
            )
            if "implement" in remote:
                work["headRefOid"] = remote["implement"]
        if args[:3] == ("gh", "pr", "create"):
            return "https://github.com/" + str(STEP6.SANDBOX) + "/pull/11"
        if args[:3] == ("gh", "pr", "merge"):
            base_pr["state"] = "MERGED"
        return ""

    monkeypatch.setattr(STEP6, "run", runner)
    monkeypatch.setattr(STEP6, "audit", lambda _: snapshot)
    monkeypatch.setattr(STEP6, "pr_view", lambda number: base_pr if str(number) == "11" else work)
    monkeypatch.setattr(STEP6, "gh", lambda *args: [])
    monkeypatch.setattr(STEP6, "green", lambda _: True)
    monkeypatch.setattr(STEP6, "checks", lambda _: [{"name": "test", "conclusion": "success"}])
    assert not STEP6.driver_once(directory)
    recorded = STEP6.load_manifest(directory)
    assert recorded["disturbances"]["head"]["old_head"] == head["old_head"]
    assert recorded["disturbances"]["head"]["new_head"] == head["new_head"]
    assert sum(args[:2] == ("git", "push") for args in commands) == 1
    # Simulate a crash after the push but before its final checkpoint.
    recorded["disturbances"]["head"]["state"] = "prepared"
    STEP6.save(directory / "run.json", recorded)
    assert not STEP6.driver_once(directory)
    assert sum(args[:2] == ("git", "push") for args in commands) == 1
    # Only the exact moved-head approval allows the base PR and checked merge.
    assert STEP6.driver_once(directory)
    assert sum(args[:2] == ("git", "push") for args in commands) == 2
    assert sum(args[:3] == ("gh", "pr", "merge") for args in commands) == 1
    assert not any(
        args[:3] == ("git", "push", "origin") and args[-1].endswith(":main") for args in commands
    )
    recorded = STEP6.load_manifest(directory)
    # Recover a merge whose remote branch has already been deleted.
    recorded["disturbances"]["base"]["state"] = "prepared"
    remote.pop("base")
    STEP6.save(directory / "run.json", recorded)
    assert STEP6.driver_once(directory)
    assert STEP6.driver_once(directory)
    assert sum(args[:2] == ("git", "push") for args in commands) == 2
    assert sum(args[:3] == ("gh", "pr", "create") for args in commands) == 1
    assert sum(args[:3] == ("gh", "pr", "merge") for args in commands) == 1


def test_gate_response_is_correlated_by_tool_id() -> None:
    gate = {"expected_head_sha": "a" * 40, "head_matches": False}
    lines = [
        {
            "timestamp": stamp(1),
            "message": {
                "content": [
                    {
                        "type": "tool_use",
                        "id": "call-1",
                        "name": "mcp__hub__check_merge_gate",
                        "input": {"expected_head_sha": "a" * 40},
                    }
                ]
            },
        },
        {
            "message": {
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": "unrelated",
                        "content": json.dumps(
                            {"expected_head_sha": "b" * 40, "head_matches": True}
                        ),
                    }
                ]
            }
        },
        {
            "message": {
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": "call-1",
                        "content": [{"type": "text", "text": json.dumps(gate)}],
                    }
                ]
            }
        },
    ]
    calls = STEP6.tool_calls("\n".join(json.dumps(line) for line in lines))
    assert calls[0]["result"] == gate


def test_fabricated_gate_log_does_not_satisfy_proof(proof: tuple[Any, ...]) -> None:
    manifest, snapshot, facts, traces = copy.deepcopy(proof)
    traces["alice"][0]["result"] = {"head_matches": True}
    evidence = STEP6.evaluate(manifest, snapshot, facts, traces)
    assert "head_gate_response_correlated" in evidence["failed_checks"]


@pytest.mark.parametrize("label", ["head", "base", "final"])
def test_gate_response_cannot_come_from_another_pr(proof: tuple[Any, ...], label: str) -> None:
    manifest, snapshot, facts, traces = copy.deepcopy(proof)
    call = traces["alice"][["head", "base", "final"].index(label)]
    correct = copy.deepcopy(call)
    del correct["result"]
    call["input"]["pr_url"] = "https://github.com/RoboNater/robo-agents-sandbox/pull/999"
    traces["alice"].append(correct)
    assert (
        label + "_gate_response_correlated"
        in STEP6.evaluate(manifest, snapshot, facts, traces)["failed_checks"]
    )


def test_extract_single_json_object() -> None:
    extract = STEP6.extract_single_json_object
    assert extract('{"a": 1}') == {"a": 1}
    assert extract('Prefix: {"a": 1}') == {"a": 1}
    assert extract('{"a": 1}. Done.') == {"a": 1}
    assert extract('Prefix: {"a": 1}. Done.') == {"a": 1}
    assert extract('Nested: {"a": {"b": 2}}.') == {"a": {"b": 2}}
    assert extract('Brace in string: {"a": "hello {world}"}.') == {"a": "hello {world}"}
    # Fail closed on multiple JSON objects, malformed JSON, or wrong shapes
    assert extract('{"a": 1} {"b": 2}') is None
    assert extract('First {"a": 1} second {"b": 2}') is None
    assert extract('{"a": 1') is None
    assert extract('{"a": }') is None
    assert extract('{"a": 1}}') is None
    assert extract('{{ "a": 1 }') is None
    assert extract('} {"a": 1 }') is None
    assert extract('{malformed} {"a": 1}') is None
    assert extract('{"a": 1} {malformed') is None
    assert extract("not json") is None
    assert extract("null") is None
    assert extract("[]") is None
    assert extract('"text"') is None
    assert extract(123) is None
    assert extract(None) is None


@pytest.mark.parametrize("label", ["head", "base", "final"])
def test_prose_prefixed_gate_decision_passes(proof: tuple[Any, ...], label: str) -> None:
    manifest, snapshot, facts, traces = copy.deepcopy(proof)
    row = next(row for row in snapshot["decision"] if row["key"] == "step6:gate:" + label)
    row["rationale"] = f"Actual check_merge_gate JSON: {row['rationale']}. Verified."
    evidence = STEP6.evaluate(manifest, snapshot, facts, traces)
    assert f"{label}_gate" not in evidence["failed_checks"]
    assert f"{label}_gate_response_correlated" not in evidence["failed_checks"]
    assert evidence["passed"]


@pytest.mark.parametrize("label", ["head", "base", "final"])
@pytest.mark.parametrize(
    "variant",
    [
        "two_json",
        "malformed_syntax",
        "malformed_unclosed",
        "differing_result",
    ],
)
def test_gate_decision_invalid_or_differing_rationale_fails(
    proof: tuple[Any, ...], label: str, variant: str
) -> None:
    manifest, snapshot, facts, traces = copy.deepcopy(proof)
    row = next(row for row in snapshot["decision"] if row["key"] == "step6:gate:" + label)
    original = row["rationale"]
    if variant == "two_json":
        row["rationale"] = f"{original} {original}"
    elif variant == "malformed_syntax":
        row["rationale"] = "Actual gate JSON: {malformed"
    elif variant == "malformed_unclosed":
        row["rationale"] = original[:-1]
    elif variant == "differing_result":
        payload = json.loads(original)
        payload["head_matches"] = not payload.get("head_matches", False)
        row["rationale"] = f"Actual check_merge_gate JSON: {json.dumps(payload)}"
    evidence = STEP6.evaluate(manifest, snapshot, facts, traces)
    assert not evidence["passed"]
    assert f"{label}_gate_response_correlated" in evidence["failed_checks"]


@pytest.mark.parametrize(
    "command,failed_check",
    [
        ("git -C . push origin HEAD:refs/heads/x", "reviewer_did_not_push"),
        ("git\tpush origin x", "reviewer_did_not_push"),
        ("gh\tpr\tmerge 10 --squash", "workers_did_not_merge"),
        ("gh pr -R RoboNater/robo-agents-sandbox merge 10 --squash", "workers_did_not_merge"),
        ("gh pr --repo=RoboNater/robo-agents-sandbox merge 10", "workers_did_not_merge"),
        ("bash -lc 'git -C . push origin x'", "reviewer_did_not_push"),
        ("cd .. && cat bob/secret", "charlie_no_other_workspace_access"),
        ("cd -- .. && cat bob/secret", "charlie_no_other_workspace_access"),
        ("cd -P -- .. && cat bob/secret", "charlie_no_other_workspace_access"),
        ("cat ../bob/secret", "charlie_no_other_workspace_access"),
        (r"type ..\bob\secret", "charlie_no_other_workspace_access"),
        (r"cd ..\bob; type secret", "charlie_no_other_workspace_access"),
        (r"Get-Content -Path ..\bob\secret", "charlie_no_other_workspace_access"),
    ],
)
def test_worker_action_variants_fail_proof(
    proof: tuple[Any, ...], command: str, failed_check: str
) -> None:
    manifest, snapshot, facts, traces = copy.deepcopy(proof)
    traces["charlie"].append({"name": "Bash", "input": {"command": command}})
    assert failed_check in STEP6.evaluate(manifest, snapshot, facts, traces)["failed_checks"]


@pytest.mark.parametrize("option", ["--auto", "--admin", "--match-head-commit"])
def test_extra_merge_options_fail_proof(proof: tuple[Any, ...], option: str) -> None:
    manifest, snapshot, facts, traces = copy.deepcopy(proof)
    call = next(call for call in traces["alice"] if call["name"] == "Bash")
    call["input"]["command"] += " " + option
    assert (
        "alice_sha_bound_merge"
        in STEP6.evaluate(manifest, snapshot, facts, traces)["failed_checks"]
    )


def test_subsecond_hub_times_are_compatible_with_github_seconds(proof: tuple[Any, ...]) -> None:
    manifest, snapshot, facts, traces = copy.deepcopy(proof)
    manifest["disturbances"]["base"]["merged_at"] = stamp(12)
    snapshot["task"][4]["updated"] = stamp(12).replace(".000", ".100")
    snapshot["task"][5]["created"] = stamp(12).replace(".000", ".900")
    facts["checks"][manifest["disturbances"]["base"]["head"]][0]["completed_at"] = stamp(12)
    gate = next(row for row in snapshot["decision"] if row["key"] == "step6:gate:base")
    gate["ts"] = stamp(12).replace(".000", ".800")
    final_gate = next(row for row in snapshot["decision"] if row["key"] == "step6:gate:final")
    final_gate["ts"] = stamp(20).replace(".000", ".100")
    merge = next(call for call in traces["alice"] if call["name"] == "Bash")
    merge["timestamp"] = stamp(20).replace(".000", ".200")
    merge["completed_at"] = stamp(20).replace(".000", ".900")
    evidence = STEP6.evaluate(manifest, snapshot, facts, traces)
    assert evidence["passed"], evidence["failed_checks"]


def test_unresolved_fixture_placeholders_fail_before_seeding() -> None:
    for value in ("<run_id>", "<account>", "/absolute/path/to/bob"):
        with pytest.raises(ValueError, match="placeholder"):
            STEP6.reject_placeholders(value)


def test_setup_failure_retries_in_place_with_same_identity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fake_step6_cli: Path
) -> None:
    origin = tmp_path / "origin"
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
    directory = tmp_path / "retry"
    original = STEP6.run
    count = 0

    def failed_clone(*args: Any, **kwargs: Any) -> str:
        nonlocal count
        if any(str(arg).endswith("bootstrap-workspace.py") for arg in args):
            count += 1
            if count == 2:
                raise OSError("transient clone failure")
        return str(original(*args, **kwargs))

    monkeypatch.setattr(STEP6, "run", failed_clone)
    with pytest.raises(OSError, match="transient"):
        STEP6.prepare(directory, str(origin), False)
    checkpoint = json.loads((directory / "setup.json").read_text())
    bob = (directory / "bob/.git/robo-agents-workspace.json").read_text()
    token = (directory / "token").read_text()
    monkeypatch.setattr(STEP6, "run", original)
    STEP6.prepare(directory, str(origin), False)
    assert STEP6.load_manifest(directory)["run_id"] == checkpoint["run_id"]
    assert (directory / "bob/.git/robo-agents-workspace.json").read_text() == bob
    assert (directory / "token").read_text() == token


def test_seeded_setup_recovers_ready_window_before_issue_creation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    directory = tmp_path / "ready-run"
    directory.mkdir()
    checkpoint = {
        "phase": "ready",
        "seed": True,
        "repository": None,
        "run_id": "unchanged-run-id",
        "scenario_sha256": STEP6.hashlib.sha256(
            (ROOT / "scenarios/step6-localhost-untrusted.json").read_bytes()
        ).hexdigest(),
    }
    STEP6.save(directory / "setup.json", checkpoint)
    (directory / "token").write_text("unchanged-private-token")
    for name in ("bob", "charlie", "driver"):
        (directory / name / ".git/info").mkdir(parents=True)
    mutations = []

    def runner(*args: Any, **kwargs: Any) -> str:
        if any(str(arg).endswith("bootstrap-workspace.py") for arg in args):
            name = args[-3]
            return json.dumps({"path": str(directory / name), "workspace_id": name + "-unchanged"})
        if args[:3] == ("gh", "issue", "create"):
            mutations.append("issue")
            return "https://github.com/RoboNater/robo-agents-sandbox/issues/77"
        if args[:3] == ("gh", "issue", "comment"):
            mutations.append("reservation")
        return "2.1.276 (Claude Code)" if args[0] == "claude" else "codex-cli 0.154.0"

    def github(*args: Any) -> dict[str, Any]:
        if args[0] == "api":
            return {
                "workflows": [{"state": "active", "name": "CI", "path": ".github/workflows/ci.yml"}]
            }
        return {
            "viewerPermission": "WRITE",
            "squashMergeAllowed": True,
            "mergeCommitAllowed": False,
            "rebaseMergeAllowed": False,
        }

    monkeypatch.setattr(STEP6, "run", runner)
    monkeypatch.setattr(STEP6, "gh", github)
    monkeypatch.setattr(STEP6, "green", lambda _: True)
    monkeypatch.setattr(STEP6, "claude_authenticated", lambda: True)
    monkeypatch.setattr(
        STEP6.subprocess,
        "run",
        lambda *args, **kwargs: subprocess.CompletedProcess([], 1, stdout="{}"),
    )
    STEP6.prepare(directory, seed=True)
    manifest = STEP6.load_manifest(directory)
    assert manifest["run_id"] == checkpoint["run_id"]
    assert (directory / "token").read_text() == "unchanged-private-token"
    assert manifest["workspaces"]["bob"]["workspace_id"] == "bob-unchanged"
    assert mutations == ["issue", "reservation"]
    assert json.loads((directory / "setup.json").read_text())["phase"] == "issue_creating"


def test_collect_success_correlates_fake_github_and_transcripts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, proof: tuple[Any, ...]
) -> None:
    manifest, snapshot, facts, traces = copy.deepcopy(proof)
    directory = tmp_path / "persistent run"
    directory.mkdir()
    manifest["run_dir"] = str(directory)
    manifest["alice_session_id"] = "test-session"
    manifest["claude_config_dir"] = str(tmp_path / "claude-config")
    manifest["workspaces"]["driver"] = {"path": str(directory / "driver")}
    manifest["workspaces"]["charlie"]["path"] = str(directory / "charlie")
    for record in facts["review_runs"]:
        record["workspace_path"] = str(directory / "charlie")
    for call in traces["charlie"]:
        if "review_check" in call:
            call["input"]["command"] = STEP6.shlex.join(
                [
                    "python3",
                    manifest["review_check_script"],
                    str(directory / "charlie"),
                    call["review_check"]["expected_head"],
                    manifest["run_id"],
                ]
            )
    review_audit = directory / "charlie/.git/step6-review-audit.jsonl"
    review_audit.parent.mkdir(parents=True)
    review_audit.write_text("\n".join(json.dumps(record) for record in facts["review_runs"]))
    manifest["disturbances"]["base"]["pr"] = {"number": 11}
    STEP6.save(directory / "run.json", manifest)
    facts["work_pr"]["mergeCommit"] = {"oid": "2" * 40}
    facts["merge_commit"]["sha"] = "2" * 40
    facts["merge_commit"]["commit"] = {"tree": {"sha": "tree-match"}}
    monkeypatch.setattr(STEP6, "audit", lambda _: snapshot)

    def github(*args: Any) -> Any:
        if args[0] == "pr":
            return facts["base_pr"] if str(args[2]) == "11" else facts["work_pr"]
        if args[0] == "issue":
            return facts["issue"]
        endpoint = args[-1]
        if endpoint.endswith("/check-runs"):
            return {"check_runs": facts["checks"][endpoint.split("/")[-2]]}
        if endpoint.endswith("/comments"):
            return [facts["comments"]]
        if endpoint.endswith(manifest["disturbances"]["head"]["new_head"]):
            return facts["head_commit"]
        return facts["merge_commit"]

    monkeypatch.setattr(STEP6, "gh", github)
    monkeypatch.setattr(
        STEP6, "run", lambda *args, **kwargs: "tree-match" if "rev-parse" in args else ""
    )
    monkeypatch.setattr(
        STEP6.subprocess, "run", lambda *args, **kwargs: subprocess.CompletedProcess([], 0)
    )
    for name in ("alice", "bob", "charlie"):
        lines = []
        for index, call in enumerate(traces[name]):
            if "review_check" in call:
                lines.append(
                    {
                        "type": "item.completed",
                        "item": {
                            "type": "command_execution",
                            "command": call["input"]["command"],
                            "status": call["status"],
                            "exit_code": call["exit_code"],
                            "aggregated_output": json.dumps(call["review_check"]),
                        },
                    }
                )
                continue
            lines.append(
                {
                    "timestamp": call.get("timestamp", stamp(1)),
                    "message": {
                        "content": [
                            {
                                "type": "tool_use",
                                "id": str(index),
                                "name": call["name"],
                                "input": call["input"],
                            }
                        ]
                    },
                }
            )
            if "merge_result" in call:
                lines.append(
                    {
                        "timestamp": call["completed_at"],
                        "message": {
                            "content": [
                                {
                                    "type": "tool_result",
                                    "tool_use_id": str(index),
                                    "content": call["merge_result"],
                                    "is_error": call["is_error"],
                                }
                            ]
                        },
                    }
                )
            if "result" in call:
                lines.append(
                    {
                        "message": {
                            "content": [
                                {
                                    "type": "tool_result",
                                    "tool_use_id": str(index),
                                    "content": json.dumps(call["result"]),
                                }
                            ]
                        }
                    }
                )
        (directory / f"{name}.transcript.jsonl").write_text(
            "\n".join(json.dumps(line) for line in lines)
        )
        if name != "alice":
            (directory / f"{name}.telemetry.jsonl").write_text(
                "\n".join(json.dumps(row) for row in facts["telemetry"][name])
            )
    project = re.sub(r"[^A-Za-z0-9]", "-", str(directory / "alice-runtime"))
    source = Path(manifest["claude_config_dir"]) / "projects" / project / "test-session.jsonl"
    source.parent.mkdir(parents=True)
    (directory / "alice.transcript.jsonl").rename(source)
    STEP6.collect(directory)
    evidence = json.loads((directory / "evidence.json").read_text())
    assert evidence["passed"], evidence["failed_checks"]
    assert (directory / "github-facts.json").exists()
    assert (directory / "hub-audit.json").exists()
    assert (directory / "tool-audit.json").exists()

    (directory / "token").write_text("private-token-" + "0" * 64)
    destination = tmp_path / "exports"
    STEP6.export_evidence(directory, destination)
    exports = list(destination.glob("*.json"))
    assert len(exports) == 5
    assert all(
        str(directory) not in path.read_text() and "private-token-" not in path.read_text()
        for path in exports
    )
    audit = json.loads((directory / "hub-audit.json").read_text())
    audit["decision"][0]["rationale"] = "private-token-" + "0" * 64
    STEP6.save(directory / "hub-audit.json", audit)
    with pytest.raises(ValueError, match="credential detected"):
        STEP6.export_evidence(directory, tmp_path / "rejected-exports")
    assert not (tmp_path / "rejected-exports").exists()


@pytest.mark.parametrize(
    "defect",
    [
        "missing",
        "head",
        "workspace",
        "identity",
        "time",
        "failure",
        "no_tool_result",
        "fake_invocation",
        "bad_source",
    ],
)
def test_review_snapshot_outside_persisted_clone_fails_proof(
    proof: tuple[Any, ...], defect: str
) -> None:
    manifest, snapshot, facts, traces = copy.deepcopy(proof)
    record = facts["review_runs"][0]
    if defect == "missing":
        facts["review_runs"] = []
    elif defect == "head":
        record["head_before"] = "0" * 40
    elif defect == "workspace":
        record["workspace_path"] = "/tmp/archive-snapshot"
    elif defect == "identity":
        record["workspace_id"] = "another-clone"
    elif defect == "time":
        record["started_at"] = stamp(0)
    elif defect == "failure":
        record["returncode"] = 1
    elif defect == "bad_source":
        record["source_head"] = "0" * 40
    elif defect == "fake_invocation":
        next(call for call in traces["charlie"] if "review_check" in call)["input"]["command"] = (
            "echo step6-review-check.py"
        )
    else:
        traces["charlie"] = [call for call in traces["charlie"] if "review_check" not in call]
    evidence = STEP6.evaluate(manifest, snapshot, facts, traces)
    assert "review_in_own_clone_1" in evidence["failed_checks"]


@pytest.mark.parametrize("failing", [False, True])
def test_review_check_entry_point_records_real_clone_test_execution(
    tmp_path: Path, failing: bool
) -> None:
    origin = tmp_path / "origin"
    subprocess.run(
        ["git", "init", "--initial-branch=main", str(origin)], check=True, capture_output=True
    )
    (origin / "tests").mkdir()
    (origin / ".gitignore").write_text("__pycache__/\n")
    (origin / "tests/test_fixture.py").write_text(
        "import unittest\nclass Fixture(unittest.TestCase):\n"
        f" def test_fixture(self): self.assertEqual(2 + 2, {3 if failing else 4})\n"
    )
    subprocess.run(
        ["git", "-C", str(origin), "add", "tests", ".gitignore"], check=True, capture_output=True
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
            "-m",
            "fixture",
        ],
        check=True,
        capture_output=True,
    )
    workspace = tmp_path / "charlie clone"
    subprocess.run(
        [str(ROOT / "scripts/bootstrap-workspace.sh"), "charlie", str(workspace), str(origin)],
        check=True,
        capture_output=True,
    )
    head = subprocess.check_output(
        ["git", "-C", str(workspace), "rev-parse", "HEAD"], text=True
    ).strip()
    command = [
        "python3",
        str(ROOT / "scripts/step6-review-check.py"),
        str(workspace),
        head,
        "test-run",
    ]
    result = subprocess.run(command, capture_output=True, text=True)
    audit = workspace / ".git/step6-review-audit.jsonl"
    record = json.loads(audit.read_text())
    assert json.loads(result.stdout.splitlines()[-1]) == record
    assert record["head_before"] == record["head_after"] == head
    assert record["workspace_path"] == str(workspace)
    assert result.returncode == record["returncode"] == (1 if failing else 0)
    assert record["clean_after"]
    assert audit.stat().st_mode & 0o077 == 0
    assert subprocess.run(command[:3] + ["0" * 40, "test-run"], capture_output=True).returncode != 0
    assert len(audit.read_text().splitlines()) == 1
    (workspace / "dirty-marker").write_text("preserve")
    assert subprocess.run(command, capture_output=True).returncode != 0
    assert (workspace / "dirty-marker").read_text() == "preserve"


def test_driver_pushes_new_branches_from_detached_head_with_real_git(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, proof: tuple[Any, ...]
) -> None:
    origin = tmp_path / "origin"
    subprocess.run(
        ["git", "init", "--initial-branch=main", str(origin)], check=True, capture_output=True
    )
    for key, value in (("user.name", "Test"), ("user.email", "test@example.com")):
        subprocess.run(["git", "-C", str(origin), "config", key, value], check=True)
    subprocess.run(
        ["git", "-C", str(origin), "commit", "--allow-empty", "-m", "baseline"],
        check=True,
        capture_output=True,
    )
    subprocess.run(["git", "-C", str(origin), "branch", "run/implement"], check=True)
    directory = tmp_path / "run"
    directory.mkdir()
    driver = directory / "driver"
    subprocess.run(["git", "clone", str(origin), str(driver)], check=True, capture_output=True)
    for key, value in (("user.name", "Test"), ("user.email", "test@example.com")):
        subprocess.run(["git", "-C", str(driver), "config", key, value], check=True)
    original = STEP6.run
    old = original("git", "rev-parse", "HEAD", cwd=driver)
    manifest, snapshot, _, _ = copy.deepcopy(proof)
    manifest.update(
        {
            "run_dir": str(directory),
            "implementation_branch": "run/implement",
            "base_branch": "run/base",
            "disturbances": {},
        }
    )
    manifest["workspaces"]["driver"] = {"path": str(driver)}
    for task in snapshot["task"]:
        task["pr_head_sha"] = old if task["id"] == "3" else task["pr_head_sha"]
        task["result_json"] = task["result_json"].replace("b" * 40, old)
    STEP6.save(directory / "run.json", manifest)
    work = {
        "url": manifest["work_pr"]["url"],
        "number": 10,
        "state": "OPEN",
        "headRefName": "run/implement",
        "baseRefName": "main",
        "headRefOid": old,
    }
    base_pr: dict[str, Any] = {"state": "OPEN", "headRefName": "run/base", "baseRefName": "main"}

    def runner(*args: Any, **kwargs: Any) -> str:
        if args[:3] == ("git", "remote", "get-url"):
            return "git@github.com:" + str(STEP6.SANDBOX) + ".git"
        if args[:3] == ("gh", "pr", "create"):
            return "https://github.com/" + str(STEP6.SANDBOX) + "/pull/11"
        if args[:3] == ("gh", "pr", "merge"):
            base_pr.update(
                {"state": "MERGED", "mergeCommit": {"oid": "e" * 40}, "mergedAt": stamp(14)}
            )
            return ""
        return str(original(*args, **kwargs))

    def view(number: Any) -> dict[str, Any]:
        if str(number) == "11":
            base_pr["headRefOid"] = STEP6.load_manifest(directory)["disturbances"]["base"]["head"]
            return base_pr
        return work

    monkeypatch.setattr(STEP6, "run", runner)
    monkeypatch.setattr(STEP6, "audit", lambda _: snapshot)
    monkeypatch.setattr(STEP6, "pr_view", view)
    monkeypatch.setattr(STEP6, "gh", lambda *args: [])
    monkeypatch.setattr(STEP6, "green", lambda _: True)
    monkeypatch.setattr(STEP6, "checks", lambda _: [{"name": "test", "conclusion": "success"}])
    assert not STEP6.driver_once(directory)
    moved = STEP6.load_manifest(directory)["disturbances"]["head"]["new_head"]
    assert moved != old
    rejected = subprocess.run(
        ["git", "-C", str(driver), "push", "origin", "HEAD:old-short-destination"],
        capture_output=True,
        text=True,
    )
    assert rejected.returncode != 0 and "not a full refname" in rejected.stderr
    work["headRefOid"] = moved
    snapshot["task"][4]["pr_head_sha"] = moved
    snapshot["task"][4]["result_json"] = snapshot["task"][4]["result_json"].replace("c" * 40, moved)
    assert STEP6.driver_once(directory)
    assert original("git", "rev-parse", "refs/heads/run/implement", cwd=origin) == moved
    base = STEP6.load_manifest(directory)["disturbances"]["base"]["head"]
    assert original("git", "rev-parse", "refs/heads/run/base", cwd=origin) == base
    assert original("git", "rev-parse", "main", cwd=origin) == old


@pytest.mark.parametrize("failure", ["workflow", "main_ci", "coordination_access"])
def test_seed_preflight_fails_before_any_mutation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    directory = tmp_path / "must-not-exist"
    calls: list[tuple[Any, ...]] = []

    def runner(*args: Any, **kwargs: Any) -> str:
        calls.append(args)
        return '{"loggedIn":true}' if args[:3] == ("claude", "auth", "status") else ""

    def github(*args: Any) -> dict[str, Any]:
        if args[0] == "repo":
            if args[2] == "RoboNater/robo-agents":
                return {"viewerPermission": "READ" if failure == "coordination_access" else "ADMIN"}
            return {
                "viewerPermission": "ADMIN",
                "squashMergeAllowed": True,
                "mergeCommitAllowed": False,
                "rebaseMergeAllowed": False,
            }
        return {
            "workflows": [
                {
                    "state": "active",
                    "path": "wrong.yml" if failure == "workflow" else ".github/workflows/ci.yml",
                    "name": "CI",
                }
            ]
        }

    monkeypatch.setattr(STEP6, "run", runner)
    monkeypatch.setattr(STEP6, "gh", github)
    monkeypatch.setattr(STEP6, "green", lambda _: failure != "main_ci")
    with pytest.raises(ValueError):
        STEP6.prepare(directory, seed=True)
    assert not directory.exists()
    assert not any("create" in args or "push" in args or "comment" in args for args in calls)


@pytest.mark.parametrize("failure", ["checkpoint", "head", "parent", "file", "content"])
def test_base_fixture_refuses_ambiguous_remote_branch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    manifest = {"run_id": "unique"}
    base = {"head": None if failure == "checkpoint" else "a" * 40, "old_main": "b" * 40}

    def runner(*args: Any, **kwargs: Any) -> str:
        if args[1] == "rev-parse":
            return "c" * 40 if failure == "parent" else "b" * 40
        if args[1] == "diff":
            return "valuable.py" if failure == "file" else "step6_base_unique.txt"
        return "wrong" if failure == "content" else "Unrelated base movement for unique"

    monkeypatch.setattr(STEP6, "run", runner)
    with pytest.raises(ValueError):
        STEP6.validate_base_fixture(
            tmp_path, manifest, base, "c" * 40 if failure == "head" else "a" * 40
        )


def test_windows_powershell_wrapper_is_unwrapped() -> None:
    wrapper = r'"C:\Program Files\PowerShell\7\pwsh.exe" -Command '
    assert STEP6.shell_payload(wrapper + "'gh pr merge 10 --squash'") == "gh pr merge 10 --squash"
    assert STEP6.shell_payload(wrapper + "'echo ''x'''") == "echo 'x'"
    assert STEP6.shell_payload('pwsh -NoProfile -Command "git log -1"') == "git log -1"
    assert STEP6.shell_payload("bash -lc 'ls'") == "bash -lc 'ls'"


def test_codex_app_server_supervisor_log_yields_timed_calls() -> None:
    gate = {"pr_url": "u", "expected_head_sha": "a" * 40, "head_matches": False}
    command = r""""C:\pwsh.exe" -Command 'gh pr merge u --squash'"""
    lines = [
        {"sent_at": stamp(1), "message": {"id": 1, "method": "turn/start"}},
        {
            "received_at": stamp(2),
            "message": {
                "method": "item/started",
                "params": {
                    "item": {
                        "id": "g",
                        "type": "mcpToolCall",
                        "server": "hub",
                        "tool": "check_merge_gate",
                        "arguments": {"pr_url": "u"},
                        "status": "inProgress",
                    }
                },
            },
        },
        {
            "received_at": stamp(3),
            "message": {
                "method": "item/completed",
                "params": {
                    "item": {
                        "id": "g",
                        "type": "mcpToolCall",
                        "server": "hub",
                        "tool": "check_merge_gate",
                        "arguments": {"pr_url": "u"},
                        "status": "completed",
                        "result": {"content": [{"type": "text", "text": json.dumps(gate)}]},
                    }
                },
            },
        },
        {
            "received_at": stamp(4),
            "message": {
                "method": "item/completed",
                "params": {
                    "item": {
                        "id": "m",
                        "type": "commandExecution",
                        "command": command,
                        "status": "completed",
                        "exitCode": 0,
                        "aggregatedOutput": "merged",
                    }
                },
            },
        },
    ]
    calls = STEP6.tool_calls("\n".join(json.dumps(line) for line in lines))
    assert calls[0]["name"] == "mcp__hub__check_merge_gate"
    assert (calls[0]["timestamp"], calls[0]["completed_at"]) == (stamp(2), stamp(3))
    assert calls[0]["result"] == gate and calls[0]["is_error"] is False
    assert calls[1]["input"]["command"] == "gh pr merge u --squash"
    assert calls[1]["merge_result"] == json.dumps("merged") and calls[1]["is_error"] is False


def test_opencode_events_yield_review_check_and_mcp_calls() -> None:
    report = {"review_check": "step6", "expected_head": "a" * 40}
    events = [
        {
            "type": "tool_use",
            "part": {
                "type": "tool",
                "tool": "hub_check_in",
                "state": {
                    "status": "completed",
                    "input": {"model": "m"},
                    "time": {"start": 0, "end": 1000},
                },
            },
        },
        {
            "type": "tool_use",
            "part": {
                "type": "tool",
                "tool": "bash",
                "state": {
                    "status": "completed",
                    "input": {"command": "python3 check.py"},
                    "output": "ok\n" + json.dumps(report),
                    "metadata": {"exit": 0},
                    "time": {"start": 2000, "end": 3000},
                },
            },
        },
        {"type": "text", "part": {"text": "done"}},
    ]
    calls = STEP6.tool_calls("\n".join(json.dumps(event) for event in events))
    assert [call["name"] for call in calls] == ["mcp__hub__check_in", "Bash"]
    assert calls[0]["timestamp"] == "1970-01-01T00:00:00.000Z"
    assert calls[1]["review_check"] == report and calls[1]["exit_code"] == 0


def test_manifest_topology_sets_expected_worker_profiles(proof: tuple[Any, ...]) -> None:
    manifest, snapshot, facts, traces = copy.deepcopy(proof)
    assert (
        "charlie_profile" not in STEP6.evaluate(manifest, snapshot, facts, traces)["failed_checks"]
    )
    manifest["harnesses"] = {"alice": "codex", "bob": "claude-code", "charlie": "opencode"}
    manifest["providers"] = {"bob": "anthropic", "charlie": "openrouter"}
    manifest["versions"]["opencode"] = "1.18.31"
    assert STEP6.harness_version(manifest, "charlie") == "1.18.31"
    assert "charlie_profile" in STEP6.evaluate(manifest, snapshot, facts, traces)["failed_checks"]
    charlie = next(row for row in snapshot["agent"] if row["name"] == "charlie")
    charlie.update({"harness": "opencode", "provider": "openrouter", "harness_version": "1.18.31"})
    assert (
        "charlie_profile" not in STEP6.evaluate(manifest, snapshot, facts, traces)["failed_checks"]
    )


def test_windows_style_paths_detect_other_workspace_access(proof: tuple[Any, ...]) -> None:
    manifest, snapshot, facts, traces = copy.deepcopy(proof)
    bob = manifest["workspaces"]["bob"]["path"]
    traces["charlie"].append(
        {"name": "read", "input": {"filePath": bob.replace("/", os.sep) + os.sep + "x"}}
    )
    assert (
        "charlie_no_other_workspace_access"
        in STEP6.evaluate(manifest, snapshot, facts, traces)["failed_checks"]
    )


@pytest.mark.skipif(os.name != "nt", reason="MSYS drive spelling exists only on Windows")
@pytest.mark.parametrize(
    "call",
    [
        {"name": "Bash", "input": {"command": "cat /c/runs/bob/secret"}},
        {"name": "Bash", "input": {"command": "cd /c/runs/bob && cat secret"}},
        {"name": "read", "input": {"filePath": "/c/runs/bob/secret"}},
    ],
)
def test_git_bash_drive_paths_detect_other_workspace_access(
    proof: tuple[Any, ...], call: dict[str, Any]
) -> None:
    manifest, snapshot, facts, traces = copy.deepcopy(proof)
    manifest["workspaces"]["bob"]["path"] = r"C:\runs\bob"
    manifest["workspaces"]["charlie"]["path"] = r"C:\runs\charlie"
    traces["charlie"].append(call)
    assert (
        "charlie_no_other_workspace_access"
        in STEP6.evaluate(manifest, snapshot, facts, traces)["failed_checks"]
    )


def test_own_workspace_paths_are_not_cross_access() -> None:
    workspace, other = str(Path("/runs/charlie").resolve()), str(Path("/runs/bob").resolve())
    for command in ("git status", r"python3 .\tests\run.py", "ls ../charlie"):
        assert "other_workspace" not in STEP6.recorded_shell_actions(command, workspace, other)


@pytest.mark.parametrize(
    "rationale",
    [
        "null",
        "[]",
        '"text"',
        '{"urls": 3}',
        "not json",
        '{"urls": []} {"urls": []}',
        'Prefix {"urls": []} suffix {"urls": []}',
        '{"urls": []}}',
        '{"urls": [}',
    ],
)
def test_wrong_shaped_follow_ups_fail_closed(proof: tuple[Any, ...], rationale: str) -> None:
    manifest, snapshot, facts, traces = copy.deepcopy(proof)
    row = next(row for row in snapshot["decision"] if row["key"] == "step6:follow-ups")
    row["rationale"] = rationale
    assert (
        "follow_ups_verified" in STEP6.evaluate(manifest, snapshot, facts, traces)["failed_checks"]
    )


def test_prose_prefixed_follow_ups_pass(proof: tuple[Any, ...]) -> None:
    manifest, snapshot, facts, traces = copy.deepcopy(proof)
    row = next(row for row in snapshot["decision"] if row["key"] == "step6:follow-ups")
    row["rationale"] = 'Verified close-out. Follow-up URLs JSON: {"urls": []}. No action.'
    evidence = STEP6.evaluate(manifest, snapshot, facts, traces)
    assert "follow_ups_verified" not in evidence["failed_checks"]
    assert evidence["passed"]


def test_codex_exec_windows_command_is_unwrapped() -> None:
    line = {
        "item": {
            "type": "command_execution",
            "command": r""""C:\pwsh.exe" -Command 'git push origin x'""",
            "status": "completed",
            "exit_code": 0,
        }
    }
    call = STEP6.tool_calls(json.dumps(line))[0]
    assert call["input"]["command"] == "git push origin x"
    assert "push" in STEP6.recorded_shell_actions(call["input"]["command"], "/w", "/o")


def test_telemetry_ignores_partially_written_records(tmp_path: Path) -> None:
    spec = importlib.util.spec_from_file_location("step6_launch", ROOT / "scripts/step6_launch.py")
    assert spec and spec.loader
    launch = importlib.util.module_from_spec(spec)
    import sys

    sys.modules.setdefault("step6", STEP6)
    spec.loader.exec_module(launch)
    path = tmp_path / "telemetry.jsonl"
    path.write_text('{"outcome": "idle"}\n')
    telemetry = launch.Telemetry(path)
    with path.open("a") as stream:
        stream.write('{"outcome": "release", "worker_')
    assert telemetry.released() is False
    with path.open("a") as stream:
        stream.write('instance_id": "x"}\n[]\n')
    assert telemetry.released() is True
