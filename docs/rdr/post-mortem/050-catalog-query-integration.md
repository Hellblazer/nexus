---
title: "Post-Mortem: Knowledge Graph and Catalog-Aware Query Planning"
date: 2026-04-05
rdr: RDR-050
status: closed
close_reason: implemented
type: post-mortem
severity: n/a
---

# Post-Mortem: RDR-050 — Knowledge Graph and Catalog-Aware Query Planning

**Closed**: 2026-04-05  **Reason**: implemented  **PR**: #126

## What Was Done

Integrated the catalog (RDR-049) into nexus's query planning system across three layers.

**Layer 1 (Link Creation) — complete via RDR-049:**
- Citation auto-generation from Semantic Scholar `bib_semantic_scholar_id` cross-matching
- Code-RDR heuristic linking (module name in RDR title)
- Manual link creation (CLI + MCP)
- `created_by` provenance on all links (Nelson's junk filtering principle)

**Layer 2 (Query Planning) — implemented:**
- 3 new plan operations: `catalog_search`, `catalog_links`, `catalog_resolve`
- Query planner agent updated with operation schemas
- `/nx:query` skill dispatches catalog operations correctly
- `$step_N.collections` extraction: catalog results narrow search corpus
- Few-shot plan templates seeded in T2 plan library (IDs 18-21, tagged `catalog`)
- Validated: "Schema Mappings" query narrows 83 to 9 collections

**Layer 3 (Concept Nodes) — deferred:**
- Ghost elements with `content_type="concept"` + `about` links
- Deferred until demonstrated need

## Success Criteria Assessment

| Criterion | Status |
|-----------|--------|
| Layer 1: link types, provenance, auto-generation | COMPLETE (via RDR-049) |
| Layer 2: planner generates valid catalog plans | COMPLETE |
| Layer 2: skill dispatches catalog operations | COMPLETE |
| Layer 2: collection extraction scopes search | COMPLETE |
| Layer 2: catalog-scoped search fewer MCP calls | COMPLETE |
| Layer 2: few-shot templates seeded | COMPLETE |
| Layer 3: concept nodes | DEFERRED (no demonstrated need) |

## What Went Well

1. **Clean layering** — Layer 1 (links), Layer 2 (query planning), Layer 3 (concepts) built in order of proven utility. Each layer only proceeded when the previous was stable.
2. **Nelson's principles scaled** — "link search is free", "categories are user business", typed links for noise reduction. The provenance model (`created_by`) paid off immediately for filtering agent-generated links from manual ones.
3. **Plan operation to MCP tool 1:1 mapping** — no translation layer needed. `catalog_search` dispatches to the `catalog_search` MCP tool directly, `catalog_links` to `catalog_links`, etc.
4. **`$step_N.collections` extraction** — catalog results seamlessly scope downstream search steps. The 83-to-9 collection narrowing on "Schema Mappings" proved the pattern works at scale.

## What Went Wrong

1. **Layer 1 was larger than expected** — ended up being the bulk of the RDR-049 implementation, not a quick prerequisite. The link graph schema, audit queries, and provenance tracking were substantial standalone work.
2. **RF-10 (non-document knowledge) gap** — catalog metadata (author, year, file_path) is a poor fit for atomic facts stored in `knowledge__` collections. Workaround: use `meta.kind` field for subdivision. This remains a design tension between document-centric and fact-centric knowledge.

## Key Learnings

1. **Query planning integration requires concrete API surface first** — trying to integrate the query planner before the catalog API was stable would have been premature. The 3-operation schema (`catalog_search`, `catalog_links`, `catalog_resolve`) emerged from real usage, not upfront design.
2. **Few-shot plan templates are the key enabler** — the planner learns catalog-aware patterns from examples, not from schema descriptions alone. IDs 18-21 cover the four most common catalog query shapes: author lookup, citation traversal, provenance chain, and metadata-scoped search.
3. **Narrow-then-search is the killer pattern** — `catalog_search` by metadata narrows collections, then scoped vector search runs only against relevant collections. This dramatically improves precision and reduces API calls compared to searching all 83 collections.
