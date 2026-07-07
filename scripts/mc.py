#!/usr/bin/env python3
"""Deterministic state manager for the CostMarshal skill.

This script keeps the fragile parts out of model prose:
- global agent memory
- per-project directories
- branch trees
- task cards and status files
- project/global performance records
- project startup connectivity checks
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


SCHEMA_VERSION = 1
DEFAULT_PRICING_CNY_PER_1M = {
    "deepseek": {"input": 1.0, "output": 2.0},
    "kimi": {"input": 1.3, "output": 6.5},
    "longcat": {"input": 0.165, "output": 0.165},
}
DEFAULT_BASE_URLS = {
    "deepseek": "https://api.deepseek.com",
    "kimi": "https://api.moonshot.cn/v1",
    "longcat": "https://api.longcat.chat/openai",
}
SIGNAL_BY_STATE = {
    "done": "DONE",
    "failed": "FAILED",
    "escalate": "ESCALATE",
}
TASK_STATES = {"planned", "running", "done", "failed", "escalate", "cancelled"}
DIFFICULTIES = {"S", "A", "B", "C"}
RISKS = {"high", "medium", "low"}
TASK_TYPES = {
    "architecture",
    "implementation",
    "analysis",
    "review",
    "mechanical",
    "summarization",
    "verification",
    "research-intake",
    "research-ideate",
    "research-execute",
    "research-evaluate",
    "research-search",
    "research-report",
}
MEMORY_FEEDBACK_ATTRIBUTIONS = {
    "memory_issue",
    "agent_capability",
    "task_mismatch",
    "environment_issue",
    "unknown",
}
TERMINAL_TASK_STATES = {"done", "failed", "escalate", "cancelled"}
ACTIVE_CLAIM_STATES = {"planned", "running"}


def now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def default_root() -> Path:
    env_root = (
        os.environ.get("COSTMARSHAL_HOME")
        or os.environ.get("MMC_HOME")
        or os.environ.get("MULTI_MODEL_CONDUCTOR_HOME")
    )
    if env_root:
        return Path(env_root).expanduser().resolve()
    codex_home = os.environ.get("CODEX_HOME")
    if codex_home:
        return (Path(codex_home).expanduser() / "costmarshal").resolve()
    return (Path.home() / ".codex" / "costmarshal").resolve()


def slugify(text: str, fallback: str = "project") -> str:
    text = text.strip().lower()
    text = re.sub(r"[^a-z0-9._-]+", "-", text)
    text = re.sub(r"-+", "-", text).strip("-._")
    return text[:64] or fallback


def redact(value: Any) -> Any:
    if isinstance(value, str):
        value = re.sub(r"(?<![A-Za-z0-9])sk-[A-Za-z0-9_\-]{8,}", "sk-REDACTED", value)
        value = re.sub(r"(?<![A-Za-z0-9])ak_[A-Za-z0-9_\-]{8,}", "ak_REDACTED", value)
        return value
    if isinstance(value, list):
        return [redact(item) for item in value]
    if isinstance(value, dict):
        return {key: redact(item) for key, item in value.items()}
    return value


def parse_env_file(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for raw_line in path.read_text(encoding="utf-8-sig").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key:
            values[key] = value
    return values


def candidate_secret_files(root: Path, explicit: Path | None = None) -> list[Path]:
    candidates: list[Path] = []
    if explicit:
        candidates.append(explicit.expanduser())
    env_secret = os.environ.get("COSTMARSHAL_SECRETS_FILE")
    if env_secret:
        candidates.append(Path(env_secret).expanduser())

    codex_home = Path(os.environ["CODEX_HOME"]).expanduser() if os.environ.get("CODEX_HOME") else None
    home = Path.home()
    skill_file = Path(__file__).resolve()
    inferred_codex_home = skill_file.parents[3] if len(skill_file.parents) >= 4 else None

    for base in [root, codex_home, inferred_codex_home, home / ".codex", Path.cwd()]:
        if not base:
            continue
        candidates.extend(
            [
                base / "config" / "secrets.env",
                base / "secrets.env",
                base / ".sandbox-secrets" / "costmarshal.env",
            ]
        )

    unique: list[Path] = []
    seen: set[str] = set()
    for path in candidates:
        resolved = str(path.expanduser().resolve()) if path.exists() else str(path.expanduser())
        if resolved not in seen:
            seen.add(resolved)
            unique.append(path.expanduser())
    return unique


def load_local_secrets(root: Path, explicit: Path | None = None) -> dict[str, Any]:
    loaded_files: list[str] = []
    loaded_names: set[str] = set()
    checked_files: list[str] = []
    for path in candidate_secret_files(root, explicit):
        checked_files.append(str(path))
        if not path.exists() or not path.is_file():
            continue
        values = parse_env_file(path)
        for key, value in values.items():
            if key not in os.environ:
                os.environ[key] = value
                loaded_names.add(key)
        loaded_files.append(str(path.resolve()))
    return {
        "loaded_files": loaded_files,
        "loaded_env_names": sorted(loaded_names),
        "checked_files": checked_files,
    }


def read_json(path: Path, default: Any | None = None) -> Any:
    if not path.exists():
        if default is not None:
            return default
        raise FileNotFoundError(path)
    return json.loads(path.read_text(encoding="utf-8"))


def atomic_write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", delete=False, dir=str(path.parent)) as handle:
        handle.write(content)
        temp_name = handle.name
    os.replace(temp_name, path)


def atomic_write_json(path: Path, data: Any) -> None:
    atomic_write_text(path, json.dumps(redact(data), ensure_ascii=False, indent=2) + "\n")


def append_jsonl(path: Path, row: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(redact(row), ensure_ascii=False, sort_keys=True) + "\n")


def touch(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(now_iso() + "\n", encoding="utf-8")


def parse_duration(value: str | None) -> int:
    if not value:
        return 0
    match = re.match(r"^(\d+(?:\.\d+)?)(ms|s|m|h|d)?$", value.strip().lower())
    if not match:
        raise SystemExit(f"Invalid duration: {value}. Use values like 30s, 10m, 1h, or 600.")
    amount = float(match.group(1))
    unit = match.group(2) or "s"
    scale = {"ms": 0.001, "s": 1, "m": 60, "h": 3600, "d": 86400}[unit]
    seconds = amount * scale
    return max(1, int(seconds + 0.999))


def quiet_wait(predicate: Any, every_seconds: int, timeout_seconds: int) -> bool:
    deadline = time.time() + timeout_seconds
    while time.time() < deadline:
        if predicate():
            return True
        remaining = max(0, int(deadline - time.time()))
        time.sleep(min(every_seconds, remaining))
    return bool(predicate())


def print_wait_event(action: str, state: str, **extra: Any) -> None:
    payload = {"action": action, "state": state, "timestamp": now_iso(), **extra}
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True))


def format_seconds(seconds: int | float | None) -> str:
    if seconds is None:
        return "-"
    total = int(float(seconds))
    if total < 60:
        return f"{total}s"
    minutes, sec = divmod(total, 60)
    if minutes < 60:
        return f"{minutes}m {sec}s" if sec else f"{minutes}m"
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h {minutes}m" if minutes else f"{hours}h"


def command_sleep(args: argparse.Namespace) -> None:
    seconds = args.seconds or parse_duration(args.duration)
    if seconds <= 0:
        raise SystemExit("sleep requires --duration or --seconds.")
    time.sleep(seconds)
    print(now_iso())


def command_wait_file(args: argparse.Namespace) -> None:
    every_seconds = parse_duration(args.every)
    timeout_seconds = parse_duration(args.timeout)
    target = args.path.expanduser()
    started = time.time()
    print_wait_event("wait-file", "start", path=str(target), every_seconds=every_seconds, timeout_seconds=timeout_seconds)
    ok = quiet_wait(lambda: target.exists(), every_seconds, timeout_seconds)
    print_wait_event(
        "wait-file",
        "ready" if ok else "timeout",
        path=str(target),
        elapsed_seconds=round(time.time() - started, 3),
    )
    if not ok:
        raise SystemExit(124)


def command_wait_contains(args: argparse.Namespace) -> None:
    every_seconds = parse_duration(args.every)
    timeout_seconds = parse_duration(args.timeout)
    target = args.path.expanduser()
    text = args.text
    started = time.time()

    def contains_text() -> bool:
        if not target.is_file():
            return False
        return text in target.read_text(encoding="utf-8", errors="ignore")

    print_wait_event("wait-contains", "start", path=str(target), every_seconds=every_seconds, timeout_seconds=timeout_seconds)
    ok = quiet_wait(contains_text, every_seconds, timeout_seconds)
    print_wait_event(
        "wait-contains",
        "ready" if ok else "timeout",
        path=str(target),
        elapsed_seconds=round(time.time() - started, 3),
    )
    if not ok:
        raise SystemExit(124)


def command_wait_command(args: argparse.Namespace) -> None:
    every_seconds = parse_duration(args.every)
    timeout_seconds = parse_duration(args.timeout)
    started = time.time()

    def command_ok() -> bool:
        try:
            result = subprocess.run(
                args.command,
                shell=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=max(1, every_seconds),
            )
            return result.returncode == 0
        except subprocess.TimeoutExpired:
            return False

    print_wait_event("wait-command", "start", every_seconds=every_seconds, timeout_seconds=timeout_seconds)
    ok = quiet_wait(command_ok, every_seconds, timeout_seconds)
    print_wait_event("wait-command", "ready" if ok else "timeout", elapsed_seconds=round(time.time() - started, 3))
    if not ok:
        raise SystemExit(124)


def command_wait_task(args: argparse.Namespace) -> None:
    root = args.root.resolve()
    project_dir = project_path(root, args.project)
    task_dir = project_dir / "tasks" / args.task
    every_seconds = parse_duration(args.every)
    timeout_seconds = parse_duration(args.timeout)
    terminal_states = {state.strip() for state in args.states.split(",") if state.strip()}
    if not terminal_states:
        raise SystemExit("wait-task requires at least one state in --states.")
    signal_names = {"done": "DONE", "failed": "FAILED", "escalate": "ESCALATE"}
    started = time.time()

    def task_ready() -> bool:
        status = read_json(task_dir / "status.json", {})
        state = status.get("state")
        if state in terminal_states:
            return True
        return any((task_dir / signal_names[state]).exists() for state in terminal_states if state in signal_names)

    print_wait_event(
        "wait-task",
        "start",
        project=str(project_dir),
        task=args.task,
        states=sorted(terminal_states),
        every_seconds=every_seconds,
        timeout_seconds=timeout_seconds,
    )
    ok = quiet_wait(task_ready, every_seconds, timeout_seconds)
    final_status = read_json(task_dir / "status.json", {})
    elapsed_seconds = round(time.time() - started, 3)
    event = {
        "timestamp": now_iso(),
        "action": "wait-task",
        "project_id": load_project(project_dir).get("id"),
        "task": args.task,
        "states": sorted(terminal_states),
        "every_seconds": every_seconds,
        "timeout_seconds": timeout_seconds,
        "elapsed_seconds": elapsed_seconds,
        "result": "ready" if ok else "timeout",
        "task_state": final_status.get("state"),
    }
    append_jsonl(project_dir / "memory" / "wait-events.jsonl", event)
    print_wait_event(
        "wait-task",
        "ready" if ok else "timeout",
        task=args.task,
        task_state=final_status.get("state"),
        elapsed_seconds=elapsed_seconds,
    )
    if not ok:
        raise SystemExit(124)


def default_agents_config() -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "updated_at": now_iso(),
        "agents": {
            "senior": {
                "tier": "high",
                "kind": "codex-subagent",
                "model": "inherit",
                "required_env": [],
                "base_url_env": None,
                "enabled": True,
                "capabilities": ["architecture", "implementation", "review", "rescue"],
            },
            "deepseek": {
                "tier": "medium",
                "kind": "openai-compatible",
                "model": "deepseek-v4-flash",
                "base_url": DEFAULT_BASE_URLS["deepseek"],
                "pricing_cny_per_1m": DEFAULT_PRICING_CNY_PER_1M["deepseek"],
                "model_env": "DEEPSEEK_MODEL",
                "temperature": 0,
                "temperature_env": "DEEPSEEK_TEMPERATURE",
                "required_env": ["DEEPSEEK_API_KEY"],
                "base_url_env": "DEEPSEEK_BASE_URL",
                "enabled": True,
                "capabilities": ["analysis", "implementation", "verification"],
            },
            "kimi": {
                "tier": "medium",
                "kind": "openai-compatible",
                "model": "kimi-k2.7-code",
                "base_url": DEFAULT_BASE_URLS["kimi"],
                "pricing_cny_per_1m": DEFAULT_PRICING_CNY_PER_1M["kimi"],
                "model_env": "KIMI_MODEL",
                "temperature": 1,
                "temperature_env": "KIMI_TEMPERATURE",
                "required_env": ["MOONSHOT_API_KEY"],
                "base_url_env": "MOONSHOT_BASE_URL",
                "enabled": True,
                "capabilities": ["implementation", "review", "patch-plan"],
            },
            "longcat": {
                "tier": "low",
                "kind": "openai-compatible",
                "model": "LongCat-2.0",
                "base_url": DEFAULT_BASE_URLS["longcat"],
                "pricing_cny_per_1m": DEFAULT_PRICING_CNY_PER_1M["longcat"],
                "model_env": "LONGCAT_MODEL",
                "temperature": 0,
                "temperature_env": "LONGCAT_TEMPERATURE",
                "required_env": ["LONGCAT_API_KEY"],
                "base_url_env": "LONGCAT_BASE_URL",
                "enabled": True,
                "capabilities": ["mechanical", "summarization", "extraction"],
            },
        },
    }


def empty_memory() -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "updated_at": now_iso(),
        "agents": {},
    }


def empty_knowledge_index() -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "updated_at": now_iso(),
        "retrieval_policy": {
            "default": "Read this small index first; attach at most one matching knowledge file unless the leader approves more.",
            "match_order": ["task_type", "kind", "source quality", "recency"],
        },
        "categories": {},
    }


def ensure_root(root: Path) -> None:
    (root / "projects").mkdir(parents=True, exist_ok=True)
    (root / "memory").mkdir(parents=True, exist_ok=True)
    (root / "memory" / "knowledge").mkdir(parents=True, exist_ok=True)
    (root / "config").mkdir(parents=True, exist_ok=True)
    config_path = root / "config" / "agents.json"
    memory_path = root / "memory" / "agent-memory.json"
    events_path = root / "memory" / "events.jsonl"
    evolution_events_path = root / "memory" / "evolution-events.jsonl"
    knowledge_index_path = root / "memory" / "knowledge-index.json"
    if not config_path.exists():
        atomic_write_json(config_path, default_agents_config())
    if not memory_path.exists():
        atomic_write_json(memory_path, empty_memory())
    if not events_path.exists():
        events_path.touch()
    if not evolution_events_path.exists():
        evolution_events_path.touch()
    if not knowledge_index_path.exists():
        atomic_write_json(knowledge_index_path, empty_knowledge_index())


def project_path(root: Path, project_arg: str) -> Path:
    path = Path(project_arg).expanduser()
    if path.exists():
        return path.resolve()
    matches = sorted((root / "projects").glob(f"*{project_arg}*"))
    if len(matches) == 1:
        return matches[0].resolve()
    if not matches:
        raise SystemExit(f"Project not found: {project_arg}")
    raise SystemExit("Project is ambiguous:\n" + "\n".join(str(path) for path in matches))


def load_project(path: Path) -> dict[str, Any]:
    return read_json(path / "project.json")


def save_project(path: Path, data: dict[str, Any]) -> None:
    data["updated_at"] = now_iso()
    atomic_write_json(path / "project.json", data)


def render_branch_tree(tree: dict[str, Any]) -> str:
    nodes = {node["id"]: node for node in tree.get("nodes", [])}
    root_id = tree.get("root_id", "root")
    lines = [f"# Branch Tree: {tree.get('project_id', 'unknown')}", ""]

    def walk(node_id: str, depth: int) -> None:
        node = nodes[node_id]
        prefix = "  " * depth + "- "
        agent = f" agent={node.get('agent')}" if node.get("agent") else ""
        state = node.get("state") or node.get("status") or "unknown"
        lines.append(f"{prefix}`{node_id}` {node.get('title', '')} [{state}]{agent}")
        for child_id in node.get("children", []):
            if child_id in nodes:
                walk(child_id, depth + 1)

    if root_id in nodes:
        walk(root_id, 0)
    else:
        lines.append("- missing root")
    lines.append("")
    return "\n".join(lines)


def load_tree(project_dir: Path) -> dict[str, Any]:
    return read_json(project_dir / "branch-tree.json")


def save_tree(project_dir: Path, tree: dict[str, Any]) -> None:
    tree["updated_at"] = now_iso()
    atomic_write_json(project_dir / "branch-tree.json", tree)
    atomic_write_text(project_dir / "branch-tree.md", render_branch_tree(tree))


def set_task_status(
    project_dir: Path,
    task: str,
    state: str,
    confidence: str | None = None,
    error: str | None = None,
    model: str | None = None,
) -> dict[str, Any]:
    task_dir = project_dir / "tasks" / task
    status_path = task_dir / "status.json"
    status = read_json(status_path)
    if state == "running":
        verify_dependencies_ready(project_dir, task)
    previous = status.get("state")
    status["state"] = state
    status["updated_at"] = now_iso()
    if confidence:
        status["confidence"] = confidence
    if error:
        status["error"] = redact(error)
    if model:
        status["model"] = redact(model)
    if state == "running" and not status.get("started_at"):
        status["started_at"] = now_iso()
    if state == "running":
        update_claim_state(project_dir, task, "running")
    if state in {"failed", "escalate"}:
        status["needs_escalation"] = True
    atomic_write_json(status_path, status)
    signal = SIGNAL_BY_STATE.get(state)
    if signal:
        touch(task_dir / signal)
    if state in TERMINAL_TASK_STATES:
        release_claims(project_dir, task, state)
    tree = load_tree(project_dir)
    for node in tree["nodes"]:
        if node["id"] == task:
            node["state"] = state
    save_tree(project_dir, tree)
    return {"task": task, "previous": previous, "state": state}


def next_task_id(project_dir: Path) -> str:
    tasks_dir = project_dir / "tasks"
    existing = []
    for path in list(tasks_dir.glob("CM-*")) + list(tasks_dir.glob("MC-*")):
        match = re.match(r"(?:CM|MC)-(\d+)", path.name)
        if match:
            existing.append(int(match.group(1)))
    return f"CM-{(max(existing) if existing else 0) + 1:04d}"


def project_structure(project_dir: Path, kind: str) -> None:
    for rel in [
        "tasks",
        "memory",
        "reports",
        "raw",
        "artifacts",
        "runbooks",
        "memory/replay",
        "handoffs",
        "locks",
        "checks",
    ]:
        (project_dir / rel).mkdir(parents=True, exist_ok=True)
    if kind in {"arbor", "feynman", "autoresearch", "research"}:
        for rel in [
            "research/idea-tree",
            "research/evals",
            "research/runs",
            "research/literature",
            "research/reports",
        ]:
            (project_dir / rel).mkdir(parents=True, exist_ok=True)


def normalize_claim_path(path: str) -> str:
    normalized = path.replace("\\", "/").strip().strip("/")
    normalized = re.sub(r"/+", "/", normalized)
    return normalized.lower() or "."


def paths_conflict(left: str, right: str) -> bool:
    left_norm = normalize_claim_path(left)
    right_norm = normalize_claim_path(right)
    if left_norm == "." or right_norm == ".":
        return True
    return left_norm == right_norm or left_norm.startswith(right_norm + "/") or right_norm.startswith(left_norm + "/")


def claims_path(project_dir: Path) -> Path:
    return project_dir / "locks" / "claims.json"


def load_claims(project_dir: Path) -> dict[str, Any]:
    return read_json(claims_path(project_dir), {"schema_version": SCHEMA_VERSION, "updated_at": None, "claims": []})


def save_claims(project_dir: Path, claims: dict[str, Any]) -> None:
    claims["updated_at"] = now_iso()
    atomic_write_json(claims_path(project_dir), claims)


def active_claim_conflicts(
    project_dir: Path,
    task_id: str,
    claim_paths: list[str],
    ignore_tasks: set[str] | None = None,
) -> list[dict[str, Any]]:
    ignore_tasks = ignore_tasks or set()
    claims = load_claims(project_dir)
    conflicts: list[dict[str, Any]] = []
    for claim in claims.get("claims", []):
        if (
            claim.get("state") not in ACTIVE_CLAIM_STATES
            or claim.get("task_id") == task_id
            or claim.get("task_id") in ignore_tasks
        ):
            continue
        for path in claim_paths:
            if paths_conflict(path, claim.get("path", "")):
                conflicts.append(claim)
    return conflicts


def add_claims(project_dir: Path, task_id: str, agent: str, claim_paths: list[str]) -> None:
    if not claim_paths:
        return
    claims = load_claims(project_dir)
    existing = {
        (claim.get("task_id"), normalize_claim_path(claim.get("path", "")))
        for claim in claims.get("claims", [])
        if claim.get("state") in ACTIVE_CLAIM_STATES
    }
    for path in claim_paths:
        key = (task_id, normalize_claim_path(path))
        if key in existing:
            continue
        claims.setdefault("claims", []).append(
            {
                "task_id": task_id,
                "agent": agent,
                "path": normalize_claim_path(path),
                "state": "planned",
                "created_at": now_iso(),
                "updated_at": now_iso(),
            }
        )
    save_claims(project_dir, claims)


def release_claims(project_dir: Path, task_id: str, final_state: str) -> None:
    claims = load_claims(project_dir)
    changed = False
    for claim in claims.get("claims", []):
        if claim.get("task_id") == task_id and claim.get("state") in ACTIVE_CLAIM_STATES:
            claim["state"] = "released"
            claim["released_at"] = now_iso()
            claim["final_task_state"] = final_state
            changed = True
    if changed:
        save_claims(project_dir, claims)


def update_claim_state(project_dir: Path, task_id: str, state: str) -> None:
    claims = load_claims(project_dir)
    changed = False
    for claim in claims.get("claims", []):
        if claim.get("task_id") == task_id and claim.get("state") in ACTIVE_CLAIM_STATES:
            claim["state"] = state
            claim["updated_at"] = now_iso()
            changed = True
    if changed:
        save_claims(project_dir, claims)


def dependency_states(project_dir: Path, task_ids: list[str]) -> list[dict[str, Any]]:
    rows = []
    for task_id in task_ids:
        status = read_json(project_dir / "tasks" / task_id / "status.json", {})
        rows.append({"task_id": task_id, "state": status.get("state", "missing")})
    return rows


def verify_dependencies_ready(project_dir: Path, task_id: str) -> None:
    card = read_json(project_dir / "tasks" / task_id / "branch-card.json", {})
    dependencies = card.get("depends_on", [])
    not_done = [row for row in dependency_states(project_dir, dependencies) if row.get("state") != "done"]
    if not_done:
        raise SystemExit(
            "Task dependencies are not complete:\n"
            + "\n".join(f"- {row['task_id']}: {row['state']}" for row in not_done)
        )


def project_cost_rows(project_dir: Path) -> list[dict[str, Any]]:
    return read_jsonl(project_dir / "memory" / "model-performance.jsonl")


def project_spend(project_dir: Path, agent: str | None = None) -> float:
    rows = project_cost_rows(project_dir)
    total = 0.0
    for row in rows:
        if agent and row.get("agent") != agent:
            continue
        total += float(row.get("estimated_cost_cny") or 0.0)
    return round(total, 6)


def enforce_budget(project_dir: Path, project: dict[str, Any], agent: str, planned_cost: float) -> None:
    budget = project.get("budget") or {}
    if planned_cost <= 0:
        return
    max_project = budget.get("max_project_cost_cny")
    if max_project is not None and project_spend(project_dir) + planned_cost > float(max_project):
        raise SystemExit(
            f"Project budget exceeded: spent={project_spend(project_dir)} planned={planned_cost} max={max_project}"
        )
    max_agent = budget.get("max_agent_cost_cny")
    if max_agent is not None and project_spend(project_dir, agent) + planned_cost > float(max_agent):
        raise SystemExit(
            f"Agent budget exceeded for {agent}: spent={project_spend(project_dir, agent)} planned={planned_cost} max={max_agent}"
        )


def command_init_root(args: argparse.Namespace) -> None:
    root = args.root.resolve()
    ensure_root(root)
    print(json.dumps({"root": str(root), "status": "ok"}, ensure_ascii=False, indent=2))


def command_new_project(args: argparse.Namespace) -> None:
    root = args.root.resolve()
    ensure_root(root)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    slug = slugify(args.name or args.objective[:48])
    project_id = f"{stamp}-{slug}"
    project_dir = root / "projects" / project_id
    if project_dir.exists():
        raise SystemExit(f"Project already exists: {project_dir}")
    project_structure(project_dir, args.kind)
    project = {
        "schema_version": SCHEMA_VERSION,
        "id": project_id,
        "name": args.name or slug,
        "kind": args.kind,
        "objective": args.objective,
        "status": "active",
        "created_at": now_iso(),
        "updated_at": now_iso(),
        "root": str(root),
        "verification_policy": {
            "initial_mode": "strict",
            "unknown_agent_mode": "strict",
            "relax_after_min_tasks": 8,
            "relax_after_min_avg_quality": 4.2,
        },
        "budget": {
            "max_project_cost_cny": args.max_project_cost_cny,
            "max_agent_cost_cny": args.max_agent_cost_cny,
        },
        "plan_approval": {
            "required": True,
            "status": "not_drafted",
            "plan_path": "plan-approval.md",
            "approved_at": None,
            "approved_by": None,
        },
    }
    atomic_write_json(project_dir / "project.json", project)
    atomic_write_text(
        project_dir / "plan-approval.md",
        "\n".join(
            [
                f"# Plan Approval: {project['name']}",
                "",
                "Status: not_drafted",
                "",
                "## Objective",
                args.objective,
                "",
                "## Leader Plan",
                "- Draft a lightweight direction check with `costmarshal.py draft-plan` before creating worker tasks.",
                "",
                "## Predictions",
                "- Predicted cost CNY:",
                "- Predicted wall time:",
                "- Predicted input tokens:",
                "- Predicted output tokens:",
                "",
                "## User Confirmation",
                "- Required before worker task creation.",
                "- Approve with `costmarshal.py approve-plan --project <project-dir>` after the user confirms.",
                "",
            ]
        ),
    )
    atomic_write_text(
        project_dir / "master-snapshot.md",
        "\n".join(
            [
                f"# Master Snapshot: {project['name']}",
                "",
                f"Created: {project['created_at']}",
                f"Kind: {args.kind}",
                "",
                "## Objective",
                args.objective,
                "",
                "## Acceptance Criteria",
                "- Define concrete acceptance criteria before dispatching worker tasks.",
                "- Verify worker outputs through artifacts, tests, or explicit evidence.",
                "",
                "## Current Risks",
                "- Agent capability is initially unknown; use strict verification.",
                "",
            ]
        ),
    )
    tree = {
        "schema_version": SCHEMA_VERSION,
        "project_id": project_id,
        "root_id": "root",
        "updated_at": now_iso(),
        "nodes": [
            {
                "id": "root",
                "parent": None,
                "kind": "root",
                "title": project["name"],
                "status": "active",
                "children": [],
            }
        ],
    }
    save_tree(project_dir, tree)
    (project_dir / "memory" / "model-performance.jsonl").touch()
    (project_dir / "memory" / "wait-events.jsonl").touch()
    atomic_write_json(project_dir / "checks" / "connectivity.json", {"checked_at": None, "agents": {}})
    print(
        json.dumps(
            {"project": str(project_dir), "project_id": project_id, "plan": str(project_dir / "plan-approval.md")},
            ensure_ascii=False,
            indent=2,
        )
    )


def render_plan_approval(project: dict[str, Any], args: argparse.Namespace, status: str = "pending") -> str:
    prediction = {
        "predicted_cost_cny": args.predicted_cost_cny,
        "predicted_wall_time": args.predicted_wall_time,
        "predicted_input_tokens": args.predicted_input_tokens,
        "predicted_output_tokens": args.predicted_output_tokens,
    }
    lines = [
        f"# Plan Approval: {project['name']}",
        "",
        f"Status: {status}",
        f"Updated: {now_iso()}",
        "",
        "## Objective",
        project.get("objective", ""),
        "",
        "## Leader Summary",
        args.summary,
        "",
        "## Proposed Steps",
    ]
    lines.extend(f"- {item}" for item in (args.step or ["TBD"]))
    lines.extend(["", "## Proposed Worker Tasks"])
    lines.extend(f"- {item}" for item in (args.task or ["TBD"]))
    lines.extend(["", "## Agent Allocation"])
    lines.extend(f"- {item}" for item in (args.agent_plan or ["TBD"]))
    lines.extend(["", "## Predictions", "```json", json.dumps(prediction, ensure_ascii=False, indent=2), "```"])
    lines.extend(["", "## Acceptance Criteria"])
    lines.extend(f"- {item}" for item in (args.acceptance or ["TBD"]))
    lines.extend(["", "## Verification Plan"])
    lines.extend(f"- {item}" for item in (args.verification or ["TBD"]))
    lines.extend(["", "## Risks"])
    lines.extend(f"- {item}" for item in (args.risk or ["TBD"]))
    lines.extend(["", "## Open Questions"])
    lines.extend(f"- {item}" for item in (args.open_question or ["None"]))
    lines.extend(
        [
            "",
            "## User Confirmation Gate",
            "- Do not create worker tasks until the user confirms this plan.",
            "- After confirmation, run `costmarshal.py approve-plan --project <project-dir>`.",
            "",
        ]
    )
    return "\n".join(lines)


def command_draft_plan(args: argparse.Namespace) -> None:
    root = args.root.resolve()
    project_dir = project_path(root, args.project)
    project = load_project(project_dir)
    plan_path = project_dir / "plan-approval.md"
    if plan_path.exists() and (project.get("plan_approval") or {}).get("status") == "approved" and not args.force:
        raise SystemExit(f"Plan is already approved: {plan_path}. Use --force to replace after user review.")
    atomic_write_text(plan_path, render_plan_approval(project, args))
    project["plan_approval"] = {
        "required": True,
        "status": "pending",
        "plan_path": "plan-approval.md",
        "drafted_at": now_iso(),
        "approved_at": None,
        "approved_by": None,
        "prediction": {
            "predicted_cost_cny": args.predicted_cost_cny,
            "predicted_wall_time": args.predicted_wall_time,
            "predicted_input_tokens": args.predicted_input_tokens,
            "predicted_output_tokens": args.predicted_output_tokens,
        },
    }
    project["updated_at"] = now_iso()
    atomic_write_json(project_dir / "project.json", project)
    print(json.dumps({"project": str(project_dir), "plan": str(plan_path), "status": "pending_user_confirmation"}, ensure_ascii=False, indent=2))


def command_approve_plan(args: argparse.Namespace) -> None:
    root = args.root.resolve()
    project_dir = project_path(root, args.project)
    project = load_project(project_dir)
    plan_path = project_dir / "plan-approval.md"
    if not plan_path.exists():
        raise SystemExit("Plan approval file not found. Run draft-plan first.")
    approval = project.get("plan_approval") or {}
    approved_at = now_iso()
    approval.update(
        {
            "required": True,
            "status": "approved",
            "plan_path": "plan-approval.md",
            "approved_at": approved_at,
            "approved_by": args.approved_by,
            "approval_note": args.note,
        }
    )
    project["plan_approval"] = approval
    project["updated_at"] = approved_at
    atomic_write_json(project_dir / "project.json", project)
    existing_lines = read_text_if_exists(plan_path).splitlines()
    for index, line in enumerate(existing_lines):
        if line.startswith("Status: "):
            existing_lines[index] = "Status: approved"
            break
    existing = "\n".join(existing_lines).rstrip()
    approval_lines = [
        "",
        "## Approval",
        f"- Status: approved",
        f"- Approved at: {approved_at}",
        f"- Approved by: {args.approved_by}",
    ]
    if args.note:
        approval_lines.append(f"- Note: {redact(args.note)}")
    atomic_write_text(plan_path, existing + "\n" + "\n".join(approval_lines) + "\n")
    print(json.dumps({"project": str(project_dir), "plan": str(plan_path), "status": "approved"}, ensure_ascii=False, indent=2))


def require_plan_approved(project_dir: Path, project: dict[str, Any], allow_unapproved: bool = False) -> None:
    if allow_unapproved:
        return
    approval = project.get("plan_approval") or {}
    if approval.get("required", True) and approval.get("status") != "approved":
        plan_path = project_dir / approval.get("plan_path", "plan-approval.md")
        raise SystemExit(
            "Project plan is not approved by the user yet.\n"
            f"- Draft/review file: {plan_path}\n"
            "- Run `costmarshal.py draft-plan --project <project-dir> --summary \"lightweight direction\" ...`, show the coarse direction and predictions to the user, then run `costmarshal.py approve-plan --project <project-dir>` after confirmation.\n"
            "- Use `--allow-unapproved-plan` only for an explicit manual override."
        )


def branch_card_markdown(card: dict[str, Any]) -> str:
    lines = [
        f"# Branch Card: {card['id']}",
        "",
        f"Title: {card['title']}",
        f"Parent: {card['parent']}",
        f"Agent: {card['preferred_agent']}",
        f"Difficulty: {card['difficulty']}",
        f"Risk: {card['risk']}",
        f"Task type: {card['task_type']}",
        "",
        "## Purpose",
        card["purpose"],
        "",
        "## Acceptance Criteria",
    ]
    lines.extend(f"- {item}" for item in card.get("acceptance", []))
    lines.extend(["", "## Allowed Context"])
    lines.extend(f"- {item}" for item in card.get("allowed_context", []))
    if card.get("replay_memory"):
        lines.extend(["", "## Replay Memory"])
        lines.extend(f"- {item}" for item in card.get("replay_memory", []))
    if card.get("depends_on"):
        lines.extend(["", "## Dependencies"])
        lines.extend(f"- {item}" for item in card.get("depends_on", []))
    lines.extend(["", "## Allowed Writes"])
    allowed_paths = card.get("write_scope", {}).get("allowed_paths", [])
    lines.extend(f"- {item}" for item in allowed_paths or ["none"])
    if card.get("claimed_paths"):
        lines.extend(["", "## Claimed Paths"])
        lines.extend(f"- {item}" for item in card.get("claimed_paths", []))
    lines.extend(["", "## Budget", "```json", json.dumps(card.get("budget", {}), indent=2), "```", ""])
    return "\n".join(lines)


def brief_markdown(card: dict[str, Any]) -> str:
    replay_memory = card.get("replay_memory", [])
    return "\n".join(
        [
            f"# Task {card['id']}",
            "",
            "## Purpose",
            card["purpose"],
            "",
            "## Acceptance Criteria",
            "\n".join(f"- {item}" for item in card.get("acceptance", [])) or "- Fill before dispatch.",
            "",
            "## Allowed Context",
            "\n".join(f"- {item}" for item in card.get("allowed_context", [])) or "- None",
            "",
            "## Replay Memory",
            "\n".join(f"- {item}" for item in replay_memory)
            or "- None",
            "",
            "## Dependencies",
            "\n".join(f"- {item}" for item in card.get("depends_on", [])) or "- None",
            "",
            "## Allowed Writes",
            "\n".join(f"- {item}" for item in card.get("write_scope", {}).get("allowed_paths", [])) or "- None",
            "",
            "## Claimed Paths",
            "\n".join(f"- {item}" for item in card.get("claimed_paths", [])) or "- None",
            "",
            "## Commands Allowed",
            "\n".join(f"- `{item}`" for item in card.get("commands_allowed", [])) or "- None specified",
            "",
            "## Commands Forbidden",
            "\n".join(f"- {item}" for item in card.get("commands_forbidden", []))
            or "- Destructive commands, secret exposure, unapproved writes",
            "",
            "## Budget",
            "```json",
            json.dumps(card.get("budget", {}), indent=2),
            "```",
            "",
            "## Return Protocol",
            "- Write `status.json`.",
            "- Write `completion-report.md`.",
            "- Create `DONE`, `FAILED`, or `ESCALATE`.",
            "",
            "## Escalate Instead Of Guessing When",
            "\n".join(f"- {item}" for item in card.get("escalate_if", [])) or "- Any requirement is unclear",
            "",
        ]
    )


def replay_memory_paths(project_dir: Path, name: str, task_type: str) -> tuple[str, Path, Path]:
    memory_name = slugify(name, "replay-memory")
    memory_dir = project_dir / "memory" / "replay" / slugify(task_type, "task") / memory_name
    return memory_name, memory_dir, memory_dir / "memory.md"


def find_replay_memory(project_dir: Path, name: str) -> Path:
    candidate = (project_dir / name).resolve() if (project_dir / name).exists() else Path(name).expanduser()
    if candidate.exists() and candidate.is_file():
        return candidate.resolve()
    memory_name = slugify(name, "replay-memory")
    matches = sorted((project_dir / "memory" / "replay").glob(f"**/{memory_name}.md"))
    matches.extend(sorted((project_dir / "memory" / "replay").glob(f"**/{memory_name}/memory.md")))
    unique = []
    seen = set()
    for path in matches:
        resolved = str(path.resolve())
        if resolved not in seen:
            seen.add(resolved)
            unique.append(path)
    if len(unique) == 1:
        return unique[0].resolve()
    if not unique:
        raise SystemExit(f"Replay memory not found: {memory_name}")
    raise SystemExit("Replay memory is ambiguous:\n" + "\n".join(str(path) for path in unique))


def replay_memory_context(project_dir: Path, names: list[str] | None) -> list[str]:
    rels: list[str] = []
    for name in names or []:
        memory_path = find_replay_memory(project_dir, name)
        metadata = read_json(memory_path.parent / "metadata.json", {})
        if metadata and metadata.get("status") != "complete":
            raise SystemExit(f"Replay memory is not complete: {memory_path}")
        if not metadata and "Reproducibility status: complete" not in read_text_if_exists(memory_path):
            raise SystemExit(f"Replay memory lacks complete reproducibility status: {memory_path}")
        rels.append(memory_path.relative_to(project_dir).as_posix())
    return rels


def command_new_task(args: argparse.Namespace) -> None:
    root = args.root.resolve()
    ensure_root(root)
    project_dir = project_path(root, args.project)
    project = load_project(project_dir)
    require_plan_approved(project_dir, project, getattr(args, "allow_unapproved_plan", False))
    task_id = args.id or next_task_id(project_dir)
    task_dir = project_dir / "tasks" / task_id
    if task_dir.exists():
        raise SystemExit(f"Task already exists: {task_dir}")
    dependencies = args.depends_on or []
    missing_dependencies = [dep for dep in dependencies if not (project_dir / "tasks" / dep / "status.json").exists()]
    if missing_dependencies:
        raise SystemExit("Task dependencies not found:\n" + "\n".join(f"- {dep}" for dep in missing_dependencies))
    claim_paths = args.claim_path or []
    conflicts = active_claim_conflicts(project_dir, task_id, claim_paths, set(dependencies))
    if conflicts and not args.allow_lock_conflict:
        raise SystemExit(
            "Path claim conflict:\n"
            + "\n".join(f"- {claim.get('path')} claimed by {claim.get('task_id')} ({claim.get('agent')})" for claim in conflicts)
        )
    enforce_budget(project_dir, project, args.agent, float(args.max_cost_cny or 0.0))
    task_dir.mkdir(parents=True)
    (task_dir / "raw").mkdir()
    (task_dir / "artifacts").mkdir()
    memory_names = (args.replay_memory or []) + (args.project_skill or [])
    replay_memory = replay_memory_context(project_dir, memory_names)
    allowed_context = args.allowed_context or ["master-snapshot.md"]
    allowed_context = allowed_context + replay_memory
    card = {
        "schema_version": SCHEMA_VERSION,
        "id": task_id,
        "project_id": project["id"],
        "title": args.title,
        "parent": args.parent,
        "agent_tier": args.agent_tier,
        "preferred_agent": args.agent,
        "difficulty": args.difficulty,
        "risk": args.risk,
        "task_type": args.task_type,
        "purpose": args.purpose,
        "acceptance": args.acceptance or [],
        "allowed_context": allowed_context,
        "replay_memory": replay_memory,
        "depends_on": dependencies,
        "forbidden_context": ["raw transcripts from other workers unless explicitly approved"],
        "write_scope": {"mode": args.write_scope, "allowed_paths": args.allowed_path or []},
        "claimed_paths": [normalize_claim_path(path) for path in claim_paths],
        "commands_allowed": args.command or [],
        "commands_forbidden": ["destructive commands", "secret exposure", "unapproved writes"],
        "expected_artifacts": ["status.json", "completion-report.md"],
        "budget": {
            "max_wall_minutes": args.max_wall_minutes,
            "max_input_tokens": args.max_input_tokens,
            "max_output_tokens": args.max_output_tokens,
            "max_cost_cny": args.max_cost_cny,
        },
        "success_signal": "DONE file or status.json state=done",
        "escalate_if": args.escalate_if
        or ["low confidence", "tests fail", "needs broader context", "budget exceeded"],
        "created_at": now_iso(),
        "updated_at": now_iso(),
    }
    atomic_write_json(task_dir / "branch-card.json", card)
    atomic_write_text(task_dir / "branch-card.md", branch_card_markdown(card))
    atomic_write_text(task_dir / "brief.md", brief_markdown(card))
    add_claims(project_dir, task_id, args.agent, claim_paths)
    status = {
        "task_id": task_id,
        "agent": args.agent,
        "state": "planned",
        "started_at": None,
        "updated_at": now_iso(),
        "confidence": "unknown",
        "summary_path": "completion-report.md",
        "artifacts": [],
        "error": None,
        "needs_escalation": False,
    }
    atomic_write_json(task_dir / "status.json", status)
    atomic_write_text(
        task_dir / "completion-report.md",
        f"# Completion Report: {task_id}\n\nStatus: planned\nAgent: {args.agent}\nTask type: {args.task_type}\nConfidence: unknown\n\n## Result\n\n## Evidence\n\n## Budget\n- Wall time:\n- Input tokens:\n- Output tokens:\n- Estimated cost CNY:\n\n## Replay Memory Feedback\n- Memory files used:\n- Was the memory sufficient: yes|partial|no\n- Memory quality score: 1-5\n- Missing or ambiguous details:\n- Suggested memory improvements:\n- Failure attribution: memory_issue|agent_capability|task_mismatch|environment_issue|unknown\n\n## Decisions Needed From Leader\n\n## Escalation Reason\n\n## Suggested Merge Note\n",
    )
    tree = load_tree(project_dir)
    nodes = {node["id"]: node for node in tree["nodes"]}
    if args.parent not in nodes:
        raise SystemExit(f"Parent branch not found: {args.parent}")
    nodes[args.parent].setdefault("children", []).append(task_id)
    tree["nodes"].append(
        {
            "id": task_id,
            "parent": args.parent,
            "kind": "task",
            "title": args.title,
            "state": "planned",
            "agent": args.agent,
            "difficulty": args.difficulty,
            "risk": args.risk,
            "task_type": args.task_type,
            "children": [],
            "path": str(task_dir.relative_to(project_dir)),
        }
    )
    save_tree(project_dir, tree)
    print(json.dumps({"task": str(task_dir), "task_id": task_id}, ensure_ascii=False, indent=2))


def read_text_if_exists(path: Path) -> str:
    return path.read_text(encoding="utf-8") if path.exists() else ""


def require_replay_memory_contract(args: argparse.Namespace, task_dir: Path) -> None:
    if args.draft:
        return
    status = read_json(task_dir / "status.json", {})
    issues = []
    if status.get("state") != "done":
        issues.append("source task status.json must have state=done")
    if not read_text_if_exists(task_dir / "completion-report.md").strip():
        issues.append("source task must have a non-empty completion-report.md")
    required = [
        ("--summary", args.summary),
        ("--memory-task-type", args.memory_task_type),
        ("--working-dir", args.working_dir),
        ("--required-input", args.required_input),
        ("--allowed-param", args.allowed_param),
        ("--allowed-command", args.allowed_command),
        ("--expected-output", args.expected_output),
        ("--success-marker", args.success_marker),
    ]
    for flag, value in required:
        if not value:
            issues.append(f"{flag} is required unless --draft is set")
    if issues:
        raise SystemExit("Replay memory is not fully reproducible:\n- " + "\n- ".join(issues))


def render_replay_memory(
    memory_name: str,
    title: str,
    project: dict[str, Any],
    source_task: str,
    card: dict[str, Any],
    completion_report: str,
    args: argparse.Namespace,
) -> str:
    summary = args.summary or "Leader must replace this with the reusable lesson from the senior demonstration."
    required_inputs = args.required_input or ["Leader must add exact input files, datasets, configs, commits, or external artifacts."]
    allowed_params = args.allowed_param or ["Replace only explicit parameters listed by the leader."]
    allowed_commands = args.allowed_command or card.get("commands_allowed", []) or ["Leader must add the exact replay command."]
    expected_outputs = args.expected_output or ["Leader must add exact output files or artifacts."]
    success_markers = args.success_marker or ["Leader must add file, log, exit-code, or test success markers."]
    dependencies = args.dependency or ["No extra dependencies beyond the project environment."]
    forbidden_changes = args.forbidden_change or ["Any file, parameter, command, dependency, or behavior not listed in this memory file."]
    verification_commands = args.verification_command or ["Use the success markers and leader verification checklist."]
    return "\n".join(
        [
            f"# Replay Memory: {title}",
            "",
            "This is a project-local replay memory file. It is the only reusable context a weaker agent should need for this proven workflow.",
            "",
            "## Classification",
            "",
            f"- Project: `{project.get('id', 'unknown')}`",
            f"- Source task: `{source_task}`",
            f"- Demonstrating agent: `{args.agent}`",
            f"- Source title: {card.get('title', '')}",
            f"- Task type: {args.memory_task_type or card.get('task_type', 'mechanical')}",
            f"- Source difficulty: {card.get('difficulty', '')}",
            f"- Source risk: {card.get('risk', '')}",
            f"- Reproducibility status: {'draft' if args.draft else 'complete'}",
            "",
            "## Reproducibility Contract",
            "",
            "- This memory file must be enough for a weaker agent to replay the workflow without reading the senior raw transcript.",
            "- All variable inputs must be listed under allowed parameter changes.",
            "- All commands must be exact and executable from the working directory.",
            "- Success must be decided only by the success markers and expected outputs listed here.",
            "- Anything not listed here is forbidden and requires escalation.",
            "",
            "## Working Directory",
            "",
            args.working_dir or "Leader must add the exact relative or absolute working directory.",
            "",
            "## Reproducible Procedure",
            "",
            summary,
            "",
            "## Required Inputs",
            "",
            "\n".join(f"- {item}" for item in required_inputs),
            "",
            "## Dependencies",
            "",
            "\n".join(f"- {item}" for item in dependencies),
            "",
            "## Allowed Parameter Changes",
            "",
            "\n".join(f"- {item}" for item in allowed_params),
            "",
            "## Allowed Commands",
            "",
            "\n".join(f"- `{item}`" for item in allowed_commands),
            "",
            "## Expected Outputs",
            "",
            "\n".join(f"- {item}" for item in expected_outputs),
            "",
            "## Success Markers",
            "",
            "\n".join(f"- {item}" for item in success_markers),
            "",
            "## Verification Commands",
            "",
            "\n".join(f"- `{item}`" for item in verification_commands),
            "",
            "## Forbidden Changes",
            "",
            "\n".join(f"- {item}" for item in forbidden_changes),
            "",
            "## Failure Protocol",
            "",
            f"- {args.failure_mode}",
            "- Do not broaden the task, invent a new method, or debug freely.",
            "- If any command fails or an expected marker is missing, write FAILED or ESCALATE and explain the exact blocker.",
            "",
            "## Leader Verification Checklist",
            "",
            "- Verify all success markers.",
            "- Review any changed files against the whitelist.",
            "- Escalate if the replay required judgment outside this memory file.",
            "",
            "## Source Completion Report Snapshot",
            "",
            "```markdown",
            completion_report.strip() or "No completion report found.",
            "```",
            "",
        ]
    )


def command_promote_memory(args: argparse.Namespace) -> None:
    root = args.root.resolve()
    project_dir = project_path(root, args.project)
    project = load_project(project_dir)
    task_dir = project_dir / "tasks" / args.source_task
    if not task_dir.exists():
        raise SystemExit(f"Source task not found: {args.source_task}")
    require_replay_memory_contract(args, task_dir)
    card = read_json(task_dir / "branch-card.json", {})
    completion_report = read_text_if_exists(task_dir / "completion-report.md")
    task_type = args.memory_task_type or card.get("task_type", "mechanical")
    memory_name, memory_dir, memory_md = replay_memory_paths(project_dir, args.name, task_type)
    if memory_dir.exists() and not args.force:
        raise SystemExit(f"Replay memory already exists: {memory_dir}. Use --force to overwrite.")
    memory_dir.mkdir(parents=True, exist_ok=True)
    title = args.title or memory_name
    metadata = {
        "schema_version": SCHEMA_VERSION,
        "created_at": now_iso(),
        "project_id": project.get("id"),
        "source_task": args.source_task,
        "name": memory_name,
        "title": title,
        "agent": args.agent,
        "memory_task_type": task_type,
        "source_task_type": card.get("task_type"),
        "source_difficulty": card.get("difficulty"),
        "source_risk": card.get("risk"),
        "status": "draft" if args.draft else "complete",
    }
    atomic_write_json(memory_dir / "metadata.json", metadata)
    atomic_write_text(
        memory_md,
        render_replay_memory(memory_name, title, project, args.source_task, card, completion_report, args),
    )
    print(
        json.dumps(
            {
                "replay_memory": memory_name,
                "memory_task_type": task_type,
                "memory": str(memory_md),
                "use_with": f"--replay-memory {memory_name}",
            },
            ensure_ascii=False,
            indent=2,
        )
    )


def replay_memory_for_feedback(project_dir: Path, args: argparse.Namespace) -> Path:
    if args.memory:
        return find_replay_memory(project_dir, args.memory)
    task_dir = project_dir / "tasks" / args.task
    card = read_json(task_dir / "branch-card.json", {})
    memories = card.get("replay_memory") or []
    if len(memories) == 1:
        return (project_dir / memories[0]).resolve()
    if not memories:
        raise SystemExit("Task has no replay memory; pass --memory explicitly if needed.")
    raise SystemExit("Task has multiple replay memories; pass --memory to choose one.")


def feedback_recommendation(row: dict[str, Any], metadata: dict[str, Any]) -> str:
    attribution = row.get("attribution")
    quality = int(row.get("memory_quality") or 0)
    outcome = row.get("outcome")
    if metadata.get("status") == "needs_revision":
        return "senior_refresh_memory"
    if attribution == "agent_capability":
        return "adjust_agent_routing_or_escalate_agent"
    if attribution == "task_mismatch":
        return "create_new_memory_or_reclassify_task"
    if attribution == "environment_issue":
        return "fix_environment_then_retry"
    if outcome != "succeeded" and quality >= 4:
        return "likely_agent_or_environment_issue"
    return "leader_review"


def command_record_memory_feedback(args: argparse.Namespace) -> None:
    root = args.root.resolve()
    project_dir = project_path(root, args.project)
    task_dir = project_dir / "tasks" / args.task
    if not task_dir.exists():
        raise SystemExit(f"Task not found: {args.task}")
    memory_path = replay_memory_for_feedback(project_dir, args)
    metadata_path = memory_path.parent / "metadata.json"
    metadata = read_json(metadata_path, {})
    task_status = read_json(task_dir / "status.json", {})
    card = read_json(task_dir / "branch-card.json", {})
    agent_name = args.agent or task_status.get("agent") or card.get("preferred_agent", "unknown")
    row = {
        "timestamp": now_iso(),
        "project_id": load_project(project_dir).get("id"),
        "task_id": args.task,
        "agent": agent_name,
        "memory": memory_path.relative_to(project_dir).as_posix(),
        "outcome": args.outcome,
        "sufficient": args.sufficient,
        "memory_quality": args.memory_quality,
        "attribution": args.attribution,
        "needs_senior_refresh": args.needs_senior_refresh,
        "missing_or_ambiguous": redact(args.issue or ""),
        "suggested_improvement": redact(args.suggestion or ""),
        "note": redact(args.note or ""),
    }
    append_jsonl(memory_path.parent / "feedback.jsonl", row)
    append_jsonl(project_dir / "memory" / "replay-feedback.jsonl", row)

    feedback_count = int(metadata.get("feedback_count") or 0) + 1
    total_quality = float(metadata.get("total_feedback_quality") or 0.0) + float(args.memory_quality)
    attribution_counts = metadata.setdefault("attribution_counts", {})
    attribution_counts[args.attribution] = int(attribution_counts.get(args.attribution) or 0) + 1
    metadata.update(
        {
            "feedback_count": feedback_count,
            "total_feedback_quality": round(total_quality, 3),
            "avg_feedback_quality": round(total_quality / feedback_count, 3),
            "last_feedback_at": now_iso(),
            "last_feedback_task": args.task,
            "last_feedback_agent": agent_name,
            "attribution_counts": attribution_counts,
        }
    )
    if args.outcome == "succeeded":
        metadata["successful_replays"] = int(metadata.get("successful_replays") or 0) + 1
    else:
        metadata["failed_or_partial_replays"] = int(metadata.get("failed_or_partial_replays") or 0) + 1
    if args.needs_senior_refresh or (args.attribution == "memory_issue" and args.memory_quality <= 3):
        metadata["status"] = "needs_revision"
        metadata["revision_reason"] = args.issue or "Replay feedback attributed failure to memory quality."
        metadata["needs_senior_refresh_count"] = int(metadata.get("needs_senior_refresh_count") or 0) + 1
    atomic_write_json(metadata_path, metadata)
    recommendation = feedback_recommendation(row, metadata)
    print(
        json.dumps(
            {
                "recorded": True,
                "memory": str(memory_path),
                "metadata_status": metadata.get("status"),
                "leader_recommendation": recommendation,
                "feedback": row,
            },
            ensure_ascii=False,
            indent=2,
        )
    )


def next_handoff_id(project_dir: Path) -> str:
    existing = []
    for path in (project_dir / "handoffs").glob("HF-*.md"):
        match = re.match(r"HF-(\d+)", path.stem)
        if match:
            existing.append(int(match.group(1)))
    return f"HF-{(max(existing) if existing else 0) + 1:04d}"


def command_record_handoff(args: argparse.Namespace) -> None:
    root = args.root.resolve()
    project_dir = project_path(root, args.project)
    source_task_dir = project_dir / "tasks" / args.source_task
    if not source_task_dir.exists():
        raise SystemExit(f"Source task not found: {args.source_task}")
    handoff_id = args.id or next_handoff_id(project_dir)
    handoff_path = project_dir / "handoffs" / f"{handoff_id}.md"
    if handoff_path.exists() and not args.force:
        raise SystemExit(f"Handoff already exists: {handoff_path}. Use --force to overwrite.")
    lines = [
        f"# Handoff: {handoff_id}",
        "",
        f"Source task: `{args.source_task}`",
        f"Target task: `{args.target_task or 'unassigned'}`",
        f"Created: {now_iso()}",
        "",
        "## Summary",
        args.summary,
        "",
        "## Decisions",
    ]
    lines.extend(f"- {item}" for item in (args.decision or ["None"]))
    lines.extend(["", "## Artifacts"])
    lines.extend(f"- {item}" for item in (args.artifact or ["None"]))
    lines.extend(["", "## Risks"])
    lines.extend(f"- {item}" for item in (args.risk_note or ["None"]))
    lines.extend(["", "## Next Steps"])
    lines.extend(f"- {item}" for item in (args.next_step or ["None"]))
    lines.extend(
        [
            "",
            "## Context Boundary",
            "- This handoff is the default context bridge.",
            "- Do not read source raw transcripts unless the leader explicitly approves.",
            "",
        ]
    )
    atomic_write_text(handoff_path, "\n".join(lines))
    row = {
        "timestamp": now_iso(),
        "handoff_id": handoff_id,
        "source_task": args.source_task,
        "target_task": args.target_task,
        "path": handoff_path.relative_to(project_dir).as_posix(),
        "summary": redact(args.summary),
    }
    append_jsonl(project_dir / "memory" / "handoffs.jsonl", row)
    print(
        json.dumps(
            {
                "handoff": handoff_id,
                "path": str(handoff_path),
                "use_with": f"--allowed-context {handoff_path.relative_to(project_dir).as_posix()}",
            },
            ensure_ascii=False,
            indent=2,
        )
    )


def command_new_review_task(args: argparse.Namespace) -> None:
    root = args.root.resolve()
    project_dir = project_path(root, args.project)
    source_task_dir = project_dir / "tasks" / args.source_task
    if not source_task_dir.exists():
        raise SystemExit(f"Source task not found: {args.source_task}")
    source_rel = f"tasks/{args.source_task}"
    title = args.title or f"Review {args.source_task}"
    purpose = args.purpose or f"Review task {args.source_task} output for correctness, evidence, and merge risk."
    review_args = argparse.Namespace(
        root=args.root,
        project=args.project,
        id=args.id,
        title=title,
        parent=args.parent or args.source_task,
        agent=args.reviewer,
        agent_tier=args.agent_tier,
        difficulty=args.difficulty,
        risk=args.risk,
        task_type="review",
        purpose=purpose,
        acceptance=args.acceptance
        or [
            "Identify correctness, safety, evidence, and integration risks.",
            "Do not implement broad fixes.",
            "Return a review-report.md or completion-report.md with accept/rework/escalate recommendation.",
        ],
        allowed_context=args.allowed_context
        or [
            f"{source_rel}/branch-card.md",
            f"{source_rel}/status.json",
            f"{source_rel}/completion-report.md",
        ],
        replay_memory=None,
        project_skill=None,
        depends_on=[args.source_task],
        allowed_path=[],
        claim_path=[],
        allow_lock_conflict=False,
        allow_unapproved_plan=getattr(args, "allow_unapproved_plan", False),
        write_scope="none",
        command=args.command or [],
        escalate_if=args.escalate_if
        or ["source evidence is insufficient", "review requires broader context", "security or architecture concern"],
        max_wall_minutes=args.max_wall_minutes,
        max_input_tokens=args.max_input_tokens,
        max_output_tokens=args.max_output_tokens,
        max_cost_cny=args.max_cost_cny,
    )
    command_new_task(review_args)


def command_set_status(args: argparse.Namespace) -> None:
    root = args.root.resolve()
    project_dir = project_path(root, args.project)
    result = set_task_status(project_dir, args.task, args.state, args.confidence, args.error)
    print(json.dumps(result, ensure_ascii=False, indent=2))


def score_to_verification(total: int, success_rate: float, avg_quality: float) -> str:
    if total < 3:
        return "strict"
    if total >= 8 and success_rate >= 0.85 and avg_quality >= 4.2:
        return "relaxed"
    if success_rate >= 0.7 and avg_quality >= 3.5:
        return "standard"
    return "strict"


def update_global_memory(root: Path, row: dict[str, Any]) -> None:
    memory_path = root / "memory" / "agent-memory.json"
    memory = read_json(memory_path, empty_memory())
    agent_name = row["agent"]
    agents = memory.setdefault("agents", {})
    agent = agents.setdefault(
        agent_name,
        {
            "tier_prior": row.get("model_tier_prior"),
            "total_tasks": 0,
            "completed": 0,
            "failed": 0,
            "escalated": 0,
            "accepted_by_leader": 0,
            "total_quality": 0.0,
            "input_tokens": 0,
            "output_tokens": 0,
            "total_tokens": 0,
            "estimated_cost_cny": 0.0,
            "avg_quality": 0.0,
            "success_rate": 0.0,
            "verification_mode": "strict",
            "by_task_type": {},
        },
    )
    agent["tier_prior"] = agent.get("tier_prior") or row.get("model_tier_prior")
    agent["total_tasks"] += 1
    if row.get("completed"):
        agent["completed"] += 1
    if not row.get("completed"):
        agent["failed"] += 1
    if row.get("needs_escalation"):
        agent["escalated"] += 1
    if row.get("accepted_by_leader"):
        agent["accepted_by_leader"] += 1
    quality = float(row.get("quality_score") or 0)
    agent["total_quality"] += quality
    input_tokens = int(row.get("input_tokens") or 0)
    output_tokens = int(row.get("output_tokens") or 0)
    agent["input_tokens"] = int(agent.get("input_tokens") or 0) + input_tokens
    agent["output_tokens"] = int(agent.get("output_tokens") or 0) + output_tokens
    agent["total_tokens"] = int(agent.get("total_tokens") or 0) + input_tokens + output_tokens
    agent["estimated_cost_cny"] = round(
        float(agent.get("estimated_cost_cny") or 0.0) + float(row.get("estimated_cost_cny") or 0.0),
        6,
    )
    total = max(1, agent["total_tasks"])
    agent["avg_quality"] = round(agent["total_quality"] / total, 3)
    agent["success_rate"] = round(agent["accepted_by_leader"] / total, 3)
    agent["verification_mode"] = score_to_verification(total, agent["success_rate"], agent["avg_quality"])
    task_type = row.get("task_type", "unknown")
    bucket = agent["by_task_type"].setdefault(
        task_type,
        {"count": 0, "accepted": 0, "total_quality": 0.0, "avg_quality": 0.0, "success_rate": 0.0},
    )
    bucket["count"] += 1
    if row.get("accepted_by_leader"):
        bucket["accepted"] += 1
    bucket["total_quality"] += quality
    bucket["avg_quality"] = round(bucket["total_quality"] / bucket["count"], 3)
    bucket["success_rate"] = round(bucket["accepted"] / bucket["count"], 3)
    memory["updated_at"] = now_iso()
    atomic_write_json(memory_path, memory)


def infer_tier(config: dict[str, Any], agent_name: str) -> str | None:
    return config.get("agents", {}).get(agent_name, {}).get("tier")


def pricing_for_agent(config: dict[str, Any], agent_name: str) -> dict[str, float] | None:
    configured = config.get("agents", {}).get(agent_name, {}).get("pricing_cny_per_1m")
    if isinstance(configured, dict):
        return {
            "input": float(configured.get("input") or configured.get("input_per_1m") or 0.0),
            "output": float(configured.get("output") or configured.get("output_per_1m") or 0.0),
        }
    return DEFAULT_PRICING_CNY_PER_1M.get(agent_name)


def estimate_cost_cny(config: dict[str, Any], agent_name: str, input_tokens: int, output_tokens: int) -> tuple[float, str]:
    pricing = pricing_for_agent(config, agent_name)
    if not pricing:
        return 0.0, "missing_pricing"
    cost = (input_tokens * pricing["input"] + output_tokens * pricing["output"]) / 1_000_000
    return round(cost, 6), "auto_pricing_cny_per_1m"


def estimate_tokens_from_chars(chars: int) -> int:
    return max(1, int((chars + 3) / 4))


def enforce_run_task_budget(
    project_dir: Path,
    project: dict[str, Any],
    config: dict[str, Any],
    card: dict[str, Any],
    agent_name: str,
    estimated_input_chars: int,
    max_output_tokens: int,
    allow_budget_overrun: bool,
) -> dict[str, Any]:
    estimated_input_tokens = estimate_tokens_from_chars(estimated_input_chars)
    estimated_cost, cost_source = estimate_cost_cny(config, agent_name, estimated_input_tokens, max_output_tokens)
    budget = card.get("budget") or {}
    max_task_cost = budget.get("max_cost_cny")
    if max_task_cost is not None and estimated_cost > float(max_task_cost) and not allow_budget_overrun:
        raise SystemExit(
            f"Task budget preflight exceeded: estimated={estimated_cost} max_task={max_task_cost}. "
            "Use --allow-budget-overrun only after leader review."
        )
    if not allow_budget_overrun:
        enforce_budget(project_dir, project, agent_name, estimated_cost)
    return {
        "estimated_input_tokens": estimated_input_tokens,
        "max_output_tokens": max_output_tokens,
        "estimated_cost_cny": estimated_cost,
        "cost_source": cost_source,
    }


def update_global_usage(root: Path, row: dict[str, Any]) -> None:
    memory_path = root / "memory" / "agent-memory.json"
    memory = read_json(memory_path, empty_memory())
    agent_name = row["agent"]
    agents = memory.setdefault("agents", {})
    agent = agents.setdefault(
        agent_name,
        {
            "tier_prior": row.get("model_tier_prior"),
            "total_tasks": 0,
            "completed": 0,
            "failed": 0,
            "escalated": 0,
            "accepted_by_leader": 0,
            "total_quality": 0.0,
            "input_tokens": 0,
            "output_tokens": 0,
            "total_tokens": 0,
            "estimated_cost_cny": 0.0,
            "avg_quality": 0.0,
            "success_rate": 0.0,
            "verification_mode": "strict",
            "by_task_type": {},
        },
    )
    agent["tier_prior"] = agent.get("tier_prior") or row.get("model_tier_prior")
    input_tokens = int(row.get("input_tokens") or 0)
    output_tokens = int(row.get("output_tokens") or 0)
    agent["input_tokens"] = int(agent.get("input_tokens") or 0) + input_tokens
    agent["output_tokens"] = int(agent.get("output_tokens") or 0) + output_tokens
    agent["total_tokens"] = int(agent.get("total_tokens") or 0) + input_tokens + output_tokens
    agent["estimated_cost_cny"] = round(
        float(agent.get("estimated_cost_cny") or 0.0) + float(row.get("estimated_cost_cny") or 0.0),
        6,
    )
    memory["updated_at"] = now_iso()
    atomic_write_json(memory_path, memory)


def record_usage_event(
    root: Path,
    project_dir: Path,
    task: str,
    agent_name: str,
    model: str,
    card: dict[str, Any],
    context_size: str,
    wall_seconds: int,
    input_tokens: int,
    output_tokens: int,
    note: str,
) -> dict[str, Any]:
    project = load_project(project_dir)
    config = read_json(root / "config" / "agents.json")
    estimated_cost_cny, cost_source = estimate_cost_cny(config, agent_name, input_tokens, output_tokens)
    row = {
        "event_type": "usage",
        "timestamp": now_iso(),
        "project_id": project["id"],
        "task_id": task,
        "agent": agent_name,
        "model": model,
        "model_tier_prior": card.get("agent_tier") or infer_tier(config, agent_name),
        "task_type": card.get("task_type", "unknown"),
        "difficulty": card.get("difficulty", "unknown"),
        "risk": card.get("risk", "unknown"),
        "context_size": context_size,
        "wall_seconds": wall_seconds,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "total_tokens": input_tokens + output_tokens,
        "estimated_cost_cny": estimated_cost_cny,
        "cost_source": cost_source,
        "note": redact(note),
    }
    append_jsonl(project_dir / "memory" / "model-performance.jsonl", row)
    append_jsonl(root / "memory" / "events.jsonl", row)
    update_global_usage(root, row)
    return row


def record_result_event(root: Path, project_dir: Path, args: argparse.Namespace) -> dict[str, Any]:
    project = load_project(project_dir)
    task_dir = project_dir / "tasks" / args.task
    card = read_json(task_dir / "branch-card.json", {})
    config = read_json(root / "config" / "agents.json")
    agent_name = args.agent or card.get("preferred_agent", "unknown")
    input_tokens = int(args.input_tokens or 0)
    output_tokens = int(args.output_tokens or 0)
    if args.estimated_cost_cny is not None:
        estimated_cost_cny = args.estimated_cost_cny
        cost_source = "caller"
    elif input_tokens or output_tokens:
        estimated_cost_cny, cost_source = estimate_cost_cny(config, agent_name, input_tokens, output_tokens)
    else:
        estimated_cost_cny = 0.0
        cost_source = "not_provided"
    row = {
        "event_type": "result",
        "timestamp": now_iso(),
        "project_id": project["id"],
        "task_id": args.task,
        "agent": agent_name,
        "model": getattr(args, "model", None) or configured_model(config, agent_name),
        "model_tier_prior": args.model_tier or card.get("agent_tier") or infer_tier(config, agent_name),
        "task_type": args.task_type or card.get("task_type", "unknown"),
        "difficulty": args.difficulty or card.get("difficulty", "unknown"),
        "risk": args.risk or card.get("risk", "unknown"),
        "context_size": args.context_size,
        "wall_seconds": args.wall_seconds,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "total_tokens": input_tokens + output_tokens,
        "estimated_cost_cny": estimated_cost_cny,
        "cost_source": cost_source,
        "completed": args.status == "done",
        "needs_escalation": args.status == "escalate",
        "accepted_by_leader": args.accepted_by_leader,
        "accepted_by_senior": args.accepted_by_senior,
        "test_result": args.test_result,
        "rework_count": args.rework_count,
        "failure_type": args.failure_type,
        "quality_score": args.quality_score,
        "note": redact(args.note or ""),
    }
    append_jsonl(project_dir / "memory" / "model-performance.jsonl", row)
    append_jsonl(root / "memory" / "events.jsonl", row)
    update_global_memory(root, row)
    status_result = set_task_status(project_dir, args.task, args.status, args.confidence, args.failure_type, row.get("model"))
    return {"recorded": True, "status": status_result, "event": row}


def command_record_result(args: argparse.Namespace) -> None:
    root = args.root.resolve()
    ensure_root(root)
    project_dir = project_path(root, args.project)
    result = record_result_event(root, project_dir, args)
    print(json.dumps(result, ensure_ascii=False, indent=2))


def agent_base_url(agent_name: str, agent: dict[str, Any]) -> str | None:
    base_url_env = agent.get("base_url_env")
    return os.environ.get(base_url_env or "") or agent.get("base_url") or DEFAULT_BASE_URLS.get(agent_name)


def agent_model(agent: dict[str, Any]) -> str:
    return os.environ.get(agent.get("model_env") or "") or agent.get("model")


def agent_temperature(agent: dict[str, Any]) -> float:
    temperature_raw = os.environ.get(agent.get("temperature_env") or "")
    return float(temperature_raw) if temperature_raw else float(agent.get("temperature", 0))


def agent_api_key(agent: dict[str, Any]) -> str | None:
    key_envs = agent.get("required_env", [])
    return os.environ.get(key_envs[0]) if key_envs else None


def command_check_agents(args: argparse.Namespace) -> None:
    root = args.root.resolve()
    secrets = load_local_secrets(root, args.secrets_file)
    ensure_root(root)
    config = read_json(root / "config" / "agents.json")
    selected = set(args.agents.split(",")) if args.agents else None
    results = {
        "checked_at": now_iso(),
        "live": args.live,
        "secrets": {
            "loaded_files": secrets["loaded_files"],
            "loaded_env_names": secrets["loaded_env_names"],
            "checked_file_count": len(secrets["checked_files"]),
        },
        "agents": {},
    }
    for name, agent in config.get("agents", {}).items():
        if selected and name not in selected:
            continue
        if not agent.get("enabled", True):
            results["agents"][name] = {"status": "disabled"}
            continue
        missing = [env for env in agent.get("required_env", []) if not os.environ.get(env)]
        if missing:
            results["agents"][name] = {"status": "missing_env", "missing_env": missing}
            continue
        if agent.get("kind") == "codex-subagent":
            results["agents"][name] = {"status": "available", "kind": agent.get("kind")}
            continue
        if not args.live:
            results["agents"][name] = {"status": "env_present", "kind": agent.get("kind")}
            continue
        results["agents"][name] = live_openai_check(name, agent, args.timeout)
    if args.project:
        project_dir = project_path(root, args.project)
        atomic_write_json(project_dir / "checks" / "connectivity.json", results)
    print(json.dumps(results, ensure_ascii=False, indent=2))


def live_openai_check(agent_name: str, agent: dict[str, Any], timeout: float) -> dict[str, Any]:
    base_url_env = agent.get("base_url_env")
    base_url = agent_base_url(agent_name, agent)
    if not base_url:
        return {"status": "missing_base_url", "base_url_env": base_url_env}
    key_envs = agent.get("required_env", [])
    api_key = agent_api_key(agent)
    if not api_key:
        return {"status": "missing_env", "missing_env": key_envs[:1]}
    url = base_url.rstrip("/") + "/chat/completions"
    model = agent_model(agent)
    temperature = agent_temperature(agent)
    body = {
        "model": model,
        "messages": [{"role": "user", "content": "Reply with OK."}],
        "max_tokens": 8,
        "temperature": temperature,
    }
    request = urllib.request.Request(
        url,
        data=json.dumps(body).encode("utf-8"),
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        method="POST",
    )
    started = time.time()
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            payload = response.read(2048)
        return {"status": "live_ok", "latency_seconds": round(time.time() - started, 3), "bytes": len(payload)}
    except urllib.error.HTTPError as exc:
        return {"status": "http_error", "code": exc.code, "latency_seconds": round(time.time() - started, 3)}
    except Exception as exc:  # noqa: BLE001 - returned as diagnostic status
        return {"status": "connection_error", "error": type(exc).__name__, "latency_seconds": round(time.time() - started, 3)}


def openai_chat_completion(
    agent_name: str,
    agent: dict[str, Any],
    messages: list[dict[str, str]],
    max_tokens: int,
    timeout: float,
) -> dict[str, Any]:
    base_url = agent_base_url(agent_name, agent)
    if not base_url:
        raise RuntimeError(f"missing base URL for agent {agent_name}")
    api_key = agent_api_key(agent)
    if not api_key:
        missing = ", ".join(agent.get("required_env", []))
        raise RuntimeError(f"missing API key env for agent {agent_name}: {missing}")
    body = {
        "model": agent_model(agent),
        "messages": messages,
        "max_tokens": max_tokens,
        "temperature": agent_temperature(agent),
    }
    request = urllib.request.Request(
        base_url.rstrip("/") + "/chat/completions",
        data=json.dumps(body).encode("utf-8"),
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def extract_openai_text(payload: dict[str, Any]) -> str:
    choices = payload.get("choices") or []
    if not choices:
        return ""
    message = choices[0].get("message") or {}
    content = message.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for item in content:
            if isinstance(item, dict) and isinstance(item.get("text"), str):
                parts.append(item["text"])
            elif isinstance(item, str):
                parts.append(item)
        return "\n".join(parts)
    text = choices[0].get("text")
    return text if isinstance(text, str) else ""


def extract_usage_tokens(payload: dict[str, Any]) -> tuple[int, int]:
    usage = payload.get("usage") or {}
    input_tokens = usage.get("prompt_tokens", usage.get("input_tokens", 0)) or 0
    output_tokens = usage.get("completion_tokens", usage.get("output_tokens", 0)) or 0
    return int(input_tokens), int(output_tokens)


def project_relative_context_path(project_dir: Path, rel_path: str) -> Path:
    candidate = (project_dir / rel_path).resolve()
    try:
        candidate.relative_to(project_dir.resolve())
    except ValueError as exc:
        raise SystemExit(f"Allowed context escapes project directory: {rel_path}") from exc
    return candidate


def load_allowed_context(
    project_dir: Path,
    paths: list[str],
    max_chars: int,
    allow_raw_context: bool = False,
) -> str:
    sections: list[str] = []
    remaining = max_chars
    for rel_path in paths:
        normalized = rel_path.replace("\\", "/").strip("/")
        if not allow_raw_context and (normalized.startswith("raw/") or "/raw/" in normalized):
            sections.append(f"## Context: {normalized}\nSkipped raw context by default.\n")
            continue
        path = project_relative_context_path(project_dir, normalized)
        if not path.exists():
            sections.append(f"## Context: {normalized}\nMissing file.\n")
            continue
        if path.is_dir():
            entries = sorted(item.relative_to(path).as_posix() for item in path.rglob("*") if item.is_file())
            text = "Directory listing:\n" + "\n".join(f"- {item}" for item in entries[:200])
        else:
            text = path.read_text(encoding="utf-8", errors="replace")
        if remaining <= 0:
            sections.append(f"## Context: {normalized}\nSkipped because context budget was exhausted.\n")
            continue
        if len(text) > remaining:
            text = text[:remaining] + "\n\n[TRUNCATED BY COSTMARSHAL CONTEXT BUDGET]\n"
        remaining -= len(text)
        sections.append(f"## Context: {normalized}\n\n{text}\n")
    return "\n".join(sections)


def worker_messages(project_dir: Path, card: dict[str, Any], brief: str, context: str) -> list[dict[str, str]]:
    system = "\n".join(
        [
            "You are a CostMarshal worker agent.",
            "Use only the provided task brief and allowed context.",
            "Do not ask for or reveal API keys or secrets.",
            "Do not claim that files were edited; this runner is read-only and cannot modify project files.",
            "Return a concise Markdown completion report with these sections:",
            "Status: done|failed|escalate",
            "Confidence: high|medium|low",
            "## Result",
            "## Evidence",
            "## Budget",
            "## Decisions Needed From Leader",
            "## Escalation Reason",
            "## Suggested Merge Note",
        ]
    )
    user = "\n".join(
        [
            f"Project id: {card.get('project_id')}",
            f"Task id: {card.get('id')}",
            f"Task type: {card.get('task_type')}",
            f"Difficulty: {card.get('difficulty')}",
            f"Risk: {card.get('risk')}",
            "",
            "# Task Brief",
            brief,
            "",
            "# Allowed Context",
            context or "No additional context was provided.",
            "",
            "Return only the completion report.",
        ]
    )
    return [{"role": "system", "content": system}, {"role": "user", "content": user}]


def parse_completion_field(report: str, field: str) -> str | None:
    match = re.search(rf"(?im)^\s*{re.escape(field)}\s*:\s*([A-Za-z_-]+)\s*$", report)
    return match.group(1).strip().lower() if match else None


def report_status(report: str, fallback: str) -> str:
    parsed = parse_completion_field(report, "Status")
    return parsed if parsed in {"done", "failed", "escalate"} else fallback


def report_confidence(report: str, fallback: str) -> str:
    parsed = parse_completion_field(report, "Confidence")
    return parsed if parsed in {"unknown", "low", "medium", "high"} else fallback


def command_run_task(args: argparse.Namespace) -> None:
    root = args.root.resolve()
    secrets = load_local_secrets(root, args.secrets_file)
    ensure_root(root)
    project_dir = project_path(root, args.project)
    project = load_project(project_dir)
    task_dir = project_dir / "tasks" / args.task
    card = read_json(task_dir / "branch-card.json")
    status = read_json(task_dir / "status.json")
    config = read_json(root / "config" / "agents.json")
    agent_name = args.agent or card.get("preferred_agent")
    agent = config.get("agents", {}).get(agent_name)
    if not agent:
        raise SystemExit(f"Agent not configured: {agent_name}")
    if agent.get("kind") != "openai-compatible":
        raise SystemExit(f"run-task only supports openai-compatible agents in v1. Agent {agent_name} is {agent.get('kind')}.")
    if not agent.get("enabled", True):
        raise SystemExit(f"Agent is disabled: {agent_name}")
    if status.get("state") not in {"planned", "failed", "escalate"} and not args.force:
        raise SystemExit(f"Task state is {status.get('state')}; use --force to rerun.")
    model = agent_model(agent)

    brief = read_text_if_exists(task_dir / "brief.md")
    context = load_allowed_context(project_dir, card.get("allowed_context", []), args.max_context_chars, args.allow_raw_context)
    max_tokens = args.max_output_tokens or int((card.get("budget") or {}).get("max_output_tokens") or 4000)
    messages = worker_messages(project_dir, card, brief, context)
    estimated_input_chars = sum(len(item.get("content", "")) for item in messages)
    budget_preflight = enforce_run_task_budget(
        project_dir,
        project,
        config,
        card,
        agent_name,
        estimated_input_chars,
        max_tokens,
        args.allow_budget_overrun,
    )
    dry_run_payload = {
        "project": str(project_dir),
        "task": args.task,
        "agent": agent_name,
        "model": model,
        "base_url": agent_base_url(agent_name, agent),
        "max_tokens": max_tokens,
        "temperature": agent_temperature(agent),
        "estimated_input_chars": estimated_input_chars,
        "budget_preflight": budget_preflight,
        "loaded_secret_files": secrets["loaded_files"],
        "would_write": [
            str(task_dir / "raw" / "worker-output.md"),
            str(task_dir / "raw" / "worker-response.json"),
            str(task_dir / "completion-report.md"),
            str(task_dir / "status.json"),
        ],
    }
    if args.dry_run:
        print(json.dumps(redact(dry_run_payload), ensure_ascii=False, indent=2))
        return

    started = time.time()
    set_task_status(project_dir, args.task, "running", "unknown", model=model)
    try:
        payload = openai_chat_completion(agent_name, agent, messages, max_tokens, args.timeout)
        output = extract_openai_text(payload).strip()
        input_tokens, output_tokens = extract_usage_tokens(payload)
        wall_seconds = int(time.time() - started)
        if not output:
            raise RuntimeError("empty model output")
        safe_output = redact(output)
        atomic_write_text(task_dir / "raw" / "worker-output.md", str(safe_output) + "\n")
        atomic_write_json(task_dir / "raw" / "worker-response.json", payload)
        report = str(safe_output) if "# Completion Report" in output or "## Result" in output else "\n".join(
            [
                f"# Completion Report: {args.task}",
                "",
                "Status: done",
                f"Agent: {agent_name}",
                f"Task type: {card.get('task_type')}",
                "Confidence: unknown",
                "",
                "## Result",
                str(safe_output),
                "",
                "## Evidence",
                "- Generated by CostMarshal run-task.",
                "",
                "## Budget",
                f"- Wall time: {wall_seconds}s",
                f"- Input tokens: {input_tokens}",
                f"- Output tokens: {output_tokens}",
                "",
                "## Decisions Needed From Leader",
                "- Verify before accepting.",
                "",
                "## Escalation Reason",
                "",
                "## Suggested Merge Note",
                "",
            ]
        )
        atomic_write_text(task_dir / "completion-report.md", report.rstrip() + "\n")
        usage_row = record_usage_event(
            root,
            project_dir,
            args.task,
            agent_name,
            model,
            card,
            args.context_size,
            wall_seconds,
            input_tokens,
            output_tokens,
            "auto-run by CostMarshal run-task; pending leader verification",
        )
        worker_status = report_status(report, args.status_on_success)
        worker_confidence = report_confidence(report, args.confidence)
        status_result = set_task_status(project_dir, args.task, worker_status, worker_confidence, model=model)
        print(
            json.dumps(
                {
                    "ran": True,
                    "status": status_result,
                    "usage_event": usage_row,
                    "usage": {"input_tokens": input_tokens, "output_tokens": output_tokens},
                    "needs_leader_record_result": True,
                },
                ensure_ascii=False,
                indent=2,
            )
        )
    except urllib.error.HTTPError as exc:
        body = exc.read(2048).decode("utf-8", errors="replace")
        command_run_task_failure(root, project_dir, task_dir, args, agent_name, model, card, started, f"HTTP {exc.code}: {body}", "api_http_error")
    except Exception as exc:  # noqa: BLE001 - report model/provider failure as task failure
        command_run_task_failure(root, project_dir, task_dir, args, agent_name, model, card, started, f"{type(exc).__name__}: {exc}", "api_error")


def command_run_task_failure(
    root: Path,
    project_dir: Path,
    task_dir: Path,
    args: argparse.Namespace,
    agent_name: str,
    model: str,
    card: dict[str, Any],
    started: float,
    error: str,
    failure_type: str,
) -> None:
    wall_seconds = int(time.time() - started)
    safe_error = redact(error)
    atomic_write_text(task_dir / "raw" / "worker-error.md", str(safe_error) + "\n")
    atomic_write_text(
        task_dir / "completion-report.md",
        "\n".join(
            [
                f"# Completion Report: {args.task}",
                "",
                "Status: failed",
                f"Agent: {agent_name}",
                f"Task type: {card.get('task_type')}",
                "Confidence: low",
                "",
                "## Result",
                "The worker API call failed.",
                "",
                "## Evidence",
                f"- Error: {safe_error}",
                "",
                "## Budget",
                f"- Wall time: {wall_seconds}s",
                "- Input tokens: 0",
                "- Output tokens: 0",
                "",
                "## Decisions Needed From Leader",
                "- Check provider connectivity, quota, model name, or task routing.",
                "",
                "## Escalation Reason",
                f"- {failure_type}",
                "",
                "## Suggested Merge Note",
                "",
            ]
        ),
    )
    result = record_result_event(
        root,
        project_dir,
        argparse.Namespace(
            task=args.task,
            agent=agent_name,
            model=model,
            model_tier=card.get("agent_tier"),
            status="failed",
            confidence="low",
            task_type=card.get("task_type"),
            difficulty=card.get("difficulty"),
            risk=card.get("risk"),
            context_size=args.context_size,
            wall_seconds=wall_seconds,
            input_tokens=0,
            output_tokens=0,
            estimated_cost_cny=None,
            accepted_by_leader=False,
            accepted_by_senior=False,
            test_result="not_run",
            rework_count=0,
            failure_type=failure_type,
            quality_score=1,
            note=f"auto-run failed: {safe_error}",
        ),
    )
    print(json.dumps({"ran": False, "result": result, "error": safe_error}, ensure_ascii=False, indent=2))


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            rows.append(json.loads(line))
    return rows


def summarize_rows(rows: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    summary: dict[str, dict[str, Any]] = {}
    for row in rows:
        agent_name = row.get("agent", "unknown")
        item = summary.setdefault(
            agent_name,
            {
                "runs": 0,
                "tasks": 0,
                "accepted": 0,
                "escalated": 0,
                "quality": 0.0,
                "cost": 0.0,
                "input_tokens": 0,
                "output_tokens": 0,
                "total_tokens": 0,
            },
        )
        event_type = row.get("event_type", "result")
        if event_type == "usage" or row.get("input_tokens") or row.get("output_tokens"):
            item["runs"] += 1
        if event_type != "usage":
            item["tasks"] += 1
            if row.get("accepted_by_leader"):
                item["accepted"] += 1
            if row.get("needs_escalation"):
                item["escalated"] += 1
            item["quality"] += float(row.get("quality_score") or 0)
        item["cost"] += float(row.get("estimated_cost_cny") or 0)
        input_tokens = int(row.get("input_tokens") or 0)
        output_tokens = int(row.get("output_tokens") or 0)
        item["input_tokens"] += input_tokens
        item["output_tokens"] += output_tokens
        item["total_tokens"] += int(row.get("total_tokens") or input_tokens + output_tokens)
    for item in summary.values():
        tasks = item["tasks"]
        item["avg_quality"] = round(item["quality"] / tasks, 3) if tasks else 0.0
        item["accept_rate"] = round(item["accepted"] / tasks, 3) if tasks else 0.0
        item["estimated_cost_cny"] = round(item["cost"], 4)
    return summary


def format_count(value: int) -> str:
    return f"{value:,}"


def task_result_events(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [row for row in rows if row.get("event_type", "result") == "result"]


def evolution_bucket_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    buckets: dict[tuple[str, str], dict[str, Any]] = {}
    for row in task_result_events(rows):
        agent = row.get("agent", "unknown")
        task_type = row.get("task_type", "unknown")
        key = (agent, task_type)
        bucket = buckets.setdefault(
            key,
            {
                "agent": agent,
                "task_type": task_type,
                "tasks": 0,
                "accepted": 0,
                "escalated": 0,
                "quality_total": 0.0,
                "cost_total": 0.0,
                "input_tokens": 0,
                "output_tokens": 0,
            },
        )
        bucket["tasks"] += 1
        if row.get("accepted_by_leader"):
            bucket["accepted"] += 1
        if row.get("needs_escalation"):
            bucket["escalated"] += 1
        bucket["quality_total"] += float(row.get("quality_score") or 0)
        bucket["cost_total"] += float(row.get("estimated_cost_cny") or 0)
        bucket["input_tokens"] += int(row.get("input_tokens") or 0)
        bucket["output_tokens"] += int(row.get("output_tokens") or 0)
    result = []
    for bucket in buckets.values():
        tasks = max(1, bucket["tasks"])
        accept_rate = bucket["accepted"] / tasks
        avg_quality = bucket["quality_total"] / tasks
        if bucket["tasks"] < 2:
            recommendation = "collect_more_evidence"
            verification = "strict"
        elif accept_rate >= 0.85 and avg_quality >= 4.2:
            recommendation = "prefer_for_low_risk"
            verification = "relaxed"
        elif accept_rate >= 0.7 and avg_quality >= 3.5:
            recommendation = "use_with_standard_review"
            verification = "standard"
        elif accept_rate < 0.5 or bucket["escalated"]:
            recommendation = "avoid_or_escalate"
            verification = "strict"
        else:
            recommendation = "use_cautiously"
            verification = "strict"
        bucket["accept_rate"] = round(accept_rate, 3)
        bucket["avg_quality"] = round(avg_quality, 3)
        bucket["avg_cost_cny"] = round(bucket["cost_total"] / tasks, 6)
        bucket["recommendation"] = recommendation
        bucket["verification_mode"] = verification
        result.append(bucket)
    return sorted(result, key=lambda item: (item["task_type"], item["agent"]))


def result_by_task(rows: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    latest: dict[str, dict[str, Any]] = {}
    for row in task_result_events(rows):
        task_id = row.get("task_id")
        if task_id:
            latest[task_id] = row
    return latest


def knowledge_kind(task_type: str, report: str) -> str:
    bug_words = ["bug", "error", "failure", "failed", "fix", "regression", "exception", "traceback"]
    lowered = report.lower()
    if task_type in {"implementation", "verification"} or any(word in lowered for word in bug_words):
        return "common_bug_or_fix"
    if task_type in {"mechanical", "summarization"}:
        return "repeatable_procedure"
    return "general_solution"


def build_knowledge_lesson(
    project: dict[str, Any],
    project_dir: Path,
    task_dir: Path,
    card: dict[str, Any],
    row: dict[str, Any],
    report: str,
) -> dict[str, Any]:
    task_id = task_dir.name
    task_type = row.get("task_type") or card.get("task_type") or "unknown"
    title = card.get("title") or task_id
    lesson_id = slugify(f"{project['id']}-{task_id}-{title}", f"{project['id']}-{task_id}")
    kind = knowledge_kind(task_type, report)
    summary = completion_result_summary(report, card.get("purpose") or title)
    return {
        "id": lesson_id,
        "title": title,
        "kind": kind,
        "task_type": task_type,
        "source_project": project["id"],
        "source_task": task_id,
        "agent": row.get("agent"),
        "model": row.get("model"),
        "quality_score": row.get("quality_score"),
        "accepted_by_leader": row.get("accepted_by_leader"),
        "summary": summary,
        "purpose": card.get("purpose", ""),
        "replay_memory": card.get("replay_memory", []),
        "created_at": now_iso(),
        "path": f"memory/knowledge/{task_type}/{lesson_id}.md",
    }


def lesson_markdown(lesson: dict[str, Any]) -> str:
    replay = lesson.get("replay_memory") or []
    lines = [
        f"# Knowledge: {lesson['title']}",
        "",
        f"Kind: `{lesson['kind']}`",
        f"Task type: `{lesson['task_type']}`",
        f"Source project: `{lesson['source_project']}`",
        f"Source task: `{lesson['source_task']}`",
        f"Agent: `{lesson.get('agent') or 'unknown'}`",
        f"Model: `{lesson.get('model') or 'unknown'}`",
        f"Quality score: `{lesson.get('quality_score')}`",
        "",
        "## When To Use",
        lesson.get("purpose") or "Use when a future task matches this task type and problem pattern.",
        "",
        "## Problem Pattern",
        lesson.get("purpose") or lesson.get("title", ""),
        "",
        "## Reusable Solution Summary",
        lesson.get("summary") or "Leader should fill in a stronger reusable lesson after review.",
        "",
        "## Retrieval Boundary",
        "- Find this file through `memory/knowledge-index.json` by task type first.",
        "- Attach only this matching knowledge file to a future worker unless the leader approves broader context.",
        "- Prefer replay memory over this lesson when exact commands or parameters must be reproduced.",
        "",
        "## Related Replay Memory",
    ]
    lines.extend(f"- `{item}`" for item in replay) if replay else lines.append("- none")
    lines.extend(["", "## Leader Notes", "- Add a more general bug pattern or reusable fix here if the source task revealed one."])
    return "\n".join(lines) + "\n"


def upsert_knowledge_index(root: Path, lessons: list[dict[str, Any]]) -> dict[str, Any]:
    index_path = root / "memory" / "knowledge-index.json"
    index = read_json(index_path, empty_knowledge_index())
    categories = index.setdefault("categories", {})
    for lesson in lessons:
        category = categories.setdefault(
            lesson["task_type"],
            {"updated_at": now_iso(), "lesson_count": 0, "lessons": []},
        )
        existing = [item for item in category.get("lessons", []) if item.get("id") != lesson["id"]]
        entry = {
            "id": lesson["id"],
            "title": lesson["title"],
            "kind": lesson["kind"],
            "path": lesson["path"],
            "source_project": lesson["source_project"],
            "source_task": lesson["source_task"],
            "agent": lesson.get("agent"),
            "model": lesson.get("model"),
            "quality_score": lesson.get("quality_score"),
            "summary": lesson.get("summary"),
            "updated_at": now_iso(),
        }
        existing.insert(0, entry)
        category["lessons"] = existing[:50]
        category["lesson_count"] = len(category["lessons"])
        category["updated_at"] = now_iso()
    index["updated_at"] = now_iso()
    atomic_write_json(index_path, index)
    return index


def write_knowledge_lessons(root: Path, lessons: list[dict[str, Any]]) -> None:
    for lesson in lessons:
        path = root / lesson["path"]
        atomic_write_text(path, lesson_markdown(lesson))


def collect_knowledge_lessons(project: dict[str, Any], project_dir: Path, rows: list[dict[str, Any]], max_lessons: int, min_quality: int) -> list[dict[str, Any]]:
    by_task = result_by_task(rows)
    lessons = []
    for task_dir in sorted((project_dir / "tasks").glob("CM-*")):
        row = by_task.get(task_dir.name)
        if not row:
            continue
        if not row.get("accepted_by_leader") or int(row.get("quality_score") or 0) < min_quality:
            continue
        card = read_json(task_dir / "branch-card.json", {})
        report = read_text_if_exists(task_dir / "completion-report.md")
        lessons.append(build_knowledge_lesson(project, project_dir, task_dir, card, row, report))
    lessons.sort(key=lambda item: (int(item.get("quality_score") or 0), item.get("created_at", "")), reverse=True)
    return lessons[:max_lessons]


def render_evolution_report(project: dict[str, Any], buckets: list[dict[str, Any]], lessons: list[dict[str, Any]], replay_rows: list[dict[str, Any]]) -> str:
    lines = [
        f"# Evolution Report: {project['name']}",
        "",
        f"Project id: `{project['id']}`",
        f"Generated: {now_iso()}",
        "",
        "## Purpose",
        "Capture what this project taught CostMarshal about model routing, verification strictness, replay memory health, and reusable solution patterns.",
        "",
        "## Routing Evolution",
        "",
        "| Agent | Task Type | Tasks | Accept Rate | Avg Quality | Escalated | Avg Cost CNY | Recommendation | Verification |",
        "| --- | --- | ---: | ---: | ---: | ---: | ---: | --- | --- |",
    ]
    if buckets:
        for item in buckets:
            lines.append(
                f"| {table_cell(item['agent'])} | {table_cell(item['task_type'])} | {item['tasks']} | {item['accept_rate']} | {item['avg_quality']} | {item['escalated']} | {item['avg_cost_cny']} | {item['recommendation']} | {item['verification_mode']} |"
            )
    else:
        lines.append("| - | - | 0 | - | - | - | - | collect_more_evidence | strict |")
    lines.extend(["", "## Replay Memory Health", "", "| Name | Type | Status | Feedback | Avg Quality | Path |", "| --- | --- | --- | ---: | ---: | --- |"])
    if replay_rows:
        for row in replay_rows:
            lines.append(
                f"| {table_cell(row.get('name'))} | {table_cell(row.get('task_type'))} | {table_cell(row.get('status'))} | {row.get('feedback_count', 0)} | {table_cell(row.get('avg_feedback_quality'))} | {table_cell(row.get('path'))} |"
            )
    else:
        lines.append("| - | - | - | 0 | - | - |")
    lines.extend(["", "## Knowledge Candidates", "", "| Task Type | Kind | Title | Source | Path |", "| --- | --- | --- | --- | --- |"])
    if lessons:
        for lesson in lessons:
            lines.append(
                f"| {table_cell(lesson['task_type'])} | {table_cell(lesson['kind'])} | {table_cell(lesson['title'])} | {table_cell(lesson['source_task'])} | {table_cell(lesson['path'])} |"
            )
    else:
        lines.append("| - | - | - | - | - |")
    lines.extend(
        [
            "",
            "## Retrieval Cost Control",
            "- Future projects should read `memory/knowledge-index.json` first.",
            "- Select by task type and attach only the one most relevant knowledge file.",
            "- Prefer replay memory for exact repeatable procedures; use knowledge files for common problems and bug patterns.",
            "- Ask senior to refine a lesson before promoting it into broader reusable guidance.",
            "",
        ]
    )
    return "\n".join(lines)


def evolve_project(root: Path, project_dir: Path, max_lessons: int = 8, min_quality: int = 4, dry_run: bool = False) -> dict[str, Any]:
    ensure_root(root)
    project = load_project(project_dir)
    rows = read_jsonl(project_dir / "memory" / "model-performance.jsonl")
    buckets = evolution_bucket_rows(rows)
    lessons = collect_knowledge_lessons(project, project_dir, rows, max_lessons, min_quality)
    replay_rows = replay_memory_rows(project_dir)
    report_text = render_evolution_report(project, buckets, lessons, replay_rows)
    event = {
        "event_type": "project_evolution",
        "timestamp": now_iso(),
        "project_id": project["id"],
        "project": str(project_dir),
        "routing_buckets": len(buckets),
        "knowledge_lessons": len(lessons),
        "replay_memory_count": len(replay_rows),
        "report": "reports/evolution-report.md",
    }
    if not dry_run:
        atomic_write_text(project_dir / "reports" / "evolution-report.md", report_text)
        append_jsonl(project_dir / "memory" / "evolution-events.jsonl", event)
        append_jsonl(root / "memory" / "evolution-events.jsonl", event)
        write_knowledge_lessons(root, lessons)
        upsert_knowledge_index(root, lessons)
        policy = {
            "schema_version": SCHEMA_VERSION,
            "updated_at": now_iso(),
            "source_project": project["id"],
            "routing": buckets,
            "retrieval_policy": {
                "index": "memory/knowledge-index.json",
                "max_knowledge_files_per_task": 1,
                "attach_full_knowledge_only_after_task_type_match": True,
            },
        }
        atomic_write_json(root / "memory" / "evolution-policy.json", policy)
    return {"event": event, "routing": buckets, "lessons": lessons, "report_text": report_text}


def command_evolve_project(args: argparse.Namespace) -> None:
    root = args.root.resolve()
    project_dir = project_path(root, args.project)
    result = evolve_project(root, project_dir, args.max_lessons, args.min_quality, args.dry_run)
    payload = {
        "project": str(project_dir),
        "report": str(project_dir / "reports" / "evolution-report.md"),
        "routing_buckets": len(result["routing"]),
        "knowledge_lessons": len(result["lessons"]),
        "dry_run": args.dry_run,
    }
    if args.dry_run:
        payload["report_preview"] = result["report_text"]
    print(json.dumps(payload, ensure_ascii=False, indent=2))


def command_finish_project(args: argparse.Namespace) -> None:
    root = args.root.resolve()
    project_dir = project_path(root, args.project)
    project = load_project(project_dir)
    config = read_json(root / "config" / "agents.json", {})
    rows = read_jsonl(project_dir / "memory" / "model-performance.jsonl")
    summary = summarize_rows(rows)
    tasks = task_rows(project_dir, config)
    lines = [
        f"# Project Summary: {project['name']}",
        "",
        f"Project id: `{project['id']}`",
        f"Finished: {now_iso()}",
        "",
        "## Objective",
        project.get("objective", ""),
        "",
        "## Agent Performance",
        "",
        "| Agent | Runs | Reviewed Tasks | Accept Rate | Avg Quality | Escalated | Input Tokens | Output Tokens | Total Tokens | Est. Cost CNY |",
        "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    for agent_name, item in sorted(summary.items()):
        lines.append(
            f"| {agent_name} | {item['runs']} | {item['tasks']} | {item['accept_rate']} | {item['avg_quality']} | {item['escalated']} | {format_count(item['input_tokens'])} | {format_count(item['output_tokens'])} | {format_count(item['total_tokens'])} | {item['estimated_cost_cny']} |"
        )
    if summary:
        totals = {
            "runs": sum(item["runs"] for item in summary.values()),
            "tasks": sum(item["tasks"] for item in summary.values()),
            "input_tokens": sum(item["input_tokens"] for item in summary.values()),
            "output_tokens": sum(item["output_tokens"] for item in summary.values()),
            "total_tokens": sum(item["total_tokens"] for item in summary.values()),
            "estimated_cost_cny": round(sum(item["estimated_cost_cny"] for item in summary.values()), 4),
        }
        lines.append(
            f"| **Total** | {totals['runs']} | {totals['tasks']} |  |  |  | {format_count(totals['input_tokens'])} | {format_count(totals['output_tokens'])} | {format_count(totals['total_tokens'])} | {totals['estimated_cost_cny']} |"
        )
    lines.extend(
        [
            "",
            "## Task Ledger",
            "",
            "| Task | State | Agent | Model | Summary | Wait Time | Waits |",
            "| --- | --- | --- | --- | --- | ---: | ---: |",
        ]
    )
    if tasks:
        for task in tasks:
            lines.append(
                f"| {table_cell(task['task_id'])} | {table_cell(task.get('state'))} | {table_cell(task.get('agent'))} | {table_cell(task.get('model'))} | {table_cell(task.get('summary'))} | {table_cell(task.get('wait_elapsed'))} | {task.get('wait_count', 0)} |"
            )
    else:
        lines.append("| - | - | - | - | - | - | - |")
    lines.extend(
        [
            "",
            "## Notes",
            "- Add final leader conclusions here.",
            "- Promote repeatable successful flows into replay memory.",
            "- Tighten verification for agents or task types with low scores.",
            "",
        ]
    )
    atomic_write_text(project_dir / "reports" / "project-summary.md", "\n".join(lines))
    evolution_result = None
    if not getattr(args, "no_evolve", False):
        evolution_result = evolve_project(root, project_dir, getattr(args, "max_lessons", 8), getattr(args, "min_quality", 4))
    project["status"] = "completed"
    save_project(project_dir, project)
    print(
        json.dumps(
            {
                "project": str(project_dir),
                "summary": str(project_dir / "reports" / "project-summary.md"),
                "evolution": None if evolution_result is None else str(project_dir / "reports" / "evolution-report.md"),
            },
            indent=2,
        )
    )


def command_recommend(args: argparse.Namespace) -> None:
    root = args.root.resolve()
    ensure_root(root)
    config = read_json(root / "config" / "agents.json")
    memory = read_json(root / "memory" / "agent-memory.json", empty_memory())
    difficulty = args.difficulty
    risk = args.risk
    task_type = args.task_type
    replay_feedback_by_agent: dict[str, int] = {}
    replay_memory_status = None
    replay_memory_path = None
    if difficulty == "S" or risk == "high":
        preferred = ["senior"]
    elif task_type == "mechanical" and difficulty in {"B", "C"} and risk == "low":
        preferred = ["longcat", "deepseek", "kimi", "senior"]
    elif task_type in {"implementation", "review"}:
        preferred = ["kimi", "deepseek", "senior"]
    elif task_type in {"analysis", "verification", "research-search"}:
        preferred = ["deepseek", "kimi", "senior"]
    else:
        preferred = ["deepseek", "kimi", "longcat", "senior"]
    if args.project and args.replay_memory:
        project_dir = project_path(root, args.project)
        replay_memory_file = find_replay_memory(project_dir, args.replay_memory)
        replay_memory_path = replay_memory_file.relative_to(project_dir).as_posix()
        replay_metadata = read_json(replay_memory_file.parent / "metadata.json", {})
        replay_memory_status = replay_metadata.get("status")
        if replay_memory_status == "needs_revision":
            preferred = ["senior"]
        for row in read_jsonl(replay_memory_file.parent / "feedback.jsonl"):
            if row.get("attribution") == "agent_capability":
                agent = row.get("agent", "unknown")
                replay_feedback_by_agent[agent] = replay_feedback_by_agent.get(agent, 0) + 1
    candidates = []
    for name in preferred:
        agent_cfg = config.get("agents", {}).get(name, {})
        agent_mem = memory.get("agents", {}).get(name, {})
        candidates.append(
            {
                "agent": name,
                "tier": agent_cfg.get("tier"),
                "verification_mode": agent_mem.get("verification_mode", "strict"),
                "avg_quality": agent_mem.get("avg_quality"),
                "success_rate": agent_mem.get("success_rate"),
                "total_tasks": agent_mem.get("total_tasks", 0),
                "replay_agent_capability_failures": replay_feedback_by_agent.get(name, 0),
            }
        )
    if replay_feedback_by_agent and replay_memory_status != "needs_revision":
        candidates.sort(key=lambda item: (item.get("replay_agent_capability_failures", 0), preferred.index(item["agent"]) if item["agent"] in preferred else 99))
    verification_mode = "strict"
    if candidates and candidates[0].get("verification_mode") in {"standard", "relaxed"} and risk == "low":
        verification_mode = candidates[0]["verification_mode"]
    print(
        json.dumps(
            {
                "task_type": task_type,
                "difficulty": difficulty,
                "risk": risk,
                "project": args.project,
                "replay_memory": replay_memory_path,
                "replay_memory_status": replay_memory_status,
                "recommended": candidates[0]["agent"],
                "verification_mode": verification_mode,
                "candidates": candidates,
            },
            ensure_ascii=False,
            indent=2,
        )
    )


def table_cell(value: Any) -> str:
    text = "" if value is None else str(value)
    return text.replace("\n", " ").replace("|", "\\|").strip() or "-"


def compact_text(value: str, limit: int = 140) -> str:
    text = re.sub(r"\s+", " ", value).strip()
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 1)].rstrip() + "..."


def completion_result_summary(report: str, fallback: str) -> str:
    if not report.strip():
        return compact_text(fallback)
    match = re.search(r"(?ims)^##\s+Result\s*\n(?P<body>.*?)(?=^##\s+|\Z)", report)
    if match:
        candidates = match.group("body").splitlines()
    else:
        candidates = report.splitlines()
    for line in candidates:
        stripped = re.sub(r"^\s*[-*]\s*", "", line).strip()
        if not stripped or stripped.startswith("#") or stripped.lower().startswith(("status:", "agent:", "task type:", "confidence:")):
            continue
        return compact_text(stripped)
    return compact_text(fallback)


def configured_model(config: dict[str, Any], agent_name: str | None) -> str:
    if not agent_name:
        return "-"
    agent = config.get("agents", {}).get(agent_name)
    if not agent:
        return "-"
    return agent_model(agent)


def wait_summary_by_task(project_dir: Path) -> dict[str, dict[str, Any]]:
    summary: dict[str, dict[str, Any]] = {}
    for row in read_jsonl(project_dir / "memory" / "wait-events.jsonl"):
        if row.get("action") != "wait-task":
            continue
        task_id = row.get("task")
        if not task_id:
            continue
        item = summary.setdefault(
            task_id,
            {"count": 0, "elapsed_seconds": 0.0, "last_result": None, "last_task_state": None, "last_wait_at": None},
        )
        item["count"] += 1
        item["elapsed_seconds"] += float(row.get("elapsed_seconds") or 0.0)
        item["last_result"] = row.get("result")
        item["last_task_state"] = row.get("task_state")
        item["last_wait_at"] = row.get("timestamp")
    for item in summary.values():
        item["elapsed_seconds"] = round(item["elapsed_seconds"], 3)
        item["elapsed"] = format_seconds(item["elapsed_seconds"])
    return summary


def task_rows(project_dir: Path, config: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    config = config or {}
    waits = wait_summary_by_task(project_dir)
    rows = []
    for task_dir in sorted((project_dir / "tasks").glob("CM-*")):
        card = read_json(task_dir / "branch-card.json", {})
        status = read_json(task_dir / "status.json", {})
        agent_name = status.get("agent") or card.get("preferred_agent")
        report = read_text_if_exists(task_dir / "completion-report.md")
        wait = waits.get(task_dir.name, {})
        rows.append(
            {
                "task_id": task_dir.name,
                "title": card.get("title"),
                "agent": agent_name,
                "model": status.get("model") or configured_model(config, agent_name),
                "state": status.get("state"),
                "summary": completion_result_summary(report, card.get("purpose") or card.get("title") or ""),
                "wait_count": wait.get("count", 0),
                "wait_elapsed_seconds": wait.get("elapsed_seconds", 0.0),
                "wait_elapsed": wait.get("elapsed", "-"),
                "last_wait_result": wait.get("last_result"),
                "depends_on": card.get("depends_on", []),
                "claimed_paths": card.get("claimed_paths", []),
                "replay_memory": card.get("replay_memory", []),
            }
        )
    return rows


def replay_memory_rows(project_dir: Path) -> list[dict[str, Any]]:
    rows = []
    for metadata_path in sorted((project_dir / "memory" / "replay").glob("**/metadata.json")):
        metadata = read_json(metadata_path, {})
        rows.append(
            {
                "name": metadata.get("name"),
                "task_type": metadata.get("memory_task_type"),
                "status": metadata.get("status"),
                "avg_feedback_quality": metadata.get("avg_feedback_quality"),
                "feedback_count": metadata.get("feedback_count", 0),
                "path": (metadata_path.parent / "memory.md").relative_to(project_dir).as_posix(),
            }
        )
    return rows


def project_status_payload(root: Path, project_dir: Path) -> dict[str, Any]:
    project = load_project(project_dir)
    config = read_json(root / "config" / "agents.json", {})
    rows = project_cost_rows(project_dir)
    summary = summarize_rows(rows)
    claims = load_claims(project_dir)
    tasks = task_rows(project_dir, config)
    state_counts: dict[str, int] = {}
    for task in tasks:
        state = task.get("state", "unknown")
        state_counts[state] = state_counts.get(state, 0) + 1
    budget = project.get("budget") or {}
    spent = project_spend(project_dir)
    return {
        "project_id": project.get("id"),
        "name": project.get("name"),
        "status": project.get("status"),
        "objective": project.get("objective"),
        "plan_approval": project.get("plan_approval") or {},
        "budget": {
            **budget,
            "spent_cny": spent,
            "remaining_project_cny": None
            if budget.get("max_project_cost_cny") is None
            else round(float(budget.get("max_project_cost_cny")) - spent, 6),
        },
        "task_state_counts": state_counts,
        "tasks": tasks,
        "active_claims": [claim for claim in claims.get("claims", []) if claim.get("state") in ACTIVE_CLAIM_STATES],
        "agent_cost_summary": summary,
        "replay_memory": replay_memory_rows(project_dir),
    }


def command_status_project(args: argparse.Namespace) -> None:
    root = args.root.resolve()
    project_dir = project_path(root, args.project)
    payload = project_status_payload(root, project_dir)
    if args.format == "json":
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return
    lines = [
        f"# CostMarshal Project Status: {payload['name']}",
        "",
        f"Project id: `{payload['project_id']}`",
        f"Status: {payload['status']}",
        f"Plan approval: {(payload.get('plan_approval') or {}).get('status', 'unknown')}",
        f"Spent CNY: {payload['budget']['spent_cny']}",
        "",
        "## Task States",
    ]
    for state, count in sorted(payload["task_state_counts"].items()):
        lines.append(f"- {state}: {count}")
    lines.extend(["", "## Active Claims"])
    if payload["active_claims"]:
        lines.extend(f"- `{claim['path']}` by {claim['task_id']} ({claim.get('agent')})" for claim in payload["active_claims"])
    else:
        lines.append("- none")
    lines.extend(["", "## Replay Memory"])
    if payload["replay_memory"]:
        lines.append("| Name | Type | Status | Feedback | Avg Quality |")
        lines.append("| --- | --- | --- | ---: | ---: |")
        for row in payload["replay_memory"]:
            lines.append(
                f"| {row.get('name')} | {row.get('task_type')} | {row.get('status')} | {row.get('feedback_count')} | {row.get('avg_feedback_quality')} |"
            )
    else:
        lines.append("- none")
    lines.extend(["", "## Tasks"])
    lines.append("| Task | State | Agent | Model | Summary | Wait | Depends On | Claims | Replay Memory |")
    lines.append("| --- | --- | --- | --- | --- | ---: | --- | --- | --- |")
    for task in payload["tasks"]:
        lines.append(
            f"| {table_cell(task['task_id'])} | {table_cell(task.get('state'))} | {table_cell(task.get('agent'))} | {table_cell(task.get('model'))} | {table_cell(task.get('summary'))} | {table_cell(task.get('wait_elapsed'))} | {table_cell(', '.join(task.get('depends_on') or []) or '-')} | {table_cell(', '.join(task.get('claimed_paths') or []) or '-')} | {table_cell(', '.join(task.get('replay_memory') or []) or '-')} |"
        )
    print("\n".join(lines))


def command_validate(args: argparse.Namespace) -> None:
    root = args.root.resolve()
    issues = []
    for rel in ["projects", "memory", "config", "memory/agent-memory.json", "memory/events.jsonl", "config/agents.json"]:
        if not (root / rel).exists():
            issues.append(f"missing {rel}")
    if args.project:
        project_dir = project_path(root, args.project)
        for rel in ["project.json", "master-snapshot.md", "plan-approval.md", "branch-tree.json", "branch-tree.md", "tasks", "memory/model-performance.jsonl"]:
            if not (project_dir / rel).exists():
                issues.append(f"project missing {rel}")
        tree = read_json(project_dir / "branch-tree.json", {"nodes": []})
        for node in tree.get("nodes", []):
            if node.get("kind") == "task":
                task_dir = project_dir / node.get("path", f"tasks/{node['id']}")
                for rel in ["branch-card.json", "brief.md", "status.json", "completion-report.md"]:
                    if not (task_dir / rel).exists():
                        issues.append(f"{node['id']} missing {rel}")
    status = "ok" if not issues else "invalid"
    print(json.dumps({"status": status, "issues": issues}, ensure_ascii=False, indent=2))
    if issues:
        raise SystemExit(1)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="CostMarshal state manager")
    parser.add_argument("--root", type=Path, default=default_root(), help="CostMarshal root directory")
    sub = parser.add_subparsers(dest="command", required=True)

    init_root = sub.add_parser("init-root", help="Create global memory/config directories")
    init_root.set_defaults(func=command_init_root)

    new_project = sub.add_parser("new-project", help="Create a project directory")
    new_project.add_argument("--name", default="")
    new_project.add_argument("--objective", required=True)
    new_project.add_argument("--kind", choices=["general", "arbor", "feynman", "autoresearch", "research"], default="general")
    new_project.add_argument("--max-project-cost-cny", type=float, default=20.0)
    new_project.add_argument("--max-agent-cost-cny", type=float)
    new_project.set_defaults(func=command_new_project)

    draft_plan = sub.add_parser("draft-plan", help="Write a lightweight leader direction check and predictions for user confirmation")
    draft_plan.add_argument("--project", required=True)
    draft_plan.add_argument("--summary", required=True)
    draft_plan.add_argument("--step", action="append", help="Optional coarse project step")
    draft_plan.add_argument("--task", action="append", help="Optional predicted first-round worker task")
    draft_plan.add_argument("--agent-plan", action="append", help="Optional initial agent allocation")
    draft_plan.add_argument("--predicted-cost-cny", type=float)
    draft_plan.add_argument("--predicted-wall-time")
    draft_plan.add_argument("--predicted-input-tokens", type=int)
    draft_plan.add_argument("--predicted-output-tokens", type=int)
    draft_plan.add_argument("--acceptance", action="append")
    draft_plan.add_argument("--verification", action="append")
    draft_plan.add_argument("--risk", action="append")
    draft_plan.add_argument("--open-question", action="append")
    draft_plan.add_argument("--force", action="store_true")
    draft_plan.set_defaults(func=command_draft_plan)

    approve_plan = sub.add_parser("approve-plan", help="Mark the project plan as user-approved")
    approve_plan.add_argument("--project", required=True)
    approve_plan.add_argument("--approved-by", default="user")
    approve_plan.add_argument("--note")
    approve_plan.set_defaults(func=command_approve_plan)

    check_agents = sub.add_parser("check-agents", help="Check configured agents")
    check_agents.add_argument("--project", help="Project path or id to store connectivity results")
    check_agents.add_argument("--agents", help="Comma-separated agent names")
    check_agents.add_argument("--live", action="store_true", help="Call provider APIs using configured base URLs")
    check_agents.add_argument("--secrets-file", type=Path, help="Optional env file with local provider keys")
    check_agents.add_argument("--timeout", type=float, default=20.0)
    check_agents.set_defaults(func=command_check_agents)

    sleep_parser = sub.add_parser("sleep", help="Quiet embedded WakeWait-style sleep")
    sleep_parser.add_argument("--duration", default="", help="Duration such as 30s, 10m, or 1h")
    sleep_parser.add_argument("--seconds", type=int, default=0)
    sleep_parser.set_defaults(func=command_sleep)

    wait_file = sub.add_parser("wait-file", help="Wait until a file or directory exists")
    wait_file.add_argument("--path", type=Path, required=True)
    wait_file.add_argument("--every", default="30s")
    wait_file.add_argument("--timeout", default="1h")
    wait_file.set_defaults(func=command_wait_file)

    wait_contains = sub.add_parser("wait-contains", help="Wait until a file contains text")
    wait_contains.add_argument("--path", type=Path, required=True)
    wait_contains.add_argument("--text", required=True)
    wait_contains.add_argument("--every", default="30s")
    wait_contains.add_argument("--timeout", default="1h")
    wait_contains.set_defaults(func=command_wait_contains)

    wait_command = sub.add_parser("wait-command", help="Wait until a shell command exits with status 0")
    wait_command.add_argument("--command", required=True)
    wait_command.add_argument("--every", default="30s")
    wait_command.add_argument("--timeout", default="1h")
    wait_command.set_defaults(func=command_wait_command)

    wait_task = sub.add_parser("wait-task", help="Wait until a CostMarshal task reaches a terminal state")
    wait_task.add_argument("--project", required=True)
    wait_task.add_argument("--task", required=True)
    wait_task.add_argument("--states", default="done,failed,escalate")
    wait_task.add_argument("--every", default="30s")
    wait_task.add_argument("--timeout", default="1h")
    wait_task.set_defaults(func=command_wait_task)

    run_task = sub.add_parser("run-task", help="Run a task with an OpenAI-compatible configured agent")
    run_task.add_argument("--project", required=True)
    run_task.add_argument("--task", required=True)
    run_task.add_argument("--agent", help="Override the task's preferred agent")
    run_task.add_argument("--secrets-file", type=Path, help="Optional env file with local provider keys")
    run_task.add_argument("--dry-run", action="store_true", help="Show provider, model, context size, and outputs without calling the API")
    run_task.add_argument("--force", action="store_true", help="Rerun a task that is not planned/failed/escalate")
    run_task.add_argument("--timeout", type=float, default=120.0)
    run_task.add_argument("--max-output-tokens", type=int)
    run_task.add_argument("--max-context-chars", type=int, default=120000)
    run_task.add_argument("--allow-budget-overrun", action="store_true", help="Allow preflight estimated cost to exceed task/project budget")
    run_task.add_argument("--allow-raw-context", action="store_true", help="Allow explicitly listed raw/ context files")
    run_task.add_argument("--status-on-success", choices=["done", "escalate"], default="done")
    run_task.add_argument("--confidence", choices=["unknown", "low", "medium", "high"], default="unknown")
    run_task.add_argument("--context-size", choices=["small", "medium", "large", "huge"], default="medium")
    run_task.set_defaults(func=command_run_task)

    new_task = sub.add_parser("new-task", help="Create a task branch")
    new_task.add_argument("--project", required=True)
    new_task.add_argument("--id")
    new_task.add_argument("--title", required=True)
    new_task.add_argument("--parent", default="root")
    new_task.add_argument("--agent", default="auto")
    new_task.add_argument("--agent-tier", choices=["high", "medium", "low", "auto"], default="auto")
    new_task.add_argument("--difficulty", choices=sorted(DIFFICULTIES), default="A")
    new_task.add_argument("--risk", choices=sorted(RISKS), default="medium")
    new_task.add_argument("--task-type", choices=sorted(TASK_TYPES), default="analysis")
    new_task.add_argument("--purpose", required=True)
    new_task.add_argument("--acceptance", action="append")
    new_task.add_argument("--allowed-context", action="append")
    new_task.add_argument("--replay-memory", action="append", help="Attach a project replay memory file from memory/replay")
    new_task.add_argument("--project-skill", action="append", help="Deprecated alias for --replay-memory")
    new_task.add_argument("--depends-on", action="append", help="Task id that must be done before this task can run")
    new_task.add_argument("--allowed-path", action="append")
    new_task.add_argument("--claim-path", action="append", help="File or directory path this task claims for writing")
    new_task.add_argument("--allow-lock-conflict", action="store_true", help="Allow overlapping claimed paths after leader review")
    new_task.add_argument("--allow-unapproved-plan", action="store_true", help="Manual override when the user explicitly approved outside CostMarshal state")
    new_task.add_argument("--write-scope", choices=["none", "single_file", "disjoint_files", "coupled_files", "unknown"], default="none")
    new_task.add_argument("--command", action="append")
    new_task.add_argument("--escalate-if", action="append")
    new_task.add_argument("--max-wall-minutes", type=int, default=20)
    new_task.add_argument("--max-input-tokens", type=int, default=50000)
    new_task.add_argument("--max-output-tokens", type=int, default=4000)
    new_task.add_argument("--max-cost-cny", type=float, default=0.5)
    new_task.set_defaults(func=command_new_task)

    promote = sub.add_parser("promote-memory", help="Promote a proven senior task into a project replay memory file")
    promote.add_argument("--project", required=True)
    promote.add_argument("--source-task", required=True)
    promote.add_argument("--name", required=True)
    promote.add_argument("--title")
    promote.add_argument("--agent", default="senior")
    promote.add_argument("--memory-task-type", choices=sorted(TASK_TYPES))
    promote.add_argument("--summary", default="")
    promote.add_argument("--working-dir")
    promote.add_argument("--required-input", action="append")
    promote.add_argument("--dependency", action="append")
    promote.add_argument("--allowed-param", action="append")
    promote.add_argument("--allowed-command", action="append")
    promote.add_argument("--expected-output", action="append")
    promote.add_argument("--success-marker", action="append")
    promote.add_argument("--verification-command", action="append")
    promote.add_argument("--forbidden-change", action="append")
    promote.add_argument(
        "--failure-mode",
        default="Stop, report the blocker, and escalate instead of debugging freely.",
    )
    promote.add_argument("--draft", action="store_true", help="Allow placeholder replay memory; not eligible for weak-agent replay")
    promote.add_argument("--force", action="store_true")
    promote.set_defaults(func=command_promote_memory)

    promote_skill = sub.add_parser("promote-skill", help="Deprecated alias for promote-memory")
    promote_skill.add_argument("--project", required=True)
    promote_skill.add_argument("--source-task", required=True)
    promote_skill.add_argument("--name", required=True)
    promote_skill.add_argument("--title")
    promote_skill.add_argument("--agent", default="senior")
    promote_skill.add_argument("--memory-task-type", choices=sorted(TASK_TYPES))
    promote_skill.add_argument("--summary", default="")
    promote_skill.add_argument("--working-dir")
    promote_skill.add_argument("--required-input", action="append")
    promote_skill.add_argument("--dependency", action="append")
    promote_skill.add_argument("--allowed-param", action="append")
    promote_skill.add_argument("--allowed-command", action="append")
    promote_skill.add_argument("--expected-output", action="append")
    promote_skill.add_argument("--success-marker", action="append")
    promote_skill.add_argument("--verification-command", action="append")
    promote_skill.add_argument("--forbidden-change", action="append")
    promote_skill.add_argument(
        "--failure-mode",
        default="Stop, report the blocker, and escalate instead of debugging freely.",
    )
    promote_skill.add_argument("--draft", action="store_true", help="Allow placeholder replay memory; not eligible for weak-agent replay")
    promote_skill.add_argument("--force", action="store_true")
    promote_skill.set_defaults(func=command_promote_memory)

    memory_feedback = sub.add_parser("record-memory-feedback", help="Record replay memory quality feedback from a replay task")
    memory_feedback.add_argument("--project", required=True)
    memory_feedback.add_argument("--task", required=True)
    memory_feedback.add_argument("--memory", help="Replay memory name or path; inferred when the task used exactly one memory")
    memory_feedback.add_argument("--agent")
    memory_feedback.add_argument("--outcome", choices=["succeeded", "failed", "partial"], required=True)
    memory_feedback.add_argument("--sufficient", choices=["yes", "partial", "no"], required=True)
    memory_feedback.add_argument("--memory-quality", type=int, choices=[1, 2, 3, 4, 5], required=True)
    memory_feedback.add_argument("--attribution", choices=sorted(MEMORY_FEEDBACK_ATTRIBUTIONS), default="unknown")
    memory_feedback.add_argument("--needs-senior-refresh", action="store_true")
    memory_feedback.add_argument("--issue")
    memory_feedback.add_argument("--suggestion")
    memory_feedback.add_argument("--note")
    memory_feedback.set_defaults(func=command_record_memory_feedback)

    handoff = sub.add_parser("record-handoff", help="Create a compressed task-to-task handoff document")
    handoff.add_argument("--project", required=True)
    handoff.add_argument("--source-task", required=True)
    handoff.add_argument("--target-task")
    handoff.add_argument("--id")
    handoff.add_argument("--summary", required=True)
    handoff.add_argument("--decision", action="append")
    handoff.add_argument("--artifact", action="append")
    handoff.add_argument("--risk-note", action="append")
    handoff.add_argument("--next-step", action="append")
    handoff.add_argument("--force", action="store_true")
    handoff.set_defaults(func=command_record_handoff)

    review_task = sub.add_parser("new-review-task", help="Create a bounded review task for another agent's output")
    review_task.add_argument("--project", required=True)
    review_task.add_argument("--source-task", required=True)
    review_task.add_argument("--reviewer", default="kimi")
    review_task.add_argument("--id")
    review_task.add_argument("--title")
    review_task.add_argument("--purpose")
    review_task.add_argument("--parent")
    review_task.add_argument("--agent-tier", choices=["high", "medium", "low", "auto"], default="medium")
    review_task.add_argument("--difficulty", choices=sorted(DIFFICULTIES), default="B")
    review_task.add_argument("--risk", choices=sorted(RISKS), default="medium")
    review_task.add_argument("--acceptance", action="append")
    review_task.add_argument("--allowed-context", action="append")
    review_task.add_argument("--command", action="append")
    review_task.add_argument("--escalate-if", action="append")
    review_task.add_argument("--max-wall-minutes", type=int, default=15)
    review_task.add_argument("--max-input-tokens", type=int, default=30000)
    review_task.add_argument("--max-output-tokens", type=int, default=3000)
    review_task.add_argument("--max-cost-cny", type=float, default=0.3)
    review_task.add_argument("--allow-unapproved-plan", action="store_true")
    review_task.set_defaults(func=command_new_review_task)

    set_status = sub.add_parser("set-status", help="Set task status and signal files")
    set_status.add_argument("--project", required=True)
    set_status.add_argument("--task", required=True)
    set_status.add_argument("--state", choices=sorted(TASK_STATES), required=True)
    set_status.add_argument("--confidence", choices=["unknown", "low", "medium", "high"])
    set_status.add_argument("--error")
    set_status.set_defaults(func=command_set_status)

    record = sub.add_parser("record-result", help="Record project/global model performance")
    record.add_argument("--project", required=True)
    record.add_argument("--task", required=True)
    record.add_argument("--agent")
    record.add_argument("--model", help="Concrete model name used for this result, if known")
    record.add_argument("--model-tier")
    record.add_argument("--status", choices=["done", "failed", "escalate"], required=True)
    record.add_argument("--confidence", choices=["unknown", "low", "medium", "high"], default="unknown")
    record.add_argument("--task-type")
    record.add_argument("--difficulty")
    record.add_argument("--risk")
    record.add_argument("--context-size", choices=["small", "medium", "large", "huge"], default="medium")
    record.add_argument("--wall-seconds", type=int, default=0)
    record.add_argument("--input-tokens", type=int, default=0)
    record.add_argument("--output-tokens", type=int, default=0)
    record.add_argument("--estimated-cost-cny", type=float)
    record.add_argument("--accepted-by-leader", action="store_true")
    record.add_argument("--accepted-by-senior", action="store_true")
    record.add_argument("--test-result", default="not_run")
    record.add_argument("--rework-count", type=int, default=0)
    record.add_argument("--failure-type")
    record.add_argument("--quality-score", type=int, choices=[1, 2, 3, 4, 5], required=True)
    record.add_argument("--note")
    record.set_defaults(func=command_record_result)

    finish = sub.add_parser("finish-project", help="Create project summary")
    finish.add_argument("--project", required=True)
    finish.add_argument("--no-evolve", action="store_true", help="Skip project evolution report and global knowledge update")
    finish.add_argument("--max-lessons", type=int, default=8, help="Maximum accepted task lessons to promote into the knowledge index")
    finish.add_argument("--min-quality", type=int, choices=[1, 2, 3, 4, 5], default=4)
    finish.set_defaults(func=command_finish_project)

    evolve = sub.add_parser("evolve-project", help="Update routing evolution, knowledge index, and project evolution report")
    evolve.add_argument("--project", required=True)
    evolve.add_argument("--max-lessons", type=int, default=8)
    evolve.add_argument("--min-quality", type=int, choices=[1, 2, 3, 4, 5], default=4)
    evolve.add_argument("--dry-run", action="store_true")
    evolve.set_defaults(func=command_evolve_project)

    recommend = sub.add_parser("recommend", help="Recommend an agent from global memory")
    recommend.add_argument("--task-type", choices=sorted(TASK_TYPES), required=True)
    recommend.add_argument("--difficulty", choices=sorted(DIFFICULTIES), required=True)
    recommend.add_argument("--risk", choices=sorted(RISKS), required=True)
    recommend.add_argument("--project", help="Project path or id for replay-memory feedback-aware routing")
    recommend.add_argument("--replay-memory", help="Replay memory name or path for feedback-aware routing")
    recommend.set_defaults(func=command_recommend)

    status_project = sub.add_parser("status-project", help="Show project dashboard with tasks, locks, budget, and replay memory health")
    status_project.add_argument("--project", required=True)
    status_project.add_argument("--format", choices=["json", "md"], default="md")
    status_project.set_defaults(func=command_status_project)

    validate = sub.add_parser("validate", help="Validate root and optional project structure")
    validate.add_argument("--project")
    validate.set_defaults(func=command_validate)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    args.func(args)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
