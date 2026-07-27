# AI Context, Profile, And RAG Notes

MaxBot 4 should follow the same safety boundary as MaxBot 2:

- AI parses and phrases.
- Backend code owns prices, availability, holds, payment, YCLIENTS, and booking status.
- Profile data can describe supported business facts and simple validated rules.
- RAG can provide approved FAQ context, not critical authority.

## Before YAML

Measure and simplify prompt context only after Stage 1 is complete. Current prompts still contain recreation-base wording and service keys directly.

## YAML Boundary

`business_profile/admin_profile.yaml` should control non-secret business content. It must not store tokens, API keys, database passwords, payment secrets, or arbitrary executable rules.

Unsupported fields require code changes:

- specialist;
- room;
- master gender;
- treatment zone;
- contraindications;
- subscription/package logic;
- complex resource choice;
- payment flow changes.

## RAG Boundary

Approved-only flow:

1. Bot or admin creates a candidate.
2. Admin approves or rejects it.
3. Approved item is stored in SQL.
4. Optional Qdrant indexing happens after approval.
5. AI receives retrieved context only for FAQ/policy style turns.

Forbidden flow:

1. Client writes something.
2. Bot stores it directly as truth.
3. Future replies use it without review.
