#!/usr/bin/env python3
"""Contract tests for canonical pricing snapshots and freshness gates."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import FrozenInstanceError
from datetime import datetime, timedelta, timezone
import json
import subprocess
import sys
from pathlib import Path
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
CLI = ROOT / "scripts" / "costmarshal.py"
sys.path.insert(0, str(ROOT))

from costmarshal_v2.routing import (  # noqa: E402
    RoutingValidationError,
    build_pricing_snapshot,
    decide_route,
    default_provider_catalog,
    estimate_cost_cny,
    pricing_snapshot_hash,
    pricing_snapshot_status,
    provider_by_id,
    validate_provider_catalog,
)


NOW = "2026-07-16T12:00:00Z"


def canonical_catalog(
    *,
    reviewed_at: str = "2026-07-15T00:00:00Z",
    effective_at: str = "2026-07-15T00:00:00Z",
    expires_at: str = "2026-08-15T00:00:00Z",
) -> dict:
    catalog = default_provider_catalog()
    prices = {
        "longcat": (1.0, 0.25, 2.0, 0.01),
        "deepseek": (2.0, 0.5, 4.0, 0.02),
        "codex": (8.0, 2.0, 24.0, 0.05),
    }
    for provider in catalog["providers"]:
        provider.pop("input_cny_per_1m", None)
        provider.pop("output_cny_per_1m", None)
        input_price, cached_price, output_price, fixed_request = prices[provider["provider_id"]]
        provider["pricing"] = build_pricing_snapshot(
            currency="CNY",
            source=f"https://pricing.example/{provider['provider_id']}",
            reviewed_at=reviewed_at,
            effective_at=effective_at,
            expires_at=expires_at,
            snapshot_id=f"release-2026-07-{provider['provider_id']}",
            input_per_1m=input_price,
            cached_input_per_1m=cached_price,
            output_per_1m=output_price,
            fixed_request=fixed_request,
        )
    return catalog


class PricingMetadataTest(unittest.TestCase):
    def test_snapshot_schema_hash_and_validation_are_canonical(self) -> None:
        catalog = canonical_catalog()
        original = deepcopy(catalog)
        normalized = validate_provider_catalog(catalog)
        self.assertEqual(catalog, original)
        snapshot = normalized["providers"][0]["pricing"]
        self.assertEqual(snapshot["snapshot_hash"], pricing_snapshot_hash(snapshot))
        self.assertEqual(snapshot["currency"], "CNY")
        self.assertEqual(pricing_snapshot_status(normalized["providers"][0], now=NOW), "current")

        bad_hash = deepcopy(catalog)
        bad_hash["providers"][0]["pricing"]["snapshot_hash"] = "sha256:" + "0" * 64
        with self.assertRaisesRegex(RoutingValidationError, "does not match"):
            validate_provider_catalog(bad_hash)

    def test_snapshot_requires_metadata_and_rejects_unknown_billing_dimensions(self) -> None:
        catalog = canonical_catalog()
        for field in (
            "currency",
            "source",
            "reviewed_at",
            "effective_at",
            "expires_at",
            "snapshot_id",
            "snapshot_hash",
        ):
            broken = deepcopy(catalog)
            broken["providers"][0]["pricing"].pop(field)
            with self.subTest(field=field):
                with self.assertRaises(RoutingValidationError):
                    validate_provider_catalog(broken)

        unsupported = deepcopy(catalog)
        unsupported["providers"][0]["pricing"]["image_cny_each"] = 0.1
        with self.assertRaisesRegex(RoutingValidationError, "unsupported charging dimensions"):
            validate_provider_catalog(unsupported)

        naive_time = deepcopy(catalog)
        snapshot = naive_time["providers"][0]["pricing"]
        snapshot["reviewed_at"] = "2026-07-15T00:00:00"
        with self.assertRaisesRegex(RoutingValidationError, "timezone"):
            validate_provider_catalog(naive_time)

    def test_current_snapshot_is_attached_immutably_to_route_decision(self) -> None:
        catalog = canonical_catalog()
        decision = decide_route(
            {"risk": "low", "task_type": "analysis"},
            catalog,
            input_tokens=1_000_000,
            output_tokens=100_000,
            now=NOW,
        )
        self.assertEqual(decision.pricing_status, "current")
        self.assertEqual(decision.pricing_currency, "CNY")
        self.assertIsNotNone(decision.price_snapshot)
        assert decision.price_snapshot is not None
        self.assertEqual(
            decision.price_snapshot.snapshot_hash,
            pricing_snapshot_hash(decision.price_snapshot.to_dict()),
        )
        payload = decision.to_dict()
        payload["price_snapshot"]["source"] = "tampered"
        self.assertNotEqual(payload["price_snapshot"]["source"], decision.price_snapshot.source)
        with self.assertRaises(FrozenInstanceError):
            decision.price_snapshot.source = "tampered"  # type: ignore[misc]

    def test_expired_and_future_snapshots_degrade_safely_at_fixed_clock(self) -> None:
        cases = {
            "expired": canonical_catalog(expires_at="2026-07-16T12:00:00Z"),
            "future-reviewed": canonical_catalog(reviewed_at="2026-07-17T00:00:00Z"),
            "future-effective": canonical_catalog(effective_at="2026-07-17T00:00:00Z"),
        }
        for expected, catalog in cases.items():
            with self.subTest(expected=expected):
                decision = decide_route(
                    {"risk": "low", "task_type": "analysis"},
                    catalog,
                    input_tokens=10_000,
                    output_tokens=1_000,
                    now=NOW,
                )
                self.assertIn(expected, decision.pricing_status)
                self.assertEqual(decision.optimization_mode, "safe-tier")
                self.assertIsNone(decision.estimated_cost_cny)
                self.assertIsNone(decision.price_snapshot)
                self.assertIn("economic pricing gate degraded", decision.explanation)
                with self.assertRaisesRegex(RoutingValidationError, "pricing status"):
                    decide_route(
                        {
                            "risk": "low",
                            "task_type": "analysis",
                            "min_success_probability": 0.1,
                        },
                        catalog,
                        input_tokens=10_000,
                        output_tokens=1_000,
                        now=NOW,
                    )

    def test_mixed_or_unsupported_currency_never_emits_cny_cost(self) -> None:
        mixed = canonical_catalog()
        mixed_snapshot = mixed["providers"][1]["pricing"]
        mixed_snapshot["currency"] = "USD"
        mixed_snapshot["snapshot_hash"] = pricing_snapshot_hash(mixed_snapshot)
        decision = decide_route(
            {"risk": "low", "task_type": "analysis"},
            mixed,
            input_tokens=10_000,
            output_tokens=1_000,
            now=NOW,
        )
        self.assertEqual(decision.pricing_status, "mixed-currency")
        self.assertIsNone(decision.estimated_cost_cny)

        usd = canonical_catalog()
        for provider in usd["providers"]:
            provider["pricing"]["currency"] = "USD"
            provider["pricing"]["snapshot_hash"] = pricing_snapshot_hash(provider["pricing"])
        decision = decide_route(
            {"risk": "low", "task_type": "analysis"},
            usd,
            input_tokens=10_000,
            output_tokens=1_000,
            now=NOW,
        )
        self.assertEqual(decision.pricing_status, "unsupported-currency:USD")
        self.assertIsNone(decision.estimated_cost_cny)

    def test_supported_cost_dimensions_and_unsupported_cached_legacy_dimension(self) -> None:
        provider = provider_by_id(canonical_catalog(), "deepseek")
        self.assertEqual(
            estimate_cost_cny(
                provider,
                input_tokens=1_000_000,
                cached_input_tokens=500_000,
                output_tokens=250_000,
            ),
            3.27,
        )
        no_cached = deepcopy(provider)
        no_cached["pricing"]["cached_input_per_1m"] = None
        no_cached["pricing"]["snapshot_hash"] = pricing_snapshot_hash(no_cached["pricing"])
        self.assertIsNone(
            estimate_cost_cny(
                no_cached,
                input_tokens=0,
                cached_input_tokens=1,
                output_tokens=0,
            )
        )

        legacy = provider_by_id(default_provider_catalog(), "deepseek")
        legacy["input_cny_per_1m"] = 1.0
        legacy["output_cny_per_1m"] = 2.0
        with self.assertRaisesRegex(RoutingValidationError, "does not support cached_input_tokens"):
            estimate_cost_cny(
                legacy,
                input_tokens=0,
                cached_input_tokens=1,
                output_tokens=0,
            )

    def test_beta_legacy_prices_remain_explicitly_compatible(self) -> None:
        catalog = default_provider_catalog()
        for provider in catalog["providers"]:
            provider["input_cny_per_1m"] = 1.0
            provider["output_cny_per_1m"] = 2.0
        decision = decide_route(
            {"risk": "low", "task_type": "analysis"},
            catalog,
            input_tokens=1000,
            output_tokens=100,
            now=NOW,
        )
        self.assertEqual(decision.pricing_status, "beta-legacy")
        self.assertEqual(decision.optimization_mode, "expected-cost-per-accepted")
        self.assertIsNone(decision.price_snapshot)
        self.assertIsNotNone(decision.estimated_cost_cny)

        mixed = canonical_catalog()
        mixed["providers"][0].pop("pricing")
        mixed["providers"][0]["input_cny_per_1m"] = 1.0
        mixed["providers"][0]["output_cny_per_1m"] = 2.0
        decision = decide_route(
            {"risk": "low", "task_type": "analysis"},
            mixed,
            input_tokens=1000,
            output_tokens=100,
            now=NOW,
        )
        self.assertEqual(decision.pricing_status, "mixed-canonical-and-beta-legacy")
        self.assertIsNone(decision.estimated_cost_cny)

    def test_dispatch_persists_selected_immutable_price_snapshot(self) -> None:
        clock = datetime.now(timezone.utc)
        reviewed = (clock - timedelta(days=1)).isoformat().replace("+00:00", "Z")
        expires = (clock + timedelta(days=1)).isoformat().replace("+00:00", "Z")
        catalog = canonical_catalog(
            reviewed_at=reviewed,
            effective_at=reviewed,
            expires_at=expires,
        )
        with tempfile.TemporaryDirectory(prefix="costmarshal-pricing-dispatch-") as raw:
            temp = Path(raw)
            workspace = temp / "workspace"
            workspace.mkdir()
            catalog_path = temp / "catalog.json"
            catalog_path.write_text(json.dumps(catalog), encoding="utf-8")

            def run(*args: str) -> dict:
                completed = subprocess.run(
                    [sys.executable, str(CLI), "--root", str(temp / "runtime"), *args],
                    cwd=temp,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    check=True,
                )
                return json.loads(completed.stdout)

            created = run(
                "init",
                "--name",
                "pricing-snapshot",
                "--objective",
                "Persist reviewed dispatch pricing",
                "--workspace",
                str(workspace),
                "--provider-catalog",
                str(catalog_path),
                "--project-budget-cny",
                "10",
                "--governance",
                "off",
                "--allow-unsafe-native-workers",
            )
            project = str(created["project"])
            run(
                "new-task",
                "--project",
                project,
                "--title",
                "priced",
                "--purpose",
                "Persist the chosen price snapshot",
                "--provider",
                "longcat",
                "--estimated-input-tokens",
                "1000",
            )
            run(
                "dispatch",
                "--project",
                project,
                "--task",
                "V2-0001",
                "--provider",
                "longcat",
                "--unsafe-native",
            )
            task = json.loads(
                (Path(project) / "tasks" / "V2-0001" / "task.json").read_text(encoding="utf-8")
            )
            snapshot = task["attempts"][0]["route_decision"]["price_snapshot"]
            self.assertEqual(snapshot, catalog["providers"][0]["pricing"])
            self.assertEqual(snapshot["snapshot_hash"], pricing_snapshot_hash(snapshot))
            self.assertEqual(
                task["attempts"][0]["reserved_cost_cny"],
                task["attempts"][0]["route_decision"]["estimated_cost_cny"],
            )


if __name__ == "__main__":
    unittest.main(verbosity=2)
