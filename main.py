from __future__ import annotations

import asyncio
import logging

from app.core.config import get_settings
from app.storage.sqlite import init_db


def main() -> None:
    settings = get_settings()
    logging.basicConfig(
        level=getattr(logging, str(settings.log_level or "INFO").upper(), logging.INFO),
        format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    )
    init_db()

    channels = [item.strip().lower() for item in (settings.client_channels or "telegram").split(",") if item.strip()]
    if not channels:
        channels = ["telegram"]

    if "max" in channels:
        from app.bot.max import run_bot
    elif "telegram" in channels:
        from app.bot.telegram import run_bot
    else:
        raise RuntimeError(f"Unsupported CLIENT_CHANNELS={settings.client_channels!r}. Use max or telegram.")

    asyncio.run(run_bot())


if __name__ == "__main__":
    main()
