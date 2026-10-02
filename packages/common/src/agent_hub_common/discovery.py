"""Repository and hub discovery for the operator CLI and future MCP bridge."""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
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
            ["git", *args], cwd=cwd, text=True, stdin=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
        ).strip()
    except (OSError, subprocess.CalledProcessError) as exc:
        raise DiscoveryError(f"cannot resolve repository with git {' '.join(args)}: {exc}") from exc


def repo_root(cwd: Path) -> tuple[Path, Path]:
    """Find the owning checkout even when cwd is inside a linked worktree."""

    common = Path(_git(cwd, "rev-parse", "--path-format=absolute", "--git-common-dir"))
    if not common.is_absolute():
        common = (cwd / common).resolve()
    common = common.resolve()
    if common.name != ".git":
        raise DiscoveryError(f"git common directory is not a checkout: {common}")
    return common.parent, common


def resolve_repository(cwd: Path, *, probe_cli: bool = False) -> Repository:
    root, common = repo_root(cwd)
    origin = _git(root, "remote", "get-url", "origin")
    if not origin:
        raise DiscoveryError("origin remote is empty")
    try:
        head = _git(root, "symbolic-ref", "--short", "refs/remotes/origin/HEAD")
    except DiscoveryError as exc:
        raise DiscoveryError(
            "origin/HEAD is unset; run git remote set-head origin --auto"
        ) from exc
    if not head.startswith("origin/"):
        raise DiscoveryError(f"unexpected origin/HEAD: {head}")
    forge = detect_forge(origin, root, probe_cli=probe_cli)
    return Repository(root, common, origin, head.removeprefix("origin/"), forge)


def state_dir(root: Path) -> Path:
    return root / ".robomate"


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
        search_text = content[hosts_match.end():]
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


def read_hub_json(root: Path) -> dict[str, Any] | None:
    path = state_dir(root) / "hub.json"
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as exc:
        raise DiscoveryError(f"cannot read {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise DiscoveryError(f"invalid hub metadata in {path}")
    return value


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
        os.replace(temporary, directory / "hub.json")
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


def discover(cwd: Path, environ: Mapping[str, str] | None = None) -> HubEndpoint:
    """Resolve explicit settings, this repo, or the sole live registry entry."""

    from .registry import live_entries

    env = os.environ if environ is None else environ
    explicit_url = env.get("ROBOMATE_HUB_URL", "").strip()
    if explicit_url:
        token = env.get("ROBOMATE_TOKEN", "").strip()
        token_file = env.get("ROBOMATE_TOKEN_FILE", "").strip()
        if not token and token_file:
            token = Path(token_file).expanduser().read_text(encoding="utf-8").strip()
        if not token:
            raise DiscoveryError("ROBOMATE_HUB_URL requires ROBOMATE_TOKEN or ROBOMATE_TOKEN_FILE")
        return HubEndpoint(explicit_url.rstrip("/"), token)
    try:
        root, _ = repo_root(cwd)
    except DiscoveryError:
        root = None
    if root is not None:
        data = read_hub_json(root)
        if data is not None and data.get("url"):
            token = (state_dir(root) / "token").read_text(encoding="utf-8").strip()
            return HubEndpoint(str(data["url"]), token)
    entries = live_entries(env)
    if len(entries) == 1:
        entry = entries[0]
        token = (state_dir(Path(entry["repo_root"])) / "token").read_text(
            encoding="utf-8"
        ).strip()
        return HubEndpoint(entry["url"], token)
    choices = ", ".join(f"{item['repo_root']} ({item['url']})" for item in entries)
    raise DiscoveryError(
        f"cannot choose a hub; set ROBOMATE_HUB_URL and ROBOMATE_TOKEN. "
        f"Registered hubs: {choices or 'none'}"
    )
