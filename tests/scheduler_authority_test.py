#!/usr/bin/env python3
"""SQLite remains authoritative across active and completely quiet scheduler cycles."""

from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import tempfile


ROOT = Path(__file__).resolve().parents[1]
CLI = ROOT / "scripts" / "costmarshal.py"


def run_raw(temp: Path, *args: str) -> subprocess.CompletedProcess[str]:
    environment = os.environ.copy()
    environment["COSTMARSHAL_V2_HOME"] = str(temp / "runtime")
    return subprocess.run(
        [sys.executable, str(CLI), "--root", str(temp / "runtime"), *args],
        text=True,
        encoding="utf-8",
        errors="replace",
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=environment,
        check=False,
    )


def run(temp: Path, *args: str) -> dict:
    completed = run_raw(temp, *args)
    if completed.returncode:
        raise AssertionError(
            f"command failed: {args}\n{completed.stdout}\n{completed.stderr}"
        )
    return json.loads(completed.stdout)


def database_document(connection: sqlite3.Connection, path: str) -> str:
    row = connection.execute(
        "SELECT content FROM documents WHERE path=?",
        (path,),
    ).fetchone()
    if row is None:
        raise AssertionError(f"missing authoritative document: {path}")
    return str(row[0])


def database_ledger(connection: sqlite3.Connection, path: str) -> list[dict]:
    return [
        json.loads(str(row[0]))
        for row in connection.execute(
            "SELECT content FROM ledger_entries WHERE path=? ORDER BY sequence",
            (path,),
        ).fetchall()
    ]


def main() -> int:
    temp = Path(tempfile.mkdtemp(prefix="costmarshal-scheduler-authority-"))
    try:
        workspace = temp / "workspace"
        workspace.mkdir()
        project = Path(
            run(
                temp,
                "init",
                "--name",
                "scheduler-authority",
                "--objective",
                "keep scheduler state authoritative",
                "--workspace",
                str(workspace),
                "--backend",
                "local",
                "--governance",
                "off",
            )["project"]
        )
        run(temp, "migrate-state", "--project", str(project), "--apply")

        # Drain initialization mailboxes, then establish a quiet baseline.
        for _ in range(3):
            payload = run(
                temp,
                "run-scheduler",
                "--project",
                str(project),
                "--once",
            )
            assert payload["status"] == "ok"

        database = project / "scheduler" / "state.db"
        with sqlite3.connect(database) as connection:
            baseline_commands = int(
                connection.execute("SELECT COUNT(*) FROM commands").fetchone()[0]
            )
            baseline_events = int(
                connection.execute(
                    "SELECT COUNT(*) FROM ledger_entries WHERE path='scheduler/events.jsonl'"
                ).fetchone()[0]
            )

        quiet = run(
            temp,
            "run-scheduler",
            "--project",
            str(project),
            "--max-cycles",
            "20",
            "--interval",
            "0.05",
        )
        assert quiet["status"] == "ok"
        assert quiet["cycles"] == 20
        assert quiet["changed_cycles"] == 0
        assert quiet["scheduler_state"]["status"] == "idle"
        assert quiet["scheduler_state"]["pid"] is None

        with sqlite3.connect(database) as connection:
            assert int(connection.execute("SELECT COUNT(*) FROM commands").fetchone()[0]) == baseline_commands
            assert int(
                connection.execute(
                    "SELECT COUNT(*) FROM ledger_entries WHERE path='scheduler/events.jsonl'"
                ).fetchone()[0]
            ) == baseline_events
            authoritative_state = json.loads(
                database_document(connection, "scheduler/state.json")
            )
            authoritative_events = database_ledger(
                connection,
                "scheduler/events.jsonl",
            )
            assert connection.execute("SELECT COUNT(*) FROM dirty_views").fetchone()[0] == 0

        file_state = json.loads(
            (project / "scheduler" / "state.json").read_text(encoding="utf-8")
        )
        file_events = [
            json.loads(line)
            for line in (project / "scheduler" / "events.jsonl")
            .read_text(encoding="utf-8")
            .splitlines()
            if line
        ]
        assert file_state == authoritative_state
        assert file_events == authoritative_events

        # An externally restored stale file is reported by validate and then
        # repaired from SQLite before the scheduler performs any work.
        stale_state = dict(file_state)
        stale_state["cycle_count"] = int(stale_state.get("cycle_count") or 0) + 999
        (project / "scheduler" / "state.json").write_text(
            json.dumps(stale_state, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        invalid = run_raw(temp, "validate", "--project", str(project))
        assert invalid.returncode == 1
        assert "compatibility view drift" in invalid.stdout
        run(temp, "run-scheduler", "--project", str(project), "--once")

        # A compatibility-only ghost task cannot become a second truth. A
        # mutable command removes the ghost before allocating the next task.
        ghost = project / "tasks" / "V2-9999" / "task.json"
        ghost.parent.mkdir(parents=True)
        ghost.write_text('{"id":"V2-9999","status":"planned"}\n', encoding="utf-8")
        invalid = run_raw(temp, "validate", "--project", str(project))
        assert invalid.returncode == 1
        assert "ghost compatibility view" in invalid.stdout
        created = run(
            temp,
            "new-task",
            "--project",
            str(project),
            "--title",
            "authoritative allocation",
            "--purpose",
            "ignore and remove compatibility ghosts",
            "--provider",
            "longcat",
        )
        assert created["task_id"] != "V2-9999"
        assert not ghost.exists()
        assert run(temp, "validate", "--project", str(project))["status"] == "ok"
        print("scheduler authority ok")
        return 0
    finally:
        shutil.rmtree(temp, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
