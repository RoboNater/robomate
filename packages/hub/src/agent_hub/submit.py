"""`robomate submit` intake: policy defaults, forge preflight, queue and event (M2 Step 4, #241)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from agent_hub_common import MAX_STATEMENT_BYTES, EventKind, WorkflowPolicy, load_config_layers
from pydantic import ValidationError

from .forge_preflight import (
    GhRunner,
    github_policy_checks,
    gitlab_policy_checks,
    gitlab_preflight,
    run_gh,
)
from .gitlab_gate import GlabRunner, run_glab
from .store import ConflictError, HubStore, PayloadTooLargeError


def _allow_no_ci_str(value: Any) -> str:
    """Normalize a policy `allow_no_ci` (bool or `"auto"`) to the preflight string."""

    if isinstance(value, bool):
        return "true" if value else "false"
    if value == "auto":
        return "auto"
    raise ValueError(f"unknown allow_no_ci={value!r}; want one of auto, true, false")


def _apply_policy_override(base: dict[str, Any], key: str, value: Any) -> None:
    """Apply one `--policy k=v` override to a policy dict, nesting dotted keys."""

    parts = key.split(".")
    # Strip a leading `policy.` prefix: `--policy policy.max_review_rounds=5`.
    if parts and parts[0] == "policy":
        parts = parts[1:]
    if not parts or not all(part.strip() for part in parts):
        raise ValueError(f"invalid --policy key {key!r}")
    # Bare role_policy keys nest under `role_policy`.
    role_keys = {
        "reviewer_harness_differs",
        "reviewer_provider_differs",
        "implementer_capabilities",
        "reviewer_capabilities",
    }
    if len(parts) == 1 and parts[0] in role_keys:
        parts = ["role_policy", parts[0]]
    # `policy.role_policy.x` after stripping `policy.` becomes `role_policy.x`; good.
    target: dict[str, Any] = base
    for part in parts[:-1]:
        existing = target.get(part)
        if existing is None:
            existing = {}
            target[part] = existing
        if not isinstance(existing, dict):
            raise ValueError(f"invalid --policy key {key!r}: {part!r} is not a table")
        target = existing
    target[parts[-1]] = value


def _resolve_policy(
    resolved_policy: WorkflowPolicy, overrides: dict[str, Any] | None
) -> dict[str, Any]:
    """Merge configuration-layer defaults with `--policy` overrides and validate."""

    base: dict[str, Any] = resolved_policy.model_dump(mode="json")
    if overrides:
        for key, value in overrides.items():
            if not isinstance(key, str) or not key.strip():
                raise ValueError("policy override keys must be non-empty strings")
            _apply_policy_override(base, key.strip(), value)
    try:
        validated = WorkflowPolicy.model_validate(base)
    except ValidationError as exc:
        details = "; ".join(
            f"{'.'.join(str(item) for item in error['loc'])}: {error['msg']}"
            for error in exc.errors()
        )
        raise ValueError(f"invalid workflow policy: {details}") from exc
    return validated.model_dump(mode="json")


async def submit_statement(
    store: HubStore,
    hub_info: Any,
    statement: str,
    policy_overrides: dict[str, Any] | None,
    source: str,
    *,
    glab_runner: GlabRunner = run_glab,
    gh_runner: GhRunner = run_gh,
) -> dict[str, Any]:
    """Queue one statement of work and emit `work_submitted` (M2 Step 4).

    - Stores bytes verbatim with SHA-256 (size-capped so `get_statement` fits
      one 16 KiB part); over-cap raises `PayloadTooLargeError` with the cap
      and remedy, and queues nothing.
    - Resolves policy from configuration layers plus `--policy` overrides;
      `submit` without overrides takes the repository layer (e.g.
      `max_review_rounds`).
    - Runs policy-dependent forge checks from the hub's shared functions;
      strict refuse on any base refuse (incl. Unsupported settings); typed CI
      `pass`/`note`/`refuse` (explicit skips probe, auto non-404 refuses naming
      the check, 404-only-in-args is not absence); GitHub runs no glab.
    - Hub without origin refuses; `allow_no_ci="auto"` resolves to bool for
      storage (merge gate expects bool).
    """

    if not isinstance(statement, str) or not statement.strip():
        raise ValueError("statement must not be empty")
    data = statement.encode("utf-8")
    if len(data) > MAX_STATEMENT_BYTES:
        raise PayloadTooLargeError(
            f"statement is {len(data)} bytes; maximum is {MAX_STATEMENT_BYTES} bytes "
            "so it fits in one 16 KiB message part with its envelope. Shorten the "
            "statement, or put the detail in an issue and submit that instead."
        )
    info = dict(hub_info or {})
    origin = str(info.get("origin") or "").strip()
    if not origin:
        raise ConflictError(
            "hub has no origin recorded; restart with `robomate up` which records origin "
            "(submit cannot bind the forge gate without it)"
        )
    forge = str(info.get("forge") or "github")
    # Policy defaults from layers; hub config + repository + user, first match wins.
    try:
        state_dir = store._state_dir()
    except Exception:
        state_dir = store.path.parent
    common_raw = info.get("git_common_dir")
    common_dir = Path(str(common_raw)) if common_raw else None
    try:
        layers = load_config_layers(
            hub_config=state_dir / "config.toml",
            git_common_dir=common_dir,
        )
    except Exception as exc:
        raise ValueError(f"invalid configuration: {exc}") from exc
    try:
        merged = _resolve_policy(layers.policy, policy_overrides)
    except ValueError as exc:
        raise ValueError(str(exc)) from exc
    merge_method = str(merged.get("merge_method") or "squash")
    try:
        allow_no_ci_input = _allow_no_ci_str(merged.get("allow_no_ci", False))
    except ValueError as exc:
        raise ValueError(str(exc)) from exc
    # Forge preflight (strict): any `refuse` in base or policy checks refuses.
    ci_status: str | None = None
    if forge == "gitlab":
        base_checks = await gitlab_preflight(origin, runner=glab_runner)
        for check in base_checks:
            if check.status == "refuse":
                raise ConflictError(f"{check.name}: {check.detail}")
        policy_checks = await gitlab_policy_checks(
            origin, merge_method=merge_method, allow_no_ci=allow_no_ci_input, runner=glab_runner
        )
        for check in policy_checks:
            if check.status == "refuse":
                raise ConflictError(f"{check.name}: {check.detail}")
        for check in policy_checks:
            if check.name == "CI configuration":
                ci_status = check.status
                break
    else:
        policy_checks = await github_policy_checks(
            origin, merge_method=merge_method, allow_no_ci=allow_no_ci_input, runner=gh_runner
        )
        for check in policy_checks:
            if check.status == "refuse":
                raise ConflictError(f"{check.name}: {check.detail}")
        for check in policy_checks:
            if check.name == "CI configuration":
                ci_status = check.status
                break
    # Resolve `auto` to bool for storage (typed status, not detail text).
    if allow_no_ci_input == "auto":
        if ci_status == "pass":
            resolved_allow_no_ci = False
        elif ci_status == "note":
            resolved_allow_no_ci = True
        elif ci_status is None:
            # Local origin (no slug, no checks): no CI to gate on.
            resolved_allow_no_ci = True
        else:
            # `refuse` already raised above; unreachable.
            raise ConflictError("CI configuration probe failed")
    else:
        resolved_allow_no_ci = allow_no_ci_input == "true"
    final_policy = dict(merged)
    final_policy["allow_no_ci"] = resolved_allow_no_ci
    try:
        WorkflowPolicy.model_validate(final_policy)
    except ValidationError as exc:
        raise ValueError(f"invalid resolved workflow policy: {exc}") from exc
    submit_source = (
        f"submit:{source}" if source and not source.startswith("submit:") else (source or "submit")
    )
    workflow_id = store.start_workflow(
        statement, policy=final_policy, source=submit_source, force_queued=True
    )
    # Read back run identity for the event and response.
    state = store.get_state()
    run_id: str | None = None
    statement_sha256: str | None = None
    status: str = "queued"
    workflow = state.get("workflow")
    if workflow is not None and workflow.get("id") == workflow_id:
        run_id = workflow.get("run_id")
        statement_sha256 = workflow.get("statement_sha256")
        status = str(workflow.get("status") or "active")
    else:
        for entry in state.get("queue") or []:
            if entry.get("workflow_id") == workflow_id:
                run_id = entry.get("run_id")
                statement_sha256 = entry.get("statement_sha256")
                status = "queued"
                break
    if run_id is None or statement_sha256 is None:
        # Fallback: the workflow exists (start succeeded) but state shape
        # changed; still emit with what we have.
        run_id = run_id or ""
        statement_sha256 = statement_sha256 or ""
    store.append_event(
        EventKind.WORK_SUBMITTED,
        {
            "workflow_id": workflow_id,
            "run_id": run_id,
            "statement_sha256": statement_sha256,
            "source": submit_source,
        },
    )
    return {
        "workflow_id": workflow_id,
        "run_id": run_id,
        "statement_sha256": statement_sha256,
        "status": status,
    }
