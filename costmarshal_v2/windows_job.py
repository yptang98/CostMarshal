from __future__ import annotations

import ctypes
import os
import re
import time
from ctypes import wintypes
from dataclasses import dataclass
from typing import Any


ERROR_FILE_NOT_FOUND = 2
ERROR_ACCESS_DENIED = 5
ERROR_INVALID_HANDLE = 6
ERROR_INVALID_PARAMETER = 87
ERROR_ALREADY_EXISTS = 183
ERROR_MORE_DATA = 234
WAIT_OBJECT_0 = 0
WAIT_TIMEOUT = 258
INFINITE = 0xFFFFFFFF
STILL_ACTIVE = 259

PROCESS_TERMINATE = 0x0001
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
SYNCHRONIZE = 0x00100000
JOB_OBJECT_ASSIGN_PROCESS = 0x0001
JOB_OBJECT_QUERY = 0x0004
JOB_OBJECT_TERMINATE = 0x0008
JOB_OBJECT_LIMIT_BREAKAWAY_OK = 0x00000800
JOB_OBJECT_LIMIT_SILENT_BREAKAWAY_OK = 0x00001000
JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000
JOB_OBJECT_BASIC_ACCOUNTING_INFORMATION_CLASS = 1
JOB_OBJECT_BASIC_PROCESS_ID_LIST_CLASS = 3
JOB_OBJECT_EXTENDED_LIMIT_INFORMATION_CLASS = 9

_WINDOWS_JOB_IDENTITY_RE = re.compile(r"windows-job-v1:([0-9a-f]{64})\Z")
_WINDOWS_JOB_NAME_RE = re.compile(r"Local\\CostMarshal-([0-9a-f]{64})\Z")
_WINDOWS_PROCESS_MARKER_RE = re.compile(r"windows-filetime:([0-9]+)\Z")


class WindowsJobError(RuntimeError):
    """A Windows runtime identity or Job Object operation is not trustworthy."""


class LARGE_INTEGER(ctypes.Structure):
    _fields_ = [("QuadPart", ctypes.c_longlong)]


class IO_COUNTERS(ctypes.Structure):
    _fields_ = [
        ("ReadOperationCount", ctypes.c_ulonglong),
        ("WriteOperationCount", ctypes.c_ulonglong),
        ("OtherOperationCount", ctypes.c_ulonglong),
        ("ReadTransferCount", ctypes.c_ulonglong),
        ("WriteTransferCount", ctypes.c_ulonglong),
        ("OtherTransferCount", ctypes.c_ulonglong),
    ]


class JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("PerProcessUserTimeLimit", LARGE_INTEGER),
        ("PerJobUserTimeLimit", LARGE_INTEGER),
        ("LimitFlags", wintypes.DWORD),
        ("MinimumWorkingSetSize", ctypes.c_size_t),
        ("MaximumWorkingSetSize", ctypes.c_size_t),
        ("ActiveProcessLimit", wintypes.DWORD),
        ("Affinity", ctypes.c_size_t),
        ("PriorityClass", wintypes.DWORD),
        ("SchedulingClass", wintypes.DWORD),
    ]


class JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("BasicLimitInformation", JOBOBJECT_BASIC_LIMIT_INFORMATION),
        ("IoInfo", IO_COUNTERS),
        ("ProcessMemoryLimit", ctypes.c_size_t),
        ("JobMemoryLimit", ctypes.c_size_t),
        ("PeakProcessMemoryUsed", ctypes.c_size_t),
        ("PeakJobMemoryUsed", ctypes.c_size_t),
    ]


class JOBOBJECT_BASIC_ACCOUNTING_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("TotalUserTime", LARGE_INTEGER),
        ("TotalKernelTime", LARGE_INTEGER),
        ("ThisPeriodTotalUserTime", LARGE_INTEGER),
        ("ThisPeriodTotalKernelTime", LARGE_INTEGER),
        ("TotalPageFaultCount", wintypes.DWORD),
        ("TotalProcesses", wintypes.DWORD),
        ("ActiveProcesses", wintypes.DWORD),
        ("TotalTerminatedProcesses", wintypes.DWORD),
    ]


@dataclass(frozen=True)
class WindowsJobReceipt:
    supervisor_pid: int
    supervisor_start_marker: str
    job_name: str
    job_identity: str
    child_pid: int | None = None
    child_start_marker: str | None = None


@dataclass(frozen=True)
class WindowsJobInspection:
    job_present: bool
    active_pids: tuple[int, ...]
    supervisor_state: str


def _require_windows() -> None:
    if os.name != "nt":
        raise WindowsJobError("Windows Job Objects are available only on Windows")


def validate_windows_job_receipt(
    *,
    supervisor_pid: int | None,
    supervisor_start_marker: str | None,
    job_name: str | None,
    job_identity: str | None,
    child_pid: int | None = None,
    child_start_marker: str | None = None,
) -> WindowsJobReceipt:
    try:
        pid = int(supervisor_pid or 0)
    except (TypeError, ValueError) as exc:
        raise WindowsJobError("Windows local runtime supervisor PID is invalid") from exc
    identity_match = _WINDOWS_JOB_IDENTITY_RE.fullmatch(str(job_identity or ""))
    name_match = _WINDOWS_JOB_NAME_RE.fullmatch(str(job_name or ""))
    marker_match = _WINDOWS_PROCESS_MARKER_RE.fullmatch(
        str(supervisor_start_marker or "")
    )
    if pid <= 0 or marker_match is None or identity_match is None or name_match is None:
        raise WindowsJobError(
            "Windows local runtime lacks a complete handle-bound Job Object receipt; "
            "legacy PID-only actors require manual recovery and cannot be stopped"
        )
    if identity_match.group(1) != name_match.group(1):
        raise WindowsJobError("Windows Job Object name does not match its persisted identity")
    normalized_child_pid: int | None = None
    if child_pid is not None:
        try:
            normalized_child_pid = int(child_pid)
        except (TypeError, ValueError) as exc:
            raise WindowsJobError("Windows Job Object child PID is invalid") from exc
        if normalized_child_pid <= 0:
            raise WindowsJobError("Windows Job Object child PID is invalid")
        if not _WINDOWS_PROCESS_MARKER_RE.fullmatch(str(child_start_marker or "")):
            raise WindowsJobError("Windows Job Object child identity marker is invalid")
    elif child_start_marker is not None:
        raise WindowsJobError("Windows Job Object has a child marker without a child PID")
    return WindowsJobReceipt(
        supervisor_pid=pid,
        supervisor_start_marker=str(supervisor_start_marker),
        job_name=str(job_name),
        job_identity=str(job_identity),
        child_pid=normalized_child_pid,
        child_start_marker=str(child_start_marker) if child_start_marker is not None else None,
    )


class WindowsJobApi:
    """Small injectable Win32 surface so PID-reuse behavior is unit-testable."""

    def __init__(self) -> None:
        _require_windows()
        self.kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        self._configure()

    def _configure(self) -> None:
        k32 = self.kernel32
        k32.CloseHandle.argtypes = [wintypes.HANDLE]
        k32.CloseHandle.restype = wintypes.BOOL
        k32.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
        k32.CreateJobObjectW.restype = wintypes.HANDLE
        k32.OpenJobObjectW.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.LPCWSTR]
        k32.OpenJobObjectW.restype = wintypes.HANDLE
        k32.SetInformationJobObject.argtypes = [
            wintypes.HANDLE,
            ctypes.c_int,
            ctypes.c_void_p,
            wintypes.DWORD,
        ]
        k32.SetInformationJobObject.restype = wintypes.BOOL
        k32.QueryInformationJobObject.argtypes = [
            wintypes.HANDLE,
            ctypes.c_int,
            ctypes.c_void_p,
            wintypes.DWORD,
            ctypes.POINTER(wintypes.DWORD),
        ]
        k32.QueryInformationJobObject.restype = wintypes.BOOL
        k32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
        k32.AssignProcessToJobObject.restype = wintypes.BOOL
        k32.IsProcessInJob.argtypes = [
            wintypes.HANDLE,
            wintypes.HANDLE,
            ctypes.POINTER(wintypes.BOOL),
        ]
        k32.IsProcessInJob.restype = wintypes.BOOL
        k32.TerminateJobObject.argtypes = [wintypes.HANDLE, wintypes.UINT]
        k32.TerminateJobObject.restype = wintypes.BOOL
        k32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        k32.OpenProcess.restype = wintypes.HANDLE
        k32.GetProcessTimes.argtypes = [
            wintypes.HANDLE,
            ctypes.POINTER(wintypes.FILETIME),
            ctypes.POINTER(wintypes.FILETIME),
            ctypes.POINTER(wintypes.FILETIME),
            ctypes.POINTER(wintypes.FILETIME),
        ]
        k32.GetProcessTimes.restype = wintypes.BOOL
        k32.GetCurrentProcess.argtypes = []
        k32.GetCurrentProcess.restype = wintypes.HANDLE
        k32.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
        k32.GetExitCodeProcess.restype = wintypes.BOOL
        k32.TerminateProcess.argtypes = [wintypes.HANDLE, wintypes.UINT]
        k32.TerminateProcess.restype = wintypes.BOOL
        k32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        k32.WaitForSingleObject.restype = wintypes.DWORD

    def close(self, handle: int | None) -> None:
        if handle:
            self.kernel32.CloseHandle(wintypes.HANDLE(handle))

    def create_job(self, name: str) -> int:
        ctypes.set_last_error(0)
        handle = self.kernel32.CreateJobObjectW(None, name)
        if not handle:
            raise WindowsJobError(
                f"CreateJobObjectW failed for the durable runtime: {ctypes.WinError(ctypes.get_last_error())}"
            )
        numeric = int(handle)
        if ctypes.get_last_error() == ERROR_ALREADY_EXISTS:
            self.close(numeric)
            raise WindowsJobError("Windows Job Object identity collided with an existing object")
        limits = JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
        # The default has both breakaway flags clear. Set it explicitly so a
        # later query can prove the runner and every descendant stayed bound.
        limits.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if not self.kernel32.SetInformationJobObject(
            wintypes.HANDLE(numeric),
            JOB_OBJECT_EXTENDED_LIMIT_INFORMATION_CLASS,
            ctypes.byref(limits),
            ctypes.sizeof(limits),
        ):
            error = ctypes.WinError(ctypes.get_last_error())
            self.close(numeric)
            raise WindowsJobError(f"SetInformationJobObject failed: {error}")
        return numeric

    def open_job(self, name: str, *, terminate: bool) -> int | None:
        access = JOB_OBJECT_QUERY | (JOB_OBJECT_TERMINATE if terminate else 0)
        ctypes.set_last_error(0)
        handle = self.kernel32.OpenJobObjectW(access, False, name)
        if handle:
            return int(handle)
        error = ctypes.get_last_error()
        if error == ERROR_FILE_NOT_FOUND:
            return None
        raise WindowsJobError(f"OpenJobObjectW failed: {ctypes.WinError(error)}")

    def query_limits(self, job_handle: int) -> int:
        limits = JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
        if not self.kernel32.QueryInformationJobObject(
            wintypes.HANDLE(job_handle),
            JOB_OBJECT_EXTENDED_LIMIT_INFORMATION_CLASS,
            ctypes.byref(limits),
            ctypes.sizeof(limits),
            None,
        ):
            raise WindowsJobError(
                f"QueryInformationJobObject(limits) failed: {ctypes.WinError(ctypes.get_last_error())}"
            )
        flags = int(limits.BasicLimitInformation.LimitFlags)
        if flags & (JOB_OBJECT_LIMIT_BREAKAWAY_OK | JOB_OBJECT_LIMIT_SILENT_BREAKAWAY_OK):
            raise WindowsJobError("Windows Job Object unexpectedly permits process breakaway")
        if not flags & JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE:
            raise WindowsJobError(
                "Windows Job Object lacks kill-on-last-handle-close containment"
            )
        return flags

    def query_active_pids(self, job_handle: int) -> tuple[int, ...]:
        accounting = JOBOBJECT_BASIC_ACCOUNTING_INFORMATION()
        if not self.kernel32.QueryInformationJobObject(
            wintypes.HANDLE(job_handle),
            JOB_OBJECT_BASIC_ACCOUNTING_INFORMATION_CLASS,
            ctypes.byref(accounting),
            ctypes.sizeof(accounting),
            None,
        ):
            raise WindowsJobError(
                f"QueryInformationJobObject(accounting) failed: {ctypes.WinError(ctypes.get_last_error())}"
            )
        capacity = max(8, int(accounting.ActiveProcesses) + 4)
        while capacity <= 65536:
            class PROCESS_ID_LIST(ctypes.Structure):
                _fields_ = [
                    ("NumberOfAssignedProcesses", wintypes.DWORD),
                    ("NumberOfProcessIdsInList", wintypes.DWORD),
                    ("ProcessIdList", ctypes.c_size_t * capacity),
                ]

            rows = PROCESS_ID_LIST()
            ctypes.set_last_error(0)
            ok = self.kernel32.QueryInformationJobObject(
                wintypes.HANDLE(job_handle),
                JOB_OBJECT_BASIC_PROCESS_ID_LIST_CLASS,
                ctypes.byref(rows),
                ctypes.sizeof(rows),
                None,
            )
            if ok:
                count = int(rows.NumberOfProcessIdsInList)
                return tuple(int(rows.ProcessIdList[index]) for index in range(count))
            error = ctypes.get_last_error()
            if error != ERROR_MORE_DATA:
                raise WindowsJobError(
                    f"QueryInformationJobObject(process list) failed: {ctypes.WinError(error)}"
                )
            capacity = max(capacity * 2, int(rows.NumberOfAssignedProcesses) + 4)
        raise WindowsJobError("Windows Job Object process list exceeded the safety bound")

    def assign_process(self, job_handle: int, process_handle: int) -> None:
        if not self.kernel32.AssignProcessToJobObject(
            wintypes.HANDLE(job_handle), wintypes.HANDLE(process_handle)
        ):
            raise WindowsJobError(
                f"AssignProcessToJobObject failed: {ctypes.WinError(ctypes.get_last_error())}"
            )

    def process_is_in_job(self, process_handle: int, job_handle: int) -> bool:
        result = wintypes.BOOL()
        if not self.kernel32.IsProcessInJob(
            wintypes.HANDLE(process_handle),
            wintypes.HANDLE(job_handle),
            ctypes.byref(result),
        ):
            raise WindowsJobError(
                f"IsProcessInJob failed: {ctypes.WinError(ctypes.get_last_error())}"
            )
        return bool(result.value)

    def open_process_exact(self, pid: int, marker: str, *, terminate: bool) -> tuple[int | None, str]:
        access = PROCESS_QUERY_LIMITED_INFORMATION | SYNCHRONIZE
        if terminate:
            access |= PROCESS_TERMINATE
        ctypes.set_last_error(0)
        handle = self.kernel32.OpenProcess(access, False, int(pid))
        if not handle:
            error = ctypes.get_last_error()
            if error == ERROR_INVALID_PARAMETER:
                return None, "absent"
            raise WindowsJobError(f"OpenProcess failed: {ctypes.WinError(error)}")
        numeric = int(handle)
        try:
            observed = self.process_marker(numeric)
        except BaseException:
            self.close(numeric)
            raise
        if observed != marker:
            self.close(numeric)
            return None, "identity_changed"
        status = self.kernel32.WaitForSingleObject(wintypes.HANDLE(numeric), 0)
        if status == WAIT_OBJECT_0:
            self.close(numeric)
            return None, "exited"
        if status != WAIT_TIMEOUT:
            self.close(numeric)
            raise WindowsJobError(f"WaitForSingleObject failed with status {int(status)}")
        return numeric, "alive"

    def process_marker(self, process_handle: int) -> str:
        created = wintypes.FILETIME()
        exited = wintypes.FILETIME()
        kernel = wintypes.FILETIME()
        user = wintypes.FILETIME()
        if not self.kernel32.GetProcessTimes(
            wintypes.HANDLE(process_handle),
            ctypes.byref(created),
            ctypes.byref(exited),
            ctypes.byref(kernel),
            ctypes.byref(user),
        ):
            raise WindowsJobError(
                f"GetProcessTimes failed: {ctypes.WinError(ctypes.get_last_error())}"
            )
        value = (int(created.dwHighDateTime) << 32) | int(created.dwLowDateTime)
        return f"windows-filetime:{value}"

    def terminate_job(self, job_handle: int, exit_code: int = 1) -> None:
        if not self.kernel32.TerminateJobObject(wintypes.HANDLE(job_handle), exit_code):
            raise WindowsJobError(
                f"TerminateJobObject failed: {ctypes.WinError(ctypes.get_last_error())}"
            )

    def terminate_process_handle(self, process_handle: int, exit_code: int = 1) -> None:
        if not self.kernel32.TerminateProcess(wintypes.HANDLE(process_handle), exit_code):
            error = ctypes.get_last_error()
            wait_status = self.kernel32.WaitForSingleObject(wintypes.HANDLE(process_handle), 0)
            if wait_status != WAIT_OBJECT_0:
                raise WindowsJobError(f"TerminateProcess failed: {ctypes.WinError(error)}")

    def wait_process(self, process_handle: int, timeout_ms: int) -> bool:
        status = self.kernel32.WaitForSingleObject(
            wintypes.HANDLE(process_handle), wintypes.DWORD(timeout_ms)
        )
        if status == WAIT_OBJECT_0:
            return True
        if status == WAIT_TIMEOUT:
            return False
        raise WindowsJobError(f"WaitForSingleObject failed with status {int(status)}")


def inspect_windows_job_runtime(
    receipt: WindowsJobReceipt,
    *,
    api: Any | None = None,
) -> WindowsJobInspection:
    api = api or WindowsJobApi()
    supervisor_handle: int | None = None
    job_handle: int | None = None
    try:
        supervisor_handle, supervisor_state = api.open_process_exact(
            receipt.supervisor_pid,
            receipt.supervisor_start_marker,
            terminate=False,
        )
        job_handle = api.open_job(receipt.job_name, terminate=False)
        if job_handle is None:
            if supervisor_state == "alive":
                raise WindowsJobError(
                    "Windows runtime supervisor is live but its bound Job Object is missing"
                )
            return WindowsJobInspection(False, (), supervisor_state)
        api.query_limits(job_handle)
        active_pids = tuple(api.query_active_pids(job_handle))
        if supervisor_state != "alive" or receipt.supervisor_pid not in active_pids:
            raise WindowsJobError(
                "Windows Job Object is not kernel-bound to its exact supervisor identity"
            )
        # A present cryptographically named job is still cleanup work even
        # while its last process is exiting. Returning live ensures STOP opens
        # and closes the exact kernel object instead of guessing from a PID.
        return WindowsJobInspection(True, active_pids, supervisor_state)
    finally:
        api.close(job_handle)
        api.close(supervisor_handle)


def stop_windows_job_runtime(
    receipt: WindowsJobReceipt,
    *,
    api: Any | None = None,
    timeout_seconds: float = 5.0,
) -> dict[str, Any]:
    api = api or WindowsJobApi()
    supervisor_handle: int | None = None
    child_handle: int | None = None
    job_handle: int | None = None
    deadline = time.monotonic() + max(0.1, timeout_seconds)

    def ensure_exited(process_handle: int | None, *, label: str) -> None:
        if process_handle is None:
            return
        remaining_ms = max(1, int(max(0.0, deadline - time.monotonic()) * 1000))
        if not api.wait_process(process_handle, remaining_ms):
            api.terminate_process_handle(process_handle)
            if not api.wait_process(process_handle, 1000):
                raise WindowsJobError(
                    f"Windows runtime {label} survived handle-bound termination"
                )

    try:
        # Open the process once and retain that exact handle through termination.
        # A recycled numeric PID can never redirect the later TerminateProcess.
        supervisor_handle, supervisor_state = api.open_process_exact(
            receipt.supervisor_pid,
            receipt.supervisor_start_marker,
            terminate=True,
        )
        child_state = "unrecorded"
        if receipt.child_pid is not None and receipt.child_start_marker is not None:
            child_handle, child_state = api.open_process_exact(
                receipt.child_pid,
                receipt.child_start_marker,
                terminate=True,
            )
        job_handle = api.open_job(receipt.job_name, terminate=True)
        if job_handle is None:
            if supervisor_state == "alive":
                raise WindowsJobError(
                    "Windows runtime supervisor is live but its bound Job Object is missing"
                )
            ensure_exited(child_handle, label="prepared child")
            return {
                "source": "windows_job_already_absent",
                "job_name": receipt.job_name,
                "supervisor_state": supervisor_state,
                "active_processes_before": 0,
            }
        api.query_limits(job_handle)
        active_before = tuple(api.query_active_pids(job_handle))
        if supervisor_state != "alive" or receipt.supervisor_pid not in active_before:
            raise WindowsJobError(
                "Windows Job Object is not kernel-bound to its exact supervisor identity"
            )
        if child_state == "alive" and receipt.child_pid not in active_before:
            raise WindowsJobError(
                "Windows Job Object is not kernel-bound to its exact prepared child identity"
            )
        api.terminate_job(job_handle)
        while api.query_active_pids(job_handle):
            if time.monotonic() >= deadline:
                raise WindowsJobError(
                    "Windows Job Object retained active processes after TerminateJobObject"
                )
            time.sleep(0.02)
        # QueryInformationJobObject can reach zero while terminated process
        # objects are still completing exit. Retain the exact handles and wait
        # for their signaled state so inherited log/pipe handles are closed
        # before STOP reports success.
        ensure_exited(child_handle, label="prepared child")
        ensure_exited(supervisor_handle, label="supervisor")
        return {
            "source": "windows_job_stop",
            "job_name": receipt.job_name,
            "supervisor_state": supervisor_state,
            "active_processes_before": len(active_before),
        }
    finally:
        api.close(job_handle)
        api.close(child_handle)
        api.close(supervisor_handle)


def verify_current_process_in_windows_job(
    receipt: WindowsJobReceipt,
    *,
    api: Any | None = None,
) -> None:
    """Prove inherited receipt values bind this runner to the exact live job."""

    api = api or WindowsJobApi()
    if receipt.child_pid is None or receipt.child_start_marker is None:
        raise WindowsJobError(
            "Windows runner registration requires its exact child PID and FILETIME receipt"
        )
    if os.getpid() != receipt.child_pid:
        raise WindowsJobError(
            "Windows runner PID does not match its prepared child receipt"
        )
    supervisor_handle: int | None = None
    job_handle: int | None = None
    try:
        supervisor_handle, supervisor_state = api.open_process_exact(
            receipt.supervisor_pid,
            receipt.supervisor_start_marker,
            terminate=False,
        )
        if supervisor_state != "alive" or supervisor_handle is None:
            raise WindowsJobError("Windows Job Object supervisor is not the exact live process")
        job_handle = api.open_job(receipt.job_name, terminate=False)
        if job_handle is None:
            raise WindowsJobError("Windows Job Object is absent during runner registration")
        api.query_limits(job_handle)
        active_pids = tuple(api.query_active_pids(job_handle))
        if receipt.supervisor_pid not in active_pids:
            raise WindowsJobError(
                "Windows Job Object does not contain its exact supervisor"
            )
        current_process = int(api.kernel32.GetCurrentProcess())
        if api.process_marker(current_process) != receipt.child_start_marker:
            raise WindowsJobError(
                "Windows runner FILETIME does not match its prepared child receipt"
            )
        if receipt.child_pid not in active_pids:
            raise WindowsJobError(
                "Windows Job Object does not contain its exact prepared runner"
            )
        if not api.process_is_in_job(current_process, job_handle):
            raise WindowsJobError(
                "Windows runner process is not a member of its inherited Job Object"
            )
    finally:
        api.close(job_handle)
        api.close(supervisor_handle)
