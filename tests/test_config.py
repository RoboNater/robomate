import socket
from pathlib import Path

import pytest
from agent_hub_common import (
    UNKNOWN,
    AgentProfile,
    ConfigurationError,
    HubSettings,
    ModelSource,
    config,
    profile_from_env,
)


def test_settings_have_local_defaults(tmp_path: Path) -> None:
    settings = HubSettings.from_env({"XDG_STATE_HOME": str(tmp_path)})

    assert settings.host == "127.0.0.1"
    assert settings.port == 8420
    assert settings.public_url == "http://127.0.0.1:8420"
    assert settings.state_dir == tmp_path / "agent-hub"
    assert settings.database_path == tmp_path / "agent-hub/hub.db"
    assert settings.token_file == tmp_path / "agent-hub/token"
    # Guides are checkout content, not state: the default is the repository's
    # own top-level guides/ directory (§6).
    assert settings.guides_dir == Path(__file__).resolve().parents[1] / "guides"
    assert settings.event_lease_s == 600.0


def test_state_paths_ignore_the_working_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env = {"XDG_STATE_HOME": "relative-state"}
    (tmp_path / "one").mkdir()
    (tmp_path / "two").mkdir()

    monkeypatch.chdir(tmp_path / "one")
    first = HubSettings.from_env(env)
    monkeypatch.chdir(tmp_path / "two")
    second = HubSettings.from_env(env)

    assert first.state_dir == second.state_dir
    assert first.database_path == second.database_path
    assert first.token_file == second.token_file
    assert first.guides_dir == second.guides_dir


def test_relative_xdg_state_home_falls_back_to_the_default() -> None:
    # The XDG base-directory specification declares a relative value invalid.
    settings = HubSettings.from_env({"XDG_STATE_HOME": "relative-state"})

    assert settings.state_dir == Path.home() / ".local/state/agent-hub"


def test_relative_state_dir_is_rejected() -> None:
    with pytest.raises(ConfigurationError, match="absolute"):
        HubSettings.from_env({"HUB_STATE_DIR": "state"})


def test_relative_guides_dir_is_rejected() -> None:
    with pytest.raises(ConfigurationError, match="absolute"):
        HubSettings.from_env({"HUB_GUIDES_DIR": "guides"})


def test_the_guides_directory_is_configurable(tmp_path: Path) -> None:
    settings = HubSettings.from_env({"HUB_GUIDES_DIR": str(tmp_path / "elsewhere/guides")})

    assert settings.guides_dir == tmp_path / "elsewhere/guides"


def test_relative_state_paths_resolve_against_the_state_directory(tmp_path: Path) -> None:
    settings = HubSettings.from_env(
        {
            "HUB_STATE_DIR": str(tmp_path / "state"),
            "HUB_DB_PATH": "state.sqlite",
            "HUB_TOKEN_FILE": "secrets/token",
        }
    )

    assert settings.database_path == tmp_path / "state/state.sqlite"
    assert settings.token_file == tmp_path / "state/secrets/token"


def test_default_state_dir_falls_back_to_the_home_directory() -> None:
    settings = HubSettings.from_env({})

    assert settings.state_dir == Path.home() / ".local/state/agent-hub"


@pytest.mark.parametrize("port", ["zero", "0", "65536"])
def test_settings_reject_invalid_ports(port: str) -> None:
    with pytest.raises(ConfigurationError):
        HubSettings.from_env({"HUB_PORT": port})


def test_settings_normalize_overrides(tmp_path: Path) -> None:
    settings = HubSettings.from_env(
        {
            "HUB_HOST": "0.0.0.0",
            "HUB_PORT": "9000",
            "HUB_PUBLIC_URL": "https://hub.example/",
            "HUB_STATE_DIR": str(tmp_path),
            "HUB_DB_PATH": "state.sqlite",
            "HUB_TOKEN": " secret ",
            "HUB_TOKEN_FILE": "unused-token",
        }
    )

    assert settings.host == "0.0.0.0"
    assert settings.port == 9000
    assert settings.public_url == "https://hub.example"
    assert settings.database_path == tmp_path / "state.sqlite"
    assert settings.token == "secret"


@pytest.mark.parametrize(
    "host",
    ["0.0.0.0", "::", "[::]", "0:0:0:0:0:0:0:0", "[0000:0000:0000:0000:0000:0000:0000:0000]", "*"],
)
def test_wildcard_bind_requires_an_explicit_public_url(host: str) -> None:
    with pytest.raises(ConfigurationError, match="HUB_PUBLIC_URL"):
        HubSettings.from_env({"HUB_HOST": host})


def test_wildcard_bind_accepts_an_explicit_public_url() -> None:
    settings = HubSettings.from_env(
        {"HUB_HOST": "0.0.0.0", "HUB_PUBLIC_URL": "http://alice-host:8420"}
    )

    assert settings.public_url == "http://alice-host:8420"


@pytest.mark.parametrize("host", ["::1", "[::1]", "0:0:0:0:0:0:0:1"])
def test_ipv6_bind_host_is_normalized_for_the_socket_and_the_url(host: str) -> None:
    settings = HubSettings.from_env({"HUB_HOST": host})

    # The resolver rejects the bracketed form, so brackets belong only in the URL.
    assert settings.host == "::1"
    assert settings.public_url == "http://[::1]:8420"
    assert socket.getaddrinfo(settings.host, settings.port, type=socket.SOCK_STREAM)


def test_hostnames_are_left_alone() -> None:
    settings = HubSettings.from_env({"HUB_HOST": "alice-host"})

    assert settings.host == "alice-host"
    assert settings.public_url == "http://alice-host:8420"


@pytest.mark.parametrize(
    ("name", "value"),
    [("HUB_PUBLIC_URL", "ftp://hub.example"), ("HUB_TOKEN", "   ")],
)
def test_settings_reject_invalid_overrides(name: str, value: str) -> None:
    with pytest.raises(ConfigurationError):
        HubSettings.from_env({name: value})


def test_wait_bounds_and_timer_liveness_have_independent_defaults() -> None:
    settings = HubSettings.from_env({})

    assert settings.default_wait_s == 120
    assert settings.max_wait_s == 300
    assert settings.lost_after_s == 180
    assert settings.sweep_interval_s == 10


def test_lowering_the_default_hold_does_not_change_timer_liveness() -> None:
    settings = HubSettings.from_env({"HUB_DEFAULT_WAIT_S": "20"})

    assert settings.max_wait_s == 50
    assert settings.lost_after_s == 180


def test_a_requested_wait_is_clamped_to_the_ceiling() -> None:
    settings = HubSettings.from_env({"HUB_DEFAULT_WAIT_S": "30", "HUB_MAX_WAIT_S": "45"})

    assert settings.bounded_wait(None) == 30
    assert settings.bounded_wait(10) == 10
    assert settings.bounded_wait(9000) == 45
    assert settings.bounded_wait(-5) == 0


@pytest.mark.parametrize(
    "env",
    [
        pytest.param({"HUB_DEFAULT_WAIT_S": "0"}, id="zero-wait"),
        pytest.param({"HUB_SWEEP_INTERVAL_S": "-1"}, id="negative-interval"),
        pytest.param({"HUB_MAX_WAIT_S": "soon"}, id="not-a-number"),
        pytest.param(
            {"HUB_DEFAULT_WAIT_S": "120", "HUB_MAX_WAIT_S": "60"}, id="ceiling-below-default"
        ),
        pytest.param({"HUB_LOST_AFTER_S": "0"}, id="zero-lost-after"),
    ],
)
def test_settings_reject_inconsistent_timings(env: dict[str, str]) -> None:
    with pytest.raises(ConfigurationError):
        HubSettings.from_env(env)


def test_guides_fall_back_to_the_state_directory_outside_a_checkout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Installed as a plain wheel there is no workspace above the package, and
    # the ancestor that would hold guides/ belongs to someone else.
    installed = tmp_path / "site-packages/a/b/c/agent_hub_common/config.py"
    installed.parent.mkdir(parents=True)
    monkeypatch.setattr(config, "__file__", str(installed))

    settings = HubSettings.from_env({"HUB_STATE_DIR": str(tmp_path / "state")})

    assert settings.guides_dir == tmp_path / "state/guides"


def test_profile_reads_the_launcher_environment() -> None:
    profile = profile_from_env(
        {
            "HUB_HARNESS": " claude-code ",
            "HUB_HARNESS_VERSION": "2.1.268",
            "HUB_PROVIDER": "anthropic",
            "HUB_MODEL": "claude-opus-5",
            "HUB_CAPABILITIES": "python,gh, python ,",
        }
    )

    assert profile == AgentProfile(
        harness="claude-code",
        harness_version="2.1.268",
        provider="anthropic",
        model="claude-opus-5",
        model_source=ModelSource.ENV,
        capabilities=("python", "gh"),
    )


@pytest.mark.parametrize(
    "env", [{}, {"HUB_HARNESS": " ", "HUB_MODEL": "", "HUB_CAPABILITIES": ","}]
)
def test_profile_fields_left_unset_are_unknown(env: dict[str, str]) -> None:
    profile = profile_from_env(env)

    assert profile == AgentProfile()
    assert (profile.harness, profile.harness_version, profile.provider, profile.model) == (
        UNKNOWN,
        UNKNOWN,
        UNKNOWN,
        UNKNOWN,
    )
    assert profile.model_source is ModelSource.UNKNOWN
    assert profile.capabilities == ()


def test_profile_accepts_the_step_4_runtime_name_for_the_harness() -> None:
    assert profile_from_env({"AGENT_RUNTIME": "codex"}).harness == "codex"
    assert profile_from_env({"AGENT_RUNTIME": "codex", "HUB_HARNESS": "gemini"}).harness == "gemini"


def test_a_model_named_unknown_has_no_source() -> None:
    assert profile_from_env({"HUB_MODEL": UNKNOWN}).model_source is ModelSource.UNKNOWN


def test_custom_event_lease_seconds(tmp_path: Path) -> None:
    settings = HubSettings.from_env({
        "XDG_STATE_HOME": str(tmp_path),
        "HUB_EVENT_LEASE_S": "300",
    })
    assert settings.event_lease_s == 300.0


def test_call_accounting_is_off_unless_enabled(tmp_path: Path) -> None:
    base = {"HUB_STATE_DIR": str(tmp_path)}
    assert HubSettings.from_env(base).call_accounting is False
    assert HubSettings.from_env(base).call_log_jsonl is None
    for value in ("1", "true", "ON", " yes "):
        assert HubSettings.from_env({**base, "HUB_CALL_ACCOUNTING": value}).call_accounting
    for value in ("0", "false", "off", "no", ""):
        assert not HubSettings.from_env({**base, "HUB_CALL_ACCOUNTING": value}).call_accounting

    jsonl = tmp_path / "calls.jsonl"
    enabled = HubSettings.from_env(
        {**base, "HUB_CALL_ACCOUNTING": "1", "HUB_CALL_LOG_JSONL": str(jsonl)}
    )
    assert enabled.call_log_jsonl == jsonl


@pytest.mark.parametrize(
    ("env", "message"),
    [
        ({"HUB_CALL_ACCOUNTING": "maybe"}, "HUB_CALL_ACCOUNTING must be"),
        (
            {"HUB_CALL_ACCOUNTING": "1", "HUB_CALL_LOG_JSONL": "calls.jsonl"},
            "HUB_CALL_LOG_JSONL must be an absolute path",
        ),
        ({"HUB_CALL_LOG_JSONL": "/abs/calls.jsonl"}, "requires HUB_CALL_ACCOUNTING=1"),
    ],
)
def test_call_accounting_settings_are_validated(
    tmp_path: Path, env: dict[str, str], message: str
) -> None:
    with pytest.raises(ConfigurationError, match=message):
        HubSettings.from_env({"HUB_STATE_DIR": str(tmp_path), **env})
