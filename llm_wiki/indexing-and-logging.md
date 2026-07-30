---
title: "Indexing And Logging"
created: 2026-07-30
tags:
  - llm-wiki
  - indexing
  - logging
---

# Indexing And Logging

The LLM Wiki uses two special files with different jobs.

## `index.md`

[[index]] is content-oriented. It is the map of the wiki.

It should list:

- major topic pages;
- one-line summaries;
- important workflow pages;
- useful navigation paths.

When answering a query, the agent should read [[index]] first, then follow links to relevant pages.

## `log.md`

[[log]] is chronological. It records what changed and why.

Recommended entry shape:

```markdown
## [YYYY-MM-DD] type | Short title

- What changed.
- Why it changed.
- Source or user request.
```

Useful event types:

- `ingest`
- `query`
- `lint`
- `maintenance`
- `schema`

See also: [[operations]], [[schema]].
