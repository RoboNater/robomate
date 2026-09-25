import sqlite3
from pathlib import Path

import agent_hub.database as database_module
import httpx
import pytest
from agent_hub import create_app
from agent_hub.database import (
    SCHEMA_VERSION,
    DatabaseVersionError,
    database,
    initialize_database,
)
from agent_hub.store import HubStore
from agent_hub_common import (
    UNKNOWN,
    AgentProfile,
    AgentStatus,
    EventKind,
    HubSettings,
    MetaKeys,
    ModelSource,
    TaskState,
)
from conftest import BASE_URL, TOKEN


def test_initialization_creates_complete_schema_and_is_idempotent(tmp_path: Path) -> None:
    path = tmp_path / "state" / "hub.db"

    initialize_database(path)
    initialize_database(path)

    with database(path) as connection:
        tables = {
            row["name"]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
        version = connection.execute("PRAGMA user_version").fetchone()[0]
        foreign_keys = connection.execute("PRAGMA foreign_keys").fetchone()[0]

    assert {"workflow", "agent", "task", "message", "event", "decision", "operation"} <= tables
    assert version == SCHEMA_VERSION
    assert foreign_keys == 1
    assert not path.with_name(f"{path.name}-wal").exists()


def test_initialization_rejects_unknown_schema_version(tmp_path: Path) -> None:
    path = tmp_path / "hub.db"
    with sqlite3.connect(path) as connection:
        connection.execute("PRAGMA user_version = 99")

    with pytest.raises(DatabaseVersionError, match="version 99"):
        initialize_database(path)


FIXTURES = Path(__file__).resolve().parent / "fixtures"
SHIPPED_VERSIONS = sorted(
    int(path.stem.removeprefix("schema_v")) for path in FIXTURES.glob("schema_v*.sql")
)


def _legacy_database(path: Path, version: int) -> None:
    """Recreate a database exactly as the commit that shipped v`version` wrote it.

    The dumps come from `scripts/dump-schema.py`, not from memory: a fixture
    that describes history by hand drifts from it silently, which is how a
    missing `idx_event_inbox` let #49's `DROP COLUMN consumed` pass CI and then
    fail on every live hub.
    """

    fixture = FIXTURES / f"schema_v{version}.sql"
    if not fixture.exists():
        raise ValueError(f"no schema dump for legacy version {version}")
    with sqlite3.connect(path) as connection:
        connection.executescript(fixture.read_text())


def _schema_objects(path: Path) -> dict[str, str]:
    """Every named schema object, as raw `sqlite_master` SQL.

    Nothing is normalised away but whitespace. Since #59 a migrated database is
    rebuilt into the shape `SCHEMA` declares, so this compares exactly — which
    is what lets it catch a column inserted in the wrong place, a duplicated
    one, or a lost constraint, none of which an order-insensitive comparison
    can see.
    """

    with database(path) as connection:
        rows = connection.execute(
            "SELECT name, sql FROM sqlite_master"
            " WHERE sql IS NOT NULL AND name NOT LIKE 'sqlite_%'"
        ).fetchall()
    return {row["name"]: " ".join(row["sql"].split()) for row in rows}


def _table_columns(path: Path, table: str) -> dict[str, tuple[str, int, str | None]]:
    with database(path) as connection:
        rows = connection.execute(f"PRAGMA table_info({table})").fetchall()
    return {row["name"]: (row["type"], row["notnull"], row["dflt_value"]) for row in rows}


def _agent_columns(path: Path) -> dict[str, tuple[str, int, str | None]]:
    return _table_columns(path, "agent")


def test_migration_from_v1_adds_an_unknown_profile(tmp_path: Path) -> None:
    path = tmp_path / "v1_hub.db"
    _legacy_database(path, version=1)
    with sqlite3.connect(path) as connection:
        connection.execute(
            "INSERT INTO agent (name, capabilities_json, status, context_id, last_seen) "
            "VALUES ('bob', '[]', 'idle', 'ctx-1', '2026-09-07T00:00:00Z')"
        )

    initialize_database(path)

    with database(path) as connection:
        version = connection.execute("PRAGMA user_version").fetchone()[0]
    assert version == SCHEMA_VERSION

    store = HubStore(path)
    agent = store.agent_by_name("bob")
    assert agent is not None
    assert agent.name == "bob"
    assert (agent.harness, agent.provider, agent.model) == (UNKNOWN, UNKNOWN, UNKNOWN)
    assert agent.model_source is ModelSource.UNKNOWN
    assert agent.workspace_id is None

    store.check_in("bob", AgentProfile(harness="codex"))
    updated = store.agent_by_name("bob")
    assert updated is not None
    assert updated.harness == "codex"


def test_migration_from_v2_carries_the_runtime_over_as_the_harness(tmp_path: Path) -> None:
    path = tmp_path / "v2_hub.db"
    _legacy_database(path, version=2)
    with sqlite3.connect(path) as connection:
        connection.executemany(
            "INSERT INTO agent (name, capabilities_json, status, context_id, last_seen, runtime) "
            "VALUES (?, '[]', 'idle', ?, '2026-09-07T00:00:00Z', ?)",
            [
                ("bob", "ctx-1", "claude-code"),
                ("charlie", "ctx-2", "codex"),
                ("dan", "ctx-3", None),
            ],
        )

    initialize_database(path)
    initialize_database(path)

    assert "runtime" not in _agent_columns(path)
    store = HubStore(path)
    harnesses = {agent.name: agent.harness for agent in store.agents()}
    assert harnesses == {"bob": "claude-code", "charlie": "codex", "dan": UNKNOWN}

def test_migration_from_v2_adds_operation_table(tmp_path: Path) -> None:
    path = tmp_path / "v2_hub.db"
    _legacy_database(path, version=2)

    initialize_database(path)

    with database(path) as connection:
        version = connection.execute("PRAGMA user_version").fetchone()[0]
        tables = {
            row["name"]
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        }
        # Insert into operation table to verify schema
        connection.execute(
            "INSERT INTO operation (actor, operation_id, payload_hash, response_json, created) "
            "VALUES ('bob', 'op-1', 'hash-1', '{\"ok\": true}', '2026-09-07T00:00:00Z')"
        )
        row = connection.execute("SELECT * FROM operation WHERE actor = 'bob'").fetchone()

    assert version == SCHEMA_VERSION
    assert "operation" in tables
    assert row["operation_id"] == "op-1"
    assert row["payload_hash"] == "hash-1"
    assert row["created"] == "2026-09-07T00:00:00Z"


@pytest.mark.parametrize("version", SHIPPED_VERSIONS)
def test_migrating_a_shipped_schema_reproduces_a_fresh_one(tmp_path: Path, version: int) -> None:
    """Both routes to v{SCHEMA_VERSION} must end at the same schema.

    Comparing every object, not a chosen few columns, is what catches a
    migration and the `SCHEMA` constant disagreeing — a table that exists two
    different ways depending on how you got there, including an index the
    migration forgot to rebuild.
    """

    fresh = tmp_path / "fresh.db"
    migrated = tmp_path / f"v{version}.db"
    initialize_database(fresh)
    _legacy_database(migrated, version)

    initialize_database(migrated)

    assert _schema_objects(migrated) == _schema_objects(fresh)
    assert _agent_columns(migrated) == _agent_columns(fresh)
    assert _table_columns(migrated, "task") == _table_columns(fresh, "task")
    assert _table_columns(migrated, "operation") == _table_columns(fresh, "operation")
    assert _table_columns(migrated, "event") == _table_columns(fresh, "event")


def test_every_shipped_version_has_a_schema_dump() -> None:
    """Bumping `SCHEMA_VERSION` includes dumping the version it replaces (#54).

    Without the outgoing dump, the next migration is only ever tested against
    schemas it was not written for.
    """

    assert list(range(1, SCHEMA_VERSION)) == SHIPPED_VERSIONS


def test_migration_from_v6_leaves_existing_tasks_unbound(tmp_path: Path) -> None:
    """#27/#41's `pr_head_sha`: tasks assigned before it was bound to nothing."""

    path = tmp_path / "v6_hub.db"
    fresh = tmp_path / "fresh.db"
    initialize_database(fresh)
    _legacy_database(path, 6)
    with sqlite3.connect(path) as connection:
        connection.execute(
            "INSERT INTO workflow (id, goal, status, created)"
            " VALUES ('wf', 'goal', 'active', '2026-09-10T00:00:00Z')"
        )
        connection.execute(
            "INSERT INTO task (id, workflow_id, role, title, instructions, state, created, updated)"
            " VALUES ('t1', 'wf', 'reviewer', 'Review', 'Look', 'completed',"
            " '2026-09-10T00:00:00Z', '2026-09-10T00:00:00Z')"
        )

    initialize_database(path)
    initialize_database(path)

    store = HubStore(path)
    old = store.get_task("t1")
    assert old is not None and old.pr_head_sha is None
    store.check_in("bob")
    head = "ABCDEF0123456789abcdef0123456789abcdef01"
    new = store.assign_task("bob", "reviewer", "Review", "Look again", pr_head_sha=head)
    assert new.pr_head_sha == head.lower()
    assert _table_columns(path, "task") == _table_columns(fresh, "task")
    with database(path) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION


def test_model_source_is_constrained_to_its_enum(tmp_path: Path) -> None:
    path = tmp_path / "hub.db"
    initialize_database(path)

    with pytest.raises(sqlite3.IntegrityError), database(path) as connection:
        connection.execute(
            "INSERT INTO agent (name, status, context_id, last_seen, model_source) "
            "VALUES ('bob', 'idle', 'ctx-1', '2026-09-07T00:00:00Z', 'guessed')"
        )


async def test_migration_from_v3_mainline_preserves_profiles_and_enables_idempotent_check_in(
    tmp_path: Path,
) -> None:
    path = tmp_path / "v3_mainline.db"
    _legacy_database(path, version=3)

    with sqlite3.connect(path) as connection:
        connection.execute(
            "INSERT INTO agent ("
            "    name, capabilities_json, status, context_id, last_seen, "
            "    harness, harness_version, provider, model, model_source, workspace_id"
            ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "bob",
                '["python", "fastapi"]',
                "idle",
                "ctx-bob-1",
                "2026-09-10T20:00:00Z",
                "claude-code",
                "1.0.0",
                "anthropic",
                "claude-3-7-sonnet",
                "env",
                "ws-bob-main",
            ),
        )

    # Migrate from v3 through the composed migrations to v5.
    initialize_database(path)

    # 1. Verify version advanced to the current schema.
    with database(path) as connection:
        version = connection.execute("PRAGMA user_version").fetchone()[0]
        tables = {
            row["name"]
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        }
        op_columns = {
            row["name"] for row in connection.execute("PRAGMA table_info(operation)").fetchall()
        }

    assert version == SCHEMA_VERSION  # 5
    assert "operation" in tables
    assert {"actor", "operation_id", "payload_hash", "response_json", "created"} <= op_columns

    # 2. Verify profile data survived intact
    store = HubStore(path)
    bob = store.agent_by_name("bob")
    assert bob is not None
    assert bob.name == "bob"
    assert bob.capabilities == ["python", "fastapi"]
    assert bob.status.value == "idle"
    assert bob.harness == "claude-code"
    assert bob.harness_version == "1.0.0"
    assert bob.provider == "anthropic"
    assert bob.model == "claude-3-7-sonnet"
    assert bob.model_source == ModelSource.ENV
    assert bob.workspace_id == "ws-bob-main"
    assert bob.last_heartbeat == "2026-09-10T20:00:00Z"
    assert bob.worker_instance_id == ""

    # 3. Verify an idempotent wire check-in succeeds on the migrated database
    guides_dir = tmp_path / "guides"
    guides_dir.mkdir()
    settings = HubSettings(
        host="127.0.0.1",
        port=8420,
        public_url="http://hub.example:8420",
        state_dir=tmp_path,
        database_path=path,
        token=TOKEN,
        token_file=tmp_path / "token",
        guides_dir=guides_dir,
        default_wait_s=0.2,
        max_wait_s=1.0,
        lost_after_s=60.0,
        sweep_interval_s=3600.0,
    )
    app = create_app(settings)
    op_id = "op-v3-migration-wire-checkin"
    payload = {
        "jsonrpc": "2.0",
        "id": "1",
        "method": "message/send",
        "params": {
            "message": {
                "messageId": "msg-v3-1",
                "role": "user",
                "parts": [{"kind": "text", "text": "READY"}],
                "metadata": {
                    MetaKeys.AGENT: "bob",
                    MetaKeys.CAPABILITIES: ["python", "fastapi"],
                    MetaKeys.HARNESS: "claude-code",
                    MetaKeys.HARNESS_VERSION: "1.0.0",
                    MetaKeys.PROVIDER: "anthropic",
                    MetaKeys.MODEL: "claude-3-7-sonnet",
                    MetaKeys.MODEL_SOURCE: "env",
                    MetaKeys.WORKSPACE_ID: "ws-bob-main",
                    MetaKeys.SCHEMA_VERSION: 1,
                    MetaKeys.OPERATION_ID: op_id,
                    MetaKeys.WORKER_INSTANCE_ID: "worker-v3-migration",
                },
            }
        },
    }

    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url=BASE_URL,
            headers={"Authorization": f"Bearer {TOKEN}"},
        ) as client,
    ):
        resp1 = await client.post("/a2a", json=payload)
        assert resp1.status_code == 200
        res1 = resp1.json()["result"]
        assert res1["kind"] == "message"
        assert res1["metadata"][MetaKeys.AGENT] == "bob"

        # Verify operation record created
        with database(path) as conn:
            op_row = conn.execute(
                "SELECT * FROM operation WHERE actor = 'bob' AND operation_id = ?", (op_id,)
            ).fetchone()
            assert op_row is not None
            assert op_row["operation_id"] == op_id
            assert op_row["created"] != ""

        # Replay identical wire check-in returns cached result
        resp2 = await client.post("/a2a", json=payload)
        assert resp2.status_code == 200
        assert resp2.json()["result"] == res1


def test_migration_from_v5_to_v6_durable_event_delivery(tmp_path: Path) -> None:
    path = tmp_path / "v5_hub.db"
    _legacy_database(path, version=5)

    with sqlite3.connect(path) as connection:
        connection.execute(
            "INSERT INTO event (kind, payload_json, consumed, ts) VALUES (?, ?, ?, ?)",
            ("agent_checked_in", '{"agent": "bob"}', 1, "2026-09-10T12:00:00Z"),
        )
        connection.execute(
            "INSERT INTO event (kind, payload_json, consumed, ts) VALUES (?, ?, ?, ?)",
            ("task_progress", '{"note": "working"}', 0, "2026-09-10T12:01:00Z"),
        )
        connection.execute(
            "INSERT INTO decision (ts, summary, rationale) VALUES (?, ?, ?)",
            ("2026-09-10T12:00:00Z", "Initial decision", "Setup"),
        )

    initialize_database(path)

    with database(path) as connection:
        version = connection.execute("PRAGMA user_version").fetchone()[0]
        event_columns = {
            row["name"] for row in connection.execute("PRAGMA table_info(event)").fetchall()
        }
        decision_columns = {
            row["name"] for row in connection.execute("PRAGMA table_info(decision)").fetchall()
        }
        events = connection.execute("SELECT * FROM event ORDER BY id").fetchall()

    assert version == SCHEMA_VERSION  # 6
    assert "consumed" not in event_columns
    assert {
        "state",
        "delivery_id",
        "delivery_attempts",
        "delivered_at",
        "delivery_expires",
        "acked_at",
    } <= event_columns
    assert "key" in decision_columns

    # Check consumed event was migrated to acked
    assert events[0]["state"] == "acked"
    assert events[0]["acked_at"] == "2026-09-10T12:00:00Z"
    assert events[0]["delivery_attempts"] == 1

    # Check unconsumed event was migrated to queued
    assert events[1]["state"] == "queued"
    assert events[1]["acked_at"] is None
    assert events[1]["delivery_attempts"] == 0

    # Test store can log deduped decision and lease the queued event
    store = HubStore(path)
    d1 = store.log_decision("Step", "Reason", key="chk-1")
    d2 = store.log_decision("Step", "Reason", key="chk-1")
    assert d1 == d2

    leased = store.lease_next_event()
    assert leased is not None
    assert leased.id == events[1]["id"]
    assert leased.state.value == "delivered"
    assert leased.delivery_attempts == 1
    assert leased.delivery_id is not None


def _populate(path: Path, version: int) -> None:
    """Write one row into every table the shipped schema at `version` has."""

    with sqlite3.connect(path) as connection:
        connection.execute(
            "INSERT INTO workflow (id, goal, status, created)"
            " VALUES ('wf', 'Ship it', 'active', '2026-09-10T00:00:00Z')"
        )
        connection.execute(
            "INSERT INTO agent (name, capabilities_json, status, context_id, last_seen)"
            " VALUES ('bob', '[\"python\"]', 'idle', 'ctx-bob', '2026-09-10T00:00:00Z')"
        )
        connection.execute(
            "INSERT INTO task (id, workflow_id, assignee, role, title, instructions, state,"
            " lease_expires, created, updated)"
            " VALUES ('t1', 'wf', 'bob', 'implementer', 'Fix it', 'Please fix it', 'working',"
            " '2026-09-10T01:00:00Z', '2026-09-10T00:00:00Z', '2026-09-10T00:30:00Z')"
        )
        connection.execute(
            "INSERT INTO message (id, task_id, context_id, sender, direction, parts_json, ts)"
            " VALUES (7, 't1', 'ctx-bob', 'bob', 'to_alice', '[]', '2026-09-10T00:20:00Z')"
        )
        connection.execute(
            "INSERT INTO event (id, kind, payload_json, ts)"
            " VALUES (11, 'task_progress', '{\"note\": \"working\"}', '2026-09-10T00:20:00Z')"
        )
        connection.execute(
            "INSERT INTO decision (id, ts, summary, rationale)"
            " VALUES (3, '2026-09-10T00:10:00Z', 'Assigned bob', 'Only worker available')"
        )
        if version >= 4:
            connection.execute(
                "INSERT INTO operation (actor, operation_id, payload_hash, response_json, created)"
                " VALUES ('bob', 'op-1', 'hash-1', '{}', '2026-09-10T00:20:00Z')"
            )


@pytest.mark.parametrize("version", SHIPPED_VERSIONS)
def test_the_rebuild_carries_every_row_across(tmp_path: Path, version: int) -> None:
    """#59 rebuilds four tables; nothing may be dropped on the way through."""

    path = tmp_path / f"v{version}.db"
    _legacy_database(path, version)
    _populate(path, version)

    initialize_database(path)

    store = HubStore(path)
    agent = store.agent_by_name("bob")
    assert agent is not None
    assert agent.capabilities == ["python"]
    assert agent.status is AgentStatus.IDLE

    task = store.get_task("t1")
    assert task is not None
    assert (task.workflow_id, task.assignee, task.role) == ("wf", "bob", "implementer")
    assert task.title == "Fix it"
    assert task.state is TaskState.WORKING
    assert task.lease_expires == "2026-09-10T01:00:00Z"

    with database(path) as connection:
        counts = {
            table: connection.execute(f"SELECT COUNT(*) AS n FROM {table}").fetchone()["n"]
            for table in ("workflow", "agent", "task", "message", "event", "decision")
        }
        event = connection.execute("SELECT * FROM event WHERE id = 11").fetchone()
        decision = connection.execute("SELECT * FROM decision WHERE id = 3").fetchone()

    assert counts == dict.fromkeys(counts, 1)
    assert event["kind"] == "task_progress"
    assert event["payload_json"] == '{"note": "working"}'
    assert decision["summary"] == "Assigned bob"
    assert decision["key"] is None


@pytest.mark.parametrize("version", SHIPPED_VERSIONS)
def test_autoincrement_keeps_counting_after_the_rebuild(tmp_path: Path, version: int) -> None:
    """Dropping the old table drops its `sqlite_sequence` row with it.

    If the rebuild lost the high-water mark, the next event would reuse an id
    an acked event already holds — and `delivery_id` is scoped by event id.
    """

    path = tmp_path / f"v{version}.db"
    _legacy_database(path, version)
    _populate(path, version)

    initialize_database(path)

    store = HubStore(path)
    appended = store.append_event(EventKind.AGENT_CHECKED_IN, {"agent": "bob"})
    assert appended.id > 11
    logged = store.log_decision("Later", "After the rebuild", key="k-1")
    assert logged > 3


@pytest.mark.parametrize("version", SHIPPED_VERSIONS)
def test_migration_is_idempotent_including_the_rebuild(tmp_path: Path, version: int) -> None:
    path = tmp_path / f"v{version}.db"
    fresh = tmp_path / "fresh.db"
    initialize_database(fresh)
    _legacy_database(path, version)
    _populate(path, version)

    initialize_database(path)
    once = _schema_objects(path)
    initialize_database(path)

    assert _schema_objects(path) == once == _schema_objects(fresh)
    with database(path) as connection:
        assert connection.execute("SELECT COUNT(*) AS n FROM task").fetchone()["n"] == 1
        # A scratch table left behind would mean a rebuild stopped half way.
        leftovers = connection.execute(
            "SELECT name FROM sqlite_master WHERE name LIKE '%__rebuilding'"
        ).fetchall()
        assert leftovers == []


def test_the_rebuild_leaves_foreign_keys_intact(tmp_path: Path) -> None:
    """`agent`, `task` and `message` reference each other across the rebuild."""

    path = tmp_path / "v1.db"
    _legacy_database(path, 1)
    _populate(path, 1)

    initialize_database(path)

    with database(path) as connection:
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
        assert connection.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        # The cascade still bites: deleting the workflow takes its task, and
        # the task's message, with it.
        connection.execute("DELETE FROM workflow WHERE id = 'wf'")
        assert connection.execute("SELECT COUNT(*) AS n FROM task").fetchone()["n"] == 0
        assert connection.execute("SELECT COUNT(*) AS n FROM message").fetchone()["n"] == 0


def test_a_failed_rebuild_rolls_back_the_rename_create_and_copy_together(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The rebuild is one transaction, including the DDL.

    Python's legacy mode opens a transaction only before DML, and `with
    connection` issues no BEGIN, so without an explicit one the rename and the
    CREATE commit on their own. A failing copy then rolls back only the copy,
    leaving the canonical table empty and the rows stranded in the scratch
    table — and because the shape is now right, the retry skips the table and
    stamps the version over the loss.
    """

    path = tmp_path / "v1.db"
    fresh = tmp_path / "fresh.db"
    initialize_database(fresh)
    _legacy_database(path, 1)
    _populate(path, 1)

    real_rebuild = database_module._rebuild_table

    def failing_rebuild(connection: sqlite3.Connection, table: str, create_sql: str) -> None:
        real_rebuild(connection, table, create_sql)
        raise sqlite3.OperationalError("simulated copy failure")

    monkeypatch.setattr(database_module, "_rebuild_table", failing_rebuild)
    with pytest.raises(sqlite3.OperationalError, match="simulated copy failure"):
        initialize_database(path)

    # The ALTER steps before the rebuild are idempotent and commit on their own,
    # by design; what must be all-or-nothing is the rebuild. So the drifted
    # tables still hold their appended-order shape, their rows are all there,
    # no scratch table is left behind, and the version still says v1 — under the
    # bug `agent` would instead be canonical, empty, and its row stranded.
    assert _schema_objects(path) != _schema_objects(fresh)
    with database(path) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 1
        counts = {
            table: connection.execute(f"SELECT COUNT(*) AS n FROM {table}").fetchone()["n"]
            for table in ("agent", "task", "event", "decision")
        }
        leftovers = connection.execute(
            "SELECT name FROM sqlite_master WHERE name LIKE '%__rebuilding'"
        ).fetchall()
    assert counts == dict.fromkeys(counts, 1)
    assert leftovers == []

    # And the retry, with nothing stranded, completes the migration.
    monkeypatch.undo()
    initialize_database(path)

    assert _schema_objects(path) == _schema_objects(fresh)
    store = HubStore(path)
    agent = store.agent_by_name("bob")
    assert agent is not None and agent.capabilities == ["python"]


@pytest.mark.parametrize("version", SHIPPED_VERSIONS)
def test_the_rebuild_keeps_the_autoincrement_high_water_mark(
    tmp_path: Path, version: int
) -> None:
    """Deleting the highest rows before migrating must not let ids be reused.

    Dropping the old table drops its `sqlite_sequence` row, and copying rows
    with explicit ids only re-establishes `max(id)` of the survivors. A reused
    event id would let a checkpoint key like `event:{id}:assign` match the
    decision Alice recorded for a different event, and she would skip the
    action she was replaying instead of performing it.
    """

    path = tmp_path / f"v{version}.db"
    _legacy_database(path, version)
    with sqlite3.connect(path) as connection:
        connection.executemany(
            "INSERT INTO event (kind, payload_json, ts) VALUES ('task_progress', '{}', 't')",
            [()] * 100,
        )
        connection.executemany(
            "INSERT INTO decision (ts, summary, rationale) VALUES ('t', 's', 'r')",
            [()] * 100,
        )
        # The tail is gone, so `max(id)` no longer tells the whole story.
        connection.execute("DELETE FROM event WHERE id > 10")
        connection.execute("DELETE FROM decision WHERE id > 10")

    initialize_database(path)

    store = HubStore(path)
    assert store.append_event(EventKind.AGENT_CHECKED_IN, {"agent": "bob"}).id == 101
    assert store.log_decision("After", "the rebuild", key="k-1") == 101


def test_migration_from_v9_adds_an_empty_call_log_and_keeps_existing_rows(
    tmp_path: Path,
) -> None:
    """#78's v10: calls before accounting existed were never measured."""

    path = tmp_path / "v9_hub.db"
    _legacy_database(path, 9)
    with sqlite3.connect(path) as connection:
        connection.execute(
            "INSERT INTO workflow (id, goal, status, created)"
            " VALUES ('wf', 'goal', 'active', '2026-09-19T00:00:00Z')"
        )
        connection.execute(
            "INSERT INTO decision (ts, summary, rationale) VALUES ('t', 'kept', 'because')"
        )

    initialize_database(path)
    initialize_database(path)

    with database(path) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
        assert connection.execute("SELECT COUNT(*) FROM call_log").fetchone()[0] == 0
        assert connection.execute("SELECT summary FROM decision").fetchone()[0] == "kept"
        indexes = {row["name"] for row in connection.execute("PRAGMA index_list(call_log)")}
    assert "idx_call_log_actor" in indexes


def test_migration_from_v10_adds_declared_model_and_keeps_existing_agents_readable(
    tmp_path: Path,
) -> None:
    """#77's v11: an agent that checked in before the column has nothing recorded."""

    path = tmp_path / "v10_hub.db"
    _legacy_database(path, 10)
    assert "declared_model" not in _agent_columns(path)
    with sqlite3.connect(path) as connection:
        connection.execute(
            "INSERT INTO agent (name, status, context_id, last_seen, harness, model,"
            " model_source) VALUES ('bob', 'idle', 'ctx-bob', '2026-09-20T00:00:00Z',"
            " 'claude-code', 'claude-opus-5', 'env')"
        )

    initialize_database(path)
    initialize_database(path)

    # Nullable, defaulting to `unknown`: no NOT NULL on a column old rows lack.
    assert _agent_columns(path)["declared_model"] == ("TEXT", 0, "'unknown'")
    with database(path) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
    store = HubStore(path)
    bob = store.agent_by_name("bob")
    assert bob is not None
    assert (bob.model, bob.model_source, bob.declared_model) == (
        "claude-opus-5",
        ModelSource.ENV,
        UNKNOWN,
    )
    [state] = store.get_state()["agents"]
    assert state["declared_model"] == UNKNOWN
    assert state["model_mismatch"] is False


def test_migration_from_v11_adds_nullable_peer_addresses(tmp_path: Path) -> None:
    """#126's v12: an agent observed before the columns existed has no address."""

    path = tmp_path / "v11_hub.db"
    _legacy_database(path, 11)
    assert "checkin_remote_addr" not in _agent_columns(path)
    with sqlite3.connect(path) as connection:
        connection.execute(
            "INSERT INTO agent (name, status, context_id, last_seen, worker_instance_id,"
            " declared_model) VALUES ('bob', 'idle', 'ctx-bob', '2026-09-24T00:00:00Z',"
            " 'bob-1', 'claude-opus-5-5')"
        )

    initialize_database(path)
    initialize_database(path)

    columns = _agent_columns(path)
    assert columns["checkin_remote_addr"] == ("TEXT", 0, None)
    assert columns["last_remote_addr"] == ("TEXT", 0, None)
    with database(path) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION == 12
    store = HubStore(path)
    bob = store.agent_by_name("bob")
    assert bob is not None
    assert bob.declared_model == "claude-opus-5-5"
    assert (bob.checkin_remote_addr, bob.last_remote_addr) == (None, None)
    [state] = store.get_state()["agents"]
    assert (state["checkin_remote_addr"], state["last_remote_addr"]) == (None, None)

    # The first heartbeat after the upgrade is what fills a legacy row in.
    assert store.heartbeat("bob", "bob-1", None, remote_addr="192.0.2.10")
    bob = store.agent_by_name("bob")
    assert bob is not None
    assert (bob.checkin_remote_addr, bob.last_remote_addr) == (None, "192.0.2.10")


def test_a_null_declared_model_reads_as_unknown(tmp_path: Path) -> None:
    path = tmp_path / "hub.db"
    initialize_database(path)
    with database(path) as connection:
        connection.execute(
            "INSERT INTO agent (name, status, context_id, last_seen, declared_model)"
            " VALUES ('bob', 'idle', 'ctx-bob', '2026-09-20T00:00:00Z', NULL)"
        )

    bob = HubStore(path).agent_by_name("bob")

    assert bob is not None and bob.declared_model == UNKNOWN


def test_call_log_accepts_only_known_boundaries(tmp_path: Path) -> None:
    path = tmp_path / "hub.db"
    initialize_database(path)

    with pytest.raises(sqlite3.IntegrityError), database(path) as connection:
        connection.execute(
            "INSERT INTO call_log (boundary, actor, tool, outcome, bytes_in, bytes_out,"
            " started, finished) VALUES ('smtp', 'bob', 'x', 'ok', 1, 1, 't', 't')"
        )
