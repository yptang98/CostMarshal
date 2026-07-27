#!/usr/bin/env python3
"""Run the complete local release suite and emit SHA-bound evidence."""

from __future__ import annotations

from datetime import datetime, timezone
import ctypes
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from run_release_gates import REQUIRED_LOCAL_TESTS  # noqa: E402


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def github_command_escape(value: object) -> str:
    return (
        str(value)
        .replace("%", "%25")
        .replace("\r", "%0D")
        .replace("\n", "%0A")
    )


def emit_github_failure_annotation(relative: str, output_tail: str) -> None:
    if os.environ.get("GITHUB_ACTIONS") != "true":
        return
    detail = output_tail.strip() or "test exited without diagnostic output"
    print(
        "::error file="
        + github_command_escape(relative)
        + "::"
        + github_command_escape(detail)
    )


def _create_windows_kill_job(process: subprocess.Popen[bytes]) -> int | None:
    if os.name != "nt":
        return None
    from ctypes import wintypes

    class BasicLimitInformation(ctypes.Structure):
        _fields_ = [
            ("PerProcessUserTimeLimit", ctypes.c_int64),
            ("PerJobUserTimeLimit", ctypes.c_int64),
            ("LimitFlags", wintypes.DWORD),
            ("MinimumWorkingSetSize", ctypes.c_size_t),
            ("MaximumWorkingSetSize", ctypes.c_size_t),
            ("ActiveProcessLimit", wintypes.DWORD),
            ("Affinity", ctypes.c_size_t),
            ("PriorityClass", wintypes.DWORD),
            ("SchedulingClass", wintypes.DWORD),
        ]

    class IoCounters(ctypes.Structure):
        _fields_ = [
            ("ReadOperationCount", ctypes.c_ulonglong),
            ("WriteOperationCount", ctypes.c_ulonglong),
            ("OtherOperationCount", ctypes.c_ulonglong),
            ("ReadTransferCount", ctypes.c_ulonglong),
            ("WriteTransferCount", ctypes.c_ulonglong),
            ("OtherTransferCount", ctypes.c_ulonglong),
        ]

    class ExtendedLimitInformation(ctypes.Structure):
        _fields_ = [
            ("BasicLimitInformation", BasicLimitInformation),
            ("IoInfo", IoCounters),
            ("ProcessMemoryLimit", ctypes.c_size_t),
            ("JobMemoryLimit", ctypes.c_size_t),
            ("PeakProcessMemoryUsed", ctypes.c_size_t),
            ("PeakJobMemoryUsed", ctypes.c_size_t),
        ]

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
    kernel32.CreateJobObjectW.restype = wintypes.HANDLE
    kernel32.SetInformationJobObject.argtypes = [
        wintypes.HANDLE,
        ctypes.c_int,
        ctypes.c_void_p,
        wintypes.DWORD,
    ]
    kernel32.SetInformationJobObject.restype = wintypes.BOOL
    kernel32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
    kernel32.AssignProcessToJobObject.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL
    job = kernel32.CreateJobObjectW(None, None)
    if not job:
        return None
    information = ExtendedLimitInformation()
    information.BasicLimitInformation.LimitFlags = 0x00002000
    configured = kernel32.SetInformationJobObject(
        job,
        9,
        ctypes.byref(information),
        ctypes.sizeof(information),
    )
    assigned = configured and kernel32.AssignProcessToJobObject(
        job,
        wintypes.HANDLE(int(process._handle)),
    )
    if not assigned:
        kernel32.CloseHandle(job)
        return None
    return int(job)


def _close_windows_job(job_handle: int | None) -> None:
    if os.name == "nt" and job_handle is not None:
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel32.CloseHandle.restype = wintypes.BOOL
        kernel32.CloseHandle(wintypes.HANDLE(job_handle))


def _terminate_process_tree(
    process: subprocess.Popen[bytes],
    *,
    job_handle: int | None,
) -> None:
    if os.name == "nt":
        if job_handle is not None:
            _close_windows_job(job_handle)
        elif process.poll() is None:
            try:
                subprocess.run(
                    [
                        "taskkill.exe",
                        "/PID",
                        str(process.pid),
                        "/T",
                        "/F",
                    ],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    timeout=10.0,
                    check=False,
                )
            except (OSError, subprocess.TimeoutExpired):
                pass
    else:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass
    if process.poll() is None:
        try:
            process.kill()
        except OSError:
            pass


def run_command(argv: list[str], *, timeout_seconds: float = 300.0) -> tuple[int, str, float]:
    started = time.monotonic()
    environment = os.environ.copy()
    environment.pop("PYTHONOPTIMIZE", None)
    popen_options: dict[str, object] = {}
    if os.name == "nt":
        popen_options["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        popen_options["start_new_session"] = True
    with tempfile.TemporaryFile(mode="w+b") as captured:
        process = subprocess.Popen(
            argv,
            cwd=ROOT,
            env=environment,
            stdout=captured,
            stderr=subprocess.STDOUT,
            **popen_options,
        )
        job_handle = _create_windows_kill_job(process)
        timed_out = False
        setup_error = os.name == "nt" and job_handle is None
        if setup_error:
            _terminate_process_tree(process, job_handle=job_handle)
            try:
                process.wait(timeout=5.0)
            except subprocess.TimeoutExpired:
                if process.poll() is None:
                    process.kill()
        else:
            try:
                process.wait(timeout=timeout_seconds)
            except subprocess.TimeoutExpired:
                timed_out = True
                _terminate_process_tree(process, job_handle=job_handle)
                job_handle = None
                try:
                    process.wait(timeout=5.0)
                except subprocess.TimeoutExpired:
                    if process.poll() is None:
                        process.kill()
            finally:
                if job_handle is not None:
                    _terminate_process_tree(process, job_handle=job_handle)
        captured.flush()
        captured.seek(0)
        output = captured.read().decode("utf-8", errors="replace")
    if setup_error:
        return (
            125,
            (output + "\nfailed to assign the test process to a kill-on-close job")[
                -4000:
            ],
            round(time.monotonic() - started, 3),
        )
    if timed_out:
        return (
            124,
            (output + "\ncommand timed out; process tree terminated")[-4000:],
            round(time.monotonic() - started, 3),
        )
    return (
        int(process.returncode or 0),
        output[-4000:],
        round(time.monotonic() - started, 3),
    )


def main(argv: list[str] | None = None) -> int:
    if sys.flags.optimize:
        raise SystemExit("release evidence refuses optimized Python; assertions must remain enabled")
    output = ROOT / "artifacts" / "local-test-report.json"
    if argv:
        if len(argv) != 1:
            raise SystemExit("usage: run_local_test_evidence.py [output.json]")
        output = Path(argv[0]).expanduser().resolve()

    git_sha = subprocess.check_output(
        ["git", "rev-parse", "HEAD"], cwd=ROOT, text=True, encoding="utf-8"
    ).strip()
    compile_code, compile_tail, compile_seconds = run_command(
        [sys.executable, "-m", "compileall", "-q", "costmarshal_v2", "tests", "scripts"]
    )
    results: list[dict[str, object]] = []
    if compile_code == 0:
        for relative in REQUIRED_LOCAL_TESTS:
            returncode, output_tail, duration_seconds = run_command(
                [sys.executable, str(ROOT / relative)],
                timeout_seconds=600.0,
            )
            results.append(
                {
                    "test": relative,
                    "returncode": returncode,
                    "duration_seconds": duration_seconds,
                    "output_tail": output_tail,
                }
            )
            if returncode != 0:
                emit_github_failure_annotation(relative, output_tail)
                break

    passed = sum(row["returncode"] == 0 for row in results)
    failed = int(compile_code != 0) + sum(row["returncode"] != 0 for row in results)
    complete = len(results) == len(REQUIRED_LOCAL_TESTS)
    status = "pass" if compile_code == 0 and complete and failed == 0 else "fail"
    report = {
        "schema_version": 1,
        "generated_at": utc_now(),
        "git_sha": git_sha,
        "status": status,
        "compileall_passed": compile_code == 0,
        "compileall_duration_seconds": compile_seconds,
        "compileall_output_tail": compile_tail,
        "tests": list(REQUIRED_LOCAL_TESTS),
        "passed": passed,
        "failed": failed,
        "results": results,
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_name(output.name + ".tmp")
    temporary.write_text(
        json.dumps(report, indent=2, sort_keys=True, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    temporary.replace(output)
    print(
        json.dumps(
            {
                "status": status,
                "git_sha": git_sha,
                "passed": passed,
                "failed": failed,
                "report": str(output),
            },
            sort_keys=True,
        )
    )
    return 0 if status == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
