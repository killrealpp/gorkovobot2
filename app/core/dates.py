from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from app.core.config import get_settings


def now_local() -> datetime:
    return datetime.now(ZoneInfo(get_settings().app_timezone))
