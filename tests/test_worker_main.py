import sys
from collections.abc import Callable
from io import StringIO
from pathlib import Path
from typing import Any, TextIO
from unittest.mock import AsyncMock, patch

import pytest
from agent_hub_common import ConfigurationError
from agent_hub_common.discovery import DiscoveryError
from conftest import closed_port_url, record_local_hub
from worker_mcp import WorkerSettings, main


def test_main_raises_on_missing_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("HUB_URL", raising=False)
    monkeypatch.delenv("HUB_TOKEN", raising=False)
    monkeypatch.delenv("AGENT_NAME", raising=False)

    with pytest.raises(ConfigurationError, match="HUB_URL must be set"):
        WorkerSettings.from_env()


def test_main_runs_server_and_reserves_stdout(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HUB_URL", "http://127.0.0.1:8420")
    monkeypatch.setenv("HUB_TOKEN", "test-token")
    monkeypatch.setenv("AGENT_NAME", "bob")

    stdout, stderr = StringIO(), StringIO()
    monkeypatch.setattr(sys, "stdout", stdout)
    monkeypatch.setattr(sys, "stderr", stderr)

    async def fake_run_worker(protocol_stdout: TextIO) -> None:
        assert protocol_stdout is stdout
        assert sys.stdout is stderr
        print("worker print goes to stderr")
        protocol_stdout.write('{"jsonrpc":"2.0"}\n')

    monkeypatch.setattr(main, "run_worker", fake_run_worker)

    main.main()
    assert stdout.getvalue() == '{"jsonrpc":"2.0"}\n'
    assert "worker print goes to stderr" in stderr.getvalue()
    assert sys.stdout is stdout


async def test_run_worker_initializes_client_and_server(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HUB_URL", "http://127.0.0.1:8420")
    monkeypatch.setenv("HUB_TOKEN", "test-token")
    monkeypatch.setenv("AGENT_NAME", "bob")

    with (
        patch("worker_mcp.main.WorkerHubClient") as mock_client_cls,
        patch("worker_mcp.main.create_worker_mcp") as mock_create_mcp,
        patch("worker_mcp.main.stdio_server") as mock_stdio_server,
    ):
        from unittest.mock import MagicMock

        mock_client = AsyncMock()
        mock_client_cls.return_value.__aenter__.return_value = mock_client
        mock_server = MagicMock()
        mock_server._mcp_server.run = AsyncMock()
        mock_server._mcp_server.create_initialization_options.return_value = {}
        mock_create_mcp.return_value = mock_server

        mock_read, mock_write = AsyncMock(), AsyncMock()
        mock_stdio_server.return_value.__aenter__.return_value = (mock_read, mock_write)

        fake_out = StringIO()
        await main.run_worker(fake_out)

        mock_client_cls.assert_called_once()
        mock_create_mcp.assert_called_once_with(mock_client)
        mock_stdio_server.assert_called_once()
        assert mock_stdio_server.call_args.kwargs["stdin"] is not None
        assert mock_stdio_server.call_args.kwargs["stdout"] is not None
        mock_server._mcp_server.run.assert_awaited_once()


@pytest.mark.parametrize(
    ("answers", "error"),
    [
        ("expected-hub", None),
        ("different-hub", "answers as hub different-hub, not expected-hub"),
        (None, "is not running"),
    ],
)
async def test_worker_bridge_checks_the_local_hub_identity_before_connecting(
    tmp_path: Path,
    healthz: Callable[[str], str],
    monkeypatch: pytest.MonkeyPatch,
    answers: str | None,
    error: str | None,
) -> None:
    """#147 r1-1: the worker bridge's first tool call verifies the hub it discovered."""

    for key in ("ROBOMATE_HUB_URL", "ROBOMATE_TOKEN", "ROBOMATE_TOKEN_FILE"):
        monkeypatch.delenv(key, raising=False)
    url = healthz(answers) if answers else closed_port_url()
    monkeypatch.chdir(record_local_hub(tmp_path / "checkout", url, "expected-hub"))
    connected: list[str] = []
    tools: dict[str, Any] = {}

    class FakeClient:
        def __init__(self, settings: WorkerSettings) -> None:
            connected.append(settings.hub_url.rstrip("/"))

        async def __aenter__(self) -> "FakeClient":
            return self

        async def close(self) -> None:
            pass

    def capture(get_client: Any, server_name: str) -> object:
        tools["get_client"] = get_client
        return object()

    async def serve(_server: object, _stdout: object) -> None:
        if error is None:
            await tools["get_client"]()
        else:
            with pytest.raises(DiscoveryError, match=error):
                await tools["get_client"]()

    monkeypatch.setattr(main, "WorkerHubClient", FakeClient)
    monkeypatch.setattr(main, "create_worker_mcp", capture)
    monkeypatch.setattr(main, "serve_mcp", serve)
    await main.run_worker_bridge(StringIO(), name="bob", harness="claude-code")
    assert connected == ([url] if error is None else [])
