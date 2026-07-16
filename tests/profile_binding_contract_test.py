from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
CLI = ROOT / "scripts" / "costmarshal.py"
sys.path.insert(0, str(ROOT))

from costmarshal_v2.actor_runner import _validate_worker_fence  # noqa: E402
from costmarshal_v2.paths import ProjectLayout  # noqa: E402
from costmarshal_v2.profile_binding import _stat_identity, read_named_profile  # noqa: E402
from costmarshal_v2.profiles import provider_profile_text  # noqa: E402
from costmarshal_v2.routing import default_provider_catalog  # noqa: E402
from project_success_policy_test import seed_paired_route_evidence  # noqa: E402


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


def reject_latest_attempt(temp: Path, env: dict[str, str], project: Path, task_id: str) -> dict:
    task = load_task(project, task_id)
    attempt = task["attempts"][-1]
    actor = str(attempt["actor_id"])
    attempt_id = str(attempt["attempt_id"])
    (project / "tasks" / task_id / "completion-report.md").write_text(
        f"# Completion Report: {task_id}\n\nStatus: escalate\n\nLeader requires the next bound profile.\n",
        encoding="utf-8",
    )
    run_json(temp, env, "heartbeat", "--project", str(project), "--actor", actor, "--status", "waiting")
    run_json(
        temp,
        env,
        "collect",
        "--command-id",
        f"CMD-profile-collect-{task_id}",
        "--project",
        str(project),
        "--task",
        task_id,
        "--attempt",
        attempt_id,
        "--actor",
        actor,
        "--state",
        "escalate",
    )
    run_json(
        temp,
        env,
        "record-result",
        "--command-id",
        f"CMD-profile-result-{task_id}",
        "--project",
        str(project),
        "--task",
        task_id,
        "--attempt",
        attempt_id,
        "--actor",
        actor,
        "--status",
        "escalate",
        "--quality-score",
        "3",
        "--summary",
        "leader rejected the current bound profile",
    )
    return attempt


def remove_paired_evidence(project: Path) -> None:
    seeded_task_ids: set[str] = set()
    for task_dir in (project / "tasks").iterdir():
        task_path = task_dir / "task.json"
        if not task_path.is_file():
            continue
        task = json.loads(task_path.read_text(encoding="utf-8"))
        task_spec = (task.get("handoff_contract") or {}).get("task_spec") or {}
        if (
            task_spec.get("title") == "paired evidence"
            and task_spec.get("purpose") == "project success fixture"
        ):
            seeded_task_ids.add(str(task.get("id") or task_dir.name))
            shutil.rmtree(task_dir)
    results_path = project / "reports" / "results.jsonl"
    retained = [
        line
        for line in results_path.read_text(encoding="utf-8").splitlines()
        if line.strip() and str(json.loads(line).get("task_id") or "") not in seeded_task_ids
    ]
    results_path.write_text(
        "".join(f"{line}\n" for line in retained),
        encoding="utf-8",
    )


def reset_paired_evidence(temp: Path, project: Path) -> None:
    remove_paired_evidence(project)
    seed_paired_route_evidence(temp, project)


def main() -> int:
    temp = Path(tempfile.mkdtemp(prefix="costmarshal-profile-binding-"))
    previous_codex_home = os.environ.get("CODEX_HOME")
    try:
        if os.name == "nt":
            common_stat = {
                "st_dev": 1,
                "st_ino": 2,
                "st_size": 3,
                "st_mtime_ns": 4,
                "st_birthtime_ns": 5,
            }
            path_stat = SimpleNamespace(**common_stat, st_ctime_ns=6)
            descriptor_stat = SimpleNamespace(**common_stat, st_ctime_ns=7)
            assert _stat_identity(path_stat) == _stat_identity(descriptor_stat)

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

        # Profile generation and route admission must agree on Codex's default
        # home when CODEX_HOME is unset.
        os.environ.pop("CODEX_HOME", None)
        fallback_user_home = temp / "fallback-user"
        fallback_codex_home = fallback_user_home / ".codex"
        fallback_codex_home.mkdir(parents=True)
        fallback_profile = fallback_codex_home / "longcat.config.toml"
        fallback_profile.write_text(
            low_text.replace("https://low.example/v1", "https://fallback.example/v1"),
            encoding="utf-8",
        )
        with patch("costmarshal_v2.profiles.Path.home", return_value=fallback_user_home):
            fallback_material = read_named_profile(
                "longcat",
                expected_env_key="LONGCAT_API_KEY",
                snapshot_relpath="profile-snapshots/fallback/longcat.config.toml",
            )
        assert fallback_material is not None
        assert fallback_material[0] == fallback_profile.read_bytes()

        env = dict(os.environ)
        env["CODEX_HOME"] = str(codex_home)
        os.environ["CODEX_HOME"] = str(codex_home)
        env_material = read_named_profile(
            "longcat",
            expected_env_key="LONGCAT_API_KEY",
            snapshot_relpath="profile-snapshots/env/longcat.config.toml",
        )
        explicit_material = read_named_profile(
            "longcat",
            expected_env_key="LONGCAT_API_KEY",
            snapshot_relpath="profile-snapshots/explicit/longcat.config.toml",
            codex_home=fallback_codex_home,
        )
        assert env_material is not None and explicit_material is not None
        assert env_material[0] == (codex_home / "longcat.config.toml").read_bytes()
        assert explicit_material[0] == fallback_profile.read_bytes()

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
        seed_paired_route_evidence(temp, project)
        first_task_id = run_json(
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
        )["task_id"]
        run_json(
            temp,
            env,
            "dispatch",
            "--project",
            str(project),
            "--task",
            first_task_id,
            "--unsafe-native",
        )
        first = load_task(project, first_task_id)
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
        first_attempt = reject_latest_attempt(temp, env, project, first_task_id)
        run_json(
            temp,
            env,
            "escalate",
            "--command-id",
            f"CMD-profile-escalate-{first_task_id}",
            "--project",
            str(project),
            "--task",
            first_task_id,
            "--attempt",
            str(first_attempt["attempt_id"]),
            "--from-actor",
            str(first_attempt["actor_id"]),
            "--reason",
            "exercise immutable continuation",
            "--unsafe-native",
        )
        continued = load_task(project, first_task_id)
        medium_attempt = continued["attempts"][-1]
        assert medium_attempt["provider"] == "deepseek"
        assert medium_attempt["profile_binding"] == bindings[1]
        admitted_medium_snapshot = temp / "runtime" / bindings[1]["snapshot_relpath"]
        assert admitted_medium_snapshot.read_text(encoding="utf-8") == medium_text

        # A changed profile is a new execution identity. Refresh only the
        # synthetic paired fixture so the second admission has exact evidence
        # for the changed bytes; old-profile observations must not leak across.
        reset_paired_evidence(temp, project)
        second_task_id = run_json(
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
        )["task_id"]
        run_json(
            temp,
            env,
            "dispatch",
            "--project",
            str(project),
            "--task",
            second_task_id,
            "--unsafe-native",
        )
        second = load_task(project, second_task_id)
        assert second["route_budget_envelope"]["plan_fingerprint"] != envelope["plan_fingerprint"]
        assert (
            second["route_budget_envelope"]["planned_steps"][2]["profile_binding"]["sha256"]
            == bindings[2]["sha256"]
        )

        missing_task_id = run_json(
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
        )["task_id"]
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
                missing_task_id,
                "--unsafe-native",
            ],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
            check=False,
        )
        assert missing_dispatch.returncode != 0
        missing_text = missing_dispatch.stdout + missing_dispatch.stderr
        assert any(
            message in missing_text
            for message in (
                "profile is unavailable at route admission",
                "no priced provider chain satisfies minimum success probability",
            )
        ), missing_text
        missing_task = load_task(project, missing_task_id)
        assert missing_task["attempts"] == []
        assert "route_budget_envelope" not in missing_task
        medium_source.write_text(changed_medium, encoding="utf-8")

        # validate must cover unexecuted active-envelope snapshots, not only
        # the profile attached to an already-created attempt.
        remove_paired_evidence(project)
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
        future_validation_text = future_validation.stdout + future_validation.stderr
        assert "active route budget envelope is not executable" in future_validation_text, future_validation_text
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
        if previous_codex_home is None:
            os.environ.pop("CODEX_HOME", None)
        else:
            os.environ["CODEX_HOME"] = previous_codex_home
        shutil.rmtree(temp, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
