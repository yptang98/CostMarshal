#!/usr/bin/env python3
"""v3.5 total-cost, model-memory, attribution, and teaching contracts."""

from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from costmarshal_v2.cost_model import (  # noqa: E402
    build_total_cost_report,
    validate_total_cost_report,
)
from costmarshal_v2.evolution import build_model_memory  # noqa: E402
from costmarshal_v2.routing import leader_acceptance_prior  # noqa: E402
from costmarshal_v2.teaching import (  # noqa: E402
    TeachingError,
    build_teaching_execution_graph,
    build_teaching_run,
    validate_teaching_run,
)


def evaluation(
    evaluation_id: str,
    *,
    model: str,
    timestamp: str,
    accepted: bool = True,
) -> dict:
    return {
        "id": evaluation_id,
        "timestamp": timestamp,
        "provider": "longcat",
        "model": model,
        "profile": "longcat",
        "profile_sha256": "sha256:profile",
        "task_type": "coding",
        "difficulty": "normal",
        "role": "builder",
        "accepted": accepted,
        "routing_success": accepted,
        "required_capabilities": ["code"],
        "scores": {
            "quality": 5 if accepted else 1,
            "efficiency": 4,
            "instruction_following": 4,
            "handoff": 4,
            "reliability": 5 if accepted else 1,
            "routing_fit": 5 if accepted else 1,
        },
        "usage": {
            "actual_tokens": 100,
            "estimated_tokens": 100,
            "wall_seconds": 5,
            "estimated_cost_cny": "0.1",
        },
        "error": {
            "attribution": "none" if accepted else "model-capability",
            "severity": 0 if accepted else 3,
        },
    }


def result(
    result_id: str,
    task_id: str,
    *,
    model: str = "LongCat-Flash",
    profile_hash: str = "profile-a",
    accepted: bool = False,
) -> dict:
    return {
        "id": result_id,
        "project_id": "P-1",
        "task_id": task_id,
        "attempt_id": "ATT-" + result_id,
        "provider": "longcat",
        "execution_model": model,
        "profile": "longcat",
        "profile_sha256": profile_hash,
        "task_type": "coding",
        "difficulty": "normal",
        "accepted_by_leader": accepted,
    }


def gate(gate_id: str, result_id: str) -> dict:
    return {
        "id": gate_id,
        "project_id": "P-1",
        "result_id": result_id,
        "passed": True,
    }


class V35CostTeachingMemoryContractTest(unittest.TestCase):
    def test_total_cost_is_observable_and_centered_on_accepted_artifacts(self) -> None:
        report = build_total_cost_report(
            project={"project_id": "P-1"},
            tasks=[
                {
                    "id": "T-1",
                    "attempts": [
                        {
                            "handoff_capsule": {
                                "handoff": {"size_bytes": 64}
                            }
                        },
                        {},
                    ],
                }
            ],
            results=[
                {
                    "id": "R-1",
                    "estimated_cost_cny": "1.25",
                    "status": "done",
                },
                {
                    "id": "R-2",
                    "estimated_cost_cny": None,
                    "status": "escalate",
                },
            ],
            evaluations=[
                {
                    "id": "E-1",
                    "role": "builder",
                    "usage": {"actual_tokens": 100, "wall_seconds": 5},
                    "error": {"attribution": "environment"},
                },
                {
                    "id": "E-2",
                    "role": "reviewer",
                    "usage": {"actual_tokens": 40, "wall_seconds": 2},
                    "error": {"attribution": "none"},
                },
            ],
            gate_results=[
                {
                    "id": "G-1",
                    "checks": [{"name": "quality", "passed": False}],
                }
            ],
            artifact_rows=[
                {"artifact_id": "A-1", "lifecycle": "candidate"},
                {"artifact_id": "A-1", "lifecycle": "accepted"},
                {"artifact_id": "A-2", "lifecycle": "rejected"},
            ],
            leader_work=[
                {
                    "id": "L-1",
                    "minutes": 10,
                    "total_tokens": 20,
                    "estimated_cost_cny": "0.25",
                }
            ],
        )
        self.assertEqual(report["metric"]["accepted_artifact_count"], 1)
        self.assertEqual(report["metric"]["known_monetary_cost_cny"], "1.500000000")
        self.assertEqual(report["metric"]["status"], "partial")
        self.assertEqual(
            report["metric"]["unknown_monetary_observation_count"], 1
        )
        self.assertEqual(
            report["dimensions"]["verification"]["reviewer_tokens"], 40
        )
        self.assertEqual(
            report["dimensions"]["rework"]["retry_count"], 1
        )
        self.assertEqual(validate_total_cost_report(report), report)

    def test_memory_isolates_versions_and_decays_confidence(self) -> None:
        reference = datetime(2026, 7, 26, tzinfo=timezone.utc)
        rows = [
            evaluation(
                "E-new",
                model="LongCat-Flash-v2",
                timestamp="2026-07-26T00:00:00+00:00",
            ),
            evaluation(
                "E-old",
                model="LongCat-Flash-v2",
                timestamp="2025-07-26T00:00:00+00:00",
            ),
            evaluation(
                "E-version",
                model="LongCat-Flash-v3",
                timestamp="2026-07-26T00:00:00+00:00",
            ),
        ]
        with tempfile.TemporaryDirectory() as temporary:
            memory = build_model_memory(
                Path(temporary),
                evaluations=rows,
                reference_time=reference,
            )
        self.assertEqual(len(memory["profiles"]), 2)
        v2 = next(
            profile
            for profile in memory["profiles"]
            if profile["identity"]["model"] == "LongCat-Flash-v2"
        )
        self.assertEqual(v2["sample_count"], 2)
        self.assertLess(v2["effective_sample_count"], 1.1)
        self.assertLess(v2["confidence"], 2 / 12)
        interval = v2["confidence_intervals"]["acceptance_rate"]
        self.assertLess(interval["lower"], interval["upper"])
        self.assertTrue(memory["evidence_policy"]["model_version_isolation"])

    def test_router_does_not_charge_external_failure_to_model(self) -> None:
        base = {
            "provider": "longcat",
            "model": "LongCat-Flash",
            "profile": None,
            "profile_sha256": None,
            "route_plan_step_index": 0,
            "route_predecessors": [],
            "task_type": "coding",
            "difficulty": "normal",
        }
        history = [
            {
                **base,
                "id": "R-ok",
                "attempt_id": "A-ok",
                "command_id": "C-ok",
                "accepted_by_leader": True,
                "error_attribution": "none",
            },
            {
                **base,
                "id": "R-env",
                "attempt_id": "A-env",
                "command_id": "C-env",
                "accepted_by_leader": False,
                "error_attribution": "environment",
            },
            {
                **base,
                "id": "R-model",
                "attempt_id": "A-model",
                "command_id": "C-model",
                "accepted_by_leader": False,
                "error_attribution": "model-capability",
            },
        ]
        prior = leader_acceptance_prior(history, "longcat")
        self.assertEqual(prior.observations, 2)
        self.assertEqual(prior.accepted, 1)
        self.assertEqual(set(prior.evidence_result_ids), {"R-ok", "R-model"})

    def test_review_teaching_graph_requires_real_reviewer_and_passing_gate(self) -> None:
        graph = build_teaching_execution_graph(
            project_id="P-1",
            task_id="T-target",
            mode="review",
            task_input={"task_type": "coding", "acceptance": "tests pass"},
        )
        assert graph is not None
        target = {
            "id": "T-target",
            "teaching": {"mode": "review", "execution_graph": graph},
        }
        tasks = [
            target,
            {"id": "T-candidate", "role": "builder"},
            {"id": "T-review", "role": "reviewer"},
        ]
        results = [
            result("R-candidate", "T-candidate"),
            result("R-review", "T-review", accepted=True),
        ]
        gates = [gate("G-review", "R-review")]
        run = build_teaching_run(
            project_id="P-1",
            task=target,
            bindings={
                "candidate_execution": "R-candidate",
                "independent_review": "R-review",
                "acceptance_gate": "G-review",
            },
            tasks=tasks,
            results=results,
            gates=gates,
            conclusion="The independent review confirmed the fixed gate.",
        )
        self.assertEqual(
            validate_teaching_run(
                run,
                task=target,
                tasks=tasks,
                results=results,
                gates=gates,
            ),
            run,
        )
        tampered = deepcopy(run)
        tampered["conclusion"] = "invented"
        with self.assertRaisesRegex(TeachingError, "binding"):
            validate_teaching_run(
                tampered,
                task=target,
                tasks=tasks,
                results=results,
                gates=gates,
            )

    def test_paired_and_replay_graphs_enforce_controlled_comparisons(self) -> None:
        tasks = [
            {"id": "T-pair", "role": "builder"},
            {"id": "T-a", "role": "builder"},
            {"id": "T-b", "role": "builder"},
            {"id": "T-compare", "role": "reviewer"},
            {"id": "T-replay", "role": "builder"},
        ]
        paired_graph = build_teaching_execution_graph(
            project_id="P-1",
            task_id="T-pair",
            mode="paired",
            task_input={"fixed": "scope"},
        )
        assert paired_graph is not None
        paired_task = {
            "id": "T-pair",
            "teaching": {"mode": "paired", "execution_graph": paired_graph},
        }
        paired_results = [
            result("R-a", "T-a", model="model-a", profile_hash="p-a"),
            result("R-b", "T-b", model="model-b", profile_hash="p-b"),
            result("R-compare", "T-compare", accepted=True),
        ]
        paired = build_teaching_run(
            project_id="P-1",
            task=paired_task,
            bindings={
                "completion_a": "R-a",
                "completion_b": "R-b",
                "comparison_review": "R-compare",
                "acceptance_gate": "G-compare",
            },
            tasks=[*tasks, paired_task],
            results=paired_results,
            gates=[gate("G-compare", "R-compare")],
            conclusion="The reviewer selected completion B against the same scope.",
        )
        self.assertEqual(paired["mode"], "paired")

        replay_graph = build_teaching_execution_graph(
            project_id="P-1",
            task_id="T-replay",
            mode="replay",
            task_input={"fixed": "scope"},
        )
        assert replay_graph is not None
        replay_task = {
            "id": "T-replay",
            "teaching": {"mode": "replay", "execution_graph": replay_graph},
        }
        replay_results = [
            result("R-base", "T-a"),
            result("R-replay", "T-b", accepted=True),
        ]
        replay = build_teaching_run(
            project_id="P-1",
            task=replay_task,
            bindings={
                "baseline_execution": "R-base",
                "replay_execution": "R-replay",
                "fixed_gate": "G-replay",
            },
            tasks=[*tasks, replay_task],
            results=replay_results,
            gates=[gate("G-replay", "R-replay")],
            conclusion="The same model passed the fixed replay gate.",
        )
        self.assertEqual(replay["mode"], "replay")


if __name__ == "__main__":
    unittest.main(verbosity=2)
