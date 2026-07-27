from __future__ import annotations

import json
import sqlite3
import time
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Iterator

import psycopg2
from psycopg2.extras import RealDictCursor

from app.core.config import get_settings, sqlite_path


T_CONVERSATIONS = "mvp_conversations"
T_MESSAGES = "mvp_messages"
T_BOOKINGS = "mvp_bookings"
T_SLOT_HOLDS = "mvp_slot_holds"
T_SYSTEM_LOGS = "mvp_system_logs"
T_ADMIN_NOTIFICATIONS = "mvp_admin_notifications"
T_AVAILABILITY_CACHE = "mvp_availability_cache"
T_AVAILABILITY_WATCHLIST = "mvp_availability_watchlist"


def _use_postgres() -> bool:
    settings = get_settings()
    return bool(settings.db_host and settings.db_name and settings.db_user)


def init_db() -> None:
    if _use_postgres():
        _init_postgres()
        return
    path = sqlite_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(path) as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS mvp_conversations (
                chat_id TEXT PRIMARY KEY,
                draft_json TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'active',
                current_step TEXT,
                updated_at TEXT NOT NULL
            )
            """
        )
        _add_column(conn, T_CONVERSATIONS, "status", "TEXT NOT NULL DEFAULT 'active'")
        _add_column(conn, T_CONVERSATIONS, "current_step", "TEXT")
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS mvp_messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id TEXT NOT NULL,
                sender TEXT NOT NULL,
                text TEXT NOT NULL,
                raw_json TEXT,
                created_at TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS mvp_bookings (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id TEXT NOT NULL,
                platform TEXT NOT NULL DEFAULT 'max',
                draft_json TEXT NOT NULL,
                status TEXT NOT NULL,
                payment_id TEXT,
                yclients_record_id TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        _add_column(conn, T_BOOKINGS, "platform", "TEXT NOT NULL DEFAULT 'max'")
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS mvp_slot_holds (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id TEXT NOT NULL,
                service_type TEXT NOT NULL,
                service_variant TEXT,
                date TEXT NOT NULL,
                time TEXT NOT NULL,
                duration REAL,
                status TEXT NOT NULL DEFAULT 'active',
                expires_at TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS mvp_system_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                level TEXT NOT NULL,
                event TEXT NOT NULL,
                message TEXT,
                payload_json TEXT,
                created_at TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS mvp_admin_notifications (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id TEXT,
                message TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending',
                created_at TEXT NOT NULL,
                sent_at TEXT
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS mvp_availability_cache (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                service_type TEXT NOT NULL,
                title TEXT NOT NULL,
                date TEXT NOT NULL,
                time TEXT,
                service_id TEXT NOT NULL,
                staff_id TEXT NOT NULL,
                status TEXT NOT NULL,
                refreshed_at TEXT NOT NULL
            )
            """
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_mvp_availability_lookup ON mvp_availability_cache(staff_id, service_id, date)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_mvp_availability_filter ON mvp_availability_cache(service_type, date)")
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS mvp_availability_watchlist (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id TEXT NOT NULL,
                service_type TEXT,
                object_title TEXT NOT NULL,
                date TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'active',
                created_at TEXT NOT NULL,
                notified_at TEXT,
                canceled_at TEXT
            )
            """
        )
        _add_column(conn, T_AVAILABILITY_WATCHLIST, "time", "TEXT")
        _add_column(conn, T_AVAILABILITY_WATCHLIST, "duration", "REAL")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_mvp_watchlist_active ON mvp_availability_watchlist(status, date)")
        conn.commit()


def _init_postgres() -> None:
    with connect() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS mvp_conversations (
                chat_id TEXT PRIMARY KEY,
                draft_json TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'active',
                current_step TEXT,
                updated_at TEXT NOT NULL
            )
            """
        )
        conn.execute("ALTER TABLE mvp_conversations ADD COLUMN IF NOT EXISTS status TEXT NOT NULL DEFAULT 'active'")
        conn.execute("ALTER TABLE mvp_conversations ADD COLUMN IF NOT EXISTS current_step TEXT")
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS mvp_messages (
                id BIGSERIAL PRIMARY KEY,
                chat_id TEXT NOT NULL,
                sender TEXT NOT NULL,
                text TEXT NOT NULL,
                raw_json TEXT,
                created_at TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS mvp_bookings (
                id BIGSERIAL PRIMARY KEY,
                chat_id TEXT NOT NULL,
                platform TEXT NOT NULL DEFAULT 'max',
                draft_json TEXT NOT NULL,
                status TEXT NOT NULL,
                payment_id TEXT,
                yclients_record_id TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        conn.execute("ALTER TABLE mvp_bookings ADD COLUMN IF NOT EXISTS platform TEXT NOT NULL DEFAULT 'max'")
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS mvp_slot_holds (
                id BIGSERIAL PRIMARY KEY,
                chat_id TEXT NOT NULL,
                service_type TEXT NOT NULL,
                service_variant TEXT,
                date TEXT NOT NULL,
                time TEXT NOT NULL,
                duration DOUBLE PRECISION,
                status TEXT NOT NULL DEFAULT 'active',
                expires_at TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS mvp_system_logs (
                id BIGSERIAL PRIMARY KEY,
                level TEXT NOT NULL,
                event TEXT NOT NULL,
                message TEXT,
                payload_json TEXT,
                created_at TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS mvp_admin_notifications (
                id BIGSERIAL PRIMARY KEY,
                chat_id TEXT,
                message TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending',
                created_at TEXT NOT NULL,
                sent_at TEXT
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS mvp_availability_cache (
                id BIGSERIAL PRIMARY KEY,
                service_type TEXT NOT NULL,
                title TEXT NOT NULL,
                date TEXT NOT NULL,
                time TEXT,
                service_id TEXT NOT NULL,
                staff_id TEXT NOT NULL,
                status TEXT NOT NULL,
                refreshed_at TEXT NOT NULL
            )
            """
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_mvp_availability_lookup ON mvp_availability_cache(staff_id, service_id, date)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_mvp_availability_filter ON mvp_availability_cache(service_type, date)")
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS mvp_availability_watchlist (
                id BIGSERIAL PRIMARY KEY,
                chat_id TEXT NOT NULL,
                service_type TEXT,
                object_title TEXT NOT NULL,
                date TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'active',
                created_at TEXT NOT NULL,
                notified_at TEXT,
                canceled_at TEXT
            )
            """
        )
        conn.execute("ALTER TABLE mvp_availability_watchlist ADD COLUMN IF NOT EXISTS time TEXT")
        conn.execute("ALTER TABLE mvp_availability_watchlist ADD COLUMN IF NOT EXISTS duration DOUBLE PRECISION")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_mvp_watchlist_active ON mvp_availability_watchlist(status, date)")


def _add_column(conn: sqlite3.Connection, table: str, name: str, definition: str) -> None:
    columns = {row[1] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}
    if name not in columns:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {definition}")


def _postgres_kwargs() -> dict[str, Any]:
    settings = get_settings()
    kwargs: dict[str, Any] = {
        "host": settings.db_host,
        "port": settings.db_port,
        "dbname": settings.db_name,
        "user": settings.db_user,
        "password": settings.db_password,
        "connect_timeout": settings.db_connect_timeout,
        "cursor_factory": RealDictCursor,
    }
    if settings.db_sslmode:
        kwargs["sslmode"] = settings.db_sslmode
    if settings.db_sslmode in {"verify-ca", "verify-full"} and settings.db_sslrootcert:
        kwargs["sslrootcert"] = str(Path(settings.db_sslrootcert).expanduser())
    if settings.db_target_session_attrs:
        kwargs["target_session_attrs"] = settings.db_target_session_attrs
    return kwargs


class _PostgresConnection:
    def __init__(self) -> None:
        last_error: Exception | None = None
        for attempt in range(3):
            try:
                self._conn = psycopg2.connect(**_postgres_kwargs())
                return
            except psycopg2.OperationalError as exc:
                last_error = exc
                if attempt == 2:
                    raise
                time.sleep(1 + attempt)
        raise last_error or RuntimeError("Postgres connection failed")

    def execute(self, query: str, params: Any = None):
        cur = self._conn.cursor()
        cur.execute(_pg_query(query), params)
        return cur

    def commit(self) -> None:
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()


def _pg_query(query: str) -> str:
    return query.replace("?", "%s")


@contextmanager
def connect() -> Iterator[Any]:
    if _use_postgres():
        conn = _PostgresConnection()
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()
        return
    conn = sqlite3.connect(sqlite_path())
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def load_draft(chat_id: str) -> dict:
    with connect() as conn:
        row = conn.execute(f"SELECT draft_json FROM {T_CONVERSATIONS} WHERE chat_id = ?", (chat_id,)).fetchone()
    return json.loads(row["draft_json"]) if row else {}


def save_draft(chat_id: str, draft: dict, *, status: str = "active", current_step: str | None = None) -> None:
    now = datetime.utcnow().isoformat()
    with connect() as conn:
        conn.execute(
            """
            INSERT INTO mvp_conversations(chat_id, draft_json, status, current_step, updated_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(chat_id) DO UPDATE SET
                draft_json = excluded.draft_json,
                status = excluded.status,
                current_step = excluded.current_step,
                updated_at = excluded.updated_at
            """,
            (chat_id, json.dumps(draft, ensure_ascii=False), status, current_step, now),
        )


def add_message(chat_id: str, sender: str, text: str, raw: dict | None = None) -> None:
    now = datetime.utcnow().isoformat()
    with connect() as conn:
        conn.execute(
            """
            INSERT INTO mvp_messages(chat_id, sender, text, raw_json, created_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (chat_id, sender, text, json.dumps(raw or {}, ensure_ascii=False), now),
        )


def list_recent_messages(
    chat_id: str,
    *,
    limit: int = 12,
    since: str | None = None,
) -> list[dict]:
    params: list[Any] = [chat_id]
    since_clause = ""
    if since:
        since_clause = " AND created_at >= ?"
        params.append(since)
    params.append(limit)
    with connect() as conn:
        rows = conn.execute(
            f"""
            SELECT sender, text, created_at
            FROM {T_MESSAGES}
            WHERE chat_id = ?
            {since_clause}
            ORDER BY id DESC
            LIMIT ?
            """,
            params,
        ).fetchall()
    return list(reversed([dict(row) for row in rows]))


def clear_messages(chat_id: str) -> None:
    with connect() as conn:
        conn.execute(f"DELETE FROM {T_MESSAGES} WHERE chat_id = ?", (chat_id,))


def create_booking(chat_id: str, draft: dict, status: str, *, platform: str | None = "max") -> int:
    now = datetime.utcnow().isoformat()
    platform_value = str(platform or "max").strip().lower()
    with connect() as conn:
        query = """
            INSERT INTO mvp_bookings(chat_id, platform, draft_json, status, payment_id, yclients_record_id, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """
        if _use_postgres():
            query += " RETURNING id"
        cur = conn.execute(
            query,
            (
                    chat_id,
                    platform_value,
                    json.dumps(draft, ensure_ascii=False),
                    status,
                    draft.get("payment_id"),
                    draft.get("yclients_record_id"),
                    now,
                    now,
                ),
            )
        if _use_postgres():
            row = cur.fetchone()
            return int(row["id"])
        return int(cur.lastrowid)


def update_booking(booking_id: int, draft: dict, status: str | None = None) -> None:
    now = datetime.utcnow().isoformat()
    with connect() as conn:
        conn.execute(
            """
            UPDATE mvp_bookings
            SET draft_json = ?, status = COALESCE(?, status), payment_id = ?, yclients_record_id = ?, updated_at = ?
            WHERE id = ?
            """,
            (
                json.dumps(draft, ensure_ascii=False),
                status,
                draft.get("payment_id"),
                draft.get("yclients_record_id"),
                now,
                booking_id,
            ),
        )


def list_pending_payments(*, platform: str | None = None) -> list[dict]:
    with connect() as conn:
        if platform:
            rows = conn.execute(
                f"SELECT * FROM {T_BOOKINGS} WHERE status IN ('waiting_payment', 'payment_superseded') AND payment_id IS NOT NULL AND platform = ? ORDER BY id ASC",
                (str(platform).strip().lower(),),
            ).fetchall()
        else:
            rows = conn.execute(
                f"SELECT * FROM {T_BOOKINGS} WHERE status IN ('waiting_payment', 'payment_superseded') AND payment_id IS NOT NULL ORDER BY id ASC"
            ).fetchall()
    return [dict(row) for row in rows]


def latest_booking(chat_id: str) -> dict | None:
    with connect() as conn:
        row = conn.execute(
            f"SELECT * FROM {T_BOOKINGS} WHERE chat_id = ? ORDER BY id DESC LIMIT 1",
            (chat_id,),
        ).fetchone()
    return dict(row) if row else None


def expire_holds() -> None:
    now = datetime.utcnow().isoformat()
    with connect() as conn:
        conn.execute(
            f"UPDATE {T_SLOT_HOLDS} SET status = 'expired', updated_at = ? WHERE status = 'active' AND expires_at <= ?",
            (now, now),
        )



def active_holds_for_service_date(service_type: str, date: str, *, ignore_chat_id: str | None = None) -> list[dict]:
    """Return active holds for service/date, optionally excluding current chat.

    Used for date-level answers: the whole date may still have available times,
    but we should not tell another client that everything is simply free while
    a concrete time is already reserved before payment.
    """
    expire_holds()
    if not service_type or not date:
        return []
    result: list[dict] = []
    with connect() as conn:
        params: list[object] = [service_type, date]
        query = """
            SELECT * FROM mvp_slot_holds
            WHERE status = 'active'
              AND service_type = ?
              AND date = ?
        """
        if ignore_chat_id:
            query += " AND chat_id != ?"
            params.append(ignore_chat_id)
        query += " ORDER BY time ASC"
        rows = conn.execute(query, params).fetchall()
        result.extend(dict(row) for row in rows)

        # Only temporary unpaid bookings may act as local reservations.
        # Real booked/rescheduled records are checked through YClients, not through
        # old PostgreSQL rows: historical rows with status='rescheduled' stay in
        # mvp_bookings and must not block future availability forever.
        booking_rows = conn.execute(
            f"""
            SELECT id, chat_id, draft_json, status, payment_id, yclients_record_id, created_at, updated_at
            FROM {T_BOOKINGS}
            WHERE status = 'waiting_payment'
            ORDER BY id DESC
            LIMIT 200
            """
        ).fetchall()

    now = datetime.utcnow()
    ttl_minutes = int(get_settings().hold_ttl_minutes or 15)
    for row in booking_rows:
        item = dict(row)
        if ignore_chat_id and str(item.get('chat_id')) == str(ignore_chat_id):
            continue
        # A waiting_payment row that already has a YClients record is not a local
        # hold. It is either an inconsistent/manual state or will be covered by
        # YClients records. Do not let it block PostgreSQL forever.
        if item.get('yclients_record_id'):
            continue
        created_or_updated = item.get('updated_at') or item.get('created_at')
        try:
            stamp = datetime.fromisoformat(str(created_or_updated))
        except Exception:
            stamp = None
        if stamp is None or (now - stamp).total_seconds() > ttl_minutes * 60:
            continue
        try:
            draft = json.loads(item.get('draft_json') or '{}')
        except Exception:
            continue
        if draft.get('service_type') != service_type or draft.get('date') != date:
            continue
        time_value = draft.get('time')
        duration_value = draft.get('duration')
        if not time_value or not duration_value:
            continue
        result.append({
            'id': f"booking:{item.get('id')}",
            'chat_id': item.get('chat_id'),
            'service_type': service_type,
            'service_variant': draft.get('service_variant'),
            'date': date,
            'time': time_value,
            'duration': duration_value,
            'status': item.get('status'),
        })

    return result

def active_hold_exists(draft: dict, *, ignore_chat_id: str | None = None) -> bool:
    """Return True if another active reservation hold conflicts.

    The old check required the exact same duration. That misses races like:
    client A holds bathhouse 14:00-22:00 and client B asks 14:00-17:00.
    For the same object/date we compare time intervals and block overlaps.
    """
    expire_holds()
    service_type = draft.get("service_type")
    date = draft.get("date")
    time_value = draft.get("time")
    duration = _hold_duration(draft.get("duration"))
    service_variant = draft.get("service_variant") or ""
    if not service_type or not date or not time_value or not duration:
        return False

    # Reuse date-level reservation source so exact conflicts see both active holds
    # and unpaid prepared bookings.
    raw_rows = active_holds_for_service_date(service_type, date, ignore_chat_id=ignore_chat_id)
    rows = []
    for row in raw_rows:
        item = dict(row)
        if (item.get('service_variant') or '') == service_variant:
            rows.append(item)

    current = _hold_interval(str(time_value), float(duration))
    if not current:
        return False

    # Business rule:
    # If this exact object already has an active hold after 08:00 on this date,
    # the object is busy for the whole day, not only for the overlapping interval.
    for row in rows:
        item = dict(row)
        if _hold_starts_after_day_open(item.get("time")):
            return True

    start_a, end_a = current
    for row in rows:
        item = dict(row)
        other = _hold_interval(str(item.get("time") or ""), float(item.get("duration") or 0))
        if not other:
            continue
        start_b, end_b = other
        if start_a < end_b and start_b < end_a:
            return True
    return False



def _hold_starts_after_day_open(time_value: object) -> bool:
    """Business rule: any object hold/booking after 08:00 makes this object busy for the whole day."""
    try:
        parts = str(time_value or "")[:5].split(":")
        hour = int(parts[0])
        minute = int(parts[1]) if len(parts) > 1 else 0
    except Exception:
        return False
    return (hour * 60 + minute) >= 8 * 60


def _hold_duration(value: object) -> float | None:
    try:
        number = float(value or 0)
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None


def _hold_interval(time_value: str, duration_hours: float) -> tuple[int, int] | None:
    try:
        parts = str(time_value)[:5].split(":")
        hour = int(parts[0])
        minute = int(parts[1]) if len(parts) > 1 else 0
    except Exception:
        return None
    if not (0 <= hour <= 23 and 0 <= minute <= 59 and duration_hours > 0):
        return None
    start = hour * 60 + minute
    end = start + int(duration_hours * 60)
    return start, end


def upsert_hold(chat_id: str, draft: dict) -> None:
    expire_holds()
    now = datetime.utcnow()
    expires_at = now + timedelta(minutes=get_settings().hold_ttl_minutes)
    with connect() as conn:
        conn.execute(
            f"UPDATE {T_SLOT_HOLDS} SET status = 'released', updated_at = ? WHERE chat_id = ? AND status = 'active'",
            (now.isoformat(), chat_id),
        )
        conn.execute(
            """
            INSERT INTO mvp_slot_holds(chat_id, service_type, service_variant, date, time, duration, status, expires_at, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, 'active', ?, ?, ?)
            """,
            (
                chat_id,
                draft.get("service_type"),
                draft.get("service_variant"),
                draft.get("date"),
                draft.get("time"),
                draft.get("duration"),
                expires_at.isoformat(),
                now.isoformat(),
                now.isoformat(),
            ),
        )


def convert_hold(chat_id: str) -> None:
    now = datetime.utcnow().isoformat()
    with connect() as conn:
        conn.execute(
            f"UPDATE {T_SLOT_HOLDS} SET status = 'converted', updated_at = ? WHERE chat_id = ? AND status = 'active'",
            (now, chat_id),
        )


def release_holds(chat_id: str) -> None:
    now = datetime.utcnow().isoformat()
    with connect() as conn:
        conn.execute(
            f"UPDATE {T_SLOT_HOLDS} SET status = 'released', updated_at = ? WHERE chat_id = ? AND status = 'active'",
            (now, chat_id),
        )


def log_system(level: str, event: str, message: str = "", payload: dict | None = None) -> None:
    with connect() as conn:
        conn.execute(
            """
            INSERT INTO mvp_system_logs(level, event, message, payload_json, created_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (level, event, message, json.dumps(payload or {}, ensure_ascii=False), datetime.utcnow().isoformat()),
        )


def enqueue_admin_notification(message: str, chat_id: str | None = None) -> None:
    with connect() as conn:
        conn.execute(
            """
            INSERT INTO mvp_admin_notifications(chat_id, message, status, created_at)
            VALUES (?, ?, 'pending', ?)
            """,
            (chat_id, message, datetime.utcnow().isoformat()),
        )


def list_pending_admin_notifications(limit: int = 20) -> list[dict]:
    with connect() as conn:
        rows = conn.execute(
            f"SELECT * FROM {T_ADMIN_NOTIFICATIONS} WHERE status = 'pending' ORDER BY id ASC LIMIT ?",
            (limit,),
        ).fetchall()
    return [dict(row) for row in rows]


def mark_admin_notification_sent(notification_id: int) -> None:
    with connect() as conn:
        conn.execute(
            f"UPDATE {T_ADMIN_NOTIFICATIONS} SET status = 'sent', sent_at = ? WHERE id = ?",
            (datetime.utcnow().isoformat(), notification_id),
        )


def replace_availability_cache(rows: list[dict[str, Any]], *, refreshed_at: str | None = None) -> None:
    refreshed_at = refreshed_at or datetime.utcnow().isoformat()
    with connect() as conn:
        conn.execute(f"DELETE FROM {T_AVAILABILITY_CACHE}")
        for row in rows:
            conn.execute(
                f"""
                INSERT INTO {T_AVAILABILITY_CACHE}
                    (service_type, title, date, time, service_id, staff_id, status, refreshed_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    row.get("service_type") or "",
                    row.get("title") or "",
                    row.get("date") or "",
                    row.get("time") or None,
                    row.get("service_id") or "",
                    row.get("staff_id") or "",
                    row.get("status") or "",
                    refreshed_at,
                ),
            )


def availability_date_exists(date: str) -> bool:
    with connect() as conn:
        row = conn.execute(
            f"SELECT 1 FROM {T_AVAILABILITY_CACHE} WHERE date = ? LIMIT 1",
            (date,),
        ).fetchone()
    return row is not None


def replace_availability_cache_for_date(
    date: str,
    rows: list[dict[str, Any]],
    *,
    refreshed_at: str | None = None,
) -> None:
    refreshed_at = refreshed_at or datetime.utcnow().isoformat()
    with connect() as conn:
        conn.execute(f"DELETE FROM {T_AVAILABILITY_CACHE} WHERE date = ?", (date,))
        for row in rows:
            conn.execute(
                f"""
                INSERT INTO {T_AVAILABILITY_CACHE}
                    (service_type, title, date, time, service_id, staff_id, status, refreshed_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    row.get("service_type") or "",
                    row.get("title") or "",
                    row.get("date") or date,
                    row.get("time") or None,
                    row.get("service_id") or "",
                    row.get("staff_id") or "",
                    row.get("status") or "",
                    refreshed_at,
                ),
            )


def availability_cache_age_seconds() -> float | None:
    with connect() as conn:
        row = conn.execute(f"SELECT MAX(refreshed_at) AS refreshed_at FROM {T_AVAILABILITY_CACHE}").fetchone()
    value = row["refreshed_at"] if row else None
    if not value:
        return None
    try:
        refreshed_at = datetime.fromisoformat(str(value))
    except ValueError:
        return None
    return max(0.0, (datetime.utcnow() - refreshed_at).total_seconds())


def get_availability_times(*, staff_id: str, service_id: str, date: str) -> tuple[bool, list[str]]:
    with connect() as conn:
        rows = conn.execute(
            f"""
            SELECT time, status
            FROM {T_AVAILABILITY_CACHE}
            WHERE staff_id = ? AND service_id = ? AND date = ?
            """,
            (str(staff_id), str(service_id), str(date)),
        ).fetchall()
    if not rows:
        return False, []
    times = sorted({str(row["time"]) for row in rows if row["status"] != "empty" and row["time"]})
    return True, times


def list_availability_rows(
    *,
    service_type: str | None = None,
    date: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    limit: int | None = None,
) -> list[dict]:
    params: list[Any] = []
    where: list[str] = []
    if service_type:
        where.append("service_type = ?")
        params.append(service_type)
    if date:
        where.append("date = ?")
        params.append(date)
    else:
        if date_from:
            where.append("date >= ?")
            params.append(date_from)
        if date_to:
            where.append("date <= ?")
            params.append(date_to)
    query = f"SELECT * FROM {T_AVAILABILITY_CACHE}"
    if where:
        query += " WHERE " + " AND ".join(where)
    query += " ORDER BY date ASC, title ASC, time ASC"
    if limit:
        query += " LIMIT ?"
        params.append(limit)
    with connect() as conn:
        rows = conn.execute(query, params).fetchall()
    return [dict(row) for row in rows]



def list_bookings(chat_id: str, *, statuses: list[str] | None = None, limit: int = 10) -> list[dict]:
    params: list[Any] = [chat_id]
    query = f"SELECT * FROM {T_BOOKINGS} WHERE chat_id = ?"
    if statuses:
        placeholders = ",".join("?" for _ in statuses)
        query += f" AND status IN ({placeholders})"
        params.extend(statuses)
    query += " ORDER BY id DESC LIMIT ?"
    params.append(limit)
    with connect() as conn:
        rows = conn.execute(query, params).fetchall()
    return [dict(row) for row in rows]


def latest_active_booking(chat_id: str) -> dict | None:
    rows = list_bookings(
        chat_id,
        statuses=["waiting_payment", "booked", "paid_needs_manual_review", "paid_yclients_error", "manual_review", "rescheduled"],
        limit=1,
    )
    return rows[0] if rows else latest_booking(chat_id)


def create_watchlist(
    chat_id: str,
    *,
    service_type: str | None,
    object_title: str,
    date: str,
    time: str | None = None,
    duration: object | None = None,
    platform: str | None = None,
) -> int:
    now = datetime.utcnow().isoformat()
    query = """
        INSERT INTO mvp_availability_watchlist(chat_id, service_type, object_title, date, time, duration, platform, status, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, 'active', ?)
    """
    if _use_postgres():
        query += " RETURNING id"
    with connect() as conn:
        cur = conn.execute(query, (chat_id, service_type, object_title, date, time, duration, platform, now))
        if _use_postgres():
            return int(cur.fetchone()["id"])
        return int(cur.lastrowid)


def list_active_watchlist(limit: int = 50, *, platform: str | None = None) -> list[dict]:
    with connect() as conn:
        if platform:
            rows = conn.execute(
                f"SELECT * FROM {T_AVAILABILITY_WATCHLIST} WHERE status = 'active' AND platform = ? ORDER BY id ASC LIMIT ?",
                (platform, limit),
            ).fetchall()
        else:
            rows = conn.execute(
                f"SELECT * FROM {T_AVAILABILITY_WATCHLIST} WHERE status = 'active' ORDER BY id ASC LIMIT ?",
                (limit,),
            ).fetchall()
    return [dict(row) for row in rows]


def mark_watchlist_notified(watchlist_id: int) -> None:
    now = datetime.utcnow().isoformat()
    with connect() as conn:
        conn.execute(
            f"UPDATE {T_AVAILABILITY_WATCHLIST} SET status='notified', notified_at=? WHERE id=?",
            (now, watchlist_id),
        )


def cancel_watchlist(watchlist_id: int) -> None:
    now = datetime.utcnow().isoformat()
    with connect() as conn:
        conn.execute(
            f"UPDATE {T_AVAILABILITY_WATCHLIST} SET status='canceled', canceled_at=? WHERE id=?",
            (now, watchlist_id),
        )
