# Codex Execution Plans

This repository uses ExecPlans for large, risky, or cross-cutting work.

An ExecPlan is a self-contained Markdown implementation plan that a new engineer or agent can follow without chat history. It must explain the goal, current repository context, exact edits, commands, validation, risks, and recovery steps.

## When To Use An ExecPlan

Create or update an ExecPlan before changing booking flow, payment behavior, availability rules, YCLIENTS sync, storage schema, AI JSON contracts, prompts, webhooks, security, deployment, retention cleanup, YAML universalization, or RAG.

Small documentation edits and narrow one-file fixes do not need a new ExecPlan, but update the wiki if they change durable project knowledge.

## Required Properties

Every ExecPlan must be:

- self-contained, so the reader needs only the current working tree and the plan;
- living, so progress and decisions are updated as work proceeds;
- beginner-readable, with every project-specific term explained plainly;
- outcome-focused, with observable behavior and validation commands;
- safe and idempotent, with retry or recovery notes for risky steps.

Do not rely on external links for required implementation knowledge. If a fact matters, include it in the plan in your own words.

## Required Sections

Every large-task ExecPlan must contain these sections:

- `Purpose / Big Picture`
- `Progress`
- `Surprises & Discoveries`
- `Decision Log`
- `Outcomes & Retrospective`
- `Context and Orientation`
- `Plan of Work`
- `Concrete Steps`
- `Validation and Acceptance`
- `Idempotence and Recovery`
- `Artifacts and Notes`
- `Interfaces and Dependencies`

If the task touches product behavior, also include `Product Rules`. If it touches persistence, include `Storage / Database Design`. If it touches AI calls, include `AI Contracts`.

`Progress`, `Surprises & Discoveries`, `Decision Log`, and `Outcomes & Retrospective` must stay current while work proceeds.

## Format

When an ExecPlan is written as the entire content of a `.md` file, omit outer triple backticks. When pasting an ExecPlan into chat, wrap the entire plan in one fenced block labeled `md` and do not nest additional triple-backtick fences inside it.

Prefer prose over tables. Checklists are mandatory only in `Progress`.

Store project ExecPlans under `wiki/` unless the user names another path. Use names such as `wiki/retention-cleanup-plan-2026-07-27.md`.

## Progress Format

Use checkbox entries with timestamps:

- `[x] (2026-07-27 11:30 Europe/Moscow) Inspected current storage schema.`
- `[ ] Add tests for the new retention report path.`
- `[ ] Partially complete: implemented dry-run; remaining: guarded apply validation.`

At every stopping point, split partial work into completed and remaining items so the next agent can resume from the plan alone.

## Decision Log Format

Record decisions like this:

- Decision: Use backend validation rather than relying on the LLM for phone numbers.
  Rationale: Payments and YCLIENTS records need deterministic validation, and this project keeps AI as a parser.
  Date/Author: 2026-07-27 / Codex.

## Git Safety

Check `git status --short --branch` before edits. Do not create commits, branches, remotes, pushes, pull requests, or history rewrites unless the user explicitly asks. Do not use destructive cleanup without explicit confirmation.

## Skeleton

# Short, action-oriented title

This ExecPlan is a living document. The sections `Progress`, `Surprises & Discoveries`, `Decision Log`, and `Outcomes & Retrospective` must be kept up to date as work proceeds.

This plan follows `PLANS.md` from the repository root.

## Purpose / Big Picture

Explain what the user can do after the change that they could not do before, and how to observe it working.

## Progress

- [ ] Initial repository inspection is complete.
- [ ] Implementation is complete.
- [ ] Validation is complete.

## Surprises & Discoveries

- Observation: None yet.
  Evidence: N/A.

## Decision Log

- Decision: None yet.
  Rationale: N/A.
  Date/Author: N/A.

## Outcomes & Retrospective

Not completed yet.

## Context and Orientation

Describe the relevant current files and concepts as if the reader has never seen this repository.

## Plan of Work

Describe the sequence of edits in prose. Name files and functions precisely.

## Concrete Steps

List exact commands and working directory. Include expected short output when useful.

## Validation and Acceptance

Describe the behavior, tests, smoke checks, and expected outputs that prove success.

## Idempotence and Recovery

Explain what can be rerun safely and how to recover from partial failure.

## Artifacts and Notes

Include concise transcripts, diffs, or evidence that matter.

## Interfaces and Dependencies

Name any modules, functions, environment variables, services, or external APIs that the implementation depends on.
