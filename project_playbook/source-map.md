# Source Map

## Preparation Layer

- `AGENTS.md` - repository working rules.
- `PLANS.md` - ExecPlan rules.
- `wiki/index.md` - wiki entry point.
- `wiki/preparation-execplan-2026-07-27.md` - current preparation plan.
- `scripts/validate.ps1` - compile and wiki validation.
- `scripts/wiki-check.ps1` - wiki structure check.

## Runtime Entry

- `main.py` - initializes settings, storage, and selected channel.
- `app/core/config.py` - environment-backed settings.
- `app/bot/max.py` - MAX adapter.
- `app/bot/telegram.py` - Telegram adapter.

## Storage

- `app/storage/sqlite.py` - SQLite/Postgres schema and persistence helpers.
- `app/storage/yclients_records_repo.py` - YCLIENTS sync storage helpers.

## Business And Dialog

- `app/data/services.yaml` - active service catalog.
- `app/data/services.py` - service loading helpers.
- `app/dialog/state.py` - booking draft model.
- `app/dialog/engine.py` - main booking flow.
- `app/dialog/pricing.py` - deterministic price calculation.
- `app/dialog/availability.py` - live availability and YCLIENTS payloads.
- `app/dialog/availability_cache.py` - cached availability for answers.
- `app/dialog/payment.py` - YooKassa payment link creation.
- `app/dialog/payment_status.py` - payment polling and YCLIENTS creation after payment.
- `app/dialog/watchlist.py` - availability watchlist.
- `app/dialog/admin_notify.py` - admin notification formatting.

## AI

- `app/ai/admin_prompt.md` - main answer prompt.
- `app/ai/router_prompt.md` - router prompt.
- `app/ai/json_contract.yaml` - structured output contract.
- `app/ai/parser.py` - AI request assembly and parsing.
- `app/ai/knowledge.md` - static knowledge context.

## Future Stage Targets

- future `app/maintenance/` - retention report and cleanup.
- future `business_profile/` - active business profile.
- future `app/knowledge/` - approved-only RAG.
