from __future__ import annotations

import json
import logging
import re

import httpx

from app.core.config import get_settings
from app.dialog.state import BookingDraft


logger = logging.getLogger(__name__)

SYSTEM_PROMPT = """
Ты классификатор подтверждения бронирования.
Верни только валидный JSON без markdown:
{"is_positive": true|false, "confidence": 0.0}

Считай положительным подтверждением:
- клиент согласен с заявкой;
- клиент просит ссылку на оплату;
- клиент говорит, что всё верно/правильно;
- клиент пишет "да", "ок", "подтверждаю", "давайте ссылку пожалуйста";
- клиент пишет короткое сленговое согласие: "газ", "го", "погнали", "жги", "делай", "делай грязь", "пойдет", "пойдёт";
- клиент неформально разрешает продолжать оформление или оплату;
- клиент явно хочет перейти к оплате.

Считай false:
- клиент задает вопрос;
- клиент хочет изменить дату, время, объект, гостей, телефон;
- клиент сомневается;
- клиент отказывается;
- клиент пишет не по теме.
"""


def is_positive_confirmation(text: str, draft: BookingDraft) -> bool:
    settings = get_settings()
    if not settings.deepseek_api_key:
        return _fallback_confirmation(text)
    payload = {
        "model": settings.openai_model,
        "temperature": 0,
        "max_tokens": 40,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {
                "role": "user",
                "content": json.dumps({"message": text, "current_draft": draft.to_dict()}, ensure_ascii=False),
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
            content = response.json()["choices"][0]["message"]["content"]
        data = _json_from_text(content)
        is_positive = bool(data.get("is_positive")) and float(data.get("confidence") or 0) >= 0.65
        logger.info(
            "Confirmation classifier result is_positive=%s confidence=%s text=%r",
            is_positive,
            data.get("confidence"),
            text[:80],
        )
        return is_positive
    except Exception as exc:
        logger.warning("Confirmation classifier failed, fallback used: %s", exc)
        return _fallback_confirmation(text)


def _json_from_text(content: str) -> dict:
    raw = content.strip()
    if raw.startswith("```"):
        raw = re.sub(r"^```(?:json)?|```$", "", raw, flags=re.I | re.M).strip()
    match = re.search(r"\{.*\}", raw, flags=re.S)
    if match:
        raw = match.group(0)
    return json.loads(raw)


def _fallback_confirmation(text: str) -> bool:
    lowered = text.lower().replace("ё", "е").strip()
    if any(word in lowered for word in ("измен", "помен", "друг", "нет", "не хочу", "стоп")):
        return False
    confirm_markers = (
        "да",
        "ок",
        "ага",
        "верно",
        "правильно",
        "подтверждаю",
        "подтверждаем",
        "газ",
        "го",
        "погнали",
        "жги",
        "делай",
        "делай грязь",
        "пойдет",
        "пойдёт",
    )
    request_markers = ("давай", "давайте", "скинь", "скиньте", "отправь", "отправьте", "пришли", "пришлите", "можно", "жду")
    payment_markers = ("ссылк", "оплат", "предоплат", "платеж", "платёж")
    direct_confirm = lowered in confirm_markers or any(marker in lowered for marker in confirm_markers[2:])
    payment_request = any(marker in lowered for marker in request_markers) and any(marker in lowered for marker in payment_markers)
    return direct_confirm or payment_request
