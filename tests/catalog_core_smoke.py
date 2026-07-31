from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path


PROJECT_ROOT = os.path.dirname(os.path.dirname(__file__))
sys.path.insert(0, PROJECT_ROOT)

from app.catalog_core import (  # noqa: E402
    BotPreviewDTO,
    CatalogPrivateRefs,
    DraftOverridesDTO,
    PublicCatalogDTO,
    RawProfileDTO,
    ResolvedCatalogDTO,
    ValidationResultDTO,
    collect_forbidden_public_paths,
    public_payload_has_forbidden_keys,
    validate_draft_like,
)


def _assert_private_keys_rejected() -> None:
    draft = {
        "schema_version": "draft_overrides.v1",
        "facilities": {
            "house": {
                "capacity": {},
                "yclients_service_id": "18201039",
                "post_payment_instruction_key": "house",
            }
        },
        "payment": {"provider": "yookassa"},
        "supabase_url": "https://example.supabase.co",
    }
    result = validate_draft_like(draft)
    assert result.ok is False
    errors = "\n".join(result.errors)
    assert "yclients_service_id" in errors
    assert "post_payment_instruction_key" in errors
    assert "payment" in errors
    assert "supabase_url" in errors


def _assert_public_leakage_detector() -> None:
    payload = {
        "business": {"name": "Причал"},
        "payment": {"provider": "yookassa"},
        "post_payment": {"text": "hidden"},
        "services": [
            {"id": "house", "yclients_service_id": "18201039"},
            {"id": "gazebo", "supabase_url": "https://example.supabase.co"},
            {"id": "bathhouse", "apiToken": "hidden"},
            {"id": "warm_gazebo", "secret": "hidden"},
        ],
    }
    paths = collect_forbidden_public_paths(payload)
    joined = "\n".join(paths)
    assert public_payload_has_forbidden_keys(payload)
    for marker in ("payment", "post_payment", "yclients_service_id", "supabase_url", "apiToken", "secret"):
        assert marker in joined


def _assert_capacity_unknown_stays_unknown() -> None:
    draft = {"facilities": {"house": {"capacity": {}}}}
    result = validate_draft_like(draft)
    assert result.to_dict() == {"ok": True, "errors": [], "warnings": []}
    assert draft["facilities"]["house"]["capacity"] == {}
    assert draft["facilities"]["house"]["capacity"] != {"max": 0}


def _assert_explicit_capacity_zero_is_not_synthesized() -> None:
    draft = {"facilities": {"house": {"capacity": {"max": 0}}}}
    result = validate_draft_like(draft)
    assert result.ok is True, result.to_dict()
    assert draft["facilities"]["house"]["capacity"]["max"] == 0


def _assert_capacity_max_must_be_int() -> None:
    result = validate_draft_like({"facilities": {"house": {"capacity": {"max": "many"}}}})
    assert result.ok is False
    assert "facilities.house.capacity.max must be an integer" in "\n".join(result.errors)


def _assert_media_path_stays_under_public_images() -> None:
    valid = validate_draft_like({"media": {"house": {"path": "app/images/dom_gostevoy.jpg"}}})
    assert valid.ok is True, valid.to_dict()

    escaped = validate_draft_like({"media": {"house": {"path": "../secret.jpg"}}})
    assert escaped.ok is False
    assert "media.house.path must stay under app/images" in "\n".join(escaped.errors)


def _assert_runtime_validator_parity_on_dangerous_drafts() -> None:
    from app.api.public_catalog import validate_catalog_overrides

    outside_root = str(Path(PROJECT_ROOT).parent / "outside.jpg")
    cases = [
        ("non_object", []),
        (
            "secret_like_keys",
            {
                "facilities": {"house": {"yclients_service_id": "18201039"}},
                "payment": {"provider": "yookassa"},
                "supabase_url": "https://example.supabase.co",
            },
        ),
        ("section_not_object", {"facilities": []}),
        ("item_not_object", {"facilities": {"house": []}}),
        ("unknown_top_level", {"unknown": {}}),
        ("unknown_item_field", {"facilities": {"house": {"unknown": "ignored"}}}),
        ("bad_sort", {"facilities": {"house": {"sort": "last"}}}),
        ("bad_visible", {"facilities": {"house": {"visible": "yes"}}}),
        ("bad_capacity_object", {"facilities": {"house": {"capacity": "many"}}}),
        ("bad_capacity_max", {"facilities": {"house": {"capacity": {"max": "many"}}}}),
        ("bad_media_refs", {"facilities": {"house": {"mediaRefs": "house"}}}),
        ("bad_price_object", {"tariffs": {"tariff:house": {"price": 1000}}}),
        ("bad_price_amount", {"tariffs": {"tariff:house": {"price": {"amount": "many"}}}}),
        ("bad_duration", {"tariffs": {"tariff:house": {"duration_minutes": "long"}}}),
        ("bad_aliases", {"media": {"house": {"aliases": "house"}}}),
        ("bad_media_path_relative_escape", {"media": {"house": {"path": "../secret.jpg"}}}),
        ("bad_media_path_absolute_escape", {"media": {"house": {"path": outside_root}}}),
        ("valid_media_path", {"media": {"house": {"path": "app/images/dom_gostevoy.jpg"}}}),
    ]

    for name, draft in cases:
        runtime = validate_catalog_overrides(draft)
        core = validate_draft_like(draft).to_dict()
        assert _normalized_validation(runtime) == _normalized_validation(core), (name, runtime, core)


def _normalized_validation(value: dict[str, object]) -> dict[str, object]:
    return {
        "ok": bool(value.get("ok")),
        "errors": sorted(str(item) for item in value.get("errors", [])),
        "warnings": sorted(str(item) for item in value.get("warnings", [])),
    }


def _assert_validation_result_shape() -> None:
    result = ValidationResultDTO(ok=True)
    assert list(result.to_dict().keys()) == ["ok", "errors", "warnings"]
    assert result.to_dict() == {"ok": True, "errors": [], "warnings": []}

    warning_result = validate_draft_like({"unknown": {}})
    assert warning_result.ok is True
    assert "unknown top-level key ignored: unknown" in warning_result.warnings


def _assert_dto_shapes_importable() -> None:
    assert RawProfileDTO(payload={"business": {"name": "Причал"}}).to_dict()["schema_version"] == "raw_profile.v1"
    assert DraftOverridesDTO(facilities={"house": {"capacity": {}}}).to_dict()["facilities"]["house"] == {
        "capacity": {}
    }
    assert PublicCatalogDTO(publicBookingPolicy={"readOnly": True}).to_dict()["publicBookingPolicy"]["readOnly"] is True
    assert BotPreviewDTO(facility_id="house").to_dict()["facility_id"] == "house"

    private_refs = CatalogPrivateRefs(integration_refs={"yclients_service_id": "hidden"})
    resolved = ResolvedCatalogDTO(private_refs=private_refs)
    assert "private_refs" not in resolved.to_dict()
    assert resolved.to_dict(include_private=True)["private_refs"]["private"] is True


def _assert_catalog_core_imports_do_not_pull_runtime_clients() -> None:
    script = """
import sys
import app.catalog_core.dto
import app.catalog_core.privacy
import app.catalog_core.validation
for name in sorted(sys.modules):
    if name.startswith("app.dialog") or name.startswith("app.integrations") or name.startswith("app.storage"):
        print(name)
"""
    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=PROJECT_ROOT,
        text=True,
        capture_output=True,
        check=True,
    )
    assert result.stdout.strip() == "", result.stdout


def main() -> None:
    _assert_private_keys_rejected()
    _assert_public_leakage_detector()
    _assert_capacity_unknown_stays_unknown()
    _assert_explicit_capacity_zero_is_not_synthesized()
    _assert_capacity_max_must_be_int()
    _assert_media_path_stays_under_public_images()
    _assert_runtime_validator_parity_on_dangerous_drafts()
    _assert_validation_result_shape()
    _assert_dto_shapes_importable()
    _assert_catalog_core_imports_do_not_pull_runtime_clients()
    print("OK catalog core smoke")


if __name__ == "__main__":
    main()
