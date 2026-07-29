---
title: "Tooling And Memory Stack"
created: 2026-07-27
tags:
  - max-bot4
  - tooling
  - graphify
  - headroom
---

# Tooling And Memory Stack

MaxBot 4 uses `AI_template` as a development and operations layer. Graphify and Headroom are optional development tools, not runtime dependencies of the bot.

## Wiki

`wiki/` is the durable human-readable project memory. Update it when architecture, operations, storage, payment, AI, or rollout decisions change.

## Graphify

Graphify is for code relationship analysis: dependency maps, call chains, storage flows, and large refactor planning.

Tracked wrapper scripts:

```powershell
.\scripts\graphify-build.ps1 -Status
.\scripts\graphify-build.ps1 -AstOnly
.\scripts\graphify-build.ps1
```

By default the script runs AST-only/local extraction and does not read `.env`. Semantic extraction requires an explicit `-Semantic` flag.

Generated output lives under `graphify-out/` and is ignored by git.

Current local status on 2026-07-29: `graphify` is on PATH and `graphify-out/graph.json` exists with 1110 nodes / 2919 links, but the graph is stale for the public catalog work. Its manifest was written on 2026-07-27 and does not include `app/api/`, `app/catalog/`, `scripts/generate_public_catalog_fixture.py`, or `tests/public_catalog_smoke.py`. Treat current code as source of truth until `.\scripts\graphify-build.ps1 -AstOnly` is run and reviewed.

## Headroom

Headroom is for compressing large logs, long diffs, graph reports, and noisy context. It is not part of bot runtime.

No repo-tracked Headroom control scripts are required for MaxBot runtime. Use the CLI directly unless a future task deliberately restores tracked wrappers:

```powershell
headroom --version
headroom doctor
```

A running proxy does not mean the current Codex session is routed.

## Status

Use the repo wrapper:

```powershell
.\scripts\tooling-status.ps1 -SkipValidation
```

Missing Graphify or Headroom is not a bot failure. It means the optional development tool is not installed or not on PATH.

Current local status on 2026-07-29: Graphify is installed, Headroom is installed (`0.31.0`), and `.\scripts\tooling-status.ps1 -SkipValidation` runs successfully. TODO: after this catalog-only pass, decide whether to keep Headroom control scripts as direct CLI documentation or add tracked wrappers in a dedicated tooling cleanup.
