"""Relative Markdown links in the top-level and docs Markdown resolve to files (#68)."""

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
DOCS = sorted(
    {
        ROOT / "README.md",
        ROOT / "AGENTS.md",
        ROOT / "guides" / "README.md",
        ROOT / "runtimes" / "README.md",
        ROOT / "scripts" / "poc" / "README.md",
        *(ROOT / "docs").rglob("*.md"),
    }
)
FENCE = re.compile(r"^(```|~~~).*?^\1", re.MULTILINE | re.DOTALL)
CODE_SPAN = re.compile(r"`[^`\n]*`")
LINK = re.compile(r"\]\(<?([^)\s>]+)>?(?:\s+\"[^\"]*\")?\)")
SCHEME = re.compile(r"^[a-z][a-z0-9+.-]*:", re.IGNORECASE)


@pytest.mark.parametrize("doc", DOCS, ids=lambda path: path.relative_to(ROOT).as_posix())
def test_relative_links_resolve(doc: Path) -> None:
    text = CODE_SPAN.sub("", FENCE.sub("", doc.read_text(encoding="utf-8")))
    targets = (match.group(1).partition("#")[0] for match in LINK.finditer(text))
    missing = [
        target
        for target in targets
        if target and not SCHEME.match(target) and not (doc.parent / target).exists()
    ]
    assert missing == []
