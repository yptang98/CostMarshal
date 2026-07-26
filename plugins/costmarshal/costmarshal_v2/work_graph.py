"""Durable dependency graph for bounded CostMarshal work packages."""

from __future__ import annotations

from collections import deque
from typing import Any, Iterable

from .paths import ProjectLayout
from .state import atomic_write_json, now_iso, read_json


WORK_GRAPH_SCHEMA = "costmarshal-work-graph-v1"
WORK_ROLES = frozenset({"scout", "builder", "reviewer", "expert"})
NODE_STATES = frozenset(
    {
        "blocked",
        "ready",
        "dispatched",
        "running",
        "waiting_leader",
        "accepted",
        "rejected",
        "escalate",
        "cancelled",
    }
)


class WorkGraphError(ValueError):
    """Raised when graph structure or a dependency transition is unsafe."""


def empty_work_graph() -> dict[str, Any]:
    return {
        "schema_version": WORK_GRAPH_SCHEMA,
        "revision": 0,
        "updated_at": now_iso(),
        "nodes": {},
    }


def load_work_graph(layout: ProjectLayout) -> dict[str, Any]:
    graph = read_json(layout.work_graph_json, default=empty_work_graph())
    validate_work_graph(graph)
    return graph


def _accepted(task: dict[str, Any]) -> bool:
    leader = task.get("leader_result") or {}
    return task.get("status") == "done" and leader.get("accepted_by_leader") is True


def _node_state(task: dict[str, Any], dependencies_ready: bool) -> str:
    if _accepted(task):
        return "accepted"
    status = str(task.get("status") or "planned")
    if status == "planned":
        return "ready" if dependencies_ready else "blocked"
    if status == "done":
        return "rejected"
    if status == "failed":
        return "rejected"
    if status in NODE_STATES:
        return status
    return "blocked"


def _normalize_dependencies(value: Iterable[str] | None, task_id: str) -> list[str]:
    dependencies: list[str] = []
    for raw in value or ():
        dependency = str(raw).strip()
        if not dependency:
            raise WorkGraphError("dependency ids must be non-empty")
        if dependency == task_id:
            raise WorkGraphError(f"task {task_id} cannot depend on itself")
        if dependency not in dependencies:
            dependencies.append(dependency)
    return dependencies


def _assert_acyclic(nodes: dict[str, Any]) -> None:
    indegree = {task_id: 0 for task_id in nodes}
    successors: dict[str, list[str]] = {task_id: [] for task_id in nodes}
    for task_id, node in nodes.items():
        for dependency in node.get("dependencies") or []:
            if dependency not in nodes:
                raise WorkGraphError(
                    f"task {task_id} depends on missing task {dependency}"
                )
            indegree[task_id] += 1
            successors[dependency].append(task_id)
    queue = deque(sorted(task_id for task_id, count in indegree.items() if count == 0))
    visited = 0
    while queue:
        task_id = queue.popleft()
        visited += 1
        for successor in sorted(successors[task_id]):
            indegree[successor] -= 1
            if indegree[successor] == 0:
                queue.append(successor)
    if visited != len(nodes):
        raise WorkGraphError("work graph contains a dependency cycle")


def validate_work_graph(graph: dict[str, Any]) -> None:
    if not isinstance(graph, dict) or graph.get("schema_version") != WORK_GRAPH_SCHEMA:
        raise WorkGraphError("invalid work graph schema")
    if type(graph.get("revision")) is not int or graph["revision"] < 0:
        raise WorkGraphError("work graph revision must be a non-negative integer")
    nodes = graph.get("nodes")
    if not isinstance(nodes, dict):
        raise WorkGraphError("work graph nodes must be an object")
    for task_id, node in nodes.items():
        if not isinstance(task_id, str) or not task_id or not isinstance(node, dict):
            raise WorkGraphError("work graph contains an invalid node")
        dependencies = node.get("dependencies")
        if not isinstance(dependencies, list) or any(
            not isinstance(item, str) or not item for item in dependencies
        ):
            raise WorkGraphError(f"task {task_id} dependencies must be string ids")
        if len(dependencies) != len(set(dependencies)) or task_id in dependencies:
            raise WorkGraphError(f"task {task_id} dependencies are invalid")
        if node.get("state") not in NODE_STATES:
            raise WorkGraphError(f"task {task_id} has invalid graph state")
        if node.get("role") not in WORK_ROLES:
            raise WorkGraphError(f"task {task_id} has invalid work role")
        for field in ("workstream_id", "repository_id"):
            value = node.get(field)
            if value is not None and (
                not isinstance(value, str) or not value
            ):
                raise WorkGraphError(f"task {task_id} has invalid {field}")
    _assert_acyclic(nodes)


def _refresh_states(graph: dict[str, Any], tasks: dict[str, dict[str, Any]]) -> None:
    nodes = graph["nodes"]
    for task_id, node in nodes.items():
        dependencies_ready = all(
            dependency in tasks and _accepted(tasks[dependency])
            for dependency in node.get("dependencies") or []
        )
        task = tasks.get(task_id)
        if task is not None:
            node["state"] = _node_state(task, dependencies_ready)
            node["task_status"] = str(task.get("status") or "planned")
            node["updated_at"] = str(task.get("updated_at") or now_iso())


def register_task(
    layout: ProjectLayout,
    task: dict[str, Any],
    *,
    known_tasks: Iterable[dict[str, Any]],
) -> dict[str, Any]:
    """Register one work package and fail closed on missing/cyclic dependencies."""

    graph = load_work_graph(layout)
    tasks = {
        str(item["id"]): item
        for item in known_tasks
        if isinstance(item, dict) and isinstance(item.get("id"), str)
    }
    task_id = str(task["id"])
    tasks[task_id] = task
    dependencies = _normalize_dependencies(task.get("dependencies"), task_id)
    missing = sorted(dependency for dependency in dependencies if dependency not in tasks)
    if missing:
        raise WorkGraphError(
            f"task {task_id} has unknown dependencies: {', '.join(missing)}"
        )
    for known_id, known in tasks.items():
        if known_id not in graph["nodes"]:
            graph["nodes"][known_id] = {
                "task_id": known_id,
                "role": str(known.get("role") or "builder"),
                "workstream_id": known.get("workstream_id"),
                "repository_id": known.get("repository_id"),
                "dependencies": _normalize_dependencies(
                    known.get("dependencies"), known_id
                ),
                "state": "blocked",
                "task_status": str(known.get("status") or "planned"),
                "created_at": str(known.get("created_at") or now_iso()),
                "updated_at": str(known.get("updated_at") or now_iso()),
            }
    graph["nodes"][task_id] = {
        "task_id": task_id,
        "role": str(task.get("role") or "builder"),
        "workstream_id": task.get("workstream_id"),
        "repository_id": task.get("repository_id"),
        "dependencies": dependencies,
        "state": "blocked",
        "task_status": str(task.get("status") or "planned"),
        "created_at": str(task.get("created_at") or now_iso()),
        "updated_at": str(task.get("updated_at") or now_iso()),
    }
    _assert_acyclic(graph["nodes"])
    _refresh_states(graph, tasks)
    graph["revision"] += 1
    graph["updated_at"] = now_iso()
    validate_work_graph(graph)
    atomic_write_json(layout.work_graph_json, graph)
    return graph


def sync_task_node(
    layout: ProjectLayout,
    task: dict[str, Any],
    *,
    known_tasks: Iterable[dict[str, Any]],
) -> dict[str, Any]:
    return register_task(layout, task, known_tasks=known_tasks)


def dispatch_blockers(
    layout: ProjectLayout,
    task_id: str,
    *,
    known_tasks: Iterable[dict[str, Any]],
) -> list[str]:
    tasks = list(known_tasks)
    task = next((item for item in tasks if item.get("id") == task_id), None)
    if task is None:
        return [f"work package {task_id} is missing"]
    task_index = {
        str(item.get("id")): item
        for item in tasks
        if isinstance(item, dict) and item.get("id")
    }
    dependencies = _normalize_dependencies(task.get("dependencies"), task_id)
    graph = load_work_graph(layout)
    persisted = graph["nodes"].get(task_id)
    if persisted is not None and persisted.get("dependencies") != dependencies:
        raise WorkGraphError(
            f"task {task_id} dependency state differs from the authoritative graph"
        )
    missing = [dependency for dependency in dependencies if dependency not in task_index]
    if missing:
        raise WorkGraphError(
            f"task {task_id} has unknown dependencies: {', '.join(sorted(missing))}"
        )
    blockers = [
        dependency
        for dependency in dependencies
        if not _accepted(task_index[dependency])
    ]
    return [
        (
            f"dependency {dependency} is "
            f"{_node_state(task_index[dependency], True)}"
        )
        for dependency in blockers
    ]


def ready_task_ids(graph: dict[str, Any]) -> list[str]:
    validate_work_graph(graph)
    return sorted(
        task_id for task_id, node in graph["nodes"].items() if node["state"] == "ready"
    )


__all__ = [
    "NODE_STATES",
    "WORK_GRAPH_SCHEMA",
    "WORK_ROLES",
    "WorkGraphError",
    "dispatch_blockers",
    "empty_work_graph",
    "load_work_graph",
    "ready_task_ids",
    "register_task",
    "sync_task_node",
    "validate_work_graph",
]
