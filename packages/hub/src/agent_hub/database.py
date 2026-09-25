"""SQLite schema creation for durable hub state."""

from __future__ import annotations

import re
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from enum import StrEnum
from pathlib import Path

from agent_hub_common import (
    UNKNOWN,
    AgentStatus,
    EventKind,
    EventState,
    ModelSource,
    TaskState,
    WorkflowStatus,
)

# v12 is #126's observed peer addresses on `agent`, after #77's v11
# `agent.declared_model`, #78's v10 `call_log` byte accounting and #51's v9
# durable binding from an assignment to its triggering event.
# Bumping this means first dumping the version it replaces:
# `uv run python scripts/dump-schema.py` writes tests/fixtures/schema_v<N>.sql,
# which is what the migration tests replay instead of a fixture written from
# memory (#54).
SCHEMA_VERSION = 12


class DatabaseVersionError(RuntimeError):
    """Raised when the on-disk schema does not match this application."""


class MigrationError(RuntimeError):
    """Raised when a migration cannot leave the database in a usable state."""


def _sql_values(enum_type: type[StrEnum]) -> str:
    # Values come only from closed application enums, never from runtime input.
    return ", ".join(f"'{item.value}'" for item in enum_type)


# The worker identity profile (spec §3). Declared once so a fresh schema and a
# migrated one get identical columns.
_BASE_PROFILE_COLUMNS = {
    "harness": f"TEXT NOT NULL DEFAULT '{UNKNOWN}'",
    "harness_version": f"TEXT NOT NULL DEFAULT '{UNKNOWN}'",
    "provider": f"TEXT NOT NULL DEFAULT '{UNKNOWN}'",
    "model": f"TEXT NOT NULL DEFAULT '{UNKNOWN}'",
    "model_source": f"TEXT NOT NULL DEFAULT '{UNKNOWN}'"
    f" CHECK (model_source IN ({_sql_values(ModelSource)}))",
    "workspace_id": "TEXT",
}
# What the runtime itself reported (#77). Nullable so a row written before the
# column existed is not forced to claim anything; readers treat NULL as unknown.
DECLARED_MODEL_COLUMNS = {
    "declared_model": f"TEXT DEFAULT '{UNKNOWN}'",
}
PROFILE_COLUMNS = _BASE_PROFILE_COLUMNS | DECLARED_MODEL_COLUMNS
# Where a worker's requests were seen coming from (#126): the transport peer
# the hub observed, not anything the worker claimed. Nullable, since no row
# written before v12 recorded it and it cannot be recovered afterwards.
PEER_ADDRESS_COLUMNS = {
    "checkin_remote_addr": "TEXT",
    "last_remote_addr": "TEXT",
}
_AGENT_SQL = "".join(
    f"\n    {name} {spec}," for name, spec in (PROFILE_COLUMNS | PEER_ADDRESS_COLUMNS).items()
)


SCHEMA = f"""
CREATE TABLE IF NOT EXISTS workflow (
    id TEXT PRIMARY KEY,
    goal TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ({_sql_values(WorkflowStatus)})),
    policy_json TEXT NOT NULL DEFAULT '{{}}',
    created TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS agent (
    name TEXT PRIMARY KEY,
    capabilities_json TEXT NOT NULL DEFAULT '[]',
    status TEXT NOT NULL CHECK (status IN ({_sql_values(AgentStatus)})),
    context_id TEXT UNIQUE,
    last_seen TEXT NOT NULL,
    worker_instance_id TEXT NOT NULL DEFAULT '',
    last_heartbeat TEXT NOT NULL DEFAULT '',
    last_progress_at TEXT,
    current_task_id TEXT,{_AGENT_SQL}
    FOREIGN KEY (current_task_id) REFERENCES task(id) ON DELETE SET NULL
);

CREATE TABLE IF NOT EXISTS task (
    id TEXT PRIMARY KEY,
    workflow_id TEXT NOT NULL,
    assignee TEXT,
    role TEXT NOT NULL,
    title TEXT NOT NULL,
    instructions TEXT NOT NULL,
    state TEXT NOT NULL CHECK (state IN ({_sql_values(TaskState)})),
    lease_expires TEXT,
    lease_duration_s REAL NOT NULL DEFAULT 1800,
    result_json TEXT,
    created TEXT NOT NULL,
    updated TEXT NOT NULL,
    pr_head_sha TEXT,
    source_event_id INTEGER,
    FOREIGN KEY (workflow_id) REFERENCES workflow(id) ON DELETE CASCADE,
    FOREIGN KEY (assignee) REFERENCES agent(name) ON DELETE SET NULL,
    FOREIGN KEY (source_event_id) REFERENCES event(id)
);

CREATE TABLE IF NOT EXISTS message (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id TEXT,
    context_id TEXT NOT NULL,
    sender TEXT NOT NULL,
    direction TEXT NOT NULL CHECK (direction IN ('to_alice', 'from_alice')),
    parts_json TEXT NOT NULL,
    ts TEXT NOT NULL,
    FOREIGN KEY (task_id) REFERENCES task(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS event (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT NOT NULL CHECK (kind IN ({_sql_values(EventKind)})),
    payload_json TEXT NOT NULL,
    state TEXT NOT NULL DEFAULT 'queued' CHECK (state IN ({_sql_values(EventState)})),
    delivery_id TEXT,
    delivery_attempts INTEGER NOT NULL DEFAULT 0,
    delivered_at TEXT,
    delivery_expires TEXT,
    acked_at TEXT,
    ts TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS decision (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    summary TEXT NOT NULL,
    rationale TEXT NOT NULL,
    key TEXT
);

CREATE TABLE IF NOT EXISTS operation (
    actor TEXT NOT NULL,
    operation_id TEXT NOT NULL,
    payload_hash TEXT NOT NULL,
    response_json TEXT NOT NULL,
    created TEXT NOT NULL,
    PRIMARY KEY (actor, operation_id)
);

CREATE TABLE IF NOT EXISTS call_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    boundary TEXT NOT NULL CHECK (boundary IN ('a2a', 'mcp')),
    actor TEXT NOT NULL,
    tool TEXT NOT NULL,
    outcome TEXT NOT NULL,
    status INTEGER,
    bytes_in INTEGER NOT NULL,
    bytes_out INTEGER NOT NULL,
    content_bytes INTEGER,
    repeat_bytes INTEGER,
    task_id TEXT,
    workflow_id TEXT,
    started TEXT NOT NULL,
    finished TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_task_workflow_state ON task(workflow_id, state);
CREATE INDEX IF NOT EXISTS idx_task_assignee ON task(assignee);
CREATE UNIQUE INDEX IF NOT EXISTS idx_task_source_event_id
    ON task(source_event_id) WHERE source_event_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_message_context_ts ON message(context_id, ts);
CREATE INDEX IF NOT EXISTS idx_event_state_id ON event(state, id);
CREATE INDEX IF NOT EXISTS idx_event_delivery_id ON event(delivery_id);
CREATE UNIQUE INDEX IF NOT EXISTS idx_decision_key ON decision(key) WHERE key IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_call_log_actor ON call_log(actor, tool);
"""
# `idx_decision_key` — not a column-level UNIQUE on `decision.key` — is what
# makes `log_decision` idempotent. It is partial (`key IS NOT NULL`), which is
# the intended semantics, and unlike a column constraint it is reachable by
# `ALTER TABLE`, so a migrated database can have it too (#59).


# `ALTER TABLE` can only append columns, and cannot add a column-level UNIQUE
# at all, so a migrated database reaches the current version with its columns in
# arrival order rather than the order `SCHEMA` declares. These are the tables
# that drift (#59); v8 rebuilds them into the declared shape.
REBUILT_TABLES = ("agent", "task", "event", "decision")

_CREATE_TABLE_RE = re.compile(r"CREATE TABLE IF NOT EXISTS (\w+)\s*\(", re.IGNORECASE)


def _schema_statements() -> list[str]:
    return [statement.strip() for statement in SCHEMA.split(";") if statement.strip()]


def _canonical_tables() -> dict[str, str]:
    """The `CREATE TABLE` statement `SCHEMA` declares, per table name."""

    statements = {}
    for statement in _schema_statements():
        match = _CREATE_TABLE_RE.match(statement)
        if match is not None:
            statements[match.group(1)] = statement
    return statements


def _index_statements() -> list[str]:
    return [
        statement
        for statement in _schema_statements()
        if statement.upper().startswith("CREATE INDEX")
        or statement.upper().startswith("CREATE UNIQUE INDEX")
    ]


def _normalized_sql(sql: str) -> str:
    """Collapse whitespace and `IF NOT EXISTS` so two spellings compare equal."""

    return " ".join(sql.replace("IF NOT EXISTS ", "").replace("if not exists ", "").split())


def connect(path: Path) -> sqlite3.Connection:
    """Open a configured SQLite connection."""

    connection = sqlite3.connect(path)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA journal_mode = WAL")
    connection.execute("PRAGMA busy_timeout = 5000")
    return connection


def initialize_database(path: Path) -> None:
    """Create the database and apply the initial schema or migrations."""

    path.parent.mkdir(parents=True, exist_ok=True)
    with database(path) as connection:
        current_version = connection.execute("PRAGMA user_version").fetchone()[0]
        if current_version == SCHEMA_VERSION:
            return
        if current_version == 0:
            connection.executescript(SCHEMA)
            connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
            return
        if current_version > SCHEMA_VERSION:
            raise DatabaseVersionError(
                f"database schema version {current_version} is incompatible with "
                f"expected version {SCHEMA_VERSION}"
            )
        # Each step inspects the table rather than trusting the version number,
        # so it is safe to re-run and migrations compose across schema versions
        # (any of v1-v11 -> v12).
        _migrate_agent_profile(connection)
        _migrate_operation_table(connection)
        _migrate_worker_heartbeat(connection)
        _migrate_event_delivery(connection)
        _migrate_decision_key(connection)
        _migrate_task_pr_head_sha(connection)
        _migrate_task_source_event_id(connection)
        _migrate_call_log(connection)
        _migrate_declared_model(connection)
        _migrate_peer_addresses(connection)

    # v8 (#59). Outside the transaction above: the rebuild needs its own
    # connection, because `PRAGMA foreign_keys` is a no-op inside one. The
    # version is stamped only once it succeeds, so a failed rebuild leaves a
    # database that the next run migrates again rather than one that claims to
    # be current.
    _rebuild_drifted_tables(path)
    with database(path) as connection:
        connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")


def _columns(connection: sqlite3.Connection, table: str) -> set[str]:
    return {row["name"] for row in connection.execute(f"PRAGMA table_info({table})").fetchall()}


def _migrate_agent_profile(connection: sqlite3.Connection) -> None:
    """Replace the v2 `runtime` column with the identity profile (#26).

    A recorded runtime was the harness name, so it carries over as `harness`;
    everything the old schema never captured is `unknown`.
    """

    columns = _columns(connection, "agent")
    for name, spec in _BASE_PROFILE_COLUMNS.items():
        if name not in columns:
            connection.execute(f"ALTER TABLE agent ADD COLUMN {name} {spec}")
    if "runtime" in columns:
        connection.execute(
            "UPDATE agent SET harness = trim(runtime) WHERE trim(coalesce(runtime, '')) != ''"
        )
        connection.execute("ALTER TABLE agent DROP COLUMN runtime")


def _migrate_operation_table(connection: sqlite3.Connection) -> None:
    connection.execute("""
        CREATE TABLE IF NOT EXISTS operation (
            actor TEXT NOT NULL,
            operation_id TEXT NOT NULL,
            payload_hash TEXT NOT NULL,
            response_json TEXT NOT NULL,
            created TEXT NOT NULL,
            PRIMARY KEY (actor, operation_id)
        )
    """)
    columns = _columns(connection, "operation")
    if "created" not in columns:
        connection.execute("ALTER TABLE operation ADD COLUMN created TEXT NOT NULL DEFAULT ''")


def _migrate_worker_heartbeat(connection: sqlite3.Connection) -> None:
    """Add timer-driven liveness and instance identity fields for schema v5."""

    agent_columns = _columns(connection, "agent")
    if "worker_instance_id" not in agent_columns:
        connection.execute(
            "ALTER TABLE agent ADD COLUMN worker_instance_id TEXT NOT NULL DEFAULT ''"
        )
    if "last_heartbeat" not in agent_columns:
        connection.execute(
            "ALTER TABLE agent ADD COLUMN last_heartbeat TEXT NOT NULL DEFAULT ''"
        )
        connection.execute("UPDATE agent SET last_heartbeat = last_seen")
    if "last_progress_at" not in agent_columns:
        connection.execute("ALTER TABLE agent ADD COLUMN last_progress_at TEXT")

    task_columns = _columns(connection, "task")
    if "lease_duration_s" not in task_columns:
        connection.execute(
            "ALTER TABLE task ADD COLUMN lease_duration_s REAL NOT NULL DEFAULT 1800"
        )
        # Existing leases retain their original window where SQLite can derive
        # it; terminal tasks and malformed legacy timestamps keep the default.
        connection.execute("""
            UPDATE task
            SET lease_duration_s = max(
                0,
                (julianday(lease_expires) - julianday(created)) * 86400
            )
            WHERE lease_expires IS NOT NULL
              AND julianday(lease_expires) IS NOT NULL
              AND julianday(created) IS NOT NULL
        """)


def _migrate_event_delivery(connection: sqlite3.Connection) -> None:
    """Migrate event table from consumed flag (v1-v5) to durable delivery leasing (v6)."""

    columns = _columns(connection, "event")
    if "state" not in columns:
        connection.execute(
            f"ALTER TABLE event ADD COLUMN state TEXT NOT NULL DEFAULT 'queued'"
            f" CHECK (state IN ({_sql_values(EventState)}))"
        )
    if "delivery_id" not in columns:
        connection.execute("ALTER TABLE event ADD COLUMN delivery_id TEXT")
    if "delivery_attempts" not in columns:
        connection.execute(
            "ALTER TABLE event ADD COLUMN delivery_attempts INTEGER NOT NULL DEFAULT 0"
        )
    if "delivered_at" not in columns:
        connection.execute("ALTER TABLE event ADD COLUMN delivered_at TEXT")
    if "delivery_expires" not in columns:
        connection.execute("ALTER TABLE event ADD COLUMN delivery_expires TEXT")
    if "acked_at" not in columns:
        connection.execute("ALTER TABLE event ADD COLUMN acked_at TEXT")

    connection.execute("DROP INDEX IF EXISTS idx_event_inbox")
    if "consumed" in columns:
        connection.execute(
            "UPDATE event SET state = 'acked', acked_at = ts, delivery_attempts = 1 "
            "WHERE consumed = 1"
        )
        connection.execute("ALTER TABLE event DROP COLUMN consumed")

    connection.execute("CREATE INDEX IF NOT EXISTS idx_event_state_id ON event(state, id)")
    connection.execute("CREATE INDEX IF NOT EXISTS idx_event_delivery_id ON event(delivery_id)")


def _migrate_decision_key(connection: sqlite3.Connection) -> None:
    """Add decision deduplication key column for schema v6."""

    columns = _columns(connection, "decision")
    if "key" not in columns:
        connection.execute("ALTER TABLE decision ADD COLUMN key TEXT")
    connection.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_decision_key ON decision(key) WHERE key IS NOT NULL"
    )


def _migrate_task_pr_head_sha(connection: sqlite3.Connection) -> None:
    """Add the head SHA a review or rebase assignment is bound to (#27, #41).

    Tasks assigned before the column existed were bound to nothing, so NULL is
    the truthful value for them.
    """

    if "pr_head_sha" not in _columns(connection, "task"):
        connection.execute("ALTER TABLE task ADD COLUMN pr_head_sha TEXT")


def _migrate_task_source_event_id(connection: sqlite3.Connection) -> None:
    """Bind new assignments to one durable source event (#51).

    Historical tasks predate the contract, so NULL is their only truthful
    source. The partial unique index is recreated after v8's canonical rebuild.
    """

    if "source_event_id" not in _columns(connection, "task"):
        connection.execute("ALTER TABLE task ADD COLUMN source_event_id INTEGER")


def _migrate_call_log(connection: sqlite3.Connection) -> None:
    """Add the per-call byte accounting table for schema v10 (#78).

    The table is new rather than altered, so it arrives in its declared shape
    and needs no rebuild. Hub calls before v10 were never measured, so it
    starts empty.
    """

    connection.execute(_canonical_tables()["call_log"])
    connection.execute("CREATE INDEX IF NOT EXISTS idx_call_log_actor ON call_log(actor, tool)")


def _migrate_declared_model(connection: sqlite3.Connection) -> None:
    """Add the model a runtime reported, beside the one that was resolved (#77).

    Agents that checked in before v11 never had it recorded, and it cannot be
    recovered afterwards, so their rows read `unknown` until they next check in.
    """

    columns = _columns(connection, "agent")
    for name, spec in DECLARED_MODEL_COLUMNS.items():
        if name not in columns:
            connection.execute(f"ALTER TABLE agent ADD COLUMN {name} {spec}")


def _migrate_peer_addresses(connection: sqlite3.Connection) -> None:
    """Add the observed peer addresses for schema v12 (#126).

    Agents that checked in before v12 were never observed, so their rows hold
    NULL until they next check in or heartbeat.
    """

    columns = _columns(connection, "agent")
    for name, spec in PEER_ADDRESS_COLUMNS.items():
        if name not in columns:
            connection.execute(f"ALTER TABLE agent ADD COLUMN {name} {spec}")


def _rebuild_drifted_tables(path: Path) -> None:
    """Rebuild every table whose shape `ALTER TABLE` could not converge (#59).

    `ADD COLUMN` appends, so a migrated `agent`, `task` or `event` carries its
    columns in arrival order rather than the order `SCHEMA` declares, and a
    column-level `UNIQUE` cannot be added at all. Neither is harmful to run
    against, but both make a table's shape depend on how the database got here,
    which is the one thing the migration tests exist to rule out.

    This is SQLite's supported procedure: rename the old table aside, create the
    declared one in its place, copy, drop the old, then recreate the indexes
    that went with it. Each table is skipped when its shape already matches, so
    the step is safe to re-run, and the whole pass is one transaction.
    """

    connection = sqlite3.connect(path)
    connection.row_factory = sqlite3.Row
    # The transaction is driven by hand. Python's legacy mode opens one only
    # before DML, and `with connection` issues no BEGIN of its own, so a rename
    # and a CREATE would each commit on their own and a failing copy would roll
    # back only the copy — leaving the canonical table empty, the rows in the
    # scratch table, and a retry seeing the right shape, skipping the table and
    # stamping the version over stranded data.
    connection.isolation_level = None
    try:
        # Both pragmas are no-ops inside a transaction, so they are set first.
        # `foreign_keys` off allows dropping a table that others reference;
        # `legacy_alter_table` keeps RENAME from re-parsing the whole schema
        # while the table those references point at is briefly absent.
        connection.execute("PRAGMA foreign_keys = OFF")
        connection.execute("PRAGMA legacy_alter_table = ON")
        connection.execute("PRAGMA busy_timeout = 5000")
        connection.execute("BEGIN IMMEDIATE")
        try:
            if _rebuild_all(connection):
                # Dropping a table drops its indexes with it.
                for statement in _index_statements():
                    connection.execute(statement)
                violations = connection.execute("PRAGMA foreign_key_check").fetchall()
                if violations:
                    raise MigrationError(
                        f"rebuilding {', '.join(REBUILT_TABLES)} left "
                        f"{len(violations)} foreign key violation(s)"
                    )
            connection.execute("COMMIT")
        except BaseException:
            connection.execute("ROLLBACK")
            raise
    finally:
        connection.close()


def _rebuild_all(connection: sqlite3.Connection) -> bool:
    """Rebuild each drifted table in place; True if any table was touched."""

    canonical = _canonical_tables()
    rebuilt = False
    for table in REBUILT_TABLES:
        row = connection.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = ?",
            (table,),
        ).fetchone()
        if row is None:
            continue
        if _normalized_sql(row["sql"]) == _normalized_sql(canonical[table]):
            continue
        _rebuild_table(connection, table, canonical[table])
        rebuilt = True
    return rebuilt


def _rebuild_table(connection: sqlite3.Connection, table: str, create_sql: str) -> None:
    """Copy one table into the shape `SCHEMA` declares.

    The old table is renamed aside and the new one created under the real name,
    rather than the other way round: `ALTER TABLE ... RENAME TO` stores the name
    quoted (`CREATE TABLE "event"`), so a table that arrived by rename would
    still not match a fresh one character for character.
    """

    scratch = f"{table}__rebuilding"
    high_water = _sequence_value(connection, table)
    connection.execute(f"DROP TABLE IF EXISTS {scratch}")
    connection.execute(f"ALTER TABLE {table} RENAME TO {scratch}")
    connection.execute(create_sql)

    # Only columns both shapes agree on; by this point the ALTER steps above
    # have added every column the canonical shape names.
    shared = _columns(connection, scratch) & _columns(connection, table)
    columns = ", ".join(sorted(shared))
    connection.execute(f"INSERT INTO {table} ({columns}) SELECT {columns} FROM {scratch}")
    connection.execute(f"DROP TABLE {scratch}")
    _restore_sequence(connection, table, high_water)


def _sequence_value(connection: sqlite3.Connection, table: str) -> int | None:
    """This table's `AUTOINCREMENT` high-water mark, or None if it has none."""

    exists = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'sqlite_sequence'"
    ).fetchone()
    if exists is None:
        return None
    row = connection.execute(
        "SELECT seq FROM sqlite_sequence WHERE name = ?", (table,)
    ).fetchone()
    return None if row is None else int(row["seq"])


def _restore_sequence(connection: sqlite3.Connection, table: str, high_water: int | None) -> None:
    """Carry an `AUTOINCREMENT` high-water mark across a rebuild.

    Dropping the old table drops its `sqlite_sequence` row, and copying rows
    with explicit ids only re-establishes `max(id)` of what survived. Where the
    highest rows had been deleted, that regresses the mark and the next insert
    reuses an id — which `AUTOINCREMENT` exists precisely to prevent, and which
    would let a checkpoint key like `event:{id}:assign` match a decision made
    for a different event.
    """

    if high_water is None:
        return
    row = connection.execute(
        "SELECT seq FROM sqlite_sequence WHERE name = ?", (table,)
    ).fetchone()
    if row is None:
        connection.execute(
            "INSERT INTO sqlite_sequence (name, seq) VALUES (?, ?)", (table, high_water)
        )
    elif int(row["seq"]) < high_water:
        connection.execute(
            "UPDATE sqlite_sequence SET seq = ? WHERE name = ?", (high_water, table)
        )


@contextmanager
def database(path: Path) -> Iterator[sqlite3.Connection]:
    """Yield a transactional connection and always close it after use."""

    connection = connect(path)
    try:
        with connection:
            yield connection
    finally:
        connection.close()
