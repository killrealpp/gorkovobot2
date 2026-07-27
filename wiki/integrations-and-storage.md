---
title: "Integrations And Storage"
created: 2026-07-27
tags:
  - max-bot4
  - storage
  - integrations
---

# Integrations And Storage

## Storage

`app/storage/sqlite.py` supports SQLite by default and Postgres when `DB_HOST`, `DB_NAME`, and `DB_USER` are configured. The current runtime tables are:

- `mvp_conversations`
- `mvp_messages`
- `mvp_bookings`
- `mvp_slot_holds`
- `mvp_system_logs`
- `mvp_admin_notifications`
- `mvp_availability_cache`
- `mvp_availability_watchlist`

## Known Baseline Risks

- `create_watchlist()` and `list_active_watchlist(platform=...)` use `mvp_availability_watchlist.platform`, but current table creation does not add that column in SQLite or Postgres DDL.
- `mvp_processed_updates` is absent. MaxBot 2 uses it for update deduplication and includes it in retention cleanup.
- `mvp_bookings` does not have a separate `payment_url` column. The payment URL currently lives in `draft_json`.
- `app/knowledge/` and `app/runtime/` are not active in MaxBot 4 yet.

## Business Profile

Stage 2 adds `business_profile/admin_profile.yaml` as the active editable non-secret business profile. It contains service catalog data, YCLIENTS service/staff ids, media mapping, payment policy text, post-payment instructions, FAQ, and prompt variables.

`app/data/admin_profile.py` loads and validates that profile. `app/data/services.py` now reads through the profile while preserving the old import API used by existing dialog code.

Secrets, database passwords, provider tokens, and runtime flags stay in `.env` and must not be copied into the business profile.

## Retention Visibility

Stage 1 has a read-only retention report in `app/maintenance/retention.py` and `scripts/retention_report.py`. It counts stale rows, protected rows, missing schema pieces, and optional voice-temp file metadata without deleting or updating anything.

These are not fixed in the preparation block. They must be handled deliberately in later ExecPlans.

## Integrations

- MAX adapter: `app/bot/max.py`, `app/bot/max_client.py`.
- Telegram adapter: `app/bot/telegram.py`.
- YooKassa payment client: `app/integrations/yookassa.py`.
- YCLIENTS schedule and records: `app/integrations/yclients.py`, `app/integrations/yclients_sync_service.py`, `app/storage/yclients_records_repo.py`.

## Safety Rule

Do not run checks against production `.env` unless the task explicitly targets a live operation. Use temporary `APP_ENV_FILE` and temporary SQLite for smoke validation.
