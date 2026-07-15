#!/usr/bin/env python3
"""Scheduler isolation policy fails before attempt persistence or reservation."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
CLI = ROOT / "scripts" / "costmarshal.py"


def run(temp: Path, *args: str, ok: bool = True) -> subprocess.CompletedProcess[str]:
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
    if ok and result.returncode:
        raise AssertionError(f"command failed: {args}\n{result.stdout}\n{result.stderr}")
    if not ok and not result.returncode:
        raise AssertionError(f"command unexpectedly succeeded: {args}\n{result.stdout}")
    return result


def create_project(temp: Path, name: str, *, allow_unsafe: bool) -> Path:
    workspace = temp / f"workspace-{name}"
    workspace.mkdir()
    args = [
        "init",
        "--name",
        name,
        "--objective",
        "verify fail-closed worker isolation",
        "--workspace",
        str(workspace),
        "--backend",
        "local",
        "--governance",
        "off",
    ]
    if allow_unsafe:
        args.append("--allow-unsafe-native-workers")
    project = Path(json.loads(run(temp, *args).stdout)["project"])
    run(
        temp,
        "new-task",
        "--project",
        str(project),
        "--title",
        "isolated",
        "--purpose",
        "prove no weak fallback",
    )
    return project


def assert_no_attempt(project: Path) -> None:
    task = json.loads((project / "tasks" / "V2-0001" / "task.json").read_text(encoding="utf-8"))
    assert task.get("attempts") == []
    actors = list((project / "scheduler" / "actors").glob("agent-*.json"))
    assert actors == []


def main() -> int:
    temp = Path(tempfile.mkdtemp(prefix="costmarshal-v2-isolation-gate-"))
    try:
        required = create_project(temp, "required", allow_unsafe=False)
        rejected = run(temp, "dispatch", "--project", str(required), "--task", "V2-0001", ok=False)
        assert "native fallback is forbidden" in (rejected.stdout + rejected.stderr)
        assert_no_attempt(required)

        no_project_opt_in = run(
            temp,
            "dispatch",
            "--project",
            str(required),
            "--task",
            "V2-0001",
            "--unsafe-native",
            ok=False,
        )
        assert "project-level and dispatch-level explicit opt-in" in (
            no_project_opt_in.stdout + no_project_opt_in.stderr
        )
        assert_no_attempt(required)

        opted_in = create_project(temp, "unsafe-opted-in", allow_unsafe=True)
        still_required = run(temp, "dispatch", "--project", str(opted_in), "--task", "V2-0001", ok=False)
        assert "native fallback is forbidden" in (still_required.stdout + still_required.stderr)
        assert_no_attempt(opted_in)
        accepted = json.loads(
            run(
                temp,
                "dispatch",
                "--project",
                str(opted_in),
                "--task",
                "V2-0001",
                "--unsafe-native",
            ).stdout
        )
        actor = json.loads(
            (opted_in / "scheduler" / "actors" / f"{accepted['actor_id']}.json").read_text(encoding="utf-8")
        )
        assert actor["isolation"]["attestation"]["backend"] == "unsafe-native"
        assert actor["isolation"]["attestation"]["strong_isolation"] is False
        assert actor["isolation"]["project_opt_in"] is True
        assert actor["isolation"]["dispatch_opt_in"] is True
        print("scheduler isolation gate ok")
        return 0
    finally:
        shutil.rmtree(temp, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
