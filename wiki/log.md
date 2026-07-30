---
title: "MaxBot 4 Wiki Log"
created: 2026-07-27
tags:
  - max-bot4
  - log
---

# MaxBot 4 Wiki Log

## 2026-07-29

- Audited public catalog API/local admin boundary for catalog-only readiness.
- Stabilized `tests/booking_flow_smoke.py` so it checks current booking-flow contracts instead of removed private dialog-engine helpers, and added it to normal `.\scripts\validate.ps1 -IncludeSmoke`.
- Restored tracked `scripts/graphify-build.ps1` and `scripts/tooling-status.ps1` wrappers without installing tools; both report local Graphify/Headroom status.
- Recorded that the existing `graphify-out/` graph is stale for public catalog work because it predates `app/api/`, `app/catalog/`, the fixture generator, and `tests/public_catalog_smoke.py`.
- Updated tooling wiki notes so Headroom control scripts are not documented as repo-present wrappers; direct CLI usage and a follow-up TODO are documented instead.

## 2026-07-28

- Added a public catalog API ExecPlan for preparing MaxBot as a backend/data-layer for Viksa Online and other clients.
- Added `GET /api/public/catalog` in MAX webhook mode. The endpoint is read-only, unauthenticated, and built from `business_profile/admin_profile.yaml`, `app/data/services.py`, media config, and `mvp_availability_cache`.
- Normalized the public catalog into business, categories, services, facilities, variants, tariffs, media, payment/post-payment public text, integration refs, rules, warnings, and availability state.
- Kept incomplete profile placeholders such as `summer_gazebo` and `gazebo_bathhouse` in `services` with warnings instead of exposing them as concrete facilities.
- Added smoke coverage for `Причал`, all real public facilities, media refs, secret-key exclusion, and honest `no-data`/`stale` availability states.
- Ran `.\scripts\validate.ps1 -IncludeSmoke` successfully with temporary `APP_ENV_FILE` and SQLite.
- Added independent public API entrypoint `app.api.server` so Viksa Online can use `http://127.0.0.1:8090` while MaxBot stays in polling mode.
- Added explicit local CORS origins for the public API: `http://127.0.0.1:5173` and `http://localhost:5173`.
- Added browser-ready media URLs through `media[].publicUrl` and `GET /api/public/media/{media_key}`.
- Added deterministic frontend fixture `tests/fixtures/public_catalog.v1.json` and `scripts/generate_public_catalog_fixture.py`.
- Expanded public catalog smoke to cover `/health`, CORS preflight, media responses, fixture validity, secret exclusion, and stale/no-data availability state behavior.

## 2026-07-27

- Added the initial wiki layer for MaxBot 4 preparation work.
- Recorded the three-stage improvement direction: retention cleanup, YAML universalization, approved-only RAG.
- Recorded baseline risks before Stage 1: missing `mvp_availability_watchlist.platform` DDL, no `mvp_processed_updates`, no active `business_profile/`, no active `app/maintenance/`, no active `app/knowledge/`.
- Added safe validation guidance: compile baseline is safe; smoke tests must use temporary `APP_ENV_FILE` and temporary SQLite.
- Ran validation: initial compile was blocked by a pre-existing syntax error in `app/ai/engine.py`, line 889.
- Added optional Graphify and Headroom development tooling scripts, `.graphifyignore`, `.graphify/providers.json`, and tooling documentation. These do not affect bot runtime.
- Repaired the minimal baseline blockers: `app/ai/engine.py` now compiles, and `app/ai/parser.py` exposes `load_engine_response_prompt()` for the existing `app/ai/engine_response_prompt.md`.
- Created an ignored local Windows `.venv/` from existing `requirements.txt` to run local validation.
- Updated `tests/smoke.py` to match the current no-op `fallback_decision()` contract.
- Ran `.\scripts\validate.ps1 -IncludeSmoke`: compile, current smoke, and wiki check pass with temporary `APP_ENV_FILE` and SQLite.
- Built AST-only Graphify output: 1110 nodes / 2919 edges.
- Recorded legacy smoke drift: `.\scripts\validate.ps1 -IncludeSmoke -IncludeLegacySmoke` fails because old smoke files import removed private `_merge_fields`.
- Added Stage 1 read-only retention report: `app/maintenance/retention.py`, `scripts/retention_report.py`, and `tests/retention_report_smoke.py`.
- Updated validation so `.\scripts\validate.ps1 -IncludeSmoke` runs the retention report smoke.
- Ran retention report validation: focused smoke passed; full `-IncludeSmoke` validation passed; local missing SQLite report returned a warning without creating `bot.sqlite3`.
- Added Stage 1 dry-run cleanup plan: `build_cleanup_plan()`, `render_cleanup_plan()`, `scripts/retention_cleanup.py`, and `tests/retention_cleanup_smoke.py`.
- Verified dry-run cleanup: normal CLI passes without mutation; `--apply --yes` is intentionally refused with mode `apply_refused`.
- Added guarded apply mode behind `--apply --yes`, including transaction-time key re-selection, `--max-rows`, `--only`, availability-cache deduplication, expired-hold repair, and scoped voice-temp deletion.
- Expanded cleanup smoke to prove refused apply, confirmed apply on temp SQLite, max row limiting, idempotence, and active-hold repair.
- Ran confirmed apply only against temporary SQLite and the missing default local SQLite target; no production cleanup was run.
- Tightened cleanup smoke to verify missing SQLite uses `apply_skipped`, bookings remain protected, and pending admin notifications survive sent-notification cleanup.
- Confirmed the current Stage 1 sandbox is the test Supabase database at `aws-0-eu-north-1.pooler.supabase.com`; read-only report and dry-run cleanup work against that schema.
- Added test-Supabase wrappers for retention report and cleanup. They inject only non-secret target settings, keep cleanup dry-run by default, and still require explicit apply confirmation.
- Added automatic retention cleanup scheduler for MAX and Telegram bot runtimes. It is disabled by default and starts only with `RETENTION_CLEANUP_ENABLED=true`.
- Added smoke coverage proving the configured scheduler can apply a scoped cleanup cycle against temporary SQLite without touching protected data.
- Aligned automatic cleanup dry-run visibility with apply behavior by listing availability-cache cleanup scopes explicitly in the default scheduler scope.
- Ran guarded cleanup apply against the agreed test Supabase sandbox for `old_inactive_slot_holds` and `past_availability_cache_dates`; 192 rows were deleted, with zero warnings.
- Verified post-cleanup test Supabase report: selected cleanup candidates are zero; protected bookings, active conversations, and pending admin notifications remain present.
- Marked Stage 1 retention cleanup complete for implementation and the test sandbox. Production cleanup remains a separate reviewed rollout.
- Added Stage 2 business profile layer: `business_profile/admin_profile.yaml`, `business_profile/knowledge.md`, and `app/data/admin_profile.py`.
- Switched compatibility service APIs in `app/data/services.py` to read from the business profile while preserving the existing import surface.
- Routed media, booking next steps, availability duration mapping, pricing, prepayment percent, post-payment messages, start text, and prompt rendering through the profile-backed layer.
- Aligned `app/ai/admin_prompt.md` and `business_profile/knowledge.md` with the profile-backed catalog, including bathhouse capacity and extra-hour pricing rules.
- Added `tests/admin_profile_smoke.py` and validation coverage for profile parity, prompt rendering, media files, profile-driven extra-hour pricing, prepayment percent, and post-payment messages.
- Re-audited Stage 2 for behavior preservation. Restored compatibility `service_title()` behavior, restored `_OBJECT_TITLE_BY_SERVICE`, kept the old bathhouse capacity guard at 15 people, and restored old post-payment/YCLIENTS comment wording while keeping values profile-backed.
- Added `tests/stage2_behavior_parity_smoke.py` and included it in `.\scripts\validate.ps1 -IncludeSmoke`.
- Ran Stage 2 validation: compile passes, `tests/admin_profile_smoke.py` passes, and the full `.\scripts\validate.ps1 -IncludeSmoke` flow passes after the profile smoke was added.
- Marked Stage 2 YAML universalization complete locally. The next implementation stage is approved-only RAG.
