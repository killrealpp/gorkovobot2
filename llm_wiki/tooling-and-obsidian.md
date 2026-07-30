---
title: "Tooling And Obsidian"
created: 2026-07-30
tags:
  - llm-wiki
  - obsidian
  - tooling
---

# Tooling And Obsidian

Obsidian is a good visual IDE for an LLM-maintained wiki: the agent edits Markdown, while the human browses links, graph view, and changed pages.

## Useful Obsidian practices

- Use Graph View to see hubs, clusters, and orphan pages.
- Keep attachments in a stable folder if images are part of the source set.
- Use YAML frontmatter consistently so plugins can query pages later.
- Keep page names stable once other pages link to them.

## Optional tools

These are optional. Do not add them as runtime dependencies for MaxBot.

- Obsidian Web Clipper: useful for converting web articles to local Markdown.
- Dataview: useful when pages have frontmatter.
- Marp: useful for slide decks generated from wiki content.
- `qmd`: possible local markdown search with hybrid search and LLM reranking.
- Simple custom search scripts can be added later if the wiki grows beyond what [[index]] can comfortably cover.

## MaxBot boundary

This LLM Wiki tooling is for knowledge maintenance. It must not change MaxBot booking/payment/YCLIENTS behavior by itself.

See also: [[architecture]], [[schema]].
