"""Large-project workstreams, repository identities, and staged integration."""

from __future__ import annotations

import json
import re
import subprocess
from collections import deque
from collections.abc import Iterable, Mapping
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from .quality import canonical_sha256
from .state import now_iso


REPOSITORY_REGISTRY_SCHEMA = "costmarshal-repository-registry-v1"
WORKSTREAM_REGISTRY_SCHEMA = "costmarshal-workstream-registry-v1"
INTEGRATION_PLAN_SCHEMA = "costmarshal-staged-integration-plan-v1"
INTEGRATION_GATE_SCHEMA = "costmarshal-integration-gate-v1"
PRODUCTION_BOUNDARY_SCHEMA = "costmarshal-production-boundary-v1"
PRODUCTION_STATUS_SCHEMA = "costmarshal-production-status-v1"
_SAFE_ID = re.compile(r"[a-z0-9][a-z0-9._-]{0,63}\Z")
_FULL_COMMIT = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})\Z")


class LargeProjectError(ValueError):
    """Raised when large-project metadata is unsafe or inconsistent."""


def _copy(value: Any) -> Any:
    return json.loads(
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    )


def _identifier(value: str, label: str) -> str:
    result = str(value or "").strip().lower()
    if not _SAFE_ID.fullmatch(result):
        raise LargeProjectError(f"{label} must match {_SAFE_ID.pattern!r}")
    return result


def _money(value: Any, label: str) -> Decimal | None:
    if value is None:
        return None
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise LargeProjectError(f"{label} must be finite non-negative CNY") from exc
    if not result.is_finite() or result < 0:
        raise LargeProjectError(f"{label} must be finite non-negative CNY")
    return result


def _money_text(value: Decimal | None) -> str | None:
    return None if value is None else format(value.quantize(Decimal("0.000000001")), "f")


def _git_identity(path: Path, *, required: bool) -> tuple[str | None, str]:
    try:
        root = Path(
            subprocess.check_output(
                ["git", "-C", str(path), "rev-parse", "--show-toplevel"],
                text=True,
                stderr=subprocess.STDOUT,
            ).strip()
        ).resolve()
        if root != path:
            if required:
                raise LargeProjectError(
                    "repository path must be the Git repository root"
                )
            return None, "unverified-default"
        head = subprocess.check_output(
            ["git", "-C", str(path), "rev-parse", "--verify", "HEAD^{commit}"],
            text=True,
            stderr=subprocess.STDOUT,
        ).strip().lower()
    except (FileNotFoundError, subprocess.CalledProcessError) as exc:
        if required:
            raise LargeProjectError(
                "registered repositories require Git and a committed HEAD"
            ) from exc
        return None, "unverified-default"
    if not _FULL_COMMIT.fullmatch(head):
        raise LargeProjectError("Git returned an invalid full commit id")
    return head, "git"


def empty_repository_registry() -> dict[str, Any]:
    return {
        "schema_version": REPOSITORY_REGISTRY_SCHEMA,
        "revision": 0,
        "updated_at": now_iso(),
        "repositories": {},
    }


def build_repository(
    *,
    repository_id: str,
    path: str | Path,
    role: str,
    require_git: bool = True,
    is_default: bool = False,
) -> dict[str, Any]:
    repo_id = _identifier(repository_id, "repository id")
    repository_path = Path(path).expanduser()
    if repository_path.is_symlink():
        raise LargeProjectError("repository path must not be a symlink")
    try:
        repository_path = repository_path.resolve(strict=True)
    except OSError as exc:
        raise LargeProjectError(f"repository path is unavailable: {exc}") from exc
    if not repository_path.is_dir():
        raise LargeProjectError("repository path must be a directory")
    head, repository_kind = _git_identity(repository_path, required=require_git)
    body = {
        "repository_id": repo_id,
        "path": str(repository_path),
        "role": str(role or "").strip() or "component",
        "kind": repository_kind,
        "registered_head": head,
        "default": bool(is_default),
        "source_mutation": False,
    }
    return {
        **body,
        "binding_sha256": canonical_sha256(body),
        "registered_at": now_iso(),
    }


def validate_repository_registry(value: Mapping[str, Any]) -> dict[str, Any]:
    row = _copy(dict(value))
    if row.get("schema_version") != REPOSITORY_REGISTRY_SCHEMA:
        raise LargeProjectError("invalid repository registry schema")
    if type(row.get("revision")) is not int or row["revision"] < 0:
        raise LargeProjectError("repository registry revision is invalid")
    repositories = row.get("repositories")
    if not isinstance(repositories, dict):
        raise LargeProjectError("repository registry entries must be an object")
    paths: set[str] = set()
    defaults = 0
    for key, repository in repositories.items():
        if _identifier(str(key), "repository id") != key or not isinstance(
            repository, dict
        ):
            raise LargeProjectError("repository registry entry is invalid")
        body = {
            field: repository.get(field)
            for field in (
                "repository_id",
                "path",
                "role",
                "kind",
                "registered_head",
                "default",
                "source_mutation",
            )
        }
        if (
            body["repository_id"] != key
            or not isinstance(body["path"], str)
            or not body["path"]
            or not isinstance(body["role"], str)
            or not body["role"].strip()
            or body["kind"] not in {"git", "unverified-default"}
            or type(body["default"]) is not bool
            or body["source_mutation"] is not False
            or repository.get("binding_sha256") != canonical_sha256(body)
        ):
            raise LargeProjectError(f"repository {key} binding is invalid")
        if body["kind"] == "git" and not _FULL_COMMIT.fullmatch(
            str(body["registered_head"] or "")
        ):
            raise LargeProjectError(f"repository {key} has invalid registered HEAD")
        canonical_path = str(Path(body["path"]).expanduser().resolve())
        if canonical_path != body["path"] or canonical_path in paths:
            raise LargeProjectError("repository paths must be canonical and unique")
        paths.add(canonical_path)
        defaults += body["default"] is True
    if defaults > 1:
        raise LargeProjectError("repository registry has multiple defaults")
    return row


def upsert_repository(
    registry: Mapping[str, Any],
    repository: Mapping[str, Any],
) -> dict[str, Any]:
    result = validate_repository_registry(registry)
    repo = _copy(dict(repository))
    repo_id = str(repo.get("repository_id") or "")
    preview = {
        **result,
        "repositories": {**result["repositories"], repo_id: repo},
    }
    preview["revision"] += 1
    preview["updated_at"] = now_iso()
    return validate_repository_registry(preview)


def repository_for_task(
    *,
    project: Mapping[str, Any],
    registry: Mapping[str, Any],
    repository_id: str | None,
) -> dict[str, Any]:
    normalized = validate_repository_registry(registry)
    repositories = normalized["repositories"]
    selected_id = (
        _identifier(repository_id, "repository id")
        if repository_id
        else next(
            (
                key
                for key, repository in repositories.items()
                if repository.get("default") is True
            ),
            None,
        )
    )
    if selected_id is None and "default" in repositories:
        selected_id = "default"
    if selected_id is None:
        workspace = project.get("workspace")
        if not isinstance(workspace, str) or not workspace:
            raise LargeProjectError("project has no default repository workspace")
        return build_repository(
            repository_id="default",
            path=workspace,
            role="primary",
            require_git=False,
            is_default=True,
        )
    repository = repositories.get(selected_id)
    if repository is None:
        raise LargeProjectError(f"unknown repository: {selected_id}")
    path = Path(repository["path"])
    if not path.is_dir() or path.is_symlink():
        raise LargeProjectError(f"repository is unavailable: {selected_id}")
    return _copy(repository)


def verify_repository_binding(repository: Mapping[str, Any]) -> dict[str, Any]:
    path = Path(str(repository.get("path") or "")).expanduser().resolve()
    if not path.is_dir() or path.is_symlink():
        raise LargeProjectError("repository binding path is unavailable")
    current_head, kind = _git_identity(
        path, required=repository.get("kind") == "git"
    )
    return {
        "repository_id": repository.get("repository_id"),
        "path": str(path),
        "kind": kind,
        "registered_head": repository.get("registered_head"),
        "current_head": current_head,
        "head_matches_registration": (
            current_head == repository.get("registered_head")
            if repository.get("registered_head") is not None
            else None
        ),
        "binding_sha256": repository.get("binding_sha256"),
    }


def _verify_rollback_commit(
    repository: Mapping[str, Any],
    *,
    rollback_ref: str,
    expected_head: str,
) -> None:
    path = Path(str(repository.get("path") or ""))
    try:
        subprocess.check_output(
            [
                "git",
                "-C",
                str(path),
                "cat-file",
                "-e",
                f"{rollback_ref}^{{commit}}",
            ],
            text=True,
            stderr=subprocess.STDOUT,
        )
        subprocess.check_output(
            [
                "git",
                "-C",
                str(path),
                "merge-base",
                "--is-ancestor",
                rollback_ref,
                expected_head,
            ],
            text=True,
            stderr=subprocess.STDOUT,
        )
    except (FileNotFoundError, subprocess.CalledProcessError) as exc:
        raise LargeProjectError(
            f"repository {repository.get('repository_id')} rollback ref must "
            "exist and be an ancestor of the frozen integration head"
        ) from exc


def empty_workstream_registry() -> dict[str, Any]:
    return {
        "schema_version": WORKSTREAM_REGISTRY_SCHEMA,
        "revision": 0,
        "updated_at": now_iso(),
        "workstreams": {},
    }


def _assert_workstream_acyclic(workstreams: Mapping[str, Any]) -> None:
    indegree = {key: 0 for key in workstreams}
    successors = {key: [] for key in workstreams}
    for key, row in workstreams.items():
        for dependency in row.get("depends_on") or []:
            if dependency not in workstreams:
                raise LargeProjectError(
                    f"workstream {key} depends on missing workstream {dependency}"
                )
            indegree[key] += 1
            successors[dependency].append(key)
    queue = deque(sorted(key for key, value in indegree.items() if value == 0))
    visited = 0
    while queue:
        key = queue.popleft()
        visited += 1
        for successor in successors[key]:
            indegree[successor] -= 1
            if indegree[successor] == 0:
                queue.append(successor)
    if visited != len(workstreams):
        raise LargeProjectError("workstream graph contains a dependency cycle")


def build_workstream(
    *,
    workstream_id: str,
    name: str,
    objective: str,
    repository_ids: Iterable[str],
    depends_on: Iterable[str],
    budget_cny: Any,
    concurrency_limit: int,
    registry: Mapping[str, Any],
    existing: Mapping[str, Any],
    project_budget_cny: Any = None,
) -> dict[str, Any]:
    stream_id = _identifier(workstream_id, "workstream id")
    if not str(name or "").strip() or not str(objective or "").strip():
        raise LargeProjectError("workstream name and objective are required")
    repositories = [
        _identifier(item, "repository id") for item in repository_ids
    ]
    repositories = list(dict.fromkeys(repositories))
    known_repositories = validate_repository_registry(registry)["repositories"]
    if not repositories or any(item not in known_repositories for item in repositories):
        raise LargeProjectError(
            "workstream requires one or more registered repositories"
        )
    dependencies = list(
        dict.fromkeys(_identifier(item, "workstream dependency") for item in depends_on)
    )
    if stream_id in dependencies:
        raise LargeProjectError("workstream cannot depend on itself")
    if type(concurrency_limit) is not int or concurrency_limit < 1:
        raise LargeProjectError("workstream concurrency limit must be positive")
    budget = _money(budget_cny, "workstream budget")
    project_budget = _money(project_budget_cny, "project budget")
    current = validate_workstream_registry(existing, repository_registry=registry)
    if stream_id in current["workstreams"]:
        raise LargeProjectError(f"workstream already exists: {stream_id}")
    allocated = sum(
        (
            _money(item.get("budget_cny"), "stored workstream budget")
            or Decimal("0")
        )
        for item in current["workstreams"].values()
    )
    if project_budget is not None and budget is not None and allocated + budget > project_budget:
        raise LargeProjectError("workstream allocations exceed the project budget")
    body = {
        "workstream_id": stream_id,
        "name": str(name).strip(),
        "objective": str(objective).strip(),
        "repository_ids": repositories,
        "depends_on": dependencies,
        "budget_cny": _money_text(budget),
        "concurrency_limit": concurrency_limit,
    }
    row = {
        **body,
        "workstream_sha256": canonical_sha256(body),
        "created_at": now_iso(),
    }
    preview = {
        **current,
        "workstreams": {**current["workstreams"], stream_id: row},
    }
    preview["revision"] += 1
    preview["updated_at"] = now_iso()
    validate_workstream_registry(preview, repository_registry=registry)
    return row


def validate_workstream_registry(
    value: Mapping[str, Any],
    *,
    repository_registry: Mapping[str, Any],
) -> dict[str, Any]:
    row = _copy(dict(value))
    if row.get("schema_version") != WORKSTREAM_REGISTRY_SCHEMA:
        raise LargeProjectError("invalid workstream registry schema")
    if type(row.get("revision")) is not int or row["revision"] < 0:
        raise LargeProjectError("workstream registry revision is invalid")
    workstreams = row.get("workstreams")
    if not isinstance(workstreams, dict):
        raise LargeProjectError("workstreams must be an object")
    repository_ids = set(
        validate_repository_registry(repository_registry)["repositories"]
    )
    for key, stream in workstreams.items():
        if _identifier(str(key), "workstream id") != key or not isinstance(
            stream, dict
        ):
            raise LargeProjectError("workstream entry is invalid")
        body = {
            field: stream.get(field)
            for field in (
                "workstream_id",
                "name",
                "objective",
                "repository_ids",
                "depends_on",
                "budget_cny",
                "concurrency_limit",
            )
        }
        if (
            body["workstream_id"] != key
            or not isinstance(body["name"], str)
            or not body["name"].strip()
            or not isinstance(body["objective"], str)
            or not body["objective"].strip()
            or not isinstance(body["repository_ids"], list)
            or not body["repository_ids"]
            or any(
                not isinstance(item, str) for item in body["repository_ids"]
            )
            or len(set(body["repository_ids"])) != len(body["repository_ids"])
            or any(item not in repository_ids for item in body["repository_ids"])
            or not isinstance(body["depends_on"], list)
            or any(
                not isinstance(item, str) for item in body["depends_on"]
            )
            or len(set(body["depends_on"])) != len(body["depends_on"])
            or any(
                not isinstance(item, str)
                or _identifier(item, "workstream dependency") != item
                for item in body["depends_on"]
            )
            or type(body["concurrency_limit"]) is not int
            or body["concurrency_limit"] < 1
            or stream.get("workstream_sha256") != canonical_sha256(body)
        ):
            raise LargeProjectError(f"workstream {key} binding is invalid")
        _money(body["budget_cny"], f"workstream {key} budget")
    _assert_workstream_acyclic(workstreams)
    return row


def append_workstream(
    registry: Mapping[str, Any],
    workstream: Mapping[str, Any],
    *,
    repository_registry: Mapping[str, Any],
) -> dict[str, Any]:
    result = validate_workstream_registry(
        registry, repository_registry=repository_registry
    )
    stream = _copy(dict(workstream))
    stream_id = str(stream.get("workstream_id") or "")
    if stream_id in result["workstreams"]:
        raise LargeProjectError(f"workstream already exists: {stream_id}")
    result["workstreams"][stream_id] = stream
    result["revision"] += 1
    result["updated_at"] = now_iso()
    return validate_workstream_registry(
        result, repository_registry=repository_registry
    )


def _task_accepted(task: Mapping[str, Any]) -> bool:
    leader = task.get("leader_result") or {}
    return (
        task.get("status") == "done"
        and isinstance(leader, Mapping)
        and leader.get("accepted_by_leader") is True
    )


def workstream_statuses(
    registry: Mapping[str, Any],
    *,
    repository_registry: Mapping[str, Any],
    tasks: Iterable[Mapping[str, Any]],
    accepted_gate_workstream_ids: Iterable[str] = (),
    task_commitment_cny: Mapping[str, Any] | None = None,
) -> dict[str, dict[str, Any]]:
    streams = validate_workstream_registry(
        registry, repository_registry=repository_registry
    )["workstreams"]
    task_rows = list(tasks)
    accepted_gates = set(accepted_gate_workstream_ids)
    statuses: dict[str, dict[str, Any]] = {}
    for stream_id in streams:
        owned = [task for task in task_rows if task.get("workstream_id") == stream_id]
        dependencies_ready = all(
            dependency in accepted_gates
            for dependency in streams[stream_id].get("depends_on") or []
        )
        active_count = sum(
            task.get("status")
            in {"dispatched", "running", "waiting_leader", "escalate"}
            for task in owned
        )
        accepted_count = sum(_task_accepted(task) for task in owned)
        known_commitment = Decimal("0")
        unknown_commitment_count = 0
        if task_commitment_cny is not None:
            for task in owned:
                task_id = str(task.get("id") or "")
                if task_id not in task_commitment_cny:
                    unknown_commitment_count += 1
                    continue
                known_commitment += (
                    _money(
                        task_commitment_cny[task_id],
                        f"task {task_id} commitment",
                    )
                    or Decimal("0")
                )
        if stream_id in accepted_gates:
            state = "accepted"
        elif not dependencies_ready:
            state = "blocked"
        elif owned and accepted_count == len(owned):
            state = "gate-ready"
        elif active_count:
            state = "active"
        else:
            state = "ready"
        statuses[stream_id] = {
            "state": state,
            "task_count": len(owned),
            "accepted_task_count": accepted_count,
            "active_task_count": active_count,
            "dependencies_ready": dependencies_ready,
            "budget_cny": streams[stream_id].get("budget_cny"),
            "known_commitment_cny": (
                _money_text(known_commitment)
                if task_commitment_cny is not None
                else None
            ),
            "unknown_commitment_count": (
                unknown_commitment_count
                if task_commitment_cny is not None
                else None
            ),
        }
    return statuses


def workstream_dispatch_blockers(
    *,
    task: Mapping[str, Any],
    tasks: Iterable[Mapping[str, Any]],
    workstream_registry: Mapping[str, Any],
    repository_registry: Mapping[str, Any],
    accepted_gate_workstream_ids: Iterable[str],
) -> list[str]:
    stream_id = task.get("workstream_id")
    if not stream_id:
        return []
    streams = validate_workstream_registry(
        workstream_registry, repository_registry=repository_registry
    )["workstreams"]
    stream = streams.get(str(stream_id))
    if stream is None:
        raise LargeProjectError(f"task references missing workstream {stream_id}")
    if task.get("repository_id") not in stream["repository_ids"]:
        raise LargeProjectError("task repository is outside its workstream")
    accepted = set(accepted_gate_workstream_ids)
    blockers = [
        f"workstream dependency {dependency} lacks an accepted integration Gate"
        for dependency in stream["depends_on"]
        if dependency not in accepted
    ]
    active = sum(
        row.get("workstream_id") == stream_id
        and row.get("id") != task.get("id")
        and row.get("status")
        in {"dispatched", "running", "waiting_leader", "escalate"}
        for row in tasks
    )
    if active >= int(stream["concurrency_limit"]):
        blockers.append(
            f"workstream concurrency quota is full ({active}/{stream['concurrency_limit']})"
        )
    return blockers


def enforce_workstream_budget(
    *,
    task: Mapping[str, Any],
    tasks: Iterable[Mapping[str, Any]],
    workstream_registry: Mapping[str, Any],
    repository_registry: Mapping[str, Any],
    task_commitment_cny: Mapping[str, Any],
    projected_task_commitment_cny: Any,
) -> None:
    stream_id = task.get("workstream_id")
    if not stream_id:
        return
    stream = validate_workstream_registry(
        workstream_registry, repository_registry=repository_registry
    )["workstreams"].get(str(stream_id))
    if stream is None:
        raise LargeProjectError(f"task references missing workstream {stream_id}")
    budget = _money(stream.get("budget_cny"), "workstream budget")
    if budget is None:
        return
    total = Decimal("0")
    for row in tasks:
        if row.get("workstream_id") != stream_id or row.get("id") == task.get("id"):
            continue
        total += _money(
            task_commitment_cny.get(str(row.get("id"))),
            "task commitment",
        ) or Decimal("0")
    total += _money(projected_task_commitment_cny, "projected task commitment") or Decimal("0")
    if total > budget:
        raise LargeProjectError(
            f"workstream budget exceeded: projected={_money_text(total)} max={_money_text(budget)}"
        )


def _latest_accepted_artifact_ids(
    artifact_rows: Iterable[Mapping[str, Any]],
    *,
    kind: str | None = None,
) -> set[str]:
    latest: dict[str, tuple[str, Any]] = {}
    for row in artifact_rows:
        artifact_id = row.get("artifact_id")
        lifecycle = row.get("lifecycle")
        if isinstance(artifact_id, str) and isinstance(lifecycle, str):
            latest[artifact_id] = (lifecycle, row.get("kind"))
    return {
        key
        for key, (lifecycle, artifact_kind) in latest.items()
        if lifecycle == "accepted"
        and (kind is None or artifact_kind == kind)
    }


def build_integration_plan(
    *,
    project_id: str,
    command_id: str,
    milestone: str,
    workstream_ids: Iterable[str],
    task_ids: Iterable[str],
    repository_ids: Iterable[str],
    interface_artifact_ids: Iterable[str],
    rollback_refs: Mapping[str, str],
    repository_registry: Mapping[str, Any],
    workstream_registry: Mapping[str, Any],
    tasks: Iterable[Mapping[str, Any]],
    artifact_rows: Iterable[Mapping[str, Any]],
) -> dict[str, Any]:
    repositories = validate_repository_registry(repository_registry)["repositories"]
    workstreams = validate_workstream_registry(
        workstream_registry, repository_registry=repository_registry
    )["workstreams"]
    selected_workstreams = list(
        dict.fromkeys(_identifier(item, "workstream id") for item in workstream_ids)
    )
    selected_repositories = list(
        dict.fromkeys(_identifier(item, "repository id") for item in repository_ids)
    )
    selected_tasks = list(dict.fromkeys(str(item).strip() for item in task_ids))
    selected_artifacts = list(
        dict.fromkeys(str(item).strip() for item in interface_artifact_ids)
    )
    if (
        not str(milestone or "").strip()
        or not selected_workstreams
        or not selected_repositories
        or not selected_tasks
    ):
        raise LargeProjectError(
            "integration plan requires milestone, workstreams, repositories, and tasks"
        )
    if any(item not in workstreams for item in selected_workstreams):
        raise LargeProjectError("integration plan references an unknown workstream")
    if any(item not in repositories for item in selected_repositories):
        raise LargeProjectError("integration plan references an unknown repository")
    if set(rollback_refs) != set(selected_repositories):
        raise LargeProjectError(
            "integration plan requires exactly one rollback ref per repository"
        )
    task_index = {
        str(task.get("id")): task for task in tasks if task.get("id")
    }
    if any(item not in task_index for item in selected_tasks):
        raise LargeProjectError("integration plan references an unknown task")
    for task_id in selected_tasks:
        task = task_index[task_id]
        if (
            task.get("workstream_id") not in selected_workstreams
            or task.get("repository_id") not in selected_repositories
        ):
            raise LargeProjectError(
                f"integration task {task_id} is outside selected workstreams/repositories"
            )
    selected_task_set = set(selected_tasks)
    for workstream_id in selected_workstreams:
        owned_tasks = {
            task_id
            for task_id, task in task_index.items()
            if task.get("workstream_id") == workstream_id
            and task.get("status") != "cancelled"
        }
        if not owned_tasks:
            raise LargeProjectError(
                f"integration workstream {workstream_id} has no task"
            )
        omitted = sorted(owned_tasks - selected_task_set)
        if omitted:
            raise LargeProjectError(
                f"integration workstream {workstream_id} omits tasks: "
                + ", ".join(omitted)
            )
    task_repositories = {
        str(task_index[task_id].get("repository_id"))
        for task_id in selected_tasks
    }
    unused_repositories = sorted(set(selected_repositories) - task_repositories)
    if unused_repositories:
        raise LargeProjectError(
            "integration repositories without selected tasks: "
            + ", ".join(unused_repositories)
        )
    accepted_artifacts = _latest_accepted_artifact_ids(
        artifact_rows, kind="interface"
    )
    if any(item not in accepted_artifacts for item in selected_artifacts):
        raise LargeProjectError(
            "integration interface Artifacts must already be accepted"
        )
    repository_bindings = {}
    for repo_id in selected_repositories:
        repository = repositories[repo_id]
        inspection = verify_repository_binding(repository)
        rollback = str(rollback_refs.get(repo_id) or "").strip().lower()
        if not _FULL_COMMIT.fullmatch(rollback):
            raise LargeProjectError(
                f"repository {repo_id} requires an exact rollback commit"
            )
        _verify_rollback_commit(
            repository,
            rollback_ref=rollback,
            expected_head=str(inspection["current_head"]),
        )
        repository_bindings[repo_id] = {
            "binding_sha256": repository["binding_sha256"],
            "expected_head": inspection["current_head"],
            "rollback_ref": rollback,
        }
    material = {
        "schema_version": INTEGRATION_PLAN_SCHEMA,
        "project_id": project_id,
        "command_id": command_id,
        "milestone": str(milestone).strip(),
        "strategy": "staged-per-repository",
        "atomic_cross_repository": False,
        "workstream_ids": selected_workstreams,
        "task_ids": selected_tasks,
        "repository_bindings": repository_bindings,
        "interface_artifact_ids": selected_artifacts,
        "source_mutation": False,
    }
    digest = canonical_sha256(material)
    return {
        **material,
        "plan_id": "INT-" + digest.removeprefix("sha256:")[:32],
        "plan_sha256": digest,
        "created_at": now_iso(),
    }


def validate_integration_plan(value: Mapping[str, Any]) -> dict[str, Any]:
    row = _copy(dict(value))
    if row.get("schema_version") != INTEGRATION_PLAN_SCHEMA:
        raise LargeProjectError("invalid integration plan schema")
    body = {
        key: item
        for key, item in row.items()
        if key not in {"plan_id", "plan_sha256", "created_at"}
    }
    digest = canonical_sha256(body)
    if (
        row.get("plan_sha256") != digest
        or row.get("plan_id") != "INT-" + digest.removeprefix("sha256:")[:32]
        or not isinstance(row.get("project_id"), str)
        or not row.get("project_id")
        or not isinstance(row.get("command_id"), str)
        or not row.get("command_id")
        or not isinstance(row.get("milestone"), str)
        or not row.get("milestone")
        or row.get("strategy") != "staged-per-repository"
        or row.get("atomic_cross_repository") is not False
        or not isinstance(row.get("workstream_ids"), list)
        or not row.get("workstream_ids")
        or any(
            not isinstance(item, str) for item in row.get("workstream_ids") or []
        )
        or any(
            _identifier(item, "workstream id") != item
            for item in row.get("workstream_ids") or []
        )
        or len(set(row["workstream_ids"])) != len(row["workstream_ids"])
        or not isinstance(row.get("task_ids"), list)
        or not row.get("task_ids")
        or any(
            not isinstance(item, str) for item in row.get("task_ids") or []
        )
        or len(set(row["task_ids"])) != len(row["task_ids"])
        or not isinstance(row.get("repository_bindings"), dict)
        or not row.get("repository_bindings")
        or not isinstance(row.get("interface_artifact_ids"), list)
        or any(
            not isinstance(item, str) or not item
            for item in row.get("interface_artifact_ids") or []
        )
        or row.get("source_mutation") is not False
    ):
        raise LargeProjectError("integration plan hash or safety binding is invalid")
    for repo_id, binding in row["repository_bindings"].items():
        if (
            _identifier(str(repo_id), "repository id") != repo_id
            or not isinstance(binding, dict)
            or not isinstance(binding.get("binding_sha256"), str)
            or not _FULL_COMMIT.fullmatch(str(binding.get("expected_head") or ""))
            or not _FULL_COMMIT.fullmatch(str(binding.get("rollback_ref") or ""))
        ):
            raise LargeProjectError(
                "integration plan repository binding is invalid"
            )
    return row


def build_integration_gate(
    *,
    plan: Mapping[str, Any],
    command_id: str,
    tasks: Iterable[Mapping[str, Any]],
    artifact_rows: Iterable[Mapping[str, Any]],
    repository_registry: Mapping[str, Any],
    approved_by: str,
) -> dict[str, Any]:
    validated = validate_integration_plan(plan)
    task_index = {
        str(task.get("id")): task for task in tasks if task.get("id")
    }
    accepted_artifacts = _latest_accepted_artifact_ids(
        artifact_rows, kind="interface"
    )
    repositories = validate_repository_registry(repository_registry)["repositories"]
    checks: list[dict[str, Any]] = []
    planned_task_ids = set(validated["task_ids"])
    for workstream_id in validated["workstream_ids"]:
        current_task_ids = {
            task_id
            for task_id, task in task_index.items()
            if task.get("workstream_id") == workstream_id
            and task.get("status") != "cancelled"
        }
        expected_task_ids = {
            task_id
            for task_id in planned_task_ids
            if (task_index.get(task_id) or {}).get("workstream_id")
            == workstream_id
        }
        checks.append(
            {
                "name": f"workstream-task-set:{workstream_id}",
                "passed": current_task_ids == expected_task_ids,
                "observed": sorted(current_task_ids),
                "expected": sorted(expected_task_ids),
            }
        )
    for task_id in validated["task_ids"]:
        checks.append(
            {
                "name": f"task:{task_id}",
                "passed": _task_accepted(task_index.get(task_id, {})),
                "observed": (task_index.get(task_id) or {}).get("status"),
                "expected": "Leader-accepted",
            }
        )
    for artifact_id in validated["interface_artifact_ids"]:
        checks.append(
            {
                "name": f"interface-artifact:{artifact_id}",
                "passed": artifact_id in accepted_artifacts,
                "observed": artifact_id in accepted_artifacts,
                "expected": True,
            }
        )
    for repo_id, binding in validated["repository_bindings"].items():
        repository = repositories.get(repo_id)
        if repository is None:
            observed = None
            passed = False
        else:
            inspection = verify_repository_binding(repository)
            observed = inspection["current_head"]
            passed = (
                repository.get("binding_sha256") == binding.get("binding_sha256")
                and observed == binding.get("expected_head")
            )
        checks.append(
            {
                "name": f"repository-head:{repo_id}",
                "passed": passed,
                "observed": observed,
                "expected": binding.get("expected_head"),
            }
        )
    body = {
        "schema_version": INTEGRATION_GATE_SCHEMA,
        "project_id": validated["project_id"],
        "command_id": str(command_id or "").strip(),
        "plan_id": validated["plan_id"],
        "plan_sha256": validated["plan_sha256"],
        "approved_by": str(approved_by or "").strip(),
        "checks": checks,
        "passed": all(check["passed"] is True for check in checks),
        "workstream_ids": list(validated["workstream_ids"]),
        "integration_authority": "Leader",
        "source_mutation": False,
    }
    if not body["command_id"] or not body["approved_by"]:
        raise LargeProjectError(
            "integration Gate requires a command id and an approver"
        )
    digest = canonical_sha256(body)
    return {
        **body,
        "gate_id": "IGATE-" + digest.removeprefix("sha256:")[:32],
        "gate_sha256": digest,
        "recorded_at": now_iso(),
    }


def validate_integration_gate(
    value: Mapping[str, Any],
    *,
    plan: Mapping[str, Any],
) -> dict[str, Any]:
    row = _copy(dict(value))
    validated_plan = validate_integration_plan(plan)
    if row.get("schema_version") != INTEGRATION_GATE_SCHEMA:
        raise LargeProjectError("invalid integration Gate schema")
    body = {
        key: item
        for key, item in row.items()
        if key not in {"gate_id", "gate_sha256", "recorded_at"}
    }
    digest = canonical_sha256(body)
    if (
        row.get("gate_sha256") != digest
        or row.get("gate_id") != "IGATE-" + digest.removeprefix("sha256:")[:32]
        or not isinstance(row.get("command_id"), str)
        or not row.get("command_id")
        or not isinstance(row.get("approved_by"), str)
        or not row.get("approved_by")
        or row.get("plan_id") != validated_plan["plan_id"]
        or row.get("plan_sha256") != validated_plan["plan_sha256"]
        or row.get("project_id") != validated_plan["project_id"]
        or row.get("workstream_ids") != validated_plan["workstream_ids"]
        or row.get("integration_authority") != "Leader"
        or not isinstance(row.get("checks"), list)
        or not row.get("checks")
        or any(
            not isinstance(check, dict)
            or not isinstance(check.get("name"), str)
            or type(check.get("passed")) is not bool
            for check in row.get("checks") or []
        )
        or row.get("passed")
        != all(check.get("passed") is True for check in row.get("checks") or [])
        or row.get("source_mutation") is not False
    ):
        raise LargeProjectError("integration Gate binding is invalid")
    return row


def _safe_endpoint(value: str, label: str) -> str:
    parsed = urlsplit(str(value or "").strip())
    if (
        parsed.scheme != "https"
        or not parsed.netloc
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
    ):
        raise LargeProjectError(
            f"{label} must be a secret-free HTTPS endpoint without query or fragment"
        )
    return parsed.geturl()


def build_production_boundary(
    *,
    mode: str,
    broker_endpoint: str,
    broker_identity: str,
    provider_proxy_endpoint: str,
    hard_budget_enforced: bool,
    evidence_artifact_ids: Mapping[str, str],
) -> dict[str, Any]:
    if mode not in {"report-only", "enforced"}:
        raise LargeProjectError("production boundary mode must be report-only or enforced")
    required_evidence = {
        "real_provider_backtest",
        "live_oci_adversarial",
        "credential_broker_attestation",
        "provider_proxy_budget_attestation",
        "schema_drift_monitor",
    }
    if set(evidence_artifact_ids) != required_evidence or any(
        not isinstance(value, str) or not value
        for value in evidence_artifact_ids.values()
    ):
        raise LargeProjectError(
            "production boundary requires all five external evidence Artifact ids"
        )
    body = {
        "schema_version": PRODUCTION_BOUNDARY_SCHEMA,
        "mode": mode,
        "credential_broker": {
            "endpoint": _safe_endpoint(broker_endpoint, "broker endpoint"),
            "workload_identity": str(broker_identity or "").strip(),
            "provider_secret_in_costmarshal": False,
        },
        "provider_proxy": {
            "endpoint": _safe_endpoint(
                provider_proxy_endpoint, "provider proxy endpoint"
            ),
            "hard_budget_enforced": bool(hard_budget_enforced),
        },
        "evidence_artifact_ids": dict(sorted(evidence_artifact_ids.items())),
        "runtime_adapter": "external-contract-required",
        "external_certification": False,
    }
    if not body["credential_broker"]["workload_identity"]:
        raise LargeProjectError("broker workload identity is required")
    digest = canonical_sha256(body)
    return {
        **body,
        "boundary_sha256": digest,
        "configured_at": now_iso(),
    }


def validate_production_boundary(value: Mapping[str, Any]) -> dict[str, Any]:
    row = _copy(dict(value))
    if row.get("schema_version") != PRODUCTION_BOUNDARY_SCHEMA:
        raise LargeProjectError("invalid production boundary schema")
    body = {
        key: item
        for key, item in row.items()
        if key not in {"boundary_sha256", "configured_at"}
    }
    if (
        row.get("boundary_sha256") != canonical_sha256(body)
        or row.get("mode") not in {"report-only", "enforced"}
        or row.get("external_certification") is not False
        or row.get("runtime_adapter") != "external-contract-required"
        or not isinstance(
            (row.get("credential_broker") or {}).get("workload_identity"),
            str,
        )
        or not (row.get("credential_broker") or {}).get("workload_identity")
        or (row.get("credential_broker") or {}).get("provider_secret_in_costmarshal")
        is not False
        or type(
            (row.get("provider_proxy") or {}).get("hard_budget_enforced")
        )
        is not bool
        or not isinstance(row.get("evidence_artifact_ids"), dict)
        or set(row.get("evidence_artifact_ids") or {})
        != {
            "real_provider_backtest",
            "live_oci_adversarial",
            "credential_broker_attestation",
            "provider_proxy_budget_attestation",
            "schema_drift_monitor",
        }
    ):
        raise LargeProjectError("production boundary binding is invalid")
    _safe_endpoint(
        str((row.get("credential_broker") or {}).get("endpoint") or ""),
        "broker endpoint",
    )
    _safe_endpoint(
        str((row.get("provider_proxy") or {}).get("endpoint") or ""),
        "provider proxy endpoint",
    )
    return row


def production_status(
    *,
    boundary: Mapping[str, Any] | None,
    artifact_rows: Iterable[Mapping[str, Any]],
    sqlite_authoritative: bool,
    worker_isolation: Mapping[str, Any],
) -> dict[str, Any]:
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

    if boundary is None:
        check("production-boundary-configured", False, False, True)
        return {
            "schema_version": PRODUCTION_STATUS_SCHEMA,
            "status": "blocked",
            "external_certification": False,
            "checks": checks,
            "warning": "No deployment is certified by local configuration alone.",
        }
    configured = validate_production_boundary(boundary)
    accepted = _latest_accepted_artifact_ids(artifact_rows)
    check("sqlite-authoritative", sqlite_authoritative, sqlite_authoritative, True)
    image = str(worker_isolation.get("image") or "")
    check(
        "digest-pinned-worker",
        bool(re.search(r"@sha256:[0-9a-f]{64}\Z", image)),
        image or None,
        "name@sha256:<64 hex>",
    )
    network_mode = worker_isolation.get("network_mode")
    check(
        "provider-proxy-network",
        network_mode == "provider-proxy",
        network_mode,
        "provider-proxy",
    )
    check(
        "hard-budget-proxy",
        (configured.get("provider_proxy") or {}).get("hard_budget_enforced")
        is True,
        (configured.get("provider_proxy") or {}).get("hard_budget_enforced"),
        True,
    )
    for evidence_type, artifact_id in configured["evidence_artifact_ids"].items():
        check(
            f"external-evidence:{evidence_type}",
            artifact_id in accepted,
            artifact_id in accepted,
            True,
        )
    # v4 defines the fail-closed external contract but intentionally does not
    # pretend the current raw-key runner is a broker adapter.
    check(
        "external-broker-runtime-adapter",
        False,
        "not-implemented",
        "attested external workload-identity adapter",
    )
    return {
        "schema_version": PRODUCTION_STATUS_SCHEMA,
        "status": "blocked",
        "external_certification": False,
        "checks": checks,
        "warning": (
            "The external broker runtime adapter is not implemented; raw-key "
            "workers remain outside the production-certified boundary."
        ),
    }


__all__ = [
    "INTEGRATION_GATE_SCHEMA",
    "INTEGRATION_PLAN_SCHEMA",
    "PRODUCTION_BOUNDARY_SCHEMA",
    "PRODUCTION_STATUS_SCHEMA",
    "REPOSITORY_REGISTRY_SCHEMA",
    "WORKSTREAM_REGISTRY_SCHEMA",
    "LargeProjectError",
    "append_workstream",
    "build_integration_gate",
    "build_integration_plan",
    "build_production_boundary",
    "build_repository",
    "build_workstream",
    "empty_repository_registry",
    "empty_workstream_registry",
    "enforce_workstream_budget",
    "production_status",
    "repository_for_task",
    "upsert_repository",
    "validate_integration_gate",
    "validate_integration_plan",
    "validate_production_boundary",
    "validate_repository_registry",
    "validate_workstream_registry",
    "verify_repository_binding",
    "workstream_dispatch_blockers",
    "workstream_statuses",
]
