#!/usr/bin/env python3
"""Fast unit checks for CostMarshal v2 pure helpers."""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from costmarshal_v2.paths import ProjectLayout, actor_runtime_name, actor_target, relpath, slugify  # noqa: E402
from costmarshal_v2.scheduler import normalize_claim_path, paths_conflict, summarize_leader_self_work, summarize_results  # noqa: E402
from costmarshal_v2.session_backend import select_backend_kind  # noqa: E402
from costmarshal_v2.state import can_transition_task  # noqa: E402
from costmarshal_v2.session_backend import command_to_string, format_actor_command  # noqa: E402


def assert_true(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def main() -> int:
    assert_true(slugify(" Agent V2/0001 ") == "agent-v2-0001", "slugify should normalize actor names")
    assert_true(actor_runtime_name("agent:V2 0001") == "agent-v2-0001", "actor runtime names should be stable slugs")
    assert_true(actor_target("cmv2-demo", "agent:V2 0001") == "cmv2-demo:agent-v2-0001", "runtime targets should combine session/actor")
    assert_true(select_backend_kind("local") == "local", "backend selection should allow explicit local")
    assert_true(select_backend_kind("tmux") == "tmux", "backend selection should allow explicit tmux")

    assert_true(can_transition_task("planned", "dispatched"), "planned should dispatch")
    assert_true(can_transition_task("dispatched", "running"), "dispatched should become running")
    assert_true(can_transition_task("running", "done"), "running should finish")
    assert_true(not can_transition_task("done", "waiting_leader"), "terminal tasks should not reopen implicitly")
    assert_true(normalize_claim_path(r"Reports\\Shared.md") == "reports/shared.md", "claim paths should normalize separators and case")
    assert_true(paths_conflict("reports", "reports/shared.md"), "parent directory claims should conflict with children")
    assert_true(paths_conflict("reports/shared.md", "reports/shared.md"), "identical claims should conflict")
    assert_true(not paths_conflict("reports/a.md", "reports/b.md"), "sibling files should not conflict")
    results = summarize_results(
        [
            {
                "status": "done",
                "agent": "deepseek",
                "accepted_by_leader": True,
                "quality_score": 4,
                "input_tokens": 100,
                "output_tokens": 50,
                "total_tokens": 150,
                "estimated_cost_cny": 0.01,
            },
            {
                "status": "escalate",
                "agent": "kimi",
                "needs_escalation": True,
                "quality_score": 2,
                "estimated_cost_cny": None,
            },
        ]
    )
    assert_true(results["count"] == 2 and results["accepted"] == 1, "result summary should count accepted attempts")
    assert_true(results["escalated"] == 1, "result summary should count escalations")
    assert_true(results["avg_quality"] == 3.0, "result summary should average quality scores")
    assert_true(results["estimated_cost_cny"] == 0.01 and results["unknown_cost_count"] == 1, "result summary should separate known and unknown costs")
    leader_work = summarize_leader_self_work(
        [
            {
                "work_type": "verification",
                "risk": "low",
                "minutes": 2,
                "input_tokens": 10,
                "output_tokens": 5,
                "total_tokens": 15,
                "estimated_cost_cny": 0.02,
            }
        ]
    )
    assert_true(leader_work["count"] == 1 and leader_work["total_minutes"] == 2, "leader self-work summary should count minutes")
    assert_true(leader_work["by_type"]["verification"] == 1 and leader_work["by_risk"]["low"] == 1, "leader self-work summary should bucket rows")

    with tempfile.TemporaryDirectory(prefix="costmarshal-v2-unit-") as tmp:
        project = Path(tmp) / "project"
        layout = ProjectLayout(root=Path(tmp), project_dir=project)
        assert_true(layout.root == Path(tmp).resolve(), "project layout root should be canonical")
        assert_true(layout.project_dir == project.resolve(), "project directory should be canonical")
        actor = {
            "id": "agent-v2-0001",
            "task_id": "V2-0001",
            "model": "gpt-5",
            "mailbox": {"dir": "scheduler/mailboxes/agent-v2-0001"},
            "prompt_path": "scheduler/actors/agent-v2-0001.prompt.md",
        }
        session = {"project_id": "P-1"}
        formatted = format_actor_command(
            "codex --model {model} --project {project} --task {task} --mailbox {mailbox} --prompt {prompt_file} --brief {brief} --report {report}",
            layout=layout,
            session=session,
            actor=actor,
        )
        assert_true("gpt-5" in formatted and "V2-0001" in formatted, "actor command should substitute known fields")
        assert_true("agent-v2-0001.prompt.md" in formatted, "actor command should substitute prompt_file")
        assert_true("brief.md" in formatted and "completion-report.md" in formatted, "actor command should substitute task files")
        assert_true(format_actor_command("codex {unknown}", layout=layout, session=session, actor=actor) == "codex {unknown}", "unknown template fields should remain safe")
        assert_true(relpath(project / "tasks" / "V2-0001", project) == "tasks/V2-0001", "relpath should use project-relative paths")

    rendered = command_to_string(["tmux", "new-session", "-s", "demo"])
    assert_true("tmux" in rendered and "demo" in rendered, "command_to_string should render commands for diagnostics")
    print("v2 unit ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
