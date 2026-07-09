#!/usr/bin/env python3
"""Contract test for v2's tmux backend using a fake tmux executable.

This exercises non-dry-run start, send, stop, and recovery paths without
requiring tmux to be installed on the development machine.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
CLI = ROOT / "scripts" / "costmarshal.py"


FAKE_TMUX = r'''
from __future__ import annotations

import json
import sys
from pathlib import Path


STATE = Path(__file__).with_suffix(".state.json")


def load() -> dict:
    if STATE.exists():
        return json.loads(STATE.read_text(encoding="utf-8"))
    return {"sessions": {}, "send_keys": []}


def save(state: dict) -> None:
    STATE.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def option(args: list[str], name: str) -> str:
    try:
        return args[args.index(name) + 1]
    except (ValueError, IndexError):
        raise SystemExit(f"missing {name}")


def command_tail(args: list[str], name: str) -> str:
    try:
        index = args.index(name) + 2
    except ValueError:
        return ""
    return " ".join(args[index + 1 :])


def main() -> int:
    args = sys.argv[1:]
    if not args:
        return 2
    state = load()
    cmd = args[0]
    if cmd == "has-session":
        session = option(args, "-t")
        return 0 if session in state["sessions"] else 1
    if cmd == "new-session":
        session = option(args, "-s")
        window = option(args, "-n")
        command = args[-1]
        state["sessions"].setdefault(session, {"windows": {}})["windows"][window] = {"command": command}
        save(state)
        return 0
    if cmd == "new-window":
        session = option(args, "-t")
        window = option(args, "-n")
        command = args[-1]
        if session not in state["sessions"]:
            print(f"missing session {session}", file=sys.stderr)
            return 1
        state["sessions"][session]["windows"][window] = {"command": command}
        save(state)
        return 0
    if cmd == "list-windows":
        session = option(args, "-t")
        for window in sorted(state["sessions"].get(session, {}).get("windows", {})):
            print(window)
        return 0
    if cmd == "send-keys":
        target = option(args, "-t")
        text = args[-2] if len(args) >= 2 else ""
        state.setdefault("send_keys", []).append({"target": target, "text": text})
        save(state)
        return 0
    if cmd == "kill-window":
        target = option(args, "-t")
        session, _, window = target.partition(":")
        state["sessions"].get(session, {}).get("windows", {}).pop(window, None)
        save(state)
        return 0
    print(f"unsupported fake tmux command: {cmd}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
'''


def run(root: Path, *args: str, expect_ok: bool = True) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env["COSTMARSHAL_V2_HOME"] = str(root)
    result = subprocess.run(
        [sys.executable, str(CLI), "--root", str(root), *args],
        cwd=str(ROOT),
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
        check=False,
    )
    if expect_ok and result.returncode != 0:
        raise AssertionError(
            f"Command failed: {' '.join(args)}\nSTDOUT:\n{result.stdout}\nSTDERR:\n{result.stderr}"
        )
    if not expect_ok and result.returncode == 0:
        raise AssertionError(f"Command unexpectedly succeeded: {' '.join(args)}\nSTDOUT:\n{result.stdout}")
    return result


def run_json(root: Path, *args: str) -> dict:
    return json.loads(run(root, *args).stdout)


def assert_true(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def make_fake_tmux(temp: Path) -> tuple[Path, Path]:
    fake_py = temp / "fake_tmux.py"
    fake_py.write_text(FAKE_TMUX, encoding="utf-8")
    fake_cmd = temp / "fake_tmux.cmd"
    fake_cmd.write_text(f'@echo off\r\n"{sys.executable}" "{fake_py}" %*\r\n', encoding="utf-8")
    return fake_cmd, fake_py.with_suffix(".state.json")


def main() -> int:
    temp = Path(tempfile.mkdtemp(prefix="costmarshal-v2-tmux-contract-"))
    try:
        fake_tmux, fake_state = make_fake_tmux(temp)
        init = run_json(
            temp,
            "init",
            "--name",
            "tmux-contract",
            "--objective",
            "Verify fake tmux backend contract",
            "--session-name",
            "cmv2-fake",
            "--backend",
            "tmux",
            "--backend-command",
            str(fake_tmux),
        )
        project = Path(init["project"])
        assert_true(init["backend"] == "tmux", "tmux contract should explicitly select the tmux backend")
        leader = run_json(temp, "start-leader", "--project", str(project), "--command", "leader {prompt_file}")
        assert_true(not leader["dry_run"], "start-leader should run through fake tmux")
        assert_true(leader["backend"] == "tmux", "start-leader should report the tmux backend")

        run_json(temp, "new-task", "--project", str(project), "--title", "Fake tmux task", "--purpose", "Exercise tmux backend")
        dispatch = run_json(
            temp,
            "dispatch",
            "--project",
            str(project),
            "--task",
            "V2-0001",
            "--model",
            "gpt-5",
            "--command",
            "agent {prompt_file} {brief}",
            "--start",
        )
        assert_true(dispatch["started"], "dispatch --start should run through fake tmux")
        run_json(temp, "send", "--project", str(project), "--to", "agent-v2-0001", "--message", "hello agent", "--runtime-send")

        state = json.loads(fake_state.read_text(encoding="utf-8"))
        windows = state["sessions"]["cmv2-fake"]["windows"]
        assert_true("leader" in windows, "leader window should exist in fake tmux")
        assert_true("agent-v2-0001" in windows, "agent window should exist in fake tmux")
        assert_true("leader.prompt.md" in windows["leader"]["command"], "leader command should include prompt path")
        assert_true("agent-v2-0001.prompt.md" in windows["agent-v2-0001"]["command"], "agent command should include prompt path")
        assert_true(state["send_keys"][-1]["target"] == "cmv2-fake:agent-v2-0001", "send --runtime-send should target the tmux adapter")

        stop = run_json(temp, "stop-actor", "--project", str(project), "--actor", "agent-v2-0001", "--stop-runtime", "--reason", "contract done")
        assert_true(stop["actor_status"] == "stopped", "stop-actor should update state after fake runtime stop")
        state = json.loads(fake_state.read_text(encoding="utf-8"))
        assert_true("agent-v2-0001" not in state["sessions"]["cmv2-fake"]["windows"], "kill-window should remove the agent window")

        state["sessions"]["cmv2-fake"]["windows"].pop("leader", None)
        fake_state.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        recovery = run_json(temp, "recover", "--project", str(project), "--plan-restarts")
        assert_true(recovery["status"] == "degraded", "recover should notice a missing running leader runtime")
        assert_true(recovery["planned_restarts"], "recover should plan a restart for missing running actors")

        print(json.dumps({"status": "ok", "temporary_state": "cleaned"}, indent=2))
        return 0
    finally:
        resolved = temp.resolve()
        temp_root = Path(tempfile.gettempdir()).resolve()
        if resolved == temp_root or temp_root not in resolved.parents:
            raise RuntimeError(f"Refusing to delete unexpected path: {resolved}")
        shutil.rmtree(resolved, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
