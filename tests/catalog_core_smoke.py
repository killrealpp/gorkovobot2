from __future__ import annotations

import os
import subprocess
import sys


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
    _assert_validation_result_shape()
    _assert_dto_shapes_importable()
    _assert_catalog_core_imports_do_not_pull_runtime_clients()
    print("OK catalog core smoke")


if __name__ == "__main__":
    main()
