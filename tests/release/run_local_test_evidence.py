#!/usr/bin/env python3
"""Run the complete local release suite and emit SHA-bound evidence."""

from __future__ import annotations

from datetime import datetime, timezone
import json
import os
from pathlib import Path
import subprocess
import sys
import time


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from run_release_gates import REQUIRED_LOCAL_TESTS  # noqa: E402


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def run_command(argv: list[str], *, timeout_seconds: float = 300.0) -> tuple[int, str, float]:
    started = time.monotonic()
    environment = os.environ.copy()
    environment.pop("PYTHONOPTIMIZE", None)
    try:
        completed = subprocess.run(
            argv,
            cwd=ROOT,
            env=environment,
            text=True,
            encoding="utf-8",
            errors="replace",
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=timeout_seconds,
            check=False,
        )
        return completed.returncode, completed.stdout[-4000:], round(time.monotonic() - started, 3)
    except subprocess.TimeoutExpired as exc:
        output = exc.stdout.decode("utf-8", errors="replace") if isinstance(exc.stdout, bytes) else str(exc.stdout or "")
        return 124, (output + "\ncommand timed out")[-4000:], round(time.monotonic() - started, 3)


def main(argv: list[str] | None = None) -> int:
    if sys.flags.optimize:
        raise SystemExit("release evidence refuses optimized Python; assertions must remain enabled")
    output = ROOT / "artifacts" / "local-test-report.json"
    if argv:
        if len(argv) != 1:
            raise SystemExit("usage: run_local_test_evidence.py [output.json]")
        output = Path(argv[0]).expanduser().resolve()

    git_sha = subprocess.check_output(
        ["git", "rev-parse", "HEAD"], cwd=ROOT, text=True, encoding="utf-8"
    ).strip()
    compile_code, compile_tail, compile_seconds = run_command(
        [sys.executable, "-m", "compileall", "-q", "costmarshal_v2", "tests", "scripts"]
    )
    results: list[dict[str, object]] = []
    if compile_code == 0:
        for relative in REQUIRED_LOCAL_TESTS:
            returncode, output_tail, duration_seconds = run_command(
                [sys.executable, str(ROOT / relative)],
                timeout_seconds=600.0,
            )
            results.append(
                {
                    "test": relative,
                    "returncode": returncode,
                    "duration_seconds": duration_seconds,
                    "output_tail": output_tail,
                }
            )
            if returncode != 0:
                break

    passed = sum(row["returncode"] == 0 for row in results)
    failed = int(compile_code != 0) + sum(row["returncode"] != 0 for row in results)
    complete = len(results) == len(REQUIRED_LOCAL_TESTS)
    status = "pass" if compile_code == 0 and complete and failed == 0 else "fail"
    report = {
        "schema_version": 1,
        "generated_at": utc_now(),
        "git_sha": git_sha,
        "status": status,
        "compileall_passed": compile_code == 0,
        "compileall_duration_seconds": compile_seconds,
        "compileall_output_tail": compile_tail,
        "tests": list(REQUIRED_LOCAL_TESTS),
        "passed": passed,
        "failed": failed,
        "results": results,
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_name(output.name + ".tmp")
    temporary.write_text(
        json.dumps(report, indent=2, sort_keys=True, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    temporary.replace(output)
    print(
        json.dumps(
            {
                "status": status,
                "git_sha": git_sha,
                "passed": passed,
                "failed": failed,
                "report": str(output),
            },
            sort_keys=True,
        )
    )
    return 0 if status == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
