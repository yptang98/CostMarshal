#!/usr/bin/env python3
"""Required OCI actors consume gateway leases, never raw provider keys."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from costmarshal_v2.actor_runner import _required_worker_bundle
from costmarshal_v2.large_project import build_production_boundary
from costmarshal_v2.paths import ProjectLayout
from costmarshal_v2.state import (
    atomic_write_json,
    load_actor,
    load_project,
    load_task,
    save_actor,
    save_task,
)


CLI = ROOT / "scripts" / "costmarshal.py"
IMAGE = "ghcr.io/example/costmarshal-worker@sha256:" + "a" * 64
RAW_PROVIDER_SECRET = "raw-provider-secret-must-not-enter-worker"
LEASE_TOKEN = "lease-token-only-in-worker"


class ProductionActorGatewayContractTest(unittest.TestCase):
    def cli(self, *args: str) -> dict:
        environment = os.environ.copy()
        environment["COSTMARSHAL_V2_HOME"] = str(self.runtime)
        environment["CODEX_HOME"] = str(self.codex_home)
        completed = subprocess.run(
            [sys.executable, str(CLI), "--root", str(self.runtime), *args],
            cwd=ROOT,
            env=environment,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        self.assertEqual(
            completed.returncode,
            0,
            f"{args}\n{completed.stdout}\n{completed.stderr}",
        )
        return json.loads(completed.stdout)

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.temp = Path(self.temp_dir.name)
        self.runtime = self.temp / "runtime"
        self.workspace = self.temp / "source"
        self.workspace.mkdir()
        (self.workspace / "README.md").write_text("gateway actor\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(self.workspace), "init", "--quiet"], check=True)
        subprocess.run(
            ["git", "-C", str(self.workspace), "config", "user.name", "CostMarshal Test"],
            check=True,
        )
        subprocess.run(
            ["git", "-C", str(self.workspace), "config", "user.email", "test@example.invalid"],
            check=True,
        )
        subprocess.run(["git", "-C", str(self.workspace), "add", "README.md"], check=True)
        subprocess.run(
            ["git", "-C", str(self.workspace), "commit", "--quiet", "-m", "base"],
            check=True,
        )
        self.codex_home = self.temp / "codex-home"
        self.codex_home.mkdir()
        (self.codex_home / "longcat.config.toml").write_text(
            "model_provider = 'longcat'\n"
            "model = 'LongCat-2.0'\n"
            "web_search = 'disabled'\n"
            "[model_providers.longcat]\n"
            "name = 'LongCat'\n"
            "base_url = 'https://api.longcat.chat/openai/v1'\n"
            "wire_api = 'responses'\n"
            "env_key = 'LONGCAT_API_KEY'\n",
            encoding="utf-8",
        )
        secrets_file = self.temp / "providers.env"
        secrets_file.write_text(
            f"LONGCAT_API_KEY={RAW_PROVIDER_SECRET}\n",
            encoding="utf-8",
        )
        project_dir = Path(
            self.cli(
                "init",
                "--name",
                "gateway-actor",
                "--objective",
                "prove lease-only required worker",
                "--workspace",
                str(self.workspace),
                "--backend",
                "local",
                "--governance",
                "off",
                "--allow-unsafe-native-workers",
                "--secrets-file",
                str(secrets_file),
            )["project"]
        )
        self.cli(
            "new-task",
            "--project",
            str(project_dir),
            "--title",
            "gateway task",
            "--purpose",
            "execute through production proxy",
            "--estimated-input-tokens",
            "50000",
            "--estimated-output-tokens",
            "10000",
        )
        dispatched = self.cli(
            "dispatch",
            "--project",
            str(project_dir),
            "--task",
            "V2-0001",
            "--unsafe-native",
        )
        self.layout = ProjectLayout(root=self.runtime, project_dir=project_dir)
        self.project = load_project(self.layout)
        self.actor = load_actor(self.layout, dispatched["actor_id"])
        self.actor["isolation"] = {
            "mode": "required",
            "attestation": {
                "schema": "costmarshal-worker-isolation-attestation-v1",
                "backend": "docker",
                "image": IMAGE,
                "image_digest": "sha256:" + "a" * 64,
                "strong_isolation": True,
            },
            "execution": {
                "engine": "docker",
                "image": IMAGE,
                "network_mode": "provider-proxy",
                "network_name": "costmarshal-provider-proxy",
                "workspace_mode": "ro",
                "limits": {
                    "memory_mb": 512,
                    "cpus": 1,
                    "pids": 64,
                    "timeout_seconds": 10,
                    "tmpfs_mb": 32,
                    "home_tmpfs_mb": 32,
                },
            },
        }
        save_actor(self.layout, self.actor)
        task = load_task(self.layout, "V2-0001")
        task["attempts"][-1]["reserved_cost_cny"] = "1.000000000"
        save_task(self.layout, task)
        evidence = {
            "real_provider_backtest": "ART-backtest",
            "live_oci_adversarial": "ART-oci",
            "credential_broker_attestation": "ART-broker",
            "provider_proxy_budget_attestation": "ART-budget",
            "schema_drift_monitor": "ART-schema",
        }
        boundary = build_production_boundary(
            mode="enforced",
            broker_endpoint="https://broker.example/v1/leases",
            broker_identity="spiffe://example.test/costmarshal/worker",
            provider_proxy_endpoint="https://provider-proxy:9443/v1",
            hard_budget_enforced=True,
            evidence_artifact_ids=evidence,
            runtime_adapter="costmarshal-gateway-v1",
            gateway_policy_sha256="sha256:" + "b" * 64,
            deployment_commit="a" * 40,
            release_version="v4.2.0",
            gateway_image="example/gateway@sha256:" + "c" * 64,
            allowed_signers_sha256="sha256:" + "d" * 64,
            signer_identities=["release@example.test"],
        )
        atomic_write_json(self.layout.production_boundary_json, boundary)
        self.cert = self.temp / "client-cert.pem"
        self.key = self.temp / "client-key.pem"
        self.ca = self.temp / "provider-ca.pem"
        for path in (self.cert, self.key, self.ca):
            path.write_text("test fixture\n", encoding="utf-8")
        self.execution_workspace = self.temp / "execution-workspace"
        self.execution_workspace.mkdir()

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_required_worker_receives_lease_proxy_profile_and_ca_only(self) -> None:
        lease = {
            "schema_version": "costmarshal-provider-lease-v1",
            "lease_token": LEASE_TOKEN,
            "token_type": "Bearer",
            "expires_at": 2_000_000_000,
            "lease_id": "LSE-fixture-00000001",
            "provider": "longcat",
            "model": "LongCat-2.0",
            "budget_nano_cny": 1_000_000_000,
            "replayed": False,
        }
        environment = {
            "COSTMARSHAL_BROKER_CLIENT_CERT_FILE": str(self.cert),
            "COSTMARSHAL_BROKER_CLIENT_KEY_FILE": str(self.key),
            "COSTMARSHAL_BROKER_CA_FILE": str(self.ca),
        }
        with patch.dict(os.environ, environment, clear=False), patch(
            "costmarshal_v2.actor_runner.request_provider_lease",
            return_value=lease,
        ) as broker:
            spec, command, redactions = _required_worker_bundle(
                self.layout,
                self.project,
                self.actor,
                execution_workspace=self.execution_workspace,
                workspace_mode="read-only",
            )
        self.assertEqual(command, ["costmarshal-worker", "--jsonl", "--model", "LongCat-2.0"])
        self.assertEqual(spec.provider_ca_path, self.ca.resolve())
        self.assertIsNotNone(spec.credential_path)
        self.assertEqual(spec.credential_path.read_text(encoding="utf-8"), LEASE_TOKEN)
        self.assertIn(LEASE_TOKEN, redactions)
        self.assertNotIn(RAW_PROVIDER_SECRET, redactions)
        profile = spec.profile_path.read_text(encoding="utf-8")
        self.assertIn("https://provider-proxy:9443/v1", profile)
        self.assertNotIn("api.longcat.chat", profile)
        self.assertEqual(
            spec.profile_sha256,
            hashlib.sha256(profile.encode("utf-8")).hexdigest(),
        )
        request = broker.call_args.kwargs
        self.assertEqual(request["provider"], "longcat")
        self.assertEqual(request["model"], "LongCat-2.0")
        self.assertEqual(request["budget_nano_cny"], 1_000_000_000)
        self.assertEqual(request["max_input_tokens"], 50000)
        self.assertEqual(request["max_output_tokens"], 10000)
        persisted = load_actor(self.layout, self.actor["id"])
        provider_lease = persisted["runtime"]["provider_lease"]
        self.assertEqual(provider_lease["lease_id"], lease["lease_id"])
        serialized = json.dumps(persisted)
        self.assertNotIn(LEASE_TOKEN, serialized)
        self.assertNotIn(RAW_PROVIDER_SECRET, serialized)


if __name__ == "__main__":
    unittest.main()
