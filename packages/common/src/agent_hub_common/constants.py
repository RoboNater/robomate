"""Constants and metadata keys used across hub and worker packages."""

from enum import StrEnum

# Operational payload limits from spec §3. They are byte limits, not character
# limits, so non-ASCII text cannot bypass them through UTF-8 expansion.
MAX_MESSAGE_PART_BYTES = 16 * 1024
MAX_TYPED_RESULT_BYTES = 32 * 1024


class MetaKeys(StrEnum):
    """Hub-specific metadata keys namespaced with `hub.` (spec §4.0)."""

    KIND = "hub.kind"
    AGENT = "hub.agent"
    CAPABILITIES = "hub.capabilities"
    HARNESS = "hub.harness"
    HARNESS_VERSION = "hub.harness_version"
    PROVIDER = "hub.provider"
    MODEL = "hub.model"
    MODEL_SOURCE = "hub.model_source"
    DECLARED_MODEL = "hub.declared_model"
    WORKSPACE_ID = "hub.workspace_id"
    STATUS = "hub.status"
    TIMEOUT = "hub.timeout"
    TIMEOUT_S = "hub.timeout_s"
    RETRY_AS_MESSAGE_ID = "hub.retry_as_message_id"
    RELEASE = "hub.release"
    RESULT = "hub.result"
    ROLE = "hub.role"
    TITLE = "hub.title"
    ASSIGNEE = "hub.assignee"
    LEASE_EXPIRES = "hub.lease_expires"
    PR_HEAD_SHA = "hub.pr_head_sha"
    ARTIFACTS = "hub.artifacts"
    STATE = "hub.state"
    SENDER = "hub.sender"
    TS = "hub.ts"
    SCHEMA_VERSION = "hub.schema_version"
    OPERATION_ID = "hub.operation_id"
    WORKER_INSTANCE_ID = "hub.worker_instance_id"
    CURRENT_TASK_ID = "hub.current_task_id"
    ACCEPTED = "hub.accepted"
