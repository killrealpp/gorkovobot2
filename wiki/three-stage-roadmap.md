---
title: "Three Stage Roadmap"
created: 2026-07-27
tags:
  - max-bot4
  - roadmap
  - retention
  - yaml
  - rag
---

# Three Stage Roadmap

MaxBot 4 is currently a working bot. Improvements should be additive blocks around the working core, not one broad rewrite.

## Stage 0: Preparation

Status: prepared in `wiki/preparation-execplan-2026-07-27.md`.

Goal: add the development and operations layer from `AI_template` without changing runtime behavior.

Outputs:

- `AGENTS.md`
- `PLANS.md`
- `templates/`
- `wiki/`
- `project_playbook/`
- safe validation scripts
- optional Graphify/Headroom tooling

## Stage 1: Retention Cleanup

Status: complete for implementation and the agreed test Supabase sandbox. Production rollout remains a separate operational step.

Goal: safely understand and clean old runtime data.

Order:

1. Read-only retention report. Status: implemented in `wiki/stage-01-retention-report-execplan-2026-07-27.md`.
2. Dry-run cleanup plan. Status: implemented in `wiki/stage-01-retention-report-execplan-2026-07-27.md`.
3. Guarded apply requiring explicit flags such as `--apply --yes`. Status: implemented, applied to the test Supabase sandbox, not run against production.
4. Optional scheduler disabled by default. Status: implemented through `RETENTION_CLEANUP_ENABLED=true`.

Current test target: Supabase `aws-0-eu-north-1.pooler.supabase.com` is the Stage 1 sandbox. Test cleanup deleted 18 inactive slot holds and 174 past-date availability-cache rows. Post-cleanup report shows zero remaining candidates in those selected groups. For no-human operation, run the bot with `RETENTION_CLEANUP_ENABLED=true` against the selected database.

Important: do not delete bookings automatically. Protect active holds, pending admin notifications, active watchlist rows, payment evidence, and YCLIENTS evidence.

## Stage 2: YAML Universalization

Status: implemented and validated locally in `wiki/stage-02-yaml-universalization-execplan-2026-07-27.md`.

Goal: move non-secret business data into one editable business profile while keeping backend rules authoritative.

The current active non-secret business source is `business_profile/admin_profile.yaml`. `app/data/services.py` keeps the old service API shape, and `app/data/services.yaml` remains a legacy parity reference.

Implemented areas:

- profile-backed service catalog, variants, prices, capacities, durations, cutoffs, and YCLIENTS ids;
- profile-backed media catalog for MAX and Telegram;
- profile-backed booking questions, upsell settings, payment percent, post-payment text, cancellation policy, and knowledge source;
- prompt variable rendering through `app/data/admin_profile.py`;
- smoke coverage in `tests/admin_profile_smoke.py`.

Secrets and runtime flags stay in `.env`.

## Stage 3: Approved-Only RAG

Goal: improve FAQ and policy answers through approved knowledge without letting RAG decide prices, availability, payment, holds, or YCLIENTS.

SQL must be the source of truth for candidates, approved items, audit log, and index state. Qdrant should be optional and rebuildable. Raw customer messages must not be indexed directly.

## Ready To Start Stage 3

Stage 1 code is implemented and validated locally. Stage 2 code is implemented and validated locally. Production cleanup has not been run, and production profile rollout still requires a reviewed live-worker switch. The next implementation stage is approved-only RAG.
