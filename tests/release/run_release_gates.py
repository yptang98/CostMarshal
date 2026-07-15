#!/usr/bin/env python3
"""Machine-readable non-beta release gate aggregator.

This script intentionally exits 2 while mandatory external evidence is blocked.
It never treats the existence of beta code or a unit test as proof of a real
provider backtest, an enabled OCI execution adapter, or exactly-once runtime
effects.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import re
import subprocess
import sys
from typing import Any, Callable


ROOT = Path(__file__).resolve().parents[2]
EVIDENCE_POLICY_PATH = ROOT / "release" / "evidence-policy.json"
sys.path.insert(0, str(ROOT))

from costmarshal_v2 import __version__  # noqa: E402
from costmarshal_v2.cli import build_parser  # noqa: E402
from runtime_evidence_contract import (  # noqa: E402
    REQUIRED_RUNTIME_CRASH_POINTS,
    REQUIRED_RUNTIME_RECOVERY_SCENARIOS,
    RUNTIME_EVIDENCE_TESTS,
)


REQUIRED_LOCAL_TESTS = (
    "tests/unit_test.py",
    "tests/smoke_test.py",
    "tests/local_backend_contract_test.py",
    "tests/tmux_contract_test.py",
    "tests/model_rotation_contract_test.py",
    "tests/three_tier_routing_test.py",
    "tests/route_oracle_test.py",
    "tests/backtest_harness_test.py",
    "tests/pricing_metadata_test.py",
    "tests/control_store_test.py",
    "tests/transactional_scheduler_test.py",
    "tests/runtime_effect_store_test.py",
    "tests/runtime_effect_scheduler_test.py",
    "tests/worker_isolation_test.py",
    "tests/worker_execution_adapter_test.py",
    "tests/container_worker_contract_test.py",
    "tests/oci_actor_runner_test.py",
    "tests/isolation_scheduler_gate_test.py",
    "tests/actor_fencing_test.py",
    "tests/actor_crash_recovery_test.py",
    "tests/runtime_recovery_reliability_test.py",
    "tests/pid_identity_test.py",
    "tests/required_credential_preflight_test.py",
    "tests/security_contract_test.py",
    "tests/actor_security_contract_test.py",
    "tests/reliability_contract_test.py",
    "tests/release_gate_test.py",
    "tests/budget_contract_test.py",
    "tests/budget_reconciliation_oracle_test.py",
    "tests/historical_state_migration_test.py",
    "tests/archmarshal_compat_test.py",
    "tests/profile_config_test.py",
    "tests/concurrency_contract_test.py",
    "scripts/install_smoke_test.py",
)
REQUIRED_TESTS = REQUIRED_LOCAL_TESTS + (
    "tests/release/run_local_test_evidence.py",
    "tests/release/run_runtime_effect_evidence.py",
    "tests/oci_live_evidence.py",
    "tests/release/run_release_gates.py",
)
AUXILIARY_DOC_OPTIONS = {"--reproduce-evidence"}
REPRODUCED_ARTIFACTS = {
    "local_test_suite": "artifacts/local-test-report.json",
    "transactional_runtime_effects": "artifacts/runtime-effect-report.json",
    "real_provider_backtest": "artifacts/backtest-report.json",
    "oci_execution_adapter": "artifacts/oci-attestation.json",
}


def gate(gate_id: str, status: str, summary: str, **details: Any) -> dict[str, Any]:
    if status not in {"pass", "fail", "blocked"}:
        raise ValueError(f"invalid gate status: {status}")
    return {
        "id": gate_id,
        "required": True,
        "status": status,
        "summary": summary,
        "details": details,
    }


def git_output(*args: str) -> str:
    return subprocess.check_output(
        ["git", *args], cwd=ROOT, text=True, encoding="utf-8", errors="replace"
    ).strip()


def cli_surface() -> tuple[set[str], set[str]]:
    parser = build_parser()
    commands: set[str] = set()
    options: set[str] = set()
    for action in parser._actions:
        options.update(action.option_strings)
        if isinstance(action, argparse._SubParsersAction):
            commands.update(action.choices)
            for child in action.choices.values():
                for child_action in child._actions:
                    options.update(child_action.option_strings)
    return commands, options


def documentation_contract() -> dict[str, Any]:
    commands, options = cli_surface()
    options.update(AUXILIARY_DOC_OPTIONS)
    docs = {
        str(path.relative_to(ROOT)).replace("\\", "/"): path.read_text(encoding="utf-8")
        for path in ROOT.rglob("*.md")
        if ".git" not in path.parts and "artifacts" not in path.parts
    }
    documented_commands: set[str] = set()
    documented_options: set[str] = set()
    for text in docs.values():
        documented_commands.update(re.findall(r"costmarshal\.py\s+([a-z][a-z0-9-]*)", text))
    for relative in ("README.md", "SKILL.md"):
        documented_options.update(re.findall(r"(?<![\w-])--[a-z][a-z0-9-]*", docs[relative]))
    missing_commands = sorted(documented_commands - commands)
    missing_options = sorted(documented_options - options)
    return gate(
        "docs_cli_contract",
        "pass" if not missing_commands and not missing_options else "fail",
        "Published Markdown command names and README/SKILL option names resolve against argparse.",
        documented_commands=sorted(documented_commands),
        documented_options=sorted(documented_options),
        missing_commands=missing_commands,
        missing_options=missing_options,
    )


def required_test_entries() -> dict[str, Any]:
    missing = [relative for relative in REQUIRED_TESTS if not (ROOT / relative).is_file()]
    return gate(
        "required_test_entries",
        "pass" if not missing else "fail",
        "Required verification entrypoints exist.",
        required=list(REQUIRED_TESTS),
        missing=missing,
    )


def load_evidence(relative: str) -> tuple[dict[str, Any] | None, str | None]:
    path = ROOT / relative
    if not path.is_file():
        return None, "evidence file is absent"
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return None, f"evidence is unreadable: {exc}"
    if not isinstance(payload, dict):
        return None, "evidence root must be an object"
    return payload, None


def load_release_policy() -> tuple[dict[str, Any] | None, list[str]]:
    try:
        payload = json.loads(EVIDENCE_POLICY_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return None, [f"release evidence policy is unreadable: {type(exc).__name__}"]
    blockers: list[str] = []
    if not isinstance(payload, dict) or payload.get("schema_version") != 1:
        return None, ["release evidence policy schema_version must be 1"]
    backtest = payload.get("backtest")
    oci = payload.get("oci")
    if not isinstance(backtest, dict):
        blockers.append("release evidence policy backtest section is missing")
    if not isinstance(oci, dict):
        blockers.append("release evidence policy OCI section is missing")
    return payload, blockers


def release_policy_gate() -> dict[str, Any]:
    policy, blockers = load_release_policy()
    configured: list[str] = []
    if policy is not None:
        backtest = policy.get("backtest") if isinstance(policy.get("backtest"), dict) else {}
        oci = policy.get("oci") if isinstance(policy.get("oci"), dict) else {}
        required = {
            "backtest.allowed_signers_sha256": backtest.get("allowed_signers_sha256"),
            "backtest.signer_identities": backtest.get("signer_identities"),
            "backtest.policy_manifest_sha256": backtest.get("policy_manifest_sha256"),
            "backtest.preregistered_commit": backtest.get("preregistered_commit"),
            "oci.worker_image": oci.get("worker_image"),
            "oci.provider_proxy_image_id": oci.get("provider_proxy_image_id"),
            "oci.provider_proxy_config_sha256": oci.get("provider_proxy_config_sha256"),
            "oci.provider_proxy_health_url_sha256": oci.get(
                "provider_proxy_health_url_sha256"
            ),
            "oci.provider_proxy_health_response_sha256": oci.get(
                "provider_proxy_health_response_sha256"
            ),
        }
        configured = [key for key, value in required.items() if value not in (None, "", [])]
        missing = sorted(set(required) - set(configured))
        blockers.extend(f"release evidence policy is not configured: {key}" for key in missing)
    return gate(
        "release_evidence_policy",
        "pass" if not blockers else "blocked",
        "Reviewed signer, preregistration, worker-image, and proxy trust roots are committed.",
        policy=str(EVIDENCE_POLICY_PATH.relative_to(ROOT)).replace("\\", "/"),
        configured=configured,
        blockers=blockers,
    )


def reproduction_contract(reproduction: dict[str, Any] | None) -> tuple[str, list[str], dict[str, str]]:
    if reproduction is None:
        return "blocked", ["evidence was not reproduced by this invocation"], {}
    blockers: list[str] = []
    if reproduction.get("status") != "pass":
        blockers.append("one or more evidence generators failed their fresh-artifact contract")
    rows = reproduction.get("commands")
    hashes: dict[str, str] = {}
    if not isinstance(rows, list):
        blockers.append("reproduction commands must be a list")
    else:
        seen: set[str] = set()
        for row in rows:
            if not isinstance(row, dict):
                blockers.append("reproduction command row is invalid")
                continue
            evidence_id = str(row.get("id") or "")
            artifact = str(row.get("artifact") or "")
            digest = str(row.get("artifact_sha256") or "")
            if evidence_id in seen:
                blockers.append(f"duplicate reproduced evidence id: {evidence_id}")
                continue
            seen.add(evidence_id)
            if REPRODUCED_ARTIFACTS.get(evidence_id) != artifact:
                blockers.append(f"unexpected reproduced artifact for {evidence_id}")
            if not re.fullmatch(r"[0-9a-f]{64}", digest):
                blockers.append(f"reproduced artifact hash is invalid for {evidence_id}")
            else:
                hashes[artifact] = digest
        if seen != set(REPRODUCED_ARTIFACTS):
            blockers.append("reproduction did not run the exact required evidence generator set")
    return ("pass" if not blockers else "fail"), blockers, hashes


def finite_number(payload: dict[str, Any], field: str) -> float | None:
    value = payload.get(field)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    result = float(value)
    return result if math.isfinite(result) else None


def plain_integer(payload: dict[str, Any], field: str) -> int | None:
    value = payload.get(field)
    return value if type(value) is int else None


def current_git_sha() -> str:
    try:
        return git_output("rev-parse", "HEAD")
    except (OSError, subprocess.CalledProcessError):
        return ""


def canonical_sha256(value: Any) -> str | None:
    try:
        encoded = json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError):
        return None
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def external_evidence_gate(
    gate_id: str,
    relative: str,
    validator: Callable[[dict[str, Any]], list[str]],
    summary: str,
    *,
    expected_sha256: str | None = None,
) -> dict[str, Any]:
    path = ROOT / relative
    try:
        raw = path.read_bytes()
        payload = json.loads(raw.decode("utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        return gate(
            gate_id,
            "blocked",
            summary,
            evidence=relative,
            blockers=[f"evidence is unavailable: {type(exc).__name__}"],
        )
    if not isinstance(payload, dict):
        return gate(
            gate_id,
            "blocked",
            summary,
            evidence=relative,
            blockers=["evidence root must be an object"],
        )
    actual_sha256 = hashlib.sha256(raw).hexdigest()
    blockers = validator(payload)
    integrity_mismatch = expected_sha256 is not None and actual_sha256 != expected_sha256
    if integrity_mismatch:
        blockers.insert(0, "evidence bytes differ from the fresh artifact produced by this invocation")
    evidence_status = payload.get("status")
    if integrity_mismatch or evidence_status == "fail":
        status = "fail"
    elif evidence_status == "blocked":
        status = "blocked"
    else:
        status = "pass" if not blockers else "fail"
    return gate(
        gate_id,
        status,
        summary,
        evidence=relative,
        blockers=blockers,
        evidence_status=evidence_status,
        evidence_sha256=actual_sha256,
        reproduced_sha256=expected_sha256,
    )


def _validate_backtest_release_policy(payload: dict[str, Any]) -> list[str]:
    policy, blockers = load_release_policy()
    if policy is None:
        return blockers
    backtest = policy.get("backtest") if isinstance(policy.get("backtest"), dict) else {}
    attestation = payload.get("external_attestation") if isinstance(payload.get("external_attestation"), dict) else {}
    if attestation.get("allowed_signers_sha256") != backtest.get("allowed_signers_sha256"):
        blockers.append("backtest signer allowlist is not the repository-approved trust root")
    identities = backtest.get("signer_identities")
    if not isinstance(identities, list) or not identities or attestation.get("signer_identity") not in identities:
        blockers.append("backtest signer identity is not repository-approved")
    if payload.get("policy_manifest_sha256") != backtest.get("policy_manifest_sha256"):
        blockers.append("backtest policy manifest was not preregistered in release policy")
    preregistered = str(backtest.get("preregistered_commit") or "")
    if not re.fullmatch(r"[0-9a-f]{40}", preregistered):
        blockers.append("backtest preregistration commit is missing or invalid")
        return blockers
    head = current_git_sha()
    ancestor = subprocess.run(
        ["git", "merge-base", "--is-ancestor", preregistered, head],
        cwd=ROOT,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    )
    if preregistered == head or ancestor.returncode != 0:
        blockers.append("backtest release policy must be committed before the evaluated release commit")
        return blockers
    try:
        prior_policy = json.loads(
            git_output("show", f"{preregistered}:release/evidence-policy.json")
        )
    except (subprocess.CalledProcessError, json.JSONDecodeError):
        blockers.append("preregistration commit does not contain a valid release evidence policy")
        return blockers
    prior_backtest = prior_policy.get("backtest") if isinstance(prior_policy, dict) else None
    if not isinstance(prior_backtest, dict) or any(
        prior_backtest.get(field) != backtest.get(field)
        for field in ("allowed_signers_sha256", "signer_identities", "policy_manifest_sha256")
    ):
        blockers.append("backtest trust root or policy manifest changed after preregistration")
    return blockers


def validate_backtest(payload: dict[str, Any]) -> list[str]:
    blockers: list[str] = []
    if payload.get("schema_version") != 1:
        blockers.append("schema_version must be 1")
    if payload.get("status") != "pass":
        blockers.append("status must be pass")
    if payload.get("evidence_scope") != "release":
        blockers.append("evidence_scope must be release")
    if payload.get("external_attestation_verified") is not True:
        blockers.append("external detached attestation must be verified")
    if payload.get("real_provider_shadow_matrix") is not True:
        blockers.append("real_provider_shadow_matrix must be true")
    if payload.get("synthetic") is not False:
        blockers.append("synthetic must be false")
    if payload.get("credentialed_collection_completed") is not True:
        blockers.append("credentialed_collection_completed must be true")
    if payload.get("credentials_used_by_harness") is not False:
        blockers.append("offline harness must not use credentials")
    if plain_integer(payload, "provider_calls_made_by_harness") != 0:
        blockers.append("offline harness must make zero provider calls")
    task_count = plain_integer(payload, "task_count")
    cost_upper = finite_number(payload, "cost_ratio_ci_upper")
    acceptance_lower = finite_number(payload, "acceptance_delta_ci_lower")
    budget_overruns = plain_integer(payload, "budget_overruns")
    if task_count is None or task_count < 200:
        blockers.append("task_count must be at least 200")
    if cost_upper is None or cost_upper >= 1.0:
        blockers.append("95% upper confidence bound for cost ratio must be below 1.0")
    if acceptance_lower is None or acceptance_lower < -0.02:
        blockers.append("acceptance non-inferiority lower bound must be at least -0.02")
    if budget_overruns != 0:
        blockers.append("budget_overruns must be zero")
    sha = current_git_sha()
    if payload.get("git_sha") != sha:
        blockers.append("evidence git_sha must match the checked-out commit")
    dataset_hash = payload.get("dataset_sha256")
    policy_hash = payload.get("policy_manifest_sha256")
    if not re.fullmatch(r"sha256:[0-9a-f]{64}", str(dataset_hash or "")):
        blockers.append("dataset_sha256 must be present and canonical")
    if not re.fullmatch(r"sha256:[0-9a-f]{64}", str(policy_hash or "")):
        blockers.append("policy_manifest_sha256 must be present and canonical")
    config = payload.get("evaluation_config")
    if not isinstance(config, dict):
        blockers.append("evaluation_config must be present")
    else:
        if config.get("git_sha") != sha:
            blockers.append("evaluation_config.git_sha must match the checked-out commit")
        if config.get("dataset_sha256") != dataset_hash:
            blockers.append("evaluation_config must bind the dataset hash")
        if config.get("policy_manifest_sha256") != policy_hash:
            blockers.append("evaluation_config must bind the policy manifest hash")
        if config.get("evidence_scope") != "release" or config.get("external_attestation_verified") is not True:
            blockers.append("evaluation_config must bind verified release evidence scope")
        if plain_integer(config, "min_tasks") is None or int(config["min_tasks"]) < 200:
            blockers.append("evaluation_config.min_tasks must be at least 200")
        if plain_integer(config, "min_tasks_per_floor") is None or int(config["min_tasks_per_floor"]) < 1:
            blockers.append("evaluation_config.min_tasks_per_floor must be positive")
        if plain_integer(config, "bootstrap_samples") is None or int(config["bootstrap_samples"]) < 200:
            blockers.append("evaluation_config.bootstrap_samples must be at least 200")
        if canonical_sha256(config) != payload.get("evaluation_config_sha256"):
            blockers.append("evaluation_config_sha256 must match canonical configuration")
    if payload.get("checkpoint_complete") is not True:
        blockers.append("checkpoint_complete must be true")
    if plain_integer(payload, "processed_tasks") != task_count:
        blockers.append("processed_tasks must equal task_count")
    floor_counts = payload.get("floor_counts")
    if not isinstance(floor_counts, dict) or any(
        type(floor_counts.get(tier)) is not int or floor_counts[tier] < 20
        for tier in ("low", "medium", "high")
    ):
        blockers.append("each safety floor must contain at least 20 tasks")
    if payload.get("blockers") != []:
        blockers.append("passing backtest evidence must have no blockers")
    attestation = payload.get("external_attestation")
    if not isinstance(attestation, dict):
        blockers.append("external_attestation must be present")
    else:
        if attestation.get("namespace") != "costmarshal-backtest-v1":
            blockers.append("external attestation namespace is invalid")
        signer = attestation.get("signer_identity")
        if not isinstance(signer, str) or not signer or any(character.isspace() for character in signer):
            blockers.append("external attestation signer identity is invalid")
        for field in ("allowed_signers_sha256", "signature_sha256"):
            if not re.fullmatch(r"sha256:[0-9a-f]{64}", str(attestation.get(field) or "")):
                blockers.append(f"external attestation {field} is invalid")
        if isinstance(config, dict):
            if config.get("signer_identity") != attestation.get("signer_identity"):
                blockers.append("evaluation_config must bind the attestation signer")
            if config.get("allowed_signers_sha256") != attestation.get("allowed_signers_sha256"):
                blockers.append("evaluation_config must bind the allowed-signers hash")
            if config.get("attestation_signature_sha256") != attestation.get("signature_sha256"):
                blockers.append("evaluation_config must bind the detached signature hash")
    bootstrap = payload.get("bootstrap")
    if not isinstance(bootstrap, dict):
        blockers.append("bootstrap evidence must be present")
    else:
        samples = plain_integer(bootstrap, "samples")
        valid_samples = plain_integer(bootstrap, "valid_cost_ratio_samples")
        if (
            samples is None
            or samples < 200
            or valid_samples is None
            or valid_samples < math.ceil(samples * 0.95)
        ):
            blockers.append("at least 95% of bootstrap samples must have a valid cost ratio")
    blockers.extend(_validate_backtest_release_policy(payload))
    return blockers


def validate_oci(payload: dict[str, Any]) -> list[str]:
    blockers: list[str] = []
    if payload.get("schema_version") != 1:
        blockers.append("schema_version must be 1")
    if payload.get("status") != "pass":
        blockers.append("status must be pass")
    if payload.get("adapter_enabled") is not True:
        blockers.append("adapter_enabled must be true")
    if payload.get("strong_isolation") is not True:
        blockers.append("strong_isolation must be true")
    if not re.fullmatch(r"[^@\s]+@sha256:[0-9a-f]{64}", str(payload.get("image") or "")):
        blockers.append("image must be digest pinned")
    escape_attempts = plain_integer(payload, "escape_attempts")
    escapes_succeeded = plain_integer(payload, "escapes_succeeded")
    if escape_attempts is None or escape_attempts <= 0:
        blockers.append("malicious escape attempts must be exercised")
    if escapes_succeeded != 0:
        blockers.append("escapes_succeeded must be zero")
    if payload.get("git_sha") != current_git_sha():
        blockers.append("evidence git_sha must match the checked-out commit")
    if payload.get("blockers") != []:
        blockers.append("passing OCI evidence must have no blockers")
    provider_proxy = payload.get("provider_proxy")
    network_id = None
    if not isinstance(provider_proxy, dict):
        blockers.append("a live provider-proxy topology must be verified")
    else:
        network_id = provider_proxy.get("network_id")
        if not re.fullmatch(r"[0-9a-f]{12,64}", str(network_id or "")):
            blockers.append("provider-proxy immutable network ID is invalid")
        if not isinstance(provider_proxy.get("container"), str) or not provider_proxy.get("container"):
            blockers.append("provider-proxy container identity is missing")
        if not re.fullmatch(r"[0-9a-f]{64}", str(provider_proxy.get("container_id") or "")):
            blockers.append("provider-proxy immutable container ID is invalid")
        if not re.fullmatch(r"[0-9a-f]{12,64}", str(provider_proxy.get("egress_network_id") or "")):
            blockers.append("provider-proxy verified egress network ID is invalid")
        if not re.fullmatch(r"sha256:[0-9a-f]{64}", str(provider_proxy.get("image_id") or "")):
            blockers.append("provider-proxy immutable image ID is invalid")
        if not re.fullmatch(r"sha256:[0-9a-f]{64}", str(provider_proxy.get("config_sha256") or "")):
            blockers.append("provider-proxy configuration hash is invalid")
        if not re.fullmatch(r"sha256:[0-9a-f]{64}", str(provider_proxy.get("health_url_sha256") or "")):
            blockers.append("provider-proxy health URL hash is invalid")
        if not re.fullmatch(r"sha256:[0-9a-f]{64}", str(provider_proxy.get("health_response_sha256") or "")):
            blockers.append("provider-proxy health response hash is invalid")
    attestation = payload.get("attestation")
    if not isinstance(attestation, dict):
        blockers.append("worker isolation attestation must be present")
    else:
        if attestation.get("schema") != "costmarshal-worker-isolation-attestation-v1":
            blockers.append("worker isolation attestation schema is invalid")
        if attestation.get("backend") not in {"docker", "podman"}:
            blockers.append("worker isolation backend is invalid")
        if attestation.get("strong_isolation") is not True or attestation.get("image") != payload.get("image"):
            blockers.append("worker isolation attestation does not bind the reviewed image")
        if attestation.get("probe_provenance") != "image-internal-digest-bound-scratch-no-network":
            blockers.append("preflight probe provenance must be scratch-only and networkless")
        network_policy = attestation.get("network_policy")
        if not isinstance(network_policy, dict) or any(
            network_policy.get(field) is not expected
            for field, expected in (("internal", True), ("trust_label", True), ("verified", True))
        ):
            blockers.append("provider-proxy network policy attestation is incomplete")
        elif network_policy.get("network_id") != network_id:
            blockers.append("worker attestation must bind the verified provider network ID")
    checks = payload.get("checks")
    required_checks = {
        "immutable_container_id_verified",
        "container_labels_verified",
        "container_image_verified",
        "container_command_verified",
        "container_user_verified",
        "container_security_options_verified",
        "container_network_attachment_verified",
        "mount_allowlist_excludes_runtime_and_aggregate",
        "runtime_hidden_ro_none",
        "runtime_hidden_rw_provider-proxy",
        "aggregate_secrets_hidden_ro_none",
        "aggregate_secrets_hidden_rw_provider-proxy",
        "engine_socket_hidden_ro_none",
        "engine_socket_hidden_rw_provider-proxy",
        "network_policy_none",
        "network_policy_provider-proxy",
        "proxy_allowlisted_request_provider-proxy",
        "symlink-output_rejected",
        "extra-output_rejected",
        "oversize-output_rejected",
    }
    if not isinstance(checks, list) or any(
        not isinstance(row, dict) or row.get("status") != "pass" for row in checks
    ):
        blockers.append("every live OCI check must pass")
    else:
        check_ids = {str(row.get("id") or "") for row in checks}
        missing = sorted(required_checks - check_ids)
        if missing:
            blockers.append("live OCI checks are incomplete: " + ", ".join(missing))
    provenance = payload.get("probe_provenance")
    if not isinstance(provenance, dict) or provenance.get("container_contract") != (
        "independent OCI engine inspect over a once-pinned local endpoint"
    ):
        blockers.append("independent engine-inspect provenance is missing")
    policy, policy_blockers = load_release_policy()
    blockers.extend(policy_blockers)
    if policy is not None:
        oci_policy = policy.get("oci") if isinstance(policy.get("oci"), dict) else {}
        if payload.get("image") != oci_policy.get("worker_image"):
            blockers.append("worker image is not the repository-approved release digest")
        if not isinstance(provider_proxy, dict):
            blockers.append("provider proxy evidence is unavailable for release policy validation")
        else:
            if provider_proxy.get("image_id") != oci_policy.get("provider_proxy_image_id"):
                blockers.append("provider proxy image ID is not repository-approved")
            if provider_proxy.get("config_sha256") != oci_policy.get("provider_proxy_config_sha256"):
                blockers.append("provider proxy configuration is not repository-approved")
            if provider_proxy.get("health_url_sha256") != oci_policy.get(
                "provider_proxy_health_url_sha256"
            ):
                blockers.append("provider proxy health URL is not repository-approved")
            if provider_proxy.get("health_response_sha256") != oci_policy.get(
                "provider_proxy_health_response_sha256"
            ):
                blockers.append("provider proxy health response is not repository-approved")
    return blockers


def validate_effect_worker(payload: dict[str, Any]) -> list[str]:
    blockers: list[str] = []
    if payload.get("schema_version") != 1:
        blockers.append("schema_version must be 1")
    if payload.get("status") != "pass":
        blockers.append("status must be pass")
    if payload.get("transactional_effect_worker") is not True:
        blockers.append("transactional_effect_worker must be true")
    crash_points = plain_integer(payload, "crash_points_tested")
    duplicate_calls = plain_integer(payload, "duplicate_provider_calls")
    orphan_effects = plain_integer(payload, "orphan_effects")
    missing_calls = plain_integer(payload, "missing_provider_calls")
    crash_point_values = payload.get("crash_points")
    recovery_scenarios = payload.get("recovery_scenarios")
    if (
        crash_points != len(REQUIRED_RUNTIME_CRASH_POINTS)
        or not isinstance(crash_point_values, list)
        or any(not isinstance(value, str) for value in crash_point_values)
        or set(crash_point_values) != set(REQUIRED_RUNTIME_CRASH_POINTS)
        or len(crash_point_values) != len(REQUIRED_RUNTIME_CRASH_POINTS)
    ):
        blockers.append("crash-point receipts must exactly match the runtime evidence contract")
    if (
        not isinstance(recovery_scenarios, list)
        or any(not isinstance(value, str) for value in recovery_scenarios)
        or set(recovery_scenarios) != set(REQUIRED_RUNTIME_RECOVERY_SCENARIOS)
        or len(recovery_scenarios) != len(REQUIRED_RUNTIME_RECOVERY_SCENARIOS)
    ):
        blockers.append("recovery-scenario receipts must exactly match the runtime evidence contract")
    if duplicate_calls != 0:
        blockers.append("duplicate_provider_calls must be zero")
    if missing_calls != 0:
        blockers.append("missing_provider_calls must be zero")
    if orphan_effects != 0:
        blockers.append("orphan_effects must be zero")
    results = payload.get("tests")
    if not isinstance(results, list) or [row.get("test") for row in results if isinstance(row, dict)] != list(
        RUNTIME_EVIDENCE_TESTS
    ) or any(not isinstance(row, dict) or plain_integer(row, "returncode") != 0 for row in results):
        blockers.append("runtime evidence test results are incomplete")
    receipts = payload.get("receipts")
    if not isinstance(receipts, list) or [row.get("test") for row in receipts if isinstance(row, dict)] != list(
        RUNTIME_EVIDENCE_TESTS
    ):
        blockers.append("runtime evidence receipts are incomplete")
    else:
        receipt_crash_points: set[str] = set()
        receipt_recovery_scenarios: set[str] = set()
        for receipt in receipts:
            receipt_points = receipt.get("crash_points") if isinstance(receipt, dict) else None
            receipt_scenarios = (
                receipt.get("recovery_scenarios") if isinstance(receipt, dict) else None
            )
            if (
                not isinstance(receipt, dict)
                or receipt.get("schema_version") != 1
                or not isinstance(receipt_points, list)
                or any(not isinstance(value, str) for value in receipt_points)
                or not isinstance(receipt_scenarios, list)
                or any(not isinstance(value, str) for value in receipt_scenarios)
                or plain_integer(receipt, "provider_calls") != plain_integer(
                    receipt, "expected_provider_calls"
                )
                or plain_integer(receipt, "orphan_effects") != 0
            ):
                blockers.append("runtime evidence receipt metrics are invalid")
                break
            receipt_crash_points.update(receipt_points)
            receipt_recovery_scenarios.update(receipt_scenarios)
        if receipt_crash_points != set(REQUIRED_RUNTIME_CRASH_POINTS):
            blockers.append("runtime evidence receipt crash points are incomplete")
        if receipt_recovery_scenarios != set(REQUIRED_RUNTIME_RECOVERY_SCENARIOS):
            blockers.append("runtime evidence receipt recovery scenarios are incomplete")
    if payload.get("errors") != []:
        blockers.append("runtime evidence generator reported errors")
    if payload.get("git_sha") != current_git_sha():
        blockers.append("evidence git_sha must match the checked-out commit")
    return blockers


def validate_local_tests(payload: dict[str, Any]) -> list[str]:
    blockers: list[str] = []
    if payload.get("schema_version") != 1:
        blockers.append("schema_version must be 1")
    if payload.get("status") != "pass":
        blockers.append("status must be pass")
    if payload.get("git_sha") != current_git_sha():
        blockers.append("evidence git_sha must match the checked-out commit")
    if payload.get("tests") != list(REQUIRED_LOCAL_TESTS):
        blockers.append("local test manifest must exactly match the required release suite")
    if plain_integer(payload, "passed") != len(REQUIRED_LOCAL_TESTS):
        blockers.append("every required local test must pass")
    if plain_integer(payload, "failed") != 0:
        blockers.append("failed local test count must be zero")
    if payload.get("compileall_passed") is not True:
        blockers.append("compileall must pass")
    results = payload.get("results")
    if not isinstance(results, list) or len(results) != len(REQUIRED_LOCAL_TESTS):
        blockers.append("local test results are incomplete")
    else:
        for expected, row in zip(REQUIRED_LOCAL_TESTS, results):
            if not isinstance(row, dict) or row.get("test") != expected or row.get("returncode") != 0:
                blockers.append(f"local test did not pass: {expected}")
    return blockers


def reproduce_evidence() -> dict[str, Any]:
    """Regenerate every derived artifact instead of trusting hand-written JSON."""

    commands: list[tuple[str, list[str], set[int], str]] = [
        (
            "local_test_suite",
            [sys.executable, str(ROOT / "tests/release/run_local_test_evidence.py")],
            {0},
            "artifacts/local-test-report.json",
        ),
        (
            "transactional_runtime_effects",
            [sys.executable, str(ROOT / "tests/release/run_runtime_effect_evidence.py")],
            {0},
            "artifacts/runtime-effect-report.json",
        ),
    ]
    backtest_command = [sys.executable, str(ROOT / "scripts/backtest_shadow_matrix.py")]
    dataset_path = os.environ.get("COSTMARSHAL_BACKTEST_DATASET")
    if dataset_path:
        backtest_command.extend(["--dataset", dataset_path])
        checkpoint_path = os.environ.get("COSTMARSHAL_BACKTEST_CHECKPOINT")
        if checkpoint_path:
            backtest_command.extend(["--checkpoint", checkpoint_path])
    signed_backtest_inputs = {
        "COSTMARSHAL_BACKTEST_ALLOWED_SIGNERS": "--allowed-signers",
        "COSTMARSHAL_BACKTEST_ATTESTATION_SIGNATURE": "--attestation-signature",
        "COSTMARSHAL_BACKTEST_SIGNER_IDENTITY": "--signer-identity",
    }
    for environment_name, option in signed_backtest_inputs.items():
        value = os.environ.get(environment_name)
        if value:
            backtest_command.extend([option, value])
    commands.append(
        ("real_provider_backtest", backtest_command, {0, 1, 2}, "artifacts/backtest-report.json")
    )

    oci_command = [sys.executable, str(ROOT / "tests/oci_live_evidence.py")]
    image = os.environ.get("COSTMARSHAL_OCI_IMAGE")
    if image:
        oci_command.extend(["--image", image])
    commands.append(
        ("oci_execution_adapter", oci_command, {0, 1, 2}, "artifacts/oci-attestation.json")
    )

    rows: list[dict[str, Any]] = []
    failed = False
    for evidence_id, argv, allowed_codes, artifact_relative in commands:
        artifact = ROOT / artifact_relative
        try:
            artifact.unlink(missing_ok=True)
            completed = subprocess.run(
                argv,
                cwd=ROOT,
                text=True,
                encoding="utf-8",
                errors="replace",
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                timeout=1800.0,
                check=False,
            )
            row = {
                "id": evidence_id,
                "returncode": completed.returncode,
                "allowed_returncodes": sorted(allowed_codes),
                "artifact": artifact_relative,
                "output_tail": completed.stdout[-2000:],
            }
            if completed.returncode not in allowed_codes:
                failed = True
            if not artifact.is_file():
                failed = True
                row["artifact_error"] = "generator did not create a fresh artifact"
            else:
                artifact_bytes = artifact.read_bytes()
                row["artifact_sha256"] = hashlib.sha256(artifact_bytes).hexdigest()
                try:
                    artifact_payload = json.loads(artifact_bytes.decode("utf-8"))
                except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                    failed = True
                    row["artifact_error"] = f"fresh artifact is invalid: {type(exc).__name__}"
                else:
                    expected_status = {0: "pass", 1: "fail", 2: "blocked"}.get(completed.returncode)
                    row["artifact_status"] = (
                        artifact_payload.get("status") if isinstance(artifact_payload, dict) else None
                    )
                    if not isinstance(artifact_payload, dict) or row["artifact_status"] != expected_status:
                        failed = True
                        row["artifact_error"] = "fresh artifact status does not match generator return code"
        except (OSError, subprocess.TimeoutExpired) as exc:
            failed = True
            row = {
                "id": evidence_id,
                "returncode": None,
                "allowed_returncodes": sorted(allowed_codes),
                "error": f"{type(exc).__name__}: {exc}",
            }
        rows.append(row)
        if failed:
            break
    return {"status": "fail" if failed else "pass", "commands": rows}


def build_report(reproduction: dict[str, Any] | None = None) -> dict[str, Any]:
    gates: list[dict[str, Any]] = []
    reproduction_status, reproduction_blockers, reproduced_hashes = reproduction_contract(
        reproduction
    )
    try:
        sha = git_output("rev-parse", "HEAD")
        sha_ok = bool(re.fullmatch(r"[0-9a-f]{40}", sha))
    except (OSError, subprocess.CalledProcessError) as exc:
        sha = ""
        sha_ok = False
        sha_error = str(exc)
    else:
        sha_error = None
    gates.append(
        gate(
            "git_sha",
            "pass" if sha_ok else "fail",
            "Release evidence is bound to an exact git commit.",
            git_sha=sha,
            error=sha_error,
        )
    )
    dirty = git_output("status", "--porcelain") if sha_ok else "unknown"
    gates.append(
        gate(
            "clean_worktree",
            "pass" if dirty == "" else "blocked",
            "A release must be built from a clean worktree.",
            dirty_entries=dirty.splitlines() if dirty else [],
        )
    )

    version_file = (ROOT / "VERSION").read_text(encoding="utf-8").strip()
    version_valid = bool(re.fullmatch(r"v\d+\.\d+\.\d+(?:-[0-9A-Za-z.-]+)?", version_file))
    gates.append(
        gate(
            "version_consistency",
            "pass" if version_valid and version_file == __version__ else "fail",
            "VERSION and package version are valid and identical.",
            version_file=version_file,
            package_version=__version__,
        )
    )
    gates.append(
        gate(
            "non_beta_version",
            "blocked" if "beta" in version_file.lower() else "pass",
            "A non-beta release cannot retain a beta prerelease version.",
            version=version_file,
        )
    )
    gates.append(documentation_contract())
    gates.append(required_test_entries())
    gates.append(release_policy_gate())
    gates.append(
        gate(
            "evidence_reproduction",
            reproduction_status,
            "Derived release evidence must be regenerated by this gate invocation.",
            reproduction=reproduction,
            blockers=reproduction_blockers,
        )
    )
    gates.append(
        external_evidence_gate(
            "local_test_suite",
            "artifacts/local-test-report.json",
            validate_local_tests,
            "All required local tests pass against the checked-out commit.",
            expected_sha256=reproduced_hashes.get("artifacts/local-test-report.json"),
        )
    )
    gates.append(
        external_evidence_gate(
            "real_provider_backtest",
            "artifacts/backtest-report.json",
            validate_backtest,
            "Real provider shadow-matrix backtest meets cost and acceptance thresholds.",
            expected_sha256=reproduced_hashes.get("artifacts/backtest-report.json"),
        )
    )
    gates.append(
        external_evidence_gate(
            "oci_execution_adapter",
            "artifacts/oci-attestation.json",
            validate_oci,
            "Enabled digest-pinned OCI execution passes malicious isolation tests.",
            expected_sha256=reproduced_hashes.get("artifacts/oci-attestation.json"),
        )
    )
    gates.append(
        external_evidence_gate(
            "transactional_runtime_effects",
            "artifacts/runtime-effect-report.json",
            validate_effect_worker,
            "Spawn/stop provider effects are processed by a crash-tested transactional effect worker.",
            expected_sha256=reproduced_hashes.get("artifacts/runtime-effect-report.json"),
        )
    )

    failed = sum(row["status"] == "fail" for row in gates)
    blocked = sum(row["status"] == "blocked" for row in gates)
    overall = "fail" if failed else "blocked" if blocked else "pass"
    exit_code = 1 if failed else 2 if blocked else 0
    return {
        "schema_version": 1,
        "generated_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "repository": str(ROOT),
        "git_sha": sha,
        "version": version_file,
        "status": overall,
        "exit_code": exit_code,
        "counts": {
            "pass": sum(row["status"] == "pass" for row in gates),
            "fail": failed,
            "blocked": blocked,
        },
        "gates": gates,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Aggregate CostMarshal non-beta release gates")
    parser.add_argument("--report", type=Path, help="Optional JSON report output path")
    parser.add_argument(
        "--reproduce-evidence",
        action="store_true",
        help="Run local, runtime-effect, offline backtest, and live OCI evidence generators before evaluating gates",
    )
    args = parser.parse_args(argv)
    reproduction = reproduce_evidence() if args.reproduce_evidence else None
    report = build_report(reproduction)
    encoded = json.dumps(report, indent=2, sort_keys=True, ensure_ascii=False)
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(encoded + "\n", encoding="utf-8")
    print(encoded)
    return int(report["exit_code"])


if __name__ == "__main__":
    raise SystemExit(main())
