#!/usr/bin/env python3
"""CostMarshal official v2 CLI entrypoint."""

from __future__ import annotations

import sys
from pathlib import Path


MINIMUM_PYTHON = (3, 11)


def require_supported_python() -> None:
    if sys.version_info < MINIMUM_PYTHON:
        required = ".".join(str(part) for part in MINIMUM_PYTHON)
        actual = f"{sys.version_info[0]}.{sys.version_info[1]}"
        raise SystemExit(
            f"CostMarshal requires Python {required}+; current interpreter is Python {actual}."
        )


require_supported_python()

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from costmarshal_v2.cli import main  # noqa: E402


if __name__ == "__main__":
    raise SystemExit(main())
