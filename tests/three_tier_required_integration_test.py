#!/usr/bin/env python3
"""Fresh required-OCI bootstrap chains obey the real leader state machine."""

from __future__ import annotations

import contextlib
import io
import json
import hashlib
import os
import shutil
import subprocess
import sys
import tempfile
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Mapping, Sequence
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(ROOT / "tests") not in sys.path:
    sys.path.insert(0, str(ROOT / "tests"))

import costmarshal_v2.actor_runner as actor_runner  # noqa: E402
from costmarshal_v2.cli import build_parser  # noqa: E402
from costmarshal_v2.paths import ProjectLayout  # noqa: E402
from costmarshal_v2.profiles import provider_profile_text  # noqa: E402
from costmarshal_v2.routing import (  # noqa: E402
    build_pricing_snapshot,
    default_provider_catalog,
)
from costmarshal_v2.scheduler import audit_result_evidence  # noqa: E402
from costmarshal_v2.state import (  # noqa: E402
    load_actor,
    load_task,
    save_task,
)
from costmarshal_v2.worker_isolation import (  # noqa: E402
    IsolationAttestation,
    OciCliBackend,
    validate_execution_spec,
)
from oci_actor_runner_test import run_actor_fixture  # noqa: E402
CLI = ROOT / "scripts" / "costmarshal.py"
IMAGE = "ghcr.io/example/costmarshal-worker@sha256:" + "7" * 64


def simulated_windows_job_runtime(
    *,
    expected_child_pid: object = None,
    expected_child_start_marker: object = None,
) -> dict[str, object]:
    """Model a verified Job receipt when this test calls run_actor directly."""

    authority_pid = (
        int(expected_child_pid)
        if type(expected_child_pid) is int and expected_child_pid > 0
        else os.getpid()
    )
    marker = (
        str(expected_child_start_marker)
        if isinstance(expected_child_start_marker, str)
        and expected_child_start_marker
        else actor_runner.pid_start_marker(authority_pid)
        or f"test-marker:{authority_pid}"
    )
    return {
        "pid": authority_pid,
        "process_start_marker": marker,
        "target": "job:costmarshal-three-tier-required-test",
        "windows_job_name": "costmarshal-three-tier-required-test",
        "windows_job_identity": "b" * 64,
        "windows_job_child_pid": authority_pid,
        "windows_job_child_start_marker": marker,
    }


if os.name == "nt":
    actor_runner._inherited_windows_job_runtime = simulated_windows_job_runtime


def cli(temp: Path, *arguments: str) -> dict:
    environment = os.environ.copy()
    environment["COSTMARSHAL_V2_HOME"] = str(temp / "runtime")
    result = subprocess.run(
        [
            sys.executable,
            str(CLI),
            "--root",
            str(temp / "runtime"),
            *arguments,
        ],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=environment,
        check=False,
    )
    if result.returncode:
        raise AssertionError(
            f"command failed: {arguments}\n{result.stdout}\n{result.stderr}"
        )
    return json.loads(result.stdout)


def cli_failure(temp: Path, *arguments: str) -> str:
    environment = os.environ.copy()
    environment["COSTMARSHAL_V2_HOME"] = str(temp / "runtime")
    result = subprocess.run(
        [
            sys.executable,
            str(CLI),
            "--root",
            str(temp / "runtime"),
            *arguments,
        ],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=environment,
        check=False,
    )
    if result.returncode == 0:
        raise AssertionError(f"command unexpectedly succeeded: {arguments}")
    return result.stdout + result.stderr


def git(repository: Path, *arguments: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(repository), *arguments],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if result.returncode:
        raise AssertionError(result.stderr)
    return result.stdout.strip()


class FakeOciPreflightBackend:
    """External OCI probe seam; scheduler policy/preflight remains real."""

    def __init__(self, kind: str) -> None:
        self.kind = kind

    def preflight(self, spec):
        validate_execution_spec(spec)
        if self.kind != "docker":
            raise AssertionError("docker should be the first successful required backend")
        return IsolationAttestation(
            schema="costmarshal-worker-isolation-attestation-v1",
            backend="docker",
            engine_version="fixture-engine",
            endpoint="local-fixture",
            platform="linux",
            rootless=True,
            image=spec.image,
            image_digest=spec.image_digest,
            strong_isolation=True,
            security_flags=("fixture-seccomp",),
            mounts=(),
            canary=(("fixture_boundary", True),),
            network_policy=(("network_id", "9" * 64),),
            probe_provenance="fixture-external-oci-probe",
        )


def required_cli(temp: Path, *arguments: str) -> dict:
    """Run the real required scheduler path while replacing only OCI preflight I/O."""

    parser = build_parser()
    args = parser.parse_args(["--root", str(temp / "runtime"), *arguments])
    output = io.StringIO()
    with patch(
        "costmarshal_v2.scheduler.OciCliBackend",
        side_effect=FakeOciPreflightBackend,
    ), contextlib.redirect_stdout(output):
        args.func(args)
    return json.loads(output.getvalue())


def required_cli_failure(temp: Path, *arguments: str) -> str:
    parser = build_parser()
    args = parser.parse_args(["--root", str(temp / "runtime"), *arguments])
    with patch(
        "costmarshal_v2.scheduler.OciCliBackend",
        side_effect=FakeOciPreflightBackend,
    ):
        try:
            args.func(args)
        except SystemExit as exc:
            return str(exc)
    raise AssertionError(f"required command unexpectedly succeeded: {arguments}")


FAKE_WORKER_SCRIPT = """
import json
from pathlib import Path
import sys

prompt = sys.stdin.read()
if "COSTMARSHAL-TASK-PROMPT-V1" not in prompt:
    raise SystemExit(3)
output_path = Path(sys.argv[1])
provider_env_key = sys.argv[2]
status = sys.argv[3]
output_path.write_text(
    "# Completion Report\\n\\n"
    f"Status: {status}\\n\\n"
    "## Result\\n"
    f"{provider_env_key}\\n",
    encoding="utf-8",
)
print(json.dumps({"usage": {"input_tokens": 10, "output_tokens": 5}}))
"""


class ThreeTierOciBackend(OciCliBackend):
    """Fake only the external engine/worker process behind the real adapter."""

    provider_calls: list[str] = []

    def __init__(self, kind: str) -> None:
        super().__init__(kind, host_system="Linux")
        self.spec = None
        self.command: tuple[str, ...] = ()
        self.container_name = ""
        self.container_id = ""
        self.labels: dict[str, str] = {}
        self.network_id: str | None = None

    def preflight(self, spec):
        validate_execution_spec(spec)
        if self.kind != "docker":
            raise AssertionError("docker should be the required actor backend")
        network_policy = (
            (("network_id", "9" * 64),)
            if spec.network_mode == "provider-proxy"
            else (("mode", "none"),)
        )
        return IsolationAttestation(
            schema="costmarshal-worker-isolation-attestation-v1",
            backend="docker",
            engine_version="fixture-engine",
            endpoint="local-fixture",
            platform="linux",
            rootless=True,
            image=spec.image,
            image_digest=spec.image_digest,
            strong_isolation=True,
            security_flags=("fixture-seccomp",),
            mounts=(),
            canary=(("fixture_boundary", True),),
            network_policy=network_policy,
            probe_provenance="fixture-external-oci-probe",
        )

    preflight_existing = preflight

    def build_run_argv(
        self,
        spec,
        command: Sequence[str],
        *,
        auto_remove: bool = True,
        container_name: str | None = None,
        labels: Mapping[str, str] | None = None,
        network_id: str | None = None,
    ) -> list[str]:
        validate_execution_spec(spec)
        assert auto_remove is False
        assert spec.image == IMAGE
        assert spec.workspace_mode == "ro"
        assert container_name
        self.spec = spec
        self.command = tuple(command)
        self.container_name = container_name
        self.container_id = hashlib.sha256(
            f"fixture-container:{spec.attempt_id}".encode("utf-8")
        ).hexdigest()
        self.labels = dict(labels or {})
        self.network_id = network_id
        key = str(spec.provider_env_key or "default")
        self.__class__.provider_calls.append(key)
        status = "done" if key == "CODEX_API_KEY" else "escalate"
        return [
            sys.executable,
            "-c",
            FAKE_WORKER_SCRIPT,
            str(spec.output_exchange / "final.md"),
            key,
            status,
        ]

    def _inspect_payload(self) -> dict[str, object]:
        spec = self.spec
        if spec is None:
            raise AssertionError("inspect occurred before the worker command was built")
        uid, gid = self._container_user()
        environment = [
            "CODEX_HOME=/home/worker/.codex",
            "COSTMARSHAL_PROFILE_PATH=/bootstrap/profile.config.toml",
            f"COSTMARSHAL_PROFILE_SHA256={spec.profile_sha256}",
            "COSTMARSHAL_OUTPUT_PATH=/out/final.md",
            f"COSTMARSHAL_WORKSPACE_MODE={spec.workspace_mode}",
            f"COSTMARSHAL_PROVIDER_ENV_KEY={spec.provider_env_key}",
            "COSTMARSHAL_PROVIDER_SECRET_FILE=/run/secrets/provider",
        ]
        mounts = [
            {
                "Type": "bind",
                "Source": str(spec.workspace),
                "Destination": "/workspace",
                "RW": spec.workspace_mode == "rw",
            },
            {
                "Type": "bind",
                "Source": str(spec.output_exchange),
                "Destination": "/out",
                "RW": True,
            },
            {
                "Type": "bind",
                "Source": str(spec.profile_path),
                "Destination": "/bootstrap/profile.config.toml",
                "RW": False,
            },
            {
                "Type": "bind",
                "Source": str(spec.credential_path),
                "Destination": "/run/secrets/provider",
                "RW": False,
            },
        ]
        network_mode = self.network_id or "none"
        networks = (
            {"provider-proxy": {"NetworkID": self.network_id}}
            if self.network_id
            else {}
        )
        log_driver, log_options = self._log_contract()
        return {
            "Id": self.container_id,
            "Name": "/" + self.container_name,
            "Config": {
                "Labels": self.labels,
                "Image": spec.image,
                "Cmd": list(self.command),
                "Entrypoint": None,
                "User": f"{uid}:{gid}",
                "Env": environment,
            },
            "HostConfig": {
                "ReadonlyRootfs": True,
                "CapDrop": ["ALL"],
                "CapAdd": [],
                "SecurityOpt": ["no-new-privileges"],
                "Privileged": False,
                "PidMode": "",
                "IpcMode": "",
                "PidsLimit": spec.limits.pids,
                "Memory": spec.limits.memory_mb * 1024 * 1024,
                "NanoCpus": int(round(spec.limits.cpus * 1_000_000_000)),
                "Init": True,
                "LogConfig": {"Type": log_driver, "Config": log_options},
                "Tmpfs": {
                    "/tmp": (
                        "rw,nosuid,nodev,noexec,"
                        f"size={spec.limits.tmpfs_mb}m"
                    ),
                    "/home/worker/.codex": (
                        "rw,nosuid,nodev,noexec,"
                        f"uid={uid},gid={gid},mode=0700,"
                        f"size={spec.limits.home_tmpfs_mb}m"
                    ),
                },
                "NetworkMode": network_mode,
            },
            "Mounts": mounts,
            "NetworkSettings": {"Networks": networks},
            "State": {"Status": "exited", "ExitCode": 0},
        }

    def _engine_argv(self, *args: str) -> list[str]:
        command = args[0] if args else ""
        if command == "inspect":
            payload = json.dumps(self._inspect_payload())
            return [sys.executable, "-c", "import sys; print(sys.argv[1])", payload]
        if command == "ps":
            return [sys.executable, "-c", "pass"]
        if command == "logs":
            event = json.dumps({"usage": {"input_tokens": 10, "output_tokens": 5}})
            return [sys.executable, "-c", "import sys; print(sys.argv[1])", event]
        if command in {"rm", "stop"}:
            return [sys.executable, "-c", "pass"]
        raise AssertionError(f"unexpected fixture OCI command: {args}")


def main() -> int:
    temp = Path(tempfile.mkdtemp(prefix="costmarshal-three-tier-required-"))
    previous_codex_home = os.environ.get("CODEX_HOME")
    ThreeTierOciBackend.provider_calls = []
    try:
        workspace = temp / "workspace"
        workspace.mkdir()
        (workspace / "README.md").write_text("three tier\n", encoding="utf-8")
        git(workspace, "init", "--quiet")
        git(workspace, "config", "user.name", "CostMarshal Test")
        git(workspace, "config", "user.email", "costmarshal@example.invalid")
        git(workspace, "add", "README.md")
        git(workspace, "commit", "--quiet", "-m", "base")

        codex_home = temp / "codex-home"
        codex_home.mkdir()
        for profile, provider, model, env_key in (
            ("longcat", "longcat", "low-model", "LONGCAT_API_KEY"),
            ("deepseek", "deepseek", "medium-model", "DEEPSEEK_API_KEY"),
        ):
            (codex_home / f"{profile}.config.toml").write_text(
                provider_profile_text(
                    provider_id=provider,
                    display_name=provider,
                    base_url=f"https://{provider}.invalid/v1",
                    model=model,
                    env_key=env_key,
                ),
                encoding="utf-8",
            )
        os.environ["CODEX_HOME"] = str(codex_home)
        secrets = temp / "providers.env"
        secrets.write_text(
            "LONGCAT_API_KEY=low-secret\n"
            "DEEPSEEK_API_KEY=medium-secret\n"
            "CODEX_API_KEY=high-secret\n",
            encoding="utf-8",
        )

        catalog = default_provider_catalog()
        clock = datetime.now(timezone.utc)
        reviewed = (clock - timedelta(days=1)).isoformat().replace("+00:00", "Z")
        expires = (clock + timedelta(days=30)).isoformat().replace("+00:00", "Z")
        prices = {"longcat": "1", "deepseek": "2", "codex": "3"}
        for provider in catalog["providers"]:
            if provider["provider_id"] == "longcat":
                provider["model"] = "low-model"
            elif provider["provider_id"] == "deepseek":
                provider["model"] = "medium-model"
            price = prices[provider["provider_id"]]
            provider["pricing"] = build_pricing_snapshot(
                currency="CNY",
                source="https://pricing.invalid/reviewed",
                reviewed_at=reviewed,
                effective_at=reviewed,
                expires_at=expires,
                snapshot_id=f"three-tier-{provider['provider_id']}",
                input_per_1m=price,
                cached_input_per_1m=price,
                output_per_1m=price,
                fixed_attempt="0",
            )
        catalog_path = temp / "catalog.json"
        catalog_path.write_text(json.dumps(catalog), encoding="utf-8")
        project_dir = Path(
            cli(
                temp,
                "init",
                "--objective",
                "required OCI three-tier collaboration",
                "--workspace",
                str(workspace),
                "--provider-catalog",
                str(catalog_path),
                "--project-budget-cny",
                "10",
                "--worker-image",
                IMAGE,
                "--secrets-file",
                str(secrets),
                "--governance",
                "off",
            )["project"]
        )
        # Mirror resolve_project's canonical-path invariant. This is material
        # on Windows runners where tempfile may return an 8.3 short alias while
        # immutable CAS receipts persist the resolved long path.
        layout = ProjectLayout(
            root=(temp / "runtime").resolve(),
            project_dir=project_dir.resolve(),
        )
        cli(temp, "migrate-state", "--project", str(project_dir), "--apply")
        task_id = cli(
            temp,
            "new-task",
            "--project",
            str(project_dir),
            "--title",
            "three-tier required chain",
            "--purpose",
            "prove low medium high collaboration",
            "--task-type",
            "analysis",
            "--difficulty",
            "normal",
            "--estimated-input-tokens",
            "1000000",
            "--estimated-output-tokens",
            "10000",
        )["task_id"]
        fresh_preview = load_task(layout, task_id)["route_preview"]
        assert fresh_preview["optimization_mode"] == "conditional-evidence-bootstrap"
        assert fresh_preview["planned_provider_ids"] == [
            "longcat",
            "deepseek",
            "codex",
        ]
        first = required_cli(
            temp,
            "dispatch",
            "--project",
            str(project_dir),
            "--task",
            task_id,
        )
        planned = load_task(layout, task_id)["route_budget_envelope"]["planned_steps"]
        assert [step["tier"] for step in planned] == ["low", "medium", "high"]
        for step in planned:
            assert step["profile_binding"]["sha256"] == step["execution_identity"][
                "profile_sha256"
            ], step

        actor_id = first["actor_id"]
        for index, expected_tier in enumerate(("low", "medium", "high")):
            actor = load_actor(layout, actor_id)
            assert actor["tier"] == expected_tier
            assert (actor.get("isolation") or {}).get("mode") == "required"
            assert isinstance(actor.get("collaboration_contract"), dict)
            assert isinstance(actor.get("prompt_binding"), dict)
            current_attempt = load_task(layout, task_id)["attempts"][-1]
            assert (current_attempt.get("isolation") or {}).get("mode") == "required"
            assert current_attempt.get("collaboration_contract_sha256") == (
                actor["collaboration_contract"]["contract_sha256"]
            )
            with patch(
                "costmarshal_v2.actor_runner.OciCliBackend",
                side_effect=ThreeTierOciBackend,
            ):
                returncode = run_actor_fixture(
                    layout,
                    actor_id,
                    attempt_id=actor["attempt_id"],
                    launch_token=actor["launch_token"],
                )
            assert returncode == 0
            before_collect_task = load_task(layout, task_id)
            before_collect_attempt = before_collect_task["attempts"][-1]
            before_collect_actor = load_actor(layout, actor_id)
            completion_sha256 = hashlib.sha256(
                (layout.project_dir / "tasks" / task_id / "completion-report.md").read_bytes()
            ).hexdigest()
            assert (
                before_collect_attempt.get("report_sha256"),
                (before_collect_actor.get("runtime") or {}).get("report_sha256"),
                completion_sha256,
            ).count(completion_sha256) == 3, (
                before_collect_attempt.get("report_sha256"),
                (before_collect_actor.get("runtime") or {}).get("report_sha256"),
                completion_sha256,
            )
            scheduler_cycles = []
            for _ in range(6):
                scheduler_cycles.append(
                    cli(temp, "run-scheduler", "--project", str(project_dir), "--once")
                )
                task = load_task(layout, task_id)
                attempt = task["attempts"][-1]
                if attempt["status"] != "running":
                    break
            scheduler_failures = [
                json.loads(line)
                for line in layout.events_jsonl.read_text(encoding="utf-8").splitlines()
                if line.strip()
                and json.loads(line).get("event_type") == "scheduler_command_failed"
            ]
            if expected_tier != "high":
                assert attempt["status"] == "waiting_leader", (
                    attempt["status"],
                    scheduler_cycles,
                    scheduler_failures[-3:],
                )
                assert attempt.get("worker_outcome") == "escalate"
                if index == 0:
                    tampered_task = load_task(layout, task_id)
                    intact_envelope = deepcopy(
                        tampered_task["route_budget_envelope"]
                    )
                    tampered_task["route_budget_envelope"]["planned_steps"] = (
                        tampered_task["route_budget_envelope"]["planned_steps"][:1]
                    )
                    save_task(layout, tampered_task)
                    missing_handoff = cli_failure(
                        temp,
                        "record-result",
                        "--command-id",
                        "CMD-three-tier-missing-handoff",
                        "--project",
                        str(project_dir),
                        "--task",
                        task_id,
                        "--attempt",
                        attempt["attempt_id"],
                        "--actor",
                        actor_id,
                        "--status",
                        "escalate",
                        "--quality-score",
                        "2",
                    )
                    assert "requires --handoff" in missing_handoff, missing_handoff
                    restored_task = load_task(layout, task_id)
                    restored_task["route_budget_envelope"] = intact_envelope
                    save_task(layout, restored_task)
                    terminal_mismatch = cli_failure(
                        temp,
                        "record-result",
                        "--command-id",
                        "CMD-three-tier-failed-successor",
                        "--project",
                        str(project_dir),
                        "--task",
                        task_id,
                        "--attempt",
                        attempt["attempt_id"],
                        "--actor",
                        actor_id,
                        "--status",
                        "failed",
                        "--quality-score",
                        "1",
                        "--handoff",
                        "A successor exists, so this must be an escalation decision.",
                    )
                    assert "must use --status escalate" in terminal_mismatch
                cli(
                    temp,
                    "record-result",
                    "--command-id",
                    f"CMD-three-tier-result-{index}",
                    "--project",
                    str(project_dir),
                    "--task",
                    task_id,
                    "--attempt",
                    attempt["attempt_id"],
                    "--actor",
                    actor_id,
                    "--status",
                    "escalate",
                    "--quality-score",
                    "2",
                    "--handoff",
                    f"{expected_tier} completed bounded analysis; continue with stronger review.",
                )
                if index == 0:
                    before_replan = load_task(layout, task_id)
                    blocked_replan = required_cli_failure(
                        temp,
                        "escalate",
                        "--project",
                        str(project_dir),
                        "--task",
                        task_id,
                        "--provider",
                        "codex",
                        "--replan",
                        "--reason",
                        "must not replace a sealed semantic envelope",
                    )
                    assert "cannot revise a sealed semantic route" in blocked_replan, blocked_replan
                    assert load_task(layout, task_id) == before_replan
                escalated = required_cli(
                    temp,
                    "escalate",
                    "--project",
                    str(project_dir),
                    "--task",
                    task_id,
                    "--reason",
                    f"leader rejected {expected_tier}",
                )
                actor_id = escalated["actor_id"]
            else:
                assert attempt["status"] == "waiting_leader"
                cli(
                    temp,
                    "record-result",
                    "--command-id",
                    "CMD-three-tier-result-high",
                    "--project",
                    str(project_dir),
                    "--task",
                    task_id,
                    "--attempt",
                    attempt["attempt_id"],
                    "--actor",
                    actor_id,
                    "--status",
                    "done",
                    "--quality-score",
                    "5",
                    "--accepted-by-leader",
                )

        final_task = load_task(layout, task_id)
        assert final_task["status"] == "done"
        assert [attempt["tier"] for attempt in final_task["attempts"]] == [
            "low",
            "medium",
            "high",
        ]
        assert all(
            attempt.get("result_attempt_output_boundary") == "sealed-required"
            for attempt in final_task["attempts"]
        )
        trusted, issues = audit_result_evidence(layout)
        assert issues == [], issues
        chain_results = [row for row in trusted if row.get("task_id") == task_id]
        assert len(chain_results) == 3
        assert [row["tier"] for row in chain_results] == ["low", "medium", "high"]

        # One sample per continuation is intentionally below the bootstrap
        # reliability threshold. A second route therefore seals the same full
        # plan, but explicit leader acceptance at low must stop before any peer
        # or stronger provider is called.
        early_task_id = cli(
            temp,
            "new-task",
            "--project",
            str(project_dir),
            "--title",
            "leader early stop",
            "--purpose",
            "prove accepted bootstrap steps do not spend successors",
            "--task-type",
            "analysis",
            "--difficulty",
            "normal",
            "--estimated-input-tokens",
            "1000000",
            "--estimated-output-tokens",
            "10000",
        )["task_id"]
        early_preview = load_task(layout, early_task_id)["route_preview"]
        assert early_preview["optimization_mode"] == "conditional-evidence-bootstrap"
        assert early_preview["planned_provider_ids"] == [
            "longcat",
            "deepseek",
            "codex",
        ]
        early_dispatch = required_cli(
            temp,
            "dispatch",
            "--project",
            str(project_dir),
            "--task",
            early_task_id,
        )
        early_actor_id = early_dispatch["actor_id"]
        early_actor = load_actor(layout, early_actor_id)
        with patch(
            "costmarshal_v2.actor_runner.OciCliBackend",
            side_effect=ThreeTierOciBackend,
        ):
            returncode = run_actor_fixture(
                layout,
                early_actor_id,
                attempt_id=early_actor["attempt_id"],
                launch_token=early_actor["launch_token"],
            )
        assert returncode == 0
        for _ in range(6):
            cli(temp, "run-scheduler", "--project", str(project_dir), "--once")
            early_task = load_task(layout, early_task_id)
            early_attempt = early_task["attempts"][-1]
            if early_attempt["status"] != "running":
                break
        assert early_attempt["status"] == "waiting_leader", early_attempt
        cli(
            temp,
            "record-result",
            "--command-id",
            "CMD-bootstrap-early-accept",
            "--project",
            str(project_dir),
            "--task",
            early_task_id,
            "--attempt",
            early_attempt["attempt_id"],
            "--actor",
            early_actor_id,
            "--status",
            "done",
            "--quality-score",
            "5",
            "--accepted-by-leader",
        )
        early_task = load_task(layout, early_task_id)
        assert early_task["status"] == "done"
        assert [attempt["tier"] for attempt in early_task["attempts"]] == ["low"]
        assert ThreeTierOciBackend.provider_calls == [
            "LONGCAT_API_KEY",
            "DEEPSEEK_API_KEY",
            "CODEX_API_KEY",
            "LONGCAT_API_KEY",
        ]
        assert cli(temp, "validate", "--project", str(project_dir))["status"] == "ok"
        print("three tier required integration ok")
        return 0
    finally:
        if previous_codex_home is None:
            os.environ.pop("CODEX_HOME", None)
        else:
            os.environ["CODEX_HOME"] = previous_codex_home
        shutil.rmtree(temp, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
