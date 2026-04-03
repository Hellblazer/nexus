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

## Indexing (CLI only — no MCP equivalent)

```bash
nx index repo <path>                 # index repo (classifies into code + docs collections)
```

## Collection Naming

Always `__` as separator: `code__myrepo`, `docs__corpus`, `knowledge__topic`

## Title Conventions

Use hyphens: `research-{topic}`, `decision-{component}-{name}`, `pattern-{name}`

For full tool reference with all parameters, see [reference.md](./reference.md).
