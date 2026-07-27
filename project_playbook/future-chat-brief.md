# Future Chat Brief

MaxBot 4 is a Python booking bot for MAX with Telegram fallback. It handles booking flow, availability, holds, YooKassa prepayment, YCLIENTS records, admin notifications, voice messages, and watchlist behavior.

The backend is the source of truth for booking, prices, availability, holds, payments, and YCLIENTS. AI parses and writes replies but must not make critical decisions.

The current preparation objective is to ready the repository for three additive improvements:

1. Retention cleanup.
2. YAML universalization.
3. Approved-only RAG.

Important current files:

- `AGENTS.md`
- `PLANS.md`
- `wiki/index.md`
- `wiki/preparation-execplan-2026-07-27.md`
- `project_playbook/three-stage-improvement-prompt.md`
- `main.py`
- `app/storage/sqlite.py`
- `app/dialog/engine.py`
- `app/dialog/state.py`
- `app/data/services.yaml`
- `app/ai/parser.py`
- `app/ai/admin_prompt.md`
- `app/ai/router_prompt.md`

Before any code changes:

- run `git status --short --branch`;
- do not print `.env`;
- do not mutate production DB;
- do not commit, push, or open PR unless explicitly asked;
- create or update an ExecPlan for storage, payment, YCLIENTS, prompt, cleanup, YAML, or RAG work.
