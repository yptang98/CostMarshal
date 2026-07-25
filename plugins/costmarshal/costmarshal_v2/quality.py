"""Artifact registry and deterministic acceptance gates."""

from __future__ import annotations

import hashlib
import json
from typing import Any, Iterable

from .paths import ProjectLayout
from .state import append_jsonl, new_id, now_iso, read_jsonl


ARTIFACT_SCHEMA = "costmarshal-artifact-event-v1"
GATE_RESULT_SCHEMA = "costmarshal-gate-result-v1"
ARTIFACT_LIFECYCLES = frozenset({"candidate", "accepted", "rejected", "superseded"})


class GateError(ValueError):
    """Raised when a gate specification is invalid."""


def canonical_sha256(value: Any) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def build_artifact_event(
    *,
    project_id: str,
    task_id: str,
    attempt_id: str,
    result_id: str,
    kind: str,
    path: str,
    sha256: str,
    size: int,
    lifecycle: str = "candidate",
) -> dict[str, Any]:
    if lifecycle not in ARTIFACT_LIFECYCLES:
        raise GateError(f"invalid artifact lifecycle: {lifecycle}")
    if not isinstance(sha256, str) or len(sha256) != 64:
        raise GateError("artifact sha256 must be a lowercase hexadecimal digest")
    try:
        int(sha256, 16)
    except ValueError as exc:
        raise GateError("artifact sha256 must be a lowercase hexadecimal digest") from exc
    if type(size) is not int or size < 0:
        raise GateError("artifact size must be a non-negative integer")
    artifact_id = f"ART-{sha256}"
    event_material = {
        "artifact_id": artifact_id,
        "task_id": task_id,
        "attempt_id": attempt_id,
        "result_id": result_id,
        "kind": kind,
        "lifecycle": lifecycle,
    }
    return {
        "schema_version": ARTIFACT_SCHEMA,
        "event_id": "AEV-" + canonical_sha256(event_material).removeprefix("sha256:")[:24],
        "timestamp": now_iso(),
        "project_id": project_id,
        "artifact_id": artifact_id,
        "task_id": task_id,
        "attempt_id": attempt_id,
        "result_id": result_id,
        "kind": kind,
        "path": path,
        "sha256": sha256,
        "size": size,
        "lifecycle": lifecycle,
    }


def append_artifact_event(layout: ProjectLayout, event: dict[str, Any]) -> bool:
    existing = read_jsonl(layout.artifacts_jsonl)
    if any(row.get("event_id") == event.get("event_id") for row in existing):
        return False
    append_jsonl(layout.artifacts_jsonl, event)
    return True


def default_gate_spec(
    *,
    min_quality_score: int = 1,
    max_error_severity: int = 5,
    required_artifact_kinds: Iterable[str] = ("completion-report",),
) -> dict[str, Any]:
    kinds = []
    for raw in required_artifact_kinds:
        kind = str(raw).strip()
        if kind and kind not in kinds:
            kinds.append(kind)
    spec = {
        "schema_version": "costmarshal-gate-spec-v1",
        "min_quality_score": min_quality_score,
        "max_error_severity": max_error_severity,
        "required_artifact_kinds": kinds,
        "require_dependencies_accepted": True,
        "require_leader_acceptance": True,
        "require_teaching_evidence_when_active": True,
    }
    validate_gate_spec(spec)
    return spec


def validate_gate_spec(spec: dict[str, Any]) -> None:
    if not isinstance(spec, dict) or spec.get("schema_version") != "costmarshal-gate-spec-v1":
        raise GateError("invalid gate specification schema")
    quality = spec.get("min_quality_score")
    severity = spec.get("max_error_severity")
    if type(quality) is not int or quality not in {1, 2, 3, 4, 5}:
        raise GateError("minimum quality score must be 1-5")
    if type(severity) is not int or severity not in {0, 1, 2, 3, 4, 5}:
        raise GateError("maximum error severity must be 0-5")
    kinds = spec.get("required_artifact_kinds")
    if not isinstance(kinds, list) or any(
        not isinstance(kind, str) or not kind.strip() for kind in kinds
    ):
        raise GateError("required artifact kinds must be non-empty strings")
    if len(kinds) != len(set(kinds)):
        raise GateError("required artifact kinds must be unique")
    for flag in (
        "require_dependencies_accepted",
        "require_leader_acceptance",
        "require_teaching_evidence_when_active",
    ):
        if type(spec.get(flag)) is not bool:
            raise GateError(f"{flag} must be boolean")


def evaluate_gates(
    *,
    project_id: str,
    task: dict[str, Any],
    result: dict[str, Any],
    dependency_states: dict[str, str],
    artifact_events: Iterable[dict[str, Any]],
    error_severity: int,
    teaching_evidence: str | None,
) -> dict[str, Any]:
    spec = task.get("gates") or default_gate_spec()
    validate_gate_spec(spec)
    checks: list[dict[str, Any]] = []

    def check(name: str, passed: bool, observed: Any, expected: Any) -> None:
        checks.append(
            {
                "name": name,
                "passed": bool(passed),
                "observed": observed,
                "expected": expected,
            }
        )

    dependencies = list(task.get("dependencies") or [])
    blocked_dependencies = [
        dependency
        for dependency in dependencies
        if dependency_states.get(dependency) != "accepted"
    ]
    check(
        "dependencies_accepted",
        not spec["require_dependencies_accepted"] or not blocked_dependencies,
        {dependency: dependency_states.get(dependency, "missing") for dependency in dependencies},
        "all accepted",
    )
    check(
        "leader_acceptance",
        not spec["require_leader_acceptance"]
        or result.get("accepted_by_leader") is True,
        result.get("accepted_by_leader"),
        True,
    )
    check(
        "minimum_quality",
        int(result.get("quality_score") or 0) >= spec["min_quality_score"],
        int(result.get("quality_score") or 0),
        f">={spec['min_quality_score']}",
    )
    check(
        "maximum_error_severity",
        error_severity <= spec["max_error_severity"],
        error_severity,
        f"<={spec['max_error_severity']}",
    )
    observed_kinds = sorted(
        {
            str(event.get("kind"))
            for event in artifact_events
            if event.get("task_id") == task.get("id")
            and event.get("result_id") == result.get("id")
        }
    )
    required_kinds = list(spec["required_artifact_kinds"])
    check(
        "required_artifacts",
        all(kind in observed_kinds for kind in required_kinds),
        observed_kinds,
        required_kinds,
    )
    teaching = task.get("teaching") or {"mode": "off"}
    teaching_active = (
        teaching.get("mode") not in {None, "off"}
        and teaching.get("enforcement") == "required"
    )
    check(
        "teaching_evidence",
        not spec["require_teaching_evidence_when_active"]
        or not teaching_active
        or bool(str(teaching_evidence or "").strip()),
        bool(str(teaching_evidence or "").strip()),
        True if teaching_active else "not required",
    )
    passed = all(item["passed"] for item in checks)
    material = {
        "task_id": task.get("id"),
        "result_id": result.get("id"),
        "spec": spec,
        "checks": checks,
    }
    return {
        "schema_version": GATE_RESULT_SCHEMA,
        "id": new_id("GATE"),
        "timestamp": now_iso(),
        "project_id": project_id,
        "task_id": task.get("id"),
        "attempt_id": result.get("attempt_id"),
        "result_id": result.get("id"),
        "passed": passed,
        "checks": checks,
        "evidence_sha256": canonical_sha256(material),
    }


def append_gate_result(layout: ProjectLayout, result: dict[str, Any]) -> bool:
    existing = read_jsonl(layout.gate_results_jsonl)
    if any(row.get("result_id") == result.get("result_id") for row in existing):
        return False
    append_jsonl(layout.gate_results_jsonl, result)
    return True


__all__ = [
    "ARTIFACT_LIFECYCLES",
    "ARTIFACT_SCHEMA",
    "GATE_RESULT_SCHEMA",
    "GateError",
    "append_artifact_event",
    "append_gate_result",
    "build_artifact_event",
    "canonical_sha256",
    "default_gate_spec",
    "evaluate_gates",
    "validate_gate_spec",
]
