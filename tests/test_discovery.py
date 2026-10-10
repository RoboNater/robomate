"""Repository discovery and atomic local metadata."""

import json
import os
import subprocess
import sys
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from agent_hub_common.discovery import (
    DiscoveryError,
    HubEndpoint,
    LocalHub,
    derive_hub_name,
    discover,
    ensure_excluded,
    find_hub,
    is_linked_worktree,
    read_hub_json,
    repository_dir,
    resolve_checkout,
    resolve_repository,
    state_dir,
    token_file,
    validate_hub_name,
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
    assert state_dir(worktree) != state_dir(repository)


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
    write_hub_json(repository, {"port": 8420})
    assert read_hub_json(repository) == {"port": 8420}

    def fail_replace(*args: object) -> None:
        raise OSError("injected replace failure")

    monkeypatch.setattr("agent_hub_common.discovery.os.replace", fail_replace)
    with pytest.raises(OSError, match="injected"):
        write_hub_json(repository, {"port": 8421})
    assert json.loads((repository / ".robomate/hub.json").read_text()) == {"port": 8420}
    assert sorted(p.name for p in (repository / ".robomate").iterdir()) == ["hub.json"]


def test_discovery_prefers_explicit_then_repo(
    repository: Path, healthz: Callable[[str], str]
) -> None:
    url = healthz("repo-id")
    write_hub_json(repository, {"url": url, "hub_id": "repo-id"})
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

    (repository / ".robomate").mkdir()
    token_file(repository).write_text("repo-token\n")
    matching = healthz("expected-hub")
    write_hub_json(repository, {"url": matching, "hub_id": "expected-hub", "name": "repo"})
    assert discover(repository, {}) == HubEndpoint(matching, "repo-token")

    other = healthz("different-hub")
    write_hub_json(repository, {"url": other, "hub_id": "expected-hub", "name": "repo"})
    with pytest.raises(DiscoveryError, match="answers as hub different-hub, not expected-hub"):
        discover(repository, {})

    stopped = closed_port_url()
    write_hub_json(
        repository, {"url": stopped, "hub_id": "expected-hub", "name": "repo", "pid": None}
    )
    with pytest.raises(DiscoveryError, match=r"hub repo \(checkout .*\) at .* is not running"):
        discover(repository, {})
    monkeypatch.setattr("agent_hub_common.registry.process_alive", lambda pid: pid == 4321)
    write_hub_json(repository, {"url": stopped, "hub_id": "expected-hub", "pid": 4321})
    with pytest.raises(DiscoveryError, match="unreachable; recorded pid 4321 is visible"):
        discover(repository, {})
    # An explicit URL is taken as given: it may name a remote hub.
    explicit = {"ROBOMATE_HUB_URL": other, "ROBOMATE_TOKEN": "t"}
    assert discover(repository, explicit) == HubEndpoint(other, "t")


def _record_hub(checkout: Path, hub_id: str, name: str, env: dict[str, str]) -> None:
    common = resolve_checkout(checkout)[1]
    write_hub_json(checkout, {"url": f"http://{name}:8420", "hub_id": hub_id, "name": name})
    token_file(checkout).write_text(f"{name}-token\n")
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
    write_hub_json(repository, {"port": 8420})
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
        assert read_hub_json(repository) == {"port": 8420}
    else:
        with pytest.raises(DiscoveryError, match="Permission denied"):
            read_hub_json(repository)
    assert attempts == min(denials + 1, 3)


def test_hub_json_invalid_content_is_not_retried(
    repository: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (repository / ".robomate").mkdir()
    (repository / ".robomate/hub.json").write_text("[1]")
    with pytest.raises(DiscoveryError, match="invalid hub metadata"):
        read_hub_json(repository)
    monkeypatch.setattr("agent_hub_common.discovery.time.sleep", pytest.fail)
    for content in (b"{not json", b"\xff"):
        (repository / ".robomate/hub.json").write_bytes(content)
        with pytest.raises(DiscoveryError, match="cannot read"):
            read_hub_json(repository)


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

    write_hub_json(repository, {"port": 8420})
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
        assert read_hub_json(repository) == {"port": 8420}
    finally:
        timer.join()


@pytest.mark.parametrize("denials", [2, 3])
def test_hub_json_write_retries_transient_permission_error(
    repository: Path, monkeypatch: pytest.MonkeyPatch, denials: int
) -> None:
    # Two retries are allowed: two denials recover, a third is reported (#114).
    write_hub_json(repository, {"port": 8420})
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
        write_hub_json(repository, {"port": 8421})
        assert read_hub_json(repository) == {"port": 8421}
    else:
        with pytest.raises(PermissionError, match="Permission denied"):
            write_hub_json(repository, {"port": 8421})
        assert read_hub_json(repository) == {"port": 8420}
    assert attempts == min(denials + 1, 3)


def test_hub_json_write_survives_windows_sharing_violation(repository: Path) -> None:
    # Reproduces #114: a concurrent reader holding hub.json open with plain
    # open() (no FILE_SHARE_DELETE) makes os.replace fail with a transient
    # sharing violation until that handle closes.
    if sys.platform != "win32":  # in the body, so mypy skips the rest off Windows
        pytest.skip("Windows share-mode semantics")
    write_hub_json(repository, {"port": 8420})
    path = repository / ".robomate/hub.json"
    holder = path.open(encoding="utf-8")
    try:
        timer = threading.Timer(0.05, holder.close)
        timer.start()
        try:
            write_hub_json(repository, {"port": 8421})
        finally:
            timer.join()
            if not holder.closed:
                holder.close()
    except BaseException:
        if not holder.closed:
            holder.close()
        raise
    assert read_hub_json(repository) == {"port": 8421}
