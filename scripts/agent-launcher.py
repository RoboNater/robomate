#!/usr/bin/env python3
"""Start, resume or ask a joined agent, without generated configs (M2 Step 10, #247).

The launcher is optional. You can also start each harness in its own worktree
and send its join line (spec §2, §9.4).
Run it from the hub's checkout (or name the hub with ``--hub``)::

    uv run --project <robomate checkout> python scripts/agent-launcher.py start bob \\
        --role implementer --harness claude-code [--model M] [--effort E] [--interactive]
    uv run --project <robomate checkout> python scripts/agent-launcher.py resume bob
    uv run --project <robomate checkout> python scripts/agent-launcher.py stop bob
    uv run --project <robomate checkout> python scripts/agent-launcher.py ask bob
    uv run --project <robomate checkout> python scripts/agent-launcher.py doctor \\
        [--hub <hub>] [--run <run_id>] [--harness <harness>]

For ``doctor``, ``--hub``, ``--run`` and ``--harness`` are optional: from inside
``<state>/runs/<id>/`` both the hub and run come from that path (conflicting
explicit flags are refused); otherwise ``--run`` defaults to the hub's only run
or its only unfinished run; ``--harness``, ``--model``, ``--effort`` and
``--clean-login`` take explicit flags, then the run's saved Denise settings
(``runs/<id>/denise/settings.json``), or refuse naming ``--harness``.

``start`` finds the agent's worktree with ``robomate workspace <agent>``, starts
the harness there with a first prompt naming the join, next step, and turn rule,
passes the harness's sandbox grants as flags, and resumes
the same conversation with a fixed continuation and #158 backoff until the
workflow is done or the worker is released, warning and then stopping after
too many consecutive long failing runs (#169). Nothing is rendered: the harness
finds the hub through its static ``robomate mcp`` entry, pinned to this hub's
``hub_id`` through ``ROBOMATE_HUB_ID`` in the child environment (#511), so the
bridge never attaches by guessing from the worktree's binding. Each launch,
exit, wait and stop is a JSON line, with the hub's
``run_id``, in ``sessions/<agent>.jsonl`` in the hub's state directory: the
operator's data, which agents never read. A remote host passes ``--hub-url``,
``--token-file``, ``--sessions-dir`` and the ``--workdir`` that ``robomate
workspace <agent> --clone`` printed there.

``resume`` takes the harness, role and worktree from that log, applies the
guards of ``agent-recovery`` (predecessor stopped and detached, same hub,
workflow taken from the hub with #223's evidence when the log predates it),
and resumes the saved conversation with a fixed continuation whose join
instruction matches that conversation: no recorded join sends the plain join
line, while a recorded join sends a bounded ``join(..., takeover=true)`` for
its own seat only (#512). ``stop`` is local process control: it stops an
attached launcher with SIGTERM and waits for its ``stop`` row, or else stops
the recorded harness of an interrupted launcher and writes its ``exit`` row,
leaving hub, workflow and agent state untouched (#534). When that launch's own
bridge telemetry records a successful join, it writes the launch's ``joined``
row before the ``exit`` row, so a following resume keeps the takeover
continuation (#571). ``ask`` opens a saved
conversation interactively with its hub tools disabled; it needs no hub.
``--interactive`` (#222) starts or resumes without automatic resume and still
records the session. ``doctor`` is Denise's optional entry, separate from all
of this: it reads the selected run locally, works with the hub stopped, and
never joins.

Supervision is per workflow (#465, M3 Step 6): the launcher pins the workflow
it first sees (never an earlier done one), follows it through terminal state
and release, then stops with a message naming a fresh ``start`` for the next
run. The hub's ``on_done: standby`` and a queued run keep agent rows; they do
not extend supervision, and a standby conversation that is still live when the
next run activates is stopped before its fresh launcher starts. A ``resume``
continues only the same saved workflow
and refuses a different hub or workflow, and a fresh ``start`` begins a new
conversation, so no earlier run's conversation, task or kickoff is adopted.
M3 keeps this a script: there is no ``robomate supervisor`` subcommand and no
persistent standby supervision across workflows. The per-harness interactive
start/resume, exact-ID capture and hub-free ask contract is ``ADAPTERS`` in
this script, rendered for the operator in
``docs/development/agent-recovery.md#adapter-matrix``. Use ``robomate status``
for the operator's "not listening" hint and its ``Continue.`` or
exact-session-resume remedy.

Every start and resume gives the harness, and so its ``robomate mcp`` bridge,
the agent's declared identity (#402): ``HUB_HARNESS``, ``HUB_HARNESS_VERSION``
(``--harness-version``, the CLI's own ``--version``), ``HUB_PROVIDER`` (from the
model, as ``run_common.resolve_provider``), ``HUB_MODEL`` and ``HUB_EFFORT``.
Codex forwards no variable to an MCP server unless told, so a Codex launch also
names them in ``-c mcp_servers.robomate.env.*``. Every start and resume also
pins ``ROBOMATE_HUB_ID`` (and, on a remote host, ``ROBOMATE_HUB_URL`` and the
``ROBOMATE_TOKEN_FILE`` path) the same way for Codex; no token value goes on
argv. ``--uv-cache-dir`` gives the
agent an isolated ``UV_CACHE_DIR`` (#285), created owner-only and granted to the
sandbox, and ``--pytest-workers`` its ``PYTEST_XDIST_AUTO_NUM_WORKERS`` (0 clears
an inherited one). The launch rows record them, so ``resume`` applies them again.
``start-run.py`` (#268) starts every agent of a scripted run this way.

Exit codes: 0 done or released; 1 resumes spent;
2 usage; 3 hub unreadable; 4 nothing safe to resume (and ``stop``: a recorded
process it will not guess about, or a stop it cannot confirm); 5 paused; 130
interrupted. An interactive or ask session returns the harness's own exit code.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import urllib.error
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

SCRIPTS = Path(__file__).resolve().parent
ROOT = SCRIPTS.parent
sys.path.insert(0, str(SCRIPTS))
import agent_launcher as common  # noqa: E402
import denise  # noqa: E402
from agent_hub_common.discovery import HUB_ID_ENV, validate_agent_name  # noqa: E402
from agent_interactive import split_arguments  # noqa: E402
from agent_recovery import (  # noqa: E402
    agent_lock,
    launcher_attached,
    lock_holder_pid,
    prepare,
    process_alive,
    process_name,
)
from agent_sessions import conversation as saved_conversation  # noqa: E402
from agent_sessions import exact_id  # noqa: E402
from agent_worker import WorkerLauncher, WorkerReader  # noqa: E402
from mcp_preflight import check as check_mcp_entry  # noqa: E402
from run_common import agy_mcp_path, bridge_identity_env, resolve_provider  # noqa: E402

ROLES = ("orchestrator", "implementer", "reviewer", "worker")
LAUNCH_EVENTS = ("start", "resume", "manual_resume")
PROGRAMS: dict[str, list[str]] = {
    "claude-code": ["claude"],
    "codex": ["codex"],
    "opencode": ["opencode"],
    "antigravity": ["agy"],
    "devin": ["devin"],
}
#: The run's permission flags (#335).
RUN_PERMISSIONS = {
    "claude-code": ["--permission-mode", "auto"],
    "codex": ["--approve-for-me"],
    "opencode": ["--auto"],
    "antigravity": ["--dangerously-skip-permissions"],
    "devin": ["--permission-mode", "dangerous", "--respect-workspace-trust=false"],
}
#: ``--safe-mode``: prompting defaults instead of the run's permissions.
SAFE_PERMISSIONS = {"codex": ["-a", "on-request"]}
#: Harnesses whose hub tools a flag or variable can switch off for ``ask``.
ASK_HARNESSES = ("claude-code", "codex", "opencode")
#: The joined launcher's per-harness adapter contract (#465, M3 Step 6).
#:
#: This table is the checked statement of what each adapter supports; the
#: tests compare it with the real code paths, and
#: docs/development/agent-recovery.md#adapter-matrix renders it for the
#: operator. A harness absent from ``PROMPTED_HARNESSES`` prints the join line
#: for the operator instead of passing it as the first prompt, and a missing
#: automatic capture always has the operator-supplied-ID remedy.
ADAPTERS: dict[str, dict[str, str]] = {
    "claude-code": {
        "interactive": "start and resume carry the join or continuation prompt",
        "capture": "the launcher chooses the exact UUID for a new conversation",
        "ask": "supported: --strict-mcp-config with no MCP config",
    },
    "codex": {
        "interactive": "start and resume carry the join or continuation prompt",
        "capture": "exact ID from this launch's new rollout under CODEX_HOME/sessions, "
        "else the operator-supplied ID",
        "ask": "supported: -c mcp_servers.robomate.enabled=false",
    },
    "opencode": {
        "interactive": "start and resume carry the join or continuation prompt",
        "capture": "no automatic capture; the operator supplies the exact ID "
        "(`opencode session list`)",
        "ask": "supported: the robomate MCP server is disabled in OPENCODE_CONFIG_CONTENT",
    },
    "antigravity": {
        "interactive": "start prints the join line for the operator; resume opens the exact "
        "conversation",
        "capture": "no automatic capture; the operator supplies the exact ID from the "
        "harness's session list",
        "ask": "refused: no tested per-launch switch disables the effective robomate MCP entry",
    },
    "devin": {
        "interactive": "start prints the join line for the operator; resume opens the exact "
        "conversation",
        "capture": "interactive has no automatic capture (operator ID); a headless launch "
        "reads the ATIF export",
        "ask": "refused: no tested per-launch switch disables the effective robomate MCP entry",
    },
}
#: Harnesses that accept the join or continuation as the first interactive prompt.
PROMPTED_HARNESSES = ("claude-code", "codex", "opencode")
_RUN_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}")
#: The xdist budget a scripted run gives each agent (``pytest_workers``).
PYTEST_WORKERS = "PYTEST_XDIST_AUTO_NUM_WORKERS"
#: How long ``stop`` waits for a signalled launcher's stop row (#534).
STOP_WAIT_S = 30.0
STOP_POLL_S = 0.1
#: How long a terminated launcher's Windows byte-range lock may take to clear.
LOCK_SETTLE_S = 1.0

Programs = Mapping[str, list[str]]


class Usage(Exception):
    """A refusal before anything was launched; exit 2."""


def say(message: str) -> None:
    print(f"agent-launcher: {message}", file=sys.stderr, flush=True)


def command_line(words: Sequence[str]) -> str:
    """A command the operator can paste: PowerShell on Windows, POSIX shells elsewhere."""

    if os.name == "nt":
        return "& " + " ".join("'" + word.replace("'", "''") + "'" for word in words)
    return shlex.join(words)


def join_line(agent: str, role: str) -> str:
    line = f"Join robomate as {agent}, {role}."
    if role == "orchestrator":
        return (
            f"{line} Use the alice-orchestrator skill. {common.ORCHESTRATOR_KICKOFF} "
            f"{common.ORCHESTRATOR_TURN_RULE} {common.ORCHESTRATOR_HUB_BOUNDARY}"
        )
    return f"{line} Then await your assignment with await_assignment. {common.WORKER_TURN_RULE}"


def continuation(agent: str, role: str, *, joined: bool) -> str:
    """The fixed text every automatic or manual resume sends (#146, #512): no state, no decision.

    ``joined`` says whether this conversation has a recorded successful join:
    only then does the launcher ask for a takeover, and only its own seat.
    """

    fixed = (
        "Your previous harness process exited before your work was done, and the launcher "
        "has resumed this same conversation. This message is a fixed continuation: it "
        "carries no operator decision, approval, or state. Your hub tools run in a new "
        "bridge process: "
    )
    if joined:
        head = fixed + (
            f'first call join(name="{agent}", role="{role}", takeover=true), which replaces '
            "only your previous session and grants no workflow authority. The hub refuses "
            "that takeover if another session's agent holds the seat: never take over a seat "
            "you did not hold, and if join fails with a refused takeover or a no-hub error, "
            "report blocked and wait for the operator. "
        )
    else:
        head = (
            f"Join robomate as {agent}, {role}. "
            + fixed
            + "No join from this conversation is recorded, so call that plain join rather "
            "than a takeover; never take over a seat you did not hold. If join fails with a "
            "discovery or mismatch error, report blocked and wait for the operator. "
        )
    if role == "orchestrator":
        return head + (
            f"{common.ORCHESTRATOR_KICKOFF} Reconcile using the returned orchestrator guide's On "
            "resume section. Keep an escalated or paused workflow as it is: escalation ends "
            "only through the operator's user_answered event. "
            f"{common.ORCHESTRATOR_RESUME_RULE} "
            f"{common.ORCHESTRATOR_TURN_RULE} {common.ORCHESTRATOR_HUB_BOUNDARY}"
        )
    if joined:
        return head + (
            "If the join response has a reattached_task, finish it and submit its result; if "
            "it has none, or the task failed or was canceled, drop it and return to "
            f"await_assignment. {common.WORKER_TURN_RULE}"
        )
    return head + (f"Then await your assignment with await_assignment. {common.WORKER_TURN_RULE}")


# -- where the hub, the log and the worktree are ------------------------------------


@dataclass(frozen=True)
class Place:
    url: str
    token_file: Path | None
    sessions: Path
    """The directory holding ``<agent>.jsonl``."""
    selector: str | None
    explicit: bool = False
    """Whether a joining child's bridge needs the URL and token file to find the hub:
    a remote host, or a hub named by ``ROBOMATE_HUB_URL``. A local hub is found
    through the worktree's binding instead."""
    checkout: Path | None = None
    hub_id: str | None = None
    """The hub this launch is pinned to (#511). A local hub's comes from its
    ``hub.json``; a remote host's is read from ``hub.info`` before launch."""


def locate(args: argparse.Namespace) -> Place:
    """The hub's URL and token file and the sessions directory, read locally."""

    remote = [args.hub_url, args.token_file]
    if any(remote):
        if not all(remote) or not args.sessions_dir:
            raise Usage("--hub-url, --token-file and --sessions-dir go together (a remote host)")
        if args.hub:
            raise Usage("--hub selects a local hub; --hub-url names a remote one, not both")
        return Place(
            args.hub_url.rstrip("/"),
            Path(args.token_file).resolve(),
            Path(args.sessions_dir),
            None,
            explicit=True,
        )
    from agent_hub_common.discovery import DiscoveryError, LocalHub, find_hub

    try:
        hub = find_hub(Path.cwd(), selector=args.hub)
    except DiscoveryError as exc:
        raise Usage(str(exc)) from None
    if isinstance(hub, LocalHub):
        sessions = Path(args.sessions_dir) if args.sessions_dir else hub.state_dir / "sessions"
        return Place(
            hub.url.rstrip("/"),
            hub.token_file,
            sessions,
            args.hub,
            checkout=hub.checkout,
            hub_id=hub.hub_id,
        )
    if not args.sessions_dir:
        raise Usage("a hub named by ROBOMATE_HUB_URL has no local state: add --sessions-dir")
    # An explicit/remote hub has no local record to read its id from; resolve it
    # from hub.info before launch (resolve_hub_id), refusing an unreadable hub.
    return Place(hub.url, hub.token_file, Path(args.sessions_dir), None, explicit=True)


def is_selection(name: str) -> bool:
    """The variables by which a bridge finds a hub explicitly (spec §4)."""
    return name in ("ROBOMATE_HUB_URL", HUB_ID_ENV) or name.startswith("ROBOMATE_TOKEN")


def cleared_environment() -> dict[str, str]:
    """This process's environment without an inherited hub selection."""
    return {
        key: value
        for key, value in os.environ.items()
        if not is_selection(key)
        and not key.startswith(("ROBOMATE_PERMISSION_", "ROBOMATE_LAUNCH_"))
    }


def hub_selection_env(place: Place) -> dict[str, str]:
    """The selection that pins a joining bridge to this hub (#511).

    Every child gets ``ROBOMATE_HUB_ID``; a remote host's bridge also gets the
    URL and the token-file path (never the token value). The id is not secret
    and survives a hub restart on a different port.
    """
    if not place.hub_id:
        raise Usage("the hub's id is unknown: the launcher cannot pin the joining bridge")
    values = {HUB_ID_ENV: place.hub_id}
    if place.explicit:
        if place.token_file is None:
            raise Usage("the hub's token file is unknown: name it with --token-file")
        values["ROBOMATE_HUB_URL"] = place.url
        values["ROBOMATE_TOKEN_FILE"] = str(place.token_file)
    return values


def hub_flags(harness: str, place: Place) -> list[str]:
    """Codex forwards no env unless named: pin the selected hub on argv too (#511)."""

    if harness != "codex":
        return []
    words: list[str] = []
    for key, value in hub_selection_env(place).items():
        words += ["-c", f"mcp_servers.robomate.env.{key}={json.dumps(value)}"]
    return words


def resolve_hub_id(place: Place, tools: Tools, agent: str, role: str) -> Place:
    """Give a remote/explicit place its hub id from hub.info, or refuse (#511).

    A local place already carries its id from hub.json. An explicit one has no
    local record, so read hub.info before launch; an unreadable hub is refused.
    """
    if place.hub_id:
        return place
    reader = make_reader(tools, place, agent, role)
    try:
        info = reader.call("hub.info")
    except (OSError, ValueError, urllib.error.URLError) as exc:
        raise Usage(f"cannot read the remote hub's id from hub.info: {exc}") from None
    hub_id = info.get("hub_id") if isinstance(info, dict) else None
    if not isinstance(hub_id, str) or not hub_id:
        raise Usage("the remote hub's hub.info names no hub_id")
    return replace(place, hub_id=hub_id)


def child_environment(place: Place, extra: Mapping[str, str] | None = None) -> dict[str, str]:
    """A joining child's environment: the selected hub, never an inherited one (#247 r1-3).

    A local hub's worktree binding names it, so any ambient selection is
    cleared and the pinned id re-added. A remote host's clone has no binding:
    its bridge gets the URL and token file the operator gave, and no
    ``ROBOMATE_TOKEN``, which would otherwise take precedence over the file.
    """
    forwarded: dict[str, str] = {}
    if place.checkout is not None:
        forwarded["ROBOMATE_LAUNCH_HUB_CHECKOUT"] = str(place.checkout)
    forwarded |= hub_selection_env(place)
    return cleared_environment() | forwarded | dict(extra or {})


@dataclass(frozen=True)
class Identity:
    """What a joined agent's launch exports beside its hub selection (#402, #285, #268).

    The bridge reads the declared identity from its environment, as the
    configured MCP entry needs; the harness's shell commands
    inherit the isolated uv cache and the xdist budget. Launch rows record
    the parts a later ``resume`` cannot derive, so it exports them again.
    """

    harness: str
    model: str = ""
    effort: str = ""
    version: str = ""
    uv_cache: Path | None = None
    pytest_workers: int | None = None
    lane_tmp: Path | None = None
    telemetry_log: Path | None = None
    """The bridge's own JSONL (#512): the launcher reads its join rows, and the
    bridge writes no other run data there."""
    workspace_setup: str = common.WORKSPACE_SETUP_AUTO

    def environment(self) -> dict[str, str]:
        provider = resolve_provider(self.harness, self.model)
        values = bridge_identity_env(self.harness, self.version, provider, self.model, self.effort)
        if self.telemetry_log is not None:
            values["HUB_TELEMETRY_LOG"] = str(self.telemetry_log)
        if self.uv_cache is not None:
            values["UV_CACHE_DIR"] = str(self.uv_cache)
        if self.lane_tmp is not None:
            values.update({key: str(self.lane_tmp) for key in ("TMPDIR", "TEMP", "TMP")})
        if self.pytest_workers:
            values[PYTEST_WORKERS] = str(self.pytest_workers)
        return values

    def apply(self, environment: Mapping[str, str]) -> dict[str, str]:
        """``environment`` with this identity; ``pytest_workers`` 0 clears an inherited budget."""

        result = dict(environment) | self.environment()
        if self.pytest_workers == 0:
            result.pop(PYTEST_WORKERS, None)
        return result

    def bridge_flags(self) -> list[str]:
        """Codex starts MCP servers with a fixed environment allowlist: name the identity."""

        if self.harness != "codex":
            return []
        values = bridge_identity_env(
            self.harness,
            self.version,
            resolve_provider(self.harness, self.model),
            self.model,
            self.effort,
        )
        if self.telemetry_log is not None:
            values["HUB_TELEMETRY_LOG"] = str(self.telemetry_log)
        words: list[str] = []
        for key, value in values.items():
            # A TOML string, so a version like 1.0 is never read as a number.
            words += ["-c", f"mcp_servers.robomate.env.{key}={json.dumps(value)}"]
        return words

    def row(self) -> dict[str, object]:
        return {
            "harness_version": self.version or None,
            "uv_cache_dir": str(self.uv_cache) if self.uv_cache is not None else None,
            "pytest_workers": self.pytest_workers,
            "lane_tmp_dir": str(self.lane_tmp) if self.lane_tmp is not None else None,
            "workspace_setup": self.workspace_setup,
        }

    def prepare(self) -> None:
        """Create the isolated uv cache owner-only (#285)."""

        if self.uv_cache is None:
            return
        for directory in (self.uv_cache.parent, self.uv_cache):
            directory.mkdir(parents=True, mode=0o700, exist_ok=True)
        if os.name != "nt":
            self.uv_cache.chmod(0o700)


def saved_identity(saved: Mapping[str, Any], harness: str, model: str, effort: str) -> Identity:
    """The identity a launch row recorded, for ``resume`` (#268)."""

    cache = saved.get("uv_cache_dir")
    workers = saved.get("pytest_workers")
    version = saved.get("harness_version")
    lane_tmp = saved.get("lane_tmp_dir")
    setup = saved.get("workspace_setup")
    return Identity(
        harness,
        model,
        effort,
        version if isinstance(version, str) else "",
        Path(cache) if isinstance(cache, str) and cache else None,
        workers if type(workers) is int and workers >= 0 else None,
        Path(lane_tmp) if isinstance(lane_tmp, str) and lane_tmp else None,
        workspace_setup=setup if isinstance(setup, str) else common.WORKSPACE_SETUP_AUTO,
    )


def robomate_workspace(agent: str, selector: str | None) -> Path:
    """The agent's worktree, created ahead of join by the operator's ``robomate workspace``."""

    command = [sys.executable, "-m", "robomate", "workspace", agent]
    if selector:
        command += ["--hub", selector]
    done = subprocess.run(
        command, capture_output=True, text=True, encoding="utf-8", errors="replace", check=False
    )
    lines = done.stdout.strip().splitlines()
    if done.returncode != 0 or not lines:
        raise Usage(f"robomate workspace {agent} failed: {done.stderr.strip() or done.stdout}")
    return Path(lines[-1].strip())


def git_text(workdir: Path, *words: str) -> str | None:
    try:
        done = subprocess.run(
            ["git", "-C", str(workdir), *words],
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=False,
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    return done.stdout.strip() if done.returncode == 0 and done.stdout.strip() else None


def default_uv_cache() -> str | None:
    if os.environ.get("UV_CACHE_DIR"):
        return os.environ["UV_CACHE_DIR"]
    uv = shutil.which("uv")
    if uv is None:
        return None
    try:
        done = subprocess.run(
            [uv, "cache", "dir"], capture_output=True, text=True, check=False, timeout=10
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    return done.stdout.strip() if done.returncode == 0 and done.stdout.strip() else None


def forge_hosts(workdir: Path) -> list[str]:
    origin = git_text(workdir, "remote", "get-url", "origin") or ""
    host = urlsplit(origin).hostname if "://" in origin else None
    if host is None and "@" in origin and ":" in origin:
        host = origin.split("@", 1)[1].split(":", 1)[0]
    if not host:
        return []
    return ["github.com", "api.github.com"] if host == "github.com" else [host]


def grants(
    harness: str, workdir: Path, token_file: Path | None, uv_cache: Path | None = None
) -> list[str]:
    """The measured Linux sandbox grants (docs/user-guide.md), as flags; no files.

    Claude Code: its sandbox with the forge hosts and a read-deny on the hub's
    token directory. Codex: workspace-write with network and the linked
    worktree's own git roots plus the uv cache, never the whole common
    directory. Other harnesses' grants are measured in M3 (§9.2): none here.
    An agent's own isolated ``uv_cache`` (#285) replaces the shared one and is
    also writable inside Claude Code's sandbox.
    """

    if harness == "claude-code":
        sandbox: dict[str, Any] = {
            "enabled": True,
            "network": {"allowedDomains": forge_hosts(workdir)},
        }
        if sys.platform.startswith("linux"):
            sandbox["failIfUnavailable"] = True
        filesystem: dict[str, list[str]] = {}
        if token_file is not None:
            filesystem["denyRead"] = [str(token_file.parent)]
        if uv_cache is not None:
            filesystem["allowWrite"] = [str(uv_cache)]
        if filesystem:
            sandbox["filesystem"] = filesystem
        return ["--settings", json.dumps({"sandbox": sandbox}, separators=(",", ":"))]
    if harness != "codex":
        return []
    roots: list[str] = []
    git_dir = git_text(workdir, "rev-parse", "--absolute-git-dir")
    common_dir = git_text(workdir, "rev-parse", "--path-format=absolute", "--git-common-dir")
    if git_dir and common_dir and Path(git_dir) != Path(common_dir):
        common_path = Path(common_dir)
        roots += [git_dir, *(str(common_path / leaf) for leaf in ("objects", "refs", "logs"))]
    cache = str(uv_cache) if uv_cache is not None else default_uv_cache()
    if cache:
        roots.append(cache)
    return [
        "-c",
        "sandbox_mode=workspace-write",
        "-c",
        "sandbox_workspace_write.network_access=true",
        "-c",
        "sandbox_workspace_write.writable_roots=" + json.dumps(roots),
    ]


def is_opencode_v2(
    program: list[str],
    environment: Mapping[str, str],
    workdir: Path | None = None,
) -> bool:
    """Return whether the installed OpenCode binary is 2.x.

    Always probes the installed binary rather than trusting a saved
    ``harness_version`` (which belongs to the session's original host state and
    is stale after an in-place 1.x -> 2.x upgrade).
    """
    from opencode_headless import detect_major_version

    return detect_major_version(program, environment, workdir) >= 2


def model_flags(
    harness: str, model: str, effort: str, *, interactive: bool, opencode_v2: bool = False
) -> list[str]:
    if harness == "opencode" and opencode_v2:
        # Interactive OpenCode 2.x rejects --model and --variant; OPENCODE_CONFIG_CONTENT
        # carries model#variant. Headless `opencode run` uses --model provider/model#variant.
        if interactive:
            return []
        if not model:
            if effort:
                common.say(
                    f"OpenCode 2.x requires --model to attach #{effort}; omitting effort {effort!r}"
                )
            return []
        spec = f"{model}#{effort}" if effort and "#" not in model else model
        return ["--model", spec]
    flags = ["--model", model] if model else []
    if not effort:
        return flags
    if harness == "codex":
        # -c parses TOML and falls back to the raw string: no quotes to lose on Windows.
        return [*flags, "-c", f"model_reasoning_effort={effort}"]
    if harness == "opencode":
        # Interactive opencode rejects --variant; the config variable carries it there.
        return flags if interactive else [*flags, "--variant", effort]
    if harness == "devin":
        raise Usage("devin has no reasoning-effort flag; omit --effort for a Devin agent")
    return [*flags, "--effort", effort]


def opencode_environment(
    effort: str, *, safe: bool, no_hub: bool = False, model: str = "", v2: bool = False
) -> dict[str, str]:
    content: dict[str, Any] = {}
    if v2:
        if no_hub:
            # OpenCode 2.x drops a partial mcp.servers.<name> override lacking type+command.
            content["mcp"] = {
                "servers": {"robomate": {"type": "local", "command": ["false"], "disabled": True}}
            }
        if model:
            content["model"] = f"{model}#{effort}" if effort and "#" not in model else model
        if safe:
            content["agents"] = {
                "build": {"permissions": [{"action": "*", "resource": "*", "effect": "ask"}]}
            }
        return {"OPENCODE_CONFIG_CONTENT": json.dumps(content)} if content else {}
    if no_hub:
        content["mcp"] = {"robomate": {"enabled": False}}
    if effort:
        content["agent"] = {"build": {"variant": effort}}
    if safe:
        content["permission"] = {"*": "ask", "external_directory": "ask"}
    return {"OPENCODE_CONFIG_CONTENT": json.dumps(content)} if content else {}


def permissions(harness: str, safe: bool) -> list[str]:
    return SAFE_PERMISSIONS.get(harness, []) if safe else RUN_PERMISSIONS[harness]


def headless_base(
    harness: str,
    program: list[str],
    flags: list[str],
    granted: list[str],
    extra: list[str],
    *,
    title: str,
    export: Path,
    opencode_v2: bool = False,
) -> list[str]:
    """The harness command without prompt or conversation words; the launcher adds them."""

    if harness == "codex":
        return [*program, "exec", "-C", ".", *RUN_PERMISSIONS[harness], *granted, *flags, *extra]
    if harness == "opencode":
        standalone = ["--standalone"] if opencode_v2 else []
        return [*program, "run", *standalone, *flags, "--title", title, *extra]
    if harness == "antigravity":
        return [*program, *flags, *RUN_PERMISSIONS[harness], *extra]
    if harness == "devin":
        # The ATIF export is where Devin's session ID is read back (#224).
        return [*program, *flags, *RUN_PERMISSIONS[harness], "--export", str(export), *extra]
    return [*program, *flags, *RUN_PERMISSIONS[harness], *granted, *extra]


def interactive_start(
    harness: str,
    program: list[str],
    flags: list[str],
    granted: list[str],
    perms: list[str],
    extra: list[str],
    *,
    prompt: str,
    session_id: str,
    opencode_v2: bool = False,
) -> tuple[list[str], bool]:
    """An interactive first launch, and whether ``prompt`` is already in it.

    AntiGravity and Devin document no initial-prompt word for an interactive
    session, so the operator types the line the launcher prints.
    """

    if harness == "claude-code":
        argv = [*program, *flags, *perms, *granted, "--session-id", session_id, *extra, prompt]
        return argv, True
    if harness == "codex":
        return [*program, "-C", ".", *perms, *granted, *flags, *extra, prompt], True
    if harness == "opencode":
        standalone = ["--standalone"] if opencode_v2 else []
        return [*program, *standalone, *perms, *flags, *extra, "--prompt", prompt], True
    return [*program, *flags, *perms, *extra], False


def interactive_resume(
    harness: str,
    program: list[str],
    flags: list[str],
    granted: list[str],
    perms: list[str],
    extra: list[str],
    conversation: str,
    *,
    ask: bool = False,
    fork: bool = False,
    opencode_v2: bool = False,
) -> list[str]:
    """Open ``conversation`` interactively; with ``ask`` its hub tools are off."""

    if harness == "claude-code":
        words = [*program, *flags, *perms]
        # No --mcp-config with --strict-mcp-config: no MCP server at all.
        words += ["--strict-mcp-config"] if ask else granted
        words += ["--fork-session"] if fork else []
        return [*words, *extra, "--resume", conversation]
    if harness == "codex":
        words = [*program, "fork" if fork else "resume"]
        words += ["-c", "mcp_servers.robomate.enabled=false"] if ask else granted
        return [*words, *perms, *flags, *extra, conversation]
    if harness == "opencode":
        standalone = ["--standalone"] if opencode_v2 else []
        words = [*program, *standalone, *perms, *flags, *(["--fork"] if fork else [])]
        return [*words, *extra, "--session", conversation]
    if harness == "antigravity":
        return [*program, *flags, *perms, *extra, "--conversation", conversation]
    return [*program, *flags, *perms, *extra, "--resume", conversation]


# -- the session log ------------------------------------------------------------------


def log_path(sessions: Path, agent: str) -> Path:
    return sessions / f"{agent}.jsonl"


def bridge_telemetry(sessions: Path, agent: str) -> Path:
    """The bridge's own JSONL, beside the launch log (#512).

    The launcher reads it after each child to learn whether that child joined,
    and to stop on a refused takeover or an unbound bridge. It is always an
    absolute path, because the bridge refuses a relative ``HUB_TELEMETRY_LOG``.
    """

    return (sessions / f"{agent}-bridge.jsonl").resolve()


def telemetry_rows(path: Path, offset: int) -> list[dict[str, Any]]:
    """Rows appended to ``path`` after ``offset``; partial or malformed lines are skipped.

    Like ``agent_worker.released``: a line still being written, or one that is
    not a JSON object, is not evidence either way.
    """

    try:
        with path.open("rb") as stream:
            stream.seek(offset)
            data = stream.read()
    except FileNotFoundError:
        return []
    rows: list[dict[str, Any]] = []
    for line in data.decode("utf-8", errors="replace").splitlines():
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if isinstance(row, dict):
            rows.append(row)
    return rows


def saved_join(rows: list[dict[str, Any]], conversation: str | None) -> bool:
    """Whether ``conversation`` has a recorded successful join (#512)."""

    return conversation is not None and any(
        row.get("event") == "joined" and row.get("conversation_id") == conversation for row in rows
    )


def saved_rows(path: Path) -> list[dict[str, Any]]:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        return []
    rows = []
    for line in lines:
        try:
            row = json.loads(line)
        except ValueError:
            raise common.Stop(
                common.EXIT_CANNOT_RESUME,
                "session_log",
                f"Incomplete or corrupt session log {path}. Inspect it; do not start the "
                "agent again over it.",
            ) from None
        if isinstance(row, dict):
            rows.append(row)
    return rows


def saved_settings(path: Path, agent: str) -> dict[str, Any]:
    """The newest launch row's harness, role, model, effort and worktree."""

    for row in reversed(saved_rows(path)):
        if (
            row.get("event") in LAUNCH_EVENTS
            and row.get("agent") == agent
            and row.get("harness") in PROGRAMS
            and row.get("role") in ROLES
            and isinstance(row.get("workdir"), str)
        ):
            return row
    raise common.Stop(
        common.EXIT_CANNOT_RESUME,
        "no_session",
        f"No saved session for {agent} in {path}: nothing to resume, and the kickoff is never "
        f"replayed. Start the agent with agent-launcher.py start {agent} instead.",
    )


def set_aside(path: Path, harness: str) -> None:
    """Keep an earlier harness's log beside the new one: recovery reads one harness per log."""

    rows = saved_rows(path)
    if not any(row.get("harness") not in (None, harness) for row in rows):
        return
    stem = path.with_suffix("")
    for number in range(1, 1000):
        target = stem.with_name(f"{stem.name}.{number}.jsonl")
        if not target.exists():
            path.rename(target)
            say(f"earlier log of another harness kept as {target}")
            return
    raise Usage(f"too many earlier logs beside {path}; move some aside")


# -- stopping by hand (#534) ----------------------------------------------------------


def live_rows(path: Path) -> list[dict[str, Any]]:
    """Complete rows for a log another process may be appending to; skip a partial line."""

    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError):
        return []
    rows: list[dict[str, Any]] = []
    for line in lines:
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if isinstance(row, dict):
            rows.append(row)
    return rows


def newest_launcher_pid(rows: list[dict[str, Any]]) -> int | None:
    """The launcher PID a launch row recorded, for a log or lock without one (#534)."""

    for row in reversed(rows):
        pid = row.get("launcher_pid")
        if row.get("event") in LAUNCH_EVENTS and isinstance(pid, int) and pid > 0:
            return pid
    return None


def report_events(events: list[str]) -> str:
    return ", ".join(events) if events else "none"


def stop_launcher(log: Path, agent: str, pid: int) -> int:
    """SIGTERM the attached launcher and wait, boundedly, for its stop row (#534)."""

    before = len(live_rows(log))
    if process_alive(pid):
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        except OSError as exc:
            raise common.Stop(
                common.EXIT_CANNOT_RESUME,
                "stop_unidentified",
                f"Could not signal {agent}'s launcher pid {pid}: {exc}. Nothing was changed.",
            ) from None
    deadline = time.monotonic() + STOP_WAIT_S
    while time.monotonic() < deadline:
        new = live_rows(log)[before:]
        events = [str(row.get("event")) for row in new]
        if "stop" in events:
            say(f"stopped {agent}'s launcher pid {pid}; rows written: {report_events(events)}")
            return 0
        if not process_alive(pid):
            break
        time.sleep(STOP_POLL_S)
    if not process_alive(pid):
        # A hard stop (Windows TerminateProcess, SIGKILL) leaves the harness
        # running and wrote no stop row: finish the job the way the orphan
        # path does, so a later resume still finds a recorded exit. Never do
        # that while the lock is still held by someone else.
        new = live_rows(log)[before:]
        if "stop" in [str(row.get("event")) for row in new]:
            say(f"stopped {agent}'s launcher pid {pid}")
            return 0
        if launcher_attached(log):
            holder = lock_holder_pid(log)
            if holder is not None and holder != pid:
                raise common.Stop(
                    common.EXIT_CANNOT_RESUME,
                    "stop_unconfirmed",
                    f"{agent}'s launcher lock is held by pid {holder}, not the recorded pid "
                    f"{pid}. Inspect that process; nothing else was changed.",
                )
            # A terminated holder's lock can take a moment to clear on Windows.
            deadline = time.monotonic() + LOCK_SETTLE_S
            while launcher_attached(log) and time.monotonic() < deadline:
                holder = lock_holder_pid(log)
                if holder is not None and holder != pid:
                    raise common.Stop(
                        common.EXIT_CANNOT_RESUME,
                        "stop_unconfirmed",
                        f"{agent}'s launcher lock is held by pid {holder}, not the recorded "
                        f"pid {pid}. Inspect that process; nothing else was changed.",
                    )
                time.sleep(STOP_POLL_S)
            if launcher_attached(log) and lock_holder_pid(log) != pid:
                # Still held by an unknown or different holder: never stop a
                # harness that may belong to the new holder (#534 r2-1).
                holder = lock_holder_pid(log)
                by = f" by pid {holder}" if holder is not None else ""
                raise common.Stop(
                    common.EXIT_CANNOT_RESUME,
                    "stop_unconfirmed",
                    f"{agent}'s launcher pid {pid} is gone, but its lock is still held{by}. "
                    "Inspect that process; nothing else was changed.",
                )
        say(f"{agent}'s launcher pid {pid} exited without a stop row")
        return stop_orphaned(log, agent)
    raise common.Stop(
        common.EXIT_CANNOT_RESUME,
        "stop_unconfirmed",
        f"Sent SIGTERM to {agent}'s launcher pid {pid}, but no stop row appeared within "
        f"{STOP_WAIT_S:g} s. Inspect that process; nothing else was changed.",
    )


def terminate_recorded(pid: int) -> bool:
    """Terminate one recorded harness process, boundedly; True once it is gone."""

    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        return True
    except OSError as exc:
        say(f"could not signal harness pid {pid}: {exc}")
        return False
    deadline = time.monotonic() + common.CHILD_KILL_AFTER_S
    while time.monotonic() < deadline:
        if not process_alive(pid):
            return True
        time.sleep(STOP_POLL_S)
    if os.name != "nt":
        try:
            os.kill(pid, signal.SIGKILL)
        except OSError:
            return not process_alive(pid)
        deadline = time.monotonic() + common.CHILD_KILL_AFTER_S
        while time.monotonic() < deadline:
            if not process_alive(pid):
                return True
            time.sleep(STOP_POLL_S)
    return not process_alive(pid)


def launch_conversation(rows: Sequence[Mapping[str, Any]], last: int) -> str | None:
    """The conversation the launch at ``last`` recorded, from that launch's own rows (#571).

    A start of a harness that prints its conversation ID records it after the
    launch row; the launcher's own ``joined`` row then named that conversation.
    """

    for index in range(last, len(rows)):
        if index != last and rows[index].get("event") in LAUNCH_EVENTS:
            break
        conversation = rows[index].get("conversation_id")
        if isinstance(conversation, str) and conversation:
            return conversation
    return None


def launch_joined(log: Path, agent: str, launch: Mapping[str, Any]) -> bool:
    """Whether this launch's own bridge telemetry records a successful join (#571).

    Launch rows record where that child's bridge telemetry starts. An older row
    without that offset gets no ``joined`` row: its rows cannot be told apart
    from another launch's, so they are never scanned.
    """

    offset = launch.get("telemetry_offset")
    if not isinstance(offset, int) or offset < 0:
        return False
    return any(
        row.get("event") == "tool_call"
        and row.get("tool") == "join"
        and row.get("phase") == "success"
        for row in telemetry_rows(bridge_telemetry(log.parent, agent), offset)
    )


def record_stopped_join(
    log: Path, agent: str, launch: Mapping[str, Any], conversation: str | None
) -> None:
    """Write the joined row the hard-killed launcher's own scan would have (#571)."""

    common.log_session(
        log,
        event="joined",
        agent=str(launch.get("agent") or agent),
        harness=launch.get("harness"),
        conversation_id=conversation,
        hub_id=launch.get("hub_id"),
        workflow_id=launch.get("workflow_id"),
        role=launch.get("role"),
        run_id=launch.get("run_id"),
    )


def record_stopped_exit(log: Path, agent: str, launch: Mapping[str, Any], pid: int) -> None:
    """Write the exit row for a harness ``stop`` terminated in the launcher's place."""

    common.log_session(
        log,
        event="exit",
        agent=str(launch.get("agent") or agent),
        harness=launch.get("harness"),
        role=launch.get("role"),
        conversation_id=launch.get("conversation_id"),
        hub_id=launch.get("hub_id"),
        workflow_id=launch.get("workflow_id"),
        run_id=launch.get("run_id"),
        pid=pid,
        exit_code=None,
        exited_at=datetime.now(UTC).isoformat(timespec="seconds"),
        stopped_by="agent-launcher stop",
    )


def stop_orphaned(log: Path, agent: str) -> int:
    """Stop the last launch's harness when no launcher is attached (#534)."""

    rows = saved_rows(log)
    launches = [i for i, row in enumerate(rows) if row.get("event") in LAUNCH_EVENTS]
    if not launches:
        say(f"Nothing is running for {agent}: no launch is recorded. Nothing was changed.")
        return 0
    last = launches[-1]
    launch = rows[last]
    if any(row.get("event") == "exit" for row in rows[last + 1 :]):
        say(f"Nothing is running for {agent}: the last launch has its exit row.")
        return 0
    pid = launch.get("pid")
    if not isinstance(pid, int) or pid <= 0:
        say(f"Nothing is running for {agent}: the last launch records no harness PID.")
        return 0
    if not process_alive(pid):
        say(
            f"Nothing is running for {agent}: recorded harness pid {pid} is gone. Nothing was "
            "changed; use --confirm-stopped on resume if no exit was recorded."
        )
        return 0
    recorded = launch.get("process")
    current = process_name(pid)
    if not isinstance(recorded, str) or not recorded:
        raise common.Stop(
            common.EXIT_CANNOT_RESUME,
            "stop_unidentified",
            f"{agent}'s recorded harness pid {pid} is alive, but this launch recorded no "
            "process identity, so it may have been reused. Check the process yourself before "
            "stopping it; nothing was changed.",
        )
    if current.startswith("unavailable") or current != recorded:
        raise common.Stop(
            common.EXIT_CANNOT_RESUME,
            "stop_unidentified",
            f"{agent}'s recorded harness pid {pid} is now {current!r}, recorded as "
            f"{recorded!r}; the PID may have been reused. Left it alone. Verify the process "
            "before stopping it; nothing was changed.",
        )
    if not terminate_recorded(pid):
        raise common.Stop(
            common.EXIT_CANNOT_RESUME,
            "stop_unconfirmed",
            f"Could not confirm that {agent}'s harness pid {pid} exited; no exit row was "
            "written. Inspect that process.",
        )
    if launch_joined(log, agent, launch):
        record_stopped_join(log, agent, launch, launch_conversation(rows, last))
    record_stopped_exit(log, agent, launch, pid)
    say(
        f"stopped {agent}'s recorded harness pid {pid}; wrote an exit row marked "
        '"stopped_by": "agent-launcher stop".'
    )
    return 0


def latest_launch_overridden(rows: Sequence[Mapping[str, Any]], agent: str) -> bool:
    """Whether ``agent``'s newest launch row overrode sandbox preflight (#554)."""

    for row in reversed(rows):
        if row.get("event") in LAUNCH_EVENTS and row.get("agent") == agent:
            return row.get("sandbox_check") == "overridden"
    return False


def stop_note(agent: str, *, allow_unsandboxed: bool = False) -> str:
    """What stays true after a local stop, and how the operator relaunches (#660)."""

    flag = " --allow-unsandboxed" if allow_unsandboxed else ""
    return (
        "The hub's workflow and agent rows were left as they are, and nothing supervises the "
        f"agent until you relaunch: {agent} uses a fresh `agent-launcher.py start` for a new "
        f"workflow, or `agent-launcher.py resume {agent}{flag}` only to continue the same one."
    )


def stop_agent(args: argparse.Namespace, extra: list[str], tools: Tools) -> int:
    """Stop the agent's launcher, or its recorded harness, without touching the hub."""

    del extra, tools
    sessions = Path(args.sessions_dir) if args.sessions_dir else locate(args).sessions
    log = log_path(sessions, args.agent)
    attached = launcher_attached(log)
    # An attached launcher may be mid-append: tolerate a partial last line.
    rows = live_rows(log) if attached else saved_rows(log)
    if attached:
        # A just-acquired holder replaces the lock body after taking it: prefer
        # the PID recorded a moment later so a stale predecessor is not signalled.
        pid = lock_holder_pid(log)
        time.sleep(STOP_POLL_S)
        pid = lock_holder_pid(log) or pid
        pid = pid or newest_launcher_pid(rows)
        if pid is None:
            raise common.Stop(
                common.EXIT_CANNOT_RESUME,
                "stop_unidentified",
                f"A launcher holds {args.agent}'s lock but recorded no PID. Inspect that "
                "process yourself; nothing was changed.",
            )
        code = stop_launcher(log, args.agent, pid)
    else:
        code = stop_orphaned(log, args.agent)
    if code == 0:
        latest = live_rows(log) if attached else saved_rows(log)
        say(stop_note(args.agent, allow_unsandboxed=latest_launch_overridden(latest, args.agent)))
    return code


# -- supervised launchers -------------------------------------------------------------


class Joined(common.Launcher):
    """Supervise a single joined agent.

    The first prompt is the join line. A resume's continuation asks for a
    takeover only when this conversation has a recorded join (#512); each
    child's bridge telemetry tells the launcher which. Rows carry the role,
    the run ID and the launch settings, and a finished earlier run on the hub
    is never pinned at start.
    """

    role = "worker"
    run_id: str | None = None
    launched = False
    joined = False
    pending_stop: tuple[str, common.Stop] | None = None
    """A terminal join outcome, raised after the child's exit row is written (r1-1 on #512)."""
    join_offset: int | None = None
    """Where the current child's bridge telemetry starts, for an interrupted scan (#534)."""

    def setup(self, args: argparse.Namespace) -> None:
        self.agent = args.agent
        self.role = args.role
        self.orchestrator = args.role == "orchestrator"
        self.joined = False
        self.pending_stop = None
        self.join_offset = None
        self.continue_prompt = continuation(args.agent, args.role, joined=False)

    def resume_name(self) -> str:
        flag = " --allow-unsandboxed" if getattr(self.args, "allow_unsandboxed", False) else ""
        return f"agent-launcher.py resume {self.agent}{flag}"

    def start_name(self) -> str:
        return f"agent-launcher.py start {self.agent}"

    def stopped_note(self, reason: str, workflow: dict[str, Any] | None) -> str | None:
        """The per-workflow contract in the operator's words (#465, M3 Step 6).

        The hub's standby policy keeps agent rows for a queued or next run, but
        this launcher supervises one workflow: the next run needs a fresh
        launcher and must never inherit this conversation as its supervisor.
        """
        if reason == "released":
            return (
                f"{self.agent} was released by the hub, so this supervisor is finished. "
                "Supervision is per workflow: start a fresh launcher when the agent is next "
                "needed."
            )
        run = (workflow or {}).get("run_id")
        where = f" {run}" if isinstance(run, str) and run else ""
        successor = (workflow or {}).get("successor")
        if isinstance(successor, str) and successor:
            successor_run = (workflow or {}).get("successor_run_id")
            following = (
                f" {successor_run}" if isinstance(successor_run, str) and successor_run else ""
            )
            head = (
                f"workflow{where} is no longer the hub's live workflow: it ended and the hub "
                f"moved to {successor}{following}"
            )
        else:
            head = f"workflow{where} is done"
        agent = (workflow or {}).get("agent")
        kept = (
            " The hub keeps the agent row joined (on_done standby or a queued run), but "
            "supervision does not follow it."
            if isinstance(agent, dict) and agent.get("status") != "released"
            else ""
        )
        return (
            f"{head}; this launcher supervised that workflow only and is stopping.{kept} "
            f"Start a fresh launcher ({self.start_name()} ...) for the next queued or new "
            "workflow; this conversation is never resumed for another workflow."
        )

    def workflow_moved_on(self, workflow: dict[str, Any] | None) -> bool:
        """A pinned workflow replaced by a live one ended, not an identity error (#660).

        One workflow runs at a time, so a different live workflow after this
        launcher pinned one can only mean the pinned workflow ended and a
        queued run was activated. Stop for the pinned workflow and name the
        successor; never adopt it. A manual resume keeps the strict identity
        refusal, because it names the conversation and workflow to continue.
        """

        return not getattr(self.args, "resume_session", None)

    def ask_hint(self) -> str:
        """The full ask command, runnable from any directory and naming the hub (#491)."""

        words = [
            "uv",
            "run",
            "--locked",
            "--project",
            str(ROOT),
            "python",
            str(SCRIPTS / "agent-launcher.py"),
            "ask",
            self.agent,
        ]
        checkout = getattr(self.args, "hub_checkout", None)
        if checkout:
            words += ["--hub", str(checkout)]
        else:
            # A remote host's clone has no local hub record; ask needs only the logs.
            words += ["--sessions-dir", str(self.args.sessions.parent)]
        return command_line(words)

    def first_launch(self) -> common.Launch:
        text = join_line(self.agent, self.role)
        return common.text_launch(self.harness, self.base, text, self.conversation or "")

    def bridge_log(self) -> Path:
        return bridge_telemetry(self.args.sessions.parent, self.agent)

    def scan_join(self, offset: int) -> None:
        """Learn from the bridge's own rows whether this child joined (#512).

        A successful join makes later resumes a takeover of this conversation's
        own seat. A refused takeover, or a bridge with no hub, is terminal: the
        launcher stops instead of re-taking a seat it does not hold. The stop
        is raised from ``resumable`` so the child's exit row is written first.
        """

        for row in telemetry_rows(self.bridge_log(), offset):
            if row.get("event") != "tool_call" or row.get("tool") != "join":
                continue
            phase = row.get("phase")
            if phase == "success":
                if not self.joined:
                    self.joined = True
                    self.log("joined")
            elif phase == "error":
                error = row.get("error")
                if not isinstance(error, str):
                    continue
                if "takeover refused:" in error:
                    self.pending_stop = (
                        "takeover_refused",
                        common.Stop(
                            common.EXIT_CANNOT_RESUME,
                            "takeover_refused",
                            "the resumed join was refused: another session's agent holds this "
                            "seat. Check `robomate status` and release or stop the other "
                            f"session, then run {self.resume_name()}.",
                        ),
                    )
                    return
                if "this robomate bridge has no hub" in error:
                    self.pending_stop = (
                        "no_hub",
                        common.Stop(
                            common.EXIT_CANNOT_RESUME,
                            "no_hub",
                            "the bridge reported no hub and will not choose one. Check "
                            "`robomate status` and the launcher's hub selection, then run "
                            f"{self.resume_name()}.",
                        ),
                    )
                    return

    def resumable(self, exit_code: int, resumes: int) -> str:
        """Raise a terminal join outcome after the loop has logged the child's exit."""

        if self.pending_stop is not None:
            event, stop = self.pending_stop
            self.pending_stop = None
            self.log(event)
            raise stop
        return super().resumable(exit_code, resumes)

    def manual_resume_text(self) -> str:
        """The manual resume's text, matching this recorded conversation (#512)."""

        self.joined = saved_join(saved_rows(self.args.sessions), self.conversation)
        self.continue_prompt = continuation(self.agent, self.role, joined=self.joined)
        return self.continue_prompt

    def log(self, event: str, **fields: object) -> None:
        extra: dict[str, object] = {"role": self.role, "run_id": self.run_id}
        if event in LAUNCH_EVENTS:
            extra |= {
                "model": self.args.model,
                "effort": self.args.effort,
                "workdir": str(self.args.cwd),
                "interactive": False,
                **self.args.identity.row(),
            }
            sandbox_check = getattr(self.args, "sandbox_check", None)
            if sandbox_check is not None:
                extra |= sandbox_check.row()
        super().log(event, **extra, **fields)

    def launch_fields(self, action: str, resumes: int, launch_id: str) -> dict[str, Any]:
        """Also record where this child's bridge telemetry starts (#571).

        An orphan ``stop`` reads only the rows this launch appended, so a join
        recorded by another launch is never taken for this one's.
        """

        fields = super().launch_fields(action, resumes, launch_id)
        fields["telemetry_offset"] = self.join_offset
        return fields

    def run_child(self, launch: common.Launch, action: str, resumes: int) -> int:
        if resumes == 0 and (not self.args.recover or not (Path(self.args.cwd) / ".venv").is_dir()):
            setup_workspace(self.args.identity.workspace_setup, Path(self.args.cwd), self.args.env)
        self.launched = True
        try:
            offset = self.bridge_log().stat().st_size
        except FileNotFoundError:
            offset = 0
        self.join_offset = offset
        code = super().run_child(launch, action, resumes)
        self.scan_join(offset)
        self.continue_prompt = continuation(self.agent, self.role, joined=self.joined)
        return code

    def log_interrupted_exit(self, code: int) -> None:
        """Keep the #512 join scan before a KeyboardInterrupt's exit row (#534)."""

        if self.join_offset is not None:
            self.scan_join(self.join_offset)
            self.continue_prompt = continuation(self.agent, self.role, joined=self.joined)
        super().log_interrupted_exit(code)

    def read_hub(self) -> dict[str, Any] | None:
        pinned = self.workflow_id
        workflow = super().read_hub()
        if pinned is None and workflow is not None and workflow["status"] == "done":
            # An earlier run, finished before this start: not this agent's (#223).
            self.workflow_id = None
            return None
        if pinned is None and workflow is not None and self.workflow_id is not None:
            say(
                f"supervising workflow {workflow['id']} ({workflow['status']}); this launcher "
                "follows one workflow and stops when it is done"
            )
        if workflow is not None and workflow.get("run_id"):
            self.run_id = str(workflow["run_id"])
        return workflow

    def refresh_workflow_id_best_effort(self) -> None:
        if self.workflow_id is not None:
            return
        try:
            hub_id, fresh = self.reader.read()
        except (OSError, ValueError, urllib.error.URLError):
            return
        if fresh is None or fresh["status"] == "done":
            return
        if self.hub_id is None or hub_id == self.hub_id:
            self.hub_id = self.hub_id or hub_id
            self.workflow_id = fresh["id"]
            self.run_id = fresh.get("run_id") or self.run_id


class JoinedOrchestrator(Joined):
    def __init__(self, args: argparse.Namespace, reader: common.HubReader | None = None) -> None:
        super().__init__(args, reader)
        self.setup(args)

    def resumable(self, exit_code: int, resumes: int) -> str:
        if self.pending_stop is None and self.bridge_completed_run():
            self.log("stop", reason="done", workflow_status="done", resumes=resumes)
            say(f"workflow done after {resumes} automatic resume(s) (confirmed by the bridge)")
            self.announce_stop("done", {"id": self.workflow_id, "run_id": self.run_id})
            return "done"
        workflow = self.read_hub()
        if (
            self.pending_stop is None
            and not self.joined
            and not (workflow is not None and workflow["status"] == "done")
        ):
            raise common.Stop(
                common.EXIT_NEVER_JOINED,
                "never_joined",
                f"{self.agent} never joined the hub; inspect the harness log "
                f"and {self.args.sessions}.",
            )
        return super().resumable(exit_code, resumes)

    def bridge_completed_run(self) -> bool:
        """The current harness's bridge received the hub's acknowledgement of done (#703)."""
        if self.join_offset is None or not self.hub_id or not self.run_id:
            return False
        return any(
            row.get("event") == "workflow_done"
            and row.get("agent") == self.agent
            and row.get("hub_id") == self.hub_id
            and row.get("run_id") == self.run_id
            for row in telemetry_rows(self.bridge_log(), self.join_offset)
        )


_UNSEEN = object()


class JoinedWorker(Joined, WorkerLauncher):
    def __init__(self, args: argparse.Namespace, reader: common.HubReader | None = None) -> None:
        super().__init__(args, reader)
        self.setup(args)
        self.stale_release: object = _UNSEEN
        self.start_instance: object = _UNSEEN

    def manual_resume_hint(self) -> str:
        return (
            f"Run {self.resume_name()}: it checks the predecessor and the hub before it "
            "resumes the saved conversation."
        )

    def own_join_evidence(self, agent: dict[str, Any] | None) -> bool:
        """This launcher's own join, not an earlier run's row on a reused hub (#546).

        ``Joined.joined`` is the bridge's own record of a join. Otherwise the
        row counts only when its instance differs from the one seen before the
        first launch: a reused hub keeps the earlier run's row and instance in
        place, so presence alone proves nothing.
        """
        instance = agent["activity"]["instance"] if agent is not None else None
        if self.start_instance is _UNSEEN:
            self.start_instance = instance
            return False
        return self.joined or (instance is not None and instance != self.start_instance)

    def before_launch(self, resumes: int) -> bool:
        """An automatic resume does not wait for its predecessor's bridge to be lost.

        This launcher's own harness has exited; a bridge it left behind is
        superseded by the successor's join with takeover, which reattaches the
        held task (§5, §7.5). The first launch and manual resume still verify
        predecessor status.
        """

        if resumes == 0:
            return bool(super().before_launch(resumes))
        workflow = self.read_hub()
        if self.finished(workflow, resumes):
            return False
        if workflow is None:
            raise common.Stop(
                common.EXIT_CANNOT_RESUME,
                "no_workflow",
                "no workflow to resume. " + self.manual_resume_hint(),
            )
        if workflow["status"] == "paused":
            raise common.Stop(
                common.EXIT_PAUSED,
                "paused",
                "workflow paused; wait until the operator resumes it. " + self.manual_resume_hint(),
            )
        return True

    def finished(self, workflow: dict[str, Any] | None, resumes: int) -> bool:
        agent = workflow.get("agent") if workflow else None
        if workflow is not None and isinstance(agent, dict) and agent.get("status") == "released":
            if not self.launched and self.stale_release is _UNSEEN:
                # Released in an earlier run, before this start: the join resets it.
                self.stale_release = agent.get("context_id")
            if agent.get("context_id") == self.stale_release:
                workflow = workflow | {"agent": agent | {"status": "idle"}}
        return super().finished(workflow, resumes)

    def released_locally(self) -> bool:
        """The current child's bridge saw the hub release this agent (#705).

        Mirrors ``JoinedOrchestrator.bridge_completed_run`` (#703): only rows
        after this child's ``join_offset`` count, from this agent and bound to
        the pinned hub, so an earlier child's or another hub's record is never
        taken for this launch. A release stays decisive when the hub is gone.
        """
        if self.join_offset is None or not self.hub_id:
            return False
        return any(
            row.get("event") == "tool_call"
            and row.get("tool") == "await_assignment"
            and row.get("phase") == "success"
            and row.get("outcome") == "release"
            and row.get("agent") == self.agent
            and row.get("hub_id") == self.hub_id
            for row in telemetry_rows(self.bridge_log(), self.join_offset)
        )


# -- the subcommands ------------------------------------------------------------------


@dataclass
class Tools:
    """What tests replace: the harness programs, the hub reader and ``robomate workspace``."""

    programs: Programs
    reader: common.HubReader | None = None
    workspace: Callable[[str, str | None], Path] = robomate_workspace


def program_for(tools: Tools, harness: str) -> list[str]:
    program = list(tools.programs.get(harness) or PROGRAMS[harness])
    return [shutil.which(program[0]) or program[0], *program[1:]]


def supervision_args(args: argparse.Namespace, place: Place, **fields: Any) -> argparse.Namespace:
    """Arguments for shared headless supervision."""

    values = {
        "hub_url": place.url,
        "token_file": place.token_file,
        "hub_checkout": str(place.checkout) if place.checkout is not None else None,
        "telemetry": None,
        "prompt": None,
        "resume_session": None,
        "resume_prompt": None,
        "recover": False,
        "hub_id": None,
        "workflow_id": None,
        "stopped_at": None,
        "confirm_stopped": False,
        "force": False,
        "allow_unsandboxed": False,
        "sandbox_check": None,
    }
    for key in (
        "max_resumes",
        "resume_delay_s",
        "resume_max_delay_s",
        "resume_total_s",
        "resume_series_reset_s",
        "warn_series_resets",
        "max_series_resets",
        "read_retries",
        "read_retry_delay_s",
        "predecessor_wait_s",
        "predecessor_poll_s",
    ):
        values[key] = getattr(args, key)
    return argparse.Namespace(**(values | fields))


def make_reader(tools: Tools, place: Place, agent: str, role: str) -> common.HubReader:
    if tools.reader is not None:
        return tools.reader
    if place.token_file is None:
        raise Usage("the hub's token file is unknown: name it with ROBOMATE_TOKEN_FILE")
    if role == "orchestrator":
        return common.HubReader(place.url, place.token_file)
    return WorkerReader(place.url, place.token_file, agent)


def supervise(launcher: Joined, sessions: Path) -> int:
    """Run headless supervision with this launcher's recovery hints."""

    with common.interrupt_handlers(launcher):
        try:
            with agent_lock(sessions):
                try:
                    return launcher.run()
                except KeyboardInterrupt:
                    launcher.interrupted_fallback()
                    return stopped(
                        launcher, common.Stop(common.EXIT_INTERRUPTED, "interrupted", "")
                    )
                except common.Stop as stop:
                    return stopped(launcher, stop)
        except common.Stop as stop:
            say(str(stop))
            return stop.code
        except common.OpenCodeConfigError as exc:
            say(f"harness configuration failure: {exc}")
            return common.EXIT_CANNOT_RESUME
        except (OSError, ValueError, urllib.error.URLError) as exc:
            say(
                f"launch unavailable: {exc}. Restore hub reachability, then run "
                f"{launcher.resume_name()}"
            )
            return common.EXIT_HUB_UNREADABLE
        finally:
            launcher.stop_child(wait=True)


def stopped(launcher: Joined, stop: common.Stop) -> int:
    launcher.log("stop", reason=stop.reason, exit_code=stop.code)
    if stop.code == common.EXIT_INTERRUPTED:
        say("interrupted; stopped the harness this launcher started and launched nothing more")
    else:
        say(str(stop))
        if launcher.args.recover:
            say(f"To only ask {launcher.agent} questions: {launcher.ask_hint()}")
    return stop.code


def announce(harness: str, safe: bool, extra: list[str]) -> None:
    mode = "safe-mode (prompting defaults)" if safe else "run permissions"
    print(f"Interactive launch: {mode}; harness arguments: {extra!a}", flush=True)


def best_effort_hub(place: Place, tools: Tools, agent: str, role: str) -> dict[str, Any]:
    """Hub, workflow and run IDs for an interactive row, when the hub answers."""

    try:
        hub_id, workflow = make_reader(tools, place, agent, role).read()
    except (OSError, ValueError, urllib.error.URLError, Usage):
        return {"hub_id": None, "workflow_id": None, "run_id": None}
    live = workflow is not None and workflow["status"] != "done"
    return {
        "hub_id": hub_id,
        "workflow_id": workflow["id"] if live and workflow else None,
        "run_id": workflow.get("run_id") if live and workflow else None,
    }


_UUID_FILE = re.compile(r"([0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12})\.jsonl$")


def codex_rollouts(home: Path) -> set[Path]:
    """Every rollout file in Codex's session store under ``home``."""
    try:
        return set((home / "sessions").rglob("rollout-*.jsonl"))
    except OSError:
        return set()


def codex_conversation(home: Path, before: set[Path], workdir: Path) -> str | None:
    """The one Codex session this launch created in ``workdir``, from Codex's own store.

    Only a rollout that did not exist before the harness was spawned is a
    candidate, so an earlier conversation, even one appended to meanwhile, is
    never taken for this one (#247 r2-1). Codex names each rollout
    ``rollout-<time>-<id>.jsonl`` and starts it with a ``session_meta`` line;
    that line must name the same ID and this worktree. Anything but exactly
    one such rollout is no capture, and the operator supplies the ID.
    """
    found = set()
    for path in codex_rollouts(home) - before:
        match = _UUID_FILE.search(path.name)
        if match is None:
            continue
        try:
            with path.open(encoding="utf-8") as stream:
                first = json.loads(stream.readline())
        except (OSError, ValueError):
            continue
        meta = first.get("payload") if isinstance(first, dict) else None
        if (
            not isinstance(meta, dict)
            or first.get("type") != "session_meta"
            or str(meta.get("id", "")).lower() != match.group(1).lower()
            or not isinstance(meta.get("cwd"), str)
            or Path(meta["cwd"]).resolve() != workdir.resolve()
        ):
            continue
        found.add(match.group(1))
    return found.pop() if len(found) == 1 else None


def capture_remedy(
    harness: str, agent: str, workdir: Path, *, allow_unsandboxed: bool = False
) -> str:
    """What the operator does when an interactive harness's conversation ID is not captured."""
    where = {
        "codex": f"run `codex resume` in {workdir} to list its sessions",
        "opencode": f"run `opencode session list` in {workdir}",
    }.get(harness, f"use {harness}'s own session list for {workdir}")
    flag = " --allow-unsandboxed" if allow_unsandboxed else ""
    return (
        f"To resume or ask later, find the exact ID ({where}) and pass it: "
        f"agent-launcher.py resume {agent} --resume-session ID{flag}, or agent-launcher.py ask "
        f"{agent} ID. Never use --last or --continue: they may pick another agent's."
    )


def run_interactive(
    argv: list[str],
    *,
    log: Path,
    row: dict[str, Any],
    event: str,
    cwd: Path,
    env: Mapping[str, str],
) -> int:
    """One interactive harness, recorded but never resumed automatically (#222).

    A Codex session's ID is looked up in Codex's store after it ends; without
    an exact capture the operator is told how to supply the ID by hand.
    """

    codex_home = Path(env.get("CODEX_HOME") or Path.home() / ".codex")
    before = codex_rollouts(codex_home) if row["harness"] == "codex" else set()
    child = subprocess.Popen(argv, cwd=cwd, env=dict(env))
    row = row | {"launcher_pid": os.getpid(), "process": common.process_identity(child.pid)}
    common.log_session(log, event=event, pid=child.pid, interactive=True, **row)
    try:
        code = child.wait()
    except KeyboardInterrupt:
        code = child.wait()
    if row["conversation_id"] is None:
        found = None
        if row["harness"] == "codex":
            found = codex_conversation(codex_home, before, cwd)
        if found is not None:
            row = row | {"conversation_id": found}
            common.log_session(
                log, event="conversation", conversation_source="codex session store", **row
            )
            say(f"conversation {found} (from the Codex session store)")
        else:
            say(
                "no conversation ID was captured. "
                + capture_remedy(
                    row["harness"],
                    row["agent"],
                    cwd,
                    allow_unsandboxed=row.get("sandbox_check") == "overridden",
                )
            )
    common.log_session(log, event="exit", exit_code=code, interactive=True, **row)
    return code


def supplied_conversation(log: Path, saved: Mapping[str, Any], supplied: str | None) -> None:
    """Record an operator-supplied ID for a launch that captured none (#222).

    Recovery refuses a start whose conversation was never recorded, so that
    no earlier conversation is reused; the operator's exact ID, given after
    an interactive start without capture, is recorded first instead.
    """
    rows = saved_rows(log)
    launches = [i for i, row in enumerate(rows) if row.get("event") in LAUNCH_EVENTS]
    if supplied is None or not launches or rows[launches[-1]].get("interactive") is not True:
        # A headless launch keeps #173's refusal and its documented remedy.
        return
    if any(isinstance(row.get("conversation_id"), str) for row in rows[launches[-1] :]):
        return
    if not exact_id(str(saved["harness"]), supplied):
        raise Usage(f"{supplied!r} is not an exact {saved['harness']} conversation ID")
    latest = rows[launches[-1]]
    common.log_session(
        log,
        event="conversation",
        conversation_source="operator",
        conversation_id=supplied,
        **{
            key: latest.get(key)
            for key in ("agent", "harness", "role", "hub_id", "workflow_id", "run_id")
        },
    )


@dataclass(frozen=True)
class SandboxCheck:
    """Result of the Linux sandbox preflight, recorded on launch rows (#554)."""

    status: str
    failure: str | None = None

    def row(self) -> dict[str, object]:
        fields: dict[str, object] = {"sandbox_check": self.status}
        if self.failure is not None:
            fields["sandbox_check_failure"] = self.failure
        return fields


def sandbox_preflight(
    harness: str, *, agent: str = "", allow_unsandboxed: bool = False
) -> SandboxCheck | None:
    """Check the Linux sandbox; refuse on failure unless explicitly overridden (#498, #554)."""
    if not sys.platform.startswith("linux") or harness not in common.SANDBOX_CHECK_HARNESSES:
        return None
    script = SCRIPTS / common.VERIFY_SANDBOXES_REL
    mode = "--codex" if harness == "codex" else "--claude"
    failure: str | None = None
    try:
        result = subprocess.run(
            [str(script), mode],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        failure = str(exc)
    else:
        if result.returncode:
            failure = result.stderr.strip() or result.stdout.strip() or f"exit {result.returncode}"
    if failure is None:
        if allow_unsandboxed:
            say(
                f"sandbox preflight passed for {agent or harness} ({harness}); "
                "--allow-unsandboxed was not needed"
            )
        return SandboxCheck(status="passed")
    if not allow_unsandboxed:
        raise Usage(
            f"sandbox preflight failed: {failure}; see docs/user-guide.md#linux-sandbox-grants"
        )
    say(common.sandbox_override_warning({agent or harness: (harness, failure)}))
    return SandboxCheck(status="overridden", failure=failure)


def warn_windows_sandbox(agent: str, harness: str) -> None:
    if sys.platform == "win32" and harness == "claude-code":
        say(
            f"warning: {agent} (claude-code) on Windows may run without a sandbox: "
            "Claude Code's Windows sandbox is not guaranteed and "
            "failIfUnavailable is off on this platform"
        )


def workspace_setup_command(setting: str, workdir: Path) -> str:
    """The setup command for this worktree; ``auto`` means the default for a uv project.

    The detection rule is a ``uv.lock`` at the worktree root, so a target that
    is not a uv project starts untouched. A blank setting or ``auto`` (compared
    case-insensitively, ignoring surrounding whitespace) asks for that default;
    ``none`` likewise turns setup off. Any other string is the command as given
    (#603, #682).
    """

    sentinel = setting.strip().lower()
    if sentinel == common.WORKSPACE_SETUP_NONE:
        return ""
    if sentinel not in ("", common.WORKSPACE_SETUP_AUTO):
        return setting
    return common.DEFAULT_WORKSPACE_SETUP if (workdir / "uv.lock").is_file() else ""


def setup_workspace(setting: str, workdir: Path, env: Mapping[str, str]) -> None:
    """Run the operator's setup in the worktree before starting the harness."""
    command = workspace_setup_command(setting, workdir)
    if not command.strip():
        return
    done = subprocess.run(
        command, shell=True, cwd=workdir, env=dict(env), capture_output=True, text=True
    )
    if done.returncode:
        output = (done.stdout + done.stderr).strip()
        raise Usage(
            f"workspace setup failed (exit {done.returncode}): {output}\n"
            "Fix the setup command or environment, then start or resume the agent again."
        )


def start(args: argparse.Namespace, extra: list[str], tools: Tools) -> int:
    preflight = sandbox_preflight(
        args.harness, agent=args.agent, allow_unsandboxed=args.allow_unsandboxed
    )
    try:
        check_mcp_entry(args.harness)
    except ValueError as exc:
        raise Usage(str(exc)) from exc
    place = resolve_hub_id(locate(args), tools, args.agent, args.role)
    if args.workdir:
        workdir = Path(args.workdir).resolve()
    elif place.selector is None and args.hub_url:
        raise Usage("a remote host passes --workdir: the path robomate workspace --clone printed")
    else:
        workdir = tools.workspace(args.agent, place.selector).resolve()
    if not workdir.is_dir():
        raise Usage(f"the worktree {workdir} does not exist")
    place.sessions.mkdir(parents=True, exist_ok=True)
    log = log_path(place.sessions, args.agent)
    program = program_for(tools, args.harness)
    oc_v2 = args.harness == "opencode" and is_opencode_v2(program, os.environ, workdir)
    flags = model_flags(
        args.harness,
        args.model,
        args.effort,
        interactive=args.interactive,
        opencode_v2=oc_v2,
    )
    identity = Identity(
        args.harness,
        args.model,
        args.effort,
        args.harness_version,
        Path(args.uv_cache_dir).resolve() if args.uv_cache_dir else None,
        args.pytest_workers,
        Path(args.lane_tmp_dir).resolve() if args.lane_tmp_dir else None,
        bridge_telemetry(place.sessions, args.agent),
        args.workspace_setup,
    )
    identity.prepare()
    warn_windows_sandbox(args.agent, args.harness)
    granted = grants(args.harness, workdir, place.token_file, identity.uv_cache)
    granted += identity.bridge_flags()
    granted += hub_flags(args.harness, place)
    with agent_lock(log):
        set_aside(log, args.harness)
    if args.interactive:
        perms = permissions(args.harness, args.safe_mode)
        session_id = str(uuid.uuid4()) if args.harness == "claude-code" else ""
        line = join_line(args.agent, args.role)
        argv, prompted = interactive_start(
            args.harness,
            program,
            flags,
            granted,
            perms,
            extra,
            prompt=line,
            session_id=session_id,
            opencode_v2=oc_v2,
        )
        env = identity.apply(
            common.launch_environment(
                workdir,
                child_environment(
                    place,
                    opencode_environment(
                        args.effort,
                        safe=args.safe_mode,
                        model=args.model,
                        v2=oc_v2,
                    )
                    if args.harness == "opencode"
                    else {},
                ),
            )
        )
        announce(args.harness, args.safe_mode, extra)
        if not prompted:
            print(f"Type this line once the harness is ready: {line}", flush=True)
        if not session_id:
            print(
                f"Note: {args.harness} does not tell an interactive launcher its conversation "
                + (
                    "ID; the launcher looks for it in the Codex session store afterwards. "
                    if args.harness == "codex"
                    else "ID. "
                )
                + capture_remedy(
                    args.harness,
                    args.agent,
                    workdir,
                    allow_unsandboxed=preflight is not None and preflight.status == "overridden",
                ),
                flush=True,
            )
        row = {
            "agent": args.agent,
            "harness": args.harness,
            "role": args.role,
            "model": args.model,
            "effort": args.effort,
            "workdir": str(workdir),
            "conversation_id": session_id or None,
            **identity.row(),
            **(preflight.row() if preflight is not None else {}),
            **best_effort_hub(place, tools, args.agent, args.role),
        }
        with agent_lock(log):
            setup_workspace(identity.workspace_setup, workdir, env)
            return run_interactive(argv, log=log, row=row, event="start", cwd=workdir, env=env)
    base = headless_base(
        args.harness,
        program,
        flags,
        granted,
        extra,
        title=f"{args.agent} {place.url or 'robomate'}",
        export=place.sessions / f"{args.agent}-devin-export.json",
        opencode_v2=oc_v2,
    )
    namespace = supervision_args(
        args,
        place,
        agent=args.agent,
        role=args.role,
        harness=args.harness,
        model=args.model,
        effort=args.effort,
        command=base,
        sessions=log,
        cwd=workdir,
        env=identity.apply(common.launch_environment(workdir, child_environment(place))),
        identity=identity,
        allow_unsandboxed=args.allow_unsandboxed,
        sandbox_check=preflight,
    )
    reader = make_reader(tools, place, args.agent, args.role)
    kind = JoinedOrchestrator if args.role == "orchestrator" else JoinedWorker
    return supervise(kind(namespace, reader), log)


def resume(args: argparse.Namespace, extra: list[str], tools: Tools) -> int:
    place = locate(args)
    log = log_path(place.sessions, args.agent)
    saved = saved_settings(log, args.agent)
    harness, role = str(saved["harness"]), str(saved["role"])
    preflight = sandbox_preflight(
        harness, agent=args.agent, allow_unsandboxed=args.allow_unsandboxed
    )
    place = resolve_hub_id(place, tools, args.agent, role)
    model, effort = str(saved.get("model") or ""), str(saved.get("effort") or "")
    workdir = Path(saved["workdir"])
    if not workdir.is_dir():
        raise common.Stop(
            common.EXIT_CANNOT_RESUME,
            "workspace",
            f"The saved worktree {workdir} is gone; restore it with robomate workspace "
            f"{args.agent} on this host before resuming.",
        )
    program = program_for(tools, harness)
    oc_v2 = harness == "opencode" and is_opencode_v2(program, os.environ, workdir)
    flags = model_flags(harness, model, effort, interactive=args.interactive, opencode_v2=oc_v2)
    identity = replace(
        saved_identity(saved, harness, model, effort),
        telemetry_log=bridge_telemetry(place.sessions, args.agent),
    )
    identity.prepare()
    warn_windows_sandbox(args.agent, harness)
    granted = grants(harness, workdir, place.token_file, identity.uv_cache)
    granted += identity.bridge_flags()
    granted += hub_flags(harness, place)
    rows = saved_rows(log)
    conversation = (args.resume_session or "").strip() or None
    if conversation is None:
        ids = {
            row["conversation_id"] for row in rows if isinstance(row.get("conversation_id"), str)
        }
        conversation = next(iter(ids)) if len(ids) == 1 else None
    text = continuation(args.agent, role, joined=saved_join(rows, conversation))
    # The operator's record of what recovery captured; the agent is never pointed at it.
    record = place.sessions / f"{args.agent}-resume.md"
    record.write_text(
        f"# Recovery record for {args.agent}\n\nSent to the agent: {text}\n",
        encoding="utf-8",
    )
    recovery = {
        "recover": True,
        "resume_prompt": record,
        "resume_session": args.resume_session,
        "hub_id": args.hub_id,
        "workflow_id": args.workflow_id,
        "stopped_at": args.stopped_at,
        "confirm_stopped": args.confirm_stopped,
        "force": args.force,
    }
    reader = make_reader(tools, place, args.agent, role)
    with agent_lock(log):
        supplied_conversation(log, saved, args.resume_session)
    if args.interactive:
        namespace = argparse.Namespace(harness=harness, sessions=log, **recovery)
        with agent_lock(log):
            conversation, _ = prepare(
                namespace, reader, args.agent, orchestrator=role == "orchestrator"
            )
            argv = interactive_resume(
                harness,
                program,
                flags,
                granted,
                permissions(harness, args.safe_mode),
                extra,
                conversation,
                opencode_v2=oc_v2,
            )
            env = identity.apply(
                common.launch_environment(
                    workdir,
                    child_environment(
                        place,
                        opencode_environment(effort, safe=args.safe_mode, model=model, v2=oc_v2)
                        if harness == "opencode"
                        else {},
                    ),
                )
            )
            if not (workdir / ".venv").is_dir():
                setup_workspace(identity.workspace_setup, workdir, env)
            announce(harness, args.safe_mode, extra)
            print(f"Send this once the conversation is open: {text}", flush=True)
            row = {
                "agent": args.agent,
                "harness": harness,
                "role": role,
                "model": model,
                "effort": effort,
                "workdir": str(workdir),
                "conversation_id": conversation,
                **identity.row(),
                **(preflight.row() if preflight is not None else {}),
                **best_effort_hub(place, tools, args.agent, role),
            }
            return run_interactive(
                argv, log=log, row=row, event="manual_resume", cwd=workdir, env=env
            )
    base = headless_base(
        harness,
        program,
        flags,
        granted,
        extra,
        title=f"{args.agent} {place.url or 'robomate'}",
        export=place.sessions / f"{args.agent}-devin-export.json",
        opencode_v2=oc_v2,
    )
    namespace = supervision_args(
        args,
        place,
        agent=args.agent,
        role=role,
        harness=harness,
        model=model,
        effort=effort,
        command=base,
        sessions=log,
        cwd=workdir,
        env=identity.apply(common.launch_environment(workdir, child_environment(place))),
        identity=identity,
        allow_unsandboxed=args.allow_unsandboxed,
        sandbox_check=preflight,
        **recovery,
    )
    kind = JoinedOrchestrator if role == "orchestrator" else JoinedWorker
    return supervise(kind(namespace, reader), log)


def ask(args: argparse.Namespace, extra: list[str], tools: Tools) -> int:
    """Open a saved conversation with its hub tools off; no hub, no run file changes."""

    sessions = Path(args.sessions_dir) if args.sessions_dir else locate(args).sessions
    log = log_path(sessions, args.agent)
    saved = saved_settings(log, args.agent)
    harness = str(saved["harness"])
    if harness not in ASK_HARNESSES:
        raise Usage(
            f"{harness} has no tested flag or variable that switches its robomate MCP server "
            "off for one launch, so asking would leave the hub tools live. Open the recorded "
            "conversation by hand with the robomate server absent from a separate profile, or "
            "stop the agent and use `resume` for hub work; see "
            "docs/development/agent-recovery.md#asking-an-agent"
        )
    try:
        # One harness per log: start sets an earlier harness's log aside.
        selected = saved_conversation(log, args.agent, harness, args.conversation_id)
    except ValueError as exc:
        raise Usage(str(exc)) from None
    workdir = Path(saved["workdir"])
    model, effort = str(saved.get("model") or ""), str(saved.get("effort") or "")
    start_dir = workdir if workdir.is_dir() else Path.cwd()
    program = program_for(tools, harness)
    oc_v2 = harness == "opencode" and is_opencode_v2(program, os.environ, start_dir)
    if harness == "opencode" and oc_v2 and args.fork:
        raise Usage(
            "OpenCode 2.x interactive CLI does not support --fork (only `opencode run --fork` "
            "does); omit --fork or fork non-interactively first"
        )
    argv = interactive_resume(
        harness,
        program,
        model_flags(harness, model, effort, interactive=True, opencode_v2=oc_v2),
        [],
        permissions(harness, args.safe_mode),
        extra,
        selected,
        ask=True,
        fork=args.fork,
        opencode_v2=oc_v2,
    )
    env = common.launch_environment(
        start_dir,
        cleared_environment()
        | (
            opencode_environment(effort, safe=args.safe_mode, no_hub=True, model=model, v2=oc_v2)
            if harness == "opencode"
            else {}
        ),
    )
    print(
        f"Ask {args.agent} ({harness}): conversation {selected}; working directory {workdir}; "
        f"hub tools are unavailable; permissions: "
        f"{'safe-mode' if args.safe_mode else 'run permissions'}; harness arguments: {extra!a}.",
        flush=True,
    )
    try:
        return subprocess.call(argv, cwd=start_dir, env=env)
    except KeyboardInterrupt:
        return common.EXIT_INTERRUPTED


def doctor_codex_mcp_flags(argv: list[str], *, environment: dict[str, str], cwd: str) -> list[str]:
    """Disable discovered servers and verify the effective config before startup.

    mcp_servers={} merges rather than clears on codex-cli 0.167.0 (#496).
    Probe in the same directory/environment with the same CLI settings as startup.
    Never print the returned config: transports may contain credentials.
    """
    remedy = "cannot verify Codex has no enabled MCP servers; retry with --clean-login"

    def servers(flags: list[str]) -> list[dict[str, Any]]:
        try:
            response = subprocess.run(
                [*argv, *flags, "mcp", "list", "--json"],
                cwd=cwd,
                env=environment,
                capture_output=True,
                text=True,
                check=True,
                timeout=30,
            )
            entries = json.loads(response.stdout)
            if not isinstance(entries, list) or any(
                not isinstance(entry, dict)
                or not isinstance(entry.get("name"), str)
                or not entry["name"]
                or not isinstance(entry.get("enabled"), bool)
                for entry in entries
            ):
                raise ValueError("invalid MCP listing")
            return entries
        except (OSError, subprocess.SubprocessError, ValueError):
            raise Usage(
                "Codex MCP configuration probe failed; check the Codex configuration and version; "
                + remedy
            ) from None

    flags = []
    table = []
    special = False
    for entry in servers([]):
        name = entry["name"]
        table.append(json.dumps(name, ensure_ascii=False) + "={enabled=false}")
        if re.fullmatch(r"[A-Za-z0-9_-]+", name):
            override = f"mcp_servers.{name}.enabled=false"
        else:
            # Codex splits dotted CLI keys literally, even inside quotes. A
            # quoted inline-table key preserves dots and other special characters.
            special = True
            continue
        flags += ["-c", override]
    if special:
        # Keep all names in this override: a whole-table assignment supersedes
        # earlier dotted overrides in the CLI layer before it merges user config.
        flags = ["-c", "mcp_servers={" + ",".join(table) + "}"]
    if any(entry["enabled"] for entry in servers(flags)):
        raise Usage(remedy)
    return flags


def doctor_opencode_environment(
    environment: dict[str, str],
    profile: Path,
    write_roots: list[str],
    *,
    model: str = "",
    effort: str = "",
    v2: bool = False,
) -> dict[str, str]:
    environment = {
        key: value for key, value in environment.items() if not key.startswith("OPENCODE_")
    }
    # Config and data are distinct in OpenCode. Isolate both to keep user,
    # project and account-level config away from this consultation.
    for name in ("CONFIG", "DATA", "STATE", "CACHE"):
        environment[f"XDG_{name}_HOME"] = str(profile / name.lower())
    environment["OPENCODE_DISABLE_PROJECT_CONFIG"] = "1"
    environment["OPENCODE_DISABLE_CLAUDE_CODE"] = "1"
    if v2:
        rules: list[dict[str, str]] = [{"action": "*", "resource": "*", "effect": "ask"}]
        for root in write_roots:
            pattern = Path(root).as_posix().rstrip("/")
            if "*" in pattern or "?" in pattern:
                raise Usage("OpenCode consultation directory grants cannot contain wildcards")
            for tool in ("read", "glob", "grep", "list"):
                rules.append({"action": tool, "resource": pattern, "effect": "allow"})
                rules.append({"action": tool, "resource": f"{pattern}/*", "effect": "allow"})
        content: dict[str, Any] = {
            "mcp": {"servers": {}},
            "agents": {"build": {"permissions": rules}},
        }
        if model:
            content["model"] = f"{model}#{effort}" if effort and "#" not in model else model
        environment["OPENCODE_CONFIG_CONTENT"] = json.dumps(content)
        return environment
    external = {"*": "ask"}
    for root in write_roots:
        pattern = Path(root).as_posix().rstrip("/")
        if "*" in pattern or "?" in pattern:
            raise Usage("OpenCode consultation directory grants cannot contain wildcards")
        external[pattern] = external[f"{pattern}/*"] = "allow"
    environment["OPENCODE_CONFIG_CONTENT"] = json.dumps(
        {"mcp": {}, "permission": {"*": "ask", "external_directory": external}}
    )
    return environment


def verify_doctor_opencode_mcp(
    program: list[str], environment: dict[str, str], workdir: Path, *, v2: bool = False
) -> None:
    from opencode_headless import OpenCodeConfigError, read_config_with_version

    try:
        config, schema = read_config_with_version(program, environment, workdir, standalone=v2)
    except OpenCodeConfigError:
        raise Usage(
            "could not verify OpenCode's MCP config; check `opencode debug config` support"
        ) from None
    mcp = config.get("mcp", {})
    if not isinstance(mcp, dict):
        raise Usage(
            "OpenCode's effective config has enabled or unknown MCP entries; check managed "
            "or doctor profile config, or choose another doctor harness"
        )
    if schema >= 2 or "servers" in mcp:
        servers = mcp.get("servers", {})
        bad = not isinstance(servers, dict) or any(
            not isinstance(server, dict) or server.get("disabled") is not True
            for server in servers.values()
        )
    else:
        bad = any(
            not isinstance(server, dict) or server.get("enabled") is not False
            for server in mcp.values()
        )
    if bad:
        raise Usage(
            "OpenCode's effective config has enabled or unknown MCP entries; check managed "
            "or doctor profile config, or choose another doctor harness"
        )


def write_doctor_json(path: Path, contents: dict[str, Any]) -> None:
    """Replace a generated doctor profile file without exposing partial JSON."""
    path.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        mode="w", encoding="utf-8", dir=path.parent, delete=False
    ) as f:
        temporary = Path(f.name)
        json.dump(contents, f)
    try:
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def doctor_remote_workers(values: list[str]) -> dict[str, str]:
    workers = {}
    for value in values:
        name, _, host = value.partition("=")
        try:
            validate_agent_name(name)
        except ValueError as exc:
            raise Usage(f"--remote-worker {exc}") from None
        if not host.strip() or name in workers:
            raise Usage("--remote-worker expects AGENT=HOST, once per remote worker")
        workers[name] = host.strip()
    return workers


def same_path(left: str | Path, right: str | Path) -> bool:
    return os.path.normcase(os.path.realpath(left)) == os.path.normcase(os.path.realpath(right))


def infer_run_directory(cwd: Path) -> tuple[Any, str, Path] | None:
    """Resolve ``(LocalHub, run_id, run_dir)`` when ``cwd`` is inside ``<state>/runs/<id>/``."""

    from agent_hub_common.discovery import (
        DiscoveryError,
        LocalHub,
        hub_checkout,
        is_legacy_state_dir,
        read_hub_json,
    )

    resolved = cwd.resolve()
    for candidate in (resolved, *resolved.parents):
        if candidate.parent.name != "runs" or not _RUN_ID.fullmatch(candidate.name):
            continue
        state_dir = candidate.parent.parent
        if not (state_dir / "hub.json").is_file():
            continue
        try:
            info = read_hub_json(state_dir)
        except DiscoveryError as exc:
            raise Usage(f"{exc}. Restore {state_dir / 'hub.json'} first.") from None
        if info is None:
            continue
        checkout = hub_checkout(info)
        if checkout is None and is_legacy_state_dir(state_dir):
            checkout = state_dir.parent
        if checkout is None:
            raise Usage(
                f"{state_dir / 'hub.json'} names no checkout; "
                "select a local hub with --hub <checkout>"
            )
        token = (
            state_dir / "token"
            if is_legacy_state_dir(state_dir)
            else state_dir.parent / "token" / "token"
        )
        return LocalHub(checkout, info, state_dir, token), candidate.name, candidate
    return None


def resolve_doctor_hub_and_run(
    args: argparse.Namespace, environment: Mapping[str, str]
) -> tuple[Any, str, dict[str, str], dict[str, str]]:
    """Resolve hub and run ID for ``doctor``, printing any inferred or defaulted choice."""

    from agent_hub_common.discovery import DiscoveryError, LocalHub, find_hub

    cwd = Path.cwd().resolve()
    inferred = infer_run_directory(cwd)
    if inferred is not None:
        inferred_hub, inferred_run, inferred_run_dir = inferred
        if args.hub:
            try:
                hub = find_hub(cwd, environment, selector=args.hub, list_hubs=False)
            except DiscoveryError as exc:
                raise Usage(
                    f"{exc}. Select a local hub with --hub <checkout>; "
                    "restore its local state if missing."
                ) from None
            assert isinstance(hub, LocalHub)
            id_mismatch = bool(
                hub.hub_id and inferred_hub.hub_id and hub.hub_id != inferred_hub.hub_id
            )
            if not same_path(hub.state_dir, inferred_hub.state_dir) or id_mismatch:
                raise Usage(
                    f"--hub {args.hub} selects {hub.label()} ({hub.state_dir}), which conflicts "
                    f"with the current run directory's {inferred_hub.label()} "
                    f"({inferred_hub.state_dir}); omit --hub or run from outside that run directory"
                )
            hub_selection = {"source": "explicit", "reason": f"--hub {args.hub}"}
        else:
            hub = inferred_hub
            reason = f"from run directory {cwd}"
            hub_selection = {"source": "cwd_run_dir", "reason": reason}
            print(f"doctor: using {hub.label()} ({reason})", flush=True)
        if args.run:
            if args.run != inferred_run:
                raise Usage(
                    f"--run {args.run} conflicts with the current run directory's run "
                    f"{inferred_run} ({inferred_run_dir}); pass --run {inferred_run} or run "
                    "from outside that run directory"
                )
            run_id = args.run
            run_selection = {"source": "explicit", "reason": f"--run {args.run}"}
        else:
            run_id = inferred_run
            reason = f"from current directory {cwd}"
            run_selection = {"source": "cwd_run_dir", "reason": reason}
            print(f"doctor: using run {run_id} ({reason})", flush=True)
    else:
        try:
            hub = find_hub(cwd, environment, selector=args.hub, list_hubs=False)
        except DiscoveryError as exc:
            raise Usage(
                f"{exc}. Select a local hub with --hub <checkout>; "
                "restore its local state if missing."
            ) from None
        assert isinstance(hub, LocalHub)
        hub_selection = (
            {"source": "explicit", "reason": f"--hub {args.hub}"}
            if args.hub
            else {"source": "checkout", "reason": f"from checkout {hub.checkout}"}
        )
        run_id = args.run or ""
        run_selection = {"source": "explicit", "reason": f"--run {run_id}"} if run_id else {}

    runs = hub.state_dir / "runs"
    available = (
        sorted(p.name for p in runs.iterdir() if (p / "manifest.json").is_file())
        if runs.is_dir()
        else []
    )
    if run_id:
        if not _RUN_ID.fullmatch(run_id) or run_id not in available:
            raise Usage(
                f"run {run_id!r} has no manifest in {runs}. Select one of this hub's runs with "
                f"--run: {', '.join(available) or '(none)'}. The doctor never creates a run."
            )
        return hub, run_id, hub_selection, run_selection

    if len(available) == 1:
        run_id = available[0]
        reason = f"the only run under {hub.label()}"
        print(f"doctor: using run {run_id} ({reason})", flush=True)
        return hub, run_id, hub_selection, {"source": "only_run", "reason": reason}

    if len(available) > 1:
        not_done: list[tuple[str, str]] = []
        for candidate in available:
            try:
                data = json.loads((runs / candidate / "manifest.json").read_text(encoding="utf-8"))
            except (OSError, ValueError):
                not_done.append((candidate, "unreadable"))
                continue
            status = data.get("status") if isinstance(data, dict) else None
            if status != "done":
                not_done.append((candidate, str(status or "unknown")))
        if len(not_done) == 1:
            run_id, status_label = not_done[0]
            reason = f"the only unfinished run under {hub.label()} (status {status_label})"
            print(f"doctor: using run {run_id} ({reason})", flush=True)
            return hub, run_id, hub_selection, {"source": "only_unfinished", "reason": reason}

    raise Usage(
        f"no --run given. Select one of this hub's runs with --run: "
        f"{', '.join(available) or '(none)'}. The doctor never creates a run."
    )


def doctor(args: argparse.Namespace, extra: list[str], tools: Tools) -> int:
    """Denise's optional entry: local reads only, no join, no MCP, no supervision."""

    if extra:
        raise Usage("doctor does not accept --harness-args; use --model, --effort or --clean-login")
    environment = cleared_environment()
    hub, run_id, hub_selection, run_selection = resolve_doctor_hub_and_run(args, environment)
    run_dir = hub.state_dir / "runs" / run_id
    try:
        manifest = json.loads((run_dir / "manifest.json").read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise Usage(
            f"{run_dir / 'manifest.json'} is unreadable ({exc}); restore it first"
        ) from None
    if not isinstance(manifest, dict) or manifest.get("run_id") != run_id:
        raise Usage(f"{run_dir / 'manifest.json'} does not describe run {run_id}; restore it first")
    manifest_hub = manifest.get("hub")
    saved_id = manifest_hub.get("hub_id") if isinstance(manifest_hub, dict) else None
    if saved_id and hub.hub_id and saved_id != hub.hub_id:
        raise Usage(
            f"run {run_id} records hub {saved_id}, but the selected hub is {hub.hub_id}; "
            "select the owning checkout with --hub or restore the matching manifest"
        )
    try:
        persisted = denise.load_joined_settings(run_dir)
    except ValueError as exc:
        if not args.harness:
            raise Usage(
                f"{exc}. Fix or remove it, or pass --harness claude|codex|opencode|antigravity."
            ) from None
        say(f"warning: ignoring unreadable saved Denise settings ({exc}); using explicit flags")
        persisted = None
    try:
        settings, used_persisted = denise.resolve_doctor_settings(
            persisted,
            harness=args.harness,
            model=args.model,
            effort=args.effort,
            clean_login=args.clean_login,
        )
    except ValueError as exc:
        raise Usage(str(exc)) from None
    if used_persisted:
        source = persisted.get("settings_from") if persisted else ""
        origin = f" (from {source})" if source and source != "none" else ""
        print(
            f"doctor: using saved Denise settings{origin}: {', '.join(used_persisted)}",
            flush=True,
        )
    if note := settings.get("effort_note"):
        print(f"doctor: {note}", flush=True)
    harness = settings["harness"]
    effective_clean_login = bool(settings["clean_login"])
    profile_name = "claude" if harness == "claude-code" else harness
    target = Path(args.target).expanduser().resolve() if args.target else hub.checkout.resolve()
    target_common = denise.git_common_dir(target) if target.is_dir() else None
    if args.target and not target_common:
        raise Usage(f"--target {target} is not a Git checkout; pass the actual checkout root")
    program = program_for(tools, harness)
    if not shutil.which(program[0], path=environment.get("PATH")):
        raise Usage(f"{program[0]} was not found; install {harness} or put it on PATH")
    context = denise.build_joined_context(
        manifest,
        run_dir=run_dir,
        state_dir=hub.state_dir,
        sessions_dir=Path(args.sessions_dir) if args.sessions_dir else hub.state_dir / "sessions",
        hub_checkout=hub.checkout,
        target=target,
        target_common=target_common,
        tooling_root=ROOT,
        settings=settings,
        remote_workers=doctor_remote_workers(args.remote_worker),
        run_selection=run_selection,
        hub_selection=hub_selection,
    )
    doctor_dir = context["doctor"]
    environment = common.launch_environment(
        doctor_dir["workdir"],
        {
            key: value
            for key, value in environment.items()
            if key not in ("AGENT_NAME", "AGENT_RUNTIME") and not key.startswith("HUB_")
        },
    )
    environment |= {key: doctor_dir["tmp"] for key in ("TMPDIR", "TEMP", "TMP")}
    write_roots = [
        root["path"] for root in context["access_inventory"]["roots"] if root["mode"] == "write"
    ]
    login = ""
    oc_v2 = harness == "opencode" and is_opencode_v2(program, environment)
    if effective_clean_login or harness in denise.ISOLATED_HARNESSES:
        # Separate homes keep authentication and MCP configuration scoped to this run.
        profile = Path(doctor_dir["configs"]) / profile_name
        doctor_dir["profile"] = str(profile)
        profile.mkdir(parents=True, mode=0o700, exist_ok=True)
        if harness == "codex":
            (profile / "config.toml").write_text(
                "# Denise's clean doctor profile: no MCP servers or approval overrides.\n",
                encoding="utf-8",
            )
            environment["CODEX_HOME"] = str(profile)
            if not (profile / "auth.json").is_file():
                login = f"Log in to Denise's clean profile: CODEX_HOME={profile} codex login"
        elif harness == "claude-code":
            environment["CLAUDE_CONFIG_DIR"] = str(profile)
            login = f"Denise's clean Claude Code profile is {profile}: use /login to log in."
        elif harness == "opencode":
            environment = doctor_opencode_environment(
                environment,
                profile,
                write_roots,
                model=settings["model"],
                effort=settings["effort"],
                v2=oc_v2,
            )
            login = (
                f"Denise's OpenCode profile is {profile}. Use /connect if a provider needs login."
            )
            context["access_inventory"]["enforcement"] = (
                "OpenCode 2.x ordered tool permissions with --standalone profile isolation; "
                "shell commands require operator review and retain OS access"
                if oc_v2
                else "OpenCode external directory tool grants; shell commands require operator "
                "review and retain OS access"
            )
        else:
            write_doctor_json(agy_mcp_path(profile), {"mcpServers": {}})
            write_doctor_json(
                profile / ".gemini" / "antigravity-cli" / "settings.json",
                {
                    "agentMode": "",
                    "toolPermission": "request-review",
                    "allowNonWorkspaceAccess": False,
                    "enableTerminalSandbox": True,
                    "permissions": {"deny": ["mcp(*)"]},
                },
            )
            environment.update({key: str(profile) for key in ("HOME", "USERPROFILE", "AGY_HOME")})
            login = f"Denise's AntiGravity home is {profile}. Follow the agy sign-in prompt."
    report = denise.render_joined(context)
    prompt = f"Read {doctor_dir['startup']} and follow the instructions in it"
    flags = model_flags(
        harness, settings["model"], settings["effort"], interactive=True, opencode_v2=oc_v2
    )
    directory_flags = [word for root in write_roots for word in ("--add-dir", root)]
    if harness == "codex":
        # No MCP server at all, and write grants only on the selected run's roots.
        argv = [*program, "-C", doctor_dir["workdir"], *flags]
        argv += ["-c", "sandbox_mode=workspace-write", "-c", "approval_policy=on-request"]
        argv += directory_flags
        argv += doctor_codex_mcp_flags(argv, environment=environment, cwd=doctor_dir["workdir"])
        argv += [prompt]
    elif harness == "claude-code":
        argv = [
            *program,
            *flags,
            "--strict-mcp-config",
            "--permission-mode",
            "default",
            *directory_flags,
        ]
        # Claude Code's variadic --add-dir consumes a trailing bare prompt as
        # another directory (#513); -- keeps the startup prompt positional.
        argv += ["--", prompt]
    elif harness == "opencode":
        verify_doctor_opencode_mcp(program, environment, Path(doctor_dir["workdir"]), v2=oc_v2)
        standalone = ["--standalone"] if oc_v2 else []
        argv = [*program, *standalone, *flags, "--prompt", prompt]
    else:
        argv = [*program, *flags, *directory_flags, "--prompt-interactive", prompt]
    print(
        f"Denise ({harness}) for run {run_id}: no hub tools; context {doctor_dir['context']}; "
        f"report {report}; write roots: {', '.join(write_roots) or '(none)'}.",
        flush=True,
    )
    if login:
        print(login, flush=True)
    try:
        return subprocess.call(argv, cwd=doctor_dir["workdir"], env=environment)
    except OSError as exc:
        raise Usage(f"could not start {harness}: {exc}. Check the harness installation.") from None


# -- arguments ------------------------------------------------------------------------


def hub_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--hub", metavar="NAME|HUB_ID|CHECKOUT", help="the hub to use")
    parser.add_argument("--hub-url", help="a remote host's hub URL (with --token-file)")
    parser.add_argument("--token-file", help="a remote host's hub bearer token file")
    parser.add_argument(
        "--sessions-dir", help="where <agent>.jsonl lives (default: the hub's state sessions/)"
    )


def backoff_options(parser: argparse.ArgumentParser, *, worker_reads: bool = True) -> None:
    parser.add_argument("--max-resumes", type=int, default=common.DEFAULT_MAX_RESUMES)
    parser.add_argument("--resume-delay-s", type=float, default=common.DEFAULT_RESUME_DELAY_S)
    parser.add_argument(
        "--resume-max-delay-s", type=float, default=common.DEFAULT_RESUME_MAX_DELAY_S
    )
    parser.add_argument("--resume-total-s", type=float, default=common.DEFAULT_RESUME_TOTAL_S)
    parser.add_argument(
        "--resume-series-reset-s", type=float, default=common.DEFAULT_RESUME_SERIES_RESET_S
    )
    parser.add_argument("--warn-series-resets", type=int, default=common.DEFAULT_WARN_SERIES_RESETS)
    parser.add_argument("--max-series-resets", type=int, default=common.DEFAULT_MAX_SERIES_RESETS)
    parser.add_argument("--read-retries", type=int)
    parser.add_argument(
        "--read-retry-delay-s", type=float, default=common.DEFAULT_READ_RETRY_DELAY_S
    )
    parser.add_argument("--predecessor-wait-s", type=float, default=300.0)
    parser.add_argument("--predecessor-poll-s", type=float, default=2.0)


def interactive_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--interactive", action="store_true", help="no automatic resume; still recorded (#222)"
    )
    parser.add_argument(
        "--safe-mode", action="store_true", help="interactive: prompting defaults (#335)"
    )


def parser() -> argparse.ArgumentParser:
    top = argparse.ArgumentParser(
        prog="agent-launcher.py",
        description="Start, resume or ask a joined agent; Denise's doctor entry.",
        epilog="Supervision is per workflow: done and release stops name a fresh start for the "
        "next run, and a queued or new run needs new launchers. Except for doctor, words after "
        "--harness-args go to the harness verbatim.",
        allow_abbrev=False,
    )
    commands = top.add_subparsers(dest="action", required=True)
    begin = commands.add_parser("start", help="start an agent in its worktree with its join line")
    begin.add_argument("agent")
    begin.add_argument("--role", required=True, choices=ROLES)
    begin.add_argument("--harness", required=True, choices=common.HARNESSES)
    begin.add_argument("--model", default="")
    begin.add_argument("--effort", default="")
    begin.add_argument("--workdir", help="a remote host's clone from robomate workspace --clone")
    begin.add_argument(
        "--harness-version",
        default="",
        help="the harness CLI's own --version, declared to the hub as HUB_HARNESS_VERSION",
    )
    begin.add_argument(
        "--uv-cache-dir", help="the agent's own isolated UV_CACHE_DIR, created owner-only (#285)"
    )
    begin.add_argument(
        "--workspace-setup",
        default=common.WORKSPACE_SETUP_AUTO,
        help="shell command to prepare the worktree before launch; 'auto' (the default, "
        "as is '') syncs a uv project, 'none' disables setup",
    )
    begin.add_argument("--lane-tmp-dir", help="the agent's explicitly assigned temporary root")
    begin.add_argument(
        "--pytest-workers",
        type=int,
        help="the agent's PYTEST_XDIST_AUTO_NUM_WORKERS; 0 clears an inherited one",
    )
    begin.add_argument(
        "--allow-unsandboxed",
        action="store_true",
        help="Linux only: continue if sandbox preflight fails for this agent only "
        "(runs without proper sandboxing and may not work at all; not inherited by resume)",
    )
    hub_options(begin)
    interactive_options(begin)
    backoff_options(begin)
    again = commands.add_parser("resume", help="resume the saved conversation, with guards")
    again.add_argument("agent")
    again.add_argument("--resume-session", help="the exact saved conversation ID")
    again.add_argument("--hub-id", help="expected hub ID when saved metadata is missing")
    again.add_argument("--workflow-id", help="expected workflow ID when saved metadata is missing")
    again.add_argument("--stopped-at", help="known old process stop time, ISO 8601 with timezone")
    again.add_argument("--confirm-stopped", action="store_true")
    again.add_argument("--force", action="store_true", help="override advisories only")
    again.add_argument(
        "--allow-unsandboxed",
        action="store_true",
        help="Linux only: continue if sandbox preflight fails for this agent only "
        "(runs without proper sandboxing and may not work at all; not inherited across resumes)",
    )
    hub_options(again)
    interactive_options(again)
    backoff_options(again)
    halt = commands.add_parser(
        "stop", help="stop the agent's attached launcher or its recorded harness"
    )
    halt.add_argument("agent")
    hub_options(halt)
    question = commands.add_parser("ask", help="ask a saved conversation questions; no hub")
    question.add_argument("agent")
    question.add_argument("conversation_id", nargs="?")
    question.add_argument("--fork", action="store_true", help="ask in a new conversation")
    question.add_argument("--safe-mode", action="store_true")
    hub_options(question)
    check = commands.add_parser("doctor", help="consult Denise about a run; no join, no hub")
    check.add_argument(
        "--hub",
        metavar="NAME|HUB_ID|CHECKOUT",
        help="the hub to read (default: current run directory or git checkout)",
    )
    check.add_argument(
        "--run",
        help="the run ID under the hub's runs/ "
        "(default: current run directory, only run, or only unfinished run)",
    )
    check.add_argument(
        "--harness",
        default="",
        choices=("claude", *denise.DOCTOR_HARNESSES, "agy"),
        help="doctor harness override (default: run's saved Denise settings)",
    )
    check.add_argument(
        "--clean-login",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="separate Claude Code or Codex login; OpenCode and AntiGravity always use one "
        "(default: run's saved Denise settings, else false)",
    )
    check.add_argument("--model", default="")
    check.add_argument(
        "--effort", default="", help="reasoning effort override. Omit this option for OpenCode."
    )
    check.add_argument(
        "--remote-worker",
        metavar="AGENT=HOST",
        action="append",
        default=[],
        help="attribute a worker's evidence to its host (repeatable)",
    )
    check.add_argument("--target", help="the target checkout (default: the hub's checkout)")
    check.add_argument("--sessions-dir", help=argparse.SUPPRESS)
    return top


def parse(argv: list[str]) -> tuple[argparse.Namespace, list[str]]:
    words, extra = split_arguments(argv)
    args = parser().parse_args(words)
    if args.action != "doctor":
        try:
            validate_agent_name(args.agent)
        except ValueError as exc:
            raise Usage(str(exc)) from None
    for key in ("max_resumes", "read_retries"):
        value = getattr(args, key, None)
        if value is not None and value < (0 if key == "max_resumes" else 1):
            raise Usage(f"--{key.replace('_', '-')} is out of range")
    if args.action in ("start", "resume"):
        role = getattr(args, "role", None)
        if args.read_retries is None:
            args.read_retries = (
                common.DEFAULT_READ_RETRIES
                if role == "orchestrator"
                else common.DEFAULT_WORKER_READ_RETRIES
            )
        for key in (
            "resume_delay_s",
            "resume_max_delay_s",
            "resume_total_s",
            "resume_series_reset_s",
            "warn_series_resets",
            "max_series_resets",
            "read_retry_delay_s",
            "predecessor_wait_s",
        ):
            if not getattr(args, key) >= 0:
                raise Usage(f"--{key.replace('_', '-')} must not be negative")
        if (
            args.max_series_resets > 0
            and args.warn_series_resets
            and args.warn_series_resets >= args.max_series_resets
        ):
            raise Usage(
                "--warn-series-resets must be less than --max-series-resets "
                "(use 0 to disable the warning)"
            )
        if not args.predecessor_poll_s > 0:
            raise Usage("--predecessor-poll-s must be positive")
        if args.safe_mode and not args.interactive:
            raise Usage("--safe-mode applies to --interactive launches")
    if args.action == "start":
        if args.pytest_workers is not None and args.pytest_workers < 0:
            raise Usage("--pytest-workers must not be negative (0 means CPU count)")
        args.harness_version = args.harness_version.strip()
    return args, extra


def main(
    argv: list[str] | None = None,
    *,
    programs: Programs | None = None,
    reader: common.HubReader | None = None,
    workspace: Callable[[str, str | None], Path] | None = None,
) -> int:
    common.safe_output_streams()
    tools = Tools(programs or {}, reader, workspace or robomate_workspace)
    try:
        args, extra = parse(sys.argv[1:] if argv is None else argv)
        action = {
            "start": start,
            "resume": resume,
            "stop": stop_agent,
            "ask": ask,
            "doctor": doctor,
        }[args.action]
        return action(args, extra, tools)
    except Usage as exc:
        say(str(exc))
        return 2
    except common.Stop as stop:
        say(str(stop))
        return stop.code


if __name__ == "__main__":
    sys.exit(main())
