#!/usr/bin/env python3
"""v4 repository, Workstream, integration, and production-boundary contracts."""

from __future__ import annotations

from copy import deepcopy
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from costmarshal_v2.cli import build_parser  # noqa: E402
from costmarshal_v2.large_project import (  # noqa: E402
    LargeProjectError,
    append_workstream,
    build_integration_gate,
    build_integration_plan,
    build_production_boundary,
    build_repository,
    build_workstream,
    empty_repository_registry,
    empty_workstream_registry,
    enforce_workstream_budget,
    production_status,
    upsert_repository,
    validate_integration_gate,
    validate_integration_plan,
    verify_repository_binding,
    workstream_dispatch_blockers,
    workstream_statuses,
)


def git(repository: Path, *args: str) -> str:
    return subprocess.check_output(
        ["git", "-C", str(repository), *args],
        text=True,
        stderr=subprocess.STDOUT,
    ).strip()


def make_repository(parent: Path, name: str) -> tuple[Path, str]:
    repository = parent / name
    repository.mkdir()
    git(repository, "init")
    git(repository, "config", "user.name", "CostMarshal Test")
    git(repository, "config", "user.email", "costmarshal-test@example.invalid")
    (repository / "README.md").write_text(f"# {name}\n", encoding="utf-8")
    git(repository, "add", "README.md")
    git(repository, "commit", "-m", "initial")
    return repository.resolve(), git(repository, "rev-parse", "HEAD")


def accepted_task(
    task_id: str,
    *,
    workstream_id: str,
    repository_id: str,
) -> dict:
    return {
        "id": task_id,
        "status": "done",
        "workstream_id": workstream_id,
        "repository_id": repository_id,
        "leader_result": {
            "status": "done",
            "accepted_by_leader": True,
        },
    }


class V4LargeProjectContractTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="costmarshal-v4-")
        self.root = Path(self.temp.name)
        self.repo_a_path, self.head_a = make_repository(self.root, "repo-a")
        self.repo_b_path, self.head_b = make_repository(self.root, "repo-b")
        self.repo_a = build_repository(
            repository_id="api",
            path=self.repo_a_path,
            role="service",
            is_default=True,
        )
        self.repo_b = build_repository(
            repository_id="web",
            path=self.repo_b_path,
            role="client",
        )
        registry = upsert_repository(empty_repository_registry(), self.repo_a)
        self.repositories = upsert_repository(registry, self.repo_b)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def _workstreams(self) -> dict:
        registry = empty_workstream_registry()
        foundation = build_workstream(
            workstream_id="foundation",
            name="Foundation",
            objective="Freeze the shared interface.",
            repository_ids=["api"],
            depends_on=[],
            budget_cny="5",
            concurrency_limit=1,
            registry=self.repositories,
            existing=registry,
            project_budget_cny="10",
        )
        registry = append_workstream(
            registry,
            foundation,
            repository_registry=self.repositories,
        )
        delivery = build_workstream(
            workstream_id="delivery",
            name="Delivery",
            objective="Integrate the client and service.",
            repository_ids=["api", "web"],
            depends_on=["foundation"],
            budget_cny="5",
            concurrency_limit=1,
            registry=self.repositories,
            existing=registry,
            project_budget_cny="10",
        )
        return append_workstream(
            registry,
            delivery,
            repository_registry=self.repositories,
        )

    def test_repository_identity_is_source_preserving_and_reports_head_drift(
        self,
    ) -> None:
        self.assertFalse(self.repo_a["source_mutation"])
        self.assertEqual(
            verify_repository_binding(self.repo_a)["current_head"],
            self.head_a,
        )
        (self.repo_a_path / "change.txt").write_text("change\n", encoding="utf-8")
        git(self.repo_a_path, "add", "change.txt")
        git(self.repo_a_path, "commit", "-m", "change")
        inspection = verify_repository_binding(self.repo_a)
        self.assertFalse(inspection["head_matches_registration"])
        self.assertEqual(self.repo_a["registered_head"], self.head_a)

    def test_legacy_default_may_be_a_git_subdirectory_but_explicit_repo_may_not(
        self,
    ) -> None:
        nested = self.repo_b_path / "workspace"
        nested.mkdir()
        default = build_repository(
            repository_id="default",
            path=nested,
            role="primary",
            require_git=False,
            is_default=True,
        )
        self.assertEqual(default["kind"], "unverified-default")
        with self.assertRaisesRegex(LargeProjectError, "Git repository root"):
            build_repository(
                repository_id="nested",
                path=nested,
                role="component",
                require_git=True,
            )

    def test_workstream_dependency_concurrency_and_budget_are_fail_closed(
        self,
    ) -> None:
        registry = self._workstreams()
        task = {
            "id": "T-delivery",
            "workstream_id": "delivery",
            "repository_id": "web",
            "status": "planned",
        }
        blockers = workstream_dispatch_blockers(
            task=task,
            tasks=[task],
            workstream_registry=registry,
            repository_registry=self.repositories,
            accepted_gate_workstream_ids=[],
        )
        self.assertIn("lacks an accepted integration Gate", blockers[0])
        active = {
            **task,
            "id": "T-active",
            "status": "running",
        }
        blockers = workstream_dispatch_blockers(
            task=task,
            tasks=[task, active],
            workstream_registry=registry,
            repository_registry=self.repositories,
            accepted_gate_workstream_ids=["foundation"],
        )
        self.assertIn("concurrency quota is full", blockers[0])
        with self.assertRaisesRegex(LargeProjectError, "budget exceeded"):
            enforce_workstream_budget(
                task=task,
                tasks=[task, active],
                workstream_registry=registry,
                repository_registry=self.repositories,
                task_commitment_cny={"T-active": "3"},
                projected_task_commitment_cny="3",
            )
        statuses = workstream_statuses(
            registry,
            repository_registry=self.repositories,
            tasks=[task, active],
            accepted_gate_workstream_ids=["foundation"],
        )
        self.assertEqual(statuses["foundation"]["state"], "accepted")
        self.assertEqual(statuses["delivery"]["state"], "active")

    def test_staged_integration_gate_detects_repository_head_drift(self) -> None:
        workstreams = self._workstreams()
        tasks = [
            accepted_task(
                "T-api",
                workstream_id="delivery",
                repository_id="api",
            ),
            accepted_task(
                "T-web",
                workstream_id="delivery",
                repository_id="web",
            ),
        ]
        artifacts = [
            {
                "artifact_id": "ART-interface",
                "kind": "interface",
                "lifecycle": "accepted",
            }
        ]
        plan = build_integration_plan(
            project_id="P-1",
            command_id="CMD-plan",
            milestone="M1",
            workstream_ids=["delivery"],
            task_ids=["T-api", "T-web"],
            repository_ids=["api", "web"],
            interface_artifact_ids=["ART-interface"],
            rollback_refs={"api": self.head_a, "web": self.head_b},
            repository_registry=self.repositories,
            workstream_registry=workstreams,
            tasks=tasks,
            artifact_rows=artifacts,
        )
        self.assertFalse(plan["atomic_cross_repository"])
        self.assertFalse(plan["source_mutation"])
        self.assertEqual(validate_integration_plan(plan), plan)
        gate = build_integration_gate(
            plan=plan,
            command_id="CMD-gate",
            tasks=tasks,
            artifact_rows=artifacts,
            repository_registry=self.repositories,
            approved_by="leader",
        )
        self.assertTrue(gate["passed"])
        self.assertEqual(validate_integration_gate(gate, plan=plan), gate)

        (self.repo_b_path / "drift.txt").write_text("drift\n", encoding="utf-8")
        git(self.repo_b_path, "add", "drift.txt")
        git(self.repo_b_path, "commit", "-m", "drift")
        drifted = build_integration_gate(
            plan=plan,
            command_id="CMD-gate-drift",
            tasks=tasks,
            artifact_rows=artifacts,
            repository_registry=self.repositories,
            approved_by="leader",
        )
        self.assertFalse(drifted["passed"])
        self.assertFalse(
            next(
                check
                for check in drifted["checks"]
                if check["name"] == "repository-head:web"
            )["passed"]
        )

    def test_production_boundary_is_secret_free_and_honestly_blocked(self) -> None:
        evidence = {
            "real_provider_backtest": "ART-backtest",
            "live_oci_adversarial": "ART-oci",
            "credential_broker_attestation": "ART-broker",
            "provider_proxy_budget_attestation": "ART-budget",
            "schema_drift_monitor": "ART-schema",
        }
        with self.assertRaisesRegex(LargeProjectError, "secret-free HTTPS"):
            build_production_boundary(
                mode="enforced",
                broker_endpoint="https://user:secret@broker.example/v1",
                broker_identity="spiffe://example/costmarshal",
                provider_proxy_endpoint="https://proxy.example/v1",
                hard_budget_enforced=True,
                evidence_artifact_ids=evidence,
            )
        boundary = build_production_boundary(
            mode="enforced",
            broker_endpoint="https://broker.example/v1",
            broker_identity="spiffe://example/costmarshal",
            provider_proxy_endpoint="https://proxy.example/v1",
            hard_budget_enforced=True,
            evidence_artifact_ids=evidence,
        )
        artifacts = [
            {"artifact_id": artifact_id, "lifecycle": "accepted"}
            for artifact_id in evidence.values()
        ]
        status = production_status(
            boundary=boundary,
            artifact_rows=artifacts,
            sqlite_authoritative=True,
            worker_isolation={
                "image": "example/worker@sha256:" + "a" * 64,
                "network_mode": "provider-proxy",
            },
        )
        self.assertEqual(status["status"], "blocked")
        self.assertFalse(status["external_certification"])
        adapter = next(
            check
            for check in status["checks"]
            if check["name"] == "external-broker-runtime-adapter"
        )
        self.assertFalse(adapter["passed"])

    def test_cli_exposes_v4_commands_and_task_ownership(self) -> None:
        parser = build_parser()
        register = parser.parse_args(
            [
                "--root",
                str(self.root),
                "register-repository",
                "--project",
                "P-1",
                "--repository-id",
                "api",
                "--path",
                str(self.repo_a_path),
            ]
        )
        self.assertEqual(register.repository_id, "api")
        task = parser.parse_args(
            [
                "--root",
                str(self.root),
                "new-task",
                "--project",
                "P-1",
                "--repository",
                "api",
                "--workstream",
                "delivery",
                "--title",
                "Task",
                "--purpose",
                "Purpose",
            ]
        )
        self.assertEqual(task.repository, "api")
        self.assertEqual(task.workstream, "delivery")

    def test_cli_persists_and_validates_a_workstream(self) -> None:
        runtime = self.root / "runtime"
        cli = ROOT / "scripts" / "costmarshal.py"
        env = {
            **os.environ,
            "CODEX_HOME": str(self.root / "codex-home"),
        }

        def run(*arguments: str) -> dict:
            completed = subprocess.run(
                [
                    sys.executable,
                    str(cli),
                    "--root",
                    str(runtime),
                    *arguments,
                ],
                cwd=str(ROOT),
                env=env,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )
            if completed.returncode:
                self.fail(
                    f"CLI failed: {' '.join(arguments)}\n"
                    f"stdout:\n{completed.stdout}\nstderr:\n{completed.stderr}"
                )
            return json.loads(completed.stdout)

        initialized = run(
            "init",
            "--name",
            "v4-contract",
            "--objective",
            "Verify v4 contracts.",
            "--workspace",
            str(self.repo_a_path),
            "--backend",
            "local",
            "--governance",
            "off",
            "--allow-unsafe-native-workers",
        )
        project = initialized["project"]
        migrated = run(
            "migrate-state",
            "--project",
            project,
            "--apply",
        )
        self.assertEqual(migrated["status"], "enabled")
        created = run(
            "create-workstream",
            "--project",
            project,
            "--workstream-id",
            "core",
            "--name",
            "Core",
            "--objective",
            "Build the core.",
            "--repository",
            "default",
            "--budget-cny",
            "1",
            "--command-id",
            "CMD-v4-workstream",
        )
        self.assertEqual(created["workstream"]["workstream_id"], "core")
        task = run(
            "new-task",
            "--project",
            project,
            "--repository",
            "default",
            "--workstream",
            "core",
            "--title",
            "Bound task",
            "--purpose",
            "Prove repository and Workstream ownership.",
            "--provider",
            "codex",
            "--tier",
            "high",
            "--command-id",
            "CMD-v4-task",
        )
        task_document = json.loads(
            (
                Path(task["task"]) / "task.json"
            ).read_text(encoding="utf-8")
        )
        self.assertEqual(task_document["repository_id"], "default")
        self.assertEqual(task_document["workstream_id"], "core")
        listed = run("workstreams", "--project", project)
        self.assertEqual(listed["statuses"]["core"]["state"], "ready")
        configured = run(
            "configure-production-boundary",
            "--project",
            project,
            "--mode",
            "report-only",
            "--broker-endpoint",
            "https://broker.example/v1",
            "--broker-identity",
            "spiffe://example/costmarshal",
            "--provider-proxy-endpoint",
            "https://proxy.example/v1",
            "--hard-budget-enforced",
            "--evidence-artifact",
            "real_provider_backtest=ART-backtest",
            "--evidence-artifact",
            "live_oci_adversarial=ART-oci",
            "--evidence-artifact",
            "credential_broker_attestation=ART-broker",
            "--evidence-artifact",
            "provider_proxy_budget_attestation=ART-budget",
            "--evidence-artifact",
            "schema_drift_monitor=ART-schema",
            "--apply",
            "--command-id",
            "CMD-v4-production-boundary",
        )
        self.assertEqual(configured["mode"], "apply")
        production = run("production-status", "--project", project)
        self.assertEqual(production["status"], "blocked")
        self.assertFalse(production["external_certification"])
        validated = run("validate", "--project", project)
        self.assertEqual(validated["status"], "ok")

    def test_tampering_breaks_integration_hashes(self) -> None:
        workstreams = self._workstreams()
        task = accepted_task(
            "T-api",
            workstream_id="foundation",
            repository_id="api",
        )
        plan = build_integration_plan(
            project_id="P-1",
            command_id="CMD-plan-tamper",
            milestone="M0",
            workstream_ids=["foundation"],
            task_ids=["T-api"],
            repository_ids=["api"],
            interface_artifact_ids=[],
            rollback_refs={"api": self.head_a},
            repository_registry=self.repositories,
            workstream_registry=workstreams,
            tasks=[task],
            artifact_rows=[],
        )
        tampered = deepcopy(plan)
        tampered["atomic_cross_repository"] = True
        with self.assertRaisesRegex(LargeProjectError, "hash or safety"):
            validate_integration_plan(tampered)


if __name__ == "__main__":
    unittest.main()
