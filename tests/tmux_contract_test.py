#!/usr/bin/env python3
"""Contract test for v2's tmux backend using a fake tmux executable.

This exercises non-dry-run start, send, stop, and recovery paths without
requiring tmux to be installed on the development machine.
"""

from __future__ import annotations

import hashlib
import json
import os
import secrets
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
CLI = ROOT / "scripts" / "costmarshal.py"
sys.path.insert(0, str(ROOT))

from costmarshal_v2.session_backend import TmuxBackend, command_to_string, tmux_format_literal  # noqa: E402


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


def ensure_session_ids(state: dict) -> None:
    used = {
        int(str(row.get("session_id", "$-1"))[1:])
        for row in state.get("sessions", {}).values()
        if str(row.get("session_id", "")).startswith("$")
        and str(row.get("session_id", ""))[1:].isdigit()
    }
    next_id = max(used, default=-1) + 1
    for name in sorted(state.get("sessions", {})):
        row = state["sessions"][name]
        if not str(row.get("session_id", "")).startswith("$"):
            row["session_id"] = f"${next_id}"
            next_id += 1


def option(args: list[str], name: str) -> str:
    try:
        return args[args.index(name) + 1]
    except (ValueError, IndexError):
        raise SystemExit(f"missing {name}")


def session_target(value: str, sessions: dict) -> str:
    exact = value.startswith("=")
    name = value[1:] if exact else value
    if name.endswith(":"):
        name = name[:-1]
    if name.startswith("$"):
        for candidate, row in sessions.items():
            if row.get("session_id") == name:
                return candidate
        return name
    if exact or name in sessions:
        return name
    matches = sorted(candidate for candidate in sessions if candidate.startswith(name))
    return matches[0] if len(matches) == 1 else name


def actor_target(value: str) -> tuple[str, str]:
    raw = value[1:] if value.startswith("=") else value
    if ":=" in raw:
        return tuple(raw.split(":=", 1))
    return tuple(raw.split(":", 1))


def main() -> int:
    args = sys.argv[1:]
    if not args:
        return 2
    state = load()
    ensure_session_ids(state)
    cmd = args[0]
    state.setdefault("calls", []).append(args)
    if cmd == "list-sessions":
        for session in sorted(state["sessions"]):
            row = state["sessions"][session]
            print(f"{row['session_id']}\t{session}")
        save(state)
        return 0
    if cmd == "has-session":
        target = option(args, "-t")
        session = session_target(target, state["sessions"])
        save(state)
        if target.startswith("="):
            return 0 if session in state["sessions"] else 1
        matches = [candidate for candidate in state["sessions"] if candidate.startswith(session)]
        return 0 if session in state["sessions"] or len(matches) == 1 else 1
    if cmd == "new-session":
        session = option(args, "-s").replace(".", "_").replace(":", "_")
        window = option(args, "-n")
        cwd = option(args, "-c")
        command = args[-1]
        next_session_id = max(
            (int(str(row["session_id"])[1:]) for row in state["sessions"].values()),
            default=-1,
        ) + 1
        state["sessions"].setdefault(
            session,
            {"session_id": f"${next_session_id}", "windows": {}},
        )["windows"][window] = {
            "command": command,
            "cwd": cwd,
            "window_id": f"@{sum(len(row['windows']) for row in state['sessions'].values()) + 1}",
        }
        save(state)
        return 0
    if cmd == "new-window":
        target = option(args, "-t")
        session = session_target(target, state["sessions"])
        window = option(args, "-n")
        cwd = option(args, "-c")
        command = args[-1]
        if session not in state["sessions"]:
            print(f"missing session {session}", file=sys.stderr)
            return 1
        state["sessions"][session]["windows"][window] = {
            "command": command,
            "cwd": cwd,
            "create_target": target,
            "window_id": f"@{sum(len(row['windows']) for row in state['sessions'].values()) + 1}",
        }
        save(state)
        return 0
    if cmd == "list-windows":
        session = session_target(option(args, "-t"), state["sessions"])
        windows = state["sessions"].get(session, {}).get("windows", {})
        include_id = "#{window_id}" in option(args, "-F")
        for window in sorted(windows):
            if include_id:
                print(f"{windows[window].get('window_id', '@0')}\t{window}")
            else:
                print(window)
        save(state)
        return 0
    if cmd == "send-keys":
        target = option(args, "-t")
        text = args[-1] if args else ""
        state.setdefault("send_keys", []).append({"target": target, "text": text, "literal": "-l" in args})
        save(state)
        return 0
    if cmd == "kill-window":
        target = option(args, "-t")
        if target.startswith("@"):
            for session_row in state["sessions"].values():
                for window, window_row in list(session_row.get("windows", {}).items()):
                    if window_row.get("window_id") == target:
                        session_row["windows"].pop(window, None)
        else:
            session, window = actor_target(target)
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


def tree_snapshot(root: Path) -> dict[str, str]:
    return {
        path.relative_to(root).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def wait_for_file(path: Path, *, timeout_seconds: float = 10.0) -> None:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        if path.is_file():
            return
        time.sleep(0.05)
    raise AssertionError(f"timed out waiting for native tmux output: {path}")


def run_native_tmux_contract(temp: Path) -> bool:
    if os.name == "nt":
        return False
    executable = shutil.which("tmux")
    if not executable:
        if os.environ.get("CI", "").lower() == "true":
            raise AssertionError("CI must provide tmux for the native backend contract")
        return False
    backend = TmuxBackend(executable)
    # Deliberately exercise the pre-hardening dotted-session compatibility
    # contract against native tmux, not only the fake backend.
    session = f"cmv2.legacy-{os.getpid()}-{secrets.token_hex(4)}"
    prefix_session = f"{session}-prefix"
    cwd = temp / "native space #{session_name}"
    cwd.mkdir(parents=True)
    input_output = temp / "native-input.txt"
    cwd_output = temp / "native-cwd.txt"
    second_cwd_output = temp / "native-second-cwd.txt"
    legacy_input_output = temp / "native-legacy-input.txt"
    socket_root = temp / "native-tmux-socket"
    socket_root.mkdir(mode=0o700)
    socket_root.chmod(0o700)
    isolated_home = temp / "native-tmux-home"
    isolated_home.mkdir(mode=0o700)
    isolated_home.chmod(0o700)
    sleeper = command_to_string([sys.executable, "-c", "import time; time.sleep(30)"])
    reader = command_to_string(
        [
            sys.executable,
            "-c",
            (
                "import os,pathlib,time; "
                f"pathlib.Path({str(cwd_output)!r}).write_text(os.getcwd(), encoding='utf-8'); "
                "value=input(); "
                f"pathlib.Path({str(input_output)!r}).write_text(value, encoding='utf-8'); "
                "time.sleep(30)"
            ),
        ]
    )
    second = command_to_string(
        [
            sys.executable,
            "-c",
            (
                "import os,pathlib,time; "
                f"pathlib.Path({str(second_cwd_output)!r}).write_text(os.getcwd(), encoding='utf-8'); "
                "time.sleep(30)"
            ),
        ]
    )
    legacy_reader = command_to_string(
        [
            sys.executable,
            "-c",
            (
                "import pathlib,time; value=input(); "
                f"pathlib.Path({str(legacy_input_output)!r}).write_text(value, encoding='utf-8'); "
                "time.sleep(30)"
            ),
        ]
    )
    isolated_environment = {
        "TMUX_TMPDIR": str(socket_root),
        "HOME": str(isolated_home),
        "XDG_CONFIG_HOME": str(isolated_home / ".config"),
    }
    previous_environment = {
        key: os.environ.get(key)
        for key in (*isolated_environment, "TMUX")
    }
    os.environ.update(isolated_environment)
    os.environ.pop("TMUX", None)
    try:
        created_prefix = subprocess.run(
            [executable, "new-session", "-d", "-s", prefix_session, "-n", "sentinel", "--", sleeper],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        assert_true(created_prefix.returncode == 0, f"native tmux prefix setup failed: {created_prefix.stderr}")
        assert_true(not backend.session_exists(session), "native exact lookup should not match a prefix session")
        first_launch = backend.start_actor(
            session_name=session,
            actor_name="reader",
            command=reader,
            cwd=cwd,
            log_path=temp / "ignored-reader.log",
        )
        assert_true(first_launch["target"] == f"{session}:reader", "native tmux should persist a stable logical target")
        listed_sessions = subprocess.run(
            [executable, "list-sessions", "-F", "#{session_id}\t#{session_name}"],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        assert_true(listed_sessions.returncode == 0, f"native tmux session listing failed: {listed_sessions.stderr}")
        assert_true(
            any(
                line.endswith(f"\t{session.replace('.', '_')}")
                for line in listed_sessions.stdout.splitlines()
            ),
            "tmux 3.4 should sanitize the dotted persisted session name at runtime",
        )
        assert_true(
            backend._resolve_session_target(session) is not None,
            "native dotted logical session should resolve to a stable session ID",
        )
        wait_for_file(cwd_output)
        reader_window_id = backend._resolve_actor_target(f"{session}:reader")
        reader_status = subprocess.run(
            [executable, "list-panes", "-t", reader_window_id, "-F", "#{pane_dead}"],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        assert_true(
            reader_status.returncode == 0 and reader_status.stdout.splitlines() == ["0"],
            f"native tmux reader pane exited before send: {reader_status.stderr}",
        )
        backend.send_text(target=f"{session}:reader", text="C-c")
        wait_for_file(input_output)
        assert_true(input_output.read_text(encoding="utf-8") == "C-c", "native tmux should inject C-c as literal text")
        assert_true(cwd_output.read_text(encoding="utf-8") == str(cwd.resolve()), "native tmux should preserve the literal cwd")
        backend.start_actor(
            session_name=session,
            actor_name="second",
            command=second,
            cwd=cwd,
            log_path=temp / "ignored-second.log",
        )
        wait_for_file(second_cwd_output)
        assert_true(
            second_cwd_output.read_text(encoding="utf-8") == str(cwd.resolve()),
            "native tmux new-window should preserve the literal cwd",
        )
        assert_true("sentinel" in backend.list_actors(prefix_session), "native exact targets should preserve the prefix session")
        backend.stop_actor(target=f"{session}:second")
        assert_true("second" not in backend.list_actors(session), "native exact stop should remove only the selected actor")
        legacy_created = subprocess.run(
            [
                executable,
                "new-window",
                "-d",
                "-t",
                f"{backend._resolve_session_target(session)}:",
                "-n",
                "legacy.reader",
                "-c",
                tmux_format_literal(str(cwd.resolve())),
                "--",
                legacy_reader,
            ],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        assert_true(legacy_created.returncode == 0, f"native legacy window setup failed: {legacy_created.stderr}")
        backend.send_text(target=f"{session}:legacy.reader", text="legacy")
        wait_for_file(legacy_input_output)
        assert_true(
            legacy_input_output.read_text(encoding="utf-8") == "legacy",
            "native legacy dotted actor send should use its resolved window ID",
        )
        backend.stop_actor(target=f"{session}:legacy.reader")
        assert_true(
            "legacy.reader" not in backend.list_actors(session),
            "native legacy dotted actor stop should remove the resolved window",
        )
    finally:
        cleanup_targets: list[str] = []
        for logical_name in (session, prefix_session):
            try:
                resolved_target = backend._resolve_session_target(logical_name)
            except RuntimeError:
                resolved_target = None
            if resolved_target is not None:
                cleanup_targets.append(resolved_target)
        for target in cleanup_targets:
            subprocess.run(
                [executable, "kill-session", "-t", target],
                text=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
            )
        remaining_sessions = subprocess.run(
            [executable, "list-sessions", "-F", "#{session_id}\t#{session_name}"],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        assert_true(
            not remaining_sessions.stdout.strip(),
            f"native tmux cleanup left sessions behind: {remaining_sessions.stdout}",
        )
        for key, previous in previous_environment.items():
            if previous is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = previous
    return True


def make_fake_tmux(temp: Path) -> tuple[Path, Path]:
    fake_py = temp / "fake_tmux.py"
    payload = FAKE_TMUX
    if os.name != "nt":
        payload = "#!/usr/bin/env python3\n" + FAKE_TMUX.lstrip()
    fake_py.write_text(payload, encoding="utf-8")
    if os.name != "nt":
        fake_py.chmod(0o700)
        return fake_py, fake_py.with_suffix(".state.json")
    fake_cmd = temp / "fake_tmux.cmd"
    fake_cmd.write_text(f'@echo off\r\n"{sys.executable}" "{fake_py}" %*\r\n', encoding="utf-8")
    return fake_cmd, fake_py.with_suffix(".state.json")


def run_tmux_inspection_failure_contract() -> None:
    class ScriptedTmuxBackend(TmuxBackend):
        def __init__(self, results: list[subprocess.CompletedProcess[str]]) -> None:
            super().__init__("tmux-scripted")
            self.results = list(results)

        def available(self) -> bool:
            return True

        def _run(
            self,
            argv: list[str],
            *,
            check: bool = True,
        ) -> subprocess.CompletedProcess[str]:
            if not self.results:
                raise AssertionError(f"unexpected tmux call: {argv}")
            return self.results.pop(0)

    absent = ScriptedTmuxBackend(
        [
            subprocess.CompletedProcess(
                ["tmux-scripted", "list-sessions"],
                1,
                "",
                "no server running on /tmp/tmux-test/default\n",
            )
        ]
    )
    assert_true(
        not absent.session_exists("cmv2-test"),
        "an explicit tmux no-server result should mean the session is absent",
    )

    failed_sessions = ScriptedTmuxBackend(
        [
            subprocess.CompletedProcess(
                ["tmux-scripted", "list-sessions"],
                1,
                "",
                "permission denied while opening the tmux socket\n",
            )
        ]
    )
    try:
        failed_sessions.session_exists("cmv2-test")
    except RuntimeError as exc:
        assert_true(
            "session inspection failed" in str(exc),
            "unexpected list-sessions failures should retain an inspection error",
        )
    else:
        raise AssertionError("an arbitrary tmux list-sessions failure was treated as absence")

    failed_windows = ScriptedTmuxBackend(
        [
            subprocess.CompletedProcess(
                ["tmux-scripted", "list-sessions"],
                0,
                "$7\tcmv2-test\n",
                "",
            ),
            subprocess.CompletedProcess(
                ["tmux-scripted", "list-windows"],
                1,
                "",
                "server lost while listing windows\n",
            ),
        ]
    )
    try:
        failed_windows.actor_alive(
            session_name="cmv2-test",
            actor_name="leader",
            target="cmv2-test:leader",
        )
    except RuntimeError as exc:
        assert_true(
            "actor inspection failed" in str(exc),
            "unexpected list-windows failures should retain an inspection error",
        )
    else:
        raise AssertionError("an arbitrary tmux list-windows failure was treated as a stopped actor")

    vanished_session = ScriptedTmuxBackend(
        [
            subprocess.CompletedProcess(
                ["tmux-scripted", "list-sessions"],
                0,
                "$7\tcmv2-test\n",
                "",
            ),
            subprocess.CompletedProcess(
                ["tmux-scripted", "list-windows"],
                1,
                "",
                "can't find session: $7\n",
            ),
        ]
    )
    assert_true(
        not vanished_session.actor_alive(
            session_name="cmv2-test",
            actor_name="leader",
            target="cmv2-test:leader",
        ),
        "an exact vanished-session response should confirm that the actor is absent",
    )


def main() -> int:
    run_tmux_inspection_failure_contract()
    temp = Path(tempfile.mkdtemp(prefix="costmarshal-v2-tmux#{session_name}-"))
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
        assert_true(Path(configured["path"]).is_file(), "tmux fixture profile should exist")

        fake_tmux, fake_state = make_fake_tmux(temp)
        fake_state.write_text(
            json.dumps(
                {
                    "sessions": {"cmv2-fake-prefix": {"windows": {"sentinel": {"command": "keep"}}}},
                    "send_keys": [],
                },
                indent=2,
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )
        for invalid_name in ("bad:name", "bad.name", "bad*name", "bad#name"):
            invalid_session = run(
                temp,
                "init",
                "--name",
                f"invalid-tmux-session-{invalid_name.encode().hex()}",
                "--objective",
                "Reject an ambiguous tmux target",
                "--session-name",
                invalid_name,
                "--backend",
                "tmux",
                "--backend-command",
                str(fake_tmux),
                "--allow-unsafe-custom-worker-commands",
                "--allow-unsafe-native-workers",
                expect_ok=False,
            )
            assert_true(
                "tmux session name" in (invalid_session.stdout + invalid_session.stderr),
                f"tmux session target syntax should reject {invalid_name!r}",
            )
        default_session = run_json(
            temp,
            "init",
            "--name",
            "default.session",
            "--objective",
            "Normalize an automatically generated tmux session",
            "--backend",
            "tmux",
            "--backend-command",
            str(fake_tmux),
            "--allow-unsafe-custom-worker-commands",
            "--allow-unsafe-native-workers",
        )
        assert_true(
            "." not in default_session["session_name"],
            "automatically generated tmux session names should not retain dots",
        )
        collision_project = Path(default_session["project"])
        run_json(
            temp,
            "new-task",
            "--project",
            str(collision_project),
            "--title",
            "First runtime name",
            "--purpose",
            "Reserve a truncated tmux runtime name",
        )
        collision_prefix = "actor-" + "a" * 50
        run_json(
            temp,
            "dispatch",
            "--project",
            str(collision_project),
            "--task",
            "V2-0001",
            "--actor-id",
            collision_prefix + "-one",
            "--unsafe-native",
        )
        run_json(
            temp,
            "new-task",
            "--project",
            str(collision_project),
            "--title",
            "Second runtime name",
            "--purpose",
            "Reject a truncated tmux runtime collision",
        )
        collision_task = collision_project / "tasks" / "V2-0002" / "task.json"
        collision_task_before = collision_task.read_bytes()
        collision_project_before = tree_snapshot(collision_project)
        collision_dispatch = run(
            temp,
            "dispatch",
            "--project",
            str(collision_project),
            "--task",
            "V2-0002",
            "--actor-id",
            collision_prefix + "-two",
            "--unsafe-native",
            expect_ok=False,
        )
        assert_true(
            "runtime actor name" in (collision_dispatch.stdout + collision_dispatch.stderr),
            "dispatch should reject actor IDs that truncate to the same tmux runtime name",
        )
        assert_true(
            collision_task.read_bytes() == collision_task_before,
            "a truncated tmux runtime collision should not persist an attempt or budget reservation",
        )
        assert_true(
            tree_snapshot(collision_project) == collision_project_before,
            "a truncated tmux runtime collision should have zero project-tree side effects",
        )
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
            "--allow-unsafe-custom-worker-commands",
            "--allow-unsafe-native-workers",
        )
        project = Path(init["project"])
        assert_true(init["backend"] == "tmux", "tmux contract should explicitly select the tmux backend")
        dry_run = run_json(
            temp,
            "start-leader",
            "--project",
            str(project),
            "--command",
            "leader {prompt_file}",
            "--dry-run",
        )
        assert_true(dry_run["dry_run"], "tmux start dry-run should not launch a runtime")
        escaped_project = str(project).replace("#", "##")
        assert_true(
            escaped_project in dry_run["planned_commands"][0],
            "tmux dry-run should format-escape the actor working directory",
        )
        leader = run_json(temp, "start-leader", "--project", str(project), "--command", "leader {prompt_file}")
        assert_true(not leader["dry_run"], "start-leader should run through fake tmux")
        assert_true(leader["backend"] == "tmux", "start-leader should report the tmux backend")

        run_json(temp, "new-task", "--project", str(project), "--title", "Fake tmux task", "--purpose", "Exercise tmux backend")
        task_path = project / "tasks" / "V2-0001" / "task.json"
        task_before_invalid_actor = task_path.read_bytes()
        project_before_invalid_actor = tree_snapshot(project)
        invalid_actor = run(
            temp,
            "dispatch",
            "--project",
            str(project),
            "--task",
            "V2-0001",
            "--actor-id",
            "bad.name",
            "--unsafe-native",
            expect_ok=False,
        )
        assert_true(
            "Invalid tmux dispatch identity" in (invalid_actor.stdout + invalid_actor.stderr),
            "dispatch should reject an actor name that cannot be represented safely in tmux",
        )
        assert_true(
            task_path.read_bytes() == task_before_invalid_actor,
            "invalid tmux actor identity should not persist an attempt or budget reservation",
        )
        assert_true(
            not (project / "scheduler" / "actors" / "bad.name.json").exists(),
            "invalid tmux actor identity should not persist an actor",
        )
        assert_true(
            not (project / "scheduler" / "mailboxes" / "bad.name").exists(),
            "invalid tmux actor identity should not persist a mailbox",
        )
        assert_true(
            tree_snapshot(project) == project_before_invalid_actor,
            "invalid tmux actor identity should have zero project-tree side effects",
        )
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
            "--unsafe-native",
        )
        assert_true(dispatch["started"], "dispatch --start should run through fake tmux")
        run_json(temp, "send", "--project", str(project), "--to", "agent-v2-0001", "--message", "C-c", "--runtime-send")

        state = json.loads(fake_state.read_text(encoding="utf-8"))
        windows = state["sessions"]["cmv2-fake"]["windows"]
        assert_true("leader" in windows, "leader window should exist in fake tmux")
        assert_true("agent-v2-0001" in windows, "agent window should exist in fake tmux")
        assert_true(windows["leader"]["cwd"] == escaped_project, "leader should start in the escaped project directory")
        assert_true(windows["agent-v2-0001"]["cwd"] == escaped_project, "agent should start in the escaped project directory")
        assert_true("leader.prompt.md" in windows["leader"]["command"], "leader command should include prompt path")
        assert_true("agent-v2-0001.prompt.md" in windows["agent-v2-0001"]["command"], "agent command should include prompt path")
        assert_true(
            windows["agent-v2-0001"]["create_target"]
            == f"{state['sessions']['cmv2-fake']['session_id']}:",
            "new actors should use the resolved stable session ID",
        )
        assert_true(
            "leader" not in state["sessions"]["cmv2-fake-prefix"]["windows"],
            "an exact session lookup must not attach to a prefix collision",
        )
        assert_true(
            state["sessions"]["cmv2-fake-prefix"]["windows"]["sentinel"]["command"] == "keep",
            "an exact session lookup must leave the prefix-collision session unchanged",
        )
        assert_true(
            any(call[0] == "list-sessions" for call in state["calls"]),
            "session existence checks should resolve a stable session ID",
        )
        runtime_send = state["send_keys"][-1]
        assert_true(len(state["send_keys"]) == 1, "runtime send should be one tmux external call")
        assert_true(
            runtime_send["target"] == windows["agent-v2-0001"]["window_id"]
            and runtime_send["literal"] is True
            and runtime_send["text"].rstrip("\r") == "C-c",
            "runtime text and submission should use one literal exact-target call",
        )
        if os.name != "nt":
            assert_true(
                runtime_send["text"] == "C-c\r",
                "POSIX fake tmux should receive the literal carriage-return submission",
            )

        stop = run_json(temp, "stop-actor", "--project", str(project), "--actor", "agent-v2-0001", "--stop-runtime", "--reason", "contract done")
        assert_true(stop["actor_status"] == "stopped", "stop-actor should update state after fake runtime stop")
        state = json.loads(fake_state.read_text(encoding="utf-8"))
        assert_true("agent-v2-0001" not in state["sessions"]["cmv2-fake"]["windows"], "kill-window should remove the agent window")
        assert_true(
            any(
                call[:3] == ["kill-window", "-t", windows["agent-v2-0001"]["window_id"]]
                for call in state["calls"]
            ),
            "runtime stop should use the stable actor window ID",
        )

        # Older beta projects could persist dotted window names. Resolve the
        # exact matching window ID so those actors can still be messaged and
        # stopped without reintroducing ambiguous target parsing.
        state["sessions"]["cmv2-fake"]["windows"]["legacy.actor"] = {
            "command": "legacy",
            "cwd": escaped_project,
            "window_id": "@77",
        }
        fake_state.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        legacy_backend = TmuxBackend(str(fake_tmux))
        legacy_backend.send_text(target="cmv2-fake:legacy.actor", text="legacy")
        legacy_backend.stop_actor(target="cmv2-fake:legacy.actor")
        state = json.loads(fake_state.read_text(encoding="utf-8"))
        assert_true(
            state["send_keys"][-1]["target"] == "@77",
            "legacy dotted actor send should resolve to one exact tmux window ID",
        )
        assert_true(
            any(call[:3] == ["kill-window", "-t", "@77"] for call in state["calls"]),
            "legacy dotted actor stop should resolve to one exact tmux window ID",
        )
        assert_true(
            "legacy.actor" not in state["sessions"]["cmv2-fake"]["windows"],
            "legacy dotted actor stop should remove only the resolved window",
        )
        state["sessions"]["cmv2_legacy"] = {
            "windows": {
                "legacy.actor": {
                    "command": "legacy-session",
                    "cwd": escaped_project,
                    "window_id": "@78",
                }
            }
        }
        fake_state.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        assert_true(
            legacy_backend.session_exists("cmv2.legacy"),
            "persisted dotted sessions should use an exact compatible lookup",
        )
        legacy_backend.stop_actor(target="cmv2.legacy:legacy.actor")
        state = json.loads(fake_state.read_text(encoding="utf-8"))
        assert_true(
            "legacy.actor" not in state["sessions"]["cmv2_legacy"]["windows"],
            "persisted dotted session stop should resolve its exact window ID",
        )

        state["sessions"]["cmv2.legacy"] = {
            "session_id": "$901",
            "windows": {"collision": {"window_id": "@901"}},
        }
        state["sessions"]["cmv2_legacy"]["session_id"] = "$902"
        fake_state.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        try:
            legacy_backend.session_exists("cmv2.legacy")
        except RuntimeError as exc:
            assert_true("ambiguous" in str(exc), "logical/sanitized collision should fail closed")
        else:
            raise AssertionError("logical/sanitized tmux session collision was accepted")
        state["sessions"].pop("cmv2.legacy", None)
        fake_state.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n", encoding="utf-8")

        created_legacy = legacy_backend.start_actor(
            session_name="cmv2.created.legacy",
            actor_name="created-reader",
            command="legacy-created-session",
            cwd=project,
            log_path=temp / "ignored-created-legacy.log",
        )
        assert_true(
            created_legacy["target"] == "cmv2.created.legacy:created-reader",
            "new dotted sessions should retain their logical persisted target",
        )
        state = json.loads(fake_state.read_text(encoding="utf-8"))
        assert_true(
            "cmv2_created_legacy" in state["sessions"]
            and "cmv2.created.legacy" not in state["sessions"],
            "fake tmux should mirror tmux 3.4 dotted session sanitation",
        )
        legacy_backend.send_text(target=created_legacy["target"], text="created")
        state = json.loads(fake_state.read_text(encoding="utf-8"))
        assert_true(
            state["send_keys"][-1]["target"]
            == state["sessions"]["cmv2_created_legacy"]["windows"]["created-reader"]["window_id"],
            "new sanitized sessions should send through their stable window ID",
        )
        legacy_backend.stop_actor(target=created_legacy["target"])

        state = json.loads(fake_state.read_text(encoding="utf-8"))
        state["sessions"]["cmv2-fake"]["windows"].pop("leader", None)
        fake_state.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        recovery = run_json(temp, "recover", "--project", str(project), "--plan-restarts")
        assert_true(recovery["status"] == "degraded", "recover should notice a missing running leader runtime")
        assert_true(recovery["planned_restarts"], "recover should plan a restart for missing running actors")
        assert_true(
            escaped_project in recovery["planned_restarts"][0],
            "recovery restart plans should preserve the escaped actor working directory",
        )

        native_tmux = run_native_tmux_contract(temp)

        print(
            json.dumps(
                {
                    "status": "ok",
                    "native_tmux": "passed" if native_tmux else "not_available",
                    "temporary_state": "cleaned",
                },
                indent=2,
            )
        )
        return 0
    finally:
        if previous_codex_home is None:
            os.environ.pop("CODEX_HOME", None)
        else:
            os.environ["CODEX_HOME"] = previous_codex_home
        resolved = temp.resolve()
        temp_root = Path(tempfile.gettempdir()).resolve()
        if resolved == temp_root or temp_root not in resolved.parents:
            raise RuntimeError(f"Refusing to delete unexpected path: {resolved}")
        shutil.rmtree(resolved, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
