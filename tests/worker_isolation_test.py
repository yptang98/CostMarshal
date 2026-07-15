from __future__ import annotations

import json
import shutil
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from typing import Sequence


ROOT = Path(__file__).resolve().parents[1]
import sys

if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


from costmarshal_v2.worker_isolation import (  # noqa: E402
    CANARY_SCHEMA,
    CommandResult,
    IsolationUnavailableError,
    IsolationValidationError,
    OciCliBackend,
    UnsafeNativeBackend,
    WorkerExecutionSpec,
    select_worker_isolation_backend,
    validate_execution_spec,
)


DIGEST = "a" * 64
IMAGE = f"ghcr.io/example/costmarshal-worker@sha256:{DIGEST}"
SECRET_VALUE = "provider-secret-must-never-appear-in-argv"


def canary_payload(**overrides) -> str:
    payload = {
        "schema": CANARY_SCHEMA,
        "uid": 65532,
        "cap_eff": "0000000000000000",
        "no_new_privileges": True,
        "rootfs_write_blocked": True,
        "workspace_readable": True,
        "workspace_writable": True,
        "output_writable": True,
        "runtime_visible": False,
        "aggregate_secrets_visible": False,
        "engine_socket_visible": False,
    }
    payload.update(overrides)
    return json.dumps(payload)


class FakeRunner:
    def __init__(self, engine: str, *, endpoint: str = "unix:///var/run/docker.sock", system: str = "linux", remote: bool = False, machine_state: str = "running", canary: str | None = None, network_internal: bool = True, network_label: str | None = "true", network_id: str = "d" * 64) -> None:
        self.engine = engine
        self.endpoint = endpoint
        self.system = system
        self.remote = remote
        self.machine_state = machine_state
        self.canary = canary or canary_payload()
        self.network_internal = network_internal
        self.network_label = network_label
        self.network_id = network_id
        self.calls: list[tuple[str, ...]] = []

    def __call__(self, argv: Sequence[str], *, timeout: float) -> CommandResult:
        call = tuple(argv)
        self.calls.append(call)
        if self.engine == "docker" and call[1:] == ("context", "show"):
            return CommandResult(0, "default\n")
        if self.engine == "docker" and "version" in call:
            return CommandResult(0, json.dumps({"Server": {"Os": self.system, "Version": "27.1.0"}}))
        if self.engine == "docker" and call[1:3] == ("context", "inspect"):
            return CommandResult(0, json.dumps({"Endpoints": {"docker": {"Host": self.endpoint}}}))
        if self.engine == "podman" and "info" in call:
            return CommandResult(
                0,
                json.dumps(
                    {
                        "host": {"os": self.system, "security": {"rootless": True}},
                        "version": {"Version": "5.6.1"},
                        "remote": self.remote,
                    }
                ),
            )
        if self.engine == "podman" and "machine" in call:
            return CommandResult(0, json.dumps({"State": self.machine_state}))
        if "image" in call and "inspect" in call:
            return CommandResult(
                0,
                json.dumps(
                    {
                        "RepoDigests": [IMAGE],
                        "Digest": f"sha256:{DIGEST}",
                        "Config": {"Entrypoint": []},
                    }
                ),
            )
        if "network" in call and "inspect" in call:
            return CommandResult(
                0,
                json.dumps(
                    {
                        "Internal": self.network_internal,
                        "Labels": {"io.costmarshal.provider-proxy": self.network_label},
                        "Id": self.network_id,
                    }
                ),
            )
        if "costmarshal-isolation-canary" in call:
            return CommandResult(0, self.canary)
        return CommandResult(2, "", "unexpected mocked command: " + " ".join(call))


class RejectingBackend:
    def __init__(self, kind: str) -> None:
        self.kind = kind
        self.calls = 0

    def preflight(self, spec: WorkerExecutionSpec):
        self.calls += 1
        raise IsolationUnavailableError("mock_rejected", f"{self.kind} rejected")


class WorkerIsolationTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = Path(tempfile.mkdtemp(prefix="costmarshal-worker-isolation-"))
        self.workspace = self.temp / "workspace"
        self.workspace.mkdir()
        (self.workspace / "source.py").write_text("print('ok')\n", encoding="utf-8")
        self.profile = self.temp / "profile.config.toml"
        self.profile.write_text("model = 'safe'\n", encoding="utf-8")
        self.output = self.temp / "out"
        self.output.mkdir()
        self.credential = self.temp / "selected-provider.secret"
        self.credential.write_text(SECRET_VALUE, encoding="utf-8")

    def tearDown(self) -> None:
        shutil.rmtree(self.temp, ignore_errors=True)

    def spec(self, **overrides) -> WorkerExecutionSpec:
        values = {
            "project_id": "project-1",
            "actor_id": "agent-v2-0001",
            "attempt_id": "ATT-20260716-0001",
            "image": IMAGE,
            "workspace": self.workspace,
            "workspace_mode": "rw",
            "profile_path": self.profile,
            "output_exchange": self.output,
            "credential_path": self.credential,
            "provider_env_key": "LONGCAT_API_KEY",
        }
        values.update(overrides)
        return WorkerExecutionSpec(**values)

    def test_digest_pin_and_mount_contract(self) -> None:
        with self.assertRaises(IsolationValidationError) as caught:
            validate_execution_spec(self.spec(image="ghcr.io/example/worker:latest"))
        self.assertEqual(caught.exception.code, "image_digest_required")

        nested_output = self.workspace / "out"
        nested_output.mkdir()
        with self.assertRaises(IsolationValidationError) as overlap:
            validate_execution_spec(self.spec(output_exchange=nested_output))
        self.assertEqual(overlap.exception.code, "mount_overlap")

        with self.assertRaises(IsolationValidationError) as unsafe_network:
            validate_execution_spec(self.spec(network_mode="host"))  # type: ignore[arg-type]
        self.assertEqual(unsafe_network.exception.code, "network_invalid")

        with self.assertRaises(IsolationValidationError) as bridge_network:
            validate_execution_spec(self.spec(network_mode="bridge"))
        self.assertEqual(bridge_network.exception.code, "network_invalid")

        with self.assertRaises(IsolationValidationError) as unpaired_credential:
            validate_execution_spec(self.spec(credential_path=None))
        self.assertEqual(unpaired_credential.exception.code, "credential_contract_invalid")

    def test_symlink_mount_component_is_rejected(self) -> None:
        target = self.temp / "real-workspace"
        target.mkdir()
        link = self.temp / "linked-workspace"
        try:
            link.symlink_to(target, target_is_directory=True)
        except (OSError, NotImplementedError):
            self.skipTest("symlinks are unavailable on this host")
        with self.assertRaises(IsolationValidationError) as caught:
            validate_execution_spec(self.spec(workspace=link))
        self.assertEqual(caught.exception.code, "mount_path_linked")

    def test_forbidden_root_overlap_is_rejected(self) -> None:
        with self.assertRaises(IsolationValidationError) as caught:
            validate_execution_spec(self.spec(forbidden_mount_roots=(self.workspace.parent,)))
        self.assertEqual(caught.exception.code, "forbidden_mount_overlap")

    def test_docker_preflight_and_worker_argv_do_not_contain_secret(self) -> None:
        runner = FakeRunner("docker")
        backend = OciCliBackend("docker", runner=runner, host_system="Linux")
        spec = self.spec(engine="docker")
        attestation = backend.preflight(spec)
        self.assertTrue(attestation.strong_isolation)
        self.assertEqual(attestation.image_digest, f"sha256:{DIGEST}")
        self.assertEqual(attestation.endpoint, "unix:///var/run/docker.sock")
        self.assertEqual(
            attestation.probe_provenance,
            "image-internal-digest-bound-scratch-no-network",
        )
        self.assertTrue(any("cryptographic build provenance" in item for item in attestation.warnings))
        argv = backend.build_run_argv(spec, ["codex", "exec", "-"])
        rendered = "\n".join(argv)
        self.assertNotIn(SECRET_VALUE, rendered)
        self.assertIn("--read-only", argv)
        self.assertIn("no-new-privileges", argv)
        self.assertIn("ALL", argv)
        self.assertIn("never", argv)
        self.assertIn("/run/secrets/provider", rendered)
        canary_argv = next(call for call in runner.calls if "costmarshal-isolation-canary" in call)
        canary_rendered = "\n".join(canary_argv)
        self.assertNotIn("/run/secrets/provider", canary_rendered)
        self.assertNotIn(str(self.profile), canary_rendered)
        self.assertNotIn(str(self.workspace), canary_rendered)
        self.assertNotIn(str(self.output), canary_rendered)
        self.assertEqual(canary_argv[canary_argv.index("--network") + 1], "none")
        self.assertEqual(argv[:4], ["docker", "--host", "unix:///var/run/docker.sock", "run"])
        self.assertTrue(any(call[:4] == ("docker", "--host", "unix:///var/run/docker.sock", "version") for call in runner.calls))
        context_inspect_index = next(
            index for index, call in enumerate(runner.calls) if call[1:3] == ("context", "inspect")
        )
        for call in runner.calls[context_inspect_index + 1 :]:
            self.assertEqual(call[:3], ("docker", "--host", "unix:///var/run/docker.sock"))

    def test_remote_docker_context_is_rejected(self) -> None:
        backend = OciCliBackend(
            "docker",
            runner=FakeRunner("docker", endpoint="ssh://builder.example.invalid"),
            host_system="Linux",
        )
        with self.assertRaises(IsolationUnavailableError) as caught:
            backend.preflight(self.spec(engine="docker"))
        self.assertEqual(caught.exception.code, "remote_engine_rejected")

        unknown = OciCliBackend(
            "docker",
            runner=FakeRunner("docker", endpoint=""),
            host_system="Linux",
        )
        with self.assertRaises(IsolationUnavailableError) as missing:
            unknown.preflight(self.spec(engine="docker"))
        self.assertEqual(missing.exception.code, "engine_endpoint_unknown")

    def test_windows_requires_linux_docker_engine(self) -> None:
        backend = OciCliBackend(
            "docker",
            runner=FakeRunner("docker", endpoint="npipe:////./pipe/docker_engine", system="windows"),
            host_system="Windows",
        )
        with self.assertRaises(IsolationUnavailableError) as caught:
            backend.preflight(self.spec(engine="docker"))
        self.assertEqual(caught.exception.code, "windows_container_mode_rejected")

    def test_windows_podman_machine_can_attest(self) -> None:
        backend = OciCliBackend("podman", runner=FakeRunner("podman"), host_system="Windows")
        result = backend.preflight(self.spec(engine="podman"))
        self.assertEqual(result.endpoint, "podman-machine")
        self.assertTrue(result.rootless)

    def test_linux_remote_podman_is_rejected(self) -> None:
        backend = OciCliBackend("podman", runner=FakeRunner("podman", remote=True), host_system="Linux")
        with self.assertRaises(IsolationUnavailableError) as caught:
            backend.preflight(self.spec(engine="podman"))
        self.assertEqual(caught.exception.code, "remote_engine_rejected")

    def test_canary_failure_is_fail_closed(self) -> None:
        runner = FakeRunner("docker", canary=canary_payload(engine_socket_visible=True))
        backend = OciCliBackend("docker", runner=runner, host_system="Linux")
        with self.assertRaises(IsolationUnavailableError) as caught:
            backend.preflight(self.spec(engine="docker"))
        self.assertEqual(caught.exception.code, "isolation_canary_failed")
        self.assertIn("engine_socket_hidden", caught.exception.details["failed"])

    def test_provider_proxy_network_requires_internal_trusted_network(self) -> None:
        spec = self.spec(
            engine="docker",
            network_mode="provider-proxy",
            network_name="costmarshal-provider-proxy",
        )
        backend = OciCliBackend("docker", runner=FakeRunner("docker"), host_system="Linux")
        attestation = backend.preflight(spec)
        self.assertEqual(dict(attestation.network_policy)["mode"], "provider-proxy")
        self.assertFalse(dict(attestation.network_policy)["external_egress"])
        self.assertEqual(dict(attestation.network_policy)["network_id"], "d" * 64)
        argv = backend.build_run_argv(spec, ["codex", "exec", "-"], network_id="d" * 64)
        self.assertEqual(argv[argv.index("--network") + 1], "d" * 64)

        for runner, code in (
            (FakeRunner("docker", network_internal=False), "provider_proxy_network_not_internal"),
            (FakeRunner("docker", network_label=None), "provider_proxy_network_untrusted"),
            (FakeRunner("docker", network_id="mutable-name"), "provider_proxy_network_id_invalid"),
        ):
            with self.assertRaises(IsolationUnavailableError) as caught:
                OciCliBackend("docker", runner=runner, host_system="Linux").preflight(spec)
            self.assertEqual(caught.exception.code, code)

    def test_required_mode_never_falls_back_to_native(self) -> None:
        docker = RejectingBackend("docker")
        podman = RejectingBackend("podman")
        native = UnsafeNativeBackend(project_opt_in=True, dispatch_opt_in=True)
        with self.assertRaises(IsolationUnavailableError) as caught:
            select_worker_isolation_backend(
                self.spec(engine="auto"),
                docker=docker,
                podman=podman,
                unsafe_native=native,
            )
        self.assertEqual(caught.exception.code, "required_isolation_unavailable")
        self.assertIn("native fallback is forbidden", str(caught.exception))
        self.assertEqual(docker.calls, 1)
        self.assertEqual(podman.calls, 1)

    def test_unsafe_native_requires_two_explicit_gates(self) -> None:
        unsafe_spec = replace(self.spec(), isolation_mode="unsafe-native")
        for project_opt_in, dispatch_opt_in in ((False, False), (True, False), (False, True)):
            backend = UnsafeNativeBackend(
                project_opt_in=project_opt_in,
                dispatch_opt_in=dispatch_opt_in,
            )
            with self.assertRaises(IsolationUnavailableError) as caught:
                backend.preflight(unsafe_spec)
            self.assertEqual(caught.exception.code, "unsafe_native_double_opt_in_required")

        backend = UnsafeNativeBackend(project_opt_in=True, dispatch_opt_in=True)
        attestation = backend.preflight(unsafe_spec)
        self.assertFalse(attestation.strong_isolation)
        with self.assertRaises(IsolationUnavailableError) as required:
            backend.preflight(self.spec())
        self.assertEqual(required.exception.code, "native_fallback_forbidden")


if __name__ == "__main__":
    unittest.main(verbosity=2)
