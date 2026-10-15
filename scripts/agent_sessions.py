"""Conversation ID syntax shared by guarded recovery and hub-free questions."""

import re
from pathlib import Path


def exact_id(harness: str, value: str) -> bool:
    if harness in ("claude-code", "codex"):
        return (
            re.fullmatch(r"[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}", value) is not None
        )
    if harness == "opencode":
        return re.fullmatch(r"ses_[A-Za-z0-9]+", value) is not None
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
