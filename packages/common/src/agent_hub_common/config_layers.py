"""Three-layer configuration reader (mvp-spec §4).

Settings are read from three TOML files, first match wins:
1. `hub`: `<state_dir>/config.toml`
2. `repository`: `<git-common-dir>/robomate/config.toml`
3. `user`: `$XDG_CONFIG_HOME/robomate/config.toml` (POSIX) or
   `%APPDATA%\\robomate\\config.toml` (Windows)

Each key may appear only in the layers allowed by the §4 key-to-layer table;
a disallowed or unknown key raises `ConfigLayerError` naming the file and key.
"""

from __future__ import annotations

import os
import sys
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from pydantic import ValidationError

from .discovery import (
    DiscoveryError,
    repository_dir,
    validate_agent_name,
)
from .models import UNKNOWN, RolePolicy, WorkflowPolicy

LayerName = Literal["hub", "repository", "user"]

_REPO_USER_LAYERS: tuple[LayerName, ...] = ("repository", "user")
_HUB_REPO_LAYERS: tuple[LayerName, ...] = ("hub", "repository")
_ALL_LAYERS: tuple[LayerName, ...] = ("hub", "repository", "user")

_POLICY_SCALAR_KEYS: frozenset[str] = frozenset(
    {
        "max_review_rounds",
        "merge_method",
        "allow_no_ci",
        "pairing_wait_s",
        "max_wall_minutes",
        "max_task_lease_min",
        "stall_after_min",
        "on_done",
    }
)
_ROLE_POLICY_KEYS: frozenset[str] = frozenset(
    {
        "reviewer_harness_differs",
        "reviewer_provider_differs",
        "implementer_capabilities",
        "reviewer_capabilities",
    }
)
_REPO_PROCESS_KEYS: frozenset[str] = frozenset(
    {
        "setup",
        "nits_issue",
        "followup_label",
        "closeout_steps",
    }
)
_AGENT_PIN_KEYS: frozenset[str] = frozenset({"model", "effort"})
_FORGE_KEYS: frozenset[str] = frozenset({"gitlab_hosts", "github_hosts"})

DEFAULT_FOLLOWUP_LABEL = "robomate-followup"


class ConfigLayerError(DiscoveryError, ValueError):
    """A configuration layer file is malformed or violates the §4 key-to-layer rules."""


@dataclass(frozen=True)
class ForgeConfig:
    """Resolved `[forge]` host lists from the repository and user layers (§4, §10)."""

    gitlab_hosts: tuple[str, ...] = ()
    github_hosts: tuple[str, ...] = ()
    _layer_hosts: tuple[tuple[str, tuple[str, ...], tuple[str, ...]], ...] = field(
        default=(), repr=False, compare=False
    )

    def detect(self, host: str) -> Literal["gitlab", "github"] | None:
        """Return the configured forge for `host`, respecting layer precedence."""
        normalized = host.strip().lower()
        if not normalized:
            return None
        for _layer, gitlab, github in self._layer_hosts:
            if normalized in gitlab:
                return "gitlab"
            if normalized in github:
                return "github"
        return None


@dataclass(frozen=True)
class AgentConfig:
    """Resolved model and effort pins for an agent or as default (§4, §5)."""

    model: str | None = None
    effort: str | None = None

    def __getitem__(self, key: str) -> str | None:
        if key == "model":
            return self.model
        if key == "effort":
            return self.effort
        raise KeyError(key)


@dataclass(frozen=True)
class ResolvedConfig:
    """Resolved settings across the hub, repository, and user configuration layers."""

    worktree_root: Path | None = None
    worktree_root_layer: LayerName | None = None
    forge: ForgeConfig = field(default_factory=ForgeConfig)
    setup: str | None = None
    nits_issue: int | str | None = None
    followup_label: str = DEFAULT_FOLLOWUP_LABEL
    closeout_steps: tuple[str, ...] = ()
    policy: WorkflowPolicy = field(default_factory=WorkflowPolicy)
    policy_overrides: dict[str, Any] = field(default_factory=dict)
    agent_defaults: AgentConfig = field(default_factory=AgentConfig)
    agents: dict[str, AgentConfig] = field(default_factory=dict)
    warnings: tuple[str, ...] = ()

    @property
    def max_review_rounds(self) -> int:
        return self.policy.max_review_rounds

    @property
    def merge_method(self) -> Literal["merge", "squash", "rebase"]:
        return self.policy.merge_method

    @property
    def allow_no_ci(self) -> bool:
        return self.policy.allow_no_ci

    @property
    def role_policy(self) -> RolePolicy:
        return self.policy.role_policy

    @property
    def pairing_wait_s(self) -> int | float:
        return self.policy.pairing_wait_s

    @property
    def max_wall_minutes(self) -> int | float:
        return self.policy.max_wall_minutes

    @property
    def max_task_lease_min(self) -> int | float:
        return self.policy.max_task_lease_min

    @property
    def stall_after_min(self) -> int | float:
        return self.policy.stall_after_min

    @property
    def on_done(self) -> Literal["release", "standby"]:
        return self.policy.on_done

    @property
    def model(self) -> str | None:
        return self.agent_defaults.model

    @property
    def effort(self) -> str | None:
        return self.agent_defaults.effort

    def agent_config(self, name: str) -> AgentConfig:
        """Return resolved pins for `name`, falling back to `agent_defaults`."""
        if name in self.agents:
            return self.agents[name]
        return self.agent_defaults


def repository_name(git_common_dir: Path) -> str:
    """Return `<repo>` from the git common directory (mvp-spec §4)."""
    repo_dir = repository_dir(git_common_dir)
    name = repo_dir.name.removesuffix(".git")
    return name or repo_dir.name


def default_worktree_root(git_common_dir: Path) -> Path:
    """Return the default `<parent>/<repo>.robomate` worktree root (mvp-spec §4)."""
    repo_dir = repository_dir(git_common_dir)
    name = repository_name(git_common_dir)
    return repo_dir.parent / f"{name}.robomate"


def repository_config_path(git_common_dir: Path) -> Path:
    """Return `<git-common-dir>/robomate/config.toml` (mvp-spec §4)."""
    return git_common_dir / "robomate" / "config.toml"


def user_config_path(
    environ: Mapping[str, str] | None = None,
    *,
    platform: str | None = None,
) -> Path:
    """Return the user-layer `config.toml` path on POSIX or Windows (mvp-spec §4).

    POSIX: `$XDG_CONFIG_HOME/robomate/config.toml` (fallback `~/.config/robomate/config.toml`).
    Windows: `%APPDATA%\\robomate\\config.toml` (fallback `~/AppData/Roaming/robomate/config.toml`).
    """
    env = os.environ if environ is None else environ
    active_platform = sys.platform if platform is None else platform
    if active_platform == "win32":
        appdata = env.get("APPDATA", "").strip()
        base = Path(appdata) if appdata else Path.home() / "AppData" / "Roaming"
    else:
        xdg = env.get("XDG_CONFIG_HOME", "").strip()
        configured = Path(xdg) if xdg else Path()
        base = configured if configured.is_absolute() else Path.home() / ".config"
    return base / "robomate" / "config.toml"


def _layer_error(
    path: Path,
    key: str,
    layer: LayerName,
    allowed: tuple[LayerName, ...],
) -> ConfigLayerError:
    allowed_list = ", ".join(allowed)
    return ConfigLayerError(
        f"invalid configuration in {path}: key {key!r} is not allowed in the {layer} layer "
        f"(allowed in: {allowed_list})"
    )


def _value_error(path: Path, key: str, detail: str) -> ConfigLayerError:
    return ConfigLayerError(f"invalid configuration in {path}: invalid value for {key!r}: {detail}")


def _unknown_key_error(path: Path, key: str) -> ConfigLayerError:
    return ConfigLayerError(f"invalid configuration in {path}: unknown key {key!r}")


def _read_toml(path: Path) -> dict[str, Any]:
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return {}
    except (OSError, UnicodeDecodeError) as exc:
        raise ConfigLayerError(f"cannot read configuration in {path}: {exc}") from exc
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise ConfigLayerError(f"invalid configuration in {path}: {exc}") from exc
    return data


def _validate_nonempty_str(path: Path, key: str, value: Any) -> str:
    if not isinstance(value, str) or not value.strip():
        raise _value_error(path, key, "expected a non-empty string")
    return value


def _validate_pin_str(path: Path, key: str, value: Any) -> str:
    validated = _validate_nonempty_str(path, key, value).strip()
    if validated == UNKNOWN:
        raise _value_error(path, key, f"cannot pin {UNKNOWN!r}")
    return validated


def _validate_str_list(path: Path, key: str, value: Any) -> tuple[str, ...]:
    if not isinstance(value, list) or not all(
        isinstance(item, str) and item.strip() for item in value
    ):
        raise _value_error(path, key, "expected a list of non-empty strings")
    return tuple(item for item in value)


@dataclass
class _ParsedLayer:
    layer: LayerName
    path: Path
    worktree_root: str | None = None
    forge_gitlab_hosts: tuple[str, ...] | None = None
    forge_github_hosts: tuple[str, ...] | None = None
    is_legacy_forge: bool = False
    setup: str | None = None
    nits_issue: int | str | None = None
    followup_label: str | None = None
    closeout_steps: tuple[str, ...] | None = None
    policy_scalars: dict[str, Any] = field(default_factory=dict)
    role_policy_fields: dict[str, Any] = field(default_factory=dict)
    agent_default_pins: dict[str, str] = field(default_factory=dict)
    agent_pins: dict[str, dict[str, str]] = field(default_factory=dict)


def _parse_role_policy_table(path: Path, prefix: str, table: Any, target: dict[str, Any]) -> None:
    if not isinstance(table, dict):
        raise _value_error(path, prefix, "expected a table")
    for key, value in table.items():
        qualified = f"{prefix}.{key}"
        if key not in _ROLE_POLICY_KEYS:
            raise _unknown_key_error(path, qualified)
        if key in target:
            raise _value_error(path, qualified, "duplicate role_policy setting in the same file")
        try:
            RolePolicy.model_validate({key: value})
        except ValidationError as exc:
            raise _value_error(path, qualified, exc.errors()[0]["msg"]) from exc
        target[key] = value


def _parse_policy_scalar(
    path: Path, qualified: str, key: str, value: Any, target: dict[str, Any]
) -> None:
    if key in target:
        raise _value_error(path, qualified, "duplicate policy setting in the same file")
    try:
        WorkflowPolicy.model_validate({key: value})
    except ValidationError as exc:
        raise _value_error(path, qualified, exc.errors()[0]["msg"]) from exc
    target[key] = value


def _parse_agent_pins_table(path: Path, prefix: str, table: Any, target: dict[str, str]) -> None:
    if not isinstance(table, dict):
        raise _value_error(path, prefix, "expected a table")
    for key, value in table.items():
        qualified = f"{prefix}.{key}"
        if key not in _AGENT_PIN_KEYS:
            raise _unknown_key_error(path, qualified)
        if key in target:
            raise _value_error(path, qualified, f"duplicate {key!r} setting in the same file")
        target[key] = _validate_pin_str(path, qualified, value)


def _parse_agents_section(
    path: Path,
    prefix: str,
    table: Any,
    default_pins: dict[str, str],
    per_agent_pins: dict[str, dict[str, str]],
) -> None:
    if not isinstance(table, dict):
        raise _value_error(path, prefix, "expected a table")
    for key, value in table.items():
        qualified = f"{prefix}.{key}"
        if key in _AGENT_PIN_KEYS and not isinstance(value, dict):
            if key in default_pins:
                raise _value_error(path, qualified, f"duplicate {key!r} setting in the same file")
            default_pins[key] = _validate_pin_str(path, qualified, value)
            continue
        if not isinstance(value, dict):
            if key not in _AGENT_PIN_KEYS:
                raise _unknown_key_error(path, qualified)
            raise _value_error(path, qualified, "expected a string or agent table")
        try:
            validate_agent_name(key)
        except ValueError as exc:
            raise _value_error(path, qualified, str(exc)) from exc
        pins = per_agent_pins.setdefault(key, {})
        _parse_agent_pins_table(path, qualified, value, pins)


def _parse_layer(
    layer: LayerName,
    path: Path,
    data: dict[str, Any],
    *,
    allow_legacy_forge: bool,
) -> _ParsedLayer:
    parsed = _ParsedLayer(layer=layer, path=path)
    for key, value in data.items():
        if key == "worktree_root":
            if layer not in _REPO_USER_LAYERS:
                raise _layer_error(path, key, layer, _REPO_USER_LAYERS)
            raw_root = _validate_nonempty_str(path, key, value).strip()
            if not Path(raw_root).expanduser().is_absolute():
                raise _value_error(path, key, "expected an absolute path")
            parsed.worktree_root = raw_root
            continue

        if key == "forge":
            if not isinstance(value, dict):
                if layer not in _REPO_USER_LAYERS and not allow_legacy_forge:
                    raise _layer_error(path, key, layer, _REPO_USER_LAYERS)
                raise _value_error(path, key, "expected a table")
            if layer not in _REPO_USER_LAYERS and not allow_legacy_forge:
                first_subkey = next(iter(value), None)
                reported = f"forge.{first_subkey}" if first_subkey else "forge"
                raise _layer_error(path, reported, layer, _REPO_USER_LAYERS)
            for subkey, subval in value.items():
                qualified = f"forge.{subkey}"
                if subkey not in _FORGE_KEYS:
                    raise _unknown_key_error(path, qualified)
                hosts = tuple(
                    item.strip().lower() for item in _validate_str_list(path, qualified, subval)
                )
                if subkey == "gitlab_hosts":
                    parsed.forge_gitlab_hosts = hosts
                else:
                    parsed.forge_github_hosts = hosts
            if (
                parsed.forge_gitlab_hosts
                and parsed.forge_github_hosts
                and (overlap := set(parsed.forge_gitlab_hosts) & set(parsed.forge_github_hosts))
            ):
                dup = sorted(overlap)[0]
                raise _value_error(
                    path, "forge", f"host {dup!r} cannot be in both gitlab_hosts and github_hosts"
                )
            if layer == "hub" and (
                parsed.forge_gitlab_hosts is not None or parsed.forge_github_hosts is not None
            ):
                parsed.is_legacy_forge = True
            continue

        if key in _REPO_PROCESS_KEYS:
            if layer not in _HUB_REPO_LAYERS:
                raise _layer_error(path, key, layer, _HUB_REPO_LAYERS)
            if key == "setup":
                parsed.setup = _validate_nonempty_str(path, key, value)
            elif key == "nits_issue":
                if isinstance(value, int) and not isinstance(value, bool):
                    if value <= 0:
                        raise _value_error(path, key, "expected a positive issue number")
                    parsed.nits_issue = value
                elif isinstance(value, str) and value.strip():
                    parsed.nits_issue = value.strip()
                else:
                    raise _value_error(path, key, "expected a positive integer or non-empty string")
            elif key == "followup_label":
                parsed.followup_label = _validate_nonempty_str(path, key, value).strip()
            elif key == "closeout_steps":
                parsed.closeout_steps = _validate_str_list(path, key, value)
            continue

        if key in _POLICY_SCALAR_KEYS:
            _parse_policy_scalar(path, key, key, value, parsed.policy_scalars)
            continue

        if key == "role_policy":
            _parse_role_policy_table(path, "role_policy", value, parsed.role_policy_fields)
            continue

        if key == "policy":
            if not isinstance(value, dict):
                raise _value_error(path, "policy", "expected a table")
            for subkey, subval in value.items():
                qualified = f"policy.{subkey}"
                if subkey == "role_policy":
                    _parse_role_policy_table(path, qualified, subval, parsed.role_policy_fields)
                elif subkey in _POLICY_SCALAR_KEYS:
                    _parse_policy_scalar(path, qualified, subkey, subval, parsed.policy_scalars)
                else:
                    raise _unknown_key_error(path, qualified)
            continue

        if key == "agents":
            _parse_agents_section(path, key, value, parsed.agent_default_pins, parsed.agent_pins)
            continue

        raise _unknown_key_error(path, key)

    return parsed


def load_config_layers(
    hub_config: Path | None = None,
    repository_config: Path | None = None,
    user_config: Path | None = None,
    *,
    state_dir: Path | None = None,
    git_common_dir: Path | None = None,
    environ: Mapping[str, str] | None = None,
    platform: str | None = None,
    allow_legacy_forge: bool | None = None,
) -> ResolvedConfig:
    """Read and merge the hub, repository, and user configuration layers (mvp-spec §4).

    First match wins across `(hub, repository, user)` for keys permitted in multiple
    layers. Any key placed in a layer that §4 forbids raises `ConfigLayerError`
    naming the file and the key.
    """
    common = git_common_dir

    if hub_config is not None:
        hub_path: Path | None = hub_config
    elif state_dir is not None:
        hub_path = state_dir / "config.toml"
    else:
        hub_path = None

    if repository_config is not None:
        repo_path: Path | None = repository_config
        if common is None and repository_config.parent.name == "robomate":
            common = repository_config.parent.parent
    elif common is not None:
        repo_path = repository_config_path(common)
    else:
        repo_path = None

    user_path = (
        user_config if user_config is not None else user_config_path(environ, platform=platform)
    )

    legacy_forge_ok = (
        (hub_path is not None and hub_path.parent.name == ".robomate")
        if allow_legacy_forge is None
        else allow_legacy_forge
    )

    layers: list[_ParsedLayer] = []
    if hub_path is not None and hub_path.exists():
        layers.append(
            _parse_layer(
                "hub",
                hub_path,
                _read_toml(hub_path),
                allow_legacy_forge=legacy_forge_ok,
            )
        )
    if repo_path is not None and repo_path.exists():
        layers.append(
            _parse_layer(
                "repository",
                repo_path,
                _read_toml(repo_path),
                allow_legacy_forge=False,
            )
        )
    if user_path is not None and user_path.exists():
        layers.append(
            _parse_layer(
                "user",
                user_path,
                _read_toml(user_path),
                allow_legacy_forge=False,
            )
        )

    warnings_list: list[str] = []
    for item in layers:
        if item.is_legacy_forge:
            target = (
                f"{repo_path} (<git-common-dir>/robomate/config.toml)"
                if repo_path is not None
                else "<git-common-dir>/robomate/config.toml"
            )
            msg = (
                f"{item.path}: [forge] in .robomate/config.toml is deprecated; move it to {target}"
            )
            warnings_list.append(msg)

    # 1. Resolve worktree_root (repository > user, else default from git_common_dir)
    worktree_root: Path | None = None
    worktree_root_layer: LayerName | None = None
    for item in layers:
        if item.layer in _REPO_USER_LAYERS and item.worktree_root is not None:
            raw = Path(item.worktree_root).expanduser()
            if item.layer == "repository":
                worktree_root = raw
            else:
                # User-level worktree_root is a parent directory (§4).
                if common is not None:
                    worktree_root = raw / f"{repository_name(common)}.robomate"
                else:
                    worktree_root = raw
            worktree_root_layer = item.layer
            break
    if worktree_root is None and common is not None:
        worktree_root = default_worktree_root(common)

    # 2. Resolve forge.* (repository > user > legacy hub .robomate/config.toml)
    forge_order = [
        *(item for item in layers if item.layer == "repository"),
        *(item for item in layers if item.layer == "user"),
        *(item for item in layers if item.layer == "hub" and item.is_legacy_forge),
    ]
    gitlab_hosts: tuple[str, ...] | None = None
    github_hosts: tuple[str, ...] | None = None
    layer_hosts: list[tuple[str, tuple[str, ...], tuple[str, ...]]] = []
    for item in forge_order:
        active_gl: tuple[str, ...] = ()
        active_gh: tuple[str, ...] = ()
        if gitlab_hosts is None and item.forge_gitlab_hosts is not None:
            gitlab_hosts = item.forge_gitlab_hosts
            active_gl = item.forge_gitlab_hosts
        if github_hosts is None and item.forge_github_hosts is not None:
            github_hosts = item.forge_github_hosts
            active_gh = item.forge_github_hosts
        if active_gl or active_gh:
            layer_hosts.append((item.layer, active_gl, active_gh))

    forge = ForgeConfig(
        gitlab_hosts=gitlab_hosts or (),
        github_hosts=github_hosts or (),
        _layer_hosts=tuple(layer_hosts),
    )

    # 3. Resolve repository-process settings (hub > repository)
    setup: str | None = None
    nits_issue: int | str | None = None
    followup_label: str | None = None
    closeout_steps: tuple[str, ...] | None = None
    for item in layers:
        if item.layer not in _HUB_REPO_LAYERS:
            continue
        if setup is None and item.setup is not None:
            setup = item.setup
        if nits_issue is None and item.nits_issue is not None:
            nits_issue = item.nits_issue
        if followup_label is None and item.followup_label is not None:
            followup_label = item.followup_label
        if closeout_steps is None and item.closeout_steps is not None:
            closeout_steps = item.closeout_steps

    # 4. Resolve policy defaults (hub > repository > user, per field)
    merged_policy_scalars: dict[str, Any] = {}
    merged_role_policy: dict[str, Any] = {}
    for item in layers:
        for key, value in item.policy_scalars.items():
            merged_policy_scalars.setdefault(key, value)
        for key, value in item.role_policy_fields.items():
            merged_role_policy.setdefault(key, value)

    policy_payload: dict[str, Any] = dict(merged_policy_scalars)
    if merged_role_policy:
        policy_payload["role_policy"] = merged_role_policy
    policy = WorkflowPolicy.model_validate(policy_payload)

    policy_overrides: dict[str, Any] = dict(merged_policy_scalars)
    if merged_role_policy:
        policy_overrides["role_policy"] = dict(merged_role_policy)

    # 5. Resolve agent defaults and per-agent pins (hub > repository > user, per field)
    default_model: str | None = None
    default_effort: str | None = None
    agent_names: set[str] = set()
    for item in layers:
        if default_model is None and "model" in item.agent_default_pins:
            default_model = item.agent_default_pins["model"]
        if default_effort is None and "effort" in item.agent_default_pins:
            default_effort = item.agent_default_pins["effort"]
        agent_names.update(item.agent_pins)

    agent_defaults = AgentConfig(model=default_model, effort=default_effort)
    resolved_agents: dict[str, AgentConfig] = {}
    for name in sorted(agent_names):
        pin_model: str | None = None
        pin_effort: str | None = None
        for item in layers:
            pins = item.agent_pins.get(name)
            if not pins:
                continue
            if pin_model is None and "model" in pins:
                pin_model = pins["model"]
            if pin_effort is None and "effort" in pins:
                pin_effort = pins["effort"]
        resolved_agents[name] = AgentConfig(
            model=pin_model if pin_model is not None else default_model,
            effort=pin_effort if pin_effort is not None else default_effort,
        )

    return ResolvedConfig(
        worktree_root=worktree_root,
        worktree_root_layer=worktree_root_layer,
        forge=forge,
        setup=setup,
        nits_issue=nits_issue,
        followup_label=followup_label if followup_label is not None else DEFAULT_FOLLOWUP_LABEL,
        closeout_steps=closeout_steps if closeout_steps is not None else (),
        policy=policy,
        policy_overrides=policy_overrides,
        agent_defaults=agent_defaults,
        agents=resolved_agents,
        warnings=tuple(warnings_list),
    )
