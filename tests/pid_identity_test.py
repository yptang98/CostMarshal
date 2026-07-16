from __future__ import annotations

import os
import platform
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from costmarshal_v2.session_backend import (  # noqa: E402
    LocalProcessBackend,
    _windows_tasklist_contains_pid,
    pid_identity_matches,
    pid_is_alive,
    pid_start_marker,
    select_backend_kind,
)


class PidIdentityTest(unittest.TestCase):
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
                self.assertFalse(
                    backend.actor_alive(
                        session_name="test",
                        actor_name="orphan-child",
                        target=f"pid:{command_leader_pid}",
                        pid=command_leader_pid,
                        process_start_marker=forged_marker,
                    )
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


if __name__ == "__main__":
    unittest.main(verbosity=2)
