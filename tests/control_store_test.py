from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from costmarshal_v2.control_store import (  # noqa: E402
    ControlStoreConflict,
    ControlStoreError,
    ControlStoreNotEnabled,
    FAULT_ENV,
    control_store_enabled,
    control_store_pragmas,
    control_transaction,
    database_path,
    marker_path,
    migrate_legacy_store,
    preview_legacy_migration,
    reconcile_project_views,
    transactional_append_jsonl,
    transactional_read_jsonl,
    transactional_read_text,
    transactional_write_text,
    validate_control_store,
)
from costmarshal_v2.paths import ProjectLayout  # noqa: E402


def make_project(base: Path, name: str = "project") -> ProjectLayout:
    project = base / name
    (project / "scheduler" / "actors").mkdir(parents=True)
    (project / "tasks" / "V2-0001").mkdir(parents=True)
    (project / "reports").mkdir()
    (project / "project.json").write_text(
        json.dumps({"schema_version": 2, "project_id": name}, indent=2) + "\n",
        encoding="utf-8",
    )
    (project / "scheduler" / "session.json").write_text(
        json.dumps({"schema_version": 2, "actors": {}}, indent=2) + "\n",
        encoding="utf-8",
    )
    (project / "tasks" / "V2-0001" / "status.json").write_text(
        json.dumps({"task_id": "V2-0001", "state": "planned"}, indent=2) + "\n",
        encoding="utf-8",
    )
    (project / "tasks" / "V2-0001" / "brief.md").write_text("# Brief\n", encoding="utf-8")
    (project / "reports" / "usage.jsonl").write_text(
        json.dumps({"id": "USG-legacy", "input_tokens": 3}, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return ProjectLayout(root=base / "runtime", project_dir=project)


class ControlStoreTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="costmarshal-control-store-")
        self.base = Path(self.temporary.name)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_preview_is_read_only_and_cutover_is_explicit(self) -> None:
        layout = make_project(self.base)
        before = (layout.project_json.read_bytes(), layout.session_json.read_bytes())
        preview = preview_legacy_migration(layout)
        self.assertEqual(preview["status"], "preview")
        self.assertGreaterEqual(preview["file_count"], 3)
        self.assertNotIn("project.json", {row["path"] for row in preview["entries"]})
        self.assertNotIn("tasks/V2-0001/brief.md", {row["path"] for row in preview["entries"]})
        self.assertFalse(database_path(layout).exists())
        self.assertFalse(marker_path(layout).exists())
        self.assertFalse(control_store_enabled(layout))
        self.assertEqual(before, (layout.project_json.read_bytes(), layout.session_json.read_bytes()))
        with self.assertRaises(ControlStoreNotEnabled):
            with control_transaction(
                layout,
                command_name="not-enabled",
                command_id="CMD-not-enabled",
                payload={},
            ):
                pass

    def test_migration_backup_wal_pragmas_and_hash_conflict(self) -> None:
        layout = make_project(self.base)
        original_project = layout.project_json.read_bytes()
        original_usage = layout.usage_jsonl.read_bytes()
        migrated = migrate_legacy_store(layout)
        self.assertEqual(migrated["status"], "enabled")
        self.assertTrue(database_path(layout).is_file())
        self.assertTrue(marker_path(layout).is_file())
        self.assertTrue(control_store_enabled(layout))
        backup = Path(migrated["backup"])
        self.assertFalse((backup / "project.json").exists())
        self.assertEqual(layout.project_json.read_bytes(), original_project)
        self.assertEqual((backup / "reports" / "usage.jsonl").read_bytes(), original_usage)
        self.assertTrue((backup / "manifest.json").is_file())

        self.assertEqual(
            control_store_pragmas(layout),
            {"journal_mode": "wal", "synchronous": 2, "trusted_schema": 0, "foreign_keys": 1},
        )

        status_path = layout.tasks_dir / "V2-0001" / "status.json"
        replacement = json.dumps({"task_id": "V2-0001", "state": "waiting_leader"}, indent=2) + "\n"
        with control_transaction(
            layout,
            command_name="collect",
            command_id="CMD-one",
            payload={"task": "V2-0001", "state": "waiting_leader"},
        ) as transaction:
            self.assertFalse(transaction.replay)
            transaction.write_document(status_path, replacement)
            transaction.append_ledger(
                layout.usage_jsonl,
                {"id": "USG-new", "input_tokens": 5},
                entry_id="USG-new",
            )
            transaction.queue_outbox(
                outbox_id="OUT-one",
                channel="actor_mailbox",
                recipient="leader",
                payload={"task": "V2-0001"},
                dedupe_key="CMD-one:leader",
            )
            transaction.set_result({"task": "V2-0001"})
        self.assertEqual(status_path.read_text(encoding="utf-8"), replacement)
        usage_rows = [json.loads(line) for line in layout.usage_jsonl.read_text(encoding="utf-8").splitlines()]
        self.assertEqual([row["id"] for row in usage_rows], ["USG-legacy", "USG-new"])

        with control_transaction(
            layout,
            command_name="collect",
            command_id="CMD-one",
            payload={"state": "waiting_leader", "task": "V2-0001"},
        ) as replay:
            self.assertTrue(replay.replay)
            self.assertEqual(replay.replay_result, {"task": "V2-0001"})
        with self.assertRaises(ControlStoreConflict):
            with control_transaction(
                layout,
                command_name="collect",
                command_id="CMD-one",
                payload={"task": "V2-0001", "state": "failed"},
            ):
                pass

        validation = validate_control_store(layout)
        self.assertEqual(validation["status"], "ok", validation)
        self.assertEqual(validation["counts"]["dirty_views"], 0)
        self.assertEqual(validation["counts"]["commands"], 1)
        self.assertEqual(validation["counts"]["outbox"], 1)

    def test_hard_exit_after_commit_recovers_dirty_view_without_reapplying_command(self) -> None:
        layout = make_project(self.base)
        migrate_legacy_store(layout)
        status_path = layout.tasks_dir / "V2-0001" / "status.json"
        old_content = status_path.read_text(encoding="utf-8")
        child = """
import json
import sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
from costmarshal_v2.control_store import control_transaction
from costmarshal_v2.paths import ProjectLayout
project = Path(sys.argv[2])
layout = ProjectLayout(root=project.parent, project_dir=project)
with control_transaction(layout, command_name='hard-crash', command_id='CMD-hard', payload={'v': 1}) as tx:
    tx.write_document(project / 'tasks' / 'V2-0001' / 'status.json', json.dumps({'task_id': 'V2-0001', 'state': 'failed'}, indent=2) + '\\n')
    tx.set_result({'committed': True})
raise AssertionError('fault point did not exit')
"""
        environment = dict(os.environ)
        environment[FAULT_ENV] = "transaction.after_commit_before_materialize"
        completed = subprocess.run(
            [sys.executable, "-c", child, str(ROOT), str(layout.project_dir)],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=environment,
            check=False,
        )
        self.assertEqual(completed.returncode, 86, completed.stderr)
        self.assertEqual(status_path.read_text(encoding="utf-8"), old_content)
        staged = validate_control_store(layout)
        self.assertEqual(staged["status"], "ok", staged)
        self.assertEqual(staged["counts"]["dirty_views"], 1)
        recovered = reconcile_project_views(layout)
        self.assertIn("tasks/V2-0001/status.json", recovered["materialized"])
        self.assertEqual(json.loads(status_path.read_text(encoding="utf-8"))["state"], "failed")
        with control_transaction(
            layout,
            command_name="hard-crash",
            command_id="CMD-hard",
            payload={"v": 1},
        ) as replay:
            self.assertTrue(replay.replay)
            self.assertEqual(replay.replay_result, {"committed": True})

    def test_hard_exit_before_marker_resumes_validated_cutover(self) -> None:
        layout = make_project(self.base, "migration-crash")
        child = """
import sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
from costmarshal_v2.control_store import migrate_legacy_store
from costmarshal_v2.paths import ProjectLayout
project = Path(sys.argv[2])
migrate_legacy_store(ProjectLayout(root=project.parent, project_dir=project))
raise AssertionError('fault point did not exit')
"""
        environment = dict(os.environ)
        environment[FAULT_ENV] = "migration.after_database_install_before_marker"
        completed = subprocess.run(
            [sys.executable, "-c", child, str(ROOT), str(layout.project_dir)],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=environment,
            check=False,
        )
        self.assertEqual(completed.returncode, 86, completed.stderr)
        self.assertTrue(database_path(layout).is_file())
        self.assertFalse(marker_path(layout).exists())
        staged = validate_control_store(layout)
        self.assertEqual(staged["status"], "staged", staged)
        resumed = migrate_legacy_store(layout)
        self.assertTrue(resumed["resumed"])
        self.assertTrue(marker_path(layout).is_file())
        self.assertEqual(validate_control_store(layout)["status"], "ok")

    def test_foreign_key_validation_reports_corruption(self) -> None:
        layout = make_project(self.base)
        migrate_legacy_store(layout)
        connection = sqlite3.connect(database_path(layout))
        try:
            connection.execute("PRAGMA foreign_keys=OFF")
            connection.execute(
                "INSERT INTO effects(effect_id, command_id, effect_type, aggregate_id, generation, "
                "payload_json, status, created_at, updated_at) "
                "VALUES('EFF-bad', 'CMD-missing', 'spawn_actor', 'actor', 1, '{}', 'pending', 'now', 'now')"
            )
            connection.commit()
        finally:
            connection.close()
        validation = validate_control_store(layout)
        self.assertEqual(validation["status"], "invalid")
        self.assertTrue(any("foreign-key" in issue for issue in validation["issues"]), validation)

    def test_transaction_hooks_decline_artifacts_and_external_paths(self) -> None:
        layout = make_project(self.base)
        migrate_legacy_store(layout)
        markdown = layout.tasks_dir / "V2-0001" / "completion-report.md"
        external = self.base / "external-profile.json"
        with control_transaction(
            layout,
            command_name="hook-boundary",
            command_id="CMD-hook-boundary",
            payload={},
        ):
            self.assertFalse(transactional_write_text(markdown, "provider output"))
            self.assertEqual(transactional_read_text(markdown), (False, None))
            self.assertFalse(transactional_write_text(external, "{}"))
            self.assertEqual(transactional_read_text(external), (False, None))
            self.assertFalse(transactional_append_jsonl(markdown, {"not": "a ledger"}))
            self.assertEqual(transactional_read_jsonl(markdown), (False, []))
        self.assertFalse(markdown.exists())
        self.assertFalse(external.exists())

    def test_migration_rejects_active_actor(self) -> None:
        layout = make_project(self.base)
        actor = layout.actors_dir / "agent-v2-0001.json"
        actor.write_text(
            json.dumps({"id": "agent-v2-0001", "status": "running", "runtime": {"pid": None}}),
            encoding="utf-8",
        )
        preview = preview_legacy_migration(layout)
        self.assertTrue(preview["actor_blockers"])
        with self.assertRaisesRegex(ControlStoreError, "quiescent project"):
            migrate_legacy_store(layout)
        self.assertFalse(database_path(layout).exists())
        self.assertFalse(marker_path(layout).exists())


if __name__ == "__main__":
    unittest.main(verbosity=2)
