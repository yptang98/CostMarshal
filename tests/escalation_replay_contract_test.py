#!/usr/bin/env python3
"""JSON scheduler replay repairs an escalation interrupted before dispatch."""

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
sys.path.insert(0, str(ROOT))

from costmarshal_v2.profiles import provider_profile_text  # noqa: E402


COMMAND_ID = "CMD-escalate-recover-missing-successor"
ENV_COMMAND_ID = "CMD-escalate-recover-env-key"
FAULT = "escalation.after_origin_before_successor"


def run(
    temp: Path,
    *args: str,
    ok: bool = True,
    fault: str | None = None,
) -> subprocess.CompletedProcess[str]:
    environment = os.environ.copy()
    environment["COSTMARSHAL_V2_HOME"] = str(temp / "runtime")
    environment.pop("COSTMARSHAL_SCHEDULER_FAULT", None)
    if fault:
        environment["COSTMARSHAL_SCHEDULER_FAULT"] = fault
    result = subprocess.run(
        [sys.executable, str(CLI), "--root", str(temp / "runtime"), *args],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=environment,
        check=False,
    )
    if ok and result.returncode:
        raise AssertionError(f"command failed: {args}\n{result.stdout}\n{result.stderr}")
    if not ok and not result.returncode:
        raise AssertionError(f"command unexpectedly succeeded: {args}\n{result.stdout}")
    return result


def run_json(temp: Path, *args: str) -> dict:
    return json.loads(run(temp, *args).stdout)


def load_task(project: Path, task_id: str = "V2-0001") -> dict:
    return json.loads(
        (project / "tasks" / task_id / "task.json").read_text(encoding="utf-8")
    )


def reject_current_attempt(temp: Path, project: Path, task_id: str, suffix: str) -> None:
    task = load_task(project, task_id)
    attempt = task["attempts"][-1]
    report = project / "tasks" / task_id / "completion-report.md"
    report.write_text(
        f"# Completion Report: {task_id}\n\nStatus: escalate\n",
        encoding="utf-8",
    )
    run_json(
        temp,
        "heartbeat",
        "--project",
        str(project),
        "--actor",
        attempt["actor_id"],
        "--status",
        "waiting",
    )
    run_json(
        temp,
        "collect",
        "--command-id",
        f"CMD-collect-{suffix}",
        "--project",
        str(project),
        "--task",
        task_id,
        "--state",
        "escalate",
    )
    run_json(
        temp,
        "record-result",
        "--command-id",
        f"CMD-result-{suffix}",
        "--project",
        str(project),
        "--task",
        task_id,
        "--status",
        "escalate",
        "--quality-score",
        "3",
    )


def main() -> int:
    temp = Path(tempfile.mkdtemp(prefix="costmarshal-escalation-replay-"))
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
        project = Path(
            run_json(
                temp,
                "init",
                "--name",
                "escalation-replay-contract",
                "--objective",
                "repair a partially committed escalation",
                "--workspace",
                str(workspace),
                "--backend",
                "local",
                "--governance",
                "off",
                "--allow-unsafe-native-workers",
            )["project"]
        )
        assert not (project / "scheduler" / "state-backend.json").exists()

        run_json(
            temp,
            "new-task",
            "--project",
            str(project),
            "--title",
            "recover escalation",
            "--purpose",
            "exercise non-SQLite escalation replay",
            "--estimated-input-tokens",
            "1000",
            "--estimated-output-tokens",
            "1000",
            "--provider",
            "longcat",
        )
        run_json(
            temp,
            "dispatch",
            "--project",
            str(project),
            "--task",
            "V2-0001",
            "--provider",
            "longcat",
            "--unsafe-native",
        )
        reject_current_attempt(temp, project, "V2-0001", "first")

        crashed = run(
            temp,
            "escalate",
            "--project",
            str(project),
            "--task",
            "V2-0001",
            "--reason",
            "recover missing successor",
            "--command-id",
            COMMAND_ID,
            "--unsafe-native",
            ok=False,
            fault=FAULT,
        )
        assert crashed.returncode == 96, (crashed.stdout, crashed.stderr)

        interrupted = load_task(project)
        assert len(interrupted["attempts"]) == 1
        origin = interrupted["attempts"][0]
        assert origin["status"] == "escalated"
        assert origin["escalation_command_id"] == COMMAND_ID
        assert origin["escalation_reason"] == "recover missing successor"
        assert origin["escalation_target_provider"] == "deepseek"
        assert not [
            row
            for row in interrupted["attempts"]
            if row.get("dispatch_command_id") == COMMAND_ID
        ]

        # A crash replay is bound to the exact successor admission prepared
        # before the origin was terminalized, not merely the same raw CLI args.
        project_json = project / "project.json"
        original_project = json.loads(project_json.read_text(encoding="utf-8"))
        drifted_project = json.loads(json.dumps(original_project))
        for provider in drifted_project["provider_catalog"]["providers"]:
            if provider["provider_id"] == "deepseek":
                provider["model"] = "drifted-after-origin"
        project_json.write_text(
            json.dumps(drifted_project, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        drifted_replay = run(
            temp,
            "escalate",
            "--project",
            str(project),
            "--task",
            "V2-0001",
            "--reason",
            "recover missing successor",
            "--command-id",
            COMMAND_ID,
            "--unsafe-native",
            ok=False,
        )
        assert "successor admission drifted" in (
            drifted_replay.stdout + drifted_replay.stderr
        )

        # Worker isolation is part of the prepared execution boundary too.
        isolation_drifted_project = json.loads(json.dumps(original_project))
        isolation_drifted_project["worker_isolation"]["limits"]["memory_mb"] = 4096
        project_json.write_text(
            json.dumps(isolation_drifted_project, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        isolation_drifted_replay = run(
            temp,
            "escalate",
            "--project",
            str(project),
            "--task",
            "V2-0001",
            "--reason",
            "recover missing successor",
            "--command-id",
            COMMAND_ID,
            "--unsafe-native",
            ok=False,
        )
        assert "successor admission drifted" in (
            isolation_drifted_replay.stdout + isolation_drifted_replay.stderr
        )
        project_json.write_text(
            json.dumps(original_project, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )

        recovered = run_json(
            temp,
            "escalate",
            "--project",
            str(project),
            "--task",
            "V2-0001",
            "--reason",
            "recover missing successor",
            "--command-id",
            COMMAND_ID,
            "--unsafe-native",
        )
        repaired = load_task(project)
        origins = [
            row
            for row in repaired["attempts"]
            if row.get("escalation_command_id") == COMMAND_ID
        ]
        successors = [
            row
            for row in repaired["attempts"]
            if row.get("dispatch_command_id") == COMMAND_ID
        ]
        assert len(repaired["attempts"]) == 2
        assert len(origins) == 1
        assert len(successors) == 1
        assert successors[0]["provider"] == "deepseek"
        assert recovered["actor_id"] == successors[0]["actor_id"]

        replay = run_json(
            temp,
            "escalate",
            "--project",
            str(project),
            "--task",
            "V2-0001",
            "--reason",
            "recover missing successor",
            "--command-id",
            COMMAND_ID,
            "--unsafe-native",
        )
        assert replay["idempotent_replay"] is True
        assert replay["attempt_id"] == successors[0]["attempt_id"]
        changed_reason = run(
            temp,
            "escalate",
            "--project",
            str(project),
            "--task",
            "V2-0001",
            "--reason",
            "different replay payload",
            "--command-id",
            COMMAND_ID,
            "--unsafe-native",
            ok=False,
        )
        assert "different request payload" in (changed_reason.stdout + changed_reason.stderr)
        changed_target = run(
            temp,
            "escalate",
            "--project",
            str(project),
            "--task",
            "V2-0001",
            "--provider",
            "codex",
            "--reason",
            "recover missing successor",
            "--command-id",
            COMMAND_ID,
            "--unsafe-native",
            ok=False,
        )
        assert "different request payload" in (changed_target.stdout + changed_target.stderr)
        final_task = load_task(project)
        assert len(final_task["attempts"]) == 2
        assert len(
            [
                row
                for row in final_task["attempts"]
                if row.get("dispatch_command_id") == COMMAND_ID
            ]
        ) == 1

        # A provider without a named profile still binds its credential
        # selector through the prepared admission.
        run_json(
            temp,
            "new-task",
            "--project",
            str(project),
            "--title",
            "recover credential identity",
            "--purpose",
            "bind successor env key across crash replay",
            "--estimated-input-tokens",
            "1000",
            "--estimated-output-tokens",
            "1000",
            "--provider",
            "deepseek",
        )
        run_json(
            temp,
            "dispatch",
            "--project",
            str(project),
            "--task",
            "V2-0002",
            "--provider",
            "deepseek",
            "--unsafe-native",
        )
        reject_current_attempt(temp, project, "V2-0002", "second")
        env_crashed = run(
            temp,
            "escalate",
            "--project",
            str(project),
            "--task",
            "V2-0002",
            "--reason",
            "recover credential identity",
            "--command-id",
            ENV_COMMAND_ID,
            "--unsafe-native",
            ok=False,
            fault=FAULT,
        )
        assert env_crashed.returncode == 96, (env_crashed.stdout, env_crashed.stderr)
        env_original_project = json.loads(project_json.read_text(encoding="utf-8"))
        env_drifted_project = json.loads(json.dumps(env_original_project))
        for provider in env_drifted_project["provider_catalog"]["providers"]:
            if provider["provider_id"] == "codex":
                provider["env_key"] = "DRIFTED_API_KEY"
        project_json.write_text(
            json.dumps(env_drifted_project, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        env_drifted_replay = run(
            temp,
            "escalate",
            "--project",
            str(project),
            "--task",
            "V2-0002",
            "--reason",
            "recover credential identity",
            "--command-id",
            ENV_COMMAND_ID,
            "--unsafe-native",
            ok=False,
        )
        assert "successor admission drifted" in (
            env_drifted_replay.stdout + env_drifted_replay.stderr
        )
        project_json.write_text(
            json.dumps(env_original_project, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        env_recovered = run_json(
            temp,
            "escalate",
            "--project",
            str(project),
            "--task",
            "V2-0002",
            "--reason",
            "recover credential identity",
            "--command-id",
            ENV_COMMAND_ID,
            "--unsafe-native",
        )
        env_task = load_task(project, "V2-0002")
        env_successors = [
            row
            for row in env_task["attempts"]
            if row.get("dispatch_command_id") == ENV_COMMAND_ID
        ]
        assert len(env_successors) == 1
        assert env_successors[0]["provider"] == "codex"
        assert env_recovered["actor_id"] == env_successors[0]["actor_id"]

        print("escalation replay contract test passed")
        return 0
    finally:
        shutil.rmtree(temp, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
