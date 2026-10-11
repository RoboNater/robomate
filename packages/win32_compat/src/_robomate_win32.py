"""ctypes stand-in for the pywin32 calls mcp's stdio client makes (#181).

pywin32 bundles adodbapi under LGPL-2.1, so the workspace excludes it
(`exclude-dependencies` in the root pyproject.toml). mcp.os.win32.utilities
still imports pywintypes, win32api, win32con and win32job on Windows, and uses
seven calls from them to put each stdio server it spawns in a kill-on-close
Job Object. This module implements exactly those calls against kernel32 from
the documented Win32 API; the four top-level modules re-export them. A name
mcp does not use is absent, so its use raises AttributeError.
"""

from __future__ import annotations

import contextlib
import ctypes
import functools
import sys
from typing import Any

PROCESS_TERMINATE = 0x0001
PROCESS_SET_QUOTA = 0x0100
JobObjectExtendedLimitInformation = 9
JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x2000
ERROR_INVALID_PARAMETER = 87

_HANDLE = ctypes.c_void_p
_DWORD = ctypes.c_ulong
_BOOL = ctypes.c_int


class error(Exception):  # pywintypes.error
    """Args are (winerror, funcname, strerror), as pywintypes.error's are."""

    def __init__(self, winerror: int, funcname: str, strerror: str) -> None:
        super().__init__(winerror, funcname, strerror)
        self.winerror = winerror
        self.funcname = funcname
        self.strerror = strerror


class PyHANDLE:
    """Owns a Win32 handle, as pywintypes.PyHANDLE does.

    It closes the handle at most once, and on collection if still open. mcp
    keeps the job handle on the process object and closes it only when it has
    to terminate the server; after a normal shutdown, collection is what closes
    the job and kills any descendants still in it.
    """

    def __init__(self, handle: int) -> None:
        self.handle = handle

    def __int__(self) -> int:
        return self.handle

    def __bool__(self) -> bool:
        return self.handle != 0

    def __repr__(self) -> str:
        return f"<PyHANDLE:{self.handle}>"

    def Close(self) -> None:
        handle, self.handle = self.handle, 0
        if handle:
            _check(_kernel32().CloseHandle(handle), "CloseHandle")

    def Detach(self) -> int:
        handle, self.handle = self.handle, 0
        return handle

    def __del__(self) -> None:
        with contextlib.suppress(Exception):  # nothing to report to during collection
            self.Close()


type _Handle = PyHANDLE | int


class _IoCounters(ctypes.Structure):
    _fields_ = [
        ("ReadOperationCount", ctypes.c_ulonglong),
        ("WriteOperationCount", ctypes.c_ulonglong),
        ("OtherOperationCount", ctypes.c_ulonglong),
        ("ReadTransferCount", ctypes.c_ulonglong),
        ("WriteTransferCount", ctypes.c_ulonglong),
        ("OtherTransferCount", ctypes.c_ulonglong),
    ]


class _BasicLimitInformation(ctypes.Structure):
    _fields_ = [
        ("PerProcessUserTimeLimit", ctypes.c_longlong),  # LARGE_INTEGER
        ("PerJobUserTimeLimit", ctypes.c_longlong),
        ("LimitFlags", _DWORD),
        ("MinimumWorkingSetSize", ctypes.c_size_t),  # SIZE_T
        ("MaximumWorkingSetSize", ctypes.c_size_t),
        ("ActiveProcessLimit", _DWORD),
        ("Affinity", ctypes.c_size_t),  # ULONG_PTR
        ("PriorityClass", _DWORD),
        ("SchedulingClass", _DWORD),
    ]


class _ExtendedLimitInformation(ctypes.Structure):
    _fields_ = [
        ("BasicLimitInformation", _BasicLimitInformation),
        ("IoInfo", _IoCounters),
        ("ProcessMemoryLimit", ctypes.c_size_t),
        ("JobMemoryLimit", ctypes.c_size_t),
        ("PeakProcessMemoryUsed", ctypes.c_size_t),
        ("PeakJobMemoryUsed", ctypes.c_size_t),
    ]


@functools.cache
def _kernel32() -> Any:
    if sys.platform != "win32":
        raise error(0, "kernel32", "the Win32 API is only available on Windows")
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.OpenProcess.argtypes = [_DWORD, _BOOL, _DWORD]
    k32.OpenProcess.restype = _HANDLE
    k32.CloseHandle.argtypes = [_HANDLE]
    k32.CloseHandle.restype = _BOOL
    k32.CreateJobObjectW.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p]
    k32.CreateJobObjectW.restype = _HANDLE
    info_args: list[Any] = [_HANDLE, ctypes.c_int, ctypes.c_void_p, _DWORD]
    k32.QueryInformationJobObject.argtypes = [*info_args, ctypes.POINTER(_DWORD)]
    k32.QueryInformationJobObject.restype = _BOOL
    k32.SetInformationJobObject.argtypes = info_args
    k32.SetInformationJobObject.restype = _BOOL
    k32.AssignProcessToJobObject.argtypes = [_HANDLE, _HANDLE]
    k32.AssignProcessToJobObject.restype = _BOOL
    k32.TerminateJobObject.argtypes = [_HANDLE, ctypes.c_uint]
    k32.TerminateJobObject.restype = _BOOL
    return k32


def _check(result: Any, funcname: str) -> Any:
    if not result:
        code = ctypes.get_last_error()  # type: ignore[attr-defined,unused-ignore]
        raise error(code, funcname, ctypes.FormatError(code))  # type: ignore[attr-defined,unused-ignore]
    return result


def _require_extended(info_class: int, funcname: str) -> None:
    if info_class != JobObjectExtendedLimitInformation:
        raise error(ERROR_INVALID_PARAMETER, funcname, f"unsupported class {info_class}")


def OpenProcess(access: int, inherit: bool, pid: int) -> PyHANDLE:
    handle = _check(_kernel32().OpenProcess(access, inherit, pid), "OpenProcess")
    return PyHANDLE(handle)


def CloseHandle(handle: _Handle) -> None:
    if isinstance(handle, PyHANDLE):
        handle.Close()  # once closed, a no-op: never a second CloseHandle on a reused value
    else:
        _check(_kernel32().CloseHandle(handle), "CloseHandle")


def CreateJobObject(security_attributes: None, name: str) -> PyHANDLE:
    job = _kernel32().CreateJobObjectW(security_attributes, name or None)
    return PyHANDLE(_check(job, "CreateJobObject"))


def QueryInformationJobObject(job: _Handle, info_class: int) -> dict[str, Any]:
    _require_extended(info_class, "QueryInformationJobObject")
    info = _ExtendedLimitInformation()
    size = ctypes.sizeof(info)
    ok = _kernel32().QueryInformationJobObject(int(job), info_class, ctypes.byref(info), size, None)
    _check(ok, "QueryInformationJobObject")
    return _to_dict(info)


def SetInformationJobObject(job: _Handle, info_class: int, info: dict[str, Any]) -> None:
    _require_extended(info_class, "SetInformationJobObject")
    struct = _from_dict(_ExtendedLimitInformation, info)
    size = ctypes.sizeof(struct)
    ok = _kernel32().SetInformationJobObject(int(job), info_class, ctypes.byref(struct), size)
    _check(ok, "SetInformationJobObject")


def AssignProcessToJobObject(job: _Handle, process: _Handle) -> None:
    ok = _kernel32().AssignProcessToJobObject(int(job), int(process))
    _check(ok, "AssignProcessToJobObject")


def TerminateJobObject(job: _Handle, exit_code: int) -> None:
    _check(_kernel32().TerminateJobObject(int(job), exit_code), "TerminateJobObject")


def _to_dict(struct: ctypes.Structure) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for name, *_ in struct._fields_:
        value = getattr(struct, name)
        out[name] = _to_dict(value) if isinstance(value, ctypes.Structure) else value
    return out


def _from_dict[T: ctypes.Structure](cls: type[T], values: dict[str, Any]) -> T:
    struct = cls()
    for name, field_type, *_ in cls._fields_:
        if name in values:
            value = values[name]
            if isinstance(field_type, type) and issubclass(field_type, ctypes.Structure):
                value = _from_dict(field_type, value)
            setattr(struct, name, value)
    return struct
