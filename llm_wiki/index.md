---
title: "LLM Wiki Index"
created: 2026-07-30
tags:
  - llm-wiki
  - index
---

# LLM Wiki Index

This folder is a root-level markdown knowledge system for the LLM Wiki pattern from the supplied `obsidian.txt` note.

The core idea: do not force the agent to rediscover knowledge from raw files on every question. Instead, let the agent incrementally maintain a persistent, interlinked wiki that compounds over time.

## Start here

- [[schema]] - how an agent should behave when maintaining this wiki.
- [[architecture]] - the three-layer model: raw sources, generated wiki, schema.
- [[operations]] - ingest, query, and lint workflows.
- [[indexing-and-logging]] - how `index.md` and `log.md` work together.
- [[tooling-and-obsidian]] - optional local tools and Obsidian practices.
- [[log]] - chronological maintenance record for this LLM Wiki folder.

## Working rule

For wiki-related tasks, the agent should read this file first, then read [[schema]], then follow the relevant workflow in [[operations]].

This folder is documentation and agent workflow guidance. It is not runtime configuration for MaxBot booking, payment, YCLIENTS, Supabase, or public catalog behavior.
