from __future__ import annotations

import re


_BAD_PREFIX_PATTERNS = [
    r"^\s*конечно!?\s*вот\s+(?:дружелюбный\s+)?(?:и\s+понятный\s+)?ответ(?:\s+клиенту)?[^:\n]*[:\n]+",
    r"^\s*вот\s+(?:дружелюбный\s+)?(?:и\s+понятный\s+)?ответ(?:\s+клиенту)?[^:\n]*[:\n]+",
    r"^\s*ответ\s+клиенту[^:\n]*[:\n]+",
    r"^\s*сообщение\s+клиенту[^:\n]*[:\n]+",
]

_FORBIDDEN_TECH_REPLACEMENTS = {
    r"\bYCLIENTS\b": "система бронирования",
    r"\bbackend\b": "система",
    r"\bAPI\b": "система",
    r"\bJSON\b": "данные",
    r"\baction\b": "действие",
    r"\bdatabase\b": "база",
    r"\bsystem\b": "система",
    r"системн(?:ая|ой|ую|ые|ых)?\s+провер(?:ка|ке|ку|ки)": "проверка",
    r"предоставленн(?:ых|ые|ой|ую)\s+данн(?:ых|ые|ым)": "данные",
}


_EMOJI_RE = re.compile(
    "["
    "\U0001F300-\U0001FAFF"
    "\U00002700-\U000027BF"
    "\U00002600-\U000026FF"
    "]",
    flags=re.UNICODE,
)


def _normalize_emojis(text: str) -> str:
    # The bot should sound like a calm administrator, not like a sticker pack.
    # Keep at most one useful emoji in normal replies. In sensitive/payment/price
    # messages remove decorative emojis, except a real success checkmark.
    if not text:
        return text

    lowered = text.lower().replace("ё", "е")
    sensitive = any(marker in lowered for marker in (
        "не подтвержд", "без предоплат", "не хочу", "не буду", "ошиб",
        "недоступ", "занят", "отмен", "возврат", "цена", "стоимость", "сколько стоит",
    ))
    allow_success_check = "✅" in text and any(marker in lowered for marker in ("оплату получил", "бронь подтвержд", "готово", "записала"))

    if not _EMOJI_RE.search(text):
        return text

    if sensitive and not allow_success_check:
        text = _EMOJI_RE.sub("", text)
    elif sensitive and allow_success_check:
        text = _EMOJI_RE.sub(lambda m: "✅" if m.group(0) == "✅" else "", text)
        first = text.find("✅")
        if first >= 0:
            text = text[: first + 1] + text[first + 1 :].replace("✅", "")
    else:
        kept = 0
        def repl(match: re.Match[str]) -> str:
            nonlocal kept
            kept += 1
            return match.group(0) if kept <= 1 else ""
        text = _EMOJI_RE.sub(repl, text)

    text = re.sub(r" {2,}", " ", text)
    text = re.sub(r"\n {1,}", "\n", text)
    return text.strip()


def normalize_llm_format(text: str) -> str:
    """Normalize LLM text before sending it to Telegram/MAX.

    Main production fix: if the model returns the literal sequence "\\n", convert it
    into real line breaks so MAX receives formatted multiline text.
    """
    text = (text or "").strip()
    if not text:
        return ""

    # Convert escaped newlines from JSON-like model output to real line breaks.
    text = text.replace("\\r\\n", "\n").replace("\\n", "\n")
    text = text.replace("\r\n", "\n").replace("\r", "\n")

    # Trim trailing spaces and collapse noisy blank lines.
    text = "\n".join(line.rstrip() for line in text.splitlines())
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def sanitize_reply(reply: str, *, fallback: str = "Подскажите, пожалуйста, что хотите уточнить?") -> str:
    """Приводит ответ LLM к виду сообщения клиенту, а не текста для оператора."""
    text = normalize_llm_format(reply)
    text = text.replace("**", "").replace("__", "")
    text = re.sub(r"^```(?:text|markdown)?\n?|\n?```$", "", text, flags=re.I | re.M).strip()
    text = re.sub(r"^[-–—]{3,}\s*", "", text).strip()

    changed = True
    while changed:
        changed = False
        for pattern in _BAD_PREFIX_PATTERNS:
            new_text = re.sub(pattern, "", text, flags=re.I | re.S).strip()
            if new_text != text:
                text = new_text
                changed = True

    # Если модель всё равно вернула текст-инструкцию, забираем часть после маркера/двоеточия.
    if re.search(r"ответ\s+клиенту|сообщение\s+клиенту|дружелюбный\s+ответ", text, flags=re.I):
        parts = re.split(r"[:\n]", text, maxsplit=1)
        if len(parts) == 2 and len(parts[1].strip()) >= 10:
            text = parts[1].strip()

    for pattern, replacement in _FORBIDDEN_TECH_REPLACEMENTS.items():
        text = re.sub(pattern, replacement, text, flags=re.I)

    text = re.sub(r"(?i)сейчас отправлю фото[^\n.]*[.\n]?", "", text).strip()
    text = _normalize_emojis(text)
    text = normalize_llm_format(text)

    if not text or re.fullmatch(r"[-–—*\s]+", text):
        return fallback
    return text
