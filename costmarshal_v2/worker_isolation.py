"""Fail-closed worker execution isolation contracts.

This module is deliberately independent from the scheduler and actor runner.
It builds and verifies OCI execution plans without putting secret *values* in
command arguments.  Docker/Podman process supervision remains a separate
concern from CostMarshal's local/tmux actor-session backends.
"""

from __future__ import annotations

import json
import math
import os
import platform
import re
import stat
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Literal, Mapping, Protocol, Sequence


IMAGE_DIGEST_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/-]*@sha256:([0-9a-f]{64})\Z")
ENV_KEY_RE = re.compile(r"[A-Z_][A-Z0-9_]{0,127}\Z")
SAFE_IDENTIFIER_RE = re.compile(r"[A-Za-z0-9]+(?:[-_.][A-Za-z0-9]+)*\Z")
CANARY_SCHEMA = "costmarshal-worker-isolation-canary-v1"


class IsolationError(RuntimeError):
    """Base error for a rejected or unavailable isolation boundary."""

    def __init__(self, code: str, message: str, *, details: Mapping[str, Any] | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.details = dict(details or {})


class IsolationValidationError(IsolationError):
    """The requested isolation plan is structurally unsafe."""


class IsolationUnavailableError(IsolationError):
    """A required isolation backend could not attest its boundary."""


@dataclass(frozen=True)
class CommandResult:
    returncode: int
    stdout: str = ""
    stderr: str = ""


class CommandRunner(Protocol):
    def __call__(self, argv: Sequence[str], *, timeout: float) -> CommandResult:
        """Execute an argv vector without a shell and return captured text."""


def subprocess_command_runner(argv: Sequence[str], *, timeout: float) -> CommandResult:
    completed = subprocess.run(
        list(argv),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
        shell=False,
        timeout=timeout,
    )
    return CommandResult(completed.returncode, completed.stdout, completed.stderr)


@dataclass(frozen=True)
class ResourceLimits:
    memory_mb: int = 2048
    cpus: float = 2.0
    pids: int = 256
    timeout_seconds: float = 30.0
    tmpfs_mb: int = 256
    home_tmpfs_mb: int = 64

    def validate(self) -> None:
        if self.memory_mb < 128:
            raise IsolationValidationError("memory_limit_invalid", "worker memory limit must be at least 128 MiB")
        if not math.isfinite(self.cpus) or not (0.1 <= self.cpus <= 256):
            raise IsolationValidationError("cpu_limit_invalid", "worker CPU limit must be between 0.1 and 256")
        if not (16 <= self.pids <= 65536):
            raise IsolationValidationError("pids_limit_invalid", "worker PID limit must be between 16 and 65536")
        if not math.isfinite(self.timeout_seconds) or self.timeout_seconds <= 0:
            raise IsolationValidationError("timeout_invalid", "worker isolation timeout must be positive")
        if self.tmpfs_mb < 16 or self.home_tmpfs_mb < 16:
            raise IsolationValidationError("tmpfs_limit_invalid", "worker tmpfs limits must be at least 16 MiB")


@dataclass(frozen=True)
class WorkerExecutionSpec:
    project_id: str
    actor_id: str
    attempt_id: str
    image: str
    workspace: Path
    workspace_mode: Literal["ro", "rw"]
    profile_path: Path
    output_exchange: Path
    credential_path: Path | None = None
    provider_env_key: str | None = None
    isolation_mode: Literal["required", "unsafe-native"] = "required"
    engine: Literal["auto", "docker", "podman"] = "auto"
    network_mode: Literal["none", "bridge", "provider-proxy"] = "none"
    network_name: str | None = None
    forbidden_mount_roots: tuple[Path, ...] = ()
    limits: ResourceLimits = field(default_factory=ResourceLimits)

    @property
    def image_digest(self) -> str:
        match = IMAGE_DIGEST_RE.fullmatch(self.image)
        if not match:
            raise IsolationValidationError(
                "image_digest_required",
                "worker image must be pinned as name@sha256:<64 lowercase hex characters>",
            )
        return f"sha256:{match.group(1)}"


@dataclass(frozen=True)
class MountAttestation:
    target: str
    mode: Literal["ro", "rw"]
    source_kind: Literal["workspace", "profile", "credential", "output"]


@dataclass(frozen=True)
class IsolationAttestation:
    schema: str
    backend: str
    engine_version: str
    endpoint: str
    platform: str
    rootless: bool | None
    image: str
    image_digest: str
    strong_isolation: bool
    security_flags: tuple[str, ...]
    mounts: tuple[MountAttestation, ...]
    canary: tuple[tuple[str, Any], ...]
    warnings: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "backend": self.backend,
            "engine_version": self.engine_version,
            "endpoint": self.endpoint,
            "platform": self.platform,
            "rootless": self.rootless,
            "image": self.image,
            "image_digest": self.image_digest,
            "strong_isolation": self.strong_isolation,
            "security_flags": list(self.security_flags),
            "mounts": [mount.__dict__ for mount in self.mounts],
            "canary": dict(self.canary),
            "warnings": list(self.warnings),
        }


@dataclass(frozen=True)
class SelectedBackend:
    backend: "WorkerIsolationBackend"
    attestation: IsolationAttestation


class WorkerIsolationBackend(Protocol):
    kind: str

    def preflight(self, spec: WorkerExecutionSpec) -> IsolationAttestation:
        ...


def _json_object(text: str, label: str) -> dict[str, Any]:
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        raise IsolationUnavailableError("engine_output_invalid", f"{label} did not return valid JSON") from exc
    if isinstance(payload, list) and len(payload) == 1 and isinstance(payload[0], dict):
        payload = payload[0]
    if not isinstance(payload, dict):
        raise IsolationUnavailableError("engine_output_invalid", f"{label} did not return one JSON object")
    return payload


def _run_checked(runner: CommandRunner, argv: Sequence[str], *, timeout: float, label: str) -> str:
    try:
        result = runner(tuple(argv), timeout=timeout)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise IsolationUnavailableError("engine_command_failed", f"{label} could not run") from exc
    if result.returncode != 0:
        raise IsolationUnavailableError(
            "engine_command_failed",
            f"{label} failed",
            details={"returncode": result.returncode, "stderr": result.stderr[-2048:]},
        )
    if len(result.stdout.encode("utf-8", errors="replace")) > 8 * 1024 * 1024:
        raise IsolationUnavailableError("engine_output_too_large", f"{label} output exceeded 8 MiB")
    return result.stdout


def _lexical_absolute(path: Path) -> Path:
    return Path(os.path.abspath(os.path.expanduser(os.fspath(path))))


def _is_reparse_or_link(path: Path) -> bool:
    try:
        info = path.lstat()
    except OSError as exc:
        raise IsolationValidationError("mount_path_unreadable", f"mount path component cannot be inspected: {path}") from exc
    if stat.S_ISLNK(info.st_mode):
        return True
    attributes = getattr(info, "st_file_attributes", 0)
    reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    return bool(attributes & reparse_flag)


def _assert_no_link_components(path: Path, label: str) -> Path:
    lexical = _lexical_absolute(path)
    if "\x00" in os.fspath(lexical) or any(character in os.fspath(lexical) for character in ("\r", "\n")):
        raise IsolationValidationError("mount_path_invalid", f"{label} contains a control character")
    if "," in os.fspath(lexical):
        raise IsolationValidationError("mount_path_invalid", f"{label} contains ',' which is ambiguous in OCI mount syntax")
    components: list[Path] = []
    current = lexical
    while current != current.parent:
        components.append(current)
        current = current.parent
    components.append(current)
    for component in reversed(components):
        if component.exists() and _is_reparse_or_link(component):
            raise IsolationValidationError("mount_path_linked", f"{label} contains a symlink/reparse component: {component}")
    try:
        return lexical.resolve(strict=True)
    except OSError as exc:
        raise IsolationValidationError("mount_path_missing", f"{label} does not resolve: {lexical}") from exc


def _overlap(left: Path, right: Path) -> bool:
    try:
        left.relative_to(right)
        return True
    except ValueError:
        pass
    try:
        right.relative_to(left)
        return True
    except ValueError:
        return False


def validate_execution_spec(spec: WorkerExecutionSpec) -> dict[str, Path | None]:
    """Validate all host mount sources before an engine sees them."""

    spec.image_digest
    spec.limits.validate()
    for value, label in (
        (spec.project_id, "project id"),
        (spec.actor_id, "actor id"),
        (spec.attempt_id, "attempt id"),
    ):
        if not SAFE_IDENTIFIER_RE.fullmatch(value):
            raise IsolationValidationError("identifier_invalid", f"{label} must be a safe identifier")
    if spec.workspace_mode not in {"ro", "rw"}:
        raise IsolationValidationError("workspace_mode_invalid", "workspace mode must be ro or rw")
    if spec.isolation_mode not in {"required", "unsafe-native"}:
        raise IsolationValidationError("isolation_mode_invalid", "isolation mode must be required or unsafe-native")
    if spec.engine not in {"auto", "docker", "podman"}:
        raise IsolationValidationError("engine_invalid", "container engine must be auto, docker, or podman")
    if spec.network_mode not in {"none", "bridge", "provider-proxy"}:
        raise IsolationValidationError(
            "network_invalid",
            "worker network mode must be none, bridge, or provider-proxy",
        )
    if spec.network_mode == "provider-proxy" and not spec.network_name:
        raise IsolationValidationError("network_invalid", "provider-proxy mode requires an explicit internal network name")
    if spec.network_mode == "none" and spec.network_name:
        raise IsolationValidationError("network_invalid", "none network mode must not name a network")
    if spec.network_name and not SAFE_IDENTIFIER_RE.fullmatch(spec.network_name):
        raise IsolationValidationError("network_invalid", "network name must be a lowercase safe identifier")
    if spec.provider_env_key is not None and not ENV_KEY_RE.fullmatch(spec.provider_env_key):
        raise IsolationValidationError("provider_env_key_invalid", "provider env key must be an uppercase identifier")
    if spec.credential_path is not None and spec.provider_env_key is None:
        raise IsolationValidationError("credential_contract_invalid", "a credential mount requires its provider env key")
    if spec.provider_env_key is not None and spec.credential_path is None:
        raise IsolationValidationError("credential_contract_invalid", "a provider env key requires a credential mount")

    workspace = _assert_no_link_components(spec.workspace, "workspace")
    profile = _assert_no_link_components(spec.profile_path, "profile")
    output = _assert_no_link_components(spec.output_exchange, "output exchange")
    credential = _assert_no_link_components(spec.credential_path, "credential") if spec.credential_path else None
    if not workspace.is_dir():
        raise IsolationValidationError("workspace_invalid", "worker workspace must be a directory")
    if not profile.is_file():
        raise IsolationValidationError("profile_invalid", "worker profile must be a regular file")
    if not output.is_dir():
        raise IsolationValidationError("output_invalid", "worker output exchange must be a directory")
    if any(output.iterdir()):
        raise IsolationValidationError("output_not_empty", "worker output exchange must be empty before launch")
    if credential is not None and not credential.is_file():
        raise IsolationValidationError("credential_invalid", "worker credential must be a regular file")

    sources = [("workspace", workspace), ("profile", profile), ("output", output)]
    if credential is not None:
        sources.append(("credential", credential))
    for index, (label, path) in enumerate(sources):
        for other_label, other in sources[index + 1 :]:
            if _overlap(path, other):
                raise IsolationValidationError(
                    "mount_overlap",
                    f"{label} and {other_label} mount sources must be disjoint",
                )
    for raw_forbidden in spec.forbidden_mount_roots:
        forbidden = _assert_no_link_components(raw_forbidden, "forbidden mount root")
        for label, path in sources:
            if _overlap(path, forbidden):
                raise IsolationValidationError(
                    "forbidden_mount_overlap",
                    f"{label} overlaps a forbidden host root",
                )
    return {"workspace": workspace, "profile": profile, "output": output, "credential": credential}


class OciCliBackend:
    """Docker/Podman CLI adapter with a mandatory security canary."""

    SECURITY_FLAGS = (
        "read-only-rootfs",
        "cap-drop-all",
        "no-new-privileges",
        "non-root-user",
        "pids-limit",
        "memory-limit",
        "cpu-limit",
        "tmpfs-only-home",
        "no-engine-socket",
    )

    def __init__(
        self,
        engine: Literal["docker", "podman"],
        *,
        runner: CommandRunner = subprocess_command_runner,
        executable: str | None = None,
        host_system: str | None = None,
    ) -> None:
        if engine not in {"docker", "podman"}:
            raise ValueError("OCI engine must be docker or podman")
        self.kind = engine
        self.executable = executable or engine
        self.runner = runner
        self.host_system = host_system or platform.system()

    def _engine_metadata(self, timeout: float) -> tuple[str, str, str, bool | None]:
        if self.kind == "docker":
            version = _json_object(
                _run_checked(
                    self.runner,
                    [self.executable, "version", "--format", "{{json .}}"],
                    timeout=timeout,
                    label="docker version",
                ),
                "docker version",
            )
            server = version.get("Server") if isinstance(version.get("Server"), dict) else {}
            system = str(server.get("Os") or server.get("OS") or "").lower()
            engine_version = str(server.get("Version") or version.get("ServerVersion") or "")
            context = _json_object(
                _run_checked(
                    self.runner,
                    [self.executable, "context", "inspect", "--format", "{{json .}}"],
                    timeout=timeout,
                    label="docker context inspect",
                ),
                "docker context inspect",
            )
            endpoints = context.get("Endpoints") if isinstance(context.get("Endpoints"), dict) else {}
            docker_endpoint = endpoints.get("docker") if isinstance(endpoints.get("docker"), dict) else {}
            endpoint = str(docker_endpoint.get("Host") or context.get("DockerEndpoint") or "")
            if not endpoint:
                raise IsolationUnavailableError(
                    "engine_endpoint_unknown",
                    "Docker context did not identify a local engine endpoint",
                )
            if not endpoint.startswith(("unix://", "npipe://")):
                raise IsolationUnavailableError("remote_engine_rejected", "remote Docker contexts are not trusted by default")
            return engine_version, endpoint, system, None

        info = _json_object(
            _run_checked(
                self.runner,
                [self.executable, "info", "--format", "json"],
                timeout=timeout,
                label="podman info",
            ),
            "podman info",
        )
        host = info.get("host") if isinstance(info.get("host"), dict) else {}
        version_row = info.get("version") if isinstance(info.get("version"), dict) else {}
        security = host.get("security") if isinstance(host.get("security"), dict) else {}
        system = str(host.get("os") or "").lower()
        engine_version = str(version_row.get("Version") or info.get("version") or "")
        rootless = security.get("rootless") if isinstance(security.get("rootless"), bool) else None
        is_remote = bool(info.get("remote") or (isinstance(info.get("client"), dict) and info["client"].get("remote")))
        endpoint = "local"
        if self.host_system.lower() == "windows":
            machine = _json_object(
                _run_checked(
                    self.runner,
                    [self.executable, "machine", "inspect", "--format", "json"],
                    timeout=timeout,
                    label="podman machine inspect",
                ),
                "podman machine inspect",
            )
            state = str(machine.get("State") or machine.get("state") or "").lower()
            if state not in {"running", "start"}:
                raise IsolationUnavailableError("podman_machine_unavailable", "Podman machine is not running")
            endpoint = "podman-machine"
        elif is_remote:
            raise IsolationUnavailableError("remote_engine_rejected", "remote Podman connections are not trusted by default")
        return engine_version, endpoint, system, rootless

    def _verify_image(self, spec: WorkerExecutionSpec) -> None:
        output = _run_checked(
            self.runner,
            [self.executable, "image", "inspect", "--format", "{{json .}}", spec.image],
            timeout=spec.limits.timeout_seconds,
            label=f"{self.kind} image inspect",
        )
        payload = _json_object(output, f"{self.kind} image inspect")
        candidates: set[str] = set()
        for key in ("Digest", "digest", "Id", "ID"):
            if isinstance(payload.get(key), str):
                candidates.add(str(payload[key]))
        for key in ("RepoDigests", "repoDigests"):
            if isinstance(payload.get(key), list):
                candidates.update(str(item) for item in payload[key])
        if spec.image not in candidates and spec.image_digest not in candidates and not any(
            item.endswith("@" + spec.image_digest) for item in candidates
        ):
            raise IsolationUnavailableError("image_digest_mismatch", "local worker image does not match the pinned digest")

    def _base_argv(self, spec: WorkerExecutionSpec, paths: Mapping[str, Path | None]) -> list[str]:
        limits = spec.limits
        argv = [
            self.executable,
            "run",
            "--rm",
            "--pull",
            "never",
            "--read-only",
            "--cap-drop",
            "ALL",
            "--security-opt",
            "no-new-privileges",
            "--pids-limit",
            str(limits.pids),
            "--memory",
            f"{limits.memory_mb}m",
            "--cpus",
            format(limits.cpus, "g"),
            "--init",
            "--user",
            "65532:65532",
            "--tmpfs",
            f"/tmp:rw,nosuid,nodev,noexec,size={limits.tmpfs_mb}m",
            "--tmpfs",
            f"/home/worker/.codex:rw,nosuid,nodev,noexec,size={limits.home_tmpfs_mb}m",
            "--mount",
            _mount_arg(paths["workspace"], "/workspace", spec.workspace_mode),
            "--mount",
            _mount_arg(paths["output"], "/out", "rw"),
            "--workdir",
            "/workspace",
            "--network",
            spec.network_name if spec.network_mode == "provider-proxy" else spec.network_mode,
        ]
        if self.kind == "podman" and self.host_system.lower() != "windows":
            argv.extend(["--userns", "keep-id"])
        return argv

    def build_run_argv(self, spec: WorkerExecutionSpec, command: Sequence[str]) -> list[str]:
        """Build a live worker command without reading credential contents."""

        paths = validate_execution_spec(spec)
        argv = self._base_argv(spec, paths)
        argv.extend(["--mount", _mount_arg(paths["profile"], "/bootstrap/profile.config.toml", "ro")])
        if paths["credential"] is not None:
            argv.extend(["--mount", _mount_arg(paths["credential"], "/run/secrets/provider", "ro")])
            argv.extend(["--env", f"COSTMARSHAL_PROVIDER_ENV_KEY={spec.provider_env_key}"])
            argv.extend(["--env", "COSTMARSHAL_PROVIDER_SECRET_FILE=/run/secrets/provider"])
        argv.extend(
            [
                "--env",
                "CODEX_HOME=/home/worker/.codex",
                "--env",
                "COSTMARSHAL_PROFILE_PATH=/bootstrap/profile.config.toml",
                "--env",
                "COSTMARSHAL_OUTPUT_PATH=/out/final.md",
                spec.image,
                *list(command),
            ]
        )
        return argv

    def build_canary_argv(self, spec: WorkerExecutionSpec) -> list[str]:
        """Build the trusted image canary command; credentials are never mounted."""

        paths = validate_execution_spec(spec)
        argv = self._base_argv(spec, paths)
        argv.extend(
            [
                "--env",
                f"COSTMARSHAL_EXPECT_WORKSPACE_MODE={spec.workspace_mode}",
                spec.image,
                "costmarshal-isolation-canary",
                "--json",
            ]
        )
        return argv

    def _run_canary(self, spec: WorkerExecutionSpec) -> dict[str, Any]:
        payload = _json_object(
            _run_checked(
                self.runner,
                self.build_canary_argv(spec),
                timeout=spec.limits.timeout_seconds,
                label=f"{self.kind} isolation canary",
            ),
            f"{self.kind} isolation canary",
        )
        expected_writable = spec.workspace_mode == "rw"
        checks = {
            "schema": payload.get("schema") == CANARY_SCHEMA,
            "non_root": isinstance(payload.get("uid"), int) and payload["uid"] != 0,
            "capabilities_dropped": str(payload.get("cap_eff") or "").strip("0") == "",
            "no_new_privileges": payload.get("no_new_privileges") is True,
            "rootfs_read_only": payload.get("rootfs_write_blocked") is True,
            "workspace_readable": payload.get("workspace_readable") is True,
            "workspace_mode": payload.get("workspace_writable") is expected_writable,
            "output_writable": payload.get("output_writable") is True,
            "runtime_hidden": payload.get("runtime_visible") is False,
            "engine_socket_hidden": payload.get("engine_socket_visible") is False,
        }
        failed = sorted(key for key, ok in checks.items() if not ok)
        if failed:
            raise IsolationUnavailableError(
                "isolation_canary_failed",
                "OCI worker isolation canary did not attest the requested boundary",
                details={"failed": failed},
            )
        return payload

    def preflight(self, spec: WorkerExecutionSpec) -> IsolationAttestation:
        if spec.isolation_mode != "required":
            raise IsolationValidationError("isolation_mode_mismatch", "OCI backends require isolation_mode=required")
        if spec.engine not in {"auto", self.kind}:
            raise IsolationValidationError("engine_mismatch", f"execution spec requested {spec.engine}, not {self.kind}")
        paths = validate_execution_spec(spec)
        version, endpoint, system, rootless = self._engine_metadata(spec.limits.timeout_seconds)
        if system != "linux":
            code = "windows_container_mode_rejected" if self.host_system.lower() == "windows" else "engine_platform_rejected"
            raise IsolationUnavailableError(code, "CostMarshal worker isolation requires a Linux container engine")
        self._verify_image(spec)
        canary = self._run_canary(spec)
        mounts = [
            MountAttestation("/workspace", spec.workspace_mode, "workspace"),
            MountAttestation("/bootstrap/profile.config.toml", "ro", "profile"),
            MountAttestation("/out", "rw", "output"),
        ]
        if paths["credential"] is not None:
            mounts.append(MountAttestation("/run/secrets/provider", "ro", "credential"))
        return IsolationAttestation(
            schema="costmarshal-worker-isolation-attestation-v1",
            backend=self.kind,
            engine_version=version,
            endpoint=endpoint,
            platform=system,
            rootless=rootless,
            image=spec.image,
            image_digest=spec.image_digest,
            strong_isolation=True,
            security_flags=self.SECURITY_FLAGS,
            mounts=tuple(mounts),
            canary=tuple(sorted(canary.items())),
        )


def _mount_arg(source: Path | None, target: str, mode: Literal["ro", "rw"]) -> str:
    if source is None:
        raise IsolationValidationError("mount_source_missing", f"mount source for {target} is missing")
    suffix = ",readonly" if mode == "ro" else ""
    return f"type=bind,src={source},dst={target}{suffix}"


class UnsafeNativeBackend:
    """Explicit compatibility escape hatch; never considered by required mode."""

    kind = "unsafe-native"

    def __init__(self, *, project_opt_in: bool, dispatch_opt_in: bool) -> None:
        self.project_opt_in = bool(project_opt_in)
        self.dispatch_opt_in = bool(dispatch_opt_in)

    def preflight(self, spec: WorkerExecutionSpec) -> IsolationAttestation:
        if spec.isolation_mode != "unsafe-native":
            raise IsolationUnavailableError(
                "native_fallback_forbidden",
                "unsafe native execution is never a fallback for required isolation",
            )
        if not self.project_opt_in or not self.dispatch_opt_in:
            raise IsolationUnavailableError(
                "unsafe_native_double_opt_in_required",
                "unsafe native workers require project-level and dispatch-level explicit opt-in",
            )
        validate_execution_spec(spec)
        return IsolationAttestation(
            schema="costmarshal-worker-isolation-attestation-v1",
            backend=self.kind,
            engine_version="host",
            endpoint="host",
            platform=platform.system().lower(),
            rootless=None,
            image=spec.image,
            image_digest=spec.image_digest,
            strong_isolation=False,
            security_flags=(),
            mounts=(),
            canary=(),
            warnings=("native workers can read host files and are not strongly isolated",),
        )


def select_worker_isolation_backend(
    spec: WorkerExecutionSpec,
    *,
    docker: WorkerIsolationBackend | None = None,
    podman: WorkerIsolationBackend | None = None,
    unsafe_native: UnsafeNativeBackend | None = None,
) -> SelectedBackend:
    """Select an attested backend without ever silently weakening isolation."""

    if spec.isolation_mode == "unsafe-native":
        if unsafe_native is None:
            raise IsolationUnavailableError("unsafe_native_unavailable", "unsafe native backend was not explicitly configured")
        return SelectedBackend(unsafe_native, unsafe_native.preflight(spec))

    candidates: list[WorkerIsolationBackend] = []
    if spec.engine in {"auto", "docker"} and docker is not None:
        candidates.append(docker)
    if spec.engine in {"auto", "podman"} and podman is not None:
        candidates.append(podman)
    failures: list[dict[str, str]] = []
    for backend in candidates:
        try:
            return SelectedBackend(backend, backend.preflight(spec))
        except IsolationError as exc:
            failures.append({"backend": backend.kind, "code": exc.code, "message": str(exc)})
    raise IsolationUnavailableError(
        "required_isolation_unavailable",
        "no OCI backend could attest required worker isolation; native fallback is forbidden",
        details={"failures": failures},
    )


__all__ = [
    "CANARY_SCHEMA",
    "CommandResult",
    "IsolationAttestation",
    "IsolationError",
    "IsolationUnavailableError",
    "IsolationValidationError",
    "MountAttestation",
    "OciCliBackend",
    "ResourceLimits",
    "SelectedBackend",
    "UnsafeNativeBackend",
    "WorkerExecutionSpec",
    "WorkerIsolationBackend",
    "select_worker_isolation_backend",
    "subprocess_command_runner",
    "validate_execution_spec",
]
