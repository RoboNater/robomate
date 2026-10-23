"""Checkout, hub-state and hub discovery for the operator CLI and the MCP bridge.

A hub belongs to the checkout where `robomate up` runs (spec §4): the main
checkout or a linked worktree. Every consumer finds a hub's state through
`state_dir()` and `token_file()`, which take the checkout's binding or the
machine registry's entry, so where state lives is a decision made in this
module only: beside the hub's agent worktrees for a new hub, in the owning
checkout for a legacy hub that has not been relocated.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
import time
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlparse

from .sharing import SHARING_RETRY_DELAYS_S, retry_sharing_violation


class DiscoveryError(RuntimeError):
    """A repository or hub cannot be identified unambiguously."""


class GitTimeoutError(DiscoveryError):
    """A `git` child hung past its deadline instead of answering (#301).

    Raised instead of a masking message (e.g. "not inside a git repository"
    or "origin/HEAD is unset") so the cause and command stay visible.
    """


@dataclass(frozen=True)
class Repository:
    root: Path
    """The owning checkout: `git rev-parse --show-toplevel`."""
    git_common_dir: Path
    origin: str
    default_branch: str
    forge: str


@dataclass(frozen=True)
class HubEndpoint:
    url: str
    token: str


# `git` should answer at once: it only reads local config and refs here, never
# the network. A hung child (a credential helper waiting on input, a stale
# index lock) must fail fast with the command named (#301), not hang `up`.
_GIT_TIMEOUT_S = 10.0


def _git(cwd: Path, *args: str) -> str:
    # stdin=DEVNULL: `robomate mcp` reaches this with stdin owned by MCP. A
    # child inheriting that pipe on Windows blocks until the next message (#65).
    # Git prints paths as UTF-8 whatever the locale, which is CP1252 on Windows
    # without Python's UTF-8 mode (#166). Only stdout is decoded.
    try:
        proc = subprocess.run(
            ["git", *args],
            cwd=cwd,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            timeout=_GIT_TIMEOUT_S,
        )
    except subprocess.TimeoutExpired as exc:
        raise GitTimeoutError(
            f"git {' '.join(args)} timed out after {_GIT_TIMEOUT_S:g} s; "
            "check the repository and try again (#301)"
        ) from exc
    except OSError as exc:
        raise DiscoveryError(f"cannot resolve repository with git {' '.join(args)}: {exc}") from exc
    if proc.returncode != 0:
        detail = proc.stderr.decode("utf-8", errors="replace").strip()
        raise DiscoveryError(
            f"cannot resolve repository with git {' '.join(args)}: "
            f"{detail or f'exit {proc.returncode}'}"
        )
    try:
        return proc.stdout.decode("utf-8").strip()
    except UnicodeDecodeError as exc:
        raise DiscoveryError(f"cannot resolve repository with git {' '.join(args)}: {exc}") from exc


def _absolute_git_path(cwd: Path, option: str) -> Path:
    path = Path(_git(cwd, "rev-parse", "--path-format=absolute", option))
    if not path.is_absolute():
        path = cwd / path
    return path.resolve()


def resolve_checkout(cwd: Path) -> tuple[Path, Path]:
    """The checkout that owns a hub from any directory inside it, and the git common directory.

    The checkout is the main checkout or a linked worktree; the common
    directory is shared by all of them and may be a bare repository. A bare
    directory has no working tree, so it owns no hub (spec §4).
    """

    try:
        common = _absolute_git_path(cwd, "--git-common-dir")
    except GitTimeoutError:
        raise
    except DiscoveryError:
        raise DiscoveryError(f"{cwd} is not inside a git repository") from None
    if _git(cwd, "rev-parse", "--is-inside-work-tree") != "true":
        if _git(cwd, "rev-parse", "--is-bare-repository") == "true":
            raise DiscoveryError(
                f"{common} is a bare repository with no working tree; "
                "run robomate in one of its linked worktrees (git worktree add)"
            )
        raise DiscoveryError(f"{cwd} is not inside a git working tree")
    return Path(_git(cwd, "rev-parse", "--show-toplevel")).resolve(), common


def is_linked_worktree(checkout: Path, common: Path) -> bool:
    """Whether a checkout is a linked worktree rather than the main checkout."""

    return _absolute_git_path(checkout, "--git-dir") != common


def repository_dir(common: Path) -> Path:
    """The directory a repository is known by, from its git common directory (spec §4).

    `~/src/my-repo/.git` and `~/src/my-repo/.bare` give `~/src/my-repo`; a
    bare `~/src/my-repo.git` is its own directory.
    """

    return common.parent if common.name.startswith(".") else common


def _default_branch(root: Path, common: Path) -> str:
    """The repository's default branch: origin/HEAD, or a bare repository's own HEAD.

    `git clone --bare` copies origin's branches into refs/heads and points the
    bare repository's HEAD at origin's default branch, but creates no
    remote-tracking refs, so there is no origin/HEAD to read (#147).
    """

    try:
        head = _git(root, "symbolic-ref", "--short", "refs/remotes/origin/HEAD")
    except GitTimeoutError:
        raise
    except DiscoveryError as exc:
        if _git(root, f"--git-dir={common}", "rev-parse", "--is-bare-repository") == "true":
            try:
                return _git(root, f"--git-dir={common}", "symbolic-ref", "--short", "HEAD")
            except GitTimeoutError:
                raise
            except DiscoveryError:
                raise DiscoveryError(f"the bare repository {common} has no HEAD branch") from exc
        raise DiscoveryError("origin/HEAD is unset; run git remote set-head origin --auto") from exc
    if not head.startswith("origin/"):
        raise DiscoveryError(f"unexpected origin/HEAD: {head}")
    return head.removeprefix("origin/")


def resolve_repository(cwd: Path, *, probe_cli: bool = False) -> Repository:
    root, common = resolve_checkout(cwd)
    try:
        origin = _git(root, "remote", "get-url", "origin")
    except GitTimeoutError:
        raise
    except DiscoveryError as exc:
        # Name the cause for a smoke checkout with no origin (#301): `up`
        # fails fast here instead of hanging.
        raise DiscoveryError(
            f"{exc}; the checkout needs an origin remote "
            "(a GitHub origin for smoke runs; see guides/worker.md)"
        ) from exc
    if not origin:
        raise DiscoveryError(
            "origin remote is empty; the checkout needs an origin remote "
            "(a GitHub origin for smoke runs; see guides/worker.md)"
        )
    try:
        default_branch = _default_branch(root, common)
    except GitTimeoutError:
        raise
    except DiscoveryError as exc:
        if "origin/HEAD is unset" in str(exc):
            raise DiscoveryError(
                f"{exc}; the smoke checkout needs origin/HEAD set "
                "(clone with an origin, or run git remote set-head origin --auto)"
            ) from exc
        raise
    forge = detect_forge(origin, root, probe_cli=probe_cli, git_common_dir=common)
    return Repository(root, common, origin, default_branch, forge)


# The only places that spell these names (a test fails if any other module does).
# A legacy hub keeps its state in the owning checkout's `.robomate/`; a hub
# relocated by Step 6 of M2 keeps it in `<worktree_root>/<hub>/state/`, and a
# worktree root is `<parent>/<repo>` plus the same suffix (spec §4).
_LEGACY_STATE_DIRNAME = ".robomate"
WORKTREE_ROOT_SUFFIX = ".robomate"
BINDING_FILENAME = "robomate-binding.json"
ROOT_RECORD_FILENAME = "robomate-root.json"
BINDING_VERSION = 1


@dataclass(frozen=True)
class Binding:
    """What a checkout's `robomate-binding.json` records (spec §4 "Binding").

    The checkout is bound to one hub as its `owner` or as an `agent` by that
    name. The record names where the hub's state and token file are and holds
    no token. `checkout` is the hub's owning checkout, which an agent's
    worktree is not. An agent binding created by the worktree manager (M2
    Step 7a, #244) also carries the worktree's `workspace_id`, which is the
    workspace identity for worktrees (spec §6); owner bindings and
    hand-written test bindings leave it unset.
    """

    role: Literal["owner", "agent"]
    hub_id: str
    name: str | None
    checkout: Path
    state_dir: Path
    token_file: Path
    agent: str | None = None
    workspace_id: str | None = None

    def to_json(self) -> dict[str, Any]:
        value: dict[str, Any] = {
            "version": BINDING_VERSION,
            "role": self.role,
            "hub_id": self.hub_id,
            "name": self.name,
            "checkout": str(self.checkout),
            "state_dir": str(self.state_dir),
            "token_file": str(self.token_file),
        }
        if self.agent is not None:
            value["agent"] = self.agent
        if self.workspace_id is not None:
            value["workspace_id"] = self.workspace_id
        return value


def legacy_state_dir(checkout: Path) -> Path:
    """Where a hub created before Step 6 keeps its state: the owning checkout's directory."""

    return checkout / _LEGACY_STATE_DIRNAME


def is_legacy_state_dir(path: Path) -> bool:
    """Whether a state directory is a checkout's legacy one rather than a hub namespace's."""

    return path.name == _LEGACY_STATE_DIRNAME


def _location_field(source: Binding | Mapping[str, Any], key: str) -> object:
    return getattr(source, key, None) if isinstance(source, Binding) else source.get(key)


def state_dir(source: Binding | Mapping[str, Any]) -> Path:
    """The one resolver from a hub's binding or registry entry to its state directory (spec §4).

    A new hub's state is in `<worktree_root>/<hub>/state/` and the record
    names it. A record that names none, written before Step 6, is a legacy
    hub run in place: its owning checkout's legacy directory.
    """

    recorded = _location_field(source, "state_dir")
    if recorded:
        return Path(str(recorded))
    checkout = source.checkout if isinstance(source, Binding) else hub_checkout(source)
    if checkout is None:
        raise DiscoveryError("the hub's record names neither a state location nor a checkout")
    return legacy_state_dir(checkout)


def token_file(source: Binding | Mapping[str, Any]) -> Path:
    """The hub's bearer token file, found through the same resolver (spec §12).

    A relocated hub keeps it in its own `token/` directory; a legacy hub, in
    its state directory.
    """

    recorded = _location_field(source, "token_file")
    if recorded:
        return Path(str(recorded))
    return state_dir(source) / "token"


def hub_checkout(info: Mapping[str, Any]) -> Path | None:
    """The owning checkout a `hub.json` or registry entry records.

    Files written before checkout scope (#147) carry only `repo_root`, which
    was the same directory.
    """

    value = info.get("checkout") or info.get("repo_root")
    return Path(str(value)) if value else None


def checkout_git_dir(checkout: Path) -> Path:
    """The checkout's own git directory, found through git, never constructed.

    `.git` in a main checkout, `<common>/worktrees/<id>` in a linked worktree
    (spec §4 "Binding").
    """

    return _absolute_git_path(checkout, "--git-dir")


# On Windows, opening hub.json while write_hub_json's os.replace still holds
# it, or while another process (antivirus, a concurrent reader) has it open
# without FILE_SHARE_DELETE, fails with a transient sharing violation that the
# C runtime reports as EACCES (#103). Retry briefly; a file that stays
# unreadable still raises DiscoveryError. The binding and the root record are
# read and written the same way.
_READ_RETRY_DELAYS_S = SHARING_RETRY_DELAYS_S
# os.replace cannot replace a destination that another process holds open
# without FILE_SHARE_DELETE, and Python's open() never passes that flag, so a
# concurrent reader (robomate status/down/discover) can cause a transient
# PermissionError (winerror 5) (#114). Retry briefly, with the same delays.
_WRITE_RETRY_DELAYS_S = SHARING_RETRY_DELAYS_S


def _read_json_object(path: Path, what: str) -> dict[str, Any] | None:
    try:
        text = retry_sharing_violation(
            lambda: path.read_text(encoding="utf-8"), _READ_RETRY_DELAYS_S
        )
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as exc:
        raise DiscoveryError(f"cannot read {path}: {exc}") from exc
    try:
        value = json.loads(text)
    except ValueError as exc:
        raise DiscoveryError(f"cannot read {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise DiscoveryError(f"invalid {what} in {path}")
    return value


def _write_json_atomically(directory: Path, filename: str, value: Mapping[str, Any]) -> None:
    descriptor, temporary = tempfile.mkstemp(prefix=".robomate-", dir=directory)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(value, stream, indent=2, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        retry_sharing_violation(
            lambda: os.replace(temporary, directory / filename), _WRITE_RETRY_DELAYS_S
        )
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def read_binding(git_dir: Path) -> Binding | None:
    """The binding in a checkout's git directory, or None when it is not bound (spec §4)."""

    path = git_dir / BINDING_FILENAME
    value = _read_json_object(path, "binding")
    if value is None:
        return None
    version = value.get("version")
    if not isinstance(version, int) or isinstance(version, bool) or version < 1:
        raise DiscoveryError(f"invalid binding in {path}: no version")
    if version > BINDING_VERSION:
        raise DiscoveryError(
            f"{path} is version {version}, written by a newer robomate than this one understands"
        )
    role, hub_id, agent = value.get("role"), value.get("hub_id"), value.get("agent")
    if role not in ("owner", "agent") or not isinstance(hub_id, str) or not hub_id:
        raise DiscoveryError(f"invalid binding in {path}: it needs a role and a hub_id")
    if (role == "agent") != (isinstance(agent, str) and bool(agent)):
        raise DiscoveryError(f"invalid binding in {path}: only an agent binding names an agent")
    paths: dict[str, Path] = {}
    for key in ("checkout", "state_dir", "token_file"):
        raw = value.get(key)
        if not isinstance(raw, str) or not Path(raw).is_absolute():
            raise DiscoveryError(f"invalid binding in {path}: {key} must be an absolute path")
        paths[key] = Path(raw)
    name = value.get("name")
    workspace_id = value.get("workspace_id")
    if workspace_id is not None:
        if not isinstance(workspace_id, str) or not re.fullmatch(r"[0-9a-f]{64}", workspace_id):
            raise DiscoveryError(
                f"invalid binding in {path}: workspace_id must be 64 lowercase hex"
            )
        if role != "agent":
            raise DiscoveryError(
                f"invalid binding in {path}: only an agent binding names a workspace_id"
            )
    return Binding(
        role,
        hub_id,
        name if isinstance(name, str) and name else None,
        paths["checkout"],
        paths["state_dir"],
        paths["token_file"],
        agent if role == "agent" else None,
        workspace_id if isinstance(workspace_id, str) else None,
    )


def write_binding(git_dir: Path, binding: Binding) -> None:
    """Record a checkout's binding in its git directory. The record holds no token."""

    _write_json_atomically(git_dir, BINDING_FILENAME, binding.to_json())


def _git_worktrees(common: Path) -> list[Path]:
    """Every checkout of a repository that still exists: the main one and the linked ones."""

    listing = _git(common, f"--git-dir={common}", "worktree", "list", "--porcelain")
    paths: list[Path] = []
    for line in listing.splitlines():
        if line.startswith("worktree "):
            path = Path(line.removeprefix("worktree "))
            if path.is_dir():
                paths.append(path)
    return paths


def hub_bindings(common: Path, hub_id: str) -> list[tuple[Path, Binding]]:
    """Each (git directory, binding) of the checkouts of one repository bound to a hub."""

    found = []
    for path in _git_worktrees(common):
        try:
            git_dir = checkout_git_dir(path)
        except DiscoveryError:
            continue
        binding = read_binding(git_dir)
        if binding is not None and binding.hub_id == hub_id:
            found.append((git_dir, binding))
    return found


# Hub names (spec §4 "Hub name"): one path component on Windows and Linux,
# and one component of a git ref.
HUB_NAME_PATTERN = re.compile(r"^[a-z0-9]([a-z0-9_-]{0,38}[a-z0-9])?$")
RESERVED_HUB_NAMES = frozenset(
    {"con", "nul", "aux", "prn", "state", "token"}
    | {f"com{n}" for n in range(1, 10)}
    | {f"lpt{n}" for n in range(1, 10)}
)


def validate_hub_name(name: str) -> str:
    """Return a valid hub name unchanged, or refuse it with the rule it breaks."""

    if not HUB_NAME_PATTERN.fullmatch(name):
        raise DiscoveryError(
            f"invalid hub name {name!r}: use 1 to 40 of a-z, 0-9, '-' and '_', "
            "starting and ending with a letter or digit"
        )
    if name in RESERVED_HUB_NAMES:
        raise DiscoveryError(f"invalid hub name {name!r}: the name is reserved")
    return name


def validate_agent_name(name: str) -> str:
    """Return a valid agent name unchanged, or refuse it with the rule it breaks (spec §5)."""

    if not HUB_NAME_PATTERN.fullmatch(name):
        raise ValueError(
            f"invalid agent name {name!r}: use 1 to 40 of a-z, 0-9, '-' and '_', "
            "starting and ending with a letter or digit"
        )
    if name in RESERVED_HUB_NAMES:
        raise ValueError(f"invalid agent name {name!r}: the name is reserved")
    return name


def derive_hub_name(checkout: Path) -> str:
    """The default hub name, from the owning checkout's directory name."""

    name = re.sub(r"[^a-z0-9_-]+", "-", checkout.name.lower())[:40].strip("-_")
    try:
        return validate_hub_name(name)
    except DiscoveryError as exc:
        raise DiscoveryError(
            f"cannot derive a hub name from {checkout.name!r} ({exc}); "
            "choose one with robomate up --name"
        ) from None


def extract_origin_host(origin: str) -> str | None:
    """Extract the remote host from an origin URL or scp-style address."""
    text = origin.strip()
    # Local paths never name a forge host (#301): Windows drive-letter
    # (`C:\\`, `C:/`), UNC (`\\\\server\\share`), `file://` URLs, and any
    # path with a backslash. Without this, `C:\\...` parses as host `c`
    # and `file:///...` as host `file`, triggering bogus `gh`/`glab` probes.
    if text.startswith("file://"):
        return None
    if re.match(r"^[a-zA-Z]:([\\/]|$)", text):
        return None
    if "\\" in text:
        return None
    try:
        parsed = urlparse(text)
        host = parsed.hostname
    except ValueError:
        return None
    if host:
        return host.lower()
    match = re.match(r"^(?:[^@]+@)?([^:/]+):", text)
    if match:
        return match.group(1).lower()
    return None


def _match_yaml_host(content: str, host: str, *, under_hosts: bool = False) -> bool:
    """Check if host is configured as a key in YAML content."""
    search_text = content
    if under_hosts:
        hosts_match = re.search(r"^[ \t]*hosts:[ \t]*(?:#.*)?$", content, re.MULTILINE)
        if not hosts_match:
            return False
        search_text = content[hosts_match.end() :]
    pattern = re.compile(rf"^[ \t]*{re.escape(host)}:[ \t]*(?:#.*)?$", re.MULTILINE)
    return bool(pattern.search(search_text))


def detect_forge(
    origin: str,
    root: Path | None = None,
    *,
    probe_cli: bool = False,
    git_common_dir: Path | None = None,
    environ: Mapping[str, str] | None = None,
) -> str:
    """Detect forge ('github', 'gitlab', or 'unknown') (spec §10)."""
    from .config_layers import load_config_layers

    host = extract_origin_host(origin)
    if not host:
        return "unknown"
    if host == "github.com":
        return "github"
    if host == "gitlab.com":
        return "gitlab"

    # 1. Check configuration layers (repository, user, and a legacy hub's config.toml)
    common = git_common_dir
    if common is None and root is not None:
        if (root / ".git").is_file() or (root / ".git" / "HEAD").is_file():
            try:
                _, common = resolve_checkout(root)
            except DiscoveryError:
                common = root / ".git"
        else:
            common = root / ".git"
    # Only a legacy hub's own config may still hold [forge] (spec §4); a hub
    # relocated by Step 6 of M2 has no such layer, and the forge is the
    # repository's and the user's to set.
    hub_cfg = legacy_state_dir(root) / "config.toml" if root is not None else None
    layered = load_config_layers(
        hub_config=hub_cfg,
        git_common_dir=common,
        environ=environ,
    )
    configured_forge = layered.forge.detect(host)
    if configured_forge is not None:
        return configured_forge

    # 2. Check glab CLI configuration file
    env = os.environ if environ is None else environ
    config_home = Path(env.get("XDG_CONFIG_HOME") or Path.home() / ".config")
    glab_config = config_home / "glab-cli" / "config.yml"
    if glab_config.is_file():
        try:
            content = glab_config.read_text(encoding="utf-8")
            if _match_yaml_host(content, host, under_hosts=True):
                return "gitlab"
        except Exception:
            pass

    # 3. Check gh CLI configuration file
    gh_hosts = config_home / "gh" / "hosts.yml"
    if gh_hosts.is_file():
        try:
            content = gh_hosts.read_text(encoding="utf-8")
            if _match_yaml_host(content, host, under_hosts=False):
                return "github"
        except Exception:
            pass

    # 4. Probe glab auth status for the host (only when probe_cli is True, e.g. at `up`)
    if probe_cli and shutil.which("glab"):
        try:
            proc = subprocess.run(
                ["glab", "auth", "status", f"--hostname={host}"],
                capture_output=True,
                encoding="utf-8",
                errors="replace",
                stdin=subprocess.DEVNULL,
                timeout=2,
            )
            output = proc.stdout + proc.stderr
            if "Logged in to" in output or (
                "REST API Endpoint" in output and "not authenticated" not in output
            ):
                return "gitlab"
        except Exception:
            pass

    # 5. Probe gh auth status for the host (only when probe_cli is True, e.g. at `up`)
    if probe_cli and shutil.which("gh"):
        try:
            proc = subprocess.run(
                ["gh", "auth", "status", f"--hostname={host}"],
                capture_output=True,
                encoding="utf-8",
                errors="replace",
                stdin=subprocess.DEVNULL,
                timeout=2,
            )
            output = proc.stdout + proc.stderr
            if "Logged in to" in output:
                return "github"
        except Exception:
            pass

    return "unknown"


def read_hub_json(state: Path) -> dict[str, Any] | None:
    """The identity and last address a hub recorded in its state directory, if any."""

    return _read_json_object(state / "hub.json", "hub metadata")


def write_hub_json(state: Path, value: Mapping[str, Any]) -> None:
    state.mkdir(mode=0o700, parents=True, exist_ok=True)
    _write_json_atomically(state, "hub.json", value)


def ensure_excluded(common: Path) -> None:
    """Keep a legacy hub's state directory out of `git status` through `info/exclude`."""

    path = common / "info" / "exclude"
    path.parent.mkdir(parents=True, exist_ok=True)
    line = f"/{_LEGACY_STATE_DIRNAME}/"
    existing = path.read_text(encoding="utf-8") if path.exists() else ""
    if line not in existing.splitlines():
        with path.open("a", encoding="utf-8") as stream:
            if existing and not existing.endswith("\n"):
                stream.write("\n")
            stream.write(f"{line}\n")


def _same_path(a: Path, b: Path) -> bool:
    return os.path.normcase(os.path.realpath(a)) == os.path.normcase(os.path.realpath(b))


def read_root_record(root: Path) -> dict[str, Any] | None:
    """The ownership record of a worktree root, if it has one (spec §4)."""

    path = root / ROOT_RECORD_FILENAME
    for delay in (0.05, 0.1, None):
        try:
            return _read_json_object(path, "worktree root record")
        except DiscoveryError:
            # A sibling hub's first `up` may be between creating the record and
            # writing it: an empty file is read again shortly.
            if delay is None or path.stat().st_size:
                raise
            time.sleep(delay)
    raise AssertionError("unreachable")  # pragma: no cover


def check_worktree_root(root: Path, common: Path) -> bool:
    """Whether `root` is this repository's worktree root; refuse it when it is another's.

    A root records, when its first hub creates it, the git common directory
    it belongs to, and a hub of any other repository refuses it and asks for a
    repository-level `worktree_root` (spec §4). Returns False for a root that
    does not exist yet or is empty, which `claim_worktree_root` then creates.
    A directory that holds other things but no record is not robomate's to
    adopt: two repositories of one name could otherwise share it unnoticed.
    """

    from .config_layers import repository_config_path

    advice = (
        f"set a repository-level worktree_root in {repository_config_path(common)} "
        "(spec §4 Configuration layers)"
    )
    if not root.exists():
        return False
    if not root.is_dir():
        raise DiscoveryError(f"the worktree root {root} is not a directory; {advice}")
    record = read_root_record(root)
    if record is None:
        if any(root.iterdir()):
            raise DiscoveryError(
                f"the worktree root {root} holds files but no {ROOT_RECORD_FILENAME}, "
                f"so it is not known to belong to {repository_dir(common)}; {advice}, "
                "or move what it holds away"
            )
        return False
    recorded = record.get("git_common_dir")
    if not isinstance(recorded, str) or not _same_path(Path(recorded), common):
        raise DiscoveryError(
            f"the worktree root {root} belongs to the repository {recorded}, not {common}; {advice}"
        )
    return True


def claim_worktree_root(root: Path, common: Path) -> None:
    """Create a worktree root with its ownership record, or confirm it is this repository's."""

    if check_worktree_root(root, common):
        return
    root.mkdir(parents=True, exist_ok=True)
    try:
        with (root / ROOT_RECORD_FILENAME).open("x", encoding="utf-8") as stream:
            json.dump({"git_common_dir": str(common)}, stream, indent=2, sort_keys=True)
            stream.write("\n")
    except FileExistsError:
        # A sibling hub of another repository created it first; compare.
        check_worktree_root(root, common)


def hub_paths(worktree_root: Path, name: str) -> tuple[Path, Path]:
    """A hub's state directory and token file under a worktree root (spec §4, §12).

    The token has a directory of its own, so a harness that sandboxes its MCP
    servers' reads can be granted the token alone, never `hub.db` or `runs/`.
    """

    namespace = worktree_root / name
    return namespace / "state", namespace / "token" / "token"


def namespace_free(namespace: Path) -> bool:
    """Whether a hub namespace directory can be created: absent, or only an abandoned skeleton.

    A start that failed after creating its directories leaves empty `state/`
    and `token/` directories and the held `up.lock`; those do not make the
    name taken.
    """

    if not namespace.exists():
        return True
    if not namespace.is_dir():
        return False
    return all(
        path.is_dir() or (path.name == "up.lock" and path.parent.name == "state")
        for path in namespace.rglob("*")
    )


@dataclass(frozen=True)
class HubPlacement:
    """Where `robomate up` keeps a checkout's hub, and whether it has to create it."""

    state_dir: Path
    token_file: Path
    new: bool
    """The hub does not exist yet, and `create_hub_dirs` makes its directories."""
    name: str | None = None
    """A new hub's name. An existing hub's name is in its hub.json."""
    worktree_root: Path | None = None
    git_common_dir: Path | None = None

    @property
    def legacy(self) -> bool:
        return is_legacy_state_dir(self.state_dir)


def plan_hub_placement(
    repo: Repository, requested_name: str | None, environ: Mapping[str, str] | None = None
) -> HubPlacement:
    """Where the hub of `repo.root` lives, for `up` (spec §4 "State location", "Migration").

    1. The checkout's binding: an owner binding names the hub's state and
       token wherever they are; a checkout bound as an agent is refused, since
       `up` runs only in a hub's owning checkout.
    2. State a hub left in the checkout before Step 6 (a hub.json, hub.db or
       config.toml of its own): it keeps running in place.
    3. Otherwise a new hub, in `<worktree_root>/<hub>/state/` with its token in
       `<worktree_root>/<hub>/token/token`. Its name must not be taken as a
       directory under the root, and the root must be this repository's.
    """

    from .config_layers import load_config_layers

    git_dir = checkout_git_dir(repo.root)

    def bound() -> HubPlacement | None:
        binding = read_binding(git_dir)
        if binding is None:
            return None
        named = binding.name or binding.hub_id
        if binding.role == "agent":
            raise DiscoveryError(
                f"{repo.root} is bound to hub {named} as agent {binding.agent!r}; "
                f"robomate up runs in a hub's owning checkout, {binding.checkout}, "
                "never in an agent's worktree"
            )
        if not binding.state_dir.is_dir():
            raise DiscoveryError(
                f"{repo.root} is bound to hub {named} (hub_id {binding.hub_id}), "
                f"whose state {binding.state_dir} is missing; restore it, or remove "
                f"{git_dir / BINDING_FILENAME} to start a new hub here"
            )
        return HubPlacement(binding.state_dir, binding.token_file, False, binding.name)

    placement = bound()
    if placement is not None:
        return placement
    legacy = legacy_state_dir(repo.root)
    if any((legacy / name).is_file() for name in ("hub.json", "hub.db", "config.toml")):
        return HubPlacement(legacy, legacy / "token", False)
    name = (
        validate_hub_name(requested_name)
        if requested_name is not None
        else derive_hub_name(repo.root)
    )
    layered = load_config_layers(git_common_dir=repo.git_common_dir, environ=environ)
    root = layered.worktree_root
    assert root is not None  # a git common directory always yields a default root
    check_worktree_root(root, repo.git_common_dir)
    namespace = root / name
    if not namespace_free(namespace):
        # A sibling `up` of this checkout may have bound the hub since the
        # first look: that is the hub already starting, not a taken name.
        placement = bound()
        if placement is not None:
            return placement
        raise DiscoveryError(
            f"hub name {name!r} is taken: {namespace} exists under the worktree root. "
            "Choose another with robomate up --name"
        )
    state, token = hub_paths(root, name)
    return HubPlacement(state, token, True, name, root, repo.git_common_dir)


def create_hub_dirs(placement: HubPlacement) -> None:
    """Create a hub's directories: `state/` and `token/` apart, both owner-only (spec §4, §12)."""

    if placement.new:
        assert placement.worktree_root is not None and placement.git_common_dir is not None
        claim_worktree_root(placement.worktree_root, placement.git_common_dir)
    for directory in {placement.state_dir, placement.token_file.parent}:
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(directory, 0o700)


@dataclass(frozen=True)
class LocalHub:
    """A hub on this machine, found through its binding or the registry's entry."""

    checkout: Path
    info: dict[str, Any]
    state_dir: Path
    token_file: Path

    @property
    def url(self) -> str:
        return str(self.info.get("url") or "")

    @property
    def hub_id(self) -> str:
        return str(self.info.get("hub_id") or "")

    @property
    def name(self) -> str | None:
        name = self.info.get("name")
        return str(name) if name else None

    def label(self) -> str:
        """How messages name this hub: its name, or its checkout before it has one."""

        return f"hub {self.name}" if self.name else f"hub for {self.checkout}"

    def endpoint(self) -> HubEndpoint:
        token = self.token_file.read_text(encoding="utf-8").strip()
        return HubEndpoint(self.url.rstrip("/"), token)


def stale_partial_text(hub: LocalHub) -> str:
    """Describe a hub.json that never finished startup: identity without address (#301)."""

    return (
        f"stale partial state remains in {hub.state_dir / 'hub.json'} "
        f"(hub_id {hub.hub_id}, no url/port recorded: startup never finished)"
    )


def verify_local_hub(hub: LocalHub) -> None:
    """Check a local hub's recorded hub_id against /healthz before it is used (spec §4).

    Nothing authenticated is sent first: a process that took the hub's
    address while it was stopped never receives the bearer token.
    """

    from .registry import (
        HEALTHZ_LIVE_PID_TIMEOUT_S,
        HEALTHZ_TIMEOUT_S,
        check_healthz,
        process_alive,
    )

    partial = hub.state_dir / "hub.json"
    # Identity without an address in an existing record is a startup that
    # failed before serving (#301). A bound hub whose state directory holds
    # no hub.json yet is still starting: it falls through and is reported
    # not running.
    if not hub.url and partial.is_file():
        raise DiscoveryError(
            f"{hub.label()} (checkout {hub.checkout}) never finished starting; "
            f"{stale_partial_text(hub)}; run robomate up in that checkout"
        )
    pid = int(hub.info.get("pid") or 0)
    alive = process_alive(pid)
    timeout_s = HEALTHZ_LIVE_PID_TIMEOUT_S if alive else HEALTHZ_TIMEOUT_S
    health = check_healthz(hub.url, timeout_s) if hub.url else None
    if health is not None and health.hub_id == hub.hub_id:
        return
    where = f"{hub.label()} (checkout {hub.checkout})" + (f" at {hub.url}" if hub.url else "")
    if health is not None and health.hub_id is not None:
        raise DiscoveryError(
            f"{where} answers as hub {health.hub_id}, not {hub.hub_id}: another process holds "
            "its address. Stop that process, then run robomate up in that checkout"
        )
    failure = health.describe() if health is not None else "is unreachable"
    # Name the stale state `up` left behind (#301): hub.json still records
    # the stopped hub, so `down` and `status` can point at it instead of
    # only saying "not running".
    stale = (
        f"; stale state remains in {partial} "
        f"(hub_id {hub.hub_id}, pid {pid or '-'}, port {hub.info.get('port') or '-'})"
        if partial.is_file()
        else ""
    )
    if alive:
        raise DiscoveryError(f"{where} {failure}; recorded pid {pid} is visible{stale}")
    if health is not None and health.timed_out:
        raise DiscoveryError(f"{where} {failure}{stale}")
    raise DiscoveryError(f"{where} is not running{stale}; run robomate up in that checkout")


def bound_hub(binding: Binding) -> LocalHub:
    """The hub a binding names. A hub whose state is gone is an error naming it (spec §4).

    A bound hub whose state directory exists but holds no `hub.json` yet is one
    that `up` bound and is still starting, or whose `hub.json` was removed. It
    is found with no address, and `verify_local_hub` reports it not running.
    """

    info = read_hub_json(binding.state_dir)
    if info is None and binding.state_dir.is_dir():
        info = {"hub_id": binding.hub_id, "name": binding.name}
    if info is None or str(info.get("hub_id") or "") != binding.hub_id:
        raise DiscoveryError(
            f"this checkout is bound to hub {binding.name or binding.hub_id} "
            f"(hub_id {binding.hub_id}), owned by checkout {binding.checkout}, "
            f"but the hub's state at {binding.state_dir} is missing or belongs to "
            "another hub"
        )
    return LocalHub(binding.checkout, info, binding.state_dir, binding.token_file)


def entry_hub(entry: Mapping[str, Any]) -> LocalHub:
    """The hub a machine-registry entry names, read from the state location it records."""

    state = state_dir(entry)
    info = read_hub_json(state)
    checkout = hub_checkout(entry)
    if info is None or checkout is None or info.get("hub_id") != entry.get("hub_id"):
        raise DiscoveryError(
            f"hub {entry.get('hub_id')} is registered with state at {state}, "
            "which no longer holds it"
        )
    return LocalHub(checkout, info, state, token_file(entry))


def local_hub(checkout: Path) -> LocalHub | None:
    """The hub a checkout is bound to or owns (spec §4 discovery rule 2).

    The checkout's binding, owner or agent, names the hub and where its state
    is. Without one, a hub that `up` recorded in the checkout's own legacy
    state before Step 6 is its hub. Identity without an address (a startup
    that failed before serving) still resolves, so `down` and `status` report
    the stale partial record instead of saying no hub is recorded (#301).
    A binding to a hub whose state is gone is an error naming that hub, never
    a fall-through to anything else.
    """

    binding = read_binding(checkout_git_dir(checkout))
    if binding is not None:
        return bound_hub(binding)
    state = legacy_state_dir(checkout)
    info = read_hub_json(state)
    if info is None or (not info.get("hub_id") and not info.get("url")):
        return None
    return LocalHub(checkout, info, state, state / "token")


def _explicit(env: Mapping[str, str]) -> HubEndpoint | None:
    url = env.get("ROBOMATE_HUB_URL", "").strip()
    if not url:
        return None
    token = env.get("ROBOMATE_TOKEN", "").strip()
    path = env.get("ROBOMATE_TOKEN_FILE", "").strip()
    if not token and path:
        token = Path(path).expanduser().read_text(encoding="utf-8").strip()
    if not token:
        raise DiscoveryError("ROBOMATE_HUB_URL requires ROBOMATE_TOKEN or ROBOMATE_TOKEN_FILE")
    return HubEndpoint(url.rstrip("/"), token)


def _is_path_selector(selector: str) -> bool:
    # A hub name or hub_id never holds a separator or a dot, so anything that
    # does is a path; `./wt-a` names a checkout beside the working directory.
    return any(char in selector for char in "/\\.~:") or Path(selector).is_absolute()


def describe_hubs(entries: list[dict[str, Any]]) -> str:
    """One line per hub: name, owning checkout, repository and URL."""

    lines = []
    for entry in entries:
        common = entry.get("git_common_dir")
        repository = repository_dir(Path(str(common))) if common else hub_checkout(entry)
        lines.append(
            f"  {entry.get('name') or '(unnamed)'}  checkout {hub_checkout(entry)}"
            f"  repository {repository}  {entry.get('url')}  hub_id {entry.get('hub_id')}"
        )
    return "\n".join(lines) or "  (none)"


def select_hub(selector: str, environ: Mapping[str, str] | None = None) -> LocalHub:
    """The hub an explicit `--hub <name | hub_id | checkout path>` names."""

    from .registry import hub_entries, live_entries

    if _is_path_selector(selector):
        path = Path(selector).expanduser()
        if not path.is_dir():
            raise DiscoveryError(f"--hub {selector}: no such checkout directory")
        try:
            checkout, _ = resolve_checkout(path.resolve())
        except DiscoveryError as exc:
            raise DiscoveryError(f"--hub {selector}: {exc}") from None
        hub = local_hub(checkout)
        if hub is None:
            raise DiscoveryError(f"--hub {selector}: no hub is recorded for checkout {checkout}")
        return hub
    entries = hub_entries(environ)
    matches = [entry for entry in entries if entry.get("hub_id") == selector] or [
        entry for entry in entries if entry.get("name") == selector
    ]
    if not matches:
        raise DiscoveryError(
            f"--hub {selector}: no registered hub has that name or hub_id. "
            f"Registered hubs:\n{describe_hubs(entries)}\n"
            f"Live hubs:\n{describe_hubs(live_entries(environ))}"
        )
    if len(matches) > 1:
        raise DiscoveryError(
            f"--hub {selector}: the name matches hubs of several repositories; "
            f"choose one by hub_id or checkout path:\n{describe_hubs(matches)}"
        )
    try:
        return entry_hub(matches[0])
    except DiscoveryError as exc:
        raise DiscoveryError(f"--hub {selector}: {exc}") from None


def find_hub(
    cwd: Path, environ: Mapping[str, str] | None = None, *, selector: str | None = None
) -> LocalHub | HubEndpoint:
    """Find the hub to act on, in the spec §4 discovery order. There is no silent attach.

    1. An explicit `--hub` selector, else `ROBOMATE_HUB_URL` with a token.
    2. The hub the checkout containing `cwd` is bound to, or owns.
    3. Otherwise an error listing the live hubs and how to choose one. A
       checkout never reaches a hub it was not given, even the only one.
    """

    from .registry import live_entries

    env = os.environ if environ is None else environ
    if selector:
        return select_hub(selector, env)
    explicit = _explicit(env)
    if explicit is not None:
        return explicit
    try:
        checkout, _ = resolve_checkout(cwd)
    except DiscoveryError as exc:
        where = str(exc)
    else:
        hub = local_hub(checkout)
        if hub is not None:
            return hub
        where = f"no hub is recorded for checkout {checkout}"
    raise DiscoveryError(
        f"{where}. Choose a hub with --hub <name | hub_id | checkout path> (CLI) or "
        "ROBOMATE_HUB_URL with ROBOMATE_TOKEN_FILE (MCP bridge), or run robomate up "
        f"here. Live hubs:\n{describe_hubs(live_entries(env))}"
    )


def discover(
    cwd: Path, environ: Mapping[str, str] | None = None, *, selector: str | None = None
) -> HubEndpoint:
    """The URL and token of the hub `find_hub` chooses, for the MCP bridge and scripts.

    A local hub is used only once /healthz reports its recorded hub_id; an
    explicit URL is taken as given.
    """

    hub = find_hub(cwd, environ, selector=selector)
    if isinstance(hub, LocalHub):
        verify_local_hub(hub)
        return hub.endpoint()
    return hub
