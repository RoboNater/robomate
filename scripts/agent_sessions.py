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


def ask_hint(sessions: Path, agent: str) -> str:
    """Final line on recovery refusals, including remote worker bundles."""
    script = sessions.parent / f"ask-{agent}.sh"
    return (
        "If the run is over and you only want to ask this agent questions, "
        f"run {script} (.ps1 on Windows); it doesn't need the hub."
    )


def ask_hint_from_args(argv: list[str], default_agent: str = "alice") -> str | None:
    """Keep parser refusals useful even before recovery has a parsed namespace."""
    try:
        sessions = Path(argv[argv.index("--sessions") + 1])
        agent = argv[argv.index("--agent") + 1] if "--agent" in argv else default_agent
    except (ValueError, IndexError):
        return None
    return ask_hint(sessions, agent)


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
