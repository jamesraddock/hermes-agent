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
"""

