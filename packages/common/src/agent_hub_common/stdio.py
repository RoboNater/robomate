"""Standard stream management for MCP processes."""

from __future__ import annotations

import sys
from collections.abc import Iterator
from contextlib import contextmanager, redirect_stdout
from typing import TextIO


@contextmanager
def reserve_stdout() -> Iterator[TextIO]:
    """Keep the original stream for MCP; send ordinary Python output to stderr.

    The MCP transport receives the yielded stream explicitly instead of
    discovering the redirected sys.stdout.
    This guards Python stream writes, not native writes to file descriptor 1
    or deliberate writes through sys.__stdout__.
    """
    protocol_stdout = sys.stdout
    with redirect_stdout(sys.stderr):
        yield protocol_stdout
