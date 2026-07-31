from __future__ import annotations

from pathlib import Path
from typing import Any

from app.catalog_core.dto import ValidationResultDTO
from app.catalog_core.privacy import collect_forbidden_public_paths


PROJECT_ROOT = Path(__file__).resolve().parents[2]
PUBLIC_MEDIA_ROOT = PROJECT_ROOT / "app" / "images"
OVERRIDE_SECTIONS: frozenset[str] = frozenset({"categories", "facilities", "tariffs", "media"})

ALLOWED_SECTION_FIELDS: dict[str, frozenset[str]] = {
    "categories": frozenset({"title", "visible", "sort"}),
    "facilities": frozenset(
        {
            "title",
            "publicTitle",
            "description",
            "warning",
            "capacity",
            "mediaRefs",
            "visible",
            "sort",
        }
    ),
    "tariffs": frozenset({"title", "price", "duration_minutes", "visible", "sort"}),
    "media": frozenset({"title", "aliases", "path", "storage", "contentType", "visible", "sort"}),
}


def validate_draft_like(value: Any) -> ValidationResultDTO:
    """Validate a Catalog Core draft-like dict without runtime side effects."""

    errors: list[str] = []
    warnings: list[str] = []

    if not isinstance(value, dict):
        return ValidationResultDTO(
            ok=False,
            errors=["draft must be a JSON object"],
            warnings=warnings,
        )

    for path in collect_forbidden_public_paths(value):
        errors.append(f"secret-like key is not allowed: {path}")

    for section in sorted(OVERRIDE_SECTIONS):
        section_value = value.get(section)
        if section_value is None:
            continue
        if not isinstance(section_value, dict):
            errors.append(f"{section} must be an object keyed by id")
            continue
        for item_id, item in section_value.items():
            if not isinstance(item, dict):
                errors.append(f"{section}.{item_id} must be an object")
                continue
            _validate_override_item(section, str(item_id), item, errors, warnings)

    for key in value:
        if key in OVERRIDE_SECTIONS or key == "schema_version":
            continue
        warnings.append(f"unknown top-level key ignored: {key}")

    return ValidationResultDTO(ok=not errors, errors=errors, warnings=warnings)


def _validate_override_item(
    section: str,
    item_id: str,
    item: dict[str, Any],
    errors: list[str],
    warnings: list[str],
) -> None:
    allowed = ALLOWED_SECTION_FIELDS[section]
    for key in item:
        if key not in allowed:
            warnings.append(f"{section}.{item_id}.{key} ignored")

    if "sort" in item and _int_or_none(item.get("sort")) is None:
        errors.append(f"{section}.{item_id}.sort must be an integer")
    if "visible" in item and not isinstance(item.get("visible"), bool):
        errors.append(f"{section}.{item_id}.visible must be a boolean")

    if section == "facilities":
        _validate_facility_item(item_id, item, errors)
    elif section == "tariffs":
        _validate_tariff_item(item_id, item, errors)
    elif section == "media":
        _validate_media_item(item_id, item, errors)


def _validate_facility_item(item_id: str, item: dict[str, Any], errors: list[str]) -> None:
    if "capacity" in item and not isinstance(item.get("capacity"), dict):
        errors.append(f"facilities.{item_id}.capacity must be an object")
    if isinstance(item.get("capacity"), dict):
        capacity = item["capacity"]
        if "max" in capacity and _int_or_none(capacity.get("max")) is None:
            errors.append(f"facilities.{item_id}.capacity.max must be an integer")
    if "mediaRefs" in item and not isinstance(item.get("mediaRefs"), list):
        errors.append(f"facilities.{item_id}.mediaRefs must be an array")


def _validate_tariff_item(item_id: str, item: dict[str, Any], errors: list[str]) -> None:
    if "price" in item and not isinstance(item.get("price"), dict):
        errors.append(f"tariffs.{item_id}.price must be an object")
    if isinstance(item.get("price"), dict):
        price = item["price"]
        if "amount" in price and _int_or_none(price.get("amount")) is None:
            errors.append(f"tariffs.{item_id}.price.amount must be an integer")
    if "duration_minutes" in item and _int_or_none(item.get("duration_minutes")) is None:
        errors.append(f"tariffs.{item_id}.duration_minutes must be an integer")


def _validate_media_item(item_id: str, item: dict[str, Any], errors: list[str]) -> None:
    if "aliases" in item and not isinstance(item.get("aliases"), list):
        errors.append(f"media.{item_id}.aliases must be an array")
    if item.get("path"):
        path = _resolve_media_path(str(item["path"]))
        if path is None:
            errors.append(f"media.{item_id}.path must stay under app/images")


def _resolve_media_path(raw_path: str) -> Path | None:
    if not raw_path:
        return None
    candidate = Path(raw_path)
    if not candidate.is_absolute():
        candidate = PROJECT_ROOT / candidate
    try:
        resolved = candidate.resolve()
        resolved.relative_to(PUBLIC_MEDIA_ROOT.resolve())
    except (OSError, ValueError):
        return None
    return resolved


def _int_or_none(value: Any) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None
