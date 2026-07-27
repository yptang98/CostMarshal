#!/usr/bin/env python3
"""v3.3 project artifact and Leader Snapshot contracts."""

from __future__ import annotations

import hashlib
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from costmarshal_v2.leader_snapshot import (  # noqa: E402
    build_leader_snapshot,
    validate_leader_snapshot,
)
from costmarshal_v2.paths import ProjectLayout  # noqa: E402
from costmarshal_v2.project_artifacts import (  # noqa: E402
    ProjectArtifactError,
    build_project_artifact,
    query_project_artifacts,
    validate_project_artifact,
)


class V33LeaderArtifactContractTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name).resolve()
        self.runtime = root / "runtime"
        self.project_dir = self.runtime / "projects" / "P-1"
        self.workspace = root / "workspace"
        self.source = root / "source"
        for path in (self.project_dir, self.workspace, self.source):
            path.mkdir(parents=True)
        self.layout = ProjectLayout(root=self.runtime, project_dir=self.project_dir)
        self.project = {
            "project_id": "P-1",
            "objective": "verify v3.3 contracts",
            "workspace": str(self.workspace),
            "source_project": str(self.source),
            "routing_policy": {"project_budget_cny": "10.000000000"},
        }

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_local_reference_and_derived_summary_preserve_source(self) -> None:
        source_file = self.workspace / "report.md"
        source_file.write_text("accepted evidence\n", encoding="utf-8")
        original = source_file.read_bytes()
        base = build_project_artifact(
            layout=self.layout,
            project=self.project,
            command_id="CMD-1",
            kind="generic",
            name="report",
            lifecycle="accepted",
            local_path=str(source_file),
        )
        self.assertFalse(base["source_mutation"])
        self.assertEqual(source_file.read_bytes(), original)
        summary_file = self.project_dir / "summary.md"
        summary_file.write_text("derived summary\n", encoding="utf-8")
        summary = build_project_artifact(
            layout=self.layout,
            project=self.project,
            command_id="CMD-2",
            kind="summary",
            name="summary",
            lifecycle="accepted",
            local_path=str(summary_file),
            derived_from=[base["artifact_id"]],
            existing_rows=[base],
        )
        self.assertEqual(summary["derived_from"], [base["artifact_id"]])
        validate_project_artifact(
            summary, known_artifact_ids={base["artifact_id"], summary["artifact_id"]}
        )
        # The persisted JSONL sequence remains authoritative when multiple
        # events share the second-resolution timestamp.  Event ids are
        # content/path-derived and therefore must not reorder the ledger.
        query_base = {
            **base,
            "timestamp": "2026-01-01T00:00:00+00:00",
            "event_id": "PAEV-z",
        }
        query_summary = {
            **summary,
            "timestamp": "2026-01-01T00:00:00+00:00",
            "event_id": "PAEV-a",
        }
        rows = query_project_artifacts(
            iter([query_base, query_summary]), lifecycle="accepted"
        )
        self.assertEqual([row["name"] for row in rows], ["report", "summary"])

    def test_external_reference_requires_non_secret_integrity_metadata(self) -> None:
        payload = b"large-server-object"
        digest = hashlib.sha256(payload).hexdigest()
        row = build_project_artifact(
            layout=self.layout,
            project=self.project,
            command_id="CMD-3",
            kind="generic",
            name="checkpoint",
            lifecycle="candidate",
            external_uri="s3://models/checkpoint.bin",
            external_size_bytes=len(payload),
            external_sha256=digest,
        )
        self.assertEqual(row["storage"]["mode"], "external-reference")
        with self.assertRaisesRegex(ProjectArtifactError, "query"):
            build_project_artifact(
                layout=self.layout,
                project=self.project,
                command_id="CMD-4",
                kind="generic",
                name="secret-checkpoint",
                lifecycle="candidate",
                external_uri="https://example.invalid/model?token=secret",
                external_size_bytes=len(payload),
                external_sha256=digest,
            )

    def test_local_reference_cannot_escape_current_project_boundaries(self) -> None:
        outside = Path(self.temp.name) / "outside.txt"
        outside.write_text("outside", encoding="utf-8")
        with self.assertRaisesRegex(ProjectArtifactError, "current runtime"):
            build_project_artifact(
                layout=self.layout,
                project=self.project,
                command_id="CMD-5",
                kind="generic",
                name="outside",
                lifecycle="candidate",
                local_path=str(outside),
            )

    def test_leader_snapshot_is_deterministic_bound_and_transcript_free(self) -> None:
        graph = {
            "schema_version": "costmarshal-work-graph-v1",
            "revision": 4,
            "nodes": {
                "V2-0001": {"state": "waiting_leader"},
                "V2-0002": {"state": "ready"},
                "V2-0003": {"state": "blocked"},
            },
        }
        tasks = [
            {
                "id": "V2-0001",
                "status": "waiting_leader",
                "risk": "high",
                "attempts": [
                    {
                        "status": "waiting_leader",
                        "reserved_cost_cny": "1",
                        "actual_cost_cny": "0.5",
                    }
                ],
            },
            {"id": "V2-0002", "status": "planned", "attempts": []},
            {"id": "V2-0003", "status": "planned", "attempts": []},
        ]
        first = build_leader_snapshot(
            project=self.project, graph=graph, tasks=tasks, artifact_rows=[]
        )
        second = build_leader_snapshot(
            project=self.project, graph=graph, tasks=tasks, artifact_rows=[]
        )
        self.assertEqual(first, second)
        self.assertFalse(first["transcript_included"])
        self.assertEqual(validate_leader_snapshot(first), first)
        changed_graph = {**graph, "revision": 5}
        changed = build_leader_snapshot(
            project=self.project,
            graph=changed_graph,
            tasks=tasks,
            artifact_rows=[],
        )
        self.assertNotEqual(first["snapshot_id"], changed["snapshot_id"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
