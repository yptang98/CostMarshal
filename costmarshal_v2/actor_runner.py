from __future__ import annotations

import argparse
import contextlib
import hashlib
import hmac
import json
import os
import re
import secrets
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import tomllib
from dataclasses import replace
from pathlib import Path
from typing import Any, Callable, Iterable
from urllib.parse import urlsplit

from .context_projection import (
    ContextProjectionError,
    apply_cumulative_change_artifact,
    build_cumulative_change_manifest,
    capture_projection_changes,
    materialize_context_projection,
    persist_change_artifact,
    verify_materialized_context_projection,
)
from .control_store import ControlStoreError, control_store_enabled, control_transaction
from .governance import (
    GovernanceError,
    enforce_governance_contract,
    load_stable_governance_project,
)
from .handoff_contract import (
    HandoffContractError,
    HandoffLimits,
    build_attempt_input_contract,
    build_attempt_output_contract,
    build_bound_prompt_bytes,
    build_collaboration_contract as build_semantic_collaboration_contract,
    build_prompt_binding as build_semantic_prompt_binding,
    validate_collaboration_contract as validate_semantic_collaboration_contract,
)
from .mailbox import send_message
from .locking import ProjectLockTimeout, advisory_file_lock, project_write_lock
from .paths import ProjectLayout, resolve_project, slugify
from .profile_binding import (
    ProfileBindingError,
    install_bound_copy,
    parse_profile_bytes,
    validate_profile_binding,
    verify_profile_snapshot,
)
from .security import (
    SecurityValidationError,
    ensure_workspace_containment,
    normalize_path_list,
    parse_secrets_text,
    provider_env_from_secrets,
    redact_secret_values,
)
from .routing import RoutingValidationError, project_provider_catalog
from .session_backend import pid_start_marker
from .worker_isolation import (
    CredentialCleanupReceipt,
    ExecutionCleanupReceipt,
    IsolationError,
    OciCliBackend,
    OciWorkerExecutionAdapter,
    ResourceLimits,
    WorkerExecutionError,
    WorkerExecutionSpec,
    cleanup_temporary_credential,
    validate_execution_spec,
    validate_output_exchange,
)
from .state import (
    SCHEMA_VERSION,
    append_event,
    atomic_write_bytes,
    atomic_write_json,
    atomic_write_text,
    actor_prompt_file,
    load_actor,
    load_project,
    load_task,
    now_iso,
    save_actor,
    save_task,
    task_dir,
)


ACTOR_FAULT_ENV = "COSTMARSHAL_ACTOR_FAULT"
PROVIDER_COMPLETION_PENDING = "finished_pending_finalize"
NATIVE_LAUNCH_BARRIER_STAGE_ENV = "COSTMARSHAL_NATIVE_LAUNCH_BARRIER_STAGE"
NATIVE_LAUNCH_BARRIER_READY_ENV = "COSTMARSHAL_NATIVE_LAUNCH_BARRIER_READY"
NATIVE_LAUNCH_BARRIER_RELEASE_ENV = "COSTMARSHAL_NATIVE_LAUNCH_BARRIER_RELEASE"


def _actor_fault(name: str) -> None:
    if os.environ.get(ACTOR_FAULT_ENV) == name:
        os._exit(87)  # intentionally bypass runner cleanup for crash recovery tests


def _native_launch_barrier(stage: str) -> None:
    """Expose a deterministic test seam without forwarding it to the provider."""

    if os.environ.get(NATIVE_LAUNCH_BARRIER_STAGE_ENV) != stage:
        return
    ready_value = os.environ.get(NATIVE_LAUNCH_BARRIER_READY_ENV)
    release_value = os.environ.get(NATIVE_LAUNCH_BARRIER_RELEASE_ENV)
    if not ready_value or not release_value:
        raise SystemExit("native launch barrier requires ready and release paths")
    ready = Path(ready_value).resolve()
    release = Path(release_value).resolve()
    ready.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_text(ready, f"{stage}\n")
    deadline = time.monotonic() + 15.0
    while not release.is_file():
        if time.monotonic() >= deadline:
            raise SystemExit(f"native launch barrier timed out at {stage}")
        time.sleep(0.01)


def _terminate_unreleased_native_child(process: subprocess.Popen[str]) -> bool:
    """Close the authorization pipe and synchronously reap an untrusted child."""

    if process.stdin is not None:
        with contextlib.suppress(BrokenPipeError, OSError):
            process.stdin.close()
    terminated = process.poll() is not None
    if not terminated:
        with contextlib.suppress(OSError):
            process.terminate()
        try:
            process.wait(timeout=2.0)
        except subprocess.TimeoutExpired:
            with contextlib.suppress(OSError):
                process.kill()
            try:
                process.wait(timeout=2.0)
            except subprocess.TimeoutExpired:
                return False
        terminated = process.poll() is not None
    if process.stdout is not None:
        with contextlib.suppress(OSError):
            process.stdout.close()
    return terminated


def load_env_file(path: Path | None, env: dict[str, str]) -> dict[str, str]:
    if not path or not path.is_file():
        return env
    loaded = dict(env)
    for raw_line in path.read_text(encoding="utf-8-sig").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        if not key or key in loaded:
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
            value = value[1:-1]
        loaded[key] = value
    return loaded


def provider_env_key(actor: dict[str, Any]) -> str | None:
    configured = actor.get("env_key")
    if configured:
        return str(configured)
    # Backward compatibility for v2 projects created before provider catalogs.
    if actor.get("provider") == "longcat":
        return "LONGCAT_API_KEY"
    return None


_WORKER_ENV_ALLOWLIST = {
    "COMSPEC",
    "HOMEDRIVE",
    "HOMEPATH",
    "LANG",
    "LOCALAPPDATA",
    "NUMBER_OF_PROCESSORS",
    "OS",
    "PATH",
    "PATHEXT",
    "PROGRAMDATA",
    "PROGRAMFILES",
    "PROGRAMFILES(X86)",
    "SYSTEMDRIVE",
    "SYSTEMROOT",
    "TEMP",
    "TMP",
    "USERDOMAIN",
    "USERNAME",
    "USERPROFILE",
    "WINDIR",
    # Test/deployment command selection is configuration, not a credential.
    "COSTMARSHAL_CODEX_BIN",
    "COSTMARSHAL_CODEX_COMMAND_JSON",
}


def _isolated_codex_home(layout: ProjectLayout, actor: dict[str, Any], inherited: dict[str, str]) -> Path:
    """Create a credential-free Codex home containing only this actor's profile."""

    target = layout.project_dir / "actor-homes" / slugify(str(actor["id"]), "actor")
    target.mkdir(parents=True, exist_ok=True)
    profile = actor.get("profile")
    binding = actor.get("profile_binding")
    if actor.get("role") == "agent" and binding is None:
        raise SystemExit("provider profile binding is required for an agent actor")
    if binding is not None:
        try:
            validate_profile_binding(binding, require_available=True)
            payload = verify_profile_snapshot(layout.root, binding)
            destination = target / (f"{profile}.config.toml" if profile else "config.toml")
            install_bound_copy(destination, payload, binding)
        except ProfileBindingError as exc:
            raise SystemExit(f"provider profile binding failed closed: {exc}") from exc
        # Authentication is a separate credential channel; its bytes are not
        # provider endpoint/config identity and are never included in the
        # profile snapshot.
        source_home = inherited.get("CODEX_HOME")
        if actor.get("tier") == "high" and source_home:
            source = Path(source_home).expanduser() / "auth.json"
            destination = target / "auth.json"
            if source.is_file() and not source.is_symlink() and not destination.exists():
                shutil.copyfile(source, destination)
        return target.resolve()
    source_home = inherited.get("CODEX_HOME")
    if profile and source_home:
        source = Path(source_home).expanduser() / f"{profile}.config.toml"
        destination = target / source.name
        if source.is_file() and not destination.exists():
            shutil.copyfile(source, destination)
    if actor.get("tier") == "high" and source_home:
        for name in ("config.toml", "auth.json"):
            source = Path(source_home).expanduser() / name
            destination = target / name
            if source.is_file() and not destination.exists():
                shutil.copyfile(source, destination)
    return target.resolve()


def isolated_actor_env(
    project: dict[str, Any],
    actor: dict[str, Any],
    *,
    layout: ProjectLayout | None = None,
) -> tuple[dict[str, str], tuple[str, ...]]:
    """Load only this actor's provider key and return values to redact.

    Every key present in the configured secrets file is first removed from the
    inherited process environment. This prevents a low/medium provider from
    observing another provider's credential merely because the scheduler was
    launched from a shell that already loaded the same dotenv file.
    """

    inherited = dict(os.environ)
    is_untrusted_worker = actor.get("role") == "agent"
    env = (
        {key: value for key, value in inherited.items() if key.upper() in _WORKER_ENV_ALLOWLIST}
        if is_untrusted_worker
        else dict(inherited)
    )
    env_key = provider_env_key(actor)
    try:
        catalog_keys = {
            str(row["env_key"])
            for row in project_provider_catalog(project)["providers"]
            if row.get("env_key")
        }
    except RoutingValidationError as exc:
        raise SystemExit(f"provider secret isolation failed: {exc}") from exc
    for key in catalog_keys:
        if key != env_key:
            env.pop(key, None)
    # This path can reveal a file containing every provider credential. The
    # host runner reads it before spawning the provider process.
    env.pop("COSTMARSHAL_SECRETS_FILE", None)
    path = default_secrets_file(project)
    if path and layout is not None:
        workspace = workspace_path(layout, project)
        for forbidden_root, label in ((workspace, "actor workspace"), (layout.root.resolve(), "CostMarshal runtime")):
            try:
                path.resolve().relative_to(forbidden_root)
            except ValueError:
                continue
            raise SystemExit(f"provider secret isolation failed: secrets file is inside the {label}")
    if not path or not path.is_file():
        if env_key and inherited.get(env_key):
            env[env_key] = inherited[env_key]
        if is_untrusted_worker and layout is not None:
            env["CODEX_HOME"] = str(_isolated_codex_home(layout, actor, inherited))
        return env, ((env[env_key],) if env_key and env.get(env_key) else ())
    try:
        secrets = parse_secrets_text(path.read_text(encoding="utf-8-sig"))
        inherited_selected = inherited.get(env_key) if env_key else None
        for key in secrets:
            if key != env_key:
                env.pop(key, None)
        if env_key and not inherited_selected:
            env.update(provider_env_from_secrets(secrets, env_key))
        elif env_key and inherited_selected:
            env[env_key] = inherited_selected
    except (OSError, SecurityValidationError) as exc:
        raise SystemExit(f"provider secret isolation failed: {exc}") from exc
    redaction_values = {value for value in secrets.values() if value}
    if env_key and env.get(env_key):
        redaction_values.add(env[env_key])
    if is_untrusted_worker and layout is not None:
        env["CODEX_HOME"] = str(_isolated_codex_home(layout, actor, inherited))
    return env, tuple(redaction_values)


def default_secrets_file(project: dict[str, Any]) -> Path | None:
    configured = project.get("secrets_file") or os.environ.get("COSTMARSHAL_SECRETS_FILE")
    if configured:
        return Path(str(configured)).expanduser().resolve()
    codex_home = os.environ.get("CODEX_HOME")
    if codex_home:
        candidate = Path(codex_home).expanduser() / ".sandbox-secrets" / "costmarshal.env"
        if candidate.is_file():
            return candidate.resolve()
    return None


def resolve_codex_command(actor: dict[str, Any]) -> list[str]:
    prefix = (actor.get("runner") or {}).get("command_prefix")
    env_prefix = os.environ.get("COSTMARSHAL_CODEX_COMMAND_JSON")
    if env_prefix:
        parsed = json.loads(env_prefix)
        if not isinstance(parsed, list) or not parsed or not all(isinstance(item, str) for item in parsed):
            raise SystemExit("COSTMARSHAL_CODEX_COMMAND_JSON must be a non-empty JSON string array")
        return parsed
    if isinstance(prefix, list) and prefix and all(isinstance(item, str) for item in prefix):
        return list(prefix)
    configured = (actor.get("runner") or {}).get("executable") or os.environ.get("COSTMARSHAL_CODEX_BIN")
    if configured:
        return [str(configured)]
    candidates = ["codex.cmd", "codex"] if os.name == "nt" else ["codex"]
    for candidate in candidates:
        resolved = shutil.which(candidate)
        if resolved:
            return [resolved]
    return [candidates[0]]


def process_argv(argv: list[str]) -> list[str]:
    executable = argv[0].lower()
    if os.name == "nt" and executable.endswith((".cmd", ".bat")):
        comspec = os.environ.get("COMSPEC") or "cmd.exe"
        return [comspec, "/d", "/s", "/c", subprocess.list2cmdline(argv)]
    return argv


def workspace_path(layout: ProjectLayout, project: dict[str, Any]) -> Path:
    # ``source_project`` is explicitly a read-only reference in the v2 project
    # contract. Older project files may omit ``workspace``; never turn that
    # omission into write access to the source project.
    configured = project.get("workspace")
    if not configured:
        raise SystemExit("project workspace is missing; source_project remains read-only and cannot be used as an actor workspace")
    resolved = Path(str(configured)).expanduser().resolve()
    if not resolved.is_dir():
        raise SystemExit(f"project workspace not found: {resolved}")
    return resolved


def validate_bound_actor_prompt(
    actor: dict[str, Any],
    prompt: Path,
) -> str:
    """Return the one immutable prompt payload admitted by the scheduler."""

    binding = actor.get("prompt_binding")
    if not isinstance(binding, dict):
        raise SystemExit("worker launch rejected: immutable prompt binding is missing")
    if binding.get("schema") != "costmarshal-context-prompt-binding-v1":
        raise SystemExit("worker launch rejected: prompt binding schema is invalid")
    try:
        payload = prompt.read_bytes()
    except OSError as exc:
        raise SystemExit(f"worker launch rejected: actor prompt is unreadable: {exc}") from exc
    expected_sha256 = "sha256:" + hashlib.sha256(payload).hexdigest()
    collaboration_sha256 = (
        actor.get("collaboration_contract") or {}
    ).get("contract_sha256")
    profile_sha256 = (actor.get("profile_binding") or {}).get("sha256")
    if (
        binding.get("attempt_id") != actor.get("attempt_id")
        or binding.get("profile_sha256") != profile_sha256
        or binding.get("collaboration_contract_sha256") != collaboration_sha256
        or binding.get("size_bytes") != len(payload)
        or binding.get("sha256") != expected_sha256
    ):
        raise SystemExit("worker launch rejected: actor prompt binding does not match admitted bytes")
    try:
        return payload.decode("utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        raise SystemExit("worker launch rejected: actor prompt is not valid UTF-8") from exc


def _collaboration_contract_sha256(contract: dict[str, Any]) -> str:
    body = dict(contract)
    body.pop("contract_sha256", None)
    encoded = json.dumps(
        body,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def _required_collaboration_contract(
    layout: ProjectLayout,
    actor: dict[str, Any],
    task: dict[str, Any],
) -> dict[str, Any]:
    contract = actor.get("collaboration_contract")
    if not isinstance(contract, dict) or contract.get("schema") != "costmarshal-context-access-contract-v1":
        raise SystemExit("required worker launch rejected: collaboration contract is missing")
    if task.get("collaboration_contract") != contract:
        raise SystemExit("required worker launch rejected: actor/task collaboration contracts differ")
    if contract.get("project_id") in {None, ""} or contract.get("task_id") != task.get("id"):
        raise SystemExit("required worker launch rejected: collaboration contract task binding is invalid")
    if contract.get("contract_sha256") != _collaboration_contract_sha256(contract):
        raise SystemExit("required worker launch rejected: collaboration contract hash is invalid")
    base_sha = contract.get("base_sha")
    if not isinstance(base_sha, str) or not re.fullmatch(r"(?:[0-9a-f]{40}|[0-9a-f]{64})", base_sha):
        raise SystemExit("required worker launch rejected: collaboration base SHA is invalid")
    context_paths = contract.get("context_paths")
    write_scope = contract.get("write_scope")
    if not isinstance(context_paths, list) or not isinstance(write_scope, list):
        raise SystemExit("required worker launch rejected: collaboration path sets are invalid")
    current_attempt = next(
        (
            row
            for row in task.get("attempts") or []
            if row.get("attempt_id") == actor.get("attempt_id")
        ),
        None,
    )
    if (
        not isinstance(current_attempt, dict)
        or current_attempt.get("collaboration_contract_sha256")
        != contract.get("contract_sha256")
        or current_attempt.get("prompt_binding") != actor.get("prompt_binding")
    ):
        raise SystemExit("required worker launch rejected: attempt collaboration binding is invalid")
    return contract


def _projection_receipt(
    projection_root: Path,
    contract: dict[str, Any],
    *,
    incoming_change_manifest_sha256: str | None = None,
    expected_runtime_receipt: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if projection_root.is_symlink() or not projection_root.is_dir():
        raise SystemExit("required worker launch rejected: projection root is invalid")
    manifest_path = projection_root / "manifest.json"
    files_root = projection_root / "files"
    if manifest_path.is_symlink() or files_root.is_symlink() or not files_root.is_dir():
        raise SystemExit("required worker launch rejected: projection artifact layout is invalid")
    try:
        manifest_payload = manifest_path.read_bytes()
        manifest = json.loads(manifest_payload.decode("utf-8", errors="strict"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SystemExit("required worker launch rejected: projection manifest is unreadable") from exc
    if not isinstance(manifest, dict):
        raise SystemExit("required worker launch rejected: projection manifest is invalid")
    canonical_manifest = (
        json.dumps(
            manifest,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
        + b"\n"
    )
    if manifest_payload != canonical_manifest:
        raise SystemExit("required worker launch rejected: projection manifest is not canonical")
    body = dict(manifest)
    manifest_sha256 = body.pop("manifest_sha256", None)
    canonical_body = json.dumps(
        body,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    observed_manifest_sha256 = "sha256:" + hashlib.sha256(canonical_body).hexdigest()
    if (
        manifest_sha256 != observed_manifest_sha256
        or manifest.get("kind") != "costmarshal-context-projection"
        or manifest.get("base_sha") != contract.get("base_sha")
        or manifest.get("allowlist") != contract.get("context_paths")
    ):
        raise SystemExit("required worker launch rejected: projection manifest binding is invalid")
    receipt = {
        "schema": "costmarshal-context-projection-receipt-v1",
        "artifact_root": str(projection_root),
        "files_root": str(files_root),
        "manifest_path": str(manifest_path),
        "manifest_sha256": observed_manifest_sha256,
        "base_sha": manifest.get("base_sha"),
        "allowlist": manifest.get("allowlist"),
        "file_count": manifest.get("file_count"),
        "total_size_bytes": manifest.get("total_size_bytes"),
        "collaboration_contract_sha256": contract.get("contract_sha256"),
        "incoming_change_manifest_sha256": incoming_change_manifest_sha256,
    }
    if expected_runtime_receipt is not None and expected_runtime_receipt != receipt:
        raise SystemExit("required worker launch rejected: persisted projection receipt changed")
    return receipt


def _incoming_change_artifact(
    layout: ProjectLayout,
    project: dict[str, Any],
    actor: dict[str, Any],
    task: dict[str, Any],
    contract: dict[str, Any],
    write_scope: tuple[str, ...],
) -> dict[str, Any] | None:
    attempts = task.get("attempts") or []
    current_index = next(
        (
            index
            for index, row in enumerate(attempts)
            if row.get("attempt_id") == actor.get("attempt_id")
        ),
        None,
    )
    if current_index in {None, 0}:
        return None
    predecessor = attempts[int(current_index) - 1]
    if (
        predecessor.get("accepted_by_leader") is not False
        or predecessor.get("recorded_result_status") not in {"failed", "escalate"}
        or not isinstance(predecessor.get("leader_result_id"), str)
    ):
        raise SystemExit(
            "required worker continuation rejected: predecessor lacks an explicit leader rejection"
        )
    if not write_scope:
        return None
    receipt = predecessor.get("change_artifact")
    if not isinstance(receipt, dict) or receipt.get("schema") != "costmarshal-change-artifact-receipt-v1":
        raise SystemExit(
            "required worker continuation rejected: predecessor change artifact is missing"
        )
    expected_root = (
        layout.root
        / "task-change-artifacts"
        / slugify(str(project.get("project_id") or "project"), "project")
        / slugify(str(actor.get("task_id") or "task"), "task")
    ).resolve()
    manifest = receipt.get("manifest")
    if not isinstance(manifest, dict):
        raise SystemExit("required worker continuation rejected: predecessor manifest is invalid")
    manifest_sha256 = receipt.get("manifest_sha256")
    body = dict(manifest)
    embedded_sha256 = body.pop("manifest_sha256", None)
    canonical_body = json.dumps(
        body,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    observed_sha256 = "sha256:" + hashlib.sha256(canonical_body).hexdigest()
    expected_manifest_path = (
        expected_root / "manifests" / f"{observed_sha256.removeprefix('sha256:')}.json"
    )
    try:
        stored_manifest = expected_manifest_path.read_bytes()
    except OSError as exc:
        raise SystemExit(
            "required worker continuation rejected: predecessor manifest bytes are unavailable"
        ) from exc
    if (
        embedded_sha256 != observed_sha256
        or manifest_sha256 != observed_sha256
        or receipt.get("artifact_root") != str(expected_root)
        or receipt.get("manifest_path") != str(expected_manifest_path)
        or stored_manifest != canonical_body
        or receipt.get("base_sha") != contract.get("base_sha")
        or receipt.get("write_scope") != list(write_scope)
        or receipt.get("collaboration_contract_sha256") != contract.get("contract_sha256")
        or manifest.get("base_sha") != contract.get("base_sha")
        or manifest.get("write_scope") != list(write_scope)
        or manifest.get("change_count") != receipt.get("change_count")
        or manifest.get("total_upsert_bytes") != receipt.get("total_upsert_bytes")
    ):
        raise SystemExit(
            "required worker continuation rejected: predecessor change artifact binding is invalid"
        )
    return receipt


def _canonical_sha256(value: Any) -> str:
    payload = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def _persist_immutable_payload(root: Path, digest: str, payload: bytes) -> Path:
    if digest != "sha256:" + hashlib.sha256(payload).hexdigest():
        raise SystemExit("immutable payload does not match its content address")
    root.mkdir(parents=True, exist_ok=True)
    path = root / digest.removeprefix("sha256:")
    if path.exists():
        if path.is_symlink() or not path.is_file() or path.read_bytes() != payload:
            raise SystemExit("content-addressed payload already exists with different bytes")
        return path
    descriptor, temporary_name = tempfile.mkstemp(prefix=".incoming-", dir=str(root))
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.link(temporary, path)
        except FileExistsError:
            if path.is_symlink() or not path.is_file() or path.read_bytes() != payload:
                raise SystemExit("content-addressed payload publication raced with different bytes")
        except OSError as exc:
            # Windows can reject a hard-link target at the legacy MAX_PATH
            # boundary even though the already-open temporary file is valid.
            # rename is atomic and non-overwriting on Windows, retaining the
            # same publish-if-absent semantics under the attempt lock.
            if os.name != "nt" or getattr(exc, "winerror", None) not in {3, 206}:
                raise
            try:
                os.rename(temporary, path)
            except FileExistsError:
                if path.is_symlink() or not path.is_file() or path.read_bytes() != payload:
                    raise SystemExit(
                        "content-addressed payload publication raced with different bytes"
                    )
    finally:
        with contextlib.suppress(FileNotFoundError):
            temporary.unlink()
    return path


def _canonical_json_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def _redact_provider_events(
    events: Iterable[dict[str, Any]],
    secret_values: Iterable[str],
) -> list[dict[str, Any]]:
    secrets_to_remove = tuple(value for value in secret_values if value)

    def redact_value(value: Any) -> Any:
        if isinstance(value, str):
            safe_value = redact_secret_values(value, secrets_to_remove)
            if any(secret in safe_value for secret in secrets_to_remove):
                raise SystemExit("provider event redaction failed closed")
            return safe_value
        if isinstance(value, list):
            return [redact_value(item) for item in value]
        if isinstance(value, dict):
            safe_dict: dict[str, Any] = {}
            for key, item in value.items():
                safe_key = redact_value(key) if isinstance(key, str) else key
                if not isinstance(safe_key, str) or safe_key in safe_dict:
                    raise SystemExit("provider event redaction produced an invalid or duplicate key")
                safe_dict[safe_key] = redact_value(item)
            return safe_dict
        return value

    redacted: list[dict[str, Any]] = []
    for event in events:
        parsed = redact_value(event)
        if not isinstance(parsed, dict):
            raise SystemExit("provider event redaction changed its object contract")
        redacted.append(parsed)
    return redacted


def _provider_completion_root(
    layout: ProjectLayout,
    project: dict[str, Any],
    actor: dict[str, Any],
) -> Path:
    return Path(os.path.abspath(
        layout.root
        / "provider-completions"
        / slugify(str(project.get("project_id") or "project"), "project")
        / slugify(str(actor.get("attempt_id") or actor.get("id") or "attempt"), "attempt")
    ))


def _is_link_or_reparse(path: Path, info: os.stat_result) -> bool:
    return stat.S_ISLNK(info.st_mode) or bool(
        getattr(info, "st_file_attributes", 0) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    )


def _ensure_completion_directory(
    authority_root: Path,
    directory: Path,
    *,
    create: bool = True,
) -> None:
    authority = Path(os.path.abspath(authority_root))
    candidate = Path(os.path.abspath(directory))
    try:
        relative = candidate.relative_to(authority)
    except ValueError as exc:
        raise SystemExit("durable provider completion path escaped its authority root") from exc
    current = authority
    for part in relative.parts:
        current = current / part
        try:
            info = current.lstat()
        except FileNotFoundError:
            if not create:
                raise SystemExit("durable provider completion directory is missing")
            try:
                current.mkdir()
            except FileExistsError:
                pass
            info = current.lstat()
        except OSError as exc:
            raise SystemExit("durable provider completion directory is unreadable") from exc
        if not stat.S_ISDIR(info.st_mode) or _is_link_or_reparse(current, info):
            raise SystemExit("durable provider completion directory contains a link or reparse point")


def _read_completion_cas(
    path_value: Any,
    *,
    expected_path: Path,
    digest: str,
    max_bytes: int,
    label: str,
    authority_root: Path,
) -> bytes:
    if not isinstance(path_value, str) or not re.fullmatch(r"sha256:[0-9a-f]{64}", digest):
        raise SystemExit(f"durable provider completion {label} receipt is invalid")
    try:
        path = Path(os.path.abspath(path_value))
        expected = Path(os.path.abspath(expected_path))
        if os.path.normcase(os.fspath(path)) != os.path.normcase(os.fspath(expected)):
            raise ValueError("content-addressed path mismatch")
        _ensure_completion_directory(authority_root, expected.parent, create=False)
        before = path.lstat()
        if not stat.S_ISREG(before.st_mode) or _is_link_or_reparse(path, before):
            raise ValueError("content-addressed payload is not a regular file")
        if before.st_size < 1 or before.st_size > max_bytes:
            raise ValueError("content-addressed payload size is invalid")
        payload = path.read_bytes()
        after = path.stat()
        if (
            (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
            != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
            or len(payload) != after.st_size
        ):
            raise ValueError("content-addressed payload changed during read")
    except (OSError, ValueError) as exc:
        raise SystemExit(f"durable provider completion {label} CAS is unavailable: {exc}") from exc
    if "sha256:" + hashlib.sha256(payload).hexdigest() != digest:
        raise SystemExit(f"durable provider completion {label} CAS hash mismatch")
    return payload


def _completion_attempt(task: dict[str, Any], actor: dict[str, Any]) -> dict[str, Any]:
    matches = [
        row
        for row in task.get("attempts") or []
        if row.get("attempt_id") == actor.get("attempt_id")
        and row.get("actor_id") == actor.get("id")
    ]
    if len(matches) != 1 or (task.get("attempts") or [])[-1] is not matches[0]:
        raise SystemExit("durable provider completion is not bound to the current attempt")
    return matches[0]


def _load_provider_completion(
    layout: ProjectLayout,
    project: dict[str, Any],
    actor: dict[str, Any],
    *,
    semantic_contract: dict[str, Any] | None,
    attempt_input: dict[str, Any] | None,
    prompt_binding: dict[str, Any] | None,
    projection_receipt: dict[str, Any] | None,
) -> tuple[dict[str, Any], bytes, list[dict[str, Any]]] | None:
    task = load_task(layout, str(actor.get("task_id") or ""))
    attempt = _completion_attempt(task, actor)
    runtime = actor.get("runtime") or {}
    candidates = [
        value
        for value in (attempt.get("provider_completion"), runtime.get("provider_completion"))
        if value is not None
    ]
    marked = (
        runtime.get("provider_execution_state") == PROVIDER_COMPLETION_PENDING
        or attempt.get("provider_execution_state") == PROVIDER_COMPLETION_PENDING
    )
    if not candidates:
        if marked:
            raise SystemExit("durable provider completion marker has no receipt")
        return None
    if any(not isinstance(value, dict) for value in candidates) or any(
        value != candidates[0] for value in candidates[1:]
    ):
        raise SystemExit("durable provider completion actor/task receipts conflict")
    receipt = dict(candidates[0])
    receipt_sha256 = receipt.pop("receipt_sha256", None)
    receipt_path = receipt.pop("path", None)
    if not isinstance(receipt_sha256, str):
        raise SystemExit("durable provider completion receipt hash is missing")
    root = _provider_completion_root(layout, project, actor)
    receipt_payload = _read_completion_cas(
        receipt_path,
        expected_path=root / "c" / receipt_sha256.removeprefix("sha256:"),
        digest=receipt_sha256,
        max_bytes=128 * 1024,
        label="receipt",
        authority_root=layout.root,
    )
    if receipt_payload != _canonical_json_bytes(receipt):
        raise SystemExit("durable provider completion receipt bytes are non-canonical")
    expected_bindings = {
        "schema_version": 1,
        "kind": "costmarshal-provider-completion",
        "task_id": actor.get("task_id"),
        "attempt_id": actor.get("attempt_id"),
        "actor_id": actor.get("id"),
        "launch_token_sha256": hashlib.sha256(
            str(actor.get("launch_token") or "").encode("utf-8")
        ).hexdigest(),
        "provider": actor.get("provider"),
        "tier": actor.get("tier"),
        "model": actor.get("model"),
        "profile": actor.get("profile"),
        "profile_sha256": (actor.get("profile_binding") or {}).get("sha256"),
        "collaboration_contract_sha256": (semantic_contract or {}).get("contract_sha256"),
        "attempt_input_sha256": (attempt_input or {}).get("attempt_input_sha256"),
        "semantic_prompt_sha256": (prompt_binding or {}).get("prompt_sha256"),
        "context_projection_manifest_sha256": (projection_receipt or {}).get("manifest_sha256"),
        "isolation_backend": runtime.get("isolation_backend"),
        "container_name": runtime.get("container_name"),
        "container_id": runtime.get("container_id"),
        "container_command_sha256": _canonical_sha256(runtime.get("container_command") or []),
        "isolation_attestation_sha256": _canonical_sha256(runtime.get("isolation_attestation")),
    }
    if any(receipt.get(key) != value for key, value in expected_bindings.items()):
        raise SystemExit("durable provider completion identity binding mismatch")
    report_row = receipt.get("report")
    events_row = receipt.get("events")
    if not isinstance(report_row, dict) or not isinstance(events_row, dict):
        raise SystemExit("durable provider completion report/events receipts are missing")
    report_digest = str(report_row.get("sha256") or "")
    events_digest = str(events_row.get("sha256") or "")
    report_bytes = _read_completion_cas(
        report_row.get("path"),
        expected_path=root / "r" / report_digest.removeprefix("sha256:"),
        digest=report_digest,
        max_bytes=1024 * 1024,
        label="report",
        authority_root=layout.root,
    )
    events_bytes = _read_completion_cas(
        events_row.get("path"),
        expected_path=root / "e" / events_digest.removeprefix("sha256:"),
        digest=events_digest,
        max_bytes=2 * 1024 * 1024,
        label="events",
        authority_root=layout.root,
    )
    if report_row.get("size_bytes") != len(report_bytes) or events_row.get("size_bytes") != len(events_bytes):
        raise SystemExit("durable provider completion payload size mismatch")
    try:
        report_bytes.decode("utf-8", errors="strict")
        event_document = json.loads(events_bytes.decode("utf-8", errors="strict"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SystemExit("durable provider completion payload encoding is invalid") from exc
    events = event_document.get("events") if isinstance(event_document, dict) else None
    if (
        event_document.get("schema") != "costmarshal-provider-events-v1"
        or not isinstance(events, list)
        or len(events) > 4096
        or any(not isinstance(event, dict) for event in events)
        or _canonical_json_bytes(event_document) != events_bytes
        or events_row.get("count") != len(events)
    ):
        raise SystemExit("durable provider completion events CAS is invalid")
    if type(receipt.get("exit_code")) is not int:
        raise SystemExit("durable provider completion exit code is invalid")
    return ({**receipt, "receipt_sha256": receipt_sha256, "path": receipt_path}, report_bytes, events)


def _persist_provider_completion(
    layout: ProjectLayout,
    project: dict[str, Any],
    actor: dict[str, Any],
    *,
    wait_receipt: Any,
    validated_output: Any,
    safe_report_bytes: bytes,
    events: list[dict[str, Any]],
    semantic_contract: dict[str, Any] | None,
    attempt_input: dict[str, Any] | None,
    prompt_binding: dict[str, Any] | None,
    projection_receipt: dict[str, Any] | None,
    attempt_id: str | None,
    launch_token: str | None,
) -> dict[str, Any]:
    event_document = {"schema": "costmarshal-provider-events-v1", "events": events}
    events_bytes = _canonical_json_bytes(event_document)
    root = _provider_completion_root(layout, project, actor)
    report_digest = "sha256:" + hashlib.sha256(safe_report_bytes).hexdigest()
    events_digest = "sha256:" + hashlib.sha256(events_bytes).hexdigest()
    # Single-letter leaf directories keep Windows paths below legacy MAX_PATH
    # even when the project and attempt identities are long.
    _ensure_completion_directory(layout.root, root / "r")
    _ensure_completion_directory(layout.root, root / "e")
    _ensure_completion_directory(layout.root, root / "c")
    report_path = _persist_immutable_payload(root / "r", report_digest, safe_report_bytes)
    events_path = _persist_immutable_payload(root / "e", events_digest, events_bytes)
    durable_actor = load_actor(layout, str(actor["id"]))
    runtime = durable_actor.get("runtime") or {}
    command = runtime.get("container_command") or []
    body = {
        "schema_version": 1,
        "kind": "costmarshal-provider-completion",
        "task_id": actor.get("task_id"),
        "attempt_id": actor.get("attempt_id"),
        "actor_id": actor.get("id"),
        "launch_token_sha256": hashlib.sha256(str(launch_token or "").encode("utf-8")).hexdigest(),
        "provider": actor.get("provider"),
        "tier": actor.get("tier"),
        "model": actor.get("model"),
        "profile": actor.get("profile"),
        "profile_sha256": (actor.get("profile_binding") or {}).get("sha256"),
        "collaboration_contract_sha256": (semantic_contract or {}).get("contract_sha256"),
        "attempt_input_sha256": (attempt_input or {}).get("attempt_input_sha256"),
        "semantic_prompt_sha256": (prompt_binding or {}).get("prompt_sha256"),
        "context_projection_manifest_sha256": (projection_receipt or {}).get("manifest_sha256"),
        "isolation_backend": runtime.get("isolation_backend"),
        "container_name": runtime.get("container_name"),
        "container_id": runtime.get("container_id"),
        "container_command_sha256": _canonical_sha256(command),
        "isolation_attestation_sha256": _canonical_sha256(runtime.get("isolation_attestation")),
        "exit_code": int(wait_receipt.exit_code),
        "stdout_bytes": int(getattr(wait_receipt, "stdout_bytes", 0)),
        "stderr_bytes": int(getattr(wait_receipt, "stderr_bytes", 0)),
        "stderr_truncated": bool(getattr(wait_receipt, "stderr_truncated", False)),
        "worker_output_sha256": "sha256:" + str(validated_output.sha256).removeprefix("sha256:"),
        "report": {"path": str(report_path), "sha256": report_digest, "size_bytes": len(safe_report_bytes)},
        "events": {"path": str(events_path), "sha256": events_digest, "size_bytes": len(events_bytes), "count": len(events)},
    }
    receipt_bytes = _canonical_json_bytes(body)
    receipt_digest = "sha256:" + hashlib.sha256(receipt_bytes).hexdigest()
    receipt_path = _persist_immutable_payload(root / "c", receipt_digest, receipt_bytes)
    completion = {**body, "receipt_sha256": receipt_digest, "path": str(receipt_path)}

    def persist_completion() -> None:
        current_actor = _validate_worker_fence(
            layout,
            str(actor["id"]),
            attempt_id=attempt_id,
            launch_token=launch_token,
            allow_provider_started=True,
        )
        current_task = load_task(layout, str(current_actor.get("task_id")))
        current_attempt = _completion_attempt(current_task, current_actor)
        prior = current_attempt.get("provider_completion")
        if prior is not None and prior != completion:
            raise SystemExit("durable provider completion changed for the same attempt")
        current_attempt["provider_completion"] = completion
        current_attempt["provider_execution_state"] = PROVIDER_COMPLETION_PENDING
        save_task(layout, current_task)
        current_runtime = current_actor.setdefault("runtime", {})
        prior = current_runtime.get("provider_completion")
        if prior is not None and prior != completion:
            raise SystemExit("actor provider completion changed for the same attempt")
        current_runtime["provider_completion"] = completion
        current_runtime["provider_execution_state"] = PROVIDER_COMPLETION_PENDING
        current_runtime["oci_lifecycle_state"] = "finished"
        save_actor(layout, current_actor)
        append_event(
            layout,
            "actor_provider_completion_observed",
            actor_id=actor.get("id"),
            task_id=actor.get("task_id"),
            attempt_id=actor.get("attempt_id"),
            provider_completion_sha256=receipt_digest,
        )

    _control_mutation(
        layout,
        command_name="runner_observe_provider_completion",
        command_id=f"RUNNER-PROVIDER-COMPLETION-{attempt_id or actor.get('id')}",
        payload={
            "actor_id": actor.get("id"),
            "attempt_id": attempt_id,
            "provider_completion_sha256": receipt_digest,
        },
        mutate=persist_completion,
    )
    return completion


def _semantic_handoff_limits(
    task: dict[str, Any],
    *,
    bootstrap_prompt_size: int,
) -> HandoffLimits:
    input_tokens = int(task.get("estimated_input_tokens") or 0)
    output_tokens = int(task.get("estimated_output_tokens") or 0)
    framing_reserve = 256
    maximum_from_input = (
        input_tokens - bootstrap_prompt_size - framing_reserve - 1024
    ) // 2
    maximum_from_output = (output_tokens - 1) // 2
    maximum_handoff = min(4096, maximum_from_input, maximum_from_output)
    if maximum_handoff <= 0:
        raise HandoffContractError(
            "estimated token budgets cannot reserve a bounded successor handoff"
        )
    return HandoffLimits(
        max_handoff_bytes=maximum_handoff,
        continuation_input_reserve_tokens=(2 * maximum_handoff) + framing_reserve,
        handoff_output_reserve_tokens=maximum_handoff,
        prompt_framing_reserve_tokens=framing_reserve,
        max_route_steps=3,
    )


def _prepare_semantic_attempt(
    layout: ProjectLayout,
    project: dict[str, Any],
    actor: dict[str, Any],
    task: dict[str, Any],
    *,
    bootstrap_prompt_bytes: bytes,
    projection_receipt: dict[str, Any],
    incoming_change_artifact: dict[str, Any] | None,
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any], bytes] | None:
    """Build the single scheduler-first low/medium/high handoff protocol."""

    envelope = task.get("route_budget_envelope")
    planned_steps = envelope.get("planned_steps") if isinstance(envelope, dict) else None
    if not isinstance(planned_steps, list) or not planned_steps:
        return None
    if len(planned_steps) > 1 and (
        int(task.get("estimated_input_tokens") or 0) <= 0
        or int(task.get("estimated_output_tokens") or 0) <= 0
    ):
        raise SystemExit(
            "required multi-tier collaboration needs positive input/output token estimates"
        )
    try:
        limits = _semantic_handoff_limits(
            task,
            bootstrap_prompt_size=len(bootstrap_prompt_bytes),
        )
    except HandoffContractError as exc:
        if len(planned_steps) > 1:
            raise SystemExit(f"required multi-tier handoff budget failed closed: {exc}") from exc
        return None

    write_scope = sorted(str(path) for path in (actor.get("collaboration_contract") or {}).get("write_scope") or [])
    base_sha = str((actor.get("collaboration_contract") or {}).get("base_sha") or "")
    if write_scope:
        initial_changes = build_cumulative_change_manifest(
            base_sha=base_sha,
            write_scope=write_scope,
            operations=[],
        )
        initial_persisted = persist_change_artifact(
            (
                layout.root
                / "task-change-artifacts"
                / slugify(str(project.get("project_id") or "project"), "project")
                / slugify(str(actor.get("task_id") or "task"), "task")
            ).resolve(),
            initial_changes,
        )
        initial_manifest_sha256 = initial_persisted.manifest_sha256
    else:
        initial_manifest_sha256 = _canonical_sha256(
            {
                "schema_version": 1,
                "kind": "costmarshal-no-writes",
                "base_sha": base_sha,
                "write_scope": [],
            }
        )

    task_spec = {
        "title": task.get("title"),
        "purpose": task.get("purpose"),
        "task_type": task.get("task_type"),
        "risk": task.get("risk"),
        "difficulty": task.get("difficulty"),
        "acceptance": task.get("acceptance") or [],
        "required_capabilities": task.get("required_capabilities") or [],
    }
    try:
        semantic_contract = build_semantic_collaboration_contract(
            task_id=str(task["id"]),
            task_spec=task_spec,
            base_sha=base_sha,
            context_allowlist=sorted(str(path) for path in projection_receipt.get("allowlist") or []),
            context_manifest_sha256=str(projection_receipt["manifest_sha256"]),
            context_file_count=int(projection_receipt.get("file_count") or 0),
            context_total_size_bytes=int(projection_receipt.get("total_size_bytes") or 0),
            write_scope=write_scope,
            initial_change_manifest_sha256=initial_manifest_sha256,
            max_changes=4096,
            max_total_upsert_bytes=128 * 1024 * 1024,
            estimated_input_tokens=int(task.get("estimated_input_tokens") or 0),
            estimated_cached_input_tokens=int(task.get("estimated_cached_input_tokens") or 0),
            estimated_output_tokens=int(task.get("estimated_output_tokens") or 0),
            handoff_limits=limits,
            route_envelope_id=str(envelope.get("envelope_id") or ""),
            route_plan_fingerprint_sha256=str(envelope.get("plan_fingerprint") or ""),
            planned_steps=planned_steps,
        )
        existing_contract = task.get("handoff_contract")
        if existing_contract is not None:
            validate_semantic_collaboration_contract(existing_contract)
            if existing_contract != semantic_contract:
                raise HandoffContractError(
                    "stored handoff contract differs from the immutable route/context/token inputs"
                )
        attempts = task.get("attempts") or []
        current_index = next(
            index
            for index, row in enumerate(attempts)
            if row.get("attempt_id") == actor.get("attempt_id")
        )
        current_attempt = attempts[current_index]
        route_step_index = current_attempt.get("route_plan_step_index")
        if type(route_step_index) is not int:
            raise HandoffContractError("attempt has no immutable route step index")
        predecessor_capsule = None
        predecessor_result = None
        if route_step_index == 0:
            incoming_manifest_sha256 = initial_manifest_sha256
            incoming_change_count = 0
            incoming_total_bytes = 0
        else:
            predecessor_attempt = attempts[current_index - 1] if current_index > 0 else None
            if not isinstance(predecessor_attempt, dict):
                raise HandoffContractError("continuation has no immediate predecessor attempt")
            predecessor_capsule = predecessor_attempt.get("handoff_capsule")
            predecessor_result = predecessor_attempt.get("handoff_result_evidence")
            if not isinstance(predecessor_capsule, dict) or not isinstance(predecessor_result, dict):
                raise HandoffContractError(
                    "continuation requires a sealed predecessor capsule and trusted result evidence"
                )
            if write_scope:
                if incoming_change_artifact is None:
                    raise HandoffContractError("continuation cumulative change artifact is missing")
                incoming_manifest_sha256 = str(incoming_change_artifact["manifest_sha256"])
                incoming_change_count = int(incoming_change_artifact.get("change_count") or 0)
                incoming_total_bytes = int(incoming_change_artifact.get("total_upsert_bytes") or 0)
            else:
                if incoming_change_artifact is not None:
                    raise HandoffContractError(
                        "read-only continuation has an unexpected cumulative change artifact"
                    )
                predecessor_changes = predecessor_capsule.get("outgoing_changes")
                if not isinstance(predecessor_changes, dict):
                    raise HandoffContractError(
                        "read-only continuation predecessor has no cumulative change receipt"
                    )
                incoming_manifest_sha256 = str(
                    predecessor_changes.get("manifest_sha256") or ""
                )
                incoming_change_count = int(
                    predecessor_changes.get("change_count") or 0
                )
                incoming_total_bytes = int(
                    predecessor_changes.get("total_upsert_bytes") or 0
                )
                if incoming_change_count != 0 or incoming_total_bytes != 0:
                    raise HandoffContractError(
                        "read-only continuation predecessor claims workspace changes"
                    )
        attempt_input = build_attempt_input_contract(
            collaboration_contract=semantic_contract,
            attempt_id=str(actor["attempt_id"]),
            actor_id=str(actor["id"]),
            route_step_index=route_step_index,
            incoming_change_manifest_sha256=incoming_manifest_sha256,
            incoming_change_count=incoming_change_count,
            incoming_total_upsert_bytes=incoming_total_bytes,
            predecessor_handoff=predecessor_capsule,
            trusted_predecessor_result=predecessor_result,
        )
        bound_prompt_bytes = build_bound_prompt_bytes(
            attempt_input=attempt_input,
            task_prompt_bytes=bootstrap_prompt_bytes,
            predecessor_handoff=predecessor_capsule,
        )
        semantic_prompt_binding = build_semantic_prompt_binding(
            collaboration_contract=semantic_contract,
            attempt_input=attempt_input,
            prompt_bytes=bound_prompt_bytes,
            predecessor_handoff=predecessor_capsule,
        )
    except (HandoffContractError, StopIteration) as exc:
        raise SystemExit(f"required semantic handoff failed closed: {exc}") from exc
    return semantic_contract, attempt_input, semantic_prompt_binding, bound_prompt_bytes


def actor_execution_workspace(
    layout: ProjectLayout,
    project: dict[str, Any],
    actor: dict[str, Any],
) -> tuple[Path, str, tuple[str, ...], str | None]:
    """Return an isolated execution root and the enforced sandbox mode.

    Report-only workers see the source workspace read-only. Workers with write
    scope run in a detached git worktree and their diff is checked after exit;
    the source workspace is never the worker's writable root.
    """

    source = workspace_path(layout, project)
    if actor.get("role") == "leader":
        return source, str((actor.get("runner") or {}).get("sandbox") or "workspace-write"), (), None
    task_id = actor.get("task_id")
    task = load_task(layout, str(task_id)) if task_id else {}
    try:
        write_paths = normalize_path_list(
            task.get("allowed_paths") or task.get("claimed_paths") or [],
            kind="allowed",
        )
    except SecurityValidationError as exc:
        raise SystemExit(f"worker write scope is invalid: {exc}") from exc
    if (actor.get("isolation") or {}).get("mode") == "required":
        contract = _required_collaboration_contract(layout, actor, task)
        if contract.get("project_id") != project.get("project_id"):
            raise SystemExit("required worker project binding differs from the collaboration contract")
        if list(write_paths) != contract.get("write_scope"):
            raise SystemExit("required worker write scope differs from the frozen collaboration contract")
        incoming_change_artifact = _incoming_change_artifact(
            layout,
            project,
            actor,
            task,
            contract,
            write_paths,
        )
        incoming_change_manifest_sha256 = (
            incoming_change_artifact.get("manifest_sha256")
            if incoming_change_artifact is not None
            else None
        )
        attempt = str(actor.get("attempt_id") or actor["id"])
        destination = (
            layout.root
            / "worker-projections"
            / slugify(str(project.get("project_id") or "project"), "project")
            / slugify(attempt, "attempt")
        ).resolve()
        runtime_receipt = (actor.get("runtime") or {}).get("context_projection")
        provider_may_have_started = bool(
            (actor.get("runtime") or {}).get("provider_execution_state")
            in {"started", PROVIDER_COMPLETION_PENDING}
            or (actor.get("runtime") or {}).get("oci_lifecycle_state")
            in {"started", "finished", "uncertain_start", "uncertain_cleanup"}
        )
        try:
            if destination.exists() or destination.is_symlink():
                if provider_may_have_started:
                    if not isinstance(runtime_receipt, dict):
                        raise SystemExit(
                            "required worker recovery rejected: started projection lacks a durable receipt"
                        )
                    _projection_receipt(
                        destination,
                        contract,
                        incoming_change_manifest_sha256=incoming_change_manifest_sha256,
                        expected_runtime_receipt=runtime_receipt,
                    )
                    projected_files = destination / "files"
                else:
                    expected_manifest_sha256 = (
                        runtime_receipt.get("manifest_sha256")
                        if isinstance(runtime_receipt, dict)
                        else None
                    )
                    verification_projection: Path | None = None
                    if expected_manifest_sha256 is None:
                        # A hard exit may occur after atomic materialization but
                        # before its receipt transaction. Recompute the expected
                        # manifest from trusted Git objects instead of trusting a
                        # replaceable self-hash in the orphan directory.
                        verification_projection = destination.parent / (
                            f".{destination.name}.verify-{secrets.token_hex(12)}"
                        )
                        expected_projection = materialize_context_projection(
                            source,
                            base_sha=str(contract["base_sha"]),
                            allowlist=contract["context_paths"],
                            destination=verification_projection,
                            allow_empty=True,
                        )
                        expected_manifest_sha256 = str(
                            expected_projection.manifest["manifest_sha256"]
                        )
                    try:
                        if incoming_change_artifact is not None:
                            projected = apply_cumulative_change_artifact(
                                destination,
                                expected_base_sha=str(contract["base_sha"]),
                                expected_allowlist=contract["context_paths"],
                                expected_projection_manifest_sha256=str(
                                    expected_manifest_sha256
                                ),
                                write_scope=write_paths,
                                change_manifest=incoming_change_artifact["manifest"],
                                change_artifact_root=incoming_change_artifact[
                                    "artifact_root"
                                ],
                                expected_change_manifest_sha256=str(
                                    incoming_change_manifest_sha256
                                ),
                            )
                        else:
                            projected = verify_materialized_context_projection(
                                destination,
                                expected_base_sha=str(contract["base_sha"]),
                                expected_allowlist=contract["context_paths"],
                                expected_manifest_sha256=expected_manifest_sha256,
                            )
                    finally:
                        if verification_projection is not None:
                            shutil.rmtree(verification_projection, ignore_errors=True)
                    projected_files = projected.files_root
            else:
                projected = materialize_context_projection(
                    source,
                    base_sha=str(contract["base_sha"]),
                    allowlist=contract["context_paths"],
                    destination=destination,
                    allow_empty=True,
                )
                if incoming_change_artifact is not None:
                    projected = apply_cumulative_change_artifact(
                        projected.artifact_root,
                        expected_base_sha=str(contract["base_sha"]),
                        expected_allowlist=contract["context_paths"],
                        expected_projection_manifest_sha256=str(
                            projected.manifest["manifest_sha256"]
                        ),
                        write_scope=write_paths,
                        change_manifest=incoming_change_artifact["manifest"],
                        change_artifact_root=incoming_change_artifact["artifact_root"],
                        expected_change_manifest_sha256=str(
                            incoming_change_manifest_sha256
                        ),
                    )
                projected_files = projected.files_root
        except ContextProjectionError as exc:
            raise SystemExit(f"required worker context projection failed closed: {exc}") from exc
        if write_paths and hasattr(os, "getuid") and os.getuid() == 0:
            try:
                os.chown(projected_files, 65532, 65532)
                for child in projected_files.rglob("*"):
                    os.chown(child, 65532, 65532, follow_symlinks=False)
            except OSError as exc:
                raise SystemExit(
                    "unable to assign the context projection to the non-root OCI worker"
                ) from exc
        return (
            projected_files,
            "workspace-write" if write_paths else "read-only",
            write_paths,
            str(contract["base_sha"]),
        )
    if not write_paths:
        return source, "read-only", (), None

    try:
        git_root = Path(
            subprocess.check_output(
                ["git", "-C", str(source), "rev-parse", "--show-toplevel"],
                text=True,
                stderr=subprocess.STDOUT,
            ).strip()
        ).resolve()
        if git_root != source:
            raise SystemExit("writable worker workspace must be the git repository root")
        base_sha = subprocess.check_output(
            ["git", "-C", str(source), "rev-parse", "HEAD"],
            text=True,
            stderr=subprocess.STDOUT,
        ).strip()
        dirty = subprocess.check_output(
            ["git", "-C", str(source), "status", "--porcelain", "--untracked-files=all"],
            text=True,
            stderr=subprocess.STDOUT,
        )
        if dirty.strip():
            raise SystemExit("writable worker dispatch requires a clean git workspace")
    except FileNotFoundError as exc:
        raise SystemExit("git is required for isolated writable worker tasks") from exc
    except subprocess.CalledProcessError as exc:
        detail = (exc.output or "").strip()
        raise SystemExit(f"writable worker dispatch requires a git workspace: {detail}") from exc

    attempt = str(actor.get("attempt_id") or actor["id"])
    if (actor.get("isolation") or {}).get("mode") == "required":
        staging = (
            layout.root
            / "worker-worktrees"
            / slugify(str(project.get("project_id") or "project"), "project")
            / slugify(attempt, "attempt")
        ).resolve()
    else:
        staging = (layout.project_dir / "worktrees" / slugify(attempt, "attempt")).resolve()
    metadata_path = layout.project_dir / "worktrees" / f"{slugify(attempt, 'attempt')}.json"
    if metadata_path.is_file():
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        base_sha = str(metadata["base_sha"])
    else:
        atomic_write_json(metadata_path, {"schema_version": 1, "attempt_id": attempt, "base_sha": base_sha})
    if not staging.exists():
        staging.parent.mkdir(parents=True, exist_ok=True)
        try:
            subprocess.run(
                ["git", "-C", str(source), "worktree", "add", "--detach", str(staging), base_sha],
                check=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
            )
        except subprocess.CalledProcessError as exc:
            raise SystemExit(f"unable to create isolated worker worktree: {(exc.stdout or '').strip()}") from exc
    if (
        (actor.get("isolation") or {}).get("mode") == "required"
        and hasattr(os, "getuid")
        and os.getuid() == 0
    ):
        try:
            os.chown(staging, 65532, 65532)
            for child in staging.rglob("*"):
                os.chown(child, 65532, 65532, follow_symlinks=False)
        except OSError as exc:
            raise SystemExit("unable to assign the isolated worktree to the non-root OCI worker") from exc
    return staging, "workspace-write", write_paths, base_sha


def worktree_changed_paths(worktree: Path, base_sha: str) -> tuple[str, ...]:
    commands = (
        ["git", "-C", str(worktree), "diff", "--name-only", "--relative", base_sha],
        ["git", "-C", str(worktree), "ls-files", "--others", "--exclude-standard"],
        ["git", "-C", str(worktree), "ls-files", "--others", "--ignored", "--exclude-standard"],
    )
    paths: set[str] = set()
    for command in commands:
        output = subprocess.check_output(command, text=True, stderr=subprocess.STDOUT)
        paths.update(line.strip().replace("\\", "/") for line in output.splitlines() if line.strip())
    return tuple(sorted(paths))


def _path_is_within_scope(path: str, scopes: tuple[str, ...]) -> bool:
    folded = path.casefold().rstrip("/")
    return any(folded == scope.casefold().rstrip("/") or folded.startswith(scope.casefold().rstrip("/") + "/") for scope in scopes)


def report_path(layout: ProjectLayout, actor: dict[str, Any]) -> Path:
    task_id = actor.get("task_id")
    if task_id:
        return task_dir(layout, str(task_id)) / "attempts" / f"{slugify(str(actor['id']), 'actor')}.md"
    return layout.reports_dir / "manager-latest.md"


def publish_task_report(layout: ProjectLayout, actor: dict[str, Any], attempt_report: Path) -> None:
    task_id = actor.get("task_id")
    if task_id and attempt_report.is_file():
        atomic_write_bytes(
            task_dir(layout, str(task_id)) / "completion-report.md",
            attempt_report.read_bytes(),
        )


def build_codex_argv(
    layout: ProjectLayout,
    actor: dict[str, Any],
    project: dict[str, Any],
    report: Path,
    *,
    execution_workspace: Path | None = None,
    sandbox: str | None = None,
) -> list[str]:
    runner = actor.get("runner") or {}
    workspace = execution_workspace or workspace_path(layout, project)
    argv = resolve_codex_command(actor) + [
        "--ask-for-approval",
        str(runner.get("approval_policy") or "never"),
        "exec",
        "--ephemeral",
        "--skip-git-repo-check",
        "--sandbox",
        str(sandbox or runner.get("sandbox") or "workspace-write"),
        "--cd",
        str(workspace),
        "--json",
        "--output-last-message",
        str(report),
    ]
    # The leader is the only model actor that needs to inspect durable control
    # state. Task workers receive their brief on stdin and the host runner owns
    # report/mailbox writes, so granting them the runtime directory would let a
    # low-cost worker tamper with locks or forge scheduler messages.
    if actor.get("role") == "leader":
        insert_at = argv.index("--json")
        argv[insert_at:insert_at] = ["--add-dir", str(layout.project_dir)]
    profile = actor.get("profile")
    if profile:
        argv.extend(["--profile", str(profile)])
    model = actor.get("model")
    if model and model != "inherit":
        argv.extend(["--model", str(model)])
    argv.append("-")
    return argv


def walk_dicts(value: Any) -> Iterable[dict[str, Any]]:
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from walk_dicts(child)
    elif isinstance(value, list):
        for child in value:
            yield from walk_dicts(child)


def usage_details_from_events(events: list[dict[str, Any]]) -> tuple[int, int, int, bool]:
    """Return ordinary input, cached input, output, and pricing completeness.

    CostMarshal-native ``cached_input_tokens`` and Anthropic-style
    ``cache_read_input_tokens`` are separate from ordinary input. OpenAI-style
    ``input_tokens_details.cached_tokens`` is a subset of total input and is
    subtracted before pricing. Invalid subset metadata is ignored, which keeps
    the total input conservatively billed at the ordinary rate. Positive cache
    creation tokens make pricing incomplete because the canonical catalog does
    not yet carry a cache-write rate; callers must preserve the reservation.
    """

    best_input = 0
    best_cached_input = 0
    best_total_input = 0
    best_output = 0
    pricing_complete = True
    for event in events:
        for item in walk_dicts(event):
            input_value = item.get("input_tokens", item.get("prompt_tokens", 0))
            output_value = item.get("output_tokens", item.get("completion_tokens", 0))
            cached_value = item.get(
                "cached_input_tokens",
                item.get("cache_read_input_tokens", 0),
            )
            details = item.get("input_tokens_details", item.get("prompt_tokens_details"))
            detail_cached = details.get("cached_tokens", 0) if isinstance(details, dict) else 0
            cache_creation_value = item.get("cache_creation_input_tokens", 0)
            ordinary_candidate = 0
            cached_candidate = 0
            has_input_dimension = False
            if type(input_value) is int and input_value >= 0:
                has_input_dimension = True
                if (
                    type(detail_cached) is int
                    and 0 <= detail_cached <= input_value
                ):
                    ordinary_candidate = input_value - detail_cached
                    cached_candidate = detail_cached
                else:
                    ordinary_candidate = input_value
            if type(cached_value) is int and cached_value >= 0:
                if cached_value:
                    has_input_dimension = True
                cached_candidate = max(cached_candidate, cached_value)
            if type(cache_creation_value) is int and cache_creation_value > 0:
                # Anthropic reports cache writes outside ordinary input. Count
                # them in total usage, but do not pretend the ordinary input
                # rate proves their monetary cost.
                has_input_dimension = True
                ordinary_candidate += cache_creation_value
                pricing_complete = False
            candidate_total = ordinary_candidate + cached_candidate
            # Usage events are cumulative snapshots. Keep the ordinary/cached
            # split from one coherent snapshot instead of taking independent
            # maxima, which can double count when the cache ratio changes.
            if has_input_dimension and candidate_total >= best_total_input:
                best_input = ordinary_candidate
                best_cached_input = cached_candidate
                best_total_input = candidate_total
            if type(output_value) is int and output_value >= 0:
                best_output = max(best_output, output_value)
    return best_input, best_cached_input, best_output, pricing_complete


def usage_from_events(events: list[dict[str, Any]]) -> tuple[int, int, int]:
    """Backward-compatible token dimensions without the pricing-status bit."""

    input_tokens, cached_input_tokens, output_tokens, _ = usage_details_from_events(events)
    return input_tokens, cached_input_tokens, output_tokens


def scheduler_command(
    layout: ProjectLayout,
    actor: dict[str, Any],
    command: str,
    args: dict[str, Any],
    *,
    body: str,
    ) -> None:
    with project_write_lock(layout):
        send_message(
            layout,
            sender=actor["id"],
            recipient="scheduler",
            subject="scheduler.command",
            body=body,
            task_id=actor.get("task_id"),
            metadata={"command": command, "args": args},
        )


def _control_mutation(
    layout: ProjectLayout,
    *,
    command_name: str,
    command_id: str,
    payload: dict[str, Any],
    mutate: Callable[[], None],
) -> bool:
    """Apply a runner-owned state transition through the enabled backend."""

    with project_write_lock(layout):
        if not control_store_enabled(layout):
            mutate()
            return True
        with control_transaction(
            layout,
            command_name=command_name,
            command_id=command_id,
            payload=payload,
        ) as transaction:
            if transaction.replay:
                return False
            mutate()
        return True


def report_status(report: Path) -> str | None:
    if not report.is_file():
        return None
    text = report.read_text(encoding="utf-8", errors="replace")
    match = re.search(r"(?im)^\s*(?:[*_]{1,2})?status\s*:\s*(done|failed|escalate)\b", text)
    return match.group(1).lower() if match else None


def _completion_scheduler_command(
    *,
    task_id: str,
    actor_id: str,
    attempt_id: str | None,
    provider: str | None,
    returncode: int,
    collected_state: str,
    needs_escalation: bool,
    plan_allows_next: bool,
) -> tuple[str, dict[str, Any], str]:
    """Workers publish evidence only; continuation always remains leader-owned."""

    command_args = {
        "task": task_id,
        "actor": actor_id,
        "attempt": attempt_id,
        "state": "waiting_leader",
        "summary": f"{provider} worker exited {returncode}; report ready.",
    }
    if needs_escalation and plan_allows_next:
        body = (
            "Worker report is ready for leader review; the next admitted tier remains "
            "blocked until an explicit rejected result and escalation command."
        )
    elif needs_escalation:
        body = "The admitted route plan is exhausted; the worker report is ready for manager review."
    else:
        body = "Worker report is ready for manager review."
    return "collect_task", command_args, body


def _required_worker_bundle(
    layout: ProjectLayout,
    project: dict[str, Any],
    actor: dict[str, Any],
    *,
    execution_workspace: Path,
    workspace_mode: str,
    allow_credential_creation: bool = True,
    finalize_only: bool = False,
) -> tuple[WorkerExecutionSpec, list[str], tuple[str, ...]]:
    """Build the durable, attempt-scoped host side of the OCI exchange contract."""

    isolation = actor.get("isolation") or {}
    execution = isolation.get("execution") or {}
    attestation = isolation.get("attestation") or {}
    if isolation.get("mode") != "required" or attestation.get("strong_isolation") is not True:
        raise SystemExit("required worker bundle rejected: strong OCI attestation is missing")
    engine = str(execution.get("engine") or attestation.get("backend") or "")
    if engine not in {"docker", "podman"} or engine != str(attestation.get("backend") or ""):
        raise SystemExit("required worker bundle rejected: OCI backend attestation mismatch")
    attempt_id = str(actor.get("attempt_id") or "")
    if not attempt_id:
        raise SystemExit("required worker bundle rejected: attempt id is missing")
    bundle = (
        layout.root
        / "worker-bundles"
        / slugify(str(project.get("project_id") or "project"), "project")
        / slugify(attempt_id, "attempt")
    ).resolve()
    profile_path = bundle / "profile.config.toml"
    output_exchange = bundle / "out"
    credential_root = bundle / "credential"
    bundle.mkdir(parents=True, exist_ok=True)
    output_exchange.mkdir(exist_ok=True)
    credential_root.mkdir(exist_ok=True)
    if credential_root.is_symlink() or not credential_root.is_dir():
        raise SystemExit("required worker credential root is invalid")
    with contextlib.suppress(OSError):
        bundle.chmod(0o700)
        output_exchange.chmod(0o733)
        credential_root.chmod(0o700)
    runtime = actor.get("runtime") or {}
    recovering_existing_container = bool(
        runtime.get("container_name")
        and runtime.get("container_command")
        and (
            runtime.get("oci_lifecycle_state") in {"prepared", "started", "finished", "cleaned"}
            or runtime.get("container_id")
            or runtime.get("provider_execution_state") == PROVIDER_COMPLETION_PENDING
        )
    )
    if any(output_exchange.iterdir()) and not (recovering_existing_container or finalize_only):
        raise SystemExit("required worker output exchange is not empty for this attempt")

    profile = actor.get("profile")
    profile_binding = actor.get("profile_binding")
    if profile_binding is None:
        raise SystemExit("required worker profile binding is required")
    profile_payload = b"# CostMarshal isolated default profile\n"
    profile_text = profile_payload.decode("utf-8")
    try:
        validate_profile_binding(profile_binding, require_available=True)
        profile_payload = verify_profile_snapshot(layout.root, profile_binding)
        profile_text = profile_payload.decode("utf-8")
    except (ProfileBindingError, UnicodeDecodeError) as exc:
        raise SystemExit(f"required worker profile binding failed closed: {exc}") from exc
    if profile:
        try:
            profile_data = parse_profile_bytes(profile_payload)
        except ProfileBindingError as exc:
            raise SystemExit("required worker profile is invalid TOML") from exc
        allowed_top = {
            "model_provider",
            "model",
            "model_reasoning_effort",
            "disable_response_storage",
            "web_search",
            "model_providers",
        }
        unknown_top = sorted(set(profile_data) - allowed_top)
        providers = profile_data.get("model_providers")
        provider_id = profile_data.get("model_provider")
        if unknown_top or not isinstance(provider_id, str) or not isinstance(providers, dict):
            raise SystemExit("required worker profile contains unsupported settings")
        provider_row = providers.get(provider_id)
        allowed_provider = {"name", "base_url", "wire_api", "env_key"}
        if not isinstance(provider_row, dict) or set(provider_row) - allowed_provider:
            raise SystemExit("required worker profile contains unsupported provider settings")
        if provider_row.get("env_key") != provider_env_key(actor):
            raise SystemExit("required worker profile env_key does not match the routed provider")
        parsed_url = urlsplit(str(provider_row.get("base_url") or ""))
        network_mode = str(execution.get("network_mode") or "provider-proxy")
        if (
            parsed_url.scheme not in {"http", "https"}
            or not parsed_url.netloc
            or parsed_url.username
            or parsed_url.password
            or parsed_url.query
            or parsed_url.fragment
            or (parsed_url.scheme == "http" and network_mode != "provider-proxy")
        ):
            raise SystemExit("required worker profile base_url is invalid")
    try:
        install_bound_copy(profile_path, profile_payload, profile_binding)
    except ProfileBindingError as exc:
        raise SystemExit(f"required worker profile snapshot failed closed: {exc}") from exc

    limits_row = execution.get("limits") or {}
    try:
        limits = ResourceLimits(
            memory_mb=int(limits_row.get("memory_mb") or 2048),
            cpus=float(limits_row.get("cpus") or 2.0),
            pids=int(limits_row.get("pids") or 256),
            timeout_seconds=float(limits_row.get("timeout_seconds") or 30.0),
            tmpfs_mb=int(limits_row.get("tmpfs_mb") or 256),
            home_tmpfs_mb=int(limits_row.get("home_tmpfs_mb") or 64),
        )
    except (TypeError, ValueError) as exc:
        raise SystemExit("required worker resource limits are invalid") from exc
    forbidden_mount_roots = [layout.project_dir.resolve()]
    if isinstance(actor.get("collaboration_contract"), dict):
        forbidden_mount_roots.append(
            Path(str(project.get("workspace") or "")).expanduser().resolve()
        )
    source_project = project.get("source_project")
    if source_project:
        forbidden_mount_roots.append(Path(str(source_project)).expanduser().resolve())
    aggregate_secrets = project.get("secrets_file")
    if aggregate_secrets:
        forbidden_mount_roots.append(Path(str(aggregate_secrets)).expanduser().resolve())
    network_mode = str(execution.get("network_mode") or "provider-proxy")
    preflight_spec = WorkerExecutionSpec(
        project_id=str(project.get("project_id") or "project"),
        actor_id=str(actor["id"]),
        attempt_id=attempt_id,
        image=str(execution.get("image") or attestation.get("image") or ""),
        workspace=execution_workspace,
        workspace_mode="rw" if workspace_mode == "workspace-write" else "ro",
        profile_path=profile_path,
        output_exchange=output_exchange,
        profile_sha256=(
            str(profile_binding.get("sha256") or "").removeprefix("sha256:")
            if profile_binding is not None
            else None
        ),
        profile_size_bytes=(
            int(profile_binding["size_bytes"])
            if profile_binding is not None
            else None
        ),
        isolation_mode="required",
        engine=engine,
        network_mode=network_mode,
        network_name=execution.get("network_name"),
        forbidden_mount_roots=tuple(forbidden_mount_roots),
        limits=limits,
    )
    try:
        validate_execution_spec(
            preflight_spec,
            require_empty_output=not (recovering_existing_container or finalize_only),
        )
    except IsolationError as exc:
        raise SystemExit(f"required worker execution spec is invalid [{exc.code}]: {exc}") from exc

    # No provider secret is materialized until every credential-free execution
    # invariant has passed.  From this point onward cleanup is part of the
    # persisted OCI lifecycle contract.
    isolated_env, secret_values = (
        ({}, ())
        if finalize_only
        else isolated_actor_env(project, actor, layout=layout)
    )
    env_key = provider_env_key(actor)
    credential_path: Path | None = None
    durable_credential = runtime.get("credential_cleanup") or {}
    if finalize_only and durable_credential.get("path"):
        candidate = Path(str(durable_credential["path"]))
        if candidate.is_file():
            credential_path = candidate
    elif env_key and isolated_env.get(env_key):
        credential_path = credential_root / "provider.secret"
        if credential_path.exists():
            if credential_path.is_symlink() or not credential_path.is_file():
                raise SystemExit("required worker temporary credential is invalid")
            cleanup = runtime.get("credential_cleanup") or {}
            resumable_preparation = (
                cleanup.get("status") in {"creating", "pending"}
                and str(cleanup.get("path") or "") == str(credential_path)
                and runtime.get("oci_lifecycle_state")
                in {None, "credential_preparing", "prepared"}
            )
            if (
                not (
                    resumable_preparation
                    or (
                        recovering_existing_container
                        and cleanup.get("status") == "pending"
                        and str(cleanup.get("path") or "") == str(credential_path)
                    )
                )
                or not hmac.compare_digest(
                    credential_path.read_text(encoding="utf-8"),
                    str(isolated_env[env_key]),
                )
            ):
                raise SystemExit("required worker temporary credential already exists")
        else:
            if not allow_credential_creation:
                raise SystemExit(
                    "governance recovery-only launch rejected: an existing OCI credential is unavailable"
                )

            def prepare_credential_cleanup() -> None:
                current_actor = _validate_worker_fence(
                    layout,
                    str(actor["id"]),
                    attempt_id=str(actor.get("attempt_id") or ""),
                    launch_token=str(actor.get("launch_token") or ""),
                )
                current_actor.setdefault("runtime", {})["credential_cleanup"] = {
                    "required": True,
                    "path": str(credential_path),
                    "status": "creating",
                }
                save_actor(layout, current_actor)

            _control_mutation(
                layout,
                command_name="runner_prepare_credential",
                command_id=(
                    f"RUNNER-CREDENTIAL-PREPARE-{attempt_id}-"
                    f"{int(runtime.get('credential_generation') or 0)}"
                ),
                payload={
                    "actor_id": actor["id"],
                    "attempt_id": attempt_id,
                    "credential_generation": int(runtime.get("credential_generation") or 0),
                    "credential_identifier": hashlib.sha256(
                        os.fsencode(credential_path)
                    ).hexdigest(),
                },
                mutate=prepare_credential_cleanup,
            )
            runtime["credential_cleanup"] = {
                "required": True,
                "path": str(credential_path),
                "status": "creating",
            }
            atomic_write_text(credential_path, str(isolated_env[env_key]))
            _actor_fault("after_credential_before_oci_prepare")
        with contextlib.suppress(OSError):
            credential_path.chmod(0o404 if hasattr(os, "getuid") and os.getuid() == 0 else 0o600)
    elif env_key and not finalize_only:
        raise SystemExit(f"required worker credential is unavailable for {env_key}")

    spec = replace(
        preflight_spec,
        credential_path=credential_path,
        provider_env_key=env_key if credential_path else None,
        credential_cleanup="delete-after-use" if credential_path else "preserve",
        credential_temp_root=credential_root if credential_path else None,
    )
    try:
        validate_execution_spec(
            spec,
            require_empty_output=not (recovering_existing_container or finalize_only),
        )
    except IsolationError as exc:
        with contextlib.suppress(IsolationError):
            cleanup_temporary_credential(spec)
        raise SystemExit(f"required worker execution spec is invalid [{exc.code}]: {exc}") from exc
    command = ["costmarshal-worker", "--jsonl"]
    model = actor.get("model")
    if model and model != "inherit":
        command.extend(["--model", str(model)])
    return spec, command, secret_values


def _expected_oci_container_name(spec: WorkerExecutionSpec) -> str:
    """Mirror the adapter's stable attempt identity for pre-start recovery state."""

    identity = f"{spec.project_id}\0{spec.actor_id}\0{spec.attempt_id}".encode("utf-8")
    suffix = hashlib.sha256(identity).hexdigest()[:16]
    stem = re.sub(
        r"[^a-z0-9_.-]+",
        "-",
        f"{spec.project_id}-{spec.actor_id}".lower(),
    ).strip("-.")
    return f"costmarshal-{stem[:72]}-{suffix}"


def _validate_worker_fence(
    layout: ProjectLayout,
    actor_id: str,
    *,
    attempt_id: str | None,
    launch_token: str | None,
    allow_provider_started: bool = False,
) -> dict[str, Any]:
    actor = load_actor(layout, actor_id)
    if actor.get("role") != "agent":
        return actor
    isolation = actor.get("isolation") or {}
    attestation = isolation.get("attestation") or {}
    if isolation.get("mode") == "required":
        execution = isolation.get("execution") or {}
        if (
            attestation.get("strong_isolation") is not True
            or attestation.get("backend") not in {"docker", "podman"}
            or execution.get("engine") != attestation.get("backend")
            or execution.get("image") != attestation.get("image")
        ):
            raise SystemExit("required worker runner rejected: OCI attestation is incomplete or inconsistent")
    elif (
        isolation.get("mode") != "unsafe-native"
        or not isolation.get("project_opt_in")
        or not isolation.get("dispatch_opt_in")
        or attestation.get("backend") != "unsafe-native"
        or attestation.get("strong_isolation") is not False
    ):
        raise SystemExit("native worker runner rejected: explicit unsafe-native attestation is missing")
    expected_attempt = str(actor.get("attempt_id") or "")
    expected_token = str(actor.get("launch_token") or "")
    if not attempt_id or not hmac.compare_digest(str(attempt_id), expected_attempt):
        raise SystemExit("worker launch rejected: attempt fence mismatch")
    if not launch_token or not expected_token or not hmac.compare_digest(str(launch_token), expected_token):
        raise SystemExit("worker launch rejected: launch token mismatch")
    if actor.get("status") in {"stopped", "failed", "idle", "waiting"}:
        raise SystemExit(f"worker launch rejected: actor is {actor.get('status')}")
    if (
        not allow_provider_started
        and (actor.get("runtime") or {}).get("provider_execution_state") == "started"
    ):
        raise SystemExit("worker launch rejected: prior provider execution outcome is unknown")
    task_id = actor.get("task_id")
    if not task_id:
        raise SystemExit("worker launch rejected: actor has no task binding")
    task = load_task(layout, str(task_id))
    attempts = task.get("attempts") or []
    current = attempts[-1] if attempts else None
    if not current or current.get("attempt_id") != expected_attempt or current.get("actor_id") != actor_id:
        raise SystemExit("worker launch rejected: attempt is no longer current")
    if current.get("prompt_binding") != actor.get("prompt_binding"):
        raise SystemExit("worker launch rejected: actor and attempt prompt bindings differ")
    profile_binding = actor.get("profile_binding")
    try:
        if profile_binding is None:
            raise ProfileBindingError("actor profile binding is missing")
        validate_profile_binding(profile_binding, require_available=True)
        if current.get("profile_binding") != profile_binding:
            raise ProfileBindingError("actor and attempt profile bindings differ")
        route_step = current.get("route_plan_step") or {}
        if route_step.get("profile_binding") is not None and route_step.get("profile_binding") != profile_binding:
            raise ProfileBindingError("attempt and route plan profile bindings differ")
        verify_profile_snapshot(layout.root, profile_binding)
    except ProfileBindingError as exc:
        raise SystemExit(f"worker launch rejected: profile binding failed closed: {exc}") from exc
    if current.get("status") not in {"preparing", "dispatched", "starting", "launch_pending", "running", "needs_recovery"}:
        raise SystemExit(f"worker launch rejected: attempt is {current.get('status')}")
    return actor


def _provider_execution_needs_recovery(actor: dict[str, Any]) -> bool:
    """Return whether an external provider may already have started.

    Governance drift blocks new provider effects, but it must not strand an OCI
    container or temporary credential whose external start is already durable
    or outcome-unknown.  Existing fencing still decides whether a particular
    recovery invocation is valid.
    """

    if actor.get("role") != "agent":
        return False
    runtime = actor.get("runtime") or {}
    if (actor.get("isolation") or {}).get("mode") != "required":
        return False
    if (
        runtime.get("provider_execution_state") == PROVIDER_COMPLETION_PENDING
        or runtime.get("provider_completion") is not None
    ):
        return True
    if runtime.get("container_id"):
        return True
    lifecycle = runtime.get("oci_lifecycle_state")
    return bool(
        (
            runtime.get("container_cleanup_unconfirmed")
            or lifecycle
            in {"prepared", "started", "finished", "uncertain_start", "uncertain_cleanup"}
        )
        and runtime.get("container_name")
        and runtime.get("container_command")
    )


def _validate_actor_governance_before_side_effect(
    layout: ProjectLayout,
    actor: dict[str, Any],
) -> bool:
    """Fail closed before locks, project writes, credentials, or provider calls."""

    recovery_possible = _provider_execution_needs_recovery(actor)
    try:
        # Validate an explicit SQLite marker read-only before trusting the
        # project.json governance authority retained across cutover.
        control_store_enabled(layout)
        project = load_stable_governance_project(layout.project_json)
    except (ControlStoreError, GovernanceError) as exc:
        code = getattr(exc, "code", "governance_project_unavailable")
        raise SystemExit(
            f"ArchMarshal governance gate blocked actor launch [{code}]: {exc}"
        ) from exc
    governance = project.get("governance") or {"mode": "off", "ready": False}
    if not isinstance(governance, dict):
        raise SystemExit(
            "ArchMarshal governance gate blocked actor launch: governance state is invalid"
        )
    try:
        contract = enforce_governance_contract(
            governance,
            project.get("workspace"),
            operation="actor launch",
        )
    except GovernanceError as exc:
        if recovery_possible:
            # A prepared deterministic container may already exist after a
            # hard exit between external create and identity persistence. The
            # caller must attach/clean only and may never fresh-start.
            return True
        raise SystemExit(
            f"ArchMarshal governance gate blocked actor launch [{exc.code}]: {exc}"
        ) from exc
    governed = bool(contract.get("governed"))
    if (
        governed
        and actor.get("role") == "agent"
        and (actor.get("isolation") or {}).get("mode") == "unsafe-native"
    ):
        raise SystemExit(
            "ArchMarshal governance gate blocked actor launch: governed projects "
            "forbid unsafe-native provider launch; use required OCI isolation"
        )
    return False


def _run_actor_once(
    layout: ProjectLayout,
    actor_id: str,
    *,
    attempt_id: str | None,
    launch_token: str | None,
    governance_recovery_only: bool = False,
) -> int:
    with project_write_lock(layout):
        actor = _validate_worker_fence(
            layout,
            actor_id,
            attempt_id=attempt_id,
            launch_token=launch_token,
        )
        project = load_project(layout)
    prompt = (layout.project_dir / str(actor.get("prompt_path") or "")).resolve()
    expected_prompt = actor_prompt_file(layout, actor_id).resolve()
    try:
        ensure_workspace_containment(layout.project_dir, prompt, must_exist=True)
    except SecurityValidationError as exc:
        raise SystemExit(f"Actor prompt is outside the runtime: {exc}") from exc
    if prompt != expected_prompt or not prompt.is_file():
        raise SystemExit(f"Actor prompt not found: {prompt}")
    prompt_text = (
        validate_bound_actor_prompt(actor, prompt)
        if actor.get("role") == "agent"
        else prompt.read_text(encoding="utf-8")
    )
    governance_recovery_only = (
        _validate_actor_governance_before_side_effect(
            layout,
            load_actor(layout, actor_id),
        )
        or governance_recovery_only
    )
    report = report_path(layout, actor)
    report.parent.mkdir(parents=True, exist_ok=True)
    execution_workspace, sandbox, write_scopes, base_sha = actor_execution_workspace(layout, project, actor)
    required_isolation = (actor.get("isolation") or {}).get("mode") == "required"
    projection_receipt: dict[str, Any] | None = None
    semantic_contract: dict[str, Any] | None = None
    attempt_input_contract: dict[str, Any] | None = None
    semantic_prompt_binding: dict[str, Any] | None = None
    semantic_prompt_receipt: dict[str, Any] | None = None
    provider_completion_recovery: tuple[dict[str, Any], bytes, list[dict[str, Any]]] | None = None
    provider_completion_observed = False
    if required_isolation:
        collaboration_contract = actor.get("collaboration_contract")
        assert isinstance(collaboration_contract, dict)
        projection_task = load_task(layout, str(actor.get("task_id")))
        incoming_change_artifact = _incoming_change_artifact(
            layout,
            project,
            actor,
            projection_task,
            collaboration_contract,
            write_scopes,
        )
        incoming_change_manifest_sha256 = (
            incoming_change_artifact.get("manifest_sha256")
            if incoming_change_artifact is not None
            else None
        )
        projection_receipt = _projection_receipt(
            execution_workspace.parent,
            collaboration_contract,
            incoming_change_manifest_sha256=incoming_change_manifest_sha256,
            expected_runtime_receipt=(actor.get("runtime") or {}).get("context_projection"),
        )

        def persist_projection_receipt() -> None:
            current_actor = _validate_worker_fence(
                layout,
                actor_id,
                attempt_id=attempt_id,
                launch_token=launch_token,
            )
            runtime = current_actor.setdefault("runtime", {})
            existing = runtime.get("context_projection")
            if existing is not None and existing != projection_receipt:
                raise SystemExit("required worker projection receipt changed before provider launch")
            runtime["context_projection"] = projection_receipt
            save_actor(layout, current_actor)
            current_task = load_task(layout, str(current_actor.get("task_id")))
            matched = False
            for current_attempt in current_task.get("attempts") or []:
                if current_attempt.get("attempt_id") == current_actor.get("attempt_id"):
                    prior = current_attempt.get("context_projection")
                    if prior is not None and prior != projection_receipt:
                        raise SystemExit("attempt projection receipt changed before provider launch")
                    current_attempt["context_projection"] = projection_receipt
                    matched = True
                    break
            if not matched:
                raise SystemExit("required worker projection receipt has no authoritative attempt")
            save_task(layout, current_task)
            append_event(
                layout,
                "actor_context_projected",
                actor_id=actor_id,
                task_id=current_actor.get("task_id"),
                attempt_id=current_actor.get("attempt_id"),
                manifest_sha256=projection_receipt.get("manifest_sha256"),
                file_count=projection_receipt.get("file_count"),
            )

        _control_mutation(
            layout,
            command_name="runner_bind_context_projection",
            command_id=f"RUNNER-CONTEXT-PROJECTION-{attempt_id or actor_id}",
            payload={
                "actor_id": actor_id,
                "attempt_id": attempt_id,
                "contract_sha256": collaboration_contract.get("contract_sha256"),
                "manifest_sha256": projection_receipt.get("manifest_sha256"),
            },
            mutate=persist_projection_receipt,
        )
        prepared_semantic_attempt = _prepare_semantic_attempt(
            layout,
            project,
            actor,
            load_task(layout, str(actor.get("task_id"))),
            bootstrap_prompt_bytes=prompt_text.encode("utf-8"),
            projection_receipt=projection_receipt,
            incoming_change_artifact=incoming_change_artifact,
        )
        if prepared_semantic_attempt is not None:
            (
                semantic_contract,
                attempt_input_contract,
                semantic_prompt_binding,
                bound_prompt_bytes,
            ) = prepared_semantic_attempt
            semantic_prompt_path = _persist_immutable_payload(
                (
                    layout.root
                    / "semantic-prompts"
                    / slugify(str(project.get("project_id") or "project"), "project")
                    / slugify(str(actor.get("attempt_id") or actor_id), "attempt")
                ).resolve(),
                str(semantic_prompt_binding["prompt_sha256"]),
                bound_prompt_bytes,
            )
            semantic_prompt_receipt = {
                "schema": "costmarshal-semantic-prompt-receipt-v1",
                "path": str(semantic_prompt_path),
                "sha256": semantic_prompt_binding["prompt_sha256"],
                "size_bytes": len(bound_prompt_bytes),
                "binding_sha256": semantic_prompt_binding["binding_sha256"],
                "attempt_input_sha256": attempt_input_contract[
                    "attempt_input_sha256"
                ],
                "collaboration_contract_sha256": semantic_contract[
                    "contract_sha256"
                ],
            }

            def persist_semantic_prompt() -> None:
                current_actor = _validate_worker_fence(
                    layout,
                    actor_id,
                    attempt_id=attempt_id,
                    launch_token=launch_token,
                )
                current_task = load_task(layout, str(current_actor.get("task_id")))
                existing_contract = current_task.get("handoff_contract")
                if existing_contract is not None and existing_contract != semantic_contract:
                    raise SystemExit("semantic handoff contract changed before provider launch")
                current_task["handoff_contract"] = semantic_contract
                matched = False
                for current_attempt in current_task.get("attempts") or []:
                    if current_attempt.get("attempt_id") == current_actor.get("attempt_id"):
                        for field, value in (
                            ("attempt_input", attempt_input_contract),
                            ("semantic_prompt_binding", semantic_prompt_binding),
                            ("semantic_prompt", semantic_prompt_receipt),
                        ):
                            prior = current_attempt.get(field)
                            if prior is not None and prior != value:
                                raise SystemExit(f"{field} changed before provider launch")
                            current_attempt[field] = value
                        current_attempt["collaboration_phase"] = "prompt_bound"
                        matched = True
                        break
                if not matched:
                    raise SystemExit("semantic prompt has no authoritative attempt")
                save_task(layout, current_task)
                runtime = current_actor.setdefault("runtime", {})
                runtime["handoff_contract_sha256"] = semantic_contract[
                    "contract_sha256"
                ]
                runtime["attempt_input_sha256"] = attempt_input_contract[
                    "attempt_input_sha256"
                ]
                runtime["semantic_prompt"] = semantic_prompt_receipt
                current_actor["handoff_contract"] = semantic_contract
                current_actor["attempt_input"] = attempt_input_contract
                current_actor["semantic_prompt_binding"] = semantic_prompt_binding
                save_actor(layout, current_actor)
                append_event(
                    layout,
                    "actor_semantic_prompt_bound",
                    actor_id=actor_id,
                    task_id=current_actor.get("task_id"),
                    attempt_id=current_actor.get("attempt_id"),
                    contract_sha256=semantic_contract["contract_sha256"],
                    attempt_input_sha256=attempt_input_contract[
                        "attempt_input_sha256"
                    ],
                    prompt_sha256=semantic_prompt_binding["prompt_sha256"],
                )

            _control_mutation(
                layout,
                command_name="runner_bind_semantic_prompt",
                command_id=f"RUNNER-SEMANTIC-PROMPT-{attempt_id or actor_id}",
                payload={
                    "actor_id": actor_id,
                    "attempt_id": attempt_id,
                    "contract_sha256": semantic_contract["contract_sha256"],
                    "attempt_input_sha256": attempt_input_contract[
                        "attempt_input_sha256"
                    ],
                    "prompt_sha256": semantic_prompt_binding["prompt_sha256"],
                },
                mutate=persist_semantic_prompt,
            )
            prompt_text = bound_prompt_bytes.decode("utf-8", errors="strict")
        provider_completion_recovery = _load_provider_completion(
            layout,
            project,
            load_actor(layout, actor_id),
            semantic_contract=semantic_contract,
            attempt_input=attempt_input_contract,
            prompt_binding=semantic_prompt_binding,
            projection_receipt=projection_receipt,
        )
        provider_completion_observed = provider_completion_recovery is not None
    recovery_generation_value = (actor.get("runtime") or {}).get("recovery_generation")
    native_recovery_generation = (
        recovery_generation_value
        if isinstance(recovery_generation_value, int) and recovery_generation_value >= 0
        else 0
    )
    argv: list[str] = []
    env: dict[str, str] = {}
    required_spec: WorkerExecutionSpec | None = None
    required_command: list[str] = []
    if required_isolation:
        required_spec, required_command, secret_values = _required_worker_bundle(
            layout,
            project,
            actor,
            execution_workspace=execution_workspace,
            workspace_mode=sandbox,
            allow_credential_creation=not governance_recovery_only,
            finalize_only=provider_completion_recovery is not None,
        )
    else:
        argv = build_codex_argv(
            layout,
            actor,
            project,
            report,
            execution_workspace=execution_workspace,
            sandbox=sandbox,
        )
        env, secret_values = isolated_actor_env(project, actor, layout=layout)
    events: list[dict[str, Any]] = []
    usage_known = not required_isolation
    runtime_registration: dict[str, Any] = {}

    def register_runner() -> None:
        current_actor = _validate_worker_fence(
            layout,
            actor_id,
            attempt_id=attempt_id,
            launch_token=launch_token,
        )
        current_actor["status"] = "running"
        runtime = current_actor.setdefault("runtime", {})
        runtime["execution_workspace"] = str(execution_workspace)
        runtime["sandbox"] = sandbox
        runtime["runner_pid"] = os.getpid()
        runtime["pid"] = os.getpid()
        if runtime.get("backend") == "local":
            runtime["target"] = f"pid:{os.getpid()}"
            runtime["log_path"] = runtime.get("log_path") or str(
                (layout.transcripts_dir / f"{slugify(actor_id, 'actor')}.log").relative_to(layout.project_dir)
            ).replace("\\", "/")
        runtime["process_start_marker"] = (
            pid_start_marker(os.getpid()) or f"unverified:{os.getpid()}:{time.time_ns()}"
        )
        runtime["registered_launch_token_sha256"] = hashlib.sha256(str(launch_token).encode("utf-8")).hexdigest()
        runtime["registered_profile_sha256"] = str(
            (current_actor.get("profile_binding") or {}).get("sha256") or ""
        ).removeprefix("sha256:")
        if runtime.get("provider_execution_state") != PROVIDER_COMPLETION_PENDING:
            runtime["provider_execution_state"] = (
                "started" if required_isolation else "launch_pending_authorization"
            )
        runtime.update(runtime_registration)
        save_actor(layout, current_actor)
        if current_actor.get("task_id") and current_actor.get("attempt_id"):
            current_task = load_task(layout, str(current_actor["task_id"]))
            current_attempt = (current_task.get("attempts") or [])[-1]
            current_attempt["status"] = "running"
            current_attempt["started_at"] = current_attempt.get("started_at") or now_iso()
            current_attempt["runner_pid"] = os.getpid()
            current_attempt["process_start_marker"] = runtime["process_start_marker"]
            if current_task.get("status") == "dispatched":
                current_task["status"] = "running"
            save_task(layout, current_task)
        append_event(
            layout,
            "actor_exec_started",
            actor_id=actor_id,
            task_id=actor.get("task_id"),
            provider=actor.get("provider"),
            profile=actor.get("profile"),
            model=actor.get("model"),
            execution_workspace=str(execution_workspace),
            sandbox=sandbox,
            isolation_backend=runtime_registration.get("isolation_backend") or "unsafe-native",
            container_name=runtime_registration.get("container_name"),
        )

    def record_native_governance_block(
        *,
        stage: str,
        error: BaseException,
        child_terminated: bool | None,
    ) -> None:
        blocked_at = now_iso()
        reason = str(error)[:1024]

        def persist_block() -> None:
            current_actor = _validate_worker_fence(
                layout,
                actor_id,
                attempt_id=attempt_id,
                launch_token=launch_token,
                allow_provider_started=True,
            )
            current_actor["status"] = "needs_recovery"
            runtime = current_actor.setdefault("runtime", {})
            runtime["provider_execution_state"] = "not_started_governance_blocked"
            runtime["governance_launch_block"] = {
                "stage": stage,
                "blocked_at": blocked_at,
                "reason": reason,
                "child_terminated": child_terminated,
            }
            save_actor(layout, current_actor)
            task_id = current_actor.get("task_id")
            if task_id:
                current_task = load_task(layout, str(task_id))
                attempts = current_task.get("attempts") or []
                if attempts:
                    current_attempt = attempts[-1]
                    if (
                        current_attempt.get("attempt_id") == current_actor.get("attempt_id")
                        and current_attempt.get("actor_id") == actor_id
                    ):
                        current_attempt["status"] = "needs_recovery"
                        current_attempt["provider_execution_state"] = (
                            "not_started_governance_blocked"
                        )
                        current_attempt["governance_launch_block"] = {
                            "stage": stage,
                            "blocked_at": blocked_at,
                            "child_terminated": child_terminated,
                        }
                current_task["status"] = "needs_recovery"
                save_task(layout, current_task)
            append_event(
                layout,
                "actor_native_launch_governance_blocked",
                actor_id=actor_id,
                task_id=current_actor.get("task_id"),
                attempt_id=current_actor.get("attempt_id"),
                stage=stage,
                child_terminated=child_terminated,
            )

        _control_mutation(
            layout,
            command_name="runner_record_native_governance_block",
            command_id=(
                "RUNNER-NATIVE-GOVERNANCE-BLOCK-"
                f"{attempt_id or actor_id}-{native_recovery_generation}-{stage}"
            ),
            payload={
                "actor_id": actor_id,
                "attempt_id": attempt_id,
                "stage": stage,
                "child_terminated": child_terminated,
            },
            mutate=persist_block,
        )

    def authorize_native_provider() -> None:
        current_actor = _validate_worker_fence(
            layout,
            actor_id,
            attempt_id=attempt_id,
            launch_token=launch_token,
        )
        runtime = current_actor.setdefault("runtime", {})
        if runtime.get("provider_execution_state") != "launch_pending_authorization":
            raise SystemExit("native provider authorization state is invalid")
        runtime["provider_execution_state"] = "started"
        runtime["provider_authorized_at"] = now_iso()
        save_actor(layout, current_actor)
        append_event(
            layout,
            "actor_native_launch_authorized",
            actor_id=actor_id,
            task_id=current_actor.get("task_id"),
            attempt_id=current_actor.get("attempt_id"),
            recovery_generation=native_recovery_generation,
        )

    registration_payload = {
        "actor_id": actor_id,
        "attempt_id": attempt_id,
        "launch_token_sha256": hashlib.sha256(str(launch_token).encode("utf-8")).hexdigest(),
    }
    if required_isolation:
        assert required_spec is not None
        adapter = OciWorkerExecutionAdapter(OciCliBackend(required_spec.engine))
        handle = None
        returncode = 125
        failure: IsolationError | None = None
        expected_container_name = _expected_oci_container_name(required_spec)
        existing_runtime = actor.get("runtime") or {}
        existing_container_name = str(existing_runtime.get("container_name") or "")
        existing_container_command = existing_runtime.get("container_command") or []
        prepared_identity = bool(
            existing_container_name == expected_container_name
            and isinstance(existing_container_command, list)
            and existing_container_command
        )
        existing_lifecycle = str(existing_runtime.get("oci_lifecycle_state") or "")
        recovering_existing = bool(
            prepared_identity
            and (
                existing_lifecycle in {"started", "finished"}
                or existing_runtime.get("container_id")
            )
        )
        recovering_prepared = bool(prepared_identity and existing_lifecycle == "prepared")
        pending_report_text: str | None = None
        cleanup_unconfirmed = False
        cleanup_error_code: str | None = None
        provider_outcome_in_memory = False
        provider_completion_durable = provider_completion_recovery is not None
        completion_cleanup_receipt = None

        def prepare_oci_runtime() -> None:
            current_actor = _validate_worker_fence(
                layout,
                actor_id,
                attempt_id=attempt_id,
                launch_token=launch_token,
            )
            runtime = current_actor.setdefault("runtime", {})
            runtime["isolation_backend"] = required_spec.engine
            runtime["container_name"] = expected_container_name
            runtime["container_command"] = list(required_command)
            runtime["output_exchange"] = str(required_spec.output_exchange)
            runtime["oci_lifecycle_state"] = "prepared"
            runtime["credential_cleanup"] = {
                "required": required_spec.credential_path is not None,
                "path": str(required_spec.credential_path) if required_spec.credential_path else None,
                "status": "pending" if required_spec.credential_path else "not_required",
            }
            save_actor(layout, current_actor)

        if provider_completion_recovery is None:
            _control_mutation(
                layout,
                command_name="runner_prepare_oci",
                command_id=(
                    f"RUNNER-PREPARE-OCI-{attempt_id or actor_id}-"
                    f"{int(existing_runtime.get('credential_generation') or 0)}"
                ),
                payload={
                    **registration_payload,
                    "container_name": expected_container_name,
                    "credential_cleanup_required": required_spec.credential_path is not None,
                    "credential_generation": int(
                        existing_runtime.get("credential_generation") or 0
                    ),
                },
                mutate=prepare_oci_runtime,
            )
            _actor_fault("after_oci_prepare_before_start")
        try:
            governance_recovery_only = (
                _validate_actor_governance_before_side_effect(
                    layout,
                    load_actor(layout, actor_id),
                )
                or governance_recovery_only
            )
            if provider_completion_recovery is not None:
                completion, completion_report_bytes, completion_events = provider_completion_recovery
                events.extend(completion_events)
                returncode = int(completion["exit_code"])
                usage_known = True
                pending_report_text = completion_report_bytes.decode("utf-8", errors="strict")
                if existing_lifecycle == "cleaned":
                    cleanup_row = existing_runtime.get("credential_cleanup") or {}
                    cleanup_required = bool(cleanup_row.get("required"))
                    cleanup_deleted = cleanup_row.get("status") == "deleted"
                    if (
                        existing_runtime.get("container_removed") is not True
                        or (cleanup_required and not cleanup_deleted)
                    ):
                        raise SystemExit(
                            "finalize-only recovery rejected: durable OCI cleanup receipt is incomplete"
                        )
                    completion_cleanup_receipt = ExecutionCleanupReceipt(
                        existing_container_name,
                        True,
                        CredentialCleanupReceipt(
                            cleanup_row.get("identifier"),
                            cleanup_required,
                            cleanup_deleted if cleanup_required else False,
                            int(cleanup_row.get("bytes_removed") or 0),
                        ),
                        tuple(existing_runtime.get("cleanup_identity_drift") or ()),
                    )
                else:
                    if (
                        not prepared_identity
                        or not re.fullmatch(r"[0-9a-f]{64}", str(existing_runtime.get("container_id") or ""))
                    ):
                        raise SystemExit(
                            "finalize-only recovery rejected: durable OCI cleanup identity is incomplete"
                        )
                    try:
                        handle = adapter.attach(
                            required_spec,
                            container_name=existing_container_name,
                            container_id=str(existing_runtime["container_id"]),
                            command=tuple(str(item) for item in existing_container_command),
                        )
                    except IsolationError:
                        completion_cleanup_receipt = adapter.cleanup_confirmed_absent(
                            required_spec,
                            container_name=existing_container_name,
                            container_id=str(existing_runtime["container_id"]),
                            command=tuple(str(item) for item in existing_container_command),
                        )
                    else:
                        inspection = adapter.inspect(handle)
                        if (
                            inspection.status not in {"exited", "dead", "stopped"}
                            or inspection.exit_code is None
                            or int(inspection.exit_code) != returncode
                        ):
                            raise SystemExit(
                                "finalize-only recovery rejected: OCI terminal receipt mismatch"
                            )
            elif recovering_existing:
                handle = adapter.attach(
                    required_spec,
                    container_name=existing_container_name,
                    container_id=(
                        str(existing_runtime["container_id"])
                        if existing_runtime.get("container_id")
                        else None
                    ),
                    command=tuple(str(item) for item in existing_container_command),
                )
            elif governance_recovery_only:
                if not prepared_identity:
                    raise SystemExit(
                        "governance recovery-only launch rejected: durable OCI identity is unavailable"
                    )
                handle = adapter.attach(
                    required_spec,
                    container_name=existing_container_name,
                    container_id=(
                        str(existing_runtime["container_id"])
                        if existing_runtime.get("container_id")
                        else None
                    ),
                    command=tuple(str(item) for item in existing_container_command),
                )
            elif recovering_prepared:
                handle = adapter.recover_or_start(
                    required_spec,
                    tuple(str(item) for item in existing_container_command),
                    container_name=existing_container_name,
                    container_id=(
                        str(existing_runtime["container_id"])
                        if existing_runtime.get("container_id")
                        else None
                    ),
                    stdin_prompt=prompt_text,
                )
            else:
                handle = adapter.start(
                    required_spec,
                    required_command,
                    stdin_prompt=prompt_text,
                )
            if provider_completion_recovery is None:
                recovered_execution = bool(getattr(handle, "recovered", False))
                if handle.container_name != expected_container_name:
                    raise SystemExit("OCI worker returned an unexpected deterministic container identity")
                runtime_registration.update(
                    {
                        "isolation_backend": required_spec.engine,
                        "container_name": handle.container_name,
                        "container_id": handle.container_id,
                        "container_command": list(handle.command),
                        "container_network_id": handle.network_id,
                        "isolation_attestation": handle.attestation.to_dict(),
                        "output_exchange": str(required_spec.output_exchange),
                        "oci_lifecycle_state": "started",
                    }
                )

                def persist_oci_identity() -> None:
                    current_actor = _validate_worker_fence(
                        layout,
                        actor_id,
                        attempt_id=attempt_id,
                        launch_token=launch_token,
                    )
                    runtime = current_actor.setdefault("runtime", {})
                    runtime.update(runtime_registration)
                    save_actor(layout, current_actor)

                _control_mutation(
                    layout,
                    command_name="runner_register_oci_identity",
                    command_id=f"RUNNER-OCI-IDENTITY-{attempt_id or actor_id}",
                    payload={
                        **registration_payload,
                        "container_name": handle.container_name,
                        "container_id": handle.container_id,
                        "container_command": list(handle.command),
                        "network_id": handle.network_id,
                    },
                    mutate=persist_oci_identity,
                )
                _actor_fault("after_oci_start_before_register")
                _control_mutation(
                    layout,
                    command_name="runner_register",
                    command_id=f"RUNNER-REGISTER-{attempt_id or actor_id}",
                    payload=registration_payload,
                    mutate=register_runner,
                )
                if recovering_existing or recovered_execution:
                    receipt = adapter.recover_wait(handle)
                else:
                    receipt = adapter.wait(handle)
                provider_outcome_in_memory = True
                events.extend(dict(event) for event in receipt.stdout_events)
                returncode = receipt.exit_code
                usage_known = True
                validated = validate_output_exchange(required_spec.output_exchange)
                pending_report_text = redact_secret_values(validated.text, secret_values)
                _persist_provider_completion(
                    layout,
                    project,
                    actor,
                    wait_receipt=receipt,
                    validated_output=validated,
                    safe_report_bytes=pending_report_text.encode("utf-8"),
                    events=_redact_provider_events(events, secret_values),
                    semantic_contract=semantic_contract,
                    attempt_input=attempt_input_contract,
                    prompt_binding=semantic_prompt_binding,
                    projection_receipt=projection_receipt,
                    attempt_id=attempt_id,
                    launch_token=launch_token,
                )
                provider_completion_durable = True
                provider_completion_observed = True
                _actor_fault("after_provider_completion_before_cleanup")
        except IsolationError as exc:
            if governance_recovery_only and handle is None:
                failure = WorkerExecutionError(
                    "governance_recovery_attach_unconfirmed",
                    "governance recovery-only attach could not confirm the durable container identity",
                    details={
                        **exc.details,
                        "container_cleanup_unconfirmed": True,
                        "container_name": exc.details.get("container_name")
                        or expected_container_name,
                    },
                )
            else:
                failure = exc
            failure_details = failure.details
            if failure_details.get("container_cleanup_unconfirmed"):

                def persist_uncertain_oci_start() -> None:
                    current_actor = _validate_worker_fence(
                        layout,
                        actor_id,
                        attempt_id=attempt_id,
                        launch_token=launch_token,
                        allow_provider_started=True,
                    )
                    uncertain_runtime = current_actor.setdefault("runtime", {})
                    uncertain_runtime["oci_lifecycle_state"] = "uncertain_start"
                    uncertain_runtime["container_name"] = failure_details.get(
                        "container_name"
                    ) or expected_container_name
                    if failure_details.get("container_id"):
                        uncertain_runtime["container_id"] = failure_details["container_id"]
                    uncertain_runtime["container_command"] = list(required_command)
                    save_actor(layout, current_actor)

                _control_mutation(
                    layout,
                    command_name="runner_record_uncertain_oci_start",
                    command_id=f"RUNNER-OCI-UNCERTAIN-{attempt_id or actor_id}",
                    payload={
                        **registration_payload,
                        "container_name": failure_details.get("container_name"),
                        "container_id": failure_details.get("container_id"),
                    },
                    mutate=persist_uncertain_oci_start,
                )
            pending_report_text = (
                "# Completion Report\n\nStatus: failed\n\n## Result\n"
                f"OCI worker failed safely [{failure.code}]: {failure}\n"
            )
        finally:
            cleanup_container_removed = False
            cleanup_credential = None
            cleanup_identity_drift: tuple[str, ...] = ()
            try:
                if provider_outcome_in_memory and not provider_completion_durable:
                    raise WorkerExecutionError(
                        "provider_completion_not_durable",
                        "provider completion was observed but not durably recorded; cleanup is deferred",
                        details={"container_cleanup_unconfirmed": True},
                    )
                if completion_cleanup_receipt is not None:
                    cleanup_container_removed = completion_cleanup_receipt.container_removed
                    cleanup_credential = completion_cleanup_receipt.credential
                    cleanup_identity_drift = completion_cleanup_receipt.identity_drift
                elif handle is not None:
                    cleanup_receipt = adapter.cleanup(handle)
                    cleanup_container_removed = cleanup_receipt.container_removed
                    cleanup_credential = cleanup_receipt.credential
                    cleanup_identity_drift = cleanup_receipt.identity_drift
                else:
                    if failure is not None and failure.details.get(
                        "container_cleanup_unconfirmed"
                    ):
                        raise WorkerExecutionError(
                            "credential_cleanup_deferred",
                            "temporary credential is preserved until uncertain container cleanup is confirmed",
                            details={"container_cleanup_unconfirmed": True},
                        )
                    cleanup_credential = cleanup_temporary_credential(required_spec)
            except IsolationError as exc:
                failure = failure or exc
                returncode = 125
                cleanup_unconfirmed = bool(
                    exc.details.get("container_cleanup_unconfirmed")
                    or provider_completion_durable
                )
                cleanup_error_code = exc.code
                existing = pending_report_text or "# Completion Report\n"
                pending_report_text = (
                    existing.rstrip()
                    + "\n\nStatus: failed\n\n## Cleanup Failure\n"
                    + f"OCI cleanup failed safely [{exc.code}]: {exc}\n"
                )
            else:
                assert cleanup_credential is not None

                def persist_oci_cleanup() -> None:
                    current_actor = _validate_worker_fence(
                        layout,
                        actor_id,
                        attempt_id=attempt_id,
                        launch_token=launch_token,
                        allow_provider_started=True,
                    )
                    cleanup_runtime = current_actor.setdefault("runtime", {})
                    cleanup_runtime["oci_lifecycle_state"] = (
                        "cleaned" if cleanup_container_removed else "not_started_cleaned"
                    )
                    cleanup_runtime["container_removed"] = cleanup_container_removed
                    cleanup_runtime["cleanup_identity_drift"] = list(cleanup_identity_drift)
                    cleanup_runtime["credential_cleanup"] = {
                        "required": cleanup_credential.requested,
                        "path": (
                            str(required_spec.credential_path)
                            if required_spec.credential_path is not None
                            else None
                        ),
                        "status": "deleted" if cleanup_credential.deleted else "not_required",
                        "identifier": cleanup_credential.credential_id,
                        "bytes_removed": cleanup_credential.bytes_removed,
                        "cleaned_at": now_iso(),
                    }
                    save_actor(layout, current_actor)

                try:
                    _control_mutation(
                        layout,
                        command_name="runner_record_oci_cleanup",
                        command_id=f"RUNNER-OCI-CLEANUP-{attempt_id or actor_id}",
                        payload={
                            **registration_payload,
                            "container_id": (
                                handle.container_id
                                if handle is not None
                                else existing_runtime.get("container_id")
                            ),
                            "container_removed": cleanup_container_removed,
                            "credential_identifier": cleanup_credential.credential_id,
                        },
                        mutate=persist_oci_cleanup,
                    )
                except Exception as exc:  # noqa: BLE001 - cleanup fact must be durable
                    failure = WorkerExecutionError(
                        "cleanup_state_persist_failed",
                        "OCI cleanup completed but its durable receipt could not be recorded",
                    )
                    returncode = 125
                    existing = pending_report_text or "# Completion Report\n"
                    pending_report_text = (
                        existing.rstrip()
                        + "\n\nStatus: failed\n\n## Cleanup Receipt Failure\n"
                        + f"OCI cleanup receipt persistence failed safely: {type(exc).__name__}\n"
                    )
        if cleanup_unconfirmed:

            def persist_uncertain_cleanup() -> None:
                current_actor = _validate_worker_fence(
                    layout,
                    actor_id,
                    attempt_id=attempt_id,
                    launch_token=launch_token,
                    allow_provider_started=True,
                )
                current_actor["status"] = "needs_recovery"
                uncertain_runtime = current_actor.setdefault("runtime", {})
                if uncertain_runtime.get("oci_lifecycle_state") != "uncertain_start":
                    uncertain_runtime["oci_lifecycle_state"] = "uncertain_cleanup"
                uncertain_runtime["container_cleanup_unconfirmed"] = True
                uncertain_runtime["cleanup_error"] = cleanup_error_code
                # Deliberately retain provider_execution_state=started (or its
                # pre-registration absence).  A cleanup uncertainty is not a
                # durable provider-finished observation.
                save_actor(layout, current_actor)
                if current_actor.get("task_id") and current_actor.get("attempt_id"):
                    current_task = load_task(layout, str(current_actor["task_id"]))
                    for current_attempt in current_task.get("attempts") or []:
                        if current_attempt.get("attempt_id") == current_actor.get("attempt_id"):
                            current_attempt["status"] = "needs_recovery"
                            current_attempt["cleanup_error"] = cleanup_error_code
                            break
                    current_task["status"] = "needs_recovery"
                    save_task(layout, current_task)
                append_event(
                    layout,
                    "oci_cleanup_uncertain",
                    actor_id=actor_id,
                    task_id=current_actor.get("task_id"),
                    attempt_id=current_actor.get("attempt_id"),
                    container_name=uncertain_runtime.get("container_name"),
                    container_id=uncertain_runtime.get("container_id"),
                    cleanup_error=cleanup_error_code,
                )

            _control_mutation(
                layout,
                command_name="runner_record_uncertain_oci_cleanup",
                command_id=f"RUNNER-OCI-CLEANUP-UNCERTAIN-{attempt_id or actor_id}",
                payload={
                    **registration_payload,
                    "container_name": expected_container_name,
                    "container_id": handle.container_id if handle is not None else None,
                    "cleanup_error": cleanup_error_code,
                },
                mutate=persist_uncertain_cleanup,
            )
            return 125
        if pending_report_text is not None:
            atomic_write_text(report, pending_report_text)
        if failure is not None:
            returncode = 125
    else:
        if _validate_actor_governance_before_side_effect(
            layout,
            load_actor(layout, actor_id),
        ):
            raise SystemExit(
                "governance recovery-only mode is unavailable for native provider launch"
            )
        _control_mutation(
            layout,
            command_name="runner_register",
            command_id=(
                f"RUNNER-REGISTER-{attempt_id or actor_id}-{native_recovery_generation}"
            ),
            payload={
                **registration_payload,
                "recovery_generation": native_recovery_generation,
            },
            mutate=register_runner,
        )
        try:
            if _validate_actor_governance_before_side_effect(
                layout,
                load_actor(layout, actor_id),
            ):
                raise SystemExit(
                    "governance recovery-only mode is unavailable for native provider launch"
                )
        except SystemExit as exc:
            record_native_governance_block(
                stage="before_popen",
                error=exc,
                child_terminated=None,
            )
            raise
        process: subprocess.Popen[str] | None = None
        try:
            process = subprocess.Popen(
                process_argv(argv),
                cwd=str(execution_workspace),
                env=env,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
            )
            try:
                _native_launch_barrier("after_popen_before_governance")
                if _validate_actor_governance_before_side_effect(
                    layout,
                    load_actor(layout, actor_id),
                ):
                    raise SystemExit(
                        "governance recovery-only mode is unavailable for native provider launch"
                    )
                _control_mutation(
                    layout,
                    command_name="runner_authorize_native_provider",
                    command_id=(
                        "RUNNER-AUTHORIZE-NATIVE-"
                        f"{attempt_id or actor_id}-{native_recovery_generation}"
                    ),
                    payload={
                        "actor_id": actor_id,
                        "attempt_id": attempt_id,
                        "recovery_generation": native_recovery_generation,
                    },
                    mutate=authorize_native_provider,
                )
            except SystemExit as exc:
                child_terminated = _terminate_unreleased_native_child(process)
                record_native_governance_block(
                    stage="after_popen",
                    error=exc,
                    child_terminated=child_terminated,
                )
                raise
            except BaseException:
                _terminate_unreleased_native_child(process)
                raise
            assert process.stdin is not None
            process.stdin.write(prompt_text)
            process.stdin.close()
            assert process.stdout is not None
            for line in process.stdout:
                safe_line = redact_secret_values(line, secret_values)
                sys.stdout.write(safe_line)
                sys.stdout.flush()
                try:
                    payload = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(payload, dict):
                    events.append(payload)
            returncode = process.wait()
        except OSError as exc:
            if process is not None:
                _terminate_unreleased_native_child(process)
            returncode = 127
            atomic_write_text(report, f"# Completion Report\n\nStatus: failed\n\n## Result\n{type(exc).__name__}: {exc}\n")

    if required_isolation and provider_completion_observed:
        _actor_fault("after_provider_cleanup_before_seal")

    changed_paths: tuple[str, ...] = ()
    change_artifact_receipt: dict[str, Any] | None = None
    if write_scopes:
        if required_isolation:
            try:
                assert base_sha is not None
                collaboration_contract = actor.get("collaboration_contract")
                assert isinstance(collaboration_contract, dict)
                prepared_changes = capture_projection_changes(
                    execution_workspace.parent,
                    expected_base_sha=base_sha,
                    expected_allowlist=collaboration_contract.get("context_paths") or [],
                    write_scope=write_scopes,
                    expected_manifest_sha256=(projection_receipt or {}).get(
                        "manifest_sha256"
                    ),
                    previous=(incoming_change_artifact or {}).get("manifest"),
                )
                persisted_changes = persist_change_artifact(
                    (
                        layout.root
                        / "task-change-artifacts"
                        / slugify(str(project.get("project_id") or "project"), "project")
                        / slugify(str(actor.get("task_id") or "task"), "task")
                    ).resolve(),
                    prepared_changes,
                )
                changed_paths = tuple(
                    str(entry["path"])
                    for entry in prepared_changes.manifest.get("changes") or []
                )
                change_artifact_receipt = {
                    "schema": "costmarshal-change-artifact-receipt-v1",
                    "manifest_sha256": persisted_changes.manifest_sha256,
                    "manifest_path": str(persisted_changes.manifest_path),
                    "artifact_root": str(persisted_changes.artifact_root),
                    "base_sha": base_sha,
                    "write_scope": list(write_scopes),
                    "change_count": prepared_changes.manifest.get("change_count"),
                    "total_upsert_bytes": prepared_changes.manifest.get("total_upsert_bytes"),
                    "manifest": prepared_changes.manifest,
                    "collaboration_contract_sha256": collaboration_contract.get(
                        "contract_sha256"
                    ),
                }
                violations = ()
            except (ContextProjectionError, OSError) as exc:
                violations = (
                    f"<change-capture-failed:{type(exc).__name__}:{str(exc)[:512]}>",
                )
        else:
            try:
                assert base_sha is not None
                changed_paths = worktree_changed_paths(execution_workspace, base_sha)
                violations = tuple(path for path in changed_paths if not _path_is_within_scope(path, write_scopes))
            except (OSError, subprocess.CalledProcessError) as exc:
                violations = (f"<diff-check-failed:{type(exc).__name__}>",)
        if violations:
            returncode = 126
            existing = report.read_text(encoding="utf-8", errors="replace") if report.is_file() else "# Completion Report\n\n"
            atomic_write_text(
                report,
                existing.rstrip()
                + "\n\nStatus: failed\n\n## CostMarshal Write-Scope Violation\n"
                + "\n".join(f"- {path}" for path in violations)
                + "\n",
            )

    input_tokens, cached_input_tokens, output_tokens, usage_pricing_complete = (
        usage_details_from_events(events)
    )
    usage_known = usage_known and usage_pricing_complete
    if report.is_file() and secret_values:
        report_text = report.read_text(encoding="utf-8", errors="replace")
        safe_report_text = redact_secret_values(report_text, secret_values)
        if safe_report_text != report_text:
            atomic_write_text(report, safe_report_text)
    task_id = actor.get("task_id")
    final_report_status = report_status(report)
    worker_outcome = (
        "failed"
        if returncode != 0
        else final_report_status
        if final_report_status in {"failed", "escalate"}
        else "waiting_leader"
    )
    # A worker reports evidence, never a task decision.  Keeping collection in
    # waiting_leader preserves the admitted budget envelope and path claims
    # until the leader explicitly records done/failed/escalate.
    collected_state = "waiting_leader"
    report_bytes = report.read_bytes() if report.is_file() else b""
    report_sha256 = hashlib.sha256(report_bytes).hexdigest() if report_bytes else None
    attempt_output_contract: dict[str, Any] | None = None
    execution_receipt: dict[str, Any] | None = None
    if semantic_contract is not None:
        if (
            attempt_input_contract is None
            or semantic_prompt_binding is None
            or semantic_prompt_receipt is None
            or report_sha256 is None
            or not report_bytes
        ):
            raise SystemExit("semantic attempt output cannot be sealed without prompt and report receipts")
        outgoing_manifest_sha256 = semantic_contract["change_policy"][
            "initial_manifest_sha256"
        ]
        outgoing_change_count = 0
        outgoing_total_bytes = 0
        if incoming_change_artifact is not None:
            outgoing_manifest_sha256 = incoming_change_artifact["manifest_sha256"]
            outgoing_change_count = int(incoming_change_artifact.get("change_count") or 0)
            outgoing_total_bytes = int(
                incoming_change_artifact.get("total_upsert_bytes") or 0
            )
        if change_artifact_receipt is not None:
            outgoing_manifest_sha256 = change_artifact_receipt["manifest_sha256"]
            outgoing_change_count = int(change_artifact_receipt.get("change_count") or 0)
            outgoing_total_bytes = int(
                change_artifact_receipt.get("total_upsert_bytes") or 0
            )
        durable_actor = load_actor(layout, actor_id)
        durable_runtime = durable_actor.get("runtime") or {}
        execution_receipt_body = {
            "schema_version": 1,
            "kind": "costmarshal-execution-receipt",
            "task_id": task_id,
            "attempt_id": actor.get("attempt_id"),
            "actor_id": actor_id,
            "provider": actor.get("provider"),
            "tier": actor.get("tier"),
            "model": actor.get("model"),
            "profile": actor.get("profile"),
            "profile_sha256": (actor.get("profile_binding") or {}).get("sha256"),
            "context_projection_manifest_sha256": (projection_receipt or {}).get(
                "manifest_sha256"
            ),
            "incoming_change_manifest_sha256": attempt_input_contract.get(
                "incoming_changes", {}
            ).get("manifest_sha256"),
            "semantic_prompt_sha256": semantic_prompt_binding["prompt_sha256"],
            "isolation_backend": durable_runtime.get("isolation_backend"),
            "container_name": durable_runtime.get("container_name"),
            "container_id": durable_runtime.get("container_id"),
            "isolation_attestation": durable_runtime.get("isolation_attestation"),
            "provider_exit_code": returncode,
        }
        execution_receipt_sha256 = _canonical_sha256(execution_receipt_body)
        execution_receipt = {
            **execution_receipt_body,
            "receipt_sha256": execution_receipt_sha256,
        }
        execution_receipt_path = _persist_immutable_payload(
            (
                layout.root
                / "execution-receipts"
                / slugify(str(project.get("project_id") or "project"), "project")
                / slugify(str(actor.get("attempt_id") or actor_id), "attempt")
            ).resolve(),
            execution_receipt_sha256,
            json.dumps(
                execution_receipt_body,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            ).encode("utf-8"),
        )
        execution_receipt["path"] = str(execution_receipt_path)
        try:
            attempt_output_contract = build_attempt_output_contract(
                collaboration_contract=semantic_contract,
                attempt_input=attempt_input_contract,
                prompt_binding=semantic_prompt_binding,
                execution_receipt_sha256=execution_receipt_sha256,
                report_sha256="sha256:" + report_sha256,
                report_size_bytes=len(report_bytes),
                outgoing_change_manifest_sha256=str(outgoing_manifest_sha256),
                outgoing_change_count=outgoing_change_count,
                outgoing_total_upsert_bytes=outgoing_total_bytes,
            )
        except HandoffContractError as exc:
            raise SystemExit(f"semantic attempt output failed closed: {exc}") from exc

        def persist_attempt_output() -> None:
            current_actor = _validate_worker_fence(
                layout,
                actor_id,
                attempt_id=attempt_id,
                launch_token=launch_token,
                allow_provider_started=True,
            )
            current_task = load_task(layout, str(current_actor.get("task_id")))
            matched = False
            for current_attempt in current_task.get("attempts") or []:
                if current_attempt.get("attempt_id") == current_actor.get("attempt_id"):
                    for field, value in (
                        ("execution_receipt", execution_receipt),
                        ("attempt_output", attempt_output_contract),
                    ):
                        prior = current_attempt.get(field)
                        if prior is not None and prior != value:
                            raise SystemExit(f"sealed {field} changed after provider exit")
                        current_attempt[field] = value
                    current_attempt["attempt_output_sha256"] = attempt_output_contract[
                        "attempt_output_sha256"
                    ]
                    current_attempt["collaboration_phase"] = "output_sealed"
                    matched = True
                    break
            if not matched:
                raise SystemExit("sealed attempt output has no authoritative attempt")
            save_task(layout, current_task)
            runtime = current_actor.setdefault("runtime", {})
            runtime["execution_receipt"] = execution_receipt
            runtime["attempt_output_sha256"] = attempt_output_contract[
                "attempt_output_sha256"
            ]
            save_actor(layout, current_actor)
            append_event(
                layout,
                "actor_attempt_output_sealed",
                actor_id=actor_id,
                task_id=current_actor.get("task_id"),
                attempt_id=current_actor.get("attempt_id"),
                attempt_output_sha256=attempt_output_contract[
                    "attempt_output_sha256"
                ],
                execution_receipt_sha256=execution_receipt["receipt_sha256"],
                outgoing_change_manifest_sha256=outgoing_manifest_sha256,
            )

        _control_mutation(
            layout,
            command_name="runner_seal_attempt_output",
            command_id=f"RUNNER-SEAL-OUTPUT-{attempt_id or actor_id}",
            payload={
                "actor_id": actor_id,
                "attempt_id": attempt_id,
                "attempt_output_sha256": attempt_output_contract[
                    "attempt_output_sha256"
                ],
                "execution_receipt_sha256": execution_receipt["receipt_sha256"],
            },
            mutate=persist_attempt_output,
        )
    if task_id:
        # Required attempts must seal their execution/report output before the
        # canonical report becomes recoverable.  A crash at the named fault
        # point now leaves enough durable receipts for scheduler recovery to
        # collect without re-running the provider.
        _actor_fault("after_attempt_report_before_publish")
        publish_task_report(layout, actor, report)
    _actor_fault("after_report_before_finalize")

    def finalize_runner() -> None:
        if task_id:
            status_path = task_dir(layout, str(task_id)) / "status.json"
            atomic_write_json(
                status_path,
                {
                    "schema_version": SCHEMA_VERSION,
                    "task_id": task_id,
                    "state": collected_state,
                    "worker_outcome": worker_outcome,
                    "updated_at": now_iso(),
                    "error": None if returncode == 0 else f"actor process exited {returncode}",
                    "actor_id": actor_id,
                    "provider": actor.get("provider"),
                    "profile": actor.get("profile"),
                    "model": actor.get("model"),
                    "attempt_id": actor.get("attempt_id"),
                    "execution_workspace": str(execution_workspace),
                    "changed_paths": list(changed_paths),
                    "context_projection": projection_receipt,
                    "change_artifact": change_artifact_receipt,
                    "report_sha256": report_sha256,
                    "report_size": len(report_bytes),
                },
            )
            current_task = load_task(layout, str(task_id))
            for current_attempt in current_task.get("attempts") or []:
                if current_attempt.get("attempt_id") == actor.get("attempt_id"):
                    current_attempt["report_path"] = str(report.relative_to(layout.project_dir)).replace("\\", "/")
                    current_attempt["report_sha256"] = report_sha256
                    current_attempt["report_size"] = len(report_bytes)
                    current_attempt["provider_exit_code"] = returncode
                    current_attempt["worker_outcome"] = worker_outcome
                    current_attempt["provider_execution_state"] = "finished"
                    if projection_receipt is not None:
                        current_attempt["context_projection"] = projection_receipt
                    if change_artifact_receipt is not None:
                        current_attempt["change_artifact"] = change_artifact_receipt
                    current_attempt["usage_status"] = (
                        "captured" if usage_known else "unknown_recovery_logs"
                    )
                    break
            save_task(layout, current_task)
            if usage_known:
                usage_args = {
                    "actor": actor_id,
                    "task": task_id,
                    "attempt": actor.get("attempt_id"),
                    "model": actor.get("model"),
                    "input_tokens": input_tokens,
                    "cached_input_tokens": cached_input_tokens,
                    "output_tokens": output_tokens,
                    "final_usage": True,
                    "note": f"provider={actor.get('provider')} profile={actor.get('profile') or '-'} exit={returncode}",
                }
                send_message(
                    layout,
                    sender=actor_id,
                    recipient="scheduler",
                    subject="scheduler.command",
                    body="Record usage captured from codex exec JSONL.",
                    task_id=str(task_id),
                    metadata={"command": "record_usage", "args": usage_args},
                )
            needs_escalation = returncode != 0 or final_report_status in {"failed", "escalate"}
            envelope = current_task.get("route_budget_envelope")
            plan_allows_next = True
            if isinstance(envelope, dict) and envelope.get("status") == "active":
                current_index = current_attempt.get("route_plan_step_index") if current_attempt else None
                plan_allows_next = bool(
                    current_attempt
                    and current_attempt.get("route_envelope_id") == envelope.get("envelope_id")
                    and type(current_index) is int
                    and current_index + 1 < len(envelope.get("planned_steps") or [])
                )
            command, command_args, body = _completion_scheduler_command(
                task_id=str(task_id),
                actor_id=actor_id,
                attempt_id=actor.get("attempt_id"),
                provider=actor.get("provider"),
                returncode=returncode,
                collected_state=collected_state,
                needs_escalation=needs_escalation,
                plan_allows_next=plan_allows_next,
            )
            send_message(
                layout,
                sender=actor_id,
                recipient="scheduler",
                subject="scheduler.command",
                body=body,
                task_id=str(task_id),
                metadata={"command": command, "args": command_args},
            )
        current_actor = load_actor(layout, actor_id)
        current_actor["status"] = "stopped" if returncode == 0 else "failed"
        current_actor.setdefault("runtime", {})["exit_code"] = returncode
        current_actor["runtime"]["finished_at"] = now_iso()
        current_actor["runtime"]["changed_paths"] = list(changed_paths)
        if projection_receipt is not None:
            current_actor["runtime"]["context_projection"] = projection_receipt
        if change_artifact_receipt is not None:
            current_actor["runtime"]["change_artifact"] = change_artifact_receipt
        current_actor["runtime"]["provider_execution_state"] = "finished"
        current_actor["runtime"]["usage_status"] = (
            "captured" if usage_known else "unknown_recovery_logs"
        )
        current_actor["runtime"]["report_sha256"] = report_sha256
        save_actor(layout, current_actor)
        append_event(
            layout,
            "actor_exec_finished",
            actor_id=actor_id,
            task_id=task_id,
            provider=actor.get("provider"),
            profile=actor.get("profile"),
            model=actor.get("model"),
            exit_code=returncode,
            input_tokens=input_tokens,
            cached_input_tokens=cached_input_tokens,
            output_tokens=output_tokens,
            changed_paths=list(changed_paths),
            context_projection_manifest=(projection_receipt or {}).get("manifest_sha256"),
            change_artifact_manifest=(change_artifact_receipt or {}).get("manifest_sha256"),
        )
    _control_mutation(
        layout,
        command_name="runner_finalize",
        command_id=f"RUNNER-FINALIZE-{attempt_id or actor_id}",
        payload={
            "actor_id": actor_id,
            "attempt_id": attempt_id,
            "exit_code": returncode,
            "report_sha256": report_sha256,
            "context_projection_manifest": (projection_receipt or {}).get(
                "manifest_sha256"
            ),
            "change_artifact_manifest": (change_artifact_receipt or {}).get(
                "manifest_sha256"
            ),
            "input_tokens": input_tokens,
            "cached_input_tokens": cached_input_tokens,
            "output_tokens": output_tokens,
        },
        mutate=finalize_runner,
    )
    return returncode


def run_actor(
    layout: ProjectLayout,
    actor_id: str,
    *,
    attempt_id: str | None = None,
    launch_token: str | None = None,
) -> int:
    """Run one actor, fencing task workers for their entire provider lifetime."""

    actor = load_actor(layout, actor_id)
    governance_recovery_only = _validate_actor_governance_before_side_effect(layout, actor)
    with project_write_lock(layout):
        actor = _validate_worker_fence(
            layout,
            actor_id,
            attempt_id=attempt_id,
            launch_token=launch_token,
        )
    if actor.get("role") != "agent":
        return _run_actor_once(layout, actor_id, attempt_id=None, launch_token=None)
    lock_path = layout.project_dir / "locks" / "attempts" / f"{slugify(str(attempt_id), 'attempt')}.runtime.lock"
    try:
        with advisory_file_lock(lock_path, timeout_seconds=0.25):
            return _run_actor_once(
                layout,
                actor_id,
                attempt_id=attempt_id,
                launch_token=launch_token,
                governance_recovery_only=governance_recovery_only,
            )
    except ProjectLockTimeout as exc:
        raise SystemExit("worker launch rejected: another runner owns this attempt") from exc


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run one CostMarshal actor through codex exec")
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--project", required=True)
    parser.add_argument("--actor", required=True)
    parser.add_argument("--attempt")
    parser.add_argument("--launch-token")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    layout = resolve_project(args.root, args.project)
    return run_actor(layout, args.actor, attempt_id=args.attempt, launch_token=args.launch_token)


if __name__ == "__main__":
    raise SystemExit(main())
