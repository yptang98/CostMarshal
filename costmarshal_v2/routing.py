from __future__ import annotations

"""Pure, fail-closed routing helpers for CostMarshal provider tiers.

This module deliberately has no dependency on the scheduler or runtime state.  A
caller can therefore validate and explain a route before it mutates a project.
Provider identity (the API/profile being called) is separate from capability
tier (low, medium, or high).
"""

from copy import deepcopy
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import hashlib
import json
import math
import re
from decimal import Decimal, InvalidOperation, ROUND_CEILING, localcontext
from typing import Any, Iterable, Mapping, Sequence


CATALOG_SCHEMA_VERSION = 1
ROUTE_PLAN_SCHEMA_V1 = "costmarshal-route-plan-v1"
ROUTE_PLAN_SCHEMA_V2 = "costmarshal-route-plan-v2"
ROUTE_PLAN_SCHEMA_VERSION = ROUTE_PLAN_SCHEMA_V2
MAX_ENABLED_PROVIDERS_PER_TIER = 16
BOOTSTRAP_MIN_CONDITIONAL_OBSERVATIONS = 10
TIERS = ("low", "medium", "high")
TIER_RANK = {tier: index for index, tier in enumerate(TIERS)}
RISKS = {"low", "medium", "high"}
DIFFICULTIES = {"simple", "normal", "hard"}
ROUTING_OBJECTIVES = {"completion-first", "cost-only"}

# Low tier is an allowlist.  Unknown or judgment-heavy work starts at medium.
LOW_TIER_TASK_TYPES = {
    "analysis",
    "documentation",
    "extraction",
    "mechanical",
    "small-edit",
    "summarization",
    "test",
    "verification",
}
MEDIUM_TIER_TASK_TYPES = {
    "implementation",
    "review",
    "code-review",
}

_PROVIDER_ID = re.compile(r"[a-z0-9][a-z0-9._-]{0,63}")
_PROFILE_ID = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9_.-]{0,62}[A-Za-z0-9])?")
_MODEL_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/@+-]{0,255}")
_PROVIDER_FIELDS = {
    "provider_id",
    "tier",
    "profile",
    "model",
    "env_key",
    "enabled",
    "priority",
    "input_cny_per_1m",
    "output_cny_per_1m",
    "pricing",
    "capabilities",
}

_PRICING_FIELDS = {
    "currency",
    "source",
    "reviewed_at",
    "effective_at",
    "expires_at",
    "snapshot_id",
    "snapshot_hash",
    "input_per_1m",
    "cached_input_per_1m",
    "output_per_1m",
    "fixed_attempt",
    "fixed_request",
}
_SNAPSHOT_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}")
_SNAPSHOT_HASH = re.compile(r"sha256:[0-9a-f]{64}")
_PROFILE_HASH = re.compile(r"sha256:[0-9a-f]{64}")

ExecutionIdentity = tuple[str, str | None, str | None]
PredecessorExecutionIdentity = tuple[str, str, str | None, str | None]

_TOKEN_FORECAST_FIELDS = {
    "estimated_input_tokens",
    "estimated_cached_input_tokens",
    "estimated_output_tokens",
    "cache_mode",
    "cache_binding",
}
_CACHE_BINDING_FIELDS = {
    "provider_id",
    "model",
    "profile",
    "profile_sha256",
}
_CACHE_MODES = {
    "none",
    "bound-origin",
    "exact-identity-reuse",
    "reclassified-as-ordinary",
}


class RoutingValidationError(ValueError):
    """Raised when routing input is invalid or cannot satisfy safety floors."""


@dataclass(frozen=True)
class PricingSnapshot:
    currency: str
    source: str
    reviewed_at: str
    effective_at: str
    expires_at: str
    snapshot_id: str
    snapshot_hash: str
    input_per_1m: str | float
    cached_input_per_1m: str | float | None
    output_per_1m: str | float
    fixed_attempt: str | float | None = None
    fixed_request: str | float | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            key: value
            for key, value in asdict(self).items()
            if value is not None
        }


@dataclass(frozen=True)
class AcceptancePrior:
    provider_id: str
    scope: str
    observations: int
    accepted: int
    prior_alpha: float
    prior_beta: float
    posterior_mean: float
    conservative_probability: float
    evidence_result_ids: tuple[str, ...]
    evidence_sha256: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class RouteDecision:
    provider_id: str
    tier: str
    profile: str | None
    model: str | None
    tier_floor: str
    requested_provider_id: str | None
    requested_tier: str | None
    routing_objective: str
    estimated_input_tokens: int
    estimated_cached_input_tokens: int
    estimated_output_tokens: int
    estimated_cost_cny: float | None
    estimated_cost_cny_exact: str | None
    acceptance_prior: AcceptancePrior
    candidate_provider_ids: tuple[str, ...]
    planned_provider_ids: tuple[str, ...]
    planned_steps: tuple[dict[str, Any], ...]
    worst_case_chain_cost_cny: float | None
    worst_case_chain_cost_cny_exact: str | None
    plan_fingerprint: str
    expected_chain_cost_cny: float | None
    expected_chain_cost_cny_exact: str | None
    expected_success_probability: float | None
    expected_success_probability_exact: str | None
    expected_cost_per_accepted_cny: float | None
    expected_cost_per_accepted_cny_exact: str | None
    optimization_mode: str
    pricing_status: str
    pricing_currency: str | None
    price_snapshot: PricingSnapshot | None
    reason: str
    explanation: str

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["candidate_provider_ids"] = list(self.candidate_provider_ids)
        payload["planned_provider_ids"] = list(self.planned_provider_ids)
        payload["planned_steps"] = deepcopy(list(self.planned_steps))
        payload["price_snapshot"] = self.price_snapshot.to_dict() if self.price_snapshot else None
        return payload


def _provider(
    provider_id: str,
    tier: str,
    *,
    profile: str | None,
    model: str | None,
    env_key: str | None,
) -> dict[str, Any]:
    return {
        "provider_id": provider_id,
        "tier": tier,
        "profile": profile,
        "model": model,
        "env_key": env_key,
        "enabled": True,
        "priority": 100,
        # Prices are intentionally unknown by default.  Deployments must set
        # reviewed prices rather than silently relying on stale vendor pricing.
        "input_cny_per_1m": None,
        "output_cny_per_1m": None,
        "capabilities": [],
    }


def default_provider_catalog() -> dict[str, Any]:
    """Return a fresh three-tier catalog suitable for a newly created project."""

    return {
        "schema_version": CATALOG_SCHEMA_VERSION,
        "providers": [
            _provider("longcat", "low", profile="longcat", model="LongCat-2.0", env_key="LONGCAT_API_KEY"),
            _provider("deepseek", "medium", profile="deepseek", model="inherit", env_key="DEEPSEEK_API_KEY"),
            # `CODEX_API_KEY` is scoped to one non-interactive `codex exec`
            # invocation. Required OCI workers cannot and must not inherit the
            # host's persisted auth.json, so new projects declare the selected
            # high-tier credential explicitly.
            _provider("codex", "high", profile=None, model="inherit", env_key="CODEX_API_KEY"),
        ],
    }


def legacy_provider_catalog() -> dict[str, Any]:
    """Return the historical LongCat/Codex catalog used by projects without one."""

    catalog = default_provider_catalog()
    catalog["providers"] = [
        row for row in catalog["providers"] if row["provider_id"] != "deepseek"
    ]
    # Projects that predate explicit catalogs inherited the host Codex login
    # for their high tier. Preserve that read behavior; only newly initialized
    # catalogs opt into the single-run CODEX_API_KEY contract.
    for row in catalog["providers"]:
        if row["provider_id"] == "codex":
            row["env_key"] = None
    return catalog


def _require_plain_number(value: Any, label: str, *, allow_none: bool = False) -> float | None:
    if value is None and allow_none:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise RoutingValidationError(f"{label} must be a finite non-negative number")
    result = float(value)
    if not math.isfinite(result) or result < 0:
        raise RoutingValidationError(f"{label} must be a finite non-negative number")
    return result


def _require_token_count(value: Any, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise RoutingValidationError(f"{label} must be a non-negative integer")
    return value


def _parse_rfc3339(value: Any, label: str) -> tuple[datetime, str]:
    if not isinstance(value, str) or not value.strip():
        raise RoutingValidationError(f"{label} must be a non-empty RFC3339 timestamp")
    raw = value.strip()
    try:
        parsed = datetime.fromisoformat(raw[:-1] + "+00:00" if raw.endswith("Z") else raw)
    except ValueError as exc:
        raise RoutingValidationError(f"{label} must be a valid RFC3339 timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise RoutingValidationError(f"{label} must include a timezone")
    utc = parsed.astimezone(timezone.utc)
    return utc, utc.isoformat().replace("+00:00", "Z")


def _normalize_now(now: datetime | str | None) -> datetime:
    if now is None:
        return datetime.now(timezone.utc)
    if isinstance(now, str):
        return _parse_rfc3339(now, "now")[0]
    if not isinstance(now, datetime) or now.tzinfo is None or now.utcoffset() is None:
        raise RoutingValidationError("now must be a timezone-aware datetime or RFC3339 timestamp")
    return now.astimezone(timezone.utc)


def _normalize_pricing_snapshot(
    raw: Mapping[str, Any],
    label: str,
    *,
    require_hash: bool,
) -> dict[str, Any]:
    if not isinstance(raw, Mapping):
        raise RoutingValidationError(f"{label} must be an object")
    unknown = set(raw) - _PRICING_FIELDS
    if unknown:
        raise RoutingValidationError(
            f"{label} has unsupported charging dimensions or unknown fields: "
            + ", ".join(sorted(str(item) for item in unknown))
        )
    currency = raw.get("currency")
    if not isinstance(currency, str) or not re.fullmatch(r"[A-Z]{3}", currency):
        raise RoutingValidationError(f"{label}.currency must be an ISO-style three-letter uppercase code")
    source = raw.get("source")
    if not isinstance(source, str) or not source.strip():
        raise RoutingValidationError(f"{label}.source must be a non-empty reviewed provenance reference")
    snapshot_id = raw.get("snapshot_id")
    if not isinstance(snapshot_id, str) or not _SNAPSHOT_ID.fullmatch(snapshot_id):
        raise RoutingValidationError(f"{label}.snapshot_id is invalid")
    reviewed_dt, reviewed_at = _parse_rfc3339(raw.get("reviewed_at"), f"{label}.reviewed_at")
    effective_dt, effective_at = _parse_rfc3339(raw.get("effective_at"), f"{label}.effective_at")
    expires_dt, expires_at = _parse_rfc3339(raw.get("expires_at"), f"{label}.expires_at")
    if effective_dt >= expires_dt:
        raise RoutingValidationError(f"{label}.effective_at must be earlier than expires_at")
    if reviewed_dt >= expires_dt:
        raise RoutingValidationError(f"{label}.reviewed_at must be earlier than expires_at")
    input_price = _decimal_money_text(
        _money_decimal(raw.get("input_per_1m"), f"{label}.input_per_1m")
    )
    output_price = _decimal_money_text(
        _money_decimal(raw.get("output_per_1m"), f"{label}.output_per_1m")
    )
    raw_cached_price = raw.get("cached_input_per_1m")
    cached_price = (
        None
        if raw_cached_price is None
        else _decimal_money_text(
            _money_decimal(raw_cached_price, f"{label}.cached_input_per_1m")
        )
    )
    if "fixed_attempt" in raw and "fixed_request" in raw:
        raise RoutingValidationError(
            f"{label} cannot mix fixed_attempt with legacy fixed_request"
        )
    legacy_fixed_request = "fixed_request" in raw
    fixed_field = "fixed_request" if legacy_fixed_request else "fixed_attempt"
    fixed_attempt = _decimal_money_text(
        _money_decimal(raw.get(fixed_field, 0), f"{label}.{fixed_field}")
    )
    if legacy_fixed_request and Decimal(fixed_attempt) != 0:
        raise RoutingValidationError(
            f"{label}.fixed_request is a per-wire-request dimension and is unsupported "
            "without request-count metering; use fixed_attempt only for a fee charged "
            "once per CostMarshal attempt"
        )
    normalized = {
        "currency": currency,
        "source": source.strip(),
        "reviewed_at": reviewed_at,
        "effective_at": effective_at,
        "expires_at": expires_at,
        "snapshot_id": snapshot_id,
        "input_per_1m": input_price,
        "cached_input_per_1m": cached_price,
        "output_per_1m": output_price,
        fixed_field: fixed_attempt,
    }
    computed_hash = "sha256:" + hashlib.sha256(
        json.dumps(normalized, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    ).hexdigest()
    legacy_normalized = {
        **normalized,
        "input_per_1m": float(input_price),
        "cached_input_per_1m": None if cached_price is None else float(cached_price),
        "output_per_1m": float(output_price),
        fixed_field: float(fixed_attempt),
    }
    legacy_hash = "sha256:" + hashlib.sha256(
        json.dumps(legacy_normalized, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    ).hexdigest()
    legacy_float_exact = all(
        Decimal(value) == Decimal(str(float(value)))
        for value in (input_price, output_price, fixed_attempt)
    ) and (
        cached_price is None
        or Decimal(cached_price) == Decimal(str(float(cached_price)))
    )
    supplied_hash = raw.get("snapshot_hash")
    if require_hash:
        if not isinstance(supplied_hash, str) or not _SNAPSHOT_HASH.fullmatch(supplied_hash):
            raise RoutingValidationError(f"{label}.snapshot_hash must be sha256:<64 lowercase hex characters>")
        accepted_hashes = {computed_hash}
        if legacy_float_exact:
            accepted_hashes.add(legacy_hash)
        if supplied_hash not in accepted_hashes:
            raise RoutingValidationError(f"{label}.snapshot_hash does not match the canonical snapshot")
        if legacy_float_exact and supplied_hash == legacy_hash and supplied_hash != computed_hash:
            legacy_normalized["snapshot_hash"] = legacy_hash
            return legacy_normalized
    normalized["snapshot_hash"] = computed_hash
    return normalized


def pricing_snapshot_hash(snapshot: Mapping[str, Any]) -> str:
    """Return the canonical integrity hash for a pricing snapshot."""

    return str(_normalize_pricing_snapshot(snapshot, "pricing", require_hash=False)["snapshot_hash"])


def build_pricing_snapshot(**values: Any) -> dict[str, Any]:
    """Build a canonical, hash-bound pricing snapshot for reviewed config."""

    return _normalize_pricing_snapshot(values, "pricing", require_hash=False)


def _snapshot_from_provider(provider: Mapping[str, Any]) -> PricingSnapshot | None:
    raw = provider.get("pricing")
    if not isinstance(raw, Mapping):
        return None
    normalized = _normalize_pricing_snapshot(raw, "provider.pricing", require_hash=True)
    return PricingSnapshot(**normalized)


def provider_price_basis(provider: Mapping[str, Any]) -> dict[str, Any]:
    """Return the deterministic price identity used by an executable route step.

    Canonical snapshots retain their complete hash-bound payload. Beta legacy
    rates remain explicitly labelled and are included only for compatibility;
    they are not upgraded to reviewed pricing by this representation.
    """

    snapshot = _snapshot_from_provider(provider)
    if snapshot is not None:
        return {"kind": "canonical", "snapshot": snapshot.to_dict()}
    input_price = provider.get("input_cny_per_1m")
    output_price = provider.get("output_cny_per_1m")
    if input_price is not None and output_price is not None:
        try:
            normalized_input = _decimal_money_text(
                _money_decimal(input_price, "provider.input_cny_per_1m")
            )
            normalized_output = _decimal_money_text(
                _money_decimal(output_price, "provider.output_cny_per_1m")
            )
        except RoutingValidationError:
            return {"kind": "unpriced"}
        return {
            "kind": "beta-legacy",
            "currency": "CNY",
            "input_per_1m": normalized_input,
            "output_per_1m": normalized_output,
        }
    return {"kind": "unpriced"}


def _cache_binding(
    provider: Mapping[str, Any],
    execution_identity: ExecutionIdentity,
) -> dict[str, Any]:
    identity = _normalize_execution_identity(
        execution_identity,
        f"provider {provider.get('provider_id') or '?'} cache identity",
    )
    return {
        "provider_id": str(provider.get("provider_id") or ""),
        "model": identity[0],
        "profile": identity[1],
        "profile_sha256": identity[2],
    }


def derive_step_token_forecast(
    *,
    input_tokens: int,
    cached_input_tokens: int,
    output_tokens: int,
    target_provider: Mapping[str, Any],
    target_execution_identity: ExecutionIdentity,
    step_index: int,
    cache_origin: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Derive one immutable route-step forecast from the task forecast.

    Cached input is never assumed portable across provider/model/profile bytes.
    The first step binds the source cache to its exact execution identity.  A
    successor may retain the cached dimension only when that complete identity
    is byte-equivalent; otherwise the same tokens are conservatively repriced as
    ordinary input.  Current route chains prohibit provider repetition, so every
    admitted successor is reclassified in practice.
    """

    ordinary = _require_token_count(input_tokens, "input_tokens")
    cached = _require_token_count(cached_input_tokens, "cached_input_tokens")
    output = _require_token_count(output_tokens, "output_tokens")
    if type(step_index) is not int or step_index < 0:
        raise RoutingValidationError("step_index must be a non-negative integer")
    target_binding = _cache_binding(target_provider, target_execution_identity)
    if not target_binding["provider_id"]:
        raise RoutingValidationError("route step cache identity lacks provider_id")
    if cached == 0:
        return {
            "estimated_input_tokens": ordinary,
            "estimated_cached_input_tokens": 0,
            "estimated_output_tokens": output,
            "cache_mode": "none",
            "cache_binding": None,
        }
    if step_index == 0:
        if cache_origin is None:
            return {
                "estimated_input_tokens": ordinary + cached,
                "estimated_cached_input_tokens": 0,
                "estimated_output_tokens": output,
                "cache_mode": "reclassified-as-ordinary",
                "cache_binding": None,
            }
        if dict(cache_origin) != target_binding:
            raise RoutingValidationError("first route step cache origin is inconsistent")
        return {
            "estimated_input_tokens": ordinary,
            "estimated_cached_input_tokens": cached,
            "estimated_output_tokens": output,
            "cache_mode": "bound-origin",
            "cache_binding": target_binding,
        }
    if cache_origin is None:
        return {
            "estimated_input_tokens": ordinary + cached,
            "estimated_cached_input_tokens": 0,
            "estimated_output_tokens": output,
            "cache_mode": "reclassified-as-ordinary",
            "cache_binding": None,
        }
    if dict(cache_origin) == target_binding:
        return {
            "estimated_input_tokens": ordinary,
            "estimated_cached_input_tokens": cached,
            "estimated_output_tokens": output,
            "cache_mode": "exact-identity-reuse",
            "cache_binding": target_binding,
        }
    return {
        "estimated_input_tokens": ordinary + cached,
        "estimated_cached_input_tokens": 0,
        "estimated_output_tokens": output,
        "cache_mode": "reclassified-as-ordinary",
        "cache_binding": None,
    }


def _validated_step_token_forecast(
    value: Any,
    *,
    label: str,
) -> dict[str, Any]:
    if not isinstance(value, Mapping) or set(value) != _TOKEN_FORECAST_FIELDS:
        raise RoutingValidationError(f"{label} has unknown or missing fields")
    forecast = deepcopy(dict(value))
    for field in (
        "estimated_input_tokens",
        "estimated_cached_input_tokens",
        "estimated_output_tokens",
    ):
        _require_token_count(forecast.get(field), f"{label}.{field}")
    if forecast.get("cache_mode") not in _CACHE_MODES:
        raise RoutingValidationError(f"{label}.cache_mode is invalid")
    binding = forecast.get("cache_binding")
    if binding is not None:
        if not isinstance(binding, Mapping) or set(binding) != _CACHE_BINDING_FIELDS:
            raise RoutingValidationError(f"{label}.cache_binding is invalid")
        if not isinstance(binding.get("provider_id"), str) or not binding["provider_id"]:
            raise RoutingValidationError(f"{label}.cache_binding.provider_id is invalid")
        if not isinstance(binding.get("model"), str) or not binding["model"]:
            raise RoutingValidationError(f"{label}.cache_binding.model is invalid")
        if binding.get("profile") is not None and (
            not isinstance(binding.get("profile"), str) or not binding["profile"]
        ):
            raise RoutingValidationError(f"{label}.cache_binding.profile is invalid")
        profile_sha256 = binding.get("profile_sha256")
        if profile_sha256 is not None and (
            not isinstance(profile_sha256, str) or not _PROFILE_HASH.fullmatch(profile_sha256)
        ):
            raise RoutingValidationError(
                f"{label}.cache_binding.profile_sha256 is invalid"
            )
        forecast["cache_binding"] = deepcopy(dict(binding))
    return forecast


def _validate_plan_token_forecasts(
    planned_steps: Sequence[Mapping[str, Any]],
    *,
    input_tokens: int,
    cached_input_tokens: int,
    output_tokens: int,
) -> None:
    cache_origin: dict[str, Any] | None = None
    for index, step in enumerate(planned_steps):
        if not isinstance(step, Mapping):
            raise RoutingValidationError(f"planned_steps[{index}] must be an object")
        identity = step.get("execution_identity")
        if not isinstance(identity, Mapping) or set(identity) != {
            "model",
            "profile",
            "profile_sha256",
        }:
            raise RoutingValidationError(
                f"planned_steps[{index}].execution_identity is invalid"
            )
        target_identity = _normalize_execution_identity(
            (
                identity.get("model"),
                identity.get("profile"),
                identity.get("profile_sha256"),
            ),
            f"planned_steps[{index}].execution_identity",
        )
        expected = derive_step_token_forecast(
            input_tokens=input_tokens,
            cached_input_tokens=cached_input_tokens,
            output_tokens=output_tokens,
            target_provider=step,
            target_execution_identity=target_identity,
            step_index=index,
            cache_origin=cache_origin,
        )
        observed = _validated_step_token_forecast(
            step.get("token_forecast"),
            label=f"planned_steps[{index}].token_forecast",
        )
        if observed != expected:
            raise RoutingValidationError(
                f"planned_steps[{index}].token_forecast is inconsistent with its cache identity"
            )
        if index == 0:
            cache_origin = expected.get("cache_binding")


def route_plan_fingerprint(
    planned_steps: Sequence[Mapping[str, Any]],
    *,
    input_tokens: int,
    cached_input_tokens: int,
    output_tokens: int,
    routing_objective: str = "cost-only",
) -> str:
    """Hash a legacy or per-step route plan without rewriting old envelopes.

    Plans whose steps all omit ``token_forecast`` retain the exact v1 payload.
    New plans require the field on every step and use v2.  Mixed schemas fail
    closed instead of silently downgrading the fingerprint.
    """

    _require_token_count(input_tokens, "input_tokens")
    _require_token_count(cached_input_tokens, "cached_input_tokens")
    _require_token_count(output_tokens, "output_tokens")
    steps = deepcopy(list(planned_steps))
    forecast_presence = [
        isinstance(step, Mapping) and "token_forecast" in step
        for step in steps
    ]
    if any(forecast_presence) and not all(forecast_presence):
        raise RoutingValidationError(
            "route plan cannot mix v1 and v2 token-forecast steps"
        )
    schema_version = ROUTE_PLAN_SCHEMA_V2 if forecast_presence and all(forecast_presence) else ROUTE_PLAN_SCHEMA_V1
    if schema_version == ROUTE_PLAN_SCHEMA_V2:
        objective = _validate_routing_objective(routing_objective)
        _validate_plan_token_forecasts(
            steps,
            input_tokens=input_tokens,
            cached_input_tokens=cached_input_tokens,
            output_tokens=output_tokens,
        )
    payload = {
        "schema_version": schema_version,
        "estimated_input_tokens": input_tokens,
        "estimated_cached_input_tokens": cached_input_tokens,
        "estimated_output_tokens": output_tokens,
        "planned_steps": steps,
    }
    if schema_version == ROUTE_PLAN_SCHEMA_V2:
        payload["routing_objective"] = objective
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def _route_plan_step(
    provider: Mapping[str, Any],
    prior: AcceptancePrior,
    cost: Decimal | None,
    *,
    index: int,
    token_forecast: Mapping[str, Any],
    execution_identity: ExecutionIdentity | None = None,
) -> dict[str, Any]:
    identity = execution_identity or (
        str(provider.get("model") or "inherit"),
        provider.get("profile") if isinstance(provider.get("profile"), str) else None,
        None,
    )
    identity = _normalize_execution_identity(identity, "route step execution identity")
    return {
        "index": index,
        "provider_id": str(provider["provider_id"]),
        "tier": str(provider["tier"]),
        "profile": provider.get("profile"),
        "model": provider.get("model"),
        "execution_identity": {
            "model": identity[0],
            "profile": identity[1],
            "profile_sha256": identity[2],
        },
        "estimated_cost_cny": None if cost is None else _decimal_money_text(cost),
        "token_forecast": _validated_step_token_forecast(
            token_forecast,
            label=f"route step {index} token_forecast",
        ),
        "acceptance_prior": prior.to_dict(),
        "price_basis": provider_price_basis(provider),
    }


def pricing_snapshot_status(
    provider: Mapping[str, Any], *, now: datetime | str | None = None
) -> str:
    """Return the deterministic pricing freshness state for one provider."""

    snapshot = _snapshot_from_provider(provider)
    if snapshot is None:
        legacy_input = provider.get("input_cny_per_1m")
        legacy_output = provider.get("output_cny_per_1m")
        return "beta-legacy" if legacy_input is not None and legacy_output is not None else "missing"
    clock = _normalize_now(now)
    reviewed = _parse_rfc3339(snapshot.reviewed_at, "pricing.reviewed_at")[0]
    effective = _parse_rfc3339(snapshot.effective_at, "pricing.effective_at")[0]
    expires = _parse_rfc3339(snapshot.expires_at, "pricing.expires_at")[0]
    if reviewed > clock:
        return "future-reviewed"
    if effective > clock:
        return "future-effective"
    if expires <= clock:
        return "expired"
    if snapshot.currency != "CNY":
        return "unsupported-currency"
    return "current"


def validate_provider_catalog(catalog: Mapping[str, Any]) -> dict[str, Any]:
    """Validate and normalize a catalog without mutating the caller's object.

    Validation is intentionally fail-closed: misspelled fields, duplicate IDs,
    unsupported tiers, and invalid prices are errors instead of auto routing.
    Missing tiers are allowed so legacy low/high projects remain valid.
    """

    if not isinstance(catalog, Mapping):
        raise RoutingValidationError("provider_catalog must be an object")
    unknown_catalog_fields = set(catalog) - {"schema_version", "providers"}
    if unknown_catalog_fields:
        raise RoutingValidationError(
            "provider_catalog has unknown fields: "
            + ", ".join(sorted(str(item) for item in unknown_catalog_fields))
        )
    if catalog.get("schema_version") != CATALOG_SCHEMA_VERSION:
        raise RoutingValidationError(
            f"provider_catalog.schema_version must be {CATALOG_SCHEMA_VERSION}"
        )
    raw_providers = catalog.get("providers")
    if not isinstance(raw_providers, list) or not raw_providers:
        raise RoutingValidationError("provider_catalog.providers must be a non-empty list")

    normalized: list[dict[str, Any]] = []
    seen: set[str] = set()
    for index, raw in enumerate(raw_providers):
        label = f"provider_catalog.providers[{index}]"
        if not isinstance(raw, Mapping):
            raise RoutingValidationError(f"{label} must be an object")
        unknown = set(raw) - _PROVIDER_FIELDS
        if unknown:
            raise RoutingValidationError(
                f"{label} has unknown fields: "
                + ", ".join(sorted(str(item) for item in unknown))
            )
        provider_id = raw.get("provider_id")
        if not isinstance(provider_id, str) or not _PROVIDER_ID.fullmatch(provider_id):
            raise RoutingValidationError(
                f"{label}.provider_id must match {_PROVIDER_ID.pattern!r}"
            )
        if provider_id in seen:
            raise RoutingValidationError(f"duplicate provider_id: {provider_id}")
        seen.add(provider_id)
        tier = raw.get("tier")
        if tier not in TIER_RANK:
            raise RoutingValidationError(f"{label}.tier must be one of: {', '.join(TIERS)}")
        profile = raw.get("profile")
        model = raw.get("model")
        env_key = raw.get("env_key")
        if profile is not None and (
            not isinstance(profile, str) or not _PROFILE_ID.fullmatch(profile)
        ):
            raise RoutingValidationError(f"{label}.profile must be null or a safe identifier")
        if model is not None and (
            not isinstance(model, str) or not _MODEL_ID.fullmatch(model)
        ):
            raise RoutingValidationError(
                f"{label}.model must be null or match {_MODEL_ID.pattern!r}"
            )
        if env_key is not None and (
            not isinstance(env_key, str) or not re.fullmatch(r"[A-Z_][A-Z0-9_]{0,127}", env_key)
        ):
            raise RoutingValidationError(f"{label}.env_key must be null or an uppercase environment variable name")
        enabled = raw.get("enabled", True)
        if not isinstance(enabled, bool):
            raise RoutingValidationError(f"{label}.enabled must be boolean")
        priority = raw.get("priority", 100)
        if isinstance(priority, bool) or not isinstance(priority, int) or priority < 0:
            raise RoutingValidationError(f"{label}.priority must be a non-negative integer")
        raw_pricing = raw.get("pricing")
        has_legacy_price = raw.get("input_cny_per_1m") is not None or raw.get("output_cny_per_1m") is not None
        if raw_pricing is not None and has_legacy_price:
            raise RoutingValidationError(
                f"{label} must not mix canonical pricing snapshots with beta legacy flat prices"
            )
        pricing = (
            _normalize_pricing_snapshot(raw_pricing, f"{label}.pricing", require_hash=True)
            if raw_pricing is not None
            else None
        )
        input_price = None
        output_price = None
        if pricing is None:
            input_price = (
                None
                if raw.get("input_cny_per_1m") is None
                else _decimal_money_text(
                    _money_decimal(raw.get("input_cny_per_1m"), f"{label}.input_cny_per_1m")
                )
            )
            output_price = (
                None
                if raw.get("output_cny_per_1m") is None
                else _decimal_money_text(
                    _money_decimal(raw.get("output_cny_per_1m"), f"{label}.output_cny_per_1m")
                )
            )
        capabilities = raw.get("capabilities", [])
        if not isinstance(capabilities, list) or any(
            not isinstance(item, str) or not item.strip() for item in capabilities
        ):
            raise RoutingValidationError(f"{label}.capabilities must be a list of non-empty strings")
        if len(capabilities) != len(set(capabilities)):
            raise RoutingValidationError(f"{label}.capabilities must not contain duplicates")
        normalized_provider = {
                "provider_id": provider_id,
                "tier": tier,
                "profile": profile,
                "model": model,
                "env_key": env_key,
                "enabled": enabled,
                "priority": priority,
                "input_cny_per_1m": input_price,
                "output_cny_per_1m": output_price,
                "capabilities": list(capabilities),
            }
        if pricing is not None:
            normalized_provider.pop("input_cny_per_1m")
            normalized_provider.pop("output_cny_per_1m")
            normalized_provider["pricing"] = pricing
        normalized.append(normalized_provider)
    return {"schema_version": CATALOG_SCHEMA_VERSION, "providers": normalized}


def project_provider_catalog(project: Mapping[str, Any]) -> dict[str, Any]:
    """Load a project's catalog, falling back only when the field is absent.

    An explicitly present null or malformed catalog is rejected.  This keeps a
    typo in a new project from silently downgrading it to legacy routing.
    """

    if not isinstance(project, Mapping):
        raise RoutingValidationError("project must be an object")
    if "provider_catalog" not in project:
        return validate_provider_catalog(legacy_provider_catalog())
    return validate_provider_catalog(project["provider_catalog"])


def _normalized_task_value(task: Mapping[str, Any], key: str, default: str) -> str:
    value = task.get(key, default)
    if not isinstance(value, str) or not value.strip():
        raise RoutingValidationError(f"task.{key} must be a non-empty string")
    return value.strip().lower()


def _validate_routing_objective(value: Any) -> str:
    if not isinstance(value, str) or value.strip().lower() not in ROUTING_OBJECTIVES:
        raise RoutingValidationError(
            "routing_objective must be one of: completion-first, cost-only"
        )
    return value.strip().lower()


def validate_task_routing(task: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(task, Mapping):
        raise RoutingValidationError("task must be an object")
    risk = _normalized_task_value(task, "risk", "low")
    difficulty = _normalized_task_value(task, "difficulty", "normal")
    task_type = _normalized_task_value(task, "task_type", "analysis")
    if risk not in RISKS:
        raise RoutingValidationError(f"task.risk must be one of: {', '.join(sorted(RISKS))}")
    if difficulty not in DIFFICULTIES:
        raise RoutingValidationError(
            f"task.difficulty must be one of: {', '.join(sorted(DIFFICULTIES))}"
        )
    required_capabilities = task.get("required_capabilities", [])
    if not isinstance(required_capabilities, list) or any(
        not isinstance(item, str) or not item.strip() for item in required_capabilities
    ):
        raise RoutingValidationError("task.required_capabilities must be a list of non-empty strings")
    normalized_capabilities = list(dict.fromkeys(item.strip() for item in required_capabilities))
    minimum_success = _require_plain_number(
        task.get("min_success_probability"),
        "task.min_success_probability",
        allow_none=True,
    )
    if minimum_success is not None and minimum_success > 1:
        raise RoutingValidationError("task.min_success_probability must be between 0 and 1")
    routing_objective = _validate_routing_objective(
        task.get("routing_objective", "cost-only")
    )
    return {
        "risk": risk,
        "difficulty": difficulty,
        "task_type": task_type,
        "required_capabilities": normalized_capabilities,
        "min_success_probability": minimum_success,
        "routing_objective": routing_objective,
    }


def auto_tier_floor(task: Mapping[str, Any]) -> str:
    """Return the minimum safe tier for auto routing."""

    values = validate_task_routing(task)
    if values["risk"] == "high" or values["difficulty"] == "hard":
        return "high"
    if values["risk"] == "medium" or values["task_type"] in MEDIUM_TIER_TASK_TYPES:
        return "medium"
    if values["risk"] == "low" and values["task_type"] in LOW_TIER_TASK_TYPES:
        return "low"
    return "medium"


def provider_by_id(catalog: Mapping[str, Any], provider_id: str) -> dict[str, Any]:
    normalized = validate_provider_catalog(catalog)
    for provider in normalized["providers"]:
        if provider["provider_id"] == provider_id:
            return provider
    raise RoutingValidationError(f"unknown provider_id: {provider_id}")


_NANO_CNY = 1_000_000_000
_NANO_QUANTUM = Decimal("0.000000001")


def _money_decimal(value: Any, label: str) -> Decimal:
    if isinstance(value, bool):
        raise RoutingValidationError(f"{label} must be a finite non-negative number")
    try:
        result = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise RoutingValidationError(f"{label} must be a finite non-negative number") from exc
    if not result.is_finite() or result < 0:
        raise RoutingValidationError(f"{label} must be a finite non-negative number")
    try:
        with localcontext() as context:
            context.prec = max(
                50,
                len(result.as_tuple().digits) + abs(result.as_tuple().exponent) + 12,
            )
            normalized = result.quantize(_NANO_QUANTUM)
    except InvalidOperation as exc:
        raise RoutingValidationError(f"{label} is outside the supported nano-CNY range") from exc
    if normalized != result:
        raise RoutingValidationError(f"{label} must have at most 9 decimal places")
    return normalized


def _decimal_money_text(value: Decimal) -> str:
    with localcontext() as context:
        context.prec = max(
            50,
            len(value.as_tuple().digits) + abs(value.as_tuple().exponent) + 12,
        )
        normalized = value.quantize(_NANO_QUANTUM)
    rendered = format(normalized, "f").rstrip("0").rstrip(".")
    return rendered or "0"


def _decimal_exact_text(value: Decimal) -> str:
    rendered = format(value, "f").rstrip("0").rstrip(".")
    return rendered or "0"


def _rounded_decimal_float(value: Decimal, places: int = 9) -> float:
    """Render a Decimal for the compatibility float API without narrowing selection math."""

    quantum = Decimal(1).scaleb(-places)
    with localcontext() as context:
        context.prec = max(50, len(value.as_tuple().digits) + places + 8)
        return float(value.quantize(quantum))


def estimate_cost_nano_cny(
    provider: Mapping[str, Any],
    *,
    input_tokens: int,
    output_tokens: int,
    cached_input_tokens: int = 0,
) -> int | None:
    """Return the conservative exact integer nano-CNY provider estimate.

    Canonical snapshots support ordinary input, cached input, output, and a
    fixed per-CostMarshal-attempt fee. Per-wire-request charges are intentionally
    unsupported because one provider attempt may issue multiple API requests.
    Unknown required dimensions yield ``None`` rather
    than a misleading partial estimate. Fractional nano-CNY usage rounds up so
    a route reservation can never understate the reviewed quote.
    """

    input_count = _require_token_count(input_tokens, "input_tokens")
    output_count = _require_token_count(output_tokens, "output_tokens")
    cached_count = _require_token_count(cached_input_tokens, "cached_input_tokens")
    raw_snapshot = provider.get("pricing")
    if raw_snapshot is not None:
        snapshot = _normalize_pricing_snapshot(
            raw_snapshot,
            "provider.pricing",
            require_hash=True,
        )
        if snapshot["currency"] != "CNY":
            return None
        cached_price = snapshot["cached_input_per_1m"]
        if cached_count and cached_price is None:
            return None
        variable_numerator = (
            input_count * int(_money_decimal(snapshot["input_per_1m"], "pricing.input_per_1m") * _NANO_CNY)
            + cached_count * int(_money_decimal(cached_price or 0, "pricing.cached_input_per_1m") * _NANO_CNY)
            + output_count * int(_money_decimal(snapshot["output_per_1m"], "pricing.output_per_1m") * _NANO_CNY)
        )
        variable_units = (variable_numerator + 1_000_000 - 1) // 1_000_000
        fixed_field = (
            "fixed_attempt" if "fixed_attempt" in snapshot else "fixed_request"
        )
        fixed_units = int(
            _money_decimal(snapshot[fixed_field], f"pricing.{fixed_field}")
            * _NANO_CNY
        )
        return fixed_units + variable_units
    raw_input_price = provider.get("input_cny_per_1m")
    raw_output_price = provider.get("output_cny_per_1m")
    if raw_input_price is None or raw_output_price is None:
        return None
    input_price = _money_decimal(raw_input_price, "provider.input_cny_per_1m")
    output_price = _money_decimal(raw_output_price, "provider.output_cny_per_1m")
    if cached_count:
        raise RoutingValidationError(
            "beta legacy pricing does not support cached_input_tokens; use a canonical pricing snapshot"
        )
    variable_numerator = (
        input_count * int(input_price * _NANO_CNY)
        + output_count * int(output_price * _NANO_CNY)
    )
    return (variable_numerator + 1_000_000 - 1) // 1_000_000


def estimate_cost_cny(
    provider: Mapping[str, Any],
    *,
    input_tokens: int,
    output_tokens: int,
    cached_input_tokens: int = 0,
) -> float | None:
    """Display form of :func:`estimate_cost_nano_cny`; ledgers use its integer result."""

    units = estimate_cost_nano_cny(
        provider,
        input_tokens=input_tokens,
        cached_input_tokens=cached_input_tokens,
        output_tokens=output_tokens,
    )
    return None if units is None else units / _NANO_CNY


def _economic_pricing_gate(
    providers: Sequence[Mapping[str, Any]],
    *,
    now: datetime | str | None,
    require_cached_input: bool = False,
) -> tuple[bool, str, str | None]:
    """Return whether providers share usable, current CNY pricing.

    All-flat catalogs retain the explicitly labelled beta compatibility path.
    Mixing legacy prices with canonical snapshots never enables optimization.
    """

    if not providers:
        return False, "missing", None
    snapshot_modes = [provider.get("pricing") is not None for provider in providers]
    if not any(snapshot_modes):
        statuses = [pricing_snapshot_status(provider, now=now) for provider in providers]
        if all(status == "beta-legacy" for status in statuses):
            if require_cached_input:
                return False, "cached-input-unsupported:beta-legacy", "CNY"
            return True, "beta-legacy", "CNY"
        return False, "missing", None
    if not all(snapshot_modes):
        return False, "mixed-canonical-and-beta-legacy", None
    snapshots = [_snapshot_from_provider(provider) for provider in providers]
    assert all(snapshot is not None for snapshot in snapshots)
    currencies = {snapshot.currency for snapshot in snapshots if snapshot is not None}
    if len(currencies) != 1:
        return False, "mixed-currency", None
    currency = next(iter(currencies))
    if currency != "CNY":
        return False, f"unsupported-currency:{currency}", currency
    statuses = [pricing_snapshot_status(provider, now=now) for provider in providers]
    if not all(status == "current" for status in statuses):
        return False, "+".join(sorted(set(statuses))), currency
    if require_cached_input and any(
        snapshot is not None and snapshot.cached_input_per_1m is None
        for snapshot in snapshots
    ):
        return False, "cached-input-price-missing", currency
    return True, "current", currency


def _row_provider_id(row: Mapping[str, Any]) -> str | None:
    value = row.get("provider_id", row.get("provider"))
    return value if isinstance(value, str) else None


def _normalize_execution_identity(value: Sequence[Any], label: str) -> ExecutionIdentity:
    if isinstance(value, (str, bytes)) or len(value) not in {2, 3}:
        raise RoutingValidationError(
            f"{label} must be a (model, profile, profile_sha256) tuple"
        )
    model, profile = value[:2]
    profile_sha256 = value[2] if len(value) == 3 else None
    if not isinstance(model, str) or not model:
        raise RoutingValidationError(f"{label} model must be a non-empty string")
    if profile is not None and not isinstance(profile, str):
        raise RoutingValidationError(f"{label} profile must be null or a string")
    if profile_sha256 is not None and (
        not isinstance(profile_sha256, str) or not _PROFILE_HASH.fullmatch(profile_sha256)
    ):
        raise RoutingValidationError(
            f"{label} profile_sha256 must be null or a canonical sha256 digest"
        )
    return model, profile, profile_sha256


def _row_execution_identity(row: Mapping[str, Any]) -> ExecutionIdentity | None:
    if "model" not in row or "profile" not in row:
        return None
    model = row.get("execution_model") or row.get("model") or "inherit"
    profile = row.get("profile") or None
    profile_sha256 = row.get("profile_sha256") if "profile_sha256" in row else None
    try:
        return _normalize_execution_identity(
            (model, profile, profile_sha256),
            "result execution identity",
        )
    except RoutingValidationError:
        return None


def _deduplicated_leader_history(
    history: Iterable[Mapping[str, Any]],
) -> list[Mapping[str, Any]]:
    """Validate and globally deduplicate explicit leader evidence.

    The global pass is intentionally performed before provider or conditional
    filtering.  One attempt therefore cannot contribute to two incompatible
    providers, routes, profiles, or predecessor lineages.
    """

    rows: list[Mapping[str, Any]] = []
    seen_attempts: dict[str, str] = {}
    seen_results: dict[str, str] = {}
    seen_commands: dict[str, str] = {}
    for row in history:
        if not isinstance(row, Mapping) or type(row.get("accepted_by_leader")) is not bool:
            continue
        try:
            canonical = json.dumps(
                dict(row),
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            )
        except (TypeError, ValueError) as exc:
            raise RoutingValidationError("leader result evidence is not canonical JSON") from exc
        duplicate = False
        for field, seen in (
            ("attempt_id", seen_attempts),
            ("id", seen_results),
            ("command_id", seen_commands),
        ):
            identity = row.get(field)
            if not isinstance(identity, str) or not identity:
                continue
            previous = seen.get(identity)
            if previous is not None:
                if previous != canonical:
                    raise RoutingValidationError(
                        f"conflicting leader result evidence for {field} {identity}"
                    )
                duplicate = True
            else:
                seen[identity] = canonical
        if not duplicate:
            rows.append(row)
    return rows


def _leader_rows(
    history: Iterable[Mapping[str, Any]],
    provider_id: str,
    *,
    execution_identity: ExecutionIdentity | None = None,
    _history_is_deduplicated: bool = False,
    require_unconditional: bool = True,
) -> list[Mapping[str, Any]]:
    rows: list[Mapping[str, Any]] = []
    source = list(history) if _history_is_deduplicated else _deduplicated_leader_history(history)
    for row in source:
        if not isinstance(row, Mapping) or _row_provider_id(row) != provider_id:
            continue
        # Only explicit leader decisions are evidence.  Worker completion and
        # truthy strings must not train the routing prior.
        if type(row.get("accepted_by_leader")) is not bool:
            continue
        if execution_identity is not None:
            if _row_execution_identity(row) != execution_identity:
                continue
        if require_unconditional:
            predecessors = row.get("route_predecessors")
            step_index = row.get("route_plan_step_index")
            if predecessors is not None and predecessors != () and predecessors != []:
                continue
            if step_index not in {None, 0}:
                continue
        rows.append(row)
    return rows


def _wilson_lower(successes: float, trials: float, z: float = 1.96) -> float:
    if trials <= 0:
        return 0.0
    proportion = successes / trials
    denominator = 1.0 + z * z / trials
    center = proportion + z * z / (2.0 * trials)
    margin = z * math.sqrt(
        (proportion * (1.0 - proportion) + z * z / (4.0 * trials)) / trials
    )
    return max(0.0, (center - margin) / denominator)


def acceptance_evidence_provenance(
    rows: Iterable[Mapping[str, Any]],
) -> tuple[tuple[str, ...], str]:
    """Bind the exact trusted result rows used by an acceptance prior."""

    canonical_rows: list[tuple[str, bytes, dict[str, Any]]] = []
    for row in rows:
        if not isinstance(row, Mapping):
            raise RoutingValidationError("acceptance evidence row must be an object")
        material = dict(row)
        try:
            encoded = json.dumps(
                material,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            ).encode("utf-8")
        except (TypeError, ValueError) as exc:
            raise RoutingValidationError(
                "acceptance evidence row is not canonical JSON"
            ) from exc
        result_id = material.get("id")
        canonical_rows.append(
            (result_id if isinstance(result_id, str) else "", encoded, material)
        )
    canonical_rows.sort(key=lambda item: (item[0], item[1]))
    result_ids = tuple(item[0] for item in canonical_rows if item[0])
    if len(result_ids) != len(set(result_ids)):
        raise RoutingValidationError("acceptance evidence contains duplicate result ids")
    payload = b"[" + b",".join(item[1] for item in canonical_rows) + b"]"
    return result_ids, "sha256:" + hashlib.sha256(payload).hexdigest()


def leader_acceptance_prior(
    history: Iterable[Mapping[str, Any]] | None,
    provider_id: str,
    *,
    task_type: str | None = None,
    difficulty: str | None = None,
    execution_identity: ExecutionIdentity | None = None,
    prior_alpha: float = 1.0,
    prior_beta: float = 2.0,
    _history_is_deduplicated: bool = False,
    require_exact_scope: bool = False,
    minimum_observations: int = 0,
    _require_unconditional: bool = True,
) -> AcceptancePrior:
    """Build a conservative Beta-style prior from leader acceptance records.

    Evidence backs off from provider+task_type+difficulty, to provider+task_type,
    then provider-wide history.  Beta(1, 2) is the default cold-start prior.
    ``conservative_probability`` is a Wilson lower bound including pseudocounts.
    """

    alpha = _require_plain_number(prior_alpha, "prior_alpha")
    beta = _require_plain_number(prior_beta, "prior_beta")
    assert alpha is not None and beta is not None
    if alpha <= 0 or beta <= 0:
        raise RoutingValidationError("prior_alpha and prior_beta must be positive")
    if type(minimum_observations) is not int or minimum_observations < 0:
        raise RoutingValidationError("minimum_observations must be a non-negative integer")
    if not isinstance(provider_id, str) or not _PROVIDER_ID.fullmatch(provider_id):
        raise RoutingValidationError("provider_id is invalid")
    if task_type is not None and (not isinstance(task_type, str) or not task_type.strip()):
        raise RoutingValidationError("task_type must be null or a non-empty string")
    if difficulty is not None and difficulty not in DIFFICULTIES:
        raise RoutingValidationError(
            f"difficulty must be one of: {', '.join(sorted(DIFFICULTIES))}"
        )
    if execution_identity is not None:
        execution_identity = _normalize_execution_identity(
            execution_identity,
            "execution_identity",
        )

    provider_rows = _leader_rows(
        history or [],
        provider_id,
        execution_identity=execution_identity,
        _history_is_deduplicated=_history_is_deduplicated,
        require_unconditional=_require_unconditional,
    )
    scope = "provider+execution" if execution_identity is not None else "provider"
    selected = provider_rows
    normalized_task_type = task_type.strip().lower() if task_type else None
    if normalized_task_type is not None:
        type_rows = [
            row
            for row in provider_rows
            if isinstance(row.get("task_type"), str)
            and str(row["task_type"]).strip().lower() == normalized_task_type
        ]
        if type_rows:
            selected = type_rows
            scope = (
                "provider+execution+task_type"
                if execution_identity is not None
                else "provider+task_type"
            )
            if difficulty is not None:
                exact_rows = [
                    row
                    for row in type_rows
                    if isinstance(row.get("difficulty"), str)
                    and str(row["difficulty"]).strip().lower() == difficulty
                ]
                if exact_rows:
                    selected = exact_rows
                    scope = (
                        "provider+execution+task_type+difficulty"
                        if execution_identity is not None
                        else "provider+task_type+difficulty"
                    )
                elif require_exact_scope:
                    selected = []
                    scope = (
                        "provider+execution+task_type+difficulty"
                        if execution_identity is not None
                        else "provider+task_type+difficulty"
                    )
        elif require_exact_scope:
            selected = []
            scope = (
                "provider+execution+task_type+difficulty"
                if execution_identity is not None and difficulty is not None
                else "provider+execution+task_type"
                if execution_identity is not None
                else "provider+task_type+difficulty"
                if difficulty is not None
                else "provider+task_type"
            )

    accepted = sum(1 for row in selected if row["accepted_by_leader"] is True)
    observations = len(selected)
    evidence_result_ids, evidence_sha256 = acceptance_evidence_provenance(selected)
    posterior_alpha = alpha + accepted
    posterior_beta = beta + observations - accepted
    trials = posterior_alpha + posterior_beta
    posterior_mean = posterior_alpha / trials
    conservative = _wilson_lower(posterior_alpha, trials)
    return AcceptancePrior(
        provider_id=provider_id,
        scope=scope,
        observations=observations,
        accepted=accepted,
        prior_alpha=alpha,
        prior_beta=beta,
        posterior_mean=round(posterior_mean, 6),
        conservative_probability=(
            round(min(posterior_mean, conservative), 6)
            if observations >= minimum_observations
            else 0.0
        ),
        evidence_result_ids=evidence_result_ids,
        evidence_sha256=evidence_sha256,
    )


def conditional_leader_acceptance_prior(
    history: Iterable[Mapping[str, Any]] | None,
    provider_id: str,
    *,
    predecessor_execution_identities: Sequence[PredecessorExecutionIdentity],
    task_type: str | None = None,
    difficulty: str | None = None,
    execution_identity: ExecutionIdentity | None = None,
    _position_index: Mapping[
        tuple[str, str, str, int], Sequence[Mapping[str, Any]]
    ] | None = None,
    _history_is_deduplicated: bool = False,
    require_exact_scope: bool = False,
    minimum_observations: int = 0,
    _evidence_index: Mapping[
        tuple[str, ExecutionIdentity, tuple[PredecessorExecutionIdentity, ...]],
        Sequence[Mapping[str, Any]],
    ]
    | None = None,
) -> AcceptancePrior:
    """Estimate acceptance after an exact, observed predecessor failure chain.

    Marginal provider outcomes are not independent. A continuation receives
    evidence only from result rows that identify the same ordered predecessor
    provider/model/profile sequence. With no paired evidence its conservative
    probability is zero, so an unobserved continuation cannot inflate an SLA.
    """

    expected_prefix_rows: list[PredecessorExecutionIdentity] = []
    for index, raw_identity in enumerate(predecessor_execution_identities):
        if isinstance(raw_identity, (str, bytes)) or len(raw_identity) != 4:
            raise RoutingValidationError(
                "conditional predecessor identity must be a "
                "(provider, model, profile, profile_sha256) tuple"
            )
        predecessor, model, profile, profile_sha256 = raw_identity
        if not isinstance(predecessor, str) or not _PROVIDER_ID.fullmatch(predecessor):
            raise RoutingValidationError("conditional predecessor provider_id is invalid")
        normalized = _normalize_execution_identity(
            (model or "inherit", profile, profile_sha256),
            f"conditional predecessor identity {index}",
        )
        expected_prefix_rows.append((predecessor, *normalized))
    expected_prefix = tuple(expected_prefix_rows)
    if not expected_prefix:
        return leader_acceptance_prior(
            history,
            provider_id,
            task_type=task_type,
            difficulty=difficulty,
            execution_identity=execution_identity,
            require_exact_scope=require_exact_scope,
            minimum_observations=minimum_observations,
        )

    # `_position_index` remains accepted for source compatibility, but exact
    # lineage is now verified by predecessor result/attempt ids instead of a
    # broad position match.
    del _position_index
    history_rows = (
        list(history or [])
        if _history_is_deduplicated
        else _deduplicated_leader_history(history or [])
    )
    evidence_index = (
        _evidence_index
        if _evidence_index is not None
        else _conditional_evidence_index(history_rows)
    )
    normalized_target = (
        _normalize_execution_identity(execution_identity, "execution_identity")
        if execution_identity is not None
        else None
    )
    if normalized_target is None:
        paired_rows = [
            row
            for (candidate_provider, _, candidate_prefix), rows in evidence_index.items()
            if candidate_provider == provider_id and candidate_prefix == expected_prefix
            for row in rows
        ]
    else:
        paired_rows = list(
            evidence_index.get(
                (provider_id, normalized_target, expected_prefix),
                (),
            )
        )

    prior = leader_acceptance_prior(
        paired_rows,
        provider_id,
        task_type=task_type,
        difficulty=difficulty,
        execution_identity=execution_identity,
        _history_is_deduplicated=True,
        require_exact_scope=require_exact_scope,
        minimum_observations=minimum_observations,
        _require_unconditional=False,
    )
    return AcceptancePrior(
        provider_id=prior.provider_id,
        scope=f"conditional:{'->'.join(item[0] for item in expected_prefix)}:{prior.scope}",
        observations=prior.observations,
        accepted=prior.accepted,
        prior_alpha=prior.prior_alpha,
        prior_beta=prior.prior_beta,
        posterior_mean=prior.posterior_mean,
        conservative_probability=(
            prior.conservative_probability if prior.observations > 0 else 0.0
        ),
        evidence_result_ids=prior.evidence_result_ids,
        evidence_sha256=prior.evidence_sha256,
    )


def _conditional_position_index(
    history: Iterable[Mapping[str, Any]],
) -> dict[tuple[str, str, str, int], list[Mapping[str, Any]]]:
    index: dict[tuple[str, str, str, int], list[Mapping[str, Any]]] = {}
    for row in history:
        if not isinstance(row, Mapping):
            continue
        task_id = row.get("task_id")
        envelope_id = row.get("route_envelope_id")
        fingerprint = row.get("route_plan_fingerprint")
        step_index = row.get("route_plan_step_index")
        if (
            isinstance(task_id, str)
            and task_id
            and isinstance(envelope_id, str)
            and envelope_id
            and isinstance(fingerprint, str)
            and fingerprint.startswith("sha256:")
            and type(step_index) is int
            and step_index >= 0
        ):
            index.setdefault(
                (task_id, envelope_id, fingerprint, step_index),
                [],
            ).append(row)
    return index


def _conditional_evidence_index(
    history: Sequence[Mapping[str, Any]],
) -> dict[
    tuple[str, ExecutionIdentity, tuple[PredecessorExecutionIdentity, ...]],
    list[Mapping[str, Any]],
]:
    """Validate every conditional lineage once and index sufficient rows."""

    result_index = {
        str(row["id"]): row
        for row in history
        if isinstance(row.get("id"), str) and row.get("id")
    }
    index: dict[
        tuple[str, ExecutionIdentity, tuple[PredecessorExecutionIdentity, ...]],
        list[Mapping[str, Any]],
    ] = {}
    for row in history:
        provider_id = _row_provider_id(row)
        target_identity = _row_execution_identity(row)
        task_id = row.get("task_id")
        envelope_id = row.get("route_envelope_id")
        fingerprint = row.get("route_plan_fingerprint")
        step_index = row.get("route_plan_step_index")
        raw_prefix = row.get("route_predecessors")
        if (
            provider_id is None
            or target_identity is None
            or not isinstance(row.get("id"), str)
            or not isinstance(row.get("attempt_id"), str)
            or not isinstance(task_id, str)
            or not task_id
            or not isinstance(envelope_id, str)
            or not envelope_id
            or not isinstance(fingerprint, str)
            or not fingerprint.startswith("sha256:")
            or type(step_index) is not int
            or step_index <= 0
            or not isinstance(raw_prefix, list)
            or len(raw_prefix) != step_index
        ):
            continue
        prefix: list[PredecessorExecutionIdentity] = []
        valid = True
        for predecessor_index, predecessor in enumerate(raw_prefix):
            if not isinstance(predecessor, Mapping):
                valid = False
                break
            predecessor_provider = predecessor.get("provider_id")
            predecessor_attempt_id = predecessor.get("attempt_id")
            predecessor_result_id = predecessor.get("result_id")
            if (
                not isinstance(predecessor_provider, str)
                or not _PROVIDER_ID.fullmatch(predecessor_provider)
                or not isinstance(predecessor_attempt_id, str)
                or not predecessor_attempt_id
                or not isinstance(predecessor_result_id, str)
                or not predecessor_result_id
            ):
                valid = False
                break
            try:
                identity = _normalize_execution_identity(
                    (
                        predecessor.get("model") or "inherit",
                        predecessor.get("profile") or None,
                        predecessor.get("profile_sha256"),
                    ),
                    "route predecessor execution identity",
                )
            except RoutingValidationError:
                valid = False
                break
            predecessor_row = result_index.get(predecessor_result_id)
            predecessor_prefix = (
                predecessor_row.get("route_predecessors")
                if predecessor_row is not None
                else None
            )
            if (
                predecessor_row is None
                or predecessor_row.get("attempt_id") != predecessor_attempt_id
                or predecessor_row.get("task_id") != task_id
                or predecessor_row.get("route_envelope_id") != envelope_id
                or predecessor_row.get("route_plan_fingerprint") != fingerprint
                or predecessor_row.get("route_plan_step_index") != predecessor_index
                or _row_provider_id(predecessor_row) != predecessor_provider
                or _row_execution_identity(predecessor_row) != identity
                or predecessor_row.get("accepted_by_leader") is not False
                or predecessor_row.get("status") != "escalate"
                or not isinstance(predecessor_prefix, list)
                or predecessor_prefix != raw_prefix[:predecessor_index]
            ):
                valid = False
                break
            prefix.append((predecessor_provider, *identity))
        if valid:
            index.setdefault(
                (provider_id, target_identity, tuple(prefix)),
                [],
            ).append(row)
    return index


def _provider_execution_identity(
    provider: Mapping[str, Any],
    execution_identities: Mapping[str, ExecutionIdentity] | None,
) -> ExecutionIdentity:
    provider_id = str(provider.get("provider_id") or "")
    supplied = (
        execution_identities.get(provider_id)
        if execution_identities is not None
        else None
    )
    value = supplied or (
        str(provider.get("model") or "inherit"),
        provider.get("profile"),
        None,
    )
    return _normalize_execution_identity(
        value,
        f"provider {provider_id} execution identity",
    )


def _rank_candidates(
    providers: Sequence[dict[str, Any]],
    *,
    history: Iterable[Mapping[str, Any]] | None,
    task_type: str,
    difficulty: str,
    input_tokens: int,
    output_tokens: int,
    cached_input_tokens: int = 0,
    allow_costs: bool = True,
    execution_identities: Mapping[str, ExecutionIdentity] | None = None,
    _history_is_deduplicated: bool = False,
    rank_by_cost_per_accepted: bool = False,
) -> list[tuple[dict[str, Any], AcceptancePrior, Decimal | None]]:
    rows: list[tuple[dict[str, Any], AcceptancePrior, Decimal | None]] = []
    history_rows = (
        list(history or [])
        if _history_is_deduplicated
        else _deduplicated_leader_history(history or [])
    )
    for provider in providers:
        provider_id = str(provider["provider_id"])
        identity = _provider_execution_identity(provider, execution_identities)
        prior = leader_acceptance_prior(
            history_rows,
            provider_id,
            task_type=task_type,
            difficulty=difficulty,
            execution_identity=identity,
            _history_is_deduplicated=True,
        )
        cost_units = (
            estimate_cost_nano_cny(
                provider,
                input_tokens=input_tokens,
                cached_input_tokens=cached_input_tokens,
                output_tokens=output_tokens,
            )
            if allow_costs
            else None
        )
        cost = None if cost_units is None else Decimal(cost_units) / _NANO_CNY
        rows.append((provider, prior, cost))
    if rank_by_cost_per_accepted:
        rows.sort(
            key=lambda item: (
                item[2] is None or item[1].conservative_probability <= 0,
                (
                    item[2] / Decimal(str(item[1].conservative_probability))
                    if item[2] is not None and item[1].conservative_probability > 0
                    else Decimal("Infinity")
                ),
                -item[1].conservative_probability,
                item[2] if item[2] is not None else Decimal("Infinity"),
                item[0]["priority"],
                item[0]["provider_id"],
            )
        )
    else:
        rows.sort(
            key=lambda item: (
                item[0]["priority"],
                -item[1].conservative_probability,
                item[2] is None,
                item[2] if item[2] is not None else math.inf,
                item[0]["provider_id"],
            )
        )
    return rows


def _auto_chain_plans(
    enabled: Sequence[dict[str, Any]],
    *,
    floor: str,
    history: Iterable[Mapping[str, Any]] | None,
    task_type: str,
    difficulty: str,
    input_tokens: int,
    output_tokens: int,
    cached_input_tokens: int = 0,
    execution_identities: Mapping[str, ExecutionIdentity] | None = None,
    _history_is_deduplicated: bool = False,
    sla_evidence: bool = False,
) -> list[dict[str, Any]]:
    """Enumerate bounded monotonic provider chains and price accepted outcomes.

    Mature economic routing may continue to a different provider in the same
    tier, but it may never repeat a provider, downgrade a tier, or admit more
    than three total attempts.  The cold-start bootstrap selector below applies
    a stricter one-provider-per-tier filter to these plans.
    """

    history_rows = (
        list(history or [])
        if _history_is_deduplicated
        else _deduplicated_leader_history(history or [])
    )
    conditional_evidence_index = _conditional_evidence_index(history_rows)
    conditional_prior_cache: dict[
        tuple[str, ExecutionIdentity, tuple[PredecessorExecutionIdentity, ...]],
        AcceptancePrior,
    ] = {}
    empty_conditional_base_cache: dict[
        tuple[str, ExecutionIdentity, bool], AcceptancePrior
    ] = {}
    marginal_rows = {
        str(row[0]["provider_id"]): row
        for row in _rank_candidates(
            [
                provider
                for provider in enabled
                if TIER_RANK[provider["tier"]] >= TIER_RANK[floor]
            ],
            history=history_rows,
            task_type=task_type,
            difficulty=difficulty,
            input_tokens=input_tokens + cached_input_tokens,
            output_tokens=output_tokens,
            cached_input_tokens=0,
            execution_identities=execution_identities,
            _history_is_deduplicated=True,
        )
    }
    ordered_rows = tuple(
        sorted(
            marginal_rows.values(),
            key=lambda row: (
                TIER_RANK[row[0]["tier"]],
                row[0]["priority"],
                row[0]["provider_id"],
            ),
        )
    )
    # With no caller-proven cache origin, a provider's execution identity,
    # per-step forecast, and immutable price are invariant across every chain
    # position.  Precompute them once instead of repeating Decimal parsing and
    # identity normalization for O(n^3) candidate chains.
    static_steps: dict[
        str,
        tuple[ExecutionIdentity, dict[str, Any], Decimal | None],
    ] = {}
    for static_provider in enabled:
        static_identity = _provider_execution_identity(
            static_provider,
            execution_identities,
        )
        static_forecast = derive_step_token_forecast(
            input_tokens=input_tokens,
            cached_input_tokens=cached_input_tokens,
            output_tokens=output_tokens,
            target_provider=static_provider,
            target_execution_identity=static_identity,
            step_index=0,
            cache_origin=None,
        )
        static_cost_units = estimate_cost_nano_cny(
            static_provider,
            input_tokens=static_forecast["estimated_input_tokens"],
            cached_input_tokens=static_forecast[
                "estimated_cached_input_tokens"
            ],
            output_tokens=static_forecast["estimated_output_tokens"],
        )
        static_steps[str(static_provider["provider_id"])] = (
            static_identity,
            static_forecast,
            (
                None
                if static_cost_units is None
                else Decimal(static_cost_units) / _NANO_CNY
            ),
        )

    def continuation_sequences(
        prefix: tuple[tuple[dict[str, Any], AcceptancePrior, Decimal | None], ...],
        *,
        last_rank: int,
        seen_provider_ids: frozenset[str],
    ) -> Iterable[tuple[tuple[dict[str, Any], AcceptancePrior, Decimal | None], ...]]:
        """Yield every legal continuation, including an immediate early stop."""

        yield prefix
        if len(prefix) >= 2:
            return
        for row in ordered_rows:
            candidate = row[0]
            provider_id = str(candidate["provider_id"])
            candidate_rank = TIER_RANK[candidate["tier"]]
            if provider_id in seen_provider_ids or candidate_rank < last_rank:
                continue
            yield from continuation_sequences(
                (*prefix, row),
                last_rank=candidate_rank,
                seen_provider_ids=seen_provider_ids | {provider_id},
            )

    plans: list[dict[str, Any]] = []
    for start in enabled:
        if TIER_RANK[start["tier"]] < TIER_RANK[floor]:
            continue
        start_identity = static_steps[str(start["provider_id"])][0]
        start_prior = leader_acceptance_prior(
            history_rows,
            start["provider_id"],
            task_type=task_type,
            difficulty=difficulty,
            execution_identity=start_identity,
            _history_is_deduplicated=True,
            require_exact_scope=sla_evidence,
            minimum_observations=10 if sla_evidence else 0,
        )
        start_row = (
            start,
            start_prior,
            None,
        )
        combinations = continuation_sequences(
            (),
            last_rank=TIER_RANK[start["tier"]],
            seen_provider_ids=frozenset({str(start["provider_id"])}),
        )
        for continuation in combinations:
            marginal_chain = [start_row, *continuation]
            chain: list[
                tuple[
                    dict[str, Any],
                    AcceptancePrior,
                    Decimal | None,
                    dict[str, Any],
                ]
            ] = []
            conditional_exact_observations: list[int] = []
            predecessor_identities: list[PredecessorExecutionIdentity] = []
            for index, (item_provider, marginal_prior, _) in enumerate(marginal_chain):
                item_identity, item_forecast, item_cost = static_steps[
                    str(item_provider["provider_id"])
                ]
                item_prior = marginal_prior
                if index:
                    cache_key = (
                        str(item_provider["provider_id"]),
                        item_identity,
                        tuple(predecessor_identities),
                    )
                    item_prior = conditional_prior_cache.get(cache_key)
                    if item_prior is None:
                        if cache_key not in conditional_evidence_index:
                            # Most bounded-catalog combinations have no exact
                            # predecessor-conditioned rows.  Their cold prior
                            # differs only by provider/execution scope and the
                            # rendered predecessor prefix, so build the common
                            # empty base once instead of running the full
                            # evidence pipeline for every O(n^3) chain.
                            empty_key = (
                                str(item_provider["provider_id"]),
                                item_identity,
                                sla_evidence,
                            )
                            empty_base = empty_conditional_base_cache.get(empty_key)
                            if empty_base is None:
                                empty_base = leader_acceptance_prior(
                                    (),
                                    item_provider["provider_id"],
                                    task_type=task_type,
                                    difficulty=difficulty,
                                    execution_identity=item_identity,
                                    _history_is_deduplicated=True,
                                    require_exact_scope=sla_evidence,
                                    minimum_observations=10 if sla_evidence else 0,
                                )
                                empty_conditional_base_cache[empty_key] = empty_base
                            item_prior = AcceptancePrior(
                                provider_id=empty_base.provider_id,
                                scope=(
                                    "conditional:"
                                    + "->".join(
                                        identity[0]
                                        for identity in predecessor_identities
                                    )
                                    + f":{empty_base.scope}"
                                ),
                                observations=0,
                                accepted=0,
                                prior_alpha=empty_base.prior_alpha,
                                prior_beta=empty_base.prior_beta,
                                posterior_mean=empty_base.posterior_mean,
                                conservative_probability=0.0,
                                evidence_result_ids=empty_base.evidence_result_ids,
                                evidence_sha256=empty_base.evidence_sha256,
                            )
                        else:
                            item_prior = conditional_leader_acceptance_prior(
                                history_rows,
                                item_provider["provider_id"],
                                predecessor_execution_identities=predecessor_identities,
                                task_type=task_type,
                                difficulty=difficulty,
                                execution_identity=item_identity,
                                _history_is_deduplicated=True,
                                require_exact_scope=sla_evidence,
                                minimum_observations=10 if sla_evidence else 0,
                                _evidence_index=conditional_evidence_index,
                            )
                        conditional_prior_cache[cache_key] = item_prior
                    # A positive success floor already requires exact scoped
                    # evidence, so the admitted prior carries the observation
                    # count needed by that path.  Without a success floor only
                    # the economically best complete bootstrap chain needs its
                    # exact observation count; compute that one lazily after
                    # exhaustive ranking instead of duplicating this lookup for
                    # every O(n^3) candidate chain.
                    if sla_evidence:
                        conditional_exact_observations.append(item_prior.observations)
                chain.append((item_provider, item_prior, item_cost, item_forecast))
                predecessor_identities.append(
                    (
                        item_provider["provider_id"],
                        item_identity[0],
                        item_identity[1],
                        item_identity[2],
                    )
                )
            if any(cost is None for _, _, cost, _ in chain):
                continue
            survival = Decimal(1)
            expected_cost = Decimal(0)
            for _, prior, cost, _ in chain:
                assert cost is not None
                probability = Decimal(str(prior.conservative_probability))
                expected_cost += survival * cost
                survival *= Decimal(1) - probability
            success_probability = Decimal(1) - survival
            if success_probability <= 0:
                continue
            objective = expected_cost / success_probability
            worst_case_cost_exact = sum(
                (item[2] for item in chain if item[2] is not None),
                Decimal(0),
            )
            plans.append(
                {
                    "provider": start,
                    "prior": start_prior,
                    "cost": chain[0][2],
                    "chain": tuple(item[0]["provider_id"] for item in chain),
                    "tiers": tuple(item[0]["tier"] for item in chain),
                    # Route-step serialization includes profile/evidence
                    # bindings and is comparatively expensive.  Preserve the
                    # evaluated rows here and materialize only the selected
                    # plan after exhaustive ranking.
                    "_chain_rows": tuple(chain),
                    "_worst_case_cost_exact": worst_case_cost_exact,
                    "_expected_cost_exact": expected_cost,
                    "_success_probability_exact": success_probability,
                    "_objective_exact": objective,
                    "_conditional_exact_observations": tuple(
                        conditional_exact_observations
                    ),
                }
            )
    plans.sort(
        key=lambda plan: (
            plan["_objective_exact"],
            -plan["_success_probability_exact"],
            plan["provider"]["priority"],
            plan["provider"]["provider_id"],
            plan["chain"],
        )
    )
    if not sla_evidence:
        available_tiers = tuple(
            tier
            for tier in TIERS[TIER_RANK[floor] :]
            if any(provider["tier"] == tier for provider in enabled)
        )
        bootstrap_candidate = next(
            (plan for plan in plans if plan["tiers"] == available_tiers),
            None,
        )
        if bootstrap_candidate is not None:
            exact_observations: list[int] = []
            predecessor_identities: list[PredecessorExecutionIdentity] = []
            for index, (item_provider, _, _, _) in enumerate(
                bootstrap_candidate["_chain_rows"]
            ):
                item_identity = _provider_execution_identity(
                    item_provider,
                    execution_identities,
                )
                if index:
                    exact_prior = conditional_leader_acceptance_prior(
                        history_rows,
                        item_provider["provider_id"],
                        predecessor_execution_identities=predecessor_identities,
                        task_type=task_type,
                        difficulty=difficulty,
                        execution_identity=item_identity,
                        _history_is_deduplicated=True,
                        require_exact_scope=True,
                        _evidence_index=conditional_evidence_index,
                    )
                    exact_observations.append(exact_prior.observations)
                predecessor_identities.append(
                    (
                        item_provider["provider_id"],
                        item_identity[0],
                        item_identity[1],
                        item_identity[2],
                    )
                )
            bootstrap_candidate["_conditional_exact_observations"] = tuple(
                exact_observations
            )
    return plans


def _conditional_bootstrap_plan(
    plans: Sequence[dict[str, Any]],
    enabled: Sequence[dict[str, Any]],
    *,
    floor: str,
) -> dict[str, Any] | None:
    """Return the most cost-efficient full chain until its lineage is reliable.

    This is an evidence-collection policy, not an assumed-success model.  The
    plan retains the real conservative probabilities (including zero for an
    unseen continuation), while leader acceptance still stops the chain before
    any unnecessary successor call.
    """

    available_tiers = tuple(
        tier
        for tier in TIERS[TIER_RANK[floor] :]
        if any(provider["tier"] == tier for provider in enabled)
    )
    if len(available_tiers) <= 1:
        return None
    full_plans = [
        plan
        for plan in plans
        if plan["tiers"] == available_tiers
    ]
    if not full_plans:
        return None
    # ``plans`` is already ordered by the exhaustive economic objective, so the
    # canonical bootstrap lineage also chooses peers economically and remains
    # deterministic under a frozen catalog/history snapshot.
    plan = full_plans[0]
    observations = plan["_conditional_exact_observations"]
    if any(
        count < BOOTSTRAP_MIN_CONDITIONAL_OBSERVATIONS
        for count in observations
    ):
        return plan
    return None


def decide_route(
    task: Mapping[str, Any],
    catalog: Mapping[str, Any],
    *,
    requested_provider_id: str | None = None,
    requested_tier: str | None = None,
    history: Iterable[Mapping[str, Any]] | None = None,
    execution_identities: Mapping[str, ExecutionIdentity] | None = None,
    input_tokens: int = 0,
    cached_input_tokens: int = 0,
    output_tokens: int = 0,
    now: datetime | str | None = None,
) -> RouteDecision:
    """Choose and explain a provider without mutating scheduler state."""

    values = validate_task_routing(task)
    normalized = validate_provider_catalog(catalog)
    history_rows = _deduplicated_leader_history(history or [])
    floor = auto_tier_floor(values)
    _require_token_count(input_tokens, "input_tokens")
    _require_token_count(cached_input_tokens, "cached_input_tokens")
    _require_token_count(output_tokens, "output_tokens")
    if requested_tier == "auto":
        requested_tier = None
    if requested_tier is not None and requested_tier not in TIER_RANK:
        raise RoutingValidationError(f"requested_tier must be one of: {', '.join(TIERS)}")

    required_capabilities = set(values["required_capabilities"])
    enabled = [
        provider
        for provider in normalized["providers"]
        if provider["enabled"] and required_capabilities.issubset(set(provider["capabilities"]))
    ]
    if not enabled:
        detail = f" with capabilities {sorted(required_capabilities)}" if required_capabilities else ""
        raise RoutingValidationError(f"provider_catalog has no enabled providers{detail}")
    if requested_provider_id is not None:
        matches = [row for row in enabled if row["provider_id"] == requested_provider_id]
        if not matches:
            known = {row["provider_id"] for row in normalized["providers"]}
            if requested_provider_id in known:
                raise RoutingValidationError(f"requested provider is disabled: {requested_provider_id}")
            raise RoutingValidationError(f"unknown provider_id: {requested_provider_id}")
        provider = matches[0]
        if TIER_RANK[provider["tier"]] < TIER_RANK[floor]:
            raise RoutingValidationError(
                f"requested provider {requested_provider_id} tier {provider['tier']} is below safe floor {floor}"
            )
        if requested_tier is not None and provider["tier"] != requested_tier:
            raise RoutingValidationError(
                f"requested provider {requested_provider_id} is tier {provider['tier']}, not {requested_tier}"
            )
        selected_tier = provider["tier"]
        candidates = [provider]
        reason = "explicit provider satisfied the task tier floor"
    else:
        minimum_rank = TIER_RANK[floor]
        if requested_tier is not None:
            if TIER_RANK[requested_tier] < minimum_rank:
                raise RoutingValidationError(
                    f"requested tier {requested_tier} is below safe floor {floor}"
                )
            minimum_rank = TIER_RANK[requested_tier]
        selected_tier = ""
        candidates = []
        for tier in TIERS[minimum_rank:]:
            tier_rows = [row for row in enabled if row["tier"] == tier]
            if tier_rows:
                selected_tier = tier
                candidates = tier_rows
                break
        if not candidates:
            raise RoutingValidationError(
                f"no enabled provider satisfies tier floor {TIERS[minimum_rank]}"
            )
        if selected_tier == floor and requested_tier is None:
            reason = f"auto routing selected the minimum safe tier {floor}"
        elif requested_tier is not None and selected_tier == requested_tier:
            reason = f"selected requested tier {requested_tier} above or at safe floor {floor}"
        else:
            base = requested_tier or floor
            reason = f"no enabled provider at tier {base}; selected next available stronger tier {selected_tier}"

    # The peer limit bounds the combinatorial auto-chain planner.  Explicit
    # provider selection is a single lookup and explicit-tier selection only
    # ranks that tier, so unrelated catalog breadth must not make either mode
    # unavailable.  Likewise, providers below the task's effective safety
    # floor can never enter an auto plan and therefore do not consume its
    # bounded peer budget.
    if requested_provider_id is None and requested_tier is None:
        planning_enabled = [
            provider
            for provider in enabled
            if TIER_RANK[provider["tier"]] >= TIER_RANK[floor]
        ]
        planning_counts = {
            tier: sum(provider["tier"] == tier for provider in planning_enabled)
            for tier in TIERS[TIER_RANK[floor] :]
        }
        oversized_tiers = {
            tier: count
            for tier, count in planning_counts.items()
            if count > MAX_ENABLED_PROVIDERS_PER_TIER
        }
        if oversized_tiers:
            detail = ", ".join(
                f"{tier}={count}" for tier, count in sorted(oversized_tiers.items())
            )
            raise RoutingValidationError(
                "too many enabled capability-compatible providers for bounded auto route optimization "
                f"({detail}); maximum per tier is {MAX_ENABLED_PROVIDERS_PER_TIER}"
            )

    pricing_scope = (
        candidates
        if requested_provider_id is not None or requested_tier is not None
        else [provider for provider in enabled if TIER_RANK[provider["tier"]] >= TIER_RANK[floor]]
    )
    optimization_pricing_ready, optimization_pricing_status, optimization_pricing_currency = _economic_pricing_gate(
        pricing_scope,
        now=now,
        # Canonical providers are priced per concrete route step below.  A
        # provider without a cached-input rate may still be a fully priced
        # cross-provider successor after cached input is reclassified as
        # ordinary.  Beta legacy prices have no cached dimension at all and
        # retain their fail-closed compatibility gate.
        # The task entry contract does not prove a cache origin, so first-step
        # cached estimates are conservatively repriced as ordinary input.
        require_cached_input=False,
    )
    candidate_pricing_ready, candidate_pricing_status, candidate_pricing_currency = _economic_pricing_gate(
        candidates,
        now=now,
        require_cached_input=False,
    )
    uses_canonical_pricing = any(provider.get("pricing") is not None for provider in pricing_scope)
    selection_pricing_ready = (
        optimization_pricing_ready if uses_canonical_pricing else candidate_pricing_ready
    )
    pricing_status = optimization_pricing_status
    pricing_currency = optimization_pricing_currency
    if not uses_canonical_pricing and candidate_pricing_ready:
        pricing_status = (
            candidate_pricing_status
            if optimization_pricing_ready
            else f"{candidate_pricing_status}-partial"
        )
        pricing_currency = candidate_pricing_currency
    ranked = _rank_candidates(
        candidates,
        history=history_rows,
        task_type=values["task_type"],
        difficulty=values["difficulty"],
        input_tokens=input_tokens + cached_input_tokens,
        output_tokens=output_tokens,
        cached_input_tokens=0,
        allow_costs=selection_pricing_ready,
        execution_identities=execution_identities,
        _history_is_deduplicated=True,
        rank_by_cost_per_accepted=(
            requested_provider_id is None
            and requested_tier is not None
            and input_tokens + cached_input_tokens + output_tokens > 0
            and selection_pricing_ready
        ),
    )
    provider, prior, cost = ranked[0]
    candidate_ids = tuple(item[0]["provider_id"] for item in ranked)
    planned_provider_ids = (provider["provider_id"],)
    planned_steps: tuple[dict[str, Any], ...] = ()
    worst_case_chain_cost: float | None = None
    worst_case_chain_cost_exact: Decimal | None = None
    expected_chain_cost = None
    expected_chain_cost_exact: Decimal | None = None
    expected_success = None
    expected_success_exact: Decimal | None = None
    expected_cost_per_accepted = None
    expected_cost_per_accepted_exact: Decimal | None = None
    optimization_mode = "safe-tier"
    economics_ready = (
        requested_provider_id is None
        and requested_tier is None
        and input_tokens + cached_input_tokens + output_tokens > 0
        and optimization_pricing_ready
    )
    if (values.get("min_success_probability") or 0) > 0 and not economics_ready:
        raise RoutingValidationError(
            "minimum success probability requires auto routing, non-zero token estimates, and current compatible pricing; "
            f"pricing status is {optimization_pricing_status}"
        )
    if economics_ready:
        plans = _auto_chain_plans(
            enabled,
            floor=floor,
            history=history_rows,
            task_type=values["task_type"],
            difficulty=values["difficulty"],
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cached_input_tokens=cached_input_tokens,
            execution_identities=execution_identities,
            _history_is_deduplicated=True,
            sla_evidence=(values.get("min_success_probability") or 0) > 0,
        )
        if not plans and (values.get("min_success_probability") or 0) > 0:
            raise RoutingValidationError(
                "no priced provider chain has enough exact, profile-bound evidence "
                f"to satisfy minimum success probability {values['min_success_probability']}"
            )
        if plans:
            minimum_success = values.get("min_success_probability")
            if minimum_success is not None and minimum_success > 0:
                minimum_success_decimal = Decimal(str(minimum_success))
                plans = [
                    plan
                    for plan in plans
                    if plan["_success_probability_exact"] >= minimum_success_decimal
                ]
                if not plans:
                    raise RoutingValidationError(
                        f"no priced provider chain satisfies minimum success probability {minimum_success}"
                    )
            if values["routing_objective"] == "completion-first":
                strongest_compatible_rank = max(
                    TIER_RANK[item["tier"]]
                    for item in enabled
                    if TIER_RANK[item["tier"]] >= TIER_RANK[floor]
                )
                plans = [
                    plan
                    for plan in plans
                    if TIER_RANK[plan["tiers"][-1]] == strongest_compatible_rank
                ]
                if not plans:
                    raise RoutingValidationError(
                        "no priced provider chain reaches the strongest compatible tier "
                        "required by completion-first routing"
                    )
            bootstrap = (
                _conditional_bootstrap_plan(plans, enabled, floor=floor)
                if minimum_success is None or minimum_success == 0
                else None
            )
            best = bootstrap or plans[0]
            provider = best["provider"]
            prior = best["prior"]
            cost = best["cost"]
            candidate_ids = tuple(
                dict.fromkeys(plan["provider"]["provider_id"] for plan in plans)
            )
            planned_provider_ids = best["chain"]
            planned_steps = tuple(
                _route_plan_step(
                    item_provider,
                    item_prior,
                    item_cost,
                    index=index,
                    token_forecast=item_forecast,
                    execution_identity=_provider_execution_identity(
                        item_provider,
                        execution_identities,
                    ),
                )
                for index, (
                    item_provider,
                    item_prior,
                    item_cost,
                    item_forecast,
                ) in enumerate(
                    best["_chain_rows"]
                )
            )
            worst_case_chain_cost_exact = best["_worst_case_cost_exact"]
            worst_case_chain_cost = float(worst_case_chain_cost_exact)
            expected_chain_cost_exact = best["_expected_cost_exact"]
            expected_chain_cost = _rounded_decimal_float(expected_chain_cost_exact)
            expected_success_exact = best["_success_probability_exact"]
            expected_success = _rounded_decimal_float(expected_success_exact)
            expected_cost_per_accepted_exact = best["_objective_exact"]
            expected_cost_per_accepted = _rounded_decimal_float(
                expected_cost_per_accepted_exact
            )
            if bootstrap is not None:
                optimization_mode = "conditional-evidence-bootstrap"
                reason = (
                    "conditional evidence bootstrap selected the most cost-efficient complete "
                    f"monotonic chain {' -> '.join(planned_provider_ids)}; each successor "
                    "runs only after an explicit leader rejection, and expected success "
                    "uses observed conservative evidence rather than an assumed SLA probability"
                )
            else:
                optimization_mode = "expected-cost-per-accepted"
                reason = (
                    f"{values['routing_objective']} optimization selected {provider['provider_id']} as the first "
                    f"step of chain {' -> '.join(planned_provider_ids)}"
                )
            if minimum_success is None and bootstrap is None:
                reason += (
                    "; no success-probability floor is configured, so multi-provider "
                    "collaboration is permitted but not required"
                )
    if not selection_pricing_ready:
        cost = None
        reason = f"{reason}; economic pricing gate degraded to safe-tier routing ({pricing_status})"
    elif not optimization_pricing_ready and requested_provider_id is None and requested_tier is None:
        reason = f"{reason}; cross-tier optimization unavailable ({optimization_pricing_status})"
    if not planned_steps:
        direct_identity = _provider_execution_identity(
            provider,
            execution_identities,
        )
        direct_forecast = derive_step_token_forecast(
            input_tokens=input_tokens,
            cached_input_tokens=cached_input_tokens,
            output_tokens=output_tokens,
            target_provider=provider,
            target_execution_identity=direct_identity,
            step_index=0,
        )
        planned_steps = (
            _route_plan_step(
                provider,
                prior,
                cost,
                index=0,
                token_forecast=direct_forecast,
                execution_identity=direct_identity,
            ),
        )
        worst_case_chain_cost = None if cost is None else float(cost)
        worst_case_chain_cost_exact = cost
    plan_fingerprint = route_plan_fingerprint(
        planned_steps,
        input_tokens=input_tokens,
        cached_input_tokens=cached_input_tokens,
        output_tokens=output_tokens,
        routing_objective=values["routing_objective"],
    )
    selected_snapshot = _snapshot_from_provider(provider) if selection_pricing_ready else None
    cost_value = None if cost is None else float(cost)
    cost_text = f"estimated cost CNY {cost_value}" if cost_value is not None else "cost unknown"
    objective_text = (
        f", expected chain cost CNY {expected_chain_cost}, expected success {expected_success}, "
        f"expected cost per accepted result CNY {expected_cost_per_accepted}"
        if expected_cost_per_accepted is not None
        else ""
    )
    explanation = (
        f"Tier floor {floor} from risk={values['risk']}, difficulty={values['difficulty']}, "
        f"task_type={values['task_type']}; {reason}; chose {provider['provider_id']} "
        f"({provider['tier']}) from [{', '.join(candidate_ids)}], {cost_text}, "
        "estimated tokens ordinary-input/cached-input/output "
        f"{input_tokens}/{cached_input_tokens}/{output_tokens}, conservative leader-acceptance "
        f"prior {prior.conservative_probability}, worst-case chain cost CNY "
        f"{worst_case_chain_cost}{objective_text}."
    )
    return RouteDecision(
        provider_id=provider["provider_id"],
        tier=provider["tier"],
        profile=provider["profile"],
        model=provider["model"],
        tier_floor=floor,
        requested_provider_id=requested_provider_id,
        requested_tier=requested_tier,
        routing_objective=values["routing_objective"],
        estimated_input_tokens=input_tokens,
        estimated_cached_input_tokens=cached_input_tokens,
        estimated_output_tokens=output_tokens,
        estimated_cost_cny=cost_value,
        estimated_cost_cny_exact=(
            None if cost is None else _decimal_money_text(cost)
        ),
        acceptance_prior=prior,
        candidate_provider_ids=candidate_ids,
        planned_provider_ids=planned_provider_ids,
        planned_steps=planned_steps,
        worst_case_chain_cost_cny=worst_case_chain_cost,
        worst_case_chain_cost_cny_exact=(
            None
            if worst_case_chain_cost_exact is None
            else _decimal_money_text(worst_case_chain_cost_exact)
        ),
        plan_fingerprint=plan_fingerprint,
        expected_chain_cost_cny=expected_chain_cost,
        expected_chain_cost_cny_exact=(
            None
            if expected_chain_cost_exact is None
            else _decimal_money_text(expected_chain_cost_exact)
        ),
        expected_success_probability=expected_success,
        expected_success_probability_exact=(
            None
            if expected_success_exact is None
            else _decimal_exact_text(expected_success_exact)
        ),
        expected_cost_per_accepted_cny=expected_cost_per_accepted,
        expected_cost_per_accepted_cny_exact=(
            None
            if expected_cost_per_accepted_exact is None
            else _decimal_money_text(expected_cost_per_accepted_exact)
        ),
        optimization_mode=optimization_mode,
        pricing_status=pricing_status,
        pricing_currency=pricing_currency,
        price_snapshot=selected_snapshot,
        reason=reason,
        explanation=explanation,
    )


def next_stronger_provider(
    catalog: Mapping[str, Any],
    current_provider_id: str,
    *,
    required_capabilities: Iterable[str] = (),
    preferred_provider_ids: Sequence[str] | None = None,
    allow_same_tier_preferred: bool = False,
    history: Iterable[Mapping[str, Any]] | None = None,
    execution_identities: Mapping[str, ExecutionIdentity] | None = None,
    task_type: str = "analysis",
    difficulty: str = "normal",
    input_tokens: int = 0,
    cached_input_tokens: int = 0,
    output_tokens: int = 0,
    now: datetime | str | None = None,
) -> dict[str, Any] | None:
    """Return the next provider in a sealed plan or a stronger fallback.

    The function skips missing tiers (legacy low/high catalogs) and returns
    ``None`` when the current provider is already at the strongest available
    tier.  A same-tier peer is eligible only when it is the exact next provider
    in a structurally valid preferred chain and the caller explicitly confirms
    that this chain came from a sealed admission envelope.  Ad-hoc selection
    remains strictly stronger-tier only.
    """

    normalized = validate_provider_catalog(catalog)
    current = provider_by_id(normalized, current_provider_id)
    if not current["enabled"]:
        raise RoutingValidationError(f"current provider is disabled: {current_provider_id}")
    if difficulty not in DIFFICULTIES:
        raise RoutingValidationError(
            f"difficulty must be one of: {', '.join(sorted(DIFFICULTIES))}"
        )
    if not isinstance(task_type, str) or not task_type.strip():
        raise RoutingValidationError("task_type must be a non-empty string")
    _require_token_count(input_tokens, "input_tokens")
    _require_token_count(cached_input_tokens, "cached_input_tokens")
    _require_token_count(output_tokens, "output_tokens")
    if isinstance(required_capabilities, (str, bytes)):
        raise RoutingValidationError("required_capabilities must be a sequence of strings")
    capabilities: set[str] = set()
    for raw in required_capabilities:
        if not isinstance(raw, str) or not raw.strip():
            raise RoutingValidationError("required_capabilities must contain non-empty strings")
        capabilities.add(raw.strip())
    enabled = [
        provider
        for provider in normalized["providers"]
        if provider["enabled"] and capabilities.issubset(set(provider["capabilities"]))
    ]
    current_identity = _provider_execution_identity(current, execution_identities)
    cache_origin = _cache_binding(current, current_identity)

    def successor_forecast(provider: Mapping[str, Any]) -> dict[str, Any]:
        return derive_step_token_forecast(
            input_tokens=input_tokens,
            cached_input_tokens=cached_input_tokens,
            output_tokens=output_tokens,
            target_provider=provider,
            target_execution_identity=_provider_execution_identity(
                provider,
                execution_identities,
            ),
            step_index=1,
            cache_origin=cache_origin,
        )

    if type(allow_same_tier_preferred) is not bool:
        raise RoutingValidationError("allow_same_tier_preferred must be boolean")
    preferred: list[str] = []
    if preferred_provider_ids:
        if isinstance(preferred_provider_ids, (str, bytes)):
            raise RoutingValidationError("preferred provider chain must be a sequence of provider IDs")
        preferred = [str(provider_id) for provider_id in preferred_provider_ids]
        if not 1 <= len(preferred) <= 3 or len(preferred) != len(set(preferred)):
            raise RoutingValidationError(
                "preferred provider chain must contain one to three unique provider IDs"
            )
        catalog_by_id = {
            str(provider["provider_id"]): provider
            for provider in normalized["providers"]
        }
        if any(provider_id not in catalog_by_id for provider_id in preferred):
            raise RoutingValidationError("preferred provider chain contains an unknown provider")
        preferred_ranks = [
            TIER_RANK[catalog_by_id[provider_id]["tier"]]
            for provider_id in preferred
        ]
        if preferred_ranks != sorted(preferred_ranks):
            raise RoutingValidationError("preferred provider chain contains a tier downgrade")

    def selected(provider: dict[str, Any], reason: str) -> dict[str, Any]:
        forecast = successor_forecast(provider)
        pricing_ready, pricing_status, pricing_currency = _economic_pricing_gate(
            [provider],
            now=now,
            require_cached_input=forecast["estimated_cached_input_tokens"] > 0,
        )
        ranked = _rank_candidates(
            [provider],
            history=history,
            task_type=task_type.strip().lower(),
            difficulty=difficulty,
            input_tokens=forecast["estimated_input_tokens"],
            output_tokens=forecast["estimated_output_tokens"],
            cached_input_tokens=forecast["estimated_cached_input_tokens"],
            allow_costs=pricing_ready,
            execution_identities=execution_identities,
        )
        chosen, prior, cost = ranked[0]
        snapshot = _snapshot_from_provider(chosen) if pricing_ready else None
        return {
            **deepcopy(chosen),
            "estimated_cost_cny": None if cost is None else float(cost),
            "acceptance_prior": prior.to_dict(),
            "pricing_status": pricing_status,
            "pricing_currency": pricing_currency,
            "price_snapshot": snapshot.to_dict() if snapshot else None,
            "token_forecast": forecast,
            "reason": reason,
        }

    if preferred:
        try:
            current_index = preferred.index(current_provider_id)
        except ValueError:
            current_index = -1
        if current_index >= 0 and current_index < len(preferred) - 1:
            planned_next_id = preferred[current_index + 1]
            planned_next = next(
                (provider for provider in enabled if provider["provider_id"] == planned_next_id),
                None,
            )
            if (
                planned_next is not None
                and (
                    TIER_RANK[planned_next["tier"]] > TIER_RANK[current["tier"]]
                    or (
                        allow_same_tier_preferred
                        and TIER_RANK[planned_next["tier"]]
                        == TIER_RANK[current["tier"]]
                    )
                )
            ):
                return selected(
                    planned_next,
                    f"continued reviewed cost-performance chain after {current_provider_id}",
                )

    for tier in TIERS[TIER_RANK[current["tier"]] + 1 :]:
        candidates = [provider for provider in enabled if provider["tier"] == tier]
        if candidates:
            if preferred_provider_ids:
                try:
                    current_index = preferred.index(current_provider_id)
                except ValueError:
                    current_index = -1
                eligible_by_id = {provider["provider_id"]: provider for provider in candidates}
                if current_index >= 0:
                    for provider_id in preferred[current_index + 1 :]:
                        provider = eligible_by_id.get(provider_id)
                        if provider is not None:
                            return selected(
                                provider,
                                f"continued reviewed cost-performance chain at the next available tier after {current_provider_id}",
                            )
            pricing_ready, pricing_status, pricing_currency = _economic_pricing_gate(
                candidates,
                now=now,
                require_cached_input=False,
            )
            ranked = []
            for candidate in candidates:
                forecast = successor_forecast(candidate)
                ranked.extend(
                    _rank_candidates(
                        [candidate],
                        history=history,
                        task_type=task_type.strip().lower(),
                        difficulty=difficulty,
                        input_tokens=forecast["estimated_input_tokens"],
                        output_tokens=forecast["estimated_output_tokens"],
                        cached_input_tokens=forecast[
                            "estimated_cached_input_tokens"
                        ],
                        allow_costs=pricing_ready,
                        execution_identities=execution_identities,
                    )
                )
            ranked.sort(
                key=lambda item: (
                    item[0]["priority"],
                    -item[1].conservative_probability,
                    item[2] is None,
                    item[2] if item[2] is not None else math.inf,
                    item[0]["provider_id"],
                )
            )
            provider, _, _ = ranked[0]
            return selected(
                provider,
                f"next capability-compatible stronger tier after {current['tier']} is {tier}",
            )
    return None
