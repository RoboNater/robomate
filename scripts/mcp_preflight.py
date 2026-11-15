"""Check the static user-level MCP entry used by joined agents."""

from __future__ import annotations

import json
import os
import re
import sys
import tomllib
from pathlib import Path


def _jsonc(text: str) -> object:
    # Preserve strings while removing JSONC comments and trailing commas.
    token = re.compile(r'"(?:\\.|[^"\\])*"|//[^\n]*|/\*[\s\S]*?\*/')
    text = token.sub(lambda m: m.group() if m.group().startswith('"') else "", text)
    token = re.compile(r'"(?:\\.|[^"\\])*"|,\s*(?=[}\]])')
    return json.loads(token.sub(lambda m: m.group() if m.group().startswith('"') else "", text))


def check(harness: str) -> None:
    """Refuse a missing entry; warn when its presence cannot be determined."""
    home = Path.home()
    configs = {
        "codex": (
            [Path(os.environ.get("CODEX_HOME") or home / ".codex") / "config.toml"],
            ("mcp_servers", "robomate"),
            "[mcp_servers.robomate]",
        ),
        "claude-code": (
            [home / ".claude.json"],
            ("mcpServers", "robomate"),
            '"mcpServers.robomate"',
        ),
        "opencode": (
            [
                Path(os.environ.get("XDG_CONFIG_HOME") or home / ".config")
                / "opencode"
                / f"opencode.{ext}"
                for ext in ("json", "jsonc")
            ],
            ("mcp", "robomate"),
            '"mcp.robomate"',
        ),
        "antigravity": (
            [home / ".gemini" / "config" / "mcp_config.json"],
            ("mcpServers", "robomate"),
            '"mcpServers.robomate"',
        ),
    }
    if harness not in configs:
        print(
            f"warning: {harness}: cannot check the user-level robomate MCP entry; "
            "see docs/mcp-setup-prompt.md",
            file=sys.stderr,
        )
        return
    paths, keys, entry = configs[harness]
    try:
        found = False
        for path in paths:
            if not path.exists():
                continue
            raw = path.read_text(encoding="utf-8")
            data = tomllib.loads(raw) if harness == "codex" else _jsonc(raw)
            for key in keys:
                if not isinstance(data, dict):
                    raise ValueError(f"invalid configuration structure in {path}")
                data = data.get(key)
                if data is None:
                    break
            # Nested tool/env tables alone create an implicit TOML table, but
            # do not register a Codex server transport.
            if harness == "codex" and isinstance(data, dict):
                data = data if "command" in data or "url" in data else None
            if data is not None:
                found = True
        if not found:
            raise FileNotFoundError
    except FileNotFoundError:
        raise ValueError(
            f"{harness}: missing {entry} in user-level config ({', '.join(map(str, paths))}); "
            f"add {entry} running `uv run --locked --project <stable-robomate-checkout> "
            f"robomate mcp --harness {harness}`; see docs/mcp-setup-prompt.md"
        ) from None
    except (OSError, ValueError) as exc:
        print(f"warning: {harness}: cannot check user-level {entry}: {exc}", file=sys.stderr)
