# Public Catalog API ExecPlan

This ExecPlan is a living document. The sections `Progress`, `Surprises & Discoveries`, `Decision Log`, and `Outcomes & Retrospective` must be kept up to date as work proceeds.

This plan follows `PLANS.md` from the repository root.

## Purpose / Big Picture

MaxBot 4 must expose a safe read-only catalog endpoint for Viksa Online and other clients without changing the existing MAX booking flow. After this work, a frontend can call `GET /api/public/catalog` and receive normalized public business, service, facility, tariff, media, integration id, rule, warning, and cached availability metadata derived from the current MaxBot profile.

The endpoint must never mutate bookings, payments, YCLIENTS records, Supabase/Postgres runtime rows, or `.env` secrets. Unknown or old availability must be represented as `no-data` or `stale`, not as confirmed availability.

## Progress

- [x] (2026-07-28 08:45 Europe/Moscow) Inspected git state, repository root, wiki index, project overview, integrations/storage notes, dialog flow notes, Stage 2 profile ExecPlan, Graphify output location, `admin_profile.yaml`, `app/data`, dialog pricing/availability/payment/media code, MAX FastAPI webhook, and storage availability-cache helpers.
- [x] (2026-07-28 09:05 Europe/Moscow) Added `app/api/public_catalog.py` and `app/api/__init__.py` with a read-only catalog builder and route registration helper.
- [x] (2026-07-28 09:08 Europe/Moscow) Registered `GET /api/public/catalog` inside the MAX FastAPI webhook app without changing the existing `/health` or MAX webhook behavior.
- [x] (2026-07-28 09:15 Europe/Moscow) Added `tests/public_catalog_smoke.py` for endpoint shape, real facilities, media refs, secret-key exclusion, and no-data/stale availability states.
- [x] (2026-07-28 09:18 Europe/Moscow) Added the public catalog smoke to `scripts/validate.ps1 -IncludeSmoke`.
- [x] (2026-07-28 09:25 Europe/Moscow) Ran compile, focused smoke, and full validation successfully.
- [x] (2026-07-28 10:05 Europe/Moscow) Added independent `app/api/server.py` FastAPI entrypoint with `/health` and public routes.
- [x] (2026-07-28 10:08 Europe/Moscow) Added `PUBLIC_API_HOST=127.0.0.1`, `PUBLIC_API_PORT=8090`, and explicit local CORS origins.
- [x] (2026-07-28 10:12 Europe/Moscow) Added browser-ready `media[].publicUrl` and `GET /api/public/media/{media_key}`.
- [x] (2026-07-28 10:18 Europe/Moscow) Added deterministic fixture generator and `tests/fixtures/public_catalog.v1.json`.
- [x] (2026-07-28 10:25 Europe/Moscow) Expanded public catalog smoke for the separate API app, `/health`, CORS, media URLs, fixture validity, secret exclusion, and stale/no-data availability honesty.
- [x] (2026-07-28 10:30 Europe/Moscow) Updated README and `.env.example`, then ran full validation successfully.
- [ ] Fix facility-level `publicTitle` so concrete objects do not inherit generic service labels.
- [ ] Add ignored local override storage and merge it into `public_catalog.v1`.
- [ ] Add local-only admin draft endpoints guarded by `CATALOG_ADMIN_LOCAL_ENABLED`.
- [ ] Regenerate fixture and expand smoke coverage for local draft overrides.

## Surprises & Discoveries

- Observation: There are no existing public API DTOs or routers; FastAPI is currently created inside `app.bot.max._run_webhook()`.
  Evidence: `rg -n "BaseModel|APIRouter|FastAPI|TestClient|DTO|dto" app tests` only found settings, dataclasses, and the local `FastAPI(title="MAX booking bot")` in `app/bot/max.py`.
- Observation: `business_profile/admin_profile.yaml` is the active service/profile source, while `app/data/services.yaml` remains a legacy parity reference.
  Evidence: `wiki/stage-02-yaml-universalization-execplan-2026-07-27.md` and `app/data/services.py`.
- Observation: Current availability cache helpers can read cache age and rows without live YCLIENTS calls.
  Evidence: `app/storage/sqlite.py` exposes `availability_cache_age_seconds()` and `list_availability_rows()`.
- Observation: The public facility list should not expose incomplete profile service placeholders as concrete facilities.
  Evidence: `summer_gazebo` and `gazebo_bathhouse` have no `facility_id`, no media, no price/tariff, and no YCLIENTS refs; they remain in `services` with warnings instead of `facilities`.
- Observation: The MaxBot polling module should not import the public API route helper at module import time.
  Evidence: `app/bot/max.py` now imports `register_public_catalog_routes` inside `_run_webhook()`, so polling-mode import stays thin.

## Decision Log

- Decision: Build the public catalog from `load_admin_profile()`, `load_services()`, `media_catalog()`, and read-only availability-cache storage helpers.
  Rationale: These are the current runtime compatibility APIs and preserve existing booking behavior.
  Date/Author: 2026-07-28 / Codex.
- Decision: Do not call `check_availability()`, `get_cached_times()`, `ensure_availability_date_cached()`, or any YCLIENTS/YooKassa client from the public endpoint.
  Rationale: Those paths can perform live external API checks or mutate cache; a public catalog request must be read-only.
  Date/Author: 2026-07-28 / Codex.
- Decision: Keep the route unauthenticated but limit output to non-secret business/profile/catalog fields and public integration references.
  Rationale: The frontend needs read access, and YCLIENTS service/staff ids are catalog references, not environment secrets.
  Date/Author: 2026-07-28 / Codex.
- Decision: Add a separate `app.api.server` entrypoint instead of changing `main.py`.
  Rationale: `main.py` must keep polling-mode MaxBot unchanged; public API should be a sibling process for frontend/backend integration.
  Date/Author: 2026-07-28 / Codex.
- Decision: Use explicit local CORS origins by default and no wildcard.
  Rationale: Viksa Online dev server needs browser access from Vite on port 5173, but production should remain deliberate.
  Date/Author: 2026-07-28 / Codex.
- Decision: Store editable catalog drafts in `data/catalog_overrides.local.json` and ignore that file in git.
  Rationale: This is a local-only admin/draft layer; it must not alter `admin_profile.yaml`, secrets, Supabase, booking records, payment state, or YCLIENTS.
  Date/Author: 2026-07-28 / Codex.
- Decision: Gate draft admin endpoints with `CATALOG_ADMIN_LOCAL_ENABLED`.
  Rationale: The standalone API can be used by a local web-admin prototype, but production must remain read-only unless explicitly enabled.
  Date/Author: 2026-07-28 / Codex.

## Outcomes & Retrospective

Completed. MaxBot now exposes `GET /api/public/catalog` both through the MAX webhook FastAPI app and through an independent public API app. The independent app starts with `.\.venv\Scripts\python.exe -m app.api.server` and defaults to `http://127.0.0.1:8090`, so Viksa Online can connect locally while MaxBot remains in polling mode.

The endpoint builds a normalized public catalog from the active profile and read-only availability-cache rows. It returns business identity, service groups, concrete facilities, variants, tariffs, media refs, payment/post-payment public text, YCLIENTS integration refs, rules, warnings, and `availability_state`.

Public media is browser-ready through `media[].publicUrl` and `GET /api/public/media/{media_key}`. Local CORS is explicit for `http://127.0.0.1:5173` and `http://localhost:5173`; no wildcard is configured by default.

The implementation did not change booking flow, payment creation, YCLIENTS record creation, Supabase/Postgres schema, runtime data, `admin_profile.yaml`, `.env`, or secret handling.

Validation passed:

- `.\.venv\Scripts\python.exe -m compileall app main.py scripts tests`
- `.\.venv\Scripts\python.exe tests\public_catalog_smoke.py`
- `.\scripts\validate.ps1 -IncludeSmoke`

The validation output includes a third-party `fastapi.testclient`/Starlette deprecation warning about `httpx`; it does not fail the smoke.

## Context and Orientation

`main.py` initializes storage and starts either MAX or Telegram. The current production path is MAX. In MAX webhook mode, `app.bot.max._run_webhook()` creates a local FastAPI app with `/health` and the MAX webhook POST route.

`business_profile/admin_profile.yaml` is the active non-secret business seed. `app/data/admin_profile.py` loads it and exposes helpers for business profile, services, aliases, media, payment percent, post-payment instructions, duration behavior, and prompt variables. `app/data/services.py` is a compatibility layer used by existing dialog code.

`app/dialog/pricing.py` calculates prices from the service catalog. `app/dialog/availability.py` maps catalog variants to YCLIENTS service/staff ids and validates availability during booking. `app/dialog/availability_cache.py` refreshes and reads cached availability. `app/storage/sqlite.py` owns SQLite/Postgres persistence, including `mvp_availability_cache`.

## Product Rules

The public endpoint must:

- return business identity for `Причал`;
- include the real objects: `Беседка №1`, `Беседка №2`, `Беседка №3`, `Беседка №4`, `Беседка №5`, `Беседка №6`, `Беседка №8`, `Крытая беседка`, `Баня`, `Теплая беседка`, and `Гостевой дом`;
- include media references for configured photos without serving or uploading files;
- include YCLIENTS service/staff ids as integration references;
- represent prices, durations, aliases, capacities, rules, warnings, and post-payment instruction references from the current profile;
- never include environment secrets, provider tokens, payment secret keys, database passwords, or admin credentials;
- represent unavailable cache as `cacheState: "no-data"` and old cache as `cacheState: "stale"`.

## Plan of Work

Create an `app/api/` module with a pure catalog builder and a small FastAPI route registration helper. The builder will normalize profile services into categories, service groups, concrete facilities, variants, tariffs, media, post-payment instructions, and availability state. It will read `mvp_availability_cache` through existing storage helpers only.

Then wire the route registration into `app.bot.max._run_webhook()` after `/health` and before the webhook route. Add a script-style smoke test that uses a temporary `APP_ENV_FILE` and SQLite database, creates the FastAPI test app in memory, and verifies both `no-data` and `stale` availability behavior. Add the new smoke to `scripts/validate.ps1 -IncludeSmoke`.

## Concrete Steps

Work from `D:\AI\max-bot4`.

1. Add `app/api/__init__.py` and `app/api/public_catalog.py`.
2. Import and call the registration helper in `app/bot/max.py`.
3. Add `tests/public_catalog_smoke.py`.
4. Update `scripts/validate.ps1` to include the new smoke.
5. Update this ExecPlan progress and outcomes.

## Validation and Acceptance

Run:

- `.\.venv\Scripts\python.exe -m compileall app main.py scripts tests`
- `.\.venv\Scripts\python.exe tests\public_catalog_smoke.py`
- `.\scripts\validate.ps1 -IncludeSmoke`

Acceptance:

- `GET /api/public/catalog` returns HTTP 200.
- Response includes `business.name == "Причал"`.
- Response includes every required real object listed in Product Rules.
- Response media refs point to existing configured image files.
- Serialized response does not contain secret-looking keys such as token, secret, api_key, password, or configured provider/payment secret variable names.
- Empty cache returns `availability_state.cacheState == "no-data"`.
- Stale cache returns `availability_state.cacheState == "stale"` and does not mark cached rows as confirmed fresh availability.

## Storage / Database Design

No schema changes. The endpoint performs only SELECT-style reads through existing helpers:

- `sqlite.availability_cache_age_seconds()`
- `sqlite.list_availability_rows()`

The test uses a temporary SQLite file and does not touch production `.env` or production data.

## Idempotence and Recovery

The endpoint builder is pure except for read-only storage queries. Re-running the smoke recreates a new temporary SQLite database. If route wiring fails, removing the import and registration call from `app/bot/max.py` restores the previous FastAPI surface. No production data migration or cleanup is involved.

## Artifacts and Notes

Catalog sources found so far:

- Business profile: `business_profile/admin_profile.yaml`, loaded by `app/data/admin_profile.py`.
- Runtime service compatibility: `app/data/services.py`.
- Legacy parity catalog: `app/data/services.yaml`.
- Pricing: `app/dialog/pricing.py`.
- Availability and YCLIENTS payload mapping: `app/dialog/availability.py`.
- Cached availability: `app/dialog/availability_cache.py` and `app/storage/sqlite.py`.
- Media resolution: `app/data/admin_profile.py` and `app/bot/media.py`.
- Payment/prepayment: `app/dialog/payment.py`.
- Post-payment instructions: `app/dialog/post_payment_message.py` and `app/dialog/payment_status.py`.

Implemented artifacts:

- `app/api/public_catalog.py`
- `app/api/__init__.py`
- `app/api/server.py`
- `tests/public_catalog_smoke.py`
- `tests/fixtures/public_catalog.v1.json`
- `scripts/generate_public_catalog_fixture.py`
- `scripts/validate.ps1`
- `app/bot/max.py`
- `.env.example`
- `README.md`

## Interfaces and Dependencies

Internal modules:

- `app.data.admin_profile`
- `app.data.services`
- `app.storage.sqlite`
- `app.bot.max`

External runtime dependencies already present in `requirements.txt`:

- `fastapi`
- `uvicorn`
- `PyYAML`

No new external dependency is planned.
