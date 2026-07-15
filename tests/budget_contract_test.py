from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from costmarshal_v2.routing import default_provider_catalog  # noqa: E402


CLI = ROOT / "scripts" / "costmarshal.py"


def run(temp: Path, *args: str, ok: bool = True) -> subprocess.CompletedProcess[str]:
    env = dict(os.environ)
    env["COSTMARSHAL_V2_HOME"] = str(temp / "runtime")
    result = subprocess.run([sys.executable, str(CLI), *args], env=env, text=True, capture_output=True)
    if ok and result.returncode:
        raise AssertionError(f"command failed {args}\n{result.stdout}\n{result.stderr}")
    return result


def data(temp: Path, *args: str) -> dict:
    return json.loads(run(temp, *args).stdout)


def main() -> int:
    temp = Path(tempfile.mkdtemp(prefix="costmarshal-v2-budget-"))
    try:
        workspace = temp / "workspace"
        workspace.mkdir()
        catalog = default_provider_catalog()
        for provider in catalog["providers"]:
            provider["input_cny_per_1m"] = 1.0
            provider["output_cny_per_1m"] = 1.0
        catalog_path = temp / "catalog.json"
        catalog_path.write_text(json.dumps(catalog), encoding="utf-8")
        init = data(
            temp,
            "init",
            "--name",
            "budget",
            "--objective",
            "hard reservation",
            "--workspace",
            str(workspace),
            "--provider-catalog",
            str(catalog_path),
            "--project-budget-cny",
            "1.5",
            "--governance",
            "off",
            "--allow-unsafe-native-workers",
        )
        project = Path(init["project"])
        for index in (1, 2):
            data(
                temp,
                "new-task",
                "--project",
                str(project),
                "--title",
                f"task {index}",
                "--purpose",
                "budget test",
                "--risk",
                "low",
                "--provider",
                "longcat",
                "--estimated-input-tokens",
                "1000000",
            )
        first = data(temp, "dispatch", "--project", str(project), "--task", "V2-0001", "--provider", "longcat", "--unsafe-native")
        rejected = run(temp, "dispatch", "--project", str(project), "--task", "V2-0002", "--provider", "longcat", "--unsafe-native", ok=False)
        assert rejected.returncode != 0 and "Project budget exceeded" in (rejected.stdout + rejected.stderr)

        actor = first["actor_id"]
        actor_state = json.loads((project / "scheduler" / "actors" / f"{actor}.json").read_text(encoding="utf-8"))
        data(
            temp,
            "record-usage",
            "--project",
            str(project),
            "--actor",
            actor,
            "--task",
            "V2-0001",
            "--attempt",
            actor_state["attempt_id"],
            "--input-tokens",
            "250000",
        )
        rejected = run(temp, "dispatch", "--project", str(project), "--task", "V2-0002", "--provider", "longcat", "--unsafe-native", ok=False)
        assert rejected.returncode != 0
        task = json.loads((project / "tasks" / "V2-0001" / "task.json").read_text(encoding="utf-8"))
        attempt = task["attempts"][0]
        assert attempt["reserved_cost_cny"] == 1.0
        assert attempt["actual_cost_cny"] == 0.25
        assert not attempt.get("cost_settled")
        data(
            temp,
            "record-result",
            "--project",
            str(project),
            "--task",
            "V2-0001",
            "--attempt",
            actor_state["attempt_id"],
            "--status",
            "failed",
            "--quality-score",
            "1",
        )
        second = data(temp, "dispatch", "--project", str(project), "--task", "V2-0002", "--provider", "longcat", "--unsafe-native")
        assert second["actor_id"] == "agent-v2-0002"
        print("budget contract ok")
        return 0
    finally:
        shutil.rmtree(temp, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
