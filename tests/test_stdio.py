"""Console escaping preserves encodings and leaves non-console streams alone."""

import io
import sys

import pytest
from agent_hub_common.stdio import escape_unencodable_output


def test_console_output_preserves_encoding_and_escapes_unencodable_text(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with (
        io.BytesIO() as buffer,
        io.TextIOWrapper(buffer, encoding="cp1252", newline="\n") as stdout,
    ):
        monkeypatch.setattr(sys, "stdout", stdout)
        escape_unencodable_output()
        print("drivér → ā", flush=True)
        assert stdout.encoding == "cp1252"
        assert buffer.getvalue() == b"driv\xe9r \\u2192 \\u0101\n"


def test_console_output_allows_non_file_streams(monkeypatch: pytest.MonkeyPatch) -> None:
    with io.StringIO() as stdout:
        monkeypatch.setattr(sys, "stdout", stdout)
        escape_unencodable_output()
        print("→ ā")
        assert stdout.getvalue() == "→ ā\n"
