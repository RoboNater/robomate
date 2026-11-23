"""Hub logging shared by the `hub` and `robomate up` entry points (#652).

Both entry points read `HUB_LOG_LEVEL` and `HUB_LOG_FILE` through
`HubSettings.from_env()` and then call `setup_hub_logging`, so the level, the
stderr format and the persistent file cannot drift apart again. Uvicorn's own
handlers are replaced by `uvicorn_log_config`: its records reach the root
logger's handlers instead, so one format covers stderr and the file alike,
and one rotating writer owns the file.
"""

from __future__ import annotations

import logging
import logging.handlers
import os
import sys
import time
from pathlib import Path
from typing import Any

# Size-bounded rotation with a fixed number of backups: five 5 MiB files
# (`hub.log.1`..`hub.log.5`, newest first) bound the state directory's growth
# while keeping enough history to debug a failed start.
LOG_FILE_MAX_BYTES = 5 * 1024 * 1024
LOG_FILE_BACKUP_COUNT = 5

# The exact format `logging.basicConfig` used before #652: `up`'s stderr
# lines and the tests that match them stay byte-for-byte unchanged.
STDERR_FORMAT = "%(levelname)s:%(name)s:%(message)s"
# The same records, timestamped in UTC like the startup stage markers (#372).
FILE_FORMAT = "%(asctime)s %(levelname)s:%(name)s:%(message)s"

# Handlers this module attaches to the root logger, tagged so a second call
# (or a test's cleanup) replaces them instead of stacking duplicates.
_HANDLER_TAG = "_robomate_hub_log_handler"


class _PrivateRotatingFileHandler(logging.handlers.RotatingFileHandler):
    """A rotating file handler that creates every file owner-only.

    `RotatingFileHandler` opens with the process umask, so the file it
    recreates after a rotation would be group-readable; hub state artifacts
    are private however often the log turns over.
    """

    def _open(self) -> Any:
        descriptor = os.open(self.baseFilename, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        return os.fdopen(descriptor, self.mode, encoding=self.encoding, errors=self.errors)


def default_log_file(state_dir: Path) -> Path:
    """Where a hub writes its persistent log unless `HUB_LOG_FILE` says otherwise."""

    return state_dir / "logs" / "hub.log"


def setup_hub_logging(level: str, log_file: Path | None) -> None:
    """Apply `level` to the root and uvicorn loggers, on stderr and `log_file`.

    The stderr handler keeps the pre-#652 format so operator-visible lines do
    not move; the file carries the same records with a UTC timestamp, in UTF-8
    regardless of the console code page, rotated at `LOG_FILE_MAX_BYTES` with
    `LOG_FILE_BACKUP_COUNT` backups. `log_file=None` (from `HUB_LOG_FILE=-`)
    disables the file. Calling this again replaces the handlers it added.
    """

    root = logging.getLogger()
    root.setLevel(level)
    for handler in list(root.handlers):
        if getattr(handler, _HANDLER_TAG, False):
            root.removeHandler(handler)
            handler.close()

    stderr_handler = logging.StreamHandler(sys.stderr)
    stderr_handler.setFormatter(logging.Formatter(STDERR_FORMAT))
    setattr(stderr_handler, _HANDLER_TAG, True)
    root.addHandler(stderr_handler)

    if log_file is not None:
        # 0700 on creation (the umask can only clear bits); the file itself is
        # opened 0600 by the handler.
        log_file.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        file_handler = _PrivateRotatingFileHandler(
            log_file,
            maxBytes=LOG_FILE_MAX_BYTES,
            backupCount=LOG_FILE_BACKUP_COUNT,
            encoding="utf-8",
        )
        file_formatter = logging.Formatter(FILE_FORMAT)
        file_formatter.converter = time.gmtime
        file_handler.setFormatter(file_formatter)
        setattr(file_handler, _HANDLER_TAG, True)
        root.addHandler(file_handler)

    # Uvicorn re-applies `uvicorn_log_config` when it builds its Config; set
    # the levels here too so the shared setup alone is observable and an
    # application that logs before the server starts is already filtered.
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        logging.getLogger(name).setLevel(level)


def clear_hub_logging() -> None:
    """Remove the handlers `setup_hub_logging` added and reset the levels."""

    root = logging.getLogger()
    for handler in list(root.handlers):
        if getattr(handler, _HANDLER_TAG, False):
            root.removeHandler(handler)
            handler.close()
    root.setLevel(logging.NOTSET)
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        logging.getLogger(name).setLevel(logging.NOTSET)


def uvicorn_log_config(level: str) -> dict[str, Any]:
    """Uvicorn's logging configuration at `level`, with no handlers of its own.

    Uvicorn normally gives `uvicorn` and `uvicorn.error` a stderr handler and
    stops propagation; here they propagate to the root logger instead, so
    their records use the hub's stderr format and reach the same rotating
    file. `disable_existing_loggers` stays false so the application's own
    loggers survive the reconfiguration. With no handlers left on
    `uvicorn.access`, uvicorn's access-log switch sees the logger empty and
    stays off alongside `access_log=False`.
    """

    return {
        "version": 1,
        "disable_existing_loggers": False,
        "formatters": {
            "default": {
                "()": "uvicorn.logging.DefaultFormatter",
                "fmt": "%(levelprefix)s %(message)s",
                "use_colors": None,
            },
            "access": {
                "()": "uvicorn.logging.AccessFormatter",
                "fmt": '%(levelprefix)s %(client_addr)s - "%(request_line)s" %(status_code)s',
                "use_colors": None,
            },
        },
        "loggers": {
            "uvicorn": {"level": level, "propagate": True},
            "uvicorn.error": {"level": level, "propagate": True},
            "uvicorn.access": {"level": level, "propagate": True},
        },
    }
