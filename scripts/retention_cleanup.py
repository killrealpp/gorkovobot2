from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.maintenance.retention import (
    RetentionPolicy,
    apply_cleanup,
    build_cleanup_plan,
    render_apply_result,
    render_cleanup_plan,
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Build or apply a guarded MaxBot 4 retention cleanup plan.")
    parser.add_argument("--limit", type=int, default=20, help="Maximum number of sample ids/names per section.")
    parser.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    parser.add_argument("--skip-files", action="store_true", help="Skip local voice temp directory scan.")
    parser.add_argument("--only", action="append", default=[], help="Limit plan to a category key or table name. Repeatable.")
    parser.add_argument("--apply", action="store_true", help="Perform real guarded cleanup. Requires --yes.")
    parser.add_argument("--yes", action="store_true", help="Confirm real cleanup when used with --apply.")
    parser.add_argument("--max-rows", type=int, default=1000, help="Maximum rows/files per apply operation.")
    parser.add_argument("--messages-days", type=int, default=RetentionPolicy.messages_days)
    parser.add_argument("--logs-days", type=int, default=RetentionPolicy.system_logs_days)
    parser.add_argument("--holds-days", type=int, default=RetentionPolicy.holds_days)
    parser.add_argument("--notifications-days", type=int, default=RetentionPolicy.admin_notifications_days)
    parser.add_argument("--availability-days", type=int, default=RetentionPolicy.availability_cache_days)
    parser.add_argument("--watchlist-days", type=int, default=RetentionPolicy.watchlist_days)
    parser.add_argument("--voice-days", type=int, default=RetentionPolicy.voice_temp_days)
    parser.add_argument("--booking-review-days", type=int, default=RetentionPolicy.booking_review_days)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    policy = RetentionPolicy(
        messages_days=args.messages_days,
        system_logs_days=args.logs_days,
        holds_days=args.holds_days,
        admin_notifications_days=args.notifications_days,
        availability_cache_days=args.availability_days,
        watchlist_days=args.watchlist_days,
        voice_temp_days=args.voice_days,
        booking_review_days=args.booking_review_days,
    )
    if args.apply:
        result = apply_cleanup(
            policy,
            max_rows=args.max_rows,
            include_files=not args.skip_files,
            only_keys=args.only,
            confirmed=bool(args.yes),
        )
        if args.json:
            print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
        else:
            print(render_apply_result(result))
        if not args.yes:
            raise SystemExit(2)
        return

    plan = build_cleanup_plan(policy, limit=args.limit, include_files=not args.skip_files, only_keys=args.only)
    if args.json:
        print(json.dumps(plan, ensure_ascii=False, indent=2, default=str))
        return
    print(render_cleanup_plan(plan))


if __name__ == "__main__":
    main()
