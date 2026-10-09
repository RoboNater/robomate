"""Checkout, hub-state and hub discovery for the operator CLI and the MCP bridge.

A hub belongs to the checkout where `robomate up` runs (spec §4): the main
checkout or a linked worktree. Every consumer finds a hub's state through
`state_dir()` and `token_file()`, so moving the state out of the checkout
(M2) is a change in this module only.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlparse


class DiscoveryError(RuntimeError):
    """A repository or hub cannot be identified unambiguously."""


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


def _git(cwd: Path, *args: str) -> str:
    # stdin=DEVNULL: `robomate mcp` reaches this with stdin owned by MCP. A
    # child inheriting that pipe on Windows blocks until the next message (#65).
    try:
        return subprocess.check_output(
            ["git", *args],
            cwd=cwd,
            text=True,
            stdin=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
        ).strip()
    except (OSError, subprocess.CalledProcessError) as exc:
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
    except DiscoveryError as exc:
        if _git(root, f"--git-dir={common}", "rev-parse", "--is-bare-repository") == "true":
            try:
                return _git(root, f"--git-dir={common}", "symbolic-ref", "--short", "HEAD")
            except DiscoveryError:
                raise DiscoveryError(f"the bare repository {common} has no HEAD branch") from exc
        raise DiscoveryError("origin/HEAD is unset; run git remote set-head origin --auto") from exc
    if not head.startswith("origin/"):
        raise DiscoveryError(f"unexpected origin/HEAD: {head}")
    return head.removeprefix("origin/")


def resolve_repository(cwd: Path, *, probe_cli: bool = False) -> Repository:
    root, common = resolve_checkout(cwd)
    origin = _git(root, "remote", "get-url", "origin")
    if not origin:
        raise DiscoveryError("origin remote is empty")
    default_branch = _default_branch(root, common)
    forge = detect_forge(origin, root, probe_cli=probe_cli)
    return Repository(root, common, origin, default_branch, forge)


def state_dir(checkout: Path) -> Path:
    """The one resolver from a hub's owning checkout to its state directory (spec §4).

    Until M2 moves state beside the hub's agent worktrees, it is the owning
    checkout's `.robomate/`, excluded through the git common directory.
    """

    return checkout / ".robomate"


def token_file(checkout: Path) -> Path:
    """The hub's bearer token file, found through the same resolver."""

    return state_dir(checkout) / "token"


def hub_checkout(info: Mapping[str, Any]) -> Path | None:
    """The owning checkout a `hub.json` or registry entry records.

    Files written before checkout scope (#147) carry only `repo_root`, which
    was the same directory.
    """

    value = info.get("checkout") or info.get("repo_root")
    return Path(str(value)) if value else None


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
    try:
        parsed = urlparse(origin)
        host = parsed.hostname
    except ValueError:
        return None
    if host:
        return host.lower()
    match = re.match(r"^(?:[^@]+@)?([^:/]+):", origin)
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


def detect_forge(origin: str, root: Path | None = None, *, probe_cli: bool = False) -> str:
    """Detect forge ('github', 'gitlab', or 'unknown') (spec §10)."""
    host = extract_origin_host(origin)
    if not host:
        return "unknown"
    if host == "github.com":
        return "github"
    if host == "gitlab.com":
        return "gitlab"

    # 1. Check repository config.toml (.robomate/config.toml only) if root is provided
    if root is not None:
        config_path = state_dir(root) / "config.toml"
        if config_path.is_file():
            try:
                data = tomllib.loads(config_path.read_text(encoding="utf-8"))
            except Exception as exc:
                raise DiscoveryError(f"invalid configuration in {config_path}: {exc}") from exc
            forge_cfg = data.get("forge")
            if isinstance(forge_cfg, dict):
                gitlab_hosts = forge_cfg.get("gitlab_hosts", [])
                if isinstance(gitlab_hosts, list) and host in gitlab_hosts:
                    return "gitlab"
                github_hosts = forge_cfg.get("github_hosts", [])
                if isinstance(github_hosts, list) and host in github_hosts:
                    return "github"

    # 2. Check glab CLI configuration file
    config_home = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
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
                text=True,
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
                text=True,
                stdin=subprocess.DEVNULL,
                timeout=2,
            )
            output = proc.stdout + proc.stderr
            if "Logged in to" in output:
                return "github"
        except Exception:
            pass

    return "unknown"


# On Windows, opening hub.json while write_hub_json's os.replace still holds
# it, or while another process (antivirus, a concurrent reader) has it open
# without FILE_SHARE_DELETE, fails with a transient sharing violation that the
# C runtime reports as EACCES (#103). Retry briefly; a file that stays
# unreadable still raises DiscoveryError.
_READ_RETRY_DELAYS_S = (0.01, 0.02, 0.05, 0.1, 0.2, 0.2) if sys.platform == "win32" else ()


def read_hub_json(root: Path) -> dict[str, Any] | None:
    path = state_dir(root) / "hub.json"
    for delay in (*_READ_RETRY_DELAYS_S, None):
        try:
            text = path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return None
        except PermissionError as exc:
            if delay is None:
                raise DiscoveryError(f"cannot read {path}: {exc}") from exc
            time.sleep(delay)
            continue
        except (OSError, ValueError) as exc:
            raise DiscoveryError(f"cannot read {path}: {exc}") from exc
        break
    try:
        value = json.loads(text)
    except ValueError as exc:
        raise DiscoveryError(f"cannot read {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise DiscoveryError(f"invalid hub metadata in {path}")
    return value


# On Windows, os.replace cannot replace a destination that another process
# holds open without FILE_SHARE_DELETE, and Python's open() never passes that
# flag, so a concurrent reader (robomate status/down/discover) can cause a
# transient PermissionError (winerror 5) (#114). Retry briefly, mirroring
# _READ_RETRY_DELAYS_S.
_WRITE_RETRY_DELAYS_S = (0.01, 0.02, 0.05, 0.1, 0.2, 0.2) if sys.platform == "win32" else ()


def write_hub_json(root: Path, value: Mapping[str, Any]) -> None:
    directory = state_dir(root)
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=".hub-", dir=directory)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(value, stream, indent=2, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        for delay in (*_WRITE_RETRY_DELAYS_S, None):
            try:
                os.replace(temporary, directory / "hub.json")
                break
            except PermissionError:
                if delay is None:
                    raise
                time.sleep(delay)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def ensure_excluded(common: Path) -> None:
    path = common / "info" / "exclude"
    path.parent.mkdir(parents=True, exist_ok=True)
    existing = path.read_text(encoding="utf-8") if path.exists() else ""
    if "/.robomate/" not in existing.splitlines():
        with path.open("a", encoding="utf-8") as stream:
            if existing and not existing.endswith("\n"):
                stream.write("\n")
            stream.write("/.robomate/\n")


@dataclass(frozen=True)
class LocalHub:
    """A hub on this machine, found through its owning checkout's state."""

    checkout: Path
    info: dict[str, Any]

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
        token = token_file(self.checkout).read_text(encoding="utf-8").strip()
        return HubEndpoint(self.url.rstrip("/"), token)


def verify_local_hub(hub: LocalHub) -> None:
    """Check a local hub's recorded hub_id against /healthz before it is used (spec §4).

    Nothing authenticated is sent first: a process that took the hub's
    address while it was stopped never receives the bearer token.
    """

    from .registry import healthz_hub_id, process_alive

    answered = healthz_hub_id(hub.url) if hub.url else None
    if answered == hub.hub_id:
        return
    where = f"{hub.label()} (checkout {hub.checkout}) at {hub.url}"
    if answered is not None:
        raise DiscoveryError(
            f"{where} answers as hub {answered}, not {hub.hub_id}: another process holds "
            "its address. Stop that process, then run robomate up in that checkout"
        )
    pid = int(hub.info.get("pid") or 0)
    if process_alive(pid):
        raise DiscoveryError(f"{where} is unreachable; recorded pid {pid} is visible")
    raise DiscoveryError(f"{where} is not running; run robomate up in that checkout")


def local_hub(checkout: Path) -> LocalHub | None:
    """The hub a checkout owns, if `up` has ever recorded one there."""

    info = read_hub_json(checkout)
    if info is None or not info.get("url"):
        return None
    return LocalHub(checkout, info)


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

    from .registry import hub_entries

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
            f"Registered hubs:\n{describe_hubs(entries)}"
        )
    if len(matches) > 1:
        raise DiscoveryError(
            f"--hub {selector}: the name matches hubs of several repositories; "
            f"choose one by hub_id or checkout path:\n{describe_hubs(matches)}"
        )
    entry = matches[0]
    registered = hub_checkout(entry)
    hub = local_hub(registered) if registered is not None else None
    if hub is None or hub.hub_id != entry.get("hub_id"):
        raise DiscoveryError(
            f"--hub {selector}: hub {entry.get('hub_id')} is registered for checkout "
            f"{registered}, which no longer holds it"
        )
    return hub


def find_hub(
    cwd: Path, environ: Mapping[str, str] | None = None, *, selector: str | None = None
) -> LocalHub | HubEndpoint:
    """Find the hub to act on, in the spec §4 discovery order. There is no silent attach.

    1. An explicit `--hub` selector, else `ROBOMATE_HUB_URL` with a token.
    2. The hub the checkout containing `cwd` owns.
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


def discover(cwd: Path, environ: Mapping[str, str] | None = None) -> HubEndpoint:
    """The URL and token of the hub `find_hub` chooses, for the MCP bridge and scripts.

    A local hub is used only once /healthz reports its recorded hub_id; an
    explicit URL is taken as given.
    """

    hub = find_hub(cwd, environ)
    if isinstance(hub, LocalHub):
        verify_local_hub(hub)
        return hub.endpoint()
    return hub
