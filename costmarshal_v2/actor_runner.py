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
from .locking import project_write_lock
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


def report_status(report: Path) -> str | None:
    if not report.is_file():
        return None
    text = report.read_text(encoding="utf-8", errors="replace")
    match = re.search(r"(?im)^\s*(?:[*_]{1,2})?status\s*:\s*(done|failed|escalate)\b", text)
    return match.group(1).lower() if match else None


def run_actor(layout: ProjectLayout, actor_id: str) -> int:
    with project_write_lock(layout):
        actor = load_actor(layout, actor_id)
        project = load_project(layout)
    prompt = (layout.project_dir / str(actor.get("prompt_path") or "")).resolve()
    expected_prompt = actor_prompt_file(layout, actor_id).resolve()
    try:
        ensure_workspace_containment(layout.project_dir, prompt, must_exist=True)
    except SecurityValidationError as exc:
        raise SystemExit(f"Actor prompt is outside the runtime: {exc}") from exc
    if prompt != expected_prompt or not prompt.is_file():
        raise SystemExit(f"Actor prompt not found: {prompt}")
    report = report_path(layout, actor)
    report.parent.mkdir(parents=True, exist_ok=True)
    execution_workspace, sandbox, write_scopes, base_sha = actor_execution_workspace(layout, project, actor)
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
    with project_write_lock(layout):
        current_actor = load_actor(layout, actor_id)
        current_actor["status"] = "running"
        current_actor.setdefault("runtime", {})["execution_workspace"] = str(execution_workspace)
        current_actor["runtime"]["sandbox"] = sandbox
        save_actor(layout, current_actor)
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
        )
    try:
        with prompt.open("r", encoding="utf-8") as prompt_handle:
            process = subprocess.Popen(
                process_argv(argv),
                cwd=str(execution_workspace),
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
    if task_id:
        publish_task_report(layout, actor, report)
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
                "attempt_id": actor.get("attempt_id"),
                "execution_workspace": str(execution_workspace),
                "changed_paths": list(changed_paths),
            },
        )
        scheduler_command(
            layout,
            actor,
            "record_usage",
            {
                "actor": actor_id,
                "task": task_id,
                "attempt": actor.get("attempt_id"),
                "model": actor.get("model"),
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
                "final_usage": True,
                "note": f"provider={actor.get('provider')} profile={actor.get('profile') or '-'} exit={returncode}",
            },
            body="Record usage captured from codex exec JSONL.",
        )
        needs_escalation = returncode != 0 or final_report_status in {"failed", "escalate"}
        actor_tier = actor.get("tier") or ("low" if actor.get("provider") == "longcat" else "high")
        if actor_tier in {"low", "medium"} and needs_escalation and project.get("auto_escalate", True):
            scheduler_command(
                layout,
                actor,
                "escalate_task",
                {
                    "task": task_id,
                    "actor": actor_id,
                    "attempt": actor.get("attempt_id"),
                    "reason": f"{actor.get('provider')} ({actor.get('tier')}) attempt requested escalation or exited {returncode}",
                    "start": True,
                },
                body="Escalate this bounded task to the next stronger provider tier.",
            )
        else:
            scheduler_command(
                layout,
                actor,
                "collect_task",
                {
                    "task": task_id,
                    "actor": actor_id,
                    "attempt": actor.get("attempt_id"),
                    "state": collected_state,
                    "summary": f"{actor.get('provider')} worker exited {returncode}; report ready.",
                },
                body="Worker report is ready for manager review.",
            )

    with project_write_lock(layout):
        current_actor = load_actor(layout, actor_id)
        current_actor["status"] = "stopped" if returncode == 0 else "failed"
        current_actor.setdefault("runtime", {})["exit_code"] = returncode
        current_actor["runtime"]["finished_at"] = now_iso()
        current_actor["runtime"]["changed_paths"] = list(changed_paths)
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
