"""Same feature survives revisions, restarts and competing board intake."""
import concurrent.futures
import copy
import json
from pathlib import Path

import pytest
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_dispatch as kbd
from hermes_cli import kanban_workflow as wf


def definition():
    def step(profile, next, inputs=(), changes=None):
        return {"profile": profile, "model": "test-model", "provider": "test-provider", "effort": "high",
                "next": next, "inputs": list(inputs), "changes": changes, "required": ["artifact"]}
    return {"id": "test-feature-v1", "start": "plan", "escalation": "decision",
            "steps": {"plan": step("planner", "critique"),
                      "critique": step("critic", "implement", ["plan"], "plan"),
                      "implement": step("implementer", "verify", ["plan", "critique"]),
                      "verify": step("verifier", "review", ["implement"], "implement"),
                      "review": step("reviewer", "merge", ["implement", "verify"], "implement"),
                      "merge": {"hold": True}, "decision": {"hold": True}}}


@pytest.fixture
def board(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    for key in ("HERMES_KANBAN_DB", "HERMES_KANBAN_BOARD", "HERMES_KANBAN_TASK", "HERMES_KANBAN_ROOT"):
        monkeypatch.delenv(key, raising=False)
    kb.create_board("delivery")
    conn = kbc.connect(board="delivery")
    wf.configure(conn, definition())
    workspace = tmp_path / "retained"
    workspace.mkdir()
    yield home, conn, workspace
    conn.close()


def handoff(conn, task, decision="pass", artifact="revision-1"):
    current = wf.state(conn, task.id)
    spec = wf.configuration(conn)[0]["steps"][task.current_step_key]
    return {"step": task.current_step_key, "revision": current["revision"], "decision": decision,
            "transition_id": f"run-{task.current_run_id}",
            "inputs": {s: current["evidence"][s]["digest"] for s in spec.get("inputs", [])},
            "evidence": {"artifact": artifact, **({"findings": [{"id": "edge-case", "summary": "Fix edge case"}]} if decision == "changes" else {})}}


def finish(conn, task, data=None):
    return kb.complete_task(conn, task.id, expected_run_id=task.current_run_id,
                            summary="Durable stage result", metadata={"workflow": data or handoff(conn, task)})


def test_same_card_revisions_restart_and_merge_hold(board):
    home, conn, workspace = board
    task_id = wf.start(conn, issue="Example/Repo#42", title="Small feature", workspace_path=workspace, board="delivery")
    assert kb.claim_task(conn, task_id) is None
    assert kbd.dispatch_once(conn, board="delivery").skipped_admission
    wf.set_enabled(conn, True)
    plan = kb.claim_task(conn, task_id)
    with pytest.raises(ValueError, match="metadata.workflow"):
        kb.complete_task(conn, task_id, expected_run_id=plan.current_run_id, summary="Premature done")
    finish(conn, plan)
    critic = kb.claim_task(conn, task_id)
    finish(conn, critic, handoff(conn, critic, "changes"))
    assert not wf.state(conn, task_id)["evidence"]
    with pytest.raises(ValueError, match="stale"):
        finish(conn, plan, {**handoff(conn, kb.get_task(conn, task_id)), "transition_id": "stale"})
    # A new connection resumes solely from the card and native evidence.
    with kbc.connect_closing(board="delivery") as resumed:
        for expected in ("plan", "critique", "implement", "verify", "review"):
            task = kb.claim_task(resumed, task_id)
            assert task.current_step_key == expected
            data = handoff(resumed, task, artifact="revised")
            finish(resumed, task, data)
            # Exact retry is idempotent even after this run has closed.
            finish(resumed, task, data)
        final = kb.get_task(resumed, task_id)
        assert (final.current_step_key, final.status, final.assignee) == ("merge", "blocked", None)
        assert kb.claim_task(resumed, task_id) is None
        assert workspace.is_dir() and final.workspace_path == str(workspace)
        assert len(kb.list_tasks(resumed)) == 1
        assert [r.step_key for r in kb.list_runs(resumed, task_id)]
        assert "revised" in kb.build_worker_context(resumed, task_id)


def test_wrong_inputs_stale_runs_and_artifact_rollback(board):
    _, conn, workspace = board
    task_id = wf.start(conn, issue="Example/Repo#43", title="Feature", workspace_path=workspace)
    wf.set_enabled(conn, True)
    plan = kb.claim_task(conn, task_id)
    data = handoff(conn, plan)
    data["evidence"]["artifacts"] = [str(workspace / "missing.md")]
    before = len(kb.list_events(conn, task_id))
    with pytest.raises(kb.ArtifactPreservationError):
        finish(conn, plan, data)
    assert kb.get_task(conn, task_id).current_run_id == plan.current_run_id
    assert len(kb.list_events(conn, task_id)) == before
    finish(conn, plan)
    critic = kb.claim_task(conn, task_id)
    bad = handoff(conn, critic)
    bad["inputs"]["plan"] = "stale digest"
    with pytest.raises(ValueError, match="input evidence"):
        finish(conn, critic, bad)
    with pytest.raises(ValueError, match="configured workflow"):
        kb.request_review(conn, task_id, expected_run_id=critic.current_run_id)
    assert kb.get_task(conn, task_id).current_run_id == critic.current_run_id


def test_cross_board_racing_intake_and_legacy_retirement(board):
    _, conn, workspace = board
    kb.create_board("other")
    with kbc.connect_closing(board="other") as c:
        wf.configure(c, definition())
    def admit(slug):
        with kbc.connect_closing(board=slug) as c:
            try:
                return wf.start(c, issue="Example/Repo#44", title="Feature", workspace_path=workspace, board=slug)
            except ValueError as exc:
                return str(exc)
    with concurrent.futures.ThreadPoolExecutor(2) as pool:
        outcomes = list(pool.map(admit, ["delivery", "other"]))
    assert sum(x.startswith("t_") for x in outcomes) == 1
    assert any("another execution owner" in x for x in outcomes)
    kb.create_board("legacy")
    with kbc.connect_closing(board="legacy") as legacy:
        old = kb.create_task(legacy, title="Old feature", assignee="old-feature-planner",
                             idempotency_key="promote:github:example/repo#45")
        with pytest.raises(ValueError, match="another execution owner"):
            wf.start(conn, issue="example/repo#45", title="Duplicate", workspace_path=workspace)
        wf.set_admission(legacy, retired_assignees=["old-feature-planner"])
        assert kb.claim_task(legacy, old) is None
        with pytest.raises(ValueError, match="retired"):
            kb.create_task(legacy, title="Respawn", assignee="old-feature-planner")
        normal = kb.create_task(legacy, title="Unrelated", assignee="maintenance")
        assert kb.claim_task(legacy, normal) is not None


def test_default_board_ownership_legacy_templates_and_archived_history(board):
    _, conn, workspace = board
    with kbc.connect_closing(board="default") as legacy:
        tid = kb.create_task(legacy, title="Legacy", idempotency_key="github:example/repo#50:plan", assignee="planner")
        legacy.execute("UPDATE tasks SET workflow_template_id='old-template' WHERE id=?", (tid,))
        claimed = kb.claim_task(legacy, tid)
        assert claimed is not None
        assert kb.complete_task(legacy, tid, expected_run_id=claimed.current_run_id, summary="Legacy completion")
        kb.archive_task(legacy, tid)
        with pytest.raises(ValueError, match="another execution owner"):
            wf.start(conn, issue="Example/Repo#50", title="Unapproved takeover", workspace_path=workspace)


def test_operator_decision_cas_and_correction_budget(board, monkeypatch):
    _, conn, workspace = board
    spec = definition()
    spec["steps"]["decision"]["decisions"] = {"revise": "plan"}
    wf.configure(conn, spec)
    tid = wf.start(conn, issue="example/repo#51", title="Feature", workspace_path=workspace)
    wf.set_enabled(conn, True)
    for attempt in range(3):
        plan = kb.claim_task(conn, tid)
        finish(conn, plan)
        critic = kb.claim_task(conn, tid)
        finish(conn, critic, handoff(conn, critic, "changes"))
    task = kb.get_task(conn, tid)
    assert (task.status, task.current_step_key) == ("blocked", "decision")
    current = wf.state(conn, tid)
    kwargs = dict(expected_revision=current["revision"], packet_digest=wf.digest(current["evidence"]),
                  decision="revise", note="Local trial operator clarifies scope", decision_id="decision-1")
    monkeypatch.setenv("HERMES_KANBAN_TASK", tid)
    with pytest.raises(PermissionError):
        wf.decide(conn, tid, **kwargs)
    monkeypatch.delenv("HERMES_KANBAN_TASK")
    result = wf.decide(conn, tid, **kwargs)
    assert result["to"] == "plan" and not result["release_executed"]
    assert wf.decide(conn, tid, **kwargs) == result
    with pytest.raises(ValueError, match="packet changed"):
        wf.decide(conn, tid, **{**kwargs, "decision_id": "decision-2"})
    assert len(kb.list_tasks(conn)) == 1


def test_cli_tool_dispatch_and_decomposition_share_native_policy(board, monkeypatch):
    from hermes_cli import kanban as cli
    from gateway.kanban_watchers_dispatcher import _KanbanDispatcher, _resolve_dispatcher_settings
    from tools.registry import registry
    import tools.kanban_tools
    _, conn, workspace = board
    result = cli.run_slash(f'--board delivery workflow start --issue example/repo#52 --title Trial --workspace {workspace}')
    tid = json.loads(result)["result"]
    # Embedded and CLI dispatch both keep an explicitly disabled board idle.
    dispatcher = _KanbanDispatcher(kb, _resolve_dispatcher_settings({}, kb))
    assert dispatcher.tick_once_for_board("delivery").skipped_admission
    assert not kbd.has_spawnable_ready(conn)
    # The real auto-decomposer would read triage cards; no call is allowed here.
    from hermes_cli import kanban_decompose
    monkeypatch.setattr(kanban_decompose, "list_triage_ids", lambda: pytest.fail("workflow board reached legacy decomposition"))
    monkeypatch.setattr(dispatcher, "_board_slugs", lambda: ["delivery"])
    assert dispatcher.auto_decompose_tick(1) == 0
    wf.set_enabled(conn, True)
    assert dispatcher.auto_decompose_tick(1) == 0
    task = kb.claim_task(conn, tid)
    monkeypatch.setenv("HERMES_KANBAN_TASK", tid)
    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", str(task.current_run_id))
    monkeypatch.setenv("HERMES_KANBAN_DB", str(kb.kanban_db_path("delivery")))
    data = handoff(conn, task)
    # Real registry handler, not a direct database call.
    result = json.loads(registry.get_entry("kanban_complete").handler({"task_id": tid, "summary": "Stage complete",
                                                               "metadata": {"workflow": data}}))
    assert result["ok"] and result["current_step_key"] == "critique" and result["status"] != "done"
    assert len(kb.list_tasks(conn)) == 1


def test_operator_recovers_worker_failure_and_show_exposes_packet(board):
    from hermes_cli import kanban as cli
    _, conn, workspace = board
    tid = wf.start(conn, issue="example/repo#53", title="Recovery", workspace_path=workspace)
    wf.set_enabled(conn, True)
    task = kb.claim_task(conn, tid)
    kb.block_task(conn, tid, reason="Worker exhausted retries", expected_run_id=task.current_run_id)
    packet = json.loads(cli.run_slash(f"--board delivery workflow show {tid}"))["result"]
    assert packet["packet_digest"] == wf.digest(packet["evidence"])
    command = (f"--board delivery workflow decide {tid} --revision {packet['revision']} "
               f"--packet-digest {packet['packet_digest']} --decision retry --decision-id recovery-1 "
               "--note 'Provider recovered'")
    first = json.loads(cli.run_slash(command))
    assert first["ok"] and first["result"]["to"] == "plan"
    assert json.loads(cli.run_slash(command)) == first
    assert kb.claim_task(conn, tid).current_step_key == "plan"


def test_configure_does_not_convert_legacy_cards_and_validates_references(board):
    _, conn, _ = board
    kb.create_board("occupied")
    with kbc.connect_closing(board="occupied") as old:
        tid = kb.create_task(old, title="Unrelated legacy work")
        with pytest.raises(ValueError, match="empty board"):
            wf.configure(old, definition())
        assert kb.claim_task(old, tid)
    invalid = definition()
    invalid["steps"]["review"]["matches"] = {"head": "plan.head"}
    with pytest.raises(ValueError, match="declared input"):
        wf.configure(conn, invalid)


def test_export_preserves_workflow_state_but_disables_execution(board, tmp_path):
    from hermes_cli import kanban_transfer
    _, conn, workspace = board
    tid = wf.start(conn, issue="example/repo#54", title="Portable", workspace_path=workspace)
    wf.set_enabled(conn, True)
    finish(conn, kb.claim_task(conn, tid))
    snapshot = tmp_path / "snapshot.db"
    kanban_transfer._snapshot_db(kb.kanban_db_path("delivery"), snapshot)
    import sqlite3
    with sqlite3.connect(snapshot) as saved:
        saved.row_factory = sqlite3.Row
        kanban_transfer._scrub_local_state(saved)
        assert wf.state(saved, tid) == wf.state(conn, tid)
        assert not wf.configuration(saved)[1]
    assert wf.configuration(conn)[1]


@pytest.mark.parametrize("fail_after_commit", [False, True])
def test_release_evidence_finishes_same_card_and_is_idempotent(board, monkeypatch, fail_after_commit):
    _, conn, workspace = board
    spec = definition()
    spec["steps"]["merge"]["decisions"] = {"approve": "release"}
    spec["steps"]["release"] = {"hold": True, "decisions": {"complete": "done"},
                                "completion_required": ["source", "release", "verification", "artifacts"],
                                "completion_matches": {"source": "implement.artifact"}}
    spec["steps"]["done"] = {"hold": True, "terminal": True, "decisions": {"revise": "plan"}}
    wf.configure(conn, spec)
    tid = wf.start(conn, issue="example/repo#55", title="Finish", workspace_path=workspace)
    child = wf.start(conn, issue="example/repo#57", title="Dependent feature", workspace_path=workspace, parents=[tid])
    assert kb.get_task(conn, child).status == "todo"
    wf.set_enabled(conn, True)
    for _ in range(5):
        finish(conn, kb.claim_task(conn, tid))
    packet = wf.state(conn, tid)
    wf.decide(conn, tid, expected_revision=packet["revision"], packet_digest=wf.digest(packet["evidence"]),
              decision="approve", decision_id="approve-1", note="Authorized fixture release")
    assert kb.get_task(conn, tid).status == "blocked"
    packet = wf.state(conn, tid)
    args = dict(expected_revision=packet["revision"], packet_digest=wf.digest(packet["evidence"]),
                decision="complete", decision_id="complete-1", note="Fixture release verified")
    with pytest.raises(ValueError, match="explicit operator release evidence"):
        wf.decide(conn, tid, **args)
    artifact = workspace / "release.txt"
    artifact.write_text("Observed local fixture release and acceptance checks passed")
    evidence = {"source": "wrong", "release": "fixture", "verification": "passed", "artifacts": [str(artifact)]}
    with pytest.raises(ValueError, match="reviewed packet"):
        wf.decide(conn, tid, evidence=evidence, **args)
    evidence["source"] = "revision-1"
    if fail_after_commit:
        def failed_refresh(_):
            raise RuntimeError("simulated post-commit refresh failure")
        with monkeypatch.context() as scoped:
            scoped.setattr(kb, "recompute_ready", failed_refresh)
            with pytest.raises(RuntimeError, match="post-commit"):
                wf.decide(conn, tid, evidence=evidence, **args)
        saved = wf.state(conn, tid)["evidence"]["release"]["data"]
        assert Path(saved["artifacts"][0]).is_file()
        kb.recompute_ready(conn)
    receipt = wf.decide(conn, tid, evidence=evidence, **args)
    assert receipt == wf.decide(conn, tid, evidence=evidence, **args)
    assert not receipt["release_executed"]
    finished = kb.get_task(conn, tid)
    assert finished.status == "done" and finished.completed_at and finished.result == args["note"]
    assert kb.get_task(conn, child).status == "ready"
    assert len([e for e in kb.list_events(conn, tid) if e.kind == "completed"]) == 1
    assert kb.list_runs(conn, tid)[-1].outcome == "completed"
    saved = wf.state(conn, tid)["evidence"]["release"]["data"]
    assert saved["artifacts"][0] != str(artifact)
    assert Path(saved["artifacts"][0]).read_text() == artifact.read_text()
    assert len(kb.list_tasks(conn)) == 2 and kb.claim_task(conn, tid) is None
    packet = wf.state(conn, tid)
    wf.decide(conn, tid, expected_revision=packet["revision"], packet_digest=wf.digest(packet["evidence"]),
              decision="revise", decision_id="reopen-1", note="Explicit new scope on same feature")
    assert kb.get_task(conn, tid).completed_at is None and kb.get_task(conn, tid).result is None


def test_same_head_with_dirty_tracked_source_cannot_pass(board):
    import subprocess
    _, conn, workspace = board
    def git(*args):
        return subprocess.run(["git", "-C", str(workspace), *args], capture_output=True, text=True, check=True).stdout.strip()
    git("init", "-b", "main")
    source = workspace / "source.py"
    source.write_text("answer = 1\n")
    git("add", "source.py")
    git("-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid", "commit", "-m", "Fixture")
    spec = definition()
    spec["steps"]["plan"]["git_head"] = True
    wf.configure(conn, spec)
    tid = wf.start(conn, issue="example/repo#56", title="Dirty head", workspace_path=workspace)
    wf.set_enabled(conn, True)
    task = kb.claim_task(conn, tid)
    result = handoff(conn, task)
    result["evidence"]["head"] = git("rev-parse", "HEAD")
    source.write_text("answer = 2\n")
    with pytest.raises(ValueError, match="tracked workspace"):
        finish(conn, task, result)
    source.write_text("answer = 1\n")
    assert finish(conn, task, result)
