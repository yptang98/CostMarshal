#!/usr/bin/env python3
"""Dispatch spawn effects survive scheduler hard exits without duplicate provider calls."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
CLI = ROOT / "scripts" / "costmarshal.py"
sys.path.insert(0, str(ROOT))

from costmarshal_v2.control_store import control_transaction, effect_status  # noqa: E402
from costmarshal_v2.locking import scheduler_daemon_lock  # noqa: E402
from costmarshal_v2.paths import resolve_project  # noqa: E402
from costmarshal_v2.session_backend import pid_is_alive  # noqa: E402
from costmarshal_v2.state import load_actor  # noqa: E402
import costmarshal_v2.scheduler as scheduler_module  # noqa: E402


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


def wait_for_count(
    counter: Path,
    expected: int,
    timeout: float = 15.0,
    *,
    project: Path | None = None,
    actor_id: str | None = None,
) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        rows = counter.read_text(encoding="utf-8").splitlines() if counter.is_file() else []
        if len(rows) >= expected:
            return
        if project is not None and actor_id is not None:
            actor_path = project / "scheduler" / "actors" / f"{actor_id}.json"
            if actor_path.is_file():
                try:
                    actor = json.loads(actor_path.read_text(encoding="utf-8"))
                    runtime = actor.get("runtime") or {}
                    pid = runtime.get("pid")
                    provider_state = runtime.get("provider_execution_state")
                    if pid and provider_state != "finished" and not pid_is_alive(int(pid)):
                        log_path = runtime.get("log_path")
                        transcript = (
                            project / str(log_path)
                            if log_path
                            else project / "transcripts" / f"{actor_id}.log"
                        )
                        transcript_tail = (
                            transcript.read_text(encoding="utf-8", errors="replace")[-4000:]
                            if transcript.is_file()
                            else "<missing>"
                        )
                        raise AssertionError(
                            f"actor {actor_id} exited before provider call {expected}; "
                            f"runtime={runtime!r}; transcript_tail={transcript_tail!r}"
                        )
                except (OSError, ValueError, json.JSONDecodeError):
                    pass
        time.sleep(0.1)
    raise AssertionError(f"provider call count did not reach {expected}")


def wait_for_process_exit(pid: int, timeout: float = 15.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not pid_is_alive(pid):
            return
        time.sleep(0.1)
    raise AssertionError(f"runtime process {pid} did not stop")


def tree_digest(root: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted([root, *root.rglob("*")], key=lambda item: item.relative_to(root).as_posix()):
        relative = path.relative_to(root).as_posix() or "."
        if path.is_symlink():
            kind = b"symlink"
            payload = os.fsencode(os.readlink(path))
        elif path.is_file():
            try:
                payload = path.read_bytes()
                kind = b"file"
            except OSError:
                # A live actor owns an exclusive runtime lock on Windows. Its
                # stable path/size still lets this regression detect any new
                # project mutation without trying to break that safety lock.
                kind = b"locked-file"
                payload = str(path.stat().st_size).encode("ascii")
        else:
            kind = b"directory"
            payload = b""
        digest.update(os.fsencode(relative) + b"\0" + kind + b"\0" + payload)
    return digest.hexdigest()


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

        no_effect_marker = "recover-no-effect-heartbeat"
        no_effect_crash = run(
            temp,
            "send",
            "--project",
            str(project),
            "--to",
            "scheduler",
            "--sender",
            "leader",
            "--subject",
            "scheduler.command",
            "--message",
            json.dumps(
                {
                    "command": "heartbeat",
                    "args": {
                        "actor": "leader",
                        "status": "running",
                        "note": no_effect_marker,
                    },
                }
            ),
            "--command-id",
            "CMD-NO-EFFECT-DIRTY-VIEW",
            env_extra={
                "COSTMARSHAL_CONTROL_STORE_FAULT": "transaction.after_commit_before_materialize",
            },
            ok=False,
        )
        assert no_effect_crash.returncode == 86
        scheduler_inbox = project / "scheduler" / "mailboxes" / "scheduler" / "inbox.jsonl"
        assert no_effect_marker not in scheduler_inbox.read_text(encoding="utf-8")
        with sqlite3.connect(project / "scheduler" / "state.db") as connection:
            assert connection.execute("SELECT COUNT(*) FROM effects").fetchone()[0] == 0
            assert connection.execute(
                "SELECT status FROM commands WHERE command_id='CMD-NO-EFFECT-DIRTY-VIEW'"
            ).fetchone()[0] == "completed"
            assert connection.execute("SELECT COUNT(*) FROM dirty_views").fetchone()[0] > 0

        # Do not replay send and do not run a repair command. One ordinary
        # scheduler cycle must materialize and execute the committed command.
        no_effect_recovered = run_json(
            temp,
            "run-scheduler",
            "--project",
            str(project),
            "--once",
        )
        assert no_effect_recovered["processed_effects"] == 0
        assert no_effect_recovered["processed_commands"] == 1
        assert no_effect_recovered["failed_commands"] == 0
        assert no_effect_marker in scheduler_inbox.read_text(encoding="utf-8")
        leader = json.loads(
            (project / "scheduler" / "actors" / "leader.json").read_text(encoding="utf-8")
        )
        assert leader["heartbeat_note"] == no_effect_marker
        with sqlite3.connect(project / "scheduler" / "state.db") as connection:
            assert connection.execute("SELECT COUNT(*) FROM effects").fetchone()[0] == 0
            assert connection.execute("SELECT COUNT(*) FROM dirty_views").fetchone()[0] == 0

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
        wait_for_count(counter, 1, project=project, actor_id=queued["actor_id"])
        wait_for_status(project, "V2-0001", "waiting_leader")

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
        wait_for_count(counter, 2, project=project, actor_id=crashed_queue["actor_id"])
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
        wait_for_count(counter, 3, project=project, actor_id=stop_target["actor_id"])
        wait_for_actor_started(project, stop_target["actor_id"])
        stop_crash = run(
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
            env_extra={"COSTMARSHAL_SCHEDULER_FAULT": "effect.after_stop_before_observe"},
            ok=False,
        )
        assert stop_crash.returncode == 96
        time.sleep(2.8)
        stop_recovered = run_json(
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
        assert stop_recovered["runtime"]["status"] == "applied"
        assert stop_recovered["runtime"]["emergency_stop"] is True
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
                (stop_recovered["runtime"]["effect_id"],),
            ).fetchone()[0] == "applied"
            stop_observation = json.loads(
                connection.execute(
                    "SELECT observation_json FROM effects WHERE effect_id=?",
                    (stop_recovered["runtime"]["effect_id"],),
                ).fetchone()[0]
            )
            assert stop_observation["source"] == "already_stopped"
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
        wait_for_count(counter, 4, project=project, actor_id=dirty_actor_id)
        wait_for_status(project, "V2-0004", "waiting_leader")
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
        main_provider_calls = len(counter.read_text(encoding="utf-8").splitlines())
        assert main_provider_calls == 4
        assert main_provider_calls - provider_calls_before_dirty == 1
        assert orphan_effects == 0

        slow_workspace = temp / "slow-stop-workspace"
        slow_workspace.mkdir()
        slow_project = Path(
            run_json(
                temp,
                "init",
                "--name",
                "slow-stop-lease",
                "--objective",
                "renew stop leases across external I/O",
                "--workspace",
                str(slow_workspace),
                "--backend",
                "local",
                "--governance",
                "off",
            )["project"]
        )
        slow_actor_path = slow_project / "scheduler" / "actors" / "leader.json"
        slow_actor_seed = json.loads(slow_actor_path.read_text(encoding="utf-8"))
        slow_actor_seed["runtime"].update(
            {
                "target": "pid:999999",
                "pid": 999999,
                "process_start_marker": "slow-stop-marker",
            }
        )
        slow_actor_path.write_text(
            json.dumps(slow_actor_seed, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        run_json(temp, "migrate-state", "--project", str(slow_project), "--apply")
        slow_layout = resolve_project(temp / "runtime", str(slow_project))
        slow_actor = load_actor(slow_layout, "leader")
        slow_effect_id = "EFF-STOP-CMD-SLOW-STOP"
        with control_transaction(
            slow_layout,
            command_name="command_stop_actor",
            command_id="CMD-SLOW-STOP",
            payload={"actor": "leader", "stop_runtime": True},
        ) as transaction:
            transaction.queue_effect(
                effect_id=slow_effect_id,
                effect_type=scheduler_module.STOP_EFFECT_TYPE,
                aggregate_id="leader",
                generation=1,
                payload=scheduler_module._stop_effect_payload(slow_actor, reason="slow audit"),
            )
            transaction.set_result({"effect_id": slow_effect_id})

        stop_started = threading.Event()
        stop_calls: list[str] = []

        class SlowStopBackend:
            kind = "local"

            def available(self) -> bool:
                return True

            def actor_alive(self, **kwargs: object) -> bool:
                return True

            def stop_actor(self, **kwargs: object) -> dict[str, str]:
                stop_calls.append(threading.current_thread().name)
                stop_started.set()
                time.sleep(3.25)
                return {"status": "stopped"}

        original_backend_from_session = scheduler_module.backend_from_session
        scheduler_module.backend_from_session = lambda session: SlowStopBackend()
        slow_results: list[dict] = []
        slow_errors: list[BaseException] = []

        def normal_stop_drainer() -> None:
            try:
                slow_results.append(
                    scheduler_module.process_runtime_effects(
                        slow_layout,
                        limit=1,
                        _governance_prevalidated=True,
                    )
                )
            except BaseException as exc:
                slow_errors.append(exc)

        def emergency_stop_drainer() -> None:
            try:
                slow_results.append(
                    scheduler_module._drain_emergency_stop_effect(
                        slow_layout,
                        effect_id=slow_effect_id,
                    )
                )
            except BaseException as exc:
                slow_errors.append(exc)

        normal_thread = threading.Thread(target=normal_stop_drainer, name="normal-drainer")
        emergency_thread = threading.Thread(target=emergency_stop_drainer, name="emergency-drainer")
        try:
            normal_thread.start()
            assert stop_started.wait(5)
            emergency_thread.start()
            normal_thread.join(10)
            emergency_thread.join(10)
        finally:
            scheduler_module.backend_from_session = original_backend_from_session
        assert not normal_thread.is_alive() and not emergency_thread.is_alive()
        assert slow_errors == []
        assert stop_calls == ["normal-drainer"]
        assert effect_status(slow_layout, slow_effect_id)["status"] == "applied"
        with sqlite3.connect(slow_project / "scheduler" / "state.db") as connection:
            assert connection.execute(
                "SELECT status FROM commands WHERE command_id='CMD-SLOW-STOP'"
            ).fetchone()[0] == "completed"

        daemon_acquired = threading.Event()
        daemon_release = threading.Event()

        def hold_daemon_lock() -> None:
            # Model a daemon sleeping for an arbitrarily long poll interval.
            # Its lifetime lock must not block the short runtime-effect mutex.
            with scheduler_daemon_lock(slow_layout, timeout_seconds=5):
                daemon_acquired.set()
                daemon_release.wait(10)

        daemon_thread = threading.Thread(target=hold_daemon_lock, name="existing-daemon")
        daemon_thread.start()
        assert daemon_acquired.wait(5)
        daemon_stop = run_json(
            temp,
            "stop-actor",
            "--project",
            str(slow_project),
            "--actor",
            "leader",
            "--stop-runtime",
            "--reason",
            "existing daemon owns drainer",
            "--command-id",
            "CMD-DAEMON-LONG-INTERVAL-STOP",
        )
        assert daemon_stop["runtime"]["status"] == "applied"
        assert daemon_stop["runtime"]["drain_deferred"] is False
        assert daemon_stop["runtime"]["effect_status"] == "applied"
        assert effect_status(slow_layout, daemon_stop["runtime"]["effect_id"])["status"] == "applied"
        assert daemon_thread.is_alive(), "the sleeping daemon must still own its lifetime lock"
        with sqlite3.connect(slow_project / "scheduler" / "state.db") as connection:
            assert connection.execute(
                "SELECT status, attempts FROM effects WHERE effect_id=?",
                (daemon_stop["runtime"]["effect_id"],),
            ).fetchone() == ("applied", 1)
            assert connection.execute(
                "SELECT status FROM commands WHERE command_id='CMD-DAEMON-LONG-INTERVAL-STOP'"
            ).fetchone()[0] == "completed"
        daemon_release.set()
        daemon_thread.join(5)
        assert not daemon_thread.is_alive()
        daemon_cycle = scheduler_module.process_runtime_effects(
            slow_layout,
            limit=1,
            _governance_prevalidated=True,
        )
        assert daemon_cycle["processed"] == []
        assert effect_status(slow_layout, daemon_stop["runtime"]["effect_id"])["status"] == "applied"

        emergency_workspace = temp / "emergency-workspace"
        emergency_workspace.mkdir()
        emergency_counter = temp / "emergency-provider-count.txt"
        emergency_fake = temp / "emergency_fake_codex.py"
        emergency_fake.write_text(
            "\n".join(
                [
                    "import pathlib, sys, time",
                    f"counter = pathlib.Path({str(emergency_counter)!r})",
                    "with counter.open('a', encoding='utf-8') as handle: handle.write('once\\n')",
                    "time.sleep(120)",
                    "output = pathlib.Path(sys.argv[sys.argv.index('--output-last-message') + 1])",
                    "output.write_text('# Completion Report\\n\\nStatus: done\\n', encoding='utf-8')",
                ]
            )
            + "\n",
            encoding="utf-8",
        )
        emergency_env = {
            "COSTMARSHAL_CODEX_COMMAND_JSON": json.dumps([sys.executable, str(emergency_fake)])
        }
        emergency_project = Path(
            run_json(
                temp,
                "init",
                "--name",
                "emergency-stop",
                "--objective",
                "stop providers even after governance drift",
                "--workspace",
                str(emergency_workspace),
                "--backend",
                "local",
                "--governance",
                "off",
                "--allow-unsafe-native-workers",
            )["project"]
        )
        run_json(
            temp,
            "new-task",
            "--project",
            str(emergency_project),
            "--title",
            "running provider",
            "--purpose",
            "exercise emergency stop",
        )
        run_json(
            temp,
            "new-task",
            "--project",
            str(emergency_project),
            "--title",
            "blocked spawn",
            "--purpose",
            "prove stale governance never starts pending work",
        )
        run_json(temp, "migrate-state", "--project", str(emergency_project), "--apply")
        emergency_running = run_json(
            temp,
            "dispatch",
            "--project",
            str(emergency_project),
            "--task",
            "V2-0001",
            "--start",
            "--unsafe-native",
            "--command-id",
            "CMD-EMERGENCY-SPAWN-RUNNING",
        )
        run_json(
            temp,
            "run-scheduler",
            "--project",
            str(emergency_project),
            "--once",
            env_extra=emergency_env,
        )
        wait_for_count(
            emergency_counter,
            1,
            project=emergency_project,
            actor_id=emergency_running["actor_id"],
        )
        wait_for_actor_started(emergency_project, emergency_running["actor_id"])
        running_actor = json.loads(
            (
                emergency_project
                / "scheduler"
                / "actors"
                / f"{emergency_running['actor_id']}.json"
            ).read_text(encoding="utf-8")
        )
        running_pid = int((running_actor.get("runtime") or {})["pid"])
        assert pid_is_alive(running_pid)
        blocked_spawn = run_json(
            temp,
            "dispatch",
            "--project",
            str(emergency_project),
            "--task",
            "V2-0002",
            "--start",
            "--unsafe-native",
            "--command-id",
            "CMD-EMERGENCY-SPAWN-BLOCKED",
        )

        project_file = emergency_project / "project.json"
        project_payload = json.loads(project_file.read_text(encoding="utf-8"))
        project_payload["governance"] = {
            "mode": "required",
            "status": "ready",
            "ready": True,
            "binding": {},
            "wrapper_path": str(temp / "missing-archmarshal-wrapper.py"),
        }
        project_file.write_text(
            json.dumps(project_payload, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )

        before_blocked_scheduler = tree_digest(emergency_project)
        blocked_scheduler = run(
            temp,
            "run-scheduler",
            "--project",
            str(emergency_project),
            "--once",
            env_extra=emergency_env,
            ok=False,
        )
        assert blocked_scheduler.returncode != 0
        assert "governance gate blocked" in blocked_scheduler.stderr.lower()
        assert tree_digest(emergency_project) == before_blocked_scheduler
        assert emergency_counter.read_text(encoding="utf-8").splitlines() == ["once"]

        # Even if the authoritative governance document becomes unreadable,
        # an explicit SQLite STOP remains a fail-safe STOP-only operation.
        # The already queued SPAWN must remain untouched.
        project_file.write_text("{corrupt-project-json\n", encoding="utf-8")

        emergency_stop_crash = run(
            temp,
            "stop-actor",
            "--project",
            str(emergency_project),
            "--actor",
            emergency_running["actor_id"],
            "--stop-runtime",
            "--reason",
            "governance drift emergency",
            "--command-id",
            "CMD-EMERGENCY-STOP-CRASH",
            env_extra={"COSTMARSHAL_SCHEDULER_FAULT": "effect.after_stop_observe_before_apply"},
            ok=False,
        )
        assert emergency_stop_crash.returncode == 96
        wait_for_process_exit(running_pid)
        with sqlite3.connect(emergency_project / "scheduler" / "state.db") as connection:
            assert connection.execute(
                "SELECT status FROM effects WHERE command_id='CMD-EMERGENCY-STOP-CRASH'"
            ).fetchone()[0] == "observed"
        time.sleep(2.8)
        emergency_stopped = run_json(
            temp,
            "stop-actor",
            "--project",
            str(emergency_project),
            "--actor",
            emergency_running["actor_id"],
            "--stop-runtime",
            "--reason",
            "governance drift emergency",
            "--command-id",
            "CMD-EMERGENCY-STOP-CRASH",
        )
        assert emergency_stopped["runtime"]["status"] == "applied"
        assert emergency_stopped["runtime"]["emergency_stop"] is True
        assert emergency_stopped["actor_status"] == "stopped"
        emergency_stop_effect = emergency_stopped["runtime"]["effect_id"]
        with sqlite3.connect(emergency_project / "scheduler" / "state.db") as connection:
            assert connection.execute(
                "SELECT status FROM effects WHERE effect_id=?", (emergency_stop_effect,)
            ).fetchone()[0] == "applied"
            assert connection.execute(
                "SELECT status FROM effects WHERE effect_id=?",
                (blocked_spawn["start"]["effect_id"],),
            ).fetchone()[0] == "pending"
            assert connection.execute(
                "SELECT status FROM commands WHERE command_id='CMD-EMERGENCY-STOP-CRASH'"
            ).fetchone()[0] == "completed"
        emergency_actor_after = json.loads(
            (
                emergency_project
                / "scheduler"
                / "actors"
                / f"{emergency_running['actor_id']}.json"
            ).read_text(encoding="utf-8")
        )
        assert emergency_actor_after["status"] == "stopped"
        assert emergency_counter.read_text(encoding="utf-8").splitlines() == ["once"]

        after_stop_before_blocked_scheduler = tree_digest(emergency_project)
        blocked_again = run(
            temp,
            "run-scheduler",
            "--project",
            str(emergency_project),
            "--once",
            env_extra=emergency_env,
            ok=False,
        )
        assert blocked_again.returncode != 0
        assert tree_digest(emergency_project) == after_stop_before_blocked_scheduler
        assert emergency_counter.read_text(encoding="utf-8").splitlines() == ["once"]

        provider_calls = main_provider_calls + len(
            emergency_counter.read_text(encoding="utf-8").splitlines()
        )
        assert provider_calls == 5
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
                        "effect.after_stop_observe_before_apply",
                        "transaction.after_commit_before_materialize",
                    ],
                    "recovery_scenarios": [
                        "corrupt_project_emergency_stop_only",
                        "governance_drift_emergency_stop_replay",
                        "slow_stop_lease_heartbeat_single_execution",
                        "daemon_sleep_does_not_block_emergency_stop",
                        "no_effect_commit_view_reconciled_by_scheduler",
                    ],
                    "provider_calls": provider_calls,
                    "expected_provider_calls": 5,
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
