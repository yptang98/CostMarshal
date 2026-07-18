#!/usr/bin/env python3
"""Contract tests for the standalone three-tier routing module."""

from __future__ import annotations

from copy import deepcopy
import hashlib
import json
import sys
import time
from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from costmarshal_v2.routing import (  # noqa: E402
    RoutingValidationError,
    auto_tier_floor,
    conditional_leader_acceptance_prior,
    decide_route,
    default_provider_catalog,
    derive_step_token_forecast,
    estimate_cost_cny,
    leader_acceptance_prior,
    legacy_provider_catalog,
    next_stronger_provider,
    project_provider_catalog,
    provider_by_id,
    route_plan_fingerprint,
    validate_provider_catalog,
)


def paired_chain_history(
    *,
    total: int = 200,
    low_accepts: int = 80,
    medium_accepts: int = 60,
    high_accepts: int = 50,
) -> list[dict]:
    """Build task-linked evidence from a real low->medium->high funnel."""

    rows: list[dict] = []
    low_failures = 0
    medium_failures = 0
    for index in range(total):
        task_id = f"TASK-paired-{index}"
        envelope_id = f"ENV-paired-{index}"
        fingerprint = "sha256:" + f"{index:064x}"[-64:]
        low_accepted = index < low_accepts
        rows.append(
            {
                "id": f"RES-low-{index}",
                "provider_id": "longcat",
                "model": "LongCat-2.0",
                "profile": "longcat",
                "task_type": "analysis",
                "difficulty": "normal",
                "task_id": task_id,
                "attempt_id": f"ATT-low-{index}",
                "route_envelope_id": envelope_id,
                "route_plan_fingerprint": fingerprint,
                "route_plan_step_index": 0,
                "route_predecessors": [],
                "accepted_by_leader": low_accepted,
                "status": "done" if low_accepted else "escalate",
            }
        )
        if low_accepted:
            continue
        medium_accepted = low_failures < medium_accepts
        low_failures += 1
        rows.append(
            {
                "id": f"RES-medium-{index}",
                "provider_id": "deepseek",
                "model": "inherit",
                "profile": "deepseek",
                "task_type": "analysis",
                "difficulty": "normal",
                "task_id": task_id,
                "attempt_id": f"ATT-medium-{index}",
                "route_envelope_id": envelope_id,
                "route_plan_fingerprint": fingerprint,
                "route_plan_step_index": 1,
                "route_predecessors": [
                    {
                        "provider_id": "longcat",
                        "model": "LongCat-2.0",
                        "profile": "longcat",
                        "profile_sha256": None,
                        "attempt_id": f"ATT-low-{index}",
                        "result_id": f"RES-low-{index}",
                    }
                ],
                "accepted_by_leader": medium_accepted,
                "status": "done" if medium_accepted else "escalate",
            }
        )
        if medium_accepted:
            continue
        high_accepted = medium_failures < high_accepts
        medium_failures += 1
        rows.append(
            {
                "id": f"RES-high-{index}",
                "provider_id": "codex",
                "model": "inherit",
                "profile": None,
                "task_type": "analysis",
                "difficulty": "normal",
                "task_id": task_id,
                "attempt_id": f"ATT-high-{index}",
                "route_envelope_id": envelope_id,
                "route_plan_fingerprint": fingerprint,
                "route_plan_step_index": 2,
                "route_predecessors": [
                    {
                        "provider_id": "longcat",
                        "model": "LongCat-2.0",
                        "profile": "longcat",
                        "profile_sha256": None,
                        "attempt_id": f"ATT-low-{index}",
                        "result_id": f"RES-low-{index}",
                    },
                    {
                        "provider_id": "deepseek",
                        "model": "inherit",
                        "profile": "deepseek",
                        "profile_sha256": None,
                        "attempt_id": f"ATT-medium-{index}",
                        "result_id": f"RES-medium-{index}",
                    },
                ],
                "accepted_by_leader": high_accepted,
                "status": "done" if high_accepted else "failed",
            }
        )
    return rows


def paired_same_tier_history(total: int = 100, peer_accepts: int = 90) -> list[dict]:
    """Build exact low-A -> low-B -> high continuation evidence."""

    rows: list[dict] = []
    for index in range(total):
        task_id = f"TASK-same-tier-{index}"
        envelope_id = f"ENV-same-tier-{index}"
        fingerprint = "sha256:" + f"{index + 10_000:064x}"[-64:]
        low_attempt_id = f"ATT-low-a-{index}"
        low_result_id = f"RES-low-a-{index}"
        rows.append(
            {
                "id": low_result_id,
                "provider_id": "longcat",
                "model": "LongCat-2.0",
                "profile": "longcat",
                "task_type": "analysis",
                "difficulty": "normal",
                "task_id": task_id,
                "attempt_id": low_attempt_id,
                "route_envelope_id": envelope_id,
                "route_plan_fingerprint": fingerprint,
                "route_plan_step_index": 0,
                "route_predecessors": [],
                "accepted_by_leader": False,
                "status": "escalate",
            }
        )
        peer_accepted = index < peer_accepts
        peer_attempt_id = f"ATT-low-b-{index}"
        peer_result_id = f"RES-low-b-{index}"
        low_predecessor = {
            "provider_id": "longcat",
            "model": "LongCat-2.0",
            "profile": "longcat",
            "profile_sha256": None,
            "attempt_id": low_attempt_id,
            "result_id": low_result_id,
        }
        rows.append(
            {
                "id": peer_result_id,
                "provider_id": "low-peer",
                "model": "Low-Peer",
                "profile": "low-peer",
                "task_type": "analysis",
                "difficulty": "normal",
                "task_id": task_id,
                "attempt_id": peer_attempt_id,
                "route_envelope_id": envelope_id,
                "route_plan_fingerprint": fingerprint,
                "route_plan_step_index": 1,
                "route_predecessors": [low_predecessor],
                "accepted_by_leader": peer_accepted,
                "status": "done" if peer_accepted else "escalate",
            }
        )
        if peer_accepted:
            continue
        rows.append(
            {
                "id": f"RES-high-after-peer-{index}",
                "provider_id": "codex",
                "model": "inherit",
                "profile": None,
                "task_type": "analysis",
                "difficulty": "normal",
                "task_id": task_id,
                "attempt_id": f"ATT-high-after-peer-{index}",
                "route_envelope_id": envelope_id,
                "route_plan_fingerprint": fingerprint,
                "route_plan_step_index": 2,
                "route_predecessors": [
                    low_predecessor,
                    {
                        "provider_id": "low-peer",
                        "model": "Low-Peer",
                        "profile": "low-peer",
                        "profile_sha256": None,
                        "attempt_id": peer_attempt_id,
                        "result_id": peer_result_id,
                    },
                ],
                "accepted_by_leader": True,
                "status": "done",
            }
        )
    return rows


class ThreeTierRoutingTest(unittest.TestCase):
    def test_route_plan_v2_binds_per_step_forecasts_and_preserves_v1_digest(self) -> None:
        decision = decide_route(
            {"risk": "low", "task_type": "analysis"},
            default_provider_catalog(),
            input_tokens=100,
            output_tokens=20,
        )
        self.assertEqual(
            decision.planned_steps[0]["token_forecast"]["cache_mode"],
            "none",
        )
        v2_digest = decision.plan_fingerprint
        drifted = deepcopy(list(decision.planned_steps))
        drifted[0]["token_forecast"]["estimated_input_tokens"] += 1
        with self.assertRaisesRegex(RoutingValidationError, "inconsistent"):
            route_plan_fingerprint(
                drifted,
                input_tokens=100,
                cached_input_tokens=0,
                output_tokens=20,
            )

        legacy_steps = deepcopy(list(decision.planned_steps))
        for step in legacy_steps:
            step.pop("token_forecast")
        legacy_payload = {
            "schema_version": "costmarshal-route-plan-v1",
            "estimated_input_tokens": 100,
            "estimated_cached_input_tokens": 0,
            "estimated_output_tokens": 20,
            "planned_steps": legacy_steps,
        }
        expected_v1 = "sha256:" + hashlib.sha256(
            json.dumps(
                legacy_payload,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            ).encode("utf-8")
        ).hexdigest()
        self.assertEqual(
            route_plan_fingerprint(
                legacy_steps,
                input_tokens=100,
                cached_input_tokens=0,
                output_tokens=20,
            ),
            expected_v1,
        )
        self.assertNotEqual(v2_digest, expected_v1)

    def test_cache_reuse_requires_exact_provider_model_profile_and_hash(self) -> None:
        provider = default_provider_catalog()["providers"][0]
        identity = ("model-a", "profile-a", "sha256:" + "a" * 64)
        origin = {
            "provider_id": provider["provider_id"],
            "model": identity[0],
            "profile": identity[1],
            "profile_sha256": identity[2],
        }
        reused = derive_step_token_forecast(
            input_tokens=100,
            cached_input_tokens=900,
            output_tokens=20,
            target_provider=provider,
            target_execution_identity=identity,
            step_index=1,
            cache_origin=origin,
        )
        self.assertEqual(
            (reused["estimated_input_tokens"], reused["estimated_cached_input_tokens"]),
            (100, 900),
        )
        self.assertEqual(reused["cache_mode"], "exact-identity-reuse")

        changed_model = derive_step_token_forecast(
            input_tokens=100,
            cached_input_tokens=900,
            output_tokens=20,
            target_provider=provider,
            target_execution_identity=("model-b", identity[1], identity[2]),
            step_index=1,
            cache_origin=origin,
        )
        self.assertEqual(
            (
                changed_model["estimated_input_tokens"],
                changed_model["estimated_cached_input_tokens"],
                changed_model["cache_mode"],
            ),
            (1000, 0, "reclassified-as-ordinary"),
        )

    def test_unproven_first_step_cache_is_priced_as_ordinary(self) -> None:
        provider = default_provider_catalog()["providers"][0]
        forecast = derive_step_token_forecast(
            input_tokens=100,
            cached_input_tokens=900,
            output_tokens=20,
            target_provider=provider,
            target_execution_identity=("LongCat-2.0", "longcat", "sha256:" + "a" * 64),
            step_index=0,
        )
        self.assertEqual(
            forecast,
            {
                "estimated_input_tokens": 1000,
                "estimated_cached_input_tokens": 0,
                "estimated_output_tokens": 20,
                "cache_mode": "reclassified-as-ordinary",
                "cache_binding": None,
            },
        )

    def test_model_ids_are_shell_safe_identifiers(self) -> None:
        catalog = default_provider_catalog()
        catalog["providers"][0]["model"] = "x&echo-injected"
        with self.assertRaisesRegex(RoutingValidationError, "model must"):
            validate_provider_catalog(catalog)

    def test_new_and_legacy_catalogs_keep_provider_id_separate_from_tier(self) -> None:
        new = validate_provider_catalog(default_provider_catalog())
        self.assertEqual(
            [(row["provider_id"], row["tier"]) for row in new["providers"]],
            [("longcat", "low"), ("deepseek", "medium"), ("codex", "high")],
        )
        self.assertEqual(
            [row["env_key"] for row in new["providers"]],
            ["LONGCAT_API_KEY", "DEEPSEEK_API_KEY", "CODEX_API_KEY"],
        )
        legacy = project_provider_catalog({"project_id": "old-v2"})
        self.assertEqual(
            [(row["provider_id"], row["tier"]) for row in legacy["providers"]],
            [("longcat", "low"), ("codex", "high")],
        )
        self.assertIsNone(legacy["providers"][1]["env_key"])
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
        unsafe_profile = default_provider_catalog()
        unsafe_profile["providers"][0]["profile"] = "../stolen"
        cases.append(unsafe_profile)
        for invalid in cases:
            with self.subTest(invalid=invalid):
                with self.assertRaises(RoutingValidationError):
                    validate_provider_catalog(invalid)

        with self.assertRaises(RoutingValidationError):
            project_provider_catalog({"provider_catalog": None})

    def test_auto_route_rejects_unbounded_relevant_peer_sets(self) -> None:
        catalog = default_provider_catalog()
        template = deepcopy(catalog["providers"][0])
        for index in range(16):
            peer = deepcopy(template)
            peer["provider_id"] = f"low-peer-{index}"
            peer["profile"] = f"low-peer-{index}"
            catalog["providers"].append(peer)
        with self.assertRaisesRegex(RoutingValidationError, "too many enabled"):
            decide_route({"risk": "low", "task_type": "analysis"}, catalog)

    def test_explicit_high_provider_ignores_oversized_low_tier(self) -> None:
        catalog = default_provider_catalog()
        template = deepcopy(catalog["providers"][0])
        for index in range(16):
            peer = deepcopy(template)
            peer["provider_id"] = f"low-peer-{index}"
            peer["profile"] = f"low-peer-{index}"
            catalog["providers"].append(peer)

        decision = decide_route(
            {"risk": "low", "task_type": "analysis"},
            catalog,
            requested_provider_id="codex",
        )

        self.assertEqual((decision.provider_id, decision.tier), ("codex", "high"))
        self.assertEqual(decision.candidate_provider_ids, ("codex",))

    def test_high_floor_ignores_oversized_low_tier_for_auto_route(self) -> None:
        catalog = default_provider_catalog()
        template = deepcopy(catalog["providers"][0])
        for index in range(16):
            peer = deepcopy(template)
            peer["provider_id"] = f"low-peer-{index}"
            peer["profile"] = f"low-peer-{index}"
            catalog["providers"].append(peer)

        decision = decide_route(
            {"risk": "high", "task_type": "analysis"},
            catalog,
        )

        self.assertEqual((decision.provider_id, decision.tier), ("codex", "high"))

    def test_explicit_provider_bypasses_oversized_same_tier(self) -> None:
        catalog = default_provider_catalog()
        template = deepcopy(catalog["providers"][0])
        for index in range(16):
            peer = deepcopy(template)
            peer["provider_id"] = f"low-peer-{index}"
            peer["profile"] = f"low-peer-{index}"
            catalog["providers"].append(peer)

        decision = decide_route(
            {"risk": "low", "task_type": "analysis"},
            catalog,
            requested_provider_id="low-peer-15",
        )

        self.assertEqual(decision.provider_id, "low-peer-15")
        self.assertEqual(decision.candidate_provider_ids, ("low-peer-15",))

    def test_explicit_tier_only_sorts_oversized_same_tier(self) -> None:
        catalog = default_provider_catalog()
        template = deepcopy(catalog["providers"][0])
        for index in range(16):
            peer = deepcopy(template)
            peer["provider_id"] = f"low-peer-{index}"
            peer["profile"] = f"low-peer-{index}"
            peer["priority"] = index
            catalog["providers"].append(peer)

        decision = decide_route(
            {"risk": "low", "task_type": "analysis"},
            catalog,
            requested_tier="low",
        )

        self.assertEqual((decision.provider_id, decision.tier), ("low-peer-0", "low"))
        self.assertEqual(len(decision.candidate_provider_ids), 17)

    def test_maximum_bounded_catalog_indexes_history_once(self) -> None:
        base = default_provider_catalog()["providers"]
        providers = []
        for template in base:
            for index in range(16):
                row = deepcopy(template)
                row["provider_id"] = f"{template['provider_id']}-{index}"
                row["profile"] = None
                row["model"] = f"model-{template['tier']}-{index}"
                row["priority"] = index
                row["input_cny_per_1m"] = 1.0 + index
                row["output_cny_per_1m"] = 1.0 + index
                providers.append(row)
        history = [
            {
                "id": f"RES-scale-{index}",
                "attempt_id": f"ATT-scale-{index}",
                "provider_id": providers[index % len(providers)]["provider_id"],
                "model": providers[index % len(providers)]["model"],
                "profile": None,
                "task_type": "analysis",
                "difficulty": "normal",
                "accepted_by_leader": index % 3 == 0,
            }
            for index in range(2_000)
        ]
        started = time.perf_counter()
        decision = decide_route(
            {"risk": "low", "task_type": "analysis"},
            {"schema_version": 1, "providers": providers},
            history=history,
            input_tokens=1_000,
            output_tokens=1_000,
        )
        elapsed = time.perf_counter() - started
        self.assertIn(decision.provider_id, {row["provider_id"] for row in providers})
        self.assertLess(elapsed, 10.0, f"bounded route optimization took {elapsed:.3f}s")

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

    def test_next_stronger_provider_reprices_cached_input_as_ordinary(self) -> None:
        catalog = default_provider_catalog()
        for provider in catalog["providers"]:
            provider["input_cny_per_1m"] = 10.0
            provider["output_cny_per_1m"] = 0.0
        successor = next_stronger_provider(
            catalog,
            "longcat",
            input_tokens=100_000,
            cached_input_tokens=900_000,
        )
        self.assertEqual(successor["provider_id"], "deepseek")
        self.assertEqual(
            successor["token_forecast"],
            {
                "estimated_input_tokens": 1_000_000,
                "estimated_cached_input_tokens": 0,
                "estimated_output_tokens": 0,
                "cache_mode": "reclassified-as-ordinary",
                "cache_binding": None,
            },
        )
        self.assertEqual(successor["estimated_cost_cny"], 10.0)

    def test_next_stronger_provider_honors_capabilities_and_reviewed_chain(self) -> None:
        catalog = default_provider_catalog()
        medium_alt = deepcopy(catalog["providers"][1])
        medium_alt["provider_id"] = "deepseek-value"
        medium_alt["priority"] = 999
        catalog["providers"].append(medium_alt)
        preferred = next_stronger_provider(
            catalog,
            "longcat",
            preferred_provider_ids=("longcat", "deepseek-value", "codex"),
        )
        self.assertEqual(preferred["provider_id"], "deepseek-value")

        catalog["providers"][1]["capabilities"] = []
        medium_alt["capabilities"] = []
        catalog["providers"][2]["capabilities"] = ["vision"]
        compatible = next_stronger_provider(
            catalog,
            "longcat",
            required_capabilities=("vision",),
        )
        self.assertEqual((compatible["provider_id"], compatible["tier"]), ("codex", "high"))

        catalog["providers"][2]["capabilities"] = ["Vision"]
        case_preserved = next_stronger_provider(
            catalog,
            "longcat",
            required_capabilities=("Vision",),
        )
        self.assertEqual(case_preserved["provider_id"], "codex")

    def test_same_tier_preferred_successor_requires_sealed_caller_authority(self) -> None:
        catalog = default_provider_catalog()
        peer = deepcopy(catalog["providers"][0])
        peer.update(
            {
                "provider_id": "low-peer",
                "profile": "low-peer",
                "model": "Low-Peer",
                "priority": 2,
            }
        )
        catalog["providers"].append(peer)
        preferred = ("longcat", "low-peer", "codex")

        ad_hoc = next_stronger_provider(
            catalog,
            "longcat",
            preferred_provider_ids=preferred,
        )
        self.assertEqual(ad_hoc["provider_id"], "deepseek")
        sealed = next_stronger_provider(
            catalog,
            "longcat",
            preferred_provider_ids=preferred,
            allow_same_tier_preferred=True,
        )
        self.assertEqual((sealed["provider_id"], sealed["tier"]), ("low-peer", "low"))
        with self.assertRaisesRegex(RoutingValidationError, "unique provider IDs"):
            next_stronger_provider(
                catalog,
                "longcat",
                preferred_provider_ids=("longcat", "longcat"),
                allow_same_tier_preferred=True,
            )
        with self.assertRaisesRegex(RoutingValidationError, "tier downgrade"):
            next_stronger_provider(
                catalog,
                "codex",
                preferred_provider_ids=("codex", "low-peer"),
                allow_same_tier_preferred=True,
            )

    def test_reviewed_high_never_bypasses_an_available_fallback_medium(self) -> None:
        catalog = default_provider_catalog()
        fallback = deepcopy(catalog["providers"][1])
        catalog["providers"][1]["enabled"] = False
        fallback.update({"provider_id": "medium-alt", "enabled": True, "priority": 50})
        catalog["providers"].append(fallback)
        selected = next_stronger_provider(
            catalog,
            "longcat",
            preferred_provider_ids=("longcat", "deepseek", "codex"),
        )
        self.assertIsNotNone(selected)
        self.assertEqual((selected["provider_id"], selected["tier"]), ("medium-alt", "medium"))

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

    def test_same_tier_selection_without_economic_inputs_uses_priority_then_evidence(self) -> None:
        catalog = default_provider_catalog()
        second = deepcopy(catalog["providers"][1])
        second["provider_id"] = "deepseek-alt"
        second["profile"] = "deepseek-alt"
        catalog["providers"].append(second)
        history = [
            {
                "provider_id": "deepseek-alt",
                "model": "inherit",
                "profile": "deepseek-alt",
                "task_type": "analysis",
                "difficulty": "normal",
                "accepted_by_leader": True,
            }
            for _ in range(8)
        ]
        history.extend(
            {
                "provider_id": "deepseek",
                "model": "inherit",
                "profile": "deepseek",
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

    def test_explicit_tier_uses_conservative_cost_per_accepted_before_priority(self) -> None:
        catalog = default_provider_catalog()
        expensive = catalog["providers"][1]
        expensive.update(
            {
                "priority": 0,
                "input_cny_per_1m": 100.0,
                "output_cny_per_1m": 100.0,
            }
        )
        value = deepcopy(expensive)
        value.update(
            {
                "provider_id": "deepseek-value",
                "profile": "deepseek-value",
                "priority": 1000,
                "input_cny_per_1m": 1.0,
                "output_cny_per_1m": 1.0,
            }
        )
        catalog["providers"].append(value)

        decision = decide_route(
            {"risk": "medium", "task_type": "analysis"},
            catalog,
            requested_tier="medium",
            input_tokens=1_000_000,
            output_tokens=1_000_000,
        )

        self.assertEqual(decision.provider_id, "deepseek-value")
        self.assertEqual(decision.candidate_provider_ids[0], "deepseek-value")
        self.assertEqual(decision.estimated_cost_cny, 2.0)

    def test_acceptance_evidence_is_scoped_to_model_and_profile_identity(self) -> None:
        history = [
            {
                "provider_id": "deepseek",
                "model": "old-good",
                "profile": "deepseek",
                "task_type": "analysis",
                "difficulty": "normal",
                "accepted_by_leader": True,
                "attempt_id": f"ATT-old-{index}",
            }
            for index in range(100)
        ]
        cold = leader_acceptance_prior(
            history,
            "deepseek",
            task_type="analysis",
            difficulty="normal",
            execution_identity=("new-unproven", "deepseek"),
        )
        proven = leader_acceptance_prior(
            history,
            "deepseek",
            task_type="analysis",
            difficulty="normal",
            execution_identity=("old-good", "deepseek"),
        )
        self.assertEqual(cold.observations, 0)
        self.assertEqual(proven.observations, 100)
        self.assertLess(cold.conservative_probability, 0.5)
        self.assertGreater(proven.conservative_probability, 0.9)

    def test_conditional_prior_cache_is_scoped_to_provider_id(self) -> None:
        catalog = default_provider_catalog()
        low = deepcopy(catalog["providers"][0])
        medium_a = deepcopy(catalog["providers"][1])
        low.update({"input_cny_per_1m": 0.1, "output_cny_per_1m": 0.1})
        medium_a.update(
            {
                "provider_id": "medium-a",
                "profile": "shared-medium",
                "model": "shared-model",
                "input_cny_per_1m": 1.0,
                "output_cny_per_1m": 1.0,
            }
        )
        medium_b = deepcopy(medium_a)
        medium_b.update(
            {
                "provider_id": "medium-b",
                "input_cny_per_1m": 0.2,
                "output_cny_per_1m": 0.2,
            }
        )
        catalog["providers"] = [low, medium_a, medium_b]

        history: list[dict] = []
        for provider_id, accepts in (("medium-a", True), ("medium-b", False)):
            for index in range(20):
                task_id = f"TASK-{provider_id}-{index}"
                envelope_id = f"ENV-{provider_id}-{index}"
                fingerprint = "sha256:" + f"{len(history):064x}"[-64:]
                low_result_id = f"RES-low-{provider_id}-{index}"
                low_attempt_id = f"ATT-low-{provider_id}-{index}"
                history.append(
                    {
                        "id": low_result_id,
                        "attempt_id": low_attempt_id,
                        "provider_id": low["provider_id"],
                        "model": low["model"],
                        "profile": low["profile"],
                        "profile_sha256": None,
                        "task_type": "analysis",
                        "difficulty": "normal",
                        "task_id": task_id,
                        "route_envelope_id": envelope_id,
                        "route_plan_fingerprint": fingerprint,
                        "route_plan_step_index": 0,
                        "route_predecessors": [],
                        "accepted_by_leader": False,
                        "status": "escalate",
                    }
                )
                history.append(
                    {
                        "id": f"RES-{provider_id}-{index}",
                        "attempt_id": f"ATT-{provider_id}-{index}",
                        "provider_id": provider_id,
                        "model": "shared-model",
                        "profile": "shared-medium",
                        "profile_sha256": None,
                        "task_type": "analysis",
                        "difficulty": "normal",
                        "task_id": task_id,
                        "route_envelope_id": envelope_id,
                        "route_plan_fingerprint": fingerprint,
                        "route_plan_step_index": 1,
                        "route_predecessors": [
                            {
                                "provider_id": low["provider_id"],
                                "model": low["model"],
                                "profile": low["profile"],
                                "profile_sha256": None,
                                "attempt_id": low_attempt_id,
                                "result_id": low_result_id,
                            }
                        ],
                        "accepted_by_leader": accepts,
                        "status": "done" if accepts else "failed",
                    }
                )

        decision = decide_route(
            {
                "risk": "low",
                "task_type": "analysis",
                "difficulty": "normal",
                "min_success_probability": 0.1,
            },
            catalog,
            history=history,
            input_tokens=1_000_000,
            output_tokens=0,
        )
        self.assertEqual(decision.planned_provider_ids, ("longcat", "medium-a"))

    def test_acceptance_prior_deduplicates_attempts_and_rejects_conflicts(self) -> None:
        duplicate = {
            "provider_id": "deepseek",
            "accepted_by_leader": False,
            "attempt_id": "ATT-duplicate",
        }
        prior = leader_acceptance_prior([duplicate, dict(duplicate)], "deepseek")
        self.assertEqual(prior.observations, 1)
        conflict = {**duplicate, "accepted_by_leader": True}
        with self.assertRaisesRegex(RoutingValidationError, "conflicting leader result"):
            leader_acceptance_prior([duplicate, conflict], "deepseek")

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
            history=paired_chain_history(),
            input_tokens=500_000,
            output_tokens=500_000,
        )
        self.assertEqual(decision.provider_id, "codex")
        self.assertEqual(decision.optimization_mode, "expected-cost-per-accepted")
        self.assertEqual(decision.planned_provider_ids, ("codex",))
        self.assertIsNotNone(decision.expected_cost_per_accepted_cny)

    def test_fresh_priced_route_bootstraps_exact_low_medium_high_lineage(self) -> None:
        catalog = default_provider_catalog()
        for index, provider in enumerate(catalog["providers"], start=1):
            provider["input_cny_per_1m"] = float(index)
            provider["output_cny_per_1m"] = float(index)

        decision = decide_route(
            {"risk": "low", "task_type": "analysis", "difficulty": "normal"},
            catalog,
            input_tokens=1_000_000,
        )

        self.assertEqual(
            decision.planned_provider_ids,
            ("longcat", "deepseek", "codex"),
        )
        self.assertEqual(decision.optimization_mode, "conditional-evidence-bootstrap")
        self.assertEqual(
            [step["acceptance_prior"]["observations"] for step in decision.planned_steps],
            [0, 0, 0],
        )
        self.assertEqual(
            decision.expected_success_probability,
            decision.acceptance_prior.conservative_probability,
        )
        self.assertIn("explicit leader rejection", decision.reason)
        self.assertIn("rather than an assumed SLA probability", decision.reason)

        medium = decide_route(
            {"risk": "medium", "task_type": "analysis", "difficulty": "normal"},
            catalog,
            input_tokens=1_000_000,
        )
        self.assertEqual(medium.planned_provider_ids, ("deepseek", "codex"))
        self.assertEqual(medium.optimization_mode, "conditional-evidence-bootstrap")

        high = decide_route(
            {"risk": "high", "task_type": "analysis", "difficulty": "normal"},
            catalog,
            input_tokens=1_000_000,
        )
        self.assertEqual(high.planned_provider_ids, ("codex",))
        self.assertEqual(high.optimization_mode, "expected-cost-per-accepted")

    def test_mature_optimizer_can_use_distinct_same_tier_peer_then_high(self) -> None:
        catalog = default_provider_catalog()
        peer = deepcopy(catalog["providers"][0])
        peer.update(
            {
                "provider_id": "low-peer",
                "profile": "low-peer",
                "model": "Low-Peer",
                "env_key": "LOW_PEER_API_KEY",
                "priority": 2,
            }
        )
        catalog["providers"].append(peer)
        prices = {
            "longcat": 0.01,
            "low-peer": 0.02,
            "deepseek": 1000.0,
            "codex": 1.0,
        }
        for provider in catalog["providers"]:
            provider["input_cny_per_1m"] = prices[provider["provider_id"]]
            provider["output_cny_per_1m"] = prices[provider["provider_id"]]

        decision = decide_route(
            {
                "risk": "low",
                "task_type": "analysis",
                "difficulty": "normal",
                "min_success_probability": 0.9,
            },
            catalog,
            history=paired_same_tier_history(),
            input_tokens=1_000_000,
        )

        self.assertEqual(
            decision.planned_provider_ids,
            ("longcat", "low-peer", "codex"),
        )
        self.assertEqual(
            [step["tier"] for step in decision.planned_steps],
            ["low", "low", "high"],
        )
        self.assertEqual(len(set(decision.planned_provider_ids)), 3)
        self.assertLessEqual(len(decision.planned_provider_ids), 3)

        bootstrap = decide_route(
            {"risk": "low", "task_type": "analysis", "difficulty": "normal"},
            catalog,
            input_tokens=1_000_000,
        )
        self.assertEqual(bootstrap.optimization_mode, "conditional-evidence-bootstrap")
        self.assertEqual(
            [step["tier"] for step in bootstrap.planned_steps],
            ["low", "medium", "high"],
        )

    def test_bootstrap_requires_ten_exact_observations_then_returns_to_economics(self) -> None:
        catalog = default_provider_catalog()
        prices = {"longcat": 80.0, "deepseek": 20.0, "codex": 1.0}
        for provider in catalog["providers"]:
            price = prices[provider["provider_id"]]
            provider["input_cny_per_1m"] = price
            provider["output_cny_per_1m"] = price

        nine = decide_route(
            {"risk": "low", "task_type": "analysis", "difficulty": "normal"},
            catalog,
            history=paired_chain_history(
                total=9,
                low_accepts=0,
                medium_accepts=0,
                high_accepts=0,
            ),
            input_tokens=1_000_000,
        )
        self.assertEqual(nine.optimization_mode, "conditional-evidence-bootstrap")
        self.assertEqual(nine.planned_provider_ids, ("longcat", "deepseek", "codex"))

        ten = decide_route(
            {"risk": "low", "task_type": "analysis", "difficulty": "normal"},
            catalog,
            history=paired_chain_history(
                total=10,
                low_accepts=0,
                medium_accepts=0,
                high_accepts=0,
            ),
            input_tokens=1_000_000,
        )
        self.assertEqual(ten.optimization_mode, "expected-cost-per-accepted")
        self.assertEqual(ten.planned_provider_ids, ("codex",))

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
            {
                "risk": "low",
                "task_type": "analysis",
                "difficulty": "normal",
                "min_success_probability": 0.8,
            },
            catalog,
            history=paired_chain_history(),
            input_tokens=500_000,
            output_tokens=500_000,
        )
        self.assertEqual(decision.provider_id, "longcat")
        self.assertEqual(decision.planned_provider_ids, ("longcat", "deepseek", "codex"))
        self.assertEqual(len(decision.candidate_provider_ids), len(set(decision.candidate_provider_ids)))
        self.assertEqual([step["index"] for step in decision.planned_steps], [0, 1, 2])
        self.assertEqual(decision.worst_case_chain_cost_cny, 10.11)
        self.assertTrue(decision.plan_fingerprint.startswith("sha256:"))
        repeated = decide_route(
            {
                "risk": "low",
                "task_type": "analysis",
                "difficulty": "normal",
                "min_success_probability": 0.8,
            },
            catalog,
            history=paired_chain_history(),
            input_tokens=500_000,
            output_tokens=500_000,
        )
        self.assertEqual(repeated.plan_fingerprint, decision.plan_fingerprint)
        drifted_steps = deepcopy(list(decision.planned_steps))
        drifted_steps[1]["estimated_cost_cny"] = 999.0
        self.assertNotEqual(
            route_plan_fingerprint(
                drifted_steps,
                input_tokens=500_000,
                cached_input_tokens=0,
                output_tokens=500_000,
            ),
            decision.plan_fingerprint,
        )
        self.assertGreater(decision.expected_success_probability or 0, decision.acceptance_prior.conservative_probability)

    def test_unpaired_marginal_history_cannot_claim_independent_chain_success(self) -> None:
        catalog = default_provider_catalog()
        for provider in catalog["providers"]:
            provider["input_cny_per_1m"] = 1.0
            provider["output_cny_per_1m"] = 1.0
        history: list[dict] = []
        for provider in catalog["providers"]:
            for index in range(100):
                history.append(
                    {
                        "provider_id": provider["provider_id"],
                        "model": provider.get("model") or "inherit",
                        "profile": provider.get("profile"),
                        "task_type": "analysis",
                        "difficulty": "normal",
                        "attempt_id": f"ATT-unpaired-{provider['provider_id']}-{index}",
                        "accepted_by_leader": index < 50,
                    }
                )
        # Every marginal lower bound is about 0.4. The former independent
        # product claimed a three-provider chain above 0.6, even though the
        # same outcomes could be perfectly correlated. Unpaired rows now
        # cannot prove any continuation benefit.
        with self.assertRaisesRegex(
            RoutingValidationError,
            "no priced provider chain satisfies",
        ):
            decide_route(
                {
                    "risk": "low",
                    "task_type": "analysis",
                    "difficulty": "normal",
                    "min_success_probability": 0.6,
                },
                catalog,
                history=history,
                input_tokens=1_000_000,
            )

    def test_conditional_success_never_trains_a_standalone_start_prior(self) -> None:
        conditional = {
            "id": "RES-conditional-high",
            "command_id": "CMD-conditional-high",
            "attempt_id": "ATT-conditional-high",
            "provider_id": "codex",
            "accepted_by_leader": True,
            "route_plan_step_index": 2,
            "route_predecessors": [
                {"provider_id": "longcat"},
                {"provider_id": "deepseek"},
            ],
        }
        prior = leader_acceptance_prior([conditional], "codex")
        self.assertEqual((prior.observations, prior.accepted), (0, 0))

        unconditional = {
            **conditional,
            "id": "RES-unconditional-high",
            "command_id": "CMD-unconditional-high",
            "attempt_id": "ATT-unconditional-high",
            "route_plan_step_index": 0,
            "route_predecessors": [],
        }
        prior = leader_acceptance_prior([conditional, unconditional], "codex")
        self.assertEqual((prior.observations, prior.accepted), (1, 1))

    def test_conditional_prior_rejects_an_impossible_recursive_prefix(self) -> None:
        history = paired_chain_history(
            total=1,
            low_accepts=0,
            medium_accepts=0,
            high_accepts=1,
        )
        medium = next(row for row in history if row["provider_id"] == "deepseek")
        medium["route_predecessors"] = []
        prior = conditional_leader_acceptance_prior(
            history,
            "codex",
            predecessor_execution_identities=(
                ("longcat", "LongCat-2.0", "longcat", None),
                ("deepseek", "inherit", "deepseek", None),
            ),
            task_type="analysis",
            difficulty="normal",
        )
        self.assertEqual((prior.observations, prior.accepted), (0, 0))

    def test_optimizer_enumerates_early_stop_and_skip_tier_chains(self) -> None:
        catalog = default_provider_catalog()
        # High is slightly less cost-efficient than low, so the unconstrained
        # optimum stops early while the SLA-constrained optimum skips the
        # prohibitively expensive medium tier.
        prices = {"longcat": 1.0, "deepseek": 1000.0, "codex": 100.0}
        for provider in catalog["providers"]:
            provider["input_cny_per_1m"] = prices[provider["provider_id"]]
            provider["output_cny_per_1m"] = prices[provider["provider_id"]]
        unconstrained = decide_route(
            {"risk": "low", "task_type": "analysis"},
            catalog,
            history=paired_chain_history(),
            input_tokens=1_000_000,
        )
        self.assertEqual(unconstrained.planned_provider_ids, ("longcat",))
        self.assertEqual(unconstrained.optimization_mode, "expected-cost-per-accepted")
        completion_first = decide_route(
            {
                "risk": "low",
                "task_type": "analysis",
                "routing_objective": "completion-first",
            },
            catalog,
            history=paired_chain_history(),
            input_tokens=1_000_000,
        )
        self.assertEqual(completion_first.routing_objective, "completion-first")
        self.assertEqual(completion_first.planned_provider_ids[-1], "codex")
        self.assertNotEqual(completion_first.planned_provider_ids, ("longcat",))
        self.assertNotEqual(
            completion_first.plan_fingerprint,
            unconstrained.plan_fingerprint,
        )
        explicit_low = decide_route(
            {
                "risk": "low",
                "task_type": "analysis",
                "routing_objective": "completion-first",
            },
            catalog,
            requested_provider_id="longcat",
            history=paired_chain_history(),
            input_tokens=1_000_000,
        )
        self.assertEqual(explicit_low.planned_provider_ids, ("longcat",))
        skip_history = []
        for row in paired_chain_history(medium_accepts=0, high_accepts=115):
            if row["provider_id"] == "deepseek":
                continue
            projected = deepcopy(row)
            if projected["provider_id"] == "codex":
                projected["route_plan_step_index"] = 1
                projected["route_predecessors"] = projected["route_predecessors"][:1]
            skip_history.append(projected)
        constrained = decide_route(
            {
                "risk": "low",
                "task_type": "analysis",
                "min_success_probability": 0.92,
            },
            catalog,
            history=skip_history,
            input_tokens=1_000_000,
        )
        self.assertEqual(constrained.planned_provider_ids, ("longcat", "codex"))
        with self.assertRaises(RoutingValidationError):
            decide_route(
                {
                    "risk": "low",
                    "task_type": "analysis",
                    "routing_objective": "cheapest-at-any-cost",
                },
                catalog,
                input_tokens=1_000_000,
            )

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

    def test_zero_success_floor_does_not_require_economic_pricing(self) -> None:
        decision = decide_route(
            {
                "risk": "low",
                "task_type": "analysis",
                "min_success_probability": 0.0,
            },
            default_provider_catalog(),
        )
        self.assertEqual(decision.provider_id, "longcat")
        self.assertEqual(decision.optimization_mode, "safe-tier")


if __name__ == "__main__":
    unittest.main(verbosity=2)
