#!/usr/bin/env python3
"""v3.4 accepted knowledge, summaries, and Skill candidate contracts."""

from __future__ import annotations

import contextlib
from copy import deepcopy
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from costmarshal_v2.paths import ProjectLayout  # noqa: E402
from costmarshal_v2.project_artifacts import (  # noqa: E402
    ProjectArtifactError,
    build_project_artifact,
)
from costmarshal_v2.project_knowledge import (  # noqa: E402
    ProjectKnowledgeError,
    build_context_view,
    build_leader_decision,
    build_project_knowledge,
    build_skill_candidate,
    validate_leader_decision,
    validate_project_knowledge,
    validate_skill_candidate,
)
from costmarshal_v2.quality import ARTIFACT_SCHEMA  # noqa: E402
from costmarshal_v2.scheduler import command_export_skill_candidate  # noqa: E402
from costmarshal_v2.state import (  # noqa: E402
    append_jsonl,
    atomic_write_json,
    ensure_runtime_dirs,
)


def artifact(artifact_id: str, task_id: str, lifecycle: str = "accepted") -> dict:
    return {
        "schema_version": ARTIFACT_SCHEMA,
        "artifact_id": artifact_id,
        "task_id": task_id,
        "lifecycle": lifecycle,
    }


class V34ProjectKnowledgeContractTest(unittest.TestCase):
    def setUp(self) -> None:
        self.rows = [
            artifact("ART-a", "V2-0001"),
            artifact("ART-b", "V2-0002"),
            artifact("ART-c", "V2-0001"),
            artifact("ART-rejected", "V2-0003", "rejected"),
        ]
        self.decision = build_leader_decision(
            project_id="P-1",
            command_id="CMD-decision",
            statement="Use the stable interface boundary.",
            rationale="Both accepted implementations depend on it.",
        )

    def test_knowledge_requires_accepted_artifact_or_explicit_decision(self) -> None:
        knowledge = build_project_knowledge(
            project_id="P-1",
            command_id="CMD-knowledge",
            kind="architecture",
            title="Stable boundary",
            source_artifact_ids=["ART-a"],
            artifact_rows=self.rows,
            leader_decision_id=self.decision["decision_id"],
            leader_decisions=[self.decision],
        )
        self.assertEqual(validate_leader_decision(self.decision), self.decision)
        tampered_decision = deepcopy(self.decision)
        tampered_decision["timestamp"] = "2099-01-01T00:00:00+00:00"
        with self.assertRaisesRegex(ProjectKnowledgeError, "hash binding"):
            validate_leader_decision(tampered_decision)
        self.assertEqual(
            validate_project_knowledge(
                knowledge,
                artifact_rows=self.rows,
                leader_decisions=[self.decision],
            ),
            knowledge,
        )
        with self.assertRaisesRegex(ProjectKnowledgeError, "not accepted"):
            build_project_knowledge(
                project_id="P-1",
                command_id="CMD-rejected",
                kind="accepted-fact",
                title="Untrusted fact",
                source_artifact_ids=["ART-rejected"],
                artifact_rows=self.rows,
                leader_decision_id=None,
                leader_decisions=[self.decision],
            )
        with self.assertRaisesRegex(ProjectKnowledgeError, "requires accepted"):
            build_project_knowledge(
                project_id="P-1",
                command_id="CMD-empty",
                kind="risk",
                title="Unproved risk",
                source_artifact_ids=[],
                artifact_rows=self.rows,
                leader_decision_id=None,
                leader_decisions=[],
            )

    def test_summary_requires_accepted_lineage_without_copying_source(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            project_dir = root / "runtime" / "projects" / "P-1"
            workspace = root / "workspace"
            project_dir.mkdir(parents=True)
            workspace.mkdir()
            summary = workspace / "summary.md"
            summary.write_text("summary\n", encoding="utf-8")
            layout = ProjectLayout(root=root / "runtime", project_dir=project_dir)
            project = {
                "project_id": "P-1",
                "workspace": str(workspace),
                "source_project": None,
            }
            accepted = build_project_artifact(
                layout=layout,
                project=project,
                command_id="CMD-summary",
                kind="summary",
                name="project-summary",
                lifecycle="accepted",
                local_path=str(summary),
                derived_from=["ART-a", "ART-b"],
                existing_rows=self.rows,
                date_bucket="2026/07/26_project-summary",
            )
            self.assertEqual(accepted["derived_from"], ["ART-a", "ART-b"])
            self.assertEqual(
                accepted["date_bucket"], "2026/07/26_project-summary"
            )
            self.assertEqual(summary.read_text(encoding="utf-8"), "summary\n")
            with self.assertRaisesRegex(ProjectArtifactError, "accepted"):
                build_project_artifact(
                    layout=layout,
                    project=project,
                    command_id="CMD-bad-summary",
                    kind="summary",
                    name="bad-summary",
                    lifecycle="accepted",
                    local_path=str(summary),
                    derived_from=["ART-rejected"],
                    existing_rows=self.rows,
                )

    def test_skill_candidate_requires_repeated_distinct_successes_and_stays_local(self) -> None:
        candidate = build_skill_candidate(
            project_id="P-1",
            command_id="CMD-skill",
            name="repeatable-review",
            artifact_id="PART-skill",
            source_artifact_ids=["ART-a", "ART-b"],
            artifact_rows=self.rows,
            applicability="Use for bounded parser reviews.",
            inputs=["Task brief", "Accepted interface"],
            steps=["Inspect accepted facts", "Run the fixed gate"],
            verification=["All fixed tests pass"],
            failure_boundaries=["Stop when interface evidence is absent"],
        )
        self.assertEqual(candidate["scope"], "project-local")
        self.assertFalse(candidate["global_install_authorized"])
        self.assertEqual(
            validate_skill_candidate(candidate, artifact_rows=self.rows),
            candidate,
        )
        with self.assertRaisesRegex(ProjectKnowledgeError, "distinct tasks"):
            build_skill_candidate(
                project_id="P-1",
                command_id="CMD-skill-bad",
                name="insufficient",
                artifact_id="PART-skill-bad",
                source_artifact_ids=["ART-a", "ART-c"],
                artifact_rows=self.rows,
                applicability="Not enough evidence.",
                inputs=["Input"],
                steps=["Step"],
                verification=["Check"],
                failure_boundaries=["Stop"],
            )

    def test_context_view_loads_bounded_references_and_indexes_cold_content(self) -> None:
        task_knowledge = build_project_knowledge(
            project_id="P-1",
            command_id="CMD-task-context",
            kind="accepted-fact",
            title="Parser interface accepts UTF-8 input",
            source_artifact_ids=["ART-a"],
            artifact_rows=self.rows,
            leader_decision_id=None,
            leader_decisions=[],
            task_id="V2-0001",
        )
        architecture = build_project_knowledge(
            project_id="P-1",
            command_id="CMD-architecture-context",
            kind="architecture",
            title="Stable parser boundary",
            source_artifact_ids=["ART-b"],
            artifact_rows=self.rows,
            leader_decision_id=None,
            leader_decisions=[],
        )
        cold = build_project_knowledge(
            project_id="P-1",
            command_id="CMD-cold-context",
            kind="milestone-summary",
            title="Unrelated billing milestone",
            source_artifact_ids=["ART-b"],
            artifact_rows=self.rows,
            leader_decision_id=None,
            leader_decisions=[],
        )
        view = build_context_view(
            project={"project_id": "P-1"},
            task={
                "id": "V2-0001",
                "title": "Repair parser interface",
                "purpose": "Accept UTF-8 safely",
                "task_type": "coding",
            },
            query="parser interface",
            knowledge_rows=[task_knowledge, architecture, cold],
            artifact_rows=self.rows,
            leader_snapshots=[
                {
                    "snapshot_id": "LSNP-1",
                    "snapshot_sha256": "sha256:snapshot",
                }
            ],
            hot_limit=4,
            warm_limit=4,
        )
        hot_ids = {row["id"] for row in view["hot"]}
        cold_ids = {row["id"] for row in view["cold_index"]}
        self.assertIn(task_knowledge["knowledge_id"], hot_ids)
        self.assertIn("LSNP-1", hot_ids)
        self.assertIn(cold["knowledge_id"], cold_ids)
        self.assertFalse(view["content_policy"]["raw_content_loaded"])
        self.assertFalse(view["content_policy"]["transcripts_loaded"])
        self.assertFalse(view["content_policy"]["cold_content_loaded"])

    def test_skill_export_is_explicit_project_local_and_never_installs(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw) / "runtime"
            project_dir = root / "projects" / "P-1"
            workspace = Path(raw) / "workspace"
            workspace.mkdir()
            layout = ProjectLayout(root=root, project_dir=project_dir)
            ensure_runtime_dirs(layout)
            atomic_write_json(
                layout.project_json,
                {
                    "project_id": "P-1",
                    "workspace": str(workspace),
                    "source_project": None,
                },
            )
            source_dir = workspace / "candidate"
            source_dir.mkdir()
            skill_file = source_dir / "SKILL.md"
            skill_file.write_text(
                "---\nname: repeatable-review\ndescription: Review bounded parsers.\n---\n",
                encoding="utf-8",
            )
            candidate_artifact = build_project_artifact(
                layout=layout,
                project={
                    "project_id": "P-1",
                    "workspace": str(workspace),
                    "source_project": None,
                },
                command_id="CMD-candidate-artifact",
                kind="skill-candidate",
                name="repeatable-review",
                lifecycle="accepted",
                local_path=str(skill_file),
                derived_from=["ART-a", "ART-b"],
                existing_rows=self.rows,
            )
            candidate = build_skill_candidate(
                project_id="P-1",
                command_id="CMD-candidate",
                name="repeatable-review",
                artifact_id=candidate_artifact["artifact_id"],
                source_artifact_ids=["ART-a", "ART-b"],
                artifact_rows=[*self.rows, candidate_artifact],
                applicability="Use for bounded parser reviews.",
                inputs=["Brief"],
                steps=["Review"],
                verification=["Test"],
                failure_boundaries=["Stop without evidence"],
            )
            for row in [*self.rows, candidate_artifact]:
                append_jsonl(layout.artifacts_jsonl, row)
            append_jsonl(layout.skill_candidates_jsonl, candidate)
            preview_output = io.StringIO()
            with contextlib.redirect_stdout(preview_output):
                command_export_skill_candidate(
                    SimpleNamespace(
                        root=root,
                        project=str(project_dir),
                        candidate=candidate["candidate_id"],
                        apply=False,
                        preview_sha=None,
                        command_id=None,
                    )
                )
            preview = json.loads(preview_output.getvalue())["preview"]
            export_dir = layout.skill_candidates_dir / "repeatable-review"
            self.assertFalse(export_dir.exists())
            applied_output = io.StringIO()
            with contextlib.redirect_stdout(applied_output):
                command_export_skill_candidate(
                    SimpleNamespace(
                        root=root,
                        project=str(project_dir),
                        candidate=candidate["candidate_id"],
                        apply=True,
                        preview_sha=preview["preview_sha256"],
                        command_id="CMD-export",
                    )
                )
            applied = json.loads(applied_output.getvalue())
            self.assertFalse(applied["installed_globally"])
            self.assertEqual((export_dir / "SKILL.md").read_bytes(), skill_file.read_bytes())
            evidence = json.loads(
                (export_dir / "evidence.json").read_text(encoding="utf-8")
            )
            self.assertFalse(evidence["global_install_authorized"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
