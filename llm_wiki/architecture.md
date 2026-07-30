---
title: "LLM Wiki Architecture"
created: 2026-07-30
tags:
  - llm-wiki
  - architecture
---

# LLM Wiki Architecture

The LLM Wiki pattern has three layers.

## 1. Raw sources

Raw sources are the curated input collection: articles, notes, papers, images, transcripts, data files, and other source material.

Rules:

- raw sources are immutable;
- the agent may read them;
- the agent should not rewrite them;
- when possible, source pages should be cited from generated wiki pages.

## 2. The generated wiki

The wiki is a directory of agent-maintained Markdown files: summaries, entity pages, concept pages, comparisons, timelines, questions, and synthesis pages.

This folder is that wiki. The agent owns the maintenance work: extracting, filing, linking, revising, and logging.

Key pages:

- [[index]] - content map.
- [[log]] - chronological history.
- [[operations]] - workflows.

## 3. The schema

The schema tells the agent how to operate. In this folder, the schema is [[schema]]. At repository level, `AGENTS.md` tells Codex when to read this LLM Wiki guidance.

## Difference from plain RAG

Plain RAG retrieves chunks from source files at query time. The LLM Wiki pattern compiles knowledge into a persistent intermediate layer first, then keeps that layer current.

That means:

- cross-references accumulate;
- contradictions can be tracked;
- useful answers can become durable pages;
- the wiki improves with each ingest and query.

See also: [[indexing-and-logging]], [[tooling-and-obsidian]].
