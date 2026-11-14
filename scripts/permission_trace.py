"""Bounded incremental parsing of a single child's private permission trace."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


def clean(value: str) -> str:
    return "".join(c for c in value if c.isprintable())[:2048]


class PermissionTrace:
    def __init__(self, path: Path, launch_id: str) -> None:
        self.path = path
        self.launch_id = launch_id
        self.offset = 0
        self.buffer = b""
        self.discarding = False
        self.pending: dict[tuple[str, str], dict[str, Any]] = {}

    def read(self) -> bool:
        """Return whether state changed; ignore incomplete and invalid records."""
        before = self.pending.copy()
        with self.path.open("rb") as stream:
            if self.path.stat().st_size < self.offset:
                # Never replay a rewritten trace as fresh evidence. Only bytes
                # appended after this reset can establish a new pending ask.
                stream.seek(0, 2)
                self.offset = stream.tell()
                self.buffer = b""
                self.discarding = False
                self.pending.clear()
            stream.seek(self.offset)
            data = stream.read(65536)
            self.offset = stream.tell()
        lines = (self.buffer + data).split(b"\n")
        self.buffer = lines.pop()
        if self.discarding and lines:
            lines.pop(0)
            self.discarding = False
        if self.discarding or len(self.buffer) > 32768:
            self.buffer = b""
            self.discarding = True
        for line in lines:
            if len(line) > 32768:
                continue
            try:
                row = json.loads(line)
            except (ValueError, UnicodeError):
                continue
            if not isinstance(row, dict) or row.get("launch_id") != self.launch_id:
                continue
            session, request = row.get("session"), row.get("request")
            if not isinstance(session, str) or not isinstance(request, str):
                continue
            if not session or not request or len(session) > 128 or len(request) > 128:
                continue
            key = (session, request)
            if row.get("type") == "permission.replied":
                self.pending.pop(key, None)
            elif row.get("type") == "permission.asked" and len(self.pending) < 128:
                permission, patterns = row.get("permission"), row.get("patterns")
                if not isinstance(permission, str) or not isinstance(patterns, list):
                    continue
                if not all(isinstance(p, str) for p in patterns):
                    continue
                self.pending[key] = {
                    "session": session,
                    "request": request,
                    "permission": clean(permission),
                    "patterns": [clean(p) for p in patterns[:8]],
                }
        return before != self.pending
