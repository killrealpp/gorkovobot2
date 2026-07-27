from __future__ import annotations

import json
import logging
from time import monotonic
from typing import Any

from app.dialog.availability import build_yclients_payload, check_availability
from app.dialog.availability_cache import refresh_availability_cache
from app.dialog.state import BookingDraft
from app.dialog.post_payment_message import build_post_payment_message, build_post_payment_messages
from app.dialog.admin_notify import (
    notify_admin_manual_review,
    notify_admin_payment_canceled,
    notify_admin_payment_received,
    notify_admin_yclients_error,
)
from app.integrations.yclients import YClientsClient
from app.integrations.yookassa import YooKassaClient
from app.storage import sqlite


logger = logging.getLogger(__name__)

_LAST_API_ERROR_NOTIFICATION: dict[str, float] = {}


def _notify_admin_api_error_throttled(
    *,
    key: str,
    title: str,
    chat_id: str,
    booking_id: int | str | None,
    error: Exception,
    draft: BookingDraft | None = None,
    throttle_seconds: int = 1800,
) -> None:
    """Send API error to admin, but do not spam every payment loop tick."""
    now = monotonic()
    last = _LAST_API_ERROR_NOTIFICATION.get(key)
    if last is not None and now - last < throttle_seconds:
        return
    _LAST_API_ERROR_NOTIFICATION[key] = now

    try:
        details = [
            title,
            f"chat_id: {chat_id}",
            f"booking_id: {booking_id}",
            f"Ошибка: {error}",
        ]
        if draft is not None:
            details.append(f"Заявка: {draft.to_dict()}")

        sqlite.enqueue_admin_notification(
            "\n".join(details),
            chat_id=str(chat_id or ""),
        )
    except Exception:
        logger.exception("Failed to enqueue API error notification")




def _save_chat_draft_if_same_payment(
    chat_id: str,
    draft: BookingDraft,
    *,
    status: str,
) -> None:
    current_payload = sqlite.load_draft(chat_id) or {}
    current_draft = BookingDraft.from_dict(current_payload)

    current_payment_id = str(
        current_draft.payment_id or ""
    )
    processed_payment_id = str(
        draft.payment_id or ""
    )

    if current_payment_id != processed_payment_id:
        logger.warning(
            "PAYMENT_CHAT_DRAFT_NOT_REPLACED "
            "chat_id=%s processed_payment_id=%s "
            "current_payment_id=%s processed_status=%s",
            chat_id,
            processed_payment_id,
            current_payment_id,
            status,
        )
        return

    sqlite.save_draft(
        chat_id,
        draft.to_dict(),
        status=status,
        current_step=draft.next_step(),
    )


def sync_paid_bookings(*, platform: str | None = None) -> list[dict[str, str]]:
    events: list[dict[str, str]] = []
    for row in sqlite.list_pending_payments(platform=platform):
        draft = BookingDraft.from_dict(json.loads(row["draft_json"]))
        row_status = str(row.get("status") or "")
        if not draft.payment_id:
            continue
        try:
            payment = YooKassaClient().get_payment(draft.payment_id)
        except Exception as exc:
            logger.exception("Failed to check YooKassa payment booking_id=%s", row["id"])
            _notify_admin_api_error_throttled(
                key=f"yookassa_check:{draft.payment_id}",
                title="Ошибка API при проверке оплаты YooKassa.",
                chat_id=str(row["chat_id"]),
                booking_id=row["id"],
                error=exc,
                draft=draft,
            )
            continue
        payment_succeeded = payment.get("status") == "succeeded" or bool(payment.get("paid"))

        # A customer may still open an old YooKassa redirect after changing the
        # object/date. Keep polling that superseded payment, but never create the
        # obsolete YClients booking if money arrives through it.
        if row_status == "payment_superseded":
            if payment_succeeded:
                draft.status = "superseded_payment_paid"
                sqlite.update_booking(int(row["id"]), draft.to_dict(), "superseded_payment_paid")
                sqlite.enqueue_admin_notification(
                    "Оплачен устаревший платёж после изменения бронирования.\n"
                    "Не создавайте запись по старым данным; проверьте возврат или зачёт в новую бронь.\n"
                    f"Booking ID: {row['id']}\n"
                    f"chat_id: {row['chat_id']}\n"
                    f"YooKassa payment_id: {draft.payment_id}\n"
                    f"Старая заявка: {draft.to_dict()}",
                    chat_id=str(row["chat_id"]),
                )
                events.append(
                    {
                        "chat_id": str(row["chat_id"]),
                        "message": (
                            "Вижу оплату по старой ссылке, которая относилась к данным до изменения брони. "
                            "Новую бронь по ней не подтверждаю. Передала администратору для проверки оплаты и возврата или зачёта."
                        ),
                    }
                )
                logger.warning(
                    "SUPERSEDED_PAYMENT_SUCCEEDED booking_id=%s payment_id=%s",
                    row["id"],
                    draft.payment_id,
                )
            elif payment.get("status") in {"canceled", "expired"}:
                draft.status = "payment_canceled"
                sqlite.update_booking(int(row["id"]), draft.to_dict(), "payment_canceled")
            continue

        if not payment_succeeded:
            if payment.get("status") in {"canceled", "expired"}:
                draft.status = "payment_canceled"
                sqlite.update_booking(int(row["id"]), draft.to_dict(), "payment_canceled")
                _save_chat_draft_if_same_payment(str(row["chat_id"]), draft, status="payment_canceled")
                notify_admin_payment_canceled(
                    chat_id=str(row["chat_id"]),
                    booking_id=int(row["id"]),
                    draft=draft,
                    status=str(payment.get("status") or "payment_canceled"),
                )
                logger.info("Payment is no longer pending booking_id=%s status=%s", row["id"], payment.get("status"))
            continue
        result = check_availability(draft, chat_id=str(row["chat_id"]))
        if not result.ok:
            draft.status = "paid_needs_manual_review"
            sqlite.update_booking(int(row["id"]), draft.to_dict(), "paid_needs_manual_review")
            _save_chat_draft_if_same_payment(str(row["chat_id"]), draft, status="paid_needs_manual_review")
            logger.warning("Paid booking unavailable booking_id=%s message=%s", row["id"], result.message)
            notify_admin_manual_review(
                chat_id=str(row["chat_id"]),
                booking_id=int(row["id"]),
                draft=draft,
                reason=result.message or "доступность не подтвердилась",
            )
            events.append(
                {
                    "chat_id": str(row["chat_id"]),
                    "message": "Оплату увидела, но место уже не подтверждается автоматически. Передала заявку на ручную проверку.",
                }
            )
            continue
        try:
            response = YClientsClient().create_book_record(build_yclients_payload(draft))
            logger.info("YCLIENTS create response shape=%s", _response_shape(response))
            draft.yclients_record_id = _extract_record_id(response)
            draft.status = "booked"
            sqlite.update_booking(int(row["id"]), draft.to_dict(), "booked")
            _save_chat_draft_if_same_payment(str(row["chat_id"]), draft, status="booked")
            sqlite.convert_hold(str(row["chat_id"]))
            notify_admin_payment_received(
                chat_id=str(row["chat_id"]),
                booking_id=int(row["id"]),
                draft=draft,
            )
            try:
                refresh_availability_cache(days=14, max_seconds=180, reason="booking_created")
            except Exception as exc:
                logger.exception("Failed to refresh availability cache after booking booking_id=%s", row["id"])
                _notify_admin_api_error_throttled(
                    key=f"yclients_refresh_after_booking:{row['id']}",
                    title="Ошибка API при обновлении доступности YClients после создания брони.",
                    chat_id=str(row["chat_id"]),
                    booking_id=row["id"],
                    error=exc,
                    draft=draft,
                )
            for customer_message in _post_payment_customer_messages(draft, platform=platform):
                events.append(
                    {
                        "chat_id": str(row["chat_id"]),
                        "message": customer_message,
                    }
                )
        except Exception:
            draft.status = "paid_yclients_error"
            sqlite.update_booking(int(row["id"]), draft.to_dict(), "paid_yclients_error")
            _save_chat_draft_if_same_payment(str(row["chat_id"]), draft, status="paid_yclients_error")
            logger.exception("Failed to create YCLIENTS record booking_id=%s", row["id"])
            notify_admin_yclients_error(
                chat_id=str(row["chat_id"]),
                booking_id=int(row["id"]),
                draft=draft,
            )
            events.append(
                {
                    "chat_id": str(row["chat_id"]),
                    "message": "Оплату увидела ✅\nНо автоматически подтвердить бронь не получилось. Передала администратору.",
                }
            )
    return events


def _post_payment_customer_messages(
    draft: BookingDraft,
    *,
    platform: str | None,
) -> list[str]:
    if platform == "max":
        return [build_post_payment_message(draft)]
    return build_post_payment_messages(draft)


def _extract_record_id(response: Any) -> str | None:
    record_keys = ("record_id", "visit_id", "id")
    if isinstance(response, dict):
        for key in record_keys:
            if response.get(key):
                return str(response[key])
        data = response.get("data")
        if isinstance(data, dict):
            for key in record_keys:
                if data.get(key):
                    return str(data[key])
            for value in data.values():
                found = _extract_record_id(value)
                if found:
                    return found
        if isinstance(data, list) and data:
            for item in data:
                found = _extract_record_id(item)
                if found:
                    return found
        for key in ("records", "visits", "appointments"):
            value = response.get(key)
            found = _extract_record_id(value)
            if found:
                return found
    if isinstance(response, list):
        for item in response:
            found = _extract_record_id(item)
            if found:
                return found
    return None


def _response_shape(response: Any) -> Any:
    if isinstance(response, dict):
        return {key: _response_shape(value) for key, value in response.items()}
    if isinstance(response, list):
        return [_response_shape(response[0])] if response else []
    return type(response).__name__
