#!/usr/bin/env python3
"""End-to-end smoke test for the CostMarshal CLI.

The test uses only temporary state and no provider network calls.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
CLI = ROOT / "scripts" / "costmarshal.py"


def run(root: Path, *args: str, expect_ok: bool = True) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        [sys.executable, str(CLI), "--root", str(root), *args],
        cwd=str(ROOT),
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
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
    result = run(root, *args)
    return json.loads(result.stdout)


def assert_true(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def main() -> int:
    temp = Path(tempfile.mkdtemp(prefix="costmarshal-smoke-"))
    try:
        run_json(temp, "init-root")
        project_info = run_json(
            temp,
            "new-project",
            "--name",
            "smoke",
            "--objective",
            "Verify CostMarshal core workflow",
        )
        project = Path(project_info["project"])
        project_json = json.loads((project / "project.json").read_text(encoding="utf-8"))
        assert_true(project_json["budget"]["max_project_cost_cny"] == 20.0, "default project budget should be CNY 20")
        assert_true((project / "memory" / "wait-events.jsonl").exists(), "wait-events.jsonl should be initialized")

        blocked = run(
            temp,
            "new-task",
            "--project",
            str(project),
            "--title",
            "blocked before approval",
            "--purpose",
            "should fail",
            "--agent",
            "deepseek",
            "--task-type",
            "analysis",
            expect_ok=False,
        )
        assert_true("not approved" in (blocked.stderr + blocked.stdout), "new-task should enforce plan approval")

        run_json(
            temp,
            "draft-plan",
            "--project",
            str(project),
            "--summary",
            "Lightweight direction check; details will adapt after first evidence.",
            "--predicted-cost-cny",
            "1",
            "--predicted-wall-time",
            "5m",
        )
        run_json(temp, "approve-plan", "--project", str(project), "--approved-by", "smoke-test")

        task_info = run_json(
            temp,
            "new-task",
            "--project",
            str(project),
            "--title",
            "Prove reusable mechanical flow",
            "--purpose",
            "Create a reusable replay memory source task",
            "--agent",
            "deepseek",
            "--difficulty",
            "B",
            "--risk",
            "low",
            "--task-type",
            "mechanical",
            "--claim-path",
            "reports/smoke.md",
        )
        assert_true(task_info["task_id"] == "CM-0001", "first task id should be CM-0001")

        run_json(
            temp,
            "record-result",
            "--project",
            str(project),
            "--task",
            "CM-0001",
            "--status",
            "done",
            "--agent",
            "deepseek",
            "--model",
            "deepseek-smoke-model",
            "--quality-score",
            "4",
            "--accepted-by-leader",
            "--input-tokens",
            "1000",
            "--output-tokens",
            "500",
        )
        run(temp, "wait-task", "--project", str(project), "--task", "CM-0001", "--every", "1s", "--timeout", "2s")

        run_json(
            temp,
            "promote-memory",
            "--project",
            str(project),
            "--source-task",
            "CM-0001",
            "--name",
            "reusable-flow",
            "--memory-task-type",
            "mechanical",
            "--summary",
            "Exact replayable smoke flow",
            "--working-dir",
            ".",
            "--required-input",
            "project exists",
            "--allowed-param",
            "shard",
            "--allowed-command",
            "python run_eval.py --shard <shard>",
            "--expected-output",
            "reports/smoke.md",
            "--success-marker",
            "status done",
        )

        replay_info = run_json(
            temp,
            "new-task",
            "--project",
            str(project),
            "--title",
            "Replay reusable flow",
            "--purpose",
            "Use replay memory with a new parameter",
            "--agent",
            "longcat",
            "--difficulty",
            "C",
            "--risk",
            "low",
            "--task-type",
            "mechanical",
            "--replay-memory",
            "reusable-flow",
            "--depends-on",
            "CM-0001",
        )
        assert_true(replay_info["task_id"] == "CM-0002", "replay task id should be CM-0002")

        run_json(
            temp,
            "record-memory-feedback",
            "--project",
            str(project),
            "--task",
            "CM-0002",
            "--outcome",
            "succeeded",
            "--sufficient",
            "yes",
            "--memory-quality",
            "5",
            "--attribution",
            "unknown",
        )

        run_json(
            temp,
            "promote-memory",
            "--project",
            str(project),
            "--source-task",
            "CM-0001",
            "--name",
            "draft-flow",
            "--summary",
            "Draft memory should not be attachable",
            "--draft",
        )
        draft_blocked = run(
            temp,
            "new-task",
            "--project",
            str(project),
            "--title",
            "Blocked draft replay",
            "--purpose",
            "should fail",
            "--agent",
            "longcat",
            "--task-type",
            "mechanical",
            "--replay-memory",
            "draft-flow",
            expect_ok=False,
        )
        assert_true("not complete" in (draft_blocked.stderr + draft_blocked.stdout), "draft replay memory should be blocked")

        status = run_json(temp, "status-project", "--project", str(project), "--format", "json")
        first_task = status["tasks"][0]
        assert_true(first_task["model"] == "deepseek-smoke-model", "status should preserve recorded model name")
        assert_true(first_task["summary"], "status should include a task summary")
        assert_true(first_task["wait_count"] == 1, "status should include wait count")
        assert_true(first_task["wait_elapsed_seconds"] >= 0, "status should include wait elapsed seconds")
        assert_true(status["replay_memory"][0]["status"] in {"complete", "draft"}, "status should include replay memory")

        validation = run_json(temp, "validate", "--project", str(project))
        assert_true(validation["status"] == "ok", "validate should pass for the smoke project")

        run_json(temp, "finish-project", "--project", str(project))
        summary = (project / "reports" / "project-summary.md").read_text(encoding="utf-8")
        assert_true("## Agent Performance" in summary, "project summary should include agent performance")
        assert_true("## Task Ledger" in summary, "project summary should include task ledger")
        assert_true("deepseek-smoke-model" in summary, "project summary should include recorded model names")
        assert_true("Wait Time" in summary, "project summary should include wait time")

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
