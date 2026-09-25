"""Operator/worker CLI for the native board workflow."""
from __future__ import annotations
import json
from pathlib import Path
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_workflow as wf


def _migrate(conn, args):
    from hermes_cli.kanban_workflow_migration import migrate
    with kbc.connect_closing(Path(args.source_db)) as source:
        return migrate(conn, source, json.loads(Path(args.file).read_text()))


def command(args):
    with kbc.connect_closing() as conn:
        handlers = {
            "migrate": lambda: _migrate(conn, args),
            "configure": lambda: wf.configure(conn, json.loads(Path(args.file).read_text())),
            "enable": lambda: wf.set_enabled(conn, True),
            "disable": lambda: wf.set_enabled(conn, False),
            "start": lambda: wf.start(conn, issue=args.issue, title=args.title,
                                      body=Path(args.body_file).read_text() if args.body_file else None,
                                      workspace_path=args.workspace),
            "show": lambda: wf.packet(conn, args.task_id),
            "decide": lambda: wf.decide(conn, args.task_id, expected_revision=args.revision,
                                       packet_digest=args.packet_digest, decision=args.decision, note=args.note,
                                       decision_id=args.decision_id,
                                       evidence=json.loads(Path(args.evidence_file).read_text()) if args.evidence_file else None),
            "admission": lambda: wf.set_admission(conn, paused=args.paused,
                                                  retired_assignees=args.retired_assignee),
        }
        result = handlers[args.workflow_action]()
        print(json.dumps({"ok": True, "result": result}))
    return 0
