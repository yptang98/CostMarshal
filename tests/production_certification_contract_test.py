from __future__ import annotations

import hashlib
import shutil
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from costmarshal_v2.large_project import (
    build_production_boundary,
    production_status,
)
from costmarshal_v2.production_evidence import (
    PRODUCTION_CERTIFICATION_NAMESPACE,
    ProductionEvidenceError,
    build_production_certification_claim,
    canonical_certification_bytes,
    verify_production_certification,
)


EVIDENCE_IDS = {
    "real_provider_backtest": "PART-backtest",
    "live_oci_adversarial": "PART-oci",
    "credential_broker_attestation": "PART-broker",
    "provider_proxy_budget_attestation": "PART-budget",
    "schema_drift_monitor": "PART-drift",
}


class ProductionCertificationContractTest(unittest.TestCase):
    def setUp(self) -> None:
        self.ssh_keygen = shutil.which("ssh-keygen")
        if not self.ssh_keygen:
            self.skipTest("ssh-keygen is required for detached-signature contract tests")
        self.owner = tempfile.TemporaryDirectory()
        self.root = Path(self.owner.name)
        self.identity = "costmarshal-release@example.test"
        self.key = self.root / "release-ed25519"
        subprocess.run(
            [
                self.ssh_keygen,
                "-q",
                "-t",
                "ed25519",
                "-N",
                "",
                "-f",
                str(self.key),
            ],
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        public_key = self.key.with_suffix(".pub").read_text(encoding="utf-8").strip()
        self.allowed = self.root / "allowed_signers"
        self.allowed.write_text(
            f"{self.identity} {public_key}\n",
            encoding="utf-8",
            newline="\n",
        )
        self.allowed_hash = "sha256:" + hashlib.sha256(
            self.allowed.read_bytes()
        ).hexdigest()
        self.policy_hash = "sha256:" + "b" * 64
        self.worker_image = "example/worker@sha256:" + "a" * 64
        self.gateway_image = "example/gateway@sha256:" + "c" * 64
        self.git_sha = "d" * 40
        self.boundary = build_production_boundary(
            mode="enforced",
            broker_endpoint="https://broker.example/v1/leases",
            broker_identity="spiffe://example.test/costmarshal/worker",
            provider_proxy_endpoint="https://proxy.example/v1",
            hard_budget_enforced=True,
            evidence_artifact_ids=EVIDENCE_IDS,
            runtime_adapter="costmarshal-gateway-v1",
            gateway_policy_sha256=self.policy_hash,
            deployment_commit=self.git_sha,
            release_version="v4.2.0",
            gateway_image=self.gateway_image,
            allowed_signers_sha256=self.allowed_hash,
            signer_identities=[self.identity],
        )
        self.report_hashes = {
            evidence_type: "sha256:" + hashlib.sha256(
                evidence_type.encode("utf-8")
            ).hexdigest()
            for evidence_type in EVIDENCE_IDS
        }
        self.artifacts = [
            {
                "schema_version": "costmarshal-project-artifact-v1",
                "artifact_id": artifact_id,
                "lifecycle": "accepted",
                "kind": "production-evidence",
                "storage": {
                    "mode": "external-reference",
                    "uri": f"https://evidence.example/{evidence_type}.json",
                    "size_bytes": 1,
                    "sha256": self.report_hashes[evidence_type],
                },
            }
            for evidence_type, artifact_id in EVIDENCE_IDS.items()
        ]

    def tearDown(self) -> None:
        self.owner.cleanup()

    def _signed_claim(
        self,
        *,
        issued_at: datetime | None = None,
        expires_at: datetime | None = None,
    ) -> tuple[Path, Path]:
        now = datetime.now(timezone.utc)
        claim = build_production_certification_claim(
            git_sha=self.git_sha,
            release_version="v4.2.0",
            boundary_sha256=self.boundary["boundary_sha256"],
            gateway_policy_sha256=self.policy_hash,
            worker_image=self.worker_image,
            gateway_image=self.gateway_image,
            evidence={
                evidence_type: {
                    "artifact_id": artifact_id,
                    "report_sha256": self.report_hashes[evidence_type],
                }
                for evidence_type, artifact_id in EVIDENCE_IDS.items()
            },
            issued_at=issued_at or now - timedelta(minutes=1),
            expires_at=expires_at or now + timedelta(hours=1),
        )
        manifest = self.root / "production-certification.json"
        manifest.write_bytes(canonical_certification_bytes(claim))
        subprocess.run(
            [
                self.ssh_keygen,
                "-Y",
                "sign",
                "-f",
                str(self.key),
                "-n",
                PRODUCTION_CERTIFICATION_NAMESPACE,
                str(manifest),
            ],
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        return manifest, Path(str(manifest) + ".sig")

    def _verify(
        self,
        manifest: Path,
        signature: Path,
        *,
        artifacts: list[dict] | None = None,
    ):
        return verify_production_certification(
            manifest_path=manifest,
            signature_path=signature,
            allowed_signers_path=self.allowed,
            signer_identities=[self.identity],
            expected_allowed_signers_sha256=self.allowed_hash,
            expected_git_sha=self.git_sha,
            expected_release_version="v4.2.0",
            expected_boundary_sha256=self.boundary["boundary_sha256"],
            expected_gateway_policy_sha256=self.policy_hash,
            expected_worker_image=self.worker_image,
            expected_gateway_image=self.gateway_image,
            artifact_rows=artifacts if artifacts is not None else self.artifacts,
            ssh_keygen=self.ssh_keygen,
        )

    def test_signed_exact_deployment_can_be_certified(self) -> None:
        manifest, signature = self._signed_claim()
        receipt = self._verify(manifest, signature)
        status = production_status(
            boundary=self.boundary,
            artifact_rows=self.artifacts,
            sqlite_authoritative=True,
            worker_isolation={
                "image": self.worker_image,
                "network_mode": "provider-proxy",
            },
            runtime_probes={
                "broker": {
                    "status": "pass",
                    "policy_sha256": self.policy_hash,
                },
                "proxy": {
                    "status": "pass",
                    "policy_sha256": self.policy_hash,
                },
            },
            certification=receipt,
        )
        self.assertEqual(status["runtime_status"], "ready")
        self.assertEqual(status["certification_status"], "certified")
        self.assertEqual(status["status"], "ready")
        self.assertTrue(status["external_certification"])

    def test_unsigned_ids_and_unaccepted_receipts_never_certify(self) -> None:
        runtime_only = production_status(
            boundary=self.boundary,
            artifact_rows=self.artifacts,
            sqlite_authoritative=True,
            worker_isolation={
                "image": self.worker_image,
                "network_mode": "provider-proxy",
            },
            runtime_probes={
                service: {
                    "status": "pass",
                    "policy_sha256": self.policy_hash,
                }
                for service in ("broker", "proxy")
            },
        )
        self.assertEqual(runtime_only["runtime_status"], "ready")
        self.assertEqual(runtime_only["status"], "blocked")
        self.assertFalse(runtime_only["external_certification"])

        manifest, signature = self._signed_claim()
        rejected = [dict(item) for item in self.artifacts]
        rejected[0] = {**rejected[0], "lifecycle": "rejected"}
        with self.assertRaisesRegex(
            ProductionEvidenceError,
            "accepted production-evidence",
        ):
            self._verify(manifest, signature, artifacts=rejected)

    def test_expired_tampered_or_wrong_trust_is_rejected(self) -> None:
        now = datetime.now(timezone.utc)
        manifest, signature = self._signed_claim(
            issued_at=now - timedelta(hours=2),
            expires_at=now - timedelta(hours=1),
        )
        with self.assertRaisesRegex(ProductionEvidenceError, "expired"):
            self._verify(manifest, signature)

        manifest, signature = self._signed_claim()
        data = manifest.read_bytes()
        manifest.write_bytes(data.replace(self.git_sha.encode(), ("e" * 40).encode()))
        with self.assertRaises(ProductionEvidenceError):
            self._verify(manifest, signature)

        manifest, signature = self._signed_claim()
        with self.assertRaisesRegex(ProductionEvidenceError, "trust root"):
            verify_production_certification(
                manifest_path=manifest,
                signature_path=signature,
                allowed_signers_path=self.allowed,
                signer_identities=[self.identity],
                expected_allowed_signers_sha256="sha256:" + "f" * 64,
                expected_git_sha=self.git_sha,
                expected_release_version="v4.2.0",
                expected_boundary_sha256=self.boundary["boundary_sha256"],
                expected_gateway_policy_sha256=self.policy_hash,
                expected_worker_image=self.worker_image,
                expected_gateway_image=self.gateway_image,
                artifact_rows=self.artifacts,
                ssh_keygen=self.ssh_keygen,
            )


if __name__ == "__main__":
    unittest.main()
