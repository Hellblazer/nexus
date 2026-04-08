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
    │     links: cites, implements-heuristic, supersedes, relates
    │     auto-generate: citation links (bib metadata), code-RDR (heuristic)
    │     MCP: catalog_search, catalog_show, catalog_links, catalog_link, catalog_link_query, catalog_link_audit
    │     CLI: nx catalog setup/search/show/links/link/unlink/stats
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

## Module Map

| Area | Files | What they do |
|------|-------|-------------|
| **Entry** | `cli.py`, `commands/` | Click CLI, one file per command group |
| **Catalog** | `catalog/catalog.py`, `catalog_db.py`, `tumbler.py`, `link_generator.py`, `auto_linker.py` | Git-backed document registry + typed link graph (JSONL + SQLite). Tumbler addressing, `descendants()`/`ancestors()`/`lca()` hierarchy helpers, `resolve_chunk()` ghost element resolution, idempotent link upsert, composable query, bulk ops, audit. Auto-linker creates links from T1 link-context on every `store_put` |
| **Storage** | `db/t1.py`, `db/t2.py`, `db/t3.py` | Tier implementations. Plans table has `ttl` column for auto-expiry |
| **Indexing** | `indexer.py`, `code_indexer.py`, `prose_indexer.py`, `index_context.py`, `indexer_utils.py`, `classifier.py`, `chunker.py`, `md_chunker.py`, `doc_indexer.py`, `pdf_extractor.py`, `pdf_chunker.py`, `bib_enricher.py`, `languages.py`, `pipeline_buffer.py`, `pipeline_stages.py`, `checkpoint.py` | Repo indexing pipeline (decomposed per RDR-032). `bib_enricher.py` queries Semantic Scholar for bibliographic metadata; `pdf_extractor.py` auto-detects math-heavy PDFs via FormulaItem counting and routes to MinerU (optional `conexus[mineru]` extra) for superior LaTeX extraction, falling back through enriched Docling to PyMuPDF normalized. MinerU processes large PDFs in 5-page subprocess batches for memory isolation (prevents OOM on formula-dense documents). Chunk metadata includes `has_formulas` boolean. `pipeline_buffer.py` provides a WAL-mode SQLite buffer for the three-stage streaming pipeline (RDR-048); `pipeline_stages.py` implements the concurrent extractor/chunker/uploader stages and orchestrator; `checkpoint.py` handles batch-path crash recovery for smaller documents (RDR-047) |
| **Export** | `exporter.py` | Collection export/import for T3 backup and migration (.nxexp format) |
| **Search** | `search_engine.py`, `search_clusterer.py`, `scoring.py`, `frecency.py`, `ripgrep_cache.py`, `filters.py` | Query, rank, rerank, Ward hierarchical clustering, shared where-filter parsing |
| **Hooks** | `commands/hooks.py` | Git hook install/uninstall/status, sentinel-bounded stanza management |
| **Verification** | `config.py` (verification section), `nx/hooks/scripts/stop_verification_hook.sh`, `nx/hooks/scripts/pre_close_verification_hook.sh`, `nx/hooks/scripts/read_verification_config.py` | Opt-in mechanical enforcement: Stop hook (session-end checks), PreToolUse hook (bd-close gate), standalone config reader. See [Configuration — Verification](configuration.md#verification) |
| **MCP Server** | `mcp_server.py`, `mcp_infra.py` | FastMCP server: tool definitions in `mcp_server.py`, infrastructure (singletons, caching, test injection) in `mcp_infra.py`. `query()` has catalog-aware routing (author, content_type, subtree, follow_links, depth). Storage: `search`, `store_put`, `store_list`, `memory_*`, `scratch_*`, `collection_*`, `plan_*`. Catalog: `catalog_search`, `catalog_show`, `catalog_links`, `catalog_link`, `catalog_unlink`, `catalog_link_query`, `catalog_link_audit`, `catalog_link_bulk`, `catalog_resolve` |
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
