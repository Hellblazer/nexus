---
name: nexus
description: Use when running nx commands for search, memory, knowledge storage, or indexing — or when unsure which nx subcommand to use
---

# Nexus Quick Reference

## Storage Tiers

| Tier | Scope | CLI | Use For |
|------|-------|-----|---------|
| T1 | Session | `nx scratch` | Hypotheses, interim findings, checkpoints |
| T2 | Persistent | `nx memory` | Cross-session state, agent relay notes |
| T3 | Permanent | `nx search`, `nx store` | Validated findings, decisions, patterns |

## Common Commands

```bash
# Search
nx search "query"                    # semantic search across T3
nx search "query" --corpus code      # code only
nx search "query" --hybrid           # semantic + ripgrep + frecency

# Memory (T2)
nx memory put "content" --project {repo} --title file.md
nx memory get --project {repo} --title file.md
nx memory search "query" --project {repo}

# Knowledge (T3)
echo "content" | nx store put - --collection knowledge --title "title" --tags "tag"
nx store list --collection knowledge

# Scratch (T1)
nx scratch put "working note"
nx scratch flag <id>                 # auto-promote to T2 at session end

# Indexing
nx index repo <path>                 # index repo (classifies into code + docs collections)
```

## Collection Naming

Always `__` as separator: `code__myrepo`, `docs__corpus`, `knowledge__topic`

## Title Conventions

Use hyphens: `research-{topic}`, `decision-{component}-{name}`, `pattern-{name}`

For full command reference with all flags and options, see [reference.md](./reference.md).
