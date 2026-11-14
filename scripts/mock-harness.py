#!/usr/bin/env python3
"""A scripted stand-in for a harness CLI, for start-run's agent-driven checks (#268).

Install shims named like the harness CLIs, put them first on ``PATH`` and start
a run; no model, forge or real harness is involved::

    python scripts/mock-harness.py --install DIR
    PATH="DIR:$PATH" uv run --project <robomate> python scripts/start-run.py ...
    $env:PATH = "DIR;$env:PATH"   # PowerShell

``agent-launcher.py`` starts a shim in the agent's worktree with its join line,
as it starts the real CLI. The shim answers ``--version`` like that CLI and
otherwise joins through its own ``robomate mcp --harness <h>`` bridge, as a
harness's static MCP entry does, then plays its standing role: Alice starts the
queued run, assigns bob an implementation and charlie its review, collects the
closeout reports, releases both and marks the run done; a worker takes each
assignment, submits a typed result, reports when asked and exits on release.
The bridge inherits the shim's environment, except that the Codex stand-in, like
Codex, forwards the identity variables only when ``-c mcp_servers.robomate.env.*``
names them. ``MOCK_HARNESS_LOG``, when set, receives one JSON line per launch.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import sys
import tomllib
import uuid
from pathlib import Path
from typing import Any

#: What each stand-in prints for ``--version`` (run_common.parse_harness_version reads it).
VERSIONS = {
    "claude-code": "2.1.999 (Claude Code)",
    "codex": "codex-cli 0.999.0",
    "opencode": "1.99.0",
    "antigravity": "1.99.0",
    "devin": "devin 3000.99.0 (mock)",
}
PROGRAMS = {"claude-code": "claude", "codex": "codex"}
IDENTITY = ("HUB_HARNESS", "HUB_HARNESS_VERSION", "HUB_PROVIDER", "HUB_MODEL", "HUB_EFFORT")
LOGGED = (*IDENTITY, "UV_CACHE_DIR", "PYTEST_XDIST_AUTO_NUM_WORKERS", "PWD", "VIRTUAL_ENV", "PATH")
SHA = "0123456789abcdef0123456789abcdef01234567"
HOLD_S = 5.0
DEADLINE_S = float(os.environ.get("MOCK_HARNESS_DEADLINE_S", "300"))
_JOIN = re.compile(r"Join robomate as ([a-z0-9_-]+), ([a-z]+)\.")
_TAKEOVER = re.compile(r'join\(name="([a-z0-9_-]+)", role="([a-z]+)", takeover=true\)')


def say(message: str) -> None:
    print(f"mock-harness: {message}", file=sys.stderr, flush=True)


def install(directory: Path) -> None:
    """Write ``claude`` and ``codex`` shims that run this file with this interpreter."""

    directory.mkdir(parents=True, exist_ok=True)
    script = Path(__file__).resolve()
    for harness, program in PROGRAMS.items():
        if os.name == "nt":
            path = directory / f"{program}.cmd"
            path.write_text(
                f'@"{sys.executable}" "{script}" --as {harness} %*\r\n', encoding="utf-8"
            )
        else:
            path = directory / program
            path.write_text(
                f"#!/bin/sh\nexec '{sys.executable}' '{script}' --as {harness} \"$@\"\n",
                encoding="utf-8",
            )
            path.chmod(0o755)
        print(path)


def prompt_of(harness: str, argv: list[str]) -> str:
    """The join line or continuation: Codex reads ``-`` from standard input, others end with it."""

    if harness == "codex" and argv and argv[-1] == "-":
        return sys.stdin.read()
    return argv[-1] if argv else ""


def codex_forwarded(argv: list[str]) -> dict[str, str]:
    """The ``-c mcp_servers.robomate.env.NAME=VALUE`` overrides, as Codex parses them."""

    forwarded: dict[str, str] = {}
    prefix = "mcp_servers.robomate.env."
    for flag, word in zip(argv, argv[1:], strict=False):
        if flag != "-c" or not word.startswith(prefix) or "=" not in word:
            continue
        key, _, raw = word[len(prefix) :].partition("=")
        try:
            value: Any = tomllib.loads(f"x = {raw}")["x"]
        except tomllib.TOMLDecodeError:
            value = raw
        forwarded[key] = str(value)
    return forwarded


def bridge_environment(harness: str, argv: list[str]) -> dict[str, str]:
    environment = dict(os.environ)
    if harness == "codex":
        for key in IDENTITY:
            environment.pop(key, None)
        environment |= codex_forwarded(argv)
    return environment


def text(result: Any) -> dict[str, Any]:
    content = "".join(getattr(item, "text", "") for item in result.content)
    if getattr(result, "isError", False):
        raise RuntimeError(content or "tool error")
    try:
        value = json.loads(content) if content else {}
    except ValueError:
        # A role guide or statement comes back as text.
        return {"text": content}
    return value if isinstance(value, dict) else {"value": value}


class Agent:
    def __init__(self, session: Any, name: str) -> None:
        self.session = session
        self.name = name
        self.deadline = asyncio.get_running_loop().time() + DEADLINE_S

    async def call(self, tool: str, **arguments: Any) -> dict[str, Any]:
        return text(await self.session.call_tool(tool, arguments))

    def check_time(self, what: str) -> None:
        if asyncio.get_running_loop().time() > self.deadline:
            raise TimeoutError(f"{self.name}: {what} not done within {DEADLINE_S:g} s")

    async def work(self) -> None:
        """A worker: typed results for each assignment, a report when asked, exit on release."""

        while True:
            self.check_time("release")
            assignment = await self.call("await_assignment", timeout_s=HOLD_S)
            if assignment.get("release") is True:
                say(f"{self.name} released")
                return
            if assignment.get("closeout_requested") is True:
                await self.call(
                    "submit_closeout_report",
                    run_id=str(assignment["run_id"]),
                    report={
                        "summary": f"{self.name} played its scripted role.",
                        "outstanding_items": [],
                        "lessons": [],
                        "claims": [],
                    },
                )
                continue
            if "task_id" not in assignment:
                continue
            task_id, role = str(assignment["task_id"]), str(assignment["role"])
            await self.call("get_role_guide", role=role)
            if assignment.get("statement_sha256"):
                await self.call("get_statement", sha256=str(assignment["statement_sha256"]))
            await self.call("report_progress", task_id=task_id, note=f"scripted {role} task")
            if role == "reviewer":
                result: dict[str, Any] = {
                    "verdict": "approved",
                    "summary": "Scripted approval.",
                    "reviewed_head_sha": assignment.get("pr_head_sha") or SHA,
                    "pr_url": "https://example.invalid/pull/1",
                }
            else:
                result = {
                    "outcome": "completed",
                    "summary": "Scripted implementation.",
                    "pr_url": "https://example.invalid/pull/1",
                    "head_sha": SHA,
                    "commits": [SHA],
                    "tests": [{"command": "scripted", "status": "passed"}],
                }
            await self.call("submit_result", task_id=task_id, result=result)
            say(f"{self.name} submitted {role} task {task_id}")

    async def orchestrate(self) -> None:
        """Alice: one implementation, one review, closeout, release, done."""

        started = await self.call("start_workflow")
        run_id = str(started.get("run_id") or "")
        say(f"alice started run {run_id} ({started.get('status')})")
        joined: dict[str, int] = {}
        implementation = review = ""
        implemented: tuple[int, str] | None = None
        reviewed = False
        ack: str | None = None
        while not reviewed:
            self.check_time("the review")
            delivered = await self.call("wait_for_event", timeout_s=HOLD_S, ack=ack)
            ack = None
            event = delivered.get("event")
            if isinstance(event, dict):
                ack = event.get("delivery_id")
                kind, payload = event.get("kind"), event.get("payload") or {}
                if kind == "agent_checked_in" and isinstance(payload.get("agent"), str):
                    joined[payload["agent"]] = int(event["id"])
                elif kind == "worker_question":
                    await self.call(
                        "reply",
                        task_id=str(payload["task_id"]),
                        text="Proceed.",
                        message_id=int(payload["message_id"]),
                    )
                elif kind in ("task_completed", "task_failed"):
                    if kind == "task_failed":
                        raise RuntimeError(f"task {payload.get('task_id')} failed")
                    if payload.get("task_id") == implementation:
                        result = payload.get("result") or {}
                        implemented = (int(event["id"]), str(result.get("head_sha") or SHA))
                    elif payload.get("task_id") == review:
                        reviewed = True
            if not implementation and "bob" in joined:
                task = await self.call(
                    "assign_task",
                    agent="bob",
                    role="implementer",
                    title=f"IMPLEMENT for run {run_id}",
                    instructions="Address the statement of work (scripted).",
                    event_id=joined["bob"],
                )
                implementation = str(task["id"])
            if implemented and not review and "charlie" in joined:
                task = await self.call(
                    "assign_task",
                    agent="charlie",
                    role="reviewer",
                    title=f"REVIEW for run {run_id}",
                    instructions="Review the change (scripted).",
                    event_id=implemented[0],
                    pr_head_sha=implemented[1],
                )
                review = str(task["id"])
        if ack:
            await self.call("wait_for_event", timeout_s=0, ack=ack)
        await self.closeout(run_id)
        for worker in ("bob", "charlie"):
            await self.call("release_agent", agent=worker)
        await self.call("set_workflow_status", status="done", summary="Scripted run complete.")
        say(f"alice marked run {run_id} done")

    async def closeout(self, run_id: str) -> None:
        await self.call("request_closeout_reports", run_id=run_id, timeout_s=60)
        ack: str | None = None
        while True:
            self.check_time("closeout")
            reports = await self.call("get_closeout_reports", run_id=run_id)
            waiting = [
                item
                for item in reports.get("missing") or []
                if item.get("author") in ("bob", "charlie")
                and item.get("reason") == "awaiting_report"
            ]
            if not waiting:
                break
            delivered = await self.call("wait_for_event", timeout_s=1, ack=ack)
            event = delivered.get("event")
            ack = event.get("delivery_id") if isinstance(event, dict) else None
        if ack:
            await self.call("wait_for_event", timeout_s=0, ack=ack)
        await self.call(
            "submit_closeout_report",
            run_id=run_id,
            report={
                "summary": "Alice orchestrated the scripted run.",
                "outstanding_items": [],
                "lessons": [],
                "claims": [],
            },
        )


async def play(harness: str, argv: list[str], line: str) -> None:
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    match = _TAKEOVER.search(line)
    takeover = match is not None
    match = match or _JOIN.search(line)
    if match is None:
        raise ValueError(f"no join line in the prompt: {line[:200]!r}")
    name, role = match.groups()
    params = StdioServerParameters(
        command=sys.executable,
        args=["-m", "robomate", "mcp", "--harness", harness],
        env=bridge_environment(harness, argv),
        cwd=os.getcwd(),
    )
    async with stdio_client(params) as (read, write), ClientSession(read, write) as session:
        await session.initialize()
        agent = Agent(session, name)
        joined = await agent.call("join", name=name, role=role, takeover=takeover)
        say(f"{name} joined as {joined.get('role', role)} (takeover={takeover})")
        if role == "orchestrator":
            await agent.orchestrate()
        else:
            await agent.work()


def main(argv: list[str]) -> int:
    if argv[:1] == ["--install"] and len(argv) == 2:
        install(Path(argv[1]))
        return 0
    if argv[:1] != ["--as"] or len(argv) < 2 or argv[1] not in VERSIONS:
        print("usage: mock-harness.py --install DIR | --as HARNESS ARGS...", file=sys.stderr)
        return 2
    harness, words = argv[1], argv[2:]
    if words == ["--version"]:
        print(VERSIONS[harness])
        return 0
    line = prompt_of(harness, words)
    if log := os.environ.get("MOCK_HARNESS_LOG"):
        record = {
            "harness": harness,
            "argv": words,
            "cwd": os.getcwd(),
            "env": {key: os.environ.get(key) for key in LOGGED},
            "bridge_env": {key: bridge_environment(harness, words).get(key) for key in IDENTITY},
        }
        with open(log, "a", encoding="utf-8") as stream:
            stream.write(json.dumps(record) + "\n")
    if harness == "codex":
        # Codex prints its session ID; the launcher records it for resume.
        print(f"session id: {uuid.uuid4()}", flush=True)
    try:
        asyncio.run(play(harness, words, line))
    except Exception as exc:
        say(f"failed: {exc!r}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
