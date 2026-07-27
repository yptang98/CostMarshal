"""Reviewed provider metadata overrides and safety-only drift guardrails.

Observations may automatically make routing more conservative, but they can
never add a capability, lower a price, or re-enable a provider.  Those
authority-increasing changes require an explicit, expiring human review.
"""

from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from typing import Any, Mapping
from urllib.parse import urlsplit

from .paths import ProjectLayout
from .routing import (
    RoutingValidationError,
    project_provider_catalog,
    validate_provider_catalog,
)
from .state import read_json, read_jsonl


OBSERVATION_SCHEMA = "costmarshal-provider-observation-v1"
METADATA_SCHEMA = "costmarshal-provider-metadata-v1"
CHECK_NAMES = ("api_schema", "pricing", "capabilities", "behavior")
CHECK_STATES = frozenset({"match", "drift", "unknown", "not-applicable"})
MAX_REVIEW_DAYS = 90


class ProviderGovernanceError(ValueError):
    """Raised when provider metadata evidence or review state is invalid."""


def canonical_sha256(value: Any) -> str:
    payload = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def _timestamp(value: Any, label: str) -> tuple[datetime, str]:
    if not isinstance(value, str) or not value.strip():
        raise ProviderGovernanceError(f"{label} must be an RFC3339 timestamp")
    raw = value.strip()
    try:
        parsed = datetime.fromisoformat(
            raw[:-1] + "+00:00" if raw.endswith("Z") else raw
        )
    except ValueError as exc:
        raise ProviderGovernanceError(
            f"{label} must be an RFC3339 timestamp"
        ) from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ProviderGovernanceError(f"{label} must include a timezone")
    utc = parsed.astimezone(timezone.utc)
    return utc, utc.isoformat().replace("+00:00", "Z")


def _source(value: Any) -> str:
    if not isinstance(value, str) or not value.strip() or len(value.strip()) > 512:
        raise ProviderGovernanceError(
            "provider observation source must be a bounded provenance reference"
        )
    result = value.strip()
    if result.startswith("urn:costmarshal:"):
        return result
    parsed = urlsplit(result)
    if (
        parsed.scheme != "https"
        or not parsed.netloc
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
    ):
        raise ProviderGovernanceError(
            "provider observation source must be a secret-free HTTPS URL or "
            "urn:costmarshal reference"
        )
    return result


def provider_binding_sha256(provider: Mapping[str, Any]) -> str:
    return canonical_sha256(dict(provider))


def validate_observation(raw: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(raw, Mapping):
        raise ProviderGovernanceError("provider observation must be an object")
    expected = {
        "schema_version",
        "observation_id",
        "provider_id",
        "observed_at",
        "recorded_at",
        "source",
        "evidence_sha256",
        "provider_binding_sha256",
        "checks",
        "command_id",
    }
    unknown = set(raw) - expected
    missing = expected - set(raw)
    if unknown or missing:
        details = []
        if missing:
            details.append("missing " + ", ".join(sorted(missing)))
        if unknown:
            details.append("unknown " + ", ".join(sorted(unknown)))
        raise ProviderGovernanceError(
            "provider observation fields are invalid: " + "; ".join(details)
        )
    if raw.get("schema_version") != OBSERVATION_SCHEMA:
        raise ProviderGovernanceError("provider observation schema is invalid")
    for name in ("observation_id", "provider_id", "command_id"):
        value = raw.get(name)
        if (
            not isinstance(value, str)
            or not value
            or len(value) > 128
            or any(character.isspace() for character in value)
        ):
            raise ProviderGovernanceError(
                f"provider observation {name} is invalid"
            )
    observed_dt, observed_at = _timestamp(raw.get("observed_at"), "observed_at")
    recorded_dt, recorded_at = _timestamp(raw.get("recorded_at"), "recorded_at")
    if observed_dt > recorded_dt + timedelta(minutes=5):
        raise ProviderGovernanceError(
            "provider observation cannot be materially future-dated"
        )
    evidence_sha = raw.get("evidence_sha256")
    binding_sha = raw.get("provider_binding_sha256")
    for label, value in (
        ("evidence_sha256", evidence_sha),
        ("provider_binding_sha256", binding_sha),
    ):
        if (
            not isinstance(value, str)
            or len(value) != 71
            or not value.startswith("sha256:")
            or any(character not in "0123456789abcdef" for character in value[7:])
        ):
            raise ProviderGovernanceError(f"{label} is invalid")
    checks = raw.get("checks")
    if not isinstance(checks, Mapping) or set(checks) != set(CHECK_NAMES):
        raise ProviderGovernanceError(
            "provider observation checks must contain exactly "
            + ", ".join(CHECK_NAMES)
        )
    normalized_checks: dict[str, str] = {}
    for name in CHECK_NAMES:
        value = checks.get(name)
        if value not in CHECK_STATES:
            raise ProviderGovernanceError(
                f"provider observation check {name} is invalid"
            )
        normalized_checks[name] = str(value)
    return {
        "schema_version": OBSERVATION_SCHEMA,
        "observation_id": str(raw["observation_id"]),
        "provider_id": str(raw["provider_id"]),
        "observed_at": observed_at,
        "recorded_at": recorded_at,
        "source": _source(raw.get("source")),
        "evidence_sha256": str(evidence_sha),
        "provider_binding_sha256": str(binding_sha),
        "checks": normalized_checks,
        "command_id": str(raw["command_id"]),
    }


def load_observations(layout: ProjectLayout) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    ids: set[str] = set()
    commands: set[str] = set()
    for index, raw in enumerate(read_jsonl(layout.provider_observations_jsonl)):
        try:
            row = validate_observation(raw)
        except ProviderGovernanceError as exc:
            raise ProviderGovernanceError(
                f"provider observation row {index} is invalid: {exc}"
            ) from exc
        if row["observation_id"] in ids:
            raise ProviderGovernanceError(
                f"duplicate provider observation {row['observation_id']}"
            )
        if row["command_id"] in commands:
            raise ProviderGovernanceError(
                f"duplicate provider observation command {row['command_id']}"
            )
        ids.add(row["observation_id"])
        commands.add(row["command_id"])
        rows.append(row)
    return rows


def empty_metadata_document() -> dict[str, Any]:
    return {
        "schema_version": METADATA_SCHEMA,
        "revision": 0,
        "providers": {},
    }


def validate_metadata_document(raw: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(raw, Mapping) or set(raw) != {
        "schema_version",
        "revision",
        "providers",
    }:
        raise ProviderGovernanceError("provider metadata document is invalid")
    if raw.get("schema_version") != METADATA_SCHEMA:
        raise ProviderGovernanceError("provider metadata schema is invalid")
    revision = raw.get("revision")
    if type(revision) is not int or revision < 0:
        raise ProviderGovernanceError("provider metadata revision is invalid")
    providers = raw.get("providers")
    if not isinstance(providers, Mapping):
        raise ProviderGovernanceError("provider metadata providers must be an object")
    normalized: dict[str, dict[str, Any]] = {}
    for provider_id, entry in providers.items():
        if not isinstance(provider_id, str) or not isinstance(entry, Mapping):
            raise ProviderGovernanceError("provider metadata entry is invalid")
        expected = {
            "review_id",
            "provider",
            "provider_binding_sha256",
            "approved_by",
            "reviewed_at",
            "expires_at",
            "observation_ids",
            "command_id",
        }
        if set(entry) != expected:
            raise ProviderGovernanceError(
                f"provider metadata entry {provider_id} fields are invalid"
            )
        catalog = validate_provider_catalog(
            {"schema_version": 1, "providers": [entry["provider"]]}
        )
        provider = catalog["providers"][0]
        if provider["provider_id"] != provider_id:
            raise ProviderGovernanceError(
                f"provider metadata key {provider_id} does not match its provider row"
            )
        binding = provider_binding_sha256(provider)
        if entry.get("provider_binding_sha256") != binding:
            raise ProviderGovernanceError(
                f"provider metadata entry {provider_id} binding hash drifted"
            )
        reviewed_dt, reviewed_at = _timestamp(
            entry.get("reviewed_at"), f"{provider_id}.reviewed_at"
        )
        expires_dt, expires_at = _timestamp(
            entry.get("expires_at"), f"{provider_id}.expires_at"
        )
        if reviewed_dt >= expires_dt:
            raise ProviderGovernanceError(
                f"provider metadata entry {provider_id} expires before review"
            )
        if expires_dt - reviewed_dt > timedelta(days=MAX_REVIEW_DAYS):
            raise ProviderGovernanceError(
                f"provider metadata entry {provider_id} exceeds {MAX_REVIEW_DAYS} days"
            )
        approved_by = entry.get("approved_by")
        if (
            not isinstance(approved_by, str)
            or not approved_by.strip()
            or len(approved_by.strip()) > 128
            or "\r" in approved_by
            or "\n" in approved_by
        ):
            raise ProviderGovernanceError(
                f"provider metadata entry {provider_id} lacks an approver"
            )
        observation_ids = entry.get("observation_ids")
        if (
            not isinstance(observation_ids, list)
            or any(not isinstance(item, str) or not item for item in observation_ids)
            or len(observation_ids) != len(set(observation_ids))
        ):
            raise ProviderGovernanceError(
                f"provider metadata entry {provider_id} observation ids are invalid"
            )
        for name in ("review_id", "command_id"):
            if not isinstance(entry.get(name), str) or not entry[name]:
                raise ProviderGovernanceError(
                    f"provider metadata entry {provider_id} {name} is invalid"
                )
        normalized[provider_id] = {
            "review_id": str(entry["review_id"]),
            "provider": provider,
            "provider_binding_sha256": binding,
            "approved_by": approved_by.strip(),
            "reviewed_at": reviewed_at,
            "expires_at": expires_at,
            "observation_ids": list(observation_ids),
            "command_id": str(entry["command_id"]),
        }
    return {
        "schema_version": METADATA_SCHEMA,
        "revision": revision,
        "providers": normalized,
    }


def load_metadata_document(layout: ProjectLayout) -> dict[str, Any]:
    return validate_metadata_document(
        read_json(layout.provider_metadata_json, empty_metadata_document())
    )


def effective_provider_catalog(
    layout: ProjectLayout,
    project: Mapping[str, Any],
    *,
    now: datetime | None = None,
) -> tuple[dict[str, Any], dict[str, dict[str, Any]]]:
    """Return the reviewed catalog with safety-only observation downgrades."""

    clock = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    catalog = project_provider_catalog(project)
    metadata = load_metadata_document(layout)
    observations = load_observations(layout)
    replacements = metadata["providers"]
    merged = deepcopy(catalog)
    for index, provider in enumerate(merged["providers"]):
        replacement = replacements.get(provider["provider_id"])
        if replacement is not None:
            merged["providers"][index] = deepcopy(replacement["provider"])
    merged = validate_provider_catalog(merged)
    provider_ids = {
        str(provider["provider_id"]) for provider in merged["providers"]
    }
    unknown_observation_providers = sorted(
        {
            row["provider_id"]
            for row in observations
            if row["provider_id"] not in provider_ids
        }
    )
    if unknown_observation_providers:
        raise ProviderGovernanceError(
            "provider observations reference unknown providers: "
            + ", ".join(unknown_observation_providers)
        )
    statuses: dict[str, dict[str, Any]] = {}
    for provider in merged["providers"]:
        provider_id = provider["provider_id"]
        review = replacements.get(provider_id)
        expiry_time = None
        if review is not None:
            expiry_time = _timestamp(review["expires_at"], "expires_at")[0]
        bound_observation_ids = set(
            review.get("observation_ids") or []
        ) if review is not None else set()
        relevant = [
            row
            for row in observations
            if row["provider_id"] == provider_id
            and row["observation_id"] not in bound_observation_ids
        ]
        drift_checks = sorted(
            {
                name
                for row in relevant
                for name, state in row["checks"].items()
                if state == "drift"
            }
        )
        unknown_checks = sorted(
            {
                name
                for row in relevant
                for name, state in row["checks"].items()
                if state == "unknown"
            }
        )
        reasons: list[str] = []
        confidence = 1.0 if review is not None else 0.75
        state = "reviewed" if review is not None else "catalog-baseline"
        if review is not None and expiry_time is not None and expiry_time <= clock:
            state = "blocked"
            confidence = 0.0
            reasons.append("review-expired")
        if drift_checks:
            state = "blocked"
            confidence = 0.0
            reasons.extend(f"drift:{name}" for name in drift_checks)
        elif unknown_checks and state != "blocked":
            state = "degraded"
            confidence = min(confidence, 0.5)
            reasons.extend(f"unknown:{name}" for name in unknown_checks)
        if state == "blocked":
            provider["enabled"] = False
        elif state == "degraded":
            provider["priority"] = min(provider["priority"] + 1000, 2**31 - 1)
        statuses[provider_id] = {
            "provider_id": provider_id,
            "state": state,
            "confidence": confidence,
            "enabled": provider["enabled"],
            "review_id": review.get("review_id") if review else None,
            "reviewed_at": review.get("reviewed_at") if review else None,
            "expires_at": review.get("expires_at") if review else None,
            "latest_observation_id": (
                relevant[-1]["observation_id"] if relevant else None
            ),
            "reasons": reasons,
        }
    return validate_provider_catalog(merged), statuses


def project_with_effective_provider_catalog(
    layout: ProjectLayout,
    project: Mapping[str, Any],
) -> tuple[dict[str, Any], dict[str, dict[str, Any]]]:
    catalog, statuses = effective_provider_catalog(layout, project)
    result = deepcopy(dict(project))
    result["provider_catalog"] = catalog
    return result, statuses


def reviewed_provider_entry(
    *,
    provider: Mapping[str, Any],
    approved_by: str,
    reviewed_at: str,
    expires_at: str,
    observation_ids: list[str],
    review_id: str,
    command_id: str,
) -> dict[str, Any]:
    catalog = validate_provider_catalog(
        {"schema_version": 1, "providers": [provider]}
    )
    normalized = catalog["providers"][0]
    entry = {
        "review_id": review_id,
        "provider": normalized,
        "provider_binding_sha256": provider_binding_sha256(normalized),
        "approved_by": approved_by,
        "reviewed_at": reviewed_at,
        "expires_at": expires_at,
        "observation_ids": observation_ids,
        "command_id": command_id,
    }
    validate_metadata_document(
        {
            "schema_version": METADATA_SCHEMA,
            "revision": 1,
            "providers": {normalized["provider_id"]: entry},
        }
    )
    return entry


__all__ = [
    "CHECK_NAMES",
    "CHECK_STATES",
    "MAX_REVIEW_DAYS",
    "METADATA_SCHEMA",
    "OBSERVATION_SCHEMA",
    "ProviderGovernanceError",
    "canonical_sha256",
    "effective_provider_catalog",
    "empty_metadata_document",
    "load_metadata_document",
    "load_observations",
    "project_with_effective_provider_catalog",
    "provider_binding_sha256",
    "reviewed_provider_entry",
    "validate_metadata_document",
    "validate_observation",
]
