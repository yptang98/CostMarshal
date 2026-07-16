from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
CLI = ROOT / "scripts" / "costmarshal.py"


def run(temp: Path, env: dict[str, str], *args: str, expect_ok: bool = True) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        [sys.executable, str(CLI), "--root", str(temp / "runtime"), *args],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
        check=False,
    )
    if expect_ok and result.returncode != 0:
        raise AssertionError(f"command failed: {args}\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}")
    return result


def run_json(temp: Path, env: dict[str, str], *args: str) -> dict:
    return json.loads(run(temp, env, *args).stdout)


def wait_until(predicate, timeout: float = 10.0) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return
        time.sleep(0.1)
    raise AssertionError("timed out waiting for actor process")


def review_and_escalate(temp: Path, env: dict[str, str], project: Path, sequence: int) -> dict:
    task = json.loads((project / "tasks" / "V2-0001" / "task.json").read_text(encoding="utf-8"))
    attempt = task["attempts"][-1]
    actor = str(attempt["actor_id"])
    attempt_id = str(attempt["attempt_id"])
    run_json(
        temp,
        env,
        "collect",
        "--command-id",
        f"CMD-rotation-collect-{sequence}",
        "--project",
        str(project),
        "--task",
        "V2-0001",
        "--attempt",
        attempt_id,
        "--actor",
        actor,
        "--state",
        "escalate",
    )
    run_json(
        temp,
        env,
        "record-result",
        "--command-id",
        f"CMD-rotation-result-{sequence}",
        "--project",
        str(project),
        "--task",
        "V2-0001",
        "--attempt",
        attempt_id,
        "--actor",
        actor,
        "--status",
        "escalate",
        "--quality-score",
        "3",
        "--summary",
        f"leader rejected rotation attempt {sequence}",
    )
    return run_json(
        temp,
        env,
        "escalate",
        "--command-id",
        f"CMD-rotation-escalate-{sequence}",
        "--project",
        str(project),
        "--task",
        "V2-0001",
        "--attempt",
        attempt_id,
        "--from-actor",
        actor,
        "--reason",
        f"leader rejected rotation attempt {sequence}",
        "--start",
        "--unsafe-native",
    )


def main() -> int:
    temp = Path(tempfile.mkdtemp(prefix="costmarshal-v2-model-rotation-"))
    try:
        workspace = temp / "workspace"
        workspace.mkdir()
        fake_log = temp / "fake-codex.jsonl"
        fake_codex = temp / "fake_codex.py"
        fake_codex.write_text(
            """from __future__ import annotations
import json
import sys
from pathlib import Path

args = sys.argv[1:]
assert args.index('--ask-for-approval') < args.index('exec'), args
profile = args[args.index('--profile') + 1] if '--profile' in args else 'codex-default'
model = args[args.index('--model') + 1] if '--model' in args else 'inherit'
report = Path(args[args.index('--output-last-message') + 1])
prompt = sys.stdin.read()
with Path(__file__).with_name('fake-codex.jsonl').open('a', encoding='utf-8') as handle:
    handle.write(json.dumps({'profile': profile, 'model': model, 'prompt_seen': 'Assigned Task' in prompt}) + '\\n')
if profile in {'longcat', 'deepseek'}:
    report.write_text('# Completion Report\\n\\nStatus: escalate\\n\\n## Result\\nNeed stronger reasoning.\\n', encoding='utf-8')
else:
    report.write_text('# Completion Report\\n\\nStatus: done\\n\\n## Result\\nCodex completed escalation.\\n', encoding='utf-8')
print(json.dumps({'type': 'turn.completed', 'usage': {'input_tokens': 11, 'output_tokens': 7}}))
""",
            encoding="utf-8",
        )
        env = dict(os.environ)
        env["COSTMARSHAL_CODEX_COMMAND_JSON"] = json.dumps([sys.executable, str(fake_codex)])
        # The built-in high tier is credential-bound.  Keep this contract test
        # hermetic while exercising the provider rotation rather than host auth.
        env["CODEX_API_KEY"] = "rotation-test-codex-key"

        profile_home = temp / "codex-home"
        configured = run_json(temp, env, "configure-profiles", "--codex-home", str(profile_home))
        assert configured["profile"] == "longcat"
        profile_text = (profile_home / "longcat.config.toml").read_text(encoding="utf-8")
        assert 'env_key = "LONGCAT_API_KEY"' in profile_text
        assert "experimental_bearer_token" not in profile_text
        run_json(
            temp,
            env,
            "configure-provider",
            "--codex-home",
            str(profile_home),
            "--profile",
            "deepseek",
            "--provider-id",
            "deepseek",
            "--base-url",
            "https://api.deepseek.example/v1",
            "--model",
            "deepseek-test",
            "--env-key",
            "DEEPSEEK_API_KEY",
        )
        env["CODEX_HOME"] = str(profile_home)

        init = run_json(
            temp,
            env,
            "init",
            "--name",
            "rotation",
            "--objective",
            "Prove low to medium to high rotation",
            "--workspace",
            str(workspace),
            "--backend",
            "local",
            "--allow-unsafe-native-workers",
        )
        project = Path(init["project"])
        run_json(
            temp,
            env,
            "new-task",
            "--project",
            str(project),
            "--title",
            "Bounded mechanical task",
            "--purpose",
            "Exercise deterministic provider escalation",
            "--task-type",
            "mechanical",
            "--provider",
            "auto",
        )
        dispatched = run_json(temp, env, "dispatch", "--project", str(project), "--task", "V2-0001", "--start", "--unsafe-native")
        assert dispatched["actor_id"] == "agent-v2-0001"

        try:
            wait_until(lambda: fake_log.is_file() and len(fake_log.read_text(encoding="utf-8").splitlines()) >= 1)
        except AssertionError as exc:
            logs = list((project / "transcripts").glob("*.log"))
            detail = "\n".join(path.read_text(encoding="utf-8", errors="replace") for path in logs)
            raise AssertionError(f"timed out waiting for actor process\n{detail}") from exc
        # Drain the runner-authored usage/escalation request. The latter is
        # intentionally rejected until the leader records explicit evidence.
        first_scheduler = run_json(temp, env, "run-scheduler", "--project", str(project), "--once")
        review_and_escalate(temp, env, project, 1)
        try:
            wait_until(lambda: fake_log.is_file() and len(fake_log.read_text(encoding="utf-8").splitlines()) >= 2)
        except AssertionError as exc:
            logs = list((project / "transcripts").glob("*.log"))
            detail = "\n".join(path.read_text(encoding="utf-8", errors="replace") for path in logs)
            task_detail = (project / "tasks" / "V2-0001" / "task.json").read_text(encoding="utf-8")
            raise AssertionError(
                f"timed out waiting for medium actor\nscheduler={first_scheduler}\n{detail}\n{task_detail}"
            ) from exc
        run_json(temp, env, "run-scheduler", "--project", str(project), "--once")
        review_and_escalate(temp, env, project, 2)
        wait_until(lambda: fake_log.is_file() and len(fake_log.read_text(encoding="utf-8").splitlines()) >= 3)
        run_json(temp, env, "run-scheduler", "--project", str(project), "--once")

        rows = [json.loads(line) for line in fake_log.read_text(encoding="utf-8").splitlines() if line.strip()]
        assert [row["profile"] for row in rows[:3]] == ["longcat", "deepseek", "codex-default"], rows
        assert all(row["prompt_seen"] for row in rows[:3]), rows
        task = json.loads((project / "tasks" / "V2-0001" / "task.json").read_text(encoding="utf-8"))
        assert [attempt["provider"] for attempt in task["attempts"]] == ["longcat", "deepseek", "codex"], task["attempts"]
        assert [attempt["tier"] for attempt in task["attempts"]] == ["low", "medium", "high"], task["attempts"]
        assert task["status"] == "waiting_leader", task
        attempt_reports = sorted((project / "tasks" / "V2-0001" / "attempts").glob("*.md"))
        assert len(attempt_reports) == 3, attempt_reports
        attempt_texts = [path.read_text(encoding="utf-8") for path in attempt_reports]
        assert any("Status: escalate" in text for text in attempt_texts), attempt_texts
        assert any("Status: done" in text for text in attempt_texts), attempt_texts
        assert "Codex completed escalation" in (project / "tasks" / "V2-0001" / "completion-report.md").read_text(encoding="utf-8")
        usage = [json.loads(line) for line in (project / "reports" / "usage.jsonl").read_text(encoding="utf-8").splitlines() if line.strip()]
        assert {row["model"] for row in usage} >= {"LongCat-2.0", "inherit"}, usage
        print(json.dumps({"status": "ok", "profiles": [row["profile"] for row in rows[:3]]}, indent=2))
        return 0
    finally:
        shutil.rmtree(temp, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
