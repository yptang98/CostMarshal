#!/usr/bin/env python3
"""CostMarshal CLI entrypoint.

Keeps the old implementation in mc.py for compatibility while exposing the
new product name in docs and commands.
"""

from mc import main


if __name__ == "__main__":
    raise SystemExit(main())
