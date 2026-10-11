#!/usr/bin/env python3
"""Archive a stopped run, excluding harness credentials, hub tokens and symlinks.

Run logs may contain sensitive task data; this is a local archive, not a
publication scrubber. Stop the run before invoking this command.
"""

from __future__ import annotations

import argparse
import os
import tarfile
from pathlib import Path, PurePosixPath


def excluded(path: str) -> bool:
    """Match credential paths regardless of whether they are links or copies."""
    parts = PurePosixPath(path).parts
    name = parts[-1].lower()
    return (
        name in {"auth.json", "antigravity-oauth-token", "jetski_state.pbtxt", ".git-credentials"}
        or tuple(part.lower() for part in parts[-3:])
        == (".gemini", "antigravity-cli", "settings.json")
        or ".robomate" in (part.lower() for part in parts)
        or name in {"token", "operator-token"}
    )


def archive_run(run_dir: Path, output: Path) -> None:
    """Write a new archive without traversing symlinks or reading excluded files."""
    run_dir = run_dir.resolve(strict=True)
    output = output.resolve()
    if not run_dir.is_dir():
        raise ValueError("run directory must be a directory")
    if output == run_dir or run_dir in output.parents:
        raise ValueError("archive output must be outside the run directory")

    def walk_error(exc: OSError) -> None:
        raise exc

    # Exclusive creation prevents replacing an existing archive accidentally.
    with output.open("xb") as stream:
        try:
            with tarfile.open(fileobj=stream, mode="w:gz", dereference=False) as archive:
                for parent, directories, files in os.walk(
                    run_dir, followlinks=False, onerror=walk_error
                ):
                    base = Path(parent)
                    directories[:] = [
                        name
                        for name in directories
                        if not (base / name).is_symlink()
                        and not (base / name).is_junction()
                        and not excluded((base / name).relative_to(run_dir).as_posix())
                    ]
                    for name in files:
                        path = base / name
                        relative = path.relative_to(run_dir).as_posix()
                        if not excluded(relative) and not path.is_symlink() and path.is_file():
                            archive.add(path, arcname=relative, recursive=False)
        except BaseException:
            stream.close()
            output.unlink()
            raise


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    archive_run(args.run_dir, args.output)


if __name__ == "__main__":
    main()
