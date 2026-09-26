import os
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
SCRIPTS = [
    ROOT / "scripts" / "poc" / "prepare-step5-demo.sh",
    ROOT / "scripts" / "poc" / "launch-step5-alice.sh",
    ROOT / "scripts" / "poc" / "launch-step5-workers.sh",
    ROOT / "scripts" / "poc" / "verify-step5-demo.sh",
]


def test_step5_launch_scripts_are_executable_and_parse() -> None:
    for script in SCRIPTS:
        assert os.access(script, os.X_OK), script
        parsed = subprocess.run(
            ["bash", "-n", script.as_posix()],
            check=False,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
        )
        assert parsed.returncode == 0, parsed.stderr


def test_alice_launcher_is_interactive_and_uses_the_checked_in_skill() -> None:
    text = (ROOT / "scripts" / "poc" / "launch-step5-alice.sh").read_text(encoding="utf-8")

    assert 'skills/alice-orchestrator"' in text
    assert "exec claude" in text
    assert "exec claude -p" not in text and "--print" not in text
    assert "--strict-mcp-config" in text
    assert "Bash(gh *)" in text


def test_prepare_and_verify_keep_evidence_outside_the_checkout() -> None:
    prepare = (ROOT / "scripts" / "poc" / "prepare-step5-demo.sh").read_text(encoding="utf-8")
    verify = (ROOT / "scripts" / "poc" / "verify-step5-demo.sh").read_text(encoding="utf-8")

    assert "RUN_DIR" in prepare
    assert "--seed" in prepare
    assert "--allow-repeat" in prepare
    assert "--verify" in verify
    assert '"$run_dir/evidence.json"' in verify
