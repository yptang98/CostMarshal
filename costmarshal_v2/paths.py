from __future__ import annotations

import os
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path


@dataclass(frozen=True)
class ProjectLayout:
    root: Path
    project_dir: Path

    @property
    def project_json(self) -> Path:
        return self.project_dir / "project.json"

    @property
    def scheduler_dir(self) -> Path:
        return self.project_dir / "scheduler"

    @property
    def session_json(self) -> Path:
        return self.scheduler_dir / "session.json"

    @property
    def events_jsonl(self) -> Path:
        return self.scheduler_dir / "events.jsonl"

    @property
    def relay_cursors_json(self) -> Path:
        return self.scheduler_dir / "relay-cursors.json"

    @property
    def scheduler_state_json(self) -> Path:
        return self.scheduler_dir / "state.json"

    @property
    def actors_dir(self) -> Path:
        return self.scheduler_dir / "actors"

    @property
    def mailboxes_dir(self) -> Path:
        return self.scheduler_dir / "mailboxes"

    @property
    def tasks_dir(self) -> Path:
        return self.project_dir / "tasks"

    @property
    def reports_dir(self) -> Path:
        return self.project_dir / "reports"

    @property
    def results_jsonl(self) -> Path:
        return self.reports_dir / "results.jsonl"

    @property
    def leader_work_jsonl(self) -> Path:
        return self.reports_dir / "leader-work.jsonl"

    @property
    def usage_jsonl(self) -> Path:
        return self.reports_dir / "usage.jsonl"

    @property
    def transcripts_dir(self) -> Path:
        return self.project_dir / "transcripts"

    @property
    def locks_json(self) -> Path:
        return self.project_dir / "locks" / "claims.json"

    @property
    def protocol_md(self) -> Path:
        return self.project_dir / "PROTOCOL.md"


def default_root() -> Path:
    env_root = os.environ.get("COSTMARSHAL_V2_HOME")
    if env_root:
        return Path(env_root).expanduser().resolve()
    codex_home = os.environ.get("CODEX_HOME")
    if codex_home:
        return (Path(codex_home).expanduser() / "costmarshal-v2").resolve()
    return (Path.home() / ".codex" / "costmarshal-v2").resolve()


def slugify(text: str, fallback: str = "item") -> str:
    text = text.strip().lower()
    text = re.sub(r"[^a-z0-9._-]+", "-", text)
    text = re.sub(r"-+", "-", text).strip("-._")
    return text[:72] or fallback


def make_project_id(name: str, objective: str) -> str:
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    slug = slugify(name or objective[:48], "project")
    return f"{stamp}-{slug}"


def actor_runtime_name(actor_id: str) -> str:
    return slugify(actor_id, "actor")[:48]


def actor_target(session_name: str, actor_id: str) -> str:
    return f"{session_name}:{actor_runtime_name(actor_id)}"


def actor_window_name(actor_id: str) -> str:
    return actor_runtime_name(actor_id)


def relpath(path: Path, base: Path) -> str:
    try:
        return path.resolve().relative_to(base.resolve()).as_posix()
    except ValueError:
        return str(path.resolve())


def resolve_project(root: Path, project_arg: str) -> ProjectLayout:
    root = root.expanduser().resolve()
    candidate = Path(project_arg).expanduser()
    if candidate.exists() and candidate.is_dir():
        project_dir = candidate.resolve()
        if not (project_dir / "project.json").is_file():
            raise SystemExit(f"Not a CostMarshal v2 project: {project_dir}")
        return ProjectLayout(root=root, project_dir=project_dir)

    matches = sorted((root / "projects").glob(f"*{project_arg}*"))
    matches = [path for path in matches if (path / "project.json").is_file()]
    if len(matches) == 1:
        return ProjectLayout(root=root, project_dir=matches[0].resolve())
    if not matches:
        raise SystemExit(f"Project not found in CostMarshal v2 root: {project_arg}")
    raise SystemExit("Project is ambiguous:\n" + "\n".join(str(path) for path in matches))
