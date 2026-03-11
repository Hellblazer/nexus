---
name: nexus
description: Use when running nx commands for search, memory, knowledge storage, or indexing — or when unsure which nx subcommand to use
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
Use search tool: query="query"                          # semantic search across T3
Use search tool: query="query", corpus="code"           # code only
Use search tool: query="query", corpus="knowledge", n=5 # knowledge with limit

# Memory (T2)
Use memory_put tool: content="content", project="{repo}", title="file.md"
Use memory_get tool: project="{repo}", title="file.md"
Use memory_search tool: query="query", project="{repo}"

# Knowledge (T3)
Use store_put tool: content="content", collection="knowledge", title="title", tags="tag"
Use store_list tool: collection="knowledge"

# Scratch (T1)
Use scratch tool: action="put", content="working note"
Use scratch_manage tool: action="flag", id="<id>"       # auto-promote to T2 at session end
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
