import json
import os
import subprocess
import sys
from pathlib import Path

import pytest


@pytest.mark.skipif(
    sys.platform == "win32",
    reason="POSIX bash supervisor is not supported on Windows",
)
def test_supervisor_reprompts_one_persistent_session_until_release(tmp_path: Path) -> None:
    fake_claude = tmp_path / "fake-claude"
    input_log = tmp_path / "inputs.jsonl"
    telemetry = tmp_path / "worker.jsonl"
    telemetry.write_text(
        '{"event":"tool_call","phase":"success","outcome": "release"}\n',
        encoding="utf-8",
    )
    mcp_config = tmp_path / "mcp.json"
    mcp_config.write_text("{}", encoding="utf-8")
    fake_claude.write_text(
        """#!/usr/bin/env bash
set -eu
printf '%s\\n' "$@" >> "$SUPERVISOR_ARGV_LOG"
count=0
release_event='{"event":"tool_call","phase":"success","outcome": "release"}'
while IFS= read -r line; do
  printf '%s\\n' "$line" >> "$SUPERVISOR_INPUT_LOG"
  count=$((count + 1))
  if [ "$count" -eq 2 ]; then
    printf '%s\\n' "$release_event" >> "$HUB_TELEMETRY_LOG"
  fi
  printf '%s\\n' '{"type":"result","subtype":"success"}'
  if [ "$count" -eq 2 ]; then
    exit 0
  fi
done
""",
        encoding="utf-8",
    )
    fake_claude.chmod(0o755)
    argv_log = tmp_path / "argv.log"
    prompt_file = tmp_path / "worker.prompt.md"
    # CRLF + tab content, as rendered from a Windows checkout: every byte must
    # survive the stream-json round trip or Claude dies with a SyntaxError.
    prompt_file.write_bytes(b"You are bob.\r\nUse\tTabs.\r\n")
    script = Path(__file__).resolve().parents[1] / "scripts" / "supervise-claude-code.sh"
    env = os.environ | {
        "CLAUDE_BIN": str(fake_claude),
        "CLAUDE_MCP_CONFIG": str(mcp_config),
        "HUB_TELEMETRY_LOG": str(telemetry),
        "CLAUDE_WORKER_PROMPT_FILE": str(prompt_file),
        "CLAUDE_REPROMPT_DELAY_S": "0",
        "CLAUDE_MAX_REPROMPTS": "2",
        "SUPERVISOR_INPUT_LOG": str(input_log),
        "SUPERVISOR_ARGV_LOG": str(argv_log),
    }

    completed = subprocess.run(
        [str(script)],
        env=env,
        text=True,
        capture_output=True,
        timeout=5,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    assert "sending supervisor reprompt 1" in completed.stderr
    assert "release observed after 1 supervisor reprompt" in completed.stderr
    # Option values use `=` form: space-separated --mcp-config/--tools/
    # --allowedTools mis-parse on Windows Claude in -p mode (PR #81 acceptance).
    argv = argv_log.read_text(encoding="utf-8").splitlines()
    assert argv[0] == "-p"
    for flag in ("--mcp-config=", "--tools=", "--allowedTools="):
        assert any(token.startswith(flag) for token in argv), argv
    assert "--mcp-config" not in argv and "--tools" not in argv
    assert "--allowedTools" not in argv
    messages = input_log.read_text(encoding="utf-8").splitlines()
    assert len(messages) == 2
    prompts = [json.loads(message)["message"]["content"][0]["text"] for message in messages]
    # Command substitution strips the file's trailing newline, but every other
    # byte (CR, TAB, quotes) must survive the stream-json round trip exactly.
    assert prompts[0] == "You are bob.\r\nUse\tTabs.\r"
    assert "Continue the unattended worker loop" in prompts[1]
    assert "path/URL" in prompts[1]
