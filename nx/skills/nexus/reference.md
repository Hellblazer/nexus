# Nexus — Agent Usage Guide

Nexus provides MCP tools for semantic search, persistent memory, and knowledge management across sessions.

**Three storage tiers:**
- **T1 scratch** — session-scoped (`scratch`, `scratch_manage` tools)
- **T2 memory** — local SQLite, survives restarts (`memory_put`, `memory_get`, `memory_delete`, `memory_search` tools)
- **T3 knowledge** — ChromaDB cloud + Voyage AI, permanent (`search`, `store_put`, `store_get`, `store_list`, `store_delete` tools)

## MCP Tool Reference

All nexus MCP tools are prefixed `mcp__plugin_nx_nexus__` in Claude Code.

There are 17 tools: `search`, `query`, `store_put`, `store_get`, `store_list`, `store_delete`, `memory_put`, `memory_get`, `memory_delete`, `memory_search`, `scratch`, `scratch_manage`, `collection_list`, `collection_info`, `collection_verify`, `plan_save`, `plan_search`.

### search

Semantic search across T3 knowledge collections.

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `query` | str | required | Search query string |
| `corpus` | str | `"knowledge,code,docs"` | Corpus prefix, comma-separated list of prefixes, full collection name, or `"all"` to search every T3 collection |
| `limit` | int | `10` | Page size (results per page) |
| `offset` | int | `0` | Skip this many results. Footer shows `next: offset=N` for next page |
| `where` | str | `""` | Metadata filter: `KEY=VALUE` or `KEY>=VALUE`, comma-separated. Operators: `=`, `>=`, `<=`, `>`, `<`, `!=`. Numeric fields auto-coerced: `bib_year`, `bib_citation_count`, `page_count` |

```
Use search tool: query="query"                                  # knowledge + code + docs (default)
Use search tool: query="query", corpus="all"                    # all T3 collections
Use search tool: query="query", corpus="code"                   # code collections only
Use search tool: query="query", corpus="knowledge__art", limit=15  # specific collection
Use search tool: query="query", where="bib_year>=2023"          # filter by year
Use search tool: query="query", where="tags=arch,bib_year>=2020" # multiple filters
```

### query

Document-level semantic search for analytical questions. Unlike `search` which returns individual chunks, `query` groups results by source document and returns the best-matching snippet per document with full metadata.

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `question` | str | required | Natural-language research question |
| `corpus` | str | `"knowledge"` | Corpus prefix or full collection name. `"all"` for all corpora |
| `where` | str | `""` | Metadata filter: `KEY=VALUE`, comma-separated. Same syntax as `search` |
| `limit` | int | `10` | Maximum documents to return |

```
Use query tool: question="adaptive resonance theory cortical maps"
Use query tool: question="speech processing", corpus="knowledge__art", where="page_count>=50"
Use query tool: question="error handling patterns", corpus="code", limit=5
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
Use store_put tool: content="finding text", collection="knowledge", title="research-topic", tags="arch"
Use store_put tool: content="notes", collection="knowledge", title="sprint-notes", ttl="30d"
```

**TTL formats**: `30d` (30 days), `4w` (4 weeks), `permanent` or `never` (no expiry).

### store_get

Retrieve the full content and metadata of a T3 entry by document ID.

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `doc_id` | str | required | Document ID (from store_list or store_put output) |
| `collection` | str | `"knowledge"` | Collection name or prefix |

```
Use store_get tool: doc_id="a1b2c3d4e5f6g7h8", collection="knowledge__notes"
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
Use store_list tool: collection="knowledge"
Use store_list tool: collection="knowledge__art", docs=true       # document-level view
Use store_list tool: collection="knowledge__notes", limit=50, offset=100
```

### store_delete

Delete a T3 knowledge entry by document ID.

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `doc_id` | str | required | Document ID to delete |
| `collection` | str | `"knowledge"` | Collection name or prefix |

```
Use store_delete tool: doc_id="a1b2c3d4e5f6g7h8", collection="knowledge"
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
Use memory_put tool: content="content", project="{repo}", title="findings.md"
Use memory_put tool: content="content", project="{repo}", title="findings.md", ttl=0
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
Use memory_get tool: project="{repo}", title="findings.md"       # get content
Use memory_get tool: project="{repo}", title=""                   # list titles only
```

### memory_delete

Delete a T2 memory entry by project and title.

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `project` | str | required | Project namespace |
| `title` | str | required | Entry title to delete |

```
Use memory_delete tool: project="{repo}", title="stale-finding"
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
Use memory_search tool: query="query"
Use memory_search tool: query="query", project="{repo}"
```

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
Use scratch tool: action="put", content="working hypothesis: the cache is stale"
Use scratch tool: action="search", query="cache"
Use scratch tool: action="list"
Use scratch tool: action="get", entry_id="<id>"
Use scratch tool: action="delete", entry_id="<id>"
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
Use scratch_manage tool: action="flag", entry_id="<id>"
Use scratch_manage tool: action="promote", entry_id="<id>", project="{repo}", title="findings.md"
```

**Usage pattern**: Use T1 scratch for in-flight working notes. Flag important items so they auto-promote to T2 at session end. Permanently validated findings go to T3 via store_put.

### collection_list

List all T3 collections with document counts.

```
Use collection_list tool
```

Returns collection names, document counts, and embedding models for every collection in the T3 database.

### collection_info

Get detailed information about a T3 collection (count, models, sample entries).

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `name` | str | required | Full collection name (e.g. `knowledge__notes`) |

```
Use collection_info tool: name="knowledge__notes"
Use collection_info tool: name="code__myrepo"
```

Returns count, index/query models, and a peek at the first few entry titles for discoverability.

### collection_verify

Verify a collection's retrieval health via known-document probe.

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `name` | str | required | Full collection name to verify |

Embeds a known document from the collection and queries it back, reporting the retrieval distance. A distance near 0 indicates healthy embedding round-trips; high distances indicate a model mismatch or corrupted index.

```
Use collection_verify tool: name="knowledge__notes"
```

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
1. Search T3 for prior art before starting work: Use search tool: `query="topic", corpus="knowledge"`
2. Index the codebase once per repo: `nx index repo <path>` (CLI)
3. Use T1 scratch for working notes during the session
4. Flag important scratch items for auto-promote to T2: Use scratch_manage tool: `action="flag", entry_id="<id>"`
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
