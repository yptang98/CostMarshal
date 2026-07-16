#!/usr/bin/env python3
"""Contracts for the offline blind shadow-matrix backtest harness."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import subprocess
import shutil
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
HARNESS = ROOT / "scripts" / "backtest_shadow_matrix.py"
TIERS = ("low", "medium", "high")
ATTESTATION_NAMESPACE = "costmarshal-backtest-v1"
sys.path.insert(0, str(ROOT))

from costmarshal_v2.routing import decide_route, default_provider_catalog  # noqa: E402
from three_tier_routing_test import paired_chain_history  # noqa: E402


def hashed(value: str) -> str:
    return "sha256:" + hashlib.sha256(value.encode("utf-8")).hexdigest()


def canonical_hash(value: object) -> str:
    encoded = json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def current_git_sha() -> str:
    return subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip()


def attested_matrix_fixture(task_count: int = 210, *, task_budget_cny: float = 2.0) -> dict:
    """Build a deterministic schema fixture; it is not real release evidence."""

    catalog = default_provider_catalog()
    prices = {
        "longcat": (0.01, 0.01),
        "deepseek": (0.1, 0.1),
        "codex": (10.0, 10.0),
    }
    for provider in catalog["providers"]:
        provider["input_cny_per_1m"], provider["output_cny_per_1m"] = prices[provider["provider_id"]]
    providers = {row["provider_id"]: row for row in catalog["providers"]}
    low_medium_high_history = paired_chain_history()
    history: list[dict] = list(low_medium_high_history)
    for row in low_medium_high_history:
        if row["provider_id"] not in {"deepseek", "codex"}:
            continue
        projected = json.loads(json.dumps(row))
        projected["task_id"] += "-medium-high"
        projected["attempt_id"] += "-medium-high"
        projected["id"] += "-medium-high"
        projected["route_envelope_id"] += "-medium-high"
        if projected["provider_id"] == "deepseek":
            projected["route_plan_step_index"] = 0
            projected["route_predecessors"] = []
        else:
            projected["route_plan_step_index"] = 1
            projected["route_predecessors"] = [
                {
                    "provider_id": "deepseek",
                    "model": "inherit",
                    "profile": "deepseek",
                    "profile_sha256": None,
                    "attempt_id": row["route_predecessors"][1]["attempt_id"]
                    + "-medium-high",
                    "result_id": row["route_predecessors"][1]["result_id"]
                    + "-medium-high",
                }
            ]
        history.append(projected)
    locked_at = "2026-07-01T00:00:00Z"
    route_now = "2026-07-01T00:00:00Z"
    tasks = []
    task_routes = {}
    route_cache = {}
    for index in range(task_count):
        floor = TIERS[index % 3]
        routing_task = {
            "risk": floor,
            "difficulty": "normal",
            "task_type": "analysis",
            "required_capabilities": [],
        }
        if floor == "low":
            routing_task["min_success_probability"] = 0.8
        elif floor == "medium":
            routing_task["min_success_probability"] = 0.8
        input_tokens = 500_000
        output_tokens = 500_000
        candidate_request = {"requested_provider_id": None, "requested_tier": None}
        baseline_request = {"requested_provider_id": "codex", "requested_tier": None}
        cached_routes = route_cache.get(floor)
        if cached_routes is None:
            candidate = decide_route(
                routing_task,
                catalog,
                history=history,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                now=route_now,
                **candidate_request,
            )
            baseline_task = dict(routing_task)
            baseline_task.pop("min_success_probability", None)
            baseline = decide_route(
                baseline_task,
                catalog,
                history=history,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                now=route_now,
                **baseline_request,
            )
            route_cache[floor] = (candidate, baseline)
        else:
            candidate, baseline = cached_routes
        candidate_chain = [providers[row]["tier"] for row in candidate.planned_provider_ids]
        baseline_chain = [providers[row]["tier"] for row in baseline.planned_provider_ids]
        accepted = {
            "low": index % 5 != 0,
            "medium": index % 10 != 0,
            "high": index % 20 != 0,
        }
        costs = {"low": 0.1, "medium": 0.2, "high": 1.0}
        outcomes = {}
        for tier in TIERS:
            outcomes[tier] = {
                "blind_result_id": f"blind-{index}-{tier}",
                "provider_call_id_hash": hashed(f"provider-call-{index}-{tier}"),
                "accepted": accepted[tier],
                "quality_score": 5 if accepted[tier] else 2,
                "actual_cost_cny": costs[tier],
            }
        task_id = f"BT-{index:04d}"
        tasks.append(
            {
                "task_id": task_id,
                "task_input_sha256": hashed(f"task-input-{index}"),
                "safety_floor": floor,
                "candidate_chain": candidate_chain,
                "baseline_chain": baseline_chain,
                "task_budget_cny": task_budget_cny,
                "policy_locked_at": locked_at,
                "review_completed_at": "2026-07-02T00:00:00Z",
                "outcomes": outcomes,
            }
        )
        task_routes[task_id] = {
            "task": routing_task,
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "now": route_now,
            "candidate_request": candidate_request,
            "baseline_request": baseline_request,
            "candidate_provider_ids": list(candidate.planned_provider_ids),
            "baseline_provider_ids": list(baseline.planned_provider_ids),
        }
    manifest = {
        "schema_version": 1,
        "routing_engine": "costmarshal_v2.routing.decide_route",
        "git_sha": current_git_sha(),
        "locked_at": locked_at,
        "provider_catalog": catalog,
        "history": history,
        "task_routes": task_routes,
    }
    manifest_hash = canonical_hash(manifest)
    manifest["manifest_sha256"] = manifest_hash
    return {
        "schema_version": 1,
        "study_id": "unit-test-shadow-matrix-fixture",
        "real_provider_shadow_matrix": True,
        "synthetic": False,
        "collection_attestation": {
            "real_provider_calls_completed": True,
            "credentialed_collection_completed": True,
            "provider_call_ids_hashed": True,
        },
        "blinding": {
            "reviewer_blinded_to_provider": True,
            "reviewer_blinded_to_tier": True,
            "outcomes_unblinded_after_policy_lock": True,
        },
        "policy_manifest_sha256": manifest_hash,
        "policy_manifest": manifest,
        "tasks": tasks,
    }


def invoke(
    temp: Path,
    *extra: str,
    allow_unsigned_test_fixture: bool = True,
) -> tuple[subprocess.CompletedProcess[str], dict]:
    output = temp / "backtest-report.json"
    command = [
        sys.executable,
        str(HARNESS),
        "--output",
        str(output),
        "--checkpoint",
        str(temp / "checkpoint.json"),
        "--bootstrap-samples",
        "300",
    ]
    if allow_unsigned_test_fixture:
        command.append("--allow-unsigned-test-fixture")
    command.extend(extra)
    completed = subprocess.run(
        command,
        cwd=temp,
        text=True,
        encoding="utf-8",
        errors="replace",
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    return completed, json.loads(output.read_text(encoding="utf-8"))


def sign_dataset(temp: Path, dataset: Path) -> tuple[Path, Path, str]:
    ssh_keygen = shutil.which("ssh-keygen")
    if ssh_keygen is None:
        raise unittest.SkipTest("ssh-keygen is unavailable")
    identity = "costmarshal-release@example.test"
    private_key = temp / "attestation-ed25519"
    subprocess.run(
        [ssh_keygen, "-q", "-t", "ed25519", "-N", "", "-f", str(private_key)],
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    public_fields = Path(str(private_key) + ".pub").read_text(encoding="utf-8").split()
    allowed_signers = temp / "allowed_signers"
    allowed_signers.write_text(
        f"{identity} {public_fields[0]} {public_fields[1]}\n",
        encoding="utf-8",
    )
    subprocess.run(
        [
            ssh_keygen,
            "-Y",
            "sign",
            "-f",
            str(private_key),
            "-n",
            ATTESTATION_NAMESPACE,
            str(dataset),
        ],
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    signature = Path(str(dataset) + ".sig")
    return allowed_signers, signature, identity


class BacktestHarnessTest(unittest.TestCase):
    def test_missing_dataset_is_honestly_blocked_without_provider_calls(self) -> None:
        with tempfile.TemporaryDirectory(prefix="costmarshal-backtest-blocked-") as raw:
            completed, report = invoke(Path(raw))
            self.assertEqual(completed.returncode, 2, completed.stderr)
            self.assertEqual(report["status"], "blocked")
            self.assertFalse(report["real_provider_shadow_matrix"])
            self.assertEqual(report["provider_calls_made_by_harness"], 0)
            self.assertFalse(report["credentials_used_by_harness"])
            self.assertTrue(any("dataset is absent" in row for row in report["blockers"]))

    def test_self_asserted_attestation_is_not_release_evidence_without_signature(self) -> None:
        with tempfile.TemporaryDirectory(prefix="costmarshal-backtest-unsigned-release-") as raw:
            temp = Path(raw)
            dataset = temp / "matrix.json"
            dataset.write_text(json.dumps(attested_matrix_fixture()), encoding="utf-8")
            completed, report = invoke(
                temp,
                "--dataset",
                str(dataset),
                allow_unsigned_test_fixture=False,
            )
            self.assertEqual(completed.returncode, 2, completed.stderr)
            self.assertEqual(report["status"], "blocked")
            self.assertEqual(report["evidence_scope"], "blocked")
            self.assertFalse(report["external_attestation_verified"])
            self.assertFalse(report["real_provider_shadow_matrix"])
            self.assertTrue(any("external attestation requires" in row for row in report["blockers"]))

    def test_duplicate_task_or_provider_call_hashes_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory(prefix="costmarshal-backtest-duplicates-") as raw:
            temp = Path(raw)
            payload = attested_matrix_fixture()
            payload["tasks"][1]["task_input_sha256"] = payload["tasks"][0]["task_input_sha256"]
            payload["tasks"][1]["outcomes"]["low"]["provider_call_id_hash"] = (
                payload["tasks"][0]["outcomes"]["low"]["provider_call_id_hash"]
            )
            dataset = temp / "matrix.json"
            dataset.write_text(json.dumps(payload), encoding="utf-8")
            completed, report = invoke(temp, "--dataset", str(dataset))
            self.assertEqual(completed.returncode, 2)
            self.assertTrue(any("duplicate task_input_sha256" in row for row in report["blockers"]))
            self.assertTrue(any("duplicate provider_call_id_hash" in row for row in report["blockers"]))

    @unittest.skipUnless(shutil.which("ssh-keygen"), "ssh-keygen is unavailable")
    def test_trusted_signature_passes_and_dataset_tampering_is_blocked(self) -> None:
        with tempfile.TemporaryDirectory(prefix="costmarshal-backtest-signed-release-") as raw:
            temp = Path(raw)
            dataset = temp / "matrix.json"
            dataset.write_text(json.dumps(attested_matrix_fixture()), encoding="utf-8")
            allowed_signers, signature, identity = sign_dataset(temp, dataset)
            signed_args = (
                "--dataset",
                str(dataset),
                "--allowed-signers",
                str(allowed_signers),
                "--attestation-signature",
                str(signature),
                "--signer-identity",
                identity,
            )
            completed, report = invoke(
                temp,
                *signed_args,
                allow_unsigned_test_fixture=False,
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)
            self.assertEqual(report["status"], "pass")
            self.assertEqual(report["evidence_scope"], "release")
            self.assertTrue(report["external_attestation_verified"])
            self.assertTrue(report["real_provider_shadow_matrix"])
            self.assertTrue(report["credentialed_collection_completed"])
            self.assertEqual(
                report["external_attestation"]["namespace"], ATTESTATION_NAMESPACE
            )
            self.assertEqual(report["external_attestation"]["signer_identity"], identity)
            self.assertRegex(
                report["external_attestation"]["allowed_signers_sha256"],
                r"^sha256:[0-9a-f]{64}$",
            )
            self.assertRegex(
                report["external_attestation"]["signature_sha256"],
                r"^sha256:[0-9a-f]{64}$",
            )

            dataset.write_bytes(dataset.read_bytes() + b"\n")
            tampered, tampered_report = invoke(
                temp,
                *signed_args,
                allow_unsigned_test_fixture=False,
            )
            self.assertEqual(tampered.returncode, 2, tampered.stderr)
            self.assertEqual(tampered_report["status"], "blocked")
            self.assertFalse(tampered_report["external_attestation_verified"])
            self.assertFalse(tampered_report["real_provider_shadow_matrix"])
            self.assertTrue(any("signature is invalid" in row for row in tampered_report["blockers"]))

    def test_checkpoint_resume_and_paired_bootstrap_are_deterministic(self) -> None:
        with tempfile.TemporaryDirectory(prefix="costmarshal-backtest-resume-") as raw:
            temp = Path(raw)
            dataset = temp / "matrix.json"
            dataset.write_text(json.dumps(attested_matrix_fixture()), encoding="utf-8")

            partial, partial_report = invoke(
                temp,
                "--dataset",
                str(dataset),
                "--max-tasks",
                "75",
            )
            self.assertEqual(partial.returncode, 2, partial.stderr)
            self.assertEqual(partial_report["status"], "blocked")
            self.assertEqual(partial_report["processed_tasks"], 75)
            checkpoint = json.loads((temp / "checkpoint.json").read_text(encoding="utf-8"))
            self.assertFalse(checkpoint["complete"])
            self.assertEqual(checkpoint["processed_tasks"], 75)

            resumed, report = invoke(temp, "--dataset", str(dataset))
            self.assertEqual(resumed.returncode, 0, resumed.stderr)
            self.assertEqual(report["status"], "pass")
            self.assertEqual(report["task_count"], 210)
            self.assertFalse(report["real_provider_shadow_matrix"])
            self.assertEqual(report["evidence_scope"], "test-only")
            self.assertFalse(report["external_attestation_verified"])
            self.assertFalse(report["credentialed_collection_completed"])
            self.assertEqual(report["provider_calls_made_by_harness"], 0)
            self.assertEqual(report["budget_overruns"], 0)
            self.assertLess(report["cost_ratio_ci_upper"], 1.0)
            self.assertGreaterEqual(report["acceptance_delta_ci_lower"], -0.02)
            self.assertEqual(report["floor_counts"], {"low": 70, "medium": 70, "high": 70})

            fresh_dir = temp / "fresh"
            fresh_dir.mkdir()
            fresh, fresh_report = invoke(fresh_dir, "--dataset", str(dataset))
            self.assertEqual(fresh.returncode, 0, fresh.stderr)
            self.assertEqual(report["bootstrap"], fresh_report["bootstrap"])
            self.assertEqual(report["metrics"], fresh_report["metrics"])

    def test_forged_checkpoint_row_is_rejected_after_recomputation(self) -> None:
        with tempfile.TemporaryDirectory(prefix="costmarshal-backtest-forged-checkpoint-") as raw:
            temp = Path(raw)
            dataset = temp / "matrix.json"
            dataset.write_text(json.dumps(attested_matrix_fixture()), encoding="utf-8")
            partial, _ = invoke(temp, "--dataset", str(dataset), "--max-tasks", "75")
            self.assertEqual(partial.returncode, 2, partial.stderr)

            checkpoint_path = temp / "checkpoint.json"
            checkpoint = json.loads(checkpoint_path.read_text(encoding="utf-8"))
            checkpoint["rows"][0]["candidate_cost_cny"] = 0.0
            checkpoint_path.write_text(json.dumps(checkpoint), encoding="utf-8")
            resumed, report = invoke(temp, "--dataset", str(dataset))
            self.assertEqual(resumed.returncode, 1, resumed.stderr)
            self.assertEqual(report["status"], "fail")
            self.assertTrue(any("recomputation" in row for row in report["blockers"]))

    def test_checkpoint_is_bound_to_bootstrap_and_threshold_config(self) -> None:
        with tempfile.TemporaryDirectory(prefix="costmarshal-backtest-config-checkpoint-") as raw:
            temp = Path(raw)
            dataset = temp / "matrix.json"
            dataset.write_text(json.dumps(attested_matrix_fixture()), encoding="utf-8")
            partial, _ = invoke(
                temp,
                "--dataset",
                str(dataset),
                "--max-tasks",
                "75",
                "--seed",
                "7",
            )
            self.assertEqual(partial.returncode, 2, partial.stderr)
            resumed, report = invoke(temp, "--dataset", str(dataset), "--seed", "8")
            self.assertEqual(resumed.returncode, 1, resumed.stderr)
            self.assertEqual(report["status"], "fail")
            self.assertTrue(any("config" in row for row in report["blockers"]))

    def test_dataset_chain_must_equal_hash_bound_costmarshal_policy_output(self) -> None:
        with tempfile.TemporaryDirectory(prefix="costmarshal-backtest-forged-chain-") as raw:
            temp = Path(raw)
            matrix = attested_matrix_fixture()
            matrix["tasks"][0]["candidate_chain"] = ["high"]
            manifest = matrix["policy_manifest"]
            manifest["task_routes"]["BT-0000"]["candidate_provider_ids"] = ["codex"]
            manifest_body = {key: value for key, value in manifest.items() if key != "manifest_sha256"}
            forged_hash = canonical_hash(manifest_body)
            manifest["manifest_sha256"] = forged_hash
            matrix["policy_manifest_sha256"] = forged_hash
            dataset = temp / "matrix.json"
            dataset.write_text(json.dumps(matrix), encoding="utf-8")
            completed, report = invoke(temp, "--dataset", str(dataset))
            self.assertEqual(completed.returncode, 2, completed.stderr)
            self.assertEqual(report["status"], "blocked")
            self.assertTrue(
                any(
                    "candidate_provider_ids differs from CostMarshal output" in row
                    for row in report["blockers"]
                )
            )

    def test_policy_cannot_be_recomputed_after_the_blind_review_lock(self) -> None:
        with tempfile.TemporaryDirectory(prefix="costmarshal-backtest-post-lock-route-") as raw:
            temp = Path(raw)
            matrix = attested_matrix_fixture()
            matrix["policy_manifest"]["task_routes"]["BT-0000"]["now"] = "2026-07-02T00:00:00Z"
            manifest = matrix["policy_manifest"]
            manifest_body = {key: value for key, value in manifest.items() if key != "manifest_sha256"}
            manifest_hash = canonical_hash(manifest_body)
            manifest["manifest_sha256"] = manifest_hash
            matrix["policy_manifest_sha256"] = manifest_hash
            dataset = temp / "matrix.json"
            dataset.write_text(json.dumps(matrix), encoding="utf-8")
            completed, report = invoke(temp, "--dataset", str(dataset))
            self.assertEqual(completed.returncode, 2, completed.stderr)
            self.assertEqual(report["status"], "blocked")
            self.assertTrue(any("pre-review policy lock time" in row for row in report["blockers"]))

    def test_unblinded_or_synthetic_evidence_is_blocked(self) -> None:
        with tempfile.TemporaryDirectory(prefix="costmarshal-backtest-blind-") as raw:
            temp = Path(raw)
            matrix = attested_matrix_fixture()
            matrix["blinding"]["reviewer_blinded_to_tier"] = False
            matrix["synthetic"] = True
            dataset = temp / "matrix.json"
            dataset.write_text(json.dumps(matrix), encoding="utf-8")
            completed, report = invoke(temp, "--dataset", str(dataset))
            self.assertEqual(completed.returncode, 2, completed.stderr)
            self.assertEqual(report["status"], "blocked")
            self.assertTrue(any("synthetic must be false" in row for row in report["blockers"]))
            self.assertTrue(any("reviewer_blinded_to_tier" in row for row in report["blockers"]))

    def test_budget_overruns_fail_completed_evidence(self) -> None:
        with tempfile.TemporaryDirectory(prefix="costmarshal-backtest-budget-") as raw:
            temp = Path(raw)
            dataset = temp / "matrix.json"
            dataset.write_text(
                json.dumps(attested_matrix_fixture(task_budget_cny=0.01)), encoding="utf-8"
            )
            completed, report = invoke(temp, "--dataset", str(dataset))
            self.assertEqual(completed.returncode, 1, completed.stderr)
            self.assertEqual(report["status"], "fail")
            self.assertGreater(report["budget_overruns"], 0)
            self.assertTrue(any("budget overruns" in row for row in report["blockers"]))

    def test_tier_keyed_matrix_rejects_ambiguous_same_tier_providers(self) -> None:
        with tempfile.TemporaryDirectory(prefix="costmarshal-backtest-ambiguous-tier-") as raw:
            temp = Path(raw)
            matrix = attested_matrix_fixture()
            duplicate = dict(matrix["policy_manifest"]["provider_catalog"]["providers"][0])
            duplicate["provider_id"] = "another-low"
            duplicate["priority"] = 200
            matrix["policy_manifest"]["provider_catalog"]["providers"].append(duplicate)
            manifest = matrix["policy_manifest"]
            manifest_body = {key: value for key, value in manifest.items() if key != "manifest_sha256"}
            manifest_hash = canonical_hash(manifest_body)
            manifest["manifest_sha256"] = manifest_hash
            matrix["policy_manifest_sha256"] = manifest_hash
            dataset = temp / "matrix.json"
            dataset.write_text(json.dumps(matrix), encoding="utf-8")
            completed, report = invoke(temp, "--dataset", str(dataset))
            self.assertEqual(completed.returncode, 2, completed.stderr)
            self.assertEqual(report["status"], "blocked")
            self.assertTrue(
                any("exactly one enabled provider per tier" in row for row in report["blockers"])
            )

    def test_undefined_bootstrap_cost_ratios_cannot_be_silently_dropped(self) -> None:
        with tempfile.TemporaryDirectory(prefix="costmarshal-backtest-bootstrap-coverage-") as raw:
            temp = Path(raw)
            matrix = attested_matrix_fixture()
            for index, task in enumerate(matrix["tasks"]):
                task["outcomes"]["high"]["accepted"] = index == 0
                task["outcomes"]["high"]["quality_score"] = 5 if index == 0 else 1
            dataset = temp / "matrix.json"
            dataset.write_text(json.dumps(matrix), encoding="utf-8")
            completed, report = invoke(temp, "--dataset", str(dataset))
            self.assertEqual(completed.returncode, 1, completed.stderr)
            self.assertEqual(report["status"], "fail")
            self.assertTrue(any("bootstrap coverage is insufficient" in row for row in report["blockers"]))


if __name__ == "__main__":
    unittest.main(verbosity=2)
