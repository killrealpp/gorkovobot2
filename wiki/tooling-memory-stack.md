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

Scripts:

```powershell
.\scripts\graphify-build.ps1 -Status
.\scripts\graphify-build.ps1 -AstOnly
.\scripts\graphify-build.ps1
```

By default the script runs AST-only/local extraction and does not read `.env`. Semantic extraction requires an explicit `-Semantic` flag.

Generated output lives under `graphify-out/` and is ignored by git. Current local AST graph: 1110 nodes / 2919 edges.

## Headroom

Headroom is for compressing large logs, long diffs, graph reports, and noisy context. It is not part of bot runtime.

Scripts:

```powershell
.\scripts\headroom-status.ps1
.\scripts\headroom-start-proxy.ps1
.\scripts\headroom-doctor.ps1
.\scripts\headroom-stop-proxy.ps1
.\scripts\codex-headroom.ps1
```

A running proxy does not mean the current Codex session is routed. Use `scripts/codex-headroom.ps1` for a new routed session.

## Status

Use:

```powershell
.\scripts\tooling-status.ps1 -SkipValidation
```

Missing Graphify or Headroom is not a bot failure. It means the optional development tool is not installed or not on PATH.

Current local status on 2026-07-27: Graphify is installed and has a local graph; Headroom is installed (`0.31.0`), while the proxy is optional and not currently running.
