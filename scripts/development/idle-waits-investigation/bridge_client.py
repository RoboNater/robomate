"""Shared helpers for the #497 baseline scripts: drive `robomate mcp` over stdio.

Reads SMOKE_CLONE, ROBOMATE_HUB_URL and ROBOMATE_TOKEN_FILE from the environment.
"""

from __future__ import annotations

import json
import os
import time
from contextlib import AsyncExitStack
from typing import Any

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

T0 = time.monotonic()


def log(*parts: Any) -> None:
    stamp = time.strftime("%T", time.gmtime())
    print(f"[+{time.monotonic() - T0:7.1f}s {stamp}]", *parts, flush=True)


def bridge_env(extra: dict[str, str] | None = None) -> dict[str, str]:
    """This process's environment without identity variables, plus `extra`."""

    env = {k: v for k, v in os.environ.items() if not k.startswith(("HUB_", "AGENT_"))}
    return env | (extra or {})


async def open_bridge(stack: AsyncExitStack, env: dict[str, str] | None = None) -> ClientSession:
    params = StdioServerParameters(
        command="uv",
        args=[
            "run",
            "--locked",
            "--project",
            os.environ["SMOKE_CLONE"],
            "robomate",
            "mcp",
            "--harness",
            "baseline",
        ],
        env=env or bridge_env(),
    )
    read, write = await stack.enter_async_context(stdio_client(params))
    session = await stack.enter_async_context(ClientSession(read, write))
    await session.initialize()
    return session


def result_text(result: Any) -> str:
    return " ".join(getattr(part, "text", "") for part in result.content)


async def call(session: ClientSession, who: str, tool: str, **args: Any) -> Any:
    """Call a tool, log its outcome and elapsed time, return the decoded result or None."""

    started = time.monotonic()
    try:
        result = await session.call_tool(tool, args)
    except Exception as exc:  # noqa: BLE001 - the failure is the observation
        log(f"{who} {tool} raised after {time.monotonic() - started:.1f}s: {exc!r}")
        return None
    text = result_text(result)
    flag = "ERROR " if result.isError else ""
    elapsed = time.monotonic() - started
    log(f"{who} {tool}({json.dumps(args)[:120]}) -> {flag}after {elapsed:.1f}s: {text[:500]!r}")
    if result.isError:
        return None
    try:
        return json.loads(text)
    except ValueError:
        return None
