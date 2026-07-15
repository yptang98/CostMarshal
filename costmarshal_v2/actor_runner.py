from __future__ import annotations

import argparse
import contextlib
import hashlib
import hmac
import json
import os
import re
import shutil
import subprocess
import sys
import time
import tomllib
from dataclasses import replace
from pathlib import Path
from typing import Any, Callable, Iterable
from urllib.parse import urlsplit

from .control_store import ControlStoreError, control_store_enabled, control_transaction
from .governance import (
    GovernanceError,
    load_stable_governance_project,
    validate_governance_binding,
)
from .mailbox import send_message
from .locking import ProjectLockTimeout, advisory_file_lock, project_write_lock
from .paths import ProjectLayout, resolve_project, slugify
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
        atomic_write_text(
            task_dir(layout, str(task_id)) / "completion-report.md",
            attempt_report.read_text(encoding="utf-8", errors="replace"),
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


def usage_from_events(events: list[dict[str, Any]]) -> tuple[int, int]:
    best_input = 0
    best_output = 0
    for event in events:
        for item in walk_dicts(event):
            input_value = item.get("input_tokens", item.get("prompt_tokens", 0))
            output_value = item.get("output_tokens", item.get("completion_tokens", 0))
            if isinstance(input_value, int):
                best_input = max(best_input, input_value)
            if isinstance(output_value, int):
                best_output = max(best_output, output_value)
    return best_input, best_output


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


def _required_worker_bundle(
    layout: ProjectLayout,
    project: dict[str, Any],
    actor: dict[str, Any],
    *,
    execution_workspace: Path,
    workspace_mode: str,
    allow_credential_creation: bool = True,
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
            runtime.get("oci_lifecycle_state") in {"prepared", "started", "finished"}
            or runtime.get("container_id")
        )
    )
    if any(output_exchange.iterdir()) and not recovering_existing_container:
        raise SystemExit("required worker output exchange is not empty for this attempt")

    profile = actor.get("profile")
    profile_text = "# CostMarshal isolated default profile\n"
    if profile:
        source_home = os.environ.get("CODEX_HOME")
        if not source_home:
            raise SystemExit(f"required worker profile is unavailable: {profile}")
        source = Path(source_home).expanduser() / f"{profile}.config.toml"
        if not source.is_file() or source.is_symlink():
            raise SystemExit(f"required worker profile is unavailable: {profile}")
        payload = source.read_bytes()
        if len(payload) > 256 * 1024:
            raise SystemExit("required worker profile exceeds 256 KiB")
        try:
            profile_text = payload.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise SystemExit("required worker profile must be UTF-8") from exc
        if "\x00" in profile_text:
            raise SystemExit("required worker profile contains a NUL byte")
        try:
            profile_data = tomllib.loads(profile_text)
        except tomllib.TOMLDecodeError as exc:
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
    atomic_write_text(profile_path, profile_text)

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
            require_empty_output=not recovering_existing_container,
        )
    except IsolationError as exc:
        raise SystemExit(f"required worker execution spec is invalid [{exc.code}]: {exc}") from exc

    # No provider secret is materialized until every credential-free execution
    # invariant has passed.  From this point onward cleanup is part of the
    # persisted OCI lifecycle contract.
    isolated_env, secret_values = isolated_actor_env(project, actor, layout=layout)
    env_key = provider_env_key(actor)
    credential_path: Path | None = None
    if env_key and isolated_env.get(env_key):
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
    elif env_key:
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
            require_empty_output=not recovering_existing_container,
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
    governance_mode = governance.get("mode")
    governance_ready = governance.get("ready")
    if governance_mode not in {"off", "auto", "required"} or not isinstance(
        governance_ready,
        bool,
    ):
        raise SystemExit(
            "ArchMarshal governance gate blocked actor launch: governance state is invalid"
        )
    governed = governance_mode == "required" or governance_ready is True
    if (
        governed
        and actor.get("role") == "agent"
        and (actor.get("isolation") or {}).get("mode") == "unsafe-native"
    ):
        raise SystemExit(
            "ArchMarshal governance gate blocked actor launch: governed projects "
            "forbid unsafe-native provider launch; use required OCI isolation"
        )
    if governance_mode != "required" and not governance_ready:
        return False
    try:
        validation = validate_governance_binding(
            governance.get("binding"),
            project.get("workspace"),
            mode="required",
            wrapper_path=governance.get("wrapper_path"),
        )
    except GovernanceError as exc:
        if recovery_possible:
            # A prepared deterministic container may already exist after a
            # hard exit between external create and identity persistence.  The
            # caller must attach/clean only; it may not fresh-start or create a
            # missing credential while governance is stale.
            return True
        raise SystemExit(
            f"ArchMarshal governance gate blocked actor launch [{exc.code}]: {exc}"
        ) from exc
    if not validation.get("valid"):
        if recovery_possible:
            return True
        raise SystemExit(
            "ArchMarshal governance gate blocked actor launch: binding is not valid"
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
            if recovering_existing:
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
                    stdin_prompt=prompt.read_text(encoding="utf-8"),
                )
            else:
                handle = adapter.start(
                    required_spec,
                    required_command,
                    stdin_prompt=prompt.read_text(encoding="utf-8"),
                )
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
                events.extend(dict(event) for event in receipt.stdout_events)
                returncode = receipt.exit_code
                usage_known = True
            else:
                receipt = adapter.wait(handle)
                events.extend(dict(event) for event in receipt.stdout_events)
                returncode = receipt.exit_code
                usage_known = True
            validated = validate_output_exchange(required_spec.output_exchange)
            pending_report_text = redact_secret_values(validated.text, secret_values)
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
                if handle is not None:
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
                cleanup_unconfirmed = bool(exc.details.get("container_cleanup_unconfirmed"))
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
                            "container_id": handle.container_id if handle is not None else None,
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
            prompt_text = prompt.read_text(encoding="utf-8")
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

    changed_paths: tuple[str, ...] = ()
    if write_scopes:
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

    input_tokens, output_tokens = usage_from_events(events)
    if report.is_file() and secret_values:
        report_text = report.read_text(encoding="utf-8", errors="replace")
        safe_report_text = redact_secret_values(report_text, secret_values)
        if safe_report_text != report_text:
            atomic_write_text(report, safe_report_text)
    task_id = actor.get("task_id")
    final_report_status = report_status(report)
    collected_state = (
        "failed"
        if returncode != 0
        else final_report_status
        if final_report_status in {"failed", "escalate"}
        else "waiting_leader"
    )
    if task_id:
        _actor_fault("after_attempt_report_before_publish")
        publish_task_report(layout, actor, report)
    report_bytes = report.read_bytes() if report.is_file() else b""
    report_sha256 = hashlib.sha256(report_bytes).hexdigest() if report_bytes else None
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
                    "updated_at": now_iso(),
                    "error": None if returncode == 0 else f"actor process exited {returncode}",
                    "actor_id": actor_id,
                    "provider": actor.get("provider"),
                    "profile": actor.get("profile"),
                    "model": actor.get("model"),
                    "attempt_id": actor.get("attempt_id"),
                    "execution_workspace": str(execution_workspace),
                    "changed_paths": list(changed_paths),
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
            actor_tier = actor.get("tier") or ("low" if actor.get("provider") == "longcat" else "high")
            if actor_tier in {"low", "medium"} and needs_escalation and project.get("auto_escalate", True):
                command = "escalate_task"
                command_args = {
                    "task": task_id,
                    "actor": actor_id,
                    "attempt": actor.get("attempt_id"),
                    "reason": f"{actor.get('provider')} ({actor.get('tier')}) attempt requested escalation or exited {returncode}",
                    "start": True,
                }
                body = "Escalate this bounded task to the next stronger provider tier."
            else:
                command = "collect_task"
                command_args = {
                    "task": task_id,
                    "actor": actor_id,
                    "attempt": actor.get("attempt_id"),
                    "state": collected_state,
                    "summary": f"{actor.get('provider')} worker exited {returncode}; report ready.",
                }
                body = "Worker report is ready for manager review."
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
            output_tokens=output_tokens,
            changed_paths=list(changed_paths),
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
            "input_tokens": input_tokens,
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
