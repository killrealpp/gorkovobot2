from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from app.dialog.state import BookingDraft


_ALIASES = {
    "service": "service_type",
    "variant": "service_variant",
    "guests": "guests_count",
    "format": "event_format",
    "name": "client_name",
}

# Only these fields may come from an LLM interpretation of a customer message.
# Payment state, pending actions, holds and external ids are engine-owned.
_CUSTOMER_FIELDS = {
    "service_type",
    "service_variant",
    "date",
    "time",
    "duration",
    "guests_count",
    "event_format",
    "upsell_items",
    "upsell_offer_count",
    "upsell_done",
    "client_name",
    "phone",
}

_STEP_FIELD = {
    "service_type": "service_type",
    "date": "date",
    "service_variant": "service_variant",
    "time": "time",
    "duration": "duration",
    "guests_count": "guests_count",
    "upsell_items": "upsell_items",
    "client_name": "client_name",
    "phone": "phone",
}

_QUESTION_WORDS = (
    "сколько",
    "какая",
    "какой",
    "какие",
    "почему",
    "зачем",
    "где",
    "когда",
    "цена",
    "стоимость",
    "можно ли",
    "есть ли",
)

_NUMBER_WORDS = (
    "один", "одна", "одно", "два", "две", "три", "четыре", "пять",
    "шесть", "семь", "восемь", "девять", "десять", "одиннадцать",
    "двенадцать", "тринадцать", "четырнадцать", "пятнадцать",
    "шестнадцать", "семнадцать", "восемнадцать", "девятнадцать",
    "двадцать", "тридцать",
)

_GAZEBO_ORDINALS = {
    "перв": 1, "втор": 2, "трет": 3, "четверт": 4,
    "пят": 5, "шест": 6, "восьм": 8,
}
_GAZEBO_NUMBERS = {1, 2, 3, 4, 5, 6, 8}


@dataclass
class TransitionValidation:
    patch: dict[str, Any] = field(default_factory=dict)
    rejected: dict[str, str] = field(default_factory=dict)

    @property
    def changed(self) -> bool:
        return bool(self.patch)


def validate_transition_patch(
    draft: BookingDraft,
    patch: dict[str, Any] | None,
    *,
    user_text: str,
    intent: str | None = None,
) -> TransitionValidation:
    """Keep only customer fields supported by the current message and state.

    The LLM still performs semantic extraction. This validator deliberately accepts
    natural answers at the expected step (for example, "в пять часов вечера" or
    "8 взрослых и 2 ребёнка") while rejecting fields copied from stale context.
    """
    result = TransitionValidation()
    if not isinstance(patch, dict):
        return result

    step = draft.next_step()
    expected_field = _STEP_FIELD.get(step or "")
    text = _normalize_text(user_text)

    for raw_key, value in patch.items():
        key = _ALIASES.get(str(raw_key), str(raw_key))
        if key not in _CUSTOMER_FIELDS:
            result.rejected[key] = "engine_owned_or_unknown"
            continue

        if key == "service_variant":
            normalized_variant = _normalize_text(str(value or ""))
            if normalized_variant in {"беседка", "обычная беседка", "gazebo"}:
                result.rejected[key] = "generic_variant_is_not_a_booking_object"
                continue

            contextual_variant = contextual_gazebo_variant_from_text(text)
            if (
                draft.service_type == "gazebo"
                and contextual_variant
                and _same_value(contextual_variant, value)
            ):
                result.patch[key] = contextual_variant
                continue

        if _same_value(getattr(draft, key, None), value):
            continue

        if _field_supported(
            key,
            text=text,
            expected_field=expected_field,
            intent=str(intent or ""),
            patch=patch,
        ):
            result.patch[key] = value
        else:
            result.rejected[key] = "no_evidence_in_current_turn"

    return result


def is_bare_non_value_reply(text: str) -> bool:
    normalized = _normalize_text(text)
    return normalized in {
        "да",
        "ага",
        "угу",
        "ок",
        "окей",
        "хорошо",
        "нет",
        "не",
        "неа",
    }


def _field_supported(
    key: str,
    *,
    text: str,
    expected_field: str | None,
    intent: str,
    patch: dict[str, Any],
) -> bool:
    if key in {"upsell_items", "upsell_offer_count", "upsell_done"}:
        return expected_field == "upsell_items" or _has_upsell_evidence(text)

    if key == "event_format":
        return _has_event_format_evidence(text)

    # Contact details commonly arrive together: "Иван, 8910...". Check this
    # before the stricter current-step rule because the whole message is not a
    # plain name even though it contains a valid one.
    if key == "client_name" and _has_phone_evidence(text):
        return bool(re.search(r"[а-яa-z]", text))

    # At the field currently being collected, accept natural language only when
    # the turn actually contains evidence for that value. This prevents a stale
    # LLM value from being accepted for messages such as "так он же занят".
    if key == expected_field:
        if is_bare_non_value_reply(text):
            return False
        if _has_specific_evidence(key, text):
            return True
        return _is_simple_expected_value(key, text) and not _looks_like_question(text)

    # Multiple booking values may be supplied in one natural sentence. Outside
    # the current step each changed field needs its own evidence in this turn.
    return _has_specific_evidence(key, text)


def _has_specific_evidence(key: str, text: str) -> bool:
    checks = {
        "service_type": _has_service_evidence,
        "service_variant": _has_variant_evidence,
        "date": _has_date_evidence,
        "time": _has_time_evidence,
        "duration": _has_duration_evidence,
        "guests_count": _has_guests_evidence,
        "client_name": _has_name_evidence,
        "phone": _has_phone_evidence,
    }
    check = checks.get(key)
    return bool(check and check(text))


def _has_service_evidence(text: str) -> bool:
    return any(marker in f" {text} " for marker in ("бесед", "бан", "гостев", " дом ", "тепл"))


def _has_variant_evidence(text: str) -> bool:
    return bool(
        "бесед" in text
        and (
            re.search(r"(?:№|#|номер\s*)?\d{1,2}", text)
            or any(marker in text for marker in ("крыт", "закрыт", "тепл"))
            or any(marker in text for marker in (
                "перв", "втор", "трет", "четверт", "пят", "шест", "седьм", "восьм", "девят",
            ))
        )
    )


def contextual_gazebo_variant_from_text(text: str) -> str | None:
    """Resolve a numbered gazebo from a contextual answer such as 'шестую'."""
    normalized = _normalize_text(text)

    ordinal_words = "|".join(rf"{root}[а-я]*" for root in _GAZEBO_ORDINALS)
    # A correction often mentions both values: «не первую, а шестую». Remove
    # explicitly rejected choices before resolving the desired one.
    normalized = re.sub(
        rf"\bне\s+(?:беседк[а-я]*\s*)?(?:(?:№|#|номер\s*)?\d{{1,2}}|(?:{ordinal_words}))\b",
        " ",
        normalized,
    )

    desired_patterns = (
        r"(?:давайте|давай|берем|берём|возьмем|возьмём|мне|нужна|хочу)\s+(?:беседк\w*\s*)?(?:№|#|номер\s*)?(\d{1,2})",
        r"(?:^|\bа\b|тогда|лучше)\s+(?:беседк\w*\s*)?(?:№|#|номер\s*)?(\d{1,2})",
        r"(?:беседк\w*\s*)(?:№|#|номер\s*)?(\d{1,2})",
        r"(?:№|#)\s*(\d{1,2})",
    )
    for pattern in desired_patterns:
        match = re.search(pattern, normalized)
        if match:
            number = int(match.group(1))
            if number in _GAZEBO_NUMBERS:
                return f"Беседка №{number}"

    for root, number in _GAZEBO_ORDINALS.items():
        if re.search(rf"\b{root}[а-я]*\b", normalized):
            return f"Беседка №{number}"

    if "бесед" in normalized:
        match = re.search(r"(?:№|#|номер\s*)?(\d{1,2})", normalized)
        if match and int(match.group(1)) in _GAZEBO_NUMBERS:
            return f"Беседка №{int(match.group(1))}"
    return None


def _has_date_evidence(text: str) -> bool:
    if any(marker in text for marker in (
        "сегодня", "завтра", "послезавтра", "выходн", "будн",
        "понедель", "вторник", "сред", "четверг", "пятниц", "суббот", "воскрес",
        "январ", "феврал", "март", "апрел", "мая", "май", "июн", "июл",
        "август", "сентябр", "октябр", "ноябр", "декабр", "числ", "дат",
    )):
        return True
    return bool(
        re.search(r"\b\d{1,2}[./-]\d{1,2}(?:[./-]\d{2,4})?\b", text)
        or re.search(r"\b(?:на|с)\s+\d{1,2}\b", text)
    )


def _has_time_evidence(text: str) -> bool:
    if re.search(r"\b\d{1,2}\s*[:.]\s*\d{2}\b", text):
        return True
    if any(marker in text for marker in ("утра", "дня", "вечера", "ночи", "полдень", "полноч")):
        return True
    if re.search(r"\b(?:в|к|с)\s+(?:\d{1,2}|[а-я]+)(?:\s+час(?:а|ов)?)?\b", text):
        return True
    return bool(re.search(r"\b\d{1,2}\s*(?:час(?:а|ов)?|ч)\b", text))


def _has_duration_evidence(text: str) -> bool:
    if any(marker in text for marker in (
        "на сутки", "суток", "сутки", "на ночь", "до утра", "часа", "часов", " час", "длитель",
    )):
        return True
    return bool(
        re.search(r"\bна\s+(?:\d{1,2}|[а-я]+)\s*(?:ч\b|час)", text)
        or re.search(r"\bс\s+.+?\s+до\s+.+", text)
    )


def _has_guests_evidence(text: str) -> bool:
    return any(marker in text for marker in (
        "гост", "человек", "взросл", "ребен", "ребён", "детей", "ребят", "нас будет", "будет нас",
        "вдвоем", "вдвоём", "втроем", "втроём", "четвером", "пятером", "шестером",
    ))


def _has_name_evidence(text: str) -> bool:
    return any(marker in text for marker in ("меня зовут", "имя", "запишите", "запиши", "на имя"))


def _has_phone_evidence(text: str) -> bool:
    digits = re.sub(r"\D", "", text)
    return len(digits) >= 10


def _has_event_format_evidence(text: str) -> bool:
    return any(marker in text for marker in (
        "день рожден", "день рождён", "корпоратив", "свадьб", "юбилей", "встреч", "обычный отдых",
    ))


def _has_upsell_evidence(text: str) -> bool:
    return any(marker in text for marker in (
        "угол", "розжиг", "решет", "решёт", "шампур", "посуд", "кальян", "лед", "лёд", "без доп",
    ))


def _looks_like_question(text: str) -> bool:
    return "?" in text or any(word in text for word in _QUESTION_WORDS)


def _is_simple_expected_value(key: str, text: str) -> bool:
    """Allow terse answers while requiring semantics for structured fields."""
    if not text or len(text.split()) > 5:
        return False
    if key == "client_name":
        return bool(re.fullmatch(r"[а-яa-z][а-яa-z\s-]{1,80}", text))
    if key == "phone":
        return _has_phone_evidence(text)
    if key in {"date", "time", "duration", "guests_count", "service_variant"}:
        return bool(re.fullmatch(r"\d{1,2}", text) or any(word in text.split() for word in _NUMBER_WORDS))
    return False


def _same_value(current: Any, proposed: Any) -> bool:
    if current is proposed:
        return True
    if isinstance(current, (int, float)) or isinstance(proposed, (int, float)):
        try:
            return float(current) == float(proposed)
        except (TypeError, ValueError):
            pass
    return str(current or "").strip().lower() == str(proposed or "").strip().lower()


def _normalize_text(text: str) -> str:
    normalized = (text or "").lower().replace("ё", "е")
    normalized = re.sub(r"\s+", " ", normalized)
    return normalized.strip()
