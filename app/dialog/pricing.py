from __future__ import annotations

from datetime import date
from typing import Any

from app.data.services import load_services, service_variants, variant_by_title
from app.dialog.state import BookingDraft


def calculate_booking_price(draft: BookingDraft) -> int | None:
    """Возвращает итоговую стоимость бронирования в рублях.

    Важно: это только расчёт цены для клиента/админа. Логику выбора service_id
    и длительности записи в YClients здесь не трогаем.
    """
    if not draft.service_type:
        return None

    extra_hour_price = _calculate_extra_hour_price(draft)
    if extra_hour_price is not None:
        return extra_hour_price

    return _calculate_regular_price(draft)


def _calculate_bathhouse_price(draft: BookingDraft) -> int | None:
    # Compatibility wrapper for old imports/tests; the value now comes from the
    # profile-backed generic extra-hour rule when configured.
    return _calculate_extra_hour_price(draft)


def _calculate_extra_hour_price(draft: BookingDraft) -> int | None:
    breakdown = extra_hour_price_breakdown(draft)
    return breakdown["total_price"] if breakdown else None


def extra_hour_price_breakdown(draft: BookingDraft) -> dict[str, int] | None:
    duration = _duration_hours(draft.duration)
    if duration is None:
        return None

    config = load_services().get(draft.service_type or "") or {}
    rules = config.get("price_rules") or {}
    after_minutes = _to_int_price(rules.get("extra_hour_after_minutes"))
    extra_price = _to_int_price(rules.get("extra_hour_price"))
    if not after_minutes or extra_price is None:
        return None

    after_hours = int(after_minutes / 60)
    if duration <= after_hours:
        return None

    base_price = _price_for_exact_duration(
        service_type=draft.service_type or "",
        duration_hours=after_hours,
        booking_date=draft.date,
    )
    if base_price is None:
        return None

    extra_hours = duration - after_hours
    extra_sum = extra_hours * extra_price
    return {
        "base_duration_hours": after_hours,
        "extra_hours": extra_hours,
        "extra_hour_price": extra_price,
        "base_price": base_price,
        "extra_sum": extra_sum,
        "total_price": base_price + extra_sum,
    }


def _calculate_regular_price(draft: BookingDraft) -> int | None:
    variant = _find_selected_variant(draft)
    if variant and variant.get("price") is not None:
        return _to_int_price(variant.get("price"))

    config = load_services().get(draft.service_type or "") or {}
    if config.get("price") is not None:
        return _to_int_price(config.get("price"))

    return None


def _find_selected_variant(draft: BookingDraft) -> dict[str, Any] | None:
    if draft.service_variant:
        found = variant_by_title(draft.service_type, draft.service_variant)
        if found:
            return found

    variants = service_variants(draft.service_type)
    if not variants:
        return None

    duration = _duration_hours(draft.duration)
    duration = _mapped_duration_hours(draft.service_type, duration)
    booking_weekday = _weekday(draft.date)

    for variant in variants:
        if not _weekday_matches(variant, booking_weekday):
            continue
        if duration is not None and variant.get("duration_minutes"):
            try:
                if int(variant.get("duration_minutes") or 0) != int(duration * 60):
                    continue
            except (TypeError, ValueError):
                continue
        return variant

    return variants[0] if variants else None


def _mapped_duration_hours(service_type: str | None, duration: int | None) -> int | None:
    if duration is None:
        return None
    config = load_services().get(service_type or "") or {}
    rules = config.get("price_rules") or {}
    for item in rules.get("map_duration_ranges") or []:
        min_exclusive = int(item.get("min_exclusive_minutes") or 0) / 60
        max_inclusive = int(item.get("max_inclusive_minutes") or 0) / 60
        target = int(item.get("target_duration_minutes") or 0) / 60
        if target and duration > min_exclusive and duration <= max_inclusive:
            return int(target)
    return duration


def _price_for_exact_duration(
    *,
    service_type: str,
    duration_hours: int,
    booking_date: str | None,
) -> int | None:
    booking_weekday = _weekday(booking_date)

    for variant in service_variants(service_type):
        try:
            minutes = int(variant.get("duration_minutes") or 0)
        except (TypeError, ValueError):
            continue

        if minutes != int(duration_hours * 60):
            continue
        if not _weekday_matches(variant, booking_weekday):
            continue
        if variant.get("price") is not None:
            return _to_int_price(variant.get("price"))

    return None


def _weekday_matches(variant: dict[str, Any], booking_weekday: int | None) -> bool:
    weekdays = variant.get("weekdays")
    if booking_weekday is None or not weekdays:
        return True
    return booking_weekday in weekdays


def _duration_hours(value: Any) -> int | None:
    if value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number <= 0:
        return None
    return int(number)


def _weekday(value: str | None) -> int | None:
    if not value:
        return None
    try:
        return date.fromisoformat(value).weekday()
    except ValueError:
        return None


def _to_int_price(value: Any) -> int | None:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None
