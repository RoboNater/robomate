"""The hub's shared fixtures, re-exported for the archived PoC acceptance tests.

These tests are outside the default `pytest` run (`testpaths = ["tests"]`), so
`tests/conftest.py` is not loaded for them. This module loads it under another
name and re-exports its fixtures and helpers, so `from conftest import ...` in
`test_step7.py` keeps working. Run them with:

    uv run --locked pytest scripts/poc/tests
"""

import importlib.util
import sys
from pathlib import Path

_PATH = Path(__file__).resolve().parents[3] / "tests" / "conftest.py"
_SPEC = importlib.util.spec_from_file_location("hub_tests_conftest", _PATH)
assert _SPEC and _SPEC.loader
_SHARED = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = _SHARED
_SPEC.loader.exec_module(_SHARED)

globals().update({k: v for k, v in vars(_SHARED).items() if not k.startswith("_")})
