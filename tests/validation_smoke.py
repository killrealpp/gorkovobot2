from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from app.dialog.engine import _merge_fields
from app.dialog.state import BookingDraft


def main() -> None:
    draft = BookingDraft(service_type="bathhouse")
    reply = _merge_fields(draft, {"date": "2026-12-01"}, "Test", "на -1 декабря 2026")
    assert reply
    assert draft.date is None

    draft = BookingDraft(service_type="bathhouse", date="2026-06-06", guests_count=10)
    reply = _merge_fields(draft, {"guests_count": 15, "duration": 6}, "Test", "15 60")
    assert reply
    assert draft.guests_count == 10
    assert draft.time is None
    assert draft.duration is None

    draft = BookingDraft(service_type="bathhouse", date="2026-06-06", guests_count=10, time="15:00")
    reply = _merge_fields(draft, {"duration": 2}, "Test", "2")
    assert reply
    assert draft.duration is None

    draft = BookingDraft(service_type="bathhouse", date="2026-06-06", guests_count=10, time="15:00")
    reply = _merge_fields(draft, {}, "Test", "5")
    assert reply is None
    assert draft.duration == 5

    draft = BookingDraft(service_type="warm_gazebo", date="2026-06-08", guests_count=10, time="18:00", duration=24)
    reply = _merge_fields(draft, {"phone": "8902261470"}, "Test", "8902261470")
    assert reply
    assert draft.phone is None

    draft = BookingDraft(service_type="warm_gazebo", date="2026-06-08", guests_count=10, time="18:00", duration=24)
    reply = _merge_fields(draft, {"phone": "9022613470"}, "Test", "9022613470")
    assert reply
    assert draft.phone is None

    draft = BookingDraft(service_type="house", date="2026-06-07", guests_count=15, time="16:30", duration=7, upsell_done=True)
    reply = _merge_fields(draft, {"client_name": "Савелий", "phone": "+79022613477"}, "Test", "Савелий\n8902261347700")
    assert reply
    assert draft.phone is None

    draft = BookingDraft(service_type="house", date="2026-06-07", guests_count=15, time="16:30", duration=7, upsell_done=True)
    reply = _merge_fields(draft, {"client_name": "Савелий", "phone": "+79022613470"}, "Test", "Савелий\n89022613470")
    assert reply is None
    assert draft.phone == "+79022613470"

    print("OK validation")


if __name__ == "__main__":
    main()
