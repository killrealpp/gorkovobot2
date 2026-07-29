from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))
FIXTURE_PATH = PROJECT_ROOT / "tests" / "fixtures" / "public_catalog.v1.json"

EXPECTED_FACILITY_IDS = {
    "gazebo-1",
    "gazebo-2",
    "gazebo-3",
    "gazebo-4",
    "gazebo-5",
    "gazebo-6",
    "gazebo-8",
    "gazebo-covered",
    "bathhouse",
    "warm-gazebo",
    "house",
}
EXPECTED_PUBLIC_TITLES = {
    "Беседка №1",
    "Беседка №2",
    "Беседка №3",
    "Беседка №4",
    "Беседка №5",
    "Беседка №6",
    "Беседка №8",
    "Крытая беседка",
    "Баня с бассейном",
    "Тёплая беседка",
    "Гостевой дом",
}
GAZEBO_1_TARIFF_ID = "tariff:gazebo-1:1440:all:1"


def _configure_temp_env(temp_root: Path) -> Path:
    env_file = temp_root / ".env"
    db_path = temp_root / "bot.sqlite3"
    overrides_path = temp_root / "catalog_overrides.local.json"
    env_file.write_text(
        "\n".join(
            [
                f"SQLITE_PATH={db_path}",
                "DB_HOST=",
                "DB_NAME=",
                "DB_USER=",
                "DB_PASSWORD=",
                "CLIENT_CHANNELS=max",
                "MAX_MODE=polling",
                "MAX_WEBHOOK_ENABLED=false",
                "PUBLIC_API_HOST=127.0.0.1",
                "PUBLIC_API_PORT=8090",
                "PUBLIC_API_CORS_ORIGINS=http://127.0.0.1:5173,http://localhost:5173",
                "CATALOG_ADMIN_LOCAL_ENABLED=false",
                f"CATALOG_OVERRIDES_PATH={overrides_path}",
                "PAYMENT_STATUS_LOOP_ENABLED=false",
                "YCLIENTS_SYNC_ENABLED=false",
                "STARTUP_AVAILABILITY_REFRESH_ENABLED=false",
                "WATCHLIST_LOOP_ENABLED=false",
            ]
        ),
        encoding="utf-8",
    )
    os.environ["APP_ENV_FILE"] = str(env_file)
    os.environ.pop("CATALOG_ADMIN_LOCAL_ENABLED", None)
    return overrides_path


def _assert_no_secret_keys(value: Any) -> None:
    forbidden = (
        "payment",
        "post_payment",
        "token",
        "secret",
        "supabase",
        "api_key",
        "password",
        "payment_secret_key",
        "yclients",
        "yclients_partner_token",
        "yclients_user_token",
        "yookassa",
        "openrouter_api_key",
        "max_bot_token",
        "db_password",
    )

    def visit(item: Any, path: str = "") -> None:
        if isinstance(item, dict):
            for key, nested in item.items():
                lowered = str(key).lower()
                assert not any(part in lowered for part in forbidden), f"secret-like key exposed at {path}.{key}"
                visit(nested, f"{path}.{key}")
        elif isinstance(item, list):
            for index, nested in enumerate(item):
                visit(nested, f"{path}[{index}]")

    visit(value)
    serialized = json.dumps(value, ensure_ascii=False).lower()
    for marker in forbidden:
        assert marker not in serialized, marker


def _assert_no_public_payment_copy(payload: dict[str, Any]) -> None:
    assert "payment" not in payload
    assert "post_payment" not in payload
    if "publicBookingPolicy" in payload:
        assert payload["publicBookingPolicy"]["mode"] == "catalog-only"
    serialized = json.dumps(payload, ensure_ascii=False)
    for marker in ("Бронь подтверждена", "Оплатить", "ЮKassa", "YooKassa"):
        assert marker not in serialized, marker


def _assert_no_public_internal_refs(payload: dict[str, Any]) -> None:
    forbidden_keys = {
        "integration_refs",
        "yclients_service_id",
        "yclients_staff_id",
        "yclients_service_ids",
        "yclients_staff_ids",
        "post_payment_instruction",
        "variant_id",
    }

    def visit(item: Any, path: str = "$") -> None:
        if isinstance(item, dict):
            for key, nested in item.items():
                assert str(key) not in forbidden_keys, f"internal key exposed at {path}.{key}"
                visit(nested, f"{path}.{key}")
        elif isinstance(item, list):
            for index, nested in enumerate(item):
                visit(nested, f"{path}[{index}]")

    visit(payload)

    from app.data.services import load_services

    internal_ids: set[str] = set()
    for service in load_services().values():
        _collect_yclients_ids(service, internal_ids)
    serialized = json.dumps(payload, ensure_ascii=False)
    assert "yclients" not in serialized.lower(), "yclients marker exposed"
    for internal_id in sorted(internal_ids):
        assert internal_id not in serialized, f"YCLIENTS id exposed: {internal_id}"


def _collect_yclients_ids(value: Any, result: set[str]) -> None:
    if isinstance(value, dict):
        for key, nested in value.items():
            if str(key) in {"yclients_service_id", "yclients_staff_id"} and nested:
                result.add(str(nested))
            _collect_yclients_ids(nested, result)
    elif isinstance(value, list):
        for nested in value:
            _collect_yclients_ids(nested, result)


def _assert_no_runtime_imports() -> None:
    api_files = [
        PROJECT_ROOT / "app" / "api" / "public_catalog.py",
        PROJECT_ROOT / "app" / "api" / "server.py",
        PROJECT_ROOT / "app" / "api" / "catalog_admin.py",
        PROJECT_ROOT / "app" / "catalog" / "reader.py",
    ]
    forbidden = (
        "from app.integrations",
        "import app.integrations",
        "from app.dialog.engine",
        "import app.dialog.engine",
        "from app.dialog.payment",
        "import app.dialog.payment",
        "from app.storage.supabase",
        "import app.storage.supabase",
    )
    for path in api_files:
        text = path.read_text(encoding="utf-8")
        for marker in forbidden:
            assert marker not in text, f"{path.name} imports runtime path: {marker}"


def _assert_no_write_module_diffs() -> None:
    guarded_paths = [
        "app/dialog/engine.py",
        "app/dialog/payment.py",
        "app/dialog/payment_status.py",
        "app/dialog/post_payment_message.py",
        "app/dialog/pricing.py",
        "app/dialog/availability.py",
        "app/integrations/yclients.py",
        "app/integrations/yclients_sync_service.py",
        "app/integrations/yookassa.py",
        "app/storage/sqlite.py",
        "app/storage/yclients_records_repo.py",
    ]
    for args in (
        ["git", "diff", "--name-only", "--", *guarded_paths],
        ["git", "diff", "--cached", "--name-only", "--", *guarded_paths],
    ):
        result = subprocess.run(args, cwd=PROJECT_ROOT, text=True, capture_output=True, check=True)
        assert not result.stdout.strip(), result.stdout


def _facilities_by_id(payload: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {str(item["id"]): item for item in payload["facilities"]}


def _normalize_ru(value: Any) -> str:
    return str(value or "").strip().lower().replace("ё", "е")


def _assert_secret_like_draft_is_rejected(client: Any) -> None:
    response = client.post(
        "/api/admin/catalog/validate",
        json={
            "supabase_url": "https://example.supabase.co",
            "facilities": {
                "gazebo-1": {
                    "yclients_service_id": "18201055",
                    "post_payment_instruction_key": "house",
                }
            },
            "payment": {"provider": "yookassa"},
        },
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["ok"] is False
    errors = "\n".join(body["validation"]["errors"])
    assert "supabase_url" in errors
    assert "yclients_service_id" in errors
    assert "post_payment_instruction_key" in errors
    assert "payment" in errors


def _assert_admin_house_preview_has_no_fake_capacity(client: Any) -> None:
    response = client.get("/api/admin/catalog/bot-preview?facility_id=house")
    assert response.status_code == 200, response.text
    preview = response.json()
    capacity = preview["facility"].get("capacity") or {}
    assert "max" not in capacity, preview
    assert capacity.get("max") is None, preview
    assert capacity != {"max": 0}, preview


def _assert_bot_catalog_house_has_no_fake_capacity() -> None:
    from app.catalog.reader import bot_services_catalog

    bot_catalog = bot_services_catalog()
    house_variants = bot_catalog["house"]["variants"]
    assert house_variants, bot_catalog["house"]
    for variant in house_variants:
        assert variant["capacity_max"] is None, variant


def _assert_catalog_shape(payload: dict[str, Any], client: Any) -> None:
    assert payload["business"]["name"] == "Причал"
    assert payload["availability_state"]["cacheState"] == "no-data"
    assert payload["source"]["liveAvailabilityChecked"] is False
    assert payload["source"]["overridesApplied"] is False
    _assert_no_public_payment_copy(payload)
    _assert_no_public_internal_refs(payload)

    assert len(payload["facilities"]) == 11
    assert len(payload["tariffs"]) == 29
    assert len(payload["media"]) == 11

    facilities = _facilities_by_id(payload)
    assert set(facilities) == EXPECTED_FACILITY_IDS
    assert {str(item.get("publicTitle")) for item in payload["facilities"]} == EXPECTED_PUBLIC_TITLES

    gazebo_1 = facilities["gazebo-1"]
    assert "Беседка №1" in gazebo_1["title"]
    assert "Беседка №1" in gazebo_1["publicTitle"]

    for facility_id, facility in facilities.items():
        if facility_id.startswith("gazebo"):
            assert _normalize_ru(facility.get("publicTitle")) != "беседка", facility

    house = facilities["house"]
    assert "max" not in (house.get("capacity") or {}), house
    assert (house.get("capacity") or {}).get("max") is None, house
    assert (house.get("capacity") or {}) != {"max": 0}, house

    media_by_key = {item["key"]: item for item in payload["media"]}
    media_refs_in_use: set[str] = set()
    for facility in payload["facilities"]:
        media_refs = facility.get("mediaRefs") or []
        assert media_refs, facility["id"]
        for media_key in media_refs:
            assert media_key in media_by_key, f"{facility['id']} references missing media {media_key}"
            media_refs_in_use.add(media_key)
    assert media_refs_in_use.issubset(set(media_by_key))

    for media in payload["media"]:
        assert media["exists"] is True, media
        assert media["publicUrl"].startswith("/api/public/media/"), media
        assert media["path"].startswith("app/images/"), media
        media_response = client.get(media["publicUrl"])
        assert media_response.status_code == 200, media
        assert media_response.headers["content-type"].startswith("image/"), media
        assert media_response.content, media

    _assert_no_secret_keys(payload)


def main() -> None:
    with tempfile.TemporaryDirectory(prefix="maxbot4-public-catalog-") as raw_temp:
        temp_root = Path(raw_temp)
        overrides_path = _configure_temp_env(temp_root)

        from fastapi.testclient import TestClient

        from app.catalog.reader import bot_services_catalog, media_path_for_key
        from app.api.public_catalog import build_public_catalog
        from app.api.server import create_public_api_app
        from app.core.config import get_settings
        from app.storage.sqlite import init_db, replace_availability_cache

        init_db()
        app = create_public_api_app()
        client = TestClient(app)
        settings = get_settings()
        assert settings.public_api_host == "127.0.0.1"
        assert settings.public_api_port == 8090

        health = client.get("/health")
        assert health.status_code == 200, health.text
        assert health.json()["ok"] is True

        fixture = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))
        expected_fixture = build_public_catalog(
            generated_at="fixture",
            read_availability_cache=False,
            include_overrides=False,
        )
        assert fixture == expected_fixture, "run scripts\\generate_public_catalog_fixture.py"
        _assert_no_secret_keys(fixture)
        _assert_no_public_payment_copy(fixture)
        _assert_no_public_internal_refs(fixture)

        preflight = client.options(
            "/api/public/catalog",
            headers={
                "Origin": "http://localhost:5173",
                "Access-Control-Request-Method": "GET",
            },
        )
        assert preflight.status_code == 200, preflight.text
        assert preflight.headers.get("access-control-allow-origin") == "http://localhost:5173"

        assert client.get("/api/admin/catalog/draft").status_code == 404
        assert client.post("/api/admin/catalog/validate", json={}).status_code == 404
        assert client.patch("/api/admin/catalog/facilities/gazebo-1", json={"publicTitle": "blocked"}).status_code == 404
        assert client.get("/api/admin/catalog/bot-preview?facility_id=gazebo-1").status_code == 404

        response = client.get(
            "/api/public/catalog",
            headers={"Origin": "http://127.0.0.1:5173"},
        )
        assert response.status_code == 200, response.text
        assert response.headers.get("access-control-allow-origin") == "http://127.0.0.1:5173"
        payload = response.json()
        _assert_catalog_shape(payload, client)

        os.environ["CATALOG_ADMIN_LOCAL_ENABLED"] = "true"
        get_settings.cache_clear()
        admin_client = TestClient(create_public_api_app())
        admin_preflight = admin_client.options(
            "/api/admin/catalog/facilities/gazebo-1",
            headers={
                "Origin": "http://localhost:5173",
                "Access-Control-Request-Method": "PATCH",
            },
        )
        assert admin_preflight.status_code == 200, admin_preflight.text
        assert admin_preflight.headers.get("access-control-allow-origin") == "http://localhost:5173"

        draft_response = admin_client.get("/api/admin/catalog/draft")
        assert draft_response.status_code == 200, draft_response.text
        assert draft_response.json()["draft"] == {}

        draft = {
            "facilities": {
                "gazebo-1": {
                    "publicTitle": "Беседка №1 тестовый черновик",
                    "capacity": {"max": 44},
                    "warning": "local draft warning",
                }
            }
        }
        validate_response = admin_client.post("/api/admin/catalog/validate", json=draft)
        assert validate_response.status_code == 200, validate_response.text
        assert validate_response.json()["ok"] is True
        _assert_secret_like_draft_is_rejected(admin_client)

        put_response = admin_client.put("/api/admin/catalog/draft", json=draft)
        assert put_response.status_code == 200, put_response.text
        assert overrides_path.exists()

        patch = {
            "publicTitle": "Беседка №1 после PATCH",
            "description": "patch description",
            "capacity": {"max": 43},
            "mediaRefs": ["Беседка №1"],
            "tariffs": {
                GAZEBO_1_TARIFF_ID: {
                    "price": {"amount": 11111}
                }
            },
        }
        patch_response = admin_client.patch("/api/admin/catalog/facilities/gazebo-1", json=patch)
        assert patch_response.status_code == 200, patch_response.text
        patch_payload = patch_response.json()
        assert patch_payload["ok"] is True
        assert patch_payload["mode"] == "maxbot-local-admin"
        assert patch_payload["facility_id"] == "gazebo-1"
        assert patch_payload["validation"]["ok"] is True
        assert patch_payload["draft"]["facilities"]["gazebo-1"]["publicTitle"] == "Беседка №1 после PATCH"
        assert patch_payload["draft"]["tariffs"][GAZEBO_1_TARIFF_ID]["price"]["amount"] == 11111

        draft_catalog = admin_client.get("/api/public/catalog").json()
        draft_gazebo_1 = _facilities_by_id(draft_catalog)["gazebo-1"]
        assert draft_catalog["source"]["overridesApplied"] is True
        assert draft_gazebo_1["publicTitle"] == "Беседка №1 после PATCH"
        assert draft_gazebo_1["description"] == "patch description"
        assert draft_gazebo_1["capacity"]["max"] == 43
        assert draft_gazebo_1["warning"] == "local draft warning"
        draft_tariff = next(item for item in draft_catalog["tariffs"] if item["id"] == GAZEBO_1_TARIFF_ID)
        assert draft_tariff["price"]["amount"] == 11111
        _assert_no_public_payment_copy(draft_catalog)
        _assert_no_public_internal_refs(draft_catalog)
        _assert_no_secret_keys(draft_catalog)

        preview_response = admin_client.get("/api/admin/catalog/bot-preview?facility_id=gazebo-1")
        assert preview_response.status_code == 200, preview_response.text
        preview = preview_response.json()
        assert preview["ok"] is True
        assert preview["mode"] == "maxbot-local-admin"
        assert preview["facility_id"] == "gazebo-1"
        assert preview["facility"]["publicTitle"] == "Беседка №1 после PATCH"
        assert preview["facility"]["description"] == "patch description"
        assert preview["facility"]["capacity"]["max"] == 43
        assert preview["tariffs"][0]["price"]["amount"] == 11111
        assert "Беседка №1 после PATCH" in preview["text"]
        assert "43" in preview["text"]
        assert "11 111 ₽" in preview["text"]
        assert preview["requested_media"] == ["Беседка №1"]
        _assert_no_public_payment_copy(preview)
        _assert_no_public_internal_refs(preview)
        _assert_no_secret_keys(preview)
        _assert_admin_house_preview_has_no_fake_capacity(admin_client)

        bot_catalog = bot_services_catalog()
        gazebo_variants = bot_catalog["gazebo"]["variants"]
        patched_variant = next(item for item in gazebo_variants if item["id"] == "gazebo-1")
        assert patched_variant["title"] == "Беседка №1 после PATCH"
        assert patched_variant["capacity_max"] == 43
        assert patched_variant["price"] == 11111
        _assert_bot_catalog_house_has_no_fake_capacity()
        assert media_path_for_key("Беседка №1").name == "besedka1.jpg"

        refreshed_at = (datetime.now(UTC).replace(tzinfo=None) - timedelta(hours=2)).isoformat()
        replace_availability_cache(
            [
                {
                    "service_type": "gazebo",
                    "title": "Беседка №1",
                    "date": "2026-08-01",
                    "time": "day",
                    "service_id": "18201055",
                    "staff_id": "3828146",
                    "status": "free",
                }
            ],
            refreshed_at=refreshed_at,
        )

        stale_payload = admin_client.get("/api/public/catalog").json()
        assert stale_payload["availability_state"]["cacheState"] == "stale"
        stale_gazebo_1 = _facilities_by_id(stale_payload)["gazebo-1"]
        assert stale_gazebo_1["availability_state"]["cacheState"] == "stale"
        dates = stale_gazebo_1["availability_state"]["dates"]
        assert dates and dates[0]["state"] == "stale", dates
        assert dates[0]["cachedState"] == "available", dates
        assert stale_payload["availability_state"]["cacheState"] != "fresh"
        assert all(item["state"] != "available" for item in dates), dates
        assert stale_payload["source"]["liveAvailabilityChecked"] is False
        _assert_no_public_payment_copy(stale_payload)
        _assert_no_public_internal_refs(stale_payload)
        _assert_no_secret_keys(stale_payload)

        _assert_no_runtime_imports()
        _assert_no_write_module_diffs()

    print("OK public catalog smoke")


if __name__ == "__main__":
    main()
