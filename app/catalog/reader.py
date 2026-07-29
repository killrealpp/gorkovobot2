from __future__ import annotations

from pathlib import Path
from typing import Any

from app.core.config import PROJECT_ROOT


def resolved_catalog(*, read_availability_cache: bool = False) -> dict[str, Any]:
    from app.api.public_catalog import build_public_catalog

    return build_public_catalog(read_availability_cache=read_availability_cache)


def facility_by_id(facility_id: str, *, catalog: dict[str, Any] | None = None) -> dict[str, Any] | None:
    payload = catalog or resolved_catalog()
    for facility in payload.get("facilities", []):
        if isinstance(facility, dict) and str(facility.get("id")) == str(facility_id):
            return facility
    return None


def bot_services_catalog() -> dict[str, Any]:
    catalog = resolved_catalog()
    tariffs_by_id = _by_key(catalog.get("tariffs", []), "id")
    facilities_by_service: dict[str, list[dict[str, Any]]] = {}
    for facility in catalog.get("facilities", []):
        if not isinstance(facility, dict) or facility.get("visible") is False:
            continue
        facilities_by_service.setdefault(str(facility.get("service_id") or ""), []).append(facility)

    result: dict[str, Any] = {}
    for service in catalog.get("services", []):
        if not isinstance(service, dict):
            continue
        service_id = str(service.get("id") or "")
        if not service_id:
            continue
        service_facilities = facilities_by_service.get(service_id, [])
        variants = []
        for facility in service_facilities:
            facility_tariffs = [
                tariffs_by_id[tariff_id]
                for tariff_id in facility.get("tariff_ids", [])
                if tariff_id in tariffs_by_id
            ]
            variants.append(
                {
                    "id": facility.get("id"),
                    "title": facility.get("publicTitle") or facility.get("title"),
                    "capacity_max": (facility.get("capacity") or {}).get("max"),
                    "description": facility.get("description"),
                    "warning": facility.get("warning"),
                    "mediaRefs": facility.get("mediaRefs") or [],
                    "tariffs": [_compact_tariff(tariff) for tariff in facility_tariffs],
                    "price": _min_price(facility_tariffs),
                }
            )
        result[service_id] = {
            "title": service.get("publicTitle") or service.get("title"),
            "facility_ids": [item.get("id") for item in service_facilities if item.get("id")],
            "variants": variants[:30],
            "rules": service.get("rules") or {},
        }
    return result


def bot_media_catalog() -> dict[str, Any]:
    catalog = resolved_catalog()
    result: dict[str, Any] = {}
    for media in catalog.get("media", []):
        if not isinstance(media, dict) or media.get("visible") is False:
            continue
        key = str(media.get("key") or "")
        if not key:
            continue
        result[key] = {
            "title": media.get("title") or key,
            "aliases": media.get("aliases") or [],
            "publicUrl": media.get("publicUrl"),
            "exists": bool(media.get("exists")),
        }
    return result


def resolve_media_key(value: str | None) -> str | None:
    text = _normalize(value)
    if not text:
        return None

    catalog = resolved_catalog()
    candidates: list[tuple[int, str, str]] = []
    media_keys = {str(item.get("key")) for item in catalog.get("media", []) if isinstance(item, dict) and item.get("key")}
    for media in catalog.get("media", []):
        if not isinstance(media, dict):
            continue
        key = str(media.get("key") or "")
        if not key:
            continue
        aliases = [key, media.get("title")]
        aliases.extend(media.get("aliases") or [])
        _extend_media_candidates(candidates, key, aliases)

    for facility in catalog.get("facilities", []):
        if not isinstance(facility, dict):
            continue
        media_refs = [str(item) for item in facility.get("mediaRefs", []) if str(item) in media_keys]
        if not media_refs:
            continue
        aliases = [facility.get("id"), facility.get("title"), facility.get("publicTitle")]
        aliases.extend(facility.get("aliases") or [])
        for media_key in media_refs:
            _extend_media_candidates(candidates, media_key, aliases)

    ranked = sorted(candidates, reverse=True)
    for _, alias, key in ranked:
        if text == alias:
            return key
    for _, alias, key in ranked:
        if text in alias or alias in text:
            return key
    return None


def media_path_for_key(media_key: str) -> Path | None:
    key = resolve_media_key(media_key) or media_key
    catalog = resolved_catalog()
    for media in catalog.get("media", []):
        if not isinstance(media, dict) or str(media.get("key") or "") != str(key):
            continue
        path = _resolve_public_media_path(media.get("path"))
        if path and path.exists():
            return path
    return None


def bot_facility_preview(facility_id: str) -> dict[str, Any] | None:
    catalog = resolved_catalog()
    facility = facility_by_id(facility_id, catalog=catalog)
    if facility is None:
        return None

    tariffs_by_id = _by_key(catalog.get("tariffs", []), "id")
    media_by_key = _by_key(catalog.get("media", []), "key")
    tariffs = [
        tariffs_by_id[tariff_id]
        for tariff_id in facility.get("tariff_ids", [])
        if tariff_id in tariffs_by_id
    ]
    media = [
        media_by_key[media_key]
        for media_key in facility.get("mediaRefs", [])
        if media_key in media_by_key
    ]
    preview_facility = _preview_facility(facility)
    return {
        "mode": "maxbot-local-admin",
        "facility_id": facility_id,
        "schema_version": catalog.get("schema_version"),
        "source": {
            "overridesApplied": bool((catalog.get("source") or {}).get("overridesApplied")),
        },
        "facility": preview_facility,
        "tariffs": [_preview_tariff(tariff) for tariff in tariffs],
        "media": [_preview_media(item) for item in media],
        "requested_media": [item.get("key") for item in media if item.get("key")],
        "text": format_bot_facility_text(facility, tariffs),
    }


def format_services_for_prompt() -> str:
    rows: list[str] = []
    for service_id, service in bot_services_catalog().items():
        title = service.get("title") or service_id
        rows.append(f"- {service_id}: {title}")
        for variant in service.get("variants", []):
            parts = [f"- {variant.get('title') or variant.get('id')}"]
            if variant.get("capacity_max") is not None:
                parts.append(f"до {variant.get('capacity_max')} человек")
            if variant.get("price") is not None:
                parts.append(f"от {_format_rub(variant.get('price'))}")
            if variant.get("description"):
                parts.append(str(variant.get("description")))
            if variant.get("warning"):
                parts.append(f"важно: {variant.get('warning')}")
            rows.append("  " + "; ".join(parts))
    return "\n".join(rows)


def format_media_for_prompt() -> str:
    rows = []
    for key, item in bot_media_catalog().items():
        title = item.get("title") or key
        rows.append(f"- {key}: {title}")
    return "\n".join(rows) or "Фото не настроены."


def format_bot_facility_text(facility: dict[str, Any], tariffs: list[dict[str, Any]]) -> str:
    title = str(facility.get("publicTitle") or facility.get("title") or facility.get("id") or "").strip()
    lines = [title]
    description = str(facility.get("description") or "").strip()
    capacity_max = (facility.get("capacity") or {}).get("max")
    warning = str(facility.get("warning") or "").strip()
    prices = [_int_or_none((tariff.get("price") or {}).get("amount")) for tariff in tariffs]
    prices = [price for price in prices if price is not None]

    if capacity_max is not None:
        lines.append(f"Вместимость: до {capacity_max} человек.")
    if prices:
        if len(set(prices)) == 1:
            lines.append(f"Стоимость: {_format_rub(prices[0])}.")
        else:
            lines.append(f"Стоимость: от {_format_rub(min(prices))}.")
    if description:
        lines.append(description)
    if warning:
        lines.append(f"Важно: {warning}")
    return "\n".join(lines)


def _compact_tariff(tariff: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": tariff.get("id"),
        "title": tariff.get("title"),
        "price": (tariff.get("price") or {}).get("amount"),
        "duration_minutes": tariff.get("duration_minutes"),
        "weekdays": tariff.get("weekdays") or [],
    }


def _preview_facility(facility: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": facility.get("id"),
        "service_id": facility.get("service_id"),
        "title": facility.get("title"),
        "publicTitle": facility.get("publicTitle"),
        "description": facility.get("description"),
        "warning": facility.get("warning"),
        "capacity": facility.get("capacity") or {},
        "mediaRefs": facility.get("mediaRefs") or [],
        "tariff_ids": facility.get("tariff_ids") or [],
    }


def _preview_tariff(tariff: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": tariff.get("id"),
        "title": tariff.get("title"),
        "price": tariff.get("price") or {},
        "duration_minutes": tariff.get("duration_minutes"),
        "weekdays": tariff.get("weekdays") or [],
    }


def _preview_media(media: dict[str, Any]) -> dict[str, Any]:
    return {
        "key": media.get("key"),
        "title": media.get("title"),
        "publicUrl": media.get("publicUrl"),
        "exists": bool(media.get("exists")),
    }


def _by_key(items: Any, key: str) -> dict[str, dict[str, Any]]:
    return {
        str(item.get(key)): item
        for item in items or []
        if isinstance(item, dict) and item.get(key)
    }


def _min_price(tariffs: list[dict[str, Any]]) -> int | None:
    prices = [_int_or_none((tariff.get("price") or {}).get("amount")) for tariff in tariffs]
    prices = [price for price in prices if price is not None]
    return min(prices) if prices else None


def _extend_media_candidates(candidates: list[tuple[int, str, str]], key: str, aliases: list[Any]) -> None:
    for alias in aliases:
        normalized = _normalize(alias)
        if normalized:
            candidates.append((len(normalized), normalized, key))


def _resolve_public_media_path(raw_path: Any) -> Path | None:
    if not raw_path:
        return None
    path = Path(str(raw_path))
    if not path.is_absolute():
        path = PROJECT_ROOT / path
    try:
        resolved = path.resolve()
        resolved.relative_to((PROJECT_ROOT / "app" / "images").resolve())
    except (OSError, ValueError):
        return None
    return resolved


def _normalize(value: Any) -> str:
    text = str(value or "").lower().replace("ё", "е")
    text = text.replace("№", "")
    text = text.replace("#", "")
    text = text.replace(".", "")
    return " ".join(text.split())


def _format_rub(value: Any) -> str:
    amount = _int_or_none(value)
    if amount is None:
        return ""
    return f"{amount:,}".replace(",", " ") + " ₽"


def _int_or_none(value: Any) -> int | None:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None
