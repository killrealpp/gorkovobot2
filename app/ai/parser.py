from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import Any

import httpx

from app.ai.response_sanitizer import sanitize_reply
from app.core.config import get_settings
from app.data.admin_profile import (
    booking_required_order,
    build_profile_knowledge,
    profile_service_keys,
    render_template_file,
)
from app.catalog.reader import bot_media_catalog, bot_services_catalog
from app.data.services import service_title
from app.dialog.pricing import calculate_booking_price, extra_hour_price_breakdown
from app.dialog.availability_cache import availability_context_for_llm, availability_object_dates_for_llm
from app.dialog.state import AdminAction, AdminDecision, BookingDraft

logger = logging.getLogger(__name__)

PROMPT_PATH = Path(__file__).resolve().with_name("admin_prompt.md")
ROUTER_PROMPT_PATH = Path(__file__).resolve().with_name("router_prompt.md")
ENGINE_RESPONSE_PROMPT_PATH = Path(__file__).resolve().with_name("engine_response_prompt.md")


def _load_knowledge() -> str:
    return build_profile_knowledge()


def load_engine_response_prompt() -> str:
    return render_template_file(ENGINE_RESPONSE_PROMPT_PATH)


def render_admin_prompt() -> str:
    return render_template_file(PROMPT_PATH)


def render_router_prompt() -> str:
    return render_template_file(ROUTER_PROMPT_PATH)


def _clip_text(value: str, max_chars: int, *, label: str) -> str:
    value = value or ""
    if max_chars <= 0 or len(value) <= max_chars:
        return value
    logger.info("LLM_CONTEXT_CLIPPED label=%s before_chars=%s after_chars=%s", label, len(value), max_chars)
    return value[:max_chars].rstrip() + "\n...[контекст сокращён для экономии токенов]"


def _should_run_media_decision(*, user_text: str, reply: str, media_catalog: dict[str, Any]) -> bool:
    """Cheap gate before the separate media LLM call.

    This keeps the old behavior when photos are actually relevant, but avoids
    spending an extra LLM request on every ordinary booking step.
    """
    text = f"{user_text}\n{reply}".lower().replace("ё", "е")
    photo_markers = (
        "фото", "фотк", "картин", "покажи", "показать",
        "скинь", "пришли", "как выглядит", "посмотреть", "видно",
    )
    if any(marker in text for marker in photo_markers):
        return True

    choice_markers = ("выберите", "можно выбрать", "варианты", "свободны", "доступны", "предложить")
    if any(marker in text for marker in choice_markers):
        catalog_titles = [str(title).lower().replace("ё", "е") for title in media_catalog.keys()]
        return any(title and title in text for title in catalog_titles)

    return False


def decide(text: str, draft: BookingDraft, *, today: str, history: list[dict[str, str]] | None = None) -> AdminDecision:
    settings = get_settings()
    if not _llm_api_key(settings):
        return fallback_decision(text)

    predecision = _decide_data_request(
        text,
        draft,
        today=today,
        history=history,
    )

    availability_query = _normalize_availability_query(predecision.get("availability_query"))

    mode = availability_query.get("mode")

    if mode == "object_dates" and availability_query.get("object_title"):
        object_context = availability_object_dates_for_llm(
            title=str(availability_query.get("object_title")),
            date_from=availability_query.get("date_from") or today,
            limit=settings.llm_object_dates_limit,
        )

        # Do not send the whole 180-day cache to the model. If the user named
        # a date, the overview is only for that date; otherwise object_context
        # already contains the useful nearest dates.
        overview_context = availability_context_for_llm(
            service_type=None,
            date=availability_query.get("date_from"),
            limit=settings.llm_availability_limit,
        ) if availability_query.get("date_from") else ""

        availability_context = (
            "БЛОК 1. ДОСТУПНОСТЬ КОНКРЕТНОГО ОБЪЕКТА\n"
            + object_context
            + ("\n\nБЛОК 2. ОБЩИЙ ОБЗОР ДОСТУПНЫХ ВАРИАНТОВ ПО ДАТАМ\n" + overview_context if overview_context else "")
        )
    else:
        availability_context = availability_context_for_llm(
            service_type=availability_query.get("service_type"),
            date=availability_query.get("date_from"),
            limit=settings.llm_availability_limit,
        )

    availability_context = _clip_text(
        availability_context,
        settings.llm_availability_max_chars,
        label="availability_context",
    )
    knowledge_context = _clip_text(
        _load_knowledge(),
        settings.llm_knowledge_max_chars,
        label="knowledge",
    )

    logger.info("LLM_PREDECISION=%s", predecision)
    logger.info("LLM_AVAILABILITY_CONTEXT:\n%s", availability_context[:6000])

    payload = {
        "model": settings.answer_model,
        "temperature": settings.openai_temperature,
        "max_tokens": settings.answer_max_tokens,
        "messages": [
            {"role": "system", "content": render_admin_prompt()},
            {
                "role": "user",
                "content": json.dumps(
                    {
                        "task": "decide_final_reply",
                        "today": today,
                        "recent_dialog": history or [],
                        "current_draft": draft.to_dict(),
                        "booking_flow_state": _booking_flow_state(draft),
                        "predecision": predecision,
                        "availability_query_used": availability_query,
                        "services_catalog": _compact_services_catalog(),
                        "media_catalog": _compact_media_catalog(),
                        "availability_cache": availability_context,
                        "knowledge": knowledge_context,
                        "message": text,
                    },
                    ensure_ascii=False,
                ),
            },
        ],
    }

    try:
        content = _post_chat_completion(payload)
        logger.info("LLM_FINAL_RAW_RESPONSE=%s", content[:6000])
    except Exception as exc:
        logger.exception("LLM final decision failed: %s", exc)
        return fallback_decision(text)

    decision = _decision_from_json(content)
    if not decision:
        decision = _decision_from_plain_text(content)
    if not decision:
        return fallback_decision(text)

    router_patch = predecision.get("fields_patch") or {}
    if router_patch:
        merged_patch = dict(router_patch)
        merged_patch.update(decision.fields_patch or {})
        decision.fields_patch = {key: value for key, value in merged_patch.items() if value not in (None, "", [])}

    proposed_draft = _draft_with_patch(draft, decision.fields_patch)

    # Отдельный LLM-шаг выравнивает финальный ответ по уже собранному draft.
    # Здесь код не решает, что говорить клиенту: он только передаёт модели факты
    # после применения fields_patch — next_step, точную цену и статус готовности.
    if proposed_draft.service_type:
        decision = _finalize_reply_with_flow_state(
            original_message=text,
            original_decision=decision,
            current_draft=draft,
            proposed_draft=proposed_draft,
            availability_context=availability_context,
            settings=settings,
        )
        proposed_draft = _draft_with_patch(draft, decision.fields_patch)

    # Финальная модель иногда игнорирует requested_media, потому что у неё слишком много задач.
    # Поэтому решение о фото выносим в отдельный маленький LLM-вызов: модель видит готовый ответ,
    # availability и media_catalog, и возвращает только список фото. Код сам не решает, где фото нужны.
    if settings.llm_media_decision_enabled and not decision.requested_media:
        media_catalog = _compact_media_catalog()
        if _should_run_media_decision(user_text=text, reply=decision.reply, media_catalog=media_catalog):
            decision.requested_media = _decide_requested_media(
                reply=decision.reply,
                availability_context=availability_context,
                media_catalog=media_catalog,
                settings=settings,
                current_draft=proposed_draft.to_dict(),
                booking_flow_state=_booking_flow_state(proposed_draft),
                decision_intent=decision.intent,
                ready_for_confirmation=decision.ready_for_confirmation,
                missing_fields=decision.missing_fields,
            )
        else:
            logger.info("LLM_MEDIA_SKIP cheap_gate=true")
            decision.requested_media = []

    logger.info("LLM_MEDIA_DECISION requested_media=%s", decision.requested_media)

    return decision





def render_watchlist_triggered_message(event: dict[str, Any]) -> str:
    """Generate a client-facing message after a watched option becomes available."""
    settings = get_settings()

    if not _llm_api_key(settings):
        raise RuntimeError("LLM API key is empty")

    model = settings.answer_model or settings.parser_model
    if not model:
        raise RuntimeError("LLM model is empty")

    facts = {
        "event": "watchlist_triggered",
        "object_title": event.get("object_title"),
        "service_type": event.get("service_type"),
        "date": event.get("date"),
        "time": event.get("time"),
        "duration": event.get("duration"),
    }

    payload = {
        "model": model,
        "temperature": 0.3,
        "max_tokens": 220,
        "messages": [
            {
                "role": "system",
                "content": (
                    "Ты пишешь сообщение клиенту базы отдыха от имени живого администратора Любови. "
                    "Код уже повторно проверил доступность и подтвердил, что вариант из события "
                    "watchlist_triggered теперь свободен. "
                    "Сформулируй только готовый клиентский ответ, без JSON и технических пояснений. "
                    "Тепло и коротко сообщи хорошую новость. Точно назови объект и дату. "
                    "Время и длительность укажи только тогда, когда они переданы. "
                    "Предложи продолжить бронирование этого варианта. "
                    "Не говори, что бронь уже создана или подтверждена. "
                    "Не придумывай цену, условия, время и другие данные."
                ),
            },
            {
                "role": "user",
                "content": json.dumps(facts, ensure_ascii=False),
            },
        ],
    }

    content = _post_chat_completion(payload).strip()
    if not content:
        raise RuntimeError("LLM returned an empty watchlist notification")

    return sanitize_reply(content)


def classify_watchlist_turn(
    *,
    user_text: str,
    last_bot_text: str,
    current_draft: dict[str, Any],
    pending_candidate: dict[str, Any] | None,
    recent_dialog: list[dict[str, Any]] | None = None,
    today: str | None = None,
) -> dict[str, Any]:
    """Small LLM classifier for the notification/watchlist sub-dialog.

    It deliberately does not use keyword lists. The model decides whether the
    user's latest message accepts the notification offer, declines/cancels it,
    or ignores the offer and continues the normal booking flow.
    """
    settings = get_settings()
    if not _llm_api_key(settings):
        return {"decision": "ignore", "confidence": 0.0, "customer_reply": ""}

    system_prompt = """
Ты классифицируешь только один маленький участок диалога: предложение уведомить клиента, если объект/время освободится.

Верни только JSON. Никакого текста вокруг.

decision:
- accept: клиент явно хочет включить уведомление/сообщение об освобождении.
- decline: клиент явно отказывается от уведомления или просит его отключить/не включать.
- ignore: клиент не отвечает на уведомление, а продолжает бронирование, выбирает другое время/дату, задаёт вопрос, уточняет цену/скидки/условия или в одном сообщении есть другое намерение.

Правила:
1. Не считай выбор другого времени/даты согласием на уведомление.
2. Если в сообщении одновременно есть выбор времени/даты и вопрос про условия, это ignore.
3. Если сообщение неоднозначное — ignore.
4. Ты не создаёшь бронь и не решаешь доступность. Только классифицируешь отношение к уведомлению.
5. customer_reply заполняй только для accept или decline: короткий ответ администратора без технических деталей. Для ignore — пустая строка.
6. Не придумывай объект/дату: используй pending_candidate/current_draft, если они есть.

Формат:
{
  "decision": "accept|decline|ignore",
  "confidence": 0.0,
  "reason": "коротко для логов",
  "customer_reply": "короткий ответ клиенту или пустая строка"
}
""".strip()
    payload = {
        "model": settings.parser_model or settings.answer_model,
        "temperature": 0,
        "max_tokens": 180,
        "messages": [
            {"role": "system", "content": system_prompt},
            {
                "role": "user",
                "content": json.dumps(
                    {
                        "today": today,
                        "last_bot_text": last_bot_text,
                        "user_text": user_text,
                        "current_draft": current_draft or {},
                        "pending_candidate": pending_candidate or {},
                        "recent_dialog": recent_dialog or [],
                    },
                    ensure_ascii=False,
                ),
            },
        ],
    }
    try:
        content = _post_chat_completion(payload)
        logger.info("LLM_WATCHLIST_CLASSIFIER_RAW=%s", content[:1000])
        data = _json_from_content(content) or {}
    except Exception as exc:
        logger.exception("LLM watchlist classifier failed: %s", exc)
        return {"decision": "ignore", "confidence": 0.0, "customer_reply": ""}

    decision = str(data.get("decision") or "ignore").strip().lower()
    if decision not in {"accept", "decline", "ignore"}:
        decision = "ignore"
    try:
        confidence = float(data.get("confidence") or 0)
    except Exception:
        confidence = 0.0
    return {
        "decision": decision,
        "confidence": confidence,
        "reason": str(data.get("reason") or ""),
        "customer_reply": _clean_reply(str(data.get("customer_reply") or "")) if data.get("customer_reply") else "",
    }

def _finalize_reply_with_flow_state(
    *,
    original_message: str,
    original_decision: AdminDecision,
    current_draft: BookingDraft,
    proposed_draft: BookingDraft,
    availability_context: str,
    settings: Any,
) -> AdminDecision:
    price_info = _booking_price_info(proposed_draft)
    flow_state = _booking_flow_state(proposed_draft)

    system_prompt = """
Ты финально редактируешь ответ администратора базы отдыха после того, как LLM уже извлекла данные в draft.

Верни только JSON. Никакого текста вокруг.

Твоя задача — не менять логику бронирования, а сделать ответ корректным по текущему proposed_draft.
Код не должен хардкодить финальную сводку, поэтому её формируешь ты.

Строгие правила:
1. Всегда сначала отвечай на вопрос клиента, если в сообщении клиента был вопрос. Затем вернись к бронированию.
2. Не обещай ссылку на оплату и не пиши, что бронь подтверждена, если flow_state.next_required_step_before_message != "confirmation".
3. Если после сообщения клиента не хватает time — спроси только время. Не спрашивай гостей или допы раньше времени.
4. Если не хватает duration — спроси длительность.
5. Если не хватает guests_count — спроси количество гостей.
6. Поле event_format необязательное. Никогда не задавай отдельный вопрос о формате отдыха. Если клиент сам назвал формат, его можно сохранить.
7. Допы предлагай, когда уже есть service_type, date, time, duration и guests_count.
8. Допы нужно предложить два раза. Если upsell_offer_count=0 и клиент отказался — предложи допы ещё раз, не переходи к имени/телефону. Если upsell_offer_count>=1 и клиент снова отказался — можно перейти к имени/телефону.
9. Если flow_state.next_required_step_before_message == "confirmation", покажи финальную сводку заявки и точную стоимость из price_info. Не выдумывай цену.
10. Для бани 8+ часов цена берётся только из price_info. Не пересчитывай её самостоятельно и не используй цену из старого ответа.
11. Если в исходном ответе есть противоречивая цена, замени её на price_info.price_rub.
12. requested_media не заполняй здесь, если бот собирает данные или показывает финальную сводку.

Стиль ответа:
- Пиши как живой администратор базы отдыха в Telegram: тепло, спокойно, уверенно и по делу.
- Без канцелярита, без длинных объяснений и без лишних извинений.
- Не используй фразу «Извините за путаницу», если реальной ошибки не было.
- Не заканчивай каждое сообщение фразами вроде «если возникнут вопросы, обращайтесь».
- Не повторяй одно и то же подтверждение несколько раз.
- Если клиент уже подтвердил действие, не проси подтвердить его снова.
- Формулировки должны быть короткими: 1–3 предложения, кроме финальной сводки брони.
- Используй реальные переносы строк между смысловыми блоками; если пишешь перенос внутри JSON-строки, используй \n.
- Используй обычно 0–1 эмодзи на сообщение: дружелюбно, но без спама. Не ставь эмодзи в ответах про цену, отказ от оплаты, ошибку, отмену или недоступность. Галочку ставь только когда действие реально выполнено.
- Для занятости пиши спокойно: «На это время уже есть бронь», а не драматично «К сожалению...».
- Для оплаты: «Бронь подготовила. Для подтверждения нужна предоплата, вот ссылка:».
- Для отказа от допов: «Хорошо, тогда просто уточню: ничего из допов не готовим?».
- Для контактов: «Напишите, пожалуйста, имя и номер телефона — оформлю бронь до конца.».

Формат ответа:
{
  "reply": "сообщение клиенту",
  "ready_for_confirmation": true или false,
  "fields_patch": {},
  "requested_media": []
}
""".strip()

    payload = {
        "model": settings.answer_model,
        "temperature": 0,
        "max_tokens": settings.finalizer_max_tokens,
        "messages": [
            {"role": "system", "content": system_prompt},
            {
                "role": "user",
                "content": json.dumps(
                    {
                        "task": "finalize_reply_with_flow_state",
                        "message": original_message,
                        "original_decision": {
                            "reply": original_decision.reply,
                            "intent": original_decision.intent,
                            "fields_patch": original_decision.fields_patch,
                            "ready_for_confirmation": original_decision.ready_for_confirmation,
                            "missing_fields": original_decision.missing_fields,
                        },
                        "current_draft_before_message": current_draft.to_dict(),
                        "proposed_draft_after_message": proposed_draft.to_dict(),
                        "flow_state_after_message": flow_state,
                        "price_info": price_info,
                        "availability_context": availability_context[:5000],
                    },
                    ensure_ascii=False,
                ),
            },
        ],
    }

    try:
        content = _post_chat_completion(payload)
        logger.info("LLM_FLOW_FINALIZER_RAW_RESPONSE=%s", content[:3000])
        data = _json_from_content(content) or {}
    except Exception as exc:
        logger.exception("LLM flow finalizer failed: %s", exc)
        return original_decision

    if not data:
        reply = _plain_text_reply_from_content(content)
        if reply:
            original_decision.reply = reply
        return original_decision

    if not isinstance(data, dict):
        return original_decision

    reply = str(data.get("reply") or original_decision.reply or "").strip()
    if reply:
        original_decision.reply = _clean_reply(reply)

    patch = data.get("fields_patch")
    if isinstance(patch, dict) and patch:
        merged_patch = dict(original_decision.fields_patch or {})
        merged_patch.update({key: value for key, value in patch.items() if value not in (None, "", [])})
        original_decision.fields_patch = merged_patch

    media = data.get("requested_media")
    if isinstance(media, list):
        original_decision.requested_media = [str(item) for item in media if item]

    original_decision.ready_for_confirmation = bool(data.get("ready_for_confirmation") or False)
    return original_decision


def _draft_with_patch(draft: BookingDraft, patch: dict[str, Any] | None) -> BookingDraft:
    data = draft.to_dict()
    if not patch:
        return BookingDraft.from_dict(data)

    aliases = {"guests": "guests_count", "variant": "service_variant", "format": "event_format", "name": "client_name"}
    allowed = set(BookingDraft.__dataclass_fields__)
    for raw_key, raw_value in patch.items():
        key = aliases.get(raw_key, raw_key)
        if key not in allowed:
            continue
        if raw_value in (None, "", []):
            continue
        data[key] = raw_value
    return BookingDraft.from_dict(data)


def _booking_price_info(draft: BookingDraft) -> dict[str, Any]:
    price = calculate_booking_price(draft)
    duration = draft.duration
    try:
        duration_hours = int(float(duration)) if duration is not None else None
    except (TypeError, ValueError):
        duration_hours = None
    extra_breakdown = extra_hour_price_breakdown(draft)

    result = {
        "price_rub": price,
        "service_type": draft.service_type,
        "duration_hours": duration_hours,
    }
    if extra_breakdown:
        base_hours = extra_breakdown["base_duration_hours"]
        extra_price = extra_breakdown["extra_hour_price"]
        result["extra_hour_price_rub"] = extra_price
        result["rule"] = (
            f"Для {service_title(draft.service_type)} дольше {base_hours} часов: "
            f"цена за {base_hours} часов + {extra_price} ₽ за каждый дополнительный час."
        )
    return result

def _decide_requested_media(
    *,
    reply: str,
    availability_context: str,
    media_catalog: dict[str, Any],
    settings: Any,
    current_draft: dict[str, Any],
    booking_flow_state: dict[str, Any],
    decision_intent: str,
    ready_for_confirmation: bool,
    missing_fields: list[str],
) -> list[str]:
    if not media_catalog:
        logger.info("LLM_MEDIA_SKIP empty_media_catalog=true")
        return []

    system_prompt = """
Ты решаешь, какие фото объектов нужно отправить клиенту после готового ответа бота.

Верни только JSON. Никакого текста вокруг.

Главный принцип:
Фото нужны, когда клиент выбирает объект. Фото НЕ нужны, когда бот уже оформляет конкретную бронь и собирает недостающие данные.

Верни [] если ответ бота:
- спрашивает время, длительность, формат мероприятия, количество гостей, имя или телефон;
- показывает финальную сводку/подтверждение уже выбранной брони;
- говорит про оплату или ссылку на оплату;
- отвечает на уточняющий вопрос внутри оформления и потом возвращает к сбору данных;
- начинается по смыслу с «отлично, выбрали...» / «записала...» / «уточните...» и не предлагает выбрать другой объект.

Фото нужны обязательно, если ответ бота:
- показывает список свободных вариантов на дату;
- предлагает клиенту выбрать из нескольких объектов;
- рекомендует конкретные альтернативные объекты вместо занятого.

Добавляй только объекты, которые упомянуты в ответе бота и есть в media_catalog.
Не добавляй объект, если в availability_context он находится в блоке НЕДОСТУПНО на обсуждаемую дату.
Для бани используй "bathhouse".
Для гостевого дома используй "house".
Для тёплой беседки используй "Тёплая беседка".
Для крытой беседки используй "Крытая беседка".
Для обычных беседок используй полные названия: "Беседка №1", "Беседка №2" и так далее.
Максимум 10 элементов.

Формат ответа строго такой:
{
  "requested_media": []
}
""".strip()

    payload = {
        "model": settings.parser_model,
        "temperature": 0,
        "max_tokens": 500,
        "messages": [
            {"role": "system", "content": system_prompt},
            {
                "role": "user",
                "content": json.dumps(
                    {
                        "task": "decide_requested_media",
                        "bot_reply": reply,
                        "current_draft": current_draft,
                        "booking_flow_state": booking_flow_state,
                        "decision_intent": decision_intent,
                        "ready_for_confirmation": ready_for_confirmation,
                        "missing_fields": missing_fields,
                        "availability_context": availability_context[:settings.llm_media_availability_max_chars],
                        "media_catalog": media_catalog,
                    },
                    ensure_ascii=False,
                ),
            },
        ],
    }

    try:
        content = _post_chat_completion(payload)
        logger.info("LLM_MEDIA_RAW_RESPONSE=%s", content[:2000])
        data = _json_from_content(content) or {}
    except Exception as exc:
        logger.exception("LLM media decision failed: %s", exc)
        return []

    requested = data.get("requested_media") or []
    if not isinstance(requested, list):
        return []

    allowed = set(media_catalog.keys())
    result: list[str] = []
    for item in requested:
        title = str(item).strip()
        if title in allowed and title not in result:
            result.append(title)
        if len(result) >= 10:
            break

    return result


def _decide_data_request(
    text: str,
    draft: BookingDraft,
    *,
    today: str,
    history: list[dict[str, str]] | None = None,
) -> dict[str, Any]:
    settings = get_settings()

    payload = {
        "model": settings.parser_model,
        "temperature": 0,
        "max_tokens": settings.parser_max_tokens,
        "messages": [
            {"role": "system", "content": render_router_prompt()},
            {
                "role": "user",
                "content": json.dumps(
                    {
                        "task": "route_data_request",
                        "today": today,
                        "recent_dialog": history or [],
                        "current_draft": draft.to_dict(),
                        "services_catalog": _compact_services_catalog(),
                        "message": text,
                    },
                    ensure_ascii=False,
                ),
            },
        ],
    }

    try:
        content = _post_chat_completion(payload)
        data = _json_from_content(content) or {}
    except Exception as exc:
        logger.exception("LLM router failed: %s", exc)
        data = {}

    if not isinstance(data, dict):
        data = {}

    data.setdefault("intent", "other")
    data["availability_query"] = _normalize_availability_query(data.get("availability_query"))
    fields_patch = data.get("fields_patch") or data.get("form_data_patch") or {}
    data["fields_patch"] = fields_patch if isinstance(fields_patch, dict) else {}

    return data


def _normalize_availability_query(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        value = {}

    mode = value.get("mode") or "date_overview"
    if mode not in {"date_overview", "object_dates"}:
        mode = "date_overview"

    service_type = value.get("service_type")
    if service_type in ("", "null", "None"):
        service_type = None

    if service_type not in {None, *profile_service_keys()}:
        service_type = None

    date_from = value.get("date_from") or value.get("date")
    date_to = value.get("date_to") or date_from
    object_title = value.get("object_title")

    if object_title in ("", "null", "None"):
        object_title = None

    return {
        "mode": mode,
        "date_from": str(date_from) if date_from else None,
        "date_to": str(date_to) if date_to else None,
        "service_type": service_type,
        "object_title": str(object_title) if object_title else None,
    }


def _llm_api_key(settings: Any) -> str:
    return str(getattr(settings, "openai_api_key", "") or getattr(settings, "deepseek_api_key", "") or "").strip()


def _llm_base_url(settings: Any) -> str:
    return str(getattr(settings, "openai_base_url", "") or getattr(settings, "deepseek_base_url", "") or "https://api.openai.com/v1").rstrip("/")



_ADMIN_LLM_ERROR_LAST_SENT: dict[str, float] = {}
_ADMIN_LLM_ERROR_COOLDOWN_SECONDS = 15 * 60


def _queue_llm_api_error_notification(
    *,
    kind: str,
    base_url: str,
    model: str,
    status_code: int | None = None,
    detail: str = "",
) -> None:
    """Queue one understandable admin alert and suppress repeated identical errors."""
    import re
    import time

    provider = "OpenRouter" if "openrouter.ai" in base_url.lower() else "LLM API"
    # Ошибки аккаунта, ключа, баланса и доступности относятся ко всему
    # провайдеру, поэтому не создаём отдельное уведомление для каждой модели.
    provider_wide_error = (
        kind in {"config", "network", "invalid_response"}
        or status_code in {401, 402, 403, 408, 429}
        or (status_code is not None and 500 <= status_code <= 599)
    )

    if provider_wide_error:
        error_key = f"{provider}:{kind}:{status_code}"
    else:
        # Например, 404 может относиться только к конкретной модели.
        error_key = f"{provider}:{kind}:{status_code}:{model}"
    now = time.monotonic()
    last_sent = _ADMIN_LLM_ERROR_LAST_SENT.get(error_key, 0.0)

    if now - last_sent < _ADMIN_LLM_ERROR_COOLDOWN_SECONDS:
        logger.warning(
            "LLM_ADMIN_ALERT_SUPPRESSED provider=%s kind=%s status=%s model=%s",
            provider,
            kind,
            status_code,
            model,
        )
        return

    if kind == "config":
        reason = "На сервере отсутствует API-ключ для нейросети."
        action = "Проверьте ключ OpenRouter в .env и перезапустите сервис."
    elif kind == "network":
        reason = "Не удалось подключиться к API нейросети."
        action = "Проверьте интернет, DNS и доступность OpenRouter."
    elif kind == "invalid_response":
        reason = "API нейросети вернул ответ в неожиданном формате."
        action = "Проверьте модель и состояние OpenRouter."
    elif status_code == 401:
        reason = "API-ключ неверный, отозван или больше не действует."
        action = "Проверьте ключ OpenRouter в .env и перезапустите сервис."
    elif status_code == 402:
        reason = "Недостаточно средств или кредитов на балансе OpenRouter."
        action = "Пополните баланс OpenRouter и проверьте ограничения аккаунта."
    elif status_code == 403:
        reason = "Доступ к API или выбранной модели запрещён."
        action = "Проверьте права API-ключа и доступность выбранной модели."
    elif status_code == 404:
        reason = "Выбранная модель или адрес API не найдены."
        action = "Проверьте название модели и базовый URL OpenRouter."
    elif status_code == 408:
        reason = "OpenRouter не успел ответить вовремя."
        action = "Проверьте доступность сервиса. Возможно, ошибка временная."
    elif status_code == 429:
        reason = "Превышен лимит запросов или исчерпана доступная квота."
        action = "Проверьте лимиты и баланс OpenRouter."
    elif status_code is not None and 500 <= status_code <= 599:
        reason = "OpenRouter или провайдер модели временно недоступен."
        action = "Подождите несколько минут и проверьте состояние сервиса."
    else:
        reason = "При обращении к API нейросети произошла неизвестная ошибка."
        action = "Проверьте логи сервера и настройки OpenRouter."

    clean_detail = str(detail or "").strip()
    clean_detail = re.sub(r"sk-[A-Za-z0-9_-]+", "[API_KEY_HIDDEN]", clean_detail)
    clean_detail = clean_detail[:500]

    lines = [
        f"⚠️ Ошибка {provider}",
        "",
    ]

    if status_code is not None:
        lines.append(f"Код ошибки: {status_code}")

    if model:
        lines.append(f"Модель: {model}")

    lines.extend([
        f"Причина: {reason}",
        f"Что сделать: {action}",
        "",
        "Бот временно использует резервный ответ.",
    ])

    if clean_detail:
        lines.extend(["", f"Техническая информация: {clean_detail}"])

    try:
        from app.storage import sqlite

        sqlite.enqueue_admin_notification(
            "\n".join(lines),
            chat_id=None,
        )
        _ADMIN_LLM_ERROR_LAST_SENT[error_key] = now

        logger.info(
            "LLM_ADMIN_ALERT_ENQUEUED provider=%s kind=%s status=%s model=%s",
            provider,
            kind,
            status_code,
            model,
        )
    except Exception:
        logger.exception(
            "LLM_ADMIN_ALERT_ENQUEUE_FAILED provider=%s kind=%s status=%s",
            provider,
            kind,
            status_code,
        )


def _post_chat_completion(payload: dict[str, Any]) -> str:
    settings = get_settings()
    api_key = _llm_api_key(settings)
    base_url = _llm_base_url(settings)
    model = str(payload.get("model") or "")

    if not api_key:
        error = RuntimeError("LLM API key is empty")
        _queue_llm_api_error_notification(
            kind="config",
            base_url=base_url,
            model=model,
            detail=str(error),
        )
        raise error

    try:
        with httpx.Client(timeout=30) as client:
            response = client.post(
                f"{base_url}/chat/completions",
                headers={"Authorization": f"Bearer {api_key}"},
                json=payload,
            )

            try:
                response.raise_for_status()
            except httpx.HTTPStatusError:
                try:
                    response_data = response.json()
                    if isinstance(response_data, dict):
                        detail = str(
                            response_data.get("error")
                            or response_data.get("message")
                            or response_data
                        )
                    else:
                        detail = str(response_data)
                except Exception:
                    detail = response.text

                _queue_llm_api_error_notification(
                    kind="http",
                    base_url=base_url,
                    model=model,
                    status_code=response.status_code,
                    detail=detail,
                )
                raise

            try:
                data = response.json()
                return str(data["choices"][0]["message"]["content"])
            except Exception as exc:
                _queue_llm_api_error_notification(
                    kind="invalid_response",
                    base_url=base_url,
                    model=model,
                    detail=f"{type(exc).__name__}: {exc}",
                )
                raise

    except httpx.RequestError as exc:
        _queue_llm_api_error_notification(
            kind="network",
            base_url=base_url,
            model=model,
            detail=f"{type(exc).__name__}: {exc}",
        )
        raise

def fallback_decision(text: str) -> AdminDecision:
    return AdminDecision(
        reply="",
        intent="info",
        fields_patch={},
        action=AdminAction("none"),
        confidence=0.0,
        ready_for_confirmation=False,
    )


def _plain_text_reply_from_content(content: str) -> str:
    raw = (content or "").strip()
    if not raw:
        return ""
    if raw.lstrip().startswith(("{", "[")):
        return ""
    if re.search(r'"(?:reply|reply_to_user|draft|fields_patch|form_data_patch)"\s*:', raw):
        return ""
    return _clean_reply(raw)


def _decision_from_plain_text(content: str) -> AdminDecision | None:
    reply = _plain_text_reply_from_content(content)
    if not reply:
        return None
    return AdminDecision(
        reply=reply,
        intent="info",
        fields_patch={},
        action=AdminAction("none"),
        confidence=0.0,
        ready_for_confirmation=False,
    )


def _json_from_content(content: str) -> dict[str, Any] | None:
    raw = content.strip()
    if raw.startswith("```"):
        raw = re.sub(r"^```(?:json)?|```$", "", raw, flags=re.I | re.M).strip()
    match = re.search(r"\{.*\}", raw, flags=re.S)
    if match:
        raw = match.group(0)
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        logger.warning("Cannot parse JSON from LLM content=%r", content[:1000])
        return None
    return data if isinstance(data, dict) else None


def _decision_from_json(content: str) -> AdminDecision | None:
    data = _json_from_content(content)
    if not data:
        return None

    action_raw = data.get("action") or "none"
    if isinstance(action_raw, dict):
        action_type = str(action_raw.get("type") or "none")
        action_params = dict(action_raw.get("params") or {})
    else:
        action_type = str(action_raw)
        action_params = {}

    fields_patch = data.get("form_data_patch") or data.get("fields_patch") or {}
    if not fields_patch and isinstance(data.get("draft"), dict):
        fields_patch = {key: value for key, value in data["draft"].items() if value not in (None, "", [])}

    requested_media = data.get("requested_media") or []
    if not isinstance(requested_media, list):
        requested_media = []

    return AdminDecision(
        reply=_clean_reply(str(data.get("reply_to_user") or data.get("reply") or "")),
        intent=str(data.get("intent") or "unknown"),
        fields_patch={key: value for key, value in fields_patch.items() if value not in (None, "", [])},
        action=AdminAction(type=action_type, params=action_params),
        missing_fields=[
            str(item)
            for item in (data.get("missing_fields") or [])
            if str(item) != "event_format"
        ],
        confidence=float(data.get("confidence") or 0),
        requested_media=[str(item) for item in requested_media if item],
        ready_for_confirmation=bool(data.get("ready_for_confirmation") or False),
    )


def _clean_reply(reply: str) -> str:
    return sanitize_reply(reply, fallback="")


def _booking_flow_state(draft: BookingDraft) -> dict[str, Any]:
    next_step = draft.next_step()
    core_ready_for_upsell = bool(
        draft.service_type
        and draft.date
        and draft.time
        and draft.duration
        and draft.guests_count
    )
    return {
        "next_required_step_before_message": next_step,
        "ready_for_confirmation_before_message": draft.ready_for_confirmation(),
        "payment_allowed_now": draft.ready_for_confirmation(),
        "core_ready_for_upsell": core_ready_for_upsell,
        "upsell_offer_count": draft.upsell_offer_count,
        "upsell_done": draft.upsell_done,
        "required_order": booking_required_order(),
        "rule": "Если next_required_step_before_message не confirmation, нельзя писать что бронь подтверждена или что ссылка на оплату будет отправлена. Сначала ответь на вопрос клиента, затем спроси ровно следующий недостающий пункт. Допы нельзя предлагать до заполнения time, duration и guests_count. Поле event_format необязательное, отдельно его не спрашивай.",
    }


def _compact_services_catalog() -> dict[str, Any]:
    return bot_services_catalog()


def _compact_media_catalog() -> dict[str, Any]:
    return bot_media_catalog()

def is_llm_rate_limited() -> bool:
    """
    Проверяет, не превышен ли лимит запросов к LLM.
    Возвращает True если лимит превышен, иначе False.
    """
    # Простая реализация - всегда возвращаем False, т.к. лимиты не настроены
    # Если нужна реальная проверка, можно добавить логику с подсчетом запросов
    return False
