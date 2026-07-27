from __future__ import annotations

import gc
import os
import sqlite3
import sys
import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))


def _utc_now() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


def main() -> None:
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        tmp_path = Path(tmp)
        db_path = tmp_path / "bot.sqlite3"
        env_path = tmp_path / ".env"
        env_path.write_text(
            "\n".join(
                [
                    f"SQLITE_PATH={db_path}",
                    f"VOICE_TEMP_DIR={tmp_path / 'voice_tmp'}",
                    "DB_HOST=",
                    "DB_NAME=",
                    "DB_USER=",
                ]
            ),
            encoding="utf-8",
        )
        os.environ["APP_ENV_FILE"] = str(env_path)
        os.environ["SQLITE_PATH"] = str(db_path)
        os.environ["DB_HOST"] = ""
        os.environ["DB_NAME"] = ""
        os.environ["DB_USER"] = ""

        from app.maintenance.retention import RetentionPolicy, build_retention_report, render_retention_report
        from app.storage.sqlite import init_db

        init_db()
        now = _utc_now().replace(microsecond=0)
        old = (now - timedelta(days=120)).isoformat()
        recent = now.isoformat()
        yesterday = (now - timedelta(days=1)).date().isoformat()
        future = (now + timedelta(days=1)).date().isoformat()

        conn = sqlite3.connect(db_path)
        try:
            conn.execute(
                """
                INSERT INTO mvp_messages(chat_id, sender, text, raw_json, created_at)
                VALUES ('chat-old', 'user', 'old message', '{}', ?)
                """,
                (old,),
            )
            conn.execute(
                """
                INSERT INTO mvp_messages(chat_id, sender, text, raw_json, created_at)
                VALUES ('chat-recent', 'assistant', 'recent message', '{}', ?)
                """,
                (recent,),
            )
            conn.execute(
                """
                INSERT INTO mvp_conversations(chat_id, draft_json, status, current_step, updated_at)
                VALUES ('chat-closed', '{}', 'closed', NULL, ?)
                """,
                (old,),
            )
            conn.execute(
                """
                INSERT INTO mvp_conversations(chat_id, draft_json, status, current_step, updated_at)
                VALUES ('chat-active', '{}', 'active', NULL, ?)
                """,
                (old,),
            )
            conn.execute(
                """
                INSERT INTO mvp_bookings(
                    chat_id, platform, draft_json, status, payment_id, yclients_record_id, created_at, updated_at
                )
                VALUES ('chat-booked', 'max', '{}', 'booked', 'pay-1', 'rec-1', ?, ?)
                """,
                (recent, recent),
            )
            conn.execute(
                """
                INSERT INTO mvp_bookings(
                    chat_id, platform, draft_json, status, payment_id, yclients_record_id, created_at, updated_at
                )
                VALUES ('chat-old-booking', 'max', '{}', 'canceled', NULL, NULL, ?, ?)
                """,
                (old, old),
            )
            conn.execute(
                """
                INSERT INTO mvp_slot_holds(
                    chat_id, service_type, service_variant, date, time, duration, status, expires_at, created_at, updated_at
                )
                VALUES ('chat-hold-old', 'gazebo', 'Gazebo 1', ?, '10:00', 2, 'released', ?, ?, ?)
                """,
                (future, old, old, old),
            )
            conn.execute(
                """
                INSERT INTO mvp_slot_holds(
                    chat_id, service_type, service_variant, date, time, duration, status, expires_at, created_at, updated_at
                )
                VALUES ('chat-hold-expired', 'gazebo', 'Gazebo 2', ?, '10:00', 2, 'active', ?, ?, ?)
                """,
                (future, old, old, old),
            )
            conn.execute(
                """
                INSERT INTO mvp_system_logs(level, event, message, payload_json, created_at)
                VALUES ('INFO', 'old', 'old log', '{}', ?)
                """,
                (old,),
            )
            conn.execute(
                """
                INSERT INTO mvp_admin_notifications(chat_id, message, status, created_at, sent_at)
                VALUES ('chat-notify-sent', 'sent', 'sent', ?, ?)
                """,
                (old, old),
            )
            conn.execute(
                """
                INSERT INTO mvp_admin_notifications(chat_id, message, status, created_at, sent_at)
                VALUES ('chat-notify-pending', 'pending', 'pending', ?, NULL)
                """,
                (old,),
            )
            conn.execute(
                """
                INSERT INTO mvp_availability_cache(service_type, title, date, time, service_id, staff_id, status, refreshed_at)
                VALUES ('gazebo', 'Gazebo 1', ?, '10:00', 'svc-1', 'staff-1', 'free', ?)
                """,
                (yesterday, old),
            )
            conn.execute(
                """
                INSERT INTO mvp_availability_watchlist(chat_id, service_type, object_title, date, time, duration, status, created_at, notified_at)
                VALUES ('chat-watch-old', 'gazebo', 'Gazebo 1', ?, '10:00', 2, 'notified', ?, ?)
                """,
                (yesterday, old, old),
            )
            conn.execute(
                """
                INSERT INTO mvp_availability_watchlist(chat_id, service_type, object_title, date, time, duration, status, created_at)
                VALUES ('chat-watch-active', 'gazebo', 'Gazebo 2', ?, '10:00', 2, 'active', ?)
                """,
                (yesterday, old),
            )
            conn.commit()
        finally:
            conn.close()

        report = build_retention_report(
            RetentionPolicy(messages_days=90, booking_review_days=90),
            limit=5,
            include_files=False,
            now=now,
        )

        assert report["mode"] == "read_only"
        assert report["database"]["backend"] == "sqlite"
        assert report["cleanup_candidates"]["old_messages"]["count"] == 1
        assert report["cleanup_candidates"]["old_inactive_conversations"]["count"] == 1
        assert report["cleanup_candidates"]["old_system_logs"]["count"] == 1
        assert report["cleanup_candidates"]["old_inactive_slot_holds"]["count"] == 1
        assert report["cleanup_candidates"]["active_holds_past_expiry"]["count"] == 1
        assert report["cleanup_candidates"]["old_sent_admin_notifications"]["count"] == 1
        assert report["cleanup_candidates"]["stale_availability_cache"]["count"] == 1
        assert report["cleanup_candidates"]["past_availability_cache_dates"]["count"] == 1
        assert report["cleanup_candidates"]["old_inactive_watchlist"]["count"] == 1
        assert report["cleanup_candidates"]["active_watchlist_past_dates"]["count"] == 1
        assert report["cleanup_candidates"]["old_unprotected_bookings_for_review"]["count"] == 1
        assert report["protected_data"]["protected_bookings"]["count"] == 1
        assert report["protected_data"]["active_conversations"]["count"] == 1
        assert report["protected_data"]["active_slot_holds"]["count"] == 1
        assert report["protected_data"]["pending_admin_notifications"]["count"] == 1
        assert report["protected_data"]["active_watchlist_rows"]["count"] == 1
        assert any("mvp_processed_updates" in item for item in report["schema_warnings"])
        assert any("platform" in item for item in report["schema_warnings"])
        assert "old message" not in render_retention_report(report)
        print("OK retention report smoke")
        gc.collect()


if __name__ == "__main__":
    main()
