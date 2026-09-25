#!/usr/bin/env python3
"""Cross-platform Step 6 runtime launchers and thin transport supervisors.

Usage: step6_launch.py {alice,bob,charlie} ABSOLUTE_RUN_DIR

In a networked (Step 7) run, ``bob`` launched from WSL starts this same
supervisor natively on Windows through interop, where RUN_DIR is Bob's
``prepare-run.py --worker-only`` run directory.

Each supervisor keeps one runtime process (and so one MCP child) alive and may
send only its fixed continuation text when the model ends a turn early. Alice
owns every assignment and workflow decision; workers pull work from the hub.
Native Windows needs these instead of the POSIX shell launchers.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import secrets
import socket
import sqlite3
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import step7
from run_common import executable
from step6 import SANDBOX, load_manifest, topology

HUB = "http://127.0.0.1:8420"
ALICE_CONTINUE = (
    "Continue orchestrating now. Reconcile from get_state and durable hub facts; never "
    "re-initialize. Keep going until the workflow is done or a rail requires a concrete "
    "operator question."
)
WORKER_CONTINUE = (
    "Continue the unattended worker loop now. If the current assignment has unfinished "
    "waiting or work, finish it and submit exactly one result before awaiting more work. "
    "Preserve path/URL text literally. Do not end before release."
)
BOB_TOOLS = (
    "Bash,Read,Edit,Write,TaskOutput,mcp__hub__check_in,mcp__hub__get_role_guide,"
    "mcp__hub__await_assignment,mcp__hub__report_progress,mcp__hub__ask_alice,"
    "mcp__hub__submit_result"
)
BOB_ALLOWED = "Read,Edit,Write,TaskOutput,Bash(git *),Bash(gh *),Bash(python3 *),mcp__hub__*"


def now():
    return datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def log(message):
    print(f"{now()} {message}", file=sys.stderr, flush=True)


def environment(manifest):
    env = os.environ.copy()
    env["PYTHONUTF8"] = "1"
    # Setting the implicit default relocates Claude's main configuration file.
    if not manifest.get("claude_config_dir_is_custom"):
        env.pop("CLAUDE_CONFIG_DIR", None)
    return env


def hub_healthy(url):
    try:
        with urllib.request.urlopen(url + "/healthz", timeout=2) as response:
            return response.status == 200
    except (OSError, urllib.error.URLError):
        return False


def workflow_status(directory):
    database = directory / "state" / "hub.db"
    if not database.exists():
        return None
    try:
        with contextlib.closing(sqlite3.connect(f"file:{database}?mode=ro", uri=True)) as db:
            row = db.execute("SELECT status FROM workflow").fetchone()
    except sqlite3.Error:
        return None
    return row[0] if row else None


class Telemetry:
    """Release observed by this launch's worker-mcp, ignoring older appended records."""

    def __init__(self, path):
        self.path = path
        self.start = path.stat().st_size if path.exists() else 0

    def released(self):
        if not self.path.exists():
            return False
        with self.path.open("rb") as stream:
            stream.seek(self.start)
            lines = stream.read().decode("utf-8", "replace").splitlines()
        for line in lines:
            try:
                record = json.loads(line)
            except ValueError:
                continue  # a record still being appended; read it next time
            if isinstance(record, dict) and record.get("outcome") == "release":
                return True
        return False


def stop_tree(process, grace=30):
    """Close stdin first so the runtime can stop its MCP child, then force the tree."""
    with contextlib.suppress(OSError, ValueError):
        if process.stdin:
            process.stdin.close()
    try:
        process.wait(grace)
    except subprocess.TimeoutExpired:
        if os.name == "nt":
            subprocess.run(["taskkill", "/T", "/F", "/PID", str(process.pid)], capture_output=True)
        else:
            process.kill()
        process.wait(10)


class AppServer:
    """Minimal JSON-RPC client for `codex app-server`; logs every message received."""

    def __init__(self, command, cwd, env, transcript, stderr):
        self.process = subprocess.Popen(
            command,
            cwd=cwd,
            env=env,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=stderr,
            text=True,
            encoding="utf-8",
            bufsize=1,
        )
        self.transcript = transcript
        self.lock = threading.Lock()
        self.responses = {}
        self.arrived = threading.Condition()
        self.turn_done = threading.Event()
        self.next_id = 0
        threading.Thread(target=self.read, daemon=True).start()

    def record(self, entry):
        with self.lock:
            self.transcript.write(json.dumps(entry) + "\n")
            self.transcript.flush()

    def send(self, message):
        self.record({"sent_at": now(), "message": message})
        with self.lock:
            self.process.stdin.write(json.dumps(message) + "\n")
            self.process.stdin.flush()

    def request(self, method, params, timeout=300):
        self.next_id += 1
        identifier = self.next_id
        self.send({"id": identifier, "method": method, "params": params})
        deadline = time.monotonic() + timeout
        with self.arrived:
            while identifier not in self.responses:
                if time.monotonic() > deadline or self.process.poll() is not None:
                    raise RuntimeError(f"{method}: no response from codex app-server")
                self.arrived.wait(1)
            response = self.responses.pop(identifier)
        if "error" in response:
            raise RuntimeError(f"{method}: {response['error']}")
        return response["result"]

    def answer(self, message):
        """Unattended: nothing reaches a human. Decline anything not pre-approved."""
        method = message["method"]
        if method.endswith("requestApproval") or method in ("execCommandApproval",):
            result = {"decision": "decline"}
        elif method == "mcpServer/elicitation/request":
            result = {"action": "decline"}
        elif method == "item/tool/requestUserInput":
            result = {"answers": {}}
        else:
            self.send({"id": message["id"], "error": {"code": -32601, "message": "unsupported"}})
            return
        log(f"declined server request {method}")
        self.send({"id": message["id"], "result": result})

    def read(self):
        for line in self.process.stdout:
            try:
                message = json.loads(line)
            except ValueError:
                self.record({"received_at": now(), "raw": line.rstrip("\n")})
                continue
            self.record({"received_at": now(), "message": message})
            if "method" in message and "id" in message:
                self.answer(message)
            elif "id" in message:
                with self.arrived:
                    self.responses[message["id"]] = message
                    self.arrived.notify_all()
            elif message.get("method") == "turn/completed":
                self.turn_done.set()
        self.turn_done.set()

    def turn(self, thread, text):
        self.turn_done.clear()
        self.request(
            "turn/start",
            {"threadId": thread, "input": [{"type": "text", "text": text, "text_elements": []}]},
        )


def alice(directory, manifest, max_turns, delay):
    if topology(manifest)[0]["alice"] != "codex":
        raise SystemExit("use scripts/launch-step6-alice.sh for interactive Claude Code Alice")
    hub = step7.local_hub(manifest, HUB)
    if hub_healthy(hub):
        raise SystemExit(f"{hub} occupied; leave other checkout listeners alone")
    env = environment(manifest)
    env["CODEX_HOME"] = str(directory / "codex-alice-home")
    runtime = directory / "alice-runtime"
    thread_file = directory / "alice.thread.json"
    with (
        (directory / "alice.transcript.jsonl").open("a", encoding="utf-8") as transcript,
        (directory / "alice.stderr").open("ab") as stderr,
    ):
        server = AppServer([executable("codex"), "app-server"], runtime, env, transcript, stderr)
        try:
            server.request(
                "initialize", {"clientInfo": {"name": "step6-supervisor", "version": "1"}}
            )
            server.send({"method": "initialized"})
            settings = {
                "model": manifest["models"]["alice"],
                "cwd": str(runtime),
                "sandbox": "workspace-write",
                "approvalPolicy": "on-request",
                "approvalsReviewer": "auto_review",
            }
            if thread_file.exists():
                # Restart: the durable hub goal/policy and the thread history are authoritative.
                thread = json.loads(thread_file.read_text())["thread_id"]
                server.request("thread/resume", {"threadId": thread, **settings})
                text = ALICE_CONTINUE
            else:
                thread = server.request("thread/start", settings)["thread"]["id"]
                thread_file.write_text(json.dumps({"thread_id": thread}) + "\n")
                text = (directory / "alice.prompt.md").read_text()
            log(f"alice thread {thread}")
            turns = 0
            while True:
                server.turn(thread, text)
                server.turn_done.wait()
                if server.process.poll() is not None:
                    raise SystemExit("codex app-server exited")
                status = workflow_status(directory)
                log(f"alice turn ended; workflow status {status}")
                if status == "done":
                    wait_for_worker_release(directory, manifest)
                    return
                if status in ("escalated", "paused"):
                    raise SystemExit(f"workflow {status}; operator attention required")
                if turns >= max_turns:
                    raise SystemExit(f"Alice ended {turns} continuation turns before done")
                turns += 1
                time.sleep(delay)
                log(f"sending Alice supervisor continuation {turns}")
                text = ALICE_CONTINUE
        finally:
            stop_tree(server.process)


def wait_for_worker_release(directory, manifest, timeout=900):
    """Keep the hub up until both workers observed release (or the deadline passes)."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        observed = []
        for name in ("bob", "charlie"):
            path = step7.telemetry_path(directory, manifest, name)
            text = path.read_text(encoding="utf-8") if path.exists() else ""
            observed.append('"outcome": "release"' in text)
        if all(observed):
            log("both workers observed release")
            return
        time.sleep(5)
    log("worker release not observed before the deadline")


def bob_on_windows(manifest, max_turns, delay):
    """From WSL: run this supervisor natively on Windows, in Bob's worker-only run directory."""
    windows = manifest["windows"]
    command, cwd = step7.windows_command(
        windows,
        [
            "uv",
            "run",
            "--locked",
            "python",
            "scripts/step6_launch.py",
            "bob",
            windows["run_dir"],
            "--max-turns",
            str(max_turns),
            "--delay",
            str(delay),
        ],
    )
    log(f"launching bob on Windows in {windows['run_dir']} through interop")
    returncode = subprocess.run(command, cwd=cwd).returncode
    if returncode:
        raise SystemExit(f"bob's Windows supervisor exited {returncode}")


def bob(directory, manifest, max_turns, delay):
    if step7.networked(manifest):
        return bob_on_windows(manifest, max_turns, delay)
    if manifest.get("worker_only"):
        config = directory / step7.WORKER_ONLY_CONFIG
        telemetry = Telemetry(directory / step7.WORKER_ONLY_TELEMETRY)
    else:
        config = directory / "bob.mcp.json"
        telemetry = Telemetry(directory / "bob.telemetry.jsonl")
    command = [
        executable("claude"),
        "-p",
        "--model",
        manifest["models"]["bob"],
        "--input-format",
        "stream-json",
        "--output-format",
        "stream-json",
        "--verbose",
        "--strict-mcp-config",
        "--mcp-config",
        str(config),
        "--permission-mode",
        "dontAsk",
        "--permission-prompts",
        "none",
        "--tools",
        BOB_TOOLS,
        "--allowedTools",
        BOB_ALLOWED,
    ]
    with (
        (directory / "bob.transcript.jsonl").open("a", encoding="utf-8") as transcript,
        (directory / "bob.stderr").open("ab") as stderr,
    ):
        process = subprocess.Popen(
            command,
            cwd=manifest["workspaces"]["bob"]["path"],
            env=environment(manifest),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=stderr,
            text=True,
            encoding="utf-8",
            bufsize=1,
        )

        def send(text):
            message = {
                "type": "user",
                "message": {"role": "user", "content": [{"type": "text", "text": text}]},
            }
            process.stdin.write(json.dumps(message) + "\n")
            process.stdin.flush()

        try:
            send((directory / "bob.prompt.md").read_text())
            turns = 0
            for line in process.stdout:
                transcript.write(line)
                transcript.flush()
                if '"type":"result"' not in line.replace(" ", ""):
                    continue
                if telemetry.released():
                    log(f"bob release observed after {turns} supervisor reprompt(s)")
                    return
                if turns >= max_turns:
                    raise SystemExit(f"bob ended {turns} turns without receiving release")
                turns += 1
                time.sleep(delay)
                log(f"bob ended before release; sending supervisor reprompt {turns}")
                send(WORKER_CONTINUE)
            if not telemetry.released():
                raise SystemExit("bob's Claude process exited before release")
        finally:
            stop_tree(process)


def opencode_binary():
    """The npm .cmd shim routes argv through cmd.exe, which mangles multi-line prompts."""
    found = executable("opencode")
    if found.lower().endswith(".cmd"):
        candidate = Path(found).parent / "node_modules/opencode-ai/bin/opencode.exe"
        if candidate.exists():
            return str(candidate)
    return found


def free_port():
    with contextlib.closing(socket.socket()) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def charlie(directory, manifest, max_turns, delay):
    if topology(manifest)[0]["charlie"] != "opencode":
        raise SystemExit("use scripts/launch-step6-charlie.sh for the Codex reviewer")
    telemetry = Telemetry(directory / "charlie.telemetry.jsonl")
    clone = manifest["workspaces"]["charlie"]["path"]
    binary = opencode_binary()
    env = environment(manifest)
    env["OPENCODE_CONFIG"] = str(directory / "charlie.opencode.json")
    env["OPENCODE_SERVER_PASSWORD"] = secrets.token_hex(16)
    port = free_port()
    url = f"http://127.0.0.1:{port}"
    # One persistent server owns the worker-mcp child across `run --attach` turns.
    with (
        (directory / "charlie.transcript.jsonl").open("a", encoding="utf-8") as transcript,
        (directory / "charlie.stderr").open("ab") as stderr,
    ):
        server = subprocess.Popen(
            [binary, "serve", "--hostname", "127.0.0.1", "--port", str(port)],
            cwd=clone,
            env=env,
            stdin=subprocess.PIPE,
            stdout=stderr,
            stderr=stderr,
        )
        try:
            deadline = time.monotonic() + 90
            while True:
                try:
                    urllib.request.urlopen(url, timeout=2).close()
                    break
                except urllib.error.HTTPError:
                    break
                except OSError:
                    if server.poll() is not None or time.monotonic() > deadline:
                        raise SystemExit("opencode serve did not start") from None
                    time.sleep(1)
            session = None
            text = (directory / "charlie.prompt.md").read_text()
            turns = 0
            while True:
                command = [binary, "run", "--attach", url, "--dir", clone, "--format", "json"]
                command += ["-m", manifest["models"]["charlie"]]
                if session:
                    command += ["--session", session]
                run = subprocess.Popen(
                    [*command, text],
                    cwd=clone,
                    env=env,
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.PIPE,
                    stderr=stderr,
                    text=True,
                    encoding="utf-8",
                )
                for line in run.stdout:
                    transcript.write(line)
                    transcript.flush()
                    if session is None:
                        with contextlib.suppress(ValueError, AttributeError):
                            session = json.loads(line).get("sessionID")
                run.wait()
                if telemetry.released():
                    log(f"charlie release observed after {turns} supervisor reprompt(s)")
                    return
                if turns >= max_turns:
                    raise SystemExit(f"charlie ended {turns} turns without receiving release")
                turns += 1
                time.sleep(delay)
                log(f"charlie ended before release (exit {run.returncode}); reprompt {turns}")
                text = WORKER_CONTINUE
        finally:
            stop_tree(server, grace=10)


def launch_manifest(directory, agent):
    """The Step 6 manifest, or on Bob's Windows host prepare-run's worker-only one.

    A worker-only run directory is reached only through ``bob_on_windows``,
    which has already checked the WSL run's seeded issue. Its clone targets the
    sandbox, or (no slug) the local clone of a ``--local-repository`` run.
    """
    value = json.loads((directory / "run.json").read_text(encoding="utf-8"))
    if value.get("worker_only") is None:
        manifest = load_manifest(directory)
        if not manifest.get("issue"):
            raise SystemExit("run has no seeded issue")
        return manifest
    if (
        agent != "bob"
        or value["worker_only"] != "bob"
        or value.get("slug") not in (SANDBOX, None)
        or not directory.is_absolute()
        or value.get("run_dir") != str(directory)
    ):
        raise SystemExit("a worker-only run directory launches only its own sandbox bob")
    return value


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("agent", choices=["alice", "bob", "charlie"])
    parser.add_argument("run_dir", type=Path)
    parser.add_argument("--max-turns", type=int, default=100)
    parser.add_argument("--delay", type=float, default=2)
    args = parser.parse_args()
    manifest = launch_manifest(args.run_dir, args.agent)
    {"alice": alice, "bob": bob, "charlie": charlie}[args.agent](
        args.run_dir, manifest, args.max_turns, args.delay
    )


if __name__ == "__main__":
    main()
