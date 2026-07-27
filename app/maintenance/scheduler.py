from __future__ import annotations

import asyncio
import logging
from typing import Any

from app.core.config import get_settings
from app.maintenance.retention import RetentionPolicy, apply_cleanup, build_cleanup_plan
from app.storage import sqlite


logger = logging.getLogger(__name__)

DEFAULT_AUTO_CLEANUP_KEYS: tuple[str, ...] = (
    "old_messages",
    "old_inactive_conversations",
    "old_system_logs",
    "old_inactive_slot_holds",
    "active_holds_past_expiry",
    "old_sent_admin_notifications",
    "stale_availability_cache",
    "past_availability_cache_dates",
    "old_inactive_watchlist",
    "old_voice_temp_files",
)


def run_retention_cleanup_once() -> dict[str, Any]:
    """Run one configured retention cycle.

    The scheduler calls this in a worker thread. Keeping it sync-friendly also
    makes the behavior easy to smoke-test without starting a bot.
    """

    settings = get_settings()
    policy = _policy_from_settings(settings)
    only_keys = _only_keys_from_settings(settings.retention_cleanup_only)
    max_rows = max(0, int(settings.retention_cleanup_max_rows or 0))
    include_files = bool(settings.retention_cleanup_include_files)

    if settings.retention_cleanup_dry_run:
        return build_cleanup_plan(
            policy,
            limit=max_rows,
            include_files=include_files,
            only_keys=only_keys,
        )

    return apply_cleanup(
        policy,
        max_rows=max_rows,
        include_files=include_files,
        only_keys=only_keys,
        confirmed=True,
    )


async def retention_cleanup_loop() -> None:
    settings = get_settings()
    if not settings.retention_cleanup_enabled:
        logger.info("Retention cleanup loop disabled")
        return

    startup_delay = max(0, int(settings.retention_cleanup_startup_delay_seconds or 0))
    interval = max(60, int(settings.retention_cleanup_interval_seconds or 86400))
    logger.info(
        "Retention cleanup loop enabled startup_delay=%s interval=%s dry_run=%s max_rows=%s only=%s",
        startup_delay,
        interval,
        settings.retention_cleanup_dry_run,
        settings.retention_cleanup_max_rows,
        settings.retention_cleanup_only,
    )

    if startup_delay:
        await asyncio.sleep(startup_delay)

    while True:
        try:
            result = await asyncio.to_thread(run_retention_cleanup_once)
            _log_result(result)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.exception("Retention cleanup loop failed")
            try:
                sqlite.log_system("ERROR", "retention_cleanup_failed", str(exc), {})
            except Exception:
                logger.exception("Failed to write retention cleanup failure to system log")
        await asyncio.sleep(interval)


def _policy_from_settings(settings: Any) -> RetentionPolicy:
    return RetentionPolicy(
        messages_days=max(1, int(settings.retention_messages_days or RetentionPolicy.messages_days)),
        system_logs_days=max(1, int(settings.retention_system_logs_days or RetentionPolicy.system_logs_days)),
        holds_days=max(1, int(settings.retention_holds_days or RetentionPolicy.holds_days)),
        admin_notifications_days=max(1, int(settings.retention_admin_notifications_days or RetentionPolicy.admin_notifications_days)),
        availability_cache_days=max(1, int(settings.retention_availability_cache_days or RetentionPolicy.availability_cache_days)),
        watchlist_days=max(1, int(settings.retention_watchlist_days or RetentionPolicy.watchlist_days)),
        voice_temp_days=max(1, int(settings.retention_voice_temp_days or RetentionPolicy.voice_temp_days)),
        booking_review_days=max(1, int(settings.retention_booking_review_days or RetentionPolicy.booking_review_days)),
    )


def _only_keys_from_settings(raw: str) -> list[str]:
    text = str(raw or "").strip()
    if not text:
        return list(DEFAULT_AUTO_CLEANUP_KEYS)
    if text.lower() == "all":
        return []
    keys: list[str] = []
    for chunk in text.replace(";", ",").split(","):
        value = chunk.strip()
        if value and value not in keys:
            keys.append(value)
    return keys


def _log_result(result: dict[str, Any]) -> None:
    summary = result.get("summary") or {}
    warnings = result.get("warnings") or []
    logger.info(
        "RETENTION_CLEANUP_CYCLE mode=%s deleted_rows=%s updated_rows=%s deleted_files=%s warnings=%s",
        result.get("mode"),
        summary.get("deleted_rows", 0),
        summary.get("updated_rows", 0),
        summary.get("deleted_files", 0),
        len(warnings),
    )
    for warning in warnings[:5]:
        logger.warning("RETENTION_CLEANUP_WARNING %s", warning)
