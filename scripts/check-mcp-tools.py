#!/usr/bin/env python3
"""Tell whether the robomate MCP setup prompt needs re-running after an update.

The harness entries themselves don't change between robomate versions, but
Codex's per-tool approvals (and any Antigravity allow-list) must name every
tool the bridge serves. This script starts the bridge from a checkout, lists
its tools, and compares them with:

  1. the snapshot saved the last time you ran it with --save, and
  2. if Codex is configured, the [mcp_servers.robomate.tools.*] approval
     tables actually in its config.toml.

Interim until robomate install and harness profiles replace it in M3 (§9.1).
Remove this script then.

Usage (any OS):

    uv run --locked python scripts/check-mcp-tools.py [checkout]          # check
    uv run --locked python scripts/check-mcp-tools.py [checkout] --save   # record baseline
    uv run --locked python scripts/check-mcp-tools.py [checkout] --list   # print tools as JSON

Workflow: after running the setup prompt, run once with --save. After each
`git pull` of the checkout, run without --save. Exit status:

    0  tools unchanged; no need to re-run the prompt
    1  re-run the prompt, then run again with --save
    2  couldn't decide (no baseline yet, or the bridge failed to start)

It contacts no hub. It does start the bridge with `uv run --locked`, which
syncs the checkout's .venv, so run it only when no robomate run is active.
It doesn't notice changes to the prompt itself or to the settings a harness
needs; it only watches the tool list.
"""

from __future__ import annotations

import argparse
import datetime
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import tomllib
from pathlib import Path

PROBE_TIMEOUT_S = 180


def snapshot_path() -> Path:
    if os.name == "nt":
        base = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local")
    else:
        base = Path(os.environ.get("XDG_STATE_HOME") or Path.home() / ".local" / "state")
    return base / "robomate-mcp-check" / "snapshot.json"


def probe(checkout: Path, harness: str) -> tuple[dict, list[str]]:
    """Start the bridge, return (serverInfo, tool names). Contacts no hub."""
    env = {
        k: v
        for k, v in os.environ.items()
        if k != "ROBOMATE_HUB_URL" and not k.startswith("ROBOMATE_TOKEN")
    }
    cmd = [
        "uv",
        "run",
        "--locked",
        "--project",
        str(checkout),
        "robomate",
        "mcp",
        "--harness",
        harness,
    ]
    with (
        tempfile.TemporaryDirectory() as cwd,
        tempfile.TemporaryFile(mode="w+t", encoding="utf-8") as stderr,
    ):
        p = subprocess.Popen(
            cmd,
            cwd=cwd,
            env=env,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=stderr,
            text=True,
            encoding="utf-8",
        )
        watchdog = threading.Timer(PROBE_TIMEOUT_S, p.kill)
        watchdog.start()

        def send(message: dict) -> None:
            p.stdin.write(json.dumps(message) + "\n")
            p.stdin.flush()

        def receive() -> dict:
            for line in p.stdout:
                message = json.loads(line)
                if "id" in message:
                    return message
            stderr.seek(0)
            raise RuntimeError("bridge exited early; stderr:\n" + stderr.read()[-3000:])

        try:
            send(
                {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "initialize",
                    "params": {
                        "protocolVersion": "2025-06-18",
                        "capabilities": {},
                        "clientInfo": {"name": "robomate-mcp-check", "version": "0"},
                    },
                }
            )
            server = receive()["result"]["serverInfo"]
            send({"jsonrpc": "2.0", "method": "notifications/initialized"})
            send({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
            tools = [tool["name"] for tool in receive()["result"]["tools"]]
            return server, tools
        finally:
            watchdog.cancel()
            p.stdin.close()
            p.terminate()
            p.wait(timeout=15)


def git_head(checkout: Path) -> str | None:
    try:
        out = subprocess.run(
            ["git", "-C", str(checkout), "rev-parse", "--short", "HEAD"],
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    return out.stdout.strip() or None


def codex_config() -> Path:
    home = os.environ.get("CODEX_HOME")
    return (Path(home) if home else Path.home() / ".codex") / "config.toml"


def check_codex(checkout: Path, tools: set[str]) -> tuple[list[str], bool]:
    """Return (problems, checked) for Codex's robomate entry."""
    path = codex_config()
    if not path.exists():
        if shutil.which("codex") is not None:
            return [f"Codex: {path} has no [mcp_servers.robomate] entry"], True
        return [], False
    try:
        config = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as exc:
        return [f"Codex: can't parse {path}: {exc}"], True
    entry = config.get("mcp_servers", {}).get("robomate")
    if entry is None:
        if shutil.which("codex") is None:
            print(f"note: {path} has no robomate entry, and codex isn't on PATH; skipping")
            return [], False
        return [f"Codex: {path} has no [mcp_servers.robomate] entry"], True

    problems = []
    approved = {
        name
        for name, table in entry.get("tools", {}).items()
        if isinstance(table, dict) and table.get("approval_mode") == "approve"
    }
    if missing := sorted(tools - approved):
        problems.append('Codex: no approval_mode = "approve" for: ' + ", ".join(missing))
    if stale := sorted(approved - tools):
        problems.append(
            "Codex: approvals for tools the bridge no longer serves: " + ", ".join(stale)
        )

    args = [str(a) for a in entry.get("args", [])]
    project = args[args.index("--project") + 1] if "--project" in args[:-1] else None
    if project is None or (
        os.path.normcase(str(Path(project).expanduser().resolve()))
        != os.path.normcase(str(checkout))
    ):
        problems.append(f"Codex: entry runs --project {project}, not {checkout}")
    return problems, True


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument(
        "checkout",
        nargs="?",
        default=str(Path(__file__).resolve().parent.parent),
        help="robomate checkout the MCP entries run (default: script repository)",
    )
    options = parser.add_mutually_exclusive_group()
    options.add_argument(
        "--save",
        action="store_true",
        help="record the current tool list as the configured baseline",
    )
    options.add_argument(
        "--list",
        action="store_true",
        help="print the bridge's server info and tools as JSON, compare nothing",
    )
    parser.add_argument(
        "--snapshot",
        type=Path,
        default=snapshot_path(),
        help="snapshot file (default: %(default)s)",
    )
    parser.add_argument("--harness", default="codex", help="--harness passed to the bridge")
    ns = parser.parse_args()
    checkout = Path(os.path.expandvars(ns.checkout)).expanduser().resolve()
    if not (checkout / "pyproject.toml").is_file() or not (checkout / "uv.lock").is_file():
        print(f"error: {checkout} doesn't look like a robomate checkout", file=sys.stderr)
        return 2

    try:
        server, tool_list = probe(checkout, ns.harness)
    except Exception as exc:  # noqa: BLE001 - report anything and exit 2
        print(f"error: bridge probe failed: {exc}", file=sys.stderr)
        return 2
    if ns.list:
        print(
            json.dumps(
                {"server": server, "tool_count": len(tool_list), "tools": tool_list}, indent=2
            )
        )
        return 0
    head = git_head(checkout)
    print(f"checkout: {checkout} @ {head or '?'}")
    tools = set(tool_list)
    print(f"bridge: {server.get('name')} {server.get('version')}, {len(tools)} tools")

    try:
        store = json.loads(ns.snapshot.read_text(encoding="utf-8"))
    except FileNotFoundError:
        store = {}
    except (OSError, ValueError) as exc:
        print(f"error: can't read {ns.snapshot}: {exc}", file=sys.stderr)
        return 2
    key = str(checkout)

    if ns.save:
        store.setdefault("checkouts", {})[key] = {
            "server_version": server.get("version"),
            "git_head": head,
            "tools": sorted(tools),
            "saved_at": datetime.datetime.now().astimezone().isoformat(timespec="seconds"),
        }
        ns.snapshot.parent.mkdir(parents=True, exist_ok=True)
        tmp = ns.snapshot.with_suffix(".tmp")
        tmp.write_text(json.dumps(store, indent=2) + "\n", encoding="utf-8")
        os.replace(tmp, ns.snapshot)
        print(f"saved baseline to {ns.snapshot}")

    problems: list[str] = []
    saved = store.get("checkouts", {}).get(key)
    if saved is None:
        print(f"no baseline for this checkout in {ns.snapshot}")
    else:
        before = set(saved["tools"])
        print(
            f"baseline: {saved.get('server_version')} @ {saved.get('git_head')}, "
            f"saved {saved.get('saved_at')}"
        )
        if added := sorted(tools - before):
            problems.append("tools added since baseline: " + ", ".join(added))
        if removed := sorted(before - tools):
            problems.append("tools removed since baseline: " + ", ".join(removed))

    codex_problems, codex_checked = check_codex(checkout, tools)
    problems += codex_problems
    if not codex_checked:
        print(f"Codex config not checked ({codex_config()})")

    if problems:
        for problem in problems:
            print(f"  - {problem}")
        print("RE-RUN the setup prompt, then run this again with --save.")
        return 1
    if saved is None and not codex_checked:
        print(
            "UNKNOWN: nothing to compare against. If the prompt was just run for this "
            "version, run again with --save."
        )
        return 2
    print("OK: tool list unchanged; no need to re-run the setup prompt.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
