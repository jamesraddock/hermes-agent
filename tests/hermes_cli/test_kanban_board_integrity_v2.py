"""Board Integrity v2 identity, routing, quarantine, and repair contracts."""

import json
import concurrent.futures
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_diagnostics as kd


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


def _template(home: Path) -> None:
    root = home / "kanban" / "route-templates"
    root.mkdir(parents=True, exist_ok=True)
    (root / "agentic-feature-v1.json").write_text(
        json.dumps(
            {
                "template_version": 1,
                "template_id": "agentic-feature-v1",
                "lineage_identity": ["issue_key"],
                "steps": {
                    "plan-review": {
                        "assignee": "feature-plan-verifier",
                        "skills_required": ["feature-plan-review"],
                        "route_identity": ["issue_key", "plan_revision", "focus"],
                        "required_meta": {
                            "issue_key": r"^\S+$",
                            "plan_revision": r"^[1-9][0-9]*$",
                            "focus": r"^\S+$",
                            "plan_package_hash": r"^[0-9a-f]{64}$",
                        },
                        "parent_steps_all": [],
                        "parent_steps_any": [],
                        "worker_parent_self_required": False,
                    },
                    "successor": {
                        "assignee": "feature-planner",
                        "skills_required": ["feature-plan"],
                        "route_identity": ["issue_key", "plan_revision", "focus"],
                        "required_meta": {
                            "issue_key": r"^\S+$",
                            "plan_revision": r"^[1-9][0-9]*$",
                            "focus": r"^\S+$",
                        },
                        "parent_steps_all": ["merge-staging", "gate:post-merge-ci"],
                        "parent_steps_any": [],
                        "worker_parent_self_required": False,
                    },
                    "fix": {
                        "assignee": "feature-implementer",
                        "skills_required": ["feature-fix-review-comments"],
                        "route_identity": ["issue_key", "reviewed_commit", "focus"],
                        "required_meta": {
                            "issue_key": r"^\S+$",
                            "reviewed_commit": r"^[0-9a-f]{40}$",
                            "focus": r"^\S+$",
                        },
                        "parent_steps_all": [],
                        "parent_steps_any": ["plan-review", "successor"],
                        "worker_parent_self_required": False,
                    },
                },
            }
        ),
        encoding="utf-8",
    )


def _route_kwargs():
    return {
        "workflow_template_id": "agentic-feature-v1",
        "step_key": "plan-review",
        "route_meta": {
            "issue_key": "github:owner/repo#1",
            "plan_revision": 1,
            "focus": "final-recheck",
            "plan_package_hash": "a" * 64,
        },
        "assignee": "feature-plan-verifier",
        "skills": ["feature-plan-review"],
    }


def test_fingerprint_canonicalization_and_persistence(kanban_home):
    _template(kanban_home)
    with kb.connect() as conn:
        first = kb.create_task(conn, title="Review", body="a\r\nb\r", **_route_kwargs())
        task = kb.get_task(conn, first)
        assert task.workflow_template_id == "agentic-feature-v1"
        assert task.current_step_key == "plan-review"
        assert task.spec_fingerprint and len(task.spec_fingerprint) == 64
        assert task.route_fingerprint and len(task.route_fingerprint) == 64
        assert json.loads(task.route_meta)["focus"] == "final-recheck"

        spec = kb._task_spec_from_row(conn, first)
        assert kb._compute_spec_fingerprint(spec) == task.spec_fingerprint
        assert spec["body_sha256"] == kb._sha256_text("a\nb\n")

        before_events = len(kb.list_events(conn, first))
        conn.execute("UPDATE tasks SET spec_fingerprint=NULL WHERE id=?", (first,))
        conn.commit()
        assert kb.backfill_spec_fingerprints(conn, batch_size=1) == 1
        assert kb.backfill_spec_fingerprints(conn, batch_size=1) == 0
        assert len(kb.list_events(conn, first)) == before_events


def test_same_key_mismatch_report_and_enforce(kanban_home, monkeypatch):
    with kb.connect() as conn:
        original = kb.create_task(
            conn, title="one", assignee="ops", idempotency_key="stable"
        )
        monkeypatch.setattr(kb, "_integrity_mode", lambda _name: "report")
        reported = kb.create_task(
            conn, title="two", assignee="ops", idempotency_key="stable"
        )
        assert reported == original
        assert any(e.kind == "idempotency_spec_mismatch" for e in kb.list_events(conn, original))

        monkeypatch.setattr(kb, "_integrity_mode", lambda _name: "enforce")
        with pytest.raises(kb.IdempotencySpecMismatch) as exc:
            kb.create_task(
                conn, title="three", assignee="ops", idempotency_key="stable"
            )
        assert exc.value.existing_task_id == original
        assert "title" in exc.value.diff
        assert "three" not in json.dumps(exc.value.diff)
        assert conn.execute("SELECT count(*) FROM tasks").fetchone()[0] == 1


def test_variant_key_duplicate_route_rejected(kanban_home, monkeypatch):
    _template(kanban_home)
    monkeypatch.setattr(kb, "_integrity_mode", lambda _name: "enforce")
    with kb.connect() as conn:
        first = kb.create_task(conn, title="review", idempotency_key="v1", **_route_kwargs())
        with pytest.raises(kb.DuplicateRouteError) as exc:
            kb.create_task(conn, title="review", idempotency_key="v1-corrected", **_route_kwargs())
        assert exc.value.existing_task_id == first
        assert conn.execute("SELECT count(*) FROM tasks").fetchone()[0] == 1


def test_route_validation_parent_steps_report_and_enforce(kanban_home, monkeypatch):
    _template(kanban_home)
    kwargs = {
        "title": "premature successor",
        "assignee": "feature-planner",
        "skills": ["feature-plan"],
        "workflow_template_id": "agentic-feature-v1",
        "step_key": "successor",
        "route_meta": {
            "issue_key": "github:owner/repo#2",
            "plan_revision": 1,
            "focus": "plan",
        },
    }
    with kb.connect() as conn:
        monkeypatch.setattr(kb, "_integrity_mode", lambda _name: "report")
        task_id = kb.create_task(conn, **kwargs)
        event = [e for e in kb.list_events(conn, task_id) if e.kind == "route_validation_failed"]
        assert event and set(event[-1].payload["missing_parent_steps"]) == {
            "merge-staging",
            "gate:post-merge-ci",
        }

        monkeypatch.setattr(kb, "_integrity_mode", lambda _name: "enforce")
        with pytest.raises(kb.RouteValidationError):
            kb.create_task(conn, **{**kwargs, "title": "rejected"})
        assert conn.execute("SELECT count(*) FROM tasks").fetchone()[0] == 1


def test_route_validation_parent_steps_any_accepts_one_alternate(
    kanban_home, monkeypatch
):
    _template(kanban_home)
    monkeypatch.setattr(kb, "_integrity_mode", lambda _name: "enforce")
    with kb.connect() as conn:
        parent = kb.create_task(conn, title="review", **_route_kwargs())
        base = {
            "title": "fix",
            "assignee": "feature-implementer",
            "skills": ["feature-fix-review-comments"],
            "workflow_template_id": "agentic-feature-v1",
            "step_key": "fix",
        }
        meta = {
            "issue_key": "github:owner/repo#1",
            "reviewed_commit": "b" * 40,
            "focus": "fix",
        }

        child = kb.create_task(conn, parents=[parent], route_meta=meta, **base)
        assert kb.parent_ids(conn, child) == [parent]
        with pytest.raises(kb.RouteValidationError):
            kb.create_task(
                conn,
                idempotency_key="invalid-fix",
                route_meta={**meta, "focus": "invalid"},
                **base,
            )


def test_typed_route_rejects_cross_issue_and_extra_parent_edges(
    kanban_home, monkeypatch
):
    _template(kanban_home)
    monkeypatch.setattr(kb, "_integrity_mode", lambda _name: "enforce")
    with kb.connect() as conn:
        local_parent = kb.create_task(conn, title="local", **_route_kwargs())
        foreign_kwargs = _route_kwargs()
        foreign_kwargs["route_meta"] = {
            **foreign_kwargs["route_meta"],
            "issue_key": "github:owner/repo#999",
        }
        foreign_parent = kb.create_task(conn, title="foreign", **foreign_kwargs)
        generic_parent = kb.create_task(conn, title="generic", assignee="ops")
        base = {
            "title": "fix",
            "assignee": "feature-implementer",
            "skills": ["feature-fix-review-comments"],
            "workflow_template_id": "agentic-feature-v1",
            "step_key": "fix",
            "route_meta": {
                "issue_key": "github:owner/repo#1",
                "reviewed_commit": "b" * 40,
                "focus": "fix",
            },
        }

        with pytest.raises(kb.RouteValidationError) as cross_issue:
            kb.create_task(
                conn,
                parents=[foreign_parent],
                idempotency_key="cross-issue",
                **base,
            )
        assert any(
            violation.code == "route_parent_lineage_mismatch"
            for violation in cross_issue.value.violations
        )

        child = kb.create_task(conn, parents=[local_parent], **base)
        with pytest.raises(kb.RouteValidationError) as extra:
            kb.link_tasks(conn, generic_parent, child)
        assert any(
            violation.code == "route_parent_template_mismatch"
            for violation in extra.value.violations
        )
        with pytest.raises(kb.RouteValidationError):
            kb.unlink_tasks(conn, local_parent, child)
        assert kb.parent_ids(conn, child) == [local_parent]


def test_config_backed_route_validation_enforces_parent_mutations(
    kanban_home, monkeypatch
):
    _template(kanban_home)
    from hermes_cli import config as config_module

    monkeypatch.setattr(
        config_module,
        "load_config_readonly",
        lambda: {"kanban": {"integrity": {"route_validation": "enforce"}}},
    )
    with kb.connect() as conn:
        parent = kb.create_task(conn, title="review", **_route_kwargs())
        generic_parent = kb.create_task(conn, title="generic", assignee="ops")
        child = kb.create_task(
            conn,
            title="fix",
            assignee="feature-implementer",
            skills=["feature-fix-review-comments"],
            parents=[parent],
            workflow_template_id="agentic-feature-v1",
            step_key="fix",
            route_meta={
                "issue_key": "github:owner/repo#1",
                "reviewed_commit": "b" * 40,
                "focus": "fix",
            },
        )

        with pytest.raises(kb.RouteValidationError):
            kb.link_tasks(conn, generic_parent, child)
        assert kb.parent_ids(conn, child) == [parent]


def test_quarantine_is_sticky_from_todo_and_blocked(kanban_home):
    with kb.connect() as conn:
        parent = kb.create_task(conn, title="parent", assignee="ops")
        todo = kb.create_task(conn, title="todo", assignee="ops", parents=[parent])
        blocked = kb.create_task(
            conn, title="blocked", assignee="ops", initial_status="blocked"
        )
        for task_id in (todo, blocked):
            assert kb.quarantine_task(conn, task_id, reason="duplicate route", actor="operator")
            task = kb.get_task(conn, task_id)
            assert task.status == "blocked"
            assert task.block_kind == "needs_input"
            assert kb._has_sticky_block(conn, task_id)
        assert kb.recompute_ready(conn) == 0


def test_supersede_route_relinks_without_promoting_children(kanban_home):
    with kb.connect() as conn:
        loser = kb.create_task(conn, title="bad", assignee="ops", idempotency_key="bad")
        winner = kb.create_task(conn, title="good", assignee="ops", idempotency_key="good")
        gate = kb.create_task(conn, title="gate", assignee="ops", parents=[loser])

        result = kb.supersede_route(
            conn,
            loser_ids=[loser],
            winner_id=winner,
            actor="operator",
            reason="canonicalize route",
        )

        assert result.winner_id == winner
        assert kb.get_task(conn, loser).status == "superseded"
        assert kb.parent_ids(conn, gate) == [winner]
        assert kb.get_task(conn, gate).status == "todo"
        assert any(e.kind == "route_repaired" for e in kb.list_events(conn, winner))


def test_board_diagnostics_find_duplicates_without_writes(kanban_home):
    with kb.connect() as conn:
        first = kb.create_task(conn, title="one", assignee="ops")
        second = kb.create_task(conn, title="two", assignee="ops")
        # Simulate a legacy/pre-enforcement board that can contain duplicates.
        kb.disable_integrity_uniqueness(conn)
        conn.execute("UPDATE tasks SET idempotency_key='dup' WHERE id IN (?, ?)", (first, second))
        conn.commit()
        before = conn.total_changes

        diags = kd.compute_board_diagnostics(conn)

        assert any(d.kind == "duplicate_active_idempotency_key" for d in diags)
        assert conn.total_changes == before


def test_parent_link_mutations_refresh_child_fingerprint(kanban_home):
    with kb.connect() as conn:
        parent = kb.create_task(conn, title="parent", assignee="ops")
        child = kb.create_task(conn, title="child", assignee="ops")
        original = kb.get_task(conn, child).spec_fingerprint

        kb.link_tasks(conn, parent, child)
        linked = kb.get_task(conn, child).spec_fingerprint
        assert linked != original
        assert linked == kb._compute_spec_fingerprint(kb._task_spec_from_row(conn, child))

        assert kb.unlink_tasks(conn, parent, child)
        unlinked = kb.get_task(conn, child).spec_fingerprint
        assert unlinked == original
        assert unlinked == kb._compute_spec_fingerprint(kb._task_spec_from_row(conn, child))


def test_create_task_ex_reports_route_dedupe_under_variant_key(
    kanban_home, monkeypatch
):
    _template(kanban_home)
    monkeypatch.setattr(kb, "_integrity_mode", lambda _name: "report")
    with kb.connect() as conn:
        first = kb.create_task_ex(
            conn, title="review", idempotency_key="v1", **_route_kwargs()
        )
        second = kb.create_task_ex(
            conn, title="review", idempotency_key="v1-corrected", **_route_kwargs()
        )
        assert first.created is True and first.deduped is False
        assert second.task_id == first.task_id
        assert second.created is False and second.deduped is True


def test_board_diagnostics_cover_drift_quarantine_and_missing_template(
    kanban_home
):
    _template(kanban_home)
    with kb.connect() as conn:
        task_id = kb.create_task(conn, title="review", **_route_kwargs())
        conn.execute(
            "UPDATE tasks SET title='drifted', route_fingerprint=NULL WHERE id=?",
            (task_id,),
        )
        conn.commit()
        assert kb.quarantine_task(
            conn, task_id, reason="bad route", actor="operator"
        )
        (kanban_home / "kanban" / "route-templates" / "agentic-feature-v1.json").unlink()

        kinds = {diag.kind for diag in kd.compute_board_diagnostics(conn)}

        assert {
            "spec_fingerprint_drift",
            "route_task_missing_fingerprint",
            "route_template_unresolvable",
            "quarantined_routes_pending",
        }.issubset(kinds)


def test_board_diagnostics_flags_child_advanced_past_superseded_parent(
    kanban_home
):
    with kb.connect() as conn:
        parent = kb.create_task(conn, title="obsolete", assignee="ops")
        child = kb.create_task(conn, title="child", assignee="ops", parents=[parent])
        conn.execute("UPDATE tasks SET status='superseded' WHERE id=?", (parent,))
        conn.execute("UPDATE tasks SET status='ready' WHERE id=?", (child,))
        conn.commit()

        diagnostics = kd.compute_board_diagnostics(conn)

        unsafe = [d for d in diagnostics if d.kind == "superseded_satisfying_child"]
        assert unsafe and unsafe[0].data == {
            "parent_id": parent,
            "child_id": child,
        }


def test_concurrent_same_key_creation_has_one_winner(kanban_home):
    def create_once():
        with kb.connect() as conn:
            return kb.create_task(
                conn,
                title="same",
                assignee="ops",
                idempotency_key="concurrent-key",
            )

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        task_ids = list(pool.map(lambda _index: create_once(), range(2)))

    assert task_ids[0] == task_ids[1]
    with kb.connect() as conn:
        assert conn.execute(
            "SELECT count(*) FROM tasks WHERE idempotency_key='concurrent-key'"
        ).fetchone()[0] == 1


@pytest.mark.parametrize("mode", ["report", "enforce"])
def test_concurrent_same_key_different_specs_has_one_winner(
    kanban_home, monkeypatch, mode
):
    monkeypatch.setattr(kb, "_integrity_mode", lambda _name: mode)

    def create_once(title):
        try:
            with kb.connect() as conn:
                return (
                    "ok",
                    kb.create_task(
                        conn,
                        title=title,
                        assignee="ops",
                        idempotency_key=f"concurrent-mismatch-{mode}",
                    ),
                )
        except kb.IdempotencySpecMismatch as exc:
            return "mismatch", exc.existing_task_id

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(create_once, ["alpha", "beta"]))

    assert len({task_id for _, task_id in outcomes}) == 1
    if mode == "report":
        assert [kind for kind, _ in outcomes].count("ok") == 2
    else:
        assert sorted(kind for kind, _ in outcomes) == ["mismatch", "ok"]
    with kb.connect() as conn:
        assert conn.execute(
            "SELECT count(*) FROM tasks WHERE idempotency_key=?",
            (f"concurrent-mismatch-{mode}",),
        ).fetchone()[0] == 1


def test_supersede_replacement_failure_rolls_back_everything(kanban_home):
    with kb.connect() as conn:
        loser = kb.create_task(conn, title="bad", assignee="ops")
        child = kb.create_task(conn, title="child", assignee="ops", parents=[loser])

        with pytest.raises(ValueError):
            kb.supersede_route(
                conn,
                loser_ids=[loser],
                replacement_spec={"title": "", "assignee": "ops"},
                actor="operator",
                reason="replacement should fail",
            )

        assert kb.get_task(conn, loser).status == "ready"
        assert kb.parent_ids(conn, child) == [loser]
        assert not any(
            event.kind == "route_superseded"
            for event in kb.list_events(conn, loser)
        )


def test_enable_uniqueness_refuses_legacy_duplicates_atomically(kanban_home):
    with kb.connect() as conn:
        first = kb.create_task(conn, title="one", assignee="ops")
        second = kb.create_task(conn, title="two", assignee="ops")
        kb.disable_integrity_uniqueness(conn)
        conn.execute(
            "UPDATE tasks SET idempotency_key='dup' WHERE id IN (?, ?)",
            (first, second),
        )
        conn.commit()

        with pytest.raises(ValueError, match="duplicate groups"):
            kb.enable_integrity_uniqueness(conn)

        unique_indexes = {
            row["name"]
            for row in conn.execute("PRAGMA index_list('tasks')").fetchall()
            if row["unique"]
        }
        assert "idx_tasks_idem_active" not in unique_indexes
        assert "idx_tasks_route_active" not in unique_indexes


def test_worker_route_creation_requires_current_task_parent(
    kanban_home, monkeypatch
):
    _template(kanban_home)
    path = kanban_home / "kanban" / "route-templates" / "agentic-feature-v1.json"
    template = json.loads(path.read_text(encoding="utf-8"))
    template["steps"]["plan-review"]["worker_parent_self_required"] = True
    path.write_text(json.dumps(template), encoding="utf-8")
    monkeypatch.setattr(kb, "_integrity_mode", lambda _name: "enforce")
    with kb.connect() as conn:
        current = kb.create_task(conn, title="current", assignee="ops")
        monkeypatch.setenv("HERMES_KANBAN_TASK", current)

        with pytest.raises(kb.RouteValidationError):
            kb.create_task(conn, title="review", **_route_kwargs())

        assert conn.execute("SELECT count(*) FROM tasks").fetchone()[0] == 1
