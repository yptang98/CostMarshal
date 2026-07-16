from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
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


def main() -> int:
    temp = Path(tempfile.mkdtemp(prefix="costmarshal-success-policy-"))
    try:
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
        assert zero_task["route_preview"]["planned_provider_ids"] == ["longcat"]

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
        shutil.rmtree(temp, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
