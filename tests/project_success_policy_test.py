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


ROOT = Path(__file__).resolve().parents[1]
CLI = ROOT / "scripts" / "costmarshal.py"


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


def seed_paired_route_evidence(temp: Path, project: Path) -> None:
    project_state = json.loads((project / "project.json").read_text(encoding="utf-8"))
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
    rows = []
    for index in range(20):
        task_id = f"EVID-{index:04d}"
        envelope_id = f"ENV-evidence-{index}"
        fingerprint = "sha256:" + f"{index:064x}"[-64:]
        common = {
            "event_type": "result",
            "evidence_schema_version": "costmarshal-result-evidence-v2",
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
        task_attempts = []

        def add_result(
            *,
            provider: str,
            tier: str,
            model: str,
            profile: str | None,
            step: int,
            predecessors: list[dict],
            accepted: bool,
        ) -> dict:
            attempt_id = f"ATT-{provider}-{index}"
            result_id = f"RES-{provider}-{index}"
            command_id = f"CMD-evidence-{provider}-{index}"
            request_contract = {"fixture": result_id}
            request_digest = "sha256:" + hashlib.sha256(
                json.dumps(
                    request_contract,
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode("utf-8")
            ).hexdigest()
            status = "done" if accepted else "escalate"
            identity = {
                "model": model,
                "profile": profile,
                "profile_sha256": profile_hashes[provider],
            }
            attempt = {
                "attempt_id": attempt_id,
                "actor_id": None,
                "provider": provider,
                "tier": tier,
                "profile": profile,
                "model": model,
                "execution_identity": identity,
                "profile_binding": {"sha256": profile_hashes[provider]},
                "route_envelope_id": envelope_id,
                "route_plan_fingerprint": fingerprint,
                "route_plan_step_index": step,
                "route_predecessors": predecessors,
                "leader_result_id": result_id,
                "accepted_by_leader": accepted,
                "quality_score": 4,
                "recorded_result_status": status,
                "result_request_contract_sha256": request_digest,
                "actual_cost_cny": "0",
                "reserved_cost_cny": "0",
                "cost_settled": True,
                "report_path": None,
                "report_sha256": None,
                "report_size": None,
            }
            task_attempts.append(attempt)
            row = {
                **common,
                "id": result_id,
                "command_id": command_id,
                "request_contract": request_contract,
                "request_contract_sha256": request_digest,
                "attempt_id": attempt_id,
                "actor_id": None,
                "provider": provider,
                "tier": tier,
                "model": model,
                "execution_model": model,
                "profile": profile,
                "profile_sha256": profile_hashes[provider],
                "route_plan_step_index": step,
                "route_predecessors": predecessors,
                "accepted_by_leader": accepted,
                "status": status,
                "report_path": None,
                "report_sha256": None,
                "report_size": None,
            }
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
            predecessors=[],
            accepted=False,
        )
        medium_accepted = index < 4
        medium = add_result(
            provider="deepseek",
            tier="medium",
            model=str(profile_models["deepseek"]),
            profile="deepseek",
            step=1,
            predecessors=[low],
            accepted=medium_accepted,
        )
        if not medium_accepted:
            add_result(
                provider="codex",
                tier="high",
                model="inherit",
                profile=None,
                step=2,
                predecessors=[low, medium],
                accepted=index < 7,
            )
        task_dir = project / "tasks" / task_id
        task_dir.mkdir(parents=True)
        (task_dir / "task.json").write_text(
            json.dumps(
                {"id": task_id, "attempts": task_attempts},
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
    results = project / "reports" / "results.jsonl"
    with results.open("a", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n")


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
        assert zero_task["route_preview"]["planned_provider_ids"] == ["deepseek"]

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
