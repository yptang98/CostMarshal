#!/usr/bin/env python3
"""Internal one-shot CostMarshal actor runner."""

from __future__ import annotations

import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from costmarshal_v2.actor_runner import main  # noqa: E402


if __name__ == "__main__":
    raise SystemExit(main())
