#!/usr/bin/env python3
"""Contract test for v2's portable local-process backend."""

from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from costmarshal_v2.session_backend import pid_is_alive  # noqa: E402


ROOT = Path(__file__).resolve().parents[1]
CLI = ROOT / "scripts" / "costmarshal.py"


def run(temp: Path, *args: str, expect_ok: bool = True) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env["COSTMARSHAL_V2_HOME"] = str(temp / "runtime")
    result = subprocess.run(
        [sys.executable, str(CLI), "--root", str(temp / "runtime"), *args],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
        check=False,
    )
    if expect_ok and result.returncode != 0:
        raise AssertionError(
            f"Command failed: {' '.join(args)}\nSTDOUT:\n{result.stdout}\nSTDERR:\n{result.stderr}"
        )
    if not expect_ok and result.returncode == 0:
        raise AssertionError(f"Command unexpectedly succeeded: {' '.join(args)}\nSTDOUT:\n{result.stdout}")
    return result


def run_json(temp: Path, *args: str) -> dict:
    return json.loads(run(temp, *args).stdout)


def assert_true(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def pid_alive(pid: int | None) -> bool:
    if not pid:
        return False
    return pid_is_alive(pid)


def kill_pid(pid: int | None) -> None:
    if not pid or not pid_alive(pid):
        return
    if os.name == "nt":
        subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)
    else:
        os.kill(pid, signal.SIGTERM)


def main() -> int:
    temp = Path(tempfile.mkdtemp(prefix="costmarshal-v2-local-backend-"))
    pid: int | None = None
    try:
        init = run_json(
            temp,
            "init",
            "--name",
            "local-backend-contract",
            "--objective",
            "Verify portable local backend contract",
            "--session-name",
            "cmv2-local",
            "--backend",
            "local",
        )
        project = Path(init["project"])
        command = f'"{sys.executable}" -c "import time; time.sleep(30)"'
        leader = run_json(temp, "start-leader", "--project", str(project), "--command", command)
        pid = int(leader["pid"])
        assert_true(leader["backend"] == "local", "start-leader should report the local backend")
        assert_true(pid_alive(pid), "local backend should start a background process")
        status = run_json(temp, "status", "--project", str(project), "--format", "json")
        actor = next(item for item in status["actors"] if item["id"] == "leader")
        assert_true(actor["runtime_backend"] == "local", "status should expose local runtime backend")
        assert_true(actor["runtime_pid"] == pid, "status should expose local runtime pid")
        recovery = run_json(temp, "recover", "--project", str(project))
        assert_true(recovery["status"] == "ok", "recover should accept a live local backend pid")
        stopped = run_json(temp, "stop-actor", "--project", str(project), "--actor", "leader", "--stop-runtime", "--reason", "contract done")
        assert_true(stopped["actor_status"] == "stopped", "stop-actor should stop a local runtime")
        time.sleep(0.5)
        assert_true(not pid_alive(pid), "local backend process should be gone after stop-runtime")
        print(json.dumps({"status": "ok", "temporary_state": "cleaned"}, indent=2))
        return 0
    finally:
        kill_pid(pid)
        resolved = temp.resolve()
        temp_root = Path(tempfile.gettempdir()).resolve()
        if resolved == temp_root or temp_root not in resolved.parents:
            raise RuntimeError(f"Refusing to delete unexpected path: {resolved}")
        shutil.rmtree(resolved)


if __name__ == "__main__":
    raise SystemExit(main())
