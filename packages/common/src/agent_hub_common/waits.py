"""Version-controlled harness profiles and hold negotiation for the bridge and hub."""

import math
import re
from collections.abc import Mapping
from dataclasses import dataclass, replace
from typing import Any

from .config import ConfigurationError
from .constants import MAX_HOLD_DEFAULT_S
from .models import ModelSource

MAX_WAIT_S = 1800.0
WAIT_ADVERTISEMENT_HEADER = "X-Robomate-Wait-Advertisement"
HTTP_MARGIN_S = 15.0
CLIENT_TIMEOUT_S = 1900
FALLBACK_WAIT_S = float(MAX_HOLD_DEFAULT_S)


@dataclass(frozen=True, slots=True)
class ClientTimeout:
    """Setting within the per-server entry, with timeout units per second."""

    field: str
    units_per_second: int
    default: int | None = None


@dataclass(frozen=True, slots=True)
class HarnessProfile:
    supported_client_range: str
    max_hold_s: float
    user_config: str
    project_config: str | None
    timeout: ClientTimeout | None
    approval_controls: str
    bridge_grants: str
    agent_grants: str
    continuation: str
    limits: str


# Supported setup ranges include provisional settings. Certification requires
# separate evidence for an exact client version, mode, settings and grants.
_CLAUDE_INTERACTIVE = HarnessProfile(
    supported_client_range="2.x (measured on 2.1.325)",
    max_hold_s=FALLBACK_WAIT_S,
    user_config="~/.claude.json (respect CLAUDE_CONFIG_DIR)",
    project_config=".mcp.json",
    timeout=ClientTimeout("timeout", 1000),
    approval_controls="Named project-server approval and tool permissions",
    bridge_grants="Read its token and identity; connect to the hub",
    agent_grants="Sandbox denyRead where available; bridge runs outside that sandbox",
    continuation="Resume by session ID",
    limits="Calls background at 120 s with default settings",
)
_CODEX = HarnessProfile(
    supported_client_range="0.x (measured on 0.169.0)",
    max_hold_s=MAX_WAIT_S,
    user_config="${CODEX_HOME:-~/.codex}/config.toml",
    project_config="Trusted .codex/config.toml",
    timeout=ClientTimeout("tool_timeout_sec", 1, 300),
    approval_controls="Per-tool approval_mode for the server",
    bridge_grants="Read its token and identity; connect to the hub",
    agent_grants="Workspace-write and scoped tool approvals; path-specific read deny unverified",
    continuation="Resume by session ID",
    limits="Long calls on the execution tool path need the foreground polling instruction",
)
_OPENCODE = HarnessProfile(
    supported_client_range="1.x (measured on 1.19.5)",
    max_hold_s=MAX_WAIT_S,
    user_config="opencode.json[c] in the OpenCode XDG config directory",
    project_config="opencode.json[c]",
    timeout=ClientTimeout("timeout", 1000),
    approval_controls="Named MCP tool permissions",
    bridge_grants="Read its token and identity; connect to the hub",
    agent_grants=(
        "Headless token-directory denies via read and external_directory tool permissions; "
        "shell checks are best effort"
    ),
    continuation="Resume by session ID",
    limits="2.x config schema and timeout settings are tracked separately",
)
_ANTIGRAVITY = HarnessProfile(
    supported_client_range="1.x (measured on 1.5.9)",
    max_hold_s=MAX_WAIT_S,
    user_config="~/.gemini/config/mcp_config.json",
    project_config=".agents/mcp_config.json where supported",
    timeout=ClientTimeout("timeoutSeconds", 1, 180),
    approval_controls="Named server and tool approvals; enabledTools where required",
    bridge_grants="Read its token and identity; connect to the hub",
    agent_grants="Workspace trust and scoped tool approvals; no per-launch token-directory deny",
    continuation="Resume needs the recorded exact conversation ID",
    limits="Native Windows SSH support is unverified",
)
_DEVIN_CLI = HarnessProfile(
    supported_client_range="Provisional; measured range pending",
    max_hold_s=FALLBACK_WAIT_S,
    user_config="~/.config/devin/mcp_config.json or %APPDATA%/devin/mcp_config.json",
    project_config=".devin/mcp_config.json",
    timeout=None,
    approval_controls="Named-tool permission and workspace trust in the selected mode",
    bridge_grants="Read its token and identity; connect to the hub",
    agent_grants="No per-launch token-directory deny in the dangerous launcher mode",
    continuation="Resume needs the recorded exact conversation ID",
    limits="Long holds are unmeasured",
)
HARNESS_PROFILES: Mapping[tuple[str, str], HarnessProfile] = {
    ("claude-code", "interactive"): _CLAUDE_INTERACTIVE,
    ("claude-code", "print"): replace(
        _CLAUDE_INTERACTIVE,
        max_hold_s=1500.0,
        continuation="Print-mode supervisor and session resume",
        limits="Observed silent idle abort at 1800 s",
    ),
    ("codex", "interactive"): _CODEX,
    ("codex", "headless"): _CODEX,
    ("opencode", "interactive"): _OPENCODE,
    ("opencode", "headless"): _OPENCODE,
    ("antigravity", "interactive"): _ANTIGRAVITY,
    ("antigravity", "headless"): _ANTIGRAVITY,
    ("devin", "cli"): _DEVIN_CLI,
    ("devin", "local"): replace(
        _DEVIN_CLI,
        supported_client_range="Attempt planned; measured range pending",
        project_config=None,
        agent_grants="Effective sandbox and read boundary need verification",
        continuation="Unverified",
        limits="User entry only for the Desktop attempt",
    ),
    ("cline", "ide"): HarnessProfile(
        supported_client_range="Attempt planned; measured range pending",
        max_hold_s=FALLBACK_WAIT_S,
        user_config="cline_mcp_settings.json on the extension host",
        project_config=None,
        timeout=None,
        approval_controls="Per-tool autoApprove where supported",
        bridge_grants="Read its token and identity; connect to the hub",
        agent_grants="Effective sandbox and read boundary need verification",
        continuation="Unverified",
        limits="Per-server timeout depends on the build; project loader and long holds unverified",
    ),
}
_DEFAULT_MODES = {
    "claude-code": "interactive",
    "codex": "interactive",
    "opencode": "interactive",
    "antigravity": "interactive",
    "devin": "cli",
    "cline": "ide",
}


def _budget(value: Any) -> float:
    if (
        isinstance(value, bool)
        or not isinstance(value, int | float)
        or not (value > HTTP_MARGIN_S and value < math.inf)
    ):
        raise ValueError(f"client_budget_s must be finite and greater than {HTTP_MARGIN_S:g}")
    return float(value)


@dataclass(frozen=True, slots=True)
class WaitAdvertisement:
    mode: str
    client_budget_s: float

    @classmethod
    def from_wire(cls, value: Any) -> "WaitAdvertisement":
        if not isinstance(value, dict) or set(value) != {"mode", "client_budget_s"}:
            raise ValueError("wait_advertisement requires mode and client_budget_s")
        mode = value["mode"]
        if not isinstance(mode, str) or re.fullmatch(r"[a-z][a-z0-9-]{0,31}", mode) is None:
            raise ValueError("wait_advertisement mode must be a lowercase identifier")
        return cls(mode, _budget(value["client_budget_s"]))


@dataclass(frozen=True, slots=True)
class EffectiveWaitProfile:
    harness: str
    mode: str
    max_hold_s: float
    client_budget_s: float | None = None


def negotiate_wait(
    harness: str,
    harness_source: ModelSource,
    advertisement: WaitAdvertisement | None,
    hub_ceiling_s: float = MAX_WAIT_S,
) -> EffectiveWaitProfile:
    if advertisement is None:
        return EffectiveWaitProfile(harness, "fallback", min(FALLBACK_WAIT_S, hub_ceiling_s))
    mode = advertisement.mode
    profile = harness_profile(harness, mode)
    if profile is None or harness_source not in {ModelSource.ENV, ModelSource.INSTALL}:
        mode = "fallback"
    maximum = profile.max_hold_s if profile is not None and mode != "fallback" else FALLBACK_WAIT_S
    return EffectiveWaitProfile(
        harness,
        mode,
        min(maximum, advertisement.client_budget_s - HTTP_MARGIN_S, hub_ceiling_s),
        advertisement.client_budget_s,
    )


def wait_advertisement(harness: str, env: Mapping[str, str]) -> WaitAdvertisement | None:
    raw = env.get("HUB_MCP_TIMEOUT_S")
    if raw is None:
        return None
    mode = env.get("HUB_HARNESS_MODE", _DEFAULT_MODES.get(harness, "unknown"))
    try:
        return WaitAdvertisement.from_wire({"mode": mode, "client_budget_s": float(raw)})
    except ValueError as exc:
        raise ConfigurationError(f"HUB_MCP_TIMEOUT_S / HUB_HARNESS_MODE: {exc}") from exc


def harness_profile(harness: str, mode: str | None = None) -> HarnessProfile | None:
    selected_mode = _DEFAULT_MODES.get(harness, "") if mode is None else mode
    return HARNESS_PROFILES.get((harness, selected_mode))


def profile_wait_s(harness: str, mode: str | None = None) -> float:
    """Unknown modes and clients retain the measured safe short hold."""
    profile = harness_profile(harness, mode)
    return profile.max_hold_s if profile is not None else FALLBACK_WAIT_S


def bridge_wait_settings(
    harness: str, env: Mapping[str, str]
) -> tuple[WaitAdvertisement | None, float]:
    """Preserve the conservative bridge default when its budget cannot fit a full profile."""
    ad = wait_advertisement(harness, env)
    limit = negotiate_wait(harness, ModelSource.INSTALL, ad).max_hold_s
    default = min(FALLBACK_WAIT_S, limit)
    if ad is not None and limit == profile_wait_s(harness, ad.mode):
        default = limit
    raw = env.get("HUB_DEFAULT_WAIT_S")
    if raw is not None:
        try:
            value = float(raw)
        except ValueError as exc:
            raise ConfigurationError("HUB_DEFAULT_WAIT_S must be a number of seconds") from exc
        if not math.isfinite(value) or not 0 < value <= MAX_WAIT_S:
            raise ConfigurationError(
                f"HUB_DEFAULT_WAIT_S must be greater than zero and at most {MAX_WAIT_S:g}"
            )
        default = min(value, default)
    return ad, default


def default_wait_s(harness: str, env: Mapping[str, str]) -> float:
    return bridge_wait_settings(harness, env)[1]
