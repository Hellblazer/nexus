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
# Search
mcp__plugin_nx_nexus__search(query="query"                          # semantic search across T3
mcp__plugin_nx_nexus__search(query="query", corpus="code"           # code only
mcp__plugin_nx_nexus__search(query="query", corpus="knowledge", limit=5 # knowledge with limit

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
```

## Catalog (T3 metadata — document registry + typed link graph)

```
# Search/browse
mcp__plugin_nx_nexus__catalog_search(query="schema mappings", author="Fagin", corpus="schema-evolution"
mcp__plugin_nx_nexus__catalog_show(tumbler="1.9.14"                    # full entry with links
mcp__plugin_nx_nexus__catalog_resolve(owner="1.1", corpus="schema-evolution"  # → collection names
mcp__plugin_nx_nexus__catalog_stats                                    # health summary

# Link graph — live documents only (deleted nodes excluded)
mcp__plugin_nx_nexus__catalog_links(tumbler="1.9.14", direction="in", link_type="cites", depth=2
  Returns {"nodes": [...], "edges": [...]}

# Link CRUD
mcp__plugin_nx_nexus__catalog_link(from_tumbler="1.1.1", to_tumbler="1.2.5", link_type="cites", created_by="user"
  Returns {"from": ..., "to": ..., "type": ..., "created": true/false}

# Admin/audit — includes orphaned links (all links, not just live)
mcp__plugin_nx_nexus__catalog_link_query(link_type="cites", created_by="bib_enricher", limit=50
mcp__plugin_nx_nexus__catalog_link_audit()                             # orphans, stats, duplicates
mcp__plugin_nx_nexus__catalog_link_bulk(link_type="cites", dry_run=True  # preview before delete
```

**Link types**: `cites` (citation), `implements-heuristic` (auto code→RDR), `supersedes`, `quotes`, `relates`, `comments`, `implements` (manual).
**Two graph views**: `mcp__plugin_nx_nexus__catalog_links` returns live-document links only. `mcp__plugin_nx_nexus__catalog_link_query` returns all links including orphans.
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
