#!/usr/bin/env python3
"""Regression paths for durable effect failure and pre-provider runner recovery."""

from __future__ import annotations

import json
import hashlib
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
CLI = ROOT / "scripts" / "costmarshal.py"
sys.path.insert(0, str(ROOT))

from costmarshal_v2.control_store import (  # noqa: E402
    control_transaction,
    dead_effect,
    effect_status,
    lease_effect,
)
from costmarshal_v2.paths import ProjectLayout, slugify  # noqa: E402
from costmarshal_v2.scheduler import (  # noqa: E402
    SPAWN_EFFECT_TYPE,
    STOP_EFFECT_TYPE,
    _stop_effect_payload,
    process_runtime_effects,
)
from costmarshal_v2.session_backend import pid_is_alive  # noqa: E402
from costmarshal_v2.state import load_actor, load_project, save_actor  # noqa: E402
from costmarshal_v2.worker_isolation import (  # noqa: E402
    WorkerExecutionError,
    cleanup_temporary_credential,
)


def run(
    temp: Path,
    *args: str,
    env_extra: dict[str, str] | None = None,
    ok: bool = True,
) -> subprocess.CompletedProcess[str]:
    environment = os.environ.copy()
    environment["COSTMARSHAL_V2_HOME"] = str(temp / "runtime")
    environment["CODEX_HOME"] = str(temp / "codex-home")
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


def wait_for_dead_actor(project: Path, actor_id: str, timeout: float = 15.0) -> dict:
    path = project / "scheduler" / "actors" / f"{actor_id}.json"
    deadline = time.monotonic() + timeout
    latest: dict = {}
    while time.monotonic() < deadline:
        latest = json.loads(path.read_text(encoding="utf-8"))
        pid = (latest.get("runtime") or {}).get("pid")
        if pid and not pid_is_alive(int(pid)):
            return latest
        time.sleep(0.1)
    raise AssertionError(f"actor did not exit: {latest}")


def wait_for_counter(counter: Path, expected: int, timeout: float = 15.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        rows = counter.read_text(encoding="utf-8").splitlines() if counter.is_file() else []
        if len(rows) >= expected:
            return
        time.sleep(0.1)
    raise AssertionError(f"provider counter did not reach {expected}")


def main() -> int:
    temp = Path(tempfile.mkdtemp(prefix="costmarshal-runtime-reliability-"))
    try:
        configured = run_json(
            temp,
            "configure-profiles",
            "--codex-home",
            str(temp / "codex-home"),
        )
        assert Path(configured["path"]).is_file()

        workspace = temp / "workspace"
        workspace.mkdir()
        project = Path(
            run_json(
                temp,
                "init",
                "--name",
                "runtime-reliability",
                "--objective",
                "recover durable runtime failures",
                "--workspace",
                str(workspace),
                "--backend",
                "local",
                "--governance",
                "off",
                "--allow-unsafe-native-workers",
            )["project"]
        )
        run_json(temp, "migrate-state", "--project", str(project), "--apply")

        run_json(temp, "new-task", "--project", str(project), "--title", "dead", "--purpose", "dead replay")
        dead_args = (
            "dispatch",
            "--project",
            str(project),
            "--task",
            "V2-0001",
            "--start",
            "--unsafe-native",
            "--command-id",
            "CMD-DEAD-REPLAY",
        )
        queued = run_json(temp, *dead_args)
        layout = ProjectLayout(root=temp / "runtime", project_dir=project)
        effect = lease_effect(
            layout,
            owner="test-owner",
            ttl_seconds=30,
            effect_types=(SPAWN_EFFECT_TYPE,),
        )
        assert effect and effect["effect_id"] == queued["start"]["effect_id"]
        dead_effect(
            layout,
            effect_id=str(effect["effect_id"]),
            owner="test-owner",
            error="deterministic backend rejection",
        )
        replay = run(temp, *dead_args, ok=False)
        assert replay.returncode != 0
        assert "Durable command CMD-DEAD-REPLAY failed [effect_dead]" in (replay.stdout + replay.stderr)
        assert "queued" not in replay.stdout
        run_json(
            temp,
            "heartbeat",
            "--project",
            str(project),
            "--actor",
            queued["actor_id"],
            "--status",
            "stopped",
        )

        counter = temp / "provider-count.txt"
        fake = temp / "fake_codex.py"
        fake.write_text(
            "\n".join(
                [
                    "import json, pathlib, sys",
                    f"counter = pathlib.Path({str(counter)!r})",
                    "with counter.open('a', encoding='utf-8') as handle: handle.write('once\\n')",
                    "output = pathlib.Path(sys.argv[sys.argv.index('--output-last-message') + 1])",
                    "output.write_text('# Completion Report\\n\\nStatus: done\\n', encoding='utf-8')",
                    "print(json.dumps({'usage': {'input_tokens': 7, 'output_tokens': 2}}))",
                ]
            )
            + "\n",
            encoding="utf-8",
        )
        provider_env = {"COSTMARSHAL_CODEX_COMMAND_JSON": json.dumps([sys.executable, str(fake)])}
        run_json(
            temp,
            "new-task",
            "--project",
            str(project),
            "--title",
            "recover",
            "--purpose",
            "runner retry",
            "--allowed-path",
            "file.txt",
            "--claim-path",
            "file.txt",
        )
        queued_retry = run_json(
            temp,
            "dispatch",
            "--project",
            str(project),
            "--task",
            "V2-0002",
            "--start",
            "--unsafe-native",
            "--command-id",
            "CMD-RUNNER-EARLY-EXIT",
        )
        actor_id = queued_retry["actor_id"]
        run_json(temp, "run-scheduler", "--project", str(project), "--once", env_extra=provider_env)
        failed_actor = wait_for_dead_actor(project, actor_id)
        assert (failed_actor.get("runtime") or {}).get("provider_execution_state") is None
        assert not counter.exists()

        (workspace / "file.txt").write_text("recovery base\n", encoding="utf-8")
        subprocess.run(["git", "init", str(workspace)], check=True, stdout=subprocess.DEVNULL)
        subprocess.run(["git", "-C", str(workspace), "config", "user.email", "test@example.com"], check=True)
        subprocess.run(["git", "-C", str(workspace), "config", "user.name", "CostMarshal Test"], check=True)
        subprocess.run(["git", "-C", str(workspace), "add", "file.txt"], check=True)
        subprocess.run(["git", "-C", str(workspace), "commit", "-m", "base"], check=True, stdout=subprocess.DEVNULL)

        recovery = run_json(
            temp,
            "recover",
            "--project",
            str(project),
            "--restart-missing",
            "--command-id",
            "CMD-RECOVER-RUNNER",
        )
        assert len(recovery["restarted"]) == 1 and recovery["restarted"][0].startswith("queued:"), recovery
        run_json(temp, "run-scheduler", "--project", str(project), "--once", env_extra=provider_env)
        wait_for_counter(counter, 1)
        time.sleep(0.5)
        assert counter.read_text(encoding="utf-8").splitlines() == ["once"]

        # Simulate the exact post-rm/pre-observe OCI crash state: the durable
        # immutable ID remains, the container is absent, and the selected
        # credential may still need idempotent deletion.
        run_json(temp, "new-task", "--project", str(project), "--title", "oci-stop", "--purpose", "finish absent cleanup")
        oci_dispatch = run_json(
            temp,
            "dispatch",
            "--project",
            str(project),
            "--task",
            "V2-0003",
            "--unsafe-native",
            "--command-id",
            "CMD-OCI-STOP-TARGET",
        )
        oci_actor_id = oci_dispatch["actor_id"]
        oci_actor = load_actor(layout, oci_actor_id)
        project_state = load_project(layout)
        attempt_id = str(oci_actor["attempt_id"])
        bundle = (
            temp
            / "runtime"
            / "worker-bundles"
            / slugify(str(project_state["project_id"]), "project")
            / slugify(attempt_id, "attempt")
        )
        credential_root = bundle / "credential"
        credential_root.mkdir(parents=True)
        credential = credential_root / "provider.secret"
        credential.write_text("selected-only", encoding="utf-8")
        oci_actor["isolation"] = {
            "mode": "required",
            "execution": {
                "engine": "docker",
                "image": "example/worker@sha256:" + "a" * 64,
                "network_mode": "none",
                "network_name": None,
                "workspace_mode": "ro",
                "limits": {},
            },
        }
        oci_actor.setdefault("runtime", {}).update(
            {
                # OCI emergency cleanup is fenced by container identity and must
                # remain available even if an unrelated native runtime binding
                # was corrupted.
                "backend": "tmux",
                "session_name": "corrupt-session",
                "actor_name": "corrupt-actor",
                "provider_execution_state": "started",
                "execution_workspace": str(workspace),
                "target": "container:durable-container",
                "container_name": "durable-container",
                "container_id": "c" * 64,
                "container_command": ["costmarshal-worker", "--jsonl"],
                "credential_cleanup": {
                    "required": True,
                    "path": str(credential),
                    "status": "pending",
                },
            }
        )
        with control_transaction(
            layout,
            command_name="test_prepare_oci_stop",
            command_id="CMD-TEST-PREPARE-OCI-STOP",
            payload={"actor_id": oci_actor_id},
        ):
            save_actor(layout, oci_actor)
        stop_effect_id = "EFF-STOP-CMD-OCI-STOP-ABSENT"
        with control_transaction(
            layout,
            command_name="command_stop_actor",
            command_id="CMD-OCI-STOP-ABSENT",
            payload={"actor": oci_actor_id, "stop_runtime": True},
        ) as transaction:
            transaction.queue_effect(
                effect_id=stop_effect_id,
                effect_type=STOP_EFFECT_TYPE,
                aggregate_id=oci_actor_id,
                generation=1,
                payload=_stop_effect_payload(oci_actor, reason="already absent"),
            )
            transaction.set_result({"effect_id": stop_effect_id})

        class AbsentAdapter:
            def __init__(self, backend: object) -> None:
                self.backend = backend

            def attach(self, *args: object, **kwargs: object) -> object:
                raise WorkerExecutionError("lifecycle_command_failed", "container was already removed")

            def cleanup_confirmed_absent(self, spec: object, **kwargs: object) -> object:
                assert kwargs["container_id"] == "c" * 64
                receipt = cleanup_temporary_credential(spec)  # type: ignore[arg-type]
                return SimpleNamespace(container_removed=True, credential=receipt)

        with patch("costmarshal_v2.scheduler.OciCliBackend", lambda engine: object()), patch(
            "costmarshal_v2.scheduler.OciWorkerExecutionAdapter",
            AbsentAdapter,
        ):
            stop_cycle = process_runtime_effects(layout, limit=1)
        assert stop_cycle["processed"][0]["source"] == "oci_already_absent", stop_cycle
        assert effect_status(layout, stop_effect_id)["status"] == "applied"
        assert load_actor(layout, oci_actor_id)["status"] == "stopped"
        assert not credential.exists()

        # The dead effect status, original durable command failure, and
        # actor/task needs_recovery projection are one rollback boundary.
        run_json(temp, "new-task", "--project", str(project), "--title", "dead-atomic", "--purpose", "fault dead projection")
        dead_atomic = run_json(
            temp,
            "dispatch",
            "--project",
            str(project),
            "--task",
            "V2-0004",
            "--start",
            "--unsafe-native",
            "--command-id",
            "CMD-DEAD-ATOMIC",
        )
        effect_id = dead_atomic["start"]["effect_id"]
        with sqlite3.connect(project / "scheduler" / "state.db") as connection:
            raw_payload = connection.execute(
                "SELECT payload_json FROM effects WHERE effect_id=?",
                (effect_id,),
            ).fetchone()[0]
            corrupt_payload = json.loads(raw_payload)
            corrupt_payload["launch_token_sha256"] = "0" * 64
            canonical = json.dumps(corrupt_payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
            connection.execute(
                "UPDATE effects SET payload_json=?, payload_sha256=? WHERE effect_id=?",
                (canonical, hashlib.sha256(canonical.encode("utf-8")).hexdigest(), effect_id),
            )
            connection.commit()
        dead_crash = run(
            temp,
            "run-scheduler",
            "--project",
            str(project),
            "--once",
            env_extra={"COSTMARSHAL_SCHEDULER_FAULT": "effect.after_dead_status_before_projection"},
            ok=False,
        )
        assert dead_crash.returncode == 96
        assert effect_status(layout, effect_id)["status"] == "leased"
        assert load_actor(layout, dead_atomic["actor_id"])["status"] == "starting"
        time.sleep(2.8)
        run_json(temp, "run-scheduler", "--project", str(project), "--once")
        assert effect_status(layout, effect_id)["status"] == "dead"
        assert load_actor(layout, dead_atomic["actor_id"])["status"] == "needs_recovery"

        # A queued spawn freezes its native runtime identity. Recovery must not
        # accept a syntactically valid actor whose backend/target drifted after
        # the effect commit, even through the registered/observed shortcuts.
        run_json(temp, "new-task", "--project", str(project), "--title", "spawn-binding", "--purpose", "fence runtime identity")
        bound_spawn = run_json(
            temp,
            "dispatch",
            "--project",
            str(project),
            "--task",
            "V2-0005",
            "--start",
            "--unsafe-native",
            "--command-id",
            "CMD-SPAWN-BINDING",
        )
        bound_effect_id = bound_spawn["start"]["effect_id"]
        bound_actor = load_actor(layout, bound_spawn["actor_id"])
        bound_actor["runtime"]["backend"] = "tmux"
        bound_actor["runtime"]["session_name"] = "victim"
        bound_actor["runtime"]["actor_name"] = "leader"
        bound_actor["runtime"]["target"] = "victim:leader"
        with control_transaction(
            layout,
            command_name="test_corrupt_spawn_binding",
            command_id="CMD-TEST-CORRUPT-SPAWN-BINDING",
            payload={"actor_id": bound_actor["id"]},
        ):
            save_actor(layout, bound_actor)
        run_json(temp, "run-scheduler", "--project", str(project), "--once")
        assert effect_status(layout, bound_effect_id)["status"] == "dead"
        assert load_actor(layout, bound_actor["id"])["status"] == "needs_recovery"

        # Pre-runtime-fence beta effects retain launch/profile/attempt fences.
        # Accept the all-or-none legacy shape so an upgrade can safely drain an
        # already committed spawn instead of stranding it permanently.
        run_json(temp, "new-task", "--project", str(project), "--title", "legacy-spawn", "--purpose", "recover legacy effect")
        legacy_spawn = run_json(
            temp,
            "dispatch",
            "--project",
            str(project),
            "--task",
            "V2-0006",
            "--start",
            "--unsafe-native",
            "--command-id",
            "CMD-LEGACY-SPAWN-FENCE",
        )
        legacy_effect_id = legacy_spawn["start"]["effect_id"]
        with sqlite3.connect(project / "scheduler" / "state.db") as connection:
            raw_payload = connection.execute(
                "SELECT payload_json FROM effects WHERE effect_id=?",
                (legacy_effect_id,),
            ).fetchone()[0]
            legacy_payload = json.loads(raw_payload)
            for key in (
                "runtime_backend",
                "runtime_session_name",
                "runtime_actor_name",
                "runtime_target",
            ):
                legacy_payload.pop(key)
            canonical = json.dumps(legacy_payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
            connection.execute(
                "UPDATE effects SET payload_json=?, payload_sha256=? WHERE effect_id=?",
                (canonical, hashlib.sha256(canonical.encode("utf-8")).hexdigest(), legacy_effect_id),
            )
            connection.commit()
        run_json(
            temp,
            "run-scheduler",
            "--project",
            str(project),
            "--once",
            env_extra=provider_env,
        )
        wait_for_counter(counter, 2)
        assert effect_status(layout, legacy_effect_id)["status"] == "applied"
        with sqlite3.connect(project / "scheduler" / "state.db") as connection:
            assert connection.execute(
                "SELECT status FROM commands WHERE command_id='CMD-DEAD-ATOMIC'"
            ).fetchone()[0] == "permanent_failed"
            orphan_effects = int(
                connection.execute(
                    "SELECT COUNT(*) FROM effects WHERE status NOT IN ('applied', 'dead')"
                ).fetchone()[0]
            )
        provider_calls = len(counter.read_text(encoding="utf-8").splitlines())
        assert provider_calls == 2
        assert orphan_effects == 0
        print("runtime recovery reliability ok")
        print(
            "COSTMARSHAL_RUNTIME_EVIDENCE="
            + json.dumps(
                {
                    "schema_version": 1,
                    "test": "tests/runtime_recovery_reliability_test.py",
                    "crash_points": ["effect.after_dead_status_before_projection"],
                    "recovery_scenarios": [
                        "runner_exit_before_provider_start",
                        "oci_stop_after_rm_before_observe",
                        "oci_cleanup_ignores_corrupt_native_binding",
                        "spawn_runtime_identity_drift_rejected",
                        "legacy_spawn_runtime_fence_migration",
                    ],
                    "provider_calls": provider_calls,
                    "expected_provider_calls": 2,
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
