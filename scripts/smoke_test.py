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
        existing = temp / "external-existing"
        (existing / "reports").mkdir(parents=True)
        (existing / "scripts").mkdir(parents=True)
        (existing / "logs").mkdir(parents=True)
        (existing / "README.md").write_text("# Existing Project\n\nCurrent goal and notes.\n", encoding="utf-8")
        (existing / "reports" / "result-summary.md").write_text("Accuracy improved after baseline run.\n", encoding="utf-8")
        (existing / "scripts" / "run_eval.py").write_text("print('eval')\n", encoding="utf-8")
        (existing / "logs" / "failed-run.log").write_text("Traceback: example failure to avoid.\n", encoding="utf-8")

        adopted_info = run_json(
            temp,
            "adopt-project",
            "--path",
            str(existing),
            "--name",
            "adopted-smoke",
            "--objective",
            "Continue the existing smoke project under CostMarshal rules",
            "--kind",
            "research",
        )
        adopted = Path(adopted_info["project"])
        adopted_project_json = json.loads((adopted / "project.json").read_text(encoding="utf-8"))
        assert_true(adopted_project_json["plan_approval"]["status"] == "not_drafted", "adopted projects should still require plan drafting")
        assert_true((adopted / "adopted-project.json").is_file(), "adopted-project.json should record source metadata")
        imported_progress = (adopted / "imported-progress.md").read_text(encoding="utf-8")
        reusable_candidates = (adopted / "reusable-candidates.md").read_text(encoding="utf-8")
        branch_tree = json.loads((adopted / "branch-tree.json").read_text(encoding="utf-8"))
        assert_true("Existing Project" in imported_progress, "imported progress should include observed project notes")
        assert_true("observed_reuse_candidate" in reusable_candidates, "reusable candidates should classify observed reuse")
        assert_true("failed_attempt" in reusable_candidates, "reusable candidates should classify failures")
        assert_true("imported-progress" in [node["id"] for node in branch_tree["nodes"]], "branch tree should include imported progress node")

        adopted_blocked = run(
            temp,
            "new-task",
            "--project",
            str(adopted),
            "--title",
            "blocked adopted task",
            "--purpose",
            "should fail before adopted plan approval",
            "--agent",
            "deepseek",
            "--task-type",
            "analysis",
            expect_ok=False,
        )
        assert_true("not approved" in (adopted_blocked.stderr + adopted_blocked.stdout), "adopted project should enforce plan approval")
        run_json(
            temp,
            "draft-plan",
            "--project",
            str(adopted),
            "--summary",
            "Use the imported progress only as evidence, then plan the next bounded step.",
            "--predicted-cost-cny",
            "1",
            "--predicted-wall-time",
            "5m",
        )
        run_json(temp, "approve-plan", "--project", str(adopted), "--approved-by", "smoke-test")
        adopted_task = run_json(
            temp,
            "new-task",
            "--project",
            str(adopted),
            "--title",
            "Review imported progress",
            "--purpose",
            "Create a bounded review after adoption approval",
            "--agent",
            "deepseek",
            "--task-type",
            "analysis",
        )
        assert_true(adopted_task["task_id"] == "CM-0001", "adopted project should create normal tasks after approval")
        adopted_validation = run_json(temp, "validate", "--project", str(adopted))
        assert_true(adopted_validation["status"] == "ok", "validate should pass for adopted project")

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
        evolution = (project / "reports" / "evolution-report.md").read_text(encoding="utf-8")
        knowledge_index = json.loads((temp / "memory" / "knowledge-index.json").read_text(encoding="utf-8"))
        assert_true("## Agent Performance" in summary, "project summary should include agent performance")
        assert_true("## Task Ledger" in summary, "project summary should include task ledger")
        assert_true("deepseek-smoke-model" in summary, "project summary should include recorded model names")
        assert_true("Wait Time" in summary, "project summary should include wait time")
        assert_true("## Routing Evolution" in evolution, "evolution report should include routing evolution")
        assert_true("## Knowledge Candidates" in evolution, "evolution report should include knowledge candidates")
        assert_true("mechanical" in knowledge_index.get("categories", {}), "knowledge index should be grouped by task type")
        mechanical_lessons = knowledge_index["categories"]["mechanical"]["lessons"]
        assert_true(mechanical_lessons, "knowledge index should contain at least one lesson")
        lesson_path = temp / mechanical_lessons[0]["path"]
        assert_true(lesson_path.is_file(), "knowledge lesson file should exist")
        assert_true("Retrieval Boundary" in lesson_path.read_text(encoding="utf-8"), "knowledge lesson should include retrieval boundary")

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
