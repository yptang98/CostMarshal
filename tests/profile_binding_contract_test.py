from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
CLI = ROOT / "scripts" / "costmarshal.py"
sys.path.insert(0, str(ROOT))

from costmarshal_v2.actor_runner import _validate_worker_fence  # noqa: E402
from costmarshal_v2.paths import ProjectLayout  # noqa: E402
from costmarshal_v2.profiles import provider_profile_text  # noqa: E402
from costmarshal_v2.routing import default_provider_catalog  # noqa: E402


def run_json(temp: Path, env: dict[str, str], *args: str) -> dict:
    result = subprocess.run(
        [sys.executable, str(CLI), "--root", str(temp / "runtime"), *args],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
        check=False,
    )
    if result.returncode != 0:
        raise AssertionError(f"command failed: {args}\n{result.stdout}\n{result.stderr}")
    return json.loads(result.stdout)


def load_task(project: Path, task_id: str) -> dict:
    return json.loads((project / "tasks" / task_id / "task.json").read_text(encoding="utf-8"))


def main() -> int:
    temp = Path(tempfile.mkdtemp(prefix="costmarshal-profile-binding-"))
    try:
        workspace = temp / "workspace"
        workspace.mkdir()
        codex_home = temp / "codex-home"
        codex_home.mkdir()
        low_text = provider_profile_text(
            provider_id="longcat",
            display_name="LongCat",
            base_url="https://low.example/v1",
            model="LongCat-2.0",
            env_key="LONGCAT_API_KEY",
            wire_api="responses",
        )
        medium_text = provider_profile_text(
            provider_id="deepseek",
            display_name="DeepSeek",
            base_url="https://medium.example/v1",
            model="deepseek-v1",
            env_key="DEEPSEEK_API_KEY",
            wire_api="responses",
        )
        (codex_home / "longcat.config.toml").write_text(low_text, encoding="utf-8")
        medium_source = codex_home / "deepseek.config.toml"
        medium_source.write_text(medium_text, encoding="utf-8")
        env = dict(os.environ)
        env["CODEX_HOME"] = str(codex_home)

        catalog = default_provider_catalog()
        for provider, price in zip(catalog["providers"], (1.0, 2.0, 3.0), strict=True):
            provider["input_cny_per_1m"] = price
            provider["output_cny_per_1m"] = price
        catalog_path = temp / "catalog.json"
        catalog_path.write_text(json.dumps(catalog), encoding="utf-8")
        project = Path(
            run_json(
                temp,
                env,
                "init",
                "--objective",
                "bind three provider profiles",
                "--workspace",
                str(workspace),
                "--provider-catalog",
                str(catalog_path),
                "--allow-unsafe-native-workers",
                "--governance",
                "off",
            )["project"]
        )
        run_json(
            temp,
            env,
            "new-task",
            "--project",
            str(project),
            "--title",
            "three profiles",
            "--purpose",
            "bind the admitted bytes",
            "--estimated-input-tokens",
            "1000000",
            "--min-success-probability",
            "0.15",
        )
        run_json(
            temp,
            env,
            "dispatch",
            "--project",
            str(project),
            "--task",
            "V2-0001",
            "--unsafe-native",
        )
        first = load_task(project, "V2-0001")
        envelope = first["route_budget_envelope"]
        bindings = [step["profile_binding"] for step in envelope["planned_steps"]]
        assert [row["status"] for row in bindings] == ["available", "available", "available"]
        assert [row["source_kind"] for row in bindings] == [
            "named-profile",
            "named-profile",
            "synthetic-default",
        ]
        assert len({row["sha256"] for row in bindings}) == 3
        assert envelope["plan_fingerprint"] != first["route_decision"]["plan_fingerprint"]
        for row in bindings:
            snapshot = temp / "runtime" / row["snapshot_relpath"]
            assert snapshot.is_file()

        changed_medium = medium_text.replace("https://medium.example/v1", "https://medium-new.example/v1")
        medium_source.write_text(changed_medium, encoding="utf-8")
        run_json(
            temp,
            env,
            "escalate",
            "--project",
            str(project),
            "--task",
            "V2-0001",
            "--reason",
            "exercise immutable continuation",
            "--unsafe-native",
        )
        continued = load_task(project, "V2-0001")
        medium_attempt = continued["attempts"][-1]
        assert medium_attempt["provider"] == "deepseek"
        assert medium_attempt["profile_binding"] == bindings[1]
        admitted_medium_snapshot = temp / "runtime" / bindings[1]["snapshot_relpath"]
        assert admitted_medium_snapshot.read_text(encoding="utf-8") == medium_text

        run_json(
            temp,
            env,
            "new-task",
            "--project",
            str(project),
            "--title",
            "changed profile",
            "--purpose",
            "prove profile bytes affect the plan identity",
            "--estimated-input-tokens",
            "1000000",
            "--min-success-probability",
            "0.15",
        )
        run_json(
            temp,
            env,
            "dispatch",
            "--project",
            str(project),
            "--task",
            "V2-0002",
            "--unsafe-native",
        )
        second = load_task(project, "V2-0002")
        assert second["route_budget_envelope"]["plan_fingerprint"] != envelope["plan_fingerprint"]
        assert (
            second["route_budget_envelope"]["planned_steps"][2]["profile_binding"]["sha256"]
            == bindings[2]["sha256"]
        )

        run_json(
            temp,
            env,
            "new-task",
            "--project",
            str(project),
            "--title",
            "missing future profile",
            "--purpose",
            "fail before admitting an incomplete chain",
            "--estimated-input-tokens",
            "1000000",
            "--min-success-probability",
            "0.15",
        )
        medium_source.unlink()
        missing_dispatch = subprocess.run(
            [
                sys.executable,
                str(CLI),
                "--root",
                str(temp / "runtime"),
                "dispatch",
                "--project",
                str(project),
                "--task",
                "V2-0003",
                "--unsafe-native",
            ],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
            check=False,
        )
        assert missing_dispatch.returncode != 0
        assert "profile is unavailable at route admission" in (
            missing_dispatch.stdout + missing_dispatch.stderr
        )
        missing_task = load_task(project, "V2-0003")
        assert missing_task["attempts"] == []
        assert "route_budget_envelope" not in missing_task
        medium_source.write_text(changed_medium, encoding="utf-8")

        # validate must cover unexecuted active-envelope snapshots, not only
        # the profile attached to an already-created attempt.
        future_binding = second["route_budget_envelope"]["planned_steps"][1][
            "profile_binding"
        ]
        future_snapshot = temp / "runtime" / future_binding["snapshot_relpath"]
        future_payload = future_snapshot.read_bytes()
        future_snapshot.chmod(stat.S_IREAD | stat.S_IWRITE)
        future_snapshot.write_bytes(future_payload + b"\n# future tamper\n")
        future_validation = subprocess.run(
            [
                sys.executable,
                str(CLI),
                "--root",
                str(temp / "runtime"),
                "validate",
                "--project",
                str(project),
            ],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
            check=False,
        )
        assert future_validation.returncode != 0
        assert "active route budget envelope is not executable" in (
            future_validation.stdout + future_validation.stderr
        )
        future_snapshot.write_bytes(future_payload)

        # Corrupting a durable snapshot is detected by the actor/attempt fence
        # before native or OCI provider execution can begin.
        current_attempt = second["attempts"][-1]
        current_binding = current_attempt["profile_binding"]
        current_snapshot = temp / "runtime" / current_binding["snapshot_relpath"]
        current_snapshot.chmod(stat.S_IREAD | stat.S_IWRITE)
        current_snapshot.write_bytes(current_snapshot.read_bytes() + b"\n# tampered\n")
        layout = ProjectLayout(root=temp / "runtime", project_dir=project)
        try:
            _validate_worker_fence(
                layout,
                str(current_attempt["actor_id"]),
                attempt_id=str(current_attempt["attempt_id"]),
                launch_token=str(current_attempt["launch_token"]),
            )
        except SystemExit as exc:
            assert "profile binding failed closed" in str(exc)
        else:
            raise AssertionError("tampered profile snapshot passed the worker fence")

        validation = subprocess.run(
            [
                sys.executable,
                str(CLI),
                "--root",
                str(temp / "runtime"),
                "validate",
                "--project",
                str(project),
            ],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
            check=False,
        )
        assert validation.returncode != 0
        print("profile binding contract ok")
        return 0
    finally:
        shutil.rmtree(temp, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
