from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Iterable

from app.core.config import PROJECT_ROOT, get_settings, sqlite_path
from app.storage import sqlite as storage


@dataclass(frozen=True)
class RetentionPolicy:
    messages_days: int = 90
    system_logs_days: int = 30
    holds_days: int = 1
    admin_notifications_days: int = 30
    availability_cache_days: int = 7
    watchlist_days: int = 30
    voice_temp_days: int = 1
    booking_review_days: int = 180


@dataclass(frozen=True)
class CleanupOperation:
    key: str
    target: str
    action: str
    key_columns: tuple[str, ...]
    where: str
    params: tuple[Any, ...]


RUNTIME_TABLES: tuple[str, ...] = (
    storage.T_CONVERSATIONS,
    storage.T_MESSAGES,
    storage.T_BOOKINGS,
    storage.T_SLOT_HOLDS,
    storage.T_SYSTEM_LOGS,
    storage.T_ADMIN_NOTIFICATIONS,
    storage.T_AVAILABILITY_CACHE,
    storage.T_AVAILABILITY_WATCHLIST,
)

RECOMMENDED_TABLES: tuple[str, ...] = (
    "mvp_processed_updates",
)

EXPECTED_COLUMNS: dict[str, set[str]] = {
    storage.T_CONVERSATIONS: {"chat_id", "draft_json", "status", "current_step", "updated_at"},
    storage.T_MESSAGES: {"id", "chat_id", "sender", "text", "raw_json", "created_at"},
    storage.T_BOOKINGS: {
        "id",
        "chat_id",
        "platform",
        "draft_json",
        "status",
        "payment_id",
        "yclients_record_id",
        "created_at",
        "updated_at",
    },
    storage.T_SLOT_HOLDS: {
        "id",
        "chat_id",
        "service_type",
        "service_variant",
        "date",
        "time",
        "duration",
        "status",
        "expires_at",
        "created_at",
        "updated_at",
    },
    storage.T_SYSTEM_LOGS: {"id", "level", "event", "message", "payload_json", "created_at"},
    storage.T_ADMIN_NOTIFICATIONS: {"id", "chat_id", "message", "status", "created_at", "sent_at"},
    storage.T_AVAILABILITY_CACHE: {
        "id",
        "service_type",
        "title",
        "date",
        "time",
        "service_id",
        "staff_id",
        "status",
        "refreshed_at",
    },
    storage.T_AVAILABILITY_WATCHLIST: {
        "id",
        "chat_id",
        "service_type",
        "object_title",
        "date",
        "time",
        "duration",
        "platform",
        "status",
        "created_at",
        "notified_at",
        "canceled_at",
    },
}

PROTECTED_BOOKING_STATUSES: tuple[str, ...] = (
    "waiting_payment",
    "booked",
    "rescheduled",
    "paid_needs_manual_review",
    "paid_yclients_error",
    "manual_review",
    "reschedule_manual_review",
)

CLEANUP_PLAN_RULES: dict[str, dict[str, str]] = {
    "old_messages": {
        "target": storage.T_MESSAGES,
        "category": "future_delete_candidate",
        "where": "created_at < messages cutoff",
        "safety": "Removes old conversation history only after explicit cleanup approval.",
    },
    "old_inactive_conversations": {
        "target": storage.T_CONVERSATIONS,
        "category": "future_delete_candidate",
        "where": "updated_at < messages cutoff AND status is not active/waiting_payment",
        "safety": "Keeps active and payment-related drafts protected.",
    },
    "old_system_logs": {
        "target": storage.T_SYSTEM_LOGS,
        "category": "future_delete_candidate",
        "where": "created_at < logs cutoff",
        "safety": "Removes old technical logs after operational review.",
    },
    "old_inactive_slot_holds": {
        "target": storage.T_SLOT_HOLDS,
        "category": "future_delete_candidate",
        "where": "status IN expired/released/converted AND hold timestamp < holds cutoff",
        "safety": "Keeps active holds protected.",
    },
    "active_holds_past_expiry": {
        "target": storage.T_SLOT_HOLDS,
        "category": "repair_candidate",
        "where": "status = active AND expires_at < now",
        "safety": "Future apply should repair to expired, not delete directly.",
    },
    "old_sent_admin_notifications": {
        "target": storage.T_ADMIN_NOTIFICATIONS,
        "category": "future_delete_candidate",
        "where": "status = sent AND sent/created timestamp < notifications cutoff",
        "safety": "Keeps pending admin notifications protected.",
    },
    "stale_availability_cache": {
        "target": storage.T_AVAILABILITY_CACHE,
        "category": "future_delete_candidate",
        "where": "refreshed_at < availability cutoff",
        "safety": "Availability cache can be regenerated; future apply must deduplicate with past-date cleanup.",
    },
    "past_availability_cache_dates": {
        "target": storage.T_AVAILABILITY_CACHE,
        "category": "future_delete_candidate",
        "where": "date < today",
        "safety": "Availability cache can be regenerated; future apply must deduplicate with stale-cache cleanup.",
    },
    "old_inactive_watchlist": {
        "target": storage.T_AVAILABILITY_WATCHLIST,
        "category": "future_delete_candidate",
        "where": "status IN notified/canceled AND watchlist timestamp < watchlist cutoff",
        "safety": "Keeps active watchlist rows protected.",
    },
    "active_watchlist_past_dates": {
        "target": storage.T_AVAILABILITY_WATCHLIST,
        "category": "manual_review_candidate",
        "where": "status = active AND date < today",
        "safety": "Active client notification intent; review before cancellation or deletion.",
    },
    "old_unprotected_bookings_for_review": {
        "target": storage.T_BOOKINGS,
        "category": "manual_review_candidate",
        "where": "updated_at < booking review cutoff AND no protected status/payment/YCLIENTS id",
        "safety": "Bookings are review-only; do not delete automatically.",
    },
}

_APPLY_SUPPORTED_KEYS: set[str] = {
    "old_messages",
    "old_inactive_conversations",
    "old_system_logs",
    "old_inactive_slot_holds",
    "active_holds_past_expiry",
    "old_sent_admin_notifications",
    "stale_availability_cache",
    "past_availability_cache_dates",
    "availability_cache_cleanup",
    "old_inactive_watchlist",
    "old_voice_temp_files",
}


def build_cleanup_plan(
    policy: RetentionPolicy | None = None,
    *,
    limit: int = 20,
    include_files: bool = True,
    only_keys: Iterable[str] | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Build a non-mutating cleanup plan from the read-only report."""

    policy = policy or RetentionPolicy()
    report = build_retention_report(policy, limit=limit, include_files=include_files, now=now)
    only = _normalize_only_keys(only_keys)
    items: list[dict[str, Any]] = []

    for key, candidate in sorted((report.get("cleanup_candidates") or {}).items()):
        rule = CLEANUP_PLAN_RULES.get(key, {})
        target = rule.get("target", "unknown")
        if not _matches_only(key, target, only):
            continue
        items.append(
            {
                "key": key,
                "target": target,
                "category": rule.get("category", "manual_review_candidate"),
                "count": int(candidate.get("count") or 0),
                "sample_ids": list(candidate.get("sample_ids") or []),
                "description": candidate.get("description") or "",
                "where": rule.get("where", ""),
                "safety": rule.get("safety", "Review before adding apply mode."),
                "apply_supported": key in _APPLY_SUPPORTED_KEYS,
            }
        )

    voice = report.get("voice_temp_files") or {}
    if include_files and voice.get("exists") and _matches_only("old_voice_temp_files", str(voice.get("path") or ""), only):
        items.append(
            {
                "key": "old_voice_temp_files",
                "target": voice.get("path"),
                "category": "future_file_delete_candidate",
                "count": int(voice.get("old_files") or 0),
                "bytes": int(voice.get("old_bytes") or 0),
                "sample_ids": list(voice.get("sample_names") or []),
                "description": f"Voice temp files older than {policy.voice_temp_days} day(s).",
                "where": "file modified time < voice temp cutoff",
                "safety": "Future apply must stay path-scoped to VOICE_TEMP_DIR.",
                "apply_supported": True,
            }
        )

    summary = {
        "future_delete_candidate_signals": sum(
            int(item.get("count") or 0)
            for item in items
            if item.get("category") in {"future_delete_candidate", "future_file_delete_candidate"}
        ),
        "manual_review_signals": sum(
            int(item.get("count") or 0)
            for item in items
            if item.get("category") == "manual_review_candidate"
        ),
        "repair_candidate_signals": sum(
            int(item.get("count") or 0)
            for item in items
            if item.get("category") == "repair_candidate"
        ),
        "items_with_matches": sum(1 for item in items if int(item.get("count") or 0) > 0),
    }

    return {
        "generated_at_utc": report.get("generated_at_utc"),
        "mode": "dry_run",
        "apply_supported": True,
        "policy": report.get("policy", asdict(policy)),
        "database": report.get("database", {}),
        "only_keys": sorted(only),
        "summary": summary,
        "items": items,
        "protected_data": report.get("protected_data", {}),
        "warnings": list(report.get("schema_warnings") or []) + list(report.get("operational_warnings") or []),
        "notes": [
            "This cleanup plan performs no deletes or updates.",
            "Dry-run is the default. Real cleanup requires explicit apply confirmation.",
            "Counts are planning signals; availability-cache sections may overlap until guarded apply deduplicates selected ids.",
            "Bookings, active holds, active watchlist rows, pending admin notifications, payment ids, and YCLIENTS ids remain protected.",
        ],
    }


def render_cleanup_plan(plan: dict[str, Any]) -> str:
    database = plan.get("database") or {}
    summary = plan.get("summary") or {}
    lines: list[str] = []
    lines.append("MaxBot 4 retention cleanup dry-run")
    lines.append("=" * 37)
    lines.append(f"Generated UTC: {plan.get('generated_at_utc')}")
    lines.append(f"Mode: {plan.get('mode')}")
    lines.append(f"Apply supported: {plan.get('apply_supported')}")
    lines.append(f"Database: {database.get('backend')} ({database.get('target')})")
    lines.append("")

    lines.append("Summary")
    lines.append("-" * 7)
    lines.append(f"- future delete candidate signals: {summary.get('future_delete_candidate_signals', 0)}")
    lines.append(f"- manual review signals: {summary.get('manual_review_signals', 0)}")
    lines.append(f"- repair candidate signals: {summary.get('repair_candidate_signals', 0)}")
    lines.append(f"- matching sections: {summary.get('items_with_matches', 0)}")
    lines.append("")

    lines.append("Plan items")
    lines.append("-" * 10)
    items = list(plan.get("items") or [])
    if not items:
        lines.append("No cleanup plan items were available.")
    for item in items:
        lines.append(f"- {item.get('key')}: {item.get('count', 0)}")
        lines.append(f"  target: {item.get('target')}")
        lines.append(f"  category: {item.get('category')}")
        lines.append(f"  apply_supported: {item.get('apply_supported')}")
        if item.get("bytes"):
            lines.append(f"  bytes: {item.get('bytes')}")
        if item.get("where"):
            lines.append(f"  where: {item.get('where')}")
        if item.get("description"):
            lines.append(f"  description: {item.get('description')}")
        if item.get("safety"):
            lines.append(f"  safety: {item.get('safety')}")
        samples = item.get("sample_ids") or []
        if samples:
            lines.append(f"  sample ids/names: {', '.join(str(value) for value in samples)}")
    lines.append("")

    lines.append("Protected data")
    lines.append("-" * 14)
    protected = plan.get("protected_data") or {}
    if not protected:
        lines.append("No protected data checks were available.")
    for key in sorted(protected):
        item = protected[key]
        lines.append(f"- {key}: {item.get('count', 0)}")
        if item.get("description"):
            lines.append(f"  {item['description']}")
    lines.append("")

    lines.append("Warnings")
    lines.append("-" * 8)
    warnings = plan.get("warnings") or []
    if warnings:
        lines.extend(f"- {warning}" for warning in warnings)
    else:
        lines.append("No warnings.")
    lines.append("")

    lines.append("Notes")
    lines.append("-" * 5)
    for note in plan.get("notes") or []:
        lines.append(f"- {note}")

    return "\n".join(lines)


def apply_cleanup(
    policy: RetentionPolicy | None = None,
    *,
    max_rows: int = 1000,
    include_files: bool = True,
    only_keys: Iterable[str] | None = None,
    now: datetime | None = None,
    confirmed: bool = False,
) -> dict[str, Any]:
    """Apply guarded cleanup operations.

    Mutating work is refused unless ``confirmed`` is true. Database candidates
    are re-selected inside the apply transaction and changed by stable keys.
    """

    policy = policy or RetentionPolicy()
    max_rows = max(0, int(max_rows))
    now = (now or _utc_now()).replace(microsecond=0)
    backend = "postgres" if storage._use_postgres() else "sqlite"
    only = _normalize_only_keys(only_keys)
    database = _database_info(backend)

    if not confirmed:
        plan = build_cleanup_plan(policy, limit=max_rows, include_files=include_files, only_keys=only, now=now)
        return {
            "generated_at_utc": _iso(now),
            "mode": "apply_refused",
            "applied": False,
            "confirmed": False,
            "max_rows": max_rows,
            "only_keys": sorted(only),
            "database": plan.get("database", database),
            "summary": {
                "deleted_rows": 0,
                "updated_rows": 0,
                "deleted_files": 0,
                "deleted_file_bytes": 0,
                "operations_with_matches": 0,
            },
            "items": [],
            "protected_data": plan.get("protected_data", {}),
            "warnings": list(plan.get("warnings") or []),
            "notes": [
                "Apply refused: pass both --apply and --yes to perform real cleanup.",
                "No database rows or files were changed.",
            ],
        }

    result: dict[str, Any] = {
        "generated_at_utc": _iso(now),
        "mode": "apply",
        "applied": True,
        "confirmed": True,
        "max_rows": max_rows,
        "only_keys": sorted(only),
        "database": database,
        "summary": {
            "deleted_rows": 0,
            "updated_rows": 0,
            "deleted_files": 0,
            "deleted_file_bytes": 0,
            "operations_with_matches": 0,
        },
        "items": [],
        "protected_data": {},
        "warnings": [],
        "notes": [
            "Database candidates were re-selected inside the apply transaction.",
            "Bookings are report-only and are not automatically deleted.",
        ],
    }

    if backend == "sqlite" and not sqlite_path().exists():
        result["mode"] = "apply_skipped"
        result["applied"] = False
        result["warnings"].append("SQLite database file does not exist; no database cleanup was applied.")
    elif max_rows <= 0:
        result["mode"] = "apply_skipped"
        result["applied"] = False
        result["warnings"].append("max_rows is 0; no database cleanup was applied.")
    else:
        with storage.connect() as conn:
            operations = _cleanup_operations(conn, policy=policy, now=now, only=only, backend=backend, warnings=result["warnings"])
            for operation in operations:
                keys = _select_operation_keys(conn, operation, max_rows=max_rows)
                item = {
                    "key": operation.key,
                    "target": operation.target,
                    "action": operation.action,
                    "selected": len(keys),
                    "deleted": 0,
                    "updated": 0,
                }
                if keys:
                    result["summary"]["operations_with_matches"] += 1
                    if operation.action == "delete":
                        item["deleted"] = _delete_operation_keys(conn, operation, keys)
                        result["summary"]["deleted_rows"] += item["deleted"]
                    elif operation.action == "repair_expired_holds":
                        item["updated"] = _repair_expired_holds_by_keys(conn, operation, keys, now=now)
                        result["summary"]["updated_rows"] += item["updated"]
                result["items"].append(item)
            _add_protected_data(conn, result, backend=backend)

    if include_files and _matches_only("old_voice_temp_files", "voice_temp_files", only):
        file_result = _delete_old_voice_temp_files(policy, now=now, max_files=max_rows)
        result["items"].append(file_result)
        result["summary"]["deleted_files"] += int(file_result.get("deleted_files") or 0)
        result["summary"]["deleted_file_bytes"] += int(file_result.get("deleted_bytes") or 0)
        result["warnings"].extend(file_result.get("warnings") or [])
        if int(file_result.get("deleted_files") or 0) > 0:
            result["mode"] = "apply"
            result["applied"] = True

    return result


def render_apply_result(result: dict[str, Any]) -> str:
    database = result.get("database") or {}
    summary = result.get("summary") or {}
    lines: list[str] = []
    lines.append("MaxBot 4 retention cleanup apply result")
    lines.append("=" * 40)
    lines.append(f"Generated UTC: {result.get('generated_at_utc')}")
    lines.append(f"Mode: {result.get('mode')}")
    lines.append(f"Applied: {result.get('applied')}")
    lines.append(f"Confirmed: {result.get('confirmed')}")
    lines.append(f"Database: {database.get('backend')} ({database.get('target')})")
    lines.append("")

    lines.append("Summary")
    lines.append("-" * 7)
    lines.append(f"- deleted rows: {summary.get('deleted_rows', 0)}")
    lines.append(f"- updated rows: {summary.get('updated_rows', 0)}")
    lines.append(f"- deleted files: {summary.get('deleted_files', 0)}")
    lines.append(f"- deleted file bytes: {summary.get('deleted_file_bytes', 0)}")
    lines.append(f"- operations with matches: {summary.get('operations_with_matches', 0)}")
    lines.append("")

    lines.append("Items")
    lines.append("-" * 5)
    items = result.get("items") or []
    if not items:
        lines.append("No apply items were changed.")
    for item in items:
        lines.append(f"- {item.get('key')}: selected={item.get('selected', 0)} deleted={item.get('deleted', 0)} updated={item.get('updated', 0)}")
        if item.get("deleted_files"):
            lines.append(f"  deleted_files: {item.get('deleted_files')}")
        if item.get("warnings"):
            for warning in item.get("warnings") or []:
                lines.append(f"  warning: {warning}")
    lines.append("")

    lines.append("Warnings")
    lines.append("-" * 8)
    warnings = result.get("warnings") or []
    if warnings:
        lines.extend(f"- {warning}" for warning in warnings)
    else:
        lines.append("No warnings.")
    lines.append("")

    lines.append("Notes")
    lines.append("-" * 5)
    for note in result.get("notes") or []:
        lines.append(f"- {note}")

    return "\n".join(lines)


def build_retention_report(
    policy: RetentionPolicy | None = None,
    *,
    limit: int = 20,
    include_files: bool = True,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Build a read-only data hygiene report.

    This function never calls init_db(). For SQLite, a missing database file is
    reported before opening a connection so the report cannot create it.
    """

    policy = policy or RetentionPolicy()
    limit = max(0, int(limit))
    now = (now or _utc_now()).replace(microsecond=0)
    backend = "postgres" if storage._use_postgres() else "sqlite"

    report: dict[str, Any] = {
        "generated_at_utc": _iso(now),
        "mode": "read_only",
        "policy": asdict(policy),
        "database": _database_info(backend),
        "tables": {},
        "cleanup_candidates": {},
        "protected_data": {},
        "schema_warnings": [],
        "operational_warnings": [],
        "notes": [
            "This report performs no deletes or updates.",
            "Counts are cleanup candidates, not cleanup approval.",
            "Message text, raw payloads, chat ids, phone numbers, and secrets are not printed.",
        ],
    }

    if backend == "sqlite" and not sqlite_path().exists():
        report["operational_warnings"].append("SQLite database file does not exist; no connection was opened.")
        if include_files:
            report["voice_temp_files"] = _voice_temp_report(policy, now=now, limit=limit)
        return report

    with storage.connect() as conn:
        _add_table_reports(conn, report, backend=backend)
        _add_recommended_table_warnings(conn, report, backend=backend)
        _add_cleanup_candidates(conn, report, policy=policy, now=now, limit=limit, backend=backend)
        _add_protected_data(conn, report, backend=backend)

    if include_files:
        report["voice_temp_files"] = _voice_temp_report(policy, now=now, limit=limit)

    return report


def render_retention_report(report: dict[str, Any]) -> str:
    database = report.get("database") or {}
    lines: list[str] = []
    lines.append("MaxBot 4 retention report")
    lines.append("=" * 25)
    lines.append(f"Generated UTC: {report.get('generated_at_utc')}")
    lines.append(f"Mode: {report.get('mode')}")
    lines.append(f"Database: {database.get('backend')} ({database.get('target')})")
    lines.append("")

    lines.append("Tables")
    lines.append("-" * 6)
    tables = report.get("tables") or {}
    if not tables:
        lines.append("No runtime tables were inspected.")
    for table in sorted(tables):
        item = tables[table]
        if not item.get("exists"):
            lines.append(f"- {table}: missing")
            continue
        lines.append(f"- {table}: {item.get('rows', 0)} rows")
        if item.get("status_counts"):
            status_counts = ", ".join(f"{key or '<empty>'}={value}" for key, value in item["status_counts"].items())
            lines.append(f"  statuses: {status_counts}")
        if item.get("oldest") or item.get("newest"):
            lines.append(f"  range: {item.get('oldest')} -> {item.get('newest')}")
    lines.append("")

    lines.append("Cleanup candidates")
    lines.append("-" * 18)
    candidates = report.get("cleanup_candidates") or {}
    if not candidates:
        lines.append("No cleanup candidate checks were available.")
    for key in sorted(candidates):
        item = candidates[key]
        lines.append(f"- {key}: {item.get('count', 0)}")
        if item.get("description"):
            lines.append(f"  {item['description']}")
        samples = item.get("sample_ids") or []
        if samples:
            lines.append(f"  sample ids: {', '.join(str(value) for value in samples)}")
    lines.append("")

    lines.append("Protected data")
    lines.append("-" * 14)
    protected = report.get("protected_data") or {}
    if not protected:
        lines.append("No protected data checks were available.")
    for key in sorted(protected):
        item = protected[key]
        lines.append(f"- {key}: {item.get('count', 0)}")
        if item.get("description"):
            lines.append(f"  {item['description']}")
    lines.append("")

    if "voice_temp_files" in report:
        voice = report["voice_temp_files"]
        lines.append("Voice temp files")
        lines.append("-" * 16)
        lines.append(f"- path: {voice.get('path')}")
        lines.append(f"- exists: {voice.get('exists')}")
        lines.append(f"- total files: {voice.get('total_files', 0)}")
        lines.append(f"- old files: {voice.get('old_files', 0)}")
        lines.append(f"- old bytes: {voice.get('old_bytes', 0)}")
        if voice.get("sample_names"):
            lines.append(f"- sample names: {', '.join(str(value) for value in voice['sample_names'])}")
        lines.append("")

    lines.append("Warnings")
    lines.append("-" * 8)
    warnings = list(report.get("schema_warnings") or []) + list(report.get("operational_warnings") or [])
    if warnings:
        lines.extend(f"- {warning}" for warning in warnings)
    else:
        lines.append("No warnings.")
    lines.append("")

    lines.append("Notes")
    lines.append("-" * 5)
    for note in report.get("notes") or []:
        lines.append(f"- {note}")

    return "\n".join(lines)


def _database_info(backend: str) -> dict[str, Any]:
    if backend == "postgres":
        settings = get_settings()
        return {
            "backend": "postgres",
            "target": f"{settings.db_host}:{settings.db_port}/{settings.db_name}",
        }
    return {
        "backend": "sqlite",
        "target": str(sqlite_path()),
        "exists": sqlite_path().exists(),
    }


def _add_table_reports(conn: Any, report: dict[str, Any], *, backend: str) -> None:
    for table in RUNTIME_TABLES:
        if not _table_exists(conn, table, backend=backend):
            report["tables"][table] = {"exists": False}
            report["schema_warnings"].append(f"Runtime table {table} is missing.")
            continue

        columns = _table_columns(conn, table, backend=backend)
        expected = EXPECTED_COLUMNS.get(table, set())
        missing = sorted(expected - columns)
        if missing:
            report["schema_warnings"].append(f"Table {table} is missing expected columns: {', '.join(missing)}.")

        item: dict[str, Any] = {
            "exists": True,
            "rows": _count(conn, table),
            "columns": sorted(columns),
            "missing_expected_columns": missing,
        }
        if "status" in columns:
            item["status_counts"] = _status_counts(conn, table)
        range_column = _range_column(columns)
        if range_column:
            item.update(_min_max(conn, table, range_column))
            item["range_column"] = range_column
        report["tables"][table] = item


def _add_recommended_table_warnings(conn: Any, report: dict[str, Any], *, backend: str) -> None:
    for table in RECOMMENDED_TABLES:
        if not _table_exists(conn, table, backend=backend):
            report["schema_warnings"].append(f"Recommended table {table} is absent.")


def _add_cleanup_candidates(
    conn: Any,
    report: dict[str, Any],
    *,
    policy: RetentionPolicy,
    now: datetime,
    limit: int,
    backend: str,
) -> None:
    messages_cutoff = _iso(now - timedelta(days=policy.messages_days))
    _add_candidate(
        conn,
        report,
        key="old_messages",
        table=storage.T_MESSAGES,
        where="created_at < ?",
        params=(messages_cutoff,),
        limit=limit,
        description=f"Messages older than {policy.messages_days} day(s).",
        required_columns=("id", "created_at"),
        backend=backend,
    )
    _add_candidate(
        conn,
        report,
        key="old_inactive_conversations",
        table=storage.T_CONVERSATIONS,
        where="updated_at < ? AND COALESCE(status, '') NOT IN ('active', 'waiting_payment')",
        params=(messages_cutoff,),
        limit=limit,
        description=f"Inactive conversations older than {policy.messages_days} day(s). Chat ids are not sampled.",
        required_columns=("updated_at", "status"),
        sample_column=None,
        backend=backend,
    )
    _add_candidate(
        conn,
        report,
        key="old_system_logs",
        table=storage.T_SYSTEM_LOGS,
        where="created_at < ?",
        params=(_iso(now - timedelta(days=policy.system_logs_days)),),
        limit=limit,
        description=f"System logs older than {policy.system_logs_days} day(s).",
        required_columns=("id", "created_at"),
        backend=backend,
    )
    _add_candidate(
        conn,
        report,
        key="old_inactive_slot_holds",
        table=storage.T_SLOT_HOLDS,
        where="status IN ('expired', 'released', 'converted') AND COALESCE(updated_at, expires_at, created_at) < ?",
        params=(_iso(now - timedelta(days=policy.holds_days)),),
        limit=limit,
        description=f"Inactive slot holds older than {policy.holds_days} day(s). Active holds are protected.",
        required_columns=("id", "status", "updated_at", "expires_at", "created_at"),
        backend=backend,
    )
    _add_candidate(
        conn,
        report,
        key="active_holds_past_expiry",
        table=storage.T_SLOT_HOLDS,
        where="status = 'active' AND expires_at < ?",
        params=(_iso(now),),
        limit=limit,
        description="Active holds that already passed expires_at. Future cleanup should repair them to expired, not delete them.",
        required_columns=("id", "status", "expires_at"),
        backend=backend,
    )
    _add_candidate(
        conn,
        report,
        key="old_sent_admin_notifications",
        table=storage.T_ADMIN_NOTIFICATIONS,
        where=f"status = 'sent' AND {_coalesce_expr(('sent_at', 'created_at'), _columns_or_empty(conn, storage.T_ADMIN_NOTIFICATIONS, backend=backend))} < ?",
        params=(_iso(now - timedelta(days=policy.admin_notifications_days)),),
        limit=limit,
        description=f"Sent admin notifications older than {policy.admin_notifications_days} day(s). Pending notifications are protected.",
        required_columns=("id", "status", "created_at"),
        backend=backend,
    )
    _add_candidate(
        conn,
        report,
        key="stale_availability_cache",
        table=storage.T_AVAILABILITY_CACHE,
        where="refreshed_at < ?",
        params=(_iso(now - timedelta(days=policy.availability_cache_days)),),
        limit=limit,
        description=f"Availability cache rows refreshed more than {policy.availability_cache_days} day(s) ago.",
        required_columns=("id", "refreshed_at"),
        backend=backend,
    )
    _add_candidate(
        conn,
        report,
        key="past_availability_cache_dates",
        table=storage.T_AVAILABILITY_CACHE,
        where="date < ?",
        params=(now.date().isoformat(),),
        limit=limit,
        description="Availability cache rows for past dates. Cache is rebuildable.",
        required_columns=("id", "date"),
        backend=backend,
    )
    _add_candidate(
        conn,
        report,
        key="old_inactive_watchlist",
        table=storage.T_AVAILABILITY_WATCHLIST,
        where=f"status IN ('notified', 'canceled') AND {_coalesce_expr(('notified_at', 'canceled_at', 'created_at'), _columns_or_empty(conn, storage.T_AVAILABILITY_WATCHLIST, backend=backend))} < ?",
        params=(_iso(now - timedelta(days=policy.watchlist_days)),),
        limit=limit,
        description=f"Inactive watchlist rows older than {policy.watchlist_days} day(s). Active watchlist rows are protected.",
        required_columns=("id", "status", "created_at"),
        backend=backend,
    )
    _add_candidate(
        conn,
        report,
        key="active_watchlist_past_dates",
        table=storage.T_AVAILABILITY_WATCHLIST,
        where="status = 'active' AND date < ?",
        params=(now.date().isoformat(),),
        limit=limit,
        description="Active watchlist rows for dates already in the past. Review before cancellation or deletion.",
        required_columns=("id", "status", "date"),
        backend=backend,
    )

    protected_clause, protected_params = _protected_booking_predicate()
    _add_candidate(
        conn,
        report,
        key="old_unprotected_bookings_for_review",
        table=storage.T_BOOKINGS,
        where=f"updated_at < ? AND NOT ({protected_clause})",
        params=(_iso(now - timedelta(days=policy.booking_review_days)), *protected_params),
        limit=limit,
        description="Old bookings without protected status, payment id, or YCLIENTS id. Bookings remain manual-review only.",
        required_columns=("id", "updated_at", "status", "payment_id", "yclients_record_id"),
        backend=backend,
    )


def _add_protected_data(conn: Any, report: dict[str, Any], *, backend: str) -> None:
    protected_clause, protected_params = _protected_booking_predicate()
    _add_protected(
        conn,
        report,
        key="protected_bookings",
        table=storage.T_BOOKINGS,
        where=protected_clause,
        params=protected_params,
        description="Bookings with protected status, payment id, or YCLIENTS id. Retention must not delete them automatically.",
        required_columns=("status", "payment_id", "yclients_record_id"),
        backend=backend,
    )
    _add_protected(
        conn,
        report,
        key="active_conversations",
        table=storage.T_CONVERSATIONS,
        where="COALESCE(status, '') IN ('active', 'waiting_payment')",
        params=(),
        description="Active conversation drafts are protected.",
        required_columns=("status",),
        backend=backend,
    )
    _add_protected(
        conn,
        report,
        key="active_slot_holds",
        table=storage.T_SLOT_HOLDS,
        where="status = 'active'",
        params=(),
        description="Active slot holds may affect availability and payment flow.",
        required_columns=("status",),
        backend=backend,
    )
    _add_protected(
        conn,
        report,
        key="pending_admin_notifications",
        table=storage.T_ADMIN_NOTIFICATIONS,
        where="status = 'pending'",
        params=(),
        description="Pending admin notifications must remain queued.",
        required_columns=("status",),
        backend=backend,
    )
    _add_protected(
        conn,
        report,
        key="active_watchlist_rows",
        table=storage.T_AVAILABILITY_WATCHLIST,
        where="status = 'active'",
        params=(),
        description="Active watchlist rows may trigger client notifications.",
        required_columns=("status",),
        backend=backend,
    )


def _add_candidate(
    conn: Any,
    report: dict[str, Any],
    *,
    key: str,
    table: str,
    where: str,
    params: Iterable[Any],
    limit: int,
    description: str,
    required_columns: Iterable[str],
    backend: str,
    sample_column: str | None = "id",
) -> None:
    if not _table_ready(conn, report, table, required_columns=required_columns, backend=backend, context=key):
        return
    item = {
        "count": _count(conn, table, where, params),
        "description": description,
        "sample_ids": [],
    }
    if sample_column and limit > 0 and sample_column in _table_columns(conn, table, backend=backend):
        rows = _fetchall(
            conn,
            f"SELECT {sample_column} FROM {table} WHERE {where} ORDER BY {sample_column} ASC LIMIT ?",
            [*list(params), limit],
        )
        item["sample_ids"] = [row.get(sample_column) for row in rows]
    report["cleanup_candidates"][key] = item


def _add_protected(
    conn: Any,
    report: dict[str, Any],
    *,
    key: str,
    table: str,
    where: str,
    params: Iterable[Any],
    description: str,
    required_columns: Iterable[str],
    backend: str,
) -> None:
    if not _table_ready(conn, report, table, required_columns=required_columns, backend=backend, context=key):
        return
    report["protected_data"][key] = {
        "count": _count(conn, table, where, params),
        "description": description,
    }


def _table_ready(
    conn: Any,
    report: dict[str, Any],
    table: str,
    *,
    required_columns: Iterable[str],
    backend: str,
    context: str,
) -> bool:
    if not _table_exists(conn, table, backend=backend):
        _append_operational_warning(report, f"Skipped {context}: table {table} is missing.")
        return False
    columns = _table_columns(conn, table, backend=backend)
    missing = sorted(set(required_columns) - columns)
    if missing:
        _append_operational_warning(report, f"Skipped {context}: table {table} is missing columns: {', '.join(missing)}.")
        return False
    return True


def _voice_temp_report(policy: RetentionPolicy, *, now: datetime, limit: int) -> dict[str, Any]:
    settings = get_settings()
    path = Path(settings.voice_temp_dir)
    if not path.is_absolute():
        path = PROJECT_ROOT / path

    result: dict[str, Any] = {
        "path": str(path),
        "exists": path.exists(),
        "total_files": 0,
        "old_files": 0,
        "old_bytes": 0,
        "sample_names": [],
    }
    if not path.exists() or not path.is_dir():
        return result

    cutoff = now - timedelta(days=policy.voice_temp_days)
    samples: list[str] = []
    for item in path.rglob("*"):
        if not item.is_file():
            continue
        result["total_files"] += 1
        try:
            stat = item.stat()
        except OSError:
            continue
        modified_at = datetime.fromtimestamp(stat.st_mtime, UTC).replace(tzinfo=None)
        if modified_at >= cutoff:
            continue
        result["old_files"] += 1
        result["old_bytes"] += stat.st_size
        if len(samples) < limit:
            samples.append(item.name)
    result["sample_names"] = samples
    return result


def _cleanup_operations(
    conn: Any,
    *,
    policy: RetentionPolicy,
    now: datetime,
    only: set[str],
    backend: str,
    warnings: list[str],
) -> list[CleanupOperation]:
    operations: list[CleanupOperation] = []

    def add(operation: CleanupOperation, *, required_columns: Iterable[str]) -> None:
        if not _matches_only(operation.key, operation.target, only):
            return
        if not _table_exists(conn, operation.target, backend=backend):
            warnings.append(f"Table {operation.target} is missing; skipped {operation.key}.")
            return
        columns = _table_columns(conn, operation.target, backend=backend)
        required = set(operation.key_columns) | set(required_columns)
        missing = sorted(required - columns)
        if missing:
            warnings.append(f"Table {operation.target} is missing columns for {operation.key}: {', '.join(missing)}.")
            return
        operations.append(operation)

    messages_cutoff = _iso(now - timedelta(days=policy.messages_days))
    add(
        CleanupOperation("old_messages", storage.T_MESSAGES, "delete", ("id",), "created_at < ?", (messages_cutoff,)),
        required_columns=("created_at",),
    )
    add(
        CleanupOperation(
            "old_inactive_conversations",
            storage.T_CONVERSATIONS,
            "delete",
            ("chat_id",),
            "updated_at < ? AND COALESCE(status, '') NOT IN ('active', 'waiting_payment')",
            (messages_cutoff,),
        ),
        required_columns=("updated_at", "status"),
    )
    add(
        CleanupOperation(
            "old_system_logs",
            storage.T_SYSTEM_LOGS,
            "delete",
            ("id",),
            "created_at < ?",
            (_iso(now - timedelta(days=policy.system_logs_days)),),
        ),
        required_columns=("created_at",),
    )
    add(
        CleanupOperation(
            "old_inactive_slot_holds",
            storage.T_SLOT_HOLDS,
            "delete",
            ("id",),
            "status IN ('expired', 'released', 'converted') AND COALESCE(updated_at, expires_at, created_at) < ?",
            (_iso(now - timedelta(days=policy.holds_days)),),
        ),
        required_columns=("status", "updated_at", "expires_at", "created_at"),
    )
    add(
        CleanupOperation(
            "active_holds_past_expiry",
            storage.T_SLOT_HOLDS,
            "repair_expired_holds",
            ("id",),
            "status = 'active' AND expires_at < ?",
            (_iso(now),),
        ),
        required_columns=("status", "expires_at", "updated_at"),
    )

    admin_columns = _columns_or_empty(conn, storage.T_ADMIN_NOTIFICATIONS, backend=backend)
    add(
        CleanupOperation(
            "old_sent_admin_notifications",
            storage.T_ADMIN_NOTIFICATIONS,
            "delete",
            ("id",),
            f"status = 'sent' AND {_coalesce_expr(('sent_at', 'created_at'), admin_columns)} < ?",
            (_iso(now - timedelta(days=policy.admin_notifications_days)),),
        ),
        required_columns=("status", "created_at"),
    )

    operations.extend(_availability_cache_operations(policy=policy, now=now, only=only, conn=conn, backend=backend, warnings=warnings))

    watch_columns = _columns_or_empty(conn, storage.T_AVAILABILITY_WATCHLIST, backend=backend)
    add(
        CleanupOperation(
            "old_inactive_watchlist",
            storage.T_AVAILABILITY_WATCHLIST,
            "delete",
            ("id",),
            f"status IN ('notified', 'canceled') AND {_coalesce_expr(('notified_at', 'canceled_at', 'created_at'), watch_columns)} < ?",
            (_iso(now - timedelta(days=policy.watchlist_days)),),
        ),
        required_columns=("status", "created_at"),
    )

    return operations


def _availability_cache_operations(
    *,
    policy: RetentionPolicy,
    now: datetime,
    only: set[str],
    conn: Any,
    backend: str,
    warnings: list[str],
) -> list[CleanupOperation]:
    target = storage.T_AVAILABILITY_CACHE
    keys = ("availability_cache_cleanup", "stale_availability_cache", "past_availability_cache_dates")
    if not any(_matches_only(key, target, only) for key in keys):
        return []
    if not _table_exists(conn, target, backend=backend):
        warnings.append(f"Table {target} is missing; skipped availability cache cleanup.")
        return []
    columns = _table_columns(conn, target, backend=backend)
    missing = sorted({"id", "refreshed_at", "date"} - columns)
    if missing:
        warnings.append(f"Table {target} is missing columns for availability cache cleanup: {', '.join(missing)}.")
        return []

    refreshed_cutoff = _iso(now - timedelta(days=policy.availability_cache_days))
    today = now.date().isoformat()
    explicit_stale = "stale_availability_cache" in only
    explicit_past = "past_availability_cache_dates" in only
    explicit_combined = _matches_only("availability_cache_cleanup", target, only)

    if not only or explicit_combined or (explicit_stale and explicit_past):
        return [
            CleanupOperation(
                "availability_cache_cleanup",
                target,
                "delete",
                ("id",),
                "refreshed_at < ? OR date < ?",
                (refreshed_cutoff, today),
            )
        ]
    if explicit_stale:
        return [CleanupOperation("stale_availability_cache", target, "delete", ("id",), "refreshed_at < ?", (refreshed_cutoff,))]
    if explicit_past:
        return [CleanupOperation("past_availability_cache_dates", target, "delete", ("id",), "date < ?", (today,))]
    return []


def _select_operation_keys(conn: Any, operation: CleanupOperation, *, max_rows: int) -> list[tuple[Any, ...]]:
    if max_rows <= 0:
        return []
    columns = ", ".join(operation.key_columns)
    order = ", ".join(f"{column} ASC" for column in operation.key_columns)
    rows = _fetchall(
        conn,
        f"""
        SELECT {columns}
        FROM {operation.target}
        WHERE {operation.where}
        ORDER BY {order}
        LIMIT ?
        """,
        [*operation.params, max_rows],
    )
    unique: list[tuple[Any, ...]] = []
    seen: set[tuple[Any, ...]] = set()
    for row in rows:
        key = tuple(row.get(column) for column in operation.key_columns)
        if key in seen:
            continue
        seen.add(key)
        unique.append(key)
    return unique


def _delete_operation_keys(conn: Any, operation: CleanupOperation, keys: list[tuple[Any, ...]]) -> int:
    affected = 0
    for chunk in _key_chunks(keys, column_count=len(operation.key_columns)):
        predicate, params = _key_predicate(operation.key_columns, chunk)
        cursor = conn.execute(f"DELETE FROM {operation.target} WHERE {predicate}", params)
        affected += int(getattr(cursor, "rowcount", 0) or 0)
    return affected


def _repair_expired_holds_by_keys(conn: Any, operation: CleanupOperation, keys: list[tuple[Any, ...]], *, now: datetime) -> int:
    affected = 0
    for chunk in _key_chunks(keys, column_count=len(operation.key_columns)):
        predicate, params = _key_predicate(operation.key_columns, chunk)
        cursor = conn.execute(
            f"UPDATE {operation.target} SET status = 'expired', updated_at = ? WHERE {predicate}",
            [_iso(now), *params],
        )
        affected += int(getattr(cursor, "rowcount", 0) or 0)
    return affected


def _delete_old_voice_temp_files(policy: RetentionPolicy, *, now: datetime, max_files: int) -> dict[str, Any]:
    settings = get_settings()
    path = Path(settings.voice_temp_dir)
    if not path.is_absolute():
        path = PROJECT_ROOT / path

    result: dict[str, Any] = {
        "key": "old_voice_temp_files",
        "target": str(path),
        "action": "delete_file",
        "selected": 0,
        "deleted_files": 0,
        "deleted_bytes": 0,
        "warnings": [],
    }
    if max_files <= 0 or not path.exists() or not path.is_dir():
        return result

    try:
        root = path.resolve(strict=True)
    except OSError as exc:
        result["warnings"].append(f"Voice temp path could not be resolved safely: {exc}.")
        return result
    if not _safe_voice_temp_root(root):
        result["warnings"].append(f"Voice temp path is too broad for apply cleanup: {root}.")
        return result

    cutoff = now - timedelta(days=policy.voice_temp_days)
    for item in sorted(path.rglob("*")):
        if result["selected"] >= max_files:
            break
        if not item.is_file():
            continue
        try:
            resolved = item.resolve(strict=True)
            resolved.relative_to(root)
            stat = item.stat()
        except (OSError, ValueError) as exc:
            result["warnings"].append(f"Skipped unsafe voice temp file candidate: {item.name} ({exc}).")
            continue
        modified_at = datetime.fromtimestamp(stat.st_mtime, UTC).replace(tzinfo=None)
        if modified_at >= cutoff:
            continue
        result["selected"] += 1
        try:
            item.unlink()
        except OSError as exc:
            result["warnings"].append(f"Failed to delete voice temp file {item.name}: {exc}.")
            continue
        result["deleted_files"] += 1
        result["deleted_bytes"] += stat.st_size
    return result


def _protected_booking_predicate() -> tuple[str, tuple[Any, ...]]:
    placeholders = _placeholders(len(PROTECTED_BOOKING_STATUSES))
    return (
        f"COALESCE(status, '') IN ({placeholders}) OR COALESCE(payment_id, '') != '' OR COALESCE(yclients_record_id, '') != ''",
        tuple(PROTECTED_BOOKING_STATUSES),
    )


def _columns_or_empty(conn: Any, table: str, *, backend: str) -> set[str]:
    if not _table_exists(conn, table, backend=backend):
        return set()
    return _table_columns(conn, table, backend=backend)


def _append_operational_warning(report: dict[str, Any], message: str) -> None:
    if "operational_warnings" in report:
        report["operational_warnings"].append(message)
    elif "warnings" in report:
        report["warnings"].append(message)
    else:
        report.setdefault("operational_warnings", []).append(message)


def _normalize_only_keys(values: Iterable[str] | None) -> set[str]:
    if not values:
        return set()
    return {str(value).strip().lower() for value in values if str(value).strip()}


def _matches_only(key: str, target: str, only: set[str]) -> bool:
    if not only:
        return True
    return key.lower() in only or target.lower() in only


def _key_chunks(keys: list[tuple[Any, ...]], *, column_count: int) -> Iterable[list[tuple[Any, ...]]]:
    max_params = 900
    chunk_size = max(1, max_params // max(1, column_count))
    for index in range(0, len(keys), chunk_size):
        yield keys[index : index + chunk_size]


def _key_predicate(key_columns: tuple[str, ...], keys: list[tuple[Any, ...]]) -> tuple[str, list[Any]]:
    if not keys:
        return "1 = 0", []
    if len(key_columns) == 1:
        column = key_columns[0]
        return f"{column} IN ({_placeholders(len(keys))})", [key[0] for key in keys]

    clauses: list[str] = []
    params: list[Any] = []
    for key in keys:
        clauses.append("(" + " AND ".join(f"{column} = ?" for column in key_columns) + ")")
        params.extend(key)
    return "(" + " OR ".join(clauses) + ")", params


def _safe_voice_temp_root(path: Path) -> bool:
    if not path.is_absolute():
        return False
    anchors = {Path(path.anchor).resolve()} if path.anchor else set()
    blocked = {PROJECT_ROOT.resolve(), Path.home().resolve(), *anchors}
    return path not in blocked and len(path.parts) >= 3


def _coalesce_expr(preferred_columns: Iterable[str], available_columns: Iterable[str]) -> str:
    available = set(available_columns)
    columns = [column for column in preferred_columns if column in available]
    if not columns:
        return "''"
    if len(columns) == 1:
        return columns[0]
    return f"COALESCE({', '.join(columns)})"


def _range_column(columns: set[str]) -> str | None:
    for candidate in ("created_at", "updated_at", "refreshed_at", "expires_at", "date"):
        if candidate in columns:
            return candidate
    return None


def _table_exists(conn: Any, table: str, *, backend: str) -> bool:
    if backend == "postgres":
        row = _fetchone(
            conn,
            """
            SELECT 1 AS value
            FROM information_schema.tables
            WHERE table_schema = 'public' AND table_name = ?
            LIMIT 1
            """,
            [table],
        )
        return row is not None
    row = _fetchone(
        conn,
        "SELECT 1 AS value FROM sqlite_master WHERE type = 'table' AND name = ? LIMIT 1",
        [table],
    )
    return row is not None


def _table_columns(conn: Any, table: str, *, backend: str) -> set[str]:
    if backend == "postgres":
        rows = _fetchall(
            conn,
            """
            SELECT column_name
            FROM information_schema.columns
            WHERE table_schema = 'public' AND table_name = ?
            """,
            [table],
        )
        return {str(row["column_name"]) for row in rows}
    rows = _fetchall(conn, f"PRAGMA table_info({table})")
    return {str(row["name"]) for row in rows}


def _count(conn: Any, table: str, where: str = "", params: Iterable[Any] = ()) -> int:
    query = f"SELECT COUNT(*) AS value FROM {table}"
    if where:
        query += f" WHERE {where}"
    row = _fetchone(conn, query, list(params))
    return int(row["value"] if row else 0)


def _status_counts(conn: Any, table: str) -> dict[str, int]:
    rows = _fetchall(
        conn,
        f"""
        SELECT COALESCE(status, '') AS status, COUNT(*) AS row_count
        FROM {table}
        GROUP BY status
        ORDER BY row_count DESC, status ASC
        """,
    )
    return {str(row["status"]): int(row["row_count"]) for row in rows}


def _min_max(conn: Any, table: str, column: str) -> dict[str, Any]:
    row = _fetchone(conn, f"SELECT MIN({column}) AS oldest, MAX({column}) AS newest FROM {table}")
    return {"oldest": row.get("oldest") if row else None, "newest": row.get("newest") if row else None}


def _fetchone(conn: Any, query: str, params: Iterable[Any] = ()) -> dict[str, Any] | None:
    row = conn.execute(query, list(params)).fetchone()
    return _row_to_dict(row) if row is not None else None


def _fetchall(conn: Any, query: str, params: Iterable[Any] = ()) -> list[dict[str, Any]]:
    rows = conn.execute(query, list(params)).fetchall()
    return [_row_to_dict(row) for row in rows]


def _row_to_dict(row: Any) -> dict[str, Any]:
    if row is None:
        return {}
    if isinstance(row, dict):
        return dict(row)
    if hasattr(row, "keys"):
        return {key: row[key] for key in row.keys()}
    return dict(row)


def _placeholders(count: int) -> str:
    return ", ".join("?" for _ in range(count))


def _iso(value: datetime) -> str:
    return value.replace(microsecond=0).isoformat()


def _utc_now() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)
