from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
CLI = ROOT / "scripts" / "costmarshal.py"
sys.path.insert(0, str(ROOT))

from costmarshal_v2.profiles import provider_profile_text  # noqa: E402


def run(temp: Path, *args: str, ok: bool = True) -> subprocess.CompletedProcess[str]:
    env = dict(os.environ)
    env["COSTMARSHAL_V2_HOME"] = str(temp / "runtime")
    result = subprocess.run([sys.executable, str(CLI), *args], text=True, capture_output=True, env=env)
    if ok and result.returncode:
        raise AssertionError(f"command failed {args}\n{result.stdout}\n{result.stderr}")
    return result


def run_json(temp: Path, *args: str) -> dict:
    return json.loads(run(temp, *args).stdout)


def append(path: Path, row: dict) -> None:
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(row, sort_keys=True) + "\n")


def command(message_id: str, actor: str, task: str, name: str, args: dict) -> dict:
    return {
        "id": message_id,
        "timestamp": "2026-01-01T00:00:00Z",
        "from": actor,
        "to": "scheduler",
        "subject": "scheduler.command",
        "task_id": task,
        "metadata": {"command": name, "args": args},
    }


def main() -> int:
    temp = Path(tempfile.mkdtemp(prefix="costmarshal-v2-reliability-"))
    try:
        codex_home = temp / "codex-home"
        codex_home.mkdir()
        for profile, provider_id, env_key in (
            ("longcat", "longcat", "LONGCAT_API_KEY"),
            ("deepseek", "deepseek", "DEEPSEEK_API_KEY"),
        ):
            (codex_home / f"{profile}.config.toml").write_text(
                provider_profile_text(
                    provider_id=provider_id,
                    display_name=provider_id,
                    base_url=f"https://{provider_id}.example/v1",
                    model="test-model",
                    env_key=env_key,
                ),
                encoding="utf-8",
            )
        os.environ["CODEX_HOME"] = str(codex_home)
        workspace = temp / "workspace"
        workspace.mkdir()
        init = run_json(temp, "init", "--name", "reliability", "--objective", "replay fencing", "--workspace", str(workspace), "--backend", "local", "--governance", "off", "--allow-unsafe-native-workers")
        project = Path(init["project"])
        run_json(temp, "new-task", "--project", str(project), "--title", "bounded", "--purpose", "test fencing", "--task-type", "analysis", "--risk", "low")
        dispatch = run_json(temp, "dispatch", "--project", str(project), "--task", "V2-0001", "--unsafe-native")
        actor = dispatch["actor_id"]
        actor_state = json.loads((project / "scheduler" / "actors" / f"{actor}.json").read_text(encoding="utf-8"))
        attempt = actor_state["attempt_id"]
        inbox = project / "scheduler" / "mailboxes" / "scheduler" / "inbox.jsonl"

        usage = command(
            "MSG-idempotent-usage",
            actor,
            "V2-0001",
            "record_usage",
            {"actor": actor, "task": "V2-0001", "attempt": attempt, "input_tokens": 10, "output_tokens": 5},
        )
        append(inbox, usage)
        append(inbox, usage)
        run_json(temp, "run-scheduler", "--project", str(project), "--once")
        usage_rows = [json.loads(line) for line in (project / "reports" / "usage.jsonl").read_text(encoding="utf-8").splitlines() if line]
        assert len([row for row in usage_rows if row.get("command_id") == "MSG-idempotent-usage"]) == 1

        report = project / "tasks" / "V2-0001" / "completion-report.md"
        report.write_text(
            "# Completion Report: V2-0001\n\nStatus: escalate\n\nNeed more capability.\n",
            encoding="utf-8",
        )
        run_json(temp, "heartbeat", "--project", str(project), "--actor", actor, "--status", "waiting")
        run_json(
            temp,
            "collect",
            "--command-id",
            "CMD-reliability-collect-low",
            "--project",
            str(project),
            "--task",
            "V2-0001",
            "--actor",
            actor,
            "--attempt",
            attempt,
            "--state",
            "escalate",
        )
        run_json(
            temp,
            "record-result",
            "--command-id",
            "CMD-reliability-reject-low",
            "--project",
            str(project),
            "--task",
            "V2-0001",
            "--actor",
            actor,
            "--attempt",
            attempt,
            "--status",
            "escalate",
            "--quality-score",
            "3",
            "--summary",
            "leader requires stronger capability",
        )

        escalation_args = {"task": "V2-0001", "actor": actor, "attempt": attempt, "reason": "need more capability", "start": False}
        append(inbox, command("MSG-escalate-1", actor, "V2-0001", "escalate_task", escalation_args))
        append(inbox, command("MSG-escalate-stale", actor, "V2-0001", "escalate_task", escalation_args))
        cycle = run_json(temp, "run-scheduler", "--project", str(project), "--once")
        assert cycle["processed_commands"] == 0
        assert cycle["failed_commands"] == 2
        task = json.loads((project / "tasks" / "V2-0001" / "task.json").read_text(encoding="utf-8"))
        assert [row["tier"] for row in task["attempts"]] == ["low"]
        run_json(
            temp,
            "escalate",
            "--project",
            str(project),
            "--task",
            "V2-0001",
            "--reason",
            "leader authorizes stronger capability",
            "--unsafe-native",
        )
        task = json.loads((project / "tasks" / "V2-0001" / "task.json").read_text(encoding="utf-8"))
        assert [row["tier"] for row in task["attempts"]] == ["low", "medium"]

        current = task["attempts"][-1]
        done_collect = command(
            "MSG-worker-done",
            current["actor_id"],
            "V2-0001",
            "collect_task",
            {"task": "V2-0001", "actor": current["actor_id"], "attempt": current["attempt_id"], "state": "done"},
        )
        append(inbox, done_collect)
        cycle = run_json(temp, "run-scheduler", "--project", str(project), "--once")
        assert cycle["failed_commands"] == 1
        task = json.loads((project / "tasks" / "V2-0001" / "task.json").read_text(encoding="utf-8"))
        assert task["status"] != "done"

        env = dict(os.environ)
        env["COSTMARSHAL_V2_HOME"] = str(temp / "runtime")
        first_scheduler = subprocess.Popen(
            [sys.executable, str(CLI), "run-scheduler", "--project", str(project), "--interval", "0.5", "--max-cycles", "4"],
            env=env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        time.sleep(0.2)
        duplicate_scheduler = run(temp, "run-scheduler", "--project", str(project), "--once", ok=False)
        assert duplicate_scheduler.returncode != 0 and "another scheduler instance" in (duplicate_scheduler.stdout + duplicate_scheduler.stderr)
        first_scheduler.communicate(timeout=10)

        broken = json.loads(
            run(
                temp,
                "init",
                "--name",
                "broken-launch",
                "--objective",
                "recover launch failure",
                "--workspace",
                str(workspace),
                "--backend",
                "tmux",
                "--backend-command",
                str(temp / "missing-tmux.exe"),
                "--governance",
                "off",
                "--allow-unsafe-native-workers",
            ).stdout
        )
        broken_project = Path(broken["project"])
        broken_json = lambda *args: json.loads(run(temp, *args).stdout)
        broken_json("new-task", "--project", str(broken_project), "--title", "launch", "--purpose", "fail safely")
        launch = run(temp, "dispatch", "--project", str(broken_project), "--task", "V2-0001", "--start", "--unsafe-native", ok=False)
        assert launch.returncode != 0
        broken_task = json.loads((broken_project / "tasks" / "V2-0001" / "task.json").read_text(encoding="utf-8"))
        broken_actor = json.loads((broken_project / "scheduler" / "actors" / "agent-v2-0001.json").read_text(encoding="utf-8"))
        assert broken_task["attempts"][-1]["status"] == "needs_recovery"
        assert broken_actor["status"] == "needs_recovery"
        print("reliability contract ok")
        return 0
    finally:
        shutil.rmtree(temp, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
