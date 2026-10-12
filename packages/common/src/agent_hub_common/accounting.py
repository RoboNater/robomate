"""The per-call accounting row the hub persists and the stdio bridge ships (#78).

Here rather than in `agent_hub.accounting` so that worker-mcp can build rows
without importing the hub server (#47). A record carries byte counts and
closed-set labels only, never payload text.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

# Hub identifiers are uuid4 hex; anything else is not one of ours and is
# dropped rather than copied into the accounting.
_ID_RE = re.compile(r"^[0-9a-f]{32}$")


def hub_id(value: Any) -> str | None:
    """Return `value` if it is a hub-generated id, else None."""

    return value if isinstance(value, str) and _ID_RE.fullmatch(value) else None


@dataclass(slots=True)
class CallRecord:
    """One call's accounting: sizes and closed-set labels only."""

    boundary: str
    actor: str
    tool: str
    outcome: str
    bytes_in: int
    bytes_out: int
    started: str
    finished: str
    status: int | None = None
    content_bytes: int | None = None
    repeat_bytes: int | None = None
    task_id: str | None = None
