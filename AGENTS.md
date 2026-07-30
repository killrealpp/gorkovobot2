# MaxBot 4 Agent Guide

Read this file first in every new Codex session for this repository.

When a task mentions Obsidian, LLM Wiki, markdown knowledge bases, source ingest,
wiki linting, durable memory, or cross-linked notes, also read
`llm_wiki/index.md` and follow the linked `llm_wiki/schema.md` workflow. That
folder is a documentation/workflow layer only; it must not override MaxBot
runtime rules, booking/payment/YCLIENTS ownership, or secret-handling rules.

## Project Summary

MaxBot 4 is a compact Python booking bot for MAX with Telegram kept as a fallback channel. The backend owns the booking scenario, availability checks, temporary holds, payments, YCLIENTS records, admin notifications, voice transcription, and watchlist behavior. AI is used as a controlled parser and response helper; it must not become the source of truth for bookings, prices, availability, payments, holds, or records.

The current default production channel is MAX through `CLIENT_CHANNELS=max`.

## Source Of Truth

Use current code as the first source of truth. Durable project memory is being introduced in this preparation block:

- `AGENTS.md` defines how Codex should work in this repository.
- `PLANS.md` defines the ExecPlan workflow for large or risky work.
- `wiki/index.md` is the wiki entry point.
- `llm_wiki/index.md` is the root-level LLM Wiki pattern entry point for
  Obsidian/markdown knowledge-base work.
- `project_playbook/three-stage-improvement-prompt.md` records the three-stage improvement direction for this repository.
- `README.md` is the short operator-facing overview.
- `app/data/services.yaml` is still the active service, price, capacity, staff, and YCLIENTS id catalog.
- `app/dialog/engine.py` is the main booking state machine.
- `app/ai/json_contract.yaml`, `app/ai/router_prompt.md`, `app/ai/admin_prompt.md`, and `app/ai/knowledge.md` define the AI contract and prompt context.

Root-level dated fix notes are historical implementation notes. Use them for context, but prefer current code, wiki, and the active ExecPlan when there is a conflict.

## Technology Stack

- Python with type hints.
- `aiogram` for Telegram polling.
- `httpx` for external HTTP calls.
- `pydantic-settings` for environment configuration.
- `PyYAML` for service and dialog data.
- SQLite for local state by default, with Postgres support through `psycopg2`.
- FastAPI/Uvicorn for MAX webhook mode.
- OpenAI-compatible chat completions, usually through OpenRouter.
- YooKassa for prepayment links.
- YCLIENTS for schedule, record creation, and record sync.

## Project Structure

- `main.py` selects the channel from `CLIENT_CHANNELS`, initializes storage, and starts the selected bot.
- `app/core/` contains settings and date helpers.
- `app/bot/` contains Telegram and MAX adapters plus media helpers.
- `app/dialog/` contains booking state, pricing, availability, payment status, post-payment text, watchlist, and the dialog engine.
- `app/ai/` contains parser, prompts, response sanitizer, rate limiting, voice transcription, and AI helpers.
- `app/integrations/` contains YCLIENTS, YCLIENTS sync, and YooKassa clients.
- `app/storage/` contains SQLite/Postgres persistence helpers.
- `app/data/` contains YAML-backed service and policy data.
- `scripts/` contains operational helpers.
- `tests/` contains script-style smoke checks.
- `wiki/` contains durable project knowledge maintained by Codex.
- `llm_wiki/` contains an interlinked Markdown guide for the LLM Wiki /
  Obsidian-style knowledge-base workflow.
- `project_playbook/` contains portable stage guidance for the three planned improvements.
- Optional dev tooling includes Graphify wrapper/status scripts under `scripts/` and direct Headroom CLI checks. These are not runtime dependencies.

## Commands

Use PowerShell from the repository root.

Install dependencies:

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

Compile baseline:

```powershell
python -m compileall app main.py scripts tests
```

Project validation:

```powershell
.\scripts\validate.ps1
```

Wiki validation:

```powershell
.\scripts\wiki-check.ps1
```

Tooling status:

```powershell
.\scripts\tooling-status.ps1 -SkipValidation
```

Graphify and Headroom are optional development tools. Missing binaries are not bot runtime failures.

Script-style smoke tests must be run only with a temporary `APP_ENV_FILE` and temporary SQLite database unless the current task explicitly targets the live environment. Do not run smoke tests against the production `.env`.

## Environment And Secrets

The user owns real secrets in `.env`. Do not print secret values. It is acceptable to mention variable names such as `MAX_BOT_TOKEN`, `OPENROUTER_API_KEY`, `YCLIENTS_PARTNER_TOKEN`, or `PAYMENT_SECRET_KEY`.

Never commit local databases, logs, sessions, `.env`, generated caches, or runtime files. Do not push, create pull requests, or deploy unless the user explicitly asks.

## Development Rules

- Inspect the repository and `git status --short --branch` before edits.
- Preserve existing project conventions and narrow fixes to the requested behavior.
- Do not create commits, branches, remotes, pushes, or history rewrites unless the user explicitly asks.
- Do not run destructive filesystem or database commands without explicit confirmation.
- Keep business rules deterministic in backend code. AI may parse or phrase, but code validates and decides.
- Update docs and tests when changing dialog state, payments, availability, AI contracts, storage schema, or integrations.
- Keep MAX and Telegram adapters thin. Shared behavior belongs in `app/dialog/`, `app/ai/`, `app/storage/`, or a future `app/runtime/` layer.

## ExecPlan Workflow

Use `PLANS.md` for large tasks before implementation, especially when changing:

- booking flow or state transitions;
- payment behavior;
- availability or hold rules;
- YCLIENTS sync or record creation;
- AI JSON contracts or prompts;
- storage schema;
- retention cleanup;
- RAG or runtime knowledge;
- security, secrets, deployment, or webhooks.

ExecPlans are living documents. Keep `Progress`, `Surprises & Discoveries`, `Decision Log`, and `Outcomes & Retrospective` updated while working.

## Known Preparation Risks

These are documented baseline risks before Stage 1:

- `app/storage/sqlite.py` writes and queries `mvp_availability_watchlist.platform`, but current DDL does not create that column.
- `mvp_processed_updates` does not exist yet, while the MaxBot 2 retention reference includes it.
- `business_profile/`, `app/maintenance/`, `app/knowledge/`, and `app/runtime/` are not active in MaxBot 4 yet.
- Smoke checks need temporary SQLite isolation to avoid production side effects.

## Graphify And Headroom

Graphify is for code relationship analysis. Use `scripts/graphify-build.ps1 -Status` to inspect the current graph, `-AstOnly` for a local graph update, and semantic mode only when API keys are intentionally available.

Headroom is for compressing large logs, diffs, graph reports, and noisy context. The repo currently documents direct CLI checks (`headroom --version`, `headroom doctor`) rather than tracked Headroom control wrappers. A running proxy is not the same as a routed Codex session; restore a dedicated wrapper in a tooling cleanup before documenting a routed Codex launch script again.

Do not add Graphify or Headroom as runtime dependencies of the bot.
