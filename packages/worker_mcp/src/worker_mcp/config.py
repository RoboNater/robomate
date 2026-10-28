"""Environment-backed configuration for worker MCP processes."""

from __future__ import annotations

import os
from collections.abc import Mapping
from contextlib import suppress
from dataclasses import dataclass, field, replace
from pathlib import Path

from agent_hub_common import (
    MAX_HOLD_DEFAULT_S,
    UNKNOWN,
    AgentProfile,
    ConfigurationError,
    ModelSource,
    profile_from_env,
)
from agent_hub_common.discovery import HubEndpoint
from agent_hub_common.workspace import canonical_workspace, read_identity

DEFAULT_WAIT_S = float(MAX_HOLD_DEFAULT_S)
DEFAULT_HEARTBEAT_S = 30.0
DEFAULT_MAX_RETRIES = 3
DEFAULT_BACKOFF_FACTOR_S = 0.5


def _positive_seconds(env: Mapping[str, str], name: str, default: float) -> float:
    raw = env.get(name)
    if raw is None:
        return default
    try:
        seconds = float(raw)
    except ValueError as exc:
        raise ConfigurationError(f"{name} must be a number of seconds") from exc
    if seconds <= 0:
        raise ConfigurationError(f"{name} must be greater than zero")
    return seconds


def _non_negative_int(env: Mapping[str, str], name: str, default: int) -> int:
    raw = env.get(name)
    if raw is None:
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise ConfigurationError(f"{name} must be an integer") from exc
    if value < 0:
        raise ConfigurationError(f"{name} must be zero or greater")
    return value


def _optional_absolute_path(env: Mapping[str, str], name: str) -> Path | None:
    raw = env.get(name, "").strip()
    if not raw:
        return None
    path = Path(raw).expanduser()
    if not path.is_absolute():
        raise ConfigurationError(f"{name} must be an absolute path")
    return path


@dataclass(frozen=True, slots=True)
class WorkerSettings:
    """Settings for the worker MCP client."""

    hub_url: str
    token: str
    agent_name: str
    # What the launcher says this worker is; reported at check-in (§4.3).
    profile: AgentProfile = field(default_factory=AgentProfile)
    default_wait_s: float = DEFAULT_WAIT_S
    heartbeat_s: float = DEFAULT_HEARTBEAT_S
    max_retries: int = DEFAULT_MAX_RETRIES
    backoff_factor_s: float = DEFAULT_BACKOFF_FACTOR_S
    telemetry_log: Path | None = None
    workspace: Path | None = None
    cwd: Path | None = None
    environ: Mapping[str, str] | None = field(default=None, compare=False)
    token_file: Path | None = None

    @classmethod
    def from_discovered(
        cls,
        endpoint: HubEndpoint,
        *,
        agent_name: str | None = None,
        harness: str | None = None,
        environ: Mapping[str, str] | None = None,
    ) -> WorkerSettings:
        """Use discovery for connection settings while retaining worker environment options."""

        base_env = os.environ if environ is None else environ
        env_had_harness = bool(
            base_env.get("HUB_HARNESS", "").strip() or base_env.get("AGENT_RUNTIME", "").strip()
        )
        env = dict(base_env)
        env["HUB_URL"] = endpoint.url
        env["HUB_TOKEN"] = endpoint.token
        if agent_name is not None:
            env["AGENT_NAME"] = agent_name
        if harness is not None and not env_had_harness:
            env["HUB_HARNESS"] = harness
        settings = replace(cls.from_env(env), token_file=endpoint.token_file)
        if settings.profile.harness != UNKNOWN:
            source = ModelSource.ENV if env_had_harness else ModelSource.INSTALL
            return replace(
                settings,
                profile=replace(settings.profile, harness_source=source),
                environ=base_env,
            )
        return replace(settings, environ=base_env)

    @classmethod
    def unjoined(
        cls,
        *,
        harness: str | None = None,
        environ: Mapping[str, str] | None = None,
        cwd: Path | None = None,
        profile: AgentProfile | None = None,
        default_wait_s: float | None = None,
    ) -> WorkerSettings:
        """Settings for a worker MCP bridge before `join` names the agent and role (§5)."""

        env = dict(os.environ if environ is None else environ)
        wait_s = (
            default_wait_s
            if default_wait_s is not None
            else _positive_seconds(env, "HUB_DEFAULT_WAIT_S", DEFAULT_WAIT_S)
        )
        heartbeat_s = _positive_seconds(env, "HUB_HEARTBEAT_S", DEFAULT_HEARTBEAT_S)
        max_retries = _non_negative_int(env, "HUB_MAX_RETRIES", DEFAULT_MAX_RETRIES)
        backoff_factor_s = _positive_seconds(env, "HUB_BACKOFF_FACTOR_S", DEFAULT_BACKOFF_FACTOR_S)
        telemetry_log = _optional_absolute_path(env, "HUB_TELEMETRY_LOG")
        resolved_profile = profile if profile is not None else profile_from_env(env)
        if profile is None:
            if resolved_profile.harness != UNKNOWN:
                resolved_profile = replace(resolved_profile, harness_source=ModelSource.ENV)
            elif harness is not None and harness.strip() and harness.strip() != UNKNOWN:
                resolved_profile = replace(
                    resolved_profile, harness=harness.strip(), harness_source=ModelSource.INSTALL
                )
        raw_hub_url = (env.get("HUB_URL") or env.get("ROBOMATE_HUB_URL") or "").strip().rstrip("/")
        raw_token = (env.get("HUB_TOKEN") or env.get("ROBOMATE_TOKEN") or "").strip()
        raw_token_file = env.get("ROBOMATE_TOKEN_FILE", "").strip()
        token_file = None
        if not raw_token and raw_token_file:
            token_file = Path(raw_token_file).expanduser().resolve()
            with suppress(OSError):
                raw_token = token_file.read_text(encoding="utf-8").strip()
        raw_agent_name = (env.get("AGENT_NAME") or env.get("ROBOMATE_AGENT_NAME") or "").strip()
        return cls(
            hub_url=raw_hub_url,
            token=raw_token,
            agent_name=raw_agent_name,
            profile=resolved_profile,
            default_wait_s=wait_s,
            heartbeat_s=heartbeat_s,
            max_retries=max_retries,
            backoff_factor_s=backoff_factor_s,
            telemetry_log=telemetry_log,
            cwd=cwd,
            environ=env,
            token_file=token_file,
        )

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None) -> WorkerSettings:
        env = os.environ if environ is None else environ

        raw_hub_url = env.get("HUB_URL")
        if not raw_hub_url or not raw_hub_url.strip():
            raise ConfigurationError("HUB_URL must be set")
        hub_url = raw_hub_url.strip().rstrip("/")
        if not hub_url.startswith(("http://", "https://")):
            raise ConfigurationError("HUB_URL must be an http(s) URL")

        raw_token = env.get("HUB_TOKEN")
        if not raw_token or not raw_token.strip():
            raise ConfigurationError("HUB_TOKEN must be set")
        token = raw_token.strip()

        raw_agent_name = env.get("AGENT_NAME")
        if not raw_agent_name or not raw_agent_name.strip():
            raise ConfigurationError("AGENT_NAME must be set")
        agent_name = raw_agent_name.strip()

        default_wait_s = _positive_seconds(env, "HUB_DEFAULT_WAIT_S", DEFAULT_WAIT_S)
        heartbeat_s = _positive_seconds(env, "HUB_HEARTBEAT_S", DEFAULT_HEARTBEAT_S)
        max_retries = _non_negative_int(env, "HUB_MAX_RETRIES", DEFAULT_MAX_RETRIES)
        backoff_factor_s = _positive_seconds(env, "HUB_BACKOFF_FACTOR_S", DEFAULT_BACKOFF_FACTOR_S)
        telemetry_log = _optional_absolute_path(env, "HUB_TELEMETRY_LOG")

        workspace = None
        profile = profile_from_env(env)
        if "HUB_WORKSPACE" in env:
            workspace = canonical_workspace(env["HUB_WORKSPACE"])
            identity = read_identity(workspace, agent_name)
            profile = replace(profile, workspace_id=identity["workspace_id"])

        return cls(
            hub_url=hub_url,
            token=token,
            agent_name=agent_name,
            profile=profile,
            workspace=workspace,
            default_wait_s=default_wait_s,
            heartbeat_s=heartbeat_s,
            max_retries=max_retries,
            backoff_factor_s=backoff_factor_s,
            telemetry_log=telemetry_log,
        )
