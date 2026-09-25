#!/usr/bin/env python3
"""Dump the schema of a freshly created database to `tests/fixtures/schema_v<N>.sql`.

Run this **before** bumping `SCHEMA_VERSION`, so the outgoing version is
captured from the code that shipped it (#54). The migration tests replay these
dumps instead of describing history from memory, which is how a real v5
database kept its `idx_event_inbox` index while the hand-written fixture did
not — and how `ALTER TABLE event DROP COLUMN consumed` passed CI and then
crashed on every live hub.

    uv run python scripts/dump-schema.py            # dump the current schema
    uv run python scripts/dump-schema.py --out -    # write it to stdout instead
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
import tempfile
from pathlib import Path

from agent_hub.database import SCHEMA_VERSION, initialize_database

FIXTURES = Path(__file__).resolve().parents[1] / "tests" / "fixtures"

HEADER = """\
-- Schema of a real v{version} database, dumped from the code that shipped it.
-- Regenerate with `uv run python scripts/dump-schema.py`; edit the schema, not this file.
"""


def dump_schema(connection: sqlite3.Connection) -> str:
    """Render one database's schema as a replayable script.

    Tables come before indexes so a replay never indexes a column that does not
    exist yet, and SQLite's own bookkeeping tables are left out: `sqlite_*` is
    reserved, so replaying its DDL is an error rather than a fidelity gain.
    """

    connection.row_factory = sqlite3.Row
    rows = connection.execute(
        "SELECT type, name, sql FROM sqlite_master"
        " WHERE sql IS NOT NULL AND name NOT LIKE 'sqlite_%'"
        " ORDER BY CASE type WHEN 'table' THEN 0 ELSE 1 END, name"
    ).fetchall()
    version = connection.execute("PRAGMA user_version").fetchone()[0]

    body = "".join(f"\n{row['sql'].strip()};\n" for row in rows)
    return f"{HEADER.format(version=version)}{body}\nPRAGMA user_version = {version};\n"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--out",
        default=None,
        help=f"destination file, or '-' for stdout (default: {FIXTURES}/schema_v<N>.sql)",
    )
    args = parser.parse_args()

    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "hub.db"
        initialize_database(path)
        connection = sqlite3.connect(path)
        try:
            text = dump_schema(connection)
        finally:
            connection.close()

    if args.out == "-":
        sys.stdout.write(text)
        return
    destination = Path(args.out) if args.out else FIXTURES / f"schema_v{SCHEMA_VERSION}.sql"
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(text)
    print(f"wrote {destination}", file=sys.stderr)


if __name__ == "__main__":
    main()
