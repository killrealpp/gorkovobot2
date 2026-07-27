# Three Stage Improvement Prompt For MaxBot 4

This file adapts the MaxBot 2 improvement pattern to `D:\AI\max-bot4`.

The order is:

0. Preparation layer from `AI_template`.
1. Retention cleanup.
2. YAML universalization.
3. Approved-only RAG.

The rule is additive work around the current working bot. Do not rewrite the whole bot, do not move runtime code into `AI_template/src`, and do not mix cleanup, business profile, and RAG in one large change.

## Current MaxBot 4 Baseline

Current runtime tables in `app/storage/sqlite.py`:

- `mvp_conversations`
- `mvp_messages`
- `mvp_bookings`
- `mvp_slot_holds`
- `mvp_system_logs`
- `mvp_admin_notifications`
- `mvp_availability_cache`
- `mvp_availability_watchlist`

Current active business source:

- `business_profile/admin_profile.yaml`
- `business_profile/knowledge.md`
- profile-rendered prompt variables through `app/data/admin_profile.py`
- compatibility service API in `app/data/services.py`

Known risks before Stage 1:

- The old local SQLite DDL may differ from the test Supabase schema.
- The agreed test Supabase schema already includes `mvp_availability_watchlist.platform` and `mvp_processed_updates`.
- `app/knowledge/` and `app/runtime/` are not active yet.
- Smoke tests must use temporary `APP_ENV_FILE` and SQLite, not production `.env`.
- Legacy internal smoke scripts import old private dialog-engine helpers and require explicit `-IncludeLegacySmoke`.

## Stage 0: Preparation

Goal: make the repository ready for safe staged changes.

Add:

- `AGENTS.md`
- `PLANS.md`
- `templates/`
- `wiki/`
- `project_playbook/`
- safe validation scripts

Do not change booking business behavior. Minimal compile/import repairs are allowed only when they unblock baseline validation.

## Stage 1: Retention Cleanup

Goal: safely understand and clean old runtime data.

Current status: read-only retention report, dry-run cleanup plan, guarded apply, automatic scheduler, and test-Supabase wrappers are implemented. Guarded apply requires `--apply --yes`; production cleanup has not been run. The agreed test Supabase cleanup has been applied and verified.

Follow the MaxBot 2 pattern:

- add `app/maintenance/retention.py`;
- add `scripts/retention_report.py`;
- add `scripts/retention_cleanup.py`;
- keep report read-only;
- keep cleanup dry-run by default;
- require explicit `--apply --yes` for real cleanup;
- use `--max-rows`;
- re-select candidate rows inside the apply transaction;
- never delete protected bookings automatically;
- protect active holds, pending admin notifications, active watchlist rows, payment ids, and YCLIENTS record ids.

Stage 1 must also decide whether to add `mvp_processed_updates` and fix the watchlist `platform` column before cleanup report coverage.
For the agreed test Supabase target, those schema pieces already exist. Production must still be inspected separately before enabling automatic apply there.

## Stage 2: YAML Universalization

Goal: move non-secret business data into a profile while keeping backend decisions deterministic.

Current status: implemented locally. `business_profile/admin_profile.yaml` is active for editable non-secret business data; `app/data/services.py` preserves the existing service API; profile smoke validation passes.

Important parity rule: Stage 2 must preserve the old working bot behavior. `tests/stage2_behavior_parity_smoke.py` protects the key behavior surface: next-step order, old service titles, bathhouse capacity guard, prices, 50% prepayment, media resolution, post-payment text, and YCLIENTS comment text.

Follow the MaxBot 2 pattern:

- create `business_profile/admin_profile.yaml`;
- create `app/data/admin_profile.py`;
- keep `.env` for secrets and runtime flags;
- preserve compatibility APIs in `app/data/services.py`;
- render prompt templates from profile variables;
- move media, payment wording, post-payment text, FAQ, services, prices, capacities, and YCLIENTS ids into the profile where safe.

YAML must not become a programming language. New booking fields or complex resource/payment logic require backend code, parser contracts, storage, and tests.

## Stage 3: Approved-Only RAG

Goal: improve FAQ answers through approved knowledge without letting RAG own critical decisions.

Follow the MaxBot 2 pattern:

- add `app/knowledge/`;
- SQL owns candidates, approved items, audit log, and index state;
- Qdrant is optional and rebuildable;
- raw client messages are not indexed directly;
- admin must approve or reject candidates;
- retrieval is enabled only through feature flags;
- retrieved context is advisory only.

RAG must not decide prices, availability, payment, holds, booking confirmation, or YCLIENTS writes.

## Safe Validation

Use:

```powershell
.\scripts\validate.ps1
```

Use isolated smoke checks only:

```powershell
.\scripts\validate.ps1 -IncludeSmoke
```

Current status: Stage 0, Stage 1, and Stage 2 are implemented locally. `.\scripts\validate.ps1`, `.\scripts\validate.ps1 -IncludeSmoke`, and `.\scripts\wiki-check.ps1` pass. Legacy smoke drift is documented separately.
