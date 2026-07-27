from __future__ import annotations

import json
import logging
import re
import threading
from datetime import datetime
from typing import Any

from app.ai.confirmation import is_positive_confirmation
from app.ai.parser import decide, classify_watchlist_turn
from app.ai.response_sanitizer import sanitize_reply
from app.core.dates import now_local
from app.core.config import get_settings
from app.data.admin_profile import load_admin_profile, media_key_for_booking, payment_prepayment_percent
from app.data.services import normalize_service_type, service_title, variant_by_title
from app.dialog.availability import check_availability
from app.dialog.payment import create_prepayment, payment_amounts
from app.dialog.admin_notify import notify_admin_booking_created, notify_admin_cancel_refund_required, notify_admin_booking_rescheduled
from app.dialog.state import BookingDraft, AdminDecision
from app.dialog.transition_validator import (
    contextual_gazebo_variant_from_text,
    is_bare_non_value_reply,
    validate_transition_patch,
)
from app.storage import sqlite
from app.dialog.watchlist import WatchlistCandidate, create_watchlist
from app.integrations.yclients import YClientsClient
from app.dialog.availability import build_yclients_payload
from app.dialog.availability_cache import refresh_availability_cache
from app.dialog.pricing import calculate_booking_price


logger = logging.getLogger(__name__)

def _core_booking_fields_collected(draft: BookingDraft) -> bool:
    return bool(
        draft.service_type
        and draft.date
        and draft.time
        and draft.duration
        and draft.guests_count
    )


def _decision_moves_to_contacts(decision: Any) -> bool:
    missing = getattr(decision, "missing_fields", None) or []
    if not isinstance(missing, list):
        return False

    missing_set = {str(x) for x in missing if x}
    if not missing_set:
        return False

    contact_fields = {"client_name", "phone"}
    return missing_set.issubset(contact_fields)


def _close_upsell_if_llm_moved_to_contacts(draft: BookingDraft, decision: Any) -> bool:
    """
    No text matching here.

    This is a state-machine reconciliation:
    if the LLM says the remaining missing fields are only contacts,
    then the upsell stage is semantically finished.
    """
    if not _core_booking_fields_collected(draft):
        return False

    if draft.upsell_done:
        return False

    if draft.upsell_items:
        draft.upsell_done = True
        return True

    if not _decision_moves_to_contacts(decision):
        return False

    draft.upsell_items = []
    draft.upsell_done = True
    try:
        draft.upsell_offer_count = max(int(draft.upsell_offer_count or 0), 2)
    except Exception:
        draft.upsell_offer_count = 2

    return True


def _is_confirm_or_payment_text(text: str) -> bool:
    low = (text or "").lower().replace("ё", "е").strip()
    return any(x in low for x in (
        "все верно",
        "всё верно",
        "все правильно",
        "всё правильно",
        "да верно",
        "да все верно",
        "да всё верно",
        "подтверждаю",
        "можно оплатить",
        "хочу оплатить",
        "оплатить",
        "перейти к оплате",
    ))


def _booking_ready_without_payment(draft: BookingDraft) -> bool:
    try:
        ready = bool(draft.ready_for_confirmation())
    except Exception:
        ready = bool(
            draft.service_type
            and draft.date
            and draft.time
            and draft.duration
            and draft.guests_count
            and draft.upsell_done
            and draft.client_name
            and draft.phone
        )

    has_payment = bool(getattr(draft, "payment_id", None) or getattr(draft, "payment_url", None))
    return ready and not has_payment


def _booking_reply_data(draft: BookingDraft) -> dict[str, Any]:
    """Structured booking facts for the answer model; contains no dialogue text."""
    return {
        "service_type": draft.service_type,
        "object_title": draft.service_variant or service_title(draft.service_type),
        "date": draft.date,
        "time": draft.time,
        "duration_hours": draft.duration,
        "guests_count": draft.guests_count,
        "event_format": draft.event_format,
        "upsell_items": list(draft.upsell_items or []),
        "client_name": draft.client_name,
        "phone": draft.phone,
        "price_rub": calculate_booking_price(draft),
        "next_step": draft.next_step(),
        "status": draft.status,
    }


def _is_side_question_turn(text: str, intent: str | None = None) -> bool:
    if str(intent or "") == "answer_question":
        return True
    normalized = (text or "").lower().replace("ё", "е")
    return "?" in normalized or any(marker in normalized for marker in (
        "можно ли", "можно вам", "напишите номер", "какой номер", "куда звон",
        "сколько стоит", "какая цена", "какой адрес", "где находит", "есть ли",
        "что входит", "как добраться", "кому позвон",
    ))


def _looks_like_general_upsell_refusal(text: str) -> bool:
    low = (text or "").lower().replace("ё", "е").strip()
    return low in {
        "нет", "не", "нет не нужно", "нет не надо", "не нужно", "не надо",
        "ничего не нужно", "ничего не надо", "без допов", "допы не нужны",
        "допов не нужно", "допов не надо"
    } or any(x in low for x in (
        "ничего из доп",
        "ничего не готов",
        "без дополнительных",
        "дополнительные не нужны",
    ))


def _has_booking_core_fields(draft: BookingDraft) -> bool:
    return bool(
        draft.service_type
        and draft.date
        and draft.time
        and draft.duration
        and draft.guests_count
    )


def _has_client_contacts(draft: BookingDraft) -> bool:
    return bool(draft.client_name and draft.phone)


def _sanitize_confirmed_before_payment_reply(
    reply: str,
    draft: BookingDraft,
    *,
    chat_id: str,
    user_text: str,
    history: list[dict[str, Any]],
    today: str,
) -> str:
    text = reply or ""
    low = text.lower().replace("ё", "е")

    dangerous = (
        "бронирование подтверждено",
        "бронь подтверждена",
        "бронь подтверждено",
        "заявка подтверждена",
        "ждем вас",
        "ждём вас",
        "все готово для бронирования",
        "всё готово для бронирования",
    )

    if not any(x in low for x in dangerous):
        return reply

    if getattr(draft, "payment_id", None) or getattr(draft, "payment_url", None):
        return reply

    if _has_booking_core_fields(draft) and _has_client_contacts(draft):
        return _llm_reply_from_engine_context(
            draft=draft,
            chat_id=chat_id,
            user_text=user_text,
            history=history,
            today=today,
            event="booking_ready_for_confirmation",
            data={
                "booking": _booking_reply_data(draft),
                "payment_required": True,
                "forbidden_claim": "booking_confirmed_before_payment",
            },
            fallback=None,
        )

    return reply


_CANONICAL_UPSELLS = {
    "coal": "уголь",
    "lighter": "розжиг",
    "grill": "решётки",
    "dishes": "посуда",
    "hookah": "кальян",
}

_UPSELL_PATTERNS = {
    "coal": ("уголь", "угля"),
    "lighter": ("розжиг",),
    "grill": ("решет", "решёт", "решетки", "решётки"),
    "dishes": ("посуд",),
    "hookah": ("кальян",),
}


def _upsell_keys_from_text(text: str) -> list[str]:
    low = (text or "").lower().replace("ё", "е")
    result: list[str] = []

    for key, patterns in _UPSELL_PATTERNS.items():
        for pattern in patterns:
            if pattern.replace("ё", "е") in low:
                if key not in result:
                    result.append(key)
                break

    return result


def _is_specific_upsell_refusal(text: str) -> bool:
    low = (text or "").lower().replace("ё", "е")

    refusal_markers = (
        "не надо",
        "не нужен",
        "не нужна",
        "не нужно",
        "без ",
        "убери",
        "уберите",
        "не добав",
    )

    has_refusal = any(marker in low for marker in refusal_markers)
    if not has_refusal:
        return False

    # Общий отказ от всех допов должен обрабатываться старой логикой.
    general_refusal = (
        "ничего не надо",
        "ничего не нужно",
        "ничего из доп",
        "без доп",
        "допы не",
        "допов не",
        "ничего не готов",
    )
    if any(marker in low for marker in general_refusal):
        return False

    return bool(_upsell_keys_from_text(low))


def _normalize_existing_upsells(items: list | None) -> list[str]:
    result: list[str] = []

    for item in items or []:
        text = str(item or "")
        keys = _upsell_keys_from_text(text)
        for key in keys:
            value = _CANONICAL_UPSELLS[key]
            if value not in result:
                result.append(value)

    return result


def _extract_recent_user_upsells(history: list[dict], current_text: str = "") -> list[str]:
    current_norm = (current_text or "").strip().lower().replace("ё", "е")

    for msg in reversed(history or []):
        role = str(msg.get("role") or msg.get("sender") or "").lower()
        text = str(msg.get("content") or msg.get("text") or "").strip()
        low = text.lower().replace("ё", "е")

        if not text:
            continue

        if current_norm and low == current_norm:
            continue

        if role and role not in {"user", "client", "customer", "human"}:
            continue

        # Не берём подсказки самого бота с полным списком допов.
        if "могу подготовить" in low or "что-нибудь добавить" in low:
            continue

        if _is_specific_upsell_refusal(text):
            continue

        keys = _upsell_keys_from_text(text)
        if keys:
            result: list[str] = []
            for key in keys:
                value = _CANONICAL_UPSELLS[key]
                if value not in result:
                    result.append(value)
            return result

    return []


def _format_human_list(items: list[str]) -> str:
    cleaned = [x for x in items if x]
    if not cleaned:
        return ""
    if len(cleaned) == 1:
        return cleaned[0]
    return ", ".join(cleaned[:-1]) + " и " + cleaned[-1]


def _handle_specific_upsell_refusal_before_llm(chat_id: str, draft: BookingDraft, history: list[dict], text: str) -> str | None:
    if not _is_specific_upsell_refusal(text):
        return None

    refused_keys = _upsell_keys_from_text(text)
    refused_names = [_CANONICAL_UPSELLS[key] for key in refused_keys if key in _CANONICAL_UPSELLS]

    existing = _normalize_existing_upsells(draft.upsell_items or [])
    if not existing:
        existing = _extract_recent_user_upsells(history, current_text=text)

    # Если до этого были выбраны допы, сохраняем их и убираем только конкретно отвергнутые.
    if existing:
        kept = [item for item in existing if item not in refused_names]

        draft.upsell_items = kept
        draft.upsell_done = True
        draft.upsell_offer_count = max(int(draft.upsell_offer_count or 0), 2)

        sqlite.save_draft(chat_id, draft.to_dict(), status="active", current_step=draft.next_step())

        return _llm_reply_from_engine_context(
            draft=draft,
            chat_id=chat_id,
            user_text=text,
            history=history,
            today=now_local().date().isoformat(),
            event="specific_upsell_items_removed",
            data={
                "removed_items": refused_names,
                "remaining_items": kept,
                "booking": _booking_reply_data(draft),
                "next_step": draft.next_step(),
            },
            fallback=None,
        )

    # Если бот не успел сохранить предыдущее сообщение, не сбрасываем всё.
    return _llm_reply_from_engine_context(
        draft=draft,
        chat_id=chat_id,
        user_text=text,
        history=history,
        today=now_local().date().isoformat(),
        event="specific_upsell_refusal_without_stable_previous_selection",
        data={
            "removed_items": refused_names,
            "preserve_unknown_previous_items": True,
            "booking": _booking_reply_data(draft),
            "next_step": draft.next_step(),
        },
        fallback=None,
    )


_ADDON_KEYWORDS = {
    "уголь": ("уголь",),
    "розжиг": ("розжиг",),
    "решётки": ("решет", "решёт"),
    "посуда": ("посуд",),
    "кальян": ("кальян",),
    "шампуры": ("шампур",),
}


def _addon_key_for_text(text: object) -> str | None:
    low = str(text or "").lower().replace("ё", "е")
    for key, variants in _ADDON_KEYWORDS.items():
        if any(v.replace("ё", "е") in low for v in variants):
            return key
    return None


def _normalize_addon_items(items: list | None) -> list[str]:
    result: list[str] = []
    for item in items or []:
        key = _addon_key_for_text(item)
        if key and key not in result:
            result.append(key)
    return result


def _specific_addon_refusal_keys(text: str) -> set[str]:
    low = (text or "").lower().replace("ё", "е").strip()

    general_refusal = (
        "ничего не",
        "ниче не",
        "без доп",
        "допы не",
        "допов не",
        "ничего из доп",
        "ничего не готов",
        "ничего не добав",
    )
    if any(x in low for x in general_refusal):
        return set()

    refusal_markers = (
        "не нужен",
        "не нужна",
        "не нужно",
        "не надо",
        "не добав",
        "убери",
        "уберите",
        "без ",
        "исключ",
    )

    if not any(marker in low for marker in refusal_markers):
        return set()

    refused: set[str] = set()
    for key, variants in _ADDON_KEYWORDS.items():
        if any(v.replace("ё", "е") in low for v in variants):
            refused.add(key)

    return refused


def _looks_like_specific_addon_refusal(text: str) -> bool:
    return bool(_specific_addon_refusal_keys(text))


def _extract_addon_items_from_text(text: str) -> list[str]:
    low = (text or "").lower().replace("ё", "е")
    refused = _specific_addon_refusal_keys(text)
    items: list[str] = []

    for key, variants in _ADDON_KEYWORDS.items():
        if key in refused:
            continue
        if any(v.replace("ё", "е") in low for v in variants):
            items.append(key)

    return items


def _extract_recent_stable_upsells_from_history(history: list[dict]) -> list[str]:
    for msg in reversed(history or []):
        role = str(msg.get("role") or msg.get("sender") or "").lower()
        text = str(msg.get("content") or msg.get("text") or "")

        if role and role not in {"user", "client", "customer"}:
            continue

        items = _extract_addon_items_from_text(text)
        if items:
            return items

    return []


def _format_addons_human(items: list[str]) -> str:
    cleaned = [str(x).strip() for x in items or [] if str(x).strip()]
    if not cleaned:
        return ""
    if len(cleaned) == 1:
        return cleaned[0]
    return ", ".join(cleaned[:-1]) + " и " + cleaned[-1]



def _remove_watchlist_offer_text(reply: str) -> str:
    return reply

def _watchlist_enabled() -> bool:
    try:
        return bool(get_settings().watchlist_enabled)
    except Exception:
        return False



def _recent_history(chat_id: str, draft: BookingDraft | None = None) -> list[dict[str, Any]]:
    return sqlite.list_recent_messages(
        chat_id,
        limit=get_settings().llm_history_limit,
        since=(draft.context_started_at if draft else None),
    )


def _refresh_availability_after_freeing_slot(reason: str) -> None:
    """Refresh availability cache in background after a slot may have become free.

    Watchlist checks use the availability cache. If we delete/reschedule a YClients
    record but keep the old cache until the next hourly/startup refresh, users who
    asked for notifications will only get them after bot restart or a long delay.
    """
    def _job() -> None:
        try:
            refresh_availability_cache(days=3, max_seconds=25, reason=reason)
        except Exception:
            logger.exception("Availability refresh after freeing slot failed reason=%s", reason)

    threading.Thread(target=_job, name=f"availability-refresh-{reason}", daemon=True).start()

_LAST_REQUESTED_MEDIA_BY_CHAT: dict[str, list[str]] = {}


def pop_requested_media(chat_id: str) -> list[str]:
    return _LAST_REQUESTED_MEDIA_BY_CHAT.pop(str(chat_id), [])


def _store_requested_media(chat_id: str, requested_media: list[str] | None) -> None:
    if requested_media:
        _LAST_REQUESTED_MEDIA_BY_CHAT[str(chat_id)] = [str(item) for item in requested_media if item]
    else:
        _LAST_REQUESTED_MEDIA_BY_CHAT.pop(str(chat_id), None)


def _booking_media_titles(draft: BookingDraft) -> list[str]:
    """Return one photo title for the object in the current booking draft."""
    media_key = media_key_for_booking(draft.service_type, draft.service_variant)
    return [media_key] if media_key else []


def _queue_booking_object_photo(chat_id: str, draft: BookingDraft) -> None:
    """Ask the platform adapter to send the booked object's photo after text."""
    titles = _booking_media_titles(draft)
    if titles:
        _store_requested_media(chat_id, titles)


_PAYMENT_CHECK_NOTICE = (
    "\n\nПосле оплаты, пожалуйста, сохраните чек об оплате и сообщение с подтверждением бронирования. "
    "При необходимости администратор может попросить показать их для уточнения брони."
)


def _payment_link_reply(
    payment_url: str,
    draft: BookingDraft | None = None,
) -> str:
    total = calculate_booking_price(draft) if draft is not None else None
    payment = (load_admin_profile().get("payment") or {})
    cancellation = (load_admin_profile().get("cancellation") or {})
    prepayment_percent = payment_prepayment_percent()
    refund_policy = str(
        cancellation.get("client_payment_link_text")
        or (
            "Условия возврата предоплаты:\n"
            "— при отмене не позднее чем за 7 дней до даты бронирования "
            "предоплата возвращается;\n"
            "— если до даты бронирования осталось меньше 7 дней, "
            "предоплата не возвращается.\n\n"
        )
    )
    if refund_policy and not refund_policy.endswith("\n\n"):
        refund_policy = refund_policy.rstrip() + "\n\n"
    link_lifetime_text = str(payment.get("link_lifetime_text") or "Ссылка действует ограниченное время, обычно около часа.")
    later_payment_text = str(payment.get("later_payment_text") or "Если ссылка истечёт — напишите «оплатить», я отправлю новую актуальную ссылку.")

    if total:
        total_int = int(total)
        prepay_int = int(round(total_int * prepayment_percent / 100))
        rest_int = total_int - prepay_int

        total_text = f"{total_int:,}".replace(",", " ")
        prepay_text = f"{prepay_int:,}".replace(",", " ")
        rest_text = f"{rest_int:,}".replace(",", " ")

        return (
            "Бронь подготовила ✅\n\n"
            f"Стоимость бронирования: {total_text} ₽\n"
            f"Предоплата {prepayment_percent}%: {prepay_text} ₽\n"
            f"Остаток при посещении: {rest_text} ₽\n\n"
            f"{refund_policy}"
            "Для подтверждения нужна предоплата.\n\n"
            "До оплаты выбранное время находится в предварительном резерве. "
            "Бронь подтверждается после внесения предоплаты.\n\n"
            f"{link_lifetime_text} "
            f"{later_payment_text}\n\n"
            "Ссылка для оплаты:\n"
            f"{payment_url}{_PAYMENT_CHECK_NOTICE}"
        )

    return (
        "Для подтверждения брони нужна предоплата.\n\n"
        f"{refund_policy}"
        "До оплаты выбранное время находится в предварительном резерве. "
        "Бронь подтверждается после внесения предоплаты.\n\n"
        f"{link_lifetime_text} "
        f"{later_payment_text}\n\n"
        "Ссылка для оплаты:\n"
        f"{payment_url}{_PAYMENT_CHECK_NOTICE}"
    )


def _format_money_for_client(value: Any) -> str:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return f"{value} ₽"
    if number.is_integer():
        return f"{int(number):,}".replace(",", " ") + " ₽"
    return f"{number:,.2f}".replace(",", " ").replace(".", ",") + " ₽"


_BATHHOUSE_CAPACITY = 15


def _is_payment_request(text: str) -> bool:
    lowered = text.lower().replace("ё", "е")
    return any(word in lowered for word in ("оплат", "предоплат", "ссылк", "платеж", "платёж")) and not any(word in lowered for word in ("не хочу", "не надо", "не буду", "отмена"))


def _looks_like_upsell_refusal(text: str) -> bool:
    lowered = (text or "").lower().replace("ё", "е").strip()
    # Важно: короткое "не" само по себе нельзя считать общим отказом во всём
    # диалоге, но этот helper используется только внутри контекста допов
    # (next_step == upsell_items / последний ответ бота был про допы). Поэтому
    # здесь "не" должно закрывать именно ответ на предложение угля/кальяна,
    # а не улетать в LLM как date_refinement.
    if re.sub(r"[\s.!?,…]+", "", lowered) in {"не", "нет", "неа", "no", "ne"}:
        return True
    refusal_markers = (
        "не надо", "не нужны", "не нужен", "без доп", "ничего не", "не готов", "не готовим",
        "нет спасибо", "нет, спасибо", "нет спасиб", "нет спа", "не хочу", "отказыва",
        "no", "ne", "неа", "нет"
    )
    return any(marker in lowered for marker in refusal_markers)


def _looks_like_upsell_final_refusal(text: str) -> bool:
    """Client explicitly asks not to hear another upsell offer."""
    lowered = (text or "").lower().replace("ё", "е")
    final_markers = (
        "не предлаг", "больше не предлаг", "не надо снова", "не нужно снова",
        "я же сказал", "я же говор", "не спрашивай", "без допов",
        "точно нет", "совсем нет", "ничего не нужно", "ничего не надо", "не готовим", "не готов"
    )
    return any(marker in lowered for marker in final_markers)


def _text_contains_time(text: str) -> bool:
    lowered = (text or "").lower().replace("ё", "е")
    if re.search(r"\b\d{1,2}[:.]\d{2}\b", lowered):
        return True
    if re.search(r"\b(?:в|к)\s*\d{1,2}(?:\s*(?:утра|дня|вечера|ночи))?\b", lowered):
        return True
    return False


def _extract_time_from_text(text: str) -> str | None:
    """Extract an explicitly typed arrival time from a short user message.

    This is not dialog logic and not customer-facing text: it prevents the LLM
    from erasing a concrete time like "в 16" and then hallucinating that the
    slot is busy without the deterministic availability check.
    """
    lowered = (text or "").lower().replace("ё", "е")
    match = re.search(r"\b(?:в|к)\s*(\d{1,2})(?:[:.](\d{2}))?(?:\s*(утра|дня|вечера|ночи))?\b", lowered)
    if not match:
        match = re.search(r"\b(\d{1,2})[:.](\d{2})\b", lowered)
    if not match:
        return None
    hour = int(match.group(1))
    minute = int(match.group(2) or 0)
    part = match.group(3) if match.lastindex and match.lastindex >= 3 else None
    if part == "вечера" and hour < 12:
        hour += 12
    elif part == "дня" and 1 <= hour <= 7:
        hour += 12
    elif part == "ночи" and hour == 12:
        hour = 0
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        return None
    return f"{hour:02d}:{minute:02d}"


def _apply_warm_gazebo_fixed_period(draft: BookingDraft, user_text: str) -> bool:
    """Normalize the fixed 14:00–12:00 warm-gazebo rental period.

    Returns True when the client explicitly supplied a different arrival time
    and needs a schedule correction instead of an unavailable-slot response.
    """
    if draft.service_type != "warm_gazebo" or not draft.date:
        return False
    explicit_time = _extract_time_from_text(user_text)
    if not explicit_time:
        return False

    draft.time = "14:00"
    draft.duration = 22
    draft.pending_action = {}
    draft.block_reason = None
    draft.blocked_until = None
    return explicit_time != "14:00"


def _reply_claims_unavailable(reply: str | None) -> bool:
    low = (reply or "").lower().replace("ё", "е")
    return any(marker in low for marker in (
        "занят", "недоступ", "нет свобод", "к сожалению", "время уже",
        "если освобод", "включить уведомление"
    ))


def _reply_has_partial_time_wording(reply: str | None) -> bool:
    low = (reply or "").lower().replace("ё", "е")
    return "часть времени" in low or "частично" in low




def _text_explicitly_refuses_extras(text: str) -> bool:
    low = (text or "").lower().replace("ё", "е")
    return (
        ("доп" in low or "уголь" in low or "розжиг" in low or "решет" in low or "решёт" in low or "посуд" in low or "кальян" in low)
        and _looks_like_upsell_refusal(low)
    )


def _repair_false_unavailable_reply_after_live_check(
    reply: str | None,
    draft: BookingDraft,
    *,
    chat_id: str,
    user_text: str,
    history: list[dict[str, Any]],
    today: str,
) -> str | None:
    """Drop LLM hallucinated unavailability after a real exact slot check.

    The model can parse all fields correctly and still write "slot is busy" in the
    final answer. Availability decisions must be engine-owned: if date+time+duration
    are present, a refusal is valid only after check_availability() says unavailable.
    """
    if not _reply_claims_unavailable(reply):
        return None
    if not (draft.service_type and draft.date and draft.time and draft.duration):
        return None

    try:
        availability = check_availability(draft, chat_id=chat_id)
    except Exception as exc:
        logger.warning("FALSE_UNAVAILABLE_RECHECK_FAILED chat_id=%s error=%s", chat_id, exc)
        return None

    if not availability.ok:
        # Real unavailability. Let the normal unavailable branch stand.
        return None

    logger.warning(
        "DROP_LLM_FALSE_UNAVAILABLE_AFTER_LIVE_CHECK chat_id=%s service_type=%s date=%s time=%s duration=%s reply=%r",
        chat_id, draft.service_type, draft.date, draft.time, draft.duration, reply,
    )
    draft.block_reason = None
    draft.blocked_until = None
    if availability.variants:
        draft.available_variants = availability.variants
    if (draft.pending_action or {}).get("type") == "watchlist_create":
        draft.pending_action = {}

    try:
        sqlite.upsert_hold(chat_id, draft.to_dict())
        logger.info(
            "SLOT_HOLD_UPSERTED_AFTER_FALSE_UNAVAILABLE chat_id=%s service_type=%s variant=%s date=%s time=%s duration=%s",
            chat_id, draft.service_type, draft.service_variant, draft.date, draft.time, draft.duration,
        )
    except Exception:
        logger.exception("Failed to upsert slot hold after false unavailable chat_id=%s", chat_id)

    return _llm_reply_from_engine_context(
        draft=draft,
        chat_id=chat_id,
        user_text=user_text,
        history=history,
        today=today,
        event="slot_available_continue_booking",
        data={
            "availability": "available",
            "object": draft.service_variant or service_title(draft.service_type),
            "date": draft.date,
            "time": draft.time,
            "duration": draft.duration,
            "guests": draft.guests_count,
            "event_format": draft.event_format,
            "next_step": draft.next_step(),
            "available_variants": availability.variants,
            "rule": "Слот проверен и свободен. Нельзя писать, что он занят или недоступен. Если конкретная беседка ещё не выбрана, перечисли все available_variants и попроси выбрать одну. Иначе продолжи с текущего next_step; если все данные собраны, попроси подтвердить бронь.",
        },
        fallback=_fallback_question(draft),
    )

def _text_contains_duration(text: str) -> bool:
    lowered = (text or "").lower().replace("ё", "е")
    if any(word in lowered for word in ("час", "ч.", "ч ", "на сутки", "сутки")):
        return True
    # Short replies like "на 8" during duration collection are valid.
    if re.search(r"\bна\s*\d{1,2}\b", lowered):
        return True
    return False


def _text_is_bare_duration_answer(text: str, before: BookingDraft, draft: BookingDraft) -> bool:
    """True for replies like "7" when the bot is collecting bath duration.

    We must not drop this as an implicit/default duration. A date like "18" is
    not treated as duration here unless a service and date are already selected
    and no explicit time/date wording is present.
    """
    lowered = (text or "").lower().replace("ё", "е").strip(" .,!?…")
    if not lowered:
        return False
    if _text_contains_time(lowered):
        return False
    if re.search(r"\b\d{1,2}\s+(?:январ|феврал|март|апрел|ма[йя]|июн|июл|август|сентябр|октябр|ноябр|декабр)", lowered):
        return False
    if not re.fullmatch(r"(?:на\s*)?\d{1,2}", lowered):
        return False
    try:
        value = int(re.search(r"\d{1,2}", lowered).group(0))
    except Exception:
        return False
    if value < 1 or value > 24:
        return False
    service_type = draft.service_type or before.service_type
    date_value = draft.date or before.date
    # If the client already chose an object/date and is before the time step, a
    # bare number is the duration, not a date. This covers "7" after the bot asks
    # "На сколько часов?".
    return bool(service_type == "bathhouse" and date_value and before.time is None)


def _looks_like_slot_explanation_request(text: str) -> bool:
    lowered = (text or "").lower().replace("ё", "е")
    markers = (
        "всмысле", "в смысле", "почему", "как так", "че", "что",
        "сколько", "на сколько", "до скольки", "кем забронир",
        "не понял", "не поняла", "мне откуда", "все верно", "всё верно",
        "подтвержда", "да", "верно"
    )
    return any(marker in lowered for marker in markers)
