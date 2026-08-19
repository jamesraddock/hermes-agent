"""Workflow-template shape validation for route-bearing Kanban tasks.

The kernel intentionally treats template and step names as opaque data.  The
workflow layer owns the vocabulary; this module only enforces mechanical shape,
identity, assignee/skill, and parent-step contracts.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
import re
from typing import Any, Mapping, Optional

from hermes_constants import get_hermes_home


@dataclass(frozen=True)
class RouteViolation:
    code: str
    message: str
    field: Optional[str] = None
    data: Optional[dict[str, Any]] = None

    def to_dict(self) -> dict[str, Any]:
        result = {"code": self.code, "message": self.message}
        if self.field is not None:
            result["field"] = self.field
        if self.data:
            result.update(self.data)
        return result


class RouteTemplateError(ValueError):
    pass


class RouteValidationError(ValueError):
    def __init__(self, violations: list[RouteViolation]):
        self.violations = violations
        super().__init__("; ".join(v.message for v in violations))


def _template_path(template_id: str) -> Path:
    if not template_id or Path(template_id).name != template_id:
        raise RouteTemplateError("template_id must be one safe path component")
    return get_hermes_home() / "kanban" / "route-templates" / f"{template_id}.json"


def load_template(template_id: str) -> Optional[dict[str, Any]]:
    path = _template_path(template_id)
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise RouteTemplateError(f"invalid route template {template_id}: {exc}") from exc
    if not isinstance(data, dict) or data.get("template_version") != 1:
        raise RouteTemplateError(f"unsupported route template {template_id}")
    if data.get("template_id") != template_id or not isinstance(data.get("steps"), dict):
        raise RouteTemplateError(f"route template identity/steps invalid: {template_id}")
    return data


def canonical_json(value: Mapping[str, Any]) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def compute_route_fingerprint(
    template: Mapping[str, Any], step_key: str, route_meta: Mapping[str, Any]
) -> str:
    step = template.get("steps", {}).get(step_key)
    if not isinstance(step, dict):
        raise RouteTemplateError(f"unknown step {step_key!r}")
    identity_fields = step.get("route_identity") or []
    if not isinstance(identity_fields, list) or not identity_fields:
        raise RouteTemplateError(f"step {step_key!r} has no route_identity")
    identity = {
        "template_id": template["template_id"],
        "step_key": step_key,
    }
    for field in identity_fields:
        if field not in route_meta:
            raise RouteTemplateError(f"missing route identity field {field!r}")
        identity[str(field)] = route_meta[field]
    return hashlib.sha256(canonical_json(identity).encode("utf-8")).hexdigest()


def validate_creation(conn, spec: Mapping[str, Any]) -> list[RouteViolation]:
    template_id = spec.get("workflow_template_id")
    if not template_id:
        return []
    try:
        template = load_template(str(template_id))
    except RouteTemplateError as exc:
        return [RouteViolation("route_template_invalid", str(exc))]
    if template is None:
        return [
            RouteViolation(
                "route_template_unavailable",
                f"route template {template_id!r} is unavailable",
                "workflow_template_id",
            )
        ]
    step_key = str(spec.get("step_key") or "")
    step = template["steps"].get(step_key)
    if not isinstance(step, dict):
        return [RouteViolation("route_step_unknown", f"unknown route step {step_key!r}", "step_key")]

    violations: list[RouteViolation] = []
    expected_assignee = step.get("assignee")
    if expected_assignee and spec.get("assignee") != expected_assignee:
        violations.append(
            RouteViolation(
                "route_assignee_mismatch",
                f"step {step_key!r} requires assignee {expected_assignee!r}",
                "assignee",
            )
        )
    skills = set(spec.get("skills") or [])
    missing_skills = sorted(set(step.get("skills_required") or []) - skills)
    if missing_skills:
        violations.append(
            RouteViolation(
                "route_skills_missing",
                f"step {step_key!r} is missing required skills",
                "skills",
                {"missing_skills": missing_skills},
            )
        )

    route_meta = spec.get("route_meta")
    if not isinstance(route_meta, Mapping):
        violations.append(RouteViolation("route_meta_missing", "route_meta must be an object", "route_meta"))
        route_meta = {}
    for field, pattern in (step.get("required_meta") or {}).items():
        if field not in route_meta:
            violations.append(
                RouteViolation("route_meta_field_missing", f"missing route metadata field {field!r}", str(field))
            )
            continue
        if not re.fullmatch(str(pattern), str(route_meta[field])):
            violations.append(
                RouteViolation("route_meta_field_invalid", f"route metadata field {field!r} is invalid", str(field))
            )

    parent_ids = tuple(str(parent) for parent in (spec.get("parents") or ()))
    if len(parent_ids) != len(set(parent_ids)):
        violations.append(
            RouteViolation(
                "route_parent_duplicate",
                "typed route parents must be unique",
                "parents",
            )
        )
    scoped_task = str(spec.get("worker_task_id") or "").strip()
    if (
        step.get("worker_parent_self_required") is True
        and scoped_task
        and scoped_task not in parent_ids
    ):
        violations.append(
            RouteViolation(
                "route_creator_parent_missing",
                "task-scoped route creation must parent the current task",
                "parents",
                {"required_parent": scoped_task},
            )
        )

    required_all = set(step.get("parent_steps_all") or [])
    required_any = set(step.get("parent_steps_any") or [])
    allowed_steps = required_all | required_any
    rows = []
    if parent_ids:
        placeholders = ",".join("?" for _ in parent_ids)
        rows = conn.execute(
            "SELECT id, workflow_template_id, current_step_key, route_meta "
            f"FROM tasks WHERE id IN ({placeholders})",
            parent_ids,
        ).fetchall()
    rows_by_id = {str(row["id"]): row for row in rows}
    missing_parent_ids = sorted(set(parent_ids) - set(rows_by_id))
    if missing_parent_ids:
        violations.append(
            RouteViolation(
                "route_parent_missing",
                "typed route references missing parents",
                "parents",
                {"missing_parent_ids": missing_parent_ids},
            )
        )

    typed_rows = []
    if allowed_steps:
        for parent_id in parent_ids:
            row = rows_by_id.get(parent_id)
            if row is None:
                continue
            if row["workflow_template_id"] != template_id:
                violations.append(
                    RouteViolation(
                        "route_parent_template_mismatch",
                        "typed route parent must use the same workflow template",
                        "parents",
                        {"parent_id": parent_id},
                    )
                )
                continue
            typed_rows.append(row)
            if row["current_step_key"] not in allowed_steps:
                violations.append(
                    RouteViolation(
                        "route_parent_step_unexpected",
                        f"parent step {row['current_step_key']!r} is not allowed for {step_key!r}",
                        "parents",
                        {"parent_id": parent_id, "parent_step": row["current_step_key"]},
                    )
                )
    else:
        typed_rows = [
            row for row in rows if row["workflow_template_id"] == template_id
        ]
        for row in typed_rows:
            violations.append(
                RouteViolation(
                    "route_parent_step_unexpected",
                    f"root step {step_key!r} cannot have a typed route parent",
                    "parents",
                    {"parent_id": row["id"], "parent_step": row["current_step_key"]},
                )
            )

    parent_step_counts: dict[str, int] = {}
    for row in typed_rows:
        parent_step = str(row["current_step_key"] or "")
        parent_step_counts[parent_step] = parent_step_counts.get(parent_step, 0) + 1
    missing_all = sorted(
        parent_step for parent_step in required_all
        if parent_step_counts.get(parent_step, 0) != 1
    )
    any_count = sum(parent_step_counts.get(parent_step, 0) for parent_step in required_any)
    missing_any = sorted(required_any) if required_any and any_count != 1 else []
    if missing_all or missing_any:
        violations.append(
            RouteViolation(
                "route_parent_steps_missing",
                f"step {step_key!r} requires exactly one parent for each all-step and exactly one any-step",
                "parents",
                {"missing_parent_steps": missing_all + missing_any},
            )
        )

    lineage_fields = template.get("lineage_identity") or []
    if not isinstance(lineage_fields, list):
        violations.append(
            RouteViolation(
                "route_template_lineage_invalid",
                "route template lineage_identity must be a list",
            )
        )
        lineage_fields = []
    for row in typed_rows:
        try:
            parent_meta = json.loads(row["route_meta"] or "{}")
        except (TypeError, ValueError):
            parent_meta = {}
        for field in lineage_fields:
            if field not in route_meta or parent_meta.get(field) != route_meta.get(field):
                violations.append(
                    RouteViolation(
                        "route_parent_lineage_mismatch",
                        f"parent {row['id']} does not match child lineage field {field!r}",
                        "parents",
                        {"parent_id": row["id"], "lineage_field": str(field)},
                    )
                )
    return violations
