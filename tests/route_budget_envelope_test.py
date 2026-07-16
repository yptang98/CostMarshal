#!/usr/bin/env python3
"""Whole-chain admission envelopes remain executable across three tiers."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
CLI = ROOT / "scripts" / "costmarshal.py"
sys.path.insert(0, str(ROOT))

from costmarshal_v2.routing import build_pricing_snapshot, default_provider_catalog  # noqa: E402
from costmarshal_v2.profiles import provider_profile_text  # noqa: E402
from project_success_policy_test import seed_paired_route_evidence  # noqa: E402


def run(temp: Path, *args: str, ok: bool = True) -> subprocess.CompletedProcess[str]:
    env = dict(os.environ)
    env["COSTMARSHAL_V2_HOME"] = str(temp / "runtime")
    completed = subprocess.run(
        [sys.executable, str(CLI), "--root", str(temp / "runtime"), *args],
        text=True,
        encoding="utf-8",
        errors="replace",
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
        check=False,
    )
    if ok and completed.returncode:
        raise AssertionError(f"command failed: {args}\n{completed.stdout}\n{completed.stderr}")
    if not ok and not completed.returncode:
        raise AssertionError(f"command unexpectedly succeeded: {args}\n{completed.stdout}")
    return completed


def run_json(temp: Path, *args: str) -> dict:
    return json.loads(run(temp, *args).stdout)


def make_project(temp: Path, name: str, budget: float | str) -> Path:
    workspace = temp / f"workspace-{name}"
    workspace.mkdir()
    catalog = default_provider_catalog()
    prices = {"longcat": 1.0, "deepseek": 2.0, "codex": 3.0}
    clock = datetime.now(timezone.utc)
    reviewed = (clock - timedelta(days=1)).isoformat().replace("+00:00", "Z")
    expires = (clock + timedelta(days=30)).isoformat().replace("+00:00", "Z")
    for provider in catalog["providers"]:
        price = prices[provider["provider_id"]]
        provider["pricing"] = build_pricing_snapshot(
            currency="CNY",
            source=f"https://pricing.example/{provider['provider_id']}",
            reviewed_at=reviewed,
            effective_at=reviewed,
            expires_at=expires,
            snapshot_id=f"{name}-{provider['provider_id']}",
            input_per_1m=price,
            cached_input_per_1m=price,
            output_per_1m=price,
            fixed_attempt=0.0,
        )
    catalog_path = temp / f"catalog-{name}.json"
    catalog_path.write_text(json.dumps(catalog), encoding="utf-8")
    created = run_json(
        temp,
        "init",
        "--name",
        name,
        "--objective",
        "three-tier route budget envelope",
        "--workspace",
        str(workspace),
        "--provider-catalog",
        str(catalog_path),
        "--project-budget-cny",
        str(budget),
        "--governance",
        "off",
        "--allow-unsafe-native-workers",
    )
    project = Path(created["project"])
    seed_paired_route_evidence(temp, project)
    return project


def new_chain_task(
    temp: Path,
    project: Path,
    title: str,
    *,
    max_cost: float | str | None = None,
    min_success: str = "0.15",
) -> str:
    args = [
        "new-task",
        "--project",
        str(project),
        "--title",
        title,
        "--purpose",
        "exercise low medium high envelope",
        "--estimated-input-tokens",
        "1000000",
        "--min-success-probability",
        min_success,
    ]
    if max_cost is not None:
        args.extend(["--max-cost-cny", str(max_cost)])
    return run_json(temp, *args)["task_id"]


def task_payload(project: Path, task_id: str) -> dict:
    return json.loads((project / "tasks" / task_id / "task.json").read_text(encoding="utf-8"))


def mark_attempts_executed(project: Path, task_id: str) -> None:
    task = task_payload(project, task_id)
    for attempt in task["attempts"]:
        attempt["provider_execution_state"] = "finished"
        actor_path = project / "scheduler" / "actors" / f"{attempt['actor_id']}.json"
        actor = json.loads(actor_path.read_text(encoding="utf-8"))
        actor.setdefault("runtime", {})["provider_execution_state"] = "finished"
        actor_path.write_text(json.dumps(actor, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    (project / "tasks" / task_id / "task.json").write_text(
        json.dumps(task, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def reject_latest_attempt(temp: Path, project: Path, task_id: str, label: str) -> dict:
    task = task_payload(project, task_id)
    attempt = task["attempts"][-1]
    report = project / "tasks" / task_id / "completion-report.md"
    report.write_text(
        f"# Completion Report: {task_id}\n\nStatus: escalate\n\n{label}\n",
        encoding="utf-8",
    )
    run_json(
        temp,
        "heartbeat",
        "--project",
        str(project),
        "--actor",
        attempt["actor_id"],
        "--status",
        "waiting",
    )
    run_json(
        temp,
        "collect",
        "--command-id",
        f"CMD-collect-{label}",
        "--project",
        str(project),
        "--task",
        task_id,
        "--attempt",
        attempt["attempt_id"],
        "--actor",
        attempt["actor_id"],
        "--state",
        "escalate",
    )
    return run_json(
        temp,
        "record-result",
        "--command-id",
        f"CMD-result-{label}",
        "--project",
        str(project),
        "--task",
        task_id,
        "--attempt",
        attempt["attempt_id"],
        "--actor",
        attempt["actor_id"],
        "--status",
        "escalate",
        "--quality-score",
        "3",
        "--summary",
        label,
    )


def remove_seed_evidence(project: Path) -> None:
    seeded_ids = {f"V2-{1000 + index:04d}" for index in range(20)}
    for task_id in seeded_ids:
        task_dir = project / "tasks" / task_id
        if not task_dir.is_dir():
            continue
        shutil.rmtree(task_dir)
    results = project / "reports" / "results.jsonl"
    retained = [
        row
        for row in results.read_text(encoding="utf-8").splitlines()
        if row.strip() and str(json.loads(row).get("task_id") or "") not in seeded_ids
    ]
    results.write_text("".join(f"{row}\n" for row in retained), encoding="utf-8")


def main() -> int:
    temp = Path(tempfile.mkdtemp(prefix="costmarshal-route-budget-envelope-"))
    try:
        codex_home = temp / "codex-home"
        codex_home.mkdir()
        for profile, provider_id, env_key in (
            ("longcat", "longcat", "LONGCAT_API_KEY"),
            ("deepseek", "deepseek", "DEEPSEEK_API_KEY"),
        ):
            (codex_home / f"{profile}.config.toml").write_text(
                provider_profile_text(
                    provider_id=provider_id,
                    display_name=provider_id,
                    base_url=f"https://{provider_id}.example/v1",
                    model="test-model",
                    env_key=env_key,
                ),
                encoding="utf-8",
            )
        os.environ["CODEX_HOME"] = str(codex_home)
        exact_budget = "999999999.123456789"
        exact_project = make_project(temp, "nano-exact", exact_budget)
        exact_task = run_json(
            temp,
            "new-task",
            "--project",
            str(exact_project),
            "--title",
            "preserve nano budget",
            "--purpose",
            "prove CLI and durable state never round through binary float",
            "--provider",
            "longcat",
            "--estimated-input-tokens",
            "1000000",
            "--max-cost-cny",
            exact_budget,
        )["task_id"]
        exact_project_state = json.loads(
            (exact_project / "project.json").read_text(encoding="utf-8")
        )
        assert exact_project_state["routing_policy"]["project_budget_cny"] == exact_budget
        assert task_payload(exact_project, exact_task)["max_cost_cny"] == exact_budget
        exact_dispatch = run_json(
            temp,
            "dispatch",
            "--project",
            str(exact_project),
            "--task",
            exact_task,
            "--provider",
            "longcat",
            "--unsafe-native",
        )
        exact_usage = run_json(
            temp,
            "record-usage",
            "--project",
            str(exact_project),
            "--actor",
            exact_dispatch["actor_id"],
            "--task",
            exact_task,
            "--attempt",
            task_payload(exact_project, exact_task)["attempts"][0]["attempt_id"],
            "--estimated-cost-cny",
            exact_budget,
        )
        assert exact_usage["event"]["estimated_cost_cny"] == exact_budget

        release_project = make_project(temp, "envelope-release", 6.5)
        too_small = new_chain_task(temp, release_project, "task cap", max_cost=1.5)
        blocked = run(
            temp,
            "dispatch",
            "--project",
            str(release_project),
            "--task",
            too_small,
            "--unsafe-native",
            ok=False,
        )
        assert "Task budget exceeded" in (blocked.stdout + blocked.stderr)

        chain_task = new_chain_task(temp, release_project, "reserve full chain")
        first = run_json(
            temp,
            "dispatch",
            "--project",
            str(release_project),
            "--task",
            chain_task,
            "--unsafe-native",
        )
        chain = task_payload(release_project, chain_task)
        envelope = chain["route_budget_envelope"]
        assert envelope["status"] == "active"
        assert envelope["reserved_cost_cny"] == "6"
        assert [step["provider_id"] for step in envelope["planned_steps"]] == [
            "longcat",
            "deepseek",
            "codex",
        ]
        assert chain["attempts"][0]["reserved_cost_cny"] == "1"
        active_result = run(
            temp,
            "record-result",
            "--command-id",
            "CMD-active-result-rejected",
            "--project",
            str(release_project),
            "--task",
            chain_task,
            "--attempt",
            chain["attempts"][0]["attempt_id"],
            "--status",
            "done",
            "--accepted-by-leader",
            "--quality-score",
            "5",
            ok=False,
        )
        assert "collect a finished report first" in (
            active_result.stdout + active_result.stderr
        )

        competing = run_json(
            temp,
            "new-task",
            "--project",
            str(release_project),
            "--title",
            "cannot steal reserved tail",
            "--purpose",
            "project envelope isolation",
            "--provider",
            "longcat",
            "--estimated-input-tokens",
            "1000000",
        )["task_id"]
        blocked = run(
            temp,
            "dispatch",
            "--project",
            str(release_project),
            "--task",
            competing,
            "--provider",
            "longcat",
            "--unsafe-native",
            ok=False,
        )
        assert "Project budget exceeded" in (blocked.stdout + blocked.stderr)

        run_json(
            temp,
            "collect",
            "--project",
            str(release_project),
            "--task",
            chain_task,
            "--state",
            "waiting_leader",
        )
        run_json(
            temp,
            "record-result",
            "--command-id",
            "CMD-release-chain-result",
            "--project",
            str(release_project),
            "--task",
            chain_task,
            "--attempt",
            chain["attempts"][0]["attempt_id"],
            "--actor",
            first["actor_id"],
            "--status",
            "done",
            "--accepted-by-leader",
            "--quality-score",
            "5",
            "--input-tokens",
            "1000000",
        )
        released = task_payload(release_project, chain_task)["route_budget_envelope"]
        assert released["status"] == "released"
        # A leader outcome releases only unused future steps. The current API
        # hold remains until a bound final-usage receipt arrives.
        assert run_json(temp, "budget", "--project", str(release_project))["commitment_cny"] == 1.0
        run_json(
            temp,
            "record-usage",
            "--project",
            str(release_project),
            "--actor",
            first["actor_id"],
            "--task",
            chain_task,
            "--attempt",
            chain["attempts"][0]["attempt_id"],
            "--input-tokens",
            "1000000",
            "--final",
        )
        duplicate_final = run(
            temp,
            "record-usage",
            "--project",
            str(release_project),
            "--actor",
            first["actor_id"],
            "--task",
            chain_task,
            "--attempt",
            chain["attempts"][0]["attempt_id"],
            "--input-tokens",
            "1",
            ok=False,
        )
        assert "Usage is already final" in (duplicate_final.stdout + duplicate_final.stderr)
        competing_dispatch = run_json(
            temp,
            "dispatch",
            "--project",
            str(release_project),
            "--task",
            competing,
            "--provider",
            "longcat",
            "--unsafe-native",
        )
        competing_payload = task_payload(release_project, competing)
        cross_bound = run(
            temp,
            "record-usage",
            "--project",
            str(release_project),
            "--actor",
            first["actor_id"],
            "--task",
            competing,
            "--attempt",
            competing_payload["attempts"][0]["attempt_id"],
            "--input-tokens",
            "1",
            ok=False,
        )
        assert "binding mismatch" in (cross_bound.stdout + cross_bound.stderr)
        assert competing_dispatch["actor_id"] == competing_payload["attempts"][0]["actor_id"]

        escalation_project = make_project(temp, "envelope-escalation", 6.0)
        escalation_task = new_chain_task(temp, escalation_project, "reuse envelope")
        run_json(
            temp,
            "dispatch",
            "--project",
            str(escalation_project),
            "--task",
            escalation_task,
            "--unsafe-native",
        )
        for expected_provider in ("deepseek", "codex"):
            reject_latest_attempt(
                temp,
                escalation_project,
                escalation_task,
                f"escalation-{expected_provider}",
            )
            run_json(
                temp,
                "escalate",
                "--project",
                str(escalation_project),
                "--task",
                escalation_task,
                "--reason",
                f"continue to {expected_provider}",
                "--unsafe-native",
            )
            current = task_payload(escalation_project, escalation_task)
            assert current["attempts"][-1]["provider"] == expected_provider
            budget = run_json(temp, "budget", "--project", str(escalation_project))
            assert budget["commitment_cny"] == 6.0
        final_chain = task_payload(escalation_project, escalation_task)
        assert [attempt["route_plan_step_index"] for attempt in final_chain["attempts"]] == [0, 1, 2]
        remove_seed_evidence(escalation_project)
        assert run_json(temp, "validate", "--project", str(escalation_project))["status"] == "ok"

        revision_project = make_project(temp, "envelope-revision", 10.0)
        override_task = new_chain_task(temp, revision_project, "reject model override")
        override = run(
            temp,
            "dispatch",
            "--project",
            str(revision_project),
            "--task",
            override_task,
            "--model",
            "unpriced-override-model",
            "--unsafe-native",
            ok=False,
        )
        assert "model/profile override" in (override.stdout + override.stderr)
        assert task_payload(revision_project, override_task)["attempts"] == []

        revision_project_json = revision_project / "project.json"
        revision_state = json.loads(revision_project_json.read_text(encoding="utf-8"))
        for provider in revision_state["provider_catalog"]["providers"]:
            if provider["provider_id"] in {"deepseek", "codex"}:
                provider["enabled"] = False
        revision_project_json.write_text(
            json.dumps(revision_state, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        early_stop = run_json(
            temp,
            "new-task",
            "--project",
            str(revision_project),
            "--title",
            "honor early stop",
            "--purpose",
            "automatic workers may not expand a selected single-step plan",
            "--estimated-input-tokens",
            "1000000",
        )["task_id"]
        early_dispatch = run_json(
            temp,
            "dispatch",
            "--project",
            str(revision_project),
            "--task",
            early_stop,
            "--unsafe-native",
        )
        early_payload = task_payload(revision_project, early_stop)
        early_attempt = early_payload["attempts"][0]
        assert len(early_payload["route_budget_envelope"]["planned_steps"]) == 1
        for provider in revision_state["provider_catalog"]["providers"]:
            if provider["provider_id"] in {"deepseek", "codex"}:
                provider["enabled"] = True
        revision_project_json.write_text(
            json.dumps(revision_state, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        reject_latest_attempt(temp, revision_project, early_stop, "early-stop")
        automatic = run(
            temp,
            "escalate",
            "--project",
            str(revision_project),
            "--task",
            early_stop,
            "--reason",
            "worker requested an unplanned continuation",
            "--from-actor",
            early_dispatch["actor_id"],
            "--attempt",
            early_attempt["attempt_id"],
            "--unsafe-native",
            ok=False,
        )
        assert "automatic escalation cannot revise" in (automatic.stdout + automatic.stderr)
        assert len(task_payload(revision_project, early_stop)["attempts"]) == 1
        run_json(
            temp,
            "escalate",
            "--project",
            str(revision_project),
            "--task",
            early_stop,
            "--provider",
            "codex",
            "--reason",
            "leader explicitly authorizes a newly admitted step",
            "--unsafe-native",
        )
        revised = task_payload(revision_project, early_stop)
        assert revised["attempts"][-1]["provider"] == "codex"
        assert revised["route_budget_envelope_history"][-1]["release_reason"] == "explicit_plan_revision"

        sla_project = make_project(temp, "envelope-sla-revision", 20.0)
        sla_task = new_chain_task(temp, sla_project, "preserve frozen success floor")
        run_json(
            temp,
            "dispatch",
            "--project",
            str(sla_project),
            "--task",
            sla_task,
            "--unsafe-native",
        )
        reject_latest_attempt(temp, sla_project, sla_task, "sla-low")
        before_rejected_revision = task_payload(sla_project, sla_task)
        rejected_revision = run(
            temp,
            "escalate",
            "--project",
            str(sla_project),
            "--task",
            sla_task,
            "--provider",
            "codex",
            "--replan",
            "--reason",
            "must not weaken the frozen success floor",
            "--unsafe-native",
            ok=False,
        )
        assert "cross-envelope predecessor-conditioned evidence" in (
            rejected_revision.stdout + rejected_revision.stderr
        )
        assert task_payload(sla_project, sla_task) == before_rejected_revision
        run_json(
            temp,
            "escalate",
            "--project",
            str(sla_project),
            "--task",
            sla_task,
            "--reason",
            "consume the admitted medium step",
            "--unsafe-native",
        )
        reject_latest_attempt(temp, sla_project, sla_task, "sla-medium")
        rejected_correlated_replan = run(
            temp,
            "escalate",
            "--project",
            str(sla_project),
            "--task",
            sla_task,
            "--provider",
            "codex",
            "--replan",
            "--reason",
            "prior attempts now make the revised route satisfy the floor",
            "--unsafe-native",
            ok=False,
        )
        assert "cross-envelope predecessor-conditioned evidence" in (
            rejected_correlated_replan.stdout + rejected_correlated_replan.stderr
        )
        sla_replanned = task_payload(sla_project, sla_task)
        assert [row["provider"] for row in sla_replanned["attempts"]] == [
            "longcat",
            "deepseek",
        ]

        drift_project = make_project(temp, "envelope-drift", 10.0)
        drift_task = new_chain_task(
            temp,
            drift_project,
            "price drift",
            min_success="0.10",
        )
        run_json(
            temp,
            "dispatch",
            "--project",
            str(drift_project),
            "--task",
            drift_task,
            "--unsafe-native",
        )
        reject_latest_attempt(temp, drift_project, drift_task, "drift-low")
        project_json = drift_project / "project.json"
        project_state = json.loads(project_json.read_text(encoding="utf-8"))
        for provider in project_state["provider_catalog"]["providers"]:
            if provider["provider_id"] == "deepseek":
                previous = provider["pricing"]
                provider["pricing"] = build_pricing_snapshot(
                    currency="CNY",
                    source="https://pricing.example/deepseek-drifted",
                    reviewed_at=previous["reviewed_at"],
                    effective_at=previous["effective_at"],
                    expires_at=previous["expires_at"],
                    snapshot_id="deepseek-drifted",
                    input_per_1m=20.0,
                    cached_input_per_1m=20.0,
                    output_per_1m=20.0,
                    fixed_attempt=0.0,
                )
        project_json.write_text(json.dumps(project_state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        drifted = run(
            temp,
            "escalate",
            "--project",
            str(drift_project),
            "--task",
            drift_task,
            "--reason",
            "must reject changed quote",
            "--unsafe-native",
            ok=False,
        )
        assert "price basis drifted" in (drifted.stdout + drifted.stderr)
        assert len(task_payload(drift_project, drift_task)["attempts"]) == 1
        unsafe_replan = run(
            temp,
            "escalate",
            "--project",
            str(drift_project),
            "--task",
            drift_task,
            "--provider",
            "codex",
            "--replan",
            "--reason",
            "leader atomically replaces the drifted tail within budget",
            "--unsafe-native",
            ok=False,
        )
        assert "cross-envelope predecessor-conditioned evidence" in (
            unsafe_replan.stdout + unsafe_replan.stderr
        )

        print("route budget envelope ok")
        return 0
    finally:
        shutil.rmtree(temp, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
