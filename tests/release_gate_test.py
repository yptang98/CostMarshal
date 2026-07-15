#!/usr/bin/env python3
"""Fail-closed contracts for machine-readable release evidence."""

from __future__ import annotations

from pathlib import Path
import json
import subprocess
import sys
import tempfile
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests" / "release"))

import run_release_gates as release_gates  # noqa: E402
from run_release_gates import (  # noqa: E402
    REQUIRED_LOCAL_TESTS,
    build_report,
    current_git_sha,
    documentation_contract,
    external_evidence_gate,
    reproduction_contract,
    validate_backtest,
    validate_effect_worker,
    validate_local_tests,
    validate_oci,
)
from runtime_evidence_contract import (  # noqa: E402
    REQUIRED_RUNTIME_CRASH_POINTS,
    REQUIRED_RUNTIME_RECOVERY_SCENARIOS,
    RUNTIME_EVIDENCE_TESTS,
)


class ReleaseGateTest(unittest.TestCase):
    def test_malformed_numeric_evidence_blocks_instead_of_crashing(self) -> None:
        sha = current_git_sha()
        backtest = {
            "status": "pass",
            "real_provider_shadow_matrix": True,
            "task_count": "200",
            "cost_ratio_ci_upper": "0.5",
            "acceptance_delta_ci_lower": "0",
            "budget_overruns": False,
            "git_sha": sha,
        }
        self.assertGreaterEqual(len(validate_backtest(backtest)), 4)
        oci = {
            "status": "pass",
            "adapter_enabled": True,
            "strong_isolation": True,
            "image": "example/worker@sha256:" + "a" * 64,
            "escape_attempts": "1",
            "escapes_succeeded": False,
            "git_sha": sha,
        }
        self.assertGreaterEqual(len(validate_oci(oci)), 2)
        effects = {
            "status": "pass",
            "transactional_effect_worker": True,
            "crash_points_tested": float("inf"),
            "duplicate_provider_calls": False,
            "orphan_effects": "0",
            "git_sha": sha,
        }
        self.assertGreaterEqual(len(validate_effect_worker(effects)), 3)

    def test_complete_sha_bound_local_suite_is_required(self) -> None:
        results = [{"test": test, "returncode": 0} for test in REQUIRED_LOCAL_TESTS]
        payload = {
            "schema_version": 1,
            "status": "pass",
            "git_sha": current_git_sha(),
            "compileall_passed": True,
            "tests": list(REQUIRED_LOCAL_TESTS),
            "passed": len(REQUIRED_LOCAL_TESTS),
            "failed": 0,
            "results": results,
        }
        self.assertEqual(validate_local_tests(payload), [])
        payload["results"] = results[:-1]
        self.assertIn("local test results are incomplete", validate_local_tests(payload))

    def test_runtime_effect_evidence_requires_exact_machine_receipts(self) -> None:
        receipts = [
            {
                "schema_version": 1,
                "test": test,
                "crash_points": [],
                "recovery_scenarios": [],
                "provider_calls": 0,
                "expected_provider_calls": 0,
                "orphan_effects": 0,
            }
            for test in RUNTIME_EVIDENCE_TESTS
        ]
        receipts[0]["crash_points"] = list(REQUIRED_RUNTIME_CRASH_POINTS)
        receipts[0]["recovery_scenarios"] = list(REQUIRED_RUNTIME_RECOVERY_SCENARIOS)
        payload = {
            "schema_version": 1,
            "status": "pass",
            "transactional_effect_worker": True,
            "git_sha": current_git_sha(),
            "crash_points_tested": len(REQUIRED_RUNTIME_CRASH_POINTS),
            "crash_points": list(REQUIRED_RUNTIME_CRASH_POINTS),
            "recovery_scenarios": list(REQUIRED_RUNTIME_RECOVERY_SCENARIOS),
            "duplicate_provider_calls": 0,
            "missing_provider_calls": 0,
            "orphan_effects": 0,
            "errors": [],
            "receipts": receipts,
            "tests": [{"test": test, "returncode": 0} for test in RUNTIME_EVIDENCE_TESTS],
        }
        self.assertEqual(validate_effect_worker(payload), [])
        payload["receipts"] = receipts[:-1]
        self.assertIn("runtime evidence receipts are incomplete", validate_effect_worker(payload))

    def test_runtime_evidence_refuses_optimized_python(self) -> None:
        with tempfile.TemporaryDirectory(prefix="costmarshal-optimized-evidence-") as raw:
            artifact = Path(raw) / "runtime.json"
            completed = subprocess.run(
                [
                    sys.executable,
                    "-O",
                    str(ROOT / "tests" / "release" / "run_runtime_effect_evidence.py"),
                    str(artifact),
                ],
                cwd=ROOT,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )
            self.assertNotEqual(completed.returncode, 0)
            self.assertFalse(artifact.exists())

    def test_release_cannot_pass_without_in_process_evidence_reproduction(self) -> None:
        report = build_report()
        gate = next(row for row in report["gates"] if row["id"] == "evidence_reproduction")
        self.assertEqual(gate["status"], "blocked")
        self.assertEqual(documentation_contract()["status"], "pass")

    def test_reproduction_contract_requires_exact_generators_and_hashes(self) -> None:
        status, blockers, hashes = reproduction_contract(
            {"status": "pass", "commands": [{"id": "fabricated", "artifact_sha256": "fake"}]}
        )
        self.assertEqual(status, "fail")
        self.assertTrue(blockers)
        self.assertEqual(hashes, {})

    def test_final_evidence_bytes_must_match_reproduced_hash(self) -> None:
        with tempfile.TemporaryDirectory(prefix="costmarshal-release-byte-binding-") as raw:
            fake_root = Path(raw)
            artifact = fake_root / "artifacts" / "evidence.json"
            artifact.parent.mkdir()
            artifact.write_text(json.dumps({"status": "pass"}), encoding="utf-8")
            with mock.patch.object(release_gates, "ROOT", fake_root):
                result = external_evidence_gate(
                    "byte_binding",
                    "artifacts/evidence.json",
                    lambda payload: [],
                    "bind final bytes",
                    expected_sha256="0" * 64,
                )
            self.assertEqual(result["status"], "fail")
            self.assertTrue(any("differ" in blocker for blocker in result["details"]["blockers"]))

    def test_arbitrary_self_signed_backtest_is_not_repository_trusted(self) -> None:
        blockers = validate_backtest(
            {
                "external_attestation": {
                    "allowed_signers_sha256": "sha256:" + "a" * 64,
                    "signer_identity": "attacker@example.invalid",
                },
                "policy_manifest_sha256": "sha256:" + "b" * 64,
            }
        )
        self.assertTrue(any("repository-approved trust root" in blocker for blocker in blockers))

    def test_local_manifest_cannot_silently_omit_a_test_file(self) -> None:
        discovered = {
            str(path.relative_to(ROOT)).replace("\\", "/")
            for path in (ROOT / "tests").glob("*_test.py")
        }
        required = set(REQUIRED_LOCAL_TESTS)
        self.assertEqual(discovered, required - {"scripts/install_smoke_test.py"})

    def test_reproduction_deletes_stale_artifact_and_requires_fresh_output(self) -> None:
        with tempfile.TemporaryDirectory(prefix="costmarshal-release-stale-") as raw:
            fake_root = Path(raw)
            artifact = fake_root / "artifacts" / "local-test-report.json"
            artifact.parent.mkdir()
            artifact.write_text('{"status":"pass"}\n', encoding="utf-8")
            completed = subprocess.CompletedProcess(args=[], returncode=0, stdout="")
            with mock.patch.object(release_gates, "ROOT", fake_root), mock.patch.object(
                release_gates.subprocess, "run", return_value=completed
            ):
                result = release_gates.reproduce_evidence()
            self.assertEqual(result["status"], "fail")
            self.assertFalse(artifact.exists())
            self.assertIn(
                "fresh artifact",
                str(result["commands"][0].get("artifact_error")),
            )


if __name__ == "__main__":
    unittest.main(verbosity=2)
