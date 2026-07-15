from __future__ import annotations

"""Opt-in SQLite control store and crash-safe legacy view materialization.

The scheduler activates these transaction hooks only after an explicit
migration writes ``state-backend.json``. Before that marker exists, legacy
JSON/JSONL remains authoritative even if a fully built database was left
behind by a crash. A malformed marker or missing enabled database fails closed.
"""

import hashlib
import json
import os
import shutil
import sqlite3
import tempfile
import threading
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from .paths import ProjectLayout


STORE_SCHEMA_VERSION = 1
BACKEND_NAME = "sqlite-wal"
DB_NAME = "state.db"
MARKER_NAME = "state-backend.json"
MIGRATION_BACKUP_DIR = "migration-backups"
FAULT_ENV = "COSTMARSHAL_CONTROL_STORE_FAULT"


class ControlStoreError(RuntimeError):
    pass


class ControlStoreNotEnabled(ControlStoreError):
    pass


class ControlStoreConflict(ControlStoreError):
    pass


_LOCAL = threading.local()


def _now() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _sha256_text(value: str) -> str:
    return _sha256_bytes(value.encode("utf-8"))


def _fault(name: str) -> None:
    """Hard-exit fault injection used by subprocess crash tests."""

    if os.environ.get(FAULT_ENV) == name:
        os._exit(86)  # noqa: PLW1510 - intentionally bypass cleanup/rollback handlers


def database_path(layout: ProjectLayout) -> Path:
    return layout.scheduler_dir / DB_NAME


def marker_path(layout: ProjectLayout) -> Path:
    return layout.project_dir / MARKER_NAME


def _connect_path(path: Path, *, wal: bool) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path, timeout=30.0, isolation_level=None)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA trusted_schema=OFF")
    connection.execute("PRAGMA foreign_keys=ON")
    connection.execute("PRAGMA busy_timeout=30000")
    connection.execute("PRAGMA synchronous=FULL")
    mode = "WAL" if wal else "DELETE"
    actual_mode = str(connection.execute(f"PRAGMA journal_mode={mode}").fetchone()[0]).lower()
    if actual_mode != mode.lower():
        connection.close()
        raise ControlStoreError(f"SQLite refused journal mode {mode}: {actual_mode}")
    return connection


def _connect(layout: ProjectLayout) -> sqlite3.Connection:
    return _connect_path(database_path(layout), wal=True)


def _schema(connection: sqlite3.Connection) -> None:
    current = int(connection.execute("PRAGMA user_version").fetchone()[0])
    if current > STORE_SCHEMA_VERSION:
        raise ControlStoreError(
            f"control store schema {current} is newer than supported {STORE_SCHEMA_VERSION}"
        )
    connection.executescript(
        """
        CREATE TABLE IF NOT EXISTS meta (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        ) STRICT;

        CREATE TABLE IF NOT EXISTS documents (
            path TEXT PRIMARY KEY,
            content TEXT NOT NULL,
            content_sha256 TEXT NOT NULL,
            updated_at TEXT NOT NULL
        ) STRICT;

        CREATE TABLE IF NOT EXISTS ledger_entries (
            path TEXT NOT NULL,
            sequence INTEGER NOT NULL,
            entry_id TEXT NOT NULL,
            content TEXT NOT NULL,
            content_sha256 TEXT NOT NULL,
            created_at TEXT NOT NULL,
            PRIMARY KEY(path, sequence),
            UNIQUE(path, entry_id)
        ) STRICT;

        CREATE TABLE IF NOT EXISTS dirty_views (
            path TEXT PRIMARY KEY,
            kind TEXT NOT NULL CHECK(kind IN ('document', 'ledger')),
            revision INTEGER NOT NULL,
            updated_at TEXT NOT NULL
        ) STRICT;

        CREATE TABLE IF NOT EXISTS commands (
            command_id TEXT PRIMARY KEY,
            command_name TEXT NOT NULL,
            payload_json TEXT NOT NULL,
            payload_sha256 TEXT NOT NULL,
            status TEXT NOT NULL CHECK(status IN (
                'received', 'awaiting_effect', 'completed',
                'retryable_failed', 'permanent_failed'
            )),
            result_json TEXT,
            error_code TEXT,
            error_detail TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            completed_at TEXT
        ) STRICT;

        CREATE TABLE IF NOT EXISTS effects (
            effect_id TEXT PRIMARY KEY,
            command_id TEXT NOT NULL REFERENCES commands(command_id),
            effect_type TEXT NOT NULL,
            aggregate_id TEXT NOT NULL,
            generation INTEGER NOT NULL,
            payload_json TEXT NOT NULL,
            status TEXT NOT NULL CHECK(status IN (
                'pending', 'leased', 'observed', 'applied',
                'retryable_failed', 'dead'
            )),
            lease_owner TEXT,
            lease_expires_at TEXT,
            attempts INTEGER NOT NULL DEFAULT 0,
            result_json TEXT,
            last_error TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            UNIQUE(command_id, effect_type, aggregate_id, generation)
        ) STRICT;

        CREATE TABLE IF NOT EXISTS outbox (
            outbox_id TEXT PRIMARY KEY,
            command_id TEXT REFERENCES commands(command_id),
            channel TEXT NOT NULL,
            sender TEXT,
            recipient TEXT,
            task_id TEXT,
            payload_json TEXT NOT NULL,
            dedupe_key TEXT NOT NULL UNIQUE,
            status TEXT NOT NULL CHECK(status IN ('pending', 'leased', 'delivered', 'dead')),
            lease_owner TEXT,
            lease_expires_at TEXT,
            attempts INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL,
            delivered_at TEXT
        ) STRICT;

        CREATE TABLE IF NOT EXISTS events (
            sequence INTEGER PRIMARY KEY AUTOINCREMENT,
            event_id TEXT NOT NULL UNIQUE,
            command_id TEXT REFERENCES commands(command_id),
            event_type TEXT NOT NULL,
            aggregate_type TEXT,
            aggregate_id TEXT,
            payload_json TEXT NOT NULL,
            created_at TEXT NOT NULL
        ) STRICT;

        CREATE TABLE IF NOT EXISTS legacy_imports (
            source_path TEXT NOT NULL,
            source_sha256 TEXT NOT NULL,
            row_number INTEGER NOT NULL,
            imported_id TEXT NOT NULL,
            imported_at TEXT NOT NULL,
            PRIMARY KEY(source_path, source_sha256, row_number)
        ) STRICT;

        CREATE INDEX IF NOT EXISTS idx_effects_status
            ON effects(status, updated_at);
        CREATE INDEX IF NOT EXISTS idx_outbox_status
            ON outbox(status, created_at);
        """
    )
    connection.execute(f"PRAGMA user_version={STORE_SCHEMA_VERSION}")
    connection.execute(
        "INSERT INTO meta(key, value) VALUES('schema_version', ?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (str(STORE_SCHEMA_VERSION),),
    )


def _relative(layout: ProjectLayout, path: Path) -> str:
    try:
        return path.resolve().relative_to(layout.project_dir.resolve()).as_posix()
    except ValueError as exc:
        raise ControlStoreError(f"control path is outside project: {path}") from exc


def _controlled_file(layout: ProjectLayout, path: Path) -> bool:
    try:
        relative = path.resolve().relative_to(layout.project_dir.resolve())
    except ValueError:
        return False
    folded = {part.casefold() for part in relative.parts}
    excluded = {
        ".git",
        "worktrees",
        "actor-homes",
        "transcripts",
        "__pycache__",
        MIGRATION_BACKUP_DIR.casefold(),
    }
    if folded & excluded:
        return False
    relative_text = relative.as_posix()
    if relative_text in {MARKER_NAME, "project.json"}:
        return False
    name = path.name.casefold()
    if name == DB_NAME or name.startswith(f"{DB_NAME}.") or name in {
        f"{DB_NAME}-wal",
        f"{DB_NAME}-shm",
    }:
        return False
    if path.suffix.casefold() == ".lock":
        return False
    # Briefs, protocol, completion reports, attempt reports, and provider
    # output are ordinary artifacts.  Collect stores only their path/hash/size;
    # the transactional store owns mutable JSON and JSONL control state.
    return path.suffix.casefold() in {".json", ".jsonl"}


def _transaction_owns(layout: ProjectLayout, path: Path) -> bool:
    """Classify hook paths without capturing artifacts or external config.

    A path lexically outside the project, Markdown/report artifact, or excluded
    static/config area is simply not handled.  A lexically owned JSON view that
    resolves outside through a symlink is an invalid control path and fails
    closed rather than falling through to a legacy external write.
    """

    project_absolute = Path(os.path.abspath(layout.project_dir))
    path_absolute = Path(os.path.abspath(path))
    try:
        lexical = path_absolute.relative_to(project_absolute)
    except ValueError:
        return False
    folded = {part.casefold() for part in lexical.parts}
    excluded = {
        ".git",
        "worktrees",
        "actor-homes",
        "transcripts",
        "__pycache__",
        MIGRATION_BACKUP_DIR.casefold(),
    }
    if folded & excluded or lexical.as_posix() in {MARKER_NAME, "project.json"}:
        return False
    if path.suffix.casefold() not in {".json", ".jsonl"}:
        return False
    try:
        path.resolve().relative_to(layout.project_dir.resolve())
    except ValueError as exc:
        raise ControlStoreError(f"owned control path resolves outside project: {path}") from exc
    return True


def _manifest_entry(layout: ProjectLayout, path: Path) -> dict[str, Any]:
    raw = path.read_bytes()
    suffix = path.suffix.casefold()
    rows = 0
    if suffix == ".json":
        try:
            json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ControlStoreError(f"invalid JSON during migration preview: {_relative(layout, path)}") from exc
        kind = "document"
    elif suffix == ".jsonl":
        kind = "ledger"
        try:
            lines = raw.decode("utf-8").splitlines()
        except UnicodeDecodeError as exc:
            raise ControlStoreError(f"invalid UTF-8 JSONL: {_relative(layout, path)}") from exc
        for number, line in enumerate(lines, start=1):
            if not line.strip():
                continue
            try:
                json.loads(line)
            except json.JSONDecodeError as exc:
                raise ControlStoreError(
                    f"invalid JSONL during migration preview: {_relative(layout, path)} line {number}"
                ) from exc
            rows += 1
    else:
        try:
            raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ControlStoreError(f"invalid UTF-8 document: {_relative(layout, path)}") from exc
        kind = "document"
    return {
        "path": _relative(layout, path),
        "kind": kind,
        "sha256": _sha256_bytes(raw),
        "size": len(raw),
        "rows": rows,
    }


def _pid_is_alive(pid: Any) -> bool:
    try:
        value = int(pid)
    except (TypeError, ValueError):
        return False
    if value <= 0:
        return False
    try:
        os.kill(value, 0)
        return True
    except PermissionError:
        return True
    except OSError:
        return False


def _legacy_actor_blockers(layout: ProjectLayout) -> list[str]:
    blockers: list[str] = []
    for path in sorted(layout.actors_dir.glob("*.json")):
        try:
            actor = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            blockers.append(f"unreadable actor state {path.name}: {type(exc).__name__}")
            continue
        actor_id = str(actor.get("id") or path.stem)
        status = str(actor.get("status") or "")
        runtime = actor.get("runtime") if isinstance(actor.get("runtime"), dict) else {}
        pid = runtime.get("pid")
        if status in {"running", "starting"}:
            blockers.append(f"actor {actor_id} has active status {status}")
        elif _pid_is_alive(pid):
            blockers.append(f"actor {actor_id} still has a live runtime pid {pid}")
    return blockers


def preview_legacy_migration(layout: ProjectLayout) -> dict[str, Any]:
    entries = [
        _manifest_entry(layout, path)
        for path in sorted(layout.project_dir.rglob("*"))
        if path.is_file() and _controlled_file(layout, path)
    ]
    digest = _sha256_text(_canonical_json(entries))
    return {
        "status": "enabled" if marker_path(layout).is_file() else "preview",
        "schema_version": STORE_SCHEMA_VERSION,
        "manifest_sha256": digest,
        "file_count": len(entries),
        "ledger_row_count": sum(int(row["rows"]) for row in entries),
        "actor_blockers": _legacy_actor_blockers(layout),
        "entries": entries,
    }


def _write_atomic(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            "w", encoding="utf-8", delete=False, dir=str(path.parent), newline="\n"
        ) as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
            temporary = Path(handle.name)
        os.replace(temporary, path)
        temporary = None
        if os.name != "nt":
            descriptor = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def _copy_backup(layout: ProjectLayout, preview: dict[str, Any], migration_id: str) -> Path:
    backup = layout.project_dir / MIGRATION_BACKUP_DIR / migration_id
    if backup.exists():
        raise ControlStoreError(f"migration backup already exists: {backup}")
    backup.mkdir(parents=True)
    for entry in preview["entries"]:
        source = layout.project_dir / Path(str(entry["path"]))
        destination = backup / Path(str(entry["path"]))
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, destination)
        if _sha256_bytes(destination.read_bytes()) != entry["sha256"]:
            raise ControlStoreError(f"backup hash mismatch: {entry['path']}")
    manifest = {
        "migration_id": migration_id,
        "created_at": _now(),
        "manifest_sha256": preview["manifest_sha256"],
        "entries": preview["entries"],
    }
    _write_atomic(backup / "manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2) + "\n")
    return backup


def _bootstrap_database(
    connection: sqlite3.Connection,
    layout: ProjectLayout,
    preview: dict[str, Any],
    *,
    migration_id: str,
    backup: Path,
) -> None:
    _schema(connection)
    connection.execute("BEGIN IMMEDIATE")
    try:
        for entry in preview["entries"]:
            relative = str(entry["path"])
            source = layout.project_dir / Path(relative)
            text = source.read_text(encoding="utf-8")
            if entry["kind"] == "ledger":
                sequence = 0
                for row_number, line in enumerate(text.splitlines(), start=1):
                    if not line.strip():
                        continue
                    sequence += 1
                    value = json.loads(line)
                    content = _canonical_json(value)
                    imported_id = "legacy-" + _sha256_text(
                        f"{relative}\0{entry['sha256']}\0{row_number}\0{content}"
                    )
                    connection.execute(
                        "INSERT INTO ledger_entries(path, sequence, entry_id, content, content_sha256, created_at) "
                        "VALUES(?, ?, ?, ?, ?, ?)",
                        (relative, sequence, imported_id, content, _sha256_text(content), _now()),
                    )
                    connection.execute(
                        "INSERT INTO legacy_imports(source_path, source_sha256, row_number, imported_id, imported_at) "
                        "VALUES(?, ?, ?, ?, ?)",
                        (relative, entry["sha256"], row_number, imported_id, _now()),
                    )
            else:
                connection.execute(
                    "INSERT INTO documents(path, content, content_sha256, updated_at) VALUES(?, ?, ?, ?)",
                    (relative, text, _sha256_text(text), _now()),
                )
                imported_id = "legacy-" + _sha256_text(f"{relative}\0{entry['sha256']}")
                connection.execute(
                    "INSERT INTO legacy_imports(source_path, source_sha256, row_number, imported_id, imported_at) "
                    "VALUES(?, ?, 0, ?, ?)",
                    (relative, entry["sha256"], imported_id, _now()),
                )
        metadata = {
            "migration_id": migration_id,
            "migration_manifest_sha256": str(preview["manifest_sha256"]),
            "migration_backup": _relative(layout, backup),
            "cutover_state": "database_built",
        }
        for key, value in metadata.items():
            connection.execute(
                "INSERT INTO meta(key, value) VALUES(?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, value),
            )
        connection.commit()
    except BaseException:
        connection.rollback()
        raise


def _read_meta(connection: sqlite3.Connection, key: str) -> str | None:
    row = connection.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    return None if row is None else str(row["value"])


def _marker_payload(connection: sqlite3.Connection, preview: dict[str, Any]) -> dict[str, Any]:
    migration_id = _read_meta(connection, "migration_id")
    backup = _read_meta(connection, "migration_backup")
    if not migration_id or not backup:
        raise ControlStoreError("staged database is missing migration metadata")
    return {
        "backend": BACKEND_NAME,
        "schema_version": STORE_SCHEMA_VERSION,
        "database": f"scheduler/{DB_NAME}",
        "migration_id": migration_id,
        "legacy_manifest_sha256": preview["manifest_sha256"],
        "backup": backup,
        "cutover_at": _now(),
    }


def _read_marker(layout: ProjectLayout) -> dict[str, Any]:
    path = marker_path(layout)
    if not path.is_file():
        raise ControlStoreNotEnabled(
            f"SQLite control store is not enabled; explicit cutover marker is missing: {path}"
        )
    try:
        marker = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ControlStoreError(f"invalid control store marker: {path}") from exc
    if marker.get("backend") != BACKEND_NAME:
        raise ControlStoreError(f"unsupported control store backend: {marker.get('backend')}")
    if marker.get("database") != f"scheduler/{DB_NAME}":
        raise ControlStoreError("control store marker database path is not canonical")
    if int(marker.get("schema_version") or 0) != STORE_SCHEMA_VERSION:
        raise ControlStoreError("control store marker schema version mismatch")
    return marker


def _validate_connection(connection: sqlite3.Connection) -> list[str]:
    issues: list[str] = []
    integrity = [str(row[0]) for row in connection.execute("PRAGMA integrity_check").fetchall()]
    if integrity != ["ok"]:
        issues.extend(f"integrity: {row}" for row in integrity)
    for row in connection.execute("PRAGMA foreign_key_check").fetchall():
        issues.append(f"foreign-key: table={row[0]} rowid={row[1]} parent={row[2]} fk={row[3]}")
    version = int(connection.execute("PRAGMA user_version").fetchone()[0])
    if version != STORE_SCHEMA_VERSION:
        issues.append(f"schema version {version} != {STORE_SCHEMA_VERSION}")
    trusted = int(connection.execute("PRAGMA trusted_schema").fetchone()[0])
    if trusted != 0:
        issues.append("trusted_schema must be OFF")
    foreign_keys = int(connection.execute("PRAGMA foreign_keys").fetchone()[0])
    if foreign_keys != 1:
        issues.append("foreign_keys must be ON")
    synchronous = int(connection.execute("PRAGMA synchronous").fetchone()[0])
    if synchronous != 2:
        issues.append("synchronous must be FULL")
    for row in connection.execute(
        "SELECT command_id, payload_json, payload_sha256 FROM commands"
    ).fetchall():
        if _sha256_text(str(row["payload_json"])) != row["payload_sha256"]:
            issues.append(f"command payload hash mismatch: {row['command_id']}")
    for row in connection.execute("SELECT path, content, content_sha256 FROM documents").fetchall():
        if _sha256_text(str(row["content"])) != row["content_sha256"]:
            issues.append(f"document hash mismatch: {row['path']}")
    for row in connection.execute(
        "SELECT path, sequence, content, content_sha256 FROM ledger_entries"
    ).fetchall():
        if _sha256_text(str(row["content"])) != row["content_sha256"]:
            issues.append(f"ledger hash mismatch: {row['path']}#{row['sequence']}")
    return issues


def migrate_legacy_store(layout: ProjectLayout) -> dict[str, Any]:
    """Explicitly back up, import, validate, and atomically cut over a project.

    If a prior process died after installing the validated database but before
    the marker, the same call verifies the manifest and finishes the cutover.
    The caller must hold both the scheduler-instance lock and project write
    lock for the full preview/backup/cutover operation.
    """

    layout.scheduler_dir.mkdir(parents=True, exist_ok=True)
    if marker_path(layout).is_file():
        validation = validate_control_store(layout)
        if validation["status"] != "ok":
            raise ControlStoreError("enabled control store failed validation")
        reconcile_project_views(layout)
        return {"status": "already_enabled", **validation}

    preview = preview_legacy_migration(layout)
    if preview["actor_blockers"]:
        raise ControlStoreError(
            "migration requires a quiescent project; " + "; ".join(preview["actor_blockers"])
        )
    database = database_path(layout)
    resumed = database.is_file()
    if not resumed:
        migration_id = f"{datetime.now().strftime('%Y%m%d-%H%M%S')}-{preview['manifest_sha256'][:12]}"
        backup = _copy_backup(layout, preview, migration_id)
        second_preview = preview_legacy_migration(layout)
        if second_preview["manifest_sha256"] != preview["manifest_sha256"]:
            raise ControlStoreError("legacy project changed while migration backup was being created")
        temporary = layout.scheduler_dir / f"{DB_NAME}.migrating.{uuid.uuid4().hex}"
        connection = _connect_path(temporary, wal=False)
        try:
            _bootstrap_database(
                connection,
                layout,
                preview,
                migration_id=migration_id,
                backup=backup,
            )
            issues = _validate_connection(connection)
            if issues:
                raise ControlStoreError("staged control store is invalid: " + "; ".join(issues))
        finally:
            connection.close()
        # Windows CRT descriptors opened read-only cannot be fsync'd.
        with temporary.open("r+b") as handle:
            os.fsync(handle.fileno())
        os.replace(temporary, database)
    connection = _connect(layout)
    try:
        _schema(connection)
        expected_manifest = _read_meta(connection, "migration_manifest_sha256")
        if expected_manifest != preview["manifest_sha256"]:
            raise ControlStoreConflict(
                "staged database manifest does not match current legacy project; refusing cutover"
            )
        issues = _validate_connection(connection)
        if issues:
            raise ControlStoreError("control store validation failed: " + "; ".join(issues))
        marker = _marker_payload(connection, preview)
    finally:
        connection.close()
    _fault("migration.after_database_install_before_marker")
    _write_atomic(marker_path(layout), json.dumps(marker, ensure_ascii=False, indent=2) + "\n")
    _fault("migration.after_marker_before_materialize")
    reconcile_project_views(layout)
    return {
        "status": "enabled",
        "resumed": resumed,
        "database": str(database),
        "marker": str(marker_path(layout)),
        "migration_id": marker["migration_id"],
        "backup": str(layout.project_dir / Path(str(marker["backup"]))),
        "manifest_sha256": preview["manifest_sha256"],
    }


def _mark_dirty(connection: sqlite3.Connection, relative: str, kind: str) -> None:
    connection.execute(
        "INSERT INTO dirty_views(path, kind, revision, updated_at) VALUES(?, ?, 1, ?) "
        "ON CONFLICT(path) DO UPDATE SET kind=excluded.kind, "
        "revision=dirty_views.revision + 1, updated_at=excluded.updated_at",
        (relative, kind, _now()),
    )


def _materialize_one(
    connection: sqlite3.Connection,
    layout: ProjectLayout,
    relative: str,
    kind: str,
) -> None:
    path = (layout.project_dir / Path(relative)).resolve()
    if not _controlled_file(layout, path):
        raise ControlStoreError(f"database contains an unsafe materialized path: {relative}")
    if kind == "document":
        row = connection.execute("SELECT content FROM documents WHERE path=?", (relative,)).fetchone()
        if row is None:
            path.unlink(missing_ok=True)
        else:
            _write_atomic(path, str(row["content"]))
        return
    if kind == "ledger":
        rows = connection.execute(
            "SELECT content FROM ledger_entries WHERE path=? ORDER BY sequence", (relative,)
        ).fetchall()
        _write_atomic(path, "".join(f"{row['content']}\n" for row in rows))
        return
    raise ControlStoreError(f"unknown dirty view kind: {kind}")


def reconcile_project_views(layout: ProjectLayout) -> dict[str, Any]:
    """Rebuild every dirty compatibility view; safe after a hard crash."""

    _read_marker(layout)
    materialized: list[str] = []
    while True:
        connection = _connect(layout)
        try:
            _schema(connection)
            row = connection.execute(
                "SELECT path, kind, revision FROM dirty_views ORDER BY path LIMIT 1"
            ).fetchone()
            if row is None:
                break
            relative = str(row["path"])
            kind = str(row["kind"])
            revision = int(row["revision"])
            _materialize_one(connection, layout, relative, kind)
            _fault("materialize.after_file_before_ack")
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                "DELETE FROM dirty_views WHERE path=? AND revision=?", (relative, revision)
            )
            connection.commit()
            materialized.append(relative)
        finally:
            connection.close()
    return {"status": "ok", "materialized": materialized}


@dataclass
class ActiveTransaction:
    layout: ProjectLayout
    connection: sqlite3.Connection
    command_id: str
    command_name: str
    payload_sha256: str
    replay: bool = False
    replay_result: Any = None
    result: Any = None

    def owns(self, path: Path) -> bool:
        return _transaction_owns(self.layout, path)

    def _ensure_mutable(self) -> None:
        if self.replay:
            raise ControlStoreError("an idempotent replay cannot mutate control state")

    def _relative(self, path: Path) -> str:
        if not self.owns(path):
            raise ControlStoreError(f"path is not an owned compatibility view: {path}")
        relative = _relative(self.layout, path)
        return relative

    def set_result(self, value: Any) -> None:
        self._ensure_mutable()
        self.result = value

    def write_document(self, path: Path, content: str) -> None:
        self._ensure_mutable()
        relative = self._relative(path)
        self.connection.execute(
            "INSERT INTO documents(path, content, content_sha256, updated_at) VALUES(?, ?, ?, ?) "
            "ON CONFLICT(path) DO UPDATE SET content=excluded.content, "
            "content_sha256=excluded.content_sha256, updated_at=excluded.updated_at",
            (relative, content, _sha256_text(content), _now()),
        )
        _mark_dirty(self.connection, relative, "document")

    def delete_document(self, path: Path) -> None:
        self._ensure_mutable()
        relative = self._relative(path)
        self.connection.execute("DELETE FROM documents WHERE path=?", (relative,))
        _mark_dirty(self.connection, relative, "document")

    def read_document(self, path: Path) -> str | None:
        row = self.connection.execute(
            "SELECT content FROM documents WHERE path=?", (self._relative(path),)
        ).fetchone()
        return None if row is None else str(row["content"])

    def append_ledger(
        self,
        path: Path,
        row: dict[str, Any],
        *,
        entry_id: str | None = None,
    ) -> str:
        self._ensure_mutable()
        relative = self._relative(path)
        content = _canonical_json(row)
        sequence = int(
            self.connection.execute(
                "SELECT COALESCE(MAX(sequence), 0) + 1 FROM ledger_entries WHERE path=?",
                (relative,),
            ).fetchone()[0]
        )
        identifier = entry_id or "entry-" + _sha256_text(
            f"{self.command_id}\0{relative}\0{sequence}\0{content}"
        )
        self.connection.execute(
            "INSERT INTO ledger_entries(path, sequence, entry_id, content, content_sha256, created_at) "
            "VALUES(?, ?, ?, ?, ?, ?)",
            (relative, sequence, identifier, content, _sha256_text(content), _now()),
        )
        _mark_dirty(self.connection, relative, "ledger")
        return identifier

    def read_ledger(self, path: Path) -> list[dict[str, Any]]:
        rows = self.connection.execute(
            "SELECT content FROM ledger_entries WHERE path=? ORDER BY sequence",
            (self._relative(path),),
        ).fetchall()
        return [json.loads(str(row["content"])) for row in rows]

    def queue_outbox(
        self,
        *,
        outbox_id: str,
        channel: str,
        payload: dict[str, Any],
        dedupe_key: str,
        sender: str | None = None,
        recipient: str | None = None,
        task_id: str | None = None,
    ) -> None:
        self._ensure_mutable()
        self.connection.execute(
            "INSERT INTO outbox(outbox_id, command_id, channel, sender, recipient, task_id, "
            "payload_json, dedupe_key, status, created_at) "
            "VALUES(?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?)",
            (
                outbox_id,
                self.command_id,
                channel,
                sender,
                recipient,
                task_id,
                _canonical_json(payload),
                dedupe_key,
                _now(),
            ),
        )

    def emit_event(
        self,
        *,
        event_id: str,
        event_type: str,
        payload: dict[str, Any],
        aggregate_type: str | None = None,
        aggregate_id: str | None = None,
    ) -> None:
        self._ensure_mutable()
        self.connection.execute(
            "INSERT INTO events(event_id, command_id, event_type, aggregate_type, aggregate_id, "
            "payload_json, created_at) VALUES(?, ?, ?, ?, ?, ?, ?)",
            (
                event_id,
                self.command_id,
                event_type,
                aggregate_type,
                aggregate_id,
                _canonical_json(payload),
                _now(),
            ),
        )


def current_transaction() -> ActiveTransaction | None:
    return getattr(_LOCAL, "transaction", None)


@contextmanager
def control_transaction(
    layout: ProjectLayout,
    *,
    command_name: str,
    command_id: str,
    payload: dict[str, Any] | None = None,
) -> Iterator[ActiveTransaction]:
    """Apply one pure control command and its views in one SQLite transaction."""

    if current_transaction() is not None:
        raise ControlStoreError("nested control transactions are not supported in phase one")
    _read_marker(layout)
    reconcile_project_views(layout)
    connection = _connect(layout)
    payload_json = _canonical_json(payload or {})
    payload_sha256 = _sha256_text(payload_json)
    transaction = ActiveTransaction(
        layout=layout,
        connection=connection,
        command_id=command_id,
        command_name=command_name,
        payload_sha256=payload_sha256,
    )
    try:
        _schema(connection)
        connection.execute("BEGIN IMMEDIATE")
        existing = connection.execute(
            "SELECT command_name, payload_sha256, status, result_json FROM commands WHERE command_id=?",
            (command_id,),
        ).fetchone()
        if existing is not None:
            if existing["command_name"] != command_name or existing["payload_sha256"] != payload_sha256:
                connection.rollback()
                raise ControlStoreConflict(
                    f"command id {command_id} was reused with a different name or payload"
                )
            if existing["status"] != "completed":
                connection.rollback()
                raise ControlStoreError(
                    f"command {command_id} is not replayable in status {existing['status']}"
                )
            transaction.replay = True
            transaction.replay_result = (
                json.loads(str(existing["result_json"])) if existing["result_json"] is not None else None
            )
            connection.rollback()
            yield transaction
            return
        now = _now()
        connection.execute(
            "INSERT INTO commands(command_id, command_name, payload_json, payload_sha256, status, "
            "created_at, updated_at) VALUES(?, ?, ?, ?, 'received', ?, ?)",
            (command_id, command_name, payload_json, payload_sha256, now, now),
        )
        _LOCAL.transaction = transaction
        try:
            yield transaction
        except BaseException:
            connection.rollback()
            raise
        result_json = None if transaction.result is None else _canonical_json(transaction.result)
        connection.execute(
            "UPDATE commands SET status='completed', result_json=?, updated_at=?, completed_at=? "
            "WHERE command_id=?",
            (result_json, _now(), _now(), command_id),
        )
        connection.commit()
    finally:
        _LOCAL.transaction = None
        connection.close()
    _fault("transaction.after_commit_before_materialize")
    reconcile_project_views(layout)


def transactional_write_text(path: Path, content: str) -> bool:
    transaction = current_transaction()
    if transaction is None or not transaction.owns(path):
        return False
    transaction.write_document(path, content)
    return True


def transactional_read_text(path: Path) -> tuple[bool, str | None]:
    transaction = current_transaction()
    if transaction is None or not transaction.owns(path):
        return False, None
    return True, transaction.read_document(path)


def transactional_append_jsonl(path: Path, row: dict[str, Any]) -> bool:
    transaction = current_transaction()
    if transaction is None or not transaction.owns(path):
        return False
    transaction.append_ledger(path, row)
    return True


def transactional_read_jsonl(path: Path) -> tuple[bool, list[dict[str, Any]]]:
    transaction = current_transaction()
    if transaction is None or not transaction.owns(path):
        return False, []
    return True, transaction.read_ledger(path)


def validate_control_store(layout: ProjectLayout) -> dict[str, Any]:
    marker: dict[str, Any] | None = None
    issues: list[str] = []
    if marker_path(layout).is_file():
        try:
            marker = _read_marker(layout)
        except ControlStoreError as exc:
            issues.append(str(exc))
    elif not database_path(layout).is_file():
        return {"status": "absent", "issues": [], "schema_version": None}
    if not database_path(layout).is_file():
        issues.append("control store database is missing")
        return {"status": "invalid", "issues": issues, "schema_version": None}
    try:
        connection = _connect(layout)
        try:
            issues.extend(_validate_connection(connection))
            if marker is not None:
                manifest = _read_meta(connection, "migration_manifest_sha256")
                if manifest != marker.get("legacy_manifest_sha256"):
                    issues.append("marker/database migration manifest mismatch")
            counts = {
                table: int(connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
                for table in (
                    "documents",
                    "ledger_entries",
                    "dirty_views",
                    "commands",
                    "effects",
                    "outbox",
                )
            }
            journal_mode = str(connection.execute("PRAGMA journal_mode").fetchone()[0]).lower()
            if marker is not None and journal_mode != "wal":
                issues.append(f"journal_mode must be WAL, got {journal_mode}")
        finally:
            connection.close()
    except (sqlite3.Error, ControlStoreError) as exc:
        issues.append(f"database validation error: {type(exc).__name__}: {exc}")
        counts = {}
        journal_mode = None
    return {
        "status": "ok" if not issues and marker is not None else ("staged" if not issues else "invalid"),
        "issues": issues,
        "schema_version": STORE_SCHEMA_VERSION,
        "journal_mode": journal_mode,
        "counts": counts,
    }


def control_store_status(layout: ProjectLayout) -> dict[str, Any]:
    return validate_control_store(layout)


def control_store_pragmas(layout: ProjectLayout) -> dict[str, Any]:
    """Return the effective safety PRAGMAs on a store-owned connection."""

    _read_marker(layout)
    connection = _connect(layout)
    try:
        return {
            "journal_mode": str(connection.execute("PRAGMA journal_mode").fetchone()[0]).lower(),
            "synchronous": int(connection.execute("PRAGMA synchronous").fetchone()[0]),
            "trusted_schema": int(connection.execute("PRAGMA trusted_schema").fetchone()[0]),
            "foreign_keys": int(connection.execute("PRAGMA foreign_keys").fetchone()[0]),
        }
    finally:
        connection.close()


def control_store_enabled(layout: ProjectLayout) -> bool:
    """Return whether the explicit SQLite cutover marker is active.

    Missing marker means legacy JSON.  A present but malformed marker or a
    missing database raises instead of silently falling back to a second truth.
    """

    if not marker_path(layout).is_file():
        return False
    _read_marker(layout)
    if not database_path(layout).is_file():
        raise ControlStoreError("SQLite cutover marker exists but the control database is missing")
    return True


def initialize_control_store(layout: ProjectLayout, *, apply: bool = False) -> dict[str, Any]:
    """Preview by default; explicit ``apply=True`` performs the cutover."""

    return migrate_legacy_store(layout) if apply else preview_legacy_migration(layout)


__all__ = [
    "ActiveTransaction",
    "BACKEND_NAME",
    "ControlStoreConflict",
    "ControlStoreError",
    "ControlStoreNotEnabled",
    "DB_NAME",
    "FAULT_ENV",
    "MARKER_NAME",
    "STORE_SCHEMA_VERSION",
    "control_store_enabled",
    "control_store_pragmas",
    "control_store_status",
    "control_transaction",
    "current_transaction",
    "database_path",
    "initialize_control_store",
    "marker_path",
    "migrate_legacy_store",
    "preview_legacy_migration",
    "reconcile_project_views",
    "transactional_append_jsonl",
    "transactional_read_jsonl",
    "transactional_read_text",
    "transactional_write_text",
    "validate_control_store",
]
