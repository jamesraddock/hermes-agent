"""Ownership transfer remains fail-closed through interruption and alias intake."""
import json
import hashlib
from pathlib import Path
import sqlite3

import pytest
from hermes_cli import kanban_db as kb, kanban_db_connect as kbc, kanban_workflow as wf
from hermes_cli import kanban_workflow_admission as ad
from hermes_cli import kanban_workflow_migration as migration


@pytest.fixture
def transfer(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr(Path, "home", lambda: tmp_path.parent)
    for key in ("HERMES_KANBAN_DB", "HERMES_KANBAN_BOARD", "HERMES_KANBAN_TASK", "HERMES_KANBAN_ROOT"):
        monkeypatch.delenv(key, raising=False)
    for name in ("old", "new", "other"):
        kb.create_board(name)
    source, target = kbc.connect(board="old"), kbc.connect(board="new")
    definition = {"id": "v1", "start": "plan", "steps": {
        "plan": {"profile": "planner", "model": "model", "provider": "provider", "effort": "high", "next": "migration_hold"},
        "migration_hold": {"hold": True, "decisions": {"revise": "plan"}}}}
    wf.configure(target, definition)
    first = kb.create_task(source, title="old root", idempotency_key="promote:github:org/repo#1", assignee="planner")
    second = kb.create_task(source, title="old child", idempotency_key="github:org/repo#2:plan", assignee="planner")
    source.execute("UPDATE tasks SET status='todo' WHERE id=?", (second,)); source.commit()
    artifact = tmp_path / "snapshot.json"; artifact.write_text('{"holds":["release"]}')
    manifest = {"transfer_id": "test-transfer", "issue": "github:org/repo#1", "issue_keys": ["github:org/repo#1", "github:org/repo#2"],
                "title": "canonical", "body": "preserve release hold", "workspace_path": str(tmp_path),
                "source_tasks": migration.snapshot(source, [first, second]), "artifacts": [str(artifact)], "operator_note": "workers and external outcomes reconciled"}
    manifest["artifact_sha256"] = {str(artifact): hashlib.sha256(artifact.read_bytes()).hexdigest()}
    wf.set_admission(source, paused=True)
    yield source, target, manifest
    source.close(); target.close()


def test_transfer_retains_history_and_resolves_every_alias(transfer):
    source, target, manifest = transfer
    tid = migration.migrate(target, source, manifest)
    assert migration.migrate(target, source, manifest) == tid
    assert migration.snapshot(source, manifest["source_tasks"]) == manifest["source_tasks"]
    assert kb.get_task(target, tid).current_step_key == "migration_hold"
    for key in manifest["issue_keys"]:
        assert ad.owners(target, key) == [(ad.db_path(target), tid)]
        assert wf.start(target, issue=key, title="duplicate", workspace_path=manifest["workspace_path"]) == tid
    wf.set_admission(source, paused=False)
    wf.set_enabled(target, True)
    for old in manifest["source_tasks"]:
        assert kb.claim_task(source, old) is None
        with pytest.raises(sqlite3.IntegrityError, match="retired"):
            source.execute("UPDATE tasks SET assignee='renamed' WHERE id=?", (old,))
    source.rollback()
    unrelated = kb.create_task(source, title="unrelated backlog", initial_status="blocked")
    source.execute("UPDATE tasks SET status='todo' WHERE id=?", (unrelated,)); source.commit()
    assert kb.recompute_ready(source) == 1
    assert kb.get_task(source, unrelated).status == "ready"
    assert kb.claim_task(target, tid) is None
    with kbc.connect_closing(board="other") as other:
        with pytest.raises(ValueError, match="owner"):
            kb.create_task(other, title="revival", idempotency_key="github:org/repo#2:implement")
    artifacts = wf.state(target, tid)["evidence"]["migration"]["data"]["artifacts"]
    assert Path(artifacts[0]).read_text() == '{"holds":["release"]}'


def test_interrupted_transfer_stays_held_and_retries_same_card(transfer, monkeypatch):
    source, target, manifest = transfer
    original = migration._check_sources
    calls = 0
    def fail_after_reservation(*args):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise RuntimeError("simulated crash")
        return original(*args)
    monkeypatch.setattr(migration, "_check_sources", fail_after_reservation)
    with pytest.raises(RuntimeError, match="simulated"):
        migration.migrate(target, source, manifest)
    tid = target.execute("SELECT task_id FROM kanban_workflow_migrations").fetchone()[0]
    assert not target.execute("SELECT finalized FROM kanban_workflow_migrations").fetchone()[0]
    with pytest.raises(ValueError, match="pending"):
        wf.decide(target, tid, expected_revision=0, packet_digest="", decision="revise", note="recover", decision_id="a")
    wf.set_admission(source, paused=False)
    assert all(kb.claim_task(source, old) is None for old in manifest["source_tasks"])
    wf.set_admission(source, paused=True)
    monkeypatch.setattr(migration, "_check_sources", original)
    assert migration.migrate(target, source, manifest) == tid
    assert target.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 1
    with pytest.raises(ValueError, match="changed manifest"):
        migration.migrate(target, source, {**manifest, "body": "altered"})


def test_changed_source_or_active_worker_refuses_transfer(transfer):
    source, target, manifest = transfer
    old = next(iter(manifest["source_tasks"]))
    source.execute("UPDATE tasks SET body='changed' WHERE id=?", (old,)); source.commit()
    with pytest.raises(ValueError, match="source task changed"):
        migration.migrate(target, source, manifest)
    assert not target.execute("SELECT 1 FROM tasks").fetchone()


def test_crash_after_retirement_keeps_one_stopped_destination(transfer, monkeypatch):
    source, target, manifest = transfer
    append = kb._append_event
    def fail_final(conn, task_id, kind, *args, **kwargs):
        if kind == "workflow_migrated":
            raise RuntimeError("crash before final commit")
        return append(conn, task_id, kind, *args, **kwargs)
    monkeypatch.setattr(kb, "_append_event", fail_final)
    with pytest.raises(RuntimeError, match="final commit"):
        migration.migrate(target, source, manifest)
    tid = target.execute("SELECT task_id FROM kanban_workflow_migrations").fetchone()[0]
    assert source.execute("SELECT COUNT(*) FROM kanban_task_retirements").fetchone()[0] == 2
    wf.set_admission(source, paused=False)
    wf.set_enabled(target, True)
    assert kb.claim_task(target, tid) is None
    assert all(kb.claim_task(source, old) is None for old in manifest["source_tasks"])
    wf.set_enabled(target, False)
    wf.set_admission(source, paused=True)
    monkeypatch.setattr(kb, "_append_event", append)
    assert migration.migrate(target, source, manifest) == tid


def test_changed_artifact_refuses_transfer(transfer):
    source, target, manifest = transfer
    Path(manifest["artifacts"][0]).write_text('changed approval')
    with pytest.raises(ValueError, match="artifact changed"):
        migration.migrate(target, source, manifest)
    assert target.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 0
