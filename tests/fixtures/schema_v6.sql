-- Schema of a real v6 database, dumped from the code that shipped it.
-- Regenerate with `uv run python scripts/dump-schema.py`; edit the schema, not this file.

CREATE TABLE agent (
    name TEXT PRIMARY KEY,
    capabilities_json TEXT NOT NULL DEFAULT '[]',
    status TEXT NOT NULL CHECK (status IN ('idle', 'busy', 'released', 'lost')),
    context_id TEXT UNIQUE,
    last_seen TEXT NOT NULL,
    worker_instance_id TEXT NOT NULL DEFAULT '',
    last_heartbeat TEXT NOT NULL DEFAULT '',
    last_progress_at TEXT,
    current_task_id TEXT,
    harness TEXT NOT NULL DEFAULT 'unknown',
    harness_version TEXT NOT NULL DEFAULT 'unknown',
    provider TEXT NOT NULL DEFAULT 'unknown',
    model TEXT NOT NULL DEFAULT 'unknown',
    model_source TEXT NOT NULL DEFAULT 'unknown' CHECK (model_source IN ('declared', 'env', 'unknown')),
    workspace_id TEXT,
    FOREIGN KEY (current_task_id) REFERENCES task(id) ON DELETE SET NULL
);

CREATE TABLE decision (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    summary TEXT NOT NULL,
    rationale TEXT NOT NULL,
    key TEXT UNIQUE
);

CREATE TABLE event (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT NOT NULL CHECK (kind IN ('agent_checked_in', 'task_progress', 'task_completed', 'task_failed', 'worker_question', 'lease_expired', 'agent_lost')),
    payload_json TEXT NOT NULL,
    state TEXT NOT NULL DEFAULT 'queued' CHECK (state IN ('queued', 'delivered', 'acked')),
    delivery_id TEXT,
    delivery_attempts INTEGER NOT NULL DEFAULT 0,
    delivered_at TEXT,
    delivery_expires TEXT,
    acked_at TEXT,
    ts TEXT NOT NULL
);

CREATE TABLE message (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id TEXT,
    context_id TEXT NOT NULL,
    sender TEXT NOT NULL,
    direction TEXT NOT NULL CHECK (direction IN ('to_alice', 'from_alice')),
    parts_json TEXT NOT NULL,
    ts TEXT NOT NULL,
    FOREIGN KEY (task_id) REFERENCES task(id) ON DELETE CASCADE
);

CREATE TABLE operation (
    actor TEXT NOT NULL,
    operation_id TEXT NOT NULL,
    payload_hash TEXT NOT NULL,
    response_json TEXT NOT NULL,
    created TEXT NOT NULL,
    PRIMARY KEY (actor, operation_id)
);

CREATE TABLE task (
    id TEXT PRIMARY KEY,
    workflow_id TEXT NOT NULL,
    assignee TEXT,
    role TEXT NOT NULL,
    title TEXT NOT NULL,
    instructions TEXT NOT NULL,
    state TEXT NOT NULL CHECK (state IN ('submitted', 'working', 'input-required', 'completed', 'failed', 'canceled')),
    lease_expires TEXT,
    lease_duration_s REAL NOT NULL DEFAULT 1800,
    result_json TEXT,
    created TEXT NOT NULL,
    updated TEXT NOT NULL,
    FOREIGN KEY (workflow_id) REFERENCES workflow(id) ON DELETE CASCADE,
    FOREIGN KEY (assignee) REFERENCES agent(name) ON DELETE SET NULL
);

CREATE TABLE workflow (
    id TEXT PRIMARY KEY,
    goal TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('active', 'paused', 'done', 'escalated')),
    policy_json TEXT NOT NULL DEFAULT '{}',
    created TEXT NOT NULL
);

CREATE UNIQUE INDEX idx_decision_key ON decision(key) WHERE key IS NOT NULL;

CREATE INDEX idx_event_delivery_id ON event(delivery_id);

CREATE INDEX idx_event_state_id ON event(state, id);

CREATE INDEX idx_message_context_ts ON message(context_id, ts);

CREATE INDEX idx_task_assignee ON task(assignee);

CREATE INDEX idx_task_workflow_state ON task(workflow_id, state);

PRAGMA user_version = 6;
