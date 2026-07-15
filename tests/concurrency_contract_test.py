#!/usr/bin/env python3
"""Process-level contract for atomic claim conflict checks."""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
CLI = ROOT / "scripts" / "costmarshal.py"
sys.path.insert(0, str(ROOT))

from costmarshal_v2.locking import project_write_lock  # noqa: E402
from costmarshal_v2.paths import resolve_project  # noqa: E402
from costmarshal_v2.scheduler import scheduler_cycle  # noqa: E402


def run_json(runtime: Path, *args: str) -> dict[str, object]:
    result = subprocess.run(
        [sys.executable, str(CLI), "--root", str(runtime), *args],
        cwd=runtime.parent,
        text=True,
        encoding="utf-8",
        errors="replace",
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=True,
    )
    return json.loads(result.stdout)


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="costmarshal-concurrency-") as raw:
        temp = Path(raw)
        runtime = temp / "runtime"
        workspace = temp / "workspace"
        workspace.mkdir()
        created = run_json(
            runtime,
            "init",
            "--name",
            "concurrency",
            "--objective",
            "Prove claim creation is serialized",
            "--workspace",
            str(workspace),
            "--backend",
            "local",
            "--governance",
            "off",
        )
        project = Path(str(created["project"]))
        layout = resolve_project(runtime, str(project))
        commands = []
        for task_id in ("V2-0101", "V2-0102"):
            commands.append(
                [
                    sys.executable,
                    str(CLI),
                    "--root",
                    str(runtime),
                    "new-task",
                    "--project",
                    str(project),
                    "--id",
                    task_id,
                    "--title",
                    task_id,
                    "--purpose",
                    "Compete for the same exact write claim",
                    "--claim-path",
                    "shared/output.txt",
                ]
            )
        processes: list[subprocess.Popen[str]] = []
        with project_write_lock(layout):
            for command in commands:
                processes.append(
                    subprocess.Popen(
                        command,
                        cwd=temp,
                        text=True,
                        encoding="utf-8",
                        errors="replace",
                        stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE,
                    )
                )
        completed = []
        for process in processes:
            stdout, stderr = process.communicate(timeout=30)
            completed.append((process.returncode, stdout, stderr))
        assert sorted(row[0] for row in completed) == [0, 1], completed
        failure_text = "\n".join(row[1] + row[2] for row in completed if row[0] != 0)
        assert "Path claim conflict" in failure_text, completed
        claims = json.loads((project / "locks" / "claims.json").read_text(encoding="utf-8"))["claims"]
        active = [row for row in claims if row.get("state") == "active"]
        assert len(active) == 1 and active[0]["path"] == "shared/output.txt", claims
        task_dirs = [path for path in (project / "tasks").iterdir() if path.is_dir()]
        assert len(task_dirs) == 1, task_dirs

        run_json(runtime, "migrate-state", "--project", str(project), "--apply")
        effect_started = threading.Event()
        release_effect = threading.Event()
        cycle_error: list[BaseException] = []

        def slow_effects(*args: object, **kwargs: object) -> dict[str, object]:
            effect_started.set()
            if not release_effect.wait(timeout=5):
                raise AssertionError("test did not release the slow effect")
            return {"status": "ok", "processed": [], "failed": [], "skipped": [], "dry_run": False}

        def run_cycle() -> None:
            try:
                scheduler_cycle(layout, command_limit=1)
            except BaseException as exc:  # noqa: BLE001 - thread assertion handoff
                cycle_error.append(exc)

        with patch("costmarshal_v2.scheduler.process_runtime_effects", slow_effects):
            thread = threading.Thread(target=run_cycle, daemon=True)
            thread.start()
            assert effect_started.wait(timeout=5)
            started = time.monotonic()
            with project_write_lock(layout, timeout_seconds=1):
                pass
            assert time.monotonic() - started < 0.9
            release_effect.set()
            thread.join(timeout=10)
        assert not thread.is_alive()
        assert cycle_error == [], cycle_error
    print("concurrency contract ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
