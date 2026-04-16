---
name: query
description: Use when questions require multi-step retrieval and analysis (extract, summarize, rank, compare, generate) over nx knowledge collections with plan decomposition and reuse.
effort: medium
---

# Query Skill

Use `mcp__plugin_nx_nexus__nx_answer` for analytical queries over nx knowledge.

`nx_answer(question, scope?, context?)` handles plan matching, execution, auto-hydration, and operator dispatch internally. No subagent coordination needed — the tool enforces plan-match-first discipline at the contract level.

## When This Skill Activates

- Cross-corpus consistency checks
- Structured extraction with comparison
- Multi-source synthesis
- Evidence-grounded generation with citations
- Author/citation/provenance queries

## What Not To Use This For

- Simple lookups — use `mcp__plugin_nx_nexus__search` directly
- Single-document retrieval — use `mcp__plugin_nx_nexus__query` directly
