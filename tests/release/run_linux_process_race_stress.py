#!/usr/bin/env python3
"""Repeat the Linux STOP/TERM spawn race before accepting a release revision."""

from __future__ import annotations

import argparse
import platform
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tests"))

from pid_identity_test import PidIdentityTest  # noqa: E402


TEST_NAME = "test_local_stop_keeps_supervisor_anchor_while_term_handler_spawns_child"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--iterations", type=int, default=50)
    args = parser.parse_args()
    if args.iterations < 1:
        parser.error("--iterations must be positive")
    if platform.system() != "Linux":
        raise SystemExit("Linux process-race stress requires Linux")

    runner = unittest.TextTestRunner(verbosity=1)
    for iteration in range(1, args.iterations + 1):
        result = runner.run(PidIdentityTest(TEST_NAME))
        if result.skipped:
            print(
                f"Linux process-race stress was skipped at iteration {iteration}: "
                f"{result.skipped}",
                file=sys.stderr,
            )
            return 1
        if not result.wasSuccessful():
            print(f"Linux process-race stress failed at iteration {iteration}", file=sys.stderr)
            return 1
    print(f"Linux process-race stress passed: {args.iterations}/{args.iterations}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
