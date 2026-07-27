# Repeatable Stages

## 0. Preparation

- Read `AGENTS.md`, `PLANS.md`, and `wiki/index.md`.
- Check `git status --short --branch`.
- Identify current runtime tables, prompts, business catalog, payment flow, YCLIENTS flow, and tests.
- Run compile-only validation.

## 1. Retention Cleanup

- Add read-only report first.
- Add dry-run cleanup second.
- Add guarded apply third.
- Add scheduler only after guarded apply is validated.
- Keep bookings and critical payment/YCLIENTS evidence protected.

## 2. YAML Universalization

- Add `business_profile/admin_profile.yaml`.
- Keep `.env` for secrets and runtime flags.
- Keep old service APIs as compatibility wrappers.
- Convert prompts to templates rendered from the profile.
- Move media, payment text, post-payment text, FAQ, and services into the profile where safe.

## 3. Approved-Only RAG

- Add SQL-backed candidates and approved items.
- Require admin approval.
- Keep Qdrant optional.
- Use retrieval only as FAQ/policy context.
- Never let retrieved text override backend decisions.

## Validation Pattern

Run focused tests after each stage, then compile:

```powershell
.\scripts\validate.ps1
```

Use temporary smoke isolation:

```powershell
.\scripts\validate.ps1 -IncludeSmoke
```
