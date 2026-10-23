"""rbm-bench run driver: a run stops cleanly when interrupted, however it was launched."""

from __future__ import annotations

import importlib.util
import json
import os
import shutil
import signal
import subprocess
import sys
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

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


SPEC = importlib.util.spec_from_file_location("rbm_bench_run_case", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
RUN_CASE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(RUN_CASE)


@pytest.fixture
def signal_handlers() -> Iterator[None]:
    saved = {sig: signal.getsignal(sig) for sig in (signal.SIGINT, signal.SIGTERM)}
    yield
    for sig, handler in saved.items():
        signal.signal(sig, handler)


@pytest.mark.skipif(os.name == "nt", reason="POSIX process groups")
def test_interrupt_while_the_hub_starts_stops_it_and_writes_a_summary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, signal_handlers: None
) -> None:
    case = tmp_path / "case.json"
    case.write_text(json.dumps({"id": "review-1-r1", "kind": "review"}), encoding="utf-8")
    eval_root = tmp_path / "root"
    real_popen = subprocess.Popen
    hubs: list[subprocess.Popen[bytes]] = []
    downs: list[Path] = []

    def clone(command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        Path(command[-1]).mkdir(parents=True)
        return subprocess.CompletedProcess(command, 0)

    def hub(command: list[str], **kwargs: Any) -> subprocess.Popen[bytes]:
        # A harmless stand-in for `robomate up`, started the same way.
        hubs.append(real_popen([sys.executable, "-c", "import time; time.sleep(60)"], **kwargs))
        return hubs[-1]

    def readiness(hub_dir: Path) -> dict[str, Any]:
        os.kill(os.getpid(), signal.SIGINT)  # the operator stops the run mid-start
        time.sleep(1)
        return {}

    def robomate(hub_dir: Path, *args: str) -> subprocess.CompletedProcess[str]:
        if args == ("down",):
            downs.append(hub_dir)
        return subprocess.CompletedProcess(["robomate", *args], 0)

    monkeypatch.setattr(RUN_CASE, "TOOLING", tmp_path / "tooling")
    monkeypatch.setattr(RUN_CASE, "seed", lambda case, name, source: {"ssh_url": "git@h:a/b.git"})
    monkeypatch.setattr(RUN_CASE.subprocess, "run", clone)
    monkeypatch.setattr(RUN_CASE.subprocess, "Popen", hub)
    monkeypatch.setattr(RUN_CASE, "hub_status", readiness)
    monkeypatch.setattr(RUN_CASE, "robomate", robomate)

    with pytest.raises(KeyboardInterrupt):
        RUN_CASE.main([str(case), "--label", "t", "--eval-root", str(eval_root)])

    assert len(hubs) == 1 and hubs[0].poll() is not None, "the hub outlived the runner"
    assert downs, "robomate down was not called"
    (results,) = (eval_root / "results").iterdir()
    summary = json.loads((results / "summary.json").read_text(encoding="utf-8"))
    assert summary["error"] == "interrupted"
