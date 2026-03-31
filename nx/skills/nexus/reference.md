# Nexus — Agent Usage Guide

Nexus provides MCP tools for semantic search, persistent memory, and knowledge management across sessions.

**Three storage tiers:**
- **T1 scratch** — session-scoped (`scratch`, `scratch_manage` tools)
- **T2 memory** — local SQLite, survives restarts (`memory_put`, `memory_get`, `memory_search` tools)
- **T3 knowledge** — ChromaDB cloud + Voyage AI, permanent (`search`, `store_put`, `store_list` tools)

## MCP Tool Reference

All nexus MCP tools are prefixed `mcp__plugin_nx_nexus__` in Claude Code.

There are 12 tools in total: `search`, `store_put`, `store_list`, `memory_put`, `memory_get`, `memory_search`, `scratch`, `scratch_manage`, `collection_list`, `collection_info`, `collection_verify`.

### search

Semantic search across T3 knowledge collections.

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `query` | str | required | Search query string |
| `corpus` | str | `"knowledge,code,docs"` | Corpus prefix, comma-separated list of prefixes, full collection name, or `"all"` to search every T3 collection |
| `n` | int | `10` | Page size (results per page) |
| `offset` | int | `0` | Skip this many results. Footer shows `next: offset=N` for next page |

```
Use search tool: query="query"                                  # knowledge + code + docs (default)
Use search tool: query="query", corpus="all"                    # all T3 collections
Use search tool: query="query", corpus="code"                   # code collections only
Use search tool: query="query", corpus="docs"                   # docs collections only
Use search tool: query="query", corpus="knowledge"              # knowledge collections only
Use search tool: query="query", corpus="code__myrepo", n=15     # specific collection
Use search tool: query="query", corpus="knowledge,rdr"          # multiple prefixes
```

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

### store_list

List entries in a T3 knowledge collection.

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `collection` | str | `"knowledge"` | Collection name or prefix |
| `limit` | int | `20` | Page size |
| `offset` | int | `0` | Skip this many entries. Footer shows `next: offset=N` for next page |

```
Use store_list tool: collection="knowledge"
Use store_list tool: collection="knowledge__notes", limit=50
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

Retrieve a memory entry by project and title, or list entries if title is empty.

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `project` | str | required | Project namespace |
| `title` | str | `""` | Entry title (empty = list all entries) |

```
Use memory_get tool: project="{repo}", title="findings.md"
Use memory_get tool: project="{repo}", title=""              # list all entries
```

### memory_search

Full-text search across T2 memory entries.

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `query` | str | required | Search query (FTS5 syntax) |
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
| `action` | str | required | `"put"`, `"search"`, `"list"`, `"get"` |
| `content` | str | `""` | Content to store (for `"put"`) |
| `query` | str | `""` | Search query (for `"search"`) |
| `tags` | str | `""` | Comma-separated tags (for `"put"`) |
| `entry_id` | str | `""` | Entry ID (for `"get"`) |
| `n` | int | `10` | Max results for search |

```
Use scratch tool: action="put", content="working hypothesis: the cache is stale"
Use scratch tool: action="search", query="cache"
Use scratch tool: action="list"
Use scratch tool: action="get", entry_id="<id>"
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

Get detailed information about a T3 collection (count, index/query models).

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `name` | str | required | Full collection name (e.g. `knowledge__notes`) |

```
Use collection_info tool: name="knowledge__notes"
Use collection_info tool: name="code__myrepo"
```

### collection_verify

Verify a collection's retrieval health via known-document probe.

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `name` | str | required | Full collection name to verify |

Embeds a known document from the collection and queries it back, reporting the retrieval distance. A distance near 0 indicates healthy embedding round-trips; high distances indicate a model mismatch or corrupted index.

```
Use collection_verify tool: name="knowledge__notes"
```

## Indexing (CLI only)

Indexing operations have no MCP equivalent — use `nx` CLI via Bash:

```bash
nx index repo <path>                       # register and index a repo
nx index repo <path> --frecency-only       # refresh git frecency scores only (fast)
nx index repo <path> --chunk-size 80       # smaller chunks for better precision
nx index pdf <path> --corpus my-papers
nx index md  <path> --corpus notes
```

## Health and Server (CLI only)

```bash
nx doctor                                  # verify all credentials and tools
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

## T2 Search Constraints

memory_search uses FTS5 full-text search — it matches literal tokens, not semantic meaning. Rules:

- Use exact terms from the stored document, not conceptual paraphrases
- `"retain slash commands"` works if those words appear verbatim in content
- Multi-word queries are AND-matched: all tokens must appear somewhere in the document
- When results are empty: drop one term at a time to identify which token has no match. Use memory_get with empty title to browse titles directly
- **Title searches always return empty**: the FTS5 index covers `content` and `tags` only — not `title`. Use memory_get with the exact title for title-based lookup.

## Code Search: When to Use search vs Grep

Use **Grep** (the Grep tool) for:
- Finding a class or function by name: `class EmbeddingClient`
- Locating all usages of a symbol: `EmbeddingClient`
- Exact text matches: error messages, config keys, import paths

Use **search tool** with `corpus="code"` for:
- Conceptual queries when you don't know the file name or function name
- Finding "what handles PDF processing" across an unfamiliar codebase
- Cross-file concept queries: "retry logic with exponential backoff"
