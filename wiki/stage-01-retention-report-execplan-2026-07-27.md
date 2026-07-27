# Stage 1 Retention Cleanup

This ExecPlan is a living document. The sections `Progress`, `Surprises & Discoveries`, `Decision Log`, and `Outcomes & Retrospective` must be kept up to date as work proceeds.

This plan follows `PLANS.md` from the repository root.

## Purpose / Big Picture

Stage 1 adds a read-only data hygiene report, a dry-run cleanup plan, and guarded apply code for MaxBot 4. The goal is to understand and clean old runtime rows without touching protected booking, payment, YCLIENTS, hold, notification, or watchlist evidence.

This block must not create payment/YCLIENTS side effects, send bot messages, or run production cleanup. Report and dry-run modes perform only SELECT queries and optional local voice-temp file metadata scanning. Apply mode exists only behind explicit `--apply --yes`; it was tested against temporary SQLite and then applied once to the agreed test Supabase sandbox.

## Progress

- [x] (2026-07-27 Europe/Moscow) Inspected `app/storage/sqlite.py` runtime tables and storage helpers.
- [x] (2026-07-27 Europe/Moscow) Compared MaxBot 2 retention report pattern and adapted it to MaxBot 4 tables.
- [x] (2026-07-27 Europe/Moscow) Added `app/maintenance/retention.py` with read-only `RetentionPolicy`, `build_retention_report()`, and `render_retention_report()`.
- [x] (2026-07-27 Europe/Moscow) Added `scripts/retention_report.py` CLI with text and JSON output.
- [x] (2026-07-27 Europe/Moscow) Added `tests/retention_report_smoke.py` using temporary SQLite.
- [x] (2026-07-27 Europe/Moscow) Added retention report smoke to `scripts/validate.ps1 -IncludeSmoke`.
- [x] (2026-07-27 Europe/Moscow) Ran focused and full safe validation successfully.
- [x] (2026-07-27 Europe/Moscow) Added `build_cleanup_plan()` and `render_cleanup_plan()` as non-mutating dry-run helpers.
- [x] (2026-07-27 Europe/Moscow) Added `scripts/retention_cleanup.py` dry-run CLI.
- [x] (2026-07-27 Europe/Moscow) Added `tests/retention_cleanup_smoke.py` proving dry-run and refused apply do not mutate temp SQLite.
- [x] (2026-07-27 Europe/Moscow) Added cleanup dry-run smoke to `scripts/validate.ps1 -IncludeSmoke`.
- [x] (2026-07-27 Europe/Moscow) Added guarded apply operations for old messages, inactive conversations, system logs, inactive holds, expired active-hold repair, sent admin notifications, availability cache, inactive watchlist rows, and scoped voice-temp files.
- [x] (2026-07-27 Europe/Moscow) Kept bookings and active watchlist past dates as manual-review/report-only candidates.
- [x] (2026-07-27 Europe/Moscow) Updated `scripts/retention_cleanup.py` so real cleanup requires `--apply --yes`.
- [x] (2026-07-27 Europe/Moscow) Expanded `tests/retention_cleanup_smoke.py` for refused apply, guarded apply, max row limiting, hold repair, idempotence, and CLI apply.
- [x] (2026-07-27 Europe/Moscow) Added automatic retention scheduler for MAX and Telegram runtimes behind `RETENTION_CLEANUP_ENABLED`.
- [x] (2026-07-27 Europe/Moscow) Added `tests/retention_scheduler_smoke.py` and included it in `scripts/validate.ps1 -IncludeSmoke`.
- [x] (2026-07-27 Europe/Moscow) Confirmed the current Stage 1 sandbox Supabase database at `aws-0-eu-north-1.pooler.supabase.com`.
- [x] (2026-07-27 Europe/Moscow) Ran guarded apply against the test Supabase sandbox for `old_inactive_slot_holds` and `past_availability_cache_dates`.
- [x] (2026-07-27 Europe/Moscow) Verified post-cleanup report: both selected cleanup candidate groups are now zero, protected bookings/conversations/notifications remain present, and warnings are zero.

## Surprises & Discoveries

- Observation: the read-only report on the default local SQLite target does not create `bot.sqlite3` when it is missing.
  Evidence: `scripts/retention_report.py --limit 3 --skip-files --json` returns `exists: false` and `SQLite database file does not exist; no connection was opened`.
- Observation: the current schema still lacks `mvp_availability_watchlist.platform`.
  Evidence: the retention smoke expects and receives a schema warning after `init_db()` creates a temp DB.
- Observation: `mvp_processed_updates` remains absent in MaxBot 4.
  Evidence: the retention report emits a recommended-table warning for `mvp_processed_updates`.
- Observation: `scripts/retention_cleanup.py --apply` without `--yes` is refused.
  Evidence: the cleanup smoke asserts exit code 2, mode `apply_refused`, and unchanged row counts.
- Observation: confirmed apply on a missing default local SQLite database does not create `bot.sqlite3`.
  Evidence: `scripts/retention_cleanup.py --apply --yes --max-rows 5 --skip-files --json` returns mode `apply_skipped` with a missing database warning, and `bot.sqlite3` remains absent.
- Observation: the agreed test Supabase schema already has `mvp_availability_watchlist.platform` and `mvp_processed_updates`.
  Evidence: `scripts/test-db-retention-report.ps1 -Json` reported both without schema warnings.
- Observation: after the test Supabase cleanup, selected cleanup candidates dropped to zero.
  Evidence: post-cleanup dry-run for `old_inactive_slot_holds,past_availability_cache_dates` returned `future_delete_candidate_signals: 0`, `items_with_matches: 0`, and `warnings: []`.

## Decision Log

- Decision: implement only the read-only report in this step.
  Rationale: the requested staged rollout starts with visibility before dry-run and apply cleanup.
  Date/Author: 2026-07-27 / Codex.
- Decision: do not sample chat ids, message text, raw payloads, phone numbers, or secrets in the report.
  Rationale: retention reports should be safe to paste into operational notes.
  Date/Author: 2026-07-27 / Codex.
- Decision: bookings are reported as protected or manual-review candidates only.
  Rationale: bookings may contain payment and YCLIENTS evidence and must not be automatically deleted.
  Date/Author: 2026-07-27 / Codex.
- Decision: keep `mvp_processed_updates` absent but visible as a schema warning.
  Rationale: adding the table is a schema/runtime decision and should be handled deliberately in a later Stage 1 substep.
  Date/Author: 2026-07-27 / Codex.
- Decision: expose guarded apply through `scripts/retention_cleanup.py --apply --yes` only.
  Rationale: dry-run remains the default, accidental `--apply` is refused without `--yes`, and production cleanup must still be an explicit operator action.
  Date/Author: 2026-07-27 / Codex.
- Decision: use a combined availability-cache apply operation by default.
  Rationale: rows can match both stale-cache and past-date candidates, so apply must deduplicate by id.
  Date/Author: 2026-07-27 / Codex.
- Decision: automatic retention cleanup is available but disabled by default.
  Rationale: test and production targets need separate review; runtime should never mutate an unknown database just because code was deployed.
  Date/Author: 2026-07-27 / Codex.
- Decision: close Stage 1 after proving report, dry-run, guarded apply, scheduler smoke, and test Supabase cleanup.
  Rationale: the remaining production step is operational rollout, not feature implementation.
  Date/Author: 2026-07-27 / Codex.

## Outcomes & Retrospective

Completed. MaxBot 4 now has a reusable read-only retention report, an operator report CLI, a dry-run cleanup CLI, guarded apply code, test-Supabase wrappers, and an automatic runtime scheduler behind `RETENTION_CLEANUP_ENABLED`.

The report covers current runtime tables, stale candidates, repair candidates, protected rows, missing schema pieces, and optional voice-temp file metadata. The cleanup CLI turns those signals into plan items by default and applies changes only with `--apply --yes`.

On the agreed test Supabase sandbox, guarded cleanup deleted 192 rows: 18 inactive slot holds and 174 availability-cache rows for past dates. Post-cleanup report confirmed zero remaining candidates in those selected groups. Protected bookings, active conversations, and pending admin notifications remained present. Production cleanup was not run.

## Context and Orientation

The report lives in `app/maintenance/retention.py` and uses the existing `app.storage.sqlite.connect()` wrapper so it can inspect SQLite or Postgres. For SQLite, it checks that the database file exists before connecting, preventing accidental local DB creation.

The CLI lives in `scripts/retention_report.py`. It supports:

    python scripts\retention_report.py --limit 20
    python scripts\retention_report.py --limit 20 --json
    python scripts\retention_report.py --limit 20 --skip-files

The smoke test lives in `tests/retention_report_smoke.py` and creates a temporary `APP_ENV_FILE` and temporary SQLite DB.

The dry-run cleanup CLI lives in `scripts/retention_cleanup.py`. It supports:

    python scripts\retention_cleanup.py --limit 20
    python scripts\retention_cleanup.py --limit 20 --json
    python scripts\retention_cleanup.py --only old_messages --skip-files

Real cleanup requires both flags:

    python scripts\retention_cleanup.py --apply --yes --max-rows 1000 --skip-files

The automatic scheduler lives in `app/maintenance/scheduler.py` and is started from both MAX and Telegram runtimes when:

    RETENTION_CLEANUP_ENABLED=true

Use `RETENTION_CLEANUP_DRY_RUN=true` when observing a new target before allowing mutations.

## Validation and Acceptance

Acceptance requires:

- report logic exists in `app/maintenance/retention.py`;
- operator CLI exists in `scripts/retention_report.py`;
- dry-run cleanup CLI exists in `scripts/retention_cleanup.py`;
- focused smoke test exists and passes;
- `scripts/validate.ps1 -IncludeSmoke` includes the retention smoke and passes;
- report does not call `init_db()`;
- missing SQLite file is reported without opening/creating it;
- protected bookings, active holds, pending notifications, and active watchlist rows are counted separately from cleanup candidates;
- dry-run cleanup plan is non-mutating;
- apply mode requires `--apply --yes`, re-selects keys inside the apply transaction, and is covered by temp SQLite smoke tests;
- automatic scheduler is disabled by default and covered by smoke tests;
- test Supabase sandbox cleanup was applied and verified;
- no commit, push, PR, production cleanup, payment write, YCLIENTS write, or bot message send occurs.

Validation evidence:

- `.\.venv\Scripts\python.exe tests\retention_report_smoke.py` passed with `OK retention report smoke`.
- `.\.venv\Scripts\python.exe tests\retention_cleanup_smoke.py` passed with `OK retention cleanup guarded apply smoke`.
- `.\scripts\validate.ps1 -IncludeSmoke` passed.
- `.\.venv\Scripts\python.exe scripts\retention_report.py --limit 3 --skip-files --json` passed and did not create the missing default local SQLite database.
- `.\.venv\Scripts\python.exe scripts\retention_cleanup.py --limit 3 --skip-files` passed as dry-run.
- `.\.venv\Scripts\python.exe scripts\retention_cleanup.py --apply --yes --max-rows 5 --skip-files --json` passed against the missing default local SQLite target as `apply_skipped` with zero changed rows and no DB creation.
- `.\scripts\test-db-retention-cleanup.ps1 -Only old_inactive_slot_holds,past_availability_cache_dates -MaxRows 1000 -Apply -Yes -Json` passed against the test Supabase sandbox and deleted 192 selected rows.
- Post-cleanup `.\scripts\test-db-retention-report.ps1 -Json` passed with zero selected candidates and no warnings.

## Next Step

Stage 1 is complete for implementation and the agreed test sandbox. The next implementation stage is Stage 2 YAML universalization. Before enabling retention apply on production, run the production report and dry-run first, then enable the scheduler only for that reviewed production target.
