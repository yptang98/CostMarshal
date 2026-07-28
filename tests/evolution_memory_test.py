from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from costmarshal_v2.evolution import (
    EvolutionError,
    active_policy_effects,
    append_evolution_cycle,
    append_retrospective_and_candidate,
    build_attempt_evaluation,
    build_evolution_cycle,
    build_model_memory,
    build_project_retrospective,
    choose_teaching_mode,
    latest_policy_candidates,
    transition_policy_candidate,
    validate_evolution_cycle,
)
from costmarshal_v2.paths import ProjectLayout
from costmarshal_v2.routing import leader_acceptance_prior
from costmarshal_v2.scheduler import _namespace_peer_routing_rows
from costmarshal_v2.state import ensure_runtime_dirs


class EvolutionMemoryTest(unittest.TestCase):
    def _evaluation(self) -> dict:
        task = {
            "id": "T001",
            "task_type": "coding",
            "difficulty": "normal",
            "risk": "low",
            "role": "builder",
            "required_capabilities": ["code"],
            "estimated_input_tokens": 100,
            "estimated_output_tokens": 100,
            "teaching": {"mode": "off", "reason": "mature"},
        }
        result = {
            "id": "RES-1",
            "project_id": "P1",
            "task_id": "T001",
            "attempt_id": "ATT-1",
            "provider": "longcat",
            "tier": "low",
            "execution_model": "LongCat-Flash",
            "profile": "longcat",
            "profile_sha256": "a" * 64,
            "accepted_by_leader": True,
            "quality_score": 5,
            "status": "done",
            "route_plan_step_index": 0,
            "total_tokens": 180,
            "estimated_cost_cny": "0.010000000",
            "cost_source": "price-snapshot",
        }
        gate = {"passed": True, "evidence_sha256": "sha256:" + "b" * 64}
        return build_attempt_evaluation(
            task=task,
            attempt={"attempt_id": "ATT-1"},
            result=result,
            gate_result=gate,
        )

    def test_memory_aggregates_traits_without_raw_content(self) -> None:
        evaluation = self._evaluation()
        second = {
            **evaluation,
            "id": "EVAL-2",
            "result_id": "RES-2",
        }
        with tempfile.TemporaryDirectory() as temporary:
            memory = build_model_memory(
                Path(temporary), evaluations=[evaluation, second]
            )
        self.assertEqual(memory["evaluation_count"], 2)
        profile = memory["profiles"][0]
        self.assertEqual(profile["sample_count"], 2)
        self.assertEqual(profile["demonstrated_capabilities"], {"code": 2})
        self.assertNotIn("summary", profile)
        self.assertNotIn("prompt", profile)
        self.assertNotIn("report", profile)

    def test_teaching_policy_separates_advisory_auto_from_explicit_gate(self) -> None:
        automatic = choose_teaching_mode(
            requested_mode="auto",
            risk="low",
            profile=None,
            max_cost_cny=None,
        )
        self.assertEqual(automatic["mode"], "paired")
        self.assertEqual(automatic["enforcement"], "advisory")
        explicit = choose_teaching_mode(
            requested_mode="review",
            risk="low",
            profile=None,
            max_cost_cny=None,
        )
        self.assertEqual(explicit["enforcement"], "required")

    def test_project_retrospective_waits_for_all_tasks_to_finish(self) -> None:
        evaluation = self._evaluation()
        project = {"project_id": "P1", "name": "demo"}
        self.assertIsNone(
            build_project_retrospective(
                project=project,
                tasks=[{"id": "T001", "status": "running"}],
                evaluations=[evaluation],
            )
        )
        retrospective = build_project_retrospective(
            project=project,
            tasks=[{"id": "T001", "status": "done"}],
            evaluations=[evaluation],
        )
        self.assertIsNotNone(retrospective)
        self.assertEqual(retrospective["routing_success_count"], 1)

    def test_evolution_cycle_is_local_advisory_and_idempotent(self) -> None:
        evaluation = {
            **self._evaluation(),
            "accepted": False,
            "routing_success": False,
            "error": {"attribution": "environment", "severity": 3},
        }
        memory = build_model_memory(
            Path("."),
            evaluations=[evaluation],
        )
        arguments = {
            "project": {"project_id": "P1", "name": "demo"},
            "tasks": [{"id": "T001", "status": "done"}],
            "evaluations": [evaluation],
            "teaching_runs": [],
            "policy_candidates": [],
            "model_memory": memory,
        }
        first = build_evolution_cycle(**arguments, trigger="result:RES-1")
        replay = build_evolution_cycle(**arguments, trigger="manual-inspection")
        self.assertEqual(first["cycle_id"], replay["cycle_id"])
        self.assertEqual(first["outcome"]["error_categories"]["external"], 1)
        self.assertFalse(first["outcome"]["external_failures_penalize_model"])
        self.assertEqual(first["teaching"]["provider_calls_created"], 0)
        self.assertFalse(first["teaching"]["automatic_execution"])
        self.assertFalse(first["policy"]["automatic_activation"])
        self.assertEqual(validate_evolution_cycle(first), first)
        tampered = {
            **first,
            "teaching": {
                **first["teaching"],
                "provider_calls_created": 1,
            },
        }
        with self.assertRaisesRegex(EvolutionError, "zero-surprise"):
            validate_evolution_cycle(tampered)
        self.assertEqual(
            first["teaching"]["recommendations"][0]["mode"],
            "paired",
        )
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            layout = ProjectLayout(root=root, project_dir=root / "projects" / "p")
            ensure_runtime_dirs(layout)
            self.assertTrue(append_evolution_cycle(layout, first))
            self.assertFalse(append_evolution_cycle(layout, replay))

    def test_policy_requires_replay_shadow_and_canary_before_activation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            layout = ProjectLayout(root=root, project_dir=root / "projects" / "p")
            ensure_runtime_dirs(layout)
            retrospective = {
                "id": "RETRO-1",
                "project_id": "P1",
                "recommendations": [
                    "replay weak task scopes before promoting a routing change"
                ],
                "evidence_sha256": "sha256:" + "c" * 64,
            }
            append_retrospective_and_candidate(layout, retrospective)
            candidate_id = latest_policy_candidates(layout)[0]["id"]
            for state in ("replayed", "shadow", "canary", "active"):
                transition_policy_candidate(
                    layout,
                    candidate_id=candidate_id,
                    to_state=state,
                    evidence=f"reviewed {state} evidence",
                    approved_by="leader",
                    apply=True,
                )
            effects = active_policy_effects(layout)
            self.assertEqual(effects["auto_teaching_floor"], "review")
            self.assertEqual(effects["active_policy_ids"], [candidate_id])

    def test_cross_project_id_reuse_is_namespaced_before_routing(self) -> None:
        base = {
            "id": "RES-1",
            "attempt_id": "ATT-1",
            "command_id": "CMD-1",
            "task_id": "T001",
            "provider": "longcat",
            "model": "LongCat-2.0",
            "profile": None,
            "accepted_by_leader": True,
            "route_plan_step_index": 0,
            "route_predecessors": [],
        }
        first = _namespace_peer_routing_rows([base], namespace="first")[0]
        second = _namespace_peer_routing_rows(
            [{**base, "accepted_by_leader": False}], namespace="second"
        )[0]
        prior = leader_acceptance_prior([first, second], "longcat")
        self.assertEqual(prior.observations, 2)


if __name__ == "__main__":
    unittest.main()
