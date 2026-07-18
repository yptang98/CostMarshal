"""Safe, deterministic context projections and cumulative change artifacts.

This module is deliberately independent from the scheduler and actor runtime.
It provides two building blocks for a future handoff protocol:

* materialize an allowlisted, immutable view of ordinary files from one exact
  Git commit without consulting the mutable worktree; and
* build and persist a base-relative cumulative upsert/delete manifest whose
  payloads live in a SHA-256 content-addressed store.

The module never checks out a tree and never copies ``.git`` or untracked
files.  All paths are validated with CostMarshal's cross-platform security
rules before filesystem mutation.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import tempfile
import time
import unicodedata
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from .security import (
    SecurityValidationError,
    normalize_allowed_path,
    normalize_claim_path,
)


class ContextProjectionError(ValueError):
    """Raised when a projection or change artifact would be unsafe."""


_GIT_OID_RE = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})\Z")
_SHA256_RE = re.compile(r"sha256:[0-9a-f]{64}\Z")
_REGULAR_GIT_MODES = frozenset({"100644", "100755"})
_SENSITIVE_DIRECTORY_NAMES = frozenset(
    {".aws", ".azure", ".gnupg", ".kube", ".ssh"}
)
_SENSITIVE_FILE_NAMES = frozenset(
    {
        ".npmrc",
        ".pypirc",
        "credentials",
        "credentials.json",
        "id_dsa",
        "id_ed25519",
        "id_ecdsa",
        "id_rsa",
        "secrets.env",
        "secrets.json",
        "secrets.yaml",
        "secrets.yml",
    }
)
_SENSITIVE_SUFFIXES = (".jks", ".kdbx", ".key", ".p12", ".pem", ".pfx")


@dataclass(frozen=True)
class ProjectionLimits:
    """Hard resource limits for one materialized context projection."""

    max_allowlist_entries: int = 256
    max_files: int = 4096
    max_file_bytes: int = 16 * 1024 * 1024
    max_total_bytes: int = 128 * 1024 * 1024
    max_git_metadata_bytes: int = 8 * 1024 * 1024
    max_manifest_bytes: int = 16 * 1024 * 1024
    max_directories: int = 4096
    git_timeout_seconds: int = 30

    def __post_init__(self) -> None:
        for field_name, value in self.__dict__.items():
            if type(value) is not int or value <= 0:
                raise ContextProjectionError(f"{field_name} must be a positive integer")


@dataclass(frozen=True)
class ChangeLimits:
    """Hard resource limits for one cumulative change manifest."""

    max_operations: int = 4096
    max_changes: int = 4096
    max_blob_bytes: int = 16 * 1024 * 1024
    max_total_upsert_bytes: int = 128 * 1024 * 1024

    def __post_init__(self) -> None:
        for field_name, value in self.__dict__.items():
            if type(value) is not int or value <= 0:
                raise ContextProjectionError(f"{field_name} must be a positive integer")


@dataclass(frozen=True)
class MaterializedContextProjection:
    artifact_root: Path
    files_root: Path
    manifest_path: Path
    manifest: Mapping[str, Any]


@dataclass(frozen=True)
class ChangeOperation:
    """One ordered mutation relative to the fixed task base commit."""

    path: str
    operation: Literal["upsert", "delete"]
    content: bytes | None = None
    mode: Literal["100644", "100755"] | None = None

    @classmethod
    def upsert(cls, path: str, content: bytes, *, executable: bool = False) -> "ChangeOperation":
        return cls(path=path, operation="upsert", content=content, mode="100755" if executable else "100644")

    @classmethod
    def delete(cls, path: str) -> "ChangeOperation":
        return cls(path=path, operation="delete")


@dataclass(frozen=True)
class PreparedChangeArtifact:
    """A canonical manifest plus newly supplied content-addressed blobs."""

    manifest: Mapping[str, Any]
    blobs: tuple[tuple[str, bytes], ...]

    @property
    def manifest_sha256(self) -> str:
        return str(self.manifest["manifest_sha256"])


@dataclass(frozen=True)
class PersistedChangeArtifact:
    artifact_root: Path
    manifest_path: Path
    manifest_sha256: str
    blob_paths: tuple[Path, ...]


@dataclass(frozen=True)
class _ScannedProjectionFile:
    path: str
    size_bytes: int
    sha256: str
    mode: str | None
    permission_bits: int | None
    content: bytes


def _canonical_json_bytes(value: object) -> bytes:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise ContextProjectionError("manifest data is not canonical JSON") from exc


def _sha256(payload: bytes) -> str:
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def _manifest_with_hash(body: Mapping[str, Any]) -> dict[str, Any]:
    result = dict(body)
    result["manifest_sha256"] = _sha256(_canonical_json_bytes(body))
    return result


def _manifest_body(manifest: Mapping[str, Any]) -> dict[str, Any]:
    body = dict(manifest)
    body.pop("manifest_sha256", None)
    return body


def _validate_self_hash(manifest: Mapping[str, Any], *, label: str) -> None:
    observed = manifest.get("manifest_sha256")
    if not isinstance(observed, str) or not _SHA256_RE.fullmatch(observed):
        raise ContextProjectionError(f"{label} manifest_sha256 is invalid")
    expected = _sha256(_canonical_json_bytes(_manifest_body(manifest)))
    if observed != expected:
        raise ContextProjectionError(f"{label} manifest hash does not match its canonical payload")


def _contains_control_character(value: str) -> bool:
    return any(ord(character) < 32 or ord(character) == 127 for character in value)


def is_sensitive_context_path(value: object) -> bool:
    """Return whether a normalized path is likely to contain credentials.

    Governance-reserved paths are rejected by the normal CostMarshal path
    validator.  This predicate covers common credential stores and private-key
    formats that are valid repository paths but unsafe to project by default.
    """

    try:
        normalized = normalize_allowed_path(value)
    except SecurityValidationError:
        return True
    parts = tuple(part.casefold() for part in normalized.split("/"))
    name = parts[-1]
    if any(part in _SENSITIVE_DIRECTORY_NAMES for part in parts):
        return True
    if name == ".env" or name.startswith(".env."):
        return True
    if name in _SENSITIVE_FILE_NAMES or name.endswith(_SENSITIVE_SUFFIXES):
        return True
    return len(parts) >= 2 and parts[-2:] == (".docker", "config.json")


def _normalize_projection_path(value: object, *, label: str, write_path: bool) -> str:
    try:
        normalized = (
            normalize_claim_path(value)
            if write_path
            else normalize_allowed_path(value)
        )
    except SecurityValidationError as exc:
        raise ContextProjectionError(f"unsafe {label}: {exc}") from exc
    if _contains_control_character(normalized):
        raise ContextProjectionError(f"{label} contains a control character")
    if is_sensitive_context_path(normalized):
        raise ContextProjectionError(f"{label} is sensitive and cannot be projected: {normalized}")
    return normalized


def _collision_key(path: str) -> str:
    return unicodedata.normalize("NFC", path).casefold()


def _assert_no_path_collisions(paths: Iterable[str], *, label: str) -> None:
    keyed = sorted((_collision_key(path), path) for path in paths)
    seen: dict[str, str] = {}
    for key, path in keyed:
        previous = seen.get(key)
        if previous is not None and previous != path:
            raise ContextProjectionError(
                f"{label} contains a case/Unicode collision: {previous!r} and {path!r}"
            )
        seen[key] = path
    for current in sorted(seen):
        components = current.split("/")
        for boundary in range(1, len(components)):
            previous = "/".join(components[:boundary])
            if previous in seen:
                raise ContextProjectionError(
                    f"{label} contains a file/directory collision: {seen[previous]!r} and {seen[current]!r}"
                )


def _normalize_path_set(
    values: Iterable[object],
    *,
    label: str,
    write_paths: bool,
    maximum: int,
    remove_redundant_children: bool,
    allow_empty: bool = False,
) -> tuple[str, ...]:
    normalized: list[str] = []
    keys: dict[str, str] = {}
    for value in values:
        if len(normalized) >= maximum:
            raise ContextProjectionError(f"{label} exceeds {maximum} entries")
        path = _normalize_projection_path(value, label=label, write_path=write_paths)
        key = _collision_key(path)
        previous = keys.get(key)
        if previous is not None:
            if previous != path:
                raise ContextProjectionError(
                    f"{label} contains a case/Unicode collision: {previous!r} and {path!r}"
                )
            continue
        keys[key] = path
        normalized.append(path)
    if not normalized and not allow_empty:
        raise ContextProjectionError(f"{label} must not be empty")
    normalized.sort()
    if remove_redundant_children:
        minimal: list[str] = []
        for path in sorted(normalized, key=lambda item: (item.count("/"), item)):
            if not any(path == parent or path.startswith(parent + "/") for parent in minimal):
                minimal.append(path)
        normalized = sorted(minimal)
    return tuple(normalized)


def _is_reparse_or_link(path: Path) -> bool:
    try:
        info = path.lstat()
    except OSError as exc:
        raise ContextProjectionError(f"path cannot be inspected safely: {path}") from exc
    return _stat_is_reparse_or_link(info)


def _stat_is_reparse_or_link(info: os.stat_result) -> bool:
    if stat.S_ISLNK(info.st_mode):
        return True
    attributes = getattr(info, "st_file_attributes", 0)
    reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    return bool(attributes & reparse_flag)


def _assert_existing_path_has_no_links(path: Path, *, label: str) -> Path:
    lexical = Path(os.path.abspath(os.path.expanduser(os.fspath(path))))
    if _contains_control_character(os.fspath(lexical)):
        raise ContextProjectionError(f"{label} contains a control character")
    components: list[Path] = []
    current = lexical
    while True:
        components.append(current)
        if current == current.parent:
            break
        current = current.parent
    for component in reversed(components):
        if component.exists() and _is_reparse_or_link(component):
            raise ContextProjectionError(f"{label} contains a symlink/reparse component: {component}")
    try:
        resolved = lexical.resolve(strict=True)
    except OSError as exc:
        raise ContextProjectionError(f"{label} does not exist: {lexical}") from exc
    return resolved


def _ensure_safe_directory(path: Path, *, label: str) -> Path:
    lexical = Path(os.path.abspath(os.path.expanduser(os.fspath(path))))
    if _contains_control_character(os.fspath(lexical)):
        raise ContextProjectionError(f"{label} contains a control character")
    missing: list[Path] = []
    current = lexical
    while not current.exists():
        missing.append(current)
        if current == current.parent:
            raise ContextProjectionError(f"{label} has no existing filesystem root")
        current = current.parent
    _assert_existing_path_has_no_links(current, label=label)
    if not current.is_dir():
        raise ContextProjectionError(f"{label} ancestor is not a directory: {current}")
    for directory in reversed(missing):
        try:
            directory.mkdir()
        except FileExistsError:
            pass
        if _is_reparse_or_link(directory) or not directory.is_dir():
            raise ContextProjectionError(f"{label} was replaced by an unsafe path: {directory}")
    return lexical


def _fsync_directory(path: Path) -> None:
    if os.name == "nt":
        return
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _read_regular_file(
    path: Path,
    *,
    expected_size: int | None = None,
    max_size: int | None = None,
) -> bytes:
    if not path.exists() or _is_reparse_or_link(path):
        raise ContextProjectionError(f"regular file is missing or linked: {path}")
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise ContextProjectionError(f"regular file cannot be opened: {path}") from exc
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode):
            raise ContextProjectionError(f"path is not a regular file: {path}")
        if expected_size is not None and info.st_size != expected_size:
            raise ContextProjectionError(f"regular file size mismatch: {path}")
        if max_size is not None and info.st_size > max_size:
            raise ContextProjectionError(f"regular file exceeds {max_size} bytes: {path}")
        chunks: list[bytes] = []
        remaining = info.st_size
        while remaining:
            chunk = os.read(descriptor, min(remaining, 1024 * 1024))
            if not chunk:
                raise ContextProjectionError(f"regular file was truncated: {path}")
            chunks.append(chunk)
            remaining -= len(chunk)
        payload = b"".join(chunks)
    finally:
        os.close(descriptor)
    return payload


def _install_content_addressed(path: Path, payload: bytes) -> None:
    """Install immutable bytes with create-only hard-link publication."""

    _ensure_safe_directory(path.parent, label="artifact directory")
    if path.exists():
        if _read_regular_file(path, expected_size=len(payload)) != payload:
            raise ContextProjectionError(f"content-addressed object already exists with different bytes: {path}")
        return
    descriptor, temporary_name = tempfile.mkstemp(prefix=".incoming-", dir=str(path.parent))
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.link(temporary, path)
        except FileExistsError:
            if _read_regular_file(path, expected_size=len(payload)) != payload:
                raise ContextProjectionError(
                    f"content-addressed object raced with different bytes: {path}"
                )
        except OSError as exc:
            raise ContextProjectionError(
                "filesystem does not support safe create-only content-addressed publication"
            ) from exc
        _fsync_directory(path.parent)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def _run_git(
    repository: Path,
    arguments: list[str],
    *,
    timeout_seconds: int,
    output_limit: int,
) -> bytes:
    environment = dict(os.environ)
    environment["GIT_OPTIONAL_LOCKS"] = "0"
    stderr_limit = 2 * 1024 * 1024
    with tempfile.TemporaryFile() as stdout_file, tempfile.TemporaryFile() as stderr_file:
        try:
            process = subprocess.Popen(
                ["git", "-C", str(repository), *arguments],
                stdout=stdout_file,
                stderr=stderr_file,
                env=environment,
            )
        except FileNotFoundError as exc:
            raise ContextProjectionError("git is required for context projection") from exc
        deadline = time.monotonic() + timeout_seconds
        try:
            while process.poll() is None:
                if time.monotonic() >= deadline:
                    process.kill()
                    process.wait()
                    raise ContextProjectionError("git command timed out during context projection")
                if os.fstat(stdout_file.fileno()).st_size > output_limit:
                    process.kill()
                    process.wait()
                    raise ContextProjectionError(f"git command output exceeded {output_limit} bytes")
                if os.fstat(stderr_file.fileno()).st_size > stderr_limit:
                    process.kill()
                    process.wait()
                    raise ContextProjectionError("git command error output exceeded the safety limit")
                time.sleep(0.005)
        except BaseException:
            if process.poll() is None:
                process.kill()
                process.wait()
            raise
        output_size = os.fstat(stdout_file.fileno()).st_size
        error_size = os.fstat(stderr_file.fileno()).st_size
        if output_size > output_limit:
            raise ContextProjectionError(f"git command output exceeded {output_limit} bytes")
        if error_size > stderr_limit:
            raise ContextProjectionError("git command error output exceeded the safety limit")
        if process.returncode != 0:
            stderr_file.seek(max(0, error_size - 2048))
            detail = stderr_file.read(2048).decode("utf-8", errors="replace").strip()
            raise ContextProjectionError(f"git command failed: {detail or process.returncode}")
        stdout_file.seek(0)
        return stdout_file.read(output_size)


def _validate_repository_and_base(
    repository: Path | str,
    base_sha: object,
    limits: ProjectionLimits,
) -> tuple[Path, str]:
    repository_path = _assert_existing_path_has_no_links(Path(repository), label="repository")
    if not repository_path.is_dir():
        raise ContextProjectionError("repository must be a directory")
    if not isinstance(base_sha, str) or not _GIT_OID_RE.fullmatch(base_sha):
        raise ContextProjectionError("base_sha must be a full lowercase 40- or 64-hex Git commit id")
    top = _run_git(
        repository_path,
        ["rev-parse", "--show-toplevel"],
        timeout_seconds=limits.git_timeout_seconds,
        output_limit=16 * 1024,
    ).decode("utf-8", errors="strict").strip()
    try:
        top_path = Path(top).resolve(strict=True)
    except OSError as exc:
        raise ContextProjectionError("git reported an invalid repository root") from exc
    if os.path.normcase(str(top_path)) != os.path.normcase(str(repository_path)):
        raise ContextProjectionError("repository must be the Git worktree root")
    resolved = _run_git(
        repository_path,
        ["rev-parse", "--verify", f"{base_sha}^{{commit}}"],
        timeout_seconds=limits.git_timeout_seconds,
        output_limit=256,
    ).decode("ascii", errors="strict").strip()
    if resolved != base_sha:
        raise ContextProjectionError("base_sha does not identify that exact commit")
    return repository_path, resolved


def _parse_tree_entries(payload: bytes) -> list[tuple[str, str, str, str]]:
    entries: list[tuple[str, str, str, str]] = []
    for raw_record in payload.split(b"\x00"):
        if not raw_record:
            continue
        try:
            metadata, raw_path = raw_record.split(b"\t", 1)
            mode_bytes, type_bytes, oid_bytes = metadata.split(b" ", 2)
            mode = mode_bytes.decode("ascii", errors="strict")
            object_type = type_bytes.decode("ascii", errors="strict")
            oid = oid_bytes.decode("ascii", errors="strict")
            path = raw_path.decode("utf-8", errors="strict")
        except (UnicodeDecodeError, ValueError) as exc:
            raise ContextProjectionError("git tree contains a malformed or non-UTF-8 entry") from exc
        if not _GIT_OID_RE.fullmatch(oid):
            raise ContextProjectionError(f"git tree contains an invalid object id for {path!r}")
        entries.append((mode, object_type, oid, path))
    return entries


def materialize_context_projection(
    repository: Path | str,
    *,
    base_sha: str,
    allowlist: Iterable[object],
    destination: Path | str,
    limits: ProjectionLimits | None = None,
    allow_empty: bool = False,
) -> MaterializedContextProjection:
    """Atomically materialize allowlisted ordinary files from ``base_sha``.

    ``destination`` is an artifact directory and must not exist.  Projected
    repository bytes are placed below ``destination/files``; the canonical
    manifest is written separately at ``destination/manifest.json``.
    """

    if type(allow_empty) is not bool:
        raise ContextProjectionError("allow_empty must be a boolean")
    effective_limits = limits or ProjectionLimits()
    allowed = _normalize_path_set(
        allowlist,
        label="context allowlist",
        write_paths=False,
        maximum=effective_limits.max_allowlist_entries,
        remove_redundant_children=True,
        allow_empty=allow_empty,
    )
    repository_path, exact_base = _validate_repository_and_base(
        repository, base_sha, effective_limits
    )
    # An empty pathspec means "the whole tree" to Git.  Never invoke ls-tree
    # in that shape; an explicitly admitted empty projection is constructed
    # without asking Git for any tree entries.
    if allowed:
        pathspecs = [f":(top,literal){path}" for path in allowed]
        raw_tree = _run_git(
            repository_path,
            ["ls-tree", "-r", "-z", "--full-tree", exact_base, "--", *pathspecs],
            timeout_seconds=effective_limits.git_timeout_seconds,
            output_limit=effective_limits.max_git_metadata_bytes,
        )
    else:
        raw_tree = b""
    entries = _parse_tree_entries(raw_tree)
    if len(entries) > effective_limits.max_files:
        raise ContextProjectionError(
            f"projection contains {len(entries)} files, exceeding {effective_limits.max_files}"
        )

    matched = {path: False for path in allowed}
    safe_entries: list[tuple[str, str, str]] = []
    for mode, object_type, oid, raw_path in entries:
        for allowed_path in allowed:
            if raw_path == allowed_path or raw_path.startswith(allowed_path + "/"):
                matched[allowed_path] = True
        normalized = _normalize_projection_path(
            raw_path, label="tracked context path", write_path=False
        )
        if normalized != raw_path:
            raise ContextProjectionError(
                f"tracked context path is not canonical across platforms: {raw_path!r}"
            )
        if mode == "120000":
            raise ContextProjectionError(f"tracked symlink is forbidden in context: {raw_path}")
        if mode == "160000" or object_type == "commit":
            raise ContextProjectionError(f"tracked submodule is forbidden in context: {raw_path}")
        if mode not in _REGULAR_GIT_MODES or object_type != "blob":
            raise ContextProjectionError(
                f"only tracked ordinary files may be projected: {raw_path} ({mode} {object_type})"
            )
        safe_entries.append((mode, oid, raw_path))
    missing = [path for path, was_matched in matched.items() if not was_matched]
    if missing:
        raise ContextProjectionError(
            "context allowlist did not match tracked entries at base_sha: " + ", ".join(missing)
        )
    _assert_no_path_collisions((path for _, _, path in safe_entries), label="projection")

    blob_cache: dict[str, bytes] = {}
    manifest_files: list[dict[str, Any]] = []
    total_bytes = 0
    for mode, oid, path in sorted(safe_entries, key=lambda item: item[2]):
        if oid not in blob_cache:
            size_payload = _run_git(
                repository_path,
                ["cat-file", "-s", oid],
                timeout_seconds=effective_limits.git_timeout_seconds,
                output_limit=128,
            )
            try:
                declared_size = int(size_payload.decode("ascii", errors="strict").strip())
            except (UnicodeDecodeError, ValueError) as exc:
                raise ContextProjectionError(f"git returned an invalid blob size for {path}") from exc
            if declared_size < 0 or declared_size > effective_limits.max_file_bytes:
                raise ContextProjectionError(
                    f"tracked file exceeds {effective_limits.max_file_bytes} bytes: {path}"
                )
            blob = _run_git(
                repository_path,
                ["cat-file", "blob", oid],
                timeout_seconds=effective_limits.git_timeout_seconds,
                output_limit=effective_limits.max_file_bytes,
            )
            if len(blob) != declared_size:
                raise ContextProjectionError(f"git blob size changed while reading {path}")
            blob_cache[oid] = blob
        blob = blob_cache[oid]
        total_bytes += len(blob)
        if total_bytes > effective_limits.max_total_bytes:
            raise ContextProjectionError(
                f"projection exceeds {effective_limits.max_total_bytes} total bytes"
            )
        manifest_files.append(
            {
                "path": path,
                "mode": mode,
                "size_bytes": len(blob),
                "sha256": _sha256(blob),
                "git_object_id": oid,
            }
        )

    manifest = _manifest_with_hash(
        {
            "schema_version": 1,
            "kind": "costmarshal-context-projection",
            "base_sha": exact_base,
            "allowlist": list(allowed),
            "file_count": len(manifest_files),
            "total_size_bytes": total_bytes,
            "files": manifest_files,
        }
    )
    destination_path = Path(os.path.abspath(os.path.expanduser(os.fspath(destination))))
    destination_parent = _ensure_safe_directory(
        destination_path.parent, label="projection destination parent"
    )
    lock_path = destination_parent / f".{destination_path.name}.projection.lock"
    try:
        lock_descriptor = os.open(lock_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError as exc:
        raise ContextProjectionError(
            f"projection destination is already being materialized: {destination_path}"
        ) from exc
    staging: Path | None = None
    try:
        os.close(lock_descriptor)
        if destination_path.exists() or destination_path.is_symlink():
            raise ContextProjectionError(f"projection destination must not exist: {destination_path}")
        staging = Path(tempfile.mkdtemp(prefix=f".{destination_path.name}.staging-", dir=str(destination_parent)))
        files_root = staging / "files"
        files_root.mkdir()
        by_path = {entry[2]: (entry[0], blob_cache[entry[1]]) for entry in safe_entries}
        for item in manifest_files:
            relative = str(item["path"])
            mode, payload = by_path[relative]
            output_path = files_root.joinpath(*relative.split("/"))
            output_path.parent.mkdir(parents=True, exist_ok=True)
            with output_path.open("xb") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            try:
                output_path.chmod(0o755 if mode == "100755" else 0o644)
            except OSError as exc:
                raise ContextProjectionError(f"cannot set projected file mode: {relative}") from exc
        with (staging / "manifest.json").open("xb") as handle:
            handle.write(_canonical_json_bytes(manifest) + b"\n")
            handle.flush()
            os.fsync(handle.fileno())
        _fsync_directory(staging)
        if destination_path.exists() or destination_path.is_symlink():
            raise ContextProjectionError(f"projection destination appeared during materialization: {destination_path}")
        os.rename(staging, destination_path)
        staging = None
        _fsync_directory(destination_parent)
    finally:
        if staging is not None:
            shutil.rmtree(staging, ignore_errors=True)
        try:
            lock_path.unlink()
        except FileNotFoundError:
            pass
    return MaterializedContextProjection(
        artifact_root=destination_path,
        files_root=destination_path / "files",
        manifest_path=destination_path / "manifest.json",
        manifest=manifest,
    )


def _validate_projection_manifest(
    manifest: Mapping[str, Any],
    *,
    limits: ProjectionLimits,
) -> tuple[str, tuple[str, ...], dict[str, dict[str, Any]]]:
    expected_keys = {
        "schema_version",
        "kind",
        "base_sha",
        "allowlist",
        "file_count",
        "total_size_bytes",
        "files",
        "manifest_sha256",
    }
    if set(manifest) != expected_keys:
        raise ContextProjectionError("projection manifest has unknown or missing fields")
    _validate_self_hash(manifest, label="projection")
    if manifest.get("schema_version") != 1 or manifest.get("kind") != "costmarshal-context-projection":
        raise ContextProjectionError("projection manifest schema or kind is unsupported")
    base_sha = manifest.get("base_sha")
    if not isinstance(base_sha, str) or not _GIT_OID_RE.fullmatch(base_sha):
        raise ContextProjectionError("projection manifest base_sha is invalid")
    raw_allowlist = manifest.get("allowlist")
    if not isinstance(raw_allowlist, list):
        raise ContextProjectionError("projection manifest allowlist must be a list")
    allowed = _normalize_path_set(
        raw_allowlist,
        label="context allowlist",
        write_paths=False,
        maximum=limits.max_allowlist_entries,
        remove_redundant_children=True,
        allow_empty=True,
    )
    if list(allowed) != raw_allowlist:
        raise ContextProjectionError("projection manifest allowlist is not canonical")
    raw_files = manifest.get("files")
    if not isinstance(raw_files, list) or len(raw_files) > limits.max_files:
        raise ContextProjectionError("projection manifest files are invalid or too numerous")
    matched = {path: False for path in allowed}
    files: dict[str, dict[str, Any]] = {}
    total_bytes = 0
    ordered_paths: list[str] = []
    expected_file_keys = {"path", "mode", "size_bytes", "sha256", "git_object_id"}
    for raw_entry in raw_files:
        if not isinstance(raw_entry, dict) or set(raw_entry) != expected_file_keys:
            raise ContextProjectionError("projection manifest file entry shape is invalid")
        raw_path = raw_entry.get("path")
        path = _normalize_projection_path(
            raw_path, label="tracked context path", write_path=False
        )
        if path != raw_path:
            raise ContextProjectionError(f"projection manifest path is not canonical: {raw_path!r}")
        covered = False
        for allowed_path in allowed:
            if path == allowed_path or path.startswith(allowed_path + "/"):
                matched[allowed_path] = True
                covered = True
        if not covered:
            raise ContextProjectionError(f"projection manifest path is outside its allowlist: {path}")
        mode = raw_entry.get("mode")
        size = raw_entry.get("size_bytes")
        digest = raw_entry.get("sha256")
        git_object_id = raw_entry.get("git_object_id")
        if mode not in _REGULAR_GIT_MODES:
            raise ContextProjectionError(f"projection manifest mode is invalid: {path}")
        if type(size) is not int or not 0 <= size <= limits.max_file_bytes:
            raise ContextProjectionError(f"projection manifest size is invalid: {path}")
        if not isinstance(digest, str) or not _SHA256_RE.fullmatch(digest):
            raise ContextProjectionError(f"projection manifest SHA-256 is invalid: {path}")
        if not isinstance(git_object_id, str) or not _GIT_OID_RE.fullmatch(git_object_id):
            raise ContextProjectionError(f"projection manifest Git object id is invalid: {path}")
        if path in files:
            raise ContextProjectionError(f"projection manifest repeats path: {path}")
        files[path] = dict(raw_entry)
        ordered_paths.append(path)
        total_bytes += size
    _assert_no_path_collisions(ordered_paths, label="projection manifest")
    if ordered_paths != sorted(ordered_paths):
        raise ContextProjectionError("projection manifest file entries are not sorted")
    missing = [path for path, was_matched in matched.items() if not was_matched]
    if missing:
        raise ContextProjectionError(
            "projection manifest allowlist has no tracked file: " + ", ".join(missing)
        )
    if manifest.get("file_count") != len(files) or manifest.get("total_size_bytes") != total_bytes:
        raise ContextProjectionError("projection manifest counters are inconsistent")
    if total_bytes > limits.max_total_bytes:
        raise ContextProjectionError("projection manifest exceeds the total byte limit")
    return base_sha, allowed, files


def _load_projection_artifact(
    artifact_root: Path | str,
    *,
    limits: ProjectionLimits,
) -> tuple[Path, dict[str, Any], str, tuple[str, ...], dict[str, dict[str, Any]]]:
    root = _assert_existing_path_has_no_links(Path(artifact_root), label="projection artifact")
    if not root.is_dir():
        raise ContextProjectionError("projection artifact must be a directory")
    root_entries: dict[str, tuple[Path, os.stat_result]] = {}
    try:
        entries = list(os.scandir(root))
    except OSError as exc:
        raise ContextProjectionError("projection artifact cannot be enumerated") from exc
    for entry in entries:
        try:
            info = entry.stat(follow_symlinks=False)
        except OSError as exc:
            raise ContextProjectionError(f"projection artifact entry cannot be inspected: {entry.name}") from exc
        if _stat_is_reparse_or_link(info):
            raise ContextProjectionError(f"projection artifact contains a symlink/reparse entry: {entry.name}")
        root_entries[entry.name] = (Path(entry.path), info)
    if set(root_entries) != {"files", "manifest.json"}:
        extras = sorted(set(root_entries) - {"files", "manifest.json"})
        missing = sorted({"files", "manifest.json"} - set(root_entries))
        detail = ", ".join([*(f"extra:{name}" for name in extras), *(f"missing:{name}" for name in missing)])
        raise ContextProjectionError(f"projection artifact root layout is invalid: {detail}")
    files_root, files_info = root_entries["files"]
    manifest_path, manifest_info = root_entries["manifest.json"]
    if not stat.S_ISDIR(files_info.st_mode) or not stat.S_ISREG(manifest_info.st_mode):
        raise ContextProjectionError("projection artifact files/manifest types are invalid")
    manifest_bytes = _read_regular_file(
        manifest_path,
        expected_size=manifest_info.st_size,
        max_size=limits.max_manifest_bytes,
    )
    try:
        manifest = json.loads(manifest_bytes.decode("utf-8", errors="strict"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ContextProjectionError("projection manifest is not valid UTF-8 JSON") from exc
    if not isinstance(manifest, dict):
        raise ContextProjectionError("projection manifest root must be an object")
    base_sha, allowed, files = _validate_projection_manifest(manifest, limits=limits)
    if manifest_bytes != _canonical_json_bytes(manifest) + b"\n":
        raise ContextProjectionError("projection manifest bytes are not canonical")
    return root, manifest, base_sha, allowed, files


def _assert_tree_path_collisions(files: Iterable[str], directories: Iterable[str]) -> None:
    file_paths = tuple(files)
    directory_paths = tuple(directories)
    keyed: dict[str, tuple[str, str]] = {}
    for kind, path in [*(('file', path) for path in file_paths), *(('directory', path) for path in directory_paths)]:
        key = _collision_key(path)
        previous = keyed.get(key)
        if previous is not None and previous != (kind, path):
            raise ContextProjectionError(
                f"projection tree contains a case/Unicode collision: {previous[1]!r} and {path!r}"
            )
        keyed[key] = (kind, path)
    for file_path in file_paths:
        file_key = _collision_key(file_path)
        for key, (_, path) in keyed.items():
            if key.startswith(file_key + "/"):
                raise ContextProjectionError(
                    f"projection tree contains a file/directory collision: {file_path!r} and {path!r}"
                )


def _scan_projection_files(
    files_root: Path,
    *,
    limits: ProjectionLimits,
) -> tuple[dict[str, _ScannedProjectionFile], set[str]]:
    scanned: dict[str, _ScannedProjectionFile] = {}
    directories: set[str] = set()
    total_bytes = 0
    stack: list[tuple[Path, str]] = [(files_root, "")]
    while stack:
        directory, prefix = stack.pop()
        try:
            entries = sorted(os.scandir(directory), key=lambda entry: entry.name)
        except OSError as exc:
            raise ContextProjectionError(f"projected directory cannot be enumerated: {directory}") from exc
        for entry in entries:
            raw_path = f"{prefix}/{entry.name}" if prefix else entry.name
            path = _normalize_projection_path(
                raw_path, label="materialized context path", write_path=False
            )
            if path != raw_path:
                raise ContextProjectionError(
                    f"materialized context path is not canonical across platforms: {raw_path!r}"
                )
            try:
                info = entry.stat(follow_symlinks=False)
            except OSError as exc:
                raise ContextProjectionError(f"materialized context path cannot be inspected: {path}") from exc
            if _stat_is_reparse_or_link(info):
                raise ContextProjectionError(f"materialized context contains a symlink/reparse entry: {path}")
            if stat.S_ISDIR(info.st_mode):
                directories.add(path)
                if len(directories) > limits.max_directories:
                    raise ContextProjectionError(
                        f"materialized context exceeds {limits.max_directories} directories"
                    )
                stack.append((Path(entry.path), path))
                continue
            if not stat.S_ISREG(info.st_mode):
                raise ContextProjectionError(f"materialized context contains a non-regular file: {path}")
            if len(scanned) >= limits.max_files:
                raise ContextProjectionError(f"materialized context exceeds {limits.max_files} files")
            payload = _read_regular_file(
                Path(entry.path),
                expected_size=info.st_size,
                max_size=limits.max_file_bytes,
            )
            total_bytes += len(payload)
            if total_bytes > limits.max_total_bytes:
                raise ContextProjectionError(
                    f"materialized context exceeds {limits.max_total_bytes} total bytes"
                )
            if os.name == "nt":
                executable_mode = None
                permission_bits = None
            else:
                permission_bits = stat.S_IMODE(info.st_mode)
                if permission_bits & 0o7000:
                    raise ContextProjectionError(
                        f"materialized context file has special permission bits: {path}"
                    )
                executable_mode = "100755" if permission_bits & 0o111 else "100644"
            scanned[path] = _ScannedProjectionFile(
                path=path,
                size_bytes=len(payload),
                sha256=_sha256(payload),
                mode=executable_mode,
                permission_bits=permission_bits,
                content=payload,
            )
    _assert_tree_path_collisions(scanned, directories)
    return scanned, directories


def _expected_projection_directories(paths: Iterable[str]) -> set[str]:
    expected: set[str] = set()
    for path in paths:
        parts = path.split("/")
        for boundary in range(1, len(parts)):
            expected.add("/".join(parts[:boundary]))
    return expected


def _validate_projection_binding(
    *,
    base_sha: str,
    allowlist: tuple[str, ...],
    expected_base_sha: object,
    expected_allowlist: Iterable[object],
    expected_manifest_sha256: object | None,
    limits: ProjectionLimits,
) -> None:
    if not isinstance(expected_base_sha, str) or not _GIT_OID_RE.fullmatch(expected_base_sha):
        raise ContextProjectionError("expected_base_sha must be a full lowercase Git commit id")
    if base_sha != expected_base_sha:
        raise ContextProjectionError("projection base_sha does not match the expected task base")
    expected_allowed = _normalize_path_set(
        expected_allowlist,
        label="expected context allowlist",
        write_paths=False,
        maximum=limits.max_allowlist_entries,
        remove_redundant_children=True,
        allow_empty=True,
    )
    if allowlist != expected_allowed:
        raise ContextProjectionError("projection allowlist does not match the expected task context")
    if expected_manifest_sha256 is not None:
        if not isinstance(expected_manifest_sha256, str) or not _SHA256_RE.fullmatch(
            expected_manifest_sha256
        ):
            raise ContextProjectionError("expected_manifest_sha256 is invalid")


def verify_materialized_context_projection(
    artifact_root: Path | str,
    *,
    expected_base_sha: str,
    expected_allowlist: Iterable[object],
    expected_manifest_sha256: str | None = None,
    limits: ProjectionLimits | None = None,
) -> MaterializedContextProjection:
    """Verify an immutable projection and its explicit task binding.

    Verification rejects extra root entries, extra files or directories,
    links/reparse points, sensitive paths, noncanonical manifest bytes, and any
    content, size, SHA-256, or executable-bit drift.  Windows does not expose a
    portable Git executable bit, so mode verification there remains bound to
    the immutable manifest while content and path checks stay strict.
    """

    effective_limits = limits or ProjectionLimits()
    root, manifest, base_sha, allowed, expected_files = _load_projection_artifact(
        artifact_root, limits=effective_limits
    )
    _validate_projection_binding(
        base_sha=base_sha,
        allowlist=allowed,
        expected_base_sha=expected_base_sha,
        expected_allowlist=expected_allowlist,
        expected_manifest_sha256=expected_manifest_sha256,
        limits=effective_limits,
    )
    if expected_manifest_sha256 is not None and manifest["manifest_sha256"] != expected_manifest_sha256:
        raise ContextProjectionError("projection manifest does not match the expected immutable identity")
    actual_files, actual_directories = _scan_projection_files(
        root / "files", limits=effective_limits
    )
    if set(actual_files) != set(expected_files):
        extras = sorted(set(actual_files) - set(expected_files))
        missing = sorted(set(expected_files) - set(actual_files))
        raise ContextProjectionError(
            f"projection file set drifted (extra={extras}, missing={missing})"
        )
    expected_directories = _expected_projection_directories(expected_files)
    if actual_directories != expected_directories:
        extras = sorted(actual_directories - expected_directories)
        missing = sorted(expected_directories - actual_directories)
        raise ContextProjectionError(
            f"projection directory set drifted (extra={extras}, missing={missing})"
        )
    for path, expected in expected_files.items():
        actual = actual_files[path]
        if actual.size_bytes != expected["size_bytes"] or actual.sha256 != expected["sha256"]:
            raise ContextProjectionError(f"projection content identity drifted: {path}")
        if actual.mode is not None and actual.mode != expected["mode"]:
            raise ContextProjectionError(f"projection executable mode drifted: {path}")
        expected_permissions = 0o755 if expected["mode"] == "100755" else 0o644
        if actual.permission_bits is not None and actual.permission_bits != expected_permissions:
            raise ContextProjectionError(f"projection permission mode drifted: {path}")
    return MaterializedContextProjection(
        artifact_root=root,
        files_root=root / "files",
        manifest_path=root / "manifest.json",
        manifest=manifest,
    )


def _path_is_in_scope(path: str, scopes: tuple[str, ...]) -> bool:
    return any(path == scope or path.startswith(scope + "/") for scope in scopes)


def _validate_change_manifest(
    manifest: Mapping[str, Any],
    *,
    limits: ChangeLimits,
) -> tuple[str, tuple[str, ...], dict[str, dict[str, Any]]]:
    expected_keys = {
        "schema_version",
        "kind",
        "base_sha",
        "write_scope",
        "change_count",
        "total_upsert_bytes",
        "changes",
        "manifest_sha256",
    }
    if set(manifest) != expected_keys:
        raise ContextProjectionError("previous change manifest has unknown or missing fields")
    _validate_self_hash(manifest, label="previous change")
    if manifest.get("schema_version") != 1 or manifest.get("kind") != "costmarshal-cumulative-changes":
        raise ContextProjectionError("previous change manifest schema or kind is unsupported")
    base_sha = manifest.get("base_sha")
    if not isinstance(base_sha, str) or not _GIT_OID_RE.fullmatch(base_sha):
        raise ContextProjectionError("previous change manifest base_sha is invalid")
    raw_scope = manifest.get("write_scope")
    if not isinstance(raw_scope, list):
        raise ContextProjectionError("previous change manifest write_scope must be a list")
    scopes = _normalize_path_set(
        raw_scope,
        label="write scope",
        write_paths=True,
        maximum=limits.max_changes,
        remove_redundant_children=True,
    )
    if list(scopes) != raw_scope:
        raise ContextProjectionError("previous change manifest write_scope is not canonical")
    raw_changes = manifest.get("changes")
    if not isinstance(raw_changes, list) or len(raw_changes) > limits.max_changes:
        raise ContextProjectionError("previous change manifest changes are invalid or too numerous")
    changes: dict[str, dict[str, Any]] = {}
    total = 0
    ordered_paths: list[str] = []
    for raw_entry in raw_changes:
        if not isinstance(raw_entry, dict):
            raise ContextProjectionError("previous change manifest entry must be an object")
        operation = raw_entry.get("operation")
        expected_entry_keys = (
            {"path", "operation"}
            if operation == "delete"
            else {"path", "operation", "mode", "size_bytes", "blob_sha256"}
        )
        if set(raw_entry) != expected_entry_keys or operation not in {"upsert", "delete"}:
            raise ContextProjectionError("previous change manifest entry shape is invalid")
        path = _normalize_projection_path(
            raw_entry.get("path"), label="change path", write_path=True
        )
        if path != raw_entry.get("path") or not _path_is_in_scope(path, scopes):
            raise ContextProjectionError(f"previous change path is noncanonical or outside write scope: {path}")
        if path in changes:
            raise ContextProjectionError(f"previous change manifest repeats path: {path}")
        entry = dict(raw_entry)
        if operation == "upsert":
            if entry.get("mode") not in _REGULAR_GIT_MODES:
                raise ContextProjectionError(f"previous change mode is invalid: {path}")
            size = entry.get("size_bytes")
            digest = entry.get("blob_sha256")
            if type(size) is not int or not 0 <= size <= limits.max_blob_bytes:
                raise ContextProjectionError(f"previous change size is invalid: {path}")
            if not isinstance(digest, str) or not _SHA256_RE.fullmatch(digest):
                raise ContextProjectionError(f"previous change blob hash is invalid: {path}")
            total += size
        changes[path] = entry
        ordered_paths.append(path)
    _assert_no_path_collisions(ordered_paths, label="previous change manifest")
    if ordered_paths != sorted(ordered_paths):
        raise ContextProjectionError("previous change manifest entries are not sorted")
    if manifest.get("change_count") != len(changes) or manifest.get("total_upsert_bytes") != total:
        raise ContextProjectionError("previous change manifest counters are inconsistent")
    if total > limits.max_total_upsert_bytes:
        raise ContextProjectionError("previous change manifest exceeds the total byte limit")
    return base_sha, scopes, changes


def _prepare_change_artifact_from_entries(
    *,
    base_sha: str,
    scopes: tuple[str, ...],
    changes: Mapping[str, Mapping[str, Any]],
    supplied_blobs: Mapping[str, bytes],
    limits: ChangeLimits,
) -> PreparedChangeArtifact:
    if len(changes) > limits.max_changes:
        raise ContextProjectionError(f"cumulative changes exceed {limits.max_changes}")
    _assert_no_path_collisions(changes, label="cumulative change manifest")
    ordered: list[dict[str, Any]] = []
    total_bytes = 0
    for path in sorted(changes):
        normalized = _normalize_projection_path(path, label="change path", write_path=True)
        if normalized != path or not _path_is_in_scope(path, scopes):
            raise ContextProjectionError(f"cumulative change path is invalid or outside write scope: {path}")
        entry = dict(changes[path])
        if entry.get("path") != path or entry.get("operation") not in {"upsert", "delete"}:
            raise ContextProjectionError(f"cumulative change entry is malformed: {path}")
        if entry["operation"] == "delete":
            if set(entry) != {"path", "operation"}:
                raise ContextProjectionError(f"cumulative delete entry is malformed: {path}")
        else:
            if set(entry) != {"path", "operation", "mode", "size_bytes", "blob_sha256"}:
                raise ContextProjectionError(f"cumulative upsert entry is malformed: {path}")
            if entry["mode"] not in _REGULAR_GIT_MODES:
                raise ContextProjectionError(f"cumulative upsert mode is invalid: {path}")
            size = entry["size_bytes"]
            digest = entry["blob_sha256"]
            if type(size) is not int or not 0 <= size <= limits.max_blob_bytes:
                raise ContextProjectionError(f"cumulative upsert size is invalid: {path}")
            if not isinstance(digest, str) or not _SHA256_RE.fullmatch(digest):
                raise ContextProjectionError(f"cumulative upsert blob hash is invalid: {path}")
            total_bytes += size
        ordered.append(entry)
    if total_bytes > limits.max_total_upsert_bytes:
        raise ContextProjectionError(
            f"cumulative upserts exceed {limits.max_total_upsert_bytes} bytes"
        )
    manifest = _manifest_with_hash(
        {
            "schema_version": 1,
            "kind": "costmarshal-cumulative-changes",
            "base_sha": base_sha,
            "write_scope": list(scopes),
            "change_count": len(ordered),
            "total_upsert_bytes": total_bytes,
            "changes": ordered,
        }
    )
    referenced_digests = {
        str(entry["blob_sha256"])
        for entry in ordered
        if entry["operation"] == "upsert"
    }
    blobs: list[tuple[str, bytes]] = []
    for digest, payload in supplied_blobs.items():
        if digest not in referenced_digests:
            continue
        if not isinstance(payload, bytes) or len(payload) > limits.max_blob_bytes:
            raise ContextProjectionError("supplied cumulative blob is invalid or too large")
        if _sha256(payload) != digest:
            raise ContextProjectionError("supplied cumulative blob does not match its SHA-256")
        blobs.append((digest, payload))
    return PreparedChangeArtifact(manifest=manifest, blobs=tuple(sorted(blobs)))


def build_cumulative_change_manifest(
    *,
    base_sha: str,
    write_scope: Iterable[object],
    operations: Iterable[ChangeOperation],
    previous: Mapping[str, Any] | PreparedChangeArtifact | None = None,
    limits: ChangeLimits | None = None,
) -> PreparedChangeArtifact:
    """Purely fold ordered operations into a base-relative cumulative manifest."""

    effective_limits = limits or ChangeLimits()
    if not isinstance(base_sha, str) or not _GIT_OID_RE.fullmatch(base_sha):
        raise ContextProjectionError("base_sha must be a full lowercase 40- or 64-hex Git commit id")
    scopes = _normalize_path_set(
        write_scope,
        label="write scope",
        write_paths=True,
        maximum=effective_limits.max_changes,
        remove_redundant_children=True,
    )
    changes: dict[str, dict[str, Any]] = {}
    if previous is not None:
        previous_manifest = previous.manifest if isinstance(previous, PreparedChangeArtifact) else previous
        prior_base, prior_scopes, prior_changes = _validate_change_manifest(
            previous_manifest, limits=effective_limits
        )
        if prior_base != base_sha:
            raise ContextProjectionError("previous change manifest belongs to a different base_sha")
        if prior_scopes != scopes:
            raise ContextProjectionError("previous change manifest belongs to a different write scope")
        changes.update({path: dict(entry) for path, entry in prior_changes.items()})

    supplied_blobs: dict[str, bytes] = {}
    operation_count = 0
    for operation in operations:
        operation_count += 1
        if operation_count > effective_limits.max_operations:
            raise ContextProjectionError(
                f"change operation count exceeds {effective_limits.max_operations}"
            )
        if not isinstance(operation, ChangeOperation):
            raise ContextProjectionError("operations must contain ChangeOperation values")
        path = _normalize_projection_path(
            operation.path, label="change path", write_path=True
        )
        if not _path_is_in_scope(path, scopes):
            raise ContextProjectionError(f"change path is outside declared write scope: {path}")
        for existing_path in changes:
            if existing_path != path and _collision_key(existing_path) == _collision_key(path):
                raise ContextProjectionError(
                    f"change paths collide by case/Unicode: {existing_path!r} and {path!r}"
                )
        if operation.operation == "delete":
            if operation.content is not None or operation.mode is not None:
                raise ContextProjectionError(f"delete operation must not contain content or mode: {path}")
            changes[path] = {"path": path, "operation": "delete"}
        elif operation.operation == "upsert":
            if not isinstance(operation.content, bytes):
                raise ContextProjectionError(f"upsert content must be immutable bytes: {path}")
            if operation.mode not in _REGULAR_GIT_MODES:
                raise ContextProjectionError(f"upsert mode must be 100644 or 100755: {path}")
            if len(operation.content) > effective_limits.max_blob_bytes:
                raise ContextProjectionError(
                    f"upsert exceeds {effective_limits.max_blob_bytes} bytes: {path}"
                )
            digest = _sha256(operation.content)
            prior_payload = supplied_blobs.get(digest)
            if prior_payload is not None and prior_payload != operation.content:
                raise ContextProjectionError("SHA-256 collision detected while preparing change blobs")
            supplied_blobs[digest] = operation.content
            changes[path] = {
                "path": path,
                "operation": "upsert",
                "mode": operation.mode,
                "size_bytes": len(operation.content),
                "blob_sha256": digest,
            }
        else:
            raise ContextProjectionError(f"unsupported change operation: {operation.operation!r}")

    return _prepare_change_artifact_from_entries(
        base_sha=base_sha,
        scopes=scopes,
        changes=changes,
        supplied_blobs=supplied_blobs,
        limits=effective_limits,
    )


def capture_projection_changes(
    artifact_root: Path | str,
    *,
    expected_base_sha: str,
    expected_allowlist: Iterable[object],
    write_scope: Iterable[object],
    expected_manifest_sha256: str | None = None,
    previous: Mapping[str, Any] | PreparedChangeArtifact | None = None,
    projection_limits: ProjectionLimits | None = None,
    change_limits: ChangeLimits | None = None,
) -> PreparedChangeArtifact:
    """Capture the current ``files/`` tree as cumulative base-relative changes.

    The immutable projection manifest is verified and bound to the caller's
    exact base SHA and allowlist before scanning worker output.  Every observed
    add, modify, mode change, or deletion must be inside ``write_scope``.

    When ``previous`` is supplied, paths observable through this projection
    are rebuilt from the current base-relative state, so restoring a file to
    its base bytes correctly removes an older cumulative change.  A previous
    upsert outside the observable allowlist must still exist in the current
    tree; otherwise its state is ambiguous and capture fails closed.
    """

    effective_projection_limits = projection_limits or ProjectionLimits()
    effective_change_limits = change_limits or ChangeLimits()
    root, projection_manifest, base_sha, allowed, baseline_files = _load_projection_artifact(
        artifact_root, limits=effective_projection_limits
    )
    _validate_projection_binding(
        base_sha=base_sha,
        allowlist=allowed,
        expected_base_sha=expected_base_sha,
        expected_allowlist=expected_allowlist,
        expected_manifest_sha256=expected_manifest_sha256,
        limits=effective_projection_limits,
    )
    if (
        expected_manifest_sha256 is not None
        and projection_manifest["manifest_sha256"] != expected_manifest_sha256
    ):
        raise ContextProjectionError(
            "projection manifest does not match the expected immutable identity"
        )
    scopes = _normalize_path_set(
        write_scope,
        label="write scope",
        write_paths=True,
        maximum=effective_change_limits.max_changes,
        remove_redundant_children=True,
    )
    current_files, current_directories = _scan_projection_files(
        root / "files", limits=effective_projection_limits
    )
    allowed_directories = _expected_projection_directories(
        [*baseline_files, *current_files]
    )
    extra_directories = sorted(current_directories - allowed_directories)
    if extra_directories:
        raise ContextProjectionError(
            f"materialized context contains extra empty directories: {extra_directories}"
        )

    previous_changes: dict[str, dict[str, Any]] = {}
    if previous is not None:
        previous_manifest = previous.manifest if isinstance(previous, PreparedChangeArtifact) else previous
        prior_base, prior_scopes, prior_changes = _validate_change_manifest(
            previous_manifest, limits=effective_change_limits
        )
        if prior_base != base_sha:
            raise ContextProjectionError("previous change manifest belongs to a different base_sha")
        if prior_scopes != scopes:
            raise ContextProjectionError("previous change manifest belongs to a different write scope")
        previous_changes = {path: dict(entry) for path, entry in prior_changes.items()}

    desired_changes = {path: dict(entry) for path, entry in previous_changes.items()}
    supplied_blobs: dict[str, bytes] = {}

    def current_mode(path: str, current: _ScannedProjectionFile) -> str:
        if current.mode is not None:
            return current.mode
        baseline = baseline_files.get(path)
        if baseline is not None:
            return str(baseline["mode"])
        prior = previous_changes.get(path)
        if prior is not None and prior.get("operation") == "upsert":
            return str(prior["mode"])
        return "100644"

    for path, baseline in baseline_files.items():
        current = current_files.get(path)
        if current is None:
            if not _path_is_in_scope(path, scopes):
                raise ContextProjectionError(f"worker deleted a path outside write scope: {path}")
            desired_changes[path] = {"path": path, "operation": "delete"}
            continue
        mode = current_mode(path, current)
        unchanged = (
            current.size_bytes == baseline["size_bytes"]
            and current.sha256 == baseline["sha256"]
            and mode == baseline["mode"]
        )
        if unchanged:
            desired_changes.pop(path, None)
            continue
        if not _path_is_in_scope(path, scopes):
            raise ContextProjectionError(f"worker modified a path outside write scope: {path}")
        desired_changes[path] = {
            "path": path,
            "operation": "upsert",
            "mode": mode,
            "size_bytes": current.size_bytes,
            "blob_sha256": current.sha256,
        }
        supplied_blobs[current.sha256] = current.content

    for path, current in current_files.items():
        if path in baseline_files:
            continue
        if not _path_is_in_scope(path, scopes):
            raise ContextProjectionError(f"worker added a path outside write scope: {path}")
        mode = current_mode(path, current)
        desired_changes[path] = {
            "path": path,
            "operation": "upsert",
            "mode": mode,
            "size_bytes": current.size_bytes,
            "blob_sha256": current.sha256,
        }
        supplied_blobs[current.sha256] = current.content

    for path, prior in previous_changes.items():
        if path in baseline_files or path in current_files:
            continue
        if _path_is_in_scope(path, allowed):
            # The projection authoritatively observed this namespace and the
            # path is now absent.  A prior new-file upsert is therefore reset;
            # a no-op delete of a non-base path is dropped as well.
            desired_changes.pop(path, None)
            continue
        if prior["operation"] == "upsert":
            raise ContextProjectionError(
                f"previous upsert is outside the observable context and absent at capture: {path}"
            )
        # An unobserved prior delete remains valid and needs no payload.

    return _prepare_change_artifact_from_entries(
        base_sha=base_sha,
        scopes=scopes,
        changes=desired_changes,
        supplied_blobs=supplied_blobs,
        limits=effective_change_limits,
    )


def apply_cumulative_change_artifact(
    artifact_root: Path | str,
    *,
    expected_base_sha: str,
    expected_allowlist: Iterable[object],
    expected_projection_manifest_sha256: str,
    write_scope: Iterable[object],
    change_manifest: Mapping[str, Any],
    change_artifact_root: Path | str,
    expected_change_manifest_sha256: str,
    projection_limits: ProjectionLimits | None = None,
    change_limits: ChangeLimits | None = None,
) -> MaterializedContextProjection:
    """Idempotently apply one validated cumulative artifact to a projection.

    This never writes to the source repository.  A retry may resume after any
    individual file operation; the final capture proves that the complete tree
    equals the requested base-relative manifest and that no unrelated context
    path drifted.
    """

    effective_projection_limits = projection_limits or ProjectionLimits()
    effective_change_limits = change_limits or ChangeLimits()
    root, projection_manifest, base_sha, allowed, _ = _load_projection_artifact(
        artifact_root,
        limits=effective_projection_limits,
    )
    _validate_projection_binding(
        base_sha=base_sha,
        allowlist=allowed,
        expected_base_sha=expected_base_sha,
        expected_allowlist=expected_allowlist,
        expected_manifest_sha256=expected_projection_manifest_sha256,
        limits=effective_projection_limits,
    )
    if projection_manifest.get("manifest_sha256") != expected_projection_manifest_sha256:
        raise ContextProjectionError("projection manifest does not match the expected immutable identity")
    if not isinstance(change_manifest, Mapping):
        raise ContextProjectionError("change_manifest must be an object")
    canonical_changes = json.loads(_canonical_json_bytes(change_manifest))
    prior_base, prior_scopes, _ = _validate_change_manifest(
        canonical_changes,
        limits=effective_change_limits,
    )
    expected_scopes = _normalize_path_set(
        write_scope,
        label="write scope",
        write_paths=True,
        maximum=effective_change_limits.max_changes,
        remove_redundant_children=True,
    )
    if prior_base != base_sha or prior_scopes != expected_scopes:
        raise ContextProjectionError("cumulative change artifact belongs to a different task base or scope")
    if not isinstance(expected_change_manifest_sha256, str) or not _SHA256_RE.fullmatch(
        expected_change_manifest_sha256
    ):
        raise ContextProjectionError("expected change manifest hash is invalid")
    if canonical_changes.get("manifest_sha256") != expected_change_manifest_sha256:
        raise ContextProjectionError("cumulative change artifact hash does not match its receipt")

    cas_root = _ensure_safe_directory(
        Path(change_artifact_root),
        label="change artifact root",
    )
    files_root = root / "files"
    for entry in canonical_changes["changes"]:
        path = str(entry["path"])
        target = files_root.joinpath(*path.split("/"))
        if entry["operation"] == "delete":
            if target.exists() or target.is_symlink():
                if _is_reparse_or_link(target) or not target.is_file():
                    raise ContextProjectionError(f"delete target is not an ordinary file: {path}")
                target.unlink()
                parent = target.parent
                while parent != files_root:
                    try:
                        parent.rmdir()
                    except OSError:
                        break
                    parent = parent.parent
            continue

        digest = str(entry["blob_sha256"])
        blob_path = cas_root / "blobs" / "sha256" / digest.removeprefix("sha256:")
        payload = _read_regular_file(
            blob_path,
            expected_size=int(entry["size_bytes"]),
            max_size=effective_change_limits.max_blob_bytes,
        )
        if _sha256(payload) != digest:
            raise ContextProjectionError(f"change artifact blob hash mismatch: {path}")
        parent = _ensure_safe_directory(target.parent, label=f"change target parent for {path}")
        if target.exists() or target.is_symlink():
            if _is_reparse_or_link(target) or not target.is_file():
                raise ContextProjectionError(f"upsert target is not an ordinary file: {path}")
        descriptor, staging_name = tempfile.mkstemp(prefix=f".{target.name}.apply-", dir=str(parent))
        staging = Path(staging_name)
        try:
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            staging.chmod(0o755 if entry["mode"] == "100755" else 0o644)
            os.replace(staging, target)
            _fsync_directory(parent)
        finally:
            try:
                staging.unlink()
            except FileNotFoundError:
                pass

    observed = capture_projection_changes(
        root,
        expected_base_sha=expected_base_sha,
        expected_allowlist=expected_allowlist,
        write_scope=expected_scopes,
        expected_manifest_sha256=expected_projection_manifest_sha256,
        previous=canonical_changes,
        projection_limits=effective_projection_limits,
        change_limits=effective_change_limits,
    )
    if observed.manifest != canonical_changes:
        raise ContextProjectionError("applied projection does not match the cumulative change manifest")
    return MaterializedContextProjection(
        artifact_root=root,
        files_root=files_root,
        manifest_path=root / "manifest.json",
        manifest=projection_manifest,
    )


def persist_change_artifact(
    artifact_root: Path | str,
    prepared: PreparedChangeArtifact,
    *,
    limits: ChangeLimits | None = None,
) -> PersistedChangeArtifact:
    """Atomically publish blobs and the exact canonical manifest payload.

    Manifest files omit the self-hash field so their filename SHA-256 is the
    hash of their exact bytes.  ``prepared.manifest`` retains the explicit
    self-hash for transport and validation.
    """

    if not isinstance(prepared, PreparedChangeArtifact):
        raise ContextProjectionError("prepared must be a PreparedChangeArtifact")
    effective_limits = limits or ChangeLimits()
    _validate_change_manifest(prepared.manifest, limits=effective_limits)
    root = _ensure_safe_directory(Path(artifact_root), label="change artifact root")
    supplied = dict(prepared.blobs)
    for digest, payload in supplied.items():
        if not isinstance(digest, str) or not _SHA256_RE.fullmatch(digest):
            raise ContextProjectionError("prepared blob hash is invalid")
        if not isinstance(payload, bytes) or len(payload) > effective_limits.max_blob_bytes:
            raise ContextProjectionError("prepared blob payload is invalid or too large")
        if _sha256(payload) != digest:
            raise ContextProjectionError("prepared blob bytes do not match their SHA-256")

    blob_paths: list[Path] = []
    for entry in prepared.manifest["changes"]:
        if entry["operation"] != "upsert":
            continue
        digest = str(entry["blob_sha256"])
        blob_path = root / "blobs" / "sha256" / digest.removeprefix("sha256:")
        payload = supplied.get(digest)
        if payload is not None:
            if len(payload) != entry["size_bytes"]:
                raise ContextProjectionError(f"prepared blob size does not match manifest: {entry['path']}")
            _install_content_addressed(blob_path, payload)
        else:
            existing = _read_regular_file(blob_path, expected_size=int(entry["size_bytes"]))
            if _sha256(existing) != digest:
                raise ContextProjectionError(f"existing blob does not match manifest: {entry['path']}")
        blob_paths.append(blob_path)

    body_bytes = _canonical_json_bytes(_manifest_body(prepared.manifest))
    manifest_sha256 = _sha256(body_bytes)
    if manifest_sha256 != prepared.manifest_sha256:
        raise ContextProjectionError("prepared manifest changed before persistence")
    manifest_path = root / "manifests" / f"{manifest_sha256.removeprefix('sha256:')}.json"
    _install_content_addressed(manifest_path, body_bytes)
    return PersistedChangeArtifact(
        artifact_root=root,
        manifest_path=manifest_path,
        manifest_sha256=manifest_sha256,
        blob_paths=tuple(sorted(set(blob_paths), key=str)),
    )


__all__ = [
    "ChangeLimits",
    "ChangeOperation",
    "ContextProjectionError",
    "MaterializedContextProjection",
    "PersistedChangeArtifact",
    "PreparedChangeArtifact",
    "ProjectionLimits",
    "apply_cumulative_change_artifact",
    "build_cumulative_change_manifest",
    "capture_projection_changes",
    "is_sensitive_context_path",
    "materialize_context_projection",
    "persist_change_artifact",
    "verify_materialized_context_projection",
]
