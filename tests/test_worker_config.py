import json
import tomllib
from pathlib import Path
from typing import Any

import pytest
from agent_hub_common import (
    AgentProfile,
    ConfigurationError,
    ModelSource,
)
from agent_hub_common.discovery import HubEndpoint
from worker_mcp.config import WorkerSettings
from worker_mcp.orchestrator import OrchestratorBridge, create_orchestrator_mcp

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
        harness_source=ModelSource.ENV,
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


def test_discovered_worker_keeps_profile_and_workspace_configuration(tmp_path: Path) -> None:
    settings = WorkerSettings.from_discovered(
        HubEndpoint("http://discovered.test/", "discovered-token"),
        agent_name="bob",
        harness="codex",
        environ={
            "HUB_URL": "http://old.test",
            "HUB_TOKEN": "old-token",
            "HUB_MODEL": "configured-model",
            "HUB_HEARTBEAT_S": "7",
            "HUB_MAX_RETRIES": "2",
        },
    )
    assert (settings.hub_url, settings.token, settings.agent_name) == (
        "http://discovered.test",
        "discovered-token",
        "bob",
    )
    assert settings.profile.harness == "codex"
    assert settings.profile.harness_source is ModelSource.INSTALL
    assert settings.profile.model == "configured-model"
    assert (settings.heartbeat_s, settings.max_retries) == (7, 2)
    # Discovery does not bypass the legacy full-clone identity check.
    with pytest.raises(ConfigurationError, match="HUB_WORKSPACE"):
        WorkerSettings.from_discovered(
            HubEndpoint("http://discovered.test", "token"),
            agent_name="bob",
            environ={"HUB_WORKSPACE": str(tmp_path)},
        )


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
    codex = tomllib.loads((ROOT / "runtimes" / "codex.config.toml").read_text(encoding="utf-8"))
    hub = codex["mcp_servers"]["robomate"]
    expected_tools = {
        "check_in",
        "get_role_guide",
        "await_assignment",
        "report_progress",
        "ask_alice",
        "submit_result",
        "submit_closeout_report",
        "get_operator_answer",
        "get_statement",
    }
    assert set(hub["tools"]) == expected_tools
    assert {tool["approval_mode"] for tool in hub["tools"].values()} == {"approve"}
    assert "HUB_TELEMETRY_LOG" in hub["env"]

    claude = json.loads((ROOT / "runtimes" / "claude-code.mcp.json").read_text(encoding="utf-8"))
    assert "HUB_TELEMETRY_LOG" in claude["mcpServers"]["robomate"]["env"]


@pytest.mark.parametrize(
    ("harness", "mode", "hold"),
    [
        ("claude-code", "print", 1500),
        ("claude-code", "interactive", 100),
        ("claude-code", "unknown", 100),
        ("codex", "headless", 1800),
        ("opencode", "headless", 1800),
        ("antigravity", "headless", 1800),
        ("devin", "headless", 100),
        ("unknown", "unknown", 100),
    ],
)
def test_measured_wait_profiles_and_precedence(harness: str, mode: str, hold: int) -> None:
    from agent_hub_common.waits import default_wait_s, profile_wait_s

    env = {"HUB_HARNESS": harness, "HUB_HARNESS_MODE": mode, "HUB_MCP_TIMEOUT_S": "1900"}
    assert profile_wait_s(harness, mode) == hold
    assert WorkerSettings.unjoined(environ=env).default_wait_s == hold
    assert (
        WorkerSettings.unjoined(
            harness=harness, environ={"HUB_HARNESS_MODE": mode, "HUB_MCP_TIMEOUT_S": "1900"}
        ).default_wait_s
        == hold
    )
    assert default_wait_s(harness, env | {"HUB_DEFAULT_WAIT_S": "45"}) == 45
    assert default_wait_s(harness, env | {"HUB_MCP_TIMEOUT_S": "330"}) == 100
    assert default_wait_s(harness, {}) == 100
    if harness == "claude-code":
        assert hold < (1800 if mode == "print" else 120)
    else:
        assert hold + 15 < 1900


@pytest.mark.parametrize("raw", ["nan", "inf", "1801", "0"])
def test_operator_wait_override_must_be_bounded(raw: str) -> None:
    with pytest.raises(ConfigurationError, match="HUB_DEFAULT_WAIT_S"):
        WorkerSettings.unjoined(environ={"HUB_DEFAULT_WAIT_S": raw})


async def test_orchestrator_timeout_schema_accepts_profiles_and_omits_default() -> None:
    tools = await create_orchestrator_mcp(OrchestratorBridge()).list_tools()
    schema: dict[str, Any] = next(t for t in tools if t.name == "wait_for_event").inputSchema
    assert schema["properties"]["timeout_s"]["default"] is None
    assert any(s.get("maximum") == 1800 for s in schema["properties"]["timeout_s"]["anyOf"])
    for rel in ("guides/worker.md", "prompts/worker.md", "guides/orchestrator.md"):
        text = (ROOT / rel).read_text(encoding="utf-8")
        assert "wait_profile.default_wait_s" in text
        assert "1500 s" in text and "1800 s" in text


def test_unjoined_remote_bridge_uses_explicit_url_and_token_file(tmp_path: Path) -> None:
    token = tmp_path / "remote-token"
    token.write_text("remote-secret\n", encoding="utf-8")
    settings = WorkerSettings.unjoined(
        cwd=tmp_path,
        environ={"ROBOMATE_HUB_URL": "http://remote:8420/", "ROBOMATE_TOKEN_FILE": str(token)},
    )
    assert settings.hub_url == "http://remote:8420"
    assert settings.token == "remote-secret"
    assert settings.token_file == token


def test_orchestrator_install_hint_and_environment_precedence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("HUB_HARNESS", raising=False)
    monkeypatch.delenv("AGENT_RUNTIME", raising=False)
    monkeypatch.delenv("HUB_DEFAULT_WAIT_S", raising=False)
    monkeypatch.setenv("HUB_MCP_TIMEOUT_S", "1900")
    assert OrchestratorBridge(harness="codex").default_wait_s == 1800
    monkeypatch.setenv("HUB_HARNESS", "claude-code")
    assert OrchestratorBridge(harness="codex").default_wait_s == 100
    monkeypatch.setenv("HUB_HARNESS_MODE", "print")
    assert OrchestratorBridge(harness="codex").default_wait_s == 1500
    monkeypatch.setenv("HUB_DEFAULT_WAIT_S", "45")
    assert OrchestratorBridge(harness="codex").default_wait_s == 45
