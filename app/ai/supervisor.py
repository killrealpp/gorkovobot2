from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from typing import Any

import httpx

from app.core.config import get_settings
from app.dialog.state import AdminDecision, BookingDraft


logger = logging.getLogger(__name__)


@dataclass
class SemanticDecision:
    positive_confirmation: bool = False
    summary_request: bool = False
    info_question: bool = False
    booking_edit: bool = False
    alternatives_request: bool = False
    reset_request: bool = False
    confidence: float = 0.0


SYSTEM_PROMPT = """
Ты семантический супервизор диалога бронирования.
Твоя задача — классифицировать последнее сообщение клиента в контексте текущей заявки.

Верни только валидный JSON без markdown:
{
  "positive_confirmation": true|false,
  "summary_request": true|false,
  "info_question": true|false,
  "booking_edit": true|false,
  "alternatives_request": true|false,
  "reset_request": true|false,
  "confidence": 0.0
}

Правила:
- positive_confirmation=true только если клиент явно подтверждает текущую заявку или просит ссылку на оплату.
- Если клиент задает вопрос, уточняет цену, условия, разницу между вариантами, зачем нужен телефон, что входит в стоимость — info_question=true.
- Если клиент просит "что сейчас в заявке", "что я бронирую", "покажи заявку", "проверь заявку" — summary_request=true.
- Если клиент меняет дату, время, объект, гостей, допы, телефон, формат или отказывается от ранее добавленного — booking_edit=true.
- Если клиент просит подобрать другой вариант/дату/время после занятости или пишет "подбери", "какие есть", "что свободно" — alternatives_request=true.
- Если клиент просит начать заново — reset_request=true.

Важно:
- Вопрос "в чем разница между крытой и теплой?" — info_question=true, summary_request=false, booking_edit=false.
- "Теплую давай" — booking_edit=true, info_question=false.
- "Подбери" после сообщения о занятости — alternatives_request=true.
- "Да" после вопроса "подтверждаем?" — positive_confirmation=true.
- "Да" после вопроса про допы может означать ответ про допы, а не подтверждение брони.
""".strip()


def supervise_message(
    text: str,
    *,
    draft_before: BookingDraft,
    draft_after: BookingDraft,
    decision: AdminDecision,
) -> SemanticDecision:
    settings = get_settings()
    if not settings.deepseek_api_key:
        return _fallback_supervisor(text)

    payload = {
        "model": settings.openai_model,
        "temperature": 0,
        "max_tokens": 160,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {
                "role": "user",
                "content": json.dumps(
                    {
                        "message": text,
                        "draft_before": draft_before.to_dict(),
                        "draft_after": draft_after.to_dict(),
                        "next_step_before": draft_before.next_step(),
                        "next_step_after": draft_after.next_step(),
                        "parser_intent": decision.intent,
                        "parser_action": decision.action.type,
                        "parser_fields_patch": decision.fields_patch,
                    },
                    ensure_ascii=False,
                ),
            },
        ],
    }
    try:
        with httpx.Client(timeout=12) as client:
            response = client.post(
                f"{settings.deepseek_base_url.rstrip('/')}/chat/completions",
                headers={"Authorization": f"Bearer {settings.deepseek_api_key}"},
                json=payload,
            )
            response.raise_for_status()
            data = _json_from_text(response.json()["choices"][0]["message"]["content"])
        semantic = SemanticDecision(
            positive_confirmation=bool(data.get("positive_confirmation")),
            summary_request=bool(data.get("summary_request")),
            info_question=bool(data.get("info_question")),
            booking_edit=bool(data.get("booking_edit")),
            alternatives_request=bool(data.get("alternatives_request")),
            reset_request=bool(data.get("reset_request")),
            confidence=float(data.get("confidence") or 0),
        )
        logger.info("Semantic supervisor result=%s text=%r", semantic, text[:100])
        return semantic
    except Exception as exc:
        logger.warning("Semantic supervisor failed, fallback used: %s", exc)
        return _fallback_supervisor(text)


def _json_from_text(content: str) -> dict[str, Any]:
    raw = content.strip()
    if raw.startswith("```"):
        raw = re.sub(r"^```(?:json)?|```$", "", raw, flags=re.I | re.M).strip()
    match = re.search(r"\{.*\}", raw, flags=re.S)
    if match:
        raw = match.group(0)
    return json.loads(raw)


def _fallback_supervisor(text: str) -> SemanticDecision:
    lowered = text.lower().replace("ё", "е").strip()
    summary = any(x in lowered for x in ("что сейчас", "что в заявке", "что я бронирую", "покажи заявку", "проверь заявку"))
    alternatives = any(x in lowered for x in ("подбери", "какие есть", "что свободно", "другое время", "другая дата"))
    info = "?" in text or any(x in lowered for x in ("в чем разница", "чем отличается", "сколько стоит", "зачем", "почему"))
    edit = any(x in lowered for x in ("поменя", "измени", "не надо", "не нужен", "свой", "другую", "другой", "теплую давай", "теплую"))
    confirm = lowered in {"да", "ок", "верно", "подтверждаю", "подтверждаем"} or (
        any(x in lowered for x in ("давай", "пришли", "отправь", "скинь")) and any(x in lowered for x in ("ссыл", "оплат"))
    )
    reset = lowered == "/start" or any(x in lowered for x in ("по новой", "заново", "сначала"))
    return SemanticDecision(
        positive_confirmation=confirm,
        summary_request=summary,
        info_question=info,
        booking_edit=edit,
        alternatives_request=alternatives,
        reset_request=reset,
        confidence=0.5,
    )
