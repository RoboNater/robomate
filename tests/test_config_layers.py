"""Unit tests for the §4 three-layer configuration reader (M2 Step 5, #242)."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
from agent_hub_common.config_layers import (
    DEFAULT_FOLLOWUP_LABEL,
    ConfigLayerError,
    default_worktree_root,
    load_config_layers,
    repository_config_path,
    user_config_path,
)
from agent_hub_common.discovery import detect_forge


def _write_toml(path: Path, content: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path


def test_user_config_path_linux_and_windows(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    posix_env = {"XDG_CONFIG_HOME": str(tmp_path / "xdg-cfg")}
    monkeypatch.setattr(sys, "platform", "linux")
    assert user_config_path(posix_env) == tmp_path / "xdg-cfg" / "robomate" / "config.toml"

    # Relative XDG_CONFIG_HOME is ignored per XDG spec, falling back to ~/.config.
    assert (
        user_config_path({"XDG_CONFIG_HOME": "relative/path"})
        == Path.home() / ".config" / "robomate" / "config.toml"
    )

    win_env = {"APPDATA": str(tmp_path / "AppData" / "Roaming")}
    monkeypatch.setattr(sys, "platform", "win32")
    assert (
        user_config_path(win_env) == tmp_path / "AppData" / "Roaming" / "robomate" / "config.toml"
    )
    assert user_config_path({}) == Path.home() / "AppData" / "Roaming" / "robomate" / "config.toml"


@pytest.mark.parametrize("sim_platform", ["linux", "win32"])
def test_precedence_for_each_key_class_on_linux_and_windows_paths(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, sim_platform: str
) -> None:
    monkeypatch.setattr(sys, "platform", sim_platform)
    env = {
        "XDG_CONFIG_HOME": str(tmp_path / "posix-config"),
        "APPDATA": str(tmp_path / "win-appdata"),
    }
    user_cfg = user_config_path(env)
    common_dir = tmp_path / "src" / "my-repo" / ".git"
    common_dir.mkdir(parents=True)
    repo_cfg = repository_config_path(common_dir)
    hub_cfg = tmp_path / "hub-state" / "config.toml"

    # User layer: sets class 1 (worktree_root, forge.*) and class 3 (policy, agents)
    _write_toml(
        user_cfg,
        """
worktree_root = "{user_wt}"
max_review_rounds = 2
merge_method = "rebase"

[forge]
gitlab_hosts = ["gl-user.corp.example"]
github_hosts = ["gh-user.corp.example"]

[role_policy]
reviewer_provider_differs = true

[agents]
model = "user-default-model"
effort = "low"

[agents.bob]
model = "user-bob-model"
effort = "medium"
""".format(user_wt=(tmp_path / "user-wt-parent").as_posix()),
    )

    with_user_only = load_config_layers(
        hub_config=hub_cfg,
        git_common_dir=common_dir,
        environ=env,
    )
    assert with_user_only.worktree_root == tmp_path / "user-wt-parent" / "my-repo.robomate"
    assert with_user_only.worktree_root_layer == "user"
    assert with_user_only.forge.gitlab_hosts == ("gl-user.corp.example",)
    assert with_user_only.forge.github_hosts == ("gh-user.corp.example",)
    assert with_user_only.max_review_rounds == 2
    assert with_user_only.merge_method == "rebase"
    assert with_user_only.role_policy.reviewer_provider_differs is True
    assert with_user_only.agent_defaults.model == "user-default-model"
    assert with_user_only.agent_defaults.effort == "low"
    assert with_user_only.agent_config("bob").model == "user-bob-model"
    assert with_user_only.agent_config("bob").effort == "medium"

    # Repository layer: overrides user on class 1 & class 3, and sets class 2
    _write_toml(
        repo_cfg,
        """
worktree_root = "{repo_wt}"
setup = "uv sync --locked"
nits_issue = 4
followup_label = "repo-followup"
closeout_steps = ["Update roadmap #2"]
max_review_rounds = 4

[forge]
gitlab_hosts = ["gl-repo.corp.example"]

[role_policy]
reviewer_harness_differs = false

[agents]
model = "repo-default-model"

[agents.bob]
model = "repo-bob-model"
""".format(repo_wt=(tmp_path / "repo-wt-explicit").as_posix()),
    )

    with_repo_and_user = load_config_layers(
        hub_config=hub_cfg,
        git_common_dir=common_dir,
        environ=env,
    )
    assert with_repo_and_user.worktree_root == tmp_path / "repo-wt-explicit"
    assert with_repo_and_user.worktree_root_layer == "repository"
    assert with_repo_and_user.forge.gitlab_hosts == ("gl-repo.corp.example",)
    # github_hosts was not set in repository layer, so user layer still supplies it
    assert with_repo_and_user.forge.github_hosts == ("gh-user.corp.example",)
    assert with_repo_and_user.setup == "uv sync --locked"
    assert with_repo_and_user.nits_issue == 4
    assert with_repo_and_user.followup_label == "repo-followup"
    assert with_repo_and_user.closeout_steps == ("Update roadmap #2",)
    assert with_repo_and_user.max_review_rounds == 4
    assert with_repo_and_user.merge_method == "rebase"
    assert with_repo_and_user.role_policy.reviewer_harness_differs is False
    assert with_repo_and_user.role_policy.reviewer_provider_differs is True
    assert with_repo_and_user.agent_defaults.model == "repo-default-model"
    assert with_repo_and_user.agent_defaults.effort == "low"
    assert with_repo_and_user.agent_config("bob").model == "repo-bob-model"
    assert with_repo_and_user.agent_config("bob").effort == "medium"

    # Hub layer: overrides repository and user on class 2 and class 3
    _write_toml(
        hub_cfg,
        """
setup = "uv sync --all-packages"
nits_issue = 19
followup_label = "hub-followup"
closeout_steps = ["Step A", "Step B"]
max_review_rounds = 6
on_done = "standby"

[agents]
effort = "high"

[agents.bob]
model = "hub-bob-model"
""",
    )

    resolved = load_config_layers(
        hub_config=hub_cfg,
        git_common_dir=common_dir,
        environ=env,
    )
    assert resolved.setup == "uv sync --all-packages"
    assert resolved.nits_issue == 19
    assert resolved.followup_label == "hub-followup"
    assert resolved.closeout_steps == ("Step A", "Step B")
    assert resolved.max_review_rounds == 6
    assert resolved.on_done == "standby"
    assert resolved.merge_method == "rebase"
    assert resolved.agent_defaults.model == "repo-default-model"
    assert resolved.agent_defaults.effort == "high"
    assert resolved.agent_config("bob").model == "hub-bob-model"
    assert resolved.agent_config("bob").effort == "medium"
    assert resolved.agent_config("dave").model == "repo-default-model"
    assert resolved.agent_config("dave").effort == "high"


def test_max_review_rounds_resolves_repository_over_user_and_hub_over_both(
    tmp_path: Path,
) -> None:
    hub_cfg = tmp_path / "state" / "config.toml"
    repo_cfg = tmp_path / "repo" / ".git" / "robomate" / "config.toml"
    user_cfg = tmp_path / "user" / "robomate" / "config.toml"

    _write_toml(user_cfg, "max_review_rounds = 2\n")
    assert load_config_layers(hub_cfg, repo_cfg, user_cfg).policy.max_review_rounds == 2

    _write_toml(repo_cfg, "max_review_rounds = 5\n")
    resolved_repo = load_config_layers(hub_cfg, repo_cfg, user_cfg)
    assert resolved_repo.max_review_rounds == 5
    assert resolved_repo.policy.max_review_rounds == 5

    _write_toml(hub_cfg, "[policy]\nmax_review_rounds = 8\n")
    resolved_hub = load_config_layers(hub_cfg, repo_cfg, user_cfg)
    assert resolved_hub.max_review_rounds == 8
    assert resolved_hub.policy.max_review_rounds == 8


def test_worktree_root_in_hub_config_is_an_error(tmp_path: Path) -> None:
    for hub_dir_name in ("state", ".robomate"):
        hub_cfg = _write_toml(
            tmp_path / hub_dir_name / "config.toml",
            'worktree_root = "/tmp/illegal-root"\n',
        )
        with pytest.raises(ConfigLayerError) as exc_info:
            load_config_layers(hub_config=hub_cfg)
        msg = str(exc_info.value)
        assert str(hub_cfg) in msg
        assert "worktree_root" in msg


@pytest.mark.parametrize("sim_platform", ["linux", "win32"])
@pytest.mark.parametrize(
    ("key", "toml_line"),
    [
        ("nits_issue", "nits_issue = 4"),
        ("followup_label", 'followup_label = "triage"'),
        ("closeout_steps", 'closeout_steps = ["step"]'),
        ("setup", 'setup = "uv sync"'),
    ],
)
def test_repo_process_keys_in_user_layer_are_errors(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    sim_platform: str,
    key: str,
    toml_line: str,
) -> None:
    monkeypatch.setattr(sys, "platform", sim_platform)
    env = {
        "XDG_CONFIG_HOME": str(tmp_path / "xdg"),
        "APPDATA": str(tmp_path / "appdata"),
    }
    user_cfg = user_config_path(env)
    _write_toml(user_cfg, f"{toml_line}\n")

    with pytest.raises(ConfigLayerError) as exc_info:
        load_config_layers(environ=env)
    msg = str(exc_info.value)
    assert str(user_cfg) in msg
    assert key in msg


def test_forge_in_non_legacy_hub_config_is_an_error(tmp_path: Path) -> None:
    hub_cfg = _write_toml(
        tmp_path / "state" / "config.toml",
        '[forge]\ngitlab_hosts = ["gitlab.internal"]\n',
    )
    with pytest.raises(ConfigLayerError) as exc_info:
        load_config_layers(hub_config=hub_cfg)
    msg = str(exc_info.value)
    assert str(hub_cfg) in msg
    assert "forge.gitlab_hosts" in msg


def test_legacy_robomate_config_toml_forge_selects_gitlab_and_warns(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    common = repo / ".git"
    common.mkdir(parents=True)
    legacy_cfg = _write_toml(
        repo / ".robomate" / "config.toml",
        '[forge]\ngitlab_hosts = ["gitlab-legacy.corp.example"]\n',
    )
    before_bytes = legacy_cfg.read_bytes()

    resolved = load_config_layers(
        hub_config=legacy_cfg,
        git_common_dir=common,
    )
    assert resolved.forge.gitlab_hosts == ("gitlab-legacy.corp.example",)
    assert resolved.forge.detect("gitlab-legacy.corp.example") == "gitlab"
    assert len(resolved.warnings) == 1
    assert str(legacy_cfg) in resolved.warnings[0]
    assert str(repository_config_path(common)) in resolved.warnings[0]
    # Nothing is rewritten for the operator.
    assert legacy_cfg.read_bytes() == before_bytes

    forge = detect_forge(
        "git@gitlab-legacy.corp.example:team/project.git",
        root=repo,
        git_common_dir=common,
    )
    assert forge == "gitlab"


def test_default_worktree_root_for_git_and_bare_common_dirs(tmp_path: Path) -> None:
    for common in (
        tmp_path / "src" / "my-repo" / ".git",
        tmp_path / "src" / "my-repo.git",
        tmp_path / "src" / "my-repo" / ".bare",
    ):
        common.mkdir(parents=True, exist_ok=True)
        assert default_worktree_root(common) == tmp_path / "src" / "my-repo.robomate"
        resolved = load_config_layers(git_common_dir=common)
        assert resolved.worktree_root == tmp_path / "src" / "my-repo.robomate"
        assert resolved.followup_label == DEFAULT_FOLLOWUP_LABEL
        assert resolved.closeout_steps == ()
        assert resolved.nits_issue is None
        assert resolved.setup is None


def test_relative_worktree_root_in_repo_or_user_layer_is_an_error(tmp_path: Path) -> None:
    repo_cfg = _write_toml(
        tmp_path / "repo" / ".git" / "robomate" / "config.toml",
        'worktree_root = "relative/worktrees"\n',
    )
    with pytest.raises(ConfigLayerError) as exc_info:
        load_config_layers(repository_config=repo_cfg)
    assert str(repo_cfg) in str(exc_info.value)
    assert "worktree_root" in str(exc_info.value)

    user_cfg = _write_toml(
        tmp_path / "user" / "robomate" / "config.toml",
        'worktree_root = "relative/worktrees"\n',
    )
    with pytest.raises(ConfigLayerError) as exc_info:
        load_config_layers(user_config=user_cfg)
    assert str(user_cfg) in str(exc_info.value)
    assert "worktree_root" in str(exc_info.value)


def test_non_utf8_config_toml_raises_config_layer_error_naming_file(tmp_path: Path) -> None:
    repo_cfg = tmp_path / ".git" / "robomate" / "config.toml"
    repo_cfg.parent.mkdir(parents=True, exist_ok=True)
    repo_cfg.write_bytes(b"\xff\xfeinvalid utf-8")

    with pytest.raises(ConfigLayerError) as exc_info:
        load_config_layers(repository_config=repo_cfg)
    assert str(repo_cfg) in str(exc_info.value)


def test_unknown_key_and_invalid_values_name_file_and_key(tmp_path: Path) -> None:
    repo_cfg = tmp_path / ".git" / "robomate" / "config.toml"

    _write_toml(repo_cfg, "mystery_setting = 42\n")
    with pytest.raises(ConfigLayerError) as exc_info:
        load_config_layers(repository_config=repo_cfg)
    assert str(repo_cfg) in str(exc_info.value)
    assert "mystery_setting" in str(exc_info.value)

    _write_toml(repo_cfg, "nits_issue = 0\n")
    with pytest.raises(ConfigLayerError) as exc_info:
        load_config_layers(repository_config=repo_cfg)
    assert str(repo_cfg) in str(exc_info.value)
    assert "nits_issue" in str(exc_info.value)

    _write_toml(repo_cfg, '[agents.con]\nmodel = "gpt-5"\n')
    with pytest.raises(ConfigLayerError) as exc_info:
        load_config_layers(repository_config=repo_cfg)
    assert str(repo_cfg) in str(exc_info.value)
    assert "agents.con" in str(exc_info.value)

    _write_toml(repo_cfg, '[agents.bob]\nmodel = "unknown"\n')
    with pytest.raises(ConfigLayerError) as exc_info:
        load_config_layers(repository_config=repo_cfg)
    assert str(repo_cfg) in str(exc_info.value)
    assert "agents.bob.model" in str(exc_info.value)
