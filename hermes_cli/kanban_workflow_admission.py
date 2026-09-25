"""Issue ownership arbitration over native board records, never a second ledger.

One lock per issue, before any board write transaction. Cooperating entrypoints
share the installation root; legacy mirrors remain intake, not execution owners.
"""
from __future__ import annotations

import contextlib
import hashlib
import re
import sqlite3
import time
from pathlib import Path

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc

_ISSUE = re.compile(r"(?:github:)?([\w.-]+/[\w.-]+)#([1-9][0-9]*)", re.IGNORECASE)


def issue_key(value):
    match = _ISSUE.fullmatch(str(value).strip())
    if not match:
        raise ValueError("issue must be github:owner/repository#number")
    return "github:" + match[1].lower() + "#" + str(int(match[2]))


def execution_key(value):
    if not isinstance(value, str):
        return None
    for prefix in ("feature:", "promote:"):
        if value.startswith(prefix):
            return issue_key(value[len(prefix):])
    return None


def owner_key(value):
    key = execution_key(value)
    if key:
        return key
    if isinstance(value, str) and value.startswith("github:") and ":" in value[7:]:
        return issue_key(value.rsplit(":", 1)[0])
    return None


def db_path(conn):
    return Path(next(row[2] for row in conn.execute("PRAGMA database_list") if row[1] == "main")).resolve()


def installation_root(conn):
    path = db_path(conn)
    return path.parent.parent.parent.parent if path.parent.parent.name == "boards" else path.parent


@contextlib.contextmanager
def issue_lock(conn, key):
    if conn.in_transaction:
        raise RuntimeError("issue admission must precede a board transaction")
    root = installation_root(conn) / "kanban" / "admission-locks"
    root.mkdir(parents=True, exist_ok=True)
    handle = (root / (hashlib.sha256(key.encode()).hexdigest() + ".lock")).open("a+b")
    held = False
    try:
        deadline = time.monotonic() + 10
        while not held:
            held = kbc._try_lock_nb(handle)
            if not held:
                if time.monotonic() >= deadline:
                    raise RuntimeError("issue admission is busy; retry later")
                time.sleep(0.02)
        yield
    finally:
        if held:
            kbc._unlock(handle)
        handle.close()


def owners(conn, key):
    root, own = installation_root(conn), db_path(conn)
    paths = {own}
    default = root / "kanban.db"
    if default.exists():
        paths.add(default)
    paths.update((root / "kanban" / "boards").rglob("kanban.db"))
    found = []
    for path in sorted(paths):
        # Never connect through profile/env resolution: a worker pins its own DB.
        other = None
        try:
            c = conn if path.resolve() == own else sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=2)
            if c is not conn:
                other = c
            for row in c.execute("SELECT id, idempotency_key FROM tasks WHERE "
                                 "idempotency_key LIKE 'feature:%' OR idempotency_key LIKE 'promote:%' "
                                 "OR idempotency_key LIKE 'github:%:%'"):
                if owner_key(row[1]) == key:
                    target = _transferred_owner(c, row[0])
                    found.append(target or (path.resolve(), row[0]))
            if _has_table(c, "kanban_workflow_migrations"):
                import json
                for migration in c.execute("SELECT task_id, manifest FROM kanban_workflow_migrations"):
                    if key in json.loads(migration[1])["issue_keys"]:
                        found.append((path.resolve(), migration[0]))
            if _has_table(c, "kanban_task_retirements"):
                import json
                for retired in c.execute("SELECT task_id, issue_keys FROM kanban_task_retirements"):
                    if key in json.loads(retired[1]):
                        target = _transferred_owner(c, retired[0])
                        found.append(target or (path.resolve(), retired[0]))
        finally:
            if other:
                other.close()
    return sorted(set(found))


def _has_table(conn, name):
    return bool(conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)).fetchone())


def _transferred_owner(conn, task_id):
    if not _has_table(conn, "kanban_task_retirements"):
        return None
    row = conn.execute("SELECT target_db, target_task, manifest_hash FROM kanban_task_retirements WHERE task_id=?", (task_id,)).fetchone()
    if not row:
        return None
    target = Path(row[0])
    # Missing/unreadable target is an error, never permission to create another owner.
    with contextlib.closing(sqlite3.connect(target.as_uri() + "?mode=ro", uri=True, timeout=2)) as other:
        if not _has_table(other, "kanban_workflow_migrations"):
            return None
        receipt = other.execute("SELECT finalized, manifest_hash FROM kanban_workflow_migrations WHERE task_id=?", (row[1],)).fetchone()
        if receipt and receipt[0] and receipt[1] == row[2]:
            return target.resolve(), row[1]
    return None


@contextlib.contextmanager
def creation_guard(conn, idempotency_key, assignee=None):
    row = conn.execute("SELECT retired_assignees FROM kanban_admission_policy WHERE singleton=1").fetchone()
    if row:
        import json
        if assignee in json.loads(row[0]):
            raise ValueError("this board has retired that feature worker; use the replacement workflow")
    key = owner_key(idempotency_key)
    if key is None:
        yield None
        return
    with issue_lock(conn, key):
        found = owners(conn, key)
        local = [task for path, task in found if path == db_path(conn)]
        remote = [str(path) + ":" + task for path, task in found if path != db_path(conn)]
        if remote or len(local) > 1:
            raise ValueError("issue already has another execution owner: " + ", ".join(remote + local))
        yield local[0] if local else None


@contextlib.contextmanager
def claim_guard(conn, task_id):
    from hermes_cli.kanban_workflow import admission
    task = kb.get_task(conn, task_id)
    if not task or not admission(conn, task):
        yield False
        return
    key = owner_key(task.idempotency_key)
    if key is None:
        yield True
        return
    with issue_lock(conn, key):
        found = owners(conn, key)
        if task.idempotency_key.startswith("feature:"):
            yield found == [(db_path(conn), task_id)]
        else:
            # Existing phase graphs are one legacy ownership cohort. They may
            # continue until retirement; a different board cannot take them over.
            yield all(path == db_path(conn) for path, _ in found)
