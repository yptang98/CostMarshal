from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from costmarshal_v2.control_store import (  # noqa: E402
    ControlStoreConflict,
    ControlStoreError,
    FAULT_ENV,
    STORE_SCHEMA_VERSION,
    apply_effect,
    control_transaction,
    database_path,
    dead_effect,
    effect_status,
    fail_effect,
    lease_effect,
    marker_path,
    migrate_legacy_store,
    observe_effect,
    reconcile_project_views,
    renew_effect_lease,
    upgrade_control_store,
    validate_control_store,
)
from costmarshal_v2.paths import ProjectLayout  # noqa: E402
import costmarshal_v2.control_store as control_store_module  # noqa: E402


def make_layout(base: Path, name: str) -> ProjectLayout:
    project = base / name
    (project / "scheduler" / "actors").mkdir(parents=True)
    (project / "project.json").write_text(
        json.dumps({"schema_version": 2, "project_id": name}) + "\n", encoding="utf-8"
    )
    (project / "scheduler" / "session.json").write_text(
        json.dumps({"schema_version": 2, "actors": {}}) + "\n", encoding="utf-8"
    )
    return ProjectLayout(root=base / "runtime", project_dir=project)


def prepare_effect(layout: ProjectLayout, command_id: str, effect_id: str) -> None:
    with control_transaction(
        layout,
        command_name="dispatch",
        command_id=command_id,
        payload={"actor": "agent-v2-0001", "start": True},
    ) as transaction:
        transaction.queue_effect(
            effect_id=effect_id,
            effect_type="spawn_actor",
            aggregate_id="agent-v2-0001",
            generation=1,
            payload={"attempt_id": "ATT-1", "launch_token": "TOKEN-1"},
        )
        transaction.set_result({"effect_id": effect_id})


def command_status(layout: ProjectLayout, command_id: str) -> str:
    connection = sqlite3.connect(database_path(layout))
    try:
        row = connection.execute(
            "SELECT status FROM commands WHERE command_id=?", (command_id,)
        ).fetchone()
        if row is None:
            raise AssertionError(f"missing command {command_id}")
        return str(row[0])
    finally:
        connection.close()


class RuntimeEffectStoreTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="costmarshal-effect-store-")
        self.base = Path(self.temporary.name)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_queue_effect_fences_command_and_rejects_payload_or_generation_reuse(self) -> None:
        layout = make_layout(self.base, "queue")
        migrate_legacy_store(layout)
        prepare_effect(layout, "CMD-spawn", "EFF-spawn")
        self.assertEqual(command_status(layout, "CMD-spawn"), "awaiting_effect")
        self.assertEqual(effect_status(layout, "EFF-spawn")["status"], "pending")
        with control_transaction(
            layout,
            command_name="dispatch",
            command_id="CMD-spawn",
            payload={"start": True, "actor": "agent-v2-0001"},
        ) as replay:
            self.assertTrue(replay.replay)
            self.assertEqual(replay.replay_status, "awaiting_effect")
            self.assertEqual(replay.replay_result, {"effect_id": "EFF-spawn"})

        with self.assertRaises(ControlStoreConflict):
            with control_transaction(
                layout,
                command_name="dispatch",
                command_id="CMD-conflict",
                payload={"actor": "agent-v2-0002"},
            ) as transaction:
                transaction.queue_effect(
                    effect_id="EFF-first",
                    effect_type="spawn_actor",
                    aggregate_id="agent-v2-0002",
                    generation=1,
                    payload={"launch_token": "one"},
                )
                transaction.queue_effect(
                    effect_id="EFF-second",
                    effect_type="spawn_actor",
                    aggregate_id="agent-v2-0002",
                    generation=1,
                    payload={"launch_token": "two"},
                )
        connection = sqlite3.connect(database_path(layout))
        try:
            self.assertIsNone(
                connection.execute(
                    "SELECT command_id FROM commands WHERE command_id='CMD-conflict'"
                ).fetchone()
            )
        finally:
            connection.close()

    def test_hard_exit_after_lease_commit_is_recovered_by_expiry_and_re_lease(self) -> None:
        layout = make_layout(self.base, "lease-crash")
        migrate_legacy_store(layout)
        prepare_effect(layout, "CMD-lease", "EFF-lease")
        child = """
import sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
from costmarshal_v2.control_store import lease_effect
from costmarshal_v2.paths import ProjectLayout
project = Path(sys.argv[2])
lease_effect(ProjectLayout(root=project.parent, project_dir=project), owner='worker-one', ttl_seconds=0.15)
raise AssertionError('fault did not exit')
"""
        environment = dict(os.environ)
        environment[FAULT_ENV] = "effect.after_lease_commit_before_spawn"
        completed = subprocess.run(
            [sys.executable, "-c", child, str(ROOT), str(layout.project_dir)],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=environment,
            check=False,
        )
        self.assertEqual(completed.returncode, 86, completed.stderr)
        first = effect_status(layout, "EFF-lease")
        self.assertEqual(first["status"], "leased")
        self.assertEqual(first["lease_owner"], "worker-one")
        self.assertEqual(first["attempts"], 1)
        time.sleep(0.2)
        validation = validate_control_store(layout)
        self.assertEqual(validation["status"], "degraded", validation)
        self.assertEqual(validation["expired_effect_leases"], ["EFF-lease"])
        second = lease_effect(layout, owner="worker-two", ttl_seconds=5)
        self.assertIsNotNone(second)
        assert second is not None
        self.assertEqual(second["effect_id"], "EFF-lease")
        self.assertEqual(second["lease_owner"], "worker-two")
        self.assertEqual(second["attempts"], 2)
        self.assertEqual(validate_control_store(layout)["status"], "ok")

    def test_live_owner_renews_lease_without_changing_attempt(self) -> None:
        layout = make_layout(self.base, "lease-renew")
        migrate_legacy_store(layout)
        prepare_effect(layout, "CMD-renew", "EFF-renew")
        first = lease_effect(layout, owner="worker-one", ttl_seconds=0.2)
        assert first is not None
        time.sleep(0.1)
        renewed = renew_effect_lease(
            layout,
            effect_id="EFF-renew",
            owner="worker-one",
            ttl_seconds=0.3,
        )
        self.assertEqual(renewed["lease_owner"], "worker-one")
        self.assertEqual(renewed["attempts"], 1)
        with self.assertRaises(ControlStoreConflict):
            renew_effect_lease(
                layout,
                effect_id="EFF-renew",
                owner="worker-two",
                ttl_seconds=1,
            )
        time.sleep(0.15)
        self.assertIsNone(lease_effect(layout, owner="worker-two", ttl_seconds=1))

    def test_materializer_lock_closes_revision_aba_window(self) -> None:
        layout = make_layout(self.base, "materializer-aba")
        migrate_legacy_store(layout)
        relative = "scheduler/session.json"
        old_payload = {"schema_version": 2, "actors": {}, "note": "OLD"}
        new_payload = {"schema_version": 2, "actors": {}, "note": "NEW"}

        def write_canonical(payload: dict[str, object]) -> None:
            content = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
            connection = sqlite3.connect(database_path(layout), timeout=5)
            try:
                connection.execute("BEGIN IMMEDIATE")
                connection.execute(
                    "UPDATE documents SET content=?, content_sha256=?, updated_at=? WHERE path=?",
                    (
                        content,
                        hashlib.sha256(content.encode("utf-8")).hexdigest(),
                        "audit",
                        relative,
                    ),
                )
                connection.execute(
                    "INSERT INTO dirty_views(path, kind, revision, updated_at) "
                    "VALUES(?, 'document', 1, 'audit') ON CONFLICT(path) DO UPDATE SET "
                    "kind='document', revision=dirty_views.revision+1, updated_at='audit'",
                    (relative,),
                )
                connection.commit()
            finally:
                connection.close()

        write_canonical(old_payload)
        paused = threading.Event()
        release = threading.Event()
        errors: list[BaseException] = []
        original_fault = control_store_module._fault

        def fault(name: str) -> None:
            if (
                name == "materialize.after_file_before_ack"
                and threading.current_thread().name == "materializer-a"
                and not paused.is_set()
            ):
                paused.set()
                if not release.wait(5):
                    raise AssertionError("materializer ABA barrier timed out")

        def materialize() -> None:
            try:
                reconcile_project_views(layout)
            except BaseException as exc:
                errors.append(exc)

        control_store_module._fault = fault
        first = threading.Thread(target=materialize, name="materializer-a")
        second = threading.Thread(target=materialize, name="materializer-b")
        try:
            first.start()
            self.assertTrue(paused.wait(5))
            second.start()
            time.sleep(0.1)
            self.assertTrue(second.is_alive(), "second materializer bypassed the cross-process lock")
            write_canonical(new_payload)
            release.set()
            first.join(5)
            second.join(5)
        finally:
            release.set()
            first.join(5)
            second.join(5)
            control_store_module._fault = original_fault
        self.assertFalse(first.is_alive())
        self.assertFalse(second.is_alive())
        self.assertEqual(errors, [])
        connection = sqlite3.connect(database_path(layout))
        try:
            canonical = json.loads(
                connection.execute(
                    "SELECT content FROM documents WHERE path=?", (relative,)
                ).fetchone()[0]
            )
            dirty = int(connection.execute("SELECT COUNT(*) FROM dirty_views").fetchone()[0])
        finally:
            connection.close()
        materialized = json.loads(layout.session_json.read_text(encoding="utf-8"))
        self.assertEqual(canonical["note"], "NEW")
        self.assertEqual(materialized["note"], "NEW")
        self.assertEqual(dirty, 0)

    def test_materializer_retries_transient_atomic_replace_sharing_failure(self) -> None:
        layout = make_layout(self.base, "replace-retry")
        migrate_legacy_store(layout)
        actor_path = layout.project_dir / "scheduler" / "actors" / "agent-v2-0001.json"
        original_replace = control_store_module.os.replace
        attempts = 0

        def sharing_conflict_then_replace(source: object, destination: object) -> None:
            nonlocal attempts
            if Path(destination).resolve() == actor_path.resolve():
                attempts += 1
                if attempts <= 2:
                    raise PermissionError(
                        5,
                        "simulated transient sharing violation",
                        str(destination),
                    )
            original_replace(source, destination)

        control_store_module.os.replace = sharing_conflict_then_replace
        try:
            with control_transaction(
                layout,
                command_name="replace_retry",
                command_id="CMD-REPLACE-RETRY",
                payload={"actor": "agent-v2-0001"},
            ) as transaction:
                transaction.write_document(
                    actor_path,
                    json.dumps(
                        {
                            "schema_version": 2,
                            "id": "agent-v2-0001",
                            "state": "ready",
                        }
                    )
                    + "\n",
                )
                transaction.set_result({"status": "ok"})
        finally:
            control_store_module.os.replace = original_replace

        self.assertEqual(attempts, 3)
        self.assertEqual(
            json.loads(actor_path.read_text(encoding="utf-8"))["state"],
            "ready",
        )
        connection = sqlite3.connect(database_path(layout))
        try:
            self.assertEqual(
                connection.execute("SELECT COUNT(*) FROM dirty_views").fetchone()[0],
                0,
            )
        finally:
            connection.close()

    def test_materializer_bounds_persistent_atomic_replace_failure(self) -> None:
        layout = make_layout(self.base, "replace-fail-closed")
        migrate_legacy_store(layout)
        actor_path = layout.project_dir / "scheduler" / "actors" / "agent-v2-0001.json"
        files_before = set(actor_path.parent.iterdir())
        original_replace = control_store_module.os.replace
        original_retry = control_store_module.ATOMIC_REPLACE_RETRY_SECONDS
        original_initial = control_store_module.ATOMIC_REPLACE_INITIAL_DELAY_SECONDS
        original_maximum = control_store_module.ATOMIC_REPLACE_MAX_DELAY_SECONDS
        attempts = 0

        def persistent_sharing_conflict(source: object, destination: object) -> None:
            nonlocal attempts
            if Path(destination).resolve() == actor_path.resolve():
                attempts += 1
                raise PermissionError(
                    5,
                    "simulated persistent sharing violation",
                    str(destination),
                )
            original_replace(source, destination)

        control_store_module.os.replace = persistent_sharing_conflict
        control_store_module.ATOMIC_REPLACE_RETRY_SECONDS = 0.03
        control_store_module.ATOMIC_REPLACE_INITIAL_DELAY_SECONDS = 0.005
        control_store_module.ATOMIC_REPLACE_MAX_DELAY_SECONDS = 0.01
        started = time.monotonic()
        try:
            with self.assertRaises(PermissionError):
                with control_transaction(
                    layout,
                    command_name="replace_fail_closed",
                    command_id="CMD-REPLACE-FAIL-CLOSED",
                    payload={"actor": "agent-v2-0001"},
                ) as transaction:
                    transaction.write_document(
                        actor_path,
                        json.dumps(
                            {
                                "schema_version": 2,
                                "id": "agent-v2-0001",
                                "state": "ready",
                            }
                        )
                        + "\n",
                    )
                    transaction.set_result({"status": "ok"})
        finally:
            elapsed = time.monotonic() - started
            control_store_module.os.replace = original_replace
            control_store_module.ATOMIC_REPLACE_RETRY_SECONDS = original_retry
            control_store_module.ATOMIC_REPLACE_INITIAL_DELAY_SECONDS = original_initial
            control_store_module.ATOMIC_REPLACE_MAX_DELAY_SECONDS = original_maximum

        self.assertGreaterEqual(attempts, 3)
        self.assertLess(elapsed, 1.0)
        self.assertEqual(set(actor_path.parent.iterdir()), files_before)
        connection = sqlite3.connect(database_path(layout))
        try:
            self.assertEqual(
                connection.execute(
                    "SELECT status FROM commands WHERE command_id='CMD-REPLACE-FAIL-CLOSED'"
                ).fetchone()[0],
                "completed",
            )
            self.assertEqual(
                connection.execute("SELECT COUNT(*) FROM dirty_views").fetchone()[0],
                1,
            )
        finally:
            connection.close()

    def test_hard_exit_after_registration_finalizes_effect_and_command_atomically(self) -> None:
        layout = make_layout(self.base, "registration-crash")
        migrate_legacy_store(layout)
        prepare_effect(layout, "CMD-register", "EFF-register")
        lease = lease_effect(layout, owner="runtime-one", ttl_seconds=10)
        self.assertIsNotNone(lease)
        child = """
import sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
from costmarshal_v2.control_store import observe_effect
from costmarshal_v2.paths import ProjectLayout
project = Path(sys.argv[2])
observe_effect(
    ProjectLayout(root=project.parent, project_dir=project),
    effect_id='EFF-register', owner='runtime-one',
    observation={'pid': 4321, 'start_marker': 'boot-1'},
)
raise AssertionError('fault did not exit')
"""
        environment = dict(os.environ)
        environment[FAULT_ENV] = "effect.after_registration_before_finalize"
        completed = subprocess.run(
            [sys.executable, "-c", child, str(ROOT), str(layout.project_dir)],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=environment,
            check=False,
        )
        self.assertEqual(completed.returncode, 86, completed.stderr)
        observed = effect_status(layout, "EFF-register")
        self.assertEqual(observed["status"], "observed")
        self.assertEqual(observed["observation"]["pid"], 4321)
        self.assertEqual(command_status(layout, "CMD-register"), "awaiting_effect")
        applied = apply_effect(
            layout,
            effect_id="EFF-register",
            owner="runtime-one",
            result={"runtime_target": "pid:4321"},
            command_result={"actor_id": "agent-v2-0001", "started": True},
        )
        self.assertEqual(applied["status"], "applied")
        self.assertEqual(command_status(layout, "CMD-register"), "completed")
        replay = apply_effect(
            layout,
            effect_id="EFF-register",
            owner="runtime-one",
            result={"runtime_target": "pid:4321"},
            command_result={"actor_id": "agent-v2-0001", "started": True},
        )
        self.assertEqual(replay["status"], "applied")

    def test_retryable_and_dead_failures_update_command_with_effect(self) -> None:
        layout = make_layout(self.base, "failures")
        migrate_legacy_store(layout)
        prepare_effect(layout, "CMD-fail", "EFF-fail")
        lease_effect(layout, owner="worker", ttl_seconds=10)
        retry = fail_effect(
            layout,
            effect_id="EFF-fail",
            owner="worker",
            error="backend unavailable",
            retryable=True,
        )
        self.assertEqual(retry["status"], "retryable_failed")
        self.assertEqual(command_status(layout, "CMD-fail"), "awaiting_effect")
        lease_effect(layout, owner="worker-two", ttl_seconds=10)
        dead = dead_effect(
            layout,
            effect_id="EFF-fail",
            owner="worker-two",
            error="invalid executable",
        )
        self.assertEqual(dead["status"], "dead")
        self.assertEqual(command_status(layout, "CMD-fail"), "permanent_failed")

    def test_expired_observed_effect_is_released_to_a_recovery_owner(self) -> None:
        layout = make_layout(self.base, "observed-recovery")
        migrate_legacy_store(layout)
        prepare_effect(layout, "CMD-observed", "EFF-observed")
        lease_effect(layout, owner="worker-one", ttl_seconds=0.15)
        observe_effect(
            layout,
            effect_id="EFF-observed",
            owner="worker-one",
            observation={"pid": 1234, "start_marker": "same-process"},
        )
        time.sleep(0.2)
        validation = validate_control_store(layout)
        self.assertEqual(validation["expired_effect_leases"], ["EFF-observed"])
        recovered = lease_effect(layout, owner="worker-two", ttl_seconds=5)
        self.assertIsNotNone(recovered)
        assert recovered is not None
        self.assertEqual(recovered["status"], "observed")
        self.assertEqual(recovered["lease_owner"], "worker-two")
        self.assertEqual(recovered["observation"]["start_marker"], "same-process")
        applied = apply_effect(
            layout,
            effect_id="EFF-observed",
            owner="worker-two",
            result={"runtime_target": "pid:1234"},
        )
        self.assertEqual(applied["status"], "applied")
        self.assertEqual(command_status(layout, "CMD-observed"), "completed")

    def test_schema_one_store_is_forward_migrated_before_use(self) -> None:
        layout = make_layout(self.base, "upgrade")
        migrate_legacy_store(layout)
        connection = sqlite3.connect(database_path(layout))
        try:
            connection.execute("ALTER TABLE effects DROP COLUMN observation_json")
            connection.execute("ALTER TABLE effects DROP COLUMN payload_sha256")
            connection.execute("PRAGMA user_version=1")
            connection.execute("UPDATE meta SET value='1' WHERE key='schema_version'")
            connection.commit()
        finally:
            connection.close()
        marker = json.loads(marker_path(layout).read_text(encoding="utf-8"))
        marker["schema_version"] = 1
        marker_path(layout).write_text(json.dumps(marker) + "\n", encoding="utf-8")
        upgraded = upgrade_control_store(layout)
        self.assertEqual(upgraded["previous_version"], 1)
        self.assertEqual(upgraded["schema_version"], STORE_SCHEMA_VERSION)
        connection = sqlite3.connect(database_path(layout))
        try:
            columns = {row[1] for row in connection.execute("PRAGMA table_info(effects)")}
            self.assertIn("payload_sha256", columns)
            self.assertIn("observation_json", columns)
            self.assertEqual(int(connection.execute("PRAGMA user_version").fetchone()[0]), STORE_SCHEMA_VERSION)
        finally:
            connection.close()
        self.assertEqual(
            json.loads(marker_path(layout).read_text(encoding="utf-8"))["schema_version"],
            STORE_SCHEMA_VERSION,
        )
        self.assertEqual(validate_control_store(layout)["status"], "ok")

    def test_schema_hash_backfill_crash_rolls_back_and_retries_cleanly(self) -> None:
        layout = make_layout(self.base, "upgrade-crash")
        migrate_legacy_store(layout)
        prepare_effect(layout, "CMD-one", "EFF-one")
        with control_transaction(
            layout,
            command_name="dispatch",
            command_id="CMD-two",
            payload={"actor": "agent-v2-0002", "start": True},
        ) as transaction:
            transaction.queue_effect(
                effect_id="EFF-two",
                effect_type="spawn_actor",
                aggregate_id="agent-v2-0002",
                generation=1,
                payload={"attempt_id": "ATT-2", "launch_token": "TOKEN-2"},
            )
            transaction.set_result({"effect_id": "EFF-two"})
        connection = sqlite3.connect(database_path(layout))
        try:
            connection.execute("ALTER TABLE effects DROP COLUMN observation_json")
            connection.execute("ALTER TABLE effects DROP COLUMN payload_sha256")
            connection.execute("PRAGMA user_version=1")
            connection.execute("UPDATE meta SET value='1' WHERE key='schema_version'")
            connection.commit()
        finally:
            connection.close()
        marker = json.loads(marker_path(layout).read_text(encoding="utf-8"))
        marker["schema_version"] = 1
        marker_path(layout).write_text(json.dumps(marker) + "\n", encoding="utf-8")
        child = """
import sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
from costmarshal_v2.control_store import upgrade_control_store
from costmarshal_v2.paths import ProjectLayout
project = Path(sys.argv[2])
upgrade_control_store(ProjectLayout(root=project.parent, project_dir=project))
raise AssertionError('fault did not exit')
"""
        environment = dict(os.environ)
        environment[FAULT_ENV] = "schema.after_effect_hash_backfill_row"
        crashed = subprocess.run(
            [sys.executable, "-c", child, str(ROOT), str(layout.project_dir)],
            env=environment,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            check=False,
        )
        self.assertEqual(crashed.returncode, 86, crashed.stderr)
        connection = sqlite3.connect(database_path(layout))
        try:
            columns = {row[1] for row in connection.execute("PRAGMA table_info(effects)")}
            self.assertNotIn("payload_sha256", columns)
            self.assertEqual(int(connection.execute("PRAGMA user_version").fetchone()[0]), 1)
        finally:
            connection.close()
        self.assertEqual(upgrade_control_store(layout)["schema_version"], STORE_SCHEMA_VERSION)
        self.assertEqual(validate_control_store(layout)["status"], "ok")

    def test_current_schema_hash_mismatch_is_not_auto_repaired(self) -> None:
        layout = make_layout(self.base, "current-tamper")
        migrate_legacy_store(layout)
        prepare_effect(layout, "CMD-tamper", "EFF-tamper")
        connection = sqlite3.connect(database_path(layout))
        try:
            connection.execute(
                "UPDATE effects SET payload_json='{}' WHERE effect_id='EFF-tamper'"
            )
            connection.commit()
        finally:
            connection.close()
        with self.assertRaisesRegex(ControlStoreError, "payload hash mismatch"):
            upgrade_control_store(layout)
        validation = validate_control_store(layout)
        self.assertEqual(validation["status"], "invalid")
        self.assertTrue(any("payload hash mismatch" in issue for issue in validation["issues"]))


if __name__ == "__main__":
    suite = unittest.defaultTestLoader.loadTestsFromTestCase(RuntimeEffectStoreTest)
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    if result.wasSuccessful():
        receipt = {
            "schema_version": 1,
            "test": "tests/runtime_effect_store_test.py",
            "crash_points": [
                "effect.after_lease_commit_before_spawn",
                "effect.after_registration_before_finalize",
                "schema.after_effect_hash_backfill_row",
            ],
                    "recovery_scenarios": [
                        "materializer_revision_aba_serialized",
                        "materializer_transient_sharing_retry_bounded",
                    ],
            "provider_calls": 0,
            "expected_provider_calls": 0,
            "orphan_effects": 0,
        }
        print("COSTMARSHAL_RUNTIME_EVIDENCE=" + json.dumps(receipt, sort_keys=True))
    raise SystemExit(0 if result.wasSuccessful() else 1)
