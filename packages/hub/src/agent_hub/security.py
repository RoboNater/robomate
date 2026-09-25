"""Bearer enforcement for the hub's protected routes.

Spec §4.1 fixes the public surface at two routes — the agent card, which has to
be fetchable before a client holds anything, and `/healthz`, which runs before
credentials exist. Everything else goes through `require_bearer`.
"""

from __future__ import annotations

from agent_hub_common import token_matches
from fastapi import HTTPException, Request, status

BEARER_SCHEME = "bearer"
CHALLENGE = {"WWW-Authenticate": "Bearer"}


def _unauthorized(detail: str) -> HTTPException:
    return HTTPException(status.HTTP_401_UNAUTHORIZED, detail=detail, headers=CHALLENGE)


def require_bearer(request: Request) -> None:
    """Reject a request that does not carry the pre-shared token."""

    expected = getattr(request.app.state, "bearer_token", None)
    if not isinstance(expected, str):  # pragma: no cover - lifespan always sets it
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE, detail="bearer token is not loaded yet"
        )

    header = request.headers.get("authorization")
    if header is None:
        raise _unauthorized("missing Authorization header")
    scheme, _, candidate = header.partition(" ")
    if scheme.lower() != BEARER_SCHEME or not candidate.strip():
        raise _unauthorized("Authorization header must be 'Bearer <token>'")
    if not token_matches(candidate.strip(), expected):
        raise _unauthorized("invalid bearer token")
