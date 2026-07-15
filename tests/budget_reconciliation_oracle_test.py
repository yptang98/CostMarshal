from __future__ import annotations

import math
import random
import sys
import unittest
from decimal import Decimal
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from costmarshal_v2.scheduler import attempt_budget_commitment  # noqa: E402


ACTIVE_STATUSES = {"preparing", "dispatched", "running", "starting", "needs_recovery"}
TERMINAL_STATUSES = {"waiting_leader", "done", "failed", "cancelled", "escalated"}
ORACLE_CASES = 20_000


class OracleRejected(ValueError):
    """The independent model cannot safely reconcile an attempt."""


def oracle_money(attempt: dict[str, Any], field: str) -> Decimal:
    if field not in attempt or attempt[field] is None or isinstance(attempt[field], bool):
        raise OracleRejected(f"{field} is unknown")
    try:
        value = Decimal(str(attempt[field]))
    except Exception as exc:  # Decimal has several input-specific exception subclasses.
        raise OracleRejected(f"{field} is invalid") from exc
    if not value.is_finite() or value < 0:
        raise OracleRejected(f"{field} must be finite and non-negative")
    return value


def oracle_commitment(attempt: dict[str, Any]) -> Decimal:
    active_or_unsettled = (
        attempt.get("status") in ACTIVE_STATUSES or not bool(attempt.get("cost_settled"))
    )
    if active_or_unsettled:
        reserved = oracle_money(attempt, "reserved_cost_cny")
        actual = oracle_money(attempt, "actual_cost_cny")
        return max(reserved, actual)
    return oracle_money(attempt, "actual_cost_cny")


def random_money(randomizer: random.Random) -> Decimal:
    return Decimal(randomizer.randrange(0, 50_000_001)) / Decimal(1_000_000)


class BudgetReconciliationOracleTest(unittest.TestCase):
    def test_20k_random_attempts_match_independent_decimal_oracle(self) -> None:
        randomizer = random.Random(0xC057A125)
        statuses = tuple(sorted(ACTIVE_STATUSES | TERMINAL_STATUSES))
        scenario_counts = {
            "reservation_held": 0,
            "actual_over_reservation": 0,
            "refund_released": 0,
            "unsettled_terminal": 0,
        }
        for case_number in range(ORACLE_CASES):
            status = randomizer.choice(statuses)
            settled = bool(randomizer.getrandbits(1))
            reserved = random_money(randomizer)
            actual = random_money(randomizer)
            attempt = {
                "attempt_id": f"ATT-oracle-{case_number}",
                "status": status,
                "cost_settled": settled,
                "reserved_cost_cny": float(reserved),
                "actual_cost_cny": float(actual),
            }
            expected = oracle_commitment(attempt)
            observed = attempt_budget_commitment(attempt)
            self.assertTrue(math.isfinite(observed), attempt)
            self.assertAlmostEqual(observed, float(expected), places=9, msg=str(attempt))

            if status in ACTIVE_STATUSES and reserved >= actual:
                scenario_counts["reservation_held"] += 1
            if actual > reserved:
                scenario_counts["actual_over_reservation"] += 1
            if status in TERMINAL_STATUSES and settled and reserved > actual:
                scenario_counts["refund_released"] += 1
            if status in TERMINAL_STATUSES and not settled:
                scenario_counts["unsettled_terminal"] += 1

        self.assertTrue(all(count >= 500 for count in scenario_counts.values()), scenario_counts)

    def test_reservation_actual_and_refund_examples(self) -> None:
        cases = [
            (
                "active reservation remains committed",
                {
                    "status": "running",
                    "cost_settled": False,
                    "reserved_cost_cny": 3.5,
                    "actual_cost_cny": 1.25,
                },
                Decimal("3.5"),
            ),
            (
                "actual spend exceeding reservation is committed",
                {
                    "status": "running",
                    "cost_settled": False,
                    "reserved_cost_cny": 3.5,
                    "actual_cost_cny": 4.25,
                },
                Decimal("4.25"),
            ),
            (
                "settlement releases unused reservation as refund",
                {
                    "status": "done",
                    "cost_settled": True,
                    "reserved_cost_cny": 3.5,
                    "actual_cost_cny": 1.25,
                },
                Decimal("1.25"),
            ),
            (
                "terminal but unsettled attempt retains reservation",
                {
                    "status": "failed",
                    "cost_settled": False,
                    "reserved_cost_cny": 3.5,
                    "actual_cost_cny": 1.25,
                },
                Decimal("3.5"),
            ),
        ]
        for label, attempt, expected in cases:
            with self.subTest(label=label):
                self.assertEqual(oracle_commitment(attempt), expected)
                self.assertAlmostEqual(attempt_budget_commitment(attempt), float(expected), places=9)

    def test_unknown_price_and_corrupt_money_fail_closed(self) -> None:
        invalid_attempts = [
            ("v2.2 active attempt has no price fields", {"status": "running", "cost_settled": False}),
            (
                "unknown reservation",
                {
                    "status": "running",
                    "cost_settled": False,
                    "reserved_cost_cny": None,
                    "actual_cost_cny": 0.0,
                },
            ),
            (
                "unknown active actual",
                {
                    "status": "running",
                    "cost_settled": False,
                    "reserved_cost_cny": 1.0,
                    "actual_cost_cny": None,
                },
            ),
            (
                "unknown settled actual",
                {
                    "status": "done",
                    "cost_settled": True,
                    "reserved_cost_cny": 1.0,
                    "actual_cost_cny": None,
                },
            ),
            (
                "negative reservation",
                {
                    "status": "running",
                    "cost_settled": False,
                    "reserved_cost_cny": -0.01,
                    "actual_cost_cny": 0.0,
                },
            ),
            (
                "negative actual",
                {
                    "status": "done",
                    "cost_settled": True,
                    "reserved_cost_cny": 1.0,
                    "actual_cost_cny": -0.01,
                },
            ),
            (
                "NaN reservation",
                {
                    "status": "running",
                    "cost_settled": False,
                    "reserved_cost_cny": float("nan"),
                    "actual_cost_cny": 0.0,
                },
            ),
            (
                "infinite actual",
                {
                    "status": "done",
                    "cost_settled": True,
                    "reserved_cost_cny": 1.0,
                    "actual_cost_cny": float("inf"),
                },
            ),
            (
                "boolean is not money",
                {
                    "status": "running",
                    "cost_settled": False,
                    "reserved_cost_cny": True,
                    "actual_cost_cny": 0.0,
                },
            ),
        ]
        for label, attempt in invalid_attempts:
            with self.subTest(label=label):
                with self.assertRaises(OracleRejected):
                    oracle_commitment(attempt)
                with self.assertRaises(ValueError, msg=label):
                    attempt_budget_commitment(attempt)


if __name__ == "__main__":
    unittest.main(verbosity=2)
