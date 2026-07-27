---
title: "Dialog Flow"
created: 2026-07-27
tags:
  - max-bot4
  - dialog
  - ai
---

# Dialog Flow

## Responsibility Split

AI helps understand user text and write replies. Backend code remains authoritative for:

- service and field validation;
- price calculation;
- availability checks;
- local slot holds;
- payment link creation;
- payment status sync;
- YCLIENTS record creation and updates;
- admin notifications and manual-review states.

## Main Files

- `app/dialog/state.py`: `BookingDraft` fields and `next_step()` order.
- `app/dialog/engine.py`: main dialog state machine, confirmation, payment handoff, reschedule/cancel flows, watchlist handling, and fallback guards.
- `app/dialog/pricing.py`: deterministic booking price calculation from the profile-backed service catalog.
- `app/dialog/availability.py`: live YCLIENTS availability checks and payload creation.
- `app/dialog/availability_cache.py`: cached availability for user-facing answers.
- `app/dialog/payment.py`: YooKassa prepayment creation.
- `app/dialog/payment_status.py`: payment polling and post-payment YCLIENTS record creation.
- `app/ai/parser.py`: AI payload assembly, parser/router/finalizer/media decisions.

## Universalization Boundary

The current booking shape is fixed in code: service, date, optional variant, time, duration, guest count, upsells, name, phone, confirmation, payment. Stage 2 makes configured business values editable in `business_profile/admin_profile.yaml`, but new fields such as specialist, room, treatment zone, contraindications, subscriptions, or complex resource choice still require backend code, parser contract, storage, and tests.

Profile-backed flow pieces now include `BookingDraft.next_step()`, start text, step questions, upsell enablement, media lookup, price calculation, prepayment percent, post-payment messages, and prompt variables.
