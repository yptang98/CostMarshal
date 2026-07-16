#!/usr/bin/env python3
"""Contract tests for canonical pricing snapshots and freshness gates."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import FrozenInstanceError
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
CLI = ROOT / "scripts" / "costmarshal.py"
sys.path.insert(0, str(ROOT))

from costmarshal_v2.actor_runner import usage_details_from_events, usage_from_events  # noqa: E402
from costmarshal_v2.routing import (  # noqa: E402
    RoutingValidationError,
    build_pricing_snapshot,
    decide_route,
    default_provider_catalog,
    estimate_cost_cny,
    estimate_cost_nano_cny,
    pricing_snapshot_hash,
    pricing_snapshot_status,
    provider_by_id,
    validate_provider_catalog,
)
from costmarshal_v2.scheduler import validate_token_triplet  # noqa: E402


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
        input_price, cached_price, output_price, fixed_attempt = prices[provider["provider_id"]]
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
            fixed_attempt=fixed_attempt,
        )
    return catalog


class PricingMetadataTest(unittest.TestCase):
    def test_zero_legacy_fixed_request_is_compatible_but_nonzero_fails_closed(self) -> None:
        legacy_zero = build_pricing_snapshot(
            currency="CNY",
            source="https://pricing.example/legacy-zero-request-fee",
            reviewed_at="2026-07-15T00:00:00Z",
            effective_at="2026-07-15T00:00:00Z",
            expires_at="2026-08-15T00:00:00Z",
            snapshot_id="legacy-zero-request-fee",
            input_per_1m="1",
            cached_input_per_1m="0",
            output_per_1m="2",
            fixed_request="0",
        )
        self.assertIn("fixed_request", legacy_zero)
        self.assertNotIn("fixed_attempt", legacy_zero)
        provider = canonical_catalog()["providers"][0]
        provider["pricing"] = legacy_zero
        normalized = validate_provider_catalog(
            {"schema_version": 1, "providers": [provider]}
        )["providers"][0]
        self.assertEqual(
            estimate_cost_nano_cny(
                normalized,
                input_tokens=1_000_000,
                cached_input_tokens=0,
                output_tokens=0,
            ),
            1_000_000_000,
        )
        with self.assertRaisesRegex(RoutingValidationError, "request-count metering"):
            build_pricing_snapshot(
                currency="CNY",
                source="https://pricing.example/unsupported-request-fee",
                reviewed_at="2026-07-15T00:00:00Z",
                effective_at="2026-07-15T00:00:00Z",
                expires_at="2026-08-15T00:00:00Z",
                snapshot_id="unsupported-request-fee",
                input_per_1m="1",
                cached_input_per_1m="0",
                output_per_1m="2",
                fixed_request="0.01",
            )

    def test_canonical_cost_uses_exact_conservative_nano_cny_math(self) -> None:
        provider = canonical_catalog()["providers"][0]
        provider["pricing"] = build_pricing_snapshot(
            currency="CNY",
            source="https://pricing.example/nano-exact",
            reviewed_at="2026-07-15T00:00:00Z",
            effective_at="2026-07-15T00:00:00Z",
            expires_at="2026-08-15T00:00:00Z",
            snapshot_id="nano-exact",
            input_per_1m=428.086698109,
            cached_input_per_1m=428.086698109,
            output_per_1m=428.086698109,
            fixed_attempt=0,
        )
        self.assertEqual(
            estimate_cost_nano_cny(
                provider,
                input_tokens=2_173_054_922,
                cached_input_tokens=0,
                output_tokens=0,
            ),
            930_255_906_368_491,
        )

    def test_canonical_price_preserves_all_nine_decimal_places(self) -> None:
        provider = canonical_catalog()["providers"][0]
        provider["pricing"] = build_pricing_snapshot(
            currency="CNY",
            source="https://pricing.example/raw-decimal",
            reviewed_at="2026-07-15T00:00:00Z",
            effective_at="2026-07-15T00:00:00Z",
            expires_at="2026-08-15T00:00:00Z",
            snapshot_id="raw-decimal",
            input_per_1m="999999999.123456789",
            cached_input_per_1m="0",
            output_per_1m="0",
            fixed_attempt="0",
        )
        self.assertEqual(provider["pricing"]["input_per_1m"], "999999999.123456789")
        self.assertEqual(
            estimate_cost_nano_cny(
                provider,
                input_tokens=1_000_000,
                cached_input_tokens=0,
                output_tokens=0,
            ),
            999_999_999_123_456_789,
        )

    def test_legacy_float_hash_cannot_collapse_an_exact_decimal_price(self) -> None:
        snapshot = build_pricing_snapshot(
            currency="CNY",
            source="https://pricing.example/no-float-collapse",
            reviewed_at="2026-07-15T00:00:00Z",
            effective_at="2026-07-15T00:00:00Z",
            expires_at="2026-08-15T00:00:00Z",
            snapshot_id="no-float-collapse",
            input_per_1m="1000000000000000063.999999999",
            cached_input_per_1m="0",
            output_per_1m="0",
            fixed_attempt="0",
        )
        legacy_payload = {
            **{key: value for key, value in snapshot.items() if key != "snapshot_hash"},
            "input_per_1m": float(snapshot["input_per_1m"]),
            "cached_input_per_1m": float(snapshot["cached_input_per_1m"]),
            "output_per_1m": float(snapshot["output_per_1m"]),
            "fixed_attempt": float(snapshot["fixed_attempt"]),
        }
        snapshot["snapshot_hash"] = "sha256:" + hashlib.sha256(
            json.dumps(
                legacy_payload,
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=False,
            ).encode("utf-8")
        ).hexdigest()
        provider = canonical_catalog()["providers"][0]
        provider["pricing"] = snapshot
        with self.assertRaisesRegex(RoutingValidationError, "does not match"):
            validate_provider_catalog(
                {"schema_version": 1, "providers": [provider]}
            )

    def test_route_objective_uses_exact_decimal_cost_for_selection(self) -> None:
        template = canonical_catalog()["providers"][0]
        cheap = deepcopy(template)
        cheap.update({"provider_id": "z-cheap", "profile": "z-cheap", "priority": 0})
        cheap["pricing"] = build_pricing_snapshot(
            currency="CNY",
            source="https://pricing.example/exact-cheap",
            reviewed_at="2026-07-15T00:00:00Z",
            effective_at="2026-07-15T00:00:00Z",
            expires_at="2026-08-15T00:00:00Z",
            snapshot_id="exact-cheap",
            input_per_1m="1000000000000000000",
            cached_input_per_1m="0",
            output_per_1m="0",
            fixed_attempt="0",
        )
        expensive = deepcopy(cheap)
        expensive.update({"provider_id": "a-expensive", "profile": "a-expensive"})
        expensive["pricing"] = build_pricing_snapshot(
            currency="CNY",
            source="https://pricing.example/exact-expensive",
            reviewed_at="2026-07-15T00:00:00Z",
            effective_at="2026-07-15T00:00:00Z",
            expires_at="2026-08-15T00:00:00Z",
            snapshot_id="exact-expensive",
            input_per_1m="1000000000000000063.999999999",
            cached_input_per_1m="0",
            output_per_1m="0",
            fixed_attempt="0",
        )
        decision = decide_route(
            {"risk": "low", "task_type": "analysis"},
            {"schema_version": 1, "providers": [expensive, cheap]},
            input_tokens=1_000_000,
            now="2026-07-16T00:00:00Z",
        )
        self.assertEqual(decision.provider_id, "z-cheap")
        self.assertEqual(decision.estimated_cost_cny_exact, "1000000000000000000")
        payload = decision.to_dict()
        self.assertEqual(payload["estimated_cost_cny_exact"], "1000000000000000000")
        self.assertNotEqual(
            expensive["pricing"]["input_per_1m"],
            payload["estimated_cost_cny_exact"],
        )

    def test_init_preserves_unquoted_catalog_decimal_lexeme(self) -> None:
        catalog = canonical_catalog()
        catalog["providers"][0]["pricing"] = build_pricing_snapshot(
            currency="CNY",
            source="https://pricing.example/catalog-decimal",
            reviewed_at="2026-07-15T00:00:00Z",
            effective_at="2026-07-15T00:00:00Z",
            expires_at="2026-08-15T00:00:00Z",
            snapshot_id="catalog-decimal",
            input_per_1m="999999999.123456789",
            cached_input_per_1m="0",
            output_per_1m="0",
            fixed_attempt="0",
        )
        encoded = json.dumps(catalog, ensure_ascii=False)
        encoded = encoded.replace('"999999999.123456789"', "999999999.123456789", 1)
        with tempfile.TemporaryDirectory(prefix="costmarshal-catalog-decimal-") as raw:
            temp = Path(raw)
            workspace = temp / "workspace"
            workspace.mkdir()
            catalog_path = temp / "catalog.json"
            catalog_path.write_text(encoded, encoding="utf-8")
            environment = os.environ.copy()
            environment["COSTMARSHAL_V2_HOME"] = str(temp / "runtime")
            completed = subprocess.run(
                [
                    sys.executable,
                    str(CLI),
                    "init",
                    "--name",
                    "catalog-decimal",
                    "--objective",
                    "preserve exact reviewed pricing",
                    "--workspace",
                    str(workspace),
                    "--provider-catalog",
                    str(catalog_path),
                    "--governance",
                    "off",
                ],
                env=environment,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)
            project = Path(json.loads(completed.stdout)["project"])
            stored = json.loads((project / "project.json").read_text(encoding="utf-8"))
            provider = provider_by_id(stored["provider_catalog"], "longcat")
            self.assertEqual(provider["pricing"]["input_per_1m"], "999999999.123456789")
            self.assertEqual(
                estimate_cost_nano_cny(
                    provider,
                    input_tokens=1_000_000,
                    cached_input_tokens=0,
                    output_tokens=0,
                ),
                999_999_999_123_456_789,
            )

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

    def test_cached_forecast_changes_the_economic_route(self) -> None:
        catalog = canonical_catalog()
        prices = {
            "longcat": (100.0, 1.0),
            "deepseek": (10.0, 10.0),
            "codex": (100.0, 100.0),
        }
        for provider in catalog["providers"]:
            input_price, cached_price = prices[provider["provider_id"]]
            provider["pricing"] = build_pricing_snapshot(
                currency="CNY",
                source=f"https://pricing.example/{provider['provider_id']}",
                reviewed_at="2026-07-15T00:00:00Z",
                effective_at="2026-07-15T00:00:00Z",
                expires_at="2026-08-15T00:00:00Z",
                snapshot_id=f"cached-route-{provider['provider_id']}",
                input_per_1m=input_price,
                cached_input_per_1m=cached_price,
                output_per_1m=0.0,
                fixed_attempt=0.0,
            )

        ordinary = decide_route(
            {"risk": "low", "task_type": "analysis"},
            catalog,
            input_tokens=1_000_000,
            output_tokens=0,
            now=NOW,
        )
        cached = decide_route(
            {"risk": "low", "task_type": "analysis"},
            catalog,
            input_tokens=0,
            cached_input_tokens=1_000_000,
            output_tokens=0,
            now=NOW,
        )
        self.assertEqual(ordinary.provider_id, "deepseek")
        self.assertEqual(cached.provider_id, "longcat")
        self.assertEqual(cached.estimated_cost_cny, 1.0)
        self.assertEqual(cached.estimated_cached_input_tokens, 1_000_000)
        self.assertIn("0/1000000/0", cached.explanation)

    def test_cached_forecast_without_compatible_price_fails_closed(self) -> None:
        legacy = default_provider_catalog()
        for provider in legacy["providers"]:
            provider["input_cny_per_1m"] = 1.0
            provider["output_cny_per_1m"] = 2.0
        decision = decide_route(
            {"risk": "low", "task_type": "analysis"},
            legacy,
            cached_input_tokens=1000,
            now=NOW,
        )
        self.assertEqual(decision.optimization_mode, "safe-tier")
        self.assertEqual(
            decision.pricing_status,
            "cached-input-unsupported:beta-legacy",
        )
        self.assertIsNone(decision.estimated_cost_cny)
        with self.assertRaisesRegex(RoutingValidationError, "pricing status"):
            decide_route(
                {
                    "risk": "low",
                    "task_type": "analysis",
                    "min_success_probability": 0.1,
                },
                legacy,
                cached_input_tokens=1000,
                now=NOW,
            )

    def test_usage_parser_separates_cached_input_dimensions(self) -> None:
        self.assertEqual(
            usage_from_events(
                [
                    {
                        "usage": {
                            "input_tokens": 1000,
                            "input_tokens_details": {"cached_tokens": 300},
                            "output_tokens": 50,
                        }
                    }
                ]
            ),
            (700, 300, 50),
        )
        self.assertEqual(
            usage_from_events(
                [
                    {
                        "usage": {
                            "input_tokens": 1000,
                            "input_tokens_details": {"cached_tokens": 0},
                            "output_tokens": 25,
                        }
                    },
                    {
                        "usage": {
                            "input_tokens": 1500,
                            "input_tokens_details": {"cached_tokens": 1000},
                            "output_tokens": 50,
                        }
                    },
                ]
            ),
            (500, 1000, 50),
        )
        self.assertEqual(
            usage_details_from_events(
                [
                    {
                        "usage": {
                            "input_tokens": 100,
                            "cache_creation_input_tokens": 1000,
                            "output_tokens": 10,
                        }
                    }
                ]
            ),
            (1100, 0, 10, False),
        )

    def test_persisted_token_dimensions_require_integers_and_consistent_total(self) -> None:
        issues: list[str] = []
        validate_token_triplet(
            {
                "input_tokens": 10,
                "cached_input_tokens": 0.5,
                "output_tokens": True,
                "total_tokens": 11,
            },
            "usage row",
            issues,
        )
        self.assertTrue(any("cached_input_tokens" in issue for issue in issues))
        self.assertTrue(any("output_tokens" in issue for issue in issues))

        issues = []
        validate_token_triplet(
            {
                "input_tokens": 10,
                "cached_input_tokens": 5,
                "output_tokens": 2,
                "total_tokens": 16,
            },
            "usage row",
            issues,
        )
        self.assertEqual(
            issues,
            [
                "usage row total_tokens must equal input_tokens + cached_input_tokens + output_tokens"
            ],
        )
        self.assertEqual(
            usage_from_events(
                [
                    {
                        "usage": {
                            "input_tokens": 700,
                            "cache_read_input_tokens": 300,
                            "output_tokens": 50,
                        }
                    }
                ]
            ),
            (700, 300, 50),
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
            profile_home = temp / "codex-home"
            env = dict(os.environ)
            env["CODEX_HOME"] = str(profile_home)

            def run(*args: str) -> dict:
                completed = subprocess.run(
                    [sys.executable, str(CLI), "--root", str(temp / "runtime"), *args],
                    cwd=temp,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    env=env,
                    check=False,
                )
                if completed.returncode != 0:
                    raise AssertionError(
                        f"command failed: {args}\nstdout:\n{completed.stdout}\nstderr:\n{completed.stderr}"
                    )
                return json.loads(completed.stdout)

            run("configure-profiles", "--codex-home", str(profile_home))
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
                "--estimated-cached-input-tokens",
                "500",
            )
            dispatched = run(
                "dispatch",
                "--project",
                project,
                "--task",
                "V2-0001",
                "--provider",
                "longcat",
                "--unsafe-native",
            )
            run(
                "new-task",
                "--project",
                project,
                "--title",
                "priced result",
                "--purpose",
                "Settle a leader result against its dispatch snapshot",
                "--provider",
                "longcat",
                "--estimated-input-tokens",
                "1000",
                "--estimated-cached-input-tokens",
                "500",
            )
            result_dispatch = run(
                "dispatch",
                "--project",
                project,
                "--task",
                "V2-0002",
                "--provider",
                "longcat",
                "--unsafe-native",
            )
            result_task = json.loads(
                (Path(project) / "tasks" / "V2-0002" / "task.json").read_text(
                    encoding="utf-8"
                )
            )
            result_attempt_id = result_task["attempts"][0]["attempt_id"]

            # Catalog updates after dispatch must not rewrite the economics of
            # either attempt. Both usage and leader result settlement use the
            # immutable snapshot embedded in their route decision.
            project_path = Path(project) / "project.json"
            project_payload = json.loads(project_path.read_text(encoding="utf-8"))
            for provider in project_payload["provider_catalog"]["providers"]:
                if provider["provider_id"] == "longcat":
                    provider["pricing"] = build_pricing_snapshot(
                        currency="CNY",
                        source="https://pricing.example/longcat-updated",
                        reviewed_at=reviewed,
                        effective_at=reviewed,
                        expires_at=expires,
                        snapshot_id="updated-after-dispatch",
                        input_per_1m=500.0,
                        cached_input_per_1m=750.0,
                        output_per_1m=900.0,
                        fixed_attempt=1.0,
                    )
            project_path.write_text(
                json.dumps(project_payload, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            first_usage = run(
                "record-usage",
                "--project",
                project,
                "--actor",
                dispatched["actor_id"],
                "--input-tokens",
                "400",
                "--cached-input-tokens",
                "200",
            )["event"]
            usage = run(
                "record-usage",
                "--project",
                project,
                "--actor",
                dispatched["actor_id"],
                "--input-tokens",
                "600",
                "--cached-input-tokens",
                "300",
                "--final",
            )["event"]
            self.assertEqual(first_usage["estimated_cost_cny"], "0.01045")
            self.assertEqual(usage["cached_input_tokens"], 300)
            self.assertEqual(usage["total_tokens"], 900)
            self.assertEqual(usage["estimated_cost_cny"], "0.000675")
            self.assertTrue(usage["cost_source"].startswith("attempt_price_snapshot:sha256:"))
            usage_rows = [
                json.loads(line)
                for line in (Path(project) / "reports" / "usage.jsonl")
                .read_text(encoding="utf-8")
                .splitlines()
                if line.strip()
            ]
            self.assertEqual(
                sum(Decimal(str(row["estimated_cost_cny"])) for row in usage_rows),
                Decimal("0.011125"),
            )
            actor_state = next(
                json.loads(path.read_text(encoding="utf-8"))
                for path in (Path(project) / "scheduler" / "actors").glob("*.json")
                if json.loads(path.read_text(encoding="utf-8")).get("id")
                == dispatched["actor_id"]
            )
            self.assertEqual(
                Decimal(str(actor_state["usage"]["estimated_cost_cny"])),
                Decimal("0.011125"),
            )
            run(
                "collect",
                "--project",
                project,
                "--task",
                "V2-0002",
                "--state",
                "waiting_leader",
            )
            result = run(
                "record-result",
                "--command-id",
                "CMD-pricing-result",
                "--project",
                project,
                "--task",
                "V2-0002",
                "--actor",
                result_dispatch["actor_id"],
                "--attempt",
                result_attempt_id,
                "--status",
                "failed",
                "--quality-score",
                "1",
                "--input-tokens",
                "1000",
                "--cached-input-tokens",
                "500",
            )["event"]
            self.assertEqual(result["estimated_cost_cny"], "0.011125")
            self.assertTrue(result["cost_source"].startswith("attempt_price_snapshot:sha256:"))
            task = json.loads(
                (Path(project) / "tasks" / "V2-0001" / "task.json").read_text(encoding="utf-8")
            )
            snapshot = task["attempts"][0]["route_decision"]["price_snapshot"]
            self.assertEqual(snapshot, catalog["providers"][0]["pricing"])
            self.assertEqual(snapshot["snapshot_hash"], pricing_snapshot_hash(snapshot))
            self.assertEqual(task["estimated_cached_input_tokens"], 500)
            self.assertEqual(
                task["attempts"][0]["route_decision"]["estimated_cached_input_tokens"],
                500,
            )
            self.assertEqual(task["attempts"][0]["reserved_cost_cny"], "0.011125")
            self.assertEqual(task["attempts"][0]["actual_cost_cny"], "0.011125")
            self.assertEqual(
                task["attempts"][0]["reserved_cost_cny"],
                task["attempts"][0]["route_decision"]["planned_steps"][0]["estimated_cost_cny"],
            )


if __name__ == "__main__":
    unittest.main(verbosity=2)
