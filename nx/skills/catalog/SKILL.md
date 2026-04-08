---
name: catalog
description: Use when working with catalog entries, resolving tumblers, creating links, understanding what's linked to a file, or seeding link context for the auto-linker. Covers resolve, link, context, and seed operations.
effort: low
---

# Catalog Manipulation

Agent-friendly operations for the document catalog and link graph. Use these instead of raw MCP calls when you need to find, link, or understand documents.

## Resolve: Find a Document

Given a file path, title, or RDR name, find its catalog tumbler.

```
# By file path (relative — post-RDR-060)
mcp__plugin_nx_nexus__catalog_search(query="src/nexus/scoring.py")
# → Extract tumbler from first result

# By title or RDR name
mcp__plugin_nx_nexus__catalog_search(query="RDR-060")
# → Extract tumbler from first result

# By content type
mcp__plugin_nx_nexus__catalog_search(query="catalog", content_type="code")
```

The result includes `tumbler`, `title`, `content_type`, `file_path`. Use the `tumbler` for all subsequent operations.

**Quick resolve pattern** (copy-paste):
```
result = mcp__plugin_nx_nexus__catalog_search(query="<file-or-title>", limit=1)
# Parse tumbler from result
```

## Context: What's Linked to This File?

Before modifying a file, understand its design context:

```bash
# CLI (fast, human-readable)
nx catalog links-for-file src/nexus/scoring.py
```

```
# MCP (for agents)
mcp__plugin_nx_nexus__catalog_search(query="src/nexus/scoring.py", limit=1)
# → get tumbler
mcp__plugin_nx_nexus__catalog_links(tumbler="<tumbler>", direction="both")
# → {"nodes": [...], "edges": [...]}
```

This shows which RDRs discuss the file, what other code it's linked to, and through what link types.

## Link: Connect Two Documents

Create a typed link between documents. Accepts tumblers or titles.

```
# By tumbler
mcp__plugin_nx_nexus__catalog_link(
    from_tumbler="1.1.115",
    to_tumbler="1.1.440",
    link_type="implements",
    created_by="<your-agent-name>"
)

# By title (resolve first)
# 1. Search for source: catalog_search(query="scoring.py") → tumbler "1.1.115"
# 2. Search for target: catalog_search(query="RDR-060") → tumbler "1.1.440"
# 3. Link: catalog_link(from_tumbler="1.1.115", to_tumbler="1.1.440", ...)
```

**Link types** (use the right one):
| Type | Meaning | When to use |
|------|---------|-------------|
| `implements` | Code implements design | Code file ← RDR that specifies it |
| `cites` | Document references another | Paper citing another paper or RDR |
| `relates` | General relationship | Two related docs without directional meaning |
| `supersedes` | Replaces an older version | New RDR superseding old one |

Do NOT use `implements-heuristic` — that's for the automated linker only.

## Seed: Set Up Auto-Linker Context

Before storing findings via `store_put`, seed T1 scratch so the auto-linker creates links automatically:

```
# 1. Resolve the target document
mcp__plugin_nx_nexus__catalog_search(query="RDR-060", limit=1)
# → tumbler "1.1.440"

# 2. Seed link-context in T1 scratch
mcp__plugin_nx_nexus__scratch(
    action="put",
    content='{"targets": [{"tumbler": "1.1.440", "link_type": "relates"}], "source_agent": "<agent-name>"}',
    tags="link-context"
)

# 3. Now store_put — auto-linker fires automatically
mcp__plugin_nx_nexus__store_put(content="...", collection="knowledge", title="...")
# → auto-linker reads link-context and creates: new_doc → 1.1.440 (relates)
```

**Multiple targets**: add more items to the `targets` array:
```json
{"targets": [
    {"tumbler": "1.1.440", "link_type": "implements"},
    {"tumbler": "1.1.383", "link_type": "cites"}
], "source_agent": "my-agent"}
```

**Skip seeding when**: no relevant RDR/document exists for what you're storing. The auto-linker handles empty context gracefully (zero links created, no error).

## Discovery: Find What Needs Linking

```bash
nx catalog orphans --no-links     # entries with zero links
nx catalog coverage               # % linked by content type
nx catalog suggest-links           # unlinked code-RDR pairs
nx catalog session-summary         # recently modified files + linked RDRs
```

## When to Create Links

- **Implementing an RDR**: link code files → RDR with `implements`
- **Storing research findings**: seed link-context before `store_put` with `cites` or `relates`
- **Architectural decisions**: link decision doc → related RDRs with `relates`
- **Consolidating knowledge**: link new → old with `supersedes`

Links are permanent and idempotent — creating the same link twice is a no-op.
