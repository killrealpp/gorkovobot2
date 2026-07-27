from __future__ import annotations

from typing import Any

from app.data.admin_profile import (
    load_profile_services,
    normalize_service_type_from_profile,
)


def load_services() -> dict[str, dict[str, Any]]:
    return load_profile_services()


def service_title(service_type: str | None) -> str:
    if not service_type:
        return "услуга"
    return (load_services().get(service_type) or {}).get("title") or service_type


def service_variants(service_type: str | None) -> list[dict[str, Any]]:
    return list((load_services().get(service_type or "") or {}).get("variants") or [])


def variant_by_title(service_type: str | None, title: str | None) -> dict[str, Any] | None:
    if not title:
        return None
    normalized = title.lower().replace("ё", "е").strip()
    for variant in service_variants(service_type):
        candidate = str(variant.get("title") or "").lower().replace("ё", "е").strip()
        if candidate == normalized or normalized in candidate or candidate in normalized:
            return variant
    return None


def normalize_service_type(value: str | None) -> str | None:
    return normalize_service_type_from_profile(value)
