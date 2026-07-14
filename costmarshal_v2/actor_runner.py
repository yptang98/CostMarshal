from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any, Iterable

from .mailbox import send_message
from .paths import ProjectLayout, resolve_project
from .state import (
    SCHEMA_VERSION,
    append_event,
    atomic_write_json,
    atomic_write_text,
    load_actor,
    load_project,
    load_task,
    now_iso,
    save_actor,
    task_dir,
)


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
    configured = project.get("workspace") or project.get("source_project")
    if configured and Path(str(configured)).is_dir():
        return Path(str(configured)).expanduser().resolve()
    return layout.project_dir


def report_path(layout: ProjectLayout, actor: dict[str, Any]) -> Path:
    task_id = actor.get("task_id")
    if task_id:
        return task_dir(layout, str(task_id)) / "completion-report.md"
    return layout.reports_dir / "manager-latest.md"


def build_codex_argv(layout: ProjectLayout, actor: dict[str, Any], project: dict[str, Any], report: Path) -> list[str]:
    runner = actor.get("runner") or {}
    workspace = workspace_path(layout, project)
    argv = resolve_codex_command(actor) + [
        "--ask-for-approval",
        str(runner.get("approval_policy") or "never"),
        "exec",
        "--ephemeral",
        "--skip-git-repo-check",
        "--sandbox",
        str(runner.get("sandbox") or "workspace-write"),
        "--cd",
        str(workspace),
        "--add-dir",
        str(layout.project_dir),
        "--json",
        "--output-last-message",
        str(report),
    ]
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
    send_message(
        layout,
        sender=actor["id"],
        recipient="scheduler",
        subject="scheduler.command",
        body=body,
        task_id=actor.get("task_id"),
        metadata={"command": command, "args": args},
    )


def report_status(report: Path) -> str | None:
    if not report.is_file():
        return None
    text = report.read_text(encoding="utf-8", errors="replace")
    match = re.search(r"(?im)^\s*(?:[*_]{1,2})?status\s*:\s*(done|failed|escalate)\b", text)
    return match.group(1).lower() if match else None


def run_actor(layout: ProjectLayout, actor_id: str) -> int:
    actor = load_actor(layout, actor_id)
    project = load_project(layout)
    prompt = layout.project_dir / str(actor.get("prompt_path") or "")
    if not prompt.is_file():
        raise SystemExit(f"Actor prompt not found: {prompt}")
    report = report_path(layout, actor)
    report.parent.mkdir(parents=True, exist_ok=True)
    argv = build_codex_argv(layout, actor, project, report)
    env = load_env_file(default_secrets_file(project), dict(os.environ))
    events: list[dict[str, Any]] = []
    actor["status"] = "running"
    save_actor(layout, actor)
    append_event(
        layout,
        "actor_exec_started",
        actor_id=actor_id,
        task_id=actor.get("task_id"),
        provider=actor.get("provider"),
        profile=actor.get("profile"),
        model=actor.get("model"),
    )
    try:
        with prompt.open("r", encoding="utf-8") as prompt_handle:
            process = subprocess.Popen(
                process_argv(argv),
                cwd=str(workspace_path(layout, project)),
                env=env,
                stdin=prompt_handle,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
            )
            assert process.stdout is not None
            for line in process.stdout:
                sys.stdout.write(line)
                sys.stdout.flush()
                try:
                    payload = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(payload, dict):
                    events.append(payload)
            returncode = process.wait()
    except OSError as exc:
        returncode = 127
        atomic_write_text(report, f"# Completion Report\n\nStatus: failed\n\n## Result\n{type(exc).__name__}: {exc}\n")

    input_tokens, output_tokens = usage_from_events(events)
    task_id = actor.get("task_id")
    if task_id:
        final_report_status = report_status(report)
        if returncode != 0:
            collected_state = "failed"
        elif final_report_status in {"failed", "escalate"}:
            collected_state = final_report_status
        else:
            collected_state = "waiting_leader"
        atomic_write_json(
            task_dir(layout, str(task_id)) / "status.json",
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
            },
        )
        scheduler_command(
            layout,
            actor,
            "record_usage",
            {
                "actor": actor_id,
                "task": task_id,
                "model": actor.get("model"),
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
                "note": f"provider={actor.get('provider')} profile={actor.get('profile') or '-'} exit={returncode}",
            },
            body="Record usage captured from codex exec JSONL.",
        )
        needs_escalation = returncode != 0 or final_report_status in {"failed", "escalate"}
        if actor.get("provider") == "longcat" and needs_escalation and project.get("auto_escalate", True):
            scheduler_command(
                layout,
                actor,
                "escalate_task",
                {"task": task_id, "reason": f"LongCat attempt requested escalation or exited {returncode}", "start": True},
                body="Escalate this bounded task from LongCat to Codex.",
            )
        else:
            scheduler_command(
                layout,
                actor,
                "collect_task",
                {
                    "task": task_id,
                    "actor": actor_id,
                    "state": collected_state,
                    "summary": f"{actor.get('provider')} worker exited {returncode}; report ready.",
                },
                body="Worker report is ready for manager review.",
            )

    actor["status"] = "stopped" if returncode == 0 else "failed"
    actor.setdefault("runtime", {})["exit_code"] = returncode
    actor["runtime"]["finished_at"] = now_iso()
    save_actor(layout, actor)
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
    )
    return returncode


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run one CostMarshal actor through codex exec")
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--project", required=True)
    parser.add_argument("--actor", required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    layout = resolve_project(args.root, args.project)
    return run_actor(layout, args.actor)


if __name__ == "__main__":
    raise SystemExit(main())
