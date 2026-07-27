from __future__ import annotations

import os
import sqlite3
import sys
import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))


def _count_rows(db_path: Path, table: str) -> int:
    conn = sqlite3.connect(db_path)
    try:
        return int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
    finally:
        conn.close()


def main() -> None:
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        tmp_path = Path(tmp)
        db_path = tmp_path / "bot.sqlite3"
        env_path = tmp_path / ".env"
        env_path.write_text(
            "\n".join(
                [
                    f"SQLITE_PATH={db_path}",
                    "DB_HOST=",
                    "DB_NAME=",
                    "DB_USER=",
                    "RETENTION_CLEANUP_ENABLED=true",
                    "RETENTION_CLEANUP_DRY_RUN=false",
                    "RETENTION_CLEANUP_ONLY=old_messages",
                    "RETENTION_CLEANUP_MAX_ROWS=10",
                    "RETENTION_MESSAGES_DAYS=90",
                    "RETENTION_CLEANUP_INCLUDE_FILES=false",
                ]
            ),
            encoding="utf-8",
        )
        os.environ["APP_ENV_FILE"] = str(env_path)
        os.environ["SQLITE_PATH"] = str(db_path)
        os.environ["DB_HOST"] = ""
        os.environ["DB_NAME"] = ""
        os.environ["DB_USER"] = ""

        from app.core.config import get_settings
        from app.maintenance.scheduler import run_retention_cleanup_once
        from app.storage.sqlite import init_db

        get_settings.cache_clear()
        init_db()

        now = datetime.now(UTC).replace(tzinfo=None, microsecond=0)
        old = (now - timedelta(days=120)).isoformat()
        recent = now.isoformat()
        conn = sqlite3.connect(db_path)
        try:
            conn.execute(
                "INSERT INTO mvp_messages(chat_id, sender, text, raw_json, created_at) VALUES (?, 'user', 'old', '{}', ?)",
                ("chat-old", old),
            )
            conn.execute(
                "INSERT INTO mvp_messages(chat_id, sender, text, raw_json, created_at) VALUES (?, 'user', 'recent', '{}', ?)",
                ("chat-new", recent),
            )
            conn.commit()
        finally:
            conn.close()

        assert _count_rows(db_path, "mvp_messages") == 2
        result = run_retention_cleanup_once()
        assert result["mode"] == "apply"
        assert result["summary"]["deleted_rows"] == 1
        assert _count_rows(db_path, "mvp_messages") == 1

        os.environ["RETENTION_CLEANUP_DRY_RUN"] = "true"
        os.environ["RETENTION_CLEANUP_ONLY"] = ""
        get_settings.cache_clear()
        yesterday = (now - timedelta(days=1)).date().isoformat()
        future = (now + timedelta(days=1)).date().isoformat()
        conn = sqlite3.connect(db_path)
        try:
            conn.execute(
                """
                INSERT INTO mvp_slot_holds(
                    chat_id, service_type, service_variant, date, time, duration, status, expires_at, created_at, updated_at
                )
                VALUES ('chat-released-hold', 'gazebo', 'Gazebo 1', ?, '10:00', 2, 'released', ?, ?, ?)
                """,
                (future, old, old, old),
            )
            conn.execute(
                """
                INSERT INTO mvp_availability_cache(service_type, title, date, time, service_id, staff_id, status, refreshed_at)
                VALUES ('gazebo', 'Gazebo 1', ?, '10:00', 'svc-1', 'staff-1', 'free', ?)
                """,
                (yesterday, recent),
            )
            conn.commit()
        finally:
            conn.close()

        dry_run = run_retention_cleanup_once()
        item_counts = {item["key"]: item["count"] for item in dry_run["items"]}
        assert dry_run["mode"] == "dry_run"
        assert item_counts["old_inactive_slot_holds"] == 1
        assert item_counts["past_availability_cache_dates"] == 1

        print("OK retention scheduler smoke")


if __name__ == "__main__":
    main()
