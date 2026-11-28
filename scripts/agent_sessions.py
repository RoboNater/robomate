"""Conversation ID syntax and selection shared by guarded recovery and hub-free questions."""

import json
import re
from pathlib import Path


def exact_id(harness: str, value: str) -> bool:
    if harness in ("claude-code", "codex"):
        return (
            re.fullmatch(r"[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}", value) is not None
        )
    if harness == "opencode":
        return re.fullmatch(r"ses_[A-Za-z0-9]+", value) is not None
    if harness == "devin":
        # Documented examples are short names and hex prefixes (``brisk-otter``,
        # ``abc12345``); the exact format awaits a live run (#224).
        return re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{2,127}", value) is not None
    return re.fullmatch(r'[^\s"]{6,128}', value) is not None and not value.startswith("-")


def conversation(sessions: Path, agent: str, harness: str, supplied: str | None) -> str:
    """Select the last recorded exact ID, without changing any run files."""
    if supplied is not None:
        if not exact_id(harness, supplied):
            raise ValueError("Invalid conversation ID; supply an exact harness ID, never -c/--last")
        return supplied
    try:
        lines = sessions.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        lines = []
    ids: list[str] = []
    for line in lines:
        try:
            row = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ValueError(
                f"Session log {sessions} is malformed; supply an explicit ID to bypass discovery"
            ) from exc
        if not isinstance(row, dict) or row.get("agent") != agent or row.get("harness") != harness:
            raise ValueError(
                f"Session log {sessions} is malformed or belongs to another agent; "
                "supply an explicit ID to bypass discovery"
            )
        value = row.get("conversation_id")
        if value is None:
            continue
        if not isinstance(value, str) or not exact_id(harness, value):
            raise ValueError(f"Invalid saved conversation ID in {sessions}")
        # A conversation may appear repeatedly or again after a newer start.
        if value in ids:
            ids.remove(value)
        ids.append(value)
    if not ids:
        raise ValueError(f"No saved conversation ID in {sessions}; supply an explicit ID")
    if len(ids) > 1:
        print("Other recorded conversations: " + ", ".join(ids[:-1]), flush=True)
    return ids[-1]
