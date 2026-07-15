#!/usr/bin/env python3
"""Run live hostile OCI probes and write release evidence without faking skips.

The harness exits 0 on pass, 1 on a demonstrated contract failure, and 2 when
no local Linux engine or digest-pinned reviewed image is available.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import time
from typing import Any
from urllib.parse import urlsplit


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from costmarshal_v2.worker_isolation import (  # noqa: E402
    IsolationError,
    IsolationUnavailableError,
    OciCliBackend,
    OciWorkerExecutionAdapter,
    ResourceLimits,
    WorkerExecutionSpec,
    cleanup_temporary_credential,
    validate_output_exchange,
)


IMAGE_RE = re.compile(r"[^@\s]+@sha256:[0-9a-f]{64}\Z")


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def git_sha() -> str | None:
    try:
        return subprocess.check_output(
            ["git", "rev-parse", "HEAD"],
            cwd=ROOT,
            text=True,
            encoding="utf-8",
            errors="replace",
        ).strip()
    except (OSError, subprocess.CalledProcessError):
        return None


def write_artifact(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + f".tmp-{os.getpid()}")
    temporary.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def engine_command(engine_argv: tuple[str, ...], *args: str, timeout: float = 30.0) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [*engine_argv, *args],
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


def select_engine(requested: str) -> str | None:
    candidates = ("docker", "podman") if requested == "auto" else (requested,)
    return next((candidate for candidate in candidates if shutil.which(candidate)), None)


def pin_engine_argv(engine: str) -> tuple[tuple[str, ...] | None, str | None]:
    """Resolve and pin Docker's verified local endpoint once."""

    if engine != "docker":
        return (engine,), None
    shown = engine_command((engine,), "context", "show", timeout=10)
    if shown.returncode != 0:
        return None, "Docker context show failed"
    context = shown.stdout.strip()
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", context):
        return None, "Docker selected an invalid context name"
    inspected = engine_command(
        (engine,),
        "context",
        "inspect",
        context,
        "--format",
        "{{json .}}",
        timeout=10,
    )
    if inspected.returncode != 0:
        return None, "Docker context inspect failed"
    try:
        payload = json.loads(inspected.stdout)
    except json.JSONDecodeError:
        return None, "Docker context inspect returned invalid JSON"
    endpoint = str(((payload.get("Endpoints") or {}).get("docker") or {}).get("Host") or "")
    if not endpoint.startswith(("unix://", "npipe://")):
        return None, "Docker context is not a verified local endpoint"
    return (engine, "--host", endpoint), None


def inspect_live_container(engine_argv: tuple[str, ...], reference: str) -> dict[str, Any]:
    last_error = "container was not inspectable"
    for _ in range(30):
        result = engine_command(engine_argv, "inspect", "--format", "{{json .}}", reference, timeout=5)
        if result.returncode == 0:
            payload = json.loads(result.stdout)
            if isinstance(payload, list) and len(payload) == 1:
                payload = payload[0]
            if isinstance(payload, dict):
                return payload
        last_error = f"inspect return code {result.returncode}"
        time.sleep(0.1)
    raise RuntimeError(last_error)


def mount_boundary_ok(
    payload: dict[str, Any],
    spec: WorkerExecutionSpec,
    forbidden: tuple[Path, ...],
) -> bool:
    mounts = payload.get("Mounts") if isinstance(payload.get("Mounts"), list) else []
    mounts = [
        row
        for row in mounts
        if isinstance(row, dict) and str(row.get("Type") or row.get("type") or "bind").lower() == "bind"
    ]
    expected = {
        "/workspace": (spec.workspace.resolve(), spec.workspace_mode == "rw"),
        "/out": (spec.output_exchange.resolve(), True),
        "/bootstrap/profile.config.toml": (spec.profile_path.resolve(), False),
        "/run/secrets/provider": (spec.credential_path.resolve(), False),  # type: ignore[union-attr]
    }
    actual_targets = {str(row.get("Destination") or row.get("destination") or "") for row in mounts if isinstance(row, dict)}
    if actual_targets != set(expected):
        return False
    for row in mounts:
        if not isinstance(row, dict):
            return False
        source_text = str(row.get("Source") or row.get("source") or "")
        if not source_text:
            return False
        source = Path(source_text).resolve()
        destination = str(row.get("Destination") or row.get("destination") or "")
        expected_source, expected_rw = expected[destination]
        if os.path.normcase(os.fspath(source)) != os.path.normcase(os.fspath(expected_source)):
            return False
        if row.get("RW", row.get("rw")) is not expected_rw:
            return False
        for blocked in forbidden:
            try:
                source.relative_to(blocked)
                return False
            except ValueError:
                pass
            try:
                blocked.relative_to(source)
                return False
            except ValueError:
                pass
    return True


def verify_provider_proxy(
    engine_argv: tuple[str, ...],
    network_name: str,
    proxy_reference: str,
) -> tuple[bool, str, dict[str, str] | None]:
    """Verify an externally provisioned, running dual-homed egress proxy."""

    network_result = engine_command(
        engine_argv,
        "network",
        "inspect",
        "--format",
        "{{json .}}",
        network_name,
        timeout=10,
    )
    if network_result.returncode != 0:
        return False, "provider network was not inspectable", None
    try:
        network = json.loads(network_result.stdout)
    except json.JSONDecodeError:
        return False, "provider network inspect returned invalid JSON", None
    network_id = str(network.get("Id") or network.get("ID") or network.get("id") or "")
    labels_row = network.get("Labels", network.get("labels"))
    labels = labels_row if isinstance(labels_row, dict) else {}
    internal = network.get("Internal", network.get("internal"))
    if internal is not True or labels.get("io.costmarshal.provider-proxy") != "true":
        return False, "provider network is not internal and CostMarshal-trusted", None
    if not re.fullmatch(r"[0-9a-f]{12,64}", network_id):
        return False, "provider network has no immutable ID", None
    try:
        proxy = inspect_live_container(engine_argv, proxy_reference)
    except (RuntimeError, json.JSONDecodeError):
        return False, "provider proxy container was not inspectable", None
    proxy_config = proxy.get("Config") if isinstance(proxy.get("Config"), dict) else {}
    proxy_labels = proxy_config.get("Labels") if isinstance(proxy_config.get("Labels"), dict) else {}
    state = proxy.get("State") if isinstance(proxy.get("State"), dict) else {}
    network_settings = proxy.get("NetworkSettings") if isinstance(proxy.get("NetworkSettings"), dict) else {}
    attached = network_settings.get("Networks") if isinstance(network_settings.get("Networks"), dict) else {}
    attached_ids = {
        str(row.get("NetworkID") or row.get("network_id") or "")
        for row in attached.values()
        if isinstance(row, dict)
    }
    if state.get("Running") is not True and str(state.get("Status") or "").lower() != "running":
        return False, "provider proxy container is not running", None
    if proxy_labels.get("io.costmarshal.provider-proxy") != "true":
        return False, "provider proxy container is missing its trust label", None
    if network_id not in attached_ids or len({item for item in attached_ids if item}) < 2:
        return False, "provider proxy is not dual-homed on the internal and egress networks", None
    egress_network_id = ""
    for attached_name, attached_row in attached.items():
        if not isinstance(attached_row, dict):
            continue
        attached_id = str(attached_row.get("NetworkID") or attached_row.get("network_id") or "")
        if attached_id == network_id:
            continue
        egress_result = engine_command(
            engine_argv,
            "network",
            "inspect",
            "--format",
            "{{json .}}",
            str(attached_name),
            timeout=10,
        )
        if egress_result.returncode != 0:
            continue
        try:
            egress = json.loads(egress_result.stdout)
        except json.JSONDecodeError:
            continue
        inspected_id = str(egress.get("Id") or egress.get("ID") or egress.get("id") or "")
        if inspected_id == attached_id and egress.get("Internal", egress.get("internal")) is False:
            egress_network_id = attached_id
            break
    if not egress_network_id:
        return False, "provider proxy has no independently verified non-internal egress network", None
    proxy_id = str(proxy.get("Id") or proxy.get("ID") or proxy.get("id") or "")
    if not re.fullmatch(r"[0-9a-f]{64}", proxy_id):
        return False, "provider proxy has no immutable container ID", None
    proxy_image_id = str(proxy.get("Image") or proxy.get("ImageID") or "")
    if not re.fullmatch(r"sha256:[0-9a-f]{64}", proxy_image_id):
        return False, "provider proxy has no immutable image ID", None
    proxy_contract = {
        "Config": proxy_config,
        "HostConfig": proxy.get("HostConfig") if isinstance(proxy.get("HostConfig"), dict) else {},
        "Mounts": proxy.get("Mounts") if isinstance(proxy.get("Mounts"), list) else [],
        "NetworkIDs": sorted(item for item in attached_ids if item),
    }
    config_sha256 = "sha256:" + hashlib.sha256(
        json.dumps(
            proxy_contract,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            default=str,
        ).encode("utf-8")
    ).hexdigest()
    return True, f"verified running dual-homed proxy {proxy_id}", {
        "network_id": network_id,
        "egress_network_id": egress_network_id,
        "container_id": proxy_id,
        "image_id": proxy_image_id,
        "config_sha256": config_sha256,
    }


def base_payload(image: str, engine: str | None) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "generated_at": utc_now(),
        "git_sha": git_sha(),
        "status": "blocked",
        "adapter_enabled": True,
        "strong_isolation": False,
        "engine": engine,
        "image": image,
        "escape_attempts": 0,
        "escapes_succeeded": 0,
        "checks": [],
        "blockers": [],
        "probe_provenance": {
            "preflight_canary": "reviewed-image-internal digest-bound probe; not an external build attestation",
            "container_contract": "independent OCI engine inspect over a once-pinned local endpoint",
            "provider_proxy": "externally provisioned live dual-homed proxy topology",
        },
    }


def run(args: argparse.Namespace) -> tuple[int, dict[str, Any]]:
    image = args.image or os.environ.get("COSTMARSHAL_OCI_IMAGE", "")
    engine = select_engine(args.engine)
    provider_network = args.provider_network or os.environ.get("COSTMARSHAL_OCI_PROVIDER_NETWORK", "")
    proxy_container = args.proxy_container or os.environ.get("COSTMARSHAL_OCI_PROXY_CONTAINER", "")
    proxy_health_url = args.proxy_health_url or os.environ.get("COSTMARSHAL_OCI_PROXY_HEALTH_URL", "")
    proxy_health_sha256 = args.proxy_health_sha256 or os.environ.get(
        "COSTMARSHAL_OCI_PROXY_HEALTH_SHA256", ""
    )
    evidence = base_payload(image, engine)
    if not IMAGE_RE.fullmatch(image):
        evidence["blockers"].append("COSTMARSHAL_OCI_IMAGE must be a digest-pinned reviewed image")
    if engine is None:
        evidence["blockers"].append("no Docker or Podman CLI is installed")
    if not provider_network or not proxy_container:
        evidence["blockers"].append(
            "a real externally provisioned provider network and dual-homed proxy container are required"
        )
    parsed_health = urlsplit(proxy_health_url)
    if (
        parsed_health.scheme not in {"http", "https"}
        or not parsed_health.netloc
        or parsed_health.username
        or parsed_health.password
        or parsed_health.fragment
        or not re.fullmatch(r"[0-9a-f]{64}", proxy_health_sha256)
    ):
        evidence["blockers"].append(
            "a credential-free proxy health URL and expected SHA-256 are required for a positive allowlisted request"
        )
    if evidence["blockers"]:
        return 2, evidence

    assert engine is not None
    engine_argv, engine_error = pin_engine_argv(engine)
    if engine_argv is None:
        evidence["blockers"].append(engine_error or "could not pin a local engine endpoint")
        return 2, evidence
    proxy_ok, proxy_detail, proxy_identity = verify_provider_proxy(
        engine_argv,
        provider_network,
        proxy_container,
    )
    if not proxy_ok:
        evidence["blockers"].append(proxy_detail)
        return 2, evidence
    assert proxy_identity is not None
    provider_network_id = proxy_identity["network_id"]
    evidence["provider_proxy"] = {
        "container": proxy_container,
        "network": provider_network,
        "network_id": provider_network_id,
        "egress_network_id": proxy_identity["egress_network_id"],
        "container_id": proxy_identity["container_id"],
        "image_id": proxy_identity["image_id"],
        "config_sha256": proxy_identity["config_sha256"],
        "health_url_sha256": "sha256:"
        + hashlib.sha256(proxy_health_url.encode("utf-8")).hexdigest(),
        "health_response_sha256": "sha256:" + proxy_health_sha256,
        "verification": proxy_detail,
    }

    temp = Path(tempfile.mkdtemp(prefix="costmarshal-oci-live-"))
    network_name = provider_network
    checks: list[dict[str, Any]] = []
    attempts = 0
    escapes = 0
    attestation: dict[str, Any] | None = None
    active_handles: list[Any] = []
    adapter: OciWorkerExecutionAdapter | None = None

    def record(check_id: str, passed: bool, *, malicious: bool = True, detail: str = "") -> None:
        nonlocal attempts, escapes
        if malicious:
            attempts += 1
            if not passed:
                escapes += 1
        checks.append({"id": check_id, "status": "pass" if passed else "fail", "detail": detail})

    try:
        runtime = temp / "host-runtime"
        runtime.mkdir()
        (runtime / "runtime-sentinel.txt").write_text("host-runtime-must-not-mount", encoding="utf-8")
        aggregate = temp / "aggregate-providers.env"
        aggregate.write_text("LOW_API_KEY=aggregate-secret-must-not-mount\n", encoding="utf-8")
        profile = temp / "profile.config.toml"
        profile.write_text("model = 'evidence-only'\n", encoding="utf-8")

        backend = OciCliBackend(engine)
        adapter = OciWorkerExecutionAdapter(backend)

        scenario_index = 0

        def make_spec(mode: str, network_mode: str) -> WorkerExecutionSpec:
            nonlocal scenario_index
            scenario_index += 1
            workspace = temp / f"workspace-{scenario_index}"
            workspace.mkdir()
            (workspace / "task.txt").write_text("probe target\n", encoding="utf-8")
            output = temp / f"output-{scenario_index}"
            output.mkdir()
            credential_root = temp / f"credential-{scenario_index}"
            credential_root.mkdir()
            credential = credential_root / "provider.secret"
            credential.write_text("selected-secret-only", encoding="utf-8")
            return WorkerExecutionSpec(
                project_id="oci-evidence",
                actor_id=f"probe-{scenario_index}",
                attempt_id=f"ATT-EVIDENCE-{scenario_index}",
                image=image,
                workspace=workspace,
                workspace_mode=mode,  # type: ignore[arg-type]
                profile_path=profile,
                output_exchange=output,
                credential_path=credential,
                provider_env_key="EVIDENCE_API_KEY",
                credential_cleanup="delete-after-use",
                credential_temp_root=credential_root,
                isolation_mode="required",
                engine=engine,  # type: ignore[arg-type]
                network_mode=network_mode,  # type: ignore[arg-type]
                network_name=network_name if network_mode == "provider-proxy" else None,
                forbidden_mount_roots=(runtime, aggregate),
                limits=ResourceLimits(
                    memory_mb=512,
                    cpus=1.0,
                    pids=64,
                    timeout_seconds=20.0,
                    tmpfs_mb=32,
                    home_tmpfs_mb=32,
                ),
            )

        def run_boundary(workspace_mode: str, network_mode: str) -> None:
            nonlocal attestation
            spec = make_spec(workspace_mode, network_mode)
            command = ["costmarshal-escape-probe", "--mode", "boundary", "--hold-ms", "1500"]
            if network_mode == "provider-proxy":
                command.extend(
                    [
                        "--proxy-health-url",
                        proxy_health_url,
                        "--proxy-health-sha256",
                        proxy_health_sha256,
                    ]
                )
            handle = adapter.start(
                spec,
                command,
                stdin_prompt="",
            )
            active_handles.append(handle)
            live = inspect_live_container(engine_argv, handle.container_id)
            adapter.inspect(handle)
            config = live.get("Config") if isinstance(live.get("Config"), dict) else {}
            host = live.get("HostConfig") if isinstance(live.get("HostConfig"), dict) else {}
            state = live.get("State") if isinstance(live.get("State"), dict) else {}
            networks_row = live.get("NetworkSettings") if isinstance(live.get("NetworkSettings"), dict) else {}
            networks = networks_row.get("Networks") if isinstance(networks_row.get("Networks"), dict) else {}
            labels = config.get("Labels") if isinstance(config.get("Labels"), dict) else {}
            live_id = str(live.get("Id") or live.get("ID") or live.get("id") or "")
            record("immutable_container_id_verified", live_id == handle.container_id)
            record("container_labels_verified", all(labels.get(key) == value for key, value in handle.labels.items()))
            record("container_image_verified", config.get("Image") == spec.image)
            record("container_command_verified", list(config.get("Cmd") or []) == list(handle.command))
            expected_uid, expected_gid = backend._container_user()
            record("container_user_verified", config.get("User") == f"{expected_uid}:{expected_gid}")
            security_opt = {str(item).lower() for item in (host.get("SecurityOpt") or [])}
            cap_drop = {str(item).upper() for item in (host.get("CapDrop") or [])}
            cap_drop_all = bool({"ALL", "CAP_ALL"} & cap_drop) or (
                engine == "podman" and live.get("EffectiveCaps") == []
            )
            record(
                "container_security_options_verified",
                host.get("ReadonlyRootfs") is True
                and cap_drop_all
                and not (host.get("CapAdd") or [])
                and any(item.startswith("no-new-privileges") for item in security_opt)
                and host.get("Privileged") is not True,
            )
            attached_ids = {
                str(row.get("NetworkID") or row.get("network_id") or "")
                for row in networks.values()
                if isinstance(row, dict)
            }
            expected_network_ids = set() if network_mode == "none" else {provider_network_id}
            record(
                "container_network_attachment_verified",
                (
                    host.get("NetworkMode") == "none"
                    and set(networks).issubset({"none"})
                    and not {item for item in attached_ids if item}
                )
                if network_mode == "none"
                else len(networks) == 1 and attached_ids == expected_network_ids,
            )
            record(
                "mount_allowlist_excludes_runtime_and_aggregate",
                mount_boundary_ok(live, spec, (runtime.resolve(), aggregate.resolve())),
            )
            record("container_live_during_independent_inspect", str(state.get("Status") or "").lower() in {"running", "created"}, malicious=False)
            receipt = adapter.wait(handle)
            event = next((row for row in receipt.stdout_events if row.get("type") == "costmarshal_escape_probe"), None)
            if event is None:
                raise RuntimeError("escape probe did not emit its JSONL evidence")
            record(f"runtime_hidden_{workspace_mode}_{network_mode}", event.get("runtime_visible") is False)
            record(f"aggregate_secrets_hidden_{workspace_mode}_{network_mode}", event.get("aggregate_secrets_visible") is False)
            record(f"engine_socket_hidden_{workspace_mode}_{network_mode}", event.get("engine_socket_visible") is False)
            record(f"selected_credential_only_{workspace_mode}_{network_mode}", event.get("selected_credential_visible") is True)
            record(
                f"workspace_mode_{workspace_mode}_{network_mode}",
                event.get("workspace_writable") is (workspace_mode == "rw"),
            )
            record(f"network_policy_{network_mode}", event.get("external_network_reachable") is False)
            record(
                f"proxy_allowlisted_request_{network_mode}",
                event.get("proxy_health_verified") is True
                if network_mode == "provider-proxy"
                else event.get("proxy_health_verified") is None,
                malicious=False,
            )
            validated = validate_output_exchange(spec.output_exchange)
            record(f"valid_exchange_{workspace_mode}_{network_mode}", validated.text.startswith("# Probe"), malicious=False)
            cleanup = adapter.cleanup(handle)
            active_handles.remove(handle)
            record(f"credential_cleanup_{workspace_mode}_{network_mode}", cleanup.credential.deleted and not spec.credential_path.exists())
            attestation = handle.attestation.to_dict()

        run_boundary("ro", "none")
        run_boundary("rw", "provider-proxy")

        expected_rejections = {
            "symlink-output": {"mount_path_linked", "output_contract_invalid"},
            "extra-output": {"output_contract_invalid"},
            "oversize-output": {"output_too_large"},
        }
        for attack_mode, expected_codes in expected_rejections.items():
            spec = make_spec("ro", "none")
            handle = adapter.start(
                spec,
                ["costmarshal-escape-probe", "--mode", attack_mode],
                stdin_prompt="",
            )
            active_handles.append(handle)
            receipt = adapter.wait(handle)
            event = next((row for row in receipt.stdout_events if row.get("type") == "costmarshal_escape_probe"), None)
            record(f"{attack_mode}_created", bool(event and event.get("output_attack_created")), malicious=False)
            rejected = False
            rejection_code = "accepted"
            try:
                validate_output_exchange(spec.output_exchange)
            except IsolationError as exc:
                rejection_code = exc.code
                rejected = exc.code in expected_codes
            record(f"{attack_mode}_rejected", rejected, detail=rejection_code)
            cleanup = adapter.cleanup(handle)
            active_handles.remove(handle)
            record(f"{attack_mode}_credential_cleanup", cleanup.credential.deleted and not spec.credential_path.exists())

        evidence.update(
            {
                "status": "pass" if escapes == 0 and all(row["status"] == "pass" for row in checks) else "fail",
                "strong_isolation": bool(attestation and attestation.get("strong_isolation") is True),
                "escape_attempts": attempts,
                "escapes_succeeded": escapes,
                "checks": checks,
                "attestation": attestation,
                "blockers": [],
            }
        )
        return (0 if evidence["status"] == "pass" else 1), evidence
    except IsolationUnavailableError as exc:
        blocked_codes = {
            "engine_command_failed",
            "engine_endpoint_unknown",
            "engine_platform_rejected",
            "windows_container_mode_rejected",
            "podman_machine_unavailable",
            "remote_engine_rejected",
            "image_digest_mismatch",
        }
        evidence.update(
            {
                "status": "blocked" if exc.code in blocked_codes else "fail",
                "escape_attempts": attempts,
                "escapes_succeeded": escapes,
                "checks": checks,
                "blockers": [f"{exc.code}: {exc}"],
            }
        )
        return (2 if evidence["status"] == "blocked" else 1), evidence
    except (IsolationError, OSError, RuntimeError, ValueError, json.JSONDecodeError) as exc:
        evidence.update(
            {
                "status": "fail",
                "escape_attempts": attempts,
                "escapes_succeeded": max(escapes, 1),
                "checks": checks,
                "blockers": [f"live OCI evidence failed safely: {type(exc).__name__}"],
            }
        )
        return 1, evidence
    finally:
        if adapter is not None:
            for handle in reversed(active_handles):
                try:
                    adapter.cleanup(handle)
                except IsolationError:
                    engine_command(engine_argv, "rm", "--force", handle.container_id, timeout=30)
                    try:
                        cleanup_temporary_credential(handle.spec)
                    except IsolationError:
                        pass
        shutil.rmtree(temp, ignore_errors=True)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--engine", choices=("auto", "docker", "podman"), default="auto")
    parser.add_argument("--image", help="digest-pinned reviewed worker image; defaults to COSTMARSHAL_OCI_IMAGE")
    parser.add_argument("--provider-network", help="pre-provisioned internal network with the CostMarshal proxy trust label")
    parser.add_argument("--proxy-container", help="running trusted proxy container attached to provider and egress networks")
    parser.add_argument("--proxy-health-url", help="credential-free allowlisted URL reached only through the provider proxy")
    parser.add_argument("--proxy-health-sha256", help="expected SHA-256 of the bounded proxy health response body")
    parser.add_argument("--artifact", type=Path, default=ROOT / "artifacts" / "oci-attestation.json")
    args = parser.parse_args()
    code, evidence = run(args)
    write_artifact(args.artifact.resolve(), evidence)
    print(json.dumps({"status": evidence["status"], "artifact": str(args.artifact.resolve())}))
    return code


if __name__ == "__main__":
    raise SystemExit(main())
