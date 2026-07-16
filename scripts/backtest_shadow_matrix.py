#!/usr/bin/env python3
"""Offline blind shadow-matrix backtest for CostMarshal release evidence.

The harness never calls a provider, reads provider credentials, or synthesizes
missing outcomes. Release scope requires an externally trusted detached
signature over the exact bytes of a previously collected matrix in which every
task has independently blind-reviewed low/medium/high outcomes.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path
import random
import re
import shutil
import subprocess
import sys
import tempfile
from typing import Any


SCHEMA_VERSION = 1
TIERS = ("low", "medium", "high")
TIER_RANK = {tier: index for index, tier in enumerate(TIERS)}
SHA256 = re.compile(r"sha256:[0-9a-f]{64}")
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from costmarshal_v2.routing import (  # noqa: E402
    RoutingValidationError,
    decide_route,
    validate_provider_catalog,
)


POLICY_MANIFEST_SCHEMA_VERSION = 1
POLICY_ROUTING_ENGINE = "costmarshal_v2.routing.decide_route"
ATTESTATION_NAMESPACE = "costmarshal-backtest-v1"


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(
        json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False, allow_nan=False) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def bytes_sha256(value: bytes) -> str:
    return "sha256:" + hashlib.sha256(value).hexdigest()


def canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )


def canonical_sha256(value: Any) -> str:
    return "sha256:" + hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def canonical_equal(left: Any, right: Any) -> bool:
    try:
        return canonical_json(left) == canonical_json(right)
    except (TypeError, ValueError):
        return False


def current_git_sha() -> str | None:
    try:
        return subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=ROOT, text=True, stderr=subprocess.DEVNULL
        ).strip()
    except (OSError, subprocess.CalledProcessError):
        return None


def verify_external_attestation(
    dataset_bytes: bytes,
    *,
    allowed_signers: Path | None,
    signature: Path | None,
    signer_identity: str | None,
    allow_unsigned_test_fixture: bool,
) -> tuple[bool, str, dict[str, Any], list[str]]:
    details: dict[str, Any] = {
        "namespace": ATTESTATION_NAMESPACE,
        "signer_identity": signer_identity,
        "allowed_signers_sha256": None,
        "signature_sha256": None,
        "ssh_keygen": None,
    }
    if allow_unsigned_test_fixture:
        return False, "test-only", details, []

    blockers: list[str] = []
    allowed_signers_bytes: bytes | None = None
    signature_bytes: bytes | None = None
    if allowed_signers is None or not allowed_signers.is_file():
        blockers.append("external attestation requires an existing --allowed-signers file")
    else:
        try:
            allowed_signers_bytes = allowed_signers.read_bytes()
        except OSError:
            blockers.append("external attestation allowed_signers file is unreadable")
        else:
            details["allowed_signers_sha256"] = bytes_sha256(allowed_signers_bytes)
    if signature is None or not signature.is_file():
        blockers.append("external attestation requires an existing --attestation-signature file")
    else:
        try:
            signature_bytes = signature.read_bytes()
        except OSError:
            blockers.append("external attestation signature file is unreadable")
        else:
            details["signature_sha256"] = bytes_sha256(signature_bytes)
    if (
        not isinstance(signer_identity, str)
        or not signer_identity
        or len(signer_identity) > 256
        or any(character.isspace() or ord(character) < 32 for character in signer_identity)
    ):
        blockers.append("external attestation requires a bounded whitespace-free --signer-identity")
    executable = shutil.which("ssh-keygen")
    details["ssh_keygen"] = executable
    if executable is None:
        blockers.append("ssh-keygen is unavailable; detached attestation cannot be verified")
    if blockers:
        return False, "blocked", details, blockers

    assert allowed_signers is not None
    assert signature is not None
    assert signer_identity is not None
    assert executable is not None
    assert allowed_signers_bytes is not None
    assert signature_bytes is not None
    try:
        with tempfile.TemporaryDirectory(prefix="costmarshal-backtest-attestation-") as raw:
            verification_root = Path(raw)
            frozen_allowed_signers = verification_root / "allowed_signers"
            frozen_signature = verification_root / "dataset.sig"
            frozen_allowed_signers.write_bytes(allowed_signers_bytes)
            frozen_signature.write_bytes(signature_bytes)
            completed = subprocess.run(
                [
                    executable,
                    "-Y",
                    "verify",
                    "-f",
                    str(frozen_allowed_signers),
                    "-I",
                    signer_identity,
                    "-n",
                    ATTESTATION_NAMESPACE,
                    "-s",
                    str(frozen_signature),
                ],
                input=dataset_bytes,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
                timeout=30,
            )
    except (OSError, subprocess.TimeoutExpired) as exc:
        blockers.append(f"external attestation verification could not run: {type(exc).__name__}")
        return False, "blocked", details, blockers
    details["verify_returncode"] = completed.returncode
    if completed.returncode != 0:
        blockers.append("dataset detached signature is invalid for the trusted signer and namespace")
        return False, "blocked", details, blockers
    return True, "release", details, []


def parse_time(value: Any, label: str, blockers: list[str]) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        blockers.append(f"{label} must be an RFC3339 timestamp")
        return None
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00" if value.endswith("Z") else value)
    except ValueError:
        blockers.append(f"{label} must be an RFC3339 timestamp")
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        blockers.append(f"{label} must include a timezone")
        return None
    return parsed.astimezone(timezone.utc)


def finite_non_negative(value: Any) -> bool:
    return (
        not isinstance(value, bool)
        and isinstance(value, (int, float))
        and math.isfinite(float(value))
        and float(value) >= 0
    )


def valid_chain(value: Any, floor: str) -> bool:
    if not isinstance(value, list) or not value or any(tier not in TIER_RANK for tier in value):
        return False
    ranks = [TIER_RANK[tier] for tier in value]
    return ranks == sorted(set(ranks)) and ranks[0] >= TIER_RANK[floor]


def validate_policy_manifest(dataset: dict[str, Any], blockers: list[str]) -> dict[str, Any] | None:
    manifest = dataset.get("policy_manifest")
    claimed_hash = dataset.get("policy_manifest_sha256")
    if not isinstance(manifest, dict):
        blockers.append("policy_manifest must be an object")
        return None
    if manifest.get("schema_version") != POLICY_MANIFEST_SCHEMA_VERSION:
        blockers.append(f"policy_manifest.schema_version must be {POLICY_MANIFEST_SCHEMA_VERSION}")
    if manifest.get("routing_engine") != POLICY_ROUTING_ENGINE:
        blockers.append(f"policy_manifest.routing_engine must be {POLICY_ROUTING_ENGINE}")

    manifest_body = {key: value for key, value in manifest.items() if key != "manifest_sha256"}
    try:
        computed_hash = canonical_sha256(manifest_body)
    except (TypeError, ValueError):
        blockers.append("policy_manifest must contain canonical finite JSON values")
        return None
    if manifest.get("manifest_sha256") != computed_hash:
        blockers.append("policy_manifest.manifest_sha256 does not match canonical manifest content")
    if claimed_hash != computed_hash:
        blockers.append("policy_manifest_sha256 does not match the embedded policy manifest")

    git_sha = current_git_sha()
    if not git_sha or manifest.get("git_sha") != git_sha:
        blockers.append("policy_manifest.git_sha must match the checked-out CostMarshal commit")
    locked_at = parse_time(manifest.get("locked_at"), "policy_manifest.locked_at", blockers)

    catalog: dict[str, Any] | None = None
    try:
        catalog = validate_provider_catalog(manifest.get("provider_catalog"))
    except (RoutingValidationError, TypeError, ValueError) as exc:
        blockers.append(f"policy_manifest.provider_catalog is invalid: {exc}")
    if catalog is not None:
        enabled_counts = {
            tier: sum(
                row.get("enabled") is True and row.get("tier") == tier
                for row in catalog["providers"]
            )
            for tier in TIERS
        }
        if enabled_counts != {tier: 1 for tier in TIERS}:
            blockers.append(
                "policy_manifest.provider_catalog must contain exactly one enabled provider per tier "
                "because shadow outcomes are keyed by tier"
            )
    history = manifest.get("history")
    if not isinstance(history, list) or any(not isinstance(row, dict) for row in history):
        blockers.append("policy_manifest.history must be a list of objects")
        history = None
    task_routes = manifest.get("task_routes")
    if not isinstance(task_routes, dict):
        blockers.append("policy_manifest.task_routes must be an object keyed by task_id")
        task_routes = None
    if catalog is None or history is None or task_routes is None or locked_at is None:
        return None
    return {
        "hash": computed_hash,
        "git_sha": git_sha,
        "locked_at": locked_at,
        "catalog": catalog,
        "history": history,
        "task_routes": task_routes,
    }


def _route_request(
    value: Any,
    label: str,
    blockers: list[str],
) -> dict[str, str | None] | None:
    if not isinstance(value, dict) or set(value) != {"requested_provider_id", "requested_tier"}:
        blockers.append(
            f"{label} must contain exactly requested_provider_id and requested_tier"
        )
        return None
    provider_id = value.get("requested_provider_id")
    tier = value.get("requested_tier")
    if provider_id is not None and (not isinstance(provider_id, str) or not provider_id.strip()):
        blockers.append(f"{label}.requested_provider_id must be null or non-empty")
        return None
    if tier is not None and (not isinstance(tier, str) or not tier.strip()):
        blockers.append(f"{label}.requested_tier must be null or non-empty")
        return None
    return {"requested_provider_id": provider_id, "requested_tier": tier}


def recompute_policy_chains(
    task_id: str,
    context: dict[str, Any],
    blockers: list[str],
) -> tuple[list[str], list[str], str] | None:
    label = f"policy_manifest.task_routes.{task_id}"
    entry = context["task_routes"].get(task_id)
    if not isinstance(entry, dict):
        blockers.append(f"{label} must be an object")
        return None
    routing_task = entry.get("task")
    if not isinstance(routing_task, dict):
        blockers.append(f"{label}.task must be an object")
        return None
    input_tokens = entry.get("input_tokens")
    cached_input_tokens = entry.get("cached_input_tokens", 0)
    output_tokens = entry.get("output_tokens")
    if type(input_tokens) is not int or input_tokens < 0:
        blockers.append(f"{label}.input_tokens must be a non-negative integer")
        return None
    if type(output_tokens) is not int or output_tokens < 0:
        blockers.append(f"{label}.output_tokens must be a non-negative integer")
        return None
    if type(cached_input_tokens) is not int or cached_input_tokens < 0:
        blockers.append(f"{label}.cached_input_tokens must be a non-negative integer")
        return None
    route_now = entry.get("now")
    parsed_route_now = parse_time(route_now, f"{label}.now", blockers)
    if parsed_route_now is None:
        return None
    if parsed_route_now != context["locked_at"]:
        blockers.append(f"{label}.now must equal the pre-review policy lock time")
        return None
    candidate_request = _route_request(entry.get("candidate_request"), f"{label}.candidate_request", blockers)
    baseline_request = _route_request(entry.get("baseline_request"), f"{label}.baseline_request", blockers)
    if candidate_request is None or baseline_request is None:
        return None
    try:
        candidate = decide_route(
            routing_task,
            context["catalog"],
            history=context["history"],
            input_tokens=input_tokens,
            cached_input_tokens=cached_input_tokens,
            output_tokens=output_tokens,
            now=route_now,
            **candidate_request,
        )
        baseline_task = dict(routing_task)
        baseline_task.pop("min_success_probability", None)
        baseline = decide_route(
            baseline_task,
            context["catalog"],
            history=context["history"],
            input_tokens=input_tokens,
            cached_input_tokens=cached_input_tokens,
            output_tokens=output_tokens,
            now=route_now,
            **baseline_request,
        )
    except (RoutingValidationError, TypeError, ValueError) as exc:
        blockers.append(f"{label} cannot be recomputed by CostMarshal: {exc}")
        return None

    candidate_ids = list(candidate.planned_provider_ids)
    baseline_ids = list(baseline.planned_provider_ids)
    if entry.get("candidate_provider_ids") != candidate_ids:
        blockers.append(f"{label}.candidate_provider_ids differs from CostMarshal output")
    if entry.get("baseline_provider_ids") != baseline_ids:
        blockers.append(f"{label}.baseline_provider_ids differs from CostMarshal output")
    providers = {row["provider_id"]: row for row in context["catalog"]["providers"]}
    try:
        candidate_tiers = [providers[provider_id]["tier"] for provider_id in candidate_ids]
        baseline_tiers = [providers[provider_id]["tier"] for provider_id in baseline_ids]
    except KeyError:
        blockers.append(f"{label} references a provider outside the bound catalog")
        return None
    return candidate_tiers, baseline_tiers, candidate.tier_floor


def validate_dataset(
    dataset: Any,
    *,
    min_tasks: int,
    min_tasks_per_floor: int,
) -> tuple[list[dict[str, Any]], list[str], dict[str, int], dict[str, Any] | None]:
    blockers: list[str] = []
    if not isinstance(dataset, dict):
        return [], ["dataset root must be an object"], {tier: 0 for tier in TIERS}, None
    if dataset.get("schema_version") != SCHEMA_VERSION:
        blockers.append(f"schema_version must be {SCHEMA_VERSION}")
    if dataset.get("real_provider_shadow_matrix") is not True:
        blockers.append("real_provider_shadow_matrix must be true")
    if dataset.get("synthetic") is not False:
        blockers.append("synthetic must be false; synthetic fixtures are never release evidence")

    attestation = dataset.get("collection_attestation")
    required_attestations = (
        "real_provider_calls_completed",
        "credentialed_collection_completed",
        "provider_call_ids_hashed",
    )
    if not isinstance(attestation, dict):
        blockers.append("collection_attestation must be an object")
    else:
        for field in required_attestations:
            if attestation.get(field) is not True:
                blockers.append(f"collection_attestation.{field} must be true")

    blinding = dataset.get("blinding")
    required_blinding = (
        "reviewer_blinded_to_provider",
        "reviewer_blinded_to_tier",
        "outcomes_unblinded_after_policy_lock",
    )
    if not isinstance(blinding, dict):
        blockers.append("blinding must be an object")
    else:
        for field in required_blinding:
            if blinding.get(field) is not True:
                blockers.append(f"blinding.{field} must be true")

    policy_context = validate_policy_manifest(dataset, blockers)

    tasks = dataset.get("tasks")
    if not isinstance(tasks, list):
        return [], blockers + ["tasks must be a list"], {tier: 0 for tier in TIERS}, policy_context
    if len(tasks) < min_tasks:
        blockers.append(f"task_count must be at least {min_tasks}; found {len(tasks)}")

    normalized: list[dict[str, Any]] = []
    task_ids: set[str] = set()
    task_input_hashes: set[str] = set()
    provider_call_hashes: set[str] = set()
    blind_ids: set[str] = set()
    floor_counts = {tier: 0 for tier in TIERS}
    for index, raw in enumerate(tasks):
        label = f"tasks[{index}]"
        if not isinstance(raw, dict):
            blockers.append(f"{label} must be an object")
            continue
        task_id = raw.get("task_id")
        if not isinstance(task_id, str) or not task_id.strip():
            blockers.append(f"{label}.task_id must be non-empty")
            continue
        if task_id in task_ids:
            blockers.append(f"duplicate task_id: {task_id}")
            continue
        task_ids.add(task_id)
        task_input_hash = raw.get("task_input_sha256")
        if not isinstance(task_input_hash, str) or not SHA256.fullmatch(task_input_hash):
            blockers.append(f"{label}.task_input_sha256 must be sha256:<64 lowercase hex>")
        elif task_input_hash in task_input_hashes:
            blockers.append(f"duplicate task_input_sha256: {task_input_hash}")
        else:
            task_input_hashes.add(task_input_hash)
        floor = raw.get("safety_floor")
        if floor not in TIER_RANK:
            blockers.append(f"{label}.safety_floor must be low, medium, or high")
            continue
        floor_counts[floor] += 1
        candidate_chain = raw.get("candidate_chain")
        baseline_chain = raw.get("baseline_chain")
        if not valid_chain(candidate_chain, floor):
            blockers.append(f"{label}.candidate_chain must be strictly increasing and respect {floor} floor")
        if not valid_chain(baseline_chain, floor):
            blockers.append(f"{label}.baseline_chain must be strictly increasing and respect {floor} floor")

        policy_chains_valid = False
        if policy_context is not None:
            recomputed = recompute_policy_chains(task_id, policy_context, blockers)
            if recomputed is not None:
                expected_candidate, expected_baseline, expected_floor = recomputed
                policy_chains_valid = True
                if floor != expected_floor:
                    blockers.append(
                        f"{label}.safety_floor differs from CostMarshal policy output {expected_floor}"
                    )
                    policy_chains_valid = False
                if candidate_chain != expected_candidate:
                    blockers.append(f"{label}.candidate_chain differs from CostMarshal policy output")
                    policy_chains_valid = False
                if baseline_chain != expected_baseline:
                    blockers.append(f"{label}.baseline_chain differs from CostMarshal policy output")
                    policy_chains_valid = False

        policy_locked = parse_time(raw.get("policy_locked_at"), f"{label}.policy_locked_at", blockers)
        review_completed = parse_time(raw.get("review_completed_at"), f"{label}.review_completed_at", blockers)
        if policy_locked and review_completed and policy_locked > review_completed:
            blockers.append(f"{label} policy must be locked before blind review completes")
        if policy_context is not None and policy_locked != policy_context["locked_at"]:
            blockers.append(f"{label}.policy_locked_at must equal policy_manifest.locked_at")
            policy_chains_valid = False

        task_budget = raw.get("task_budget_cny")
        if task_budget is not None and not finite_non_negative(task_budget):
            blockers.append(f"{label}.task_budget_cny must be null or non-negative")

        outcomes = raw.get("outcomes")
        normalized_outcomes: dict[str, dict[str, Any]] = {}
        if not isinstance(outcomes, dict) or set(outcomes) != set(TIERS):
            blockers.append(f"{label}.outcomes must contain exactly low, medium, and high")
        else:
            for tier in TIERS:
                outcome = outcomes[tier]
                outcome_label = f"{label}.outcomes.{tier}"
                if not isinstance(outcome, dict):
                    blockers.append(f"{outcome_label} must be an object")
                    continue
                forbidden = {"provider_id", "model", "reviewer_visible_tier"}.intersection(outcome)
                if forbidden:
                    blockers.append(f"{outcome_label} exposes forbidden identity fields: {sorted(forbidden)}")
                blind_id = outcome.get("blind_result_id")
                if not isinstance(blind_id, str) or not blind_id.strip():
                    blockers.append(f"{outcome_label}.blind_result_id must be non-empty")
                elif blind_id in blind_ids:
                    blockers.append(f"duplicate blind_result_id: {blind_id}")
                else:
                    blind_ids.add(blind_id)
                call_hash = outcome.get("provider_call_id_hash")
                if not isinstance(call_hash, str) or not SHA256.fullmatch(call_hash):
                    blockers.append(f"{outcome_label}.provider_call_id_hash must be sha256:<64 lowercase hex>")
                elif call_hash in provider_call_hashes:
                    blockers.append(f"duplicate provider_call_id_hash: {call_hash}")
                else:
                    provider_call_hashes.add(call_hash)
                accepted = outcome.get("accepted")
                if type(accepted) is not bool:
                    blockers.append(f"{outcome_label}.accepted must be boolean")
                quality = outcome.get("quality_score")
                if type(quality) is not int or quality not in {1, 2, 3, 4, 5}:
                    blockers.append(f"{outcome_label}.quality_score must be 1-5")
                cost = outcome.get("actual_cost_cny")
                if not finite_non_negative(cost):
                    blockers.append(f"{outcome_label}.actual_cost_cny must be non-negative")
                if type(accepted) is bool and finite_non_negative(cost):
                    normalized_outcomes[tier] = {
                        "accepted": accepted,
                        "quality_score": quality,
                        "actual_cost_cny": float(cost),
                    }
        if (
            valid_chain(candidate_chain, floor)
            and valid_chain(baseline_chain, floor)
            and policy_chains_valid
            and len(normalized_outcomes) == 3
        ):
            normalized.append(
                {
                    "task_id": task_id,
                    "safety_floor": floor,
                    "candidate_chain": list(candidate_chain),
                    "baseline_chain": list(baseline_chain),
                    "task_budget_cny": None if task_budget is None else float(task_budget),
                    "outcomes": normalized_outcomes,
                }
            )
    for tier, count in floor_counts.items():
        if count < min_tasks_per_floor:
            blockers.append(
                f"safety_floor {tier} requires at least {min_tasks_per_floor} tasks; found {count}"
            )
    if policy_context is not None and set(policy_context["task_routes"]) != task_ids:
        blockers.append("policy_manifest.task_routes keys must exactly match dataset task_ids")
    if policy_context is not None:
        history_task_ids = {
            str(row.get("task_id"))
            for row in policy_context["history"]
            if row.get("task_id") is not None
        }
        if history_task_ids.intersection(task_ids):
            blockers.append("policy_manifest.history must not contain outcomes from study tasks")
    return normalized, blockers, floor_counts, policy_context


def evaluate_chain(task: dict[str, Any], chain_name: str) -> tuple[bool, float, int]:
    accepted = False
    total_cost = 0.0
    attempts = 0
    for tier in task[chain_name]:
        outcome = task["outcomes"][tier]
        total_cost += float(outcome["actual_cost_cny"])
        attempts += 1
        if outcome["accepted"]:
            accepted = True
            break
    return accepted, round(total_cost, 9), attempts


def evaluate_task(task: dict[str, Any]) -> dict[str, Any]:
    candidate_accepted, candidate_cost, candidate_attempts = evaluate_chain(task, "candidate_chain")
    baseline_accepted, baseline_cost, baseline_attempts = evaluate_chain(task, "baseline_chain")
    task_budget = task.get("task_budget_cny")
    return {
        "task_id": task["task_id"],
        "safety_floor": task["safety_floor"],
        "candidate_accepted": candidate_accepted,
        "candidate_cost_cny": candidate_cost,
        "candidate_attempts": candidate_attempts,
        "baseline_accepted": baseline_accepted,
        "baseline_cost_cny": baseline_cost,
        "baseline_attempts": baseline_attempts,
        "task_budget_overrun": task_budget is not None and candidate_cost > float(task_budget),
    }


def aggregate(rows: list[dict[str, Any]]) -> dict[str, Any]:
    count = len(rows)
    candidate_accepted = sum(row["candidate_accepted"] for row in rows)
    baseline_accepted = sum(row["baseline_accepted"] for row in rows)
    candidate_cost = sum(float(row["candidate_cost_cny"]) for row in rows)
    baseline_cost = sum(float(row["baseline_cost_cny"]) for row in rows)
    candidate_cpa = candidate_cost / candidate_accepted if candidate_accepted else None
    baseline_cpa = baseline_cost / baseline_accepted if baseline_accepted else None
    cost_ratio = (
        candidate_cpa / baseline_cpa
        if candidate_cpa is not None and baseline_cpa not in {None, 0.0}
        else None
    )
    return {
        "task_count": count,
        "candidate_accepted": candidate_accepted,
        "baseline_accepted": baseline_accepted,
        "candidate_acceptance_rate": candidate_accepted / count if count else None,
        "baseline_acceptance_rate": baseline_accepted / count if count else None,
        "acceptance_delta": (candidate_accepted - baseline_accepted) / count if count else None,
        "candidate_total_cost_cny": round(candidate_cost, 9),
        "baseline_total_cost_cny": round(baseline_cost, 9),
        "candidate_cost_per_accepted_cny": None if candidate_cpa is None else round(candidate_cpa, 9),
        "baseline_cost_per_accepted_cny": None if baseline_cpa is None else round(baseline_cpa, 9),
        "cost_ratio": None if cost_ratio is None else round(cost_ratio, 9),
    }


def percentile(values: list[float], probability: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    position = (len(ordered) - 1) * probability
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return ordered[lower]
    weight = position - lower
    return ordered[lower] * (1.0 - weight) + ordered[upper] * weight


def bootstrap(rows: list[dict[str, Any]], *, samples: int, seed: int) -> dict[str, Any]:
    rng = random.Random(seed)
    acceptance_deltas: list[float] = []
    cost_ratios: list[float] = []
    for _ in range(samples):
        sample = [rows[rng.randrange(len(rows))] for _ in rows]
        metrics = aggregate(sample)
        acceptance_deltas.append(float(metrics["acceptance_delta"]))
        if metrics["cost_ratio"] is not None:
            cost_ratios.append(float(metrics["cost_ratio"]))
    return {
        "seed": seed,
        "samples": samples,
        "valid_cost_ratio_samples": len(cost_ratios),
        "acceptance_delta_ci_lower": percentile(acceptance_deltas, 0.025),
        "acceptance_delta_ci_upper": percentile(acceptance_deltas, 0.975),
        "cost_ratio_ci_lower": percentile(cost_ratios, 0.025),
        "cost_ratio_ci_upper": percentile(cost_ratios, 0.975),
    }


def base_report(*, dataset: Path | None, dataset_hash: str | None) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "generated_at": now_iso(),
        "git_sha": current_git_sha(),
        "status": "blocked",
        "evidence_scope": "blocked",
        "external_attestation_verified": False,
        "external_attestation": None,
        "real_provider_shadow_matrix": False,
        "synthetic": None,
        "dataset": str(dataset.resolve()) if dataset else None,
        "dataset_sha256": dataset_hash,
        "credentials_used_by_harness": False,
        "provider_calls_made_by_harness": 0,
        "task_count": 0,
        "cost_ratio_ci_upper": None,
        "acceptance_delta_ci_lower": None,
        "budget_overruns": 0,
        "blockers": [],
    }


def evaluation_config(
    args: argparse.Namespace,
    *,
    git_sha: str | None,
    dataset_hash: str,
    policy_manifest_hash: str,
    evidence_scope: str,
    external_attestation: dict[str, Any],
) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "git_sha": git_sha,
        "dataset_sha256": dataset_hash,
        "policy_manifest_sha256": policy_manifest_hash,
        "evidence_scope": evidence_scope,
        "external_attestation_verified": evidence_scope == "release",
        "signer_identity": external_attestation.get("signer_identity"),
        "allowed_signers_sha256": external_attestation.get("allowed_signers_sha256"),
        "attestation_signature_sha256": external_attestation.get("signature_sha256"),
        "min_tasks": args.min_tasks,
        "min_tasks_per_floor": args.min_tasks_per_floor,
        "bootstrap_samples": args.bootstrap_samples,
        "seed": args.seed,
        "max_cost_ratio_ci_upper": args.max_cost_ratio_upper,
        "acceptance_noninferiority_margin": args.acceptance_noninferiority_margin,
        "project_budget_cny": args.project_budget_cny,
    }


def build_checkpoint_payload(
    *,
    rows: list[dict[str, Any]],
    tasks: list[dict[str, Any]],
    dataset_hash: str,
    git_sha: str | None,
    policy_manifest_hash: str,
    config: dict[str, Any],
    config_hash: str,
) -> dict[str, Any]:
    return {
        "schema_version": 2,
        "git_sha": git_sha,
        "dataset_sha256": dataset_hash,
        "policy_manifest_sha256": policy_manifest_hash,
        "config": config,
        "config_sha256": config_hash,
        "processed_tasks": len(rows),
        "total_tasks": len(tasks),
        "complete": len(rows) == len(tasks),
        "rows": rows,
    }


def emit(report: dict[str, Any], output: Path) -> int:
    atomic_write_json(output, report)
    print(json.dumps(report, indent=2, sort_keys=True, ensure_ascii=False, allow_nan=False))
    return 0 if report["status"] == "pass" else 1 if report["status"] == "fail" else 2


def run(args: argparse.Namespace) -> int:
    dataset_path: Path | None = args.dataset
    output: Path = args.output
    checkpoint: Path = args.checkpoint
    if dataset_path is None or not dataset_path.is_file():
        report = base_report(dataset=dataset_path, dataset_hash=None)
        report["blockers"] = [
            "real blind shadow-matrix dataset is absent",
            "credentialed real-provider collection attestation is unavailable",
            "the offline harness will not fabricate data or call providers",
        ]
        return emit(report, output)

    try:
        dataset_bytes = dataset_path.read_bytes()
    except OSError as exc:
        report = base_report(dataset=dataset_path, dataset_hash=None)
        report["blockers"] = [f"dataset is unreadable: {exc}"]
        return emit(report, output)
    dataset_hash = bytes_sha256(dataset_bytes)
    report = base_report(dataset=dataset_path, dataset_hash=dataset_hash)
    attestation_verified, evidence_scope, attestation, attestation_blockers = (
        verify_external_attestation(
            dataset_bytes,
            allowed_signers=args.allowed_signers,
            signature=args.attestation_signature,
            signer_identity=args.signer_identity,
            allow_unsigned_test_fixture=args.allow_unsigned_test_fixture,
        )
    )
    report.update(
        {
            "evidence_scope": evidence_scope,
            "external_attestation_verified": attestation_verified,
            "external_attestation": attestation,
        }
    )
    if attestation_blockers:
        report["blockers"] = attestation_blockers
        return emit(report, output)
    try:
        dataset = json.loads(dataset_bytes.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        report["blockers"] = [f"dataset is unreadable: {exc}"]
        return emit(report, output)

    tasks, blockers, floor_counts, policy_context = validate_dataset(
        dataset,
        min_tasks=args.min_tasks,
        min_tasks_per_floor=args.min_tasks_per_floor,
    )
    report.update(
        {
            "study_id": dataset.get("study_id") if isinstance(dataset, dict) else None,
            "synthetic": dataset.get("synthetic") if isinstance(dataset, dict) else None,
            "floor_counts": floor_counts,
            "task_count": len(dataset.get("tasks") or []) if isinstance(dataset, dict) else 0,
        }
    )
    if blockers:
        report["blockers"] = blockers
        return emit(report, output)

    assert policy_context is not None
    git_sha = current_git_sha()
    policy_manifest_hash = policy_context["hash"]
    config = evaluation_config(
        args,
        git_sha=git_sha,
        dataset_hash=dataset_hash,
        policy_manifest_hash=policy_manifest_hash,
        evidence_scope=evidence_scope,
        external_attestation=attestation,
    )
    config_hash = canonical_sha256(config)
    report.update(
        {
            "policy_manifest_sha256": policy_manifest_hash,
            "evaluation_config": config,
            "evaluation_config_sha256": config_hash,
        }
    )

    checkpoint_payload: dict[str, Any]
    if checkpoint.is_file():
        try:
            checkpoint_payload = json.loads(checkpoint.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            report["status"] = "fail"
            report["blockers"] = [f"checkpoint is unreadable: {exc}"]
            return emit(report, output)
        checkpoint_blockers: list[str] = []
        if not isinstance(checkpoint_payload, dict):
            checkpoint_blockers.append("checkpoint root must be an object")
            checkpoint_payload = {}
        if checkpoint_payload.get("schema_version") != 2:
            checkpoint_blockers.append("checkpoint schema_version must be 2")
        bindings = {
            "dataset_sha256": dataset_hash,
            "git_sha": git_sha,
            "policy_manifest_sha256": policy_manifest_hash,
            "config_sha256": config_hash,
        }
        for field, expected in bindings.items():
            if checkpoint_payload.get(field) != expected:
                checkpoint_blockers.append(f"checkpoint {field} does not match current evaluation")
        stored_config = checkpoint_payload.get("config")
        if not isinstance(stored_config, dict) or not canonical_equal(stored_config, config):
            checkpoint_blockers.append("checkpoint config does not match current evaluation")
        rows = checkpoint_payload.get("rows")
        if not isinstance(rows, list):
            checkpoint_blockers.append("checkpoint rows must be a list")
            rows = []
        if len(rows) > len(tasks):
            checkpoint_blockers.append("checkpoint contains more rows than the dataset")
        processed_tasks = checkpoint_payload.get("processed_tasks")
        total_tasks = checkpoint_payload.get("total_tasks")
        complete_flag = checkpoint_payload.get("complete")
        if type(processed_tasks) is not int or processed_tasks != len(rows):
            checkpoint_blockers.append("checkpoint processed_tasks does not match rows")
        if type(total_tasks) is not int or total_tasks != len(tasks):
            checkpoint_blockers.append("checkpoint total_tasks does not match the dataset")
        if type(complete_flag) is not bool or complete_flag != (len(rows) == len(tasks)):
            checkpoint_blockers.append("checkpoint complete flag is inconsistent")
        for index, row in enumerate(rows):
            expected_row = evaluate_task(tasks[index]) if index < len(tasks) else None
            if expected_row is None or not canonical_equal(row, expected_row):
                checkpoint_blockers.append(
                    f"checkpoint row {index} does not match recomputation from the dataset"
                )
                break
        if checkpoint_blockers:
            report["status"] = "fail"
            report["blockers"] = checkpoint_blockers
            return emit(report, output)
    else:
        rows = []

    remaining = tasks[len(rows) :]
    if args.max_tasks is not None:
        remaining = remaining[: args.max_tasks]
    for task in remaining:
        rows.append(evaluate_task(task))
        if len(rows) % args.checkpoint_every == 0:
            atomic_write_json(
                checkpoint,
                build_checkpoint_payload(
                    rows=rows,
                    tasks=tasks,
                    dataset_hash=dataset_hash,
                    git_sha=git_sha,
                    policy_manifest_hash=policy_manifest_hash,
                    config=config,
                    config_hash=config_hash,
                ),
            )
    complete = len(rows) == len(tasks)
    atomic_write_json(
        checkpoint,
        build_checkpoint_payload(
            rows=rows,
            tasks=tasks,
            dataset_hash=dataset_hash,
            git_sha=git_sha,
            policy_manifest_hash=policy_manifest_hash,
            config=config,
            config_hash=config_hash,
        ),
    )
    if not complete:
        report.update(
            {
                "real_provider_shadow_matrix": attestation_verified,
                "processed_tasks": len(rows),
                "blockers": ["checkpoint incomplete; resume with the same dataset and checkpoint"],
                "checkpoint": str(checkpoint.resolve()),
            }
        )
        return emit(report, output)

    metrics = aggregate(rows)
    bootstrap_metrics = bootstrap(rows, samples=args.bootstrap_samples, seed=args.seed)
    task_budget_overruns = sum(bool(row["task_budget_overrun"]) for row in rows)
    candidate_total = float(metrics["candidate_total_cost_cny"])
    project_budget_overrun = (
        args.project_budget_cny is not None and candidate_total > args.project_budget_cny
    )
    budget_overruns = task_budget_overruns + int(project_budget_overrun)
    acceptance_lower = bootstrap_metrics["acceptance_delta_ci_lower"]
    cost_upper = bootstrap_metrics["cost_ratio_ci_upper"]
    threshold_failures: list[str] = []
    minimum_valid_cost_samples = math.ceil(args.bootstrap_samples * 0.95)
    if bootstrap_metrics["valid_cost_ratio_samples"] < minimum_valid_cost_samples:
        threshold_failures.append(
            "cost ratio bootstrap coverage is insufficient: "
            f"{bootstrap_metrics['valid_cost_ratio_samples']} valid samples, "
            f"requires at least {minimum_valid_cost_samples}"
        )
    if cost_upper is None:
        threshold_failures.append("cost ratio bootstrap CI is unavailable")
    elif cost_upper >= args.max_cost_ratio_upper:
        threshold_failures.append(
            f"cost ratio CI upper {cost_upper:.6f} is not below {args.max_cost_ratio_upper:.6f}"
        )
    if acceptance_lower is None:
        threshold_failures.append("acceptance delta bootstrap CI is unavailable")
    elif acceptance_lower < -args.acceptance_noninferiority_margin:
        threshold_failures.append(
            f"acceptance delta CI lower {acceptance_lower:.6f} is below "
            f"{-args.acceptance_noninferiority_margin:.6f}"
        )
    if budget_overruns:
        threshold_failures.append(f"budget overruns must be zero; found {budget_overruns}")

    report.update(
        {
            "status": "fail" if threshold_failures else "pass",
            "real_provider_shadow_matrix": attestation_verified,
            "synthetic": False,
            "credentialed_collection_completed": attestation_verified,
            "processed_tasks": len(rows),
            "checkpoint": str(checkpoint.resolve()),
            "checkpoint_complete": True,
            "metrics": metrics,
            "bootstrap": bootstrap_metrics,
            "cost_ratio_ci_lower": bootstrap_metrics["cost_ratio_ci_lower"],
            "cost_ratio_ci_upper": cost_upper,
            "acceptance_delta_ci_lower": acceptance_lower,
            "acceptance_delta_ci_upper": bootstrap_metrics["acceptance_delta_ci_upper"],
            "task_budget_overruns": task_budget_overruns,
            "project_budget_overrun": project_budget_overrun,
            "budget_overruns": budget_overruns,
            "thresholds": {
                "max_cost_ratio_ci_upper": args.max_cost_ratio_upper,
                "acceptance_noninferiority_margin": args.acceptance_noninferiority_margin,
                "minimum_valid_cost_ratio_bootstrap_fraction": 0.95,
                "project_budget_cny": args.project_budget_cny,
            },
            "blockers": threshold_failures,
        }
    )
    return emit(report, output)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Evaluate an offline real-provider blind shadow matrix; never calls providers"
    )
    parser.add_argument("--dataset", type=Path, help="Attested real-provider shadow-matrix JSON")
    parser.add_argument("--output", type=Path, default=Path("artifacts/backtest-report.json"))
    parser.add_argument("--checkpoint", type=Path, default=Path("artifacts/backtest-checkpoint.json"))
    parser.add_argument("--min-tasks", type=int, default=200)
    parser.add_argument("--min-tasks-per-floor", type=int, default=20)
    parser.add_argument("--bootstrap-samples", type=int, default=5000)
    parser.add_argument("--seed", type=int, default=20260716)
    parser.add_argument("--checkpoint-every", type=int, default=25)
    parser.add_argument("--max-tasks", type=int, help="Process at most this many additional tasks")
    parser.add_argument("--max-cost-ratio-upper", type=float, default=1.0)
    parser.add_argument("--acceptance-noninferiority-margin", type=float, default=0.02)
    parser.add_argument("--project-budget-cny", type=float)
    parser.add_argument(
        "--allowed-signers",
        type=Path,
        help="OpenSSH allowed_signers file trusted to attest the exact dataset bytes",
    )
    parser.add_argument(
        "--attestation-signature",
        type=Path,
        help="Detached ssh-keygen -Y signature over the exact dataset bytes",
    )
    parser.add_argument("--signer-identity", help="Identity matched against the allowed_signers file")
    parser.add_argument(
        "--allow-unsigned-test-fixture",
        action="store_true",
        help="Evaluate unsigned test data as test-only; never emits release evidence",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.min_tasks < 200:
        parser.error("--min-tasks cannot be below 200 for release evidence")
    if args.min_tasks_per_floor < 1:
        parser.error("--min-tasks-per-floor must be positive")
    if args.bootstrap_samples < 200:
        parser.error("--bootstrap-samples must be at least 200")
    if args.checkpoint_every < 1:
        parser.error("--checkpoint-every must be positive")
    if args.max_tasks is not None and args.max_tasks < 0:
        parser.error("--max-tasks must be non-negative")
    if not math.isfinite(args.max_cost_ratio_upper) or not 0 < args.max_cost_ratio_upper:
        parser.error("--max-cost-ratio-upper must be positive")
    if (
        not math.isfinite(args.acceptance_noninferiority_margin)
        or not 0 <= args.acceptance_noninferiority_margin <= 1
    ):
        parser.error("--acceptance-noninferiority-margin must be between 0 and 1")
    if args.project_budget_cny is not None and (
        not math.isfinite(args.project_budget_cny) or args.project_budget_cny < 0
    ):
        parser.error("--project-budget-cny must be non-negative")
    if args.allow_unsigned_test_fixture and any(
        value is not None
        for value in (args.allowed_signers, args.attestation_signature, args.signer_identity)
    ):
        parser.error(
            "--allow-unsigned-test-fixture cannot be combined with external attestation options"
        )
    return run(args)


if __name__ == "__main__":
    raise SystemExit(main())
