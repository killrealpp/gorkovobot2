---
title: "MaxBot 4 Wiki Index"
created: 2026-07-27
tags:
  - max-bot4
  - index
---

# MaxBot 4 Wiki Index

This wiki is the durable project memory for MaxBot 4. It was added as a preparation layer before implementing three improvements: retention cleanup, YAML universalization, and approved-only RAG.

## Start Here

- [[project-overview]] - what the bot does and which files own the main behavior.
- [[integrations-and-storage]] - runtime tables, SQLite/Postgres, YCLIENTS, YooKassa, MAX, Telegram, and known schema risks.
- [[dialog-flow]] - current booking flow and AI/backend responsibility split.
- [[quality-and-operations]] - safe validation commands and rules for avoiding production side effects.
- [[three-stage-roadmap]] - preparation, retention cleanup, YAML profile, and approved-only RAG roadmap.
- [[tooling-memory-stack]] - optional Graphify/Headroom/wiki tooling layer.
- [[preparation-execplan-2026-07-27]] - the active preparation ExecPlan.
- [[stage-01-retention-report-execplan-2026-07-27]] - Stage 1 read-only retention report and dry-run cleanup ExecPlan.
- [[stage-02-yaml-universalization-execplan-2026-07-27]] - Stage 2 business profile/YAML universalization ExecPlan.
- [[public-catalog-api-execplan-2026-07-28]] - read-only public catalog API for external frontends.
- [[log]] - chronological wiki maintenance log.

## Active Boundary

The current runtime still lives in `app/`. Stage 2 adds `business_profile/admin_profile.yaml` as the editable non-secret business profile, while backend decisions remain in `app/`.
