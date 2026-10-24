"""Named branches for runs (M2 Step 7c, #315; mvp-spec §6).

The hub names the branch in a run's first implementer assignment,
`robomate/<hub>/<agent>/<label>`, with `<label>` derived from the statement
(issue number or a slug) and validated like a name. It is refused if the ref
exists locally or on origin, and later tasks of the run reuse it. The workflow
row records it (DB schema v26).
"""

from __future__ import annotations

import hashlib
import logging
import re
import subprocess
from pathlib import Path

logger = logging.getLogger(__name__)


class BranchError(RuntimeError):
    """A run branch cannot be named or reused for this assignment."""


_ISSUE_NUMBER_RE = re.compile(r"#(\d+)")


def derive_branch_label(statement: str) -> str:
    """Derive a branch `<label>` from a statement of work.

    An issue reference (`#315`, as rendered by `robomate submit --issue`) gives
    its number; anything else is slugified like a hub/agent name (lowercase,
    runs of other characters become `-`, cut to 40, stripped). A slug that is
    not a valid agent name (pattern or reserved word like `token`, `con`,
    `state`) and a statement with no usable text both fall back to the first
    8 hex of its SHA-256, so derivation always produces a valid label.
    """

    from agent_hub_common.discovery import validate_agent_name

    text = (statement or "").strip()
    match = _ISSUE_NUMBER_RE.search(text)
    if match:
        return match.group(1)
    slug = re.sub(r"[^a-z0-9_-]+", "-", text.lower())
    slug = re.sub(r"-{2,}", "-", slug).strip("-_")[:40].strip("-_")
    if slug and re.fullmatch(r"[a-z0-9]([a-z0-9_-]{0,38}[a-z0-9])?", slug):
        try:
            validate_agent_name(slug)
        except ValueError:
            pass
        else:
            return slug
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:8]


def branch_name(hub: str, agent: str, label: str) -> str:
    """Return `robomate/<hub>/<agent>/<label>`, validating each component like a name."""

    # Imported here so the module stays importable without the discovery
    # dependency at type-check time; the pattern is the same as hub/agent names.
    from agent_hub_common.discovery import validate_agent_name, validate_hub_name

    validate_hub_name(hub)
    validate_agent_name(agent)
    # Labels are validated like names (plan Step 7c).
    try:
        validate_agent_name(label)
    except ValueError as exc:
        raise BranchError(f"invalid branch label {label!r}: {exc}") from exc
    return f"robomate/{hub}/{agent}/{label}"


def _ref_exists(common: Path, ref: str) -> bool:
    """Whether `ref` exists in the repository at `common`.

    A git failure (missing binary, timeout, OSError) refuses rather than
    accepting silently: without a readable ref list the hub cannot guarantee
    the label is unique (r1-4).
    """

    try:
        completed = subprocess.run(
            ["git", f"--git-dir={common}", "show-ref", "--verify", ref],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            timeout=30,
        )
    except (subprocess.TimeoutExpired, OSError) as exc:
        logger.warning("cannot check ref %s in %s: %s", ref, common, exc)
        raise BranchError(f"cannot check whether branch ref {ref!r} exists: {exc}") from exc
    return completed.returncode == 0


def ensure_branch_available(common: Path | None, branch: str) -> None:
    """Refuse `branch` if its ref already exists locally or on origin.

    Local is `refs/heads/<branch>`; origin is the remote-tracking
    `refs/remotes/origin/<branch>`. With no repository context (`common is
    None`, e.g. test hubs without `hub_info`), there is nothing to check and
    the branch is accepted. Raises `BranchError` naming where the ref exists,
    or when the ref list cannot be read (fail closed, r1-4).
    """

    if common is None:
        return
    local_ref = f"refs/heads/{branch}"
    if _ref_exists(common, local_ref):
        raise BranchError(f"branch {branch!r} already exists locally ({local_ref})")
    origin_ref = f"refs/remotes/origin/{branch}"
    if _ref_exists(common, origin_ref):
        raise BranchError(f"branch {branch!r} already exists on origin ({origin_ref})")
