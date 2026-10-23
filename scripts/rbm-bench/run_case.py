#!/usr/bin/env python3
"""Run one eval case end to end: seed, hub, prepare, sandboxed agents, collect.

    uv run --locked python scripts/rbm-bench/run_case.py CASE.json --label glm53 \\
        --bob-harness opencode --bob-model opencode-go/glm-5.3

Run it from the **tooling copy** (``~/robomate-eval/tooling``), not from a
checkout under ``~/lw``: the sandbox hides ``~/lw``, and the tooling directory
is what the MCP broker serves. Steps:

1. Seed a new GitLab project ``eval-<run id>`` from the case (``seed.py``).
2. Clone it to ``<eval-root>/runs/<run id>/hub`` and start its hub with the
   tooling's ``robomate up --forge gitlab``.
3. ``prepare-run.py`` renders the run. Re-fix cases use ``--issue N`` with the
   full Alice/bob/charlie workflow; re-review cases use a review-only
   statement, and this script plays a scripted Alice that assigns charlie one
   review of the MR at the case's head.
4. Each start script's harness runs through ``jail.py`` (no network but the
   egress proxy, no home directory but the harness's own files), with
   ``services.py`` running outside for the proxy and the MCP broker.
5. Wait for the workflow to finish or the time budget, stop everything, and
   collect ``hub-report.py`` output, the egress log, the forge state and, for
   re-fix, the diff of the new ``main``, into ``<eval-root>/results/<run id>/``.

Results and case files stay outside the run directory, which the agents see.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import os
import shutil
import signal
import sqlite3
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
from seed import HOST, glab, seed  # noqa: E402

TOOLING = Path(__file__).resolve().parents[2]
DEFAULT_EVAL_ROOT = Path.home() / "robomate-eval"
ACCOUNT = "RoboNater"
AGENTS = ("alice", "bob", "charlie")


def say(message: str) -> None:
    stamp = datetime.now(UTC).strftime("%H:%M:%S")
    print(f"[eval {stamp}] {message}", file=sys.stderr, flush=True)


def robomate(hub_dir: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["uv", "run", "--locked", "--project", str(TOOLING), "robomate", *args],
        cwd=hub_dir,
        capture_output=True,
        text=True,
    )


def hub_status(hub_dir: Path) -> dict[str, Any]:
    result = robomate(hub_dir, "status", "--json")
    try:
        data = json.loads(result.stdout)
    except ValueError:
        return {}
    if isinstance(data, list):
        data = data[0] if data else {}
    return data if isinstance(data, dict) else {}


def start_hub(hub_dir: Path, log: Path) -> tuple[subprocess.Popen[bytes], dict[str, Any]]:
    with log.open("ab") as out:
        process = subprocess.Popen(
            ["uv", "run", "--locked", "--project", str(TOOLING), "robomate", "up"]
            + ["--forge", "gitlab"],
            cwd=hub_dir,
            stdout=out,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
    deadline = time.monotonic() + 120
    while time.monotonic() < deadline:
        status = hub_status(hub_dir)
        if status.get("running") is not False and status.get("url") and status.get("token_file"):
            return process, status
        if process.poll() is not None:
            raise RuntimeError(f"robomate up exited {process.returncode}; see {log}")
        time.sleep(1)
    robomate(hub_dir, "down")
    stop(process, 30)
    raise RuntimeError(f"hub did not come up; see {log}")


def review_statement(case: dict[str, Any], record: dict[str, Any]) -> str:
    issue = case.get("issue")
    issue_line = (
        f"It implements issue #{issue['number']} ({record['web_url']}/-/issues/{issue['number']})."
        if issue
        else "It names no issue; the merge request description is the statement of work."
    )
    return (
        f"# Review merge request !{record['mr_iid']}\n\n"
        f"Review merge request {record['mr_url']} at head `{record['head']}`. {issue_line}\n\n"
        "This workflow is review only: one REVIEW task for the reviewer, no implementation, "
        "no merge.\n"
    )


def prepare(
    args: argparse.Namespace,
    case: dict[str, Any],
    record: dict[str, Any],
    run_dir: Path,
    hub_dir: Path,
    results: Path,
) -> None:
    command = [
        sys.executable,
        str(TOOLING / "scripts" / "prepare-run.py"),
        "--hub-repo",
        str(hub_dir),
        "--repository",
        record["ssh_url"],
        "--run-dir",
        str(run_dir),
        "--account",
        ACCOUNT,
        "--merge-method",
        "squash",
        "--allow-no-ci",
        "true",
        "--pytest-workers",
        str(args.pytest_workers),
        "--max-resumes",
        str(args.max_resumes),
    ]
    if case["kind"] == "refix":
        command += ["--issue", str(case["issue"]["number"])]
    else:
        statement = results / "statement.md"
        statement.write_text(review_statement(case, record), encoding="utf-8")
        command += ["--work-file", str(statement)]
    for agent in AGENTS:
        for option in ("harness", "model", "effort"):
            value = getattr(args, f"{agent}_{option}")
            if value:
                command += [f"--{agent}-{option}", value]
    with (results / "prepare-run.log").open("w", encoding="utf-8") as log:
        subprocess.run(command, cwd=TOOLING, stdout=log, stderr=subprocess.STDOUT, check=True)


def detach_tooling(run_dir: Path) -> None:
    """Replace links into the tooling checkout with copies; the sandbox cannot see it."""
    for path in sorted(run_dir.rglob("*")):
        if path.is_symlink() and TOOLING in Path(os.readlink(path)).resolve().parents:
            target = path.resolve()
            path.unlink()
            if target.is_dir():
                shutil.copytree(target, path)
            else:
                shutil.copy2(target, path)


def repoint_prompts(run_dir: Path) -> None:
    """Point prompts at the run's own skill copy; the tooling path is hidden in the sandbox."""
    skill = TOOLING / "skills" / "alice-orchestrator"
    copy = run_dir / "alice-runtime" / ".agents" / "skills" / "alice-orchestrator"
    if not copy.is_dir():
        copy = run_dir / "alice-runtime" / ".claude" / "skills" / "alice-orchestrator"
    for prompt in run_dir.glob("*.prompt.md"):
        text = prompt.read_text(encoding="utf-8")
        if str(skill) in text:
            prompt.write_text(text.replace(str(skill), str(copy)), encoding="utf-8")


def jail_scripts(run_dir: Path) -> list[str]:
    """Prefix each start/resume script's harness command with jail.py."""
    jail = TOOLING / "scripts" / "rbm-bench" / "jail.py"
    wrapped = []
    for agent in AGENTS:
        for kind in ("start", "resume"):
            script = run_dir / f"{kind}-{agent}.sh"
            if not script.exists():
                continue
            lines = script.read_text(encoding="utf-8").splitlines(keepends=True)
            done = False
            for index, line in enumerate(lines):
                if "-launcher.py" in line and " -- " in line:
                    prefix = f" -- python3 {jail} --run-dir {run_dir} --agent {agent} -- "
                    lines[index] = line.replace(" -- ", prefix, 1)
                    done = True
            if not done:
                raise RuntimeError(f"{script} has no launcher line to wrap")
            script.write_text("".join(lines), encoding="utf-8")
            wrapped.append(script.name)
    return wrapped


def leaks_of_tooling_path(run_dir: Path) -> list[str]:
    """Text files in the run directory that name the tooling path outside a known use."""
    found = []
    for path in run_dir.rglob("*"):
        if not path.is_file() or path.stat().st_size > 2_000_000 or "jail" in path.parts:
            continue
        if any(
            part in ("hub", "bob", "charlie", ".git")
            for part in path.relative_to(run_dir).parts[:1]
        ):
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        for line in text.splitlines():
            if str(TOOLING) in line and "--project" not in line and "-launcher.py" not in line:
                found.append(f"{path.relative_to(run_dir)}: {line.strip()[:160]}")
    return found


def workflow_status(state_dir: Path) -> str:
    db = state_dir / "hub.db"
    try:
        connection = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=5)
        try:
            row = connection.execute(
                "SELECT status FROM workflow ORDER BY created DESC LIMIT 1"
            ).fetchone()
        finally:
            connection.close()
    except sqlite3.Error:
        return "unknown"
    return row[0] if row else "none"


def launch(script: Path, log: Path) -> subprocess.Popen[bytes]:
    with log.open("ab") as out:
        return subprocess.Popen(
            ["bash", str(script)],
            cwd=script.parent,
            stdout=out,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            start_new_session=True,
        )


def stop(process: subprocess.Popen[bytes], grace_s: float = 30) -> None:
    if process.poll() is not None:
        return
    with contextlib.suppress(ProcessLookupError):
        os.killpg(process.pid, signal.SIGTERM)
    try:
        process.wait(grace_s)
    except subprocess.TimeoutExpired:
        with contextlib.suppress(ProcessLookupError):
            os.killpg(process.pid, signal.SIGKILL)
        process.wait()


# -- scripted Alice for re-review cases -------------------------------------------------


async def review_driver(
    case: dict[str, Any],
    record: dict[str, Any],
    hub: dict[str, Any],
    budget_s: float,
    log: Path,
) -> dict[str, Any]:
    """Assign charlie one review of the MR at the case head and wait for its result."""
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    env = {
        **os.environ,
        "ROBOMATE_HUB_URL": hub["url"],
        "ROBOMATE_TOKEN_FILE": hub["token_file"],
        "PYTHONUTF8": "1",
    }
    params = StdioServerParameters(
        command="uv",
        args=["run", "--locked", "--project", str(TOOLING), "robomate", "mcp"]
        + ["--role", "orchestrator"],
        env=env,
    )
    journal = log.open("a", encoding="utf-8")

    def note(**fields: Any) -> None:
        journal.write(json.dumps({"time": datetime.now(UTC).isoformat(), **fields}) + "\n")
        journal.flush()

    async def call(session: ClientSession, name: str, arguments: dict[str, Any]) -> Any:
        result = await session.call_tool(name, arguments)
        content: Any = result.structuredContent
        if content is None and result.content:
            text = getattr(result.content[0], "text", "")
            try:
                content = json.loads(text)
            except ValueError:
                content = text
        if result.isError:
            raise RuntimeError(f"{name} failed: {content}")
        return content

    issue = case.get("issue")
    issue_url = f"{record['web_url']}/-/issues/{issue['number']}" if issue else None
    instructions = (
        (f"Issue: {issue_url}\n" if issue_url else "")
        + f"Merge request: {record['mr_url']}\n"
        + "Acceptance criteria: the requirements and the Tests section of "
        + ("the issue" if issue_url else "the merge request description")
        + ".\n\n"
        + f"Please review and comment on {record['mr_url']} at {record['head']}. "
        + "Identify yourself in the PR comment as "
        + f'"Reviewer agent charlie on behalf of {ACCOUNT}". '
        + "Use a PR comment, not native approval.\n"
        + "Use finding IDs beginning with r1-."
    )
    outcome: dict[str, Any] = {"status": "timeout"}
    async with stdio_client(params) as (read, write), ClientSession(read, write) as session:
        await session.initialize()
        note(
            step="join",
            result=await call(session, "join", {"name": "alice", "harness": "eval-driver"}),
        )
        note(step="start_workflow", result=await call(session, "start_workflow", {}))
        deadline = time.monotonic() + budget_s
        ack: str | None = None
        task_id: str | None = None
        while time.monotonic() < deadline:
            arguments: dict[str, Any] = {"timeout_s": 60}
            if ack:
                arguments["ack"] = ack
            delivered = await call(session, "wait_for_event", arguments)
            event = delivered.get("event") if isinstance(delivered, dict) else None
            ack = None
            if not isinstance(event, dict):
                continue
            ack = event.get("delivery_id")
            kind, payload = event.get("kind"), event.get("payload") or {}
            note(
                step="event", kind=kind, payload={k: v for k, v in payload.items() if k != "result"}
            )
            if kind == "agent_checked_in" and payload.get("agent") == "charlie" and task_id is None:
                task = await call(
                    session,
                    "assign_task",
                    {
                        "agent": "charlie",
                        "role": "reviewer",
                        "title": f"REVIEW for {case['id']} @ {record['head'][:7]} r1-",
                        "instructions": instructions,
                        "event_id": event["id"],
                        "lease_min": 120,
                        "pr_head_sha": record["head"],
                    },
                )
                task_id = task.get("id") or (task.get("task") or {}).get("id")
                note(step="assigned", task=task_id)
            elif kind == "worker_question" and payload.get("task_id") == task_id:
                await call(
                    session,
                    "reply",
                    {
                        "task_id": task_id,
                        "text": "No further guidance is available for this review; "
                        "use your best judgment and record any assumption in the review.",
                        "message_id": payload["message_id"],
                    },
                )
            elif kind in ("task_completed", "task_failed") and payload.get("task_id") == task_id:
                outcome = {
                    "status": kind,
                    "result": payload.get("result"),
                    "summary": payload.get("summary"),
                }
                break
        if ack:
            await call(session, "wait_for_event", {"timeout_s": 1, "ack": ack})
        with contextlib.suppress(RuntimeError):
            await call(session, "release_agent", {"agent": "charlie"})
        with contextlib.suppress(RuntimeError):
            await call(
                session,
                "set_workflow_status",
                {"status": "done", "summary": f"eval review {outcome['status']}"},
            )
    journal.close()
    return outcome


# -- collection -----------------------------------------------------------------------------


def collect(
    case: dict[str, Any],
    record: dict[str, Any],
    run_dir: Path,
    hub: dict[str, Any],
    results: Path,
) -> dict[str, Any]:
    state_dir = Path(hub["state_dir"])
    report = subprocess.run(
        [sys.executable, str(TOOLING / "scripts" / "hub-report.py")]
        + ["--state-dir", str(state_dir), "--format", "json"],
        capture_output=True,
        text=True,
    )
    (results / "hub-report.json").write_text(report.stdout or report.stderr, encoding="utf-8")
    egress = run_dir / "jail" / "egress.jsonl"
    denied: dict[str, int] = {}
    if egress.exists():
        shutil.copy2(egress, results / "egress.jsonl")
        for line in egress.read_text(encoding="utf-8").splitlines():
            entry = json.loads(line)
            if entry.get("decision") == "deny" and entry.get("host"):
                key = f"{entry['host']}:{entry.get('port')}"
                denied[key] = denied.get(key, 0) + 1
    project = record["project"].replace("/", "%2F")
    mrs = glab("GET", f"projects/{project}/merge_requests?state=all&per_page=100")
    (results / "merge-requests.json").write_text(json.dumps(mrs, indent=1), encoding="utf-8")
    notes = {}
    for mr in mrs:
        notes[str(mr["iid"])] = glab(
            "GET", f"projects/{project}/merge_requests/{mr['iid']}/notes?per_page=100&sort=asc"
        )
    (results / "mr-notes.json").write_text(json.dumps(notes, indent=1), encoding="utf-8")
    heads = subprocess.run(
        ["git", "ls-remote", record["ssh_url"], "refs/heads/main"],
        capture_output=True,
        text=True,
    ).stdout.split()
    summary: dict[str, Any] = {
        "case": case["id"],
        "kind": case["kind"],
        "workflow_status": workflow_status(state_dir),
        "egress_denied": denied,
        "merge_requests": [
            {"iid": mr["iid"], "state": mr["state"], "sha": mr["sha"], "title": mr["title"]}
            for mr in mrs
        ],
        "main": heads[0] if heads else None,
    }
    if case["kind"] == "refix" and summary["main"] and summary["main"] != case["base"]:
        repo = results / "main.git"
        subprocess.run(["git", "clone", "-q", "--bare", record["ssh_url"], str(repo)], check=True)
        diff = subprocess.run(
            ["git", "-C", str(repo), "diff", case["base"], summary["main"]],
            capture_output=True,
            text=True,
        ).stdout
        (results / "main.diff").write_text(diff, encoding="utf-8")
        summary["diff_stat"] = subprocess.run(
            ["git", "-C", str(repo), "diff", "--shortstat", case["base"], summary["main"]],
            capture_output=True,
            text=True,
        ).stdout.strip()
    return summary


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("case", type=Path)
    parser.add_argument("--label", required=True, help="short candidate label for the run id")
    parser.add_argument("--eval-root", type=Path, default=DEFAULT_EVAL_ROOT)
    parser.add_argument("--budget-min", type=float, default=180)
    parser.add_argument("--pytest-workers", type=int, default=3)
    parser.add_argument("--max-resumes", type=int, default=3)
    for agent in AGENTS:
        for option in ("harness", "model", "effort"):
            parser.add_argument(f"--{agent}-{option}", default="")
    args = parser.parse_args(argv)
    if Path.home() / "lw" in TOOLING.parents:
        parser.error(f"run from the tooling copy, not {TOOLING} (the sandbox hides ~/lw)")

    case = json.loads(args.case.read_text(encoding="utf-8"))
    stamp = datetime.now(UTC).strftime("%m%d-%H%M")
    run_id = f"{case['id']}-{args.label}-{stamp}"
    run_dir = args.eval_root / "runs" / run_id
    results = args.eval_root / "results" / run_id
    results.mkdir(parents=True)
    run_dir.mkdir(parents=True)
    hub_dir = run_dir / "hub"
    config = {k: v for k, v in vars(args).items() if k not in ("case", "eval_root")}
    (results / "config.json").write_text(
        json.dumps({"run_id": run_id, "case": str(args.case), **config}, indent=1, default=str),
        encoding="utf-8",
    )

    say(f"seeding eval-{run_id} on {HOST}")
    record = seed(case, f"eval-{run_id}", args.eval_root / "source.git")
    (results / "seed.json").write_text(json.dumps(record, indent=1), encoding="utf-8")
    subprocess.run(["git", "clone", "-q", record["ssh_url"], str(hub_dir)], check=True)

    say("starting hub")
    hub_process, hub = start_hub(hub_dir, results / "hub.log")
    processes: dict[str, subprocess.Popen[bytes]] = {}
    services: subprocess.Popen[bytes] | None = None
    summary: dict[str, Any] = {}
    started = time.monotonic()
    try:
        say(f"hub at {hub['url']}; preparing run")
        summary["error"] = "interrupted"
        prepare(args, case, record, run_dir, hub_dir, results)
        detach_tooling(run_dir)
        repoint_prompts(run_dir)
        wrapped = jail_scripts(run_dir)
        leaks = leaks_of_tooling_path(run_dir)
        (results / "prepare-checks.json").write_text(
            json.dumps({"wrapped": wrapped, "tooling_path_mentions": leaks}, indent=1),
            encoding="utf-8",
        )
        with (results / "services.log").open("ab") as out:
            services = subprocess.Popen(
                [sys.executable, str(TOOLING / "scripts" / "rbm-bench" / "services.py")]
                + ["--run-dir", str(run_dir), "--hub-url", hub["url"]]
                + ["--token-file", hub["token_file"]],
                stdout=out,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
        while not (run_dir / "jail" / "sock" / "mcp.sock").exists():
            if services.poll() is not None:
                raise RuntimeError("services.py exited; see services.log")
            time.sleep(0.2)

        budget_s = args.budget_min * 60
        if case["kind"] == "refix":
            for agent in AGENTS:
                say(f"launching {agent}")
                processes[agent] = launch(run_dir / f"start-{agent}.sh", results / f"{agent}.log")
                time.sleep(5)
            while time.monotonic() - started < budget_s:
                status = workflow_status(Path(hub["state_dir"]))
                if status in ("done", "escalated"):
                    say(f"workflow {status}")
                    break
                if all(p.poll() is not None for p in processes.values()):
                    say("every agent exited")
                    break
                time.sleep(30)
            else:
                say("time budget spent")
        else:
            say("launching charlie and the scripted review driver")
            processes["charlie"] = launch(run_dir / "start-charlie.sh", results / "charlie.log")
            summary["review"] = asyncio.run(
                review_driver(case, record, hub, budget_s, results / "driver.jsonl")
            )
            say(f"review {summary['review']['status']}")
            time.sleep(20)
        del summary["error"]
    except Exception as exc:
        summary["error"] = repr(exc)
        raise
    finally:
        summary["elapsed_min"] = round((time.monotonic() - started) / 60, 1)
        for agent, process in processes.items():
            say(f"stopping {agent}")
            stop(process)
        try:
            summary |= collect(case, record, run_dir, hub, results)
        except Exception as exc:  # collection must not keep the hub running
            summary["collect_error"] = repr(exc)
        say("stopping hub")
        robomate(hub_dir, "down")
        stop(hub_process, 60)
        if services is not None:
            stop(services, 5)
        (results / "summary.json").write_text(json.dumps(summary, indent=1), encoding="utf-8")
        say(f"results in {results}")
        print(json.dumps(summary, indent=1))


if __name__ == "__main__":
    main()
