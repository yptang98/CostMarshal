#!/usr/bin/env python3
"""Dispatch spawn effects survive scheduler hard exits without duplicate provider calls."""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
CLI = ROOT / "scripts" / "costmarshal.py"


def run(
    temp: Path,
    *args: str,
    env_extra: dict[str, str] | None = None,
    ok: bool = True,
) -> subprocess.CompletedProcess[str]:
    environment = os.environ.copy()
    environment["COSTMARSHAL_V2_HOME"] = str(temp / "runtime")
    environment.update(env_extra or {})
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
    return result


def run_json(temp: Path, *args: str, env_extra: dict[str, str] | None = None) -> dict:
    return json.loads(run(temp, *args, env_extra=env_extra).stdout)


def wait_for_count(counter: Path, expected: int, timeout: float = 15.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        rows = counter.read_text(encoding="utf-8").splitlines() if counter.is_file() else []
        if len(rows) >= expected:
            return
        time.sleep(0.1)
    raise AssertionError(f"provider call count did not reach {expected}")


def wait_for_status(project: Path, task_id: str, expected: str, timeout: float = 15.0) -> None:
    path = project / "tasks" / task_id / "status.json"
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if path.is_file():
            try:
                if json.loads(path.read_text(encoding="utf-8"))["state"] == expected:
                    return
            except (KeyError, json.JSONDecodeError):
                pass
        time.sleep(0.1)
    raise AssertionError(f"{task_id} status did not reach {expected}")


def wait_for_actor_started(project: Path, actor_id: str, timeout: float = 15.0) -> None:
    path = project / "scheduler" / "actors" / f"{actor_id}.json"
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        actor = json.loads(path.read_text(encoding="utf-8"))
        if (actor.get("runtime") or {}).get("provider_execution_state") == "started":
            return
        time.sleep(0.1)
    raise AssertionError(f"{actor_id} did not register provider execution")


def main() -> int:
    temp = Path(tempfile.mkdtemp(prefix="costmarshal-v2-runtime-effect-scheduler-"))
    try:
        workspace = temp / "workspace"
        workspace.mkdir()
        counter = temp / "provider-count.txt"
        fake = temp / "fake_codex.py"
        fake.write_text(
            "\n".join(
                [
                    "import json, pathlib, sys",
                    f"counter = pathlib.Path({str(counter)!r})",
                    "with counter.open('a', encoding='utf-8') as handle: handle.write('once\\n')",
                    "output = pathlib.Path(sys.argv[sys.argv.index('--output-last-message') + 1])",
                    "output.write_text('# Completion Report\\n\\nStatus: done\\n\\n## Result\\neffect-safe\\n', encoding='utf-8')",
                    "print(json.dumps({'usage': {'input_tokens': 4, 'output_tokens': 2}}))",
                ]
            )
            + "\n",
            encoding="utf-8",
        )
        provider_env = {"COSTMARSHAL_CODEX_COMMAND_JSON": json.dumps([sys.executable, str(fake)])}
        project = Path(
            run_json(
                temp,
                "init",
                "--name",
                "runtime-effects",
                "--objective",
                "recover external spawn effects",
                "--workspace",
                str(workspace),
                "--backend",
                "local",
                "--governance",
                "off",
                "--allow-unsafe-native-workers",
            )["project"]
        )
        run_json(temp, "new-task", "--project", str(project), "--title", "normal", "--purpose", "spawn once")
        run_json(temp, "migrate-state", "--project", str(project), "--apply")
        queued = run_json(
            temp,
            "dispatch",
            "--project",
            str(project),
            "--task",
            "V2-0001",
            "--start",
            "--unsafe-native",
            "--command-id",
            "CMD-SPAWN-NORMAL",
        )
        assert queued["started"] is False and queued["start_queued"] is True
        assert queued["start"]["status"] == "queued"
        assert not counter.exists()
        cycle = run_json(temp, "run-scheduler", "--project", str(project), "--once", env_extra=provider_env)
        assert cycle["processed_effects"] == 1
        wait_for_count(counter, 1)

        run_json(temp, "new-task", "--project", str(project), "--title", "crash", "--purpose", "recover spawn")
        crashed_queue = run_json(
            temp,
            "dispatch",
            "--project",
            str(project),
            "--task",
            "V2-0002",
            "--start",
            "--unsafe-native",
            "--command-id",
            "CMD-SPAWN-CRASH",
        )
        crash_env = {**provider_env, "COSTMARSHAL_SCHEDULER_FAULT": "effect.after_spawn_before_observe"}
        crashed = run(
            temp,
            "run-scheduler",
            "--project",
            str(project),
            "--once",
            env_extra=crash_env,
            ok=False,
        )
        assert crashed.returncode == 96
        time.sleep(2.8)
        recovered = run_json(temp, "run-scheduler", "--project", str(project), "--once", env_extra=provider_env)
        recovered_effects = recovered["last_effects"]["processed"]
        assert len(recovered_effects) == 1
        assert recovered_effects[0]["effect_id"] == crashed_queue["start"]["effect_id"]
        assert recovered_effects[0]["source"] in {"runner_registration", "backend_start"}
        wait_for_count(counter, 2)
        wait_for_status(project, "V2-0002", "waiting_leader")
        run_json(temp, "run-scheduler", "--project", str(project), "--once", env_extra=provider_env)
        time.sleep(0.3)
        assert counter.read_text(encoding="utf-8").splitlines() == ["once", "once"]

        replay = run_json(
            temp,
            "dispatch",
            "--project",
            str(project),
            "--task",
            "V2-0002",
            "--start",
            "--unsafe-native",
            "--command-id",
            "CMD-SPAWN-CRASH",
        )
        assert replay == crashed_queue
        with sqlite3.connect(project / "scheduler" / "state.db") as connection:
            statuses = dict(connection.execute("SELECT command_id, status FROM commands WHERE command_id LIKE 'CMD-SPAWN-%'"))
            effect_rows = list(connection.execute("SELECT effect_id, status FROM effects ORDER BY effect_id"))
        assert statuses == {"CMD-SPAWN-CRASH": "completed", "CMD-SPAWN-NORMAL": "completed"}
        assert all(status == "applied" for _, status in effect_rows)
        assert run_json(temp, "validate", "--project", str(project))["status"] == "ok"

        long_fake = temp / "long_fake_codex.py"
        long_fake.write_text(
            "\n".join(
                [
                    "import pathlib, sys, time",
                    f"counter = pathlib.Path({str(counter)!r})",
                    "with counter.open('a', encoding='utf-8') as handle: handle.write('once\\n')",
                    "time.sleep(60)",
                    "output = pathlib.Path(sys.argv[sys.argv.index('--output-last-message') + 1])",
                    "output.write_text('# Completion Report\\n\\nStatus: done\\n', encoding='utf-8')",
                ]
            )
            + "\n",
            encoding="utf-8",
        )
        long_env = {"COSTMARSHAL_CODEX_COMMAND_JSON": json.dumps([sys.executable, str(long_fake)])}
        run_json(temp, "new-task", "--project", str(project), "--title", "stop", "--purpose", "recover stop")
        stop_target = run_json(
            temp,
            "dispatch",
            "--project",
            str(project),
            "--task",
            "V2-0003",
            "--start",
            "--unsafe-native",
            "--command-id",
            "CMD-SPAWN-STOP",
        )
        run_json(temp, "run-scheduler", "--project", str(project), "--once", env_extra=long_env)
        wait_for_count(counter, 3)
        wait_for_actor_started(project, stop_target["actor_id"])
        stop_queued = run_json(
            temp,
            "stop-actor",
            "--project",
            str(project),
            "--actor",
            stop_target["actor_id"],
            "--stop-runtime",
            "--reason",
            "fault injection",
            "--command-id",
            "CMD-STOP-CRASH",
        )
        assert stop_queued["runtime"]["status"] == "queued"
        stop_crash = run(
            temp,
            "run-scheduler",
            "--project",
            str(project),
            "--once",
            env_extra={"COSTMARSHAL_SCHEDULER_FAULT": "effect.after_stop_before_observe"},
            ok=False,
        )
        assert stop_crash.returncode == 96
        time.sleep(2.8)
        stop_recovered = run_json(temp, "run-scheduler", "--project", str(project), "--once")
        assert stop_recovered["processed_effects"] == 1
        assert stop_recovered["last_effects"]["processed"][0]["source"] == "already_stopped"
        time.sleep(0.3)
        assert counter.read_text(encoding="utf-8").splitlines() == ["once", "once", "once"]
        stopped_actor = json.loads(
            (project / "scheduler" / "actors" / f"{stop_target['actor_id']}.json").read_text(encoding="utf-8")
        )
        assert stopped_actor["status"] == "stopped"
        with sqlite3.connect(project / "scheduler" / "state.db") as connection:
            assert connection.execute(
                "SELECT status FROM commands WHERE command_id='CMD-STOP-CRASH'"
            ).fetchone()[0] == "completed"
            assert connection.execute(
                "SELECT status FROM effects WHERE effect_id=?",
                (stop_queued["runtime"]["effect_id"],),
            ).fetchone()[0] == "applied"
            orphan_effects = int(
                connection.execute(
                    "SELECT COUNT(*) FROM effects WHERE status NOT IN ('applied', 'dead')"
                ).fetchone()[0]
            )

        run_json(
            temp,
            "new-task",
            "--project",
            str(project),
            "--title",
            "dirty compatibility view",
            "--purpose",
            "recover committed effect without replaying dispatch",
        )
        dirty_dispatch = run(
            temp,
            "dispatch",
            "--project",
            str(project),
            "--task",
            "V2-0004",
            "--start",
            "--unsafe-native",
            "--command-id",
            "CMD-SPAWN-DIRTY-VIEW",
            env_extra={
                **provider_env,
                "COSTMARSHAL_CONTROL_STORE_FAULT": "transaction.after_commit_before_materialize",
            },
            ok=False,
        )
        assert dirty_dispatch.returncode == 86
        provider_calls_before_dirty = len(counter.read_text(encoding="utf-8").splitlines())
        assert provider_calls_before_dirty == 3
        with sqlite3.connect(project / "scheduler" / "state.db") as connection:
            dirty_effect = connection.execute(
                "SELECT effect_id, status, payload_json FROM effects "
                "WHERE command_id='CMD-SPAWN-DIRTY-VIEW'"
            ).fetchone()
            assert dirty_effect is not None
            assert dirty_effect[1] == "pending"
            dirty_actor_id = json.loads(dirty_effect[2])["actor_id"]
            assert connection.execute(
                "SELECT status FROM commands WHERE command_id='CMD-SPAWN-DIRTY-VIEW'"
            ).fetchone()[0] == "awaiting_effect"
        assert not (project / "scheduler" / "actors" / f"{dirty_actor_id}.json").exists()

        # Do not replay dispatch: one scheduler cycle must reconcile the
        # committed DB state, run the provider once, and complete the effect.
        dirty_recovered = run_json(
            temp,
            "run-scheduler",
            "--project",
            str(project),
            "--once",
            env_extra=provider_env,
        )
        assert dirty_recovered["processed_effects"] == 1
        assert dirty_recovered["failed_effects"] == 0
        assert dirty_recovered["last_effects"]["processed"][0]["effect_id"] == dirty_effect[0]
        wait_for_count(counter, 4)
        with sqlite3.connect(project / "scheduler" / "state.db") as connection:
            assert connection.execute(
                "SELECT status FROM effects WHERE effect_id=?", (dirty_effect[0],)
            ).fetchone()[0] == "applied"
            assert connection.execute(
                "SELECT status FROM commands WHERE command_id='CMD-SPAWN-DIRTY-VIEW'"
            ).fetchone()[0] == "completed"
            orphan_effects = int(
                connection.execute(
                    "SELECT COUNT(*) FROM effects WHERE status NOT IN ('applied', 'dead')"
                ).fetchone()[0]
            )
        provider_calls = len(counter.read_text(encoding="utf-8").splitlines())
        assert provider_calls == 4
        assert provider_calls - provider_calls_before_dirty == 1
        assert orphan_effects == 0
        print("runtime effect scheduler ok")
        print(
            "COSTMARSHAL_RUNTIME_EVIDENCE="
            + json.dumps(
                {
                    "schema_version": 1,
                    "test": "tests/runtime_effect_scheduler_test.py",
                    "crash_points": [
                        "effect.after_spawn_before_observe",
                        "effect.after_stop_before_observe",
                        "transaction.after_commit_before_materialize",
                    ],
                    "recovery_scenarios": [],
                    "provider_calls": provider_calls,
                    "expected_provider_calls": 4,
                    "orphan_effects": orphan_effects,
                },
                sort_keys=True,
            )
        )
        return 0
    finally:
        shutil.rmtree(temp, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
