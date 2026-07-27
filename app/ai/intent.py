# app/ai/intent.py
from __future__ import annotations

import json
import logging
import re
from functools import lru_cache
from typing import Any

import httpx

from app.core.config import get_settings

logger = logging.getLogger(__name__)

SYSTEM_PROMPT = """
Ты классификатор намерений пользователя в диалоге бронирования.

Проанализируй сообщение пользователя и верни JSON с полями:

{
  "is_negative": true/false,
  "is_positive": true/false,
  "wants_upsell": true/false,
  "wants_no_upsell": true/false,
  "has_own_stuff": true/false,
  "needs_clarification": true/false,
  "confidence": 0.0-1.0
}

Правила (примеры):
- "нет", "не", "неа", "нинада", "-", "ни", "не надо", "ничего не нужно" → is_negative=true, wants_no_upsell=true
- "да", "ок", "ага", "хорошо", "конечно", "давай" → is_positive=true
- "кальян давай", "хочу уголь", "допы возьмем" → wants_upsell=true
- "ничего не надо", "без допов", "допы не нужны" → wants_no_upsell=true
- "у нас всё своё", "с собой возьмем", "свои угли" → has_own_stuff=true
- "6-7", "шесть или семь", "не знаю" → needs_clarification=true

Верни ТОЛЬКО JSON, без пояснений.
"""

def classify_intent(text: str, context: str = None) -> dict[str, Any]:
    settings = get_settings()
    if not settings.deepseek_api_key:
        return _fallback_classify(text)

    user_content = {"message": text}
    if context:
        user_content["context"] = context

    payload = {
        "model": settings.openai_model,
        "temperature": 0,
        "max_tokens": 200,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": json.dumps(user_content, ensure_ascii=False)},
        ],
    }

    try:
        with httpx.Client(timeout=10) as client:
            response = client.post(
                f"{settings.deepseek_base_url.rstrip('/')}/chat/completions",
                headers={"Authorization": f"Bearer {settings.deepseek_api_key}"},
                json=payload,
            )
            response.raise_for_status()
            content = response.json()["choices"][0]["message"]["content"]
            return _parse_json(content)
    except Exception as e:
        logger.warning("Intent classification failed: %s", e)
        return _fallback_classify(text)


def _parse_json(content: str) -> dict[str, Any]:
    raw = content.strip()
    if raw.startswith("```"):
        raw = re.sub(r"^```(?:json)?|```$", "", raw, flags=re.I | re.M).strip()
    match = re.search(r"\{.*\}", raw, flags=re.S)
    if match:
        raw = match.group(0)
    try:
        return json.loads(raw)
    except:
        return {}


def _fallback_classify(text: str) -> dict[str, Any]:
    lowered = text.lower().replace("ё", "е").strip()

    if lowered in {"-", "--", "—", "нет", "не", "ни", "неа", "неаа", "нее", "нинада", "ненада"}:
        return {"is_negative": True, "wants_no_upsell": True, "confidence": 0.8}
    if re.search(r"\b(не надо|ничего не надо|не нужно|не требуется|без доп)\b", lowered):
        return {"is_negative": True, "wants_no_upsell": True, "confidence": 0.8}

    if lowered in {"да", "ок", "ага", "давай", "го", "погнали", "хорошо", "конечно"}:
        return {"is_positive": True, "confidence": 0.8}

    if re.search(r"\b(вс[её] есть|сво[её]|сами|с собой|своими|свои)\b", lowered):
        return {"has_own_stuff": True, "wants_no_upsell": True, "confidence": 0.7}

    if re.search(r"\b(кальян|уголь|розжиг|реш[её]тк|шампур|посуда|л[её]д)\b", lowered):
        if not re.search(r"\b(не|без)\b", lowered):
            return {"wants_upsell": True, "confidence": 0.7}

    return {"needs_clarification": True, "confidence": 0.5}
