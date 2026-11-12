"""The scripted run start, prep-standard-run's M2 successor (#268).

Settings tests load ``scripts/start-run.py`` in process: precedence (defaults,
then the file, then the command line), refused keys, the issue/work-file choice,
config generation and the statement and launch commands it derives. The
end-to-end tests run the script as the operator would, under the isolated
smoke environment (#145): their own state, configuration, operator token and
port block, a local origin, and ``scripts/mock-harness.py`` shims standing in
for ``claude`` and ``codex``. Each starts and stops its own hub and reaches a
``done`` run with no further input.
"""

from __future__ import annotations

import importlib.util
import json
import os
import sqlite3
import subprocess
import sys
import tomllib
from pathlib import Path
from typing import Any

import pytest
from conftest import reserve_port_block
from timing import LIVENESS_S

ROOT = Path(__file__).resolve().parents[1]


def load(name: str, path: Path) -> Any:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


START = load("start_run_cli", ROOT / "scripts/start-run.py")
HARNESS = load("mock_harness_cli", ROOT / "scripts/mock-harness.py")
PREP = load("prep_standard_run_area_for_start_run", ROOT / "scripts/prep-standard-run-area.py")


def settings(*words: str) -> tuple[dict[str, Any], dict[str, Any]]:
    values, policy = START.settings(START.parser().parse_args(list(words)))
    return values, policy


def write(path: Path, text: str) -> Path:
    path.write_text(text, encoding="utf-8")
    return path


# -- settings --------------------------------------------------------------------------


def test_the_command_line_overrides_the_file_which_overrides_the_defaults(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    configs = tmp_path / "configs"
    configs.mkdir()
    (configs / "work.md").write_text("Do it.\n", encoding="utf-8")
    config = write(
        configs / "run.toml",
        'run_parent_dir = "parent"\nrun_dir = "first"\naccount = "file-bot"\n'
        'work_file = "work.md"\nbob_model = "file-model"\nbob_effort = "high"\n'
        "pytest_workers = 2\nresume_delay_s = 7\n\n[policy]\nmax_review_rounds = 4\n",
    )
    monkeypatch.chdir(tmp_path)

    values, policy = settings(
        "--config", str(config), "--account", "cli-bot", "--bob-effort", "max",
        "--policy", "max_review_rounds=5", "--policy", "allow_no_ci=auto",
    )  # fmt: skip

    assert values["account"] == "cli-bot"
    assert values["bob_model"] == "file-model"
    assert values["bob_effort"] == "max"
    assert values["pytest_workers"] == 2
    assert values["resume_delay_s"] == 7.0
    assert values["charlie_harness"] == START.DEFAULTS["charlie_harness"]
    # File paths are relative to the file; run_dir to run_parent_dir.
    assert values["work_file"] == str((configs / "work.md").resolve())
    assert values["run_parent_dir"] == str((configs / "parent").resolve())
    assert values["run_dir"] == str((configs / "parent" / "first").resolve())
    assert policy == {"max_review_rounds": 5, "allow_no_ci": "auto"}


def test_command_line_paths_are_relative_to_the_working_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    (tmp_path / "repo").mkdir()

    values, _ = settings("--repository", "repo", "--run-parent-dir", "runs", "--run-dir", "r1")

    assert values["repository"] == str((tmp_path / "repo").resolve())
    assert values["run_dir"] == str((tmp_path / "runs" / "r1").resolve())


@pytest.mark.parametrize(
    ("text", "message"),
    [
        ('surprise = "x"\n', "unknown config keys: surprise"),
        ('roadmap = "#2"\n', "roadmap was renamed to reservations"),
        ('issue = "42"\n', "issue must be a int"),
        ("auto_start = 1\n", "auto_start must be a bool"),
        ('resume_total_s = "1h"\n', "resume_total_s must be a number of seconds"),
        ('policy = "squash"\n', "policy must be a table"),
        ("stall_after_min = 5\n[policy]\nstall_after_min = 6\n", "set both at the top level"),
        ("[policy]\nsurprise = 1\n", "surprise"),
        ('[policy]\nmerge_method = "octopus"\n', "merge_method"),
        ('[policy]\n"bad key" = 1\n', "invalid policy key"),
    ],
)
def test_unknown_or_mistyped_keys_are_refused(tmp_path: Path, text: str, message: str) -> None:
    config = write(tmp_path / "run.toml", text)

    with pytest.raises(ValueError, match=message.replace("(", r"\(")):
        settings("--config", str(config))


def test_unknown_or_invalid_policy_overrides_are_refused() -> None:
    with pytest.raises(ValueError, match="surprise"):
        settings("--policy", "surprise=1")
    with pytest.raises(ValueError, match="max_review_rounds"):
        settings("--policy", "max_review_rounds=0")
    with pytest.raises(ValueError, match="k=v"):
        settings("--policy", "max_review_rounds")


def test_issue_and_work_file_are_mutually_exclusive(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    work = write(tmp_path / "work.md", "Do it.\n")
    with pytest.raises(SystemExit) as exited:
        START.parser().parse_args(["--issue", "1", "--work-file", str(work)])
    assert exited.value.code == 2
    assert "not allowed with argument" in capsys.readouterr().err

    both = write(tmp_path / "both.toml", f"issue = 1\nwork_file = {json.dumps(str(work))}\n")
    with pytest.raises(ValueError, match="choose either issue or work_file"):
        settings("--config", str(both))

    # Selecting the work on the command line replaces the file's selection.
    issue = write(tmp_path / "issue.toml", "issue = 7\n")
    values, _ = settings("--config", str(issue), "--work-file", str(work))
    assert values["issue"] == 0 and values["work_file"] == str(work.resolve())
    worked = write(tmp_path / "worked.toml", f"work_file = {json.dumps(str(work))}\n")
    values, _ = settings("--config", str(worked), "--issue", "9")
    assert values["issue"] == 9 and values["work_file"] == ""


def test_generated_default_config_reads_back_as_the_defaults(tmp_path: Path) -> None:
    path = tmp_path / "default.toml"

    assert START.main(["--generate-default-config", str(path)]) == 0
    values, policy = START.load_config(path)

    assert policy == {}
    assert values == START.DEFAULTS | {
        # A path in the file is read relative to the file.
        "run_parent_dir": START.canonical_path(str(START.DEFAULTS["run_parent_dir"]), tmp_path)
    }
    text = path.read_text(encoding="utf-8")
    assert "[policy]" in text and "# merge_method" in text
    # Generation never overwrites.
    assert START.main(["--generate-default-config", str(path)]) == 1


def test_generated_config_saves_the_merged_settings_for_the_next_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    write(tmp_path / "work.md", "Do it.\n")
    config = write(
        tmp_path / "run.toml",
        'run_dir = "/abs/run"\naccount = "bot"\ncharlie_model = "gpt-x"\n'
        "[policy]\nrole_policy = { reviewer_harness_differs = false }\n",
    )
    merged = tmp_path / "merged.toml"

    code = START.main(
        ["--config", str(config), "--work-file", "work.md", "--alice-effort", "high",
         "--no-auto-start", "--policy", 'merge_method="rebase"', "--generate-config", str(merged)]
    )  # fmt: skip

    assert code == 0
    values, policy = START.load_config(merged)
    assert values["alice_effort"] == "high"
    assert values["charlie_model"] == "gpt-x"
    assert values["auto_start"] is False
    assert values["work_file"] == str((tmp_path / "work.md").resolve())
    assert policy == {"role_policy.reviewer_harness_differs": False, "merge_method": "rebase"}
    # The saved file starts the same run with no command-line options.
    again, policy_again = settings("--config", str(merged))
    original, original_policy = settings(
        "--config", str(config), "--work-file", "work.md", "--alice-effort", "high",
        "--no-auto-start", "--policy", 'merge_method="rebase"',
    )  # fmt: skip
    assert (again, policy_again) == (original, original_policy)


def test_a_prep_standard_run_config_is_read_as_it_is(tmp_path: Path) -> None:
    path = tmp_path / "standard.toml"
    PREP.write_config(path, PREP.DEFAULTS | {"stall_after_min": 25, "alice_harness": "codex"})

    values, policy = START.load_config(path)

    assert values["alice_harness"] == "codex"
    assert values["auto_start"] is True
    # Its top-level stall keys are policy keys now.
    assert policy == {"stall_after_min": 25, "stall_action_after_min": 0}
    assert set(PREP.DEFAULTS) - set(START.DEFAULTS) == set(START.POLICY_SHORTHANDS)


def test_starting_requires_the_run_inputs_and_refuses_unsafe_ones(tmp_path: Path) -> None:
    values, policy = settings("--run-dir", str(tmp_path / "run"))
    base = dict(values) | {"repository": "https://github.com/o/r.git", "account": "a", "issue": 1}
    START.validate(base, policy, starting=True)
    for key in ("repository", "run_dir", "account"):
        with pytest.raises(ValueError, match=f"{key} is required"):
            START.validate(base | {key: ""}, policy, starting=True)
    with pytest.raises(ValueError, match="choose --issue or --work-file"):
        START.validate(base | {"issue": 0}, policy, starting=True)
    with pytest.raises(ValueError, match="outside the robomate checkout"):
        START.validate(base | {"run_dir": str(ROOT / "run")}, policy, starting=True)
    with pytest.raises(ValueError, match="does not exist"):
        START.validate(
            base | {"issue": 0, "work_file": str(tmp_path / "missing.md")}, policy, starting=True
        )
    empty = write(tmp_path / "empty.md", "\n")
    with pytest.raises(ValueError, match="is empty"):
        START.validate(base | {"issue": 0, "work_file": str(empty)}, policy, starting=True)


@pytest.mark.parametrize(
    ("words", "message"),
    [
        (["--bob-harness", "vim"], "invalid bob_harness"),
        (["--bob-harness", "devin", "--bob-effort", "high"], "devin has no reasoning-effort"),
        (["--denise-settings-from", "nobody"], "denise_settings_from"),
        (["--hub-name", "Bad Name"], "invalid hub name"),
        (["--hub-port", "70000"], "hub_port"),
        (["--forge", "bitbucket"], "forge must be"),
        (["--max-resumes", "-2"], "max_resumes"),
        (["--resume-delay-s", "-1"], "resume_delay_s"),
        (["--pytest-workers", "-1"], "pytest_workers"),
    ],
)
def test_invalid_settings_are_refused_before_anything_starts(
    words: list[str], message: str
) -> None:
    with pytest.raises(ValueError, match=message):
        settings(*words)


def test_harness_aliases_and_the_hub_name(tmp_path: Path) -> None:
    values, _ = settings("--alice-harness", "Claude", "--bob-harness", "agy")
    assert START.harness_of(values, "alice") == "claude-code"
    assert START.harness_of(values, "bob") == "antigravity"
    named, _ = settings("--run-dir", str(tmp_path / "Issue 268"))
    assert START.hub_name(named) == "issue-268"
    assert START.hub_name(named | {"hub_name": "chosen"}) == "chosen"


# -- what a start derives -----------------------------------------------------------


def test_an_issue_statement_carries_the_counter_rules_and_the_account(tmp_path: Path) -> None:
    values, _ = settings("--issue", "42", "--account", "the-bot", "--reservations", "#409")

    text = START.compose_statement(values, origin="git@github.com:octo/repo.git", forge="github")

    assert text.startswith(
        "Address issue `octo/repo#42`, merge its pull request, and close out. "
        "The run's reservations issue is (octo/repo#409); read it only if a counter is needed. "
    )
    assert START.COUNTER_CLOSE_OUT in text
    assert text.endswith("\n\nForge comment identity account: `the-bot`.")


def test_a_work_file_statement_keeps_the_work_and_names_no_reservations(tmp_path: Path) -> None:
    work = write(tmp_path / "work.md", "  Rewrite the guide.\n\nKeep it short.\n")
    values, _ = settings("--work-file", str(work), "--account", "the-bot")

    text = START.compose_statement(values, origin="https://gitlab.example/g/p.git", forge="gitlab")

    assert text.startswith("Rewrite the guide.\n\nKeep it short.\n\nWhen done, close out. ")
    assert START.NO_RESERVATIONS in text
    assert "`the-bot`" in text


def test_each_agent_starts_headless_with_its_harness_model_effort_and_bounds(
    tmp_path: Path,
) -> None:
    values, policy = settings(
        "--run-dir", str(tmp_path / "run"), "--bob-model", "m-b", "--bob-effort", "max",
        "--charlie-model", "gpt-c", "--pytest-workers", "3", "--max-resumes", "2",
        "--resume-total-s", "90",
    )  # fmt: skip
    run_dir = Path(values["run_dir"])
    run = START.Run(values, policy, run_dir, run_dir / "run", "run")
    run.versions = {"claude-code": "2.1.999", "codex": "0.999.0"}

    for agent, role in START.AGENTS.items():
        words = START.launch_words(run, agent, interactive=False)
        harness = START.harness_of(values, agent)

        def option(flag: str, words: list[str] = words) -> str:
            return words[words.index(flag) + 1]

        assert words[1:4] == [str(ROOT / "scripts/agent-launcher.py"), "start", agent]
        assert option("--role") == role
        assert option("--harness") == harness
        assert option("--hub") == str(run.checkout)
        assert option("--harness-version") == run.versions[harness]
        cache = run_dir / "tmp" / agent / "uv-cache"
        assert option("--uv-cache-dir") == str(cache)
        assert cache.parent.is_dir()
        if os.name != "nt":
            assert cache.parent.stat().st_mode & 0o777 == 0o700
        assert option("--pytest-workers") == "3"
        assert option("--max-resumes") == "2"
        assert option("--resume-total-s") == "90"
        assert "--interactive" not in words
        model, effort = values[f"{agent}_model"], values[f"{agent}_effort"]
        assert (option("--model") if model else None) == (model or None)
        assert (option("--effort") if effort else None) == (effort or None)
        interactive = START.launch_words(run, agent, interactive=True)
        assert interactive[-1] == "--interactive" and "--max-resumes" not in interactive


# -- end to end ----------------------------------------------------------------------


def git(*words: str, cwd: Path | None = None) -> str:
    return subprocess.run(
        ["git", *words], cwd=cwd, check=True, capture_output=True, text=True
    ).stdout.strip()


@pytest.fixture
def smoke(tmp_path: Path) -> dict[str, Any]:
    """The isolated smoke environment (#145) with a local origin and harness shims."""

    source = tmp_path / "source"
    source.mkdir()
    git("init", "-q", "-b", "main", cwd=source)
    git(
        "-c", "user.name=t", "-c", "user.email=t@example.com",
        "commit", "-q", "--allow-empty", "-m", "start", cwd=source,
    )  # fmt: skip
    origin = tmp_path / "origin.git"
    git("clone", "-q", "--bare", str(source), str(origin))
    shims = tmp_path / "bin"
    HARNESS.install(shims)
    env = {
        key: value
        for key, value in os.environ.items()
        if key not in ("ROBOMATE_HUB_URL", "HUB_STATE_DIR", "PYTEST_XDIST_AUTO_NUM_WORKERS")
        and not key.startswith(("ROBOMATE_TOKEN", "HUB_"))
    } | {
        "PATH": str(shims) + os.pathsep + os.environ.get("PATH", ""),
        "XDG_STATE_HOME": str(tmp_path / "xdg-state"),
        "XDG_CONFIG_HOME": str(tmp_path / "xdg-config"),
        "LOCALAPPDATA": str(tmp_path / "localappdata"),
        "APPDATA": str(tmp_path / "appdata"),
        "ROBOMATE_OPERATOR_TOKEN_FILE": str(tmp_path / "operator-token"),
        "ROBOMATE_BASE_PORT": str(reserve_port_block()),
        "MOCK_HARNESS_LOG": str(tmp_path / "harness.jsonl"),
        # Hang bounds for the stand-ins, not budgets.
        "MOCK_HARNESS_DEADLINE_S": str(4 * LIVENESS_S),
        "PYTHONIOENCODING": "utf-8",
    }
    config = write(
        tmp_path / "run.toml",
        f"repository = {json.dumps(str(origin))}\n"
        f"run_parent_dir = {json.dumps(str(tmp_path / 'runs'))}\n"
        'run_dir = "scripted"\naccount = "example-bot"\nforge = "github"\n'
        'alice_model = "claude-opus-5-5"\nalice_effort = "high"\n'
        'bob_model = "claude-sonnet-5-5"\nbob_effort = "max"\n'
        'charlie_model = "gpt-5.6-sol"\ncharlie_effort = "medium"\n'
        "pytest_workers = 3\nmax_resumes = 0\n\n[policy]\nmax_review_rounds = 2\n",
    )
    return {"root": tmp_path, "env": env, "config": config, "origin": origin}


def agent_rows(database: Path) -> dict[str, dict[str, Any]]:
    connection = sqlite3.connect(database)
    connection.row_factory = sqlite3.Row
    try:
        return {row["name"]: dict(row) for row in connection.execute("SELECT * FROM agent")}
    finally:
        connection.close()


@pytest.mark.parametrize("selection", ["issue", "work_file"])
def test_one_start_runs_the_team_headless_to_a_done_run(
    smoke: dict[str, Any], selection: str
) -> None:
    root: Path = smoke["root"]
    work = write(root / "statement.md", "Scripted work: change nothing.\n")
    select = ["--issue", "7"] if selection == "issue" else ["--work-file", str(work)]

    done = subprocess.run(
        [sys.executable, str(ROOT / "scripts/start-run.py"), "--config", str(smoke["config"]),
         *select],
        cwd=root,
        env=smoke["env"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        # A hang bound for a hub start and three agents, not a budget.
        timeout=5 * LIVENESS_S,
    )  # fmt: skip

    run_dir = root / "runs" / "scripted"
    logs = "\n".join(
        f"--- {path.name} ---\n{path.read_text(encoding='utf-8', errors='replace')[-3000:]}"
        for path in sorted((run_dir / "logs").glob("*.log"))
    )
    assert done.returncode == 0, f"{done.stdout}\n{done.stderr}\n{logs}"
    assert "workflow done" in done.stdout
    assert "hub scripted stopped" in done.stdout
    checkout = run_dir / "scripted"
    state = run_dir / "scripted.robomate" / "scripted" / "state"

    # The hub this start began is stopped.
    status = subprocess.run(
        [sys.executable, "-m", "robomate", "status", "--json"],
        cwd=checkout,
        env=smoke["env"],
        capture_output=True,
        text=True,
        timeout=2 * LIVENESS_S,
    )
    assert status.returncode == 1 and json.loads(status.stdout)["running"] is False

    # Each agent was launched once, headless, in its own worktree, with its settings.
    launched = [
        json.loads(line)
        for line in (root / "harness.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    by_agent = {Path(record["cwd"]).name: record for record in launched}
    assert sorted(by_agent) == ["alice", "bob", "charlie"] and len(launched) == 3
    values = tomllib.loads(smoke["config"].read_text(encoding="utf-8"))
    versions = {"claude-code": "2.1.999", "codex": "0.999.0"}
    for agent, record in by_agent.items():
        harness = "codex" if agent == "charlie" else "claude-code"
        argv = record["argv"]
        assert record["harness"] == harness
        assert argv[argv.index("--model") + 1] == values[f"{agent}_model"]
        if harness == "codex":
            assert argv[:2] == ["exec", "-C"] and argv[-1] == "-"
            assert f"model_reasoning_effort={values['charlie_effort']}" in argv
        else:
            assert argv[-2] == "-p"
            assert argv[-1] == f"Join robomate as {agent}, {START.AGENTS[agent]}."
            assert argv[argv.index("--effort") + 1] == values[f"{agent}_effort"]
        assert not any("--mcp-config" in word or "prompt" in word.lower() for word in argv)
        assert record["env"]["PYTEST_XDIST_AUTO_NUM_WORKERS"] == "3"
        cache = Path(record["env"]["UV_CACHE_DIR"])
        assert cache == run_dir / "tmp" / agent / "uv-cache"
        if os.name != "nt":
            assert cache.stat().st_mode & 0o777 == 0o700
        assert record["bridge_env"]["HUB_HARNESS_VERSION"] == versions[harness]

    # The hub recorded each agent's launcher identity with source env (#402).
    agents = agent_rows(state / "hub.db")
    for agent in START.AGENTS:
        row = agents[agent]
        harness = "codex" if agent == "charlie" else "claude-code"
        assert row["harness"] == harness and row["harness_source"] == "env"
        assert row["harness_version"] == versions[harness]
        assert row["provider"] == ("openai" if agent == "charlie" else "anthropic")
        assert (row["model"], row["model_source"]) == (values[f"{agent}_model"], "env")
        assert (row["effort"], row["effort_source"]) == (values[f"{agent}_effort"], "env")

    # The submitted statement: the work, the counter rules and the account.
    connection = sqlite3.connect(state / "hub.db")
    try:
        goal, policy, status_value = connection.execute(
            "SELECT goal, policy_json, status FROM workflow"
        ).fetchone()
    finally:
        connection.close()
    assert status_value == "done"
    expected_start = (
        "Address issue `#7`, merge its pull request, and close out."
        if selection == "issue"
        else "Scripted work: change nothing.\n\nWhen done, close out."
    )
    assert goal.startswith(expected_start)
    assert goal.endswith("Forge comment identity account: `example-bot`.")
    assert json.loads(policy)["max_review_rounds"] == 2

    # Closeout reports were collected in the hub and exported (#215).
    run_id = next(path.name for path in (state / "runs").iterdir())
    assert sorted(p.name for p in (state / "runs" / run_id / "closeout").glob("report-*.md"))

    # Nothing rendered: the run directory holds the checkout, the hub's directory,
    # the agents' logs and their uv caches, and no config, prompt or script.
    assert sorted(path.name for path in run_dir.iterdir()) == [
        "logs",
        "scripted",
        "scripted.robomate",
        "tmp",
    ]
    assert sorted(path.name for path in (run_dir / "logs").iterdir()) == [
        "alice.log",
        "bob.log",
        "charlie.log",
        "hub.log",
    ]
    assert sorted(path.name for path in (run_dir / "tmp").iterdir()) == ["alice", "bob", "charlie"]
