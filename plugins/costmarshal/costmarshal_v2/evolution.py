"""Evidence-backed learning records, model memory, and teaching policy."""

from __future__ import annotations

from collections import Counter, defaultdict
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
import hashlib
import json
from pathlib import Path
from statistics import fmean
from typing import Any, Iterable

from .paths import ProjectLayout
from .quality import canonical_sha256
from .state import append_jsonl, new_id, now_iso, read_jsonl


EVALUATION_SCHEMA = "costmarshal-attempt-evaluation-v1"
RETROSPECTIVE_SCHEMA = "costmarshal-project-retrospective-v1"
POLICY_CANDIDATE_SCHEMA = "costmarshal-policy-candidate-v1"
MODEL_MEMORY_SCHEMA = "costmarshal-model-memory-v1"
ERROR_ATTRIBUTIONS = frozenset(
    {
        "none",
        "unknown",
        "routing",
        "model-capability",
        "instruction",
        "context",
        "tool",
        "environment",
        "dependency",
        "verification",
        "budget",
        "timeout",
        "human-review",
    }
)
TEACHING_MODES = frozenset({"auto", "off", "review", "paired", "replay"})
POLICY_LIFECYCLE = (
    "observed",
    "aggregated",
    "candidate",
    "replayed",
    "shadow",
    "canary",
    "active",
    "deprecated",
    "rolled_back",
)
POLICY_TRANSITIONS = {
    "candidate": ("replayed", "deprecated"),
    "replayed": ("shadow", "deprecated"),
    "shadow": ("canary", "deprecated"),
    "canary": ("active", "deprecated"),
    "active": ("deprecated", "rolled_back"),
    "deprecated": (),
    "rolled_back": (),
}


class EvolutionError(ValueError):
    """Raised when learning evidence is invalid or unsafe to aggregate."""


def _score(value: Any, label: str) -> int:
    if type(value) is not int or value not in {1, 2, 3, 4, 5}:
        raise EvolutionError(f"{label} must be an integer from 1 to 5")
    return value


def _derived_efficiency(
    task: dict[str, Any], result: dict[str, Any], accepted: bool
) -> int:
    estimated = sum(
        int(task.get(field) or 0)
        for field in (
            "estimated_input_tokens",
            "estimated_cached_input_tokens",
            "estimated_output_tokens",
        )
    )
    actual = int(result.get("total_tokens") or 0)
    if not accepted:
        return 1 if actual > 0 else 2
    if estimated <= 0 or actual <= 0:
        return 3
    ratio = actual / estimated
    if ratio <= 0.85:
        return 5
    if ratio <= 1.10:
        return 4
    if ratio <= 1.35:
        return 3
    if ratio <= 1.75:
        return 2
    return 1


def _derived_routing_fit(task: dict[str, Any], result: dict[str, Any]) -> int:
    accepted = result.get("accepted_by_leader") is True
    step = int(result.get("route_plan_step_index") or 0)
    if accepted and step == 0:
        return 5
    if accepted:
        return max(2, 4 - min(step, 2))
    if result.get("status") == "escalate":
        return 2
    return 1


def _attempt_wall_seconds(attempt: dict[str, Any]) -> int | None:
    started = attempt.get("started_at")
    finished = attempt.get("finished_at")
    if not isinstance(started, str) or not isinstance(finished, str):
        return None
    try:
        start_time = datetime.fromisoformat(started.replace("Z", "+00:00"))
        finish_time = datetime.fromisoformat(finished.replace("Z", "+00:00"))
    except ValueError:
        return None
    if start_time.tzinfo is None or finish_time.tzinfo is None:
        return None
    seconds = int((finish_time - start_time).total_seconds())
    return seconds if seconds >= 0 else None


def build_attempt_evaluation(
    *,
    task: dict[str, Any],
    attempt: dict[str, Any],
    result: dict[str, Any],
    gate_result: dict[str, Any],
    error_attribution: str | None = None,
    error_severity: int = 0,
    efficiency_score: int | None = None,
    instruction_score: int | None = None,
    handoff_score: int | None = None,
    teaching_evidence: str | None = None,
) -> dict[str, Any]:
    accepted = result.get("accepted_by_leader") is True
    attribution = str(error_attribution or ("none" if accepted else "unknown"))
    if attribution not in ERROR_ATTRIBUTIONS:
        raise EvolutionError(f"unsupported error attribution: {attribution}")
    if type(error_severity) is not int or error_severity not in {0, 1, 2, 3, 4, 5}:
        raise EvolutionError("error severity must be 0-5")
    if accepted and attribution != "none" and error_severity == 0:
        raise EvolutionError("a non-none error attribution requires positive severity")
    if not accepted and attribution == "none":
        raise EvolutionError("a rejected result cannot use error attribution none")
    quality = _score(int(result.get("quality_score") or 0), "quality score")
    efficiency = _score(
        efficiency_score
        if efficiency_score is not None
        else _derived_efficiency(task, result, accepted),
        "efficiency score",
    )
    instruction = _score(
        instruction_score if instruction_score is not None else quality,
        "instruction score",
    )
    handoff = _score(
        handoff_score
        if handoff_score is not None
        else (4 if accepted else 3 if result.get("status") == "escalate" else 1),
        "handoff score",
    )
    reliability = max(
        1,
        min(
            5,
            (5 if accepted else 2 if result.get("status") == "escalate" else 1)
            - max(0, error_severity - 2),
        ),
    )
    routing_fit = _derived_routing_fit(task, result)
    score_values = [quality, efficiency, instruction, handoff, reliability, routing_fit]
    score_total = round(fmean(score_values), 3)
    estimated_tokens = sum(
        int(task.get(field) or 0)
        for field in (
            "estimated_input_tokens",
            "estimated_cached_input_tokens",
            "estimated_output_tokens",
        )
    )
    source_hash = canonical_sha256(result)
    material = {
        "result_id": result.get("id"),
        "source_result_sha256": source_hash,
        "scores": {
            "quality": quality,
            "efficiency": efficiency,
            "instruction_following": instruction,
            "handoff": handoff,
            "reliability": reliability,
            "routing_fit": routing_fit,
        },
        "error": {"attribution": attribution, "severity": error_severity},
        "gate_evidence_sha256": gate_result.get("evidence_sha256"),
    }
    return {
        "schema_version": EVALUATION_SCHEMA,
        "id": new_id("EVAL"),
        "timestamp": now_iso(),
        "project_id": result.get("project_id"),
        "task_id": result.get("task_id"),
        "attempt_id": result.get("attempt_id"),
        "result_id": result.get("id"),
        "source_result_sha256": source_hash,
        "provider": result.get("provider"),
        "tier": result.get("tier"),
        "model": result.get("execution_model") or result.get("model"),
        "profile": result.get("profile"),
        "profile_sha256": result.get("profile_sha256"),
        "task_type": task.get("task_type") or "unknown",
        "difficulty": task.get("difficulty") or "normal",
        "risk": task.get("risk") or "low",
        "role": task.get("role") or "builder",
        "required_capabilities": list(task.get("required_capabilities") or []),
        "accepted": accepted,
        "routing_success": bool(
            accepted
            and quality >= 3
            and error_severity <= 1
            and gate_result.get("passed") is True
        ),
        "gate_passed": gate_result.get("passed") is True,
        "scores": material["scores"],
        "score_total": score_total,
        "error": material["error"],
        "usage": {
            "estimated_tokens": estimated_tokens,
            "actual_tokens": int(result.get("total_tokens") or 0),
            "estimated_cost_cny": result.get("estimated_cost_cny"),
            "cost_source": result.get("cost_source"),
            "wall_seconds": _attempt_wall_seconds(attempt),
        },
        "teaching": {
            "mode": (task.get("teaching") or {}).get("mode") or "off",
            "reason": (task.get("teaching") or {}).get("reason"),
            "evidence": str(teaching_evidence or "").strip() or None,
        },
        "counterfactual": {
            "status": (
                "paired-observation"
                if (task.get("teaching") or {}).get("mode") == "paired"
                else "unobserved"
            ),
            "warning": "Do not infer an untried model outcome from this attempt.",
        },
        "learning_state": "observed",
        "evidence_sha256": canonical_sha256(material),
    }


def append_evaluation(layout: ProjectLayout, evaluation: dict[str, Any]) -> bool:
    rows = read_jsonl(layout.evaluations_jsonl)
    if any(row.get("result_id") == evaluation.get("result_id") for row in rows):
        return False
    append_jsonl(layout.evaluations_jsonl, evaluation)
    return True


def _iter_project_evaluations(root: Path) -> Iterable[dict[str, Any]]:
    projects_dir = root / "projects"
    if not projects_dir.is_dir():
        return
    for path in sorted(projects_dir.glob("*/reports/evaluations.jsonl")):
        for row in read_jsonl(path):
            if isinstance(row, dict) and row.get("schema_version") == EVALUATION_SCHEMA:
                yield row


def build_model_memory(
    root: Path, *, evaluations: Iterable[dict[str, Any]] | None = None
) -> dict[str, Any]:
    """Build a disposable cross-project aggregate; raw prompts never enter it."""

    raw_source = list(evaluations) if evaluations is not None else list(
        _iter_project_evaluations(root)
    )
    source: list[dict[str, Any]] = []
    seen_evaluations: dict[str, str] = {}
    for row in raw_source:
        evaluation_id = row.get("id")
        if not isinstance(evaluation_id, str) or not evaluation_id:
            continue
        digest = canonical_sha256(row)
        previous = seen_evaluations.get(evaluation_id)
        if previous is not None:
            if previous != digest:
                raise EvolutionError(
                    f"conflicting model-memory evaluation id: {evaluation_id}"
                )
            continue
        seen_evaluations[evaluation_id] = digest
        source.append(row)
    groups: dict[tuple[str, ...], list[dict[str, Any]]] = defaultdict(list)
    for row in source:
        identity = (
            str(row.get("provider") or "unknown"),
            str(row.get("model") or "inherit"),
            str(row.get("profile") or ""),
            str(row.get("profile_sha256") or ""),
            str(row.get("task_type") or "unknown"),
            str(row.get("difficulty") or "normal"),
            str(row.get("role") or "builder"),
        )
        groups[identity].append(row)
    profiles: list[dict[str, Any]] = []
    for identity, rows in sorted(groups.items()):
        errors = Counter(
            str((row.get("error") or {}).get("attribution") or "unknown")
            for row in rows
            if (row.get("error") or {}).get("attribution") != "none"
        )
        capabilities = Counter(
            capability
            for row in rows
            if row.get("routing_success") is True
            for capability in row.get("required_capabilities") or []
            if isinstance(capability, str)
        )
        score_names = (
            "quality",
            "efficiency",
            "instruction_following",
            "handoff",
            "reliability",
            "routing_fit",
        )
        means = {
            name: round(
                fmean(
                    float((row.get("scores") or {}).get(name) or 0) for row in rows
                ),
                3,
            )
            for name in score_names
        }
        sample_count = len(rows)
        accepted = sum(row.get("accepted") is True for row in rows)
        routing_successes = sum(row.get("routing_success") is True for row in rows)
        actual_tokens = [
            int((row.get("usage") or {}).get("actual_tokens") or 0)
            for row in rows
        ]
        forecast_ratios = [
            int((row.get("usage") or {}).get("actual_tokens") or 0)
            / int((row.get("usage") or {}).get("estimated_tokens") or 1)
            for row in rows
            if int((row.get("usage") or {}).get("estimated_tokens") or 0) > 0
        ]
        wall_seconds = [
            int((row.get("usage") or {}).get("wall_seconds"))
            for row in rows
            if type((row.get("usage") or {}).get("wall_seconds")) is int
        ]
        costs = []
        for row in rows:
            value = (row.get("usage") or {}).get("estimated_cost_cny")
            if value is None:
                continue
            try:
                costs.append(Decimal(str(value)))
            except (InvalidOperation, ValueError):
                continue
        profiles.append(
            {
                "identity": {
                    "provider": identity[0],
                    "model": identity[1],
                    "profile": identity[2] or None,
                    "profile_sha256": identity[3] or None,
                },
                "scope": {
                    "task_type": identity[4],
                    "difficulty": identity[5],
                    "role": identity[6],
                },
                "sample_count": sample_count,
                "accepted_count": accepted,
                "routing_success_count": routing_successes,
                "acceptance_rate": round(accepted / sample_count, 4),
                "routing_success_rate": round(routing_successes / sample_count, 4),
                "confidence": round(min(1.0, sample_count / 12.0), 4),
                "score_means": means,
                "usage_means": {
                    "actual_tokens": round(fmean(actual_tokens), 3),
                    "forecast_ratio": (
                        round(fmean(forecast_ratios), 4)
                        if forecast_ratios
                        else None
                    ),
                    "wall_seconds": (
                        round(fmean(wall_seconds), 3) if wall_seconds else None
                    ),
                    "estimated_cost_cny": (
                        format(
                            sum(
                                costs,
                                Decimal("0"),
                            )
                            / Decimal(len(costs)),
                            "f",
                        )
                        if costs
                        else None
                    ),
                },
                "demonstrated_capabilities": dict(sorted(capabilities.items())),
                "error_attribution": dict(sorted(errors.items())),
                "latest_observation_at": max(
                    str(row.get("timestamp") or "") for row in rows
                ),
                "evidence_evaluation_ids": sorted(
                    str(row.get("id")) for row in rows if row.get("id")
                ),
            }
        )
    evidence_ids = sorted(
        str(row.get("id")) for row in source if isinstance(row.get("id"), str)
    )
    return {
        "schema_version": MODEL_MEMORY_SCHEMA,
        "generated_at": now_iso(),
        "source": "rebuildable-project-evaluation-ledgers",
        "privacy": "aggregate-only; no prompts, reports, summaries, or raw artifacts",
        "evaluation_count": len(source),
        "profiles": profiles,
        "evidence_sha256": canonical_sha256(evidence_ids),
    }


def choose_teaching_mode(
    *,
    requested_mode: str,
    risk: str,
    profile: dict[str, Any] | None,
    max_cost_cny: str | None,
) -> dict[str, Any]:
    """Select teaching only when uncertainty justifies its extra review cost."""

    if requested_mode not in TEACHING_MODES:
        raise EvolutionError(f"unsupported teaching mode: {requested_mode}")
    if requested_mode != "auto":
        return {
            "schema_version": "costmarshal-teaching-decision-v1",
            "mode": requested_mode,
            "reason": "explicit task policy",
            "required_evidence": requested_mode != "off",
            "enforcement": "required" if requested_mode != "off" else "none",
            "decided_at": now_iso(),
        }
    sample_count = int((profile or {}).get("sample_count") or 0)
    confidence = float((profile or {}).get("confidence") or 0.0)
    routing_success_rate = float((profile or {}).get("routing_success_rate") or 0.0)
    if risk == "high":
        mode, reason = "review", "high-risk work requires independent teaching evidence"
    elif sample_count < 3:
        mode, reason = "paired", "cold-start model/task scope has fewer than three observations"
    elif sample_count >= 5 and routing_success_rate < 0.6:
        mode, reason = "replay", "repeated weak outcomes require a controlled replay"
    elif confidence < 0.5:
        mode, reason = "review", "model/task evidence confidence is still low"
    else:
        mode, reason = "off", "evidence is sufficiently mature for normal execution"
    if mode == "paired" and max_cost_cny == "0.000000000":
        mode, reason = "review", "paired teaching was reduced to review by the zero budget"
    return {
        "schema_version": "costmarshal-teaching-decision-v1",
        "mode": mode,
        "reason": reason,
        "required_evidence": mode != "off",
        "enforcement": "advisory" if mode != "off" else "none",
        "sample_count": sample_count,
        "confidence": confidence,
        "decided_at": now_iso(),
    }


def profile_for_task(
    memory: dict[str, Any],
    *,
    provider: str,
    model: str,
    profile_sha256: str | None,
    task_type: str,
    difficulty: str,
    role: str,
) -> dict[str, Any] | None:
    candidates = []
    for item in memory.get("profiles") or []:
        identity = item.get("identity") or {}
        scope = item.get("scope") or {}
        if (
            identity.get("provider") == provider
            and identity.get("model") == model
            and identity.get("profile_sha256") == profile_sha256
            and scope.get("task_type") == task_type
            and scope.get("difficulty") == difficulty
            and scope.get("role") == role
        ):
            candidates.append(item)
    return max(candidates, key=lambda item: int(item.get("sample_count") or 0), default=None)


def _money_sum(rows: Iterable[dict[str, Any]]) -> str | None:
    total = Decimal("0")
    seen = False
    for row in rows:
        value = (row.get("usage") or {}).get("estimated_cost_cny")
        if value is None:
            continue
        try:
            total += Decimal(str(value))
            seen = True
        except (InvalidOperation, ValueError):
            continue
    return format(total, "f") if seen else None


def build_project_retrospective(
    *,
    project: dict[str, Any],
    tasks: Iterable[dict[str, Any]],
    evaluations: Iterable[dict[str, Any]],
) -> dict[str, Any] | None:
    task_rows = list(tasks)
    if not task_rows or any(
        task.get("status") not in {"done", "failed", "cancelled"} for task in task_rows
    ):
        return None
    rows = list(evaluations)
    result_ids = sorted(str(row.get("result_id")) for row in rows if row.get("result_id"))
    evidence_hash = canonical_sha256(result_ids)
    accepted = sum(row.get("accepted") is True for row in rows)
    routing_success = sum(row.get("routing_success") is True for row in rows)
    errors = Counter(
        str((row.get("error") or {}).get("attribution") or "unknown")
        for row in rows
        if (row.get("error") or {}).get("attribution") != "none"
    )
    recommendations = []
    if rows and routing_success / len(rows) < 0.7:
        recommendations.append("replay weak task scopes before promoting a routing change")
    if errors.get("instruction", 0) or errors.get("context", 0):
        recommendations.append("tighten work-package instructions or context projection")
    if any((row.get("scores") or {}).get("efficiency", 5) <= 2 for row in rows):
        recommendations.append("recalibrate token forecasts for inefficient scopes")
    if not recommendations:
        recommendations.append("retain the current policy and continue observation")
    return {
        "schema_version": RETROSPECTIVE_SCHEMA,
        "id": new_id("RETRO"),
        "timestamp": now_iso(),
        "project_id": project.get("project_id"),
        "project_name": project.get("name"),
        "task_count": len(task_rows),
        "attempt_evaluation_count": len(rows),
        "accepted_count": accepted,
        "routing_success_count": routing_success,
        "estimated_cost_cny": _money_sum(rows),
        "error_attribution": dict(sorted(errors.items())),
        "recommendations": recommendations,
        "evidence_result_ids": result_ids,
        "evidence_sha256": evidence_hash,
    }


def append_retrospective_and_candidate(
    layout: ProjectLayout, retrospective: dict[str, Any]
) -> tuple[bool, bool]:
    retrospectives = read_jsonl(layout.retrospectives_jsonl)
    if any(
        row.get("evidence_sha256") == retrospective.get("evidence_sha256")
        for row in retrospectives
    ):
        return False, False
    append_jsonl(layout.retrospectives_jsonl, retrospective)
    candidate = {
        "schema_version": POLICY_CANDIDATE_SCHEMA,
        "id": new_id("POL"),
        "timestamp": now_iso(),
        "project_id": retrospective.get("project_id"),
        "state": "candidate",
        "allowed_transitions": ["replayed", "deprecated"],
        "automatic_activation": False,
        "policy_changes": {
            "auto_teaching_floor": (
                "review"
                if any(
                    "replay weak" in recommendation
                    or "tighten work-package" in recommendation
                    for recommendation in retrospective.get("recommendations") or []
                )
                else "off"
            ),
            "scope": {"project_id": retrospective.get("project_id")},
        },
        "recommendations": retrospective.get("recommendations") or [],
        "source_retrospective_id": retrospective.get("id"),
        "evidence_sha256": retrospective.get("evidence_sha256"),
        "promotion_policy": (
            "candidate -> replayed -> shadow -> canary -> active; "
            "explicit review is required at every transition"
        ),
    }
    append_jsonl(layout.policy_candidates_jsonl, candidate)
    return True, True


def latest_policy_candidates(layout: ProjectLayout) -> list[dict[str, Any]]:
    latest: dict[str, dict[str, Any]] = {}
    for row in read_jsonl(layout.policy_candidates_jsonl):
        candidate_id = str(row.get("candidate_id") or row.get("id") or "")
        if candidate_id:
            latest[candidate_id] = row
    return [latest[candidate_id] for candidate_id in sorted(latest)]


def transition_policy_candidate(
    layout: ProjectLayout,
    *,
    candidate_id: str,
    to_state: str,
    evidence: str,
    approved_by: str,
    apply: bool,
) -> dict[str, Any]:
    latest = {
        str(row.get("candidate_id") or row.get("id") or ""): row
        for row in latest_policy_candidates(layout)
    }
    current = latest.get(candidate_id)
    if current is None:
        raise EvolutionError(f"policy candidate not found: {candidate_id}")
    current_state = str(current.get("state") or "")
    allowed = POLICY_TRANSITIONS.get(current_state)
    if allowed is None or to_state not in allowed:
        raise EvolutionError(
            f"policy transition {current_state} -> {to_state} is not allowed"
        )
    evidence_text = str(evidence or "").strip()
    approver = str(approved_by or "").strip()
    if not evidence_text or not approver:
        raise EvolutionError("policy promotion requires evidence and an approver")
    event = {
        "schema_version": POLICY_CANDIDATE_SCHEMA,
        "id": new_id("POL-EVENT"),
        "event_type": "policy_transition",
        "candidate_id": candidate_id,
        "timestamp": now_iso(),
        "project_id": current.get("project_id"),
        "state": to_state,
        "previous_state": current_state,
        "allowed_transitions": list(POLICY_TRANSITIONS[to_state]),
        "automatic_activation": False,
        "approved_by": approver,
        "evidence": evidence_text,
        "policy_changes": current.get("policy_changes") or {},
        "source_retrospective_id": current.get("source_retrospective_id"),
        "source_candidate_event_id": current.get("id"),
        "evidence_sha256": canonical_sha256(
            {
                "candidate_id": candidate_id,
                "previous_state": current_state,
                "state": to_state,
                "approved_by": approver,
                "evidence": evidence_text,
                "policy_changes": current.get("policy_changes") or {},
            }
        ),
    }
    if apply:
        append_jsonl(layout.policy_candidates_jsonl, event)
    return {"applied": bool(apply), "transition": event}


def active_policy_effects(layout: ProjectLayout) -> dict[str, Any]:
    policies = [
        row
        for row in latest_policy_candidates(layout)
        if row.get("state") == "active"
    ]
    teaching_floors = [
        str((row.get("policy_changes") or {}).get("auto_teaching_floor") or "off")
        for row in policies
    ]
    return {
        "active_policy_ids": [
            str(row.get("candidate_id") or row.get("id")) for row in policies
        ],
        "auto_teaching_floor": (
            "review" if "review" in teaching_floors else "off"
        ),
    }


__all__ = [
    "ERROR_ATTRIBUTIONS",
    "EVALUATION_SCHEMA",
    "EvolutionError",
    "MODEL_MEMORY_SCHEMA",
    "POLICY_LIFECYCLE",
    "RETROSPECTIVE_SCHEMA",
    "TEACHING_MODES",
    "append_evaluation",
    "append_retrospective_and_candidate",
    "active_policy_effects",
    "build_attempt_evaluation",
    "build_model_memory",
    "build_project_retrospective",
    "choose_teaching_mode",
    "latest_policy_candidates",
    "profile_for_task",
    "transition_policy_candidate",
]
