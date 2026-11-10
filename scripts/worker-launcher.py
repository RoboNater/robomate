#!/usr/bin/env python3
"""Resume a headless worker on any supported harness until release (#115, #158, #165).

Uses agent_launcher.py for the same harness commands, conversation-ID capture,
child supervision and bounded backoff as Alice. Before each launch, bearer-only
hub status is checked: cleanly detached predecessors can be replaced at once;
attached predecessors must become lost first (which fails their task). An
orphan bridge that continues heartbeating stops recovery after 300 seconds,
rather than launching a duplicate. The operator stops that bridge and resumes
by hand. No hub mutation or ownership takeover is performed by this launcher.

Each launch and exit is recorded in <name>-sessions.jsonl. --resume-session
and --resume-prompt give hand resumes the same supervision and a fresh budget.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import agent_launcher as common  # noqa: E402
from agent_worker import (  # noqa: E402,F401
    CONTINUE_PROMPT,
    WorkerLauncher,
    WorkerReader,
    released,
    telemetry_size,
)


def parse_args(argv: list[str] | None) -> argparse.Namespace:
    return common.parse_args(argv, worker=True)


def main(argv: list[str] | None = None, reader: common.HubReader | None = None) -> int:
    return common.main(argv, reader, launcher_type=WorkerLauncher, worker=True)


if __name__ == "__main__":
    sys.exit(main())
