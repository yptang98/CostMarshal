"""Cross-platform advisory project locks for CostMarshal control-plane writes."""

from __future__ import annotations

import os
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import BinaryIO, Iterator

from .paths import ProjectLayout


class ProjectLockTimeout(RuntimeError):
    pass


_LOCAL = threading.local()


def _held() -> dict[str, tuple[int, BinaryIO]]:
    value = getattr(_LOCAL, "held", None)
    if value is None:
        value = {}
        _LOCAL.held = value
    return value


def _try_lock(handle: BinaryIO) -> bool:
    handle.seek(0)
    if os.name == "nt":
        import msvcrt

        try:
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            return True
        except OSError:
            return False
    import fcntl

    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        return True
    except BlockingIOError:
        return False


def _unlock(handle: BinaryIO) -> None:
    handle.seek(0)
    if os.name == "nt":
        import msvcrt

        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        return
    import fcntl

    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


@contextmanager
def advisory_file_lock(lock_path: Path, *, timeout_seconds: float = 15.0) -> Iterator[None]:
    if timeout_seconds <= 0:
        raise ValueError("timeout_seconds must be positive")
    lock_path = lock_path.resolve()
    key = os.path.normcase(str(lock_path))
    held = _held()
    if key in held:
        count, handle = held[key]
        held[key] = (count + 1, handle)
        try:
            yield
        finally:
            count, handle = held[key]
            held[key] = (count - 1, handle)
        return

    lock_path.parent.mkdir(parents=True, exist_ok=True)
    handle = lock_path.open("a+b")
    if lock_path.stat().st_size == 0:
        handle.write(b"0")
        handle.flush()
    deadline = time.monotonic() + timeout_seconds
    try:
        while not _try_lock(handle):
            if time.monotonic() >= deadline:
                raise ProjectLockTimeout(f"timed out waiting for project write lock: {lock_path}")
            time.sleep(0.05)
        held[key] = (1, handle)
        try:
            yield
        finally:
            held.pop(key, None)
            _unlock(handle)
    finally:
        handle.close()


@contextmanager
def project_write_lock(layout: ProjectLayout, *, timeout_seconds: float = 15.0) -> Iterator[None]:
    """Serialize one project's control-plane mutations across threads/processes."""

    with advisory_file_lock(layout.project_dir / "locks" / "project.lock", timeout_seconds=timeout_seconds):
        yield


@contextmanager
def scheduler_instance_lock(layout: ProjectLayout, *, timeout_seconds: float = 0.25) -> Iterator[None]:
    """Allow only one long-running scheduler loop per project."""

    with advisory_file_lock(layout.project_dir / "locks" / "scheduler.instance.lock", timeout_seconds=timeout_seconds):
        yield


__all__ = ["ProjectLockTimeout", "advisory_file_lock", "project_write_lock", "scheduler_instance_lock"]
