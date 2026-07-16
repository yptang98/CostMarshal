from __future__ import annotations

import csv
import os
import platform
import re
import shlex
import shutil
import signal
import subprocess
import time
from pathlib import Path
from typing import Any

from .paths import ProjectLayout


ActorCommand = str | list[str]
_TMUX_COMPONENT_RE = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9_-]{0,62}[A-Za-z0-9])?\Z")
_TMUX_LEGACY_ACTOR_RE = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9_.-]{0,62}[A-Za-z0-9])?\Z")
_TMUX_WINDOW_ID_RE = re.compile(r"@[0-9]+\Z")


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
            result = subprocess.run(
                ["tasklist", "/FI", f"PID eq {pid}", "/FO", "CSV", "/NH"],
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )
            return _windows_tasklist_contains_pid(result.stdout, pid)
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
    stat_path = Path(f"/proc/{int(pid)}/stat")
    try:
        raw = stat_path.read_text(encoding="utf-8")
        fields = raw[raw.rfind(")") + 2 :].split()
        start_ticks = fields[19]
        boot_id_path = Path("/proc/sys/kernel/random/boot_id")
        boot_id = boot_id_path.read_text(encoding="ascii").strip() if boot_id_path.is_file() else "unknown"
        return f"linux-proc:{boot_id}:{start_ticks}"
    except (IndexError, OSError, ValueError):
        return None


def pid_identity_matches(pid: int | None, marker: str | None) -> bool:
    return bool(marker) and pid_start_marker(pid) == marker


def _linux_process_group_members(process_group: int) -> dict[int, str]:
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
                raw = stat_path.read_text(encoding="utf-8")
                fields = raw[raw.rfind(")") + 2 :].split()
                state = fields[0]
                member_group = int(fields[2])
                start_ticks = fields[19]
                member_pid = int(stat_path.parent.name)
            except (IndexError, OSError, ValueError):
                continue
            if member_group == process_group and state != "Z":
                members[member_pid] = f"linux-proc:{boot_id}:{start_ticks}"
    except OSError:
        return {}
    return members


def _posix_process_group_alive(process_group: int) -> bool:
    """Return whether a Linux process group still has a non-zombie member."""

    if Path("/proc").is_dir():
        return bool(_linux_process_group_members(process_group))
    try:
        os.killpg(process_group, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


class TmuxBackend:
    kind = "tmux"

    def __init__(self, executable: str = "tmux") -> None:
        self.executable = executable

    def available(self) -> bool:
        return shutil.which(self.executable) is not None

    def _run(self, argv: list[str], *, check: bool = True) -> subprocess.CompletedProcess[str]:
        return subprocess.run(argv, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=check)

    def _resolve_actor_target(self, target: str) -> str:
        validate_persisted_tmux_target(target)
        session_value, actor_value = target.split(":", 1)
        try:
            validate_tmux_name(actor_value, label="actor name")
        except RuntimeError:
            if not self.available():
                raise RuntimeError(
                    "tmux is required to resolve a legacy dotted actor window"
                )
            result = self._run(
                [
                    self.executable,
                    "list-windows",
                    "-t",
                    tmux_persisted_session_target(session_value),
                    "-F",
                    "#{window_id}\t#{window_name}",
                ],
                check=False,
            )
            if result.returncode != 0:
                raise RuntimeError(f"tmux legacy actor lookup failed: {result.stderr.strip()}")
            matches: list[str] = []
            for line in result.stdout.splitlines():
                window_id, separator, window_name = line.partition("\t")
                if separator and window_name == actor_value and _TMUX_WINDOW_ID_RE.fullmatch(window_id):
                    matches.append(window_id)
            if len(matches) != 1:
                raise RuntimeError(
                    f"tmux legacy actor lookup requires one exact window, found {len(matches)}"
                )
            return matches[0]
        return f"={session_value}:={actor_value}"

    def session_exists(self, session_name_value: str) -> bool:
        target = tmux_persisted_session_target(session_name_value)
        if not self.available():
            return False
        result = self._run([self.executable, "has-session", "-t", target], check=False)
        return result.returncode == 0

    def list_actors(self, session_name_value: str) -> list[str]:
        target = tmux_persisted_session_target(session_name_value)
        if not self.available() or not self.session_exists(session_name_value):
            return []
        result = self._run([self.executable, "list-windows", "-t", target, "-F", "#{window_name}"], check=False)
        if result.returncode != 0:
            return []
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
        exists = self.session_exists(session_name) if session_exists is None else session_exists
        cwd_args = ["-c", tmux_format_literal(str(Path(cwd).resolve()))] if cwd is not None else []
        if exists:
            create = [
                self.executable,
                "new-window",
                "-d",
                "-t",
                tmux_persisted_new_window_target(session_name),
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

    def start_actor(self, *, session_name: str, actor_name: str, command: ActorCommand, cwd: Path, log_path: Path) -> dict[str, Any]:
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

    def start_actor(self, *, session_name: str, actor_name: str, command: ActorCommand, cwd: Path, log_path: Path) -> dict[str, Any]:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_handle = log_path.open("a", encoding="utf-8")
        creationflags = 0
        start_new_session = False
        if os.name == "nt":
            creationflags = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS
        else:
            start_new_session = True
        process = subprocess.Popen(
            command,
            cwd=str(cwd),
            shell=isinstance(command, str),
            stdin=subprocess.DEVNULL,
            stdout=log_handle,
            stderr=subprocess.STDOUT,
            text=True,
            creationflags=creationflags,
            start_new_session=start_new_session,
        )
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
            return [["taskkill", "/PID", str(pid or 0), "/T", "/F"]]
        return [["kill", "-TERM", "--", f"-{pid or 0}"]]

    def stop_actor(
        self,
        *,
        target: str,
        pid: int | None = None,
        process_start_marker: str | None = None,
    ) -> dict[str, Any]:
        if not pid:
            raise RuntimeError("local process backend has no pid to stop")
        if not process_start_marker:
            raise RuntimeError("local process backend has no OS process identity marker; refusing unsafe PID-only stop")
        if not pid_identity_matches(pid, process_start_marker):
            raise RuntimeError("local process identity changed; refusing to stop a reused PID")
        if os.name == "nt":
            argv = ["taskkill", "/PID", str(pid), "/T", "/F"]
            result = subprocess.run(argv, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)
            if result.returncode != 0 and pid_is_alive(pid):
                raise RuntimeError(f"taskkill failed: {result.stderr.strip() or result.stdout.strip()}")
            return {"command": command_to_string(argv)}
        try:
            process_group = os.getpgid(pid)
        except ProcessLookupError as exc:
            raise RuntimeError(
                "local actor process group disappeared before its identity could be verified"
            ) from exc
        if process_group != pid:
            raise RuntimeError(
                "local process backend actor is not its process-group leader; "
                "refusing an unsafe group stop"
            )
        bound_group_members = _linux_process_group_members(process_group)
        if Path("/proc").is_dir() and bound_group_members.get(pid) != process_start_marker:
            raise RuntimeError(
                "local actor process-group membership does not match its verified start marker"
            )

        term_argv = ["kill", "-TERM", "--", f"-{process_group}"]
        try:
            os.killpg(process_group, signal.SIGTERM)
        except ProcessLookupError:
            return {"command": command_to_string(term_argv), "commands": [command_to_string(term_argv)]}
        deadline = time.monotonic() + 2.0
        while _posix_process_group_alive(process_group) and time.monotonic() < deadline:
            time.sleep(0.05)
        commands = [command_to_string(term_argv)]
        if _posix_process_group_alive(process_group):
            # Escalate only while the verified leader or another member bound
            # before TERM still has the same creation marker in this PGID.
            # Otherwise fail closed rather than risk signaling a reused group.
            try:
                leader_still_bound = (
                    pid_identity_matches(pid, process_start_marker)
                    and os.getpgid(pid) == process_group
                )
            except ProcessLookupError:
                leader_still_bound = False
            current_group_members = _linux_process_group_members(process_group)
            bound_member_survives = any(
                current_group_members.get(member_pid) == member_marker
                for member_pid, member_marker in bound_group_members.items()
            )
            safe_to_escalate = leader_still_bound or bound_member_survives
            if not safe_to_escalate:
                raise RuntimeError(
                    "local actor process group did not stop and no bound member identity survives; "
                    "refusing unsafe SIGKILL escalation"
                )
            kill_argv = ["kill", "-KILL", "--", f"-{process_group}"]
            commands.append(command_to_string(kill_argv))
            try:
                os.killpg(process_group, signal.SIGKILL)
            except ProcessLookupError:
                return {"command": commands[-1], "commands": commands}
            kill_deadline = time.monotonic() + 1.0
            while _posix_process_group_alive(process_group) and time.monotonic() < kill_deadline:
                time.sleep(0.05)
            if _posix_process_group_alive(process_group):
                raise RuntimeError("local actor process group remained live after safe SIGKILL escalation")
        return {"command": commands[-1], "commands": commands}

    def actor_alive(
        self,
        *,
        session_name: str,
        actor_name: str,
        target: str | None = None,
        pid: int | None = None,
        process_start_marker: str | None = None,
    ) -> bool:
        alive = pid_is_alive(pid)
        if not alive or not process_start_marker:
            return alive
        return pid_identity_matches(pid, process_start_marker)


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
