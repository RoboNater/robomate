"""Role guides served as static markdown (spec §4.2).

`get_role_guide` is the runtime-agnostic replacement for Claude Code skills: a
worker on any runtime fetches the same text over HTTP, so this route is what
the provider-agnostic design rests on rather than a convenience. The route is
bearer-protected like every other non-discovery route (§4.1).
"""

from __future__ import annotations

import re
from pathlib import Path

from fastapi import HTTPException, Response, status

# Guide names are role names as they appear in `assign_task(role=...)` —
# lowercase slugs. Constraining the name to that alphabet is also what keeps a
# request from naming a path: no separators, no dot segments, no absolute
# paths. Documentation that lives alongside the guides (README.md) is not a
# role and is not served.
# Matched with `fullmatch`: `$` alone would also accept a trailing newline,
# which a percent-encoded request can carry.
ROLE_PATTERN = re.compile(r"[a-z][a-z0-9-]*")

MEDIA_TYPE = "text/markdown; charset=utf-8"


def _not_found(role: str) -> HTTPException:
    return HTTPException(status.HTTP_404_NOT_FOUND, detail=f"no guide for role {role!r}")


def guide_response(guides_dir: Path, role: str, forge: str | None = None) -> Response:
    """Serve `{role}.md` from the guides directory, optionally composed with forge appendix."""

    if not ROLE_PATTERN.fullmatch(role):
        raise _not_found(role)

    # Both sides are resolved before comparing so that a symlink inside the
    # directory cannot hand out a file from outside it.
    root = guides_dir.resolve()
    path = (root / f"{role}.md").resolve()
    if not path.is_relative_to(root) or not path.is_file():
        raise _not_found(role)

    content = path.read_bytes()
    if forge and ROLE_PATTERN.fullmatch(forge):
        appendix_path = (root / "forge" / f"{forge}.md").resolve()
        if appendix_path.is_file() and appendix_path.is_relative_to(root):
            content = content + b"\n\n" + appendix_path.read_bytes()

    return Response(content=content, media_type=MEDIA_TYPE)
