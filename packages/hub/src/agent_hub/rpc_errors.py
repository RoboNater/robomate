"""Stable RPC error codes shared by responses and persisted gate failures."""

from pydantic import ValidationError

from .merge_gate import MergeGateError
from .store import ConflictError, InvalidPolicyError, NotFoundError, PayloadTooLargeError

INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
INTERNAL_ERROR = -32603
NOT_FOUND = -32001
CONFLICT = -32002
PAYLOAD_TOO_LARGE = -32003
MERGE_GATE_UNAVAILABLE = -32004


def error_code(exc: Exception) -> int:
    """Map hub exceptions once, for both transports and durable gate readings."""
    if isinstance(exc, ValidationError | InvalidPolicyError | ValueError):
        return INVALID_PARAMS
    if isinstance(exc, NotFoundError):
        return NOT_FOUND
    if isinstance(exc, ConflictError):
        return CONFLICT
    if isinstance(exc, PayloadTooLargeError):
        return PAYLOAD_TOO_LARGE
    if isinstance(exc, MergeGateError):
        return MERGE_GATE_UNAVAILABLE
    return INTERNAL_ERROR
