from __future__ import annotations

import ctypes
import json
import os
import secrets
import subprocess
import sys
import time
from ctypes import wintypes
from pathlib import Path
from typing import Any


if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from costmarshal_v2.windows_job import WindowsJobApi, WindowsJobError  # noqa: E402


CREATE_SUSPENDED = 0x00000004
CREATE_NEW_PROCESS_GROUP = 0x00000200
CREATE_NO_WINDOW = 0x08000000
CREATE_UNICODE_ENVIRONMENT = 0x00000400
STARTF_USESTDHANDLES = 0x00000100


class STARTUPINFOW(ctypes.Structure):
    _fields_ = [
        ("cb", wintypes.DWORD),
        ("lpReserved", wintypes.LPWSTR),
        ("lpDesktop", wintypes.LPWSTR),
        ("lpTitle", wintypes.LPWSTR),
        ("dwX", wintypes.DWORD),
        ("dwY", wintypes.DWORD),
        ("dwXSize", wintypes.DWORD),
        ("dwYSize", wintypes.DWORD),
        ("dwXCountChars", wintypes.DWORD),
        ("dwYCountChars", wintypes.DWORD),
        ("dwFillAttribute", wintypes.DWORD),
        ("dwFlags", wintypes.DWORD),
        ("wShowWindow", wintypes.WORD),
        ("cbReserved2", wintypes.WORD),
        ("lpReserved2", ctypes.POINTER(wintypes.BYTE)),
        ("hStdInput", wintypes.HANDLE),
        ("hStdOutput", wintypes.HANDLE),
        ("hStdError", wintypes.HANDLE),
    ]


class PROCESS_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("hProcess", wintypes.HANDLE),
        ("hThread", wintypes.HANDLE),
        ("dwProcessId", wintypes.DWORD),
        ("dwThreadId", wintypes.DWORD),
    ]


def _configure_create_process(api: WindowsJobApi) -> None:
    kernel32 = api.kernel32
    kernel32.CreateProcessW.argtypes = [
        wintypes.LPCWSTR,
        wintypes.LPWSTR,
        ctypes.c_void_p,
        ctypes.c_void_p,
        wintypes.BOOL,
        wintypes.DWORD,
        ctypes.c_void_p,
        wintypes.LPCWSTR,
        ctypes.POINTER(STARTUPINFOW),
        ctypes.POINTER(PROCESS_INFORMATION),
    ]
    kernel32.CreateProcessW.restype = wintypes.BOOL
    kernel32.ResumeThread.argtypes = [wintypes.HANDLE]
    kernel32.ResumeThread.restype = wintypes.DWORD
    kernel32.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
    kernel32.GetExitCodeProcess.restype = wintypes.BOOL


def _command_line(command: str | list[str], *, shell: bool) -> str:
    if shell:
        if not isinstance(command, str):
            raise WindowsJobError("shell command must be a string")
        comspec = os.environ.get("COMSPEC") or str(Path(os.environ["SystemRoot"]) / "System32" / "cmd.exe")
        # Match subprocess.Popen(shell=True): cmd.exe expects one literal outer
        # quote pair around the already-rendered command, not CRT backslash
        # escaping of that command as a list2cmdline argument.
        return f'{subprocess.list2cmdline([comspec])} /d /s /c "{command}"'
    if not isinstance(command, list) or not command or not all(
        isinstance(item, str) and item for item in command
    ):
        raise WindowsJobError("direct command must be a non-empty string list")
    return subprocess.list2cmdline(command)


def _emit_receipt(payload: dict[str, Any]) -> None:
    sys.stdout.write(json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n")
    sys.stdout.flush()


def _start_suspended_child(
    api: WindowsJobApi,
    *,
    command: str | list[str],
    shell: bool,
    cwd: str,
    log_path: str,
    extra_environment: dict[str, str],
) -> tuple[int, int, int, str]:
    import msvcrt

    _configure_create_process(api)
    Path(log_path).parent.mkdir(parents=True, exist_ok=True)
    with open(log_path, "ab", buffering=0) as log_handle, open(os.devnull, "rb") as input_handle:
        log_os_handle = int(msvcrt.get_osfhandle(log_handle.fileno()))
        input_os_handle = int(msvcrt.get_osfhandle(input_handle.fileno()))
        os.set_handle_inheritable(log_os_handle, True)
        os.set_handle_inheritable(input_os_handle, True)
        try:
            startup = STARTUPINFOW()
            startup.cb = ctypes.sizeof(startup)
            startup.dwFlags = STARTF_USESTDHANDLES
            startup.hStdInput = wintypes.HANDLE(input_os_handle)
            startup.hStdOutput = wintypes.HANDLE(log_os_handle)
            startup.hStdError = wintypes.HANDLE(log_os_handle)
            process_info = PROCESS_INFORMATION()
            command_buffer = ctypes.create_unicode_buffer(
                _command_line(command, shell=shell)
            )
            environment = os.environ.copy()
            environment.update(extra_environment)
            environment_buffer = ctypes.create_unicode_buffer(
                "\0".join(
                    f"{key}={value}"
                    for key, value in sorted(environment.items(), key=lambda row: row[0].upper())
                )
                + "\0\0"
            )
            if not api.kernel32.CreateProcessW(
                None,
                command_buffer,
                None,
                None,
                True,
                CREATE_SUSPENDED
                | CREATE_NEW_PROCESS_GROUP
                | CREATE_NO_WINDOW
                | CREATE_UNICODE_ENVIRONMENT,
                environment_buffer,
                str(Path(cwd).resolve()),
                ctypes.byref(startup),
                ctypes.byref(process_info),
            ):
                raise WindowsJobError(
                    f"CreateProcessW(CREATE_SUSPENDED) failed: {ctypes.WinError(ctypes.get_last_error())}"
                )
        finally:
            os.set_handle_inheritable(log_os_handle, False)
            os.set_handle_inheritable(input_os_handle, False)
    process_handle = int(process_info.hProcess)
    thread_handle = int(process_info.hThread)
    child_pid = int(process_info.dwProcessId)
    try:
        child_marker = api.process_marker(process_handle)
    except BaseException:
        api.close(thread_handle)
        api.close(process_handle)
        raise
    return process_handle, thread_handle, child_pid, child_marker


def main() -> int:
    if os.name != "nt":
        raise SystemExit("Windows Job Object supervisor can run only on Windows")
    try:
        payload = json.loads(sys.stdin.readline())
    except (json.JSONDecodeError, OSError) as exc:
        _emit_receipt({"schema": "costmarshal-windows-job-receipt-v1", "status": "error", "error": str(exc)})
        return 70
    if not isinstance(payload, dict):
        _emit_receipt({"schema": "costmarshal-windows-job-receipt-v1", "status": "error", "error": "payload is not an object"})
        return 70

    api = WindowsJobApi()
    job_handle: int | None = None
    process_handle: int | None = None
    thread_handle: int | None = None
    receipt_sent = False
    child_resumed = False
    try:
        token = secrets.token_hex(32)
        job_name = f"Local\\CostMarshal-{token}"
        job_identity = f"windows-job-v1:{token}"
        job_handle = api.create_job(job_name)
        supervisor_pid = os.getpid()
        supervisor_marker = api.process_marker(int(api.kernel32.GetCurrentProcess()))
        process_handle, thread_handle, child_pid, child_marker = _start_suspended_child(
            api,
            command=payload.get("command"),
            shell=bool(payload.get("shell")),
            cwd=str(payload.get("cwd") or ""),
            log_path=str(payload.get("log_path") or ""),
            extra_environment={
                "COSTMARSHAL_WINDOWS_JOB_NAME": job_name,
                "COSTMARSHAL_WINDOWS_JOB_IDENTITY": job_identity,
                "COSTMARSHAL_WINDOWS_JOB_SUPERVISOR_PID": str(supervisor_pid),
                "COSTMARSHAL_WINDOWS_JOB_SUPERVISOR_MARKER": supervisor_marker,
            },
        )
        api.assign_process(job_handle, process_handle)
        api.query_limits(job_handle)
        supervisor_handle, supervisor_state = api.open_process_exact(
            supervisor_pid,
            supervisor_marker,
            terminate=False,
        )
        if supervisor_handle is None or supervisor_state != "alive":
            raise WindowsJobError("supervisor could not bind its own process handle identity")
        api.close(supervisor_handle)
        # Bind the supervisor and child to the same kernel object only after
        # the child was explicitly assigned while suspended. This makes a
        # persisted supervisor/job pairing non-swappable during recovery.
        api.assign_process(job_handle, int(api.kernel32.GetCurrentProcess()))
        active_pids = set(api.query_active_pids(job_handle))
        if supervisor_pid not in active_pids or child_pid not in active_pids:
            raise WindowsJobError(
                "prepared Job Object lacks its supervisor or suspended child"
            )
        receipt_payload = {
            "schema": "costmarshal-windows-job-receipt-v1",
            "supervisor_pid": supervisor_pid,
            "supervisor_start_marker": supervisor_marker,
            "job_name": job_name,
            "job_identity": job_identity,
            "child_pid": child_pid,
            "child_start_marker": child_marker,
        }
        _emit_receipt({**receipt_payload, "status": "prepared"})
        acknowledgement = json.loads(sys.stdin.readline())
        if (
            not isinstance(acknowledgement, dict)
            or acknowledgement.get("action") != "resume"
            or acknowledgement.get("job_identity") != job_identity
        ):
            raise WindowsJobError("parent did not acknowledge the exact prepared Job Object")
        resume_result = int(api.kernel32.ResumeThread(wintypes.HANDLE(thread_handle)))
        if resume_result == 0xFFFFFFFF:
            raise WindowsJobError(f"ResumeThread failed: {ctypes.WinError(ctypes.get_last_error())}")
        child_resumed = True
        api.close(thread_handle)
        thread_handle = None
        _emit_receipt({**receipt_payload, "status": "started"})
        receipt_sent = True
        try:
            sys.stdout.close()
        except OSError:
            pass
        api.wait_process(process_handle, 0xFFFFFFFF)
        exit_code = wintypes.DWORD()
        if not api.kernel32.GetExitCodeProcess(
            wintypes.HANDLE(process_handle), ctypes.byref(exit_code)
        ):
            raise WindowsJobError(
                f"GetExitCodeProcess failed: {ctypes.WinError(ctypes.get_last_error())}"
            )
        while set(api.query_active_pids(job_handle)) - {supervisor_pid}:
            time.sleep(0.05)
        return int(exit_code.value) if int(exit_code.value) != 0x103 else 1
    except BaseException as exc:  # noqa: BLE001 - the supervisor must contain every partial launch
        if job_handle is not None:
            try:
                api.terminate_job(job_handle)
            except BaseException:
                pass
        if process_handle is not None and not child_resumed:
            try:
                api.terminate_process_handle(process_handle)
            except BaseException:
                pass
        if not receipt_sent:
            try:
                _emit_receipt(
                    {
                        "schema": "costmarshal-windows-job-receipt-v1",
                        "status": "error",
                        "error": f"{type(exc).__name__}: {exc}",
                    }
                )
            except BaseException:
                pass
        return 70
    finally:
        api.close(thread_handle)
        api.close(process_handle)
        api.close(job_handle)


if __name__ == "__main__":
    raise SystemExit(main())
