"""Additive tables for native board workflows and dispatch admission."""
SCHEMA = """
CREATE TABLE IF NOT EXISTS kanban_workflow_config (
    singleton INTEGER PRIMARY KEY CHECK(singleton = 1),
    definition TEXT NOT NULL,
    enabled INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS kanban_workflow_state (
    task_id TEXT PRIMARY KEY REFERENCES tasks(id),
    revision INTEGER NOT NULL DEFAULT 0,
    evidence TEXT NOT NULL DEFAULT '{}',
    corrections TEXT NOT NULL DEFAULT '{}'
);
CREATE TABLE IF NOT EXISTS kanban_admission_policy (
    singleton INTEGER PRIMARY KEY CHECK(singleton = 1),
    paused INTEGER NOT NULL DEFAULT 0,
    retired_assignees TEXT NOT NULL DEFAULT '[]'
);
CREATE TABLE IF NOT EXISTS kanban_workflow_migrations (
    task_id TEXT PRIMARY KEY REFERENCES tasks(id),
    transfer_id TEXT NOT NULL UNIQUE,
    manifest_hash TEXT NOT NULL,
    manifest TEXT NOT NULL,
    finalized INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS kanban_task_retirements (
    task_id TEXT PRIMARY KEY REFERENCES tasks(id),
    target_db TEXT NOT NULL,
    target_task TEXT NOT NULL,
    manifest_hash TEXT NOT NULL,
    issue_keys TEXT NOT NULL
);
CREATE TRIGGER IF NOT EXISTS kanban_retired_task_update
BEFORE UPDATE ON tasks WHEN EXISTS (SELECT 1 FROM kanban_task_retirements WHERE task_id=OLD.id)
BEGIN SELECT RAISE(ABORT, 'task retired to replacement workflow'); END;
CREATE TRIGGER IF NOT EXISTS kanban_retired_task_delete
BEFORE DELETE ON tasks WHEN EXISTS (SELECT 1 FROM kanban_task_retirements WHERE task_id=OLD.id)
BEGIN SELECT RAISE(ABORT, 'task retired to replacement workflow'); END;
"""

