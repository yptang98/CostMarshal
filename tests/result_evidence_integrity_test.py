"""Authoritative result evidence, profile rotation, and replay recovery."""

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
    completed = subprocess.run(
        [sys.executable, str(CLI), "--root", str(temp / "runtime"), *args],
        text=True,
        encoding="utf-8",
        errors="replace",
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=dict(os.environ),
        check=False,
    )
    if ok and completed.returncode:
        raise AssertionError(f"command failed: {args}\n{completed.stdout}\n{completed.stderr}")
    if not ok and not completed.returncode:
        raise AssertionError(f"command unexpectedly succeeded: {args}\n{completed.stdout}")
    return completed


def run_json(temp: Path, *args: str) -> dict:
    return json.loads(run(temp, *args).stdout)


def main() -> int:
    temp = Path(tempfile.mkdtemp(prefix="costmarshal-result-evidence-"))
    previous_codex_home = os.environ.get("CODEX_HOME")
    try:
        workspace = temp / "workspace"
        workspace.mkdir()
        codex_home = temp / "codex-home"
        codex_home.mkdir()
        profile = codex_home / "longcat.config.toml"
        profile.write_text(
            "\n".join(
                [
                    'model_provider = "longcat"',
                    '[model_providers.longcat]',
                    'name = "longcat"',
                    'base_url = "https://longcat.invalid/v1"',
                    'wire_api = "responses"',
                    'env_key = "LONGCAT_API_KEY"',
                    "",
                ]
            ),
            encoding="utf-8",
        )
        os.environ["CODEX_HOME"] = str(codex_home)

        project = Path(
            run_json(
                temp,
                "init",
                "--name",
                "result-evidence",
                "--objective",
                "bind routing evidence to authoritative attempts",
                "--workspace",
                str(workspace),
                "--governance",
                "off",
                "--allow-unsafe-native-workers",
            )["project"]
        )
        task_id = run_json(
            temp,
            "new-task",
            "--project",
            str(project),
            "--title",
            "accepted evidence",
            "--purpose",
            "exercise exact result binding",
            "--provider",
            "longcat",
        )["task_id"]
        dispatch = run_json(
            temp,
            "dispatch",
            "--project",
            str(project),
            "--task",
            task_id,
            "--provider",
            "longcat",
            "--unsafe-native",
        )
        task_dir = project / "tasks" / task_id
        (task_dir / "completion-report.md").write_text(
            f"# Completion Report: {task_id}\n\nStatus: done\n",
            encoding="utf-8",
        )
        run_json(
            temp,
            "heartbeat",
            "--project",
            str(project),
            "--actor",
            dispatch["actor_id"],
            "--status",
            "waiting",
        )
        run_json(
            temp,
            "collect",
            "--command-id",
            "CMD-evidence-collect",
            "--project",
            str(project),
            "--task",
            task_id,
            "--state",
            "waiting_leader",
        )
        result_args = (
            "record-result",
            "--command-id",
            "CMD-evidence-result",
            "--project",
            str(project),
            "--task",
            task_id,
            "--status",
            "done",
            "--accepted-by-leader",
            "--quality-score",
            "5",
            "--summary",
            "bound acceptance",
        )
        recorded = run_json(temp, *result_args)
        assert recorded["event"]["execution_model"] == "LongCat-2.0"
        replay = run_json(temp, *result_args)
        assert replay["idempotent_replay"] is True
        changed_replay = run(
            temp,
            *result_args[:-2],
            "--summary",
            "different payload",
            ok=False,
        )
        assert "exact idempotent replay" in (changed_replay.stdout + changed_replay.stderr)
        overridden_model = run(
            temp,
            *result_args,
            "--model",
            "forged-model",
            ok=False,
        )
        assert "cannot override" in (overridden_model.stdout + overridden_model.stderr)

        route_before = run_json(
            temp,
            "route",
            "--project",
            str(project),
            "--provider",
            "longcat",
        )
        assert route_before["decision"]["acceptance_prior"]["observations"] == 1

        # Simulate the legacy append-before-task-save crash.  Exact replay must
        # recover the already appended row instead of charging a second result.
        task_file = task_dir / "task.json"
        task = json.loads(task_file.read_text(encoding="utf-8"))
        attempt = task["attempts"][-1]
        for field in (
            "leader_result_id",
            "accepted_by_leader",
            "quality_score",
            "recorded_result_status",
            "result_request_contract_sha256",
        ):
            attempt.pop(field, None)
        attempt["status"] = "waiting_leader"
        task["status"] = "waiting_leader"
        task.pop("leader_result", None)
        task_file.write_text(json.dumps(task, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        (task_dir / "status.json").write_text(
            json.dumps(
                {
                    "schema_version": 2,
                    "task_id": task_id,
                    "state": "waiting_leader",
                    "updated_at": "2026-07-16T00:00:00Z",
                    "error": None,
                },
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
        recovered = run_json(temp, *result_args)
        assert recovered["recovered_orphan_result"] is True
        assert len((project / "reports" / "results.jsonl").read_text(encoding="utf-8").splitlines()) == 1
        assert run_json(temp, "validate", "--project", str(project))["status"] == "ok"

        # Same logical profile name with different bytes is a new execution
        # identity.  Old evidence remains auditable but cannot prove the new
        # route's acceptance probability.
        profile.write_text(profile.read_text(encoding="utf-8") + "# rotated\n", encoding="utf-8")
        route_after = run_json(
            temp,
            "route",
            "--project",
            str(project),
            "--provider",
            "longcat",
        )
        assert route_after["decision"]["acceptance_prior"]["observations"] == 0

        forged = dict(recorded["event"])
        forged["id"] = "RES-forged"
        forged["command_id"] = "CMD-forged"
        forged["attempt_id"] = "ATT-does-not-exist"
        with (project / "reports" / "results.jsonl").open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(forged, ensure_ascii=False, separators=(",", ":")) + "\n")
        still_cold = run_json(
            temp,
            "route",
            "--project",
            str(project),
            "--provider",
            "longcat",
        )
        assert still_cold["decision"]["acceptance_prior"]["observations"] == 0
        invalid = run(temp, "validate", "--project", str(project), ok=False)
        assert "unknown attempt ATT-does-not-exist" in (invalid.stdout + invalid.stderr)

        print("result evidence integrity ok")
        return 0
    finally:
        if previous_codex_home is None:
            os.environ.pop("CODEX_HOME", None)
        else:
            os.environ["CODEX_HOME"] = previous_codex_home
        shutil.rmtree(temp, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
