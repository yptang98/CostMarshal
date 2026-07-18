#!/usr/bin/env python3
"""Run real local crash boundaries and emit SHA-bound runtime-effect evidence."""

from __future__ import annotations

from datetime import datetime, timezone
import json
import os
from pathlib import Path
import subprocess
import sys
from typing import Any


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(Path(__file__).resolve().parent))

from runtime_evidence_contract import (  # noqa: E402
    RECEIPT_PREFIX,
    REQUIRED_RUNTIME_CRASH_POINTS,
    REQUIRED_RUNTIME_RECOVERY_SCENARIOS,
    RUNTIME_EVIDENCE_TESTS,
)


def _plain_nonnegative_integer(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


def _parse_receipt(output: str, expected_test: str) -> tuple[dict[str, Any] | None, str | None]:
    markers = [line[len(RECEIPT_PREFIX) :] for line in output.splitlines() if line.startswith(RECEIPT_PREFIX)]
    if len(markers) != 1:
        return None, f"expected exactly one runtime receipt, found {len(markers)}"
    try:
        receipt = json.loads(markers[0])
    except json.JSONDecodeError as exc:
        return None, f"runtime receipt is invalid JSON: {exc}"
    if not isinstance(receipt, dict):
        return None, "runtime receipt must be an object"
    if receipt.get("schema_version") != 1 or receipt.get("test") != expected_test:
        return None, "runtime receipt identity does not match the executed test"
    for field in ("crash_points", "recovery_scenarios"):
        values = receipt.get(field)
        if not isinstance(values, list) or any(not isinstance(value, str) or not value for value in values):
            return None, f"runtime receipt {field} must be a list of non-empty strings"
        if len(values) != len(set(values)):
            return None, f"runtime receipt {field} contains duplicates"
    for field in ("provider_calls", "expected_provider_calls", "orphan_effects"):
        if _plain_nonnegative_integer(receipt.get(field)) is None:
            return None, f"runtime receipt {field} must be a non-negative integer"
    return receipt, None


def main(argv: list[str] | None = None) -> int:
    if sys.flags.optimize:
        raise SystemExit("runtime evidence refuses optimized Python; assertions must remain enabled")
    output = ROOT / "artifacts" / "runtime-effect-report.json"
    if argv:
        if len(argv) != 1:
            raise SystemExit("usage: run_runtime_effect_evidence.py [output.json]")
        output = Path(argv[0]).expanduser().resolve()
    results: list[dict[str, Any]] = []
    receipts: list[dict[str, Any]] = []
    errors: list[str] = []
    passed = True
    for relative in RUNTIME_EVIDENCE_TESTS:
        environment = os.environ.copy()
        environment.pop("PYTHONOPTIMIZE", None)
        try:
            completed = subprocess.run(
                [sys.executable, str(ROOT / relative)],
                cwd=ROOT,
                env=environment,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                timeout=600.0,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            output_text = (
                exc.stdout.decode("utf-8", errors="replace")
                if isinstance(exc.stdout, bytes)
                else str(exc.stdout or "")
            )
            completed = subprocess.CompletedProcess(
                [sys.executable, str(ROOT / relative)],
                124,
                stdout=output_text + "\nruntime evidence test timed out",
            )
        receipt, receipt_error = _parse_receipt(completed.stdout, relative)
        result = {
            "test": relative,
            "returncode": completed.returncode,
            "output_tail": completed.stdout[-2000:],
        }
        results.append(result)
        if receipt_error is not None:
            result["receipt_error"] = receipt_error
            errors.append(f"{relative}: {receipt_error}")
        else:
            assert receipt is not None
            receipts.append(receipt)
        passed = passed and completed.returncode == 0 and receipt_error is None
    crash_points = sorted({point for receipt in receipts for point in receipt["crash_points"]})
    recovery_scenarios = sorted(
        {scenario for receipt in receipts for scenario in receipt["recovery_scenarios"]}
    )
    duplicate_provider_calls = sum(
        max(0, int(receipt["provider_calls"]) - int(receipt["expected_provider_calls"]))
        for receipt in receipts
    )
    missing_provider_calls = sum(
        max(0, int(receipt["expected_provider_calls"]) - int(receipt["provider_calls"]))
        for receipt in receipts
    )
    orphan_effects = sum(int(receipt["orphan_effects"]) for receipt in receipts)
    if set(crash_points) != set(REQUIRED_RUNTIME_CRASH_POINTS):
        errors.append("runtime crash-point receipts do not exactly match the release contract")
    if set(recovery_scenarios) != set(REQUIRED_RUNTIME_RECOVERY_SCENARIOS):
        errors.append("runtime recovery-scenario receipts do not exactly match the release contract")
    if duplicate_provider_calls or missing_provider_calls or orphan_effects:
        errors.append("runtime receipts report provider-call divergence or orphan effects")
    passed = passed and not errors and len(receipts) == len(RUNTIME_EVIDENCE_TESTS)
    git_sha = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip()
    payload = {
        "schema_version": 1,
        "generated_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "git_sha": git_sha,
        "status": "pass" if passed else "fail",
        "transactional_effect_worker": passed,
        "crash_points_tested": len(crash_points),
        "crash_points": crash_points,
        "recovery_scenarios": recovery_scenarios,
        "duplicate_provider_calls": duplicate_provider_calls,
        "missing_provider_calls": missing_provider_calls,
        "orphan_effects": orphan_effects,
        "errors": errors,
        "receipts": receipts,
        "tests": results,
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps(payload, indent=2, sort_keys=True))
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
