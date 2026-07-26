"""Deterministic, transcript-free Leader Snapshot contract."""

from __future__ import annotations

import hashlib
import json
from collections import Counter
from collections.abc import Iterable, Mapping
from decimal import Decimal, InvalidOperation
from typing import Any

from .project_artifacts import PROJECT_ARTIFACT_SCHEMA


LEADER_SNAPSHOT_SCHEMA = "leader-snapshot-v1"


class LeaderSnapshotError(ValueError):
    """Raised when a Leader Snapshot is malformed or internally inconsistent."""


def _canonical_bytes(value: object) -> bytes:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise LeaderSnapshotError("snapshot data must be canonical JSON") from exc


def _digest(value: object) -> str:
    return "sha256:" + hashlib.sha256(_canonical_bytes(value)).hexdigest()


def _money(value: object) -> Decimal:
    if value is None:
        return Decimal("0")
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise LeaderSnapshotError("budget data contains invalid money") from exc
    if not result.is_finite() or result < 0:
        raise LeaderSnapshotError("budget data contains invalid money")
    return result


def _money_text(value: Decimal) -> str:
    return format(value.quantize(Decimal("0.000000001")), "f")


def _artifact_binding(rows: list[Mapping[str, Any]]) -> dict[str, Any]:
    event_ids = sorted(
        str(row.get("event_id"))
        for row in rows
        if isinstance(row.get("event_id"), str)
    )
    return {
        "event_count": len(event_ids),
        "event_ids_sha256": _digest(event_ids),
    }


def _budget(tasks: list[Mapping[str, Any]]) -> dict[str, Any]:
    spent = Decimal("0")
    reserved = Decimal("0")
    for task in tasks:
        task_reserved = Decimal("0")
        for attempt in task.get("attempts") or []:
            if not isinstance(attempt, Mapping):
                continue
            actual = _money(attempt.get("actual_cost_cny"))
            estimate = _money(attempt.get("estimated_cost_cny"))
            hold = _money(attempt.get("reserved_cost_cny"))
            spent += actual
            status = str(attempt.get("status") or "")
            if status in {"prepared", "dispatched", "running", "waiting_leader"}:
                task_reserved += max(hold, estimate)
        envelope = task.get("route_budget_envelope")
        if isinstance(envelope, Mapping) and envelope.get("status") == "active":
            task_reserved = max(
                task_reserved, _money(envelope.get("reserved_cost_cny"))
            )
        reserved += task_reserved
    material = {
        "spent_cny": _money_text(spent),
        "reserved_cny": _money_text(reserved),
    }
    material["revision_sha256"] = _digest(material)
    return material


def _pending_decisions(tasks: list[Mapping[str, Any]]) -> list[dict[str, str]]:
    decisions: list[dict[str, str]] = []
    for task in sorted(tasks, key=lambda item: str(item.get("id") or "")):
        task_id = str(task.get("id") or "")
        status = str(task.get("status") or "unknown")
        if status == "waiting_leader":
            decisions.append(
                {
                    "task_id": task_id,
                    "decision": "accept-or-reject",
                    "reason": "worker result awaits Leader gate review",
                }
            )
        elif status == "escalate":
            decisions.append(
                {
                    "task_id": task_id,
                    "decision": "authorize-escalation",
                    "reason": "rejected attempt requests the next admitted route step",
                }
            )
    return decisions


def _risks(tasks: list[Mapping[str, Any]], graph: Mapping[str, Any]) -> list[dict[str, Any]]:
    risks: list[dict[str, Any]] = []
    blocked = sorted(
        task_id
        for task_id, node in (graph.get("nodes") or {}).items()
        if isinstance(node, Mapping) and node.get("state") == "blocked"
    )
    if blocked:
        risks.append(
            {
                "kind": "blocked-work",
                "severity": "medium",
                "task_ids": blocked,
            }
        )
    for task in sorted(tasks, key=lambda item: str(item.get("id") or "")):
        risk = str(task.get("risk") or "medium")
        if risk in {"high", "critical"} and task.get("status") not in {
            "done",
            "failed",
            "cancelled",
        }:
            risks.append(
                {
                    "kind": "task-risk",
                    "severity": risk,
                    "task_ids": [str(task.get("id") or "")],
                }
            )
    return risks


def build_leader_snapshot(
    *,
    project: Mapping[str, Any],
    graph: Mapping[str, Any],
    tasks: Iterable[Mapping[str, Any]],
    artifact_rows: Iterable[Mapping[str, Any]],
) -> dict[str, Any]:
    """Build the same snapshot bytes for the same durable project state."""

    task_rows = list(tasks)
    artifacts = list(artifact_rows)
    nodes = graph.get("nodes")
    if not isinstance(nodes, Mapping) or type(graph.get("revision")) is not int:
        raise LeaderSnapshotError("work graph is invalid")
    states = Counter(
        str(node.get("state") or "unknown")
        for node in nodes.values()
        if isinstance(node, Mapping)
    )
    recent = [
        {
            "artifact_id": row.get("artifact_id"),
            "event_id": row.get("event_id"),
            "kind": row.get("kind"),
            "lifecycle": row.get("lifecycle"),
        }
        for row in artifacts
        if row.get("lifecycle") in {"accepted", "rejected"}
    ][-20:]
    pending = _pending_decisions(task_rows)
    risks = _risks(task_rows, graph)
    budget = _budget(task_rows)
    ready = sorted(
        task_id
        for task_id, node in nodes.items()
        if isinstance(node, Mapping) and node.get("state") == "ready"
    )
    blocked = sorted(
        task_id
        for task_id, node in nodes.items()
        if isinstance(node, Mapping) and node.get("state") == "blocked"
    )
    actions: list[str] = []
    if pending:
        actions.append("review-pending-decisions")
    if ready:
        actions.append("dispatch-ready-work")
    if blocked:
        actions.append("resolve-blocked-dependencies")
    if not actions:
        actions.append("inspect-project-status")
    body = {
        "schema_version": LEADER_SNAPSHOT_SCHEMA,
        "project_id": str(project.get("project_id") or ""),
        "objective": str(project.get("objective") or ""),
        "state_binding": {
            "work_graph_revision": graph["revision"],
            "budget_revision_sha256": budget["revision_sha256"],
            "artifact_revision": _artifact_binding(artifacts),
        },
        "work_graph": {
            "state_counts": dict(sorted(states.items())),
            "ready_task_ids": ready,
            "blocked_task_ids": blocked,
        },
        "pending_decisions": pending,
        "recent_artifacts": recent,
        "risks": risks,
        "budget": {
            "limit_cny": (project.get("routing_policy") or {}).get(
                "project_budget_cny"
            ),
            "spent_cny": budget["spent_cny"],
            "reserved_cny": budget["reserved_cny"],
        },
        "recommended_actions": actions,
        "transcript_included": False,
    }
    snapshot_sha256 = _digest(body)
    return {
        **body,
        "snapshot_id": "LSNP-" + snapshot_sha256.removeprefix("sha256:")[:32],
        "snapshot_sha256": snapshot_sha256,
    }


def validate_leader_snapshot(value: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(value, Mapping) or value.get("schema_version") != LEADER_SNAPSHOT_SCHEMA:
        raise LeaderSnapshotError("unsupported Leader Snapshot schema")
    row = json.loads(_canonical_bytes(dict(value)))
    digest = row.pop("snapshot_sha256", None)
    snapshot_id = row.pop("snapshot_id", None)
    expected = _digest(row)
    if digest != expected or snapshot_id != "LSNP-" + expected.removeprefix("sha256:")[:32]:
        raise LeaderSnapshotError("Leader Snapshot hash binding is invalid")
    if row.get("transcript_included") is not False:
        raise LeaderSnapshotError("Leader Snapshot must not include a transcript")
    return json.loads(_canonical_bytes(dict(value)))


def latest_leader_snapshot(rows: Iterable[Mapping[str, Any]]) -> dict[str, Any] | None:
    validated = [validate_leader_snapshot(row) for row in rows]
    return validated[-1] if validated else None


__all__ = [
    "LEADER_SNAPSHOT_SCHEMA",
    "LeaderSnapshotError",
    "build_leader_snapshot",
    "latest_leader_snapshot",
    "validate_leader_snapshot",
]
