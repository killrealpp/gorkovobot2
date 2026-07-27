from __future__ import annotations

import logging
import threading
import time
from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from app.core.config import get_settings
from app.data.services import load_services
from app.integrations.yclients import YClientsClient, YClientsError
from app.storage import sqlite

logger = logging.getLogger(__name__)

_LOCK = threading.Lock()
_REFRESH_LOCK = threading.RLock()
_CACHE: dict[tuple[str, str, str], list[str]] = {}
_LAST_REFRESH: datetime | None = None


def cache_age_seconds() -> float | None:
    return sqlite.availability_cache_age_seconds()


def is_cache_fresh(*, max_age_seconds: int = 900) -> bool:
    age = cache_age_seconds()
    return age is not None and age < max_age_seconds


def refresh_availability_cache_if_stale(
    *,
    days: int = 21,
    max_seconds: int = 180,
    max_age_seconds: int = 900,
    reason: str = "scheduled",
) -> bool:
    age = cache_age_seconds()
    if age is not None and age < max_age_seconds:
        logger.info(
            "Availability cache refresh skipped reason=%s age_seconds=%.0f max_age_seconds=%s",
            reason,
            age,
            max_age_seconds,
        )
        _load_cache_from_db()
        return False

    refresh_availability_cache(days=days, max_seconds=max_seconds, reason=reason)
    return True


def refresh_availability_cache(
    *,
    days: int = 21,
    max_seconds: int = 180,
    reason: str = "manual",
) -> None:
    if reason != "manual" and not _REFRESH_LOCK.acquire(blocking=False):
        logger.info(
            "Availability cache refresh skipped reason=%s another_refresh_running=true",
            reason,
        )
        return

    try:
        _refresh_availability_cache_locked(
            days=days,
            max_seconds=max_seconds,
            reason=reason,
        )
    finally:
        if reason != "manual":
            _REFRESH_LOCK.release()


def _refresh_availability_cache_locked(
    *,
    days: int = 21,
    max_seconds: int = 180,
    reason: str = "manual",
) -> None:
    global _CACHE, _LAST_REFRESH

    # The stored business horizon is 90 days. Cap stale environment values such
    # as 180 to avoid hammering YClients with hundreds of per-day requests.
    days = max(1, min(int(days), 90))

    if reason == "booking_created" and _LAST_REFRESH:
        seconds_ago = (datetime.utcnow() - _LAST_REFRESH).total_seconds()
        if seconds_ago < 60:
            logger.info("Skipping cache refresh, last was %.0f sec ago", seconds_ago)
            return

    if reason == "booking_created":
        days = max(days, 21)
        max_seconds = max(max_seconds, 180)

    started = time.monotonic()
    today = datetime.now(ZoneInfo(get_settings().app_timezone)).date()

    client = YClientsClient()
    rows: list[dict[str, str]] = []
    cache: dict[tuple[str, str, str], list[str]] = {}

    items = _iter_yclients_items()
    staff_map: dict[str, list[dict[str, Any]]] = {}
    cutoff_by_staff: dict[str, str] = {}

    for service_type, service, variant in items:
        service_id = str(variant.get("yclients_service_id") or service.get("yclients_service_id") or "")
        staff_id = str(variant.get("yclients_staff_id") or service.get("yclients_staff_id") or "")
        title = str(variant.get("title") or service.get("title") or service_type)
        block_full_day = bool(service.get("block_full_day_on_any_booking"))
        business_day_cutoff = str(service.get("business_day_cutoff") or "08:00")

        if not service_id or not staff_id:
            continue

        staff_map.setdefault(staff_id, []).append(
            {
                "service_type": service_type,
                "title": title,
                "service_id": service_id,
                "staff_id": staff_id,
                "block_full_day": block_full_day,
            }
        )
        cutoff_by_staff[staff_id] = business_day_cutoff

    unique_staff_ids = list(staff_map.keys())

    logger.info(
        "Availability cache refresh started reason=%s days=%s staff_ids=%s variants=%s max_seconds=%s",
        reason,
        days,
        len(unique_staff_ids),
        len(items),
        max_seconds,
    )

    request_count = 0

    for offset in range(days):
        if time.monotonic() - started >= max_seconds:
            logger.warning("Availability cache refresh stopped by time budget rows=%s", len(rows))
            break

        date = (today + timedelta(days=offset)).isoformat()
        busy_staff_ids = _busy_staff_ids_for_date(client, date, cutoff_by_staff=cutoff_by_staff)

        logger.info("AVAIL_RECORDS date=%s busy_staff_ids=%s", date, sorted(busy_staff_ids))

        for staff_id in unique_staff_ids:
            if time.monotonic() - started >= max_seconds:
                break

            staff_items = staff_map.get(staff_id, [])
            full_day_items = [item for item in staff_items if item["block_full_day"]]
            slot_items = [item for item in staff_items if not item["block_full_day"]]

            for item in full_day_items:
                service_type = str(item["service_type"])
                title = str(item["title"])
                service_id = str(item["service_id"])
                sid = str(item["staff_id"])
                key = (sid, service_id, date)

                if sid in busy_staff_ids:
                    cache[key] = []
                    rows.append(
                        {
                            "service_type": service_type,
                            "title": title,
                            "date": date,
                            "time": "",
                            "service_id": service_id,
                            "staff_id": sid,
                            "status": "empty",
                        }
                    )
                    logger.debug(
                        "AVAIL_FULL_DAY_BUSY date=%s title=%s staff_id=%s service_id=%s",
                        date,
                        title,
                        sid,
                        service_id,
                    )
                else:
                    # "day" — маркер для LLM-кэша: объект свободен на дату как дневной объект.
                    cache[key] = ["day"]
                    rows.append(
                        {
                            "service_type": service_type,
                            "title": title,
                            "date": date,
                            "time": "day",
                            "service_id": service_id,
                            "staff_id": sid,
                            "status": "free",
                        }
                    )
                    logger.debug(
                        "AVAIL_FULL_DAY_FREE date=%s title=%s staff_id=%s service_id=%s",
                        date,
                        title,
                        sid,
                        service_id,
                    )

            if not slot_items:
                continue

            request_count += 1
            if request_count % 5 == 0:
                time.sleep(0.5)

            try:
                raw_times = client.get_book_times(staff_id=staff_id, date=date)
                all_times = sorted(
                    set(
                        slot_time
                        for item in raw_times
                        if (slot_time := _extract_time(item))
                    )
                )
                logger.info(
                    "AVAIL_BOOK_TIMES date=%s staff_id=%s times_count=%s times_preview=%s",
                    date,
                    staff_id,
                    len(all_times),
                    all_times[:8],
                )
            except YClientsError as exc:
                logger.warning(
                    "Availability cache YCLIENTS error staff=%s date=%s error=%s",
                    staff_id,
                    date,
                    exc,
                )
                if "429" in str(exc):
                    time.sleep(2)
                all_times = []

            for item in slot_items:
                service_type = str(item["service_type"])
                title = str(item["title"])
                service_id = str(item["service_id"])
                sid = str(item["staff_id"])
                key = (sid, service_id, date)
                cache[key] = all_times

                if all_times:
                    for slot_time in all_times:
                        rows.append(
                            {
                                "service_type": service_type,
                                "title": title,
                                "date": date,
                                "time": slot_time,
                                "service_id": service_id,
                                "staff_id": sid,
                                "status": "free",
                            }
                        )
                else:
                    rows.append(
                        {
                            "service_type": service_type,
                            "title": title,
                            "date": date,
                            "time": "",
                            "service_id": service_id,
                            "staff_id": sid,
                            "status": "empty",
                        }
                    )

    logger.info(
        "AVAIL_ROWS_READY rows=%s free_rows=%s empty_rows=%s sample=%s",
        len(rows),
        sum(1 for row in rows if row.get("status") == "free"),
        sum(1 for row in rows if row.get("status") == "empty"),
        rows[:10],
    )

    refreshed_at = datetime.utcnow().isoformat()
    sqlite.replace_availability_cache(rows, refreshed_at=refreshed_at)

    with _LOCK:
        _CACHE = cache
        _LAST_REFRESH = datetime.fromisoformat(refreshed_at)

    logger.info(
        "Availability cache refreshed reason=%s rows=%s elapsed_s=%.1f",
        reason,
        len(rows),
        time.monotonic() - started,
    )


def refresh_availability_cache_for_date(
    date: str,
    *,
    reason: str = "date_missing",
) -> None:
    """Догружает в кэш ровно одну дату из YClients.

    Используется, когда клиент спрашивает дату, которой ещё нет в таблице.
    Не очищает весь кэш и не трогает остальные даты.
    """
    global _LAST_REFRESH

    with _REFRESH_LOCK:
        if sqlite.availability_date_exists(date):
            logger.info("Availability cache date refresh skipped reason=%s date=%s exists=true", reason, date)
            _load_cache_from_db(force=True)
            return

        client = YClientsClient()
        rows, cache = _build_availability_rows_for_date(client, date)
        refreshed_at = datetime.utcnow().isoformat()
        sqlite.replace_availability_cache_for_date(date, rows, refreshed_at=refreshed_at)

        with _LOCK:
            for key in list(_CACHE.keys()):
                if key[2] == date:
                    _CACHE.pop(key, None)
            _CACHE.update(cache)
            _LAST_REFRESH = datetime.fromisoformat(refreshed_at)

        logger.info(
            "Availability cache single date refreshed reason=%s date=%s rows=%s",
            reason,
            date,
            len(rows),
        )


def ensure_availability_date_cached(date: str, *, reason: str = "date_missing") -> bool:
    _load_cache_from_db()
    if sqlite.availability_date_exists(date):
        return True

    logger.info("AVAIL_DATE_MISSING date=%s refresh_live=true reason=%s", date, reason)
    try:
        refresh_availability_cache_for_date(date, reason=reason)
        return True
    except Exception:
        logger.exception("Failed to refresh missing availability date=%s", date)
        return False


def _build_availability_rows_for_date(
    client: YClientsClient,
    date: str,
) -> tuple[list[dict[str, str]], dict[tuple[str, str, str], list[str]]]:
    rows: list[dict[str, str]] = []
    cache: dict[tuple[str, str, str], list[str]] = {}

    items = _iter_yclients_items()
    staff_map: dict[str, list[dict[str, Any]]] = {}
    cutoff_by_staff: dict[str, str] = {}

    for service_type, service, variant in items:
        service_id = str(variant.get("yclients_service_id") or service.get("yclients_service_id") or "")
        staff_id = str(variant.get("yclients_staff_id") or service.get("yclients_staff_id") or "")
        title = str(variant.get("title") or service.get("title") or service_type)
        block_full_day = bool(service.get("block_full_day_on_any_booking"))
        business_day_cutoff = str(service.get("business_day_cutoff") or "08:00")

        if not service_id or not staff_id:
            continue

        staff_map.setdefault(staff_id, []).append(
            {
                "service_type": service_type,
                "title": title,
                "service_id": service_id,
                "staff_id": staff_id,
                "block_full_day": block_full_day,
            }
        )
        cutoff_by_staff[staff_id] = business_day_cutoff

    busy_staff_ids = _busy_staff_ids_for_date(client, date, cutoff_by_staff=cutoff_by_staff)
    logger.info("AVAIL_RECORDS date=%s busy_staff_ids=%s", date, sorted(busy_staff_ids))

    request_count = 0

    for staff_id, staff_items in staff_map.items():
        full_day_items = [item for item in staff_items if item["block_full_day"]]
        slot_items = [item for item in staff_items if not item["block_full_day"]]

        for item in full_day_items:
            service_type = str(item["service_type"])
            title = str(item["title"])
            service_id = str(item["service_id"])
            sid = str(item["staff_id"])
            key = (sid, service_id, date)

            if sid in busy_staff_ids:
                cache[key] = []
                rows.append(
                    {
                        "service_type": service_type,
                        "title": title,
                        "date": date,
                        "time": "",
                        "service_id": service_id,
                        "staff_id": sid,
                        "status": "empty",
                    }
                )
                logger.debug("AVAIL_FULL_DAY_BUSY date=%s title=%s staff_id=%s service_id=%s", date, title, sid, service_id)
            else:
                cache[key] = ["day"]
                rows.append(
                    {
                        "service_type": service_type,
                        "title": title,
                        "date": date,
                        "time": "day",
                        "service_id": service_id,
                        "staff_id": sid,
                        "status": "free",
                    }
                )
                logger.debug("AVAIL_FULL_DAY_FREE date=%s title=%s staff_id=%s service_id=%s", date, title, sid, service_id)

        if not slot_items:
            continue

        request_count += 1
        if request_count % 5 == 0:
            time.sleep(0.5)

        try:
            raw_times = client.get_book_times(staff_id=staff_id, date=date)
            all_times = sorted(
                set(
                    slot_time
                    for item in raw_times
                    if (slot_time := _extract_time(item))
                )
            )
            logger.info(
                "AVAIL_BOOK_TIMES date=%s staff_id=%s times_count=%s times_preview=%s",
                date,
                staff_id,
                len(all_times),
                all_times[:8],
            )
        except YClientsError as exc:
            logger.warning(
                "Availability cache YCLIENTS error staff=%s date=%s error=%s",
                staff_id,
                date,
                exc,
            )
            if "429" in str(exc):
                time.sleep(2)
            all_times = []

        for item in slot_items:
            service_type = str(item["service_type"])
            title = str(item["title"])
            service_id = str(item["service_id"])
            sid = str(item["staff_id"])
            key = (sid, service_id, date)
            cache[key] = all_times

            if all_times:
                for slot_time in all_times:
                    rows.append(
                        {
                            "service_type": service_type,
                            "title": title,
                            "date": date,
                            "time": slot_time,
                            "service_id": service_id,
                            "staff_id": sid,
                            "status": "free",
                        }
                    )
            else:
                rows.append(
                    {
                        "service_type": service_type,
                        "title": title,
                        "date": date,
                        "time": "",
                        "service_id": service_id,
                        "staff_id": sid,
                        "status": "empty",
                    }
                )

    return rows, cache


def get_cached_times(*, staff_id: str, service_id: str, date: str) -> tuple[bool, list[str]]:
    with _LOCK:
        key = (str(staff_id), str(service_id), str(date))
        if key in _CACHE:
            return True, list(_CACHE[key])

    _load_cache_from_db()

    with _LOCK:
        key = (str(staff_id), str(service_id), str(date))
        if key in _CACHE:
            return True, list(_CACHE[key])

    # Если дата вообще отсутствует в таблице, догружаем её live из YClients.
    if date and not sqlite.availability_date_exists(str(date)):
        ensure_availability_date_cached(str(date), reason="date_missing_for_booking_check")

        with _LOCK:
            key = (str(staff_id), str(service_id), str(date))
            if key in _CACHE:
                return True, list(_CACHE[key])

    return False, []


def availability_context_for_llm(
    *,
    service_type: str | None = None,
    date: str | None = None,
    date_to: str | None = None,
    limit: int = 80,
) -> str:
    _load_cache_from_db()

    if date:
        range_end = date_to or date
        try:
            current = datetime.fromisoformat(date).date()
            end = datetime.fromisoformat(range_end).date()
        except ValueError:
            current = end = None
        if current and end and end >= current:
            while current <= end:
                value = current.isoformat()
                if not ensure_availability_date_cached(value, reason="date_missing_for_llm"):
                    return (
                        f"По дате {value} нет актуальных данных доступности. "
                        "Нельзя утверждать, что объекты свободны."
                    )
                current += timedelta(days=1)

    exact_date = date if not date_to or date_to == date else None
    range_start = date if date_to and date_to != date else None
    range_end = date_to if date_to and date_to != date else None
    try:
        rows = sqlite.list_availability_rows(
            service_type=service_type,
            date=exact_date,
            date_from=range_start,
            date_to=range_end,
            limit=limit * 4,
        )
    except TypeError as exc:
        # Rolling/partial deploy compatibility: an older storage module does not
        # yet accept date_from/date_to. Query each date exactly instead of taking
        # the bot down while the second file is being updated.
        if "date_from" not in str(exc) and "date_to" not in str(exc):
            raise
        logger.warning(
            "AVAIL_STORAGE_RANGE_API_OLD date=%s date_to=%s; using exact-date fallback",
            date,
            date_to,
        )
        if not range_start or not range_end:
            rows = sqlite.list_availability_rows(
                service_type=service_type,
                date=exact_date,
                limit=limit * 4,
            )
        else:
            rows = []
            current = datetime.fromisoformat(range_start).date()
            end = datetime.fromisoformat(range_end).date()
            while current <= end and len(rows) < limit * 4:
                rows.extend(sqlite.list_availability_rows(
                    service_type=service_type,
                    date=current.isoformat(),
                    limit=max(1, limit * 4 - len(rows)),
                ))
                current += timedelta(days=1)

    logger.debug(
        "AVAIL_CONTEXT_FOR_LLM service_type=%s date=%s date_to=%s rows=%s sample=%s",
        service_type,
        date,
        date_to,
        len(rows),
        rows[:20],
    )

    available_by_date: dict[str, set[str]] = {}
    unavailable_by_date: dict[str, set[str]] = {}

    for row in rows:
        row_date = str(row.get("date") or "")
        title = str(row.get("title") or "").strip()
        status = str(row.get("status") or "")
        time_value = row.get("time")

        if not row_date or not title:
            continue

        if status == "free" and time_value:
            available_by_date.setdefault(row_date, set()).add(title)
        else:
            unavailable_by_date.setdefault(row_date, set()).add(title)

    for row_date, titles in available_by_date.items():
        unavailable_by_date.setdefault(row_date, set()).difference_update(titles)

    dates = sorted(set(available_by_date.keys()) | set(unavailable_by_date.keys()))

    if not dates:
        logger.debug("AVAIL_CONTEXT_TEXT_FOR_LLM: empty")
        return "Кэш доступности пуст. Нельзя утверждать, что объекты свободны."

    lines: list[str] = [
        "Правило доступности:",
        "Свободными считаются только объекты из блока ДОСТУПНО.",
        "Объекты из блока НЕДОСТУПНО нельзя предлагать клиенту как свободные.",
        "services_catalog можно использовать для описания объектов, цен, вместимости и характеристик, но не для вывода о свободности.",
        "Если клиент спрашивает общую доступность на дату, перечисли все подходящие свободные типы объектов из блока ДОСТУПНО, а не только объект из current_draft.",
        "",
    ]

    for row_date in dates:
        available = sorted(available_by_date.get(row_date) or [])
        unavailable = sorted(unavailable_by_date.get(row_date) or [])

        lines.append(f"ДАТА {row_date}")

        if available:
            lines.append("ДОСТУПНО:")
            lines.extend(f"- {title}" for title in available)
        else:
            lines.append("ДОСТУПНО: ничего")

        if unavailable:
            lines.append("НЕДОСТУПНО:")
            lines.extend(f"- {title}" for title in unavailable)

        lines.append("")

    suffix = (
        f"Обновлено: {_LAST_REFRESH.isoformat(timespec='minutes')}"
        if _LAST_REFRESH
        else "Обновлено: неизвестно"
    )
    context = suffix + "\n" + "\n".join(lines)

    logger.debug("AVAIL_CONTEXT_TEXT_FOR_LLM:\n%s", context[:6000])
    return context


def availability_object_dates_for_llm(
    *,
    title: str,
    date_from: str | None = None,
    limit: int = 21,
) -> str:
    _load_cache_from_db()

    rows = sqlite.list_availability_rows(
        service_type=None,
        date=None,
        limit=5000,
    )

    query_title = str(title or "").strip()
    normalized_query = _normalize_title_for_match(query_title)

    by_date: dict[str, dict[str, set[str]]] = {}

    for row in rows:
        row_date = str(row.get("date") or "")
        row_title = str(row.get("title") or "").strip()
        status = str(row.get("status") or "")
        time_value = row.get("time")

        if not row_date or not row_title:
            continue

        if date_from and row_date < date_from:
            continue

        if not _title_matches_object(row_title, normalized_query):
            continue

        by_date.setdefault(row_date, {"available": set(), "unavailable": set()})

        if status == "free" and time_value:
            by_date[row_date]["available"].add(row_title)
        else:
            by_date[row_date]["unavailable"].add(row_title)

    dates = sorted(by_date.keys())[:limit]

    if not dates:
        return (
            f"По объекту «{query_title}» нет данных в кэше. "
            "Нельзя утверждать, что он свободен."
        )

    lines: list[str] = [
        f"Доступность объекта: {query_title}",
        "Правило: если дата в НЕДОСТУПНО, нельзя говорить, что объект свободен в эту дату.",
        "",
    ]

    first_available_date: str | None = None

    for row_date in dates:
        available = by_date[row_date]["available"]
        unavailable = by_date[row_date]["unavailable"]

        # Если у одного объекта несколько вариантов длительности, достаточно одного свободного варианта,
        # чтобы назвать объект доступным на дату.
        if available:
            lines.append(f"{row_date}: ДОСТУПНО")
            lines.append("  Свободные варианты:")
            for item in sorted(available):
                lines.append(f"  - {item}")
            if not first_available_date:
                first_available_date = row_date
        elif unavailable:
            lines.append(f"{row_date}: НЕДОСТУПНО")
            lines.append("  Недоступные варианты:")
            for item in sorted(unavailable):
                lines.append(f"  - {item}")
        else:
            lines.append(f"{row_date}: НЕТ ДАННЫХ")

    lines.append("")

    if first_available_date:
        lines.append(f"Ближайшая свободная дата: {first_available_date}")
    else:
        lines.append("Ближайшая свободная дата в текущем диапазоне не найдена.")

    context = "\n".join(lines)
    logger.debug("AVAIL_OBJECT_CONTEXT_FOR_LLM:\n%s", context[:6000])
    return context


def _title_matches_object(row_title: str, normalized_query: str) -> bool:
    row = _normalize_title_for_match(row_title)
    query = normalized_query

    if not query:
        return False

    if row == query:
        return True

    # "Баня с бассейном" должна матчить "Баня с бассейном, 3 часа".
    if row.startswith(query + ",") or row.startswith(query + " "):
        return True

    # "Гостевой дом" должна матчить "Гостевой дом, сутки/4 часа".
    if query in {"баня с бассейном", "гостевой дом"} and row.startswith(query):
        return True

    return False


def _normalize_title_for_match(value: str) -> str:
    value = value.lower().replace("ё", "е")
    value = value.replace("№", "")
    value = value.replace("#", "")
    value = value.replace("теплая", "теплая")
    value = " ".join(value.split())
    return value


def busy_staff_ids_for_date(date: str) -> set[str]:
    client = YClientsClient()
    cutoff_by_staff: dict[str, str] = {}
    for _service_type, service, variant in _iter_yclients_items():
        staff_id = str(variant.get("yclients_staff_id") or service.get("yclients_staff_id") or "")
        if staff_id:
            cutoff_by_staff[staff_id] = str(service.get("business_day_cutoff") or "08:00")
    return _busy_staff_ids_for_date(client, date, cutoff_by_staff=cutoff_by_staff)


def _busy_staff_ids_for_date(
    client: YClientsClient,
    date: str,
    *,
    cutoff_by_staff: dict[str, str] | None = None,
) -> set[str]:
    busy: set[str] = set()

    try:
        for page in range(1, 6):
            records = client.get_records(start_date=date, end_date=date, page=page)

            logger.info(
                "YCLIENTS_RECORDS_PAGE date=%s page=%s count=%s",
                date,
                page,
                len(records or []),
            )

            if not records:
                break

            for record in records:
                if not isinstance(record, dict):
                    continue

                staff_id = _record_staff_id(record)
                start_dt = _record_start_datetime(record)
                cancelled = _record_is_cancelled(record)

                logger.info(
                    "YCLIENTS_RECORD_PARSED target_date=%s staff_id=%s start_dt=%s cancelled=%s service_titles=%s",
                    date,
                    staff_id,
                    start_dt.isoformat() if start_dt else None,
                    cancelled,
                    _record_service_titles(record),
                )

                if cancelled or not staff_id:
                    continue

                if not start_dt:
                    busy.add(staff_id)
                    continue

                cutoff_time = (cutoff_by_staff or {}).get(staff_id, "08:00")
                if _record_blocks_business_day(
                    record,
                    target_date=date,
                    start_dt=start_dt,
                    cutoff_time=cutoff_time,
                ):
                    busy.add(staff_id)
                else:
                    logger.info(
                        "YCLIENTS_RECORD_SKIP_OVERLAP target_date=%s staff_id=%s record_start_date=%s start_time=%s",
                        date,
                        staff_id,
                        start_dt.date().isoformat(),
                        start_dt.strftime("%H:%M"),
                    )

            if len(records) < 200:
                break

    except Exception as exc:
        logger.warning("YCLIENTS_RECORDS_BUSY_CHECK_FAILED date=%s error=%s", date, exc)

    return busy


def _record_duration_minutes(record: dict[str, Any]) -> int | None:
    for key in ("duration", "duration_minutes", "length", "seance_length"):
        raw = record.get(key)
        if raw in (None, ""):
            continue
        try:
            value = float(raw)
        except (TypeError, ValueError):
            continue
        # Some APIs return seconds instead of minutes.
        if value > 24 * 60:
            value = value / 60
        return int(value)

    appointments = record.get("appointments") or []
    if isinstance(appointments, list):
        for item in appointments:
            if isinstance(item, dict):
                nested = _record_duration_minutes(item)
                if nested:
                    return nested
    return None


def _local_naive(dt: datetime) -> datetime:
    """Convert API datetimes to local naive values before availability comparisons."""
    if dt.tzinfo is None:
        return dt
    return dt.astimezone(ZoneInfo(get_settings().app_timezone)).replace(tzinfo=None)


def _record_blocks_business_day(
    record: dict[str, Any],
    *,
    target_date: str,
    start_dt: datetime,
    cutoff_time: str = "08:00",
) -> bool:
    """Apply the same object-specific cutoff as live availability checks.

    A booking from the previous day blocks the target date only if it ends after
    the configured checkout boundary. The warm gazebo normally runs from 14:00
    until 12:00 the next day, so checkout exactly at noon leaves the next 14:00
    arrival available.
    """
    start_dt = _local_naive(start_dt)
    target_start = datetime.fromisoformat(f"{target_date} 00:00:00")
    cutoff = datetime.fromisoformat(f"{target_date} {cutoff_time}:00")
    target_end = target_start + timedelta(days=1)

    duration_minutes = _record_duration_minutes(record)
    if duration_minutes:
        end_dt = start_dt + timedelta(minutes=duration_minutes)
        blocks = start_dt < target_end and end_dt > cutoff
    else:
        # If duration is unknown, we can only safely block records that start on
        # the target day after 08:00. Previous-day records without duration are
        # treated like early-morning tails and do not close the whole day.
        blocks = start_dt.date().isoformat() == target_date and start_dt.strftime("%H:%M") >= cutoff_time

    logger.info(
        "AVAIL_CACHE_BUSINESS_DAY_RECORD target_date=%s cutoff=%s start=%s duration_min=%s blocks=%s",
        target_date,
        cutoff_time,
        start_dt.isoformat(),
        duration_minutes,
        blocks,
    )
    return blocks


def _record_staff_id(record: dict[str, Any]) -> str:
    if record.get("staff_id"):
        return str(record.get("staff_id"))

    staff = record.get("staff")
    if isinstance(staff, dict) and staff.get("id"):
        return str(staff.get("id"))

    if isinstance(staff, list):
        for item in staff:
            if isinstance(item, dict) and item.get("id"):
                return str(item.get("id"))

    return ""


def _record_start_datetime(record: dict[str, Any]) -> datetime | None:
    for key in ("datetime", "date", "seance_date", "start_at"):
        value = record.get(key)
        if not value:
            continue
        parsed = _parse_datetime(value)
        if parsed:
            return parsed

    date_value = record.get("date")
    time_value = record.get("time") or record.get("start_time")
    if date_value and time_value:
        return _parse_datetime(f"{str(date_value)[:10]} {str(time_value)[:5]}")

    return None


def _parse_datetime(value: Any) -> datetime | None:
    text = str(value).strip()
    if not text:
        return None

    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        pass

    try:
        return datetime.fromisoformat(text.replace("T", " ")[:19])
    except ValueError:
        pass

    return None


def _record_is_cancelled(record: dict[str, Any]) -> bool:
    if record.get("deleted") is True:
        return True

    status = str(record.get("status") or record.get("record_status") or "").lower()
    return status in {"cancelled", "canceled", "deleted", "removed"}


def _record_service_titles(record: dict[str, Any]) -> list[str]:
    result: list[str] = []
    services = record.get("services") or []
    if isinstance(services, list):
        for service in services:
            if isinstance(service, dict) and service.get("title"):
                result.append(str(service.get("title")))
    return result


def _iter_yclients_items() -> list[tuple[str, dict[str, Any], dict[str, Any]]]:
    items: list[tuple[str, dict[str, Any], dict[str, Any]]] = []

    for service_type, service in load_services().items():
        variants = list(service.get("variants") or [])
        if variants:
            for variant in variants:
                items.append((service_type, service, variant))
        elif service.get("yclients_service_id") and service.get("yclients_staff_id"):
            items.append((service_type, service, service))

    priority = {"warm_gazebo": 0, "bathhouse": 1, "house": 2, "gazebo": 3}
    return sorted(items, key=lambda item: priority.get(item[0], 99))


def _load_cache_from_db(*, force: bool = False) -> None:
    if _CACHE and not force:
        return

    cache: dict[tuple[str, str, str], list[str]] = {}
    for row in sqlite.list_availability_rows():
        key = (
            row.get("staff_id", ""),
            row.get("service_id", ""),
            row.get("date", ""),
        )
        if all(key):
            cache.setdefault(key, [])
            if row.get("status") != "empty" and row.get("time"):
                cache[key].append(row.get("time", ""))

    with _LOCK:
        if force:
            _CACHE.clear()
        if not _CACHE:
            _CACHE.update({key: sorted(set(times)) for key, times in cache.items()})


def _extract_time(value: Any) -> str | None:
    if isinstance(value, str):
        return _normalize_time(value)

    if isinstance(value, dict):
        if value.get("time"):
            return _normalize_time(str(value["time"]))
        for key in ("datetime", "seance_date"):
            if value.get(key):
                raw = str(value[key])
                try:
                    return datetime.fromisoformat(raw.replace("Z", "+00:00")).strftime("%H:%M")
                except ValueError:
                    match = raw[11:16] if len(raw) >= 16 else raw[:5]
                    return _normalize_time(match)

    return None


def _normalize_time(value: str) -> str | None:
    value = value.strip()
    if not value:
        return None
    try:
        parts = value[:5].split(":")
        hour = int(parts[0])
        minute = int(parts[1]) if len(parts) > 1 else 0
        if 0 <= hour <= 23 and 0 <= minute <= 59:
            return f"{hour:02d}:{minute:02d}"
    except Exception:
        return None
    return None
