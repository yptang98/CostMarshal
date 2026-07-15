#!/usr/bin/env python3
"""Smoke test for the official CostMarshal v2 CLI entrypoint."""

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


def run(root: Path, *args: str, expect_ok: bool = True) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env["COSTMARSHAL_V2_HOME"] = str(root)
    result = subprocess.run(
        [sys.executable, str(CLI), "--root", str(root), *args],
        cwd=str(ROOT),
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


def run_json(root: Path, *args: str) -> dict:
    return json.loads(run(root, *args).stdout)


def assert_true(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def main() -> int:
    temp = Path(tempfile.mkdtemp(prefix="costmarshal-v2-official-smoke-"))
    try:
        init = run_json(
            temp,
            "init",
            "--name",
            "official-v2-smoke",
            "--objective",
            "Verify official v2 CLI entrypoint",
            "--backend",
            "local",
            "--allow-unsafe-native-workers",
        )
        project = Path(init["project"])
        assert_true(init["backend"] == "local", "official smoke should use portable local backend")
        assert_true((project / "project.json").is_file(), "init should create v2 project state")

        help_text = run(temp, "--version").stdout
        assert_true("v2.3.0-beta" in help_text, "official CLI should expose v2 version")

        plan = run_json(temp, "start-leader", "--project", str(project), "--command", "codex --prompt {prompt_file}", "--dry-run")
        assert_true(plan["backend"] == "local", "start-leader should use v2 backend abstraction")
        assert_true(Path(plan["prompt_file"]).is_file(), "leader prompt should be durable")

        task = run_json(
            temp,
            "new-task",
            "--project",
            str(project),
            "--title",
            "Official v2 task",
            "--purpose",
            "Exercise official v2 task lifecycle",
            "--claim-path",
            "reports/result.md",
        )
        assert_true(task["task_id"] == "V2-0001", "new-task should use v2 task ids")
        dispatch = run_json(temp, "dispatch", "--project", str(project), "--task", "V2-0001", "--model", "inherit", "--unsafe-native")
        assert_true(dispatch["actor_id"] == "agent-v2-0001", "dispatch should create a v2 agent actor")
        usage = run_json(
            temp,
            "record-usage",
            "--project",
            str(project),
            "--actor",
            "agent-v2-0001",
            "--input-tokens",
            "8",
            "--output-tokens",
            "4",
            "--estimated-cost-cny",
            "0.001",
        )
        assert_true(usage["event"]["total_tokens"] == 12, "record-usage should record actor token usage")

        report = project / "tasks" / "V2-0001" / "completion-report.md"
        report.write_text("# Completion Report: V2-0001\n\nStatus: done\n\n## Result\nOfficial smoke done.\n", encoding="utf-8")
        run_json(temp, "collect", "--project", str(project), "--task", "V2-0001", "--state", "waiting_leader")
        result = run_json(
            temp,
            "record-result",
            "--project",
            str(project),
            "--task",
            "V2-0001",
            "--status",
            "done",
            "--quality-score",
            "4",
            "--accepted-by-leader",
        )
        assert_true(result["event"]["accepted_by_leader"] is True, "record-result should record leader acceptance")

        status = run_json(temp, "status", "--project", str(project), "--format", "json")
        assert_true(status["backend"]["kind"] == "local", "status should expose backend state")
        assert_true(status["result_summary"]["accepted"] == 1, "status should summarize accepted results")
        dashboard = run_json(temp, "dashboard", "--project", str(project), "--format", "json")
        assert_true(any(row["id"] == "scheduler" for row in dashboard["processes"]), "dashboard should include the scheduler process row")
        agent_process = next(row for row in dashboard["processes"] if row["id"] == "agent-v2-0001")
        assert_true(agent_process["token_usage"]["total_tokens"] == 12, "dashboard should expose agent token totals")
        validation = run_json(temp, "validate", "--project", str(project))
        assert_true(validation["status"] == "ok", "validate should pass for official v2 smoke")

        print(json.dumps({"status": "ok", "temporary_state": "cleaned"}, indent=2))
        return 0
    finally:
        resolved = temp.resolve()
        temp_root = Path(tempfile.gettempdir()).resolve()
        if resolved == temp_root or temp_root not in resolved.parents:
            raise RuntimeError(f"Refusing to delete unexpected path: {resolved}")
        shutil.rmtree(resolved, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
