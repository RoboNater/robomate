"""rbm-bench run driver: the documented detached launch can still be stopped."""

from __future__ import annotations

import os
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts/rbm-bench/run_case.py"

# Loads run_case.py as the real runner would, reports SIGINT's disposition before
# and after restore_interrupts(), then waits to be interrupted.
CHILD = f"""
import importlib.util, signal, sys, time
spec = importlib.util.spec_from_file_location("run_case", {str(SCRIPT)!r})
run_case = importlib.util.module_from_spec(spec)
spec.loader.exec_module(run_case)
inherited = signal.getsignal(signal.SIGINT) is signal.SIG_IGN
run_case.restore_interrupts()
with open(sys.argv[1], "w") as out:
    out.write(f"ready ignored={{inherited}}\\n")
try:
    time.sleep(60)
except KeyboardInterrupt:
    with open(sys.argv[1], "a") as out:
        out.write("interrupted\\n")
"""


def wait_for(path: Path, text: str, timeout_s: float = 30) -> str:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if path.exists() and text in (content := path.read_text()):
            return content
        time.sleep(0.05)
    raise AssertionError(f"{text!r} never appeared in {path}")


@pytest.mark.skipif(os.name == "nt" or shutil.which("setsid") is None, reason="POSIX setsid launch")
@pytest.mark.parametrize("stop", [signal.SIGINT, signal.SIGTERM])
def test_detached_launch_stops_on_signal(tmp_path: Path, stop: signal.Signals) -> None:
    status = tmp_path / "status"
    pid_file = tmp_path / "pid"
    child = tmp_path / "child.py"
    child.write_text(CHILD, encoding="utf-8")
    # The launch review-run-prompt.md documents: a background job of a
    # non-interactive shell, which starts with SIGINT ignored.
    subprocess.run(
        [
            "bash",
            "-c",
            f'setsid nohup "{sys.executable}" "{child}" "{status}" > /dev/null 2>&1 < /dev/null & '
            f'echo $! > "{pid_file}"',
        ],
        check=True,
    )
    assert wait_for(status, "ready") == "ready ignored=True\n"
    os.kill(int(pid_file.read_text()), stop)
    assert wait_for(status, "interrupted").endswith("interrupted\n")
