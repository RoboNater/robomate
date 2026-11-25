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
            [Path(os.environ.get("CLAUDE_CONFIG_DIR") or home) / ".claude.json"],
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
    found = False
    uncertain = False
    for path in paths:
        try:
            raw = path.read_text(encoding="utf-8")
            data = tomllib.loads(raw) if harness == "codex" else _jsonc(raw)
            key_paths = (
                [("mcp", "servers", "robomate"), ("mcp", "robomate")]
                if harness == "opencode"
                else [keys]
            )
            for candidate_keys in key_paths:
                node: object = data
                for key in candidate_keys:
                    if not isinstance(node, dict):
                        raise ValueError(f"invalid configuration structure in {path}")
                    node = node.get(key)
                    if node is None:
                        break
                # Nested tool/env tables alone create an implicit TOML table, but
                # do not register a Codex server transport.
                if isinstance(node, dict) and (
                    harness != "codex" or "command" in node or "url" in node
                ):
                    found = True
                    break
        except FileNotFoundError:
            continue
        except (OSError, ValueError) as exc:
            uncertain = True
            print(f"warning: {harness}: cannot check user-level {entry}: {exc}", file=sys.stderr)
    if not found and not uncertain:
        raise ValueError(
            f"{harness}: missing {entry} in user-level config ({', '.join(map(str, paths))}); "
            f"add {entry} running `uv run --locked --project <stable-robomate-checkout> "
            f"robomate mcp --harness {harness}`; see docs/mcp-setup-prompt.md"
        )
