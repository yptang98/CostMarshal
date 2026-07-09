from __future__ import annotations

import json
import os
import re
import tempfile
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .paths import ProjectLayout, relpath, slugify


SCHEMA_VERSION = 2
TASK_STATES = {"planned", "dispatched", "running", "waiting_leader", "done", "failed", "escalate", "cancelled"}
TERMINAL_TASK_STATES = {"done", "failed", "escalate", "cancelled"}
ACTOR_STATES = {"configured", "starting", "running", "idle", "waiting", "needs_recovery", "stopped", "failed"}
ACTIVE_TASK_STATES = {"planned", "dispatched", "running", "waiting_leader"}
TASK_STATE_TRANSITIONS = {
    "planned": {"dispatched", "cancelled"},
    "dispatched": {"running", "waiting_leader", "done", "failed", "escalate", "cancelled"},
    "running": {"waiting_leader", "done", "failed", "escalate", "cancelled"},
    "waiting_leader": {"done", "failed", "escalate", "cancelled"},
    "done": set(),
    "failed": set(),
    "escalate": set(),
    "cancelled": set(),
}


def now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def new_id(prefix: str) -> str:
    return f"{prefix}-{datetime.now().strftime('%Y%m%d%H%M%S')}-{uuid.uuid4().hex[:8]}"


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


def atomic_write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", delete=False, dir=str(path.parent)) as handle:
        handle.write(content)
        temp_name = handle.name
    os.replace(temp_name, path)


def atomic_write_json(path: Path, data: Any) -> None:
    atomic_write_text(path, json.dumps(redact(data), ensure_ascii=False, indent=2) + "\n")


def read_json(path: Path, default: Any | None = None) -> Any:
    if not path.exists():
        if default is not None:
            return default
        raise FileNotFoundError(path)
    return json.loads(path.read_text(encoding="utf-8"))


def append_jsonl(path: Path, row: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(redact(row), ensure_ascii=False, sort_keys=True) + "\n")


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    rows: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            rows.append(json.loads(line))
    return rows


def ensure_runtime_dirs(layout: ProjectLayout) -> None:
    for path in [
        layout.project_dir,
        layout.scheduler_dir,
        layout.actors_dir,
        layout.mailboxes_dir,
        layout.tasks_dir,
        layout.reports_dir,
        layout.transcripts_dir,
        layout.project_dir / "artifacts",
        layout.project_dir / "locks",
    ]:
        path.mkdir(parents=True, exist_ok=True)
    if not layout.events_jsonl.exists():
        layout.events_jsonl.touch()
    if not layout.results_jsonl.exists():
        layout.results_jsonl.touch()
    if not layout.leader_work_jsonl.exists():
        layout.leader_work_jsonl.touch()
    if not layout.usage_jsonl.exists():
        layout.usage_jsonl.touch()
    if not layout.relay_cursors_json.exists():
        atomic_write_json(
            layout.relay_cursors_json,
            {"schema_version": SCHEMA_VERSION, "updated_at": now_iso(), "actors": {}},
        )
    if not layout.scheduler_state_json.exists():
        atomic_write_json(
            layout.scheduler_state_json,
            {
                "schema_version": SCHEMA_VERSION,
                "id": "scheduler",
                "role": "scheduler",
                "status": "idle",
                "pid": None,
                "started_at": None,
                "heartbeat_at": None,
                "last_cycle_at": None,
                "cycle_count": 0,
                "processed_commands": 0,
            },
        )
    if not layout.locks_json.exists():
        atomic_write_json(
            layout.locks_json,
            {"schema_version": SCHEMA_VERSION, "updated_at": now_iso(), "claims": []},
        )


def actor_file(layout: ProjectLayout, actor_id: str) -> Path:
    return layout.actors_dir / f"{slugify(actor_id, 'actor')}.json"


def actor_prompt_file(layout: ProjectLayout, actor_id: str) -> Path:
    return layout.actors_dir / f"{slugify(actor_id, 'actor')}.prompt.md"


def actor_exists(layout: ProjectLayout, actor_id: str) -> bool:
    return actor_file(layout, actor_id).is_file()


def task_dir(layout: ProjectLayout, task_id: str) -> Path:
    return layout.tasks_dir / task_id


def task_json(layout: ProjectLayout, task_id: str) -> Path:
    return task_dir(layout, task_id) / "task.json"


def task_exists(layout: ProjectLayout, task_id: str) -> bool:
    return task_json(layout, task_id).is_file()


def load_project(layout: ProjectLayout) -> dict[str, Any]:
    return read_json(layout.project_json)


def save_project(layout: ProjectLayout, project: dict[str, Any]) -> None:
    project["updated_at"] = now_iso()
    atomic_write_json(layout.project_json, project)


def load_session(layout: ProjectLayout) -> dict[str, Any]:
    return read_json(layout.session_json)


def save_session(layout: ProjectLayout, session: dict[str, Any]) -> None:
    session["updated_at"] = now_iso()
    atomic_write_json(layout.session_json, session)


def load_actor(layout: ProjectLayout, actor_id: str) -> dict[str, Any]:
    return read_json(actor_file(layout, actor_id))


def save_actor(layout: ProjectLayout, actor: dict[str, Any]) -> None:
    actor["updated_at"] = now_iso()
    atomic_write_json(actor_file(layout, actor["id"]), actor)


def load_task(layout: ProjectLayout, task_id: str) -> dict[str, Any]:
    return read_json(task_json(layout, task_id))


def save_task(layout: ProjectLayout, task: dict[str, Any]) -> None:
    task["updated_at"] = now_iso()
    atomic_write_json(task_json(layout, task["id"]), task)


def append_event(layout: ProjectLayout, event_type: str, **fields: Any) -> dict[str, Any]:
    row = {"id": new_id("EVT"), "timestamp": now_iso(), "event_type": event_type, **fields}
    append_jsonl(layout.events_jsonl, row)
    return row


def mailbox_dir(layout: ProjectLayout, actor_id: str) -> Path:
    return layout.mailboxes_dir / slugify(actor_id, "actor")


def ensure_mailbox(layout: ProjectLayout, actor_id: str) -> dict[str, str]:
    box = mailbox_dir(layout, actor_id)
    box.mkdir(parents=True, exist_ok=True)
    inbox = box / "inbox.jsonl"
    outbox = box / "outbox.jsonl"
    if not inbox.exists():
        inbox.touch()
    if not outbox.exists():
        outbox.touch()
    return {
        "dir": relpath(box, layout.project_dir),
        "inbox": relpath(inbox, layout.project_dir),
        "outbox": relpath(outbox, layout.project_dir),
    }


def mailbox_counts(layout: ProjectLayout, actor_id: str) -> dict[str, int]:
    box = mailbox_dir(layout, actor_id)
    return {
        "inbox": len(read_jsonl(box / "inbox.jsonl")),
        "outbox": len(read_jsonl(box / "outbox.jsonl")),
    }


def next_task_id(layout: ProjectLayout) -> str:
    existing: list[int] = []
    for path in layout.tasks_dir.glob("V2-*"):
        match = re.match(r"V2-(\d+)$", path.name)
        if match:
            existing.append(int(match.group(1)))
    return f"V2-{(max(existing) if existing else 0) + 1:04d}"


def compact_text(value: str, limit: int = 160) -> str:
    text = re.sub(r"\s+", " ", value).strip()
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 1)].rstrip() + "..."


def can_transition_task(current: str | None, target: str) -> bool:
    if current is None or current == target:
        return True
    return target in TASK_STATE_TRANSITIONS.get(current, set())
