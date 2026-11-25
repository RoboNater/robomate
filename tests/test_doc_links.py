"""Relative Markdown links in the top-level and docs Markdown resolve to files (#68).

A fragment on a relative Markdown link must also match a heading or explicit
anchor in its target (#650). Tests cited by name in the testing docs must still
exist under ``tests/``.
"""

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
CITED_TEST_DOCS = (
    ROOT / "docs" / "development" / "testing-patterns.md",
    ROOT / "docs" / "development" / "testing-time-and-waits.md",
)
FENCE = re.compile(r"^(```|~~~).*?^\1", re.MULTILINE | re.DOTALL)
CODE_SPAN = re.compile(r"`[^`\n]*`")
LINK = re.compile(r"\]\(<?([^)\s>]+)>?(?:\s+\"[^\"]*\")?\)")
SCHEME = re.compile(r"^[a-z][a-z0-9+.-]*:", re.IGNORECASE)
HEADING = re.compile(r"^ {0,3}#{1,6}[ \t]+(.*?)(?:[ \t]+#+)?[ \t]*$", re.MULTILINE)
EXPLICIT_ANCHOR = re.compile(r"<a\s+[^>]*\b(?:id|name)=\"([^\"]+)\"", re.IGNORECASE)
CITED_TEST_NAME = re.compile(r"`(test_\w+)`")
DOC_TEST_FILE = re.compile(r"tests/test_[\w.-]+\.py")
TEST_DEFINITION = re.compile(r"^(?:async )?def (test_\w+)\b", re.MULTILINE)

_ANCHOR_CACHE: dict[Path, set[str]] = {}


def _without_fences(text: str) -> str:
    return FENCE.sub("", text)


def _link_targets(doc: Path) -> list[str]:
    text = _without_fences(doc.read_text(encoding="utf-8"))
    return [match.group(1) for match in LINK.finditer(CODE_SPAN.sub("", text))]


def _relative_path(doc: Path, link: str) -> Path | None:
    path = link.partition("#")[0]
    if not path or SCHEME.match(link):
        return None
    return doc.parent / path


def _slug(text: str) -> str:
    return re.sub(r"[^\w \-]", "", text.lower().strip()).replace(" ", "-")


def _heading_slugs(text: str) -> set[str]:
    seen: set[str] = set()
    counters: dict[str, int] = {}
    for match in HEADING.finditer(text):
        base = _slug(match.group(1))
        if not base:
            continue
        slug = base
        if slug in seen:
            number = counters.get(base, 1)
            while f"{base}-{number}" in seen:
                number += 1
            counters[base] = number
            slug = f"{base}-{number}"
        seen.add(slug)
    return seen


def _explicit_anchors(text: str) -> set[str]:
    return {match.group(1) for match in EXPLICIT_ANCHOR.finditer(text)}


def _anchors(path: Path) -> set[str]:
    if path not in _ANCHOR_CACHE:
        text = _without_fences(path.read_text(encoding="utf-8"))
        _ANCHOR_CACHE[path] = _heading_slugs(text) | _explicit_anchors(text)
    return _ANCHOR_CACHE[path]


def _fragment_links(doc: Path) -> list[tuple[str, Path, str]]:
    links: list[tuple[str, Path, str]] = []
    for link in _link_targets(doc):
        if SCHEME.match(link):
            continue
        path, separator, fragment = link.partition("#")
        if not separator or not fragment:
            continue
        target = doc if not path else doc.parent / path
        if target.suffix.lower() == ".md":
            links.append((link, target, fragment))
    return links


def _broken_fragment_links(doc: Path) -> list[str]:
    return [
        link
        for link, target, fragment in _fragment_links(doc)
        if target.exists() and fragment not in _anchors(target)
    ]


def _paragraphs(text: str) -> list[str]:
    return [part for part in re.split(r"\n\s*\n", text) if part.strip()]


def _defined_tests() -> dict[str, set[Path]]:
    defined: dict[str, set[Path]] = {}
    for path in (ROOT / "tests").rglob("test_*.py"):
        for match in TEST_DEFINITION.finditer(path.read_text(encoding="utf-8")):
            defined.setdefault(match.group(1), set()).add(path.resolve())
    return defined


@pytest.mark.parametrize("doc", DOCS, ids=lambda path: path.relative_to(ROOT).as_posix())
def test_relative_links_resolve(doc: Path) -> None:
    missing = [
        target
        for target in (_relative_path(doc, link) for link in _link_targets(doc))
        if target is not None and not target.exists()
    ]
    assert missing == []


@pytest.mark.parametrize("doc", DOCS, ids=lambda path: path.relative_to(ROOT).as_posix())
def test_relative_link_fragments_resolve(doc: Path) -> None:
    broken = [f"{doc.relative_to(ROOT).as_posix()}: {link}" for link in _broken_fragment_links(doc)]
    assert broken == []


@pytest.mark.parametrize("doc", CITED_TEST_DOCS, ids=lambda path: path.relative_to(ROOT).as_posix())
def test_cited_tests_exist(doc: Path) -> None:
    defined = _defined_tests()
    problems: list[str] = []
    for paragraph in _paragraphs(_without_fences(doc.read_text(encoding="utf-8"))):
        names = CITED_TEST_NAME.findall(paragraph)
        if not names:
            continue
        files = {
            path.resolve() for path in (ROOT / name for name in DOC_TEST_FILE.findall(paragraph))
        }
        paired = next(iter(files)) if len(files) == 1 else None
        for name in names:
            if name not in defined:
                problems.append(
                    f"{doc.relative_to(ROOT).as_posix()}: {name} is not defined under tests/"
                )
            elif paired is not None and paired not in defined[name]:
                problems.append(
                    f"{doc.relative_to(ROOT).as_posix()}: {name} is not defined in "
                    f"{paired.relative_to(ROOT).as_posix()}"
                )
    assert problems == []


def test_heading_slugs_follow_github_rules() -> None:
    text = _without_fences(
        "# Testing patterns (#589)\n"
        "## 3. Nested pytest\n"
        "## 3. Nested pytest\n"
        "## `os.kill(pid, 0)` is `CTRL_C_EVENT` on Windows; use `process_alive`\n"
        "```\n"
        "## not a heading inside a fence\n"
        "```\n"
    )
    assert _heading_slugs(text) == {
        "testing-patterns-589",
        "3-nested-pytest",
        "3-nested-pytest-1",
        "oskillpid-0-is-ctrl_c_event-on-windows-use-process_alive",
    }


def test_explicit_anchor_counts(tmp_path: Path) -> None:
    doc = tmp_path / "doc.md"
    doc.write_text('<a id="custom-anchor"></a>\n\n## Heading\n', encoding="utf-8")
    assert _anchors(doc) == {"custom-anchor", "heading"}


def test_fragment_links_ignore_non_markdown_targets(tmp_path: Path) -> None:
    doc = tmp_path / "doc.md"
    doc.write_text(
        "[source](../pkg/module.py#L10)\n"
        "[same](#heading)\n"
        "[other](other.md#section)\n"
        "[external](https://example.com/a.md#section)\n",
        encoding="utf-8",
    )
    assert [link for link, _, _ in _fragment_links(doc)] == ["#heading", "other.md#section"]


def test_numbered_duplicate_fragments_match(tmp_path: Path) -> None:
    target = tmp_path / "target.md"
    target.write_text("# What happened\n\n# What happened\n", encoding="utf-8")
    source = tmp_path / "source.md"
    source.write_text(
        "[first](target.md#what-happened)\n[second](target.md#what-happened-1)\n",
        encoding="utf-8",
    )
    assert _broken_fragment_links(source) == []


def test_fragment_matches_backticked_punctuated_heading(tmp_path: Path) -> None:
    target = tmp_path / "target.md"
    target.write_text(
        "## `os.kill(pid, 0)` is `CTRL_C_EVENT` on Windows; use `process_alive`\n",
        encoding="utf-8",
    )
    source = tmp_path / "source.md"
    source.write_text(
        "[liveness](target.md#oskillpid-0-is-ctrl_c_event-on-windows-use-process_alive)\n",
        encoding="utf-8",
    )
    assert _broken_fragment_links(source) == []


def test_explicit_anchor_fragment_matches(tmp_path: Path) -> None:
    target = tmp_path / "target.md"
    target.write_text('<a id="custom-anchor"></a>\n\n## Heading\n', encoding="utf-8")
    source = tmp_path / "source.md"
    source.write_text("[custom](target.md#custom-anchor)\n", encoding="utf-8")
    assert _broken_fragment_links(source) == []
