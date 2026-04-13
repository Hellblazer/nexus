# MCP Servers

Nexus ships **two** MCP (Model Context Protocol) servers as part of the `nx`
Claude Code plugin. This page explains what each server exposes, when to use
which, and which operations are intentionally kept out of MCP.

If you only need to use the CLI, skip this page. Everything here matters for
agents (especially Claude Code subagents) that reach into Nexus via MCP tools.

## Why two servers

Before RDR-062, a single monolithic `nexus` server exposed 30 tools spanning
storage, memory, scratch, plans, and catalog. The split into two focused
servers was driven by three problems the monolith created:

1. **Tool-choice noise.** Agents saw one large pool of tools and routinely
   picked the wrong one. Scoping catalog operations to their own server lets
   agents (and humans) reason about fewer tools at a time.
2. **Permission blast radius.** Blanket auto-approval for one 30-tool server
   meant auto-approving admin operations like `store_delete` or
   `collection_verify`. Splitting lets each server have its own permission
   posture.
3. **Short names for catalog tools.** Inside the `nexus-catalog` server,
   `search` is unambiguous — the server name already provides the `catalog`
   context. The pre-RDR-062 names all had a redundant `catalog_` prefix
   (`catalog_search`, `catalog_show`, etc.). The short names are the primary
   API; the long names survive only as Python function names for CLI-only
   callers and the backward-compat shim.

## The two servers at a glance

| Server | Entry point | Tools | Purpose |
|---|---|---|---|
| `nexus` | `nx-mcp` | 15 | Storage tiers, memory, scratch, plans, consolidation |
| `nexus-catalog` | `nx-mcp-catalog` | 10 | Document catalog, link graph, tumbler resolution |

Both servers are bundled in the `nx` plugin's `.mcp.json`. Installing the
plugin (`/plugin install nx@nexus-plugins`) registers both with Claude Code
automatically. No separate install step.

## `nexus` — core storage and memory (15 tools)

Full tool names follow Claude Code's convention: `mcp__plugin_nx_nexus__<tool>`.

### Tier operations

| Tool | Purpose |
|---|---|
| `search` | Semantic chunk search over T3 collections. Supports `topic` for topic-scoped search, `cluster_by="semantic"` for topic grouping, and automatic same-topic distance boost. |
| `query` | Document-level catalog-aware retrieval (scope by `author`, `content_type`, `subtree`, `follow_links`, `depth`). Results ranked with both link-aware and topic-aware boosting. |
| `store_put` | Write a document into a T3 collection. Triggers a post-store hook that auto-assigns the document to its nearest topic via centroid ANN lookup. |
| `store_get` | Retrieve a document by id from a T3 collection |
| `store_list` | Paginate documents in a T3 collection |

### Memory (T2)

| Tool | Purpose |
|---|---|
| `memory_put` | Write a per-project persistent note |
| `memory_get` | Retrieve a note by `(project, title)` or by id |
| `memory_search` | FTS5 keyword search over T2 memory |
| `memory_delete` | Delete a single note |
| `memory_consolidate` | Find overlaps, merge, or flag stale entries (see [Memory and Tasks § Consolidation](memory-and-tasks.md#consolidation-rdr-061-e6)) |

### Scratch (T1)

| Tool | Purpose |
|---|---|
| `scratch` | Put / get / search / list / delete session-scoped scratch entries |
| `scratch_manage` | Flag for promotion, unflag, promote to T2, reconnect after T1 server restart |

### Supporting

| Tool | Purpose |
|---|---|
| `collection_list` | List all T3 collections visible to the current credentials |
| `plan_save` | Persist a query execution plan (TTL-bounded) for later reuse |
| `plan_search` | Retrieve cached plans by query similarity |

## `nexus-catalog` — document catalog (10 tools)

Full tool names follow the same convention: `mcp__plugin_nx_nexus-catalog__<tool>`.
The tool name (the part after `__`) is short — no redundant `catalog_` prefix.

| Tool | Purpose |
|---|---|
| `search` | Metadata search across the catalog (title, author, corpus, file path) |
| `show` | Full metadata + all inbound/outbound links for a document |
| `list` | Browse catalog entries with filters (type, subtree, owner) |
| `register` | Add a new document to the catalog |
| `update` | Update metadata on an existing catalog entry |
| `link` | Create a typed link between two documents (`cites`, `implements`, `supersedes`, `relates`, `formalizes`, custom) |
| `links` | Return live links for a document (deleted nodes excluded) — optional BFS traversal via `depth` |
| `link_query` | Query the full link table including orphans (admin/audit view) |
| `resolve` | Resolve a file path, title, or tumbler to a catalog entry |
| `stats` | Summary stats — total entries, link counts by type, orphan counts |

See [Document Catalog](catalog.md) for conceptual background and CLI equivalents.

## 6 operations kept CLI-only

Some operations are intentionally **not** exposed as MCP tools. They are
still available via `nx` CLI for human operators, but agents cannot invoke
them. The rationale is uniform: these are destructive, expensive, or
maintenance operations where a human-in-the-loop confirmation matters more
than agent convenience.

| CLI command | Why it's not in MCP |
|---|---|
| `nx store delete` | Destructive deletion of T3 documents |
| `nx collection info` | Expensive ChromaDB introspection better suited to a human debugging session |
| `nx collection verify` | Full-collection scan; expensive and rarely needed by agents |
| `nx catalog unlink` | Destructive edge removal |
| `nx catalog link-audit` | Full-graph scan; expensive and human-oriented |
| `nx catalog link-bulk-delete` (hidden) | Bulk link deletion by filter; high blast radius if misused |
| `nx taxonomy *` | Topic discovery, review, merge, split, rename, rebuild. These are operator curation tasks, not agent tasks. Agents benefit from taxonomy via `search(topic=...)` and automatic topic boost on `search`/`query`. |

The underlying Python functions still exist under the same names inside
`src/nexus/mcp/core.py` and `src/nexus/mcp/catalog.py` — they're just no
longer decorated with `@mcp.tool()`. The legacy `nexus.mcp_server` module is
kept as a backward-compat shim that re-exports all 30 functions (15 core +
10 catalog + 6 demoted) so any external code that imported directly from the
old module keeps working.

## Pagination

Three tools return paged results and accept an `offset` parameter:

- `search`
- `store_list`
- `memory_search`

The response includes a footer line:

```
--- showing 1-20 of 57. next: offset=20
--- showing 41-57 of 57. (end)
```

Pass `offset=N` back to the same tool to fetch the next page. The default
page size is 20 for list-style tools and the `n_results` passed through to
ChromaDB for `search`.

## Permission auto-approval

The plugin installs a `PermissionRequest` hook that auto-approves any tool
call matching `mcp__plugin_nx_.*`. This covers both servers (`nexus` and
`nexus-catalog`) plus the bundled `sequential-thinking` server. Dangerous
system operations — force-push, `bd delete`, deploys — are **not** matched
by this hook and remain subject to the normal confirmation flow.

If you are writing a custom agent that should operate with stricter
permission boundaries, remove or narrow the matcher in
`nx/hooks/hooks.json`'s `PermissionRequest` section.

## Which server should an agent call?

| Task | Server | Tool |
|---|---|---|
| "Find code that handles retries" | `nexus` | `search` |
| "Search within the PDF extraction topic" | `nexus` | `search` with `topic="Math-aware PDF Extraction"` |
| "Summarize all papers by Fagin on schema mappings" | `nexus` | `query` with `author="Fagin"` |
| "What RDRs cite this paper?" | `nexus-catalog` | `links` with `link_type="cites"` |
| "What T3 collection is this paper in?" | `nexus-catalog` | `search` or `resolve` |
| "Persist this research finding" | `nexus` | `store_put` |
| "Remember for next session: we picked Postgres" | `nexus` | `memory_put` |
| "Share a hypothesis with a sibling agent" | `nexus` | `scratch` (put/get) |
| "Cache this query plan for reuse" | `nexus` | `plan_save` |

The rule of thumb: **content** (chunks, documents, notes) is on `nexus`,
**metadata and relationships** (catalog entries, typed links, tumblers) are
on `nexus-catalog`. `query` is the one core-server tool that crosses the
boundary — it uses catalog metadata to scope a content search.

## References

- [Architecture § Module Map (MCP Servers row)](architecture.md#module-map) — developer-oriented internals
- [Document Catalog](catalog.md) — conceptual introduction to what the catalog is
- [Querying Guide](querying-guide.md) — when to use `nx search` vs `query()` MCP vs `/nx:query` skill
- [CLI Reference — nx catalog](cli-reference.md#nx-catalog) — CLI equivalents for the 10 catalog tools
- [nx plugin README](../nx/README.md) — plugin installation, hooks, auto-approval
