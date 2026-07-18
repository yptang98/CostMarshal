#!/usr/bin/env python3
"""Build or verify the committed, runtime-only Codex plugin snapshot."""

from __future__ import annotations

import argparse
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import shutil
import stat
import tempfile


ROOT = Path(__file__).resolve().parents[1]
PACKAGE_PARENT = ROOT / "plugins"
PACKAGE = PACKAGE_PARENT / "costmarshal"

ROOT_FILES = (
    "CHANGELOG.md",
    "LICENSE",
    "requirements.txt",
    "SECURITY.md",
    "SKILL.md",
    "VERSION",
)
TREE_FILES = (
    ".codex-plugin/plugin.json",
    "agents/openai.yaml",
    "container/worker/.dockerignore",
    "container/worker/Dockerfile",
    "container/worker/README.md",
    "container/worker/costmarshal-escape-probe.js",
    "container/worker/costmarshal-isolation-canary.js",
    "container/worker/costmarshal-worker.js",
    "costmarshal_v2/__init__.py",
    "costmarshal_v2/actor_runner.py",
    "costmarshal_v2/change_apply.py",
    "costmarshal_v2/cli.py",
    "costmarshal_v2/context_projection.py",
    "costmarshal_v2/control_store.py",
    "costmarshal_v2/governance.py",
    "costmarshal_v2/handoff_contract.py",
    "costmarshal_v2/locking.py",
    "costmarshal_v2/mailbox.py",
    "costmarshal_v2/paths.py",
    "costmarshal_v2/profile_binding.py",
    "costmarshal_v2/profiles.py",
    "costmarshal_v2/routing.py",
    "costmarshal_v2/scheduler.py",
    "costmarshal_v2/security.py",
    "costmarshal_v2/session_backend.py",
    "costmarshal_v2/state.py",
    "costmarshal_v2/tmux_backend.py",
    "costmarshal_v2/windows_job.py",
    "costmarshal_v2/windows_job_supervisor.py",
    "costmarshal_v2/worker_isolation.py",
    "skills/orchestrate-cost-aware-agents/SKILL.md",
    "skills/orchestrate-cost-aware-agents/agents/openai.yaml",
)
SCANNED_TREES = (
    ".codex-plugin",
    "agents",
    "container",
    "costmarshal_v2",
    "skills",
)
INDIVIDUAL_FILES = (
    "references/migration-v3.md",
    "references/protocol.md",
    "references/storage.md",
    "scripts/costmarshal.py",
    "scripts/costmarshal_actor.py",
)
IGNORED_NAMES = {"__pycache__", ".mypy_cache", ".pytest_cache", ".ruff_cache"}
IGNORED_SUFFIXES = {".pyc", ".pyo"}
FORBIDDEN_NAMES = {
    ".agents",
    ".ds_store",
    ".env",
    ".git",
    ".github",
    ".npmrc",
    "artifacts",
    "mc.py",
    "secrets.json",
    "thumbs.db",
}
FORBIDDEN_ROOTS = {"plugins", "release", "tests"}
MAX_PACKAGE_FILES = 64
MAX_FILE_BYTES = 1024 * 1024
MAX_TOTAL_BYTES = 4 * 1024 * 1024
MAX_RELATIVE_PATH_BYTES = 240
WINDOWS_RESERVED_STEMS = {
    "aux",
    "con",
    "nul",
    "prn",
    *(f"com{number}" for number in range(1, 10)),
    *(f"lpt{number}" for number in range(1, 10)),
}
INVALID_PORTABLE_CHARACTERS = set('<>:"\\|?*')


def _is_link_or_reparse(path: Path) -> bool:
    if path.is_symlink():
        return True
    try:
        attributes = path.lstat().st_file_attributes
    except FileNotFoundError:
        return False
    except AttributeError:
        return False
    return bool(attributes & stat.FILE_ATTRIBUTE_REPARSE_POINT)


def _is_independent_regular_file(path: Path) -> bool:
    metadata = path.lstat()
    return (
        stat.S_ISREG(metadata.st_mode)
        and metadata.st_nlink == 1
        and not _is_link_or_reparse(path)
    )


def _assert_unredirected_source(path: Path) -> None:
    root = ROOT.resolve(strict=True)
    try:
        relative = path.relative_to(ROOT)
    except ValueError as exc:
        raise RuntimeError(f"plugin package source escaped the repository: {path}") from exc
    current = root
    if _is_link_or_reparse(current):
        raise RuntimeError(f"plugin package repository root is linked: {current}")
    for part in relative.parts:
        current = current / part
        if _is_link_or_reparse(current):
            raise RuntimeError(f"plugin package source is linked or redirected: {path}")
        if not current.exists():
            raise RuntimeError(f"plugin package source is missing: {path}")
    if current.resolve(strict=True) != path.resolve(strict=True):
        raise RuntimeError(f"plugin package source resolved unexpectedly: {path}")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _admitted(path: Path) -> bool:
    return (
        not any(part.casefold() in IGNORED_NAMES for part in path.parts)
        and path.suffix.casefold() not in IGNORED_SUFFIXES
    )


def _portable(path: Path) -> bool:
    relative = path.as_posix()
    if len(relative.encode("utf-8")) > MAX_RELATIVE_PATH_BYTES:
        return False
    for part in path.parts:
        if (
            not part
            or part.endswith((" ", "."))
            or any(
                ord(character) < 32 or character in INVALID_PORTABLE_CHARACTERS
                for character in part
            )
            or part.split(".", 1)[0].casefold() in WINDOWS_RESERVED_STEMS
        ):
            return False
    return True


def _forbidden(path: Path) -> bool:
    parts = tuple(part.casefold() for part in path.parts)
    return bool(
        (parts and parts[0] in FORBIDDEN_ROOTS)
        or any(part in FORBIDDEN_NAMES or part in IGNORED_NAMES for part in parts)
        or path.suffix.casefold() in IGNORED_SUFFIXES
    )


def _limit_errors(files: dict[str, Path]) -> list[str]:
    errors: list[str] = []
    if len(files) > MAX_PACKAGE_FILES:
        errors.append(f"file-count:{len(files)}>{MAX_PACKAGE_FILES}")
    total = 0
    lowered: dict[str, str] = {}
    for relative, path in files.items():
        size = path.stat().st_size
        total += size
        if size > MAX_FILE_BYTES:
            errors.append(f"file-size:{relative}:{size}>{MAX_FILE_BYTES}")
        if not _portable(Path(relative)):
            errors.append(f"nonportable:{relative}")
        previous = lowered.setdefault(relative.casefold(), relative)
        if previous != relative:
            errors.append(f"case-collision:{previous}:{relative}")
    if total > MAX_TOTAL_BYTES:
        errors.append(f"total-size:{total}>{MAX_TOTAL_BYTES}")
    return errors


def source_files() -> dict[str, Path]:
    files: dict[str, Path] = {}
    allowed = tuple(ROOT_FILES + INDIVIDUAL_FILES + TREE_FILES)
    allowed_paths = {Path(relative).as_posix() for relative in allowed}
    allowed_directories: set[str] = set()
    for relative in allowed_paths:
        parent = Path(relative).parent
        while parent != Path("."):
            allowed_directories.add(parent.as_posix())
            parent = parent.parent
    for relative in allowed:
        source = ROOT / relative
        relative_path = Path(relative)
        _assert_unredirected_source(source)
        if (
            not _is_independent_regular_file(source)
            or not _portable(relative_path)
            or _forbidden(relative_path)
        ):
            raise RuntimeError(
                f"plugin package source is missing, linked, or nonportable: {relative}"
            )
        files[relative_path.as_posix()] = source
    for relative in SCANNED_TREES:
        tree = ROOT / relative
        _assert_unredirected_source(tree)
        if not tree.is_dir() or _is_link_or_reparse(tree):
            raise RuntimeError(f"plugin package source tree is missing or linked: {relative}")
        for source in sorted(tree.rglob("*")):
            source_relative = source.relative_to(ROOT)
            _assert_unredirected_source(source)
            if _is_link_or_reparse(source):
                raise RuntimeError(f"plugin package source contains a link: {source_relative}")
            if not _admitted(source_relative):
                continue
            source_key = source_relative.as_posix()
            if not _portable(source_relative) or _forbidden(source_relative):
                raise RuntimeError(
                    f"plugin package source contains a forbidden path: {source_relative}"
                )
            if source.is_file():
                if source_key not in allowed_paths or not _is_independent_regular_file(source):
                    raise RuntimeError(
                        f"plugin package source file is not explicitly admitted: {source_relative}"
                    )
            elif source.is_dir():
                if source_key not in allowed_directories:
                    raise RuntimeError(
                        f"plugin package source directory is not explicitly admitted: {source_relative}"
                    )
            else:
                raise RuntimeError(
                    f"plugin package source is not a regular entry: {source_relative}"
                )
    limit_errors = _limit_errors(files)
    if limit_errors:
        raise RuntimeError(f"plugin package source limits failed: {limit_errors}")
    return dict(sorted(files.items()))


def _package_entries(package_root: Path) -> tuple[dict[str, Path], set[str]]:
    if not package_root.is_dir() or _is_link_or_reparse(package_root):
        return {}, set()
    files: dict[str, Path] = {}
    directories: set[str] = set()
    for path in sorted(package_root.rglob("*")):
        relative = path.relative_to(package_root)
        if _is_link_or_reparse(path):
            raise RuntimeError(f"plugin package contains a link: {relative}")
        if path.is_file():
            if not _is_independent_regular_file(path):
                raise RuntimeError(
                    f"plugin package file is not an independent regular file: {relative}"
                )
            files[relative.as_posix()] = path
        elif path.is_dir():
            directories.add(relative.as_posix())
        else:
            raise RuntimeError(f"plugin package contains a non-regular entry: {relative}")
    return files, directories


def package_files(package_root: Path = PACKAGE) -> dict[str, Path]:
    return _package_entries(package_root)[0]


def package_differences(package_root: Path = PACKAGE) -> list[str]:
    expected = source_files()
    actual, actual_directories = _package_entries(package_root)
    expected_directories: set[str] = set()
    for relative in expected:
        parent = Path(relative).parent
        while parent != Path("."):
            expected_directories.add(parent.as_posix())
            parent = parent.parent
    differences: list[str] = []
    for relative in sorted(expected.keys() - actual.keys()):
        differences.append(f"missing:{relative}")
    for relative in sorted(actual.keys() - expected.keys()):
        differences.append(f"unexpected:{relative}")
    for relative in sorted(expected.keys() & actual.keys()):
        if _sha256(expected[relative]) != _sha256(actual[relative]):
            differences.append(f"content:{relative}")
    for relative in sorted(expected_directories - actual_directories):
        differences.append(f"missing-directory:{relative}")
    for relative in sorted(actual_directories - expected_directories):
        differences.append(f"unexpected-directory:{relative}")
    lowered: dict[str, str] = {}
    for relative in sorted(set(actual) | actual_directories):
        previous = lowered.setdefault(relative.casefold(), relative)
        if previous != relative:
            differences.append(f"case-collision:{previous}:{relative}")
        relative_path = Path(relative)
        if not _portable(relative_path):
            differences.append(f"nonportable:{relative}")
        if _forbidden(relative_path):
            differences.append(f"forbidden:{relative}")
    differences.extend(_limit_errors(actual))
    version = (ROOT / "VERSION").read_text(encoding="utf-8").strip().removeprefix("v")
    for manifest_path in (
        ROOT / ".codex-plugin" / "plugin.json",
        package_root / ".codex-plugin" / "plugin.json",
    ):
        if manifest_path.is_file():
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            if manifest.get("version") != version:
                try:
                    label = manifest_path.relative_to(ROOT).as_posix()
                except ValueError:
                    label = str(manifest_path)
                differences.append(f"version:{label}")
    return differences


def _assert_safe_package_location() -> None:
    root = ROOT.resolve(strict=True)
    expected_parent = root / "plugins"
    expected_package = expected_parent / "costmarshal"
    parent_matches_root = False
    try:
        parent_matches_root = os.path.samefile(PACKAGE_PARENT.parent, root)
    except OSError:
        pass
    package_parent_matches = (
        os.path.normcase(os.path.abspath(PACKAGE.parent))
        == os.path.normcase(os.path.abspath(PACKAGE_PARENT))
    )
    if (
        not parent_matches_root
        or PACKAGE_PARENT.name.casefold() != "plugins"
        or not package_parent_matches
        or PACKAGE.name.casefold() != "costmarshal"
    ):
        raise RuntimeError(
            f"refusing package path outside the canonical repository location: {PACKAGE}"
        )
    if PACKAGE_PARENT.exists() and (
        not PACKAGE_PARENT.is_dir()
        or _is_link_or_reparse(PACKAGE_PARENT)
        or PACKAGE_PARENT.resolve(strict=True) != expected_parent
    ):
        raise RuntimeError(f"plugin package parent is linked or redirected: {PACKAGE_PARENT}")
    PACKAGE_PARENT.mkdir(parents=False, exist_ok=True)
    if (
        _is_link_or_reparse(PACKAGE_PARENT)
        or PACKAGE_PARENT.resolve(strict=True) != expected_parent
    ):
        raise RuntimeError(f"plugin package parent changed during validation: {PACKAGE_PARENT}")
    if PACKAGE.exists() and (
        not PACKAGE.is_dir() or _is_link_or_reparse(PACKAGE)
    ):
        raise RuntimeError(f"plugin package target is not a regular directory: {PACKAGE}")


def _remove_readonly(action, path: str, _error) -> None:
    os.chmod(path, stat.S_IWRITE)
    action(path)


def _remove_generated_tree(path: Path) -> None:
    if (
        path.parent.resolve(strict=True) != PACKAGE_PARENT.resolve(strict=True)
        or _is_link_or_reparse(path)
        or not path.is_dir()
    ):
        raise RuntimeError(f"refusing unsafe generated package path: {path}")
    shutil.rmtree(path, onerror=_remove_readonly)


def _copy_regular_file(source: Path, destination: Path) -> None:
    before = source.lstat()
    if not _is_independent_regular_file(source):
        raise RuntimeError(f"plugin package source changed type before copy: {source}")
    with source.open("rb") as reader, destination.open("xb") as writer:
        opened = os.fstat(reader.fileno())
        if (opened.st_dev, opened.st_ino, opened.st_size) != (
            before.st_dev,
            before.st_ino,
            before.st_size,
        ):
            raise RuntimeError(f"plugin package source changed during open: {source}")
        shutil.copyfileobj(reader, writer, length=1024 * 1024)
        writer.flush()
        os.fsync(writer.fileno())
    after = source.lstat()
    if (
        (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
        != (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
        or not _is_independent_regular_file(source)
    ):
        raise RuntimeError(f"plugin package source changed during copy: {source}")
    shutil.copystat(source, destination, follow_symlinks=False)
    if not _is_independent_regular_file(destination):
        raise RuntimeError(f"staged plugin file is not independent: {destination}")


def _sync_directory(path: Path) -> None:
    if os.name == "nt":
        return
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


@contextmanager
def _package_lock():
    key = hashlib.sha256(str(PACKAGE).encode("utf-8")).hexdigest()[:24]
    lock_path = Path(tempfile.gettempdir()) / f"costmarshal-plugin-package-{key}.lock"
    with lock_path.open("a+b") as handle:
        if handle.seek(0, os.SEEK_END) == 0:
            handle.write(b"\0")
            handle.flush()
        handle.seek(0)
        try:
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            raise RuntimeError("another plugin package sync is already running") from exc
        try:
            yield
        finally:
            handle.seek(0)
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _recover_interrupted_write(backup: Path) -> None:
    if backup.exists():
        if _is_link_or_reparse(backup) or not backup.is_dir():
            raise RuntimeError(f"plugin package backup is not a regular directory: {backup}")
        if not PACKAGE.exists():
            differences = package_differences(backup)
            if differences:
                raise RuntimeError(
                    "interrupted plugin package backup failed validation; "
                    f"refusing recovery: {differences[:8]}"
                )
            backup.replace(PACKAGE)
            _sync_directory(PACKAGE_PARENT)
        else:
            differences = package_differences(PACKAGE)
            if differences:
                raise RuntimeError(
                    "plugin package and interrupted backup both exist; "
                    f"manual review required: {differences[:8]}"
                )
            _remove_generated_tree(backup)
    for stale in PACKAGE_PARENT.glob(".costmarshal-package-*"):
        _remove_generated_tree(stale)


def write_package() -> None:
    _assert_safe_package_location()
    backup = PACKAGE_PARENT / ".costmarshal-backup"
    with _package_lock():
        _recover_interrupted_write(backup)
        temporary_root = Path(
            tempfile.mkdtemp(prefix=".costmarshal-package-", dir=PACKAGE_PARENT)
        ).resolve()
        staged = temporary_root / "costmarshal"
        staged.mkdir()
        try:
            for relative, source in source_files().items():
                destination = staged / relative
                destination.parent.mkdir(parents=True, exist_ok=True)
                _copy_regular_file(source, destination)
            for directory in sorted(
                (path for path in staged.rglob("*") if path.is_dir()),
                key=lambda path: len(path.parts),
                reverse=True,
            ):
                _sync_directory(directory)
            _sync_directory(staged)
            staged_differences = package_differences(staged)
            if staged_differences:
                raise RuntimeError(
                    f"staged plugin package failed validation: {staged_differences[:8]}"
                )
            if PACKAGE.exists():
                PACKAGE.replace(backup)
                _sync_directory(PACKAGE_PARENT)
            try:
                staged.replace(PACKAGE)
                _sync_directory(PACKAGE_PARENT)
                final_differences = package_differences(PACKAGE)
                if final_differences:
                    raise RuntimeError(
                        f"installed plugin package failed validation: {final_differences[:8]}"
                    )
            except BaseException:
                if PACKAGE.exists():
                    _remove_generated_tree(PACKAGE)
                if backup.exists():
                    backup.replace(PACKAGE)
                    _sync_directory(PACKAGE_PARENT)
                raise
            if backup.exists():
                _remove_generated_tree(backup)
        finally:
            if temporary_root.exists():
                _remove_generated_tree(temporary_root)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--write", action="store_true", help="atomically refresh the package")
    args = parser.parse_args()
    if args.write:
        write_package()
    differences = package_differences()
    print(
        json.dumps(
            {
                "status": "pass" if not differences else "fail",
                "package": str(PACKAGE),
                "files": len(package_files()),
                "differences": differences,
            },
            sort_keys=True,
        )
    )
    return 0 if not differences else 1


if __name__ == "__main__":
    raise SystemExit(main())
