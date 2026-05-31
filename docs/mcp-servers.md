# MCP Servers

Nexus ships two MCP servers, bundled in the Claude Code plugin and the Claude Desktop `.mcpb` extension. This page is the **tool catalog** — every tool, on which server, with a one-line purpose.

For **when to use which retrieval interface**, see [Querying Guide](querying-guide.md). For conceptual background, see [Document Catalog](catalog.md) and [Storage Tiers](storage-tiers.md).

## The three servers

| Server | Entry point | Tools | Purpose |
|---|---|---|---|
| `nexus` | `nx-mcp` | 26 | Storage tiers, retrieval, operators, orchestration |
| `nexus-catalog` | `nx-mcp-catalog` | 10 | Document catalog, link graph, tumbler resolution |
| `devonthink` | `nx-mcp-devonthink` | ~17 (DT present) / 1 (DT absent) | DEVONthink agent surface: AI/content/bib/capture tools + the `dt_incorporate` composite (RDR-139 Layer A') |

The `nexus` and `nexus-catalog` servers register automatically when you install the plugin (`/plugin install conexus@nexus-plugins`) or the `.mcpb` extension. No separate install.

**Substrate dependency**: since conexus 4.34.0 (RDR-120), storage tools route through the T2 daemon (and the T3 daemon in local mode). The Claude Code plugin's SessionStart hook auto-spawns `nx daemon t2 ensure-running`; for a daemon that survives reboots independent of Claude Code, run `nx daemon t2 install --autostart` once. See [Container Integration](container-integration.md) for the multi-process / multi-host model.

## `nexus` — retrieval + storage (26 tools)

Full tool names follow `mcp__plugin_conexus_nexus__<tool>`.

### Retrieval (T3)

| Tool | Purpose |
|---|---|
| `search` | Semantic chunk search over T3 collections. Supports `topic` for topic-scoped search, `cluster_by="semantic"` for topic grouping, automatic same-topic distance boost |
| `query` | Document-level catalog-aware retrieval (scope by `author`, `content_type`, `subtree`, `follow_links`, `depth`). Link-aware + topic-aware ranking |
| `store_put` | Write a document into a T3 collection. Fires post-store hooks: batch chain auto-assigns to nearest topic; document-grain chain enqueues aspect extraction on `knowledge__*` (RDR-089) |
| `store_get` | Retrieve a document by id from a T3 collection |
| `store_get_many` | Batch hydration: given N ids, return N contents (with `missing` for not-found). Handles 300+ ids beyond the ChromaDB quota |
| `store_list` | Paginate documents in a T3 collection |

### Memory (T2)

| Tool | Purpose |
|---|---|
| `memory_put` | Write a per-project persistent note |
| `memory_get` | Retrieve by `(project, title)` or id. Title resolution is exact-then-prefix; ambiguous prefixes return candidates rather than picking one |
| `memory_search` | FTS5 keyword search over T2 memory |
| `memory_delete` | Delete a single note |
| `memory_consolidate` | Find overlaps, merge, flag stale entries. See [storage tiers § T2](storage-tiers.md#t2----memory-bank) |

### Scratch (T1)

| Tool | Purpose |
|---|---|
| `scratch` | Put / get / search / list / delete session-scoped entries |
| `scratch_manage` | Flag for promotion, unflag, promote to T2, reconnect after T1 restart |

### Collections + plan library

| Tool | Purpose |
|---|---|
| `collection_list` | List all T3 collections visible to the current credentials |
| `plan_save` | Persist a plan template or ad-hoc plan (TTL-bounded) for later reuse |
| `plan_search` | Retrieve cached plans by semantic similarity (FTS5) |
| `traverse` | Walk the catalog link graph from seed tumblers with typed link filters or a named purpose. Depth capped at 3. Returns `{tumblers, ids, collections}` for downstream retrieval |

### Operators (LLM-backed, RDR-079)

Each operator spawns a `claude -p --output-format json --json-schema …` subprocess with a task-specific system prompt. Structured output is unwrapped from the wrapper.

Inside `nx_answer` / `plan_run`, consecutive operator steps collapse into a single subprocess via operator bundling (55–72% latency savings). Direct MCP-tool calls still spawn per-operator subprocesses. See [Querying Guide § Operator bundling](querying-guide.md#operator-bundling).

| Tool | Purpose |
|---|---|
| `operator_extract` | Pull structured fields (`fields="a,b,c"`) from free text |
| `operator_rank` | Order items by a criterion |
| `operator_compare` | Compare items focused on a specific axis |
| `operator_summarize` | Summarize content (citation-aware via `cited=True`) |
| `operator_generate` | Generate text following a template, grounded in `context` |
| `operator_filter` | Narrow items by a natural-language criterion (RDR-088 §D.4). Returns `{items, rationale[{id, reason}]}` |
| `operator_check` | Cross-item consistency probe (RDR-088 §D.2). Returns `{ok, evidence[{item_id, quote, role}]}` |
| `operator_verify` | Single-claim verification against one evidence source (RDR-088 §D.2). Returns `{verified, reason, citations[]}` |

### Orchestration (RDR-080)

| Tool | Purpose |
|---|---|
| `nx_answer` | Retrieval trunk: `plan_match` → `plan_run` → record. Plan-miss falls through to an inline `claude -p` planner. See [Querying Guide § nx_answer](querying-guide.md#conexusquery-skill--nx_answer-mcp-tool-analytical-queries) |
| `nx_tidy` | Consolidate T3 knowledge entries on a topic |
| `nx_enrich_beads` | Enrich a bead with execution context (file paths, test commands, constraints) |
| `nx_plan_audit` | Audit a plan for correctness and codebase alignment |

All four `nx_*` tools are async with a configurable `timeout` (default 120s).

## `nexus-catalog` — document catalog (10 tools)

Full tool names follow `mcp__plugin_conexus_nexus-catalog__<tool>`. No redundant `catalog_` prefix on the short names.

| Tool | Purpose |
|---|---|
| `search` | Metadata search across the catalog (title, author, corpus, file path) |
| `show` | Full metadata + all inbound/outbound links for a document |
| `list` | Browse catalog entries with filters (type, subtree, owner) |
| `register` | Add a new document to the catalog |
| `update` | Update metadata on an existing catalog entry |
| `link` | Create a typed link between two documents |
| `links` | Return live links for a document (deleted nodes excluded). Optional BFS via `depth` |
| `link_query` | Query the full link table including orphans (admin / audit view) |
| `resolve` | Resolve a file path, title, or tumbler to a catalog entry |
| `stats` | Summary stats — total entries, link counts by type, orphan counts |

## `devonthink` — DEVONthink agent surface (RDR-139 Layer A')

Full tool names follow `mcp__plugin_conexus_devonthink__<tool>`. This server is
the agent-facing face of the DEVONthink integration: a nexus-owned MCP server
that proxies a curated slice of DEVONthink's built-in MCP plus nexus composites.

It **always spawns** and gates internally on `available()`. When DEVONthink is
reachable it advertises the curated surface below; when DEVONthink is absent it
advertises only `devonthink_status` (zero DT tools), still exits 0, and never
errors a DT-less consumer. The `conexus/.mcp.json` entry carries
`alwaysLoad:false` purely as a tool-search startup optimization — the internal
gate is the optionality mechanism, not the `.mcp.json` flag.

The agent's DEVONthink entry point is a record UUID obtained from a **nexus**
search (DT's own selectors stay on the `nx dt` CLI, out of scope here), after
which it uses these tools and the `dt_incorporate` composite.

| Tool | Purpose |
|---|---|
| `devonthink_status` | Whether DT is reachable + open-database count (the always-present stub) |
| `find_similar_records` | DT AI "See Also" neighbours of a record |
| `classify_record` | DT AI suggested groups for a record |
| `extract_record_content` | AI-optimised text of a record |
| `extract_record_highlights` / `extract_record_mentions` | Markdown summary of a record's highlights / mentions |
| `get_record_text` / `get_record_annotation` | A record's body / annotation note |
| `get_record_links` / `get_databases` | A record's item links / the open databases |
| `resolve_doi_metadata` / `search_crossref` / `resolve_google_books_metadata` | Bibliographic resolution (CrossRef / Google Books) |
| `capture_web_page` / `download_pdf_from_doi` / `import_file` | Capture a URL / DOI PDF / loose file into DT |
| `dt_incorporate` | Composite: for an already-indexed record, create DT-derived `relates` edges (Layer B) and stamp the nexus identity back onto the DT record (Layer F), in one agent call |

## CLI-only operations

Some operations are intentionally not exposed as MCP tools — they are destructive, expensive, or maintenance tasks where human-in-the-loop confirmation matters. Available via `nx` CLI only.

| CLI command | Why not MCP |
|---|---|
| `nx store delete` | Destructive T3 document deletion |
| `nx collection info` | Expensive ChromaDB introspection, human-debugging shape |
| `nx collection verify` | Full-collection scan; rarely needed by agents |
| `nx catalog unlink` | Destructive edge removal |
| `nx catalog link-audit` | Full-graph scan, operator-oriented |
| `nx catalog link-bulk-delete` | Bulk link deletion by filter; high blast radius |
| `nx taxonomy *` | Topic curation tasks (discover, review, merge, split, rebuild). Agents benefit from taxonomy via the automatic boost in `search`/`query` |

The Python functions still exist in `src/nexus/mcp/core.py` and `src/nexus/mcp/catalog.py`; they just lack the `@mcp.tool()` decorator.

## Routing rule of thumb

| Task | Server | Tool |
|---|---|---|
| Find code that handles retries | `nexus` | `search` |
| Search within a topic | `nexus` | `search` with `topic=` |
| Summarize papers by an author | `nexus` | `query` with `author=` |
| What RDRs cite this paper? | `nexus-catalog` | `links` with `link_type="cites"` |
| What collection is this paper in? | `nexus-catalog` | `search` or `resolve` |
| Persist a research finding | `nexus` | `store_put` |
| Remember for next session | `nexus` | `memory_put` |
| Share a hypothesis with a sibling agent | `nexus` | `scratch` |
| Cache a query plan for reuse | `nexus` | `plan_save` |

Content (chunks, documents, notes) is on `nexus`; metadata and relationships (entries, typed links, tumblers) are on `nexus-catalog`. `query` crosses the boundary — it uses catalog metadata to scope a content search.

## Pagination

Three tools return paged results and accept `offset`: `search`, `store_list`, `memory_search`. Response footer:

```
--- showing 1-20 of 57. next: offset=20
--- showing 41-57 of 57. (end)
```

Pass `offset=N` back to the same tool to fetch the next page. Default page size: 20 for list-style tools; `n_results` for `search`.

## Permission auto-approval

The plugin installs a `PermissionRequest` hook that auto-approves any tool call matching `mcp__plugin_conexus_.*`. This covers both servers plus the bundled `sequential-thinking` server. Dangerous system operations (force-push, `bd delete`, deploys) are not matched and stay behind the normal confirmation flow.

To enforce stricter permission boundaries on a custom agent, narrow the matcher in `conexus/hooks/hooks.json`.

## See also

- [Querying Guide](querying-guide.md) — when to use which interface, the `nx_answer` trunk, operator bundling, search quality features
- [Document Catalog](catalog.md) — what the catalog is, link types, purposes, topic taxonomy
- [Architecture § Module Map](architecture.md#module-map) — internal module layout
- [CLI Reference — nx catalog](cli-reference.md#nx-catalog) — CLI equivalents for catalog tools
