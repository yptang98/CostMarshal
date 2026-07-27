"""Safe project-level artifact references and immutable lineage.

CostMarshal owns only metadata inside its current runtime project. Source
artifacts are never copied, moved, overwritten, or deleted by this module.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Iterable, Mapping
from datetime import date
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from .paths import ProjectLayout
from .state import append_jsonl, now_iso, read_jsonl


PROJECT_ARTIFACT_SCHEMA = "costmarshal-project-artifact-v1"
ARTIFACT_LINEAGE_SCHEMA = "costmarshal-artifact-lineage-v1"
PROJECT_ARTIFACT_LIFECYCLES = frozenset(
    {"candidate", "accepted", "rejected", "superseded"}
)
PROJECT_ARTIFACT_KINDS = frozenset(
    {
        "generic",
        "summary",
        "skill-candidate",
        "charter",
        "architecture",
        "adr",
        "interface",
        "accepted-fact",
        "risk",
        "milestone-summary",
        "production-evidence",
    }
)
LOCAL_ARTIFACT_MAX_BYTES = 16 * 1024 * 1024
_SHA256_RE = re.compile(r"(?:sha256:)?([0-9a-f]{64})\Z")
_SAFE_NAME_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}\Z")
_ALLOWED_EXTERNAL_SCHEMES = frozenset({"https", "s3", "gs", "az", "ssh", "file"})


class ProjectArtifactError(ValueError):
    """Raised when project artifact metadata would be unsafe or ambiguous."""


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
        raise ProjectArtifactError("artifact metadata must be canonical JSON") from exc


def _digest(value: object) -> str:
    return "sha256:" + hashlib.sha256(_canonical_bytes(value)).hexdigest()


def _normalize_sha256(value: str) -> str:
    match = _SHA256_RE.fullmatch(str(value or ""))
    if not match:
        raise ProjectArtifactError("sha256 must be 64 lowercase hexadecimal characters")
    return "sha256:" + match.group(1)


def _normalize_name(value: str, label: str) -> str:
    result = str(value or "").strip()
    if not _SAFE_NAME_RE.fullmatch(result):
        raise ProjectArtifactError(f"{label} is invalid")
    return result


def _logical_bucket(value: str | None, name: str) -> str:
    if value:
        parts = str(value).replace("\\", "/").split("/")
        if (
            len(parts) != 3
            or not all(part.isdigit() for part in parts[:2])
            or "_" not in parts[2]
        ):
            raise ProjectArtifactError(
                "date bucket must use YYYY/MM/DD_name without changing source files"
            )
        day_text, logical_name = parts[2].split("_", 1)
        if not day_text.isdigit() or not _SAFE_NAME_RE.fullmatch(logical_name):
            raise ProjectArtifactError(
                "date bucket must use YYYY/MM/DD_name without changing source files"
            )
        try:
            date(int(parts[0]), int(parts[1]), int(day_text))
        except ValueError as exc:
            raise ProjectArtifactError("date bucket contains an invalid date") from exc
        return f"{parts[0]}/{parts[1]}/{day_text}_{logical_name}"
    today = date.today()
    return f"{today:%Y/%m/%d}_{name}"


def _allowed_roots(layout: ProjectLayout, project: Mapping[str, Any]) -> list[Path]:
    roots = [layout.project_dir.resolve()]
    for field in ("workspace", "source_project"):
        raw = project.get(field)
        if isinstance(raw, str) and raw.strip():
            roots.append(Path(raw).expanduser().resolve())
    for raw in project.get("additional_artifact_roots") or []:
        if isinstance(raw, str) and raw.strip():
            roots.append(Path(raw).expanduser().resolve())
    return roots


def _local_receipt(
    layout: ProjectLayout,
    project: Mapping[str, Any],
    raw_path: str,
) -> dict[str, Any]:
    source_path = Path(raw_path).expanduser()
    if source_path.is_symlink():
        raise ProjectArtifactError("local artifact must not be a symlink")
    try:
        path = source_path.resolve(strict=True)
    except OSError as exc:
        raise ProjectArtifactError(f"local artifact is unavailable: {exc}") from exc
    if not path.is_file():
        raise ProjectArtifactError("local artifact must be a regular non-symlink file")
    owner: Path | None = None
    for root in _allowed_roots(layout, project):
        try:
            path.relative_to(root)
            owner = root
            break
        except ValueError:
            continue
    if owner is None:
        raise ProjectArtifactError(
            "local artifact must stay inside the current runtime, workspace, or read-only source project"
        )
    size = path.stat().st_size
    if size > LOCAL_ARTIFACT_MAX_BYTES:
        raise ProjectArtifactError(
            "large artifacts are external-reference-only; register a non-secret URI, size, and sha256"
        )
    hasher = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            hasher.update(chunk)
    return {
        "mode": "local-reference",
        "path": str(path),
        "owner_root": str(owner),
        "size_bytes": size,
        "sha256": "sha256:" + hasher.hexdigest(),
    }


def _external_receipt(uri: str, *, size_bytes: int, sha256: str) -> dict[str, Any]:
    parsed = urlsplit(str(uri or ""))
    if (
        parsed.scheme.casefold() not in _ALLOWED_EXTERNAL_SCHEMES
        or parsed.query
        or parsed.fragment
        or parsed.username
        or parsed.password
    ):
        raise ProjectArtifactError(
            "external URI must use an allowed scheme and contain no credentials, query, or fragment"
        )
    if type(size_bytes) is not int or size_bytes < 0:
        raise ProjectArtifactError("external artifact size must be a non-negative integer")
    return {
        "mode": "external-reference",
        "uri": uri,
        "size_bytes": size_bytes,
        "sha256": _normalize_sha256(sha256),
    }


def validate_project_artifact(
    value: Mapping[str, Any],
    *,
    known_artifact_ids: Iterable[str] = (),
) -> dict[str, Any]:
    if not isinstance(value, Mapping) or value.get("schema_version") != PROJECT_ARTIFACT_SCHEMA:
        raise ProjectArtifactError("unsupported project artifact schema")
    row = json.loads(_canonical_bytes(dict(value)))
    expected = {
        "schema_version",
        "lineage_schema_version",
        "event_id",
        "command_id",
        "timestamp",
        "project_id",
        "artifact_id",
        "kind",
        "name",
        "lifecycle",
        "task_id",
        "date_bucket",
        "source_mutation",
        "storage",
        "derived_from",
        "metadata",
    }
    if set(row) != expected:
        raise ProjectArtifactError("project artifact has unknown or missing fields")
    if row.get("lineage_schema_version") != ARTIFACT_LINEAGE_SCHEMA:
        raise ProjectArtifactError("unsupported artifact lineage schema")
    for field in ("event_id", "command_id", "project_id", "artifact_id", "name"):
        if not isinstance(row.get(field), str) or not row[field]:
            raise ProjectArtifactError(f"{field} must be non-empty text")
    kind = row.get("kind")
    if kind not in PROJECT_ARTIFACT_KINDS:
        raise ProjectArtifactError(f"unsupported project artifact kind: {kind}")
    if row.get("lifecycle") not in PROJECT_ARTIFACT_LIFECYCLES:
        raise ProjectArtifactError("invalid project artifact lifecycle")
    if row.get("source_mutation") is not False:
        raise ProjectArtifactError("project artifacts must declare source_mutation=false")
    storage = row.get("storage")
    if not isinstance(storage, dict) or storage.get("mode") not in {
        "local-reference",
        "external-reference",
    }:
        raise ProjectArtifactError("project artifact storage receipt is invalid")
    _normalize_sha256(str(storage.get("sha256") or ""))
    if type(storage.get("size_bytes")) is not int or storage["size_bytes"] < 0:
        raise ProjectArtifactError("project artifact size is invalid")
    if storage["mode"] == "local-reference":
        if set(storage) != {
            "mode",
            "path",
            "owner_root",
            "size_bytes",
            "sha256",
        }:
            raise ProjectArtifactError("local artifact receipt fields are invalid")
        path = Path(str(storage.get("path") or ""))
        owner = Path(str(storage.get("owner_root") or ""))
        if not path.is_absolute() or not owner.is_absolute():
            raise ProjectArtifactError("local artifact receipt paths must be absolute")
        try:
            path.relative_to(owner)
        except ValueError as exc:
            raise ProjectArtifactError(
                "local artifact path escapes its recorded owner root"
            ) from exc
    else:
        if set(storage) != {"mode", "uri", "size_bytes", "sha256"}:
            raise ProjectArtifactError("external artifact receipt fields are invalid")
        if (
            _external_receipt(
                str(storage.get("uri") or ""),
                size_bytes=storage["size_bytes"],
                sha256=str(storage["sha256"]),
            )
            != storage
        ):
            raise ProjectArtifactError("external artifact receipt is not canonical")
    sources = row.get("derived_from")
    if not isinstance(sources, list) or any(not isinstance(item, str) for item in sources):
        raise ProjectArtifactError("derived_from must be a list of artifact ids")
    if len(sources) != len(set(sources)) or row["artifact_id"] in sources:
        raise ProjectArtifactError("artifact lineage must be unique and acyclic")
    known = set(known_artifact_ids)
    missing = [item for item in sources if item not in known]
    if missing:
        raise ProjectArtifactError(
            "artifact lineage references unknown artifacts: " + ", ".join(missing)
        )
    if kind in {"summary", "milestone-summary", "skill-candidate"} and not sources:
        raise ProjectArtifactError(f"{kind} requires non-empty derived_from lineage")
    if not isinstance(row.get("metadata"), dict):
        raise ProjectArtifactError("artifact metadata must be an object")
    return row


def build_project_artifact(
    *,
    layout: ProjectLayout,
    project: Mapping[str, Any],
    command_id: str,
    kind: str,
    name: str,
    lifecycle: str,
    local_path: str | None = None,
    external_uri: str | None = None,
    external_size_bytes: int | None = None,
    external_sha256: str | None = None,
    derived_from: Iterable[str] = (),
    task_id: str | None = None,
    date_bucket: str | None = None,
    metadata: Mapping[str, Any] | None = None,
    existing_rows: Iterable[Mapping[str, Any]] = (),
) -> dict[str, Any]:
    exact_name = _normalize_name(name, "artifact name")
    if kind not in PROJECT_ARTIFACT_KINDS:
        raise ProjectArtifactError(f"unsupported project artifact kind: {kind}")
    if lifecycle not in PROJECT_ARTIFACT_LIFECYCLES:
        raise ProjectArtifactError("invalid project artifact lifecycle")
    if bool(local_path) == bool(external_uri):
        raise ProjectArtifactError("choose exactly one local path or external URI")
    if local_path:
        storage = _local_receipt(layout, project, local_path)
    else:
        if external_size_bytes is None or external_sha256 is None:
            raise ProjectArtifactError(
                "external artifacts require explicit size and sha256"
            )
        storage = _external_receipt(
            str(external_uri),
            size_bytes=external_size_bytes,
            sha256=external_sha256,
        )
    sources = list(dict.fromkeys(str(item) for item in derived_from if str(item)))
    source_rows = list(existing_rows)
    latest_lifecycle: dict[str, str] = {}
    source_tasks: dict[str, str | None] = {}
    for item in source_rows:
        artifact_id = item.get("artifact_id")
        if isinstance(artifact_id, str) and artifact_id:
            latest_lifecycle[artifact_id] = str(item.get("lifecycle") or "")
            raw_task_id = item.get("task_id")
            source_tasks[artifact_id] = str(raw_task_id) if raw_task_id else None
    if kind in {"summary", "milestone-summary", "skill-candidate"}:
        unaccepted = [
            item for item in sources if latest_lifecycle.get(item) != "accepted"
        ]
        if unaccepted:
            raise ProjectArtifactError(
                f"{kind} sources must be accepted artifacts: "
                + ", ".join(unaccepted)
            )
    if kind == "skill-candidate":
        source_task_ids = {
            source_tasks.get(item) for item in sources if source_tasks.get(item)
        }
        if len(sources) < 2 or len(source_task_ids) < 2:
            raise ProjectArtifactError(
                "skill-candidate requires accepted evidence from two distinct tasks"
            )
    identity = {
        "project_id": project.get("project_id"),
        "kind": kind,
        "name": exact_name,
        "storage_sha256": storage["sha256"],
        "derived_from": sources,
    }
    artifact_id = "PART-" + _digest(identity).removeprefix("sha256:")[:32]
    event_material = {
        **identity,
        "artifact_id": artifact_id,
        "command_id": command_id,
        "lifecycle": lifecycle,
    }
    row = {
        "schema_version": PROJECT_ARTIFACT_SCHEMA,
        "lineage_schema_version": ARTIFACT_LINEAGE_SCHEMA,
        "event_id": "PAEV-" + _digest(event_material).removeprefix("sha256:")[:32],
        "command_id": command_id,
        "timestamp": now_iso(),
        "project_id": str(project.get("project_id") or ""),
        "artifact_id": artifact_id,
        "kind": kind,
        "name": exact_name,
        "lifecycle": lifecycle,
        "task_id": task_id,
        "date_bucket": _logical_bucket(date_bucket, exact_name),
        "source_mutation": False,
        "storage": storage,
        "derived_from": sources,
        "metadata": json.loads(_canonical_bytes(dict(metadata or {}))),
    }
    known_ids = {
        str(item.get("artifact_id"))
        for item in source_rows
        if isinstance(item, Mapping) and item.get("artifact_id")
    }
    return validate_project_artifact(row, known_artifact_ids=known_ids)


def append_project_artifact(layout: ProjectLayout, row: Mapping[str, Any]) -> bool:
    existing = read_jsonl(layout.artifacts_jsonl)
    if any(item.get("event_id") == row.get("event_id") for item in existing):
        return False
    if any(item.get("command_id") == row.get("command_id") for item in existing):
        raise ProjectArtifactError(
            "command id is already bound to a different project artifact"
        )
    known_ids = {
        str(item.get("artifact_id"))
        for item in existing
        if isinstance(item, dict) and item.get("artifact_id")
    }
    validated = validate_project_artifact(row, known_artifact_ids=known_ids)
    append_jsonl(layout.artifacts_jsonl, validated)
    return True


def verify_project_artifact_source(
    layout: ProjectLayout,
    project: Mapping[str, Any],
    row: Mapping[str, Any],
    *,
    known_artifact_ids: Iterable[str] = (),
) -> dict[str, Any]:
    """Fail closed when a referenced local source no longer matches its receipt."""

    validated = validate_project_artifact(
        row,
        known_artifact_ids=known_artifact_ids,
    )
    storage = validated["storage"]
    if storage["mode"] == "external-reference":
        return {"status": "external-unverified", "artifact_id": row["artifact_id"]}
    current = _local_receipt(layout, project, str(storage["path"]))
    if current != storage:
        raise ProjectArtifactError(
            f"local artifact source drifted from receipt: {row['artifact_id']}"
        )
    return {"status": "verified", "artifact_id": row["artifact_id"]}


def query_project_artifacts(
    rows: Iterable[Mapping[str, Any]],
    *,
    kind: str | None = None,
    lifecycle: str | None = None,
    task_id: str | None = None,
    date_bucket: str | None = None,
    model: str | None = None,
) -> list[dict[str, Any]]:
    source_rows = list(rows)
    result: list[dict[str, Any]] = []
    known_ids = {
        str(item.get("artifact_id"))
        for item in source_rows
        if isinstance(item, Mapping) and item.get("artifact_id")
    }
    for item in source_rows:
        if item.get("schema_version") != PROJECT_ARTIFACT_SCHEMA:
            continue
        row = validate_project_artifact(item, known_artifact_ids=known_ids)
        if kind is not None and row["kind"] != kind:
            continue
        if lifecycle is not None and row["lifecycle"] != lifecycle:
            continue
        if task_id is not None and row["task_id"] != task_id:
            continue
        if date_bucket is not None and row["date_bucket"] != date_bucket:
            continue
        if model is not None and row["metadata"].get("model") != model:
            continue
        result.append(row)
    return sorted(result, key=lambda item: (item["timestamp"], item["event_id"]))


__all__ = [
    "ARTIFACT_LINEAGE_SCHEMA",
    "LOCAL_ARTIFACT_MAX_BYTES",
    "PROJECT_ARTIFACT_KINDS",
    "PROJECT_ARTIFACT_LIFECYCLES",
    "PROJECT_ARTIFACT_SCHEMA",
    "ProjectArtifactError",
    "append_project_artifact",
    "build_project_artifact",
    "query_project_artifacts",
    "validate_project_artifact",
    "verify_project_artifact_source",
]
