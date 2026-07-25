from __future__ import annotations

import unittest
from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from costmarshal_v2.quality import (
    build_artifact_event,
    default_gate_spec,
    evaluate_gates,
)


class QualityGateTest(unittest.TestCase):
    def test_acceptance_requires_dependencies_quality_and_artifact(self) -> None:
        task = {
            "id": "T002",
            "dependencies": ["T001"],
            "gates": default_gate_spec(
                min_quality_score=4,
                max_error_severity=1,
            ),
            "teaching": {"mode": "off", "enforcement": "none"},
        }
        result = {
            "id": "RES-1",
            "attempt_id": "ATT-1",
            "accepted_by_leader": True,
            "quality_score": 4,
        }
        artifact = build_artifact_event(
            project_id="P1",
            task_id="T002",
            attempt_id="ATT-1",
            result_id="RES-1",
            kind="completion-report",
            path="tasks/T002/completion-report.md",
            sha256="a" * 64,
            size=12,
            lifecycle="accepted",
        )
        gate = evaluate_gates(
            project_id="P1",
            task=task,
            result=result,
            dependency_states={"T001": "accepted"},
            artifact_events=[artifact],
            error_severity=1,
            teaching_evidence=None,
        )
        self.assertTrue(gate["passed"])

        blocked = evaluate_gates(
            project_id="P1",
            task=task,
            result={**result, "quality_score": 3},
            dependency_states={"T001": "accepted"},
            artifact_events=[artifact],
            error_severity=1,
            teaching_evidence=None,
        )
        self.assertFalse(blocked["passed"])
        self.assertIn(
            "minimum_quality",
            [check["name"] for check in blocked["checks"] if not check["passed"]],
        )

    def test_explicit_teaching_mode_requires_evidence(self) -> None:
        task = {
            "id": "T001",
            "dependencies": [],
            "gates": default_gate_spec(),
            "teaching": {"mode": "review", "enforcement": "required"},
        }
        result = {
            "id": "RES-1",
            "attempt_id": "ATT-1",
            "accepted_by_leader": True,
            "quality_score": 5,
        }
        artifact = build_artifact_event(
            project_id="P1",
            task_id="T001",
            attempt_id="ATT-1",
            result_id="RES-1",
            kind="completion-report",
            path="report.md",
            sha256="b" * 64,
            size=1,
            lifecycle="accepted",
        )
        gate = evaluate_gates(
            project_id="P1",
            task=task,
            result=result,
            dependency_states={},
            artifact_events=[artifact],
            error_severity=0,
            teaching_evidence=None,
        )
        self.assertFalse(gate["passed"])


if __name__ == "__main__":
    unittest.main()
