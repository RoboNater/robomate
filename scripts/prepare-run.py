#!/usr/bin/env python3
"""Generate a whole user run directory in one command.

Takes a target repository and a run directory and produces the layout the
user guide describes: bootstrapped bob/charlie clones, a state directory with
a reused bearer token, rendered MCP configs from the ``runtimes/`` templates,
a run-local Codex home with its auth link, rendered worker prompts, cheap
prerequisite checks, and paste-ready launch commands plus the Alice kickoff
prompt.

Example:
    uv run --locked python scripts/prepare-run.py \\
        --repository git@github.com:your-org/your-repo.git \\
        --run-dir /absolute/path/to/my-run \\
        --issue 42 --account your-github-username

``--work-file PATH`` replaces ``--issue N`` when the job is not exactly one
issue: the file's text or Markdown statement of work becomes Alice's goal.

Supported worker harnesses are ``claude-code`` and ``codex`` (the paste-ready
pair); anything else fails up front with an actionable message.

Networked runs (#125) separate three addresses: ``--hub-host`` is the hub's
bind address (``HUB_HOST``), ``--hub-url`` the address every worker dials
(``HUB_URL``), and ``--public-url`` the address the agent card advertises
(``HUB_PUBLIC_URL``, defaulting to ``--hub-url``). The hub binds the
``--hub-url`` port (``HUB_PORT``) unless ``--hub-port`` names another, e.g.
behind port forwarding (#133). ``--remote-worker NAME``
leaves that worker to another host, which renders it with
``--worker-only NAME`` from its own robo-agents checkout::

    # hub host (e.g. WSL2)
    uv run --locked python scripts/prepare-run.py --repository ... \\
        --run-dir /abs/run --issue 42 --hub-host 0.0.0.0 \\
        --hub-url http://172.26.115.68:8420 --remote-worker bob
    # worker host (e.g. Windows, Git Bash), as printed by the command above
    uv run --locked python scripts/prepare-run.py --worker-only bob \\
        --repository ... --run-dir C:/runs/my-run \\
        --hub-url http://172.26.115.68:8420 --token-file '\\\\wsl.localhost\\...\\token'

The remote worker is rendered on its own host rather than from the hub host
into a shared directory such as ``/mnt/c``: its clone must be bootstrapped by
that host's git so the identity ``path`` matches ``HUB_WORKSPACE`` exactly, its
harness version must come from that host's CLI, and ``/mnt/c`` (DrvFs without
``metadata``) ignores ``chmod``, so a token written there cannot be kept
owner-only. The worker host only reads the hub's token file; it never mints one.
"""

from __future__ import annotations

import argparse
import hashlib
import ipaddress
import json
import os
import subprocess
import sys
from pathlib import Path, PurePath, PureWindowsPath
from typing import Any
from urllib.parse import urlsplit

from agent_hub_common.config import WILDCARD_HOST_ALIAS

sys.path.insert(0, str(Path(__file__).resolve().parent))
from run_common import (  # noqa: E402
    PROVIDERS,
    ROOT,
    VERSION_COMMANDS,
    bootstrap_clone,
    clone_source,
    codex_home,
    ensure_token,
    link_or_copy,
    parse_github_slug,
    parse_harness_version,
    render_claude_mcp,
    render_codex_config,
    render_worker_prompt,
    run,
    run_output,
    save,
    write_private_text,
)

DEFAULT_BOB_HARNESS = "claude-code"
DEFAULT_CHARLIE_HARNESS = "codex"
#: Worker harnesses prepare-run can render configs *and* print verified
#: paste-ready launch lines for. opencode needs its serve/attach supervisor
#: loop and gemini CLI flags are unverified, so those topologies stay on the
#: manual walkthrough in docs/user-guide.md.
SUPPORTED_HARNESSES = ("claude-code", "codex")
WORKERS = ("bob", "charlie")
DEFAULT_HUB_HOST = "127.0.0.1"
DEFAULT_HUB_PORT = 8420
DEFAULT_HUB_URL = f"http://127.0.0.1:{DEFAULT_HUB_PORT}"


def worker_env(
    name: str,
    harness: str,
    version: str,
    provider: str,
    model: str,
    capabilities: str,
    workspace: str,
    token: str,
    telemetry: str,
    hub_url: str = DEFAULT_HUB_URL,
) -> dict[str, str]:
    return {
        "HUB_URL": hub_url,
        "HUB_TOKEN": token,
        "AGENT_NAME": name,
        "HUB_WORKSPACE": workspace,
        "HUB_HARNESS": harness,
        "HUB_HARNESS_VERSION": version,
        "HUB_PROVIDER": provider,
        "HUB_MODEL": model,
        "HUB_CAPABILITIES": capabilities,
        "HUB_TELEMETRY_LOG": telemetry,
        "PYTHONUTF8": "1",
    }


def check_gh_auth() -> None:
    try:
        run("gh", "auth", "status")
    except (OSError, subprocess.CalledProcessError) as exc:
        raise ValueError(
            "gh is not authenticated (gh auth status failed); run `gh auth login` and retry"
        ) from exc


def repo_settings(slug: str) -> dict[str, Any]:
    try:
        return json.loads(
            run(
                "gh",
                "repo",
                "view",
                slug,
                "--json",
                "viewerPermission,squashMergeAllowed,mergeCommitAllowed,rebaseMergeAllowed",
            )
        )
    except (OSError, subprocess.CalledProcessError) as exc:
        raise ValueError(
            f"cannot read repository settings for {slug}; "
            "check `gh auth status` and the repository name"
        ) from exc


def repo_has_workflows(slug: str) -> bool:
    try:
        payload = json.loads(run("gh", "api", f"repos/{slug}/actions/workflows"))
    except (OSError, subprocess.CalledProcessError) as exc:
        raise ValueError(
            f"cannot list workflows for {slug}; check `gh auth status` and repo access"
        ) from exc
    workflows = payload.get("workflows", []) if isinstance(payload, dict) else []
    return bool(workflows)


def codex_login_status(home: Path) -> str:
    try:
        stdout, stderr = run_output(
            "codex", "login", "status", env={"CODEX_HOME": str(home)}
        )
    except (OSError, subprocess.CalledProcessError):
        return "not logged in (codex login status failed)"
    # Codex CLI prints its status line to stderr on some platforms (exit 0),
    # possibly after WARNING preamble lines; prefer the first non-warning line.
    output = stdout or stderr
    candidates = [line for line in output.splitlines() if line.strip()]
    preferred = [line for line in candidates if not line.lstrip().startswith("WARNING:")]
    line = (preferred or candidates or [""])[0]
    return line if line else "logged in (empty status)"


def url_host(url: str, flag: str) -> str:
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise ValueError(f"{flag} must be an http(s) URL with a host, got {url!r}")
    return parts.hostname


def url_port(url: str, flag: str) -> int:
    """The port a client dials for ``url``: explicit, or the scheme's default."""
    parts = urlsplit(url)
    try:
        port = parts.port
    except ValueError as exc:
        raise ValueError(f"{flag} has an invalid port, got {url!r}") from exc
    if port is None:
        return 443 if parts.scheme == "https" else 80
    return check_port(port, flag)


def check_port(port: int, flag: str) -> int:
    if not 1 <= port <= 65535:
        raise ValueError(f"{flag} must name a port between 1 and 65535, got {port}")
    return port


def host_literal(host: str) -> str:
    return host[1:-1] if host.startswith("[") and host.endswith("]") else host


def is_loopback(host: str) -> bool:
    literal = host_literal(host)
    if literal.lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(literal).is_loopback
    except ValueError:
        return False


def is_wildcard(host: str) -> bool:
    """The unspecified address in any spelling, as ``HubSettings.from_env()`` sees it."""
    literal = host_literal(host)
    if literal == WILDCARD_HOST_ALIAS:
        return True
    try:
        return ipaddress.ip_address(literal).is_unspecified
    except ValueError:
        return False


def network_settings(
    hub_host: str,
    hub_url: str,
    public_url: str | None,
    remote_worker: str | None,
    hub_port: int | None = None,
) -> dict[str, Any]:
    """Validate the bind address, the URL workers dial, and the advertised URL.

    The bind port defaults to the ``--hub-url`` port. A wildcard bind needs a
    dialable, non-loopback advertised URL (mirroring ``HubSettings.from_env()``);
    a hub that binds loopback cannot be dialed at a non-loopback URL; a worker
    on another host cannot dial loopback.
    """
    host = hub_host.strip()
    if not host:
        raise ValueError("--hub-host must not be empty")
    dial = hub_url.strip().rstrip("/")
    advertised = dial if public_url is None else public_url.strip().rstrip("/")
    dial_loopback = is_loopback(url_host(dial, "--hub-url"))
    dial_port = url_port(dial, "--hub-url")
    port = dial_port if hub_port is None else check_port(hub_port, "--hub-port")
    advertised_host = url_host(advertised, "--public-url")
    url_port(advertised, "--public-url")
    if is_wildcard(host) and (is_loopback(advertised_host) or is_wildcard(advertised_host)):
        raise ValueError(
            f"--hub-host {host} binds every interface, so --public-url (default: "
            f"--hub-url) must be a dialable non-loopback URL, got {advertised!r}; "
            "workers cannot dial a bind address"
        )
    if not dial_loopback and is_loopback(host):
        raise ValueError(
            f"--hub-url {dial} is not loopback but --hub-host {host} only binds "
            "loopback; pass --hub-host 0.0.0.0 or the address in --hub-url"
        )
    if remote_worker is not None and dial_loopback:
        raise ValueError(
            f"--remote-worker {remote_worker} dials --hub-url from another host, so it "
            f"cannot be loopback ({dial}); pass the hub host's address, e.g. "
            f"http://<wsl-eth0>:{dial_port}"
        )
    return {"hub_host": host, "hub_port": port, "hub_url": dial, "public_url": advertised}


def preflight_lines(
    hub_url: str, public_url: str | None, hub_port: int | None = None
) -> list[str]:
    """Reachability checks to run on a worker host before launching it.

    ``public_url`` and ``hub_port`` are None under ``--worker-only``, which
    cannot know the hub run's ``--public-url`` or bind port.
    """
    card = (
        f"{public_url}/a2a" if public_url is not None else "the hub run's --public-url + /a2a"
    )
    dial_port = url_port(hub_url, "--hub-url")
    forward = (
        [
            f"The hub binds port {hub_port} but workers dial port {dial_port}; "
            f"forward {dial_port} to {hub_port} before running these checks."
        ]
        if hub_port is not None and hub_port != dial_port
        else []
    )
    return [
        "Preflight (run on each worker host that is not the hub host; "
        "PowerShell needs curl.exe, since curl is an alias there):",
        *forward,
        f"curl.exe -fsS {hub_url}/healthz",
        f"curl.exe -fsS {hub_url}/.well-known/agent-card.json   "
        f"# its url must be {card}",
        f"Warning: if {url_host(hub_url, '--hub-url')} is a WSL2 NAT address (eth0), it "
        "changes whenever WSL restarts and the LAN cannot reach it; re-read it with "
        "`ip -4 -o addr show eth0` and render the run again after a restart.",
    ]


def check_manifest(manifest_path: Path, run_dir: Path, repository: str) -> None:
    if manifest_path.exists():
        previous = json.loads(manifest_path.read_text(encoding="utf-8"))
        if previous.get("run_dir") != str(run_dir):
            raise ValueError("manifest belongs to a different run directory")
        if previous.get("repository") != repository:
            raise ValueError(
                "run directory already prepared for another repository; use a fresh one"
            )


def probe_versions(harnesses: set[str]) -> tuple[dict[str, str], dict[str, str]]:
    """Each harness's ``--version`` output by CLI, and its parsed version by harness."""
    versions: dict[str, str] = {}
    parsed_versions: dict[str, str] = {}
    for harness in sorted(harnesses):
        command = VERSION_COMMANDS[harness]
        try:
            output = run(command, "--version")
        except (OSError, subprocess.CalledProcessError) as exc:
            raise ValueError(
                f"harness {harness!r} requires the {command!r} CLI on PATH "
                f"({command} --version failed); install and authenticate it first"
            ) from exc
        versions[command] = output
        parsed_versions[harness] = parse_harness_version(harness, output)
    return versions, parsed_versions


THROWAWAY_CLOSE_OUT = (
    "close out with no roadmap edit; record the merge only in the workflow summary"
)


def read_work_file(path: Path) -> dict[str, str]:
    """The statement of work, stripped, with the file's path and sha256.

    A missing, unreadable, or empty file is an error.
    """
    try:
        data = path.read_bytes()
        text = data.decode("utf-8").strip()
    except FileNotFoundError as exc:
        raise ValueError(f"--work-file {path} does not exist") from exc
    except (OSError, UnicodeDecodeError) as exc:
        raise ValueError(f"--work-file {path} is not a readable UTF-8 text file") from exc
    if not text:
        raise ValueError(f"--work-file {path} is empty; write the statement of work first")
    return {"text": text, "path": str(path), "sha256": hashlib.sha256(data).hexdigest()}


def render_goal(
    slug: str | None, repository: str, issue: int | None, work: str | None
) -> str:
    """The durable goal: the statement of work, or the one-issue sentence."""
    if work is not None:
        return f"{work}\n\nWhen done, {THROWAWAY_CLOSE_OUT}."
    if issue is not None:
        target = f"{slug or repository}#{issue}"
    else:
        target = "<issue-owner>/<issue-repository>#<issue>"
    return f"Address issue `{target}`, merge its pull request, and {THROWAWAY_CLOSE_OUT}."


def render_alice_prompt(goal: str, account: str | None, policy: dict[str, Any]) -> str:
    prompt = (ROOT / "prompts/alice.md").read_text(encoding="utf-8")
    begin = prompt.index("Goal:")
    end = prompt.index("GitHub comment identity account:")
    prompt = prompt[:begin] + "Goal: " + goal + "\n\n" + prompt[end:]
    prompt = prompt.replace("<account>", account if account else "<account>")
    begin = prompt.index("```json") + len("```json")
    end = prompt.index("```", begin)
    prompt = prompt[:begin] + "\n" + json.dumps(policy, indent=2) + "\n" + prompt[end:]
    return prompt


def codex_launch(home: Path, git_dir: Path, prompt: Path) -> list[str]:
    """Paste-ready ``codex exec`` lines for the current platform.

    The ``VAR=value cmd ... < file`` prefix and ``<`` redirection are POSIX
    shell syntax; PowerShell needs ``$env:`` assignments and pipes the prompt
    through ``Get-Content`` instead.
    """
    if os.name == "nt":
        return [
            f'$env:CODEX_HOME = "{home}"',
            f'Get-Content -Raw "{prompt}" | codex exec --ephemeral -C . '
            f'--add-dir "{git_dir}" --approve-for-me -',
        ]
    return [
        f'CODEX_HOME="{home}" codex exec --ephemeral -C . '
        f'--add-dir "{git_dir}" --approve-for-me - < "{prompt}"',
    ]


def remote_note(name: str) -> str:
    return f"# {name} runs on the worker host: render and launch it there (below)"


def launch_lines(
    run_dir: Path,
    configs: Path,
    alice_runtime: Path,
    bob_dir: Path | None,
    charlie_dir: Path | None,
    bob_harness: str,
    charlie_harness: str,
    remote_worker: str | None = None,
) -> list[str]:
    """Paste-ready launch commands, one block per agent; paths are quoted.

    A remote worker's directory is None; its block is a note pointing at the
    ``--worker-only`` command printed after the launch commands.
    """
    lines = [
        f'cd "{alice_runtime}"',
        f'claude --strict-mcp-config --mcp-config "{configs / "alice.mcp.json"}"',
        "",
    ]
    if remote_worker == "bob" or bob_dir is None:
        lines.append(remote_note("bob"))
    elif bob_harness == "codex":
        lines.append(f'cd "{bob_dir}"')
        lines += codex_launch(
            configs / "bob-codex", bob_dir / ".git", run_dir / "bob.prompt.md"
        )
    else:
        lines.append(f'cd "{bob_dir}"')
        lines.append(
            f'claude --strict-mcp-config --mcp-config "{configs / "bob.mcp.json"}"'
        )
    lines.append("")
    if remote_worker == "charlie" or charlie_dir is None:
        lines.append(remote_note("charlie"))
        return lines
    lines.append(f'cd "{charlie_dir}"')
    if charlie_harness == "codex":
        lines += codex_launch(
            configs / "codex", charlie_dir / ".git", run_dir / "charlie.prompt.md"
        )
    else:
        lines.append(
            f'claude --strict-mcp-config --mcp-config "{configs / "charlie.mcp.json"}"'
        )
    return lines


def codex_home_name(name: str) -> str:
    return "codex" if name == "charlie" else "bob-codex"


def git_bash_path(path: PurePath) -> str:
    """Git Bash spelling of a Windows drive path (``C:/x`` -> ``/c/x``)."""
    drive = path.drive
    if isinstance(path, PureWindowsPath) and len(drive) == 2 and drive[1] == ":":
        return "/" + drive[0].lower() + path.as_posix()[2:]
    return path.as_posix()


def worker_launch(name: str, harness: str, run_dir: PurePath, workspace: PurePath) -> list[str]:
    """A remote worker's launch lines, spelled for its host.

    On a Windows host the lines are for Git Bash: the shell's own ``cd`` and
    ``<`` take Git Bash spellings, while arguments and variables handed to
    native programs (claude, codex) keep forward-slash Windows paths (#75).
    """
    lines = [f'cd "{git_bash_path(workspace)}"']
    configs = run_dir / "configs"
    if harness == "codex":
        home = configs / codex_home_name(name)
        prompt = git_bash_path(run_dir / f"{name}.prompt.md")
        lines.append(
            f'CODEX_HOME="{home.as_posix()}" codex exec --ephemeral -C . '
            f'--add-dir "{(workspace / ".git").as_posix()}" --approve-for-me - < "{prompt}"'
        )
    else:
        config = (configs / f"{name}.mcp.json").as_posix()
        lines.append(f'claude --strict-mcp-config --mcp-config "{config}"')
    return lines


def require_owner_only(path: Path) -> None:
    """Refuse a file its filesystem left group/world-readable, removing it.

    ``save`` and ``write_private_text`` chmod 0600, which a DrvFs mount without
    ``metadata`` (``/mnt/c`` from WSL) silently ignores. On Windows the mode
    bits say nothing; the file inherits the run directory's ACL.
    """
    if os.name != "nt" and path.stat().st_mode & 0o077:
        path.unlink()
        raise ValueError(
            f"{path.parent} ignores POSIX permissions (chmod 0600 left a file readable by "
            "other users); put the run directory on a filesystem that honours them"
        )


def write_secret(path: Path, write: Any) -> None:
    """Write a token-bearing file only where chmod 0600 is known to hold.

    An empty probe file is checked first, so no token reaches a filesystem
    that ignores the mode; the written file is checked again and removed if
    it still came out loose.
    """
    probe = path.with_name(f".{path.name}.permission-probe")
    write_private_text(probe, "")
    require_owner_only(probe)
    probe.unlink()
    write(path)
    require_owner_only(path)


def render_worker_bundle(
    name: str,
    harness: str,
    version: str,
    provider: str,
    model: str,
    capabilities: str,
    token: str,
    hub_url: str,
    root: PurePath,
    run_dir: PurePath,
    workspace: PurePath,
    out_dir: Path,
) -> dict[str, Any]:
    """Render one worker's config and prompt for the host that runs it.

    ``root``, ``run_dir`` and ``workspace`` are that host's paths, written with
    forward slashes; ``out_dir`` is where this process writes ``run_dir``'s
    files. They are the same directory under ``--worker-only``.
    """
    configs = out_dir / "configs"
    configs.mkdir(parents=True, mode=0o700, exist_ok=True)
    env = worker_env(
        name,
        harness,
        version,
        provider,
        model,
        capabilities,
        workspace.as_posix(),
        token,
        (run_dir / f"{name}-telemetry.jsonl").as_posix(),
        hub_url,
    )
    if harness == "codex":
        home = codex_home(configs, codex_home_name(name))
        worker_args = ["run", "--locked", "--directory", root.as_posix(), "worker-mcp"]
        text = render_codex_config(env, worker_args)
        write_secret(home / "config.toml", lambda path: write_private_text(path, text))
        config = run_dir / "configs" / codex_home_name(name) / "config.toml"
    else:
        mcp = render_claude_mcp(env, root.as_posix())
        write_secret(configs / f"{name}.mcp.json", lambda path: save(path, mcp))
        config = run_dir / "configs" / f"{name}.mcp.json"
    (out_dir / f"{name}.prompt.md").write_text(render_worker_prompt(name), encoding="utf-8")
    return {
        "config": config.as_posix(),
        "prompt": (run_dir / f"{name}.prompt.md").as_posix(),
        "launch": worker_launch(name, harness, run_dir, workspace),
    }


def read_hub_token(token_file: Path) -> str:
    """The hub's bearer token, read in place; the worker host never mints one."""
    try:
        if os.name != "nt" and token_file.stat().st_mode & 0o077:
            raise ValueError(
                f"--token-file {token_file} must be owner-only (mode 0600), as the hub requires"
            )
        token = token_file.read_text(encoding="utf-8").strip()
    except FileNotFoundError as exc:
        raise ValueError(
            f"--token-file {token_file} does not exist; pass the hub's <state-dir>/token "
            "as this host reads it"
        ) from exc
    except (OSError, UnicodeDecodeError) as exc:
        raise ValueError(f"--token-file {token_file} is not readable") from exc
    if not token:
        raise ValueError(f"--token-file {token_file} is empty")
    return token


def remote_token_path(token_path: Path) -> str:
    """The hub's token file as a Windows host reads it, when the hub runs in WSL."""
    distro = os.environ.get("WSL_DISTRO_NAME")
    if distro:
        return "\\\\wsl.localhost\\" + distro + str(token_path).replace("/", "\\")
    return str(token_path)


def worker_only_command(
    name: str,
    repository: str,
    hub_url: str,
    token_path: Path,
    harness: str,
    model: str,
    provider: str | None,
    capabilities: str,
) -> str:
    """The ``--worker-only`` command to run on the remote worker's host.

    Single-quoted values read literally in both Git Bash and PowerShell.
    """
    args = [
        "uv run --locked python scripts/prepare-run.py",
        f"--worker-only {name}",
        f"--repository '{repository}'",
        "--run-dir '<absolute run directory on the worker host>'",
        f"--hub-url '{hub_url}'",
        f"--token-file '{remote_token_path(token_path)}'",
        f"--{name} {harness}",
    ]
    if model:
        args.append(f"--{name}-model '{model}'")
    if provider:
        args.append(f"--{name}-provider '{provider}'")
    if capabilities:
        args.append(f"--{name}-capabilities '{capabilities}'")
    return " ".join(args)


def prepare(
    repository: str,
    run_dir: Path,
    issue: int | None = None,
    account: str | None = None,
    bob_harness: str = DEFAULT_BOB_HARNESS,
    charlie_harness: str = DEFAULT_CHARLIE_HARNESS,
    bob_model: str = "",
    charlie_model: str = "",
    bob_provider: str | None = None,
    charlie_provider: str | None = None,
    bob_capabilities: str = "",
    charlie_capabilities: str = "",
    bob_dir: Path | None = None,
    charlie_dir: Path | None = None,
    state_dir: Path | None = None,
    merge_method: str = "squash",
    allow_no_ci: str = "auto",
    skip_github_checks: bool = False,
    public_url: str | None = None,
    work_file: Path | None = None,
    hub_host: str = DEFAULT_HUB_HOST,
    hub_url: str = DEFAULT_HUB_URL,
    remote_worker: str | None = None,
    hub_port: int | None = None,
) -> dict[str, Any]:
    if work_file is not None and issue is not None:
        raise ValueError(
            "--work-file and --issue are mutually exclusive; name the issue inside "
            "the statement of work, or pass --issue alone"
        )
    statement = read_work_file(work_file.resolve()) if work_file is not None else None
    work_text = statement["text"] if statement is not None else None
    if not run_dir.is_absolute() or run_dir != run_dir.resolve():
        raise ValueError("RUN_DIR must be absolute and canonical")
    if run_dir == ROOT or ROOT in run_dir.parents:
        raise ValueError("RUN_DIR must be outside the coordination checkout")
    if not repository:
        raise ValueError("--repository must not be empty")
    for label, harness in (("bob", bob_harness), ("charlie", charlie_harness)):
        if harness not in SUPPORTED_HARNESSES:
            raise ValueError(
                f"--{label} harness {harness!r} is not yet supported by prepare-run; "
                f"expected one of {list(SUPPORTED_HARNESSES)} "
                "(assemble other topologies via the manual walkthrough in "
                "docs/user-guide.md)"
            )
    if merge_method not in ("squash", "merge", "rebase"):
        raise ValueError("--merge-method must be one of squash, merge, rebase")
    if allow_no_ci not in ("auto", "true", "false"):
        raise ValueError("--allow-no-ci must be one of auto, true, false")
    if remote_worker is not None and remote_worker not in WORKERS:
        raise ValueError(f"--remote-worker must be one of {list(WORKERS)}")
    network = network_settings(hub_host, hub_url, public_url, remote_worker, hub_port)
    networked = remote_worker is not None or network != {
        "hub_host": DEFAULT_HUB_HOST,
        "hub_port": DEFAULT_HUB_PORT,
        "hub_url": DEFAULT_HUB_URL,
        "public_url": DEFAULT_HUB_URL,
    }
    local = [name for name in WORKERS if name != remote_worker]
    if remote_worker is not None and (bob_dir if remote_worker == "bob" else charlie_dir):
        raise ValueError(
            f"--{remote_worker}-dir names a clone on this host; pass the remote clone "
            f"to --worker-only {remote_worker} on the worker host instead"
        )

    providers = {
        "bob": bob_provider or PROVIDERS[bob_harness],
        "charlie": charlie_provider or PROVIDERS[charlie_harness],
    }

    bob_path = bob_dir or (run_dir / "bob")
    charlie_path = charlie_dir or (run_dir / "charlie")
    for path, label in ((bob_path, "--bob-dir"), ((charlie_path), "--charlie-dir")):
        if not path.is_absolute() or path != path.resolve():
            raise ValueError(f"{label} must be absolute and canonical")
    if bob_path == charlie_path:
        raise ValueError("--bob-dir and --charlie-dir must differ")
    resolved_state = state_dir or (run_dir / "hub-state")
    if not resolved_state.is_absolute() or resolved_state != resolved_state.resolve():
        raise ValueError("--state-dir must be absolute and canonical")

    harnesses = {"bob": bob_harness, "charlie": charlie_harness}
    # Harness versions come from the CLIs themselves, never placeholders; a
    # remote worker's version is probed on its own host by --worker-only.
    versions, parsed_versions = probe_versions({harnesses[name] for name in local})

    slug = parse_github_slug(repository)
    # A bare owner/repo slug passes the gh checks below but is not a valid
    # `git clone` argument; expand it to its https URL for bootstrapping.
    clone_from = clone_source(repository, slug)
    if slug is None and not skip_github_checks:
        # Local paths (e.g. disposable test origins) have no GitHub API surface.
        skip_github_checks = True
    if not skip_github_checks:
        check_gh_auth()
    policy_allow_no_ci = False
    merge_note = "skipped (local repository or --skip-github-checks)"
    ci_note = "skipped (local repository or --skip-github-checks)"
    if slug is not None and not skip_github_checks:
        settings = repo_settings(slug)
        if settings.get("viewerPermission") not in ("ADMIN", "MAINTAIN", "WRITE"):
            raise ValueError(
                f"repository {slug} requires push access "
                f"(viewerPermission {settings.get('viewerPermission')!r}); "
                "check `gh auth status` and repo permissions"
            )
        allowed = {
            "squash": settings.get("squashMergeAllowed"),
            "merge": settings.get("mergeCommitAllowed"),
            "rebase": settings.get("rebaseMergeAllowed"),
        }
        if not allowed[merge_method]:
            raise ValueError(
                f"repository {slug} does not allow the {merge_method!r} merge method; "
                "enable it in repository settings or pass another --merge-method"
            )
        merge_note = f"{slug} allows {merge_method}"
        has_workflows = repo_has_workflows(slug)
        ci_note = (
            f"{slug} has workflows (allow_no_ci=false)"
            if has_workflows
            else f"{slug} has no workflows (allow_no_ci=true)"
        )
        detected = not has_workflows
        policy_allow_no_ci = detected if allow_no_ci == "auto" else allow_no_ci == "true"
    else:
        if allow_no_ci == "auto":
            policy_allow_no_ci = slug is None
            if slug is None:
                ci_note = "local repository has no GitHub workflows (allow_no_ci=true)"
        else:
            policy_allow_no_ci = allow_no_ci == "true"

    run_dir.mkdir(parents=True, mode=0o700, exist_ok=True)
    manifest_path = run_dir / "run.json"
    check_manifest(manifest_path, run_dir, repository)

    # Bootstrap before minting the token so a failed clone leaves no state behind.
    paths = {"bob": bob_path, "charlie": charlie_path}
    workspaces = {name: bootstrap_clone(name, paths[name], clone_from) for name in local}
    token = ensure_token(resolved_state / "token")
    if len(local) == 2 and (
        workspaces["bob"]["workspace_id"] == workspaces["charlie"]["workspace_id"]
    ):
        raise ValueError("bob and charlie must have distinct workspace IDs")

    configs = run_dir / "configs"
    configs.mkdir(mode=0o700, exist_ok=True)

    models = {"bob": bob_model, "charlie": charlie_model}
    capabilities = {"bob": bob_capabilities, "charlie": charlie_capabilities}
    rendered_configs: dict[str, str] = {}

    for name in local:
        harness = harnesses[name]
        # Keep the drive-letter case bootstrap printed; read_identity compares the string.
        workspace = workspaces[name]["path"]
        telemetry = str(run_dir / f"{name}-telemetry.jsonl")
        env = worker_env(
            name,
            harness,
            parsed_versions[harness],
            providers[name],
            models[name],
            capabilities[name],
            workspace,
            token,
            telemetry,
            network["hub_url"],
        )
        if harness == "codex":
            home = codex_home(configs, codex_home_name(name))
            worker_args = ["run", "--locked", "--directory", str(ROOT), "worker-mcp"]
            write_private_text(home / "config.toml", render_codex_config(env, worker_args))
            rendered_configs[name] = str(home / "config.toml")
        else:
            save(configs / f"{name}.mcp.json", render_claude_mcp(env))
            rendered_configs[name] = str(configs / f"{name}.mcp.json")

    hub_env = {
        "HUB_STATE_DIR": str(resolved_state),
        "HUB_TOKEN": token,
        "HUB_PUBLIC_URL": network["public_url"],
        "HUB_GUIDES_DIR": str(ROOT / "guides"),
        "PYTHONUTF8": "1",
    }
    if network["hub_host"] != DEFAULT_HUB_HOST:
        hub_env["HUB_HOST"] = network["hub_host"]
    if network["hub_port"] != DEFAULT_HUB_PORT:
        hub_env["HUB_PORT"] = str(network["hub_port"])
    hub_args = ["run", "--locked", "--directory", str(ROOT), "hub"]
    save(
        configs / "alice.mcp.json",
        {"mcpServers": {"hub": {"command": "uv", "args": hub_args, "env": hub_env}}},
    )

    # Alice's working directory, kept apart from the worker clones and the
    # token: the orchestrator skill is linked in run-locally (as in step6), so
    # the quickstart needs no user-wide skill installation.
    alice_runtime = run_dir / "alice-runtime"
    (alice_runtime / ".claude" / "skills").mkdir(parents=True, exist_ok=True)
    link_or_copy(
        ROOT / "skills/alice-orchestrator",
        alice_runtime / ".claude" / "skills" / "alice-orchestrator",
    )

    for name in local:
        (run_dir / f"{name}.prompt.md").write_text(
            render_worker_prompt(name), encoding="utf-8"
        )

    policy = {
        "max_review_rounds": 3,
        "merge_method": merge_method,
        "allow_no_ci": policy_allow_no_ci,
        "role_policy": {
            "reviewer_harness_differs": bob_harness != charlie_harness,
            # Baseline from prompts/alice.md; Alice still observes any real
            # provider difference at pairing time via check-in profiles.
            "reviewer_provider_differs": False,
            "implementer_capabilities": capabilities["bob"].split(",")
            if capabilities["bob"]
            else [],
            "reviewer_capabilities": capabilities["charlie"].split(",")
            if capabilities["charlie"]
            else [],
        },
        "pairing_wait_s": 120,
        "max_wall_minutes": 180,
        "max_task_lease_min": 120,
    }
    goal = render_goal(slug, repository, issue, work_text)
    (run_dir / "alice.prompt.md").write_text(
        render_alice_prompt(goal, account, policy), encoding="utf-8"
    )
    # The goal text is what Alice initializes with; path and hash tie it to the file.
    work = {
        "goal": goal,
        "path": statement["path"] if statement is not None else None,
        "sha256": statement["sha256"] if statement is not None else None,
    }

    manifest = {
        "schema_version": 1,
        "repository": repository,
        "clone_repository": clone_from,
        "slug": slug,
        "run_dir": str(run_dir),
        "state_dir": str(resolved_state),
        "workspaces": workspaces,
        "harnesses": harnesses,
        "providers": providers,
        "models": models,
        "versions": versions,
        "policy": policy,
        "work": work,
        "issue": issue,
        "account": account,
        "merge_method": merge_method,
        "configs": rendered_configs,
    }
    if networked:
        manifest["network"] = {**network, "remote_worker": remote_worker}
    save(manifest_path, manifest)

    codex_auth: dict[str, str] = {}
    for name in local:
        if harnesses[name] == "codex":
            home_name = codex_home_name(name)
            codex_auth[name] = f"{home_name}: {codex_login_status(configs / home_name)}"
    if not codex_auth:
        codex_auth = {"codex": "no codex worker in this topology"}

    report: dict[str, Any] = {
        "run_dir": str(run_dir),
        "repository": repository,
        "slug": slug,
        "issue": issue,
        "workspaces": {name: workspaces[name]["path"] for name in local},
        "configs": {"alice": str(configs / "alice.mcp.json"), **rendered_configs},
        "prompts": {
            name: str(run_dir / f"{name}.prompt.md") for name in ("alice", *local)
        },
        "checks": {
            "gh_auth": "ok" if not skip_github_checks else "skipped",
            "versions": versions,
            "merge": merge_note,
            "ci": ci_note,
            "codex_auth": codex_auth,
        },
    }
    if networked:
        report["network"] = manifest["network"]
    print(json.dumps(report, indent=2, sort_keys=True))
    print("\nLaunch commands (paste in order):")
    for line in launch_lines(
        run_dir,
        configs,
        alice_runtime,
        Path(workspaces["bob"]["path"]) if "bob" in workspaces else None,
        Path(workspaces["charlie"]["path"]) if "charlie" in workspaces else None,
        bob_harness,
        charlie_harness,
        remote_worker,
    ):
        print(line if line else "")
    if remote_worker is not None:
        print(
            f"\nRemote worker {remote_worker}: on its host, from a robo-agents checkout "
            "at this commit, run (then launch it with the lines that prints):"
        )
        print(
            worker_only_command(
                remote_worker,
                repository,
                network["hub_url"],
                resolved_state / "token",
                harnesses[remote_worker],
                models[remote_worker],
                bob_provider if remote_worker == "bob" else charlie_provider,
                capabilities[remote_worker],
            )
        )
    if not is_loopback(url_host(network["hub_url"], "--hub-url")):
        print()
        for line in preflight_lines(
            network["hub_url"], network["public_url"], network["hub_port"]
        ):
            print(line)
    print(f"\nAlice kickoff prompt: {run_dir / 'alice.prompt.md'}")
    if (issue is None and work_text is None) or account is None:
        print("Fill any remaining <issue>/<account> placeholders before launching Alice.")
    return manifest


def prepare_worker(
    name: str,
    repository: str,
    run_dir: Path,
    hub_url: str,
    token_file: Path,
    harness: str,
    model: str = "",
    provider: str | None = None,
    capabilities: str = "",
    worker_dir: Path | None = None,
) -> dict[str, Any]:
    """Render one remote worker on the host that runs it (``--worker-only``).

    Reads the hub's token file in place, bootstraps the clone with this host's
    git, and renders the config, prompt and launch lines with this host's paths.
    """
    if name not in WORKERS:
        raise ValueError(f"--worker-only must be one of {list(WORKERS)}")
    if harness not in SUPPORTED_HARNESSES:
        raise ValueError(
            f"--{name} harness {harness!r} is not yet supported by prepare-run; "
            f"expected one of {list(SUPPORTED_HARNESSES)}"
        )
    if not repository:
        raise ValueError("--repository must not be empty")
    if not run_dir.is_absolute() or run_dir != run_dir.resolve():
        raise ValueError("RUN_DIR must be absolute and canonical")
    if run_dir == ROOT or ROOT in run_dir.parents:
        raise ValueError("RUN_DIR must be outside the coordination checkout")
    worker_path = worker_dir or (run_dir / name)
    if not worker_path.is_absolute() or worker_path != worker_path.resolve():
        raise ValueError(f"--{name}-dir must be absolute and canonical")
    dial = hub_url.strip().rstrip("/")
    if is_loopback(url_host(dial, "--hub-url")):
        raise ValueError(
            f"--worker-only {name} dials the hub from another host, so --hub-url cannot "
            f"be loopback ({dial}); pass the hub host's address"
        )
    url_port(dial, "--hub-url")
    token = read_hub_token(token_file)
    versions, parsed_versions = probe_versions({harness})

    slug = parse_github_slug(repository)
    clone_from = clone_source(repository, slug)
    run_dir.mkdir(parents=True, mode=0o700, exist_ok=True)
    manifest_path = run_dir / "run.json"
    check_manifest(manifest_path, run_dir, repository)
    identity = bootstrap_clone(name, worker_path, clone_from)
    resolved_provider = provider or PROVIDERS[harness]
    # Keep the drive-letter case bootstrap printed; read_identity compares the string.
    bundle = render_worker_bundle(
        name,
        harness,
        parsed_versions[harness],
        resolved_provider,
        model,
        capabilities,
        token,
        dial,
        ROOT,
        run_dir,
        Path(identity["path"]),
        run_dir,
    )
    manifest = {
        "schema_version": 1,
        "worker_only": name,
        "repository": repository,
        "clone_repository": clone_from,
        "slug": slug,
        "run_dir": str(run_dir),
        "hub_url": dial,
        "workspaces": {name: identity},
        "harnesses": {name: harness},
        "providers": {name: resolved_provider},
        "models": {name: model},
        "versions": versions,
        "configs": {name: bundle["config"]},
    }
    save(manifest_path, manifest)
    checks: dict[str, Any] = {"versions": versions}
    if harness == "codex":
        home_name = codex_home_name(name)
        checks["codex_auth"] = {
            name: f"{home_name}: {codex_login_status(run_dir / 'configs' / home_name)}"
        }
    print(
        json.dumps(
            {
                "run_dir": str(run_dir),
                "worker_only": name,
                "hub_url": dial,
                "workspace": identity["path"],
                "config": bundle["config"],
                "prompt": bundle["prompt"],
                "checks": checks,
            },
            indent=2,
            sort_keys=True,
        )
    )
    print()
    for line in preflight_lines(dial, None):
        print(line)
    print(f"\nLaunch command for {name} (paste after the preflight passes):")
    for line in bundle["launch"]:
        print(line)
    return manifest


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repository", required=True)
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument("--issue", type=int, default=None)
    parser.add_argument(
        "--work-file",
        type=Path,
        default=None,
        help="text or Markdown statement of work used as Alice's goal (not with --issue)",
    )
    parser.add_argument("--account", default=None)
    parser.add_argument("--bob", default=DEFAULT_BOB_HARNESS)
    parser.add_argument("--charlie", default=DEFAULT_CHARLIE_HARNESS)
    parser.add_argument("--bob-model", default="")
    parser.add_argument("--charlie-model", default="")
    parser.add_argument("--bob-provider", default=None)
    parser.add_argument("--charlie-provider", default=None)
    parser.add_argument("--bob-capabilities", default="")
    parser.add_argument("--charlie-capabilities", default="")
    parser.add_argument("--bob-dir", type=Path, default=None)
    parser.add_argument("--charlie-dir", type=Path, default=None)
    parser.add_argument("--state-dir", type=Path, default=None)
    parser.add_argument("--merge-method", default="squash")
    parser.add_argument("--allow-no-ci", default="auto")
    parser.add_argument("--skip-github-checks", action="store_true")
    parser.add_argument(
        "--hub-host",
        default=DEFAULT_HUB_HOST,
        help="hub bind address (HUB_HOST); 0.0.0.0 needs a non-loopback --public-url",
    )
    parser.add_argument(
        "--hub-url", default=DEFAULT_HUB_URL, help="the HUB_URL every worker dials"
    )
    parser.add_argument(
        "--hub-port",
        type=int,
        default=None,
        help="hub bind port (HUB_PORT), 1-65535; defaults to the --hub-url port",
    )
    parser.add_argument(
        "--public-url",
        default=None,
        help="address the agent card advertises (HUB_PUBLIC_URL); defaults to --hub-url",
    )
    parser.add_argument(
        "--remote-worker",
        choices=WORKERS,
        default=None,
        help="leave this worker to another host, rendered there with --worker-only",
    )
    parser.add_argument(
        "--worker-only",
        choices=WORKERS,
        default=None,
        help="on a remote worker's host: render only this worker (needs --token-file)",
    )
    parser.add_argument(
        "--token-file",
        type=Path,
        default=None,
        help="with --worker-only: the hub's token file as this host reads it",
    )
    args = parser.parse_args()
    if args.worker_only is not None:
        hub_only = {
            "--issue": args.issue is not None,
            "--work-file": args.work_file is not None,
            "--account": args.account is not None,
            "--state-dir": args.state_dir is not None,
            "--remote-worker": args.remote_worker is not None,
            "--hub-host": args.hub_host != DEFAULT_HUB_HOST,
            "--hub-port": args.hub_port is not None,
            "--public-url": args.public_url is not None,
        }
        if given := [flag for flag, present in hub_only.items() if present]:
            parser.error(f"hub-host flags do not apply to --worker-only: {', '.join(given)}")
        if args.token_file is None:
            parser.error("--worker-only needs --token-file, the hub's token file")
        name = args.worker_only
        try:
            prepare_worker(
                name,
                args.repository,
                args.run_dir,
                args.hub_url,
                args.token_file,
                getattr(args, name),
                getattr(args, f"{name}_model"),
                getattr(args, f"{name}_provider"),
                getattr(args, f"{name}_capabilities"),
                getattr(args, f"{name}_dir"),
            )
        except ValueError as exc:
            sys.exit(f"prepare-run: error: {exc}")
        return
    if args.token_file is not None:
        parser.error("--token-file is only for --worker-only")
    try:
        prepare(
            args.repository,
            args.run_dir,
            args.issue,
            args.account,
            args.bob,
            args.charlie,
            args.bob_model,
            args.charlie_model,
            args.bob_provider,
            args.charlie_provider,
            args.bob_capabilities,
            args.charlie_capabilities,
            args.bob_dir,
            args.charlie_dir,
            args.state_dir,
            args.merge_method,
            args.allow_no_ci,
            args.skip_github_checks,
            args.public_url,
            args.work_file,
            args.hub_host,
            args.hub_url,
            args.remote_worker,
            args.hub_port,
        )
    except ValueError as exc:
        sys.exit(f"prepare-run: error: {exc}")


if __name__ == "__main__":
    main()
