from __future__ import annotations

import json
import logging
import re
import threading
from datetime import datetime
from typing import Any

from app.ai.confirmation import is_positive_confirmation
from app.ai.parser import decide, classify_watchlist_turn, load_engine_response_prompt
from app.ai.response_sanitizer import sanitize_reply
from app.core.dates import now_local
from app.core.config import get_settings
from app.data.admin_profile import media_key_for_booking, payment_prepayment_percent, service_config
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
    reply = _llm_reply_from_engine_context(
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
    return reply


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


def _payment_link_reply(
    payment_url: str,
    draft: BookingDraft,
    *,
    chat_id: str,
    user_text: str = "",
    history: list[dict[str, Any]] | None = None,
    today: str | None = None,
) -> str:
    total = calculate_booking_price(draft)
    total_int = int(total) if total else None
    prepayment_percent = payment_prepayment_percent()
    prepay_int = int(round(total_int * prepayment_percent / 100)) if total_int is not None else None
    reply = _llm_reply_from_engine_context(
        draft=draft,
        chat_id=chat_id,
        user_text=user_text,
        history=history or _recent_history(chat_id, draft),
        today=today or now_local().date().isoformat(),
        event="payment_link_created",
        data={
            "booking": _booking_reply_data(draft),
            "payment_url": payment_url,
            "total_price_rub": total_int,
            "prepayment_percent": prepayment_percent,
            "prepayment_rub": prepay_int,
            "remaining_rub": total_int - prepay_int if total_int is not None and prepay_int is not None else None,
            "refund_full_if_days_before_at_least": 7,
            "payment_link_ttl_minutes_approx": 60,
            "payment_status": "waiting_payment",
            "rules": [
                "include_payment_url_verbatim",
                "include_price_breakdown_if_present",
                "include_refund_policy",
                "ask_customer_to_keep_receipt_and_bot_confirmation",
                "do_not_claim_payment_succeeded",
            ],
        },
        fallback=None,
    )
    return reply if payment_url in reply else "\n\n".join(part for part in (reply, payment_url) if part)


def _format_money_for_client(value: Any) -> str:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return f"{value} ₽"
    if number.is_integer():
        return f"{int(number):,}".replace(",", " ") + " ₽"
    return f"{number:,.2f}".replace(",", " ").replace(".", ",") + " ₽"


_BATHHOUSE_CAPACITY = 15


def _service_capacity_max(service_type: str | None) -> int | None:
    try:
        value = service_config(service_type).get("capacity_max")
        return int(value) if value else None
    except (TypeError, ValueError):
        return None


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


def _looks_like_upsell_deferred_or_declined(text: str) -> bool:
    lowered = (text or "").lower().replace("ё", "е")
    return any(marker in lowered for marker in (
        "уточним у администратор", "спросим у администратор",
        "при необходимости", "если понадобится", "если будет нужно",
        "на месте решим", "потом решим", "если что закажем",
    ))


def _text_contains_time(text: str) -> bool:
    lowered = (text or "").lower().replace("ё", "е")
    if re.search(r"\b\d{1,2}[:.]\d{2}\b", lowered):
        return True
    if re.search(r"\b(?:в|к|с)\s*\d{1,2}(?:\s*(?:утра|дня|вечера|ночи))?\b", lowered):
        return True
    return False


def _extract_time_from_text(text: str) -> str | None:
    """Extract an explicitly typed arrival time from a short user message.

    This is not dialog logic and not customer-facing text: it prevents the LLM
    from erasing a concrete time like "в 16" and then hallucinating that the
    slot is busy without the deterministic availability check.
    """
    lowered = (text or "").lower().replace("ё", "е")
    match = re.search(r"\b(?:в|к|с)\s*(\d{1,2})(?:[:.](\d{2}))?(?:\s*(утра|дня|вечера|ночи))?\b", lowered)
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
            "rules": ["slot_is_available", "list_available_variants_if_object_not_selected", "continue_from_next_step"],
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


def _last_bot_text(history: list[dict[str, Any]] | None) -> str:
    for item in reversed(history or []):
        sender = str(item.get("sender") or "").lower()
        if sender in {"user", "client"}:
            continue
        text = str(item.get("text") or "").strip()
        if text:
            return text
    return ""


def _last_bot_offered_watchlist(history: list[dict[str, Any]] | None) -> bool:
    text = _last_bot_text(history).lower().replace("ё", "е")
    return bool(text and ("уведом" in text or "если освобод" in text or "сообщ" in text and "освобод" in text))


def _last_bot_confirmed_watchlist(history: list[dict[str, Any]] | None) -> bool:
    text = _last_bot_text(history).lower().replace("ё", "е")
    if not text:
        return False
    return (
        "уведом" in text
        and ("включ" in text or "сообщ" in text)
        and ("если" in text and "освобод" in text)
    )


def _watchlist_created_reply(
    candidate: WatchlistCandidate,
    *,
    already: bool = False,
) -> str:
    return ""

def _infer_service_type_from_text(text: str) -> str | None:
    lowered = (text or "").lower().replace("ё", "е")
    if "бан" in lowered:
        return "bathhouse"
    if "тепл" in lowered or "тёпл" in lowered:
        return "warm_gazebo"
    if "дом" in lowered or "гост" in lowered:
        return "house"
    if "бесед" in lowered or "крыт" in lowered:
        return "gazebo"
    return None


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


def _infer_watchlist_candidate_from_context(text: str, draft: BookingDraft, history: list[dict[str, Any]] | None) -> WatchlistCandidate | None:
    candidate = _make_watchlist_candidate_from_draft(draft)
    if candidate:
        return candidate

    pieces: list[str] = [text or ""]
    for item in reversed(history or []):
        pieces.append(str(item.get("text") or ""))
        if len(pieces) >= 6:
            break
    combined = "\n".join(pieces)
    lowered = combined.lower().replace("ё", "е")

    date = draft.date or _extract_explicit_day_date(combined)
    service_type = draft.service_type or _infer_service_type_from_text(combined)

    title = draft.service_variant or ""
    gazebo_match = re.search(r"\b(?:беседк[аи]?|беседку|беседка)\s*№?\s*(\d{1,2})\b", lowered)
    if gazebo_match:
        service_type = "gazebo"
        title = f"Беседка №{int(gazebo_match.group(1))}"
    elif "крыт" in lowered and "бесед" in lowered:
        service_type = "gazebo"
        title = "Крытая беседка"
    elif service_type and not title:
        title = _OBJECT_TITLE_BY_SERVICE.get(service_type) or service_title(service_type)

    if not date or not title:
        return None
    return WatchlistCandidate(
        service_type=service_type,
        object_title=str(title),
        date=str(date),
        time=draft.time,
        duration=draft.duration,
    )



def _candidate_dict(candidate: WatchlistCandidate | None) -> dict[str, Any] | None:
    if not candidate:
        return None
    return {
        "service_type": candidate.service_type,
        "object_title": candidate.object_title,
        "date": candidate.date,
        "time": candidate.time,
        "duration": candidate.duration,
    }


def _candidate_from_pending_action(draft: BookingDraft) -> WatchlistCandidate | None:
    pending = draft.pending_action or {}
    if pending.get("type") != "watchlist_create":
        return None
    raw = pending.get("candidate") or {}
    if not isinstance(raw, dict):
        return None
    if not raw.get("date") or not raw.get("object_title"):
        return None
    return WatchlistCandidate(
        service_type=raw.get("service_type") or draft.service_type,
        object_title=str(raw.get("object_title") or ""),
        date=str(raw.get("date") or ""),
        time=(str(raw.get("time") or "").strip() or None),
        duration=raw.get("duration") or draft.duration,
    )


def _cancel_active_watchlist_for_candidate(chat_id: str, candidate: WatchlistCandidate | None) -> int:
    if not candidate:
        return 0
    normalized_title = (candidate.object_title or "").strip().lower().replace("ё", "е")
    canceled = 0
    for row in sqlite.list_active_watchlist(limit=500):
        if str(row.get("chat_id") or "") != str(chat_id):
            continue
        row_title = str(row.get("object_title") or "").strip().lower().replace("ё", "е")
        if (
            str(row.get("date") or "") == str(candidate.date or "")
            and row_title == normalized_title
            and str(row.get("service_type") or "") == str(candidate.service_type or "")
        ):
            sqlite.cancel_watchlist(int(row["id"]))
            canceled += 1
            logger.info("WATCHLIST_CANCELED_BY_CLIENT id=%s chat_id=%s", row.get("id"), chat_id)
    return canceled


def _watchlist_llm_context_is_active(
    draft: BookingDraft,
    history: list[dict[str, Any]] | None,
) -> bool:
    return (draft.pending_action or {}).get("type") == "watchlist_create"


def _is_bare_affirmation(text: str) -> bool:
    normalized = re.sub(
        r"[^a-zа-я0-9]+",
        " ",
        (text or "").lower().replace("ё", "е"),
    ).strip()
    return normalized in {"да", "ага", "угу", "ок", "окей", "хорошо"}


def _reset_booking_after_watchlist_created(
    draft: BookingDraft,
    candidate: WatchlistCandidate,
) -> None:
    """Close the rejected booking path while keeping the selected object handy."""
    fresh = BookingDraft(
        service_type=candidate.service_type,
        service_variant=(candidate.object_title if candidate.service_type == "gazebo" else None),
        status="active",
        pending_action={
            "type": "watchlist_active",
            "candidate": candidate.__dict__,
        },
        context_started_at=draft.context_started_at,
    )
    draft.__dict__.update(fresh.__dict__)


def _handle_active_watchlist_followup(
    text: str,
    draft: BookingDraft,
    *,
    chat_id: str,
    history: list[dict[str, Any]],
    today: str,
) -> str | None:
    pending = draft.pending_action or {}
    if pending.get("type") != "watchlist_active":
        return None

    raw = pending.get("candidate") or {}
    if _is_bare_affirmation(text):
        return _llm_reply_from_engine_context(
            draft=draft, chat_id=chat_id, user_text=text, history=history, today=today,
            event="watchlist_already_active", data={"candidate": raw}, fallback=None,
        )

    if _text_contains_time(text) and not _extract_explicit_day_date(text):
        # An exact-time watch may continue with another time on the same date.
        # A date-level watch means the whole date is unavailable, so a bare time
        # must never restart booking against that rejected date.
        if raw.get("time"):
            draft.date = str(raw.get("date") or "") or None
            draft.pending_action = {}
            return None
        return _llm_reply_from_engine_context(
            draft=draft, chat_id=chat_id, user_text=text, history=history, today=today,
            event="watchlist_active_requires_different_date",
            data={"candidate": raw, "booking": _booking_reply_data(draft)}, fallback=None,
        )

    # A meaningful new request starts a fresh booking path for the same object.
    draft.pending_action = {}
    return None

def _handle_watchlist_llm_turn(
    *,
    chat_id: str,
    text: str,
    draft: BookingDraft,
    history: list[dict[str, Any]],
    today: str,
    platform: str | None = None,
) -> str | None:
    """Let a small LLM decide accept/decline/ignore for notification offers.

    The engine owns the side effects (create/cancel DB rows) and safe state
    transitions. The LLM classifies the user's relation to the notification offer;
    replies that could advance an unavailable booking stay deterministic.
    """
    if not _watchlist_llm_context_is_active(draft, history):
        return None

    # Кандидат берётся только из сохранённого состояния.
    # Не восстанавливаем объект, дату или время из фраз переписки.
    candidate = _candidate_from_pending_action(draft)
    result = classify_watchlist_turn(
        user_text=text,
        last_bot_text=_last_bot_text(history),
        current_draft=draft.to_dict(),
        pending_candidate=_candidate_dict(candidate),
        recent_dialog=history[-6:] if history else [],
        today=today,
    )
    decision = str(result.get("decision") or "ignore")
    logger.info(
        "WATCHLIST_LLM_DECISION decision=%s confidence=%s reason=%s text=%r",
        decision,
        result.get("confidence"),
        result.get("reason"),
        text,
    )

    if decision == "accept":
        if not candidate:
            logger.warning("WATCHLIST_LLM_ACCEPT_NO_CANDIDATE chat_id=%s text=%r draft=%s", chat_id, text, _draft_log_line(draft))
            return None
        watch_id = create_watchlist(chat_id, candidate, platform=platform)
        _reset_booking_after_watchlist_created(draft, candidate)
        sqlite.save_draft(chat_id, draft.to_dict(), status="active", current_step=draft.next_step())
        logger.info(
            "WATCHLIST_CREATED_LLM id=%s chat_id=%s service_type=%s object_title=%s date=%s",
            watch_id,
            chat_id,
            candidate.service_type,
            candidate.object_title,
            candidate.date,
        )
        return sanitize_reply(_llm_reply_from_engine_context(
            draft=draft,
            chat_id=chat_id,
            user_text=text,
            history=history,
            today=today,
            event="watchlist_created",
            data={
                "watch_id": watch_id,
                "service_type": candidate.service_type,
                "object_title": candidate.object_title,
                "date": candidate.date,
                "time": candidate.time,
                "duration": candidate.duration,
            },
            fallback=str(result.get("customer_reply") or "").strip() or None,
        ))

    if decision == "decline":
        canceled = _cancel_active_watchlist_for_candidate(chat_id, candidate)
        draft.pending_action = {}

        # Declining a notification does not make the rejected booking option
        # available. Keep an exact rejected interval blocked and return to time
        # selection. If the whole date was rejected, return to date selection.
        if candidate and candidate.time:
            draft.service_type = candidate.service_type or draft.service_type
            draft.date = candidate.date or draft.date
            draft.time = None
            draft.duration = candidate.duration or draft.duration
            draft.block_reason = "slot_unavailable"
            draft.blocked_until = candidate.time
        else:
            if candidate and str(draft.date or "") == str(candidate.date or ""):
                draft.date = None
            draft.time = None
            draft.block_reason = None
            draft.blocked_until = None

        sqlite.save_draft(chat_id, draft.to_dict(), status="active", current_step=draft.next_step())
        logger.info(
            "WATCHLIST_DECLINED_LLM chat_id=%s canceled=%s next_step=%s blocked_until=%s",
            chat_id,
            canceled,
            draft.next_step(),
            draft.blocked_until,
        )
        return sanitize_reply(_llm_reply_from_engine_context(
            draft=draft,
            chat_id=chat_id,
            user_text=text,
            history=history,
            today=today,
            event="watchlist_declined_continue_booking",
            data={
                "candidate": _candidate_dict(candidate),
                "canceled_watchlist_count": canceled,
                "booking": _booking_reply_data(draft),
                "next_step": draft.next_step(),
            },
            fallback=None,
        ))

    # The user chose another path (for example another time or a question about
    # price/discounts). Drop the pending notification action and let the normal
    # booking/LLM flow handle the message.
    if (draft.pending_action or {}).get("type") == "watchlist_create":
        logger.info("WATCHLIST_LLM_IGNORED_CLEAR_PENDING text=%r", text)
        draft.pending_action = {}
        sqlite.save_draft(chat_id, draft.to_dict(), status="active", current_step=draft.next_step())
    return None

def _looks_like_new_time_choice(text: str, draft: BookingDraft | None = None) -> bool:
    lowered = (text or "").lower().replace("ё", "е")
    if _text_contains_time(text):
        return True
    if re.search(r"\bс\s*\d{1,2}\s*(?:до|-|—)\s*\d{1,2}\b", lowered):
        return True
    # During time collection, short phrases like «с 14 до 22», «тогда в 14»
    # should always be treated as a new time, not as agreement to a watchlist.
    if draft and draft.next_step() == "time" and re.search(r"\b(?:в|к|с|на|тогда|давайте)\s*\d{1,2}\b", lowered):
        return True
    return False


def _client_mentions_selected_service(text: str, draft: BookingDraft) -> bool:
    lowered = (text or "").lower().replace("ё", "е")
    if draft.service_type == "bathhouse":
        return "бан" in lowered
    if draft.service_type == "house":
        return "дом" in lowered or "гост" in lowered
    if draft.service_type == "warm_gazebo":
        return "тепл" in lowered or "тёпл" in lowered
    if draft.service_type == "gazebo":
        return "бесед" in lowered
    return False


def _extract_explicit_day_date(text: str) -> str | None:
    """Resolve phrases like «на 16», «на 16 июня», «16 июня» as a date.

    This is deliberately narrow: it is used to protect booking flows from the LLM
    reading «на 16» as 16:00 when the client is actually correcting the date.
    """
    lowered = (text or "").lower().replace("ё", "е")
    if not lowered.strip():
        return None
    # Do not reinterpret explicit times/durations as dates.
    if any(marker in lowered for marker in ("час", "ч.", "вечера", "утра", "дня", "ночи", ":")):
        return None
    m = re.search(r"(?:\bна\s+|\b)(\d{1,2})\s*(января|февраля|марта|апреля|мая|июня|июля|августа|сентября|октября|ноября|декабря)?\b", lowered)
    if not m:
        return None
    day = int(m.group(1))
    if day < 1 or day > 31:
        return None
    today = now_local().date()
    month = _MONTHS_RU.get(m.group(2) or "") or today.month
    year = today.year
    try:
        candidate = datetime(year, month, day).date()
    except ValueError:
        return None
    if not m.group(2) and candidate < today:
        # Bare day in the past means next month.
        month += 1
        if month > 12:
            month = 1
            year += 1
        try:
            candidate = datetime(year, month, day).date()
        except ValueError:
            return None
    return candidate.isoformat()


def _looks_like_date_change_for_current_service(text: str, draft: BookingDraft) -> bool:
    lowered = (text or "").lower().replace("ё", "е")
    if not draft.service_type:
        return False
    if _extract_explicit_day_date(text):
        # «нет давайте на 16» after a bot asks for duration is commonly a date correction.
        if any(marker in lowered for marker in ("нет", "давайте", "лучше", "всмысле", "имею", "дат", "июн", "июл")):
            return True
    return False


def _derive_time_duration_from_range(text: str) -> tuple[str | None, int | None]:
    lowered = (text or "").lower().replace("ё", "е")
    m = re.search(
        r"\bс\s*(\d{1,2})(?:[:.](\d{2}))?\s*(?:до|-|—)\s*"
        r"(\d{1,2})(?:[:.](\d{2}))?\b",
        lowered,
    )
    if not m:
        return None, None
    start_h = int(m.group(1))
    start_m = int(m.group(2) or 0)
    end_h = int(m.group(3))
    end_m = int(m.group(4) or 0)
    if not (0 <= start_h <= 23 and 0 <= start_m <= 59 and 0 <= end_h <= 23 and 0 <= end_m <= 59):
        return None, None
    start_minutes = start_h * 60 + start_m
    end_minutes = end_h * 60 + end_m
    if end_minutes <= start_minutes:
        end_minutes += 24 * 60
    duration_hours = (end_minutes - start_minutes) / 60
    if duration_hours <= 0 or duration_hours > 24:
        return None, None
    duration = int(duration_hours) if float(duration_hours).is_integer() else None
    return f"{start_h:02d}:{start_m:02d}", duration


def _derive_duration_from_end_time(text: str, start_time: str | None) -> int | None:
    if not start_time:
        return None
    match = re.search(r"\bдо\s*(\d{1,2})(?:[:.](\d{2}))?\b", (text or "").lower())
    if not match:
        return None
    try:
        start_hour, start_minute = [int(part) for part in str(start_time)[:5].split(":")]
        end_hour = int(match.group(1))
        end_minute = int(match.group(2) or 0)
    except (TypeError, ValueError):
        return None
    if not (0 <= end_hour <= 23 and 0 <= end_minute <= 59):
        return None
    start_total = start_hour * 60 + start_minute
    end_total = end_hour * 60 + end_minute
    if end_total <= start_total:
        end_total += 24 * 60
    minutes = end_total - start_total
    return minutes // 60 if minutes > 0 and minutes % 60 == 0 else None


def _explicitly_asks_for_photo(text: str) -> bool:
    lowered = (text or "").lower().replace("ё", "е")
    return any(marker in lowered for marker in ("фото", "фотк", "покажи", "как выглядит", "выгляд", "посмотреть"))


def _gazebo_titles_from_text(text: str) -> list[str]:
    """Extract gazebo titles from model text, including compact lists.

    The model often writes "Беседка №1, №2, №3". Only the first number has the
    word "Беседка", but all numbers are media commands from the same sentence.
    """
    result: list[str] = []
    normalized = (text or "").replace("ё", "е")

    def add(number: str) -> None:
        title = f"Беседка №{int(number)}"
        if title not in result:
            result.append(title)

    for sentence in re.split(r"[.!?\n;]+", normalized):
        low = sentence.lower()
        if "бесед" not in low:
            continue
        if any(marker in low for marker in ("занят", "недоступ", "нет свобод", "уже есть брон")):
            continue

        for number in re.findall(r"(?:№|#|номер)?\s*(1|2|3|4|5|6|8)\b", low):
            add(number)

    return result


def _media_titles_from_recent_context(draft: BookingDraft, history: list[dict[str, Any]] | None) -> list[str]:
    """Find the object(s) the client is most likely asking photos for.

    This is not phrase scripting. It is a state/context resolver for explicit photo requests:
    the user asks for photos, the engine takes the last selected or offered object and sends media.
    """
    titles: list[str] = []
    known_gazebo_titles = [
        "Беседка №1",
        "Беседка №2",
        "Беседка №3",
        "Беседка №4",
        "Беседка №5",
        "Беседка №6",
        "Беседка №8",
        "Крытая беседка",
        "Тёплая беседка",
        "Теплая беседка",
    ]

    def add(title: str | None) -> None:
        if not title:
            return
        value = str(title).strip()
        if value == "Теплая беседка":
            value = "Тёплая беседка"
        if value and value not in titles:
            titles.append(value)

    def collect_from_text(value: str) -> None:
        import re

        text = (value or "").replace("ё", "е")
        for raw_sentence in re.split(r"[.!?\n]+", text):
            sentence = raw_sentence.strip()
            low = sentence.lower()
            if not sentence:
                continue

            # Do not send a photo for the object that was explicitly described as unavailable.
            if any(marker in low for marker in ("занят", "недоступ", "нет свобод", "уже есть брон")):
                continue

            for title in _gazebo_titles_from_text(sentence):
                add(title)

            if "крыт" in low and "бесед" in low:
                add("Крытая беседка")
            if "тепл" in low and "бесед" in low:
                add("Тёплая беседка")
            if "бан" in low or "бассейн" in low:
                add("bathhouse")
            if "гостев" in low or "гостевой дом" in low:
                add("house")

    # First use the last assistant messages: these contain the last offered variants.
    for item in reversed(history or []):
        sender = str(item.get("sender") or item.get("role") or "").lower()
        if sender not in {"assistant", "bot"}:
            continue
        collect_from_text(str(item.get("text") or item.get("content") or ""))
        if titles:
            return titles[:10]

    # Then fallback to the current draft.
    if draft.service_variant:
        add(draft.service_variant)
    elif draft.service_type == "bathhouse":
        add("bathhouse")
    elif draft.service_type == "house":
        add("house")
    elif draft.service_type == "warm_gazebo":
        add("Тёплая беседка")

    return titles[:10]


def _is_broad_availability_reply(reply: str, draft: BookingDraft) -> bool:
    lowered = (reply or "").lower().replace("ё", "е")
    object_markers = ("баня", "беседк", "гостевой", "дом", "теплая", "тёплая", "крытая")
    mentioned = sum(1 for marker in object_markers if marker in lowered)
    list_markers = ("доступны", "свободны", "следующие варианты", "на сегодня", "на завтра")
    return mentioned >= 3 and any(marker in lowered for marker in list_markers)


_GAZEBO_RECOMMENDATION_TITLES = (
    "Беседка №1", "Беседка №2", "Беседка №3", "Беседка №4",
    "Беседка №5", "Беседка №6", "Беседка №8", "Крытая беседка",
)
_GAZEBO_COMFORT_PRIORITY = (
    "Крытая беседка", "Беседка №1", "Беседка №3", "Беседка №8",
    "Беседка №2", "Беседка №4", "Беседка №6", "Беседка №5",
)


def _gazebo_titles_mentioned_anywhere(text: str) -> list[str]:
    normalized = (text or "").lower().replace("ё", "е")
    return [title for title in _GAZEBO_RECOMMENDATION_TITLES if title.lower().replace("ё", "е") in normalized]


def _looks_like_gazebo_recommendation_request(text: str) -> bool:
    lowered = (text or "").lower().replace("ё", "е")
    return any(marker in lowered for marker in (
        "комфорт", "какая лучше", "какой лучше", "посовет", "рекоменд",
        "поудобнее", "удобнее", "с розет", "со свет", "от дожд", "от вет",
    ))


def _available_gazebos_for_recommendation(draft: BookingDraft) -> list[str]:
    if not draft.date:
        return []
    try:
        rows = sqlite.list_availability_rows(service_type="gazebo", date=draft.date, limit=100)
    except Exception:
        logger.exception("GAZEBO_RECOMMENDATION_CACHE_READ_FAILED date=%s", draft.date)
        return []

    available = {
        str(row.get("title") or "").strip()
        for row in rows
        if str(row.get("status") or "") == "free" and row.get("time")
    }
    result: list[str] = []
    for title in _GAZEBO_COMFORT_PRIORITY:
        if title not in available:
            continue
        variant = variant_by_title("gazebo", title) or {}
        capacity = variant.get("capacity_max")
        if draft.guests_count and capacity and draft.guests_count > int(capacity):
            continue
        result.append(title)
    return result


def _guard_gazebo_recommendation_against_cache(
    reply: str,
    draft: BookingDraft,
    *,
    user_text: str,
    chat_id: str,
    history: list[dict[str, Any]],
    today: str,
) -> str:
    """Never recommend a gazebo that is unavailable on the discussed date.

    The knowledge base may describe every gazebo, including occupied ones. This
    final deterministic guard intersects a model recommendation with the exact-date
    availability cache (and known capacity) before the customer sees it.
    """
    if not _looks_like_gazebo_recommendation_request(user_text):
        return reply
    mentioned = _gazebo_titles_mentioned_anywhere(reply)
    if not mentioned:
        return reply
    allowed = _available_gazebos_for_recommendation(draft)
    if not allowed:
        return reply
    forbidden = [title for title in mentioned if title not in allowed]
    if not forbidden:
        return reply

    logger.warning(
        "DROP_UNAVAILABLE_GAZEBO_RECOMMENDATION date=%s forbidden=%s allowed=%s reply=%r",
        draft.date, forbidden, allowed, reply,
    )
    return _llm_reply_from_engine_context(
        draft=draft,
        chat_id=chat_id,
        user_text=user_text,
        history=history,
        today=today,
        event="gazebo_recommendation_repair",
        data={
            "date": draft.date,
            "guests_count": draft.guests_count,
            "allowed_gazebos": allowed,
            "forbidden_gazebos": forbidden,
            "existing_reply_to_replace": reply,
            "rules": [
                "recommend_only_allowed_gazebos",
                "use_knowledge_only_for_characteristics",
                "answer_the_customer_question",
            ],
        },
        fallback=None,
    )


def _neutralize_staff_blame(
    reply: str,
    draft: BookingDraft,
    *,
    user_text: str,
    chat_id: str,
    history: list[dict[str, Any]],
    today: str,
) -> str:
    """Ask the response model to replace accusations with neutral wording."""
    blame_patterns = (
        r"(?i)(?:возможно,?\s*)?[А-ЯЁA-Z][А-Яа-яЁёA-Za-z-]+\s+ошиб(?:ся|лась)\.?",
        r"(?i)(?:администратор|сотрудник|менеджер|коллега)[^.\n!?]{0,80}ошиб(?:ся|лась|ается)\.?",
        r"(?i)вам\s+(?:сказали|сообщили)[^.\n!?]{0,40}(?:неверно|неправильно)\.?",
    )
    cleaned = reply or ""
    changed = False
    for pattern in blame_patterns:
        cleaned, count = re.subn(pattern, "", cleaned)
        changed = changed or count > 0
    if not changed:
        return reply
    cleaned = re.sub(r"[ \t]+\n", "\n", cleaned)
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned).strip()
    return _llm_reply_from_engine_context(
        draft=draft,
        chat_id=chat_id,
        user_text=user_text,
        history=history,
        today=today,
        event="staff_information_conflict_repair",
        data={
            "existing_reply_without_accusation": cleaned,
            "rules": [
                "do_not_blame_or_correct_employee",
                "state_information_conflict_neutrally",
                "request_manual_review_without_promising_completed_action",
            ],
        },
        fallback=None,
    )


def _looks_like_options_photo_context(text: str) -> bool:
    """User is browsing object variants; send photos for listed variants automatically."""
    lowered = (text or "").lower().replace("ё", "е")
    if any(marker in lowered for marker in (
        "что кроме", "какие варианты", "что есть", "че есть", "что имеется", "че имеется",
        "что у вас есть", "что у вас имеется", "какие объекты", "все варианты",
        "все доступные варианты", "вообще все варианты", "в целом",
        "какие есть", "рассмотреть варианты", "покажи варианты", "варианты на",
        "а что на", "что на ", "что доступно на", "что свободно на"
    )):
        return True
    # Typical message: «а что на 22 есть» / «на 20 что есть».
    return bool(re.search(r"\b(?:а\s+)?что\s+на\s+\d{1,2}\b", lowered) or re.search(r"\bна\s+\d{1,2}\s+что\s+(?:есть|свобод|доступ)", lowered))


def _last_bot_asked_confirmation(history: list[dict[str, Any]] | None) -> bool:
    for item in reversed(history or []):
        sender = str(item.get("sender") or "").lower()
        text = str(item.get("text") or "").lower().replace("ё", "е")
        if sender in {"user", "client"}:
            continue
        return any(marker in text for marker in ("все верно", "всё верно", "подтверд", "если все правильно", "если всё правильно"))
    return False


def _booking_core_with_contacts_ready(draft: BookingDraft) -> bool:
    return bool(
        draft.service_type and draft.date and draft.time and draft.duration
        and draft.guests_count and draft.client_name and draft.phone
    )


def _maybe_force_watchlist_offer(reply: str, draft: BookingDraft) -> str:
    return reply

def _bathhouse_message_starts_booking(user_text: str, decision_intent: str | None) -> bool:
    lowered = (user_text or "").lower().replace("ё", "е")
    if any(marker in lowered for marker in ("свобод", "доступ", "занят")) and not any(
        marker in lowered for marker in ("заброни", "оформ", "хочу", "берем", "берём", "возьм", "давайте")
    ):
        return False
    if any(marker in lowered for marker in ("заброни", "оформ", "хочу бан", "берем бан", "берём бан", "возьм", "давайте бан")):
        return True
    return str(decision_intent or "") in {"booking", "booking_request", "continue_booking"}


def _maybe_answer_bathhouse_date_availability(
    *,
    before: BookingDraft,
    draft: BookingDraft,
    chat_id: str,
    user_text: str,
    history: list[dict[str, Any]],
    today: str,
    current_message_has_duration: bool,
    decision_intent: str | None,
) -> str | None:
    """Answer bathhouse day availability before collecting booking details."""
    if draft.service_type != "bathhouse":
        return None
    if not draft.date or draft.duration or current_message_has_duration:
        return None

    changed_relevant = any(
        getattr(before, key) != getattr(draft, key)
        for key in ("service_type", "date", "time")
    )
    if not changed_relevant:
        return None

    if (draft.pending_action or {}).get("type") == "watchlist_create":
        logger.info("BATHHOUSE_DURATION_REQUIRED_CLEAR_WATCHLIST_PENDING chat_id=%s", chat_id)
        draft.pending_action = {}
    draft.block_reason = None
    draft.blocked_until = None

    try:
        availability = check_availability(draft, chat_id=chat_id)
    except Exception:
        logger.exception("BATHHOUSE_DATE_AVAILABILITY_CHECK_FAILED chat_id=%s date=%s", chat_id, draft.date)
        return None

    if not availability.ok:
        event = "schedule_not_open" if availability.message == "schedule_not_open" else "bathhouse_date_unavailable"
        return _llm_reply_from_engine_context(
            draft=draft,
            chat_id=chat_id,
            user_text=user_text,
            history=history,
            today=today,
            event=event,
            data=_availability_event_data(draft, availability.message),
            fallback=_fallback_question(draft),
        )

    draft.available_variants = availability.variants
    starts_booking = _bathhouse_message_starts_booking(user_text, decision_intent)

    return _llm_reply_from_engine_context(
        draft=draft,
        chat_id=chat_id,
        user_text=user_text,
        history=history,
        today=today,
        event=("bathhouse_date_available_continue_booking" if starts_booking else "bathhouse_date_available_offer_booking"),
        data={
            "service_type": draft.service_type,
            "object_title": draft.service_variant or service_title(draft.service_type),
            "date": draft.date,
            "time_already_given": draft.time,
            "next_step": "duration",
            "minimum_duration_hours": 3,
            "duration_has_no_seven_hour_maximum": True,
            "booking_requested": starts_booking,
            "rules": (["confirm_date_available", "ask_only_bathhouse_duration"] if starts_booking else ["confirm_date_available", "ask_if_customer_wants_to_book"]),
        },
        fallback=_fallback_question(draft),
    )



def _guard_duration_based_date_only_reply(
    reply: str,
    before: BookingDraft,
    draft: BookingDraft,
    *,
    chat_id: str,
    user_text: str,
    history: list[dict[str, Any]],
    today: str,
) -> str:
    """Never let the LLM close a whole date for duration/time based objects.

    Bathhouse/house availability depends on the requested duration and start time.
    A booking that starts the previous day or one occupied interval must not make the
    bot tell the client that the whole date is unavailable before exact time is known.
    """
    if draft.service_type not in {"bathhouse", "house"}:
        return reply
    if not draft.date:
        return reply
    # Only guard broad date-level answers. Exact slot conflicts are handled by
    # _maybe_check_availability after time+duration are known.
    if draft.time and draft.duration:
        return reply
    low = (reply or "").lower().replace("ё", "е")
    suspicious = any(marker in low for marker in (
        "недоступ", "нет свобод", "все занято", "все заняты", "ближайшая свободная дата",
        "ближайший свободный день", "к сожалению",
    ))
    if not suspicious:
        return reply
    return _llm_reply_from_engine_context(
        draft=draft,
        chat_id=chat_id,
        user_text=user_text,
        history=history,
        today=today,
        event="availability_requires_exact_interval",
        data={
            "booking": _booking_reply_data(draft),
            "missing_fields": [
                field
                for field, value in (("time", draft.time), ("duration", draft.duration))
                if not value
            ],
            "rule": "do_not_claim_whole_date_unavailable_before_exact_interval",
        },
        fallback=None,
    )


def _time_to_minutes(value: str | None) -> int | None:
    try:
        hour, minute = str(value or "")[:5].split(":")
        return int(hour) * 60 + int(minute)
    except Exception:
        return None


def _duration_to_minutes(value: Any) -> int | None:
    try:
        minutes = int(float(value) * 60)
        return minutes if minutes > 0 else None
    except Exception:
        return None


def _reserved_by_other_on_exact_time(draft: BookingDraft, *, chat_id: str) -> dict[str, Any] | None:
    if not (draft.service_type and draft.date and draft.time and draft.duration):
        return None
    try:
        rows = sqlite.active_holds_for_service_date(draft.service_type, draft.date, ignore_chat_id=chat_id)
    except Exception:
        logger.exception("ACTIVE_HOLDS_EXACT_FAILED chat_id=%s", chat_id)
        return None
    start = _time_to_minutes(draft.time)
    dur = _duration_to_minutes(draft.duration)
    if start is None or dur is None:
        return None
    end = start + dur
    current_variant = draft.service_variant or ""
    for row in rows:
        item = dict(row)
        # For numbered gazebos the exact object matters; for bathhouse/house/warm gazebo
        # service_type is already the object.
        other_variant = item.get("service_variant") or ""
        if draft.service_type == "gazebo" and other_variant != current_variant:
            continue
        other_start = _time_to_minutes(str(item.get("time") or ""))
        other_dur = _duration_to_minutes(item.get("duration"))
        if other_start is None or other_dur is None:
            continue
        other_end = other_start + other_dur
        if start < other_end and other_start < end:
            return {"time": item.get("time"), "duration": item.get("duration"), "status": item.get("status")}
    return None


def _filter_requested_media_for_customer(
    requested_media: list[str] | None,
    *,
    reply: str,
    before: BookingDraft,
    draft: BookingDraft,
    user_text: str,
) -> list[str]:
    media = [str(item) for item in (requested_media or []) if item]

    inferred = _infer_requested_media_from_context(
        user_text=user_text,
        reply=reply,
        before=before,
        draft=draft,
    )

    explicit_photo = _explicitly_asks_for_photo(user_text)
    options_photo = _looks_like_options_photo_context(user_text)

    if (
        (getattr(draft, "pending_action", {}) or {}).get("type")
        in {"await_reschedule_date", "reschedule_booking"}
    ):
        logger.info("MEDIA_SUPPRESSED reschedule_flow media=%s", media)
        return []

    broad_gazebo_availability = bool(
        draft.service_type == "gazebo"
        and not draft.service_variant
        and _is_broad_availability_reply(reply, draft)
    )

    if broad_gazebo_availability:
        result = inferred or media
        logger.info("MEDIA_ALLOWED gazebo_gallery media=%s", result)
        return result

    if (
        draft.service_type == "gazebo"
        and draft.service_variant
        and before.service_variant != draft.service_variant
        and not explicit_photo
        and not options_photo
    ):
        return [str(draft.service_variant)]

    if not media:
        media = inferred

    if not media:
        return []

    if explicit_photo or options_photo:
        return media

    if _is_broad_availability_reply(reply, draft):
        logger.info(
            "MEDIA_SUPPRESSED broad_non_gazebo media=%s",
            media,
        )
        return []

    collecting_steps = {
        "time",
        "duration",
        "guests_count",
        "upsell_items",
        "client_name",
        "phone",
        "confirmation",
    }

    if (
        draft.service_type
        and draft.next_step() in collecting_steps
        and before.service_type == draft.service_type
        and before.service_variant == draft.service_variant
    ):
        logger.info(
            "MEDIA_SUPPRESSED collecting_flow step=%s media=%s",
            draft.next_step(),
            media,
        )
        return []

    return media



def _infer_requested_media_from_context_base(
    *,
    user_text: str,
    reply: str,
    before: BookingDraft,
    draft: BookingDraft,
) -> list[str]:
    user_lower = (user_text or "").lower().replace("ё", "е")
    reply_lower = (reply or "").lower().replace("ё", "е")
    combined = user_lower + " " + reply_lower

    result: list[str] = []

    def add(item: str) -> None:
        if item and item not in result:
            result.append(item)

    explicit_photo = (
        _explicitly_asks_for_photo(user_text)
        or any(
            marker in combined
            for marker in (
                "отправляю фото",
                "фото доступ",
                "фото вариантов",
            )
        )
    )

    options_photo = _looks_like_options_photo_context(user_text)

    object_newly_selected = bool(
        draft.service_type
        and (
            draft.service_type != before.service_type
            or draft.service_variant != before.service_variant
        )
    )

    broad_gazebo_availability = bool(
        draft.service_type == "gazebo"
        and not draft.service_variant
        and _is_broad_availability_reply(reply, draft)
    )

    if not (
        explicit_photo
        or options_photo
        or object_newly_selected
        or broad_gazebo_availability
    ):
        return []

    if draft.service_type == "bathhouse":
        add("bathhouse")

    if draft.service_type == "house":
        add("house")

    if draft.service_type == "warm_gazebo":
        add("Тёплая беседка")

    # Выбранный вариант имеет приоритет. Число из даты больше
    # не может случайно превратиться в номер беседки.
    if draft.service_type == "gazebo":
        if draft.service_variant:
            add(str(draft.service_variant))
        else:
            for title in _gazebo_titles_from_text(reply):
                add(title)

            if "крыт" in reply_lower and "бесед" in reply_lower:
                add("Крытая беседка")

    if "тепл" in reply_lower and "бесед" in reply_lower:
        add("Тёплая беседка")

    if "гостев" in reply_lower or re.search(r"\bдом\b", reply_lower):
        add("house")

    if "бан" in reply_lower:
        add("bathhouse")

    return result

# POINT_8_SPECIFIC_GAZEBO_MEDIA
def _infer_requested_media_from_context(
    *,
    user_text: str,
    reply: str,
    before: BookingDraft,
    draft: BookingDraft,
) -> list[str]:
    result = _infer_requested_media_from_context_base(
        user_text=user_text,
        reply=reply,
        before=before,
        draft=draft,
    )

    if result:
        return result

    if draft.service_type != "gazebo":
        return []

    reply_lower = (reply or "").lower().replace("ё", "е")

    # Дополнительная страховка: если бот сообщает, что свободна
    # одна конкретная беседка, отправляем именно её фото.
    if "бесед" not in reply_lower:
        return []

    found: list[str] = []

    def add(item: str) -> None:
        if item and item not in found:
            found.append(item)

    for title in _gazebo_titles_from_text(reply):
        add(title)

    if "крыт" in reply_lower and "бесед" in reply_lower:
        add("Крытая беседка")

    if "тепл" in reply_lower and "бесед" in reply_lower:
        add("Тёплая беседка")

    if found:
        logger.info(
            "MEDIA_ALLOWED specific_available_gazebo media=%s",
            found,
        )

    return found




def _availability_event_data(draft: BookingDraft, availability_message: str | None = None) -> dict[str, Any]:
    schedule_not_open = availability_message == "schedule_not_open"
    return {
        "service_type": draft.service_type,
        "object_title": draft.service_variant or service_title(draft.service_type) or "выбранный вариант",
        "date": draft.date,
        "human_date": _human_date(draft.date),
        "time": draft.time or draft.blocked_until,
        "available_times": "" if schedule_not_open else (availability_message or ""),
        "schedule_not_open": schedule_not_open,
        "can_watchlist": bool(draft.service_type and draft.date and not schedule_not_open),
        "must_offer_watchlist": bool(draft.service_type and draft.date and not schedule_not_open),
        "next_expected_step": "date" if schedule_not_open else draft.next_step(),
    }


def _ensure_watchlist_offer_in_reply(reply: str, draft: BookingDraft) -> str:
    return reply

def _explicitly_insists_on_current_unavailable_date(text: str, draft: BookingDraft) -> bool:
    if not (draft.pending_action or {}).get("type") == "watchlist_create":
        return False
    lowered = (text or "").lower().replace("ё", "е").strip()
    if not lowered:
        return False
    if any(marker in lowered for marker in ("эту дату", "именно эту", "хочу эту", "хочу 19", "надо 19", "мне 19")):
        return True
    # If the client repeats the same day/month from the pending watchlist date.
    cand = (draft.pending_action or {}).get("candidate") or {}
    date = cand.get("date") or draft.date
    try:
        day = int(str(date).split("-")[2])
    except Exception:
        return False
    return bool(re.search(rf"\b{day}\s*(?:июн|числ|го)?", lowered) and any(m in lowered for m in ("хочу", "надо", "нужн", "давайте")))


def _available_dates_for_service(
    service_type: str | None,
    *,
    limit: int = 10,
    current_booking: BookingDraft | None = None,
    chat_id: str | None = None,
) -> list[str]:
    """Return dates that are really free for the selected object/service.

    Cache rows are only candidates. Before showing a date during reschedule, run
    the same deterministic live availability check that will be used when the
    client chooses the date. This prevents contradictions like: first offering
    1 July, then saying 1 July is busy.
    """
    if not service_type:
        return []
    try:
        rows = sqlite.list_availability_rows(service_type=service_type, limit=5000)
    except Exception:
        logger.exception("AVAILABLE_DATES_FROM_CACHE_FAILED service_type=%s", service_type)
        return []

    today = now_local().date().isoformat()
    candidate_dates: list[str] = []
    for row in rows:
        date = str(row.get("date") or "")
        status = str(row.get("status") or "")
        time_value = str(row.get("time") or "")
        if not date or date < today:
            continue
        if status == "empty" or not time_value:
            continue
        if date not in candidate_dates:
            candidate_dates.append(date)

    dates: list[str] = []
    for date in candidate_dates:
        if current_booking:
            probe = BookingDraft.from_dict(current_booking.to_dict())
            probe.service_type = service_type or probe.service_type
            probe.date = date
            # Keep the existing paid booking's time/duration/variant. For houses,
            # baths and gazebos this is what determines whether the move is possible.
            try:
                availability = check_availability(probe, chat_id=chat_id)
            except Exception:
                logger.exception("AVAILABLE_DATES_LIVE_CHECK_FAILED service_type=%s date=%s", service_type, date)
                continue
            if not availability.ok:
                logger.info(
                    "AVAILABLE_DATES_SKIP_BUSY service_type=%s date=%s object=%s",
                    service_type,
                    date,
                    probe.service_variant or service_title(probe.service_type),
                )
                continue
        dates.append(date)
        if len(dates) >= limit:
            break
    return dates


def _should_engine_own_service_date_list(before: BookingDraft, draft: BookingDraft, user_text: str) -> bool:
    lowered = (user_text or "").lower().replace("ё", "е")
    if not draft.service_type or draft.date:
        return False
    if draft.next_step() != "date":
        return False
    # The client selected an object or asks when it is free. Date lists are
    # operational data, so code must provide the list and LLM must only phrase it.
    if before.service_type != draft.service_type:
        return True
    return any(marker in lowered for marker in ("когда", "даты", "дату", "свобод", "есть", "пораньше", "позже"))


def _engine_service_date_list_reply(
    *,
    draft: BookingDraft,
    chat_id: str,
    user_text: str,
    history: list[dict[str, Any]],
    today: str,
) -> str:
    dates = _available_dates_for_service(draft.service_type, limit=10)
    draft.last_offered_dates = dates[:20]
    draft.last_offered_service_type = draft.service_type
    draft.last_offered_object_title = draft.service_variant or _OBJECT_TITLE_BY_SERVICE.get(draft.service_type or "") or service_title(draft.service_type)
    return _llm_reply_from_engine_context(
        draft=draft,
        chat_id=chat_id,
        user_text=user_text,
        history=history,
        today=today,
        event="available_dates_for_service",
        data={
            "service_type": draft.service_type,
            "object_title": draft.last_offered_object_title,
            "available_dates": dates,
            "rules": ["use_only_available_dates"],
            "next_expected_step": "date",
        },
        fallback=_fallback_question(draft),
    )


def _make_watchlist_candidate_from_draft(draft: BookingDraft) -> WatchlistCandidate | None:
    if not draft.service_type or not draft.date:
        return None
    title = draft.service_variant or _OBJECT_TITLE_BY_SERVICE.get(draft.service_type or "") or service_title(draft.service_type)
    if not title:
        return None
    return WatchlistCandidate(
        service_type=draft.service_type,
        object_title=str(title),
        date=str(draft.date),
        time=draft.time,
        duration=draft.duration,
    )


def _normalize_event_format_value(value: str | None) -> str | None:
    if not value:
        return value
    text = str(value).strip()
    lowered = text.lower().replace("ё", "е")
    # Do not store the client's literal phrase like "что то другое" as a format.
    # The admin/client summary should use a canonical value.
    if "друг" in lowered or re.search(r"что\s*-?\s*то\s+друг", lowered):
        return "другое"
    casual_rest = ("бух", "пьян", "тус", "посид", "отдох", "отдых", "чил", "шашлык")
    if any(marker in lowered for marker in casual_rest):
        return "отдых"
    birthday = ("день рожд", "др", "днюх", "юбилей")
    if any(marker in lowered for marker in birthday):
        return "день рождения"
    return text



def _llm_reply_from_engine_context(
    *,
    draft: BookingDraft,
    chat_id: str,
    user_text: str,
    history: list[dict[str, Any]],
    today: str,
    event: str,
    data: dict[str, Any] | None = None,
    fallback: str | None = None,
) -> str:
    """Ask the answer model to write client-facing text from a structured engine event.

    The engine owns state and operations. It must not script customer messages.
    This helper passes a compact, non-technical event to the LLM and uses only the
    generated reply. Returned draft/actions from this call are intentionally ignored.
    """
    payload = data or {}
    context = (
        f"{user_text}\n\n"
        f"ENGINE_EVENT={event}\n"
        f"ENGINE_DATA={payload}\n\n"
        f"{load_engine_response_prompt()}"
    )
    try:
        decision = decide(context, draft, today=today, history=history)
        text = (decision.reply or "").strip()
        if text:
            if event == "upsell_second_offer":
                low_second = text.lower().replace("ё", "е")
                # The second touch must not sound like the exact first sales pitch.
                # If the model repeats the first offer, replace it with a shorter final check.
                repeated_first_offer = (
                    "не пришлось везти" in low_second
                    or "что-нибудь добавить" in low_second
                    or "что нибудь добавить" in low_second
                )
                if repeated_first_offer:
                    logger.warning("LLM_UPSELL_SECOND_REPEATED_FIRST_OFFER reply=%r", text)
                if not _reply_mentions_upsell_for_engine(text):
                    logger.warning("LLM_UPSELL_SECOND_IGNORED_EVENT reply=%r", text)
            return text
    except Exception:
        logger.exception("LLM_ENGINE_CONTEXT_REPLY_FAILED event=%s", event)
    return fallback if fallback is not None else ""



def _looks_like_later_payment(text: str) -> bool:
    low = (text or "").lower().replace("ё", "е")
    has_payment = any(x in low for x in ("оплат", "предоплат", "платеж", "платёж"))
    has_later = any(x in low for x in (
        "вечером", "позже", "потом", "завтра", "через час", "через пару часов",
        "после работы", "когда будет интернет", "как будет интернет"
    ))
    return has_payment and has_later


def _looks_like_payment_link_refresh_request(text: str) -> bool:
    low = (text or "").lower().replace("ё", "е")

    markers = (
        "ссылка не работает",
        "ссылка не открывается",
        "ссылка истек",
        "ссылка просроч",
        "ссылка уже не",
        "срок ссылки",
        "новую ссыл",
        "актуальную ссыл",
        "новую оплат",
        "повторную оплат",
        "повторно оплат",
        "снова оплат",
        "обнови ссыл",
        "скинь ссыл",
        "отправь ссыл",
        "дай ссыл",
        "можно ссыл",
        "хочу оплат",
        "готов оплат",
        "оплатить",
        "перейти к оплат",
        "подтверждаю оплат",
    )

    if any(m in low for m in markers):
        return True

    # Короткое "все верно" на этапе waiting_payment часто означает переход к оплате.
    if low.strip() in {"все верно", "всё верно", "да", "да все верно", "подтверждаю"}:
        return True

    return False


def _find_waiting_payment_booking_id(chat_id: str, draft: BookingDraft) -> int | None:
    try:
        rows = sqlite.list_pending_payments()
    except Exception:
        logger.exception("PAYMENT_REFRESH_LIST_PENDING_FAILED chat_id=%s", chat_id)
        return None

    same_chat = []
    for row in rows or []:
        if str(row.get("chat_id")) != str(chat_id):
            continue
        same_chat.append(row)

        if draft.payment_id and str(row.get("payment_id") or "") == str(draft.payment_id):
            try:
                return int(row["id"])
            except Exception:
                return None

    # Если payment_id уже поменялся/потерялся, берём последнюю ожидающую оплату этого чата.
    if same_chat:
        try:
            return int(same_chat[-1]["id"])
        except Exception:
            return None

    return None


def _refresh_waiting_payment_link(chat_id: str, draft: BookingDraft, *, platform: str | None = "max") -> str:
    booking_id = _find_waiting_payment_booking_id(chat_id, draft)
    if booking_id is None:
        logger.warning("PAYMENT_REFRESH_NO_BOOKING_ID chat_id=%s draft=%s", chat_id, _draft_log_line(draft))
        return _llm_reply_from_engine_context(
            draft=draft, chat_id=chat_id, user_text="", history=_recent_history(chat_id, draft),
            today=now_local().date().isoformat(), event="payment_link_refresh_failed",
            data={"reason": "booking_not_found", "admin_notified": False}, fallback=None,
        )

    try:
        payment_result = create_prepayment(draft, chat_id=chat_id, booking_id=booking_id)
        payment_id = str(payment_result[0])
        payment_url = str(payment_result[1])
    except Exception:
        logger.exception("PAYMENT_REFRESH_CREATE_FAILED chat_id=%s booking_id=%s", chat_id, booking_id)
        return _llm_reply_from_engine_context(
            draft=draft, chat_id=chat_id, user_text="", history=_recent_history(chat_id, draft),
            today=now_local().date().isoformat(), event="payment_link_refresh_failed",
            data={"reason": "payment_provider_error", "admin_notified": False}, fallback=None,
        )

    draft.payment_id = payment_id
    draft.payment_url = payment_url
    draft.status = "waiting_payment"

    try:
        sqlite.update_booking(booking_id, draft.to_dict(), status="waiting_payment")
        sqlite.save_draft(chat_id, draft.to_dict(), status="waiting_payment", current_step=None)
    except Exception:
        logger.exception("PAYMENT_REFRESH_SAVE_FAILED chat_id=%s booking_id=%s payment_id=%s", chat_id, booking_id, payment_id)

    logger.info("PAYMENT_LINK_REFRESHED chat_id=%s booking_id=%s payment_id=%s", chat_id, booking_id, payment_id)

    reply = _llm_reply_from_engine_context(
        draft=draft, chat_id=chat_id, user_text="", history=_recent_history(chat_id, draft),
        today=now_local().date().isoformat(), event="payment_link_refreshed",
        data={
            "payment_url": payment_url,
            "payment_link_ttl_minutes_approx": 60,
            "rules": ["include_payment_url_verbatim", "do_not_claim_payment_succeeded"],
        },
        fallback=None,
    )
    return reply if payment_url in reply else "\n\n".join(part for part in (reply, payment_url) if part)


def _looks_like_payment_refusal(text: str) -> bool:
    lowered = (text or "").lower().replace("ё", "е")
    refusal_markers = (
        "не хочу оплачивать", "не буду оплачивать", "не хочу платить", "не буду платить",
        "без оплаты", "без предоплаты", "не хочу предоплату", "не буду предоплату",
        "налич", "на месте", "потом оплат", "позже оплат", "откажусь от оплаты",
    )
    return any(marker in lowered for marker in refusal_markers)


def _cancellation_days_until(
    draft: BookingDraft,
) -> int | None:
    if not draft.date:
        return None

    try:
        booking_date = datetime.fromisoformat(
            str(draft.date)
        ).date()
    except ValueError:
        return None

    return (booking_date - now_local().date()).days


def _cancellation_confirmation_reply(
    draft: BookingDraft,
    *,
    chat_id: str,
    user_text: str,
    history: list[dict[str, Any]],
    today: str,
) -> str:
    days_until = _cancellation_days_until(draft)
    return _llm_reply_from_engine_context(
        draft=draft, chat_id=chat_id, user_text=user_text, history=history, today=today,
        event="cancel_booking_confirmation",
        data={
            "booking": _booking_reply_data(draft),
            "days_until_booking": days_until,
            "refund_full_if_days_before_at_least": 7,
        },
        fallback=None,
    )


def _cancellation_completed_reply(
    draft: BookingDraft,
    *,
    manual_review: bool,
    chat_id: str,
) -> str:
    days_until = _cancellation_days_until(draft)
    return _llm_reply_from_engine_context(
        draft=draft, chat_id=chat_id, user_text="", history=_recent_history(chat_id),
        today=now_local().date().isoformat(), event="cancel_booking_completed",
        data={
            "booking": _booking_reply_data(draft),
            "manual_review": manual_review,
            "admin_notified": True,
            "days_until_booking": days_until,
            "refund_full_if_days_before_at_least": 7,
        },
        fallback=None,
    )

def _payment_pending_fallback_reply(draft: BookingDraft, user_text: str) -> str:
    return ""


def _parse_date_fragment_for_payment_change(fragment: str) -> str | None:
    fragment = str(fragment or "").strip()
    if not fragment:
        return None

    ru_dates_fn = globals().get("_extract_ru_dates_from_text")
    if callable(ru_dates_fn):
        try:
            dates = ru_dates_fn(fragment)
            if dates:
                return str(dates[0])
        except Exception:
            logger.exception("PAYMENT_DATE_FRAGMENT_RU_PARSE_FAILED fragment=%r", fragment)

    try:
        value = _extract_explicit_day_date(fragment)
        if value:
            return str(value)
    except Exception:
        logger.exception("PAYMENT_DATE_FRAGMENT_EXPLICIT_PARSE_FAILED fragment=%r", fragment)

    return None


def _extract_waiting_payment_new_date(text: str, draft: BookingDraft) -> str | None:
    low = (text or "").lower().replace("ё", "е")
    if not low.strip():
        return None

    fragments: list[str] = []

    patterns = [
        r"\bне\s+на\s+.+?\s+а\s+на\s+([^,.!?]+)",
        r"\bа\s+на\s+([^,.!?]+)",
        r"\bдавайте\s+на\s+([^,.!?]+)",
        r"\bдавай\s+на\s+([^,.!?]+)",
        r"\bлучше\s+на\s+([^,.!?]+)",
        r"\bна\s+([^,.!?]+?)\s+лучше\b",
        r"\bперенес\w*\s+на\s+([^,.!?]+)",
        r"\bпоменя\w*\s+на\s+([^,.!?]+)",
        r"\bизмен\w*\s+на\s+([^,.!?]+)",
    ]

    for pattern in patterns:
        for match in re.finditer(pattern, low):
            fragments.append(match.group(1))

    # fallback: if the whole message has dates, choose a date different from current draft date
    whole_dates: list[str] = []
    ru_dates_fn = globals().get("_extract_ru_dates_from_text")
    if callable(ru_dates_fn):
        try:
            whole_dates = [str(x) for x in (ru_dates_fn(low) or [])]
        except Exception:
            whole_dates = []

    for fragment in fragments:
        date_value = _parse_date_fragment_for_payment_change(fragment)
        if date_value and date_value != draft.date:
            return date_value

    for date_value in whole_dates:
        if date_value and date_value != draft.date:
            return date_value

    explicit = _parse_date_fragment_for_payment_change(low)
    if explicit and explicit != draft.date:
        return explicit

    return None


def _looks_like_waiting_payment_date_change(text: str, draft: BookingDraft) -> bool:
    low = (text or "").lower().replace("ё", "е")
    if not draft.date:
        return False

    has_change_marker = any(marker in low for marker in (
        "перенес", "перенести", "поменя", "измен", "другая дата",
        "не получится", "не могу", "лучше", "давайте на", "давай на",
        "не на", "а на",
    ))

    if not has_change_marker:
        return False

    return bool(_extract_waiting_payment_new_date(text, draft))


def _waiting_payment_reschedule_reply(
    chat_id: str,
    text: str,
    draft: BookingDraft,
    *,
    history: list[dict[str, Any]],
    today: str,
    platform: str | None = "max",
) -> str | None:
    new_date = _extract_waiting_payment_new_date(text, draft)
    if not new_date or new_date == draft.date:
        return None

    old_draft = BookingDraft.from_dict(draft.to_dict())
    old_booking_id = _find_waiting_payment_booking_id(chat_id, draft)

    probe = BookingDraft.from_dict(draft.to_dict())
    probe.date = new_date
    probe.payment_id = None
    probe.payment_url = None
    probe.yclients_record_id = None
    probe.status = "collecting"
    probe.block_reason = None
    probe.blocked_until = None

    availability = check_availability(probe, chat_id=chat_id)
    if not availability.ok:
        logger.info(
            "WAITING_PAYMENT_RESCHEDULE_UNAVAILABLE chat_id=%s old_date=%s new_date=%s draft=%s",
            chat_id,
            old_draft.date,
            new_date,
            _draft_log_line(probe),
        )
        sqlite.save_draft(chat_id, old_draft.to_dict(), status="waiting_payment", current_step=None)
        return _llm_reply_from_engine_context(
            draft=old_draft,
            chat_id=chat_id,
            user_text=text,
            history=history,
            today=today,
            event="reschedule_date_unavailable",
            data={
                "old_date": old_draft.date,
                "requested_date": new_date,
                "object_title": old_draft.service_variant or service_title(old_draft.service_type),
                "payment_url": old_draft.payment_url,
                "message": availability.message,
            },
            fallback=None,
        )

    try:
        new_booking_id = sqlite.create_booking(chat_id, probe.to_dict(), status="waiting_payment", platform=platform)
        payment_id, payment_url = create_prepayment(probe, chat_id=chat_id, booking_id=new_booking_id)

        probe.payment_id = str(payment_id)
        probe.payment_url = str(payment_url)
        probe.status = "waiting_payment"

        sqlite.update_booking(new_booking_id, probe.to_dict(), status="waiting_payment")

        if old_booking_id is not None:
            try:
                sqlite.update_booking(old_booking_id, old_draft.to_dict(), status="payment_superseded")
            except Exception:
                logger.exception("WAITING_PAYMENT_OLD_BOOKING_CANCEL_MARK_FAILED chat_id=%s booking_id=%s", chat_id, old_booking_id)

        try:
            sqlite.upsert_hold(chat_id, probe.to_dict())
        except Exception:
            logger.exception("WAITING_PAYMENT_RESCHEDULE_HOLD_FAILED chat_id=%s booking_id=%s", chat_id, new_booking_id)

        draft.__dict__.update(probe.__dict__)
        sqlite.save_draft(chat_id, draft.to_dict(), status="waiting_payment", current_step=None)

        try:
            notify_admin_booking_created(chat_id=chat_id, booking_id=new_booking_id, draft=draft)
        except Exception:
            logger.exception("WAITING_PAYMENT_RESCHEDULE_ADMIN_NOTIFY_FAILED chat_id=%s booking_id=%s", chat_id, new_booking_id)

        logger.info(
            "WAITING_PAYMENT_RESCHEDULED_WITH_NEW_PAYMENT chat_id=%s old_booking_id=%s new_booking_id=%s old_date=%s new_date=%s payment_id=%s",
            chat_id,
            old_booking_id,
            new_booking_id,
            old_draft.date,
            new_date,
            payment_id,
        )

        return _llm_reply_from_engine_context(
            draft=draft, chat_id=chat_id, user_text=text, history=history, today=today,
            event="waiting_payment_rescheduled_with_new_payment",
            data={
                "old_date": old_draft.date,
                "new_date": new_date,
                "old_payment_link_invalid": True,
                "payment_url": payment_url,
                "booking": _booking_reply_data(draft),
                "rules": ["include_payment_url_verbatim", "do_not_claim_payment_succeeded"],
            },
            fallback=None,
        )

    except Exception as exc:
        logger.exception("WAITING_PAYMENT_RESCHEDULE_PAYMENT_FAILED chat_id=%s new_date=%s", chat_id, new_date)
        sqlite.save_draft(chat_id, old_draft.to_dict(), status="waiting_payment", current_step=None)
        sqlite.enqueue_admin_notification(
            "Клиент попросил поменять дату брони на этапе предоплаты, но новая ссылка оплаты не создалась.\n"
            f"chat_id: {chat_id}\n"
            f"Старая дата: {old_draft.date}\n"
            f"Новая дата: {new_date}\n"
            f"Ошибка: {exc}\n"
            f"Заявка: {old_draft.to_dict()}",
            chat_id=chat_id,
        )
        return _llm_reply_from_engine_context(
            draft=old_draft, chat_id=chat_id, user_text=text, history=history, today=today,
            event="waiting_payment_reschedule_failed",
            data={"requested_date": new_date, "admin_notified": True}, fallback=None,
        )


def _waiting_payment_variant_change_reply(
    chat_id: str,
    text: str,
    draft: BookingDraft,
    new_variant: str,
    *,
    history: list[dict[str, Any]],
    today: str,
) -> str:
    """Invalidate a prepared payment before changing its concrete gazebo."""
    old_draft = BookingDraft.from_dict(draft.to_dict())
    old_booking_id = _find_waiting_payment_booking_id(chat_id, old_draft)
    variant = variant_by_title("gazebo", new_variant)
    canonical_variant = str((variant or {}).get("title") or new_variant)

    if not variant:
        return _llm_reply_from_engine_context(
            draft=old_draft,
            chat_id=chat_id,
            user_text=text,
            history=history,
            today=today,
            event="waiting_payment_variant_unavailable",
            data={
                "requested_variant": new_variant,
                "current_variant": old_draft.service_variant,
                "reason": "unknown_variant",
            },
            fallback=None,
        )

    probe = BookingDraft.from_dict(old_draft.to_dict())
    probe.service_variant = canonical_variant
    probe.payment_id = None
    probe.payment_url = None
    probe.yclients_record_id = None
    probe.status = "active"
    probe.block_reason = None
    probe.blocked_until = None
    probe.pending_action = {}

    availability = check_availability(probe, chat_id=chat_id)
    if not availability.ok:
        sqlite.save_draft(chat_id, old_draft.to_dict(), status="waiting_payment", current_step=None)
        logger.info(
            "WAITING_PAYMENT_VARIANT_CHANGE_UNAVAILABLE chat_id=%s old_variant=%s new_variant=%s",
            chat_id,
            old_draft.service_variant,
            canonical_variant,
        )
        return _llm_reply_from_engine_context(
            draft=old_draft,
            chat_id=chat_id,
            user_text=text,
            history=history,
            today=today,
            event="waiting_payment_variant_unavailable",
            data={
                "requested_variant": canonical_variant,
                "current_variant": old_draft.service_variant,
                "message": availability.message,
            },
            fallback=None,
        )

    if old_booking_id is not None:
        sqlite.update_booking(old_booking_id, old_draft.to_dict(), status="payment_superseded")

    draft.__dict__.update(probe.__dict__)
    sqlite.upsert_hold(chat_id, draft.to_dict())
    sqlite.save_draft(chat_id, draft.to_dict(), status="active", current_step=draft.next_step())
    logger.info(
        "WAITING_PAYMENT_VARIANT_CHANGED_RECONFIRM_REQUIRED chat_id=%s booking_id=%s old_variant=%s new_variant=%s",
        chat_id,
        old_booking_id,
        old_draft.service_variant,
        canonical_variant,
    )
    return _llm_reply_from_engine_context(
        draft=draft,
        chat_id=chat_id,
        user_text=text,
        history=history,
        today=today,
        event="waiting_payment_booking_changed_requires_confirmation",
        data={
            "old_variant": old_draft.service_variant,
            "new_variant": canonical_variant,
            "old_payment_link_must_not_be_used": True,
            "booking": _booking_reply_data(draft),
        },
        fallback=None,
    )


def _fix_waiting_payment_reply_payment_link(reply: str, draft: BookingDraft) -> str:
    if not reply:
        return reply

    result = reply

    if draft.payment_url:
        result = re.sub(
            r"Ссылка\s+для\s+оплаты\s*:\s*оплатить\.?",
            str(draft.payment_url),
            result,
            flags=re.I,
        )
        result = re.sub(
            r"Для\s+подтверждения\s+перейдите\s+по\s+ссылке\s*:\s*оплатить\.?",
            str(draft.payment_url),
            result,
            flags=re.I,
        )

    result = re.sub(r"₽\s*₽", "₽", result)
    return result.strip()


def _guard_waiting_payment_confirmation(
    reply: str,
    draft: BookingDraft,
    *,
    chat_id: str,
    user_text: str,
    history: list[dict[str, Any]],
    today: str,
) -> str:
    """Do not let a conversational reply impersonate a YooKassa callback.

    A customer's statement that payment was made is not payment evidence. Only
    the payment synchronization loop may change the booking status to booked and
    send the actual confirmation, post-payment instructions and admin alert.
    """
    if draft.status != "waiting_payment":
        return reply

    normalized = (reply or "").lower().replace("ё", "е")
    false_confirmation_markers = (
        "оплата прошла успешно",
        "оплата подтверждена",
        "оплату получила",
        "платеж подтвержден",
        "платеж прошел",
        "бронь подтверждена",
        "бронирование подтверждено",
        "ждем вас",
    )
    if not any(marker in normalized for marker in false_confirmation_markers):
        return reply

    logger.warning(
        "WAITING_PAYMENT_FALSE_CONFIRMATION_BLOCKED payment_id=%s reply=%r",
        draft.payment_id,
        reply,
    )
    return _llm_reply_from_engine_context(
        draft=draft, chat_id=chat_id, user_text=user_text, history=history, today=today,
        event="waiting_payment_false_confirmation_repair",
        data={
            "payment_status": "waiting_payment",
            "existing_reply_to_replace": reply,
            "rules": ["do_not_claim_payment_succeeded", "say_payment_status_is_being_checked"],
        },
        fallback=None,
    )

def _handle_waiting_payment_dialog(
    chat_id: str,
    text: str,
    draft: BookingDraft,
    *,
    history: list[dict[str, Any]],
    today: str,
    platform: str | None = "max",
) -> str | None:
    """Keep conversation alive while a prepared booking is waiting for payment.

    Payment stage is not a modal lock. The client may ask questions, refuse online
    payment, ask for the payment link again, or discuss details. Do not clear
    payment_id/payment_url and do not return to upsell/contact collection only
    because the message is not a positive confirmation.
    """
    if draft.status != "waiting_payment":
        return None

    # Preserve the pending payment state unless the user explicitly resets the
    # whole dialog with /start. A non-positive message like «я не хочу оплачивать»
    # must not be interpreted as refusing extras.
    draft.status = "waiting_payment"

    # A pending payment belongs to one concrete booking. If the customer starts
    # discussing another object, keep the old payment in the bookings table and
    # open a clean draft for a separate booking. Otherwise the answer model can
    # describe the second object while the engine still carries the first
    # booking's payment_id/payment_url.
    additional_service_type = _waiting_payment_additional_service_type(text, draft)
    if additional_service_type:
        previous = BookingDraft.from_dict(draft.to_dict())
        next_draft = _fresh_dialog_draft()
        next_draft.service_type = additional_service_type
        next_draft.date = previous.date
        next_draft.client_name = previous.client_name
        next_draft.phone = previous.phone
        next_draft.status = "active"
        draft.__dict__.update(next_draft.__dict__)
        history.clear()
        sqlite.save_draft(chat_id, draft.to_dict(), status="active", current_step=draft.next_step())
        logger.info(
            "WAITING_PAYMENT_SECOND_BOOKING_STARTED chat_id=%s previous_service=%s new_service=%s previous_payment_id=%s",
            chat_id,
            previous.service_type,
            additional_service_type,
            previous.payment_id,
        )
        return None

    new_variant = contextual_gazebo_variant_from_text(text)
    if (
        draft.service_type == "gazebo"
        and new_variant
        and new_variant != draft.service_variant
    ):
        return sanitize_reply(_waiting_payment_variant_change_reply(
            chat_id,
            text,
            draft,
            new_variant,
            history=history,
            today=today,
        ))

    if _looks_like_waiting_payment_date_change(text, draft):
        reschedule_reply = _waiting_payment_reschedule_reply(
            chat_id,
            text,
            draft,
            history=history,
            today=today,
            platform=platform,
        )
        if reschedule_reply is not None:
            return sanitize_reply(reschedule_reply, fallback=_payment_pending_fallback_reply(draft, text))

    if _looks_like_later_payment(text):
        sqlite.save_draft(chat_id, draft.to_dict(), status="waiting_payment", current_step=None)
        return _llm_reply_from_engine_context(
            draft=draft, chat_id=chat_id, user_text=text, history=history, today=today,
            event="payment_deferred_by_customer",
            data={"payment_link_ttl_minutes_approx": 60, "payment_status": "waiting_payment"},
            fallback=None,
        )

    if _looks_like_payment_link_refresh_request(text):
        return _refresh_waiting_payment_link(chat_id, draft, platform=platform)

    fallback = _payment_pending_fallback_reply(draft, text)
    reply = _llm_reply_from_engine_context(
        draft=draft,
        chat_id=chat_id,
        user_text=text,
        history=history,
        today=today,
        event="waiting_payment_dialog",
        data={
            "payment_url": draft.payment_url,
            "payment_id": draft.payment_id,
            "service_type": draft.service_type,
            "service_variant": draft.service_variant,
            "date": draft.date,
            "time": draft.time,
            "duration": draft.duration,
            "guests_count": draft.guests_count,
            "event_format": draft.event_format,
            "client_name": draft.client_name,
            "phone": draft.phone,
            "rules": [
                "do_not_clear_payment_state",
                "do_not_return_to_upsell_or_contacts",
                "answer_customer_question_normally",
                "if_payment_refused_explain_booking_is_not_confirmed_without_prepayment",
                "customer_words_are_not_payment_confirmation",
                "never_claim_payment_succeeded_while_status_is_waiting_payment",
                "tell_customer_payment_is_being_checked",
            ],
        },
        fallback=fallback,
    )
    clean_reply = sanitize_reply(reply, fallback=fallback)
    clean_reply = _fix_waiting_payment_reply_payment_link(clean_reply, draft)
    clean_reply = _guard_waiting_payment_confirmation(
        clean_reply, draft, chat_id=chat_id, user_text=text, history=history, today=today,
    )
    if draft.payment_url and draft.payment_url in clean_reply:
        _queue_booking_object_photo(chat_id, draft)
    sqlite.save_draft(chat_id, draft.to_dict(), status="waiting_payment", current_step=None)
    return clean_reply


_CONFIRMED_BOOKING_STATUSES = {"booked", "rescheduled", "paid_needs_manual_review", "paid_yclients_error"}


def _looks_like_price_question(text: str) -> bool:
    lowered = (text or "").lower().replace("ё", "е")
    return any(marker in lowered for marker in (
        "цена", "стоимость", "сколько стоит", "скок стоит", "по чем", "почем",
        "будний", "будни", "выходн", "50 процентов", "50%", "прайс", "тариф",
    ))


def _confirmed_booking_fallback_reply(draft: BookingDraft, user_text: str) -> str:
    return ""


def _handle_confirmed_booking_dialog(chat_id: str, text: str, draft: BookingDraft, *, history: list[dict[str, Any]], today: str) -> str | None:
    """Keep the dialog alive after payment/booking confirmation.

    A paid booking is not the end of the conversation. The customer can ask about
    price, arrival, extras, cancellation or reschedule. Cancel/reschedule are still
    handled by `_maybe_start_booking_operation`; this handler covers normal questions
    so the bot does not answer with a useless «Чем могу помочь?».
    """
    if draft.status not in _CONFIRMED_BOOKING_STATUSES:
        return None

    # After payment, questions like “а когда свободен?” usually continue a
    # reschedule discussion. Keep this deterministic and save pending_action, so
    # the next short date reply (“1 июля тогда”) is treated as a reschedule date,
    # not as a fresh booking/question.
    if _looks_like_reschedule_free_dates_question(text) and draft.service_type:
        booking, _select_reply = _select_booking_for_operation(chat_id, {}, operation="reschedule", current_draft=draft)
        booking_draft = BookingDraft.from_dict(json.loads(booking["draft_json"])) if booking else draft
        dates = _available_dates_for_service(
            booking_draft.service_type,
            limit=10,
            current_booking=booking_draft,
            chat_id=chat_id,
        )
        draft.last_offered_dates = dates[:20]
        draft.last_offered_service_type = booking_draft.service_type
        draft.last_offered_object_title = booking_draft.service_variant or _OBJECT_TITLE_BY_SERVICE.get(booking_draft.service_type or "") or service_title(booking_draft.service_type)
        if booking:
            draft.pending_action = {"type": "await_reschedule_date", "booking_id": int(booking["id"])}
        sqlite.save_draft(chat_id, draft.to_dict(), status=draft.status, current_step=None)
        return sanitize_reply(_llm_reply_from_engine_context(
            draft=draft, chat_id=chat_id, user_text=text, history=history, today=today,
            event="reschedule_available_dates",
            data={"available_dates": dates, "booking": _booking_reply_data(booking_draft)},
            fallback=None,
        ))

    fallback = _confirmed_booking_fallback_reply(draft, text)
    price = calculate_booking_price(draft)
    reply = _llm_reply_from_engine_context(
        draft=draft,
        chat_id=chat_id,
        user_text=text,
        history=history,
        today=today,
        event="confirmed_booking_dialog",
        data={
            "booking_status": draft.status,
            "service_type": draft.service_type,
            "service_variant": draft.service_variant,
            "date": draft.date,
            "time": draft.time,
            "duration": draft.duration,
            "guests_count": draft.guests_count,
            "event_format": draft.event_format,
            "price_rub": price,
            "client_name": draft.client_name,
            "phone": draft.phone,
            "yclients_record_id": draft.yclients_record_id,
            "rules": [
                "booking_is_already_confirmed",
                "answer_customer_question_normally",
                "do_not_restart_new_booking_flow",
                "do_not_ask_for_contacts_or_upsells_again",
                "do_not_reply_with_generic_how_can_i_help",
            ],
        },
        fallback=fallback,
    )
    sqlite.save_draft(chat_id, draft.to_dict(), status=draft.status, current_step=None)
    return sanitize_reply(reply, fallback=fallback)


class DialogBlocked(Exception):
    pass


def _mentions_unsupported_location(text: str) -> bool:
    normalized = (text or "").lower().replace("ё", "е")
    return bool(
        re.search(r"\bрусал(?:к|очк)[аиуеойы]?\b", normalized)
        or "беленьк" in normalized and "песоч" in normalized
    )


def _fresh_dialog_draft() -> BookingDraft:
    return BookingDraft(context_started_at=datetime.utcnow().isoformat())


def _looks_like_new_booking_cycle(text: str) -> bool:
    lowered = (text or "").lower().replace("ё", "е")
    if "забронирован" in lowered and any(
        marker in lowered for marker in ("сколько", "на сколько", "до скольки", "когда", "кем")
    ):
        return False
    if "переоформ" in lowered:
        return True
    request_markers = (
        "свобод", "заброни", "забронировать", "оформить брон", "новая брон",
        "новую брон", "хочу бесед", "хочу бан", "хочу дом", "подберите",
    )
    object_markers = (
        "бесед", "бан", "дом", "гостев", "объект", "вариант", "дат", "июл",
        "август", "сентябр", "октябр", "ноябр", "декабр", "январ", "феврал",
        "март", "апрел", "мая", "июн",
    )
    return any(marker in lowered for marker in request_markers) and any(
        marker in lowered for marker in object_markers
    )


def _looks_like_discard_canceled_draft(text: str) -> bool:
    lowered = (text or "").lower().replace("ё", "е")
    return any(marker in lowered for marker in (
        "убрать брон", "убер", "удалить брон", "удали брон", "отменить заявку",
        "отмени заявку", "не нужна брон", "не надо брон", "отказаться от заявки",
    ))


def _canceled_draft_matches_real_booking(chat_id: str, draft: BookingDraft) -> bool:
    try:
        rows = _actual_booking_rows(chat_id)
        return _find_row_matching_draft(rows, draft) is not None
    except Exception:
        logger.exception("CANCELED_DRAFT_REAL_BOOKING_CHECK_FAILED chat_id=%s", chat_id)
        return False


def _refresh_canceled_payment_link(
    chat_id: str,
    draft: BookingDraft,
    *,
    platform: str | None,
    user_text: str,
    history: list[dict[str, Any]],
    today: str,
) -> str:
    if _canceled_draft_matches_real_booking(chat_id, draft):
        return _llm_reply_from_engine_context(
            draft=draft, chat_id=chat_id, user_text=user_text, history=history, today=today,
            event="confirmed_booking_payment_link_not_needed",
            data={"booking": _booking_reply_data(draft)}, fallback=None,
        )

    # Never reuse an expired YooKassa payment. A successful retry is a new
    # booking/payment attempt; the canceled row remains unchanged for audit.
    draft.payment_id = None
    draft.payment_url = None
    draft.status = "active"
    draft.block_reason = None
    draft.blocked_until = None
    draft.pending_action = {}

    if not (_has_booking_core_fields(draft) and _has_client_contacts(draft) and draft.upsell_done):
        sqlite.save_draft(
            chat_id,
            draft.to_dict(),
            status="active",
            current_step=draft.next_step(),
        )
        return _llm_reply_from_engine_context(
            draft=draft, chat_id=chat_id, user_text=user_text, history=history, today=today,
            event="canceled_payment_requires_booking_details",
            data={"booking": _booking_reply_data(draft), "next_step": draft.next_step()}, fallback=None,
        )

    return _create_payment_or_admin_handoff(chat_id, draft, platform=platform)


def handle_text(chat_id: str, user_name: str, text: str, *, platform: str | None = None) -> str:
    logger.info("=== JSON_ENGINE HANDLE_TEXT START === chat_id=%s text=%r", chat_id, text)

    draft = BookingDraft.from_dict(sqlite.load_draft(chat_id))
    logger.info("DRAFT BEFORE: %s", _draft_log_line(draft))

    if _is_hard_reset(text):
        draft = _fresh_dialog_draft()
        sqlite.save_draft(chat_id, draft.to_dict(), status="active", current_step=draft.next_step())
        return sanitize_reply(_llm_reply_from_engine_context(
            draft=draft,
            chat_id=chat_id,
            user_text=text,
            history=[],
            today=now_local().date().isoformat(),
            event="restart_dialog",
            data={},
            fallback=None,
        ))

    restarted_after_canceled = False
    if draft.status == "payment_canceled":
        explicit_operation = (
            _is_explicit_booking_management_request(text, operation="cancel", params={})
            or _is_explicit_booking_management_request(text, operation="reschedule", params={})
        )
        has_real_booking = False
        if explicit_operation:
            try:
                has_real_booking = bool(_actual_booking_rows(chat_id))
            except Exception:
                logger.exception("CANCELED_STATE_ACTIVE_BOOKING_CHECK_FAILED chat_id=%s", chat_id)

        if _looks_like_payment_link_refresh_request(text):
            canceled_history = _recent_history(chat_id, draft)
            canceled_today = now_local().date().isoformat()
            return sanitize_reply(_refresh_canceled_payment_link(
                chat_id, draft, platform=platform, user_text=text,
                history=canceled_history, today=canceled_today,
            ))

        if _looks_like_discard_canceled_draft(text) and not has_real_booking:
            draft = _fresh_dialog_draft()
            sqlite.release_holds(chat_id)
            sqlite.save_draft(chat_id, draft.to_dict(), status="active", current_step=draft.next_step())
            return sanitize_reply(_llm_reply_from_engine_context(
                draft=draft, chat_id=chat_id, user_text=text, history=[],
                today=now_local().date().isoformat(), event="unpaid_draft_discarded",
                data={"booking": _booking_reply_data(draft)}, fallback=None,
            ))

        if _looks_like_new_booking_cycle(text) and not (explicit_operation and has_real_booking):
            logger.info("PAYMENT_CANCELED_NEW_DIALOG_RESET chat_id=%s text=%r", chat_id, text)
            sqlite.release_holds(chat_id)
            draft = _fresh_dialog_draft()
            sqlite.save_draft(chat_id, draft.to_dict(), status="active", current_step=draft.next_step())
            restarted_after_canceled = True

        if draft.status == "payment_canceled" and not (explicit_operation and has_real_booking):
            sqlite.save_draft(chat_id, draft.to_dict(), status="payment_canceled", current_step=None)
            return sanitize_reply(_llm_reply_from_engine_context(
                draft=draft, chat_id=chat_id, user_text=text,
                history=_recent_history(chat_id, draft), today=now_local().date().isoformat(),
                event="payment_canceled_dialog",
                data={"booking": _booking_reply_data(draft)}, fallback=None,
            ))

    history = [] if restarted_after_canceled else _recent_history(chat_id, draft)
    today = now_local().date().isoformat()

    normalized_text = _normalize_user_text_for_dialog(text)

    if _mentions_unsupported_location(text):
        sqlite.save_draft(chat_id, draft.to_dict(), status=draft.status or "active", current_step=draft.next_step())
        return sanitize_reply(_llm_reply_from_engine_context(
            draft=draft,
            chat_id=chat_id,
            user_text=text,
            history=history,
            today=today,
            event="unsupported_location",
            data={
                "unsupported_location_name": "пляж Русалка (Беленький песочек)",
                "supported_location_name": "пляж Максима Горького",
                "booking_url": "https://vk.com/gostevoydomvyksa",
                "rules": [
                    "do_not_list_objects",
                    "do_not_check_availability",
                    "do_not_send_media",
                    "do_not_continue_booking_flow",
                    "do_not_ask_time_or_duration",
                ],
            },
            fallback=None,
        ))

    if _explicitly_asks_for_photo(text):
        media_titles = _media_titles_from_recent_context(draft, history)
        if media_titles:
            _store_requested_media(chat_id, media_titles)
            return sanitize_reply(_llm_reply_from_engine_context(
                draft=draft,
                chat_id=chat_id,
                user_text=text,
                history=history,
                today=today,
                event="photo_request",
                data={
                    "requested_media": media_titles,
                    "do_not_check_availability": True,
                    "do_not_change_booking": True,
                },
                fallback=None,
            ))

        return sanitize_reply(_llm_reply_from_engine_context(
            draft=draft,
            chat_id=chat_id,
            user_text=text,
            history=history,
            today=today,
            event="photo_request_no_object_context",
            data={
                "do_not_check_availability": True,
                "do_not_change_booking": True,
            },
            fallback=None,
        ))

    if draft.phone and not _is_valid_phone(draft.phone):
        draft.phone = None
        draft.status = "active"
        sqlite.save_draft(chat_id, draft.to_dict(), status="active", current_step=draft.next_step())
        return sanitize_reply(_llm_reply_from_engine_context(
            draft=draft,
            chat_id=chat_id,
            user_text=text,
            history=history,
            today=today,
            event="invalid_phone",
            data={"phone_digits_required": 11, "next_step": "phone"},
            fallback=None,
        ))

    if draft.phone and _is_valid_phone(draft.phone):
        draft.phone = _format_phone(draft.phone)


    # A date/time refinement is a new booking choice, never consent to an older
    # notification offer. Clear it before the small classifier sees a bare date.
    if (
        (draft.pending_action or {}).get("type") == "watchlist_create"
        and (
            _looks_like_date_refinement_request(normalized_text, draft)
            or bool(_extract_ru_dates_from_text(normalized_text))
            or _looks_like_new_time_choice(normalized_text, draft)
        )
        and not ("уведом" in normalized_text and _looks_positive(normalized_text))
    ):
        logger.info("WATCHLIST_PENDING_CLEARED_BY_NEW_BOOKING_CHOICE text=%r", text)
        draft.pending_action = {}

    # Watchlist accept/decline is classified by a small LLM, while the engine
    # performs side effects. This prevents broad hardcoded phrase matching from
    # treating messages like «на 16:00, а есть скидки?» as notification consent.
    if _watchlist_enabled():
        watchlist_reply = _handle_watchlist_llm_turn(
            chat_id=chat_id,
            text=text,
            draft=draft,
            history=history,
            today=today,
            platform=platform,
        )
        if watchlist_reply is not None:
            return sanitize_reply(watchlist_reply)

    active_watchlist_reply = _handle_active_watchlist_followup(
        text, draft, chat_id=chat_id, history=history, today=today,
    )
    if active_watchlist_reply is not None:
        sqlite.save_draft(chat_id, draft.to_dict(), status="active", current_step=draft.next_step())
        return sanitize_reply(active_watchlist_reply)

    # A short yes/no is not a value for data-collection steps. Confirmation,
    # watchlist and upsell states are handled above and intentionally excluded.
    value_step = draft.next_step()
    if value_step in {
        "service_type", "date", "service_variant", "time", "duration",
        "guests_count", "client_name", "phone",
    } and is_bare_non_value_reply(text):
        sqlite.save_draft(chat_id, draft.to_dict(), status="active", current_step=value_step)
        return sanitize_reply(_llm_reply_from_engine_context(
            draft=draft,
            chat_id=chat_id,
            user_text=text,
            history=history,
            today=today,
            event="required_field_not_provided",
            data={"next_step": value_step, "booking": _booking_reply_data(draft)},
            fallback=None,
        ))

    upsell_refusal_reply = None
    if (
        draft.next_step() == "upsell_items"
        and not _looks_like_payment_refusal(normalized_text)
    ):
        upsell_refusal_reply = _handle_specific_upsell_refusal_before_llm(
            chat_id,
            draft,
            history,
            normalized_text,
        )

    if upsell_refusal_reply:
        return sanitize_reply(upsell_refusal_reply, fallback=_fallback_question(draft))

    # If client refuses extras after contacts/core booking are already collected,
    # do not ask name/phone again and do not confirm booking before payment.
    if (
        draft.next_step() == "upsell_items"
        and int(draft.upsell_offer_count or 0) >= 2
        and not _looks_like_payment_refusal(normalized_text)
        and _looks_like_general_upsell_refusal(normalized_text)
        and _has_booking_core_fields(draft)
    ):
        draft.upsell_items = []
        draft.upsell_done = True
        draft.upsell_offer_count = max(int(draft.upsell_offer_count or 0), 2)

        sqlite.save_draft(chat_id, draft.to_dict(), status="active", current_step=draft.next_step())

        return sanitize_reply(_llm_reply_from_engine_context(
            draft=draft,
            chat_id=chat_id,
            user_text=text,
            history=history,
            today=today,
            event=(
                "booking_ready_for_confirmation"
                if _has_client_contacts(draft)
                else "ask_missing_contacts_after_upsell_closed"
            ),
            data={"booking": _booking_reply_data(draft), "next_step": draft.next_step()},
            fallback=None,
        ))

    waiting_payment_reply = _handle_waiting_payment_dialog(
        chat_id,
        text,
        draft,
        history=history,
        today=today,
        platform=platform,
    )
    if waiting_payment_reply is not None:
        logger.info("WAITING_PAYMENT_DIALOG_HANDLED chat_id=%s status=%s payment_id=%s", chat_id, draft.status, draft.payment_id)
        return waiting_payment_reply

    # Booking operations must not depend on the LLM deciding an action.
    # If the client clearly asks to move/cancel an existing paid booking, start the
    # operation state here and keep using the current real booking instead of
    # exposing old/stale rows from PostgreSQL.
    booking_op_reply = _maybe_start_booking_operation(chat_id, text, draft, history=history, today=today)
    if booking_op_reply is not None:
        sqlite.save_draft(chat_id, draft.to_dict(), status=draft.status or "active", current_step=draft.next_step())
        return sanitize_reply(booking_op_reply)

    # Confirmation of a pending cancel/reschedule must run before the generic
    # confirmed-booking chat handler. Otherwise a short "да" is treated as a
    # normal question and the actual YClients operation is never executed.
    if (draft.pending_action or {}).get("type") in {"cancel_booking", "reschedule_booking", "await_reschedule_date"}:
        pending_reply = _handle_pending_action(chat_id, text, draft)
        if pending_reply is not None:
            sqlite.save_draft(chat_id, draft.to_dict(), status=draft.status or "active", current_step=draft.next_step())
            return sanitize_reply(pending_reply)

    confirmed_booking_reply = _handle_confirmed_booking_dialog(chat_id, text, draft, history=history, today=today)
    if confirmed_booking_reply is not None:
        logger.info("CONFIRMED_BOOKING_DIALOG_HANDLED chat_id=%s status=%s record_id=%s", chat_id, draft.status, draft.yclients_record_id)
        return confirmed_booking_reply

    # If a selected time failed live availability, keep the client inside the current
    # new-booking flow. This must run BEFORE pending_action handling: otherwise a
    # phrase like «давайте с 14 до 22» can be mistaken for agreement to watchlist.
    if draft.block_reason == "slot_unavailable":
        if _looks_like_new_time_choice(text, draft):
            draft.block_reason = None
            draft.blocked_until = None
            if (draft.pending_action or {}).get("type") == "watchlist_create":
                draft.pending_action = {}
        elif _looks_like_slot_explanation_request(text):
            sqlite.save_draft(chat_id, draft.to_dict(), status="active", current_step=draft.next_step())
            return sanitize_reply(_llm_reply_from_engine_context(
                draft=draft,
                chat_id=chat_id,
                user_text=text,
                history=history,
                today=today,
                event="blocked_interval_explanation",
                data={
                    "booking": _booking_reply_data(draft),
                    "blocked_time": draft.blocked_until,
                    "privacy_rule": "do_not_disclose_other_booking_details",
                },
                fallback=None,
            ))
        else:
            # A rejected interval must not trap the whole dialog. A new date,
            # object or availability question belongs to the normal LLM flow.
            if (draft.pending_action or {}).get("type") == "watchlist_create":
                draft.pending_action = {}
            draft.block_reason = None
            draft.blocked_until = None

    if (draft.pending_action or {}).get("type") == "watchlist_create" and _looks_like_new_time_choice(text, draft):
        logger.info("WATCHLIST_PENDING_CLEARED_BY_TIME_CHOICE text=%r", text)
        draft.pending_action = {}

    if _explicitly_insists_on_current_unavailable_date(text, draft):
        logger.info("WATCHLIST_PENDING_CLIENT_INSISTS_ON_UNAVAILABLE_DATE text=%r", text)
        raw = (draft.pending_action or {}).get("candidate") or {}
        draft.service_type = raw.get("service_type") or draft.service_type
        draft.date = raw.get("date") or draft.date
        reply = _ensure_watchlist_offer_in_reply(_llm_reply_from_engine_context(
            draft=draft,
            chat_id=chat_id,
            user_text=text,
            history=history,
            today=today,
            event="availability_unavailable",
            data=_availability_event_data(draft),
            fallback=_fallback_question(draft),
        ), draft)
        decision_media = _filter_requested_media_for_customer([], reply=reply, before=draft, draft=draft, user_text=text)
        _store_requested_media(chat_id, decision_media)
        sqlite.save_draft(chat_id, draft.to_dict(), status="active", current_step=draft.next_step())
        return sanitize_reply(_sanitize_confirmed_before_payment_reply(
            reply, draft, chat_id=chat_id, user_text=text, history=history, today=today,
        ))

    pending_reply = _handle_pending_action(chat_id, text, draft)
    if pending_reply is not None:
        sqlite.save_draft(chat_id, draft.to_dict(), status=draft.status or "active", current_step=draft.next_step())
        return sanitize_reply(pending_reply)

    # Upsell is a controlled mini-flow. Client asked to do exactly two soft touches:
    # 1) after event format; 2) after a polite first refusal. A generic "нет спасибо"
    # must NOT immediately close extras, otherwise доп. продажа never happens.
    core_ready_for_upsell = bool(draft.service_type and draft.date and draft.time and draft.duration and draft.guests_count)
    last_bot_offered_upsell = any(
        item.get("sender") != "user" and _reply_mentions_upsell_for_engine(str(item.get("text") or ""))
        for item in (history or [])[-4:]
    )
    if (
        core_ready_for_upsell
        and draft.next_step() == "upsell_items"
        and _looks_like_upsell_deferred_or_declined(text)
    ):
        draft.upsell_offer_count = 2
        draft.upsell_done = True
        sqlite.save_draft(chat_id, draft.to_dict(), status="active", current_step=draft.next_step())
        return sanitize_reply(_llm_reply_from_engine_context(
            draft=draft,
            chat_id=chat_id,
            user_text=text,
            history=history,
            today=today,
            event="ask_missing_contacts_after_upsell_closed",
            data={"booking": _booking_reply_data(draft), "next_step": draft.next_step()},
            fallback=None,
        ))
    if (
        core_ready_for_upsell
        and draft.next_step() == "upsell_items"
        and _looks_like_upsell_refusal(text)
        and not _looks_like_payment_refusal(text)
    ):
        current_count = int(draft.upsell_offer_count or 0)
        if current_count <= 0 and last_bot_offered_upsell:
            current_count = 1
            draft.upsell_offer_count = 1
            draft.upsell_done = False
        if current_count <= 1:
            # Critical final-day guard: do not call the LLM here. Under 429/fallback it
            # sometimes closes upsells after the first refusal. The owner explicitly
            # wants a second short touch, so make this state transition deterministic.
            draft.upsell_offer_count = 2
            draft.upsell_done = False
            sqlite.save_draft(chat_id, draft.to_dict(), status="active", current_step=draft.next_step())
            return sanitize_reply(_llm_reply_from_engine_context(
                draft=draft,
                chat_id=chat_id,
                user_text=text,
                history=history,
                today=today,
                event="upsell_second_offer",
                data={"booking": _booking_reply_data(draft), "offer_count": 2},
                fallback=None,
            ))
        draft.upsell_offer_count = 2
        draft.upsell_done = True
        sqlite.save_draft(chat_id, draft.to_dict(), status="active", current_step=draft.next_step())
        return sanitize_reply(_llm_reply_from_engine_context(
            draft=draft,
            chat_id=chat_id,
            user_text=text,
            history=history,
            today=today,
            event="ask_missing_contacts_after_upsell_closed",
            data={"booking": _booking_reply_data(draft), "next_step": draft.next_step()},
            fallback=None,
        ))

    # If the second upsell was already made and the client sends contact details
    # instead of explicitly saying no, treat that as declining extras and proceed.
    if draft.next_step() == "upsell_items" and int(draft.upsell_offer_count or 0) >= 2 and _message_has_contact_data(text):
        draft.upsell_done = True

    if draft.ready_for_confirmation():
        if _is_payment_request(text) or is_positive_confirmation(text, draft):
            return _create_payment_or_admin_handoff(chat_id, draft, platform=platform)

    # Emergency guard: if the bot already asked for final confirmation and the
    # client agrees, never let the answer model say “confirmed” without creating
    # a payment link. This is worse than skipping a missed second upsell touch.
    if _booking_core_with_contacts_ready(draft) and is_positive_confirmation(text, draft) and _last_bot_asked_confirmation(history):
        draft.upsell_done = True
        if int(draft.upsell_offer_count or 0) < 2:
            draft.upsell_offer_count = 2
        return _create_payment_or_admin_handoff(chat_id, draft, platform=platform)

    llm_text = normalized_text
    explicit_date_override = _extract_explicit_day_date(normalized_text) if _looks_like_date_change_for_current_service(normalized_text, draft) else None
    if explicit_date_override:
        llm_text = (
            f"{text}\n\n"
            f"СТРУКТУРНЫЙ КОНТЕКСТ: клиент уточняет именно дату {explicit_date_override} для уже выбранного объекта. "
            "Это НЕ время 16:00 и НЕ длительность. Сохрани текущий service_type, очисти time/duration если нужно, "
            "верни date равным этой дате и продолжай бронирование этого же объекта."
        )
    elif _looks_like_date_refinement_request(normalized_text, draft):
        llm_text = (
            f"{normalized_text}\n\n"
            "СТРУКТУРНЫЙ КОНТЕКСТ: клиент не отменяет бронь и не меняет объект; "
            "он просит другие даты для уже выбранного объекта. "
            "Сохрани текущий service_type и верни intent=date_refinement."
        )
    elif draft.service_type and any(word in normalized_text.lower() for word in ["не обязательно", "другой вариант", "другие", "не только", "или"]):
        llm_text = f"{normalized_text} (клиент не хочет {draft.service_type}, предложи другие типы объектов: дом, баня, тёплая беседка)"

    decision = decide(llm_text, draft, today=today, history=history)

    additional_service_type = _requested_additional_service_type(text, draft)
    if additional_service_type:
        logger.info(
            "ADDITIONAL_SERVICE_SPLIT_REQUIRED chat_id=%s current=%s additional=%s text=%r",
            chat_id,
            draft.service_type,
            additional_service_type,
            text,
        )
        reply = _llm_reply_from_engine_context(
            draft=draft,
            chat_id=chat_id,
            user_text=text,
            history=history,
            today=today,
            event="multiple_objects_require_separate_bookings",
            data={
                "current_booking": _booking_reply_data(draft),
                "additional_service_type": additional_service_type,
                "additional_object_title": service_title(additional_service_type),
                "rule": "one_draft_one_object_separate_availability_and_payment_ask_which_booking_to_complete_first",
            },
            fallback=None,
        )
        sqlite.save_draft(chat_id, draft.to_dict(), status="active", current_step=draft.next_step())
        return sanitize_reply(reply)

    # Не создаём watchlist только когда он выключен настройкой.
    if (
        not _watchlist_enabled()
        and getattr(getattr(decision, "action", None), "type", None)
        == "offer_watchlist"
    ):
        decision.action.type = "none"
        decision.action.params = {}
        draft.pending_action = {}

    if getattr(decision, "reply", None):
        decision.reply = _remove_watchlist_offer_text(decision.reply)

    transition = validate_transition_patch(
        draft,
        decision.fields_patch,
        user_text=text,
        intent=getattr(decision, "intent", None),
    )
    rejected_expected_step = (
        draft.next_step()
        if draft.next_step() in transition.rejected
        else None
    )
    if transition.rejected:
        logger.warning(
            "LLM_TRANSITION_FIELDS_REJECTED chat_id=%s step=%s rejected=%s text=%r",
            chat_id,
            draft.next_step(),
            transition.rejected,
            text,
        )
    decision.fields_patch = transition.patch

    if (
        draft.next_step() in transition.rejected
        and _reply_claims_unavailable(getattr(decision, "reply", None))
    ):
        decision.reply = ""

    if draft.service_type == "bathhouse":
        recovered_duration = (
            _duration_from_text_value(text)
            or _duration_from_recent_history(history)
            or _duration_from_chat_messages(chat_id)
            or _duration_from_text_value(getattr(draft, "service_variant", None))
        )
        if recovered_duration:
            try:
                if not isinstance(decision.fields_patch, dict):
                    decision.fields_patch = {}
                if not decision.fields_patch.get("duration"):
                    decision.fields_patch["duration"] = recovered_duration
                logger.info(
                    "BATHHOUSE_DURATION_FORCED_INTO_PATCH chat_id=%s duration=%s patch=%s",
                    chat_id,
                    recovered_duration,
                    decision.fields_patch,
                )
            except Exception:
                logger.exception("BATHHOUSE_DURATION_FORCE_PATCH_FAILED chat_id=%s", chat_id)

    action_reply = _handle_decision_action(chat_id, decision, draft, user_text=text, platform=platform)
    if action_reply is not None:
        decision.requested_media = _filter_requested_media_for_customer(
            decision.requested_media,
            reply=action_reply,
            before=draft,
            draft=draft,
            user_text=text,
        )
        _store_requested_media(chat_id, decision.requested_media)
        sqlite.save_draft(chat_id, draft.to_dict(), status="active", current_step=draft.next_step())
        return sanitize_reply(action_reply)

    before_patch = BookingDraft.from_dict(draft.to_dict())
    _apply_patch(draft, decision.fields_patch)

    if draft.service_type == "bathhouse" and not draft.duration:
        _recover_bathhouse_duration_if_missing(chat_id, draft, history=history, user_text=text)
    _restore_lost_core_fields(draft, before_patch, user_text=text, history=history)

    # Structured state reconciliation.
    # Do not inspect user's words. Trust the LLM's structured missing_fields:
    # if only contacts remain, the upsell stage is closed.
    if _close_upsell_if_llm_moved_to_contacts(draft, decision):
        logger.info("UPSELL_CLOSED_BY_STRUCTURED_DECISION chat_id=%s next=%s", chat_id, draft.next_step())

    # Business-state gate:
    # When all booking fields are collected, the next step is always
    # summary -> client confirms -> payment link.
    # LLM must not finish/confirm booking before payment.
    if (
        _booking_ready_without_payment(draft)
        and not _is_confirm_or_payment_text(normalized_text)
        and not _is_side_question_turn(normalized_text, getattr(decision, "intent", None))
    ):
        sqlite.save_draft(chat_id, draft.to_dict(), status="active", current_step=draft.next_step())
        return sanitize_reply(_llm_reply_from_engine_context(
            draft=draft,
            chat_id=chat_id,
            user_text=text,
            history=history,
            today=today,
            event="booking_ready_for_confirmation",
            data={"booking": _booking_reply_data(draft), "payment_required": True},
            fallback=None,
        ))

    # If the client refuses only one extra after selecting other extras,
    # do not reset all selected extras.
    # Example: "розжиг, решётки, посуда" -> "кальян не надо"
    # means keep розжиг/решётки/посуда and only exclude кальян.
    if _looks_like_specific_addon_refusal(normalized_text):
        refused_keys = _specific_addon_refusal_keys(normalized_text)

        base_items = _normalize_addon_items(before_patch.upsell_items or [])
        if not base_items:
            base_items = _extract_recent_stable_upsells_from_history(history)

        for item in _extract_addon_items_from_text(normalized_text):
            if item not in base_items:
                base_items.append(item)

        if base_items:
            kept_items = [item for item in base_items if item not in refused_keys]
            draft.upsell_items = kept_items
            draft.upsell_done = True
            draft.upsell_offer_count = max(int(draft.upsell_offer_count or 0), 2)

            reply = _llm_reply_from_engine_context(
                draft=draft,
                chat_id=chat_id,
                user_text=text,
                history=history,
                today=today,
                event="specific_upsell_items_removed",
                data={
                    "removed_items": refused_keys,
                    "remaining_items": kept_items,
                    "booking": _booking_reply_data(draft),
                    "next_step": draft.next_step(),
                },
                fallback=None,
            )

            sqlite.save_draft(chat_id, draft.to_dict(), status="active", current_step=draft.next_step())
            return sanitize_reply(_sanitize_confirmed_before_payment_reply(
                reply, draft, chat_id=chat_id, user_text=text, history=history, today=today,
            ))

        # Race-safe branch: if the previous message with selected extras is still being processed,
        # do not answer with "ничего из допов не готовим" and do not wipe extras.
        reply = _llm_reply_from_engine_context(
            draft=draft,
            chat_id=chat_id,
            user_text=text,
            history=history,
            today=today,
            event="specific_upsell_refusal_without_stable_previous_selection",
            data={
                "removed_items": refused_keys,
                "preserve_unknown_previous_items": True,
                "booking": _booking_reply_data(draft),
                "next_step": draft.next_step(),
            },
            fallback=None,
        )
        return sanitize_reply(_sanitize_confirmed_before_payment_reply(
            reply, draft, chat_id=chat_id, user_text=text, history=history, today=today,
        ))
    if explicit_date_override:
        logger.info("DATE_OVERRIDE_CURRENT_SERVICE text=%r date=%s service_type=%s", text, explicit_date_override, before_patch.service_type)
        draft.service_type = before_patch.service_type or draft.service_type
        draft.service_variant = before_patch.service_variant or draft.service_variant
        draft.date = explicit_date_override
        draft.time = None
        # The client corrected the date, not duration. Do not keep an implicit/default duration.
        if not _text_contains_duration(text):
            draft.duration = None
        draft.pending_action = {}
        draft.block_reason = None
        draft.blocked_until = None
    range_time, range_duration = _derive_time_duration_from_range(text)
    if range_time:
        logger.info("TIME_RANGE_DERIVED text=%r time=%s duration=%s", text, range_time, range_duration)
        draft.time = range_time
        if range_duration:
            draft.duration = range_duration
    elif not draft.duration:
        end_time_duration = _derive_duration_from_end_time(text, draft.time or before_patch.time)
        if end_time_duration:
            draft.duration = end_time_duration

    explicit_time = _extract_time_from_text(text)
    if explicit_time and not range_time and draft.service_type and draft.date and (draft.duration or before_patch.duration):
        # BATHHOUSE_DURATION_RECOVERY_GUARD
        if draft.service_type == "bathhouse" and not draft.duration:
            _recover_bathhouse_duration_if_missing(chat_id, draft, history=locals().get("history"), user_text=str(locals().get("text") or locals().get("user_text") or ""))

        if not draft.duration and before_patch.duration:
            draft.duration = before_patch.duration
        # Critical: the LLM sometimes extracts time in predecision, then erases it
        # in final JSON and writes "занято" without deterministic checks. Restore
        # the user-provided time so _maybe_check_availability is the only source
        # of truth for slot availability.
        if not draft.time or _reply_claims_unavailable(getattr(decision, "reply", None)):
            logger.info("RESTORE_EXPLICIT_TIME_FROM_TEXT text=%r time=%s old_time=%s", text, explicit_time, draft.time)
            draft.time = explicit_time
            if _reply_claims_unavailable(getattr(decision, "reply", None)):
                decision.reply = ""
                if getattr(decision, "action", None) and decision.action.type == "offer_watchlist":
                    decision.action.type = "none"
                    decision.action.params = {}
                    draft.pending_action = {}
    # Do not let the LLM silently default bathhouse duration to 3 hours when
    # the client only said "баню на завтра". At the same time, NEVER erase a
    # real duration that was already collected earlier: follow-up replies like
    # "в 17", "9", "отдых", "нет", "да" often return the previous duration in
    # LLM JSON, and older guard logic was wiping it, breaking slot holds and
    # watchlist exact checks.
    current_message_has_duration = (
        _text_contains_duration(normalized_text)
        or _text_is_bare_duration_answer(normalized_text, before_patch, draft)
        or range_duration is not None
    )
    if (
        draft.service_type == "bathhouse"
        and before_patch.duration is not None
        and draft.duration != before_patch.duration
        and not current_message_has_duration
    ):
        logger.info(
            "PRESERVE_EXISTING_BATHHOUSE_DURATION text=%r before=%s llm=%s",
            text, before_patch.duration, draft.duration,
        )
        draft.duration = before_patch.duration
    elif (
        draft.service_type == "bathhouse"
        and before_patch.duration is None
        and draft.duration is not None
        and not current_message_has_duration
        and str(draft.duration).strip() in {"3", "3.0"}
    ):
        logger.info("DROP_IMPLICIT_BATHHOUSE_DEFAULT_DURATION text=%r duration=%s", text, draft.duration)
        draft.duration = None

    duration_first_reply = _maybe_answer_bathhouse_date_availability(
        before=before_patch,
        draft=draft,
        chat_id=chat_id,
        user_text=text,
        history=history,
        today=today,
        current_message_has_duration=current_message_has_duration,
        decision_intent=getattr(decision, "intent", None),
    )
    if duration_first_reply:
        decision.requested_media = _filter_requested_media_for_customer(
            decision.requested_media,
            reply=duration_first_reply,
            before=before_patch,
            draft=draft,
            user_text=normalized_text,
        )
        _store_requested_media(chat_id, decision.requested_media)
        sqlite.save_draft(chat_id, draft.to_dict(), status="active", current_step=draft.next_step())
        logger.info("BATHHOUSE_DATE_AVAILABILITY_ANSWERED chat_id=%s draft=%s", chat_id, _draft_log_line(draft))
        return sanitize_reply(duration_first_reply, fallback=_fallback_question(draft))

    requested_warm_gazebo_time = _extract_time_from_text(text)
    if _apply_warm_gazebo_fixed_period(draft, text):
        reply = _llm_reply_from_engine_context(
            draft=draft,
            chat_id=chat_id,
            user_text=text,
            history=history,
            today=today,
            event="warm_gazebo_fixed_period_correction",
            data={
                "object_title": "Тёплая беседка",
                "date": draft.date,
                "arrival_time": "14:00",
                "checkout_time": "12:00",
                "checkout_day": "next_day",
                "requested_time": requested_warm_gazebo_time,
                "rules": ["explain_fixed_period", "do_not_claim_unavailable", "do_not_offer_watchlist", "ask_if_period_suits"],
            },
            fallback=None,
        )
        sqlite.save_draft(chat_id, draft.to_dict(), status="active", current_step=draft.next_step())
        return sanitize_reply(reply, fallback=_fallback_question(draft))

    if _text_explicitly_refuses_extras(normalized_text) and _booking_core_with_contacts_ready(draft):
        logger.info("ONE_SHOT_UPSELL_REFUSAL_APPLIED chat_id=%s text=%r", chat_id, text)
        draft.upsell_done = True
        if int(draft.upsell_offer_count or 0) < 2:
            draft.upsell_offer_count = 2

    if _should_engine_own_service_date_list(before_patch, draft, normalized_text):
        logger.info("ENGINE_OWNS_SERVICE_DATE_LIST service_type=%s", draft.service_type)
        reply = _engine_service_date_list_reply(draft=draft, chat_id=chat_id, user_text=normalized_text, history=history, today=today)
        decision.requested_media = _filter_requested_media_for_customer(
            decision.requested_media,
            reply=reply,
            before=before_patch,
            draft=draft,
            user_text=normalized_text,
        )
        _store_requested_media(chat_id, decision.requested_media)
        sqlite.save_draft(chat_id, draft.to_dict(), status="active", current_step=draft.next_step())
        logger.info("DRAFT SAVED: %s", _draft_log_line(draft))
        return sanitize_reply(_sanitize_confirmed_before_payment_reply(
            reply, draft, chat_id=chat_id, user_text=text, history=history, today=today,
        ))

    logger.info("DRAFT PROPOSED: %s", _draft_log_line(draft))
    proposed_snapshot = BookingDraft.from_dict(draft.to_dict())

    # Если модель распознала готовность к оплате уже после применения полей,
    # не даём ей увести клиента обратно в допы/разговоры.
    if draft.ready_for_confirmation() and (getattr(decision, "wants_payment", False) or decision.action.type == "create_payment" or _is_payment_request(text) or is_positive_confirmation(text, draft)):
        return _create_payment_or_admin_handoff(chat_id, draft, platform=platform)

    if _booking_core_with_contacts_ready(draft) and is_positive_confirmation(text, draft) and _last_bot_asked_confirmation(history):
        draft.upsell_done = True
        if int(draft.upsell_offer_count or 0) < 2:
            draft.upsell_offer_count = 2
        return _create_payment_or_admin_handoff(chat_id, draft, platform=platform)

    business_violation = _validate_business_rules(before_patch, draft)
    if business_violation:
        _restore_core_fields_from_snapshot(draft, proposed_snapshot, reason="before_save_after_draft_proposed")
        sqlite.save_draft(chat_id, draft.to_dict(), status="active", current_step=draft.next_step())
        return sanitize_reply(_llm_reply_from_engine_context(
            draft=draft,
            chat_id=chat_id,
            user_text=text,
            history=history,
            today=today,
            event=str(business_violation["event"]),
            data=dict(business_violation["data"]),
            fallback=None,
        ))

    before_availability_check = BookingDraft.from_dict(draft.to_dict())
    availability_reply = _maybe_check_availability(chat_id, before_patch, draft, user_text=text, history=history, today=today)
    _restore_core_fields_from_snapshot(draft, before_availability_check, reason="after_maybe_check_availability")
    if availability_reply:
        decision.requested_media = _filter_requested_media_for_customer(
            decision.requested_media,
            reply=availability_reply,
            before=before_patch,
            draft=draft,
            user_text=text,
        )
        _store_requested_media(chat_id, decision.requested_media)
        _restore_core_fields_from_snapshot(draft, proposed_snapshot, reason="before_save_after_draft_proposed")
        sqlite.save_draft(chat_id, draft.to_dict(), status="active", current_step=draft.next_step())
        if not _watchlist_enabled():
            availability_reply = _remove_watchlist_offer_text(availability_reply)
        return sanitize_reply(availability_reply, fallback=_fallback_question(draft))

    # Финальную сводку и переход к оплате формирует LLM.
    # Engine здесь не хардкодит текст заявки, чтобы не терять ответы на вопросы клиента
    # и не подменять цену, рассчитанную/переданную модели.
    reply = decision.reply or _fallback_question(draft)
    repaired_reply = _repair_false_unavailable_reply_after_live_check(
        reply,
        draft,
        chat_id=chat_id,
        user_text=text,
        history=history,
        today=today,
    )
    if repaired_reply is not None:
        reply = repaired_reply
        if getattr(decision, "action", None) and decision.action.type == "offer_watchlist":
            decision.action.type = "none"
            decision.action.params = {}
    reply = _canonicalize_price_in_reply(reply, draft)
    reply = _guard_duration_based_date_only_reply(
        reply, before_patch, draft,
        chat_id=chat_id, user_text=text, history=history, today=today,
    )
    reply = _ensure_next_step(reply, before_patch, draft)
    reply = _client_guard_reply(reply, user_text=text, before=before_patch, draft=draft, chat_id=chat_id, history=history, today=today)
    reply = _guard_duration_based_date_only_reply(
        reply, before_patch, draft,
        chat_id=chat_id, user_text=text, history=history, today=today,
    )
    unchanged_required_step = (
        before_patch.next_step()
        and before_patch.next_step() == draft.next_step()
        and draft.next_step() in {
            "service_type", "date", "service_variant", "time", "duration",
            "guests_count", "client_name", "phone",
        }
    )
    if (rejected_expected_step and draft.next_step() == rejected_expected_step) or unchanged_required_step:
        reply = _resume_rejected_step(
            reply, draft,
            chat_id=chat_id, user_text=text, history=history, today=today,
        )
    # These are final customer-safety filters and must run after every possible
    # LLM rewrite above. Catalog descriptions cannot override date availability,
    # and the bot must never resolve a data conflict by blaming an employee.
    reply = _guard_gazebo_recommendation_against_cache(
        reply, draft, user_text=text, chat_id=chat_id, history=history, today=today,
    )
    reply = _neutralize_staff_blame(
        reply, draft, user_text=text, chat_id=chat_id, history=history, today=today,
    )
    decision.requested_media = _filter_requested_media_for_customer(
        decision.requested_media,
        reply=reply,
        before=before_patch,
        draft=draft,
        user_text=text,
    )

    # При включённом watchlist сохраняем предложение уведомления.
    if not _watchlist_enabled():
        reply = _remove_watchlist_offer_text(reply)
    reply = sanitize_reply(reply, fallback=_fallback_question(draft))
    _store_requested_media(chat_id, decision.requested_media)
    _remember_offered_dates_from_reply(draft, reply, decision)
    _restore_core_fields_from_snapshot(draft, proposed_snapshot, reason="before_save_after_draft_proposed")
    sqlite.save_draft(chat_id, draft.to_dict(), status="active", current_step=draft.next_step())
    logger.info("DRAFT SAVED: %s", _draft_log_line(draft))
    return reply

_MONTHS_RU = {
    "января": 1,
    "февраля": 2,
    "марта": 3,
    "апреля": 4,
    "мая": 5,
    "июня": 6,
    "июля": 7,
    "августа": 8,
    "сентября": 9,
    "октября": 10,
    "ноября": 11,
    "декабря": 12,
}

_OBJECT_TITLE_BY_SERVICE = {
    "bathhouse": "Баня с бассейном",
    "house": "Гостевой дом",
    "warm_gazebo": "Теплая беседка",
}


def _remember_offered_dates_from_reply(draft: BookingDraft, reply: str, decision: AdminDecision) -> None:
    dates = _extract_ru_dates_from_text(reply)
    if not dates:
        return

    service_type = draft.service_type
    object_title = draft.service_variant or _OBJECT_TITLE_BY_SERVICE.get(service_type or "")

    # Если это общий список разных объектов, не затираем уже выбранный объект.
    # Для активной брони бани/дома/тёплой беседки сохраняем именно её контекст.
    draft.last_offered_dates = dates[:20]
    draft.last_offered_service_type = service_type
    draft.last_offered_object_title = object_title
    logger.info(
        "LAST_OFFERED_DATES service_type=%s object_title=%s dates=%s",
        draft.last_offered_service_type,
        draft.last_offered_object_title,
        draft.last_offered_dates,
    )


def _extract_ru_dates_from_text(text: str) -> list[str]:
    if not text:
        return []
    today = now_local().date()
    found: list[str] = []
    lowered = text.lower().replace("ё", "е")

    month_names = "|".join(_MONTHS_RU.keys())

    # Диапазоны вида "22–30 июня" / "22-30 июня".
    range_pattern = re.compile(rf"\b(\d{{1,2}})\s*(?:-|–|—)\s*(\d{{1,2}})\s+({month_names})")
    for match in range_pattern.finditer(lowered):
        start_day = int(match.group(1))
        end_day = int(match.group(2))
        month = _MONTHS_RU.get(match.group(3))
        if not month or start_day > end_day or end_day - start_day > 31:
            continue
        for day in range(start_day, end_day + 1):
            year = today.year
            try:
                candidate = datetime(year, month, day).date()
            except ValueError:
                continue
            if candidate < today and (today - candidate).days > 31:
                try:
                    candidate = datetime(year + 1, month, day).date()
                except ValueError:
                    continue
            iso = candidate.isoformat()
            if iso not in found:
                found.append(iso)

    # Берёт именно плотную дату-фразу перед месяцем: "14 и 15 июня", "22, 23, 25 июня".
    pattern = re.compile(rf"((?:\d{{1,2}}\s*(?:,|и|или)?\s*){{1,12}})\s+({month_names})")
    for match in pattern.finditer(lowered):
        raw_days = match.group(1)
        month = _MONTHS_RU.get(match.group(2))
        if not month:
            continue
        for day_raw in re.findall(r"\d{1,2}", raw_days):
            day = int(day_raw)
            if day < 1 or day > 31:
                continue
            year = today.year
            try:
                candidate = datetime(year, month, day).date()
            except ValueError:
                continue
            # Если дата уже явно ушла далеко в прошлое, это следующий год.
            if candidate < today and (today - candidate).days > 31:
                try:
                    candidate = datetime(year + 1, month, day).date()
                except ValueError:
                    continue
            iso = candidate.isoformat()
            if iso not in found:
                found.append(iso)
    return sorted(found)


def _message_has_contact_data(text: str) -> bool:
    if not text:
        return False
    digits = re.sub(r"\D", "", text)
    if len(digits) >= 10:
        return True
    # Typical two-line contact: name + phone may contain the phone in a separate line;
    # if there is a likely name but no full phone, do not close upsells yet.
    return False


def _looks_like_date_refinement_request(text: str, draft: BookingDraft) -> bool:
    if not draft.service_type:
        return False
    if not (draft.last_offered_dates or draft.date):
        return False
    lowered = (text or "").lower().replace("ё", "е").strip()
    if not lowered:
        return False

    # Broad semantic guard for date refinement. It is not tied to one phrase like
    # "попозже": it protects the selected object when the client rejects offered
    # dates or asks for later/other dates.
    date_words = ("дата", "даты", "дату", "число", "когда", "свободн", "июн", "июл", "август", "сент")
    refinement_words = ("друг", "позже", "попозже", "дальше", "след", "не подходит", "не эти", "не на", "когда")

    if any(w in lowered for w in date_words) and any(w in lowered for w in refinement_words):
        return True

    # A short "нет/не" immediately after the bot offered dates usually means
    # "not these dates", not "cancel the selected object".
    if lowered in {"не", "нет", "неа", "не подходит"} and draft.last_offered_dates:
        return True

    return False


def _normalize_user_text_for_dialog(text: str) -> str:
    lowered = (text or "").lower().replace("ё", "е").strip()
    # Cheap typo protection for the very common "че сть" -> "че есть".
    if re.fullmatch(r"ч[ео]?\s*ст[ьъ]?", lowered):
        return "че есть"
    return text


def _is_valid_phone(phone: str) -> bool:
    digits = re.sub(r"\D", "", phone or "")

    if len(digits) == 10 and digits.startswith("9"):
        return True

    return (
        len(digits) == 11
        and digits.startswith(("7", "8"))
    )



def _format_phone(phone: str) -> str:
    digits = re.sub(r"\D", "", phone or "")

    if len(digits) == 10 and digits.startswith("9"):
        return "+7" + digits

    if len(digits) == 11 and digits.startswith("8"):
        return "+7" + digits[1:]

    if len(digits) == 11 and digits.startswith("7"):
        return "+" + digits

    return phone



def _duration_from_text_value(text: str | None) -> float | int | None:
    """Extract duration like '3 часа' from text/service_variant."""
    low = (text or "").lower().replace("ё", "е")
    match = re.search(r"\b(\d{1,2})(?:[,.](\d))?\s*(?:ч|час|часа|часов)\b", low)
    if not match:
        return None

    whole = int(match.group(1))
    frac = match.group(2)
    value = float(f"{whole}.{frac}") if frac else float(whole)

    if value <= 0 or value > 48:
        return None

    return int(value) if value.is_integer() else value


def _duration_from_recent_history(history: list[dict[str, Any]] | None) -> float | int | None:
    """Recover duration from recent user messages if LLM forgot it."""
    for item in reversed(history or []):
        try:
            sender = str(item.get("sender") or item.get("role") or "").lower()
            text = str(item.get("text") or item.get("content") or "")
        except Exception:
            continue

        if sender not in {"user", "customer", "client"}:
            continue

        duration = _duration_from_text_value(text)
        if duration:
            return duration

    return None


def _duration_from_chat_messages(chat_id: str, limit: int = 30) -> float | int | None:
    """Recover duration from stored user messages for current chat."""
    try:
        with sqlite.connect() as conn:
            rows = conn.execute(
                """
                SELECT sender, text
                FROM mvp_messages
                WHERE chat_id = ?
                ORDER BY id DESC
                LIMIT ?
                """,
                (str(chat_id), int(limit)),
            ).fetchall()
    except Exception:
        logger.exception("DURATION_RECOVERY_MESSAGES_FAILED chat_id=%s", chat_id)
        return None

    for row in rows:
        try:
            item = dict(row)
            if str(item.get("sender") or "").lower() != "user":
                continue
            duration = _duration_from_text_value(str(item.get("text") or ""))
            if duration:
                return duration
        except Exception:
            continue

    return None


def _recover_bathhouse_duration_if_missing(
    chat_id: str,
    draft: BookingDraft,
    *,
    history: list[dict[str, Any]] | None = None,
    user_text: str = "",
) -> bool:
    """Hard guard: bathhouse duration must not be lost after user already said it."""
    if draft.service_type != "bathhouse" or draft.duration:
        return False

    duration = (
        _duration_from_text_value(user_text)
        or _duration_from_recent_history(history)
        or _duration_from_chat_messages(chat_id)
        or _duration_from_text_value(getattr(draft, "service_variant", None))
    )

    if not duration:
        return False

    draft.duration = duration

    logger.info(
        "BATHHOUSE_DURATION_RECOVERED chat_id=%s duration=%s draft=%s",
        chat_id,
        duration,
        _draft_log_line(draft),
    )
    return True


def _reconcile_duration_from_variant(draft: BookingDraft) -> None:
    """LLM sometimes writes 'Баня с бассейном, 3 часа' into service_variant
    instead of the structured duration field. Keep service_variant for YClients
    matching, but also fill draft.duration.
    """
    if draft.duration:
        return

    candidates = [
        getattr(draft, "service_variant", None),
        *(getattr(draft, "available_variants", None) or []),
    ]

    for value in candidates:
        duration = _duration_from_text_value(str(value or ""))
        if duration:
            draft.duration = duration
            return


def _restore_lost_core_fields(draft: BookingDraft, before: BookingDraft, *, user_text: str, history: list[dict[str, Any]] | None = None) -> None:
    """Prevent accidental rollback of already collected booking fields.

    The model may omit or move fields between service_variant/duration. The engine
    owns state, so collected date/time/duration must not disappear unless the user
    clearly changes service/date/time/duration.
    """
    _reconcile_duration_from_variant(draft)

    same_service = before.service_type == draft.service_type
    same_date = before.date == draft.date

    text_duration = _duration_from_text_value(user_text)
    history_duration = _duration_from_recent_history(history)

    if text_duration and not draft.duration:
        draft.duration = text_duration

    if history_duration and not draft.duration:
        draft.duration = history_duration

    if same_service and same_date:
        if before.time and not draft.time and not _text_contains_time(user_text):
            draft.time = before.time

        if before.duration and not draft.duration and not text_duration:
            draft.duration = before.duration

    _reconcile_duration_from_variant(draft)


def _restore_core_fields_from_snapshot(draft: BookingDraft, snapshot: BookingDraft, *, reason: str = "") -> None:
    """Restore already collected booking core if some later branch accidentally wiped it."""
    if not snapshot:
        return

    restored: list[str] = []

    for field in (
        "service_type",
        "service_variant",
        "date",
        "time",
        "duration",
        "guests_count",
        "event_format",
    ):
        old_value = getattr(snapshot, field, None)
        new_value = getattr(draft, field, None)

        if old_value and not new_value:
            setattr(draft, field, old_value)
            restored.append(field)

    if restored:
        logger.warning(
            "CORE_FIELDS_RESTORED reason=%s fields=%s draft=%s",
            reason,
            restored,
            _draft_log_line(draft),
        )


def _apply_patch(draft: BookingDraft, patch: dict[str, Any] | None, *, from_user_edit: bool = False) -> None:
    if not patch:
        return
    if from_user_edit and draft.ready_for_confirmation():
        draft.status = "active"
        draft.payment_id = None
        draft.payment_url = None
    aliases = {"guests": "guests_count", "variant": "service_variant", "format": "event_format", "name": "client_name"}
    allowed = set(BookingDraft.__dataclass_fields__)

    # Сначала применяем счётчик предложений допов, чтобы upsell_done проверял
    # уже актуальное значение независимо от порядка ключей в JSON от LLM.
    if "upsell_offer_count" in patch:
        count = _to_int(patch.get("upsell_offer_count"))
        if count is not None:
            draft.upsell_offer_count = max(0, min(int(count), 2))

    # LLM often returns null for fields it is not focused on. Null must not erase
    # already collected booking details (e.g. duration=8 disappearing during final
    # confirmation). Clear time/duration only when the same patch actually changes
    # date or service, because then old time/duration may no longer apply.
    incoming_date = patch.get("date")
    incoming_service = patch.get("service_type") or patch.get("service")
    try:
        normalized_incoming_date = _normalize_date(str(incoming_date)) if incoming_date else None
    except Exception:
        normalized_incoming_date = None
    try:
        normalized_incoming_service = normalize_service_type(str(incoming_service)) if incoming_service else None
    except Exception:
        normalized_incoming_service = None
    patch_changes_date = bool(normalized_incoming_date and normalized_incoming_date != draft.date)
    patch_changes_service = bool(normalized_incoming_service and normalized_incoming_service != draft.service_type)

    for raw_key, raw_value in patch.items():
        key = aliases.get(raw_key, raw_key)
        if key not in allowed:
            continue
        if key == "upsell_offer_count":
            continue
        value = raw_value
        if isinstance(value, str) and value.strip().lower() in {"null", "none", "undefined", "не указано", "неизвестно"}:
            continue
        if value is None:
            if key == "upsell_items":
                draft.upsell_items = []
            elif key == "upsell_done":
                draft.upsell_done = False
            elif key in {"time", "duration"}:
                if from_user_edit or patch_changes_date or patch_changes_service:
                    setattr(draft, key, None)
            elif key in {"service_variant"}:
                if from_user_edit or patch_changes_service:
                    setattr(draft, key, None)
            elif key in {"service_type", "date"}:
                if from_user_edit:
                    setattr(draft, key, None)
            else:
                # Do not erase guests/format/name/phone just because LLM omitted them.
                if from_user_edit:
                    setattr(draft, key, None)
            continue
        if key == "service_type":
            value = normalize_service_type(str(value)) if value else None
            if value and value != draft.service_type:
                draft.service_variant = None
                draft.time = None
                draft.duration = None
                draft.event_format = None
                draft.upsell_items = []
                draft.upsell_done = False
        elif key == "guests_count":
            value = _to_int(value)
        elif key == "duration":
            value = _normalize_duration(value)
            if isinstance(value, (int, float)) and value > 48:
                logger.warning(f"Duration too large: {value}, ignoring")
                continue
        elif key == "time":
            value = _normalize_time(str(value)) if value else None
        elif key == "date":
            value = _normalize_date(str(value)) if value else None
        elif key == "event_format":
            value = _normalize_event_format_value(str(value)) if value else None
        elif key == "upsell_items":
            value = list(value or []) if isinstance(value, list) else []
        elif key == "upsell_done":
            value = bool(value)
            logger.info("UPSEL: value=%s offer_count=%s items=%s", value, draft.upsell_offer_count, draft.upsell_items)
        if value in ("", []) and key not in ("upsell_items", "upsell_done"):
            continue
        setattr(draft, key, value)

    _reconcile_upsell_state(draft, patch)


def _reconcile_upsell_state(draft: BookingDraft, patch: dict[str, Any] | None) -> None:
    if not patch:
        return
    core_ready = bool(draft.service_type and draft.date and draft.time and draft.duration and draft.guests_count)
    if not core_ready:
        return

    patched_done = bool(patch.get("upsell_done") is True)
    has_items = bool(draft.upsell_items)

    # Once upsells are closed, never reopen them just because the counter is stale.
    if draft.upsell_done and int(draft.upsell_offer_count or 0) >= 2:
        return

    if patched_done and not has_items:
        # The business owner wants the upsell step to be explicit.
        # Do not let the LLM silently close extras and jump to contacts/confirmation.
        # Real refusals are handled before the LLM in handle_text().
        draft.upsell_offer_count = max(1, min(int(draft.upsell_offer_count or 0), 1))
        draft.upsell_done = False


def _validate_business_rules(before: BookingDraft, draft: BookingDraft) -> dict[str, Any] | None:
    bathhouse_capacity = _service_capacity_max("bathhouse") or _BATHHOUSE_CAPACITY
    if draft.service_type == "bathhouse" and draft.guests_count and draft.guests_count > bathhouse_capacity:
        attempted_guests = draft.guests_count
        draft.guests_count = before.guests_count
        return {
            "event": "capacity_exceeded",
            "data": {
                "service_type": "bathhouse",
                "capacity_max": bathhouse_capacity,
                "attempted_guests_count": attempted_guests,
                "booking": _booking_reply_data(draft),
                "next_step": draft.next_step(),
            },
        }
    return None


def _maybe_check_availability(chat_id: str, before: BookingDraft, draft: BookingDraft, *, user_text: str, history: list[dict[str, Any]], today: str) -> str | None:
    changed_keys = []
    for key in ("service_type", "service_variant", "date", "time", "duration", "guests_count"):
        if getattr(before, key) != getattr(draft, key):
            changed_keys.append(key)
    if not changed_keys:
        return None
    if not draft.service_type or not draft.date:
        return None

    # Never expose date-level partial occupancy to customers.
    # Without exact time, the only correct next step is to ask the missing field.
    # Exact conflicts are checked only after date + duration + time are known.

    exact_reserved = _reserved_by_other_on_exact_time(draft, chat_id=chat_id)
    if exact_reserved:
        logger.info("EXACT_HOLD_CONFLICT chat_id=%s service_type=%s date=%s time=%s duration=%s other=%s", chat_id, draft.service_type, draft.date, draft.time, draft.duration, exact_reserved)
        blocked_time = draft.time
        # Build watchlist candidate BEFORE clearing draft.time, otherwise the
        # watcher will later check only the whole date and may send a false
        # "slot is free" notification while the exact 16:00-24:00 interval is
        # still held by another chat/payment.
        candidate = _make_watchlist_candidate_from_draft(draft)
        draft.block_reason = "slot_unavailable"
        draft.blocked_until = blocked_time
        draft.time = None
        if candidate:
            draft.pending_action = {"type": "watchlist_create", "candidate": candidate.__dict__}
        reply = _llm_reply_from_engine_context(
            draft=draft,
            chat_id=chat_id,
            user_text=user_text,
            history=history,
            today=today,
            event="watchlist_offer",
            data={**_availability_event_data(draft, "time_held"), "conflict_reason": "held_by_other_booking_or_hold", "rules": ["do_not_disclose_hold_or_payment", "offer_another_time_or_watchlist"]},
            fallback=_fallback_question(draft),
        )
        return reply

    try:
        availability = check_availability(draft, chat_id=chat_id)
    except Exception as exc:
        logger.warning("Availability check failed: %s", exc)
        return None

    if not availability.ok:
        # No schedule/open booking window in YClients is not the same as a busy
        # slot. Do not create watchlist and do not say the object is available.
        if availability.message == "schedule_not_open":
            draft.available_variants = []
            draft.pending_action = None
            draft.block_reason = "schedule_not_open"
            reply = _llm_reply_from_engine_context(
                draft=draft,
                chat_id=chat_id,
                user_text=user_text,
                history=history,
                today=today,
                event="schedule_not_open",
                data=_availability_event_data(draft, availability.message),
                fallback=_fallback_question(draft),
            )
            return reply

        # Date-level unavailability is valid when it comes from records that start on
        # the target date. Previous-day overlaps are filtered in availability.py.
        # So if no time is selected yet, tell the client the chosen date is busy and
        # offer a watchlist instead of collecting contacts for an impossible date.
        draft.available_variants = availability.variants
        # Watchlist is offered only after a real unavailable result. For bathhouse/house
        # this means the concrete time was checked, not just a date-level draft.
        candidate = _make_watchlist_candidate_from_draft(draft)
        if candidate:
            draft.pending_action = {"type": "watchlist_create", "candidate": candidate.__dict__}
        # If exact time was selected, do not let the bot collect contacts first and
        # reveal the problem only at payment. Keep date/duration and ask for a new time.
        if draft.time:
            blocked_time = draft.time
            draft.block_reason = "slot_unavailable"
            draft.blocked_until = blocked_time
            draft.time = None
        reply = _llm_reply_from_engine_context(
            draft=draft,
            chat_id=chat_id,
            user_text=user_text,
            history=history,
            today=today,
            event="watchlist_offer",
            data=_availability_event_data(draft, availability.message),
            fallback=_fallback_question(draft),
        )
        return reply

    if availability.variants:
        draft.available_variants = availability.variants

    # Do not tell customers that the date is "partially occupied".
    # Ask exact time/duration first; then the engine checks the exact interval.

    # Reserve the selected object/time as soon as the client has chosen enough
    # concrete booking details. This protects two clients racing for the same
    # object: the first one who reaches date+time+duration keeps a temporary hold,
    # the second one gets an unavailable message before payment.
    if draft.service_type and draft.date and draft.time and draft.duration:
        try:
            sqlite.upsert_hold(chat_id, draft.to_dict())
            logger.info("SLOT_HOLD_UPSERTED chat_id=%s service_type=%s variant=%s date=%s time=%s duration=%s", chat_id, draft.service_type, draft.service_variant, draft.date, draft.time, draft.duration)
        except Exception:
            logger.exception("Failed to upsert slot hold chat_id=%s", chat_id)

    return None


def _ensure_next_step(reply: str, before: BookingDraft, draft: BookingDraft) -> str:
    """Return the LLM reply without appending customer-facing text.

    Engine may protect state, but it must not write dialog lines for the model.
    Previously this function appended hardcoded next-step questions, including the
    second upsell question, which produced contradictory replies like asking for
    a name and then asking about extras in the same message.
    """
    return reply


def _resume_rejected_step(
    reply: str,
    draft: BookingDraft,
    *,
    chat_id: str,
    user_text: str,
    history: list[dict[str, Any]],
    today: str,
) -> str:
    """Let the answer model preserve its answer and resume the unfilled step."""
    return _llm_reply_from_engine_context(
        draft=draft,
        chat_id=chat_id,
        user_text=user_text,
        history=history,
        today=today,
        event="answer_side_question_and_resume_required_step",
        data={
            "existing_answer": reply,
            "booking": _booking_reply_data(draft),
            "required_step": draft.next_step(),
        },
        fallback=reply,
    )

def _client_guard_reply(reply: str, *, user_text: str, before: BookingDraft, draft: BookingDraft, chat_id: str, history: list[dict[str, Any]], today: str) -> str:
    """Final safety layer for customer-facing wording.

    This does not decide business logic. It prevents admin/developer wording from
    leaking to clients and keeps the upsell flow from contradicting state.
    """
    low_reply = (reply or "").lower().replace("ё", "е")

    # Customer-facing rule: no "partially occupied" concept. For the client the
    # slot is either available or unavailable. If exact time is still missing, just
    # ask for it; do not mention other people's bookings or partial occupancy.
    if _reply_has_partial_time_wording(reply) and draft.next_step() in {"time", "duration"}:
        return _llm_reply_from_engine_context(
            draft=draft, chat_id=chat_id, user_text=user_text, history=history, today=today,
            event="availability_requires_exact_interval",
            data={"booking": _booking_reply_data(draft), "next_step": draft.next_step()},
            fallback=None,
        )

    # If LLM says the exact time is busy but the engine did not return an
    # unavailable result, treat it as hallucination. Real unavailability exits
    # earlier from _maybe_check_availability.
    if _reply_claims_unavailable(reply) and draft.service_type and draft.date and draft.time and draft.duration and draft.next_step() not in {"time", "duration"}:
        logger.warning("DROP_LLM_FALSE_UNAVAILABLE_REPLY chat_id=%s service_type=%s date=%s time=%s duration=%s reply=%r", chat_id, draft.service_type, draft.date, draft.time, draft.duration, reply)
        return _llm_reply_from_engine_context(
            draft=draft, chat_id=chat_id, user_text=user_text, history=history, today=today,
            event="availability_claim_rejected_after_live_check",
            data={"booking": _booking_reply_data(draft), "next_step": draft.next_step()},
            fallback=None,
        )

    # The order must be client-friendly: first collect booking essentials, then
    # offer extras, then ask contacts. If the LLM tries to ask name/phone before
    # the upsell step, keep the state-machine order.
    if draft.next_step() == "upsell_items":
        if int(draft.upsell_offer_count or 0) <= 0:
            draft.upsell_offer_count = 1
            draft.upsell_done = False
            return _llm_reply_from_engine_context(
                draft=draft,
                chat_id=chat_id,
                user_text=user_text,
                history=history,
                today=today,
                event="upsell_first_offer",
                data={
                    "offer_count": 1,
                    "items_to_offer": ["уголь", "розжиг", "решётки", "посуда", "кальян"],
                    "tone": "soft_useful_not_pushy",
                    "next_step_if_refused": "upsell_second_offer",
                },
                fallback=reply,
            )

        # Refusals while on upsell_items are handled before LLM in handle_text(),
        # because the state must decide whether to show the second and final offer.
        return reply

    # If the client already refused twice/finally, never let the LLM reopen extras.
    if draft.upsell_done and _reply_mentions_upsell_for_engine(reply) and draft.next_step() in {"client_name", "phone", "confirmation"}:
        return _llm_reply_from_engine_context(
            draft=draft,
            chat_id=chat_id,
            user_text=user_text,
            history=history,
            today=today,
            event="ask_missing_contacts_after_upsell_closed",
            data={
                "client_name_present": bool(draft.client_name),
                "phone_present": bool(draft.phone),
                "next_step": draft.next_step(),
            },
            fallback=reply,
        )

    # If the client asks a side question while the booking is ready for confirmation,
    # do not replace the answer with a plain summary. Let the LLM answer the actual
    # question and then return to the confirmation step.
    if draft.ready_for_confirmation() and _is_side_question_turn(user_text):
        return _llm_reply_from_engine_context(
            draft=draft,
            chat_id=chat_id,
            user_text=user_text,
            history=history,
            today=today,
            event="pre_payment_side_question",
            data={
                "existing_reply_to_fix": reply,
                "rules": ["answer_customer_question_first", "then_return_to_confirmation", "do_not_replace_answer_with_summary"],
                "booking_summary": {
                    "object": draft.service_variant or service_title(draft.service_type),
                    "date": draft.date,
                    "time": draft.time,
                    "duration": draft.duration,
                    "guests": draft.guests_count,
                    "event_format": draft.event_format,
                    "upsell_items": draft.upsell_items,
                    "price": calculate_booking_price(draft),
                },
            },
            fallback=reply,
        )

    # The user can send contact details and a side question in one message
    # («Савелий / phone / комары есть?»). The LLM may answer only the side
    # question and forget to move the booking forward. If all fields are now ready,
    # ask for final confirmation in the same customer-facing response.
    if draft.ready_for_confirmation() and not _reply_asks_for_confirmation_or_payment(reply):
        return _llm_reply_from_engine_context(
            draft=draft,
            chat_id=chat_id,
            user_text=user_text,
            history=history,
            today=today,
            event="answer_side_question_and_ask_confirmation",
            data={
                "existing_reply_to_preserve": reply,
                "booking_summary": {
                    "object": draft.service_variant or service_title(draft.service_type),
                    "date": draft.date,
                    "time": draft.time,
                    "duration": draft.duration,
                    "guests": draft.guests_count,
                    "event_format": draft.event_format,
                    "client_name": draft.client_name,
                    "phone_present": bool(draft.phone),
                },
                "rules": ["answer_customer_question_first", "ask_booking_confirmation", "do_not_ask_contacts_again"],
            },
            fallback=reply,
        )

    return _polish_customer_wording(reply)


def _reply_mentions_upsell_for_engine(reply: str) -> bool:
    lowered = (reply or "").lower().replace("ё", "е")
    return any(marker in lowered for marker in ("доп", "уголь", "розжиг", "решет", "решёт", "посуда", "кальян"))


def _looks_like_event_format_example_request(text: str) -> bool:
    lowered = (text or "").lower().replace("ё", "е").strip(" ?!.…")
    return lowered in {"например", "какие", "какой например", "что например", "типа", "типо"} or any(
        marker in lowered for marker in ("какие форматы", "что за формат", "что можно", "например что")
    )


def _reply_asks_for_confirmation_or_payment(reply: str) -> bool:
    lowered = (reply or "").lower().replace("ё", "е")
    return any(marker in lowered for marker in ("подтверд", "все верно", "всё верно", "перейти к оплат", "ссылк", "оплат"))


def _date_level_hold_notice(draft: BookingDraft, *, chat_id: str) -> dict[str, Any] | None:
    if not draft.service_type or not draft.date or draft.time:
        return None
    try:
        rows = sqlite.active_holds_for_service_date(draft.service_type, draft.date, ignore_chat_id=chat_id)
    except Exception:
        logger.exception("ACTIVE_HOLDS_FOR_SERVICE_DATE_FAILED chat_id=%s", chat_id)
        return None
    if not rows:
        return None
    times = []
    for row in rows[:10]:
        time_value = str(row.get("time") or "")
        duration = row.get("duration")
        if time_value:
            times.append({"time": time_value, "duration": duration})
    return {"held_times": times, "count": len(rows)}

def _polish_customer_wording(reply: str) -> str:
    return (reply or "").strip()


def _has_refusal(reply: str) -> bool:
    lowered = reply.lower().replace("ё", "е")
    return any(word in lowered for word in ("отказ", "не надо", "не нужно", "нет", "без допов"))



def _canonicalize_price_in_reply(reply: str, draft: BookingDraft) -> str:
    if not reply:
        return reply
    price = calculate_booking_price(draft)
    if not price:
        return reply
    price_text = f"{price:,}".replace(",", " ")
    patterns = [
        r"(общая стоимость(?:\s+составит|\s*[:—-])?\s*)\d[\d\s]*(?:руб(?:\.|лей)?|₽)",
        r"(стоимость(?:\s+составит|\s*[:—-])\s*)\d[\d\s]*(?:руб(?:\.|лей)?|₽)",
        r"(итого(?:\s*[:—-])?\s*)\d[\d\s]*(?:руб(?:\.|лей)?|₽)",
    ]
    result = reply
    for pattern in patterns:
        result = re.sub(pattern, lambda m: f"{m.group(1)}{price_text} ₽", result, flags=re.I)
    return result



def _create_payment_or_admin_handoff(chat_id: str, draft: BookingDraft, *, platform: str | None = "max") -> str:
    if draft.service_type == "gazebo" and draft.next_step() == "service_variant":
        draft.status = "active"
        draft.payment_id = None
        draft.payment_url = None
        sqlite.save_draft(chat_id, draft.to_dict(), status="active", current_step="service_variant")
        logger.warning("CREATE_PAYMENT_BLOCKED_GENERIC_GAZEBO draft=%s", _draft_log_line(draft))
        return _llm_reply_from_engine_context(
            draft=draft,
            chat_id=chat_id,
            user_text="подтверждение бронирования",
            history=_recent_history(chat_id, draft),
            today=now_local().date().isoformat(),
            event="required_field_not_provided",
            data={
                "next_step": "service_variant",
                "booking": _booking_reply_data(draft),
                "rule": "specific_gazebo_number_required_before_payment",
            },
            fallback=None,
        )

    availability = check_availability(draft, chat_id=chat_id)
    if not availability.ok:
        logger.warning("CREATE_PAYMENT_BLOCKED_SLOT_UNAVAILABLE draft=%s", _draft_log_line(draft))
        blocked_time = draft.time
        draft.status = "active"
        draft.block_reason = "slot_unavailable"
        draft.blocked_until = blocked_time
        draft.time = None
        draft.payment_id = None
        draft.payment_url = None
        draft.pending_action = {}
        sqlite.save_draft(chat_id, draft.to_dict(), status="active", current_step=draft.next_step())
        return _llm_reply_from_engine_context(
            draft=draft,
            chat_id=chat_id,
            user_text="подтверждение оплаты",
            history=_recent_history(chat_id, draft),
            today=now_local().date().isoformat(),
            event="payment_blocked_unavailable_time",
            data=_availability_event_data(draft, availability.message),
            fallback=_fallback_question(draft),
        )

    draft.block_reason = None
    draft.blocked_until = None

    try:
        booking_id = sqlite.create_booking(chat_id, draft.to_dict(), status="waiting_payment", platform=platform)
        payment_id, payment_url = create_prepayment(draft, chat_id=chat_id, booking_id=booking_id)
        draft.payment_id = payment_id
        draft.payment_url = payment_url
        draft.status = "waiting_payment"
        sqlite.update_booking(booking_id, draft.to_dict(), status="waiting_payment")
        sqlite.save_draft(chat_id, draft.to_dict(), status="waiting_payment", current_step=draft.next_step())
        sqlite.upsert_hold(chat_id, draft.to_dict())
        notify_admin_booking_created(chat_id=chat_id, booking_id=booking_id, draft=draft)
        _queue_booking_object_photo(chat_id, draft)
        return _payment_link_reply(payment_url, draft, chat_id=chat_id)
    except Exception as exc:
        logger.exception("Payment creation failed chat_id=%s", chat_id)
        sqlite.enqueue_admin_notification(f"Клиент подтвердил заявку, но автоматическая оплата не создалась.\nchat_id: {chat_id}\nОшибка: {exc}\nЗаявка: {draft.to_dict()}", chat_id=chat_id)
        return _llm_reply_from_engine_context(
            draft=draft,
            chat_id=chat_id,
            user_text="клиент подтвердил финальную сводку",
            history=_recent_history(chat_id),
            today=now_local().date().isoformat(),
            event="payment_link_failed_admin_notified",
            data={"admin_notified": True},
            fallback=_fallback_question(draft),
        )




def _handle_decision_action(chat_id: str, decision: AdminDecision, draft: BookingDraft, *, user_text: str = "", platform: str | None = "max") -> str | None:
    action_type = (decision.action.type or "none").strip()
    params = decision.action.params or {}

    # Booking management actions are allowed only when the client clearly asks
    # to cancel/reschedule or provides a Booking ID. This prevents replies like
    # "в смысле?" from exposing internal booking lists during a new booking flow.
    if action_type in {"request_cancel_confirmation", "request_reschedule_confirmation"}:
        operation = "cancel" if action_type == "request_cancel_confirmation" else "reschedule"
        if not _is_explicit_booking_management_request(user_text, operation=operation, params=params):
            logger.warning("BOOKING_OPERATION_ACTION_IGNORED action=%s text=%r params=%s", action_type, user_text, params)
            return None

    if action_type == "create_payment":
        if draft.ready_for_confirmation():
            return _create_payment_or_admin_handoff(chat_id, draft, platform=platform)
        logger.warning("CREATE_PAYMENT_BLOCKED_NOT_READY draft=%s", _draft_log_line(draft))
        return None

    if action_type == "new_booking":
        explicit = bool(params.get("explicit") or params.get("confirmed") or params.get("confirmed_new_booking"))
        has_active_draft = bool(draft.service_type or draft.date or draft.time or draft.duration or draft.guests_count or draft.client_name or draft.phone)
        if has_active_draft and not explicit:
            logger.warning("NEW_BOOKING_ACTION_IGNORED active_draft=True params=%s", params)
            return None
        draft.reset()
        return decision.reply or _llm_reply_from_engine_context(
            draft=draft,
            chat_id=chat_id,
            user_text=user_text,
            history=_recent_history(chat_id),
            today=now_local().date().isoformat(),
            event="new_booking_started",
            data={},
            fallback=_fallback_question(draft),
        )

    if action_type == "offer_watchlist":
        # Availability and watchlist candidates are engine-owned. The normal
        # flow applies validated fields and then _maybe_check_availability creates
        # the pending watchlist only after a real unavailable result.
        logger.info("LLM_WATCHLIST_ACTION_DEFERRED_TO_AVAILABILITY text=%r params=%s", user_text, params)
        return None

    if action_type == "request_cancel_confirmation":
        booking, select_reply = _select_booking_for_operation(chat_id, params, operation="cancel", current_draft=draft)
        if select_reply:
            return _llm_reply_from_engine_context(
                draft=draft, chat_id=chat_id, user_text=user_text, history=_recent_history(chat_id),
                today=now_local().date().isoformat(), event="select_booking_for_cancel",
                data=select_reply, fallback=None,
            )
        if not booking:
            return _llm_reply_from_engine_context(draft=draft, chat_id=chat_id, user_text=user_text, history=_recent_history(chat_id), today=now_local().date().isoformat(), event="cancel_booking_not_found", data={}, fallback=_fallback_question(draft))
        booking_draft = BookingDraft.from_dict(__import__('json').loads(booking["draft_json"]))
        draft.pending_action = {"type": "cancel_booking", "booking_id": int(booking["id"]), "reason": params.get("reason")}
        return _llm_reply_from_engine_context(
            draft=draft, chat_id=chat_id, user_text=user_text, history=_recent_history(chat_id),
            today=now_local().date().isoformat(), event="cancel_booking_confirmation",
            data={
                "booking": _booking_reply_data(booking_draft),
                "days_until_booking": _cancellation_days_until(booking_draft),
                "refund_full_if_days_before_at_least": 7,
            },
            fallback=None,
        )

    if action_type == "request_reschedule_confirmation":
        booking, select_reply = _select_booking_for_operation(chat_id, params, operation="reschedule", current_draft=draft)
        if select_reply:
            return _llm_reply_from_engine_context(
                draft=draft, chat_id=chat_id, user_text=user_text, history=_recent_history(chat_id),
                today=now_local().date().isoformat(), event="select_booking_for_reschedule",
                data=select_reply, fallback=None,
            )
        if not booking:
            return _llm_reply_from_engine_context(draft=draft, chat_id=chat_id, user_text=user_text, history=_recent_history(chat_id), today=now_local().date().isoformat(), event="reschedule_booking_not_found", data={}, fallback=_fallback_question(draft))
        booking_draft = BookingDraft.from_dict(__import__('json').loads(booking["draft_json"]))
        new_date = params.get("new_date") or params.get("date") or params.get("date_from") or decision.fields_patch.get("date") or draft.date
        new_time = params.get("new_time") or params.get("time") or decision.fields_patch.get("time") or booking_draft.time
        if not new_date:
            draft.pending_action = {"type": "await_reschedule_date", "booking_id": int(booking["id"])}
            return _llm_reply_from_engine_context(draft=draft, chat_id=chat_id, user_text=user_text, history=_recent_history(chat_id), today=now_local().date().isoformat(), event="ask_reschedule_date", data={}, fallback=_fallback_question(draft))
        probe = BookingDraft.from_dict(booking_draft.to_dict())
        probe.date = _normalize_date(str(new_date)) or str(new_date)
        if new_time:
            probe.time = _normalize_time(str(new_time)) or str(new_time)
        availability = check_availability(probe, chat_id=chat_id)
        if not availability.ok:
            draft.pending_action = {"type": "await_reschedule_date", "booking_id": int(booking["id"])}
            return _llm_reply_from_engine_context(draft=draft, chat_id=chat_id, user_text=user_text, history=_recent_history(chat_id), today=now_local().date().isoformat(), event="reschedule_date_unavailable", data={"date": probe.date, "object_title": probe.service_variant or service_title(probe.service_type)}, fallback=_fallback_question(draft))
        draft.pending_action = {
            "type": "reschedule_booking",
            "booking_id": int(booking["id"]),
            "new_date": probe.date,
            "new_time": probe.time,
        }
        return sanitize_reply(_reschedule_confirmation_reply(
            draft, booking_draft, new_date=probe.date, new_time=probe.time,
            chat_id=chat_id, user_text=user_text, history=_recent_history(chat_id),
            today=now_local().date().isoformat(),
        ))

    return None


def _is_explicit_booking_management_request(text: str, *, operation: str, params: dict[str, Any] | None = None) -> bool:
    params = params or {}
    if params.get("booking_id") or params.get("id"):
        return True
    lowered = (text or "").lower().replace("ё", "е")
    if operation == "cancel":
        markers = (
            "отмен", "удал", "снять брон", "убрать брон", "отказаться от бро",
            "вернуть предоплат", "возврат",
        )
    else:
        markers = (
            "перенес", "перенести", "перенеси", "перенс", "перенсем", "перенесем",
            "перенос", "поменять дату", "изменить дату", "перезапис", "переоформ",
            "на другую дату",
        )
    return any(marker in lowered for marker in markers)


def _maybe_start_booking_operation_base(
    chat_id: str,
    text: str,
    draft: BookingDraft,
    *,
    history: list[dict[str, Any]],
    today: str,
) -> str | None:
    lowered = (text or "").lower().replace("ё", "е")

    if draft.pending_action:
        return None

    if _is_explicit_booking_management_request(
        text,
        operation="reschedule",
        params={},
    ):
        booking, select_reply = _select_booking_for_operation(
            chat_id,
            {},
            operation="reschedule",
            current_draft=draft,
        )

        if select_reply:
            return _llm_reply_from_engine_context(
                draft=draft,
                chat_id=chat_id,
                user_text=text,
                history=history,
                today=today,
                event="select_booking_for_reschedule",
                data=select_reply,
                fallback=None,
            )

        if booking:
            booking_draft = BookingDraft.from_dict(
                json.loads(booking["draft_json"])
            )

            new_date = _extract_date_from_user_text(
                text,
                base_date=booking_draft.date,
            )
            new_time = (
                _extract_time_from_text(text)
                or booking_draft.time
            )

            if new_date:
                probe = BookingDraft.from_dict(
                    booking_draft.to_dict()
                )
                probe.date = new_date

                if new_time:
                    probe.time = new_time

                availability = check_availability(
                    probe,
                    chat_id=chat_id,
                )

                if not availability.ok:
                    draft.pending_action = {
                        "type": "await_reschedule_date",
                        "booking_id": int(booking["id"]),
                    }

                    return _llm_reply_from_engine_context(
                        draft=draft,
                        chat_id=chat_id,
                        user_text=text,
                        history=history,
                        today=today,
                        event="reschedule_date_unavailable",
                        data={
                            "date": new_date,
                            "object_title": (
                                probe.service_variant
                                or service_title(probe.service_type)
                            ),
                        },
                        fallback=_fallback_question(draft),
                    )

                draft.pending_action = {
                    "type": "reschedule_booking",
                    "booking_id": int(booking["id"]),
                    "new_date": new_date,
                    "new_time": new_time,
                }

                return sanitize_reply(
                    _reschedule_confirmation_reply(
                        draft,
                        booking_draft,
                        new_date=new_date,
                        new_time=new_time,
                        chat_id=chat_id,
                        user_text=text,
                        history=history,
                        today=today,
                    )
                )

            draft.pending_action = {
                "type": "await_reschedule_date",
                "booking_id": int(booking["id"]),
            }

            return _llm_reply_from_engine_context(
                draft=draft,
                chat_id=chat_id,
                user_text=text,
                history=history,
                today=today,
                event="ask_reschedule_date",
                data={
                    "current_booking": _booking_reply_data(booking_draft)
                },
                fallback=_fallback_question(draft),
            )

    if _is_explicit_booking_management_request(
        text,
        operation="cancel",
        params={},
    ):
        booking, select_reply = _select_booking_for_operation(
            chat_id,
            {},
            operation="cancel",
            current_draft=draft,
        )

        if select_reply:
            return _llm_reply_from_engine_context(
                draft=draft,
                chat_id=chat_id,
                user_text=text,
                history=history,
                today=today,
                event="select_booking_for_cancel",
                data=select_reply,
                fallback=None,
            )

        if booking:
            booking_draft = BookingDraft.from_dict(
                json.loads(booking["draft_json"])
            )

            draft.pending_action = {
                "type": "cancel_booking",
                "booking_id": int(booking["id"]),
                "reason": text,
            }

            return _cancellation_confirmation_reply(
                booking_draft,
                chat_id=chat_id,
                user_text=text,
                history=history,
                today=today,
            )

    return None

# POINT_10_IMMEDIATE_ADMIN_OPERATION_NOTICE
def _operation_request_days_until(
    booking_draft: BookingDraft,
) -> int | None:
    if not booking_draft.date:
        return None

    try:
        booking_date = datetime.fromisoformat(
            str(booking_draft.date)
        ).date()
    except ValueError:
        return None

    local_today = (
        now_local().date()
        if callable(globals().get("now_local"))
        else datetime.now().date()
    )

    return (booking_date - local_today).days


def _notify_admin_operation_request(
    chat_id: str,
    draft: BookingDraft,
    action_type: str,
) -> tuple[bool, int | None]:
    pending = dict(draft.pending_action or {})

    if pending.get("admin_request_notified"):
        return True, pending.get("days_until_booking")

    booking_id = pending.get("booking_id")

    if not booking_id:
        return False, None

    booking_draft: BookingDraft | None = None

    try:
        rows = [
            row
            for row in sqlite.list_bookings(
                chat_id,
                limit=20,
            )
            if int(row["id"]) == int(booking_id)
        ]

        if rows:
            payload = json.loads(rows[0]["draft_json"])
            booking_draft = BookingDraft.from_dict(payload)
    except Exception:
        logger.exception(
            "OPERATION_REQUEST_BOOKING_LOAD_FAILED "
            "chat_id=%s booking_id=%s",
            chat_id,
            booking_id,
        )

    days_until = (
        _operation_request_days_until(booking_draft)
        if booking_draft is not None
        else None
    )

    action_label = (
        "отмену"
        if action_type == "cancel_booking"
        else "перенос"
    )

    object_title = (
        booking_draft.service_variant
        if booking_draft is not None
        and booking_draft.service_variant
        else (
            service_title(booking_draft.service_type)
            if booking_draft is not None
            else "не определён"
        )
    )

    booking_date = (
        booking_draft.date
        if booking_draft is not None
        else "не определена"
    )

    booking_time = (
        booking_draft.time
        if booking_draft is not None
        else "не определено"
    )

    if days_until is None:
        policy = "Срок до бронирования не удалось определить."
    elif days_until < 7:
        policy = (
            f"До бронирования осталось {days_until} дн. "
            "При отмене предоплата не возвращается."
        )
    else:
        policy = (
            f"До бронирования осталось {days_until} дн. "
            "При отмене предоплата подлежит возврату."
        )

    message = (
        f"Новый запрос клиента на {action_label} брони.\n"
        "Операция ещё ожидает подтверждения клиента.\n\n"
        f"chat_id: {chat_id}\n"
        f"booking_id: {booking_id}\n"
        f"Объект: {object_title}\n"
        f"Дата: {booking_date}\n"
        f"Время: {booking_time}\n"
        f"{policy}"
    )

    try:
        sqlite.enqueue_admin_notification(
            message,
            chat_id=chat_id,
        )
    except Exception:
        logger.exception(
            "OPERATION_REQUEST_ADMIN_NOTIFY_FAILED "
            "chat_id=%s booking_id=%s",
            chat_id,
            booking_id,
        )
        return False, days_until

    pending["admin_request_notified"] = True
    pending["days_until_booking"] = days_until
    draft.pending_action = pending

    logger.info(
        "OPERATION_REQUEST_ADMIN_NOTIFIED "
        "chat_id=%s booking_id=%s action=%s days=%s",
        chat_id,
        booking_id,
        action_type,
        days_until,
    )

    return True, days_until


def _maybe_start_booking_operation(
    chat_id: str,
    text: str,
    draft: BookingDraft,
    *,
    history: list[dict[str, Any]],
    today: str,
) -> str | None:
    reply = _maybe_start_booking_operation_base(
        chat_id,
        text,
        draft,
        history=history,
        today=today,
    )

    if reply is None:
        return None

    pending = draft.pending_action or {}
    action_type = str(pending.get("type") or "")

    if action_type not in {
        "cancel_booking",
        "reschedule_booking",
        "await_reschedule_date",
    }:
        return reply

    notified, days_until = _notify_admin_operation_request(
        chat_id,
        draft,
        action_type,
    )

    return _llm_reply_from_engine_context(
        draft=draft, chat_id=chat_id, user_text=text, history=history, today=today,
        event="booking_operation_request_reply",
        data={
            "existing_reply": reply,
            "action_type": action_type,
            "admin_notified": notified,
            "days_until_booking": days_until,
            "refund_full_if_days_before_at_least": 7,
            "alternative_actions": ["reschedule_booking", "change_service"],
        },
        fallback=None,
    )




def _select_booking_for_operation(chat_id: str, params: dict[str, Any], *, operation: str, current_draft: BookingDraft | None = None) -> tuple[dict | None, dict[str, Any] | None]:
    booking_id = params.get("booking_id") or params.get("id")
    rows = _actual_booking_rows(chat_id)

    if booking_id:
        for row in rows:
            if str(row.get("id")) == str(booking_id):
                return row, None
        return None, {"reason": "booking_not_found", "requested_booking_id": booking_id}

    # Prefer the booking currently loaded in dialog state. This prevents exposing old
    # PostgreSQL rows that may already be deleted manually through support/YClients.
    if current_draft:
        current = _find_row_matching_draft(rows, current_draft)
        if current:
            return current, None

    rows = _filter_existing_yclients_rows(rows)

    if len(rows) == 1:
        return rows[0], None

    if len(rows) > 1:
        choices: list[dict[str, Any]] = []
        for idx, row in enumerate(rows[:5], start=1):
            bd = BookingDraft.from_dict(json.loads(row["draft_json"]))
            choices.append({"choice_number": idx, "booking_id": row.get("id"), "booking": _booking_reply_data(bd)})
        return None, {"reason": "multiple_bookings", "choices": choices}

    return None, None


def _actual_booking_rows(chat_id: str) -> list[dict]:
    # Only bookings that should really exist in YClients. Waiting payments and old
    # error/manual rows should not be shown to a client as their real bookings.
    rows = sqlite.list_bookings(chat_id, statuses=["booked", "rescheduled"], limit=20)
    result: list[dict] = []
    for row in rows:
        try:
            bd = BookingDraft.from_dict(json.loads(row["draft_json"]))
        except Exception:
            continue
        if bd.yclients_record_id:
            result.append(row)
    return result


def _find_row_matching_draft(rows: list[dict], draft: BookingDraft) -> dict | None:
    for row in rows:
        try:
            bd = BookingDraft.from_dict(json.loads(row["draft_json"]))
        except Exception:
            continue
        if draft.yclients_record_id and bd.yclients_record_id and str(bd.yclients_record_id) == str(draft.yclients_record_id):
            return row
        if draft.payment_id and bd.payment_id and str(bd.payment_id) == str(draft.payment_id):
            return row
        if draft.service_type == bd.service_type and draft.date == bd.date and draft.time == bd.time and str(draft.duration) == str(bd.duration):
            return row
    return None


def _filter_existing_yclients_rows(rows: list[dict]) -> list[dict]:
    filtered: list[dict] = []
    for row in rows:
        try:
            bd = BookingDraft.from_dict(json.loads(row["draft_json"]))
        except Exception:
            continue
        if _yclients_record_exists_safe(bd):
            filtered.append(row)
    return filtered


def _yclients_record_exists_safe(draft: BookingDraft) -> bool:
    if not draft.yclients_record_id or not draft.date:
        return False
    try:
        records = YClientsClient().get_records(start_date=draft.date, end_date=draft.date, page=1)
        rid = str(draft.yclients_record_id)
        for record in records:
            if str(record.get("id") or "") == rid or str(record.get("record_id") or "") == rid:
                return True
    except Exception:
        # If YClients check fails, keep the row rather than losing the user's booking.
        logger.warning("YCLIENTS_RECORD_EXISTENCE_CHECK_FAILED record_id=%s", draft.yclients_record_id)
        return True
    return False


def _looks_like_reschedule_free_dates_question(text: str) -> bool:
    lowered = (text or "").lower().replace("ё", "е")
    return any(marker in lowered for marker in (
        "когда свобод", "когда есть", "какие даты", "какие дни", "что свобод", "когда можно", "а когда",
    ))


def _reschedule_confirmation_reply(
    draft: BookingDraft,
    booking_draft: BookingDraft,
    *,
    new_date: str,
    new_time: str | None,
    chat_id: str,
    user_text: str,
    history: list[dict[str, Any]],
    today: str,
) -> str:
    return _llm_reply_from_engine_context(
        draft=draft, chat_id=chat_id, user_text=user_text, history=history, today=today,
        event="reschedule_booking_confirmation",
        data={
            "booking": _booking_reply_data(booking_draft),
            "new_date": new_date,
            "new_time": new_time or booking_draft.time or draft.time,
            "prepayment_already_counted": True,
        },
        fallback=None,
    )

def _handle_pending_action(chat_id: str, text: str, draft: BookingDraft) -> str | None:
    pending = draft.pending_action or {}
    if not pending:
        return None

    action_type = str(pending.get("type") or "")

    if action_type == "watchlist_create":
        if not _watchlist_enabled():
            logger.info("WATCHLIST_DISABLED pending cleared")
            draft.pending_action = {}
            return None
        # Handled earlier by _handle_watchlist_llm_turn. Returning None here
        # prevents phrase-list confirmation from creating notifications.
        return None

    if _looks_negative(text):
        draft.pending_action = {}
        return _llm_reply_from_engine_context(
        draft=draft,
        chat_id=chat_id,
        user_text=text,
        history=_recent_history(chat_id),
        today=now_local().date().isoformat(),
        event="pending_action_declined",
        data={"pending_action_type": action_type},
        fallback=_fallback_question(draft),
    )

    if action_type == "cancel_booking":
        if not _looks_positive(text):
            return None
        booking_id = int(pending.get("booking_id"))
        return _perform_cancel_booking(chat_id, booking_id, draft, reason=str(pending.get("reason") or ""))

    if action_type == "reschedule_booking":
        if not _looks_positive(text):
            return None
        booking_id = int(pending.get("booking_id"))
        new_date = str(pending.get("new_date") or "")
        new_time = str(pending.get("new_time") or "") or None
        return _perform_reschedule_booking(chat_id, booking_id, new_date, new_time, draft)

    if action_type == "await_reschedule_date":
        booking_id = int(pending.get("booking_id"))
        rows = [row for row in sqlite.list_bookings(chat_id, limit=20) if int(row["id"]) == booking_id]
        if not rows:
            draft.pending_action = {}
            return _llm_reply_from_engine_context(draft=draft, chat_id=chat_id, user_text=text, history=_recent_history(chat_id), today=now_local().date().isoformat(), event="booking_not_found_for_pending_action", data={"action": action_type}, fallback=_fallback_question(draft))
        booking_draft = BookingDraft.from_dict(__import__('json').loads(rows[0]["draft_json"]))

        # In an active reschedule chain, a question like «а когда свободно» must not
        # fall through to the normal media/booking flow. Keep the pending reschedule
        # action, list dates, and do not send object photos.
        if _looks_like_reschedule_free_dates_question(text):
            dates = _available_dates_for_service(booking_draft.service_type, limit=10, current_booking=booking_draft, chat_id=chat_id)
            draft.last_offered_dates = dates[:20]
            draft.last_offered_service_type = booking_draft.service_type
            draft.last_offered_object_title = booking_draft.service_variant or _OBJECT_TITLE_BY_SERVICE.get(booking_draft.service_type or "") or service_title(booking_draft.service_type)
            draft.pending_action = {"type": "await_reschedule_date", "booking_id": booking_id}
            return sanitize_reply(_llm_reply_from_engine_context(
                draft=draft,
                chat_id=chat_id,
                user_text=text,
                history=_recent_history(chat_id),
                today=now_local().date().isoformat(),
                event="reschedule_available_dates",
                data={"available_dates": dates, "booking": _booking_reply_data(booking_draft)},
                fallback=None,
            ))

        new_date = _extract_date_from_user_text(text, base_date=booking_draft.date)
        if not new_date:
            return None
        probe = BookingDraft.from_dict(booking_draft.to_dict())
        probe.date = new_date
        # Keep the paid booking's original time and duration unless the client explicitly changes them.
        probe.time = probe.time or booking_draft.time
        probe.duration = probe.duration or booking_draft.duration
        availability = check_availability(probe, chat_id=chat_id)
        if not availability.ok:
            draft.pending_action = {"type": "await_reschedule_date", "booking_id": booking_id}
            return _llm_reply_from_engine_context(draft=draft, chat_id=chat_id, user_text=text, history=_recent_history(chat_id), today=now_local().date().isoformat(), event="reschedule_date_unavailable", data={"date": new_date, "object_title": probe.service_variant or service_title(probe.service_type)}, fallback=_fallback_question(draft))
        draft.pending_action = {
            "type": "reschedule_booking",
            "booking_id": booking_id,
            "new_date": new_date,
            "new_time": probe.time,
        }
        return sanitize_reply(_reschedule_confirmation_reply(
            draft, booking_draft, new_date=new_date, new_time=probe.time,
            chat_id=chat_id, user_text=text, history=_recent_history(chat_id),
            today=now_local().date().isoformat(),
        ))

    return None


def _perform_cancel_booking(
    chat_id: str,
    booking_id: int,
    draft: BookingDraft,
    *,
    reason: str = "",
) -> str:
    rows = [
        row
        for row in sqlite.list_bookings(chat_id, limit=20)
        if int(row["id"]) == int(booking_id)
    ]

    if not rows:
        draft.pending_action = {}

        return _llm_reply_from_engine_context(
            draft=draft,
            chat_id=chat_id,
            user_text="операция с бронью",
            history=_recent_history(chat_id),
            today=now_local().date().isoformat(),
            event="booking_not_found_admin_handoff",
            data={"admin_notified": True},
            fallback=_fallback_question(draft),
        )

    row = rows[0]
    booking_draft = BookingDraft.from_dict(
        json.loads(row["draft_json"])
    )

    yclients_error = None

    if booking_draft.yclients_record_id:
        try:
            YClientsClient().delete_record(
                booking_draft.yclients_record_id
            )
            _refresh_availability_after_freeing_slot(
                "booking_cancelled"
            )
        except Exception as exc:
            yclients_error = str(exc)
            logger.exception(
                "Failed to delete YCLIENTS record "
                "booking_id=%s",
                booking_id,
            )

    booking_draft.status = "canceled"

    sqlite.update_booking(
        booking_id,
        booking_draft.to_dict(),
        "canceled",
    )
    sqlite.release_holds(chat_id)

    notify_admin_cancel_refund_required(
        chat_id=chat_id,
        booking_id=booking_id,
        draft=booking_draft,
        reason=reason or yclients_error,
    )

    draft.pending_action = {}

    return _cancellation_completed_reply(
        booking_draft,
        manual_review=bool(yclients_error),
        chat_id=chat_id,
    )



def _perform_reschedule_booking(chat_id: str, booking_id: int, new_date: str, new_time: str | None, draft: BookingDraft) -> str:
    rows = [row for row in sqlite.list_bookings(chat_id, limit=20) if int(row["id"]) == int(booking_id)]
    if not rows:
        draft.pending_action = {}
        return _llm_reply_from_engine_context(draft=draft, chat_id=chat_id, user_text="операция с бронью", history=_recent_history(chat_id), today=now_local().date().isoformat(), event="booking_not_found_admin_handoff", data={"admin_notified": True}, fallback=_fallback_question(draft))
    row = rows[0]
    booking_draft = BookingDraft.from_dict(json.loads(row["draft_json"]))
    old_date, old_time, old_record_id = booking_draft.date, booking_draft.time, booking_draft.yclients_record_id
    booking_draft.date = _normalize_date(new_date) or new_date
    if new_time:
        booking_draft.time = _normalize_time(new_time) or new_time

    availability = check_availability(booking_draft, chat_id=chat_id)
    if not availability.ok:
        draft.pending_action = {"type": "await_reschedule_date", "booking_id": booking_id}
        return _llm_reply_from_engine_context(draft=draft, chat_id=chat_id, user_text="перенос брони", history=_recent_history(chat_id), today=now_local().date().isoformat(), event="reschedule_date_unavailable", data={"date": booking_draft.date, "object_title": booking_draft.service_variant or service_title(booking_draft.service_type)}, fallback=_fallback_question(draft))

    yclients_error = None
    try:
        # Safer than PUT /record with a book_record-shaped payload: create the new
        # YClients record first, then delete the old one. If creation fails, the old
        # booking remains untouched.
        response = YClientsClient().create_book_record(build_yclients_payload(booking_draft))
        new_record_id = _extract_yclients_record_id(response)
        if new_record_id:
            booking_draft.yclients_record_id = str(new_record_id)
        if old_record_id:
            YClientsClient().delete_record(str(old_record_id))
            _refresh_availability_after_freeing_slot("booking_rescheduled_old_slot_freed")
    except Exception as exc:
        yclients_error = str(exc)
        logger.exception("Failed to recreate YCLIENTS record for reschedule booking_id=%s", booking_id)

    booking_draft.status = "rescheduled" if not yclients_error else "reschedule_manual_review"
    sqlite.update_booking(booking_id, booking_draft.to_dict(), booking_draft.status)
    sqlite.save_draft(chat_id, booking_draft.to_dict(), status=booking_draft.status, current_step=booking_draft.next_step())
    sqlite.release_holds(chat_id)
    notify_admin_booking_rescheduled(chat_id=chat_id, booking_id=booking_id, draft=booking_draft, old_date=old_date, old_time=old_time)

    # Keep in-memory draft consistent with what was really written to DB/YClients.
    # Otherwise the outer handler may save the old date back over the fixed draft.
    draft.__dict__.update(booking_draft.to_dict())
    draft.pending_action = {}

    if yclients_error:
        return _llm_reply_from_engine_context(draft=draft, chat_id=chat_id, user_text="перенос брони", history=_recent_history(chat_id), today=now_local().date().isoformat(), event="reschedule_manual_review", data={"admin_notified": True}, fallback=_fallback_question(draft))
    return _llm_reply_from_engine_context(
        draft=draft, chat_id=chat_id, user_text="", history=_recent_history(chat_id),
        today=now_local().date().isoformat(), event="reschedule_booking_completed",
        data={
            "booking": _booking_reply_data(booking_draft),
            "old_date": old_date,
            "old_time": old_time,
            "prepayment_already_counted": True,
        },
        fallback=None,
    )


def _extract_yclients_record_id(response: Any) -> str | None:
    if isinstance(response, list) and response:
        for item in response:
            if isinstance(item, dict):
                value = item.get("record_id") or item.get("id")
                if value:
                    return str(value)
    if isinstance(response, dict):
        value = response.get("record_id") or response.get("id")
        if value:
            return str(value)
        data = response.get("data")
        if data is not None:
            return _extract_yclients_record_id(data)
    return None

def _extract_date_from_user_text(text: str, *, base_date: str | None = None) -> str | None:
    lowered = (text or "").lower().replace("ё", "е")
    today = now_local().date()
    base = None
    if base_date:
        try:
            base = datetime.fromisoformat(base_date).date()
        except ValueError:
            base = None

    # Explicit YYYY-MM-DD.
    m = re.search(r"\b(20\d{2}-\d{2}-\d{2})\b", lowered)
    if m:
        return m.group(1)

    # Russian month name: «23 июня».
    month_names = "|".join(_MONTHS_RU.keys())
    m = re.search(rf"\b(\d{{1,2}})\s+({month_names})\b", lowered)
    if m:
        day = int(m.group(1))
        month = _MONTHS_RU.get(m.group(2))
        if month:
            year = today.year
            try:
                candidate = datetime(year, month, day).date()
                if candidate < today:
                    candidate = datetime(year + 1, month, day).date()
                return candidate.isoformat()
            except ValueError:
                return None

    # Short follow-up in an active reschedule chain: «тогда на 23».
    m = re.search(r"\b(?:на|к)\s+(\d{1,2})\b", lowered) or re.search(r"^\s*(\d{1,2})\s*$", lowered)
    if m:
        day = int(m.group(1))
        month = (base or today).month
        year = (base or today).year
        try:
            candidate = datetime(year, month, day).date()
            if candidate < today:
                # If the inferred date is already in the past, move one month forward.
                next_month = month + 1
                next_year = year
                if next_month > 12:
                    next_month = 1
                    next_year += 1
                candidate = datetime(next_year, next_month, day).date()
            return candidate.isoformat()
        except ValueError:
            return None

    return None


def _looks_positive(text: str) -> bool:
    lowered = text.lower().replace("ё", "е")
    return bool(is_positive_confirmation(text, BookingDraft()) or any(word in lowered for word in ("да", "подтверж", "подтаверж", "верно", "соглас", "включ", "хорошо", "ок")))


def _looks_negative(text: str) -> bool:
    lowered = text.lower().replace("ё", "е")
    return any(word in lowered for word in ("нет", "не надо", "отмена", "отбой", "не нужно"))


def _fallback_question(draft: BookingDraft) -> str:
    return ""


def _mentioned_service_types(text: str) -> set[str]:
    normalized = (text or "").lower().replace("ё", "е")
    found: set[str] = set()
    if "бан" in normalized:
        found.add("bathhouse")
    if "дом" in normalized:
        found.add("house")
    if "бесед" in normalized:
        found.add("warm_gazebo" if "тепл" in normalized else "gazebo")
    return found


def _waiting_payment_additional_service_type(text: str, draft: BookingDraft) -> str | None:
    """Return a different object mentioned while the current payment is pending."""
    if draft.status != "waiting_payment" or not draft.service_type:
        return None

    normalized = (text or "").lower().replace("ё", "е")
    if any(marker in normalized for marker in (
        "вместо", "замени", "заменить", "поменя", "переоформ", "перенес",
    )):
        return None

    different = _mentioned_service_types(normalized) - {draft.service_type}
    if not different:
        return None
    return sorted(different)[0]


def _requested_additional_service_type(text: str, draft: BookingDraft) -> str | None:
    """Detect adding a second object without confusing it with replacement."""
    if not draft.service_type:
        return None

    normalized = (text or "").lower().replace("ё", "е")
    if any(marker in normalized for marker in (
        "вместо", "замени", "заменить", "поменя", "не баню, а", "не дом, а", "не беседку, а",
    )):
        return None

    mentioned = _mentioned_service_types(normalized)
    additional = mentioned - {draft.service_type}
    if not additional:
        return None

    additive = any(marker in normalized for marker in (
        "добав", "еще", "вместе", "плюс", "также", "оба", "две брони", "два объекта",
        "и бан", "и дом", "и бесед",
    ))
    if not additive:
        return None

    return sorted(additional)[0]


def _to_int(value: Any) -> int | None:
    if isinstance(value, int): return value
    if isinstance(value, float): return int(value)
    if isinstance(value, str):
        digits = re.findall(r"\d+", value)
        if digits: return max(int(x) for x in digits)
    return None


def _normalize_duration(value: Any) -> int | float | None:
    if isinstance(value, (int, float)):
        number = int(value) if float(value).is_integer() else float(value)
        if number > 48: number = number / 60
        return int(number) if float(number).is_integer() else float(number)
    if isinstance(value, str):
        value_lower = value.lower()
        if any(word in value_lower for word in ["сутк", "день", "дня"]): return 24
        if "полтор" in value_lower: return 1.5
        match = re.search(r"\d+(?:[,.]\d+)?", value)
        if match:
            number = float(match.group(0).replace(",", "."))
            if number > 48: number = number / 60
            return int(number) if number.is_integer() else number
    return None


def _normalize_time(value: str) -> str | None:
    raw = value.strip().lower().replace(".", ":")
    match = re.search(r"(\d{1,2})[:\s]?(\d{2})?", raw)
    if not match: return None
    hour = int(match.group(1))
    minute = int(match.group(2) or 0)
    if 0 <= hour <= 23 and 0 <= minute <= 59: return f"{hour:02d}:{minute:02d}"
    return None


def _normalize_date(value: str) -> str | None:
    value = value.strip()
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", value): return value
    return None


def _human_date(value: str | None) -> str:
    if not value:
        return ""
    try:
        dt = datetime.fromisoformat(value)
    except ValueError:
        return value
    months = {1: "января", 2: "февраля", 3: "марта", 4: "апреля", 5: "мая", 6: "июня", 7: "июля", 8: "августа", 9: "сентября", 10: "октября", 11: "ноября", 12: "декабря"}
    return f"{dt.day} {months[dt.month]} {dt.year}"


def _is_hard_reset(text: str) -> bool:
    lowered = text.lower().replace("ё", "е").strip()
    return lowered == "/start" or lowered in {"заново", "начать заново", "по новой", "сначала"}


def _is_abusive_only(text: str) -> bool:
    lowered = text.lower().replace("ё", "е").strip()
    abusive = ("иди нах", "пошел", "пошла", "сука", "блять", "ебан", "хуй")
    return any(word in lowered for word in abusive) and len(lowered.split()) <= 5


def _draft_log_line(draft: BookingDraft) -> str:
    return (f"service_type={draft.service_type} date={draft.date} guests={draft.guests_count} variant={draft.service_variant} time={draft.time} duration={draft.duration} format={draft.event_format} upsell_done={draft.upsell_done} name={draft.client_name} phone={draft.phone} status={draft.status} next={draft.next_step()}")
