#!/usr/bin/env python3
"""Contract tests for the standalone three-tier routing module."""

from __future__ import annotations

from copy import deepcopy
import sys
from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from costmarshal_v2.routing import (  # noqa: E402
    RoutingValidationError,
    auto_tier_floor,
    decide_route,
    default_provider_catalog,
    estimate_cost_cny,
    leader_acceptance_prior,
    legacy_provider_catalog,
    next_stronger_provider,
    project_provider_catalog,
    provider_by_id,
    validate_provider_catalog,
)


class ThreeTierRoutingTest(unittest.TestCase):
    def test_new_and_legacy_catalogs_keep_provider_id_separate_from_tier(self) -> None:
        new = validate_provider_catalog(default_provider_catalog())
        self.assertEqual(
            [(row["provider_id"], row["tier"]) for row in new["providers"]],
            [("longcat", "low"), ("deepseek", "medium"), ("codex", "high")],
        )
        self.assertEqual([row["env_key"] for row in new["providers"]], ["LONGCAT_API_KEY", "DEEPSEEK_API_KEY", None])
        legacy = project_provider_catalog({"project_id": "old-v2"})
        self.assertEqual(
            [(row["provider_id"], row["tier"]) for row in legacy["providers"]],
            [("longcat", "low"), ("codex", "high")],
        )
        explicit = project_provider_catalog({"provider_catalog": new})
        self.assertEqual(len(explicit["providers"]), 3)

    def test_catalog_validation_is_fail_closed_and_non_mutating(self) -> None:
        catalog = default_provider_catalog()
        original = deepcopy(catalog)
        validate_provider_catalog(catalog)
        self.assertEqual(catalog, original)

        cases = []
        duplicate = default_provider_catalog()
        duplicate["providers"][1]["provider_id"] = "longcat"
        cases.append(duplicate)
        invalid_tier = default_provider_catalog()
        invalid_tier["providers"][0]["tier"] = "cheap"
        cases.append(invalid_tier)
        negative_price = default_provider_catalog()
        negative_price["providers"][0]["input_cny_per_1m"] = -1
        cases.append(negative_price)
        bool_price = default_provider_catalog()
        bool_price["providers"][0]["input_cny_per_1m"] = True
        cases.append(bool_price)
        typo = default_provider_catalog()
        typo["providers"][0]["ouput_cny_per_1m"] = 1
        cases.append(typo)
        for invalid in cases:
            with self.subTest(invalid=invalid):
                with self.assertRaises(RoutingValidationError):
                    validate_provider_catalog(invalid)

        with self.assertRaises(RoutingValidationError):
            project_provider_catalog({"provider_catalog": None})

    def test_auto_tier_floor_is_conservative(self) -> None:
        self.assertEqual(auto_tier_floor({"risk": "high"}), "high")
        self.assertEqual(auto_tier_floor({"risk": "low", "difficulty": "hard"}), "high")
        self.assertEqual(auto_tier_floor({"risk": "medium"}), "medium")
        self.assertEqual(auto_tier_floor({"risk": "low", "task_type": "implementation"}), "medium")
        self.assertEqual(auto_tier_floor({"risk": "low", "task_type": "review"}), "medium")
        self.assertEqual(auto_tier_floor({"risk": "low", "task_type": "analysis"}), "low")
        self.assertEqual(auto_tier_floor({"risk": "low", "task_type": "architecture"}), "medium")
        with self.assertRaises(RoutingValidationError):
            auto_tier_floor({"risk": "urgent"})

    def test_default_auto_routes_low_medium_and_high(self) -> None:
        catalog = default_provider_catalog()
        low = decide_route({"risk": "low", "task_type": "analysis"}, catalog)
        medium = decide_route({"risk": "medium", "task_type": "analysis"}, catalog)
        high = decide_route({"risk": "low", "difficulty": "hard"}, catalog)
        self.assertEqual((low.provider_id, low.tier), ("longcat", "low"))
        self.assertEqual((medium.provider_id, medium.tier), ("deepseek", "medium"))
        self.assertEqual((high.provider_id, high.tier), ("codex", "high"))
        self.assertIn("Tier floor low", low.explanation)
        self.assertIn("conservative leader-acceptance prior", low.explanation)
        self.assertEqual(low.to_dict()["candidate_provider_ids"], ["longcat"])

    def test_legacy_medium_floor_skips_to_high(self) -> None:
        decision = decide_route(
            {"risk": "medium", "task_type": "analysis"}, legacy_provider_catalog()
        )
        self.assertEqual((decision.provider_id, decision.tier), ("codex", "high"))
        self.assertIn("next available stronger tier high", decision.reason)

    def test_explicit_requests_cannot_bypass_safe_floor(self) -> None:
        catalog = default_provider_catalog()
        with self.assertRaises(RoutingValidationError):
            decide_route(
                {"risk": "high"}, catalog, requested_provider_id="longcat"
            )
        with self.assertRaises(RoutingValidationError):
            decide_route({"risk": "medium"}, catalog, requested_tier="low")
        with self.assertRaises(RoutingValidationError):
            decide_route({"risk": "low"}, catalog, requested_provider_id="typo")
        explicit = decide_route(
            {"risk": "low"}, catalog, requested_provider_id="deepseek"
        )
        self.assertEqual(explicit.provider_id, "deepseek")

    def test_next_stronger_provider_supports_three_and_two_tier_chains(self) -> None:
        catalog = default_provider_catalog()
        self.assertEqual(next_stronger_provider(catalog, "longcat")["provider_id"], "deepseek")
        self.assertEqual(next_stronger_provider(catalog, "deepseek")["provider_id"], "codex")
        self.assertIsNone(next_stronger_provider(catalog, "codex"))
        self.assertEqual(
            next_stronger_provider(legacy_provider_catalog(), "longcat")["provider_id"],
            "codex",
        )

    def test_cost_estimation_requires_complete_reviewed_pricing(self) -> None:
        catalog = default_provider_catalog()
        provider = provider_by_id(catalog, "deepseek")
        self.assertIsNone(estimate_cost_cny(provider, input_tokens=100, output_tokens=50))
        provider["input_cny_per_1m"] = 2.0
        provider["output_cny_per_1m"] = 6.0
        self.assertEqual(
            estimate_cost_cny(provider, input_tokens=500_000, output_tokens=250_000),
            2.5,
        )
        decision_catalog = default_provider_catalog()
        decision_catalog["providers"][1]["input_cny_per_1m"] = 2.0
        decision_catalog["providers"][1]["output_cny_per_1m"] = 6.0
        decision = decide_route(
            {"risk": "medium"},
            decision_catalog,
            input_tokens=500_000,
            output_tokens=250_000,
        )
        self.assertEqual(decision.estimated_cost_cny, 2.5)
        with self.assertRaises(RoutingValidationError):
            estimate_cost_cny(provider, input_tokens=True, output_tokens=0)

    def test_leader_acceptance_prior_ignores_worker_self_report_and_backs_off(self) -> None:
        cold = leader_acceptance_prior([], "deepseek", task_type="review", difficulty="normal")
        self.assertEqual(cold.observations, 0)
        self.assertLess(cold.posterior_mean, 0.5)
        history = [
            {
                "provider_id": "deepseek",
                "task_type": "review",
                "difficulty": "normal",
                "accepted_by_leader": True,
            },
            {
                "provider": "deepseek",
                "task_type": "review",
                "difficulty": "normal",
                "accepted_by_leader": True,
            },
            {
                "provider_id": "deepseek",
                "task_type": "analysis",
                "difficulty": "normal",
                "accepted_by_leader": False,
            },
            {"provider_id": "deepseek", "completed": True},
            {"provider_id": "deepseek", "accepted_by_leader": "yes"},
        ]
        exact = leader_acceptance_prior(
            history, "deepseek", task_type="review", difficulty="normal"
        )
        self.assertEqual(exact.scope, "provider+task_type+difficulty")
        self.assertEqual((exact.observations, exact.accepted), (2, 2))
        self.assertGreater(exact.posterior_mean, cold.posterior_mean)
        backed_off = leader_acceptance_prior(
            history, "deepseek", task_type="missing", difficulty="normal"
        )
        self.assertEqual(backed_off.scope, "provider")
        self.assertEqual(backed_off.observations, 3)

    def test_same_tier_selection_uses_priority_then_conservative_evidence(self) -> None:
        catalog = default_provider_catalog()
        second = deepcopy(catalog["providers"][1])
        second["provider_id"] = "deepseek-alt"
        second["profile"] = "deepseek-alt"
        catalog["providers"].append(second)
        history = [
            {
                "provider_id": "deepseek-alt",
                "task_type": "analysis",
                "difficulty": "normal",
                "accepted_by_leader": True,
            }
            for _ in range(8)
        ]
        history.extend(
            {
                "provider_id": "deepseek",
                "task_type": "analysis",
                "difficulty": "normal",
                "accepted_by_leader": False,
            }
            for _ in range(8)
        )
        evidence_choice = decide_route({"risk": "medium"}, catalog, history=history)
        self.assertEqual(evidence_choice.provider_id, "deepseek-alt")
        catalog["providers"][-1]["priority"] = 101
        priority_choice = decide_route({"risk": "medium"}, catalog, history=history)
        self.assertEqual(priority_choice.provider_id, "deepseek")

    def test_disabled_or_missing_safe_tier_fails_closed(self) -> None:
        catalog = default_provider_catalog()
        catalog["providers"][2]["enabled"] = False
        with self.assertRaises(RoutingValidationError):
            decide_route({"risk": "high"}, catalog)
        catalog = default_provider_catalog()
        catalog["providers"][1]["enabled"] = False
        decision = decide_route({"risk": "medium"}, catalog)
        self.assertEqual((decision.provider_id, decision.tier), ("codex", "high"))

    def test_cross_tier_optimizer_minimizes_expected_cost_per_accepted_result(self) -> None:
        catalog = default_provider_catalog()
        prices = {
            "longcat": (80.0, 80.0),
            "deepseek": (20.0, 20.0),
            "codex": (1.0, 1.0),
        }
        for provider in catalog["providers"]:
            provider["input_cny_per_1m"], provider["output_cny_per_1m"] = prices[provider["provider_id"]]
        decision = decide_route(
            {"risk": "low", "task_type": "analysis", "difficulty": "normal"},
            catalog,
            input_tokens=500_000,
            output_tokens=500_000,
        )
        self.assertEqual(decision.provider_id, "codex")
        self.assertEqual(decision.optimization_mode, "expected-cost-per-accepted")
        self.assertEqual(decision.planned_provider_ids, ("codex",))
        self.assertIsNotNone(decision.expected_cost_per_accepted_cny)

    def test_cross_tier_optimizer_keeps_cheap_escalation_chain(self) -> None:
        catalog = default_provider_catalog()
        prices = {
            "longcat": (0.01, 0.01),
            "deepseek": (0.1, 0.1),
            "codex": (10.0, 10.0),
        }
        for provider in catalog["providers"]:
            provider["input_cny_per_1m"], provider["output_cny_per_1m"] = prices[provider["provider_id"]]
        decision = decide_route(
            {"risk": "low", "task_type": "analysis", "difficulty": "normal"},
            catalog,
            input_tokens=500_000,
            output_tokens=500_000,
        )
        self.assertEqual(decision.provider_id, "longcat")
        self.assertEqual(decision.planned_provider_ids, ("longcat", "deepseek", "codex"))
        self.assertGreater(decision.expected_success_probability or 0, decision.acceptance_prior.conservative_probability)

    def test_required_capabilities_are_hard_route_constraints(self) -> None:
        catalog = default_provider_catalog()
        catalog["providers"][1]["capabilities"] = ["vision"]
        decision = decide_route(
            {"risk": "low", "task_type": "analysis", "required_capabilities": ["vision"]},
            catalog,
        )
        self.assertEqual((decision.provider_id, decision.tier), ("deepseek", "medium"))
        with self.assertRaises(RoutingValidationError):
            decide_route(
                {"risk": "low", "task_type": "analysis", "required_capabilities": ["vision"]},
                catalog,
                requested_provider_id="longcat",
            )

    def test_minimum_success_probability_is_fail_closed_for_priced_chains(self) -> None:
        catalog = default_provider_catalog()
        for provider in catalog["providers"]:
            provider["input_cny_per_1m"] = 1.0
            provider["output_cny_per_1m"] = 1.0
        with self.assertRaises(RoutingValidationError):
            decide_route(
                {"risk": "low", "task_type": "analysis", "min_success_probability": 0.99},
                catalog,
                input_tokens=1000,
                output_tokens=1000,
            )
        with self.assertRaises(RoutingValidationError):
            decide_route({"risk": "low", "min_success_probability": 1.1}, catalog)


if __name__ == "__main__":
    unittest.main(verbosity=2)
