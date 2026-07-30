from __future__ import annotations

import os
import sys
from datetime import timezone
from decimal import Decimal


sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from app.bot.media import extract_media_titles_from_reply, paths_for_requested_media  # noqa: E402
from app.data.admin_profile import payment_prepayment_percent  # noqa: E402
from app.data.services import normalize_service_type, service_title  # noqa: E402
import app.dialog.availability as availability_module  # noqa: E402
from app.dialog.availability import build_yclients_payload, suitable_variants  # noqa: E402
from app.dialog.payment import payment_amounts  # noqa: E402
from app.dialog.post_payment_message import build_post_payment_messages  # noqa: E402
from app.dialog.pricing import calculate_booking_price, extra_hour_price_breakdown  # noqa: E402
from app.dialog.state import BookingDraft  # noqa: E402


def _assert_next_step_contract() -> None:
    cases = [
        (BookingDraft(), "service_type"),
        (BookingDraft(service_type="gazebo"), "date"),
        (BookingDraft(service_type="gazebo", date="2026-08-01"), "service_variant"),
        (
            BookingDraft(
                service_type="gazebo",
                date="2026-08-01",
                service_variant="Беседка №1",
            ),
            "time",
        ),
        (
            BookingDraft(
                service_type="gazebo",
                date="2026-08-01",
                service_variant="Беседка №1",
                time="18:00",
            ),
            "duration",
        ),
        (
            BookingDraft(
                service_type="gazebo",
                date="2026-08-01",
                service_variant="Беседка №1",
                time="18:00",
                duration=6,
            ),
            "guests_count",
        ),
        (
            BookingDraft(
                service_type="gazebo",
                date="2026-08-01",
                service_variant="Беседка №1",
                time="18:00",
                duration=6,
                guests_count=12,
            ),
            "upsell_items",
        ),
        (
            BookingDraft(
                service_type="gazebo",
                date="2026-08-01",
                service_variant="Беседка №1",
                time="18:00",
                duration=6,
                guests_count=12,
                upsell_done=True,
            ),
            "client_name",
        ),
        (
            BookingDraft(
                service_type="gazebo",
                date="2026-08-01",
                service_variant="Беседка №1",
                time="18:00",
                duration=6,
                guests_count=12,
                upsell_done=True,
                client_name="Савелий",
            ),
            "phone",
        ),
        (
            BookingDraft(
                service_type="bathhouse",
                date="2026-08-03",
            ),
            "duration",
        ),
        (
            BookingDraft(
                service_type="bathhouse",
                date="2026-08-03",
                time="12:00",
            ),
            "duration",
        ),
        (
            BookingDraft(
                service_type="bathhouse",
                date="2026-08-03",
                duration=8,
            ),
            "time",
        ),
        (BookingDraft(service_type="house", date="2026-08-03"), "time"),
        (BookingDraft(service_type="house", date="2026-08-03", time="16:00"), "duration"),
        (BookingDraft(service_type="warm_gazebo", date="2026-08-03"), "time"),
    ]
    for draft, expected in cases:
        assert draft.next_step() == expected, (draft, draft.next_step(), expected)

    ready = BookingDraft(
        service_type="warm_gazebo",
        date="2026-08-08",
        time="14:00",
        duration=22,
        guests_count=10,
        event_format="отдых",
        upsell_done=True,
        client_name="Савелий",
        phone="+79022613470",
    )
    assert ready.next_step() == "confirmation"
    assert ready.ready_for_confirmation()

    missing_gazebo_variant = BookingDraft(
        service_type="gazebo",
        date="2026-08-01",
        time="18:00",
        duration=6,
        guests_count=12,
        upsell_done=True,
        client_name="Савелий",
        phone="+79022613470",
    )
    assert missing_gazebo_variant.next_step() == "service_variant"
    assert not missing_gazebo_variant.ready_for_confirmation()

    open_upsells = BookingDraft(
        service_type="warm_gazebo",
        date="2026-08-08",
        time="14:00",
        duration=22,
        guests_count=10,
        event_format="отдых",
        client_name="Савелий",
        phone="+79022613470",
    )
    assert open_upsells.next_step() == "upsell_items"
    assert not open_upsells.ready_for_confirmation()


def _assert_catalog_alias_contract() -> None:
    assert normalize_service_type("баня с бассейном") == "bathhouse"
    assert normalize_service_type("гостевой дом") == "house"
    assert normalize_service_type("тёплая беседка") == "warm_gazebo"
    assert normalize_service_type("беседка номер 1") == "gazebo"
    assert service_title(None) == "услуга"
    assert service_title("gazebo") == "Беседка"
    assert service_title("bathhouse") == "Баня"
    assert service_title("house") == "Дом"


def _assert_pricing_and_payment_contract() -> None:
    assert calculate_booking_price(BookingDraft(service_type="gazebo", service_variant="Беседка №1")) == 10500
    assert calculate_booking_price(BookingDraft(service_type="bathhouse", date="2026-08-03", duration=8)) == 16200
    assert calculate_booking_price(BookingDraft(service_type="bathhouse", date="2026-08-01", duration=8)) == 20050
    assert calculate_booking_price(BookingDraft(service_type="house", date="2026-08-03", duration=8)) == 10500
    assert calculate_booking_price(BookingDraft(service_type="house", date="2026-08-07", duration=24)) == 12600

    breakdown = extra_hour_price_breakdown(
        BookingDraft(service_type="bathhouse", date="2026-08-03", duration=8)
    )
    assert breakdown is not None
    assert breakdown["base_duration_hours"] == 7
    assert breakdown["extra_hours"] == 1
    assert breakdown["extra_hour_price"] == 1500
    assert breakdown["total_price"] == 16200

    priced = BookingDraft(
        service_type="bathhouse",
        date="2026-08-03",
        time="12:00",
        duration=8,
        guests_count=6,
    )
    assert payment_prepayment_percent() == 50
    amounts = payment_amounts(priced)
    assert amounts["total"] == Decimal("16200.00")
    assert amounts["prepayment"] == Decimal("8100.00")
    assert amounts["remaining"] == Decimal("8100.00")


def _assert_media_contract() -> None:
    titles = extract_media_titles_from_reply(
        "Вот фото вариантов: Баня с бассейном, Гостевой дом, Беседка 1"
    )
    assert titles == ["bathhouse", "house", "Беседка №1"]

    paths = paths_for_requested_media(["bathhouse", "house", "Тёплая беседка", "Беседка №1"])
    assert [path.name for path in paths] == [
        "banya.jpg",
        "dom_gostevoy.jpg",
        "besedka_teplaya.jpg",
        "besedka1.jpg",
    ]


def _assert_yclients_payload_contract() -> None:
    draft = BookingDraft(
        service_type="bathhouse",
        date="2026-08-03",
        time="12:00",
        duration=8,
        guests_count=6,
        event_format="отдых",
        upsell_items=["кальян"],
        upsell_done=True,
        client_name="Савелий",
        phone="+7 902 261-34-70",
        payment_id="pay-test",
    )

    variants = suitable_variants(draft)
    assert len(variants) == 1
    assert int(variants[0]["duration_minutes"]) == 420

    original_zone_info = availability_module.ZoneInfo
    try:
        availability_module.ZoneInfo = lambda _key: timezone.utc
        payload = build_yclients_payload(draft)
    finally:
        availability_module.ZoneInfo = original_zone_info

    assert payload["phone"] == "79022613470"
    assert payload["fullname"] == "Савелий"
    assert payload["notify_by_sms"] == 0
    assert payload["notify_by_email"] == 0
    assert len(payload["appointments"]) == 1
    assert payload["appointments"][0]["datetime"] == "2026-08-03T12:00:00"

    comment = payload["comment"]
    assert "Объект: Баня." in comment
    assert "Предоплата внесена через YooKassa: 50%." in comment
    assert "YooKassa payment_id: pay-test." in comment
    assert "Важно: в YClients используется услуга бани на 7 часов" in comment
    assert "Доплата сверх 7 часов: 1 500 ₽." in comment


def _assert_post_payment_contract() -> None:
    draft = BookingDraft(
        service_type="bathhouse",
        date="2026-08-03",
        time="12:00",
        duration=8,
        guests_count=6,
        event_format="отдых",
        upsell_done=True,
        client_name="Савелий",
        phone="+79022613470",
    )
    messages = build_post_payment_messages(draft)
    assert len(messages) >= 2
    assert "Оплату получила ✅" in messages[0]
    assert "Бронь подтверждена" in messages[0]
    assert "Стоимость бронирования: 16 200 ₽" in messages[0]
    assert "Предоплата 50%: 8 100 ₽" in messages[0]
    assert "vc7ki6Pm3LPYQA" in messages[1]


def main() -> None:
    _assert_next_step_contract()
    _assert_catalog_alias_contract()
    _assert_pricing_and_payment_contract()
    _assert_media_contract()
    _assert_yclients_payload_contract()
    _assert_post_payment_contract()
    print("OK booking flow smoke")


if __name__ == "__main__":
    main()
