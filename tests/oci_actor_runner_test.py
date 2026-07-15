#!/usr/bin/env python3
"""The actor runner supervises a required OCI adapter and imports only its validated exchange."""

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

from costmarshal_v2.actor_runner import (  # noqa: E402
    _expected_oci_container_name,
    _required_worker_bundle,
    run_actor,
)
from costmarshal_v2.paths import ProjectLayout  # noqa: E402
from costmarshal_v2.state import load_actor, load_project, save_actor, save_project  # noqa: E402
from costmarshal_v2.worker_isolation import (  # noqa: E402
    WorkerExecutionError,
    cleanup_temporary_credential,
    validate_execution_spec,
)


CLI = ROOT / "scripts" / "costmarshal.py"
IMAGE = "ghcr.io/example/costmarshal-worker@sha256:" + ("a" * 64)


def cli(temp: Path, *args: str) -> dict:
    environment = os.environ.copy()
    environment["COSTMARSHAL_V2_HOME"] = str(temp / "runtime")
    completed = subprocess.run(
        [sys.executable, str(CLI), "--root", str(temp / "runtime"), *args],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=environment,
        check=False,
    )
    if completed.returncode:
        raise AssertionError(f"command failed: {args}\n{completed.stdout}\n{completed.stderr}")
    return json.loads(completed.stdout)


def queued_command_args(project_dir: Path, task_id: str, command: str) -> list[dict]:
    matches: list[dict] = []
    seen_message_ids: set[str] = set()
    for mailbox_path in project_dir.rglob("*.jsonl"):
        try:
            lines = mailbox_path.read_text(encoding="utf-8").splitlines()
        except OSError:
            continue
        for line in lines:
            try:
                message = json.loads(line)
            except json.JSONDecodeError:
                continue
            metadata = message.get("metadata") if isinstance(message, dict) else None
            if not isinstance(metadata, dict) or metadata.get("command") != command:
                continue
            args = metadata.get("args") or {}
            if isinstance(args, dict) and args.get("task") == task_id:
                message_id = str(message.get("id") or "")
                if message_id and message_id in seen_message_ids:
                    continue
                if message_id:
                    seen_message_ids.add(message_id)
                matches.append(args)
    return matches


class FakeOciAdapter:
    started_spec = None
    started_command = None
    prompt = None
    credential_deleted = False
    attached = False
    start_calls = 0
    attach_calls = 0

    def __init__(self, backend) -> None:
        self.backend = backend

    def start(self, spec, command, *, stdin_prompt: str):
        self.__class__.start_calls += 1
        validate_execution_spec(spec)
        assert spec.image == IMAGE
        assert spec.workspace_mode == "ro"
        assert spec.credential_path is not None
        assert spec.credential_path.read_text(encoding="utf-8") == "selected-secret"
        assert spec.provider_env_key == "LONGCAT_API_KEY"
        assert str(spec.output_exchange).startswith(str(spec.output_exchange.parents[2]))
        assert str(spec.output_exchange).startswith(str(spec.profile_path.parent))
        assert str(spec.forbidden_mount_roots[0]) not in str(spec.output_exchange)
        assert command == ["costmarshal-worker", "--jsonl", "--model", "LongCat-2.0"]
        assert "V2-" in stdin_prompt
        self.__class__.started_spec = spec
        self.__class__.started_command = list(command)
        self.__class__.prompt = stdin_prompt
        return SimpleNamespace(
            spec=spec,
            container_name=_expected_oci_container_name(spec),
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
            "# Completion Report\n\nStatus: done\n\n## Result\ncontainer-safe\n",
            encoding="utf-8",
        )
        return SimpleNamespace(
            exit_code=0,
            stdout_events=({"usage": {"input_tokens": 9, "output_tokens": 4}},),
        )

    def attach(self, spec, *, container_name: str, container_id: str | None, command):
        self.__class__.attach_calls += 1
        validate_execution_spec(spec)
        assert container_name == _expected_oci_container_name(spec)
        assert container_id is None
        assert tuple(command) == ("costmarshal-worker", "--jsonl", "--model", "LongCat-2.0")
        self.__class__.attached = True
        return SimpleNamespace(
            spec=spec,
            container_name=container_name,
            container_id="d" * 64,
            command=tuple(command),
            network_id="c" * 64,
            recovered=True,
            attestation=SimpleNamespace(
                to_dict=lambda: {
                    "schema": "costmarshal-worker-isolation-attestation-v1",
                    "backend": "docker",
                    "image": IMAGE,
                    "strong_isolation": True,
                }
            ),
        )

    def recover_or_start(
        self,
        spec,
        command,
        *,
        container_name: str,
        container_id: str | None = None,
        stdin_prompt: str,
    ):
        # The first prepared-state fixture represents a crash before the
        # external create.  A real adapter proves absence by a complete list;
        # this focused runner fake then exercises the fresh-start branch.
        return self.start(spec, list(command), stdin_prompt=stdin_prompt)

    def inspect(self, handle):
        (handle.spec.output_exchange / "final.md").write_text(
            "# Completion Report\n\nStatus: done\n\n## Result\nrecovered-container-safe\n",
            encoding="utf-8",
        )
        return SimpleNamespace(status="exited", exit_code=0)

    def recover_wait(self, handle):
        self.inspect(handle)
        return SimpleNamespace(
            exit_code=0,
            stdout_events=({"usage": {"input_tokens": 11, "output_tokens": 5}},),
        )

    def cleanup(self, handle):
        receipt = cleanup_temporary_credential(handle.spec)
        self.__class__.credential_deleted = receipt.deleted
        return SimpleNamespace(container_removed=True, credential=receipt, identity_drift=())


class HardExitOciAdapter:
    """File-backed fake daemon that survives the runner's real os._exit()."""

    last_cleanup_state: dict | None = None

    def __init__(self, backend) -> None:
        self.backend = backend
        self.root = Path(os.environ["COSTMARSHAL_HARD_EXIT_DAEMON"])
        self.root.mkdir(parents=True, exist_ok=True)
        self.state_path = self.root / "container.json"
        self.calls_path = self.root / "provider-calls.txt"
        self.fresh_starts_path = self.root / "fresh-starts.txt"

    @staticmethod
    def _identity(spec, command):
        return SimpleNamespace(
            spec=spec,
            container_name=_expected_oci_container_name(spec),
            container_id="e" * 64,
            command=tuple(command),
            network_id="c" * 64,
            recovered=True,
            attestation=SimpleNamespace(
                to_dict=lambda: {
                    "schema": "costmarshal-worker-isolation-attestation-v1",
                    "backend": "docker",
                    "image": IMAGE,
                    "strong_isolation": True,
                }
            ),
        )

    def _read_state(self) -> dict:
        for _ in range(100):
            try:
                return json.loads(self.state_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                import time

                time.sleep(0.01)
        raise WorkerExecutionError("fake_daemon_unavailable", "hard-exit fake daemon state is unavailable")

    def start(self, spec, command, *, stdin_prompt: str):
        validate_execution_spec(spec)
        if os.environ.get("COSTMARSHAL_HARD_EXIT_ON_START") != "1":
            self.fresh_starts_path.write_text("1\n", encoding="utf-8")
            raise WorkerExecutionError("duplicate_fresh_start", "recovery attempted a fresh provider start")
        handle = self._identity(spec, command)
        state = {
            "container_name": handle.container_name,
            "container_id": handle.container_id,
            "command": list(handle.command),
            "status": "running",
            "exit_code": None,
        }
        self.state_path.write_text(json.dumps(state), encoding="utf-8")
        provider = r'''
import json
import sys
import time
from pathlib import Path
state_path = Path(sys.argv[1])
calls_path = Path(sys.argv[2])
output = Path(sys.argv[3])
prompt = sys.stdin.buffer.read()
import hashlib
count = int(calls_path.read_text(encoding="utf-8").strip() or "0") if calls_path.exists() else 0
calls_path.write_text(str(count + 1) + "\n", encoding="utf-8")
time.sleep(0.15)
(output / "final.md").write_text(
    "# Completion Report\n\nStatus: done\n\n## Result\nhard-exit-recovered\n",
    encoding="utf-8",
)
payload = json.loads(state_path.read_text(encoding="utf-8"))
payload.update({
    "status": "exited",
    "exit_code": 0,
    "prompt_sha256": hashlib.sha256(prompt).hexdigest(),
    "usage": {"input_tokens": 23, "output_tokens": 8},
})
temporary = state_path.with_suffix(".tmp")
temporary.write_text(json.dumps(payload), encoding="utf-8")
temporary.replace(state_path)
'''
        with tempfile.TemporaryFile(mode="w+b") as prompt_source:
            prompt_source.write(stdin_prompt.encode("utf-8"))
            prompt_source.flush()
            prompt_source.seek(0)
            subprocess.Popen(
                [
                    sys.executable,
                    "-c",
                    provider,
                    str(self.state_path),
                    str(self.calls_path),
                    str(spec.output_exchange),
                ],
                stdin=prompt_source,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                close_fds=True,
            )
        for _ in range(500):
            if self.calls_path.is_file():
                break
            import time

            time.sleep(0.01)
        else:
            raise AssertionError("external provider child did not start")
        # This is the regression window: the external create/provider call is
        # observable, but adapter.start has not returned the immutable ID.
        os._exit(88)

    def attach(self, spec, *, container_name: str, container_id: str | None, command):
        state = self._read_state()
        handle = self._identity(spec, command)
        assert container_name == handle.container_name
        assert container_id in {None, handle.container_id}
        assert state["container_name"] == handle.container_name
        assert state["container_id"] == handle.container_id
        assert state["command"] == list(handle.command)
        return handle

    def recover_or_start(
        self,
        spec,
        command,
        *,
        container_name: str,
        container_id: str | None = None,
        stdin_prompt: str,
    ):
        if self.state_path.is_file():
            return self.attach(
                spec,
                container_name=container_name,
                container_id=container_id,
                command=command,
            )
        return self.start(spec, command, stdin_prompt=stdin_prompt)

    def inspect(self, handle):
        state = self._read_state()
        return SimpleNamespace(status=state["status"], exit_code=state["exit_code"])

    def recover_wait(self, handle):
        import time

        for _ in range(200):
            state = self._read_state()
            if state["status"] == "exited":
                return SimpleNamespace(
                    exit_code=state["exit_code"],
                    stdout_events=({"usage": state["usage"]},),
                )
            time.sleep(0.01)
        raise WorkerExecutionError("worker_recovery_timeout", "fake container did not exit")

    def cleanup(self, handle):
        state = self._read_state()
        if state["status"] != "exited":
            raise WorkerExecutionError(
                "container_cleanup_unconfirmed",
                "fake container is still running",
                details={"container_cleanup_unconfirmed": True},
            )
        self.__class__.last_cleanup_state = dict(state)
        self.state_path.unlink()
        receipt = cleanup_temporary_credential(handle.spec)
        return SimpleNamespace(container_removed=True, credential=receipt, identity_drift=())


class CleanupUnconfirmedAdapter(FakeOciAdapter):
    def cleanup(self, handle):
        raise WorkerExecutionError(
            "container_cleanup_unconfirmed",
            "container removal was not confirmed",
            details={
                "container_cleanup_unconfirmed": True,
                "container_name": handle.container_name,
                "container_id": handle.container_id,
                "credential_deleted": False,
            },
        )


class RecoveryLogsUnavailableAdapter(FakeOciAdapter):
    def recover_wait(self, handle):
        raise WorkerExecutionError(
            "worker_recovery_logs_unavailable",
            "recovered worker logs are unavailable; usage remains unknown",
        )


def main() -> int:
    temp = Path(tempfile.mkdtemp(prefix="costmarshal-v2-oci-actor-runner-"))
    try:
        workspace = temp / "workspace"
        workspace.mkdir()
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
            "LONGCAT_API_KEY=selected-secret\nDEEPSEEK_API_KEY=must-not-mount\n",
            encoding="utf-8",
        )
        project_dir = Path(
            cli(
                temp,
                "init",
                "--name",
                "oci-runner",
                "--objective",
                "required container execution",
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
        cli(temp, "new-task", "--project", str(project_dir), "--title", "container", "--purpose", "run isolated")
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
        save_actor(layout, actor)
        project["secrets_file"] = str(secrets_file)
        save_project(layout, project)

        # Invalid execution metadata must fail before the selected provider
        # credential is ever materialized in the attempt bundle.
        invalid_limits_actor = json.loads(json.dumps(actor))
        invalid_limits_actor["isolation"]["execution"]["limits"]["memory_mb"] = 1
        with patch.dict(os.environ, {"CODEX_HOME": str(codex_home)}, clear=False):
            try:
                _required_worker_bundle(
                    layout,
                    project,
                    invalid_limits_actor,
                    execution_workspace=workspace,
                    workspace_mode="read-only",
                )
            except SystemExit as exc:
                assert "memory_limit_invalid" in str(exc)
            else:
                raise AssertionError("invalid OCI limits were accepted")
        assert not list((layout.root / "worker-bundles").rglob("provider.secret"))

        profile_path = codex_home / "longcat.config.toml"
        valid_profile = profile_path.read_text(encoding="utf-8")
        profile_path.write_text(valid_profile.replace("/v1'", "/v1?tenant=unsafe'"), encoding="utf-8")
        with patch.dict(os.environ, {"CODEX_HOME": str(codex_home)}, clear=False):
            try:
                _required_worker_bundle(
                    layout,
                    project,
                    actor,
                    execution_workspace=workspace,
                    workspace_mode="read-only",
                )
            except SystemExit as exc:
                assert "base_url is invalid" in str(exc)
            else:
                raise AssertionError("provider base_url query was accepted")
        assert not list((layout.root / "worker-bundles").rglob("provider.secret"))

        insecure_actor = json.loads(json.dumps(actor))
        insecure_actor["isolation"]["execution"]["network_mode"] = "none"
        insecure_actor["isolation"]["execution"]["network_name"] = None
        profile_path.write_text(valid_profile.replace("https://", "http://"), encoding="utf-8")
        with patch.dict(os.environ, {"CODEX_HOME": str(codex_home)}, clear=False):
            try:
                _required_worker_bundle(
                    layout,
                    project,
                    insecure_actor,
                    execution_workspace=workspace,
                    workspace_mode="read-only",
                )
            except SystemExit as exc:
                assert "base_url is invalid" in str(exc)
            else:
                raise AssertionError("HTTP provider URL was accepted without provider-proxy isolation")
        assert not list((layout.root / "worker-bundles").rglob("provider.secret"))
        profile_path.write_text(valid_profile, encoding="utf-8")

        # A hard exit after credential creation but before OCI lifecycle
        # preparation must be resumable without weakening orphan checks.
        with patch.dict(os.environ, {"CODEX_HOME": str(codex_home)}, clear=False):
            prepared_spec, _, _ = _required_worker_bundle(
                layout,
                project,
                actor,
                execution_workspace=workspace,
                workspace_mode="read-only",
            )
            assert prepared_spec.credential_path is not None
            assert prepared_spec.credential_path.is_file()
            prepared_actor = load_actor(layout, actor["id"])
            assert prepared_actor["runtime"]["credential_cleanup"]["status"] == "creating"
            resumed_spec, _, _ = _required_worker_bundle(
                layout,
                project,
                prepared_actor,
                execution_workspace=workspace,
                workspace_mode="read-only",
            )
            assert resumed_spec.credential_path == prepared_spec.credential_path
        cleanup_temporary_credential(prepared_spec)

        child = """
import sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
from costmarshal_v2.actor_runner import run_actor
from costmarshal_v2.paths import ProjectLayout
raise SystemExit(run_actor(
    ProjectLayout(root=Path(sys.argv[2]), project_dir=Path(sys.argv[3])),
    sys.argv[4], attempt_id=sys.argv[5], launch_token=sys.argv[6],
))
"""

        def crash_runner(fault: str) -> subprocess.CompletedProcess[str]:
            environment = os.environ.copy()
            environment.update(
                {
                    "CODEX_HOME": str(codex_home),
                    "COSTMARSHAL_ACTOR_FAULT": fault,
                }
            )
            return subprocess.run(
                [
                    sys.executable,
                    "-c",
                    child,
                    str(ROOT),
                    str(layout.root),
                    str(project_dir),
                    actor["id"],
                    actor["attempt_id"],
                    actor["launch_token"],
                ],
                env=environment,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )

        credential_crash = crash_runner("after_credential_before_oci_prepare")
        assert credential_crash.returncode == 87, credential_crash.stderr
        after_credential = load_actor(layout, actor["id"])
        assert after_credential["runtime"]["credential_cleanup"]["status"] == "creating"
        assert Path(after_credential["runtime"]["credential_cleanup"]["path"]).is_file()
        after_credential["status"] = "starting"
        save_actor(layout, after_credential)
        recovery = cli(temp, "recover", "--project", str(project_dir))
        assert any(actor["id"] in issue for issue in recovery["issues"]), recovery
        after_cleanup = load_actor(layout, actor["id"])
        assert after_cleanup["runtime"]["credential_cleanup"]["status"] == "deleted_recovered"
        assert after_cleanup["runtime"]["credential_generation"] == 1
        assert not Path(after_cleanup["runtime"]["credential_cleanup"]["path"]).exists()
        prepare_crash = crash_runner("after_oci_prepare_before_start")
        assert prepare_crash.returncode == 87, prepare_crash.stderr
        after_prepare = load_actor(layout, actor["id"])
        assert after_prepare["runtime"]["oci_lifecycle_state"] == "prepared"
        assert after_prepare["runtime"].get("container_id") is None

        with patch.dict(os.environ, {"CODEX_HOME": str(codex_home)}, clear=False), patch(
            "costmarshal_v2.actor_runner.OciWorkerExecutionAdapter",
            FakeOciAdapter,
        ):
            returncode = run_actor(
                layout,
                actor["id"],
                attempt_id=actor["attempt_id"],
                launch_token=actor["launch_token"],
            )
        assert returncode == 0
        assert FakeOciAdapter.credential_deleted is True
        assert FakeOciAdapter.started_spec is not None
        assert secrets_file.resolve() in FakeOciAdapter.started_spec.forbidden_mount_roots
        assert not FakeOciAdapter.started_spec.credential_path.exists()
        attempt_report = project_dir / "tasks" / "V2-0001" / "attempts" / f"{actor['id']}.md"
        assert "container-safe" in attempt_report.read_text(encoding="utf-8")
        finished = load_actor(layout, actor["id"])
        assert finished["runtime"]["isolation_backend"] == "docker"
        assert finished["runtime"]["provider_execution_state"] == "finished"
        assert finished["runtime"]["container_name"] == _expected_oci_container_name(
            FakeOciAdapter.started_spec
        )
        assert finished["runtime"]["container_id"] == "b" * 64
        assert finished["runtime"]["oci_lifecycle_state"] == "cleaned"
        assert finished["runtime"]["container_removed"] is True
        assert finished["runtime"]["credential_cleanup"]["status"] == "deleted"
        assert finished["runtime"]["container_command"] == FakeOciAdapter.started_command
        status = json.loads((project_dir / "tasks" / "V2-0001" / "status.json").read_text(encoding="utf-8"))
        assert status["state"] == "waiting_leader"

        # A scheduler restart may know only the deterministic name and exact argv.
        # The runner must attach, discover the immutable ID, and import the existing
        # container's output instead of starting the provider a second time.
        cli(temp, "new-task", "--project", str(project_dir), "--title", "recover", "--purpose", "attach existing")
        recovered_dispatch = cli(
            temp,
            "dispatch",
            "--project",
            str(project_dir),
            "--task",
            "V2-0002",
            "--unsafe-native",
        )
        recovered_actor = load_actor(layout, recovered_dispatch["actor_id"])
        recovered_actor["isolation"] = json.loads(json.dumps(actor["isolation"]))
        identity_spec = SimpleNamespace(
            project_id=project["project_id"],
            actor_id=recovered_actor["id"],
            attempt_id=recovered_actor["attempt_id"],
        )
        recovered_actor.setdefault("runtime", {}).update(
            {
                "container_name": _expected_oci_container_name(identity_spec),
                "container_command": ["costmarshal-worker", "--jsonl", "--model", "LongCat-2.0"],
                "oci_lifecycle_state": "started",
            }
        )
        save_actor(layout, recovered_actor)
        FakeOciAdapter.attached = False
        with patch.dict(os.environ, {"CODEX_HOME": str(codex_home)}, clear=False), patch(
            "costmarshal_v2.actor_runner.OciWorkerExecutionAdapter",
            FakeOciAdapter,
        ):
            recovered_returncode = run_actor(
                layout,
                recovered_actor["id"],
                attempt_id=recovered_actor["attempt_id"],
                launch_token=recovered_actor["launch_token"],
            )
        assert recovered_returncode == 0
        assert FakeOciAdapter.attached is True
        recovered_finished = load_actor(layout, recovered_actor["id"])
        assert recovered_finished["runtime"]["container_id"] == "d" * 64
        assert FakeOciAdapter.start_calls == 1
        assert FakeOciAdapter.attach_calls == 1
        recovered_report = (
            project_dir / "tasks" / "V2-0002" / "attempts" / f"{recovered_actor['id']}.md"
        )
        assert "recovered-container-safe" in recovered_report.read_text(encoding="utf-8")

        # Real runner hard-exit after the external create/provider call but
        # before adapter.start returns an immutable ID.  Recovery sees only
        # the durable prepared name/argv and must attach, never fresh-start.
        cli(temp, "new-task", "--project", str(project_dir), "--title", "hard-exit", "--purpose", "recover external create")
        hard_dispatch = cli(
            temp,
            "dispatch",
            "--project",
            str(project_dir),
            "--task",
            "V2-0003",
            "--unsafe-native",
        )
        hard_actor = load_actor(layout, hard_dispatch["actor_id"])
        hard_actor["isolation"] = json.loads(json.dumps(actor["isolation"]))
        save_actor(layout, hard_actor)
        hard_prompt = project_dir / str(hard_actor["prompt_path"])
        import hashlib

        expected_prompt_sha256 = hashlib.sha256(
            hard_prompt.read_text(encoding="utf-8").encode("utf-8")
        ).hexdigest()
        hard_daemon = temp / "hard-exit-daemon"
        hard_child = """
import sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
sys.path.insert(0, str(Path(sys.argv[1]) / 'tests'))
import costmarshal_v2.actor_runner as actor_runner
from costmarshal_v2.paths import ProjectLayout
from oci_actor_runner_test import HardExitOciAdapter
actor_runner.OciWorkerExecutionAdapter = HardExitOciAdapter
raise SystemExit(actor_runner.run_actor(
    ProjectLayout(root=Path(sys.argv[2]), project_dir=Path(sys.argv[3])),
    sys.argv[4], attempt_id=sys.argv[5], launch_token=sys.argv[6],
))
"""
        hard_environment = os.environ.copy()
        hard_environment.update(
            {
                "CODEX_HOME": str(codex_home),
                "COSTMARSHAL_HARD_EXIT_DAEMON": str(hard_daemon),
                "COSTMARSHAL_HARD_EXIT_ON_START": "1",
            }
        )
        hard_crash = subprocess.run(
            [
                sys.executable,
                "-c",
                hard_child,
                str(ROOT),
                str(layout.root),
                str(project_dir),
                hard_actor["id"],
                hard_actor["attempt_id"],
                hard_actor["launch_token"],
            ],
            env=hard_environment,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        assert hard_crash.returncode == 88, hard_crash.stderr
        after_external_create = load_actor(layout, hard_actor["id"])
        assert after_external_create["runtime"]["oci_lifecycle_state"] == "prepared"
        assert after_external_create["runtime"].get("container_id") is None
        assert (hard_daemon / "container.json").is_file()
        assert (hard_daemon / "provider-calls.txt").read_text(encoding="utf-8").strip() == "1"

        recovery_environment = {
            "CODEX_HOME": str(codex_home),
            "COSTMARSHAL_HARD_EXIT_DAEMON": str(hard_daemon),
        }
        with patch.dict(os.environ, recovery_environment, clear=False), patch(
            "costmarshal_v2.actor_runner.OciWorkerExecutionAdapter",
            HardExitOciAdapter,
        ):
            hard_recovered = run_actor(
                layout,
                hard_actor["id"],
                attempt_id=hard_actor["attempt_id"],
                launch_token=hard_actor["launch_token"],
            )
        assert hard_recovered == 0
        assert (hard_daemon / "provider-calls.txt").read_text(encoding="utf-8").strip() == "1"
        assert not (hard_daemon / "fresh-starts.txt").exists()
        assert not (hard_daemon / "container.json").exists()
        assert HardExitOciAdapter.last_cleanup_state is not None
        assert HardExitOciAdapter.last_cleanup_state["prompt_sha256"] == expected_prompt_sha256
        assert HardExitOciAdapter.last_cleanup_state["usage"] == {
            "input_tokens": 23,
            "output_tokens": 8,
        }
        hard_finished = load_actor(layout, hard_actor["id"])
        assert hard_finished["runtime"]["container_id"] == "e" * 64
        assert hard_finished["runtime"]["oci_lifecycle_state"] == "cleaned"
        assert hard_finished["runtime"]["provider_execution_state"] == "finished"
        assert hard_finished["runtime"]["usage_status"] == "captured"
        usage_commands = queued_command_args(project_dir, "V2-0003", "record_usage")
        assert len(usage_commands) == 1, usage_commands
        assert usage_commands[0]["input_tokens"] == 23
        assert usage_commands[0]["output_tokens"] == 8

        # A failed rm/inspect with a still-present container must preserve the
        # credential and leave provider completion unresolved.
        cli(temp, "new-task", "--project", str(project_dir), "--title", "cleanup", "--purpose", "preserve uncertain credential")
        cleanup_dispatch = cli(
            temp,
            "dispatch",
            "--project",
            str(project_dir),
            "--task",
            "V2-0004",
            "--unsafe-native",
        )
        cleanup_actor = load_actor(layout, cleanup_dispatch["actor_id"])
        cleanup_actor["isolation"] = json.loads(json.dumps(actor["isolation"]))
        save_actor(layout, cleanup_actor)
        with patch.dict(os.environ, {"CODEX_HOME": str(codex_home)}, clear=False), patch(
            "costmarshal_v2.actor_runner.OciWorkerExecutionAdapter",
            CleanupUnconfirmedAdapter,
        ):
            cleanup_returncode = run_actor(
                layout,
                cleanup_actor["id"],
                attempt_id=cleanup_actor["attempt_id"],
                launch_token=cleanup_actor["launch_token"],
            )
        assert cleanup_returncode == 125
        cleanup_uncertain = load_actor(layout, cleanup_actor["id"])
        assert cleanup_uncertain["status"] == "needs_recovery"
        assert cleanup_uncertain["runtime"]["oci_lifecycle_state"] == "uncertain_cleanup"
        assert cleanup_uncertain["runtime"]["provider_execution_state"] == "started"
        assert cleanup_uncertain["runtime"]["credential_cleanup"]["status"] == "pending"
        assert Path(cleanup_uncertain["runtime"]["credential_cleanup"]["path"]).is_file()
        assert not (
            project_dir / "tasks" / "V2-0004" / "attempts" / f"{cleanup_actor['id']}.md"
        ).exists()
        cleanup_task = json.loads(
            (project_dir / "tasks" / "V2-0004" / "task.json").read_text(encoding="utf-8")
        )
        assert cleanup_task["status"] == "needs_recovery"
        assert cleanup_task["attempts"][-1]["status"] == "needs_recovery"

        # If durable engine logs cannot recover usage, never emit a final
        # zero-token receipt that could release the reservation cheaply.
        cli(temp, "new-task", "--project", str(project_dir), "--title", "usage", "--purpose", "keep recovered usage unknown")
        usage_dispatch = cli(
            temp,
            "dispatch",
            "--project",
            str(project_dir),
            "--task",
            "V2-0005",
            "--unsafe-native",
        )
        usage_actor = load_actor(layout, usage_dispatch["actor_id"])
        usage_actor["isolation"] = json.loads(json.dumps(actor["isolation"]))
        usage_identity = SimpleNamespace(
            project_id=project["project_id"],
            actor_id=usage_actor["id"],
            attempt_id=usage_actor["attempt_id"],
        )
        usage_actor.setdefault("runtime", {}).update(
            {
                "container_name": _expected_oci_container_name(usage_identity),
                "container_command": ["costmarshal-worker", "--jsonl", "--model", "LongCat-2.0"],
                "oci_lifecycle_state": "started",
            }
        )
        save_actor(layout, usage_actor)
        with patch.dict(os.environ, {"CODEX_HOME": str(codex_home)}, clear=False), patch(
            "costmarshal_v2.actor_runner.OciWorkerExecutionAdapter",
            RecoveryLogsUnavailableAdapter,
        ):
            usage_returncode = run_actor(
                layout,
                usage_actor["id"],
                attempt_id=usage_actor["attempt_id"],
                launch_token=usage_actor["launch_token"],
            )
        assert usage_returncode == 125
        unknown_usage_actor = load_actor(layout, usage_actor["id"])
        assert unknown_usage_actor["runtime"]["usage_status"] == "unknown_recovery_logs"
        unknown_usage_task = json.loads(
            (project_dir / "tasks" / "V2-0005" / "task.json").read_text(encoding="utf-8")
        )
        assert unknown_usage_task["attempts"][-1]["usage_status"] == "unknown_recovery_logs"
        assert not unknown_usage_task["attempts"][-1].get("cost_settled")
        unknown_usage_commands = queued_command_args(project_dir, "V2-0005", "record_usage")
        assert unknown_usage_commands == [], unknown_usage_commands
        print("oci actor runner ok")
        print(
            "COSTMARSHAL_RUNTIME_EVIDENCE="
            + json.dumps(
                {
                    "schema_version": 1,
                    "test": "tests/oci_actor_runner_test.py",
                    "crash_points": [
                        "after_credential_before_oci_prepare",
                        "after_oci_prepare_before_start",
                        "after_external_create_before_durable_identity",
                    ],
                    "recovery_scenarios": [
                        "credential_after_create_before_oci_prepare",
                        "oci_prepared_before_start",
                        "deterministic_name_attach_after_hard_exit",
                        "cleanup_unconfirmed_preserves_credential",
                        "recovered_usage_unknown_preserves_budget_reservation",
                    ],
                    "provider_calls": 1,
                    "expected_provider_calls": 1,
                    "orphan_effects": 0,
                },
                sort_keys=True,
            )
        )
        return 0
    finally:
        shutil.rmtree(temp, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
