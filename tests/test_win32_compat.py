"""pywin32 stays out; robomate-win32-compat does what mcp needs of it (#181).

pywin32 bundles adodbapi under LGPL-2.1. The root pyproject.toml excludes it,
and packages/win32_compat provides the calls mcp.os.win32.utilities makes to
keep each stdio server it spawns, with that server's descendants, in a
kill-on-close Job Object. The Windows tests drive mcp's own job code, so an mcp
release that needs a call the package lacks fails here instead of quietly
running without a job.
"""

from __future__ import annotations

import ctypes
import gc
import importlib.metadata
import subprocess
import sys
import sysconfig
import tomllib
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.types import TextContent

ROOT = Path(__file__).resolve().parents[1]

# What a pywin32 install leaves in site-packages.
PYWIN32_FILES = (
    "adodbapi",
    "pythonwin",
    "pywin32_system32",
    "pywin32.pth",
    "win32",
    "win32com",
    "win32comext",
)

SLEEPER = [sys.executable, "-c", "import time; time.sleep(60)"]

# A stdio MCP server that spawns a grandchild and reports both PIDs.
SERVER = f"""
import os, subprocess
from mcp.server.fastmcp import FastMCP

grandchild = subprocess.Popen({SLEEPER!r})
app = FastMCP("win32-compat")


@app.tool()
def pids() -> str:
    return f"{{os.getpid()}} {{grandchild.pid}}"


app.run()
"""

# A client that prints the PIDs, waits for a line on stdin, then dies abruptly
# while it still holds the job handle, as a crashed or killed client would.
CLIENT = """
import asyncio, os, sys
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


async def main() -> None:
    params = StdioServerParameters(command=sys.executable, args=[sys.argv[1]])
    async with stdio_client(params) as (read, write), ClientSession(read, write) as session:
        await session.initialize()
        result = await session.call_tool("pids", {})
        print(result.content[0].text, flush=True)
        sys.stdin.readline()
        os._exit(0)


asyncio.run(main())
"""


def test_lockfile_excludes_pywin32() -> None:
    lock = tomllib.loads((ROOT / "uv.lock").read_text(encoding="utf-8"))
    assert "pywin32" in lock["manifest"]["excludes"]
    packages = {package["name"]: package for package in lock["package"]}
    assert "pywin32" not in packages
    assert "robomate-win32-compat" in packages
    for name, package in packages.items():
        dependencies = {dependency["name"] for dependency in package.get("dependencies", [])}
        assert "pywin32" not in dependencies, name


def test_environment_has_no_pywin32() -> None:
    with pytest.raises(importlib.metadata.PackageNotFoundError):
        importlib.metadata.distribution("pywin32")
    site_dirs = {Path(sysconfig.get_path(key)) for key in ("purelib", "platlib")}
    leftovers = [site / name for site in site_dirs for name in PYWIN32_FILES]
    assert [path for path in leftovers if path.exists()] == []


def test_win32_modules_are_the_compat_package() -> None:
    if sys.platform != "win32":  # in the body, so mypy skips the rest off Windows
        pytest.skip("robomate-win32-compat is installed on Windows only")
    import _robomate_win32
    import pywintypes
    import win32api
    import win32con
    import win32job

    importlib.metadata.distribution("robomate-win32-compat")
    home = Path(str(_robomate_win32.__file__)).parent
    for module in (pywintypes, win32api, win32con, win32job):
        assert Path(str(module.__file__)).parent == home, module.__name__
    assert win32job.CreateJobObject is _robomate_win32.CreateJobObject


def test_job_limit_structure_matches_the_win32_layout() -> None:
    if sys.platform != "win32":
        pytest.skip("robomate-win32-compat is installed on Windows only")
    import _robomate_win32

    # sizeof(JOBOBJECT_EXTENDED_LIMIT_INFORMATION): 144 on 64-bit Windows, 112 on 32-bit.
    expected = 144 if ctypes.sizeof(ctypes.c_void_p) == 8 else 112
    assert ctypes.sizeof(_robomate_win32._ExtendedLimitInformation) == expected


def test_job_limits_round_trip() -> None:
    if sys.platform != "win32":
        pytest.skip("robomate-win32-compat is installed on Windows only")
    import pywintypes
    import win32api
    import win32job

    job = win32job.CreateJobObject(None, "")
    try:
        info_class = win32job.JobObjectExtendedLimitInformation
        info = win32job.QueryInformationJobObject(job, info_class)
        kill_on_close = win32job.JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        assert not info["BasicLimitInformation"]["LimitFlags"] & kill_on_close
        info["BasicLimitInformation"]["LimitFlags"] |= kill_on_close
        win32job.SetInformationJobObject(job, info_class, info)
        info = win32job.QueryInformationJobObject(job, info_class)
        assert info["BasicLimitInformation"]["LimitFlags"] & kill_on_close
        with pytest.raises(pywintypes.error) as raised:
            win32job.QueryInformationJobObject(job, 2)  # JobObjectBasicAccountingInformation
        assert raised.value.winerror == 87
    finally:
        win32api.CloseHandle(job)


def test_win32_errors_carry_code_function_and_message() -> None:
    if sys.platform != "win32":
        pytest.skip("robomate-win32-compat is installed on Windows only")
    import pywintypes
    import win32api
    import win32con

    with pytest.raises(pywintypes.error) as raised:
        win32api.OpenProcess(win32con.PROCESS_TERMINATE, False, 0)  # the idle process
    assert raised.value.winerror == 87  # ERROR_INVALID_PARAMETER
    assert raised.value.funcname == "OpenProcess"
    assert raised.value.strerror


@pytest.fixture
def processes() -> Iterator[_Processes]:
    tracked = _Processes()
    try:
        yield tracked
    finally:
        tracked.cleanup()


def test_mcp_job_object_holds_and_terminates_a_child(processes: _Processes) -> None:
    if sys.platform != "win32":
        pytest.skip("Windows Job Objects")
    import pywintypes
    import win32api
    import win32job
    from mcp.os.win32 import utilities

    child = processes.watch(processes.spawn(SLEEPER).pid)
    job = utilities._create_job_object()
    assert isinstance(job, pywintypes.PyHANDLE)
    stand_in = SimpleNamespace(pid=child.pid)
    utilities._maybe_assign_process_to_job(cast(Any, stand_in), job)
    assert stand_in._job_object is job
    assert child.in_job(job)

    win32job.TerminateJobObject(job, 1)
    assert child.exited_within(10)
    win32api.CloseHandle(job)
    assert not job


@pytest.mark.parametrize("close", ["CloseHandle", "collection"])
def test_closing_the_job_handle_kills_its_processes(processes: _Processes, close: str) -> None:
    if sys.platform != "win32":
        pytest.skip("Windows Job Objects")
    import win32api
    import win32con
    import win32job

    job = win32job.CreateJobObject(None, "")
    info_class = win32job.JobObjectExtendedLimitInformation
    info = win32job.QueryInformationJobObject(job, info_class)
    info["BasicLimitInformation"]["LimitFlags"] |= win32job.JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
    win32job.SetInformationJobObject(job, info_class, info)
    child = processes.watch(processes.spawn(SLEEPER).pid)
    access = win32con.PROCESS_SET_QUOTA | win32con.PROCESS_TERMINATE
    process_handle = win32api.OpenProcess(access, False, child.pid)
    win32job.AssignProcessToJobObject(job, process_handle)
    win32api.CloseHandle(process_handle)
    assert not child.exited_within(0.2)

    if close == "CloseHandle":
        win32api.CloseHandle(job)
        assert not job
        win32api.CloseHandle(job)  # a second close is a no-op, never a reused value's close
    else:
        del job
        gc.collect()
    assert child.exited_within(10)


def test_failed_assignment_closes_the_job() -> None:
    if sys.platform != "win32":
        pytest.skip("Windows Job Objects")
    from mcp.os.win32 import utilities

    job = utilities._create_job_object()
    assert job is not None
    stand_in = SimpleNamespace(pid=0)  # OpenProcess fails on the idle process
    utilities._maybe_assign_process_to_job(cast(Any, stand_in), job)
    assert not hasattr(stand_in, "_job_object")
    assert not job  # mcp closed it, and nothing is left for collection to close again


def test_abrupt_client_death_kills_server_and_grandchild(
    processes: _Processes, tmp_path: Path
) -> None:
    if sys.platform != "win32":
        pytest.skip("Windows Job Objects")
    server_script = tmp_path / "server.py"
    server_script.write_text(SERVER, encoding="utf-8")
    client_script = tmp_path / "client.py"
    client_script.write_text(CLIENT, encoding="utf-8")
    client = processes.spawn(
        [sys.executable, str(client_script), str(server_script)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
    )
    assert client.stdin is not None and client.stdout is not None
    server_pid, grandchild_pid = map(int, client.stdout.readline().split())
    server = processes.watch(server_pid)
    grandchild = processes.watch(grandchild_pid)
    assert not grandchild.exited_within(0.2)

    client.stdin.write("\n")
    client.stdin.flush()
    assert client.wait(10) == 0
    assert server.exited_within(10)
    assert grandchild.exited_within(10)


async def test_normal_shutdown_kills_the_grandchild_on_collection(
    processes: _Processes, tmp_path: Path
) -> None:
    if sys.platform != "win32":
        pytest.skip("Windows Job Objects")
    server_script = tmp_path / "server.py"
    server_script.write_text(SERVER, encoding="utf-8")
    params = StdioServerParameters(command=sys.executable, args=[str(server_script)])
    async with stdio_client(params) as (read, write), ClientSession(read, write) as session:
        await session.initialize()
        result = await session.call_tool("pids", {})
        content = result.content[0]
        assert isinstance(content, TextContent)
        _, grandchild_pid = map(int, content.text.split())
        grandchild = processes.watch(grandchild_pid)

    # mcp closes the job only when it has to terminate the server. After a clean
    # exit, collecting the process object closes the job, as pywin32's PyHANDLE does.
    gc.collect()
    assert grandchild.exited_within(10)


SYNCHRONIZE = 0x00100000
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
PROCESS_TERMINATE = 0x0001
WAIT_OBJECT_0 = 0


def _kernel32() -> Any:
    if sys.platform != "win32":
        raise RuntimeError("Windows only")
    from ctypes import wintypes

    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.OpenProcess.restype = wintypes.HANDLE
    k32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    k32.WaitForSingleObject.restype = wintypes.DWORD
    k32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    k32.TerminateProcess.argtypes = [wintypes.HANDLE, wintypes.UINT]
    k32.IsProcessInJob.argtypes = [wintypes.HANDLE, wintypes.HANDLE, ctypes.c_void_p]
    k32.CloseHandle.argtypes = [wintypes.HANDLE]
    return k32


class _Watched:
    """An open handle on a test process, so its PID cannot be reused under the test."""

    def __init__(self, pid: int) -> None:
        self.pid = pid
        access = SYNCHRONIZE | PROCESS_QUERY_LIMITED_INFORMATION | PROCESS_TERMINATE
        self.handle = _kernel32().OpenProcess(access, False, pid)
        if not self.handle:
            raise OSError(f"cannot open process {pid}")

    def exited_within(self, seconds: float) -> bool:
        return bool(_kernel32().WaitForSingleObject(self.handle, int(seconds * 1000)) == 0)

    def in_job(self, job: object) -> bool:
        member = ctypes.c_int()
        if not _kernel32().IsProcessInJob(self.handle, int(cast(Any, job)), ctypes.byref(member)):
            raise OSError(f"IsProcessInJob failed for {self.pid}")
        return bool(member.value)

    def kill(self) -> None:
        if self.handle:
            if not self.exited_within(0):
                _kernel32().TerminateProcess(self.handle, 1)
            _kernel32().CloseHandle(self.handle)
            self.handle = None


class _Processes:
    """Every process a test starts or watches, killed at teardown even on failure."""

    def __init__(self) -> None:
        self.spawned: list[subprocess.Popen[Any]] = []
        self.watched: list[_Watched] = []

    def spawn(self, args: list[str], **kwargs: Any) -> subprocess.Popen[Any]:
        process = subprocess.Popen(args, **kwargs)
        self.spawned.append(process)
        return process

    def watch(self, pid: int) -> _Watched:
        watched = _Watched(pid)
        self.watched.append(watched)
        return watched

    def cleanup(self) -> None:
        for watched in self.watched:
            watched.kill()
        for process in self.spawned:
            if process.poll() is None:
                process.kill()
            for stream in (process.stdin, process.stdout, process.stderr):
                if stream is not None:
                    stream.close()
            process.wait(10)
