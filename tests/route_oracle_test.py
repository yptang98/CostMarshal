#!/usr/bin/env python3
"""Seeded independent exhaustive oracle for cross-tier route optimization."""

from __future__ import annotations

from decimal import Decimal, ROUND_CEILING
import math
import random
import sys
from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from costmarshal_v2.routing import (  # noqa: E402
    RoutingValidationError,
    build_pricing_snapshot,
    decide_route,
    default_provider_catalog,
)


SEED = 20260716
CASE_COUNT = 10_000
TIERS = ("low", "medium", "high")
TIER_RANK = {tier: index for index, tier in enumerate(TIERS)}
BOOTSTRAP_MIN_CONDITIONAL_OBSERVATIONS = 10
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


def independent_probability(history: list[dict], provider: dict, task: dict) -> float:
    rows = [
        row
        for row in history
        if row.get("provider_id") == provider["provider_id"]
        and row.get("model") == (provider.get("model") or "inherit")
        and row.get("profile") == provider.get("profile")
        and type(row.get("accepted_by_leader")) is bool
    ]
    type_rows = [row for row in rows if row.get("task_type") == task["task_type"]]
    if type_rows:
        rows = type_rows
        difficulty_rows = [
            row for row in rows if row.get("difficulty") == task["difficulty"]
        ]
        if difficulty_rows:
            rows = difficulty_rows
    accepted = sum(row["accepted_by_leader"] is True for row in rows)
    posterior_successes = 1.0 + accepted
    trials = 3.0 + len(rows)
    mean = posterior_successes / trials
    return round(min(mean, wilson_lower(posterior_successes, trials)), 6)


def independent_sla_observations(history: list[dict], provider: dict, task: dict) -> int:
    """Count only the exact execution/task scope admissible for an SLA proof."""

    return sum(
        1
        for row in history
        if row.get("provider_id") == provider["provider_id"]
        and row.get("model") == (provider.get("model") or "inherit")
        and row.get("profile") == provider.get("profile")
        and row.get("task_type") == task["task_type"]
        and row.get("difficulty") == task["difficulty"]
        and type(row.get("accepted_by_leader")) is bool
    )


def independent_cost(
    provider: dict,
    input_tokens: int,
    cached_input_tokens: int,
    output_tokens: int,
) -> Decimal | None:
    pricing = provider.get("pricing")
    if isinstance(pricing, dict):
        cached_rate = pricing.get("cached_input_per_1m")
        if cached_input_tokens and cached_rate is None:
            return None
        input_rate = pricing["input_per_1m"]
        output_rate = pricing["output_per_1m"]
        fixed_attempt = pricing.get("fixed_attempt", pricing.get("fixed_request", "0"))
    else:
        if cached_input_tokens:
            return None
        cached_rate = 0
        input_rate = provider["input_cny_per_1m"]
        output_rate = provider["output_cny_per_1m"]
        fixed_attempt = 0
    input_nano = int(Decimal(str(input_rate)) * 1_000_000_000)
    cached_nano = int(Decimal(str(cached_rate or 0)) * 1_000_000_000)
    output_nano = int(Decimal(str(output_rate)) * 1_000_000_000)
    nano_cny = (
        Decimal(
            input_tokens * input_nano
            + cached_input_tokens * cached_nano
            + output_tokens * output_nano
        )
        / Decimal(1_000_000)
    ).to_integral_value(rounding=ROUND_CEILING)
    fixed_nano = int(Decimal(str(fixed_attempt)) * 1_000_000_000)
    return (nano_cny + fixed_nano) / Decimal(1_000_000_000)


def display(value: Decimal) -> float:
    return float(
        value.quantize(Decimal("0.000000001"))
    )


def independent_oracle(
    task: dict,
    catalog: dict,
    history: list[dict],
    input_tokens: int,
    output_tokens: int,
    cached_input_tokens: int = 0,
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
        provider["provider_id"]: independent_probability(history, provider, task)
        for provider in eligible
    }
    plans: list[dict] = []
    ordered_eligible = tuple(
        sorted(
            eligible,
            key=lambda provider: (
                TIER_RANK[provider["tier"]],
                provider["priority"],
                provider["provider_id"],
            ),
        )
    )

    def continuations(
        prefix: tuple[dict, ...],
        *,
        last_rank: int,
        seen: frozenset[str],
    ):
        yield prefix
        if len(prefix) >= 2:
            return
        for provider in ordered_eligible:
            provider_id = provider["provider_id"]
            rank = TIER_RANK[provider["tier"]]
            if provider_id in seen or rank < last_rank:
                continue
            yield from continuations(
                (*prefix, provider),
                last_rank=rank,
                seen=seen | {provider_id},
            )

    for start in eligible:
        if TIER_RANK[start["tier"]] < TIER_RANK[floor]:
            continue
        for continuation in continuations(
            (),
            last_rank=TIER_RANK[start["tier"]],
            seen=frozenset({start["provider_id"]}),
        ):
            chain = (start, *continuation)
            survival = Decimal(1)
            expected_cost = Decimal(0)
            step_costs: list[Decimal] = []
            step_forecasts: list[tuple[int, int, int]] = []
            for step_index, provider in enumerate(chain):
                provider_id = provider["provider_id"]
                step_input = input_tokens if step_index == 0 else input_tokens + cached_input_tokens
                step_cached = cached_input_tokens if step_index == 0 else 0
                step_cost = independent_cost(
                    provider,
                    step_input,
                    step_cached,
                    output_tokens,
                )
                if step_cost is None:
                    break
                step_costs.append(step_cost)
                step_forecasts.append((step_input, step_cached, output_tokens))
                expected_cost += survival * step_cost
                # Random oracle rows deliberately have no task-linked route
                # lineage. Only the first marginal can prove success; a later
                # hop has zero conditional lower bound until paired history
                # exists. Dedicated contract tests cover paired chains.
                step_probability = probability[provider_id] if step_index == 0 else 0.0
                survival *= Decimal(1) - Decimal(str(step_probability))
            if len(step_costs) != len(chain):
                continue
            success_probability = Decimal(1) - survival
            objective = expected_cost / success_probability
            plans.append(
                {
                    "provider": start,
                    "chain": tuple(row["provider_id"] for row in chain),
                    "expected_cost": display(expected_cost),
                    "success_probability": display(success_probability),
                    "objective": display(objective),
                    "first_cost": float(step_costs[0]),
                    "worst_cost": display(sum(step_costs, Decimal(0))),
                    "step_costs": tuple(display(value) for value in step_costs),
                    "step_forecasts": tuple(step_forecasts),
                    "_success": success_probability,
                    "_objective": objective,
                    # Random oracle rows are intentionally unconditional.  No
                    # continuation lineage can therefore be counted as mature.
                    "_conditional_exact_observations": tuple(0 for _ in chain[1:]),
                }
            )
    plans.sort(
        key=lambda plan: (
            plan["_objective"],
            -plan["_success"],
            plan["provider"]["priority"],
            plan["provider"]["provider_id"],
            plan["chain"],
        )
    )
    minimum_success = task.get("min_success_probability")
    if minimum_success is not None and minimum_success > 0:
        floor = Decimal(str(minimum_success))
        plans = [
            plan
            for plan in plans
            if plan["_success"] >= floor
            and independent_sla_observations(
                history,
                plan["provider"],
                task,
            )
            >= 10
        ]
    else:
        available_tiers = tuple(
            tier
            for tier in TIERS[TIER_RANK[floor] :]
            if any(provider["tier"] == tier for provider in eligible)
        )
        if len(available_tiers) > 1:
            full_plans = [
                plan
                for plan in plans
                if tuple(
                    next(
                        provider["tier"]
                        for provider in eligible
                        if provider["provider_id"] == provider_id
                    )
                    for provider_id in plan["chain"]
                )
                == available_tiers
            ]
            if full_plans and any(
                count < BOOTSTRAP_MIN_CONDITIONAL_OBSERVATIONS
                for count in full_plans[0]["_conditional_exact_observations"]
            ):
                return full_plans[0]
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
        for observation in range(observations):
            history.append(
                {
                    "provider_id": provider["provider_id"],
                    "model": provider.get("model") or "inherit",
                    "profile": provider.get("profile"),
                    "task_type": task_type,
                    "difficulty": task["difficulty"],
                    "attempt_id": f"ATT-{provider['provider_id']}-{observation}",
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
