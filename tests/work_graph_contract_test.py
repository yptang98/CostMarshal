from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from costmarshal_v2.paths import ProjectLayout
from costmarshal_v2.state import ensure_runtime_dirs
from costmarshal_v2.work_graph import (
    WorkGraphError,
    dispatch_blockers,
    ready_task_ids,
    register_task,
    sync_task_node,
)


def task(task_id: str, *, dependencies: list[str] | None = None) -> dict:
    return {
        "id": task_id,
        "role": "builder",
        "dependencies": dependencies or [],
        "status": "planned",
        "created_at": "2026-01-01T00:00:00+00:00",
        "updated_at": "2026-01-01T00:00:00+00:00",
    }


class WorkGraphContractTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        root = Path(self.temporary.name)
        self.layout = ProjectLayout(root=root, project_dir=root / "projects" / "p")
        ensure_runtime_dirs(self.layout)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_dependency_join_blocks_until_predecessor_is_accepted(self) -> None:
        predecessor = task("T001")
        successor = task("T002", dependencies=["T001"])
        graph = register_task(
            self.layout,
            successor,
            known_tasks=[predecessor, successor],
        )
        self.assertEqual(graph["nodes"]["T001"]["state"], "ready")
        self.assertEqual(graph["nodes"]["T002"]["state"], "blocked")
        self.assertEqual(
            dispatch_blockers(
                self.layout,
                "T002",
                known_tasks=[predecessor, successor],
            ),
            ["dependency T001 is ready"],
        )

        predecessor["status"] = "done"
        predecessor["leader_result"] = {
            "status": "done",
            "accepted_by_leader": True,
        }
        graph = sync_task_node(
            self.layout,
            predecessor,
            known_tasks=[predecessor, successor],
        )
        self.assertEqual(graph["nodes"]["T001"]["state"], "accepted")
        self.assertEqual(ready_task_ids(graph), ["T002"])

    def test_cycle_is_rejected(self) -> None:
        first = task("T001", dependencies=["T002"])
        second = task("T002", dependencies=["T001"])
        with self.assertRaisesRegex(WorkGraphError, "cycle"):
            register_task(self.layout, first, known_tasks=[first, second])


if __name__ == "__main__":
    unittest.main()
