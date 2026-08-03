#!/usr/bin/env python3
"""Internal one-shot CostMarshal actor runner."""

from __future__ import annotations

import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from costmarshal_v2.actor_runner import main  # noqa: E402


def _reconfigure_utf8() -> None:
    """Write actor protocol output as UTF-8 regardless of console codepage.

    Windows consoles commonly use GBK/cp936; provider or tool output can
    contain characters outside that encoding (including U+FFFD replacement
    characters).  Reconfiguring the streams prevents UnicodeEncodeError from
    killing a finished actor after the provider call already succeeded.
    """

    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, OSError, ValueError):
            pass


if __name__ == "__main__":
    _reconfigure_utf8()
    raise SystemExit(main())
