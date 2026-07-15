#!/usr/bin/env python3
"""Scheduler commands use SQLite atomically after an explicit state cutover."""

from __future__ import annotations

import json
import os
from copy import deepcopy
import shutil
import sqlite3
import subprocess
import sys
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
CLI = ROOT / "scripts" / "costmarshal.py"
sys.path.insert(0, str(ROOT))

from costmarshal_v2.routing import default_provider_catalog  # noqa: E402


def run(temp: Path, *args: str, ok: bool = True, fault: str | None = None) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env["COSTMARSHAL_V2_HOME"] = str(temp / "runtime")
    if fault:
        env["COSTMARSHAL_CONTROL_STORE_FAULT"] = fault
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


def run_json(temp: Path, *args: str) -> dict:
    return json.loads(run(temp, *args).stdout)


def main() -> int:
    temp = Path(tempfile.mkdtemp(prefix="costmarshal-v2-transactional-scheduler-"))
    try:
        workspace = temp / "workspace"
        workspace.mkdir()
        catalog = default_provider_catalog()
        prices = {"longcat": 0.01, "deepseek": 5.0, "codex": 10.0}
        for provider in catalog["providers"]:
            provider["input_cny_per_1m"] = prices[provider["provider_id"]]
            provider["output_cny_per_1m"] = prices[provider["provider_id"]]
        medium_value = deepcopy(catalog["providers"][1])
        medium_value["provider_id"] = "deepseek-value"
        medium_value["profile"] = "deepseek-value"
        medium_value["priority"] = 999
        medium_value["input_cny_per_1m"] = 0.1
        medium_value["output_cny_per_1m"] = 0.1
        catalog["providers"].append(medium_value)
        catalog_path = temp / "providers.json"
        catalog_path.write_text(json.dumps(catalog), encoding="utf-8")
        project = Path(
            run_json(
                temp,
                "init",
                "--name",
                "transactional-scheduler",
                "--objective",
                "atomic scheduler state",
                "--workspace",
                str(workspace),
                "--backend",
                "local",
                "--governance",
                "off",
                "--allow-unsafe-native-workers",
                "--provider-catalog",
                str(catalog_path),
            )["project"]
        )
        run_json(
            temp,
            "new-task",
            "--project",
            str(project),
            "--title",
            "first",
            "--purpose",
            "replay dispatch",
            "--estimated-input-tokens",
            "500000",
            "--estimated-output-tokens",
            "500000",
        )
        preview = run_json(temp, "migrate-state", "--project", str(project))
        assert preview["status"] == "preview"
        migration = run_json(temp, "migrate-state", "--project", str(project), "--apply")
        assert migration["status"] == "enabled"

        first = run_json(
            temp,
            "dispatch",
            "--project",
            str(project),
            "--task",
            "V2-0001",
            "--command-id",
            "CMD-dispatch-first",
            "--unsafe-native",
        )
        replay = run_json(
            temp,
            "dispatch",
            "--project",
            str(project),
            "--task",
            "V2-0001",
            "--command-id",
            "CMD-dispatch-first",
            "--unsafe-native",
        )
        assert replay == first
        task = json.loads((project / "tasks" / "V2-0001" / "task.json").read_text(encoding="utf-8"))
        assert len(task["attempts"]) == 1

        escalated = run_json(
            temp,
            "escalate",
            "--project",
            str(project),
            "--task",
            "V2-0001",
            "--reason",
            "exercise nested transactional dispatch",
            "--command-id",
            "CMD-escalate-first",
            "--unsafe-native",
        )
        assert escalated["task_id"] == "V2-0001"
        task = json.loads((project / "tasks" / "V2-0001" / "task.json").read_text(encoding="utf-8"))
        assert len(task["attempts"]) == 2
        assert task["attempts"][1]["tier"] == "medium"
        assert task["attempts"][0]["route_decision"]["planned_provider_ids"][1] == "deepseek-value"
        assert task["attempts"][1]["provider"] == "deepseek-value"

        conflict = run(
            temp,
            "dispatch",
            "--project",
            str(project),
            "--task",
            "V2-0001",
            "--provider",
            "deepseek",
            "--command-id",
            "CMD-dispatch-first",
            "--unsafe-native",
            ok=False,
        )
        assert "reused with a different" in (conflict.stdout + conflict.stderr)

        actor_id = first["actor_id"]
        actor = json.loads((project / "scheduler" / "actors" / f"{actor_id}.json").read_text(encoding="utf-8"))
        usage_args = (
            "record-usage",
            "--project",
            str(project),
            "--actor",
            actor_id,
            "--task",
            "V2-0001",
            "--attempt",
            actor["attempt_id"],
            "--input-tokens",
            "11",
            "--output-tokens",
            "7",
            "--command-id",
            "CMD-usage-first",
        )
        usage = run_json(temp, *usage_args)
        assert run_json(temp, *usage_args) == usage
        usage_rows = [json.loads(line) for line in (project / "reports" / "usage.jsonl").read_text(encoding="utf-8").splitlines() if line]
        assert len([row for row in usage_rows if row.get("command_id") == "CMD-usage-first"]) == 1

        run_json(
            temp,
            "new-task",
            "--project",
            str(project),
            "--title",
            "second",
            "--purpose",
            "hard exit after commit",
            "--command-id",
            "CMD-new-second",
        )
        crashed = run(
            temp,
            "dispatch",
            "--project",
            str(project),
            "--task",
            "V2-0002",
            "--command-id",
            "CMD-dispatch-crash",
            "--unsafe-native",
            ok=False,
            fault="transaction.after_commit_before_materialize",
        )
        assert crashed.returncode == 86
        recovered = run_json(
            temp,
            "dispatch",
            "--project",
            str(project),
            "--task",
            "V2-0002",
            "--command-id",
            "CMD-dispatch-crash",
            "--unsafe-native",
        )
        assert recovered["task_id"] == "V2-0002"
        second = json.loads((project / "tasks" / "V2-0002" / "task.json").read_text(encoding="utf-8"))
        assert len(second["attempts"]) == 1

        run_json(
            temp,
            "new-task",
            "--project",
            str(project),
            "--title",
            "nested-start",
            "--purpose",
            "queue escalation start in outer transaction",
            "--command-id",
            "CMD-new-nested-start",
        )
        initial_nested = run_json(
            temp,
            "dispatch",
            "--project",
            str(project),
            "--task",
            "V2-0003",
            "--unsafe-native",
            "--command-id",
            "CMD-dispatch-nested-start",
        )
        nested_started = run_json(
            temp,
            "escalate",
            "--project",
            str(project),
            "--task",
            "V2-0003",
            "--reason",
            "exercise nested effect queue",
            "--start",
            "--unsafe-native",
            "--command-id",
            "CMD-escalate-nested-start",
        )
        nested_task = json.loads((project / "tasks" / "V2-0003" / "task.json").read_text(encoding="utf-8"))
        assert len(nested_task["attempts"]) == 2
        assert nested_task["attempts"][-1]["status"] == "launch_pending"
        with sqlite3.connect(project / "scheduler" / "state.db") as connection:
            assert connection.execute(
                "SELECT COUNT(*) FROM effects WHERE command_id='CMD-escalate-nested-start' AND status='pending'"
            ).fetchone()[0] == 1

        store = run_json(temp, "state-store", "--project", str(project))
        assert store["status"] == "ok"
        with sqlite3.connect(project / "scheduler" / "state.db") as connection:
            assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
            assert connection.execute("SELECT COUNT(*) FROM commands WHERE command_id='CMD-dispatch-crash'").fetchone()[0] == 1
            assert connection.execute("SELECT COUNT(*) FROM dirty_views").fetchone()[0] == 0
        assert run_json(temp, "validate", "--project", str(project))["status"] == "ok"
        print("transactional scheduler ok")
        return 0
    finally:
        shutil.rmtree(temp, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
