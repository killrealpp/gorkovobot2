from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import quote

from fastapi import HTTPException
from starlette.responses import FileResponse

from app.core.config import PROJECT_ROOT, get_settings
from app.data.admin_profile import (
    load_admin_profile,
    media_catalog,
    profile_path,
)
from app.data.services import load_services
from app.storage import sqlite


PUBLIC_CATALOG_PATH = "/api/public/catalog"
PUBLIC_MEDIA_PREFIX = "/api/public/media"
DEFAULT_AVAILABILITY_MAX_AGE_SECONDS = 15 * 60
AVAILABILITY_ROW_LIMIT = 20000
PUBLIC_MEDIA_ROOT = PROJECT_ROOT / "app" / "images"
OVERRIDE_SECTIONS = {"categories", "facilities", "tariffs", "media"}
SECRET_KEY_MARKERS = (
    "payment",
    "post_payment",
    "token",
    "secret",
    "api_key",
    "access_key",
    "password",
    "supabase",
    "service_role",
    "anon_key",
    "yclients",
    "yookassa",
    "payment_secret_key",
    "yclients_partner_token",
    "yclients_user_token",
    "openrouter_api_key",
    "max_bot_token",
    "db_password",
)


def register_public_catalog_routes(app: Any) -> None:
    @app.get(PUBLIC_CATALOG_PATH)
    async def public_catalog() -> dict[str, Any]:
        # Keep the public catalog endpoint a fast catalog-only read model.
        # Cached availability remains available to explicit builder callers only;
        # live availability/booking is a separate backend stage.
        return build_public_catalog(read_availability_cache=False)

    @app.get(f"{PUBLIC_MEDIA_PREFIX}/{{media_key:path}}")
    async def public_media(media_key: str) -> FileResponse:
        response = public_media_response(media_key)
        if response is None:
            raise HTTPException(status_code=404, detail="media not found")
        return response


def public_media_response(media_key: str) -> FileResponse | None:
    path = _public_media_path_for_key(media_key, overrides=load_catalog_overrides())
    if path is None:
        return None
    return FileResponse(path, media_type=_content_type(path))


def build_public_catalog(
    *,
    availability_max_age_seconds: int = DEFAULT_AVAILABILITY_MAX_AGE_SECONDS,
    generated_at: str | None = None,
    read_availability_cache: bool = True,
    include_overrides: bool = True,
) -> dict[str, Any]:
    profile = load_admin_profile()
    services_map = load_services()
    overrides = load_catalog_overrides() if include_overrides else {}
    media_items = _build_media_items(overrides=overrides)
    media_by_key = {item["key"]: item for item in media_items}
    if read_availability_cache:
        availability_state, availability_rows, availability_warnings = _availability_snapshot(
            max_age_seconds=availability_max_age_seconds,
        )
    else:
        availability_state, availability_rows, availability_warnings = _empty_availability_snapshot()
    categories: list[dict[str, Any]] = []
    services: list[dict[str, Any]] = []
    facilities: list[dict[str, Any]] = []
    variants: list[dict[str, Any]] = []
    tariffs: list[dict[str, Any]] = []
    warnings: list[dict[str, Any]] = list(availability_warnings)

    for service_type, service in services_map.items():
        service_facility_ids: list[str] = []
        service_variant_ids: list[str] = []
        service_tariff_ids: list[str] = []
        service_warnings: list[dict[str, Any]] = []
        service_variants = list(service.get("variants") or [])

        category = _category_contract(service_type, service)
        categories.append(category)

        if _service_variants_are_facilities(service):
            for index, variant in enumerate(service_variants):
                variant_id = _variant_id(service_type, index, variant)
                facility_id = str(variant.get("facility_id") or variant_id)
                refs = _combined_integration_refs(variant, [])
                media_refs = _media_refs_for_key(str(variant.get("media_key") or ""), media_by_key)
                tariff = _tariff_contract(
                    service_type=service_type,
                    facility_id=facility_id,
                    variant_id=variant_id,
                    source=variant,
                    service=service,
                    index=index,
                )
                item_warnings = _entity_warnings(
                    entity_type="facility",
                    entity_id=facility_id,
                    refs=refs,
                    media_refs=media_refs,
                    has_tariffs=bool(tariff),
                )
                if tariff:
                    tariffs.append(tariff)
                    service_tariff_ids.append(tariff["id"])

                variants.append(
                    _clean_none(
                        {
                            "id": variant_id,
                            "kind": "facility",
                            "service_id": service_type,
                            "facility_id": facility_id,
                            "title": str(variant.get("title") or facility_id),
                            "aliases": _string_list(variant.get("aliases")),
                            "mediaRefs": media_refs,
                            "tariff_ids": [tariff["id"]] if tariff else [],
                            "rules": _variant_rules(variant),
                            "warnings": item_warnings,
                        }
                    )
                )
                facilities.append(
                    _facility_contract(
                        service_type=service_type,
                        facility_id=facility_id,
                        title=str(variant.get("title") or facility_id),
                        service=service,
                        source=variant,
                        refs=refs,
                        media_refs=media_refs,
                        tariff_ids=[tariff["id"]] if tariff else [],
                        availability_rows=availability_rows,
                        global_availability=availability_state,
                        warnings=item_warnings,
                    )
                )
                service_facility_ids.append(facility_id)
                service_variant_ids.append(variant_id)
                warnings.extend(item_warnings)
        else:
            facility_id = str(service.get("facility_id") or "")
            refs_sources = service_variants if service_variants else []
            refs = _combined_integration_refs(service, refs_sources)
            media_refs = _media_refs_for_key(str(service.get("media_key") or ""), media_by_key)

            for index, variant in enumerate(service_variants):
                variant_id = _variant_id(service_type, index, variant)
                tariff = _tariff_contract(
                    service_type=service_type,
                    facility_id=facility_id or service_type,
                    variant_id=variant_id,
                    source=variant,
                    service=service,
                    index=index,
                )
                if tariff:
                    tariffs.append(tariff)
                    service_tariff_ids.append(tariff["id"])
                variants.append(
                    _clean_none(
                        {
                            "id": variant_id,
                            "kind": "tariff_option",
                            "service_id": service_type,
                            "facility_id": facility_id or None,
                            "title": str(variant.get("title") or variant_id),
                            "aliases": _string_list(variant.get("aliases")),
                            "tariff_ids": [tariff["id"]] if tariff else [],
                            "rules": _variant_rules(variant),
                        }
                    )
                )
                service_variant_ids.append(variant_id)

            if not service_variants and service.get("price") is not None:
                tariff = _tariff_contract(
                    service_type=service_type,
                    facility_id=facility_id or service_type,
                    variant_id=None,
                    source=service,
                    service=service,
                    index=0,
                )
                if tariff:
                    tariffs.append(tariff)
                    service_tariff_ids.append(tariff["id"])

            has_public_facility = bool(facility_id and (refs.get("yclients_service_id") or refs.get("yclients_service_ids")))
            has_public_facility = has_public_facility or bool(facility_id and media_refs)
            if has_public_facility:
                item_warnings = _entity_warnings(
                    entity_type="facility",
                    entity_id=facility_id,
                    refs=refs,
                    media_refs=media_refs,
                    has_tariffs=bool(service_tariff_ids),
                )
                facilities.append(
                    _facility_contract(
                        service_type=service_type,
                        facility_id=facility_id,
                        title=_service_facility_title(service_type, service),
                        service=service,
                        source=service,
                        refs=refs,
                        media_refs=media_refs,
                        tariff_ids=service_tariff_ids,
                        availability_rows=availability_rows,
                        global_availability=availability_state,
                        warnings=item_warnings,
                    )
                )
                service_facility_ids.append(facility_id)
                warnings.extend(item_warnings)
            else:
                service_warnings.append(
                    {
                        "code": "not_exposed_as_facility",
                        "message": "Service has no concrete public facility, media, or public tariff in the current profile.",
                    }
                )

        if not service_facility_ids and not service_tariff_ids:
            service_warnings.append(
                {
                    "code": "incomplete_public_catalog_item",
                    "message": "Service is present in the MaxBot profile but lacks enough data for a bookable public catalog item.",
                }
            )
        warnings.extend({"entityType": "service", "entityId": service_type, **item} for item in service_warnings)

        services.append(
            _clean_none(
                {
                    "id": service_type,
                    "category_id": category["id"],
                    "title": str(service.get("title") or service_type),
                    "publicTitle": service.get("public_title"),
                    "aliases": _string_list(service.get("aliases")),
                    "facility_ids": service_facility_ids,
                    "variant_ids": service_variant_ids,
                    "tariff_ids": service_tariff_ids,
                    "rules": _service_rules(service),
                    "warnings": service_warnings,
                }
            )
        )

    business = profile.get("business") or {}
    catalog = _clean_none(
        {
            "schema_version": "public_catalog.v1",
            "generated_at": generated_at or _utc_now_iso(),
            "source": {
                "catalog": "business_profile.admin_profile",
                "profile_path": _relative_path(profile_path()),
                "overridesApplied": bool(overrides),
                "availability": "mvp_availability_cache",
                "liveAvailabilityChecked": False,
            },
            "business": {
                "name": business.get("name"),
                "type": business.get("type"),
                "city": business.get("city"),
                "address": business.get("address"),
                "description": business.get("description"),
                "contact_phone": business.get("contact_phone"),
                "service_area_rule": business.get("service_area_rule"),
            },
            "categories": categories,
            "services": services,
            "facilities": facilities,
            "variants": variants,
            "tariffs": tariffs,
            "media": media_items,
            "availability_state": availability_state,
            "publicBookingPolicy": {
                "mode": "catalog-only",
                "readOnly": True,
                "bookingWritesEnabled": False,
                "liveAvailabilityChecked": False,
                "availabilityCacheState": availability_state.get("cacheState"),
            },
            "warnings": _dedupe_warnings(warnings),
        }
    )
    return apply_catalog_overrides(catalog, overrides) if include_overrides and overrides else catalog


def catalog_overrides_path() -> Path:
    raw_path = Path(str(get_settings().catalog_overrides_path or "data/catalog_overrides.local.json"))
    if raw_path.is_absolute():
        return raw_path
    return PROJECT_ROOT / raw_path


def load_catalog_overrides() -> dict[str, Any]:
    path = catalog_overrides_path()
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {}
    return raw if isinstance(raw, dict) else {}


def save_catalog_overrides(overrides: dict[str, Any]) -> None:
    validation = validate_catalog_overrides(overrides)
    if not validation["ok"]:
        raise ValueError("; ".join(validation["errors"]))
    path = catalog_overrides_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(overrides, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def validate_catalog_overrides(value: Any) -> dict[str, Any]:
    errors: list[str] = []
    warnings: list[str] = []
    if not isinstance(value, dict):
        return {"ok": False, "errors": ["draft must be a JSON object"], "warnings": warnings}

    _collect_secret_key_errors(value, errors)
    for section in OVERRIDE_SECTIONS:
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

    return {"ok": not errors, "errors": errors, "warnings": warnings}


def apply_catalog_overrides(catalog: dict[str, Any], overrides: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(overrides, dict) or not overrides:
        return catalog
    result = json.loads(json.dumps(catalog, ensure_ascii=False))

    _apply_collection_overrides(result, "categories", "id", overrides.get("categories"))
    _apply_collection_overrides(result, "facilities", "id", overrides.get("facilities"))
    _apply_collection_overrides(result, "tariffs", "id", overrides.get("tariffs"))
    _apply_collection_overrides(result, "media", "key", overrides.get("media"))

    source = result.setdefault("source", {})
    if isinstance(source, dict):
        source["overridesApplied"] = True
    return _clean_none(result)


def _validate_override_item(
    section: str,
    item_id: str,
    item: dict[str, Any],
    errors: list[str],
    warnings: list[str],
) -> None:
    allowed = {
        "categories": {"title", "visible", "sort"},
        "facilities": {
            "title",
            "publicTitle",
            "description",
            "warning",
            "capacity",
            "mediaRefs",
            "visible",
            "sort",
        },
        "tariffs": {"title", "price", "duration_minutes", "visible", "sort"},
        "media": {"title", "aliases", "path", "storage", "contentType", "visible", "sort"},
    }[section]
    for key in item:
        if key not in allowed:
            warnings.append(f"{section}.{item_id}.{key} ignored")
    if "sort" in item and _int_or_none(item.get("sort")) is None:
        errors.append(f"{section}.{item_id}.sort must be an integer")
    if "visible" in item and not isinstance(item.get("visible"), bool):
        errors.append(f"{section}.{item_id}.visible must be a boolean")
    if section == "facilities" and "capacity" in item and not isinstance(item.get("capacity"), dict):
        errors.append(f"facilities.{item_id}.capacity must be an object")
    if section == "facilities" and isinstance(item.get("capacity"), dict):
        capacity = item["capacity"]
        if "max" in capacity and _int_or_none(capacity.get("max")) is None:
            errors.append(f"facilities.{item_id}.capacity.max must be an integer")
    if section == "facilities" and "mediaRefs" in item and not isinstance(item.get("mediaRefs"), list):
        errors.append(f"facilities.{item_id}.mediaRefs must be an array")
    if section == "tariffs" and "price" in item and not isinstance(item.get("price"), dict):
        errors.append(f"tariffs.{item_id}.price must be an object")
    if section == "tariffs" and isinstance(item.get("price"), dict):
        price = item["price"]
        if "amount" in price and _int_or_none(price.get("amount")) is None:
            errors.append(f"tariffs.{item_id}.price.amount must be an integer")
    if section == "tariffs" and "duration_minutes" in item and _int_or_none(item.get("duration_minutes")) is None:
        errors.append(f"tariffs.{item_id}.duration_minutes must be an integer")
    if section == "media" and "aliases" in item and not isinstance(item.get("aliases"), list):
        errors.append(f"media.{item_id}.aliases must be an array")
    if section == "media" and item.get("path"):
        path = _resolve_media_path(str(item["path"]))
        if path is None:
            errors.append(f"media.{item_id}.path must stay under app/images")


def _collect_secret_key_errors(value: Any, errors: list[str], path: str = "$") -> None:
    if isinstance(value, dict):
        for key, nested in value.items():
            lowered = str(key).lower()
            if any(marker in lowered for marker in SECRET_KEY_MARKERS):
                errors.append(f"secret-like key is not allowed: {path}.{key}")
            _collect_secret_key_errors(nested, errors, f"{path}.{key}")
    elif isinstance(value, list):
        for index, nested in enumerate(value):
            _collect_secret_key_errors(nested, errors, f"{path}[{index}]")


def _apply_collection_overrides(
    catalog: dict[str, Any],
    section: str,
    identity_key: str,
    overrides: Any,
) -> None:
    if not isinstance(overrides, dict):
        return
    collection = catalog.get(section)
    if not isinstance(collection, list):
        return
    by_id = {str(item.get(identity_key)): item for item in collection if isinstance(item, dict)}
    for item_id, override in overrides.items():
        if not isinstance(override, dict):
            continue
        item = by_id.get(str(item_id))
        if item is None and section == "media":
            item = {"key": str(item_id)}
            collection.append(item)
            by_id[str(item_id)] = item
        if item is None:
            continue
        if section == "categories":
            _apply_category_override(item, override)
        elif section == "facilities":
            _apply_facility_override(item, override)
        elif section == "tariffs":
            _apply_tariff_override(item, override)
        elif section == "media":
            _apply_media_override(item, override)

    indexed = list(enumerate(collection))
    indexed.sort(key=lambda pair: _sort_key(pair[1], pair[0]))
    collection[:] = [item for _, item in indexed]


def _apply_category_override(item: dict[str, Any], override: dict[str, Any]) -> None:
    _copy_scalar_overrides(item, override, {"title", "visible", "sort"})


def _apply_facility_override(item: dict[str, Any], override: dict[str, Any]) -> None:
    _copy_scalar_overrides(item, override, {"title", "publicTitle", "description", "warning", "visible", "sort"})
    capacity = override.get("capacity")
    if isinstance(capacity, dict):
        item_capacity = item.setdefault("capacity", {})
        if isinstance(item_capacity, dict) and "max" in capacity:
            item_capacity["max"] = _int_or_none(capacity.get("max"))
    if "mediaRefs" in override:
        item["mediaRefs"] = _string_list(override.get("mediaRefs"))


def _apply_tariff_override(item: dict[str, Any], override: dict[str, Any]) -> None:
    _copy_scalar_overrides(item, override, {"title", "visible", "sort"})
    price = override.get("price")
    if isinstance(price, dict):
        item_price = item.setdefault("price", {})
        if isinstance(item_price, dict) and "amount" in price:
            item_price["amount"] = _int_or_none(price.get("amount"))
    if "duration_minutes" in override:
        duration_minutes = _int_or_none(override.get("duration_minutes"))
        item["duration_minutes"] = duration_minutes
        item["duration_hours"] = _duration_hours(duration_minutes)


def _apply_media_override(item: dict[str, Any], override: dict[str, Any]) -> None:
    _copy_scalar_overrides(item, override, {"title", "storage", "contentType", "visible", "sort"})
    if "aliases" in override:
        item["aliases"] = _string_list(override.get("aliases"))
    if "path" in override:
        path = _resolve_media_path(str(override.get("path") or ""))
        item["path"] = _relative_path(path) if path else None
    path = _path_for_media_item(item)
    public_path = _public_media_path_from_path(path)
    item["publicUrl"] = public_media_url(str(item["key"]))
    item["exists"] = bool(public_path and public_path.exists())
    if item.get("contentType") is None:
        item["contentType"] = _content_type(path)


def _copy_scalar_overrides(item: dict[str, Any], override: dict[str, Any], keys: set[str]) -> None:
    for key in keys:
        if key in override:
            item[key] = override[key]


def _sort_key(item: Any, index: int) -> tuple[int, int, str]:
    if not isinstance(item, dict):
        return (1, index, "")
    sort = _int_or_none(item.get("sort"))
    identity = str(item.get("title") or item.get("key") or item.get("id") or "")
    if sort is None:
        return (1, index, identity)
    return (0, sort, identity)


def _category_contract(service_type: str, service: dict[str, Any]) -> dict[str, Any]:
    return _clean_none(
        {
            "id": service_type,
            "title": str(service.get("public_title") or service.get("title") or service_type),
            "service_ids": [service_type],
        }
    )


def _facility_contract(
    *,
    service_type: str,
    facility_id: str,
    title: str,
    service: dict[str, Any],
    source: dict[str, Any],
    refs: dict[str, Any],
    media_refs: list[str],
    tariff_ids: list[str],
    availability_rows: list[dict[str, Any]],
    global_availability: dict[str, Any],
    warnings: list[dict[str, Any]],
) -> dict[str, Any]:
    return _clean_none(
        {
            "id": facility_id,
            "service_id": service_type,
            "title": title,
            "publicTitle": _facility_public_title(title, service, source),
            "aliases": _facility_aliases(service, source),
            "capacity": _capacity_contract(service, source),
            "mediaRefs": media_refs,
            "tariff_ids": tariff_ids,
            "rules": _facility_rules(service, source),
            "availability_state": _availability_for_refs(
                refs=refs,
                rows=availability_rows,
                global_state=global_availability,
            ),
            "warnings": warnings,
        }
    )


def _tariff_contract(
    *,
    service_type: str,
    facility_id: str,
    variant_id: str | None,
    source: dict[str, Any],
    service: dict[str, Any],
    index: int,
) -> dict[str, Any] | None:
    price = source.get("price")
    if price is None:
        return None
    duration_minutes = _int_or_none(source.get("duration_minutes"))
    if duration_minutes is None:
        duration_minutes = _int_or_none(service.get("default_duration_minutes"))
    title = str(source.get("title") or service.get("title") or facility_id)
    tariff_id = _tariff_id(
        service_type=service_type,
        facility_id=facility_id,
        title=title,
        duration_minutes=duration_minutes,
        weekdays=source.get("weekdays"),
        index=index,
    )
    return _clean_none(
        {
            "id": tariff_id,
            "service_id": service_type,
            "facility_id": facility_id,
            "title": title,
            "price": {
                "amount": _int_or_none(price),
                "currency": "RUB",
            },
            "duration_minutes": duration_minutes,
            "duration_hours": _duration_hours(duration_minutes),
            "weekdays": _string_or_int_list(source.get("weekdays")),
        }
    )


def _build_media_items(*, overrides: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for key, item in _effective_media_catalog(overrides).items():
        path = _path_for_media_item(item)
        public_path = _public_media_path_for_key(key, overrides=overrides)
        items.append(
            _clean_none(
                {
                    "key": key,
                    "title": item.get("title") or key,
                    "aliases": _string_list(item.get("aliases")),
                    "publicUrl": public_media_url(key),
                    "path": _relative_path(path) if path else None,
                    "storage": item.get("storage") or "local-file",
                    "exists": bool(public_path and public_path.exists()),
                    "contentType": item.get("contentType") or _content_type(path),
                }
            )
        )
    return items


def _availability_snapshot(*, max_age_seconds: int) -> tuple[dict[str, Any], list[dict[str, Any]], list[dict[str, Any]]]:
    warnings: list[dict[str, Any]] = []
    try:
        age_seconds = sqlite.availability_cache_age_seconds()
        rows = sqlite.list_availability_rows(limit=AVAILABILITY_ROW_LIMIT)
    except Exception as exc:
        return (
            {
                "cacheState": "no-data",
                "source": "mvp_availability_cache",
                "liveAvailabilityChecked": False,
                "warning": f"availability cache read failed: {type(exc).__name__}",
            },
            [],
            [
                {
                    "code": "availability_cache_read_failed",
                    "message": f"Availability cache could not be read: {type(exc).__name__}.",
                }
            ],
        )

    latest_refreshed_at = _latest_refreshed_at(rows)
    if not rows or age_seconds is None:
        cache_state = "no-data"
    elif age_seconds > max_age_seconds:
        cache_state = "stale"
    else:
        cache_state = "fresh"

    if cache_state in {"no-data", "stale"}:
        warnings.append(
            {
                "code": f"availability_{cache_state}",
                "message": "Availability is not fresh; do not present cached rows as confirmed availability.",
            }
        )

    return (
        _clean_none(
            {
                "cacheState": cache_state,
                "source": "mvp_availability_cache",
                "liveAvailabilityChecked": False,
                "maxAgeSeconds": max_age_seconds,
                "ageSeconds": int(age_seconds) if age_seconds is not None else None,
                "refreshedAt": latest_refreshed_at,
                "rowCount": len(rows),
                "dateRange": _date_range(rows),
            }
        ),
        rows,
        warnings,
    )


def _empty_availability_snapshot() -> tuple[dict[str, Any], list[dict[str, Any]], list[dict[str, Any]]]:
    return (
        {
            "cacheState": "no-data",
            "source": "mvp_availability_cache",
            "liveAvailabilityChecked": False,
            "rowCount": 0,
        },
        [],
        [
            {
                "code": "availability_no-data",
                "message": "Availability cache is not included in this catalog build.",
            }
        ],
    )


def _availability_for_refs(
    *,
    refs: dict[str, Any],
    rows: list[dict[str, Any]],
    global_state: dict[str, Any],
) -> dict[str, Any]:
    cache_state = str(global_state.get("cacheState") or "no-data")
    keys = {
        (str(staff_id), str(service_id))
        for staff_id in refs.get("yclients_staff_ids", [])
        for service_id in refs.get("yclients_service_ids", [])
        if staff_id and service_id
    }
    if refs.get("yclients_staff_id") and refs.get("yclients_service_id"):
        keys.add((str(refs["yclients_staff_id"]), str(refs["yclients_service_id"])))

    if cache_state == "no-data" or not keys:
        return {
            "cacheState": "no-data",
            "source": "mvp_availability_cache",
            "liveAvailabilityChecked": False,
            "dates": [],
        }

    matched = [
        row
        for row in rows
        if (str(row.get("staff_id") or ""), str(row.get("service_id") or "")) in keys
    ]
    if not matched:
        return {
            "cacheState": "no-data" if cache_state == "fresh" else cache_state,
            "source": "mvp_availability_cache",
            "liveAvailabilityChecked": False,
            "dates": [],
        }

    by_date: dict[str, list[dict[str, Any]]] = {}
    for row in matched:
        date = str(row.get("date") or "")
        if date:
            by_date.setdefault(date, []).append(row)

    dates: list[dict[str, Any]] = []
    cached_available_count = 0
    for date, date_rows in sorted(by_date.items()):
        times = sorted(
            {
                str(row.get("time"))
                for row in date_rows
                if row.get("status") == "free" and row.get("time")
            }
        )
        cached_state = "available" if times else "unavailable"
        if cached_state == "available":
            cached_available_count += 1
        if cache_state == "fresh":
            state = cached_state
            cached_state_field = None
        else:
            state = "stale"
            cached_state_field = cached_state
        dates.append(
            _clean_none(
                {
                    "date": date,
                    "state": state,
                    "cachedState": cached_state_field,
                    "times": times,
                }
            )
        )

    return _clean_none(
        {
            "cacheState": cache_state,
            "source": "mvp_availability_cache",
            "liveAvailabilityChecked": False,
            "summary": {
                "knownDateCount": len(dates),
                "availableDateCount": cached_available_count if cache_state == "fresh" else 0,
                "cachedAvailableDateCount": cached_available_count,
            },
            "dates": dates,
        }
    )


def _service_variants_are_facilities(service: dict[str, Any]) -> bool:
    return bool(service.get("requires_variant") and service.get("variants"))


def _service_facility_title(service_type: str, service: dict[str, Any]) -> str:
    if service_type in {"house", "warm_gazebo"} and service.get("public_title"):
        return _capitalize_first(str(service.get("public_title") or ""))
    return str(service.get("title") or service.get("public_title") or service_type)


def _facility_public_title(title: str, service: dict[str, Any], source: dict[str, Any]) -> str:
    if source is service:
        return _capitalize_first(str(source.get("public_title") or source.get("title") or title))
    return str(source.get("public_title") or source.get("title") or title)


def _facility_aliases(service: dict[str, Any], source: dict[str, Any]) -> list[str]:
    aliases = [source.get("title"), source.get("public_title")]
    aliases.extend(source.get("aliases") or [])
    if source is service:
        aliases.extend(service.get("aliases") or [])
    return list(dict.fromkeys(str(item) for item in aliases if item))


def _capacity_contract(service: dict[str, Any], source: dict[str, Any]) -> dict[str, Any]:
    capacity = source.get("capacity_max") if source.get("capacity_max") is not None else service.get("capacity_max")
    sleep_capacity = (
        source.get("sleep_capacity_max")
        if source.get("sleep_capacity_max") is not None
        else service.get("sleep_capacity_max")
    )
    return _clean_none(
        {
            "max": _int_or_none(capacity),
            "sleepMax": _int_or_none(sleep_capacity),
        }
    )


def _service_rules(service: dict[str, Any]) -> dict[str, Any]:
    return _clean_none(
        {
            "requiresVariant": _bool_or_none(service.get("requires_variant")),
            "requiresDuration": _bool_or_none(service.get("requires_duration")),
            "requiresGuestsCount": _bool_or_none(service.get("requires_guests_count")),
            "collectDurationBeforeTime": _bool_or_none(_first_present(
                service.get("collect_duration_before_time"),
                service.get("require_duration_before_availability"),
            )),
            "defaultDurationMinutes": _int_or_none(service.get("default_duration_minutes")),
            "blockFullDayOnAnyBooking": _bool_or_none(service.get("block_full_day_on_any_booking")),
            "businessDayCutoff": service.get("business_day_cutoff"),
            "priceRules": service.get("price_rules") or None,
            "knowledgeAnchor": service.get("knowledge_anchor"),
        }
    )


def _facility_rules(service: dict[str, Any], source: dict[str, Any]) -> dict[str, Any]:
    rules = _service_rules(service)
    if source is not service:
        rules.update(_variant_rules(source))
    return _clean_none(rules)


def _variant_rules(variant: dict[str, Any]) -> dict[str, Any]:
    return _clean_none(
        {
            "durationMinutes": _int_or_none(variant.get("duration_minutes")),
            "weekdays": _string_or_int_list(variant.get("weekdays")),
        }
    )


def _combined_integration_refs(
    primary: dict[str, Any],
    sources: list[dict[str, Any]],
) -> dict[str, Any]:
    service_ids: list[str] = []
    staff_ids: list[str] = []
    for source in [primary, *sources]:
        service_id = str(source.get("yclients_service_id") or "")
        staff_id = str(source.get("yclients_staff_id") or "")
        if service_id:
            service_ids.append(service_id)
        if staff_id:
            staff_ids.append(staff_id)

    primary_service_id = str(primary.get("yclients_service_id") or "")
    primary_staff_id = str(primary.get("yclients_staff_id") or "")
    service_ids = sorted(set(service_ids), key=service_ids.index)
    staff_ids = sorted(set(staff_ids), key=staff_ids.index)

    return _clean_none(
        {
            "yclients_service_id": primary_service_id or (service_ids[0] if service_ids else None),
            "yclients_staff_id": primary_staff_id or (staff_ids[0] if staff_ids else None),
            "yclients_service_ids": service_ids,
            "yclients_staff_ids": staff_ids,
        }
    )


def _entity_warnings(
    *,
    entity_type: str,
    entity_id: str,
    refs: dict[str, Any],
    media_refs: list[str],
    has_tariffs: bool,
) -> list[dict[str, Any]]:
    warnings: list[dict[str, Any]] = []
    if not media_refs:
        warnings.append(
            {
                "entityType": entity_type,
                "entityId": entity_id,
                "code": "missing_media",
                "message": "No media reference is configured for this item.",
            }
        )
    if not has_tariffs:
        warnings.append(
            {
                "entityType": entity_type,
                "entityId": entity_id,
                "code": "missing_tariff",
                "message": "No public price/tariff is configured for this item.",
            }
        )
    return warnings


def _media_refs_for_key(key: str, media_by_key: dict[str, dict[str, Any]]) -> list[str]:
    return [key] if key and key in media_by_key else []


def public_media_url(media_key: str) -> str:
    return f"{PUBLIC_MEDIA_PREFIX}/{quote(str(media_key), safe='')}"


def _effective_media_catalog(overrides: dict[str, Any] | None = None) -> dict[str, dict[str, Any]]:
    result = json.loads(json.dumps(media_catalog(), ensure_ascii=False))
    media_overrides = (overrides or {}).get("media")
    if not isinstance(media_overrides, dict):
        return result
    for key, override in media_overrides.items():
        if not isinstance(override, dict):
            continue
        item = result.setdefault(str(key), {})
        for field in ("title", "aliases", "path", "storage", "contentType", "visible", "sort"):
            if field in override:
                item[field] = override[field]
    return result


def _path_for_media_item(item: dict[str, Any] | None) -> Path | None:
    if not isinstance(item, dict) or not item.get("path"):
        return None
    return _resolve_media_path(str(item["path"]))


def _public_media_path_for_key(media_key: str, *, overrides: dict[str, Any] | None = None) -> Path | None:
    item = _effective_media_catalog(overrides).get(str(media_key))
    return _public_media_path_from_path(_path_for_media_item(item))


def _public_media_path_from_path(path: Path | None) -> Path | None:
    if path is None:
        return None
    try:
        resolved = path.resolve()
        resolved.relative_to(PUBLIC_MEDIA_ROOT.resolve())
    except (OSError, ValueError):
        return None
    if not resolved.is_file():
        return None
    return resolved


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


def _variant_id(service_type: str, index: int, variant: dict[str, Any]) -> str:
    if variant.get("facility_id"):
        return str(variant["facility_id"])
    return f"{service_type}-option-{index + 1}"


def _tariff_id(
    *,
    service_type: str,
    facility_id: str,
    title: str,
    duration_minutes: int | None,
    weekdays: Any,
    index: int,
) -> str:
    duration = str(duration_minutes or "default")
    weekday_part = "-".join(str(item) for item in _string_or_int_list(weekdays)) or "all"
    return f"tariff:{facility_id}:{duration}:{weekday_part}:{index + 1}"


def _latest_refreshed_at(rows: list[dict[str, Any]]) -> str | None:
    values = [str(row.get("refreshed_at") or "") for row in rows if row.get("refreshed_at")]
    return max(values) if values else None


def _date_range(rows: list[dict[str, Any]]) -> dict[str, str] | None:
    dates = sorted({str(row.get("date") or "") for row in rows if row.get("date")})
    if not dates:
        return None
    return {"from": dates[0], "to": dates[-1]}


def _relative_path(path: Path | None) -> str | None:
    if path is None:
        return None
    try:
        return path.resolve().relative_to(PROJECT_ROOT.resolve()).as_posix()
    except ValueError:
        return str(path)


def _content_type(path: Path | None) -> str | None:
    if path is None:
        return None
    suffix = path.suffix.lower()
    if suffix in {".jpg", ".jpeg"}:
        return "image/jpeg"
    if suffix == ".png":
        return "image/png"
    if suffix == ".webp":
        return "image/webp"
    return None


def _duration_hours(duration_minutes: int | None) -> float | None:
    if duration_minutes is None:
        return None
    value = duration_minutes / 60
    return int(value) if value.is_integer() else value


def _int_or_none(value: Any) -> int | None:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None


def _bool_or_none(value: Any) -> bool | None:
    if value is None:
        return None
    return bool(value)


def _first_present(*values: Any) -> Any:
    for value in values:
        if value is not None:
            return value
    return None


def _string_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item) for item in value if item not in (None, "")]


def _string_or_int_list(value: Any) -> list[Any]:
    if not isinstance(value, list):
        return []
    result: list[Any] = []
    for item in value:
        if isinstance(item, int):
            result.append(item)
        elif item not in (None, ""):
            result.append(str(item))
    return result


def _capitalize_first(value: str) -> str:
    return value[:1].upper() + value[1:] if value else value


def _utc_now_iso() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _clean_none(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: _clean_none(item) for key, item in value.items() if item is not None}
    if isinstance(value, list):
        return [_clean_none(item) for item in value]
    return value


def _dedupe_warnings(warnings: list[dict[str, Any]]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    seen: set[tuple[tuple[str, str], ...]] = set()
    for warning in warnings:
        key = tuple(sorted((str(k), str(v)) for k, v in warning.items()))
        if key in seen:
            continue
        seen.add(key)
        result.append(warning)
    return result
