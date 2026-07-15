"""Fail-closed worker execution isolation contracts.

This module is deliberately independent from the scheduler and actor runner.
It builds and verifies OCI execution plans without putting secret *values* in
command arguments.  Docker/Podman process supervision remains a separate
concern from CostMarshal's local/tmux actor-session backends.
"""

from __future__ import annotations

import contextlib
import hashlib
import hmac
import json
import math
import os
import platform
import re
import stat
import subprocess
import tempfile
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, BinaryIO, Callable, Literal, Mapping, Protocol, Sequence


IMAGE_DIGEST_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/-]*@sha256:([0-9a-f]{64})\Z")
ENV_KEY_RE = re.compile(r"[A-Z_][A-Z0-9_]{0,127}\Z")
SAFE_IDENTIFIER_RE = re.compile(r"[A-Za-z0-9]+(?:[-_.][A-Za-z0-9]+)*\Z")
CANARY_SCHEMA = "costmarshal-worker-isolation-canary-v1"
OUTPUT_MAX_BYTES = 1024 * 1024
STDOUT_JSONL_MAX_BYTES = 1024 * 1024
STDERR_CAPTURE_MAX_BYTES = 64 * 1024


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
    credential_cleanup: Literal["preserve", "delete-after-use"] = "preserve"
    credential_temp_root: Path | None = None
    isolation_mode: Literal["required", "unsafe-native"] = "required"
    engine: Literal["auto", "docker", "podman"] = "auto"
    network_mode: Literal["none", "provider-proxy"] = "none"
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
    network_policy: tuple[tuple[str, Any], ...] = ()
    probe_provenance: str = "image-internal-digest-bound-scratch-no-network"
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
            "network_policy": dict(self.network_policy),
            "probe_provenance": self.probe_provenance,
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


def validate_execution_spec(
    spec: WorkerExecutionSpec,
    *,
    require_empty_output: bool = True,
) -> dict[str, Path | None]:
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
    if spec.network_mode not in {"none", "provider-proxy"}:
        raise IsolationValidationError(
            "network_invalid",
            "required worker network mode must be none or provider-proxy; bridge is forbidden",
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
    if spec.credential_cleanup not in {"preserve", "delete-after-use"}:
        raise IsolationValidationError("credential_cleanup_invalid", "credential cleanup mode is invalid")
    if spec.credential_cleanup == "delete-after-use" and (
        spec.credential_path is None or spec.credential_temp_root is None
    ):
        raise IsolationValidationError(
            "credential_cleanup_invalid",
            "temporary credential cleanup requires a credential path and temp root",
        )
    if spec.credential_cleanup == "preserve" and spec.credential_temp_root is not None:
        raise IsolationValidationError(
            "credential_cleanup_invalid",
            "credential temp root is only valid with delete-after-use",
        )

    workspace = _assert_no_link_components(spec.workspace, "workspace")
    profile = _assert_no_link_components(spec.profile_path, "profile")
    output = _assert_no_link_components(spec.output_exchange, "output exchange")
    credential = _assert_no_link_components(spec.credential_path, "credential") if spec.credential_path else None
    credential_temp_root = (
        _assert_no_link_components(spec.credential_temp_root, "credential temp root")
        if spec.credential_temp_root
        else None
    )
    if not workspace.is_dir():
        raise IsolationValidationError("workspace_invalid", "worker workspace must be a directory")
    if not profile.is_file():
        raise IsolationValidationError("profile_invalid", "worker profile must be a regular file")
    if not output.is_dir():
        raise IsolationValidationError("output_invalid", "worker output exchange must be a directory")
    if require_empty_output and any(output.iterdir()):
        raise IsolationValidationError("output_not_empty", "worker output exchange must be empty before launch")
    if credential is not None and not credential.is_file():
        raise IsolationValidationError("credential_invalid", "worker credential must be a regular file")
    if credential_temp_root is not None:
        if not credential_temp_root.is_dir():
            raise IsolationValidationError("credential_cleanup_invalid", "credential temp root must be a directory")
        try:
            credential.relative_to(credential_temp_root)  # type: ignore[union-attr]
        except ValueError as exc:
            raise IsolationValidationError(
                "credential_cleanup_invalid",
                "temporary credential must be contained by its declared temp root",
            ) from exc

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

    def _container_user(self) -> tuple[int, int]:
        uid = 65532
        gid = 65532
        if self.host_system.lower() != "windows" and hasattr(os, "getuid") and os.getuid() > 0:
            uid = os.getuid()
            gid = os.getgid()
        return uid, gid

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
        self._docker_endpoint: str | None = None

    def _engine_argv(self, *args: str) -> list[str]:
        if self.kind == "docker":
            if self._docker_endpoint is None:
                raise IsolationUnavailableError(
                    "docker_endpoint_unverified",
                    "Docker command refused before a local endpoint was verified",
                )
            return [self.executable, "--host", self._docker_endpoint, *args]
        return [self.executable, *args]

    def _engine_metadata(self, timeout: float) -> tuple[str, str, str, bool | None]:
        if self.kind == "docker":
            context_name = _run_checked(
                self.runner,
                [self.executable, "context", "show"],
                timeout=timeout,
                label="docker context show",
            ).strip()
            if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", context_name):
                raise IsolationUnavailableError("docker_context_invalid", "Docker selected an invalid context name")
            context = _json_object(
                _run_checked(
                    self.runner,
                    [
                        self.executable,
                        "context",
                        "inspect",
                        context_name,
                        "--format",
                        "{{json .}}",
                    ],
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
            # Pin the already-verified local endpoint. Re-resolving a mutable
            # context name for every lifecycle command creates a TOCTOU window.
            self._docker_endpoint = endpoint
            version = _json_object(
                _run_checked(
                    self.runner,
                    self._engine_argv("version", "--format", "{{json .}}"),
                    timeout=timeout,
                    label="docker version",
                ),
                "docker version",
            )
            server = version.get("Server") if isinstance(version.get("Server"), dict) else {}
            system = str(server.get("Os") or server.get("OS") or "").lower()
            engine_version = str(server.get("Version") or version.get("ServerVersion") or "")
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
            self._engine_argv("image", "inspect", "--format", "{{json .}}", spec.image),
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
        config = payload.get("Config") if isinstance(payload.get("Config"), dict) else None
        if config is None or config.get("Entrypoint") not in (None, [], ""):
            raise IsolationUnavailableError(
                "image_entrypoint_rejected",
                "reviewed worker image must declare an empty entrypoint",
            )

    def _verify_network(self, spec: WorkerExecutionSpec) -> dict[str, Any]:
        if spec.network_mode == "none":
            return {"mode": "none", "external_egress": False, "verified": True}
        assert spec.network_name is not None
        payload = _json_object(
            _run_checked(
                self.runner,
                self._engine_argv(
                    "network",
                    "inspect",
                    "--format",
                    "{{json .}}",
                    spec.network_name,
                ),
                timeout=spec.limits.timeout_seconds,
                label=f"{self.kind} provider proxy network inspect",
            ),
            f"{self.kind} provider proxy network inspect",
        )
        internal = payload.get("Internal", payload.get("internal"))
        labels = payload.get("Labels", payload.get("labels"))
        if internal is not True:
            raise IsolationUnavailableError(
                "provider_proxy_network_not_internal",
                "provider-proxy network must be engine-attested as internal",
            )
        if not isinstance(labels, dict) or labels.get("io.costmarshal.provider-proxy") != "true":
            raise IsolationUnavailableError(
                "provider_proxy_network_untrusted",
                "provider-proxy network is missing its CostMarshal trust label",
            )
        network_id = str(payload.get("Id") or payload.get("ID") or payload.get("id") or "")
        if not re.fullmatch(r"[0-9a-f]{12,64}", network_id):
            raise IsolationUnavailableError(
                "provider_proxy_network_id_invalid",
                "provider-proxy network did not expose an immutable engine ID",
            )
        return {
            "mode": "provider-proxy",
            "name": spec.network_name,
            "internal": True,
            "trust_label": True,
            "external_egress": False,
            "verified": True,
            "network_id": network_id,
        }

    def _base_argv(
        self,
        spec: WorkerExecutionSpec,
        paths: Mapping[str, Path | None],
        *,
        auto_remove: bool = True,
        container_name: str | None = None,
        labels: Mapping[str, str] | None = None,
        network_arg: str | None = None,
    ) -> list[str]:
        limits = spec.limits
        container_uid, container_gid = self._container_user()
        argv = [
            *self._engine_argv("run"),
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
            "--log-driver",
            "local" if self.kind == "docker" else "k8s-file",
            "--user",
            f"{container_uid}:{container_gid}",
            "--tmpfs",
            f"/tmp:rw,nosuid,nodev,noexec,size={limits.tmpfs_mb}m",
            "--tmpfs",
            f"/home/worker/.codex:rw,nosuid,nodev,noexec,uid={container_uid},gid={container_gid},mode=0700,size={limits.home_tmpfs_mb}m",
            "--mount",
            _mount_arg(paths["workspace"], "/workspace", spec.workspace_mode),
            "--mount",
            _mount_arg(paths["output"], "/out", "rw"),
            "--workdir",
            "/workspace",
            "--network",
            network_arg or spec.network_mode,
        ]
        if auto_remove:
            argv.insert(len(self._engine_argv("run")), "--rm")
        if container_name is not None:
            if not SAFE_IDENTIFIER_RE.fullmatch(container_name) or len(container_name) > 128:
                raise IsolationValidationError("container_name_invalid", "managed container name is invalid")
            argv.extend(["--name", container_name])
        for key, value in sorted((labels or {}).items()):
            if not re.fullmatch(r"[a-z0-9][a-z0-9_.-]{0,127}", key):
                raise IsolationValidationError("container_label_invalid", "managed container label key is invalid")
            if not SAFE_IDENTIFIER_RE.fullmatch(value) or len(value) > 128:
                raise IsolationValidationError("container_label_invalid", "managed container label value is invalid")
            argv.extend(["--label", f"{key}={value}"])
        if self.kind == "podman" and self.host_system.lower() != "windows":
            argv.extend(["--userns", "keep-id"])
        return argv

    def build_run_argv(
        self,
        spec: WorkerExecutionSpec,
        command: Sequence[str],
        *,
        auto_remove: bool = True,
        container_name: str | None = None,
        labels: Mapping[str, str] | None = None,
        network_id: str | None = None,
    ) -> list[str]:
        """Build a live worker command without reading credential contents."""

        paths = validate_execution_spec(spec)
        if spec.network_mode == "provider-proxy":
            if not network_id or not re.fullmatch(r"[0-9a-f]{12,64}", network_id):
                raise IsolationValidationError(
                    "provider_proxy_network_unverified",
                    "managed worker requires the immutable verified provider-proxy network ID",
                )
            network_arg = network_id
        else:
            network_arg = "none"
        argv = self._base_argv(
            spec,
            paths,
            auto_remove=auto_remove,
            container_name=container_name,
            labels=labels,
            network_arg=network_arg,
        )
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
                "--env",
                f"COSTMARSHAL_WORKSPACE_MODE={spec.workspace_mode}",
                spec.image,
                *list(command),
            ]
        )
        return argv

    def build_canary_argv(
        self,
        spec: WorkerExecutionSpec,
        scratch_workspace: Path,
        scratch_output: Path,
    ) -> list[str]:
        """Build the digest-bound canary against host scratch mounts and no network."""

        paths: dict[str, Path | None] = {
            "workspace": scratch_workspace,
            "output": scratch_output,
            "profile": None,
            "credential": None,
        }
        argv = self._base_argv(spec, paths, network_arg="none")
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
        with tempfile.TemporaryDirectory(prefix="costmarshal-oci-canary-") as raw_scratch:
            scratch = Path(raw_scratch)
            scratch_workspace = scratch / "workspace"
            scratch_output = scratch / "out"
            scratch_workspace.mkdir()
            scratch_output.mkdir()
            with contextlib.suppress(OSError):
                scratch.chmod(0o711)
                scratch_workspace.chmod(0o777)
                scratch_output.chmod(0o777)
            payload = _json_object(
                _run_checked(
                    self.runner,
                    self.build_canary_argv(spec, scratch_workspace, scratch_output),
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
            "aggregate_secrets_hidden": payload.get("aggregate_secrets_visible") is False,
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

    def _preflight(self, spec: WorkerExecutionSpec, *, require_empty_output: bool) -> IsolationAttestation:
        if spec.isolation_mode != "required":
            raise IsolationValidationError("isolation_mode_mismatch", "OCI backends require isolation_mode=required")
        if spec.engine not in {"auto", self.kind}:
            raise IsolationValidationError("engine_mismatch", f"execution spec requested {spec.engine}, not {self.kind}")
        paths = validate_execution_spec(spec, require_empty_output=require_empty_output)
        version, endpoint, system, rootless = self._engine_metadata(spec.limits.timeout_seconds)
        if system != "linux":
            code = "windows_container_mode_rejected" if self.host_system.lower() == "windows" else "engine_platform_rejected"
            raise IsolationUnavailableError(code, "CostMarshal worker isolation requires a Linux container engine")
        network_policy = self._verify_network(spec)
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
            network_policy=tuple(sorted(network_policy.items())),
            probe_provenance="image-internal-digest-bound-scratch-no-network",
            warnings=(
                "canary code is digest-bound inside the worker image; cryptographic build provenance is required externally",
            ),
        )

    def preflight(self, spec: WorkerExecutionSpec) -> IsolationAttestation:
        return self._preflight(spec, require_empty_output=True)

    def preflight_existing(self, spec: WorkerExecutionSpec) -> IsolationAttestation:
        """Revalidate engine/image/network for recovery without requiring an empty exchange."""

        return self._preflight(spec, require_empty_output=False)


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


class WorkerExecutionError(IsolationError):
    """A managed worker lifecycle operation failed without exposing worker output."""


@dataclass(frozen=True)
class CapturedProcessResult:
    exit_code: int
    stdout: bytes = b""
    stderr: bytes = b""
    timed_out: bool = False
    stdout_truncated: bool = False
    stderr_truncated: bool = False


class ManagedProcess(Protocol):
    pid: int

    def communicate_bounded(
        self,
        input_bytes: bytes,
        *,
        timeout: float,
        max_stdout_bytes: int,
        max_stderr_bytes: int,
    ) -> CapturedProcessResult:
        ...

    def poll(self) -> int | None:
        ...

    def kill(self) -> None:
        ...


class _SubprocessManagedProcess:
    """Bounded binary pipe adapter for the production subprocess implementation."""

    def __init__(self, process: subprocess.Popen[bytes]) -> None:
        self._process = process
        self.pid = process.pid

    def communicate_bounded(
        self,
        input_bytes: bytes,
        *,
        timeout: float,
        max_stdout_bytes: int,
        max_stderr_bytes: int,
    ) -> CapturedProcessResult:
        stdout_parts: list[bytes] = []
        stderr_parts: list[bytes] = []
        stdout_size = [0]
        stderr_size = [0]
        stdout_overflow = threading.Event()
        stderr_overflow = threading.Event()

        def drain(stream: Any, parts: list[bytes], size: list[int], limit: int, overflow: threading.Event) -> None:
            if stream is None:
                return
            while True:
                chunk = stream.read(8192)
                if not chunk:
                    return
                remaining = max(0, limit - size[0])
                if remaining:
                    parts.append(chunk[:remaining])
                    size[0] += min(len(chunk), remaining)
                if len(chunk) > remaining:
                    overflow.set()
                    try:
                        self._process.kill()
                    except OSError:
                        pass
                    return

        stdout_thread = threading.Thread(
            target=drain,
            args=(self._process.stdout, stdout_parts, stdout_size, max_stdout_bytes, stdout_overflow),
            daemon=True,
        )
        stderr_thread = threading.Thread(
            target=drain,
            args=(self._process.stderr, stderr_parts, stderr_size, max_stderr_bytes, stderr_overflow),
            daemon=True,
        )
        stdout_thread.start()
        stderr_thread.start()
        try:
            if self._process.stdin is not None:
                try:
                    self._process.stdin.write(input_bytes)
                    self._process.stdin.flush()
                except (BrokenPipeError, OSError):
                    pass
                finally:
                    self._process.stdin.close()
            timed_out = False
            try:
                exit_code = self._process.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                timed_out = True
                self._process.kill()
                exit_code = self._process.wait()
        finally:
            stdout_thread.join(timeout=5)
            stderr_thread.join(timeout=5)
        return CapturedProcessResult(
            exit_code=exit_code,
            stdout=b"".join(stdout_parts),
            stderr=b"".join(stderr_parts),
            timed_out=timed_out,
            stdout_truncated=stdout_overflow.is_set(),
            stderr_truncated=stderr_overflow.is_set(),
        )

    def poll(self) -> int | None:
        return self._process.poll()

    def kill(self) -> None:
        self._process.kill()


class _DetachedManagedProcess:
    """Lifecycle-only placeholder for a container recovered by its durable identity."""

    pid = 0

    def communicate_bounded(
        self,
        input_bytes: bytes,
        *,
        timeout: float,
        max_stdout_bytes: int,
        max_stderr_bytes: int,
    ) -> CapturedProcessResult:
        raise WorkerExecutionError("worker_handle_detached", "a recovered container cannot be waited through its old client")

    def poll(self) -> int | None:
        return 0

    def kill(self) -> None:
        return None


def subprocess_process_factory(argv: Sequence[str], stdin_source: BinaryIO) -> ManagedProcess:
    process = subprocess.Popen(
        list(argv),
        stdin=stdin_source,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        shell=False,
    )
    return _SubprocessManagedProcess(process)


@dataclass(frozen=True)
class ContainerInspection:
    container_name: str
    container_id: str
    status: str
    exit_code: int | None
    labels_verified: bool = True


@dataclass(frozen=True)
class WorkerWaitReceipt:
    container_name: str
    backend: str
    exit_code: int
    stdout_events: tuple[Mapping[str, Any], ...]
    stdout_bytes: int
    stderr_bytes: int
    stderr_truncated: bool


@dataclass(frozen=True)
class CredentialCleanupReceipt:
    credential_id: str | None
    requested: bool
    deleted: bool
    bytes_removed: int


@dataclass(frozen=True)
class ExecutionCleanupReceipt:
    container_name: str
    container_removed: bool
    credential: CredentialCleanupReceipt
    identity_drift: tuple[str, ...] = ()


@dataclass(frozen=True)
class ValidatedWorkerOutput:
    text: str
    utf8_bytes: int
    sha256: str


@dataclass
class WorkerExecutionHandle:
    spec: WorkerExecutionSpec = field(repr=False)
    backend: OciCliBackend = field(repr=False)
    process: ManagedProcess = field(repr=False)
    container_name: str
    container_id: str
    labels: Mapping[str, str] = field(repr=False)
    command: tuple[str, ...] = field(repr=False)
    network_id: str | None = field(repr=False)
    attestation: IsolationAttestation
    _stdin_prompt: bytes = field(repr=False)
    _secret_values: tuple[str, ...] = field(repr=False)
    recovered: bool = False
    state: Literal["started", "finished", "stopped", "cleaned"] = "started"


def _managed_container_identity(spec: WorkerExecutionSpec) -> tuple[str, dict[str, str]]:
    identity = f"{spec.project_id}\0{spec.actor_id}\0{spec.attempt_id}".encode("utf-8")
    suffix = hashlib.sha256(identity).hexdigest()[:16]
    stem = re.sub(r"[^a-z0-9_.-]+", "-", f"{spec.project_id}-{spec.actor_id}".lower()).strip("-.")
    name = f"costmarshal-{stem[:72]}-{suffix}"
    labels = {
        "io.costmarshal.managed": "true",
        "io.costmarshal.project": spec.project_id,
        "io.costmarshal.actor": spec.actor_id,
        "io.costmarshal.attempt": spec.attempt_id,
        "io.costmarshal.identity": suffix,
    }
    return name, labels


def _collect_string_leaves(value: Any, target: set[str]) -> None:
    if isinstance(value, str):
        if len(value) >= 4:
            target.add(value)
    elif isinstance(value, list):
        for item in value:
            _collect_string_leaves(item, target)
    elif isinstance(value, dict):
        for item in value.values():
            _collect_string_leaves(item, target)


def _credential_secret_values(path: Path | None) -> tuple[str, ...]:
    if path is None:
        return ()
    try:
        payload = path.read_bytes()
    except OSError as exc:
        raise WorkerExecutionError("credential_read_failed", "temporary credential could not be read safely") from exc
    if len(payload) > 256 * 1024:
        raise WorkerExecutionError("credential_too_large", "credential file exceeds 256 KiB")
    try:
        text = payload.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise WorkerExecutionError("credential_encoding_invalid", "credential file must be UTF-8") from exc
    values: set[str] = set()
    stripped = text.strip()
    if len(stripped) >= 4:
        values.add(stripped)
    for line in text.splitlines():
        line = line.strip()
        if len(line) >= 4:
            values.add(line)
        if "=" in line:
            candidate = line.split("=", 1)[1].strip().strip("'\"")
            if len(candidate) >= 4:
                values.add(candidate)
    try:
        _collect_string_leaves(json.loads(text), values)
    except json.JSONDecodeError:
        pass
    return tuple(sorted(values, key=len, reverse=True))


def _redact_text(value: str, secrets: Sequence[str]) -> str:
    for secret in secrets:
        value = value.replace(secret, "[REDACTED]")
    return value


def _redact_json(value: Any, secrets: Sequence[str]) -> Any:
    if isinstance(value, str):
        return _redact_text(value, secrets)
    if isinstance(value, list):
        return [_redact_json(item, secrets) for item in value]
    if isinstance(value, dict):
        return {
            _redact_text(str(key), secrets): _redact_json(item, secrets)
            for key, item in value.items()
        }
    return value


def _credential_identifier(path: Path | None) -> str | None:
    if path is None:
        return None
    return hashlib.sha256(os.fsencode(_lexical_absolute(path))).hexdigest()[:24]


def cleanup_temporary_credential(spec: WorkerExecutionSpec) -> CredentialCleanupReceipt:
    """Delete only an explicitly declared temp credential and return a path-free receipt."""

    identifier = _credential_identifier(spec.credential_path)
    if spec.credential_cleanup == "preserve":
        return CredentialCleanupReceipt(identifier, False, False, 0)
    if spec.credential_path is None or spec.credential_temp_root is None:
        raise WorkerExecutionError("credential_cleanup_failed", "temporary credential path is missing")
    credential = _lexical_absolute(spec.credential_path)
    lexical_root = _lexical_absolute(spec.credential_temp_root)
    temp_root = _assert_no_link_components(spec.credential_temp_root, "credential temp root")
    try:
        relative = credential.relative_to(lexical_root)
    except ValueError as exc:
        raise WorkerExecutionError(
            "credential_cleanup_failed",
            "temporary credential escaped its declared temp root",
        ) from exc
    current = temp_root
    for part in relative.parts[:-1]:
        current = current / part
        try:
            info = current.lstat()
        except FileNotFoundError:
            return CredentialCleanupReceipt(identifier, True, True, 0)
        except OSError as exc:
            raise WorkerExecutionError("credential_cleanup_failed", "temporary credential parent is unreadable") from exc
        if not stat.S_ISDIR(info.st_mode) or _is_reparse_or_link(current):
            raise WorkerExecutionError("credential_cleanup_failed", "temporary credential parent is not a safe directory")
    try:
        info = credential.lstat()
        if not stat.S_ISREG(info.st_mode) or _is_reparse_or_link(credential):
            raise WorkerExecutionError("credential_cleanup_failed", "temporary credential is not a regular file")
        size = info.st_size
        credential.unlink()
    except FileNotFoundError:
        return CredentialCleanupReceipt(identifier, True, True, 0)
    except IsolationError:
        raise
    except OSError as exc:
        raise WorkerExecutionError("credential_cleanup_failed", "temporary credential could not be deleted") from exc
    return CredentialCleanupReceipt(identifier, True, True, size)


def validate_output_exchange(
    output_exchange: Path,
    *,
    max_bytes: int = OUTPUT_MAX_BYTES,
) -> ValidatedWorkerOutput:
    """Validate the single-file host exchange contract and return trusted UTF-8."""

    exchange = _assert_no_link_components(output_exchange, "output exchange")
    if not exchange.is_dir():
        raise IsolationValidationError("output_invalid", "worker output exchange must be a directory")
    try:
        entries = list(exchange.iterdir())
    except OSError as exc:
        raise IsolationValidationError("output_unreadable", "worker output exchange cannot be listed") from exc
    if len(entries) != 1 or entries[0].name != "final.md":
        raise IsolationValidationError(
            "output_contract_invalid",
            "worker output exchange must contain only final.md",
        )
    final_path = _assert_no_link_components(entries[0], "worker final output")
    before = final_path.lstat()
    if not stat.S_ISREG(before.st_mode) or _is_reparse_or_link(final_path):
        raise IsolationValidationError("output_contract_invalid", "final.md must be a regular file")
    try:
        with final_path.open("rb") as stream:
            payload = stream.read(max_bytes + 1)
    except OSError as exc:
        raise IsolationValidationError("output_unreadable", "final.md cannot be read") from exc
    if len(payload) > max_bytes:
        raise IsolationValidationError("output_too_large", f"final.md exceeds {max_bytes} bytes")
    after = final_path.lstat()
    fingerprint_before = (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
    fingerprint_after = (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
    if fingerprint_before != fingerprint_after or after.st_size != len(payload):
        raise IsolationValidationError("output_changed_during_read", "final.md changed while it was validated")
    try:
        text = payload.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise IsolationValidationError("output_encoding_invalid", "final.md must be valid UTF-8") from exc
    return ValidatedWorkerOutput(text=text, utf8_bytes=len(payload), sha256=hashlib.sha256(payload).hexdigest())


class OciWorkerExecutionAdapter:
    """Managed attached OCI execution with verified identity and bounded output."""

    def __init__(
        self,
        backend: OciCliBackend,
        *,
        process_factory: Callable[[Sequence[str], BinaryIO], ManagedProcess] = subprocess_process_factory,
        max_stdout_bytes: int = STDOUT_JSONL_MAX_BYTES,
        max_stderr_bytes: int = STDERR_CAPTURE_MAX_BYTES,
        max_prompt_bytes: int = 2 * 1024 * 1024,
    ) -> None:
        self.backend = backend
        self.process_factory = process_factory
        self.max_stdout_bytes = max_stdout_bytes
        self.max_stderr_bytes = max_stderr_bytes
        self.max_prompt_bytes = max_prompt_bytes
        if min(max_stdout_bytes, max_stderr_bytes, max_prompt_bytes) <= 0:
            raise ValueError("execution adapter byte limits must be positive")

    def _run_lifecycle(self, argv: Sequence[str], *, timeout: float, label: str) -> str:
        try:
            result = self.backend.runner(tuple(argv), timeout=timeout)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise WorkerExecutionError("lifecycle_command_failed", f"{label} could not run") from exc
        if result.returncode != 0:
            raise WorkerExecutionError(
                "lifecycle_command_failed",
                f"{label} failed",
                details={"returncode": result.returncode},
            )
        if len(result.stdout.encode("utf-8", errors="replace")) > 2 * 1024 * 1024:
            raise WorkerExecutionError("lifecycle_output_too_large", f"{label} output exceeded 2 MiB")
        return result.stdout

    def _inspect_payload(self, reference: str, *, timeout: float) -> dict[str, Any]:
        try:
            return _json_object(
                self._run_lifecycle(
                    self.backend._engine_argv("inspect", "--format", "{{json .}}", reference),
                    timeout=timeout,
                    label=f"{self.backend.kind} container inspect",
                ),
                f"{self.backend.kind} container inspect",
            )
        except IsolationUnavailableError as exc:
            raise WorkerExecutionError("container_inspect_invalid", "container inspect output was invalid") from exc

    @staticmethod
    def _same_host_path(actual: str, expected: Path) -> bool:
        try:
            actual_path = Path(actual).resolve(strict=True)
            expected_path = expected.resolve(strict=True)
        except OSError:
            return False
        return os.path.normcase(os.fspath(actual_path)) == os.path.normcase(os.fspath(expected_path))

    def _validate_inspect_contract(
        self,
        handle: WorkerExecutionHandle,
        payload: Mapping[str, Any],
    ) -> ContainerInspection:
        container_id, config = self._validate_container_identity(handle, payload)

        image_candidates = {
            str(config.get("Image") or ""),
            str(payload.get("ImageName") or ""),
        }
        if handle.spec.image not in image_candidates:
            raise WorkerExecutionError("container_image_mismatch", "managed container image reference did not match")
        image_digest = str(payload.get("ImageDigest") or "")
        if image_digest and image_digest != handle.spec.image_digest:
            raise WorkerExecutionError("container_image_mismatch", "managed container image digest did not match")
        if list(config.get("Cmd") or []) != list(handle.command):
            raise WorkerExecutionError("container_command_mismatch", "managed container command did not match")
        if config.get("Entrypoint") not in (None, [], ""):
            raise WorkerExecutionError("container_entrypoint_rejected", "managed worker image must not override its command")
        expected_uid, expected_gid = self.backend._container_user()
        if str(config.get("User") or "") != f"{expected_uid}:{expected_gid}":
            raise WorkerExecutionError("container_user_mismatch", "managed container user did not match")

        host = payload.get("HostConfig") if isinstance(payload.get("HostConfig"), dict) else {}
        security_opt = {str(item).lower() for item in (host.get("SecurityOpt") or [])}
        cap_drop = {str(item).upper() for item in (host.get("CapDrop") or [])}
        cap_add = [item for item in (host.get("CapAdd") or []) if item]
        effective_caps = payload.get("EffectiveCaps")
        cap_drop_all = bool({"ALL", "CAP_ALL"} & cap_drop) or (
            self.backend.kind == "podman" and effective_caps == []
        )
        unsafe = []
        if host.get("ReadonlyRootfs") is not True:
            unsafe.append("read_only_rootfs")
        if not cap_drop_all:
            unsafe.append("cap_drop_all")
        if cap_add:
            unsafe.append("cap_add")
        if not any(item.startswith("no-new-privileges") for item in security_opt):
            unsafe.append("no_new_privileges")
        if host.get("Privileged") is True:
            unsafe.append("privileged")
        if str(host.get("PidMode") or "").lower() == "host":
            unsafe.append("host_pid")
        if str(host.get("IpcMode") or "").lower() == "host":
            unsafe.append("host_ipc")
        raw_pids = host.get("PidsLimit")
        if type(raw_pids) is not int or raw_pids != handle.spec.limits.pids:
            unsafe.append("pids_limit")
        raw_memory = host.get("Memory")
        if type(raw_memory) is not int or raw_memory != handle.spec.limits.memory_mb * 1024 * 1024:
            unsafe.append("memory_limit")
        raw_nano_cpus = host.get("NanoCpus", host.get("NanoCPUs"))
        expected_nano_cpus = int(round(handle.spec.limits.cpus * 1_000_000_000))
        if type(raw_nano_cpus) is not int or raw_nano_cpus != expected_nano_cpus:
            unsafe.append("cpu_limit")
        if host.get("Init") is not True:
            unsafe.append("init_process")
        log_config = host.get("LogConfig") if isinstance(host.get("LogConfig"), dict) else {}
        recoverable_log_drivers = (
            {"local", "json-file"}
            if self.backend.kind == "docker"
            else {"k8s-file", "json-file"}
        )
        if str(log_config.get("Type") or "") not in recoverable_log_drivers:
            unsafe.append("recoverable_log_driver")
        tmpfs = host.get("Tmpfs") if isinstance(host.get("Tmpfs"), dict) else {}

        def tmpfs_matches(path: str, size_mb: int, required: set[str]) -> bool:
            raw = tmpfs.get(path)
            if not isinstance(raw, str):
                return False
            options = {item.strip().lower() for item in raw.split(",") if item.strip()}
            size_options = {f"size={size_mb}m", f"size={size_mb * 1024 * 1024}"}
            return required.issubset(options) and bool(options & size_options)

        expected_uid, expected_gid = self.backend._container_user()
        if set(tmpfs) != {"/tmp", "/home/worker/.codex"}:
            unsafe.append("tmpfs_targets")
        if not tmpfs_matches("/tmp", handle.spec.limits.tmpfs_mb, {"rw", "nosuid", "nodev", "noexec"}):
            unsafe.append("tmpfs_limit")
        if not tmpfs_matches(
            "/home/worker/.codex",
            handle.spec.limits.home_tmpfs_mb,
            {
                "rw",
                "nosuid",
                "nodev",
                "noexec",
                f"uid={expected_uid}",
                f"gid={expected_gid}",
                "mode=0700",
            },
        ):
            unsafe.append("home_tmpfs_limit")
        if unsafe:
            raise WorkerExecutionError(
                "container_security_contract_mismatch",
                "managed container security options did not match",
                details={"checks": sorted(unsafe)},
            )

        paths = validate_execution_spec(handle.spec, require_empty_output=False)
        expected_mounts: dict[str, tuple[Path, bool]] = {
            "/workspace": (paths["workspace"], handle.spec.workspace_mode == "rw"),  # type: ignore[dict-item]
            "/out": (paths["output"], True),  # type: ignore[dict-item]
            "/bootstrap/profile.config.toml": (paths["profile"], False),  # type: ignore[dict-item]
        }
        if paths["credential"] is not None:
            expected_mounts["/run/secrets/provider"] = (paths["credential"], False)  # type: ignore[dict-item]
        actual_mounts: dict[str, Mapping[str, Any]] = {}
        for row in payload.get("Mounts") or []:
            if not isinstance(row, dict):
                raise WorkerExecutionError("container_mount_mismatch", "managed container mount metadata was invalid")
            destination = str(row.get("Destination") or row.get("destination") or "")
            mount_type = str(row.get("Type") or row.get("type") or "").lower()
            if mount_type == "tmpfs" and destination in {"/tmp", "/home/worker/.codex"}:
                continue
            if destination in actual_mounts:
                raise WorkerExecutionError("container_mount_mismatch", "managed container repeated a mount target")
            actual_mounts[destination] = row
        if set(actual_mounts) != set(expected_mounts):
            raise WorkerExecutionError("container_mount_mismatch", "managed container mount targets did not match")
        for destination, (expected_source, expected_rw) in expected_mounts.items():
            row = actual_mounts[destination]
            if str(row.get("Type") or row.get("type") or "").lower() != "bind":
                raise WorkerExecutionError("container_mount_mismatch", "managed container mount type did not match")
            source = str(row.get("Source") or row.get("source") or "")
            if not self._same_host_path(source, expected_source):
                raise WorkerExecutionError("container_mount_mismatch", "managed container mount source did not match")
            raw_rw = row.get("RW", row.get("rw"))
            if not isinstance(raw_rw, bool) or raw_rw is not expected_rw:
                raise WorkerExecutionError("container_mount_mismatch", "managed container mount mode did not match")

        network_mode = str(host.get("NetworkMode") or "")
        networks_row = payload.get("NetworkSettings") if isinstance(payload.get("NetworkSettings"), dict) else {}
        networks = networks_row.get("Networks") if isinstance(networks_row.get("Networks"), dict) else {}
        if handle.spec.network_mode == "none":
            if network_mode != "none" or any(key != "none" for key in networks):
                raise WorkerExecutionError("container_network_mismatch", "managed container was not isolated from networks")
        else:
            if not handle.network_id:
                raise WorkerExecutionError("container_network_mismatch", "verified provider-proxy network ID is missing")
            attached_ids = {
                str(row.get("NetworkID") or row.get("network_id") or "")
                for row in networks.values()
                if isinstance(row, dict)
            }
            if len(networks) != 1 or attached_ids != {handle.network_id}:
                raise WorkerExecutionError("container_network_mismatch", "managed container network ID did not match")

        serialized = json.dumps(payload, sort_keys=True, default=str)
        if any(secret in serialized for secret in handle._secret_values):
            raise WorkerExecutionError("secret_in_container_metadata", "credential material appeared in container metadata")
        state = payload.get("State") if isinstance(payload.get("State"), dict) else {}
        status = str(state.get("Status") or state.get("status") or "unknown").lower()
        raw_exit = state.get("ExitCode", state.get("exitCode"))
        exit_code = raw_exit if isinstance(raw_exit, int) else None
        return ContainerInspection(handle.container_name, container_id, status, exit_code)

    def _validate_container_identity(
        self,
        handle: WorkerExecutionHandle,
        payload: Mapping[str, Any],
    ) -> tuple[str, Mapping[str, Any]]:
        container_id = str(payload.get("Id") or payload.get("ID") or payload.get("id") or "").lower()
        if not re.fullmatch(r"[0-9a-f]{64}", container_id):
            raise WorkerExecutionError("container_id_invalid", "managed container did not expose an immutable ID")
        if handle.container_id and not hmac.compare_digest(container_id, handle.container_id):
            raise WorkerExecutionError("container_id_mismatch", "managed container ID did not match its durable identity")
        config = payload.get("Config") if isinstance(payload.get("Config"), dict) else {}
        actual_labels = config.get("Labels") if isinstance(config.get("Labels"), dict) else {}
        mismatched = sorted(key for key, value in handle.labels.items() if actual_labels.get(key) != value)
        if mismatched:
            raise WorkerExecutionError(
                "container_label_mismatch",
                "managed container identity labels did not match",
                details={"label_keys": mismatched},
            )
        return container_id, config

    def _container_rows(self, *, timeout: float) -> list[Mapping[str, Any]]:
        """Return a complete no-trunc container listing or fail closed."""

        listing = self._run_lifecycle(
            self.backend._engine_argv(
                "ps",
                "--all",
                "--no-trunc",
                "--format",
                "{{json .}}",
            ),
            timeout=timeout,
            label=f"{self.backend.kind} container listing",
        )
        rows: list[Mapping[str, Any]] = []
        for line in listing.splitlines():
            if not line.strip():
                continue
            try:
                payload = json.loads(line)
            except json.JSONDecodeError as exc:
                raise WorkerExecutionError(
                    "container_listing_invalid",
                    "container listing was not valid JSONL",
                ) from exc
            if isinstance(payload, list):
                if not all(isinstance(item, dict) for item in payload):
                    raise WorkerExecutionError(
                        "container_listing_invalid",
                        "container listing contained an invalid row",
                    )
                rows.extend(payload)
            elif isinstance(payload, dict):
                rows.append(payload)
            else:
                raise WorkerExecutionError(
                    "container_listing_invalid",
                    "container listing contained an invalid row",
                )
        return rows

    @staticmethod
    def _listed_container_identity(row: Mapping[str, Any]) -> tuple[str, set[str]]:
        listed_id = str(row.get("ID") or row.get("Id") or row.get("id") or "").lower()
        raw_names = row.get("Names", row.get("Name", row.get("names", row.get("name"))))
        names = raw_names if isinstance(raw_names, list) else [raw_names]
        return listed_id, {str(name or "").lstrip("/") for name in names}

    def _listed_reference(
        self,
        *,
        container_name: str,
        container_id: str | None,
        timeout: float,
    ) -> str | None:
        """Discover an exact deterministic identity, with absence proved by a full list."""

        for row in self._container_rows(timeout=timeout):
            listed_id, names = self._listed_container_identity(row)
            if (container_id and listed_id == container_id) or container_name in names:
                return listed_id if re.fullmatch(r"[0-9a-f]{64}", listed_id) else container_name
        return None

    def _confirm_container_absent(
        self,
        *,
        container_name: str,
        container_id: str | None,
        timeout: float,
    ) -> bool:
        return self._listed_reference(
            container_name=container_name,
            container_id=container_id,
            timeout=timeout,
        ) is None

    def _cleanup_unregistered_start(
        self,
        handle: WorkerExecutionHandle,
        payload: Mapping[str, Any] | None,
    ) -> None:
        """Close a container created by this still-attached run client."""

        container_id = ""
        try:
            try:
                current = payload or self._inspect_payload(
                    handle.container_name,
                    timeout=handle.spec.limits.timeout_seconds,
                )
            except WorkerExecutionError as inspect_error:
                if self._confirm_container_absent(
                    container_name=handle.container_name,
                    container_id=None,
                    timeout=handle.spec.limits.timeout_seconds,
                ):
                    if handle.process.poll() is None:
                        with contextlib.suppress(OSError):
                            handle.process.kill()
                    return
                raise inspect_error
            container_id, _ = self._validate_container_identity(handle, current)
            raw_name = str(current.get("Name") or current.get("Names") or "")
            names = {
                item.lstrip("/")
                for item in ([raw_name] if isinstance(raw_name, str) else list(raw_name))
            }
            if handle.container_name not in names:
                raise WorkerExecutionError(
                    "uncertain_container_identity",
                    "unregistered container identity could not be bound safely",
                )
            try:
                self._run_lifecycle(
                    self.backend._engine_argv("rm", "--force", container_id),
                    timeout=handle.spec.limits.timeout_seconds,
                    label=f"{self.backend.kind} uncertain-start cleanup",
                )
            except WorkerExecutionError as remove_error:
                if not self._confirm_container_absent(
                    container_name=handle.container_name,
                    container_id=container_id,
                    timeout=handle.spec.limits.timeout_seconds,
                ):
                    raise remove_error
            if handle.process.poll() is None:
                with contextlib.suppress(OSError):
                    handle.process.kill()
        except IsolationError as exc:
            raise WorkerExecutionError(
                "uncertain_start_cleanup_failed",
                "managed worker start failed before durable registration and container cleanup was not confirmed",
                details={
                    "container_cleanup_unconfirmed": True,
                    "container_name": handle.container_name,
                    "container_id": container_id or None,
                    "cleanup_error": exc.code,
                },
            ) from exc

    def start(
        self,
        spec: WorkerExecutionSpec,
        command: Sequence[str],
        *,
        stdin_prompt: str,
    ) -> WorkerExecutionHandle:
        if not command or any(not isinstance(item, str) or "\x00" in item for item in command):
            raise IsolationValidationError("worker_command_invalid", "worker command must be a non-empty argv vector")
        prompt = stdin_prompt.encode("utf-8")
        if len(prompt) > self.max_prompt_bytes:
            raise IsolationValidationError("worker_prompt_too_large", "worker stdin prompt exceeds its byte limit")
        attestation = self.backend.preflight(spec)
        container_name, labels = _managed_container_identity(spec)
        secrets = _credential_secret_values(spec.credential_path)
        network_id_value = dict(attestation.network_policy).get("network_id")
        network_id = str(network_id_value) if network_id_value else None
        argv = self.backend.build_run_argv(
            spec,
            command,
            auto_remove=False,
            container_name=container_name,
            labels=labels,
            network_id=network_id,
        )
        if any(secret in argument for secret in secrets for argument in argv):
            raise WorkerExecutionError("secret_in_argv", "credential material was detected in worker argv")
        attestation_json = json.dumps(attestation.to_dict(), sort_keys=True, default=str)
        if any(secret in attestation_json for secret in secrets):
            raise WorkerExecutionError("secret_in_attestation", "credential material was detected in attestation")
        try:
            # The docker/podman client inherits an independent read handle for
            # the immutable prompt snapshot at Popen time.  A runner os._exit
            # after external create therefore cannot turn the worker input into
            # an empty-stdin provider call.
            with tempfile.TemporaryFile(mode="w+b") as prompt_source:
                prompt_source.write(prompt)
                prompt_source.flush()
                prompt_source.seek(0)
                process = self.process_factory(tuple(argv), prompt_source)
        except OSError as exc:
            raise WorkerExecutionError("worker_start_failed", "managed worker process could not start") from exc
        handle = WorkerExecutionHandle(
            spec=spec,
            backend=self.backend,
            process=process,
            container_name=container_name,
            container_id="",
            labels=labels,
            command=tuple(command),
            network_id=network_id,
            attestation=attestation,
            _stdin_prompt=b"",
            _secret_values=secrets,
        )
        deadline = time.monotonic() + min(5.0, spec.limits.timeout_seconds)
        while True:
            payload: dict[str, Any] | None = None
            try:
                payload = self._inspect_payload(container_name, timeout=min(2.0, spec.limits.timeout_seconds))
                inspection = self._validate_inspect_contract(handle, payload)
                handle.container_id = inspection.container_id
                if process.poll() not in {None, 0}:
                    # A competing client for the same deterministic name may
                    # have won the create race after a runner hard-exit.  The
                    # full inspect contract proves this is the same attempt;
                    # detach from the losing client and recover lifecycle by ID.
                    handle.process = _DetachedManagedProcess()
                    handle.recovered = True
                    handle._stdin_prompt = b""
                return handle
            except WorkerExecutionError as exc:
                retryable = exc.code == "lifecycle_command_failed"
                if not retryable or process.poll() is not None or time.monotonic() >= deadline:
                    self._cleanup_unregistered_start(handle, payload)
                    with contextlib.suppress(OSError):
                        process.kill()
                    raise
                time.sleep(0.05)

    def _assert_handle(self, handle: WorkerExecutionHandle) -> None:
        if handle.backend is not self.backend:
            raise WorkerExecutionError("worker_handle_invalid", "worker handle belongs to another backend")
        if handle.state == "cleaned":
            raise WorkerExecutionError("worker_already_cleaned", "worker container has already been cleaned")

    def attach(
        self,
        spec: WorkerExecutionSpec,
        *,
        container_name: str,
        container_id: str | None = None,
        command: Sequence[str],
    ) -> WorkerExecutionHandle:
        """Recover lifecycle control for the deterministic container identity."""

        expected_name, labels = _managed_container_identity(spec)
        if container_name != expected_name:
            raise WorkerExecutionError("container_identity_mismatch", "container name did not match the attempt identity")
        if container_id is not None and not re.fullmatch(r"[0-9a-f]{64}", container_id):
            raise WorkerExecutionError("container_id_invalid", "durable container ID is missing or invalid")
        if not command or any(not isinstance(item, str) or "\x00" in item for item in command):
            raise WorkerExecutionError("container_command_invalid", "durable worker command is missing or invalid")
        attestation = self.backend.preflight_existing(spec)
        network_id_value = dict(attestation.network_policy).get("network_id")
        handle = WorkerExecutionHandle(
            spec=spec,
            backend=self.backend,
            process=_DetachedManagedProcess(),
            container_name=container_name,
            container_id=container_id or "",
            labels=labels,
            command=tuple(command),
            network_id=str(network_id_value) if network_id_value else None,
            attestation=attestation,
            _stdin_prompt=b"",
            _secret_values=_credential_secret_values(spec.credential_path),
            recovered=True,
        )
        reference = container_id or container_name
        payload = self._inspect_payload(reference, timeout=spec.limits.timeout_seconds)
        inspection = self._validate_inspect_contract(handle, payload)
        handle.container_id = inspection.container_id
        return handle

    def recover_or_start(
        self,
        spec: WorkerExecutionSpec,
        command: Sequence[str],
        *,
        container_name: str,
        container_id: str | None = None,
        stdin_prompt: str,
    ) -> WorkerExecutionHandle:
        """Attach before start for a durably prepared deterministic identity."""

        expected_name, _ = _managed_container_identity(spec)
        if container_name != expected_name:
            raise WorkerExecutionError(
                "container_identity_mismatch",
                "prepared container name did not match the attempt identity",
            )
        try:
            return self.attach(
                spec,
                container_name=container_name,
                container_id=container_id,
                command=command,
            )
        except WorkerExecutionError as exc:
            if exc.code != "lifecycle_command_failed":
                raise
        reference = self._listed_reference(
            container_name=container_name,
            container_id=container_id,
            timeout=spec.limits.timeout_seconds,
        )
        if reference is not None:
            return self.attach(
                spec,
                container_name=container_name,
                container_id=(reference if re.fullmatch(r"[0-9a-f]{64}", reference) else None),
                command=command,
            )
        return self.start(spec, command, stdin_prompt=stdin_prompt)

    def cleanup_confirmed_absent(
        self,
        spec: WorkerExecutionSpec,
        *,
        container_name: str,
        container_id: str,
        command: Sequence[str],
    ) -> ExecutionCleanupReceipt:
        """Finish cleanup only after a durable container ID is proved absent.

        This is the recovery path for a crash after ``rm`` succeeded but before
        the stop effect observation committed.  A daemon/inspect error alone is
        never treated as absence; a successful, complete container listing must
        omit both the immutable ID and deterministic name.
        """

        expected_name, _ = _managed_container_identity(spec)
        if container_name != expected_name:
            raise WorkerExecutionError(
                "container_identity_mismatch",
                "container name did not match the attempt identity",
            )
        if not re.fullmatch(r"[0-9a-f]{64}", container_id):
            raise WorkerExecutionError(
                "container_id_invalid",
                "durable container ID is required to confirm cleanup",
            )
        if not command or any(not isinstance(item, str) or "\x00" in item for item in command):
            raise WorkerExecutionError(
                "container_command_invalid",
                "durable worker command is missing or invalid",
            )
        self.backend.preflight_existing(spec)
        if not self._confirm_container_absent(
            container_name=container_name,
            container_id=container_id,
            timeout=spec.limits.timeout_seconds,
        ):
            raise WorkerExecutionError(
                "container_still_present",
                "managed container is still present after an uncertain cleanup",
            )
        credential = cleanup_temporary_credential(spec)
        return ExecutionCleanupReceipt(container_name, True, credential)

    def inspect(self, handle: WorkerExecutionHandle) -> ContainerInspection:
        self._assert_handle(handle)
        payload = self._inspect_payload(handle.container_id, timeout=handle.spec.limits.timeout_seconds)
        return self._validate_inspect_contract(handle, payload)

    def stop(self, handle: WorkerExecutionHandle, *, grace_seconds: int = 10) -> ContainerInspection:
        if not (0 <= grace_seconds <= 300):
            raise IsolationValidationError("stop_grace_invalid", "stop grace must be between 0 and 300 seconds")
        inspection = self.inspect(handle)
        self._run_lifecycle(
            [
                *self.backend._engine_argv("stop"),
                "--time",
                str(grace_seconds),
                handle.container_id,
            ],
            timeout=handle.spec.limits.timeout_seconds + grace_seconds,
            label=f"{self.backend.kind} container stop",
        )
        handle.state = "stopped"
        return inspection

    def _stop_after_failure(self, handle: WorkerExecutionHandle) -> None:
        try:
            self.stop(handle, grace_seconds=0)
        except IsolationError:
            pass

    def wait(self, handle: WorkerExecutionHandle, *, timeout: float | None = None) -> WorkerWaitReceipt:
        self._assert_handle(handle)
        if handle.state != "started":
            raise WorkerExecutionError("worker_state_invalid", "only a started worker can be waited")
        effective_timeout = timeout if timeout is not None else handle.spec.limits.timeout_seconds
        if not math.isfinite(effective_timeout) or effective_timeout <= 0:
            raise IsolationValidationError("timeout_invalid", "worker wait timeout must be positive")
        result = handle.process.communicate_bounded(
            b"",
            timeout=effective_timeout,
            max_stdout_bytes=self.max_stdout_bytes,
            max_stderr_bytes=self.max_stderr_bytes,
        )
        handle._stdin_prompt = b""
        if result.timed_out:
            self._stop_after_failure(handle)
            raise WorkerExecutionError("worker_timeout", "managed worker exceeded its execution timeout")
        if result.stdout_truncated:
            self._stop_after_failure(handle)
            raise WorkerExecutionError("worker_stdout_limit_exceeded", "managed worker stdout exceeded its byte limit")
        if result.stderr_truncated:
            self._stop_after_failure(handle)
            raise WorkerExecutionError("worker_stderr_limit_exceeded", "managed worker stderr exceeded its byte limit")
        try:
            events = self._parse_stdout_events(handle, result.stdout)
        except IsolationError:
            self._stop_after_failure(handle)
            raise
        inspection = self.inspect(handle)
        if inspection.status not in {"exited", "dead"}:
            if result.exit_code != 0:
                # A deterministic-name contender may lose after another
                # recovery client created the same attempt container, or an
                # attached engine client may die while the container remains
                # alive.  In both cases the fully verified immutable identity
                # is the authority; recover logs instead of stopping it or
                # launching another provider call.
                handle.process = _DetachedManagedProcess()
                handle.recovered = True
                return self.recover_wait(handle, timeout=effective_timeout)
            self._stop_after_failure(handle)
            raise WorkerExecutionError(
                "worker_container_not_exited",
                "managed worker client exited before the container reached a terminal state",
                details={"status": inspection.status},
            )
        if inspection.exit_code is not None and inspection.exit_code != result.exit_code:
            handle.process = _DetachedManagedProcess()
            handle.recovered = True
            return self.recover_wait(handle, timeout=effective_timeout)
        handle.state = "finished"
        return WorkerWaitReceipt(
            container_name=handle.container_name,
            backend=self.backend.kind,
            exit_code=result.exit_code,
            stdout_events=events,
            stdout_bytes=len(result.stdout),
            stderr_bytes=len(result.stderr),
            stderr_truncated=result.stderr_truncated,
        )

    def _parse_stdout_events(
        self,
        handle: WorkerExecutionHandle,
        stdout: bytes,
    ) -> tuple[Mapping[str, Any], ...]:
        try:
            stdout_text = stdout.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise WorkerExecutionError("worker_stdout_encoding_invalid", "managed worker stdout must be UTF-8 JSONL") from exc
        events: list[Mapping[str, Any]] = []
        for line_number, line in enumerate(stdout_text.splitlines(), start=1):
            if not line.strip():
                continue
            if len(line.encode("utf-8")) > 64 * 1024:
                raise WorkerExecutionError(
                    "worker_stdout_line_too_large",
                    "managed worker emitted an oversized JSONL record",
                    details={"line": line_number},
                )
            try:
                event = json.loads(line)
            except json.JSONDecodeError as exc:
                raise WorkerExecutionError(
                    "worker_stdout_json_invalid",
                    "managed worker stdout contained invalid JSONL",
                    details={"line": line_number},
                ) from exc
            if not isinstance(event, dict):
                raise WorkerExecutionError(
                    "worker_stdout_json_invalid",
                    "managed worker JSONL records must be objects",
                    details={"line": line_number},
                )
            events.append(_redact_json(event, handle._secret_values))
            if len(events) > 4096:
                raise WorkerExecutionError("worker_stdout_event_limit", "managed worker emitted too many JSONL records")
        return tuple(events)

    def recover_wait(
        self,
        handle: WorkerExecutionHandle,
        *,
        timeout: float | None = None,
    ) -> WorkerWaitReceipt:
        """Wait by durable container identity and recover bounded JSONL logs."""

        self._assert_handle(handle)
        effective_timeout = timeout if timeout is not None else handle.spec.limits.timeout_seconds
        if not math.isfinite(effective_timeout) or effective_timeout <= 0:
            raise IsolationValidationError("timeout_invalid", "worker recovery timeout must be positive")
        deadline = time.monotonic() + effective_timeout
        while True:
            inspection = self.inspect(handle)
            if inspection.status in {"exited", "dead", "stopped"}:
                break
            if time.monotonic() >= deadline:
                with contextlib.suppress(IsolationError):
                    self.stop(handle, grace_seconds=0)
                raise WorkerExecutionError(
                    "worker_recovery_timeout",
                    "recovered OCI worker did not reach a terminal state",
                )
            time.sleep(0.1)
        try:
            result = self.backend.runner(
                tuple(self.backend._engine_argv("logs", handle.container_id)),
                timeout=effective_timeout,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise WorkerExecutionError(
                "worker_recovery_logs_unavailable",
                "recovered worker logs could not be read; usage remains unknown",
            ) from exc
        if result.returncode != 0:
            raise WorkerExecutionError(
                "worker_recovery_logs_unavailable",
                "recovered worker logs could not be read; usage remains unknown",
                details={"returncode": result.returncode},
            )
        stdout = result.stdout.encode("utf-8")
        stderr = result.stderr.encode("utf-8")
        if len(stdout) > self.max_stdout_bytes:
            raise WorkerExecutionError(
                "worker_stdout_limit_exceeded",
                "recovered worker stdout exceeded its byte limit; usage remains unknown",
            )
        if len(stderr) > self.max_stderr_bytes:
            raise WorkerExecutionError(
                "worker_stderr_limit_exceeded",
                "recovered worker stderr exceeded its byte limit; usage remains unknown",
            )
        events = self._parse_stdout_events(handle, stdout)
        exit_code = int(inspection.exit_code) if inspection.exit_code is not None else 125
        handle.state = "finished"
        return WorkerWaitReceipt(
            container_name=handle.container_name,
            backend=self.backend.kind,
            exit_code=exit_code,
            stdout_events=events,
            stdout_bytes=len(stdout),
            stderr_bytes=len(stderr),
            stderr_truncated=False,
        )

    def cleanup(self, handle: WorkerExecutionHandle) -> ExecutionCleanupReceipt:
        self._assert_handle(handle)
        identity_drift: tuple[str, ...] = ()
        removal_confirmed = False
        try:
            payload = self._inspect_payload(handle.container_id, timeout=handle.spec.limits.timeout_seconds)
            container_id = str(payload.get("Id") or payload.get("ID") or payload.get("id") or "").lower()
            if not re.fullmatch(r"[0-9a-f]{64}", container_id) or not hmac.compare_digest(
                container_id, handle.container_id
            ):
                raise WorkerExecutionError(
                    "container_id_mismatch",
                    "managed container ID did not match its durable cleanup identity",
                )
            config = payload.get("Config") if isinstance(payload.get("Config"), dict) else {}
            actual_labels = config.get("Labels") if isinstance(config.get("Labels"), dict) else {}
            identity_drift = tuple(
                sorted(key for key, value in handle.labels.items() if actual_labels.get(key) != value)
            )
            self._run_lifecycle(
                self.backend._engine_argv("rm", "--force", handle.container_id),
                timeout=handle.spec.limits.timeout_seconds,
                label=f"{self.backend.kind} container cleanup",
            )
            removal_confirmed = True
            if handle.process.poll() is None:
                try:
                    handle.process.kill()
                except OSError:
                    pass
        except IsolationError as exc:
            try:
                removal_confirmed = self._confirm_container_absent(
                    container_name=handle.container_name,
                    container_id=handle.container_id,
                    timeout=handle.spec.limits.timeout_seconds,
                )
            except IsolationError as confirm_error:
                raise WorkerExecutionError(
                    "container_cleanup_unconfirmed",
                    "managed container cleanup could not be confirmed; credential was preserved",
                    details={
                        "container_cleanup_unconfirmed": True,
                        "container_name": handle.container_name,
                        "container_id": handle.container_id,
                        "cleanup_error": exc.code,
                        "confirmation_error": confirm_error.code,
                        "credential_deleted": False,
                    },
                ) from exc
            if not removal_confirmed:
                raise WorkerExecutionError(
                    "container_cleanup_unconfirmed",
                    "managed container is still present after cleanup failure; credential was preserved",
                    details={
                        "container_cleanup_unconfirmed": True,
                        "container_name": handle.container_name,
                        "container_id": handle.container_id,
                        "cleanup_error": exc.code,
                        "credential_deleted": False,
                    },
                ) from exc
        if not removal_confirmed:
            raise WorkerExecutionError(
                "container_cleanup_unconfirmed",
                "managed container cleanup was not confirmed; credential was preserved",
                details={
                    "container_cleanup_unconfirmed": True,
                    "container_name": handle.container_name,
                    "container_id": handle.container_id,
                    "credential_deleted": False,
                },
            )
        credential = cleanup_temporary_credential(handle.spec)
        handle._secret_values = ()
        handle._stdin_prompt = b""
        handle.state = "cleaned"
        return ExecutionCleanupReceipt(handle.container_name, True, credential, identity_drift)


__all__ = [
    "CANARY_SCHEMA",
    "CapturedProcessResult",
    "CommandResult",
    "ContainerInspection",
    "CredentialCleanupReceipt",
    "ExecutionCleanupReceipt",
    "IsolationAttestation",
    "IsolationError",
    "IsolationUnavailableError",
    "IsolationValidationError",
    "MountAttestation",
    "OciCliBackend",
    "OciWorkerExecutionAdapter",
    "OUTPUT_MAX_BYTES",
    "ResourceLimits",
    "STDERR_CAPTURE_MAX_BYTES",
    "STDOUT_JSONL_MAX_BYTES",
    "SelectedBackend",
    "UnsafeNativeBackend",
    "WorkerExecutionSpec",
    "WorkerExecutionError",
    "WorkerExecutionHandle",
    "WorkerIsolationBackend",
    "WorkerWaitReceipt",
    "ValidatedWorkerOutput",
    "cleanup_temporary_credential",
    "select_worker_isolation_backend",
    "subprocess_command_runner",
    "subprocess_process_factory",
    "validate_execution_spec",
    "validate_output_exchange",
]
