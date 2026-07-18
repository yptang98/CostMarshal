#!/usr/bin/env python3
"""Durable required-provider completion is finalize-only and fail-closed."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

import costmarshal_v2.actor_runner as actor_runner  # noqa: E402
from costmarshal_v2.paths import ProjectLayout  # noqa: E402
from costmarshal_v2.routing import route_plan_fingerprint  # noqa: E402
from costmarshal_v2.scheduler import bind_actor_prompt, prepare_collaboration_contract  # noqa: E402
from costmarshal_v2.state import (  # noqa: E402
    load_actor,
    load_project,
    load_task,
    save_actor,
    save_project,
    save_task,
)
from costmarshal_v2.worker_isolation import cleanup_temporary_credential  # noqa: E402
from oci_actor_runner_test import IMAGE, cli  # noqa: E402


class CrashAfterCleanup(RuntimeError):
    pass


def simulated_windows_job_runtime(
    *,
    expected_child_pid: object = None,
    expected_child_start_marker: object = None,
) -> dict[str, object]:
    """Model the already-verified supervisor receipt for direct runner tests."""

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
        "target": "job:costmarshal-provider-completion-test",
        "windows_job_name": "costmarshal-provider-completion-test",
        "windows_job_identity": "a" * 64,
        "windows_job_child_pid": authority_pid,
        "windows_job_child_start_marker": marker,
    }


if os.name == "nt":
    # This contract invokes run_actor directly. Production Windows launches are
    # covered separately and must inherit a real verified Job Object receipt.
    actor_runner._inherited_windows_job_runtime = simulated_windows_job_runtime


class CompletionAdapter:
    provider_calls = 0

    def __init__(self, backend) -> None:
        self.backend = backend

    def start(self, spec, command, *, stdin_prompt: str):
        self.__class__.provider_calls += 1
        calls_path = os.environ.get("COSTMARSHAL_PROVIDER_CALLS_FILE")
        if calls_path:
            with Path(calls_path).open("a", encoding="utf-8") as stream:
                stream.write("provider\n")
        daemon_path = os.environ.get("COSTMARSHAL_COMPLETION_DAEMON")
        if daemon_path:
            Path(daemon_path).write_text("present\n", encoding="utf-8")
        return SimpleNamespace(
            spec=spec,
            container_name=actor_runner._expected_oci_container_name(spec),
            container_id="b" * 64,
            command=tuple(command),
            network_id="c" * 64,
            recovered=False,
            attestation=SimpleNamespace(
                to_dict=lambda: {
                    "schema": "costmarshal-worker-isolation-attestation-v1",
                    "backend": "docker",
                    "image": IMAGE,
                    "strong_isolation": True,
                }
            ),
        )

    def wait(self, handle):
        (handle.spec.output_exchange / "final.md").write_text(
            "# Completion Report\n\nStatus: done\n\n## Result\nsealed once\n",
            encoding="utf-8",
        )
        return SimpleNamespace(
            exit_code=0,
            stdout_events=(
                {"usage": {"input_tokens": 17, "output_tokens": 6}},
                {"message": 'escaped ab"cd selected-secret'},
            ),
            stdout_bytes=128,
            stderr_bytes=0,
            stderr_truncated=False,
        )

    def attach(self, spec, *, container_name: str, container_id: str | None, command):
        daemon_path = os.environ.get("COSTMARSHAL_COMPLETION_DAEMON")
        if not daemon_path or not Path(daemon_path).is_file():
            raise AssertionError("durable completion container is missing")
        return SimpleNamespace(
            spec=spec,
            container_name=container_name,
            container_id=str(container_id),
            command=tuple(command),
            network_id="c" * 64,
            recovered=True,
            attestation=SimpleNamespace(to_dict=lambda: {}),
        )

    def inspect(self, handle):
        return SimpleNamespace(status="exited", exit_code=0)

    def cleanup(self, handle):
        credential = cleanup_temporary_credential(handle.spec)
        daemon_path = os.environ.get("COSTMARSHAL_COMPLETION_DAEMON")
        if daemon_path:
            Path(daemon_path).unlink(missing_ok=True)
        return SimpleNamespace(
            container_removed=True,
            credential=credential,
            identity_drift=(),
        )


class NoProviderAdapter:
    provider_methods = 0
    init_count = 0

    def __init__(self, backend) -> None:
        self.__class__.init_count += 1
        self.backend = backend

    def __getattr__(self, name):
        if name in {"start", "recover_or_start", "wait", "recover_wait", "attach", "inspect", "cleanup"}:
            def forbidden(*args, **kwargs):
                self.__class__.provider_methods += 1
                raise AssertionError(f"finalize-only recovery called {name}")

            return forbidden
        raise AttributeError(name)


def prepare_fixture(
    temp: Path,
    *,
    sqlite_authority: bool = True,
) -> tuple[ProjectLayout, Path, dict, Path]:
    workspace = temp / "workspace"
    workspace.mkdir(parents=True)
    (workspace / "README.md").write_text("bounded workspace\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(workspace), "init", "--quiet"], check=True)
    subprocess.run(["git", "-C", str(workspace), "config", "user.name", "CostMarshal Test"], check=True)
    subprocess.run(["git", "-C", str(workspace), "config", "user.email", "costmarshal@example.invalid"], check=True)
    subprocess.run(["git", "-C", str(workspace), "add", "README.md"], check=True)
    subprocess.run(["git", "-C", str(workspace), "commit", "--quiet", "-m", "base"], check=True)
    codex_home = temp / "codex-home"
    codex_home.mkdir()
    (codex_home / "longcat.config.toml").write_text(
        "model_provider = 'longcat'\nmodel = 'LongCat-2.0'\nweb_search = 'disabled'\n"
        "[model_providers.longcat]\nname = 'LongCat'\nbase_url = 'https://example.invalid/v1'\n"
        "env_key = 'LONGCAT_API_KEY'\n",
        encoding="utf-8",
    )
    secrets_file = temp / "providers.env"
    secrets_file.write_text(
        'LONGCAT_API_KEY=selected-secret\nOTHER_SECRET=ab"cd\n',
        encoding="utf-8",
    )
    project_dir = Path(
        cli(
            temp,
            "init",
            "--name",
            "provider-completion",
            "--objective",
            "finalize without another provider call",
            "--workspace",
            str(workspace),
            "--backend",
            "local",
            "--governance",
            "off",
            "--allow-unsafe-native-workers",
            "--secrets-file",
            str(secrets_file),
        )["project"]
    )
    cli(
        temp,
        "new-task",
        "--project",
        str(project_dir),
        "--title",
        "completion",
        "--purpose",
        "crash after cleanup",
        "--estimated-input-tokens",
        "50000",
        "--estimated-output-tokens",
        "10000",
    )
    with patch.dict(os.environ, {"CODEX_HOME": str(codex_home)}, clear=False):
        dispatched = cli(
            temp,
            "dispatch",
            "--project",
            str(project_dir),
            "--task",
            "V2-0001",
            "--unsafe-native",
        )
    layout = ProjectLayout(root=temp / "runtime", project_dir=project_dir)
    actor = load_actor(layout, dispatched["actor_id"])
    project = load_project(layout)
    task = load_task(layout, "V2-0001")
    attempt = task["attempts"][-1]
    step = json.loads(json.dumps(task["route_decision"]["planned_steps"][0]))
    step.update(
        execution_identity=attempt["execution_identity"],
        model=attempt["model"],
        profile=attempt["profile"],
        profile_binding=attempt["profile_binding"],
    )
    fingerprint = route_plan_fingerprint(
        [step],
        input_tokens=task["estimated_input_tokens"],
        cached_input_tokens=task["estimated_cached_input_tokens"],
        output_tokens=task["estimated_output_tokens"],
    )
    task["route_budget_envelope"] = {
        "envelope_id": "ENV-provider-completion",
        "plan_fingerprint": fingerprint,
        "planned_steps": [step],
        "status": "active",
    }
    attempt["route_envelope_id"] = "ENV-provider-completion"
    attempt["route_plan_fingerprint"] = fingerprint
    attempt["route_plan_step_index"] = 0
    attempt["route_plan_step"] = step
    save_task(layout, task)
    actor["isolation"] = {
        "mode": "required",
        "project_opt_in": False,
        "dispatch_opt_in": False,
        "attestation": {
            "schema": "costmarshal-worker-isolation-attestation-v1",
            "backend": "docker",
            "image": IMAGE,
            "image_digest": "sha256:" + ("a" * 64),
            "strong_isolation": True,
        },
        "execution": {
            "engine": "docker",
            "image": IMAGE,
            "network_mode": "provider-proxy",
            "network_name": "costmarshal-provider-proxy",
            "workspace_mode": "ro",
            "limits": {
                "memory_mb": 512,
                "cpus": 1.0,
                "pids": 64,
                "timeout_seconds": 10.0,
                "tmpfs_mb": 32,
                "home_tmpfs_mb": 32,
            },
        },
    }
    task = load_task(layout, "V2-0001")
    collaboration = prepare_collaboration_contract(project, task)
    task["collaboration_contract"] = collaboration
    save_task(layout, task)
    actor["collaboration_contract"] = collaboration
    prompt_binding = bind_actor_prompt(layout, actor)
    task = load_task(layout, "V2-0001")
    task["attempts"][-1]["collaboration_contract_sha256"] = collaboration["contract_sha256"]
    task["attempts"][-1]["prompt_binding"] = prompt_binding
    save_task(layout, task)
    save_actor(layout, actor)
    project["secrets_file"] = str(secrets_file)
    save_project(layout, project)
    if sqlite_authority:
        cli(temp, "migrate-state", "--project", str(project_dir), "--apply")
    return layout, codex_home, actor, secrets_file


def crash_after_cleanup(layout: ProjectLayout, codex_home: Path, actor: dict) -> None:
    def fault(stage: str) -> None:
        if stage == "after_provider_cleanup_before_seal":
            raise CrashAfterCleanup(stage)

    with patch.dict(os.environ, {"CODEX_HOME": str(codex_home)}, clear=False), patch(
        "costmarshal_v2.actor_runner.OciWorkerExecutionAdapter",
        CompletionAdapter,
    ), patch("costmarshal_v2.actor_runner._actor_fault", side_effect=fault):
        try:
            actor_runner.run_actor(
                layout,
                actor["id"],
                attempt_id=actor["attempt_id"],
                launch_token=actor["launch_token"],
            )
        except CrashAfterCleanup:
            pass
        else:
            raise AssertionError("cleanup-before-seal crash seam was not reached")


def main() -> int:
    root = Path(tempfile.mkdtemp(prefix="costmarshal-provider-completion-"))
    try:
        first = root / "success"
        first.mkdir()
        layout, codex_home, actor, _ = prepare_fixture(first, sqlite_authority=True)
        CompletionAdapter.provider_calls = 0
        NoProviderAdapter.provider_methods = 0
        NoProviderAdapter.init_count = 0
        crash_after_cleanup(layout, codex_home, actor)
        crashed_actor = load_actor(layout, actor["id"])
        assert crashed_actor["runtime"]["provider_execution_state"] == "finished_pending_finalize"
        assert crashed_actor["runtime"]["oci_lifecycle_state"] == "cleaned"
        assert crashed_actor["runtime"]["credential_cleanup"]["status"] == "deleted"
        completion_root = layout.root / "provider-completions"
        durable_bytes = b"\n".join(path.read_bytes() for path in completion_root.rglob("*") if path.is_file())
        assert b"selected-secret" not in durable_bytes
        completion = crashed_actor["runtime"]["provider_completion"]
        durable_events = json.loads(Path(completion["events"]["path"]).read_text(encoding="utf-8"))["events"]
        event_strings = [
            value
            for event in durable_events
            for value in event.values()
            if isinstance(value, str)
        ]
        assert all(
            secret not in value
            for value in event_strings
            for secret in ("selected-secret", 'ab"cd')
        )
        with patch.dict(os.environ, {"CODEX_HOME": str(codex_home)}, clear=False), patch(
            "costmarshal_v2.actor_runner.OciWorkerExecutionAdapter",
            NoProviderAdapter,
        ):
            assert actor_runner.run_actor(
                layout,
                actor["id"],
                attempt_id=actor["attempt_id"],
                launch_token=actor["launch_token"],
            ) == 0
        assert CompletionAdapter.provider_calls == 1
        assert NoProviderAdapter.provider_methods == 0
        final_task = load_task(layout, "V2-0001")
        assert final_task["attempts"][-1]["collaboration_phase"] == "output_sealed"

        precleanup = root / "precleanup"
        precleanup.mkdir()
        layout3, codex_home3, actor3, _ = prepare_fixture(precleanup)
        daemon = precleanup / "container.flag"
        calls = precleanup / "provider-calls.txt"
        child = """
import sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
sys.path.insert(0, str(Path(sys.argv[1]) / 'tests'))
import costmarshal_v2.actor_runner as runner
from costmarshal_v2.paths import ProjectLayout
from provider_completion_recovery_test import CompletionAdapter, simulated_windows_job_runtime
if runner.os.name == "nt":
    runner._inherited_windows_job_runtime = simulated_windows_job_runtime
runner.OciWorkerExecutionAdapter = CompletionAdapter
raise SystemExit(runner.run_actor(
    ProjectLayout(root=Path(sys.argv[2]), project_dir=Path(sys.argv[3])),
    sys.argv[4], attempt_id=sys.argv[5], launch_token=sys.argv[6],
))
"""
        environment = os.environ.copy()
        environment.update(
            {
                "CODEX_HOME": str(codex_home3),
                "COSTMARSHAL_ACTOR_FAULT": "after_provider_completion_before_cleanup",
                "COSTMARSHAL_COMPLETION_DAEMON": str(daemon),
                "COSTMARSHAL_PROVIDER_CALLS_FILE": str(calls),
            }
        )
        crashed = subprocess.run(
            [
                sys.executable,
                "-c",
                child,
                str(ROOT),
                str(layout3.root),
                str(layout3.project_dir),
                actor3["id"],
                actor3["attempt_id"],
                actor3["launch_token"],
            ],
            env=environment,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        assert crashed.returncode == 87, crashed.stderr
        assert daemon.is_file()
        assert calls.read_text(encoding="utf-8").splitlines() == ["provider"]
        pending = load_actor(layout3, actor3["id"])
        assert pending["runtime"]["provider_execution_state"] == "finished_pending_finalize"
        assert pending["runtime"]["oci_lifecycle_state"] == "finished"
        recovery_environment = dict(environment)
        recovery_environment.pop("COSTMARSHAL_ACTOR_FAULT", None)
        with patch.dict(os.environ, recovery_environment, clear=False), patch(
            "costmarshal_v2.actor_runner.OciWorkerExecutionAdapter",
            CompletionAdapter,
        ):
            assert actor_runner.run_actor(
                layout3,
                actor3["id"],
                attempt_id=actor3["attempt_id"],
                launch_token=actor3["launch_token"],
            ) == 0
        assert not daemon.exists()
        assert calls.read_text(encoding="utf-8").splitlines() == ["provider"]
        assert not Path(
            pending["runtime"]["credential_cleanup"]["path"]
        ).exists()

        second = root / "corrupt"
        second.mkdir()
        layout2, codex_home2, actor2, _ = prepare_fixture(second)
        crash_after_cleanup(layout2, codex_home2, actor2)
        receipt = load_actor(layout2, actor2["id"])["runtime"]["provider_completion"]
        report_path = Path(receipt["report"]["path"])
        report_path.write_bytes(report_path.read_bytes() + b"tamper")
        NoProviderAdapter.provider_methods = 0
        NoProviderAdapter.init_count = 0
        with patch.dict(os.environ, {"CODEX_HOME": str(codex_home2)}, clear=False), patch(
            "costmarshal_v2.actor_runner.OciWorkerExecutionAdapter",
            NoProviderAdapter,
        ):
            try:
                actor_runner.run_actor(
                    layout2,
                    actor2["id"],
                    attempt_id=actor2["attempt_id"],
                    launch_token=actor2["launch_token"],
                )
            except SystemExit as exc:
                assert "CAS" in str(exc) or "mismatch" in str(exc)
            else:
                raise AssertionError("corrupt provider completion CAS was accepted")
        assert NoProviderAdapter.provider_methods == 0
        assert NoProviderAdapter.init_count == 0

        corrupt_receipt = root / "corrupt-receipt"
        corrupt_receipt.mkdir()
        layout4, codex_home4, actor4, _ = prepare_fixture(corrupt_receipt)
        crash_after_cleanup(layout4, codex_home4, actor4)
        receipt4 = load_actor(layout4, actor4["id"])["runtime"]["provider_completion"]
        receipt_path = Path(receipt4["path"])
        receipt_path.write_bytes(receipt_path.read_bytes() + b"tamper")
        NoProviderAdapter.provider_methods = 0
        NoProviderAdapter.init_count = 0
        with patch.dict(os.environ, {"CODEX_HOME": str(codex_home4)}, clear=False), patch(
            "costmarshal_v2.actor_runner.OciWorkerExecutionAdapter",
            NoProviderAdapter,
        ):
            try:
                actor_runner.run_actor(
                    layout4,
                    actor4["id"],
                    attempt_id=actor4["attempt_id"],
                    launch_token=actor4["launch_token"],
                )
            except SystemExit as exc:
                assert "receipt" in str(exc) or "CAS" in str(exc)
            else:
                raise AssertionError("corrupt provider completion receipt was accepted")
        assert NoProviderAdapter.provider_methods == 0
        assert NoProviderAdapter.init_count == 0

        escaped = root / "escaped"
        escaped.mkdir()
        authority = root / "authority"
        authority.mkdir()
        linked = authority / "provider-completions"
        try:
            linked.symlink_to(escaped, target_is_directory=True)
        except OSError:
            pass
        else:
            try:
                actor_runner._ensure_completion_directory(authority, linked / "project")
            except SystemExit as exc:
                assert "link or reparse" in str(exc)
            else:
                raise AssertionError("provider completion symlink escape was accepted")

        simulated_reparse = authority / "simulated-reparse"
        simulated_reparse.mkdir()
        original_lstat = Path.lstat

        def reparse_lstat(path: Path):
            info = original_lstat(path)
            if path == simulated_reparse:
                return SimpleNamespace(
                    st_mode=info.st_mode,
                    st_file_attributes=0x400,
                )
            return info

        with patch.object(Path, "lstat", reparse_lstat):
            try:
                actor_runner._ensure_completion_directory(
                    authority,
                    simulated_reparse / "project",
                )
            except SystemExit as exc:
                assert "link or reparse" in str(exc)
            else:
                raise AssertionError("provider completion reparse point was accepted")

        redacted = actor_runner._redact_provider_events(
            [{"a": 'ab"cd', "b": "ab\\cd", "c": "abc\tdef", "d": "prefix x suffix"}],
            ('ab"cd', "ab\\cd", "abc\tdef", "x"),
        )
        assert all(
            secret not in value
            for value in redacted[0].values()
            for secret in ('ab"cd', "ab\\cd", "abc\tdef", "x")
        )
        print("provider completion recovery ok")
        print(
            "COSTMARSHAL_RUNTIME_EVIDENCE="
            + json.dumps(
                {
                    "schema_version": 1,
                    "test": "tests/provider_completion_recovery_test.py",
                    "crash_points": [
                        "after_provider_completion_before_cleanup",
                        "after_provider_cleanup_before_seal",
                    ],
                    "recovery_scenarios": [
                        "provider_completion_precleanup_hard_exit_finalize_only",
                        "provider_completion_sqlite_cleanup_replay",
                        "provider_completion_cas_tamper_fail_closed",
                        "provider_completion_secret_redaction",
                        "provider_completion_reparse_rejected",
                    ],
                    "expected_provider_calls": 4,
                    "provider_calls": CompletionAdapter.provider_calls
                    + len(calls.read_text(encoding="utf-8").splitlines()),
                    "orphan_effects": 0,
                },
                sort_keys=True,
            )
        )
        return 0
    finally:
        shutil.rmtree(root, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
