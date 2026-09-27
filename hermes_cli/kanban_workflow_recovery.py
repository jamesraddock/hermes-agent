"""Audited, operator-only continuation of idle same-card workflows."""
from __future__ import annotations

import json
import time

from hermes_cli import kanban_db as kb, kanban_workflow as wf


def _invalidated(definition, target):
    invalid = {target}
    while True:
        more = {name for name, step in definition['steps'].items()
                if set(step.get('inputs', [])) & invalid}
        if more <= invalid:
            return invalid
        invalid |= more


def _compatible(old, new):
    if {k: v for k, v in old.items() if k != 'steps'} != {k: v for k, v in new.items() if k != 'steps'}:
        raise ValueError('upgrade must preserve workflow identity, start, escalation and repositories')
    added = set(new['steps']) - set(old['steps'])
    if any(new['steps'][name].get('hold') for name in added):
        raise ValueError('upgrade may insert worker stages, not new approval or terminal stages')
    mutable = {'instruction', 'next', 'resume_steps', 'approval_inputs', 'resolve_findings'}
    for name, before in old['steps'].items():
        after = new['steps'].get(name)
        if after is None or {k: v for k, v in before.items() if k not in mutable} != {
                k: v for k, v in after.items() if k not in mutable}:
            raise ValueError(f'{name}: upgrade must preserve roles, evidence contracts and release decisions')
        if not set(before.get('approval_inputs', [])) <= set(after.get('approval_inputs', [])):
            raise ValueError('upgrade cannot remove approval prerequisites')
        if before.get('resolve_findings') and not after.get('resolve_findings'):
            raise ValueError('upgrade cannot remove correction resolution requirements')
        target = after.get('next')
        seen = set()
        while target != before.get('next'):
            if target not in added or target in seen:
                raise ValueError('upgrade may only insert worker stages before the existing next stage')
            seen.add(target)
            target = new['steps'][target]['next']


def upgrade(conn, definition, *, expected_digest, note):
    """Insert preparation work while keeping existing approvals and release boundaries."""
    wf._operator_only()
    definition = wf.validate_definition(definition)
    if not isinstance(note, str) or not note.strip():
        raise ValueError('record the upgrade reason and authority')
    with kb.write_txn(conn):
        old, enabled = wf.configuration(conn)
        if not old or wf.digest(old) != expected_digest:
            raise ValueError('workflow definition changed; inspect it before upgrading')
        if enabled or conn.execute("SELECT 1 FROM tasks WHERE current_run_id IS NOT NULL OR status='running' LIMIT 1").fetchone():
            raise ValueError('disable dispatch and drain all board workers before upgrading')
        if conn.execute('SELECT 1 FROM kanban_workflow_migrations WHERE finalized=0 LIMIT 1').fetchone():
            raise ValueError('finish pending ownership transfers before upgrading')
        _compatible(old, definition)
        tasks = conn.execute('SELECT id, current_step_key FROM tasks WHERE workflow_template_id=? AND status!=?',
                             (old['id'], 'archived')).fetchall()
        receipt = {'previous_digest': expected_digest, 'definition_digest': wf.digest(definition),
                   'note': note, 'recorded_at': int(time.time()), 'actor_surface': 'local-operator-cli',
                   'previous_definition': old, 'release_executed': False}
        for task in tasks:
            if task['current_step_key'] not in definition['steps']:
                raise ValueError('upgrade would strand an existing card')
            kb._append_event(conn, task['id'], 'workflow_upgraded', receipt)
            # A definition change invalidates a previously-read operator decision packet.
            conn.execute('UPDATE kanban_workflow_state SET revision=revision+1 WHERE task_id=?', (task['id'],))
        conn.execute('UPDATE kanban_workflow_config SET definition=? WHERE singleton=1', (json.dumps(definition),))
        return {k: v for k, v in receipt.items() if k != 'previous_definition'}


def resume(conn, task_id, *, target, expected_revision, packet_digest, note, decision_id):
    """Resume permitted work, never approve a release or manufacture a passing stage."""
    wf._operator_only()
    if not all(isinstance(v, str) and v.strip() for v in (note, decision_id, target)):
        raise ValueError('target, decision_id and an explicit recovery reason are required')
    request_hash = wf.digest({'target': target, 'revision': expected_revision,
                              'packet': packet_digest, 'note': note})
    with kb.write_txn(conn):
        for row in conn.execute("SELECT payload FROM task_events WHERE task_id=? AND kind='workflow_resumed'", (task_id,)):
            prior = json.loads(row[0])
            if prior['decision_id'] == decision_id:
                if prior['request_hash'] != request_hash:
                    raise ValueError('decision_id reused with a different recovery')
                return prior
        task = kb.get_task(conn, task_id)
        definition, _ = wf.configuration(conn)
        current = wf.state(conn, task_id)
        if (not task or not definition or task.workflow_template_id != definition['id']
                or task.status not in ('blocked', 'ready', 'todo', 'triage') or task.current_run_id is not None
                or type(expected_revision) is not int or current['revision'] != expected_revision
                or wf.digest(current['evidence']) != packet_digest):
            raise ValueError('recovery packet changed; inspect the current card')
        if conn.execute('SELECT 1 FROM kanban_workflow_migrations WHERE task_id=? AND finalized=0', (task_id,)).fetchone():
            raise ValueError('finish the pending ownership transfer before resuming')
        source = definition['steps'][task.current_step_key]
        step = definition['steps'].get(target, {})
        if target not in source.get('resume_steps', []) or step.get('hold') or not step.get('profile'):
            raise ValueError('this stage does not permit that recovery target')
        invalid = _invalidated(definition, target)
        for name in step.get('inputs', []):
            artifact = current['evidence'].get(name)
            if name in invalid or not artifact or any(current['evidence'].get(s, {}).get('digest') != d
                                                      for s, d in artifact.get('inputs', {}).items()):
                raise ValueError(f'recovery requires current {name} evidence')
        if step.get('git_head'):
            for field, ref in step.get('matches', {}).items():
                if field == 'head':
                    name, key = ref.split('.', 1)
                    wf._assert_workspace_head(task, current['evidence'][name]['data'].get(key))
        removed = {k: v for k, v in current['evidence'].items() if k in invalid}
        reset = {k: v for k, v in current['corrections'].items() if k.split(':', 1)[0] in invalid}
        evidence = {k: v for k, v in current['evidence'].items() if k not in invalid}
        corrections = {k: v for k, v in current['corrections'].items() if k not in reset}
        receipt = {'decision_id': decision_id, 'request_hash': request_hash, 'from': task.current_step_key,
                   'to': target, 'revision': expected_revision + 1, 'note': note, 'recorded_at': int(time.time()),
                   'actor_surface': 'local-operator-cli', 'invalidated_evidence': removed,
                   'previous_corrections': reset, 'release_executed': False}
        conn.execute('UPDATE kanban_workflow_state SET revision=revision+1, evidence=?, corrections=? WHERE task_id=?',
                     (json.dumps(evidence), json.dumps(corrections), task_id))
        wf._route(conn, task_id, definition, target)
        kb._append_event(conn, task_id, 'workflow_resumed', receipt)
        return receipt
