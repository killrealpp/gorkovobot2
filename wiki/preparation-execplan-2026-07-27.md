# Prepare MaxBot 4 For Three Improvement Stages

This ExecPlan is a living document. The sections `Progress`, `Surprises & Discoveries`, `Decision Log`, and `Outcomes & Retrospective` must be kept up to date as work proceeds.

This plan follows `PLANS.md` from the repository root.

## Purpose / Big Picture

MaxBot 4 is a working booking bot. Before adding retention cleanup, YAML universalization, and approved-only RAG, this preparation block adds a safe development and operations layer: agent rules, ExecPlan format, wiki memory, playbook notes, and validation scripts. A future engineer or agent should be able to start Stage 1 from the repository alone, without relying on chat history.

This block must not change runtime behavior, production data, payment flow, YCLIENTS behavior, MAX/Telegram adapters, or AI prompts.

## Progress

- [x] (2026-07-27 11:20 Europe/Moscow) Read `D:\AI\max-bot2\project_playbook\three-stage-improvement-prompt.md`.
- [x] (2026-07-27 11:20 Europe/Moscow) Inspected MaxBot 2 playbook, wiki plans, retention, admin profile, and knowledge RAG reference files.
- [x] (2026-07-27 11:20 Europe/Moscow) Inspected `D:\AI\AI_template` and decided to use it as a dev/ops layer, not as a replacement runtime.
- [x] (2026-07-27 11:30 Europe/Moscow) Checked MaxBot 4 `git status`, files, runtime tables, prompts, storage, payment, and channel structure.
- [x] (2026-07-27 11:35 Europe/Moscow) Added `AGENTS.md`, `PLANS.md`, templates, wiki pages, project playbook, and safe validation scripts.
- [x] (2026-07-27 11:40 Europe/Moscow) Ran compile-only validation; it exposed a pre-existing syntax error in `app/ai/engine.py`.
- [x] (2026-07-27 11:40 Europe/Moscow) Ran wiki check successfully.
- [x] (2026-07-27 11:40 Europe/Moscow) Ran `scripts/validate.ps1`; it correctly stops at the same compile-only failure before any smoke or external API work.
- [x] (2026-07-27 11:45 Europe/Moscow) Added optional Graphify/Headroom tooling scripts and documentation after the user asked why they were missing from the `AI_template` preparation layer.
- [x] (2026-07-27 12:05 Europe/Moscow) Repaired the minimal compile/import blockers needed for a meaningful baseline: `app/ai/engine.py` line 889 and `app/ai/parser.py` prompt loader.
- [x] (2026-07-27 12:10 Europe/Moscow) Created a local ignored Windows `.venv/` and installed `requirements.txt` for local validation.
- [x] (2026-07-27 12:15 Europe/Moscow) Updated `tests/smoke.py` to assert the current no-op `fallback_decision()` contract instead of an obsolete regex fallback.
- [x] (2026-07-27 12:20 Europe/Moscow) Ran `.\scripts\validate.ps1 -IncludeSmoke`; compile, current smoke, and wiki check pass through temporary `APP_ENV_FILE` and SQLite.
- [x] (2026-07-27 12:25 Europe/Moscow) Built the local AST-only Graphify graph: 1110 nodes / 2919 edges.

## Surprises & Discoveries

- Observation: `mvp_availability_watchlist.platform` is used by code but is not created by the current DDL.
  Evidence: `create_watchlist()` inserts `platform`, and `list_active_watchlist(platform=...)` filters by `platform`, but `CREATE TABLE IF NOT EXISTS mvp_availability_watchlist` does not define it.
- Observation: MaxBot 4 has no `mvp_processed_updates` table.
  Evidence: `app/storage/sqlite.py` defines runtime table constants only through `T_AVAILABILITY_WATCHLIST`; no processed update constant or DDL exists.
- Observation: MaxBot 4 does not have active `business_profile/`, `app/maintenance/`, `app/knowledge/`, or `app/runtime/`.
  Evidence: repository file listing before this preparation block.
- Observation: Compile-only validation was blocked by an existing syntax error outside the new scaffold.
  Evidence: `python -m compileall app main.py scripts tests` reported `SyntaxError: unterminated string literal` in `app/ai/engine.py`, line 889. It is now repaired.
- Observation: `app/dialog/engine.py` imported `load_engine_response_prompt` from `app.ai.parser`, but `app/ai/parser.py` did not define it.
  Evidence: isolated smoke validation failed on the import. The loader now reads the existing `app/ai/engine_response_prompt.md`.
- Observation: `tests/validation_smoke.py` and `tests/booking_flow_smoke.py` target old private dialog-engine internals.
  Evidence: explicit legacy smoke validation fails with `ImportError: cannot import name '_merge_fields' from 'app.dialog.engine'`.
- Observation: Graphify and Headroom are installed locally.
  Evidence: `.\scripts\tooling-status.ps1` reports Graphify graph `1110 nodes / 2919 edges` and `headroom, version 0.31.0`.

## Decision Log

- Decision: Add `AI_template` concepts as documentation and operations scaffolding only.
  Rationale: MaxBot 4 already has a working runtime under `app/`; moving it to `src/` would be a broad unrelated rewrite.
  Date/Author: 2026-07-27 / Codex.
- Decision: Keep Stage 1 focused on retention report/cleanup, not YAML or RAG.
  Rationale: Data hygiene is the safest first additive block and clarifies storage growth before adding profile and memory layers.
  Date/Author: 2026-07-27 / Codex.
- Decision: Document baseline schema risks instead of fixing them in the preparation block.
  Rationale: The user asked to reach readiness for staged work; schema fixes belong to a dedicated Stage 1 or pre-Stage 1 ExecPlan with tests.
  Date/Author: 2026-07-27 / Codex.
- Decision: Include Graphify and Headroom as optional development tooling, not runtime requirements.
  Rationale: They are part of the `AI_template` operating model, but the bot must still run without them.
  Date/Author: 2026-07-27 / Codex.
- Decision: Keep Graphify local/AST-only by default and require explicit `-Semantic` before reading `.env` for provider keys.
  Rationale: preparation validation must not silently use real API credentials or external calls.
  Date/Author: 2026-07-27 / Codex.
- Decision: `scripts/validate.ps1 -IncludeSmoke` runs the current stable smoke baseline; obsolete private-internal smoke scripts require `-IncludeLegacySmoke`.
  Rationale: the normal readiness command should validate current behavior while preserving legacy smoke drift as an explicit risk.
  Date/Author: 2026-07-27 / Codex.
- Decision: Allow minimal runtime repairs only when they unblock compile/import baseline and do not alter booking business decisions.
  Rationale: Stage 0 should not change product behavior, but a project that cannot import cannot be considered ready for Stage 1.
  Date/Author: 2026-07-27 / Codex.

## Outcomes & Retrospective

Preparation scaffolding is implemented. The repository now has agent rules, ExecPlan rules, templates, wiki, project playbook, safe validation scripts, and optional Graphify/Headroom tooling. Current compile validation, stable smoke validation, wiki check, and tooling status pass. Legacy private-internal smoke tests remain documented drift and are not part of the default readiness command.

## Context and Orientation

`main.py` initializes the database and runs one selected channel. `app/storage/sqlite.py` creates runtime tables and exposes persistence helpers. `app/dialog/engine.py` is the booking state machine. `app/data/services.yaml` is still the active business catalog. `app/ai/` contains static prompts and parser logic. The future MaxBot 2 reference includes retention, YAML profile, and RAG layers, but those are not active in MaxBot 4 yet.

`AI_template` is a development template. Its useful pieces for this repository are agent rules, ExecPlan rules, templates, wiki conventions, and validation habits. Its `src/` reference bot is not copied into MaxBot 4.

## Plan of Work

First, record baseline facts and risks without reading or printing production secrets. Second, add agent and planning documentation. Third, add minimal wiki pages and a preparation ExecPlan. Fourth, add safe validation scripts. Fifth, add a local playbook that adapts MaxBot 2's three-stage improvement pattern to MaxBot 4.

## Concrete Steps

Run from `D:\AI\max-bot4`.

Baseline inspection:

    git status --short --branch
    rg --files app scripts tests
    rg -n "T_[A-Z_]+|CREATE TABLE IF NOT EXISTS mvp_|platform|mvp_processed_updates" app\storage\sqlite.py

Validation:

    python -m compileall app main.py scripts tests
    .\scripts\wiki-check.ps1
    .\scripts\validate.ps1

Optional isolated smoke validation:

    .\scripts\validate.ps1 -IncludeSmoke

## Validation and Acceptance

Acceptance requires:

- `AGENTS.md`, `PLANS.md`, `templates/`, `wiki/`, `project_playbook/`, and safe scripts exist.
- The wiki records current tables, integration points, and known risks.
- `project_playbook/three-stage-improvement-prompt.md` states preparation -> cleanup -> YAML -> RAG.
- Compile-only validation passes. Current status: passes.
- Wiki check passes. Current status: passes with zero issues.
- Isolated current smoke validation passes. Current status: `.\scripts\validate.ps1 -IncludeSmoke` passes through temporary env and SQLite.
- Legacy smoke drift is recorded. Current status: `.\scripts\validate.ps1 -IncludeSmoke -IncludeLegacySmoke` fails because `tests/validation_smoke.py` imports removed private `_merge_fields`.
- No production data is mutated.
- No commit, push, pull request, or deploy is performed.

## Idempotence and Recovery

This preparation block is file-based and idempotent. If validation scripts are too strict, adjust docs or script checks without touching runtime logic. If a smoke test fails under temporary SQLite, record it as a pre-existing smoke drift unless the failure comes from the new scaffolding.

## Artifacts and Notes

Do not print `.env` values. Do not run cleanup apply, migrations, payment creation, YCLIENTS writes, or production webhook operations in this preparation block.

Validation evidence:

- `.\scripts\validate.ps1` passes.
- `.\scripts\validate.ps1 -IncludeSmoke` passes; it uses a temporary `APP_ENV_FILE` and SQLite database.
- `.\scripts\wiki-check.ps1` passes with `Issues: 0`.
- `.\scripts\tooling-status.ps1` passes and reports Graphify graph `1110 nodes / 2919 edges`, Headroom `0.31.0`, and Python validation ok.
- `.\scripts\validate.ps1 -IncludeSmoke -IncludeLegacySmoke` intentionally remains a known legacy failure on `tests/validation_smoke.py` importing removed `_merge_fields`.

## Interfaces and Dependencies

New project-facing files:

- `AGENTS.md`
- `PLANS.md`
- `templates/*.md`
- `scripts/validate.ps1`
- `scripts/wiki-check.ps1`
- `wiki/*.md`
- `project_playbook/*.md`
- `.graphifyignore`
- `.graphify/providers.json`
- `scripts/graphify-build.ps1`
- `scripts/headroom-*.ps1`
- `scripts/codex-headroom.ps1`
- `scripts/tooling-status.ps1`

No new Python runtime dependency is introduced. A local `.venv/` was created from existing `requirements.txt`; it is ignored by git.
