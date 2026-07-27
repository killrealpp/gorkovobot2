from __future__ import annotations

import gc
import os
import sqlite3
import subprocess
import sys
import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))


def _utc_now() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


def _count_rows(db_path: Path, table: str) -> int:
    conn = sqlite3.connect(db_path)
    try:
        row = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()
        return int(row[0])
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

        missing_apply = subprocess.run(
            [
                sys.executable,
                str(PROJECT_ROOT / "scripts" / "retention_cleanup.py"),
                "--apply",
                "--yes",
                "--max-rows",
                "5",
                "--skip-files",
                "--json",
            ],
            cwd=PROJECT_ROOT,
            env={**os.environ, "APP_ENV_FILE": str(env_path), "SQLITE_PATH": str(db_path), "DB_HOST": "", "DB_NAME": "", "DB_USER": ""},
            text=True,
            capture_output=True,
            check=True,
        )
        assert '"mode": "apply_skipped"' in missing_apply.stdout
        assert not db_path.exists()

        from app.maintenance.retention import RetentionPolicy, apply_cleanup, build_cleanup_plan, render_cleanup_plan
        from app.storage.sqlite import init_db

        init_db()
        now = _utc_now().replace(microsecond=0)
        old = (now - timedelta(days=120)).isoformat()
        recent = now.isoformat()
        future = (now + timedelta(days=1)).date().isoformat()

        conn = sqlite3.connect(db_path)
        try:
            for chat_id in ("chat-old-a", "chat-old-b"):
                conn.execute(
                    """
                    INSERT INTO mvp_messages(chat_id, sender, text, raw_json, created_at)
                    VALUES (?, 'user', 'old message', '{}', ?)
                    """,
                    (chat_id, old),
                )
            conn.execute(
                """
                INSERT INTO mvp_messages(chat_id, sender, text, raw_json, created_at)
                VALUES ('chat-recent', 'user', 'recent message', '{}', ?)
                """,
                (recent,),
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
                INSERT INTO mvp_bookings(
                    chat_id, platform, draft_json, status, payment_id, yclients_record_id, created_at, updated_at
                )
                VALUES ('chat-old-booking', 'max', '{}', 'canceled', NULL, NULL, ?, ?)
                """,
                (old, old),
            )
            conn.execute(
                """
                INSERT INTO mvp_bookings(
                    chat_id, platform, draft_json, status, payment_id, yclients_record_id, created_at, updated_at
                )
                VALUES ('chat-protected-booking', 'max', '{}', 'booked', 'pay-1', 'rec-1', ?, ?)
                """,
                (old, old),
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
                INSERT INTO mvp_slot_holds(
                    chat_id, service_type, service_variant, date, time, duration, status, expires_at, created_at, updated_at
                )
                VALUES ('chat-hold-expired', 'gazebo', 'Gazebo 2', ?, '10:00', 2, 'active', ?, ?, ?)
                """,
                (future, old, old, old),
            )
            conn.commit()
        finally:
            conn.close()

        before_messages = _count_rows(db_path, "mvp_messages")
        before_logs = _count_rows(db_path, "mvp_system_logs")
        before_bookings = _count_rows(db_path, "mvp_bookings")

        plan = build_cleanup_plan(RetentionPolicy(messages_days=90), limit=5, include_files=False, now=now)
        assert plan["mode"] == "dry_run"
        assert plan["apply_supported"] is True
        assert plan["summary"]["items_with_matches"] >= 2
        assert plan["items"]
        rendered = render_cleanup_plan(plan)
        assert "old_messages" in rendered
        assert "old message" not in rendered

        cli = subprocess.run(
            [sys.executable, str(PROJECT_ROOT / "scripts" / "retention_cleanup.py"), "--limit", "5", "--skip-files", "--json"],
            cwd=PROJECT_ROOT,
            env={**os.environ, "APP_ENV_FILE": str(env_path), "SQLITE_PATH": str(db_path), "DB_HOST": "", "DB_NAME": "", "DB_USER": ""},
            text=True,
            capture_output=True,
            check=True,
        )
        assert '"mode": "dry_run"' in cli.stdout

        refused = subprocess.run(
            [sys.executable, str(PROJECT_ROOT / "scripts" / "retention_cleanup.py"), "--apply", "--skip-files", "--json"],
            cwd=PROJECT_ROOT,
            env={**os.environ, "APP_ENV_FILE": str(env_path), "SQLITE_PATH": str(db_path), "DB_HOST": "", "DB_NAME": "", "DB_USER": ""},
            text=True,
            capture_output=True,
            check=False,
        )
        assert refused.returncode == 2
        assert '"mode": "apply_refused"' in refused.stdout

        assert _count_rows(db_path, "mvp_messages") == before_messages
        assert _count_rows(db_path, "mvp_system_logs") == before_logs

        first = apply_cleanup(
            RetentionPolicy(messages_days=90),
            max_rows=1,
            include_files=False,
            only_keys=["old_messages"],
            now=now,
            confirmed=True,
        )
        assert first["mode"] == "apply"
        assert first["summary"]["deleted_rows"] == 1
        assert _count_rows(db_path, "mvp_messages") == before_messages - 1
        assert _count_rows(db_path, "mvp_system_logs") == before_logs

        second = apply_cleanup(
            RetentionPolicy(messages_days=90),
            max_rows=10,
            include_files=False,
            only_keys=["old_messages"],
            now=now,
            confirmed=True,
        )
        assert second["summary"]["deleted_rows"] == 1
        third = apply_cleanup(
            RetentionPolicy(messages_days=90),
            max_rows=10,
            include_files=False,
            only_keys=["old_messages"],
            now=now,
            confirmed=True,
        )
        assert third["summary"]["deleted_rows"] == 0
        assert _count_rows(db_path, "mvp_messages") == 1
        assert _count_rows(db_path, "mvp_bookings") == before_bookings

        repair = apply_cleanup(
            RetentionPolicy(messages_days=90),
            max_rows=10,
            include_files=False,
            only_keys=["active_holds_past_expiry"],
            now=now,
            confirmed=True,
        )
        assert repair["summary"]["updated_rows"] == 1
        conn = sqlite3.connect(db_path)
        try:
            row = conn.execute("SELECT status FROM mvp_slot_holds WHERE chat_id = 'chat-hold-expired'").fetchone()
            assert row and row[0] == "expired"
        finally:
            conn.close()

        notifications = apply_cleanup(
            RetentionPolicy(messages_days=90),
            max_rows=10,
            include_files=False,
            only_keys=["old_sent_admin_notifications"],
            now=now,
            confirmed=True,
        )
        assert notifications["summary"]["deleted_rows"] == 1
        conn = sqlite3.connect(db_path)
        try:
            rows = conn.execute("SELECT status FROM mvp_admin_notifications ORDER BY status").fetchall()
            assert [row[0] for row in rows] == ["pending"]
        finally:
            conn.close()

        cli_apply = subprocess.run(
            [
                sys.executable,
                str(PROJECT_ROOT / "scripts" / "retention_cleanup.py"),
                "--apply",
                "--yes",
                "--max-rows",
                "10",
                "--only",
                "old_system_logs",
                "--skip-files",
                "--json",
            ],
            cwd=PROJECT_ROOT,
            env={**os.environ, "APP_ENV_FILE": str(env_path), "SQLITE_PATH": str(db_path), "DB_HOST": "", "DB_NAME": "", "DB_USER": ""},
            text=True,
            capture_output=True,
            check=True,
        )
        assert '"mode": "apply"' in cli_apply.stdout
        assert _count_rows(db_path, "mvp_system_logs") == 0
        print("OK retention cleanup guarded apply smoke")
        gc.collect()


if __name__ == "__main__":
    main()
