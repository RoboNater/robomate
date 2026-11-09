#!/usr/bin/env python3
"""Start, resume or ask a joined agent, without generated configs (M2 Step 10, #247).

The successor of ``alice-launcher.py``, ``worker-launcher.py``,
``agent-recovery.py`` and ``agent-ask.py``, which stay for prepared runs until
Step 11. It is optional and outside the core: the core experience is still the
operator starting a harness in a worktree and typing one line (spec §2, §9.4).
Run it from the hub's checkout (or name the hub with ``--hub``)::

    uv run --project <robomate checkout> python scripts/agent-launcher.py start bob \\
        --role implementer --harness claude-code [--model M] [--effort E] [--interactive]
    uv run --project <robomate checkout> python scripts/agent-launcher.py resume bob
    uv run --project <robomate checkout> python scripts/agent-launcher.py ask bob
    uv run --project <robomate checkout> python scripts/agent-launcher.py doctor \\
        --run <run_id> --harness codex

``start`` finds the agent's worktree with ``robomate workspace <agent>``, starts
the harness there with the join line (``Join robomate as bob, implementer.``)
as its only prompt, passes the harness's sandbox grants as flags, and resumes
the same conversation with a fixed continuation and #158 backoff until the
workflow is done or the worker is released. Nothing is rendered: the harness
finds the hub through its static ``robomate mcp`` entry and the worktree's
binding. Each launch, exit, wait and stop is a JSON line, with the hub's
``run_id``, in ``sessions/<agent>.jsonl`` in the hub's state directory: the
operator's data, which agents never read. A remote host passes ``--hub-url``,
``--token-file``, ``--sessions-dir`` and the ``--workdir`` that ``robomate
workspace <agent> --clone`` printed there.

``resume`` takes the harness, role and worktree from that log, applies the
guards of ``agent-recovery`` (predecessor stopped and detached, same hub,
workflow taken from the hub with #223's evidence when the log predates it),
and resumes the saved conversation with a one-line text telling the agent to
``join`` with ``takeover=true``. ``ask`` opens a saved conversation
interactively with its hub tools disabled; it needs no hub. ``--interactive``
(#222) starts or resumes without automatic resume and still records the
session. ``doctor`` is Denise's optional entry, separate from all of this: it
reads the selected run locally, works with the hub stopped, and never joins.

Exit codes as the prepared launchers: 0 done or released; 1 resumes spent;
2 usage; 3 hub unreadable; 4 nothing safe to resume; 5 paused; 130 interrupted.
An interactive or ask session returns the harness's own exit code.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import urllib.error
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

SCRIPTS = Path(__file__).resolve().parent
ROOT = SCRIPTS.parent
sys.path.insert(0, str(SCRIPTS))
import agent_launcher as common  # noqa: E402
from agent_interactive import split_arguments  # noqa: E402
from agent_recovery import agent_lock, prepare  # noqa: E402
from agent_sessions import conversation as saved_conversation  # noqa: E402
from agent_sessions import exact_id  # noqa: E402
from agent_worker import WorkerLauncher, WorkerReader  # noqa: E402

ROLES = ("orchestrator", "implementer", "reviewer", "worker")
LAUNCH_EVENTS = ("start", "resume", "manual_resume")
PROGRAMS: dict[str, list[str]] = {
    "claude-code": ["claude"],
    "codex": ["codex"],
    "opencode": ["opencode"],
    "antigravity": ["agy"],
    "devin": ["devin"],
}
#: The run's permission flags (#335), as the prepared runs use them.
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
DOCTOR_HARNESSES = ("claude-code", "codex")
_RUN_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}")

Programs = Mapping[str, list[str]]


class Usage(Exception):
    """A refusal before anything was launched; exit 2."""


def say(message: str) -> None:
    print(f"agent-launcher: {message}", file=sys.stderr, flush=True)


def join_line(agent: str, role: str) -> str:
    return f"Join robomate as {agent}, {role}."


def continuation(agent: str, role: str) -> str:
    """The fixed text every automatic or manual resume sends (#146): no state, no decision."""

    head = (
        "Your previous harness process exited before your work was done, and the launcher "
        "has resumed this same conversation. This message is a fixed continuation: it "
        "carries no operator decision, approval, or state. Your hub tools run in a new "
        f'bridge process: first call join(name="{agent}", role="{role}", takeover=true), '
        "which replaces only your previous session and grants no workflow authority. "
    )
    if role == "orchestrator":
        return head + (
            "Then call get_state and reconcile using the returned orchestrator guide's On "
            "resume section. Keep an escalated or paused workflow as it is: escalation ends "
            "only through the operator's user_answered event. Do not start the workflow "
            "again and do not repeat completed work. Do not end your turn until the "
            "workflow is done."
        )
    return head + (
        "Its reattached_task is the task you still hold: finish it and submit its result; "
        "if the task failed or was canceled, drop it and return to await_assignment. Every "
        "reply must contain a tool call until await_assignment returns release: true. Wait "
        "for CI and other work in the foreground. Do not end your turn until "
        "await_assignment returns release: true."
    )


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
        return Place(hub.url.rstrip("/"), hub.token_file, sessions, args.hub)
    if not args.sessions_dir:
        raise Usage("a hub named by ROBOMATE_HUB_URL has no local state: add --sessions-dir")
    return Place(hub.url, hub.token_file, Path(args.sessions_dir), None, explicit=True)


def is_selection(name: str) -> bool:
    """The variables by which a bridge finds a hub explicitly (spec §4)."""
    return name == "ROBOMATE_HUB_URL" or name.startswith("ROBOMATE_TOKEN")


def cleared_environment() -> dict[str, str]:
    """This process's environment without an inherited hub selection."""
    return {key: value for key, value in os.environ.items() if not is_selection(key)}


def child_environment(place: Place, extra: Mapping[str, str] | None = None) -> dict[str, str]:
    """A joining child's environment: the selected hub, never an inherited one (#247 r1-3).

    A local hub's worktree binding names it, so any ambient selection is
    cleared. A remote host's clone has no binding: its bridge gets the URL and
    token file the operator gave, and no ``ROBOMATE_TOKEN``, which would
    otherwise take precedence over the file.
    """
    forwarded: dict[str, str] = {}
    if place.explicit:
        if place.token_file is None:
            raise Usage("the hub's token file is unknown: name it with --token-file")
        forwarded = {"ROBOMATE_HUB_URL": place.url, "ROBOMATE_TOKEN_FILE": str(place.token_file)}
    return cleared_environment() | forwarded | dict(extra or {})


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


def uv_cache() -> str | None:
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


def grants(harness: str, workdir: Path, token_file: Path | None) -> list[str]:
    """The measured Linux sandbox grants (docs/user-guide.md), as flags; no files.

    Claude Code: its sandbox with the forge hosts and a read-deny on the hub's
    token directory. Codex: workspace-write with network and the linked
    worktree's own git roots plus the uv cache, never the whole common
    directory. Other harnesses' grants are measured in M3 (§9.2): none here.
    """

    if harness == "claude-code":
        sandbox: dict[str, Any] = {
            "enabled": True,
            "network": {"allowedDomains": forge_hosts(workdir)},
        }
        if token_file is not None:
            sandbox["filesystem"] = {"denyRead": [str(token_file.parent)]}
        return ["--settings", json.dumps({"sandbox": sandbox}, separators=(",", ":"))]
    if harness != "codex":
        return []
    roots: list[str] = []
    git_dir = git_text(workdir, "rev-parse", "--absolute-git-dir")
    common_dir = git_text(workdir, "rev-parse", "--path-format=absolute", "--git-common-dir")
    if git_dir and common_dir and Path(git_dir) != Path(common_dir):
        common_path = Path(common_dir)
        roots += [git_dir, *(str(common_path / leaf) for leaf in ("objects", "refs", "logs"))]
    cache = uv_cache()
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


def model_flags(harness: str, model: str, effort: str, *, interactive: bool) -> list[str]:
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


def opencode_environment(effort: str, *, safe: bool, no_hub: bool = False) -> dict[str, str]:
    content: dict[str, Any] = {}
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
) -> list[str]:
    """The harness command without prompt or conversation words; the launcher adds them."""

    if harness == "codex":
        return [*program, "exec", "-C", ".", *RUN_PERMISSIONS[harness], *granted, *flags, *extra]
    if harness == "opencode":
        return [*program, "run", *RUN_PERMISSIONS[harness], *flags, "--title", title, *extra]
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
        return [*program, *perms, *flags, *extra, "--prompt", prompt], True
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
        words = [*program, *perms, *flags, *(["--fork"] if fork else [])]
        return [*words, *extra, "--session", conversation]
    if harness == "antigravity":
        return [*program, *flags, *perms, *extra, "--conversation", conversation]
    return [*program, *flags, *perms, *extra, "--resume", conversation]


# -- the session log ------------------------------------------------------------------


def log_path(sessions: Path, agent: str) -> Path:
    return sessions / f"{agent}.jsonl"


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


# -- supervised launchers -------------------------------------------------------------


class Joined(common.Launcher):
    """What a joined agent's supervision changes in the prepared launchers.

    The prompt is the join line, every resume tells the agent to join with
    takeover, rows carry the role, the run ID and the launch settings, and a
    finished earlier run on the hub is never pinned at start.
    """

    role = "worker"
    run_id: str | None = None
    launched = False

    def setup(self, args: argparse.Namespace) -> None:
        self.agent = args.agent
        self.role = args.role
        self.orchestrator = args.role == "orchestrator"
        self.continue_prompt = continuation(args.agent, args.role)

    def resume_name(self) -> str:
        return f"agent-launcher.py resume {self.agent}"

    def start_name(self) -> str:
        return f"agent-launcher.py start {self.agent}"

    def first_launch(self) -> common.Launch:
        text = join_line(self.agent, self.role)
        return common.text_launch(self.harness, self.base, text, self.conversation or "")

    def manual_resume_text(self) -> str:
        return self.continue_prompt

    def log(self, event: str, **fields: object) -> None:
        extra: dict[str, object] = {"role": self.role, "run_id": self.run_id}
        if event in LAUNCH_EVENTS:
            extra |= {
                "model": self.args.model,
                "effort": self.args.effort,
                "workdir": str(self.args.cwd),
                "interactive": False,
            }
        super().log(event, **extra, **fields)

    def run_child(self, launch: common.Launch, action: str, resumes: int) -> int:
        self.launched = True
        return super().run_child(launch, action, resumes)

    def read_hub(self) -> dict[str, Any] | None:
        pinned = self.workflow_id
        workflow = super().read_hub()
        if pinned is None and workflow is not None and workflow["status"] == "done":
            # An earlier run, finished before this start: not this agent's (#223).
            self.workflow_id = None
            return None
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


_UNSEEN = object()


class JoinedWorker(Joined, WorkerLauncher):
    def __init__(self, args: argparse.Namespace, reader: common.HubReader | None = None) -> None:
        super().__init__(args, reader)
        self.setup(args)
        self.stale_release: object = _UNSEEN

    def manual_resume_hint(self) -> str:
        return (
            f"Run {self.resume_name()}: it checks the predecessor and the hub before it "
            "resumes the saved conversation."
        )

    def before_launch(self, resumes: int) -> bool:
        """An automatic resume does not wait for its predecessor's bridge to be lost.

        This launcher's own harness has exited; a bridge it left behind is
        superseded by the successor's join with takeover, which reattaches the
        held task (§5, §7.5). Waiting for that bridge to become lost, as the
        prepared launcher does before a check-in, would fail the task instead.
        The first launch, and a manual resume, keep the prepared checks.
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
    """The namespace the prepared launchers' supervision reads."""

    values = {
        "hub_url": place.url,
        "token_file": place.token_file,
        "telemetry": None,
        "prompt": None,
        "resume_session": None,
        "resume_prompt": None,
        "recover": False,
        "recovery_config": None,
        "hub_id": None,
        "workflow_id": None,
        "stopped_at": None,
        "confirm_stopped": False,
        "force": False,
    }
    for key in (
        "max_resumes",
        "resume_delay_s",
        "resume_max_delay_s",
        "resume_total_s",
        "resume_series_reset_s",
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
    """common.main's supervision, with this launcher's own hints."""

    previous = signal.signal(signal.SIGTERM, launcher.interrupt)
    try:
        with agent_lock(sessions):
            try:
                return launcher.run()
            except KeyboardInterrupt:
                launcher.interrupted = True
                launcher.stop_child(wait=True)
                return stopped(launcher, common.Stop(common.EXIT_INTERRUPTED, "interrupted", ""))
            except common.Stop as stop:
                return stopped(launcher, stop)
    except common.Stop as stop:
        say(str(stop))
        return stop.code
    except (OSError, ValueError, urllib.error.URLError) as exc:
        say(
            f"launch unavailable: {exc}. Restore hub reachability, then run "
            f"{launcher.resume_name()}"
        )
        return common.EXIT_HUB_UNREADABLE
    finally:
        launcher.stop_child(wait=True)
        signal.signal(signal.SIGTERM, previous)


def stopped(launcher: Joined, stop: common.Stop) -> int:
    launcher.log("stop", reason=stop.reason, exit_code=stop.code)
    if stop.code == common.EXIT_INTERRUPTED:
        say("interrupted; stopped the harness this launcher started and launched nothing more")
    else:
        say(str(stop))
        if launcher.args.recover:
            say(f"To only ask {launcher.agent} questions: agent-launcher.py ask {launcher.agent}")
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


def capture_remedy(harness: str, agent: str, workdir: Path) -> str:
    """What the operator does when an interactive harness's conversation ID is not captured."""
    where = {
        "codex": f"run `codex resume` in {workdir} to list its sessions",
        "opencode": f"run `opencode session list` in {workdir}",
    }.get(harness, f"use {harness}'s own session list for {workdir}")
    return (
        f"To resume or ask later, find the exact ID ({where}) and pass it: "
        f"agent-launcher.py resume {agent} --resume-session ID, or agent-launcher.py ask "
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
                + capture_remedy(row["harness"], row["agent"], cwd)
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


def start(args: argparse.Namespace, extra: list[str], tools: Tools) -> int:
    place = locate(args)
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
    flags = model_flags(args.harness, args.model, args.effort, interactive=args.interactive)
    granted = grants(args.harness, workdir, place.token_file)
    with agent_lock(log):
        set_aside(log, args.harness)
    if args.interactive:
        perms = permissions(args.harness, args.safe_mode)
        session_id = str(uuid.uuid4()) if args.harness == "claude-code" else ""
        line = join_line(args.agent, args.role)
        argv, prompted = interactive_start(
            args.harness, program, flags, granted, perms, extra, prompt=line, session_id=session_id
        )
        env = child_environment(
            place,
            opencode_environment(args.effort, safe=args.safe_mode)
            if args.harness == "opencode"
            else {},
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
                + capture_remedy(args.harness, args.agent, workdir),
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
            **best_effort_hub(place, tools, args.agent, args.role),
        }
        with agent_lock(log):
            return run_interactive(argv, log=log, row=row, event="start", cwd=workdir, env=env)
    base = headless_base(
        args.harness,
        program,
        flags,
        granted,
        extra,
        title=f"{args.agent} {place.url or 'robomate'}",
        export=place.sessions / f"{args.agent}-devin-export.json",
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
        env=child_environment(place),
    )
    reader = make_reader(tools, place, args.agent, args.role)
    kind = JoinedOrchestrator if args.role == "orchestrator" else JoinedWorker
    return supervise(kind(namespace, reader), log)


def resume(args: argparse.Namespace, extra: list[str], tools: Tools) -> int:
    place = locate(args)
    log = log_path(place.sessions, args.agent)
    saved = saved_settings(log, args.agent)
    harness, role = str(saved["harness"]), str(saved["role"])
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
    flags = model_flags(harness, model, effort, interactive=args.interactive)
    granted = grants(harness, workdir, place.token_file)
    # The operator's record of what recovery captured; the agent is never pointed at it.
    record = place.sessions / f"{args.agent}-resume.md"
    record.write_text(
        f"# Recovery record for {args.agent}\n\n"
        f"Sent to the agent: {continuation(args.agent, role)}\n",
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
        namespace = argparse.Namespace(
            harness=harness, sessions=log, recovery_config=None, **recovery
        )
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
            )
            env = child_environment(
                place,
                opencode_environment(effort, safe=args.safe_mode) if harness == "opencode" else {},
            )
            announce(harness, args.safe_mode, extra)
            print(
                f"Send this once the conversation is open: {continuation(args.agent, role)}",
                flush=True,
            )
            row = {
                "agent": args.agent,
                "harness": harness,
                "role": role,
                "model": model,
                "effort": effort,
                "workdir": str(workdir),
                "conversation_id": conversation,
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
        env=child_environment(place),
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
            f"{harness} has no flag or variable that switches its robomate server off, so "
            "asking would leave the hub tools live; open the conversation by hand with that "
            "server disabled in its configuration"
        )
    try:
        # One harness per log: start sets an earlier harness's log aside.
        selected = saved_conversation(log, args.agent, harness, args.conversation_id)
    except ValueError as exc:
        raise Usage(str(exc)) from None
    workdir = Path(saved["workdir"])
    model, effort = str(saved.get("model") or ""), str(saved.get("effort") or "")
    argv = interactive_resume(
        harness,
        program_for(tools, harness),
        model_flags(harness, model, effort, interactive=True),
        [],
        permissions(harness, args.safe_mode),
        extra,
        selected,
        ask=True,
        fork=args.fork,
    )
    env = (
        opencode_environment(effort, safe=args.safe_mode, no_hub=True)
        if harness == "opencode"
        else {}
    )
    print(
        f"Ask {args.agent} ({harness}): conversation {selected}; working directory {workdir}; "
        f"hub tools are unavailable; permissions: "
        f"{'safe-mode' if args.safe_mode else 'run permissions'}; harness arguments: {extra!a}.",
        flush=True,
    )
    try:
        return subprocess.call(
            argv, cwd=workdir if workdir.is_dir() else None, env=cleared_environment() | env
        )
    except KeyboardInterrupt:
        return common.EXIT_INTERRUPTED


def doctor(args: argparse.Namespace, extra: list[str], tools: Tools) -> int:
    """Denise's optional entry: local reads only, no join, no MCP, no supervision."""

    import denise
    from agent_hub_common.discovery import DiscoveryError, LocalHub, find_hub

    try:
        hub = find_hub(Path.cwd(), selector=args.hub)
    except DiscoveryError as exc:
        raise Usage(str(exc)) from None
    if not isinstance(hub, LocalHub):
        raise Usage(
            "the doctor reads a local hub's state: select it with --hub, not ROBOMATE_HUB_URL"
        )
    runs = hub.state_dir / "runs"
    available = (
        sorted(p.name for p in runs.iterdir() if (p / "manifest.json").is_file())
        if runs.is_dir()
        else []
    )
    if not args.run or not _RUN_ID.fullmatch(args.run) or args.run not in available:
        named = f"run {args.run!r} has no manifest in {runs}" if args.run else "no --run given"
        raise Usage(
            f"{named}. Select one of this hub's runs with --run: "
            f"{', '.join(available) or '(none)'}. The doctor never creates a run."
        )
    run_dir = runs / args.run
    try:
        manifest = json.loads((run_dir / "manifest.json").read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise Usage(
            f"{run_dir / 'manifest.json'} is unreadable ({exc}); restore it first"
        ) from None
    if not isinstance(manifest, dict) or manifest.get("run_id") != args.run:
        raise Usage(
            f"{run_dir / 'manifest.json'} does not describe run {args.run}; restore it first"
        )
    try:
        settings = denise.resolve_settings(
            {}, settings_from="none", harness=args.harness, model=args.model, effort=args.effort
        )
    except ValueError as exc:
        raise Usage(str(exc)) from None
    if settings["status"] != "ready":
        raise Usage(settings["skip_reason"])
    harness = settings["harness"]
    profile_name = "codex" if harness == "codex" else "claude"
    target = Path(args.target).resolve() if args.target else None
    context = denise.build_joined_context(
        manifest,
        run_dir=run_dir,
        state_dir=hub.state_dir,
        sessions_dir=Path(args.sessions_dir) if args.sessions_dir else hub.state_dir / "sessions",
        target=target,
        tooling_root=ROOT,
        settings=settings,
    )
    doctor_dir = context["doctor"]
    # Denise's own harness home/config under the selected run (docs/denise/guide.md):
    # no robomate MCP entry, no team permissions, no unattended approvals.
    profile = Path(doctor_dir["configs"]) / profile_name
    doctor_dir["profile"] = str(profile)
    report = denise.render_joined(context)
    profile.mkdir(parents=True, mode=0o700, exist_ok=True)
    if harness == "codex":
        (profile / "config.toml").write_text(
            "# Denise's doctor profile (agent-launcher.py doctor): no MCP servers and no\n"
            "# unattended approvals. Rewritten at each start; log in once with\n"
            f"#   CODEX_HOME={profile} codex login\n",
            encoding="utf-8",
        )
        profile_variable = "CODEX_HOME"
        login = (
            ""
            if (profile / "auth.json").is_file()
            else f"Denise's Codex profile has no login yet: run `CODEX_HOME={profile} codex "
            "login`, or log in when Codex asks. Never point it at another agent's home."
        )
    else:
        profile_variable = "CLAUDE_CONFIG_DIR"
        login = (
            f"Denise's Claude Code profile is {profile}: log in there when Claude Code asks, "
            "never with another agent's configuration."
        )
    write_roots = [
        root["path"] for root in context["access_inventory"]["roots"] if root["mode"] == "write"
    ]
    read_roots = [
        root["path"] for root in context["access_inventory"]["roots"] if root["mode"] == "read"
    ]
    prompt = f"Read {doctor_dir['startup']} and follow the instructions in it"
    program = program_for(tools, harness)
    flags = model_flags(harness, settings["model"], settings["effort"], interactive=True)
    if harness == "codex":
        # No MCP server at all, and write grants only on the selected run's roots.
        argv = [*program, "-C", doctor_dir["workdir"], "-c", "mcp_servers={}"]
        argv += ["-c", "sandbox_mode=workspace-write"]
        for root in write_roots:
            argv += ["--add-dir", root]
        argv += [*flags, *extra, prompt]
    else:
        argv = [*program, *flags, "--strict-mcp-config"]
        for root in [*write_roots, *read_roots]:
            argv += ["--add-dir", root]
        argv += [*extra, prompt]
    print(
        f"Denise ({harness}) for run {args.run}: no hub tools; context {doctor_dir['context']}; "
        f"report {report}; write roots: {', '.join(write_roots) or '(none)'}; "
        f"harness arguments: {extra!a}.",
        flush=True,
    )
    if login:
        print(login, flush=True)
    # No inherited hub selection: discovery is scoped to each operator command.
    environment = cleared_environment() | {profile_variable: str(profile)}
    environment |= {key: doctor_dir["tmp"] for key in ("TMPDIR", "TEMP", "TMP")}
    return subprocess.call(argv, cwd=doctor_dir["workdir"], env=environment)


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
        epilog="Words after --harness-args go to the harness verbatim, after generated flags.",
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
    hub_options(again)
    interactive_options(again)
    backoff_options(again)
    question = commands.add_parser("ask", help="ask a saved conversation questions; no hub")
    question.add_argument("agent")
    question.add_argument("conversation_id", nargs="?")
    question.add_argument("--fork", action="store_true", help="ask in a new conversation")
    question.add_argument("--safe-mode", action="store_true")
    hub_options(question)
    check = commands.add_parser("doctor", help="consult Denise about a run; no join, no hub")
    check.add_argument("--hub", metavar="NAME|HUB_ID|CHECKOUT", help="the hub to read")
    check.add_argument("--run", help="the run ID under the hub's runs/")
    check.add_argument("--harness", required=True, choices=("claude", *DOCTOR_HARNESSES))
    check.add_argument("--model", default="")
    check.add_argument("--effort", default="")
    check.add_argument("--target", help="the target checkout (default: the hub's checkout)")
    check.add_argument("--sessions-dir", help=argparse.SUPPRESS)
    return top


def parse(argv: list[str]) -> tuple[argparse.Namespace, list[str]]:
    words, extra = split_arguments(argv)
    args = parser().parse_args(words)
    if args.action != "doctor":
        from agent_hub_common.discovery import validate_agent_name

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
            "read_retry_delay_s",
            "predecessor_wait_s",
        ):
            if not getattr(args, key) >= 0:
                raise Usage(f"--{key.replace('_', '-')} must not be negative")
        if not args.predecessor_poll_s > 0:
            raise Usage("--predecessor-poll-s must be positive")
        if args.safe_mode and not args.interactive:
            raise Usage("--safe-mode applies to --interactive launches")
    return args, extra


def main(
    argv: list[str] | None = None,
    *,
    programs: Programs | None = None,
    reader: common.HubReader | None = None,
    workspace: Callable[[str, str | None], Path] | None = None,
) -> int:
    tools = Tools(programs or {}, reader, workspace or robomate_workspace)
    try:
        args, extra = parse(sys.argv[1:] if argv is None else argv)
        action = {"start": start, "resume": resume, "ask": ask, "doctor": doctor}[args.action]
        return action(args, extra, tools)
    except Usage as exc:
        say(str(exc))
        return 2
    except common.Stop as stop:
        say(str(stop))
        return stop.code


if __name__ == "__main__":
    sys.exit(main())
