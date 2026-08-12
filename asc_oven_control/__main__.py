"""``python -m asc_oven_control`` entry point."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from asc_oven_control.app import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
