#!/usr/bin/env python3
"""Static production packaging and fail-closed deployment contracts."""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from costmarshal_v2.production_gateway import GatewayError, GatewayPolicy
from scripts.costmarshal_production_deploy import (
    DeploymentBlocked,
    _container_database_path,
    _healthy_services,
)


class ProductionDeploymentContractTest(unittest.TestCase):
    def test_gateway_container_is_non_root_read_only_and_digest_driven(self) -> None:
        dockerfile = (
            ROOT / "container" / "gateway" / "Dockerfile"
        ).read_text(encoding="utf-8")
        self.assertIn("ARG PYTHON_BASE_IMAGE", dockerfile)
        self.assertIn("FROM ${PYTHON_BASE_IMAGE}", dockerfile)
        self.assertNotRegex(dockerfile, r"(?m)^FROM\s+[^$]")
        self.assertIn("USER 65532:65532", dockerfile)
        self.assertNotIn("pip install", dockerfile)

    def test_compose_separates_broker_proxy_secrets_and_worker_egress(self) -> None:
        compose = (
            ROOT / "deploy" / "production" / "compose.yaml"
        ).read_text(encoding="utf-8")
        self.assertIn("read_only: true", compose)
        self.assertIn("cap_drop: [ALL]", compose)
        self.assertIn("no-new-privileges:true", compose)
        self.assertIn("name: costmarshal-provider-proxy", compose)
        self.assertIn("internal: true", compose)
        broker, proxy = compose.split("  provider-proxy:", 1)
        proxy_service = proxy.split("\nnetworks:\n", 1)[0]
        self.assertNotIn("/run/provider-credentials", broker)
        self.assertIn("/run/provider-credentials:ro", proxy_service)
        self.assertIn("COSTMARSHAL_PROVIDER_CREDENTIALS_DIR", proxy_service)
        self.assertIn("COSTMARSHAL_GATEWAY_STATE_DIR", broker)
        self.assertIn("COSTMARSHAL_GATEWAY_STATE_DIR", proxy_service)
        self.assertNotIn("workload_client_ca", proxy_service)
        self.assertEqual(compose.count("healthcheck:"), 2)
        self.assertIn("127.0.0.1', 8443", broker)
        self.assertIn("127.0.0.1', 9443", proxy_service)
        self.assertNotRegex(compose, r"(?i)(api[_-]?key|secret)\s*:\s*[\"']?[A-Za-z0-9_-]{16,}")

    def test_example_policy_cannot_start_until_prices_and_identity_are_reviewed(self) -> None:
        example = ROOT / "deploy" / "production" / "gateway-policy.example.json"
        with self.assertRaises(GatewayError):
            GatewayPolicy.load(example)

    def test_policy_validation_cli_returns_deployment_binding(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            temp = Path(raw)
            policy_path = temp / "gateway-policy.json"
            policy_path.write_text(
                json.dumps(
                    {
                        "schema_version": "costmarshal-production-gateway-policy-v1",
                        "issuer": "costmarshal-broker",
                        "audience": "costmarshal-provider-proxy",
                        "lease_ttl_seconds": 300,
                        "database_path": str(temp / "gateway.db"),
                        "providers": {
                            "longcat": {
                                "base_url": "https://api.longcat.chat/openai/v1",
                                "credential_file": str(temp / "longcat.secret"),
                                "auth_header": "Authorization",
                                "auth_scheme": "Bearer",
                                "models": {
                                    "LongCat-2.0": {
                                        "input_nano_cny_per_million": 1,
                                        "output_nano_cny_per_million": 1,
                                        "request_overhead_tokens": 1024,
                                        "max_output_tokens": 32768,
                                    }
                                },
                            }
                        },
                        "workloads": {
                            "spiffe://example.test/costmarshal/worker": {
                                "providers": ["longcat"],
                                "max_lease_budget_nano_cny": 1000000,
                                "max_lease_ttl_seconds": 300,
                            }
                        },
                    }
                ),
                encoding="utf-8",
            )
            completed = subprocess.run(
                [
                    sys.executable,
                    str(ROOT / "scripts" / "costmarshal_gateway.py"),
                    "validate-policy",
                    "--config",
                    str(policy_path),
                ],
                cwd=ROOT,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        result = json.loads(completed.stdout)
        self.assertEqual(result["status"], "ok")
        self.assertRegex(result["policy_sha256"], r"^sha256:[0-9a-f]{64}$")
        self.assertEqual(result["providers"], ["longcat"])

    def test_deployment_entrypoint_is_preview_first_and_secret_safe(self) -> None:
        script = (
            ROOT / "scripts" / "costmarshal_production_deploy.py"
        ).read_text(encoding="utf-8")
        self.assertIn('"--apply"', script)
        self.assertIn('"status": "ready-to-deploy"', script)
        self.assertIn('receipt["status"] = "deployed"', script)
        self.assertIn('"status": "blocked"', script)
        self.assertIn('"config", "--quiet"', script)
        self.assertIn('"up",', script)
        self.assertIn('"--wait"', script)
        self.assertIn('"--format",', script)
        self.assertIn('"healthy"', script)
        self.assertIn("source checkout is dirty", script)
        self.assertIn("credential_present", script)
        self.assertNotIn("credential_sha256", script)
        self.assertNotIn("LONGCAT_API_KEY", script)

    def test_production_database_must_use_the_shared_state_mount(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            temp = Path(raw)
            policy = temp / "gateway-policy.json"
            policy.write_text(
                json.dumps({"database_path": "/var/lib/costmarshal/gateway.db"}),
                encoding="utf-8",
            )
            self.assertEqual(
                _container_database_path(policy),
                "/var/lib/costmarshal/gateway.db",
            )
            for unsafe in (
                "gateway.db",
                "/tmp/gateway.db",
                "/var/lib/costmarshal/nested/gateway.db",
                "/var/lib/costmarshal/../gateway.db",
            ):
                policy.write_text(
                    json.dumps({"database_path": unsafe}),
                    encoding="utf-8",
                )
                with self.assertRaises(DeploymentBlocked):
                    _container_database_path(policy)

    def test_deployment_receipt_requires_both_services_healthy(self) -> None:
        healthy = json.dumps(
            [
                {
                    "Service": "credential-broker",
                    "State": "running",
                    "Health": "healthy",
                },
                {
                    "Service": "provider-proxy",
                    "State": "running",
                    "Health": "healthy",
                },
            ]
        )
        self.assertEqual(
            [row["service"] for row in _healthy_services(healthy)],
            ["credential-broker", "provider-proxy"],
        )
        unhealthy = healthy.replace('"healthy"', '"starting"', 1)
        with self.assertRaises(DeploymentBlocked):
            _healthy_services(unhealthy)


if __name__ == "__main__":
    unittest.main()
