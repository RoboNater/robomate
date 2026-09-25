import json
import tomllib
from pathlib import Path

import pytest
from agent_hub_common import AgentProfile, ConfigurationError, ModelSource
from worker_mcp.config import WorkerSettings

ROOT = Path(__file__).resolve().parents[1]


def test_worker_settings_parses_valid_env() -> None:
    env = {
        "HUB_URL": "http://127.0.0.1:8420/",
        "HUB_TOKEN": "secret-token",
        "AGENT_NAME": "bob",
    }
    settings = WorkerSettings.from_env(env)
    assert settings.hub_url == "http://127.0.0.1:8420"
    assert settings.token == "secret-token"
    assert settings.agent_name == "bob"
    # Nothing the launcher left unset is guessed, not even the harness.
    assert settings.profile == AgentProfile()
    assert settings.default_wait_s == 100.0
    assert settings.heartbeat_s == 30.0
    assert settings.max_retries == 3
    assert settings.backoff_factor_s == 0.5


def test_worker_settings_custom_overrides(tmp_path: Path) -> None:
    telemetry_path = tmp_path / "worker-telemetry.jsonl"
    env = {
        "HUB_URL": "https://hub.example.com",
        "HUB_TOKEN": "token-123",
        "AGENT_NAME": "charlie",
        "HUB_HARNESS": "codex",
        "HUB_HARNESS_VERSION": "0.154.0",
        "HUB_PROVIDER": "openai",
        "HUB_MODEL": "example-codex-model",
        "HUB_CAPABILITIES": "python, gh,,python",
        "HUB_DEFAULT_WAIT_S": "45.5",
        "HUB_HEARTBEAT_S": "12.5",
        "HUB_MAX_RETRIES": "5",
        "HUB_BACKOFF_FACTOR_S": "1.5",
        "HUB_TELEMETRY_LOG": str(telemetry_path),
    }
    settings = WorkerSettings.from_env(env)
    assert settings.hub_url == "https://hub.example.com"
    assert settings.agent_name == "charlie"
    assert settings.profile == AgentProfile(
        harness="codex",
        harness_version="0.154.0",
        provider="openai",
        model="example-codex-model",
        model_source=ModelSource.ENV,
        capabilities=("python", "gh"),
    )
    assert settings.default_wait_s == 45.5
    assert settings.heartbeat_s == 12.5
    assert settings.max_retries == 5
    assert settings.backoff_factor_s == 1.5
    assert settings.telemetry_log == telemetry_path


@pytest.mark.parametrize(
    ("env", "match"),
    [
        ({}, "HUB_URL must be set"),
        ({"HUB_URL": ""}, "HUB_URL must be set"),
        ({"HUB_URL": "ftp://hub"}, r"HUB_URL must be an http\(s\) URL"),
        ({"HUB_URL": "http://hub"}, "HUB_TOKEN must be set"),
        ({"HUB_URL": "http://hub", "HUB_TOKEN": ""}, "HUB_TOKEN must be set"),
        ({"HUB_URL": "http://hub", "HUB_TOKEN": "tok"}, "AGENT_NAME must be set"),
        ({"HUB_URL": "http://hub", "HUB_TOKEN": "tok", "AGENT_NAME": ""}, "AGENT_NAME must be set"),
        (
            {
                "HUB_URL": "http://hub",
                "HUB_TOKEN": "tok",
                "AGENT_NAME": "bob",
                "HUB_DEFAULT_WAIT_S": "-1",
            },
            "must be greater than zero",
        ),
        (
            {
                "HUB_URL": "http://hub",
                "HUB_TOKEN": "tok",
                "AGENT_NAME": "bob",
                "HUB_DEFAULT_WAIT_S": "abc",
            },
            "must be a number of seconds",
        ),
        (
            {
                "HUB_URL": "http://hub",
                "HUB_TOKEN": "tok",
                "AGENT_NAME": "bob",
                "HUB_HEARTBEAT_S": "0",
            },
            "must be greater than zero",
        ),
        (
            {
                "HUB_URL": "http://hub",
                "HUB_TOKEN": "tok",
                "AGENT_NAME": "bob",
                "HUB_MAX_RETRIES": "-2",
            },
            "must be zero or greater",
        ),
        (
            {
                "HUB_URL": "http://hub",
                "HUB_TOKEN": "tok",
                "AGENT_NAME": "bob",
                "HUB_MAX_RETRIES": "xyz",
            },
            "must be an integer",
        ),
        (
            {
                "HUB_URL": "http://hub",
                "HUB_TOKEN": "tok",
                "AGENT_NAME": "bob",
                "HUB_TELEMETRY_LOG": "relative.jsonl",
            },
            "must be an absolute path",
        ),
    ],
)
def test_worker_settings_rejects_invalid_env(env: dict[str, str], match: str) -> None:
    with pytest.raises(ConfigurationError, match=match):
        WorkerSettings.from_env(env)


def test_runtime_templates_configure_endurance_and_codex_tool_approvals() -> None:
    codex = tomllib.loads(
        (ROOT / "runtimes" / "codex.config.toml").read_text(encoding="utf-8")
    )
    hub = codex["mcp_servers"]["hub"]
    expected_tools = {
        "check_in",
        "get_role_guide",
        "await_assignment",
        "report_progress",
        "ask_alice",
        "submit_result",
    }
    assert set(hub["tools"]) == expected_tools
    assert {tool["approval_mode"] for tool in hub["tools"].values()} == {"approve"}
    assert "HUB_TELEMETRY_LOG" in hub["env"]

    claude = json.loads(
        (ROOT / "runtimes" / "claude-code.mcp.json").read_text(encoding="utf-8")
    )
    assert "HUB_TELEMETRY_LOG" in claude["mcpServers"]["hub"]["env"]
