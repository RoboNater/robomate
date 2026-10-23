"""Shared `setup` and agent-pin loader for joins (M2 Step 7b, #314).

Both the worker A2A join (`protocol.py`) and the orchestrator RPC join
(`rpc.py`) return `setup` and apply per-agent model/effort pins from the
configuration layers (Step 5). One helper keeps the two paths in step.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from agent_hub_common import load_config_layers


def load_join_config(
    *,
    state_dir: Path | None,
    git_common_dir: Path | str | None,
    agent_name: str,
) -> tuple[str | None, str | None, str | None]:
    """Return (setup, pin_model, pin_effort) for `agent_name`.

    Raises `ConfigLayerError` naming the file and key when a layer file is
    invalid, so callers can refuse the join with that message.
    """

    common: Path | None = None
    if git_common_dir is not None:
        common = (
            Path(str(git_common_dir)) if not isinstance(git_common_dir, Path) else git_common_dir
        )
    hub_config: Path | None = (state_dir / "config.toml") if state_dir is not None else None
    layers = load_config_layers(hub_config=hub_config, git_common_dir=common)
    pinned = layers.agent_config(agent_name)
    return layers.setup, pinned.model, pinned.effort


def join_config_from_manager(
    worktree_manager: Any | None,
    state_dir: Path | None,
    agent_name: str,
    *,
    fallback_common_dir: Path | str | None = None,
) -> tuple[str | None, str | None, str | None]:
    """Load join config using a worktree manager's common dir when available."""

    common: Path | str | None = fallback_common_dir
    if worktree_manager is not None:
        common = worktree_manager.common
    return load_join_config(state_dir=state_dir, git_common_dir=common, agent_name=agent_name)
