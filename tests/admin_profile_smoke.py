from __future__ import annotations

import os
import re
import sys
from decimal import Decimal
from pathlib import Path

import yaml


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from app.ai.parser import render_admin_prompt, render_router_prompt  # noqa: E402
from app.bot.media import paths_for_requested_media  # noqa: E402
from app.data.admin_profile import (  # noqa: E402
    addon_offer_titles,
    booking_max_upsell_offers,
    booking_start_reply,
    booking_upsell_enabled,
    build_profile_knowledge,
    compact_media_catalog,
    load_admin_profile,
    load_profile_services,
    media_catalog,
    media_path_for_key,
    payment_prepayment_percent,
    profile_path,
    resolve_media_key,
    service_availability_duration_minutes,
)
from app.data.services import load_services, normalize_service_type  # noqa: E402
from app.dialog.availability import suitable_variants  # noqa: E402
from app.dialog.payment import payment_amounts  # noqa: E402
from app.dialog.post_payment_message import build_post_payment_messages  # noqa: E402
from app.dialog.pricing import calculate_booking_price, extra_hour_price_breakdown  # noqa: E402
from app.dialog.state import BookingDraft  # noqa: E402
import app.dialog.state as state_module  # noqa: E402


def _assert_no_template_vars(text: str, label: str) -> None:
    leftover = re.findall(r"\{\{[^}]+\}\}", text)
    assert not leftover, f"{label} has unrendered variables: {leftover[:5]}"


def _assert_services_preserve_legacy_values() -> None:
    legacy = yaml.safe_load((PROJECT_ROOT / "app" / "data" / "services.yaml").read_text(encoding="utf-8")) or {}
    services = load_profile_services()
    for key, service in legacy.items():
        profile_service = services.get(key)
        assert profile_service is not None, key
        for field in (
            "title",
            "yclients_service_id",
            "yclients_staff_id",
            "default_duration_minutes",
            "price",
            "capacity_max",
            "sleep_capacity_max",
            "block_full_day_on_any_booking",
            "business_day_cutoff",
            "knowledge_anchor",
        ):
            if key == "bathhouse" and field == "capacity_max":
                # The old running bot enforced 15 people in app/dialog/engine.py,
                # even though services.yaml carried 20. Preserve runtime behavior.
                assert profile_service.get(field) == 15, f"{key}.{field}"
                continue
            assert service.get(field) == profile_service.get(field), f"{key}.{field}"
        variants = service.get("variants") or []
        profile_variants = profile_service.get("variants") or []
        assert len(variants) == len(profile_variants), key
        for index, (variant, profile_variant) in enumerate(zip(variants, profile_variants), 1):
            for field in ("title", "yclients_service_id", "yclients_staff_id", "duration_minutes", "weekdays", "price", "capacity_max"):
                assert variant.get(field) == profile_variant.get(field), f"{key}.variants[{index}].{field}"


def main() -> None:
    profile = load_admin_profile()
    assert profile_path().exists(), profile_path()
    assert isinstance(profile["business"], dict)
    assert isinstance(profile["assistant"], dict)
    assert isinstance(profile["booking"], dict)
    assert isinstance(profile["services"], dict)

    services = load_profile_services()
    expected_keys = {"gazebo", "bathhouse", "warm_gazebo", "summer_gazebo", "house", "gazebo_bathhouse"}
    assert expected_keys.issubset(set(services)), set(services)
    assert services["bathhouse"]["capacity_max"] == 15
    assert load_services() == services
    _assert_services_preserve_legacy_values()

    assert normalize_service_type("баня с бассейном") == "bathhouse"
    assert normalize_service_type("гостевой дом") == "house"
    assert normalize_service_type("беседка номер 1") == "gazebo"

    admin_prompt = render_admin_prompt()
    router_prompt = render_router_prompt()
    _assert_no_template_vars(admin_prompt, "admin_prompt")
    _assert_no_template_vars(router_prompt, "router_prompt")
    assert "gazebo" in router_prompt and "bathhouse" in router_prompt
    knowledge = build_profile_knowledge()
    assert "Причал" in knowledge
    assert "Беседка №1" in knowledge
    assert "Баня с бассейном — до 15 человек" in knowledge
    assert "Баня с бассейном — до 20 человек" not in knowledge
    assert booking_start_reply()

    media = media_catalog()
    assert compact_media_catalog().keys() == media.keys()
    for key in media:
        path = media_path_for_key(key)
        assert path is not None, key
        assert path.exists(), str(path)

    assert resolve_media_key("Беседка №1") == "Беседка №1"
    requested_paths = paths_for_requested_media(["Беседка №1", "bathhouse", "Гостевой дом"])
    assert len(requested_paths) == 3

    gazebo_draft = BookingDraft(service_type="gazebo", date="2026-08-01", guests_count=10)
    assert gazebo_draft.next_step() == "service_variant"

    bathhouse_draft = BookingDraft(service_type="bathhouse", date="2026-08-03")
    assert bathhouse_draft.next_step() == "duration"
    assert service_availability_duration_minutes("bathhouse", 8 * 60) == 7 * 60
    assert {int(item["duration_minutes"]) for item in suitable_variants(BookingDraft(service_type="bathhouse", date="2026-08-03", duration=8))} == {420}
    assert booking_upsell_enabled()
    assert booking_max_upsell_offers() == 2
    assert addon_offer_titles()

    original_upsell_enabled = state_module.booking_upsell_enabled
    try:
        state_module.booking_upsell_enabled = lambda: False
        no_upsell_draft = BookingDraft(
            service_type="bathhouse",
            date="2026-08-03",
            time="12:00",
            duration=3,
            guests_count=4,
        )
        assert no_upsell_draft.next_step() == "client_name"
    finally:
        state_module.booking_upsell_enabled = original_upsell_enabled

    priced_draft = BookingDraft(service_type="bathhouse", date="2026-08-03", time="12:00", duration=8, guests_count=6)
    assert calculate_booking_price(priced_draft) == 16200
    breakdown = extra_hour_price_breakdown(priced_draft)
    assert breakdown is not None
    assert breakdown["base_duration_hours"] == 7
    assert breakdown["extra_hour_price"] == 1500
    bathhouse_rules = services["bathhouse"]["price_rules"]
    original_extra_hour_price = bathhouse_rules["extra_hour_price"]
    try:
        bathhouse_rules["extra_hour_price"] = 2000
        assert calculate_booking_price(priced_draft) == 16700
    finally:
        bathhouse_rules["extra_hour_price"] = original_extra_hour_price
    assert payment_prepayment_percent() == 50
    assert payment_amounts(priced_draft)["prepayment"] == Decimal("8100.00")

    post_payment = build_post_payment_messages(priced_draft)
    assert any("vc7ki6Pm3LPYQA" in message for message in post_payment)

    print("OK admin profile smoke")


if __name__ == "__main__":
    main()
