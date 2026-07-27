#!/usr/bin/env python3
"""Fail-closed preflight and explicit single-host production deployment."""

from __future__ import annotations

import argparse
import hashlib
import ipaddress
import json
import os
import re
import shutil
import ssl
import stat
import subprocess
import sys
import tempfile
import urllib.parse
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from costmarshal_v2 import __version__  # noqa: E402
from costmarshal_v2.production_gateway import (  # noqa: E402
    GatewayError,
    GatewayPolicy,
    probe_gateway_health,
)


IMAGE_DIGEST = re.compile(r"[^@\s]+@sha256:[0-9a-f]{64}")
SECRET_FILES = {
    "COSTMARSHAL_LEASE_SIGNING_KEY_FILE": (32, 256 * 1024, True),
    "COSTMARSHAL_BROKER_TLS_CERT_FILE": (1, 1024 * 1024, False),
    "COSTMARSHAL_BROKER_TLS_KEY_FILE": (1, 1024 * 1024, True),
    "COSTMARSHAL_WORKLOAD_CLIENT_CA_FILE": (1, 1024 * 1024, False),
    "COSTMARSHAL_PROXY_TLS_CERT_FILE": (1, 1024 * 1024, False),
    "COSTMARSHAL_PROXY_TLS_KEY_FILE": (1, 1024 * 1024, True),
    "COSTMARSHAL_BROKER_CLIENT_CERT_FILE": (1, 1024 * 1024, False),
    "COSTMARSHAL_BROKER_CLIENT_KEY_FILE": (1, 1024 * 1024, True),
    "COSTMARSHAL_BROKER_CA_FILE": (1, 1024 * 1024, False),
}
DIRECTORIES = {
    "COSTMARSHAL_GATEWAY_STATE_DIR",
    "COSTMARSHAL_GATEWAY_AUDIT_DIR",
    "COSTMARSHAL_PROVIDER_CREDENTIALS_DIR",
}
ENVIRONMENT_VARIABLES = frozenset(
    set(SECRET_FILES)
    | DIRECTORIES
    | {
        "COSTMARSHAL_GATEWAY_IMAGE",
        "COSTMARSHAL_BROKER_BIND_IP",
        "COSTMARSHAL_PROXY_BIND_IP",
        "COSTMARSHAL_BROKER_ENDPOINT",
        "COSTMARSHAL_PROVIDER_PROXY_ENDPOINT",
    }
)


class DeploymentBlocked(RuntimeError):
    """A production precondition is absent or unsafe."""


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return "sha256:" + digest.hexdigest()


def _safe_regular_file(
    path: Path,
    *,
    minimum: int,
    maximum: int,
    label: str,
    private: bool = True,
) -> None:
    try:
        info = path.lstat()
    except OSError as exc:
        raise DeploymentBlocked(f"{label} is unavailable") from exc
    reparse = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    if (
        not stat.S_ISREG(info.st_mode)
        or stat.S_ISLNK(info.st_mode)
        or bool(getattr(info, "st_file_attributes", 0) & reparse)
        or info.st_size < minimum
        or info.st_size > maximum
    ):
        raise DeploymentBlocked(f"{label} is not a bounded regular file")
    if private and os.name != "nt" and info.st_mode & 0o077:
        raise DeploymentBlocked(f"{label} must not be accessible to group/other")


def _safe_directory(
    path: Path,
    *,
    label: str,
    required_uid: int | None = None,
) -> None:
    try:
        info = path.lstat()
    except OSError as exc:
        raise DeploymentBlocked(f"{label} is unavailable") from exc
    reparse = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    if (
        not stat.S_ISDIR(info.st_mode)
        or stat.S_ISLNK(info.st_mode)
        or bool(getattr(info, "st_file_attributes", 0) & reparse)
    ):
        raise DeploymentBlocked(f"{label} is not a real directory")
    if os.name != "nt":
        if info.st_mode & 0o022:
            raise DeploymentBlocked(f"{label} must not be group/world writable")
        if required_uid is not None and info.st_uid != required_uid:
            raise DeploymentBlocked(f"{label} must be owned by uid {required_uid}")


def _environment_path(name: str) -> Path:
    raw = os.environ.get(name)
    if not raw:
        raise DeploymentBlocked(f"required environment variable {name} is absent")
    return Path(raw).expanduser().resolve()


def _load_environment_file(path: Path) -> list[str]:
    resolved = path.expanduser().resolve()
    try:
        resolved.relative_to(ROOT)
    except ValueError:
        pass
    else:
        raise DeploymentBlocked(
            "production environment file must stay outside the source repository"
        )
    _safe_regular_file(
        resolved,
        minimum=1,
        maximum=64 * 1024,
        label="production environment file",
    )
    try:
        lines = resolved.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError) as exc:
        raise DeploymentBlocked(
            "production environment file is not readable UTF-8"
        ) from exc
    loaded: dict[str, str] = {}
    for line_number, raw in enumerate(lines, start=1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        match = re.fullmatch(r"([A-Z][A-Z0-9_]*)=(.+)", line)
        if not match:
            raise DeploymentBlocked(
                f"production environment file line {line_number} is invalid"
            )
        name, value = match.groups()
        if name not in ENVIRONMENT_VARIABLES:
            raise DeploymentBlocked(
                f"production environment file variable is not allowed: {name}"
            )
        if name in loaded:
            raise DeploymentBlocked(
                f"production environment file repeats variable: {name}"
            )
        loaded[name] = value
    if not loaded:
        raise DeploymentBlocked("production environment file contains no settings")
    for name, value in loaded.items():
        existing = os.environ.get(name)
        if existing is not None and existing != value:
            raise DeploymentBlocked(
                f"production environment conflicts with existing variable: {name}"
            )
    os.environ.update(loaded)
    return sorted(loaded)


def _loopback_bind_address(name: str) -> str:
    raw = str(os.environ.get(name) or "127.0.0.1").strip()
    try:
        address = ipaddress.ip_address(raw)
    except ValueError as exc:
        raise DeploymentBlocked(f"{name} must be a literal IPv4 loopback address") from exc
    if address.version != 4 or not address.is_loopback:
        raise DeploymentBlocked(f"{name} must be a literal IPv4 loopback address")
    return str(address)


def _gateway_endpoint(name: str, *, suffix: str, port: int) -> str:
    raw = str(os.environ.get(name) or "").strip()
    try:
        parsed = urllib.parse.urlsplit(raw)
        parsed_port = parsed.port
    except ValueError as exc:
        raise DeploymentBlocked(f"{name} is not a valid HTTPS endpoint") from exc
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
        or parsed.path.rstrip("/") != suffix
        or parsed_port != port
    ):
        raise DeploymentBlocked(
            f"{name} must be a credential-free HTTPS {suffix} endpoint "
            f"on IPv4 loopback port {port}"
        )
    try:
        endpoint_address = ipaddress.ip_address(parsed.hostname)
    except ValueError as exc:
        raise DeploymentBlocked(
            f"{name} must use a literal IPv4 loopback host"
        ) from exc
    if endpoint_address.version != 4 or not endpoint_address.is_loopback:
        raise DeploymentBlocked(
            f"{name} must use a literal IPv4 loopback host"
        )
    return parsed.geturl()


def _validate_tls_material() -> dict[str, str]:
    broker_cert = _environment_path("COSTMARSHAL_BROKER_TLS_CERT_FILE")
    broker_key = _environment_path("COSTMARSHAL_BROKER_TLS_KEY_FILE")
    proxy_cert = _environment_path("COSTMARSHAL_PROXY_TLS_CERT_FILE")
    proxy_key = _environment_path("COSTMARSHAL_PROXY_TLS_KEY_FILE")
    client_cert = _environment_path("COSTMARSHAL_BROKER_CLIENT_CERT_FILE")
    client_key = _environment_path("COSTMARSHAL_BROKER_CLIENT_KEY_FILE")
    client_ca = _environment_path("COSTMARSHAL_WORKLOAD_CLIENT_CA_FILE")
    server_ca = _environment_path("COSTMARSHAL_BROKER_CA_FILE")
    try:
        for cert_file, key_file in (
            (broker_cert, broker_key),
            (proxy_cert, proxy_key),
        ):
            context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            context.minimum_version = ssl.TLSVersion.TLSv1_2
            context.load_cert_chain(str(cert_file), str(key_file), password="")
        client_context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        client_context.load_cert_chain(
            str(client_cert),
            str(client_key),
            password="",
        )
        client_ca_context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        client_ca_context.load_verify_locations(cafile=str(client_ca))
        ssl.create_default_context(ssl.Purpose.SERVER_AUTH, cafile=str(server_ca))
    except (OSError, ssl.SSLError) as exc:
        raise DeploymentBlocked(
            "TLS certificates, private keys, or CA bundles are invalid or mismatched"
        ) from exc
    return {
        "client_cert_file": str(client_cert),
        "client_key_file": str(client_key),
        "server_ca_file": str(server_ca),
    }


def _run(
    command: list[str],
    *,
    cwd: Path,
    timeout: int = 60,
) -> str:
    try:
        completed = subprocess.run(
            command,
            cwd=cwd,
            env=dict(os.environ),
            text=True,
            encoding="utf-8",
            errors="replace",
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise DeploymentBlocked(f"command unavailable: {command[0]}") from exc
    if completed.returncode != 0:
        raise DeploymentBlocked(
            f"command failed without exposing its output: {' '.join(command[:3])}"
        )
    return completed.stdout.strip()


def _source_identity() -> tuple[str, str]:
    commit = _run(["git", "rev-parse", "HEAD"], cwd=ROOT)
    if not re.fullmatch(r"[0-9a-f]{40}", commit):
        raise DeploymentBlocked("source checkout has no exact Git commit")
    dirty = _run(["git", "status", "--porcelain"], cwd=ROOT)
    if dirty:
        raise DeploymentBlocked("source checkout is dirty; deploy a committed release")
    return commit, __version__


def _raw_policy(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise DeploymentBlocked("gateway policy is not readable JSON") from exc
    if not isinstance(value, dict):
        raise DeploymentBlocked("gateway policy root must be an object")
    return value


def _container_database_path(policy_path: Path) -> str:
    raw = _raw_policy(policy_path)
    database_path = raw.get("database_path")
    if (
        not isinstance(database_path, str)
        or not re.fullmatch(
            r"/var/lib/costmarshal/[A-Za-z0-9][A-Za-z0-9._-]*",
            database_path,
        )
    ):
        raise DeploymentBlocked(
            "gateway policy database_path must be one direct regular filename "
            "under /var/lib/costmarshal"
        )
    return database_path


def _credential_receipts(policy_path: Path) -> list[dict[str, Any]]:
    raw = _raw_policy(policy_path)
    providers = raw.get("providers")
    if not isinstance(providers, dict) or not providers:
        raise DeploymentBlocked("gateway policy has no providers")
    host_root = _environment_path("COSTMARSHAL_PROVIDER_CREDENTIALS_DIR")
    receipts: list[dict[str, Any]] = []
    for provider_id, row in sorted(providers.items()):
        if not isinstance(row, dict):
            raise DeploymentBlocked(f"provider {provider_id} policy is invalid")
        credential = row.get("credential_file")
        prefix = "/run/provider-credentials/"
        if (
            not isinstance(credential, str)
            or not credential.startswith(prefix)
            or "/" in credential[len(prefix) :]
            or credential.endswith("/")
        ):
            raise DeploymentBlocked(
                f"provider {provider_id} credential_file must be a direct "
                "child of /run/provider-credentials"
            )
        filename = credential[len(prefix) :]
        host_file = host_root / filename
        _safe_regular_file(
            host_file,
            minimum=1,
            maximum=256 * 1024,
            label=f"credential for provider {provider_id}",
        )
        receipts.append(
            {
                "provider_id": provider_id,
                "container_path": credential,
                "credential_present": True,
            }
        )
    return receipts


def _compose_service_rows(raw: str) -> list[dict[str, Any]]:
    try:
        decoded = json.loads(raw)
    except json.JSONDecodeError:
        rows: list[Any] = []
        try:
            for line in raw.splitlines():
                if line.strip():
                    rows.append(json.loads(line))
        except json.JSONDecodeError as exc:
            raise DeploymentBlocked("Docker Compose service status was not JSON") from exc
    else:
        rows = decoded if isinstance(decoded, list) else [decoded]
    if not rows or any(not isinstance(row, dict) for row in rows):
        raise DeploymentBlocked("Docker Compose returned no service status")
    return rows


def _healthy_services(raw: str) -> list[dict[str, str]]:
    rows = _compose_service_rows(raw)
    expected = {"credential-broker", "provider-proxy"}
    services: dict[str, dict[str, str]] = {}
    for row in rows:
        service = row.get("Service")
        state = row.get("State")
        health = row.get("Health")
        if not all(isinstance(value, str) and value for value in (service, state, health)):
            raise DeploymentBlocked("Docker Compose service status is incomplete")
        if service in services:
            raise DeploymentBlocked("Docker Compose returned duplicate service status")
        services[service] = {
            "service": service,
            "state": state.lower(),
            "health": health.lower(),
        }
    if set(services) != expected:
        raise DeploymentBlocked(
            "deployment did not leave exactly the Broker and Proxy services"
        )
    for service in sorted(expected):
        status = services[service]
        if status["state"] != "running" or status["health"] != "healthy":
            raise DeploymentBlocked(
                f"deployment service {service} is not running and healthy"
            )
    return [services[service] for service in sorted(expected)]


def _verified_provider_proxy_network(raw: str) -> dict[str, str | bool]:
    try:
        decoded = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise DeploymentBlocked("provider-proxy network inspection was not JSON") from exc
    rows = decoded if isinstance(decoded, list) else [decoded]
    if len(rows) != 1 or not isinstance(rows[0], dict):
        raise DeploymentBlocked("provider-proxy network inspection was ambiguous")
    row = rows[0]
    network_id = str(row.get("Id") or "")
    labels = row.get("Labels")
    if (
        row.get("Name") != "costmarshal-provider-proxy"
        or row.get("Driver") != "bridge"
        or row.get("Internal") is not True
        or not re.fullmatch(r"[0-9a-f]{12,64}", network_id)
        or not isinstance(labels, dict)
        or labels.get("io.costmarshal.provider-proxy") != "true"
    ):
        raise DeploymentBlocked(
            "provider-proxy network is not an internal labelled bridge"
        )
    return {
        "name": "costmarshal-provider-proxy",
        "network_id": network_id,
        "driver": "bridge",
        "internal": True,
        "trusted": True,
    }


def preflight(args: argparse.Namespace) -> dict[str, Any]:
    compose = args.compose.expanduser().resolve()
    policy_path = args.policy.expanduser().resolve()
    if policy_path != compose.parent / "gateway-policy.json":
        raise DeploymentBlocked(
            "compose mounts only its sibling gateway-policy.json; use that exact path"
        )
    if not compose.is_file():
        raise DeploymentBlocked("production Compose file is unavailable")
    try:
        policy = GatewayPolicy.load(policy_path)
    except GatewayError as exc:
        raise DeploymentBlocked(f"gateway policy is invalid: {exc}") from exc
    database_path = _container_database_path(policy_path)
    image = os.environ.get("COSTMARSHAL_GATEWAY_IMAGE", "")
    if not IMAGE_DIGEST.fullmatch(image):
        raise DeploymentBlocked(
            "COSTMARSHAL_GATEWAY_IMAGE must be name@sha256:<64 lowercase hex>"
        )
    for name, (minimum, maximum, private) in SECRET_FILES.items():
        _safe_regular_file(
            _environment_path(name),
            minimum=minimum,
            maximum=maximum,
            label=name,
            private=private,
        )
    tls_files = _validate_tls_material()
    broker_bind_address = _loopback_bind_address(
        "COSTMARSHAL_BROKER_BIND_IP"
    )
    proxy_bind_address = _loopback_bind_address(
        "COSTMARSHAL_PROXY_BIND_IP"
    )
    broker_endpoint = _gateway_endpoint(
        "COSTMARSHAL_BROKER_ENDPOINT",
        suffix="/v1/leases",
        port=8443,
    )
    proxy_endpoint = _gateway_endpoint(
        "COSTMARSHAL_PROVIDER_PROXY_ENDPOINT",
        suffix="/v1",
        port=9443,
    )
    if (
        urllib.parse.urlsplit(broker_endpoint).hostname
        != broker_bind_address
        or urllib.parse.urlsplit(proxy_endpoint).hostname
        != proxy_bind_address
    ):
        raise DeploymentBlocked(
            "gateway endpoint hosts must exactly match their loopback bind addresses"
        )
    for name in DIRECTORIES:
        _safe_directory(
            _environment_path(name),
            label=name,
            required_uid=(
                65532
                if name
                in {
                    "COSTMARSHAL_GATEWAY_STATE_DIR",
                    "COSTMARSHAL_GATEWAY_AUDIT_DIR",
                }
                else None
            ),
        )
    credentials = _credential_receipts(policy_path)
    engine = args.engine
    if shutil.which(engine) is None:
        raise DeploymentBlocked(f"container engine is unavailable: {engine}")
    engine_version = _run(
        [engine, "version", "--format", "{{.Server.Version}}"],
        cwd=compose.parent,
    )
    compose_version = _run(
        [engine, "compose", "version", "--short"],
        cwd=compose.parent,
    )
    _run(
        [engine, "compose", "-f", str(compose), "config", "--quiet"],
        cwd=compose.parent,
    )
    raw_digests = _run(
        [engine, "image", "inspect", image, "--format", "{{json .RepoDigests}}"],
        cwd=compose.parent,
    )
    try:
        digests = json.loads(raw_digests)
    except json.JSONDecodeError as exc:
        raise DeploymentBlocked("container image inspection was not JSON") from exc
    expected_digest = image.split("@", 1)[1]
    if (
        not isinstance(digests, list)
        or not any(str(item).endswith("@" + expected_digest) for item in digests)
    ):
        raise DeploymentBlocked("local gateway image does not carry the required digest")
    commit, release_version = _source_identity()
    receipt = {
        "schema_version": "costmarshal-production-deployment-preflight-v1",
        "status": "ready-to-deploy",
        "deployment_commit": commit,
        "release_version": release_version,
        "gateway_image": image,
        "gateway_policy_sha256": policy.sha256,
        "gateway_database_path": database_path,
        "compose_sha256": _sha256(compose),
        "engine": engine,
        "engine_version": engine_version,
        "compose_version": compose_version,
        "providers": sorted(policy.providers),
        "credential_receipts": credentials,
        "broker_endpoint": broker_endpoint,
        "provider_proxy_endpoint": proxy_endpoint,
        "loopback_bind_addresses": {
            "broker": broker_bind_address,
            "proxy": proxy_bind_address,
        },
        "tls_material": {
            "status": "parseable-key-pairs-and-ca-bundles",
            "client_identity_material_present": bool(tls_files),
        },
    }
    if args.env_file is not None:
        receipt["environment_file_sha256"] = _sha256(
            args.env_file.expanduser().resolve()
        )
        receipt["environment_variables_loaded"] = list(args.loaded_environment)
    return receipt


def atomic_write_json(path: Path, value: Any) -> None:
    path = path.expanduser().resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=str(path.parent),
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(value, stream, ensure_ascii=False, sort_keys=True, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def deploy(args: argparse.Namespace) -> dict[str, Any]:
    receipt = preflight(args)
    if not args.apply:
        receipt["next_command"] = (
            "rerun this command with --apply after reviewing the preflight receipt"
        )
        return receipt
    compose = args.compose.expanduser().resolve()
    _run(
        [
            args.engine,
            "compose",
            "-f",
            str(compose),
            "up",
            "-d",
            "--wait",
            "--remove-orphans",
        ],
        cwd=compose.parent,
        timeout=args.timeout_seconds,
    )
    raw_services = _run(
        [
            args.engine,
            "compose",
            "-f",
            str(compose),
            "ps",
            "--all",
            "--format",
            "json",
        ],
        cwd=compose.parent,
    )
    services = _healthy_services(raw_services)
    raw_network = _run(
        [
            args.engine,
            "network",
            "inspect",
            "costmarshal-provider-proxy",
        ],
        cwd=compose.parent,
    )
    network = _verified_provider_proxy_network(raw_network)
    client_cert = _environment_path("COSTMARSHAL_BROKER_CLIENT_CERT_FILE")
    client_key = _environment_path("COSTMARSHAL_BROKER_CLIENT_KEY_FILE")
    server_ca = _environment_path("COSTMARSHAL_BROKER_CA_FILE")
    try:
        health = [
            probe_gateway_health(
                endpoint=str(receipt["broker_endpoint"]),
                expected_policy_sha256=str(receipt["gateway_policy_sha256"]),
                ca_file=server_ca,
                client_cert_file=client_cert,
                client_key_file=client_key,
                timeout_seconds=min(15.0, float(args.timeout_seconds)),
            ),
            probe_gateway_health(
                endpoint=str(receipt["provider_proxy_endpoint"]),
                expected_policy_sha256=str(receipt["gateway_policy_sha256"]),
                ca_file=server_ca,
                timeout_seconds=min(15.0, float(args.timeout_seconds)),
            ),
        ]
    except GatewayError as exc:
        raise DeploymentBlocked(
            "services started but policy-bound TLS health verification failed"
        ) from exc
    receipt["status"] = "deployed"
    receipt["services"] = services
    receipt["provider_proxy_network"] = network
    receipt["policy_bound_tls_health"] = health
    return receipt


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Preflight and explicitly deploy the production Broker/Proxy"
    )
    parser.add_argument(
        "--compose",
        type=Path,
        default=ROOT / "deploy" / "production" / "compose.yaml",
    )
    parser.add_argument(
        "--policy",
        type=Path,
        default=ROOT / "deploy" / "production" / "gateway-policy.json",
    )
    parser.add_argument("--engine", choices=["docker"], default="docker")
    parser.add_argument("--timeout-seconds", type=int, default=180)
    parser.add_argument("--output", type=Path)
    parser.add_argument(
        "--env-file",
        type=Path,
        help=(
            "Root-owned, secret-free production variable file outside the "
            "repository; only the documented allowlist is accepted"
        ),
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Start the reviewed services; without this flag the command is read-only",
    )
    return parser


def main() -> int:
    args = build_parser().parse_args()
    args.loaded_environment = []
    if args.timeout_seconds < 30 or args.timeout_seconds > 900:
        result = {
            "schema_version": "costmarshal-production-deployment-preflight-v1",
            "status": "blocked",
            "blocker": "timeout-seconds must be in the range 30..900",
        }
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
        return 2
    try:
        if args.env_file is not None:
            args.loaded_environment = _load_environment_file(args.env_file)
        result = deploy(args)
    except DeploymentBlocked as exc:
        result = {
            "schema_version": "costmarshal-production-deployment-preflight-v1",
            "status": "blocked",
            "blocker": str(exc),
        }
        if args.output:
            atomic_write_json(args.output, result)
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
        return 2
    if args.output:
        atomic_write_json(args.output, result)
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
