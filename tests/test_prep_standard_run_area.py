"""The interim helper merges configuration without losing paths or work selection."""

from __future__ import annotations

import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tomllib
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "prep_standard_run_area", ROOT / "scripts/prep-standard-run-area.py"
)
assert SPEC and SPEC.loader
HELPER = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(HELPER)


def test_removed_roadmap_inputs_name_their_replacements(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    with pytest.raises(SystemExit) as error:
        HELPER.main(["--roadmap", "owner/repo#2"])
    assert error.value.code == 2
    assert "--roadmap was renamed to --reservations" in capsys.readouterr().err
    config = tmp_path / "old.toml"
    config.write_text('roadmap = "owner/repo#2"\n', encoding="utf-8")
    output = tmp_path / "output.toml"
    with pytest.raises(SystemExit) as error:
        HELPER.main(["--config", str(config), "--generate-config", str(output)])
    assert error.value.code == 1
    assert "roadmap was renamed to reservations" in capsys.readouterr().err
    assert not output.exists()


def test_generation_round_trip_and_no_preparation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def unexpected(*args: Any, **kwargs: Any) -> None:
        pytest.fail("config generation must not run git or prepare-run")

    monkeypatch.setattr(HELPER.subprocess, "run", unexpected)
    blank = tmp_path / "default.toml"
    HELPER.main(["--generate-default-config", str(blank), "--issue", "99"])
    assert tomllib.loads(blank.read_text()) == HELPER.DEFAULTS
    target = tmp_path / "configured.toml"
    HELPER.main(
        [
            "--config",
            str(blank),
            "--generate-config",
            str(target),
            "--repository",
            "git@github.com:owner/repo.git",
            "--issue",
            "42",
            "--run-parent-dir",
            str(tmp_path / "with spaces"),
            "--run-dir",
            "run",
            "--account",
            'user"name',
            "--bob-model",
            "model\\with\ncharacters",
            "--no-auto-start",
        ]
    )
    values = tomllib.loads(target.read_text())
    assert values["issue"] == 42
    assert values["run_dir"] == str(tmp_path / "with spaces/run")
    assert values["account"] == 'user"name'
    assert values["bob_model"] == "model\\with\ncharacters"
    assert values["auto_start"] is False
    assert HELPER.settings(HELPER.parser().parse_args(["--config", str(target)])) == values
    before = target.read_bytes()
    with pytest.raises(SystemExit) as error:
        HELPER.main(["--generate-default-config", str(target)])
    assert error.value.code == 1
    assert target.read_bytes() == before
    assert not (tmp_path / "with spaces").exists()


def test_config_paths_and_cli_work_replacement(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    config = config_dir / "settings.toml"
    config.write_text('run_parent_dir = "../runs"\nrun_dir = "one"\nissue = 42\n')
    monkeypatch.chdir(tmp_path)
    args = HELPER.parser().parse_args(
        ["--config", str(config), "--work-file", "cli work.md", "--bob-harness", "codex"]
    )
    values = HELPER.settings(args)
    assert values["issue"] == 0
    assert values["work_file"] == str(tmp_path / "cli work.md")
    assert values["run_dir"] == str(tmp_path / "runs/one")
    assert values["bob_harness"] == "codex"
    config.write_text('work_file = "statement.md"\n')
    values = HELPER.settings(HELPER.parser().parse_args(["--config", str(config)]))
    assert values["work_file"] == str(config_dir / "statement.md")
    values = HELPER.settings(HELPER.parser().parse_args(["--config", str(config), "--issue", "43"]))
    assert values["work_file"] == "" and values["issue"] == 43


def test_doctor_config_cli_precedence_and_side_effect_free_resolution(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def unexpected(*args: Any, **kwargs: Any) -> None:
        pytest.fail("configuration resolution must not probe git, a provider or a hub")

    monkeypatch.setattr(HELPER.subprocess, "run", unexpected)
    config = tmp_path / "doctor.toml"
    config.write_text(
        'denise_settings_from = "charlie"\ndenise_harness = "claude"\n'
        'denise_model = "from config"\ndenise_effort = "config effort"\n'
    )
    output = tmp_path / "merged.toml"
    HELPER.main(
        [
            "--config",
            str(config),
            "--generate-config",
            str(output),
            "--denise-settings-from",
            "none",
            "--denise-harness",
            "codex",
            "--denise-model",
            'future "model"',
            "--denise-effort",
            "future effort",
        ]
    )
    values = tomllib.loads(output.read_text())
    assert values["denise_settings_from"] == "none"
    assert values["denise_harness"] == "codex"
    assert values["denise_model"] == 'future "model"'
    assert values["denise_effort"] == "future effort"
    # Selecting an unsupported source still serializes a valid team configuration.
    config.write_text('bob_harness = "devin"\ndenise_settings_from = "bob"\n')
    HELPER.main(["--config", str(config), "--generate-config", str(tmp_path / "devin.toml")])


@pytest.mark.parametrize(
    "content",
    [
        "isssue = 42",
        'issue = "42"',
        "issue = true",
        "auto_start = 1",
        "issue = -1",
        'forge = "other"',
        'bob_harness = "other"',
        'denise_settings_from = "worker"',
        'denise_settings_from = "none"',
        'denise_harness = "devin"',
        "denise_model = 42",
        "denise_effort = false",
        'issue = 42\nwork_file = "statement.md"',
        "not toml",
    ],
)
def test_bad_config_fails_before_mutation(tmp_path: Path, content: str) -> None:
    config = tmp_path / "invalid.toml"
    config.write_text(content)
    output = tmp_path / "output.toml"
    with pytest.raises(SystemExit) as error:
        HELPER.main(["--config", str(config), "--generate-config", str(output)])
    assert error.value.code == 1
    assert not output.exists()


@pytest.mark.parametrize("flags", [[], ["--issue", "42"], ["--work-file", "missing.md"]])
def test_incomplete_preparation_does_not_create_run(tmp_path: Path, flags: list[str]) -> None:
    run_dir = tmp_path / "run"
    with pytest.raises(SystemExit):
        HELPER.main(["--run-dir", str(run_dir), *flags])
    assert not run_dir.exists()


@pytest.mark.parametrize("work_file", [False, True])
def test_prepare_clones_and_forwards_arguments(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    work_file: bool,
) -> None:
    source = tmp_path / "source repo"
    subprocess.run(["git", "init", "-b", "main", str(source)], check=True, capture_output=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(source),
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
    run_dir = tmp_path / "run with spaces"
    statement = tmp_path / "work file.md"
    statement.write_text("Do the assigned work.")
    flags = ["--work-file", str(statement)] if work_file else ["--issue", "42"]
    calls: list[list[str]] = []
    real_run = subprocess.run

    def run(command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        calls.append(command)
        if command[0] == sys.executable:
            assert kwargs["cwd"] == ROOT
            return subprocess.CompletedProcess(command, 0)
        return real_run(command, **kwargs)

    monkeypatch.setattr(HELPER.subprocess, "run", run)
    prompted: list[str] = []
    monkeypatch.setattr("builtins.input", lambda message: prompted.append(message))
    args = [
        "--repository",
        str(source),
        "--run-dir",
        str(run_dir),
        "--account",
        "tester",
        "--reservations",
        "owner/repo#2",
        "--bob-harness",
        "codex",
        "--bob-model",
        "model with spaces",
        "--bob-effort",
        "high",
        "--denise-settings-from",
        "charlie",
        "--denise-harness",
        "claude",
        "--denise-model",
        "future doctor model",
        "--denise-effort",
        "future doctor effort",
        "--no-auto-start",
        *flags,
    ]
    HELPER.main(args)
    assert (run_dir / "hub/.git").is_dir()
    assert len(prompted) == 1
    command = calls[-1]
    assert command[:2] == [sys.executable, str(ROOT / "scripts/prepare-run.py")]
    assert command[command.index("--hub-repo") + 1] == str(run_dir / "hub")
    assert command[command.index("--bob-model") + 1] == "model with spaces"
    assert command[command.index(flags[0]) + 1] == flags[1]
    assert ("--issue" in command) != ("--work-file" in command)
    assert "--no-auto-start" in command
    assert command[command.index("--denise-settings-from") + 1] == "charlie"
    assert command[command.index("--denise-harness") + 1] == "claude"
    assert command[command.index("--denise-model") + 1] == "future doctor model"
    assert command[command.index("--denise-effort") + 1] == "future doctor effort"
    calls.clear()
    HELPER.main([*args, "--yes"])
    assert len(prompted) == 1
    assert not any("clone" in call for call in calls)
    calls.clear()
    with pytest.raises(SystemExit):
        HELPER.main([*args, "--repository", "different-repo", "--yes"])
    assert len(calls) == 1  # Only the origin read, no prepare or clone.


def test_prepare_failure_is_not_hidden(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def fail(command: list[str], **kwargs: Any) -> None:
        raise subprocess.CalledProcessError(7, command)

    monkeypatch.setattr(HELPER.subprocess, "run", fail)
    with pytest.raises(SystemExit) as error:
        HELPER.main(
            [
                "--repository",
                "repo",
                "--run-dir",
                str(tmp_path / "run"),
                "--account",
                "tester",
                "--issue",
                "42",
                "--yes",
            ]
        )
    assert error.value.code == 1


def test_windows_hub_command_quotes_paths(monkeypatch: pytest.MonkeyPatch) -> None:
    # Patch only this module's os reference; changing os.name globally breaks pathlib.
    from types import SimpleNamespace

    monkeypatch.setattr(HELPER, "os", SimpleNamespace(name="nt"))
    hub = Path("C:/run's directory/hub")
    command = HELPER.hub_start_command(hub, "github")
    quoted = str(hub).replace("'", "''")
    assert f"Set-Location -LiteralPath '{quoted}'" in command
    assert "--forge github" in command


@pytest.mark.skipif(
    shutil.which("pwsh") is None and shutil.which("powershell") is None,
    reason="requires PowerShell",
)
def test_powershell_wrapper_preserves_arguments_and_sync_failure(tmp_path: Path) -> None:
    powershell = shutil.which("pwsh") or shutil.which("powershell")
    assert powershell is not None
    log = tmp_path / "uv.jsonl"
    fake = tmp_path / "fake_uv.py"
    fake.write_text(
        "import json, os, sys\n"
        "with open(os.environ['UV_TEST_LOG'], 'a') as f:\n"
        "    f.write(json.dumps(sys.argv[1:]) + '\\n')\n"
        "sys.exit(int(os.environ.get('UV_TEST_EXIT', '0')))\n"
    )
    if os.name == "nt":
        (tmp_path / "uv.cmd").write_text(f'@"{sys.executable}" "{fake}" %*\n')
    else:
        executable = tmp_path / "uv"
        executable.write_text(f"#!{sys.executable}\n" + fake.read_text())
        executable.chmod(0o755)
    env = {
        **os.environ,
        "PATH": str(tmp_path) + os.pathsep + os.environ["PATH"],
        "UV_TEST_LOG": str(log),
    }
    command = [
        powershell,
        "-NoProfile",
        "-File",
        str(ROOT / "scripts/prep-standard-run.ps1"),
        "--work-file",
        "work with spaces.md",
        "--account",
        "user with spaces",
    ]
    subprocess.run(command, check=True, cwd=tmp_path, env=env)
    calls = [json.loads(line) for line in log.read_text().splitlines()]
    assert calls[0][:3] == ["sync", "--locked", "--all-packages"]
    assert calls[1][-4:] == ["--work-file", "work with spaces.md", "--account", "user with spaces"]
    log.write_text("")
    result = subprocess.run(command, cwd=tmp_path, env={**env, "UV_TEST_EXIT": "7"})
    assert result.returncode == 7
    assert len(log.read_text().splitlines()) == 1


@pytest.mark.skipif(os.name == "nt" or shutil.which("bash") is None, reason="requires Bash")
def test_bash_wrapper_preserves_arguments_and_stops_on_sync_failure(tmp_path: Path) -> None:
    fake_uv = tmp_path / "uv"
    log = tmp_path / "uv.jsonl"
    fake_uv.write_text(
        f"#!{sys.executable}\n"
        "import json, os, sys\n"
        "with open(os.environ['UV_TEST_LOG'], 'a') as f:\n"
        "    f.write(json.dumps(sys.argv[1:]) + '\\n')\n"
        "sys.exit(int(os.environ.get('UV_TEST_EXIT', '0')))\n"
    )
    fake_uv.chmod(0o755)
    env = {
        **os.environ,
        "PATH": str(tmp_path) + os.pathsep + os.environ["PATH"],
        "UV_TEST_LOG": str(log),
    }
    log.write_text("")
    subprocess.run(
        ["bash", str(ROOT / "scripts/prep-standard-run.sh"), "--account", "user with spaces"],
        check=True,
        cwd=tmp_path,
        env=env,
    )
    calls = [json.loads(line) for line in log.read_text().splitlines()]
    assert calls[0][:3] == ["sync", "--locked", "--all-packages"]
    assert calls[1][-2:] == ["--account", "user with spaces"]
    log.write_text("")
    result = subprocess.run(
        ["bash", str(ROOT / "scripts/prep-standard-run.sh")],
        cwd=tmp_path,
        env={**env, "UV_TEST_EXIT": "7"},
    )
    assert result.returncode == 7
    assert len(log.read_text().splitlines()) == 1


@pytest.mark.parametrize("cli_value", [None, 3])
def test_pytest_workers_config_and_flag_reach_prepare_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, cli_value: int | None
) -> None:
    config = tmp_path / "workers.toml"
    config.write_text("pytest_workers = 2\n", encoding="utf-8")
    arguments = [
        "--config",
        str(config),
        "--repository",
        "o/r",
        "--run-dir",
        str(tmp_path / "run"),
        "--account",
        "operator",
        "--issue",
        "264",
    ]
    if cli_value is not None:
        arguments += ["--pytest-workers", str(cli_value)]
    values = HELPER.settings(HELPER.parser().parse_args(arguments))
    calls: list[list[str]] = []

    def capture(command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        calls.append(command)
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(HELPER.subprocess, "run", capture)
    HELPER.prepare(values, yes=True)
    command = calls[-1]
    assert command[command.index("--pytest-workers") + 1] == str(cli_value or 2)


def test_negative_pytest_workers_config_rejected(tmp_path: Path) -> None:
    config = tmp_path / "invalid-workers.toml"
    config.write_text("pytest_workers = -1\n", encoding="utf-8")
    with pytest.raises(ValueError, match="pytest_workers"):
        HELPER.settings(HELPER.parser().parse_args(["--config", str(config)]))
