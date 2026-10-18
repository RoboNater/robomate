import json
import re
import tomllib
from pathlib import Path
from typing import Any

import pytest
import worker_mcp.config as worker_config_mod
from agent_hub_common import (
    MAX_HOLD_DEFAULT_S,
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
        "get_operator_answer",
        "get_statement",
    }
    assert set(hub["tools"]) == expected_tools
    assert {tool["approval_mode"] for tool in hub["tools"].values()} == {"approve"}
    assert "HUB_TELEMETRY_LOG" in hub["env"]

    claude = json.loads((ROOT / "runtimes" / "claude-code.mcp.json").read_text(encoding="utf-8"))
    assert "HUB_TELEMETRY_LOG" in claude["mcpServers"]["robomate"]["env"]


def _check_hold_defaults(
    *,
    worker_default_wait_s: float,
    wait_for_event_default_s: float,
    guide_texts: dict[str, str],
) -> None:
    assert MAX_HOLD_DEFAULT_S == 100
    assert worker_default_wait_s <= MAX_HOLD_DEFAULT_S, (
        f"worker_mcp.config.DEFAULT_WAIT_S={worker_default_wait_s} > {MAX_HOLD_DEFAULT_S}"
    )
    assert wait_for_event_default_s <= MAX_HOLD_DEFAULT_S, (
        f"orchestrator wait_for_event timeout_s={wait_for_event_default_s} > {MAX_HOLD_DEFAULT_S}"
    )
    for rel_path, text in guide_texts.items():
        literals = [
            float(m.group(1)) for m in re.finditer(r"timeout_s\s*=\s*(\d+(?:\.\d+)?)", text)
        ]
        assert literals, f"{rel_path} has no timeout_s= literals"
        for value in literals:
            assert value <= MAX_HOLD_DEFAULT_S, (
                f"{rel_path} has timeout_s={value:g} > {MAX_HOLD_DEFAULT_S}"
            )


async def test_max_hold_default_s_bounds_worker_orchestrator_and_guide_timeouts() -> None:
    """#12: MCP hold defaults stay at or below 100 s across code, schema, and guides."""
    tools = await create_orchestrator_mcp(OrchestratorBridge()).list_tools()
    wait_tool = next(t for t in tools if t.name == "wait_for_event")
    schema: dict[str, Any] = wait_tool.inputSchema
    wait_default = float(schema["properties"]["timeout_s"]["default"])
    guide_texts = {
        rel: (ROOT / rel).read_text(encoding="utf-8")
        for rel in ("guides/worker.md", "prompts/worker.md", "guides/orchestrator.md")
    }

    _check_hold_defaults(
        worker_default_wait_s=worker_config_mod.DEFAULT_WAIT_S,
        wait_for_event_default_s=wait_default,
        guide_texts=guide_texts,
    )

    with pytest.raises(AssertionError, match=r"worker_mcp\.config\.DEFAULT_WAIT_S"):
        _check_hold_defaults(
            worker_default_wait_s=120.0,
            wait_for_event_default_s=wait_default,
            guide_texts=guide_texts,
        )
    with pytest.raises(AssertionError, match="orchestrator wait_for_event"):
        _check_hold_defaults(
            worker_default_wait_s=worker_config_mod.DEFAULT_WAIT_S,
            wait_for_event_default_s=120.0,
            guide_texts=guide_texts,
        )
    for rel in guide_texts:
        tampered = {**guide_texts, rel: guide_texts[rel] + "\nwait_for_event(timeout_s=100.5)\n"}
        with pytest.raises(AssertionError, match=re.escape(rel)):
            _check_hold_defaults(
                worker_default_wait_s=worker_config_mod.DEFAULT_WAIT_S,
                wait_for_event_default_s=wait_default,
                guide_texts=tampered,
            )
