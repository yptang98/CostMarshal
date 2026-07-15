#!/usr/bin/env python3
"""Seeded independent exhaustive oracle for cross-tier route optimization."""

from __future__ import annotations

from itertools import product
import math
import random
import sys
from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from costmarshal_v2.routing import RoutingValidationError, decide_route  # noqa: E402


SEED = 20260716
CASE_COUNT = 10_000
TIERS = ("low", "medium", "high")
TIER_RANK = {tier: index for index, tier in enumerate(TIERS)}
LOW_TYPES = {
    "analysis",
    "documentation",
    "extraction",
    "mechanical",
    "small-edit",
    "summarization",
    "test",
    "verification",
}
MEDIUM_TYPES = {"implementation", "review", "code-review"}


def independent_floor(task: dict) -> str:
    if task["risk"] == "high" or task["difficulty"] == "hard":
        return "high"
    if task["risk"] == "medium" or task["task_type"] in MEDIUM_TYPES:
        return "medium"
    if task["risk"] == "low" and task["task_type"] in LOW_TYPES:
        return "low"
    return "medium"


def wilson_lower(successes: float, trials: float, z: float = 1.96) -> float:
    proportion = successes / trials
    denominator = 1.0 + z * z / trials
    center = proportion + z * z / (2.0 * trials)
    margin = z * math.sqrt(
        (proportion * (1.0 - proportion) + z * z / (4.0 * trials)) / trials
    )
    return max(0.0, (center - margin) / denominator)


def independent_probability(history: list[dict], provider_id: str) -> float:
    rows = [
        row
        for row in history
        if row.get("provider_id") == provider_id and type(row.get("accepted_by_leader")) is bool
    ]
    accepted = sum(row["accepted_by_leader"] is True for row in rows)
    posterior_successes = 1.0 + accepted
    trials = 3.0 + len(rows)
    mean = posterior_successes / trials
    return round(min(mean, wilson_lower(posterior_successes, trials)), 6)


def independent_cost(provider: dict, input_tokens: int, output_tokens: int) -> float:
    return round(
        (
            input_tokens * float(provider["input_cny_per_1m"])
            + output_tokens * float(provider["output_cny_per_1m"])
        )
        / 1_000_000,
        9,
    )


def independent_oracle(
    task: dict,
    catalog: dict,
    history: list[dict],
    input_tokens: int,
    output_tokens: int,
) -> dict | None:
    floor = independent_floor(task)
    required = set(task.get("required_capabilities") or [])
    eligible = [
        provider
        for provider in catalog["providers"]
        if provider["enabled"] and required.issubset(set(provider["capabilities"]))
    ]
    if not eligible or not any(TIER_RANK[row["tier"]] >= TIER_RANK[floor] for row in eligible):
        return None

    probability = {
        provider["provider_id"]: independent_probability(history, provider["provider_id"])
        for provider in eligible
    }
    cost = {
        provider["provider_id"]: independent_cost(provider, input_tokens, output_tokens)
        for provider in eligible
    }

    def peer_key(provider: dict) -> tuple:
        return (
            provider["priority"],
            -probability[provider["provider_id"]],
            cost[provider["provider_id"]],
            provider["provider_id"],
        )

    plans: list[dict] = []
    for start in eligible:
        if TIER_RANK[start["tier"]] < TIER_RANK[floor]:
            continue
        stronger_groups: list[list[dict]] = []
        for tier in TIERS[TIER_RANK[start["tier"]] + 1 :]:
            peers = sorted((row for row in eligible if row["tier"] == tier), key=peer_key)
            if peers:
                stronger_groups.append(peers)
        continuations = product(*stronger_groups) if stronger_groups else [()]
        for continuation in continuations:
            chain = (start, *continuation)
            survival = 1.0
            expected_cost = 0.0
            for provider in chain:
                provider_id = provider["provider_id"]
                expected_cost += survival * cost[provider_id]
                survival *= 1.0 - probability[provider_id]
            success_probability = 1.0 - survival
            plans.append(
                {
                    "provider": start,
                    "chain": tuple(row["provider_id"] for row in chain),
                    "expected_cost": round(expected_cost, 9),
                    "success_probability": round(success_probability, 9),
                    "objective": round(expected_cost / success_probability, 9),
                    "first_cost": cost[start["provider_id"]],
                }
            )
    plans.sort(
        key=lambda plan: (
            plan["objective"],
            -plan["success_probability"],
            plan["provider"]["priority"],
            plan["provider"]["provider_id"],
        )
    )
    minimum_success = task.get("min_success_probability")
    if minimum_success is not None:
        plans = [plan for plan in plans if plan["success_probability"] >= minimum_success]
    return plans[0] if plans else None


def random_case(rng: random.Random, index: int) -> tuple[dict, dict, list[dict], int, int]:
    providers: list[dict] = []
    capability_options = [[], ["text"], ["vision"], ["tools"], ["text", "tools"]]
    for tier in TIERS:
        for peer in range(rng.randint(1, 3)):
            providers.append(
                {
                    "provider_id": f"{tier}-{index}-{peer}",
                    "tier": tier,
                    "profile": f"{tier}-{peer}",
                    "model": "inherit",
                    "env_key": None,
                    "enabled": rng.random() < 0.82,
                    "priority": rng.randint(0, 3),
                    "input_cny_per_1m": rng.choice([0.1, 0.5, 1.0, 2.0, 5.0, 13.0]),
                    "output_cny_per_1m": rng.choice([0.2, 1.0, 3.0, 8.0, 21.0, 55.0]),
                    "capabilities": list(rng.choice(capability_options)),
                }
            )
    catalog = {"schema_version": 1, "providers": providers}
    task_type = rng.choice(
        ["analysis", "documentation", "test", "implementation", "review", "architecture"]
    )
    required = rng.choice([[], ["text"], ["vision"], ["tools"], ["text", "tools"]])
    task = {
        "risk": rng.choice(["low", "medium", "high"]),
        "difficulty": rng.choice(["simple", "normal", "hard"]),
        "task_type": task_type,
        "required_capabilities": list(required),
    }
    minimum_success = rng.choice([None, None, 0.0, 0.05, 0.1, 0.2, 0.5, 0.9])
    if minimum_success is not None:
        task["min_success_probability"] = minimum_success

    history: list[dict] = []
    for provider in providers:
        observations = rng.randint(0, 12)
        for _ in range(observations):
            history.append(
                {
                    "provider_id": provider["provider_id"],
                    "task_type": task_type,
                    "difficulty": task["difficulty"],
                    "accepted_by_leader": rng.random() < rng.uniform(0.05, 0.95),
                }
            )
    return task, catalog, history, rng.randint(1, 1_000_000), rng.randint(1, 250_000)


class RouteOracleTest(unittest.TestCase):
    def test_seeded_exhaustive_oracle_matches_ten_thousand_cases(self) -> None:
        rng = random.Random(SEED)
        routed = 0
        rejected = 0
        for index in range(CASE_COUNT):
            task, catalog, history, input_tokens, output_tokens = random_case(rng, index)
            oracle = independent_oracle(task, catalog, history, input_tokens, output_tokens)
            if oracle is None:
                with self.assertRaises(RoutingValidationError, msg=f"case={index} task={task}"):
                    decide_route(
                        task,
                        catalog,
                        history=history,
                        input_tokens=input_tokens,
                        output_tokens=output_tokens,
                    )
                rejected += 1
                continue

            decision = decide_route(
                task,
                catalog,
                history=history,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
            )
            message = f"case={index} task={task} oracle={oracle} decision={decision.to_dict()}"
            self.assertEqual(decision.provider_id, oracle["provider"]["provider_id"], message)
            self.assertEqual(decision.planned_provider_ids, oracle["chain"], message)
            self.assertEqual(decision.estimated_cost_cny, oracle["first_cost"], message)
            self.assertEqual(decision.expected_chain_cost_cny, oracle["expected_cost"], message)
            self.assertEqual(
                decision.expected_success_probability,
                oracle["success_probability"],
                message,
            )
            self.assertEqual(decision.expected_cost_per_accepted_cny, oracle["objective"], message)
            self.assertGreaterEqual(TIER_RANK[decision.tier], TIER_RANK[independent_floor(task)], message)
            selected = next(row for row in catalog["providers"] if row["provider_id"] == decision.provider_id)
            self.assertTrue(
                set(task["required_capabilities"]).issubset(set(selected["capabilities"])),
                message,
            )
            if task.get("min_success_probability") is not None:
                self.assertGreaterEqual(
                    decision.expected_success_probability or 0.0,
                    task["min_success_probability"],
                    message,
                )
            routed += 1
        self.assertEqual(routed + rejected, CASE_COUNT)
        self.assertGreater(routed, 1000)
        self.assertGreater(rejected, 1000)
        print({"seed": SEED, "cases": CASE_COUNT, "routed": routed, "rejected": rejected})


if __name__ == "__main__":
    unittest.main(verbosity=2)
