from datetime import datetime
from typing import Any

from psycopg2.extensions import connection as PgConnection
from psycopg2.extras import Json


def mark_sync_started(conn: PgConnection, sync_name: str, now: datetime) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO yclients_sync_state (sync_name, last_started_at, updated_at)
            VALUES (%s, %s, %s)
            ON CONFLICT (sync_name) DO UPDATE SET
                last_started_at = EXCLUDED.last_started_at,
                last_error = NULL,
                updated_at = EXCLUDED.updated_at
            """,
            (sync_name, now, now),
        )


def mark_sync_finished(
    conn: PgConnection,
    *,
    sync_name: str,
    now: datetime,
    success: bool,
    records_seen: int,
    records_upserted: int,
    error: str | None = None,
) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO yclients_sync_state (
                sync_name,
                last_finished_at,
                last_success_at,
                last_error,
                records_seen,
                records_upserted,
                updated_at
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (sync_name) DO UPDATE SET
                last_finished_at = EXCLUDED.last_finished_at,
                last_success_at = COALESCE(EXCLUDED.last_success_at, yclients_sync_state.last_success_at),
                last_error = EXCLUDED.last_error,
                records_seen = EXCLUDED.records_seen,
                records_upserted = EXCLUDED.records_upserted,
                updated_at = EXCLUDED.updated_at
            """,
            (
                sync_name,
                now,
                now if success else None,
                error,
                records_seen,
                records_upserted,
                now,
            ),
        )


def upsert_record(conn: PgConnection, record: dict[str, Any]) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO yclients_records (
                yclients_record_id,
                company_id,
                service_type,
                yclients_service_id,
                yclients_staff_id,
                service_title,
                staff_title,
                client_name,
                client_phone,
                status,
                attendance,
                start_at,
                end_at,
                duration_minutes,
                raw_payload,
                synced_at,
                updated_at
            )
            VALUES (
                %(yclients_record_id)s,
                %(company_id)s,
                %(service_type)s,
                %(yclients_service_id)s,
                %(yclients_staff_id)s,
                %(service_title)s,
                %(staff_title)s,
                %(client_name)s,
                %(client_phone)s,
                %(status)s,
                %(attendance)s,
                %(start_at)s,
                %(end_at)s,
                %(duration_minutes)s,
                %(raw_payload)s,
                %(synced_at)s,
                %(updated_at)s
            )
            ON CONFLICT (yclients_record_id) DO UPDATE SET
                company_id = EXCLUDED.company_id,
                service_type = EXCLUDED.service_type,
                yclients_service_id = EXCLUDED.yclients_service_id,
                yclients_staff_id = EXCLUDED.yclients_staff_id,
                service_title = EXCLUDED.service_title,
                staff_title = EXCLUDED.staff_title,
                client_name = EXCLUDED.client_name,
                client_phone = EXCLUDED.client_phone,
                status = EXCLUDED.status,
                attendance = EXCLUDED.attendance,
                start_at = EXCLUDED.start_at,
                end_at = EXCLUDED.end_at,
                duration_minutes = EXCLUDED.duration_minutes,
                raw_payload = EXCLUDED.raw_payload,
                synced_at = EXCLUDED.synced_at,
                updated_at = EXCLUDED.updated_at
            """,
            record | {"raw_payload": Json(record["raw_payload"])},
        )


def upsert_busy_interval(conn: PgConnection, interval: dict[str, Any]) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """
            DELETE FROM resource_busy_intervals
            WHERE source = %(source)s
              AND source_record_id = %(source_record_id)s
              AND (
                  yclients_service_id IS DISTINCT FROM %(yclients_service_id)s
                  OR yclients_staff_id IS DISTINCT FROM %(yclients_staff_id)s
              )
            """,
            interval,
        )
        cur.execute(
            """
            INSERT INTO resource_busy_intervals (
                source,
                source_record_id,
                service_type,
                yclients_service_id,
                yclients_staff_id,
                title,
                start_at,
                end_at,
                status,
                raw_payload,
                updated_at
            )
            VALUES (
                %(source)s,
                %(source_record_id)s,
                %(service_type)s,
                %(yclients_service_id)s,
                %(yclients_staff_id)s,
                %(title)s,
                %(start_at)s,
                %(end_at)s,
                %(status)s,
                %(raw_payload)s,
                %(updated_at)s
            )
            ON CONFLICT (source, source_record_id, yclients_service_id, yclients_staff_id)
            DO UPDATE SET
                service_type = EXCLUDED.service_type,
                title = EXCLUDED.title,
                start_at = EXCLUDED.start_at,
                end_at = EXCLUDED.end_at,
                status = EXCLUDED.status,
                raw_payload = EXCLUDED.raw_payload,
                updated_at = EXCLUDED.updated_at
            """,
            interval | {"raw_payload": Json(interval["raw_payload"])},
        )


def delete_busy_interval(
    conn: PgConnection,
    *,
    source: str,
    source_record_id: str,
) -> int:
    with conn.cursor() as cur:
        cur.execute(
            """
            DELETE FROM resource_busy_intervals
            WHERE source = %s
              AND source_record_id = %s
            """,
            (source, source_record_id),
        )
        return cur.rowcount


def delete_record_by_id(conn: PgConnection, *, record_id: str) -> int:
    with conn.cursor() as cur:
        cur.execute(
            """
            DELETE FROM resource_busy_intervals
            WHERE source = 'yclients'
              AND source_record_id = %s
            """,
            (record_id,),
        )
        deleted = cur.rowcount
        cur.execute(
            """
            DELETE FROM yclients_records
            WHERE yclients_record_id = %s
            """,
            (record_id,),
        )
        return deleted + cur.rowcount


def has_active_busy_interval_for_booking(
    conn: PgConnection,
    *,
    booking_id: int,
    yclients_record_id: str | None = None,
) -> bool:
    record_id = str(yclients_record_id or "").strip()
    params: list[Any] = [str(booking_id), record_id]
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT 1
            FROM resource_busy_intervals
            WHERE status = 'active'
              AND (
                  (source = 'bot_booking' AND source_record_id = %s)
                  OR (source = 'yclients' AND source_record_id = %s)
                  OR raw_payload ->> 'yclients_record_id' = %s
              )
            LIMIT 1
            """,
            params + [record_id],
        )
        return cur.fetchone() is not None


def list_busy_intervals(
    conn: PgConnection,
    *,
    service_type: str,
    staff_id: str,
    start_at: datetime,
    end_at: datetime,
) -> list[dict[str, Any]]:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT *
            FROM resource_busy_intervals
            WHERE service_type = %s
              AND yclients_staff_id = %s
              AND status = 'active'
              AND start_at < %s
              AND end_at > %s
            ORDER BY start_at ASC
            """,
            (service_type, staff_id, end_at, start_at),
        )
        return list(cur.fetchall())


def list_busy_intervals_for_service(
    conn: PgConnection,
    *,
    service_type: str,
    start_at: datetime,
    end_at: datetime,
) -> list[dict[str, Any]]:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT *
            FROM resource_busy_intervals
            WHERE service_type = %s
              AND status = 'active'
              AND start_at < %s
              AND end_at > %s
            ORDER BY start_at ASC
            """,
            (service_type, end_at, start_at),
        )
        return list(cur.fetchall())


def list_busy_intervals_starting_on_service_date(
    conn: PgConnection,
    *,
    service_type: str,
    start_at: datetime,
    end_at: datetime,
) -> list[dict[str, Any]]:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT *
            FROM resource_busy_intervals
            WHERE service_type = %s
              AND status = 'active'
              AND start_at >= %s
              AND start_at < %s
            ORDER BY start_at ASC
            """,
            (service_type, start_at, end_at),
        )
        return list(cur.fetchall())


def list_busy_intervals_crossing_service_time(
    conn: PgConnection,
    *,
    service_type: str,
    moment: datetime,
) -> list[dict[str, Any]]:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT *
            FROM resource_busy_intervals
            WHERE service_type = %s
              AND status = 'active'
              AND start_at < %s
              AND end_at > %s
            ORDER BY start_at ASC
            """,
            (service_type, moment, moment),
        )
        return list(cur.fetchall())


def delete_records_ended_before(conn: PgConnection, cutoff: datetime) -> int:
    with conn.cursor() as cur:
        cur.execute(
            """
            DELETE FROM resource_busy_intervals
            WHERE end_at < %s
            """,
            (cutoff,),
        )
        deleted_intervals = cur.rowcount
        cur.execute(
            """
            DELETE FROM yclients_records
            WHERE end_at < %s
            """,
            (cutoff,),
        )
        return deleted_intervals + cur.rowcount


def delete_records_missing_from_sync_window(
    conn: PgConnection,
    *,
    start_at: datetime,
    end_at: datetime,
    seen_record_ids: set[str],
) -> int:
    with conn.cursor() as cur:
        params: list[Any] = [start_at, end_at]
        missing_filter = ""
        if seen_record_ids:
            missing_filter = "AND yclients_record_id <> ALL(%s)"
            params.append(list(seen_record_ids))
        cur.execute(
            f"""
            SELECT yclients_record_id
            FROM yclients_records
            WHERE start_at >= %s
              AND start_at < %s
              {missing_filter}
            """,
            params,
        )
        record_ids = [row["yclients_record_id"] for row in cur.fetchall()]
        if not record_ids:
            return 0
        cur.execute(
            """
            DELETE FROM resource_busy_intervals
            WHERE source = 'yclients'
              AND source_record_id = ANY(%s)
            """,
            (record_ids,),
        )
        deleted_intervals = cur.rowcount
        cur.execute(
            """
            DELETE FROM yclients_records
            WHERE yclients_record_id = ANY(%s)
            """,
            (record_ids,),
        )
        return deleted_intervals + cur.rowcount
