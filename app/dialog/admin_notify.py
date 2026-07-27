from __future__ import annotations

from datetime import datetime

from app.data.services import service_title
from app.core.dates import now_local
from app.dialog.pricing import calculate_booking_price, extra_hour_price_breakdown
from app.dialog.state import BookingDraft
from app.storage import sqlite


def notify_admin_booking_created(
    *,
    chat_id: str,
    booking_id: int,
    draft: BookingDraft,
) -> None:
    sqlite.enqueue_admin_notification(
        _booking_message(
            title="Новая бронь",
            chat_id=chat_id,
            booking_id=booking_id,
            draft=draft,
            payment_status="ожидает оплаты",
        ),
        chat_id=chat_id,
    )


def notify_admin_payment_received(
    *,
    chat_id: str,
    booking_id: int,
    draft: BookingDraft,
) -> None:
    sqlite.enqueue_admin_notification(
        _booking_message(
            title="Оплата получена по брони",
            chat_id=chat_id,
            booking_id=booking_id,
            draft=draft,
            payment_status="оплачено",
        ),
        chat_id=chat_id,
    )


def notify_admin_payment_canceled(
    *,
    chat_id: str,
    booking_id: int,
    draft: BookingDraft,
    status: str,
) -> None:
    sqlite.enqueue_admin_notification(
        _booking_message(
            title="Оплата по брони не прошла / отменена",
            chat_id=chat_id,
            booking_id=booking_id,
            draft=draft,
            payment_status=status or "не оплачено",
        ),
        chat_id=chat_id,
    )


def notify_admin_manual_review(
    *,
    chat_id: str,
    booking_id: int,
    draft: BookingDraft,
    reason: str,
) -> None:
    sqlite.enqueue_admin_notification(
        _booking_message(
            title="Оплаченная заявка требует ручной проверки",
            chat_id=chat_id,
            booking_id=booking_id,
            draft=draft,
            payment_status="оплачено, нужна ручная проверка",
            extra_lines=[f"Причина: {reason}"],
        ),
        chat_id=chat_id,
    )


def notify_admin_yclients_error(
    *,
    chat_id: str,
    booking_id: int,
    draft: BookingDraft,
    error: str | None = None,
) -> None:
    extra_lines = []
    if error:
        extra_lines.append(f"Ошибка: {error}")
    sqlite.enqueue_admin_notification(
        _booking_message(
            title="Оплата прошла, но автоматическая запись в YClients не создалась",
            chat_id=chat_id,
            booking_id=booking_id,
            draft=draft,
            payment_status="оплачено, запись не создана автоматически",
            extra_lines=extra_lines,
        ),
        chat_id=chat_id,
    )


def _booking_message(
    *,
    title: str,
    chat_id: str,
    booking_id: int,
    draft: BookingDraft,
    payment_status: str,
    extra_lines: list[str] | None = None,
) -> str:
    price = calculate_booking_price(draft)

    lines = [
        title,
        "",
        f"Booking ID: {booking_id}",
        f"chat_id: {chat_id}",
        f"Статус оплаты: {payment_status}",
        "",
        f"Объект: {_object_title(draft)}",
        f"Дата: {draft.date or 'не указана'}",
        f"Время: {draft.time or 'не указано'}",
        f"Длительность: {_format_duration(draft.duration)}",
        f"Гостей: {draft.guests_count or 'не указано'}",
        f"Формат: {draft.event_format or 'не указан'}",
        f"Допы: {', '.join(draft.upsell_items) if draft.upsell_items else 'без допов'}",
        f"Имя: {draft.client_name or 'не указано'}",
        f"Телефон: {draft.phone or 'не указан'}",
    ]

    if price:
        lines.append(f"Стоимость: {price:,} ₽".replace(",", " "))
        breakdown = _price_breakdown(draft)
        if breakdown:
            lines.append(f"Расчёт стоимости: {breakdown}")
    if draft.payment_id:
        lines.append(f"YooKassa payment_id: {draft.payment_id}")
    if draft.yclients_record_id:
        lines.append(f"YClients record_id: {draft.yclients_record_id}")
    if extra_lines:
        lines.append("")
        lines.extend(extra_lines)

    return "\n".join(lines)


def _object_title(draft: BookingDraft) -> str:
    if draft.service_variant:
        return draft.service_variant
    if draft.service_type == "bathhouse":
        return "Баня с бассейном"
    if draft.service_type == "house":
        return "Гостевой дом"
    return service_title(draft.service_type)


def _format_duration(value: object) -> str:
    if value is None:
        return "не указана"
    try:
        number = float(value)
    except (TypeError, ValueError):
        return str(value)
    if number == 24:
        return "сутки"
    if number.is_integer():
        return f"{int(number)} ч"
    return f"{number:g} ч"



def _price_breakdown(draft: BookingDraft) -> str | None:
    breakdown = extra_hour_price_breakdown(draft)
    if not breakdown:
        return None
    base_hours = breakdown["base_duration_hours"]
    extra_hours = breakdown["extra_hours"]
    extra_price = breakdown["extra_hour_price"]
    base_price = breakdown["base_price"]
    total = breakdown["total_price"]
    return (
        f"{base_price:,} ₽ за {base_hours} часов + "
        f"{extra_hours} × {extra_price:,} ₽ = {total:,} ₽"
    ).replace(",", " ")


def notify_admin_cancel_refund_required(
    *,
    chat_id: str,
    booking_id: int,
    draft: BookingDraft,
    reason: str | None = None,
) -> None:
    days_until: int | None = None

    if draft.date:
        try:
            booking_date = datetime.fromisoformat(
                str(draft.date)
            ).date()
            days_until = (
                booking_date - now_local().date()
            ).days
        except ValueError:
            days_until = None

    if days_until is not None and days_until >= 7:
        title = "Отмена брони: требуется возврат предоплаты"
        payment_status = "предоплата подлежит возврату"
        policy_line = (
            "До бронирования не меньше 7 дней. "
            "Проверьте оплату и выполните возврат."
        )
    elif days_until is not None:
        title = "Отмена брони: предоплата невозвратная"
        payment_status = "возврат не требуется"
        policy_line = (
            "До бронирования меньше 7 дней. "
            "Предоплата по условиям бронирования "
            "не возвращается."
        )
    else:
        title = "Отмена брони: проверьте возврат"
        payment_status = "нужна ручная проверка"
        policy_line = (
            "Не удалось определить срок до даты бронирования."
        )

    extra_lines = [
        "Клиент подтвердил отмену брони.",
        policy_line,
    ]

    if reason:
        extra_lines.append(
            f"Причина/комментарий: {reason}"
        )

    sqlite.enqueue_admin_notification(
        _booking_message(
            title=title,
            chat_id=chat_id,
            booking_id=booking_id,
            draft=draft,
            payment_status=payment_status,
            extra_lines=extra_lines,
        ),
        chat_id=chat_id,
    )



def notify_admin_booking_rescheduled(
    *,
    chat_id: str,
    booking_id: int,
    draft: BookingDraft,
    old_date: str | None,
    old_time: str | None,
) -> None:
    sqlite.enqueue_admin_notification(
        _booking_message(
            title="Бронь перенесена",
            chat_id=chat_id,
            booking_id=booking_id,
            draft=draft,
            payment_status="проверьте статус оплаты по брони",
            extra_lines=[f"Было: {old_date or 'не указано'} {old_time or ''}".strip()],
        ),
        chat_id=chat_id,
    )
