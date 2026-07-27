from __future__ import annotations

import re
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml

from app.core.config import PROJECT_ROOT, get_settings


REPO_ROOT = PROJECT_ROOT
DEFAULT_PROFILE_PATH = REPO_ROOT / "business_profile" / "admin_profile.yaml"
LEGACY_KNOWLEDGE_PATH = REPO_ROOT / "app" / "ai" / "knowledge.md"
_TEMPLATE_PATTERN = re.compile(r"\{\{\s*([a-zA-Z0-9_.-]+)\s*\}\}")


def profile_path() -> Path:
    raw_path = (get_settings().admin_profile_path or "").strip()
    if not raw_path:
        return DEFAULT_PROFILE_PATH
    path = Path(raw_path)
    if not path.is_absolute():
        path = REPO_ROOT / path
    return path


@lru_cache
def load_admin_profile() -> dict[str, Any]:
    """Load the editable non-secret business profile."""

    path = profile_path()
    if not path.exists():
        raise RuntimeError(f"admin profile file does not exist: {path}")
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(data, dict):
        raise RuntimeError("admin_profile.yaml must contain a YAML mapping")
    _validate_profile(data)
    return data


def load_profile_services() -> dict[str, dict[str, Any]]:
    services = load_admin_profile().get("services") or {}
    return services if isinstance(services, dict) else {}


def profile_service_keys() -> set[str]:
    return set(load_profile_services().keys())


def service_config(service_type: str | None) -> dict[str, Any]:
    if not service_type:
        return {}
    return load_profile_services().get(str(service_type), {}) or {}


def service_requires_variant(service_type: str | None) -> bool:
    service = service_config(service_type)
    if "requires_variant" in service:
        return bool(service.get("requires_variant"))
    return bool(service.get("variants"))


def service_requires_duration(service_type: str | None) -> bool:
    service = service_config(service_type)
    if "requires_duration" in service:
        return bool(service.get("requires_duration"))
    return True


def service_requires_guests_count(service_type: str | None) -> bool:
    service = service_config(service_type)
    if "requires_guests_count" in service:
        return bool(service.get("requires_guests_count"))
    return True


def service_collect_duration_before_time(service_type: str | None) -> bool:
    service = service_config(service_type)
    return bool(service.get("collect_duration_before_time") or service.get("require_duration_before_availability"))


def service_availability_duration_minutes(service_type: str | None, requested_minutes: int | None) -> int | None:
    if requested_minutes is None:
        return None

    rules = service_config(service_type).get("price_rules") or {}
    extra_after = _int_or_none(rules.get("extra_hour_after_minutes"))
    if extra_after and requested_minutes > extra_after:
        return extra_after

    for item in rules.get("map_duration_ranges") or []:
        min_exclusive = _int_or_none(item.get("min_exclusive_minutes")) or 0
        max_inclusive = _int_or_none(item.get("max_inclusive_minutes")) or 0
        target = _int_or_none(item.get("target_duration_minutes"))
        if target and requested_minutes > min_exclusive and (not max_inclusive or requested_minutes <= max_inclusive):
            return target

    return requested_minutes


def service_public_title(service_type: str | None) -> str:
    service = service_config(service_type)
    return str(service.get("public_title") or service.get("title") or service_type or "услуга")


def normalize_service_type_from_profile(value: str | None) -> str | None:
    text = _normalize_text(value)
    if not text:
        return None

    services = load_profile_services()
    if text in services:
        return text

    alias_rows: list[tuple[int, str, str]] = []
    for key, service in services.items():
        aliases = [key, service.get("title"), service.get("public_title")]
        aliases.extend(service.get("aliases") or [])
        for variant in service.get("variants") or []:
            aliases.append(variant.get("title"))
            aliases.extend(variant.get("aliases") or [])
        for alias in aliases:
            normalized = _normalize_text(alias)
            if normalized:
                alias_rows.append((len(normalized), normalized, key))

    for _length, alias, key in sorted(alias_rows, reverse=True):
        if text == alias or alias in text:
            return key
    return None


def booking_start_reply() -> str:
    booking = load_admin_profile().get("booking") or {}
    return str(booking.get("start_reply") or "Здравствуйте! Чем могу помочь?")


def booking_question(step: str | None) -> str:
    booking = load_admin_profile().get("booking") or {}
    questions = booking.get("questions") or {}
    if step and step in questions:
        return str(questions[step])
    return str(questions.get("fallback") or "чем могу помочь?")


def booking_required_order() -> list[str]:
    booking = load_admin_profile().get("booking") or {}
    order = booking.get("required_order") or []
    return [str(item) for item in order if item]


def booking_upsell_enabled() -> bool:
    booking = load_admin_profile().get("booking") or {}
    return _bool_value(booking.get("upsell_enabled", True), default=True) and bool(addon_catalog())


def booking_max_upsell_offers() -> int:
    if not booking_upsell_enabled():
        return 0
    booking = load_admin_profile().get("booking") or {}
    try:
        value = int(booking.get("max_upsell_offers", 2))
    except (TypeError, ValueError):
        value = 2
    return max(0, min(value, 5))


def addon_catalog() -> list[dict[str, Any]]:
    items = load_admin_profile().get("addons") or []
    return items if isinstance(items, list) else []


def addon_offer_titles() -> list[str]:
    result: list[str] = []
    for addon in addon_catalog():
        title = str(addon.get("title") or addon.get("key") or "").strip()
        if title:
            result.append(title)
    return result


def addon_keyword_map() -> dict[str, tuple[str, ...]]:
    result: dict[str, tuple[str, ...]] = {}
    for addon in addon_catalog():
        key = str(addon.get("key") or addon.get("title") or "").strip()
        title = str(addon.get("title") or key).strip()
        if not key or not title:
            continue
        aliases = [key, title]
        aliases.extend(str(item) for item in addon.get("aliases") or [])
        if addon.get("description"):
            aliases.append(str(addon.get("description")))
        normalized = tuple(dict.fromkeys(_normalize_text(item) for item in aliases if _normalize_text(item)))
        if normalized:
            result[title] = normalized
    return result


def payment_prepayment_percent() -> int:
    payment = load_admin_profile().get("payment") or {}
    try:
        value = int(payment.get("prepayment_percent") or 50)
    except (TypeError, ValueError):
        value = 50
    return max(0, min(value, 100))


def media_catalog() -> dict[str, dict[str, Any]]:
    media = load_admin_profile().get("media") or {}
    items = media.get("items") or {}
    return items if isinstance(items, dict) else {}


def media_path_for_key(key: str) -> Path | None:
    item = media_catalog().get(key) or {}
    path = item.get("path")
    if not path:
        return None
    candidate = Path(str(path))
    if not candidate.is_absolute():
        candidate = REPO_ROOT / candidate
    return candidate


def resolve_media_key(value: str | None) -> str | None:
    text = _normalize_text(value)
    if not text:
        return None

    rows: list[tuple[int, str, str]] = []
    for key, item in media_catalog().items():
        aliases = [key, item.get("title")]
        aliases.extend(item.get("aliases") or [])
        for alias in aliases:
            normalized = _normalize_text(alias)
            if normalized:
                rows.append((len(normalized), normalized, key))

    for _length, alias, key in sorted(rows, reverse=True):
        if text == alias or alias in text:
            return key
    return None


def compact_media_catalog() -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, item in media_catalog().items():
        result[key] = {
            "title": item.get("title") or key,
            "aliases": item.get("aliases") or [],
        }
    return result


def media_key_for_booking(service_type: str | None, service_variant: str | None = None) -> str | None:
    if service_variant:
        variant = variant_config_by_title(service_type, service_variant) or {}
        if variant.get("media_key"):
            return str(variant.get("media_key"))
        resolved = resolve_media_key(service_variant)
        if resolved:
            return resolved
    service = service_config(service_type)
    if service.get("media_key"):
        return str(service.get("media_key"))
    return resolve_media_key(service.get("title") or service_type)


def variant_config_by_title(service_type: str | None, title: str | None) -> dict[str, Any] | None:
    if not title:
        return None
    normalized = _normalize_text(title)
    for variant in (service_config(service_type).get("variants") or []):
        aliases = [variant.get("title")]
        aliases.extend(variant.get("aliases") or [])
        for alias in aliases:
            candidate = _normalize_text(alias)
            if candidate and (candidate == normalized or normalized in candidate or candidate in normalized):
                return variant
    return None


def build_profile_knowledge() -> str:
    profile = load_admin_profile()
    lines: list[str] = []

    business = profile.get("business") or {}
    lines.append("# Профиль бизнеса")
    lines.append(f"Название: {business.get('name')}")
    lines.append(f"Тип: {business.get('type')}")
    lines.append(f"Город: {business.get('city')}")
    if business.get("address"):
        lines.append(f"Адрес/ориентир: {business.get('address')}")
    if business.get("contact_phone"):
        lines.append(f"Телефон администратора: {business.get('contact_phone')}")
    if business.get("service_area_rule"):
        lines.append(f"Правило зоны обслуживания: {business.get('service_area_rule')}")

    knowledge = profile.get("knowledge") or {}
    if knowledge.get("summary"):
        lines.extend(["", "# Общая база знаний", str(knowledge.get("summary")).strip()])

    facts = knowledge.get("facts") or []
    if facts:
        lines.extend(["", "# Факты"])
        lines.extend(f"- {fact}" for fact in facts if fact)

    services = load_profile_services()
    lines.extend(["", "# Услуги"])
    for key, service in services.items():
        lines.append(_service_context_line(key, service))
        for variant in service.get("variants") or []:
            lines.append("  " + _variant_context_line(variant))

    addons = addon_catalog()
    if addons:
        lines.extend(["", "# Дополнительные услуги"])
        for addon in addons:
            title = addon.get("title")
            price = addon.get("price")
            desc = addon.get("description")
            offer = addon.get("offer_text")
            price_text = f", {price} ₽" if price is not None else ""
            lines.append(f"- {title}{price_text}: {desc or ''} {offer or ''}".strip())

    faq = knowledge.get("faq") or []
    if faq:
        lines.extend(["", "# Частые вопросы"])
        for item in faq:
            lines.append(f"Вопрос: {item.get('question')}")
            lines.append(f"Ответ: {item.get('answer')}")

    for source in knowledge.get("source_files") or []:
        source_path = _resolve_profile_path(source)
        if source_path.exists():
            lines.extend(["", f"# Детальная база знаний: {source}"])
            lines.append(source_path.read_text(encoding="utf-8").strip())

    if not knowledge.get("source_files") and LEGACY_KNOWLEDGE_PATH.exists():
        lines.extend(["", "# Детальная база знаний: legacy app/ai/knowledge.md"])
        lines.append(LEGACY_KNOWLEDGE_PATH.read_text(encoding="utf-8").strip())

    return "\n".join(str(line).rstrip() for line in lines if line is not None).strip()


def render_template(template: str) -> str:
    values = prompt_variables()

    def replace(match: re.Match[str]) -> str:
        return str(values.get(match.group(1), ""))

    return _TEMPLATE_PATTERN.sub(replace, template)


def render_template_file(path: Path) -> str:
    if not path.exists():
        return ""
    return render_template(path.read_text(encoding="utf-8"))


def prompt_variables() -> dict[str, str]:
    profile = load_admin_profile()
    business = profile.get("business") or {}
    assistant = profile.get("assistant") or {}
    booking = profile.get("booking") or {}
    payment = profile.get("payment") or {}
    cancellation = profile.get("cancellation") or {}

    return {
        "business.identity": _business_identity(business),
        "assistant.persona": _assistant_persona(assistant, business),
        "booking.required_order": "\n".join(f"- {item}" for item in booking_required_order()),
        "booking.questions": _format_questions(booking.get("questions") or {}),
        "booking.ignored_fields": _format_list(booking.get("ignored_fields") or [], empty="нет"),
        "booking.entity_name": str(booking.get("entity_name") or "заявка"),
        "booking.action_verb": str(booking.get("action_verb") or "оформить"),
        "booking.upsell_enabled": str(booking_upsell_enabled()).lower(),
        "booking.max_upsell_offers": str(booking_max_upsell_offers()),
        "services.keys": ", ".join(load_profile_services().keys()),
        "services.catalog": _format_services_for_prompt(),
        "services.alias_rules": _format_service_aliases_for_prompt(),
        "addons.catalog": _format_addons_for_prompt(),
        "addons.offer_titles": ", ".join(addon_offer_titles()),
        "media.catalog": _format_media_for_prompt(),
        "payment.policy": _payment_policy(payment),
        "cancellation.policy": _cancellation_policy(cancellation),
        "knowledge.context": build_profile_knowledge(),
        "prompt.extra_rules": _format_prompt_rules(profile.get("prompt_rules") or {}),
        "business.contact_phone": str(business.get("contact_phone") or ""),
    }


def _validate_profile(data: dict[str, Any]) -> None:
    for key in ("business", "assistant", "booking", "services"):
        if not isinstance(data.get(key), dict):
            raise RuntimeError(f"admin_profile.yaml must contain mapping section {key!r}")
    if not data["services"]:
        raise RuntimeError("admin_profile.yaml services section must not be empty")
    media_items = ((data.get("media") or {}).get("items") or {})
    if not isinstance(media_items, dict):
        raise RuntimeError("admin_profile.yaml media.items must be a mapping when present")


def _resolve_profile_path(value: str | Path) -> Path:
    path = Path(str(value))
    if not path.is_absolute():
        path = REPO_ROOT / path
    return path


def _normalize_text(value: Any) -> str:
    text = str(value or "").lower().replace("ё", "е")
    text = text.replace("№", "")
    text = text.replace("#", "")
    text = text.replace(".", "")
    return " ".join(text.split())


def _bool_value(value: Any, *, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    text = str(value).strip().lower()
    if text in {"1", "true", "yes", "y", "on", "да"}:
        return True
    if text in {"0", "false", "no", "n", "off", "нет"}:
        return False
    return default


def _int_or_none(value: Any) -> int | None:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None


def _business_identity(business: dict[str, Any]) -> str:
    parts = [
        f"Название: {business.get('name')}",
        f"Тип: {business.get('type')}",
        f"Город: {business.get('city')}",
    ]
    if business.get("address"):
        parts.append(f"Адрес/ориентир: {business.get('address')}")
    if business.get("description"):
        parts.append(f"Описание: {business.get('description')}")
    if business.get("service_area_rule"):
        parts.append(str(business.get("service_area_rule")))
    return "\n".join(parts)


def _assistant_persona(assistant: dict[str, Any], business: dict[str, Any]) -> str:
    phrases = ", ".join(str(item) for item in assistant.get("action_phrases") or [])
    return (
        f"Ты {assistant.get('name')}, {assistant.get('role')} "
        f"{business.get('type')} «{business.get('name')}». "
        f"Тон: {assistant.get('tone')}. "
        f"Грамматический стиль: {assistant.get('gender')}. "
        f"Подтверждающие фразы: {phrases}."
    )


def _format_questions(questions: dict[str, Any]) -> str:
    return "\n".join(f"- {key}: {value}" for key, value in questions.items())


def _format_list(items: list[Any], *, empty: str) -> str:
    rows = [f"- {item}" for item in items if item]
    return "\n".join(rows) if rows else empty


def _format_services_for_prompt() -> str:
    lines: list[str] = []
    for key, service in load_profile_services().items():
        lines.append(_service_context_line(key, service))
        aliases = ", ".join(str(item) for item in service.get("aliases") or [])
        if aliases:
            lines.append(f"  Синонимы: {aliases}")
        for variant in service.get("variants") or []:
            lines.append("  " + _variant_context_line(variant))
        rules = service.get("price_rules") or {}
        if rules.get("extra_hour_instruction"):
            lines.append(f"  Правило длительности: {rules.get('extra_hour_instruction')}")
    return "\n".join(lines)


def _service_context_line(key: str, service: dict[str, Any]) -> str:
    parts = [f"- {key}: {service.get('title')}"]
    if service.get("public_title"):
        parts.append(f"клиентское название: {service.get('public_title')}")
    if service.get("capacity_max"):
        parts.append(f"до {service.get('capacity_max')} человек")
    if service.get("sleep_capacity_max"):
        parts.append(f"ночёвка до {service.get('sleep_capacity_max')} человек")
    if service.get("price") is not None:
        parts.append(f"{service.get('price')} ₽")
    if service.get("requires_variant"):
        parts.append("нужно выбрать вариант")
    return "; ".join(parts)


def _variant_context_line(variant: dict[str, Any]) -> str:
    parts = [f"- {variant.get('title')}"]
    if variant.get("capacity_max"):
        parts.append(f"до {variant.get('capacity_max')} человек")
    if variant.get("duration_minutes"):
        parts.append(f"{int(variant.get('duration_minutes')) // 60} ч")
    if variant.get("weekdays"):
        parts.append(f"дни недели {variant.get('weekdays')}")
    if variant.get("price") is not None:
        parts.append(f"{variant.get('price')} ₽")
    return "; ".join(parts)


def _format_service_aliases_for_prompt() -> str:
    rows: list[str] = []
    for key, service in load_profile_services().items():
        aliases = [service.get("title"), service.get("public_title")]
        aliases.extend(service.get("aliases") or [])
        aliases = [str(item) for item in aliases if item]
        if aliases:
            rows.append(f"- {key}: {', '.join(aliases)}")
    return "\n".join(rows)


def _format_addons_for_prompt() -> str:
    lines: list[str] = []
    for addon in addon_catalog():
        price = addon.get("price")
        price_text = f" — {price} ₽" if price is not None else ""
        lines.append(f"- {addon.get('title')}{price_text}: {addon.get('description') or ''}. {addon.get('offer_text') or ''}".strip())
    return "\n".join(lines) or "Дополнительные услуги не настроены."


def _format_media_for_prompt() -> str:
    rows: list[str] = []
    for key, item in media_catalog().items():
        title = item.get("title") or key
        rows.append(f"- {key}: {title}")
    return "\n".join(rows) or "Фото не настроены."


def _payment_policy(payment: dict[str, Any]) -> str:
    if not payment.get("enabled", True):
        return "Онлайн-предоплата выключена; не обещай ссылку на оплату."
    return (
        f"Предоплата: {payment.get('prepayment_percent', 50)}%. "
        f"{payment.get('confirmation_rule') or ''} "
        f"{payment.get('link_lifetime_text') or ''} "
        f"Если клиент оплатит позже: {payment.get('later_payment_text') or ''}"
    ).strip()


def _cancellation_policy(cancellation: dict[str, Any]) -> str:
    return str(cancellation.get("policy_text") or "Отмена и перенос только после подтверждения клиента.")


def _format_prompt_rules(rules: dict[str, Any]) -> str:
    lines: list[str] = []
    for section in ("universal", "project_specific"):
        for item in rules.get(section) or []:
            lines.append(f"- {item}")
    return "\n".join(lines)
