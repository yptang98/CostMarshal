from __future__ import annotations

import csv
import json
import os
import platform
import queue
import re
import secrets
import shlex
import shutil
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any, Callable

from .paths import ProjectLayout
from .windows_job import (
    WindowsJobError,
    inspect_windows_job_runtime,
    stop_windows_job_runtime,
    validate_windows_job_receipt,
)


ActorCommand = str | list[str]
_TMUX_COMPONENT_RE = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9_-]{0,62}[A-Za-z0-9])?\Z")
_TMUX_LEGACY_ACTOR_RE = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9_.-]{0,62}[A-Za-z0-9])?\Z")
_TMUX_SESSION_ID_RE = re.compile(r"\$[0-9]+\Z")
_TMUX_WINDOW_ID_RE = re.compile(r"@[0-9]+\Z")
_LINUX_PROCESS_TOKEN_PREFIX = "costmarshal-process-"
_LINUX_PROCESS_TOKEN_RE = re.compile(r"costmarshal-process-([0-9a-f]{64})(?:\s|\)|\Z)")
_LINUX_PROCESS_MARKER_RE = re.compile(
    r"linux-proc-v2:([^:]+):([0-9]+):([0-9]+):([0-9]+):([0-9a-f]{64})\Z"
)
WINDOWS_JOB_HANDSHAKE_TIMEOUT_SECONDS = 10.0
_LOCAL_PROCESS_SUPERVISOR = r"""
import json
import os
import pathlib
import subprocess
import sys
import time

payload = json.loads(sys.argv[1])
child = subprocess.Popen(payload["command"], shell=payload["shell"])
returncode = child.wait()
self_pid = os.getpid()
process_group = os.getpgrp()
session_id = os.getsid(0)
proc_root = pathlib.Path("/proc")
while True:
    other_member = False
    try:
        for stat_path in proc_root.glob("[0-9]*/stat"):
            try:
                member_pid = int(stat_path.parent.name)
                if member_pid == self_pid:
                    continue
                raw = stat_path.read_text(encoding="utf-8")
                fields = raw[raw.rfind(")") + 2 :].split()
                if (
                    fields[0] != "Z"
                    and int(fields[2]) == process_group
                    and int(fields[3]) == session_id
                ):
                    other_member = True
                    break
            except (IndexError, OSError, ValueError):
                continue
    except OSError:
        other_member = True
    if not other_member:
        break
    time.sleep(0.25)
raise SystemExit(returncode if returncode >= 0 else 128 - returncode)
"""


def _bounded_readline(
    stream: Any,
    *,
    label: str,
    timeout_seconds: float = WINDOWS_JOB_HANDSHAKE_TIMEOUT_SECONDS,
) -> str:
    """Read one supervisor receipt without allowing a hung pipe to own STOP forever."""

    result: queue.Queue[tuple[str, object]] = queue.Queue(maxsize=1)

    def read_line() -> None:
        try:
            result.put(("line", stream.readline()))
        except BaseException as exc:  # noqa: BLE001 - transport errors cross the helper thread
            result.put(("error", exc))

    reader = threading.Thread(
        target=read_line,
        name=f"costmarshal-{label}-reader",
        daemon=True,
    )
    reader.start()
    try:
        kind, value = result.get(timeout=max(0.01, float(timeout_seconds)))
    except queue.Empty as exc:
        raise RuntimeError(
            f"Windows Job Object supervisor timed out waiting for {label}"
        ) from exc
    if kind == "error":
        assert isinstance(value, BaseException)
        raise RuntimeError(
            f"Windows Job Object supervisor failed while reading {label}"
        ) from value
    return str(value)


def validate_tmux_name(value: str, *, label: str) -> str:
    if not isinstance(value, str) or not _TMUX_COMPONENT_RE.fullmatch(value):
        raise RuntimeError(
            f"tmux {label} must be 1-64 characters, start and end with a letter or number, "
            "and contain only letters, numbers, hyphens, and underscores"
        )
    return value


def validate_tmux_target(value: str) -> str:
    if not isinstance(value, str):
        raise RuntimeError("tmux target must be a session:actor string")
    session_value, separator, actor_value = value.partition(":")
    if not separator or ":" in actor_value:
        raise RuntimeError("tmux target must be a session:actor string")
    validate_tmux_name(session_value, label="session name")
    validate_tmux_name(actor_value, label="actor name")
    return value


def validate_persisted_tmux_actor_name(value: str) -> str:
    """Accept the pre-hardening dotted window names needed for safe shutdown."""
    if not isinstance(value, str) or not _TMUX_LEGACY_ACTOR_RE.fullmatch(value):
        raise RuntimeError(
            "persisted tmux actor name must be 1-64 safe legacy characters"
        )
    return value


def validate_persisted_tmux_session_name(value: str) -> str:
    """Accept pre-hardening dotted session names while rejecting target syntax."""
    if not isinstance(value, str) or not _TMUX_LEGACY_ACTOR_RE.fullmatch(value):
        raise RuntimeError(
            "persisted tmux session name must be 1-64 safe legacy characters"
        )
    return value


def tmux_runtime_session_name(value: str) -> str:
    """Mirror tmux 3.4 session_check_name for accepted persisted names."""

    validate_persisted_tmux_session_name(value)
    return value.replace(".", "_").replace(":", "_")


def validate_persisted_tmux_target(value: str) -> str:
    if not isinstance(value, str):
        raise RuntimeError("persisted tmux target must be a session:actor string")
    session_value, separator, actor_value = value.partition(":")
    if not separator or ":" in actor_value:
        raise RuntimeError("persisted tmux target must be a session:actor string")
    validate_persisted_tmux_session_name(session_value)
    validate_persisted_tmux_actor_name(actor_value)
    return value


def tmux_format_literal(value: str) -> str:
    """Escape a literal used where tmux performs format expansion."""
    return value.replace("#", "##")


def tmux_session_target(session_name_value: str) -> str:
    validate_tmux_name(session_name_value, label="session name")
    return f"={session_name_value}"


def tmux_persisted_session_target(session_name_value: str) -> str:
    validate_persisted_tmux_session_name(session_name_value)
    return f"={session_name_value}"


def tmux_new_window_target(session_name_value: str) -> str:
    return f"{tmux_session_target(session_name_value)}:"


def tmux_persisted_new_window_target(session_name_value: str) -> str:
    return f"{tmux_persisted_session_target(session_name_value)}:"


def tmux_actor_target(target: str) -> str:
    validate_tmux_target(target)
    session_value, actor_value = target.split(":", 1)
    return f"={session_value}:={actor_value}"


def command_to_string(argv: list[str]) -> str:
    if os.name == "nt":
        return subprocess.list2cmdline(argv)
    return " ".join(shlex.quote(item) for item in argv)


def command_display(command: ActorCommand) -> str:
    return command if isinstance(command, str) else command_to_string(command)


def format_actor_command(template: str, *, layout: ProjectLayout, session: dict[str, Any], actor: dict[str, Any]) -> str:
    task_id = actor.get("task_id") or ""
    prompt = actor.get("prompt_path") or ""
    brief = f"tasks/{task_id}/brief.md" if task_id else ""
    report = f"tasks/{task_id}/completion-report.md" if task_id else ""
    values = {
        "project": str(layout.project_dir),
        "project_id": session.get("project_id", ""),
        "actor": actor.get("id", ""),
        "task": task_id,
        "model": actor.get("model", "inherit"),
        "mailbox": str(layout.project_dir / actor.get("mailbox", {}).get("dir", "")),
        "prompt": str(layout.project_dir / prompt) if prompt else "",
        "prompt_file": str(layout.project_dir / prompt) if prompt else "",
        "brief": str(layout.project_dir / brief) if brief else "",
        "report": str(layout.project_dir / report) if report else "",
    }
    try:
        return template.format(**values)
    except (KeyError, ValueError):
        return template


def default_backend_kind() -> str:
    if os.name == "nt":
        return "local"
    return "tmux" if shutil.which("tmux") else "local"


def select_backend_kind(requested: str | None) -> str:
    selected = default_backend_kind() if not requested or requested == "auto" else requested
    if selected not in {"tmux", "local"}:
        raise SystemExit(f"Unsupported session backend: {requested}")
    if selected == "local" and os.name != "nt" and platform.system().lower() != "linux":
        raise SystemExit("The local process backend is supported only on Windows and Linux; use tmux")
    return selected


def session_backend_config(session: dict[str, Any]) -> dict[str, Any]:
    legacy = session.get("tmux") or {}
    return session.get("backend") or {
        "kind": "tmux",
        "session_name": legacy.get("session_name"),
        "executable": legacy.get("executable", "tmux"),
        "enabled": legacy.get("enabled", True),
    }


def session_name(session: dict[str, Any]) -> str:
    return str(session_backend_config(session).get("session_name") or "")


def session_backend_kind(session: dict[str, Any]) -> str:
    return str(session_backend_config(session).get("kind") or "local")


def actor_runtime(actor: dict[str, Any]) -> dict[str, Any]:
    legacy = actor.get("tmux") or {}
    return actor.setdefault(
        "runtime",
        {
            "backend": "tmux",
            "session_name": legacy.get("session_name"),
            "actor_name": legacy.get("window_name"),
            "target": legacy.get("target"),
            "pid": None,
            "started_at": legacy.get("started_at"),
            "last_launch_command": legacy.get("last_launch_command"),
        },
    )


def _windows_tasklist_contains_pid(output: str, pid: int) -> bool:
    for row in csv.reader(output.splitlines()):
        if len(row) > 1 and row[1].strip() == str(pid):
            return True
    return False


def pid_is_alive(pid: int | None) -> bool:
    if not pid:
        return False
    try:
        if os.name == "nt":
            # Keep liveness handle-bound and locale/permission independent.
            # ``tasklist`` may itself be denied in restricted Codex sessions and
            # its localized text is not an operating-system identity receipt.
            import ctypes
            from ctypes import wintypes

            process_query_limited_information = 0x1000
            synchronize = 0x00100000
            wait_object_0 = 0
            wait_timeout = 258
            error_access_denied = 5
            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel32.OpenProcess.argtypes = [
                wintypes.DWORD,
                wintypes.BOOL,
                wintypes.DWORD,
            ]
            kernel32.OpenProcess.restype = wintypes.HANDLE
            kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
            kernel32.WaitForSingleObject.restype = wintypes.DWORD
            kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
            kernel32.CloseHandle.restype = wintypes.BOOL
            ctypes.set_last_error(0)
            handle = kernel32.OpenProcess(
                process_query_limited_information | synchronize,
                False,
                int(pid),
            )
            if not handle:
                error = ctypes.get_last_error()
                # Access denied cannot prove absence. Callers that need STOP
                # authority use the stronger FILETIME/Job Object receipt path.
                return error == error_access_denied
            try:
                status = int(kernel32.WaitForSingleObject(handle, 0))
                if status == wait_timeout:
                    return True
                if status == wait_object_0:
                    return False
                return False
            finally:
                kernel32.CloseHandle(handle)
        os.kill(pid, 0)
        # A zombie has no executable runtime left and cannot service provider
        # work. Treating it as live also prevents bounded process-group STOP
        # from completing while the detached runner's parent reaps it.
        stat_path = Path(f"/proc/{int(pid)}/stat")
        if stat_path.is_file():
            raw = stat_path.read_text(encoding="utf-8")
            fields = raw[raw.rfind(")") + 2 :].split()
            if fields and fields[0] == "Z":
                return False
        return True
    except (IndexError, OSError, ValueError):
        return False


def pid_start_marker(pid: int | None) -> str | None:
    """Return an OS-backed process creation identity, not merely a PID."""

    if not pid or not pid_is_alive(pid):
        return None
    if os.name == "nt":
        try:
            import ctypes
            from ctypes import wintypes

            process_query_limited_information = 0x1000
            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
            kernel32.OpenProcess.restype = wintypes.HANDLE
            kernel32.GetProcessTimes.argtypes = [
                wintypes.HANDLE,
                ctypes.POINTER(wintypes.FILETIME),
                ctypes.POINTER(wintypes.FILETIME),
                ctypes.POINTER(wintypes.FILETIME),
                ctypes.POINTER(wintypes.FILETIME),
            ]
            kernel32.GetProcessTimes.restype = wintypes.BOOL
            kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
            kernel32.CloseHandle.restype = wintypes.BOOL
            handle = kernel32.OpenProcess(
                process_query_limited_information, False, int(pid)
            )
            if not handle:
                return None
            try:
                created = wintypes.FILETIME()
                exited = wintypes.FILETIME()
                kernel = wintypes.FILETIME()
                user = wintypes.FILETIME()
                if not kernel32.GetProcessTimes(
                    handle,
                    ctypes.byref(created),
                    ctypes.byref(exited),
                    ctypes.byref(kernel),
                    ctypes.byref(user),
                ):
                    return None
                value = (int(created.dwHighDateTime) << 32) | int(created.dwLowDateTime)
                return f"windows-filetime:{value}"
            finally:
                kernel32.CloseHandle(handle)
        except (AttributeError, OSError, ValueError):
            return None
    record = _linux_process_record(int(pid))
    return None if record is None else str(record["marker"])


def pid_identity_matches(pid: int | None, marker: str | None) -> bool:
    return bool(marker) and pid_start_marker(pid) == marker


def _linux_process_token(pid: int) -> str | None:
    """Return the unguessable inherited launch token held by a Linux process."""

    fd_root = Path(f"/proc/{pid}/fd")
    try:
        descriptors = list(fd_root.iterdir())
    except OSError:
        return None
    for descriptor in descriptors:
        try:
            target = os.readlink(descriptor)
        except OSError:
            continue
        match = _LINUX_PROCESS_TOKEN_RE.search(target)
        if match is not None:
            return match.group(1)
    return None


def _linux_process_stat(pid: int, *, boot_id: str | None = None) -> dict[str, Any] | None:
    """Read cheap non-zombie Linux stat identity without walking process fds."""

    stat_path = Path(f"/proc/{pid}/stat")
    try:
        raw = stat_path.read_text(encoding="utf-8")
        fields = raw[raw.rfind(")") + 2 :].split()
        state = fields[0]
        process_group = int(fields[2])
        session_id = int(fields[3])
        start_ticks = fields[19]
        if state == "Z":
            return None
        if boot_id is None:
            boot_id_path = Path("/proc/sys/kernel/random/boot_id")
            boot_id = (
                boot_id_path.read_text(encoding="ascii").strip()
                if boot_id_path.is_file()
                else "unknown"
            )
        return {
            "pid": pid,
            "state": state,
            "process_group": process_group,
            "session_id": session_id,
            "start_ticks": start_ticks,
            "boot_id": boot_id,
        }
    except (IndexError, OSError, ValueError):
        return None


def _linux_process_record(pid: int, *, boot_id: str | None = None) -> dict[str, Any] | None:
    """Read one Linux identity together with inherited launch evidence."""

    record = _linux_process_stat(pid, boot_id=boot_id)
    if record is None:
        return None
    token = _linux_process_token(pid)
    marker = f"linux-proc:{record['boot_id']}:{record['start_ticks']}"
    if token is not None:
        marker = (
            f"linux-proc-v2:{record['boot_id']}:{record['start_ticks']}:"
            f"{record['process_group']}:{record['session_id']}:{token}"
        )
    return {**record, "token": token, "marker": marker}


def _linux_marker_identity(marker: str | None) -> dict[str, Any] | None:
    if not isinstance(marker, str):
        return None
    match = _LINUX_PROCESS_MARKER_RE.fullmatch(marker)
    if match is None:
        return None
    return {
        "boot_id": match.group(1),
        "start_ticks": match.group(2),
        "process_group": int(match.group(3)),
        "session_id": int(match.group(4)),
        "token": match.group(5),
    }


def _linux_process_group_members(
    process_group: int,
    *,
    session_id: int | None = None,
) -> dict[int, str]:
    """Return non-zombie group members bound to their Linux start markers."""

    members: dict[int, str] = {}
    proc_root = Path("/proc")
    if not proc_root.is_dir():
        return members
    boot_id_path = Path("/proc/sys/kernel/random/boot_id")
    try:
        boot_id = boot_id_path.read_text(encoding="ascii").strip()
        for stat_path in proc_root.glob("[0-9]*/stat"):
            try:
                member_pid = int(stat_path.parent.name)
                stat_record = _linux_process_stat(member_pid, boot_id=boot_id)
            except (OSError, ValueError):
                continue
            if (
                stat_record is None
                or stat_record["process_group"] != process_group
                or (session_id is not None and stat_record["session_id"] != session_id)
            ):
                continue
            record = _linux_process_record(member_pid, boot_id=boot_id)
            if (
                record is not None
                and record["process_group"] == process_group
                and (session_id is None or record["session_id"] == session_id)
            ):
                members[member_pid] = str(record["marker"])
    except OSError:
        return {}
    return members


def _marker_has_process_token(marker: str, token: str, *, boot_id: str) -> bool:
    identity = _linux_marker_identity(marker)
    return bool(
        identity is not None
        and identity["token"] == token
        and identity["boot_id"] == boot_id
    )


def _linux_member_identity_matches(
    pid: int,
    marker: str,
    *,
    process_group: int,
    session_id: int,
) -> bool:
    record = _linux_process_record(pid)
    return bool(
        record is not None
        and record["marker"] == marker
        and record["process_group"] == process_group
        and record["session_id"] == session_id
    )


def _verified_local_process_group(
    pid: int,
    marker: str,
    *,
    require_owned_group: bool,
) -> tuple[int, int, dict[int, str]] | None:
    """Resolve a launch to members without trusting a reusable PGID alone."""

    if not Path("/proc").is_dir():
        return None
    durable = _linux_marker_identity(marker)
    leader_matches = pid_identity_matches(pid, marker)
    if durable is not None:
        process_group = int(durable["process_group"])
        session_id = int(durable["session_id"])
        # The durable supervisor is the session/group leader. The actor runner
        # may later rebind runtime.pid to its own child PID, so require the
        # recorded group to own the session rather than requiring pid == PGID.
        if process_group != session_id:
            return None
        members = _linux_process_group_members(
            process_group,
            session_id=session_id,
        )
        token_member_exists = any(
            _marker_has_process_token(
                member_marker,
                str(durable["token"]),
                boot_id=str(durable["boot_id"]),
            )
            for member_marker in members.values()
        )
        if not leader_matches and not token_member_exists:
            if members:
                raise RuntimeError(
                    "local process identity changed and the recorded process group is still live; "
                    "refusing to treat an unverified group as stopped"
                )
            return None
        return process_group, session_id, members

    # Compatibility for pre-v2 Linux markers. Without an inherited launch
    # capability, the original leader must still be present to prove ownership.
    if not leader_matches:
        return None
    try:
        process_group = os.getpgid(pid)
        session_id = os.getsid(pid)
    except ProcessLookupError:
        return None
    if require_owned_group and (process_group != pid or session_id != pid):
        return None
    members = _linux_process_group_members(process_group, session_id=session_id)
    if members.get(pid) != marker:
        return None
    return process_group, session_id, members


def _verified_group_anchor(
    *,
    leader_pid: int,
    leader_marker: str,
    process_group: int,
    session_id: int,
    bound_members: dict[int, str],
) -> tuple[int, str]:
    """Bind the member that must survive until every other group member is gone."""

    durable = _linux_marker_identity(leader_marker)
    if durable is not None:
        anchor_pid = process_group
        anchor_record = _linux_process_record(anchor_pid)
        if (
            anchor_record is not None
            and anchor_record["process_group"] == process_group
            and anchor_record["session_id"] == session_id
            and _marker_has_process_token(
                str(anchor_record["marker"]),
                str(durable["token"]),
                boot_id=str(durable["boot_id"]),
            )
        ):
            return anchor_pid, str(anchor_record["marker"])
        raise RuntimeError(
            "local durable process-group supervisor identity disappeared; "
            "refusing to signal an unanchored process group"
        )

    if bound_members.get(leader_pid) == leader_marker:
        return leader_pid, leader_marker
    raise RuntimeError(
        "local process-group leader identity disappeared; refusing to signal an unanchored process group"
    )


def _anchored_group_members(
    *,
    anchor_pid: int,
    anchor_marker: str,
    process_group: int,
    session_id: int,
) -> dict[int, str]:
    """Return a complete current snapshot or fail if a live group lost its anchor."""

    current = _linux_process_group_members(process_group, session_id=session_id)
    if _linux_member_identity_matches(
        anchor_pid,
        anchor_marker,
        process_group=process_group,
        session_id=session_id,
    ):
        current[anchor_pid] = anchor_marker
        return current
    if not current:
        return {}
    raise RuntimeError(
        "local process-group supervisor identity disappeared while group members remain live; "
        "refusing to report STOP success"
    )


def _signal_verified_members(
    members: dict[int, str],
    *,
    process_group: int,
    session_id: int,
    signal_number: int,
) -> list[list[str]]:
    """Signal pidfd-pinned, start-marker-verified members without PID races."""

    pidfd_open = getattr(os, "pidfd_open", None)
    pidfd_send_signal = getattr(signal, "pidfd_send_signal", None)
    if pidfd_open is None or pidfd_send_signal is None:
        raise RuntimeError(
            "local POSIX STOP requires pidfd_open and pidfd_send_signal; refusing PID-only signaling"
        )
    signal_name = "TERM" if signal_number == signal.SIGTERM else "KILL"
    commands: list[list[str]] = []
    for member_pid, member_marker in sorted(members.items()):
        try:
            pidfd = pidfd_open(member_pid, 0)
        except ProcessLookupError:
            continue
        try:
            if not _linux_member_identity_matches(
                member_pid,
                member_marker,
                process_group=process_group,
                session_id=session_id,
            ):
                continue
            argv = ["pidfd-send-signal", f"-{signal_name}", "--", str(member_pid)]
            try:
                pidfd_send_signal(pidfd, signal_number, None, 0)
            except ProcessLookupError:
                continue
            commands.append(argv)
        finally:
            os.close(pidfd)
    return commands


class TmuxBackend:
    kind = "tmux"

    def __init__(self, executable: str = "tmux") -> None:
        self.executable = executable

    def available(self) -> bool:
        return shutil.which(self.executable) is not None

    def _run(self, argv: list[str], *, check: bool = True) -> subprocess.CompletedProcess[str]:
        return subprocess.run(argv, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=check)

    @staticmethod
    def _session_listing_confirms_absence(result: subprocess.CompletedProcess[str]) -> bool:
        """Recognize only tmux's explicit empty-server results as absence."""

        if result.returncode != 1 or result.stdout.strip():
            return False
        detail = result.stderr.strip()
        return detail == "no sessions" or re.fullmatch(r"no server running on [^\r\n]+", detail) is not None

    @classmethod
    def _window_listing_confirms_absence(
        cls,
        result: subprocess.CompletedProcess[str],
        *,
        session_target: str,
    ) -> bool:
        """Recognize only an exact vanished-session result as actor absence."""

        if cls._session_listing_confirms_absence(result):
            return True
        return bool(
            result.returncode == 1
            and not result.stdout.strip()
            and result.stderr.strip() == f"can't find session: {session_target}"
        )

    def _resolve_session_target(self, session_name_value: str) -> str | None:
        """Resolve one logical persisted name to an unambiguous stable session ID."""

        validate_persisted_tmux_session_name(session_name_value)
        if not self.available():
            return None
        result = self._run(
            [
                self.executable,
                "list-sessions",
                "-F",
                "#{session_id}\t#{session_name}",
            ],
            check=False,
        )
        if result.returncode != 0:
            if self._session_listing_confirms_absence(result):
                return None
            detail = result.stderr.strip() or result.stdout.strip() or f"exit {result.returncode}"
            raise RuntimeError(f"tmux session inspection failed: {detail}")
        runtime_name = tmux_runtime_session_name(session_name_value)
        accepted_names = {session_name_value, runtime_name}
        matches: list[str] = []
        for line in result.stdout.splitlines():
            session_id, separator, observed_name = line.partition("\t")
            if not separator or observed_name not in accepted_names:
                continue
            if not _TMUX_SESSION_ID_RE.fullmatch(session_id):
                raise RuntimeError("tmux returned an invalid session ID for a persisted session")
            matches.append(session_id)
        if len(matches) > 1:
            raise RuntimeError(
                "persisted tmux session name is ambiguous between its logical and sanitized forms"
            )
        return matches[0] if matches else None

    def _resolve_actor_target(self, target: str) -> str:
        validate_persisted_tmux_target(target)
        session_value, actor_value = target.split(":", 1)
        validate_persisted_tmux_actor_name(actor_value)
        session_target = self._resolve_session_target(session_value)
        if session_target is None:
            raise RuntimeError("tmux actor lookup could not resolve its persisted session")
        result = self._run(
            [
                self.executable,
                "list-windows",
                "-t",
                session_target,
                "-F",
                "#{window_id}\t#{window_name}",
            ],
            check=False,
        )
        if result.returncode != 0:
            raise RuntimeError(f"tmux actor lookup failed: {result.stderr.strip()}")
        matches: list[str] = []
        for line in result.stdout.splitlines():
            window_id, separator, window_name = line.partition("\t")
            if separator and window_name == actor_value and _TMUX_WINDOW_ID_RE.fullmatch(window_id):
                matches.append(window_id)
        if len(matches) != 1:
            raise RuntimeError(
                f"tmux actor lookup requires one exact window, found {len(matches)}"
            )
        return matches[0]

    def session_exists(self, session_name_value: str) -> bool:
        return self._resolve_session_target(session_name_value) is not None

    def list_actors(self, session_name_value: str) -> list[str]:
        target = self._resolve_session_target(session_name_value)
        if target is None:
            return []
        result = self._run([self.executable, "list-windows", "-t", target, "-F", "#{window_name}"], check=False)
        if result.returncode != 0:
            if self._window_listing_confirms_absence(result, session_target=target):
                return []
            detail = result.stderr.strip() or result.stdout.strip() or f"exit {result.returncode}"
            raise RuntimeError(f"tmux actor inspection failed: {detail}")
        return [line.strip() for line in result.stdout.splitlines() if line.strip()]

    def start_plan(
        self,
        *,
        session_name: str,
        actor_name: str,
        command: ActorCommand,
        session_exists: bool | None = None,
        cwd: Path | None = None,
    ) -> list[list[str]]:
        validate_persisted_tmux_session_name(session_name)
        validate_tmux_name(actor_name, label="actor name")
        command_text = command_display(command)
        resolved_session = self._resolve_session_target(session_name)
        exists = resolved_session is not None if session_exists is None else session_exists
        cwd_args = ["-c", tmux_format_literal(str(Path(cwd).resolve()))] if cwd is not None else []
        if exists:
            if resolved_session is None:
                raise RuntimeError("tmux session was expected to exist but could not be resolved uniquely")
            create = [
                self.executable,
                "new-window",
                "-d",
                "-t",
                f"{resolved_session}:",
                "-n",
                actor_name,
                *cwd_args,
                "--",
                command_text,
            ]
        else:
            create = [
                self.executable,
                "new-session",
                "-d",
                "-s",
                session_name,
                "-n",
                actor_name,
                *cwd_args,
                "--",
                command_text,
            ]
        return [create]

    def start_actor(
        self,
        *,
        session_name: str,
        actor_name: str,
        command: ActorCommand,
        cwd: Path,
        log_path: Path,
        prepared_callback: Callable[[dict[str, Any]], None] | None = None,
    ) -> dict[str, Any]:
        if not self.available():
            raise RuntimeError(f"tmux executable not found: {self.executable}")
        resolved_cwd = Path(cwd).resolve()
        if not resolved_cwd.is_dir():
            raise RuntimeError(f"tmux actor working directory is unavailable: {resolved_cwd}")
        target = f"{session_name}:{actor_name}"
        commands = self.start_plan(
            session_name=session_name,
            actor_name=actor_name,
            command=command,
            cwd=resolved_cwd,
        )
        for argv in commands:
            result = self._run(argv, check=False)
            if result.returncode != 0:
                raise RuntimeError(f"tmux command failed: {command_to_string(argv)}\n{result.stderr.strip()}")
        if self._resolve_session_target(session_name) is None:
            raise RuntimeError("tmux started an actor but its persisted session could not be resolved")
        self._resolve_actor_target(target)
        return {
            "commands": [command_to_string(argv) for argv in commands],
            "target": target,
            "pid": None,
            "log_path": None,
        }

    def send_text(self, *, target: str, text: str) -> dict[str, Any]:
        if not self.available():
            raise RuntimeError(f"tmux executable not found: {self.executable}")
        exact_target = self._resolve_actor_target(target)
        # A literal carriage return submits the text in both canonical terminals
        # and raw TUIs without creating a second, independently failing tmux call.
        argv = [self.executable, "send-keys", "-l", "-t", exact_target, "--", text + "\r"]
        result = self._run(argv, check=False)
        if result.returncode != 0:
            raise RuntimeError(f"tmux send-keys failed: {result.stderr.strip()}")
        rendered = command_to_string(argv)
        return {"command": rendered, "commands": [rendered]}

    def stop_plan(self, *, target: str, pid: int | None = None) -> list[list[str]]:
        return [[self.executable, "kill-window", "-t", self._resolve_actor_target(target)]]

    def stop_actor(
        self,
        *,
        target: str,
        pid: int | None = None,
        process_start_marker: str | None = None,
    ) -> dict[str, Any]:
        if not self.available():
            raise RuntimeError(f"tmux executable not found: {self.executable}")
        exact_target = self._resolve_actor_target(target)
        argv = [self.executable, "kill-window", "-t", exact_target]
        result = self._run(argv, check=False)
        if result.returncode != 0:
            raise RuntimeError(f"tmux kill-window failed: {result.stderr.strip()}")
        return {"command": command_to_string(argv)}

    def actor_alive(
        self,
        *,
        session_name: str,
        actor_name: str,
        target: str | None = None,
        pid: int | None = None,
        process_start_marker: str | None = None,
    ) -> bool:
        return actor_name in set(self.list_actors(session_name))


class LocalProcessBackend:
    kind = "local"

    def __init__(self, executable: str | None = None) -> None:
        self.executable = executable or "local-process"

    def available(self) -> bool:
        return True

    def session_exists(self, session_name_value: str) -> bool:
        return True

    def list_actors(self, session_name_value: str) -> list[str]:
        return []

    def start_plan(
        self,
        *,
        session_name: str,
        actor_name: str,
        command: ActorCommand,
        session_exists: bool | None = None,
        cwd: Path | None = None,
    ) -> list[list[str]]:
        return [[self.executable, "start", "--session", session_name, "--actor", actor_name, "--", command_display(command)]]

    def start_actor(
        self,
        *,
        session_name: str,
        actor_name: str,
        command: ActorCommand,
        cwd: Path,
        log_path: Path,
        prepared_callback: Callable[[dict[str, Any]], None] | None = None,
    ) -> dict[str, Any]:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_handle = log_path.open("a", encoding="utf-8")
        creationflags = 0
        start_new_session = False
        process_token_fd: int | None = None
        stdin_source: int = subprocess.DEVNULL
        launch_command: ActorCommand = command
        launch_shell = isinstance(command, str)
        if os.name == "nt":
            supervisor_path = Path(__file__).with_name("windows_job_supervisor.py").resolve()
            if not supervisor_path.is_file():
                log_handle.close()
                raise RuntimeError("Windows Job Object supervisor is unavailable")
            supervisor = subprocess.Popen(
                [sys.executable, str(supervisor_path)],
                cwd=str(cwd),
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=log_handle,
                text=True,
                creationflags=(
                    subprocess.CREATE_NEW_PROCESS_GROUP
                    | subprocess.DETACHED_PROCESS
                    | subprocess.CREATE_NO_WINDOW
                ),
            )
            receipt = None
            try:
                assert supervisor.stdin is not None
                supervisor.stdin.write(
                    json.dumps(
                        {
                            "command": command,
                            "shell": isinstance(command, str),
                            "cwd": str(Path(cwd).resolve()),
                            "log_path": str(log_path.resolve()),
                        },
                        ensure_ascii=False,
                        separators=(",", ":"),
                        allow_nan=False,
                    )
                    + "\n"
                )
                supervisor.stdin.flush()
                assert supervisor.stdout is not None
                receipt_line = _bounded_readline(
                    supervisor.stdout,
                    label="prepared receipt",
                )
                if not receipt_line:
                    returncode = supervisor.poll()
                    raise RuntimeError(
                        "Windows Job Object supervisor exited without a launch receipt"
                        + (f" (exit {returncode})" if returncode is not None else "")
                    )
                try:
                    receipt_payload = json.loads(receipt_line)
                except json.JSONDecodeError as exc:
                    raise RuntimeError("Windows Job Object supervisor returned an invalid receipt") from exc
                if (
                    not isinstance(receipt_payload, dict)
                    or receipt_payload.get("schema") != "costmarshal-windows-job-receipt-v1"
                    or receipt_payload.get("status") != "prepared"
                ):
                    detail = (
                        receipt_payload.get("error")
                        if isinstance(receipt_payload, dict)
                        else "invalid receipt"
                    )
                    raise RuntimeError(f"Windows Job Object launch failed: {detail}")
                receipt = validate_windows_job_receipt(
                    supervisor_pid=receipt_payload.get("supervisor_pid"),
                    supervisor_start_marker=receipt_payload.get("supervisor_start_marker"),
                    job_name=receipt_payload.get("job_name"),
                    job_identity=receipt_payload.get("job_identity"),
                    child_pid=receipt_payload.get("child_pid"),
                    child_start_marker=receipt_payload.get("child_start_marker"),
                )
                if receipt.supervisor_pid != supervisor.pid:
                    raise RuntimeError("Windows Job Object receipt supervisor PID is not the launched process")
                inspection = inspect_windows_job_runtime(receipt)
                if not inspection.job_present or inspection.supervisor_state != "alive":
                    raise RuntimeError("prepared Windows Job Object receipt is no longer live")
                prepared_launch = {
                    "target": f"job:{receipt.job_name}",
                    "pid": receipt.supervisor_pid,
                    "process_start_marker": receipt.supervisor_start_marker,
                    "windows_job_name": receipt.job_name,
                    "windows_job_identity": receipt.job_identity,
                    "windows_job_child_pid": receipt.child_pid,
                    "windows_job_child_start_marker": receipt.child_start_marker,
                    "log_path": str(log_path),
                }
                if prepared_callback is not None:
                    prepared_callback(dict(prepared_launch))
                supervisor.stdin.write(
                    json.dumps(
                        {"action": "resume", "job_identity": receipt.job_identity},
                        ensure_ascii=False,
                        separators=(",", ":"),
                    )
                    + "\n"
                )
                supervisor.stdin.flush()
                supervisor.stdin.close()
                started_line = _bounded_readline(
                    supervisor.stdout,
                    label="started receipt",
                )
                supervisor.stdout.close()
                try:
                    started_payload = json.loads(started_line)
                except json.JSONDecodeError as exc:
                    raise RuntimeError(
                        "Windows Job Object supervisor returned an invalid final receipt"
                    ) from exc
                if (
                    not isinstance(started_payload, dict)
                    or started_payload.get("status") != "started"
                    or {
                        key: started_payload.get(key)
                        for key in (
                            "schema",
                            "supervisor_pid",
                            "supervisor_start_marker",
                            "job_name",
                            "job_identity",
                            "child_pid",
                            "child_start_marker",
                        )
                    }
                    != {
                        key: receipt_payload.get(key)
                        for key in (
                            "schema",
                            "supervisor_pid",
                            "supervisor_start_marker",
                            "job_name",
                            "job_identity",
                            "child_pid",
                            "child_start_marker",
                        )
                    }
                ):
                    raise RuntimeError(
                        "Windows Job Object final receipt does not match the prepared identity"
                    )
                final_inspection = inspect_windows_job_runtime(receipt)
                if not final_inspection.job_present:
                    raise RuntimeError("started Windows Job Object is no longer live")
                # The durable supervisor owns its Job handle. CostMarshal keeps
                # only the PID+FILETIME receipt, so release Popen's duplicate
                # process handle instead of retaining a hidden second lifetime.
                supervisor._handle.Close()  # type: ignore[attr-defined]
                supervisor._handle = None  # type: ignore[attr-defined]
                supervisor.returncode = 0
                return {
                    "commands": [
                        command_to_string(
                            [
                                self.executable,
                                "start-job",
                                "--session",
                                session_name,
                                "--actor",
                                actor_name,
                            ]
                        )
                    ],
                    **prepared_launch,
                }
            except BaseException:
                cleanup_error: BaseException | None = None
                try:
                    if receipt is not None:
                        stop_windows_job_runtime(receipt)
                    elif supervisor.poll() is None:
                        supervisor.terminate()
                    supervisor.wait(timeout=10)
                except BaseException as exc:  # noqa: BLE001 - cleanup uncertainty replaces the launch error
                    cleanup_error = exc
                if cleanup_error is not None:
                    raise RuntimeError(
                        "Windows Job Object launch failed and handle-bound cleanup was not confirmed"
                    ) from cleanup_error
                raise
            finally:
                if supervisor.stdin is not None and not supervisor.stdin.closed:
                    supervisor.stdin.close()
                if supervisor.stdout is not None and not supervisor.stdout.closed:
                    supervisor.stdout.close()
                log_handle.close()
        else:
            if (
                platform.system() != "Linux"
                or not hasattr(os, "memfd_create")
                or not hasattr(os, "pidfd_open")
                or not hasattr(signal, "pidfd_send_signal")
            ):
                log_handle.close()
                raise RuntimeError(
                    "local POSIX actors require Linux procfs, memfd, and pidfd signal support"
                )
            start_new_session = True
            try:
                process_token_fd = os.memfd_create(
                    _LINUX_PROCESS_TOKEN_PREFIX + secrets.token_hex(32),
                    flags=0,
                )
            except OSError:
                log_handle.close()
                raise
            # An empty memfd has the same EOF behavior as DEVNULL for reads,
            # while standard fd 0 survives ordinary close_fds=True child
            # spawns. Descendants therefore retain a launch capability unless
            # they explicitly replace stdin, in which case recovery fails safe.
            stdin_source = process_token_fd
            supervisor_payload = json.dumps(
                {"command": command, "shell": isinstance(command, str)},
                ensure_ascii=False,
                separators=(",", ":"),
                allow_nan=False,
            )
            launch_command = [
                sys.executable,
                "-c",
                _LOCAL_PROCESS_SUPERVISOR,
                supervisor_payload,
            ]
            launch_shell = False
        try:
            process = subprocess.Popen(
                launch_command,
                cwd=str(cwd),
                shell=launch_shell,
                stdin=stdin_source,
                stdout=log_handle,
                stderr=subprocess.STDOUT,
                text=True,
                creationflags=creationflags,
                start_new_session=start_new_session,
            )
        finally:
            if process_token_fd is not None:
                os.close(process_token_fd)
            log_handle.close()
        return {
            "commands": [command_to_string([self.executable, "start", "--session", session_name, "--actor", actor_name, "--", command_display(command)])],
            "target": f"pid:{process.pid}",
            "pid": process.pid,
            "log_path": str(log_path),
        }

    def send_text(self, *, target: str, text: str) -> dict[str, Any]:
        raise RuntimeError("local process backend does not support interactive send; use mailbox relay instead")

    def stop_plan(self, *, target: str, pid: int | None = None) -> list[list[str]]:
        if os.name == "nt":
            return [[self.executable, "verified-job-stop", "--supervisor-pid", str(pid or 0)]]
        return [[self.executable, "verified-stop", "--pid", str(pid or 0)]]

    def stop_actor(
        self,
        *,
        target: str,
        pid: int | None = None,
        process_start_marker: str | None = None,
        windows_job_name: str | None = None,
        windows_job_identity: str | None = None,
        windows_job_child_pid: int | None = None,
        windows_job_child_start_marker: str | None = None,
    ) -> dict[str, Any]:
        if not pid:
            raise RuntimeError("local process backend has no pid to stop")
        if not process_start_marker:
            raise RuntimeError("local process backend has no OS process identity marker; refusing unsafe PID-only stop")
        if os.name == "nt":
            receipt = validate_windows_job_receipt(
                supervisor_pid=pid,
                supervisor_start_marker=process_start_marker,
                job_name=windows_job_name,
                job_identity=windows_job_identity,
                child_pid=windows_job_child_pid,
                child_start_marker=windows_job_child_start_marker,
            )
            observation = stop_windows_job_runtime(receipt)
            rendered = command_to_string(
                [
                    self.executable,
                    "verified-job-stop",
                    "--job",
                    receipt.job_name,
                ]
            )
            return {"command": rendered, "commands": [rendered], **observation}
        verified_group = _verified_local_process_group(
            pid,
            process_start_marker,
            require_owned_group=True,
        )
        if verified_group is None:
            raise RuntimeError(
                "local process identity changed or its durable process-group evidence disappeared; "
                "refusing to stop a reused PID or PGID"
            )
        process_group, session_id, bound_group_members = verified_group
        anchor_pid, anchor_marker = _verified_group_anchor(
            leader_pid=pid,
            leader_marker=process_start_marker,
            process_group=process_group,
            session_id=session_id,
            bound_members=bound_group_members,
        )
        commands: list[str] = []

        def current_members() -> dict[int, str]:
            return _anchored_group_members(
                anchor_pid=anchor_pid,
                anchor_marker=anchor_marker,
                process_group=process_group,
                session_id=session_id,
            )

        def sweep_non_anchor(signal_number: int, *, deadline: float) -> dict[int, str]:
            signalled: set[tuple[int, str]] = set()
            while True:
                current = current_members()
                non_anchor = {
                    member_pid: member_marker
                    for member_pid, member_marker in current.items()
                    if member_pid != anchor_pid
                }
                if not non_anchor:
                    return current
                pending = {
                    member_pid: member_marker
                    for member_pid, member_marker in non_anchor.items()
                    if (member_pid, member_marker) not in signalled
                }
                if pending:
                    signalled.update(pending.items())
                    signal_commands = _signal_verified_members(
                        pending,
                        process_group=process_group,
                        session_id=session_id,
                        signal_number=signal_number,
                    )
                    commands.extend(command_to_string(argv) for argv in signal_commands)
                if time.monotonic() >= deadline:
                    return current
                time.sleep(0.05)

        current_group_members = sweep_non_anchor(
            signal.SIGTERM,
            deadline=time.monotonic() + 2.0,
        )
        non_anchor_members = {
            member_pid: member_marker
            for member_pid, member_marker in current_group_members.items()
            if member_pid != anchor_pid
        }
        if non_anchor_members:
            current_group_members = sweep_non_anchor(
                signal.SIGKILL,
                deadline=time.monotonic() + 1.0,
            )
            non_anchor_members = {
                member_pid: member_marker
                for member_pid, member_marker in current_group_members.items()
                if member_pid != anchor_pid
            }
            if non_anchor_members:
                raise RuntimeError(
                    "local actor process group retained non-supervisor members after "
                    "identity-verified SIGKILL escalation"
                )

        current_group_members = current_members()
        if anchor_pid in current_group_members:
            anchor_commands = _signal_verified_members(
                {anchor_pid: anchor_marker},
                process_group=process_group,
                session_id=session_id,
                signal_number=signal.SIGTERM,
            )
            commands.extend(command_to_string(argv) for argv in anchor_commands)
            anchor_deadline = time.monotonic() + 1.0
            current_group_members = current_members()
            while current_group_members and time.monotonic() < anchor_deadline:
                time.sleep(0.05)
                current_group_members = current_members()
            if current_group_members:
                if set(current_group_members) != {anchor_pid}:
                    raise RuntimeError(
                        "local actor process group created new members while its supervisor was stopping"
                    )
                kill_commands = _signal_verified_members(
                    {anchor_pid: anchor_marker},
                    process_group=process_group,
                    session_id=session_id,
                    signal_number=signal.SIGKILL,
                )
                commands.extend(command_to_string(argv) for argv in kill_commands)
                kill_deadline = time.monotonic() + 1.0
                current_group_members = current_members()
                while current_group_members and time.monotonic() < kill_deadline:
                    time.sleep(0.05)
                    current_group_members = current_members()
            if current_group_members:
                raise RuntimeError(
                    "local actor process-group supervisor remained live after identity-verified SIGKILL escalation"
                )

        remaining = _linux_process_group_members(process_group, session_id=session_id)
        if remaining:
            raise RuntimeError(
                "local actor process group remained live after its verified supervisor exited"
            )
        if not commands:
            commands = [
                command_to_string([self.executable, "verified-stop", "--pid", str(pid)])
            ]
        return {"command": commands[-1], "commands": commands}

    def actor_alive(
        self,
        *,
        session_name: str,
        actor_name: str,
        target: str | None = None,
        pid: int | None = None,
        process_start_marker: str | None = None,
        windows_job_name: str | None = None,
        windows_job_identity: str | None = None,
        windows_job_child_pid: int | None = None,
        windows_job_child_start_marker: str | None = None,
    ) -> bool:
        if not pid or not process_start_marker:
            return False
        if os.name == "nt":
            receipt = validate_windows_job_receipt(
                supervisor_pid=pid,
                supervisor_start_marker=process_start_marker,
                job_name=windows_job_name,
                job_identity=windows_job_identity,
                child_pid=windows_job_child_pid,
                child_start_marker=windows_job_child_start_marker,
            )
            return inspect_windows_job_runtime(receipt).job_present
        return (
            _verified_local_process_group(
                pid,
                process_start_marker,
                require_owned_group=True,
            )
            is not None
        )


def backend_from_session(session: dict[str, Any]) -> TmuxBackend | LocalProcessBackend:
    config = session_backend_config(session)
    kind = str(config.get("kind") or "local")
    if kind == "tmux":
        return TmuxBackend(str(config.get("executable") or "tmux"))
    if kind == "local":
        return LocalProcessBackend(str(config.get("executable") or "local-process"))
    raise SystemExit(f"Unsupported session backend: {kind}")


def platform_summary() -> dict[str, str]:
    return {
        "os_name": os.name,
        "system": platform.system(),
        "release": platform.release(),
    }
