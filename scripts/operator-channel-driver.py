#!/usr/bin/env python3
"""Scripted operator-channel validation on the GitHub sandbox (#133).

Runs the ten steps of docs/development/operator-channel-validation.md once,
end to end. This script plays every part: the orchestrator (over `/rpc`), both
workers (over A2A, with worker-mcp's client) and the operator (with
`robomate inbox` and `robomate answer`). No agent harness and no person takes
any action, and the evidence it writes says so in its first line.

    uv run --locked python scripts/operator-channel-driver.py
    uv run --locked python scripts/operator-channel-driver.py --operator-ls

It starts its own throwaway hub on a fresh clone of the sandbox, isolated as
guides/worker.md requires of a smoke hub (#145): its registry, operator token
and hub state all live in one temporary directory, inherited `ROBOMATE_*` and
`HUB_*` variables are removed, and it reaches the hub only by the explicit URL
and token that `discover()` returns for its own clone. It stops that hub on
every exit path and prints the state directory, port and stop confirmation.

The machine's own `hubs.json` and `operator-token` are never opened: the
script compares their `os.stat` size and mtime before and after the run.
`--operator-ls` also runs `robomate ls` in the caller's normal environment
while the hub is up and checks that it does not list the driver's hub. That
command calls every registered hub, so only the operator passes it; a worker
must not, because it would reach the run's hub.

Live side effects on the sandbox: one issue, one branch, one PR, one PR
comment and one squash merge per run.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import platform
import shutil
import signal
import socket
import sqlite3
import stat
import subprocess
import sys
import tempfile
import time
import traceback
import urllib.error
import urllib.request
import uuid
from collections.abc import Mapping, Sequence
from contextlib import AsyncExitStack, closing
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
from agent_hub_common import (
    AgentProfile,
    ImplementerOutcome,
    ImplementerResult,
    ModelSource,
    ReviewerResult,
    ReviewerVerdict,
    TestResult,
)
from agent_hub_common.discovery import HubEndpoint, discover
from agent_hub_common.registry import operator_token_path, registry_path
from worker_mcp.client import WorkerHubClient, WorkerProtocolError
from worker_mcp.config import WorkerSettings

SCRIPT = "scripts/operator-channel-driver.py"
SANDBOX = "RoboNater/robo-agents-sandbox"
ROOT = Path(__file__).resolve().parents[1]
DEFAULT_EVIDENCE_DIR = ROOT / "docs" / "evidence"

# The names the script acts under, so no hub row can be read as an agent's.
ORCHESTRATOR = "script-alice"
IMPLEMENTER = "script-bob"
REVIEWER = "script-charlie"
PROFILE = AgentProfile(
    harness="operator-channel-driver",
    provider="none",
    model="none",
    model_source=ModelSource.ENV,
)

# Selectors a caller's shell may carry. An explicit URL comes first in
# discovery, so any of these would point a command at another hub (#145).
DISCOVERY_OVERRIDES = ("ROBOMATE_HUB_URL", "ROBOMATE_TOKEN", "ROBOMATE_TOKEN_FILE")

CONFLICT = -32002
OPERATOR_REQUIRED = -32005
GATE_ATTEMPTS = 8
# The sandbox's own test command, run and reported from this one definition.
SANDBOX_TESTS = ("-m", "unittest", "discover", "-s", "tests", "-v")


class DriverError(RuntimeError):
    """A step did not behave as the validation requires."""


class RpcRefused(RuntimeError):
    def __init__(self, method: str, code: int | None, message: str) -> None:
        super().__init__(f"{method} refused: {message} ({code})")
        self.method = method
        self.code = code
        self.message = message


# -- child output ------------------------------------------------------------

# Every captured child writes UTF-8, which is how the script reads it. gh and
# Git already do; a Python child (`robomate`, the sandbox's tests) writes its
# stdio in the locale code page once redirected on Windows, unless told (#164).
# Set per child, never in the caller's environment.
CHILD_ENCODING = {"PYTHONIOENCODING": "utf-8"}
# A byte that is not UTF-8 after all stays visible as `\xNN` in a diagnostic.
DECODE_ERRORS = "backslashreplace"


def child_env(base: Mapping[str, str]) -> dict[str, str]:
    """`base` with Python child stdio set to the encoding the script reads."""

    return dict(base) | CHILD_ENCODING


# -- isolation ---------------------------------------------------------------


def _inherited_selector(key: str) -> bool:
    return key.upper().startswith(("ROBOMATE_", "HUB_")) or key.upper() == "AGENT_NAME"


def isolated_env(base: Mapping[str, str], temp: Path) -> dict[str, str]:
    """The environment for every `robomate` command and the hub the script runs.

    Every location the hub derives falls inside `temp`: the registry through
    XDG_STATE_HOME (Windows: LOCALAPPDATA), the operator credential through
    ROBOMATE_OPERATOR_TOKEN_FILE. Inherited ROBOMATE_* and HUB_* variables are
    dropped, so neither discovery nor the hub's settings can pick up the
    caller's hub.
    """

    env = child_env({key: value for key, value in base.items() if not _inherited_selector(key)})
    state = temp / "state"
    env.update(
        {
            "XDG_STATE_HOME": str(state),
            "LOCALAPPDATA": str(state),
            "ROBOMATE_OPERATOR_TOKEN_FILE": str(temp / "operator-token"),
            # `robomate up` keeps its state in the clone's own checkout state
            # whatever this says; set anyway, as guides/worker.md asks.
            "HUB_STATE_DIR": str(temp / "hub-state"),
        }
    )
    return env


@dataclass(frozen=True, slots=True)
class FileStat:
    path: str
    exists: bool
    size: int | None = None
    mtime_ns: int | None = None

    def describe(self) -> str:
        if not self.exists:
            return "absent"
        return f"size {self.size}, mtime_ns {self.mtime_ns}"


def stat_only(path: Path) -> FileStat:
    """Size and mtime, never the content: the operator token is never read."""

    try:
        info = path.stat()
    except FileNotFoundError:
        return FileStat(str(path), False)
    if not stat.S_ISREG(info.st_mode):
        raise DriverError(f"{path} is not a regular file")
    return FileStat(str(path), True, info.st_size, info.st_mtime_ns)


@dataclass(frozen=True, slots=True)
class MachineState:
    """The caller's real registry and operator credential, by stat alone."""

    registry: FileStat
    operator_token: FileStat

    @classmethod
    def capture(cls, environ: Mapping[str, str]) -> MachineState:
        return cls(stat_only(registry_path(environ)), stat_only(operator_token_path(environ)))


def free_port() -> int:
    """An OS-assigned free loopback port, so the hub never takes a run hub's."""

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def robomate_executable() -> Path:
    name = "robomate.exe" if sys.platform == "win32" else "robomate"
    path = Path(sys.executable).with_name(name)
    if not path.exists():
        raise DriverError(f"{path} not found; run with `uv run --locked python {SCRIPT}`")
    return path


def _healthz(url: str) -> str | None:
    try:
        with urllib.request.urlopen(f"{url}/healthz", timeout=2) as response:
            data = json.load(response)
    except (OSError, ValueError, urllib.error.URLError):
        return None
    return str(data.get("hub_id")) if isinstance(data, dict) else None


@dataclass(slots=True)
class HubFacts:
    clone: str
    temp_dir: str
    port: int
    url: str = ""
    hub_id: str = ""
    pid: int | None = None
    state_dir: str = ""
    registry: str = ""
    registered: bool = False
    stopped: bool = False
    stop_detail: str = "not started"
    registry_stopped: bool | None = None


class IsolatedHub:
    """The script's own `robomate up`, stopped on every exit path."""

    def __init__(self, clone: Path, temp: Path, env: dict[str, str], port: int) -> None:
        self.clone = clone
        self.temp = temp
        self.env = env
        self.facts = HubFacts(clone=str(clone), temp_dir=str(temp), port=port)
        self.robomate = robomate_executable()
        self.process: subprocess.Popen[bytes] | None = None
        self.endpoint: HubEndpoint | None = None
        self.stdout_log = temp / "hub-stdout.log"
        self.stderr_log = temp / "hub-stderr.log"

    def __enter__(self) -> IsolatedHub:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.stop()

    def start(self, timeout_s: float = 60) -> HubEndpoint:
        # Files, not pipes: nothing reads the hub's output while it runs. The
        # hub writes them through its own stdio, so they are opened as bytes:
        # their encoding is the child's, which `self.env` sets (#164).
        with open(self.stdout_log, "wb") as out, open(self.stderr_log, "wb") as err:
            self.process = subprocess.Popen(
                [str(self.robomate), "up", "--port", str(self.facts.port)],
                cwd=self.clone,
                env=self.env,
                stdin=subprocess.DEVNULL,
                stdout=out,
                stderr=err,
            )
        deadline = time.monotonic() + timeout_s
        printed: dict[str, str] = {}
        while "ROBOMATE_TOKEN_FILE" not in printed:
            if self.process.poll() is not None:
                err = self.stderr_log.read_text(encoding="utf-8", errors=DECODE_ERRORS).strip()
                raise DriverError(f"hub exited early: {err}")
            if time.monotonic() > deadline:
                raise DriverError("hub did not start")
            time.sleep(0.1)
            for line in self.stdout_log.read_text(
                encoding="utf-8", errors=DECODE_ERRORS
            ).splitlines():
                key, sep, value = line.partition("=")
                if sep and key in ("ROBOMATE_HUB_URL", "ROBOMATE_TOKEN_FILE"):
                    printed[key] = value.strip()
        # The hub's own output names its state: the token file sits in it.
        self.facts.state_dir = str(Path(printed["ROBOMATE_TOKEN_FILE"]).parent)
        # Discovery in the clone, never a path the script builds (#54 moves
        # hub state out of the checkout). The environment has no explicit URL.
        discovery_env = {k: v for k, v in self.env.items() if k not in DISCOVERY_OVERRIDES}
        endpoint = discover(self.clone, discovery_env)
        if endpoint.url != printed["ROBOMATE_HUB_URL"].rstrip("/"):
            raise DriverError(f"discovered {endpoint.url}, but the hub printed another URL")
        hub_id = _healthz(endpoint.url)
        if hub_id is None:
            raise DriverError(f"{endpoint.url}/healthz does not answer")
        info = _plain_rpc(endpoint, "hub.info")
        if Path(str(info["repo_root"])).resolve() != self.clone.resolve():
            raise DriverError(f"{endpoint.url} serves {info['repo_root']}, not this clone")
        if int(info["port"]) != self.facts.port or info["hub_id"] != hub_id:
            raise DriverError(f"{endpoint.url} is not the hub this script started")
        self.endpoint = endpoint
        self.facts.url = endpoint.url
        self.facts.hub_id = hub_id
        self.facts.pid = int(info["pid"])
        registry = registry_path(self.env)
        self.facts.registry = str(registry)
        self.facts.registered = hub_id in _registry_ids(registry)
        print(f"Driver hub: {endpoint.url} (port {self.facts.port}, hub_id {hub_id})", flush=True)
        print(f"Driver hub state directory: {self.facts.state_dir}", flush=True)
        print(f"Driver hub registry: {registry}", flush=True)
        return endpoint

    def explicit_env(self) -> dict[str, str]:
        """The isolated environment, pinned to this hub by URL and token."""

        if self.endpoint is None:
            raise DriverError("the hub has not started")
        return self.env | {
            "ROBOMATE_HUB_URL": self.endpoint.url,
            "ROBOMATE_TOKEN": self.endpoint.token,
        }

    def run(self, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [str(self.robomate), *args],
            cwd=self.clone,
            env=self.explicit_env(),
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors=DECODE_ERRORS,
            timeout=60,
        )

    def stop(self) -> None:
        process = self.process
        if process is None:
            return
        steps: list[str] = []
        if process.poll() is None and self.endpoint is not None:
            try:
                down = self.run("down")
                steps.append(
                    f"`robomate down` exited {down.returncode}: "
                    f"{(down.stdout or down.stderr).strip()}"
                )
            except (OSError, subprocess.SubprocessError) as exc:
                steps.append(f"`robomate down` failed: {exc}")
        try:
            process.wait(timeout=30)
        except subprocess.TimeoutExpired:
            process.terminate()
            steps.append("terminated after `robomate down` did not stop it")
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=10)
                steps.append("killed")
        steps.append(f"hub process exited with code {process.returncode}")
        answering = self.facts.url and _healthz(self.facts.url) == self.facts.hub_id
        if self.facts.url:
            steps.append(
                f"{self.facts.url}/healthz still answers"
                if answering
                else f"{self.facts.url}/healthz no longer answers"
            )
        self.facts.stopped = not answering
        if self.facts.registry:
            # The registry keeps a stopped hub's entry, without a pid (#147).
            self.facts.registry_stopped = self.facts.hub_id not in _registry_ids(
                Path(self.facts.registry), running=True
            )
        self.facts.stop_detail = "; ".join(steps)
        self.process = None
        print(
            f"Driver hub {'stopped' if self.facts.stopped else 'NOT STOPPED'}: "
            f"{self.facts.stop_detail}",
            flush=True,
        )


def _registry_ids(path: Path, *, running: bool = False) -> set[str]:
    try:
        entries = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return set()
    return {
        str(entry.get("hub_id"))
        for entry in entries
        if isinstance(entry, dict) and (entry.get("pid") or not running)
    }


def _plain_rpc(endpoint: HubEndpoint, method: str) -> dict[str, Any]:
    request = urllib.request.Request(
        f"{endpoint.url}/rpc",
        data=json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": {}}).encode(),
        headers={"Authorization": f"Bearer {endpoint.token}", "Content-Type": "application/json"},
    )
    with urllib.request.urlopen(request, timeout=10) as response:
        body = json.load(response)
    if "error" in body:
        raise DriverError(f"{method} failed: {body['error']}")
    result: dict[str, Any] = body["result"]
    return result


def operator_ls(environ: Mapping[str, str], hub: HubFacts, cwd: Path) -> dict[str, Any]:
    """Run `robomate ls --json` in the caller's normal environment.

    It contacts every hub in the machine registry, so only the operator runs it.
    """

    listing = subprocess.run(
        [str(robomate_executable()), "ls", "--json"],
        cwd=cwd,
        env=child_env(environ),
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors=DECODE_ERRORS,
        timeout=60,
    )
    if listing.returncode != 0:
        raise DriverError(f"robomate ls failed: {listing.stderr.strip()}")
    entries = json.loads(listing.stdout)
    listed = any(
        entry.get("url") == hub.url or entry.get("repo_root") == hub.clone for entry in entries
    )
    print(
        f"Operator `robomate ls`: {len(entries)} hubs listed; driver's hub listed: {listed}",
        flush=True,
    )
    return {"hubs_listed": len(entries), "driver_hub_listed": listed}


# -- the parts the script plays ----------------------------------------------


class Orchestrator:
    """The orchestrator's `/rpc` calls, under the script's actor name."""

    def __init__(self, endpoint: HubEndpoint) -> None:
        self.session = str(uuid.uuid4())
        self.client = httpx.AsyncClient(
            base_url=endpoint.url,
            headers={
                "Authorization": f"Bearer {endpoint.token}",
                "X-Robomate-Actor": ORCHESTRATOR,
                "X-Robomate-Session": self.session,
            },
            timeout=180,
        )
        self.pending_ack: str | None = None

    async def aclose(self) -> None:
        await self.client.aclose()

    async def call(self, method: str, **params: Any) -> dict[str, Any]:
        request = {"jsonrpc": "2.0", "id": uuid.uuid4().hex, "method": method, "params": params}
        response = await self.client.post("/rpc", json=request)
        response.raise_for_status()
        body = response.json()
        if "error" in body:
            raise RpcRefused(method, body["error"].get("code"), str(body["error"]["message"]))
        result: dict[str, Any] = body["result"]
        return result

    async def refused(self, method: str, **params: Any) -> RpcRefused:
        try:
            await self.call(method, **params)
        except RpcRefused as exc:
            return exc
        raise DriverError(f"{method} was accepted; it must be refused")

    async def hold(self, timeout_s: float = 100) -> dict[str, Any] | None:
        """One `wait_for_event` hold, acking the event handled last."""

        params: dict[str, Any] = {"timeout_s": timeout_s}
        if self.pending_ack is not None:
            params["ack"] = self.pending_ack
        result = await self.call("wait_for_event", **params)
        self.pending_ack = None
        event: dict[str, Any] | None = result["event"]
        if event is not None:
            self.pending_ack = event["delivery_id"]
        return event

    async def next_event(
        self, kind: str, *, task_id: str | None = None, timeout_s: float = 120
    ) -> dict[str, Any]:
        """Hold until an event of `kind` arrives, acking the others it passes."""

        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            event = await self.hold(min(20.0, max(1.0, deadline - time.monotonic())))
            if event is None or event["kind"] != kind:
                continue
            if task_id is None or event["payload"].get("task_id") == task_id:
                return event
        raise DriverError(f"no {kind} event within {timeout_s:g} s")


def worker(endpoint: HubEndpoint, name: str) -> WorkerHubClient:
    return WorkerHubClient(
        WorkerSettings(hub_url=endpoint.url, token=endpoint.token, agent_name=name, profile=PROFILE)
    )


# -- forge -------------------------------------------------------------------


def run(args: Sequence[str], cwd: Path | None = None, input_text: str | None = None) -> str:
    completed = subprocess.run(
        list(args),
        cwd=cwd,
        env=child_env(os.environ),
        input=input_text,
        stdin=None if input_text is not None else subprocess.DEVNULL,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors=DECODE_ERRORS,
        check=False,
    )
    if completed.returncode != 0:
        detail = completed.stderr.strip() or completed.stdout.strip() or "no output"
        raise DriverError(f"{' '.join(args[:3])} failed ({completed.returncode}): {detail}")
    return completed.stdout.strip()


def clone_repository(repository: str, target: Path) -> None:
    """A fresh full clone over gh's configured git protocol, so pushes authenticate."""

    run(["gh", "repo", "clone", repository, str(target), "--", "--quiet"])


def gh_json(*args: str) -> Any:
    return json.loads(run(["gh", *args]))


def pr_head(pr_url: str) -> str:
    return run(["gh", "pr", "view", pr_url, "--json", "headRefOid", "--jq", ".headRefOid"])


# -- the record ---------------------------------------------------------------


@dataclass(slots=True)
class Step:
    number: int
    title: str
    played: str
    action: str
    hub: str
    forge: str
    passed: bool


@dataclass(slots=True)
class Record:
    run_id: str
    repository: str
    started: str
    robomate_commit: str
    platform: str = field(default_factory=platform.platform)
    hub: HubFacts | None = None
    machine_before: MachineState | None = None
    machine_after: MachineState | None = None
    operator_ls: dict[str, Any] | None = None
    steps: list[Step] = field(default_factory=list)
    facts: dict[str, Any] = field(default_factory=dict)
    audit: list[dict[str, Any]] = field(default_factory=list)
    decisions: list[dict[str, Any]] = field(default_factory=list)
    questions: list[dict[str, Any]] = field(default_factory=list)
    error: str | None = None

    def step(
        self, number: int, title: str, played: str, action: str, *, hub: str, forge: str = "—"
    ) -> None:
        self.steps.append(Step(number, title, played, action, hub, forge, True))
        print(f"Step {number} passed: {title}", flush=True)

    @property
    def machine_unchanged(self) -> bool | None:
        if self.machine_before is None or self.machine_after is None:
            return None
        return self.machine_before == self.machine_after

    @property
    def passed(self) -> bool:
        hub = self.hub
        return (
            self.error is None
            and [step.number for step in self.steps] == list(range(1, 11))
            and hub is not None
            and hub.stopped
            and bool(self.machine_unchanged)
            and not (self.operator_ls or {}).get("driver_hub_listed", False)
        )


def script(part: str) -> str:
    return f"script, as {part}"


# -- the ten steps -------------------------------------------------------------


async def scenario(
    hub: IsolatedHub, record: Record, temp: Path, operator_env: Mapping[str, str] | None
) -> None:
    endpoint = hub.endpoint
    assert endpoint is not None
    facts = record.facts
    run_id = record.run_id
    repository = record.repository
    record.step(
        1,
        "Start a hub on a fresh clone of the sandbox repository",
        script("operator"),
        f"`gh repo clone` into a new temporary directory; `robomate up --port {hub.facts.port}`",
        hub=(
            f"hub `{hub.facts.hub_id}` at {hub.facts.url}; registered only in the "
            f"throwaway registry: {hub.facts.registered}"
        ),
        forge=f"clone of `{repository}` at `{facts['base_sha']}`",
    )
    if operator_env is not None:
        record.operator_ls = operator_ls(operator_env, hub.facts, temp)

    async with AsyncExitStack() as stack:
        alice = Orchestrator(endpoint)
        stack.push_async_callback(alice.aclose)
        bob = await stack.enter_async_context(worker(endpoint, IMPLEMENTER))
        charlie = await stack.enter_async_context(worker(endpoint, REVIEWER))

        # Step 2: a seeded issue and the workflow over it.
        issue_url = run(
            [
                "gh",
                "issue",
                "create",
                "--repo",
                repository,
                "--title",
                f"Operator-channel scripted validation [{run_id}]",
                "--body-file",
                "-",
            ],
            input_text=(
                f"Seeded by `{SCRIPT}` in robomate for run `{run_id}` (robomate#133).\n\n"
                "A script, not an agent or the operator, performs every action on this "
                f"issue: it adds `runs/operator-channel-{run_id}.txt` and merges it.\n"
            ),
        )
        issue_number = int(issue_url.rstrip("/").rsplit("/", 1)[-1])
        facts["issue_url"] = issue_url
        workflow = await alice.call(
            "initialize_workflow",
            goal=f"Scripted operator-channel validation {run_id}: address {issue_url}",
            policy={"merge_method": "squash", "role_policy": {"reviewer_harness_differs": False}},
        )
        await bob.check_in()
        await charlie.check_in()
        checked_in: dict[str, int] = {}
        while len(checked_in) < 2:
            checkin = await alice.next_event("agent_checked_in")
            checked_in[checkin["payload"]["agent"]] = int(checkin["id"])
        state = await alice.call("get_state")
        record.step(
            2,
            "Start a workflow on a seeded sandbox issue",
            script(f"orchestrator `{ORCHESTRATOR}` and workers"),
            f"`gh issue create`; `initialize_workflow`; `{IMPLEMENTER}` and `{REVIEWER}` check in",
            hub=(
                f"workflow `{workflow['id']}` {state['workflow']['status']}; "
                f"agent_checked_in events {sorted(checked_in.values())}"
            ),
            forge=f"issue {issue_url}",
        )

        # Step 3: the implementer opens the PR.
        task = await alice.call(
            "assign_task",
            agent=IMPLEMENTER,
            role="implementer",
            title=f"IMPLEMENT sandbox#{issue_number}",
            instructions=f"Add runs/operator-channel-{run_id}.txt for {issue_url}.",
            event_id=checked_in[IMPLEMENTER],
        )
        assignment = await bob.await_assignment(timeout_s=30)
        if assignment.get("task_id") != task["id"]:
            raise DriverError(f"{IMPLEMENTER} received {assignment}, not task {task['id']}")
        guide = await bob.get_role_guide("implementer")
        workspace = temp / IMPLEMENTER
        clone_repository(repository, workspace)
        branch = f"operator-channel/{run_id}"
        marker = f"runs/operator-channel-{run_id}.txt"
        run(["git", "switch", "--quiet", "-c", branch], cwd=workspace)
        (workspace / marker).write_text(
            f"Scripted operator-channel validation run {run_id}.\n"
            f"Every action in this run was performed by {SCRIPT} (robomate#133).\n",
            encoding="utf-8",
        )
        run([sys.executable, *SANDBOX_TESTS], cwd=workspace)
        run(["git", "add", marker], cwd=workspace)
        run(
            [
                "git",
                "commit",
                "--quiet",
                "-m",
                f"Record scripted operator-channel run {run_id}",
                "-m",
                f"Committed by {SCRIPT}, playing the implementer.",
            ],
            cwd=workspace,
        )
        head = run(["git", "rev-parse", "HEAD"], cwd=workspace)
        run(["git", "push", "--quiet", "-u", "origin", branch], cwd=workspace)
        pr_url = run(
            [
                "gh",
                "pr",
                "create",
                "--repo",
                repository,
                "--base",
                "main",
                "--head",
                branch,
                "--title",
                f"Operator-channel scripted validation [{run_id}]",
                "--body-file",
                "-",
            ],
            input_text=(
                f"Closes #{issue_number}\n\n"
                f"Opened by `{SCRIPT}` playing implementer `{IMPLEMENTER}` on behalf of "
                "RoboNater. No agent or operator authored this change.\n"
            ),
        )
        if pr_head(pr_url) != head:
            raise DriverError(f"{pr_url} head is not the pushed {head}")
        await bob.submit_result(
            task["id"],
            ImplementerResult(
                outcome=ImplementerOutcome.COMPLETED,
                summary=f"Scripted: added {marker}",
                pr_url=pr_url,
                head_sha=head,
                commits=[head],
                tests=[TestResult(command=" ".join(["python", *SANDBOX_TESTS]), status="passed")],
            ),
        )
        completed = await alice.next_event("task_completed", task_id=task["id"])
        facts.update(pr_url=pr_url, head_sha=head, branch=branch)
        record.step(
            3,
            "Assign an implementer task. The PR is opened",
            script(f"orchestrator and implementer `{IMPLEMENTER}`"),
            "`assign_task`; `await_assignment`, `get_role_guide` "
            f"({len(guide)} bytes), push, `gh pr create`, `submit_result`",
            hub=f"task `{task['id']}` completed; task_completed event {completed['id']}",
            forge=f"{pr_url} at head `{head}`",
        )

        # Step 4: escalate, then hold on wait_for_event.
        question = (
            f"Scripted validation {run_id}: {pr_url} is open at head {head}. "
            "Approve review and merge?"
        )
        options = ["Approve: review and merge", "Hold: do not merge"]
        asked = await alice.call("ask_user", question=question, options=options)
        question_id = int(asked["question_id"])
        escalated = (await alice.call("get_state"))["workflow"]["status"]
        if escalated != "escalated":
            raise DriverError(f"workflow is {escalated} after ask_user")
        inbox = hub.run("inbox", "--json")
        listed = [q["question_id"] for q in json.loads(inbox.stdout or "[]")]
        if inbox.returncode != 0 or question_id not in listed:
            raise DriverError(f"robomate inbox does not list question {question_id}")
        held = asyncio.create_task(alice.hold(100))
        await asyncio.sleep(1)
        if held.done():
            raise DriverError(f"wait_for_event returned before the answer: {held.result()}")
        record.step(
            4,
            "The orchestrator calls ask_user and holds on wait_for_event",
            script(f"orchestrator `{ORCHESTRATOR}`"),
            "`ask_user` with two options; `wait_for_event(timeout_s=100)` left holding",
            hub=f"question {question_id}; workflow {escalated}; `robomate inbox` lists {listed}",
        )

        # Step 5: what escalation refuses.
        refusals = [
            await alice.refused("set_workflow_status", status="active", summary="Resume early"),
            await alice.refused(
                "assign_task",
                agent=REVIEWER,
                role="reviewer",
                title="REVIEW before the answer",
                instructions=f"Review {pr_url}",
                event_id=checked_in[REVIEWER],
                pr_head_sha=head,
            ),
            await alice.refused("check_merge_gate", pr_url=pr_url, expected_head_sha=head),
        ]
        for refusal in refusals:
            if refusal.code != CONFLICT:
                raise DriverError(f"expected CONFLICT ({CONFLICT}), got {refusal}")
        if held.done():
            raise DriverError("the hold ended during the refusals")
        record.step(
            5,
            "While escalated, set_workflow_status(active), assign_task and check_merge_gate "
            "are refused",
            script(f"orchestrator `{ORCHESTRATOR}`"),
            "the three calls, made while the hold is pending",
            hub="; ".join(f"`{r.method}` {r.code}: {r.message}" for r in refusals),
        )

        # Step 6: a worker holds only the bearer token.
        try:
            await bob._post_rpc(
                "hub.answer", {"question_id": question_id, "answer": options[0]}, path="/rpc"
            )
        except WorkerProtocolError as exc:
            worker_refusal = exc
        else:
            raise DriverError("hub.answer with the bearer token alone was accepted")
        if worker_refusal.code != OPERATOR_REQUIRED:
            raise DriverError(f"expected OPERATOR_REQUIRED, got {worker_refusal}")
        still = await bob.get_operator_answer(question_id)
        if still["status"] != "unanswered" or held.done():
            raise DriverError(f"question {question_id} changed after the refused answer: {still}")
        record.step(
            6,
            "A worker-side hub.answer with only the bearer token is refused",
            script(f"worker `{IMPLEMENTER}`"),
            "`hub.answer` on `/rpc` over the worker's client, without the operator credential",
            hub=(
                f"refused {worker_refusal.code}: {worker_refusal}; question {question_id} "
                f"still {still['status']}"
            ),
        )

        # Step 7: the operator answers.
        answered = await asyncio.to_thread(hub.run, "answer", str(question_id), "--option", "1")
        if answered.returncode != 0:
            raise DriverError(f"robomate answer failed: {answered.stderr.strip()}")
        record.step(
            7,
            "The operator answers with robomate answer",
            script("operator"),
            f"`robomate answer {question_id} --option 1` with the run's temporary operator token",
            hub=answered.stdout.strip(),
        )

        # Step 8: the answer arrives; resume; the reviewer confirms and approves.
        event = await asyncio.wait_for(held, 110)
        if (
            event is None
            or event["kind"] != "user_answered"
            or event["payload"].get("question_id") != question_id
            or event["payload"].get("answer") != options[0]
        ):
            raise DriverError(f"the hold returned {event}, not the answer")
        decision = await alice.call(
            "log_decision",
            summary=f"Operator approved {pr_url} (question {question_id})",
            rationale=f"user_answered event {event['id']}: {options[0]}",
            key=f"operator-answer-{question_id}",
        )
        await alice.call(
            "set_workflow_status",
            status="active",
            summary=f"Resumed after the operator answered question {question_id}",
        )
        review = await alice.call(
            "assign_task",
            agent=REVIEWER,
            role="reviewer",
            title=f"REVIEW {pr_url}",
            instructions=(
                f"Review {pr_url} at {head}. Confirm with get_operator_answer that the "
                f"operator approved question {question_id} before approving."
            ),
            event_id=int(event["id"]),
            pr_head_sha=head,
        )
        assignment = await charlie.await_assignment(timeout_s=30)
        if assignment.get("task_id") != review["id"] or assignment.get("pr_head_sha") != head:
            raise DriverError(f"{REVIEWER} received {assignment}, not review {review['id']}")
        guide = await charlie.get_role_guide("reviewer")
        confirmed = await charlie.get_operator_answer(question_id)
        if confirmed["status"] != "answered" or confirmed["answer"] != options[0]:
            raise DriverError(f"get_operator_answer shows {confirmed}")
        if pr_head(pr_url) != head:
            raise DriverError(f"{pr_url} moved off the head under review")
        review_url = run(
            [
                "gh",
                "pr",
                "comment",
                pr_url,
                "--body",
                f"Scripted reviewer `{REVIEWER}` ({SCRIPT}, run {run_id}) on behalf of "
                f"RoboNater: approved at {head}. The hub records the operator's answer to "
                f"question {question_id}: {options[0]!r}. No agent or person reviewed this.",
            ]
        )
        await charlie.submit_result(
            review["id"],
            ReviewerResult(
                verdict=ReviewerVerdict.APPROVED,
                summary="Scripted approval after confirming the operator's answer",
                pr_url=pr_url,
                review_url=review_url,
                reviewed_head_sha=head,
            ),
        )
        approved = await alice.next_event("task_completed", task_id=review["id"])
        facts["review_url"] = review_url
        record.step(
            8,
            "The orchestrator receives user_answered and resumes; the reviewer confirms the "
            "decision with get_operator_answer and approves",
            script(f"orchestrator `{ORCHESTRATOR}` and reviewer `{REVIEWER}`"),
            "the held `wait_for_event` returns; `log_decision`; `set_workflow_status(active)`; "
            f"`assign_task`; `get_role_guide` ({len(guide)} bytes); `get_operator_answer`; "
            "`gh pr comment`; `submit_result` approved",
            hub=(
                f"user_answered event {event['id']} for question {question_id}; decision "
                f"{decision['id']}; review task `{review['id']}` completed (event "
                f"{approved['id']})"
            ),
            forge=f"review comment {review_url} at `{head}`",
        )

        # Step 9: the gate passes; the PR merges bound to the approved head.
        gate: dict[str, Any] = {}
        for _ in range(GATE_ATTEMPTS):
            gate = await alice.call("check_merge_gate", pr_url=pr_url, expected_head_sha=head)
            if gate["ci"] in ("pending", "no_checks") or gate["mergeable"] == "unknown":
                await asyncio.sleep(2)
                continue
            break
        if not (
            gate.get("pr_state") == "open"
            and gate.get("head_matches")
            and gate.get("ci") == "pass"
            and gate.get("mergeable") == "clean"
            and not gate.get("base_behind_main")
        ):
            raise DriverError(f"the merge gate does not pass: {gate}")
        run(["gh", "pr", "merge", pr_url, "--squash", "--match-head-commit", head], cwd=temp)
        merged = gh_json("pr", "view", pr_url, "--json", "state,headRefOid,mergeCommit,mergedAt")
        if merged["state"] != "MERGED" or merged["headRefOid"] != head:
            raise DriverError(f"{pr_url} did not merge at {head}: {merged}")
        merge_sha = merged["mergeCommit"]["oid"]
        issue_state = ""
        for _ in range(15):
            issue_state = gh_json("issue", "view", issue_url, "--json", "state")["state"]
            if issue_state == "CLOSED":
                break
            await asyncio.sleep(2)
        await alice.call(
            "log_decision",
            summary=f"Merged {pr_url} at {head}",
            rationale=f"Gate passed; squash merge commit {merge_sha}",
            key=f"merged-{head}",
        )
        await alice.call("set_workflow_status", status="done", summary=f"Merged {pr_url}")
        for name, client in ((IMPLEMENTER, bob), (REVIEWER, charlie)):
            await alice.call("release_agent", agent=name)
            if (await client.await_assignment(timeout_s=10)).get("release") is not True:
                raise DriverError(f"{name} was not released")
        facts.update(merge_sha=merge_sha, merged_at=merged["mergedAt"], issue_state=issue_state)
        checks = ", ".join(f"{c['name']}={c['bucket']}" for c in gate["checks"])
        record.step(
            9,
            "The gate passes at the approved head; the PR merges bound to it",
            script(f"orchestrator `{ORCHESTRATOR}`"),
            f"`check_merge_gate`; `gh pr merge --squash --match-head-commit {head}`; "
            "`set_workflow_status(done)`; `release_agent` for both workers",
            hub=(
                f"gate: head_matches {gate['head_matches']}, ci {gate['ci']} ({checks}), "
                f"mergeable {gate['mergeable']}, base_behind_main {gate['base_behind_main']}"
            ),
            forge=(
                f"merged {merged['mergedAt']} as `{merge_sha}`, head `{merged['headRefOid']}`; "
                f"issue #{issue_number} {issue_state}"
            ),
        )

    # Step 10: every change names its actor.
    read_hub_rows(record, Path(hub.facts.state_dir))
    problems = audit_problems(record.audit, record.decisions, record.questions)
    if problems:
        raise DriverError("; ".join(problems))
    record.step(
        10,
        "Check the rpc_audit and decision rows: every change names its actor",
        script("operator"),
        "read the hub database read-only",
        hub=(
            f"{len(record.audit)} rpc_audit rows, {len(record.decisions)} decision rows, "
            f"{len(record.questions)} operator_question rows; every accepted call and every "
            "decision names its actor (tables below)"
        ),
    )


def read_hub_rows(record: Record, state_dir: Path) -> None:
    """Read the audit tables read-only, as scripts/hub-report.py does."""

    uri = (state_dir / "hub.db").resolve().as_uri() + "?mode=ro"
    with closing(sqlite3.connect(uri, uri=True)) as connection:
        connection.row_factory = sqlite3.Row
        record.audit = [
            dict(row)
            for row in connection.execute(
                "SELECT id, ts, actor, session, method, outcome FROM rpc_audit ORDER BY id"
            )
        ]
        record.decisions = [
            dict(row)
            for row in connection.execute(
                "SELECT id, ts, summary, rationale, key, actor, session FROM decision ORDER BY id"
            )
        ]
        record.questions = [
            dict(row)
            for row in connection.execute(
                "SELECT id, asked, actor, session, answer, answered, answered_by, resumed_by"
                " FROM operator_question ORDER BY id"
            )
        ]


def audit_problems(
    audit: Sequence[Mapping[str, Any]],
    decisions: Sequence[Mapping[str, Any]],
    questions: Sequence[Mapping[str, Any]],
) -> list[str]:
    """What step 10 requires of the rows; empty when every change names its actor."""

    problems = [
        f"rpc_audit row {row['id']} ({row['method']}) applied without an actor"
        for row in audit
        if row["outcome"] == "ok" and not row["actor"]
    ]
    problems += [
        f"decision row {row['id']} names no actor" for row in decisions if not row["actor"]
    ]

    def has(method: str, outcome: str, actor: str | None) -> bool:
        return any(
            row["method"] == method and row["outcome"] == outcome and row["actor"] == actor
            for row in audit
        )

    expected = [
        ("ask_user", "ok", ORCHESTRATOR),
        ("set_workflow_status", f"error {CONFLICT}", ORCHESTRATOR),
        ("assign_task", f"error {CONFLICT}", ORCHESTRATOR),
        ("check_merge_gate", f"error {CONFLICT}", ORCHESTRATOR),
        ("hub.answer", f"error {OPERATOR_REQUIRED}", None),
        ("hub.answer", "ok", "operator"),
        ("set_workflow_status", "ok", ORCHESTRATOR),
        ("check_merge_gate", "ok", ORCHESTRATOR),
    ]
    problems += [
        f"no rpc_audit row {method} {outcome} by {actor or 'an unidentified caller'}"
        for method, outcome, actor in expected
        if not has(method, outcome, actor)
    ]
    for row in audit:
        if row["actor"] == "operator" and row["session"] is not None:
            problems.append(f"rpc_audit row {row['id']} gives the operator a session")
    for question in questions:
        if question["actor"] != ORCHESTRATOR or question["answered_by"] != "operator":
            problems.append(f"operator question {question['id']} misattributed")
        if question["resumed_by"] is None:
            problems.append(f"operator question {question['id']} was never resumed")
    return problems


# -- evidence -----------------------------------------------------------------


def _cell(value: object) -> str:
    text = "—" if value is None or value == "" else str(value)
    return text.replace("|", "\\|").replace("\n", " ")


def render_evidence(record: Record) -> str:
    hub = record.hub
    lines = [
        f"**Scripted run: every action below was performed by `{SCRIPT}`, not by agents "
        "and not by the operator.** It played the orchestrator, both workers and the operator "
        "through the hub's real interfaces.",
        "",
        f"# Operator-channel validation, scripted run `{record.run_id}`",
        "",
        f"- Result: **{'PASSED' if record.passed else 'FAILED'}**",
        "- Issue: robomate#133; step list: "
        "[operator-channel-validation.md](../development/operator-channel-validation.md)",
        f"- Sandbox: `{record.repository}`",
        f"- Started: {record.started}",
        f"- robomate commit: `{record.robomate_commit}`",
        f"- Platform: `{record.platform}`",
        f"- Actor names the script used: orchestrator `{ORCHESTRATOR}`, implementer "
        f"`{IMPLEMENTER}`, reviewer `{REVIEWER}`; the hub records the operator channel as "
        "`operator`, and here the script used it too",
    ]
    if record.error:
        lines.append(f"- Error: {_cell(record.error)}")
    lines += [
        "",
        "## Steps",
        "",
        "| # | Step | Performed by | Action | Hub facts | Forge facts | Result |",
        "|---|---|---|---|---|---|---|",
    ]
    for step in record.steps:
        lines.append(
            f"| {step.number} | {_cell(step.title)} | {_cell(step.played)} | "
            f"{_cell(step.action)} | {_cell(step.hub)} | {_cell(step.forge)} | "
            f"{'pass' if step.passed else 'FAIL'} |"
        )
    done = {step.number for step in record.steps}
    for number in range(1, 11):
        if number not in done:
            lines.append(f"| {number} | not reached | script | — | — | — | FAIL |")
    lines += ["", "## Isolated hub", "", "| Fact | Value | Recorded by |", "|---|---|---|"]
    if hub is not None:
        for name, value in (
            ("Temporary directory", hub.temp_dir),
            ("Clone", hub.clone),
            ("Hub state directory", hub.state_dir),
            ("Port", hub.port),
            ("URL", hub.url),
            ("hub_id", hub.hub_id),
            ("pid", hub.pid),
            ("Registry (throwaway)", hub.registry),
            ("Registered in the throwaway registry", hub.registered),
            ("Registry records it stopped", hub.registry_stopped),
            ("Stopped", hub.stopped),
            ("Stop", hub.stop_detail),
        ):
            lines.append(f"| {name} | {_cell(value)} | script |")
    lines += [
        "",
        "## Machine state",
        "",
        "Compared by `os.stat` only; neither file is opened.",
        "",
        "| File | Before | After | Unchanged | Recorded by |",
        "|---|---|---|---|---|",
    ]
    before, after = record.machine_before, record.machine_after
    if before is not None:
        for label, first, second in (
            ("hubs.json", before.registry, after.registry if after else None),
            ("operator-token", before.operator_token, after.operator_token if after else None),
        ):
            lines.append(
                f"| {label} `{first.path}` | {first.describe()} | "
                f"{second.describe() if second else 'not captured'} | {first == second} | script |"
            )
    if record.operator_ls is None:
        lines += [
            "",
            "`robomate ls` in the operator's normal environment was not run (`--operator-ls` "
            "not given): it contacts every hub in the machine registry, so it is left to the "
            "operator's own rerun.",
        ]
    else:
        lines += [
            "",
            f"`robomate ls --json` in the caller's normal environment, run by the script "
            f"while its hub was up, listed {record.operator_ls['hubs_listed']} hubs; the "
            f"driver's hub listed: {record.operator_ls['driver_hub_listed']}.",
        ]
    lines += [
        "",
        "## rpc_audit",
        "",
        "| id | ts | actor | session | method | outcome | Performed by |",
        "|---|---|---|---|---|---|---|",
    ]
    for row in record.audit:
        lines.append(
            f"| {row['id']} | {row['ts']} | {_cell(row['actor'])} | {_cell(row['session'])} | "
            f"{row['method']} | {row['outcome']} | script |"
        )
    lines += [
        "",
        "A refused call made without the orchestrator headers, such as the worker's "
        "`hub.answer`, has no actor: the hub records only what the caller declared.",
        "",
        "## decision",
        "",
        "| id | ts | actor | session | summary | rationale | Performed by |",
        "|---|---|---|---|---|---|---|",
    ]
    for row in record.decisions:
        lines.append(
            f"| {row['id']} | {row['ts']} | {_cell(row['actor'])} | {_cell(row['session'])} | "
            f"{_cell(row['summary'])} | {_cell(row['rationale'])} | script |"
        )
    lines += [
        "",
        "## operator_question",
        "",
        "| id | asked | actor | answered | answered_by | resumed_by | Performed by |",
        "|---|---|---|---|---|---|---|",
    ]
    for row in record.questions:
        lines.append(
            f"| {row['id']} | {row['asked']} | {_cell(row['actor'])} | {_cell(row['answered'])}"
            f" | {_cell(row['answered_by'])} | {_cell(row['resumed_by'])} | script |"
        )
    lines += ["", "## Forge facts", "", "| Fact | Value | Recorded by |", "|---|---|---|"]
    for name, value in sorted(record.facts.items()):
        lines.append(f"| {name} | {_cell(value)} | script |")
    return "\n".join(lines) + "\n"


# -- main ---------------------------------------------------------------------


def _remove_tree(path: Path) -> None:
    def make_writable(function: Any, target: str, _exc: object) -> None:
        os.chmod(target, stat.S_IWRITE)
        for _ in range(5):
            try:
                function(target)
                return
            except PermissionError:
                time.sleep(0.1)
        function(target)

    shutil.rmtree(path, onexc=make_writable)


def robomate_commit() -> str:
    """The commit the driver ran from, marked when tracked files differ from it."""

    head = run(["git", "rev-parse", "HEAD"], cwd=ROOT)
    dirty = run(["git", "status", "--porcelain", "--untracked-files=no"], cwd=ROOT)
    return f"{head} (with uncommitted changes)" if dirty else head


def _interrupt(signum: int, _frame: object) -> None:
    raise KeyboardInterrupt(f"signal {signum}")


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0] if __doc__ else None)
    parser.add_argument("--repository", default=SANDBOX)
    parser.add_argument("--evidence-dir", type=Path, default=DEFAULT_EVIDENCE_DIR)
    parser.add_argument(
        "--operator-ls",
        action="store_true",
        help="also run `robomate ls` in your normal environment (operator only)",
    )
    parser.add_argument("--keep-temp", action="store_true", help="keep the temporary directory")
    args = parser.parse_args(argv)
    # A plain SIGTERM would skip the finally blocks that stop the hub.
    signal.signal(signal.SIGTERM, _interrupt)

    # On Windows, Git may use bundled MSYS2 OpenSSH which cannot find user keys/hosts
    # when HOME is overridden. Point GIT_SSH to Windows OpenSSH if not already set.
    if sys.platform == "win32" and "GIT_SSH" not in os.environ:
        system_root = os.environ.get("SYSTEMROOT", r"C:\Windows")
        system_ssh = Path(system_root) / "System32" / "OpenSSH" / "ssh.exe"
        if system_ssh.exists():
            os.environ["GIT_SSH"] = str(system_ssh)

    run_id = f"{datetime.now(UTC):%Y%m%d%H%M%S}_{uuid.uuid4().hex[:8]}"
    caller_env = dict(os.environ)
    record = Record(
        run_id=run_id,
        repository=args.repository,
        started=datetime.now(UTC).isoformat(timespec="seconds"),
        robomate_commit=robomate_commit(),
        platform=platform.platform(),
    )
    record.machine_before = MachineState.capture(caller_env)
    temp = Path(tempfile.mkdtemp(prefix="robomate-opchan-")).resolve()
    print(f"Run {run_id}; temporary directory {temp}", flush=True)
    env = isolated_env(caller_env, temp)
    clone = temp / args.repository.rsplit("/", 1)[-1]
    hub = IsolatedHub(clone, temp, env, free_port())
    record.hub = hub.facts
    try:
        with hub:
            clone_repository(args.repository, clone)
            record.facts["base_sha"] = run(["git", "rev-parse", "HEAD"], cwd=clone)
            hub.start()
            asyncio.run(scenario(hub, record, temp, caller_env if args.operator_ls else None))
    except (Exception, KeyboardInterrupt) as exc:
        record.error = f"{type(exc).__name__}: {exc}"
        traceback.print_exc()
    finally:
        record.machine_after = MachineState.capture(caller_env)
        args.evidence_dir.mkdir(parents=True, exist_ok=True)
        evidence = args.evidence_dir / f"operator-channel-scripted-{run_id}.md"
        evidence.write_text(render_evidence(record), encoding="utf-8")
        print(f"Evidence: {evidence}", flush=True)
        if args.keep_temp:
            print(f"Kept {temp}", flush=True)
        else:
            _remove_tree(temp)
    print(
        f"Hub state directory {hub.facts.state_dir}; port {hub.facts.port}; "
        f"stopped {hub.facts.stopped}; machine registry and operator token unchanged "
        f"{record.machine_unchanged}",
        flush=True,
    )
    print("PASSED" if record.passed else "FAILED", flush=True)
    return 0 if record.passed else 1


if __name__ == "__main__":
    sys.exit(main())
