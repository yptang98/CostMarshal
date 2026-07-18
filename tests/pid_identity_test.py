from __future__ import annotations

import os
import platform
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import costmarshal_v2.session_backend as session_backend_module  # noqa: E402
import costmarshal_v2.actor_runner as actor_runner_module  # noqa: E402
from costmarshal_v2.session_backend import (  # noqa: E402
    LocalProcessBackend,
    _bounded_readline,
    _windows_tasklist_contains_pid,
    pid_identity_matches,
    pid_is_alive,
    pid_start_marker,
    select_backend_kind,
)
from costmarshal_v2.windows_job import (  # noqa: E402
    WindowsJobApi,
    WindowsJobReceipt,
    stop_windows_job_runtime,
    validate_windows_job_receipt,
)


class PidIdentityTest(unittest.TestCase):
    def test_linux_group_inspection_error_is_not_treated_as_empty(self) -> None:
        with patch.object(
            session_backend_module.Path,
            "is_dir",
            return_value=True,
        ), patch.object(
            session_backend_module.Path,
            "read_text",
            return_value="test-boot-id",
        ), patch.object(
            session_backend_module.Path,
            "glob",
            side_effect=OSError("procfs enumeration denied"),
        ):
            with self.assertRaisesRegex(
                RuntimeError,
                "Linux process-group inspection failed",
            ):
                session_backend_module._linux_process_group_members(
                    11,
                    session_id=11,
                )

    def test_actor_liveness_fails_closed_without_procfs(self) -> None:
        backend = LocalProcessBackend()
        with patch.object(
            session_backend_module.os,
            "name",
            "posix",
        ), patch.object(session_backend_module, "Path") as path_class:
            path_class.return_value.is_dir.return_value = False
            with self.assertRaisesRegex(
                RuntimeError,
                "Linux process identity inspection unavailable",
            ):
                backend.actor_alive(
                    session_name="test",
                    actor_name="actor",
                    target="pid:11",
                    pid=11,
                    process_start_marker="linux-proc:test-boot-id:1",
                )

    def test_anchored_group_rechecks_stale_nonempty_teardown_snapshot(self) -> None:
        snapshots = iter(({22: "child-marker"}, {}))
        with patch.object(
            session_backend_module,
            "_linux_process_group_members",
            side_effect=lambda *args, **kwargs: dict(next(snapshots)),
        ), patch.object(
            session_backend_module,
            "_linux_member_identity_matches",
            return_value=False,
        ), patch.object(session_backend_module.time, "sleep"):
            self.assertEqual(
                session_backend_module._anchored_group_members(
                    anchor_pid=11,
                    anchor_marker="anchor-marker",
                    process_group=11,
                    session_id=11,
                ),
                {},
            )

    def test_anchored_group_retries_transient_anchor_identity_read(self) -> None:
        with patch.object(
            session_backend_module,
            "_linux_process_group_members",
            return_value={22: "child-marker"},
        ), patch.object(
            session_backend_module,
            "_linux_member_identity_matches",
            side_effect=(False, True),
        ), patch.object(session_backend_module.time, "sleep"):
            self.assertEqual(
                session_backend_module._anchored_group_members(
                    anchor_pid=11,
                    anchor_marker="anchor-marker",
                    process_group=11,
                    session_id=11,
                ),
                {11: "anchor-marker", 22: "child-marker"},
            )

    def test_anchored_group_still_rejects_confirmed_live_unanchored_members(self) -> None:
        with patch.object(
            session_backend_module,
            "_linux_process_group_members",
            return_value={22: "unbound-marker"},
        ) as snapshots, patch.object(
            session_backend_module,
            "_linux_member_identity_matches",
            return_value=False,
        ), patch.object(session_backend_module.time, "sleep"):
            with self.assertRaisesRegex(RuntimeError, "supervisor identity disappeared"):
                session_backend_module._anchored_group_members(
                    anchor_pid=11,
                    anchor_marker="anchor-marker",
                    process_group=11,
                    session_id=11,
                )
        self.assertEqual(snapshots.call_count, 3)

    def test_windows_job_receipt_read_is_bounded(self) -> None:
        release = threading.Event()

        class BlockingStream:
            def readline(self) -> str:
                release.wait(5)
                return ""

        started = time.monotonic()
        try:
            with self.assertRaisesRegex(RuntimeError, "timed out waiting for prepared receipt"):
                _bounded_readline(
                    BlockingStream(),
                    label="prepared receipt",
                    timeout_seconds=0.05,
                )
        finally:
            release.set()
        self.assertLess(time.monotonic() - started, 1.0)

    def test_windows_tasklist_pid_match_is_exact(self) -> None:
        output = '\n'.join(
            [
                '"python.exe","12345","Console","1","10,000 K"',
                '"python.exe","9123","Console","1","10,000 K"',
            ]
        )
        self.assertTrue(_windows_tasklist_contains_pid(output, 12345))
        self.assertFalse(_windows_tasklist_contains_pid(output, 123))

    def test_local_backend_rejects_platforms_without_process_identity(self) -> None:
        with patch("costmarshal_v2.session_backend.os.name", "posix"), patch(
            "costmarshal_v2.session_backend.platform.system", return_value="Darwin"
        ):
            with self.assertRaisesRegex(SystemExit, "Windows and Linux"):
                select_backend_kind("local")

    def test_local_stop_requires_matching_os_process_identity(self) -> None:
        process = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(60)"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=os.name != "nt",
        )
        try:
            marker = None
            deadline = time.monotonic() + 5
            while marker is None and time.monotonic() < deadline:
                marker = pid_start_marker(process.pid)
                time.sleep(0.01)
            self.assertIsNotNone(marker)
            self.assertTrue(pid_identity_matches(process.pid, marker))
            backend = LocalProcessBackend()
            if os.name == "nt":
                with self.assertRaisesRegex(RuntimeError, "complete handle-bound Job Object receipt"):
                    backend.actor_alive(
                        session_name="test",
                        actor_name="actor",
                        target=f"pid:{process.pid}",
                        pid=process.pid,
                        process_start_marker=marker,
                    )
                with self.assertRaisesRegex(RuntimeError, "complete handle-bound Job Object receipt"):
                    backend.stop_actor(
                        target=f"pid:{process.pid}",
                        pid=process.pid,
                        process_start_marker=marker,
                    )
                self.assertTrue(pid_is_alive(process.pid))
                process.terminate()
                process.wait(timeout=10)
                return
            self.assertFalse(
                backend.actor_alive(
                    session_name="test",
                    actor_name="actor",
                    target=f"pid:{process.pid}",
                    pid=process.pid,
                    process_start_marker=None,
                )
            )
            self.assertTrue(
                backend.actor_alive(
                    session_name="test",
                    actor_name="actor",
                    target=f"pid:{process.pid}",
                    pid=process.pid,
                    process_start_marker=marker,
                )
            )
            with self.assertRaisesRegex(RuntimeError, "identity changed"):
                backend.stop_actor(
                    target=f"pid:{process.pid}",
                    pid=process.pid,
                    process_start_marker=f"{marker}-forged",
                )
            self.assertTrue(pid_is_alive(process.pid))
            if os.name != "nt":
                self.assertEqual(
                    backend.stop_plan(target=f"pid:{process.pid}", pid=process.pid),
                    [["local-process", "verified-stop", "--pid", str(process.pid)]],
                )
            backend.stop_actor(
                target=f"pid:{process.pid}",
                pid=process.pid,
                process_start_marker=marker,
            )
            process.wait(timeout=10)
        finally:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=10)

    @unittest.skipUnless(os.name == "nt", "named Job Objects require Windows")
    def test_windows_stop_uses_bound_handles_when_supervisor_pid_was_reused(self) -> None:
        receipt = validate_windows_job_receipt(
            supervisor_pid=321,
            supervisor_start_marker="windows-filetime:123456",
            job_name="Local\\CostMarshal-" + "a" * 64,
            job_identity="windows-job-v1:" + "a" * 64,
            child_pid=654,
            child_start_marker="windows-filetime:654321",
        )

        class FakeApi:
            def __init__(self) -> None:
                self.job_terminated = False
                self.process_termination_called = False
                self.closed: list[object] = []
                self.opened_processes: list[tuple[int, str, bool]] = []

            def open_process_exact(self, pid: int, marker: str, *, terminate: bool):
                self.opened_processes.append((pid, marker, terminate))
                return None, "identity_changed"

            def open_job(self, name: str, *, terminate: bool):
                self.opened_job = (name, terminate)
                return "job-handle"

            def query_limits(self, handle: object) -> int:
                return 0

            def query_active_pids(self, handle: object):
                return () if self.job_terminated else (654,)

            def terminate_job(self, handle: object) -> None:
                self.job_terminated = True

            def terminate_process_handle(self, handle: object) -> None:
                self.process_termination_called = True

            def wait_process(self, handle: object, timeout_ms: int) -> bool:
                return True

            def close(self, handle: object) -> None:
                if handle is not None:
                    self.closed.append(handle)

        api = FakeApi()
        with self.assertRaisesRegex(
            RuntimeError, "not kernel-bound to its exact supervisor"
        ):
            stop_windows_job_runtime(receipt, api=api)
        self.assertEqual(
            api.opened_processes,
            [
                (321, "windows-filetime:123456", True),
                (654, "windows-filetime:654321", True),
            ],
        )
        self.assertEqual(api.opened_job, (receipt.job_name, True))
        self.assertFalse(api.job_terminated)
        self.assertFalse(api.process_termination_called)
        self.assertIn("job-handle", api.closed)

    @unittest.skipUnless(os.name == "nt", "named Job Objects require Windows")
    def test_windows_job_tracks_child_after_command_leader_exit_and_stops_tree(self) -> None:
        backend = LocalProcessBackend()
        launch: dict[str, object] | None = None
        child_pid: int | None = None
        with tempfile.TemporaryDirectory(prefix="costmarshal-windows-job-child-") as raw:
            temp = Path(raw)
            child_file = temp / "child.pid"
            leader_code = (
                "import pathlib,subprocess,sys; "
                "child=subprocess.Popen([sys.executable,'-c','import time; time.sleep(60)'],"
                "creationflags=subprocess.CREATE_NO_WINDOW); "
                "pathlib.Path(sys.argv[1]).write_text(str(child.pid),encoding='ascii')"
            )
            try:
                launch = backend.start_actor(
                    session_name="test",
                    actor_name="windows-orphan-child",
                    command=[sys.executable, "-c", leader_code, str(child_file)],
                    cwd=temp,
                    log_path=temp / "actor.log",
                )
                deadline = time.monotonic() + 10
                while (
                    (not child_file.is_file() or child_file.stat().st_size == 0)
                    and time.monotonic() < deadline
                ):
                    time.sleep(0.02)
                self.assertTrue(
                    child_file.is_file() and child_file.stat().st_size > 0,
                    "command leader did not publish child PID",
                )
                child_pid = int(child_file.read_text(encoding="ascii"))
                while (
                    pid_is_alive(int(launch["windows_job_child_pid"]))
                    and time.monotonic() < deadline
                ):
                    time.sleep(0.02)
                self.assertFalse(
                    pid_is_alive(int(launch["windows_job_child_pid"])),
                    "command leader remained live",
                )
                self.assertTrue(pid_is_alive(child_pid), "child should outlive its command leader")
                kwargs = {
                    key: launch.get(key)
                    for key in (
                        "windows_job_name",
                        "windows_job_identity",
                        "windows_job_child_pid",
                        "windows_job_child_start_marker",
                    )
                }
                self.assertTrue(
                    backend.actor_alive(
                        session_name="test",
                        actor_name="windows-orphan-child",
                        target=str(launch["target"]),
                        pid=int(launch["pid"]),
                        process_start_marker=str(launch["process_start_marker"]),
                        **kwargs,
                    )
                )
                backend.stop_actor(
                    target=str(launch["target"]),
                    pid=int(launch["pid"]),
                    process_start_marker=str(launch["process_start_marker"]),
                    **kwargs,
                )
                deadline = time.monotonic() + 10
                while pid_is_alive(child_pid) and time.monotonic() < deadline:
                    time.sleep(0.02)
                self.assertFalse(pid_is_alive(child_pid), "Job Object child survived STOP")
                launch = None
            finally:
                if launch is not None:
                    kwargs = {
                        key: launch.get(key)
                        for key in (
                            "windows_job_name",
                            "windows_job_identity",
                            "windows_job_child_pid",
                            "windows_job_child_start_marker",
                        )
                    }
                    try:
                        backend.stop_actor(
                            target=str(launch["target"]),
                            pid=int(launch["pid"]),
                            process_start_marker=str(launch["process_start_marker"]),
                            **kwargs,
                        )
                    except RuntimeError:
                        pass

    @unittest.skipUnless(os.name == "nt", "named Job Objects require Windows")
    def test_windows_job_rejects_breakaway_child(self) -> None:
        backend = LocalProcessBackend()
        launch: dict[str, object] | None = None
        with tempfile.TemporaryDirectory(prefix="costmarshal-windows-job-breakaway-") as raw:
            temp = Path(raw)
            result_file = temp / "breakaway.txt"
            code = "\n".join(
                [
                    "import pathlib, subprocess, sys, time",
                    "result = pathlib.Path(sys.argv[1])",
                    "try:",
                    "    subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'], creationflags=subprocess.CREATE_BREAKAWAY_FROM_JOB)",
                    "except OSError as exc:",
                    "    result.write_text(f'rejected:{exc.winerror}', encoding='ascii')",
                    "else:",
                    "    result.write_text('escaped', encoding='ascii')",
                    "time.sleep(60)",
                ]
            )
            try:
                launch = backend.start_actor(
                    session_name="test",
                    actor_name="windows-breakaway",
                    command=[sys.executable, "-c", code, str(result_file)],
                    cwd=temp,
                    log_path=temp / "actor.log",
                )
                deadline = time.monotonic() + 10
                while (
                    (not result_file.is_file() or result_file.stat().st_size == 0)
                    and time.monotonic() < deadline
                ):
                    time.sleep(0.02)
                self.assertTrue(
                    result_file.is_file() and result_file.stat().st_size > 0,
                    "breakaway probe did not finish",
                )
                self.assertRegex(result_file.read_text(encoding="ascii"), r"rejected:[0-9]+")
                self.assertNotEqual(result_file.read_text(encoding="ascii"), "escaped")
            finally:
                if launch is not None:
                    kwargs = {
                        key: launch.get(key)
                        for key in (
                            "windows_job_name",
                            "windows_job_identity",
                            "windows_job_child_pid",
                            "windows_job_child_start_marker",
                        )
                    }
                    backend.stop_actor(
                        target=str(launch["target"]),
                        pid=int(launch["pid"]),
                        process_start_marker=str(launch["process_start_marker"]),
                        **kwargs,
                    )

    @unittest.skipUnless(os.name == "nt", "named Job Objects require Windows")
    def test_windows_supervisor_crash_closes_job_and_kills_bound_children(self) -> None:
        backend = LocalProcessBackend()
        launch: dict[str, object] | None = None
        with tempfile.TemporaryDirectory(prefix="costmarshal-windows-job-reopen-") as raw:
            temp = Path(raw)
            try:
                launch = backend.start_actor(
                    session_name="test",
                    actor_name="windows-reopen",
                    command=[sys.executable, "-c", "import time; time.sleep(60)"],
                    cwd=temp,
                    log_path=temp / "actor.log",
                )
                api = WindowsJobApi()
                supervisor_handle, state = api.open_process_exact(
                    int(launch["pid"]),
                    str(launch["process_start_marker"]),
                    terminate=True,
                )
                self.assertEqual(state, "alive")
                self.assertIsNotNone(supervisor_handle)
                assert supervisor_handle is not None
                try:
                    api.terminate_process_handle(supervisor_handle)
                    self.assertTrue(api.wait_process(supervisor_handle, 5000))
                finally:
                    api.close(supervisor_handle)
                kwargs = {
                    key: launch.get(key)
                    for key in (
                        "windows_job_name",
                        "windows_job_identity",
                        "windows_job_child_pid",
                        "windows_job_child_start_marker",
                    )
                }
                deadline = time.monotonic() + 10
                while (
                    pid_is_alive(int(launch["windows_job_child_pid"]))
                    and time.monotonic() < deadline
                ):
                    time.sleep(0.02)
                self.assertFalse(
                    pid_is_alive(int(launch["windows_job_child_pid"])),
                    "kill-on-job-close did not contain a supervisor crash",
                )
                self.assertFalse(
                    backend.actor_alive(
                        session_name="test",
                        actor_name="windows-reopen",
                        target=str(launch["target"]),
                        pid=int(launch["pid"]),
                        process_start_marker=str(launch["process_start_marker"]),
                        **kwargs,
                    ),
                    "closed Job Object should be absent after crash containment",
                )
                result = backend.stop_actor(
                    target=str(launch["target"]),
                    pid=int(launch["pid"]),
                    process_start_marker=str(launch["process_start_marker"]),
                    **kwargs,
                )
                self.assertEqual(result["source"], "windows_job_already_absent")
                self.assertIn(result["supervisor_state"], {"absent", "exited"})
                launch = None
            finally:
                if launch is not None:
                    kwargs = {
                        key: launch.get(key)
                        for key in (
                            "windows_job_name",
                            "windows_job_identity",
                            "windows_job_child_pid",
                            "windows_job_child_start_marker",
                        )
                    }
                    backend.stop_actor(
                        target=str(launch["target"]),
                        pid=int(launch["pid"]),
                        process_start_marker=str(launch["process_start_marker"]),
                        **kwargs,
                    )

    @unittest.skipUnless(os.name == "nt", "named Job Objects require Windows")
    def test_windows_natural_completion_releases_supervisor_and_job(self) -> None:
        backend = LocalProcessBackend()
        with tempfile.TemporaryDirectory(prefix="costmarshal-windows-job-natural-") as raw:
            temp = Path(raw)
            launch = backend.start_actor(
                session_name="test",
                actor_name="windows-natural",
                command=[sys.executable, "-c", "raise SystemExit(0)"],
                cwd=temp,
                log_path=temp / "actor.log",
            )
            kwargs = {
                key: launch.get(key)
                for key in (
                    "windows_job_name",
                    "windows_job_identity",
                    "windows_job_child_pid",
                    "windows_job_child_start_marker",
                )
            }
            deadline = time.monotonic() + 10
            while pid_is_alive(int(launch["pid"])) and time.monotonic() < deadline:
                time.sleep(0.02)
            self.assertFalse(pid_is_alive(int(launch["pid"])))
            self.assertFalse(
                backend.actor_alive(
                    session_name="test",
                    actor_name="windows-natural",
                    target=str(launch["target"]),
                    pid=int(launch["pid"]),
                    process_start_marker=str(launch["process_start_marker"]),
                    **kwargs,
                ),
                "natural completion left a named Job Object behind",
            )

    @unittest.skipUnless(os.name == "nt", "named Job Objects require Windows")
    def test_windows_prepared_callback_failure_contains_suspended_child(self) -> None:
        backend = LocalProcessBackend()
        prepared: dict[str, object] = {}
        with tempfile.TemporaryDirectory(prefix="costmarshal-windows-job-callback-") as raw:
            temp = Path(raw)

            def reject(receipt: dict[str, object]) -> None:
                prepared.update(receipt)
                raise RuntimeError("simulated durable receipt failure")

            with self.assertRaisesRegex(RuntimeError, "simulated durable receipt failure"):
                backend.start_actor(
                    session_name="test",
                    actor_name="windows-callback",
                    command=[sys.executable, "-c", "import time; time.sleep(60)"],
                    cwd=temp,
                    log_path=temp / "actor.log",
                    prepared_callback=reject,
                )
            self.assertTrue(prepared.get("windows_job_identity"))
            kwargs = {
                key: prepared.get(key)
                for key in (
                    "windows_job_name",
                    "windows_job_identity",
                    "windows_job_child_pid",
                    "windows_job_child_start_marker",
                )
            }
            self.assertFalse(
                backend.actor_alive(
                    session_name="test",
                    actor_name="windows-callback",
                    target=str(prepared["target"]),
                    pid=int(prepared["pid"]),
                    process_start_marker=str(prepared["process_start_marker"]),
                    **kwargs,
                )
            )
            self.assertFalse(pid_is_alive(int(prepared["windows_job_child_pid"])))

    @unittest.skipUnless(os.name == "nt", "named Job Objects require Windows")
    def test_windows_runner_rejects_shape_valid_forged_job_environment(self) -> None:
        backend = LocalProcessBackend()
        launch: dict[str, object] | None = None
        with tempfile.TemporaryDirectory(prefix="costmarshal-windows-job-forged-") as raw:
            temp = Path(raw)
            try:
                launch = backend.start_actor(
                    session_name="test",
                    actor_name="windows-forged",
                    command=[sys.executable, "-c", "import time; time.sleep(60)"],
                    cwd=temp,
                    log_path=temp / "actor.log",
                )
                environment = {
                    "COSTMARSHAL_WINDOWS_JOB_NAME": str(launch["windows_job_name"]),
                    "COSTMARSHAL_WINDOWS_JOB_IDENTITY": str(launch["windows_job_identity"]),
                    "COSTMARSHAL_WINDOWS_JOB_SUPERVISOR_PID": str(launch["pid"]),
                    "COSTMARSHAL_WINDOWS_JOB_SUPERVISOR_MARKER": str(
                        launch["process_start_marker"]
                    ),
                }
                with patch.dict(os.environ, environment, clear=False):
                    with self.assertRaisesRegex(
                        SystemExit, "not a member of its inherited Job Object"
                    ):
                        actor_runner_module._inherited_windows_job_runtime()
            finally:
                if launch is not None:
                    kwargs = {
                        key: launch.get(key)
                        for key in (
                            "windows_job_name",
                            "windows_job_identity",
                            "windows_job_child_pid",
                            "windows_job_child_start_marker",
                        )
                    }
                    backend.stop_actor(
                        target=str(launch["target"]),
                        pid=int(launch["pid"]),
                        process_start_marker=str(launch["process_start_marker"]),
                        **kwargs,
                    )

    @unittest.skipUnless(os.name == "nt", "named Job Objects require Windows")
    def test_windows_swapped_supervisor_and_job_receipts_fail_closed(self) -> None:
        backend = LocalProcessBackend()
        launches: list[dict[str, object]] = []
        with tempfile.TemporaryDirectory(prefix="costmarshal-windows-job-swap-") as raw:
            temp = Path(raw)
            try:
                for suffix in ("a", "b"):
                    launches.append(
                        backend.start_actor(
                            session_name="test",
                            actor_name=f"windows-swap-{suffix}",
                            command=[sys.executable, "-c", "import time; time.sleep(60)"],
                            cwd=temp,
                            log_path=temp / f"actor-{suffix}.log",
                        )
                    )
                first, second = launches
                with self.assertRaisesRegex(
                    RuntimeError, "not kernel-bound to its exact supervisor"
                ):
                    backend.actor_alive(
                        session_name="test",
                        actor_name="windows-swap",
                        target=str(second["target"]),
                        pid=int(first["pid"]),
                        process_start_marker=str(first["process_start_marker"]),
                        windows_job_name=str(second["windows_job_name"]),
                        windows_job_identity=str(second["windows_job_identity"]),
                        windows_job_child_pid=int(second["windows_job_child_pid"]),
                        windows_job_child_start_marker=str(
                            second["windows_job_child_start_marker"]
                        ),
                    )
                self.assertTrue(pid_is_alive(int(first["pid"])))
                self.assertTrue(pid_is_alive(int(second["pid"])))
            finally:
                for launch in launches:
                    kwargs = {
                        key: launch.get(key)
                        for key in (
                            "windows_job_name",
                            "windows_job_identity",
                            "windows_job_child_pid",
                            "windows_job_child_start_marker",
                        )
                    }
                    backend.stop_actor(
                        target=str(launch["target"]),
                        pid=int(launch["pid"]),
                        process_start_marker=str(launch["process_start_marker"]),
                        **kwargs,
                    )

    @unittest.skipUnless(
        os.name != "nt"
        and platform.system() == "Linux"
        and Path("/proc").is_dir()
        and hasattr(os, "memfd_create")
        and hasattr(os, "pidfd_open")
        and hasattr(signal, "pidfd_send_signal"),
        "durable process-group identity requires Linux procfs, memfd, and pidfd",
    )
    def test_local_backend_tracks_and_stops_child_after_group_leader_exit(self) -> None:
        backend = LocalProcessBackend()
        child_pid: int | None = None
        child_marker: str | None = None
        command_leader_marker: str | None = None
        supervisor_pid: int | None = None
        supervisor_marker: str | None = None
        with tempfile.TemporaryDirectory(prefix="costmarshal-local-orphan-") as raw:
            temp = Path(raw)
            child_file = temp / "child.pid"
            release_file = temp / "release"
            leader_code = (
                "import os,pathlib,subprocess,sys,time; "
                "child=subprocess.Popen([sys.executable,'-c','import time; time.sleep(60)']); "
                "pathlib.Path(sys.argv[1]).write_text(f'{os.getpid()}:{child.pid}',encoding='ascii'); "
                "release=pathlib.Path(sys.argv[2]); "
                "\nwhile not release.exists(): time.sleep(0.01)\n"
                "os._exit(0)"
            )
            launch = backend.start_actor(
                session_name="test",
                actor_name="orphan-child",
                command=[
                    sys.executable,
                    "-c",
                    leader_code,
                    str(child_file),
                    str(release_file),
                ],
                cwd=temp,
                log_path=temp / "actor.log",
            )
            supervisor_pid = int(launch["pid"])
            try:
                deadline = time.monotonic() + 10
                while not child_file.is_file() and time.monotonic() < deadline:
                    time.sleep(0.01)
                self.assertTrue(child_file.is_file(), "leader did not publish its child pid")
                command_leader_text, child_text = child_file.read_text(encoding="ascii").split(":", 1)
                command_leader_pid = int(command_leader_text)
                child_pid = int(child_text)
                command_leader_marker = pid_start_marker(command_leader_pid)
                self.assertIsNotNone(command_leader_marker)
                assert command_leader_marker is not None
                self.assertTrue(command_leader_marker.startswith("linux-proc-v2:"))
                supervisor_marker = pid_start_marker(supervisor_pid)
                self.assertIsNotNone(supervisor_marker)
                assert supervisor_marker is not None
                self.assertTrue(supervisor_marker.startswith("linux-proc-v2:"), supervisor_marker)
                child_marker = pid_start_marker(child_pid)
                self.assertIsNotNone(child_marker)
                self.assertEqual(os.getpgid(child_pid), supervisor_pid)
                self.assertEqual(os.getsid(child_pid), supervisor_pid)

                release_file.write_text("exit", encoding="ascii")
                deadline = time.monotonic() + 10
                while pid_is_alive(command_leader_pid) and time.monotonic() < deadline:
                    time.sleep(0.01)
                self.assertFalse(pid_is_alive(command_leader_pid), "command leader remained executable")
                self.assertTrue(pid_is_alive(supervisor_pid), "durable supervisor exited before its child")
                self.assertTrue(pid_is_alive(child_pid), "child should outlive its command leader")
                self.assertTrue(
                    backend.actor_alive(
                        session_name="test",
                        actor_name="orphan-child",
                        target=f"pid:{command_leader_pid}",
                        pid=command_leader_pid,
                        process_start_marker=command_leader_marker,
                    )
                )

                forged_marker = command_leader_marker[:-1] + (
                    "0" if command_leader_marker[-1] != "0" else "1"
                )
                with self.assertRaisesRegex(RuntimeError, "identity changed"):
                    backend.actor_alive(
                        session_name="test",
                        actor_name="orphan-child",
                        target=f"pid:{command_leader_pid}",
                        pid=command_leader_pid,
                        process_start_marker=forged_marker,
                    )
                with self.assertRaisesRegex(RuntimeError, "identity changed"):
                    backend.stop_actor(
                        target=f"pid:{command_leader_pid}",
                        pid=command_leader_pid,
                        process_start_marker=forged_marker,
                    )
                self.assertTrue(pid_is_alive(child_pid))

                backend.stop_actor(
                    target=f"pid:{command_leader_pid}",
                    pid=command_leader_pid,
                    process_start_marker=command_leader_marker,
                )
                deadline = time.monotonic() + 10
                while (
                    (pid_is_alive(child_pid) or pid_is_alive(supervisor_pid))
                    and time.monotonic() < deadline
                ):
                    time.sleep(0.01)
                self.assertFalse(pid_is_alive(child_pid), "verified orphan child survived STOP")
                self.assertFalse(pid_is_alive(supervisor_pid), "verified supervisor survived STOP")
            finally:
                if (
                    supervisor_pid is not None
                    and supervisor_marker is not None
                    and backend.actor_alive(
                        session_name="test",
                        actor_name="orphan-child",
                        target=f"pid:{supervisor_pid}",
                        pid=supervisor_pid,
                        process_start_marker=supervisor_marker,
                    )
                ):
                    backend.stop_actor(
                        target=f"pid:{supervisor_pid}",
                        pid=supervisor_pid,
                        process_start_marker=supervisor_marker,
                    )
                if (
                    child_pid is not None
                    and child_marker is not None
                    and pid_identity_matches(child_pid, child_marker)
                ):
                    os.kill(child_pid, signal.SIGKILL)

    @unittest.skipUnless(
        os.name != "nt"
        and platform.system() == "Linux"
        and Path("/proc").is_dir()
        and hasattr(os, "memfd_create")
        and hasattr(os, "pidfd_open")
        and hasattr(signal, "pidfd_send_signal"),
        "durable process-group identity requires Linux procfs, memfd, and pidfd",
    )
    def test_local_stop_keeps_supervisor_anchor_while_term_handler_spawns_child(self) -> None:
        backend = LocalProcessBackend()
        supervisor_pid: int | None = None
        supervisor_marker: str | None = None
        leader_pid: int | None = None
        leader_marker: str | None = None
        child_pid: int | None = None
        child_marker: str | None = None
        with tempfile.TemporaryDirectory(prefix="costmarshal-local-term-race-") as raw:
            temp = Path(raw)
            ready_file = temp / "leader.pid"
            child_file = temp / "spawned-child.pid"
            leader_code = "\n".join(
                [
                    "import os, pathlib, signal, subprocess, sys, time",
                    "ready = pathlib.Path(sys.argv[1])",
                    "spawned = pathlib.Path(sys.argv[2])",
                    "def on_term(signum, frame):",
                    "    signal.signal(signal.SIGTERM, signal.SIG_IGN)",
                    "    child = subprocess.Popen(",
                    "        [sys.executable, '-c', 'import time; time.sleep(60)'],",
                    "        stdin=subprocess.DEVNULL,",
                    "        stdout=subprocess.DEVNULL,",
                    "        stderr=subprocess.DEVNULL,",
                    "        close_fds=True,",
                    "    )",
                    "    spawned.write_text(str(child.pid), encoding='ascii')",
                    "    os._exit(0)",
                    "signal.signal(signal.SIGTERM, on_term)",
                    "ready.write_text(str(os.getpid()), encoding='ascii')",
                    "while True:",
                    "    time.sleep(1)",
                ]
            )
            launch = backend.start_actor(
                session_name="test",
                actor_name="term-race",
                command=[
                    sys.executable,
                    "-c",
                    leader_code,
                    str(ready_file),
                    str(child_file),
                ],
                cwd=temp,
                log_path=temp / "actor.log",
            )
            supervisor_pid = int(launch["pid"])
            original_signal_verified_members = session_backend_module._signal_verified_members
            coordinated = False

            def signal_with_spawn_barrier(
                members: dict[int, str],
                *,
                process_group: int,
                session_id: int,
                signal_number: int,
            ) -> list[list[str]]:
                nonlocal coordinated, child_pid, child_marker
                commands = original_signal_verified_members(
                    members,
                    process_group=process_group,
                    session_id=session_id,
                    signal_number=signal_number,
                )
                if (
                    not coordinated
                    and signal_number == signal.SIGTERM
                    and leader_pid is not None
                    and leader_pid in members
                ):
                    coordinated = True
                    deadline = time.monotonic() + 5
                    while not child_file.is_file() and time.monotonic() < deadline:
                        time.sleep(0.01)
                    self.assertTrue(child_file.is_file(), "TERM handler did not spawn its child")
                    child_pid = int(child_file.read_text(encoding="ascii"))
                    while child_marker is None and time.monotonic() < deadline:
                        child_marker = pid_start_marker(child_pid)
                        time.sleep(0.01)
                    self.assertIsNotNone(child_marker, "spawned child had no durable start marker")
                    while pid_is_alive(leader_pid) and time.monotonic() < deadline:
                        time.sleep(0.01)
                    self.assertFalse(pid_is_alive(leader_pid), "TERM handler leader did not exit")
                    if supervisor_pid in members:
                        while pid_is_alive(supervisor_pid) and time.monotonic() < deadline:
                            time.sleep(0.01)
                        self.assertFalse(
                            pid_is_alive(supervisor_pid),
                            "legacy all-member signaling did not release the supervisor anchor",
                        )
                return commands

            try:
                deadline = time.monotonic() + 10
                while not ready_file.is_file() and time.monotonic() < deadline:
                    time.sleep(0.01)
                self.assertTrue(ready_file.is_file(), "leader did not publish its pid")
                leader_pid = int(ready_file.read_text(encoding="ascii"))
                leader_marker = pid_start_marker(leader_pid)
                supervisor_marker = pid_start_marker(supervisor_pid)
                self.assertIsNotNone(leader_marker)
                self.assertIsNotNone(supervisor_marker)
                assert leader_marker is not None
                with patch.object(
                    session_backend_module,
                    "_signal_verified_members",
                    side_effect=signal_with_spawn_barrier,
                ):
                    backend.stop_actor(
                        target=f"pid:{leader_pid}",
                        pid=leader_pid,
                        process_start_marker=leader_marker,
                    )
                self.assertTrue(coordinated, "STOP did not exercise the TERM-handler spawn barrier")
                self.assertIsNotNone(child_pid)
                self.assertIsNotNone(child_marker)
                assert child_pid is not None
                assert child_marker is not None
                self.assertFalse(
                    pid_identity_matches(child_pid, child_marker),
                    "a child spawned during STOP survived the anchored sweep",
                )
                self.assertFalse(pid_is_alive(supervisor_pid), "verified supervisor survived STOP")
            finally:
                for candidate_pid, candidate_marker in (
                    (child_pid, child_marker),
                    (leader_pid, leader_marker),
                    (supervisor_pid, supervisor_marker),
                ):
                    if (
                        candidate_pid is not None
                        and candidate_marker is not None
                        and pid_identity_matches(candidate_pid, candidate_marker)
                    ):
                        os.kill(candidate_pid, signal.SIGKILL)


if __name__ == "__main__":
    unittest.main(verbosity=2)
