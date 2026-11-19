#!/usr/bin/env python3
"""Regenerate the worker prompt's inlined etiquette from its canonical guide."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
GUIDE = ROOT / "guides" / "worker.md"
PROMPT = ROOT / "prompts" / "worker.md"
# Keep this prompt-only preamble here; the rest of the prompt mirrors GUIDE.
PREFIX = (
    b"You are `$AGENT_NAME`, a persistent robo-agents worker. Follow the inlined\n"
    b"worker etiquette below. Check in, pull assignments, and continue until Alice\n"
    b"releases you.\n"
    b"\n"
)
FIX_COMMAND = "uv run --locked python scripts/sync-worker-prompt.py"


def normalize_newlines(content: bytes) -> bytes:
    """Normalize CRLF copies left in existing Windows checkouts for comparison."""

    return content.replace(b"\r\n", b"\n")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check",
        action="store_true",
        help="fail if prompts/worker.md does not match the guide",
    )
    args = parser.parse_args()

    expected = PREFIX + GUIDE.read_bytes()
    if args.check:
        if PROMPT.is_file() and normalize_newlines(PROMPT.read_bytes()) == expected:
            return 0
        print(
            f"prompts/worker.md is out of date; run `{FIX_COMMAND}`.",
            file=sys.stderr,
        )
        return 1

    PROMPT.write_bytes(expected)
    return 0


if __name__ == "__main__":
    sys.exit(main())
