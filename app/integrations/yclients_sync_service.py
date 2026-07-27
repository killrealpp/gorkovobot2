import logging
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from psycopg2.extensions import connection as PgConnection

from app.core.config import get_settings
from app.db.connection import get_connection
from app.storage import yclients_records_repo
from app.integrations.yclients import YClientsClient, YClientsError
from app.data.services import load_services as load_services_map

logger = logging.getLogger(__name__)

SYNC_NAME = "yclients_records"
CANCELLED_STATUSES = {"cancelled", "canceled", "deleted", "removed", "not_come"}


@dataclass
class SyncResult:
    records_seen: int
    records_upserted: int


@dataclass
class SyncWindow:
    now: datetime
    tz: ZoneInfo
    start_day: date
    end_day: date
    window_start: datetime
    window_end: datetime


@dataclass
class FetchedSyncRecords:
    window: SyncWindow
    raw_records: list[dict[str, Any]]


def build_sync_window(
    *,
    days_back: int = 1,
    days_forward: int = 60,
    now: datetime | None = None,
) -> SyncWindow:
    settings = get_settings()
    tz = ZoneInfo(settings.app_timezone)
    resolved_now = now.astimezone(tz) if now else datetime.now(tz)
    start_day = resolved_now.date() - timedelta(days=days_back)
    end_day = resolved_now.date() + timedelta(days=days_forward)
    return SyncWindow(
        now=resolved_now,
        tz=tz,
        start_day=start_day,
        end_day=end_day,
        window_start=datetime.combine(start_day, time.min, tzinfo=tz),
        window_end=datetime.combine(end_day + timedelta(days=1), time.min, tzinfo=tz),
    )


def fetch_records(window: SyncWindow) -> FetchedSyncRecords:
    client = YClientsClient()
    raw_records = _load_records(
        client,
        start_date=window.start_day.isoformat(),
        end_date=window.end_day.isoformat(),
    )
    return FetchedSyncRecords(window=window, raw_records=raw_records)


def apply_records(conn: PgConnection, fetched: FetchedSyncRecords) -> SyncResult:
    window = fetched.window
    yclients_records_repo.mark_sync_started(conn, SYNC_NAME, window.now)
    seen = len(fetched.raw_records)
    upserted = 0
    try:
        yclients_records_repo.delete_records_ended_before(
            conn,
            window.now - timedelta(days=3),
        )
        seen_ids: set[str] = set()
        for raw in fetched.raw_records:
            normalized = normalize_record(raw, now=window.now, tz=window.tz)
            if not normalized:
                continue
            seen_ids.add(normalized["yclients_record_id"])
            yclients_records_repo.upsert_record(conn, normalized)
            yclients_records_repo.upsert_busy_interval(
                conn,
                _busy_interval_from_record(normalized, window.now),
            )
            upserted += 1
        if seen_ids:
            yclients_records_repo.delete_records_missing_from_sync_window(
                conn,
                start_at=window.window_start,
                end_at=window.window_end,
                seen_record_ids=seen_ids,
            )
        yclients_records_repo.mark_sync_finished(
            conn,
            sync_name=SYNC_NAME,
            now=window.now,
            success=True,
            records_seen=seen,
            records_upserted=upserted,
        )
    except Exception as exc:
        _mark_sync_failed_safely(
            sync_name=SYNC_NAME,
            now=window.now,
            records_seen=seen,
            records_upserted=upserted,
            error=str(exc),
        )
        raise
    return SyncResult(records_seen=seen, records_upserted=upserted)


def sync_records_once(
    *,
    days_back: int = 1,
    days_forward: int = 60,
    now: datetime | None = None,
) -> SyncResult:
    window = build_sync_window(days_back=days_back, days_forward=days_forward, now=now)
    try:
        fetched = fetch_records(window)
    except Exception as exc:
        _mark_sync_failed_safely(
            sync_name=SYNC_NAME,
            now=window.now,
            records_seen=0,
            records_upserted=0,
            error=str(exc),
        )
        raise
    with get_connection() as conn:
        return apply_records(conn, fetched)


def sync_records(
    conn: PgConnection,
    *,
    days_back: int = 1,
    days_forward: int = 60,
    now: datetime | None = None,
) -> SyncResult:
    window = build_sync_window(days_back=days_back, days_forward=days_forward, now=now)
    fetched = fetch_records(window)
    return apply_records(conn, fetched)


def _mark_sync_failed_safely(
    *,
    sync_name: str,
    now: datetime,
    records_seen: int,
    records_upserted: int,
    error: str,
) -> None:
    try:
        with get_connection() as error_conn:
            yclients_records_repo.mark_sync_finished(
                error_conn,
                sync_name=sync_name,
                now=now,
                success=False,
                records_seen=records_seen,
                records_upserted=records_upserted,
                error=error,
            )
    except Exception:
        logger.exception("Failed to persist YCLIENTS sync error state")


def normalize_record(
    raw: dict[str, Any],
    *,
    now: datetime,
    tz: ZoneInfo,
) -> dict[str, Any] | None:
    record_id = _string(raw.get("id") or raw.get("record_id"))
    if not record_id:
        return None

    service = _first_dict(raw.get("services")) or _first_dict(raw.get("service")) or {}
    staff = _first_dict(raw.get("staff")) or _first_dict(raw.get("staffs")) or {}
    client = _first_dict(raw.get("client")) or {}

    service_id = _string(
        service.get("id")
        or raw.get("service_id")
        or raw.get("services_id")
        or raw.get("service_ids")
    )
    staff_id = _string(staff.get("id") or raw.get("staff_id") or raw.get("master_id"))
    start_at = _parse_datetime(
        raw.get("datetime")
        or raw.get("date")
        or raw.get("start_at")
        or raw.get("seance_time"),
        tz,
    )
    duration_minutes = _duration_minutes(
        raw.get("length")
        or raw.get("duration")
        or raw.get("duration_minutes")
        or service.get("duration")
    )
    if not start_at:
        return None
    if duration_minutes is None:
        duration_minutes = 60
    end_at = _parse_datetime(raw.get("end_at") or raw.get("date_end"), tz)
    if not end_at:
        end_at = start_at + timedelta(minutes=duration_minutes)

    service_type = _service_type_for_ids(service_id, staff_id)
    status = _status(raw)
    return {
        "yclients_record_id": record_id,
        "company_id": _string(raw.get("company_id")),
        "service_type": service_type,
        "yclients_service_id": service_id,
        "yclients_staff_id": staff_id,
        "service_title": _string(service.get("title") or service.get("name") or raw.get("service_title")),
        "staff_title": _string(staff.get("name") or staff.get("title") or raw.get("staff_name")),
        "client_name": _string(client.get("name") or raw.get("client_name")),
        "client_phone": _string(client.get("phone") or raw.get("phone")),
        "status": status,
        "attendance": _int_or_none(raw.get("attendance")),
        "start_at": start_at,
        "end_at": end_at,
        "duration_minutes": duration_minutes,
        "raw_payload": raw,
        "synced_at": now,
        "updated_at": now,
    }


def _load_records(
    client: YClientsClient,
    *,
    start_date: str,
    end_date: str,
) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    page = 1
    while True:
        chunk = client.get_records(start_date=start_date, end_date=end_date, page=page)
        records.extend(item for item in chunk if isinstance(item, dict))
        if len(chunk) < 200:
            break
        page += 1
        if page > 20:
            logger.warning("Stopping YCLIENTS records sync after 20 pages")
            break
    return records


def _busy_interval_from_record(record: dict[str, Any], now: datetime) -> dict[str, Any]:
    return {
        "source": "yclients",
        "source_record_id": record["yclients_record_id"],
        "service_type": record["service_type"] or "unknown",
        "yclients_service_id": record["yclients_service_id"],
        "yclients_staff_id": record["yclients_staff_id"] or "",
        "title": record["service_title"],
        "start_at": record["start_at"],
        "end_at": record["end_at"],
        "status": "cancelled" if _is_cancelled(record["status"]) else "active",
        "raw_payload": record["raw_payload"],
        "updated_at": now,
    }


def _service_type_for_ids(service_id: str, staff_id: str) -> str | None:
    for service_type, config in load_services_map().items():
        candidates = list(config.get("variants") or []) or [config]
        for item in candidates:
            if staff_id and str(item.get("yclients_staff_id") or "") == staff_id:
                return service_type
    for service_type, config in load_services_map().items():
        candidates = list(config.get("variants") or []) or [config]
        for item in candidates:
            if service_id and str(item.get("yclients_service_id") or "") == service_id:
                return service_type
    return None


def _parse_datetime(value: Any, tz: ZoneInfo) -> datetime | None:
    if not value:
        return None
    if isinstance(value, datetime):
        return value.astimezone(tz) if value.tzinfo else value.replace(tzinfo=tz)
    if isinstance(value, date):
        return datetime.combine(value, time.min, tzinfo=tz)
    text = str(value).strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        try:
            parsed_date = date.fromisoformat(text[:10])
        except ValueError:
            return None
        return datetime.combine(parsed_date, time.min, tzinfo=tz)
    return parsed.astimezone(tz) if parsed.tzinfo else parsed.replace(tzinfo=tz)


def _duration_minutes(value: Any) -> int | None:
    if value in (None, ""):
        return None
    try:
        number = int(float(str(value)))
    except ValueError:
        return None
    return number // 60 if number > 24 * 60 else number


def _status(raw: dict[str, Any]) -> str:
    value = raw.get("status") or raw.get("record_status") or raw.get("deleted")
    if value is True:
        return "cancelled"
    if value is False or value is None:
        return "active"
    return str(value).lower()


def _is_cancelled(status: str | None) -> bool:
    if not status:
        return False
    return status.lower() in CANCELLED_STATUSES


def _first_dict(value: Any) -> dict[str, Any] | None:
    if isinstance(value, dict):
        return value
    if isinstance(value, list):
        for item in value:
            if isinstance(item, dict):
                return item
    return None


def _string(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, list):
        return _string(value[0]) if value else ""
    return str(value)


def _int_or_none(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None
