from __future__ import annotations

from decimal import Decimal, ROUND_HALF_UP
import logging

from app.data.admin_profile import payment_prepayment_percent
from app.data.services import service_title
from app.dialog.state import BookingDraft
from app.dialog.pricing import calculate_booking_price
from app.integrations.yookassa import YooKassaClient


logger = logging.getLogger(__name__)


def payment_amounts(draft: BookingDraft) -> dict[str, Decimal]:
    """Return exact booking total, configured prepayment and remaining balance.

    YooKassa receives only the prepayment amount. If the project cannot calculate
    the full booking price from the profile-backed catalog, it is safer to stop
    payment creation than to charge a wrong amount.
    """
    total_price = calculate_booking_price(draft)
    if total_price is None:
        raise RuntimeError(f"Cannot calculate booking price for prepayment: {draft.to_dict()}")

    total = Decimal(str(total_price)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    percent = Decimal(str(payment_prepayment_percent()))
    prepayment = (total * percent / Decimal("100")).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    remaining = (total - prepayment).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    return {"total": total, "prepayment": prepayment, "remaining": remaining}


def create_prepayment(draft: BookingDraft, *, chat_id: str, booking_id: int) -> tuple[str, str]:
    amounts = payment_amounts(draft)
    amount = amounts["prepayment"]
    title = draft.service_variant or service_title(draft.service_type)
    description = f"Предоплата {payment_prepayment_percent()}% за {title}"
    logger.info(
        "Creating prepayment chat_id=%s booking_id=%s amount=%s total=%s remaining=%s service_type=%s variant=%s",
        chat_id,
        booking_id,
        amount,
        amounts["total"],
        amounts["remaining"],
        draft.service_type,
        draft.service_variant,
    )
    response = YooKassaClient().create_payment(
        amount=amount,
        description=description,
        metadata={
            "chat_id": chat_id,
            "booking_id": str(booking_id),
            "source": "admin_niz_mvp",
            "payment_kind": f"prepayment_{payment_prepayment_percent()}_percent",
            "total_price_rub": str(amounts["total"]),
            "prepayment_rub": str(amounts["prepayment"]),
            "remaining_rub": str(amounts["remaining"]),
            "service_type": str(draft.service_type or ""),
            "object_title": str(title or ""),
        },
        customer_phone=draft.phone,
    )
    payment_id = str(response.get("id") or "")
    url = ((response.get("confirmation") or {}).get("confirmation_url") or "")
    if not payment_id or not url:
        raise RuntimeError(f"YooKassa did not return payment link: {response}")
    logger.info("Prepayment created chat_id=%s booking_id=%s payment_id=%s", chat_id, booking_id, payment_id)
    return payment_id, url
