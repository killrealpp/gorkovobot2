from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from app.dialog.engine import (
    _allowed_action_type,
    _contextual_fields_from_history,
    _finalize_upsells_for_payment_intent,
    _is_confirmation_text,
    _is_summary_question,
    _merge_fields,
    _payment_intent,
    _ready_for_payment,
    _slot_ready_for_availability,
)
from app.dialog.state import BookingDraft


def main() -> None:
    draft = BookingDraft(
        service_type="gazebo",
        date="2026-06-20",
        guests_count=30,
        service_variant="Беседка №1",
        time="18:00",
        duration=6,
        event_format="просто отдых",
        client_name="Luv",
    )
    assert draft.next_step() == "upsell_items"
    _merge_fields(draft, {}, "Luv", "ничего не надо")
    assert draft.upsell_offer_count == 1
    assert not draft.upsell_done
    assert draft.next_step() == "upsell_items"
    _merge_fields(draft, {}, "Luv", "не надо допов")
    assert draft.upsell_offer_count == 2
    assert draft.upsell_done
    assert draft.next_step() == "phone"

    draft = BookingDraft(
        service_type="gazebo",
        date="2026-06-20",
        guests_count=30,
        service_variant="Беседка №1",
        time="18:00",
        duration=6,
    )
    _merge_fields(draft, {}, "Luv", "С 19 до 01")
    assert draft.service_variant == "Беседка №1"
    assert draft.time == "19:00"
    assert draft.duration == 6

    draft = BookingDraft(
        service_type="gazebo",
        date="2026-06-15",
        guests_count=15,
        service_variant="Беседка №6",
        time="18:00",
        duration=6,
        event_format="просто отдых",
        upsell_items=["кальян"],
        upsell_done=True,
        client_name="Luv",
        phone="89022613470",
        payment_id="old",
        payment_url="https://example.test/pay",
        status="waiting_payment",
    )
    _merge_fields(draft, {}, "Luv", "У нас свой кальян, нам не надо допов")
    assert draft.upsell_items == []
    assert draft.payment_id is None
    assert draft.payment_url is None
    assert draft.status == "collecting"

    draft = BookingDraft(service_type="gazebo", date="2026-06-20", guests_count=15)
    assert not _is_summary_question("А в чем разница между крытой и теплой?")
    _merge_fields(draft, {"service_type": "warm_gazebo"}, "Luv", "А в чем разница между крытой и теплой?")
    assert draft.service_type == "gazebo"
    _merge_fields(draft, {}, "Luv", "Теплую давай")
    assert draft.service_type == "warm_gazebo"
    assert draft.service_variant is None
    _merge_fields(draft, {}, "Luv", "с 6 вечера до 12 ночи")
    assert draft.service_type == "warm_gazebo"
    assert draft.time == "18:00"
    assert draft.duration == 6

    draft = BookingDraft(service_type="gazebo", date="2026-06-06")
    _merge_fields(draft, {"date": "2026-06-10", "guests_count": 10}, "Luv", "10")
    assert draft.date == "2026-06-06"
    assert draft.guests_count == 10

    draft = BookingDraft(service_type="warm_gazebo", date="2026-06-08", time="15:00", duration=17)
    _merge_fields(draft, {"date": "2026-06-06", "guests_count": 10}, "Luv", "10 человек")
    assert draft.date == "2026-06-08"
    assert draft.guests_count == 10
    _merge_fields(draft, {"date": "2026-06-06", "guests_count": 15}, "Luv", "ой, а нас 15 будет")
    assert draft.date == "2026-06-08"
    assert draft.guests_count == 15

    draft = BookingDraft(service_type="gazebo", date="2026-06-06", guests_count=10)
    _merge_fields(draft, {"date": "2026-06-10", "service_variant": "Беседка №3"}, "Luv", "третью")
    assert draft.date == "2026-06-06"
    assert draft.service_variant == "Беседка №3"

    draft = BookingDraft(
        service_type="gazebo",
        date="2026-06-06",
        guests_count=10,
        service_variant="Беседка №5",
        time="06:00",
        duration=24,
    )
    assert _slot_ready_for_availability(draft)

    draft = BookingDraft(service_type="warm_gazebo", guests_count=10)
    assert _allowed_action_type("list_available_dates", "А подешевле есть?", draft, semantic_info_question=False) == "none"

    draft = BookingDraft(service_type="warm_gazebo", date="2026-06-06", guests_count=10, duration=24)
    _merge_fields(draft, {}, "Luv", "Давай пятую, на завтра")
    assert draft.service_type == "gazebo"
    assert draft.service_variant == "Беседка №5"

    draft = BookingDraft(service_type="bathhouse", date="2026-06-06", guests_count=10, time="15:00")
    _merge_fields(
        draft,
        {"duration": 8, "event_format": "просто отдых"},
        "Luv",
        "Отдохнуть после тяжелой недели, много денег заработали",
    )
    assert draft.duration is None
    assert draft.event_format == "просто отдых"

    draft = BookingDraft(
        service_type="bathhouse",
        date="2026-06-06",
        guests_count=10,
        time="15:00",
        duration=5,
        event_format="просто отдых",
        upsell_items=["кальян"],
    )
    _merge_fields(draft, {"upsell_items": ["кальян"]}, "Luv", "Банька, бассейн, покушать и покурить кальян. Кальян у нас свой")
    assert draft.upsell_items == []

    draft = BookingDraft(
        service_type="warm_gazebo",
        date="2026-06-08",
        guests_count=10,
        time="15:00",
        duration=24,
        event_format="просто отдых",
        upsell_done=True,
    )
    reply = _merge_fields(draft, {"guests_count": 89022613470}, "Luv", "89022613470")
    assert reply is None
    assert draft.guests_count == 10
    assert draft.phone == "+79022613470"
    assert _is_confirmation_text("Формируй")
    assert _is_confirmation_text("И где ссылка?")
    assert _is_confirmation_text("Газ")
    assert _is_confirmation_text("погнали")
    assert _payment_intent("Все давай я готов внести оплату")

    ready = BookingDraft(
        service_type="warm_gazebo",
        date="2026-06-08",
        guests_count=10,
        time="18:00",
        duration=24,
        event_format="просто отдых",
        upsell_done=True,
        client_name="Савелий",
        phone="+79022613470",
    )
    assert _allowed_action_type("create_payment", "Да", ready, semantic_info_question=False) == "create_payment"

    ready_with_open_upsells = BookingDraft(
        service_type="warm_gazebo",
        date="2026-06-06",
        guests_count=10,
        time="18:00",
        duration=24,
        event_format="просто отдых",
        upsell_items=["забивка + уголь"],
        client_name="Савелий",
        phone="+79022613470",
    )
    assert not _ready_for_payment(ready_with_open_upsells)
    _finalize_upsells_for_payment_intent(ready_with_open_upsells)
    assert _ready_for_payment(ready_with_open_upsells)

    house = BookingDraft(service_type="house", date="2026-06-07", guests_count=15)
    _merge_fields(house, {"duration": 7}, "Luv", "на 7 часов")
    assert house.duration == 7
    assert house.next_step() == "time"
    _merge_fields(house, {"time": "16:30", "duration": 7}, "Luv", "в 16 30 тогда")
    assert house.time == "16:30"
    assert house.duration == 7
    assert house.next_step() == "upsell_items"
    _merge_fields(house, {}, "Luv", "на месте решу")
    assert house.upsell_items == []
    assert house.upsell_done
    assert house.next_step() == "client_name"

    contextual = _contextual_fields_from_history(
        "Да",
        BookingDraft(guests_count=10),
        [
            {
                "sender": "assistant",
                "text": "Для 10 человек я бы посоветовала Тёплую беседку. На 7 июня есть свободные старты.",
            }
        ],
    )
    assert contextual["service_type"] == "warm_gazebo"
    assert contextual["date"] == "2026-06-07"

    print("OK booking flow")


if __name__ == "__main__":
    main()
