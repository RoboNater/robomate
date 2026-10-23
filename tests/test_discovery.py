"""Repository discovery and atomic local metadata."""

import errno
import json
import os
import re
import shutil
import subprocess
import sys
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from agent_hub_common import registry
from agent_hub_common.discovery import (
    BINDING_FILENAME,
    ROOT_RECORD_FILENAME,
    Binding,
    DiscoveryError,
    GitTimeoutError,
    HubEndpoint,
    LocalHub,
    check_worktree_root,
    checkout_git_dir,
    claim_worktree_root,
    create_hub_dirs,
    derive_hub_name,
    discover,
    ensure_excluded,
    find_hub,
    hub_bindings,
    is_legacy_state_dir,
    is_linked_worktree,
    legacy_state_dir,
    local_hub,
    plan_hub_placement,
    read_binding,
    read_hub_json,
    read_root_record,
    repository_dir,
    resolve_checkout,
    resolve_repository,
    state_dir,
    token_file,
    validate_hub_name,
    verify_local_hub,
    write_binding,
    write_hub_json,
)
from agent_hub_common.registry import register
from conftest import closed_port_url


def git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


@pytest.fixture
def repository(tmp_path: Path) -> Path:
    return init_repository(tmp_path / "repo")


def init_repository(root: Path) -> Path:
    root.mkdir(parents=True)
    git(root, "init", "-b", "main")
    git(root, "config", "user.name", "Test")
    git(root, "config", "user.email", "test@example.com")
    git(root, "commit", "--allow-empty", "-m", "initial")
    git(root, "remote", "add", "origin", "git@github.com:example/repo.git")
    git(root, "symbolic-ref", "refs/remotes/origin/HEAD", "refs/remotes/origin/main")
    return root


def test_each_checkout_owns_its_hub_and_shares_the_common_dir(
    repository: Path, tmp_path: Path
) -> None:
    worktree = tmp_path / "linked"
    git(repository, "worktree", "add", "-b", "linked", str(worktree))
    nested = worktree / "nested"
    nested.mkdir()
    common = repository / ".git"
    for cwd, owner in ((repository, repository), (worktree, worktree), (nested, worktree)):
        assert resolve_checkout(cwd) == (owner, common)
        info = resolve_repository(cwd)
        assert (info.root, info.default_branch, info.forge) == (owner, "main", "github")
    assert not is_linked_worktree(repository, common)
    assert is_linked_worktree(worktree, common)
    assert legacy_state_dir(worktree) != legacy_state_dir(repository)


def test_checkout_under_a_non_ascii_path_resolves_to_its_actual_path(tmp_path: Path) -> None:
    # Git prints paths as UTF-8; decoding them with the locale (CP1252 on
    # Windows without UTF-8 mode) names a directory that does not exist (#166).
    root = init_repository(tmp_path / "café" / "repo")
    worktree = tmp_path / "café" / "linked é"
    git(root, "worktree", "add", "-b", "linked", str(worktree))
    common = root / ".git"
    for cwd, owner in ((root, root), (worktree, worktree)):
        assert resolve_checkout(cwd) == (owner, common)
        assert resolve_repository(cwd).root == owner


def test_linked_worktree_of_a_bare_repository_owns_a_hub(repository: Path, tmp_path: Path) -> None:
    layout = tmp_path / "r"
    bare = layout / ".bare"
    subprocess.run(
        ["git", "clone", "--bare", str(repository), str(bare)], check=True, capture_output=True
    )
    git(bare, "worktree", "add", str(layout / "main"), "main")
    git(bare, "worktree", "add", "-b", "feature", str(layout / "feature"))
    assert resolve_checkout(layout / "main") == (layout / "main", bare)
    assert repository_dir(bare) == layout
    # A plain bare clone has no origin/HEAD: the bare repository's HEAD names
    # the default branch, whatever branch the worktree has checked out (#147 r1-2).
    for worktree in ("main", "feature"):
        info = resolve_repository(layout / worktree)
        assert (info.root, info.default_branch) == (layout / worktree, "main")
    with pytest.raises(DiscoveryError, match="bare repository with no working tree"):
        resolve_checkout(bare)
    with pytest.raises(DiscoveryError, match="not inside a git working tree"):
        resolve_checkout(repository / ".git")


@pytest.mark.parametrize(
    ("common", "expected"),
    [
        ("src/my-repo/.git", "src/my-repo"),
        ("src/my-repo.git", "src/my-repo.git"),
        ("src/my-repo/.bare", "src/my-repo"),
    ],
)
def test_repository_dir_follows_the_common_dir(common: str, expected: str) -> None:
    assert repository_dir(Path(common)) == Path(expected)


@pytest.mark.parametrize(
    ("directory", "name"),
    [
        ("wt-a", "wt-a"),
        ("My Repo (copy)", "my-repo-copy"),
        ("feature.branch_2", "feature-branch_2"),
        ("--Édition--", "dition"),
        ("x" * 50, "x" * 40),
        ("a" * 39 + "-b", "a" * 39),
    ],
)
def test_hub_name_is_derived_from_the_checkout_directory(directory: str, name: str) -> None:
    assert derive_hub_name(Path("/src") / directory) == name


@pytest.mark.parametrize("directory", ["...", "CON", "Token", "lpt9", "é", "-_-"])
def test_underivable_hub_name_asks_for_name(directory: str) -> None:
    with pytest.raises(DiscoveryError, match="--name"):
        derive_hub_name(Path("/src") / directory)


@pytest.mark.parametrize("name", ["a", "wt-a", "a_b", "0", "x" * 40, "com0", "states"])
def test_valid_hub_names(name: str) -> None:
    assert validate_hub_name(name) == name


@pytest.mark.parametrize(
    "name",
    ["", "A", "-a", "a-", "a_", "a.b", "a/b", "a..b", "x" * 41, "nul", "com1", "state", "token"],
)
def test_invalid_hub_names_are_refused(name: str) -> None:
    with pytest.raises(DiscoveryError, match="invalid hub name"):
        validate_hub_name(name)


def test_exclude_once_and_atomic_hub_json(
    repository: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    common = repository / ".git"
    ensure_excluded(common)
    ensure_excluded(common)
    assert (common / "info/exclude").read_text().splitlines().count("/.robomate/") == 1
    write_hub_json(legacy_state_dir(repository), {"port": 8420})
    assert read_hub_json(legacy_state_dir(repository)) == {"port": 8420}

    def fail_replace(*args: object) -> None:
        raise OSError("injected replace failure")

    monkeypatch.setattr("agent_hub_common.discovery.os.replace", fail_replace)
    with pytest.raises(OSError, match="injected"):
        write_hub_json(legacy_state_dir(repository), {"port": 8421})
    assert json.loads((repository / ".robomate/hub.json").read_text()) == {"port": 8420}
    assert sorted(p.name for p in (repository / ".robomate").iterdir()) == ["hub.json"]


def test_discovery_prefers_explicit_then_repo(
    repository: Path, healthz: Callable[[str], str]
) -> None:
    url = healthz("repo-id")
    write_hub_json(legacy_state_dir(repository), {"url": url, "hub_id": "repo-id"})
    (repository / ".robomate/token").write_text("repo-token\n")
    assert discover(repository, {}) == HubEndpoint(url, "repo-token")
    assert discover(
        repository,
        {"ROBOMATE_HUB_URL": "http://explicit:8421/", "ROBOMATE_TOKEN": "explicit-token"},
    ) == HubEndpoint("http://explicit:8421", "explicit-token")


def test_discover_checks_the_local_hub_identity_before_use(
    repository: Path, healthz: Callable[[str], str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """#147 r1-1: a local hub is used only when /healthz reports its recorded hub_id."""

    legacy_state_dir(repository).mkdir()
    (legacy_state_dir(repository) / "token").write_text("repo-token\n")
    matching = healthz("expected-hub")
    write_hub_json(
        legacy_state_dir(repository), {"url": matching, "hub_id": "expected-hub", "name": "repo"}
    )
    assert discover(repository, {}) == HubEndpoint(matching, "repo-token")

    other = healthz("different-hub")
    write_hub_json(
        legacy_state_dir(repository), {"url": other, "hub_id": "expected-hub", "name": "repo"}
    )
    with pytest.raises(DiscoveryError, match="answers as hub different-hub, not expected-hub"):
        discover(repository, {})

    stopped = closed_port_url()
    write_hub_json(
        legacy_state_dir(repository),
        {"url": stopped, "hub_id": "expected-hub", "name": "repo", "pid": None},
    )
    with pytest.raises(DiscoveryError, match=r"hub repo \(checkout .*\) at .* is not running"):
        discover(repository, {})
    monkeypatch.setattr("agent_hub_common.registry.process_alive", lambda pid: pid == 4321)
    write_hub_json(
        legacy_state_dir(repository), {"url": stopped, "hub_id": "expected-hub", "pid": 4321}
    )
    with pytest.raises(DiscoveryError, match="unreachable; recorded pid 4321 is visible"):
        discover(repository, {})
    # An explicit URL is taken as given: it may name a remote hub.
    explicit = {"ROBOMATE_HUB_URL": other, "ROBOMATE_TOKEN": "t"}
    assert discover(repository, explicit) == HubEndpoint(other, "t")


def _record_hub(checkout: Path, hub_id: str, name: str, env: dict[str, str]) -> None:
    common = resolve_checkout(checkout)[1]
    write_hub_json(
        legacy_state_dir(checkout), {"url": f"http://{name}:8420", "hub_id": hub_id, "name": name}
    )
    (legacy_state_dir(checkout) / "token").write_text(f"{name}-token\n")
    register(
        {
            "hub_id": hub_id,
            "name": name,
            "checkout": str(checkout),
            "git_common_dir": str(common),
            "url": f"http://{name}:8420",
            "pid": None,
        },
        env,
    )


def test_discovery_never_falls_back_to_the_only_hub(
    repository: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "hub"
    (root / ".robomate").mkdir(parents=True)
    (root / ".robomate/token").write_text("registry-token\n")
    one = {"name": "one", "checkout": str(root), "url": "http://registry:8420", "hub_id": "one"}
    monkeypatch.setattr("agent_hub_common.registry.live_entries", lambda env: [one])
    outside = tmp_path / "outside"
    outside.mkdir()
    with pytest.raises(DiscoveryError, match=r"(?s)not inside a git repository.*--hub.*one  "):
        discover(outside, {})
    with pytest.raises(DiscoveryError, match=r"(?s)no hub is recorded for .*registry:8420"):
        discover(repository, {})


def test_hub_selector_by_name_hub_id_or_checkout(repository: Path, tmp_path: Path) -> None:
    env = {"XDG_STATE_HOME": str(tmp_path / "xdg"), "LOCALAPPDATA": str(tmp_path / "local")}
    worktree = tmp_path / "wt-b"
    git(repository, "worktree", "add", "-b", "wt-b", str(worktree))
    _record_hub(repository, "id-a", "repo", env)
    _record_hub(worktree, "id-b", "wt-b", env)
    explicit = {"ROBOMATE_HUB_URL": "http://explicit:1", "ROBOMATE_TOKEN": "t"} | env
    for selector in ("wt-b", "id-b", str(worktree), str(worktree / "."), str(worktree)):
        hub = find_hub(repository, explicit, selector=selector)
        assert isinstance(hub, LocalHub)
        assert (hub.checkout, hub.hub_id, hub.name) == (worktree, "id-b", "wt-b")
        assert hub.endpoint() == HubEndpoint("http://wt-b:8420", "wt-b-token")
    # Without a selector the explicit URL comes first, then the owned hub.
    assert find_hub(worktree, explicit) == HubEndpoint("http://explicit:1", "t")
    for checkout, name in ((worktree, "wt-b"), (repository, "repo")):
        owned = find_hub(checkout, env)
        assert isinstance(owned, LocalHub) and owned.checkout == checkout
        assert owned.endpoint() == HubEndpoint(f"http://{name}:8420", f"{name}-token")
    with pytest.raises(DiscoveryError, match="no registered hub has that name"):
        find_hub(repository, env, selector="missing")
    plain = tmp_path / "plain"
    plain.mkdir()
    with pytest.raises(DiscoveryError, match="not inside a git repository"):
        find_hub(repository, env, selector=str(plain))
    with pytest.raises(DiscoveryError, match="no such checkout directory"):
        find_hub(repository, env, selector=str(tmp_path / "missing"))
    no_hub = tmp_path / "no-hub"
    no_hub.mkdir()
    git(no_hub, "init")
    with pytest.raises(DiscoveryError, match="no hub is recorded for checkout"):
        find_hub(repository, env, selector=str(no_hub))


def test_a_name_shared_by_two_repositories_needs_a_hub_id(repository: Path, tmp_path: Path) -> None:
    env = {"XDG_STATE_HOME": str(tmp_path / "xdg"), "LOCALAPPDATA": str(tmp_path / "local")}
    other = tmp_path / "other" / "main"
    other.mkdir(parents=True)
    git(other, "init", "-b", "main")
    _record_hub(repository, "id-a", "main", env)
    _record_hub(other, "id-b", "main", env)
    with pytest.raises(DiscoveryError, match=r"(?s)several repositories.*id-a.*id-b"):
        find_hub(repository, env, selector="main")
    hub = find_hub(repository, env, selector="id-b")
    assert isinstance(hub, LocalHub) and hub.checkout == other


@pytest.mark.parametrize("denials", [2, 3])
def test_hub_json_read_retries_transient_permission_error(
    repository: Path, monkeypatch: pytest.MonkeyPatch, denials: int
) -> None:
    # Two retries are allowed: two denials recover, a third is reported (#103).
    write_hub_json(legacy_state_dir(repository), {"port": 8420})
    monkeypatch.setattr("agent_hub_common.discovery._READ_RETRY_DELAYS_S", (0.0, 0.0))
    real_read_text = Path.read_text
    attempts = 0

    def flaky_read_text(self: Path, *args: Any, **kwargs: Any) -> str:
        nonlocal attempts
        attempts += 1
        if attempts <= denials:
            raise PermissionError(13, "Permission denied", str(self))
        return real_read_text(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", flaky_read_text)
    if denials < 3:
        assert read_hub_json(legacy_state_dir(repository)) == {"port": 8420}
    else:
        with pytest.raises(DiscoveryError, match="Permission denied"):
            read_hub_json(legacy_state_dir(repository))
    assert attempts == min(denials + 1, 3)


def test_hub_json_invalid_content_is_not_retried(
    repository: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    legacy_state_dir(repository).mkdir()
    (repository / ".robomate/hub.json").write_text("[1]")
    with pytest.raises(DiscoveryError, match="invalid hub metadata"):
        read_hub_json(legacy_state_dir(repository))
    monkeypatch.setattr("agent_hub_common.sharing.time.sleep", pytest.fail)
    for content in (b"{not json", b"\xff"):
        (repository / ".robomate/hub.json").write_bytes(content)
        with pytest.raises(DiscoveryError, match="cannot read"):
            read_hub_json(legacy_state_dir(repository))


def test_hub_json_read_survives_windows_sharing_violation(repository: Path) -> None:
    # Reproduces #103: os.replace and scanners hold hub.json with DELETE access;
    # Python's open() does not pass FILE_SHARE_DELETE, so it fails with EACCES
    # until that handle closes.
    if sys.platform != "win32":  # in the body, so mypy skips the rest off Windows
        pytest.skip("Windows share-mode semantics")
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateFileW.restype = wintypes.HANDLE
    kernel32.CreateFileW.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.LPVOID,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HANDLE,
    ]
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    delete, share_all, open_existing = 0x00010000, 0x7, 3

    write_hub_json(legacy_state_dir(repository), {"port": 8420})
    path = repository / ".robomate/hub.json"
    handle = kernel32.CreateFileW(str(path), delete, share_all, None, open_existing, 0, None)
    assert handle != wintypes.HANDLE(-1).value, ctypes.get_last_error()
    try:
        with pytest.raises(PermissionError):
            path.read_text(encoding="utf-8")
    except BaseException:
        kernel32.CloseHandle(handle)
        raise
    timer = threading.Timer(0.05, kernel32.CloseHandle, (handle,))
    timer.start()
    try:
        assert read_hub_json(legacy_state_dir(repository)) == {"port": 8420}
    finally:
        timer.join()


@pytest.mark.parametrize("denials", [2, 3])
def test_hub_json_write_retries_transient_permission_error(
    repository: Path, monkeypatch: pytest.MonkeyPatch, denials: int
) -> None:
    # Two retries are allowed: two denials recover, a third is reported (#114).
    write_hub_json(legacy_state_dir(repository), {"port": 8420})
    monkeypatch.setattr("agent_hub_common.discovery._WRITE_RETRY_DELAYS_S", (0.0, 0.0))
    real_replace = os.replace
    attempts = 0

    def flaky_replace(src: Any, dst: Any) -> None:
        nonlocal attempts
        attempts += 1
        if attempts <= denials:
            raise PermissionError(13, "Permission denied", str(dst))
        real_replace(src, dst)

    monkeypatch.setattr("agent_hub_common.discovery.os.replace", flaky_replace)
    if denials < 3:
        write_hub_json(legacy_state_dir(repository), {"port": 8421})
        assert read_hub_json(legacy_state_dir(repository)) == {"port": 8421}
    else:
        with pytest.raises(PermissionError, match="Permission denied"):
            write_hub_json(legacy_state_dir(repository), {"port": 8421})
        assert read_hub_json(legacy_state_dir(repository)) == {"port": 8420}
    assert attempts == min(denials + 1, 3)


def test_hub_json_write_survives_windows_sharing_violation(repository: Path) -> None:
    # Reproduces #114: a concurrent reader holding hub.json open with plain
    # open() (no FILE_SHARE_DELETE) makes os.replace fail with a transient
    # sharing violation until that handle closes.
    if sys.platform != "win32":  # in the body, so mypy skips the rest off Windows
        pytest.skip("Windows share-mode semantics")
    write_hub_json(legacy_state_dir(repository), {"port": 8420})
    path = repository / ".robomate/hub.json"
    holder = path.open(encoding="utf-8")
    try:
        timer = threading.Timer(0.05, holder.close)
        timer.start()
        try:
            write_hub_json(legacy_state_dir(repository), {"port": 8421})
        finally:
            timer.join()
            if not holder.closed:
                holder.close()
    except BaseException:
        if not holder.closed:
            holder.close()
        raise
    assert read_hub_json(legacy_state_dir(repository)) == {"port": 8421}


def test_discover_waits_for_a_busy_hub_and_names_a_silent_one(
    repository: Path,
    healthz: Callable[[str, float], str],
    silent_url: Callable[[], str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#230: a hub slower than the old 0.5 s check is used; a silent one is named as such."""

    legacy_state_dir(repository).mkdir()
    (legacy_state_dir(repository) / "token").write_text("repo-token\n")
    slow = healthz("expected-hub", 1.0)
    write_hub_json(
        legacy_state_dir(repository), {"url": slow, "hub_id": "expected-hub", "name": "repo"}
    )
    assert discover(repository, {}) == HubEndpoint(slow, "repo-token")

    monkeypatch.setattr("agent_hub_common.registry.HEALTHZ_TIMEOUT_S", 0.2)
    monkeypatch.setattr("agent_hub_common.registry.HEALTHZ_LIVE_PID_TIMEOUT_S", 0.4)
    silent = silent_url()
    write_hub_json(
        legacy_state_dir(repository), {"url": silent, "hub_id": "expected-hub", "name": "repo"}
    )
    with pytest.raises(
        DiscoveryError,
        match=r"at .* did not answer /healthz within 0.2 s; stale state remains in",
    ):
        discover(repository, {})
    monkeypatch.setattr("agent_hub_common.registry.process_alive", lambda pid: pid == 4321)
    write_hub_json(
        legacy_state_dir(repository), {"url": silent, "hub_id": "expected-hub", "pid": 4321}
    )
    with pytest.raises(
        DiscoveryError, match="did not answer /healthz within 0.4 s; recorded pid 4321 is visible"
    ):
        discover(repository, {})
    # A slow answer from the wrong hub is still refused.
    other = healthz("different-hub", 1.0)
    write_hub_json(
        legacy_state_dir(repository), {"url": other, "hub_id": "expected-hub", "pid": 4321}
    )
    monkeypatch.setattr("agent_hub_common.registry.HEALTHZ_LIVE_PID_TIMEOUT_S", 10.0)
    with pytest.raises(DiscoveryError, match="answers as hub different-hub, not expected-hub"):
        discover(repository, {})


def test_identity_without_address_reports_never_finished_starting(tmp_path: Path) -> None:
    """#301 r1-3: a startup that failed before serving resolves its partial record."""

    root = init_repository(tmp_path / "repo")
    write_hub_json(
        legacy_state_dir(root), {"hub_id": "partial-id", "name": "repo", "checkout": str(root)}
    )
    hub = local_hub(root)
    assert isinstance(hub, LocalHub)
    assert (hub.hub_id, hub.url) == ("partial-id", "")
    with pytest.raises(DiscoveryError, match="never finished starting"):
        verify_local_hub(hub)
    with pytest.raises(DiscoveryError, match="stale partial state remains in"):
        verify_local_hub(hub)


def test_git_timeout_is_not_masked_by_resolve_checkout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#301 r1-4: a hung `git` child reports its timeout, not a wrong location."""

    def hung(*args: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
        raise subprocess.TimeoutExpired(["git", "rev-parse"], 10)

    monkeypatch.setattr(subprocess, "run", hung)
    with pytest.raises(GitTimeoutError, match="timed out after 10"):
        resolve_checkout(tmp_path)


def test_git_timeout_is_not_masked_as_unset_origin_head(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#301 r1-4: a timeout reading origin/HEAD is not reported as an unset ref."""

    root = init_repository(tmp_path / "repo")
    real_run = subprocess.run

    def selective(args: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        if "symbolic-ref" in args:
            raise subprocess.TimeoutExpired(args, 10)
        return real_run(args, **kwargs)

    monkeypatch.setattr(subprocess, "run", selective)
    with pytest.raises(GitTimeoutError, match="timed out"):
        resolve_repository(root)


# -- Step 6 of M2: where state and token live, bindings, the worktree root ------


@pytest.fixture
def layers(tmp_path: Path) -> dict[str, str]:
    """An environment whose user configuration is empty and private to the test."""

    return {
        "XDG_CONFIG_HOME": str(tmp_path / "xdg-config"),
        "APPDATA": str(tmp_path / "appdata"),
        "XDG_STATE_HOME": str(tmp_path / "xdg-state"),
        "LOCALAPPDATA": str(tmp_path / "local"),
    }


def _write_user_config(env: dict[str, str], text: str) -> Path:
    base = Path(env["APPDATA"] if sys.platform == "win32" else env["XDG_CONFIG_HOME"])
    path = base / "robomate" / "config.toml"
    path.parent.mkdir(parents=True)
    path.write_text(text, encoding="utf-8")
    return path


def test_resolver_takes_a_binding_or_a_registry_entry(tmp_path: Path) -> None:
    state, token = tmp_path / "ns" / "state", tmp_path / "ns" / "token" / "token"
    binding = Binding("owner", "hid", "ns", tmp_path / "co", state, token)
    assert (state_dir(binding), token_file(binding)) == (state, token)
    entry = {"hub_id": "hid", "checkout": str(tmp_path / "co"), "state_dir": str(state)}
    # An entry written before the token had a directory of its own names none.
    assert (state_dir(entry), token_file(entry)) == (state, state / "token")
    assert token_file(entry | {"token_file": str(token)}) == token
    # A record that names no location is a legacy hub, run in place in its checkout.
    for legacy in ({"checkout": str(tmp_path / "co")}, {"repo_root": str(tmp_path / "co")}):
        assert state_dir(legacy) == legacy_state_dir(tmp_path / "co")
        assert is_legacy_state_dir(state_dir(legacy))
        assert token_file(legacy) == legacy_state_dir(tmp_path / "co") / "token"
    assert not is_legacy_state_dir(state)
    with pytest.raises(DiscoveryError, match="neither a state location nor a checkout"):
        state_dir({"hub_id": "hid"})


def test_binding_is_recorded_in_the_checkouts_own_git_directory_without_a_token(
    repository: Path, tmp_path: Path
) -> None:
    worktree = tmp_path / "linked"
    git(repository, "worktree", "add", "-b", "linked", str(worktree))
    main_git, linked_git = checkout_git_dir(repository), checkout_git_dir(worktree)
    assert main_git == repository / ".git"
    assert linked_git.parent == repository / ".git" / "worktrees"
    state, token = tmp_path / "ns" / "state", tmp_path / "ns" / "token" / "token"
    owner = Binding("owner", "hid", "repo", repository, state, token)
    agent = Binding("agent", "hid", "repo", repository, state, token, agent="bob")
    assert read_binding(main_git) is None
    write_binding(main_git, owner)
    write_binding(linked_git, agent)
    assert read_binding(main_git) == owner and read_binding(linked_git) == agent
    token.parent.mkdir(parents=True)
    token.write_text("secret-token\n")
    for git_dir in (main_git, linked_git):
        text = (git_dir / BINDING_FILENAME).read_text(encoding="utf-8")
        assert "secret-token" not in text
        assert json.loads(text)["token_file"] == str(token)
    # A binding lives and dies with its worktree (spec §4).
    git(repository, "worktree", "remove", "--force", str(worktree))
    assert not linked_git.exists()
    assert read_binding(main_git) == owner


@pytest.mark.parametrize(
    ("change", "message"),
    [
        ({"role": "boss"}, "needs a role and a hub_id"),
        ({"hub_id": ""}, "needs a role and a hub_id"),
        ({"agent": "bob"}, "only an agent binding names an agent"),
        ({"state_dir": "relative/state"}, "state_dir must be an absolute path"),
        ({"version": 99}, "newer robomate"),
        ({"version": None}, "no version"),
    ],
)
def test_malformed_binding_is_refused_naming_the_file(
    repository: Path, tmp_path: Path, change: dict[str, Any], message: str
) -> None:
    git_dir = checkout_git_dir(repository)
    write_binding(
        git_dir,
        Binding("owner", "hid", "repo", repository, tmp_path / "s", tmp_path / "t"),
    )
    value = json.loads((git_dir / BINDING_FILENAME).read_text(encoding="utf-8")) | change
    (git_dir / BINDING_FILENAME).write_text(json.dumps(value), encoding="utf-8")
    with pytest.raises(DiscoveryError, match=message):
        read_binding(git_dir)
    (git_dir / BINDING_FILENAME).write_text("[1, 2]", encoding="utf-8")
    with pytest.raises(DiscoveryError, match="invalid binding"):
        read_binding(git_dir)


def test_a_new_hub_is_placed_beside_its_agent_worktrees(
    repository: Path, tmp_path: Path, layers: dict[str, str]
) -> None:
    repo = resolve_repository(repository)
    root = tmp_path / "repo.robomate"
    placement = plan_hub_placement(repo, None, layers)
    assert placement.new and not placement.legacy and placement.name == "repo"
    assert placement.state_dir == root / "repo" / "state"
    assert placement.token_file == root / "repo" / "token" / "token"
    assert not root.exists()  # planning creates nothing
    assert plan_hub_placement(repo, "feature", layers).state_dir == root / "feature" / "state"
    with pytest.raises(DiscoveryError, match="invalid hub name"):
        plan_hub_placement(repo, "Not.Valid", layers)
    with pytest.raises(DiscoveryError, match="invalid hub name"):
        plan_hub_placement(repo, "state", layers)

    create_hub_dirs(placement)
    record = json.loads((root / ROOT_RECORD_FILENAME).read_text(encoding="utf-8"))
    assert record == {"git_common_dir": str(repository / ".git")}
    assert placement.state_dir.is_dir() and placement.token_file.parent.is_dir()
    if sys.platform != "win32":
        # The token has a directory of its own, and state holds none (spec §12).
        assert placement.state_dir.stat().st_mode & 0o777 == 0o700
        assert placement.token_file.parent.stat().st_mode & 0o777 == 0o700
    assert not (placement.state_dir / "token").exists()
    assert placement.token_file.parent != placement.state_dir


def test_a_configured_worktree_root_places_the_hub(
    repository: Path, tmp_path: Path, layers: dict[str, str]
) -> None:
    elsewhere = tmp_path / "elsewhere"
    _write_user_config(layers, f"worktree_root = {json.dumps(str(elsewhere))}\n")
    placement = plan_hub_placement(resolve_repository(repository), None, layers)
    assert placement.state_dir == elsewhere / "repo.robomate" / "repo" / "state"


def test_a_root_created_by_one_repository_is_refused_for_another_of_the_same_name(
    tmp_path: Path, layers: dict[str, str]
) -> None:
    """Two repositories called `repo` derive one `<repo>.robomate` under a shared parent."""

    shared = tmp_path / "shared"
    _write_user_config(layers, f"worktree_root = {json.dumps(str(shared))}\n")
    repo_a = init_repository(tmp_path / "a" / "repo")
    repo_b = init_repository(tmp_path / "b" / "repo")
    first = plan_hub_placement(resolve_repository(repo_a), None, layers)
    create_hub_dirs(first)
    assert first.state_dir == shared / "repo.robomate" / "repo" / "state"
    with pytest.raises(DiscoveryError) as refused:
        plan_hub_placement(resolve_repository(repo_b), "other", layers)
    message = str(refused.value)
    assert f"belongs to the repository {repo_a / '.git'}" in message
    assert f"not {repo_b / '.git'}" in message
    assert str(repo_b / ".git" / "robomate" / "config.toml") in message
    # A repository-level worktree_root is the way out (spec §4).
    (repo_b / ".git" / "robomate").mkdir()
    (repo_b / ".git" / "robomate" / "config.toml").write_text(
        f"worktree_root = {json.dumps(str(tmp_path / 'b-root'))}\n", encoding="utf-8"
    )
    chosen = plan_hub_placement(resolve_repository(repo_b), None, layers)
    assert chosen.state_dir == tmp_path / "b-root" / "repo" / "state"
    # The same repository may add a hub of its own beside the first.
    sibling = tmp_path / "a" / "sibling"
    git(repo_a, "worktree", "add", "-b", "sibling", str(sibling))
    assert plan_hub_placement(resolve_repository(sibling), None, layers).new


def test_a_directory_that_holds_other_things_is_not_adopted_as_a_worktree_root(
    repository: Path, tmp_path: Path, layers: dict[str, str]
) -> None:
    root = tmp_path / "repo.robomate"
    root.mkdir()
    (root / "notes.txt").write_text("not robomate's\n")
    with pytest.raises(DiscoveryError, match=f"holds files but no {ROOT_RECORD_FILENAME}"):
        plan_hub_placement(resolve_repository(repository), None, layers)
    (root / "notes.txt").unlink()  # an empty directory is
    assert plan_hub_placement(resolve_repository(repository), None, layers).new


def test_a_taken_name_is_refused_but_an_abandoned_start_is_not(
    repository: Path, tmp_path: Path, layers: dict[str, str]
) -> None:
    repo = resolve_repository(repository)
    placement = plan_hub_placement(repo, None, layers)
    create_hub_dirs(placement)
    (placement.state_dir / "up.lock").write_text("0")  # a start that stopped before hub.json
    assert plan_hub_placement(repo, None, layers).new
    write_hub_json(placement.state_dir, {"hub_id": "other", "name": "repo"})
    with pytest.raises(DiscoveryError, match=r"hub name 'repo' is taken: .*--name"):
        plan_hub_placement(repo, None, layers)


def test_up_follows_the_owner_binding_wherever_state_is(
    repository: Path, tmp_path: Path, layers: dict[str, str]
) -> None:
    state, token = tmp_path / "ns" / "state", tmp_path / "ns" / "token" / "token"
    state.mkdir(parents=True)
    write_binding(
        checkout_git_dir(repository),
        Binding("owner", "hid", "chosen", repository, state, token),
    )
    placement = plan_hub_placement(resolve_repository(repository), "ignored", layers)
    assert (placement.state_dir, placement.token_file, placement.new) == (state, token, False)
    # A binding to state that is gone is not a licence to start another hub.
    state.rmdir()
    with pytest.raises(DiscoveryError, match=r"bound to hub chosen \(hub_id hid\).*is missing"):
        plan_hub_placement(resolve_repository(repository), None, layers)


def test_a_legacy_hub_is_placed_in_its_own_checkout(
    repository: Path, layers: dict[str, str]
) -> None:
    repo = resolve_repository(repository)
    for filename in ("hub.json", "hub.db", "config.toml"):
        legacy_state_dir(repository).mkdir(exist_ok=True)
        (legacy_state_dir(repository) / filename).write_text("{}")
        placement = plan_hub_placement(repo, None, layers)
        assert placement.legacy and not placement.new
        assert placement.token_file == legacy_state_dir(repository) / "token"
        (legacy_state_dir(repository) / filename).unlink()
    assert plan_hub_placement(repo, None, layers).new


def test_up_is_refused_in_a_checkout_bound_as_an_agent(
    repository: Path, tmp_path: Path, layers: dict[str, str]
) -> None:
    worktree = tmp_path / "agent-bob"
    git(repository, "worktree", "add", "-b", "agent-bob", str(worktree))
    write_binding(
        checkout_git_dir(worktree),
        Binding("agent", "hid", "team", repository, tmp_path / "s", tmp_path / "t", agent="bob"),
    )
    with pytest.raises(DiscoveryError) as refused:
        plan_hub_placement(resolve_repository(worktree), None, layers)
    message = str(refused.value)
    assert "bound to hub team as agent 'bob'" in message and str(repository) in message


def _bound_hub(tmp_path: Path, owner: Path) -> tuple[Path, Path]:
    state, token = tmp_path / "ns" / "state", tmp_path / "ns" / "token" / "token"
    write_hub_json(state, {"hub_id": "hid", "name": "team", "url": "http://team:8420"})
    token.parent.mkdir(parents=True)
    token.write_text("team-token\n")
    return state, token


def test_discovery_follows_an_agent_binding_to_the_hub_and_its_token(
    repository: Path, tmp_path: Path, layers: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Rule 2: a worktree bound as an agent reaches the owner's hub, and nothing else."""

    worktree = tmp_path / "agent-bob"
    git(repository, "worktree", "add", "-b", "agent-bob", str(worktree))
    stranger = tmp_path / "stranger"
    git(repository, "worktree", "add", "-b", "stranger", str(stranger))
    state, token = _bound_hub(tmp_path, repository)
    write_binding(
        checkout_git_dir(worktree),
        Binding("agent", "hid", "team", repository, state, token, agent="bob"),
    )
    nested = worktree / "src"
    nested.mkdir()
    for cwd in (worktree, nested):
        hub = find_hub(cwd, layers)
        assert isinstance(hub, LocalHub)
        assert (hub.checkout, hub.state_dir, hub.token_file) == (repository, state, token)
        assert hub.endpoint() == HubEndpoint("http://team:8420", "team-token")
    # A worktree with no binding reaches nothing, however many bound ones are beside it.
    # That error lists the live hubs, so it reads the machine registry: only the
    # test's own (#243 r1-2), never the operator's.
    locked: list[Path] = []
    real_locked = registry._locked

    def recording(path: Path) -> Any:
        locked.append(path)
        return real_locked(path)

    monkeypatch.setattr(registry, "_locked", recording)
    with pytest.raises(DiscoveryError, match="no hub is recorded for checkout"):
        find_hub(stranger, layers)
    assert locked and all(path.is_relative_to(tmp_path) for path in locked)


def test_a_binding_to_a_missing_hub_names_it_and_never_falls_through(
    repository: Path, tmp_path: Path, layers: dict[str, str]
) -> None:
    state, token = _bound_hub(tmp_path, repository)
    write_binding(
        checkout_git_dir(repository),
        Binding("owner", "hid", "team", repository, state, token),
    )
    # The checkout also holds legacy state of another hub: the binding still wins.
    write_hub_json(legacy_state_dir(repository), {"hub_id": "legacy", "url": "http://legacy:1"})
    bound = find_hub(repository, layers)
    assert isinstance(bound, LocalHub) and bound.url == "http://team:8420"
    # Bound, but its hub.json is not there (a start in progress): found, and not running.
    (state / "hub.json").unlink()
    starting = find_hub(repository, layers)
    assert isinstance(starting, LocalHub) and (starting.hub_id, starting.url) == ("hid", "")
    with pytest.raises(DiscoveryError, match=r"hub team \(checkout [^)]*\) is not running"):
        verify_local_hub(starting)
    shutil.rmtree(state)
    with pytest.raises(DiscoveryError) as missing:
        find_hub(repository, layers)
    message = str(missing.value)
    assert "hub team (hub_id hid)" in message and f"owned by checkout {repository}" in message
    assert str(state) in message
    # Another hub's state at that location is not the bound hub either.
    write_hub_json(state, {"hub_id": "other", "url": "http://other:1"})
    with pytest.raises(DiscoveryError, match="belongs to another hub"):
        find_hub(repository, layers)


def test_a_stopped_bound_hub_is_an_error_naming_it_and_its_checkout(
    repository: Path, tmp_path: Path, layers: dict[str, str]
) -> None:
    state, token = _bound_hub(tmp_path, repository)
    write_hub_json(state, {"hub_id": "hid", "name": "team", "url": closed_port_url()})
    worktree = tmp_path / "agent-bob"
    git(repository, "worktree", "add", "-b", "agent-bob", str(worktree))
    write_binding(
        checkout_git_dir(worktree),
        Binding("agent", "hid", "team", repository, state, token, agent="bob"),
    )
    hub = find_hub(worktree, layers)
    assert isinstance(hub, LocalHub)
    with pytest.raises(
        DiscoveryError, match=rf"hub team \(checkout {re.escape(str(repository))}\)"
    ):
        verify_local_hub(hub)


def test_a_relocated_hub_is_selected_by_name_after_its_checkout_is_gone(
    repository: Path, tmp_path: Path, layers: dict[str, str]
) -> None:
    worktree = tmp_path / "wt"
    git(repository, "worktree", "add", "-b", "wt", str(worktree))
    state, token = _bound_hub(tmp_path, worktree)
    register(
        {
            "hub_id": "hid",
            "name": "team",
            "checkout": str(worktree),
            "git_common_dir": str(repository / ".git"),
            "state_dir": str(state),
            "token_file": str(token),
            "url": "http://team:8420",
            "pid": None,
        },
        layers,
    )
    git(repository, "worktree", "remove", "--force", str(worktree))
    hub = find_hub(repository, layers, selector="team")
    assert isinstance(hub, LocalHub)
    assert (hub.checkout, hub.state_dir, hub.hub_id) == (worktree, state, "hid")
    # The main checkout owns no hub and does not attach to it.
    with pytest.raises(DiscoveryError, match="no hub is recorded for checkout"):
        find_hub(repository, layers)
    # Once its state is gone the entry is pruned (spec §4), and nothing is followed.
    (state / "hub.json").unlink()
    with pytest.raises(DiscoveryError, match="no registered hub has that name or hub_id"):
        find_hub(repository, layers, selector="hid")


def test_hub_bindings_finds_the_checkouts_bound_to_one_hub(
    repository: Path, tmp_path: Path
) -> None:
    state, token = tmp_path / "s", tmp_path / "t"
    bound, other, unbound = (tmp_path / name for name in ("bound", "other", "unbound"))
    for path in (bound, other, unbound):
        git(repository, "worktree", "add", "-b", path.name, str(path))
    write_binding(
        checkout_git_dir(repository), Binding("owner", "hid", "team", repository, state, token)
    )
    write_binding(
        checkout_git_dir(bound), Binding("agent", "hid", "team", repository, state, token, "bob")
    )
    write_binding(
        checkout_git_dir(other), Binding("agent", "elsewhere", "x", repository, state, token, "al")
    )
    found = hub_bindings(repository / ".git", "hid")
    assert sorted(binding.role for _, binding in found) == ["agent", "owner"]
    assert {git_dir for git_dir, _ in found} == {
        checkout_git_dir(repository),
        checkout_git_dir(bound),
    }


def test_no_module_outside_discovery_names_the_legacy_state_directory() -> None:
    """Every reader goes through the resolver, so only discovery.py spells `.robomate`.

    The name of a path component, not the `<repo>.robomate` worktree-root suffix
    or an identifier such as `mcp_servers.robomate`.
    """

    root = Path(__file__).resolve().parents[1]
    modules = [
        *root.glob("packages/*/src/**/*.py"),
        *(
            path
            for path in root.glob("scripts/**/*.py")
            if "poc" not in path.relative_to(root).parts
        ),
    ]
    assert len(modules) > 30
    named = re.compile(r"(?<![\w}>])\.robomate\b")
    offenders = [
        f"{path.relative_to(root)}:{number}"
        for path in modules
        if path.name != "discovery.py"
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1)
        if named.search(line)
    ]
    assert offenders == []


# -- #327: concurrent worktree-root ownership publication ---------------------


def test_empty_root_record_is_retried_then_refused(
    repository: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#327 signature 1: an empty record is a transient publish, retried, then refused."""

    root = tmp_path / "repo.robomate"
    root.mkdir()
    (root / ROOT_RECORD_FILENAME).write_text("", encoding="utf-8")
    # Two retries allowed (0.05 s + 0.1 s): a third read still empty raises.
    monkeypatch.setattr("agent_hub_common.discovery.time.sleep", lambda s: None)
    with pytest.raises(DiscoveryError, match="cannot read"):
        read_root_record(root)
    # A non-empty invalid record is refused at once, without sleeping.
    (root / ROOT_RECORD_FILENAME).write_text("{not json", encoding="utf-8")
    sleeps: list[float] = []
    monkeypatch.setattr("agent_hub_common.discovery.time.sleep", lambda s: sleeps.append(s))
    with pytest.raises(DiscoveryError, match="cannot read"):
        read_root_record(root)
    assert sleeps == []


def test_partially_written_root_record_is_refused(
    repository: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#327: a truncated non-empty record is corrupt, not transient: no retry."""

    root = tmp_path / "repo.robomate"
    root.mkdir()
    (root / ROOT_RECORD_FILENAME).write_text('{"git_common_dir":', encoding="utf-8")
    monkeypatch.setattr(
        "agent_hub_common.discovery.time.sleep", lambda s: pytest.fail("must not retry")
    )
    with pytest.raises(DiscoveryError, match="cannot read"):
        read_root_record(root)


def test_concurrent_claims_for_same_repository_all_succeed(
    repository: Path, tmp_path: Path
) -> None:
    """#327 signatures 1-2: racing first `up`s publish one identical record."""

    import threading

    common = repository / ".git"
    root = tmp_path / "repo.robomate"
    barrier = threading.Barrier(8)
    errors: list[BaseException] = []

    def claim() -> None:
        try:
            barrier.wait(timeout=10)
            claim_worktree_root(root, common)
        except BaseException as exc:  # noqa: BLE001 - collected and re-raised below
            errors.append(exc)

    threads = [threading.Thread(target=claim) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)
    assert not [t for t in threads if t.is_alive()]
    assert errors == []
    assert check_worktree_root(root, common) is True
    record = read_root_record(root)
    assert record == {"git_common_dir": str(common)}
    # No temp files leak into the root (they would trip the unrelated-nonempty
    # refusal for a concurrent reader) nor beside it.
    assert [p.name for p in root.iterdir()] == [ROOT_RECORD_FILENAME]
    assert list(root.parent.glob(".robomate-root-*")) == []


def test_concurrent_readers_never_observe_an_empty_record(repository: Path, tmp_path: Path) -> None:
    """#327 signature 1: publication is atomic; readers see missing or complete."""

    import threading

    common = repository / ".git"
    root = tmp_path / "repo.robomate"
    stop = threading.Event()
    observed: list[object] = []
    failures: list[BaseException] = []

    def reader() -> None:
        while not stop.is_set():
            try:
                value = read_root_record(root)
                observed.append(value is not None)
            except DiscoveryError as exc:
                # Only a foreign record may raise here; an empty file must not
                # surface as "cannot read ... Expecting value".
                if "Expecting value" in str(exc):
                    failures.append(exc)
                    stop.set()
                    return
            except FileNotFoundError:
                pass

    readers = [threading.Thread(target=reader, daemon=True) for _ in range(4)]
    for thread in readers:
        thread.start()
    try:
        for _ in range(20):
            # Fresh root each round to re-exercise the publish window.
            import shutil

            shutil.rmtree(root, ignore_errors=True)
            claim_worktree_root(root, common)
            assert read_root_record(root) == {"git_common_dir": str(common)}
    finally:
        stop.set()
    for thread in readers:
        thread.join(timeout=10)
    assert failures == []


def test_concurrent_claims_for_different_repositories_refuse_foreign(
    tmp_path: Path,
) -> None:
    """#327: two repos sharing one `<name>.robomate` path still refuse each other."""

    import threading

    repo_a = init_repository(tmp_path / "a" / "repo")
    repo_b = init_repository(tmp_path / "b" / "repo")
    common_a, common_b = repo_a / ".git", repo_b / ".git"
    # Both derive `<shared>/repo.robomate` when the user config points there.
    root = tmp_path / "shared" / "repo.robomate"
    barrier = threading.Barrier(2)
    outcomes: dict[str, str] = {}

    def claim(name: str, common: Path) -> None:
        barrier.wait(timeout=10)
        try:
            claim_worktree_root(root, common)
            outcomes[name] = "ok"
        except DiscoveryError as exc:
            outcomes[name] = str(exc)

    first = threading.Thread(target=claim, args=("a", common_a))
    second = threading.Thread(target=claim, args=("b", common_b))
    first.start()
    second.start()
    first.join(timeout=30)
    second.join(timeout=30)
    assert sorted(outcomes) == ["a", "b"]
    assert sorted(outcomes.values()) != ["ok", "ok"]
    winner = "a" if outcomes["a"] == "ok" else "b"
    loser = "b" if winner == "a" else "a"
    assert "belongs to the repository" in outcomes[loser]
    # The winner's record survives intact.
    expected = common_a if winner == "a" else common_b
    assert read_root_record(root) == {"git_common_dir": str(expected)}


def test_unrelated_nonempty_root_and_foreign_record_are_still_refused(
    repository: Path, tmp_path: Path
) -> None:
    """#327: atomic publication preserves both refusals."""

    common = repository / ".git"
    root = tmp_path / "repo.robomate"
    root.mkdir()
    (root / "notes.txt").write_text("not robomate's\n")
    with pytest.raises(DiscoveryError, match=f"holds files but no {ROOT_RECORD_FILENAME}"):
        check_worktree_root(root, common)
    with pytest.raises(DiscoveryError, match=f"holds files but no {ROOT_RECORD_FILENAME}"):
        claim_worktree_root(root, common)
    (root / "notes.txt").unlink()
    other = tmp_path / "other"
    other.mkdir()
    (root / ROOT_RECORD_FILENAME).write_text(
        json.dumps({"git_common_dir": str(other)}), encoding="utf-8"
    )
    with pytest.raises(DiscoveryError, match="belongs to the repository"):
        check_worktree_root(root, common)


def test_check_reconciles_record_published_between_read_and_iterdir(
    repository: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#327 r1-1: a same-repo publish between read(None) and iterdir must not refuse."""

    from unittest.mock import patch as mock_patch

    import agent_hub_common.discovery as d

    common = repository / ".git"
    root = tmp_path / "repo.robomate"
    root.mkdir()
    original_read = d.read_root_record

    def interleaved_read(path: Path) -> dict[str, Any] | None:
        record = original_read(path)
        if record is None:
            # Another legitimate claimant publishes after this caller's read,
            # before its directory enumeration.
            with mock_patch.object(d, "read_root_record", original_read):
                d.claim_worktree_root(root, common)
        return record

    monkeypatch.setattr(d, "read_root_record", interleaved_read)
    # Without reconciliation this raises the unrelated-root refusal while the
    # final record is valid and same-repo; with it, the claim succeeds.
    d.claim_worktree_root(root, common)
    assert original_read(root) == {"git_common_dir": str(common)}
    assert d.check_worktree_root(root, common) is True


def test_claim_fails_explicitly_without_hardlink_support(
    repository: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#327 r1-2: no silent non-atomic fallback; explicit failure, nothing partial."""

    import agent_hub_common.discovery as d

    common = repository / ".git"
    root = tmp_path / "repo.robomate"

    def no_link(src: object, dst: object) -> None:
        raise OSError(errno.EOPNOTSUPP, "no links")

    monkeypatch.setattr("agent_hub_common.discovery.os.link", no_link)
    with pytest.raises(DiscoveryError, match="hard links are not supported"):
        d.claim_worktree_root(root, common)
    # Nothing partial is left behind: readers see missing, not empty/corrupt,
    # and the empty root is claimable (False), not an unrelated refusal.
    assert d.read_root_record(root) is None
    assert d.check_worktree_root(root, common) is False
    assert [p.name for p in root.iterdir()] == []
    assert list(root.parent.glob(".robomate-root-*")) == []
