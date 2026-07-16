#!/usr/bin/env python3
"""Smoke test for the CostMarshal v2 runtime package."""

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
    temp = Path(tempfile.mkdtemp(prefix="costmarshal-v2-smoke-"))
    try:
        source = temp / "legacy-source"
        source.mkdir()
        (source / "project.json").write_text('{"schema_version": 1, "id": "legacy-source"}\n', encoding="utf-8")
        (source / "marker.txt").write_text("do not mutate\n", encoding="utf-8")
        missing_source = run(
            temp,
            "init",
            "--name",
            "bad-source",
            "--objective",
            "Should reject missing source project",
            "--source-project",
            str(temp / "does-not-exist"),
            expect_ok=False,
        )
        assert_true("Source project not found" in (missing_source.stderr + missing_source.stdout), "init should reject a missing source project")

        init = run_json(
            temp,
            "init",
            "--name",
            "v2-smoke",
            "--objective",
            "Verify scheduler-first v2 runtime",
            "--source-project",
            str(source),
            "--leader-model",
            "gpt-5",
            "--session-name",
            "cmv2-smoke",
            "--backend",
            "local",
            "--allow-unsafe-native-workers",
        )
        project = Path(init["project"])
        assert_true(init["backend"] == "local", "smoke project should force the portable local backend")
        assert_true(project.is_dir(), "v2 project should be created")
        assert_true(not (source / "v2").exists(), "v2 init should not write inside the referenced legacy project")
        assert_true((source / "marker.txt").read_text(encoding="utf-8") == "do not mutate\n", "source project should remain untouched")

        start_plan = run_json(temp, "start-leader", "--project", str(project), "--command", "codex --prompt {prompt_file}", "--dry-run")
        assert_true(start_plan["dry_run"], "start-leader dry-run should not launch a runtime")
        assert_true(start_plan["backend"] == "local", "start-leader should use the configured local backend")
        assert_true(start_plan["planned_commands"], "start-leader dry-run should show backend command plan")
        assert_true(Path(start_plan["prompt_file"]).is_file(), "start-leader should refresh a durable leader prompt")
        assert_true(start_plan["prompt_file"] in start_plan["planned_commands"][0], "backend command plan should include prompt_file placeholder")
        leader_prompt = Path(start_plan["prompt_file"]).read_text(encoding="utf-8")
        assert_true("Role: `leader`" in leader_prompt, "leader prompt should declare the leader role")

        task = run_json(
            temp,
            "new-task",
            "--project",
            str(project),
            "--title",
            "Bounded inspection",
            "--purpose",
            "Inspect only the supplied source marker and report status",
            "--task-type",
            "analysis",
            "--agent",
            "deepseek",
            "--model",
            "deepseek-v4-flash",
            "--acceptance",
            "Report path is available to the leader",
            "--allowed-context",
            str(source / "marker.txt"),
            "--claim-path",
            "reports/shared.md",
        )
        assert_true(task["task_id"] == "V2-0001", "first v2 task id should be V2-0001")
        lock_conflict = run(
            temp,
            "new-task",
            "--project",
            str(project),
            "--title",
            "Conflicting writer",
            "--purpose",
            "Should not claim a path while another active task owns it",
            "--claim-path",
            "reports/shared.md",
            expect_ok=False,
        )
        assert_true("Path claim conflict" in (lock_conflict.stderr + lock_conflict.stdout), "new-task should reject active write-claim conflicts")

        dispatch = run_json(
            temp,
            "dispatch",
            "--project",
            str(project),
            "--task",
            "V2-0001",
            "--agent",
            "deepseek",
            "--model",
            "deepseek-v4-flash",
            "--command",
            "codex --model {model}",
            "--unsafe-native",
        )
        assert_true(dispatch["actor_id"] == "agent-v2-0001", "dispatch should create a task-scoped agent")
        assert_true(Path(dispatch["prompt_file"]).is_file(), "dispatch should create a durable agent prompt")
        agent_prompt = Path(dispatch["prompt_file"]).read_text(encoding="utf-8")
        assert_true("Role: `agent`" in agent_prompt, "agent prompt should declare the agent role")
        assert_true("## Efficiency Contract" in agent_prompt, "agent prompt should avoid redundant control-plane reads")
        assert_true("The assigned task below is authoritative" in agent_prompt, "agent prompt should inline bounded task context")
        unknown_send = run(
            temp,
            "send",
            "--project",
            str(project),
            "--to",
            "missing-agent",
            "--message",
            "should fail",
            expect_ok=False,
        )
        assert_true("Actor not found" in (unknown_send.stderr + unknown_send.stdout), "send should reject unknown actor recipients")
        delivered_once = run_json(
            temp,
            "send",
            "--project",
            str(project),
            "--sender",
            "leader",
            "--to",
            "agent-v2-0001",
            "--message",
            "already delivered by scheduler",
        )
        first_relay = run_json(temp, "relay", "--project", str(project), "--actor", "leader")
        assert_true(first_relay["processed"] == 1, "relay should process the leader outbox cursor")
        assert_true(first_relay["skipped"][0]["id"] == delivered_once["message"]["id"], "relay should skip messages already in the target inbox")
        leader_outbox = project / "scheduler" / "mailboxes" / "leader" / "outbox.jsonl"
        manual_message = {
            "id": "MSG-manual-relay",
            "timestamp": "2026-01-01T00:00:00+00:00",
            "from": "leader",
            "to": "agent-v2-0001",
            "subject": "Manual outbox relay",
            "body": "Please inspect your bounded task.",
            "task_id": "V2-0001",
        }
        with leader_outbox.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(manual_message, sort_keys=True) + "\n")
        second_relay = run_json(temp, "relay", "--project", str(project), "--actor", "leader")
        assert_true(second_relay["processed"] == 1, "relay should process only new outbox lines after the cursor")
        assert_true(second_relay["delivered"][0]["id"] == "MSG-manual-relay", "relay should deliver actor-authored outbox messages")
        agent_inbox = project / "scheduler" / "mailboxes" / "agent-v2-0001" / "inbox.jsonl"
        assert_true("MSG-manual-relay" in agent_inbox.read_text(encoding="utf-8"), "relayed message should appear in the agent inbox")

        run_json(temp, "heartbeat", "--project", str(project), "--actor", "agent-v2-0001", "--status", "running")
        running_status = run_json(temp, "status", "--project", str(project), "--format", "json")
        running_task = next(item for item in running_status["tasks"] if item["id"] == "V2-0001")
        assert_true(running_task["status"] == "running", "agent running heartbeat should advance the task to running")
        agent_outbox = project / "scheduler" / "mailboxes" / "agent-v2-0001" / "outbox.jsonl"
        usage_command = {
            "id": "MSG-agent-usage-command",
            "timestamp": "2026-01-01T00:00:00+00:00",
            "from": "agent-v2-0001",
            "to": "scheduler",
            "subject": "scheduler.command",
            "body": "record agent token usage",
            "task_id": "V2-0001",
            "metadata": {
                "command": "record_usage",
                "args": {
                    "input_tokens": 12,
                    "output_tokens": 3,
                    "estimated_cost_cny": 0.002,
                    "note": "scheduler loop smoke",
                },
            },
        }
        with agent_outbox.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(usage_command, sort_keys=True) + "\n")
        scheduler_once = run_json(temp, "run-scheduler", "--project", str(project), "--once", "--interval", "0.1")
        assert_true(scheduler_once["processed_commands"] == 1, "run-scheduler --once should execute agent-authored scheduler commands")
        dashboard = run_json(temp, "dashboard", "--project", str(project), "--format", "json")
        agent_process = next(item for item in dashboard["processes"] if item["id"] == "agent-v2-0001")
        assert_true(agent_process["token_usage"]["total_tokens"] == 15, "dashboard should expose cumulative agent token usage")
        scheduler_process = next(item for item in dashboard["processes"] if item["id"] == "scheduler")
        assert_true(scheduler_process["role"] == "scheduler", "dashboard should expose the scheduler process row")
        run_json(temp, "send", "--project", str(project), "--to", "leader", "--subject", "Manual relay", "--message", "Scheduler relay smoke message")
        missing_report = run(
            temp,
            "collect",
            "--project",
            str(project),
            "--task",
            "V2-0001",
            "--state",
            "waiting_leader",
            "--report",
            str(project / "tasks" / "V2-0001" / "missing-report.md"),
            expect_ok=False,
        )
        assert_true("Report file not found" in (missing_report.stderr + missing_report.stdout), "collect should reject missing terminal reports")

        report = project / "tasks" / "V2-0001" / "completion-report.md"
        report.write_text("# Completion Report: V2-0001\n\nStatus: done\n\n## Result\nSmoke done.\n", encoding="utf-8")
        run_json(
            temp,
            "heartbeat",
            "--project",
            str(project),
            "--actor",
            "agent-v2-0001",
            "--status",
            "waiting",
        )
        collect = run_json(temp, "collect", "--project", str(project), "--task", "V2-0001", "--state", "waiting_leader")
        assert_true(collect["actor_id"] == "agent-v2-0001", "collect should infer the task actor")
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
            "--model",
            "deepseek-v4-flash",
            "--input-tokens",
            "100",
            "--output-tokens",
            "50",
            "--estimated-cost-cny",
            "0.01",
            "--summary",
            "Smoke accepted",
        )
        assert_true(result["event"]["accepted_by_leader"] is True, "record-result should persist leader acceptance")
        leader_work = run_json(
            temp,
            "record-leader-work",
            "--project",
            str(project),
            "--task",
            "V2-0001",
            "--work-type",
            "verification",
            "--risk",
            "low",
            "--scope",
            "Smoke acceptance sampling",
            "--reason",
            "Leader acceptance requires evidence check",
            "--minutes",
            "2",
            "--estimated-cost-cny",
            "0.01",
        )
        assert_true(leader_work["event"]["work_type"] == "verification", "record-leader-work should persist leader audit rows")
        missing_self_work_task = run(
            temp,
            "record-leader-work",
            "--project",
            str(project),
            "--task",
            "V2-9999",
            "--scope",
            "Missing task",
            "--reason",
            "Negative smoke check",
            expect_ok=False,
        )
        assert_true("Task not found" in (missing_self_work_task.stderr + missing_self_work_task.stdout), "leader self-work should reject missing tasks")
        stopped = run_json(temp, "stop-actor", "--project", str(project), "--actor", "agent-v2-0001", "--reason", "smoke complete")
        assert_true(stopped["actor_status"] == "stopped", "stop-actor should mark the agent stopped")
        released_lock_task = run_json(
            temp,
            "new-task",
            "--project",
            str(project),
            "--title",
            "Released lock reuse",
            "--purpose",
            "Should be allowed after the first task reaches a terminal state",
            "--claim-path",
            "reports/shared.md",
        )
        assert_true(released_lock_task["task_id"] == "V2-0002", "released write claims should allow later tasks to reuse a path")
        invalid_transition = run(
            temp,
            "collect",
            "--project",
            str(project),
            "--task",
            "V2-0001",
            "--state",
            "waiting_leader",
            expect_ok=False,
        )
        assert_true("Invalid task state transition" in (invalid_transition.stderr + invalid_transition.stdout), "terminal tasks should not move back to waiting_leader")

        status = run_json(temp, "status", "--project", str(project), "--format", "json")
        assert_true(status["project"]["schema_version"] == 2, "status should report v2 schema")
        assert_true(status["actor_count"] == 2, "leader plus one agent should be registered")
        assert_true(status["task_state_counts"]["done"] == 1, "collected task should be marked done")
        assert_true(status["active_locks"][0]["task_id"] == "V2-0002", "status should expose active write claims for the new task")
        assert_true(status["active_locks"][0]["path"] == "reports/shared.md", "active write claim should use normalized paths")
        assert_true(status["result_summary"]["count"] == 1, "status should summarize recorded results")
        assert_true(status["result_summary"]["accepted"] == 1, "status should count leader-accepted results")
        assert_true(status["result_summary"]["estimated_cost_cny"] == 0.01, "status should summarize result cost")
        assert_true(status["leader_self_work"]["count"] == 1, "status should summarize leader self-work audit rows")
        assert_true(status["leader_self_work"]["total_minutes"] == 2, "status should summarize leader self-work minutes")
        assert_true(status["leader_self_work"]["estimated_cost_cny"] == 0.01, "status should summarize leader self-work cost")
        assert_true(status["usage_summary"]["count"] == 1, "status should summarize actor usage rows")
        agent = next(actor for actor in status["actors"] if actor["id"] == "agent-v2-0001")
        assert_true(agent["status"] == "stopped", "stopped agent should appear in status")
        assert_true(agent["token_usage"]["total_tokens"] == 150, "status should use the final result token count when it exceeds partial usage")
        leader = next(actor for actor in status["actors"] if actor["id"] == "leader")
        assert_true(leader["mailbox_counts"]["inbox"] >= 2, "leader should receive dispatch and collect messages")
        assert_true(status["relay_cursors"]["actors"]["leader"]["outbox_lines"] == 2, "status should expose the leader relay cursor")
        leader_create_task = {
            "id": "MSG-leader-create-task-command",
            "timestamp": "2026-01-01T00:00:00+00:00",
            "from": "leader",
            "to": "scheduler",
            "subject": "scheduler.command",
            "body": "create a follow-up task",
            "metadata": {
                "command": "create_task",
                "args": {
                    "title": "Leader commanded task",
                    "purpose": "Verify leader-authored scheduler commands create tasks",
                    "claim_path": "reports/leader-commanded.md",
                },
            },
        }
        with leader_outbox.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(leader_create_task, sort_keys=True) + "\n")
        leader_scheduler_once = run_json(temp, "run-scheduler", "--project", str(project), "--once", "--interval", "0.1")
        assert_true(leader_scheduler_once["processed_commands"] == 1, "run-scheduler should execute leader-authored scheduler commands")
        commanded_status = run_json(temp, "status", "--project", str(project), "--format", "json")
        assert_true(any(task["title"] == "Leader commanded task" for task in commanded_status["tasks"]), "leader create_task command should create a durable task")

        recovery = run_json(temp, "recover", "--project", str(project), "--plan-restarts")
        assert_true(recovery["status"] in {"ok", "degraded"}, "recover should audit without requiring tmux")
        validation = run_json(temp, "validate", "--project", str(project))
        assert_true(validation["status"] == "ok", "validate should pass for the v2 smoke project")
        agent_prompt_path = Path(dispatch["prompt_file"])
        agent_prompt_path.unlink()
        missing_prompt = run(temp, "validate", "--project", str(project), expect_ok=False)
        assert_true("prompt file missing" in (missing_prompt.stderr + missing_prompt.stdout), "validate should catch a missing actor prompt")
        run_json(temp, "recover", "--project", str(project))
        repaired_validation = run_json(temp, "validate", "--project", str(project))
        assert_true(repaired_validation["status"] == "ok", "recover should rebuild missing actor prompts")
        status_path = project / "tasks" / "V2-0001" / "status.json"
        status_data = json.loads(status_path.read_text(encoding="utf-8"))
        status_data["state"] = "failed"
        status_path.write_text(json.dumps(status_data, indent=2) + "\n", encoding="utf-8")
        invalid = run(temp, "validate", "--project", str(project), expect_ok=False)
        assert_true("task.json status=done but status.json state=failed" in (invalid.stderr + invalid.stdout), "validate should catch task/status drift")

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
