from __future__ import annotations

import json
import shutil
import sys
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from typing import Callable, Mapping, Sequence


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


from costmarshal_v2.worker_isolation import (  # noqa: E402
    CANARY_SCHEMA,
    CapturedProcessResult,
    CommandResult,
    IsolationValidationError,
    OciCliBackend,
    OciWorkerExecutionAdapter,
    WorkerExecutionError,
    WorkerExecutionSpec,
    validate_output_exchange,
)


DIGEST = "b" * 64
IMAGE = f"ghcr.io/example/costmarshal-worker@sha256:{DIGEST}"
SECRET = "provider-secret-never-log-this-value"


def canary_json() -> str:
    return json.dumps(
        {
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
    )


class LifecycleRunner:
    def __init__(self, engine: str = "docker") -> None:
        self.engine = engine
        self.calls: list[tuple[str, ...]] = []
        self.run_argv: tuple[str, ...] | None = None
        self.labels_override: Mapping[str, str] | None = None
        self.payload_mutator: Callable[[dict[str, object]], None] | None = None
        self.status = "exited"
        self.exit_code = 0
        self.inspect_failure = False
        self.remove_failure = False
        self.listed_present = False
        self.logs_failure = False
        self.logs_stdout = json.dumps(
            {"usage": {"input_tokens": 19, "output_tokens": 7}}
        ) + "\n"
        self.logs_stderr = ""

    @staticmethod
    def _command(call: tuple[str, ...]) -> tuple[str, ...]:
        if len(call) >= 4 and call[1] in {"--context", "--host"}:
            return call[3:]
        return call[1:]

    def _inspect_payload(self) -> dict[str, object]:
        if self.run_argv is None:
            raise AssertionError("container inspect occurred before the mocked run argv was recorded")
        argv = list(self.run_argv)

        def option(name: str) -> str:
            return argv[argv.index(name) + 1]

        labels: dict[str, str] = {}
        for index, item in enumerate(argv[:-1]):
            if item == "--label":
                key, value = argv[index + 1].split("=", 1)
                labels[key] = value
        if self.labels_override is not None:
            labels = dict(self.labels_override)

        mounts: list[dict[str, object]] = []
        for index, item in enumerate(argv[:-1]):
            if item != "--mount":
                continue
            fields: dict[str, str] = {}
            flags: set[str] = set()
            for field in argv[index + 1].split(","):
                if "=" in field:
                    key, value = field.split("=", 1)
                    fields[key] = value
                else:
                    flags.add(field)
            mounts.append(
                {
                    "Type": fields["type"],
                    "Source": fields["src"],
                    "Destination": fields["dst"],
                    "RW": "readonly" not in flags,
                }
            )
        image_index = argv.index(IMAGE)
        network_mode = option("--network")
        networks = {} if network_mode == "none" else {"provider-proxy": {"NetworkID": network_mode}}
        payload: dict[str, object] = {
            "Id": "c" * 64,
            "Name": "/" + option("--name"),
            "Config": {
                "Labels": labels,
                "Image": IMAGE if self.engine == "docker" else "",
                "Cmd": argv[image_index + 1 :],
                "Entrypoint": None,
                "User": option("--user"),
                "Env": [],
            },
            "HostConfig": {
                "ReadonlyRootfs": True,
                "CapDrop": ["ALL"] if self.engine == "docker" else ["CAP_ALL"],
                "CapAdd": [],
                "SecurityOpt": ["no-new-privileges"],
                "Privileged": False,
                "PidMode": "",
                "IpcMode": "",
                "PidsLimit": 256,
                "Memory": int(option("--memory")[:-1]) * 1024 * 1024,
                "NanoCpus": int(float(option("--cpus")) * 1_000_000_000),
                "Init": "--init" in argv,
                "LogConfig": {"Type": option("--log-driver"), "Config": {}},
                "Tmpfs": {
                    argv[index + 1].split(":", 1)[0]: argv[index + 1].split(":", 1)[1]
                    for index, item in enumerate(argv[:-1])
                    if item == "--tmpfs"
                },
                "NetworkMode": network_mode,
            },
            "Mounts": mounts,
            "NetworkSettings": {"Networks": networks},
            "State": {"Status": self.status, "ExitCode": self.exit_code},
        }
        if self.engine == "podman":
            payload["ImageName"] = IMAGE
            payload["ImageDigest"] = f"sha256:{DIGEST}"
            payload["EffectiveCaps"] = []
        if self.payload_mutator is not None:
            self.payload_mutator(payload)
        return payload

    def __call__(self, argv: Sequence[str], *, timeout: float) -> CommandResult:
        call = tuple(argv)
        self.calls.append(call)
        command = self._command(call)
        if self.engine == "podman" and command and command[0] == "info":
            return CommandResult(
                0,
                json.dumps(
                    {
                        "host": {"os": "linux", "security": {"rootless": True}},
                        "version": {"Version": "5.6.1"},
                        "remote": False,
                    }
                ),
            )
        if command == ("context", "show"):
            return CommandResult(0, "default\n")
        if command and command[0] == "version":
            return CommandResult(0, json.dumps({"Server": {"Os": "linux", "Version": "27.1.0"}}))
        if command[:2] == ("context", "inspect"):
            return CommandResult(0, json.dumps({"Endpoints": {"docker": {"Host": "unix:///var/run/docker.sock"}}}))
        if command[:2] == ("image", "inspect"):
            return CommandResult(0, json.dumps({"RepoDigests": [IMAGE], "Config": {"Entrypoint": []}}))
        if "costmarshal-isolation-canary" in call:
            return CommandResult(0, canary_json())
        if command and command[0] == "inspect":
            if self.inspect_failure:
                return CommandResult(1, "", "inspect failed")
            return CommandResult(0, json.dumps(self._inspect_payload()))
        if command and command[0] == "ps":
            if self.listed_present:
                payload = self._inspect_payload()
                return CommandResult(
                    0,
                    json.dumps({"ID": payload["Id"], "Names": payload["Name"]}) + "\n",
                )
            return CommandResult(0, "")
        if command and command[0] == "logs":
            if self.logs_failure:
                return CommandResult(1, "", "logs unavailable")
            return CommandResult(0, self.logs_stdout, self.logs_stderr)
        if command and command[0] == "rm" and self.remove_failure:
            return CommandResult(1, "", "remove failed")
        if command and command[0] in {"stop", "rm"}:
            if command[0] == "rm":
                self.listed_present = False
            return CommandResult(0, call[-1] + "\n")
        return CommandResult(2, "", "unexpected mocked command")


class FakeManagedProcess:
    def __init__(self, result: CapturedProcessResult) -> None:
        self.pid = 12345
        self.result = result
        self.input_bytes: bytes | None = None
        self.timeout: float | None = None
        self.stdout_limit: int | None = None
        self.stderr_limit: int | None = None
        self.killed = False
        self.completed = False
        self.preloaded_input = b""

    def communicate_bounded(
        self,
        input_bytes: bytes,
        *,
        timeout: float,
        max_stdout_bytes: int,
        max_stderr_bytes: int,
    ) -> CapturedProcessResult:
        self.input_bytes = self.preloaded_input + input_bytes
        self.timeout = timeout
        self.stdout_limit = max_stdout_bytes
        self.stderr_limit = max_stderr_bytes
        self.completed = not self.result.timed_out
        return self.result

    def poll(self) -> int | None:
        return self.result.exit_code if self.completed else None

    def kill(self) -> None:
        self.killed = True
        self.completed = True


class FakeProcessFactory:
    def __init__(self, process: FakeManagedProcess, runner: LifecycleRunner) -> None:
        self.process = process
        self.runner = runner
        self.argv: tuple[str, ...] | None = None

    def __call__(self, argv: Sequence[str], stdin_source) -> FakeManagedProcess:
        self.argv = tuple(argv)
        self.runner.run_argv = self.argv
        self.process.preloaded_input = stdin_source.read()
        return self.process


class WorkerExecutionAdapterTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = Path(tempfile.mkdtemp(prefix="costmarshal-execution-adapter-"))
        self.workspace = self.temp / "workspace"
        self.workspace.mkdir()
        (self.workspace / "task.py").write_text("print('ok')\n", encoding="utf-8")
        self.profile = self.temp / "profile.config.toml"
        self.profile.write_text("model = 'safe'\n", encoding="utf-8")
        self.output = self.temp / "output"
        self.output.mkdir()
        self.credential_root = self.temp / "credential-temp"
        self.credential_root.mkdir()
        self.credential = self.credential_root / "provider.secret"
        self.credential.write_text(SECRET, encoding="utf-8")

    def tearDown(self) -> None:
        shutil.rmtree(self.temp, ignore_errors=True)

    def spec(self, **overrides) -> WorkerExecutionSpec:
        values = {
            "project_id": "project-1",
            "actor_id": "agent-v2-0001",
            "attempt_id": "ATT-20260716-0002",
            "image": IMAGE,
            "workspace": self.workspace,
            "workspace_mode": "rw",
            "profile_path": self.profile,
            "output_exchange": self.output,
            "credential_path": self.credential,
            "provider_env_key": "PROVIDER_API_KEY",
            "credential_cleanup": "delete-after-use",
            "credential_temp_root": self.credential_root,
            "engine": "docker",
        }
        values.update(overrides)
        return WorkerExecutionSpec(**values)

    def adapter(self, result: CapturedProcessResult, *, engine: str = "docker", **kwargs):
        runner = LifecycleRunner(engine)
        process = FakeManagedProcess(result)
        factory = FakeProcessFactory(process, runner)
        backend = OciCliBackend(engine, runner=runner, host_system="Linux")
        adapter = OciWorkerExecutionAdapter(backend, process_factory=factory, **kwargs)
        return adapter, runner, process, factory

    def start(self, result: CapturedProcessResult, **adapter_kwargs):
        adapter, runner, process, factory = self.adapter(result, **adapter_kwargs)
        handle = adapter.start(self.spec(), ["codex", "exec", "-"], stdin_prompt="perform the task")
        return adapter, runner, process, factory, handle

    def test_start_uses_argv_labels_and_never_places_prompt_or_secret_in_argv(self) -> None:
        adapter, runner, process, factory, handle = self.start(CapturedProcessResult(0, b'{"type":"ok"}\n'))
        self.assertIsNotNone(factory.argv)
        rendered = "\n".join(factory.argv or ())
        self.assertNotIn(SECRET, rendered)
        self.assertNotIn("perform the task", rendered)
        self.assertIn("--name", factory.argv)
        self.assertIn("--label", factory.argv)
        self.assertIn("--log-driver", factory.argv)
        self.assertEqual(factory.argv[factory.argv.index("--log-driver") + 1], "local")
        self.assertNotIn("--rm", factory.argv)
        self.assertNotIn(SECRET, json.dumps(handle.attestation.to_dict(), default=str))
        self.assertNotIn(SECRET, repr(handle))

    def test_start_contract_failure_removes_unregistered_container(self) -> None:
        adapter, runner, process, factory = self.adapter(CapturedProcessResult(0))
        runner.payload_mutator = lambda payload: payload["HostConfig"].__setitem__(  # type: ignore[index]
            "Privileged", True
        )
        with self.assertRaises(WorkerExecutionError) as caught:
            adapter.start(self.spec(), ["codex", "exec", "-"], stdin_prompt="perform the task")
        self.assertEqual(caught.exception.code, "container_security_contract_mismatch")
        rm_calls = [
            call
            for call in runner.calls
            if LifecycleRunner._command(call)[:2] == ("rm", "--force")
        ]
        self.assertEqual(len(rm_calls), 1)
        self.assertEqual(rm_calls[0][-1], "c" * 64)

    def test_nonzero_run_client_still_closes_unregistered_container(self) -> None:
        adapter, runner, process, factory = self.adapter(CapturedProcessResult(17))
        process.completed = True
        runner.listed_present = True
        runner.payload_mutator = lambda payload: payload["HostConfig"].__setitem__(  # type: ignore[index]
            "Privileged", True
        )
        with self.assertRaises(WorkerExecutionError) as caught:
            adapter.start(self.spec(), ["codex", "exec", "-"], stdin_prompt="perform the task")
        self.assertEqual(caught.exception.code, "container_security_contract_mismatch")
        rm_calls = [
            call
            for call in runner.calls
            if LifecycleRunner._command(call)[:2] == ("rm", "--force")
        ]
        self.assertEqual(len(rm_calls), 1)
        self.assertFalse(runner.listed_present)

    def test_podman_inspect_uses_image_name_digest_and_effective_caps(self) -> None:
        adapter, runner, process, factory = self.adapter(CapturedProcessResult(0), engine="podman")
        spec = self.spec(engine="podman")
        handle = adapter.start(spec, ["codex", "exec", "-"], stdin_prompt="perform the task")
        self.assertEqual((factory.argv or ())[0:2], ("podman", "run"))
        inspection = adapter.inspect(handle)
        self.assertEqual(inspection.container_id, "c" * 64)

    def test_wait_sends_stdin_parses_jsonl_and_redacts_credential(self) -> None:
        stdout = (json.dumps({"type": "message", "text": f"prefix {SECRET} suffix"}) + "\n").encode()
        adapter, runner, process, factory, handle = self.start(
            CapturedProcessResult(0, stdout, f"stderr {SECRET}".encode())
        )
        receipt = adapter.wait(handle, timeout=7)
        self.assertEqual(process.input_bytes, b"perform the task")
        self.assertEqual(process.timeout, 7)
        self.assertEqual(receipt.stdout_events[0]["text"], "prefix [REDACTED] suffix")
        self.assertNotIn(SECRET, repr(receipt))
        self.assertEqual(receipt.stderr_bytes, len(f"stderr {SECRET}".encode()))

    def test_invalid_json_error_does_not_echo_secret(self) -> None:
        adapter, runner, process, factory, handle = self.start(
            CapturedProcessResult(0, f"not-json {SECRET}\n".encode())
        )
        with self.assertRaises(WorkerExecutionError) as caught:
            adapter.wait(handle)
        self.assertEqual(caught.exception.code, "worker_stdout_json_invalid")
        self.assertNotIn(SECRET, str(caught.exception))
        self.assertNotIn(SECRET, json.dumps(caught.exception.details))

    def test_stdout_limit_and_timeout_fail_closed_with_stop(self) -> None:
        adapter, runner, process, factory, handle = self.start(
            CapturedProcessResult(137, b"x", stdout_truncated=True)
        )
        with self.assertRaises(WorkerExecutionError) as oversized:
            adapter.wait(handle)
        self.assertEqual(oversized.exception.code, "worker_stdout_limit_exceeded")
        self.assertTrue(any(LifecycleRunner._command(call)[0] == "stop" for call in runner.calls if LifecycleRunner._command(call)))

        adapter2, runner2, process2, factory2, handle2 = self.start(
            CapturedProcessResult(137, timed_out=True)
        )
        with self.assertRaises(WorkerExecutionError) as timed_out:
            adapter2.wait(handle2, timeout=0.5)
        self.assertEqual(timed_out.exception.code, "worker_timeout")
        self.assertEqual(process2.timeout, 0.5)
        self.assertTrue(any(LifecycleRunner._command(call)[0] == "stop" for call in runner2.calls if LifecycleRunner._command(call)))

        adapter3, runner3, process3, factory3, handle3 = self.start(
            CapturedProcessResult(137, stderr_truncated=True)
        )
        with self.assertRaises(WorkerExecutionError) as stderr_limit:
            adapter3.wait(handle3)
        self.assertEqual(stderr_limit.exception.code, "worker_stderr_limit_exceeded")
        self.assertTrue(any(LifecycleRunner._command(call)[0] == "stop" for call in runner3.calls if LifecycleRunner._command(call)))

    def test_wait_requires_terminal_container_and_matching_exit_code(self) -> None:
        adapter, runner, process, factory, handle = self.start(CapturedProcessResult(0))
        runner.status = "running"
        with self.assertRaises(WorkerExecutionError) as running:
            adapter.wait(handle)
        self.assertEqual(running.exception.code, "worker_container_not_exited")

        adapter2, runner2, process2, factory2, handle2 = self.start(CapturedProcessResult(0))
        runner2.exit_code = 7
        recovered = adapter2.wait(handle2)
        self.assertEqual(recovered.exit_code, 7)
        self.assertEqual(recovered.stdout_events[0]["usage"]["input_tokens"], 19)

    def test_name_race_loser_recovers_existing_container_logs(self) -> None:
        adapter, runner, process, factory, handle = self.start(CapturedProcessResult(125))
        runner.status = "exited"
        runner.exit_code = 0
        receipt = adapter.wait(handle)
        self.assertEqual(receipt.exit_code, 0)
        self.assertEqual(receipt.stdout_events[0]["usage"]["input_tokens"], 19)
        self.assertTrue(handle.recovered)

    def test_label_verification_blocks_stop_but_exact_id_cleanup_closes_container(self) -> None:
        adapter, runner, process, factory, handle = self.start(CapturedProcessResult(0))
        runner.labels_override = {**handle.labels, "io.costmarshal.attempt": "wrong-attempt"}
        with self.assertRaises(WorkerExecutionError) as stopped:
            adapter.stop(handle)
        self.assertEqual(stopped.exception.code, "container_label_mismatch")
        self.assertFalse(any(LifecycleRunner._command(call)[0] == "stop" for call in runner.calls if LifecycleRunner._command(call)))

        cleaned = adapter.cleanup(handle)
        self.assertIn("io.costmarshal.attempt", cleaned.identity_drift)
        self.assertTrue(cleaned.credential.deleted)
        self.assertFalse(self.credential.exists())
        self.assertTrue(any(LifecycleRunner._command(call)[0] == "rm" for call in runner.calls if LifecycleRunner._command(call)))

    def test_inspect_stop_cleanup_and_temp_credential_receipt(self) -> None:
        adapter, runner, process, factory, handle = self.start(CapturedProcessResult(0))
        inspection = adapter.inspect(handle)
        self.assertTrue(inspection.labels_verified)
        self.assertEqual(inspection.exit_code, 0)
        adapter.stop(handle, grace_seconds=3)
        receipt = adapter.cleanup(handle)
        self.assertTrue(receipt.container_removed)
        self.assertTrue(receipt.credential.requested)
        self.assertTrue(receipt.credential.deleted)
        self.assertEqual(receipt.credential.bytes_removed, len(SECRET.encode()))
        self.assertFalse(self.credential.exists())
        self.assertTrue(any(LifecycleRunner._command(call)[:3] == ("stop", "--time", "3") for call in runner.calls))
        self.assertTrue(any(LifecycleRunner._command(call)[:2] == ("rm", "--force") for call in runner.calls))
        rm_call = next(call for call in runner.calls if LifecycleRunner._command(call)[:2] == ("rm", "--force"))
        self.assertEqual(rm_call[-1], handle.container_id)
        self.assertNotIn(SECRET, repr(receipt))

    def test_cleanup_is_idempotent_when_credential_is_already_absent(self) -> None:
        adapter, runner, process, factory, handle = self.start(CapturedProcessResult(0))
        self.credential.unlink()
        receipt = adapter.cleanup(handle)
        self.assertTrue(receipt.container_removed)
        self.assertTrue(receipt.credential.requested)
        self.assertTrue(receipt.credential.deleted)
        self.assertEqual(receipt.credential.bytes_removed, 0)
        rm_call = next(call for call in runner.calls if LifecycleRunner._command(call)[:2] == ("rm", "--force"))
        self.assertEqual(rm_call[-1], handle.container_id)

    def test_cleanup_failure_preserves_credential_until_absence_is_proved(self) -> None:
        for failure_mode in ("inspect", "remove"):
            with self.subTest(failure_mode=failure_mode):
                self.credential.write_text(SECRET, encoding="utf-8")
                adapter, runner, process, factory, handle = self.start(CapturedProcessResult(0))
                runner.listed_present = True
                if failure_mode == "inspect":
                    runner.inspect_failure = True
                else:
                    runner.remove_failure = True
                with self.assertRaises(WorkerExecutionError) as caught:
                    adapter.cleanup(handle)
                self.assertEqual(caught.exception.code, "container_cleanup_unconfirmed")
                self.assertTrue(caught.exception.details["container_cleanup_unconfirmed"])
                self.assertFalse(caught.exception.details["credential_deleted"])
                self.assertTrue(self.credential.is_file())

    def test_cleanup_failure_deletes_credential_only_after_confirmed_absence(self) -> None:
        adapter, runner, process, factory, handle = self.start(CapturedProcessResult(0))
        runner.inspect_failure = True
        runner.listed_present = False
        receipt = adapter.cleanup(handle)
        self.assertTrue(receipt.container_removed)
        self.assertTrue(receipt.credential.deleted)
        self.assertFalse(self.credential.exists())

    def test_inspect_rejects_every_immutable_execution_contract_drift(self) -> None:
        def nested(payload: dict[str, object], key: str) -> dict[str, object]:
            value = payload[key]
            self.assertIsInstance(value, dict)
            return value  # type: ignore[return-value]

        mutations: tuple[tuple[str, Callable[[dict[str, object]], None]], ...] = (
            ("container_id_mismatch", lambda payload: payload.__setitem__("Id", "d" * 64)),
            ("container_image_mismatch", lambda payload: nested(payload, "Config").__setitem__("Image", "other")),
            ("container_command_mismatch", lambda payload: nested(payload, "Config").__setitem__("Cmd", ["other"])),
            ("container_user_mismatch", lambda payload: nested(payload, "Config").__setitem__("User", "0:0")),
            (
                "container_security_contract_mismatch",
                lambda payload: nested(payload, "HostConfig").__setitem__("Privileged", True),
            ),
            (
                "container_security_contract_mismatch",
                lambda payload: nested(payload, "HostConfig").pop("Memory"),
            ),
            (
                "container_security_contract_mismatch",
                lambda payload: nested(payload, "HostConfig").__setitem__("NanoCpus", 1),
            ),
            (
                "container_security_contract_mismatch",
                lambda payload: nested(payload, "HostConfig").__setitem__("Tmpfs", {}),
            ),
            (
                "container_security_contract_mismatch",
                lambda payload: nested(nested(payload, "HostConfig"), "LogConfig").__setitem__(
                    "Type", "none"
                ),
            ),
            ("container_mount_mismatch", lambda payload: payload.__setitem__("Mounts", [])),
            (
                "container_network_mismatch",
                lambda payload: nested(nested(payload, "NetworkSettings"), "Networks").__setitem__(
                    "bridge", {"NetworkID": "e" * 64}
                ),
            ),
        )
        for expected_code, mutate in mutations:
            with self.subTest(expected_code=expected_code):
                adapter, runner, process, factory, handle = self.start(CapturedProcessResult(0))
                runner.payload_mutator = mutate
                with self.assertRaises(WorkerExecutionError) as caught:
                    adapter.inspect(handle)
                self.assertEqual(caught.exception.code, expected_code)

    def test_attach_recovers_lifecycle_control_by_deterministic_identity(self) -> None:
        adapter, runner, process, factory, original = self.start(CapturedProcessResult(0))
        (self.output / "final.md").write_text("existing output", encoding="utf-8")
        recovered = adapter.attach(
            self.spec(),
            container_name=original.container_name,
            container_id=original.container_id,
            command=original.command,
        )
        self.assertEqual(recovered.container_name, original.container_name)
        discovered = adapter.attach(
            self.spec(),
            container_name=original.container_name,
            command=original.command,
        )
        self.assertEqual(discovered.container_id, original.container_id)
        receipt = adapter.recover_wait(discovered)
        self.assertEqual(receipt.stdout_events[0]["usage"]["input_tokens"], 19)
        self.assertEqual(receipt.stdout_events[0]["usage"]["output_tokens"], 7)
        inspection = adapter.stop(recovered, grace_seconds=0)
        self.assertTrue(inspection.labels_verified)
        with self.assertRaises(WorkerExecutionError) as mismatch:
            adapter.attach(
                self.spec(),
                container_name=original.container_name + "-other",
                container_id=original.container_id,
                command=original.command,
            )
        self.assertEqual(mismatch.exception.code, "container_identity_mismatch")

    def test_recovered_usage_fails_closed_when_engine_logs_are_unavailable(self) -> None:
        adapter, runner, process, factory, original = self.start(CapturedProcessResult(0))
        recovered = adapter.attach(
            self.spec(),
            container_name=original.container_name,
            container_id=original.container_id,
            command=original.command,
        )
        runner.logs_failure = True
        with self.assertRaises(WorkerExecutionError) as caught:
            adapter.recover_wait(recovered)
        self.assertEqual(caught.exception.code, "worker_recovery_logs_unavailable")

    def test_cleanup_confirmed_absent_requires_durable_identity_and_deletes_credential(self) -> None:
        adapter, runner, process, factory, original = self.start(CapturedProcessResult(0))
        receipt = adapter.cleanup_confirmed_absent(
            self.spec(),
            container_name=original.container_name,
            container_id=original.container_id,
            command=original.command,
        )
        self.assertTrue(receipt.container_removed)
        self.assertTrue(receipt.credential.deleted)
        self.assertFalse(self.credential.exists())
        self.assertTrue(
            any(LifecycleRunner._command(call)[:1] == ("ps",) for call in runner.calls)
        )

    def test_validate_output_exchange_accepts_only_trusted_final_md(self) -> None:
        final = self.output / "final.md"
        final.write_bytes(b"# Final\n\nDone.\n")
        result = validate_output_exchange(self.output)
        self.assertEqual(result.text, "# Final\n\nDone.\n")
        self.assertEqual(result.utf8_bytes, len(result.text.encode()))
        self.assertEqual(len(result.sha256), 64)

        (self.output / "extra.txt").write_text("not allowed", encoding="utf-8")
        with self.assertRaises(IsolationValidationError) as extra:
            validate_output_exchange(self.output)
        self.assertEqual(extra.exception.code, "output_contract_invalid")

    def test_validate_output_exchange_rejects_invalid_utf8_and_oversize(self) -> None:
        final = self.output / "final.md"
        final.write_bytes(b"\xff\xfe")
        with self.assertRaises(IsolationValidationError) as invalid:
            validate_output_exchange(self.output)
        self.assertEqual(invalid.exception.code, "output_encoding_invalid")

        final.write_bytes(b"x" * 33)
        with self.assertRaises(IsolationValidationError) as large:
            validate_output_exchange(self.output, max_bytes=32)
        self.assertEqual(large.exception.code, "output_too_large")

    def test_validate_output_exchange_rejects_symlink(self) -> None:
        real = self.temp / "real-final.md"
        real.write_text("safe", encoding="utf-8")
        link = self.output / "final.md"
        try:
            link.symlink_to(real)
        except (OSError, NotImplementedError):
            self.skipTest("symlinks are unavailable on this host")
        with self.assertRaises(IsolationValidationError) as linked:
            validate_output_exchange(self.output)
        self.assertEqual(linked.exception.code, "mount_path_linked")


if __name__ == "__main__":
    unittest.main(verbosity=2)
