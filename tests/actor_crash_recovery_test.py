#!/usr/bin/env python3
"""A post-provider runner crash imports the report without re-running the API."""

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
sys.path.insert(0, str(ROOT))

from costmarshal_v2.session_backend import pid_is_alive  # noqa: E402


def run(
    temp: Path,
    *args: str,
    env_extra: dict[str, str] | None = None,
    ok: bool = True,
) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env["COSTMARSHAL_V2_HOME"] = str(temp / "runtime")
    env["CODEX_HOME"] = str(temp / "codex-home")
    env.update(env_extra or {})
    result = subprocess.run(
        [sys.executable, str(CLI), "--root", str(temp / "runtime"), *args],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
        check=False,
    )
    if ok and result.returncode:
        raise AssertionError(f"command failed: {args}\n{result.stdout}\n{result.stderr}")
    return result


def run_json(temp: Path, *args: str, env_extra: dict[str, str] | None = None) -> dict:
    return json.loads(run(temp, *args, env_extra=env_extra).stdout)


def main() -> int:
    temp = Path(tempfile.mkdtemp(prefix="costmarshal-v2-actor-crash-recovery-"))
    previous_codex_home = os.environ.get("CODEX_HOME")
    try:
        codex_home = temp / "codex-home"
        os.environ["CODEX_HOME"] = str(codex_home)
        configured = run_json(
            temp,
            "configure-profiles",
            "--codex-home",
            str(codex_home),
        )
        assert configured["profile"] == "longcat"
        assert Path(configured["path"]).is_file()

        workspace = temp / "workspace"
        workspace.mkdir()
        counter = temp / "provider-count.txt"
        fake = temp / "fake_codex.py"
        fake.write_text(
            "\n".join(
                [
                    "import json, pathlib, sys",
                    f"counter = pathlib.Path({str(counter)!r})",
                    "with counter.open('a', encoding='utf-8') as handle: handle.write('once\\n')",
                    "output = pathlib.Path(sys.argv[sys.argv.index('--output-last-message') + 1])",
                    "output.write_text('# Completion Report\\n\\nStatus: done\\n\\n## Result\\ncrash-safe\\n', encoding='utf-8')",
                    "print(json.dumps({'type': 'status', 'payload': {'input_tokens': 0, 'output_tokens': 0}}))",
                ]
            )
            + "\n",
            encoding="utf-8",
        )
        project = Path(
            run_json(
                temp,
                "init",
                "--name",
                "actor-crash",
                "--objective",
                "recover provider completion",
                "--workspace",
                str(workspace),
                "--backend",
                "local",
                "--governance",
                "off",
                "--allow-unsafe-native-workers",
            )["project"]
        )
        run_json(temp, "new-task", "--project", str(project), "--title", "crash", "--purpose", "recover report")
        canonical_report = project / "tasks" / "V2-0001" / "completion-report.md"
        canonical_before = canonical_report.read_bytes()
        child_env = {
            "COSTMARSHAL_CODEX_COMMAND_JSON": json.dumps([sys.executable, str(fake)]),
            "COSTMARSHAL_ACTOR_FAULT": "after_attempt_report_before_publish",
        }
        dispatched = run_json(
            temp,
            "dispatch",
            "--project",
            str(project),
            "--task",
            "V2-0001",
            "--start",
            "--unsafe-native",
            env_extra=child_env,
        )
        pid = int(dispatched["start"]["pid"])
        report = project / "tasks" / "V2-0001" / "attempts" / "agent-v2-0001.md"
        deadline = time.monotonic() + 15
        while (not report.is_file() or pid_is_alive(pid)) and time.monotonic() < deadline:
            time.sleep(0.1)
        assert report.is_file() and not pid_is_alive(pid)
        assert counter.read_text(encoding="utf-8").splitlines() == ["once"]
        assert canonical_report.read_bytes() == canonical_before

        recovered = run_json(temp, "recover", "--project", str(project))
        assert recovered["recovered_reports"] == ["agent-v2-0001"]
        assert canonical_report.read_text(encoding="utf-8") == report.read_text(encoding="utf-8"), (
            canonical_report.read_text(encoding="utf-8"),
            report.read_text(encoding="utf-8"),
        )
        second = run_json(temp, "recover", "--project", str(project))
        assert second["recovered_reports"] == []
        assert counter.read_text(encoding="utf-8").splitlines() == ["once"]

        cycle = run_json(temp, "run-scheduler", "--project", str(project), "--once")
        assert cycle["processed_commands"] == 1
        task = json.loads((project / "tasks" / "V2-0001" / "task.json").read_text(encoding="utf-8"))
        assert task["status"] == "waiting_leader"
        assert task["attempts"][-1]["report_sha256"]
        usage_rows = [
            line
            for line in (project / "reports" / "usage.jsonl").read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        assert usage_rows == [], usage_rows
        assert run_json(temp, "validate", "--project", str(project))["status"] == "ok"
        provider_calls = len(counter.read_text(encoding="utf-8").splitlines())
        assert provider_calls == 1
        print("actor crash recovery ok")
        print(
            "COSTMARSHAL_RUNTIME_EVIDENCE="
            + json.dumps(
                {
                    "schema_version": 1,
                    "test": "tests/actor_crash_recovery_test.py",
                    "crash_points": ["after_attempt_report_before_publish"],
                    "recovery_scenarios": [],
                    "provider_calls": provider_calls,
                    "expected_provider_calls": 1,
                    "orphan_effects": 0,
                },
                sort_keys=True,
            )
        )
        return 0
    finally:
        if previous_codex_home is None:
            os.environ.pop("CODEX_HOME", None)
        else:
            os.environ["CODEX_HOME"] = previous_codex_home
        shutil.rmtree(temp, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
