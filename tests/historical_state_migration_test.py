from __future__ import annotations

import json
import contextlib
import io
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any
from types import SimpleNamespace


ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tests" / "fixtures" / "state_migrations"
sys.path.insert(0, str(ROOT))

from costmarshal_v2.control_store import (  # noqa: E402
    control_transaction,
    database_path,
    migrate_legacy_store,
    preview_legacy_migration,
    validate_control_store,
)
from costmarshal_v2.paths import ProjectLayout  # noqa: E402
from costmarshal_v2.scheduler import command_budget_status  # noqa: E402


RELEASE_FIXTURES = ("v2_0.json", "v2_1.json", "v2_2.json", "v2_3.json")


def load_fixture(name: str) -> dict[str, Any]:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def materialize_fixture(base: Path, fixture: dict[str, Any]) -> tuple[ProjectLayout, dict[str, bytes]]:
    project = base / str(fixture["release"]).replace(".", "-")
    original: dict[str, bytes] = {}
    for relative, document in fixture["documents"].items():
        path = project / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        content = json.dumps(document, ensure_ascii=False, indent=2) + "\n"
        path.write_text(content, encoding="utf-8")
        # Capture the real compatibility-view bytes. Windows text mode expands
        # newlines, and migration must preserve what is actually on disk.
        original[relative] = path.read_bytes()
    for relative, rows in fixture["ledgers"].items():
        path = project / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        content = "".join(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n" for row in rows)
        path.write_text(content, encoding="utf-8")
        original[relative] = path.read_bytes()
    return ProjectLayout(root=base / "runtime", project_dir=project), original


def database_snapshot(layout: ProjectLayout) -> tuple[dict[str, Any], dict[str, list[Any]]]:
    connection = sqlite3.connect(database_path(layout))
    try:
        documents = {
            str(path): json.loads(str(content))
            for path, content in connection.execute("SELECT path, content FROM documents ORDER BY path")
        }
        ledgers: dict[str, list[Any]] = {}
        for path, content in connection.execute(
            "SELECT path, content FROM ledger_entries ORDER BY path, sequence"
        ):
            ledgers.setdefault(str(path), []).append(json.loads(str(content)))
        for path, in connection.execute("SELECT DISTINCT path FROM ledger_entries ORDER BY path"):
            ledgers.setdefault(str(path), [])
        return documents, ledgers
    finally:
        connection.close()


class HistoricalStateMigrationTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="costmarshal-historical-migration-")
        self.base = Path(self.temporary.name)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_v2_release_snapshots_migrate_without_state_loss(self) -> None:
        for fixture_name in RELEASE_FIXTURES:
            fixture = load_fixture(fixture_name)
            with self.subTest(release=fixture["release"], source_commit=fixture["source_commit"]):
                layout, original = materialize_fixture(self.base, fixture)
                preview = preview_legacy_migration(layout)
                controlled_documents = {
                    path: value
                    for path, value in fixture["documents"].items()
                    if path != "project.json"
                }
                self.assertEqual(preview["actor_blockers"], [])
                self.assertEqual(
                    {entry["path"] for entry in preview["entries"]},
                    set(controlled_documents) | set(fixture["ledgers"]),
                )
                self.assertEqual(
                    preview["ledger_row_count"],
                    sum(len(rows) for rows in fixture["ledgers"].values()),
                )

                migrated = migrate_legacy_store(layout)
                self.assertEqual(migrated["status"], "enabled", migrated)
                self.assertEqual(validate_control_store(layout)["status"], "ok")
                self.assertEqual(layout.project_json.read_bytes(), original["project.json"])

                if fixture["release"] == "v2.2.0-beta":
                    output = io.StringIO()
                    with contextlib.redirect_stdout(output):
                        command_budget_status(
                            SimpleNamespace(root=layout.root, project=str(layout.project_dir))
                        )
                    budget = json.loads(output.getvalue())
                    self.assertEqual(budget["status"], "blocked")
                    self.assertIsNone(budget["commitment_cny"])
                    self.assertEqual(
                        budget["attempts"][0]["reconciliation_status"], "unknown"
                    )

                backup = Path(migrated["backup"])
                self.assertFalse((backup / "project.json").exists())
                for relative in set(controlled_documents) | set(fixture["ledgers"]):
                    self.assertEqual((backup / relative).read_bytes(), original[relative], relative)

                stored_documents, stored_ledgers = database_snapshot(layout)
                self.assertEqual(stored_documents, controlled_documents)
                # Empty JSONL files intentionally have no rows in the normalized ledger table.
                expected_nonempty_ledgers = {
                    path: rows for path, rows in fixture["ledgers"].items() if rows
                }
                self.assertEqual(stored_ledgers, expected_nonempty_ledgers)

                status_path = layout.tasks_dir / "V2-0001" / "status.json"
                status = json.loads(status_path.read_text(encoding="utf-8"))
                status["migration_probe"] = fixture["release"]
                replacement = json.dumps(status, ensure_ascii=False, indent=2) + "\n"
                with control_transaction(
                    layout,
                    command_name="historical-migration-probe",
                    command_id=f"CMD-{fixture['release']}-probe",
                    payload={"release": fixture["release"]},
                ) as transaction:
                    transaction.write_document(status_path, replacement)
                    transaction.set_result({"release": fixture["release"]})
                self.assertEqual(status_path.read_text(encoding="utf-8"), replacement)
                self.assertEqual(layout.project_json.read_bytes(), original["project.json"])

    def test_fixtures_pin_the_expected_historical_capability_transitions(self) -> None:
        fixtures = {fixture["release"]: fixture for fixture in map(load_fixture, RELEASE_FIXTURES)}
        v20_documents = fixtures["v2.0.0"]["documents"]
        v21_documents = fixtures["v2.1.0-beta"]["documents"]
        v22_task = fixtures["v2.2.0-beta"]["documents"]["tasks/V2-0001/task.json"]
        v23 = fixtures["v2.3.0-beta"]
        v23_project = v23["documents"]["project.json"]
        v23_attempt = v23["documents"]["tasks/V2-0001/task.json"]["attempts"][0]

        self.assertNotIn("scheduler/state.json", v20_documents)
        self.assertIn("scheduler/state.json", v21_documents)
        self.assertEqual(v22_task["attempts"][0]["provider"], "longcat")
        self.assertNotIn("reserved_cost_cny", v22_task["attempts"][0])
        self.assertEqual(v23_project["routing_policy"]["tier_order"], ["low", "medium", "high"])
        self.assertEqual(v23_project["governance"]["provider"], "archmarshal")
        self.assertEqual(v23_attempt["reserved_cost_cny"], 0.75)
        self.assertEqual(v23_attempt["actual_cost_cny"], 0.42)


if __name__ == "__main__":
    unittest.main(verbosity=2)
