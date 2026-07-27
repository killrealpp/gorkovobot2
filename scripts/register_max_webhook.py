from __future__ import annotations

import asyncio

from app.bot.max_client import MaxClient
from app.core.config import get_settings


async def main() -> None:
    settings = get_settings()
    if not settings.max_webhook_url:
        raise RuntimeError("MAX_WEBHOOK_URL is empty")
    client = MaxClient(
        token=settings.max_bot_token,
        base_url=settings.max_api_base_url,
        trust_env=bool(settings.http_trust_env),
    )
    try:
        result = await client.subscribe_webhook(
            url=settings.max_webhook_url,
            secret=settings.max_webhook_secret or None,
            update_types=["message_created", "bot_started"],
        )
        print(result)
    finally:
        await client.close()


if __name__ == "__main__":
    asyncio.run(main())
