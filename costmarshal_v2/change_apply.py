from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping

from .context_projection import (
    ChangeLimits,
    ContextProjectionError,
    build_cumulative_change_manifest,
)
from .locking import ProjectLockTimeout, advisory_file_lock
from .security import SecurityValidationError, normalize_path_list


class ChangeApplyError(RuntimeError):
    """Raised when leader preview/apply cannot preserve its Git CAS contract."""


_GIT_OID_RE = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})\Z")
_SHA256_RE = re.compile(r"sha256:[0-9a-f]{64}\Z")


@dataclass(frozen=True)
class PreparedChangePreview:
    repository: Path
    expected_head_sha: str
    base_sha: str
    write_scope: tuple[str, ...]
    change_manifest_sha256: str
    patch_path: Path
    patch_sha256: str
    patch_size_bytes: int
    candidate_tree_sha: str
    changed_paths: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        body = {
            "schema": "costmarshal-git-change-preview-v1",
            "repository": str(self.repository),
            "expected_head_sha": self.expected_head_sha,
            "base_sha": self.base_sha,
            "write_scope": list(self.write_scope),
            "change_manifest_sha256": self.change_manifest_sha256,
            "patch_path": str(self.patch_path),
            "patch_sha256": self.patch_sha256,
            "patch_size_bytes": self.patch_size_bytes,
            "candidate_tree_sha": self.candidate_tree_sha,
            "changed_paths": list(self.changed_paths),
        }
        body["preview_receipt_sha256"] = "sha256:" + hashlib.sha256(
            json.dumps(
                body,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            ).encode("utf-8")
        ).hexdigest()
        return body


def prepared_change_preview_from_dict(
    value: Mapping[str, Any],
    *,
    expected_repository: Path | str,
    expected_base_sha: str,
    expected_write_scope: Iterable[object],
    expected_change_manifest_sha256: str,
    expected_preview_root: Path | str,
) -> PreparedChangePreview:
    """Load a durable preview only when every authoritative binding matches."""

    if not isinstance(value, Mapping):
        raise ChangeApplyError("stored change preview is not an object")
    receipt = dict(value)
    expected_keys = {
        "schema",
        "repository",
        "expected_head_sha",
        "base_sha",
        "write_scope",
        "change_manifest_sha256",
        "patch_path",
        "patch_sha256",
        "patch_size_bytes",
        "candidate_tree_sha",
        "changed_paths",
        "preview_receipt_sha256",
    }
    if set(receipt) != expected_keys:
        raise ChangeApplyError("stored change preview has unknown or missing fields")
    claimed_hash = receipt.pop("preview_receipt_sha256", None)
    observed_hash = "sha256:" + hashlib.sha256(
        json.dumps(
            receipt,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    ).hexdigest()
    if claimed_hash != observed_hash:
        raise ChangeApplyError("stored change preview receipt hash is invalid")
    repository, _ = _repository_and_head(expected_repository)
    try:
        scopes = tuple(sorted(normalize_path_list(expected_write_scope, kind="allowed")))
    except SecurityValidationError as exc:
        raise ChangeApplyError(str(exc)) from exc
    if (
        receipt.get("schema") != "costmarshal-git-change-preview-v1"
        or receipt.get("repository") != str(repository)
        or receipt.get("base_sha") != expected_base_sha
        or receipt.get("expected_head_sha") != expected_base_sha
        or receipt.get("write_scope") != list(scopes)
        or receipt.get("change_manifest_sha256")
        != expected_change_manifest_sha256
    ):
        raise ChangeApplyError("stored change preview differs from the task contract")
    patch_sha256 = receipt.get("patch_sha256")
    patch_size = receipt.get("patch_size_bytes")
    candidate_tree = receipt.get("candidate_tree_sha")
    if (
        not isinstance(patch_sha256, str)
        or not _SHA256_RE.fullmatch(patch_sha256)
        or type(patch_size) is not int
        or patch_size < 0
        or not isinstance(candidate_tree, str)
        or not _GIT_OID_RE.fullmatch(candidate_tree)
    ):
        raise ChangeApplyError("stored change preview contains an invalid patch/tree receipt")
    preview_root = Path(expected_preview_root).expanduser().resolve()
    raw_patch_path = Path(str(receipt.get("patch_path") or "")).expanduser()
    if raw_patch_path.is_symlink():
        raise ChangeApplyError("stored preview patch must not be a symlink")
    patch_path = raw_patch_path.resolve()
    try:
        patch_path.relative_to(preview_root)
    except ValueError as exc:
        raise ChangeApplyError("stored preview patch escapes the preview root") from exc
    raw_changed_paths = receipt.get("changed_paths")
    if not isinstance(raw_changed_paths, list) or not all(
        isinstance(path, str) for path in raw_changed_paths
    ):
        raise ChangeApplyError("stored preview changed paths are invalid")
    changed_paths: list[str] = []
    for path in raw_changed_paths:
        try:
            normalized = normalize_path_list([path], kind="allowed")
        except SecurityValidationError as exc:
            raise ChangeApplyError(str(exc)) from exc
        if normalized != (path,):
            raise ChangeApplyError("stored preview changed path is not canonical")
        changed_paths.append(path)
    if changed_paths != sorted(set(changed_paths)):
        raise ChangeApplyError("stored preview changed paths are not sorted and unique")
    preview = PreparedChangePreview(
        repository=repository,
        expected_head_sha=expected_base_sha,
        base_sha=expected_base_sha,
        write_scope=scopes,
        change_manifest_sha256=expected_change_manifest_sha256,
        patch_path=patch_path,
        patch_sha256=patch_sha256,
        patch_size_bytes=patch_size,
        candidate_tree_sha=candidate_tree,
        changed_paths=tuple(changed_paths),
    )
    _read_verified_patch(preview)
    return preview


def _run_git(
    repository: Path,
    arguments: list[str],
    *,
    input_bytes: bytes | None = None,
    timeout_seconds: int = 60,
) -> bytes:
    environment = {
        key: value
        for key, value in os.environ.items()
        if not key.upper().startswith("GIT_")
    }
    environment.update(
        {
            "GIT_TERMINAL_PROMPT": "0",
        }
    )
    try:
        # Repository-local hooks and fsmonitor programs are executable code,
        # not preview evidence.  Override both for every Git subprocess so an
        # untrusted workspace cannot turn source inspection into host code
        # execution (worktree add would otherwise run post-checkout).
        with tempfile.TemporaryDirectory(prefix="costmarshal-disabled-hooks-") as hooks:
            result = subprocess.run(
                [
                    "git",
                    "-c",
                    f"core.hooksPath={hooks}",
                    "-c",
                    "core.fsmonitor=false",
                    "-c",
                    "core.untrackedCache=false",
                    "-C",
                    str(repository),
                    *arguments,
                ],
                input=input_bytes,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
                env=environment,
                timeout=timeout_seconds,
            )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ChangeApplyError(f"git operation failed: {arguments[0]}") from exc
    if result.returncode != 0:
        detail = result.stderr.decode("utf-8", errors="replace").strip()[:2048]
        raise ChangeApplyError(f"git {' '.join(arguments)} failed: {detail}")
    return result.stdout


def _repository_and_head(repository: Path | str) -> tuple[Path, str]:
    root = Path(repository).expanduser().resolve()
    if not root.is_dir():
        raise ChangeApplyError("apply repository does not exist")
    observed_root = Path(
        _run_git(root, ["rev-parse", "--show-toplevel"])
        .decode("utf-8", errors="strict")
        .strip()
    ).resolve()
    if observed_root != root:
        raise ChangeApplyError("apply repository must be the Git repository root")
    head = (
        _run_git(root, ["rev-parse", "--verify", "HEAD^{commit}"])
        .decode("ascii", errors="strict")
        .strip()
        .lower()
    )
    if not _GIT_OID_RE.fullmatch(head):
        raise ChangeApplyError("Git returned an invalid full HEAD id")
    return root, head


def _require_clean(repository: Path) -> None:
    status = _run_git(repository, ["status", "--porcelain=v1", "--untracked-files=all"])
    if status:
        raise ChangeApplyError("leader apply requires a completely clean Git worktree and index")


def _read_verified_patch(preview: PreparedChangePreview) -> bytes:
    """Read a preview patch only after bounded, descriptor-level receipt checks."""

    if not _SHA256_RE.fullmatch(preview.patch_sha256):
        raise ChangeApplyError("preview patch digest is invalid")
    if preview.patch_size_bytes < 0:
        raise ChangeApplyError("preview patch size is invalid")
    path = preview.patch_path
    if path.is_symlink() or not path.is_file():
        raise ChangeApplyError("preview patch is unavailable or not a regular file")
    try:
        with path.open("rb") as handle:
            before = os.fstat(handle.fileno())
            if not stat.S_ISREG(before.st_mode):
                raise ChangeApplyError("preview patch is not a regular file")
            if before.st_size != preview.patch_size_bytes:
                raise ChangeApplyError("preview patch size changed after review")
            payload = handle.read(preview.patch_size_bytes + 1)
            after = os.fstat(handle.fileno())
    except OSError as exc:
        raise ChangeApplyError("preview patch is unavailable") from exc
    if (
        before.st_dev != after.st_dev
        or before.st_ino != after.st_ino
        or before.st_size != after.st_size
        or len(payload) != preview.patch_size_bytes
    ):
        raise ChangeApplyError("preview patch changed while it was being read")
    if "sha256:" + hashlib.sha256(payload).hexdigest() != preview.patch_sha256:
        raise ChangeApplyError("preview patch bytes changed after review")
    if path.is_symlink() or not path.is_file():
        raise ChangeApplyError("preview patch changed while it was being read")
    return payload


def require_prepared_change_source_ready(preview: PreparedChangePreview) -> None:
    """Require the reviewed source HEAD and clean state to still be current."""

    repository, head = _repository_and_head(preview.repository)
    if repository != preview.repository or head != preview.expected_head_sha:
        raise ChangeApplyError("source HEAD changed after preview")
    _require_clean(repository)


def _git_common_dir(repository: Path) -> Path:
    common = Path(
        _run_git(repository, ["rev-parse", "--path-format=absolute", "--git-common-dir"])
        .decode("utf-8", errors="strict")
        .strip()
    ).resolve()
    if not common.is_dir():
        raise ChangeApplyError("Git common directory is unavailable")
    return common


def _canonical_manifest(
    manifest: Mapping[str, Any],
    *,
    base_sha: str,
    write_scope: tuple[str, ...],
    limits: ChangeLimits,
) -> dict[str, Any]:
    if not isinstance(manifest, Mapping):
        raise ChangeApplyError("change manifest must be an object")
    try:
        canonical = json.loads(
            json.dumps(
                dict(manifest),
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            )
        )
        validated = build_cumulative_change_manifest(
            base_sha=base_sha,
            write_scope=write_scope,
            operations=[],
            previous=canonical,
            limits=limits,
        )
    except (ContextProjectionError, TypeError, ValueError) as exc:
        raise ChangeApplyError(f"change manifest is invalid: {exc}") from exc
    if validated.manifest != canonical:
        raise ChangeApplyError("change manifest is not canonical")
    return canonical


def _assert_safe_target(root: Path, relative: str) -> Path:
    target = root.joinpath(*relative.split("/"))
    current = root
    for part in relative.split("/")[:-1]:
        current = current / part
        if current.exists() or current.is_symlink():
            info = current.lstat()
            if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
                raise ChangeApplyError(f"change target has a linked/non-directory ancestor: {relative}")
        else:
            current.mkdir()
    if target.exists() or target.is_symlink():
        info = target.lstat()
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
            raise ChangeApplyError(f"change target is not an ordinary file: {relative}")
    return target


def _read_blob(
    artifact_root: Path,
    entry: Mapping[str, Any],
    *,
    limits: ChangeLimits,
) -> bytes:
    digest = str(entry["blob_sha256"])
    blob_root = (artifact_root / "blobs" / "sha256").resolve()
    try:
        blob_root.relative_to(artifact_root)
    except ValueError as exc:
        raise ChangeApplyError("change blob store escapes its artifact root") from exc
    path = blob_root / digest.removeprefix("sha256:")
    try:
        path.resolve(strict=True).relative_to(blob_root)
    except (OSError, ValueError) as exc:
        raise ChangeApplyError(f"change blob escapes its artifact root: {entry['path']}") from exc
    if path.is_symlink() or not path.is_file():
        raise ChangeApplyError(f"change blob is unavailable: {entry['path']}")
    payload = path.read_bytes()
    if (
        len(payload) != entry["size_bytes"]
        or len(payload) > limits.max_blob_bytes
        or "sha256:" + hashlib.sha256(payload).hexdigest() != digest
    ):
        raise ChangeApplyError(f"change blob receipt mismatch: {entry['path']}")
    return payload


def _verify_candidate_index(
    repository: Path,
    manifest: Mapping[str, Any],
    artifact_root: Path,
    *,
    limits: ChangeLimits,
) -> None:
    """Prove that Git staged the exact requested mode and blob for every path."""

    for entry in manifest["changes"]:
        relative = str(entry["path"])
        index_row = _run_git(
            repository,
            ["ls-files", "--stage", "-z", "--", f":(top,literal){relative}"],
        )
        if entry["operation"] == "delete":
            if index_row:
                raise ChangeApplyError(f"deleted path remains in candidate index: {relative}")
            continue
        records = [record for record in index_row.split(b"\0") if record]
        if len(records) != 1:
            raise ChangeApplyError(f"upsert path is not unique in candidate index: {relative}")
        try:
            metadata, raw_path = records[0].split(b"\t", 1)
            mode_bytes, oid_bytes, stage_bytes = metadata.split(b" ", 2)
            mode = mode_bytes.decode("ascii", errors="strict")
            oid = oid_bytes.decode("ascii", errors="strict")
            stage = stage_bytes.decode("ascii", errors="strict")
            indexed_path = raw_path.decode("utf-8", errors="strict")
        except (UnicodeDecodeError, ValueError) as exc:
            raise ChangeApplyError(f"Git returned a malformed index row for {relative}") from exc
        payload = _read_blob(artifact_root, entry, limits=limits)
        expected_oid = (
            _run_git(repository, ["hash-object", "--stdin"], input_bytes=payload)
            .decode("ascii", errors="strict")
            .strip()
            .lower()
        )
        if (
            indexed_path != relative
            or stage != "0"
            or mode != entry["mode"]
            or oid != expected_oid
        ):
            raise ChangeApplyError(
                f"candidate index mode/blob differs from cumulative manifest: {relative}"
            )


def _staged_changed_paths(repository: Path, base_sha: str) -> tuple[str, ...]:
    return tuple(
        line.decode("utf-8", errors="strict")
        for line in _run_git(
            repository,
            ["diff", "--cached", "--name-only", "-z", base_sha, "--"],
        ).split(b"\0")
        if line
    )


def _binary_staged_patch(repository: Path, base_sha: str) -> bytes:
    return _run_git(
        repository,
        [
            "diff",
            "--cached",
            "--binary",
            "--full-index",
            "--no-ext-diff",
            base_sha,
            "--",
        ],
    )


def _verify_reviewed_patch_against_manifest(
    repository: Path,
    *,
    base_sha: str,
    manifest: Mapping[str, Any],
    artifact_root: Path,
    patch_payload: bytes,
    candidate_tree_sha: str,
    expected_changed_paths: tuple[str, ...],
    scratch_root: Path,
    limits: ChangeLimits,
) -> None:
    """Rebuild the patch in isolation before touching the leader worktree."""

    scratch_parent = Path(
        tempfile.mkdtemp(prefix=".apply-verify-", dir=str(scratch_root))
    )
    worktree = scratch_parent / "worktree"
    _run_git(repository, ["worktree", "add", "--detach", str(worktree), base_sha])
    try:
        if patch_payload:
            _run_git(worktree, ["apply", "--index", "--check", "-"], input_bytes=patch_payload)
            _run_git(worktree, ["apply", "--index", "-"], input_bytes=patch_payload)
        _verify_candidate_index(
            worktree,
            manifest,
            artifact_root,
            limits=limits,
        )
        observed_paths = _staged_changed_paths(worktree, base_sha)
        expected_manifest_paths = tuple(
            str(entry["path"]) for entry in manifest["changes"]
        )
        observed_tree = (
            _run_git(worktree, ["write-tree"])
            .decode("ascii", errors="strict")
            .strip()
            .lower()
        )
        regenerated_patch = _binary_staged_patch(worktree, base_sha)
        if (
            observed_paths != expected_changed_paths
            or observed_paths != expected_manifest_paths
            or observed_tree != candidate_tree_sha
            or regenerated_patch != patch_payload
        ):
            raise ChangeApplyError(
                "reviewed patch does not reproduce the exact cumulative manifest and candidate tree"
            )
    finally:
        try:
            _run_git(repository, ["worktree", "remove", "--force", str(worktree)])
        except ChangeApplyError:
            shutil.rmtree(worktree, ignore_errors=True)
            try:
                _run_git(repository, ["worktree", "prune"])
            except ChangeApplyError:
                pass
        shutil.rmtree(scratch_parent, ignore_errors=True)


def _apply_manifest_to_root(
    root: Path,
    manifest: Mapping[str, Any],
    artifact_root: Path,
    *,
    limits: ChangeLimits,
) -> None:
    for entry in manifest["changes"]:
        relative = str(entry["path"])
        target = _assert_safe_target(root, relative)
        if entry["operation"] == "delete":
            if target.exists():
                target.unlink()
            continue
        payload = _read_blob(artifact_root, entry, limits=limits)
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{target.name}.apply-", dir=str(target.parent)
        )
        temporary = Path(temporary_name)
        try:
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            temporary.chmod(0o755 if entry["mode"] == "100755" else 0o644)
            os.replace(temporary, target)
        finally:
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass


def prepare_change_preview(
    repository: Path | str,
    *,
    base_sha: str,
    write_scope: Iterable[object],
    change_manifest: Mapping[str, Any],
    change_artifact_root: Path | str,
    preview_root: Path | str,
    limits: ChangeLimits | None = None,
) -> PreparedChangePreview:
    """Build a reviewed binary patch and candidate tree off the exact task base."""

    effective_limits = limits or ChangeLimits()
    source, head = _repository_and_head(repository)
    _require_clean(source)
    if not isinstance(base_sha, str) or not _GIT_OID_RE.fullmatch(base_sha):
        raise ChangeApplyError("base_sha must be a full lowercase Git commit id")
    exact_base = (
        _run_git(source, ["rev-parse", "--verify", f"{base_sha}^{{commit}}"])
        .decode("ascii", errors="strict")
        .strip()
        .lower()
    )
    if exact_base != base_sha or head != base_sha:
        raise ChangeApplyError("preview requires clean source HEAD to equal the frozen task base")
    try:
        scopes = tuple(sorted(normalize_path_list(write_scope, kind="allowed")))
    except SecurityValidationError as exc:
        raise ChangeApplyError(str(exc)) from exc
    if not scopes:
        raise ChangeApplyError("change preview requires a non-empty write scope")
    manifest = _canonical_manifest(
        change_manifest,
        base_sha=base_sha,
        write_scope=scopes,
        limits=effective_limits,
    )
    artifact_root = Path(change_artifact_root).expanduser().resolve()
    if artifact_root.is_symlink() or not artifact_root.is_dir():
        raise ChangeApplyError("change artifact root is unavailable")
    destination_parent = Path(preview_root).expanduser().resolve()
    destination_parent.mkdir(parents=True, exist_ok=True)
    try:
        destination_parent.relative_to(source)
    except ValueError:
        pass
    else:
        raise ChangeApplyError("preview artifacts must be outside the source repository")
    staging_parent = Path(
        tempfile.mkdtemp(prefix=".preview-build-", dir=str(destination_parent))
    )
    destination = staging_parent / "worktree"
    _run_git(source, ["worktree", "add", "--detach", str(destination), base_sha])
    patch_payload = b""
    candidate_tree = ""
    changed_paths: tuple[str, ...] = ()
    try:
        _apply_manifest_to_root(
            destination,
            manifest,
            artifact_root,
            limits=effective_limits,
        )
        _run_git(destination, ["add", "-A", "--", *scopes])
        for entry in manifest["changes"]:
            if entry["operation"] == "upsert":
                _run_git(
                    destination,
                    [
                        "update-index",
                        "--chmod=+x" if entry["mode"] == "100755" else "--chmod=-x",
                        "--",
                        str(entry["path"]),
                    ],
                )
        _verify_candidate_index(
            destination,
            manifest,
            artifact_root,
            limits=effective_limits,
        )
        changed_paths = _staged_changed_paths(destination, base_sha)
        expected_paths = tuple(str(entry["path"]) for entry in manifest["changes"])
        if changed_paths != expected_paths:
            raise ChangeApplyError(
                "candidate Git tree changed a different path set than the cumulative manifest"
            )
        patch_payload = _binary_staged_patch(destination, base_sha)
        candidate_tree = (
            _run_git(destination, ["write-tree"])
            .decode("ascii", errors="strict")
            .strip()
            .lower()
        )
        if not _GIT_OID_RE.fullmatch(candidate_tree):
            raise ChangeApplyError("Git returned an invalid candidate tree id")
    finally:
        try:
            _run_git(source, ["worktree", "remove", "--force", str(destination)])
        except ChangeApplyError:
            if destination.exists() and destination.resolve().parent == staging_parent:
                shutil.rmtree(destination, ignore_errors=True)
            try:
                _run_git(source, ["worktree", "prune"])
            except ChangeApplyError:
                pass
        shutil.rmtree(staging_parent, ignore_errors=True)
    patch_sha256 = "sha256:" + hashlib.sha256(patch_payload).hexdigest()
    patch_root = destination_parent / "patches" / "sha256"
    patch_root.mkdir(parents=True, exist_ok=True)
    patch_path = patch_root / patch_sha256.removeprefix("sha256:")
    if patch_path.exists():
        if patch_path.is_symlink() or patch_path.read_bytes() != patch_payload:
            raise ChangeApplyError("content-addressed preview patch is corrupt")
    else:
        descriptor, temporary_name = tempfile.mkstemp(prefix=".patch-", dir=str(patch_root))
        temporary = Path(temporary_name)
        try:
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(patch_payload)
                handle.flush()
                os.fsync(handle.fileno())
            try:
                os.link(temporary, patch_path)
            except FileExistsError:
                if patch_path.read_bytes() != patch_payload:
                    raise ChangeApplyError("preview patch publication raced with different bytes")
        finally:
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass
    preview = PreparedChangePreview(
        repository=source,
        expected_head_sha=head,
        base_sha=base_sha,
        write_scope=scopes,
        change_manifest_sha256=str(manifest["manifest_sha256"]),
        patch_path=patch_path,
        patch_sha256=patch_sha256,
        patch_size_bytes=len(patch_payload),
        candidate_tree_sha=candidate_tree,
        changed_paths=changed_paths,
    )
    # Preview creation may take long enough for another process to mutate the
    # source.  Do not publish a review receipt for a source state that is
    # already stale at the point this command returns.
    require_prepared_change_source_ready(preview)
    return preview


def apply_prepared_change_preview(
    preview: PreparedChangePreview,
    *,
    expected_repository: Path | str,
    expected_base_sha: str,
    expected_write_scope: Iterable[object],
    expected_change_manifest_sha256: str,
    change_manifest: Mapping[str, Any],
    change_artifact_root: Path | str,
    scratch_root: Path | str | None = None,
    limits: ChangeLimits | None = None,
) -> dict[str, Any]:
    """Stage one manifest-proven patch under a repository-wide apply lease."""

    if not isinstance(preview, PreparedChangePreview):
        raise ChangeApplyError("preview must be a PreparedChangePreview")
    effective_limits = limits or ChangeLimits()
    repository, _ = _repository_and_head(expected_repository)
    try:
        scopes = tuple(sorted(normalize_path_list(expected_write_scope, kind="allowed")))
    except SecurityValidationError as exc:
        raise ChangeApplyError(str(exc)) from exc
    if (
        preview.repository != repository
        or preview.base_sha != expected_base_sha
        or preview.expected_head_sha != expected_base_sha
        or preview.write_scope != scopes
        or preview.change_manifest_sha256 != expected_change_manifest_sha256
    ):
        raise ChangeApplyError("preview differs from the authoritative repository/task contract")
    manifest = _canonical_manifest(
        change_manifest,
        base_sha=expected_base_sha,
        write_scope=scopes,
        limits=effective_limits,
    )
    if manifest.get("manifest_sha256") != expected_change_manifest_sha256:
        raise ChangeApplyError("authoritative change manifest hash differs from the preview")
    artifact_root = Path(change_artifact_root).expanduser().resolve()
    if artifact_root.is_symlink() or not artifact_root.is_dir():
        raise ChangeApplyError("change artifact root is unavailable")
    patch_payload = _read_verified_patch(preview)
    scratch = (
        Path(scratch_root).expanduser().resolve()
        if scratch_root is not None
        else preview.patch_path.parent.parent.parent.resolve()
    )
    scratch.mkdir(parents=True, exist_ok=True)
    try:
        scratch.relative_to(repository)
    except ValueError:
        pass
    else:
        raise ChangeApplyError("apply verification scratch root must be outside the repository")

    common_dir = _git_common_dir(repository)
    try:
        apply_lock = advisory_file_lock(
            common_dir / "costmarshal.apply.lock",
            timeout_seconds=15.0,
        )
        with apply_lock:
            locked_repository, head = _repository_and_head(repository)
            if locked_repository != repository or head != preview.expected_head_sha:
                raise ChangeApplyError("source HEAD changed after preview")
            _verify_reviewed_patch_against_manifest(
                repository,
                base_sha=expected_base_sha,
                manifest=manifest,
                artifact_root=artifact_root,
                patch_payload=patch_payload,
                candidate_tree_sha=preview.candidate_tree_sha,
                expected_changed_paths=preview.changed_paths,
                scratch_root=scratch,
                limits=effective_limits,
            )

            status = _run_git(
                repository,
                ["status", "--porcelain=v1", "-z", "--untracked-files=all"],
            )
            if status:
                staged_patch = _binary_staged_patch(repository, preview.expected_head_sha)
                unstaged = _run_git(
                    repository,
                    ["diff", "--binary", "--no-ext-diff", "--"],
                )
                untracked = _run_git(
                    repository,
                    ["ls-files", "--others", "--exclude-standard", "-z"],
                )
                conflicts = _run_git(repository, ["ls-files", "--unmerged", "-z"])
                staged_paths = _staged_changed_paths(
                    repository, preview.expected_head_sha
                )
                observed_tree = (
                    _run_git(repository, ["write-tree"])
                    .decode("ascii", errors="strict")
                    .strip()
                    .lower()
                )
                _, replay_head = _repository_and_head(repository)
                if (
                    staged_patch == patch_payload
                    and not unstaged
                    and not untracked
                    and not conflicts
                    and staged_paths == preview.changed_paths
                    and observed_tree == preview.candidate_tree_sha
                    and replay_head == preview.expected_head_sha
                ):
                    return {
                        "status": "already_applied",
                        "head_sha": replay_head,
                        "candidate_tree_sha": observed_tree,
                        "patch_sha256": preview.patch_sha256,
                        "staged": True,
                    }
                raise ChangeApplyError(
                    "source worktree/index is dirty with state different from the reviewed patch"
                )

            if not patch_payload:
                observed_tree = (
                    _run_git(repository, ["write-tree"])
                    .decode("ascii", errors="strict")
                    .strip()
                    .lower()
                )
                _, final_head = _repository_and_head(repository)
                if (
                    observed_tree != preview.candidate_tree_sha
                    or final_head != preview.expected_head_sha
                ):
                    raise ChangeApplyError(
                        "empty preview does not match the current candidate tree"
                    )
                return {
                    "status": "nothing_to_apply",
                    "head_sha": final_head,
                    "candidate_tree_sha": observed_tree,
                    "patch_sha256": preview.patch_sha256,
                    "staged": False,
                }

            _run_git(
                repository,
                ["apply", "--index", "--check", "-"],
                input_bytes=patch_payload,
            )
            _run_git(
                repository,
                ["apply", "--index", "-"],
                input_bytes=patch_payload,
            )
            observed_tree = (
                _run_git(repository, ["write-tree"])
                .decode("ascii", errors="strict")
                .strip()
                .lower()
            )
            staged_patch = _binary_staged_patch(repository, preview.expected_head_sha)
            unstaged = _run_git(
                repository,
                ["diff", "--binary", "--no-ext-diff", "--"],
            )
            untracked = _run_git(
                repository,
                ["ls-files", "--others", "--exclude-standard", "-z"],
            )
            conflicts = _run_git(repository, ["ls-files", "--unmerged", "-z"])
            staged_paths = _staged_changed_paths(repository, preview.expected_head_sha)
            _, final_head = _repository_and_head(repository)
            if (
                observed_tree != preview.candidate_tree_sha
                or staged_patch != patch_payload
                or unstaged
                or untracked
                or conflicts
                or staged_paths != preview.changed_paths
                or final_head != preview.expected_head_sha
            ):
                raise ChangeApplyError(
                    "apply outcome is uncertain because repository state changed concurrently"
                )
            return {
                "status": "applied",
                "head_sha": final_head,
                "candidate_tree_sha": observed_tree,
                "patch_sha256": preview.patch_sha256,
                "staged": True,
            }
    except ProjectLockTimeout as exc:
        raise ChangeApplyError("timed out waiting for the repository apply lease") from exc


__all__ = [
    "ChangeApplyError",
    "PreparedChangePreview",
    "apply_prepared_change_preview",
    "prepared_change_preview_from_dict",
    "prepare_change_preview",
]
