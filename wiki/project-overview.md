---
title: "Project Overview"
created: 2026-07-27
tags:
  - max-bot4
  - overview
---

# Project Overview

MaxBot 4 is a Python booking bot. The current production-oriented path uses MAX, with Telegram kept as a fallback channel. AI helps parse messages and produce customer-facing wording, while backend code owns booking decisions.

## Current Runtime Shape

- `main.py` initializes settings, calls `init_db()`, selects one channel from `CLIENT_CHANNELS`, and runs either `app.bot.max.run_bot()` or `app.bot.telegram.run_bot()`.
- `app/dialog/engine.py` owns the booking state machine and most deterministic guard logic.
- `app/dialog/state.py` defines `BookingDraft`, the current fixed booking data shape.
- `business_profile/admin_profile.yaml` is the active editable non-secret business profile with services, prices, capacities, durations, media, payment wording, and YCLIENTS ids.
- `app/data/services.yaml` remains a legacy parity reference for the original service catalog.
- `app/ai/admin_prompt.md`, `app/ai/router_prompt.md`, `app/ai/json_contract.yaml`, and `app/ai/parser.py` define the AI behavior surface.
- `app/storage/sqlite.py` owns SQLite/Postgres schema and persistence helpers.

## Current Non-Goals

The project still does not move the bot into `AI_template/src`. Retention cleanup and YAML profile loading are implemented; approved-only RAG is not implemented yet.

## Direction

The project is being prepared for three additive stages:

1. Safe retention report and cleanup.
2. Business universalization through a non-secret YAML profile.
3. Approved-only RAG with SQL as the source of truth and optional Qdrant indexing.
