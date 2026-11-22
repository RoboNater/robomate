"""Bound guide pages by their JSON-escaped size for MCP clients."""

import json


def guide_page(guide: str, page: int) -> str:
    """Return a numbered page; its JSON-escaped content is at most 18 KB."""
    if page < 1:
        raise ValueError("page must be at least 1")
    pages: list[str] = []
    start = 0
    size = 0
    for index, char in enumerate(guide):
        char_size = len(json.dumps(char, ensure_ascii=True)) - 2
        if size + char_size > 18000:
            pages.append(guide[start:index])
            start = index
            size = 0
        size += char_size
    pages.append(guide[start:])
    if page > len(pages):
        raise ValueError(f"page exceeds guide page count ({len(pages)})")
    return f"Page {page}/{len(pages)}\n" + pages[page - 1]
