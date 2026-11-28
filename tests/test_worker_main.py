import sys
from io import StringIO
from typing import TextIO

import pytest
from worker_mcp import main


def test_main_runs_server_and_reserves_stdout(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HUB_URL", "http://127.0.0.1:8420")
    monkeypatch.setenv("HUB_TOKEN", "test-token")
    monkeypatch.setenv("AGENT_NAME", "bob")

    stdout, stderr = StringIO(), StringIO()
    monkeypatch.setattr(sys, "stdout", stdout)
    monkeypatch.setattr(sys, "stderr", stderr)

    async def fake_run_worker_bridge(protocol_stdout: TextIO) -> None:
        assert protocol_stdout is stdout
        assert sys.stdout is stderr
        print("worker print goes to stderr")
        protocol_stdout.write('{"jsonrpc":"2.0"}\n')

    monkeypatch.setattr(main, "run_worker_bridge", fake_run_worker_bridge)

    main.main()
    assert stdout.getvalue() == '{"jsonrpc":"2.0"}\n'
    assert "worker print goes to stderr" in stderr.getvalue()
    assert sys.stdout is stdout
