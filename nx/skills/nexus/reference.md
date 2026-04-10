# Nexus — Agent Usage Guide

Nexus provides MCP tools for semantic search, persistent memory, and knowledge management across sessions.

**Three storage tiers:**
- **T1 scratch** — session-scoped (`scratch`, `scratch_manage` tools)
- **T2 memory** — local SQLite, survives restarts (`memory_put`, `memory_get`, `memory_delete`, `memory_search` tools)
- **T3 knowledge** — ChromaDB cloud + Voyage AI, permanent (`search`, `store_put`, `store_get`, `store_list` tools)

## MCP Tool Reference

Core tools are prefixed `mcp__plugin_nx_nexus__`; catalog tools are prefixed `mcp__plugin_nx_nexus-catalog__`.

There are 15 core tools: `search`, `query`, `store_put`, `store_get`, `store_list`, `memory_put`, `memory_get`, `memory_delete`, `memory_search`, `memory_consolidate`, `scratch`, `scratch_manage`, `collection_list`, `plan_save`, `plan_search`.
There are 10 catalog tools (nexus-catalog server): `search`, `show`, `list`, `register`, `update`, `link`, `links`, `link_query`, `resolve`, `stats`.

### search

Semantic search across T3 knowledge collections.

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `query` | str | required | Search query string |
| `corpus` | str | `"knowledge,code,docs"` | Corpus prefix, comma-separated list of prefixes, full collection name, or `"all"` to search every T3 collection |
| `limit` | int | `10` | Page size (results per page) |
| `offset` | int | `0` | Skip this many results. Footer shows `next: offset=N` for next page |
| `where` | str | `""` | Metadata filter: `KEY=VALUE` or `KEY>=VALUE`, comma-separated. Operators: `=`, `>=`, `<=`, `>`, `<`, `!=`. Numeric fields auto-coerced: `bib_year`, `bib_citation_count`, `page_count` |
| `cluster_by` | str | `""` | Set to `"semantic"` to group results by Ward hierarchical clustering. Each result gets `_cluster_label` metadata |

```
mcp__plugin_nx_nexus__search(query="query"                                  # knowledge + code + docs (default)
mcp__plugin_nx_nexus__search(query="query", corpus="all"                    # all T3 collections
mcp__plugin_nx_nexus__search(query="query", corpus="code"                   # code collections only
mcp__plugin_nx_nexus__search(query="query", corpus="knowledge__art", limit=15  # specific collection
mcp__plugin_nx_nexus__search(query="query", where="bib_year>=2023"          # filter by year
mcp__plugin_nx_nexus__search(query="query", where="section_type!=references" # exclude reference sections
mcp__plugin_nx_nexus__search(query="query", cluster_by="semantic"           # group results by topic
```

**Automatic quality features**: Results are automatically filtered by per-corpus distance thresholds (cloud/Voyage only), knowledge/docs/rdr collections over-fetch at 4x, and high-selectivity metadata filters route through the catalog for faster retrieval.

### query

Document-level semantic search for analytical questions. Unlike `search` which returns individual chunks, `query` groups results by source document and returns the best-matching snippet per document with full metadata.

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `question` | str | required | Natural-language research question |
| `corpus` | str | `"knowledge"` | Corpus prefix or full collection name. `"all"` for all corpora |
| `where` | str | `""` | Metadata filter: `KEY=VALUE`, comma-separated. Same syntax as `search` |
| `limit` | int | `10` | Maximum documents to return |

```
mcp__plugin_nx_nexus__query(question="adaptive resonance theory cortical maps"
mcp__plugin_nx_nexus__query(question="speech processing", corpus="knowledge__art", where="page_count>=50"
mcp__plugin_nx_nexus__query(question="error handling patterns", corpus="code", limit=5
```

Returns per-document: title, relevance score, bibliographic metadata (year, authors, venue, citations), technical metadata (pages, chunks, extraction method, formulas), collection, and best matching snippet.

Use `search` for chunk-level retrieval. Use `query` when you need to know **which documents** match.

### store_put

Store content in the T3 permanent knowledge store.

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `content` | str | required | Text content to store |
| `collection` | str | `"knowledge"` | Collection name or prefix |
| `title` | str | `""` | Document title (recommended for dedup) |
| `tags` | str | `""` | Comma-separated tags |
| `ttl` | str | `"permanent"` | TTL: `Nd`, `Nw`, or `"permanent"` |

```
mcp__plugin_nx_nexus__store_put(content="finding text", collection="knowledge", title="research-topic", tags="arch"
mcp__plugin_nx_nexus__store_put(content="notes", collection="knowledge", title="sprint-notes", ttl="30d"
```

**TTL formats**: `30d` (30 days), `4w` (4 weeks), `permanent` or `never` (no expiry).

### store_get

Retrieve the full content and metadata of a T3 entry by document ID.

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `doc_id` | str | required | Document ID (from store_list or store_put output) |
| `collection` | str | `"knowledge"` | Collection name or prefix |

```
mcp__plugin_nx_nexus__store_get(doc_id="a1b2c3d4e5f6g7h8", collection="knowledge__notes"
```

### store_list

List entries in a T3 knowledge collection.

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `collection` | str | `"knowledge"` | Collection name or prefix |
| `limit` | int | `20` | Page size |
| `offset` | int | `0` | Skip this many entries. Footer shows `next: offset=N` for next page |
| `docs` | bool | `false` | Show unique documents instead of individual chunks. Deduplicates by content_hash, shows title, chunk count, page count, extraction method |

```
mcp__plugin_nx_nexus__store_list(collection="knowledge"
mcp__plugin_nx_nexus__store_list(collection="knowledge__art", docs=true       # document-level view
mcp__plugin_nx_nexus__store_list(collection="knowledge__notes", limit=50, offset=100
```

### memory_put

Store a memory entry in T2 (SQLite). Upserts by (project, title).

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `content` | str | required | Text content to store |
| `project` | str | required | Project namespace |
| `title` | str | required | Entry title (unique within project) |
| `tags` | str | `""` | Comma-separated tags |
| `ttl` | int | `30` | Time-to-live in days (0 for permanent) |

```
mcp__plugin_nx_nexus__memory_put(content="content", project="{repo}", title="findings.md"
mcp__plugin_nx_nexus__memory_put(content="content", project="{repo}", title="findings.md", ttl=0
```

**Project naming**: Use purpose-specific suffixes:
- bare `{repo}` — general project memory and notes
- `{repo}_rdr` — RDR documents and gate results

### memory_get

Retrieve a memory entry by project and title.

When title is empty, lists all entries for the project (titles only — a second call with the specific title is required to get content).

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `project` | str | required | Project namespace |
| `title` | str | `""` | Entry title. Leave empty to LIST entries (titles only) |

```
mcp__plugin_nx_nexus__memory_get(project="{repo}", title="findings.md"       # get content
mcp__plugin_nx_nexus__memory_get(project="{repo}", title=""                   # list titles only
```

### memory_delete

Delete a T2 memory entry by project and title.

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `project` | str | required | Project namespace |
| `title` | str | required | Entry title to delete |

```
mcp__plugin_nx_nexus__memory_delete(project="{repo}", title="stale-finding"
```

### memory_search

Full-text search across T2 memory entries. Searches title, content, and tags fields via FTS5.

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `query` | str | required | Search query (FTS5 syntax — matches tokens in title, content, and tags) |
| `project` | str | `""` | Optional project filter |
| `limit` | int | `20` | Page size |
| `offset` | int | `0` | Skip this many results. Footer shows `next: offset=N` for next page |

```
mcp__plugin_nx_nexus__memory_search(query="query"
mcp__plugin_nx_nexus__memory_search(query="query", project="{repo}"
```

### memory_consolidate

T2 memory hygiene (RDR-061 E6): find overlapping entries, merge duplicates, flag stale entries. Merge is destructive and gated by `dry_run` and `confirm_destructive`.

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `action` | str | required | `"find-overlaps"`, `"merge"`, `"flag-stale"` |
| `project` | str | required | T2 project namespace |
| `min_similarity` | float | `0.7` | Jaccard threshold for `find-overlaps` |
| `idle_days` | int | `30` | Staleness threshold for `flag-stale` |
| `keep_id` | int | `0` | Entry ID to keep when merging (must be > 0) |
| `delete_ids` | str | `""` | Comma-separated IDs to delete during merge |
| `merged_content` | str | `""` | Replacement content for kept entry |
| `dry_run` | bool | `False` | Preview merge without modifying T2 |
| `confirm_destructive` | bool | `False` | Required when merging > 1 entry |

```
mcp__plugin_nx_nexus__memory_consolidate(action="find-overlaps", project="{repo}"
mcp__plugin_nx_nexus__memory_consolidate(action="flag-stale", project="{repo}", idle_days=30
mcp__plugin_nx_nexus__memory_consolidate(action="merge", project="{repo}",
    keep_id=42, delete_ids="43", merged_content="...", dry_run=True
mcp__plugin_nx_nexus__memory_consolidate(action="merge", project="{repo}",
    keep_id=42, delete_ids="43,44,45", merged_content="...", confirm_destructive=True
```

Merge safety: aborts with `KeyError` if `keep_id` does not exist (prevents data loss when `expire()` races with merge). `delete_ids` are preserved in that case.

### scratch

T1 session scratch pad — ephemeral within-session storage.

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `action` | str | required | `"put"`, `"search"`, `"list"`, `"get"`, `"delete"` |
| `content` | str | `""` | Content to store (for `"put"`) |
| `query` | str | `""` | Search query (for `"search"`) |
| `tags` | str | `""` | Comma-separated tags (for `"put"`) |
| `entry_id` | str | `""` | Entry ID (for `"get"`, `"delete"`) |
| `limit` | int | `10` | Max results for search |

```
mcp__plugin_nx_nexus__scratch(action="put", content="working hypothesis: the cache is stale"
mcp__plugin_nx_nexus__scratch(action="search", query="cache"
mcp__plugin_nx_nexus__scratch(action="list"
mcp__plugin_nx_nexus__scratch(action="get", entry_id="<id>"
mcp__plugin_nx_nexus__scratch(action="delete", entry_id="<id>"
```

### scratch_manage

Manage scratch entries: flag for persistence or promote to T2.

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `action` | str | required | `"flag"`, `"promote"` |
| `entry_id` | str | required | Scratch entry ID |
| `project` | str | `""` | Target project (required for promote) |
| `title` | str | `""` | Target title (required for promote) |

```
mcp__plugin_nx_nexus__scratch_manage(action="flag", entry_id="<id>"
mcp__plugin_nx_nexus__scratch_manage(action="promote", entry_id="<id>", project="{repo}", title="findings.md"
```

**Promote return value** (RDR-057 Phase 1c): The promote action returns a `PromotionReport` rendered in the response string:
- `(action=new)` — clean write, no overlap detected
- `(action=overlap_detected)` — FTS5 found a similar existing T2 entry. The new entry is still written as a separate row; the agent must decide whether to also call `memory_consolidate(action="merge")` to dedupe.

**Usage pattern**: Use T1 scratch for in-flight working notes. Flag important items so they auto-promote to T2 at session end. Permanently validated findings go to T3 via store_put.

### collection_list

List all T3 collections with document counts.

```
mcp__plugin_nx_nexus__collection_list()
```

Returns collection names, document counts, and embedding models for every collection in the T3 database.

### plan_save

Save a query execution plan to the T2 plan library.

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `query` | str | required | Original natural-language question |
| `plan_json` | str | required | JSON string: `{"steps": [...], "tools_used": [...], "outcome_notes": "..."}` |
| `project` | str | `""` | Project namespace |
| `outcome` | str | `"success"` | Plan outcome: "success" or "partial" |
| `tags` | str | `""` | Comma-separated tags |

```
Use plan_save tool: query="How many ART papers in T3?", plan_json='{"steps": ["collection_list", "store_list docs=true"], "tools_used": ["collection_list", "store_list"], "outcome_notes": "78 papers found"}', project="nexus"
```

### plan_search

Search the T2 plan library for similar query plans.

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `query` | str | required | Search query |
| `project` | str | `""` | Optional project filter |
| `limit` | int | `5` | Maximum results |

```
Use plan_search tool: query="enumerate documents", project="nexus"
```

## Indexing (CLI only)

Indexing operations have no MCP equivalent — use `nx` CLI via Bash:

```bash
nx index repo <path>                       # register and index a repo
nx index repo <path> --frecency-only       # refresh git frecency scores only (fast)
nx index repo <path> --chunk-size 80       # smaller chunks for better precision
nx index pdf <path> --corpus my-papers
nx index pdf --dir <path> --collection knowledge__art  # batch index directory
nx index md  <path> --corpus notes
```

## Health and Server (CLI only)

```bash
nx doctor                                  # verify all credentials and tools
nx mineru start                            # start MinerU PDF extraction server
nx mineru status                           # check server health
nx mineru stop                             # stop server
```

## Workflow — when and why to use each tier

**Session lifecycle:**
1. Search T3 for prior art before starting work: mcp__plugin_nx_nexus__search(`query="topic", corpus="knowledge"`
2. Index the codebase once per repo: `nx index repo <path>` (CLI)
3. Use T1 scratch for working notes during the session
4. Flag important scratch items for auto-promote to T2: mcp__plugin_nx_nexus__scratch_manage(`action="flag", entry_id="<id>"`
5. Persist validated findings to T3 at session end: Use store_put tool

**Tier selection:**
- **T1 scratch**: hypotheses, interim findings, checkpoints — anything ephemeral to this session
- **T2 memory**: cross-session state, agent relay notes, active project context
- **T3 knowledge**: validated findings, architectural decisions, reusable patterns — anything worth keeping permanently

**Collection naming**: always `__` as separator — `code__myrepo`, `docs__corpus`, `knowledge__topic`. Colons are invalid in ChromaDB collection names.

**Title conventions** (use hyphens, not colons):
- `research-{topic}` — research findings
- `decision-{component}-{name}` — architectural decisions
- `pattern-{name}` — reusable patterns
- `debug-{component}-{issue}` — debugging insights

## T2 Search

memory_search uses FTS5 full-text search — it matches literal tokens, not semantic meaning. Rules:

- Searches all three fields: **title**, **content**, and **tags**
- Use exact terms from the stored document, not conceptual paraphrases
- `"retain slash commands"` works if those words appear verbatim
- Multi-word queries are AND-matched: all tokens must appear somewhere in the document
- When results are empty: drop one term at a time to identify which token has no match. Use memory_get with empty title to browse titles directly

## Code Search: When to Use search vs Grep

Use **Grep** (the Grep tool) for:
- Finding a class or function by name: `class EmbeddingClient`
- Locating all usages of a symbol: `EmbeddingClient`
- Exact text matches: error messages, config keys, import paths

Use **search tool** with `corpus="code"` for:
- Conceptual queries when you don't know the file name or function name
- Finding "what handles PDF processing" across an unfamiliar codebase
- Cross-file concept queries: "retry logic with exponential backoff"
