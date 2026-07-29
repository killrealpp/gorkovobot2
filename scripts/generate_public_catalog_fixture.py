from __future__ import annotations

import json
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

FIXTURE_PATH = PROJECT_ROOT / "tests" / "fixtures" / "public_catalog.v1.json"


def main() -> None:
    from app.api.public_catalog import build_public_catalog

    payload = build_public_catalog(
        generated_at="fixture",
        read_availability_cache=False,
        include_overrides=False,
    )
    FIXTURE_PATH.parent.mkdir(parents=True, exist_ok=True)
    FIXTURE_PATH.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(f"OK wrote {FIXTURE_PATH}")


if __name__ == "__main__":
    main()
