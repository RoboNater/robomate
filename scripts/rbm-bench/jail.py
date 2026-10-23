#!/usr/bin/env python3
"""Run one harness command inside the eval sandbox (bubblewrap, no network of its own).

    python3 scripts/rbm-bench/jail.py --run-dir RUN --agent bob -- claude -p ...

Used as the harness prefix in an eval run's start scripts, after the
launcher's ``--``, so the launcher and the robomate tooling stay outside and
only the harness (and everything it spawns) is confined:

- **Network:** a private network namespace with loopback only. 127.0.0.1:3128
  is forwarded to ``services.py``'s allowlisting egress proxy, set as
  ``HTTP(S)_PROXY``; git over SSH tunnels through it via ``connect.py``.
- **Files:** the root filesystem read-only, with ``/tmp``, ``/mnt``,
  ``/run/user`` and the whole home directory replaced by empty tmpfs. Only the
  harness binaries, credentials and caches listed below are bound back, plus
  the run directory itself (read-write). Session histories (Claude projects,
  Codex sessions, the OpenCode database) are per-run, per-agent directories,
  so earlier conversations and other runs are not visible.
- **Tooling:** ``uv run ... --project <tooling> robomate mcp`` is answered by
  a shim that relays to ``services.py``'s MCP broker, so the robomate checkout
  that runs the eval is not mounted.
- **Web tools:** turned off per harness (Claude Code ``--disallowedTools``,
  Codex ``web_search`` and browser features, OpenCode ``webfetch``/``websearch``
  permissions), since some of them run on the provider's side, past any proxy.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sqlite3
import sys
from pathlib import Path

TOOLING = Path(__file__).resolve().parents[2]
HERE = Path(__file__).resolve().parent
PROXY_PORT = 3128

HOME = Path.home()
#: Read-only binds under the home directory, when present.
HOME_RO = (
    ".local/bin",
    ".local/share/uv",
    ".local/share/claude",
    ".nvm",
    ".opencode",
    ".config/opencode",
    ".cache/opencode",
    ".config/glab-cli",
    ".certs",
    ".ssh",
    ".gitconfig",
)
#: Read-write binds under the home directory (shared caches and config).
HOME_RW = (".cache/uv", ".claude", ".claude.json", ".codex")
#: Per-agent replacements for history-bearing paths inside the binds above.
PRIVATE_DIRS = (
    ".claude/projects",
    ".claude/file-history",
    ".claude/plans",
    ".claude/sessions",
    ".claude/session-env",
    ".claude/shell-snapshots",
    ".claude/paste-cache",
    ".claude/debug",
    ".claude/jobs",
    ".claude/todos",
    ".claude/tasks",
    ".codex/sessions",
    ".codex/archived_sessions",
    ".codex/log",
    ".local/share/opencode",
)
PRIVATE_FILES = (".claude/history.jsonl", ".codex/history.jsonl", ".codex/history.json")
#: Credentials bound back into a per-agent replacement directory.
CREDENTIALS = (".local/share/opencode/auth.json",)
UNSET = ("GH_TOKEN", "GITHUB_TOKEN", "GH_ENTERPRISE_TOKEN", "SSH_AUTH_SOCK", "BROWSER")

CODEX_OFF = (
    'web_search="disabled"',
    "features.web_search_request=false",
    "features.web_search_cached=false",
    "features.standalone_web_search=false",
    "features.browser_use=false",
    "features.browser_use_external=false",
    "features.computer_use=false",
    "features.apps=false",
)
OPENCODE_OFF = {"permission": {"webfetch": "deny", "websearch": "deny"}}


def install_bin(run_dir: Path, tooling: Path) -> Path:
    """Write the in-sandbox helpers to ``<run>/jail/bin`` (idempotent)."""
    bin_dir = run_dir / "jail" / "bin"
    bin_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    for name in ("forward.py", "connect.py", "mcp_relay.py"):
        shutil.copyfile(HERE / "jail_bin" / name, bin_dir / name)
    real_uv = shutil.which("uv")
    if real_uv is None:
        raise SystemExit("jail: uv is not on PATH")
    shim = bin_dir / "uv"
    shim.write_text(
        "#!/bin/sh\n"
        "# Eval sandbox: robomate mcp from the tooling checkout goes to the broker.\n"
        f'if [ "$1 $2 $3 $4 $5 $6" = "run --locked --project {tooling} robomate mcp" ]; then\n'
        f'  exec python3 {bin_dir / "mcp_relay.py"} "$@"\n'
        "fi\n"
        f'exec {real_uv} "$@"\n',
        encoding="utf-8",
    )
    shim.chmod(0o755)
    return bin_dir


def seed_opencode_db(data_dir: Path) -> None:
    """Give a fresh per-agent OpenCode database the operator's login and nothing else.

    OpenCode keeps its console login (``account``, ``account_state``) in the
    same SQLite database as every past session, so the database cannot be
    bound in. A new one gets the real schema and migration records, so
    OpenCode treats it as current, and the two login tables' rows.
    """
    target = data_dir / "opencode.db"
    source = HOME / ".local/share/opencode/opencode.db"
    if target.exists() or not source.is_file():
        return
    real = sqlite3.connect(f"file:{source}?mode=ro", uri=True)
    fresh = sqlite3.connect(target)
    try:
        for (sql,) in real.execute(
            "SELECT sql FROM sqlite_master WHERE sql IS NOT NULL AND name NOT LIKE 'sqlite_%' "
            "ORDER BY CASE type WHEN 'table' THEN 0 ELSE 1 END"
        ):
            fresh.execute(sql)
        for table in ("migration", "data_migration", "account", "account_state"):
            rows = real.execute(f'SELECT * FROM "{table}"').fetchall()
            if rows:
                marks = ", ".join("?" * len(rows[0]))
                fresh.executemany(f'INSERT INTO "{table}" VALUES ({marks})', rows)
        fresh.commit()
    finally:
        real.close()
        fresh.close()
    target.chmod(0o600)


def harness_command(command: list[str]) -> list[str]:
    """``command`` with the harness's web tools turned off."""
    name = Path(command[0]).name
    if name == "claude":
        return [command[0], "--disallowedTools", "WebFetch", "WebSearch", *command[1:]]
    if name == "codex":
        extra = [word for setting in CODEX_OFF for word in ("-c", setting)]
        return [command[0], *extra, *command[1:]]
    return command


def bwrap_argv(run_dir: Path, agent: str, tooling: Path, command: list[str]) -> list[str]:
    jail = run_dir / "jail"
    bin_dir = install_bin(run_dir, tooling)
    private = jail / "home" / agent
    sock = jail / "sock"
    argv = [
        "bwrap",
        "--die-with-parent",
        "--unshare-net",
        "--unshare-pid",
        "--unshare-ipc",
        "--unshare-uts",
        "--hostname",
        f"eval-{agent}",
        "--ro-bind",
        "/",
        "/",
        "--dev",
        "/dev",
        "--proc",
        "/proc",
        "--tmpfs",
        "/tmp",
        "--tmpfs",
        "/var/tmp",
        "--tmpfs",
        "/mnt",
    ]
    run_user = Path(f"/run/user/{os.getuid()}")
    if run_user.is_dir():
        argv += ["--tmpfs", str(run_user)]
    argv += ["--tmpfs", str(HOME)]
    for rel in HOME_RO:
        if (HOME / rel).exists():
            argv += ["--ro-bind", str(HOME / rel), str(HOME / rel)]
    for rel in HOME_RW:
        if (HOME / rel).exists():
            argv += ["--bind", str(HOME / rel), str(HOME / rel)]
    for rel in PRIVATE_DIRS:
        target = HOME / rel
        if (HOME / rel.split("/")[0]).exists():
            source = private / rel.replace("/", "__")
            source.mkdir(parents=True, exist_ok=True, mode=0o700)
            if rel == ".local/share/opencode":
                seed_opencode_db(source)
            argv += ["--bind", str(source), str(target)]
    for rel in PRIVATE_FILES:
        target = HOME / rel
        if target.parent.exists():
            source = private / rel.replace("/", "__")
            source.touch(mode=0o600, exist_ok=True)
            argv += ["--bind", str(source), str(target)]
    for rel in CREDENTIALS:
        if (HOME / rel).is_file():
            argv += ["--ro-bind", str(HOME / rel), str(HOME / rel)]
    argv += ["--bind", str(run_dir), str(run_dir)]
    for key in UNSET:
        argv += ["--unsetenv", key]
    proxy = f"http://127.0.0.1:{PROXY_PORT}"
    env = {
        "PATH": f"{bin_dir}:{os.environ.get('PATH', '/usr/bin:/bin')}",
        "HTTPS_PROXY": proxy,
        "HTTP_PROXY": proxy,
        "https_proxy": proxy,
        "http_proxy": proxy,
        "ALL_PROXY": proxy,
        "NO_PROXY": "localhost,127.0.0.1,::1",
        "no_proxy": "localhost,127.0.0.1,::1",
        "EVAL_PROXY_PORT": str(PROXY_PORT),
        "EVAL_MCP_SOCKET": str(sock / "mcp.sock"),
        "EVAL_AGENT": agent,
        "GIT_SSH_COMMAND": f"ssh -o ProxyCommand='python3 {bin_dir / 'connect.py'} %h %p'",
        "OPENCODE_CONFIG_CONTENT": json.dumps(OPENCODE_OFF),
        "GH_CONFIG_DIR": str(private / "gh-config"),
    }
    for key, value in env.items():
        argv += ["--setenv", key, value]
    argv += [
        "--chdir",
        os.getcwd(),
        "--",
        "python3",
        str(bin_dir / "forward.py"),
        f"{PROXY_PORT}={sock / 'egress.sock'}",
        "--",
        *harness_command(command),
    ]
    return argv


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument("--agent", required=True)
    parser.add_argument("--tooling", type=Path, default=TOOLING)
    parser.add_argument("--print", action="store_true", help="print the bwrap argv and exit")
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)
    command = args.command[1:] if args.command[:1] == ["--"] else args.command
    if not command:
        parser.error("give the command to run after --")
    run_dir = args.run_dir.resolve()
    if not (run_dir / "jail" / "sock" / "egress.sock").exists():
        parser.error(f"services.py is not running for {run_dir} (no jail/sock/egress.sock)")
    full = bwrap_argv(run_dir, args.agent, args.tooling.resolve(), command)
    if args.print:
        print(json.dumps(full, indent=1))
        return
    sys.stdout.flush()
    os.execvp(full[0], full)


if __name__ == "__main__":
    main()
