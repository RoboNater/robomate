#!/usr/bin/env python3
"""M5 acceptance run driver script (historical run record).

NOTE / RUN RECORD NOTICE:
This script is an unmaintained historical run record of acceptance run
`20261002_e8b76429` executed on 2026-10-02 against
`gitlab-box.local:RoboNater/robomate-glab-sandbox` for issue #76 / milestone M5.
It is NOT a maintained tool or part of production CI.

Important properties and limitations:
- Live side effects: Running this script interacts with live infrastructure,
  creating branches, pushing commits, opening MR !11 and !12, and merging them
  on gitlab-box.local:RoboNater/robomate-glab-sandbox.
- Execution environment: Hard-codes specific workspace paths (/home/alfred/...)
  and checkout directories. It cannot run outside this specific test machine.
- Static analysis: Located in docs/evidence/ and intentionally excluded from
  mypy's configured source files (tool.mypy files = ["packages", "tests"]).
- Execution nature: This script directly coordinated all git operations, hub
  JSON-RPC calls, WorkerHubClient A2A calls, and GitLab CLI/REST API calls.
  It passed hardcoded AgentProfile strings ("claude-code", "antigravity") in
  its client calls; no independent model or LLM agent harnesses were invoked.
- diverged_commits_count: During the run, line 944 fetched the MR without
  `?include_diverged_commits_count=true`. GitLab omitted the field from the
  response, so the Python fallback `.get("diverged_commits_count", 1)` recorded 1.
  The true git divergence between 08726cbd and 48369da3 was 2 commits (b1f6ecc and
  merge commit 48369da3).
- Post-run formatting: Long string literals and line wraps were reformatted to
  comply with ruff E501 (line-length 100), and unused imports were removed.
"""

import asyncio
import json
import logging
import re
import shutil
import subprocess
import sys
import time
import urllib.request
import uuid
from pathlib import Path
from typing import Any

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("m5-acceptance")

REPO_DIR = Path("/home/alfred/lw/w535-robomate/w535r-run-log/25-issue-76/bob").resolve()
BASE_DIR = Path("/home/alfred/lw/w535-robomate/w535r-run-log/25-issue-76").resolve()
SANDBOX_TARGET = BASE_DIR / "m5-sandbox"
RUN_DIR = BASE_DIR / "m5-acceptance"

sys.path.insert(0, str(REPO_DIR / "packages" / "common" / "src"))
sys.path.insert(0, str(REPO_DIR / "packages" / "hub" / "src"))
sys.path.insert(0, str(REPO_DIR / "packages" / "worker_mcp" / "src"))

from agent_hub_common import (  # noqa: E402
    AgentProfile,
    Finding,
    ImplementerResult,
    ModelSource,
    RebaseResult,
    ReviewerResult,
    TestResult,
    WorkflowPolicy,
)
from agent_hub_common.workspace import canonical_workspace, read_identity  # noqa: E402
from worker_mcp.client import WorkerHubClient  # noqa: E402
from worker_mcp.config import WorkerSettings  # noqa: E402


def run_cmd(
    cmd: list[str], cwd: Path | None = None, check: bool = True
) -> subprocess.CompletedProcess[str]:
    logger.info("Running: %s (cwd=%s)", " ".join(cmd), cwd or Path.cwd())
    res = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True)
    if check and res.returncode != 0:
        logger.error(
            "Command failed: %s\nStdout: %s\nStderr: %s", " ".join(cmd), res.stdout, res.stderr
        )
        raise RuntimeError(f"Command failed ({res.returncode}): {' '.join(cmd)}\n{res.stderr}")
    return res


def glab_api(
    path: str, method: str = "GET", fields: list[str] | None = None, check: bool = True
) -> Any:
    cmd = ["glab", "api", "--hostname", "gitlab-box.local"]
    if method != "GET":
        cmd.extend(["-X", method])
    if fields:
        for f in fields:
            cmd.extend(["-f", f])
    cmd.append(path)
    res = run_cmd(cmd, check=check)
    if not res.stdout.strip():
        return None
    try:
        return json.loads(res.stdout)
    except json.JSONDecodeError:
        return res.stdout


def rpc_call(
    url: str,
    token: str,
    method: str,
    params: dict[str, Any],
    actor: str = "alice",
    session_id: str | None = None,
) -> Any:
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "X-Robomate-Actor": actor,
        "X-Robomate-Session": session_id or str(uuid.uuid4()),
    }
    payload = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params}).encode(
        "utf-8"
    )
    req = urllib.request.Request(f"{url.rstrip('/')}/rpc", data=payload, headers=headers)
    with urllib.request.urlopen(req, timeout=30) as resp:
        data = json.load(resp)
    if "error" in data:
        raise RuntimeError(f"RPC {method} error: {data['error']}")
    return data["result"]


def wait_for_pipeline(sha: str, timeout_s: int = 120) -> tuple[int, str]:
    logger.info("Waiting for pipeline for SHA %s...", sha)
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        data = glab_api(f"projects/RoboNater%2Frobomate-glab-sandbox/pipelines?sha={sha}")
        if data and isinstance(data, list) and len(data) > 0:
            pipeline = data[0]
            pid = pipeline["id"]
            status = pipeline["status"]
            logger.info("Pipeline %s status: %s", pid, status)
            if status in ("success", "failed", "canceled"):
                return pid, status
        time.sleep(3)
    raise TimeoutError(f"Pipeline for {sha} timed out after {timeout_s}s")


def wait_for_mr_mergeable(iid: int, timeout_s: int = 60) -> dict[str, Any]:
    logger.info("Waiting for MR !%s to become mergeable...", iid)
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        mr = glab_api(f"projects/RoboNater%2Frobomate-glab-sandbox/merge_requests/{iid}")
        status = mr.get("detailed_merge_status") or mr.get("merge_status")
        logger.info(
            "MR !%s merge status: %s (detailed: %s)",
            iid,
            mr.get("merge_status"),
            mr.get("detailed_merge_status"),
        )
        if (
            status in ("mergeable", "can_be_merged")
            and mr.get("detailed_merge_status") != "checking"
        ):
            return mr
        time.sleep(2)
    raise TimeoutError(f"MR !{iid} did not become mergeable within {timeout_s}s")


def get_next_event(
    hub_url: str,
    hub_token: str,
    alice_session_id: str,
    expected_kind: str | None = None,
    ack: str | None = None,
    timeout_s: float = 30.0,
) -> dict[str, Any]:
    deadline = time.time() + timeout_s
    cur_ack = ack
    while time.time() < deadline:
        params: dict[str, Any] = {"timeout_s": 2}
        if cur_ack:
            params["ack"] = cur_ack
        res = rpc_call(
            hub_url, hub_token, "wait_for_event", params, actor="alice", session_id=alice_session_id
        )
        ev = res.get("event")
        if ev is not None:
            cur_ack = ev["delivery_id"]
            logger.info(
                "wait_for_event received event: kind=%s id=%s", ev.get("kind"), ev.get("id")
            )
            if expected_kind is None or ev.get("kind") == expected_kind:
                return ev
        time.sleep(1)
    raise TimeoutError(f"Timed out waiting for event (expected_kind={expected_kind})")


async def main() -> None:
    facts: dict[str, Any] = {}
    run_id = f"20261002_{uuid.uuid4().hex[:8]}"
    facts["run_id"] = run_id
    logger.info("Starting M5 Acceptance Run with ID: %s", run_id)

    # 1. Capture environment & versions
    git_head = run_cmd(["git", "-C", str(REPO_DIR), "rev-parse", "HEAD"]).stdout.strip()
    facts["robomate_commit"] = git_head

    glab_ver = run_cmd(["glab", "version"]).stdout.strip()
    facts["glab_version"] = glab_ver

    glab_api_ver = glab_api("version")
    facts["gitlab_version"] = glab_api_ver

    project_settings = glab_api("projects/RoboNater%2Frobomate-glab-sandbox")
    facts["project_settings"] = {
        "merge_method": project_settings.get("merge_method"),
        "squash_option": project_settings.get("squash_option"),
        "automatic_rebase_enabled": project_settings.get("automatic_rebase_enabled"),
        "auto_devops_enabled": project_settings.get("auto_devops_enabled"),
        "only_allow_merge_if_pipeline_succeeds": project_settings.get(
            "only_allow_merge_if_pipeline_succeeds"
        ),
        "allow_merge_on_skipped_pipeline": project_settings.get("allow_merge_on_skipped_pipeline"),
        "remove_source_branch_after_merge": project_settings.get(
            "remove_source_branch_after_merge"
        ),
    }
    facts["runner_settings"] = {
        "name": "runner01",
        "gitlab_runner_version": "19.4.1",
        "executor": "docker on Podman",
        "concurrent": 1,
        "memory_limit": "2 GiB",
        "cpu_limit": "2 CPUs",
        "pull_policy": ["always", "if-not-present"],
        "tags": "untagged",
    }

    # 2. Start robomate up in SANDBOX_TARGET
    if (SANDBOX_TARGET / ".robomate").exists():
        shutil.rmtree(SANDBOX_TARGET / ".robomate")

    hub_proc = subprocess.Popen(
        ["uv", "run", "--project", str(REPO_DIR), "robomate", "up"],
        cwd=SANDBOX_TARGET,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    up_lines: list[str] = []
    hub_url = ""
    for _ in range(50):
        line = hub_proc.stdout.readline()
        if not line:
            break
        up_lines.append(line.strip())
        logger.info("[Hub Up] %s", line.strip())
        if "Hub running at" in line:
            m = re.search(r"Hub running at (http[^\s]+)", line)
            if m:
                hub_url = m.group(1)
            break

    if not hub_url:
        hub_proc.kill()
        raise RuntimeError("Failed to start hub; no URL found in up output.")

    facts["up_preflight_lines"] = up_lines
    time.sleep(1)

    hub_json_path = SANDBOX_TARGET / ".robomate" / "hub.json"
    hub_token_path = SANDBOX_TARGET / ".robomate" / "token"
    hub_info = json.loads(hub_json_path.read_text(encoding="utf-8"))
    hub_token = hub_token_path.read_text(encoding="utf-8").strip()
    facts["hub_json"] = hub_info

    logger.info("Hub online at %s (pid=%s)", hub_url, hub_info.get("pid"))

    try:
        # 3. Run prepare-run.py
        if RUN_DIR.exists():
            shutil.rmtree(RUN_DIR)

        prep_cmd = [
            "uv",
            "run",
            "--project",
            str(REPO_DIR),
            "python",
            str(REPO_DIR / "scripts" / "prepare-run.py"),
            "--hub-repo",
            str(SANDBOX_TARGET),
            "--repository",
            "git@gitlab-box.local:RoboNater/robomate-glab-sandbox.git",
            "--run-dir",
            str(RUN_DIR),
            "--issue",
            "1",
            "--account",
            "RoboNater",
            "--merge-method",
            "merge",
            "--alice-harness",
            "opencode",
            "--alice-model",
            "opencode/muse-spark-1.3-contributor-free",
            "--alice-effort",
            "high",
            "--bob-harness",
            "antigravity",
            "--bob-model",
            "gemini-3.8-flash",
            "--bob-effort",
            "high",
            "--charlie-harness",
            "claude",
            "--charlie-model",
            "claude-opus-5-5",
            "--charlie-effort",
            "high",
        ]
        prep_res = run_cmd(prep_cmd)
        facts["prepare_run_output"] = prep_res.stdout

        # 4. Initialize Alice workflow
        alice_session_id = str(uuid.uuid4())
        facts["alice_session_id"] = alice_session_id
        policy = WorkflowPolicy(merge_method="merge", allow_no_ci=False)
        goal = (
            "Address issue #1 in RoboNater/robomate-glab-sandbox: "
            "Add maximum queue size limit to JobQueue"
        )
        rpc_call(
            hub_url,
            hub_token,
            "initialize_workflow",
            {"goal": goal, "policy": policy.model_dump()},
            actor="alice",
            session_id=alice_session_id,
        )
        logger.info("Alice initialized workflow with policy merge_method=merge")

        # 5. Set up Worker clients
        bob_ws = canonical_workspace(RUN_DIR / "bob")
        bob_id = read_identity(bob_ws, "bob")["workspace_id"]
        bob_settings = WorkerSettings(
            hub_url=hub_url,
            token=hub_token,
            agent_name="bob",
            workspace=bob_ws,
            telemetry_log=RUN_DIR / "bob-telemetry.jsonl",
            profile=AgentProfile(
                harness="antigravity",
                provider="google",
                model="gemini-3.8-flash",
                model_source=ModelSource.ENV,
                workspace_id=bob_id,
            ),
        )

        charlie_ws = canonical_workspace(RUN_DIR / "charlie")
        charlie_id = read_identity(charlie_ws, "charlie")["workspace_id"]
        charlie_settings = WorkerSettings(
            hub_url=hub_url,
            token=hub_token,
            agent_name="charlie",
            workspace=charlie_ws,
            telemetry_log=RUN_DIR / "charlie-telemetry.jsonl",
            profile=AgentProfile(
                harness="claude-code",
                provider="anthropic",
                model="claude-opus-5-5",
                model_source=ModelSource.ENV,
                workspace_id=charlie_id,
            ),
        )

        bob_client = WorkerHubClient(bob_settings)
        charlie_client = WorkerHubClient(charlie_settings)

        async with bob_client, charlie_client:
            await bob_client.check_in()
            ev_bob = get_next_event(
                hub_url, hub_token, alice_session_id, expected_kind="agent_checked_in"
            )
            await charlie_client.check_in()
            ev_charlie = get_next_event(
                hub_url,
                hub_token,
                alice_session_id,
                expected_kind="agent_checked_in",
                ack=ev_bob["delivery_id"],
            )
            logger.info(
                "Bob and Charlie checked in: ev_bob=%s ev_charlie=%s",
                ev_bob["id"],
                ev_charlie["id"],
            )
            last_event_id = ev_bob["id"]
            last_delivery_id = ev_charlie["delivery_id"]

            # 6. Task 1: IMPLEMENT
            task1 = rpc_call(
                hub_url,
                hub_token,
                "assign_task",
                {
                    "agent": "bob",
                    "role": "implementer",
                    "title": "IMPLEMENT for sandbox#1",
                    "instructions": "Implement max_size for JobQueue per issue #1",
                    "event_id": last_event_id,
                },
                actor="alice",
                session_id=alice_session_id,
            )
            t1_id = task1.get("task_id") or task1["id"]
            logger.info("Assigned IMPLEMENT task %s to bob", t1_id)

            t1_assign = await bob_client.await_assignment(timeout_s=5)
            assert t1_assign.get("task_id") == t1_id
            await bob_client.get_role_guide("implementer")
            await bob_client.report_progress(
                task_id=t1_id, note="Implementing max_size in JobQueue"
            )

            # Do the implementation in bob's clone
            branch_suffix = uuid.uuid4().hex[:6]
            branch_name = f"issue-1-queue-limit-{branch_suffix}"
            run_cmd(["git", "checkout", "-b", branch_name], cwd=bob_ws)

            # Edit robo_sandbox/queue.py
            queue_py = bob_ws / "robo_sandbox" / "queue.py"
            queue_code = (
                '"""An intentionally small in-memory queue that '
                'sandbox issues can extend."""\n\n'
                """from collections import deque
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True, slots=True)
class Job:
    \"\"\"A unit of work stored in the queue.\"\"\"

    job_id: str
    payload: Any


class JobQueue:
    \"\"\"A first-in, first-out collection with unique active job identifiers.\"\"\"

    def __init__(self, max_size: int | None = None) -> None:
        if max_size is not None and max_size <= 0:
            raise ValueError("max_size must be positive")
        self._max_size = max_size
        self._jobs: deque[Job] = deque()
        self._job_ids: set[str] = set()

    def submit(self, job: Job) -> None:
        \"\"\"Append a job, rejecting an identifier already in the queue.\"\"\"
        if not job.job_id:
            raise ValueError("job_id must not be empty")
        if job.job_id in self._job_ids:
            raise ValueError(f"job_id already queued: {job.job_id}")
        if self._max_size is not None and len(self._jobs) >= self._max_size:
            raise OverflowError("queue is full")
        self._jobs.append(job)
        self._job_ids.add(job.job_id)

    def take(self) -> Job | None:
        \"\"\"Remove and return the oldest job, or ``None`` when empty.\"\"\"
        if not self._jobs:
            return None
        job = self._jobs.popleft()
        self._job_ids.remove(job.job_id)
        return job

    def __len__(self) -> int:
        return len(self._jobs)
"""
            )
            queue_py.write_text(queue_code, encoding="utf-8")

            # Edit tests/test_queue.py
            test_queue_py = bob_ws / "tests" / "test_queue.py"
            test_code = """import unittest

from robo_sandbox import Job, JobQueue


class JobQueueTests(unittest.TestCase):
    def test_jobs_are_taken_in_submission_order(self) -> None:
        queue = JobQueue()
        first = Job("first", {"attempt": 1})
        second = Job("second", {"attempt": 2})

        queue.submit(first)
        queue.submit(second)

        self.assertEqual(queue.take(), first)
        self.assertEqual(queue.take(), second)
        self.assertIsNone(queue.take())

    def test_active_job_ids_are_unique(self) -> None:
        queue = JobQueue()
        queue.submit(Job("same", None))

        with self.assertRaisesRegex(ValueError, "already queued"):
            queue.submit(Job("same", None))

        queue.take()
        queue.submit(Job("same", "identifier can be reused after take"))
        self.assertEqual(len(queue), 1)

    def test_empty_job_id_is_rejected(self) -> None:
        queue = JobQueue()

        with self.assertRaisesRegex(ValueError, "must not be empty"):
            queue.submit(Job("", None))

    def test_max_size_overflow_raises_error(self) -> None:
        queue = JobQueue(max_size=2)
        queue.submit(Job("j1", 1))
        queue.submit(Job("j2", 2))
        with self.assertRaises(OverflowError):
            queue.submit(Job("j3", 3))

    def test_negative_max_size_rejected(self) -> None:
        with self.assertRaises(ValueError):
            JobQueue(max_size=-1)


if __name__ == "__main__":
    unittest.main()
"""
            test_queue_py.write_text(test_code, encoding="utf-8")

            run_cmd(["python3", "-m", "unittest", "discover", "-s", "tests", "-v"], cwd=bob_ws)
            run_cmd(["git", "add", "robo_sandbox/queue.py", "tests/test_queue.py"], cwd=bob_ws)
            run_cmd(
                [
                    "git",
                    "commit",
                    "-m",
                    "Add max_size parameter to JobQueue with capacity enforcement",
                ],
                cwd=bob_ws,
            )
            head1_sha = run_cmd(["git", "rev-parse", "HEAD"], cwd=bob_ws).stdout.strip()
            run_cmd(["git", "push", "-u", "origin", branch_name], cwd=bob_ws)

            # Create MR on GitLab
            mr_create_res = run_cmd(
                [
                    "glab",
                    "mr",
                    "create",
                    "-R",
                    "gitlab-box.local/RoboNater/robomate-glab-sandbox",
                    "--source-branch",
                    branch_name,
                    "--target-branch",
                    "main",
                    "--title",
                    f"Add maximum queue size limit to JobQueue ({run_id[:8]})",
                    "--description",
                    "Closes #1\n\nImplementation agent bob on behalf of RoboNater",
                    "--yes",
                ],
                cwd=bob_ws,
            )
            mr_url_match = re.search(
                r"(https://gitlab-box\.local/RoboNater/robomate-glab-sandbox/-/merge_requests/(\d+))",
                mr_create_res.stdout,
            )
            if not mr_url_match:
                raise RuntimeError(f"Could not parse MR URL from: {mr_create_res.stdout}")
            mr_url = mr_url_match.group(1)
            mr_iid = int(mr_url_match.group(2))
            logger.info("Created MR !%s: %s at head %s", mr_iid, mr_url, head1_sha)

            # Wait for CI on head1
            pipe1_id, pipe1_status = wait_for_pipeline(head1_sha)
            facts["pipeline1"] = {"id": pipe1_id, "status": pipe1_status, "sha": head1_sha}

            # Submit ImplementerResult
            r1 = ImplementerResult(
                outcome="completed",
                summary="Implemented max_size parameter and unit tests",
                pr_url=mr_url,
                head_sha=head1_sha,
                commits=[head1_sha],
                tests=[TestResult(command="python3 -m unittest discover -s tests -v", status="ok")],
            )
            await bob_client.submit_result(t1_id, r1)
            logger.info("Bob submitted IMPLEMENT result for head %s", head1_sha)

            # Alice waits for result event, acking previous event
            ev2 = get_next_event(
                hub_url,
                hub_token,
                alice_session_id,
                expected_kind="task_completed",
                ack=last_delivery_id,
            )
            last_event_id = ev2["id"]
            last_delivery_id = ev2["delivery_id"]
            logger.info("Received event for task 1 complete: id=%s", last_event_id)

            # 7. Alice Gate Check 1
            gate1 = rpc_call(
                hub_url,
                hub_token,
                "check_merge_gate",
                {"pr_url": mr_url, "expected_head_sha": head1_sha},
                actor="alice",
                session_id=alice_session_id,
            )
            facts["gate_check_1"] = gate1
            logger.info(
                "Gate check 1: ci=%s mergeable=%s behind=%s",
                gate1["ci"],
                gate1["mergeable"],
                gate1["base_behind_main"],
            )

            # 8. Task 2: REVIEW (changes_requested)
            task2 = rpc_call(
                hub_url,
                hub_token,
                "assign_task",
                {
                    "agent": "charlie",
                    "role": "reviewer",
                    "title": f"REVIEW round 1 for MR !{mr_iid}",
                    "instructions": f"Review MR !{mr_iid} at head {head1_sha}",
                    "event_id": last_event_id,
                    "pr_head_sha": head1_sha,
                },
                actor="alice",
                session_id=alice_session_id,
            )
            t2_id = task2.get("task_id") or task2["id"]
            logger.info("Assigned REVIEW task %s to charlie", t2_id)

            t2_assign = await charlie_client.await_assignment(timeout_s=5)
            assert t2_assign.get("task_id") == t2_id
            await charlie_client.get_role_guide("reviewer")

            # Charlie checks out head1_sha and runs tests
            run_cmd(["git", "fetch", "origin"], cwd=charlie_ws)
            run_cmd(["git", "checkout", head1_sha], cwd=charlie_ws)
            run_cmd(["python3", "-m", "unittest", "discover", "-s", "tests", "-v"], cwd=charlie_ws)

            # Charlie posts changes_requested note
            note_body = (
                "Reviewer agent charlie on behalf of RoboNater: finding r1-1 (blocking): "
                "Add an explicit test case confirming max_size=0 raises ValueError, "
                "not only negative numbers."
            )
            note_res = glab_api(
                f"projects/RoboNater%2Frobomate-glab-sandbox/merge_requests/{mr_iid}/notes",
                method="POST",
                fields=[f"body={note_body}"],
            )
            note_id = note_res["id"]
            note_url = f"{mr_url}#note_{note_id}"
            readback_note = glab_api(
                f"projects/RoboNater%2Frobomate-glab-sandbox/merge_requests/{mr_iid}/notes/{note_id}"
            )
            resolvable = readback_note.get("resolvable", False)
            facts["review_round_1_note"] = {
                "id": note_id,
                "url": note_url,
                "resolvable": resolvable,
                "finding_id": "r1-1",
            }
            logger.info("Charlie posted review note %s (resolvable=%s)", note_url, resolvable)

            rev_res1 = ReviewerResult(
                verdict="changes_requested",
                summary="Changes requested: explicit test for max_size=0 required",
                reviewed_head_sha=head1_sha,
                pr_url=mr_url,
                review_url=note_url,
                blocking_findings=[
                    Finding(
                        id="r1-1",
                        text=(
                            "Add an explicit test case confirming max_size=0 raises ValueError, "
                            "not only negative numbers."
                        ),
                    )
                ],
                nonblocking_findings=[],
                tests=[TestResult(command="python3 -m unittest discover -s tests -v", status="ok")],
            )
            await charlie_client.submit_result(t2_id, rev_res1)
            logger.info("Charlie submitted changes_requested result")

            # Alice waits for result event, acking previous event
            ev3 = get_next_event(
                hub_url,
                hub_token,
                alice_session_id,
                expected_kind="task_completed",
                ack=last_delivery_id,
            )
            last_event_id = ev3["id"]
            last_delivery_id = ev3["delivery_id"]
            logger.info("Received event for task 2 review complete: id=%s", last_event_id)

            # 9. Task 3: ADDRESS
            task3 = rpc_call(
                hub_url,
                hub_token,
                "assign_task",
                {
                    "agent": "bob",
                    "role": "implementer",
                    "title": f"ADDRESS findings for MR !{mr_iid}",
                    "instructions": f"Address finding r1-1 on MR !{mr_iid}",
                    "event_id": last_event_id,
                },
                actor="alice",
                session_id=alice_session_id,
            )
            t3_id = task3.get("task_id") or task3["id"]
            logger.info("Assigned ADDRESS task %s to bob", t3_id)

            t3_assign = await bob_client.await_assignment(timeout_s=5)
            assert t3_assign.get("task_id") == t3_id
            await bob_client.get_role_guide("implementer")

            # Bob addresses finding r1-1
            run_cmd(["git", "checkout", branch_name], cwd=bob_ws)
            test_queue_py = bob_ws / "tests" / "test_queue.py"
            target_method = (
                "    def test_negative_max_size_rejected(self) -> None:\n"
                "        with self.assertRaises(ValueError):\n"
                "            JobQueue(max_size=-1)\n"
            )
            replacement_methods = (
                "    def test_negative_max_size_rejected(self) -> None:\n"
                "        with self.assertRaises(ValueError):\n"
                "            JobQueue(max_size=-1)\n\n"
                "    def test_zero_max_size_rejected(self) -> None:\n"
                "        with self.assertRaises(ValueError):\n"
                "            JobQueue(max_size=0)\n"
            )
            test_code_v2 = test_code.replace(target_method, replacement_methods)
            test_queue_py.write_text(test_code_v2, encoding="utf-8")
            run_cmd(["python3", "-m", "unittest", "discover", "-s", "tests", "-v"], cwd=bob_ws)
            run_cmd(["git", "add", "tests/test_queue.py"], cwd=bob_ws)
            run_cmd(
                ["git", "commit", "-m", "Address review finding r1-1: test zero max_size"],
                cwd=bob_ws,
            )
            head2_sha = run_cmd(["git", "rev-parse", "HEAD"], cwd=bob_ws).stdout.strip()
            run_cmd(["git", "push", "origin", branch_name], cwd=bob_ws)

            pipe2_id, pipe2_status = wait_for_pipeline(head2_sha)
            facts["pipeline2"] = {"id": pipe2_id, "status": pipe2_status, "sha": head2_sha}

            addr_res = ImplementerResult(
                outcome="completed",
                summary="Addressed finding r1-1 by adding test_zero_max_size_rejected",
                pr_url=mr_url,
                head_sha=head2_sha,
                commits=[head2_sha],
                resolved_finding_ids=["r1-1"],
                tests=[TestResult(command="python3 -m unittest discover -s tests -v", status="ok")],
            )
            await bob_client.submit_result(t3_id, addr_res)
            logger.info("Bob submitted ADDRESS result for head %s", head2_sha)

            # Alice waits for result event, acking previous event
            ev4 = get_next_event(
                hub_url,
                hub_token,
                alice_session_id,
                expected_kind="task_completed",
                ack=last_delivery_id,
            )
            last_event_id = ev4["id"]
            last_delivery_id = ev4["delivery_id"]
            logger.info("Received event for task 3 address complete: id=%s", last_event_id)

            # 10. Alice Gate Check 2
            gate2 = rpc_call(
                hub_url,
                hub_token,
                "check_merge_gate",
                {"pr_url": mr_url, "expected_head_sha": head2_sha},
                actor="alice",
                session_id=alice_session_id,
            )
            facts["gate_check_2"] = gate2
            logger.info(
                "Gate check 2: ci=%s mergeable=%s behind=%s",
                gate2["ci"],
                gate2["mergeable"],
                gate2["base_behind_main"],
            )

            # 11. Task 4: REVIEW (round 2: approved)
            task4 = rpc_call(
                hub_url,
                hub_token,
                "assign_task",
                {
                    "agent": "charlie",
                    "role": "reviewer",
                    "title": f"REVIEW round 2 for MR !{mr_iid}",
                    "instructions": f"Re-review MR !{mr_iid} at head {head2_sha}",
                    "event_id": last_event_id,
                    "pr_head_sha": head2_sha,
                },
                actor="alice",
                session_id=alice_session_id,
            )
            t4_id = task4.get("task_id") or task4["id"]
            logger.info("Assigned REVIEW round 2 task %s to charlie", t4_id)

            t4_assign = await charlie_client.await_assignment(timeout_s=5)
            assert t4_assign.get("task_id") == t4_id
            await charlie_client.get_role_guide("reviewer")

            run_cmd(["git", "fetch", "origin"], cwd=charlie_ws)
            run_cmd(["git", "checkout", head2_sha], cwd=charlie_ws)
            run_cmd(["python3", "-m", "unittest", "discover", "-s", "tests", "-v"], cwd=charlie_ws)

            appr_note_body = (
                "Reviewer agent charlie on behalf of RoboNater: Approved. "
                f"Finding r1-1 is resolved and all unit tests pass at head {head2_sha}."
            )
            appr_note_res = glab_api(
                f"projects/RoboNater%2Frobomate-glab-sandbox/merge_requests/{mr_iid}/notes",
                method="POST",
                fields=[f"body={appr_note_body}"],
            )
            appr_note_id = appr_note_res["id"]
            appr_note_url = f"{mr_url}#note_{appr_note_id}"
            facts["review_round_2_approval_note"] = {
                "id": appr_note_id,
                "url": appr_note_url,
                "approved_head": head2_sha,
                "resolvable": False,
            }

            rev_res2 = ReviewerResult(
                verdict="approved",
                summary="Approved: finding r1-1 resolved, all tests passing",
                reviewed_head_sha=head2_sha,
                pr_url=mr_url,
                review_url=appr_note_url,
                blocking_findings=[],
                nonblocking_findings=[],
                tests=[TestResult(command="python3 -m unittest discover -s tests -v", status="ok")],
            )
            await charlie_client.submit_result(t4_id, rev_res2)
            logger.info("Charlie submitted approval result for head %s", head2_sha)

            # Alice waits for result event, acking previous event
            ev5 = get_next_event(
                hub_url,
                hub_token,
                alice_session_id,
                expected_kind="task_completed",
                ack=last_delivery_id,
            )
            last_event_id = ev5["id"]
            last_delivery_id = ev5["delivery_id"]
            logger.info("Received event for task 4 approval complete: id=%s", last_event_id)

            # 12. STALE-BASE DISTURBANCE (Operator lands unrelated change on main)
            main_before = glab_api(
                "projects/RoboNater%2Frobomate-glab-sandbox/repository/branches/main"
            )["commit"]["id"]
            facts["main_before_sha"] = main_before

            logger.info(
                "Operator landing unrelated change on main from issue-76-stale-base-unrelated..."
            )
            unrelated_mr = glab_api(
                "projects/RoboNater%2Frobomate-glab-sandbox/merge_requests",
                method="POST",
                fields=[
                    "source_branch=issue-76-stale-base-unrelated",
                    "target_branch=main",
                    "title=docs: note GitLab workflow exercises in README (stale base test)",
                ],
            )
            unrelated_iid = unrelated_mr["iid"]
            unrelated_head = unrelated_mr["sha"]
            logger.info("Unrelated MR !%s created at sha %s", unrelated_iid, unrelated_head)

            # Wait for MR to be mergeable in GitLab backend
            wait_for_mr_mergeable(unrelated_iid)

            # Merge unrelated MR
            merge_unrelated_res = run_cmd(
                [
                    "glab",
                    "mr",
                    "merge",
                    str(unrelated_iid),
                    "-R",
                    "gitlab-box.local/RoboNater/robomate-glab-sandbox",
                    "--sha",
                    unrelated_head,
                    "--auto-merge=false",
                    "--squash=false",
                    "--remove-source-branch",
                    "--yes",
                ]
            )
            logger.info(
                "Merged unrelated MR !%s: %s", unrelated_iid, merge_unrelated_res.stdout.strip()
            )
            time.sleep(4)

            main_after = glab_api(
                "projects/RoboNater%2Frobomate-glab-sandbox/repository/branches/main"
            )["commit"]["id"]
            facts["main_after_sha"] = main_after
            facts["unrelated_change"] = {
                "branch": "issue-76-stale-base-unrelated",
                "head": unrelated_head,
                "mr_iid": unrelated_iid,
            }
            logger.info("Main advanced from %s to %s", main_before, main_after)

            # 13. Alice Gate Check 3 (Stale base check)
            time.sleep(3)
            gate3 = rpc_call(
                hub_url,
                hub_token,
                "check_merge_gate",
                {"pr_url": mr_url, "expected_head_sha": head2_sha},
                actor="alice",
                session_id=alice_session_id,
            )
            mr_info_stale = glab_api(
                f"projects/RoboNater%2Frobomate-glab-sandbox/merge_requests/{mr_iid}"
            )
            diverged_count = mr_info_stale.get("diverged_commits_count", 1)
            facts["gate_check_3"] = gate3
            facts["stale_base_evidence"] = {
                "base_behind_main": gate3.get("base_behind_main"),
                "diverged_commits_count": diverged_count,
                "expected_head_sha": head2_sha,
            }
            logger.info(
                "Gate check 3 (stale base): behind=%s diverged_count=%s",
                gate3["base_behind_main"],
                diverged_count,
            )

            # 14. Task 5: REBASE
            task5 = rpc_call(
                hub_url,
                hub_token,
                "assign_task",
                {
                    "agent": "bob",
                    "role": "rebase",
                    "title": f"REBASE MR !{mr_iid}",
                    "instructions": f"Rebase MR !{mr_iid} onto new main",
                    "event_id": last_event_id,
                    "pr_head_sha": head2_sha,
                },
                actor="alice",
                session_id=alice_session_id,
            )
            t5_id = task5.get("task_id") or task5["id"]
            logger.info("Assigned REBASE task %s to bob", t5_id)

            t5_assign = await bob_client.await_assignment(timeout_s=5)
            assert t5_assign.get("task_id") == t5_id
            await bob_client.get_role_guide("rebase")

            # Bob rebases onto origin/main
            run_cmd(["git", "fetch", "origin"], cwd=bob_ws)
            run_cmd(["git", "rebase", "origin/main"], cwd=bob_ws)
            run_cmd(["python3", "-m", "unittest", "discover", "-s", "tests", "-v"], cwd=bob_ws)
            head3_sha = run_cmd(["git", "rev-parse", "HEAD"], cwd=bob_ws).stdout.strip()
            run_cmd(["git", "push", "origin", branch_name, "--force-with-lease"], cwd=bob_ws)
            logger.info("Bob rebased onto main; new head is %s", head3_sha)

            pipe3_id, pipe3_status = wait_for_pipeline(head3_sha)
            facts["pipeline3"] = {"id": pipe3_id, "status": pipe3_status, "sha": head3_sha}

            rebase_res = RebaseResult(
                outcome="completed",
                summary="Rebased onto main cleanly without conflict",
                pr_url=mr_url,
                head_sha=head3_sha,
                conflict_files=[],
                tests=[TestResult(command="python3 -m unittest discover -s tests -v", status="ok")],
            )
            await bob_client.submit_result(t5_id, rebase_res)
            logger.info("Bob submitted REBASE result with conflict_files=[]")

            # Alice waits for result event, acking previous event
            ev6 = get_next_event(
                hub_url,
                hub_token,
                alice_session_id,
                expected_kind="task_completed",
                ack=last_delivery_id,
            )
            last_event_id = ev6["id"]
            last_delivery_id = ev6["delivery_id"]
            logger.info("Received event for task 5 rebase complete: id=%s", last_event_id)

            # 15. Alice Gate Check 4 (Final gate check)
            time.sleep(3)
            gate4 = rpc_call(
                hub_url,
                hub_token,
                "check_merge_gate",
                {"pr_url": mr_url, "expected_head_sha": head3_sha},
                actor="alice",
                session_id=alice_session_id,
            )
            facts["gate_check_4"] = gate4
            logger.info(
                "Gate check 4: ci=%s mergeable=%s behind=%s",
                gate4["ci"],
                gate4["mergeable"],
                gate4["base_behind_main"],
            )

            # 16. Alice executes Merge bound to head3_sha
            wait_for_mr_mergeable(mr_iid)
            logger.info("Alice merging MR !%s bound to sha %s...", mr_iid, head3_sha)
            merge_res = run_cmd(
                [
                    "glab",
                    "mr",
                    "merge",
                    str(mr_iid),
                    "-R",
                    "gitlab-box.local/RoboNater/robomate-glab-sandbox",
                    "--sha",
                    head3_sha,
                    "--auto-merge=false",
                    "--squash=false",
                    "--remove-source-branch",
                    "--yes",
                ]
            )
            logger.info("glab mr merge output: %s", merge_res.stdout.strip())
            time.sleep(4)

            mr_final = glab_api(
                f"projects/RoboNater%2Frobomate-glab-sandbox/merge_requests/{mr_iid}"
            )
            facts["final_mr_state"] = {
                "state": mr_final.get("state"),
                "merge_commit_sha": mr_final.get("merge_commit_sha"),
                "squash_commit_sha": mr_final.get("squash_commit_sha"),
                "source_branch": branch_name,
                "target_branch": "main",
                "sha": mr_final.get("sha"),
            }
            logger.info(
                "Final MR state: %s, merge_commit_sha: %s",
                mr_final.get("state"),
                mr_final.get("merge_commit_sha"),
            )

            # Verify source branch deleted
            branch_check = glab_api(
                f"projects/RoboNater%2Frobomate-glab-sandbox/repository/branches/{branch_name}",
                check=False,
            )
            facts["source_branch_deleted"] = (
                branch_check is None
                or "404" in str(branch_check)
                or (isinstance(branch_check, dict) and "message" in branch_check)
            )
            logger.info("Source branch deleted: %s", facts["source_branch_deleted"])

            # 17. Alice marks workflow done and releases workers
            rpc_call(
                hub_url,
                hub_token,
                "set_workflow_status",
                {"status": "done", "summary": "M5 acceptance run completed successfully"},
                actor="alice",
                session_id=alice_session_id,
            )
            rpc_call(
                hub_url,
                hub_token,
                "release_agent",
                {"agent": "bob"},
                actor="alice",
                session_id=alice_session_id,
            )
            rpc_call(
                hub_url,
                hub_token,
                "release_agent",
                {"agent": "charlie"},
                actor="alice",
                session_id=alice_session_id,
            )
            bob_release = await bob_client.await_assignment(timeout_s=5)
            charlie_release = await charlie_client.await_assignment(timeout_s=5)
            facts["worker_releases"] = {"bob": bob_release, "charlie": charlie_release}
            logger.info("Workers released: bob=%s charlie=%s", bob_release, charlie_release)

    finally:
        # Shutdown hub
        logger.info("Stopping hub at %s...", hub_url)
        try:
            rpc_call(
                hub_url, hub_token, "hub.shutdown", {}, actor="alice", session_id=alice_session_id
            )
        except Exception as e:
            logger.warning("Hub shutdown exception: %s", e)
        time.sleep(1)
        if hub_proc.poll() is None:
            hub_proc.kill()

    # 18. Generate hub report
    logger.info("Generating hub report...")
    (RUN_DIR / "bob-telemetry.jsonl").touch(exist_ok=True)
    (RUN_DIR / "charlie-telemetry.jsonl").touch(exist_ok=True)
    hub_report_json = run_cmd(
        [
            "uv",
            "run",
            "--project",
            str(REPO_DIR),
            "python",
            str(REPO_DIR / "scripts" / "hub-report.py"),
            "--state-dir",
            str(SANDBOX_TARGET / ".robomate"),
            "--telemetry",
            str(RUN_DIR / "bob-telemetry.jsonl"),
            "--telemetry",
            str(RUN_DIR / "charlie-telemetry.jsonl"),
            "--format",
            "json",
        ]
    )
    facts["hub_report_json"] = json.loads(hub_report_json.stdout)

    hub_report_md = run_cmd(
        [
            "uv",
            "run",
            "--project",
            str(REPO_DIR),
            "python",
            str(REPO_DIR / "scripts" / "hub-report.py"),
            "--state-dir",
            str(SANDBOX_TARGET / ".robomate"),
            "--telemetry",
            str(RUN_DIR / "bob-telemetry.jsonl"),
            "--telemetry",
            str(RUN_DIR / "charlie-telemetry.jsonl"),
            "--format",
            "md",
        ]
    )
    facts["hub_report_md"] = hub_report_md.stdout

    # Save facts to json
    facts_file = BASE_DIR / f"m5-acceptance-{run_id}.json"
    facts_file.write_text(json.dumps(facts, indent=2), encoding="utf-8")
    logger.info("Saved acceptance facts to %s", facts_file)
    print(f"ACCEPTANCE_RUN_COMPLETE:{run_id}:{facts_file}")


if __name__ == "__main__":
    asyncio.run(main())
