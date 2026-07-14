from __future__ import annotations

import contextlib
import io
import json
import os
import sys
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from .mailbox import deliver_outbox_message, inbox_message_ids, send_message
from .paths import ProjectLayout, actor_runtime_name, actor_target, default_root, make_project_id, relpath, resolve_project, slugify
from .session_backend import (
    actor_runtime,
    backend_from_session,
    command_display,
    command_to_string,
    format_actor_command,
    pid_is_alive,
    platform_summary,
    select_backend_kind,
    session_backend_config,
    session_backend_kind,
    session_name as backend_session_name,
)
from .state import (
    ACTOR_STATES,
    ACTIVE_TASK_STATES,
    SCHEMA_VERSION,
    TASK_STATES,
    append_event,
    append_jsonl,
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

PROVIDERS = {"auto", "codex", "longcat"}
LONGCAT_TASK_TYPES = {
    "analysis",
    "documentation",
    "extraction",
    "mechanical",
    "small-edit",
    "summarization",
    "test",
    "verification",
}


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
    return [
        sys.executable,
        str(script),
        "--root",
        str(layout.root),
        "--project",
        str(layout.project_dir),
        "--actor",
        str(actor["id"]),
    ]


def actor_launch_command(layout: ProjectLayout, session: dict[str, Any], actor: dict[str, Any]) -> str | list[str]:
    template = actor.get("command_template")
    if template:
        return format_actor_command(str(template), layout=layout, session=session, actor=actor)
    return default_actor_argv(layout, actor)


def route_provider(task: dict[str, Any], requested: str | None = None) -> str:
    value = str(requested or task.get("provider") or task.get("agent_name") or "auto").lower()
    if value not in PROVIDERS:
        value = "auto"
    if value != "auto":
        return value
    if str(task.get("risk") or "low") == "high":
        return "codex"
    if str(task.get("difficulty") or "normal") == "hard":
        return "codex"
    return "longcat" if str(task.get("task_type") or "analysis") in LONGCAT_TASK_TYPES else "codex"


def provider_defaults(provider: str, model: str | None, profile: str | None) -> tuple[str, str | None]:
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
        "profile": actor.get("profile"),
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
    normalized = path.replace("\\", "/").strip().strip("/")
    normalized = "/".join(part for part in normalized.split("/") if part)
    return normalized.lower() or "."


def paths_conflict(left: str, right: str) -> bool:
    left_norm = normalize_claim_path(left)
    right_norm = normalize_claim_path(right)
    if left_norm == "." or right_norm == ".":
        return True
    return left_norm == right_norm or left_norm.startswith(right_norm + "/") or right_norm.startswith(left_norm + "/")


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
        if claim.get("task_id") == task_id and claim.get("state") == "active":
            claim["actor_id"] = actor_id
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
    if result < 0:
        raise SystemExit(f"{label} must be non-negative")
    return result


def total_tokens(input_tokens: int, output_tokens: int) -> int:
    return input_tokens + output_tokens


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
    workspace = project.get("workspace")
    if workspace:
        lines.extend(
            [
                "",
                "## Workspace",
                f"- `{workspace}`",
                "- Treat claimed and allowed write paths as relative to this workspace unless they are absolute.",
            ]
        )
    source_project = project.get("source_project")
    if source_project:
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
    profile: str | None = None,
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
        "profile": profile,
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
            "- Agent: executes one bounded task through an explicit Codex or LongCat profile and returns structured status/report files.",
            "",
            "## Isolation",
            "- Actors communicate through `scheduler/mailboxes/<actor>/`.",
            "- Agent context is the task brief plus explicitly listed paths.",
            "- Raw transcripts belong in `transcripts/` and are not leader context by default.",
            "- The scheduler relays report paths and state, not raw reasoning.",
            "",
            "## Return Protocol",
            "- Agents return one structured final response; the actor runner writes `completion-report.md` and task status.",
            "- The actor runner records usage and asks the scheduler to collect or escalate the task.",
            "- LongCat escalates to a fresh Codex attempt instead of expanding context or changing write scope on its own.",
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
    ensure_runtime_dirs(layout)
    source_project = Path(args.source_project).expanduser().resolve() if args.source_project else None
    if source_project and not source_project.is_dir():
        raise SystemExit(f"Source project not found: {source_project}")
    workspace_arg = getattr(args, "workspace", None)
    workspace = Path(workspace_arg).expanduser().resolve() if workspace_arg else Path.cwd().resolve()
    if not workspace.is_dir():
        raise SystemExit(f"Workspace not found: {workspace}")
    secrets_arg = getattr(args, "secrets_file", None)
    secrets_file = Path(secrets_arg).expanduser().resolve() if secrets_arg else None
    if secrets_file and not secrets_file.is_file():
        raise SystemExit(f"Secrets file not found: {secrets_file}")
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
        "auto_escalate": bool(getattr(args, "auto_escalate", True)),
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
        profile=getattr(args, "leader_profile", None),
    )
    save_actor(layout, leader)
    sync_actor_summary(layout, leader)
    atomic_write_text(layout.protocol_md, protocol_text())
    append_event(layout, "project_initialized", project_id=project_id, source_project=str(source_project) if source_project else None, backend=backend_kind)
    print_json({"status": "ok", "project": str(project_dir), "project_id": project_id, "session_name": session_name, "backend": backend_kind})


def start_actor(layout: ProjectLayout, actor: dict[str, Any], *, dry_run: bool) -> dict[str, Any]:
    session = load_session(layout)
    backend = backend_from_session(session)
    session_name = backend_session_name(session)
    if not session_name:
        raise SystemExit("v2 session is missing backend.session_name")
    prompt_path = refresh_actor_prompt(layout, actor)
    save_actor(layout, actor)
    sync_actor_summary(layout, actor)
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
            "planned_commands": [command_to_string(argv) for argv in plan],
        }
    launch = backend.start_actor(
        session_name=session_name,
        actor_name=runtime_name,
        command=command,
        cwd=layout.project_dir,
        log_path=layout.transcripts_dir / f"{slugify(actor['id'], 'actor')}.log",
    )
    actor["status"] = "running"
    runtime["started_at"] = now_iso()
    runtime["last_launch_command"] = command_display(command)
    runtime["target"] = launch.get("target")
    runtime["pid"] = launch.get("pid")
    runtime["log_path"] = relpath(Path(launch["log_path"]), layout.project_dir) if launch.get("log_path") else None
    save_actor(layout, actor)
    sync_actor_summary(layout, actor)
    append_event(layout, "actor_started", actor_id=actor["id"], backend=backend.kind, runtime_target=runtime.get("target"), pid=runtime.get("pid"))
    return {
        "actor": actor["id"],
        "dry_run": False,
        "backend": backend.kind,
        "prompt_file": str(prompt_path),
        "commands": launch.get("commands", []),
        "pid": runtime.get("pid"),
        "runtime_target": runtime.get("target"),
    }


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
            f"- Requested provider: {task.get('provider') or 'auto'}",
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
    if not str(args.title or "").strip():
        raise SystemExit("title is required")
    if not str(args.purpose or "").strip():
        raise SystemExit("purpose is required")
    task_id = args.id or next_task_id(layout)
    directory = task_dir(layout, task_id)
    if directory.exists():
        raise SystemExit(f"Task already exists: {task_id}")
    claim_paths = args.claim_path or []
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
        "provider": getattr(args, "provider", "auto"),
        "profile": getattr(args, "profile", None),
        "status": "planned",
        "agent_id": None,
        "agent_name": args.agent,
        "model": args.model,
        "created_at": now_iso(),
        "updated_at": now_iso(),
        "acceptance": args.acceptance or [],
        "allowed_context": args.allowed_context or [],
        "allowed_paths": args.allowed_path or [],
        "claimed_paths": [normalize_claim_path(path) for path in claim_paths],
        "lock_conflict_override": bool(args.allow_lock_conflict),
        "report_path": relpath(directory / "completion-report.md", layout.project_dir),
        "attempts": [],
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


def command_dispatch(args: Any) -> None:
    layout = resolve_project(args.root, args.project)
    require_task(layout, args.task)
    task = load_task(layout, args.task)
    force = bool(getattr(args, "force", False))
    if task.get("status") in {"done", "failed", "cancelled"} and not force:
        raise SystemExit(f"Task is already terminal: {args.task}")
    session = load_session(layout)
    actor_id = args.actor_id or f"agent-{args.task.lower()}"
    if (layout.actors_dir / f"{slugify(actor_id, 'actor')}.json").exists() and not force:
        raise SystemExit(f"Actor already exists: {actor_id}")
    provider = route_provider(task, getattr(args, "provider", None))
    inherited_profile = task.get("profile") if task.get("provider") in {None, "auto", provider} else None
    model, profile = provider_defaults(
        provider,
        getattr(args, "model", None) or task.get("model") or "inherit",
        getattr(args, "profile", None) or inherited_profile,
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
        profile=profile,
    )
    if args.dry_run:
        plan = start_actor(layout, actor, dry_run=True) if args.start else {"planned_commands": []}
        print_json({"status": "ok", "dry_run": True, "actor": actor, "start_plan": plan})
        return
    save_actor(layout, actor)
    sync_actor_summary(layout, actor)
    session = load_session(layout)
    session.setdefault("task_bindings", {})[args.task] = actor_id
    save_session(layout, session)
    task["agent_id"] = actor_id
    task["agent_name"] = args.agent or task.get("agent_name")
    task["model"] = model
    task["provider"] = provider
    task["profile"] = profile
    task.setdefault("attempts", []).append(
        {
            "attempt": len(task.get("attempts") or []) + 1,
            "actor_id": actor_id,
            "provider": provider,
            "profile": profile,
            "model": model,
            "started_at": now_iso() if args.start else None,
            "escalation_reason": getattr(args, "escalation_reason", None),
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
        start_payload = start_actor(layout, actor, dry_run=False)
        task = load_task(layout, args.task)
        set_task_state(layout, task, "running")
    append_event(
        layout,
        "task_dispatched",
        task_id=args.task,
        actor_id=actor_id,
        provider=provider,
        profile=profile,
        model=model,
        started=bool(args.start),
    )
    print_json(
        {
            "status": "ok",
            "task_id": args.task,
            "actor_id": actor_id,
            "prompt_file": str(layout.project_dir / actor.get("prompt_path", "")),
            "started": bool(args.start),
            "start": start_payload,
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
    leader_only = {"create_task", "dispatch_task", "record_result"}
    if command in leader_only and sender != LEADER_ID:
        raise SystemExit(f"{command} may only be issued by the leader")
    if command in {"collect_task", "record_usage", "heartbeat", "stop_actor", "escalate_task"} and sender != LEADER_ID:
        actor = load_actor(layout, sender)
        target_actor = str(command_args.get("actor") or sender)
        if command in {"record_usage", "heartbeat", "stop_actor"} and target_actor != sender:
            raise SystemExit(f"{command} may only target the sender unless issued by the leader")
        if command == "collect_task":
            task_id = str(command_args.get("task") or message.get("task_id") or actor.get("task_id") or "")
            if actor.get("role") == "agent" and task_id and actor.get("task_id") != task_id:
                raise SystemExit(f"agent {sender} cannot collect task {task_id}")
        if command == "escalate_task":
            task_id = str(command_args.get("task") or message.get("task_id") or actor.get("task_id") or "")
            if actor.get("role") != "agent" or actor.get("task_id") != task_id:
                raise SystemExit(f"agent {sender} cannot escalate task {task_id}")
            if actor.get("provider") != "longcat":
                raise SystemExit("only a LongCat worker may auto-escalate to Codex")


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
            profile=command_args.get("profile"),
            acceptance=as_list(command_args.get("acceptance")),
            allowed_context=as_list(command_args.get("allowed_context")),
            allowed_path=as_list(command_args.get("allowed_path")),
            claim_path=as_list(command_args.get("claim_path")),
            allow_lock_conflict=as_bool(command_args.get("allow_lock_conflict")),
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
            profile=command_args.get("profile"),
            command=command_args.get("actor_command") or command_args.get("command_template"),
            start=as_bool(command_args.get("start")),
            dry_run=False,
            force=as_bool(command_args.get("force")),
            escalation_reason=command_args.get("escalation_reason"),
        )
    if command == "escalate_task":
        task_id = str(command_args.get("task") or message.get("task_id") or "")
        return run_cli_helper(
            layout,
            command_escalate,
            task=task_id,
            reason=str(command_args.get("reason") or "LongCat requested escalation"),
            actor_id=command_args.get("actor_id"),
            profile=command_args.get("profile"),
            model=command_args.get("model"),
            start=as_bool(command_args.get("start")),
            dry_run=False,
            force=False,
        )
    if command == "collect_task":
        actor = command_args.get("actor") or (sender if sender != LEADER_ID else None)
        task_id = str(command_args.get("task") or message.get("task_id") or "")
        return run_cli_helper(
            layout,
            command_collect,
            task=task_id,
            actor=actor,
            state=str(command_args.get("state") or "waiting_leader"),
            report=command_args.get("report"),
            summary=command_args.get("summary"),
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
            model=command_args.get("model"),
            input_tokens=int(command_args.get("input_tokens") or 0),
            output_tokens=int(command_args.get("output_tokens") or 0),
            estimated_cost_cny=command_args.get("estimated_cost_cny"),
            summary=command_args.get("summary"),
            note=command_args.get("note"),
        )
    if command == "record_usage":
        return run_cli_helper(
            layout,
            command_record_usage,
            actor=str(command_args.get("actor") or sender),
            task=command_args.get("task") or message.get("task_id"),
            model=command_args.get("model"),
            input_tokens=int(command_args.get("input_tokens") or 0),
            output_tokens=int(command_args.get("output_tokens") or 0),
            estimated_cost_cny=command_args.get("estimated_cost_cny"),
            note=command_args.get("note"),
        )
    if command == "heartbeat":
        return run_cli_helper(
            layout,
            command_heartbeat,
            actor=str(command_args.get("actor") or sender),
            status=str(command_args.get("status") or "running"),
            note=command_args.get("note"),
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
    for offset, message in enumerate(pending, start=start_line + 1):
        try:
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


def scheduler_cycle(layout: ProjectLayout, *, relay_limit: int | None = None, command_limit: int | None = None, dry_run: bool = False) -> dict[str, Any]:
    relays: list[dict[str, Any]] = []
    for actor in actor_rows(layout):
        payload = relay_actor_outbox(layout, actor_id=actor["id"], limit=relay_limit, dry_run=dry_run)
        if payload["processed"] or payload["delivered"] or payload["skipped"]:
            relays.append(payload)
    commands = process_scheduler_inbox(layout, limit=command_limit, dry_run=dry_run)
    changed = bool(relays or commands["processed"] or commands["failed"])
    if not dry_run:
        state = load_scheduler_state(layout)
        state["status"] = "running"
        state["pid"] = os.getpid()
        state["heartbeat_at"] = now_iso()
        state["last_cycle_at"] = now_iso()
        state["cycle_count"] = int(state.get("cycle_count") or 0) + 1
        state["processed_commands"] = int(state.get("processed_commands") or 0) + len(commands["processed"])
        save_scheduler_state(layout, state)
        append_event(
            layout,
            "scheduler_cycle",
            relayed_actor_count=len(relays),
            processed_command_count=len(commands["processed"]),
            failed_command_count=len(commands["failed"]),
            changed=changed,
        )
    return {
        "status": "ok" if commands["status"] == "ok" else "degraded",
        "changed": changed,
        "relays": relays,
        "commands": commands,
    }


def command_run_scheduler(args: Any) -> None:
    layout = resolve_project(args.root, args.project)
    ensure_runtime_dirs(layout)
    ensure_mailbox(layout, SCHEDULER_ID)
    started_at = now_iso()
    if not args.dry_run:
        update_scheduler_state(
            layout,
            status="running",
            pid=os.getpid(),
            started_at=started_at,
            heartbeat_at=started_at,
            last_cycle_at=None,
        )
    interval = max(float(args.interval), 0.05)
    max_cycles = 1 if args.once else int(args.max_cycles or 0)
    cycles: list[dict[str, Any]] = []
    completed_status = "idle" if args.once or max_cycles else "stopped"
    try:
        while True:
            cycle = scheduler_cycle(layout, relay_limit=args.relay_limit, command_limit=args.command_limit, dry_run=bool(args.dry_run))
            cycles.append(cycle)
            if args.once:
                break
            if max_cycles and len(cycles) >= max_cycles:
                break
            time.sleep(interval)
    except KeyboardInterrupt:
        completed_status = "stopped"
    finally:
        if not args.dry_run:
            update_scheduler_state(
                layout,
                status=completed_status,
                pid=os.getpid(),
                heartbeat_at=now_iso(),
                stopped_at=now_iso() if completed_status == "stopped" else None,
            )
    print_json(
        {
            "status": "ok" if all(cycle["status"] == "ok" for cycle in cycles) else "degraded",
            "project": str(layout.project_dir),
            "cycles": len(cycles),
            "changed_cycles": sum(1 for cycle in cycles if cycle["changed"]),
            "processed_commands": sum(len(cycle["commands"]["processed"]) for cycle in cycles),
            "failed_commands": sum(len(cycle["commands"]["failed"]) for cycle in cycles),
            "scheduler_state": load_scheduler_state(layout),
        }
    )


def command_escalate(args: Any) -> None:
    layout = resolve_project(args.root, args.project)
    require_task(layout, args.task)
    task = load_task(layout, args.task)
    attempts = task.get("attempts") or []
    if attempts and attempts[-1].get("provider") == "codex" and not getattr(args, "force", False):
        raise SystemExit(f"Task is already assigned to Codex: {args.task}")
    next_attempt = len(attempts) + 1
    actor_id = getattr(args, "actor_id", None) or f"agent-{args.task.lower()}-codex-{next_attempt}"
    append_event(layout, "task_escalation_requested", task_id=args.task, actor_id=actor_id, reason=args.reason)
    command_dispatch(
        SimpleNamespace(
            root=args.root,
            project=args.project,
            task=args.task,
            actor_id=actor_id,
            agent="codex",
            provider="codex",
            profile=getattr(args, "profile", None),
            model=getattr(args, "model", None) or "inherit",
            command=None,
            start=bool(getattr(args, "start", False)),
            dry_run=bool(getattr(args, "dry_run", False)),
            force=True,
            escalation_reason=args.reason,
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
        else:
            runtime_payload = {"backend": backend.kind, **backend.stop_actor(target=target, pid=pid)}
    if args.dry_run:
        print_json({"status": "ok", "dry_run": True, "actor": args.actor, "runtime": runtime_payload})
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


def command_collect(args: Any) -> None:
    layout = resolve_project(args.root, args.project)
    if args.state and args.state not in TASK_STATES:
        raise SystemExit(f"Invalid task state: {args.state}")
    require_task(layout, args.task)
    task = load_task(layout, args.task)
    actor_id = args.actor or task.get("agent_id")
    report_path = Path(args.report).expanduser().resolve() if args.report else task_dir(layout, args.task) / "completion-report.md"
    if args.state in {"done", "failed", "escalate", "waiting_leader"} and not report_path.is_file():
        raise SystemExit(f"Report file not found: {report_path}")
    task["report_path"] = relpath(report_path, layout.project_dir)
    task["collected_at"] = now_iso()
    if args.summary:
        task["summary"] = compact_text(args.summary)
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


def command_record_result(args: Any) -> None:
    layout = resolve_project(args.root, args.project)
    require_task(layout, args.task)
    if args.status not in RESULT_TASK_STATES:
        raise SystemExit(f"record-result status must be one of: {', '.join(sorted(RESULT_TASK_STATES))}")
    if args.accepted_by_leader and args.status != "done":
        raise SystemExit("--accepted-by-leader requires --status done")
    if int(args.quality_score) not in {1, 2, 3, 4, 5}:
        raise SystemExit("quality-score must be 1-5")
    input_tokens = require_non_negative_int(args.input_tokens, "input-tokens")
    output_tokens = require_non_negative_int(args.output_tokens, "output-tokens")
    estimated_cost_cny = require_non_negative_float(args.estimated_cost_cny, "estimated-cost-cny")
    task = load_task(layout, args.task)
    project = load_project(layout)
    actor_id = args.actor or task.get("agent_id")
    agent_name = args.agent or task.get("agent_name") or actor_id or "unknown"
    model = args.model or task.get("model") or "inherit"
    row = {
        "id": new_id("RES"),
        "event_type": "result",
        "timestamp": now_iso(),
        "project_id": project.get("project_id"),
        "task_id": args.task,
        "actor_id": actor_id,
        "agent": agent_name,
        "provider": task.get("provider"),
        "profile": task.get("profile"),
        "model": model,
        "task_type": task.get("task_type") or "unknown",
        "status": args.status,
        "completed": args.status == "done",
        "needs_escalation": args.status == "escalate",
        "accepted_by_leader": bool(args.accepted_by_leader),
        "quality_score": args.quality_score,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "total_tokens": total_tokens(input_tokens, output_tokens),
        "estimated_cost_cny": estimated_cost_cny,
        "cost_source": cost_source(estimated_cost_cny),
        "summary": compact_text(args.summary) if args.summary else "",
        "note": args.note or "",
        "report_path": task.get("report_path"),
    }
    append_jsonl(layout.results_jsonl, row)
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
    output_tokens = require_non_negative_int(args.output_tokens, "output-tokens")
    minutes = require_non_negative_int(args.minutes, "minutes")
    estimated_cost_cny = require_non_negative_float(args.estimated_cost_cny, "estimated-cost-cny")
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
        "output_tokens": output_tokens,
        "total_tokens": total_tokens(input_tokens, output_tokens),
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
    actor = load_actor(layout, args.actor)
    task_id = args.task or actor.get("task_id")
    if task_id:
        require_task(layout, task_id)
    input_tokens = require_non_negative_int(args.input_tokens, "input-tokens")
    output_tokens = require_non_negative_int(args.output_tokens, "output-tokens")
    estimated_cost_cny = require_non_negative_float(args.estimated_cost_cny, "estimated-cost-cny")
    project = load_project(layout)
    row = {
        "id": new_id("USG"),
        "event_type": "usage",
        "timestamp": now_iso(),
        "project_id": project.get("project_id"),
        "actor_id": args.actor,
        "role": actor.get("role"),
        "agent": actor.get("agent_name") or ("leader" if actor.get("role") == "leader" else args.actor),
        "task_id": task_id,
        "provider": actor.get("provider"),
        "profile": actor.get("profile"),
        "model": args.model or actor.get("model") or "inherit",
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "total_tokens": total_tokens(input_tokens, output_tokens),
        "estimated_cost_cny": estimated_cost_cny,
        "cost_source": cost_source(estimated_cost_cny),
        "note": args.note or "",
    }
    append_jsonl(layout.usage_jsonl, row)
    actor_usage = actor.setdefault("usage", {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0, "estimated_cost_cny": 0.0, "unknown_cost_count": 0})
    actor_usage["input_tokens"] = int(actor_usage.get("input_tokens") or 0) + input_tokens
    actor_usage["output_tokens"] = int(actor_usage.get("output_tokens") or 0) + output_tokens
    actor_usage["total_tokens"] = int(actor_usage.get("total_tokens") or 0) + row["total_tokens"]
    if estimated_cost_cny is None:
        actor_usage["unknown_cost_count"] = int(actor_usage.get("unknown_cost_count") or 0) + 1
    else:
        actor_usage["estimated_cost_cny"] = round(float(actor_usage.get("estimated_cost_cny") or 0.0) + estimated_cost_cny, 6)
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


def leader_work_rows(layout: ProjectLayout) -> list[dict[str, Any]]:
    return read_jsonl(layout.leader_work_jsonl)


def usage_rows(layout: ProjectLayout) -> list[dict[str, Any]]:
    return read_jsonl(layout.usage_jsonl)


def empty_token_bucket() -> dict[str, Any]:
    return {
        "input_tokens": 0,
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
        "output_tokens": output_tokens,
        "total_tokens": total_token_count,
        "estimated_cost_cny": round(estimated_cost_cny, 6),
        "unknown_cost_count": unknown_cost_count,
        "latest_events": rows[-5:],
    }


def summarize_leader_self_work(rows: list[dict[str, Any]]) -> dict[str, Any]:
    total_minutes = 0
    input_tokens = 0
    output_tokens = 0
    total_token_count = 0
    estimated_cost_cny = 0.0
    unknown_cost_count = 0
    by_type: dict[str, int] = {}
    by_risk: dict[str, int] = {}
    for row in rows:
        total_minutes += int(row.get("minutes") or 0)
        input_tokens += int(row.get("input_tokens") or 0)
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
    output_tokens = 0
    total_token_count = 0
    estimated_cost_cny = 0.0
    unknown_cost_count = 0
    by_actor: dict[str, dict[str, Any]] = {}
    for row in rows:
        input_value = int(row.get("input_tokens") or 0)
        output_value = int(row.get("output_tokens") or 0)
        total_value = int(row.get("total_tokens") or 0)
        input_tokens += input_value
        output_tokens += output_value
        total_token_count += total_value
        actor_id = row.get("actor_id") or "unknown"
        bucket = by_actor.setdefault(actor_id, {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0, "estimated_cost_cny": 0.0, "unknown_cost_count": 0})
        bucket["input_tokens"] += input_value
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
                "attempts": task.get("attempts") or [],
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
            "| Agent Actor | Agent | Input | Output | Total | Cost CNY | Source |",
            "| --- | --- | ---: | ---: | ---: | ---: | --- |",
        ]
    )
    agent_rows = [row for row in payload["processes"] if row.get("role") == "agent"]
    if agent_rows:
        for row in agent_rows:
            tokens = row.get("token_usage") or empty_token_bucket()
            lines.append(
                f"| {row.get('id')} | {row.get('agent_name') or row.get('id')} | {tokens.get('input_tokens', 0)} | {tokens.get('output_tokens', 0)} | {tokens.get('total_tokens', 0)} | {tokens.get('estimated_cost_cny', 0.0)} | {tokens.get('source') or 'none'} |"
            )
    else:
        lines.append("| - | - | 0 | 0 | 0 | 0.0 | none |")
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
            f"- Tokens: in {results['input_tokens']} / out {results['output_tokens']} / total {results['total_tokens']}",
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
            f"- Tokens: in {usage['input_tokens']} / out {usage['output_tokens']} / total {usage['total_tokens']}",
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
            f"- Tokens: in {leader_work['input_tokens']} / out {leader_work['output_tokens']} / total {leader_work['total_tokens']}",
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
    ensure_runtime_dirs(layout)
    session = load_session(layout)
    backend = backend_from_session(session)
    session_name = backend_session_name(session)
    issues: list[str] = []
    planned_restarts: list[str] = []
    restarted: list[str] = []
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
            )
            if actor.get("status") == "running" and not is_alive:
                issues.append(f"running actor missing runtime: {actor['id']}")
                actor_data = load_actor(layout, actor["id"])
                actor_data["status"] = "needs_recovery"
                save_actor(layout, actor_data)
                sync_actor_summary(layout, actor_data)
                if args.plan_restarts:
                    command = actor_launch_command(layout, session, actor_data)
                    plan = backend.start_plan(session_name=session_name, actor_name=runtime_name, command=command, session_exists=True)
                    planned_restarts.extend(command_to_string(argv) for argv in plan)
                if args.restart_missing:
                    actor_data["status"] = "starting"
                    save_actor(layout, actor_data)
                    sync_actor_summary(layout, actor_data)
                    start_payload = start_actor(layout, actor_data, dry_run=False)
                    restarted.extend(start_payload.get("commands", []))
    status = "ok" if not issues else "degraded"
    session = load_session(layout)
    session["recovery"] = {"last_recovered_at": now_iso(), "last_status": status, "issues": issues}
    save_session(layout, session)
    append_event(layout, "recovery_audit", status=status, issue_count=len(issues))
    print_json({"status": status, "issues": issues, "planned_restarts": planned_restarts, "restarted": restarted})


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
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        issues.append(f"{label} must be numeric")
        return
    if numeric < 0:
        issues.append(f"{label} must be non-negative")


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
    for task in task_rows(layout):
        if task.get("agent_id") and task["agent_id"] not in actor_ids:
            issues.append(f"{task['id']} references missing actor {task['agent_id']}")
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
        validate_non_negative_number(row.get("input_tokens"), f"{label} input_tokens", issues)
        validate_non_negative_number(row.get("output_tokens"), f"{label} output_tokens", issues)
        validate_non_negative_number(row.get("total_tokens"), f"{label} total_tokens", issues)
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
        validate_non_negative_number(row.get("minutes"), f"{label} minutes", issues)
        validate_non_negative_number(row.get("input_tokens"), f"{label} input_tokens", issues)
        validate_non_negative_number(row.get("output_tokens"), f"{label} output_tokens", issues)
        validate_non_negative_number(row.get("total_tokens"), f"{label} total_tokens", issues)
        validate_non_negative_number(row.get("estimated_cost_cny"), f"{label} estimated_cost_cny", issues, allow_none=True)
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
        validate_non_negative_number(row.get("input_tokens"), f"{label} input_tokens", issues)
        validate_non_negative_number(row.get("output_tokens"), f"{label} output_tokens", issues)
        validate_non_negative_number(row.get("total_tokens"), f"{label} total_tokens", issues)
        validate_non_negative_number(row.get("estimated_cost_cny"), f"{label} estimated_cost_cny", issues, allow_none=True)
    return issues


def command_validate(args: Any) -> None:
    layout = resolve_project(args.root, args.project)
    issues = validate_layout(layout)
    status = "ok" if not issues else "invalid"
    print_json({"status": status, "issues": issues})
    if issues:
        raise SystemExit(1)


def root_from_args(args: Any) -> Path:
    return args.root.resolve() if args.root else default_root()
