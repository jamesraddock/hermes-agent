"""Durable, same-card workflows configured by the board operator.

Execution stays in the native dispatcher. A completion closes one run and
advances the card; a human decision is a durable hold, never worker authority.
"""
from __future__ import annotations

import copy
import hashlib
import json
import os
import time
from pathlib import Path

from hermes_cli import kanban_db as kb



def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"),
                                     ensure_ascii=False).encode()).hexdigest()


def configuration(conn):
    row = conn.execute("SELECT definition, enabled FROM kanban_workflow_config WHERE singleton=1").fetchone()
    return (json.loads(row[0]), bool(row[1])) if row else (None, False)


def managed(conn, task):
    if not task:
        return False
    definition, _ = configuration(conn)
    return bool((definition and task.workflow_template_id == definition["id"]) or conn.execute(
        "SELECT 1 FROM kanban_workflow_state WHERE task_id=?", (task.id,)).fetchone())


def validate_definition(value):
    value = copy.deepcopy(value)
    if not isinstance(value, dict) or not isinstance(value.get("id"), str) or not value["id"].strip():
        raise ValueError("workflow id is required")
    steps = value.get("steps")
    if not isinstance(steps, dict) or value.get("start") not in steps:
        raise ValueError("workflow start must name a step")
    for name, step in steps.items():
        if not isinstance(name, str) or not isinstance(step, dict):
            raise ValueError("steps must map names to step definitions")
        if step.get("terminal") and step.get("hold") is not True:
            raise ValueError("terminal stages cannot dispatch a worker")
        if step.get("hold") is True:
            if step.get("profile") or step.get("next") or step.get("changes"):
                raise ValueError("human holds cannot dispatch or advance automatically")
            decisions = step.get("decisions", {})
            if not isinstance(decisions, dict) or any(k not in ("approve", "revise", "complete") or v not in steps for k, v in decisions.items()):
                raise ValueError(f"{name}: invalid operator decision target")
            if "revise" in decisions and steps[decisions["revise"]].get("inputs"):
                raise ValueError(f"{name}: operator revise must return to a stage with no input evidence")
            required = step.get("completion_required", [])
            if not isinstance(required, list) or any(not isinstance(k, str) or not k for k in required):
                raise ValueError(f"{name}: invalid completion evidence contract")
            for source in step.get("completion_matches", {}).values():
                if not isinstance(source, str) or "." not in source or source.split(".", 1)[0] not in steps:
                    raise ValueError(f"{name}: invalid completion evidence reference")
            source = step.get("head_from")
            if source is not None and (not isinstance(source, str) or "." not in source
                                       or source.split(".", 1)[0] not in steps or not source.split(".", 1)[1]):
                raise ValueError(f"{name}: invalid head_from reference")
            continue
        if not all(isinstance(step.get(k), str) and step[k].strip() for k in ("profile", "model", "provider", "effort")):
            raise ValueError(f"{name}: explicit profile/model/provider/effort required")
        kb.normalize_reasoning_effort(step["effort"])
        if step.get("next") not in steps or (step.get("changes") and step["changes"] not in steps):
            raise ValueError(f"{name}: invalid transition target")
        if steps[step["next"]].get("terminal"):
            raise ValueError(f"{name}: workers cannot directly complete the feature")
        for field in ("inputs", "required", "skills"):
            items = step.get(field, [])
            if not isinstance(items, list) or any(not isinstance(x, str) or not x for x in items):
                raise ValueError(f"{name}: {field} must be a string list")
        if any(x not in steps or x == name for x in step.get("inputs", [])):
            raise ValueError(f"{name}: invalid input stage")
        matches = step.get("matches", {})
        if not isinstance(matches, dict) or any(not isinstance(k, str) or not isinstance(v, str)
                or "." not in v or v.split(".", 1)[0] not in step.get("inputs", []) or not v.split(".", 1)[1]
                for k, v in matches.items()):
            raise ValueError(f"{name}: matches must refer to declared input fields")
    if any(s.get("changes") for s in steps.values()):
        escalation = steps.get(value.get("escalation"), {})
        if escalation.get("hold") is not True:
            raise ValueError("revision workflows need an escalation hold")
    repositories = value.get("repositories", {})
    if not isinstance(repositories, dict) or any(not isinstance(repo, dict)
            or not isinstance(repo.get("path"), str) or not repo["path"]
            or not isinstance(repo.get("base"), str) or not repo["base"] for repo in repositories.values()):
        raise ValueError("repositories must map names to explicit path/base pairs")
    if any("git_head" in spec and type(spec["git_head"]) is not bool for spec in steps.values()):
        raise ValueError("git_head must be boolean")
    if not any(s.get("hold") is True for s in steps.values()):
        raise ValueError("workflow needs a human decision hold")
    return value


def _operator_only():
    kb._assert_not_delegated_child_mutation()
    if os.environ.get("HERMES_KANBAN_TASK"):
        raise PermissionError("workflow configuration is an operator action")


def configure(conn, definition):
    _operator_only()
    definition = validate_definition(definition)
    with kb.write_txn(conn):
        old, _ = configuration(conn)
        if not old and conn.execute("SELECT 1 FROM tasks LIMIT 1").fetchone():
            raise ValueError("configure a new empty board; legacy cards require explicit migration")
        if old and old != definition and conn.execute(
            "SELECT 1 FROM tasks WHERE workflow_template_id IS NOT NULL LIMIT 1"
        ).fetchone():
            raise ValueError("existing workflow cards pin their definition; use a new board for a different version")
        conn.execute("INSERT INTO kanban_workflow_config VALUES (1, ?, 0) "
                     "ON CONFLICT(singleton) DO UPDATE SET definition=excluded.definition",
                     (json.dumps(definition),))


def set_enabled(conn, enabled):
    _operator_only()
    with kb.write_txn(conn):
        if not configuration(conn)[0]:
            raise ValueError("configure a workflow first")
        conn.execute("UPDATE kanban_workflow_config SET enabled=? WHERE singleton=1", (int(enabled),))


def set_admission(conn, *, paused=False, retired_assignees=()):
    _operator_only()
    if any(not isinstance(p, str) or not p.strip() for p in retired_assignees):
        raise ValueError("retired assignees must be nonempty profile names")
    with kb.write_txn(conn):
        conn.execute("INSERT INTO kanban_admission_policy VALUES (1, ?, ?) "
                     "ON CONFLICT(singleton) DO UPDATE SET paused=excluded.paused, "
                     "retired_assignees=excluded.retired_assignees",
                     (int(paused), json.dumps(list(retired_assignees))))


def admission(conn, task=None):
    row = conn.execute("SELECT paused, retired_assignees FROM kanban_admission_policy WHERE singleton=1").fetchone()
    if row and row[0]:
        return False
    if row and task and task.assignee in json.loads(row[1]):
        return False
    definition, enabled = configuration(conn)
    if not definition:
        return not managed(conn, task)
    if not enabled:
        return False
    if task is None:
        return True
    if task.workflow_template_id != definition["id"]:
        return False
    step = definition["steps"].get(task.current_step_key)
    return bool(step and not step.get("hold") and task.assignee == step["profile"]
                and task.model_override == step["model"] and task.provider_override == step["provider"]
                and task.reasoning_effort == step["effort"])


def _route(conn, task_id, definition, step_key):
    step = definition["steps"][step_key]
    hold = step.get("hold") is True
    status = "done" if step.get("terminal") else "blocked" if hold else kb._landing_status_after_parents(conn, task_id)
    conn.execute("UPDATE tasks SET current_step_key=?, assignee=?, skills=?, model_override=?, "
                 "provider_override=?, reasoning_effort=?, status=?, claim_lock=NULL, claim_expires=NULL, "
                 "worker_pid=NULL, worker_started_at=NULL, consecutive_failures=0 WHERE id=?",
                 (step_key, step.get("profile"), json.dumps(step.get("skills", [])), step.get("model"),
                  step.get("provider"), step.get("effort"), status, task_id))
    if hold:
        kb._append_event(conn, task_id, "completed" if step.get("terminal") else "blocked", {"reason": step.get("instruction", "Human decision required"),
                                                  "actor": "workflow", "step": step_key})


def start(conn, *, issue, title, body=None, workspace_path=None, board=None,
          parents=(), tenant=None, priority=0, created_by="workflow-intake", session_id=None):
    from hermes_cli.kanban_workflow_admission import issue_key, creation_guard
    key = issue_key(issue)
    definition, _ = configuration(conn)
    if not definition:
        raise ValueError("board has no configured workflow")
    if os.environ.get("HERMES_KANBAN_TASK"):
        raise PermissionError("workers advance their existing feature; intake is an operator action")
    repo = None
    if workspace_path is None:
        repo = definition.get("repositories", {}).get(key.removeprefix("github:").split("#")[0])
        if not isinstance(repo, dict) or not repo.get("path") or not repo.get("base"):
            raise ValueError("configure this repository path/base or supply a retained workspace")
        workspace_path = repo["path"]
    if not Path(workspace_path).is_dir():
        raise ValueError("workspace must be an existing retained directory/worktree")
    base = kb._git_out(Path(workspace_path), "rev-parse", "--verify", repo["base"] + "^{commit}") if repo else None
    if repo and not base:
        raise ValueError("configured repository base is unavailable; fetch it before intake")
    with creation_guard(conn, "feature:" + key) as existing:
        if existing:
            task = kb.get_task(conn, existing)
            if task.workflow_template_id != definition["id"]:
                raise ValueError("issue already belongs to a legacy task")
            return existing
        branch = "feature/" + key.split("#")[1] + "-" + kb._new_task_id() if repo else None
        if repo:
            from hermes_cli.kanban_db_workspace import _git
            result = _git(Path(workspace_path), "branch", branch, base, timeout=30)
            if result.returncode:
                raise ValueError("could not prepare feature branch at the configured base")
        try:
            with kb.write_txn(conn):
                if configuration(conn)[0] != definition:
                    raise ValueError("workflow definition changed during intake; retry")
                task_id = kb._create_task(conn, title=title, body=body, workspace_kind="worktree" if repo else "dir",
                                          workspace_path=str(Path(workspace_path).resolve()), branch_name=branch,
                                          idempotency_key="feature:" + key, initial_status="blocked", board=board,
                                          created_by=created_by, parents=parents, tenant=tenant,
                                          priority=priority, session_id=session_id)
                conn.execute("UPDATE tasks SET workflow_template_id=? WHERE id=?", (definition["id"], task_id))
                conn.execute("INSERT INTO kanban_workflow_state(task_id) VALUES (?)", (task_id,))
                _route(conn, task_id, definition, definition["start"])
                kb._append_event(conn, task_id, "workflow_started", {"issue": key, "definition_digest": digest(definition)})
                return task_id
        except Exception:
            if repo:
                _git(Path(workspace_path), "branch", "-D", branch, timeout=30)
            raise


def state(conn, task_id):
    row = conn.execute("SELECT * FROM kanban_workflow_state WHERE task_id=?", (task_id,)).fetchone()
    if not row:
        raise ValueError("task has no native workflow state")
    return {"revision": row["revision"], "evidence": json.loads(row["evidence"]),
            "corrections": json.loads(row["corrections"])}



def packet(conn, task_id):
    current = state(conn, task_id)
    task = kb.get_task(conn, task_id)
    step = configuration(conn)[0]["steps"][task.current_step_key]
    decisions = list(step.get("decisions", {})) if step.get("hold") else ["retry", "revise"]
    return {**current, "packet_digest": digest(current["evidence"]), "step": task.current_step_key,
            "status": task.status, "instruction": step.get("instruction", ""), "decisions": decisions}

def _preserve_artifacts(conn, task, metadata, staged):
    from hermes_cli.kanban_workflow_admission import db_path, installation_root
    paths = metadata.get("artifacts", [])
    if not isinstance(paths, list) or any(not isinstance(p, str) or not p for p in paths):
        raise kb.ArtifactPreservationError("artifacts must be a list of file paths")
    database = db_path(conn)
    root = (database.parent / "attachments" if database.parent.parent.name == "boards"
            else installation_root(conn) / "kanban" / "attachments") / task.id
    persisted = []
    hashes = {}
    for value in paths:
        source = Path(value).expanduser()
        if not source.is_absolute():
            source = Path(task.workspace_path) / source
        if not source.is_file():
            raise kb.ArtifactPreservationError(f"declared artifact is unavailable: {value}")
        root.mkdir(parents=True, exist_ok=True)
        target = kb._unique_attachment_path(root, kb._safe_attachment_name(source.name), set(staged))
        staged.append(target)
        kb._copy_capped(source, target, value)
        hashes[str(target)] = hashlib.sha256(target.read_bytes()).hexdigest()
        kb._insert_completion_attachment(conn, task.id, filename=target.name, stored_path=str(target),
                                         size=target.stat().st_size, created_at=int(time.time()),
                                         uploaded_by="workflow_advance")
        persisted.append(str(target))
    if paths:
        metadata["artifacts"] = persisted
        metadata["artifact_sha256"] = hashes



def _assert_workspace_head(task, head):
    from hermes_cli.kanban_db_workspace import _git
    if not head or head != kb._git_out(Path(task.workspace_path), "rev-parse", "HEAD"):
        raise ValueError("evidence head is not the current workspace HEAD")
    if _git(Path(task.workspace_path), "diff", "--quiet", "HEAD", "--", timeout=30).returncode:
        raise ValueError("tracked workspace changes are not part of the reviewed HEAD")

def advance(conn, task_id, *, expected_run_id, expected_step, revision, decision,
            evidence, inputs, summary, transition_id, worker_session_id=None):
    """Commit a worker's stage result, guarded by its native run and revision."""
    if not isinstance(transition_id, str) or not transition_id.strip():
        raise ValueError("transition_id is required")
    if decision not in ("pass", "changes") or not isinstance(evidence, dict) or not isinstance(inputs, dict):
        raise ValueError("decision must be pass/changes with evidence and inputs objects")
    if not isinstance(summary, str) or not summary.strip():
        raise ValueError("a handoff summary is required")
    request = {"run": expected_run_id, "step": expected_step, "revision": revision,
               "decision": decision, "evidence": evidence, "inputs": inputs, "summary": summary}
    request_hash = digest(request)
    staged = []
    try:
        with kb.write_txn(conn):
            previous = conn.execute("SELECT payload FROM task_events WHERE task_id=? AND kind='workflow_advanced' "
                                    "ORDER BY id DESC", (task_id,)).fetchall()
            for row in previous:
                event = json.loads(row[0])
                if event.get("transition_id") == transition_id:
                    if event.get("request_hash") != request_hash:
                        raise ValueError("transition_id was already used with a different result")
                    return event
            task = kb.get_task(conn, task_id)
            definition, _ = configuration(conn)
            if not task or not definition or task.workflow_template_id != definition["id"]:
                raise ValueError("task does not belong to this board workflow")
            current = state(conn, task_id)
            if (type(expected_run_id) is not int or type(revision) is not int
                    or task.current_run_id != expected_run_id or task.current_step_key != expected_step
                    or current["revision"] != revision or task.status != "running" or not task.claim_lock):
                raise ValueError("stale workflow stage/run/revision")
            run = kb.get_run(conn, expected_run_id)
            step = definition["steps"][expected_step]
            if (step.get("hold") or run.ended_at is not None or run.profile != step["profile"]
                    or run.step_key != expected_step or task.assignee != run.profile
                    or task.model_override != step["model"] or task.provider_override != step["provider"]
                    or task.reasoning_effort != step["effort"] or not kb._parents_satisfied(conn, task_id)):
                raise ValueError("run ownership, role, or dependency changed")
            expected_inputs = {s: current["evidence"][s]["digest"] for s in step.get("inputs", [])
                               if s in current["evidence"]}
            if len(expected_inputs) != len(step.get("inputs", [])) or inputs != expected_inputs:
                raise ValueError("input evidence does not match current upstream stage artifacts")
            if decision == "pass" and any(k not in evidence or evidence[k] is None
                                          for k in step.get("required", [])):
                raise ValueError("stage evidence is missing required fields")
            if decision == "pass":
                for field, source in step.get("matches", {}).items():
                    upstream, upstream_field = source.split(".", 1)
                    if evidence.get(field) != current["evidence"][upstream]["data"].get(upstream_field):
                        raise ValueError(f"{field} does not match the current {upstream} artifact")
                if step.get("git_head"):
                    _assert_workspace_head(task, evidence.get("head"))
            target = step.get("next" if decision == "pass" else "changes")
            if not target:
                raise ValueError("stage does not permit this decision")
            metadata = copy.deepcopy(evidence)
            _preserve_artifacts(conn, task, metadata, staged)
            if decision == "changes":
                if not evidence.get("findings"):
                    raise ValueError("changes requires actionable findings")
                findings = evidence["findings"]
                if not isinstance(findings, list) or any(not isinstance(f, dict) or not f.get("id")
                                                        or not f.get("summary") for f in findings):
                    raise ValueError("findings need stable id and actionable summary")
                counts = []
                for finding_id in {str(f["id"]) for f in findings}:
                    key = expected_step + ":" + finding_id
                    current["corrections"][key] = current["corrections"].get(key, 0) + 1
                    counts.append(current["corrections"][key])
                if max(counts) >= 3:
                    target = definition.get("escalation")
                    if target not in definition["steps"] or not definition["steps"][target].get("hold"):
                        raise ValueError("workflow needs a human escalation hold after two failed corrections")
                # Invalidate evidence dependent on the stage being revised.
                invalid = {step["changes"], expected_step}
                while True:
                    more = {s for s, spec in definition["steps"].items() if set(spec.get("inputs", [])) & invalid}
                    if more <= invalid:
                        break
                    invalid |= more
                for name in invalid:
                    current["evidence"].pop(name, None)
            else:
                current["evidence"][expected_step] = {"digest": digest(metadata), "data": metadata,
                                                      "run_id": expected_run_id, "profile": run.profile,
                                                      "worker_session_id": worker_session_id, "inputs": inputs}
            kb._end_run(conn, task_id, outcome="stage_passed" if decision == "pass" else "changes_requested",
                        status="done", summary=summary, metadata=metadata)
            _route(conn, task_id, definition, target)
            conn.execute("UPDATE kanban_workflow_state SET revision=revision+1, evidence=?, corrections=? WHERE task_id=?",
                         (json.dumps(current["evidence"]), json.dumps(current["corrections"]), task_id))
            event = {"transition_id": transition_id, "request_hash": request_hash, "from": expected_step,
                     "to": target, "decision": decision, "revision": revision+1, "summary": summary,
                     "evidence_digest": digest(metadata), "run_id": expected_run_id}
            kb._append_event(conn, task_id, "workflow_advanced", event, run_id=expected_run_id)
            return event
    except Exception:
        if staged:
            kb._discard_staged_copies(staged, staged[0].parent)
        raise


def decide(conn, task_id, *, expected_revision, packet_digest, decision, note, decision_id, evidence=None):
    """Local operator decision; records intent, never executes a release.

    This is the same trusted-host operator boundary as native board management.
    It is not a cryptographic James identity or a fence against arbitrary shell
    access as the same OS user. External approvals need an authenticated adapter.
    """
    _operator_only()
    if not isinstance(note, str) or not note.strip():
        raise ValueError("record the operator decision and its source")
    if not isinstance(decision_id, str) or not decision_id.strip():
        raise ValueError("decision_id is required")
    request_hash = digest({"revision": expected_revision, "packet": packet_digest, "decision": decision, "note": note, "evidence": evidence})
    staged = []
    try:
        with kb.write_txn(conn):
            for row in conn.execute("SELECT payload FROM task_events WHERE task_id=? AND kind='workflow_operator_decision'", (task_id,)):
                prior = json.loads(row[0])
                if prior.get("decision_id") == decision_id:
                    if prior.get("request_hash") != request_hash:
                        raise ValueError("decision_id was reused with a different decision")
                    return prior
            task = kb.get_task(conn, task_id)
            definition, _ = configuration(conn)
            current = state(conn, task_id)
            if (not task or not definition or task.workflow_template_id != definition["id"]
                    or task.status not in ("blocked", "triage", "ready", "todo", "done") or task.current_run_id is not None
                    or type(expected_revision) is not int or current["revision"] != expected_revision
                    or digest(current["evidence"]) != packet_digest):
                raise ValueError("decision packet changed; read the current card before deciding")
            step = definition["steps"][task.current_step_key]
            target = step.get("decisions", {}).get(decision)
            if not step.get("hold"):
                # Native crash/timeout/retry exhaustion can block a worker stage.
                # Operator recovery keeps the card and evidence; the same CAS applies.
                target = task.current_step_key if decision == "retry" else definition["start"] if decision == "revise" else None
            if target not in definition["steps"]:
                raise ValueError("this hold does not permit that operator decision")
            if definition["steps"][target].get("terminal"):
                if decision != "complete" or not isinstance(evidence, dict) or not kb._parents_satisfied(conn, task_id):
                    raise ValueError("completion requires explicit operator release evidence and satisfied dependencies")
                if any(not evidence.get(k) for k in step.get("completion_required", [])):
                    raise ValueError("completion evidence is missing required release/verification fields")
                for field, reference in step.get("completion_matches", {}).items():
                    upstream, upstream_field = reference.split(".", 1)
                    if evidence.get(field) != current["evidence"].get(upstream, {}).get("data", {}).get(upstream_field):
                        raise ValueError("release evidence does not match the reviewed packet")
                metadata = copy.deepcopy(evidence)
                _preserve_artifacts(conn, task, metadata, staged)
                current["evidence"][task.current_step_key] = {"digest": digest(metadata), "data": metadata,
                                                             "actor_surface": "local-operator-cli"}
                conn.execute("UPDATE kanban_workflow_state SET evidence=? WHERE task_id=?",
                             (json.dumps(current["evidence"]), task_id))
            source = step.get("head_from")
            if decision == "approve" and source:
                source_stage, source_field = source.split(".", 1)
                head = current["evidence"].get(source_stage, {}).get("data", {}).get(source_field)
                _assert_workspace_head(task, head)
            if decision == "revise":
                current["evidence"] = {}
                conn.execute("UPDATE kanban_workflow_state SET evidence='{}', corrections='{}' WHERE task_id=?", (task_id,))
            _route(conn, task_id, definition, target)
            conn.execute("UPDATE kanban_workflow_state SET revision=revision+1 WHERE task_id=?", (task_id,))
            receipt = {"decision_id": decision_id, "request_hash": request_hash,
                       "decision": decision, "packet_digest": packet_digest, "note": note,
                       "recorded_at": int(time.time()),
                       "actor_surface": "local-operator-cli", "uid": os.getuid() if hasattr(os, "getuid") else None,
                       "from": task.current_step_key, "to": target, "revision": expected_revision+1,
                       "release_executed": False}
            kb._append_event(conn, task_id, "workflow_operator_decision", receipt)
            return receipt
    except Exception:
        if staged:
            kb._discard_staged_copies(staged, staged[0].parent)
        raise


def context(conn, task):
    if not managed(conn, task):
        return ""
    definition, _ = configuration(conn)
    current = state(conn, task.id)
    step = definition["steps"][task.current_step_key]
    return ("\n## Durable workflow\n" + json.dumps({"workflow": task.workflow_template_id,
            "step": task.current_step_key, "instruction": step.get("instruction", ""),
            "required_evidence": step.get("required", []),
            "expected_inputs": {name: current["evidence"][name]["digest"] for name in step.get("inputs", [])
                                if name in current["evidence"]},
            "matches": step.get("matches", {}), "git_head_required": step.get("git_head", False),
            "allowed_decisions": ["pass", "changes"] if step.get("changes") else ["pass"],
            "packet_digest": digest(current["evidence"]), **current}, indent=2)
            + "\nComplete this stage using kanban_complete metadata.workflow: "
              "{step, revision, decision: pass|changes, transition_id, inputs: {stage: digest}, evidence: {...}}. "
              "Put file paths in evidence.artifacts; the controller copies and hashes them during completion. "
              "Do not base64-encode files or call kanban_attach for this handoff. "
              "This advances the SAME card. Never create phase children, change the assignee, "
              "use request_review/request_changes, merge, deploy, or close the source issue. "
              "Human decision stages remain blocked.\n")
