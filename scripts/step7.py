#!/usr/bin/env python3
"""Step 7 networked topology for the Step 6 harness (#140).

The hub, Alice and Charlie run in WSL2. Bob runs natively on the Windows host
and dials the hub at WSL's ``eth0`` address across the Hyper-V vSwitch.
``scripts/step6.py`` stays the one entry point. It calls into this module only
when the scenario's ``topology`` is ``networked``, so a Step 6 (localhost) run
takes none of these paths.

This is a helper module, not an entry point. At module level it imports only
the standard library and ``run_common``, because
``scripts/prepare-step6-demo.sh`` runs ``step6.py`` under a bare ``python3``.
``prepare-run.py`` and the workspace packages are loaded where they are used.
"""

from __future__ import annotations

import asyncio
import contextlib
import functools
import importlib.util
import ipaddress
import json
import os
import posixpath
import re
import shlex
import shutil
import socket
import sqlite3
import subprocess
import time
import urllib.request
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath, PureWindowsPath

from run_common import ROOT, executable, run

LOCALHOST = "localhost"
NETWORKED = "networked"
TOPOLOGIES = (LOCALHOST, NETWORKED)
WINDOWS_BASH = "/mnt/c/Program Files/Git/bin/bash.exe"
WINDOWS_CURL = "/mnt/c/Windows/System32/curl.exe"
# What `prepare-run.py --worker-only bob` writes in Bob's Windows run directory.
WORKER_ONLY_CONFIG = "configs/bob.mcp.json"
WORKER_ONLY_TELEMETRY = "bob-telemetry.jsonl"
IDENTITY_FILE = "robo-agents-workspace.json"
PROBE_AGENT = "bob-duplicate-probe"
LIVE = ("idle", "busy")
SAMPLED = (
    "status",
    "current_task_id",
    "last_heartbeat",
    "last_remote_addr",
    "worker_instance_id",
    "context_id",
    "workspace_id",
)


def now():
    return datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def instant(timestamp):
    return datetime.fromisoformat(timestamp.replace("Z", "+00:00"))


@functools.cache
def prepare_run():
    """``scripts/prepare-run.py``: the #125/#133 network rules and ``--worker-only``."""
    spec = importlib.util.spec_from_file_location("prepare_run", ROOT / "scripts/prepare-run.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def topology_of(scenario):
    value = scenario.get("topology", LOCALHOST)
    if value not in TOPOLOGIES:
        raise ValueError(f"scenario topology must be one of {TOPOLOGIES}, got {value!r}")
    return value


def networked(manifest):
    return manifest.get("topology") == NETWORKED


# Paths. Every workspace carries its host; a path is only ever compared with
# paths spelled for the same host (#140 item 5).


def windows_path(text, flag):
    """An absolute Windows directory below a drive root, with forward slashes."""
    path = PureWindowsPath(text)
    if len(path.drive) != 2 or not path.root or len(path.parts) < 2 or ".." in path.parts:
        raise ValueError(
            f"{flag} must be an absolute Windows directory such as C:/work/run, got {text!r}"
        )
    return path.as_posix()


def mount(path):
    """The WSL spelling of a Windows drive path: ``C:\\work\\x`` -> ``/mnt/c/work/x``."""
    windows = PureWindowsPath(path)
    return "/mnt/" + windows.drive[0].lower() + windows.as_posix()[2:]


def qualified(host, path):
    """``windows:C:/work/run/bob`` or ``wsl:/home/me/run/charlie``."""
    return f"{host}:{PureWindowsPath(path).as_posix() if host == 'windows' else path}"


def local_path(workspace):
    """Where this WSL process reads a clone: Bob's Windows clone through ``/mnt``."""
    if workspace.get("host") == "windows":
        return Path(mount(workspace["path"]))
    return Path(workspace["path"])


def telemetry_path(directory, manifest, name):
    if networked(manifest) and name == "bob":
        return Path(mount(manifest["windows"]["run_dir"])) / WORKER_ONLY_TELEMETRY
    return directory / f"{name}.telemetry.jsonl"


# Masking runs on the decoded strings of a document, before it is serialized.
# A string can still hold JSON text of its own (a tool input or result), so a
# separator is a slash or backslashes at any escaping depth, including JSON's
# optional ``\/``, and a non-ASCII character may appear as a ``\uXXXX`` escape.
SEP = r"(?:\\*/|\\+)"
BEFORE = r"(?<![\w.-])"
AFTER = r"(?![\w.-])"
ESCAPED = r"\\+u[0-9a-f]{4}"


def literal(text):
    """``text`` as a pattern, each non-ASCII character also matched as a JSON escape."""
    pattern = ""
    for character in text:
        if ord(character) < 0x80:
            pattern += re.escape(character)
            continue
        units = character.encode("utf-16-be")
        escaped = "".join(
            r"\\+u" + units[index : index + 2].hex() for index in range(0, len(units), 2)
        )
        pattern += f"(?:{re.escape(character)}|{escaped})"
    return pattern


def windows_root_pattern(path):
    """Every spelling of a Windows directory: ``C:/``, ``C:\\``, ``/c/``, ``/mnt/c/``."""
    windows = PureWindowsPath(path)
    drive = re.escape(windows.drive[0])
    parts = "".join(SEP + literal(part) for part in windows.parts[1:])
    return BEFORE + rf"(?:windows:)?(?:{drive}:|/mnt/{drive}|/{drive})" + parts + AFTER


def wsl_root_pattern(path):
    """Every spelling of a WSL directory, including ``\\\\wsl.localhost\\<distro>\\...``."""
    parts = "".join(SEP + literal(part) for part in PurePosixPath(path).parts[1:])
    unc = r"(?:(?:\\+|//)wsl(?:\.localhost|\$)" + SEP + r"[^\\/\s\"']+)?"
    return BEFORE + r"(?:wsl:)?" + unc + parts + AFTER


def root_pattern(workspace):
    if workspace.get("host") == "windows":
        return re.compile(windows_root_pattern(workspace["path"]), re.IGNORECASE)
    return re.compile(wsl_root_pattern(workspace["path"]), re.IGNORECASE)


# A profile name is a whole path component of any character Windows allows in
# a name: spaces, apostrophes, ``&()[]{};,=+!@#$%^~`` and non-ASCII letters.
# Only the reserved ``<>:"/\|?*`` and control characters end it, because none
# can occur inside it. A name followed by no separator (``cd '/c/Users/Ann' &&
# ls``) is masked through to the end of the string: it fails safe. The
# separator after ``Users`` is first taken as spelled after the drive, so it
# never swallows the backslash that opens a ``\uXXXX`` name character.
NAME = r"(?:[^\\/:*?\"<>|\x00-\x1f]|" + ESCAPED + ")+"
PROFILE = re.compile(
    BEFORE
    + r"((?:[a-z]:|/mnt/[a-z]|/[a-z])("
    + SEP
    + r"))(?:users|documents and settings)(\2|"
    + SEP
    + ")"
    + NAME,
    re.IGNORECASE,
)


def mask(text, directory, manifest):
    """Mask both hosts' run roots and Windows profile paths in every recorded spelling."""
    text = re.sub(
        windows_root_pattern(manifest["windows"]["run_dir"]), "windows:/RUN", text, flags=re.I
    )
    text = re.sub(wsl_root_pattern(str(directory)), "wsl:/RUN", text, flags=re.I)
    return PROFILE.sub(r"\g<1>Users\g<3><user>", text)


def mask_document(value, directory, manifest):
    """``mask`` applied to every string and key, so no JSON escaping hides a name."""
    if isinstance(value, str):
        return mask(value, directory, manifest)
    if isinstance(value, dict):
        return {
            mask(key, directory, manifest): mask_document(item, directory, manifest)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [mask_document(item, directory, manifest) for item in value]
    return value


# Addressing.


def parse_eth0(output):
    match = re.search(r"\binet (\d{1,3}(?:\.\d{1,3}){3})/\d+", output)
    if not match:
        raise ValueError("eth0 has no IPv4 address; Step 7 needs WSL2 in NAT mode")
    address = ipaddress.ip_address(match[1])
    if address.is_loopback or address.is_unspecified or not address.is_private:
        raise ValueError(f"eth0 address {address} is not a private NAT address")
    return str(address)


def read_eth0():
    return parse_eth0(run("ip", "-4", "-o", "addr", "show", "eth0"))


def lost_after_s():
    from agent_hub_common.config import DEFAULT_LOST_AFTER_S

    return DEFAULT_LOST_AFTER_S


def network_settings(eth0, port, distro):
    """This topology's #125/#133 settings, validated by prepare-run's own rules.

    The hub binds every interface on ``port`` and advertises WSL's ``eth0``
    address, which Bob dials from Windows. Charlie dials loopback on the same
    port.
    """
    settings = prepare_run().network_settings("0.0.0.0", f"http://{eth0}:{port}", None, "bob", port)
    return {
        **settings,
        "charlie_url": f"http://127.0.0.1:{settings['hub_port']}",
        "remote_worker": "bob",
        "wsl_eth0": eth0,
        "wsl_distro": distro,
        "lost_after_s": lost_after_s(),
    }


def hub_env(network):
    return {
        "HUB_HOST": network["hub_host"],
        "HUB_PORT": str(network["hub_port"]),
        "HUB_PUBLIC_URL": network["public_url"],
        "HUB_LOST_AFTER_S": str(network["lost_after_s"]),
    }


def local_hub(manifest, default):
    """The hub's loopback URL for this run: the run's port, or Step 6's default."""
    if networked(manifest):
        return manifest["network"]["charlie_url"]
    return default


def launch_lines(directory):
    return [
        f"scripts/launch-step6-alice.sh {shlex.quote(str(directory))}",
        f"scripts/launch-step6-charlie.sh {shlex.quote(str(directory))}",
        f"uv run --locked python scripts/step6_launch.py bob {shlex.quote(str(directory))}",
        f"uv run --locked python scripts/run-step6-disturbances.py {shlex.quote(str(directory))}",
    ]


# Windows through WSL interop. Environment variables do not cross into Windows
# processes, so a command's exports go into its Git Bash script.


def windows_command(windows, args, exports=None):
    """argv and cwd that run ``args`` natively on Windows, in Git Bash, from the checkout.

    The script crosses WSL's Windows command line, which collapses doubled
    backslashes (``\\\\wsl.localhost`` arrives as ``\\wsl.localhost``), so
    a backslash is refused: Windows reads forward-slash paths, UNC included.
    """
    checkout = prepare_run().git_bash_path(PureWindowsPath(windows["checkout"]))
    script = f"cd {shlex.quote(checkout)} || exit 97; "
    script += "".join(
        f"export {key}={shlex.quote(value)}; " for key, value in (exports or {}).items()
    )
    script += "exec " + shlex.join(args)
    if "\\" in script:
        raise ValueError("pass forward-slash paths to Windows; a backslash does not survive")
    return [windows["bash"], "-lc", script], mount(windows["checkout"])


def windows_call(windows, args, exports=None):
    command, cwd = windows_command(windows, args, exports)
    return subprocess.run(
        command, cwd=cwd, capture_output=True, text=True, encoding="utf-8", errors="replace"
    )


def output_tail(completed):
    text = (completed.stderr or "") + "\n" + (completed.stdout or "")
    return "\n".join(line for line in text.splitlines() if line.strip())[-600:]


def windows_clone_source(repository, distro):
    """The clone source as Windows git reads it, and the exports it needs.

    A GitHub URL passes through. A WSL path (the ``--local-repository`` dry
    run) is read over ``//wsl.localhost``. Windows git refuses a repository
    another user owns, so that one path is trusted for this command only:
    ``GIT_CONFIG_*`` is command-scope configuration and changes no global
    setting.
    """
    if not repository.startswith("/"):
        return repository, {}
    source = f"//wsl.localhost/{distro}{repository}"
    return source, {
        "GIT_CONFIG_COUNT": "2",
        "GIT_CONFIG_KEY_0": "safe.directory",
        "GIT_CONFIG_VALUE_0": source,
        "GIT_CONFIG_KEY_1": "safe.directory",
        "GIT_CONFIG_VALUE_1": source + "/.git",
    }


def port_free(port):
    with contextlib.suppress(OSError), socket.create_connection(("127.0.0.1", port), timeout=1):
        return False
    return True


def healthy(url):
    try:
        with urllib.request.urlopen(url + "/healthz", timeout=2) as response:
            return response.status == 200
    except OSError:
        return False


@contextlib.contextmanager
def preflight_hub(directory, env, url, timeout=90):
    """A throwaway hub with the run's bind settings and its own state directory.

    The hub exits when its MCP stdin closes, which is how it is stopped.
    """
    state = directory / "preflight-state"
    state.mkdir(mode=0o700, exist_ok=True)
    with (directory / "preflight-hub.log").open("ab") as log:
        process = subprocess.Popen(
            [executable("uv"), "run", "--locked", "--directory", str(ROOT), "hub"],
            env={**os.environ, **env, "HUB_STATE_DIR": str(state)},
            stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            stderr=log,
        )
        try:
            deadline = time.monotonic() + timeout
            while not healthy(url):
                if process.poll() is not None or time.monotonic() > deadline:
                    raise ValueError("preflight hub did not start; see preflight-hub.log")
                time.sleep(0.5)
            yield
        finally:
            assert process.stdin
            process.stdin.close()
            try:
                process.wait(30)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(10)


def windows_curl(windows, url):
    """``curl.exe`` on Windows, so the request leaves from the Windows network stack."""
    completed = subprocess.run(
        [windows["curl"], "-fsS", "--max-time", "10", url],
        cwd=mount(windows["checkout"]),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if completed.returncode:
        raise ValueError(f"curl.exe {url} exited {completed.returncode}: {output_tail(completed)}")
    return json.loads(completed.stdout)


def windows_checkout(windows):
    """Windows git's view of the Windows checkout: its HEAD, and whether it is dirty."""
    head = windows_call(windows, ["git", "rev-parse", "HEAD"])
    status = windows_call(windows, ["git", "status", "--porcelain"])
    if head.returncode or status.returncode:
        raise ValueError(output_tail(head if head.returncode else status))
    return head.stdout.strip(), bool(status.stdout.strip())


def preflight(directory, manifest, hub):
    """Everything that must hold before Bob is rendered on Windows or an issue is seeded."""
    windows, network = manifest["windows"], manifest["network"]
    checks = []

    def record(name, passed, detail):
        checks.append({"check": name, "passed": bool(passed), "detail": detail})

    record("wsl_distro", network.get("wsl_distro"), network.get("wsl_distro") or "unset")
    bash = Path(windows["bash"]).is_file()
    curl = Path(windows["curl"]).is_file()
    record("windows_bash", bash, windows["bash"])
    record("windows_curl", curl, windows["curl"])
    if bash:
        try:
            head, dirty = windows_checkout(windows)
            record(
                "windows_checkout",
                head == manifest["coordination_head"] and not dirty,
                f"{windows['checkout']} at {head}{' (dirty)' if dirty else ''}; "
                f"hub checkout at {manifest['coordination_head']}",
            )
        except ValueError as exc:
            record("windows_checkout", False, str(exc))
    else:
        record("windows_checkout", False, "skipped: no Git Bash")
    local = local_hub(manifest, None)
    free = port_free(network["hub_port"])
    record("hub_port_free", free, f"127.0.0.1:{network['hub_port']}")
    health = card = None
    if free and curl:
        try:
            with preflight_hub(directory, hub, local):
                health = windows_curl(windows, network["hub_url"] + "/healthz")
                card = windows_curl(windows, network["hub_url"] + "/.well-known/agent-card.json")
        except ValueError as exc:  # the hub did not start, or curl.exe failed
            record("preflight_error", False, str(exc))
    expected = network["public_url"] + "/a2a"
    record("windows_healthz", health == {"status": "ok"}, json.dumps(health))
    url = card.get("url") if isinstance(card, dict) else None
    record("windows_agent_card_url", url == expected, f"{url} (expected {expected})")
    return {"passed": all(check["passed"] for check in checks), "checks": checks}


def prepare_bob(directory, manifest, repository):
    """Render Bob on Windows with ``prepare-run.py --worker-only`` through interop.

    Windows git bootstraps the clone (``scripts/bootstrap-workspace.py``), so
    the identity's path is Windows' own spelling of ``HUB_WORKSPACE``. The
    hub's token is read in place over ``//wsl.localhost``; no copy is written
    on Windows except the config that carries it as ``HUB_TOKEN``.
    """
    windows, network = manifest["windows"], manifest["network"]
    source, exports = windows_clone_source(repository, network["wsl_distro"])
    args = [
        "uv",
        "run",
        "--locked",
        "python",
        "scripts/prepare-run.py",
        "--worker-only",
        "bob",
        "--repository",
        source,
        "--run-dir",
        windows["run_dir"],
        "--hub-url",
        network["hub_url"],
        "--token-file",
        prepare_run().remote_token_path(directory / "token").replace("\\", "/"),
        "--bob",
        "claude-code",
        "--bob-provider",
        manifest["providers"]["bob"],
        "--bob-capabilities",
        "python,gh",
    ]
    if manifest["models"]["bob"]:
        args += ["--bob-model", manifest["models"]["bob"]]
    completed = windows_call(windows, args, exports)
    if completed.returncode:
        raise ValueError(
            "prepare-run --worker-only bob failed on Windows: " + output_tail(completed)
        )
    rendered = json.loads(
        (Path(mount(windows["run_dir"])) / "run.json").read_text(encoding="utf-8")
    )
    identity = rendered["workspaces"]["bob"]
    clone = Path(mount(identity["path"]))
    skill = clone / ".claude/skills/worker"
    if not skill.exists():
        shutil.copytree(ROOT / "skills/worker", skill)
    exclude(clone, ".claude/")
    return {
        **identity,
        "host": "windows",
        "qualified_path": qualified("windows", identity["path"]),
    }, rendered["versions"]["claude"]


def exclude(clone, entry):
    """Keep local runtime files out of the clone's commits, once."""
    path = clone / ".git/info/exclude"
    text = path.read_text(encoding="utf-8") if path.exists() else ""
    if entry not in text.splitlines():
        path.parent.mkdir(exist_ok=True)
        path.write_text(text + ("" if text.endswith("\n") or not text else "\n") + entry + "\n")


def place_canaries(manifest):
    """An uncommitted canary in each clone, before any assignment (#70 check 5)."""
    canaries = {}
    for name in ("bob", "charlie"):
        canary = {
            "file": f".step7-canary-{name}",
            "token": f"STEP7-CANARY-{name.upper()}-{manifest['run_id']}",
        }
        clone = local_path(manifest["workspaces"][name])
        exclude(clone, canary["file"])
        path = clone / canary["file"]
        if not path.exists():
            path.write_text(canary["token"] + "\n", encoding="utf-8")
        canaries[name] = canary
    return canaries


# Driver-side observation while the run is live.


def agent_row(directory, name):
    database = directory / "state" / "hub.db"
    with contextlib.closing(sqlite3.connect(f"file:{database}?mode=ro", uri=True)) as db:
        db.row_factory = sqlite3.Row
        row = db.execute("SELECT * FROM agent WHERE name = ?", (name,)).fetchone()
    return {key: row[key] for key in SAMPLED} if row else None


def observe(directory, manifest, snapshot, probe=None):
    """Sample Bob's hub row, and probe the duplicate workspace once while he is busy.

    Returns whether the manifest changed.
    """
    bob = next((row for row in snapshot["agent"] if row["name"] == "bob"), None)
    if bob is None:
        return False
    sample = {key: bob.get(key) for key in SAMPLED}
    samples = manifest.setdefault("heartbeat_samples", [])
    changed = not samples or {key: samples[-1].get(key) for key in SAMPLED} != sample
    if changed:
        samples.append({"sampled_at": now(), **sample})
    if "duplicate_probe" not in manifest and bob["status"] == "busy":
        manifest["duplicate_probe"] = (probe or probe_duplicate)(directory, manifest)
        changed = True
    return changed


def observing(snapshot):
    """Keep sampling until Bob is released or the workflow is over."""
    bob = next((row for row in snapshot["agent"] if row["name"] == "bob"), {})
    workflow = snapshot["workflow"][0] if snapshot["workflow"] else {}
    return bob.get("status") != "released" and workflow.get("status") not in ("done", "escalated")


async def duplicate_check_in(settings, transport=None):
    """Check in once; return the HTTP status and the hub's refusal message, if any."""
    import httpx
    from worker_mcp.client import WorkerHubClient, WorkerProtocolError

    statuses = []

    async def status(response):
        statuses.append(response.status_code)

    async with httpx.AsyncClient(
        base_url=settings.hub_url, transport=transport, event_hooks={"response": [status]}
    ) as http:
        try:
            await WorkerHubClient(settings, http).check_in()
        except WorkerProtocolError as exc:
            return statuses[-1] if statuses else None, exc.message
    return statuses[-1] if statuses else None, None


def duplicate_clone(directory, manifest):
    """A second clone whose identity file is a copy of Bob's (#70 check 4).

    Only ``agent`` and ``path`` are rewritten, so the copy passes the worker's
    own identity checks and presents Bob's ``workspace_id`` from another
    clone under another name.
    """
    identity = json.loads(
        (local_path(manifest["workspaces"]["bob"]) / ".git" / IDENTITY_FILE).read_text(
            encoding="utf-8"
        )
    )
    clone = directory / "duplicate-probe"
    if not clone.exists():
        run("git", "clone", "--quiet", manifest["workspaces"]["driver"]["path"], clone)
        run("git", "remote", "set-url", "origin", identity["repository"], cwd=clone)
    target = clone / ".git" / IDENTITY_FILE
    target.unlink(missing_ok=True)
    descriptor = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
        json.dump({**identity, "agent": PROBE_AGENT, "path": str(clone)}, stream)
    return clone, identity["workspace_id"]


def probe_duplicate(directory, manifest, transport=None):
    from worker_mcp.config import WorkerSettings

    clone, workspace_id = duplicate_clone(directory, manifest)
    settings = WorkerSettings.from_env(
        {
            "HUB_URL": manifest["network"]["charlie_url"],
            "HUB_TOKEN": (directory / "token").read_text().strip(),
            "AGENT_NAME": PROBE_AGENT,
            "HUB_WORKSPACE": str(clone),
            "HUB_HARNESS": "duplicate-probe",
            "HUB_MAX_RETRIES": "0",
        }
    )
    before = agent_row(directory, "bob")
    status, message = asyncio.run(duplicate_check_in(settings, transport))
    return {
        "at": now(),
        "agent": PROBE_AGENT,
        "workspace_id": workspace_id,
        "status_code": status,
        "refused": message is not None,
        "message": message,
        "bob_before": before,
        "bob_after": agent_row(directory, "bob"),
    }


# Collection.


def pull_windows(directory, manifest):
    """Copy Bob's transcript and telemetry from his Windows run directory."""
    source = Path(mount(manifest["windows"]["run_dir"]))
    shutil.copyfile(source / "bob.transcript.jsonl", directory / "bob.transcript.jsonl")
    shutil.copyfile(source / WORKER_ONLY_TELEMETRY, directory / "bob.telemetry.jsonl")


def leaks(clone, canary):
    """Paths in ``clone`` named like the canary or containing its token."""
    token = canary["token"].encode()
    found = []
    for root, directories, files in os.walk(clone):
        inside_git = ".git" in Path(root).relative_to(clone).parts
        for name in files + directories:
            path = Path(root) / name
            hit = name == canary["file"]
            if not hit and not inside_git and name in files and path.stat().st_size < 4 << 20:
                with contextlib.suppress(OSError):
                    hit = token in path.read_bytes()
            if hit:
                found.append(path.relative_to(clone).as_posix())
    return sorted(found)


def canary_facts(manifest):
    facts = {}
    for name, other in (("bob", "charlie"), ("charlie", "bob")):
        canary = manifest["canaries"][name]
        own = local_path(manifest["workspaces"][name]) / canary["file"]
        facts[name] = {
            "present_in_own": own.is_file()
            and own.read_text(encoding="utf-8").strip() == canary["token"],
            "found_in_other": leaks(local_path(manifest["workspaces"][other]), canary),
        }
    return facts


# Verifier checks 1-5 (#140 item 6), on top of every Step 6 check.


def remote_peer(address, eth0):
    try:
        ip = ipaddress.ip_address(address)
    except (TypeError, ValueError):
        return False
    return not (ip.is_loopback or ip.is_unspecified) and str(ip) != eth0


def loopback_peer(address):
    try:
        return ipaddress.ip_address(address).is_loopback
    except (TypeError, ValueError):
        return False


def heartbeat_gaps(records, task_ids):
    """Largest gap per task, all in Bob's own clock: assignment, heartbeats, result.

    None marks a task whose assignment or result is missing from telemetry.
    """
    gaps = {}
    for task_id in task_ids:

        def times(tool, task_id=task_id):
            return [
                instant(record["timestamp"])
                for record in records
                if record.get("event") == "tool_call"
                and record.get("tool") == tool
                and record.get("phase") == "success"
                and record.get("task_id") == task_id
            ]

        starts, ends = times("await_assignment"), times("submit_result")
        if not starts or not ends:
            gaps[task_id] = None
            continue
        start, end = min(starts), max(ends)
        beats = sorted(
            instant(record["timestamp"])
            for record in records
            if record.get("event") == "heartbeat"
            and record.get("phase") == "success"
            and record.get("accepted") is True
            and start <= instant(record["timestamp"]) <= end
        )
        points = [start, *beats, end]
        gaps[task_id] = max(
            (later - earlier).total_seconds()
            for earlier, later in zip(points, points[1:], strict=False)
        )
    return gaps


def advanced(samples):
    """Whether a later sample's ``last_heartbeat`` is newer than an earlier one's."""
    earliest = None
    for sample in samples:
        value = sample.get("last_heartbeat")
        if not value:
            continue
        if earliest is not None and value > earliest:
            return True
        earliest = value if earliest is None else min(earliest, value)
    return False


def checkin_workspace_ids(snapshot, name):
    ids = []
    for event in snapshot["event"]:
        if event.get("kind") != "agent_checked_in":
            continue
        payload = json.loads(event.get("payload_json") or "{}")
        if payload.get("agent") == name:
            ids.append(payload.get("workspace_id"))
    return ids


def cross_host_access(manifest, name, calls, audit):
    """Whether a worker's recorded calls reach the other clone, spelled for its own host.

    The other clone is matched in every spelling its host has (``C:\\``,
    ``/c/``, ``/mnt/c/``; ``/home/...``, ``\\\\wsl.localhost\\...``). Charlie can
    also reach Bob's clone by a relative path or ``cd`` inside WSL, so his
    shell words are resolved against ``/mnt`` with Step 6's own audit.
    """
    other = "charlie" if name == "bob" else "bob"
    workspace, target = manifest["workspaces"][name], manifest["workspaces"][other]
    pattern = root_pattern(target)
    mounted = str(local_path(target))
    for call in calls:
        if any(pattern.search(text) for text in audit["strings"](call["input"])):
            return True
        if workspace.get("host") != "wsl" or target.get("host") != "windows":
            continue
        command = call["input"].get("command", "")
        if "other_workspace" in audit["shell_actions"](command, workspace["path"], mounted):
            return True
        if any(
            isinstance(value, str)
            and audit["within"](posixpath.join(workspace["path"], value), mounted)
            for key, value in call["input"].items()
            if key in ("file_path", "filePath", "path")
        ):
            return True
    return False


def evaluate(manifest, snapshot, facts, traces, audit):
    """Checks 1-5, each fail-closed on missing facts.

    ``audit`` carries Step 6's ``strings``, ``shell_actions`` and ``within``.
    """
    network = manifest["network"]
    agents = {row["name"]: row for row in snapshot["agent"]}
    bob, charlie = agents.get("bob", {}), agents.get("charlie", {})
    samples = manifest.get("heartbeat_samples", [])
    telemetry = facts.get("telemetry", {}).get("bob", [])
    workspaces = manifest["workspaces"]
    checks = {}

    # 1. Boundary crossed.
    eth0 = network["wsl_eth0"]
    checks["step7_bob_remote_peer"] = all(
        remote_peer(address, eth0)
        for address in (
            bob.get("checkin_remote_addr"),
            bob.get("last_remote_addr"),
            *(sample["last_remote_addr"] for sample in samples if sample.get("last_remote_addr")),
        )
    )
    checks["step7_charlie_loopback_peer"] = loopback_peer(
        charlie.get("checkin_remote_addr")
    ) and loopback_peer(charlie.get("last_remote_addr"))
    dialed = [record["hub_url"] for record in telemetry if "hub_url" in record]
    checks["step7_bob_dialed_public_url"] = (
        any(record.get("event") == "session_started" for record in telemetry)
        and bool(dialed)
        and all(url == network["public_url"] for url in dialed)
    )

    # 2. Live heartbeats.
    tasks = [task for task in snapshot["task"] if task["assignee"] == "bob"]
    gaps = heartbeat_gaps(telemetry, [task["id"] for task in tasks])
    checks["step7_heartbeat_gaps"] = bool(gaps) and all(
        gap is not None and gap < network["lost_after_s"] for gap in gaps.values()
    )
    checks["step7_no_agent_lost"] = not any(
        event.get("kind") == "agent_lost" for event in snapshot["event"]
    )
    longest = max(
        tasks,
        key=lambda task: instant(task["updated"]) - instant(task["created"]),
        default=None,
    )
    checks["step7_hub_heartbeat_advanced"] = longest is not None and advanced(
        [sample for sample in samples if sample.get("current_task_id") == longest["id"]]
    )

    # 3. #70 distinct, stable workspace identities.
    identities = {name: workspaces[name]["workspace_id"] for name in ("bob", "charlie")}
    checks["step7_distinct_workspace_ids"] = (
        all(identities.values())
        and identities["bob"] != identities["charlie"]
        and bob.get("workspace_id") == identities["bob"]
        and charlie.get("workspace_id") == identities["charlie"]
    )
    checks["step7_stable_workspace_ids"] = all(
        (ids := checkin_workspace_ids(snapshot, name)) and set(ids) == {identities[name]}
        for name in ("bob", "charlie")
    )

    # 4. #70 duplicate refused while Bob is live, and Bob unaffected.
    probe = manifest.get("duplicate_probe") or {}
    before, after = probe.get("bob_before") or {}, probe.get("bob_after") or {}
    instance = before.get("worker_instance_id")
    message = probe.get("message") or ""
    checks["step7_duplicate_refused"] = (
        probe.get("status_code") == 409
        and probe.get("refused") is True
        and probe.get("workspace_id") == identities["bob"]
        and f"workspace {identities['bob']} is occupied by live agent bob" in message
        and before.get("status") in LIVE
        and after.get("status") in LIVE
        and bool(instance)
        and instance == after.get("worker_instance_id") == bob.get("worker_instance_id")
        and before.get("context_id") == after.get("context_id")
        and PROBE_AGENT not in agents
        and any(
            sample.get("sampled_at", "") > probe.get("at", "")
            and sample.get("worker_instance_id") == instance
            and (sample.get("last_heartbeat") or "") > (after.get("last_heartbeat") or "")
            for sample in samples
        )
    )

    # 5. #70 uncommitted isolation.
    canaries = facts.get("canaries", {})
    checks["step7_canary_isolation"] = all(
        canaries.get(name, {}).get("present_in_own") is True
        and canaries[name].get("found_in_other") == []
        for name in ("bob", "charlie")
    )
    for name in ("bob", "charlie"):
        calls = traces.get(name, [])
        checks[f"step7_{name}_no_cross_host_access"] = bool(calls) and not cross_host_access(
            manifest, name, calls, audit
        )
    return checks
