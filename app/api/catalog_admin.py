from __future__ import annotations

from typing import Any

from fastapi import HTTPException, Request

from app.api.public_catalog import (
    build_public_catalog,
    catalog_overrides_path,
    load_catalog_overrides,
    save_catalog_overrides,
    validate_catalog_overrides,
)
from app.catalog.reader import bot_facility_preview
from app.core.config import PROJECT_ROOT


ADMIN_DRAFT_PATH = "/api/admin/catalog/draft"
ADMIN_FACILITY_PATH = "/api/admin/catalog/facilities/{facility_id}"
ADMIN_BOT_PREVIEW_PATH = "/api/admin/catalog/bot-preview"
ADMIN_VALIDATE_PATH = "/api/admin/catalog/validate"
FACILITY_PATCH_FIELDS = {"title", "publicTitle", "description", "warning", "capacity", "mediaRefs", "visible", "sort"}
TARIFF_PATCH_FIELDS = {"title", "price", "duration_minutes", "visible", "sort"}
MEDIA_PATCH_FIELDS = {"title", "aliases", "path", "storage", "contentType", "visible", "sort"}


def register_catalog_admin_routes(app: Any) -> None:
    @app.get(ADMIN_DRAFT_PATH)
    async def get_catalog_draft() -> dict[str, Any]:
        draft = load_catalog_overrides()
        validation = validate_catalog_overrides(draft)
        return {
            "ok": validation["ok"],
            "path": _draft_path_for_response(),
            "draft": draft if validation["ok"] else {},
            "draftRedacted": not validation["ok"] and bool(draft),
            "validation": validation,
        }

    @app.put(ADMIN_DRAFT_PATH)
    async def put_catalog_draft(request: Request) -> dict[str, Any]:
        draft = await _json_body(request)
        validation = validate_catalog_overrides(draft)
        if not validation["ok"]:
            raise HTTPException(status_code=422, detail=validation)
        save_catalog_overrides(draft)
        return {
            "ok": True,
            "path": _draft_path_for_response(),
            "draft": draft,
            "validation": validation,
        }

    @app.patch(ADMIN_FACILITY_PATH)
    async def patch_catalog_facility(facility_id: str, request: Request) -> dict[str, Any]:
        patch = await _json_body(request)
        known_tariff_ids = _known_tariff_ids_for_facility(facility_id)
        if known_tariff_ids is None:
            raise HTTPException(status_code=404, detail=f"unknown facility: {facility_id}")

        draft = _merge_facility_patch(
            draft=load_catalog_overrides(),
            facility_id=facility_id,
            patch=patch,
            known_tariff_ids=known_tariff_ids,
        )
        validation = validate_catalog_overrides(draft)
        if not validation["ok"]:
            raise HTTPException(status_code=422, detail=validation)

        save_catalog_overrides(draft)
        return {
            "ok": True,
            "mode": "maxbot-local-admin",
            "facility_id": facility_id,
            "draft": draft,
            "validation": validation,
        }

    @app.post(ADMIN_VALIDATE_PATH)
    async def validate_catalog_draft(request: Request) -> dict[str, Any]:
        draft = await _optional_json_body(request)
        if draft is None:
            draft = load_catalog_overrides()
        validation = validate_catalog_overrides(draft)
        return {
            "ok": validation["ok"],
            "path": _draft_path_for_response(),
            "validation": validation,
        }

    @app.get(ADMIN_BOT_PREVIEW_PATH)
    async def get_bot_catalog_preview(facility_id: str) -> dict[str, Any]:
        preview = bot_facility_preview(facility_id)
        if preview is None:
            raise HTTPException(status_code=404, detail=f"unknown facility: {facility_id}")
        return {
            "ok": True,
            **preview,
        }


async def _json_body(request: Request) -> dict[str, Any]:
    try:
        value = await request.json()
    except Exception as exc:
        raise HTTPException(status_code=400, detail="request body must be valid JSON") from exc
    if not isinstance(value, dict):
        raise HTTPException(status_code=400, detail="request body must be a JSON object")
    return value


async def _optional_json_body(request: Request) -> Any:
    if not (request.headers.get("content-length") or request.headers.get("transfer-encoding")):
        return None
    try:
        return await request.json()
    except Exception as exc:
        raise HTTPException(status_code=400, detail="request body must be valid JSON") from exc


def _merge_facility_patch(
    *,
    draft: dict[str, Any],
    facility_id: str,
    patch: dict[str, Any],
    known_tariff_ids: set[str],
) -> dict[str, Any]:
    result = _json_copy(draft) if isinstance(draft, dict) else {}
    changed = False

    facility_patch = _pick_patch_fields(patch, FACILITY_PATCH_FIELDS)
    if facility_patch:
        facilities_draft = result.get("facilities")
        if not isinstance(facilities_draft, dict):
            facilities_draft = {}
            result["facilities"] = facilities_draft
        facility_draft = facilities_draft.setdefault(facility_id, {})
        if not isinstance(facility_draft, dict):
            facility_draft = {}
            facilities_draft[facility_id] = facility_draft
        _deep_merge_known(facility_draft, facility_patch, nested_object_fields={"capacity"})
        changed = True

    tariff_patches = _keyed_patch_items(patch.get("tariffs"), id_fields=("id", "tariff_id"))
    if tariff_patches:
        tariffs_draft = result.get("tariffs")
        if not isinstance(tariffs_draft, dict):
            tariffs_draft = {}
            result["tariffs"] = tariffs_draft
        for tariff_id, tariff_patch in tariff_patches.items():
            if tariff_id not in known_tariff_ids:
                raise HTTPException(status_code=400, detail=f"unknown tariff for {facility_id}: {tariff_id}")
            clean_patch = _pick_patch_fields(tariff_patch, TARIFF_PATCH_FIELDS)
            tariff_draft = tariffs_draft.setdefault(tariff_id, {})
            if not isinstance(tariff_draft, dict):
                tariff_draft = {}
                tariffs_draft[tariff_id] = tariff_draft
            _deep_merge_known(tariff_draft, clean_patch, nested_object_fields={"price"})
            changed = True

    media_patches = _keyed_patch_items(
        patch.get("media", patch.get("mediaItems")),
        id_fields=("key", "media_key"),
    )
    if media_patches:
        media_draft = result.get("media")
        if not isinstance(media_draft, dict):
            media_draft = {}
            result["media"] = media_draft
        for media_key, media_patch in media_patches.items():
            clean_patch = _pick_patch_fields(media_patch, MEDIA_PATCH_FIELDS)
            item_draft = media_draft.setdefault(media_key, {})
            if not isinstance(item_draft, dict):
                item_draft = {}
                media_draft[media_key] = item_draft
            _deep_merge_known(item_draft, clean_patch, nested_object_fields=set())
            changed = True

    if not changed:
        raise HTTPException(status_code=400, detail="patch does not contain supported catalog fields")
    return result


def _known_tariff_ids_for_facility(facility_id: str) -> set[str] | None:
    catalog = build_public_catalog(
        generated_at="admin-validation",
        read_availability_cache=False,
        include_overrides=False,
    )
    for facility in catalog.get("facilities", []):
        if isinstance(facility, dict) and str(facility.get("id")) == facility_id:
            return {str(item) for item in facility.get("tariff_ids", []) if item}
    return None


def _pick_patch_fields(patch: dict[str, Any], allowed: set[str]) -> dict[str, Any]:
    return {key: patch[key] for key in allowed if key in patch}


def _deep_merge_known(target: dict[str, Any], patch: dict[str, Any], *, nested_object_fields: set[str]) -> None:
    for key, value in patch.items():
        if key in nested_object_fields and isinstance(value, dict):
            nested = target.setdefault(key, {})
            if not isinstance(nested, dict):
                nested = {}
                target[key] = nested
            nested.update(value)
        else:
            target[key] = value


def _keyed_patch_items(raw: Any, *, id_fields: tuple[str, ...]) -> dict[str, dict[str, Any]]:
    if raw is None:
        return {}
    if isinstance(raw, dict):
        result: dict[str, dict[str, Any]] = {}
        for item_id, patch in raw.items():
            if not isinstance(patch, dict):
                raise HTTPException(status_code=400, detail=f"patch item {item_id} must be an object")
            result[str(item_id)] = patch
        return result
    if isinstance(raw, list):
        result = {}
        for patch in raw:
            if not isinstance(patch, dict):
                raise HTTPException(status_code=400, detail="patch list items must be objects")
            item_id = next((str(patch[field]) for field in id_fields if patch.get(field)), "")
            if not item_id:
                raise HTTPException(status_code=400, detail=f"patch list items must include one of {id_fields}")
            result[item_id] = patch
        return result
    raise HTTPException(status_code=400, detail="patch section must be an object or array")


def _json_copy(value: dict[str, Any]) -> dict[str, Any]:
    import json

    return json.loads(json.dumps(value, ensure_ascii=False))


def _draft_path_for_response() -> str:
    path = catalog_overrides_path()
    try:
        return path.resolve().relative_to(PROJECT_ROOT.resolve()).as_posix()
    except (OSError, ValueError):
        return str(path)
