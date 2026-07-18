"""Pure, hash-bound contracts for bounded multi-tier handoffs.

This module deliberately has no scheduler, filesystem, provider, or OCI side
effects.  It defines the immutable evidence chain for at most three distinct
providers whose tiers never decrease; a mature sealed route may therefore use
a same-tier peer before a stronger provider:

* one task collaboration contract freezes the Git base, projected context,
  write scope, initial cumulative change manifest, and token reserves;
* every attempt input binds the exact route step, incoming cumulative changes,
  and (after the first step) one bounded predecessor handoff;
* the prompt binding hashes the exact bytes delivered to the provider;
* a rejected attempt can seal one bounded handoff capsule for its successor;
* an accepted attempt can produce a leader-visible, hash-bound apply preview.

Canonical objects are self-hashed.  The hash is over the canonical JSON body
without the self-hash field, matching the manifest convention in
``context_projection``.  Callers must still persist content-addressed objects
before publishing pointers to them and must fence state changes transactionally.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any

from .security import (
    SecurityValidationError,
    normalize_allowed_path,
    normalize_claim_path,
    validate_actor_id,
    validate_task_id,
)
from .routing import TIER_RANK, route_plan_fingerprint
from .profile_binding import ProfileBindingError, validate_profile_binding


class HandoffContractError(ValueError):
    """Raised when a collaboration contract is unsafe or internally inconsistent."""


_SHA256_RE = re.compile(r"sha256:[0-9a-f]{64}\Z")
_GIT_OID_RE = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})\Z")
_ATTEMPT_ID_RE = re.compile(r"ATT-[A-Za-z0-9][A-Za-z0-9_.-]{0,127}\Z")
_RESULT_ID_RE = re.compile(r"RES-[A-Za-z0-9][A-Za-z0-9_.-]{0,127}\Z")
_ENVELOPE_ID_RE = re.compile(r"ENV-[A-Za-z0-9][A-Za-z0-9_.-]{0,127}\Z")
_RESULT_EVIDENCE_SCHEMA = "costmarshal-result-evidence-v3"
_PROMPT_MAGIC = b"COSTMARSHAL-BOUND-PROMPT-V1\n"
_TASK_PROMPT_DELIMITER = b"\nCOSTMARSHAL-TASK-PROMPT-V1\n"
_ROUTE_STEP_V1_KEYS = {
    "index",
    "provider_id",
    "tier",
    "profile",
    "model",
    "execution_identity",
    "estimated_cost_cny",
    "acceptance_prior",
    "price_basis",
}
_ROUTE_STEP_V2_KEYS = _ROUTE_STEP_V1_KEYS | {"token_forecast"}
_TOKEN_FORECAST_KEYS = {
    "estimated_input_tokens",
    "estimated_cached_input_tokens",
    "estimated_output_tokens",
    "cache_mode",
    "cache_binding",
}
_CACHE_BINDING_KEYS = {"provider_id", "model", "profile", "profile_sha256"}

TASK_CONTRACT_KIND = "costmarshal-collaboration-contract"
ATTEMPT_INPUT_KIND = "costmarshal-attempt-input"
ATTEMPT_OUTPUT_KIND = "costmarshal-attempt-output"
PROMPT_BINDING_KIND = "costmarshal-prompt-binding"
HANDOFF_CAPSULE_KIND = "costmarshal-handoff-capsule"
APPLY_PREVIEW_KIND = "costmarshal-change-apply-preview"


@dataclass(frozen=True)
class HandoffLimits:
    """Hard limits for the successor-visible handoff payload and lineage."""

    max_handoff_bytes: int
    continuation_input_reserve_tokens: int
    handoff_output_reserve_tokens: int
    prompt_framing_reserve_tokens: int = 2048
    max_route_steps: int = 3

    def __post_init__(self) -> None:
        for field_name, value in self.__dict__.items():
            if type(value) is not int or value <= 0:
                raise HandoffContractError(f"{field_name} must be a positive integer")
        # A UTF-8 byte is a conservative tokenizer-independent upper bound of
        # one token.  This avoids claiming tokenizer precision across providers.
        if self.handoff_output_reserve_tokens < self.max_handoff_bytes:
            raise HandoffContractError(
                "handoff_output_reserve_tokens must cover the UTF-8 byte upper bound"
            )
        if self.continuation_input_reserve_tokens < (
            (2 * self.max_handoff_bytes) + self.prompt_framing_reserve_tokens
        ):
            raise HandoffContractError(
                "continuation_input_reserve_tokens must cover JSON-escaped handoff bytes and prompt framing"
            )


COLLABORATION_PHASE_TRANSITIONS: Mapping[str, frozenset[str]] = {
    "prepared": frozenset({"projected"}),
    "projected": frozenset({"prompt_bound"}),
    "prompt_bound": frozenset({"launch_authorized"}),
    "launch_authorized": frozenset({"running", "needs_recovery"}),
    "running": frozenset({"output_sealed", "needs_recovery"}),
    "needs_recovery": frozenset({"running", "output_sealed"}),
    "output_sealed": frozenset({"awaiting_leader"}),
    "awaiting_leader": frozenset({"rejected", "accepted"}),
    "rejected": frozenset({"handoff_sealed", "route_exhausted"}),
    "handoff_sealed": frozenset({"successor_prepared"}),
    "accepted": frozenset({"no_changes_complete", "changes_previewed"}),
    "changes_previewed": frozenset({"changes_applied"}),
    "successor_prepared": frozenset(),
    "no_changes_complete": frozenset(),
    "changes_applied": frozenset(),
    "route_exhausted": frozenset(),
}


def _canonical_json_bytes(value: object) -> bytes:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise HandoffContractError("contract data is not canonical JSON") from exc


def _sha256(payload: bytes) -> str:
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def _with_self_hash(body: Mapping[str, Any], field: str) -> dict[str, Any]:
    result = json.loads(_canonical_json_bytes(body))
    result[field] = _sha256(_canonical_json_bytes(body))
    return result


def _validate_self_hash(
    value: Mapping[str, Any],
    *,
    kind: str,
    hash_field: str,
    schema_versions: frozenset[int] = frozenset({1}),
) -> dict[str, Any]:
    if value.get("schema_version") not in schema_versions or value.get("kind") != kind:
        raise HandoffContractError(f"unsupported {kind} schema")
    observed = value.get(hash_field)
    if not isinstance(observed, str) or not _SHA256_RE.fullmatch(observed):
        raise HandoffContractError(f"{kind} {hash_field} is invalid")
    body = dict(value)
    body.pop(hash_field, None)
    if _sha256(_canonical_json_bytes(body)) != observed:
        raise HandoffContractError(f"{kind} self-hash does not match its canonical payload")
    return json.loads(_canonical_json_bytes(value))


def _require_sha256(value: object, label: str) -> str:
    if not isinstance(value, str) or not _SHA256_RE.fullmatch(value):
        raise HandoffContractError(f"{label} must be a sha256:<lowercase-hex> digest")
    return value


def _require_git_oid(value: object, label: str) -> str:
    if not isinstance(value, str) or not _GIT_OID_RE.fullmatch(value):
        raise HandoffContractError(f"{label} must be a full lowercase Git object id")
    return value


def _require_identifier(value: object, label: str, pattern: re.Pattern[str]) -> str:
    if not isinstance(value, str) or not pattern.fullmatch(value):
        raise HandoffContractError(f"{label} is invalid")
    return value


def _validate_route_step(
    step: Mapping[str, Any], *, index: int, prior_rank: int, seen_providers: set[str]
) -> int:
    route_keys = set(step) - {"profile_binding"}
    if (
        route_keys != _ROUTE_STEP_V1_KEYS
        and route_keys != _ROUTE_STEP_V2_KEYS
    ) or "profile_binding" not in step:
        raise HandoffContractError(
            f"planned_steps[{index}] has unknown or missing route-step fields"
        )
    if step.get("index") != index:
        raise HandoffContractError(f"planned_steps[{index}].index is inconsistent")
    provider_id = step.get("provider_id")
    tier = step.get("tier")
    identity = _canonical_mapping(
        step.get("execution_identity"), f"planned_steps[{index}].execution_identity"
    )
    _require_exact_keys(
        identity,
        {"model", "profile", "profile_sha256"},
        f"planned_steps[{index}].execution_identity",
    )
    if not isinstance(provider_id, str) or not provider_id.strip() or len(provider_id) > 128:
        raise HandoffContractError(f"planned_steps[{index}].provider_id is invalid")
    if any(ord(character) < 33 or ord(character) == 127 for character in provider_id):
        raise HandoffContractError(f"planned_steps[{index}].provider_id contains control/space")
    if provider_id in seen_providers:
        raise HandoffContractError("planned route repeats a provider_id")
    seen_providers.add(provider_id)
    if tier not in TIER_RANK or TIER_RANK[tier] < prior_rank:
        raise HandoffContractError("planned route tiers must be non-decreasing")
    if (
        not isinstance(identity.get("model"), str)
        or not identity.get("model")
        or (
            identity.get("profile") is not None
            and (
                not isinstance(identity.get("profile"), str)
                or not identity.get("profile")
            )
        )
    ):
        raise HandoffContractError(
            f"planned_steps[{index}] lacks an exact model/profile execution identity"
        )
    _require_sha256(
        identity.get("profile_sha256"), f"planned_steps[{index}] profile_sha256"
    )
    if step.get("model") != identity["model"] or step.get("profile") != identity["profile"]:
        raise HandoffContractError(
            f"planned_steps[{index}] model/profile drift from execution_identity"
        )
    try:
        profile_binding = validate_profile_binding(
            step.get("profile_binding"), require_available=True
        )
    except ProfileBindingError as exc:
        raise HandoffContractError(
            f"planned_steps[{index}] profile binding is invalid: {exc}"
        ) from exc
    if (
        profile_binding.get("sha256") != identity["profile_sha256"]
        or profile_binding.get("logical_name") != identity["profile"]
        or profile_binding.get("model") not in {None, identity["model"]}
    ):
        raise HandoffContractError(
            f"planned_steps[{index}] profile binding drifts from execution_identity"
        )
    if route_keys == _ROUTE_STEP_V2_KEYS:
        forecast = _canonical_mapping(
            step.get("token_forecast"), f"planned_steps[{index}].token_forecast"
        )
        _require_exact_keys(
            forecast,
            _TOKEN_FORECAST_KEYS,
            f"planned_steps[{index}].token_forecast",
        )
        for field in (
            "estimated_input_tokens",
            "estimated_cached_input_tokens",
            "estimated_output_tokens",
        ):
            _require_non_negative_int(
                forecast.get(field), f"planned_steps[{index}].token_forecast.{field}"
            )
        if forecast.get("cache_mode") not in {
            "none",
            "bound-origin",
            "exact-identity-reuse",
            "reclassified-as-ordinary",
        }:
            raise HandoffContractError(
                f"planned_steps[{index}].token_forecast.cache_mode is invalid"
            )
        binding = forecast.get("cache_binding")
        cache_mode = forecast.get("cache_mode")
        if cache_mode in {"none", "reclassified-as-ordinary"} and binding is not None:
            raise HandoffContractError(
                f"planned_steps[{index}].token_forecast has an unexpected cache binding"
            )
        if cache_mode in {"bound-origin", "exact-identity-reuse"} and binding is None:
            raise HandoffContractError(
                f"planned_steps[{index}].token_forecast lacks its cache binding"
            )
        if binding is not None:
            binding = _canonical_mapping(
                binding, f"planned_steps[{index}].token_forecast.cache_binding"
            )
            _require_exact_keys(
                binding,
                _CACHE_BINDING_KEYS,
                f"planned_steps[{index}].token_forecast.cache_binding",
            )
            if (
                not isinstance(binding.get("provider_id"), str)
                or not binding.get("provider_id")
                or binding.get("provider_id") != provider_id
                or not isinstance(binding.get("model"), str)
                or not binding.get("model")
                or binding.get("model") != identity.get("model")
                or binding.get("profile") != identity.get("profile")
                or binding.get("profile_sha256") != identity.get("profile_sha256")
            ):
                raise HandoffContractError(
                    f"planned_steps[{index}] cache binding drifts from execution_identity"
                )
    return TIER_RANK[tier]


def _require_non_negative_int(value: object, label: str) -> int:
    if type(value) is not int or value < 0:
        raise HandoffContractError(f"{label} must be a non-negative integer")
    return value


def _require_positive_int(value: object, label: str) -> int:
    result = _require_non_negative_int(value, label)
    if result == 0:
        raise HandoffContractError(f"{label} must be positive")
    return result


def _canonical_mapping(value: object, label: str) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise HandoffContractError(f"{label} must be an object")
    return json.loads(_canonical_json_bytes(dict(value)))


def _require_exact_keys(value: Mapping[str, Any], expected: set[str], label: str) -> None:
    observed = set(value)
    if observed != expected:
        raise HandoffContractError(
            f"{label} has unknown or missing fields: expected {sorted(expected)}, got {sorted(observed)}"
        )


def _canonical_paths(
    values: Iterable[object], *, label: str, write_paths: bool
) -> list[str]:
    if not isinstance(values, (list, tuple)):
        raise HandoffContractError(f"{label} must be a JSON-style list of paths")
    normalizer = normalize_claim_path if write_paths else normalize_allowed_path
    normalized: list[str] = []
    seen: set[str] = set()
    try:
        for value in values:
            path = normalizer(value)
            collision_key = path.casefold()
            if collision_key in seen:
                raise HandoffContractError(f"{label} contains a case-insensitive duplicate: {path}")
            seen.add(collision_key)
            normalized.append(path)
    except SecurityValidationError as exc:
        raise HandoffContractError(str(exc)) from exc
    if normalized != sorted(normalized):
        raise HandoffContractError(f"{label} must be sorted canonically")
    return normalized


def validate_collaboration_phase_transition(current: object, target: object) -> str:
    """Validate one forward-only artifact-state transition."""

    if not isinstance(current, str) or current not in COLLABORATION_PHASE_TRANSITIONS:
        raise HandoffContractError(f"unknown collaboration phase: {current!r}")
    if not isinstance(target, str) or target not in COLLABORATION_PHASE_TRANSITIONS:
        raise HandoffContractError(f"unknown collaboration phase: {target!r}")
    if target not in COLLABORATION_PHASE_TRANSITIONS[current]:
        raise HandoffContractError(
            f"invalid collaboration phase transition: {current} -> {target}"
        )
    return target


def validate_attempt_phase_transition(
    *,
    collaboration_contract: Mapping[str, Any],
    attempt_input: Mapping[str, Any],
    current: object,
    target: object,
) -> str:
    """Validate a phase transition against the attempt's position in its route."""

    contract = validate_collaboration_contract(collaboration_contract)
    attempt = validate_attempt_input(attempt_input, collaboration_contract=contract)
    if attempt.get("collaboration_contract_sha256") != contract["contract_sha256"]:
        raise HandoffContractError("attempt phase transition uses the wrong collaboration contract")
    validated_target = validate_collaboration_phase_transition(current, target)
    step_index = int(attempt["route_step_index"])
    last_index = len(contract["route_policy"]["planned_steps"]) - 1
    if validated_target == "route_exhausted" and step_index != last_index:
        raise HandoffContractError("only the final admitted route step may become route_exhausted")
    if validated_target in {"handoff_sealed", "successor_prepared"} and step_index >= last_index:
        raise HandoffContractError("the final admitted route step has no successor handoff")
    return validated_target


def build_collaboration_contract(
    *,
    task_id: str,
    task_spec: Mapping[str, Any],
    base_sha: str,
    context_allowlist: Iterable[object],
    context_manifest_sha256: str,
    context_file_count: int,
    context_total_size_bytes: int,
    write_scope: Iterable[object],
    initial_change_manifest_sha256: str,
    max_changes: int,
    max_total_upsert_bytes: int,
    estimated_input_tokens: int,
    estimated_cached_input_tokens: int,
    estimated_output_tokens: int,
    handoff_limits: HandoffLimits,
    route_envelope_id: str,
    route_plan_fingerprint_sha256: str,
    planned_steps: Iterable[Mapping[str, Any]],
    routing_objective: str = "cost-only",
) -> dict[str, Any]:
    """Freeze one task's collaboration inputs before its first provider launch."""

    try:
        exact_task_id = validate_task_id(task_id)
    except SecurityValidationError as exc:
        raise HandoffContractError(str(exc)) from exc
    if not isinstance(handoff_limits, HandoffLimits):
        raise HandoffContractError("handoff_limits must be HandoffLimits")
    input_tokens = _require_positive_int(estimated_input_tokens, "estimated_input_tokens")
    cached_tokens = _require_non_negative_int(
        estimated_cached_input_tokens, "estimated_cached_input_tokens"
    )
    output_tokens = _require_positive_int(estimated_output_tokens, "estimated_output_tokens")
    if input_tokens <= handoff_limits.continuation_input_reserve_tokens:
        raise HandoffContractError(
            "estimated_input_tokens must exceed the continuation input reserve"
        )
    if output_tokens <= handoff_limits.handoff_output_reserve_tokens:
        raise HandoffContractError(
            "estimated_output_tokens must exceed the handoff output reserve"
        )
    canonical_task_spec = _canonical_mapping(task_spec, "task_spec")
    if not canonical_task_spec:
        raise HandoffContractError("task_spec must not be empty")
    if not isinstance(planned_steps, (list, tuple)):
        raise HandoffContractError("planned_steps must be an ordered list of route steps")
    canonical_steps = [
        _canonical_mapping(step, f"planned_steps[{index}]")
        for index, step in enumerate(planned_steps)
    ]
    if not 1 <= len(canonical_steps) <= handoff_limits.max_route_steps:
        raise HandoffContractError("planned_steps must contain one to three route steps")
    prior_rank = -1
    seen_providers: set[str] = set()
    for index, step in enumerate(canonical_steps):
        prior_rank = _validate_route_step(
            step,
            index=index,
            prior_rank=prior_rank,
            seen_providers=seen_providers,
        )
    step_forecast_presence = ["token_forecast" in step for step in canonical_steps]
    if any(step_forecast_presence) and not all(step_forecast_presence):
        raise HandoffContractError("planned_steps cannot mix route-step schema versions")
    contract_schema_version = 2 if all(step_forecast_presence) else 1
    normalized_objective = str(routing_objective).strip().lower()
    if normalized_objective not in {"completion-first", "cost-only"}:
        raise HandoffContractError(
            "routing_objective must be completion-first or cost-only"
        )
    expected_plan_fingerprint = route_plan_fingerprint(
        canonical_steps,
        input_tokens=input_tokens,
        cached_input_tokens=cached_tokens,
        output_tokens=output_tokens,
        routing_objective=normalized_objective,
    )
    if route_plan_fingerprint_sha256 != expected_plan_fingerprint:
        raise HandoffContractError("route plan fingerprint does not match its exact bound steps")
    _require_identifier(route_envelope_id, "route_envelope_id", _ENVELOPE_ID_RE)
    body = {
        "schema_version": contract_schema_version,
        "kind": TASK_CONTRACT_KIND,
        "task_id": exact_task_id,
        "task_spec": canonical_task_spec,
        "base_sha": _require_git_oid(base_sha, "base_sha"),
        "context_projection": {
            "manifest_sha256": _require_sha256(
                context_manifest_sha256, "context_manifest_sha256"
            ),
            "allowlist": _canonical_paths(
                context_allowlist, label="context_allowlist", write_paths=False
            ),
            "file_count": _require_non_negative_int(
                context_file_count, "context_file_count"
            ),
            "total_size_bytes": _require_non_negative_int(
                context_total_size_bytes, "context_total_size_bytes"
            ),
        },
        "change_policy": {
            "write_scope": _canonical_paths(
                write_scope, label="write_scope", write_paths=True
            ),
            "initial_manifest_sha256": _require_sha256(
                initial_change_manifest_sha256, "initial_change_manifest_sha256"
            ),
            "max_changes": _require_positive_int(max_changes, "max_changes"),
            "max_total_upsert_bytes": _require_positive_int(
                max_total_upsert_bytes, "max_total_upsert_bytes"
            ),
        },
        "token_policy": {
            "estimated_input_tokens": input_tokens,
            "estimated_cached_input_tokens": cached_tokens,
            "estimated_output_tokens": output_tokens,
            "working_input_budget_tokens": (
                input_tokens - handoff_limits.continuation_input_reserve_tokens
            ),
            "working_output_budget_tokens": (
                output_tokens - handoff_limits.handoff_output_reserve_tokens
            ),
            "continuation_input_reserve_tokens": (
                handoff_limits.continuation_input_reserve_tokens
            ),
            "handoff_output_reserve_tokens": handoff_limits.handoff_output_reserve_tokens,
            "prompt_framing_reserve_tokens": handoff_limits.prompt_framing_reserve_tokens,
            "max_handoff_bytes": handoff_limits.max_handoff_bytes,
            "token_upper_bound": "one-utf8-byte-per-token",
        },
        "route_policy": {
            "max_steps": handoff_limits.max_route_steps,
            "route_envelope_id": route_envelope_id,
            "plan_fingerprint": expected_plan_fingerprint,
            "planned_steps": canonical_steps,
        },
    }
    if contract_schema_version == 2:
        body["route_policy"]["routing_objective"] = normalized_objective
    return _with_self_hash(body, "contract_sha256")


def validate_collaboration_contract(value: Mapping[str, Any]) -> dict[str, Any]:
    """Validate a task collaboration contract, including its derived budgets."""

    contract = _validate_self_hash(
        value,
        kind=TASK_CONTRACT_KIND,
        hash_field="contract_sha256",
        schema_versions=frozenset({1, 2}),
    )
    _require_exact_keys(
        contract,
        {
            "schema_version",
            "kind",
            "task_id",
            "task_spec",
            "base_sha",
            "context_projection",
            "change_policy",
            "token_policy",
            "route_policy",
            "contract_sha256",
        },
        "collaboration contract",
    )
    try:
        validate_task_id(contract.get("task_id"))
    except SecurityValidationError as exc:
        raise HandoffContractError(str(exc)) from exc
    _require_git_oid(contract.get("base_sha"), "base_sha")
    task_spec = _canonical_mapping(contract.get("task_spec"), "task_spec")
    if not task_spec:
        raise HandoffContractError("task_spec must not be empty")
    context = _canonical_mapping(contract.get("context_projection"), "context_projection")
    changes = _canonical_mapping(contract.get("change_policy"), "change_policy")
    tokens = _canonical_mapping(contract.get("token_policy"), "token_policy")
    route = _canonical_mapping(contract.get("route_policy"), "route_policy")
    _require_exact_keys(
        context,
        {"manifest_sha256", "allowlist", "file_count", "total_size_bytes"},
        "context projection",
    )
    _require_exact_keys(
        changes,
        {"write_scope", "initial_manifest_sha256", "max_changes", "max_total_upsert_bytes"},
        "change policy",
    )
    _require_exact_keys(
        tokens,
        {
            "estimated_input_tokens",
            "estimated_cached_input_tokens",
            "estimated_output_tokens",
            "working_input_budget_tokens",
            "working_output_budget_tokens",
            "continuation_input_reserve_tokens",
            "handoff_output_reserve_tokens",
            "prompt_framing_reserve_tokens",
            "max_handoff_bytes",
            "token_upper_bound",
        },
        "token policy",
    )
    route_keys = {"max_steps", "route_envelope_id", "plan_fingerprint", "planned_steps"}
    if contract.get("schema_version") == 2:
        route_keys.add("routing_objective")
    _require_exact_keys(route, route_keys, "route policy")
    routing_objective = (
        str(route.get("routing_objective") or "").strip().lower()
        if contract.get("schema_version") == 2
        else "cost-only"
    )
    if routing_objective not in {"completion-first", "cost-only"}:
        raise HandoffContractError("stored routing objective is invalid")
    _require_sha256(context.get("manifest_sha256"), "context projection hash")
    _canonical_paths(context.get("allowlist"), label="context allowlist", write_paths=False)
    _require_non_negative_int(context.get("file_count"), "context file_count")
    _require_non_negative_int(context.get("total_size_bytes"), "context total size")
    _canonical_paths(changes.get("write_scope"), label="write scope", write_paths=True)
    _require_sha256(changes.get("initial_manifest_sha256"), "initial change manifest hash")
    _require_positive_int(changes.get("max_changes"), "max changes")
    _require_positive_int(
        changes.get("max_total_upsert_bytes"), "max total upsert bytes"
    )
    input_tokens = _require_positive_int(tokens.get("estimated_input_tokens"), "estimated input")
    output_tokens = _require_positive_int(tokens.get("estimated_output_tokens"), "estimated output")
    input_reserve = _require_positive_int(
        tokens.get("continuation_input_reserve_tokens"), "continuation input reserve"
    )
    output_reserve = _require_positive_int(
        tokens.get("handoff_output_reserve_tokens"), "handoff output reserve"
    )
    framing = _require_positive_int(
        tokens.get("prompt_framing_reserve_tokens"), "prompt framing reserve"
    )
    max_bytes = _require_positive_int(tokens.get("max_handoff_bytes"), "max handoff bytes")
    if tokens.get("token_upper_bound") != "one-utf8-byte-per-token":
        raise HandoffContractError("unsupported token upper bound")
    if output_reserve < max_bytes or input_reserve < (2 * max_bytes) + framing:
        raise HandoffContractError("stored token reserves do not cover the bounded handoff")
    if tokens.get("working_input_budget_tokens") != input_tokens - input_reserve:
        raise HandoffContractError("working input budget is inconsistent")
    if tokens.get("working_output_budget_tokens") != output_tokens - output_reserve:
        raise HandoffContractError("working output budget is inconsistent")
    if input_tokens <= input_reserve or output_tokens <= output_reserve:
        raise HandoffContractError("token reserves exhaust the step budget")
    if tokens.get("estimated_cached_input_tokens") is None:
        raise HandoffContractError("estimated cached input is missing")
    _require_non_negative_int(tokens.get("estimated_cached_input_tokens"), "estimated cached input")
    max_steps = _require_positive_int(route.get("max_steps"), "max route steps")
    if max_steps > 3:
        raise HandoffContractError("CostMarshal collaboration supports at most three provider attempts")
    _require_identifier(route.get("route_envelope_id"), "route envelope id", _ENVELOPE_ID_RE)
    plan_fingerprint = _require_sha256(route.get("plan_fingerprint"), "route plan fingerprint")
    raw_steps = route.get("planned_steps")
    if not isinstance(raw_steps, list) or not 1 <= len(raw_steps) <= max_steps:
        raise HandoffContractError("stored route steps are invalid")
    prior_rank = -1
    seen_providers: set[str] = set()
    for index, raw_step in enumerate(raw_steps):
        step = _canonical_mapping(raw_step, f"planned step {index}")
        prior_rank = _validate_route_step(
            step,
            index=index,
            prior_rank=prior_rank,
            seen_providers=seen_providers,
        )
    step_forecast_presence = [
        isinstance(step, Mapping) and "token_forecast" in step for step in raw_steps
    ]
    if any(step_forecast_presence) and not all(step_forecast_presence):
        raise HandoffContractError("stored route cannot mix route-step schema versions")
    expected_contract_schema = 2 if all(step_forecast_presence) else 1
    if contract.get("schema_version") != expected_contract_schema:
        raise HandoffContractError("collaboration contract schema does not match its route steps")
    observed_fingerprint = route_plan_fingerprint(
        raw_steps,
        input_tokens=input_tokens,
        cached_input_tokens=int(tokens["estimated_cached_input_tokens"]),
        output_tokens=output_tokens,
        routing_objective=routing_objective,
    )
    if observed_fingerprint != plan_fingerprint:
        raise HandoffContractError("stored route plan fingerprint is inconsistent")
    return contract


def _step_token_allocation(
    contract: Mapping[str, Any], step_index: int
) -> dict[str, Any]:
    """Return the exact attempt budget for one legacy or per-step route."""

    tokens = json.loads(_canonical_json_bytes(contract["token_policy"]))
    if contract.get("schema_version") == 1:
        return tokens
    steps = contract["route_policy"]["planned_steps"]
    if step_index < 0 or step_index >= len(steps):
        raise HandoffContractError("route_step_index exceeds the immutable admitted route plan")
    forecast = _canonical_mapping(
        steps[step_index].get("token_forecast"),
        f"planned_steps[{step_index}].token_forecast",
    )
    input_tokens = _require_positive_int(
        forecast.get("estimated_input_tokens"), "step estimated input"
    )
    cached_tokens = _require_non_negative_int(
        forecast.get("estimated_cached_input_tokens"), "step estimated cached input"
    )
    output_tokens = _require_positive_int(
        forecast.get("estimated_output_tokens"), "step estimated output"
    )
    input_reserve = int(tokens["continuation_input_reserve_tokens"])
    output_reserve = int(tokens["handoff_output_reserve_tokens"])
    if input_tokens <= input_reserve or output_tokens <= output_reserve:
        raise HandoffContractError("per-step token forecast is exhausted by handoff reserves")
    tokens["estimated_input_tokens"] = input_tokens
    tokens["estimated_cached_input_tokens"] = cached_tokens
    tokens["estimated_output_tokens"] = output_tokens
    tokens["working_input_budget_tokens"] = input_tokens - input_reserve
    tokens["working_output_budget_tokens"] = output_tokens - output_reserve
    return tokens


def build_attempt_input_contract(
    *,
    collaboration_contract: Mapping[str, Any],
    attempt_id: str,
    actor_id: str,
    route_step_index: int,
    incoming_change_manifest_sha256: str,
    incoming_change_count: int,
    incoming_total_upsert_bytes: int,
    predecessor_handoff: Mapping[str, Any] | None = None,
    trusted_predecessor_result: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Bind the exact immutable inputs for one provider attempt."""

    contract = validate_collaboration_contract(collaboration_contract)
    exact_attempt_id = _require_identifier(attempt_id, "attempt_id", _ATTEMPT_ID_RE)
    try:
        exact_actor_id = validate_actor_id(actor_id)
    except SecurityValidationError as exc:
        raise HandoffContractError(str(exc)) from exc
    step_index = _require_non_negative_int(route_step_index, "route_step_index")
    route_policy = contract["route_policy"]
    planned_steps = route_policy["planned_steps"]
    if step_index >= len(planned_steps):
        raise HandoffContractError("route_step_index exceeds the immutable admitted route plan")

    handoff_ref: dict[str, Any] | None = None
    if step_index == 0:
        if predecessor_handoff is not None or trusted_predecessor_result is not None:
            raise HandoffContractError("the first route step must not have a predecessor handoff")
        expected_manifest = contract["change_policy"]["initial_manifest_sha256"]
    else:
        if predecessor_handoff is None:
            raise HandoffContractError("a continuation route step requires a predecessor handoff")
        if trusted_predecessor_result is None:
            raise HandoffContractError("a continuation route step requires trusted predecessor result evidence")
        capsule = validate_handoff_capsule(
            predecessor_handoff,
            trusted_leader_result=trusted_predecessor_result,
        )
        if capsule.get("task_id") != contract["task_id"]:
            raise HandoffContractError("predecessor handoff belongs to a different task")
        if capsule.get("collaboration_contract_sha256") != contract["contract_sha256"]:
            raise HandoffContractError("predecessor handoff belongs to a different collaboration contract")
        if capsule.get("route_step_index") != step_index - 1:
            raise HandoffContractError("predecessor handoff is not the immediately preceding route step")
        expected_manifest = capsule["outgoing_changes"]["manifest_sha256"]
        handoff_ref = {
            "attempt_id": capsule["attempt_id"],
            "result_id": capsule["leader_rejection"]["result_id"],
            "capsule_sha256": capsule["capsule_sha256"],
            "previous_capsule_sha256": capsule.get("previous_capsule_sha256"),
        }
    incoming_manifest = _require_sha256(
        incoming_change_manifest_sha256, "incoming_change_manifest_sha256"
    )
    if incoming_manifest != expected_manifest:
        raise HandoffContractError("incoming changes do not match the predecessor's cumulative artifact")
    change_count = _require_non_negative_int(
        incoming_change_count, "incoming_change_count"
    )
    total_upsert_bytes = _require_non_negative_int(
        incoming_total_upsert_bytes, "incoming_total_upsert_bytes"
    )
    change_policy = contract["change_policy"]
    if change_count > change_policy["max_changes"]:
        raise HandoffContractError("incoming changes exceed the task change-count limit")
    if total_upsert_bytes > change_policy["max_total_upsert_bytes"]:
        raise HandoffContractError("incoming changes exceed the task byte limit")
    if step_index == 0 and (change_count != 0 or total_upsert_bytes != 0):
        raise HandoffContractError("the initial cumulative change artifact must be empty")
    if predecessor_handoff is not None:
        outgoing = capsule["outgoing_changes"]
        if (
            change_count != outgoing["change_count"]
            or total_upsert_bytes != outgoing["total_upsert_bytes"]
        ):
            raise HandoffContractError("incoming change counters do not match the predecessor capsule")
        handoff_payload = capsule["handoff"]["text"].encode("utf-8")
        token_policy = contract["token_policy"]
        if len(handoff_payload) > token_policy["max_handoff_bytes"]:
            raise HandoffContractError("predecessor handoff exceeds the task byte limit")
        if (
            (2 * len(handoff_payload)) + token_policy["prompt_framing_reserve_tokens"]
            > token_policy["continuation_input_reserve_tokens"]
        ):
            raise HandoffContractError("predecessor handoff exceeds the continuation reserve")

    canonical_route_binding = {
        "route_envelope_id": route_policy["route_envelope_id"],
        "route_plan_fingerprint": route_policy["plan_fingerprint"],
        "route_plan_step_index": step_index,
        "planned_step": json.loads(_canonical_json_bytes(planned_steps[step_index])),
    }
    body = {
        "schema_version": int(contract["schema_version"]),
        "kind": ATTEMPT_INPUT_KIND,
        "task_id": contract["task_id"],
        "attempt_id": exact_attempt_id,
        "actor_id": exact_actor_id,
        "route_step_index": step_index,
        "route_binding": canonical_route_binding,
        "collaboration_contract_sha256": contract["contract_sha256"],
        "base_sha": contract["base_sha"],
        "context_manifest_sha256": contract["context_projection"]["manifest_sha256"],
        "incoming_changes": {
            "manifest_sha256": incoming_manifest,
            "change_count": change_count,
            "total_upsert_bytes": total_upsert_bytes,
        },
        "predecessor_handoff": handoff_ref,
        "token_allocation": _step_token_allocation(contract, step_index),
    }
    return _with_self_hash(body, "attempt_input_sha256")


def validate_attempt_input(
    value: Mapping[str, Any],
    *,
    collaboration_contract: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    attempt = _validate_self_hash(
        value,
        kind=ATTEMPT_INPUT_KIND,
        hash_field="attempt_input_sha256",
        schema_versions=frozenset({1, 2}),
    )
    _require_exact_keys(
        attempt,
        {
            "schema_version",
            "kind",
            "task_id",
            "attempt_id",
            "actor_id",
            "route_step_index",
            "route_binding",
            "collaboration_contract_sha256",
            "base_sha",
            "context_manifest_sha256",
            "incoming_changes",
            "predecessor_handoff",
            "token_allocation",
            "attempt_input_sha256",
        },
        "attempt input",
    )
    try:
        validate_task_id(attempt.get("task_id"))
        validate_actor_id(attempt.get("actor_id"))
    except SecurityValidationError as exc:
        raise HandoffContractError(str(exc)) from exc
    _require_identifier(attempt.get("attempt_id"), "attempt id", _ATTEMPT_ID_RE)
    _require_sha256(attempt.get("collaboration_contract_sha256"), "collaboration contract hash")
    _require_git_oid(attempt.get("base_sha"), "base_sha")
    _require_sha256(attempt.get("context_manifest_sha256"), "context manifest hash")
    step_index = _require_non_negative_int(attempt.get("route_step_index"), "route step index")
    route_binding = _canonical_mapping(attempt.get("route_binding"), "route binding")
    _require_exact_keys(
        route_binding,
        {
            "route_envelope_id",
            "route_plan_fingerprint",
            "route_plan_step_index",
            "planned_step",
        },
        "route binding",
    )
    _require_identifier(route_binding.get("route_envelope_id"), "route envelope id", _ENVELOPE_ID_RE)
    _require_sha256(route_binding.get("route_plan_fingerprint"), "route plan fingerprint")
    if route_binding.get("route_plan_step_index") != step_index:
        raise HandoffContractError("route binding step index is inconsistent")
    planned_step = _canonical_mapping(route_binding.get("planned_step"), "planned step")
    _validate_route_step(
        planned_step,
        index=step_index,
        prior_rank=-1,
        seen_providers=set(),
    )
    incoming = _canonical_mapping(attempt.get("incoming_changes"), "incoming changes")
    _require_exact_keys(
        incoming,
        {"manifest_sha256", "change_count", "total_upsert_bytes"},
        "incoming changes",
    )
    _require_sha256(incoming.get("manifest_sha256"), "incoming manifest hash")
    _require_non_negative_int(incoming.get("change_count"), "incoming change count")
    _require_non_negative_int(incoming.get("total_upsert_bytes"), "incoming upsert bytes")
    predecessor = attempt.get("predecessor_handoff")
    if step_index == 0 and predecessor is not None:
        raise HandoffContractError("first route step has an unexpected predecessor handoff")
    if step_index > 0:
        predecessor = _canonical_mapping(predecessor, "predecessor handoff")
        _require_exact_keys(
            predecessor,
            {"attempt_id", "result_id", "capsule_sha256", "previous_capsule_sha256"},
            "predecessor handoff",
        )
        _require_identifier(predecessor.get("attempt_id"), "predecessor attempt id", _ATTEMPT_ID_RE)
        _require_identifier(predecessor.get("result_id"), "predecessor result id", _RESULT_ID_RE)
        _require_sha256(predecessor.get("capsule_sha256"), "predecessor capsule hash")
        previous_capsule = predecessor.get("previous_capsule_sha256")
        if previous_capsule is not None:
            _require_sha256(previous_capsule, "previous predecessor capsule hash")
    tokens = _canonical_mapping(attempt.get("token_allocation"), "token allocation")
    _require_exact_keys(
        tokens,
        {
            "estimated_input_tokens",
            "estimated_cached_input_tokens",
            "estimated_output_tokens",
            "working_input_budget_tokens",
            "working_output_budget_tokens",
            "continuation_input_reserve_tokens",
            "handoff_output_reserve_tokens",
            "prompt_framing_reserve_tokens",
            "max_handoff_bytes",
            "token_upper_bound",
        },
        "token allocation",
    )
    if tokens.get("token_upper_bound") != "one-utf8-byte-per-token":
        raise HandoffContractError("attempt token upper bound is unsupported")
    for field_name in tokens:
        if field_name != "token_upper_bound":
            _require_non_negative_int(tokens.get(field_name), f"token allocation {field_name}")
    if collaboration_contract is not None:
        contract = validate_collaboration_contract(collaboration_contract)
        if (
            attempt.get("schema_version") != contract.get("schema_version")
            or
            attempt.get("task_id") != contract["task_id"]
            or attempt.get("collaboration_contract_sha256") != contract["contract_sha256"]
            or attempt.get("base_sha") != contract["base_sha"]
            or attempt.get("context_manifest_sha256")
            != contract["context_projection"]["manifest_sha256"]
            or tokens != _step_token_allocation(contract, step_index)
            or step_index >= len(contract["route_policy"]["planned_steps"])
            or route_binding["route_envelope_id"]
            != contract["route_policy"]["route_envelope_id"]
            or route_binding["route_plan_fingerprint"]
            != contract["route_policy"]["plan_fingerprint"]
            or planned_step != contract["route_policy"]["planned_steps"][step_index]
        ):
            raise HandoffContractError("attempt input drifts from its collaboration contract")
    return attempt


def _bound_prompt_parts(
    attempt: Mapping[str, Any],
    predecessor_handoff: Mapping[str, Any] | None,
) -> tuple[dict[str, Any], bytes]:
    predecessor_ref = attempt.get("predecessor_handoff")
    if predecessor_ref is None:
        if predecessor_handoff is not None:
            raise HandoffContractError("first-step prompt must not include a predecessor handoff")
        capsule_sha256 = None
        handoff_sha256 = None
        handoff_bytes = b""
    else:
        if predecessor_handoff is None:
            raise HandoffContractError("continuation prompt requires the exact predecessor handoff")
        capsule = validate_handoff_capsule(predecessor_handoff)
        if (
            capsule.get("capsule_sha256") != predecessor_ref.get("capsule_sha256")
            or capsule.get("attempt_id") != predecessor_ref.get("attempt_id")
            or capsule["leader_rejection"].get("result_id") != predecessor_ref.get("result_id")
        ):
            raise HandoffContractError("prompt predecessor handoff does not match attempt input")
        capsule_sha256 = capsule["capsule_sha256"]
        handoff_sha256 = capsule["handoff"]["sha256"]
        handoff_bytes = capsule["handoff"]["text"].encode("utf-8")
    header = {
        "schema_version": 1,
        "attempt_input_sha256": attempt["attempt_input_sha256"],
        "predecessor_capsule_sha256": capsule_sha256,
        "handoff_sha256": handoff_sha256,
        "handoff_size_bytes": len(handoff_bytes),
    }
    evidence_envelope = {
        "schema_version": 1,
        "kind": "costmarshal-untrusted-predecessor-evidence",
        "capsule_sha256": capsule_sha256,
        "text": handoff_bytes.decode("utf-8", errors="strict") if handoff_bytes else None,
    }
    return header, _canonical_json_bytes(evidence_envelope)


def build_bound_prompt_bytes(
    *,
    attempt_input: Mapping[str, Any],
    task_prompt_bytes: bytes,
    predecessor_handoff: Mapping[str, Any] | None = None,
) -> bytes:
    """Frame task instructions with the exact predecessor handoff, if any."""

    attempt = validate_attempt_input(attempt_input)
    if not isinstance(task_prompt_bytes, bytes) or not task_prompt_bytes:
        raise HandoffContractError("task_prompt_bytes must be non-empty immutable bytes")
    header, evidence_envelope = _bound_prompt_parts(attempt, predecessor_handoff)
    return (
        _PROMPT_MAGIC
        + _canonical_json_bytes(header)
        + b"\n"
        + evidence_envelope
        + _TASK_PROMPT_DELIMITER
        + task_prompt_bytes
    )


def _validate_bound_prompt_bytes(
    attempt: Mapping[str, Any],
    prompt_bytes: bytes,
    predecessor_handoff: Mapping[str, Any] | None,
) -> None:
    if not isinstance(prompt_bytes, bytes) or not prompt_bytes.startswith(_PROMPT_MAGIC):
        raise HandoffContractError("prompt is not a canonical CostMarshal bound prompt")
    expected_header, expected_evidence_envelope = _bound_prompt_parts(
        attempt, predecessor_handoff
    )
    header_start = len(_PROMPT_MAGIC)
    header_end = prompt_bytes.find(b"\n", header_start)
    if header_end < 0:
        raise HandoffContractError("bound prompt header is missing")
    raw_header = prompt_bytes[header_start:header_end]
    try:
        decoded_header = json.loads(raw_header.decode("utf-8", errors="strict"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise HandoffContractError("bound prompt header is invalid") from exc
    if raw_header != _canonical_json_bytes(decoded_header) or decoded_header != expected_header:
        raise HandoffContractError("bound prompt header does not match immutable attempt inputs")
    body = prompt_bytes[header_end + 1 :]
    if not body.startswith(expected_evidence_envelope + _TASK_PROMPT_DELIMITER):
        raise HandoffContractError("bound prompt omits or changes the exact predecessor handoff")
    task_payload = body[len(expected_evidence_envelope + _TASK_PROMPT_DELIMITER) :]
    if not task_payload:
        raise HandoffContractError("bound prompt task instructions are empty")


def build_prompt_binding(
    *,
    collaboration_contract: Mapping[str, Any],
    attempt_input: Mapping[str, Any],
    prompt_bytes: bytes,
    predecessor_handoff: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Hash canonical prompt bytes after every semantic input has been frozen."""

    attempt = validate_attempt_input(
        attempt_input, collaboration_contract=collaboration_contract
    )
    _validate_bound_prompt_bytes(attempt, prompt_bytes, predecessor_handoff)
    prompt_token_upper_bound = len(prompt_bytes)
    working_input_budget = attempt["token_allocation"].get(
        "working_input_budget_tokens"
    )
    if type(working_input_budget) is not int or prompt_token_upper_bound > working_input_budget:
        raise HandoffContractError(
            "prompt UTF-8 byte upper bound exceeds the immutable working input budget"
        )
    body = {
        "schema_version": 1,
        "kind": PROMPT_BINDING_KIND,
        "task_id": attempt["task_id"],
        "attempt_id": attempt["attempt_id"],
        "attempt_input_sha256": attempt["attempt_input_sha256"],
        "prompt_sha256": _sha256(prompt_bytes),
        "prompt_size_bytes": len(prompt_bytes),
        "prompt_token_upper_bound": prompt_token_upper_bound,
    }
    return _with_self_hash(body, "binding_sha256")


def validate_prompt_binding(
    value: Mapping[str, Any], *, prompt_bytes: bytes | None = None
) -> dict[str, Any]:
    binding = _validate_self_hash(
        value, kind=PROMPT_BINDING_KIND, hash_field="binding_sha256"
    )
    _require_exact_keys(
        binding,
        {
            "schema_version",
            "kind",
            "task_id",
            "attempt_id",
            "attempt_input_sha256",
            "prompt_sha256",
            "prompt_size_bytes",
            "prompt_token_upper_bound",
            "binding_sha256",
        },
        "prompt binding",
    )
    _require_sha256(binding.get("attempt_input_sha256"), "attempt input hash")
    _require_sha256(binding.get("prompt_sha256"), "prompt hash")
    try:
        validate_task_id(binding.get("task_id"))
    except SecurityValidationError as exc:
        raise HandoffContractError(str(exc)) from exc
    _require_identifier(binding.get("attempt_id"), "attempt id", _ATTEMPT_ID_RE)
    _require_positive_int(binding.get("prompt_size_bytes"), "prompt size")
    token_upper_bound = _require_positive_int(
        binding.get("prompt_token_upper_bound"), "prompt token upper bound"
    )
    if token_upper_bound != binding["prompt_size_bytes"]:
        raise HandoffContractError("prompt token upper bound is inconsistent")
    if prompt_bytes is not None:
        if not isinstance(prompt_bytes, bytes):
            raise HandoffContractError("prompt_bytes must be immutable bytes")
        if len(prompt_bytes) != binding["prompt_size_bytes"] or _sha256(prompt_bytes) != binding["prompt_sha256"]:
            raise HandoffContractError("prompt bytes do not match their immutable binding")
    return binding


def build_attempt_output_contract(
    *,
    collaboration_contract: Mapping[str, Any],
    attempt_input: Mapping[str, Any],
    prompt_binding: Mapping[str, Any],
    execution_receipt_sha256: str,
    report_sha256: str,
    report_size_bytes: int,
    outgoing_change_manifest_sha256: str,
    outgoing_change_count: int,
    outgoing_total_upsert_bytes: int,
) -> dict[str, Any]:
    """Seal report and cumulative changes before any leader result is recorded."""

    contract = validate_collaboration_contract(collaboration_contract)
    attempt = validate_attempt_input(attempt_input, collaboration_contract=contract)
    binding = validate_prompt_binding(prompt_binding)
    if attempt.get("collaboration_contract_sha256") != contract["contract_sha256"]:
        raise HandoffContractError("attempt input belongs to a different collaboration contract")
    if (
        binding.get("task_id") != attempt["task_id"]
        or binding.get("attempt_id") != attempt["attempt_id"]
        or binding.get("attempt_input_sha256") != attempt["attempt_input_sha256"]
    ):
        raise HandoffContractError("prompt binding belongs to a different attempt input")
    outgoing_count = _require_non_negative_int(
        outgoing_change_count, "outgoing_change_count"
    )
    outgoing_bytes = _require_non_negative_int(
        outgoing_total_upsert_bytes, "outgoing_total_upsert_bytes"
    )
    change_policy = contract["change_policy"]
    if outgoing_count > change_policy["max_changes"]:
        raise HandoffContractError("outgoing changes exceed the task change-count limit")
    if outgoing_bytes > change_policy["max_total_upsert_bytes"]:
        raise HandoffContractError("outgoing changes exceed the task byte limit")
    return seal_attempt_output(
        task_id=attempt["task_id"],
        attempt_id=attempt["attempt_id"],
        route_step_index=attempt["route_step_index"],
        collaboration_contract_sha256=contract["contract_sha256"],
        attempt_input_sha256=attempt["attempt_input_sha256"],
        prompt_binding_sha256=binding["binding_sha256"],
        execution_receipt_sha256=execution_receipt_sha256,
        report_sha256=report_sha256,
        report_size_bytes=report_size_bytes,
        outgoing_change_manifest_sha256=outgoing_change_manifest_sha256,
        outgoing_change_count=outgoing_count,
        outgoing_total_upsert_bytes=outgoing_bytes,
    )


def seal_attempt_output(
    *,
    task_id: str,
    attempt_id: str,
    route_step_index: int,
    collaboration_contract_sha256: str,
    attempt_input_sha256: str,
    prompt_binding_sha256: str,
    execution_receipt_sha256: str,
    report_sha256: str,
    report_size_bytes: int,
    outgoing_change_manifest_sha256: str,
    outgoing_change_count: int,
    outgoing_total_upsert_bytes: int,
) -> dict[str, Any]:
    """Seal already-normalized scheduler receipts into one immutable output."""

    try:
        exact_task_id = validate_task_id(task_id)
    except SecurityValidationError as exc:
        raise HandoffContractError(str(exc)) from exc
    exact_attempt_id = _require_identifier(attempt_id, "attempt id", _ATTEMPT_ID_RE)
    step_index = _require_non_negative_int(route_step_index, "route step index")
    outgoing_count = _require_non_negative_int(
        outgoing_change_count, "outgoing change count"
    )
    outgoing_bytes = _require_non_negative_int(
        outgoing_total_upsert_bytes, "outgoing total upsert bytes"
    )
    body = {
        "schema_version": 1,
        "kind": ATTEMPT_OUTPUT_KIND,
        "task_id": exact_task_id,
        "attempt_id": exact_attempt_id,
        "route_step_index": step_index,
        "collaboration_contract_sha256": _require_sha256(
            collaboration_contract_sha256, "collaboration contract hash"
        ),
        "attempt_input_sha256": _require_sha256(
            attempt_input_sha256, "attempt input hash"
        ),
        "prompt_binding_sha256": _require_sha256(
            prompt_binding_sha256, "prompt binding hash"
        ),
        "execution_receipt_sha256": _require_sha256(
            execution_receipt_sha256, "execution_receipt_sha256"
        ),
        "report_receipt": {
            "sha256": _require_sha256(report_sha256, "report_sha256"),
            "size_bytes": _require_positive_int(report_size_bytes, "report_size_bytes"),
        },
        "outgoing_changes": {
            "manifest_sha256": _require_sha256(
                outgoing_change_manifest_sha256,
                "outgoing_change_manifest_sha256",
            ),
            "change_count": outgoing_count,
            "total_upsert_bytes": outgoing_bytes,
        },
    }
    return validate_attempt_output(_with_self_hash(body, "attempt_output_sha256"))


def validate_attempt_output(value: Mapping[str, Any]) -> dict[str, Any]:
    output = _validate_self_hash(
        value, kind=ATTEMPT_OUTPUT_KIND, hash_field="attempt_output_sha256"
    )
    _require_exact_keys(
        output,
        {
            "schema_version",
            "kind",
            "task_id",
            "attempt_id",
            "route_step_index",
            "collaboration_contract_sha256",
            "attempt_input_sha256",
            "prompt_binding_sha256",
            "execution_receipt_sha256",
            "report_receipt",
            "outgoing_changes",
            "attempt_output_sha256",
        },
        "attempt output",
    )
    try:
        validate_task_id(output.get("task_id"))
    except SecurityValidationError as exc:
        raise HandoffContractError(str(exc)) from exc
    _require_identifier(output.get("attempt_id"), "attempt id", _ATTEMPT_ID_RE)
    _require_non_negative_int(output.get("route_step_index"), "route step index")
    for field_name in (
        "collaboration_contract_sha256",
        "attempt_input_sha256",
        "prompt_binding_sha256",
        "execution_receipt_sha256",
    ):
        _require_sha256(output.get(field_name), field_name)
    report = _canonical_mapping(output.get("report_receipt"), "report receipt")
    _require_exact_keys(report, {"sha256", "size_bytes"}, "report receipt")
    _require_sha256(report.get("sha256"), "report hash")
    _require_positive_int(report.get("size_bytes"), "report size")
    outgoing = _canonical_mapping(output.get("outgoing_changes"), "outgoing changes")
    _require_exact_keys(
        outgoing,
        {"manifest_sha256", "change_count", "total_upsert_bytes"},
        "outgoing changes",
    )
    _require_sha256(outgoing.get("manifest_sha256"), "outgoing manifest hash")
    _require_non_negative_int(outgoing.get("change_count"), "outgoing change count")
    _require_non_negative_int(outgoing.get("total_upsert_bytes"), "outgoing upsert bytes")
    return output


def build_handoff_capsule(
    *,
    collaboration_contract: Mapping[str, Any],
    attempt_input: Mapping[str, Any],
    attempt_output: Mapping[str, Any],
    leader_result: Mapping[str, Any],
    handoff_text: str,
) -> dict[str, Any]:
    """Seal one leader-rejected attempt into a bounded successor-visible capsule."""

    contract = validate_collaboration_contract(collaboration_contract)
    attempt = validate_attempt_input(attempt_input, collaboration_contract=contract)
    output = validate_attempt_output(attempt_output)
    if attempt.get("collaboration_contract_sha256") != contract["contract_sha256"]:
        raise HandoffContractError("attempt input belongs to a different collaboration contract")
    if (
        output.get("collaboration_contract_sha256") != contract["contract_sha256"]
        or output.get("attempt_input_sha256") != attempt["attempt_input_sha256"]
        or output.get("attempt_id") != attempt["attempt_id"]
        or output.get("route_step_index") != attempt["route_step_index"]
    ):
        raise HandoffContractError("attempt output belongs to a different attempt input")
    result = _canonical_mapping(leader_result, "leader_result")
    if result.get("evidence_schema_version") != _RESULT_EVIDENCE_SCHEMA:
        raise HandoffContractError("leader rejection is not trusted result evidence v3")
    rejected_status = result.get("status")
    if rejected_status not in {"failed", "escalate"}:
        raise HandoffContractError("handoff requires an explicit leader-rejected failed/escalate result")
    if result.get("accepted_by_leader") is not False:
        raise HandoffContractError("handoff requires accepted_by_leader=false")
    if result.get("attempt_id") != attempt["attempt_id"]:
        raise HandoffContractError("leader rejection belongs to a different attempt")
    if result.get("task_id") != contract["task_id"]:
        raise HandoffContractError("leader rejection belongs to a different task")
    if result.get("attempt_output_sha256") != output["attempt_output_sha256"]:
        raise HandoffContractError("leader rejection does not bind the sealed attempt output")
    if (
        result.get("report_sha256") != output["report_receipt"]["sha256"]
        or result.get("report_size") != output["report_receipt"]["size_bytes"]
    ):
        raise HandoffContractError("leader rejection report does not match the sealed attempt output")
    leader_result_id = _require_identifier(
        result.get("id"), "leader result id", _RESULT_ID_RE
    )
    leader_result_sha256 = _sha256(_canonical_json_bytes(result))
    if not isinstance(handoff_text, str):
        raise HandoffContractError("handoff_text must be text")
    if any(
        (ord(character) < 32 and character not in {"\n", "\t"})
        or ord(character) == 127
        for character in handoff_text
    ):
        raise HandoffContractError("handoff text contains forbidden control characters")
    if (
        _PROMPT_MAGIC.rstrip(b"\n").decode("ascii") in handoff_text
        or _TASK_PROMPT_DELIMITER.strip().decode("ascii") in handoff_text
    ):
        raise HandoffContractError("handoff text contains reserved CostMarshal prompt framing")
    handoff_bytes = handoff_text.encode("utf-8", errors="strict")
    token_policy = contract["token_policy"]
    if not handoff_bytes or len(handoff_bytes) > token_policy["max_handoff_bytes"]:
        raise HandoffContractError("handoff text is empty or exceeds its immutable byte limit")
    if len(handoff_bytes) > token_policy["handoff_output_reserve_tokens"]:
        raise HandoffContractError("handoff text exceeds the conservative output token reserve")
    if (
        (2 * len(handoff_bytes)) + token_policy["prompt_framing_reserve_tokens"]
        > token_policy["continuation_input_reserve_tokens"]
    ):
        raise HandoffContractError("handoff text exceeds the conservative continuation input reserve")
    predecessor = attempt.get("predecessor_handoff")
    body = {
        "schema_version": 1,
        "kind": HANDOFF_CAPSULE_KIND,
        "task_id": contract["task_id"],
        "attempt_id": attempt["attempt_id"],
        "route_step_index": attempt["route_step_index"],
        "collaboration_contract_sha256": contract["contract_sha256"],
        "attempt_input_sha256": attempt["attempt_input_sha256"],
        "attempt_output_sha256": output["attempt_output_sha256"],
        "previous_capsule_sha256": (
            predecessor.get("capsule_sha256") if isinstance(predecessor, Mapping) else None
        ),
        "leader_rejection": {
            "result_id": leader_result_id,
            "status": rejected_status,
            "accepted_by_leader": False,
            "evidence_schema_version": _RESULT_EVIDENCE_SCHEMA,
            "evidence_sha256": leader_result_sha256,
        },
        "report_receipt": json.loads(_canonical_json_bytes(output["report_receipt"])),
        "handoff": {
            "encoding": "utf-8",
            "text": handoff_text,
            "size_bytes": len(handoff_bytes),
            "sha256": _sha256(handoff_bytes),
            "token_upper_bound": len(handoff_bytes),
        },
        "outgoing_changes": json.loads(_canonical_json_bytes(output["outgoing_changes"])),
    }
    return _with_self_hash(body, "capsule_sha256")


def validate_handoff_capsule(
    value: Mapping[str, Any],
    *,
    trusted_leader_result: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    capsule = _validate_self_hash(
        value, kind=HANDOFF_CAPSULE_KIND, hash_field="capsule_sha256"
    )
    _require_exact_keys(
        capsule,
        {
            "schema_version",
            "kind",
            "task_id",
            "attempt_id",
            "route_step_index",
            "collaboration_contract_sha256",
            "attempt_input_sha256",
            "attempt_output_sha256",
            "previous_capsule_sha256",
            "leader_rejection",
            "report_receipt",
            "handoff",
            "outgoing_changes",
            "capsule_sha256",
        },
        "handoff capsule",
    )
    try:
        validate_task_id(capsule.get("task_id"))
    except SecurityValidationError as exc:
        raise HandoffContractError(str(exc)) from exc
    _require_identifier(capsule.get("attempt_id"), "attempt id", _ATTEMPT_ID_RE)
    _require_non_negative_int(capsule.get("route_step_index"), "route step index")
    for field_name in (
        "collaboration_contract_sha256",
        "attempt_input_sha256",
        "attempt_output_sha256",
    ):
        _require_sha256(capsule.get(field_name), field_name)
    previous_capsule = capsule.get("previous_capsule_sha256")
    if previous_capsule is not None:
        _require_sha256(previous_capsule, "previous capsule hash")
    rejection = _canonical_mapping(capsule.get("leader_rejection"), "leader rejection")
    if rejection.get("accepted_by_leader") is not False or rejection.get("status") not in {
        "failed",
        "escalate",
    }:
        raise HandoffContractError("handoff is not bound to an explicit leader rejection")
    _require_exact_keys(
        rejection,
        {
            "result_id",
            "status",
            "accepted_by_leader",
            "evidence_schema_version",
            "evidence_sha256",
        },
        "leader rejection",
    )
    if rejection.get("evidence_schema_version") != _RESULT_EVIDENCE_SCHEMA:
        raise HandoffContractError("handoff leader result evidence schema is invalid")
    _require_sha256(rejection.get("evidence_sha256"), "leader result evidence hash")
    _require_identifier(rejection.get("result_id"), "result id", _RESULT_ID_RE)
    report = _canonical_mapping(capsule.get("report_receipt"), "report receipt")
    _require_exact_keys(report, {"sha256", "size_bytes"}, "report receipt")
    _require_sha256(report.get("sha256"), "report hash")
    _require_positive_int(report.get("size_bytes"), "report size")
    handoff = _canonical_mapping(capsule.get("handoff"), "handoff")
    _require_exact_keys(
        handoff,
        {"encoding", "text", "size_bytes", "sha256", "token_upper_bound"},
        "handoff",
    )
    if handoff.get("encoding") != "utf-8" or not isinstance(handoff.get("text"), str):
        raise HandoffContractError("handoff text encoding is invalid")
    payload = handoff["text"].encode("utf-8", errors="strict")
    if handoff.get("size_bytes") != len(payload) or handoff.get("token_upper_bound") != len(payload):
        raise HandoffContractError("handoff byte/token counters are inconsistent")
    if handoff.get("sha256") != _sha256(payload):
        raise HandoffContractError("handoff text receipt is invalid")
    if any(
        (ord(character) < 32 and character not in {"\n", "\t"})
        or ord(character) == 127
        for character in handoff["text"]
    ):
        raise HandoffContractError("handoff text contains forbidden control characters")
    outgoing = _canonical_mapping(capsule.get("outgoing_changes"), "outgoing changes")
    _require_exact_keys(
        outgoing,
        {"manifest_sha256", "change_count", "total_upsert_bytes"},
        "outgoing changes",
    )
    _require_sha256(outgoing.get("manifest_sha256"), "outgoing manifest hash")
    _require_non_negative_int(outgoing.get("change_count"), "outgoing change count")
    _require_non_negative_int(outgoing.get("total_upsert_bytes"), "outgoing upsert bytes")
    if trusted_leader_result is not None:
        result = _canonical_mapping(trusted_leader_result, "trusted leader result")
        if (
            result.get("evidence_schema_version") != _RESULT_EVIDENCE_SCHEMA
            or _sha256(_canonical_json_bytes(result)) != rejection["evidence_sha256"]
            or result.get("id") != rejection["result_id"]
            or result.get("task_id") != capsule["task_id"]
            or result.get("attempt_id") != capsule["attempt_id"]
            or result.get("attempt_output_sha256") != capsule["attempt_output_sha256"]
            or result.get("status") != rejection["status"]
            or result.get("accepted_by_leader") is not False
            or result.get("report_sha256") != report["sha256"]
            or result.get("report_size") != report["size_bytes"]
        ):
            raise HandoffContractError("handoff capsule does not match trusted leader result evidence")
    return capsule


def build_apply_preview_contract(
    *,
    collaboration_contract: Mapping[str, Any],
    accepted_attempt_input: Mapping[str, Any],
    accepted_attempt_output: Mapping[str, Any],
    accepted_leader_result: Mapping[str, Any],
    expected_source_head_sha: str,
    patch_sha256: str,
    patch_size_bytes: int,
    candidate_tree_sha: str,
) -> dict[str, Any]:
    """Create the exact compare-and-swap contract shown before opt-in apply."""

    contract = validate_collaboration_contract(collaboration_contract)
    attempt = validate_attempt_input(
        accepted_attempt_input, collaboration_contract=contract
    )
    output = validate_attempt_output(accepted_attempt_output)
    if attempt.get("collaboration_contract_sha256") != contract["contract_sha256"]:
        raise HandoffContractError("accepted attempt belongs to a different collaboration contract")
    if (
        output.get("collaboration_contract_sha256") != contract["contract_sha256"]
        or output.get("attempt_input_sha256") != attempt["attempt_input_sha256"]
        or output.get("attempt_id") != attempt["attempt_id"]
    ):
        raise HandoffContractError("accepted output belongs to a different attempt input")
    result = _canonical_mapping(accepted_leader_result, "accepted_leader_result")
    if result.get("evidence_schema_version") != _RESULT_EVIDENCE_SCHEMA:
        raise HandoffContractError("accepted result is not trusted result evidence v3")
    if (
        result.get("attempt_id") != attempt["attempt_id"]
        or result.get("task_id") != contract["task_id"]
        or result.get("status") != "done"
        or result.get("accepted_by_leader") is not True
        or result.get("attempt_output_sha256") != output["attempt_output_sha256"]
        or result.get("report_sha256") != output["report_receipt"]["sha256"]
        or result.get("report_size") != output["report_receipt"]["size_bytes"]
    ):
        raise HandoffContractError(
            "apply preview requires a done result explicitly accepted for this attempt"
        )
    leader_result_id = _require_identifier(
        result.get("id"), "leader_result_id", _RESULT_ID_RE
    )
    leader_result_sha256 = _sha256(_canonical_json_bytes(result))
    body = {
        "schema_version": 1,
        "kind": APPLY_PREVIEW_KIND,
        "task_id": contract["task_id"],
        "collaboration_contract_sha256": contract["contract_sha256"],
        "accepted_attempt_id": attempt["attempt_id"],
        "accepted_attempt_input_sha256": attempt["attempt_input_sha256"],
        "accepted_attempt_output_sha256": output["attempt_output_sha256"],
        "accepted_leader_result": {
            "result_id": leader_result_id,
            "evidence_schema_version": _RESULT_EVIDENCE_SCHEMA,
            "evidence_sha256": leader_result_sha256,
        },
        "base_sha": contract["base_sha"],
        "expected_source_head_sha": _require_git_oid(
            expected_source_head_sha, "expected_source_head_sha"
        ),
        "cumulative_changes": json.loads(_canonical_json_bytes(output["outgoing_changes"])),
        "patch_receipt": {
            "sha256": _require_sha256(patch_sha256, "patch_sha256"),
            "size_bytes": _require_non_negative_int(patch_size_bytes, "patch_size_bytes"),
        },
        "candidate_tree_sha": _require_git_oid(candidate_tree_sha, "candidate_tree_sha"),
        "apply_policy": {
            "requires_clean_worktree": True,
            "expected_head_compare_and_swap": True,
            "conflict_policy": "fail",
            "leader_opt_in_required": True,
        },
    }
    return _with_self_hash(body, "preview_sha256")


def validate_apply_preview_contract(
    value: Mapping[str, Any],
    *,
    collaboration_contract: Mapping[str, Any] | None = None,
    accepted_attempt_output: Mapping[str, Any] | None = None,
    trusted_leader_result: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    preview = _validate_self_hash(
        value, kind=APPLY_PREVIEW_KIND, hash_field="preview_sha256"
    )
    _require_exact_keys(
        preview,
        {
            "schema_version",
            "kind",
            "task_id",
            "collaboration_contract_sha256",
            "accepted_attempt_id",
            "accepted_attempt_input_sha256",
            "accepted_attempt_output_sha256",
            "accepted_leader_result",
            "base_sha",
            "expected_source_head_sha",
            "cumulative_changes",
            "patch_receipt",
            "candidate_tree_sha",
            "apply_policy",
            "preview_sha256",
        },
        "apply preview",
    )
    policy = _canonical_mapping(preview.get("apply_policy"), "apply policy")
    if policy != {
        "conflict_policy": "fail",
        "expected_head_compare_and_swap": True,
        "leader_opt_in_required": True,
        "requires_clean_worktree": True,
    }:
        raise HandoffContractError("apply policy is not fail-closed")
    try:
        validate_task_id(preview.get("task_id"))
    except SecurityValidationError as exc:
        raise HandoffContractError(str(exc)) from exc
    _require_git_oid(preview.get("base_sha"), "base_sha")
    _require_git_oid(preview.get("expected_source_head_sha"), "expected source head")
    _require_git_oid(preview.get("candidate_tree_sha"), "candidate tree")
    _require_sha256(preview.get("collaboration_contract_sha256"), "collaboration contract hash")
    _require_sha256(preview.get("accepted_attempt_input_sha256"), "accepted attempt input hash")
    _require_sha256(preview.get("accepted_attempt_output_sha256"), "accepted attempt output hash")
    _require_identifier(preview.get("accepted_attempt_id"), "accepted attempt id", _ATTEMPT_ID_RE)
    result = _canonical_mapping(preview.get("accepted_leader_result"), "accepted leader result")
    _require_exact_keys(
        result,
        {"result_id", "evidence_schema_version", "evidence_sha256"},
        "accepted leader result",
    )
    _require_identifier(result.get("result_id"), "accepted leader result id", _RESULT_ID_RE)
    if result.get("evidence_schema_version") != _RESULT_EVIDENCE_SCHEMA:
        raise HandoffContractError("accepted leader result evidence schema is invalid")
    _require_sha256(result.get("evidence_sha256"), "accepted result evidence hash")
    changes = _canonical_mapping(preview.get("cumulative_changes"), "cumulative changes")
    _require_exact_keys(
        changes,
        {"manifest_sha256", "change_count", "total_upsert_bytes"},
        "cumulative changes",
    )
    _require_sha256(changes.get("manifest_sha256"), "cumulative change manifest hash")
    _require_non_negative_int(changes.get("change_count"), "cumulative change count")
    _require_non_negative_int(changes.get("total_upsert_bytes"), "cumulative upsert bytes")
    patch = _canonical_mapping(preview.get("patch_receipt"), "patch receipt")
    _require_exact_keys(patch, {"sha256", "size_bytes"}, "patch receipt")
    _require_sha256(patch.get("sha256"), "patch hash")
    _require_non_negative_int(patch.get("size_bytes"), "patch size")
    if collaboration_contract is not None:
        contract = validate_collaboration_contract(collaboration_contract)
        if (
            preview.get("task_id") != contract["task_id"]
            or preview.get("collaboration_contract_sha256") != contract["contract_sha256"]
            or preview.get("base_sha") != contract["base_sha"]
        ):
            raise HandoffContractError("apply preview drifts from its collaboration contract")
    if accepted_attempt_output is not None:
        output = validate_attempt_output(accepted_attempt_output)
        if (
            output.get("attempt_id") != preview["accepted_attempt_id"]
            or output.get("attempt_input_sha256")
            != preview["accepted_attempt_input_sha256"]
            or output.get("attempt_output_sha256")
            != preview["accepted_attempt_output_sha256"]
            or output.get("outgoing_changes") != changes
        ):
            raise HandoffContractError("apply preview drifts from the accepted attempt output")
    if trusted_leader_result is not None:
        trusted = _canonical_mapping(trusted_leader_result, "trusted leader result")
        if (
            trusted.get("evidence_schema_version") != _RESULT_EVIDENCE_SCHEMA
            or trusted.get("id") != result["result_id"]
            or _sha256(_canonical_json_bytes(trusted)) != result["evidence_sha256"]
            or trusted.get("task_id") != preview["task_id"]
            or trusted.get("attempt_id") != preview["accepted_attempt_id"]
            or trusted.get("attempt_output_sha256")
            != preview["accepted_attempt_output_sha256"]
            or trusted.get("status") != "done"
            or trusted.get("accepted_by_leader") is not True
        ):
            raise HandoffContractError("apply preview does not match trusted accepted result evidence")
    return preview


__all__ = [
    "APPLY_PREVIEW_KIND",
    "ATTEMPT_INPUT_KIND",
    "ATTEMPT_OUTPUT_KIND",
    "COLLABORATION_PHASE_TRANSITIONS",
    "HANDOFF_CAPSULE_KIND",
    "HandoffContractError",
    "HandoffLimits",
    "PROMPT_BINDING_KIND",
    "TASK_CONTRACT_KIND",
    "build_apply_preview_contract",
    "build_attempt_input_contract",
    "build_attempt_output_contract",
    "build_bound_prompt_bytes",
    "build_collaboration_contract",
    "build_handoff_capsule",
    "build_prompt_binding",
    "seal_attempt_output",
    "validate_apply_preview_contract",
    "validate_attempt_input",
    "validate_attempt_phase_transition",
    "validate_attempt_output",
    "validate_collaboration_contract",
    "validate_collaboration_phase_transition",
    "validate_handoff_capsule",
    "validate_prompt_binding",
]
