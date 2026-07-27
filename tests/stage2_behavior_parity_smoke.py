from __future__ import annotations

import os
import sys
from datetime import timezone
from decimal import Decimal


PROJECT_ROOT = os.path.dirname(os.path.dirname(__file__))
sys.path.insert(0, PROJECT_ROOT)

from app.bot.media import extract_media_titles_from_reply, paths_for_requested_media  # noqa: E402
from app.data.admin_profile import (  # noqa: E402
    booking_question,
    booking_start_reply,
    load_profile_services,
    payment_prepayment_percent,
)
from app.data.services import normalize_service_type, service_title  # noqa: E402
import app.dialog.availability as availability_module  # noqa: E402
from app.dialog.engine import _validate_business_rules  # noqa: E402
from app.dialog.payment import payment_amounts  # noqa: E402
from app.dialog.post_payment_message import build_post_payment_messages  # noqa: E402
from app.dialog.pricing import calculate_booking_price  # noqa: E402
from app.dialog.state import BookingDraft  # noqa: E402


def _assert_next_step_parity() -> None:
    cases = [
        (BookingDraft(), "service_type"),
        (BookingDraft(service_type="gazebo"), "date"),
        (BookingDraft(service_type="gazebo", date="2026-08-01"), "service_variant"),
        (BookingDraft(service_type="gazebo", date="2026-08-01", service_variant="Беседка №1"), "time"),
        (BookingDraft(service_type="gazebo", date="2026-08-01", service_variant="Беседка №1", time="18:00"), "duration"),
        (BookingDraft(service_type="bathhouse", date="2026-08-03"), "duration"),
        (BookingDraft(service_type="bathhouse", date="2026-08-03", time="12:00"), "duration"),
        (BookingDraft(service_type="house", date="2026-08-03"), "time"),
        (BookingDraft(service_type="house", date="2026-08-03", time="16:00"), "duration"),
        (BookingDraft(service_type="warm_gazebo", date="2026-08-03"), "time"),
    ]
    for draft, expected in cases:
        assert draft.next_step() == expected, (draft, draft.next_step(), expected)

    ready_for_upsell = BookingDraft(
        service_type="bathhouse",
        date="2026-08-03",
        time="12:00",
        duration=3,
        guests_count=4,
    )
    assert ready_for_upsell.next_step() == "upsell_items"


def _assert_text_parity() -> None:
    assert booking_start_reply() == (
        "Здравствуйте! Это бот для бронирования на базе отдыха «Причал» в Выксе. "
        "Что хотите забронировать: беседку, баню, дом или тёплую беседку?"
    )
    assert booking_question("service_type") == "что хотите забронировать — беседку, баню, дом или тёплую беседку?"
    assert booking_question("date") == "на какую дату планируете?"
    assert booking_question("service_variant") == "какую беседку выбираем?"
    assert booking_question("time") == "во сколько планируете приехать?"
    assert booking_question("duration") == "на сколько часов бронируем?"
    assert booking_question("guests_count") == "сколько вас будет человек?"
    assert booking_question("upsell_items") == "нужны ли допы — уголь, розжиг, лёд, посуда или кальян?"
    assert booking_question("client_name") == "как вас записать?"
    assert booking_question("phone") == "оставьте номер телефона для брони."
    assert booking_question("confirmation") == "проверьте заявку и подтвердите, если всё верно."
    assert booking_question(None) == "чем могу помочь?"

    assert service_title(None) == "услуга"
    assert service_title("gazebo") == "Беседка"
    assert service_title("bathhouse") == "Баня"
    assert service_title("house") == "Дом"
    assert service_title("warm_gazebo") == "Теплая беседка"


def _assert_business_rule_parity() -> None:
    services = load_profile_services()
    assert services["bathhouse"]["capacity_max"] == 15
    before = BookingDraft(service_type="bathhouse", guests_count=None)
    draft = BookingDraft(service_type="bathhouse", date="2026-08-03", guests_count=16)
    result = _validate_business_rules(before, draft)
    assert result is not None
    assert result["event"] == "capacity_exceeded"
    assert result["data"]["capacity_max"] == 15
    assert draft.guests_count is None


def _assert_pricing_and_payment_parity() -> None:
    assert calculate_booking_price(BookingDraft(service_type="gazebo", service_variant="Беседка №1")) == 10500
    assert calculate_booking_price(BookingDraft(service_type="bathhouse", date="2026-08-03", duration=8)) == 16200
    assert calculate_booking_price(BookingDraft(service_type="bathhouse", date="2026-08-01", duration=8)) == 20050
    assert calculate_booking_price(BookingDraft(service_type="house", date="2026-08-03", duration=8)) == 10500
    assert calculate_booking_price(BookingDraft(service_type="house", date="2026-08-07", duration=24)) == 12600

    priced = BookingDraft(service_type="bathhouse", date="2026-08-03", time="12:00", duration=8, guests_count=6)
    assert payment_prepayment_percent() == 50
    amounts = payment_amounts(priced)
    assert amounts["total"] == Decimal("16200.00")
    assert amounts["prepayment"] == Decimal("8100.00")
    assert amounts["remaining"] == Decimal("8100.00")


def _assert_media_parity() -> None:
    titles = extract_media_titles_from_reply("Вот фото вариантов: Баня с бассейном, Гостевой дом, Беседка 1")
    assert titles == ["bathhouse", "house", "Беседка №1"]

    paths = paths_for_requested_media(["bathhouse", "house", "Теплая беседка", "Беседка №1"])
    assert [path.name for path in paths] == ["banya.jpg", "dom_gostevoy.jpg", "besedka_teplaya.jpg", "besedka1.jpg"]


def _assert_post_payment_and_yclients_text_parity() -> None:
    draft = BookingDraft(service_type="bathhouse", date="2026-08-03", time="12:00", duration=8, guests_count=6)
    messages = build_post_payment_messages(draft)
    assert messages[0] == (
        "Оплату получила ✅\n\n"
        "Бронь подтверждена. Ждём вас!\n\n"
        "Ваша бронь: Баня с бассейном, 3 августа, в 12:00, на 8 часов.\n\n"
        "Стоимость бронирования: 16 200 ₽\n"
        "Предоплата 50%: 8 100 ₽\n"
        "Остаток при посещении: 8 100 ₽"
    )
    assert "Видеоинструкция по приезде в баню с бассейном:" in messages[1]
    assert "https://disk.yandex.ru/d/vc7ki6Pm3LPYQA" in messages[1]
    assert "поэтому хотели бы поделиться общей информацией" in messages[1]
    assert "А также проинформируем вас" in messages[1]
    assert "На нашей территории допускается использование только угля" in messages[1]
    assert "И обращаем ваше внимание" in messages[1]

    availability_module.ZoneInfo = lambda _key: timezone.utc
    payload = availability_module.build_yclients_payload(draft)
    comment = payload["comment"]
    assert "Объект: Баня." in comment
    assert "Предоплата внесена через YooKassa: 50%." in comment
    assert "Важно: в YClients используется услуга бани на 7 часов" in comment
    assert "Доплата сверх 7 часов: 1 500 ₽." in comment


def _assert_parser_alias_parity() -> None:
    assert normalize_service_type("баня с бассейном") == "bathhouse"
    assert normalize_service_type("дом") == "house"
    assert normalize_service_type("гостевой дом") == "house"
    assert normalize_service_type("тёплая беседка") == "warm_gazebo"
    assert normalize_service_type("беседка номер 1") == "gazebo"


def main() -> None:
    _assert_next_step_parity()
    _assert_text_parity()
    _assert_business_rule_parity()
    _assert_pricing_and_payment_parity()
    _assert_media_parity()
    _assert_post_payment_and_yclients_text_parity()
    _assert_parser_alias_parity()
    print("OK stage2 behavior parity smoke")


if __name__ == "__main__":
    main()
