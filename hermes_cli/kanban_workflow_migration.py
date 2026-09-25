"""Operator-only, resumable ownership transfer between native boards.

The destination is reserved first and held. Source rows become immutable and
non-executable before finalization. Every intermediate state denies dispatch;
no cross-database atomic commit or archive-as-ownership-release is assumed.
"""
from __future__ import annotations

import contextlib
import hashlib
import json
from pathlib import Path

from hermes_cli import kanban_db as kb, kanban_workflow as wf
from hermes_cli import kanban_workflow_admission as admission


def snapshot(conn, task_ids):
    """Capture exact rows for an operator-reviewed compare-and-swap manifest."""
    result = {}
    for tid in sorted(task_ids):
        row = conn.execute("SELECT * FROM tasks WHERE id=?", (tid,)).fetchone()
        if not row:
            raise ValueError(f"missing source task: {tid}")
        result[tid] = dict(row)
    return result


def _check_sources(source, manifest, target_path, target_id, fingerprint):
    policy = source.execute("SELECT paused FROM kanban_admission_policy WHERE singleton=1").fetchone()
    if not policy or not policy[0]:
        raise ValueError("pause source-board admission during ownership transfer")
    for tid, expected in manifest["source_tasks"].items():
        current = snapshot(source, [tid])[tid]
        if current != expected:
            raise ValueError(f"source task changed since reviewed manifest: {tid}")
        if current["current_run_id"] or current["worker_pid"] or current["claim_lock"] or current["status"] == "running":
            raise ValueError(f"source worker has not been reconciled: {tid}")
        retired = source.execute("SELECT target_db,target_task,manifest_hash FROM kanban_task_retirements WHERE task_id=?", (tid,)).fetchone()
        if retired and tuple(retired) != (str(target_path), target_id, fingerprint):
            raise ValueError(f"source already transferred elsewhere: {tid}")


def migrate(conn, source, manifest, *, board=None):
    """Transfer the reviewed source cohort into one held destination card.

    Manifest: transfer_id, issue, issue_keys (including child aliases), title,
    body (including preserved holds), workspace_path, source_tasks (exact rows),
    artifacts (snapshot/hold packet and retained evidence paths), operator_note.
    Caller reconciles real process trees/external actions; DB liveness alone is
    insufficient. This API does not clear any product hold or infer approval.
    """
    wf._operator_only()
    definition, enabled = wf.configuration(conn)
    if enabled or not definition:
        raise ValueError("configure and disable destination admission before migration")
    hold = definition["steps"].get("migration_hold", {})
    if not hold.get("hold") or hold.get("terminal"):
        raise ValueError("definition needs a nonterminal migration_hold")
    manifest = json.loads(json.dumps(manifest))
    key = admission.issue_key(manifest["issue"])
    keys = sorted(set(admission.issue_key(k) for k in manifest["issue_keys"]))
    if key not in keys or keys != manifest["issue_keys"] or not manifest["source_tasks"]:
        raise ValueError("manifest needs canonical sorted issue aliases and source rows")
    if not manifest.get("operator_note") or not manifest.get("artifacts"):
        raise ValueError("record reconciled workers/outcomes and attach preserved evidence")
    if not Path(manifest["workspace_path"]).is_dir():
        raise ValueError("retain an existing feature workspace")
    target_path, source_path = admission.db_path(conn), admission.db_path(source)
    if target_path == source_path or admission.installation_root(conn) != admission.installation_root(source):
        raise ValueError("transfer requires distinct boards in one installation")
    expected_hashes = manifest.get("artifact_sha256", {})
    if set(expected_hashes) != set(manifest["artifacts"]):
        raise ValueError("manifest must pin every source artifact hash")
    for path, sha in expected_hashes.items():
        if hashlib.sha256(Path(path).read_bytes()).hexdigest() != sha:
            raise ValueError("source artifact changed since reviewed manifest")
    fingerprint = wf.digest(manifest)
    staged = []
    with contextlib.ExitStack() as locks:
        for alias in keys:
            locks.enter_context(admission.issue_lock(conn, alias))
        receipt = conn.execute("SELECT * FROM kanban_workflow_migrations WHERE transfer_id=?", (manifest["transfer_id"],)).fetchone()
        if receipt and receipt["manifest_hash"] != fingerprint:
            raise ValueError("transfer_id was reused with a changed manifest")
        tid = receipt["task_id"] if receipt else None
        expected_owners = {(source_path, task_id) for task_id in manifest["source_tasks"]}
        if tid:
            expected_owners.add((target_path, tid))
        for alias in keys:
            if set(admission.owners(conn, alias)) - expected_owners:
                raise ValueError("manifest omits an existing issue owner")
        with kb.write_txn(source):
            _check_sources(source, manifest, target_path, tid, fingerprint)
        if receipt and receipt["finalized"]:
            return tid
        if not receipt:
            try:
                with kb.write_txn(conn):
                    tid = kb._create_task(conn, title=manifest["title"], body=manifest["body"],
                                          workspace_kind="dir", workspace_path=manifest["workspace_path"],
                                          initial_status="blocked", idempotency_key="feature:" + key,
                                          created_by="workflow-migration", board=board)
                    conn.execute("UPDATE tasks SET workflow_template_id=? WHERE id=?", (definition["id"], tid))
                    conn.execute("INSERT INTO kanban_workflow_state(task_id) VALUES (?)", (tid,))
                    wf._route(conn, tid, definition, "migration_hold")
                    evidence = {"artifacts": manifest["artifacts"], "manifest_hash": fingerprint,
                                "source_db": str(source_path), "source_task_ids": sorted(manifest["source_tasks"]),
                                "operator_note": manifest["operator_note"], "issue_keys": keys}
                    wf._preserve_artifacts(conn, kb.get_task(conn, tid), evidence, staged)
                    if list(evidence["artifact_sha256"].values()) != [expected_hashes[p] for p in manifest["artifacts"]]:
                        raise ValueError("artifact changed while copying")
                    conn.execute("UPDATE kanban_workflow_state SET evidence=? WHERE task_id=?",
                                 (json.dumps({"migration": {"digest": wf.digest(evidence), "data": evidence}}), tid))
                    conn.execute("INSERT INTO kanban_workflow_migrations VALUES (?,?,?,?,0)",
                                 (tid, manifest["transfer_id"], fingerprint, json.dumps(manifest)))
                    kb._append_event(conn, tid, "workflow_migration_reserved", {"manifest_hash": fingerprint, "source_db": str(source_path)})
                staged.clear()
            except Exception:
                if staged:
                    kb._discard_staged_copies(staged, staged[0].parent)
                raise
        with kb.write_txn(source):
            _check_sources(source, manifest, target_path, tid, fingerprint)
            for old_id in manifest["source_tasks"]:
                source.execute("INSERT OR IGNORE INTO kanban_task_retirements VALUES (?,?,?,?,?)",
                               (old_id, str(target_path), tid, fingerprint, json.dumps(keys)))
                kb._append_event(source, old_id, "workflow_retired", {"target_db": str(target_path), "target_task": tid, "manifest_hash": fingerprint})
        # Source retirement committed. A crash here leaves the destination held
        # and all source cards fenced; repeating the same manifest finishes it.
        with kb.write_txn(conn):
            task = kb.get_task(conn, tid)
            if task.current_step_key != "migration_hold" or task.status != "blocked":
                raise ValueError("pending destination was changed")
            evidence = wf.state(conn, tid)["evidence"]["migration"]["data"]
            for path, sha in evidence["artifact_sha256"].items():
                if hashlib.sha256(Path(path).read_bytes()).hexdigest() != sha:
                    raise ValueError("preserved migration artifact changed")
            conn.execute("UPDATE kanban_workflow_migrations SET finalized=1 WHERE task_id=?", (tid,))
            kb._append_event(conn, tid, "workflow_migrated", {"manifest_hash": fingerprint, "source_task_ids": sorted(manifest["source_tasks"])})
        return tid
