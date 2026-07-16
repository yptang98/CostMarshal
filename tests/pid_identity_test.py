from __future__ import annotations

import os
import subprocess
import sys
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
                    [["kill", "-TERM", "--", f"-{process.pid}"]],
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


if __name__ == "__main__":
    unittest.main(verbosity=2)
