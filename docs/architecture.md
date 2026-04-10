# Architecture

> When in doubt, check `src/nexus/` — the code is the ground truth.

## How It Fits Together

Nexus has three layers: a CLI (for humans) and an MCP server (for agents) that
talk to three storage tiers, an indexing pipeline that fills them, and a search
engine that queries across them.

```
Human                   Agent (Claude Code)
  │                         │
  ▼                         ▼
CLI (cli.py)            MCP Server (mcp_server.py)
  │                         │
  └──────────┬──────────────┘
             │
    ├── Index: classify → chunk → embed → store
    │     code: classify(SKIP|CODE|PROSE|PDF) → tree-sitter AST → context prefix → voyage-code-3 → code__<repo>
    │     prose: SemanticMarkdownChunker (md) or line-split → voyage-context-3 → docs__<repo>
    │     rdr:   SemanticMarkdownChunker → voyage-context-3 → rdr__<repo>
    │     pdf:   auto-detect routing (Docling → MinerU → PyMuPDF) → table/formula detection → bib enrichment → voyage-context-3 → docs__<corpus>
    │     skip:  .xml/.json/.yml/.html/.css/.lock/etc → silently ignored
    │
    ├── Search: query → retrieve → rerank → format
    │     semantic, hybrid (+ frecency + ripgrep)
    │
    ├── Catalog: JSONL truth → SQLite cache → typed link graph
    │     documents: tumbler addressing (1.owner.doc), FTS5 search
    │     links: cites, implements-heuristic, supersedes, relates, formalizes
    │     auto-generate: citation links (bib metadata), code-RDR (heuristic)
    │     MCP: nexus-catalog server — search, show, list, register, update,
    │          link, links, link_query, resolve, stats (short names, RDR-062)
    │     CLI: nx catalog setup/search/show/links/link/unlink/stats
    │     Demoted to CLI-only: catalog_unlink, catalog_link_audit, catalog_link_bulk
    │
    └── Storage tiers
          T1: ChromaDB HTTP server (session scratch, shared across agent processes)
          T2: SQLite + FTS5 (persistent memory, project context)
          T3: ChromaDB PersistentClient + ONNX (local, zero-config)
              OR ChromaDB Cloud + Voyage AI (cloud, higher quality)
                code__*       voyage-code-3 index + query
                docs__*       voyage-context-3 (CCE) index + query
                rdr__*        voyage-context-3 (CCE) index + query
                knowledge__*  voyage-context-3 (CCE) index + query
```

Data flows upward (T1 → T2 → T3).

## Catalog & Link Graph

The catalog is a document registry that sits alongside T3. While T3 stores document
*content* as vector embeddings, the catalog stores document *metadata* (title, author,
collection, tumbler address) and *relationships* (citations, implementations, supersedes).

**What populates it**: Indexing (`nx index repo`, `nx index pdf`, `nx index rdr`) auto-registers
entries via catalog hooks. MCP `store_put` also registers entries. `nx enrich` adds bibliographic
metadata and enables citation link generation.

**What agents use it for**: Finding which T3 collection a paper is in (`catalog_search` →
`physical_collection`), traversing citations (`catalog_links` with `link_type="cites"`),
and scoping semantic search to relevant collections instead of searching everything.

**Link types in use**:
- `cites` — citation relationships (auto-created by `nx enrich` from Semantic Scholar references)
- `implements-heuristic` — code→RDR links (auto-created by indexer from title substring matching)
- `supersedes` — created by RDR close and knowledge-tidier when documents are replaced
- `relates` — created by agents (debugger, deep-analyst, codebase-analyzer) linking related findings
- `implements`, `quotes`, `comments` — available for manual use

**Span formats** for sub-document link references:
- `42-57` — line range (positional, may become stale on re-index)
- `3:100-250` — chunk:char range (positional)
- `chash:<sha256hex>` — content-addressed chunk identity (preferred, survives re-indexing)
- `chash:<sha256hex>:<start>-<end>` — character range within a content-addressed chunk

Content-hash spans reference chunks by `chunk_text_hash` metadata (SHA-256 of stored chunk text). All 5 indexers (code, prose, doc PDF, doc markdown, streaming PDF pipeline) emit `chunk_text_hash` alongside the existing file-level `content_hash`. For existing collections, `nx catalog setup` or `nx collection backfill-hash` adds the field without re-embedding. `link_audit()` verifies chash spans resolve in T3.

**Tumbler ordering**: Comparison operators (`<`, `<=`, `>`, `>=`) use -1 sentinel padding for cross-depth ordering — parent tumblers sort before their children. `Tumbler.spans_overlap()` detects positional span overlap using these operators.

**Two graph views**: `catalog_links` returns only links between live documents (deleted nodes excluded).
`catalog_link_query` returns all links including orphans — useful for admin/audit.

**CCE single-chunk note**: For CCE collections (`docs__*`, `rdr__*`, `knowledge__*`), documents with only one chunk are embedded via `contextualized_embed(inputs=[[chunk]])`.

## T2 Domain Stores

`src/nexus/db/t2/` is a Python package split into four domain-specific
stores. Each store owns its own tables in a shared SQLite file and runs
against its own `sqlite3.Connection` in WAL mode. Reads in one domain
are never blocked by writes in another (the Phase 1 global Python
mutex is gone); concurrent writes across domains still serialize at
SQLite's single-writer WAL lock, but `busy_timeout=5000` absorbs the
brief contention without raising `OperationalError`.

| Store      | Class             | Attribute       | Responsibility                                                             |
|------------|-------------------|-----------------|----------------------------------------------------------------------------|
| Memory     | `MemoryStore`     | `db.memory`     | Persistent notes, project context, FTS5 search, access tracking, TTL       |
| Plans      | `PlanLibrary`     | `db.plans`      | Plan templates, plan search, plan TTL                                      |
| Taxonomy   | `CatalogTaxonomy` | `db.taxonomy`   | Topic clustering, topic assignment                                         |
| Telemetry  | `Telemetry`       | `db.telemetry`  | Relevance log (query/chunk/action triples), retention-based expiry         |

`T2Database` is a composing facade: it constructs the four stores in
order (memory → plans → taxonomy → telemetry), re-exposes their public
methods as thin delegates for backward compatibility, and runs
cross-domain operations like `expire()` over all of them. The facade
holds no database connection of its own — every SQL statement runs
through a specific domain store.

**Preferred call style for new code**:

```python
db = T2Database(path)
db.memory.search("fts query", project="myproj")   # domain method
db.plans.save_plan(query, plan_json)               # domain method
db.telemetry.log_relevance(query, ...)             # domain method
```

Existing call sites that use `db.search(...)`, `db.save_plan(...)`,
etc. continue to work via facade delegation — no migration required.

### Concurrency Model (RDR-063 Phase 2)

Phase 2 replaced a single shared connection with per-store connections:

| Phase      | Connection                | Lock                          | Cross-domain writes     |
|------------|---------------------------|-------------------------------|-------------------------|
| Phase 1    | one `SharedConnection`    | one `threading.Lock`          | serialized in Python    |
| Phase 2    | one per store             | one `threading.Lock` per store | coordinated in SQLite   |

Phase 2 consequences:

- **Cross-domain reads no longer block on unrelated writes**: a
  `memory_search` on one thread and a `plan_save` on another run in
  parallel because the Phase 1 shared Python mutex is gone. Concurrent
  *writes* across domains still serialize at SQLite's single-writer
  WAL lock, but `busy_timeout=5000` absorbs the brief queue so callers
  do not see `OperationalError: database is locked`.
- **Telemetry no longer interferes with search**: MCP relevance-log
  writes run on the telemetry connection, so `memory_search` is not
  blocked by access-tracking hooks.
- **Cluster rebuilds don't freeze memory**: `taxonomy.cluster_and_persist`
  runs on the taxonomy connection; the long numpy clustering phase holds
  no T2 locks, so interactive memory operations continue during the
  bulk of the rebuild. (The initial `memory.get_all()` snapshot read
  still briefly acquires `memory`'s lock, as any read does.)
- **Parallel writes to the same store are serialized** by that store's
  own `threading.Lock` plus the SQLite file-level write lock — callers
  never see `OperationalError: database is locked`.

**Migrations**: Each store owns its schema-migration guards and runs
them the first time it opens a given database path. The guards are
per-domain, so concurrent `T2Database` constructors on the same path
can each reach their own `_init_schema` without coordinating through a
single global migration lock. (`T2Database.__init__` itself constructs
the four stores sequentially in dependency order — the per-domain
guard matters for multi-process / multi-constructor races, not for the
sequential initialization inside a single `T2Database`.)

**In-memory SQLite**: Tests that want an ephemeral database should use
a temp file path, not `":memory:"` — `:memory:` databases are
per-connection, so the four stores would each see a distinct empty
database and `test_t2_concurrency.py` would no longer exercise the
cross-domain WAL path.

See `src/nexus/db/t2/__init__.py` for the facade source and
`tests/test_t2_concurrency.py` for the concurrency test suite.

## Module Map

| Area | Files | What they do |
|------|-------|-------------|
| **Entry** | `cli.py`, `commands/` | Click CLI, one file per command group |
| **Catalog** | `catalog/catalog.py`, `catalog_db.py`, `tumbler.py`, `link_generator.py`, `auto_linker.py` | Git-backed document registry + typed link graph (JSONL + SQLite). Tumbler addressing, `descendants()`/`ancestors()`/`lca()` hierarchy helpers, `resolve_chunk()` ghost element resolution, idempotent link upsert, composable query, bulk ops, audit. Auto-linker creates links from T1 link-context on every `store_put` |
| **Storage** | `db/t1.py`, `db/t2/`, `db/t3.py` | Tier implementations. T2 is a package split into four domain stores (see § T2 Domain Stores). Plans table has `ttl` column for auto-expiry |
| **Indexing** | `indexer.py`, `code_indexer.py`, `prose_indexer.py`, `index_context.py`, `indexer_utils.py`, `classifier.py`, `chunker.py`, `md_chunker.py`, `doc_indexer.py`, `pdf_extractor.py`, `pdf_chunker.py`, `bib_enricher.py`, `languages.py`, `pipeline_buffer.py`, `pipeline_stages.py`, `checkpoint.py` | Repo indexing pipeline (decomposed per RDR-032). `bib_enricher.py` queries Semantic Scholar for bibliographic metadata; `pdf_extractor.py` auto-detects math-heavy PDFs via FormulaItem counting and routes to MinerU (optional `conexus[mineru]` extra) for superior LaTeX extraction, falling back through enriched Docling to PyMuPDF normalized. MinerU processes large PDFs in 5-page subprocess batches for memory isolation (prevents OOM on formula-dense documents). Chunk metadata includes `has_formulas` boolean. `pipeline_buffer.py` provides a WAL-mode SQLite buffer for the three-stage streaming pipeline (RDR-048); `pipeline_stages.py` implements the concurrent extractor/chunker/uploader stages and orchestrator; `checkpoint.py` handles batch-path crash recovery for smaller documents (RDR-047) |
| **Export** | `exporter.py` | Collection export/import for T3 backup and migration (.nxexp format) |
| **Search** | `search_engine.py`, `search_clusterer.py`, `scoring.py`, `frecency.py`, `ripgrep_cache.py`, `filters.py` | Query, rank, rerank, Ward hierarchical clustering, shared where-filter parsing |
| **Hooks** | `commands/hooks.py` | Git hook install/uninstall/status, sentinel-bounded stanza management |
| **Verification** | `config.py` (verification section), `nx/hooks/scripts/stop_verification_hook.sh`, `nx/hooks/scripts/pre_close_verification_hook.sh`, `nx/hooks/scripts/read_verification_config.py` | Opt-in mechanical enforcement: Stop hook (session-end checks), PreToolUse hook (bd-close gate), standalone config reader. See [Configuration — Verification](configuration.md#verification) |
| **MCP Servers** | `mcp/core.py`, `mcp/catalog.py`, `mcp_infra.py`, `mcp_server.py` (shim) | Dual-server FastMCP architecture (RDR-062). **Core server (`nexus`, 15 tools)**: `search`, `query`, `store_put`, `store_get`, `store_list`, `memory_put`, `memory_get`, `memory_delete`, `memory_search`, `memory_consolidate`, `scratch`, `scratch_manage`, `collection_list`, `plan_save`, `plan_search`. **Catalog server (`nexus-catalog`, 10 tools)**: `search`, `show`, `list`, `register`, `update`, `link`, `links`, `link_query`, `resolve`, `stats` (short names — the `catalog_` prefix is dropped since the server namespace already provides context). **Demoted to CLI-only (6 tools)**: `store_delete`, `collection_info`, `collection_verify`, `catalog_unlink`, `catalog_link_audit`, `catalog_link_bulk`. Backward-compat shim at `mcp_server.py` re-exports all 30 functions. `query()` has catalog-aware routing (author, content_type, subtree, follow_links, depth). Singletons and test injection in `mcp_infra.py` |
| **Enrichment** | `bib_enricher.py`, `commands/enrich.py` | Semantic Scholar bibliographic metadata lookup + `nx enrich` CLI backfill command |
| **Support** | `config.py`, `registry.py`, `corpus.py`, `session.py`, `hooks.py`, `ttl.py`, `formatters.py`, `types.py`, `errors.py`, `retry.py` | Configuration, naming, formatting, session lifecycle, transient-error retry |

## Design Decisions

1. **Protocols over ABCs** — `typing.Protocol` for structural subtyping, no inheritance coupling.
2. **No ORM** — Direct `sqlite3` for T2. Schema is simple; WAL + FTS5 are stdlib.
3. **Constructor injection** — Dependencies via constructor, no global singletons.
4. **Ported, not imported** — SeaGOAT and Arcaneum patterns rewritten in Nexus module structure.
5. **PPID-chain session propagation** — The `SessionStart` hook starts a per-session ChromaDB HTTP server (using the `chroma` entry-point co-installed with the package) and writes its address to `~/.config/nexus/sessions/{ppid}.session`, keyed by the Claude Code process PID. Child agents walk the OS PPID chain to find the nearest ancestor session file and connect to the same server, sharing T1 scratch across the entire agent tree. Concurrent independent windows stay isolated via disjoint process trees. Falls back to `EphemeralClient` when the server cannot start or the PPID chain yields no record.

## Heritage

| Tool | What Nexus borrows |
|------|-------------------|
| **mgrep** | UX patterns, citation format, Claude Code integration |
| **SeaGOAT** | Git frecency scoring, hybrid search, persistent server |
| **Arcaneum** | PDF extraction + chunking pipelines, RDR process |

Storage (ChromaDB + Voyage AI) and embedding layers are Nexus's own.

For project origins and inspirations, see [historical.md](historical.md).
