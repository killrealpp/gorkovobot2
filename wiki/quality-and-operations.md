---
title: "Quality And Operations"
created: 2026-07-27
tags:
  - max-bot4
  - quality
  - operations
---

# Quality And Operations

## Safe Baseline Validation

Compile-only validation is safe and does not call external APIs:

```powershell
python -m compileall app main.py scripts tests
```

The project wrapper runs compile and wiki checks:

```powershell
.\scripts\validate.ps1
```

Current baseline note: on 2026-07-27, compile validation passes after two minimal baseline repairs: the unterminated string in `app/ai/engine.py` and the missing `load_engine_response_prompt()` import target in `app/ai/parser.py`.

A local Windows `.venv/` can be used for validation. It is ignored by git and was created only to run local checks from this workstation.

## Smoke Tests

Smoke tests must not use the production `.env`. Use the validation wrapper with isolated temporary runtime settings:

```powershell
.\scripts\validate.ps1 -IncludeSmoke
```

This wrapper creates a temporary `APP_ENV_FILE` and temporary SQLite database before running script-style smoke tests.

Current status: `.\scripts\validate.ps1 -IncludeSmoke` passes with isolated settings.

The current smoke set includes `tests/smoke.py`, `tests/admin_profile_smoke.py`, `tests/stage2_behavior_parity_smoke.py`, the read-only retention report smoke, the retention cleanup smoke, and the retention scheduler smoke.

Legacy internal smoke scripts are available behind an explicit flag:

```powershell
.\scripts\validate.ps1 -IncludeSmoke -IncludeLegacySmoke
```

Current legacy status: `tests/validation_smoke.py` and `tests/booking_flow_smoke.py` are stale relative to the current dialog engine internals. The first failure is `ImportError: cannot import name '_merge_fields' from 'app.dialog.engine'`.

## External Side Effects

Avoid live side effects unless explicitly requested:

- no production DB mutations;
- no live YooKassa payment creation;
- no live YCLIENTS writes;
- no external admin/customer messages;
- no commit, push, pull request, or deploy without explicit instruction.

## Retention Report

The Stage 1 report is read-only:

```powershell
.\.venv\Scripts\python.exe scripts\retention_report.py --limit 20 --skip-files
.\.venv\Scripts\python.exe scripts\retention_report.py --limit 20 --skip-files --json
```

Do not run it against a live database unless the task explicitly targets that live read-only inspection.

The Stage 1 cleanup command is dry-run by default:

```powershell
.\.venv\Scripts\python.exe scripts\retention_cleanup.py --limit 20 --skip-files
.\.venv\Scripts\python.exe scripts\retention_cleanup.py --limit 20 --skip-files --json
```

Current status: real cleanup exists only behind explicit confirmation:

```powershell
.\.venv\Scripts\python.exe scripts\retention_cleanup.py --apply --yes --max-rows 1000 --skip-files
```

Do not run confirmed apply against a live database until report and dry-run output have been reviewed.

## Test Supabase Retention Target

The current cleanup sandbox is the test Supabase database:

```text
DB_HOST=aws-0-eu-north-1.pooler.supabase.com
DB_PORT=5432
DB_NAME=postgres
DB_USER=postgres.apchmukcofaggmamkdte
```

The password stays in local secrets and must not be printed. Use the wrappers below when intentionally targeting this test database:

```powershell
.\scripts\test-db-retention-report.ps1 -Json
.\scripts\test-db-retention-cleanup.ps1 -Json
```

The cleanup wrapper is dry-run by default. A real test cleanup requires both `-Apply` and `-Yes`, plus a reviewed `-Only` scope when possible:

```powershell
.\scripts\test-db-retention-cleanup.ps1 -Only old_inactive_slot_holds -Apply -Yes
```

Do not point these wrappers at production. Production cleanup must use an explicit report and dry-run for the production DSN before any confirmed apply.

## Automatic Retention Cleanup

The bot can run retention cleanup automatically as a background task. It starts with the bot only when enabled:

```text
RETENTION_CLEANUP_ENABLED=true
RETENTION_CLEANUP_INTERVAL_SECONDS=86400
RETENTION_CLEANUP_STARTUP_DELAY_SECONDS=300
RETENTION_CLEANUP_MAX_ROWS=1000
RETENTION_CLEANUP_DRY_RUN=false
```

Automatic cleanup uses the same guarded engine as `scripts/retention_cleanup.py`: it never deletes bookings automatically, protects active holds, protects active watchlist rows, protects pending admin notifications, and logs summary counts instead of message text or secrets.

Default automatic scope:

```text
old_messages,old_inactive_conversations,old_system_logs,old_inactive_slot_holds,active_holds_past_expiry,old_sent_admin_notifications,stale_availability_cache,past_availability_cache_dates,old_inactive_watchlist,old_voice_temp_files
```

Use `RETENTION_CLEANUP_DRY_RUN=true` to let the bot log planning output without mutating rows. Use `RETENTION_CLEANUP_ONLY=...` to narrow the automated scope during testing.

## Documentation Rule

For durable product, storage, AI, payment, or integration decisions, update the relevant wiki page and append `wiki/log.md`.

## Business Profile Validation

The Stage 2 profile smoke can be run directly:

```powershell
.\.venv\Scripts\python.exe tests\admin_profile_smoke.py
.\.venv\Scripts\python.exe tests\stage2_behavior_parity_smoke.py
```

These checks are local-only and do not call external APIs. They prove profile/service parity, prompt rendering, media paths, next-step behavior, profile-driven extra-hour pricing, prepayment percent, post-payment message rendering, and old behavior parity for the key booking surface.
