#!/usr/bin/env python3
"""Attempt launch tokens and lifetime locks prevent duplicate provider execution."""

from __future__ import annotations

import hashlib
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
ACTOR = ROOT / "scripts" / "costmarshal_actor.py"
sys.path.insert(0, str(ROOT))

from costmarshal_v2.paths import resolve_project  # noqa: E402
from costmarshal_v2.scheduler import default_actor_argv  # noqa: E402


def cli(temp: Path, *args: str, ok: bool = True) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env["COSTMARSHAL_V2_HOME"] = str(temp / "runtime")
    env["CODEX_HOME"] = str(temp / "codex-home")
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
    if not ok and not result.returncode:
        raise AssertionError(f"command unexpectedly succeeded: {args}\n{result.stdout}")
    return result


def cli_json(temp: Path, *args: str) -> dict:
    return json.loads(cli(temp, *args).stdout)


def main() -> int:
    temp = Path(tempfile.mkdtemp(prefix="costmarshal-v2-actor-fence-"))
    previous_codex_home = os.environ.get("CODEX_HOME")
    try:
        codex_home = temp / "codex-home"
        os.environ["CODEX_HOME"] = str(codex_home)
        configured = cli_json(
            temp,
            "configure-profiles",
            "--codex-home",
            str(codex_home),
        )
        assert configured["profile"] == "longcat"
        assert Path(configured["path"]).is_file()

        workspace = temp / "workspace"
        workspace.mkdir()
        project = Path(
            cli_json(
                temp,
                "init",
                "--name",
                "actor-fence",
                "--objective",
                "Prove one provider process per attempt",
                "--workspace",
                str(workspace),
                "--backend",
                "local",
                "--governance",
                "off",
                "--allow-unsafe-native-workers",
            )["project"]
        )
        cli_json(
            temp,
            "new-task",
            "--project",
            str(project),
            "--title",
            "fenced",
            "--purpose",
            "test duplicate launch fencing",
            "--risk",
            "low",
        )

        preview = cli_json(temp, "dispatch", "--project", str(project), "--task", "V2-0001", "--start", "--dry-run", "--unsafe-native")
        assert preview["actor"]["launch_token"] == "<redacted>"
        assert "token_urlsafe" not in json.dumps(preview)

        dispatched = cli_json(temp, "dispatch", "--project", str(project), "--task", "V2-0001", "--unsafe-native")
        actor_id = dispatched["actor_id"]
        actor_file = project / "scheduler" / "actors" / f"{actor_id}.json"
        actor = json.loads(actor_file.read_text(encoding="utf-8"))
        task_file = project / "tasks" / "V2-0001" / "task.json"
        task_state = json.loads(task_file.read_text(encoding="utf-8"))
        task_launch_token = task_state["attempts"][0]["launch_token"]
        launch_token = "-leading-url-safe-launch-token"
        actor["launch_token"] = launch_token
        actor_file.write_text(
            json.dumps(actor, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )

        for command_name in ("status", "dashboard"):
            public_state = cli(
                temp,
                command_name,
                "--project",
                str(project),
                "--format",
                "json",
            ).stdout
            assert task_launch_token not in public_state
            assert launch_token not in public_state

        counter = temp / "provider-invocations.txt"
        ready = temp / "provider-ready"
        fake = temp / "fake_codex.py"
        fake.write_text(
            "\n".join(
                [
                    "import json, pathlib, sys, time",
                    f"counter = pathlib.Path({str(counter)!r})",
                    f"ready = pathlib.Path({str(ready)!r})",
                    "with counter.open('a', encoding='utf-8') as handle: handle.write('invoked\\n')",
                    "ready.touch()",
                    "output = pathlib.Path(sys.argv[sys.argv.index('--output-last-message') + 1])",
                    "time.sleep(2.0)",
                    "output.write_text('# Completion Report\\n\\nStatus: done\\n\\n## Result\\nfenced\\n', encoding='utf-8')",
                    "print(json.dumps({'usage': {'input_tokens': 3, 'output_tokens': 2}}))",
                ]
            )
            + "\n",
            encoding="utf-8",
        )
        layout = resolve_project(temp / "runtime", str(project))
        command = default_actor_argv(layout, actor)
        assert command[0] == sys.executable
        assert command[1] == str(ACTOR)
        assert f"--launch-token={launch_token}" in command
        assert "--launch-token" not in command
        env = os.environ.copy()
        env["COSTMARSHAL_V2_HOME"] = str(temp / "runtime")
        env["CODEX_HOME"] = str(codex_home)
        env["COSTMARSHAL_CODEX_COMMAND_JSON"] = json.dumps([sys.executable, str(fake)])

        wrong = subprocess.run(
            command[:-1] + ["--launch-token=wrong-token"],
            text=True,
            capture_output=True,
            env=env,
            check=False,
        )
        assert wrong.returncode != 0
        assert "launch token mismatch" in (wrong.stdout + wrong.stderr)
        assert not counter.exists()

        first = subprocess.Popen(command, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env)
        deadline = time.monotonic() + 10
        while not ready.exists() and time.monotonic() < deadline:
            if first.poll() is not None:
                stdout, stderr = first.communicate()
                raise AssertionError(f"first runner exited before provider start\n{stdout}\n{stderr}")
            time.sleep(0.05)
        assert ready.exists(), "provider did not start"

        duplicate = subprocess.run(command, text=True, capture_output=True, env=env, check=False, timeout=5)
        assert duplicate.returncode != 0
        duplicate_error = duplicate.stdout + duplicate.stderr
        assert (
            "another runner owns this attempt" in duplicate_error
            or "prior provider execution outcome is unknown" in duplicate_error
        )
        stdout, stderr = first.communicate(timeout=10)
        assert first.returncode == 0, f"first runner failed\n{stdout}\n{stderr}"
        assert counter.read_text(encoding="utf-8").splitlines() == ["invoked"]

        actor = json.loads(actor_file.read_text(encoding="utf-8"))
        registered = actor["runtime"]["registered_launch_token_sha256"]
        assert registered == hashlib.sha256(launch_token.encode("utf-8")).hexdigest()
        actor["status"] = "running"
        actor.setdefault("runtime", {})["pid"] = 999_999_999
        actor["runtime"]["provider_execution_state"] = "finished"
        actor_file.write_text(
            json.dumps(actor, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        recovery_output = cli(
            temp,
            "recover",
            "--project",
            str(project),
            "--plan-restarts",
        ).stdout
        assert launch_token not in recovery_output
        assert task_launch_token not in recovery_output
        assert "<launch-token-redacted>" in recovery_output
        print("actor fencing ok")
        return 0
    finally:
        if previous_codex_home is None:
            os.environ.pop("CODEX_HOME", None)
        else:
            os.environ["CODEX_HOME"] = previous_codex_home
        shutil.rmtree(temp, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
