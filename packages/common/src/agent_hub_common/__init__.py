"""Shared building blocks for the hub and worker MCP processes."""

from importlib import import_module
from typing import TYPE_CHECKING, Any

from .clock import iso_after, to_iso, utcnow, utcnow_iso
from .config import DEFAULT_EVENT_LEASE_S, ConfigurationError, HubSettings, profile_from_env
from .constants import (
    AGENT_HEADER,
    MAX_HOLD_DEFAULT_S,
    MAX_MESSAGE_PART_BYTES,
    MAX_STATEMENT_BYTES,
    MAX_TYPED_RESULT_BYTES,
    SHA_HEX_40_RE,
    UNKNOWN,
    MetaKeys,
)
from .discovery import validate_agent_name
from .stdio import reserve_stdout
from .token import TokenError, load_or_create_token, read_token_file, token_matches

if TYPE_CHECKING:
    from .config_layers import (
        AgentConfig,
        ConfigLayerError,
        ForgeConfig,
        ResolvedConfig,
        default_worktree_root,
        load_config_layers,
        repository_config_path,
        repository_name,
        user_config_path,
    )
    from .models import (
        CLOSEOUT_WAIT_DEFAULT_S,
        CLOSEOUT_WAIT_MAX_S,
        IMPLEMENTER_RESULT_SCHEMA,
        REBASE_RESULT_SCHEMA,
        REVIEWER_RESULT_SCHEMA,
        SCHEMA_VERSION,
        WORKER_STANDING_ROLES,
        AgentCloseoutReport,
        AgentProfile,
        AgentStatus,
        CloseoutContext,
        CloseoutResult,
        EventKind,
        EventState,
        Finding,
        ImplementerOutcome,
        ImplementerResult,
        ModelSource,
        RebaseResult,
        ReviewerResult,
        ReviewerVerdict,
        RolePolicy,
        StandingRole,
        TaskResult,
        TaskRole,
        TaskState,
        TestResult,
        WorkflowPolicy,
        WorkflowStatus,
        standing_role_accepts_task,
    )

# #647: `.models` and `.config_layers` import pydantic, which dominates the
# non-serving commands' import cost. Every name below still resolves through
# `from agent_hub_common import X`, but only when first requested, so
# `import robomate.cli` leaves pydantic out. `up` and `mcp` load it on use.
_LAZY_MODULES: dict[str, str] = {
    "CLOSEOUT_WAIT_DEFAULT_S": ".models",
    "CLOSEOUT_WAIT_MAX_S": ".models",
    "IMPLEMENTER_RESULT_SCHEMA": ".models",
    "REBASE_RESULT_SCHEMA": ".models",
    "REVIEWER_RESULT_SCHEMA": ".models",
    "SCHEMA_VERSION": ".models",
    "WORKER_STANDING_ROLES": ".models",
    "AgentCloseoutReport": ".models",
    "AgentProfile": ".models",
    "AgentStatus": ".models",
    "CloseoutContext": ".models",
    "CloseoutResult": ".models",
    "EventKind": ".models",
    "EventState": ".models",
    "Finding": ".models",
    "ImplementerOutcome": ".models",
    "ImplementerResult": ".models",
    "ModelSource": ".models",
    "RebaseResult": ".models",
    "ReviewerResult": ".models",
    "ReviewerVerdict": ".models",
    "RolePolicy": ".models",
    "StandingRole": ".models",
    "TaskResult": ".models",
    "TaskRole": ".models",
    "TaskState": ".models",
    "TestResult": ".models",
    "WorkflowPolicy": ".models",
    "WorkflowStatus": ".models",
    "standing_role_accepts_task": ".models",
    "AgentConfig": ".config_layers",
    "ConfigLayerError": ".config_layers",
    "ForgeConfig": ".config_layers",
    "ResolvedConfig": ".config_layers",
    "default_worktree_root": ".config_layers",
    "load_config_layers": ".config_layers",
    "repository_config_path": ".config_layers",
    "repository_name": ".config_layers",
    "user_config_path": ".config_layers",
}


def __getattr__(name: str) -> Any:
    """Resolve the pydantic-backed re-exports on first use (PEP 562, #647)."""

    module_name = _LAZY_MODULES.get(name)
    if module_name is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    value = getattr(import_module(module_name, __name__), name)
    globals()[name] = value
    return value


__all__ = [
    "AGENT_HEADER",
    "CLOSEOUT_WAIT_DEFAULT_S",
    "CLOSEOUT_WAIT_MAX_S",
    "IMPLEMENTER_RESULT_SCHEMA",
    "REBASE_RESULT_SCHEMA",
    "REVIEWER_RESULT_SCHEMA",
    "SCHEMA_VERSION",
    "SHA_HEX_40_RE",
    "UNKNOWN",
    "WORKER_STANDING_ROLES",
    "AgentCloseoutReport",
    "CloseoutContext",
    "CloseoutResult",
    "AgentConfig",
    "AgentProfile",
    "AgentStatus",
    "ConfigLayerError",
    "ConfigurationError",
    "DEFAULT_EVENT_LEASE_S",
    "EventKind",
    "EventState",
    "Finding",
    "ForgeConfig",
    "HubSettings",
    "ImplementerOutcome",
    "ImplementerResult",
    "MAX_HOLD_DEFAULT_S",
    "MAX_MESSAGE_PART_BYTES",
    "MAX_STATEMENT_BYTES",
    "MAX_TYPED_RESULT_BYTES",
    "MetaKeys",
    "ModelSource",
    "RebaseResult",
    "ResolvedConfig",
    "RolePolicy",
    "ReviewerResult",
    "ReviewerVerdict",
    "StandingRole",
    "TaskResult",
    "TaskRole",
    "TaskState",
    "TestResult",
    "TokenError",
    "WorkflowStatus",
    "WorkflowPolicy",
    "default_worktree_root",
    "iso_after",
    "load_config_layers",
    "load_or_create_token",
    "profile_from_env",
    "read_token_file",
    "repository_config_path",
    "repository_name",
    "reserve_stdout",
    "standing_role_accepts_task",
    "to_iso",
    "token_matches",
    "user_config_path",
    "utcnow",
    "utcnow_iso",
    "validate_agent_name",
]
