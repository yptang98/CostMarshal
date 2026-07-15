from __future__ import annotations

import csv
import os
import platform
import shlex
import shutil
import signal
import subprocess
from pathlib import Path
from typing import Any

from .paths import ProjectLayout


ActorCommand = str | list[str]


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
        return True
    except OSError:
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


class TmuxBackend:
    kind = "tmux"

    def __init__(self, executable: str = "tmux") -> None:
        self.executable = executable

    def available(self) -> bool:
        return shutil.which(self.executable) is not None

    def _run(self, argv: list[str], *, check: bool = True) -> subprocess.CompletedProcess[str]:
        return subprocess.run(argv, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=check)

    def session_exists(self, session_name_value: str) -> bool:
        if not self.available():
            return False
        result = self._run([self.executable, "has-session", "-t", session_name_value], check=False)
        return result.returncode == 0

    def list_actors(self, session_name_value: str) -> list[str]:
        if not self.available() or not self.session_exists(session_name_value):
            return []
        result = self._run([self.executable, "list-windows", "-t", session_name_value, "-F", "#{window_name}"], check=False)
        if result.returncode != 0:
            return []
        return [line.strip() for line in result.stdout.splitlines() if line.strip()]

    def start_plan(self, *, session_name: str, actor_name: str, command: ActorCommand, session_exists: bool | None = None) -> list[list[str]]:
        command_text = command_display(command)
        exists = self.session_exists(session_name) if session_exists is None else session_exists
        if exists:
            return [[self.executable, "new-window", "-t", session_name, "-n", actor_name, command_text]]
        return [[self.executable, "new-session", "-d", "-s", session_name, "-n", actor_name, command_text]]

    def start_actor(self, *, session_name: str, actor_name: str, command: ActorCommand, cwd: Path, log_path: Path) -> dict[str, Any]:
        if not self.available():
            raise RuntimeError(f"tmux executable not found: {self.executable}")
        commands = self.start_plan(session_name=session_name, actor_name=actor_name, command=command)
        for argv in commands:
            result = self._run(argv, check=False)
            if result.returncode != 0:
                raise RuntimeError(f"tmux command failed: {command_to_string(argv)}\n{result.stderr.strip()}")
        return {
            "commands": [command_to_string(argv) for argv in commands],
            "target": f"{session_name}:{actor_name}",
            "pid": None,
            "log_path": None,
        }

    def send_text(self, *, target: str, text: str) -> dict[str, Any]:
        if not self.available():
            raise RuntimeError(f"tmux executable not found: {self.executable}")
        argv = [self.executable, "send-keys", "-t", target, text, "Enter"]
        result = self._run(argv, check=False)
        if result.returncode != 0:
            raise RuntimeError(f"tmux send-keys failed: {result.stderr.strip()}")
        return {"command": command_to_string(argv)}

    def stop_plan(self, *, target: str, pid: int | None = None) -> list[list[str]]:
        return [[self.executable, "kill-window", "-t", target]]

    def stop_actor(
        self,
        *,
        target: str,
        pid: int | None = None,
        process_start_marker: str | None = None,
    ) -> dict[str, Any]:
        if not self.available():
            raise RuntimeError(f"tmux executable not found: {self.executable}")
        argv = [self.executable, "kill-window", "-t", target]
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

    def start_plan(self, *, session_name: str, actor_name: str, command: ActorCommand, session_exists: bool | None = None) -> list[list[str]]:
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
        return [["kill", "-TERM", str(pid or 0)]]

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
        os.kill(pid, signal.SIGTERM)
        return {"command": command_to_string(["kill", "-TERM", str(pid)])}

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
