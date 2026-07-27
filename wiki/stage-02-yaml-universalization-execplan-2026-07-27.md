---
title: "Stage 02 YAML Universalization ExecPlan"
created: 2026-07-27
tags:
  - max-bot4
  - execplan
  - yaml
  - business-profile
---

# Stage 02 YAML Universalization ExecPlan

## Goal

Move editable, non-secret business data into `business_profile/admin_profile.yaml` while preserving the current MaxBot 4 runtime behavior.

This stage must not mutate production data, create real YooKassa payments, write YCLIENTS records, commit, push, or deploy.

## Scope

Implemented profile-backed areas:

- service catalog, variants, prices, capacities, durations, business-day cutoffs, and YCLIENTS service/staff ids;
- booking start text, next-step questions, required field order, and upsell settings;
- media catalog and object-to-photo mapping for MAX and Telegram;
- prepayment percent, payment wording, post-payment messages, cancellation policy, and video instructions;
- prompt variable rendering and combined knowledge context;
- compatibility API in `app/data/services.py` so existing imports continue to work.

Out of scope:

- new booking fields;
- new storage tables;
- real production profile switching;
- RAG indexing or retrieval;
- changing secrets, tokens, passwords, or production database rows.

## Implementation Notes

`business_profile/admin_profile.yaml` is now the active non-secret profile. `ADMIN_PROFILE_PATH` can override it, but secrets and runtime flags remain in `.env`.

`app/data/admin_profile.py` owns profile loading, validation, prompt rendering helpers, service rules, media lookup, payment percent lookup, and profile knowledge assembly.

`app/data/services.py` remains as a compatibility layer. `load_services()`, `service_title()`, `service_variants()`, `variant_by_title()`, and `normalize_service_type()` now read through the business profile.

Backend logic remains authoritative. YAML can describe configured rules, but it does not execute arbitrary business logic.

## Validation

Passed:

```powershell
.\.venv\Scripts\python.exe -m compileall app main.py scripts tests
.\.venv\Scripts\python.exe tests\admin_profile_smoke.py
.\.venv\Scripts\python.exe tests\stage2_behavior_parity_smoke.py
```

The smoke verifies:

- the active profile exists and has required sections;
- profile services preserve the legacy `app/data/services.yaml` service ids, staff ids, prices, capacities, durations, and cutoffs;
- prompt templates render without leftover `{{...}}` variables;
- profile knowledge includes the copied detailed knowledge file;
- all configured media files exist;
- booking next steps remain compatible for gazebo and bathhouse flows;
- bathhouse 8-hour availability maps to the 7-hour YCLIENTS service;
- extra-hour pricing actually changes when the profile price is changed in memory;
- prepayment percent and post-payment video text come from the profile.

The parity smoke verifies that the old working behavior is preserved for:

- booking next-step order;
- `/start` and fallback step questions;
- compatibility `service_title()` values;
- bathhouse capacity guard at 15 people;
- representative gazebo, bathhouse, and house prices;
- 50% prepayment amounts;
- media key/path resolution;
- post-payment customer text;
- YCLIENTS booking comment text.

## Known Boundaries

- `app/ai/admin_prompt.md` still contains project-specific business guard text. Stage 2 renders profile variables into prompts, but it does not rewrite every prompt rule into generic YAML.
- `app/dialog/engine.py` still contains object-specific safety guards for bathhouse, house, warm gazebo, and gazebos. These are intentional backend rules and should only be generalized when covered by focused tests.
- `app/data/services.yaml` remains as a legacy reference and parity baseline, not the primary active runtime source. One intentional profile value follows old runtime behavior instead of the stale YAML value: bathhouse capacity is 15 because the old engine guard enforced 15.
- Production rollout should review the exact `business_profile/admin_profile.yaml` content before enabling this branch on a live worker.

## Result

Stage 2 is implemented and validated locally. The project is ready to start Stage 3 planning and implementation: approved-only RAG.
