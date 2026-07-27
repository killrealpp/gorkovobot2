from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from app.core.config import get_settings
from app.data.admin_profile import payment_prepayment_percent, service_availability_duration_minutes
from app.data.services import load_services, service_title, service_variants, variant_by_title
from app.dialog.availability_cache import get_cached_times
from app.dialog.state import BookingDraft
from app.dialog.pricing import calculate_booking_price, extra_hour_price_breakdown
from app.storage import sqlite

logger = logging.getLogger(__name__)


_GENERIC_GAZEBO_VARIANTS = {"", "беседка", "обычная беседка", "gazebo"}


def _is_generic_gazebo_variant(value: str | None) -> bool:
    return str(value or "").strip().lower().replace("ё", "е") in _GENERIC_GAZEBO_VARIANTS


@dataclass
class Availability:
    ok: bool
    message: str
    variants: list[str]


def suitable_variants(draft: BookingDraft) -> list[dict[str, Any]]:
    variants = service_variants(draft.service_type)
    if not variants:
        config = load_services().get(draft.service_type or "") or {}
        variants = [config] if config else []
    weekday = datetime.fromisoformat(draft.date).date().weekday() if draft.date else None
    result: list[dict[str, Any]] = []
    for variant in variants:
        if variant.get("weekdays") and weekday is not None and weekday not in variant["weekdays"]:
            continue
        if draft.guests_count and variant.get("capacity_max") and draft.guests_count > int(variant["capacity_max"]):
            continue
        if draft.duration and variant.get("duration_minutes"):
            requested_minutes = int(float(draft.duration) * 60)
            requested_minutes = service_availability_duration_minutes(draft.service_type, requested_minutes) or requested_minutes
            if requested_minutes != int(variant["duration_minutes"]):
                continue
        result.append(variant)
    result.sort(key=lambda item: (int(item.get("capacity_max") or 9999), int(item.get("price") or 999999)))
    return result[:10]

def _parse_record_datetime(record: dict[str, Any]) -> datetime | None:
    """Return real start datetime of a YClients record.

    Important: YClients may include a plain `date` field that reflects the
    requested calendar day, not the actual start of the booking. For the
    object business-day rule we must rely on fields that contain both date
    and time, or on nested appointments.
    """
    for key in ("datetime", "seance_date", "start_datetime", "date_time"):
        raw = record.get(key)
        if not raw:
            continue
        text = str(raw).replace("Z", "+00:00")
        if len(text) < 16:
            continue
        try:
            dt = datetime.fromisoformat(text)
            return dt.replace(tzinfo=None)
        except ValueError:
            try:
                return datetime.strptime(text[:19], "%Y-%m-%dT%H:%M:%S")
            except ValueError:
                pass
    appointments = record.get("appointments") or []
    if isinstance(appointments, list):
        for item in appointments:
            if isinstance(item, dict):
                nested = _parse_record_datetime(item)
                if nested:
                    return nested
    return None


def _record_start_date(record: dict[str, Any]) -> str | None:
    dt = _parse_record_datetime(record)
    return dt.date().isoformat() if dt else None


def _record_duration_minutes(record: dict[str, Any]) -> int | None:
    for key in ("duration", "duration_minutes", "length", "seance_length"):
        raw = record.get(key)
        if raw in (None, ""):
            continue
        try:
            value = float(raw)
        except (TypeError, ValueError):
            continue
        # YClients integrations usually return minutes, but sometimes seconds.
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
    """Convert API datetimes to local naive values before comparisons.

    YClients sometimes returns offset-aware values (+03:00), while internally we
    build target day cutoffs as naive local datetimes. Comparing those directly
    raises: can't compare offset-naive and offset-aware datetimes.
    """
    if dt.tzinfo is None:
        return dt
    return dt.astimezone(ZoneInfo(get_settings().app_timezone)).replace(tzinfo=None)


def _business_day_cutoff(service_type: str) -> str:
    service = load_services().get(service_type) or {}
    return str(service.get("business_day_cutoff") or "08:00")


def _record_blocks_business_day(record: dict[str, Any], *, date_str: str, service_type: str) -> bool:
    """Apply the object-specific cutoff for day-level availability.

    Most objects use an 08:00 checkout boundary. The warm gazebo is rented
    from 14:00 until 12:00 the next day: checkout exactly at 12:00 does not
    block the next arrival at 14:00, while an overrun past 12:00 does.

    If duration is unavailable, stay practical:
    - starts on target date at/after 08:00 => blocks;
    - starts before target date => does not block (most common case is evening
      booking ending at night/early morning).
    """
    start_dt = _parse_record_datetime(record)
    if not start_dt:
        # If we cannot read real start time, be conservative: treat as busy.
        return True
    start_dt = _local_naive(start_dt)

    cutoff_time = _business_day_cutoff(service_type)
    target_start = datetime.fromisoformat(f"{date_str} 00:00:00")
    cutoff = datetime.fromisoformat(f"{date_str} {cutoff_time}:00")
    target_end = target_start + timedelta(days=1)

    duration_minutes = _record_duration_minutes(record)
    if duration_minutes:
        end_dt = start_dt + timedelta(minutes=duration_minutes)
        blocks = start_dt < target_end and end_dt > cutoff
    else:
        blocks = start_dt.date().isoformat() == date_str and start_dt >= cutoff

    logger.info(
        "AVAIL_BUSINESS_DAY_RECORD service_type=%s target_date=%s cutoff=%s start=%s duration_min=%s blocks=%s",
        service_type,
        date_str,
        cutoff_time,
        start_dt.isoformat(),
        duration_minutes,
        blocks,
    )
    return blocks


def _has_any_booking_for_date(service_type: str, date_str: str, staff_id: str) -> bool:
    from app.integrations.yclients import YClientsClient
    try:
        client = YClientsClient()
        records = client.get_records(start_date=date_str, end_date=date_str, page=1)
        for r in records:
            if str(r.get('staff_id', '')) != staff_id:
                continue
            if not _record_blocks_business_day(r, date_str=date_str, service_type=service_type):
                logger.info(
                    "AVAIL_IGNORE_BEFORE_CUTOFF_RECORD service_type=%s target_date=%s cutoff=%s staff_id=%s record_start_date=%s",
                    service_type, date_str, _business_day_cutoff(service_type), staff_id, _record_start_date(r),
                )
                continue
            return True
        return False
    except Exception as exc:
        logger.warning("AVAIL_RECORD_BUSY_CHECK_FAILED date=%s staff_id=%s error=%s", date_str, staff_id, exc)
        return False


def _any_staff_busy_for_date(service_type: str, date: str, variants: list[dict[str, Any]]) -> bool:
    """Whether YClients has any real booking for the relevant staff/date."""
    seen: set[str] = set()
    for variant in variants:
        staff_id = str(variant.get("yclients_staff_id") or "")
        if not staff_id or staff_id in seen:
            continue
        seen.add(staff_id)
        if _has_any_booking_for_date(service_type, date, staff_id):
            return True
    return False


def _any_variant_has_live_schedule(variants: list[dict[str, Any]], date: str) -> bool | None:
    """Return whether YClients exposes at least one book time for these variants.

    `False` means the API responded successfully for at least one variant/staff,
    but every response had zero book times. Combined with zero records this is
    treated as "schedule is not opened yet", not as "free".
    `None` means we could not verify live schedule at all.
    """
    checked = False
    seen_keys: set[tuple[str, str]] = set()
    for variant in variants:
        service_id = str(variant.get("yclients_service_id") or "")
        staff_id = str(variant.get("yclients_staff_id") or "")
        if not service_id or not staff_id:
            continue
        key = (staff_id, service_id)
        if key in seen_keys:
            continue
        seen_keys.add(key)
        live_times = _live_book_times_for_variant(variant, date)
        if live_times is None:
            continue
        checked = True
        if live_times:
            return True
    return False if checked else None

def check_availability(draft: BookingDraft, *, chat_id: str | None = None) -> Availability:
    if not draft.service_type or not draft.date:
        return Availability(False, "", [])

    variants = suitable_variants(draft)

    # Общая доступность бани на дату не зависит от выбранной длительности:
    # по правилам базы любая бронь, занимающая этот день после 08:00, закрывает
    # весь день. Длительность нужна позже — для оформления и проверки точного
    # времени, но не для ответа на вопрос «баня свободна в этот день?».
    if draft.service_type == "bathhouse" and not draft.duration:
        schedule_state = _any_variant_has_live_schedule(variants, draft.date)
        if _any_staff_busy_for_date(draft.service_type, draft.date, variants):
            return Availability(False, "date_busy", [])
        if _date_has_active_hold_safe(draft, chat_id=chat_id):
            return Availability(False, "date_held", [])
        if schedule_state is False:
            logger.info("AVAIL_SCHEDULE_NOT_OPEN service_type=%s date=%s stage=bathhouse_date", draft.service_type, draft.date)
            return Availability(False, "schedule_not_open", [])
        titles = [str(item.get("title") or "") for item in variants if item.get("title")]
        return Availability(True, "date_available", list(dict.fromkeys(titles)))

    # "Беседка" is a category, not a concrete YClients object. Older dialogs and
    # occasional LLM replies may still leave that generic value in the draft.
    # Treat it exactly like an empty selection and check every suitable gazebo;
    # otherwise _selected_variant() falls back to the first variant and the bot
    # can incorrectly claim that all gazebos are busy based on one object.
    if draft.service_type == "gazebo" and _is_generic_gazebo_variant(draft.service_variant):
        titles: list[str] = []
        for item in variants:
            title = str(item.get("title") or "")
            service_id = str(item.get("yclients_service_id") or "")
            staff_id = str(item.get("yclients_staff_id") or "")
            if not title:
                continue
            if not service_id or not staff_id:
                continue
            known, cached_times = get_cached_times(staff_id=staff_id, service_id=service_id, date=draft.date)

            if _has_any_booking_for_date(draft.service_type, draft.date, staff_id):
                continue

            if draft.time and draft.duration:
                probe = BookingDraft.from_dict(draft.to_dict())
                probe.service_variant = title
                if _active_hold_exists_safe(probe, chat_id=chat_id):
                    continue

            if known and cached_times:
                if "day" in {str(t) for t in cached_times if t}:
                    schedule_state = _any_variant_has_live_schedule([item], draft.date)
                    if schedule_state is False:
                        logger.info("AVAIL_SCHEDULE_NOT_OPEN service_type=%s date=%s title=%s", draft.service_type, draft.date, title)
                        continue
                titles.append(title)
        if titles:
            return Availability(True, "", titles)
        if _any_variant_has_live_schedule(variants, draft.date) is False and not _any_staff_busy_for_date(draft.service_type, draft.date, variants):
            logger.info("AVAIL_SCHEDULE_NOT_OPEN service_type=%s date=%s stage=gazebo_options", draft.service_type, draft.date)
            return Availability(False, "schedule_not_open", [])
        return Availability(False, "", [])

    selected = _selected_variant(draft, variants)
    if not selected:
        return Availability(False, "", [])
    service_id = str(selected.get("yclients_service_id") or "")
    staff_id = str(selected.get("yclients_staff_id") or "")
    if not service_id or not staff_id:
        return Availability(False, "", [])

    # Date-only guard for day-level objects. If YClients already has a real
    # booking for this object/date, or another chat has a fresh local hold, do
    # not say "available" at the date step. Offer notification/another date.
    if draft.service_type in {"bathhouse", "house", "warm_gazebo"} and not draft.time:
        if _has_any_booking_for_date(draft.service_type, draft.date, staff_id):
            return Availability(False, "date_busy", [])
        if _date_has_active_hold_safe(draft, chat_id=chat_id):
            return Availability(False, "date_held", [])

    known, cached_times = get_cached_times(staff_id=staff_id, service_id=service_id, date=draft.date)
    if not known:
        schedule_state = _any_variant_has_live_schedule([selected], draft.date)
        if schedule_state is False and not _has_any_booking_for_date(draft.service_type, draft.date, staff_id):
            logger.info("AVAIL_SCHEDULE_NOT_OPEN service_type=%s date=%s title=%s stage=unknown_cache", draft.service_type, draft.date, selected.get("title"))
            return Availability(False, "schedule_not_open", [])
        return Availability(True, "", [str(selected.get("title") or "")])

    busy_for_staff = _has_any_booking_for_date(draft.service_type, draft.date, staff_id)
    if busy_for_staff:
        return Availability(False, "", [])

    if not cached_times:
        schedule_state = _any_variant_has_live_schedule([selected], draft.date)
        if schedule_state is False:
            logger.info("AVAIL_SCHEDULE_NOT_OPEN service_type=%s date=%s title=%s stage=empty_times", draft.service_type, draft.date, selected.get("title"))
            return Availability(False, "schedule_not_open", [])

    normalized_times = sorted(set(str(item) for item in cached_times if item))
    title = str(selected.get("title") or "")

    # Для всех объектов кэш хранит маркер
    # "day": значит объект свободен на всю дату. В таком случае не надо искать
    # конкретное время старта в get_book_times — иначе после оплаты бронь на 10 часов
    # ошибочно уходит в ручную проверку с причиной "доступность не подтвердилась".
    if "day" in normalized_times:
        live_times = _live_book_times_for_variant(selected, draft.date)
        if live_times == []:
            logger.info("AVAIL_SCHEDULE_NOT_OPEN service_type=%s date=%s title=%s stage=day_marker_without_book_times", draft.service_type, draft.date, title)
            return Availability(False, "schedule_not_open", [])
        if draft.time and draft.duration and _active_hold_exists_safe(draft, chat_id=chat_id):
            return Availability(False, "time_held", [])
        # День свободен по /records, но перед оплатой/созданием записи
        # YClients ещё должен разрешать конкретное время старта для выбранной услуги.
        # Иначе получаем оплату, а /book_record потом отвечает 422.
        if draft.time and live_times is not None:
            if draft.time in live_times:
                return Availability(True, "", [title])
            return Availability(False, "time_not_available", live_times)
        return Availability(True, "", [title])

    if not draft.time:
        return Availability(True, "", [title])

    if draft.time in normalized_times:
        if draft.duration and _active_hold_exists_safe(draft, chat_id=chat_id):
            return Availability(False, "time_held", [])
        return Availability(True, "", [title])

    return Availability(False, "time_not_available", normalized_times)



def _live_book_times_for_variant(variant: dict[str, Any], date: str) -> list[str] | None:
    service_id = str(variant.get("yclients_service_id") or "")
    staff_id = str(variant.get("yclients_staff_id") or "")
    if not service_id or not staff_id or not date:
        return None
    try:
        from app.integrations.yclients import YClientsClient
        raw_times = YClientsClient().get_book_times(staff_id=staff_id, service_id=service_id, date=date)
    except Exception as exc:
        logger.warning("YCLIENTS live slot check failed staff_id=%s service_id=%s date=%s error=%s", staff_id, service_id, date, exc)
        return None
    result = sorted(set(t for item in raw_times if (t := _extract_time(item))))
    logger.info("YCLIENTS_LIVE_SLOT_CHECK date=%s staff_id=%s service_id=%s times=%s", date, staff_id, service_id, result[:20])
    return result


def list_available_dates(
    draft: BookingDraft,
    *,
    days: int = 14,
    limit: int = 5,
    chat_id: str | None = None,
) -> list[dict[str, Any]]:
    if not draft.service_type:
        return []
    start_date = datetime.fromisoformat(draft.date).date() if draft.date else datetime.now().date()
    variants = suitable_variants(draft)
    results: list[dict[str, Any]] = []
    for offset in range(days):
        date = (start_date + timedelta(days=offset)).isoformat()
        for variant in variants:
            probe = BookingDraft.from_dict(draft.to_dict())
            probe.date = date
            probe.service_variant = str(variant.get("title") or probe.service_variant or "")
            service_id = str(variant.get("yclients_service_id") or "")
            staff_id = str(variant.get("yclients_staff_id") or "")
            if not service_id or not staff_id:
                continue
            known, cached_times = get_cached_times(staff_id=staff_id, service_id=service_id, date=date)
            if not known:
                continue
            normalized_times = sorted(cached_times)
            if not normalized_times:
                continue
            if _has_any_booking_for_date(draft.service_type, date, staff_id):
                continue
            if draft.time and draft.time not in normalized_times:
                shown_times = normalized_times[:3]
            else:
                shown_times = [draft.time] if draft.time else normalized_times[:3]
            if _active_hold_exists_safe(probe, chat_id=chat_id):
                continue
            results.append({"date": date, "title": probe.service_variant, "times": shown_times})
            if len(results) >= limit:
                return results
    return results


def build_yclients_payload(draft: BookingDraft) -> dict[str, Any]:
    variants = suitable_variants(draft)
    selected = _selected_variant(draft, variants)
    if not selected:
        raise RuntimeError("Cannot resolve YCLIENTS service/staff ids")
    service_id = str(selected.get("yclients_service_id") or "")
    staff_id = str(selected.get("yclients_staff_id") or "")
    if not service_id or not staff_id:
        raise RuntimeError("YCLIENTS ids are not configured")
    dt = datetime.fromisoformat(f"{draft.date} {draft.time}:00").replace(tzinfo=ZoneInfo(get_settings().app_timezone))
    appointment = {
        "id": 1,
        "services": [int(service_id)],
        "staff_id": int(staff_id),
        "datetime": dt.strftime("%Y-%m-%dT%H:%M:%S"),
    }

    # Важно: не передаём duration/length в YClients. Для бани на 8-10 часов
    # выбирается service_id базовой услуги на 7 часов, а фактическая длительность
    # и доплата фиксируются в комментарии. Так YClients не пытается найти
    # несуществующую услугу "10 часов" и не ломает создание записи после оплаты.
    return {
        "phone": _digits_phone(draft.phone or ""),
        "fullname": draft.client_name or "Клиент",
        "email": "",
        "comment": _comment(draft),
        "notify_by_sms": 0,
        "notify_by_email": 0,
        "appointments": [appointment],
    }


def booking_period(draft: BookingDraft) -> str:
    if not draft.time or not draft.duration:
        return "время не указано"
    start = datetime.fromisoformat(f"2000-01-01 {draft.time}:00")
    end = start + timedelta(hours=float(draft.duration))
    suffix = " следующего дня" if end.day != start.day else ""
    return f"с {start:%H:%M} до {end:%H:%M}{suffix}"


def _selected_variant(draft: BookingDraft, variants: list[dict[str, Any]]) -> dict[str, Any] | None:
    if draft.service_variant:
        if draft.service_type == "gazebo" and _is_generic_gazebo_variant(draft.service_variant):
            return None
        return variant_by_title(draft.service_type, draft.service_variant) or (variants[0] if variants else None)
    return variants[0] if variants else None


def _extract_time(value: Any) -> str | None:
    if isinstance(value, str):
        return _normalize_time(value)
    if isinstance(value, dict):
        if value.get("time"):
            return _normalize_time(str(value["time"]))
        for key in ("datetime", "seance_date"):
            if value.get(key):
                raw = str(value[key])
                if "T" in raw:
                    try:
                        return datetime.fromisoformat(raw.replace("Z", "+00:00")).strftime("%H:%M")
                    except ValueError:
                        return _normalize_time(raw[11:16])
                return _normalize_time(raw)
    return None


def _normalize_time(value: str) -> str | None:
    try:
        parts = value.strip()[:5].split(":")
        hour = int(parts[0])
        minute = int(parts[1]) if len(parts) > 1 else 0
        if 0 <= hour <= 23 and 0 <= minute <= 59:
            return f"{hour:02d}:{minute:02d}"
    except Exception:
        return None
    return None



def _date_has_active_hold_safe(draft: BookingDraft, *, chat_id: str | None = None) -> bool:
    """Return True if another chat/payment already holds this object on the date.

    For day-level objects such as bathhouse/house/warm gazebo, a concrete local
    hold must make the date look busy before we collect more details. Otherwise
    the bot says "date is free" while another user is already reserving an
    overlapping slot and waiting for payment.
    """
    try:
        rows = sqlite.active_holds_for_service_date(draft.service_type or "", draft.date or "", ignore_chat_id=chat_id)
    except Exception:
        return False
    if not rows:
        return False
    variant = draft.service_variant or ""
    for row in rows:
        row_variant = row.get("service_variant") or ""
        if not variant or row_variant == variant:
            logger.info(
                "AVAIL_DATE_LOCAL_HOLD_CONFLICT chat_id=%s service_type=%s date=%s other=%s",
                chat_id, draft.service_type, draft.date, {k: row.get(k) for k in ("chat_id", "time", "duration", "status")}
            )
            return True
    return False

def _active_hold_exists_safe(draft: BookingDraft, *, chat_id: str | None = None) -> bool:
    try:
        return sqlite.active_hold_exists(draft.to_dict(), ignore_chat_id=chat_id)
    except Exception:
        return False


def _digits_phone(phone: str) -> str:
    digits = "".join(ch for ch in phone if ch.isdigit())
    if digits.startswith("8") and len(digits) == 11:
        return "7" + digits[1:]
    if digits.startswith("9") and len(digits) == 10:
        return "7" + digits
    return digits


def _comment(draft: BookingDraft) -> str:
    upsells = ", ".join(draft.upsell_items) or "не указаны"
    price = calculate_booking_price(draft)
    price_text = f"{price:,}".replace(",", " ") + " ₽" if price else "не рассчитана"
    lines = [
        "Заявка из Telegram-бота.",
        f"Объект: {service_title(draft.service_type)}.",
        f"Дата: {draft.date or 'не указана'}.",
        f"Время: {booking_period(draft)}.",
        f"Гостей: {draft.guests_count or 'не указано'}.",
        f"Формат: {draft.event_format or 'не указано'}.",
        f"Допы: {upsells}.",
        f"Итоговая стоимость: {price_text}.",
        "",
        f"Предоплата внесена через YooKassa: {payment_prepayment_percent()}%.",
        f"YooKassa payment_id: {getattr(draft, 'payment_id', None) or 'не указан'}.",
        "Остаток оплачивается при посещении.",
    ]
    breakdown = extra_hour_price_breakdown(draft)
    if breakdown and draft.duration:
        duration = float(draft.duration)
        base_hours = breakdown["base_duration_hours"]
        extra_sum = breakdown["extra_sum"]
        lines.append(
            f"Важно: в YClients используется услуга бани на {base_hours} часов, "
            f"фактическая бронь — {duration:g} часов. "
            f"Доплата сверх {base_hours} часов: {extra_sum:,} ₽.".replace(",", " ")
        )
    return "\n".join(lines)
