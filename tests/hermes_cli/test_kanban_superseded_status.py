"""Board Integrity v2 status and dependency invariants."""

from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


def test_superseded_parent_does_not_satisfy_child(kanban_home):
    with kb.connect() as conn:
        parent = kb.create_task(conn, title="obsolete route", assignee="ops")
        child = kb.create_task(
            conn, title="dependent route", assignee="ops", parents=[parent]
        )
        conn.execute(
            "UPDATE tasks SET status = 'superseded' WHERE id = ?", (parent,)
        )
        conn.commit()

        assert kb.recompute_ready(conn) == 0
        assert kb.get_task(conn, child).status == "todo"
        assert kb.claim_task(conn, child) is None


def test_archived_parent_still_satisfies_child(kanban_home):
    with kb.connect() as conn:
        parent = kb.create_task(conn, title="retired prerequisite", assignee="ops")
        child = kb.create_task(
            conn, title="dependent route", assignee="ops", parents=[parent]
        )

        assert kb.archive_task(conn, parent)
        assert kb.get_task(conn, child).status == "ready"


def test_create_under_archived_parent_starts_ready(kanban_home):
    with kb.connect() as conn:
        parent = kb.create_task(conn, title="retired prerequisite", assignee="ops")
        assert kb.archive_task(conn, parent)

        child = kb.create_task(
            conn, title="new dependent", assignee="ops", parents=[parent]
        )

        assert kb.get_task(conn, child).status == "ready"


def test_link_under_archived_parent_does_not_demote(kanban_home):
    with kb.connect() as conn:
        parent = kb.create_task(conn, title="retired prerequisite", assignee="ops")
        assert kb.archive_task(conn, parent)
        child = kb.create_task(conn, title="new dependent", assignee="ops")

        kb.link_tasks(conn, parent, child)

        assert kb.get_task(conn, child).status == "ready"


def test_delete_archived_accepts_superseded(kanban_home):
    with kb.connect() as conn:
        task_id = kb.create_task(conn, title="obsolete route", assignee="ops")
        conn.execute(
            "UPDATE tasks SET status = 'superseded' WHERE id = ?", (task_id,)
        )
        conn.commit()

        assert kb.delete_archived_task(conn, task_id) is True
        assert kb.get_task(conn, task_id) is None


def test_archive_cannot_turn_superseded_into_satisfying_parent(kanban_home):
    with kb.connect() as conn:
        parent = kb.create_task(conn, title="obsolete route", assignee="ops")
        child = kb.create_task(
            conn, title="dependent route", assignee="ops", parents=[parent]
        )
        conn.execute("UPDATE tasks SET status='superseded' WHERE id=?", (parent,))
        conn.commit()

        assert kb.archive_task(conn, parent) is False
        assert kb.get_task(conn, parent).status == "superseded"
        assert kb.recompute_ready(conn) == 0
        assert kb.get_task(conn, child).status == "todo"
