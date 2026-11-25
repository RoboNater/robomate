"""Bridge and hub compatibility information for worker registration."""

from __future__ import annotations

import hashlib
import json
from functools import cache
from importlib.metadata import PackageNotFoundError, version
from typing import TYPE_CHECKING

from .constants import UNKNOWN

if TYPE_CHECKING:
    from pydantic import BaseModel


@cache
def installed_version(distribution: str) -> str:
    try:
        return version(distribution)
    except PackageNotFoundError:
        return UNKNOWN


@cache
def result_schema_fingerprint(*models: type[BaseModel]) -> str:
    """Hash the complete result schemas, independent of model and key order."""
    schemas = sorted(
        json.dumps(model.model_json_schema(), sort_keys=True, separators=(",", ":"))
        for model in models
    )
    return hashlib.sha256("\n".join(schemas).encode("utf-8")).hexdigest()
