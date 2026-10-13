"""Bounded retry for Windows sharing violations on small state files.

On Windows, `os.replace` cannot replace a destination that another process
holds open without FILE_SHARE_DELETE, and Python's `open()` never passes that
flag. A reader that opens the file while a writer's `os.replace` holds it,
or while a scanner (antivirus, indexer) has it open, fails the same way. The
C runtime reports both as EACCES, which Python raises as `PermissionError`.
The condition clears within milliseconds, so readers and writers of
`hub.json` (#103, #114) and `hubs.json` (#180) retry briefly; a file that
stays locked still raises.
"""

from __future__ import annotations

import sys
import time
from collections.abc import Callable, Iterable

# About 0.6 s in total, well inside the registry lock's 5 s deadline. Empty
# off Windows, where a PermissionError is a real permission problem.
SHARING_RETRY_DELAYS_S: tuple[float, ...] = (
    (0.01, 0.02, 0.05, 0.1, 0.2, 0.2) if sys.platform == "win32" else ()
)


def retry_sharing_violation[T](operation: Callable[[], T], delays: Iterable[float]) -> T:
    """Run `operation`, retrying after each delay while it raises PermissionError.

    The last PermissionError propagates once the delays run out. Any other
    exception, FileNotFoundError included, propagates at once.
    """

    for delay in (*delays, None):
        try:
            return operation()
        except PermissionError:
            if delay is None:
                raise
            time.sleep(delay)
    raise AssertionError("unreachable")  # pragma: no cover
