---
title: "LLM Wiki Operations"
created: 2026-07-30
tags:
  - llm-wiki
  - operations
---

# LLM Wiki Operations

This page defines the main workflows for [[index|the LLM Wiki]].

## Ingest

Use when the user adds a source and asks the agent to process it.

Steps:

1. Read the source.
2. Identify key claims, entities, concepts, dates, contradictions, and open questions.
3. Discuss uncertain interpretation with the user when needed.
4. Create or update the relevant wiki pages.
5. Add cross-links between related pages.
6. Update [[index]] if pages were added or substantially changed.
7. Append an entry to [[log]].

## Query

Use when the user asks a question against the wiki.

Steps:

1. Read [[index]].
2. Open the most relevant linked pages.
3. Answer from the maintained wiki first.
4. If the answer creates durable synthesis, offer or make a new wiki page when the user asks.
5. Log important durable work in [[log]].

## Lint

Use when the user asks for a health check.

Check for:

- contradictions between pages;
- stale claims superseded by newer sources;
- orphan pages with no inbound links;
- important concepts mentioned but not promoted to pages;
- missing cross-references;
- unclear source provenance;
- TODOs that need user decisions.

## Maintenance style

Prefer small, reviewable edits. Keep links dense enough for navigation, but avoid turning every noun into a link.

See also: [[schema]], [[indexing-and-logging]].
