#!/usr/bin/env python3
"""Result evidence must remain bound to the actor-sealed semantic output."""

from __future__ import annotations

import hashlib
import json
import shutil
import sys
import tempfile
import unittest
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from costmarshal_v2.handoff_contract import (  # noqa: E402
    HandoffContractError,
    HandoffLimits,
    build_attempt_input_contract,
    build_attempt_output_contract,
    build_bound_prompt_bytes,
    build_collaboration_contract,
    build_prompt_binding,
)
from costmarshal_v2.actor_runner import _prepare_semantic_attempt  # noqa: E402
from costmarshal_v2.paths import ProjectLayout, slugify  # noqa: E402
from costmarshal_v2.routing import (  # noqa: E402
    acceptance_evidence_provenance,
    provider_price_basis,
    route_plan_fingerprint,
)
from costmarshal_v2.scheduler import (  # noqa: E402
    RESULT_EVIDENCE_SCHEMA,
    _apply_leader_result_binding,
    _bind_rejected_attempt_handoff,
    _result_request_contract,
    audit_result_evidence,
    build_rejected_attempt_handoff,
    execute_scheduler_command,
    validate_envelope_dispatch_step,
)


HANDOFF_TEXT = "Low tier completed parsing; stronger review is still required."


def digest(label: str) -> str:
    return "sha256:" + hashlib.sha256(label.encode("utf-8")).hexdigest()


def canonical_bytes(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


class ResultAttemptOutputBindingTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = Path(tempfile.mkdtemp(prefix="costmarshal-output-binding-"))
        self.layout = ProjectLayout(
            root=self.temp / "runtime",
            project_dir=self.temp / "runtime" / "projects" / "binding",
        )
        self.layout.project_dir.mkdir(parents=True)
        self.layout.project_json.write_text(
            json.dumps({"schema_version": 2, "project_id": "binding"}) + "\n",
            encoding="utf-8",
        )
        (self.layout.tasks_dir / "V2-0001").mkdir(parents=True)
        self.layout.reports_dir.mkdir(parents=True)
        self.task, self.attempt, self.result = self._fixture()
        self._write_task(self.task)
        self.layout.results_jsonl.write_text(
            json.dumps(self.result, ensure_ascii=False, separators=(",", ":")) + "\n",
            encoding="utf-8",
        )

    def tearDown(self) -> None:
        shutil.rmtree(self.temp, ignore_errors=True)

    @staticmethod
    def _route_step(index: int, tier: str) -> dict[str, object]:
        profile_sha256 = digest(f"{tier}-profile")
        return {
            "index": index,
            "provider_id": f"{tier}-api",
            "tier": tier,
            "model": f"{tier}-model",
            "profile": f"{tier}-profile",
            "execution_identity": {
                "model": f"{tier}-model",
                "profile": f"{tier}-profile",
                "profile_sha256": profile_sha256,
            },
            "estimated_cost_cny": str(index + 1),
            "acceptance_prior": {"probability": "0.5", "source": "test"},
            "price_basis": {"kind": "test"},
            "profile_binding": {
                "schema_version": "costmarshal-profile-binding-v1",
                "status": "available",
                "logical_name": f"{tier}-profile",
                "source_kind": "named-profile",
                "sha256": profile_sha256,
                "size_bytes": 100,
                "provider_identity": f"{tier}-api",
                "base_url": "https://example.invalid/v1",
                "wire_api": "responses",
                "env_key": f"{tier.upper()}_API_KEY",
                "model": f"{tier}-model",
                "snapshot_relpath": f"profiles/ENV-001/{tier}.toml",
            },
        }

    def _fixture(self) -> tuple[dict[str, object], dict[str, object], dict[str, object]]:
        steps = [
            self._route_step(0, "low"),
            self._route_step(1, "medium"),
            self._route_step(2, "high"),
        ]
        contract = build_collaboration_contract(
            task_id="V2-0001",
            task_spec={"title": "bind output", "purpose": "test result evidence"},
            base_sha="a" * 40,
            context_allowlist=["src/parser.py"],
            context_manifest_sha256=digest("context"),
            context_file_count=1,
            context_total_size_bytes=100,
            write_scope=["src/parser.py"],
            initial_change_manifest_sha256=digest("empty-changes"),
            max_changes=10,
            max_total_upsert_bytes=10_000,
            estimated_input_tokens=20_000,
            estimated_cached_input_tokens=0,
            estimated_output_tokens=10_000,
            handoff_limits=HandoffLimits(
                max_handoff_bytes=4096,
                continuation_input_reserve_tokens=10_240,
                handoff_output_reserve_tokens=4096,
                prompt_framing_reserve_tokens=2048,
                max_route_steps=3,
            ),
            route_envelope_id="ENV-001",
            route_plan_fingerprint_sha256=route_plan_fingerprint(
                steps,
                input_tokens=20_000,
                cached_input_tokens=0,
                output_tokens=10_000,
            ),
            planned_steps=steps,
        )
        attempt_input = build_attempt_input_contract(
            collaboration_contract=contract,
            attempt_id="ATT-low-001",
            actor_id="agent-v2-0001",
            route_step_index=0,
            incoming_change_manifest_sha256=digest("empty-changes"),
            incoming_change_count=0,
            incoming_total_upsert_bytes=0,
        )
        prompt = build_bound_prompt_bytes(
            attempt_input=attempt_input,
            task_prompt_bytes=b"Perform the bounded low-tier work.",
        )
        prompt_binding = build_prompt_binding(
            collaboration_contract=contract,
            attempt_input=attempt_input,
            prompt_bytes=prompt,
        )
        prompt_dir = (
            self.layout.root
            / "semantic-prompts"
            / "binding"
            / slugify("ATT-low-001", "attempt")
        )
        prompt_dir.mkdir(parents=True)
        prompt_path = prompt_dir / prompt_binding["prompt_sha256"].removeprefix("sha256:")
        prompt_path.write_bytes(prompt)
        prompt_receipt = {
            "schema": "costmarshal-semantic-prompt-receipt-v1",
            "path": str(prompt_path.resolve()),
            "sha256": prompt_binding["prompt_sha256"],
            "size_bytes": len(prompt),
            "binding_sha256": prompt_binding["binding_sha256"],
            "attempt_input_sha256": attempt_input["attempt_input_sha256"],
            "collaboration_contract_sha256": contract["contract_sha256"],
        }
        report = b"# Completion Report\n\nStatus: escalate\n"
        report_sha256 = hashlib.sha256(report).hexdigest()
        execution_body = {
            "schema_version": 1,
            "kind": "costmarshal-execution-receipt",
            "task_id": "V2-0001",
            "attempt_id": "ATT-low-001",
            "actor_id": "agent-v2-0001",
            "provider": "low-api",
            "tier": "low",
            "model": "low-model",
            "profile": "low-profile",
            "profile_sha256": digest("low-profile"),
            "context_projection_manifest_sha256": digest("projection"),
            "incoming_change_manifest_sha256": digest("empty-changes"),
            "semantic_prompt_sha256": prompt_binding["prompt_sha256"],
            "isolation_backend": "test-oci",
            "container_name": "sealed-test",
            "container_id": "container-test",
            "isolation_attestation": {"test": True},
            "provider_exit_code": 1,
        }
        execution_sha256 = "sha256:" + hashlib.sha256(
            canonical_bytes(execution_body)
        ).hexdigest()
        receipt_dir = (
            self.layout.root
            / "execution-receipts"
            / "binding"
            / slugify("ATT-low-001", "attempt")
        )
        receipt_dir.mkdir(parents=True)
        receipt_path = receipt_dir / execution_sha256.removeprefix("sha256:")
        receipt_path.write_bytes(canonical_bytes(execution_body))
        execution_receipt = {
            **execution_body,
            "receipt_sha256": execution_sha256,
            "path": str(receipt_path),
        }
        attempt_output = build_attempt_output_contract(
            collaboration_contract=contract,
            attempt_input=attempt_input,
            prompt_binding=prompt_binding,
            execution_receipt_sha256=execution_sha256,
            report_sha256="sha256:" + report_sha256,
            report_size_bytes=len(report),
            outgoing_change_manifest_sha256=digest("empty-changes"),
            outgoing_change_count=0,
            outgoing_total_upsert_bytes=0,
        )
        attempt: dict[str, object] = {
            "attempt_id": "ATT-low-001",
            "actor_id": "agent-v2-0001",
            "provider": "low-api",
            "tier": "low",
            "model": "low-model",
            "profile": "low-profile",
            "profile_binding": {"sha256": digest("low-profile")},
            "execution_identity": {
                "model": "low-model",
                "profile": "low-profile",
                "profile_sha256": digest("low-profile"),
            },
            "isolation": {"mode": "required"},
            "route_envelope_id": "ENV-001",
            "route_plan_fingerprint": contract["route_policy"]["plan_fingerprint"],
            "route_plan_step_index": 0,
            "route_predecessors": [],
            "context_projection": {"manifest_sha256": digest("projection")},
            "provider_exit_code": 1,
            "status": "escalate",
            "report_path": "tasks/V2-0001/completion-report.md",
            "report_sha256": report_sha256,
            "report_size": len(report),
            "attempt_input": attempt_input,
            "semantic_prompt_binding": prompt_binding,
            "semantic_prompt": prompt_receipt,
            "execution_receipt": execution_receipt,
            "attempt_output": attempt_output,
            "attempt_output_sha256": attempt_output["attempt_output_sha256"],
        }
        args = SimpleNamespace(
            status="escalate",
            accepted_by_leader=False,
            quality_score=2,
            actor=None,
            agent=None,
            model=None,
            input_tokens=0,
            cached_input_tokens=0,
            output_tokens=0,
            estimated_cost_cny=None,
            summary="low tier needs escalation",
            handoff=HANDOFF_TEXT,
            note="",
        )
        request, request_sha256 = _result_request_contract(
            args,
            command_id="CMD-result-001",
            task_id="V2-0001",
            attempt_id="ATT-low-001",
            attempt_output_sha256=attempt_output["attempt_output_sha256"],
            attempt_output_boundary="sealed-required",
            task_type="analysis",
            difficulty="normal",
        )
        result: dict[str, object] = {
            "id": "RES-result-001",
            "command_id": "CMD-result-001",
            "event_type": "result",
            "evidence_schema_version": RESULT_EVIDENCE_SCHEMA,
            "project_id": "binding",
            "request_contract": request,
            "request_contract_sha256": request_sha256,
            "timestamp": "2026-07-16T00:00:00Z",
            "task_id": "V2-0001",
            "task_type": "analysis",
            "difficulty": "normal",
            "attempt_id": "ATT-low-001",
            "route_envelope_id": "ENV-001",
            "route_plan_fingerprint": contract["route_policy"]["plan_fingerprint"],
            "route_plan_step_index": 0,
            "route_predecessors": [],
            "actor_id": "agent-v2-0001",
            "agent": "agent-v2-0001",
            "provider": "low-api",
            "tier": "low",
            "profile": "low-profile",
            "profile_sha256": digest("low-profile"),
            "execution_model": "low-model",
            "model": "low-model",
            "status": "escalate",
            "accepted_by_leader": False,
            "quality_score": 2,
            "summary": "low tier needs escalation",
            "attempt_output_sha256": attempt_output["attempt_output_sha256"],
            "attempt_output_boundary": "sealed-required",
            "report_path": attempt["report_path"],
            "report_sha256": attempt_output["report_receipt"]["sha256"],
            "report_size": len(report),
        }
        task: dict[str, object] = {
            "id": "V2-0001",
            "task_type": "analysis",
            "difficulty": "normal",
            "handoff_contract": contract,
            "attempts": [attempt],
        }
        _apply_leader_result_binding(task, attempt, result)
        _bind_rejected_attempt_handoff(
            task=task,
            attempt=attempt,
            trusted_result=result,
            handoff_text=HANDOFF_TEXT,
        )
        return task, attempt, result

    def _write_task(self, task: dict[str, object]) -> None:
        (self.layout.tasks_dir / "V2-0001" / "task.json").write_text(
            json.dumps(task, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )

    def test_trusted_result_is_rejected_after_output_mutation(self) -> None:
        trusted, issues = audit_result_evidence(self.layout)
        self.assertEqual(trusted, [self.result])
        self.assertEqual(issues, [])

        mutated = deepcopy(self.task)
        mutated["attempts"][0]["attempt_output"]["outgoing_changes"][
            "change_count"
        ] = 1
        self._write_task(mutated)
        trusted, issues = audit_result_evidence(self.layout)
        self.assertEqual(trusted, [])
        self.assertTrue(
            any("sealed required attempt output is invalid" in issue for issue in issues),
            issues,
        )

    def test_result_cannot_move_evidence_between_task_or_difficulty_buckets(self) -> None:
        for field, forged_value in (
            ("task_type", "coding"),
            ("difficulty", "hard"),
        ):
            with self.subTest(field=field):
                forged = deepcopy(self.result)
                forged[field] = forged_value
                self.layout.results_jsonl.write_text(
                    json.dumps(forged, ensure_ascii=False, separators=(",", ":"))
                    + "\n",
                    encoding="utf-8",
                )
                trusted, issues = audit_result_evidence(self.layout)
                self.assertEqual(trusted, [])
                self.assertTrue(
                    any(f"{field} does not match attempt binding" in issue for issue in issues),
                    issues,
                )

    def test_result_cannot_cross_project_boundary(self) -> None:
        for forged_project in (None, "foreign-project"):
            with self.subTest(project_id=forged_project):
                forged = deepcopy(self.result)
                if forged_project is None:
                    forged.pop("project_id")
                else:
                    forged["project_id"] = forged_project
                self.layout.results_jsonl.write_text(
                    json.dumps(forged, ensure_ascii=False, separators=(",", ":"))
                    + "\n",
                    encoding="utf-8",
                )
                trusted, issues = audit_result_evidence(self.layout)
                self.assertEqual(trusted, [])
                self.assertTrue(
                    any("project_id does not match" in issue for issue in issues),
                    issues,
                )

    def test_prompt_cas_is_required_and_hash_bound(self) -> None:
        for mutation in ("missing", "tampered"):
            with self.subTest(mutation=mutation):
                mutated = deepcopy(self.task)
                receipt = mutated["attempts"][0]["semantic_prompt"]
                prompt_path = Path(receipt["path"])
                original = prompt_path.read_bytes() if prompt_path.exists() else b""
                if mutation == "missing":
                    prompt_path.unlink()
                else:
                    prompt_path.write_bytes(original + b"tamper")
                self._write_task(mutated)
                trusted, issues = audit_result_evidence(self.layout)
                self.assertEqual(trusted, [])
                self.assertTrue(
                    any("semantic prompt receipt" in issue for issue in issues),
                    issues,
                )
                prompt_path.parent.mkdir(parents=True, exist_ok=True)
                prompt_path.write_bytes(original)
                self._write_task(self.task)

    def test_impossible_leader_decision_is_not_routing_evidence(self) -> None:
        forged_task = deepcopy(self.task)
        forged_result = deepcopy(self.result)
        forged_result["status"] = "done"
        forged_result["accepted_by_leader"] = False
        request = forged_result["request_contract"]
        request["status"] = "done"
        request["accepted_by_leader"] = False
        request_sha256 = "sha256:" + hashlib.sha256(canonical_bytes(request)).hexdigest()
        forged_result["request_contract_sha256"] = request_sha256
        forged_attempt = forged_task["attempts"][0]
        forged_attempt["recorded_result_status"] = "done"
        forged_attempt["accepted_by_leader"] = False
        forged_attempt["result_request_contract_sha256"] = request_sha256
        self._write_task(forged_task)
        self.layout.results_jsonl.write_text(
            json.dumps(forged_result, ensure_ascii=False, separators=(",", ":")) + "\n",
            encoding="utf-8",
        )
        trusted, issues = audit_result_evidence(self.layout)
        self.assertEqual(trusted, [])
        self.assertTrue(
            any("leader acceptance must be true exactly" in issue for issue in issues),
            issues,
        )

    def test_continuation_requires_its_admitted_evidence_to_remain_trusted(self) -> None:
        evidence_ids, evidence_sha256 = acceptance_evidence_provenance([self.result])
        provider = {
            "provider_id": "low-api",
            "tier": "low",
            "input_cny_per_1m": "1",
            "output_cny_per_1m": "1",
        }
        step = {
            "provider_id": "low-api",
            "tier": "low",
            "price_basis": provider_price_basis(provider),
            "estimated_cost_cny": "1",
            "acceptance_prior": {
                "observations": 1,
                "evidence_result_ids": list(evidence_ids),
                "evidence_sha256": evidence_sha256,
            },
        }
        task = {"id": "V2-0001", "attempts": []}
        envelope = {
            "planned_steps": [step],
            "envelope_id": "ENV-evidence-freshness",
            "estimated_input_tokens": 1_000_000,
            "estimated_cached_input_tokens": 0,
            "estimated_output_tokens": 0,
        }
        decision = SimpleNamespace(
            provider_id="low-api",
            tier="low",
            planned_steps=(step,),
            estimated_input_tokens=1_000_000,
            estimated_cached_input_tokens=0,
            estimated_output_tokens=0,
        )
        accepted_index, _ = validate_envelope_dispatch_step(
            task,
            envelope,
            decision,
            provider,
            trusted_history=[self.result],
        )
        self.assertEqual(accepted_index, 0)
        future_result = deepcopy(self.result)
        future_result["id"] = "RES-future-evidence"
        future_ids, future_sha256 = acceptance_evidence_provenance([future_result])
        future_step = {
            **step,
            "provider_id": "high-api",
            "tier": "high",
            "acceptance_prior": {
                "observations": 1,
                "evidence_result_ids": list(future_ids),
                "evidence_sha256": future_sha256,
            },
        }
        envelope["planned_steps"] = [step, future_step]
        with self.assertRaisesRegex(ValueError, "step 1 acceptance evidence is no longer trusted"):
            validate_envelope_dispatch_step(
                task,
                envelope,
                decision,
                provider,
                trusted_history=[self.result],
            )
        envelope["planned_steps"] = [step]
        with self.assertRaisesRegex(ValueError, "no longer trusted"):
            validate_envelope_dispatch_step(
                task,
                envelope,
                decision,
                provider,
                trusted_history=[],
            )
        mutated = deepcopy(self.result)
        mutated["summary"] = "changed after admission"
        with self.assertRaisesRegex(ValueError, "evidence drifted"):
            validate_envelope_dispatch_step(
                task,
                envelope,
                decision,
                provider,
                trusted_history=[mutated],
            )

    def test_cross_task_duplicate_attempt_id_invalidates_all_evidence(self) -> None:
        duplicate = deepcopy(self.task)
        duplicate["id"] = "V2-9999"
        duplicate_dir = self.layout.tasks_dir / "V2-9999"
        duplicate_dir.mkdir(parents=True)
        (duplicate_dir / "task.json").write_text(
            json.dumps(duplicate, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        trusted, issues = audit_result_evidence(self.layout)
        self.assertEqual(trusted, [])
        self.assertTrue(
            any("ambiguous duplicate authoritative attempt" in issue for issue in issues),
            issues,
        )

    def test_misplaced_task_document_is_never_routing_authority(self) -> None:
        canonical_dir = self.layout.tasks_dir / "V2-0001"
        misplaced_dir = self.layout.tasks_dir / "orphan-copy"
        canonical_dir.rename(misplaced_dir)
        trusted, issues = audit_result_evidence(self.layout)
        self.assertEqual(trusted, [])
        self.assertTrue(
            any("non-authoritative task document" in issue for issue in issues),
            issues,
        )

    def test_linked_task_document_is_never_routing_authority(self) -> None:
        canonical = self.layout.tasks_dir / "V2-0001" / "task.json"
        external = self.temp / "external-task.json"
        external.write_bytes(canonical.read_bytes())
        canonical.unlink()
        try:
            canonical.symlink_to(external)
        except OSError as exc:
            self.skipTest(f"file symlink unavailable: {exc}")
        trusted, issues = audit_result_evidence(self.layout)
        self.assertEqual(trusted, [])
        self.assertTrue(
            any("symlink/reparse" in issue for issue in issues),
            issues,
        )

    def test_linked_project_document_invalidates_v3_evidence(self) -> None:
        external = self.temp / "external-project.json"
        external.write_bytes(self.layout.project_json.read_bytes())
        self.layout.project_json.unlink()
        try:
            self.layout.project_json.symlink_to(external)
        except OSError as exc:
            self.skipTest(f"file symlink unavailable: {exc}")
        trusted, issues = audit_result_evidence(self.layout)
        self.assertEqual(trusted, [])
        self.assertTrue(
            any("authoritative project identity is unavailable" in issue for issue in issues),
            issues,
        )

    def test_linked_prompt_cas_root_is_never_trusted(self) -> None:
        prompt_path = Path(self.attempt["semantic_prompt"]["path"])
        prompt_dir = prompt_path.parent
        external = self.temp / "external-prompt-cas"
        prompt_dir.rename(external)
        try:
            prompt_dir.symlink_to(external, target_is_directory=True)
        except OSError as exc:
            self.skipTest(f"directory symlink unavailable: {exc}")
        trusted, issues = audit_result_evidence(self.layout)
        self.assertEqual(trusted, [])
        self.assertTrue(
            any("semantic prompt CAS root" in issue for issue in issues),
            issues,
        )

    def test_windows_reparse_task_directory_is_never_routing_authority(self) -> None:
        authoritative_dir = self.layout.tasks_dir / "V2-0001"
        original_lstat = Path.lstat

        def lstat_with_reparse(path: Path):
            info = original_lstat(path)
            if Path(path) == authoritative_dir:
                return SimpleNamespace(
                    st_mode=info.st_mode,
                    st_size=info.st_size,
                    st_mtime_ns=info.st_mtime_ns,
                    st_ctime_ns=getattr(info, "st_ctime_ns", 0),
                    st_ino=getattr(info, "st_ino", 0),
                    st_file_attributes=0x400,
                )
            return info

        with patch.object(Path, "lstat", lstat_with_reparse):
            trusted, issues = audit_result_evidence(self.layout)
        self.assertEqual(trusted, [])
        self.assertTrue(
            any("symlink/reparse" in issue for issue in issues),
            issues,
        )

    def test_v2_history_is_retained_but_never_trains_v3_routing(self) -> None:
        historical = deepcopy(self.result)
        historical["evidence_schema_version"] = "costmarshal-result-evidence-v2"
        # v2 stored the collected report's bare hex digest.  v3 binds the
        # actor-sealed report receipt with a ``sha256:`` prefix.
        historical["report_sha256"] = self.attempt["report_sha256"]
        self.layout.results_jsonl.write_text(
            json.dumps(historical, ensure_ascii=False, separators=(",", ":")) + "\n",
            encoding="utf-8",
        )

        trusted, issues = audit_result_evidence(self.layout)
        self.assertEqual(trusted, [])
        self.assertEqual(issues, [])

        historical["attempt_id"] = "ATT-forged-legacy"
        self.layout.results_jsonl.write_text(
            json.dumps(historical, ensure_ascii=False, separators=(",", ":")) + "\n",
            encoding="utf-8",
        )
        trusted, issues = audit_result_evidence(self.layout)
        self.assertEqual(trusted, [])
        self.assertTrue(any("unknown legacy attempt" in issue for issue in issues), issues)

    def test_handoff_helper_is_pure_and_requires_exact_trusted_rejection(self) -> None:
        before = deepcopy(self.task)
        capsule = build_rejected_attempt_handoff(
            task=self.task,
            attempt=self.attempt,
            trusted_result=self.result,
            handoff_text=HANDOFF_TEXT,
        )
        self.assertEqual(capsule["attempt_output_sha256"], self.attempt["attempt_output_sha256"])
        self.assertEqual(self.task, before)

        forged = deepcopy(self.result)
        forged["attempt_output_sha256"] = digest("forged-output")
        with self.assertRaisesRegex(HandoffContractError, "exact trusted rejected result"):
            build_rejected_attempt_handoff(
                task=self.task,
                attempt=self.attempt,
                trusted_result=forged,
                handoff_text=HANDOFF_TEXT,
            )

        contract = self.task["handoff_contract"]
        outgoing = self.attempt["attempt_output"]["outgoing_changes"]
        medium = build_attempt_input_contract(
            collaboration_contract=contract,
            attempt_id="ATT-medium-002",
            actor_id="agent-v2-0002",
            route_step_index=1,
            incoming_change_manifest_sha256=outgoing["manifest_sha256"],
            incoming_change_count=outgoing["change_count"],
            incoming_total_upsert_bytes=outgoing["total_upsert_bytes"],
            predecessor_handoff=self.attempt["handoff_capsule"],
            trusted_predecessor_result=self.attempt["handoff_result_evidence"],
        )
        medium_prompt = build_bound_prompt_bytes(
            attempt_input=medium,
            task_prompt_bytes=b"Continue at the medium tier.",
            predecessor_handoff=self.attempt["handoff_capsule"],
        )
        self.assertIn(HANDOFF_TEXT.encode("utf-8"), medium_prompt)

    def test_request_contract_separates_sealed_and_unsafe_native_boundaries(self) -> None:
        args = SimpleNamespace(
            status="failed",
            accepted_by_leader=False,
            quality_score=1,
            actor=None,
            agent=None,
            model=None,
            input_tokens=0,
            cached_input_tokens=0,
            output_tokens=0,
            estimated_cost_cny=None,
            summary="",
            note="",
        )
        sealed, sealed_sha = _result_request_contract(
            args,
            command_id="CMD-boundary",
            task_id="V2-0001",
            attempt_id="ATT-low-001",
            attempt_output_sha256=self.attempt["attempt_output_sha256"],
            attempt_output_boundary="sealed-required",
            task_type="analysis",
            difficulty="normal",
        )
        unsafe, unsafe_sha = _result_request_contract(
            args,
            command_id="CMD-boundary",
            task_id="V2-0001",
            attempt_id="ATT-low-001",
            attempt_output_sha256=None,
            attempt_output_boundary="unsealed-unsafe-native",
            task_type="analysis",
            difficulty="normal",
        )
        self.assertEqual(unsafe["attempt_output_boundary"], "unsealed-unsafe-native")
        self.assertIsNone(unsafe["attempt_output_sha256"])
        self.assertNotEqual(sealed_sha, unsafe_sha)
        self.assertNotEqual(sealed, unsafe)

    def test_read_only_continuation_uses_capsule_receipt_without_change_artifact(self) -> None:
        capsule = {
            "outgoing_changes": {
                "manifest_sha256": digest("read-only-no-writes"),
                "change_count": 0,
                "total_upsert_bytes": 0,
            }
        }
        predecessor_result = {"id": "RES-read-only-predecessor"}
        task = {
            "id": "V2-0001",
            "title": "read-only chain",
            "purpose": "continue without workspace writes",
            "estimated_input_tokens": 20000,
            "estimated_cached_input_tokens": 0,
            "estimated_output_tokens": 10000,
            "route_budget_envelope": {
                "envelope_id": "ENV-read-only",
                "plan_fingerprint": digest("read-only-plan"),
                "planned_steps": [{"index": 0}, {"index": 1}],
            },
            "attempts": [
                {
                    "attempt_id": "ATT-read-only-low",
                    "handoff_capsule": capsule,
                    "handoff_result_evidence": predecessor_result,
                },
                {
                    "attempt_id": "ATT-read-only-medium",
                    "route_plan_step_index": 1,
                },
            ],
        }
        actor = {
            "id": "agent-v2-read-only-medium",
            "attempt_id": "ATT-read-only-medium",
            "collaboration_contract": {
                "base_sha": "a" * 40,
                "write_scope": [],
            },
        }
        projection = {
            "manifest_sha256": digest("context"),
            "allowlist": [],
            "file_count": 0,
            "total_size_bytes": 0,
        }
        attempt_input = {"attempt_input_sha256": digest("attempt-input")}
        with patch(
            "costmarshal_v2.actor_runner.build_semantic_collaboration_contract",
            return_value={"contract_sha256": digest("collaboration")},
        ), patch(
            "costmarshal_v2.actor_runner.build_attempt_input_contract",
            return_value=attempt_input,
        ) as build_input, patch(
            "costmarshal_v2.actor_runner.build_bound_prompt_bytes",
            return_value=b"bound prompt",
        ), patch(
            "costmarshal_v2.actor_runner.build_semantic_prompt_binding",
            return_value={"binding_sha256": digest("binding")},
        ):
            prepared = _prepare_semantic_attempt(
                self.layout,
                {"project_id": "project"},
                actor,
                task,
                bootstrap_prompt_bytes=b"task prompt",
                projection_receipt=projection,
                incoming_change_artifact=None,
            )
        self.assertIsNotNone(prepared)
        call = build_input.call_args.kwargs
        self.assertEqual(
            call["incoming_change_manifest_sha256"],
            capsule["outgoing_changes"]["manifest_sha256"],
        )
        self.assertEqual(call["incoming_change_count"], 0)
        self.assertEqual(call["incoming_total_upsert_bytes"], 0)

    def test_legacy_required_without_semantic_envelope_is_unsealed_history_only(self) -> None:
        task = deepcopy(self.task)
        attempt = task["attempts"][0]
        task.pop("handoff_contract", None)
        for field in (
            "attempt_input",
            "semantic_prompt_binding",
            "semantic_prompt",
            "execution_receipt",
            "attempt_output",
            "attempt_output_sha256",
            "handoff_capsule",
            "handoff_result_evidence",
        ):
            attempt.pop(field, None)
        request = deepcopy(self.result["request_contract"])
        request["attempt_output_sha256"] = None
        request["attempt_output_boundary"] = "unsealed-legacy-required"
        request["handoff_argument"] = ""
        request_sha256 = "sha256:" + hashlib.sha256(canonical_bytes(request)).hexdigest()
        result = deepcopy(self.result)
        result["request_contract"] = request
        result["request_contract_sha256"] = request_sha256
        result["attempt_output_sha256"] = None
        result["attempt_output_boundary"] = "unsealed-legacy-required"
        result["report_sha256"] = attempt["report_sha256"]
        attempt["result_request_contract_sha256"] = request_sha256
        attempt["result_attempt_output_sha256"] = None
        attempt["result_attempt_output_boundary"] = "unsealed-legacy-required"
        self._write_task(task)
        self.layout.results_jsonl.write_text(
            json.dumps(result, ensure_ascii=False, separators=(",", ":")) + "\n",
            encoding="utf-8",
        )

        trusted, issues = audit_result_evidence(self.layout)
        self.assertEqual(trusted, [])
        self.assertEqual(issues, [])
        with self.assertRaises(HandoffContractError):
            build_rejected_attempt_handoff(
                task=task,
                attempt=attempt,
                trusted_result=result,
                handoff_text="legacy evidence must not enter a semantic successor",
            )

    def test_orphan_crash_recovery_rebuilds_exact_handoff_pair(self) -> None:
        task = deepcopy(self.task)
        attempt = task["attempts"][0]
        task.pop("leader_result", None)
        for field in (
            "leader_result_id",
            "accepted_by_leader",
            "quality_score",
            "recorded_result_status",
            "result_request_contract_sha256",
            "result_attempt_output_sha256",
            "result_attempt_output_boundary",
            "handoff_capsule",
            "handoff_result_evidence",
        ):
            attempt.pop(field, None)
        _apply_leader_result_binding(task, attempt, self.result)
        orphan_handoff = self.result["request_contract"]["handoff_argument"]
        changed = _bind_rejected_attempt_handoff(
            task=task,
            attempt=attempt,
            trusted_result=self.result,
            handoff_text=orphan_handoff,
        )
        self.assertTrue(changed)
        self.assertEqual(attempt["handoff_result_evidence"], self.result)
        self.assertEqual(
            attempt["handoff_capsule"]["handoff"]["text"],
            HANDOFF_TEXT,
        )
        self._write_task(task)
        trusted, issues = audit_result_evidence(self.layout)
        self.assertEqual(trusted, [self.result])
        self.assertEqual(issues, [])

    def test_mailbox_record_result_forwards_handoff_argument(self) -> None:
        command_args = {
            "task": "V2-0001",
            "status": "escalate",
            "quality_score": 2,
            "handoff": HANDOFF_TEXT,
        }
        with patch("costmarshal_v2.scheduler.run_cli_helper") as helper:
            helper.return_value = {"status": "ok"}
            execute_scheduler_command(
                self.layout,
                sender="leader",
                command="record_result",
                command_args=command_args,
                message={"id": "MSG-result-001", "task_id": "V2-0001"},
            )
        self.assertEqual(helper.call_args.kwargs["handoff"], HANDOFF_TEXT)


if __name__ == "__main__":
    unittest.main()
