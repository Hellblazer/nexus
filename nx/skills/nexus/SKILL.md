---
name: nexus
description: Use when running nx commands for search, memory, knowledge storage, or indexing — or when unsure which nx subcommand to use
effort: low
---

# Nexus Quick Reference

## Storage Tiers

| Tier | Scope | MCP Tools | Use For |
|------|-------|-----------|---------|
| T1 | Session | `scratch`, `scratch_manage` | Hypotheses, interim findings, checkpoints |
| T2 | Persistent | `memory_put`, `memory_get`, `memory_search` | Cross-session state, agent relay notes |
| T3 | Permanent | `search`, `store_put`, `store_list` | Validated findings, decisions, patterns |

## Common Operations

```
# Analytical answer (RDR-080) — plan-match-first, falls through to inline planner on miss
mcp__plugin_nx_nexus__nx_answer(question="how does plan matching work"                  # primary front door
mcp__plugin_nx_nexus__nx_answer(question="...", dimensions={"verb":"research"}          # pin a verb for the matcher
mcp__plugin_nx_nexus__nx_answer(question="...", scope="1.2"                             # catalog subtree filter

# Search (chunk-level) / query (document-level)
mcp__plugin_nx_nexus__search(query="query"                          # semantic search across T3
mcp__plugin_nx_nexus__search(query="query", corpus="code"           # code only
mcp__plugin_nx_nexus__search(query="query", structured=True         # returns {ids, tumblers, distances, collections}
mcp__plugin_nx_nexus__query(question="...", corpus="knowledge", follow_links="cites"    # catalog-aware, document-level

# Batch hydrate chunks past the ChromaDB 300-record quota
mcp__plugin_nx_nexus__store_get_many(ids=["id1","id2","id3"], collections="knowledge__art"
mcp__plugin_nx_nexus__store_get_many(ids="id1,id2", collections="rdr__nexus", structured=True

# Walk the catalog link graph (depth capped at 3 — SC-4)
mcp__plugin_nx_nexus__traverse(seeds=["1.1.635"], link_types=["implements","cites"], depth=2
mcp__plugin_nx_nexus__traverse(seeds="1.1.635", purpose="find-implementations"          # link_types XOR purpose

# Analytical operators — each spawns `claude -p` (default timeout 120s)
mcp__plugin_nx_nexus__operator_summarize(content="...", cited=True
mcp__plugin_nx_nexus__operator_extract(inputs=["doc1","doc2"], fields="title,year,author"
mcp__plugin_nx_nexus__operator_rank(items=["a","b","c"], criterion="relevance to X"
mcp__plugin_nx_nexus__operator_compare(items=["x","y"], focus="scalability"
mcp__plugin_nx_nexus__operator_generate(template="release note", context="..."

# Background hygiene — call and let run (long-lived claude -p subprocesses)
mcp__plugin_nx_nexus__nx_tidy()                                     # T2 memory consolidation
mcp__plugin_nx_nexus__nx_enrich_beads()                             # design-notes auto-fill
mcp__plugin_nx_nexus__nx_plan_audit()                               # plan library quality sweep

# Memory (T2)
mcp__plugin_nx_nexus__memory_put(content="content", project="{repo}", title="file.md"
mcp__plugin_nx_nexus__memory_get(project="{repo}", title="file.md"
mcp__plugin_nx_nexus__memory_search(query="query", project="{repo}"

# Knowledge (T3)
mcp__plugin_nx_nexus__store_put(content="content", collection="knowledge", title="title", tags="tag"
mcp__plugin_nx_nexus__store_list(collection="knowledge"

# Scratch (T1)
mcp__plugin_nx_nexus__scratch(action="put", content="working note"
mcp__plugin_nx_nexus__scratch_manage(action="flag", entry_id="<id>"       # auto-promote to T2 at session end

# Plan library (T2)
mcp__plugin_nx_nexus__plan_search(query="retrieval"                       # find reusable plans
mcp__plugin_nx_nexus__plan_save(query="...", plan_json="{...}"            # persist a successful plan
```

## When to reach for each

- **`nx_answer`** — analytical questions, cross-corpus synthesis. Primary front door for research / review / analyze / debug verb skills. Plan-match-first; falls through to inline planner on miss.
- **`search` vs `query`** — `search` returns chunks (finest grain), `query` returns documents grouped by source. Use `search` for fragment hunting, `query` for literature scoping + catalog traversal.
- **`traverse`** — walk the typed link graph from known tumblers. `link_types` XOR `purpose`; depth ≤ 3.
- **`store_get_many`** — batch-hydrate chunk IDs from `search(structured=True)` or `traverse`. Safe past the 300-record write cap.
- **Operators** — content transforms; take raw text, return structured JSON. Use after retrieval to summarize/extract/rank/compare/generate.
- **`nx_tidy` / `nx_enrich_beads` / `nx_plan_audit`** — background hygiene; slow (claude -p). Call and move on.

## Catalog (T3 metadata — document registry + typed link graph)

```
# Search/browse
mcp__plugin_nx_nexus-catalog__search(query="schema mappings", author="Fagin", corpus="schema-evolution"
mcp__plugin_nx_nexus-catalog__show(tumbler="1.9.14"                    # full entry with links
mcp__plugin_nx_nexus-catalog__resolve(owner="1.1", corpus="schema-evolution"  # → collection names
mcp__plugin_nx_nexus-catalog__stats                                    # health summary

# Link graph — live documents only (deleted nodes excluded)
mcp__plugin_nx_nexus-catalog__links(tumbler="1.9.14", direction="in", link_type="cites", depth=2
  Returns {"nodes": [...], "edges": [...]}

# Link CRUD
mcp__plugin_nx_nexus-catalog__link(from_tumbler="1.1.1", to_tumbler="1.2.5", link_type="cites", created_by="user"
  Returns {"from": ..., "to": ..., "type": ..., "created": true/false}

# Admin/audit — includes orphaned links (all links, not just live)
mcp__plugin_nx_nexus-catalog__link_query(link_type="cites", created_by="bib_enricher", limit=50
```

**Link types**: `cites` (citation), `implements-heuristic` (auto code→RDR), `supersedes`, `quotes`, `relates`, `comments`, `implements` (manual).
**Two graph views**: `mcp__plugin_nx_nexus-catalog__links` returns live-document links only. `mcp__plugin_nx_nexus-catalog__link_query` returns all links including orphans.
Use catalog for: author queries, citation traversal, provenance chains, corpus-scoped search.
The `/nx:query` skill handles full catalog-aware plan execution.

## Indexing (CLI only — no MCP equivalent)

```bash
nx index repo <path>                 # index repo (classifies into code + docs collections)
```

## Collection Naming

Always `__` as separator: `code__myrepo`, `docs__corpus`, `knowledge__topic`

## Title Conventions

Use hyphens: `research-{topic}`, `decision-{component}-{name}`, `pattern-{name}`

For full tool reference with all parameters, see [reference.md](./reference.md).
