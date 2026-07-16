from __future__ import annotations

import contextlib
import functools
import hashlib
import io
import json
import math
import os
import re
import secrets
import stat
import subprocess
import sys
import tempfile
import threading
import time
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from .control_store import (
    ControlStoreError,
    apply_effect,
    audit_project_views,
    control_store_status,
    control_store_enabled,
    control_document_transaction,
    control_transaction,
    current_transaction,
    effect_status,
    fail_effect,
    lease_effect,
    observe_effect,
    reconcile_project_views,
    renew_effect_lease,
)
from .change_apply import (
    ChangeApplyError,
    PreparedChangePreview,
    apply_prepared_change_preview,
    prepare_change_preview,
    prepared_change_preview_from_dict,
    require_prepared_change_source_ready,
)
from .mailbox import deliver_outbox_message, inbox_message_ids, send_message
from .paths import ProjectLayout, actor_runtime_name, actor_target, default_root, make_project_id, relpath, resolve_project, slugify
from .session_backend import (
    actor_runtime,
    backend_from_session,
    command_display,
    command_to_string,
    format_actor_command,
    pid_start_marker,
    pid_is_alive,
    platform_summary,
    select_backend_kind,
    session_backend_config,
    session_backend_kind,
    session_name as backend_session_name,
)
from .security import (
    SecurityValidationError,
    normalize_allowed_path as secure_normalize_allowed_path,
    normalize_claim_path as secure_normalize_claim_path,
    normalize_path_list,
)
from .routing import (
    TIER_RANK,
    RoutingValidationError,
    acceptance_evidence_provenance,
    decide_route,
    default_provider_catalog,
    estimate_cost_cny as estimate_provider_cost,
    estimate_cost_nano_cny as estimate_provider_cost_units,
    next_stronger_provider,
    pricing_snapshot_status,
    provider_price_basis,
    project_provider_catalog,
    provider_by_id,
    route_plan_fingerprint,
    validate_provider_catalog,
)
from .governance import (
    GovernanceError,
    enforce_governance_contract,
    governance_launcher_path,
    inspect_governance,
)
from .handoff_contract import (
    HandoffContractError,
    build_apply_preview_contract,
    build_handoff_capsule,
    validate_attempt_input,
    validate_attempt_output,
    validate_collaboration_contract as validate_semantic_collaboration_contract,
    validate_handoff_capsule,
    validate_prompt_binding as validate_semantic_prompt_binding,
)
from .locking import (
    ProjectLockTimeout,
    project_write_lock,
    scheduler_daemon_lock,
    scheduler_instance_lock,
)
from .profile_binding import (
    ProfileBindingError,
    install_profile_snapshot,
    read_named_profile,
    synthetic_default_profile,
    validate_profile_binding,
    verify_profile_snapshot,
)
from .worker_isolation import (
    IsolationError,
    OciCliBackend,
    OciWorkerExecutionAdapter,
    ResourceLimits,
    UnsafeNativeBackend,
    WorkerExecutionSpec,
    cleanup_temporary_credential,
    select_worker_isolation_backend,
)
from .state import (
    ACTOR_STATES,
    ACTIVE_TASK_STATES,
    SCHEMA_VERSION,
    TASK_STATES,
    append_event,
    append_jsonl,
    atomic_write_bytes,
    atomic_write_json,
    atomic_write_text,
    actor_exists,
    actor_prompt_file,
    can_transition_task,
    compact_text,
    ensure_mailbox,
    ensure_runtime_dirs,
    load_actor,
    load_project,
    load_session,
    load_task,
    mailbox_counts,
    new_id,
    now_iso,
    read_json,
    read_jsonl,
    save_actor,
    save_project,
    save_session,
    save_task,
    next_task_id,
    task_exists,
    task_dir,
)


SCHEDULER_ID = "scheduler"
LEADER_ID = "leader"
RESULT_TASK_STATES = {"done", "failed", "escalate"}
RISKS = {"high", "medium", "low"}
LEADER_WORK_TYPES = {"planning", "integration", "verification", "emergency-fix", "trivial-glue", "other"}
SCHEDULER_COMMANDS = {
    "create_task",
    "dispatch_task",
    "collect_task",
    "record_result",
    "record_usage",
    "heartbeat",
    "stop_actor",
    "escalate_task",
}

PROVIDERS = {"auto", "codex", "deepseek", "longcat"}
SCHEDULER_FAULT_ENV = "COSTMARSHAL_SCHEDULER_FAULT"
SPAWN_EFFECT_TYPE = "spawn_actor"
STOP_EFFECT_TYPE = "stop_actor"
SPAWN_EFFECT_LEASE_SECONDS = 2.0
SPAWN_EFFECT_MAX_ATTEMPTS = 5
SCHEDULER_HEARTBEAT_SECONDS = 10.0
GOVERNANCE_PREFLIGHT_COMMANDS = frozenset(
    {
        "command_start_leader",
        "command_new_task",
        "command_dispatch",
        "command_escalate",
        "command_apply_changes",
        "command_relay",
        "command_recover",
    }
)
GOVERNANCE_SNAPSHOT_ATTEMPTS = 3
GOVERNANCE_PROJECT_MAX_BYTES = 8 * 1024 * 1024


class GovernancePreflightBlocked(SystemExit):
    """A governance refusal raised before any project-side mutation."""


def _scheduler_fault(name: str) -> None:
    """Crash-only integration hook used to prove effect recovery boundaries."""

    if os.environ.get(SCHEDULER_FAULT_ENV) == name:
        os._exit(96)


@contextlib.contextmanager
def _effect_lease_guard(
    layout: ProjectLayout,
    *,
    effect_id: str,
    owner: str,
) -> Any:
    """Keep one owner fenced across external runtime I/O.

    A daemon heartbeat extends only the still-live lease owned by this drainer.
    A process crash releases the scheduler instance lock and stops heartbeats;
    the unchanged expiry then remains the recovery boundary for a new owner.
    """

    stopped = threading.Event()
    failures: list[BaseException] = []
    interval = max(0.05, SPAWN_EFFECT_LEASE_SECONDS / 4.0)
    renew_effect_lease(
        layout,
        effect_id=effect_id,
        owner=owner,
        ttl_seconds=SPAWN_EFFECT_LEASE_SECONDS,
    )

    def heartbeat() -> None:
        while not stopped.wait(interval):
            try:
                renew_effect_lease(
                    layout,
                    effect_id=effect_id,
                    owner=owner,
                    ttl_seconds=SPAWN_EFFECT_LEASE_SECONDS,
                )
            except BaseException as exc:  # captured and re-raised on the drainer thread
                failures.append(exc)
                stopped.set()
                return

    thread = threading.Thread(
        target=heartbeat,
        name=f"costmarshal-effect-lease-{effect_id}",
        daemon=True,
    )
    thread.start()
    body_failed = False
    try:
        yield
    except BaseException:
        body_failed = True
        raise
    finally:
        stopped.set()
        thread.join(timeout=max(1.0, SPAWN_EFFECT_LEASE_SECONDS))
        if failures and not body_failed:
            current = effect_status(layout, effect_id)
            if current.get("status") != "applied":
                raise failures[0]


def print_json(payload: dict[str, Any]) -> None:
    print(json.dumps(payload, ensure_ascii=False, indent=2))


def default_scheduler_state() -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "id": SCHEDULER_ID,
        "role": "scheduler",
        "status": "idle",
        "pid": None,
        "started_at": None,
        "heartbeat_at": None,
        "last_cycle_at": None,
        "cycle_count": 0,
        "processed_commands": 0,
    }


def load_scheduler_state(layout: ProjectLayout) -> dict[str, Any]:
    state = read_json(layout.scheduler_state_json, default_scheduler_state())
    if not isinstance(state, dict):
        return default_scheduler_state()
    return {**default_scheduler_state(), **state}


def save_scheduler_state(layout: ProjectLayout, state: dict[str, Any]) -> None:
    state["schema_version"] = SCHEMA_VERSION
    atomic_write_json(layout.scheduler_state_json, state)


def update_scheduler_state(layout: ProjectLayout, **fields: Any) -> dict[str, Any]:
    state = load_scheduler_state(layout)
    state.update(fields)
    save_scheduler_state(layout, state)
    return state


def default_actor_command(model: str | None) -> str:
    """Legacy display helper retained for compatibility with existing callers."""
    if not model or model == "inherit":
        return "codex exec -"
    return f"codex exec --model {model} -"


def default_actor_argv(layout: ProjectLayout, actor: dict[str, Any]) -> list[str]:
    script = Path(__file__).resolve().parents[1] / "scripts" / "costmarshal_actor.py"
    argv = [
        sys.executable,
        str(script),
        "--root",
        str(layout.root),
        "--project",
        str(layout.project_dir),
        "--actor",
        str(actor["id"]),
    ]
    if actor.get("attempt_id"):
        argv.extend(["--attempt", str(actor["attempt_id"])])
    if actor.get("launch_token"):
        # urlsafe tokens may legally begin with "-".  Keep the capability in
        # the same argv element as its option so argparse cannot reinterpret a
        # random token as a new flag and kill the detached runner at startup.
        argv.append(f"--launch-token={actor['launch_token']}")
    return argv


def actor_launch_command(layout: ProjectLayout, session: dict[str, Any], actor: dict[str, Any]) -> str | list[str]:
    template = actor.get("command_template")
    if template:
        return format_actor_command(str(template), layout=layout, session=session, actor=actor)
    return default_actor_argv(layout, actor)


def redact_launch_token(value: Any, actor: dict[str, Any]) -> Any:
    """Keep the fencing capability out of logs, plans, and CLI responses."""

    token = str(actor.get("launch_token") or "")
    if not token:
        return value
    if isinstance(value, str):
        return value.replace(token, "<launch-token-redacted>")
    if isinstance(value, list):
        return [redact_launch_token(item, actor) for item in value]
    if isinstance(value, dict):
        return {key: redact_launch_token(item, actor) for key, item in value.items()}
    return value


def route_provider(
    task: dict[str, Any],
    requested: str | None = None,
    *,
    project: dict[str, Any] | None = None,
    history: list[dict[str, Any]] | None = None,
) -> str:
    """Compatibility wrapper around the fail-closed tier router."""

    catalog = project_provider_catalog(project or {})
    value = requested or task.get("provider_request") or task.get("provider") or "auto"
    provider_id = None if value == "auto" else str(value).lower()
    return decide_route(task, catalog, requested_provider_id=provider_id, history=history).provider_id


def provider_defaults(provider: str, model: str | None, profile: str | None) -> tuple[str, str | None]:
    if provider == "deepseek":
        return (model if model and model != "inherit" else "inherit", profile or "deepseek")
    if provider == "longcat":
        return (model if model and model != "inherit" else "LongCat-2.0", profile or "longcat")
    return (model or "inherit", profile)


def actor_summary(actor: dict[str, Any]) -> dict[str, Any]:
    runtime = actor_runtime(actor)
    return {
        "id": actor["id"],
        "role": actor["role"],
        "status": actor.get("status"),
        "model": actor.get("model"),
        "provider": actor.get("provider"),
        "tier": actor.get("tier"),
        "profile": actor.get("profile"),
        "attempt_id": actor.get("attempt_id"),
        "task_id": actor.get("task_id"),
        "path": f"scheduler/actors/{slugify(actor['id'], 'actor')}.json",
        "prompt_path": actor.get("prompt_path"),
        "runtime": runtime,
        "runtime_backend": runtime.get("backend"),
        "runtime_target": runtime.get("target"),
        "runtime_pid": runtime.get("pid"),
    }


def require_actor(layout: ProjectLayout, actor_id: str) -> None:
    if not actor_exists(layout, actor_id):
        raise SystemExit(f"Actor not found: {actor_id}")


def require_task(layout: ProjectLayout, task_id: str) -> None:
    if not task_exists(layout, task_id):
        raise SystemExit(f"Task not found: {task_id}")


def normalize_claim_path(path: str) -> str:
    try:
        return secure_normalize_claim_path(path).casefold()
    except SecurityValidationError as exc:
        raise SystemExit(str(exc)) from exc


def paths_conflict(left: str, right: str) -> bool:
    left_norm = normalize_claim_path(left)
    right_norm = normalize_claim_path(right)
    if left_norm == "." or right_norm == ".":
        return True
    return left_norm == right_norm or left_norm.startswith(right_norm + "/") or right_norm.startswith(left_norm + "/")


def path_is_claimed(path: str, claims: list[str]) -> bool:
    path_norm = normalize_claim_path(path)
    return any(path_norm == normalize_claim_path(claim) or path_norm.startswith(normalize_claim_path(claim) + "/") for claim in claims)


def load_locks(layout: ProjectLayout) -> dict[str, Any]:
    return read_json(layout.locks_json, {"schema_version": SCHEMA_VERSION, "updated_at": None, "claims": []})


def save_locks(layout: ProjectLayout, locks: dict[str, Any]) -> None:
    locks["schema_version"] = SCHEMA_VERSION
    locks["updated_at"] = now_iso()
    atomic_write_json(layout.locks_json, locks)


def active_lock_conflicts(layout: ProjectLayout, task_id: str, claim_paths: list[str]) -> list[dict[str, Any]]:
    conflicts: list[dict[str, Any]] = []
    normalized_paths = [normalize_claim_path(path) for path in claim_paths]
    if not normalized_paths:
        return conflicts
    locks = load_locks(layout)
    for claim in locks.get("claims", []):
        if claim.get("state") != "active" or claim.get("task_id") == task_id:
            continue
        for path in normalized_paths:
            if paths_conflict(path, claim.get("path", "")):
                conflicts.append(claim)
                break
    return conflicts


def add_task_claims(
    layout: ProjectLayout,
    *,
    task_id: str,
    actor: str | None,
    agent: str | None,
    claim_paths: list[str],
    override: bool = False,
) -> None:
    if not claim_paths:
        return
    locks = load_locks(layout)
    existing = {
        (claim.get("task_id"), claim.get("path"))
        for claim in locks.get("claims", [])
        if claim.get("state") == "active"
    }
    for raw_path in claim_paths:
        path = normalize_claim_path(raw_path)
        key = (task_id, path)
        if key in existing:
            continue
        locks.setdefault("claims", []).append(
            {
                "task_id": task_id,
                "actor_id": actor,
                "agent": agent,
                "path": path,
                "state": "active",
                "override": override,
                "created_at": now_iso(),
                "updated_at": now_iso(),
            }
        )
    save_locks(layout, locks)


def release_task_claims(layout: ProjectLayout, task_id: str, final_state: str) -> None:
    locks = load_locks(layout)
    changed = False
    for claim in locks.get("claims", []):
        if claim.get("task_id") == task_id and claim.get("state") == "active":
            claim["state"] = "released"
            claim["released_at"] = now_iso()
            claim["final_task_state"] = final_state
            claim["updated_at"] = now_iso()
            changed = True
    if changed:
        save_locks(layout, locks)


def assign_task_claim_actor(layout: ProjectLayout, task_id: str, actor_id: str) -> None:
    locks = load_locks(layout)
    changed = False
    for claim in locks.get("claims", []):
        if claim.get("task_id") == task_id:
            claim["actor_id"] = actor_id
            claim["state"] = "active"
            claim.pop("released_at", None)
            claim.pop("final_task_state", None)
            claim["updated_at"] = now_iso()
            changed = True
    if changed:
        save_locks(layout, locks)


def active_lock_rows(layout: ProjectLayout) -> list[dict[str, Any]]:
    return [claim for claim in load_locks(layout).get("claims", []) if claim.get("state") == "active"]


def require_non_negative_int(value: int | None, label: str) -> int:
    result = int(value or 0)
    if result < 0:
        raise SystemExit(f"{label} must be non-negative")
    return result


def require_non_negative_float(value: float | None, label: str) -> float | None:
    if value is None:
        return None
    result = float(value)
    if not math.isfinite(result) or result < 0:
        raise SystemExit(f"{label} must be finite and non-negative")
    return result


def require_probability(value: float | None, label: str) -> float | None:
    result = require_non_negative_float(value, label)
    if result is not None and result > 1.0:
        raise SystemExit(f"{label} must be between 0 and 1")
    return result


def total_tokens(
    input_tokens: int,
    output_tokens: int,
    cached_input_tokens: int = 0,
) -> int:
    return input_tokens + cached_input_tokens + output_tokens


LEGACY_PRICE_SNAPSHOT_SCHEMA = "costmarshal-beta-legacy-price-v1"


def _legacy_price_snapshot_hash(payload: dict[str, Any]) -> str:
    canonical = dict(payload)
    canonical.pop("snapshot_hash", None)
    encoded = json.dumps(
        canonical,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def build_legacy_price_snapshot(provider: dict[str, Any]) -> dict[str, Any] | None:
    """Bind beta legacy rates to an attempt without calling them reviewed."""

    if provider.get("pricing") is not None:
        return None
    input_price = provider.get("input_cny_per_1m")
    output_price = provider.get("output_cny_per_1m")
    if input_price is None or output_price is None:
        return None
    try:
        normalized_input = _money_text(input_price, "legacy input price")
        normalized_output = _money_text(output_price, "legacy output price")
    except ValueError:
        return None
    snapshot = {
        "schema_version": LEGACY_PRICE_SNAPSHOT_SCHEMA,
        "provider_id": str(provider.get("provider_id") or ""),
        "currency": "CNY",
        "input_per_1m": normalized_input,
        "output_per_1m": normalized_output,
    }
    snapshot["snapshot_hash"] = _legacy_price_snapshot_hash(snapshot)
    return snapshot


def _attempt_pricing_spec(
    project: dict[str, Any],
    attempt: dict[str, Any] | None,
    provider_id: str,
) -> tuple[dict[str, Any] | None, str, bool]:
    """Return the immutable pricing spec bound to an attempt.

    The boolean says whether the spec is durably bound and can settle budget.
    Historical beta attempts without a stored basis may still be estimated from
    the current legacy catalog, but that estimate remains explicitly unverified.
    """

    if attempt is not None:
        attempt_provider = str(attempt.get("provider") or "")
        if attempt_provider and attempt_provider != provider_id:
            raise RoutingValidationError(
                f"attempt provider {attempt_provider} does not match usage provider {provider_id}"
            )
        route = attempt.get("route_decision")
        if isinstance(route, dict):
            route_provider = str(route.get("provider_id") or "")
            if route_provider and route_provider != provider_id:
                raise RoutingValidationError(
                    f"route provider {route_provider} does not match usage provider {provider_id}"
                )
            snapshot = route.get("price_snapshot")
            if snapshot is not None:
                if not isinstance(snapshot, dict):
                    raise RoutingValidationError("attempt price_snapshot must be an object")
                snapshot_hash = str(snapshot.get("snapshot_hash") or "")
                return (
                    {"pricing": snapshot},
                    f"attempt_price_snapshot:{snapshot_hash}",
                    True,
                )
        legacy = attempt.get("legacy_price_snapshot")
        if legacy is not None:
            if not isinstance(legacy, dict):
                raise RoutingValidationError("attempt legacy_price_snapshot must be an object")
            expected_keys = {
                "schema_version",
                "provider_id",
                "currency",
                "input_per_1m",
                "output_per_1m",
                "snapshot_hash",
            }
            if set(legacy) != expected_keys:
                raise RoutingValidationError("attempt legacy_price_snapshot has unexpected fields")
            if legacy.get("schema_version") != LEGACY_PRICE_SNAPSHOT_SCHEMA:
                raise RoutingValidationError("attempt legacy_price_snapshot schema is unsupported")
            if legacy.get("provider_id") != provider_id or legacy.get("currency") != "CNY":
                raise RoutingValidationError("attempt legacy_price_snapshot identity mismatch")
            if legacy.get("snapshot_hash") != _legacy_price_snapshot_hash(legacy):
                raise RoutingValidationError("attempt legacy_price_snapshot hash mismatch")
            spec = {
                "input_cny_per_1m": legacy.get("input_per_1m"),
                "output_cny_per_1m": legacy.get("output_per_1m"),
            }
            return (
                spec,
                f"attempt_legacy_price_snapshot:{legacy['snapshot_hash']}",
                True,
            )

        current = provider_by_id(project_provider_catalog(project), provider_id)
        if current.get("pricing") is None:
            return current, "provider_catalog_legacy_unbound", False
        return None, "attempt_price_snapshot_missing", False

    current = provider_by_id(project_provider_catalog(project), provider_id)
    return current, "provider_catalog_without_attempt", False


def cost_source(estimated_cost_cny: float | None) -> str:
    return "caller" if estimated_cost_cny is not None else "not_provided"


def set_task_state(
    layout: ProjectLayout,
    task: dict[str, Any],
    state: str,
    *,
    error: str | None = None,
    allow_any_transition: bool = False,
) -> None:
    if state not in TASK_STATES:
        raise SystemExit(f"Invalid task state: {state}")
    current = task.get("status")
    if not allow_any_transition and not can_transition_task(current, state):
        raise SystemExit(f"Invalid task state transition: {task['id']} {current} -> {state}")
    task["status"] = state
    if state in {"done", "failed", "cancelled"}:
        try:
            release_route_budget_envelope(task, f"task_{state}")
        except ValueError as exc:
            raise SystemExit(f"Task budget reconciliation failed closed: {exc}") from exc
    save_task(layout, task)
    status_payload = {
        "schema_version": SCHEMA_VERSION,
        "task_id": task["id"],
        "state": state,
        "updated_at": now_iso(),
        "error": error,
    }
    atomic_write_json(task_dir(layout, task["id"]) / "status.json", status_payload)
    if state not in ACTIVE_TASK_STATES:
        release_task_claims(layout, task["id"], state)


def sync_actor_summary(layout: ProjectLayout, actor: dict[str, Any]) -> None:
    session = load_session(layout)
    session.setdefault("actors", {})[actor["id"]] = actor_summary(actor)
    save_session(layout, session)


def actor_role_contract(role: str) -> list[str]:
    if role == "leader":
        return [
            "Own planning, task decomposition, routing, verification, and final acceptance.",
            "Read structured reports and mailbox messages before raw transcripts.",
            "Do not perform substantial worker implementation without recording an explicit leader-work exception in the project notes.",
            "Ask the scheduler to dispatch bounded agent tasks instead of absorbing worker work.",
        ]
    if role == "agent":
        return [
            "Work only on the assigned task and explicit context in the task brief.",
            "Do the bounded work in the workspace; the runner owns task status, usage, mailbox, and report persistence.",
            "Escalate rather than changing write scope, reading raw transcripts, exposing secrets, or making architectural decisions outside the brief.",
            "Return one concise final report for leader verification; do not spend tokens trying to edit CostMarshal runtime files.",
        ]
    return ["Follow the CostMarshal v2 protocol for this actor role."]


def render_actor_prompt(layout: ProjectLayout, actor: dict[str, Any]) -> str:
    project = load_project(layout)
    session = load_session(layout)
    task_id = actor.get("task_id")
    task = load_task(layout, task_id) if task_id and task_exists(layout, task_id) else None
    mailbox = actor.get("mailbox") or {}
    lines = [
        f"# CostMarshal v2 Actor Prompt: {actor['id']}",
        "",
        f"Project: {project.get('name')} (`{project.get('project_id')}`)",
        f"Objective: {project.get('objective')}",
        f"Role: `{actor.get('role')}`",
        f"Actor status: `{actor.get('status')}`",
        f"Provider: `{actor.get('provider') or 'codex'}`",
        f"Profile: `{actor.get('profile') or 'default'}`",
        f"Model: `{actor.get('model')}`",
        f"Session backend: `{session_backend_kind(session)}`",
        f"Session: `{backend_session_name(session)}`",
        "",
        "## Role Contract",
    ]
    lines.extend(f"- {item}" for item in actor_role_contract(actor.get("role", "")))
    if actor.get("role") == "leader":
        lines.extend(
            [
                "",
                "## Required Files",
                f"- Runtime project: `{layout.project_dir}`",
                f"- Protocol: `{layout.protocol_md}`",
                f"- Actor state: `{layout.actors_dir / (slugify(actor['id'], 'actor') + '.json')}`",
                f"- Inbox: `{layout.project_dir / str(mailbox.get('inbox') or '')}`",
                "",
                "## Manager Output",
                "- Review durable task reports and return recommendations in one final response.",
                "- The runner persists that response as `reports/manager-latest.md`; use the CLI for state-changing decisions.",
            ]
        )
    else:
        lines.extend(
            [
                "",
                "## Efficiency Contract",
                "- The assigned task below is authoritative; do not reread the brief, inbox, status, protocol, or actor files.",
                "- Use tools only when the bounded task itself requires workspace inspection or edits.",
                "- Do not manage lifecycle or scheduler state; the deterministic runner does that after your final response.",
            ]
        )
    if task:
        task_path = task_dir(layout, task_id)
        lines.extend(
            [
                "",
                "## Assigned Task",
                f"- Task id: `{task_id}`",
                f"- Title: {task.get('title')}",
                f"- State: `{task.get('status')}`",
                f"- Task type: `{task.get('task_type')}`",
                f"- Risk: `{task.get('risk')}`",
                f"- Difficulty: `{task.get('difficulty')}`",
                f"- Purpose: {task.get('purpose')}",
                f"- Acceptance: {', '.join(task.get('acceptance') or []) or 'Leader acceptance is required.'}",
            ]
        )
        claimed_paths = task.get("claimed_paths") or []
        if claimed_paths:
            lines.extend(["", "## Claimed Write Paths"])
            lines.extend(f"- `{path}`" for path in claimed_paths)
    collaboration_contract = actor.get("collaboration_contract")
    if actor.get("role") == "agent" and isinstance(collaboration_contract, dict):
        lines.extend(
            [
                "",
                "## Isolated Collaboration Workspace",
                "- Work only inside `/workspace`; host workspace and source-project paths are not mounted.",
                f"- Fixed Git base: `{collaboration_contract.get('base_sha')}`",
                f"- Contract: `{collaboration_contract.get('contract_sha256')}`",
                "- Context is a tracked-file projection from that exact base; dirty and untracked host files are excluded.",
                "",
                "### Visible Context",
            ]
        )
        context_paths = collaboration_contract.get("context_paths") or []
        lines.extend(f"- `{path}`" for path in context_paths)
        if not context_paths:
            lines.append("- No repository files are visible initially.")
        lines.extend(["", "### Writable Scope"])
        write_scope = collaboration_contract.get("write_scope") or []
        lines.extend(f"- `{path}`" for path in write_scope)
        if not write_scope:
            lines.append("- No workspace writes are permitted.")
        lines.extend(
            [
                "- The runner captures allowed changes as a base-relative, content-addressed artifact for the next tier and leader review.",
                "- Stop and escalate instead of requesting or probing paths outside this contract.",
            ]
        )
    workspace = project.get("workspace")
    if workspace and not (
        actor.get("role") == "agent" and isinstance(collaboration_contract, dict)
    ):
        lines.extend(
            [
                "",
                "## Workspace",
                f"- `{workspace}`",
                "- Treat claimed and allowed write paths as relative to this workspace unless they are absolute.",
            ]
        )
    source_project = project.get("source_project")
    if source_project and not (
        actor.get("role") == "agent" and isinstance(collaboration_contract, dict)
    ):
        lines.extend(
            [
                "",
                "## Read-Only Source Project",
                f"- `{source_project}`",
                "- Treat this as reference material. Do not write into it from v2 unless the leader creates an explicit task allowing that path.",
            ]
        )
    lines.extend(
        [
            "",
            "## Recovery",
            "- If resumed after disconnect, read this prompt first, then inspect your inbox and assigned task status.",
            "- If task state conflicts with your local memory, trust the durable files and ask the scheduler/leader for a fresh dispatch message.",
            "- The runner persists your final response as the completion report and updates lifecycle files after you exit.",
            "- Do not edit `status.json`, `completion-report.md`, or scheduler mailboxes yourself.",
            "- End with exactly one final report using `Status: done`, `Status: failed`, or `Status: escalate`, plus result, evidence, and blockers.",
            "",
        ]
    )
    return "\n".join(lines)


def refresh_actor_prompt(layout: ProjectLayout, actor: dict[str, Any]) -> Path:
    prompt_path = actor_prompt_file(layout, actor["id"])
    actor["prompt_path"] = relpath(prompt_path, layout.project_dir)
    atomic_write_text(prompt_path, render_actor_prompt(layout, actor))
    return prompt_path


def make_actor(
    layout: ProjectLayout,
    *,
    actor_id: str,
    role: str,
    model: str,
    command_template: str | None,
    session_name: str,
    backend_kind: str,
    task_id: str | None = None,
    agent_name: str | None = None,
    provider: str = "codex",
    tier: str = "high",
    profile: str | None = None,
    env_key: str | None = None,
    attempt_id: str | None = None,
    launch_token: str | None = None,
    status: str = "configured",
) -> dict[str, Any]:
    mailbox = ensure_mailbox(layout, actor_id)
    runtime_name = actor_runtime_name(actor_id)
    actor = {
        "schema_version": SCHEMA_VERSION,
        "id": actor_id,
        "role": role,
        "status": status,
        "model": model,
        "provider": provider,
        "tier": tier,
        "profile": profile,
        "env_key": env_key,
        "attempt_id": attempt_id,
        "launch_token": launch_token,
        "agent_name": agent_name,
        "task_id": task_id,
        "created_at": now_iso(),
        "updated_at": now_iso(),
        "heartbeat_at": None,
        "mailbox": mailbox,
        "prompt_path": relpath(actor_prompt_file(layout, actor_id), layout.project_dir),
        "command_template": command_template,
        "runner": {
            "kind": "codex-exec",
            "sandbox": "workspace-write",
            "approval_policy": "never",
        },
        "context_policy": {
            "default": "Use mailbox messages and explicit task briefs only.",
            "raw_transcripts": "Do not read other actors' transcripts unless the leader explicitly authorizes an audit.",
        },
        "runtime": {
            "backend": backend_kind,
            "session_name": session_name,
            "actor_name": runtime_name,
            "target": actor_target(session_name, actor_id) if backend_kind == "tmux" else None,
            "pid": None,
            "log_path": None,
            "started_at": None,
            "last_launch_command": None,
        },
    }
    refresh_actor_prompt(layout, actor)
    return actor


def preflight_worker_isolation(
    layout: ProjectLayout,
    project: dict[str, Any],
    task: dict[str, Any],
    actor: dict[str, Any],
    *,
    unsafe_native: bool,
) -> dict[str, Any]:
    """Attest the requested worker boundary before reserving budget or state."""

    config = project.get("worker_isolation") or {}
    project_opt_in = bool(config.get("allow_unsafe_native_workers"))
    governance = project.get("governance") or {}
    if unsafe_native and (
        governance.get("mode") == "required" or governance.get("ready") is True
    ):
        raise SystemExit("ArchMarshal active governance forbids unsafe-native workers")
    mode = "unsafe-native" if unsafe_native else "required"
    if (
        mode == "required"
        and str(actor.get("provider") or "").lower() == "codex"
        and str(actor.get("tier") or "").lower() == "high"
        and not str(actor.get("env_key") or "").strip()
    ):
        raise SystemExit(
            "required worker preflight failed: built-in codex/high provider is missing env_key "
            "(expected CODEX_API_KEY); update the explicit provider catalog before dispatch"
        )
    configured_image = config.get("image")
    if not configured_image and mode == "unsafe-native":
        configured_image = "costmarshal/unsafe-native@sha256:" + ("0" * 64)
    limits_row = config.get("limits") or {}
    limits = ResourceLimits(
        memory_mb=int(limits_row.get("memory_mb") or 2048),
        cpus=float(limits_row.get("cpus") or 2.0),
        pids=int(limits_row.get("pids") or 256),
        timeout_seconds=float(limits_row.get("timeout_seconds") or 30.0),
    )
    scratch_root = layout.root / "worker-preflight"
    scratch_root.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="attempt-", dir=str(scratch_root)) as temporary_name:
        temporary = Path(temporary_name)
        profile = temporary / "profile.config.toml"
        profile.write_text("# CostMarshal preflight profile placeholder\n", encoding="utf-8")
        output = temporary / "out"
        output.mkdir()
        projected_workspace = temporary / "workspace"
        projected_workspace.mkdir()
        network_mode = "none" if mode == "unsafe-native" else str(config.get("network_mode") or "provider-proxy")
        network_name = None if network_mode == "none" else config.get("network_name")
        spec = WorkerExecutionSpec(
            project_id=str(project.get("project_id") or "project"),
            actor_id=str(actor["id"]),
            attempt_id=str(actor.get("attempt_id") or actor["id"]),
            image=str(configured_image or ""),
            # A preflight canary must never mount the host source workspace.
            # Actual execution receives a separate, attempt-bound projection.
            workspace=projected_workspace.resolve(),
            workspace_mode="rw" if (task.get("allowed_paths") or []) else "ro",
            profile_path=profile,
            output_exchange=output,
            isolation_mode=mode,
            engine=str(config.get("engine") or "auto"),
            network_mode=network_mode,
            network_name=str(network_name) if network_name else None,
            forbidden_mount_roots=(
                layout.project_dir.resolve(),
                Path(str(project["workspace"])).resolve(),
            )
            if mode == "required"
            else (),
            limits=limits,
        )
        try:
            selected = select_worker_isolation_backend(
                spec,
                docker=OciCliBackend("docker"),
                podman=OciCliBackend("podman"),
                unsafe_native=UnsafeNativeBackend(
                    project_opt_in=project_opt_in,
                    dispatch_opt_in=unsafe_native,
                ),
            )
        except IsolationError as exc:
            raise SystemExit(f"worker isolation preflight failed [{exc.code}]: {exc}") from exc
    attestation = selected.attestation.to_dict()
    return {
        "mode": mode,
        "project_opt_in": project_opt_in,
        "dispatch_opt_in": bool(unsafe_native),
        "attestation": attestation,
        "execution": {
            "engine": selected.backend.kind,
            "image": str(configured_image or ""),
            "network_mode": network_mode,
            "network_name": str(network_name) if network_name else None,
            "workspace_mode": spec.workspace_mode,
            "limits": {
                "memory_mb": limits.memory_mb,
                "cpus": limits.cpus,
                "pids": limits.pids,
                "timeout_seconds": limits.timeout_seconds,
                "tmpfs_mb": limits.tmpfs_mb,
                "home_tmpfs_mb": limits.home_tmpfs_mb,
            },
        },
    }


def protocol_text() -> str:
    return "\n".join(
        [
            "# CostMarshal v2 Protocol",
            "",
            "The scheduler is a relay and process supervisor. It does not perform project reasoning, implementation, or technical review.",
            "",
            "## Roles",
            "- Scheduler: creates actors, writes mailbox messages, checks heartbeats, records events, and recovers sessions.",
            "- Leader: an on-demand Codex manager that plans, decomposes work, verifies reports, and owns final acceptance.",
            "- Agent: executes one bounded task through an explicit low, medium, or high provider profile and returns a structured report.",
            "",
            "## Isolation",
            "- Actors communicate through `scheduler/mailboxes/<actor>/`.",
            "- Agent context is the task brief plus explicitly listed paths.",
            "- Raw transcripts belong in `transcripts/` and are not leader context by default.",
            "- The scheduler relays report paths and state, not raw reasoning.",
            "",
            "## Return Protocol",
            "- Agents return one structured final response; the actor runner writes `completion-report.md` and task status.",
            "- The actor runner records usage and asks the scheduler only to collect evidence for leader review.",
            "- A failed low/medium report remains waiting_leader; only an explicit leader rejection and escalation can start another provider.",
            "",
            "## Actor-Authored Scheduler Commands",
            "Append JSONL to your outbox. Example:",
            "```json",
            "{\"from\":\"leader\",\"to\":\"scheduler\",\"subject\":\"scheduler.command\",\"metadata\":{\"command\":\"dispatch_task\",\"args\":{\"task\":\"V2-0001\",\"model\":\"gpt-5\",\"start\":true}}}",
            "```",
            "The scheduler loop relays this through `run-scheduler`; the scheduler is still only a command executor, not a planner.",
            "",
        ]
    )


def command_init(args: Any) -> None:
    root = args.root.resolve()
    root.mkdir(parents=True, exist_ok=True)
    (root / "projects").mkdir(parents=True, exist_ok=True)
    project_id = make_project_id(args.name, args.objective)
    project_dir = root / "projects" / project_id
    if project_dir.exists():
        raise SystemExit(f"Project already exists: {project_dir}")
    layout = ProjectLayout(root=root, project_dir=project_dir)
    source_project = Path(args.source_project).expanduser().resolve() if args.source_project else None
    if source_project and not source_project.is_dir():
        raise SystemExit(f"Source project not found: {source_project}")
    workspace_arg = getattr(args, "workspace", None)
    workspace = Path(workspace_arg).expanduser().resolve() if workspace_arg else Path.cwd().resolve()
    if not workspace.is_dir():
        raise SystemExit(f"Workspace not found: {workspace}")
    try:
        workspace.relative_to(root)
        raise SystemExit("Actor workspace must not be inside the CostMarshal runtime root")
    except ValueError:
        pass
    try:
        root.relative_to(workspace)
        raise SystemExit("CostMarshal runtime root must not be inside the actor workspace")
    except ValueError:
        pass
    if source_project is not None:
        try:
            workspace.relative_to(source_project)
            raise SystemExit("Writable workspace must be disjoint from the read-only source project")
        except ValueError:
            pass
        try:
            source_project.relative_to(workspace)
            raise SystemExit("Writable workspace must be disjoint from the read-only source project")
        except ValueError:
            pass
    secrets_arg = getattr(args, "secrets_file", None)
    secrets_file = Path(secrets_arg).expanduser().resolve() if secrets_arg else None
    if secrets_file and not secrets_file.is_file():
        raise SystemExit(f"Secrets file not found: {secrets_file}")
    if secrets_file:
        try:
            secrets_file.relative_to(workspace)
            raise SystemExit("Secrets file must be outside the actor workspace")
        except ValueError:
            pass
    catalog_path = getattr(args, "provider_catalog", None)
    try:
        if catalog_path:
            raw_catalog = json.loads(
                Path(catalog_path).expanduser().read_text(encoding="utf-8"),
                parse_float=str,
            )
            provider_catalog = validate_provider_catalog(raw_catalog)
        else:
            provider_catalog = validate_provider_catalog(default_provider_catalog())
    except (OSError, json.JSONDecodeError, RoutingValidationError) as exc:
        raise SystemExit(f"Invalid provider catalog: {exc}") from exc
    raw_project_budget = getattr(args, "project_budget_cny", None)
    try:
        project_budget = (
            None
            if raw_project_budget is None
            else _money_text(raw_project_budget, "project-budget-cny")
        )
    except ValueError as exc:
        raise SystemExit(str(exc)) from exc
    default_min_success = require_probability(
        getattr(args, "default_min_success_probability", None),
        "default-min-success-probability",
    )
    governance_mode = str(getattr(args, "governance", None) or "auto")
    governance_launcher = getattr(args, "archmarshal_launcher", None)
    governance_wrapper = getattr(args, "archmarshal_wrapper", None)
    try:
        governance_inspection = inspect_governance(
            workspace,
            mode=governance_mode,
            launcher_path=governance_launcher,
            wrapper_path=governance_wrapper,
        )
    except GovernanceError as exc:
        raise SystemExit(f"ArchMarshal governance check failed [{exc.code}]: {exc}") from exc
    ensure_runtime_dirs(layout)
    session_name = args.session_name or f"cmv2-{slugify(project_id)[:42]}"
    requested_backend = getattr(args, "backend", None) or "auto"
    backend_kind = select_backend_kind(requested_backend)
    backend_command = getattr(args, "backend_command", None) or getattr(args, "tmux_command", None)
    if backend_kind == "tmux":
        backend_command = backend_command or "tmux"
    else:
        backend_command = backend_command or "local-process"
    project = {
        "schema_version": SCHEMA_VERSION,
        "project_id": project_id,
        "name": args.name or slugify(args.objective[:48], "project"),
        "objective": args.objective,
        "status": "active",
        "created_at": now_iso(),
        "updated_at": now_iso(),
        "runtime_root": str(root),
        "workspace": str(workspace),
        "secrets_file": str(secrets_file) if secrets_file else None,
        # Retained for state-file compatibility only. Workers never authorize
        # the next provider step; continuation is an explicit leader action.
        "auto_escalate": False,
        "escalation_policy": "explicit-leader-only",
        "allow_unsafe_custom_worker_commands": bool(getattr(args, "allow_unsafe_custom_worker_commands", False)),
        "worker_isolation": {
            "mode": str(getattr(args, "worker_isolation", None) or "required"),
            "engine": str(getattr(args, "container_engine", None) or "auto"),
            "image": getattr(args, "worker_image", None),
            "pull_policy": "never",
            "network_mode": str(getattr(args, "worker_network", None) or "provider-proxy"),
            "network_name": getattr(args, "worker_network_name", None),
            "allow_unsafe_native_workers": bool(getattr(args, "allow_unsafe_native_workers", False)),
            "limits": {"memory_mb": 2048, "cpus": 2.0, "pids": 256, "timeout_seconds": 30.0},
        },
        "provider_catalog": provider_catalog,
        "routing_policy": {
            "version": 2,
            "mode": "cost-performance",
            "tier_order": ["low", "medium", "high"],
            "project_budget_cny": project_budget,
            "default_min_success_probability": default_min_success,
            "prices_require_review": True,
        },
        "governance": {
            "provider": "archmarshal",
            "mode": governance_mode,
            "launcher_path": (
                str(Path(governance_launcher or governance_wrapper).expanduser().resolve())
                if governance_launcher or governance_wrapper
                else None
            ),
            "status": governance_inspection.get("status"),
            "ready": bool(governance_inspection.get("ready")),
            "doctor_state": governance_inspection.get("doctor_state"),
            "warnings": governance_inspection.get("warnings") or [],
            "binding": governance_inspection.get("binding"),
        },
        "manager_mode": "on-demand",
        "source_project": str(source_project) if source_project else None,
        "source_project_mode": "read-only-reference" if source_project else "none",
        "scheduler_contract": {
            "role": "relay-only",
            "must_not_do": ["technical planning", "implementation", "review", "reading raw actor transcripts by default"],
            "must_do": ["durable state", "mailbox relay", "process supervision", "recovery audit"],
        },
    }
    atomic_write_json(layout.project_json, project)
    session = {
        "schema_version": SCHEMA_VERSION,
        "project_id": project_id,
        "status": "configured",
        "created_at": now_iso(),
        "updated_at": now_iso(),
        "backend": {
            "kind": backend_kind,
            "requested": requested_backend,
            "session_name": session_name,
            "executable": backend_command,
            "enabled": True,
            "platform": platform_summary(),
        },
        "leader_actor_id": LEADER_ID,
        "actors": {},
        "task_bindings": {},
        "recovery": {"last_recovered_at": None, "last_status": "not_run", "issues": []},
    }
    atomic_write_json(layout.session_json, session)
    ensure_mailbox(layout, SCHEDULER_ID)
    leader = make_actor(
        layout,
        actor_id=LEADER_ID,
        role="leader",
        model=args.leader_model,
        command_template=getattr(args, "leader_command", None),
        session_name=session_name,
        backend_kind=backend_kind,
        provider="codex",
        tier="high",
        profile=getattr(args, "leader_profile", None),
    )
    save_actor(layout, leader)
    sync_actor_summary(layout, leader)
    atomic_write_text(layout.protocol_md, protocol_text())
    append_event(layout, "project_initialized", project_id=project_id, source_project=str(source_project) if source_project else None, backend=backend_kind)
    print_json({"status": "ok", "project": str(project_dir), "project_id": project_id, "session_name": session_name, "backend": backend_kind})


def start_actor(
    layout: ProjectLayout,
    actor: dict[str, Any],
    *,
    dry_run: bool,
    persist_runtime_state: bool = True,
) -> dict[str, Any]:
    project = load_project(layout)
    governance = project.get("governance") or {"mode": "off"}
    try:
        enforce_governance_contract(
            governance,
            project.get("workspace"),
            operation="actor launch",
        )
    except GovernanceError as exc:
        raise SystemExit(f"ArchMarshal governance gate blocked actor launch [{exc.code}]: {exc}") from exc
    if actor.get("role") == "agent" and actor.get("command_template"):
        if (actor.get("isolation") or {}).get("mode") == "required":
            raise SystemExit("Custom worker commands cannot bypass required OCI isolation")
        if not project.get("allow_unsafe_custom_worker_commands"):
            raise SystemExit("Custom worker commands are disabled because they bypass sandbox and secret isolation")
    session = load_session(layout)
    backend = backend_from_session(session)
    session_name = backend_session_name(session)
    if not session_name:
        raise SystemExit("v2 session is missing backend.session_name")
    prompt_path = actor_prompt_file(layout, actor["id"])
    command = actor_launch_command(layout, session, actor)
    runtime = actor_runtime(actor)
    runtime["backend"] = backend.kind
    runtime_name = runtime.get("actor_name") or actor_runtime_name(actor["id"])
    runtime["actor_name"] = runtime_name
    runtime["session_name"] = session_name
    if dry_run:
        session_exists = backend.session_exists(session_name) if backend.available() else False
        plan = backend.start_plan(session_name=session_name, actor_name=runtime_name, command=command, session_exists=session_exists)
        return {
            "actor": actor["id"],
            "dry_run": True,
            "backend": backend.kind,
            "backend_available": backend.available(),
            "prompt_file": str(prompt_path),
            "planned_commands": redact_launch_token([command_to_string(argv) for argv in plan], actor),
        }
    prompt_binding = actor.get("prompt_binding")
    if actor.get("role") == "agent" and isinstance(prompt_binding, dict):
        try:
            prompt_payload = prompt_path.read_bytes()
        except OSError as exc:
            raise SystemExit(f"Bound actor prompt is unavailable: {exc}") from exc
        observed_sha256 = "sha256:" + hashlib.sha256(prompt_payload).hexdigest()
        if (
            prompt_binding.get("schema") != PROMPT_BINDING_SCHEMA
            or prompt_binding.get("attempt_id") != actor.get("attempt_id")
            or prompt_binding.get("size_bytes") != len(prompt_payload)
            or prompt_binding.get("sha256") != observed_sha256
        ):
            raise SystemExit("Bound actor prompt changed after dispatch admission")
    else:
        prompt_path = refresh_actor_prompt(layout, actor)
    if persist_runtime_state:
        save_actor(layout, actor)
        sync_actor_summary(layout, actor)
    launch = backend.start_actor(
        session_name=session_name,
        actor_name=runtime_name,
        command=command,
        cwd=layout.project_dir,
        log_path=layout.transcripts_dir / f"{slugify(actor['id'], 'actor')}.log",
    )
    process_marker = pid_start_marker(launch.get("pid")) if backend.kind == "local" else None
    if persist_runtime_state:
        actor = load_actor(layout, actor["id"])
        runtime = actor_runtime(actor)
        actor["status"] = "running"
        runtime["started_at"] = now_iso()
        runtime["last_launch_command"] = redact_launch_token(command_display(command), actor)
        runtime["target"] = launch.get("target")
        runtime["pid"] = launch.get("pid")
        runtime["process_start_marker"] = process_marker
        runtime["log_path"] = relpath(Path(launch["log_path"]), layout.project_dir) if launch.get("log_path") else None
        save_actor(layout, actor)
        sync_actor_summary(layout, actor)
        append_event(layout, "actor_started", actor_id=actor["id"], backend=backend.kind, runtime_target=runtime.get("target"), pid=runtime.get("pid"))
    else:
        runtime = {
            "target": launch.get("target"),
            "pid": launch.get("pid"),
            "process_start_marker": process_marker,
            "log_path": relpath(Path(launch["log_path"]), layout.project_dir) if launch.get("log_path") else None,
        }
    return {
        "actor": actor["id"],
        "dry_run": False,
        "backend": backend.kind,
        "prompt_file": str(prompt_path),
        "commands": redact_launch_token(launch.get("commands", []), actor),
        "pid": runtime.get("pid"),
        "runtime_target": runtime.get("target"),
        "process_start_marker": runtime.get("process_start_marker"),
    }


def _spawn_effect_id(attempt_id: str) -> str:
    return f"EFF-SPAWN-{attempt_id}"


def _spawn_effect_payload(actor: dict[str, Any]) -> dict[str, Any]:
    launch_token = str(actor.get("launch_token") or "")
    profile_binding = actor.get("profile_binding") or {}
    return {
        "actor_id": str(actor["id"]),
        "task_id": str(actor.get("task_id") or ""),
        "attempt_id": str(actor.get("attempt_id") or ""),
        "launch_token_sha256": hashlib.sha256(launch_token.encode("utf-8")).hexdigest(),
        "profile_sha256": str(profile_binding.get("sha256") or "").removeprefix("sha256:"),
    }


def _stop_effect_payload(actor: dict[str, Any], *, reason: str | None) -> dict[str, Any]:
    runtime = actor.get("runtime") or {}
    return {
        "actor_id": str(actor["id"]),
        "attempt_id": str(actor.get("attempt_id") or ""),
        "process_start_marker": str(runtime.get("process_start_marker") or ""),
        "container_name": str(runtime.get("container_name") or ""),
        "reason": str(reason or ""),
    }


def _validate_stop_effect(layout: ProjectLayout, effect: dict[str, Any]) -> dict[str, Any]:
    payload = effect.get("payload") or {}
    actor_id = str(payload.get("actor_id") or "")
    if effect.get("effect_type") != STOP_EFFECT_TYPE or not actor_id or not actor_exists(layout, actor_id):
        raise ValueError("stop effect references a missing actor")
    actor = load_actor(layout, actor_id)
    if str(payload.get("attempt_id") or "") != str(actor.get("attempt_id") or ""):
        raise ValueError("stop effect attempt fence does not match the actor")
    expected_marker = str(payload.get("process_start_marker") or "")
    current_marker = str((actor.get("runtime") or {}).get("process_start_marker") or "")
    if expected_marker and current_marker and expected_marker != current_marker:
        raise ValueError("stop effect process marker is stale")
    return actor


def _required_stop_spec(layout: ProjectLayout, actor: dict[str, Any]) -> WorkerExecutionSpec:
    isolation = actor.get("isolation") or {}
    execution = isolation.get("execution") or {}
    runtime = actor.get("runtime") or {}
    session = load_session(layout)
    project_id = str(session.get("project_id") or layout.project_dir.name or "project")
    execution_workspace = runtime.get("execution_workspace")
    if not execution_workspace:
        # Older, pre-runtime-identity projects may still need their immutable
        # project configuration.  Recoverable OCI actors persist the execution
        # workspace before start, so corrupt project.json does not block STOP.
        project = load_project(layout)
        execution_workspace = project.get("workspace")
    attempt_id = str(actor.get("attempt_id") or "")
    bundle = (
        layout.root
        / "worker-bundles"
        / slugify(project_id, "project")
        / slugify(attempt_id, "attempt")
    ).resolve()
    limits_row = execution.get("limits") or {}
    credential_cleanup = runtime.get("credential_cleanup") or {}
    credential_path = (
        Path(str(credential_cleanup.get("path"))).resolve()
        if (
            credential_cleanup.get("required")
            and credential_cleanup.get("path")
            and Path(str(credential_cleanup.get("path"))).is_file()
        )
        else None
    )
    return WorkerExecutionSpec(
        project_id=project_id,
        actor_id=str(actor["id"]),
        attempt_id=attempt_id,
        image=str(execution.get("image") or ""),
        workspace=Path(str(execution_workspace or "")).resolve(),
        workspace_mode="rw" if execution.get("workspace_mode") == "rw" else "ro",
        profile_path=bundle / "profile.config.toml",
        output_exchange=bundle / "out",
        credential_path=credential_path,
        provider_env_key=str(actor.get("env_key")) if credential_path and actor.get("env_key") else None,
        credential_cleanup="delete-after-use" if credential_path else "preserve",
        credential_temp_root=bundle / "credential" if credential_path else None,
        isolation_mode="required",
        engine=str(execution.get("engine") or "auto"),
        network_mode=str(execution.get("network_mode") or "provider-proxy"),
        network_name=execution.get("network_name"),
        forbidden_mount_roots=(layout.project_dir.resolve(),),
        limits=ResourceLimits(
            memory_mb=int(limits_row.get("memory_mb") or 2048),
            cpus=float(limits_row.get("cpus") or 2.0),
            pids=int(limits_row.get("pids") or 256),
            timeout_seconds=float(limits_row.get("timeout_seconds") or 30.0),
            tmpfs_mb=int(limits_row.get("tmpfs_mb") or 256),
            home_tmpfs_mb=int(limits_row.get("home_tmpfs_mb") or 64),
        ),
    )


def _cleanup_prestart_credential(
    layout: ProjectLayout,
    actor: dict[str, Any],
) -> dict[str, Any] | None:
    """Delete a crash-left credential before any provider execution started."""

    runtime = actor.get("runtime") or {}
    cleanup = runtime.get("credential_cleanup") or {}
    if (
        runtime.get("provider_execution_state")
        in {"started", "finished_pending_finalize", "finished"}
        or cleanup.get("status") not in {"creating", "pending"}
        or not cleanup.get("required")
        or not cleanup.get("path")
    ):
        return None
    raw_path = Path(str(cleanup["path"]))
    if raw_path.exists():
        spec = _required_stop_spec(layout, actor)
        if spec.credential_path is None:
            raise IsolationError(
                "credential_cleanup_failed",
                "pre-start credential is not a safe regular file",
            )
        receipt = cleanup_temporary_credential(spec)
        credential_id = receipt.credential_id
        bytes_removed = receipt.bytes_removed
        deleted = receipt.deleted
    else:
        credential_id = hashlib.sha256(os.fsencode(raw_path.absolute())).hexdigest()[:24]
        bytes_removed = 0
        deleted = True
    actor_data = load_actor(layout, str(actor["id"]))
    actor_runtime_data = actor_data.setdefault("runtime", {})
    actor_runtime_data["credential_generation"] = int(
        actor_runtime_data.get("credential_generation") or 0
    ) + 1
    actor_runtime_data["credential_cleanup"] = {
        "required": True,
        "path": str(raw_path),
        "status": "deleted_recovered",
        "identifier": credential_id,
        "bytes_removed": bytes_removed,
        "recovered_at": now_iso(),
    }
    save_actor(layout, actor_data)
    sync_actor_summary(layout, actor_data)
    append_event(
        layout,
        "prestart_credential_recovered",
        actor_id=actor_data["id"],
        attempt_id=actor_data.get("attempt_id"),
        credential_identifier=credential_id,
        bytes_removed=bytes_removed,
    )
    return {
        "identifier": credential_id,
        "deleted": deleted,
        "bytes_removed": bytes_removed,
    }
def _registered_spawn_observation(actor: dict[str, Any]) -> dict[str, Any] | None:
    """Return durable proof that this exact fenced attempt already entered its runner."""

    payload = _spawn_effect_payload(actor)
    runtime = actor.get("runtime") or {}
    if runtime.get("registered_launch_token_sha256") != payload["launch_token_sha256"]:
        return None
    if runtime.get("registered_profile_sha256") != payload["profile_sha256"]:
        return None
    if runtime.get("provider_execution_state") not in {
        "started",
        "finished_pending_finalize",
        "finished",
    }:
        return None
    return {
        "source": "runner_registration",
        "actor_id": payload["actor_id"],
        "task_id": payload["task_id"],
        "attempt_id": payload["attempt_id"],
        "launch_token_sha256": payload["launch_token_sha256"],
        "backend": runtime.get("backend"),
        "pid": runtime.get("pid") or runtime.get("runner_pid"),
        "runtime_target": runtime.get("target"),
        "process_start_marker": runtime.get("process_start_marker"),
        "container_name": runtime.get("container_name"),
        "container_id": runtime.get("container_id"),
        "container_command": runtime.get("container_command"),
        "container_network_id": runtime.get("container_network_id"),
    }


def _validate_spawn_effect(layout: ProjectLayout, effect: dict[str, Any]) -> dict[str, Any]:
    payload = effect.get("payload") or {}
    actor_id = str(payload.get("actor_id") or "")
    task_id = str(payload.get("task_id") or "")
    attempt_id = str(payload.get("attempt_id") or "")
    if effect.get("effect_type") != SPAWN_EFFECT_TYPE or not actor_id or not task_id or not attempt_id:
        raise ValueError("spawn effect payload is incomplete")
    if not actor_exists(layout, actor_id) or not task_exists(layout, task_id):
        raise ValueError("spawn effect references missing actor or task")
    actor = load_actor(layout, actor_id)
    expected = _spawn_effect_payload(actor)
    if any(str(payload.get(key) or "") != str(expected[key]) for key in expected):
        raise ValueError("spawn effect fence or payload hash does not match the actor")
    task = load_task(layout, task_id)
    attempt = next(
        (row for row in task.get("attempts") or [] if row.get("attempt_id") == attempt_id),
        None,
    )
    if attempt is None or attempt.get("actor_id") != actor_id:
        raise ValueError("spawn effect attempt is not bound to its actor")
    if (task.get("attempts") or [])[-1].get("attempt_id") != attempt_id:
        raise ValueError("spawn effect attempt is stale")
    return actor


def _record_spawn_observation(
    layout: ProjectLayout,
    *,
    effect: dict[str, Any],
    observation: dict[str, Any],
) -> None:
    """Materialize launch metadata idempotently through the canonical control store."""

    effect_id = str(effect["effect_id"])
    payload = effect.get("payload") or {}
    with control_transaction(
        layout,
        command_name="effect_spawn_observed",
        command_id=f"EFFECT-STATE-{effect_id}",
        payload={"effect_id": effect_id, "observation": observation},
    ) as transaction:
        if transaction.replay:
            return
        actor = _validate_spawn_effect(layout, effect)
        runtime = actor.setdefault("runtime", {})
        if runtime.get("provider_execution_state") not in {
            "started",
            "finished_pending_finalize",
            "finished",
        }:
            actor["status"] = "running"
        runtime["started_at"] = runtime.get("started_at") or now_iso()
        runtime["target"] = observation.get("runtime_target") or runtime.get("target")
        runtime["pid"] = observation.get("pid") or runtime.get("pid")
        runtime["process_start_marker"] = (
            observation.get("process_start_marker") or runtime.get("process_start_marker")
        )
        runtime["log_path"] = observation.get("log_path") or runtime.get("log_path")
        runtime["spawn_effect_id"] = effect_id
        save_actor(layout, actor)
        sync_actor_summary(layout, actor)
        task = load_task(layout, str(payload["task_id"]))
        for attempt in task.get("attempts") or []:
            if attempt.get("attempt_id") == payload["attempt_id"]:
                if attempt.get("status") in {"preparing", "dispatched", "starting", "launch_pending"}:
                    attempt["status"] = "running"
                attempt["started_at"] = attempt.get("started_at") or now_iso()
                attempt["spawn_effect_id"] = effect_id
                break
        if task.get("status") == "dispatched":
            set_task_state(layout, task, "running")
        else:
            save_task(layout, task)
        append_event(
            layout,
            "actor_started",
            actor_id=actor["id"],
            task_id=payload["task_id"],
            attempt_id=payload["attempt_id"],
            backend=observation.get("backend"),
            runtime_target=observation.get("runtime_target"),
            pid=observation.get("pid"),
            effect_id=effect_id,
        )


def _record_stop_observation(
    layout: ProjectLayout,
    *,
    effect: dict[str, Any],
    observation: dict[str, Any],
) -> None:
    effect_id = str(effect["effect_id"])
    payload = effect.get("payload") or {}
    with control_transaction(
        layout,
        command_name="effect_stop_observed",
        command_id=f"EFFECT-STATE-{effect_id}",
        payload={"effect_id": effect_id, "observation": observation},
    ) as transaction:
        if transaction.replay:
            return
        actor = _validate_stop_effect(layout, effect)
        actor["status"] = "stopped"
        actor["stopped_at"] = actor.get("stopped_at") or now_iso()
        actor["stop_reason"] = payload.get("reason") or actor.get("stop_reason")
        actor["stop_effect_id"] = effect_id
        runtime = actor.setdefault("runtime", {})
        runtime["stop_observation"] = observation
        if observation.get("source") in {"oci_stop", "oci_already_absent"}:
            runtime["oci_lifecycle_state"] = "cleaned"
            cleanup = runtime.get("credential_cleanup")
            if isinstance(cleanup, dict):
                cleanup["status"] = (
                    "deleted" if observation.get("credential_deleted") else "cleanup_not_confirmed"
                )
        save_actor(layout, actor)
        sync_actor_summary(layout, actor)
        if actor.get("role") == "agent":
            send_message(
                layout,
                sender=SCHEDULER_ID,
                recipient=LEADER_ID,
                subject=f"Actor stopped: {actor['id']}",
                body=f"{actor['id']} is stopped. Reason: {payload.get('reason') or 'not specified'}.",
                task_id=actor.get("task_id"),
            )
        append_event(
            layout,
            "actor_stopped",
            actor_id=actor["id"],
            stop_runtime=True,
            reason=payload.get("reason"),
            effect_id=effect_id,
            observation_source=observation.get("source"),
        )


def _record_terminal_effect_failure(
    layout: ProjectLayout,
    *,
    effect: dict[str, Any],
    error: str,
    owner: str | None,
) -> None:
    """Atomically fail an effect and project recoverable actor/task state."""

    payload = effect.get("payload") or {}
    actor_id = str(payload.get("actor_id") or "")
    effect_id = str(effect["effect_id"])
    with control_transaction(
        layout,
        command_name="effect_terminal_failure",
        command_id=f"EFFECT-FAILURE-STATE-{effect_id}",
        payload={"effect_id": effect_id, "error": error},
    ) as transaction:
        if transaction.replay:
            return
        transaction.finalize_dead_effect(effect_id=effect_id, owner=owner, error=error)
        _scheduler_fault("effect.after_dead_status_before_projection")
        if not actor_id or not actor_exists(layout, actor_id):
            return
        actor = load_actor(layout, actor_id)
        runtime = actor.setdefault("runtime", {})
        runtime["effect_failure_id"] = effect_id
        runtime["effect_failure_error"] = error
        runtime["effect_failure_at"] = now_iso()
        task_id = actor.get("task_id")
        attempt_id = actor.get("attempt_id")
        stale_attempt = False
        if task_id and attempt_id and task_exists(layout, str(task_id)):
            task = load_task(layout, str(task_id))
            task_attempts = task.get("attempts") or []
            stale_attempt = bool(
                task_attempts and task_attempts[-1].get("attempt_id") != attempt_id
            )
            for attempt in task_attempts:
                if attempt.get("attempt_id") != attempt_id:
                    continue
                attempt["effect_failure_id"] = effect_id
                attempt["launch_error"] = error
                if not stale_attempt:
                    attempt["status"] = "needs_recovery"
                break
            save_task(layout, task)
        if stale_attempt:
            actor["status"] = "stopped"
            actor["stopped_at"] = actor.get("stopped_at") or now_iso()
            runtime["effect_failure_disposition"] = "stale_attempt_ignored"
        else:
            actor["status"] = "needs_recovery"
        save_actor(layout, actor)
        sync_actor_summary(layout, actor)
        append_event(
            layout,
            "runtime_effect_stale_ignored" if stale_attempt else "runtime_effect_failed_permanently",
            effect_id=effect_id,
            effect_type=effect.get("effect_type"),
            actor_id=actor_id,
            task_id=task_id,
            attempt_id=attempt_id,
            error=error,
        )


def _execute_stop_effect(
    layout: ProjectLayout,
    *,
    effect: dict[str, Any],
    owner: str,
) -> dict[str, Any]:
    actor = _validate_stop_effect(layout, effect)
    runtime = actor.get("runtime") or {}
    payload = effect.get("payload") or {}
    if effect.get("status") == "observed" or effect.get("observation"):
        observation = effect.get("observation") or {}
        if effect.get("status") != "observed":
            observe_effect(layout, effect_id=str(effect["effect_id"]), owner=owner, observation=observation)
    else:
        session = load_session(layout)
        backend = backend_from_session(session)
        backend_alive = backend.available() and backend.actor_alive(
            session_name=backend_session_name(session),
            actor_name=runtime.get("actor_name") or actor_runtime_name(actor["id"]),
            target=runtime.get("target"),
            pid=runtime.get("pid"),
            process_start_marker=runtime.get("process_start_marker"),
        )
        provider_finished = runtime.get("provider_execution_state") == "finished"
        source = "already_stopped" if provider_finished or not backend_alive else "backend_stop"
        container_status = None
        cleanup = None
        if (
            (actor.get("isolation") or {}).get("mode") == "required"
            and runtime.get("container_name")
            and runtime.get("container_command")
            and not provider_finished
        ):
            spec = _required_stop_spec(layout, actor)
            adapter = OciWorkerExecutionAdapter(OciCliBackend(spec.engine))
            durable_container_id = str(runtime.get("container_id") or "")
            try:
                handle = adapter.attach(
                    spec,
                    container_name=str(runtime["container_name"]),
                    container_id=durable_container_id or None,
                    command=tuple(str(item) for item in runtime["container_command"]),
                )
                if not durable_container_id:
                    def persist_stop_identity() -> None:
                        current_actor = _validate_stop_effect(layout, effect)
                        current_actor.setdefault("runtime", {})["container_id"] = handle.container_id
                        save_actor(layout, current_actor)

                    with control_transaction(
                        layout,
                        command_name="effect_stop_identity",
                        command_id=f"EFFECT-STOP-IDENTITY-{effect['effect_id']}",
                        payload={"effect_id": effect["effect_id"], "container_id": handle.container_id},
                    ) as identity_transaction:
                        if not identity_transaction.replay:
                            persist_stop_identity()
                    durable_container_id = handle.container_id
                    runtime["container_id"] = handle.container_id
                inspection = adapter.stop(handle, grace_seconds=10)
                cleanup = adapter.cleanup(handle)
                container_status = inspection.status
                source = "oci_stop"
            except IsolationError:
                if not re.fullmatch(r"[0-9a-f]{64}", durable_container_id):
                    raise
                cleanup = adapter.cleanup_confirmed_absent(
                    spec,
                    container_name=str(runtime["container_name"]),
                    container_id=durable_container_id,
                    command=tuple(str(item) for item in runtime["container_command"]),
                )
                container_status = "absent"
                source = "oci_already_absent"
        elif backend_alive:
            backend.stop_actor(
                target=str(runtime.get("target") or ""),
                pid=runtime.get("pid"),
                process_start_marker=runtime.get("process_start_marker"),
            )
        _scheduler_fault("effect.after_stop_before_observe")
        observation = {
            "source": source,
            "actor_id": actor["id"],
            "attempt_id": actor.get("attempt_id"),
            "process_start_marker": runtime.get("process_start_marker") or payload.get("process_start_marker"),
            "backend": runtime.get("backend"),
            "runtime_target": runtime.get("target"),
            "pid": runtime.get("pid"),
            "container_name": runtime.get("container_name"),
            "container_id": runtime.get("container_id"),
            "container_status": container_status,
            "container_removed": cleanup.container_removed if cleanup is not None else None,
            "credential_deleted": cleanup.credential.deleted if cleanup is not None else None,
        }
        observe_effect(layout, effect_id=str(effect["effect_id"]), owner=owner, observation=observation)
        _scheduler_fault("effect.after_stop_observe_before_apply")
    _record_stop_observation(layout, effect=effect, observation=observation)
    applied = apply_effect(
        layout,
        effect_id=str(effect["effect_id"]),
        owner=owner,
        result={"status": "stopped", "observation": observation},
    )
    return {
        "effect_id": str(effect["effect_id"]),
        "effect_type": STOP_EFFECT_TYPE,
        "actor_id": observation.get("actor_id"),
        "attempt_id": observation.get("attempt_id"),
        "status": applied.get("status"),
        "source": observation.get("source"),
    }


def _process_runtime_effects_under_instance_lock(
    layout: ProjectLayout,
    *,
    limit: int = 16,
    dry_run: bool = False,
    _governance_prevalidated: bool = False,
    _emergency_stop_only: bool = False,
    _effect_ids: tuple[str, ...] | None = None,
) -> dict[str, Any]:
    """Lease and apply recoverable runtime effects without holding a DB transaction over I/O."""

    if not _emergency_stop_only and not _governance_prevalidated:
        _validate_governance_preflight(layout, operation="runtime-effects")
    if not control_store_enabled(layout):
        return {"status": "disabled", "processed": [], "failed": [], "dry_run": bool(dry_run)}
    if dry_run:
        return {"status": "ok", "processed": [], "failed": [], "dry_run": True}
    owner = f"scheduler:{os.getpid()}:{secrets.token_hex(8)}"
    processed: list[dict[str, Any]] = []
    failed: list[dict[str, Any]] = []
    for _ in range(max(0, int(limit))):
        effect = lease_effect(
            layout,
            owner=owner,
            ttl_seconds=SPAWN_EFFECT_LEASE_SECONDS,
            effect_types=(STOP_EFFECT_TYPE,) if _emergency_stop_only else (SPAWN_EFFECT_TYPE, STOP_EFFECT_TYPE),
            effect_ids=_effect_ids,
        )
        if effect is None:
            break
        effect_id = str(effect["effect_id"])
        try:
            with _effect_lease_guard(layout, effect_id=effect_id, owner=owner):
                # The effect lease is canonical SQLite state, while the runtime
                # executors still validate/load actor and task compatibility
                # views.  A command can hard-exit after committing the effect but
                # before materializing those views.  Reconcile after the lease so
                # we also cover a concurrent command commit that happens while the
                # scheduler is selecting work; otherwise a valid effect can be
                # mistaken for corrupt input and marked dead.
                reconcile_project_views(layout)
                if effect.get("effect_type") == STOP_EFFECT_TYPE:
                    processed.append(_execute_stop_effect(layout, effect=effect, owner=owner))
                else:
                    actor = _validate_spawn_effect(layout, effect)
                    if effect.get("status") == "observed":
                        observation = effect.get("observation") or {}
                    else:
                        observation = effect.get("observation") or _registered_spawn_observation(actor)
                        if observation is None:
                            launch = start_actor(layout, actor, dry_run=False, persist_runtime_state=False)
                            observation = {
                                "source": "backend_start",
                                "actor_id": actor["id"],
                                "task_id": actor.get("task_id"),
                                "attempt_id": actor.get("attempt_id"),
                                "launch_token_sha256": _spawn_effect_payload(actor)["launch_token_sha256"],
                                "backend": launch.get("backend"),
                                "pid": launch.get("pid"),
                                "runtime_target": launch.get("runtime_target"),
                                "process_start_marker": launch.get("process_start_marker"),
                                "log_path": relpath(
                                    layout.transcripts_dir / f"{slugify(actor['id'], 'actor')}.log",
                                    layout.project_dir,
                                ),
                            }
                            _scheduler_fault("effect.after_spawn_before_observe")
                        observe_effect(layout, effect_id=effect_id, owner=owner, observation=observation)
                    _record_spawn_observation(layout, effect=effect, observation=observation)
                    applied = apply_effect(
                        layout,
                        effect_id=effect_id,
                        owner=owner,
                        result={"status": "started", "observation": observation},
                    )
                    processed.append(
                        {
                            "effect_id": effect_id,
                            "effect_type": SPAWN_EFFECT_TYPE,
                            "actor_id": observation.get("actor_id"),
                            "attempt_id": observation.get("attempt_id"),
                            "status": applied.get("status"),
                            "source": observation.get("source"),
                        }
                    )
        except ValueError as exc:
            error = f"{type(exc).__name__}: {exc}"
            _record_terminal_effect_failure(
                layout,
                effect=effect,
                error=error,
                owner=owner,
            )
            failed.append({"effect_id": effect_id, "retryable": False, "error": error})
        except (Exception, SystemExit) as exc:  # external runtime failures are durable and retryable
            error = f"{type(exc).__name__}: {exc}"
            retryable = int(effect.get("attempts") or 0) < SPAWN_EFFECT_MAX_ATTEMPTS
            if retryable:
                fail_effect(layout, effect_id=effect_id, owner=owner, error=error, retryable=True)
            else:
                _record_terminal_effect_failure(
                    layout,
                    effect=effect,
                    error=error,
                    owner=owner,
                )
            failed.append({"effect_id": effect_id, "retryable": retryable, "error": error})
    return {
        "status": "ok" if not failed else "degraded",
        "processed": processed,
        "failed": failed,
        "dry_run": False,
    }


def process_runtime_effects(
    layout: ProjectLayout,
    *,
    limit: int = 16,
    dry_run: bool = False,
    _governance_prevalidated: bool = False,
    _emergency_stop_only: bool = False,
    _effect_ids: tuple[str, ...] | None = None,
    _instance_timeout_seconds: float = 15.0,
) -> dict[str, Any]:
    """Run one mutually-exclusive normal or emergency runtime-effect drainer."""

    with scheduler_instance_lock(layout, timeout_seconds=_instance_timeout_seconds):
        return _process_runtime_effects_under_instance_lock(
            layout,
            limit=limit,
            dry_run=dry_run,
            _governance_prevalidated=_governance_prevalidated,
            _emergency_stop_only=_emergency_stop_only,
            _effect_ids=_effect_ids,
        )


def _drain_emergency_stop_effect(layout: ProjectLayout, *, effect_id: str) -> dict[str, Any]:
    """Apply one user-authorized STOP while governance blocks every start path.

    A crashed prior drainer can leave the effect leased/observed for the short
    recovery TTL.  Poll only that durable status, lease only STOP effects, and
    reuse the normal stop executor once the old lease expires.  Spawn effects,
    relay, scheduler inbox, and scheduler-cycle mutations are never entered.
    """

    deadline = time.monotonic() + SPAWN_EFFECT_LEASE_SECONDS + 2.0
    while True:
        current = effect_status(layout, effect_id)
        if current.get("effect_type") != STOP_EFFECT_TYPE:
            raise SystemExit(f"emergency stop rejected non-stop effect: {effect_id}")
        if current.get("status") == "applied":
            return current
        if current.get("status") == "dead":
            raise SystemExit(
                f"Emergency stop effect {effect_id} failed permanently: "
                f"{current.get('last_error') or 'unknown error'}"
            )
        try:
            batch = process_runtime_effects(
                layout,
                limit=1,
                dry_run=False,
                _emergency_stop_only=True,
                _effect_ids=(effect_id,),
                _instance_timeout_seconds=0.25,
            )
        except ProjectLockTimeout:
            # A healthy daemon owns the drainer mutex.  It will observe this
            # committed STOP in its next cycle; never steal or re-lease from it.
            current = effect_status(layout, effect_id)
            if current.get("status") == "applied":
                return current
            if time.monotonic() >= deadline:
                return current
            time.sleep(0.05)
            continue
        target_failure = next(
            (row for row in batch.get("failed") or [] if row.get("effect_id") == effect_id),
            None,
        )
        if target_failure is not None:
            raise SystemExit(
                f"Emergency stop effect {effect_id} remains retryable: "
                f"{target_failure.get('error') or 'unknown error'}"
            )
        current = effect_status(layout, effect_id)
        if current.get("status") == "applied":
            return current
        if time.monotonic() >= deadline:
            raise SystemExit(
                f"Emergency stop effect {effect_id} is still {current.get('status')}; "
                "retry the same command id"
            )
        time.sleep(0.05)


def command_start_leader(args: Any) -> None:
    layout = resolve_project(args.root, args.project)
    require_actor(layout, LEADER_ID)
    actor = load_actor(layout, LEADER_ID)
    if args.model:
        actor["model"] = args.model
    if getattr(args, "profile", None):
        actor["profile"] = args.profile
    actor["provider"] = "codex"
    if args.command:
        actor["command_template"] = args.command
    payload = start_actor(layout, actor, dry_run=args.dry_run)
    print_json({"status": "ok", **payload})


# These two receipts cover only host-context access and the bootstrap prompt.
# The scheduler-first low/medium/high semantic handoff uses handoff_contract.py
# and deliberately has a different wire kind.
COLLABORATION_CONTRACT_SCHEMA = "costmarshal-context-access-contract-v1"
PROMPT_BINDING_SCHEMA = "costmarshal-context-prompt-binding-v1"


def _canonical_contract_sha256(payload: dict[str, Any]) -> str:
    body = dict(payload)
    body.pop("contract_sha256", None)
    encoded = json.dumps(
        body,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def _required_context_paths(
    workspace: Path,
    raw_paths: list[object],
) -> tuple[str, ...]:
    """Normalize required-isolation context to the configured Git workspace."""

    normalized: list[str] = []
    for raw in raw_paths:
        if not isinstance(raw, str) or not raw.strip():
            raise SecurityValidationError("allowed context paths must be non-empty strings")
        candidate = Path(raw).expanduser()
        if candidate.is_absolute():
            resolved = candidate.resolve()
            try:
                relative = resolved.relative_to(workspace)
            except ValueError as exc:
                raise SecurityValidationError(
                    f"required-isolation context must be inside the configured workspace: {resolved}"
                ) from exc
            raw = relative.as_posix()
        normalized.append(secure_normalize_allowed_path(raw))
    return normalize_path_list(normalized, kind="allowed")


def _exact_workspace_base(workspace: Path, requested_base: str | None = None) -> str:
    try:
        git_root = Path(
            subprocess.check_output(
                ["git", "-C", str(workspace), "rev-parse", "--show-toplevel"],
                text=True,
                stderr=subprocess.STDOUT,
            ).strip()
        ).resolve()
        if git_root != workspace:
            raise SystemExit(
                "required worker context projection requires workspace to be the Git repository root"
            )
        revision = requested_base or "HEAD"
        exact = subprocess.check_output(
            ["git", "-C", str(workspace), "rev-parse", "--verify", f"{revision}^{{commit}}"],
            text=True,
            stderr=subprocess.STDOUT,
        ).strip().lower()
    except FileNotFoundError as exc:
        raise SystemExit("git is required for required worker context projection") from exc
    except subprocess.CalledProcessError as exc:
        detail = (exc.output or "").strip()
        raise SystemExit(
            f"required worker context projection requires a committed Git base: {detail}"
        ) from exc
    if not re.fullmatch(r"(?:[0-9a-f]{40}|[0-9a-f]{64})", exact):
        raise SystemExit("git returned an invalid full commit id for context projection")
    if requested_base is not None and exact != requested_base:
        raise SystemExit("the frozen collaboration base no longer resolves to the exact commit")
    return exact


def prepare_collaboration_contract(
    project: dict[str, Any],
    task: dict[str, Any],
) -> dict[str, Any]:
    """Create or revalidate the immutable task collaboration boundary."""

    workspace = Path(str(project.get("workspace") or "")).expanduser().resolve()
    if not workspace.is_dir():
        raise SystemExit("required worker context projection needs an existing workspace")
    try:
        context_paths = _required_context_paths(
            workspace,
            list(task.get("allowed_context") or []),
        )
        write_scope = normalize_path_list(
            task.get("allowed_paths") or [],
            kind="allowed",
        )
    except SecurityValidationError as exc:
        raise SystemExit(f"required worker collaboration contract is invalid: {exc}") from exc

    previous = task.get("collaboration_contract")
    previous_base = previous.get("base_sha") if isinstance(previous, dict) else None
    if previous_base is not None and not isinstance(previous_base, str):
        raise SystemExit("stored collaboration contract has an invalid base_sha")
    base_sha = _exact_workspace_base(workspace, previous_base)
    contract: dict[str, Any] = {
        "schema": COLLABORATION_CONTRACT_SCHEMA,
        "project_id": str(project.get("project_id") or ""),
        "task_id": str(task.get("id") or ""),
        "base_sha": base_sha,
        "context_paths": list(context_paths),
        "write_scope": list(write_scope),
        "projection": {
            "source": "tracked-git-objects-only",
            "workspace_mount": "/workspace",
            "exclude_git_metadata": True,
            "exclude_untracked_and_dirty_worktree": True,
            "sensitive_paths": "deny",
        },
        "change_exchange": {
            "format": "costmarshal-cumulative-changes",
            "base_relative": True,
            "content_addressed": True,
            "leader_apply_required": True,
        },
    }
    contract["contract_sha256"] = _canonical_contract_sha256(contract)
    if previous is not None and previous != contract:
        raise SystemExit(
            "stored collaboration contract does not match the frozen Git base/context/write scope"
        )
    return contract


def bind_actor_prompt(layout: ProjectLayout, actor: dict[str, Any]) -> dict[str, Any]:
    """Render and hash the exact prompt admitted for one attempt."""

    prompt_path = refresh_actor_prompt(layout, actor)
    payload = prompt_path.read_bytes()
    binding = {
        "schema": PROMPT_BINDING_SCHEMA,
        "attempt_id": actor.get("attempt_id"),
        "profile_sha256": (actor.get("profile_binding") or {}).get("sha256"),
        "collaboration_contract_sha256": (
            actor.get("collaboration_contract") or {}
        ).get("contract_sha256"),
        "size_bytes": len(payload),
        "sha256": "sha256:" + hashlib.sha256(payload).hexdigest(),
    }
    actor["prompt_binding"] = binding
    return binding


def render_task_brief(task: dict[str, Any]) -> str:
    return "\n".join(
        [
            f"# Task {task['id']}: {task['title']}",
            "",
            "You are a CostMarshal v2 agent actor. Work only from this brief and the explicitly listed context.",
            "",
            "## Purpose",
            task["purpose"],
            "",
            "## Task Type",
            task["task_type"],
            "",
            "## Routing",
            f"- Risk: {task.get('risk') or 'low'}",
            f"- Difficulty: {task.get('difficulty') or 'normal'}",
            f"- Requested provider: {task.get('provider_request') or task.get('provider') or 'auto'}",
            f"- Requested tier: {task.get('tier_request') or 'auto'}",
            f"- Preview route: {(task.get('route_preview') or {}).get('provider_id') or 'not evaluated'}",
            f"- Required capabilities: {', '.join(task.get('required_capabilities') or []) or 'none'}",
            (
                "- Estimated tokens (ordinary input / cached input / output): "
                f"{int(task.get('estimated_input_tokens') or 0)} / "
                f"{int(task.get('estimated_cached_input_tokens') or 0)} / "
                f"{int(task.get('estimated_output_tokens') or 0)}"
            ),
            "",
            "## Acceptance Criteria",
            "\n".join(f"- {item}" for item in task.get("acceptance", [])) or "- Leader acceptance is required.",
            "",
            "## Allowed Context",
            "\n".join(f"- {item}" for item in task.get("allowed_context", [])) or "- None.",
            "",
            "## Allowed Writes",
            "\n".join(f"- {item}" for item in task.get("allowed_paths", [])) or "- Only this task directory.",
            "",
            "## Claimed Writes",
            "\n".join(f"- {item}" for item in task.get("claimed_paths", [])) or "- None.",
            "",
            "## Return Protocol",
            "- Return one final response with `Status: done`, `Status: failed`, or `Status: escalate`.",
            "- Include result, evidence, blockers, and any decision needed from the manager.",
            "- The actor runner persists the report, task state, usage, and scheduler command after exit.",
            "- Do not edit CostMarshal runtime files or mailboxes yourself.",
            "- Stop and escalate if the task requires broader context, new write scope, secrets, or architectural judgment.",
            "",
        ]
    )


def command_new_task(args: Any) -> None:
    layout = resolve_project(args.root, args.project)
    command_id = getattr(args, "command_id", None)
    if command_id:
        existing = next((row for row in task_rows(layout) if row.get("created_by_command_id") == command_id), None)
        if existing:
            print_json({"status": "ok", "idempotent_replay": True, "task_id": existing["id"]})
            return
    if not str(args.title or "").strip():
        raise SystemExit("title is required")
    if not str(args.purpose or "").strip():
        raise SystemExit("purpose is required")
    project = load_project(layout)
    provider_request = str(getattr(args, "provider", None) or "auto").lower()
    tier_request = str(getattr(args, "tier", None) or "auto").lower()
    estimated_input_tokens = require_non_negative_int(getattr(args, "estimated_input_tokens", 0), "estimated-input-tokens")
    estimated_cached_input_tokens = require_non_negative_int(
        getattr(args, "estimated_cached_input_tokens", 0),
        "estimated-cached-input-tokens",
    )
    estimated_output_tokens = require_non_negative_int(getattr(args, "estimated_output_tokens", 0), "estimated-output-tokens")
    raw_max_cost = getattr(args, "max_cost_cny", None)
    try:
        max_cost_cny = (
            None
            if raw_max_cost is None
            else _money_text(raw_max_cost, "max-cost-cny")
        )
    except ValueError as exc:
        raise SystemExit(str(exc)) from exc
    explicit_min_success = require_probability(
        getattr(args, "min_success_probability", None),
        "min-success-probability",
    )
    project_min_success = require_probability(
        (project.get("routing_policy") or {}).get("default_min_success_probability"),
        "stored default-min-success-probability",
    )
    auto_economic_route = provider_request == "auto" and tier_request == "auto"
    if explicit_min_success is not None:
        effective_min_success = explicit_min_success
        min_success_source = "task-explicit"
    elif auto_economic_route and project_min_success is not None:
        effective_min_success = project_min_success
        min_success_source = "project-default"
    elif auto_economic_route:
        effective_min_success = None
        min_success_source = "legacy-none"
    else:
        effective_min_success = None
        min_success_source = "explicit-route-not-applicable"
    routing_stub = {
        "risk": getattr(args, "risk", "low"),
        "difficulty": getattr(args, "difficulty", "normal"),
        "task_type": args.task_type,
        "required_capabilities": getattr(args, "required_capabilities", None) or [],
        "min_success_probability": effective_min_success,
    }
    try:
        route_preview = decide_route(
            routing_stub,
            project_provider_catalog(project),
            requested_provider_id=None if provider_request == "auto" else provider_request,
            requested_tier=None if tier_request == "auto" else tier_request,
            history=trusted_result_rows(layout),
            execution_identities=_routing_execution_identities(project),
            input_tokens=estimated_input_tokens,
            cached_input_tokens=estimated_cached_input_tokens,
            output_tokens=estimated_output_tokens,
        )
    except (ProfileBindingError, RoutingValidationError) as exc:
        raise SystemExit(f"Task routing is invalid: {exc}") from exc
    task_id = args.id or next_task_id(layout)
    directory = task_dir(layout, task_id)
    if directory.exists():
        raise SystemExit(f"Task already exists: {task_id}")
    try:
        claim_paths = list(normalize_path_list(args.claim_path or [], kind="claim"))
        allowed_paths = list(normalize_path_list(args.allowed_path or [], kind="allowed"))
    except SecurityValidationError as exc:
        raise SystemExit(str(exc)) from exc
    uncovered_allowed = [path for path in allowed_paths if not path_is_claimed(path, claim_paths)]
    if uncovered_allowed:
        raise SystemExit(
            "Every allowed write path must be covered by a write claim: " + ", ".join(uncovered_allowed)
        )
    conflicts = active_lock_conflicts(layout, task_id, claim_paths)
    if conflicts and not args.allow_lock_conflict:
        raise SystemExit(
            "Path claim conflict:\n"
            + "\n".join(f"- {claim.get('path')} claimed by {claim.get('task_id')} ({claim.get('actor_id') or claim.get('agent')})" for claim in conflicts)
        )
    directory.mkdir(parents=True)
    task = {
        "schema_version": SCHEMA_VERSION,
        "id": task_id,
        "title": args.title,
        "purpose": args.purpose,
        "task_type": args.task_type,
        "risk": getattr(args, "risk", "low"),
        "difficulty": getattr(args, "difficulty", "normal"),
        "provider": "auto",
        "tier": "auto",
        "provider_request": provider_request,
        "tier_request": tier_request,
        "profile": getattr(args, "profile", None),
        "status": "planned",
        "agent_id": None,
        "agent_name": args.agent,
        "model": args.model,
        "estimated_input_tokens": estimated_input_tokens,
        "estimated_cached_input_tokens": estimated_cached_input_tokens,
        "estimated_output_tokens": estimated_output_tokens,
        "max_cost_cny": max_cost_cny,
        "required_capabilities": getattr(args, "required_capabilities", None) or [],
        "min_success_probability": effective_min_success,
        "min_success_probability_source": min_success_source,
        "route_preview": route_preview.to_dict(),
        "created_at": now_iso(),
        "updated_at": now_iso(),
        "acceptance": args.acceptance or [],
        "allowed_context": args.allowed_context or [],
        "allowed_paths": allowed_paths,
        "claimed_paths": [normalize_claim_path(path) for path in claim_paths],
        "lock_conflict_override": bool(args.allow_lock_conflict),
        "report_path": relpath(directory / "completion-report.md", layout.project_dir),
        "attempts": [],
        "created_by_command_id": command_id,
    }
    atomic_write_json(directory / "task.json", task)
    atomic_write_json(
        directory / "status.json",
        {"schema_version": SCHEMA_VERSION, "task_id": task_id, "state": "planned", "updated_at": now_iso(), "error": None},
    )
    atomic_write_text(directory / "brief.md", render_task_brief(task))
    atomic_write_text(
        directory / "completion-report.md",
        f"# Completion Report: {task_id}\n\nStatus: planned\n\n## Result\n\n## Evidence\n\n## Blockers\n\n## Decisions Needed From Leader\n\n",
    )
    add_task_claims(
        layout,
        task_id=task_id,
        actor=None,
        agent=args.agent,
        claim_paths=claim_paths,
        override=bool(args.allow_lock_conflict),
    )
    append_event(layout, "task_created", task_id=task_id, agent=args.agent, model=args.model)
    print_json({"status": "ok", "task_id": task_id, "task": str(directory)})


def command_route(args: Any) -> None:
    """Explain a three-tier route without mutating project state."""

    layout = resolve_project(args.root, args.project)
    project = load_project(layout)
    catalog = project_provider_catalog(project)
    explicit_min_success = require_probability(
        getattr(args, "min_success_probability", None),
        "min-success-probability",
    )
    project_min_success = require_probability(
        (project.get("routing_policy") or {}).get("default_min_success_probability"),
        "stored default-min-success-probability",
    )
    auto_economic_route = args.provider == "auto" and args.tier == "auto"
    effective_min_success = (
        explicit_min_success
        if explicit_min_success is not None
        else project_min_success if auto_economic_route else None
    )
    min_success_source = (
        "task-explicit"
        if explicit_min_success is not None
        else "project-default"
        if auto_economic_route and project_min_success is not None
        else "legacy-none"
        if auto_economic_route
        else "explicit-route-not-applicable"
    )
    task = {
        "risk": args.risk,
        "difficulty": args.difficulty,
        "task_type": args.task_type,
        "required_capabilities": getattr(args, "required_capabilities", None) or [],
        "min_success_probability": effective_min_success,
        "min_success_probability_source": min_success_source,
    }
    try:
        trusted_history = trusted_result_rows(layout)
        decision = decide_route(
            task,
            catalog,
            requested_provider_id=None if args.provider == "auto" else args.provider,
            requested_tier=None if args.tier == "auto" else args.tier,
            history=trusted_history,
            execution_identities=_routing_execution_identities(project),
            input_tokens=require_non_negative_int(args.estimated_input_tokens, "estimated-input-tokens"),
            cached_input_tokens=require_non_negative_int(
                args.estimated_cached_input_tokens,
                "estimated-cached-input-tokens",
            ),
            output_tokens=require_non_negative_int(args.estimated_output_tokens, "estimated-output-tokens"),
        )
    except (ProfileBindingError, RoutingValidationError) as exc:
        raise SystemExit(f"Unable to explain route: {exc}") from exc
    print_json(
        {
            "status": "ok",
            "project": str(layout.project_dir),
            "task": task,
            "decision": decision.to_dict(),
            "catalog": catalog,
        }
    )


def command_providers(args: Any) -> None:
    layout = resolve_project(args.root, args.project)
    catalog = project_provider_catalog(load_project(layout))
    print_json({"status": "ok", "project": str(layout.project_dir), "catalog": catalog})


def command_budget_status(args: Any) -> None:
    layout = resolve_project(args.root, args.project)
    project = load_project(layout)
    limit = (project.get("routing_policy") or {}).get("project_budget_cny")
    attempts: list[dict[str, Any]] = []
    tasks: list[dict[str, Any]] = []
    reconciliation_errors: list[str] = []
    commitment_units = 0
    for task in task_rows(layout):
        task_error = None
        task_commitment_value = None
        try:
            task_commitment_units = _task_budget_commitment_units(task)
            task_commitment_value = _money_from_units(task_commitment_units)
            commitment_units += task_commitment_units
        except ValueError as exc:
            task_error = str(exc)
            reconciliation_errors.append(f"{task['id']}: {task_error}")
        for attempt in task.get("attempts") or []:
            error = None
            attempt_commitment = None
            try:
                attempt_commitment = attempt_budget_commitment(attempt)
            except ValueError as exc:
                error = str(exc)
                if task_error is None:
                    reconciliation_errors.append(f"{task['id']}: {error}")
            attempts.append(
                {
                    "task_id": task["id"],
                    "attempt_id": attempt.get("attempt_id"),
                    "provider": attempt.get("provider"),
                    "status": attempt.get("status"),
                    "reserved_cost_cny": attempt.get("reserved_cost_cny"),
                    "actual_cost_cny": attempt.get("actual_cost_cny"),
                    "commitment_cny": attempt_commitment,
                    "cost_settled": bool(attempt.get("cost_settled")),
                    "reconciliation_status": "unknown" if error else "ok",
                    "reconciliation_error": error,
                }
            )
        tasks.append(
            {
                "task_id": task["id"],
                "status": task.get("status"),
                "commitment_cny": task_commitment_value,
                "route_budget_envelope": task.get("route_budget_envelope"),
                "reconciliation_status": "unknown" if task_error else "ok",
                "reconciliation_error": task_error,
            }
        )
    commitment_value = (
        None if reconciliation_errors else _money_from_units(commitment_units)
    )
    remaining_value = None
    if limit is not None and commitment_value is not None:
        try:
            remaining_value = _money_from_units(
                _money_units(limit, "stored project budget") - commitment_units
            )
        except ValueError as exc:
            reconciliation_errors.append(f"project budget: {exc}")
            commitment_value = None
    print_json(
        {
            "status": "blocked" if reconciliation_errors else "ok",
            "project": str(layout.project_dir),
            "limit_cny": limit,
            "commitment_cny": commitment_value,
            "remaining_cny": remaining_value,
            "reconciliation_errors": reconciliation_errors,
            "tasks": tasks,
            "attempts": attempts,
        }
    )


def command_governance_status(args: Any) -> None:
    layout = resolve_project(args.root, args.project)
    project = load_project(layout)
    governance = project.get("governance") or {"mode": "off", "ready": False}
    validation: dict[str, Any] | None = None
    error: str | None = None
    try:
        contract = enforce_governance_contract(
            governance,
            project.get("workspace"),
            operation="governance status",
        )
        validation = contract.get("validation")
    except GovernanceError as exc:
        error = f"{exc.code}: {exc}"
    print_json(
        {
            "status": "ok" if error is None else "blocked",
            "project": str(layout.project_dir),
            "governance": governance,
            "validation": validation,
            "error": error,
        }
    )
    if error:
        raise SystemExit(1)


def command_governance_rebind(args: Any) -> None:
    """Preview or explicitly replace only CostMarshal's read-only binding."""

    layout = resolve_project(args.root, args.project)
    project = load_project(layout)
    governance = project.get("governance") or {"mode": "off", "ready": False}
    if governance.get("mode") == "off":
        raise SystemExit("governance rebind requires an existing auto/required integration")
    launcher_path = (
        getattr(args, "archmarshal_launcher", None)
        or getattr(args, "archmarshal_wrapper", None)
        or governance_launcher_path(governance)
    )
    try:
        inspection = inspect_governance(
            project.get("workspace"),
            mode="required",
            launcher_path=launcher_path,
        )
    except GovernanceError as exc:
        raise SystemExit(f"ArchMarshal governance rebind blocked [{exc.code}]: {exc}") from exc
    new_binding = inspection.get("binding")
    if not inspection.get("ready") or not isinstance(new_binding, dict):
        raise SystemExit("ArchMarshal governance rebind blocked: current identity is not ready")
    old_binding = governance.get("binding")
    changed = old_binding != new_binding
    payload = {
        "status": "ok",
        "project": str(layout.project_dir),
        "mode": "apply" if args.apply else "preview",
        "changed": changed,
        "old_binding": old_binding,
        "new_binding": new_binding,
    }
    if not args.apply:
        print_json(payload)
        return
    history = list(governance.get("binding_history") or [])
    if old_binding is not None:
        history.append(
            {
                "binding": old_binding,
                "replaced_at": now_iso(),
                "reason": "explicit_costmarshal_rebind",
            }
        )
    governance.update(
        {
            "launcher_path": str(Path(launcher_path).expanduser().resolve()),
            "status": "ready",
            "ready": True,
            "doctor_state": inspection.get("doctor_state"),
            "warnings": [],
            "binding": new_binding,
            "binding_history": history[-10:],
            "rebound_at": now_iso(),
        }
    )
    governance.pop("wrapper_path", None)
    project["governance"] = governance
    save_project(layout, project)
    append_event(
        layout,
        "governance_binding_rebound",
        changed=changed,
        old_format=old_binding.get("format") if isinstance(old_binding, dict) else None,
        new_format=new_binding.get("format"),
    )
    print_json(payload)


def command_dispatch(args: Any) -> None:
    layout = resolve_project(args.root, args.project)
    require_task(layout, args.task)
    task = load_task(layout, args.task)
    command_id = getattr(args, "command_id", None)
    if command_id:
        existing_attempt = next((row for row in task.get("attempts") or [] if row.get("dispatch_command_id") == command_id), None)
        if existing_attempt:
            print_json({"status": "ok", "idempotent_replay": True, "task_id": args.task, "attempt": existing_attempt})
            return
    force = bool(getattr(args, "force", False))
    if task.get("status") in {"done", "failed", "cancelled"} and not force:
        raise SystemExit(f"Task is already terminal: {args.task}")
    existing_attempts = task.get("attempts") or []
    if (
        existing_attempts
        and existing_attempts[-1].get("status")
        in {"preparing", "dispatched", "launch_pending", "starting", "running", "needs_recovery"}
        and not getattr(args, "escalation_reason", None)
    ):
        raise SystemExit(
            f"Task already has an active attempt: {existing_attempts[-1].get('attempt_id')}; "
            "use the fenced escalation workflow"
        )
    conflicts = active_lock_conflicts(layout, args.task, task.get("claimed_paths") or [])
    if conflicts:
        raise SystemExit(
            "Path claim conflict while dispatching:\n"
            + "\n".join(
                f"- {claim.get('path')} claimed by {claim.get('task_id')} ({claim.get('actor_id') or claim.get('agent')})"
                for claim in conflicts
            )
        )
    project = load_project(layout)
    governance = project.get("governance") or {"mode": "off"}
    try:
        governance_contract = enforce_governance_contract(
            governance,
            project.get("workspace"),
            operation="dispatch",
        )
    except GovernanceError as exc:
        raise SystemExit(f"ArchMarshal governance gate blocked dispatch [{exc.code}]: {exc}") from exc
    unsafe_native = bool(getattr(args, "unsafe_native", False))
    if unsafe_native and governance_contract.get("governed"):
        raise SystemExit("ArchMarshal active governance forbids unsafe-native workers")
    collaboration_contract: dict[str, Any] | None = None
    try:
        catalog = project_provider_catalog(project)
        persisted_envelope = validate_route_budget_envelope(task)
        routing_task = task
        if persisted_envelope is not None and persisted_envelope.get("status") == "active":
            # The whole-chain SLA was already checked when the immutable plan
            # was admitted. A continuation routes one explicit bound step and
            # is validated against that plan below.
            routing_task = dict(task)
            routing_task.pop("min_success_probability", None)
        stored_provider_request = str(task.get("provider_request") or "auto").lower()
        stored_tier_request = str(task.get("tier_request") or "auto").lower()
        raw_provider = getattr(args, "provider", None)
        raw_tier = getattr(args, "tier", None)
        if not getattr(args, "escalation_reason", None):
            if raw_provider is not None and str(raw_provider).lower() != stored_provider_request:
                raise RoutingValidationError(
                    "dispatch cannot change the task's frozen provider routing mode; create a new task"
                )
            if raw_tier is not None and str(raw_tier).lower() != stored_tier_request:
                raise RoutingValidationError(
                    "dispatch cannot change the task's frozen tier routing mode; create a new task"
                )
            raw_provider = stored_provider_request
            raw_tier = stored_tier_request
        else:
            raw_provider = raw_provider if raw_provider is not None else stored_provider_request
            raw_tier = raw_tier if raw_tier is not None else stored_tier_request
        trusted_history = trusted_result_rows(layout)
        decision = decide_route(
            routing_task,
            catalog,
            requested_provider_id=None if raw_provider == "auto" else str(raw_provider).lower(),
            requested_tier=None if raw_tier == "auto" else str(raw_tier).lower(),
            history=trusted_history,
            execution_identities=_routing_execution_identities(project),
            input_tokens=int(task.get("estimated_input_tokens") or 0),
            cached_input_tokens=int(task.get("estimated_cached_input_tokens") or 0),
            output_tokens=int(task.get("estimated_output_tokens") or 0),
        )
    except (ProfileBindingError, RoutingValidationError, ValueError) as exc:
        raise SystemExit(f"Unable to route task: {exc}") from exc
    provider_spec = provider_by_id(catalog, decision.provider_id)
    semantic_trace_exists = task.get("handoff_contract") is not None or any(
        isinstance(previous_attempt, dict)
        and any(
            previous_attempt.get(field) is not None
            for field in (
                "attempt_input",
                "semantic_prompt_binding",
                "attempt_output",
                "attempt_output_sha256",
            )
        )
        for previous_attempt in task.get("attempts") or []
    )
    if (
        bool(getattr(args, "replan_active", False))
        and bool(task.get("attempts"))
        and semantic_trace_exists
    ):
        raise SystemExit(
            f"task {task.get('id') or '?'} cannot revise a sealed semantic route; "
            "continue its admitted envelope or create a new task"
        )
    max_cost = task.get("max_cost_cny")
    project_budget = (project.get("routing_policy") or {}).get("project_budget_cny")
    try:
        max_cost_units = (
            None if max_cost is None else _money_units(max_cost, "stored task budget")
        )
        project_budget_units = (
            None
            if project_budget is None
            else _money_units(project_budget, "stored project budget")
        )
    except ValueError as exc:
        raise SystemExit(f"Budget configuration failed closed: {exc}") from exc
    if max_cost is not None or project_budget is not None:
        if (
            not int(task.get("estimated_input_tokens") or 0)
            and not int(task.get("estimated_cached_input_tokens") or 0)
            and not int(task.get("estimated_output_tokens") or 0)
        ):
            raise SystemExit("Budgeted dispatch requires non-zero estimated input or output tokens")
        if decision.worst_case_chain_cost_cny is None:
            raise SystemExit(
                f"Budgeted dispatch requires a fully priced executable chain for provider {decision.provider_id}"
            )
        if any(
            (step.get("price_basis") or {}).get("kind") != "canonical"
            for step in decision.planned_steps
        ):
            raise SystemExit(
                "Budgeted dispatch requires current reviewed canonical pricing for every planned provider; "
                "beta legacy flat prices are compatibility-only"
            )
    try:
        financial_contract_required = bool(
            max_cost is not None
            or project_budget is not None
            or persisted_envelope is not None
            or decision.worst_case_chain_cost_cny is not None
        )
        if financial_contract_required:
            attempt_commitment_units = sum(
                _attempt_budget_commitment_units(row)
                for row in task.get("attempts") or []
            )
            current_task_commitment_units = _task_budget_commitment_units(task)
            current_task_commitment = _money_from_units(current_task_commitment_units)
        else:
            # An unpriced compatibility task with no configured budget has no
            # financial contract to reconcile. It remains visibly unknown in
            # `budget`, but can preserve legacy safe-tier escalation behavior.
            attempt_commitment_units = 0
            current_task_commitment = 0.0
            current_task_commitment_units = 0
        existing_envelope = persisted_envelope
        active_envelope = (
            existing_envelope
            if existing_envelope is not None and existing_envelope.get("status") == "active"
            else None
        )
        candidate_envelope: dict[str, Any] | None = None
        superseded_envelope: dict[str, Any] | None = None
        route_plan_step_index: int | None = None
        route_plan_step: dict[str, Any] | None = None
        if active_envelope is not None:
            next_bound_step = _envelope_step_index(task, active_envelope)
            replan_active = bool(getattr(args, "replan_active", False))
            if replan_active:
                if not bool(getattr(args, "allow_plan_revision", False)):
                    raise ValueError(
                        f"task {task.get('id') or '?'} automatic escalation cannot revise an active plan"
                    )
                superseded_envelope = active_envelope
                candidate_envelope = make_route_budget_envelope(
                    layout,
                    project,
                    task,
                    decision,
                    baseline_commitment_units=attempt_commitment_units,
                )
                if candidate_envelope is None:
                    raise ValueError(
                        f"task {task.get('id') or '?'} active plan revision requires a fully priced step"
                    )
                effective_envelope = candidate_envelope
                route_plan_step_index = 0
                route_plan_step = candidate_envelope["planned_steps"][0]
            elif next_bound_step < len(active_envelope["planned_steps"]):
                route_plan_step_index, route_plan_step = validate_envelope_dispatch_step(
                    task,
                    active_envelope,
                    decision,
                    provider_spec,
                    trusted_history=trusted_history,
                )
                effective_envelope = active_envelope
            else:
                # A manually requested escalation after a completed one-step
                # plan is an explicit revision, not an unbudgeted tail call.
                if not bool(getattr(args, "allow_plan_revision", False)):
                    raise ValueError(
                        f"task {task.get('id') or '?'} route plan is exhausted; "
                        "automatic escalation cannot revise the admitted plan"
                    )
                superseded_envelope = active_envelope
                candidate_envelope = make_route_budget_envelope(
                    layout,
                    project,
                    task,
                    decision,
                    baseline_commitment_units=attempt_commitment_units,
                )
                if candidate_envelope is None:
                    raise ValueError(
                        f"task {task.get('id') or '?'} explicit plan revision requires a fully priced step"
                    )
                effective_envelope = candidate_envelope
                route_plan_step_index = 0
                route_plan_step = candidate_envelope["planned_steps"][0]
        else:
            candidate_envelope = make_route_budget_envelope(
                layout,
                project,
                task,
                decision,
                baseline_commitment_units=attempt_commitment_units,
            )
            effective_envelope = candidate_envelope
            if candidate_envelope is not None:
                route_plan_step_index = 0
                route_plan_step = candidate_envelope["planned_steps"][0]
        if (
            candidate_envelope is not None
            and bool(task.get("attempts"))
            and semantic_trace_exists
        ):
            raise ValueError(
                f"task {task.get('id') or '?'} cannot revise a sealed semantic route; "
                "continue its admitted envelope or create a new task"
            )
        decision_step_cost = (decision.planned_steps[0] if decision.planned_steps else {}).get(
            "estimated_cost_cny"
        )
        hop_commitment_units = _money_units(
            decision_step_cost or 0,
            f"task {task.get('id') or '?'} dispatch estimated_cost_cny",
        )
        projected_attempt_commitment_units = attempt_commitment_units + hop_commitment_units
        future_step_commitment_units = 0
        if effective_envelope is not None and route_plan_step_index is not None:
            future_step_commitment_units = sum(
                _money_units(
                    step.get("estimated_cost_cny"),
                    f"task {task.get('id') or '?'} route plan future step estimated_cost_cny",
                )
                for step in effective_envelope["planned_steps"][route_plan_step_index + 1 :]
            )
        projected_task_commitment_units = (
            projected_attempt_commitment_units + future_step_commitment_units
        )
        incremental_commitment_units = max(
            0,
            projected_task_commitment_units - current_task_commitment_units,
        )
        projected_task_commitment = _money_from_units(projected_task_commitment_units)
        incremental_commitment = _money_from_units(incremental_commitment_units)
    except ValueError as exc:
        raise SystemExit(f"Task budget reconciliation failed closed: {exc}") from exc
    if max_cost_units is not None and projected_task_commitment_units > max_cost_units:
        raise SystemExit(
            f"Task budget exceeded: committed={round(current_task_commitment, 6)} "
            f"projected={projected_task_commitment} max={max_cost}"
        )
    if project_budget is not None:
        try:
            known_spend_units = _project_budget_commitment_units(layout)
            known_spend = _money_from_units(known_spend_units)
        except ValueError as exc:
            raise SystemExit(f"Project budget reconciliation failed closed: {exc}") from exc
        assert project_budget_units is not None
        if known_spend_units + incremental_commitment_units > project_budget_units:
            raise SystemExit(
                f"Project budget exceeded: committed={round(known_spend, 6)} "
                f"incremental={incremental_commitment} projected={round(known_spend + incremental_commitment, 9)} "
                f"max={project_budget}"
            )
    session = load_session(layout)
    attempt_id = new_id("ATT")
    launch_token = secrets.token_urlsafe(32)
    direct_step: dict[str, Any] | None = None
    try:
        dry_run = bool(getattr(args, "dry_run", False))
        if route_plan_step is not None and route_plan_step.get("profile_binding") is not None:
            profile_binding = validate_profile_binding(route_plan_step["profile_binding"])
        else:
            direct_step = json.loads(
                json.dumps(
                    route_plan_step or decision.planned_steps[0],
                    ensure_ascii=False,
                    allow_nan=False,
                )
            )
            direct_step = _bind_route_step_profiles(
                layout,
                project,
                attempt_id,
                [direct_step],
            )[0]
            profile_binding = validate_profile_binding(direct_step["profile_binding"])
        if bool(getattr(args, "start", False)):
            validate_profile_binding(profile_binding, require_available=True)
    except (ProfileBindingError, RoutingValidationError) as exc:
        raise SystemExit(f"Provider profile binding failed closed: {exc}") from exc
    default_actor_id = (
        f"agent-{args.task.lower()}"
        if not task.get("attempts")
        else f"agent-{args.task.lower()}-{decision.provider_id}-{len(task.get('attempts') or []) + 1}"
    )
    actor_id = args.actor_id or default_actor_id
    if (layout.actors_dir / f"{slugify(actor_id, 'actor')}.json").exists() and not force:
        raise SystemExit(f"Actor already exists: {actor_id}")
    provider = decision.provider_id
    tier = decision.tier
    requested_model = getattr(args, "model", None)
    requested_profile = getattr(args, "profile", None)
    if not task.get("attempts"):
        requested_model = requested_model or task.get("model")
        requested_profile = requested_profile or task.get("profile")
    model = requested_model if requested_model and requested_model != "inherit" else (decision.model or "inherit")
    profile = requested_profile or decision.profile
    bound_step_for_identity = route_plan_step or direct_step
    bound_execution_identity = (
        bound_step_for_identity.get("execution_identity")
        if isinstance(bound_step_for_identity, dict)
        else None
    )
    if (
        route_plan_step is None
        and isinstance(direct_step, dict)
        and isinstance(bound_execution_identity, dict)
        and bound_execution_identity.get("profile") == profile
        and model != bound_execution_identity.get("model")
    ):
        # A direct fallback has no admitted price-bound route identity yet.
        # Freeze an explicit model override into the attempt before any state
        # is persisted; admitted envelope steps remain immutable below.
        direct_step = json.loads(
            json.dumps(direct_step, ensure_ascii=False, allow_nan=False)
        )
        direct_step["model"] = model
        direct_step["execution_identity"]["model"] = model
        bound_step_for_identity = direct_step
        bound_execution_identity = direct_step["execution_identity"]
    if (
        not isinstance(bound_execution_identity, dict)
        or bound_execution_identity.get("profile") != profile
        or model not in {"inherit", bound_execution_identity.get("model")}
    ):
        raise SystemExit(
            "Dispatch model/profile override does not match the immutable execution identity"
        )
    if route_plan_step is not None:
        planned_model = route_plan_step.get("model") or "inherit"
        planned_profile = route_plan_step.get("profile")
        if model != planned_model or profile != planned_profile:
            raise SystemExit(
                "Dispatch model/profile override does not match the price-bound route plan: "
                f"planned model={planned_model!r} profile={planned_profile!r}, "
                f"requested model={model!r} profile={profile!r}"
            )
    command_template = getattr(args, "command", None)
    actor = make_actor(
        layout,
        actor_id=actor_id,
        role="agent",
        model=model,
        command_template=command_template,
        session_name=backend_session_name(session) or "costmarshal-v2",
        backend_kind=session_backend_kind(session),
        task_id=args.task,
        agent_name=args.agent or task.get("agent_name"),
        provider=provider,
        tier=tier,
        profile=profile,
        env_key=provider_spec.get("env_key"),
        attempt_id=attempt_id,
        launch_token=launch_token,
    )
    actor["profile_binding"] = profile_binding
    isolation = preflight_worker_isolation(
        layout,
        project,
        task,
        actor,
        unsafe_native=unsafe_native,
    )
    actor["isolation"] = isolation
    if not unsafe_native:
        # The OCI canary uses only an empty temporary mount. Freeze the real
        # Git/context boundary only after strong isolation is available, but
        # still before any durable attempt or budget reservation is written.
        collaboration_contract = prepare_collaboration_contract(project, task)
        actor["collaboration_contract"] = json.loads(
            json.dumps(
                collaboration_contract,
                ensure_ascii=False,
                allow_nan=False,
            )
        )
    prompt_binding = bind_actor_prompt(layout, actor)
    semantic_preview = {
        "actor": actor,
        "route_decision": decision.to_dict(),
        "route_budget_envelope": effective_envelope,
        "budget_projection": {
            "current_task_commitment_cny": current_task_commitment,
            "incremental_commitment_cny": incremental_commitment,
            "projected_task_commitment_cny": projected_task_commitment,
        },
    }
    expected_admission_fingerprint = getattr(
        args,
        "expected_prepared_admission_fingerprint",
        None,
    )
    if expected_admission_fingerprint is not None:
        observed_admission = _prepared_escalation_admission(semantic_preview)
        if _prepared_escalation_fingerprint(observed_admission) != expected_admission_fingerprint:
            raise SystemExit("Escalation successor admission changed before persistence")
    if not dry_run:
        try:
            if effective_envelope is not None:
                _materialize_envelope_profiles(layout, project, effective_envelope)
            elif direct_step is not None:
                _materialize_step_profile(layout, project, direct_step)
        except (ProfileBindingError, RoutingValidationError) as exc:
            raise SystemExit(f"Provider profile binding failed closed: {exc}") from exc
    if args.dry_run:
        plan = start_actor(layout, actor, dry_run=True) if args.start else {"planned_commands": []}
        actor_preview = dict(actor)
        if actor_preview.get("launch_token"):
            actor_preview["launch_token"] = "<redacted>"
        print_json(
            {
                "status": "ok",
                "dry_run": True,
                "actor": actor_preview,
                "route_decision": decision.to_dict(),
                "route_budget_envelope": effective_envelope,
                "budget_projection": semantic_preview["budget_projection"],
                "start_plan": plan,
            }
        )
        return
    deferred_start = bool(args.start and current_transaction() is not None and control_store_enabled(layout))
    if deferred_start and actor.get("command_template"):
        raise SystemExit("custom worker commands cannot use recoverable runtime effects after SQLite cutover")
    if deferred_start:
        actor["status"] = "starting"
    save_actor(layout, actor)
    sync_actor_summary(layout, actor)
    session = load_session(layout)
    session.setdefault("task_bindings", {})[args.task] = actor_id
    save_session(layout, session)
    task["agent_id"] = actor_id
    task["agent_name"] = args.agent or task.get("agent_name")
    task["model"] = model
    task["provider"] = provider
    task["tier"] = tier
    task["profile"] = profile
    task["route_decision"] = decision.to_dict()
    if collaboration_contract is not None:
        task["collaboration_contract"] = json.loads(
            json.dumps(
                collaboration_contract,
                ensure_ascii=False,
                allow_nan=False,
            )
        )
    if candidate_envelope is not None:
        if superseded_envelope is not None:
            archived_envelope = json.loads(
                json.dumps(superseded_envelope, ensure_ascii=False, allow_nan=False)
            )
            archived_envelope["status"] = "released"
            archived_envelope["released_at"] = now_iso()
            archived_envelope["release_reason"] = (
                "explicit_active_plan_revision"
                if bool(getattr(args, "replan_active", False))
                else "explicit_plan_revision"
            )
            task.setdefault("route_budget_envelope_history", []).append(archived_envelope)
        task["route_budget_envelope"] = candidate_envelope
    route_predecessors: list[dict[str, Any]] = []
    if effective_envelope is not None and route_plan_step_index is not None:
        for predecessor_index, predecessor in enumerate(
            effective_envelope["planned_steps"][:route_plan_step_index]
        ):
            predecessor_attempts = [
                row
                for row in task.get("attempts") or []
                if row.get("route_envelope_id") == effective_envelope.get("envelope_id")
                and row.get("route_plan_fingerprint")
                == effective_envelope.get("plan_fingerprint")
                and row.get("route_plan_step_index") == predecessor_index
            ]
            if len(predecessor_attempts) != 1:
                raise SystemExit(
                    "Route continuation lacks one authoritative predecessor attempt"
                )
            predecessor_attempt = predecessor_attempts[0]
            predecessor_identity = _attempt_execution_identity(predecessor_attempt)
            if (
                predecessor_identity is None
                or not isinstance(predecessor_attempt.get("leader_result_id"), str)
                or predecessor_attempt.get("accepted_by_leader") is not False
                or predecessor_attempt.get("recorded_result_status")
                not in {"failed", "escalate"}
            ):
                raise SystemExit(
                    "Route continuation requires an explicit, profile-bound leader rejection "
                    "for every predecessor"
                )
            route_predecessors.append(
                {
                    "provider_id": predecessor.get("provider_id"),
                    "model": predecessor_identity["model"],
                    "profile": predecessor_identity["profile"],
                    "profile_sha256": predecessor_identity["profile_sha256"],
                    "attempt_id": predecessor_attempt.get("attempt_id"),
                    "result_id": predecessor_attempt.get("leader_result_id"),
                }
            )
    execution_identity = bound_execution_identity
    if not isinstance(execution_identity, dict):
        raise SystemExit("Dispatch lacks an immutable provider execution identity")
    task.setdefault("attempts", []).append(
        {
            "attempt": len(task.get("attempts") or []) + 1,
            "attempt_id": attempt_id,
            "launch_token": launch_token,
            "actor_id": actor_id,
            "provider": provider,
            "tier": tier,
            "profile": profile,
            "model": model,
            "execution_identity": json.loads(
                json.dumps(execution_identity, ensure_ascii=False, allow_nan=False)
            ),
            "profile_binding": profile_binding,
            "status": "launch_pending" if deferred_start else "running" if args.start else "dispatched",
            "started_at": None if deferred_start else now_iso() if args.start else None,
            "finished_at": None,
            "route_decision": decision.to_dict(),
            "escalation_reason": getattr(args, "escalation_reason", None),
            "dispatch_command_id": command_id,
            "reserved_cost_cny": (
                route_plan_step.get("estimated_cost_cny")
                if route_plan_step is not None
                else decision_step_cost
            ),
            "actual_cost_cny": 0.0,
            "route_envelope_id": (
                effective_envelope.get("envelope_id")
                if effective_envelope is not None
                else None
            ),
            "route_plan_fingerprint": (
                effective_envelope.get("plan_fingerprint")
                if effective_envelope is not None
                else None
            ),
            "route_plan_step_index": route_plan_step_index,
            "route_plan_step": route_plan_step,
            "route_predecessors": route_predecessors,
            "collaboration_contract_sha256": (
                collaboration_contract.get("contract_sha256")
                if collaboration_contract is not None
                else None
            ),
            "prompt_binding": json.loads(
                json.dumps(prompt_binding, ensure_ascii=False, allow_nan=False)
            ),
            "legacy_price_snapshot": build_legacy_price_snapshot(provider_spec),
            "isolation": isolation,
        }
    )
    assign_task_claim_actor(layout, args.task, actor_id)
    set_task_state(layout, task, "dispatched", allow_any_transition=force)
    body = "\n".join(
        [
            f"Task: {args.task}",
            f"Brief: {relpath(task_dir(layout, args.task) / 'brief.md', layout.project_dir)}",
            f"Task directory: {relpath(task_dir(layout, args.task), layout.project_dir)}",
            "Use only the bounded prompt and explicitly listed context. Return one final report; the runner owns status and report files.",
        ]
    )
    send_message(layout, sender=SCHEDULER_ID, recipient=actor_id, subject=f"Dispatch {args.task}", body=body, task_id=args.task)
    send_message(layout, sender=SCHEDULER_ID, recipient=LEADER_ID, subject=f"Task dispatched: {args.task}", body=f"{args.task} is assigned to {actor_id}.", task_id=args.task)
    start_payload: dict[str, Any] | None = None
    if args.start:
        transaction = current_transaction()
        if deferred_start and transaction is not None:
            effect_id = _spawn_effect_id(attempt_id)
            transaction.queue_effect(
                effect_id=effect_id,
                effect_type=SPAWN_EFFECT_TYPE,
                aggregate_id=attempt_id,
                generation=len(task.get("attempts") or []),
                payload=_spawn_effect_payload(actor),
            )
            start_payload = {"status": "queued", "effect_id": effect_id}
        else:
            try:
                start_payload = start_actor(layout, actor, dry_run=False)
            except BaseException as exc:
                actor_state = load_actor(layout, actor_id)
                actor_state["status"] = "needs_recovery"
                actor_state.setdefault("runtime", {})["launch_error"] = f"{type(exc).__name__}: {exc}"
                save_actor(layout, actor_state)
                sync_actor_summary(layout, actor_state)
                failed_task = load_task(layout, args.task)
                for attempt in failed_task.get("attempts") or []:
                    if attempt.get("attempt_id") == attempt_id:
                        attempt["status"] = "needs_recovery"
                        attempt["launch_error"] = f"{type(exc).__name__}: {exc}"
                        break
                save_task(layout, failed_task)
                append_event(layout, "actor_launch_failed", actor_id=actor_id, task_id=args.task, attempt_id=attempt_id, error=f"{type(exc).__name__}: {exc}")
                raise
            task = load_task(layout, args.task)
            set_task_state(layout, task, "running")
    append_event(
        layout,
        "task_dispatched",
        task_id=args.task,
        actor_id=actor_id,
        provider=provider,
        tier=tier,
        profile=profile,
        model=model,
        route_plan_fingerprint=(
            effective_envelope.get("plan_fingerprint")
            if effective_envelope is not None
            else None
        ),
        route_envelope_id=(
            effective_envelope.get("envelope_id")
            if effective_envelope is not None
            else None
        ),
        route_plan_step_index=route_plan_step_index,
        route_plan_reserved_cost_cny=(
            effective_envelope.get("reserved_cost_cny")
            if effective_envelope is not None
            else None
        ),
        started=bool(args.start and not deferred_start),
        start_queued=deferred_start,
    )
    print_json(
        {
            "status": "ok",
            "task_id": args.task,
            "actor_id": actor_id,
            "prompt_file": str(layout.project_dir / actor.get("prompt_path", "")),
            "started": bool(args.start and not deferred_start),
            "start_queued": deferred_start,
            "start": start_payload,
            "route_plan_fingerprint": (
                effective_envelope.get("plan_fingerprint")
                if effective_envelope is not None
                else None
            ),
            "route_envelope_id": (
                effective_envelope.get("envelope_id")
                if effective_envelope is not None
                else None
            ),
            "route_plan_step_index": route_plan_step_index,
            "budget_projection": {
                "incremental_commitment_cny": incremental_commitment,
                "projected_task_commitment_cny": projected_task_commitment,
            },
        }
    )


def command_send(args: Any) -> None:
    layout = resolve_project(args.root, args.project)
    if args.to != SCHEDULER_ID:
        require_actor(layout, args.to)
    if args.sender not in {SCHEDULER_ID} and not actor_exists(layout, args.sender):
        raise SystemExit(f"Sender actor not found: {args.sender}")
    message = send_message(
        layout,
        sender=args.sender,
        recipient=args.to,
        subject=args.subject,
        body=args.message,
        task_id=args.task,
    )
    runtime_payload: dict[str, Any] | None = None
    if getattr(args, "runtime_send", False) or getattr(args, "tmux_send", False):
        actor = load_actor(layout, args.to)
        session = load_session(layout)
        backend = backend_from_session(session)
        runtime = actor_runtime(actor)
        target = runtime.get("target")
        if not target:
            raise SystemExit(f"Actor has no runtime target: {args.to}")
        runtime_payload = {"backend": backend.kind, **backend.send_text(target=target, text=args.message)}
    print_json({"status": "ok", "message": message, "runtime": runtime_payload})


def load_relay_cursors(layout: ProjectLayout) -> dict[str, Any]:
    return read_json(layout.relay_cursors_json, {"schema_version": SCHEMA_VERSION, "updated_at": now_iso(), "actors": {}})


def save_relay_cursors(layout: ProjectLayout, cursors: dict[str, Any]) -> None:
    cursors["schema_version"] = SCHEMA_VERSION
    cursors["updated_at"] = now_iso()
    atomic_write_json(layout.relay_cursors_json, cursors)


def relay_actor_outbox(
    layout: ProjectLayout,
    *,
    actor_id: str,
    limit: int | None = None,
    dry_run: bool = False,
) -> dict[str, Any]:
    require_actor(layout, actor_id)
    outbox_path = layout.mailboxes_dir / slugify(actor_id, "actor") / "outbox.jsonl"
    rows = read_jsonl(outbox_path)
    cursors = load_relay_cursors(layout)
    actor_cursor = cursors.setdefault("actors", {}).setdefault(actor_id, {})
    start_line = int(actor_cursor.get("outbox_lines") or 0)
    if start_line > len(rows):
        start_line = 0
    pending = rows[start_line:]
    if limit is not None:
        pending = pending[:limit]
    deliveries: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    for offset, row in enumerate(pending, start=start_line + 1):
        recipient = row.get("to")
        sender = row.get("from") or actor_id
        if sender != actor_id:
            raise SystemExit(f"Outbox line {offset} has from={sender}, expected {actor_id}")
        if not recipient:
            raise SystemExit(f"Outbox line {offset} is missing 'to'")
        if recipient != SCHEDULER_ID:
            require_actor(layout, recipient)
        message_id = row.get("id")
        already_delivered = isinstance(message_id, str) and message_id in inbox_message_ids(layout, recipient)
        if already_delivered:
            skipped.append({"line": offset, "id": message_id, "to": recipient, "reason": "already_delivered"})
            continue
        if dry_run:
            deliveries.append({"line": offset, "id": message_id, "from": sender, "to": recipient, "dry_run": True})
            continue
        message = dict(row)
        message["from"] = sender
        delivered = deliver_outbox_message(layout, message=message)
        deliveries.append({"line": offset, "id": delivered.get("id"), "from": sender, "to": recipient})
    if not dry_run:
        actor_cursor["outbox_lines"] = start_line + len(pending)
        actor_cursor["last_relayed_at"] = now_iso()
        actor_cursor["last_delivery_count"] = len(deliveries)
        actor_cursor["last_skipped_count"] = len(skipped)
        save_relay_cursors(layout, cursors)
    return {
        "status": "ok",
        "actor": actor_id,
        "outbox_lines": len(rows),
        "start_line": start_line,
        "processed": len(pending),
        "delivered": deliveries,
        "skipped": skipped,
        "dry_run": bool(dry_run),
    }


def command_relay(args: Any) -> None:
    layout = resolve_project(args.root, args.project)
    print_json(relay_actor_outbox(layout, actor_id=args.actor, limit=args.limit, dry_run=bool(args.dry_run)))


def as_list(value: Any) -> list[str] | None:
    if value is None:
        return None
    if isinstance(value, list):
        return [str(item) for item in value]
    return [str(value)]


def as_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return False
    if isinstance(value, (int, float)):
        return bool(value)
    return str(value).strip().lower() in {"1", "true", "yes", "y", "on"}


def run_cli_helper(layout: ProjectLayout, func: Any, **fields: Any) -> dict[str, Any]:
    args = SimpleNamespace(root=layout.root, project=str(layout.project_dir), **fields)
    output = io.StringIO()
    with contextlib.redirect_stdout(output):
        func(args)
    text = output.getvalue().strip()
    if not text:
        return {}
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return {"stdout": text}


def parse_scheduler_command(message: dict[str, Any]) -> tuple[str, dict[str, Any]] | None:
    metadata = message.get("metadata") if isinstance(message.get("metadata"), dict) else {}
    nested = metadata.get("scheduler") if isinstance(metadata.get("scheduler"), dict) else {}
    command = (
        nested.get("command")
        or metadata.get("scheduler_command")
        or metadata.get("command")
        or message.get("scheduler_command")
        or message.get("command")
    )
    args = nested.get("args") or metadata.get("args") or metadata.get("command_args") or message.get("args") or {}
    subject = str(message.get("subject") or "").strip().lower()
    body = str(message.get("body") or "").strip()
    if not command and subject.startswith("scheduler.command") and body.startswith("{"):
        try:
            body_payload = json.loads(body)
        except json.JSONDecodeError:
            body_payload = {}
        if isinstance(body_payload, dict):
            command = body_payload.get("command")
            args = body_payload.get("args") or {}
    if not command:
        return None
    if not isinstance(args, dict):
        raise ValueError("scheduler command args must be an object")
    normalized = str(command).strip().replace("-", "_")
    return normalized, args


def require_scheduler_command_authority(layout: ProjectLayout, *, sender: str, command: str, command_args: dict[str, Any], message: dict[str, Any]) -> None:
    if sender not in {LEADER_ID, SCHEDULER_ID}:
        require_actor(layout, sender)
    leader_only = {"create_task", "dispatch_task", "record_result", "escalate_task"}
    if command in leader_only and sender != LEADER_ID:
        raise SystemExit(f"{command} may only be issued by the leader")
    if command in {"collect_task", "record_usage", "heartbeat", "stop_actor"} and sender != LEADER_ID:
        actor = load_actor(layout, sender)
        target_actor = str(command_args.get("actor") or sender)
        if command in {"record_usage", "heartbeat", "stop_actor"} and target_actor != sender:
            raise SystemExit(f"{command} may only target the sender unless issued by the leader")
        if command == "collect_task":
            task_id = str(command_args.get("task") or message.get("task_id") or actor.get("task_id") or "")
            if actor.get("role") == "agent" and task_id and actor.get("task_id") != task_id:
                raise SystemExit(f"agent {sender} cannot collect task {task_id}")
            state = str(command_args.get("state") or "waiting_leader")
            if state != "waiting_leader":
                raise SystemExit(
                    "worker collect may only request waiting_leader; failed/escalate "
                    "are leader-owned result decisions"
                )
            task = load_task(layout, task_id)
            attempts = task.get("attempts") or []
            current = attempts[-1] if attempts else {}
            attempt_id = command_args.get("attempt")
            if not attempt_id or attempt_id != current.get("attempt_id") or sender != current.get("actor_id"):
                raise SystemExit(f"stale worker collect rejected for task {task_id}")


def execute_scheduler_command(
    layout: ProjectLayout,
    *,
    sender: str,
    command: str,
    command_args: dict[str, Any],
    message: dict[str, Any],
    dry_run: bool = False,
) -> dict[str, Any]:
    if command not in SCHEDULER_COMMANDS:
        raise SystemExit(f"Unsupported scheduler command: {command}")
    require_scheduler_command_authority(layout, sender=sender, command=command, command_args=command_args, message=message)
    if dry_run:
        return {"dry_run": True, "command": command, "args": command_args}
    command_id = message.get("id")
    if command == "create_task":
        return run_cli_helper(
            layout,
            command_new_task,
            id=command_args.get("id"),
            title=str(command_args.get("title") or ""),
            purpose=str(command_args.get("purpose") or ""),
            task_type=str(command_args.get("task_type") or "analysis"),
            agent=str(command_args.get("agent") or "auto"),
            model=str(command_args.get("model") or "inherit"),
            risk=str(command_args.get("risk") or "low"),
            difficulty=str(command_args.get("difficulty") or "normal"),
            provider=str(command_args.get("provider") or "auto"),
            tier=str(command_args.get("tier") or "auto"),
            profile=command_args.get("profile"),
            estimated_input_tokens=int(command_args.get("estimated_input_tokens") or 0),
            estimated_cached_input_tokens=int(
                command_args.get("estimated_cached_input_tokens") or 0
            ),
            estimated_output_tokens=int(command_args.get("estimated_output_tokens") or 0),
            max_cost_cny=command_args.get("max_cost_cny"),
            required_capabilities=as_list(command_args.get("required_capabilities")),
            min_success_probability=command_args.get("min_success_probability"),
            acceptance=as_list(command_args.get("acceptance")),
            allowed_context=as_list(command_args.get("allowed_context")),
            allowed_path=as_list(command_args.get("allowed_path")),
            claim_path=as_list(command_args.get("claim_path")),
            allow_lock_conflict=as_bool(command_args.get("allow_lock_conflict")),
            command_id=command_id,
        )
    if command == "dispatch_task":
        task_id = str(command_args.get("task") or message.get("task_id") or "")
        return run_cli_helper(
            layout,
            command_dispatch,
            task=task_id,
            actor_id=command_args.get("actor_id"),
            agent=command_args.get("agent"),
            model=command_args.get("model"),
            provider=command_args.get("provider"),
            tier=command_args.get("tier"),
            profile=command_args.get("profile"),
            command=command_args.get("actor_command") or command_args.get("command_template"),
            start=as_bool(command_args.get("start")),
            dry_run=False,
            force=as_bool(command_args.get("force")),
            unsafe_native=as_bool(command_args.get("unsafe_native")),
            escalation_reason=command_args.get("escalation_reason"),
            command_id=command_id,
        )
    if command == "escalate_task":
        task_id = str(command_args.get("task") or message.get("task_id") or "")
        return run_cli_helper(
            layout,
            command_escalate,
            task=task_id,
            reason=str(command_args.get("reason") or "Worker requested a stronger provider tier"),
            actor_id=command_args.get("actor_id"),
            from_actor=(command_args.get("actor") or sender) if sender != LEADER_ID else None,
            attempt=command_args.get("attempt"),
            profile=command_args.get("profile"),
            model=command_args.get("model"),
            provider=command_args.get("provider"),
            to_tier=command_args.get("to_tier"),
            start=as_bool(command_args.get("start")),
            dry_run=False,
            force=False,
            unsafe_native=as_bool(command_args.get("unsafe_native")),
            command_id=command_id,
        )
    if command == "collect_task":
        actor = command_args.get("actor") or (sender if sender != LEADER_ID else None)
        task_id = str(command_args.get("task") or message.get("task_id") or "")
        return run_cli_helper(
            layout,
            command_collect,
            task=task_id,
            actor=actor,
            attempt=command_args.get("attempt"),
            state=str(command_args.get("state") or "waiting_leader"),
            report=command_args.get("report"),
            summary=command_args.get("summary"),
            command_id=command_id,
        )
    if command == "record_result":
        task_id = str(command_args.get("task") or message.get("task_id") or "")
        return run_cli_helper(
            layout,
            command_record_result,
            task=task_id,
            status=str(command_args.get("status") or ""),
            quality_score=int(command_args.get("quality_score") or 0),
            accepted_by_leader=as_bool(command_args.get("accepted_by_leader") or command_args.get("accepted")),
            agent=command_args.get("agent"),
            actor=command_args.get("actor"),
            attempt=command_args.get("attempt"),
            model=command_args.get("model"),
            input_tokens=int(command_args.get("input_tokens") or 0),
            cached_input_tokens=int(command_args.get("cached_input_tokens") or 0),
            output_tokens=int(command_args.get("output_tokens") or 0),
            estimated_cost_cny=command_args.get("estimated_cost_cny"),
            summary=command_args.get("summary"),
            handoff=command_args.get("handoff"),
            note=command_args.get("note"),
            command_id=command_id,
        )
    if command == "record_usage":
        return run_cli_helper(
            layout,
            command_record_usage,
            actor=str(command_args.get("actor") or sender),
            task=command_args.get("task") or message.get("task_id"),
            attempt=command_args.get("attempt"),
            model=command_args.get("model"),
            input_tokens=int(command_args.get("input_tokens") or 0),
            cached_input_tokens=int(command_args.get("cached_input_tokens") or 0),
            output_tokens=int(command_args.get("output_tokens") or 0),
            estimated_cost_cny=command_args.get("estimated_cost_cny"),
            final_usage=as_bool(command_args.get("final_usage") or command_args.get("final")),
            note=command_args.get("note"),
            command_id=command_id,
        )
    if command == "heartbeat":
        return run_cli_helper(
            layout,
            command_heartbeat,
            actor=str(command_args.get("actor") or sender),
            status=str(command_args.get("status") or "running"),
            note=command_args.get("note"),
            command_id=command_id,
        )
    if command == "stop_actor":
        return run_cli_helper(
            layout,
            command_stop_actor,
            actor=str(command_args.get("actor") or sender),
            reason=command_args.get("reason"),
            stop_runtime=as_bool(command_args.get("stop_runtime")),
            kill_window=as_bool(command_args.get("stop_runtime")),
            dry_run=False,
            command_id=command_id,
        )
    raise SystemExit(f"Unsupported scheduler command: {command}")


def acknowledge_scheduler_command(layout: ProjectLayout, *, message: dict[str, Any], status: str, body: str, metadata: dict[str, Any] | None = None) -> None:
    sender = message.get("from")
    if not sender or sender == SCHEDULER_ID or not actor_exists(layout, str(sender)):
        return
    send_message(
        layout,
        sender=SCHEDULER_ID,
        recipient=str(sender),
        subject=f"Scheduler command {status}: {message.get('id') or '(no id)'}",
        body=body,
        task_id=message.get("task_id"),
        metadata=metadata,
    )


def process_scheduler_inbox(layout: ProjectLayout, *, limit: int | None = None, dry_run: bool = False) -> dict[str, Any]:
    ensure_mailbox(layout, SCHEDULER_ID)
    inbox_path = layout.mailboxes_dir / slugify(SCHEDULER_ID, "actor") / "inbox.jsonl"
    rows = read_jsonl(inbox_path)
    cursors = load_relay_cursors(layout)
    command_cursor = cursors.setdefault("scheduler_commands", {})
    start_line = int(command_cursor.get("inbox_lines") or 0)
    if start_line > len(rows):
        start_line = 0
    pending = rows[start_line:]
    if limit is not None:
        pending = pending[:limit]
    processed: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    failed: list[dict[str, Any]] = []
    # A SQLite scheduler work transaction advances the inbox cursor atomically
    # with command effects, acknowledgements, and audit rows. It therefore
    # does not need to rescan the complete event ledger every cycle. Legacy
    # file mode retains the historical replay check.
    completed_ids = (
        set()
        if current_transaction() is not None and control_store_enabled(layout)
        else {
            row.get("message_id")
            for row in read_jsonl(layout.events_jsonl)
            if row.get("event_type") == "scheduler_command_executed" and row.get("message_id")
        }
    )
    for offset, message in enumerate(pending, start=start_line + 1):
        try:
            if message.get("id") in completed_ids:
                skipped.append({"line": offset, "id": message.get("id"), "reason": "idempotent_replay"})
                continue
            parsed = parse_scheduler_command(message)
            if not parsed:
                skipped.append({"line": offset, "id": message.get("id"), "reason": "not_a_scheduler_command"})
                continue
            command, command_args = parsed
            sender = str(message.get("from") or "")
            result = execute_scheduler_command(layout, sender=sender, command=command, command_args=command_args, message=message, dry_run=dry_run)
            processed.append({"line": offset, "id": message.get("id"), "from": sender, "command": command, "result": result})
            if not dry_run:
                acknowledge_scheduler_command(
                    layout,
                    message=message,
                    status="ok",
                    body=f"Executed `{command}` from {sender}.",
                    metadata={"command": command, "result": result},
                )
                append_event(layout, "scheduler_command_executed", message_id=message.get("id"), sender=sender, command=command)
        except SystemExit as exc:
            error = str(exc)
            failed.append({"line": offset, "id": message.get("id"), "error": error})
            if not dry_run:
                acknowledge_scheduler_command(layout, message=message, status="failed", body=error, metadata={"error": error})
                append_event(layout, "scheduler_command_failed", message_id=message.get("id"), sender=message.get("from"), error=error)
        except Exception as exc:  # noqa: BLE001 - command failures are returned as durable scheduler errors
            error = f"{type(exc).__name__}: {exc}"
            failed.append({"line": offset, "id": message.get("id"), "error": error})
            if not dry_run:
                acknowledge_scheduler_command(layout, message=message, status="failed", body=error, metadata={"error": error})
                append_event(layout, "scheduler_command_failed", message_id=message.get("id"), sender=message.get("from"), error=error)
    if not dry_run:
        command_cursor["inbox_lines"] = start_line + len(pending)
        command_cursor["last_processed_at"] = now_iso()
        command_cursor["last_processed_count"] = len(processed)
        command_cursor["last_failed_count"] = len(failed)
        save_relay_cursors(layout, cursors)
    return {
        "status": "ok" if not failed else "degraded",
        "inbox_lines": len(rows),
        "start_line": start_line,
        "processed": processed,
        "skipped": skipped,
        "failed": failed,
        "dry_run": bool(dry_run),
    }


def _scheduler_relay_actor_ids(layout: ProjectLayout) -> list[str]:
    actor_ids: list[str] = []
    for path in sorted(layout.actors_dir.glob("*.json")):
        try:
            actor = read_json(path, {})
        except (FileNotFoundError, json.JSONDecodeError):
            continue
        actor_id = actor.get("id") if isinstance(actor, dict) else None
        if isinstance(actor_id, str) and actor_id:
            actor_ids.append(actor_id)
    return actor_ids


def _scheduler_pending_work_snapshot(
    layout: ProjectLayout,
    *,
    relay_limit: int | None,
    command_limit: int | None,
) -> dict[str, Any] | None:
    """Hash only the bounded mailbox rows that one control transaction will consume."""

    cursors = load_relay_cursors(layout)
    relay_rows: list[dict[str, Any]] = []
    for actor_id in _scheduler_relay_actor_ids(layout):
        path = layout.mailboxes_dir / slugify(actor_id, "actor") / "outbox.jsonl"
        rows = read_jsonl(path)
        actor_cursor = (cursors.get("actors") or {}).get(actor_id) or {}
        start_line = int(actor_cursor.get("outbox_lines") or 0)
        if start_line > len(rows):
            start_line = 0
        pending = rows[start_line:]
        if relay_limit is not None:
            pending = pending[:relay_limit]
        if pending:
            relay_rows.append(
                {
                    "actor_id": actor_id,
                    "start_line": start_line,
                    "rows": [
                        {
                            "id": row.get("id"),
                            "sha256": hashlib.sha256(
                                json.dumps(
                                    row,
                                    ensure_ascii=False,
                                    sort_keys=True,
                                    separators=(",", ":"),
                                    allow_nan=False,
                                ).encode("utf-8")
                            ).hexdigest(),
                        }
                        for row in pending
                    ],
                }
            )
    inbox_path = layout.mailboxes_dir / slugify(SCHEDULER_ID, "actor") / "inbox.jsonl"
    inbox_rows = read_jsonl(inbox_path)
    command_cursor = cursors.get("scheduler_commands") or {}
    command_start = int(command_cursor.get("inbox_lines") or 0)
    if command_start > len(inbox_rows):
        command_start = 0
    pending_commands = inbox_rows[command_start:]
    if command_limit is not None:
        pending_commands = pending_commands[:command_limit]
    if not relay_rows and not pending_commands:
        return None
    return {
        "schema_version": 1,
        "relay_limit": relay_limit,
        "command_limit": command_limit,
        "relays": relay_rows,
        "scheduler": {
            "start_line": command_start,
            "rows": [
                {
                    "id": row.get("id"),
                    "sha256": hashlib.sha256(
                        json.dumps(
                            row,
                            ensure_ascii=False,
                            sort_keys=True,
                            separators=(",", ":"),
                            allow_nan=False,
                        ).encode("utf-8")
                    ).hexdigest(),
                }
                for row in pending_commands
            ],
        },
    }


def _scheduler_work_command_id(snapshot: dict[str, Any]) -> str:
    encoded = json.dumps(
        snapshot,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return "SCHED-WORK-" + hashlib.sha256(encoded).hexdigest()


def _scheduler_heartbeat_due(state: dict[str, Any]) -> bool:
    raw = state.get("heartbeat_at")
    if not isinstance(raw, str) or not raw:
        return True
    try:
        observed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return True
    if observed.tzinfo is None:
        return True
    return (datetime.now(timezone.utc) - observed.astimezone(timezone.utc)).total_seconds() >= SCHEDULER_HEARTBEAT_SECONDS


def _update_scheduler_state_authoritative(
    layout: ProjectLayout,
    *,
    force: bool = True,
    count_cycle: bool = False,
    **fields: Any,
) -> dict[str, Any]:
    if not control_store_enabled(layout) or current_transaction() is not None:
        state = load_scheduler_state(layout)
        if not force and not _scheduler_heartbeat_due(state):
            return state
        if count_cycle:
            fields["cycle_count"] = int(state.get("cycle_count") or 0) + 1
        return update_scheduler_state(layout, **fields)
    state = load_scheduler_state(layout)
    if not force and not _scheduler_heartbeat_due(state):
        return state
    with control_document_transaction(layout):
        state = load_scheduler_state(layout)
        if not force and not _scheduler_heartbeat_due(state):
            return state
        if count_cycle:
            fields["cycle_count"] = int(state.get("cycle_count") or 0) + 1
        return update_scheduler_state(layout, **fields)


def scheduler_cycle(layout: ProjectLayout, *, relay_limit: int | None = None, command_limit: int | None = None, dry_run: bool = False) -> dict[str, Any]:
    # The canonical launcher has already passed the read-only governance gate. Repair
    # committed compatibility views before actor/outbox/inbox reads even when
    # there is no runtime effect to lease.  This closes the crash window for
    # runner finalization, usage, collect, and mailbox-only transactions.  The
    # second reconciliation after an effect lease remains necessary to cover a
    # command that commits concurrently with effect selection.
    if not dry_run and control_store_enabled(layout):
        reconcile_project_views(layout)
    effects = process_runtime_effects(
        layout,
        limit=command_limit or 16,
        dry_run=dry_run,
        _governance_prevalidated=True,
    )
    relays: list[dict[str, Any]] = []
    commands: dict[str, Any]
    sqlite_mode = bool(not dry_run and control_store_enabled(layout))
    snapshot = _scheduler_pending_work_snapshot(
        layout,
        relay_limit=relay_limit,
        command_limit=command_limit,
    )

    def process_control_work() -> tuple[list[dict[str, Any]], dict[str, Any]]:
        cycle_relays: list[dict[str, Any]] = []
        for actor_id in _scheduler_relay_actor_ids(layout):
            payload = relay_actor_outbox(
                layout,
                actor_id=actor_id,
                limit=relay_limit,
                dry_run=dry_run,
            )
            if payload["processed"] or payload["delivered"] or payload["skipped"]:
                cycle_relays.append(payload)
        cycle_commands = process_scheduler_inbox(
            layout,
            limit=command_limit,
            dry_run=dry_run,
        )
        return cycle_relays, cycle_commands

    if sqlite_mode and snapshot is not None:
        command_id = _scheduler_work_command_id(snapshot)
        with control_transaction(
            layout,
            command_name="scheduler_mailbox_cycle",
            command_id=command_id,
            payload=snapshot,
        ) as transaction:
            if transaction.replay:
                # Reconciliation at the start of the cycle normally makes a
                # replay unreachable; returning a quiet cycle remains safe.
                relays = []
                commands = {
                    "status": "ok",
                    "inbox_lines": 0,
                    "start_line": 0,
                    "processed": [],
                    "skipped": [],
                    "failed": [],
                    "dry_run": False,
                }
            else:
                authoritative_snapshot = _scheduler_pending_work_snapshot(
                    layout,
                    relay_limit=relay_limit,
                    command_limit=command_limit,
                )
                if authoritative_snapshot != snapshot:
                    raise ControlStoreError(
                        "scheduler mailbox snapshot changed before its authoritative transaction"
                    )
                relays, commands = process_control_work()
                state = load_scheduler_state(layout)
                update_scheduler_state(
                    layout,
                    status="running",
                    pid=os.getpid(),
                    heartbeat_at=now_iso(),
                    last_cycle_at=now_iso(),
                    cycle_count=int(state.get("cycle_count") or 0) + 1,
                    processed_commands=int(state.get("processed_commands") or 0)
                    + len(commands["processed"]),
                )
                append_event(
                    layout,
                    "scheduler_cycle",
                    scheduler_work_command_id=command_id,
                    processed_effect_count=len(effects["processed"]),
                    failed_effect_count=len(effects["failed"]),
                    relayed_actor_count=len(relays),
                    processed_command_count=len(commands["processed"]),
                    failed_command_count=len(commands["failed"]),
                    changed=True,
                )
                transaction.set_result({"status": commands["status"]})
    elif snapshot is not None or not sqlite_mode:
        relays, commands = process_control_work()
    else:
        commands = {
            "status": "ok",
            "inbox_lines": 0,
            "start_line": 0,
            "processed": [],
            "skipped": [],
            "failed": [],
            "dry_run": bool(dry_run),
        }
    changed = bool(effects["processed"] or effects["failed"] or relays or commands["processed"] or commands["failed"])
    if not dry_run and (not sqlite_mode or snapshot is None):
        _update_scheduler_state_authoritative(
            layout,
            force=bool(changed),
            count_cycle=True,
            status="running",
            pid=os.getpid(),
            heartbeat_at=now_iso(),
            last_cycle_at=now_iso(),
        )
        if changed and not sqlite_mode:
            append_event(
                layout,
                "scheduler_cycle",
                processed_effect_count=len(effects["processed"]),
                failed_effect_count=len(effects["failed"]),
                relayed_actor_count=len(relays),
                processed_command_count=len(commands["processed"]),
                failed_command_count=len(commands["failed"]),
                changed=True,
            )
    return {
        "status": "ok" if commands["status"] == "ok" and effects["status"] in {"ok", "disabled"} else "degraded",
        "changed": changed,
        "effects": effects,
        "relays": relays,
        "commands": commands,
    }


def _command_run_scheduler_loop(args: Any) -> None:
    layout = resolve_project(args.root, args.project)
    _validate_governance_preflight(layout, operation="run-scheduler")
    ensure_runtime_dirs(layout)
    ensure_mailbox(layout, SCHEDULER_ID)
    if control_store_enabled(layout):
        audit_project_views(layout, repair=True)
    started_at = now_iso()
    if not args.dry_run:
        _update_scheduler_state_authoritative(
            layout,
            status="running",
            pid=os.getpid(),
            started_at=started_at,
            heartbeat_at=started_at,
            last_cycle_at=None,
            stopped_at=None,
        )
    interval = max(float(args.interval), 0.05)
    max_cycles = 1 if args.once else int(args.max_cycles or 0)
    cycle_count = 0
    changed_cycles = 0
    processed_effect_count = 0
    failed_effect_count = 0
    processed_command_count = 0
    failed_command_count = 0
    last_effects: dict[str, Any] | None = None
    all_cycles_ok = True
    completed_status = "idle" if args.once or max_cycles else "stopped"
    governance_blocked = False
    try:
        while True:
            cycle = scheduler_cycle(layout, relay_limit=args.relay_limit, command_limit=args.command_limit, dry_run=bool(args.dry_run))
            cycle_count += 1
            changed_cycles += int(bool(cycle["changed"]))
            processed_effect_count += len(cycle["effects"]["processed"])
            failed_effect_count += len(cycle["effects"]["failed"])
            processed_command_count += len(cycle["commands"]["processed"])
            failed_command_count += len(cycle["commands"]["failed"])
            last_effects = cycle["effects"]
            all_cycles_ok = all_cycles_ok and cycle["status"] == "ok"
            if args.once:
                break
            if max_cycles and cycle_count >= max_cycles:
                break
            time.sleep(interval)
    except KeyboardInterrupt:
        completed_status = "stopped"
    except GovernancePreflightBlocked:
        governance_blocked = True
        raise
    finally:
        if not args.dry_run and not governance_blocked:
            _update_scheduler_state_authoritative(
                layout,
                status=completed_status,
                pid=None,
                heartbeat_at=now_iso(),
                stopped_at=now_iso(),
            )
    print_json(
        {
            "status": "ok" if all_cycles_ok else "degraded",
            "project": str(layout.project_dir),
            "cycles": cycle_count,
            "changed_cycles": changed_cycles,
            "processed_effects": processed_effect_count,
            "failed_effects": failed_effect_count,
            "last_effects": last_effects,
            "processed_commands": processed_command_count,
            "failed_commands": failed_command_count,
            "scheduler_state": load_scheduler_state(layout),
        }
    )


def command_run_scheduler(args: Any) -> None:
    layout = resolve_project(args.root, args.project)
    _validate_governance_preflight(layout, operation="run-scheduler")
    try:
        with scheduler_daemon_lock(layout):
            _command_run_scheduler_loop(args)
    except ProjectLockTimeout as exc:
        raise SystemExit(f"another scheduler instance is already active: {exc}") from exc


def _escalation_request_payload(args: Any) -> dict[str, Any]:
    def optional_text(name: str, *, lower: bool = False) -> str | None:
        raw = getattr(args, name, None)
        if raw is None:
            return None
        value = str(raw)
        return value.lower() if lower else value

    return {
        "reason": str(getattr(args, "reason", "")),
        "requested_provider": optional_text("provider", lower=True),
        "requested_tier": optional_text("to_tier", lower=True),
        "profile": optional_text("profile"),
        "model": optional_text("model"),
        "start": bool(getattr(args, "start", False)),
        "replan": bool(getattr(args, "replan", False)),
        "unsafe_native": bool(getattr(args, "unsafe_native", False)),
        "force": bool(getattr(args, "force", False)),
        "expected_attempt": optional_text("attempt"),
        "expected_actor": optional_text("from_actor"),
        "actor_id": optional_text("actor_id"),
    }


def _escalation_request_fingerprint(payload: dict[str, Any]) -> str:
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def _stable_profile_binding(binding: Any) -> Any:
    if not isinstance(binding, dict):
        return binding
    return {
        key: value
        for key, value in binding.items()
        if key != "snapshot_relpath"
    }


def _prepared_escalation_admission(preview: dict[str, Any]) -> dict[str, Any]:
    actor = preview.get("actor") if isinstance(preview.get("actor"), dict) else {}
    decision = preview.get("route_decision") if isinstance(preview.get("route_decision"), dict) else {}
    envelope = (
        preview.get("route_budget_envelope")
        if isinstance(preview.get("route_budget_envelope"), dict)
        else None
    )
    steps = (
        json.loads(json.dumps(envelope.get("planned_steps") or [], ensure_ascii=False, allow_nan=False))
        if envelope is not None
        else []
    )
    for step in steps:
        if isinstance(step, dict) and "profile_binding" in step:
            step["profile_binding"] = _stable_profile_binding(step.get("profile_binding"))
    admission = {
        "provider": actor.get("provider"),
        "tier": actor.get("tier"),
        "profile": actor.get("profile"),
        "model": actor.get("model"),
        "agent_name": actor.get("agent_name"),
        "env_key": actor.get("env_key"),
        "command_template": actor.get("command_template"),
        "runner": actor.get("runner"),
        "runtime_backend": (
            (actor.get("runtime") or {}).get("backend")
            if isinstance(actor.get("runtime"), dict)
            else None
        ),
        "isolation": actor.get("isolation"),
        "profile_binding": _stable_profile_binding(actor.get("profile_binding")),
        "route_decision": decision,
        "admitted_steps": steps,
        "budget_projection": preview.get("budget_projection"),
    }
    # Assert JSON-canonicality now; this payload is a durable replay fence.
    json.dumps(admission, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return admission


def _prepared_escalation_fingerprint(admission: dict[str, Any]) -> str:
    encoded = json.dumps(
        admission,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def _validate_escalation_replay_payload(
    origin: dict[str, Any],
    successor: dict[str, Any] | None,
    args: Any,
    command_id: str,
) -> None:
    requested = _escalation_request_payload(args)
    requested_fingerprint = _escalation_request_fingerprint(requested)
    recorded = origin.get("escalation_request")
    recorded_fingerprint = origin.get("escalation_request_fingerprint")
    if recorded is not None or recorded_fingerprint is not None:
        if not isinstance(recorded, dict) or not isinstance(recorded_fingerprint, str):
            raise SystemExit(f"Escalation command {command_id} has a corrupt request binding")
        if _escalation_request_fingerprint(recorded) != recorded_fingerprint:
            raise SystemExit(f"Escalation command {command_id} has a corrupt request fingerprint")
        if recorded_fingerprint != requested_fingerprint or recorded != requested:
            raise SystemExit(
                f"Escalation command {command_id} was replayed with a different request payload"
            )
        return

    # Compatibility for attempts created before request fingerprints existed.
    if origin.get("escalation_reason") != requested["reason"]:
        raise SystemExit(f"Escalation command {command_id} was replayed with a different reason")
    if requested["requested_provider"] is not None and (
        origin.get("escalation_target_provider") != requested["requested_provider"]
    ):
        raise SystemExit(
            f"Escalation command {command_id} was replayed with a different target provider"
        )
    if requested["requested_tier"] is not None and (
        origin.get("escalation_target_tier") != requested["requested_tier"]
    ):
        raise SystemExit(f"Escalation command {command_id} was replayed with a different target tier")
    if successor is not None:
        for field in ("profile", "model"):
            if requested[field] is not None and successor.get(field) != requested[field]:
                raise SystemExit(
                    f"Escalation command {command_id} was replayed with a different {field}"
                )


def command_escalate(args: Any) -> None:
    layout = resolve_project(args.root, args.project)
    require_task(layout, args.task)
    task = load_task(layout, args.task)
    command_id = getattr(args, "command_id", None)
    incomplete_origin: dict[str, Any] | None = None
    if command_id:
        attempts_for_command = task.get("attempts") or []
        origins = [
            row
            for row in attempts_for_command
            if row.get("escalation_command_id") == command_id
        ]
        successors = [
            row
            for row in attempts_for_command
            if row.get("dispatch_command_id") == command_id
        ]
        if len(origins) > 1 or len(successors) > 1:
            raise SystemExit(
                f"Escalation command {command_id} has duplicate origin or successor attempts"
            )
        if successors:
            if not origins:
                raise SystemExit(
                    f"Escalation command {command_id} has a successor without an origin attempt"
                )
            _validate_escalation_replay_payload(
                origins[0],
                successors[0],
                args,
                command_id,
            )
            print_json(
                {
                    "status": "ok",
                    "idempotent_replay": True,
                    "task_id": args.task,
                    "attempt_id": successors[0].get("attempt_id"),
                }
            )
            return
        if origins:
            incomplete_origin = origins[0]
            _validate_escalation_replay_payload(
                incomplete_origin,
                None,
                args,
                command_id,
            )
            if incomplete_origin is not attempts_for_command[-1]:
                raise SystemExit(
                    f"Escalation command {command_id} is incomplete but its origin is no longer current"
                )
            if incomplete_origin.get("status") != "escalated":
                raise SystemExit(
                    f"Escalation command {command_id} has an invalid incomplete origin state"
                )
    attempts = task.get("attempts") or []
    if not attempts:
        raise SystemExit(f"Task has no provider attempt to escalate: {args.task}")
    previous = incomplete_origin or attempts[-1]
    expected_attempt = getattr(args, "attempt", None)
    expected_actor = getattr(args, "from_actor", None)
    if expected_attempt and previous.get("attempt_id") != expected_attempt:
        raise SystemExit(f"Stale escalation attempt rejected: {expected_attempt}")
    if expected_actor and previous.get("actor_id") != expected_actor:
        raise SystemExit(f"Stale escalation actor rejected: {expected_actor}")
    if (
        not isinstance(previous.get("leader_result_id"), str)
        or previous.get("accepted_by_leader") is not False
        or previous.get("recorded_result_status") not in {"failed", "escalate"}
    ):
        raise SystemExit(
            "Escalation requires an explicit leader-rejected record-result for the current attempt"
        )
    if (
        (previous.get("isolation") or {}).get("mode") == "required"
        and _required_attempt_output_boundary(task, previous) == "sealed-required"
    ):
        capsule = previous.get("handoff_capsule")
        stored_result = previous.get("handoff_result_evidence")
        trusted_matches = [
            row
            for row in trusted_result_rows(layout)
            if row.get("id") == previous.get("leader_result_id")
            and row.get("attempt_id") == previous.get("attempt_id")
        ]
        if (
            not isinstance(capsule, dict)
            or not isinstance(stored_result, dict)
            or len(trusted_matches) != 1
            or stored_result != trusted_matches[0]
        ):
            raise SystemExit(
                "Sealed required escalation needs an intact leader-bound handoff; "
                "record the rejection with --handoff"
            )
        try:
            validate_handoff_capsule(
                capsule,
                trusted_leader_result=trusted_matches[0],
            )
        except HandoffContractError as exc:
            raise SystemExit(
                f"Sealed required escalation handoff is invalid: {exc}"
            ) from exc
    project = load_project(layout)
    try:
        catalog = project_provider_catalog(project)
        current_provider = str(attempts[-1].get("provider") or task.get("provider") or "")
        current_spec = provider_by_id(catalog, current_provider)
        explicit_provider = getattr(args, "provider", None)
        explicit_tier = getattr(args, "to_tier", None)
        if explicit_provider:
            target = provider_by_id(catalog, str(explicit_provider))
        elif explicit_tier:
            decision = decide_route(
                task,
                catalog,
                requested_tier=str(explicit_tier),
                history=trusted_result_rows(layout),
                execution_identities=_routing_execution_identities(project),
                input_tokens=int(task.get("estimated_input_tokens") or 0),
                cached_input_tokens=int(task.get("estimated_cached_input_tokens") or 0),
                output_tokens=int(task.get("estimated_output_tokens") or 0),
            )
            target = provider_by_id(catalog, decision.provider_id)
        else:
            preferred_chain: list[str] = []
            for prior_attempt in attempts:
                route = prior_attempt.get("route_decision") or {}
                chain = route.get("planned_provider_ids") if isinstance(route, dict) else None
                if (
                    isinstance(chain, list)
                    and current_provider in chain
                    and chain.index(current_provider) < len(chain) - 1
                ):
                    preferred_chain = [str(provider_id) for provider_id in chain]
                    break
            target = next_stronger_provider(
                catalog,
                current_provider,
                required_capabilities=task.get("required_capabilities") or (),
                preferred_provider_ids=preferred_chain,
                history=trusted_result_rows(layout),
                execution_identities=_routing_execution_identities(project),
                task_type=str(task.get("task_type") or "analysis"),
                difficulty=str(task.get("difficulty") or "normal"),
                input_tokens=int(task.get("estimated_input_tokens") or 0),
                cached_input_tokens=int(task.get("estimated_cached_input_tokens") or 0),
                output_tokens=int(task.get("estimated_output_tokens") or 0),
            )
        if target is None:
            raise SystemExit(f"Task is already at the strongest enabled provider tier: {args.task}")
        if TIER_RANK[str(target["tier"])] <= TIER_RANK[str(current_spec["tier"])] and not getattr(args, "force", False):
            raise SystemExit(
                f"Escalation target {target['provider_id']} ({target['tier']}) is not stronger than {current_provider} ({current_spec['tier']})"
            )
    except (ProfileBindingError, RoutingValidationError) as exc:
        raise SystemExit(f"Unable to escalate task: {exc}") from exc
    if incomplete_origin is not None:
        if incomplete_origin.get("escalation_reason") != args.reason:
            raise SystemExit(
                f"Escalation command {command_id} was replayed with a different reason"
            )
        recorded_target = incomplete_origin.get("escalation_target_provider")
        if recorded_target and recorded_target != target["provider_id"]:
            raise SystemExit(
                f"Escalation command {command_id} was replayed with a different target provider"
            )
    actor_id = getattr(args, "actor_id", None)
    def escalation_dispatch_args(
        *,
        dry_run: bool,
        dispatch_command_id: str | None,
        expected_prepared_admission_fingerprint: str | None = None,
    ) -> SimpleNamespace:
        return SimpleNamespace(
            root=args.root,
            project=args.project,
            task=args.task,
            actor_id=actor_id,
            agent=target["provider_id"],
            provider=target["provider_id"],
            tier=target["tier"],
            profile=getattr(args, "profile", None),
            model=getattr(args, "model", None),
            command=None,
            start=bool(getattr(args, "start", False)),
            dry_run=dry_run,
            force=True,
            escalation_reason=args.reason,
            allow_plan_revision=expected_attempt is None and expected_actor is None,
            replan_active=bool(getattr(args, "replan", False)),
            unsafe_native=bool(getattr(args, "unsafe_native", False))
            or (previous.get("isolation") or {}).get("mode") == "unsafe-native",
            command_id=dispatch_command_id,
            expected_prepared_admission_fingerprint=expected_prepared_admission_fingerprint,
        )

    if bool(getattr(args, "dry_run", False)):
        command_dispatch(escalation_dispatch_args(dry_run=True, dispatch_command_id=None))
        return
    # Validate routing, governance, budget, claims, and launch planning before
    # ending the current attempt. A real spawn failure remains a recoverable
    # new attempt rather than leaving the task with no active successor.
    preview_stream = io.StringIO()
    with contextlib.redirect_stdout(preview_stream):
        command_dispatch(escalation_dispatch_args(dry_run=True, dispatch_command_id=None))
    try:
        prepared_admission = _prepared_escalation_admission(
            json.loads(preview_stream.getvalue())
        )
    except (json.JSONDecodeError, TypeError, ValueError) as exc:
        raise SystemExit("Escalation dry-run did not produce a canonical prepared admission") from exc
    prepared_fingerprint = _prepared_escalation_fingerprint(prepared_admission)
    if incomplete_origin is not None:
        recorded_admission = incomplete_origin.get("escalation_prepared_admission")
        recorded_fingerprint = incomplete_origin.get("escalation_prepared_admission_fingerprint")
        if not isinstance(recorded_admission, dict) or not isinstance(recorded_fingerprint, str):
            raise SystemExit(
                f"Escalation command {command_id} lacks its prepared successor admission"
            )
        if _prepared_escalation_fingerprint(recorded_admission) != recorded_fingerprint:
            raise SystemExit(
                f"Escalation command {command_id} has a corrupt prepared successor admission"
            )
        if recorded_fingerprint != prepared_fingerprint or recorded_admission != prepared_admission:
            raise SystemExit(
                f"Escalation command {command_id} successor admission drifted after the origin commit"
            )
    if incomplete_origin is None:
        previous["status"] = "escalated"
        previous["finished_at"] = now_iso()
        previous["escalation_reason"] = args.reason
        previous["escalation_command_id"] = command_id
        previous["escalation_target_provider"] = target["provider_id"]
        previous["escalation_target_tier"] = target["tier"]
        previous["escalation_request"] = _escalation_request_payload(args)
        previous["escalation_request_fingerprint"] = _escalation_request_fingerprint(
            previous["escalation_request"]
        )
        previous["escalation_prepared_admission"] = prepared_admission
        previous["escalation_prepared_admission_fingerprint"] = prepared_fingerprint
        save_task(layout, task)
        append_event(
            layout,
            "task_escalation_requested",
            task_id=args.task,
            actor_id=actor_id,
            from_provider=current_provider,
            to_provider=target["provider_id"],
            to_tier=target["tier"],
            reason=args.reason,
        )
        _scheduler_fault("escalation.after_origin_before_successor")
    command_dispatch(
        escalation_dispatch_args(
            dry_run=False,
            dispatch_command_id=command_id,
            expected_prepared_admission_fingerprint=prepared_fingerprint,
        )
    )


def command_heartbeat(args: Any) -> None:
    layout = resolve_project(args.root, args.project)
    if args.status not in ACTOR_STATES:
        raise SystemExit(f"Invalid actor status: {args.status}")
    require_actor(layout, args.actor)
    actor = load_actor(layout, args.actor)
    actor["status"] = args.status
    actor["heartbeat_at"] = now_iso()
    if args.note:
        actor["heartbeat_note"] = args.note
    save_actor(layout, actor)
    sync_actor_summary(layout, actor)
    if args.status == "running" and actor.get("task_id") and task_exists(layout, actor["task_id"]):
        task = load_task(layout, actor["task_id"])
        if task.get("status") == "dispatched":
            set_task_state(layout, task, "running")
    refresh_actor_prompt(layout, actor)
    append_event(layout, "actor_heartbeat", actor_id=args.actor, status=args.status)
    print_json({"status": "ok", "actor": args.actor, "actor_status": args.status})


def command_stop_actor(args: Any) -> None:
    layout = resolve_project(args.root, args.project)
    require_actor(layout, args.actor)
    actor = load_actor(layout, args.actor)
    session = load_session(layout)
    runtime_payload = None
    stop_runtime = getattr(args, "stop_runtime", False) or getattr(args, "kill_window", False)
    if stop_runtime:
        runtime = actor_runtime(actor)
        target = runtime.get("target")
        if not target:
            raise SystemExit(f"Actor has no runtime target: {args.actor}")
        backend = backend_from_session(session)
        pid = runtime.get("pid")
        if args.dry_run:
            runtime_payload = {
                "backend": backend.kind,
                "planned_commands": [command_to_string(argv) for argv in backend.stop_plan(target=target, pid=pid)],
            }
        elif current_transaction() is not None and control_store_enabled(layout):
            transaction = current_transaction()
            assert transaction is not None
            effect_id = f"EFF-STOP-{transaction.command_id}"
            transaction.queue_effect(
                effect_id=effect_id,
                effect_type=STOP_EFFECT_TYPE,
                aggregate_id=str(actor["id"]),
                generation=1,
                payload=_stop_effect_payload(actor, reason=args.reason),
            )
            actor["stop_requested_at"] = now_iso()
            actor["stop_effect_id"] = effect_id
            save_actor(layout, actor)
            append_event(
                layout,
                "actor_stop_requested",
                actor_id=actor["id"],
                reason=args.reason,
                effect_id=effect_id,
            )
            runtime_payload = {"status": "queued", "effect_id": effect_id, "backend": backend.kind}
        else:
            runtime_payload = {
                "backend": backend.kind,
                **backend.stop_actor(
                    target=target,
                    pid=pid,
                    process_start_marker=runtime.get("process_start_marker"),
                ),
            }
    if args.dry_run:
        print_json({"status": "ok", "dry_run": True, "actor": args.actor, "runtime": runtime_payload})
        return
    if stop_runtime and current_transaction() is not None and runtime_payload and runtime_payload.get("status") == "queued":
        print_json({"status": "ok", "actor": args.actor, "actor_status": actor.get("status"), "runtime": runtime_payload})
        return
    actor["status"] = "stopped"
    actor["stopped_at"] = now_iso()
    if args.reason:
        actor["stop_reason"] = args.reason
    save_actor(layout, actor)
    sync_actor_summary(layout, actor)
    append_event(layout, "actor_stopped", actor_id=args.actor, stop_runtime=bool(stop_runtime), reason=args.reason)
    if actor.get("role") == "agent":
        send_message(
            layout,
            sender=SCHEDULER_ID,
            recipient=LEADER_ID,
            subject=f"Actor stopped: {args.actor}",
            body=f"{args.actor} is stopped. Reason: {args.reason or 'not specified'}.",
            task_id=actor.get("task_id"),
        )
    print_json({"status": "ok", "actor": args.actor, "actor_status": "stopped", "runtime": runtime_payload})


def _prefixed_sha256(value: Any, label: str) -> str:
    if not isinstance(value, str):
        raise HandoffContractError(f"{label} is missing")
    if re.fullmatch(r"sha256:[0-9a-f]{64}", value):
        return value
    if re.fullmatch(r"[0-9a-f]{64}", value):
        return "sha256:" + value
    raise HandoffContractError(f"{label} is not a SHA-256 digest")


def _required_attempt_output_boundary(
    task: dict[str, Any],
    attempt: dict[str, Any],
) -> str:
    """Classify required workers without treating partial semantics as legacy."""

    if (attempt.get("isolation") or {}).get("mode") != "required":
        raise HandoffContractError("attempt is not a required-isolation worker")
    envelope = task.get("route_budget_envelope")
    planned_steps = envelope.get("planned_steps") if isinstance(envelope, dict) else None
    multi_step_envelope = isinstance(planned_steps, list) and len(planned_steps) > 1
    semantic_trace = task.get("handoff_contract") is not None or any(
        attempt.get(field) is not None
        for field in (
            "attempt_input",
            "semantic_prompt_binding",
            "semantic_prompt",
            "execution_receipt",
            "attempt_output",
            "attempt_output_sha256",
        )
    )
    return (
        "sealed-required"
        if multi_step_envelope or semantic_trace
        else "unsealed-legacy-required"
    )


def _attempt_has_admitted_successor(
    task: dict[str, Any],
    attempt: dict[str, Any],
) -> bool:
    envelope_id = attempt.get("route_envelope_id")
    fingerprint = attempt.get("route_plan_fingerprint")
    step_index = attempt.get("route_plan_step_index")
    if type(step_index) is not int or step_index < 0:
        return False
    semantic_contract = task.get("handoff_contract")
    if isinstance(semantic_contract, dict):
        try:
            sealed_contract = validate_semantic_collaboration_contract(
                semantic_contract
            )
        except HandoffContractError:
            # The caller will reject the corrupt semantic output separately.
            # Requiring a handoff here is the conservative classification.
            return True
        route_policy = sealed_contract["route_policy"]
        if (
            route_policy.get("route_envelope_id") != envelope_id
            or route_policy.get("plan_fingerprint") != fingerprint
        ):
            return True
        sealed_steps = route_policy.get("planned_steps") or []
        return step_index + 1 < len(sealed_steps)
    candidates = [task.get("route_budget_envelope")]
    candidates.extend(task.get("route_budget_envelope_history") or [])
    for envelope in candidates:
        if not isinstance(envelope, dict):
            continue
        if (
            envelope.get("envelope_id") == envelope_id
            and envelope.get("plan_fingerprint") == fingerprint
        ):
            steps = envelope.get("planned_steps")
            return isinstance(steps, list) and step_index + 1 < len(steps)
    return False


def _validate_required_attempt_output(
    layout: ProjectLayout,
    project: dict[str, Any],
    task: dict[str, Any],
    attempt: dict[str, Any],
) -> dict[str, Any]:
    semantic_contract = task.get("handoff_contract")
    attempt_input = attempt.get("attempt_input")
    prompt_binding = attempt.get("semantic_prompt_binding")
    prompt_receipt = attempt.get("semantic_prompt")
    stored = attempt.get("attempt_output")
    execution_receipt = attempt.get("execution_receipt")
    if not all(
        isinstance(value, dict)
        for value in (
            semantic_contract,
            attempt_input,
            prompt_binding,
            prompt_receipt,
            stored,
            execution_receipt,
        )
    ):
        raise HandoffContractError("required attempt semantic input/output receipts are incomplete")
    semantic_contract = validate_semantic_collaboration_contract(semantic_contract)
    attempt_input = validate_attempt_input(
        attempt_input,
        collaboration_contract=semantic_contract,
    )
    prompt_binding = validate_semantic_prompt_binding(prompt_binding)
    project_id = project.get("project_id")
    if not isinstance(project_id, str) or not project_id:
        raise HandoffContractError("required attempt belongs to an invalid project identity")
    expected_prompt_receipt_keys = {
        "schema",
        "path",
        "sha256",
        "size_bytes",
        "binding_sha256",
        "attempt_input_sha256",
        "collaboration_contract_sha256",
    }
    if set(prompt_receipt) != expected_prompt_receipt_keys:
        raise HandoffContractError("required semantic prompt receipt fields are invalid")
    prompt_payload = _read_content_addressed_receipt(
        trusted_root=layout.root,
        root=(
            layout.root
            / "semantic-prompts"
            / slugify(project_id, "project")
            / slugify(str(attempt.get("attempt_id") or "attempt"), "attempt")
        ),
        raw_path=prompt_receipt.get("path"),
        digest=prompt_receipt.get("sha256"),
        expected_size=prompt_receipt.get("size_bytes"),
        max_bytes=SEMANTIC_PROMPT_MAX_BYTES,
        label="semantic prompt",
    )
    prompt_binding = validate_semantic_prompt_binding(
        prompt_binding,
        prompt_bytes=prompt_payload,
    )
    if (
        prompt_receipt.get("schema") != SEMANTIC_PROMPT_RECEIPT_SCHEMA
        or prompt_receipt.get("sha256") != prompt_binding.get("prompt_sha256")
        or prompt_receipt.get("size_bytes") != prompt_binding.get("prompt_size_bytes")
        or prompt_receipt.get("binding_sha256") != prompt_binding.get("binding_sha256")
        or prompt_receipt.get("attempt_input_sha256")
        != attempt_input.get("attempt_input_sha256")
        or prompt_receipt.get("collaboration_contract_sha256")
        != semantic_contract.get("contract_sha256")
    ):
        raise HandoffContractError(
            "required semantic prompt receipt does not match its immutable bindings"
        )
    validated = validate_attempt_output(stored)
    execution_body = dict(execution_receipt)
    execution_path_raw = execution_body.pop("path", None)
    execution_sha256 = execution_body.pop("receipt_sha256", None)
    canonical_execution = json.dumps(
        execution_body,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    observed_execution_sha256 = "sha256:" + hashlib.sha256(canonical_execution).hexdigest()
    execution_payload = _read_content_addressed_receipt(
        trusted_root=layout.root,
        root=(
            layout.root
            / "execution-receipts"
            / slugify(project_id, "project")
            / slugify(str(attempt.get("attempt_id") or "attempt"), "attempt")
        ),
        raw_path=execution_path_raw,
        digest=execution_sha256,
        expected_size=len(canonical_execution),
        max_bytes=RECEIPT_MAX_BYTES,
        label="execution",
    )
    expected_report_sha256 = _prefixed_sha256(
        attempt.get("report_sha256"),
        "attempt report receipt",
    )
    execution_identity = _attempt_execution_identity(attempt)
    outgoing = validated.get("outgoing_changes") or {}
    change_receipt = attempt.get("change_artifact")
    expected_outgoing = (
        {
            "manifest_sha256": change_receipt.get("manifest_sha256"),
            "change_count": change_receipt.get("change_count"),
            "total_upsert_bytes": change_receipt.get("total_upsert_bytes"),
        }
        if isinstance(change_receipt, dict)
        else attempt_input.get("incoming_changes")
    )
    projection = attempt.get("context_projection") or {}
    expected_core = {
        "task_id": task.get("id"),
        "attempt_id": attempt.get("attempt_id"),
        "actor_id": attempt.get("actor_id"),
        "provider": attempt.get("provider"),
        "tier": attempt.get("tier"),
        "model": (execution_identity or {}).get("model"),
        "profile": (execution_identity or {}).get("profile"),
        "profile_sha256": (execution_identity or {}).get("profile_sha256"),
        "context_projection_manifest_sha256": projection.get("manifest_sha256"),
        "incoming_change_manifest_sha256": attempt_input.get("incoming_changes", {}).get(
            "manifest_sha256"
        ),
        "semantic_prompt_sha256": prompt_binding.get("prompt_sha256"),
        "provider_exit_code": attempt.get("provider_exit_code"),
    }
    if (
        execution_sha256 != observed_execution_sha256
        or execution_payload != canonical_execution
        or execution_body.get("schema_version") != 1
        or execution_body.get("kind") != "costmarshal-execution-receipt"
        or any(execution_body.get(field) != value for field, value in expected_core.items())
        or execution_identity is None
        or prompt_binding.get("task_id") != task.get("id")
        or prompt_binding.get("attempt_id") != attempt.get("attempt_id")
        or prompt_binding.get("attempt_input_sha256")
        != attempt_input.get("attempt_input_sha256")
        or validated.get("task_id") != task.get("id")
        or validated.get("attempt_id") != attempt.get("attempt_id")
        or validated.get("route_step_index") != attempt.get("route_plan_step_index")
        or validated.get("collaboration_contract_sha256")
        != semantic_contract.get("contract_sha256")
        or validated.get("attempt_input_sha256")
        != attempt_input.get("attempt_input_sha256")
        or validated.get("prompt_binding_sha256") != prompt_binding.get("binding_sha256")
        or validated.get("execution_receipt_sha256") != execution_sha256
        or validated.get("report_receipt")
        != {
            "sha256": expected_report_sha256,
            "size_bytes": attempt.get("report_size"),
        }
        or outgoing != expected_outgoing
        or attempt.get("attempt_output_sha256") != validated.get("attempt_output_sha256")
    ):
        raise HandoffContractError(
            "required attempt output does not match task/route/profile/prompt/execution/report bindings"
        )
    return validated


def _change_preview_root(
    layout: ProjectLayout,
    project: dict[str, Any],
    task: dict[str, Any],
    attempt: dict[str, Any],
) -> Path:
    return (
        layout.root
        / "change-previews"
        / slugify(str(project.get("project_id") or "project"), "project")
        / slugify(str(task.get("id") or "task"), "task")
        / slugify(str(attempt.get("attempt_id") or "attempt"), "attempt")
    ).resolve()


def _semantic_change_inputs(
    layout: ProjectLayout,
    project: dict[str, Any],
    task: dict[str, Any],
    attempt: dict[str, Any],
) -> tuple[
    dict[str, Any],
    dict[str, Any],
    tuple[str, ...],
    dict[str, Any],
    Path,
    Path,
]:
    if _required_attempt_output_boundary(task, attempt) != "sealed-required":
        raise ChangeApplyError("change review requires a sealed required-isolation attempt")
    contract = validate_semantic_collaboration_contract(task.get("handoff_contract"))
    output = _validate_required_attempt_output(layout, project, task, attempt)
    change_policy = contract.get("change_policy") or {}
    try:
        write_scope = tuple(
            sorted(normalize_path_list(change_policy.get("write_scope") or [], kind="allowed"))
        )
    except SecurityValidationError as exc:
        raise ChangeApplyError(str(exc)) from exc
    if not write_scope:
        raise ChangeApplyError("attempt is read-only and has no workspace changes to review")
    receipt = attempt.get("change_artifact")
    if not isinstance(receipt, dict):
        raise ChangeApplyError("attempt has no cumulative change artifact")
    manifest = receipt.get("manifest")
    outgoing = output.get("outgoing_changes") or {}
    if (
        not isinstance(manifest, dict)
        or receipt.get("manifest_sha256") != outgoing.get("manifest_sha256")
        or receipt.get("change_count") != outgoing.get("change_count")
        or receipt.get("total_upsert_bytes") != outgoing.get("total_upsert_bytes")
        or receipt.get("base_sha") != contract.get("base_sha")
        or receipt.get("write_scope") != list(write_scope)
        or receipt.get("collaboration_contract_sha256") != contract.get("contract_sha256")
        or manifest.get("manifest_sha256") != outgoing.get("manifest_sha256")
    ):
        raise ChangeApplyError(
            "cumulative change artifact differs from the sealed attempt output"
        )
    artifact_root = Path(str(receipt.get("artifact_root") or "")).expanduser().resolve()
    expected_artifact_root = (layout.root / "task-change-artifacts").resolve()
    try:
        artifact_root.relative_to(expected_artifact_root)
    except ValueError as exc:
        raise ChangeApplyError("change artifact escapes the CostMarshal runtime root") from exc
    workspace = Path(str(project.get("workspace") or "")).expanduser().resolve()
    preview_root = _change_preview_root(layout, project, task, attempt)
    return contract, output, write_scope, manifest, artifact_root, preview_root


def _load_bound_change_preview(
    layout: ProjectLayout,
    project: dict[str, Any],
    task: dict[str, Any],
    attempt: dict[str, Any],
    *,
    require_source_ready: bool = False,
) -> tuple[
    PreparedChangePreview,
    dict[str, Any],
    dict[str, Any],
    tuple[str, ...],
    dict[str, Any],
    Path,
    Path,
]:
    contract, output, write_scope, manifest, artifact_root, preview_root = (
        _semantic_change_inputs(layout, project, task, attempt)
    )
    stored = attempt.get("change_preview_receipt")
    if not isinstance(stored, dict):
        raise ChangeApplyError("leader acceptance requires a durable change preview")
    preview = prepared_change_preview_from_dict(
        stored,
        expected_repository=project.get("workspace"),
        expected_base_sha=str(contract["base_sha"]),
        expected_write_scope=write_scope,
        expected_change_manifest_sha256=str(output["outgoing_changes"]["manifest_sha256"]),
        expected_preview_root=preview_root,
    )
    if preview.changed_paths != tuple(str(entry["path"]) for entry in manifest["changes"]):
        raise ChangeApplyError("stored preview path set differs from the cumulative manifest")
    if require_source_ready:
        require_prepared_change_source_ready(preview)
    return (
        preview,
        contract,
        output,
        write_scope,
        manifest,
        artifact_root,
        preview_root,
    )


def command_collect(args: Any) -> None:
    layout = resolve_project(args.root, args.project)
    if args.state and args.state not in {"waiting_leader", "failed", "escalate"}:
        raise SystemExit("collect state must be waiting_leader, failed, or escalate; done requires leader record-result acceptance")
    require_task(layout, args.task)
    task = load_task(layout, args.task)
    project = load_project(layout)
    command_id = getattr(args, "command_id", None)
    if command_id and task.get("last_collect_command_id") == command_id:
        print_json({"status": "ok", "idempotent_replay": True, "task_id": args.task})
        return
    actor_id = args.actor or task.get("agent_id")
    attempt_id = getattr(args, "attempt", None)
    if not attempt_id and actor_id and actor_exists(layout, actor_id):
        attempt_id = load_actor(layout, actor_id).get("attempt_id")
    attempts = task.get("attempts") or []
    latest_attempt = attempts[-1] if attempts else None
    if attempt_id and latest_attempt and attempt_id != latest_attempt.get("attempt_id"):
        raise SystemExit(
            f"Stale collect attempt rejected for task {args.task}: {attempt_id}; "
            f"latest is {latest_attempt.get('attempt_id')}"
        )
    if actor_id and latest_attempt and latest_attempt.get("actor_id") not in {None, actor_id}:
        raise SystemExit(
            f"Stale collect actor rejected for task {args.task}: {actor_id}; "
            f"latest is {latest_attempt.get('actor_id')}"
        )
    report_path = Path(args.report).expanduser().resolve() if args.report else task_dir(layout, args.task) / "completion-report.md"
    if args.state in {"done", "failed", "escalate", "waiting_leader"} and not report_path.is_file():
        raise SystemExit(f"Report file not found: {report_path}")
    try:
        resolved_report = report_path.resolve(strict=True)
        resolved_report.relative_to(task_dir(layout, args.task).resolve())
        if report_path.is_symlink() or not resolved_report.is_file():
            raise ValueError("report is not a regular task file")
        report_bytes = resolved_report.read_bytes()
        if len(report_bytes) > 1024 * 1024:
            raise ValueError("report exceeds 1 MiB")
    except (OSError, ValueError) as exc:
        raise SystemExit(f"Report receipt is invalid: {exc}") from exc
    actor_for_receipt: dict[str, Any] | None = None
    if actor_id and actor_exists(layout, actor_id):
        actor_for_receipt = load_actor(layout, actor_id)
        runtime_state = (actor_for_receipt.get("runtime") or {}).get("provider_execution_state")
        if actor_for_receipt.get("status") in {"running", "starting", "needs_recovery"} or runtime_state in {
            "started",
            "finished_pending_finalize",
            "launch_pending_authorization",
        }:
            raise SystemExit("Cannot collect a report while provider execution is active or uncertain")
    else:
        runtime_state = None
    task["report_path"] = relpath(report_path, layout.project_dir)
    task["collected_at"] = now_iso()
    if args.summary:
        task["summary"] = compact_text(args.summary)
    task["last_collect_command_id"] = command_id
    if attempt_id:
        matched = False
        for attempt in task.get("attempts") or []:
            if attempt.get("attempt_id") == attempt_id:
                attempt["status"] = args.state
                attempt["finished_at"] = attempt.get("finished_at") or now_iso()
                attempt["report_path"] = relpath(resolved_report, layout.project_dir)
                attempt["report_sha256"] = hashlib.sha256(report_bytes).hexdigest()
                attempt["report_size"] = len(report_bytes)
                attempt["provider_execution_state"] = (
                    runtime_state
                    if runtime_state in {"finished", "finished_recovered"}
                    else "manual_report_collected"
                )
                if (attempt.get("isolation") or {}).get("mode") == "required":
                    output_boundary = _required_attempt_output_boundary(task, attempt)
                    if actor_for_receipt is None:
                        raise SystemExit("Required attempt output validation needs its authoritative actor")
                    actor_runtime_receipt = actor_for_receipt.get("runtime") or {}
                    if output_boundary == "sealed-required":
                        receipt_mismatches = [
                            label
                            for label, actor_value, attempt_value in (
                                (
                                    "attempt_id",
                                    actor_for_receipt.get("attempt_id"),
                                    attempt.get("attempt_id"),
                                ),
                                (
                                    "task_id",
                                    actor_for_receipt.get("task_id"),
                                    task.get("id"),
                                ),
                                (
                                    "handoff_contract",
                                    actor_for_receipt.get("handoff_contract"),
                                    task.get("handoff_contract"),
                                ),
                                (
                                    "attempt_input",
                                    actor_for_receipt.get("attempt_input"),
                                    attempt.get("attempt_input"),
                                ),
                                (
                                    "semantic_prompt_binding",
                                    actor_for_receipt.get("semantic_prompt_binding"),
                                    attempt.get("semantic_prompt_binding"),
                                ),
                                (
                                    "semantic_prompt",
                                    actor_runtime_receipt.get("semantic_prompt"),
                                    attempt.get("semantic_prompt"),
                                ),
                                (
                                    "provider_execution_state",
                                    actor_runtime_receipt.get("provider_execution_state"),
                                    attempt.get("provider_execution_state"),
                                ),
                                (
                                    "report_sha256",
                                    actor_runtime_receipt.get("report_sha256"),
                                    attempt.get("report_sha256"),
                                ),
                                (
                                    "attempt_output_sha256",
                                    actor_runtime_receipt.get("attempt_output_sha256"),
                                    attempt.get("attempt_output_sha256"),
                                ),
                                (
                                    "execution_receipt",
                                    actor_runtime_receipt.get("execution_receipt"),
                                    attempt.get("execution_receipt"),
                                ),
                            )
                            if actor_value != attempt_value
                        ]
                        if receipt_mismatches:
                            raise SystemExit(
                                "Required attempt actor/output receipts do not match the "
                                "authoritative attempt: " + ", ".join(receipt_mismatches)
                            )
                        try:
                            _validate_required_attempt_output(
                                layout,
                                project,
                                task,
                                attempt,
                            )
                        except HandoffContractError as exc:
                            raise SystemExit(f"Required attempt output validation failed closed: {exc}") from exc
                    elif (
                        actor_for_receipt.get("handoff_contract") is not None
                        or actor_for_receipt.get("attempt_input") is not None
                        or actor_for_receipt.get("semantic_prompt_binding") is not None
                        or actor_runtime_receipt.get("semantic_prompt") is not None
                        or actor_runtime_receipt.get("attempt_output_sha256") is not None
                        or actor_runtime_receipt.get("execution_receipt") is not None
                    ):
                        raise SystemExit(
                            "Legacy required attempt contains partial semantic receipts"
                        )
                matched = True
                break
        if not matched:
            raise SystemExit(f"Attempt not found for task {args.task}: {attempt_id}")
    set_task_state(layout, task, args.state)
    if actor_id:
        require_actor(layout, actor_id)
        actor = load_actor(layout, actor_id)
        actor["status"] = "idle" if task["status"] in {"done", "failed", "escalate", "cancelled"} else "waiting"
        save_actor(layout, actor)
        sync_actor_summary(layout, actor)
        refresh_actor_prompt(layout, actor)
    body = "\n".join(
        [
            f"Task: {args.task}",
            f"State: {task['status']}",
            f"Report path: {task['report_path']}",
            f"Summary: {task.get('summary') or '(scheduler did not read or summarize the report)'}",
        ]
    )
    message = send_message(layout, sender=SCHEDULER_ID, recipient=LEADER_ID, subject=f"Collect {args.task}", body=body, task_id=args.task)
    append_event(layout, "task_collected", task_id=args.task, actor_id=actor_id, state=task["status"])
    print_json({"status": "ok", "task_id": args.task, "actor_id": actor_id, "message_id": message["id"]})


def command_preview_changes(args: Any) -> None:
    """Create a durable, source-isolated Git preview before leader acceptance."""

    layout = resolve_project(args.root, args.project)
    require_task(layout, args.task)
    command_id = getattr(args, "command_id", None)
    if not isinstance(command_id, str) or not command_id.strip():
        raise SystemExit("preview-changes requires --command-id")
    command_id = command_id.strip()
    project = load_project(layout)
    task = load_task(layout, args.task)
    attempts = task.get("attempts") or []
    requested_attempt = getattr(args, "attempt", None)
    attempt = (
        next(
            (row for row in attempts if row.get("attempt_id") == requested_attempt),
            None,
        )
        if requested_attempt
        else (attempts[-1] if attempts else None)
    )
    if not isinstance(attempt, dict):
        raise SystemExit("preview-changes requires an existing provider attempt")
    if attempts and attempt is not attempts[-1]:
        raise SystemExit("preview-changes refuses a stale non-current attempt")
    if attempt.get("status") not in {"waiting_leader", "failed", "escalate"}:
        raise SystemExit("preview-changes requires a collected finished attempt")
    if attempt.get("leader_result_id") is not None:
        raise SystemExit("preview-changes must occur before the leader result is recorded")
    try:
        (
            contract,
            output,
            write_scope,
            manifest,
            artifact_root,
            preview_root,
        ) = _semantic_change_inputs(layout, project, task, attempt)
        if attempt.get("change_preview_command_id") == command_id:
            (
                replay,
                *_rest,
            ) = _load_bound_change_preview(
                layout,
                project,
                task,
                attempt,
                require_source_ready=True,
            )
            print_json(
                {
                    "status": "ok",
                    "idempotent_replay": True,
                    "preview": replay.to_dict(),
                }
            )
            return
        preview = prepare_change_preview(
            project.get("workspace"),
            base_sha=str(contract["base_sha"]),
            write_scope=write_scope,
            change_manifest=manifest,
            change_artifact_root=artifact_root,
            preview_root=preview_root,
        )
    except (ChangeApplyError, HandoffContractError) as exc:
        raise SystemExit(f"Unable to prepare sealed change preview: {exc}") from exc
    attempt["change_preview_receipt"] = preview.to_dict()
    attempt["change_preview_command_id"] = command_id
    attempt["change_preview_attempt_output_sha256"] = output.get(
        "attempt_output_sha256"
    )
    attempt["change_preview_created_at"] = now_iso()
    save_task(layout, task)
    append_event(
        layout,
        "change_preview_created",
        task_id=task.get("id"),
        attempt_id=attempt.get("attempt_id"),
        patch_sha256=preview.patch_sha256,
        candidate_tree_sha=preview.candidate_tree_sha,
    )
    print_json(
        {
            "status": "ok",
            "source_workspace_modified": False,
            "preview": preview.to_dict(),
        }
    )


def command_apply_changes(args: Any) -> None:
    """Preview or explicitly stage the exact leader-accepted candidate tree."""

    layout = resolve_project(args.root, args.project)
    require_task(layout, args.task)
    project = load_project(layout)
    task = load_task(layout, args.task)
    attempts = task.get("attempts") or []
    requested_attempt = getattr(args, "attempt", None)
    attempt = (
        next(
            (row for row in attempts if row.get("attempt_id") == requested_attempt),
            None,
        )
        if requested_attempt
        else (attempts[-1] if attempts else None)
    )
    if not isinstance(attempt, dict):
        raise SystemExit("apply-changes requires an existing provider attempt")
    if attempts and attempt is not attempts[-1]:
        raise SystemExit("apply-changes refuses a stale non-current attempt")
    if (
        attempt.get("recorded_result_status") != "done"
        or attempt.get("accepted_by_leader") is not True
        or not isinstance(attempt.get("leader_result_id"), str)
    ):
        raise SystemExit("apply-changes requires an explicitly accepted done result")
    try:
        (
            preview,
            contract,
            output,
            write_scope,
            manifest,
            artifact_root,
            preview_root,
        ) = _load_bound_change_preview(layout, project, task, attempt)
    except (ChangeApplyError, HandoffContractError) as exc:
        raise SystemExit(f"Accepted change preview is invalid: {exc}") from exc
    trusted_result = next(
        (
            row
            for row in trusted_result_rows(layout)
            if row.get("id") == attempt.get("leader_result_id")
            and row.get("attempt_id") == attempt.get("attempt_id")
        ),
        None,
    )
    if not isinstance(trusted_result, dict):
        raise SystemExit("apply-changes requires intact trusted leader result evidence")
    try:
        apply_contract = build_apply_preview_contract(
            collaboration_contract=contract,
            accepted_attempt_input=attempt.get("attempt_input"),
            accepted_attempt_output=output,
            accepted_leader_result=trusted_result,
            expected_source_head_sha=preview.expected_head_sha,
            patch_sha256=preview.patch_sha256,
            patch_size_bytes=preview.patch_size_bytes,
            candidate_tree_sha=preview.candidate_tree_sha,
        )
    except HandoffContractError as exc:
        raise SystemExit(f"Unable to bind accepted apply preview: {exc}") from exc
    if not getattr(args, "apply", False):
        print_json(
            {
                "status": "ok",
                "apply": False,
                "instruction": "Repeat with --apply, --preview-sha, and --command-id after review.",
                "contract": apply_contract,
                "preview": preview.to_dict(),
            }
        )
        return
    command_id = getattr(args, "command_id", None)
    if not isinstance(command_id, str) or not command_id.strip():
        raise SystemExit("apply-changes --apply requires --command-id")
    command_id = command_id.strip()
    if getattr(args, "preview_sha", None) != apply_contract.get("preview_sha256"):
        raise SystemExit("apply-changes --preview-sha does not match the current accepted contract")
    existing = attempt.get("change_apply_receipt")
    if isinstance(existing, dict):
        if existing.get("command_id") != command_id:
            raise SystemExit(
                "accepted changes were already applied under a different command-id"
            )
        if existing.get("preview_sha256") != apply_contract.get("preview_sha256"):
            raise SystemExit("apply-changes command-id is bound to a different preview")
    try:
        applied = apply_prepared_change_preview(
            preview,
            expected_repository=project.get("workspace"),
            expected_base_sha=str(contract["base_sha"]),
            expected_write_scope=write_scope,
            expected_change_manifest_sha256=str(
                output["outgoing_changes"]["manifest_sha256"]
            ),
            change_manifest=manifest,
            change_artifact_root=artifact_root,
            scratch_root=(
                layout.root
                / "apply-verify"
                / hashlib.sha256(
                    (
                        str(project.get("project_id") or "project")
                        + ":"
                        + str(attempt.get("attempt_id") or "attempt")
                    ).encode("utf-8")
                ).hexdigest()[:16]
            ),
        )
    except ChangeApplyError as exc:
        raise SystemExit(f"Accepted change apply failed closed: {exc}") from exc
    if isinstance(existing, dict):
        print_json(
            {
                "status": "ok",
                "idempotent_replay": True,
                "apply": existing,
                "revalidated_outcome": applied,
            }
        )
        return
    receipt = {
        "schema_version": 1,
        "kind": "costmarshal-change-apply-receipt",
        "command_id": command_id,
        "applied_at": now_iso(),
        "preview_sha256": apply_contract["preview_sha256"],
        "patch_sha256": preview.patch_sha256,
        "candidate_tree_sha": preview.candidate_tree_sha,
        "outcome": applied,
    }
    attempt["change_apply_contract"] = apply_contract
    attempt["change_apply_receipt"] = receipt
    save_task(layout, task)
    append_event(
        layout,
        "change_apply_recorded",
        task_id=task.get("id"),
        attempt_id=attempt.get("attempt_id"),
        preview_sha256=apply_contract["preview_sha256"],
        apply_status=applied.get("status"),
    )
    print_json({"status": "ok", "apply": receipt})


def _result_request_contract(
    args: Any,
    *,
    command_id: str,
    task_id: str,
    attempt_id: str,
    attempt_output_sha256: str | None,
    attempt_output_boundary: str,
    task_type: str,
    difficulty: str,
) -> tuple[dict[str, Any], str]:
    try:
        estimated_cost = (
            None
            if args.estimated_cost_cny is None
            else _money_text(args.estimated_cost_cny, "estimated-cost-cny")
        )
    except ValueError as exc:
        raise SystemExit(str(exc)) from exc
    contract = {
        "schema_version": "costmarshal-record-result-request-v1",
        "command_id": command_id,
        "task_id": task_id,
        "attempt_id": attempt_id,
        "attempt_output_sha256": attempt_output_sha256,
        "attempt_output_boundary": attempt_output_boundary,
        "task_type": task_type,
        "difficulty": difficulty,
        "status": args.status,
        "accepted_by_leader": bool(args.accepted_by_leader),
        "quality_score": int(args.quality_score),
        "actor_argument": getattr(args, "actor", None),
        "agent_argument": getattr(args, "agent", None),
        "model_argument": getattr(args, "model", None),
        "input_tokens_argument": require_non_negative_int(
            args.input_tokens,
            "input-tokens",
        ),
        "cached_input_tokens_argument": require_non_negative_int(
            getattr(args, "cached_input_tokens", 0),
            "cached-input-tokens",
        ),
        "output_tokens_argument": require_non_negative_int(
            args.output_tokens,
            "output-tokens",
        ),
        "estimated_cost_cny_argument": estimated_cost,
        "summary_argument": compact_text(args.summary) if args.summary else "",
        "handoff_argument": getattr(args, "handoff", None) or "",
        "note_argument": args.note or "",
    }
    payload = json.dumps(
        contract,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return contract, "sha256:" + hashlib.sha256(payload).hexdigest()


def build_rejected_attempt_handoff(
    *,
    task: dict[str, Any],
    attempt: dict[str, Any],
    trusted_result: dict[str, Any],
    handoff_text: str,
) -> dict[str, Any]:
    """Purely seal one audited rejection for a successor; never dispatch it."""

    if (attempt.get("isolation") or {}).get("mode") != "required":
        raise HandoffContractError("handoff requires a sealed required-isolation attempt")
    raw_contract = task.get("handoff_contract")
    if not isinstance(raw_contract, dict):
        raise HandoffContractError("handoff requires a sealed semantic collaboration contract")
    contract = validate_semantic_collaboration_contract(raw_contract)
    attempt_input = validate_attempt_input(
        attempt.get("attempt_input"),
        collaboration_contract=contract,
    )
    attempt_output = validate_attempt_output(attempt.get("attempt_output"))
    execution_identity = _attempt_execution_identity(attempt)
    leader_binding = task.get("leader_result") or {}
    expected_result_fields = {
        "evidence_schema_version": RESULT_EVIDENCE_SCHEMA,
        "task_id": task.get("id"),
        "attempt_id": attempt.get("attempt_id"),
        "actor_id": attempt.get("actor_id"),
        "provider": attempt.get("provider"),
        "tier": attempt.get("tier"),
        "profile": (execution_identity or {}).get("profile"),
        "profile_sha256": (execution_identity or {}).get("profile_sha256"),
        "execution_model": (execution_identity or {}).get("model"),
        "route_envelope_id": attempt.get("route_envelope_id"),
        "route_plan_fingerprint": attempt.get("route_plan_fingerprint"),
        "route_plan_step_index": attempt.get("route_plan_step_index"),
        "route_predecessors": attempt.get("route_predecessors") or [],
        "attempt_output_sha256": attempt_output.get("attempt_output_sha256"),
        "attempt_output_boundary": "sealed-required",
        "report_path": attempt.get("report_path"),
        "report_sha256": attempt_output.get("report_receipt", {}).get("sha256"),
        "report_size": attempt.get("report_size"),
        "request_contract_sha256": attempt.get("result_request_contract_sha256"),
    }
    if (
        execution_identity is None
        or attempt_output.get("task_id") != task.get("id")
        or attempt_output.get("attempt_id") != attempt.get("attempt_id")
        or attempt_output.get("attempt_input_sha256")
        != attempt_input.get("attempt_input_sha256")
        or attempt.get("attempt_output_sha256")
        != attempt_output.get("attempt_output_sha256")
        or attempt.get("result_attempt_output_sha256")
        != attempt_output.get("attempt_output_sha256")
        or attempt.get("result_attempt_output_boundary") != "sealed-required"
        or attempt.get("leader_result_id") != trusted_result.get("id")
        or leader_binding.get("result_id") != trusted_result.get("id")
        or attempt.get("accepted_by_leader") is not False
        or attempt.get("recorded_result_status") not in {"failed", "escalate"}
        or trusted_result.get("accepted_by_leader") is not False
        or trusted_result.get("status") != attempt.get("recorded_result_status")
        or any(
            trusted_result.get(field) != expected
            for field, expected in expected_result_fields.items()
        )
    ):
        raise HandoffContractError(
            "handoff requires the exact trusted rejected result and sealed attempt output"
        )
    request_contract = trusted_result.get("request_contract")
    if (
        not isinstance(request_contract, dict)
        or request_contract.get("attempt_output_sha256")
        != attempt_output.get("attempt_output_sha256")
        or request_contract.get("attempt_output_boundary") != "sealed-required"
        or request_contract.get("handoff_argument") != handoff_text
    ):
        raise HandoffContractError(
            "handoff result request does not bind the sealed attempt output"
        )
    return build_handoff_capsule(
        collaboration_contract=contract,
        attempt_input=attempt_input,
        attempt_output=attempt_output,
        leader_result=trusted_result,
        handoff_text=handoff_text,
    )


def _bind_rejected_attempt_handoff(
    *,
    task: dict[str, Any],
    attempt: dict[str, Any],
    trusted_result: dict[str, Any],
    handoff_text: str,
) -> bool:
    """Idempotently persist the exact capsule/result pair consumed by a successor."""

    capsule = build_rejected_attempt_handoff(
        task=task,
        attempt=attempt,
        trusted_result=trusted_result,
        handoff_text=handoff_text,
    )
    existing_capsule = attempt.get("handoff_capsule")
    existing_result = attempt.get("handoff_result_evidence")
    if existing_capsule is not None and existing_capsule != capsule:
        raise HandoffContractError("sealed handoff capsule changed after leader result")
    if existing_result is not None and existing_result != trusted_result:
        raise HandoffContractError("sealed handoff result evidence changed after leader result")
    changed = existing_capsule is None or existing_result is None
    attempt["handoff_capsule"] = capsule
    attempt["handoff_result_evidence"] = json.loads(
        json.dumps(trusted_result, ensure_ascii=False, allow_nan=False)
    )
    attempt["collaboration_phase"] = "handoff_sealed"
    return changed


def _apply_leader_result_binding(
    task: dict[str, Any],
    attempt: dict[str, Any],
    row: dict[str, Any],
) -> None:
    attempt["status"] = row["status"]
    attempt["finished_at"] = attempt.get("finished_at") or row["timestamp"]
    attempt["leader_result_id"] = row["id"]
    attempt["accepted_by_leader"] = row["accepted_by_leader"]
    attempt["quality_score"] = row["quality_score"]
    attempt["recorded_result_status"] = row["status"]
    attempt["result_request_contract_sha256"] = row["request_contract_sha256"]
    attempt["result_attempt_output_sha256"] = row.get("attempt_output_sha256")
    attempt["result_attempt_output_boundary"] = row.get("attempt_output_boundary")
    attempt["result_estimated_cost_cny"] = row.get("estimated_cost_cny")
    if not attempt.get("cost_settled"):
        attempt["cost_settlement_blocked_reason"] = "actual cost is not verified"
    attempt["estimated_cost_cny"] = row.get("estimated_cost_cny")
    attempt["cost_source"] = row.get("cost_source")
    task["leader_result"] = {
        "result_id": row["id"],
        "recorded_at": row["timestamp"],
        "status": row["status"],
        "accepted_by_leader": row["accepted_by_leader"],
        "quality_score": row["quality_score"],
        "summary": row["summary"],
        "agent": row["agent"],
        "model": row["model"],
    }
    if row["summary"]:
        task["summary"] = row["summary"]


def command_record_result(args: Any) -> None:
    layout = resolve_project(args.root, args.project)
    require_task(layout, args.task)
    command_id = getattr(args, "command_id", None)
    if not isinstance(command_id, str) or not command_id.strip():
        raise SystemExit("record-result requires --command-id")
    command_id = command_id.strip()
    if args.status not in RESULT_TASK_STATES:
        raise SystemExit(f"record-result status must be one of: {', '.join(sorted(RESULT_TASK_STATES))}")
    if args.accepted_by_leader and args.status != "done":
        raise SystemExit("--accepted-by-leader requires --status done")
    if args.status == "done" and not args.accepted_by_leader:
        raise SystemExit("--status done requires --accepted-by-leader")
    if int(args.quality_score) not in {1, 2, 3, 4, 5}:
        raise SystemExit("quality-score must be 1-5")
    task = load_task(layout, args.task)
    project = load_project(layout)
    attempts = task.get("attempts") or []
    requested_attempt = getattr(args, "attempt", None)
    attempt = next((row for row in attempts if row.get("attempt_id") == requested_attempt), None) if requested_attempt else (attempts[-1] if attempts else None)
    if requested_attempt and attempt is None:
        raise SystemExit(f"Attempt not found for task {args.task}: {requested_attempt}")
    if attempt is not None and attempts and attempt is not attempts[-1]:
        raise SystemExit(
            f"Stale leader result attempt rejected for task {args.task}: "
            f"{attempt.get('attempt_id')}; latest is {attempts[-1].get('attempt_id')}"
        )
    if attempt is None:
        raise SystemExit(f"Leader result requires a bound provider attempt for task {args.task}")
    attempt_id = str(attempt.get("attempt_id") or "")
    execution_identity = _attempt_execution_identity(attempt)
    if execution_identity is None:
        raise SystemExit("Leader result requires an immutable profile-bound execution identity")
    if args.model is not None and args.model != execution_identity["model"]:
        raise SystemExit(
            "record-result --model cannot override the attempt execution identity"
        )
    isolation_mode = (attempt.get("isolation") or {}).get("mode")
    if isolation_mode == "required":
        attempt_output_boundary = _required_attempt_output_boundary(task, attempt)
        if attempt_output_boundary == "sealed-required":
            try:
                attempt_output = _validate_required_attempt_output(
                    layout,
                    project,
                    task,
                    attempt,
                )
            except HandoffContractError as exc:
                raise SystemExit(
                    f"Leader result requires an intact sealed required-worker output: {exc}"
                ) from exc
            attempt_output_sha256: str | None = str(
                attempt_output["attempt_output_sha256"]
            )
            result_report_sha256 = str(attempt_output["report_receipt"]["sha256"])
        else:
            attempt_output_sha256 = None
            result_report_sha256 = attempt.get("report_sha256")
    else:
        if attempt.get("attempt_output") is not None or attempt.get("attempt_output_sha256") is not None:
            raise SystemExit(
                "Non-required worker contains an unexpected semantic attempt output; validate state"
            )
        attempt_output_sha256 = None
        attempt_output_boundary = (
            "unsealed-unsafe-native"
            if isolation_mode == "unsafe-native"
            else "unsealed-legacy-non-required"
        )
        result_report_sha256 = attempt.get("report_sha256")
    handoff_text = getattr(args, "handoff", None) or ""
    admitted_successor = _attempt_has_admitted_successor(task, attempt)
    if (
        attempt_output_boundary == "sealed-required"
        and args.status == "failed"
        and admitted_successor
    ):
        raise SystemExit(
            "A sealed required attempt with an admitted successor must use "
            "--status escalate; failed is terminal"
        )
    if (
        attempt_output_boundary == "sealed-required"
        and args.status == "escalate"
        and admitted_successor
    ):
        if not handoff_text:
            raise SystemExit(
                "A rejected sealed required attempt requires --handoff"
            )
    if handoff_text and (
        attempt_output_boundary != "sealed-required" or args.status == "done"
    ):
        raise SystemExit(
            "--handoff is only valid for a rejected sealed required attempt"
        )
    if (
        attempt_output_boundary == "sealed-required"
        and args.status == "done"
        and int((attempt_output.get("outgoing_changes") or {}).get("change_count") or 0)
        > 0
    ):
        try:
            _load_bound_change_preview(
                layout,
                project,
                task,
                attempt,
                require_source_ready=True,
            )
        except (ChangeApplyError, HandoffContractError) as exc:
            raise SystemExit(
                "Leader acceptance of workspace changes requires preview-changes first: "
                f"{exc}"
            ) from exc
    request_contract, request_contract_sha256 = _result_request_contract(
        args,
        command_id=command_id,
        task_id=args.task,
        attempt_id=attempt_id,
        attempt_output_sha256=attempt_output_sha256,
        attempt_output_boundary=attempt_output_boundary,
        task_type=str(task.get("task_type") or "unknown"),
        difficulty=str(task.get("difficulty") or "normal"),
    )
    existing_results = result_rows(layout)
    command_result = (
        next((row for row in existing_results if row.get("command_id") == command_id), None)
        if command_id
        else None
    )
    recorded_result_id = attempt.get("leader_result_id")
    if recorded_result_id is not None:
        recorded = next(
            (row for row in existing_results if row.get("id") == recorded_result_id),
            None,
        )
        if recorded is None:
            raise SystemExit("Attempt references a missing leader result; validate and recover state")
        if (
            recorded.get("command_id") == command_id
            and recorded.get("task_id") == args.task
            and recorded.get("attempt_id") == attempt_id
            and recorded.get("request_contract_sha256") == request_contract_sha256
            and attempt.get("result_request_contract_sha256")
            == request_contract_sha256
        ):
            recorded_handoff = (recorded.get("request_contract") or {}).get(
                "handoff_argument"
            )
            if recorded_handoff:
                try:
                    recovered_handoff = _bind_rejected_attempt_handoff(
                        task=task,
                        attempt=attempt,
                        trusted_result=recorded,
                        handoff_text=str(recorded_handoff),
                    )
                except HandoffContractError as exc:
                    raise SystemExit(f"Leader handoff replay failed closed: {exc}") from exc
                if recovered_handoff:
                    save_task(layout, task)
            print_json({"status": "ok", "idempotent_replay": True, "result": recorded})
            return
        raise SystemExit(
            f"Leader result is already recorded for attempt {attempt.get('attempt_id')}; "
            "reuse its original command-id for an exact idempotent replay"
        )
    orphan_results = [
        row for row in existing_results if row.get("attempt_id") == attempt_id
    ]
    if len(orphan_results) > 1:
        raise SystemExit(
            f"Multiple unbound results exist for attempt {attempt_id}; validate and recover manually"
        )
    if orphan_results:
        orphan = orphan_results[0]
        if (
            orphan.get("evidence_schema_version") != RESULT_EVIDENCE_SCHEMA
            or orphan.get("command_id") != command_id
            or orphan.get("task_id") != args.task
            or orphan.get("request_contract_sha256") != request_contract_sha256
            or orphan.get("attempt_output_sha256") != attempt_output_sha256
            or orphan.get("attempt_output_boundary") != attempt_output_boundary
            or orphan.get("actor_id") != attempt.get("actor_id")
            or orphan.get("provider") != attempt.get("provider")
            or orphan.get("profile") != execution_identity["profile"]
            or orphan.get("profile_sha256") != execution_identity["profile_sha256"]
            or orphan.get("execution_model") != execution_identity["model"]
            or orphan.get("route_envelope_id") != attempt.get("route_envelope_id")
            or orphan.get("route_plan_fingerprint")
            != attempt.get("route_plan_fingerprint")
            or orphan.get("route_plan_step_index")
            != attempt.get("route_plan_step_index")
            or orphan.get("route_predecessors")
            != (attempt.get("route_predecessors") or [])
            or orphan.get("report_path") != attempt.get("report_path")
            or orphan.get("report_sha256") != result_report_sha256
            or orphan.get("report_size") != attempt.get("report_size")
        ):
            raise SystemExit(
                f"Unbound result for attempt {attempt_id} does not match its immutable request and receipts"
            )
        _apply_leader_result_binding(task, attempt, orphan)
        orphan_handoff = (orphan.get("request_contract") or {}).get(
            "handoff_argument"
        )
        if orphan_handoff:
            try:
                _bind_rejected_attempt_handoff(
                    task=task,
                    attempt=attempt,
                    trusted_result=orphan,
                    handoff_text=str(orphan_handoff),
                )
            except HandoffContractError as exc:
                raise SystemExit(f"Leader handoff recovery failed closed: {exc}") from exc
        set_task_state(layout, task, str(orphan["status"]))
        append_event(
            layout,
            "result_recorded_recovered",
            task_id=args.task,
            actor_id=attempt.get("actor_id"),
            result_id=orphan["id"],
            status=orphan["status"],
            accepted_by_leader=orphan["accepted_by_leader"],
        )
        print_json(
            {
                "status": "ok",
                "idempotent_replay": True,
                "recovered_orphan_result": True,
                "result": orphan,
            }
        )
        return
    if command_result is not None:
        raise SystemExit("record-result command-id is already bound to a different result")
    if attempt.get("status") not in {"waiting_leader", "failed", "escalate"}:
        raise SystemExit(
            f"Leader result rejected while latest attempt is {attempt.get('status')}; collect a finished report first"
        )
    actor_id = args.actor or attempt.get("actor_id") or task.get("agent_id")
    if not actor_id or attempt.get("actor_id") != actor_id or not actor_exists(layout, str(actor_id)):
        raise SystemExit("Leader result actor/attempt binding is missing or stale")
    result_actor = load_actor(layout, str(actor_id))
    runtime_state = (result_actor.get("runtime") or {}).get("provider_execution_state")
    if result_actor.get("status") in {"running", "starting", "needs_recovery"} or runtime_state in {
        "started",
        "finished_pending_finalize",
        "launch_pending_authorization",
    }:
        raise SystemExit("Leader result rejected while provider execution is active or uncertain")
    report_relpath = attempt.get("report_path")
    report_sha256 = attempt.get("report_sha256")
    report_size = attempt.get("report_size")
    if not isinstance(report_relpath, str) or not isinstance(report_sha256, str) or type(report_size) is not int:
        raise SystemExit("Leader result requires a bound report receipt")
    try:
        result_report = (layout.project_dir / report_relpath).resolve(strict=True)
        result_report.relative_to(task_dir(layout, args.task).resolve())
        report_payload = result_report.read_bytes()
    except (OSError, ValueError) as exc:
        raise SystemExit(f"Leader result report receipt is unavailable: {exc}") from exc
    if len(report_payload) != report_size or hashlib.sha256(report_payload).hexdigest() != report_sha256:
        raise SystemExit("Leader result report receipt does not match the collected bytes")
    input_tokens = require_non_negative_int(args.input_tokens, "input-tokens")
    cached_input_tokens = require_non_negative_int(
        getattr(args, "cached_input_tokens", 0),
        "cached-input-tokens",
    )
    output_tokens = require_non_negative_int(args.output_tokens, "output-tokens")
    if attempt:
        input_tokens = input_tokens or int(attempt.get("input_tokens") or 0)
        cached_input_tokens = cached_input_tokens or int(
            attempt.get("cached_input_tokens") or 0
        )
        output_tokens = output_tokens or int(attempt.get("output_tokens") or 0)
    try:
        estimated_cost_cny = (
            None
            if args.estimated_cost_cny is None
            else _money_text(args.estimated_cost_cny, "estimated-cost-cny")
        )
    except ValueError as exc:
        raise SystemExit(str(exc)) from exc
    provider_id = (attempt or {}).get("provider") or task.get("provider")
    pricing_source = "caller_unverified" if estimated_cost_cny is not None else "not_provided"
    cost_verified = False
    if estimated_cost_cny is None and attempt and attempt.get("estimated_cost_cny") is not None:
        estimated_cost_cny = attempt["estimated_cost_cny"]
        pricing_source = str(attempt.get("cost_source") or "attempt_usage")
        cost_verified = bool(attempt.get("cost_settled"))
    elif estimated_cost_cny is None and provider_id:
        try:
            spec, bound_source, price_bound = _attempt_pricing_spec(
                project,
                attempt,
                str(provider_id),
            )
            if spec is not None:
                estimated_units = estimate_provider_cost_units(
                    spec,
                    input_tokens=input_tokens,
                    cached_input_tokens=cached_input_tokens,
                    output_tokens=output_tokens,
                )
                estimated_cost_cny = (
                    None
                    if estimated_units is None
                    else _money_text(
                        Decimal(estimated_units) / _MONEY_SCALE,
                        "result estimated cost",
                    )
                )
            pricing_source = bound_source
            if estimated_cost_cny is not None:
                # A zero-token estimate is not proof that a provider request
                # was free; it commonly means the usage event was missing.
                # Leader-supplied result tokens are useful for reporting, but
                # they are not a provider final-usage receipt and cannot release
                # the attempt hold.
                cost_verified = False
        except RoutingValidationError as exc:
            raise SystemExit(f"Unable to price result: {exc}") from exc
    actor_id = args.actor or (attempt or {}).get("actor_id") or task.get("agent_id")
    agent_name = args.agent or task.get("agent_name") or actor_id or "unknown"
    model = execution_identity["model"]
    row = {
        "id": new_id("RES"),
        "command_id": command_id,
        "event_type": "result",
        "evidence_schema_version": RESULT_EVIDENCE_SCHEMA,
        "request_contract": request_contract,
        "request_contract_sha256": request_contract_sha256,
        "attempt_output_sha256": attempt_output_sha256,
        "attempt_output_boundary": attempt_output_boundary,
        "timestamp": now_iso(),
        "project_id": project.get("project_id"),
        "task_id": args.task,
        "attempt_id": (attempt or {}).get("attempt_id"),
        "route_envelope_id": (attempt or {}).get("route_envelope_id"),
        "route_plan_fingerprint": (attempt or {}).get("route_plan_fingerprint"),
        "route_plan_step_index": (attempt or {}).get("route_plan_step_index"),
        "route_predecessors": json.loads(
            json.dumps(
                (attempt or {}).get("route_predecessors") or [],
                ensure_ascii=False,
                allow_nan=False,
            )
        ),
        "actor_id": actor_id,
        "agent": agent_name,
        "provider": provider_id,
        "tier": (attempt or {}).get("tier") or task.get("tier"),
        "profile": execution_identity["profile"],
        "profile_sha256": execution_identity["profile_sha256"],
        "execution_model": execution_identity["model"],
        "model": model,
        "task_type": task.get("task_type") or "unknown",
        "difficulty": task.get("difficulty") or "normal",
        "status": args.status,
        "completed": args.status == "done",
        "needs_escalation": args.status == "escalate",
        "accepted_by_leader": bool(args.accepted_by_leader),
        "quality_score": args.quality_score,
        "input_tokens": input_tokens,
        "cached_input_tokens": cached_input_tokens,
        "output_tokens": output_tokens,
        "total_tokens": total_tokens(input_tokens, output_tokens, cached_input_tokens),
        "estimated_cost_cny": estimated_cost_cny,
        "cost_source": pricing_source,
        "summary": compact_text(args.summary) if args.summary else "",
        "note": args.note or "",
        "report_path": attempt.get("report_path"),
        "report_sha256": result_report_sha256,
        "report_size": attempt.get("report_size"),
    }
    if handoff_text:
        preview_task = json.loads(json.dumps(task, ensure_ascii=False, allow_nan=False))
        preview_attempt = next(
            row
            for row in preview_task.get("attempts") or []
            if row.get("attempt_id") == attempt_id
        )
        _apply_leader_result_binding(preview_task, preview_attempt, row)
        try:
            _bind_rejected_attempt_handoff(
                task=preview_task,
                attempt=preview_attempt,
                trusted_result=row,
                handoff_text=handoff_text,
            )
        except HandoffContractError as exc:
            raise SystemExit(f"Leader handoff validation failed closed: {exc}") from exc
    append_jsonl(layout.results_jsonl, row)
    _apply_leader_result_binding(task, attempt, row)
    if handoff_text:
        _bind_rejected_attempt_handoff(
            task=task,
            attempt=attempt,
            trusted_result=row,
            handoff_text=handoff_text,
        )
    set_task_state(layout, task, args.status)
    append_event(layout, "result_recorded", task_id=args.task, actor_id=actor_id, result_id=row["id"], status=args.status, accepted_by_leader=bool(args.accepted_by_leader))
    send_message(
        layout,
        sender=SCHEDULER_ID,
        recipient=LEADER_ID,
        subject=f"Result recorded: {args.task}",
        body=f"{args.task} recorded as {args.status}; accepted_by_leader={bool(args.accepted_by_leader)}; quality={args.quality_score}.",
        task_id=args.task,
    )
    print_json({"status": "ok", "recorded": True, "event": row})


def command_record_leader_work(args: Any) -> None:
    layout = resolve_project(args.root, args.project)
    if args.task:
        require_task(layout, args.task)
    if args.work_type not in LEADER_WORK_TYPES:
        raise SystemExit(f"Invalid leader work type: {args.work_type}")
    if args.risk not in RISKS:
        raise SystemExit(f"Invalid risk: {args.risk}")
    input_tokens = require_non_negative_int(args.input_tokens, "input-tokens")
    cached_input_tokens = require_non_negative_int(
        getattr(args, "cached_input_tokens", 0),
        "cached-input-tokens",
    )
    output_tokens = require_non_negative_int(args.output_tokens, "output-tokens")
    minutes = require_non_negative_int(args.minutes, "minutes")
    try:
        estimated_cost_cny = (
            None
            if args.estimated_cost_cny is None
            else _money_text(args.estimated_cost_cny, "estimated-cost-cny")
        )
    except ValueError as exc:
        raise SystemExit(str(exc)) from exc
    project = load_project(layout)
    row = {
        "id": new_id("LWK"),
        "event_type": "leader_self_work",
        "timestamp": now_iso(),
        "project_id": project.get("project_id"),
        "task_id": args.task,
        "agent": "leader",
        "model": args.model,
        "work_type": args.work_type,
        "risk": args.risk,
        "scope": args.scope,
        "reason": args.reason,
        "files": args.file or [],
        "minutes": minutes,
        "wall_seconds": minutes * 60,
        "input_tokens": input_tokens,
        "cached_input_tokens": cached_input_tokens,
        "output_tokens": output_tokens,
        "total_tokens": total_tokens(input_tokens, output_tokens, cached_input_tokens),
        "estimated_cost_cny": estimated_cost_cny,
        "cost_source": cost_source(estimated_cost_cny),
        "note": args.note or "",
    }
    append_jsonl(layout.leader_work_jsonl, row)
    append_event(layout, "leader_self_work_recorded", task_id=args.task, work_id=row["id"], work_type=args.work_type, risk=args.risk)
    print_json({"status": "ok", "recorded": True, "event": row})


def command_record_usage(args: Any) -> None:
    layout = resolve_project(args.root, args.project)
    require_actor(layout, args.actor)
    command_id = getattr(args, "command_id", None)
    if command_id:
        existing = next((row for row in usage_rows(layout) if row.get("command_id") == command_id), None)
        if existing:
            print_json({"status": "ok", "idempotent_replay": True, "usage": existing})
            return
    actor = load_actor(layout, args.actor)
    task_id = args.task or actor.get("task_id")
    if task_id:
        require_task(layout, task_id)
    if args.task and actor.get("task_id") and str(args.task) != str(actor.get("task_id")):
        raise SystemExit(
            f"Usage actor/task binding mismatch: actor {args.actor} belongs to {actor.get('task_id')}, not {args.task}"
        )
    attempt_id = getattr(args, "attempt", None) or actor.get("attempt_id")
    if getattr(args, "attempt", None) and actor.get("attempt_id") != attempt_id:
        raise SystemExit(
            f"Usage actor/attempt binding mismatch: actor {args.actor} belongs to {actor.get('attempt_id')}, not {attempt_id}"
        )
    bound_attempt: dict[str, Any] | None = None
    if task_id and attempt_id:
        bound_task = load_task(layout, task_id)
        bound_attempt = next(
            (row for row in bound_task.get("attempts") or [] if row.get("attempt_id") == attempt_id),
            None,
        )
        if bound_attempt is None:
            raise SystemExit(f"Usage attempt {attempt_id} is not bound to task {task_id}")
        if bound_attempt.get("actor_id") != args.actor:
            raise SystemExit(
                f"Usage actor/attempt binding mismatch: attempt {attempt_id} belongs to {bound_attempt.get('actor_id')}"
            )
        for field in ("provider", "tier", "profile", "model"):
            actor_value = actor.get(field)
            attempt_value = bound_attempt.get(field)
            if (actor_value or None) != (attempt_value or None):
                raise SystemExit(f"Usage {field} binding mismatch for attempt {attempt_id}")
        if bound_attempt.get("usage_final"):
            raise SystemExit(f"Usage is already final for attempt {attempt_id}")
    input_tokens = require_non_negative_int(args.input_tokens, "input-tokens")
    cached_input_tokens = require_non_negative_int(
        getattr(args, "cached_input_tokens", 0),
        "cached-input-tokens",
    )
    output_tokens = require_non_negative_int(args.output_tokens, "output-tokens")
    raw_reported_cost = getattr(args, "estimated_cost_cny", None)
    try:
        estimated_cost_cny = (
            None
            if raw_reported_cost is None
            else _money_text(raw_reported_cost, "estimated-cost-cny")
        )
    except ValueError as exc:
        raise SystemExit(str(exc)) from exc
    project = load_project(layout)
    pricing_source = "caller_unverified" if estimated_cost_cny is not None else "not_provided"
    cost_verified = False
    if estimated_cost_cny is None and actor.get("provider"):
        try:
            spec, bound_source, price_bound = _attempt_pricing_spec(
                project,
                bound_attempt,
                str(actor.get("provider")),
            )
            if spec is not None:
                if price_bound and bound_attempt is not None:
                    previous_input = int(bound_attempt.get("input_tokens") or 0)
                    previous_cached = int(
                        bound_attempt.get("cached_input_tokens") or 0
                    )
                    previous_output = int(bound_attempt.get("output_tokens") or 0)
                    prior_usage_count = int(
                        bound_attempt.get("usage_event_count") or 0
                    )
                    cumulative_units = estimate_provider_cost_units(
                        spec,
                        input_tokens=previous_input + input_tokens,
                        cached_input_tokens=previous_cached + cached_input_tokens,
                        output_tokens=previous_output + output_tokens,
                    )
                    previous_units = (
                        0
                        if prior_usage_count == 0
                        else estimate_provider_cost_units(
                            spec,
                            input_tokens=previous_input,
                            cached_input_tokens=previous_cached,
                            output_tokens=previous_output,
                        )
                    )
                    estimated_units = (
                        None
                        if cumulative_units is None or previous_units is None
                        else cumulative_units - previous_units
                    )
                    if estimated_units is not None and estimated_units < 0:
                        raise RoutingValidationError(
                            "incremental immutable usage cost became negative"
                        )
                else:
                    estimated_units = estimate_provider_cost_units(
                        spec,
                        input_tokens=input_tokens,
                        cached_input_tokens=cached_input_tokens,
                        output_tokens=output_tokens,
                    )
                estimated_cost_cny = (
                    None
                    if estimated_units is None
                    else _money_text(
                        Decimal(estimated_units) / _MONEY_SCALE,
                        "usage estimated cost",
                    )
                )
            pricing_source = bound_source
            if estimated_cost_cny is not None:
                cost_verified = (
                    price_bound
                    and input_tokens + cached_input_tokens + output_tokens > 0
                )
        except RoutingValidationError as exc:
            raise SystemExit(f"Unable to price usage: {exc}") from exc
    row = {
        "id": new_id("USG"),
        "command_id": command_id,
        "event_type": "usage",
        "timestamp": now_iso(),
        "project_id": project.get("project_id"),
        "actor_id": args.actor,
        "role": actor.get("role"),
        "agent": actor.get("agent_name") or ("leader" if actor.get("role") == "leader" else args.actor),
        "task_id": task_id,
        "attempt_id": attempt_id,
        "provider": actor.get("provider"),
        "tier": actor.get("tier"),
        "profile": actor.get("profile"),
        "model": args.model or actor.get("model") or "inherit",
        "input_tokens": input_tokens,
        "cached_input_tokens": cached_input_tokens,
        "output_tokens": output_tokens,
        "total_tokens": total_tokens(input_tokens, output_tokens, cached_input_tokens),
        "estimated_cost_cny": estimated_cost_cny,
        "cost_source": pricing_source,
        "final_usage": bool(getattr(args, "final_usage", False)),
        "note": args.note or "",
    }
    append_jsonl(layout.usage_jsonl, row)
    if task_id and attempt_id:
        task = load_task(layout, task_id)
        for attempt in task.get("attempts") or []:
            if attempt.get("attempt_id") == attempt_id:
                attempt["input_tokens"] = int(attempt.get("input_tokens") or 0) + input_tokens
                attempt["cached_input_tokens"] = int(
                    attempt.get("cached_input_tokens") or 0
                ) + cached_input_tokens
                attempt["output_tokens"] = int(attempt.get("output_tokens") or 0) + output_tokens
                attempt["total_tokens"] = int(attempt.get("total_tokens") or 0) + row["total_tokens"]
                attempt["usage_event_count"] = int(
                    attempt.get("usage_event_count") or 0
                ) + 1
                if (
                    row["total_tokens"] > 0 or raw_reported_cost is not None
                ) and not cost_verified:
                    attempt["usage_cost_unverified"] = True
                authoritative_units: int | None = None
                authoritative_source = pricing_source
                provider_id = attempt.get("provider") or actor.get("provider")
                if provider_id:
                    try:
                        cumulative_spec, cumulative_source, cumulative_bound = _attempt_pricing_spec(
                            project,
                            attempt,
                            str(provider_id),
                        )
                        if cumulative_spec is not None and cumulative_bound:
                            authoritative_units = estimate_provider_cost_units(
                                cumulative_spec,
                                input_tokens=attempt["input_tokens"],
                                cached_input_tokens=attempt["cached_input_tokens"],
                                output_tokens=attempt["output_tokens"],
                            )
                            authoritative_source = cumulative_source
                    except RoutingValidationError as exc:
                        raise SystemExit(f"Unable to reconcile cumulative usage: {exc}") from exc
                if authoritative_units is not None:
                    authoritative_cost = _money_text(
                        Decimal(authoritative_units) / _MONEY_SCALE,
                        "cumulative usage cost",
                    )
                    attempt["actual_cost_cny"] = authoritative_cost
                    attempt["estimated_cost_cny"] = authoritative_cost
                cumulative_verified = bool(
                    authoritative_units is not None
                    and attempt["total_tokens"] > 0
                    and not attempt.get("usage_cost_unverified")
                )
                attempt["actual_cost_verified"] = cumulative_verified
                if bool(getattr(args, "final_usage", False)):
                    attempt["usage_final"] = True
                    attempt["cost_settled"] = cumulative_verified
                    if cumulative_verified:
                        attempt.pop("cost_settlement_blocked_reason", None)
                    else:
                        attempt["cost_settlement_blocked_reason"] = (
                            "final cumulative usage contains unverified tokens or lacks immutable pricing"
                        )
                attempt["cost_source"] = authoritative_source
                break
        save_task(layout, task)
    actor_usage = actor.setdefault(
        "usage",
        {
            "input_tokens": 0,
            "cached_input_tokens": 0,
            "output_tokens": 0,
            "total_tokens": 0,
            "estimated_cost_cny": 0.0,
            "unknown_cost_count": 0,
        },
    )
    actor_usage["input_tokens"] = int(actor_usage.get("input_tokens") or 0) + input_tokens
    actor_usage["cached_input_tokens"] = int(
        actor_usage.get("cached_input_tokens") or 0
    ) + cached_input_tokens
    actor_usage["output_tokens"] = int(actor_usage.get("output_tokens") or 0) + output_tokens
    actor_usage["total_tokens"] = int(actor_usage.get("total_tokens") or 0) + row["total_tokens"]
    if estimated_cost_cny is None:
        actor_usage["unknown_cost_count"] = int(actor_usage.get("unknown_cost_count") or 0) + 1
    else:
        actor_cost_units = _money_units(
            actor_usage.get("estimated_cost_cny") or 0,
            "actor accumulated usage cost",
        ) + _money_units(estimated_cost_cny, "usage row estimated cost")
        actor_usage["estimated_cost_cny"] = _money_text(
            Decimal(actor_cost_units) / _MONEY_SCALE,
            "actor accumulated usage cost",
        )
    actor["heartbeat_at"] = now_iso()
    save_actor(layout, actor)
    sync_actor_summary(layout, actor)
    append_event(layout, "usage_recorded", actor_id=args.actor, task_id=task_id, usage_id=row["id"], total_tokens=row["total_tokens"])
    print_json({"status": "ok", "recorded": True, "event": row})


def actor_rows(layout: ProjectLayout) -> list[dict[str, Any]]:
    rows = []
    for path in sorted(layout.actors_dir.glob("*.json")):
        actor = read_json(path, {})
        actor_id = actor.get("id")
        if actor_id:
            runtime = actor_runtime(actor)
            rows.append(
                {
                    **actor_summary(actor),
                    "heartbeat_at": actor.get("heartbeat_at"),
                    "runtime_backend": runtime.get("backend"),
                    "runtime_target": runtime.get("target"),
                    "runtime_pid": runtime.get("pid"),
                    "runtime_log_path": runtime.get("log_path"),
                    "mailbox_counts": mailbox_counts(layout, actor_id),
                }
            )
    return rows


def result_rows(layout: ProjectLayout) -> list[dict[str, Any]]:
    return read_jsonl(layout.results_jsonl)


RESULT_EVIDENCE_SCHEMA = "costmarshal-result-evidence-v3"
SEMANTIC_PROMPT_RECEIPT_SCHEMA = "costmarshal-semantic-prompt-receipt-v1"
SEMANTIC_PROMPT_MAX_BYTES = 64 * 1024 * 1024
RECEIPT_MAX_BYTES = 8 * 1024 * 1024
AUTHORITY_DOCUMENT_MAX_BYTES = 16 * 1024 * 1024


def _lexical_components_under(
    trusted_root: Path,
    candidate: Path,
    *,
    label: str,
) -> tuple[Path, list[os.stat_result]]:
    """Inspect an un-resolved path beneath a trusted root and reject links/reparse."""

    lexical_root = Path(os.path.abspath(os.path.expanduser(os.fspath(trusted_root))))
    lexical_candidate = Path(os.path.abspath(os.path.expanduser(os.fspath(candidate))))
    try:
        relative = lexical_candidate.relative_to(lexical_root)
    except ValueError as exc:
        raise HandoffContractError(f"{label} escapes its trusted root") from exc
    current = lexical_root
    components = [lexical_root]
    for part in relative.parts:
        current = current / part
        components.append(current)
    states: list[os.stat_result] = []
    reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    try:
        for component in components:
            info = component.lstat()
            if stat.S_ISLNK(info.st_mode) or bool(
                getattr(info, "st_file_attributes", 0) & reparse_flag
            ):
                raise HandoffContractError(
                    f"{label} contains a symlink/reparse component"
                )
            states.append(info)
    except HandoffContractError:
        raise
    except OSError as exc:
        raise HandoffContractError(f"{label} is unavailable: {exc}") from exc
    return lexical_candidate, states


def _read_stable_authority_document(
    *,
    trusted_root: Path,
    path: Path,
    max_bytes: int,
    label: str,
) -> bytes:
    lexical_path, states = _lexical_components_under(
        trusted_root,
        path,
        label=label,
    )
    before = states[-1]
    if (
        not stat.S_ISREG(before.st_mode)
        or before.st_size < 0
        or before.st_size > max_bytes
    ):
        raise HandoffContractError(f"{label} file type or size is invalid")
    try:
        payload = lexical_path.read_bytes()
        after = lexical_path.lstat()
    except OSError as exc:
        raise HandoffContractError(f"{label} cannot be read: {exc}") from exc
    reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    if stat.S_ISLNK(after.st_mode) or bool(
        getattr(after, "st_file_attributes", 0) & reparse_flag
    ):
        raise HandoffContractError(f"{label} changed into a symlink/reparse entry")
    before_identity = (
        int(before.st_size),
        int(before.st_mtime_ns),
        int(getattr(before, "st_ctime_ns", 0)),
        int(getattr(before, "st_ino", 0)),
    )
    after_identity = (
        int(after.st_size),
        int(after.st_mtime_ns),
        int(getattr(after, "st_ctime_ns", 0)),
        int(getattr(after, "st_ino", 0)),
    )
    if before_identity != after_identity or len(payload) != before.st_size:
        raise HandoffContractError(f"{label} changed while it was read")
    return payload


def _read_content_addressed_receipt(
    *,
    trusted_root: Path,
    root: Path,
    raw_path: Any,
    digest: Any,
    expected_size: Any,
    max_bytes: int,
    label: str,
) -> bytes:
    """Read one immutable CAS object without following link/reparse components."""

    if (
        not isinstance(digest, str)
        or not re.fullmatch(r"sha256:[0-9a-f]{64}", digest)
        or type(expected_size) is not int
        or expected_size < 0
        or expected_size > max_bytes
        or not isinstance(raw_path, str)
        or not raw_path
    ):
        raise HandoffContractError(f"{label} receipt metadata is invalid")
    try:
        safe_root = Path(os.path.abspath(os.path.expanduser(os.fspath(root))))
        _lexical_components_under(trusted_root, safe_root, label=f"{label} CAS root")
        supplied = Path(raw_path).expanduser()
        if not supplied.is_absolute():
            raise HandoffContractError(f"{label} receipt path is not absolute")
        expected = safe_root / digest.removeprefix("sha256:")
        if os.path.normcase(os.path.normpath(str(supplied))) != os.path.normcase(
            os.path.normpath(str(expected))
        ):
            raise HandoffContractError(
                f"{label} receipt path is not its canonical content address"
            )
        _, states = _lexical_components_under(
            safe_root,
            expected,
            label=f"{label} receipt",
        )
        before = states[-1]
        if not stat.S_ISREG(before.st_mode) or before.st_size != expected_size:
            raise HandoffContractError(f"{label} receipt file type or size is invalid")
        payload = expected.read_bytes()
        after = expected.lstat()
    except HandoffContractError:
        raise
    except (OSError, ValueError) as exc:
        raise HandoffContractError(f"{label} receipt CAS is unavailable: {exc}") from exc
    before_identity = (
        int(before.st_size),
        int(before.st_mtime_ns),
        int(getattr(before, "st_ctime_ns", 0)),
        int(getattr(before, "st_ino", 0)),
    )
    after_identity = (
        int(after.st_size),
        int(after.st_mtime_ns),
        int(getattr(after, "st_ctime_ns", 0)),
        int(getattr(after, "st_ino", 0)),
    )
    observed = "sha256:" + hashlib.sha256(payload).hexdigest()
    reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    if (
        not stat.S_ISREG(after.st_mode)
        or stat.S_ISLNK(after.st_mode)
        or bool(getattr(after, "st_file_attributes", 0) & reparse_flag)
        or before_identity != after_identity
        or len(payload) != expected_size
        or observed != digest
    ):
        raise HandoffContractError(f"{label} receipt bytes changed or do not match")
    return payload


def _attempt_execution_identity(attempt: dict[str, Any]) -> dict[str, Any] | None:
    raw = attempt.get("execution_identity")
    binding = attempt.get("profile_binding")
    bound_sha = binding.get("sha256") if isinstance(binding, dict) else None
    if isinstance(raw, dict):
        identity = {
            "model": raw.get("model"),
            "profile": raw.get("profile"),
            "profile_sha256": raw.get("profile_sha256"),
        }
    else:
        identity = {
            "model": attempt.get("model") or "inherit",
            "profile": attempt.get("profile"),
            "profile_sha256": bound_sha,
        }
    if (
        not isinstance(identity["model"], str)
        or not identity["model"]
        or identity["profile"] is not None
        and not isinstance(identity["profile"], str)
        or not isinstance(identity["profile_sha256"], str)
        or not re.fullmatch(r"sha256:[0-9a-f]{64}", identity["profile_sha256"])
        or identity["profile_sha256"] != bound_sha
    ):
        return None
    return identity


def audit_result_evidence(
    layout: ProjectLayout,
) -> tuple[list[dict[str, Any]], list[str]]:
    """Return task-bound leader evidence and integrity issues.

    Results are an audit projection, not an authority.  Every routing row must
    round-trip through the authoritative task attempt and its immutable route,
    profile, report, request, and predecessor-result bindings.
    """

    rows = result_rows(layout)
    issues: list[str] = []
    try:
        project_payload = _read_stable_authority_document(
            trusted_root=layout.project_dir,
            path=layout.project_json,
            max_bytes=AUTHORITY_DOCUMENT_MAX_BYTES,
            label="authoritative project document",
        )
        project = json.loads(project_payload.decode("utf-8", errors="strict"))
        if not isinstance(project, dict):
            raise TypeError("project document is not an object")
    except (
        HandoffContractError,
        OSError,
        UnicodeDecodeError,
        ValueError,
        TypeError,
        json.JSONDecodeError,
    ) as exc:
        project = {}
        project_identity_issue = f"authoritative project identity is unavailable: {exc}"
    else:
        project_id_value = project.get("project_id")
        if not isinstance(project_id_value, str) or not project_id_value:
            project_identity_issue = "authoritative project identity is missing"
        elif project_id_value != layout.project_dir.name:
            project_identity_issue = (
                "authoritative project identity does not match its canonical directory"
            )
        else:
            project_identity_issue = None
    project_id = project.get("project_id") if project_identity_issue is None else None
    attempts: dict[str, tuple[dict[str, Any], dict[str, Any]]] = {}
    ambiguous_attempt_ids: set[str] = set()
    tasks: dict[str, dict[str, Any]] = {}
    for task_path in sorted(layout.tasks_dir.glob("*/task.json")):
        try:
            task_payload = _read_stable_authority_document(
                trusted_root=layout.tasks_dir,
                path=task_path,
                max_bytes=AUTHORITY_DOCUMENT_MAX_BYTES,
                label=f"authoritative task document {task_path}",
            )
            task = json.loads(task_payload.decode("utf-8", errors="strict"))
            if not isinstance(task, dict):
                raise TypeError("task document is not an object")
        except (
            HandoffContractError,
            OSError,
            UnicodeDecodeError,
            ValueError,
            TypeError,
            json.JSONDecodeError,
        ) as exc:
            issues.append(f"non-authoritative task document at {task_path}: {exc}")
            continue
        task_id = task.get("id")
        if not isinstance(task_id, str) or not task_id:
            issues.append(f"non-authoritative task document at {task_path}: missing task id")
            continue
        try:
            expected_task_path = Path(
                os.path.abspath(task_dir(layout, task_id) / "task.json")
            )
        except SystemExit as exc:
            issues.append(
                f"non-authoritative task document at {task_path}: {exc}"
            )
            continue
        if (
            os.path.normcase(os.path.normpath(str(task_path)))
            != os.path.normcase(os.path.normpath(str(expected_task_path)))
            or task_path.parent.name != task_id
        ):
            issues.append(
                f"non-authoritative task document at {task_path}: canonical path is {expected_task_path}"
            )
            continue
        if task_id in tasks:
            issues.append(f"duplicate authoritative task_id {task_id}")
            for prior_attempt in tasks[task_id].get("attempts") or []:
                if isinstance(prior_attempt, dict) and isinstance(
                    prior_attempt.get("attempt_id"), str
                ):
                    ambiguous_attempt_ids.add(str(prior_attempt["attempt_id"]))
            for duplicate_attempt in task.get("attempts") or []:
                if isinstance(duplicate_attempt, dict) and isinstance(
                    duplicate_attempt.get("attempt_id"), str
                ):
                    ambiguous_attempt_ids.add(str(duplicate_attempt["attempt_id"]))
            continue
        tasks[task_id] = task
        for attempt in task.get("attempts") or []:
            if not isinstance(attempt, dict):
                continue
            attempt_id = attempt.get("attempt_id")
            if not isinstance(attempt_id, str) or not attempt_id:
                continue
            if attempt_id in attempts:
                issues.append(f"duplicate authoritative attempt_id {attempt_id}")
                ambiguous_attempt_ids.add(attempt_id)
            else:
                attempts[attempt_id] = (task, attempt)

    result_ids: dict[str, tuple[int, dict[str, Any]]] = {}
    command_ids: dict[str, tuple[int, dict[str, Any]]] = {}
    attempt_results: dict[str, tuple[int, dict[str, Any]]] = {}
    invalid_rows: set[int] = set()
    untrusted_rows: set[int] = set()

    def reject(index: int, reason: str) -> None:
        invalid_rows.add(index)
        issues.append(f"results.jsonl line {index + 1} is not trusted routing evidence: {reason}")

    def exclude(index: int) -> None:
        """Retain valid compatibility history without using it for routing."""

        untrusted_rows.add(index)

    for index, row in enumerate(rows):
        if not isinstance(row, dict):
            reject(index, "row is not an object")
            continue
        result_id = row.get("id")
        attempt_id = row.get("attempt_id")
        command_id = row.get("command_id")
        evidence_schema = row.get("evidence_schema_version")
        legacy_v2 = evidence_schema == "costmarshal-result-evidence-v2"
        if evidence_schema not in {
            RESULT_EVIDENCE_SCHEMA,
            "costmarshal-result-evidence-v2",
        }:
            reject(index, "unsupported or missing evidence schema")
        status = row.get("status")
        accepted = row.get("accepted_by_leader")
        quality = row.get("quality_score")
        if status not in RESULT_TASK_STATES:
            reject(index, "result status is invalid")
        if type(accepted) is not bool:
            reject(index, "leader acceptance must be a boolean")
        elif accepted is not (status == "done"):
            reject(index, "leader acceptance must be true exactly for status done")
        if type(quality) is not int or quality not in {1, 2, 3, 4, 5}:
            reject(index, "quality score must be an integer from 1 to 5")
        if not legacy_v2:
            if project_identity_issue is not None:
                reject(index, project_identity_issue)
            elif row.get("project_id") != project_id:
                reject(index, "project_id does not match the authoritative project")
        if not isinstance(result_id, str) or not result_id:
            reject(index, "missing result id")
        elif result_id in result_ids:
            reject(index, f"duplicate result id {result_id}")
            reject(result_ids[result_id][0], f"duplicate result id {result_id}")
        else:
            result_ids[result_id] = (index, row)
        if not isinstance(command_id, str) or not command_id:
            reject(index, "missing command id")
        elif command_id in command_ids:
            reject(index, f"duplicate command id {command_id}")
            reject(command_ids[command_id][0], f"duplicate command id {command_id}")
        else:
            command_ids[command_id] = (index, row)
        if not isinstance(attempt_id, str) or not attempt_id:
            reject(index, "missing attempt id")
            continue
        if attempt_id in ambiguous_attempt_ids:
            reject(index, f"ambiguous duplicate authoritative attempt {attempt_id}")
            continue
        if attempt_id in attempt_results:
            reject(index, f"duplicate result for attempt {attempt_id}")
            reject(
                attempt_results[attempt_id][0],
                f"duplicate result for attempt {attempt_id}",
            )
        else:
            attempt_results[attempt_id] = (index, row)
        if legacy_v2:
            # v2 predates the sealed output/boundary/handoff contract. It is
            # preserved as historical data but can never train v3 routing.
            # Its minimum authoritative bindings still have to be intact so
            # migration does not silently legitimize forged history.
            legacy_binding = attempts.get(attempt_id)
            if legacy_binding is None:
                reject(index, f"unknown legacy attempt {attempt_id}")
                exclude(index)
                continue
            legacy_task, legacy_attempt = legacy_binding
            legacy_identity = _attempt_execution_identity(legacy_attempt)
            legacy_expected = {
                "task_id": legacy_task.get("id"),
                "attempt_id": attempt_id,
                "actor_id": legacy_attempt.get("actor_id"),
                "provider": legacy_attempt.get("provider"),
                "tier": legacy_attempt.get("tier"),
                "profile": (legacy_identity or {}).get("profile"),
                "profile_sha256": (legacy_identity or {}).get("profile_sha256"),
                "execution_model": (legacy_identity or {}).get("model"),
                "model": (legacy_identity or {}).get("model"),
                "route_envelope_id": legacy_attempt.get("route_envelope_id"),
                "route_plan_fingerprint": legacy_attempt.get(
                    "route_plan_fingerprint"
                ),
                "route_plan_step_index": legacy_attempt.get(
                    "route_plan_step_index"
                ),
                "route_predecessors": legacy_attempt.get("route_predecessors")
                or [],
                "task_type": legacy_task.get("task_type") or "unknown",
                "difficulty": legacy_task.get("difficulty") or "normal",
                "report_path": legacy_attempt.get("report_path"),
                "report_sha256": legacy_attempt.get("report_sha256"),
                "report_size": legacy_attempt.get("report_size"),
            }
            if legacy_identity is None:
                reject(index, "legacy attempt lacks an immutable execution identity")
            for field, expected_value in legacy_expected.items():
                observed = row.get(field)
                if field == "provider":
                    observed = row.get("provider_id", row.get("provider"))
                if observed != expected_value:
                    reject(index, f"legacy {field} does not match attempt binding")
            if legacy_attempt.get("leader_result_id") != result_id:
                reject(index, "legacy attempt does not bind this result id")
            if (
                legacy_attempt.get("accepted_by_leader")
                != row.get("accepted_by_leader")
                or legacy_attempt.get("quality_score") != row.get("quality_score")
                or legacy_attempt.get("recorded_result_status")
                != row.get("status")
            ):
                reject(index, "legacy leader decision does not match attempt")
            exclude(index)
            continue
        binding = attempts.get(attempt_id)
        if binding is None:
            reject(index, f"unknown attempt {attempt_id}")
            continue
        task, attempt = binding
        expected_identity = _attempt_execution_identity(attempt)
        isolation_mode = (attempt.get("isolation") or {}).get("mode")
        if isolation_mode == "required":
            expected_attempt_output_boundary = _required_attempt_output_boundary(
                task,
                attempt,
            )
            if expected_attempt_output_boundary == "sealed-required":
                expected_attempt_output_sha256 = attempt.get("attempt_output_sha256")
                expected_result_report_sha256 = None
                try:
                    validated_attempt_output = _validate_required_attempt_output(
                        layout,
                        project,
                        task,
                        attempt,
                    )
                except HandoffContractError as exc:
                    reject(index, f"sealed required attempt output is invalid: {exc}")
                else:
                    expected_attempt_output_sha256 = validated_attempt_output.get(
                        "attempt_output_sha256"
                    )
                    expected_result_report_sha256 = (
                        validated_attempt_output.get("report_receipt") or {}
                    ).get("sha256")
            else:
                expected_attempt_output_sha256 = None
                expected_result_report_sha256 = attempt.get("report_sha256")
        else:
            expected_attempt_output_sha256 = None
            expected_result_report_sha256 = attempt.get("report_sha256")
            expected_attempt_output_boundary = (
                "unsealed-unsafe-native"
                if isolation_mode == "unsafe-native"
                else "unsealed-legacy-non-required"
            )
            if (
                attempt.get("attempt_output") is not None
                or attempt.get("attempt_output_sha256") is not None
            ):
                reject(index, "non-required attempt unexpectedly contains a semantic output")
        expected = {
            "project_id": project_id,
            "task_id": task.get("id"),
            "attempt_id": attempt_id,
            "actor_id": attempt.get("actor_id"),
            "provider": attempt.get("provider"),
            "tier": attempt.get("tier"),
            "profile": (expected_identity or {}).get("profile"),
            "profile_sha256": (expected_identity or {}).get("profile_sha256"),
            "execution_model": (expected_identity or {}).get("model"),
            "model": (expected_identity or {}).get("model"),
            "route_envelope_id": attempt.get("route_envelope_id"),
            "route_plan_fingerprint": attempt.get("route_plan_fingerprint"),
            "route_plan_step_index": attempt.get("route_plan_step_index"),
            "route_predecessors": attempt.get("route_predecessors") or [],
            "report_path": attempt.get("report_path"),
            "report_sha256": expected_result_report_sha256,
            "report_size": attempt.get("report_size"),
            "attempt_output_sha256": expected_attempt_output_sha256,
            "attempt_output_boundary": expected_attempt_output_boundary,
            "task_type": task.get("task_type") or "unknown",
            "difficulty": task.get("difficulty") or "normal",
            "request_contract_sha256": attempt.get("result_request_contract_sha256"),
        }
        if expected_attempt_output_boundary != "sealed-required":
            # Unsealed compatibility/development outcomes remain auditable but
            # can never train required-isolation production routing.
            exclude(index)
        if expected_identity is None:
            reject(index, "attempt lacks an immutable execution identity")
        for field, expected_value in expected.items():
            observed = row.get(field)
            if field == "provider":
                observed = row.get("provider_id", row.get("provider"))
            if observed != expected_value:
                reject(index, f"{field} does not match attempt binding")
        if attempt.get("leader_result_id") != result_id:
            reject(index, "attempt does not bind this result id")
        if attempt.get("accepted_by_leader") != row.get("accepted_by_leader"):
            reject(index, "leader acceptance does not match attempt")
        if attempt.get("quality_score") != row.get("quality_score"):
            reject(index, "quality score does not match attempt")
        if attempt.get("recorded_result_status") != row.get("status"):
            reject(index, "recorded status does not match attempt")
        if (
            attempt.get("result_attempt_output_sha256")
            != expected_attempt_output_sha256
            or attempt.get("result_attempt_output_boundary")
            != expected_attempt_output_boundary
        ):
            reject(index, "attempt result binding does not match its output boundary")
        if (
            expected_attempt_output_boundary == "sealed-required"
            and row.get("status") == "failed"
            and _attempt_has_admitted_successor(task, attempt)
        ):
            reject(index, "sealed failed result cannot retain an admitted successor")
        if (
            not isinstance(row.get("request_contract_sha256"), str)
            or not re.fullmatch(r"sha256:[0-9a-f]{64}", row["request_contract_sha256"])
        ):
            reject(index, "request contract digest is invalid")
        request_contract = row.get("request_contract")
        if not isinstance(request_contract, dict):
            reject(index, "request contract is missing")
        else:
            encoded_contract = json.dumps(
                request_contract,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            ).encode("utf-8")
            if (
                "sha256:" + hashlib.sha256(encoded_contract).hexdigest()
                != row.get("request_contract_sha256")
            ):
                reject(index, "request contract digest does not match its payload")
            request_binding = {
                "schema_version": "costmarshal-record-result-request-v1",
                "command_id": command_id,
                "task_id": task.get("id"),
                "attempt_id": attempt_id,
                "attempt_output_sha256": expected_attempt_output_sha256,
                "attempt_output_boundary": expected_attempt_output_boundary,
                "task_type": task.get("task_type") or "unknown",
                "difficulty": task.get("difficulty") or "normal",
                "status": row.get("status"),
                "accepted_by_leader": row.get("accepted_by_leader"),
                "quality_score": row.get("quality_score"),
            }
            if any(
                request_contract.get(field) != value
                for field, value in request_binding.items()
            ):
                reject(index, "request contract does not match its result/output binding")
            handoff_argument = request_contract.get("handoff_argument")
            if not isinstance(handoff_argument, str):
                reject(index, "request contract handoff argument is invalid")
            elif handoff_argument:
                if (
                    expected_attempt_output_boundary != "sealed-required"
                    or row.get("status") not in {"failed", "escalate"}
                ):
                    reject(index, "handoff is not attached to a rejected sealed result")
                capsule = attempt.get("handoff_capsule")
                handoff_result = attempt.get("handoff_result_evidence")
                try:
                    validated_capsule = validate_handoff_capsule(
                        capsule,
                        trusted_leader_result=row,
                    )
                except (AttributeError, HandoffContractError) as exc:
                    reject(index, f"persisted handoff capsule is invalid: {exc}")
                else:
                    if (
                        handoff_result != row
                        or validated_capsule.get("collaboration_contract_sha256")
                        != (task.get("handoff_contract") or {}).get("contract_sha256")
                        or validated_capsule.get("attempt_input_sha256")
                        != (attempt.get("attempt_input") or {}).get("attempt_input_sha256")
                        or validated_capsule.get("attempt_output_sha256")
                        != expected_attempt_output_sha256
                        or (validated_capsule.get("handoff") or {}).get("text")
                        != handoff_argument
                    ):
                        reject(index, "persisted handoff does not match its exact result/attempt")
            else:
                if (
                    expected_attempt_output_boundary == "sealed-required"
                    and row.get("status") == "escalate"
                    and _attempt_has_admitted_successor(task, attempt)
                ):
                    reject(index, "sealed rejected result is missing its required handoff")
                if (
                    attempt.get("handoff_capsule") is not None
                    or attempt.get("handoff_result_evidence") is not None
                ):
                    reject(index, "attempt contains handoff evidence absent from its request")

    # Verify every conditional lineage against the exact rejected predecessor
    # result and authoritative attempt.  A metadata-only route prefix is not
    # sufficient evidence.
    for index, row in enumerate(rows):
        if index in invalid_rows or index in untrusted_rows or not isinstance(row, dict):
            continue
        predecessors = row.get("route_predecessors") or []
        if not isinstance(predecessors, list):
            reject(index, "route_predecessors is not a list")
            continue
        row_step_index = row.get("route_plan_step_index")
        if (
            type(row_step_index) is not int
            or row_step_index < 0
            or len(predecessors) != row_step_index
        ):
            reject(index, "route predecessor prefix length does not match its step index")
            continue
        if predecessors and row.get("attempt_output_boundary") != "sealed-required":
            if row.get("attempt_output_boundary") in {
                "unsealed-unsafe-native",
                "unsealed-legacy-required",
                "unsealed-legacy-non-required",
            }:
                # Operational continuation metadata remains valid history, but
                # only sealed required evidence may train conditional priors.
                exclude(index)
            else:
                reject(index, "collaboration lineage requires a sealed required output")
            continue
        for predecessor_index, predecessor in enumerate(predecessors):
            if not isinstance(predecessor, dict):
                reject(index, "route predecessor is not an object")
                break
            predecessor_result_entry = result_ids.get(
                str(predecessor.get("result_id") or "")
            )
            predecessor_result = (
                predecessor_result_entry[1]
                if predecessor_result_entry is not None
                else None
            )
            predecessor_attempt_id = predecessor.get("attempt_id")
            predecessor_binding = attempts.get(str(predecessor_attempt_id or ""))
            if predecessor_result is None or predecessor_binding is None:
                reject(index, "route predecessor result/attempt is missing")
                break
            predecessor_task, predecessor_attempt = predecessor_binding
            predecessor_identity = _attempt_execution_identity(predecessor_attempt)
            expected_prefix = predecessors[:predecessor_index]
            if (
                predecessor_result.get("attempt_id") != predecessor_attempt_id
                or predecessor_result.get("attempt_output_boundary")
                != "sealed-required"
                or predecessor_result.get("accepted_by_leader") is not False
                or predecessor_result.get("status") != "escalate"
                or predecessor_attempt.get("accepted_by_leader") is not False
                or predecessor_attempt.get("recorded_result_status") != "escalate"
                or predecessor_attempt.get("leader_result_id") != predecessor.get("result_id")
                or predecessor_task.get("id") != row.get("task_id")
                or predecessor_attempt.get("route_envelope_id") != row.get("route_envelope_id")
                or predecessor_attempt.get("route_plan_fingerprint")
                != row.get("route_plan_fingerprint")
                or predecessor_attempt.get("route_plan_step_index") != predecessor_index
                or predecessor.get("provider_id") != predecessor_attempt.get("provider")
                or predecessor.get("model") != (predecessor_identity or {}).get("model")
                or predecessor.get("profile") != (predecessor_identity or {}).get("profile")
                or predecessor.get("profile_sha256")
                != (predecessor_identity or {}).get("profile_sha256")
                or predecessor_result.get("route_predecessors") != expected_prefix
                or (predecessor_attempt.get("route_predecessors") or [])
                != expected_prefix
            ):
                reject(index, "route predecessor lineage is not an exact rejected result")
                break

    # Integrity is transitive: a successor cannot stay trusted after any
    # predecessor result in its lineage was rejected or excluded.  Iterate to
    # a fixed point so high-tier rows are removed even when an intermediate
    # predecessor becomes invalid in the same audit.
    propagated = True
    while propagated:
        propagated = False
        for index, row in enumerate(rows):
            if index in invalid_rows or index in untrusted_rows or not isinstance(row, dict):
                continue
            predecessors = row.get("route_predecessors") or []
            if not isinstance(predecessors, list):
                continue
            broken = False
            for predecessor in predecessors:
                if not isinstance(predecessor, dict):
                    continue
                entry = result_ids.get(str(predecessor.get("result_id") or ""))
                if entry is not None and (
                    entry[0] in invalid_rows or entry[0] in untrusted_rows
                ):
                    broken = True
                    break
            if broken:
                reject(index, "route predecessor is not trusted routing evidence")
                propagated = True

    trusted = [
        row
        for index, row in enumerate(rows)
        if index not in invalid_rows and index not in untrusted_rows
    ]
    return trusted, issues


def trusted_result_rows(layout: ProjectLayout) -> list[dict[str, Any]]:
    trusted, _ = audit_result_evidence(layout)
    return trusted


def leader_work_rows(layout: ProjectLayout) -> list[dict[str, Any]]:
    return read_jsonl(layout.leader_work_jsonl)


def usage_rows(layout: ProjectLayout) -> list[dict[str, Any]]:
    return read_jsonl(layout.usage_jsonl)


ROUTE_BUDGET_ENVELOPE_SCHEMA = "costmarshal-route-budget-envelope-v2"
_MONEY_SCALE = 1_000_000_000
_MONEY_QUANTUM = Decimal("0.000000001")


def _profile_snapshot_relpath(
    project_id: str,
    owner_id: str,
    index: int,
    profile: str | None,
) -> str:
    label = slugify(profile or "default", "profile")
    return (
        Path("profile-snapshots")
        / slugify(project_id, "project")
        / slugify(owner_id, "owner")
        / f"{index:02d}-{label}.config.toml"
    ).as_posix()


def _bind_route_step_profiles(
    layout: ProjectLayout,
    project: dict[str, Any],
    owner_id: str,
    steps: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    catalog = project_provider_catalog(project)
    bound_steps = json.loads(json.dumps(steps, ensure_ascii=False, allow_nan=False))
    for index, step in enumerate(bound_steps):
        provider = provider_by_id(catalog, str(step.get("provider_id") or ""))
        profile = step.get("profile")
        snapshot_relpath = _profile_snapshot_relpath(
            str(project.get("project_id") or "project"),
            owner_id,
            index,
            profile if isinstance(profile, str) else None,
        )
        if profile is None:
            _, binding = synthetic_default_profile(snapshot_relpath=snapshot_relpath)
        else:
            material = read_named_profile(
                str(profile),
                expected_env_key=provider.get("env_key"),
                snapshot_relpath=snapshot_relpath,
            )
            if material is None:
                raise ProfileBindingError(
                    f"provider profile is unavailable at route admission: {profile}"
                )
            binding = material[1]
        expected_identity = step.get("execution_identity")
        resolved_model = (
            str(provider.get("model"))
            if provider.get("model") not in {None, "", "inherit"}
            else str(binding.get("model") or "inherit")
        )
        actual_identity = {
            "model": resolved_model,
            "profile": profile if isinstance(profile, str) else None,
            "profile_sha256": binding.get("sha256"),
        }
        if expected_identity is not None and expected_identity != actual_identity:
            raise ProfileBindingError(
                f"provider {provider['provider_id']} profile identity changed during route admission"
            )
        step["execution_identity"] = actual_identity
        step["profile_binding"] = binding
    return bound_steps


def _routing_execution_identities(
    project: dict[str, Any],
) -> dict[str, tuple[str, str | None, str | None]]:
    """Resolve the exact mutable-profile identities before reading priors."""

    catalog = project_provider_catalog(project)
    identities: dict[str, tuple[str, str | None, str | None]] = {}
    for index, provider in enumerate(catalog["providers"]):
        provider_id = str(provider["provider_id"])
        profile = provider.get("profile")
        preview_path = _profile_snapshot_relpath(
            str(project.get("project_id") or "project"),
            "routing-evidence",
            index,
            profile if isinstance(profile, str) else None,
        )
        if profile is None:
            _, binding = synthetic_default_profile(snapshot_relpath=preview_path)
        else:
            material = read_named_profile(
                str(profile),
                expected_env_key=provider.get("env_key"),
                snapshot_relpath=preview_path,
            )
            if material is None:
                identities[provider_id] = (
                    str(provider.get("model") or "inherit"),
                    str(profile),
                    None,
                )
                continue
            binding = material[1]
        resolved_model = (
            str(provider.get("model"))
            if provider.get("model") not in {None, "", "inherit"}
            else str(binding.get("model") or "inherit")
        )
        identities[provider_id] = (
            resolved_model,
            profile if isinstance(profile, str) else None,
            str(binding["sha256"]),
        )
    return identities


def _materialize_step_profile(
    layout: ProjectLayout,
    project: dict[str, Any],
    step: dict[str, Any],
) -> dict[str, Any]:
    binding = validate_profile_binding(step.get("profile_binding"))
    if binding.get("status") != "available":
        return binding
    try:
        verify_profile_snapshot(layout.root, binding)
        return binding
    except ProfileBindingError:
        pass
    if binding.get("source_kind") == "synthetic-default":
        payload, current = synthetic_default_profile(
            snapshot_relpath=str(binding["snapshot_relpath"]),
        )
    else:
        provider = provider_by_id(
            project_provider_catalog(project),
            str(step.get("provider_id") or ""),
        )
        material = read_named_profile(
            str(binding.get("logical_name") or ""),
            expected_env_key=provider.get("env_key"),
            snapshot_relpath=str(binding["snapshot_relpath"]),
        )
        if material is None:
            raise ProfileBindingError("admitted provider profile disappeared before snapshot commit")
        payload, current = material
    if current != binding:
        raise ProfileBindingError("provider profile changed after route admission; replan explicitly")
    install_profile_snapshot(layout.root, payload, binding)
    return binding


def _materialize_envelope_profiles(
    layout: ProjectLayout,
    project: dict[str, Any],
    envelope: dict[str, Any],
) -> None:
    for step in envelope.get("planned_steps") or []:
        if step.get("profile_binding") is not None:
            _materialize_step_profile(layout, project, step)


def _budget_money(value: Any, label: str) -> float:
    return _money_from_units(_money_units(value, label))


def _money_decimal(value: Any, label: str) -> Decimal:
    if isinstance(value, bool):
        raise ValueError(f"{label} must be a finite non-negative number")
    try:
        decimal_value = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValueError(f"{label} must be a finite non-negative number") from exc
    if not decimal_value.is_finite() or decimal_value < 0:
        raise ValueError(f"{label} must be a finite non-negative number")
    try:
        normalized = decimal_value.quantize(_MONEY_QUANTUM)
    except InvalidOperation as exc:
        raise ValueError(f"{label} is outside the supported 9-decimal money range") from exc
    if normalized != decimal_value:
        raise ValueError(f"{label} must have at most 9 decimal places")
    return normalized


def _money_units(value: Any, label: str) -> int:
    return int(_money_decimal(value, label) * _MONEY_SCALE)


def _money_text(value: Any, label: str) -> str:
    normalized = _money_decimal(value, label)
    rendered = format(normalized, "f").rstrip("0").rstrip(".")
    return rendered or "0"


def _money_from_units(units: int) -> float:
    return units / _MONEY_SCALE


def _acceptance_probability(raw: Any, label: str) -> float:
    if isinstance(raw, bool):
        raise ValueError(f"{label} must be a finite probability")
    try:
        value = float(raw)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{label} must be a finite probability") from exc
    if not math.isfinite(value) or not 0.0 <= value <= 1.0:
        raise ValueError(f"{label} must be between 0 and 1")
    return value


def _enforce_task_success_floor(
    layout: ProjectLayout,
    task: dict[str, Any],
    candidate_steps: list[dict[str, Any]],
) -> float | None:
    """Enforce the frozen SLA for the remaining, conditionally estimated chain.

    Outcomes already observed are facts, not independent chances that can be
    multiplied into a replacement plan.  Until cross-envelope conditional
    evidence is modeled explicitly, replanning after any prior attempt fails
    closed; continuing the already admitted envelope remains supported.
    """

    raw_minimum = task.get("min_success_probability")
    if raw_minimum is None:
        return None
    minimum = _acceptance_probability(
        raw_minimum,
        f"task {task.get('id') or '?'} minimum success probability",
    )
    if minimum == 0:
        return 0.0
    probability_rows: list[tuple[str, Any]] = []
    prior_attempts = [
        attempt
        for attempt in task.get("attempts") or []
        if isinstance(attempt, dict)
    ]
    if prior_attempts:
        if any(
            not isinstance(attempt.get("leader_result_id"), str)
            or attempt.get("accepted_by_leader") is not False
            for attempt in prior_attempts
        ):
            raise ValueError(
                f"task {task.get('id') or '?'} cannot revise its SLA route while a prior attempt lacks an explicit leader rejection"
            )
        raise ValueError(
            f"task {task.get('id') or '?'} revised route cannot prove its frozen success SLA "
            "because the new envelope lacks exact cross-envelope predecessor-conditioned evidence"
        )
    for index, step in enumerate(candidate_steps):
        prior = step.get("acceptance_prior")
        if not isinstance(prior, dict):
            raise ValueError(
                f"task {task.get('id') or '?'} candidate step {index} lacks its acceptance prior"
            )
        probability_rows.append(
            (
                f"candidate step {index} acceptance prior",
                prior.get("conservative_probability"),
            )
        )
    survival = 1.0
    for label, raw_probability in probability_rows:
        survival *= 1.0 - _acceptance_probability(raw_probability, label)
    combined = round(1.0 - survival, 9)
    if combined + 1e-12 < minimum:
        raise ValueError(
            f"task {task.get('id') or '?'} revised route success probability {combined} "
            f"is below its frozen minimum {minimum}"
        )
    return combined


def validate_route_budget_envelope(task: dict[str, Any]) -> dict[str, Any] | None:
    """Validate the immutable whole-chain reservation stored on a task."""

    raw = task.get("route_budget_envelope")
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise ValueError(f"task {task.get('id') or '?'} route_budget_envelope must be an object")
    expected_fields = {
        "schema_version",
        "envelope_id",
        "plan_fingerprint",
        "estimated_input_tokens",
        "estimated_cached_input_tokens",
        "estimated_output_tokens",
        "planned_steps",
        "reserved_cost_cny",
        "baseline_commitment_cny",
        "status",
        "created_at",
        "released_at",
        "release_reason",
    }
    if set(raw) != expected_fields:
        raise ValueError(f"task {task.get('id') or '?'} route_budget_envelope has unexpected fields")
    if raw.get("schema_version") != ROUTE_BUDGET_ENVELOPE_SCHEMA:
        raise ValueError(f"task {task.get('id') or '?'} route_budget_envelope schema is unsupported")
    if not isinstance(raw.get("envelope_id"), str) or not raw["envelope_id"].startswith("ENV-"):
        raise ValueError(f"task {task.get('id') or '?'} route_budget_envelope envelope_id is invalid")
    status = raw.get("status")
    if status not in {"active", "released"}:
        raise ValueError(f"task {task.get('id') or '?'} route_budget_envelope status is invalid")
    if not isinstance(raw.get("created_at"), str) or not raw["created_at"]:
        raise ValueError(f"task {task.get('id') or '?'} route_budget_envelope created_at is invalid")
    if status == "active":
        if raw.get("released_at") is not None or raw.get("release_reason") is not None:
            raise ValueError(f"task {task.get('id') or '?'} active route_budget_envelope is marked released")
    elif not isinstance(raw.get("released_at"), str) or not raw.get("released_at"):
        raise ValueError(f"task {task.get('id') or '?'} released route_budget_envelope lacks released_at")
    elif not isinstance(raw.get("release_reason"), str) or not raw.get("release_reason"):
        raise ValueError(f"task {task.get('id') or '?'} released route_budget_envelope lacks release_reason")
    token_fields = (
        "estimated_input_tokens",
        "estimated_cached_input_tokens",
        "estimated_output_tokens",
    )
    for field in token_fields:
        if type(raw.get(field)) is not int or raw[field] < 0:
            raise ValueError(f"task {task.get('id') or '?'} route_budget_envelope {field} is invalid")
    steps = raw.get("planned_steps")
    if not isinstance(steps, list) or not steps:
        raise ValueError(f"task {task.get('id') or '?'} route_budget_envelope planned_steps is invalid")
    step_cost_units = 0
    for index, step in enumerate(steps):
        if not isinstance(step, dict):
            raise ValueError(f"task {task.get('id') or '?'} route_budget_envelope step {index} is invalid")
        if step.get("index") != index:
            raise ValueError(f"task {task.get('id') or '?'} route_budget_envelope steps are not contiguous")
        if not isinstance(step.get("provider_id"), str) or not step.get("provider_id"):
            raise ValueError(f"task {task.get('id') or '?'} route_budget_envelope step {index} lacks provider_id")
        if step.get("tier") not in TIER_RANK:
            raise ValueError(f"task {task.get('id') or '?'} route_budget_envelope step {index} has invalid tier")
        if not isinstance(step.get("acceptance_prior"), dict):
            raise ValueError(f"task {task.get('id') or '?'} route_budget_envelope step {index} lacks acceptance_prior")
        if not isinstance(step.get("price_basis"), dict):
            raise ValueError(f"task {task.get('id') or '?'} route_budget_envelope step {index} lacks price_basis")
        execution_identity = step.get("execution_identity")
        if not isinstance(execution_identity, dict) or set(execution_identity) != {
            "model",
            "profile",
            "profile_sha256",
        }:
            raise ValueError(
                f"task {task.get('id') or '?'} route_budget_envelope step {index} lacks execution_identity"
            )
        if (
            not isinstance(execution_identity.get("model"), str)
            or not execution_identity.get("model")
            or execution_identity.get("profile") != step.get("profile")
            or not isinstance(execution_identity.get("profile_sha256"), str)
            or not re.fullmatch(
                r"sha256:[0-9a-f]{64}",
                execution_identity["profile_sha256"],
            )
        ):
            raise ValueError(
                f"task {task.get('id') or '?'} route_budget_envelope step {index} execution_identity is invalid"
            )
        if step.get("profile_binding") is not None:
            try:
                validated_binding = validate_profile_binding(step.get("profile_binding"))
                if validated_binding.get("sha256") != execution_identity.get("profile_sha256"):
                    raise ValueError("profile hash does not match execution_identity")
            except ProfileBindingError as exc:
                raise ValueError(
                    f"task {task.get('id') or '?'} route_budget_envelope step {index} profile binding is invalid"
                ) from exc
        step_cost_units += _money_units(
            step.get("estimated_cost_cny"),
            f"task {task.get('id') or '?'} route_budget_envelope step {index} estimated_cost_cny",
        )
    reserved_units = _money_units(
        raw.get("reserved_cost_cny"),
        f"task {task.get('id') or '?'} route_budget_envelope reserved_cost_cny",
    )
    _budget_money(
        raw.get("baseline_commitment_cny"),
        f"task {task.get('id') or '?'} route_budget_envelope baseline_commitment_cny",
    )
    if reserved_units != step_cost_units:
        raise ValueError(f"task {task.get('id') or '?'} route_budget_envelope reserve does not equal its planned steps")
    try:
        fingerprint = route_plan_fingerprint(
            steps,
            input_tokens=raw["estimated_input_tokens"],
            cached_input_tokens=raw["estimated_cached_input_tokens"],
            output_tokens=raw["estimated_output_tokens"],
        )
    except (RoutingValidationError, TypeError, ValueError) as exc:
        raise ValueError(f"task {task.get('id') or '?'} route_budget_envelope fingerprint input is invalid") from exc
    if raw.get("plan_fingerprint") != fingerprint:
        raise ValueError(f"task {task.get('id') or '?'} route_budget_envelope fingerprint mismatch")
    return raw


def make_route_budget_envelope(
    layout: ProjectLayout,
    project: dict[str, Any],
    task: dict[str, Any],
    decision: Any,
    *,
    baseline_commitment_units: int,
) -> dict[str, Any] | None:
    steps = list(decision.planned_steps)
    if not steps or decision.worst_case_chain_cost_cny is None:
        return None
    _enforce_task_success_floor(layout, task, steps)
    envelope_id = new_id("ENV")
    try:
        steps = _bind_route_step_profiles(layout, project, envelope_id, steps)
    except (ProfileBindingError, RoutingValidationError) as exc:
        raise ValueError(f"provider profile admission failed: {exc}") from exc
    plan_fingerprint = route_plan_fingerprint(
        steps,
        input_tokens=decision.estimated_input_tokens,
        cached_input_tokens=decision.estimated_cached_input_tokens,
        output_tokens=decision.estimated_output_tokens,
    )
    reserve_units = sum(
        _money_units(
            step.get("estimated_cost_cny"),
            f"task {task.get('id') or '?'} admitted route step estimated_cost_cny",
        )
        for step in steps
    )
    reserve = _money_text(
        Decimal(reserve_units) / _MONEY_SCALE,
        "route budget envelope reserve",
    )
    envelope = {
        "schema_version": ROUTE_BUDGET_ENVELOPE_SCHEMA,
        "envelope_id": envelope_id,
        "plan_fingerprint": plan_fingerprint,
        "estimated_input_tokens": decision.estimated_input_tokens,
        "estimated_cached_input_tokens": decision.estimated_cached_input_tokens,
        "estimated_output_tokens": decision.estimated_output_tokens,
        "planned_steps": json.loads(json.dumps(steps, ensure_ascii=False, allow_nan=False)),
        "reserved_cost_cny": reserve,
        "baseline_commitment_cny": _money_text(
            Decimal(baseline_commitment_units) / _MONEY_SCALE,
            "route budget envelope baseline commitment",
        ),
        "status": "active",
        "created_at": now_iso(),
        "released_at": None,
        "release_reason": None,
    }
    preview = dict(task)
    preview["route_budget_envelope"] = envelope
    validate_route_budget_envelope(preview)
    return envelope


def release_route_budget_envelope(task: dict[str, Any], reason: str) -> None:
    envelope = validate_route_budget_envelope(task)
    if envelope is None or envelope.get("status") != "active":
        return
    envelope["status"] = "released"
    envelope["released_at"] = now_iso()
    envelope["release_reason"] = reason


def _envelope_step_index(task: dict[str, Any], envelope: dict[str, Any]) -> int:
    envelope_id = envelope["envelope_id"]
    indexes: list[int] = []
    for attempt in task.get("attempts") or []:
        if attempt.get("route_envelope_id") != envelope_id:
            continue
        index = attempt.get("route_plan_step_index")
        if type(index) is not int or index < 0:
            raise ValueError(f"task {task.get('id') or '?'} has an invalid route plan step binding")
        indexes.append(index)
    if sorted(indexes) != list(range(len(indexes))):
        raise ValueError(f"task {task.get('id') or '?'} route plan step bindings are not contiguous")
    return len(indexes)


def _validate_step_acceptance_evidence(
    task: dict[str, Any],
    step: dict[str, Any],
    step_index: int,
    *,
    trusted_history: list[dict[str, Any]],
) -> None:
    prior = step.get("acceptance_prior")
    if not isinstance(prior, dict):
        raise ValueError(
            f"task {task.get('id') or '?'} route plan step {step_index} lacks acceptance evidence provenance"
        )
    observations = prior.get("observations")
    raw_evidence_ids = prior.get("evidence_result_ids")
    evidence_sha256 = prior.get("evidence_sha256")
    if type(observations) is not int or observations < 0:
        raise ValueError(
            f"task {task.get('id') or '?'} route plan step {step_index} has invalid acceptance observations"
        )
    if observations == 0 and raw_evidence_ids is None and evidence_sha256 is None:
        # Compatibility for an admitted cold-start envelope that used no
        # historical evidence. Non-empty legacy priors fail closed below.
        evidence_ids: tuple[str, ...] = ()
    else:
        if (
            not isinstance(raw_evidence_ids, (list, tuple))
            or any(not isinstance(item, str) or not item for item in raw_evidence_ids)
            or len(set(raw_evidence_ids)) != len(raw_evidence_ids)
            or not isinstance(evidence_sha256, str)
            or not re.fullmatch(r"sha256:[0-9a-f]{64}", evidence_sha256)
        ):
            raise ValueError(
                f"task {task.get('id') or '?'} route plan step {step_index} has invalid acceptance evidence provenance"
            )
        evidence_ids = tuple(raw_evidence_ids)
    if observations > 0 and len(evidence_ids) != observations:
        raise ValueError(
            f"task {task.get('id') or '?'} route plan step {step_index} acceptance evidence ids are incomplete"
        )
    if evidence_ids:
        trusted_by_id = {
            str(row.get("id")): row
            for row in trusted_history
            if isinstance(row.get("id"), str) and row.get("id")
        }
        if any(result_id not in trusted_by_id for result_id in evidence_ids):
            raise ValueError(
                f"task {task.get('id') or '?'} route plan step {step_index} acceptance evidence is no longer trusted"
            )
        current_ids, current_sha256 = acceptance_evidence_provenance(
            trusted_by_id[result_id] for result_id in evidence_ids
        )
        if current_ids != evidence_ids or current_sha256 != evidence_sha256:
            raise ValueError(
                f"task {task.get('id') or '?'} route plan step {step_index} acceptance evidence drifted"
            )


def validate_envelope_dispatch_step(
    task: dict[str, Any],
    envelope: dict[str, Any],
    decision: Any,
    provider_spec: dict[str, Any],
    *,
    trusted_history: list[dict[str, Any]],
) -> tuple[int, dict[str, Any]]:
    index = _envelope_step_index(task, envelope)
    steps = envelope["planned_steps"]
    if index >= len(steps):
        raise ValueError(f"task {task.get('id') or '?'} route budget envelope has no remaining step")
    step = steps[index]
    if decision.provider_id != step.get("provider_id") or decision.tier != step.get("tier"):
        raise ValueError(
            f"task {task.get('id') or '?'} route plan requires step {index} "
            f"{step.get('provider_id')} ({step.get('tier')}), not {decision.provider_id} ({decision.tier})"
        )
    current_basis = provider_price_basis(provider_spec)
    if current_basis != step.get("price_basis"):
        raise ValueError(
            f"task {task.get('id') or '?'} route plan price basis drifted before step {index}; replan explicitly"
        )
    for remaining_index, remaining_step in enumerate(steps[index:], start=index):
        _validate_step_acceptance_evidence(
            task,
            remaining_step,
            remaining_index,
            trusted_history=trusted_history,
        )
    planned_cost_units = _money_units(
        step.get("estimated_cost_cny"),
        f"task {task.get('id') or '?'} route plan step {index} estimated_cost_cny",
    )
    current_step = decision.planned_steps[0] if decision.planned_steps else None
    if current_step is None or _money_units(
        current_step.get("estimated_cost_cny"),
        f"task {task.get('id') or '?'} current route estimated_cost_cny",
    ) != planned_cost_units:
        raise ValueError(f"task {task.get('id') or '?'} route plan cost drifted before step {index}; replan explicitly")
    forecast = (
        decision.estimated_input_tokens,
        decision.estimated_cached_input_tokens,
        decision.estimated_output_tokens,
    )
    bound_forecast = (
        envelope["estimated_input_tokens"],
        envelope["estimated_cached_input_tokens"],
        envelope["estimated_output_tokens"],
    )
    if forecast != bound_forecast:
        raise ValueError(f"task {task.get('id') or '?'} route plan token forecast drifted; replan explicitly")
    return index, step


def _attempt_budget_commitment_units(attempt: dict[str, Any]) -> int:
    active_or_unsettled = (
        attempt.get("status")
        in {"preparing", "dispatched", "launch_pending", "running", "starting", "needs_recovery"}
        or not attempt.get("cost_settled")
    )

    def amount_units(field: str, *, required: bool, default: int = 0) -> int:
        raw = attempt.get(field)
        if raw is None:
            if required:
                raise ValueError(f"attempt {attempt.get('attempt_id') or attempt.get('attempt') or '?'} is missing {field}")
            return default
        if isinstance(raw, bool):
            raise ValueError(f"attempt {attempt.get('attempt_id') or '?'} has invalid {field}")
        return _money_units(
            raw,
            f"attempt {attempt.get('attempt_id') or attempt.get('attempt') or '?'} {field}",
        )

    actual_units = amount_units("actual_cost_cny", required=True)
    reserved_units = amount_units("reserved_cost_cny", required=active_or_unsettled)
    if active_or_unsettled:
        return max(actual_units, reserved_units)
    return actual_units


def attempt_budget_commitment(attempt: dict[str, Any]) -> float:
    return _money_from_units(_attempt_budget_commitment_units(attempt))


def _task_budget_commitment_units(task: dict[str, Any]) -> int:
    attempt_units = sum(
        _attempt_budget_commitment_units(attempt)
        for attempt in task.get("attempts") or []
    )
    envelope = validate_route_budget_envelope(task)
    if envelope is None or envelope.get("status") != "active":
        return attempt_units
    next_step = _envelope_step_index(task, envelope)
    future_units = sum(
        _money_units(
            step.get("estimated_cost_cny"),
            f"task {task.get('id') or '?'} route_budget_envelope future step estimated_cost_cny",
        )
        for step in envelope["planned_steps"][next_step:]
    )
    return attempt_units + future_units


def task_budget_commitment(task: dict[str, Any]) -> float:
    return _money_from_units(_task_budget_commitment_units(task))


def _project_budget_commitment_units(layout: ProjectLayout) -> int:
    return sum(_task_budget_commitment_units(task) for task in task_rows(layout))


def project_budget_commitment(layout: ProjectLayout) -> float:
    return _money_from_units(_project_budget_commitment_units(layout))


def empty_token_bucket() -> dict[str, Any]:
    return {
        "input_tokens": 0,
        "cached_input_tokens": 0,
        "output_tokens": 0,
        "total_tokens": 0,
        "estimated_cost_cny": 0.0,
        "unknown_cost_count": 0,
        "usage_rows": 0,
        "result_rows": 0,
        "leader_work_rows": 0,
        "source": "none",
    }


def add_token_values(bucket: dict[str, Any], row: dict[str, Any], *, source: str) -> None:
    bucket["input_tokens"] = int(bucket.get("input_tokens") or 0) + int(row.get("input_tokens") or 0)
    bucket["cached_input_tokens"] = int(
        bucket.get("cached_input_tokens") or 0
    ) + int(row.get("cached_input_tokens") or 0)
    bucket["output_tokens"] = int(bucket.get("output_tokens") or 0) + int(row.get("output_tokens") or 0)
    bucket["total_tokens"] = int(bucket.get("total_tokens") or 0) + int(row.get("total_tokens") or 0)
    if row.get("estimated_cost_cny") is None:
        bucket["unknown_cost_count"] = int(bucket.get("unknown_cost_count") or 0) + 1
    else:
        bucket["estimated_cost_cny"] = round(float(bucket.get("estimated_cost_cny") or 0.0) + float(row.get("estimated_cost_cny") or 0.0), 6)
    key = f"{source}_rows"
    bucket[key] = int(bucket.get(key) or 0) + 1
    if bucket.get("source") in {None, "none"}:
        bucket["source"] = source
    elif source not in str(bucket.get("source", "")).split("+"):
        bucket["source"] = f"{bucket['source']}+{source}"


def add_token_bucket(bucket: dict[str, Any], source_bucket: dict[str, Any]) -> None:
    bucket["input_tokens"] = int(bucket.get("input_tokens") or 0) + int(source_bucket.get("input_tokens") or 0)
    bucket["cached_input_tokens"] = int(
        bucket.get("cached_input_tokens") or 0
    ) + int(source_bucket.get("cached_input_tokens") or 0)
    bucket["output_tokens"] = int(bucket.get("output_tokens") or 0) + int(source_bucket.get("output_tokens") or 0)
    bucket["total_tokens"] = int(bucket.get("total_tokens") or 0) + int(source_bucket.get("total_tokens") or 0)
    bucket["estimated_cost_cny"] = round(float(bucket.get("estimated_cost_cny") or 0.0) + float(source_bucket.get("estimated_cost_cny") or 0.0), 6)
    bucket["unknown_cost_count"] = int(bucket.get("unknown_cost_count") or 0) + int(source_bucket.get("unknown_cost_count") or 0)
    for key in ["usage_rows", "result_rows", "leader_work_rows"]:
        bucket[key] = int(bucket.get(key) or 0) + int(source_bucket.get(key) or 0)
    source = source_bucket.get("source")
    if source and source != "none":
        if bucket.get("source") in {None, "none"}:
            bucket["source"] = source
        elif source not in str(bucket.get("source", "")).split("+"):
            bucket["source"] = f"{bucket['source']}+{source}"


def actor_token_summary(
    results: list[dict[str, Any]],
    leader_work: list[dict[str, Any]],
    usage: list[dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    buckets: dict[str, dict[str, Any]] = {}
    usage_by_task: dict[tuple[str, str], dict[str, Any]] = {}
    result_by_task: dict[tuple[str, str], dict[str, Any]] = {}
    for row in usage:
        actor_id = row.get("actor_id")
        if not actor_id:
            continue
        task_key = str(row.get("task_id") or row.get("id") or "")
        add_token_values(usage_by_task.setdefault((actor_id, task_key), empty_token_bucket()), row, source="usage")
    for row in results:
        actor_id = row.get("actor_id") or row.get("agent")
        if not actor_id:
            continue
        task_key = str(row.get("task_id") or row.get("id") or "")
        add_token_values(result_by_task.setdefault((actor_id, task_key), empty_token_bucket()), row, source="result")
    for key in sorted(set(usage_by_task) | set(result_by_task)):
        usage_bucket = usage_by_task.get(key)
        result_bucket = result_by_task.get(key)
        if usage_bucket and result_bucket:
            chosen = usage_bucket if int(usage_bucket.get("total_tokens") or 0) >= int(result_bucket.get("total_tokens") or 0) else result_bucket
        else:
            chosen = usage_bucket or result_bucket or empty_token_bucket()
        add_token_bucket(buckets.setdefault(key[0], empty_token_bucket()), chosen)
    for row in leader_work:
        add_token_values(buckets.setdefault(LEADER_ID, empty_token_bucket()), row, source="leader_work")
    return buckets


def summarize_results(rows: list[dict[str, Any]]) -> dict[str, Any]:
    quality_total = 0
    quality_count = 0
    by_status: dict[str, int] = {}
    by_agent: dict[str, dict[str, Any]] = {}
    input_tokens = 0
    cached_input_tokens = 0
    output_tokens = 0
    total_token_count = 0
    estimated_cost_cny = 0.0
    unknown_cost_count = 0
    accepted = 0
    escalated = 0
    for row in rows:
        status = row.get("status") or "unknown"
        by_status[status] = by_status.get(status, 0) + 1
        agent = row.get("agent") or "unknown"
        agent_bucket = by_agent.setdefault(agent, {"count": 0, "accepted": 0, "escalated": 0, "estimated_cost_cny": 0.0})
        agent_bucket["count"] += 1
        if row.get("accepted_by_leader"):
            accepted += 1
            agent_bucket["accepted"] += 1
        if row.get("needs_escalation") or status == "escalate":
            escalated += 1
            agent_bucket["escalated"] += 1
        quality = row.get("quality_score")
        if isinstance(quality, int):
            quality_total += quality
            quality_count += 1
        input_tokens += int(row.get("input_tokens") or 0)
        cached_input_tokens += int(row.get("cached_input_tokens") or 0)
        output_tokens += int(row.get("output_tokens") or 0)
        total_token_count += int(row.get("total_tokens") or 0)
        if row.get("estimated_cost_cny") is None:
            unknown_cost_count += 1
        else:
            cost = float(row.get("estimated_cost_cny") or 0.0)
            estimated_cost_cny += cost
            agent_bucket["estimated_cost_cny"] += cost
    for bucket in by_agent.values():
        bucket["estimated_cost_cny"] = round(bucket["estimated_cost_cny"], 6)
    return {
        "count": len(rows),
        "accepted": accepted,
        "escalated": escalated,
        "accept_rate": round(accepted / len(rows), 3) if rows else 0.0,
        "avg_quality": round(quality_total / quality_count, 3) if quality_count else 0.0,
        "by_status": by_status,
        "by_agent": by_agent,
        "input_tokens": input_tokens,
        "cached_input_tokens": cached_input_tokens,
        "output_tokens": output_tokens,
        "total_tokens": total_token_count,
        "estimated_cost_cny": round(estimated_cost_cny, 6),
        "unknown_cost_count": unknown_cost_count,
        "latest_events": rows[-5:],
    }


def summarize_leader_self_work(rows: list[dict[str, Any]]) -> dict[str, Any]:
    total_minutes = 0
    input_tokens = 0
    cached_input_tokens = 0
    output_tokens = 0
    total_token_count = 0
    estimated_cost_cny = 0.0
    unknown_cost_count = 0
    by_type: dict[str, int] = {}
    by_risk: dict[str, int] = {}
    for row in rows:
        total_minutes += int(row.get("minutes") or 0)
        input_tokens += int(row.get("input_tokens") or 0)
        cached_input_tokens += int(row.get("cached_input_tokens") or 0)
        output_tokens += int(row.get("output_tokens") or 0)
        total_token_count += int(row.get("total_tokens") or 0)
        if row.get("estimated_cost_cny") is None:
            unknown_cost_count += 1
        else:
            estimated_cost_cny += float(row.get("estimated_cost_cny") or 0.0)
        work_type = row.get("work_type") or "unknown"
        risk = row.get("risk") or "unknown"
        by_type[work_type] = by_type.get(work_type, 0) + 1
        by_risk[risk] = by_risk.get(risk, 0) + 1
    return {
        "count": len(rows),
        "total_minutes": total_minutes,
        "total_wall_seconds": total_minutes * 60,
        "input_tokens": input_tokens,
        "cached_input_tokens": cached_input_tokens,
        "output_tokens": output_tokens,
        "total_tokens": total_token_count,
        "estimated_cost_cny": round(estimated_cost_cny, 6),
        "unknown_cost_count": unknown_cost_count,
        "by_type": by_type,
        "by_risk": by_risk,
        "latest_events": rows[-5:],
    }


def summarize_usage(rows: list[dict[str, Any]]) -> dict[str, Any]:
    input_tokens = 0
    cached_input_tokens = 0
    output_tokens = 0
    total_token_count = 0
    estimated_cost_cny = 0.0
    unknown_cost_count = 0
    by_actor: dict[str, dict[str, Any]] = {}
    for row in rows:
        input_value = int(row.get("input_tokens") or 0)
        cached_input_value = int(row.get("cached_input_tokens") or 0)
        output_value = int(row.get("output_tokens") or 0)
        total_value = int(row.get("total_tokens") or 0)
        input_tokens += input_value
        cached_input_tokens += cached_input_value
        output_tokens += output_value
        total_token_count += total_value
        actor_id = row.get("actor_id") or "unknown"
        bucket = by_actor.setdefault(
            actor_id,
            {
                "input_tokens": 0,
                "cached_input_tokens": 0,
                "output_tokens": 0,
                "total_tokens": 0,
                "estimated_cost_cny": 0.0,
                "unknown_cost_count": 0,
            },
        )
        bucket["input_tokens"] += input_value
        bucket["cached_input_tokens"] += cached_input_value
        bucket["output_tokens"] += output_value
        bucket["total_tokens"] += total_value
        if row.get("estimated_cost_cny") is None:
            unknown_cost_count += 1
            bucket["unknown_cost_count"] += 1
        else:
            cost = float(row.get("estimated_cost_cny") or 0.0)
            estimated_cost_cny += cost
            bucket["estimated_cost_cny"] = round(float(bucket.get("estimated_cost_cny") or 0.0) + cost, 6)
    return {
        "count": len(rows),
        "input_tokens": input_tokens,
        "cached_input_tokens": cached_input_tokens,
        "output_tokens": output_tokens,
        "total_tokens": total_token_count,
        "estimated_cost_cny": round(estimated_cost_cny, 6),
        "unknown_cost_count": unknown_cost_count,
        "by_actor": by_actor,
        "latest_events": rows[-5:],
    }


def task_rows(layout: ProjectLayout) -> list[dict[str, Any]]:
    rows = []
    for path in sorted(layout.tasks_dir.glob("*/task.json")):
        task = read_json(path, {})
        rows.append(
            {
                "id": task.get("id"),
                "title": task.get("title"),
                "status": task.get("status"),
                "agent_id": task.get("agent_id"),
                "provider": task.get("provider"),
                "profile": task.get("profile"),
                "model": task.get("model"),
                "attempts": [
                    redact_launch_token(attempt, attempt)
                    if isinstance(attempt, dict)
                    else attempt
                    for attempt in task.get("attempts") or []
                ],
                "route_budget_envelope": task.get("route_budget_envelope"),
                "report_path": task.get("report_path"),
                "summary": task.get("summary"),
                "leader_result": task.get("leader_result"),
            }
        )
    return rows


def status_payload(layout: ProjectLayout) -> dict[str, Any]:
    project = load_project(layout)
    session = load_session(layout)
    actors = actor_rows(layout)
    tasks = task_rows(layout)
    results = result_rows(layout)
    leader_work = leader_work_rows(layout)
    usage = usage_rows(layout)
    token_by_actor = actor_token_summary(results, leader_work, usage)
    for actor in actors:
        actor["token_usage"] = token_by_actor.get(actor["id"], empty_token_bucket())
    task_counts: dict[str, int] = {}
    for task in tasks:
        state = task.get("status") or "unknown"
        task_counts[state] = task_counts.get(state, 0) + 1
    return {
        "project": project,
        "session": session,
        "scheduler": load_scheduler_state(layout),
        "backend": session_backend_config(session),
        "actor_count": len(actors),
        "actors": actors,
        "task_count": len(tasks),
        "task_state_counts": task_counts,
        "tasks": tasks,
        "result_summary": summarize_results(results),
        "leader_self_work": summarize_leader_self_work(leader_work),
        "usage_summary": summarize_usage(usage),
        "relay_cursors": load_relay_cursors(layout),
        "active_locks": active_lock_rows(layout),
        "control_store": control_store_status(layout),
    }


def process_liveness(layout: ProjectLayout, actor: dict[str, Any]) -> dict[str, Any]:
    session = load_session(layout)
    backend = backend_from_session(session)
    runtime = actor.get("runtime") or actor_runtime(actor)
    runtime_name = runtime.get("actor_name") or actor_runtime_name(actor["id"])
    backend_available = backend.available()
    alive = False
    reason = "backend_unavailable"
    if backend_available:
        alive = backend.actor_alive(
            session_name=backend_session_name(session),
            actor_name=runtime_name,
            target=runtime.get("target"),
            pid=runtime.get("pid"),
            process_start_marker=runtime.get("process_start_marker"),
        )
        reason = "alive" if alive else "not_found"
    return {"alive": alive, "reason": reason, "backend_available": backend_available}


def scheduler_process_row(layout: ProjectLayout) -> dict[str, Any]:
    ensure_mailbox(layout, SCHEDULER_ID)
    state = load_scheduler_state(layout)
    pid = state.get("pid")
    try:
        pid_value = int(pid) if pid else None
    except (TypeError, ValueError):
        pid_value = None
    alive = pid_is_alive(pid_value) if pid_value else False
    return {
        "id": SCHEDULER_ID,
        "role": "scheduler",
        "status": state.get("status") or "idle",
        "alive": alive,
        "liveness_reason": "alive" if alive else "not_running",
        "runtime_backend": "process",
        "runtime_target": f"pid:{pid_value}" if pid_value else None,
        "runtime_pid": pid_value,
        "runtime_log_path": None,
        "model": "-",
        "provider": "-",
        "profile": "-",
        "task_id": None,
        "heartbeat_at": state.get("heartbeat_at"),
        "mailbox_counts": mailbox_counts(layout, SCHEDULER_ID),
        "token_usage": empty_token_bucket(),
        "cycle_count": state.get("cycle_count", 0),
        "processed_commands": state.get("processed_commands", 0),
    }


def dashboard_payload(layout: ProjectLayout) -> dict[str, Any]:
    payload = status_payload(layout)
    process_rows = [scheduler_process_row(layout)]
    for actor in payload["actors"]:
        liveness = process_liveness(layout, actor)
        row = {
            **actor,
            "alive": liveness["alive"],
            "liveness_reason": liveness["reason"],
            "runtime_backend_available": liveness["backend_available"],
        }
        process_rows.append(row)
    events = read_jsonl(layout.events_jsonl)
    payload["processes"] = process_rows
    payload["recent_events"] = events[-8:]
    payload["scheduler_commands"] = (payload.get("relay_cursors") or {}).get("scheduler_commands") or {}
    return payload


def render_dashboard(payload: dict[str, Any]) -> str:
    project = payload["project"]
    backend = payload.get("backend") or {}
    scheduler = payload.get("scheduler") or {}
    lines = [
        f"# CostMarshal v2 Dashboard: {project.get('name')}",
        "",
        f"Project id: `{project.get('project_id')}`",
        f"Objective: {compact_text(project.get('objective') or '', 120)}",
        f"Backend: `{backend.get('kind')}` session `{backend.get('session_name')}`",
        f"Scheduler: `{scheduler.get('status')}` pid `{scheduler.get('pid') or '-'}` cycles `{scheduler.get('cycle_count') or 0}` commands `{scheduler.get('processed_commands') or 0}`",
        "",
        "## Process Board",
        "| Process | Role | State | Alive | PID/Target | Provider/Profile | Model | Task | Mailbox | Tokens | Cost CNY | Log |",
        "| --- | --- | --- | --- | --- | --- | --- | --- | ---: | ---: | ---: | --- |",
    ]
    for row in payload["processes"]:
        counts = row.get("mailbox_counts") or {"inbox": 0, "outbox": 0}
        tokens = row.get("token_usage") or empty_token_bucket()
        target = row.get("runtime_target") or (f"pid:{row.get('runtime_pid')}" if row.get("runtime_pid") else "-")
        log_path = row.get("runtime_log_path") or "-"
        alive = "yes" if row.get("alive") else "no"
        lines.append(
            f"| {row.get('id')} | {row.get('role')} | {row.get('status')} | {alive} | {target} | {row.get('provider') or '-'} / {row.get('profile') or 'default'} | {row.get('model') or '-'} | {row.get('task_id') or '-'} | in {counts.get('inbox', 0)} / out {counts.get('outbox', 0)} | {tokens.get('total_tokens', 0)} | {tokens.get('estimated_cost_cny', 0.0)} | {log_path} |"
        )
    lines.extend(
        [
            "",
            "## Agent Token Totals",
            "| Agent Actor | Agent | Input | Cached Input | Output | Total | Cost CNY | Source |",
            "| --- | --- | ---: | ---: | ---: | ---: | ---: | --- |",
        ]
    )
    agent_rows = [row for row in payload["processes"] if row.get("role") == "agent"]
    if agent_rows:
        for row in agent_rows:
            tokens = row.get("token_usage") or empty_token_bucket()
            lines.append(
                f"| {row.get('id')} | {row.get('agent_name') or row.get('id')} | {tokens.get('input_tokens', 0)} | {tokens.get('cached_input_tokens', 0)} | {tokens.get('output_tokens', 0)} | {tokens.get('total_tokens', 0)} | {tokens.get('estimated_cost_cny', 0.0)} | {tokens.get('source') or 'none'} |"
            )
    else:
        lines.append("| - | - | 0 | 0 | 0 | 0 | 0.0 | none |")
    results = payload["result_summary"]
    usage = payload["usage_summary"]
    lines.extend(
        [
            "",
            "## Ledgers",
            f"- Usage rows: {usage['count']} tokens total {usage['total_tokens']} cost {usage['estimated_cost_cny']} unknown {usage['unknown_cost_count']}",
            f"- Result rows: {results['count']} accepted {results['accepted']} tokens total {results['total_tokens']} cost {results['estimated_cost_cny']} unknown {results['unknown_cost_count']}",
            "",
            "## Scheduler Commands",
            f"- Inbox cursor: {payload.get('scheduler_commands', {}).get('inbox_lines', 0)}",
            f"- Last processed: {payload.get('scheduler_commands', {}).get('last_processed_at') or '-'}",
            f"- Last counts: ok {payload.get('scheduler_commands', {}).get('last_processed_count', 0)} / failed {payload.get('scheduler_commands', {}).get('last_failed_count', 0)}",
            "",
            "## Tasks",
            "| Task | State | Actor | Provider/Profile | Model | Attempts | Report |",
            "| --- | --- | --- | --- | --- | ---: | --- |",
        ]
    )
    for task in payload["tasks"]:
        lines.append(f"| {task.get('id')} | {task.get('status')} | {task.get('agent_id') or '-'} | {task.get('provider') or '-'} / {task.get('profile') or 'default'} | {task.get('model') or '-'} | {len(task.get('attempts') or [])} | {task.get('report_path') or '-'} |")
    lines.extend(["", "## Recent Events"])
    if payload["recent_events"]:
        lines.append("| Event | Actor | Task | Time |")
        lines.append("| --- | --- | --- | --- |")
        for event in payload["recent_events"]:
            lines.append(f"| {event.get('event_type')} | {event.get('actor_id') or event.get('sender') or '-'} | {event.get('task_id') or '-'} | {event.get('timestamp') or '-'} |")
    else:
        lines.append("- none")
    return "\n".join(lines)


def command_dashboard(args: Any) -> None:
    layout = resolve_project(args.root, args.project)
    interval = max(float(args.interval), 0.1)
    while True:
        payload = dashboard_payload(layout)
        if args.format == "json":
            print_json(payload)
        else:
            if args.watch:
                os.system("cls" if os.name == "nt" else "clear")
            print(render_dashboard(payload))
        if not args.watch:
            break
        time.sleep(interval)


def command_status(args: Any) -> None:
    layout = resolve_project(args.root, args.project)
    payload = status_payload(layout)
    if args.format == "json":
        print_json(payload)
        return
    lines = [
        f"# CostMarshal v2 Status: {payload['project'].get('name')}",
        "",
        f"Project id: `{payload['project'].get('project_id')}`",
        f"Backend: `{(payload.get('backend') or {}).get('kind')}`",
        f"Session: `{(payload.get('backend') or {}).get('session_name')}`",
        f"Scheduler: `{payload['project'].get('scheduler_contract', {}).get('role')}`",
        f"Scheduler state: `{payload.get('scheduler', {}).get('status')}` pid `{payload.get('scheduler', {}).get('pid') or '-'}` cycles `{payload.get('scheduler', {}).get('cycle_count') or 0}`",
        "",
        "## Actors",
        "| Actor | Role | Status | Backend | Provider/Profile | Model | Task | Mailbox | Tokens |",
        "| --- | --- | --- | --- | --- | --- | --- | ---: | ---: |",
    ]
    for actor in payload["actors"]:
        counts = actor["mailbox_counts"]
        tokens = actor.get("token_usage") or empty_token_bucket()
        lines.append(
            f"| {actor['id']} | {actor['role']} | {actor.get('status')} | {actor.get('runtime_backend') or '-'} | {actor.get('provider') or '-'} / {actor.get('profile') or 'default'} | {actor.get('model')} | {actor.get('task_id') or '-'} | in {counts['inbox']} / out {counts['outbox']} | {tokens.get('total_tokens', 0)} |"
        )
    lines.extend(["", "## Relay Cursors"])
    relay_actors = (payload.get("relay_cursors") or {}).get("actors") or {}
    if relay_actors:
        lines.append("| Actor | Outbox Lines | Last Relayed |")
        lines.append("| --- | ---: | --- |")
        for actor_id, cursor in sorted(relay_actors.items()):
            lines.append(f"| {actor_id} | {cursor.get('outbox_lines', 0)} | {cursor.get('last_relayed_at') or '-'} |")
    else:
        lines.append("- none")
    results = payload["result_summary"]
    lines.extend(
        [
            "",
            "## Result Ledger",
            f"- Records: {results['count']}",
            f"- Accepted by leader: {results['accepted']} (rate {results['accept_rate']})",
            f"- Avg quality: {results['avg_quality']}",
            f"- Tokens: in {results['input_tokens']} / cached {results['cached_input_tokens']} / out {results['output_tokens']} / total {results['total_tokens']}",
            f"- Est. cost CNY: {results['estimated_cost_cny']} (unknown {results['unknown_cost_count']})",
        ]
    )
    if results["latest_events"]:
        lines.extend(["", "| Task | Status | Agent | Model | Quality | Accepted |", "| --- | --- | --- | --- | ---: | --- |"])
        for event in results["latest_events"]:
            lines.append(
                f"| {event.get('task_id')} | {event.get('status')} | {event.get('agent') or '-'} | {event.get('model') or '-'} | {event.get('quality_score') or '-'} | {event.get('accepted_by_leader')} |"
            )
    usage = payload["usage_summary"]
    lines.extend(
        [
            "",
            "## Actor Usage Ledger",
            f"- Records: {usage['count']}",
            f"- Tokens: in {usage['input_tokens']} / cached {usage['cached_input_tokens']} / out {usage['output_tokens']} / total {usage['total_tokens']}",
            f"- Est. cost CNY: {usage['estimated_cost_cny']} (unknown {usage['unknown_cost_count']})",
        ]
    )
    leader_work = payload["leader_self_work"]
    lines.extend(
        [
            "",
            "## Leader Self-Work",
            f"- Records: {leader_work['count']}",
            f"- Minutes: {leader_work['total_minutes']}",
            f"- Tokens: in {leader_work['input_tokens']} / cached {leader_work['cached_input_tokens']} / out {leader_work['output_tokens']} / total {leader_work['total_tokens']}",
            f"- Est. cost CNY: {leader_work['estimated_cost_cny']} (unknown {leader_work['unknown_cost_count']})",
        ]
    )
    if leader_work["latest_events"]:
        lines.extend(["", "| Task | Type | Risk | Minutes | Scope |", "| --- | --- | --- | ---: | --- |"])
        for event in leader_work["latest_events"]:
            lines.append(
                f"| {event.get('task_id') or '-'} | {event.get('work_type')} | {event.get('risk')} | {event.get('minutes') or 0} | {compact_text(event.get('scope') or '', 64) or '-'} |"
            )
    lines.extend(["", "## Active Write Claims"])
    if payload["active_locks"]:
        lines.append("| Path | Task | Actor | Agent |")
        lines.append("| --- | --- | --- | --- |")
        for claim in payload["active_locks"]:
            lines.append(f"| {claim.get('path')} | {claim.get('task_id')} | {claim.get('actor_id') or '-'} | {claim.get('agent') or '-'} |")
    else:
        lines.append("- none")
    lines.extend(["", "## Tasks", "| Task | Status | Actor | Model | Report |", "| --- | --- | --- | --- | --- |"])
    for task in payload["tasks"]:
        lines.append(f"| {task['id']} | {task.get('status')} | {task.get('agent_id') or '-'} | {task.get('model') or '-'} | {task.get('report_path') or '-'} |")
    print("\n".join(lines))


def command_recover(args: Any) -> None:
    layout = resolve_project(args.root, args.project)
    project = load_project(layout)
    governance = project.get("governance") or {"mode": "off"}
    try:
        enforce_governance_contract(
            governance,
            project.get("workspace"),
            operation="recovery",
        )
    except GovernanceError as exc:
        raise SystemExit(
            f"ArchMarshal governance gate blocked recovery [{exc.code}]: {exc}"
        ) from exc
    ensure_runtime_dirs(layout)
    session = load_session(layout)
    backend = backend_from_session(session)
    session_name = backend_session_name(session)
    issues: list[str] = []
    planned_restarts: list[str] = []
    restarted: list[str] = []
    recovered_reports: list[str] = []
    for actor in actor_rows(layout):
        ensure_mailbox(layout, actor["id"])
        prompt_path = actor.get("prompt_path")
        prompt_missing = not prompt_path or not (layout.project_dir / prompt_path).is_file()
        if prompt_missing:
            actor_data = load_actor(layout, actor["id"])
            refresh_actor_prompt(layout, actor_data)
            save_actor(layout, actor_data)
            sync_actor_summary(layout, actor_data)
    if not backend.available():
        issues.append(f"{backend.kind} backend not available: {backend.executable}")
    else:
        for actor in actor_rows(layout):
            runtime = actor_runtime(actor)
            runtime_name = runtime.get("actor_name") or actor_runtime_name(actor["id"])
            is_alive = backend.actor_alive(
                session_name=session_name,
                actor_name=runtime_name,
                target=runtime.get("target"),
                pid=runtime.get("pid"),
                process_start_marker=runtime.get("process_start_marker"),
            )
            if actor.get("status") in {"running", "starting", "needs_recovery"} and not is_alive:
                if runtime.get("oci_lifecycle_state") == "uncertain_start":
                    issues.append(
                        f"uncertain OCI start requires stop-actor --stop-runtime before restart: {actor['id']}"
                    )
                    actor_data = load_actor(layout, actor["id"])
                    actor_data["status"] = "needs_recovery"
                    save_actor(layout, actor_data)
                    sync_actor_summary(layout, actor_data)
                    continue
                try:
                    credential_receipt = _cleanup_prestart_credential(layout, actor)
                except IsolationError as exc:
                    issues.append(
                        f"pre-start credential cleanup failed for {actor['id']} [{exc.code}]: {exc}"
                    )
                    actor_data = load_actor(layout, actor["id"])
                    actor_data["status"] = "needs_recovery"
                    save_actor(layout, actor_data)
                    sync_actor_summary(layout, actor_data)
                    continue
                if credential_receipt is not None:
                    actor = load_actor(layout, actor["id"])
                    runtime = actor_runtime(actor)
                if runtime.get("provider_execution_state") == "started" and actor.get("task_id"):
                    attempt_report = task_dir(layout, str(actor["task_id"])) / "attempts" / f"{slugify(actor['id'], 'actor')}.md"
                    try:
                        resolved_report = attempt_report.resolve(strict=True)
                        resolved_report.relative_to(task_dir(layout, str(actor["task_id"])).resolve())
                        if attempt_report.is_symlink() or not attempt_report.is_file():
                            raise ValueError("attempt report is not a regular file")
                        report_bytes = attempt_report.read_bytes()
                        if len(report_bytes) > 1024 * 1024:
                            raise ValueError("attempt report exceeds 1 MiB")
                        report_text = report_bytes.decode("utf-8")
                        report_text = report_text.replace("\r\n", "\n").replace("\r", "\n")
                        status_match = re.search(
                            r"(?im)^\s*(?:[*_]{1,2})?status\s*:\s*(done|failed|escalate)\b",
                            report_text,
                        )
                        if status_match is None:
                            raise ValueError("attempt report has no valid Status field")
                    except (OSError, UnicodeDecodeError, ValueError) as exc:
                        issues.append(f"provider outcome unknown for {actor['id']}: {exc}")
                        status_match = None
                    if status_match is not None:
                        report_status = status_match.group(1).lower()
                        recovered_task = load_task(layout, str(actor["task_id"]))
                        attempts = recovered_task.get("attempts") or []
                        current_attempt = attempts[-1] if attempts else None
                        if not current_attempt or current_attempt.get("attempt_id") != actor.get("attempt_id"):
                            issues.append(f"stale recovered report ignored for {actor['id']}")
                            stale_actor = load_actor(layout, actor["id"])
                            stale_actor["status"] = "stopped"
                            stale_actor.setdefault("runtime", {})["provider_execution_state"] = "finished_stale_ignored"
                            stale_actor["runtime"]["recovered_at"] = now_iso()
                            save_actor(layout, stale_actor)
                            sync_actor_summary(layout, stale_actor)
                            continue
                        actor_data = load_actor(layout, actor["id"])
                        actor_data["status"] = "stopped"
                        actor_runtime_data = actor_data.setdefault("runtime", {})
                        actor_runtime_data["provider_execution_state"] = "finished_recovered"
                        actor_runtime_data["report_sha256"] = hashlib.sha256(report_bytes).hexdigest()
                        actor_runtime_data["report_size"] = len(report_bytes)
                        actor_runtime_data["recovered_at"] = now_iso()
                        save_actor(layout, actor_data)
                        sync_actor_summary(layout, actor_data)
                        current_attempt["report_path"] = relpath(attempt_report, layout.project_dir)
                        current_attempt["report_sha256"] = actor_runtime_data["report_sha256"]
                        current_attempt["report_size"] = len(report_bytes)
                        current_attempt["provider_execution_state"] = "finished_recovered"
                        current_attempt["worker_outcome"] = (
                            report_status
                            if report_status in {"failed", "escalate"}
                            else "waiting_leader"
                        )
                        save_task(layout, recovered_task)
                        collected_state = "waiting_leader"
                        canonical_report = task_dir(layout, str(actor["task_id"])) / "completion-report.md"
                        # Publish exactly the bytes that passed containment,
                        # type, size, encoding, and Status validation. This
                        # replaces any stale canonical report from an earlier
                        # attempt before collect is queued.
                        atomic_write_bytes(canonical_report, report_bytes)
                        send_message(
                            layout,
                            sender=actor["id"],
                            recipient=SCHEDULER_ID,
                            subject="scheduler.command",
                            body="Recover a completed provider report without re-running the provider.",
                            task_id=str(actor["task_id"]),
                            metadata={
                                "command": "collect_task",
                                "args": {
                                    "task": actor["task_id"],
                                    "actor": actor["id"],
                                    "attempt": actor.get("attempt_id"),
                                    "state": collected_state,
                                    "report": str(canonical_report),
                                    "summary": "Recovered an attempt-specific report after runner interruption.",
                                },
                            },
                        )
                        append_event(
                            layout,
                            "actor_report_recovered",
                            actor_id=actor["id"],
                            task_id=actor["task_id"],
                            attempt_id=actor.get("attempt_id"),
                            report_sha256=actor_runtime_data["report_sha256"],
                        )
                        recovered_reports.append(actor["id"])
                        continue
                pending_finalize = (
                    runtime.get("provider_execution_state")
                    == "finished_pending_finalize"
                )
                if not pending_finalize or not args.restart_missing:
                    issues.append(
                        (
                            f"provider completion pending finalization: {actor['id']}"
                            if pending_finalize
                            else f"recoverable actor missing runtime: {actor['id']} ({actor.get('status')})"
                        )
                    )
                actor_data = load_actor(layout, actor["id"])
                actor_data["status"] = "needs_recovery"
                save_actor(layout, actor_data)
                sync_actor_summary(layout, actor_data)
                if args.plan_restarts:
                    command = actor_launch_command(layout, session, actor_data)
                    plan = backend.start_plan(session_name=session_name, actor_name=runtime_name, command=command, session_exists=True)
                    planned_restarts.extend(
                        redact_launch_token(command_to_string(argv), actor_data)
                        for argv in plan
                    )
                if args.restart_missing and runtime.get("provider_execution_state") != "started":
                    transaction = current_transaction()
                    if transaction is not None and control_store_enabled(layout):
                        attempt_id = str(actor_data.get("attempt_id") or "")
                        if not attempt_id:
                            issues.append(f"recoverable actor has no attempt id: {actor_data['id']}")
                            continue
                        actor_runtime_data = actor_data.setdefault("runtime", {})
                        recovery_generation = int(actor_runtime_data.get("recovery_generation") or 0) + 1
                        effect_id = (
                            f"EFF-SPAWN-RECOVER-{slugify(attempt_id, 'attempt')}-{recovery_generation}"
                        )
                        actor_runtime_data["recovery_generation"] = recovery_generation
                        actor_runtime_data["recovery_effect_id"] = effect_id
                        actor_data["status"] = "starting"
                        save_actor(layout, actor_data)
                        sync_actor_summary(layout, actor_data)
                        recovered_task = load_task(layout, str(actor_data["task_id"]))
                        for attempt in recovered_task.get("attempts") or []:
                            if attempt.get("attempt_id") == attempt_id:
                                attempt["status"] = "launch_pending"
                                attempt["recovery_effect_id"] = effect_id
                                attempt.pop("launch_error", None)
                                break
                        save_task(layout, recovered_task)
                        transaction.queue_effect(
                            effect_id=effect_id,
                            effect_type=SPAWN_EFFECT_TYPE,
                            aggregate_id=attempt_id,
                            generation=recovery_generation,
                            payload=_spawn_effect_payload(actor_data),
                        )
                        restarted.append(f"queued:{effect_id}")
                    else:
                        actor_data["status"] = "starting"
                        save_actor(layout, actor_data)
                        sync_actor_summary(layout, actor_data)
                        start_payload = start_actor(layout, actor_data, dry_run=False)
                        restarted.extend(start_payload.get("commands", []))
                        if actor_data.get("task_id") and actor_data.get("attempt_id"):
                            recovered_task = load_task(layout, actor_data["task_id"])
                            for attempt in recovered_task.get("attempts") or []:
                                if attempt.get("attempt_id") == actor_data.get("attempt_id"):
                                    attempt["status"] = "running"
                                    attempt["started_at"] = attempt.get("started_at") or now_iso()
                                    attempt.pop("launch_error", None)
                                    break
                            if recovered_task.get("status") == "dispatched":
                                set_task_state(layout, recovered_task, "running")
                            else:
                                save_task(layout, recovered_task)
    status = "ok" if not issues else "degraded"
    session = load_session(layout)
    session["recovery"] = {"last_recovered_at": now_iso(), "last_status": status, "issues": issues}
    save_session(layout, session)
    append_event(layout, "recovery_audit", status=status, issue_count=len(issues))
    print_json({"status": status, "issues": issues, "planned_restarts": planned_restarts, "restarted": restarted, "recovered_reports": recovered_reports})


def read_rows_for_validation(path: Path, label: str, issues: list[str]) -> list[dict[str, Any]]:
    try:
        return read_jsonl(path)
    except json.JSONDecodeError as exc:
        issues.append(f"{label} contains invalid JSONL: line {exc.lineno}")
    except OSError as exc:
        issues.append(f"{label} cannot be read: {exc}")
    return []


def validate_non_negative_number(value: Any, label: str, issues: list[str], *, allow_none: bool = False) -> None:
    if value is None and allow_none:
        return
    if isinstance(value, bool):
        issues.append(f"{label} must be numeric")
        return
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        issues.append(f"{label} must be numeric")
        return
    if not math.isfinite(numeric):
        issues.append(f"{label} must be finite")
        return
    if numeric < 0:
        issues.append(f"{label} must be non-negative")


def validate_non_negative_integer(value: Any, label: str, issues: list[str]) -> bool:
    if type(value) is not int or value < 0:
        issues.append(f"{label} must be a non-negative integer")
        return False
    return True


def validate_token_triplet(row: dict[str, Any], label: str, issues: list[str]) -> None:
    input_value = row.get("input_tokens")
    cached_value = row.get("cached_input_tokens", 0)
    output_value = row.get("output_tokens")
    total_value = row.get("total_tokens")
    valid = all(
        (
            validate_non_negative_integer(input_value, f"{label} input_tokens", issues),
            validate_non_negative_integer(cached_value, f"{label} cached_input_tokens", issues),
            validate_non_negative_integer(output_value, f"{label} output_tokens", issues),
            validate_non_negative_integer(total_value, f"{label} total_tokens", issues),
        )
    )
    if valid and total_value != input_value + cached_value + output_value:
        issues.append(
            f"{label} total_tokens must equal input_tokens + cached_input_tokens + output_tokens"
        )


def validate_layout(layout: ProjectLayout) -> list[str]:
    issues: list[str] = []
    required = [
        layout.project_json,
        layout.session_json,
        layout.scheduler_state_json,
        layout.events_jsonl,
        layout.relay_cursors_json,
        layout.locks_json,
        layout.protocol_md,
        layout.reports_dir,
        layout.results_jsonl,
        layout.leader_work_jsonl,
        layout.usage_jsonl,
        layout.actors_dir,
        layout.mailboxes_dir,
        layout.tasks_dir,
    ]
    for path in required:
        if not path.exists():
            issues.append(f"missing {relpath(path, layout.project_dir)}")
    if issues:
        return issues
    project = load_project(layout)
    if project.get("schema_version") != SCHEMA_VERSION:
        issues.append("project schema_version is not 2")
    try:
        catalog = project_provider_catalog(project)
    except RoutingValidationError as exc:
        catalog = None
        issues.append(f"invalid provider catalog: {exc}")
    governance = project.get("governance")
    if governance is not None:
        if not isinstance(governance, dict) or governance.get("mode") not in {"off", "auto", "required"}:
            issues.append("invalid governance configuration")
        elif governance.get("ready") and not isinstance(governance.get("binding"), dict):
            issues.append("ready governance configuration is missing its binding")
    source_project = project.get("source_project")
    if source_project and not Path(source_project).exists():
        issues.append(f"source project no longer exists: {source_project}")
    session = load_session(layout)
    scheduler_state = load_scheduler_state(layout)
    if scheduler_state.get("id") != SCHEDULER_ID:
        issues.append("scheduler state id must be scheduler")
    backend_config = session_backend_config(session)
    backend_kind = backend_config.get("kind")
    if backend_kind not in {"local", "tmux"}:
        issues.append(f"invalid session backend: {backend_kind}")
    if not backend_config.get("session_name"):
        issues.append("backend session_name is required")
    leader_id = session.get("leader_actor_id") or LEADER_ID
    if not (layout.actors_dir / f"{slugify(leader_id, 'actor')}.json").is_file():
        issues.append("missing leader actor file")
    for actor in actor_rows(layout):
        runtime = actor_runtime(actor)
        runtime_backend = runtime.get("backend")
        if runtime_backend not in {"local", "tmux"}:
            issues.append(f"{actor['id']} has invalid runtime backend: {runtime_backend}")
        if runtime_backend != backend_kind:
            issues.append(f"{actor['id']} runtime backend {runtime_backend} does not match session backend {backend_kind}")
        if not actor.get("prompt_path"):
            issues.append(f"{actor['id']} missing prompt_path")
        elif not (layout.project_dir / actor["prompt_path"]).is_file():
            issues.append(f"{actor['id']} prompt file missing: {actor['prompt_path']}")
        counts = mailbox_counts(layout, actor["id"])
        if counts["inbox"] < 0 or counts["outbox"] < 0:
            issues.append(f"invalid mailbox for {actor['id']}")
    actor_ids = {actor["id"] for actor in actor_rows(layout)}
    task_ids = {task["id"] for task in task_rows(layout)}
    active_claims = active_lock_rows(layout)
    for claim in active_claims:
        if claim.get("task_id") not in task_ids:
            issues.append(f"active claim references missing task {claim.get('task_id')}: {claim.get('path')}")
        task = load_task(layout, claim["task_id"]) if claim.get("task_id") in task_ids else {}
        if task and task.get("status") not in ACTIVE_TASK_STATES:
            issues.append(f"active claim belongs to inactive task {claim.get('task_id')}: {claim.get('path')}")
    for index, claim in enumerate(active_claims):
        for other in active_claims[index + 1 :]:
            if other.get("task_id") == claim.get("task_id"):
                continue
            if (claim.get("override") or other.get("override")):
                continue
            if paths_conflict(claim.get("path", ""), other.get("path", "")):
                issues.append(f"active claim conflict: {claim.get('path')} ({claim.get('task_id')}) overlaps {other.get('path')} ({other.get('task_id')})")
    for task_json in sorted(layout.tasks_dir.glob("*/task.json")):
        task = read_json(task_json, {})
        if task.get("agent_id") and task["agent_id"] not in actor_ids:
            issues.append(f"{task['id']} references missing actor {task['agent_id']}")
        if task.get("status") == "done":
            leader_result = task.get("leader_result") or {}
            if leader_result.get("status") != "done" or leader_result.get("accepted_by_leader") is not True:
                issues.append(f"{task['id']} is done without an accepted leader result")
        validate_non_negative_integer(
            task.get("estimated_input_tokens", 0),
            f"{task['id']} estimated_input_tokens",
            issues,
        )
        validate_non_negative_integer(
            task.get("estimated_cached_input_tokens", 0),
            f"{task['id']} estimated_cached_input_tokens",
            issues,
        )
        validate_non_negative_integer(
            task.get("estimated_output_tokens", 0),
            f"{task['id']} estimated_output_tokens",
            issues,
        )
        envelope_by_id: dict[str, dict[str, Any]] = {}
        current_envelope: dict[str, Any] | None = None
        try:
            current_envelope = validate_route_budget_envelope(task)
            if current_envelope is not None:
                envelope_by_id[str(current_envelope["envelope_id"])] = current_envelope
            if (
                current_envelope is not None
                or task.get("max_cost_cny") is not None
                or (project.get("routing_policy") or {}).get("project_budget_cny") is not None
            ):
                task_budget_commitment(task)
        except ValueError as exc:
            issues.append(f"{task['id']} has invalid route budget envelope: {exc}")
        envelope_history = task.get("route_budget_envelope_history") or []
        if not isinstance(envelope_history, list):
            issues.append(f"{task['id']} route_budget_envelope_history must be a list")
            envelope_history = []
        for envelope_index, historical_envelope in enumerate(envelope_history):
            preview_task = {
                "id": task.get("id"),
                "route_budget_envelope": historical_envelope,
            }
            try:
                validated_history = validate_route_budget_envelope(preview_task)
                if validated_history is None or validated_history.get("status") != "released":
                    raise ValueError("historical route budget envelope must be released")
                envelope_id = str(validated_history["envelope_id"])
                if envelope_id in envelope_by_id:
                    raise ValueError(f"duplicate envelope_id {envelope_id}")
                envelope_by_id[envelope_id] = validated_history
            except ValueError as exc:
                issues.append(
                    f"{task['id']} route_budget_envelope_history[{envelope_index}] is invalid: {exc}"
                )
        if current_envelope is not None and current_envelope.get("status") == "active" and catalog is not None:
            try:
                next_step = _envelope_step_index(task, current_envelope)
                required_capabilities = set(task.get("required_capabilities") or [])
                for step in current_envelope["planned_steps"][next_step:]:
                    profile_binding = validate_profile_binding(
                        step.get("profile_binding"),
                        require_available=True,
                    )
                    verify_profile_snapshot(layout.root, profile_binding)
                    current_provider = provider_by_id(catalog, str(step.get("provider_id") or ""))
                    if not current_provider.get("enabled"):
                        raise ValueError(f"future provider {current_provider['provider_id']} is disabled")
                    if not required_capabilities.issubset(set(current_provider.get("capabilities") or [])):
                        raise ValueError(f"future provider {current_provider['provider_id']} lost required capabilities")
                    if provider_price_basis(current_provider) != step.get("price_basis"):
                        raise ValueError(f"future provider {current_provider['provider_id']} price basis drifted")
                    pricing_state = pricing_snapshot_status(current_provider)
                    if pricing_state not in {"current", "beta-legacy"}:
                        raise ValueError(
                            f"future provider {current_provider['provider_id']} pricing is {pricing_state}"
                        )
            except (ProfileBindingError, RoutingValidationError, ValueError) as exc:
                issues.append(f"{task['id']} active route budget envelope is not executable: {exc}")
        try:
            normalized_claims = list(normalize_path_list(task.get("claimed_paths") or [], kind="claim"))
            normalized_allowed = list(normalize_path_list(task.get("allowed_paths") or [], kind="allowed"))
            if normalized_claims != (task.get("claimed_paths") or []):
                issues.append(f"{task['id']} claimed_paths are not canonical")
            if normalized_allowed != (task.get("allowed_paths") or []):
                issues.append(f"{task['id']} allowed_paths are not canonical")
        except SecurityValidationError as exc:
            issues.append(f"{task['id']} has unsafe paths: {exc}")
        attempt_ids: set[str] = set()
        for attempt in task.get("attempts") or []:
            attempt_id = attempt.get("attempt_id")
            if not attempt_id:
                # Legacy attempts predate first-class attempt ids.
                continue
            if attempt_id in attempt_ids:
                issues.append(f"{task['id']} has duplicate attempt_id {attempt_id}")
            attempt_ids.add(attempt_id)
            envelope_id = attempt.get("route_envelope_id")
            plan_fingerprint = attempt.get("route_plan_fingerprint")
            if envelope_id is not None or plan_fingerprint is not None:
                bound_envelope = envelope_by_id.get(str(envelope_id))
                step_index = attempt.get("route_plan_step_index")
                if bound_envelope is None:
                    issues.append(f"{task['id']} attempt {attempt_id} references an unknown route plan")
                elif plan_fingerprint != bound_envelope.get("plan_fingerprint"):
                    issues.append(f"{task['id']} attempt {attempt_id} route plan fingerprint mismatch")
                elif type(step_index) is not int or not 0 <= step_index < len(bound_envelope["planned_steps"]):
                    issues.append(f"{task['id']} attempt {attempt_id} has invalid route plan step index")
                else:
                    planned_step = bound_envelope["planned_steps"][step_index]
                    if attempt.get("route_plan_step") != planned_step:
                        issues.append(f"{task['id']} attempt {attempt_id} route plan step binding mismatch")
                    if attempt.get("provider") != planned_step.get("provider_id"):
                        issues.append(f"{task['id']} attempt {attempt_id} route plan provider mismatch")
                    if (attempt.get("model") or "inherit") != (planned_step.get("model") or "inherit"):
                        issues.append(f"{task['id']} attempt {attempt_id} route plan model mismatch")
                    if attempt.get("profile") != planned_step.get("profile"):
                        issues.append(f"{task['id']} attempt {attempt_id} route plan profile mismatch")
                    if (
                        planned_step.get("profile_binding") is not None
                        and attempt.get("profile_binding") != planned_step.get("profile_binding")
                    ):
                        issues.append(f"{task['id']} attempt {attempt_id} route plan profile binding mismatch")
            if attempt.get("profile_binding") is None and attempt.get("status") in {
                "preparing",
                "dispatched",
                "launch_pending",
                "starting",
                "running",
                "needs_recovery",
            }:
                issues.append(f"{task['id']} active attempt {attempt_id} is missing its profile binding")
            if attempt.get("profile_binding") is not None:
                try:
                    binding = validate_profile_binding(attempt.get("profile_binding"))
                    if binding.get("status") == "available":
                        verify_profile_snapshot(layout.root, binding)
                except ProfileBindingError as exc:
                    issues.append(
                        f"{task['id']} attempt {attempt_id} profile binding is invalid: {exc}"
                    )
                actor_id = attempt.get("actor_id")
                if actor_id and actor_exists(layout, str(actor_id)):
                    actor_data = load_actor(layout, str(actor_id))
                    if actor_data.get("profile_binding") != attempt.get("profile_binding"):
                        issues.append(f"{task['id']} attempt {attempt_id} actor profile binding mismatch")
            execution_identity = _attempt_execution_identity(attempt)
            if execution_identity is None:
                issues.append(
                    f"{task['id']} attempt {attempt_id} is missing an immutable execution identity"
                )
            elif isinstance(attempt.get("route_plan_step"), dict) and (
                attempt["route_plan_step"].get("execution_identity")
                != execution_identity
            ):
                issues.append(
                    f"{task['id']} attempt {attempt_id} route execution identity mismatch"
                )
            if catalog is not None:
                try:
                    spec = provider_by_id(catalog, str(attempt.get("provider") or ""))
                    if attempt.get("tier") != spec.get("tier"):
                        issues.append(f"{task['id']} attempt {attempt_id} tier/provider mismatch")
                except RoutingValidationError as exc:
                    issues.append(f"{task['id']} attempt {attempt_id} has invalid provider: {exc}")
            provider_id = str(attempt.get("provider") or "")
            if provider_id:
                try:
                    pricing_spec, _, _ = _attempt_pricing_spec(project, attempt, provider_id)
                    if pricing_spec is not None:
                        estimate_provider_cost(
                            pricing_spec,
                            input_tokens=0,
                            cached_input_tokens=0,
                            output_tokens=0,
                        )
                except RoutingValidationError as exc:
                    issues.append(
                        f"{task['id']} attempt {attempt_id} has invalid price binding: {exc}"
                    )
        task_path = task_dir(layout, task["id"])
        for rel in ["task.json", "brief.md", "status.json", "completion-report.md"]:
            if not (task_path / rel).exists():
                issues.append(f"{task['id']} missing {rel}")
        status = read_json(task_path / "status.json", {}) if (task_path / "status.json").exists() else {}
        if status and status.get("state") != task.get("status"):
            issues.append(f"{task['id']} task.json status={task.get('status')} but status.json state={status.get('state')}")
        report_path = task.get("report_path")
        if task.get("status") in {"done", "failed", "escalate", "waiting_leader"} and report_path:
            resolved = Path(report_path)
            if not resolved.is_absolute():
                resolved = layout.project_dir / resolved
            if not resolved.is_file():
                issues.append(f"{task['id']} report_path is missing: {report_path}")
    result_rows_to_validate = read_rows_for_validation(layout.results_jsonl, "results.jsonl", issues)
    for index, row in enumerate(result_rows_to_validate, start=1):
        label = f"results.jsonl line {index}"
        if row.get("event_type") != "result":
            issues.append(f"{label} event_type must be result")
        task_id = row.get("task_id")
        if task_id not in task_ids:
            issues.append(f"{label} references missing task {task_id}")
        status = row.get("status")
        if status not in RESULT_TASK_STATES:
            issues.append(f"{label} has invalid status {status}")
        if row.get("accepted_by_leader") and status != "done":
            issues.append(f"{label} accepted_by_leader requires status done")
        quality = row.get("quality_score")
        if type(quality) is not int or quality not in {1, 2, 3, 4, 5}:
            issues.append(f"{label} quality_score must be 1-5")
        validate_token_triplet(row, label, issues)
        validate_non_negative_number(row.get("estimated_cost_cny"), f"{label} estimated_cost_cny", issues, allow_none=True)
    leader_work_rows_to_validate = read_rows_for_validation(layout.leader_work_jsonl, "leader-work.jsonl", issues)
    for index, row in enumerate(leader_work_rows_to_validate, start=1):
        label = f"leader-work.jsonl line {index}"
        if row.get("event_type") != "leader_self_work":
            issues.append(f"{label} event_type must be leader_self_work")
        task_id = row.get("task_id")
        if task_id and task_id not in task_ids:
            issues.append(f"{label} references missing task {task_id}")
        if row.get("work_type") not in LEADER_WORK_TYPES:
            issues.append(f"{label} has invalid work_type {row.get('work_type')}")
        if row.get("risk") not in RISKS:
            issues.append(f"{label} has invalid risk {row.get('risk')}")
        if not row.get("scope"):
            issues.append(f"{label} scope is required")
        if not row.get("reason"):
            issues.append(f"{label} reason is required")
        validate_non_negative_integer(row.get("minutes"), f"{label} minutes", issues)
        validate_token_triplet(row, label, issues)
        validate_non_negative_number(row.get("estimated_cost_cny"), f"{label} estimated_cost_cny", issues, allow_none=True)
    _, result_evidence_issues = audit_result_evidence(layout)
    issues.extend(result_evidence_issues)
    usage_rows_to_validate = read_rows_for_validation(layout.usage_jsonl, "usage.jsonl", issues)
    for index, row in enumerate(usage_rows_to_validate, start=1):
        label = f"usage.jsonl line {index}"
        if row.get("event_type") != "usage":
            issues.append(f"{label} event_type must be usage")
        actor_id = row.get("actor_id")
        if actor_id not in actor_ids:
            issues.append(f"{label} references missing actor {actor_id}")
        task_id = row.get("task_id")
        if task_id and task_id not in task_ids:
            issues.append(f"{label} references missing task {task_id}")
        validate_token_triplet(row, label, issues)
        validate_non_negative_number(row.get("estimated_cost_cny"), f"{label} estimated_cost_cny", issues, allow_none=True)
    store = control_store_status(layout)
    if store.get("status") == "invalid":
        issues.extend(f"control store: {issue}" for issue in store.get("issues") or [])
    elif control_store_enabled(layout):
        view_audit = audit_project_views(layout, repair=False)
        issues.extend(
            f"control store compatibility view drift: {path}"
            for path in view_audit.get("drifted") or []
        )
        issues.extend(
            f"control store ghost compatibility view: {path}"
            for path in view_audit.get("ghosts") or []
        )
    return issues


def command_validate(args: Any) -> None:
    layout = resolve_project(args.root, args.project)
    issues = validate_layout(layout)
    status = "ok" if not issues else "invalid"
    print_json({"status": status, "issues": issues})
    if issues:
        raise SystemExit(1)


def _governance_project_file_state(path: Path) -> tuple[int, int, int, int]:
    try:
        info = path.lstat()
    except OSError as exc:
        raise ControlStoreError("governance preflight could not inspect project.json") from exc
    reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    if (
        not stat.S_ISREG(info.st_mode)
        or stat.S_ISLNK(info.st_mode)
        or bool(getattr(info, "st_file_attributes", 0) & reparse_flag)
    ):
        raise ControlStoreError("governance preflight rejected unsafe project.json")
    if info.st_size < 0 or info.st_size > GOVERNANCE_PROJECT_MAX_BYTES:
        raise ControlStoreError("governance preflight rejected oversized project.json")
    return (
        int(info.st_size),
        int(info.st_mtime_ns),
        int(getattr(info, "st_ctime_ns", 0)),
        int(getattr(info, "st_ino", 0)),
    )


def _capture_governance_project(path: Path) -> tuple[tuple[tuple[int, int, int, int], str], bytes]:
    before = _governance_project_file_state(path)
    try:
        payload = path.read_bytes()
    except OSError as exc:
        raise ControlStoreError("governance preflight could not read project.json") from exc
    after = _governance_project_file_state(path)
    if before != after or len(payload) != before[0]:
        raise ControlStoreError("governance preflight project.json changed while read")
    return (before, hashlib.sha256(payload).hexdigest()), payload


def _stable_governance_project_snapshot(layout: ProjectLayout) -> bytes:
    last_error: ControlStoreError | None = None
    for _ in range(GOVERNANCE_SNAPSHOT_ATTEMPTS):
        try:
            first_signature, _ = _capture_governance_project(layout.project_json)
            second_signature, second_payload = _capture_governance_project(layout.project_json)
        except ControlStoreError as exc:
            last_error = exc
            continue
        if first_signature == second_signature:
            return second_payload
        last_error = ControlStoreError(
            "governance preflight project.json was not stable across reads"
        )
    raise last_error or ControlStoreError("governance preflight could not snapshot project.json")


def _load_authoritative_project_for_governance(layout: ProjectLayout) -> dict[str, Any]:
    # project.json is intentionally excluded from the transactional compatibility
    # views, so it remains the authoritative governance source after SQLite
    # cutover. Validate the marker/database relationship read-only, then take a
    # stable double-read without opening the source DB/WAL or acknowledging dirty
    # views.
    try:
        control_store_enabled(layout)
    except ControlStoreError:
        raise
    content = _stable_governance_project_snapshot(layout)
    try:
        project = json.loads(content.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ControlStoreError("governance preflight authoritative project document is invalid") from exc
    if not isinstance(project, dict):
        raise ControlStoreError("governance preflight authoritative project document is invalid")
    return project


def _validate_governance_preflight(layout: ProjectLayout, *, operation: str) -> None:
    try:
        project = _load_authoritative_project_for_governance(layout)
    except ControlStoreError as exc:
        raise GovernancePreflightBlocked(
            f"ArchMarshal governance gate blocked {operation}: authoritative project state is unavailable ({exc})"
        ) from exc
    governance = project.get("governance") or {"mode": "off", "ready": False}
    if not isinstance(governance, dict):
        raise GovernancePreflightBlocked(
            f"ArchMarshal governance gate blocked {operation}: governance state is invalid"
        )
    try:
        enforce_governance_contract(
            governance,
            project.get("workspace"),
            operation=operation,
        )
    except GovernanceError as exc:
        raise GovernancePreflightBlocked(
            f"ArchMarshal governance gate blocked {operation} [{exc.code}]: {exc}"
        ) from exc


def _command_requires_governance_preflight(function: Any, args: Any) -> bool:
    if function.__name__ in GOVERNANCE_PREFLIGHT_COMMANDS:
        return True
    return function.__name__ == "command_send" and bool(
        getattr(args, "runtime_send", False) or getattr(args, "tmux_send", False)
    )


def _locked_project_command(function: Any) -> Any:
    @functools.wraps(function)
    def wrapped(args: Any) -> Any:
        layout = resolve_project(args.root, args.project)
        governance_preflight = _command_requires_governance_preflight(function, args)
        stop_runtime_requested = bool(
            function.__name__ == "command_stop_actor"
            and (getattr(args, "stop_runtime", False) or getattr(args, "kill_window", False))
            and not getattr(args, "dry_run", False)
        )
        # An explicit SQLite-backed stop is always a safety operation.  It must
        # not depend on project.json being readable, nor race a governance mode
        # change between command commit and effect drain.  The post-commit path
        # leases exactly this command's STOP effect and no SPAWN work.
        emergency_stop = bool(stop_runtime_requested and control_store_enabled(layout))
        output = ""
        result: Any = None
        command_id: str | None = None
        try:
            if governance_preflight:
                _validate_governance_preflight(
                    layout,
                    operation=function.__name__.removeprefix("command_").replace("_", "-"),
                )
            with project_write_lock(layout):
                # Nested scheduler commands already share the outer SQLite
                # transaction.  Opening a second reconciliation connection
                # while BEGIN IMMEDIATE is held self-deadlocks the process.
                if current_transaction() is not None:
                    return function(args)
                if governance_preflight:
                    _validate_governance_preflight(
                        layout,
                        operation=function.__name__.removeprefix("command_").replace("_", "-"),
                    )
                if not control_store_enabled(layout):
                    return function(args)
                reconcile_project_views(layout)
                audit_project_views(layout, repair=True)
                external_effect = (
                    (function.__name__ == "command_send" and bool(getattr(args, "runtime_send", False)))
                    or function.__name__ == "command_start_leader"
                    or function.__name__ == "command_preview_changes"
                    or (
                        function.__name__ == "command_apply_changes"
                        and bool(getattr(args, "apply", False))
                    )
                )
                if external_effect:
                    raise SystemExit(
                        "this command has an OS runtime side effect and is blocked after SQLite cutover "
                        "until it can be processed through the recoverable effect outbox"
                    )
                command_id = str(getattr(args, "command_id", None) or new_id("CMD"))
                payload = {
                    key: (str(value) if isinstance(value, Path) else value)
                    for key, value in vars(args).items()
                    if key != "func"
                }
                with control_transaction(
                    layout,
                    command_name=function.__name__,
                    command_id=command_id,
                    payload=payload,
                ) as transaction:
                    if transaction.replay:
                        if transaction.replay_status == "permanent_failed":
                            detail = transaction.replay_error_detail or "external effect failed permanently"
                            code = transaction.replay_error_code or "command_permanent_failed"
                            raise SystemExit(
                                f"Durable command {command_id} failed [{code}]: {detail}"
                            )
                        replay = transaction.replay_result or {}
                        replay_output = replay.get("stdout") if isinstance(replay, dict) else None
                        if replay_output:
                            output = str(replay_output)
                        else:
                            buffer = io.StringIO()
                            with contextlib.redirect_stdout(buffer):
                                print_json({"status": "ok", "idempotent_replay": True, "command_id": command_id})
                            output = buffer.getvalue()
                        if not emergency_stop:
                            if output:
                                print(output, end="" if output.endswith("\n") else "\n")
                            return None
                    else:
                        buffer = io.StringIO()
                        with contextlib.redirect_stdout(buffer):
                            result = function(args)
                        output = buffer.getvalue()
                        transaction.set_result({"stdout": output})
                if not emergency_stop:
                    if output:
                        print(output, end="" if output.endswith("\n") else "\n")
                    return result
            assert command_id is not None
            effect_id = f"EFF-STOP-{command_id}"
            applied = _drain_emergency_stop_effect(layout, effect_id=effect_id)
            try:
                response = json.loads(output) if output else {}
            except json.JSONDecodeError:
                response = {}
            if isinstance(response, dict):
                runtime = response.get("runtime")
                if not isinstance(runtime, dict):
                    runtime = {}
                    response["runtime"] = runtime
                effect_applied = applied.get("status") == "applied"
                runtime.update(
                    {
                        "status": "applied" if effect_applied else "queued",
                        "effect_id": effect_id,
                        "effect_status": applied.get("status"),
                        "emergency_stop": True,
                        "drain_deferred": not effect_applied,
                    }
                )
                if effect_applied:
                    response["actor_status"] = "stopped"
                output = json.dumps(response, ensure_ascii=False, indent=2) + "\n"
            if output:
                print(output, end="" if output.endswith("\n") else "\n")
            return result
        except (ProjectLockTimeout, ControlStoreError) as exc:
            raise SystemExit(str(exc)) from exc

    return wrapped


def _locked_layout_operation(function: Any) -> Any:
    @functools.wraps(function)
    def wrapped(layout: ProjectLayout, *args: Any, **kwargs: Any) -> Any:
        try:
            if function.__name__ == "scheduler_cycle":
                _validate_governance_preflight(layout, operation="scheduler-cycle")
            # SQLite effect leasing/observation is the cross-process writer
            # fence. Holding the legacy project file lock across slow OCI I/O
            # would starve a freshly spawned runner before it can register.
            if function.__name__ == "scheduler_cycle" and control_store_enabled(layout):
                return function(layout, *args, **kwargs)
            with project_write_lock(layout):
                if function.__name__ == "scheduler_cycle":
                    _validate_governance_preflight(layout, operation="scheduler-cycle")
                return function(layout, *args, **kwargs)
        except ProjectLockTimeout as exc:
            raise SystemExit(str(exc)) from exc

    return wrapped


# One OS-backed writer gate protects every read-modify-write control-plane
# command. Nested calls such as escalate -> dispatch are reentrant in-process.
for _command_name in (
    "command_start_leader",
    "command_new_task",
    "command_dispatch",
    "command_escalate",
    "command_send",
    "command_relay",
    "command_heartbeat",
    "command_stop_actor",
    "command_collect",
    "command_preview_changes",
    "command_apply_changes",
    "command_record_result",
    "command_record_leader_work",
    "command_record_usage",
    "command_governance_rebind",
    "command_recover",
):
    globals()[_command_name] = _locked_project_command(globals()[_command_name])

scheduler_cycle = _locked_layout_operation(scheduler_cycle)


def root_from_args(args: Any) -> Path:
    return args.root.resolve() if args.root else default_root()
