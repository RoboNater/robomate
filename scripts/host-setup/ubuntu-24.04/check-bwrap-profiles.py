#!/usr/bin/env python3
"""Conservatively identify AppArmor attachments that might match system bwrap."""

import fnmatch
import re
import sys
from pathlib import Path

TARGET = "/usr/bin/bwrap"


def may_match(pattern: str) -> bool:
    # Unknown variables may expand to anything. Overmatching fails closed.
    pattern = re.sub(r"@\{[^}]+\}", "*", pattern)
    match = re.search(r"\{([^{}]*)\}", pattern)
    if match:
        return any(
            may_match(pattern[: match.start()] + choice + pattern[match.end() :])
            for choice in match.group(1).split(",")
        )
    return fnmatch.fnmatchcase(TARGET, pattern)


def attachments(text: str) -> list[str]:
    result = []
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or not re.search(r"\{\s*(?:#.*)?$", line):
            continue
        words = line.split()
        if words[0] == "profile":
            words = words[1:]
            if len(words) > 1 and not words[1].startswith(("{", "flags=")):
                words = words[1:]
        if words and words[0].startswith(("/", "@{")):
            result.append(words[0])
    return result


def profile_contents(path: Path) -> list[str]:
    return [
        line.strip()
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]


def main() -> int:
    directory, own = map(Path, sys.argv[1:])
    for path in sorted(directory.iterdir()):
        if not path.is_file():
            continue
        if path.name == "bwrap-userns-restrict":
            continue
        try:
            if path.name == "bwrap" and profile_contents(path) == profile_contents(own):
                continue
            patterns = attachments(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError) as exc:
            print(f"Cannot inspect profile {path}: {exc}", file=sys.stderr)
            return 1
        if any(may_match(pattern) for pattern in patterns):
            print(f"Conflicting profile: {path}", file=sys.stderr)
            return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
