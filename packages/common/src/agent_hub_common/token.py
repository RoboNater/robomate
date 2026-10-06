"""Pre-shared bearer and operator token provisioning and comparison."""

from __future__ import annotations

import hmac
import os
import secrets
import stat
import sys
import time
from pathlib import Path


class TokenError(RuntimeError):
    """Raised when a bearer token cannot be loaded safely."""


class _EmptyTokenFileError(TokenError):
    """The token file exists but holds nothing yet, or never will."""


def _read_token(path: Path, label: str = "bearer token") -> str:
    try:
        permissions = stat.S_IMODE(path.stat().st_mode)
    except OSError as exc:
        raise TokenError(f"cannot inspect {label} file: {path}") from exc
    # On Windows, stat().st_mode does not represent POSIX group/other mode bits
    # (0o077); Windows filesystem security is managed via NTFS ACLs. POSIX
    # permission validation is therefore only enforced on non-Windows platforms.
    if sys.platform != "win32" and (permissions & 0o077):
        raise TokenError(f"{label} file must not be accessible by group or others: {path}")

    try:
        token = path.read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise TokenError(f"cannot read {label} file: {path}") from exc
    if not token:
        raise _EmptyTokenFileError(f"{label} file is empty: {path}")
    return token


def read_token_file(token_file: Path, label: str) -> str:
    """Read an existing owner-only token file, refusing a loose or empty one."""

    return _read_token(token_file, label)


# How long a reader waits for a concurrent creator to finish writing.
CREATION_GRACE_S = 2.0


def _read_created_token(path: Path, label: str) -> str:
    """Read a token another process may have created but not yet written.

    The operator token is shared by every hub on the machine, so two `up`s can
    race to create it: the loser of O_EXCL can find the file still empty.
    """

    deadline = time.monotonic() + CREATION_GRACE_S
    while True:
        try:
            return _read_token(path, label)
        except _EmptyTokenFileError:
            # Only emptiness can mean a creator mid-write, and it may finish
            # at any moment after the read: re-read rather than inspect the
            # file again. Any other TokenError is final at once.
            if time.monotonic() >= deadline:
                raise
        time.sleep(0.02)


def load_or_create_token(
    explicit_token: str | None, token_file: Path, *, label: str = "bearer token"
) -> str:
    """Return an injected token, or atomically create/read a mode-0600 token file.

    On Windows, the token file's permissions are governed by NTFS ACLs rather than
    POSIX mode bits, so mode-0600 protection is unverified on that platform.
    """

    if explicit_token is not None:
        explicit_token = explicit_token.strip()
        if not explicit_token:
            raise TokenError(f"explicit {label} cannot be empty")
        return explicit_token

    token_file.parent.mkdir(parents=True, exist_ok=True)
    generated = secrets.token_urlsafe(32)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    try:
        descriptor = os.open(token_file, flags, 0o600)
    except FileExistsError:
        return _read_created_token(token_file, label)
    except OSError as exc:
        raise TokenError(f"cannot create {label} file: {token_file}") from exc

    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(f"{generated}\n")
            stream.flush()
            os.fsync(stream.fileno())
    except OSError as exc:
        token_file.unlink(missing_ok=True)
        raise TokenError(f"cannot write {label} file: {token_file}") from exc
    return generated


def token_matches(candidate: str, expected: str) -> bool:
    """Compare bearer tokens without content-dependent early exit."""

    return hmac.compare_digest(candidate.encode(), expected.encode())
