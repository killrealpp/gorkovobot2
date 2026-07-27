from __future__ import annotations

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from app.dialog.availability_cache import refresh_availability_cache


def main() -> None:
    days = int(sys.argv[1]) if len(sys.argv) > 1 else 14
    max_seconds = int(sys.argv[2]) if len(sys.argv) > 2 else 120
    refresh_availability_cache(days=days, max_seconds=max_seconds)
    print("OK availability cache refreshed: mvp_availability_cache")


if __name__ == "__main__":
    main()
