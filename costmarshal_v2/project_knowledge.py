"""Accepted-evidence-only project knowledge and reusable Skill candidates."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Iterable, Mapping
from typing import Any

from .project_artifacts import PROJECT_ARTIFACT_SCHEMA
from .quality import ARTIFACT_SCHEMA
from .state import now_iso


LEADER_DECISION_SCHEMA = "costmarshal-leader-decision-v1"
PROJECT_KNOWLEDGE_SCHEMA = "costmarshal-project-knowledge-v1"
SKILL_CANDIDATE_SCHEMA = "costmarshal-skill-candidate-v1"
KNOWLEDGE_KINDS = frozenset(
    {
        "charter",
        "architecture",
        "adr",
        "interface",
        "accepted-fact",
        "risk",
        "milestone-summary",
    }
)
_ID_RE = re.compile(r"[A-Za-z][A-Za-z0-9_.:-]{2,191}\Z")


class ProjectKnowledgeError(ValueError):
    """Raised when unaccepted evidence is promoted into durable knowledge."""


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
        raise ProjectKnowledgeError("knowledge metadata must be canonical JSON") from exc


def _digest(value: object) -> str:
    return "sha256:" + hashlib.sha256(_canonical_bytes(value)).hexdigest()


def _bounded_text(value: object, label: str, *, limit: int = 16_384) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ProjectKnowledgeError(f"{label} must be non-empty text")
    if len(value.encode("utf-8")) > limit:
        raise ProjectKnowledgeError(f"{label} exceeds its byte limit")
    if any(
        (ord(character) < 32 and character not in {"\n", "\t"})
        or ord(character) == 127
        for character in value
    ):
        raise ProjectKnowledgeError(f"{label} contains forbidden control characters")
    return value.strip()


def build_leader_decision(
    *,
    project_id: str,
    command_id: str,
    statement: str,
    rationale: str,
    task_id: str | None = None,
) -> dict[str, Any]:
    body = {
        "schema_version": LEADER_DECISION_SCHEMA,
        "project_id": _bounded_text(project_id, "project id", limit=256),
        "command_id": _bounded_text(command_id, "command id", limit=256),
        "task_id": task_id,
        "statement": _bounded_text(statement, "decision statement"),
        "rationale": _bounded_text(rationale, "decision rationale"),
        "accepted": True,
    }
    identity_digest = _digest(body)
    record = {
        **body,
        "decision_id": "LDEC-" + identity_digest.removeprefix("sha256:")[:32],
        "timestamp": now_iso(),
    }
    return {**record, "decision_sha256": _digest(record)}


def validate_leader_decision(value: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(value, Mapping) or value.get("schema_version") != LEADER_DECISION_SCHEMA:
        raise ProjectKnowledgeError("unsupported Leader decision schema")
    row = json.loads(_canonical_bytes(dict(value)))
    expected = {
        "schema_version",
        "project_id",
        "command_id",
        "task_id",
        "statement",
        "rationale",
        "accepted",
        "decision_id",
        "timestamp",
        "decision_sha256",
    }
    if set(row) != expected or row.get("accepted") is not True:
        raise ProjectKnowledgeError("Leader decision fields are invalid")
    body = {
        key: row[key]
        for key in (
            "schema_version",
            "project_id",
            "command_id",
            "task_id",
            "statement",
            "rationale",
            "accepted",
        )
    }
    identity_digest = _digest(body)
    if (
        row.get("decision_sha256")
        != _digest({key: value for key, value in row.items() if key != "decision_sha256"})
        or row.get("decision_id")
        != "LDEC-" + identity_digest.removeprefix("sha256:")[:32]
    ):
        raise ProjectKnowledgeError("Leader decision hash binding is invalid")
    return row


def _latest_artifact_lifecycle(
    artifact_rows: Iterable[Mapping[str, Any]],
) -> tuple[dict[str, str], dict[str, str | None]]:
    lifecycle: dict[str, str] = {}
    task_ids: dict[str, str | None] = {}
    for row in artifact_rows:
        if row.get("schema_version") not in {
            ARTIFACT_SCHEMA,
            PROJECT_ARTIFACT_SCHEMA,
        }:
            continue
        artifact_id = row.get("artifact_id")
        if isinstance(artifact_id, str) and artifact_id:
            lifecycle[artifact_id] = str(row.get("lifecycle") or "")
            task_id = row.get("task_id")
            task_ids[artifact_id] = str(task_id) if task_id else None
    return lifecycle, task_ids


def require_accepted_artifacts(
    artifact_ids: Iterable[str],
    artifact_rows: Iterable[Mapping[str, Any]],
    *,
    minimum: int = 1,
    distinct_tasks: bool = False,
) -> list[str]:
    sources = list(dict.fromkeys(str(item) for item in artifact_ids if str(item)))
    if len(sources) < minimum:
        raise ProjectKnowledgeError(
            f"at least {minimum} accepted source artifact(s) are required"
        )
    lifecycle, task_ids = _latest_artifact_lifecycle(artifact_rows)
    invalid = [item for item in sources if lifecycle.get(item) != "accepted"]
    if invalid:
        raise ProjectKnowledgeError(
            "knowledge sources are missing or not accepted: " + ", ".join(invalid)
        )
    if distinct_tasks:
        tasks = {task_ids.get(item) for item in sources if task_ids.get(item)}
        if len(tasks) < minimum:
            raise ProjectKnowledgeError(
                f"Skill candidate requires accepted evidence from {minimum} distinct tasks"
            )
    return sources


def build_project_knowledge(
    *,
    project_id: str,
    command_id: str,
    kind: str,
    title: str,
    source_artifact_ids: Iterable[str],
    artifact_rows: Iterable[Mapping[str, Any]],
    leader_decision_id: str | None,
    leader_decisions: Iterable[Mapping[str, Any]],
    task_id: str | None = None,
) -> dict[str, Any]:
    if kind not in KNOWLEDGE_KINDS:
        raise ProjectKnowledgeError(f"unsupported knowledge kind: {kind}")
    sources = list(
        dict.fromkeys(str(item) for item in source_artifact_ids if str(item))
    )
    if sources:
        sources = require_accepted_artifacts(sources, artifact_rows)
    decisions = {
        str(row.get("decision_id")): validate_leader_decision(row)
        for row in leader_decisions
    }
    if leader_decision_id is not None:
        decision = decisions.get(leader_decision_id)
        if decision is None or decision.get("project_id") != project_id:
            raise ProjectKnowledgeError(
                "knowledge references a missing or foreign Leader decision"
            )
    if not sources and leader_decision_id is None:
        raise ProjectKnowledgeError(
            "knowledge requires accepted Artifact evidence or an explicit Leader decision"
        )
    body = {
        "schema_version": PROJECT_KNOWLEDGE_SCHEMA,
        "project_id": project_id,
        "command_id": command_id,
        "kind": kind,
        "title": _bounded_text(title, "knowledge title", limit=512),
        "task_id": task_id,
        "source_artifact_ids": sources,
        "leader_decision_id": leader_decision_id,
        "status": "accepted",
    }
    identity_digest = _digest(body)
    record = {
        **body,
        "knowledge_id": "KNOW-" + identity_digest.removeprefix("sha256:")[:32],
        "timestamp": now_iso(),
    }
    return {**record, "knowledge_sha256": _digest(record)}


def validate_project_knowledge(
    value: Mapping[str, Any],
    *,
    artifact_rows: Iterable[Mapping[str, Any]],
    leader_decisions: Iterable[Mapping[str, Any]],
) -> dict[str, Any]:
    if not isinstance(value, Mapping) or value.get("schema_version") != PROJECT_KNOWLEDGE_SCHEMA:
        raise ProjectKnowledgeError("unsupported project knowledge schema")
    row = json.loads(_canonical_bytes(dict(value)))
    expected = {
        "schema_version",
        "project_id",
        "command_id",
        "kind",
        "title",
        "task_id",
        "source_artifact_ids",
        "leader_decision_id",
        "status",
        "knowledge_id",
        "timestamp",
        "knowledge_sha256",
    }
    if set(row) != expected or row.get("status") != "accepted":
        raise ProjectKnowledgeError("project knowledge fields are invalid")
    body = {
        key: row[key]
        for key in (
            "schema_version",
            "project_id",
            "command_id",
            "kind",
            "title",
            "task_id",
            "source_artifact_ids",
            "leader_decision_id",
            "status",
        )
    }
    identity_digest = _digest(body)
    if (
        row.get("knowledge_id")
        != "KNOW-" + identity_digest.removeprefix("sha256:")[:32]
        or row.get("knowledge_sha256")
        != _digest({key: value for key, value in row.items() if key != "knowledge_sha256"})
    ):
        raise ProjectKnowledgeError("project knowledge hash binding is invalid")
    build_project_knowledge(
        project_id=str(row.get("project_id") or ""),
        command_id=str(row.get("command_id") or ""),
        kind=str(row.get("kind") or ""),
        title=str(row.get("title") or ""),
        source_artifact_ids=row.get("source_artifact_ids") or [],
        artifact_rows=artifact_rows,
        leader_decision_id=row.get("leader_decision_id"),
        leader_decisions=leader_decisions,
        task_id=row.get("task_id"),
    )
    return row


def build_skill_candidate(
    *,
    project_id: str,
    command_id: str,
    name: str,
    artifact_id: str,
    source_artifact_ids: Iterable[str],
    artifact_rows: Iterable[Mapping[str, Any]],
    applicability: str,
    inputs: Iterable[str],
    steps: Iterable[str],
    verification: Iterable[str],
    failure_boundaries: Iterable[str],
) -> dict[str, Any]:
    sources = require_accepted_artifacts(
        source_artifact_ids,
        artifact_rows,
        minimum=2,
        distinct_tasks=True,
    )
    list_fields: dict[str, list[str]] = {}
    for field, values in (
        ("inputs", inputs),
        ("steps", steps),
        ("verification", verification),
        ("failure_boundaries", failure_boundaries),
    ):
        normalized = [
            _bounded_text(value, f"Skill candidate {field}", limit=2048)
            for value in values
        ]
        if not normalized or len(normalized) > 32:
            raise ProjectKnowledgeError(
                f"Skill candidate {field} must contain 1..32 items"
            )
        list_fields[field] = normalized
    body = {
        "schema_version": SKILL_CANDIDATE_SCHEMA,
        "project_id": project_id,
        "command_id": command_id,
        "name": _bounded_text(name, "Skill candidate name", limit=128),
        "artifact_id": artifact_id,
        "source_artifact_ids": sources,
        "applicability": _bounded_text(
            applicability, "Skill candidate applicability", limit=4096
        ),
        **list_fields,
        "scope": "project-local",
        "global_install_authorized": False,
    }
    identity_digest = _digest(body)
    record = {
        **body,
        "candidate_id": "SKC-" + identity_digest.removeprefix("sha256:")[:32],
        "timestamp": now_iso(),
    }
    return {**record, "candidate_sha256": _digest(record)}


def validate_skill_candidate(
    value: Mapping[str, Any],
    *,
    artifact_rows: Iterable[Mapping[str, Any]],
) -> dict[str, Any]:
    if not isinstance(value, Mapping) or value.get("schema_version") != SKILL_CANDIDATE_SCHEMA:
        raise ProjectKnowledgeError("unsupported Skill candidate schema")
    row = json.loads(_canonical_bytes(dict(value)))
    expected = {
        "schema_version",
        "project_id",
        "command_id",
        "name",
        "artifact_id",
        "source_artifact_ids",
        "applicability",
        "inputs",
        "steps",
        "verification",
        "failure_boundaries",
        "scope",
        "global_install_authorized",
        "candidate_id",
        "timestamp",
        "candidate_sha256",
    }
    if (
        set(row) != expected
        or row.get("scope") != "project-local"
        or row.get("global_install_authorized") is not False
    ):
        raise ProjectKnowledgeError("Skill candidate fields are invalid")
    body = {
        key: row[key]
        for key in (
            "schema_version",
            "project_id",
            "command_id",
            "name",
            "artifact_id",
            "source_artifact_ids",
            "applicability",
            "inputs",
            "steps",
            "verification",
            "failure_boundaries",
            "scope",
            "global_install_authorized",
        )
    }
    identity_digest = _digest(body)
    if (
        row.get("candidate_id")
        != "SKC-" + identity_digest.removeprefix("sha256:")[:32]
        or row.get("candidate_sha256")
        != _digest({key: value for key, value in row.items() if key != "candidate_sha256"})
    ):
        raise ProjectKnowledgeError("Skill candidate hash binding is invalid")
    build_skill_candidate(
        project_id=str(row.get("project_id") or ""),
        command_id=str(row.get("command_id") or ""),
        name=str(row.get("name") or ""),
        artifact_id=str(row.get("artifact_id") or ""),
        source_artifact_ids=row.get("source_artifact_ids") or [],
        artifact_rows=artifact_rows,
        applicability=str(row.get("applicability") or ""),
        inputs=row.get("inputs") or [],
        steps=row.get("steps") or [],
        verification=row.get("verification") or [],
        failure_boundaries=row.get("failure_boundaries") or [],
    )
    return row


__all__ = [
    "KNOWLEDGE_KINDS",
    "LEADER_DECISION_SCHEMA",
    "PROJECT_KNOWLEDGE_SCHEMA",
    "SKILL_CANDIDATE_SCHEMA",
    "ProjectKnowledgeError",
    "build_leader_decision",
    "build_project_knowledge",
    "build_skill_candidate",
    "require_accepted_artifacts",
    "validate_leader_decision",
    "validate_project_knowledge",
    "validate_skill_candidate",
]
