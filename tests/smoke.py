from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from app.ai.parser import _decision_from_plain_text, fallback_decision
from app.dialog.state import BookingDraft
from app.storage.sqlite import active_hold_exists, init_db, release_holds, upsert_hold


def main() -> None:
    parsed = fallback_decision("Беседку на 15 человек")
    assert parsed.intent == "info"
    assert parsed.fields_patch == {}
    assert parsed.action.type == "none"

    plain = _decision_from_plain_text("Здравствуйте! Что хотите забронировать?")
    assert plain is not None
    assert plain.reply == "Здравствуйте! Что хотите забронировать?"

    draft = BookingDraft(service_type="gazebo", date="2026-06-06", guests_count=15)
    assert draft.next_step() == "service_variant"

    init_db()
    hold = {
        "service_type": "gazebo",
        "service_variant": "Беседка №1",
        "date": "2026-06-06",
        "time": "18:00",
        "duration": 6,
    }
    release_holds("smoke-a")
    upsert_hold("smoke-a", hold)
    assert not active_hold_exists(hold, ignore_chat_id="smoke-a")
    assert active_hold_exists(hold, ignore_chat_id="smoke-b")
    release_holds("smoke-a")
    print("OK smoke")


if __name__ == "__main__":
    main()
