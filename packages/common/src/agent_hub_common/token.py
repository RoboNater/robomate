"""Pre-shared bearer token provisioning and comparison."""

from __future__ import annotations

import hmac
import os
import secrets
import stat
import sys
from pathlib import Path


class TokenError(RuntimeError):
    """Raised when a bearer token cannot be loaded safely."""


def _read_token(path: Path) -> str:
    try:
        permissions = stat.S_IMODE(path.stat().st_mode)
    except OSError as exc:
        raise TokenError(f"cannot inspect bearer token file: {path}") from exc
    # On Windows, stat().st_mode does not represent POSIX group/other mode bits
    # (0o077); Windows filesystem security is managed via NTFS ACLs. POSIX
    # permission validation is therefore only enforced on non-Windows platforms.
    if sys.platform != "win32" and (permissions & 0o077):
        raise TokenError(f"bearer token file must not be accessible by group or others: {path}")

    try:
        token = path.read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise TokenError(f"cannot read bearer token file: {path}") from exc
    if not token:
        raise TokenError(f"bearer token file is empty: {path}")
    return token


def load_or_create_token(explicit_token: str | None, token_file: Path) -> str:
    """Return an injected token, or atomically create/read a mode-0600 token file.

    On Windows, the token file's permissions are governed by NTFS ACLs rather than
    POSIX mode bits, so mode-0600 protection is unverified on that platform.
    """

    if explicit_token is not None:
        explicit_token = explicit_token.strip()
        if not explicit_token:
            raise TokenError("explicit bearer token cannot be empty")
        return explicit_token

    token_file.parent.mkdir(parents=True, exist_ok=True)
    generated = secrets.token_urlsafe(32)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    try:
        descriptor = os.open(token_file, flags, 0o600)
    except FileExistsError:
        return _read_token(token_file)
    except OSError as exc:
        raise TokenError(f"cannot create bearer token file: {token_file}") from exc

    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(f"{generated}\n")
            stream.flush()
            os.fsync(stream.fileno())
    except OSError as exc:
        token_file.unlink(missing_ok=True)
        raise TokenError(f"cannot write bearer token file: {token_file}") from exc
    return generated


def token_matches(candidate: str, expected: str) -> bool:
    """Compare bearer tokens without content-dependent early exit."""

    return hmac.compare_digest(candidate.encode(), expected.encode())
