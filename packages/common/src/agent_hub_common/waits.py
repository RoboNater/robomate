"""Measured idle hold profiles and the bridge's single timeout resolver (#559)."""

import math
from collections.abc import Mapping

from .config import ConfigurationError

MAX_WAIT_S = 1800.0
HTTP_MARGIN_S = 15.0
CLIENT_TIMEOUT_S = 1900
FALLBACK_WAIT_S = 100.0


def profile_wait_s(harness: str, mode: str = "unknown") -> float:
    """Unknown modes and clients retain the measured safe short hold."""
    if harness == "claude-code":
        return 1500.0 if mode == "print" else FALLBACK_WAIT_S
    if harness in {"codex", "opencode", "antigravity"}:
        return MAX_WAIT_S
    return FALLBACK_WAIT_S


def default_wait_s(harness: str, env: Mapping[str, str]) -> float:
    """Explicit operator default, then profile if the client advertises its budget.

    Old static entries have no budget advertisement and keep their 100 s holds.
    The hub clamps every request separately to HUB_MAX_WAIT_S. Explicit tool
    timeouts override this default (and remain subject to the same hub clamp).
    """
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
        return value
    wait = profile_wait_s(harness, env.get("HUB_HARNESS_MODE", "unknown"))
    try:
        budget = float(env.get("HUB_MCP_TIMEOUT_S", "330"))
    except ValueError as exc:
        raise ConfigurationError("HUB_MCP_TIMEOUT_S must be a number of seconds") from exc
    return wait if math.isfinite(budget) and budget >= wait + HTTP_MARGIN_S else FALLBACK_WAIT_S
