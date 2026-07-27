from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from app.data.admin_profile import load_admin_profile, payment_prepayment_percent, service_config, service_requires_duration
from app.data.services import service_title, variant_by_title
from app.dialog.payment import payment_amounts
from app.dialog.state import BookingDraft


def build_post_payment_messages(
    draft: BookingDraft,
) -> list[str]:
    profile = _post_payment_config()
    confirmation = "\n\n".join(
        part.strip()
        for part in (
            str(profile.get("paid_received_text") or "Оплату получили."),
            str(profile.get("confirmed_text") or "Запись подтверждена."),
            _booking_line(draft),
            _price_lines(draft),
        )
        if part and part.strip()
    )

    messages = [confirmation]
    instruction = _arrival_instruction(draft)
    if instruction:
        messages.append(instruction)

    return messages


def build_post_payment_message(
    draft: BookingDraft,
) -> str:
    return "\n\n".join(build_post_payment_messages(draft))


def _booking_line(draft: BookingDraft) -> str:
    title = _object_title(draft)
    chunks: list[str] = [title]
    if draft.date:
        chunks.append(_format_date(draft.date))
    if draft.time:
        chunks.append(f"в {draft.time}")
    if draft.duration and service_requires_duration(draft.service_type):
        chunks.append(f"на {_format_duration(draft.duration)}")
    return "Ваша бронь: " + ", ".join(chunks) + "."


def _price_lines(draft: BookingDraft) -> str:
    percent = payment_prepayment_percent()
    try:
        amounts = payment_amounts(draft)
    except Exception:
        return f"Предоплата {percent}% получена. Оставшуюся сумму можно оплатить на месте."

    return (
        f"Стоимость бронирования: {_format_money(amounts['total'])}\n"
        f"Предоплата {percent}%: {_format_money(amounts['prepayment'])}\n"
        f"Остаток при посещении: {_format_money(amounts['remaining'])}"
    )


def _arrival_instruction(draft: BookingDraft) -> str:
    kind = _instruction_kind(draft)
    post_payment = _post_payment_config()
    instructions = post_payment.get("instructions") or {}
    config = instructions.get(kind) or instructions.get(post_payment.get("default_instruction_key") or "default") or {}

    parts: list[str] = []
    header = str(config.get("header") or "").strip()
    video_url = str(config.get("video_url") or "").strip()
    if header and video_url:
        parts.append(f"{header}\n{video_url}")
    elif header:
        parts.append(header)

    if config.get("include_common_info", True):
        common_text = _full_arrival_text()
        if common_text:
            parts.append(common_text)

    return "\n\n".join(part for part in parts if part)


def _full_arrival_text() -> str:
    common_info = _post_payment_config().get("common_info") or []
    return "\n\n".join(str(item).strip() for item in common_info if str(item).strip())


def _instruction_kind(draft: BookingDraft) -> str:
    if draft.service_variant:
        variant = variant_by_title(draft.service_type, draft.service_variant) or {}
        if variant.get("post_payment_instruction_key"):
            return str(variant.get("post_payment_instruction_key"))

    service = service_config(draft.service_type)
    if service.get("post_payment_instruction_key"):
        return str(service.get("post_payment_instruction_key"))

    return str(_post_payment_config().get("default_instruction_key") or "default")


def _object_title(draft: BookingDraft) -> str:
    if draft.service_type == "bathhouse":
        return "Баня с бассейном"
    if draft.service_variant:
        return str(draft.service_variant)
    if draft.service_type == "house":
        return "Гостевой дом"
    if draft.service_type == "warm_gazebo":
        return "Тёплая беседка"
    return service_title(draft.service_type)


def _post_payment_config() -> dict[str, object]:
    value = load_admin_profile().get("post_payment") or {}
    return value if isinstance(value, dict) else {}


def _format_date(value: str) -> str:
    month_names = {
        1: "января", 2: "февраля", 3: "марта", 4: "апреля",
        5: "мая", 6: "июня", 7: "июля", 8: "августа",
        9: "сентября", 10: "октября", 11: "ноября", 12: "декабря",
    }
    try:
        date_obj = datetime.fromisoformat(str(value)).date()
    except ValueError:
        return str(value)
    return f"{date_obj.day} {month_names.get(date_obj.month, '')}".strip()


def _format_duration(value: object) -> str:
    value_str = str(value).strip().lower()
    if "сут" in value_str:
        return "сутки"
    try:
        hours = float(value)
    except (TypeError, ValueError):
        return str(value)
    if hours.is_integer():
        hours_int = int(hours)
        if hours_int == 24:
            return "сутки"
        suffix = "часов"
        if hours_int % 10 == 1 and hours_int % 100 != 11:
            suffix = "час"
        elif hours_int % 10 in {2, 3, 4} and hours_int % 100 not in {12, 13, 14}:
            suffix = "часа"
        return f"{hours_int} {suffix}"
    return f"{hours:g} часа"


def _format_money(value: Decimal) -> str:
    if value == value.to_integral_value():
        return f"{int(value):,}".replace(",", " ") + " ₽"
    return f"{value:,.2f}".replace(",", " ").replace(".", ",") + " ₽"
