"""Governed teaching execution graphs and evidence-bound run records."""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping
from typing import Any

from .quality import canonical_sha256
from .state import new_id, now_iso


TEACHING_GRAPH_SCHEMA = "costmarshal-teaching-execution-graph-v1"
TEACHING_RUN_SCHEMA = "costmarshal-teaching-run-v1"
TEACHING_GRAPH_MODES = frozenset({"review", "paired", "replay"})


class TeachingError(ValueError):
    """Raised when teaching evidence does not satisfy its execution graph."""


_NODE_BLUEPRINTS: dict[str, tuple[tuple[str, str, tuple[str, ...]], ...]] = {
    "review": (
        ("candidate_execution", "result", ()),
        ("independent_review", "reviewer-result", ("candidate_execution",)),
        ("acceptance_gate", "gate", ("independent_review",)),
    ),
    "paired": (
        ("completion_a", "result", ()),
        ("completion_b", "result", ()),
        (
            "comparison_review",
            "reviewer-result",
            ("completion_a", "completion_b"),
        ),
        ("acceptance_gate", "gate", ("comparison_review",)),
    ),
    "replay": (
        ("baseline_execution", "result", ()),
        ("replay_execution", "result", ("baseline_execution",)),
        ("fixed_gate", "gate", ("replay_execution",)),
    ),
}


def _json_copy(value: Any) -> Any:
    return json.loads(
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    )


def build_teaching_execution_graph(
    *,
    project_id: str,
    task_id: str,
    mode: str,
    task_input: Mapping[str, Any],
) -> dict[str, Any] | None:
    """Build a deterministic graph; off mode intentionally has no graph."""

    if mode == "off":
        return None
    if mode not in TEACHING_GRAPH_MODES:
        raise TeachingError(f"unsupported teaching graph mode: {mode}")
    input_copy = _json_copy(dict(task_input))
    nodes = [
        {
            "node_id": node_id,
            "evidence_kind": evidence_kind,
            "depends_on": list(depends_on),
        }
        for node_id, evidence_kind, depends_on in _NODE_BLUEPRINTS[mode]
    ]
    body = {
        "schema_version": TEACHING_GRAPH_SCHEMA,
        "project_id": project_id,
        "task_id": task_id,
        "mode": mode,
        "input_sha256": canonical_sha256(input_copy),
        "nodes": nodes,
    }
    digest = canonical_sha256(body)
    return {
        **body,
        "graph_id": "TGRAPH-" + digest.removeprefix("sha256:")[:32],
        "graph_sha256": digest,
    }


def validate_teaching_execution_graph(value: Mapping[str, Any]) -> dict[str, Any]:
    row = _json_copy(dict(value))
    if row.get("schema_version") != TEACHING_GRAPH_SCHEMA:
        raise TeachingError("unsupported teaching execution graph schema")
    mode = row.get("mode")
    if mode not in TEACHING_GRAPH_MODES:
        raise TeachingError("teaching graph mode is invalid")
    expected_nodes = [
        {
            "node_id": node_id,
            "evidence_kind": evidence_kind,
            "depends_on": list(depends_on),
        }
        for node_id, evidence_kind, depends_on in _NODE_BLUEPRINTS[str(mode)]
    ]
    if row.get("nodes") != expected_nodes:
        raise TeachingError("teaching graph topology is invalid")
    body = {
        key: item
        for key, item in row.items()
        if key not in {"graph_id", "graph_sha256"}
    }
    digest = canonical_sha256(body)
    if (
        row.get("graph_sha256") != digest
        or row.get("graph_id")
        != "TGRAPH-" + digest.removeprefix("sha256:")[:32]
    ):
        raise TeachingError("teaching graph hash binding is invalid")
    return row


def _indexed(rows: Iterable[Mapping[str, Any]], key: str) -> dict[str, dict[str, Any]]:
    return {
        str(row[key]): _json_copy(dict(row))
        for row in rows
        if isinstance(row, Mapping)
        and isinstance(row.get(key), str)
        and row.get(key)
    }


def _result_identity(result: Mapping[str, Any]) -> tuple[str, str, str]:
    return (
        str(result.get("provider") or ""),
        str(result.get("execution_model") or result.get("model") or "inherit"),
        str(result.get("profile_sha256") or result.get("profile") or ""),
    )


def _result_scope(result: Mapping[str, Any]) -> tuple[str, str]:
    return (
        str(result.get("task_type") or "unknown"),
        str(result.get("difficulty") or "normal"),
    )


def _binding_map(
    graph: Mapping[str, Any],
    bindings: Mapping[str, str],
) -> dict[str, str]:
    expected = {str(node["node_id"]) for node in graph["nodes"]}
    supplied = set(bindings)
    if supplied != expected:
        missing = sorted(expected - supplied)
        extra = sorted(supplied - expected)
        detail = []
        if missing:
            detail.append("missing " + ", ".join(missing))
        if extra:
            detail.append("unknown " + ", ".join(extra))
        raise TeachingError("teaching node bindings are incomplete: " + "; ".join(detail))
    normalized: dict[str, str] = {}
    for node_id, evidence_id in bindings.items():
        if not isinstance(evidence_id, str) or not evidence_id.strip():
            raise TeachingError(f"teaching node {node_id} requires an evidence id")
        normalized[node_id] = evidence_id.strip()
    return normalized


def build_teaching_run(
    *,
    project_id: str,
    task: Mapping[str, Any],
    bindings: Mapping[str, str],
    tasks: Iterable[Mapping[str, Any]],
    results: Iterable[Mapping[str, Any]],
    gates: Iterable[Mapping[str, Any]],
    conclusion: str,
) -> dict[str, Any]:
    teaching = task.get("teaching") or {}
    graph = validate_teaching_execution_graph(
        teaching.get("execution_graph") or {}
    )
    if graph.get("task_id") != task.get("id") or graph.get("project_id") != project_id:
        raise TeachingError("teaching graph is not bound to this project/task")
    node_bindings = _binding_map(graph, bindings)
    task_index = _indexed(tasks, "id")
    result_index = _indexed(results, "id")
    gate_index = _indexed(gates, "id")
    evidence: dict[str, dict[str, Any]] = {}
    for node in graph["nodes"]:
        node_id = str(node["node_id"])
        evidence_id = node_bindings[node_id]
        kind = str(node["evidence_kind"])
        if kind in {"result", "reviewer-result"}:
            row = result_index.get(evidence_id)
            if row is None:
                raise TeachingError(f"{node_id} references missing result {evidence_id}")
            if row.get("project_id") != project_id:
                raise TeachingError(f"{node_id} result belongs to another project")
            if type(row.get("accepted_by_leader")) is not bool:
                raise TeachingError(f"{node_id} lacks an explicit Leader decision")
            if kind == "reviewer-result":
                source_task = task_index.get(str(row.get("task_id") or ""))
                if source_task is None or source_task.get("role") != "reviewer":
                    raise TeachingError(
                        f"{node_id} must bind a result from a reviewer work package"
                    )
            evidence[node_id] = {
                "kind": kind,
                "id": evidence_id,
                "sha256": canonical_sha256(row),
                "task_id": row.get("task_id"),
                "attempt_id": row.get("attempt_id"),
            }
        else:
            row = gate_index.get(evidence_id)
            if row is None:
                raise TeachingError(f"{node_id} references missing gate {evidence_id}")
            if row.get("project_id") != project_id:
                raise TeachingError(f"{node_id} gate belongs to another project")
            if row.get("passed") is not True:
                raise TeachingError(f"{node_id} requires a passing gate")
            evidence[node_id] = {
                "kind": "gate",
                "id": evidence_id,
                "sha256": canonical_sha256(row),
                "result_id": row.get("result_id"),
            }

    mode = str(graph["mode"])
    if mode == "review":
        candidate = result_index[node_bindings["candidate_execution"]]
        review = result_index[node_bindings["independent_review"]]
        gate = gate_index[node_bindings["acceptance_gate"]]
        if candidate["id"] == review["id"]:
            raise TeachingError("independent review must be a distinct result")
        if _result_scope(candidate) != _result_scope(review):
            raise TeachingError("review evidence must match the candidate task scope")
        if gate.get("result_id") != review.get("id"):
            raise TeachingError("review acceptance gate must bind the reviewer result")
    elif mode == "paired":
        first = result_index[node_bindings["completion_a"]]
        second = result_index[node_bindings["completion_b"]]
        comparison = result_index[node_bindings["comparison_review"]]
        gate = gate_index[node_bindings["acceptance_gate"]]
        if first["id"] == second["id"]:
            raise TeachingError("paired completions must be distinct results")
        if _result_identity(first) == _result_identity(second):
            raise TeachingError("paired completions require distinct execution identities")
        if _result_scope(first) != _result_scope(second):
            raise TeachingError("paired completions must share one task scope")
        if _result_scope(first) != _result_scope(comparison):
            raise TeachingError("paired comparison must share the completion task scope")
        if gate.get("result_id") != comparison.get("id"):
            raise TeachingError("paired acceptance gate must bind the comparison result")
    else:
        baseline = result_index[node_bindings["baseline_execution"]]
        replay = result_index[node_bindings["replay_execution"]]
        gate = gate_index[node_bindings["fixed_gate"]]
        if baseline["id"] == replay["id"]:
            raise TeachingError("replay requires distinct baseline and replay results")
        if _result_identity(baseline) != _result_identity(replay):
            raise TeachingError("replay must hold the execution identity fixed")
        if _result_scope(baseline) != _result_scope(replay):
            raise TeachingError("replay must hold the task scope fixed")
        if gate.get("result_id") != replay.get("id"):
            raise TeachingError("fixed replay gate must bind the replay result")

    conclusion_text = str(conclusion or "").strip()
    if not conclusion_text:
        raise TeachingError("teaching run requires a concise conclusion")
    material = {
        "schema_version": TEACHING_RUN_SCHEMA,
        "project_id": project_id,
        "task_id": task.get("id"),
        "mode": mode,
        "graph_id": graph["graph_id"],
        "graph_sha256": graph["graph_sha256"],
        "bindings": evidence,
        "conclusion": conclusion_text,
    }
    digest = canonical_sha256(material)
    return {
        **material,
        "run_id": "TEACH-" + digest.removeprefix("sha256:")[:32],
        "recorded_at": now_iso(),
        "run_sha256": digest,
    }


def validate_teaching_run(
    value: Mapping[str, Any],
    *,
    task: Mapping[str, Any],
    tasks: Iterable[Mapping[str, Any]],
    results: Iterable[Mapping[str, Any]],
    gates: Iterable[Mapping[str, Any]],
) -> dict[str, Any]:
    row = _json_copy(dict(value))
    if row.get("schema_version") != TEACHING_RUN_SCHEMA:
        raise TeachingError("unsupported teaching run schema")
    rebuilt = build_teaching_run(
        project_id=str(row.get("project_id") or ""),
        task=task,
        bindings={
            str(node_id): str(binding.get("id") or "")
            for node_id, binding in (row.get("bindings") or {}).items()
            if isinstance(binding, Mapping)
        },
        tasks=tasks,
        results=results,
        gates=gates,
        conclusion=str(row.get("conclusion") or ""),
    )
    for field in (
        "run_id",
        "run_sha256",
        "graph_id",
        "graph_sha256",
        "bindings",
    ):
        if row.get(field) != rebuilt.get(field):
            raise TeachingError(f"teaching run {field} binding is invalid")
    return row


__all__ = [
    "TEACHING_GRAPH_MODES",
    "TEACHING_GRAPH_SCHEMA",
    "TEACHING_RUN_SCHEMA",
    "TeachingError",
    "build_teaching_execution_graph",
    "build_teaching_run",
    "validate_teaching_execution_graph",
    "validate_teaching_run",
]
