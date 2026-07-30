---
title: "LLM Wiki Schema"
created: 2026-07-30
tags:
  - llm-wiki
  - schema
  - agent-contract
---

# LLM Wiki Schema

This page is the operating contract for agents maintaining [[index|this LLM Wiki]].

## Agent contract

When a task mentions Obsidian, wiki maintenance, durable memory, source ingest, markdown knowledge files, cross-links, or LLM Wiki:

1. Read [[index]] first.
2. Read this page.
3. Select the workflow from [[operations]].
4. Treat raw sources as immutable.
5. Update generated wiki pages with clear cross-links.
6. Update [[index]] when pages are added or substantially changed.
7. Append a short entry to [[log]] for durable chronology.

## Page conventions

Use Markdown with YAML frontmatter:

```yaml
---
title: "Readable Page Title"
created: YYYY-MM-DD
updated: YYYY-MM-DD
tags:
  - llm-wiki
---
```

Prefer Obsidian-style links for local pages:

- `[[architecture]]`
- `[[operations]]`
- `[[indexing-and-logging]]`

Use normal Markdown links for external URLs.

## Source discipline

- Raw sources are read-only.
- Generated wiki pages are maintained by the agent.
- If a new source contradicts an old page, do not silently overwrite the old claim. Note the contradiction and update the affected page.
- If a claim is uncertain, mark it as uncertain instead of smoothing it into false confidence.

## Scope

This schema governs only the `llm_wiki/` folder. It does not override MaxBot production rules in `AGENTS.md`.

See also: [[architecture]], [[operations]], [[indexing-and-logging]].
