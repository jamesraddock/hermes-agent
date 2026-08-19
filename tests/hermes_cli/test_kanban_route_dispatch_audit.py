"""Pre-dispatch Board Integrity route audit tests."""

import json
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
    root = home / "kanban" / "route-templates"
    root.mkdir(parents=True)
    (root / "audit.json").write_text(
        json.dumps(
            {
                "template_version": 1,
                "template_id": "audit",
                "steps": {
                    "review": {
                        "assignee": "reviewer",
                        "skills_required": ["review-skill"],
                        "route_identity": ["issue_key", "plan_revision", "focus"],
                        "required_meta": {
                            "issue_key": r"^\S+$",
                            "plan_revision": r"^[1-9][0-9]*$",
                            "focus": r"^\S+$",
                        },
                        "parent_steps_all": [],
                        "parent_steps_any": [],
                        "worker_parent_self_required": False,
                    },
                    "gate": {
                        "assignee": "gatekeeper",
                        "skills_required": ["gate-skill"],
                        "route_identity": ["issue_key", "focus"],
                        "required_meta": {"issue_key": r"^\S+$", "focus": r"^\S+$"},
                        "parent_steps_all": [],
                        "parent_steps_any": [],
                        "worker_parent_self_required": False,
                        "gate": {"completers": ["gatekeeper"], "human": False},
                    },
                    "successor": {
                        "assignee": "worker",
                        "skills_required": ["work-skill"],
                        "route_identity": ["issue_key", "focus"],
                        "required_meta": {"issue_key": r"^\S+$", "focus": r"^\S+$"},
                        "parent_steps_all": ["gate"],
                        "parent_steps_any": [],
                        "worker_parent_self_required": False,
                    },
                },
            }
        ),
        encoding="utf-8",
    )
    return home


def _route(conn):
    return kb.create_task(
        conn,
        title="review",
        assignee="reviewer",
        skills=["review-skill"],
        workflow_template_id="audit",
        step_key="review",
        route_meta={
            "issue_key": "github:o/r#1",
            "plan_revision": 1,
            "focus": "final",
        },
    )


def test_enforce_skips_then_quarantines_sticky(
    kanban_home, monkeypatch, all_assignees_spawnable
):
    monkeypatch.setattr(
        kb,
        "_integrity_mode",
        lambda name: "enforce" if name == "dispatch_audit" else "report",
    )
    with kb.connect() as conn:
        task_id = _route(conn)
        conn.execute("UPDATE tasks SET assignee='wrong' WHERE id=?", (task_id,))
        conn.commit()

        first = kb.dispatch_once(conn, spawn_fn=lambda *_a, **_k: 99)
        assert first.spawned == []
        assert (task_id, "skipped") in first.route_audited
        assert kb.get_task(conn, task_id).status == "ready"

        second = kb.dispatch_once(conn, spawn_fn=lambda *_a, **_k: 99)
        assert second.spawned == []
        assert (task_id, "quarantined") in second.route_audited
        assert kb.get_task(conn, task_id).status == "blocked"
        assert kb._has_sticky_block(conn, task_id)
        blocked = [e for e in kb.list_events(conn, task_id) if e.kind == "blocked"][-1]
        assert blocked.payload["reason"].startswith("route-quarantine:")


def test_generic_task_never_audited(
    kanban_home, monkeypatch, all_assignees_spawnable
):
    monkeypatch.setattr(kb, "_integrity_mode", lambda _name: "enforce")
    with kb.connect() as conn:
        task_id = kb.create_task(conn, title="generic", assignee="worker")
        result = kb.dispatch_once(conn, spawn_fn=lambda *_a, **_k: None)
        assert any(row[0] == task_id for row in result.spawned)
        assert result.route_audited == []


def test_nonhuman_gate_requires_authorized_completer_run(kanban_home):
    with kb.connect() as conn:
        gate = kb.create_task(
            conn,
            title="gate",
            assignee="gatekeeper",
            skills=["gate-skill"],
            workflow_template_id="audit",
            step_key="gate",
            route_meta={"issue_key": "github:o/r#2", "focus": "ci"},
        )
        kb.claim_task(conn, gate)
        assert kb.complete_task(conn, gate, summary="done")
        conn.execute(
            "UPDATE task_runs SET profile='wrong-profile' WHERE task_id=?",
            (gate,),
        )
        conn.commit()
        successor = kb.create_task(
            conn,
            title="successor",
            assignee="worker",
            skills=["work-skill"],
            parents=[gate],
            workflow_template_id="audit",
            step_key="successor",
            route_meta={"issue_key": "github:o/r#2", "focus": "next"},
        )

        violations = kb._audit_route_before_dispatch(conn, successor)

        assert any(v["code"] == "route_gate_provenance_invalid" for v in violations)
