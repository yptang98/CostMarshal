#!/usr/bin/env python3
"""Dispatch spawn effects survive scheduler hard exits without duplicate provider calls."""

from __future__ import annotations

import hashlib
import json
import os
import signal
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
from costmarshal_v2.session_backend import pid_is_alive, pid_start_marker  # noqa: E402
from costmarshal_v2.state import load_actor, load_session, load_task, save_actor, save_task  # noqa: E402
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


def read_pid_file(path: Path) -> int | None:
    try:
        value = int(path.read_text(encoding="ascii").strip())
    except (OSError, ValueError):
        return None
    return value if value > 0 else None


def wait_for_pid_exit(pid: int, *, timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not pid_is_alive(pid):
            return True
        time.sleep(0.05)
    return not pid_is_alive(pid)


def cleanup_test_pid(pid: int) -> None:
    """Best-effort exact-PID cleanup for a failed process-tree assertion."""

    marker = pid_start_marker(pid)
    if marker is None:
        return
    if os.name == "nt":
        subprocess.run(
            ["taskkill", "/PID", str(pid), "/T", "/F"],
            text=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
    elif pid_start_marker(pid) == marker:
        os.kill(pid, signal.SIGTERM)
    if wait_for_pid_exit(pid, timeout=2.0):
        return
    if os.name != "nt" and pid_start_marker(pid) == marker:
        os.kill(pid, signal.SIGKILL)
        wait_for_pid_exit(pid, timeout=1.0)


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
    previous_codex_home = os.environ.get("CODEX_HOME")
    try:
        codex_home = temp / "codex-home"
        os.environ["CODEX_HOME"] = str(codex_home)
        configured = run_json(
            temp,
            "configure-profiles",
            "--codex-home",
            str(codex_home),
        )
        assert Path(configured["path"]).is_file()

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

        # A user-authorized STOP linearizes against an older pending SPAWN.
        # Drain STOP first, then prove the normal scheduler cannot resurrect the
        # stopped actor or call its provider.
        run_json(temp, "new-task", "--project", str(project), "--title", "stop-pending", "--purpose", "cancel pending spawn")
        pending_stop = run_json(
            temp,
            "dispatch",
            "--project",
            str(project),
            "--task",
            "V2-0005",
            "--start",
            "--unsafe-native",
            "--command-id",
            "CMD-SPAWN-STOP-PENDING",
        )
        project_layout = resolve_project(temp / "runtime", str(project))
        stopped_pending = run_json(
            temp,
            "stop-actor",
            "--project",
            str(project),
            "--actor",
            pending_stop["actor_id"],
            "--stop-runtime",
            "--reason",
            "cancel before spawn",
            "--command-id",
            "CMD-STOP-PENDING-SPAWN",
        )
        stop_effect_id = stopped_pending["runtime"]["effect_id"]
        assert stopped_pending["runtime"]["status"] == "applied"
        assert effect_status(project_layout, stop_effect_id)["status"] == "applied"
        assert effect_status(project_layout, pending_stop["start"]["effect_id"])["status"] == "dead"
        cancelled_actor = load_actor(project_layout, pending_stop["actor_id"])
        assert cancelled_actor["status"] == "stopped"
        cancelled_task = json.loads((project / "tasks" / "V2-0005" / "task.json").read_text(encoding="utf-8"))
        assert cancelled_task["attempts"][-1]["status"] == "cancelled"
        run_json(temp, "run-scheduler", "--project", str(project), "--once", env_extra=provider_env)
        assert len(counter.read_text(encoding="utf-8").splitlines()) == 4
        with sqlite3.connect(project / "scheduler" / "state.db") as connection:
            orphan_effects = int(
                connection.execute(
                    "SELECT COUNT(*) FROM effects WHERE status NOT IN ('applied', 'dead')"
                ).fetchone()[0]
            )
        assert orphan_effects == 0

        # Exercise the harder platform-specific linearization: SPAWN owns its
        # durable lease inside backend.start_actor when a public stop-actor
        # commits STOP. Windows must cancel the prepared child before resume;
        # POSIX must let its already-entered unsuspended start finish exactly
        # once, then let STOP consume that PID/marker. Neither platform may
        # resurrect the actor, duplicate the provider, or retain an orphan.
        run_json(
            temp,
            "new-task",
            "--project",
            str(project),
            "--title",
            "stop-racing-spawn",
            "--purpose",
            "linearize stop against an in-flight external start",
        )
        racing_spawn = run_json(
            temp,
            "dispatch",
            "--project",
            str(project),
            "--task",
            "V2-0006",
            "--start",
            "--unsafe-native",
            "--command-id",
            "CMD-SPAWN-RACING-STOP",
        )
        race_provider_counter = temp / "race-provider-count.txt"
        race_provider_pid_file = temp / "race-provider.pid"
        race_provider = temp / "race_provider.py"
        race_provider.write_text(
            "\n".join(
                [
                    "import os, pathlib, sys, time",
                    f"pathlib.Path({str(race_provider_pid_file)!r}).write_text(str(os.getpid()), encoding='ascii')",
                    f"counter = pathlib.Path({str(race_provider_counter)!r})",
                    "with counter.open('a', encoding='utf-8') as handle: handle.write('once\\n')",
                    "time.sleep(60)",
                    "output = pathlib.Path(sys.argv[sys.argv.index('--output-last-message') + 1])",
                    "output.write_text('# Completion Report\\n\\nStatus: done\\n', encoding='utf-8')",
                ]
            )
            + "\n",
            encoding="utf-8",
        )
        race_provider_command = json.dumps([sys.executable, str(race_provider)])
        previous_provider_command = os.environ.get("COSTMARSHAL_CODEX_COMMAND_JSON")
        real_backend = scheduler_module.backend_from_session(load_session(project_layout))
        original_backend_from_session = scheduler_module.backend_from_session
        race_start_entered = threading.Event()
        race_start_release = threading.Event()
        race_start_calls: list[dict[str, object]] = []
        race_scheduler_results: list[dict] = []
        race_stop_results: list[dict] = []
        race_errors: list[BaseException] = []

        class BarrierStartBackend:
            kind = "local"

            def start_actor(self, **kwargs: object) -> dict[str, object]:
                race_start_entered.set()
                if not race_start_release.wait(10):
                    raise RuntimeError("timed out waiting to release the racing SPAWN")
                launch = real_backend.start_actor(**kwargs)
                race_start_calls.append(dict(launch))
                provider_deadline = time.monotonic() + 10
                while time.monotonic() < provider_deadline:
                    calls = (
                        race_provider_counter.read_text(encoding="utf-8").splitlines()
                        if race_provider_counter.is_file()
                        else []
                    )
                    if calls:
                        break
                    pid = launch.get("pid")
                    if isinstance(pid, int) and not pid_is_alive(pid):
                        raise RuntimeError("racing runner exited before entering its provider")
                    time.sleep(0.05)
                else:
                    raise RuntimeError("racing runner did not enter its provider")
                return launch

        barrier_backend = BarrierStartBackend()

        def drain_racing_spawn() -> None:
            try:
                race_scheduler_results.append(
                    scheduler_module.process_runtime_effects(
                        project_layout,
                        limit=1,
                        _governance_prevalidated=True,
                    )
                )
            except BaseException as exc:
                race_errors.append(exc)

        def public_racing_stop() -> None:
            try:
                race_stop_results.append(
                    run_json(
                        temp,
                        "stop-actor",
                        "--project",
                        str(project),
                        "--actor",
                        racing_spawn["actor_id"],
                        "--stop-runtime",
                        "--reason",
                        "stop committed during backend start",
                        "--command-id",
                        "CMD-STOP-RACING-SPAWN",
                    )
                )
            except BaseException as exc:
                race_errors.append(exc)

        racing_scheduler_thread = threading.Thread(
            target=drain_racing_spawn,
            name="racing-spawn-drainer",
        )
        racing_stop_thread = threading.Thread(
            target=public_racing_stop,
            name="public-racing-stop",
        )
        race_runner_alive_after_stop = False
        race_provider_alive_after_stop = False
        try:
            os.environ["COSTMARSHAL_CODEX_COMMAND_JSON"] = race_provider_command
            scheduler_module.backend_from_session = lambda session: barrier_backend
            racing_scheduler_thread.start()
            assert race_start_entered.wait(5), "SPAWN did not enter the external start barrier"
            assert effect_status(project_layout, racing_spawn["start"]["effect_id"])["status"] == "leased"

            racing_stop_thread.start()
            racing_stop_effect_id = "EFF-STOP-CMD-STOP-RACING-SPAWN"
            stop_commit_deadline = time.monotonic() + 5
            stop_commit_status = None
            while time.monotonic() < stop_commit_deadline:
                with sqlite3.connect(project / "scheduler" / "state.db") as connection:
                    row = connection.execute(
                        "SELECT status FROM effects WHERE effect_id=?",
                        (racing_stop_effect_id,),
                    ).fetchone()
                if row is not None:
                    stop_commit_status = str(row[0])
                    break
                if race_errors:
                    raise race_errors[0]
                time.sleep(0.05)
            assert stop_commit_status == "pending", "public STOP was not committed behind the leased SPAWN"
            assert effect_status(project_layout, racing_spawn["start"]["effect_id"])["status"] == "leased"
            race_start_release.set()
            racing_scheduler_thread.join(10)
            racing_stop_thread.join(10)
        finally:
            race_start_release.set()
            racing_scheduler_thread.join(10)
            racing_stop_thread.join(10)
            scheduler_module.backend_from_session = original_backend_from_session
            if previous_provider_command is None:
                os.environ.pop("COSTMARSHAL_CODEX_COMMAND_JSON", None)
            else:
                os.environ["COSTMARSHAL_CODEX_COMMAND_JSON"] = previous_provider_command
            provider_pid = read_pid_file(race_provider_pid_file)
            for launch in race_start_calls:
                pid = launch.get("pid")
                if isinstance(pid, int):
                    race_runner_alive_after_stop = race_runner_alive_after_stop or pid_is_alive(pid)
                if isinstance(pid, int) and pid_is_alive(pid):
                    marker = pid_start_marker(pid)
                    if marker:
                        try:
                            real_backend.stop_actor(
                                target=str(launch.get("target") or ""),
                                pid=pid,
                                process_start_marker=marker,
                                **{
                                    key: launch.get(key)
                                    for key in (
                                        "windows_job_name",
                                        "windows_job_identity",
                                        "windows_job_child_pid",
                                        "windows_job_child_start_marker",
                                    )
                                },
                            )
                        except BaseException as exc:
                            race_errors.append(exc)
            if provider_pid is not None:
                race_provider_alive_after_stop = pid_is_alive(provider_pid)
                if race_provider_alive_after_stop:
                    try:
                        cleanup_test_pid(provider_pid)
                    except BaseException as exc:
                        race_errors.append(exc)

        assert not racing_scheduler_thread.is_alive() and not racing_stop_thread.is_alive()
        assert race_errors == [], repr(race_errors)
        assert race_runner_alive_after_stop is False, "STOP left the externally started runner alive"
        assert race_provider_alive_after_stop is False, "STOP left the provider child alive"
        assert len(race_scheduler_results) == 1
        if os.name == "nt":
            # STOP committed before the Windows prepared callback authorized
            # ResumeThread. The child must remain suspended and be contained;
            # a provider call is no longer an acceptable outcome.
            assert race_start_calls == []
            assert race_scheduler_results[0]["processed"] == []
            assert len(race_scheduler_results[0]["failed"]) == 1
            assert (
                race_scheduler_results[0]["failed"][0]["effect_id"]
                == racing_spawn["start"]["effect_id"]
            )
            assert "stopped before Windows Job resume authorization" in str(
                race_scheduler_results[0]["failed"][0]["error"]
            )
        else:
            # POSIX local launch has no suspended-child preparation primitive.
            # The already-entered start therefore linearizes first, and STOP
            # must consume its one exact receipt without a duplicate launch.
            assert len(race_start_calls) == 1
            assert race_scheduler_results[0]["failed"] == []
            assert len(race_scheduler_results[0]["processed"]) == 1
            assert (
                race_scheduler_results[0]["processed"][0]["effect_id"]
                == racing_spawn["start"]["effect_id"]
            )
        assert len(race_stop_results) == 1
        racing_stop = race_stop_results[0]
        assert racing_stop["runtime"]["status"] == "applied"
        assert racing_stop["runtime"]["effect_status"] == "applied"
        assert racing_stop["runtime"]["drain_deferred"] is False
        assert effect_status(project_layout, racing_stop["runtime"]["effect_id"])["status"] == "applied"
        racing_actor = load_actor(project_layout, racing_spawn["actor_id"])
        assert racing_actor["status"] == "stopped"
        racing_task = load_task(project_layout, "V2-0006")
        racing_attempt = next(
            row
            for row in racing_task["attempts"]
            if row["attempt_id"] == racing_actor["attempt_id"]
        )
        assert racing_attempt["status"] == "cancelled"
        race_provider_calls = (
            len(race_provider_counter.read_text(encoding="utf-8").splitlines())
            if race_provider_counter.is_file()
            else 0
        )
        race_provider_pid = read_pid_file(race_provider_pid_file)
        if os.name == "nt":
            assert race_provider_pid is None, "provider started after STOP won resume authorization"
            assert race_provider_calls == 0
        else:
            assert race_provider_pid is not None, "POSIX start did not reach its provider"
            assert wait_for_pid_exit(race_provider_pid), "STOP left the POSIX provider alive"
            assert race_provider_calls == 1
        run_json(
            temp,
            "run-scheduler",
            "--project",
            str(project),
            "--once",
            env_extra={"COSTMARSHAL_CODEX_COMMAND_JSON": race_provider_command},
        )
        time.sleep(0.3)
        assert (
            len(race_provider_counter.read_text(encoding="utf-8").splitlines())
            if race_provider_counter.is_file()
            else 0
        ) == race_provider_calls
        assert len(race_start_calls) == (0 if os.name == "nt" else 1)
        assert load_actor(project_layout, racing_spawn["actor_id"])["status"] == "stopped"
        assert load_task(project_layout, "V2-0006")["attempts"][-1]["status"] == "cancelled"
        with sqlite3.connect(project / "scheduler" / "state.db") as connection:
            orphan_effects = int(
                connection.execute(
                    "SELECT COUNT(*) FROM effects WHERE status NOT IN ('applied', 'dead')"
                ).fetchone()[0]
            )
        assert orphan_effects == 0

        # An explicit recovery generation must not consume a registration made
        # by the missing prior runner. The original pending effect is fenced
        # dead and the newly authorized generation performs exactly one start.
        run_json(
            temp,
            "new-task",
            "--project",
            str(project),
            "--title",
            "generation-fenced-recovery",
            "--purpose",
            "prove explicit recovery cannot reuse stale runner registration",
        )
        stale_registration = run_json(
            temp,
            "dispatch",
            "--project",
            str(project),
            "--task",
            "V2-0007",
            "--start",
            "--unsafe-native",
            "--command-id",
            "CMD-SPAWN-STALE-REGISTRATION",
        )
        stale_actor = load_actor(project_layout, stale_registration["actor_id"])
        stale_runtime = stale_actor.setdefault("runtime", {})
        stale_generation = int(stale_runtime["recovery_generation"])
        stale_runtime["registered_launch_token_sha256"] = hashlib.sha256(
            str(stale_actor["launch_token"]).encode("utf-8")
        ).hexdigest()
        stale_runtime["registered_profile_sha256"] = str(
            (stale_actor.get("profile_binding") or {}).get("sha256") or ""
        ).removeprefix("sha256:")
        stale_runtime["registered_recovery_generation"] = stale_generation
        stale_runtime["provider_execution_state"] = "launch_pending_authorization"
        stale_runtime["pid"] = None
        stale_runtime["process_start_marker"] = None
        stale_runtime["target"] = None
        for key in (
            "windows_job_name",
            "windows_job_identity",
            "windows_job_child_pid",
            "windows_job_child_start_marker",
        ):
            stale_runtime.pop(key, None)
        stale_actor["status"] = "needs_recovery"
        save_actor(project_layout, stale_actor)
        stale_task = load_task(project_layout, "V2-0007")
        stale_task["status"] = "needs_recovery"
        stale_task["attempts"][-1]["status"] = "needs_recovery"
        save_task(project_layout, stale_task)

        explicit_recovery = run_json(
            temp,
            "recover",
            "--project",
            str(project),
            "--restart-missing",
            "--command-id",
            "CMD-RECOVER-STALE-REGISTRATION",
        )
        recovered_actor = load_actor(project_layout, stale_registration["actor_id"])
        recovered_runtime = recovered_actor["runtime"]
        recovery_effect_id = str(recovered_runtime["recovery_effect_id"])
        assert recovered_runtime["recovery_generation"] == stale_generation + 1
        assert explicit_recovery["restarted"] == [f"queued:{recovery_effect_id}"]

        generation_cycle = run_json(
            temp,
            "run-scheduler",
            "--project",
            str(project),
            "--once",
            env_extra=provider_env,
        )
        assert [
            row["effect_id"] for row in generation_cycle["last_effects"]["failed"]
        ] == [stale_registration["start"]["effect_id"]]
        assert [
            row["effect_id"] for row in generation_cycle["last_effects"]["processed"]
        ] == [recovery_effect_id]
        wait_for_count(
            counter,
            main_provider_calls + 1,
            project=project,
            actor_id=stale_registration["actor_id"],
        )
        wait_for_status(project, "V2-0007", "waiting_leader")
        assert effect_status(
            project_layout, stale_registration["start"]["effect_id"]
        )["status"] == "dead"
        assert effect_status(project_layout, recovery_effect_id)["status"] == "applied"
        final_registration = load_actor(
            project_layout, stale_registration["actor_id"]
        )["runtime"]
        assert (
            final_registration["registered_recovery_generation"]
            == stale_generation + 1
        )
        main_provider_calls += 1

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
        run_json(temp, "migrate-state", "--project", str(slow_project), "--apply")
        slow_layout = resolve_project(temp / "runtime", str(slow_project))
        with control_transaction(
            slow_layout,
            command_name="test_seed_slow_stop_runtime",
            command_id="TEST-SEED-SLOW-STOP-RUNTIME",
            payload={"actor": "leader", "pid": 999999},
        ) as transaction:
            if not transaction.replay:
                slow_actor_seed = load_actor(slow_layout, "leader")
                slow_actor_seed["runtime"].update(
                    {
                        "target": "pid:999999",
                        "pid": 999999,
                        "process_start_marker": "slow-stop-marker",
                    }
                )
                save_actor(slow_layout, slow_actor_seed)
                transaction.set_result({"status": "seeded"})
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

        # The synthetic slow backend above represents a confirmed STOP but has
        # no real Windows Job receipt. Remove its test-only PID binding before
        # exercising an idempotent public STOP through the real backend.
        with control_transaction(
            slow_layout,
            command_name="test_clear_slow_stop_runtime",
            command_id="TEST-CLEAR-SLOW-STOP-RUNTIME",
            payload={"actor": "leader"},
        ) as transaction:
            if not transaction.replay:
                stopped_actor = load_actor(slow_layout, "leader")
                stopped_runtime = stopped_actor.setdefault("runtime", {})
                stopped_runtime["target"] = None
                stopped_runtime["pid"] = None
                stopped_runtime["process_start_marker"] = None
                save_actor(slow_layout, stopped_actor)
                transaction.set_result({"status": "cleared"})

        daemon_acquired = threading.Event()
        daemon_release = threading.Event()

        def hold_daemon_lock() -> None:
            # Model a daemon sleeping for an arbitrarily long poll interval.
            # Its lifetime lock must not block the short runtime-effect mutex.
            with scheduler_daemon_lock(slow_layout, timeout_seconds=5):
                daemon_acquired.set()
                daemon_release.wait()

        daemon_thread = threading.Thread(target=hold_daemon_lock, name="existing-daemon")
        daemon_thread.start()
        try:
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
        finally:
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

        # A permanently failed STOP must never claim the runtime was stopped.
        failed_stop_actor = load_actor(slow_layout, "leader")
        failed_stop_actor["status"] = "running"
        failed_stop_actor.pop("stopped_at", None)
        failed_stop_actor["stop_requested_at"] = scheduler_module.now_iso()
        failed_stop_actor["runtime"].update(
            {
                "target": "pid:999997",
                "pid": 999997,
                "process_start_marker": "still-live-marker",
            }
        )
        failed_stop_effect_id = "EFF-STOP-CMD-PERMANENT-STOP-FAILURE"
        with control_transaction(
            slow_layout,
            command_name="command_stop_actor",
            command_id="CMD-PERMANENT-STOP-FAILURE",
            payload={"actor": "leader", "stop_runtime": True},
        ) as transaction:
            transaction.queue_effect(
                effect_id=failed_stop_effect_id,
                effect_type=scheduler_module.STOP_EFFECT_TYPE,
                aggregate_id="leader",
                generation=1,
                payload=scheduler_module._stop_effect_payload(
                    failed_stop_actor,
                    reason="exercise permanent stop failure",
                ),
            )
            save_actor(slow_layout, failed_stop_actor)
            transaction.set_result({"effect_id": failed_stop_effect_id})
        with sqlite3.connect(slow_project / "scheduler" / "state.db") as connection:
            connection.execute(
                "UPDATE effects SET attempts=? WHERE effect_id=?",
                (scheduler_module.SPAWN_EFFECT_MAX_ATTEMPTS - 1, failed_stop_effect_id),
            )
            connection.commit()

        class PermanentStopFailureBackend:
            kind = "local"

            def available(self) -> bool:
                return True

            def actor_alive(self, **kwargs: object) -> bool:
                return True

            def stop_actor(self, **kwargs: object) -> dict[str, str]:
                raise RuntimeError("simulated permanent stop failure")

        scheduler_module.backend_from_session = lambda session: PermanentStopFailureBackend()
        try:
            failed_stop_cycle = scheduler_module.process_runtime_effects(
                slow_layout,
                limit=1,
                _governance_prevalidated=True,
            )
        finally:
            scheduler_module.backend_from_session = original_backend_from_session
        assert failed_stop_cycle["failed"][0]["retryable"] is False
        assert effect_status(slow_layout, failed_stop_effect_id)["status"] == "dead"
        failed_stop_projection = load_actor(slow_layout, "leader")
        assert failed_stop_projection["status"] == "needs_recovery"
        assert failed_stop_projection.get("stopped_at") is None
        assert (
            failed_stop_projection["runtime"]["effect_failure_disposition"]
            == "stop_failed_runtime_may_be_live"
        )

        # A STOP for an older task attempt is still a STOP: if the external
        # runtime operation dies permanently, neither the actor nor that old
        # attempt may be projected as stopped merely because a successor exists.
        run_json(
            temp,
            "new-task",
            "--project",
            str(slow_project),
            "--title",
            "stale stop",
            "--purpose",
            "prove failed old-attempt stop remains recoverable",
        )
        stale_task = load_task(slow_layout, "V2-0001")
        stale_task["attempts"] = [
            {
                "attempt": 1,
                "attempt_id": "ATT-STALE-STOP",
                "actor_id": "stale-stop-agent",
                "status": "running",
                "stopped_at": "must-be-cleared",
            },
            {
                "attempt": 2,
                "attempt_id": "ATT-SUCCESSOR",
                "actor_id": "successor-agent",
                "status": "running",
            },
        ]
        stale_stop_actor = {
            "schema_version": 2,
            "id": "stale-stop-agent",
            "role": "agent",
            "status": "running",
            "task_id": "V2-0001",
            "attempt_id": "ATT-STALE-STOP",
            "stopped_at": "must-be-cleared",
            "stop_requested_at": scheduler_module.now_iso(),
            "runtime": {
                "backend": "local",
                "target": "pid:999996",
                "pid": 999996,
                "process_start_marker": "stale-stop-live-marker",
            },
        }
        stale_stop_effect_id = "EFF-STOP-CMD-PERMANENT-STALE-STOP-FAILURE"
        with control_transaction(
            slow_layout,
            command_name="command_stop_actor",
            command_id="CMD-PERMANENT-STALE-STOP-FAILURE",
            payload={"actor": "stale-stop-agent", "stop_runtime": True},
        ) as transaction:
            save_task(slow_layout, stale_task)
            save_actor(slow_layout, stale_stop_actor)
            transaction.queue_effect(
                effect_id=stale_stop_effect_id,
                effect_type=scheduler_module.STOP_EFFECT_TYPE,
                aggregate_id="stale-stop-agent",
                generation=1,
                payload=scheduler_module._stop_effect_payload(
                    stale_stop_actor,
                    reason="exercise permanent stale-attempt stop failure",
                ),
            )
            transaction.set_result({"effect_id": stale_stop_effect_id})
        with sqlite3.connect(slow_project / "scheduler" / "state.db") as connection:
            connection.execute(
                "UPDATE effects SET attempts=? WHERE effect_id=?",
                (scheduler_module.SPAWN_EFFECT_MAX_ATTEMPTS - 1, stale_stop_effect_id),
            )
            connection.commit()
        scheduler_module.backend_from_session = lambda session: PermanentStopFailureBackend()
        try:
            stale_stop_cycle = scheduler_module.process_runtime_effects(
                slow_layout,
                limit=1,
                _governance_prevalidated=True,
            )
        finally:
            scheduler_module.backend_from_session = original_backend_from_session
        assert stale_stop_cycle["failed"][0]["retryable"] is False
        assert effect_status(slow_layout, stale_stop_effect_id)["status"] == "dead"
        stale_stop_projection = load_actor(slow_layout, "stale-stop-agent")
        assert stale_stop_projection["status"] == "needs_recovery"
        assert stale_stop_projection.get("stopped_at") is None
        assert (
            stale_stop_projection["runtime"]["effect_failure_disposition"]
            == "stop_failed_runtime_may_be_live"
        )
        stale_task_projection = load_task(slow_layout, "V2-0001")
        assert stale_task_projection["attempts"][0]["status"] == "needs_recovery"
        assert stale_task_projection["attempts"][0].get("stopped_at") is None
        assert stale_task_projection["attempts"][1]["status"] == "running"

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
        assert emergency_stop_crash.returncode == 96, (
            emergency_stop_crash.returncode,
            emergency_stop_crash.stdout[-2000:],
            emergency_stop_crash.stderr[-2000:],
        )
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

        provider_calls = (
            main_provider_calls
            + len(emergency_counter.read_text(encoding="utf-8").splitlines())
            + race_provider_calls
        )
        expected_provider_calls = 6 if os.name == "nt" else 7
        assert provider_calls == expected_provider_calls
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
                        "stop_cancels_pending_spawn_before_provider",
                        "stop_linearizes_against_inflight_spawn",
                        "explicit_recovery_rejects_stale_runner_registration",
                        "stop_permanent_failure_remains_recoverable",
                    ],
                    "provider_calls": provider_calls,
                    "expected_provider_calls": expected_provider_calls,
                    "orphan_effects": orphan_effects,
                },
                sort_keys=True,
            )
        )
        return 0
    finally:
        if previous_codex_home is None:
            os.environ.pop("CODEX_HOME", None)
        else:
            os.environ["CODEX_HOME"] = previous_codex_home
        shutil.rmtree(temp, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
