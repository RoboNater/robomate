#!/usr/bin/env python3
"""Start a scripted robomate run from one config file (M2, #268).

The joined successor of ``prep-standard-run``: one command starts the hub of a
fresh checkout, the agents' worktrees, the submitted work and all three agents
headless under the launcher's supervision; the run then proceeds without
further input. It renders no team config, prompt or start script: it runs the
M2 commands (``robomate up``, ``robomate workspace``, ``robomate submit``,
``scripts/agent-launcher.py start``) in order. The same Python runs under Bash
and PowerShell::

    uv run --locked --project <robomate> python <robomate>/scripts/start-run.py \\
        --config run-config.toml --issue 42
    uv run --locked --project <robomate> python <robomate>/scripts/start-run.py \\
        --config run-config.toml --work-file statement.md
    uv run --locked --project <robomate> python <robomate>/scripts/start-run.py \\
        --generate-default-config run-config.toml
    uv run --locked --project <robomate> python <robomate>/scripts/start-run.py \\
        --config run-config.toml --issue 42 --generate-config next-run.toml

TOML keys use underscores and CLI options hyphens; the command line overrides
the file, and either ``issue`` or ``work_file`` selects the work. A
``prep-standard-run`` config is read as it is. ``[policy]`` keys, and
``--policy k=v``, go to ``robomate submit --policy`` over the configuration
layers. The repository is cloned to ``<run_dir>/<hub_name>``; the hub keeps its
state, token and worktrees beside it, in ``<hub_name>.robomate/``. The command
stays in the foreground until every agent's launcher exits, then stops the hub
if it started it. Agents' output goes to ``<run_dir>/logs/<agent>.log``.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import re
import shlex
import signal
import subprocess
import sys
import time
import tomllib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import IO, Any

SCRIPTS = Path(__file__).resolve().parent
ROOT = SCRIPTS.parent
sys.path.insert(0, str(SCRIPTS))
import agent_launcher as launcher_defaults  # noqa: E402
from denise import resolve_settings  # noqa: E402
from run_common import probe_harness_version  # noqa: E402

#: The standard team: names and standing roles are fixed; harness, model and effort are set.
AGENTS = {"alice": "orchestrator", "bob": "implementer", "charlie": "reviewer"}
#: The case-insensitive harness spellings prep-standard-run accepts.
HARNESS_NAMES = {
    "claude": "claude-code",
    "claude-code": "claude-code",
    "codex": "codex",
    "opencode": "opencode",
    "antigravity": "antigravity",
    "agy": "antigravity",
    "devin": "devin",
}
DEFAULTS: dict[str, str | int | float | bool] = {
    "repository": "",
    "run_parent_dir": "~/working",
    "run_dir": "",
    "hub_name": "",
    "hub_port": 0,
    "issue": 0,
    "work_file": "",
    "account": "",
    "reservations": "",
    "forge": "",
    "alice_harness": "claude",
    "alice_model": "",
    "alice_effort": "",
    "bob_harness": "claude",
    "bob_model": "",
    "bob_effort": "",
    "charlie_harness": "codex",
    "charlie_model": "",
    "charlie_effort": "",
    "denise_settings_from": "bob",
    "denise_harness": "",
    "denise_model": "",
    "denise_effort": "",
    "auto_start": True,
    "pytest_workers": 0,
    "max_resumes": -1,
    "resume_delay_s": launcher_defaults.DEFAULT_RESUME_DELAY_S,
    "resume_max_delay_s": launcher_defaults.DEFAULT_RESUME_MAX_DELAY_S,
    "resume_total_s": launcher_defaults.DEFAULT_RESUME_TOTAL_S,
    "resume_series_reset_s": launcher_defaults.DEFAULT_RESUME_SERIES_RESET_S,
}
FLOAT_KEYS = ("resume_delay_s", "resume_max_delay_s", "resume_total_s", "resume_series_reset_s")
PATH_KEYS = ("run_parent_dir", "work_file")
#: prep-standard-run's top-level policy keys; this reads them into ``[policy]``.
POLICY_SHORTHANDS = ("stall_after_min", "stall_action_after_min")
COMMENTS = {
    "repository": "Clone URL or local path of the target repository.",
    "run_dir": "The run's directory; relative to run_parent_dir unless absolute.",
    "hub_name": "Hub name (it appears in branch names); empty derives it from run_dir.",
    "hub_port": "0 lets robomate up take the first free port from 8420.",
    "issue": "Select the work with issue = N or work_file = PATH, not both.",
    "account": "The forge account the agents name in their comments.",
    "reservations": "Optional shared-counter reservations issue (N, #N or OWNER/REPO#N).",
    "forge": "github or gitlab; empty lets robomate up detect it from the origin.",
    "alice_harness": "claude, codex, opencode, antigravity or devin; model and effort optional.",
    "denise_settings_from": "Denise (doctor) settings: alice, bob, charlie or none.",
    "auto_start": "false prints each agent's interactive start command instead.",
    "pytest_workers": "Workers per pytest -n auto run; 0 uses the CPU count.",
    "max_resumes": "Launcher resume bounds; max_resumes = -1 is unlimited.",
}
POLICY_COMMENT = """\
# Workflow policy for this run, over the hub, repository and user
# configuration layers (robomate submit --policy k=v). For example:
# merge_method = "squash"
# max_review_rounds = 3
# allow_no_ci = "auto"
# reviewer_harness_differs = true
# stall_after_min = 20
# stall_action_after_min = 0
"""
#: The counter and close-out text prepare-run puts in every statement (#34).
COUNTER_CLOSE_OUT = (
    "Only if the run uses a shared counter, claim a unique value in the run's "
    "reservations issue before writing code that uses it; use the shared-body "
    "procedure, advance the next value, and have the reviewer verify the claim. "
    "After merge, close out only that claim as shipped; release it if abandoned. "
    "If no counter is used, no reservations close-out task is needed. "
    "Ordinary runs require no roadmap decision comment, adjudication or completion edit; "
    "the Closes reference closes the issue and triage maintains the plan."
)
NO_RESERVATIONS = (
    "No reservations issue was supplied. If the work needs a shared counter, ask the "
    "operator to identify its authoritative shared issue before using a value; a hub "
    "log alone is not a reservation."
)
#: Hang bounds, not budgets: a healthy hub answers within seconds.
HUB_START_TIMEOUT_S = 180.0
HUB_STOP_TIMEOUT_S = 60.0
CLI_TIMEOUT_S = 180.0
AGENT_STOP_TIMEOUT_S = 30.0
POLL_S = 0.5
_POLICY_KEY = re.compile(r"[A-Za-z0-9_]+(\.[A-Za-z0-9_]+)*")


class Interrupted(Exception):
    """SIGTERM or Ctrl-C: stop the agents and the hub this command started."""


def say(message: str) -> None:
    print(f"start-run: {message}", flush=True)


# -- settings -----------------------------------------------------------------------


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(
        prog="start-run.py",
        description="Start a scripted run: hub, worktrees, submitted work and three agents.",
        allow_abbrev=False,
    )
    result.add_argument("--config", type=Path, help="TOML run config file")
    mode = result.add_mutually_exclusive_group()
    mode.add_argument(
        "--generate-default-config", type=Path, metavar="PATH", help="write the defaults and stop"
    )
    mode.add_argument(
        "--generate-config",
        type=Path,
        metavar="PATH",
        help="write the merged defaults, config and CLI values instead of starting",
    )
    result.add_argument(
        "--policy",
        action="append",
        default=[],
        metavar="k=v",
        help="override a [policy] key for this run (repeatable; TOML values)",
    )
    work = result.add_mutually_exclusive_group()
    for key, default in DEFAULTS.items():
        flag = "--" + key.replace("_", "-")
        group = work if key in ("issue", "work_file") else result
        if isinstance(default, bool):
            group.add_argument(flag, action=argparse.BooleanOptionalAction, default=None)
        else:
            kind = float if key in FLOAT_KEYS else int if isinstance(default, int) else str
            group.add_argument(flag, type=kind, default=None)
    return result


def canonical_path(value: str, base: Path) -> str:
    return str((base / Path(value).expanduser()).resolve())


def flatten(table: Mapping[str, Any], prefix: str = "") -> dict[str, Any]:
    """``[policy]`` as the dotted keys ``robomate submit --policy`` takes."""

    flat: dict[str, Any] = {}
    for key, value in table.items():
        name = f"{prefix}{key}"
        if not _POLICY_KEY.fullmatch(name):
            raise ValueError(f"invalid policy key {name!r}")
        if isinstance(value, dict):
            flat |= flatten(value, f"{name}.")
        else:
            flat[name] = value
    return flat


def parse_policy_override(spec: str) -> tuple[str, Any]:
    """One ``--policy k=v``, typed as ``robomate submit`` types it (TOML, else a string)."""

    from robomate.cli import _parse_policy_override

    key, value = _parse_policy_override(spec)
    if not _POLICY_KEY.fullmatch(key):
        raise ValueError(f"invalid policy key {key!r}")
    return key, value


def load_config(path: Path) -> tuple[dict[str, Any], dict[str, Any]]:
    """The file's run settings and its ``[policy]`` keys; unknown keys are refused."""

    config = path.expanduser().resolve()
    with config.open("rb") as stream:
        loaded = tomllib.load(stream)
    if "roadmap" in loaded:
        raise ValueError("roadmap was renamed to reservations in the run config")
    raw_policy = loaded.pop("policy", {})
    if not isinstance(raw_policy, dict):
        raise ValueError("policy must be a table: [policy]")
    policy = flatten(raw_policy)
    for key in POLICY_SHORTHANDS:
        if key in loaded:
            if key in policy:
                raise ValueError(f"{key} is set both at the top level and in [policy]; keep one")
            policy[key] = loaded.pop(key)
    if unknown := loaded.keys() - DEFAULTS.keys():
        raise ValueError(f"unknown config keys: {', '.join(sorted(unknown))}")
    values: dict[str, Any] = {}
    for key, value in loaded.items():
        expected = DEFAULTS[key]
        if key in FLOAT_KEYS:
            if type(value) not in (int, float):
                raise ValueError(f"{key} must be a number of seconds")
            value = float(value)
        elif type(value) is not type(expected):
            raise ValueError(f"{key} must be a {type(expected).__name__}")
        if key in PATH_KEYS and value:
            value = canonical_path(value, config.parent)
        if key == "repository" and value and (config.parent / value).is_dir():
            value = canonical_path(value, config.parent)
        # An absolute run_dir is portable even when the config is moved.
        if key == "run_dir" and value and Path(value).expanduser().is_absolute():
            value = canonical_path(value, config.parent)
        values[key] = value
    return values, policy


def settings(args: argparse.Namespace) -> tuple[dict[str, Any], dict[str, Any]]:
    """Defaults, then the config file, then the command line; validated."""

    values: dict[str, Any] = dict(DEFAULTS)
    policy: dict[str, Any] = {}
    if args.config:
        loaded, policy = load_config(args.config)
        values |= loaded
    for key in DEFAULTS:
        value = getattr(args, key)
        if value is not None:
            values[key] = value
    for spec in args.policy:
        key, value = parse_policy_override(spec)
        policy[key] = value
    if values["repository"] and Path(values["repository"]).expanduser().is_dir():
        values["repository"] = canonical_path(values["repository"], Path.cwd())
    # Selecting work on the command line replaces the config's selection.
    if args.issue is not None:
        values["work_file"] = ""
    if args.work_file is not None:
        values["issue"] = 0
    for key in PATH_KEYS:
        if values[key]:
            values[key] = canonical_path(values[key], Path.cwd())
    if values["run_dir"]:
        values["run_dir"] = canonical_path(values["run_dir"], Path(values["run_parent_dir"]))
    validate(values, policy, starting=False)
    return values, policy


def harness_of(values: Mapping[str, Any], agent: str) -> str:
    return HARNESS_NAMES[str(values[f"{agent}_harness"]).strip().lower()]


def hub_name(values: Mapping[str, Any]) -> str:
    from agent_hub_common.discovery import derive_hub_name

    if values["hub_name"]:
        return str(values["hub_name"])
    return derive_hub_name(Path(values["run_dir"]))


def validate_policy(policy: Mapping[str, Any]) -> None:
    """The hub's own policy check, over the built-in defaults: unknown keys are refused."""

    from agent_hub.submit import _resolve_policy
    from agent_hub_common import WorkflowPolicy

    _resolve_policy(WorkflowPolicy(), dict(policy))


def denise(values: Mapping[str, Any]) -> dict[str, str]:
    team = {
        name: {key: str(values[f"{name}_{key}"]) for key in ("harness", "model", "effort")}
        for name in AGENTS
    }
    return resolve_settings(
        team,
        **{key: values[f"denise_{key}"] for key in ("settings_from", "harness", "model", "effort")},
    )


def validate(values: Mapping[str, Any], policy: Mapping[str, Any], *, starting: bool) -> None:
    from agent_hub_common import MAX_STATEMENT_BYTES
    from agent_hub_common.discovery import DiscoveryError, validate_hub_name

    if values["pytest_workers"] < 0:
        raise ValueError("pytest_workers must not be negative (0 means CPU count)")
    if values["issue"] < 0:
        raise ValueError("issue must be positive (0 means unset)")
    if values["issue"] and values["work_file"]:
        raise ValueError("choose either issue or work_file")
    if values["forge"] not in ("", "github", "gitlab"):
        raise ValueError("forge must be github, gitlab or empty (detected)")
    if not 0 <= values["hub_port"] <= 65535:
        raise ValueError("hub_port must be 0 (first free) or a TCP port")
    if values["hub_name"]:
        try:
            validate_hub_name(values["hub_name"])
        except DiscoveryError as exc:
            raise ValueError(str(exc)) from None
    if values["max_resumes"] < -1:
        raise ValueError("max_resumes must be -1 (unlimited) or a count")
    for key in FLOAT_KEYS:
        if not math.isfinite(values[key]) or values[key] < 0:
            raise ValueError(f"{key} must be a finite, non-negative number of seconds")
    for name in AGENTS:
        if str(values[f"{name}_harness"]).strip().lower() not in HARNESS_NAMES:
            raise ValueError(f"invalid {name}_harness {values[f'{name}_harness']!r}")
        if harness_of(values, name) == "devin" and values[f"{name}_effort"]:
            raise ValueError(f"devin has no reasoning-effort flag; leave {name}_effort empty")
    denise(values)
    validate_policy(policy)
    if not starting:
        return
    for key in ("repository", "run_dir", "account"):
        if not str(values[key]).strip():
            raise ValueError(f"{key} is required")
    if not (values["issue"] or values["work_file"]):
        raise ValueError("choose --issue or --work-file (or set it in the config)")
    run_dir = Path(values["run_dir"])
    if run_dir == ROOT or ROOT in run_dir.parents:
        raise ValueError("run_dir must be outside the robomate checkout")
    if values["work_file"]:
        text = read_work(Path(values["work_file"]))
        if len(text.encode("utf-8")) > MAX_STATEMENT_BYTES:
            raise ValueError(
                f"work_file is over {MAX_STATEMENT_BYTES} bytes, the statement cap; shorten "
                "it, or put the detail in an issue and select that instead"
            )
    hub_name(values)


def read_work(path: Path) -> str:
    try:
        text = path.read_bytes().decode("utf-8").strip()
    except FileNotFoundError as exc:
        raise ValueError(f"work_file {path} does not exist") from exc
    except (OSError, UnicodeDecodeError) as exc:
        raise ValueError(f"work_file {path} is not a readable UTF-8 text file") from exc
    if not text:
        raise ValueError(f"work_file {path} is empty; write the statement of work first")
    return text


def toml_literal(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int | float):
        if isinstance(value, float) and not math.isfinite(value):
            raise ValueError(f"{value!r} has no TOML form here")
        return repr(value)
    if isinstance(value, str):
        return json.dumps(value, ensure_ascii=False)
    if isinstance(value, list):
        return "[" + ", ".join(toml_literal(item) for item in value) + "]"
    raise ValueError(f"cannot write {value!r} as a TOML value")


def write_config(path: Path, values: Mapping[str, Any], policy: Mapping[str, Any]) -> None:
    """Create a TOML run config; refuse to overwrite an existing file."""

    lines = [
        "# robomate scripted run (scripts/start-run.py). Empty strings and 0 mean unset.",
        "# Command-line options override these; --issue or --work-file selects the work.",
    ]
    for key, value in values.items():
        if key in COMMENTS:
            lines.append(f"# {COMMENTS[key]}")
        lines.append(f"{key} = {toml_literal(value)}")
    lines += ["", "[policy]", POLICY_COMMENT.rstrip("\n")]
    lines += [f"{key} = {toml_literal(value)}" for key, value in policy.items()]
    with path.expanduser().open("x", encoding="utf-8") as stream:
        stream.write("\n".join(lines) + "\n")
    say(f"wrote {path}")


# -- the statement --------------------------------------------------------------------


def origin_slug(origin: str, forge: str) -> str | None:
    if forge == "gitlab":
        try:
            from agent_hub.gitlab_gate import GitLabProject

            return GitLabProject.from_origin(origin).path
        except Exception:
            return None
    from agent_hub.forge_preflight import parse_github_slug

    return parse_github_slug(origin)


def compose_statement(values: Mapping[str, Any], *, origin: str, forge: str) -> str:
    """What prepare-run queued, for the joined run: the work, its counter rules, the account.

    An issue gets ``robomate submit --issue``'s own one-issue sentence. The
    reservations target and close-out text follow, as prepare-run wrote them,
    and the account the agents name in comments closes it, where prepare-run
    put it in Alice's prompt. The statement is the one per-run text every
    assignment carries (``get_statement``).
    """

    from robomate.cli import _render_issue_statement

    slug = origin_slug(origin, forge)
    reservations = str(values["reservations"]).strip()
    if not reservations:
        target = NO_RESERVATIONS
    else:
        if match := re.fullmatch(r"#?(\d+)", reservations):
            reservations = f"{slug or origin}#{match.group(1)}"
        target = (
            f"The run's reservations issue is ({reservations}); read it only if a counter "
            "is needed."
        )
    rules = f"{target} {COUNTER_CLOSE_OUT}"
    if values["issue"]:
        body = f"{_render_issue_statement(int(values['issue']), origin, forge)} {rules}"
    else:
        body = f"{read_work(Path(values['work_file']))}\n\nWhen done, close out. {rules}"
    return f"{body}\n\nForge comment identity account: `{values['account']}`."


# -- commands -------------------------------------------------------------------------


def operator_environment() -> dict[str, str]:
    """This environment without an inherited hub selection: commands find the run's hub."""

    return {
        key: value
        for key, value in os.environ.items()
        if key != "ROBOMATE_HUB_URL" and not key.startswith("ROBOMATE_TOKEN")
    }


def robomate(
    checkout: Path, *words: str, stdin: str | None = None, check: bool = True
) -> subprocess.CompletedProcess[str]:
    done = subprocess.run(
        [sys.executable, "-m", "robomate", *words],
        cwd=checkout,
        env=operator_environment(),
        input=stdin,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=CLI_TIMEOUT_S,
        check=False,
    )
    if check and done.returncode != 0:
        detail = (done.stderr.strip() or done.stdout.strip() or f"exit {done.returncode}")[-2000:]
        raise RuntimeError(f"robomate {words[0]} failed: {detail}")
    return done


def hub_status(checkout: Path) -> dict[str, Any] | None:
    """``robomate status --json`` for the checkout's hub when it serves this checkout."""

    try:
        done = robomate(checkout, "status", "--json", check=False)
    except subprocess.TimeoutExpired:
        return None
    try:
        status = json.loads(done.stdout)
    except ValueError:
        return None
    if not isinstance(status, dict) or status.get("running") is False:
        return None
    reported = status.get("checkout") or status.get("repo_root")
    if not reported or os.path.realpath(str(reported)) != os.path.realpath(checkout):
        return None
    return status


def command_line(words: Sequence[str]) -> str:
    """A command the operator can paste: PowerShell on Windows, POSIX shells elsewhere."""

    if os.name == "nt":
        return "& " + " ".join("'" + word.replace("'", "''") + "'" for word in words)
    return shlex.join(words)


def git(*words: str) -> str:
    return subprocess.run(
        ["git", *words], check=True, capture_output=True, text=True, encoding="utf-8"
    ).stdout.strip()


def private_dir(path: Path) -> Path:
    path.mkdir(parents=True, mode=0o700, exist_ok=True)
    if os.name != "nt":
        path.chmod(0o700)
    return path


@dataclass
class Run:
    """One start: its places, the processes it owns and what it reports."""

    values: dict[str, Any]
    policy: dict[str, Any]
    run_dir: Path
    checkout: Path
    name: str
    versions: dict[str, str] = field(default_factory=dict)
    hub: subprocess.Popen[str] | None = None
    agents: dict[str, subprocess.Popen[str]] = field(default_factory=dict)
    logs: list[IO[str]] = field(default_factory=list)
    run_id: str = ""
    status: dict[str, Any] = field(default_factory=dict)

    @property
    def logs_dir(self) -> Path:
        return self.run_dir / "logs"

    def log_file(self, name: str) -> IO[str]:
        self.logs_dir.mkdir(parents=True, exist_ok=True)
        stream = (self.logs_dir / f"{name}.log").open("a", encoding="utf-8")
        self.logs.append(stream)
        return stream


def probe_versions(values: Mapping[str, Any]) -> dict[str, str]:
    """Each team harness's own ``--version`` (#402): no placeholder, or no start."""

    versions: dict[str, str] = {}
    for agent in AGENTS:
        harness = harness_of(values, agent)
        if harness not in versions:
            versions[harness] = probe_harness_version(harness)[1]
    return versions


def ensure_checkout(values: Mapping[str, Any], checkout: Path) -> None:
    repository = str(values["repository"])
    if checkout.exists():
        origin = git("-C", str(checkout), "remote", "get-url", "origin")
        if origin != repository:
            raise ValueError(f"existing checkout {checkout} has origin {origin!r}, not repository")
        say(f"reusing checkout {checkout}")
        return
    checkout.parent.mkdir(parents=True, exist_ok=True)
    say(f"cloning {repository} into {checkout}")
    subprocess.run(["git", "clone", "-q", "--", repository, str(checkout)], check=True)


def start_hub(run: Run) -> dict[str, Any]:
    """Reuse the checkout's live hub, or start ``robomate up`` and wait until it answers."""

    status = hub_status(run.checkout)
    if status is not None:
        say(f"reusing hub {status.get('name')} at {status.get('url')}; it is left running")
        return status
    words = [sys.executable, "-m", "robomate", "up", "--name", run.name]
    if run.values["forge"]:
        words += ["--forge", str(run.values["forge"])]
    if run.values["hub_port"]:
        words += ["--port", str(run.values["hub_port"])]
    stream = run.log_file("hub")
    run.hub = subprocess.Popen(
        words,
        cwd=run.checkout,
        env=operator_environment(),
        stdin=subprocess.DEVNULL,
        stdout=stream,
        stderr=subprocess.STDOUT,
        text=True,
    )
    say(f"starting hub {run.name} (log {run.logs_dir / 'hub.log'})")
    deadline = time.monotonic() + HUB_START_TIMEOUT_S
    while time.monotonic() < deadline:
        if run.hub.poll() is not None:
            raise RuntimeError(f"robomate up exited with {run.hub.returncode}: {tail(run, 'hub')}")
        status = hub_status(run.checkout)
        if status is not None:
            say(f"hub {status.get('name')} serving at {status.get('url')}")
            return status
        time.sleep(POLL_S)
    raise RuntimeError(
        f"the hub did not answer within {HUB_START_TIMEOUT_S:g} s: {tail(run, 'hub')}"
    )


def tail(run: Run, name: str, lines: int = 20) -> str:
    try:
        text = (run.logs_dir / f"{name}.log").read_text(encoding="utf-8", errors="replace")
    except OSError:
        return "(no log)"
    return "\n".join(text.splitlines()[-lines:])


def refuse_live_run(status: Mapping[str, Any]) -> None:
    workflow = status.get("workflow")
    if isinstance(workflow, dict) and workflow.get("status") not in (None, "done"):
        raise RuntimeError(
            f"the hub already has run {workflow.get('run_id')} ({workflow.get('status')}); "
            "finish it, or start this run in another run_dir"
        )
    if status.get("queue"):
        raise RuntimeError("the hub already has queued work; finish it first")


def submit(run: Run, status: Mapping[str, Any]) -> None:
    origin = str(status.get("origin") or "")
    forge = str(status.get("forge") or "github")
    statement = compose_statement(run.values, origin=origin, forge=forge)
    words = ["submit", "-"]
    for key, value in run.policy.items():
        words += ["--policy", f"{key}={toml_literal(value)}"]
    done = robomate(run.checkout, *words, stdin=statement)
    printed = done.stdout.strip()
    match = re.search(r" run (\S+)$", printed)
    run.run_id = match.group(1) if match else ""
    say(printed or "submitted")


def launch_words(run: Run, agent: str, *, interactive: bool) -> list[str]:
    values = run.values
    harness = harness_of(values, agent)
    cache = private_dir(run.run_dir / "tmp" / agent) / "uv-cache"
    words = [
        sys.executable,
        str(SCRIPTS / "agent-launcher.py"),
        "start",
        agent,
        "--role",
        AGENTS[agent],
        "--harness",
        harness,
        "--hub",
        str(run.checkout),
        "--harness-version",
        run.versions[harness],
        "--uv-cache-dir",
        str(cache),
        "--pytest-workers",
        str(values["pytest_workers"]),
    ]
    for key in ("model", "effort"):
        if values[f"{agent}_{key}"]:
            words += [f"--{key}", str(values[f"{agent}_{key}"])]
    if interactive:
        return [*words, "--interactive"]
    if values["max_resumes"] >= 0:
        words += ["--max-resumes", str(values["max_resumes"])]
    for key in FLOAT_KEYS:
        words += ["--" + key.replace("_", "-"), f"{values[key]:g}"]
    return words


def start_agents(run: Run) -> None:
    for agent in AGENTS:
        words = launch_words(run, agent, interactive=False)
        run.agents[agent] = subprocess.Popen(
            words,
            cwd=run.checkout,
            env=operator_environment(),
            stdin=subprocess.DEVNULL,
            stdout=run.log_file(agent),
            stderr=subprocess.STDOUT,
            text=True,
        )
        say(
            f"started {agent} ({AGENTS[agent]}, {harness_of(run.values, agent)}) "
            f"pid {run.agents[agent].pid}; log {run.logs_dir / (agent + '.log')}"
        )


def resume_hint(run: Run, agent: str) -> str:
    words = [
        "uv",
        "run",
        "--locked",
        "--project",
        str(ROOT),
        "python",
        str(SCRIPTS / "agent-launcher.py"),
        "resume",
        agent,
        "--hub",
        str(run.checkout),
    ]
    return command_line(words)


def supervise(run: Run) -> int:
    """Wait for every launcher; report each exit, with the resume command after a failure."""

    worst = 0
    pending = dict(run.agents)
    while pending:
        for agent, process in list(pending.items()):
            code = process.poll()
            if code is None:
                continue
            del pending[agent]
            say(f"{agent}'s launcher exited with {code}")
            if code:
                worst = worst or code
                say(
                    f"see {run.logs_dir / (agent + '.log')}; while the hub runs, resume with: "
                    + resume_hint(run, agent)
                )
        time.sleep(POLL_S)
    return worst


def wait_for_done(run: Run) -> None:
    """With auto_start = false: the hub stays up until the run is done or interrupted."""

    while True:
        if run.hub is not None and run.hub.poll() is not None:
            say(f"the hub exited with {run.hub.returncode}")
            return
        status = hub_status(run.checkout)
        workflow = (status or {}).get("workflow")
        if isinstance(workflow, dict) and workflow.get("status") == "done":
            return
        time.sleep(5 * POLL_S)


def stop_agents(run: Run) -> None:
    """After an interrupt: the launchers stop their own harnesses on SIGTERM or Ctrl-C."""

    live = [process for process in run.agents.values() if process.poll() is None]
    if os.name != "nt":
        for process in live:
            process.terminate()
    deadline = time.monotonic() + AGENT_STOP_TIMEOUT_S
    for process in live:
        try:
            process.wait(timeout=max(0.0, deadline - time.monotonic()))
        except subprocess.TimeoutExpired:
            process.terminate()
            try:
                process.wait(timeout=AGENT_STOP_TIMEOUT_S)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()


def stop_hub(run: Run) -> None:
    """Stop only the hub this command started, with ``robomate down``."""

    if run.hub is None:
        return
    if run.hub.poll() is None:
        done = robomate(run.checkout, "down", check=False)
        if done.returncode != 0:
            say(f"robomate down failed: {done.stderr.strip() or done.stdout.strip()}")
        try:
            run.hub.wait(timeout=HUB_STOP_TIMEOUT_S)
        except subprocess.TimeoutExpired:
            run.hub.terminate()
            run.hub.wait(timeout=HUB_STOP_TIMEOUT_S)
    say(f"hub {run.name} stopped (exit {run.hub.returncode}); `robomate up` in {run.checkout}")


def report(run: Run) -> bool:
    """The run's final state, where its closeout reports are, and how to consult Denise."""

    status = hub_status(run.checkout) or run.status
    workflow = status.get("workflow") if isinstance(status, dict) else None
    done = isinstance(workflow, dict) and workflow.get("status") == "done"
    run_id = (workflow or {}).get("run_id") if isinstance(workflow, dict) else None
    run_id = run_id or run.run_id
    if isinstance(workflow, dict):
        say(f"run {run_id}: workflow {workflow.get('status')}")
    state = status.get("state_dir") if isinstance(status, dict) else None
    if state and run_id:
        closeout = Path(str(state)) / "runs" / str(run_id) / "closeout"
        if closeout.is_dir():
            say(f"closeout reports: {closeout}")
    doctor = denise(run.values)
    if doctor["status"] != "ready":
        say(f"Denise skipped: {doctor['skip_reason']}")
    elif run_id:
        words = [
            "uv",
            "run",
            "--locked",
            "--project",
            str(ROOT),
            "python",
            str(SCRIPTS / "agent-launcher.py"),
            "doctor",
            "--hub",
            str(run.checkout),
            "--run",
            str(run_id),
            "--harness",
            doctor["harness"],
        ]
        for key in ("model", "effort"):
            if doctor[key]:
                words += [f"--{key}", doctor[key]]
        say("consult Denise about this run with: " + command_line(words))
    return done


def start(values: dict[str, Any], policy: dict[str, Any]) -> int:
    validate(values, policy, starting=True)
    run_dir = Path(values["run_dir"])
    name = hub_name(values)
    run = Run(values, policy, run_dir, run_dir / name, name)
    run.versions = probe_versions(values)
    run_dir.mkdir(parents=True, exist_ok=True)
    previous = signal.getsignal(signal.SIGTERM)

    def terminated(*_: object) -> None:
        raise Interrupted()

    if os.name != "nt":
        signal.signal(signal.SIGTERM, terminated)
    code = 1
    try:
        ensure_checkout(values, run.checkout)
        status = start_hub(run)
        refuse_live_run(status)
        for agent in AGENTS:
            path = robomate(run.checkout, "workspace", agent).stdout.strip().splitlines()[-1]
            say(f"{agent}'s worktree: {path}")
        submit(run, status)
        run.status = hub_status(run.checkout) or status
        if values["auto_start"]:
            start_agents(run)
            code = supervise(run)
        else:
            say("auto_start = false: start each agent in its own terminal with:")
            for agent in AGENTS:
                print(command_line(launch_words(run, agent, interactive=True)), flush=True)
            say("waiting until the run is done (Ctrl-C stops the hub this command started)")
            wait_for_done(run)
            code = 0
        done = report(run)
        if code == 0 and not done:
            code = 1
    except (Interrupted, KeyboardInterrupt):
        say("interrupted; stopping the agents and the hub this command started")
        stop_agents(run)
        code = launcher_defaults.EXIT_INTERRUPTED
    finally:
        try:
            stop_hub(run)
        finally:
            for stream in run.logs:
                stream.close()
            if os.name != "nt":
                signal.signal(signal.SIGTERM, previous)
    return code


def main(argv: list[str] | None = None) -> int:
    cli = parser()
    arguments = sys.argv[1:] if argv is None else argv
    if any(arg == "--roadmap" or arg.startswith("--roadmap=") for arg in arguments):
        cli.error("--roadmap was renamed to --reservations")
    args = cli.parse_args(arguments)
    try:
        if args.generate_default_config:
            write_config(args.generate_default_config, DEFAULTS, {})
            return 0
        values, policy = settings(args)
        if args.generate_config:
            write_config(args.generate_config, values, policy)
            return 0
        return start(values, policy)
    except (
        OSError,
        ValueError,
        RuntimeError,
        subprocess.CalledProcessError,
        subprocess.TimeoutExpired,
    ) as exc:
        print(f"start-run: error: {exc}", file=sys.stderr, flush=True)
        return 1


if __name__ == "__main__":
    sys.exit(main())
