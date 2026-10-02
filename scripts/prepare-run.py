#!/usr/bin/env python3
"""Generate a whole user run directory in one command.

Takes a target repository and a run directory and produces the layout the
user guide describes: bootstrapped bob/charlie clones, a connection to an
existing ``robomate up`` hub, rendered MCP configs from the ``runtimes/`` templates,
a run-local Codex home with its auth link, rendered worker prompts, cheap
prerequisite checks, and one ``start-<agent>.sh`` script per agent plus the
Alice kickoff prompt.

Example:
    uv run --locked python scripts/prepare-run.py \\
        --hub-repo /absolute/path/to/target-repository \\
        --repository git@github.com:your-org/your-repo.git \\
        --run-dir /absolute/path/to/my-run \\
        --issue 42 --account your-github-username

``--work-file PATH`` replaces ``--issue N`` when the job is not exactly one
issue: the file's text or Markdown statement of work becomes Alice's goal.

Each agent runs under Claude Code, Codex, OpenCode, or AntiGravity
(``--<agent>-harness claude|codex|opencode|antigravity``, case-insensitive),
with an optional ``--<agent>-model`` and ``--<agent>-effort``
passed through to its CLI unchecked. Start scripts hand each agent its prompt
file (auto-start) unless ``--no-auto-start`` is given (#30). Any other harness
fails up front with an actionable message.

For networked runs, start ``robomate up --bind ... --public-url ...`` in the
target repository first. The existing hub URL is read from ``--hub-repo``;
``--hub-url`` and ``--public-url`` can only confirm that URL. ``--remote-worker NAME``
leaves that worker to another host, which renders it with
``--worker-only NAME`` from its own robo-agents checkout::

    # hub host (e.g. WSL2)
    uv run --locked python scripts/prepare-run.py --repository ... \\
        --hub-repo /abs/target --run-dir /abs/run --issue 42 --remote-worker bob
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
import contextlib
import hashlib
import ipaddress
import json
import os
import re
import shlex
import sqlite3
import subprocess
import sys
from pathlib import Path, PurePath, PureWindowsPath
from typing import Any
from urllib.parse import urlsplit

from agent_hub_common.discovery import read_hub_json
from agent_hub_common.registry import hub_healthy, process_alive

sys.path.insert(0, str(Path(__file__).resolve().parent))
from run_common import (  # noqa: E402
    ROOT,
    VERSION_COMMANDS,
    agy_home,
    agy_mcp_path,
    bootstrap_clone,
    clone_source,
    codex_home,
    codex_mcp,
    codex_sandbox,
    link_or_copy,
    parse_github_slug,
    parse_harness_version,
    render_antigravity_mcp,
    render_claude_mcp,
    render_codex_config,
    render_opencode_config,
    render_worker_prompt,
    resolve_provider,
    run,
    run_output,
    save,
    write_private_text,
)

DEFAULT_ALICE_HARNESS = "claude-code"
DEFAULT_BOB_HARNESS = "claude-code"
DEFAULT_CHARLIE_HARNESS = "codex"
DEFAULT_HARNESSES = {
    "alice": DEFAULT_ALICE_HARNESS,
    "bob": DEFAULT_BOB_HARNESS,
    "charlie": DEFAULT_CHARLIE_HARNESS,
}
#: Harnesses prepare-run can render configs *and* verified start scripts for,
#: by the case-insensitive spellings ``--<agent>-harness`` accepts.
HARNESS_NAMES = {
    "claude": "claude-code",
    "claude-code": "claude-code",
    "codex": "codex",
    "opencode": "opencode",
    "antigravity": "antigravity",
    "agy": "antigravity",
}
WORKERS = ("bob", "charlie")
AGENTS = ("alice", *WORKERS)
#: The hub's MCP tools, which a Codex or AntiGravity Alice's config enables and approves.
ALICE_TOOLS = [
    "get_state",
    "initialize_workflow",
    "wait_for_event",
    "assign_task",
    "check_merge_gate",
    "reply",
    "set_task_state",
    "release_agent",
    "set_workflow_status",
    "log_decision",
]
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
    token_file: str,
    telemetry: str,
    hub_url: str = DEFAULT_HUB_URL,
) -> dict[str, str]:
    return {
        "ROBOMATE_HUB_URL": hub_url,
        "ROBOMATE_TOKEN_FILE": token_file,
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


def agy_login_status(home: Path) -> str:
    """Credential status for an isolated AntiGravity home."""
    cli_dir = home / ".gemini" / "antigravity-cli"
    if (cli_dir / "antigravity-oauth-token").exists():
        return "logged in (antigravity-oauth-token)"
    if os.name == "nt" and (cli_dir / "jetski_state.pbtxt").exists():
        return "logged in (jetski_state.pbtxt)"
    return "not logged in (missing ~/.gemini/antigravity-cli/antigravity-oauth-token)"


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


def check_hub_workflow(
    hub_repo: Path, run_dir: Path, goal: str, policy: dict[str, Any] | None = None
) -> None:
    """Refuse a different run on a hub whose single workflow is initialized."""
    path = hub_repo / ".robomate" / "hub.db"
    if not path.exists():
        return
    try:
        with contextlib.closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)) as db:
            row = db.execute("SELECT goal, status, policy_json FROM workflow LIMIT 1").fetchone()
    except sqlite3.Error as exc:
        raise ValueError(f"cannot read workflow from {path}: {exc}") from exc
    if row is None:
        return
    manifest_path = run_dir / "run.json"
    if manifest_path.exists():
        previous = json.loads(manifest_path.read_text(encoding="utf-8"))
        if (previous.get("run_dir") == str(run_dir)
                and previous.get("hub_repo") == str(hub_repo)
                and previous.get("state_dir") == str(path.parent)
                and previous.get("work", {}).get("goal") == goal == row[0]):
            if policy is not None and json.loads(row[2]) != policy:
                raise ValueError(
                    f"hub in {hub_repo} already has workflow status={row[1]!r}, "
                    f"goal={row[0]!r}, but its policy differs; resume with the "
                    "original preparation options (including merge method, harnesses, "
                    "capabilities, and CI setting), or use a fresh dedicated target clone"
                )
            return
    raise ValueError(
        f"hub in {hub_repo} already has workflow status={row[1]!r}, goal={row[0]!r}; "
        "for a new run, stop this hub with `robomate down`, use a fresh dedicated "
        "target clone as --hub-repo, run `robomate up` there, and retry "
        "(docs/user-guide.md: Start the hub and prepare a run). To resume this "
        "workflow, rerun preparation with its original run directory and goal"
    )


def harness_name(flag: str, value: str) -> str:
    """The harness ``value`` names, case-insensitively; ``claude`` is ``claude-code``."""
    harness = HARNESS_NAMES.get(value.strip().lower())
    if harness is None:
        raise ValueError(
            f"{flag} {value!r} is not yet supported by prepare-run; expected claude, "
            "codex, opencode, or antigravity (assemble other topologies via the manual "
            "walkthrough in docs/user-guide.md)"
        )
    return harness


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


def resolve_roadmap(roadmap: str | None, slug: str | None, repository: str) -> str | None:
    """Render ``--roadmap`` as Alice's goal names it, or None when omitted (#34).

    A bare number (digits, optionally with a leading ``#``) names that issue of
    the repository prepare-run is operating on (``slug or repository``); any
    other value (``OWNER/REPO#N`` and the like) passes through verbatim for
    Alice and bob to resolve at close-out.
    """
    if roadmap is None:
        return None
    if not roadmap.strip():
        raise ValueError("--roadmap must not be empty")
    match = re.fullmatch(r"#?(\d+)", roadmap)
    if match:
        return f"{slug or repository}#{match.group(1)}"
    return roadmap


def render_goal(
    slug: str | None,
    repository: str,
    issue: int | None,
    work: str | None,
    roadmap: str | None = None,
) -> str:
    """The durable goal: the statement of work, or the one-issue sentence (#34)."""
    rendered = resolve_roadmap(roadmap, slug, repository)
    if rendered is not None:
        instructions = (
            f"The implementer bob should make a decision on what roadmap ({rendered}) "
            "updates are necessary, if any, when they open the PR and include it as a "
            "PR comment so it can be reviewed. After the merge the implementer bob "
            "should update the roadmap per the adjudicated PR if necessary. Make sure "
            "you include that in bob's initial tasking."
        )
        if work is not None:
            return f"{work}\n\nWhen done, close out. {instructions}"
        if issue is not None:
            target = f"{slug or repository}#{issue}"
        else:
            target = "<issue-owner>/<issue-repository>#<issue>"
        return f"Address issue `{target}`, merge its pull request, and close out. {instructions}"
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


def prompt_sections(name: str, harness: str) -> str:
    """Harness notes and the post-release close-out appended to a rendered prompt (#30).

    Claude Code backgrounds any tool call still running at 120 s, so a Claude
    agent is told to keep every hub wait at 100 s.
    """
    sections = []
    if harness == "claude-code":
        waits = "`wait_for_event`" if name == "alice" else "`await_assignment`"
        sections.append(
            f"Use a 100 second wait time for all hub waits including {waits}. This "
            "prevents the wait from being shifted to a background task by the harness."
        )
    elif name == "alice":
        skill = ROOT / "skills/alice-orchestrator/SKILL.md"
        runtime_label = {
            "codex": "Codex",
            "opencode": "OpenCode",
            "antigravity": "AntiGravity",
        }.get(harness, harness)
        sections.append(
            f"{runtime_label} runtime note: if the alice-orchestrator skill is not already loaded, "
            f"read {skill} in full and follow it. Hub tools are the MCP server `robomate`."
        )
    if name == "alice":
        sections.append(
            "After you release bob and charlie, please write your summary/closeout report "
            "to `closeout-report-alice.md`. Also add any outstanding items which weren't "
            "addressed, and any lessons learned worth mentioning to the closeout report."
        )
    else:
        sections.append(
            "After you are released by Alice, please write your summary/closeout report to "
            f"`closeout-report-{name}.md`. Also add any outstanding items which weren't "
            "addressed, and any lessons learned worth mentioning to the closeout report. "
            "This file should be left uncommitted."
        )
    return "".join(f"\n{section}\n" for section in sections)


def shell_word(value: str, powershell: bool = False) -> str:
    """``value`` as one literal shell word, quoted only when it needs it.

    Both shells expand ``$`` inside double quotes, so a path is single-quoted.
    """
    if not powershell:
        return shlex.quote(value)
    return value if re.fullmatch(r"[\w.-]+", value) else "'" + value.replace("'", "''") + "'"


def auto_start_instruction(prompt: str, powershell: bool = False) -> str:
    return shell_word(f"Read {prompt} and follow the instructions in it", powershell)


def model_flags(
    harness: str,
    model: str,
    effort: str,
    powershell: bool = False,
    auto_start: bool = True,
) -> list[str]:
    """The CLI flags selecting ``model`` and reasoning ``effort``; empty ones are omitted."""
    flags = ["--model", shell_word(model, powershell)] if model else []
    if effort and harness == "codex":
        # -c parses its value as TOML, falling back to the raw string, so the
        # bare form serves PowerShell, which strips embedded double quotes.
        quoted = effort if powershell else f'"{effort}"'
        flags += ["-c", shell_word(f"model_reasoning_effort={quoted}", powershell)]
    elif effort and harness == "opencode":
        # `--variant` is an `opencode run` option; interactive `opencode`
        # (`--no-auto-start`) rejects it.
        if not auto_start:
            raise ValueError(
                "opencode only accepts --variant (--<agent>-effort) under auto-start "
                "(opencode run); omit --<agent>-effort with --no-auto-start"
            )
        flags += ["--variant", shell_word(effort, powershell)]
    elif effort:
        flags += ["--effort", shell_word(effort, powershell)]
    return flags


def claude_launch(
    config: str,
    prompt: str,
    prompt_dir: str,
    flags: list[str],
    auto_start: bool,
    powershell: bool = False,
) -> list[str]:
    """A ``claude`` launch; auto-start runs it in print mode on its prompt file.

    ``--add-dir`` lets Claude read the prompt when it is outside the clone.
    """
    words = ["claude", *flags]
    if auto_start:
        words += ["--permission-mode", "auto"]
    words += [
        "--strict-mcp-config",
        "--mcp-config",
        shell_word(config, powershell),
        "--add-dir",
        shell_word(prompt_dir, powershell),
    ]
    if auto_start:
        words += ["-p", auto_start_instruction(prompt, powershell)]
    return [" ".join(words)]


def codex_launch(
    home: str,
    git_dir: str | None,
    prompt: str,
    flags: list[str],
    auto_start: bool,
    powershell: bool = False,
) -> list[str]:
    """A ``codex`` launch; auto-start pipes the prompt file into ``codex exec``.

    Sessions are kept (no ``--ephemeral``) so they can be inspected after the
    run. The ``VAR=value cmd ... < file`` prefix and ``<`` redirection are POSIX
    shell syntax; PowerShell needs ``$env:`` assignments and pipes the prompt
    through ``Get-Content`` instead. Without a ``git_dir`` (Alice's runtime is
    not a clone) ``--skip-git-repo-check`` is passed to ``codex exec`` so it
    starts outside a Git repo (#45); plain interactive ``codex``
    (``--no-auto-start``) rejects that flag, so it is omitted there.
    """
    words = ["codex", "exec"] if auto_start else ["codex"]
    words += ["-C", "."]
    if git_dir is not None:
        words += ["--add-dir", shell_word(git_dir, powershell)]
    elif auto_start:
        words += ["--skip-git-repo-check"]
    words += ["--approve-for-me", *flags]
    command = " ".join(words)
    home_word = shell_word(home, powershell)
    prompt_word = shell_word(prompt, powershell)
    if powershell:
        return [
            f"$env:CODEX_HOME = {home_word}",
            f"Get-Content -Raw {prompt_word} | {command} -" if auto_start else command,
        ]
    command = f"CODEX_HOME={home_word} {command}"
    return [f"{command} - < {prompt_word}" if auto_start else command]


def opencode_launch(
    config: str,
    prompt: str,
    flags: list[str],
    auto_start: bool,
    powershell: bool = False,
) -> list[str]:
    """An ``opencode`` launch; auto-start runs ``opencode run`` on its prompt file.

    Passing a single-line instruction pointing at ``prompt`` (with
    ``external_directory`` allowed in the rendered config) avoids the Windows
    npm ``.cmd`` shim mangling multi-line prompts in ``argv``.
    """
    words = ["opencode", "run", "--auto", *flags] if auto_start else ["opencode", "--auto", *flags]
    if auto_start:
        words.append(auto_start_instruction(prompt, powershell))
    command = " ".join(words)
    config_word = shell_word(config, powershell)
    if powershell:
        return [f"$env:OPENCODE_CONFIG = {config_word}", command]
    return [f"OPENCODE_CONFIG={config_word} {command}"]


def antigravity_launch(
    home: str,
    prompt: str,
    prompt_dir: str,
    flags: list[str],
    auto_start: bool,
    powershell: bool = False,
) -> list[str]:
    """An ``agy`` launch isolated to ``home`` via ``HOME`` and ``USERPROFILE``.

    ``agy`` loads ``~/.gemini/config/mcp_config.json`` via Go's
    ``os.UserHomeDir()``, which reads ``HOME`` on POSIX and ``USERPROFILE`` on
    Windows (including when ``agy.exe`` is launched from Git Bash). Before
    overriding ``HOME``/``USERPROFILE``, ``GIT_CONFIG_GLOBAL`` (and on POSIX the
    XDG base directories; on Windows ``gh`` and ``uv`` use ``%APPDATA%`` and
    ``%LOCALAPPDATA%`` unless XDG is set) is pinned to the operator's real home
    so ``git``, ``gh``, and ``uv`` keep their config and caches without linking
    ``.ssh`` or ``.config`` into the run directory.
    """
    words = [
        "agy",
        *flags,
        "--dangerously-skip-permissions",
        "--add-dir",
        shell_word(prompt_dir, powershell),
    ]
    if auto_start:
        words += ["-p", auto_start_instruction(prompt, powershell)]
    command = " ".join(words)
    home_word = shell_word(home, powershell)
    if powershell:
        return [
            "$oldHome = $env:HOME; $oldProfile = $env:USERPROFILE; "
            "$oldGitConfig = $env:GIT_CONFIG_GLOBAL",
            "try { "
            "if (-not $env:GIT_CONFIG_GLOBAL) { $env:GIT_CONFIG_GLOBAL = "
            'if ($oldHome) { "$oldHome\\.gitconfig" } '
            'else { "$env:USERPROFILE\\.gitconfig" } }; '
            f"$env:HOME = {home_word}; $env:USERPROFILE = {home_word}; {command} "
            "} finally { "
            "$env:HOME = $oldHome; $env:USERPROFILE = $oldProfile; "
            "$env:GIT_CONFIG_GLOBAL = $oldGitConfig }",
        ]
    git_config = 'GIT_CONFIG_GLOBAL="${GIT_CONFIG_GLOBAL:-$HOME/.gitconfig}"'
    if os.name == "nt" or PureWindowsPath(home).drive:
        env_prefix = f"{git_config} HOME={home_word} USERPROFILE={home_word}"
    else:
        env_prefix = (
            f"{git_config} "
            'XDG_CONFIG_HOME="${XDG_CONFIG_HOME:-$HOME/.config}" '
            'XDG_CACHE_HOME="${XDG_CACHE_HOME:-$HOME/.cache}" '
            'XDG_DATA_HOME="${XDG_DATA_HOME:-$HOME/.local/share}" '
            f"HOME={home_word} USERPROFILE={home_word}"
        )
    return [f"{env_prefix} {command}"]


def harness_launch(
    harness: str,
    config: str,
    git_dir: str | None,
    prompt: str,
    prompt_dir: str,
    flags: list[str],
    auto_start: bool,
    powershell: bool = False,
) -> list[str]:
    """Dispatch to the harness's launch-line builder."""
    if harness == "codex":
        return codex_launch(config, git_dir, prompt, flags, auto_start, powershell)
    if harness == "opencode":
        return opencode_launch(config, prompt, flags, auto_start, powershell)
    if harness == "antigravity":
        return antigravity_launch(config, prompt, prompt_dir, flags, auto_start, powershell)
    return claude_launch(config, prompt, prompt_dir, flags, auto_start, powershell)


def launch_lines(
    harness: str,
    workdir: Path,
    config: Path,
    prompt: Path,
    git_dir: Path | None,
    model: str = "",
    effort: str = "",
    auto_start: bool = True,
    tmp_dir: Path | None = None,
) -> list[str]:
    """One local agent's start lines for this platform; paths are shell-quoted.

    ``config`` is the MCP config for Claude Code, ``OPENCODE_CONFIG`` for
    OpenCode, ``CODEX_HOME`` for Codex, and the isolated home for AntiGravity.
    """
    powershell = os.name == "nt"
    flags = model_flags(harness, model, effort, powershell, auto_start)
    lines = harness_launch(
        harness,
        str(config),
        None if git_dir is None else str(git_dir),
        str(prompt),
        str(prompt.parent),
        flags,
        auto_start,
        powershell,
    )
    temp_lines = []
    if tmp_dir is not None:
        if powershell:
            temp_lines = [
                f"$env:TEMP = {shell_word(str(tmp_dir), powershell)}",
                f"$env:TMP = {shell_word(str(tmp_dir), powershell)}",
            ]
        else:
            temp_lines = [f"export TMPDIR={shell_word(str(tmp_dir), powershell)}"]
    return [f"cd {shell_word(str(workdir), powershell)}", *temp_lines, *lines]


def start_script(lines: list[str], powershell: bool = False) -> str:
    """A start script running ``lines``, stopping if its ``cd`` fails."""
    if powershell:
        header = ["# Generated by scripts/prepare-run.py", '$ErrorActionPreference = "Stop"']
    else:
        header = ["#!/usr/bin/env bash", "# Generated by scripts/prepare-run.py", "set -e"]
    return "\n".join([*header, *lines]) + "\n"


def write_script(path: Path, text: str) -> None:
    path.write_text(text, encoding="utf-8", newline="\n")
    path.chmod(0o700)


def remote_note(name: str) -> str:
    return f"# {name} runs on the worker host: render and launch it there (below)"


def codex_home_name(name: str) -> str:
    return "codex" if name == "charlie" else f"{name}-codex"


def agy_home_name(name: str) -> str:
    return f"{name}-agy"


def launch_config_path(harness: str, configs: PurePath, name: str) -> PurePath:
    """The config or isolated-home path passed to ``launch_lines`` for ``name``."""
    if harness == "codex":
        return configs / codex_home_name(name)
    if harness == "opencode":
        return configs / f"{name}.opencode.json"
    if harness == "antigravity":
        return configs / agy_home_name(name)
    return configs / f"{name}.mcp.json"


def git_bash_path(path: PurePath) -> str:
    """Git Bash spelling of a Windows drive path (``C:/x`` -> ``/c/x``)."""
    drive = path.drive
    if isinstance(path, PureWindowsPath) and len(drive) == 2 and drive[1] == ":":
        return "/" + drive[0].lower() + path.as_posix()[2:]
    return path.as_posix()


def worker_launch(
    name: str,
    harness: str,
    run_dir: PurePath,
    workspace: PurePath,
    model: str = "",
    effort: str = "",
    auto_start: bool = True,
    tmp_dir: PurePath | None = None,
) -> list[str]:
    """A remote worker's launch lines, spelled for its host.

    On a Windows host the lines are for Git Bash: the shell's own ``cd`` and
    ``<`` take Git Bash spellings, while arguments and variables handed to
    native programs (claude, codex, opencode, agy) keep forward-slash Windows paths (#75).
    """
    if tmp_dir is None:
        tmp_dir = run_dir / "tmp" / name
    prompt = run_dir / f"{name}.prompt.md"
    flags = model_flags(harness, model, effort, auto_start=auto_start)
    lines = harness_launch(
        harness,
        launch_config_path(harness, run_dir / "configs", name).as_posix(),
        (workspace / ".git").as_posix(),
        git_bash_path(prompt) if harness == "codex" else prompt.as_posix(),
        run_dir.as_posix(),
        flags,
        auto_start,
    )
    is_windows = bool(run_dir.drive) or os.name == "nt"
    if is_windows:
        temp_lines = [
            f"export TMPDIR={shell_word(git_bash_path(tmp_dir))}",
            f"export TEMP={shell_word(tmp_dir.as_posix())}",
            f"export TMP={shell_word(tmp_dir.as_posix())}",
        ]
    else:
        temp_lines = [f"export TMPDIR={shell_word(tmp_dir.as_posix())}"]
    return [f"cd {shell_word(git_bash_path(workspace))}", *temp_lines, *lines]


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


def write_worker_config(
    name: str,
    harness: str,
    env: dict[str, str],
    model: str,
    root: str,
    configs: Path,
    *,
    secret: bool = False,
) -> Path:
    """Write ``name``'s worker MCP config under ``configs`` and return its path."""
    if harness == "codex":
        home = codex_home(configs, codex_home_name(name))
        worker_args = [
            "run", "--locked", "--project", root, "robomate", "mcp", "--role", "worker"
        ]
        text = render_codex_config(env, worker_args)
        target = home / "config.toml"
        if secret:
            write_secret(target, lambda path: write_private_text(path, text))
        else:
            write_private_text(target, text)
        return target
    if harness == "opencode":
        mcp = render_opencode_config(env, root, model=model)
        target = configs / f"{name}.opencode.json"
    elif harness == "antigravity":
        home = agy_home(configs, agy_home_name(name))
        mcp = render_antigravity_mcp(env, root)
        target = agy_mcp_path(home)
    else:
        mcp = render_claude_mcp(env, root)
        target = configs / f"{name}.mcp.json"
    if secret:
        write_secret(target, lambda path: save(path, mcp))
    else:
        save(target, mcp)
    return target


def render_worker_bundle(
    name: str,
    harness: str,
    version: str,
    provider: str,
    model: str,
    capabilities: str,
    token_file: str,
    hub_url: str,
    root: PurePath,
    run_dir: PurePath,
    workspace: PurePath,
    out_dir: Path,
    effort: str = "",
    auto_start: bool = True,
) -> dict[str, Any]:
    """Render one worker's config, prompt and start script for the host that runs it.

    ``root``, ``run_dir`` and ``workspace`` are that host's paths, written with
    forward slashes; ``out_dir`` is where this process writes ``run_dir``'s
    files. They are the same directory under ``--worker-only``. The start
    script is for bash, which on Windows means Git Bash.
    """
    configs = out_dir / "configs"
    configs.mkdir(parents=True, mode=0o700, exist_ok=True)
    tmp_parent = out_dir / "tmp"
    tmp_parent.mkdir(parents=True, mode=0o700, exist_ok=True)
    if os.name != "nt":
        tmp_parent.chmod(0o700)
    agent_tmp = tmp_parent / name
    agent_tmp.mkdir(parents=True, mode=0o700, exist_ok=True)
    if os.name != "nt":
        agent_tmp.chmod(0o700)
    env = worker_env(
        name,
        harness,
        version,
        provider,
        model,
        capabilities,
        workspace.as_posix(),
        token_file,
        (run_dir / f"{name}-telemetry.jsonl").as_posix(),
        hub_url,
    )
    written = write_worker_config(
        name, harness, env, model, root.as_posix(), configs, secret=True
    )
    config = run_dir / written.relative_to(out_dir)
    (out_dir / f"{name}.prompt.md").write_text(
        render_worker_prompt(name) + prompt_sections(name, harness), encoding="utf-8"
    )
    launch = worker_launch(name, harness, run_dir, workspace, model, effort, auto_start)
    write_script(out_dir / f"start-{name}.sh", start_script(launch))
    return {
        "config": config.as_posix(),
        "prompt": (run_dir / f"{name}.prompt.md").as_posix(),
        "launch": launch,
        "script": (run_dir / f"start-{name}.sh").as_posix(),
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
    effort: str = "",
    auto_start: bool = True,
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
        f"--{name}-harness {harness}",
    ]
    if model:
        args.append(f"--{name}-model '{model}'")
    if effort:
        args.append(f"--{name}-effort '{effort}'")
    if not auto_start:
        args.append("--no-auto-start")
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
    hub_url: str | None = None,
    remote_worker: str | None = None,
    hub_port: int | None = None,
    alice_harness: str = DEFAULT_ALICE_HARNESS,
    alice_model: str = "",
    alice_effort: str = "",
    bob_effort: str = "",
    charlie_effort: str = "",
    auto_start: bool = True,
    roadmap: str | None = None,
    hub_repo: Path | None = None,
) -> dict[str, Any]:
    if work_file is not None and issue is not None:
        raise ValueError(
            "--work-file and --issue are mutually exclusive; name the issue inside "
            "the statement of work, or pass --issue alone"
        )
    if roadmap is not None and not roadmap.strip():
        raise ValueError("--roadmap must not be empty")
    statement = read_work_file(work_file.resolve()) if work_file is not None else None
    work_text = statement["text"] if statement is not None else None
    if not run_dir.is_absolute() or run_dir != run_dir.resolve():
        raise ValueError("RUN_DIR must be absolute and canonical")
    if run_dir == ROOT or ROOT in run_dir.parents:
        raise ValueError("RUN_DIR must be outside the coordination checkout")
    if not repository:
        raise ValueError("--repository must not be empty")
    alice_harness = harness_name("--alice-harness", alice_harness)
    bob_harness = harness_name("--bob-harness", bob_harness)
    charlie_harness = harness_name("--charlie-harness", charlie_harness)
    for h, m, e in (
        (alice_harness, alice_model, alice_effort),
        (bob_harness, bob_model, bob_effort),
        (charlie_harness, charlie_model, charlie_effort),
    ):
        model_flags(h, m, e, auto_start=auto_start)
    if merge_method not in ("squash", "merge", "rebase"):
        raise ValueError("--merge-method must be one of squash, merge, rebase")
    if allow_no_ci not in ("auto", "true", "false"):
        raise ValueError("--allow-no-ci must be one of auto, true, false")
    if remote_worker is not None and remote_worker not in WORKERS:
        raise ValueError(f"--remote-worker must be one of {list(WORKERS)}")
    if hub_repo is None:
        raise ValueError("--hub-repo is required; start robomate up in that repository first")
    if hub_host != DEFAULT_HUB_HOST:
        raise ValueError("--hub-host belongs to robomate up --bind; start the hub with "
                         "--bind and --public-url before preparing the run")
    if not hub_repo.is_absolute() or hub_repo != hub_repo.resolve():
        raise ValueError("--hub-repo must be absolute and canonical")
    hub_info = read_hub_json(hub_repo)
    if hub_info is None or not hub_info.get("pid"):
        raise ValueError(f"no running robomate hub in {hub_repo}; run robomate up first")
    live_url = str(hub_info["url"])
    live_port = int(hub_info["port"])
    if not process_alive(int(hub_info["pid"])) or not hub_healthy(
        live_url, str(hub_info["hub_id"])
    ):
        raise ValueError(f"hub in {hub_repo} is not responding; run robomate up first")
    if hub_url is not None and hub_url.rstrip("/") != live_url:
        raise ValueError("--hub-url differs from the running hub; configure robomate up")
    if public_url is not None and public_url.rstrip("/") != live_url:
        raise ValueError("--public-url differs from the running hub; configure robomate up")
    if hub_port is not None and hub_port != live_port:
        raise ValueError("--hub-port differs from the running hub; configure robomate up")
    network = {"hub_port": live_port, "hub_url": live_url, "public_url": live_url}
    if remote_worker is not None and is_loopback(url_host(live_url, "--hub-repo")):
        raise ValueError("a remote worker cannot dial a loopback hub; restart robomate up "
                         "with --bind and --public-url")
    networked = remote_worker is not None or network != {
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
        "bob": resolve_provider(bob_harness, bob_model, bob_provider),
        "charlie": resolve_provider(charlie_harness, charlie_model, charlie_provider),
    }

    bob_path = bob_dir or (run_dir / "bob")
    charlie_path = charlie_dir or (run_dir / "charlie")
    for path, label in ((bob_path, "--bob-dir"), ((charlie_path), "--charlie-dir")):
        if not path.is_absolute() or path != path.resolve():
            raise ValueError(f"{label} must be absolute and canonical")
    if bob_path == charlie_path:
        raise ValueError("--bob-dir and --charlie-dir must differ")
    resolved_state = hub_repo / ".robomate"
    if state_dir is not None and state_dir != resolved_state:
        raise ValueError("--state-dir is set by --hub-repo and cannot differ")
    slug = parse_github_slug(repository)
    goal = render_goal(slug, repository, issue, work_text, roadmap)
    check_hub_workflow(hub_repo, run_dir, goal)

    harnesses = {"bob": bob_harness, "charlie": charlie_harness}
    # Harness versions come from the CLIs themselves, never placeholders; a
    # remote worker's version is probed on its own host by --worker-only.
    versions, parsed_versions = probe_versions({harnesses[name] for name in local})

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

    policy = {
        "max_review_rounds": 3,
        "merge_method": merge_method,
        "allow_no_ci": policy_allow_no_ci,
        "role_policy": {
            "reviewer_harness_differs": bob_harness != charlie_harness,
            # Baseline from prompts/alice.md; Alice still observes any real
            # provider difference at pairing time via check-in profiles.
            "reviewer_provider_differs": False,
            "implementer_capabilities": bob_capabilities.split(",")
            if bob_capabilities
            else [],
            "reviewer_capabilities": charlie_capabilities.split(",")
            if charlie_capabilities
            else [],
        },
        "pairing_wait_s": 120,
        "max_wall_minutes": 180,
        "max_task_lease_min": 120,
    }
    check_hub_workflow(hub_repo, run_dir, goal, policy)

    run_dir.mkdir(parents=True, mode=0o700, exist_ok=True)
    manifest_path = run_dir / "run.json"
    check_manifest(manifest_path, run_dir, repository)

    # Bootstrap before minting the token so a failed clone leaves no state behind.
    paths = {"bob": bob_path, "charlie": charlie_path}
    workspaces = {name: bootstrap_clone(name, paths[name], clone_from) for name in local}
    read_hub_token(resolved_state / "token")
    if len(local) == 2 and (
        workspaces["bob"]["workspace_id"] == workspaces["charlie"]["workspace_id"]
    ):
        raise ValueError("bob and charlie must have distinct workspace IDs")

    configs = run_dir / "configs"
    configs.mkdir(mode=0o700, exist_ok=True)

    tmp_parent = run_dir / "tmp"
    tmp_parent.mkdir(mode=0o700, exist_ok=True)
    if os.name != "nt":
        tmp_parent.chmod(0o700)
    for name in ("alice", *local):
        agent_tmp = tmp_parent / name
        agent_tmp.mkdir(mode=0o700, exist_ok=True)
        if os.name != "nt":
            agent_tmp.chmod(0o700)

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
            str(resolved_state / "token"),
            telemetry,
            network["hub_url"],
        )
        rendered_configs[name] = str(
            write_worker_config(name, harness, env, models[name], str(ROOT), configs)
        )

    hub_env = {"ROBOMATE_HUB_URL": live_url,
               "ROBOMATE_TOKEN_FILE": str(resolved_state / "token"), "PYTHONUTF8": "1"}
    hub_args = ["run", "--locked", "--project", str(ROOT),
                "robomate", "mcp", "--role", "orchestrator"]
    if alice_harness == "codex":
        alice_home = codex_home(configs, codex_home_name("alice"))
        (alice_home / "skills").mkdir(exist_ok=True)
        link_or_copy(
            ROOT / "skills/alice-orchestrator", alice_home / "skills" / "alice-orchestrator"
        )
        write_private_text(
            alice_home / "config.toml",
            codex_sandbox() + codex_mcp("uv", hub_args, hub_env, ALICE_TOOLS, 330),
        )
        alice_config = alice_home / "config.toml"
    elif alice_harness == "opencode":
        alice_config = configs / "alice.opencode.json"
        save(
            alice_config,
            render_opencode_config(hub_env, model=alice_model, role="orchestrator"),
        )
    elif alice_harness == "antigravity":
        alice_home = agy_home(configs, agy_home_name("alice"))
        skills_dir = alice_home / ".gemini" / "config" / "skills"
        skills_dir.mkdir(parents=True, exist_ok=True)
        link_or_copy(ROOT / "skills/alice-orchestrator", skills_dir / "alice-orchestrator")
        alice_config = agy_mcp_path(alice_home)
        save(
            alice_config,
            render_antigravity_mcp(hub_env, tools=ALICE_TOOLS, role="orchestrator"),
        )
    else:
        alice_config = configs / "alice.mcp.json"
        save(
            alice_config,
            {"mcpServers": {"robomate": {"command": "uv", "args": hub_args, "env": hub_env}}},
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
    if alice_harness in ("opencode", "antigravity"):
        (alice_runtime / ".agents" / "skills").mkdir(parents=True, exist_ok=True)
        link_or_copy(
            ROOT / "skills/alice-orchestrator",
            alice_runtime / ".agents" / "skills" / "alice-orchestrator",
        )

    for name in local:
        (run_dir / f"{name}.prompt.md").write_text(
            render_worker_prompt(name) + prompt_sections(name, harnesses[name]),
            encoding="utf-8",
        )

    (run_dir / "alice.prompt.md").write_text(
        render_alice_prompt(goal, account, policy) + prompt_sections("alice", alice_harness),
        encoding="utf-8",
    )
    # The goal text is what Alice initializes with; path and hash tie it to the file.
    work = {
        "goal": goal,
        "path": statement["path"] if statement is not None else None,
        "sha256": statement["sha256"] if statement is not None else None,
        "roadmap": resolve_roadmap(roadmap, slug, repository),
    }

    manifest = {
        "schema_version": 1,
        "repository": repository,
        "clone_repository": clone_from,
        "slug": slug,
        "run_dir": str(run_dir),
        "state_dir": str(resolved_state),
        "hub_repo": str(hub_repo),
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

    # One start script per local agent; a remote worker's is rendered on its host.
    launch = {
        "alice": (alice_harness, alice_model, alice_effort),
        "bob": (bob_harness, bob_model, bob_effort),
        "charlie": (charlie_harness, charlie_model, charlie_effort),
    }
    workdirs = {"alice": alice_runtime, **{name: Path(workspaces[name]["path"]) for name in local}}
    suffix = "ps1" if os.name == "nt" else "sh"
    scripts: dict[str, str] = {}
    for name in ("alice", *local):
        harness, model, effort = launch[name]
        config = Path(launch_config_path(harness, configs, name))
        lines = launch_lines(
            harness,
            workdirs[name],
            config,
            run_dir / f"{name}.prompt.md",
            None if name == "alice" else workdirs[name] / ".git",
            model,
            effort,
            auto_start,
            tmp_dir=run_dir / "tmp" / name,
        )
        script = run_dir / f"start-{name}.{suffix}"
        write_script(script, start_script(lines, os.name == "nt"))
        scripts[name] = str(script)
    manifest["launch"] = {
        "auto_start": auto_start,
        "agents": {
            name: {
                "harness": launch[name][0],
                "model": launch[name][1],
                "effort": launch[name][2],
                "script": scripts[name],
            }
            for name in scripts
        },
    }
    save(manifest_path, manifest)

    codex_auth: dict[str, str] = {}
    agy_auth: dict[str, str] = {}
    for name in ("alice", *local):
        if launch[name][0] == "codex":
            home_name = codex_home_name(name)
            codex_auth[name] = f"{home_name}: {codex_login_status(configs / home_name)}"
        elif launch[name][0] == "antigravity":
            home_name = agy_home_name(name)
            agy_auth[name] = f"{home_name}: {agy_login_status(configs / home_name)}"
    if not codex_auth:
        codex_auth = {"codex": "no codex worker in this topology"}

    checks: dict[str, Any] = {
        "gh_auth": "ok" if not skip_github_checks else "skipped",
        "versions": versions,
        "merge": merge_note,
        "ci": ci_note,
        "codex_auth": codex_auth,
    }
    if agy_auth:
        checks["agy_auth"] = agy_auth
    unknown_providers = {
        name: f"unknown: pass --{name}-model or --{name}-provider to record the model maker"
        for name in local
        if providers[name] == "unknown"
    }
    if unknown_providers:
        checks["provider"] = unknown_providers

    report: dict[str, Any] = {
        "run_dir": str(run_dir),
        "repository": repository,
        "slug": slug,
        "issue": issue,
        "workspaces": {name: workspaces[name]["path"] for name in local},
        "configs": {"alice": str(alice_config), **rendered_configs},
        "prompts": {
            name: str(run_dir / f"{name}.prompt.md") for name in ("alice", *local)
        },
        "start_scripts": scripts,
        "checks": checks,
    }
    if networked:
        report["network"] = manifest["network"]
    print(json.dumps(report, indent=2, sort_keys=True))
    print("\nStart scripts (run each in its own terminal, alice first):")
    for script in scripts.values():
        print(script)
    if remote_worker is not None:
        print(remote_note(remote_worker))
    if not auto_start:
        print(
            "Auto-start is off: once each agent is up, tell it to read and follow its "
            "prompt file (see prompts above)."
        )
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
                launch[remote_worker][2],
                auto_start,
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
    effort: str = "",
    auto_start: bool = True,
) -> dict[str, Any]:
    """Render one remote worker on the host that runs it (``--worker-only``).

    Reads the hub's token file in place, bootstraps the clone with this host's
    git, and renders the config, prompt and start script with this host's paths.
    """
    if name not in WORKERS:
        raise ValueError(f"--worker-only must be one of {list(WORKERS)}")
    harness = harness_name(f"--{name}-harness", harness)
    model_flags(harness, model, effort, auto_start=auto_start)
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
    read_hub_token(token_file)
    versions, parsed_versions = probe_versions({harness})

    slug = parse_github_slug(repository)
    clone_from = clone_source(repository, slug)
    run_dir.mkdir(parents=True, mode=0o700, exist_ok=True)
    manifest_path = run_dir / "run.json"
    check_manifest(manifest_path, run_dir, repository)
    identity = bootstrap_clone(name, worker_path, clone_from)
    resolved_provider = resolve_provider(harness, model, provider)
    # Keep the drive-letter case bootstrap printed; read_identity compares the string.
    bundle = render_worker_bundle(
        name,
        harness,
        parsed_versions[harness],
        resolved_provider,
        model,
        capabilities,
        str(token_file),
        dial,
        ROOT,
        run_dir,
        Path(identity["path"]),
        run_dir,
        effort,
        auto_start,
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
        "launch": {
            "auto_start": auto_start,
            "agents": {
                name: {
                    "harness": harness,
                    "model": model,
                    "effort": effort,
                    "script": bundle["script"],
                }
            },
        },
    }
    save(manifest_path, manifest)
    checks: dict[str, Any] = {"versions": versions}
    if harness == "codex":
        home_name = codex_home_name(name)
        checks["codex_auth"] = {
            name: f"{home_name}: {codex_login_status(run_dir / 'configs' / home_name)}"
        }
    elif harness == "antigravity":
        home_name = agy_home_name(name)
        checks["agy_auth"] = {
            name: f"{home_name}: {agy_login_status(run_dir / 'configs' / home_name)}"
        }
    if resolved_provider == "unknown":
        checks["provider"] = {
            name: f"unknown: pass --{name}-model or --{name}-provider to record the model maker"
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
                "start_script": bundle["script"],
                "checks": checks,
            },
            indent=2,
            sort_keys=True,
        )
    )
    print()
    for line in preflight_lines(dial, None):
        print(line)
    print(f"\nStart script for {name} (run it in bash or Git Bash once the preflight passes):")
    print(bundle["script"])
    if not auto_start:
        print(
            f"Auto-start is off: once {name} is up, tell it to read and follow "
            f"{bundle['prompt']}."
        )
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
    parser.add_argument(
        "--roadmap",
        default=None,
        metavar="ROADMAP",
        help="roadmap issue bob may update: bare N or #N of the target repository, "
        "or OWNER/REPO#N verbatim (default: no roadmap update required)",
    )
    for name, default in (("alice", "claude"), ("bob", "claude"), ("charlie", "codex")):
        # --bob / --charlie are the earlier spellings of the worker harness flags.
        aliases = [] if name == "alice" else [f"--{name}"]
        parser.add_argument(
            f"--{name}-harness",
            *aliases,
            dest=f"{name}_harness",
            metavar="HARNESS",
            default=None,
            help=f"claude, codex, opencode, or antigravity, case-insensitive (default: {default})",
        )
    for name in AGENTS:
        pins = "" if name == "alice" else f"; also pins {name}'s HUB_MODEL"
        parser.add_argument(
            f"--{name}-model",
            metavar="MODEL",
            default="",
            help=f"passed to the CLI's --model unchecked{pins}",
        )
    for name in AGENTS:
        parser.add_argument(
            f"--{name}-effort",
            metavar="EFFORT",
            default="",
            help="reasoning effort, passed to the CLI unchecked",
        )
    parser.add_argument(
        "--no-auto-start",
        dest="auto_start",
        action="store_false",
        help="start scripts open each agent without handing it its prompt file",
    )
    parser.add_argument("--bob-provider", default=None)
    parser.add_argument("--charlie-provider", default=None)
    parser.add_argument("--bob-capabilities", default="")
    parser.add_argument("--charlie-capabilities", default="")
    parser.add_argument("--bob-dir", type=Path, default=None)
    parser.add_argument("--charlie-dir", type=Path, default=None)
    parser.add_argument("--state-dir", type=Path, default=None)
    parser.add_argument("--hub-repo", type=Path, default=None,
                        help="absolute repository path with a running robomate up hub")
    parser.add_argument("--merge-method", default="squash")
    parser.add_argument("--allow-no-ci", default="auto")
    parser.add_argument("--skip-github-checks", action="store_true")
    parser.add_argument(
        "--hub-host",
        default=DEFAULT_HUB_HOST,
        help="legacy flag; set the bind address with robomate up --bind instead",
    )
    parser.add_argument(
        "--hub-url", default=None, help="optional check against the running hub URL"
    )
    parser.add_argument(
        "--hub-port",
        type=int,
        default=None,
        help="optional check against the running robomate up port",
    )
    parser.add_argument(
        "--public-url",
        default=None,
        help="optional check against the running hub URL",
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
    # None means the flag was not given; an empty value is still validated.
    harnesses = dict(DEFAULT_HARNESSES)
    for name in AGENTS:
        if (value := getattr(args, f"{name}_harness")) is not None:
            harnesses[name] = value
    if args.worker_only is not None:
        hub_only = {
            "--issue": args.issue is not None,
            "--work-file": args.work_file is not None,
            "--account": args.account is not None,
            "--roadmap": args.roadmap is not None,
            "--state-dir": args.state_dir is not None,
            "--hub-repo": args.hub_repo is not None,
            "--remote-worker": args.remote_worker is not None,
            "--hub-host": args.hub_host != DEFAULT_HUB_HOST,
            "--hub-port": args.hub_port is not None,
            "--public-url": args.public_url is not None,
            "--alice-harness": args.alice_harness is not None,
            "--alice-model": bool(args.alice_model),
            "--alice-effort": bool(args.alice_effort),
        }
        if given := [flag for flag, present in hub_only.items() if present]:
            parser.error(f"hub-host flags do not apply to --worker-only: {', '.join(given)}")
        if args.token_file is None:
            parser.error("--worker-only needs --token-file, the hub's token file")
        if args.hub_url is None:
            parser.error("--worker-only needs --hub-url, the address this host dials")
        name = args.worker_only
        try:
            prepare_worker(
                name,
                args.repository,
                args.run_dir,
                args.hub_url,
                args.token_file,
                harnesses[name],
                getattr(args, f"{name}_model"),
                getattr(args, f"{name}_provider"),
                getattr(args, f"{name}_capabilities"),
                getattr(args, f"{name}_dir"),
                getattr(args, f"{name}_effort"),
                args.auto_start,
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
            harnesses["bob"],
            harnesses["charlie"],
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
            alice_harness=harnesses["alice"],
            alice_model=args.alice_model,
            alice_effort=args.alice_effort,
            bob_effort=args.bob_effort,
            charlie_effort=args.charlie_effort,
            auto_start=args.auto_start,
            roadmap=args.roadmap,
            hub_repo=args.hub_repo,
        )
    except ValueError as exc:
        sys.exit(f"prepare-run: error: {exc}")


if __name__ == "__main__":
    main()
