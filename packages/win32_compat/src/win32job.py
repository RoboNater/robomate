"""win32job subset for mcp (#181); see _robomate_win32."""

from _robomate_win32 import (
    JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE,
    AssignProcessToJobObject,
    CreateJobObject,
    JobObjectExtendedLimitInformation,
    QueryInformationJobObject,
    SetInformationJobObject,
    TerminateJobObject,
)

__all__ = [
    "JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE",
    "AssignProcessToJobObject",
    "CreateJobObject",
    "JobObjectExtendedLimitInformation",
    "QueryInformationJobObject",
    "SetInformationJobObject",
    "TerminateJobObject",
]
