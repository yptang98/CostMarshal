"""Observable total-cost accounting centered on accepted Artifacts."""

from __future__ import annotations

import json
from collections import Counter
from collections.abc import Iterable, Mapping
from decimal import Decimal, InvalidOperation
from typing import Any

from .quality import canonical_sha256
from .state import now_iso


TOTAL_COST_REPORT_SCHEMA = "costmarshal-total-cost-report-v1"


class CostModelError(ValueError):
    """Raised when cost observations are malformed or internally inconsistent."""


def _money(value: object) -> Decimal | None:
    if value is None:
        return None
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise CostModelError("cost observation contains invalid money") from exc
    if not result.is_finite() or result < 0:
        raise CostModelError("cost observation contains invalid money")
    return result


def _money_text(value: Decimal) -> str:
    return format(value.quantize(Decimal("0.000000001")), "f")


def _accepted_artifact_ids(rows: Iterable[Mapping[str, Any]]) -> list[str]:
    latest: dict[str, str] = {}
    for row in rows:
        artifact_id = row.get("artifact_id")
        lifecycle = row.get("lifecycle")
        if isinstance(artifact_id, str) and isinstance(lifecycle, str):
            latest[artifact_id] = lifecycle
    return sorted(
        artifact_id
        for artifact_id, lifecycle in latest.items()
        if lifecycle == "accepted"
    )


def build_total_cost_report(
    *,
    project: Mapping[str, Any],
    tasks: Iterable[Mapping[str, Any]],
    results: Iterable[Mapping[str, Any]],
    evaluations: Iterable[Mapping[str, Any]],
    gate_results: Iterable[Mapping[str, Any]],
    artifact_rows: Iterable[Mapping[str, Any]],
    leader_work: Iterable[Mapping[str, Any]],
    usage_events: Iterable[Mapping[str, Any]] = (),
) -> dict[str, Any]:
    task_rows = list(tasks)
    result_rows = list(results)
    evaluation_rows = list(evaluations)
    gate_rows = list(gate_results)
    artifacts = list(artifact_rows)
    leader_rows = list(leader_work)
    usage_rows = list(usage_events)
    accepted_artifacts = _accepted_artifact_ids(artifacts)

    provider_known = Decimal("0")
    provider_unknown = 0
    for row in result_rows:
        value = _money(row.get("estimated_cost_cny"))
        if value is None:
            provider_unknown += 1
        else:
            provider_known += value
    terminal_attempt_ids = {
        str(row.get("attempt_id"))
        for row in result_rows
        if isinstance(row.get("attempt_id"), str) and row.get("attempt_id")
    }
    unmatched_usage_rows = [
        row
        for row in usage_rows
        if str(row.get("attempt_id") or "") not in terminal_attempt_ids
    ]
    for row in unmatched_usage_rows:
        value = _money(row.get("estimated_cost_cny"))
        if value is None:
            provider_unknown += 1
        else:
            provider_known += value
    leader_known = Decimal("0")
    leader_unknown = 0
    for row in leader_rows:
        value = _money(row.get("estimated_cost_cny"))
        if value is None:
            leader_unknown += 1
        else:
            leader_known += value
    known_total = provider_known + leader_known
    unknown_count = provider_unknown + leader_unknown

    execution_tokens = 0
    execution_seconds = 0
    verification_tokens = 0
    verification_seconds = 0
    in_progress_tokens = sum(
        int(row.get("total_tokens") or 0) for row in unmatched_usage_rows
    )
    errors: Counter[str] = Counter()
    for row in evaluation_rows:
        usage = row.get("usage") or {}
        tokens = int(usage.get("actual_tokens") or 0)
        seconds = int(usage.get("wall_seconds") or 0)
        if row.get("role") == "reviewer":
            verification_tokens += tokens
            verification_seconds += seconds
        else:
            execution_tokens += tokens
            execution_seconds += seconds
        attribution = str((row.get("error") or {}).get("attribution") or "none")
        if attribution != "none":
            errors[attribution] += 1

    attempt_count = sum(
        len(task.get("attempts") or [])
        for task in task_rows
        if isinstance(task, Mapping)
    )
    retry_count = sum(
        max(0, len(task.get("attempts") or []) - 1)
        for task in task_rows
        if isinstance(task, Mapping)
    )
    escalations = sum(row.get("status") == "escalate" for row in result_rows)
    handoff_count = 0
    handoff_bytes = 0
    for task in task_rows:
        for attempt in task.get("attempts") or []:
            capsule = attempt.get("handoff_capsule")
            if not isinstance(capsule, Mapping):
                continue
            receipt = capsule.get("handoff") or {}
            handoff_count += 1
            handoff_bytes += int(receipt.get("size_bytes") or 0)

    failed_gate_reasons: Counter[str] = Counter()
    for row in gate_rows:
        for check in row.get("checks") or []:
            if isinstance(check, Mapping) and check.get("passed") is False:
                failed_gate_reasons[str(check.get("name") or "unknown")] += 1

    leader_minutes = sum(int(row.get("minutes") or 0) for row in leader_rows)
    leader_tokens = sum(int(row.get("total_tokens") or 0) for row in leader_rows)
    artifact_count = len(accepted_artifacts)
    if artifact_count:
        known_per_artifact = _money_text(known_total / Decimal(artifact_count))
        metric_status = "complete" if unknown_count == 0 else "partial"
    else:
        known_per_artifact = None
        metric_status = "unavailable-no-accepted-artifacts"
    evidence = {
        "task_ids": sorted(
            str(row.get("id")) for row in task_rows if row.get("id")
        ),
        "result_ids": sorted(
            str(row.get("id")) for row in result_rows if row.get("id")
        ),
        "evaluation_ids": sorted(
            str(row.get("id")) for row in evaluation_rows if row.get("id")
        ),
        "gate_ids": sorted(
            str(row.get("id")) for row in gate_rows if row.get("id")
        ),
        "artifact_ids": accepted_artifacts,
        "leader_work_ids": sorted(
            str(row.get("id")) for row in leader_rows if row.get("id")
        ),
        "unmatched_usage_ids": sorted(
            str(row.get("id")) for row in unmatched_usage_rows if row.get("id")
        ),
    }
    body = {
        "schema_version": TOTAL_COST_REPORT_SCHEMA,
        "project_id": str(project.get("project_id") or ""),
        "metric": {
            "name": "cost-per-accepted-artifact",
            "status": metric_status,
            "accepted_artifact_count": artifact_count,
            "known_monetary_cost_cny": _money_text(known_total),
            "known_monetary_cost_per_accepted_artifact_cny": known_per_artifact,
            "unknown_monetary_observation_count": unknown_count,
            "warning": (
                "Observable non-monetary costs are not assigned invented prices."
            ),
        },
        "dimensions": {
            "execution": {
                "worker_tokens": execution_tokens,
                "worker_wall_seconds": execution_seconds,
                "in_progress_unmatched_tokens": in_progress_tokens,
                "in_progress_unmatched_usage_count": len(unmatched_usage_rows),
                "known_provider_cost_cny": _money_text(provider_known),
            },
            "verification": {
                "reviewer_tokens": verification_tokens,
                "reviewer_wall_seconds": verification_seconds,
            },
            "rework": {
                "attempt_count": attempt_count,
                "retry_count": retry_count,
                "escalation_count": escalations,
            },
            "collaboration_context": {
                "structured_handoff_count": handoff_count,
                "structured_handoff_bytes": handoff_bytes,
            },
            "leader_attention": {
                "review_minutes": leader_minutes,
                "tokens": leader_tokens,
                "known_cost_cny": _money_text(leader_known),
            },
            "failure_recovery_risk": {
                "error_attribution": dict(sorted(errors.items())),
                "failed_gate_reasons": dict(sorted(failed_gate_reasons.items())),
            },
        },
        "evidence": evidence,
        "evidence_sha256": canonical_sha256(evidence),
    }
    report_sha256 = canonical_sha256(body)
    return {
        **body,
        "report_id": "COST-" + report_sha256.removeprefix("sha256:")[:32],
        "generated_at": now_iso(),
        "report_sha256": report_sha256,
    }


def validate_total_cost_report(value: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(value, Mapping) or value.get("schema_version") != TOTAL_COST_REPORT_SCHEMA:
        raise CostModelError("unsupported total cost report schema")
    row = json.loads(
        json.dumps(
            dict(value),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    )
    if not isinstance(row.get("project_id"), str) or not row["project_id"]:
        raise CostModelError("total cost report project_id is required")
    metric = row.get("metric")
    dimensions = row.get("dimensions")
    evidence = row.get("evidence")
    if (
        not isinstance(metric, dict)
        or not isinstance(dimensions, dict)
        or not isinstance(evidence, dict)
    ):
        raise CostModelError("total cost report structure is invalid")
    accepted_count = metric.get("accepted_artifact_count")
    unknown_count = metric.get("unknown_monetary_observation_count")
    if (
        type(accepted_count) is not int
        or accepted_count < 0
        or type(unknown_count) is not int
        or unknown_count < 0
    ):
        raise CostModelError("total cost report metric counts are invalid")
    artifact_ids = evidence.get("artifact_ids")
    if (
        not isinstance(artifact_ids, list)
        or any(not isinstance(item, str) or not item for item in artifact_ids)
        or len(artifact_ids) != len(set(artifact_ids))
        or accepted_count != len(artifact_ids)
    ):
        raise CostModelError("total cost report Artifact evidence is invalid")
    known_total = _money(metric.get("known_monetary_cost_cny"))
    if known_total is None:
        raise CostModelError("total cost report known monetary cost is required")
    expected_status = (
        "unavailable-no-accepted-artifacts"
        if accepted_count == 0
        else "partial"
        if unknown_count
        else "complete"
    )
    if metric.get("status") != expected_status:
        raise CostModelError("total cost report metric status is inconsistent")
    per_artifact = _money(
        metric.get("known_monetary_cost_per_accepted_artifact_cny")
    )
    if accepted_count == 0:
        if per_artifact is not None:
            raise CostModelError("cost per Artifact requires an accepted Artifact")
    elif per_artifact is None or _money_text(per_artifact) != _money_text(
        known_total / Decimal(accepted_count)
    ):
        raise CostModelError("cost per Artifact is inconsistent")
    body = {
        key: val
        for key, val in row.items()
        if key not in {"report_id", "generated_at", "report_sha256"}
    }
    digest = canonical_sha256(body)
    if (
        row.get("report_sha256") != digest
        or row.get("report_id")
        != "COST-" + digest.removeprefix("sha256:")[:32]
        or row.get("evidence_sha256")
        != canonical_sha256(row.get("evidence") or {})
    ):
        raise CostModelError("total cost report hash binding is invalid")
    return row


__all__ = [
    "TOTAL_COST_REPORT_SCHEMA",
    "CostModelError",
    "build_total_cost_report",
    "validate_total_cost_report",
]
