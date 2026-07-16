from __future__ import annotations

import json
import hashlib
import os
import shutil
import subprocess
import sys
import tempfile
import tomllib
from pathlib import Path
from types import SimpleNamespace


ROOT = Path(__file__).resolve().parents[1]
CLI = ROOT / "scripts" / "costmarshal.py"
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from costmarshal_v2.handoff_contract import (  # noqa: E402
    HandoffLimits,
    build_attempt_input_contract,
    build_attempt_output_contract,
    build_bound_prompt_bytes,
    build_collaboration_contract,
    build_prompt_binding,
)
from costmarshal_v2.paths import ProjectLayout, slugify  # noqa: E402
from costmarshal_v2.routing import route_plan_fingerprint  # noqa: E402
from costmarshal_v2.scheduler import (  # noqa: E402
    RESULT_EVIDENCE_SCHEMA,
    _apply_leader_result_binding,
    _bind_rejected_attempt_handoff,
    _result_request_contract,
    audit_result_evidence,
)


def run(temp: Path, *args: str, ok: bool = True) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        [sys.executable, str(CLI), "--root", str(temp / "runtime"), *args],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=dict(os.environ),
        check=False,
    )
    if ok and result.returncode != 0:
        raise AssertionError(f"command failed: {args}\n{result.stdout}\n{result.stderr}")
    if not ok and result.returncode == 0:
        raise AssertionError(f"command unexpectedly succeeded: {args}\n{result.stdout}")
    return result


def run_json(temp: Path, *args: str) -> dict:
    return json.loads(run(temp, *args).stdout)


def canonical_bytes(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def digest(value: str | bytes) -> str:
    payload = value if isinstance(value, bytes) else value.encode("utf-8")
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def seed_paired_route_evidence(temp: Path, project: Path) -> None:
    project_state = json.loads((project / "project.json").read_text(encoding="utf-8"))
    project_id = str(project_state["project_id"])
    providers = {
        row["provider_id"]: row
        for row in project_state["provider_catalog"]["providers"]
    }
    profile_hashes = {
        "longcat": "sha256:"
        + hashlib.sha256((Path(os.environ["CODEX_HOME"]) / "longcat.config.toml").read_bytes()).hexdigest(),
        "deepseek": "sha256:"
        + hashlib.sha256((Path(os.environ["CODEX_HOME"]) / "deepseek.config.toml").read_bytes()).hexdigest(),
        "codex": "sha256:"
        + hashlib.sha256(b"# CostMarshal isolated default profile\n").hexdigest(),
    }
    profile_models = {}
    for provider_id in ("longcat", "deepseek"):
        configured = providers[provider_id].get("model")
        parsed = tomllib.loads(
            (Path(os.environ["CODEX_HOME"]) / f"{provider_id}.config.toml").read_text(
                encoding="utf-8"
            )
        )
        profile_models[provider_id] = (
            configured
            if configured not in {None, "", "inherit"}
            else parsed.get("model") or "inherit"
        )
    default_profile = b"# CostMarshal isolated default profile\n"
    provider_bindings = {
        "longcat": {
            "schema_version": "costmarshal-profile-binding-v1",
            "status": "available",
            "logical_name": "longcat",
            "source_kind": "named-profile",
            "sha256": profile_hashes["longcat"],
            "size_bytes": (Path(os.environ["CODEX_HOME"]) / "longcat.config.toml").stat().st_size,
            "provider_identity": "longcat",
            "base_url": "https://longcat.invalid/v1",
            "wire_api": "responses",
            "env_key": "LONGCAT_API_KEY",
            "model": str(profile_models["longcat"]),
            "snapshot_relpath": "profile-snapshots/evidence/longcat.config.toml",
        },
        "deepseek": {
            "schema_version": "costmarshal-profile-binding-v1",
            "status": "available",
            "logical_name": "deepseek",
            "source_kind": "named-profile",
            "sha256": profile_hashes["deepseek"],
            "size_bytes": (Path(os.environ["CODEX_HOME"]) / "deepseek.config.toml").stat().st_size,
            "provider_identity": "deepseek",
            "base_url": "https://deepseek.invalid/v1",
            "wire_api": "responses",
            "env_key": "DEEPSEEK_API_KEY",
            "model": str(profile_models["deepseek"]),
            "snapshot_relpath": "profile-snapshots/evidence/deepseek.config.toml",
        },
        "codex": {
            "schema_version": "costmarshal-profile-binding-v1",
            "status": "available",
            "logical_name": None,
            "source_kind": "synthetic-default",
            "sha256": profile_hashes["codex"],
            "size_bytes": len(default_profile),
            "provider_identity": None,
            "base_url": None,
            "wire_api": None,
            "env_key": None,
            "model": "inherit",
            "snapshot_relpath": "profile-snapshots/evidence/default.config.toml",
        },
    }
    rows: list[dict] = []
    layout = ProjectLayout(root=temp / "runtime", project_dir=project)
    snapshot_root = layout.root / "profile-snapshots" / "evidence"
    snapshot_root.mkdir(parents=True, exist_ok=True)
    (snapshot_root / "longcat.config.toml").write_bytes(
        (Path(os.environ["CODEX_HOME"]) / "longcat.config.toml").read_bytes()
    )
    (snapshot_root / "deepseek.config.toml").write_bytes(
        (Path(os.environ["CODEX_HOME"]) / "deepseek.config.toml").read_bytes()
    )
    (snapshot_root / "default.config.toml").write_bytes(default_profile)
    for index in range(20):
        task_id = f"V2-{1000 + index:04d}"
        envelope_id = f"ENV-evidence-{index}"
        provider_specs = (
            ("longcat", "low", str(profile_models["longcat"]), "longcat"),
            ("deepseek", "medium", str(profile_models["deepseek"]), "deepseek"),
            ("codex", "high", "inherit", None),
        )
        planned_steps = []
        for step, (provider, tier, model, profile) in enumerate(provider_specs):
            identity = {
                "model": model,
                "profile": profile,
                "profile_sha256": profile_hashes[provider],
            }
            planned_steps.append(
                {
                    "index": step,
                    "provider_id": provider,
                    "tier": tier,
                    "model": model,
                    "profile": profile,
                    "execution_identity": identity,
                    "estimated_cost_cny": str(step + 1),
                    "acceptance_prior": {"probability": "0.5", "source": "fixture"},
                    "price_basis": {"kind": "fixture"},
                    "profile_binding": provider_bindings[provider],
                }
            )
        fingerprint = route_plan_fingerprint(
            planned_steps,
            input_tokens=20_000,
            cached_input_tokens=0,
            output_tokens=10_000,
        )
        initial_manifest = digest(f"empty-changes-{index}")
        context_manifest = digest(f"context-{index}")
        contract = build_collaboration_contract(
            task_id=task_id,
            task_spec={"title": "paired evidence", "purpose": "project success fixture"},
            base_sha=hashlib.sha1(task_id.encode("utf-8")).hexdigest(),
            context_allowlist=[],
            context_manifest_sha256=context_manifest,
            context_file_count=0,
            context_total_size_bytes=0,
            write_scope=[],
            initial_change_manifest_sha256=initial_manifest,
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
            route_envelope_id=envelope_id,
            route_plan_fingerprint_sha256=fingerprint,
            planned_steps=planned_steps,
        )
        common = {
            "event_type": "result",
            "evidence_schema_version": RESULT_EVIDENCE_SCHEMA,
            "project_id": project_id,
            "timestamp": "2026-07-16T00:00:00Z",
            "task_id": task_id,
            "task_type": "analysis",
            "difficulty": "normal",
            "status": "done",
            "quality_score": 4,
            "input_tokens": 0,
            "cached_input_tokens": 0,
            "output_tokens": 0,
            "total_tokens": 0,
            "estimated_cost_cny": 0,
            "route_envelope_id": envelope_id,
            "route_plan_fingerprint": fingerprint,
        }
        task: dict = {
            "id": task_id,
            "status": "planned",
            "task_type": "analysis",
            "difficulty": "normal",
            "estimated_input_tokens": 20_000,
            "estimated_cached_input_tokens": 0,
            "estimated_output_tokens": 10_000,
            "handoff_contract": contract,
            "route_budget_envelope": {
                "schema_version": "costmarshal-route-budget-envelope-v2",
                "envelope_id": envelope_id,
                "plan_fingerprint": fingerprint,
                "estimated_input_tokens": 20_000,
                "estimated_cached_input_tokens": 0,
                "estimated_output_tokens": 10_000,
                "planned_steps": json.loads(json.dumps(planned_steps)),
                "reserved_cost_cny": "6",
                "baseline_commitment_cny": "0",
                "status": "released",
                "created_at": "2026-07-16T00:00:00Z",
                "released_at": "2026-07-16T00:00:01Z",
                "release_reason": "fixture route completed",
            },
            "attempts": [],
        }
        predecessors: list[dict] = []
        predecessor_capsule: dict | None = None
        predecessor_result: dict | None = None

        def add_result(
            *,
            provider: str,
            tier: str,
            model: str,
            profile: str | None,
            step: int,
            accepted: bool,
            final_step: bool = False,
        ) -> dict:
            attempt_id = f"ATT-{provider}-{index}"
            result_id = f"RES-{provider}-{index}"
            command_id = f"CMD-evidence-{provider}-{index}"
            actor_id = f"agent-v2-evidence-{index}-{tier}"
            status = "done" if accepted else ("failed" if final_step else "escalate")
            handoff_text = "" if accepted or final_step else f"{tier} evidence requires the next admitted tier."
            identity = {
                "model": model,
                "profile": profile,
                "profile_sha256": profile_hashes[provider],
            }
            attempt_input = build_attempt_input_contract(
                collaboration_contract=contract,
                attempt_id=attempt_id,
                actor_id=actor_id,
                route_step_index=step,
                incoming_change_manifest_sha256=initial_manifest,
                incoming_change_count=0,
                incoming_total_upsert_bytes=0,
                predecessor_handoff=predecessor_capsule,
                trusted_predecessor_result=predecessor_result,
            )
            prompt = build_bound_prompt_bytes(
                attempt_input=attempt_input,
                task_prompt_bytes=f"Evaluate paired route sample {index} at {tier} tier.".encode("utf-8"),
                predecessor_handoff=predecessor_capsule,
            )
            prompt_binding = build_prompt_binding(
                collaboration_contract=contract,
                attempt_input=attempt_input,
                prompt_bytes=prompt,
                predecessor_handoff=predecessor_capsule,
            )
            prompt_dir = (
                layout.root
                / "semantic-prompts"
                / slugify(project_id, "project")
                / slugify(attempt_id, "attempt")
            )
            prompt_dir.mkdir(parents=True, exist_ok=True)
            prompt_path = prompt_dir / prompt_binding["prompt_sha256"].removeprefix(
                "sha256:"
            )
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
            report = f"# Fixture report\n\nTier: {tier}\nStatus: {status}\n".encode("utf-8")
            report_sha256 = hashlib.sha256(report).hexdigest()
            task_dir = project / "tasks" / task_id
            task_dir.mkdir(parents=True, exist_ok=True)
            report_path = task_dir / f"{attempt_id}-report.md"
            report_path.write_bytes(report)
            execution_body = {
                "schema_version": 1,
                "kind": "costmarshal-execution-receipt",
                "task_id": task_id,
                "attempt_id": attempt_id,
                "actor_id": actor_id,
                "provider": provider,
                "tier": tier,
                "model": model,
                "profile": profile,
                "profile_sha256": profile_hashes[provider],
                "context_projection_manifest_sha256": context_manifest,
                "incoming_change_manifest_sha256": initial_manifest,
                "semantic_prompt_sha256": prompt_binding["prompt_sha256"],
                "isolation_backend": "fixture-oci",
                "container_name": f"fixture-{index}-{tier}",
                "container_id": f"fixture-container-{index}-{tier}",
                "isolation_attestation": {"fixture": True},
                "provider_exit_code": 0,
            }
            execution_payload = canonical_bytes(execution_body)
            execution_sha256 = digest(execution_payload)
            receipt_dir = (
                layout.root
                / "execution-receipts"
                / slugify(project_id, "project")
                / slugify(attempt_id, "attempt")
            )
            receipt_dir.mkdir(parents=True, exist_ok=True)
            receipt_path = receipt_dir / execution_sha256.removeprefix("sha256:")
            receipt_path.write_bytes(execution_payload)
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
                outgoing_change_manifest_sha256=initial_manifest,
                outgoing_change_count=0,
                outgoing_total_upsert_bytes=0,
            )
            attempt = {
                "attempt_id": attempt_id,
                "actor_id": actor_id,
                "provider": provider,
                "tier": tier,
                "profile": profile,
                "model": model,
                "execution_identity": identity,
                "profile_binding": json.loads(json.dumps(provider_bindings[provider])),
                "route_envelope_id": envelope_id,
                "route_plan_fingerprint": fingerprint,
                "route_plan_step_index": step,
                "route_plan_step": json.loads(json.dumps(planned_steps[step])),
                "route_predecessors": json.loads(json.dumps(predecessors)),
                "isolation": {"mode": "required"},
                "context_projection": {"manifest_sha256": context_manifest},
                "provider_exit_code": 0,
                "status": status,
                "actual_cost_cny": "0",
                "reserved_cost_cny": "0",
                "cost_settled": True,
                "report_path": report_path.relative_to(project).as_posix(),
                "report_sha256": report_sha256,
                "report_size": len(report),
                "attempt_input": attempt_input,
                "semantic_prompt_binding": prompt_binding,
                "semantic_prompt": prompt_receipt,
                "execution_receipt": execution_receipt,
                "attempt_output": attempt_output,
                "attempt_output_sha256": attempt_output["attempt_output_sha256"],
            }
            task["attempts"].append(attempt)
            request_contract, request_digest = _result_request_contract(
                SimpleNamespace(
                    status=status,
                    accepted_by_leader=accepted,
                    quality_score=4,
                    actor=None,
                    agent=None,
                    model=None,
                    input_tokens=0,
                    cached_input_tokens=0,
                    output_tokens=0,
                    estimated_cost_cny=None,
                    summary=f"{tier} paired evidence",
                    handoff=handoff_text,
                    note="",
                ),
                command_id=command_id,
                task_id=task_id,
                attempt_id=attempt_id,
                attempt_output_sha256=attempt_output["attempt_output_sha256"],
                attempt_output_boundary="sealed-required",
                task_type="analysis",
                difficulty="normal",
            )
            row = {
                **common,
                "id": result_id,
                "command_id": command_id,
                "request_contract": request_contract,
                "request_contract_sha256": request_digest,
                "attempt_id": attempt_id,
                "actor_id": actor_id,
                "agent": actor_id,
                "provider_id": provider,
                "provider": provider,
                "tier": tier,
                "model": model,
                "execution_model": model,
                "profile": profile,
                "profile_sha256": profile_hashes[provider],
                "route_plan_step_index": step,
                "route_predecessors": json.loads(json.dumps(predecessors)),
                "accepted_by_leader": accepted,
                "status": status,
                "summary": f"{tier} paired evidence",
                "attempt_output_sha256": attempt_output["attempt_output_sha256"],
                "attempt_output_boundary": "sealed-required",
                "report_path": attempt["report_path"],
                "report_sha256": attempt_output["report_receipt"]["sha256"],
                "report_size": len(report),
            }
            _apply_leader_result_binding(task, attempt, row)
            if handoff_text:
                _bind_rejected_attempt_handoff(
                    task=task,
                    attempt=attempt,
                    trusted_result=row,
                    handoff_text=handoff_text,
                )
            rows.append(row)
            return {
                "provider_id": provider,
                "model": model,
                "profile": profile,
                "profile_sha256": profile_hashes[provider],
                "attempt_id": attempt_id,
                "result_id": result_id,
            }

        low = add_result(
            provider="longcat",
            tier="low",
            model=str(profile_models["longcat"]),
            profile="longcat",
            step=0,
            accepted=False,
        )
        predecessor_capsule = task["attempts"][-1]["handoff_capsule"]
        predecessor_result = task["attempts"][-1]["handoff_result_evidence"]
        predecessors.append(low)
        medium_accepted = index < 4
        medium = add_result(
            provider="deepseek",
            tier="medium",
            model=str(profile_models["deepseek"]),
            profile="deepseek",
            step=1,
            accepted=medium_accepted,
        )
        if not medium_accepted:
            predecessor_capsule = task["attempts"][-1]["handoff_capsule"]
            predecessor_result = task["attempts"][-1]["handoff_result_evidence"]
            predecessors.append(medium)
            add_result(
                provider="codex",
                tier="high",
                model="inherit",
                profile=None,
                step=2,
                accepted=index < 7,
                final_step=True,
            )
        task["status"] = task["attempts"][-1]["status"]
        task_dir = project / "tasks" / task_id
        last_report = project / task["attempts"][-1]["report_path"]
        (task_dir / "brief.md").write_text(
            f"# {task_id}\n\nPaired three-tier routing evidence fixture.\n",
            encoding="utf-8",
        )
        (task_dir / "status.json").write_text(
            json.dumps(
                {
                    "schema_version": 2,
                    "task_id": task_id,
                    "state": task["status"],
                },
                ensure_ascii=False,
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
        (task_dir / "completion-report.md").write_bytes(last_report.read_bytes())
        (task_dir / "task.json").write_text(
            json.dumps(
                task,
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
    results = project / "reports" / "results.jsonl"
    with results.open("a", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n")
    assert sum(row["tier"] == "medium" for row in rows) == 20
    assert sum(row["tier"] == "medium" and row["accepted_by_leader"] for row in rows) == 4
    assert sum(row["tier"] == "high" for row in rows) == 16
    assert sum(row["tier"] == "high" and row["accepted_by_leader"] for row in rows) == 3
    trusted, issues = audit_result_evidence(layout)
    assert len(trusted) == 56, issues
    assert issues == [], issues


def main() -> int:
    temp = Path(tempfile.mkdtemp(prefix="costmarshal-success-policy-"))
    previous_codex_home = os.environ.get("CODEX_HOME")
    try:
        codex_home = temp / "codex-home"
        codex_home.mkdir()
        for profile, env_key in (
            ("longcat", "LONGCAT_API_KEY"),
            ("deepseek", "DEEPSEEK_API_KEY"),
        ):
            (codex_home / f"{profile}.config.toml").write_text(
                "\n".join(
                    [
                        f'model_provider = "{profile}"',
                        f'[model_providers.{profile}]',
                        f'name = "{profile}"',
                        f'base_url = "https://{profile}.invalid/v1"',
                        'wire_api = "responses"',
                        f'env_key = "{env_key}"',
                        "",
                    ]
                ),
                encoding="utf-8",
            )
        os.environ["CODEX_HOME"] = str(codex_home)
        workspace = temp / "workspace"
        workspace.mkdir()
        catalog = {
            "schema_version": 1,
            "providers": [
                {
                    "provider_id": "longcat",
                    "tier": "low",
                    "profile": "longcat",
                    "model": "LongCat-2.0",
                    "env_key": "LONGCAT_API_KEY",
                    "enabled": True,
                    "priority": 100,
                    "input_cny_per_1m": 1.0,
                    "output_cny_per_1m": 1.0,
                    "capabilities": [],
                },
                {
                    "provider_id": "deepseek",
                    "tier": "medium",
                    "profile": "deepseek",
                    "model": "inherit",
                    "env_key": "DEEPSEEK_API_KEY",
                    "enabled": True,
                    "priority": 100,
                    "input_cny_per_1m": 2.0,
                    "output_cny_per_1m": 2.0,
                    "capabilities": [],
                },
                {
                    "provider_id": "codex",
                    "tier": "high",
                    "profile": None,
                    "model": "inherit",
                    "env_key": "CODEX_API_KEY",
                    "enabled": True,
                    "priority": 100,
                    "input_cny_per_1m": 3.0,
                    "output_cny_per_1m": 3.0,
                    "capabilities": [],
                },
            ],
        }
        catalog_path = temp / "catalog.json"
        catalog_path.write_text(json.dumps(catalog), encoding="utf-8")
        init = run_json(
            temp,
            "init",
            "--objective",
            "freeze a project success SLA",
            "--workspace",
            str(workspace),
            "--provider-catalog",
            str(catalog_path),
            "--default-min-success-probability",
            "0.10",
            "--governance",
            "off",
        )
        project = Path(init["project"])
        seed_paired_route_evidence(temp, project)

        inherited = run_json(
            temp,
            "new-task",
            "--project",
            str(project),
            "--title",
            "inherited",
            "--purpose",
            "inherit project SLA",
            "--estimated-input-tokens",
            "1000000",
        )
        inherited_task = json.loads((Path(inherited["task"]) / "task.json").read_text(encoding="utf-8"))
        assert inherited_task["min_success_probability"] == 0.1
        assert inherited_task["min_success_probability_source"] == "project-default"
        assert len(inherited_task["route_preview"]["planned_provider_ids"]) == 2

        explicit = run_json(
            temp,
            "new-task",
            "--project",
            str(project),
            "--title",
            "explicit",
            "--purpose",
            "override project SLA",
            "--estimated-input-tokens",
            "1000000",
            "--min-success-probability",
            "0.15",
        )
        explicit_task = json.loads((Path(explicit["task"]) / "task.json").read_text(encoding="utf-8"))
        assert explicit_task["min_success_probability"] == 0.15
        assert explicit_task["min_success_probability_source"] == "task-explicit"
        assert len(explicit_task["route_preview"]["planned_provider_ids"]) == 3

        zero = run_json(
            temp,
            "new-task",
            "--project",
            str(project),
            "--title",
            "zero",
            "--purpose",
            "explicitly allow no success floor",
            "--estimated-input-tokens",
            "1000000",
            "--min-success-probability",
            "0",
        )
        zero_task = json.loads((Path(zero["task"]) / "task.json").read_text(encoding="utf-8"))
        assert zero_task["min_success_probability"] == 0.0
        # Medium/high observations in this fixture are conditional continuation
        # evidence and must not inflate their standalone start priors.
        assert zero_task["route_preview"]["planned_provider_ids"] == [
            "longcat",
            "deepseek",
        ]

        pinned = run_json(
            temp,
            "new-task",
            "--project",
            str(project),
            "--title",
            "pinned",
            "--purpose",
            "explicit provider ignores project SLA",
            "--provider",
            "longcat",
            "--estimated-input-tokens",
            "1000000",
        )
        pinned_task = json.loads((Path(pinned["task"]) / "task.json").read_text(encoding="utf-8"))
        assert pinned_task["min_success_probability"] is None
        assert pinned_task["min_success_probability_source"] == "explicit-route-not-applicable"
        explicit_to_auto = run(
            temp,
            "dispatch",
            "--project",
            str(project),
            "--task",
            pinned_task["id"],
            "--provider",
            "auto",
            ok=False,
        )
        assert "frozen provider routing mode" in (explicit_to_auto.stdout + explicit_to_auto.stderr)
        auto_to_explicit = run(
            temp,
            "dispatch",
            "--project",
            str(project),
            "--task",
            inherited_task["id"],
            "--provider",
            "longcat",
            ok=False,
        )
        assert "frozen provider routing mode" in (auto_to_explicit.stdout + auto_to_explicit.stderr)
        tier_override = run(
            temp,
            "dispatch",
            "--project",
            str(project),
            "--task",
            inherited_task["id"],
            "--tier",
            "high",
            ok=False,
        )
        assert "frozen tier routing mode" in (tier_override.stdout + tier_override.stderr)

        project_file = project / "project.json"
        project_state = json.loads(project_file.read_text(encoding="utf-8"))
        project_state["routing_policy"]["default_min_success_probability"] = 0.15
        project_file.write_text(json.dumps(project_state, ensure_ascii=False, indent=2), encoding="utf-8")
        frozen = json.loads((Path(inherited["task"]) / "task.json").read_text(encoding="utf-8"))
        assert frozen["min_success_probability"] == 0.1
        route = run_json(
            temp,
            "route",
            "--project",
            str(project),
            "--estimated-input-tokens",
            "1000000",
        )
        assert route["task"]["min_success_probability"] == 0.15
        assert route["task"]["min_success_probability_source"] == "project-default"
        assert len(route["decision"]["planned_provider_ids"]) == 3

        other_workspace = temp / "other-workspace"
        other_workspace.mkdir()
        invalid = run(
            temp,
            "init",
            "--objective",
            "invalid SLA",
            "--workspace",
            str(other_workspace),
            "--default-min-success-probability",
            "1.1",
            "--governance",
            "off",
            ok=False,
        )
        assert "between 0 and 1" in (invalid.stdout + invalid.stderr)

        results_path = project / "reports" / "results.jsonl"
        result_rows = [
            json.loads(line)
            for line in results_path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        predecessor = next(row for row in result_rows if row["id"] == "RES-longcat-4")
        predecessor["task_type"] = "forged-bucket"
        results_path.write_text(
            "".join(
                json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n"
                for row in result_rows
            ),
            encoding="utf-8",
        )
        trusted_after_tamper, tamper_issues = audit_result_evidence(
            ProjectLayout(root=temp / "runtime", project_dir=project)
        )
        trusted_ids = {row["id"] for row in trusted_after_tamper}
        assert "RES-longcat-4" not in trusted_ids
        assert "RES-deepseek-4" not in trusted_ids
        assert "RES-codex-4" not in trusted_ids
        assert any("predecessor is not trusted" in issue for issue in tamper_issues)
        print("project success policy ok")
        return 0
    finally:
        if previous_codex_home is None:
            os.environ.pop("CODEX_HOME", None)
        else:
            os.environ["CODEX_HOME"] = previous_codex_home
        shutil.rmtree(temp, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
