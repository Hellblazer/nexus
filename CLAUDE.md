# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## License & Copyright

This repo is **AGPL-3.0** — see `LICENSE`. The license file covers all files; you do not need to annotate every file.

For Python source files, use the short SPDX form where conventional:
```python
# SPDX-License-Identifier: AGPL-3.0-or-later
```
Add a copyright line below for substantial new modules (not boilerplate):
```python
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
```

**Agent files, skill files, command `.md` files, config files**: no header needed — omit to save tokens.

## Development Commands

```bash
# Setup
uv sync                           # install deps
scripts/reinstall-tool.sh         # install nx CLI locally (preserves extras)

# Tests
uv run pytest                     # full unit suite (no API keys needed)
uv run pytest tests/test_indexer.py   # single file
uv run pytest -k "test_frecency"      # by name pattern
uv run pytest --cov=nexus             # with coverage
uv run pytest -m integration          # E2E (requires real API keys: copy .env.example → .env)

# After changes, reinstall CLI
uv sync && scripts/reinstall-tool.sh
nx --version
```

Unit tests use `chromadb.EphemeralClient` + bundled ONNX MiniLM — no API keys or network.

## Project Overview

Nexus is a Python 3.12+ CLI + persistent server for semantic search and knowledge management. Published on PyPI as **`conexus`**; the CLI entry point is **`nx`** (`src/nexus/` is the package).

**Three storage tiers:**
- T1: `chromadb.EphemeralClient` (or HTTP server via SessionStart hook) — session scratch (`nx scratch`)
- T2: SQLite + FTS5 — seven domain stores: `MemoryStore` (persistent notes + FTS5), `PlanLibrary` (plan templates), `CatalogTaxonomy` (topic clustering + assignment), `Telemetry` (relevance log), `ChashIndex` (content-hash chunk index, RDR-086), `DocumentAspects` (structured aspect rows, RDR-089), `AspectExtractionQueue` (async extractor WAL queue, RDR-089). Facade: `T2Database`. Plan tools: `plan_save(ttl=30)`/`plan_search` MCP tools, 12 builtin templates seeded at `nx catalog setup`.
- T3: `chromadb.PersistentClient` + local ONNX embeddings (local mode, zero-config) OR `chromadb.CloudClient` + `VoyageAIEmbeddingFunction` (cloud mode) — permanent knowledge (`nx store`, `nx search`)

**T3 ChromaDB database**: a single `chromadb.CloudClient` database (`CHROMA_DATABASE` value, e.g. `nexus`). All collection prefixes coexist in one database:
- `code__*` collections — `voyage-code-3` for both index and query
- `docs__*` collections — `voyage-context-3` (CCE) index + query
- `rdr__*` collections — `voyage-context-3`
- `knowledge__*` collections — `voyage-context-3`

**Collection naming**: always `__` as separator — `code__myrepo`, `docs__corpus`, `knowledge__topic` (colons are invalid in ChromaDB collection names).

**Single-chunk CCE**: Documents with only 1 chunk in CCE collections (`docs__*`, `knowledge__*`, `rdr__*`) are embedded via `contextualized_embed()` with `inputs=[[chunk]]`.

**Session propagation (T1)**: The `SessionStart` hook starts a per-session ChromaDB HTTP server, writes its address to `~/.config/nexus/sessions/{ppid}.session`. Child agents walk the OS PPID chain to find the nearest ancestor session file and share T1 scratch across the agent tree. Falls back to `EphemeralClient` when the server cannot start.

**Catalog (T3 metadata layer)**: Git-backed document registry that tracks *what* is indexed and *how documents relate*. JSONL files are the source of truth; SQLite + FTS5 is the query cache (rebuilt automatically on mtime change). Tumblers (hierarchical addresses like `1.2.5`) identify documents. Every indexing pathway (`index repo`, `index pdf`, `index rdr`, MCP `store_put`) auto-registers entries. `nx catalog setup` creates and populates the catalog in one step.

**Link graph**: Typed edges between documents (`cites`, `implements`, `implements-heuristic`, `supersedes`, `relates`, or custom). `created_by` tracks provenance. Three creation paths:

1. **Post-hoc** (batch, after indexing): `generate_citation_links()`, `generate_code_rdr_links()`, `generate_rdr_filepath_links()` in `link_generator.py`
2. **Auto-linker** (`auto_linker.py`): fires on every `store_put` MCP call, reads `link-context` from T1 scratch (tag: `link-context`), creates links to seeded targets. Skills seed before dispatch; agents self-seed from their task prompt when no context exists.
3. **Agent-direct**: agents call `catalog_link` MCP tool during work for precise typed links

**Two graph views**: `catalog_links` returns live-document links only. `catalog_link_query` returns all including orphans. The `query` MCP tool has catalog-aware routing (`author`, `content_type`, `subtree`, `follow_links`, `depth`) for scoped search.

**Pagination**: All list-returning tools include footers when truncated — `offset=N` for next page.

## Catalog Link Graph

When researching a file's design intent, use the link graph:

```bash
nx catalog links-for-file src/nexus/catalog/catalog.py   # linked RDRs/code
nx catalog session-summary                                 # recently modified files + links
nx catalog coverage                                        # link coverage by content type
nx catalog orphans --no-links                              # unlinked entries
```

In the `query` MCP tool, use `follow_links` with a link type to traverse the graph:
```
query("how does path resolution work", follow_links="implements", subtree="1.1")
```

**T3 expire guard**: always filter `ttl_days > 0 AND expires_at != "" AND expires_at < now` — the `expires_at != ""` guard is mandatory: permanent entries use `expires_at=""` which sorts before ISO timestamps and would be incorrectly deleted by a 2-condition guard.

## Source Layout

```
src/nexus/           # Core package
  cli.py             # Click entry point; registers all command groups
  commands/          # One file per CLI command group
    index.py         # nx index (repo, pdf, rdr)
    search_cmd.py    # nx search
    memory.py        # nx memory
    scratch.py       # nx scratch
    store.py         # nx store
    collection.py    # nx collection
    config_cmd.py    # nx config
    hooks.py         # nx hooks (user-facing hook management)
    hook.py          # nx hook (hidden; git hook stanza management)
    doctor.py        # nx doctor (includes --check-schema for T2 validation, RDR-076)
    upgrade.py       # nx upgrade (--dry-run, --force, --auto; T2 migrations + T3 upgrade steps, RDR-076)
    enrich.py        # nx enrich
    catalog.py       # nx catalog
    mineru.py        # nx mineru
    console.py       # nx console (embedded web UI server)
    taxonomy_cmd.py  # nx taxonomy (topic browsing, discovery, cross-collection projection via project subcommand)
    _helpers.py      # Shared CLI helpers (default_db_path)
    _provision.py    # ChromaDB Cloud provisioning helpers
  catalog/           # Xanadu-inspired document catalog (JSONL truth + SQLite cache)
    catalog.py       # Core: link(), link_query(), graph(), delete_document(), link_audit(), descendants(), resolve_chunk()
    catalog_db.py    # SQLite schema + FTS5 + UNIQUE link constraint + descendants() SQL helper
    tumbler.py       # Hierarchical addresses (depth, ancestors, lca) + JSONL readers with resilience
    auto_linker.py   # Storage-boundary auto-linking from T1 scratch link-context
    link_generator.py # Post-hoc batch linkers: citation, code-RDR heuristic, RDR file-path
    consolidation.py # Collection consolidation: merge per-paper collections into corpus-level collections
  console/           # Embedded web UI (FastAPI + HTMX)
    app.py           # FastAPI app factory; mounts routes and static files
    config.py        # Console-specific configuration
    watchers.py      # File/event watchers for live UI updates
    routes/          # FastAPI routers (activity, campaigns, health, partials)
  db/                # Storage tier implementations
    migrations.py    # Centralised T2 migration registry: Migration dataclass, apply_pending(), T3UpgradeStep, version tracking (RDR-076)
    t1.py            # T1 ChromaDB client (ephemeral or HTTP)
    t2/              # T2 SQLite package: MemoryStore, PlanLibrary, CatalogTaxonomy, Telemetry domain stores + T2Database facade
    t3.py            # T3 ChromaDB client (persistent local or cloud)
    local_ef.py      # Local ONNX embedding function (MiniLM, zero-config)
    chroma_quotas.py # ChromaDB Cloud quota constants, error hierarchy, and validator (RDR-005)
  mcp/               # MCP server split into two FastMCP servers
    core.py          # nexus MCP server: search, store, memory, scratch, collections, plans (14 tools)
    catalog.py       # nexus-catalog MCP server: catalog search/show/list/register/update/link/resolve/stats (10 tools)
  indexer.py         # Repo indexing pipeline orchestrator (classify → dispatch → embed → store)
  code_indexer.py    # Code file indexing: AST chunking, context extraction, Voyage AI embedding (extracted from indexer.py, RDR-032)
  prose_indexer.py   # Prose file indexing: semantic markdown chunking, CCE embedding (extracted from indexer.py, RDR-032)
  index_context.py   # IndexContext dataclass: shared indexing parameters replacing 12-parameter signatures
  indexer_utils.py   # Shared indexing utilities: staleness checks, context-prefix building, gitignore/repo-root helpers
  classifier.py      # File classification: CODE / PROSE / PDF / SKIP
  chunker.py         # Tree-sitter AST chunking (31 languages)
  languages.py       # LANGUAGE_REGISTRY: unified extension→language map (single source of truth)
  md_chunker.py      # Semantic markdown splitter for prose
  pdf_extractor.py   # PDF extraction: auto-routing Docling→MinerU→PyMuPDF (RDR-044)
  pdf_chunker.py     # PDF → chunks
  bib_enricher.py    # Semantic Scholar bibliographic metadata lookup
  doc_indexer.py     # Incremental doc indexer with hash-based dedup and CCE embedding
  pipeline_buffer.py # SQLite WAL buffer for streaming PDF pipeline (RDR-048)
  pipeline_stages.py # Concurrent extractor/chunker/uploader stages + orchestrator
  checkpoint.py      # Batch-path crash recovery (RDR-047)
  exporter.py        # Collection export/import (.nxexp format: gzip+msgpack, with embeddings)
  health.py          # Health check data model and runner (shared by nx doctor and nx console)
  search_engine.py   # Cross-corpus search: over-fetch, thresholds, topic pre-filter/boost/grouping, catalog pre-filtering
  search_clusterer.py # Ward hierarchical clustering for search results (fallback when topic coverage <50%)
  frecency.py        # Git frecency scoring
  scoring.py         # Reranking + quality_score (RDR-055 E2) + apply_topic_boost() (RDR-070)
  filters.py         # Shared where-filter parsing (MCP + CLI)
  ripgrep_cache.py   # ripgrep integration for hybrid search
  session.py         # Session lifecycle (T1 server start/connect)
  config.py          # Config hierarchy + .nexus.yml
  registry.py        # Collection → database routing + list_sibling_collections() for cross-collection scoping (RDR-075)
  corpus.py          # Corpus naming utilities
  formatters.py      # Result display formatting
  types.py           # Shared type definitions
  errors.py          # Error types
  retry.py           # Transient-error retry logic
  ttl.py             # TTL / expiry helpers
  hooks.py           # Git hook stanza management
  logging_setup.py   # Structured logging configuration (cli/console/mcp/hook modes, rotating file handler)
  taxonomy.py        # Deprecation shim — imports forwarded to db.t2.catalog_taxonomy (RDR-070)
  mcp_server.py      # Backward-compat shim — re-exports all MCP tools from nexus.mcp package
  mcp_infra.py       # MCP server infrastructure: singletons, caching, test injection, post-store hook framework (single-doc + batch + document-grain shapes, see "Post-Store Hooks" section), taxonomy_assign_batch_hook + chash_dual_write_batch_hook, check_version_compatibility (RDR-076)
  aspect_extractor.py # Synchronous Claude-CLI aspect extractor (RDR-089 P1.2): scholarly-paper-v1 config keyed on knowledge__* prefix, retry with transient/hard classification, null-byte defense, content-sourcing fallback
  aspect_worker.py   # Async aspect-extraction worker (RDR-089 nexus-qeo8): daemon-thread drain of T2 aspect_extraction_queue, lazy singleton, registers aspect_extraction_enqueue_hook on the document-grain chain
nx/                  # Claude Code plugin (skills, agents, hooks, slash commands)
tests/               # pytest suite (unit + integration + e2e/)
docs/                # Documentation (architecture.md is the module map)
```

## Post-Store Hooks

Three parallel hook contracts let modules register per-document enrichment that fires on every storage event, MCP `store_put` and CLI bulk ingest alike. All live in `src/nexus/mcp_infra.py`. Consumers register in exactly one shape based on the grain of work and whether they benefit from batched dependency calls; the framework fires every chain from every path so coverage is symmetric.

- **Single-document hook chain** (RDR-070). Register with `register_post_store_hook(fn)`; fired by `fire_post_store_hooks(doc_id, collection, content)` from MCP `store_put` once per call and from every CLI ingest path once per document in the batch. For per-document work keyed on `doc_id`. The single-doc chain is currently empty by default.
- **Batch hook chain** (RDR-095). Register with `register_post_store_batch_hook(fn)`; fired by `fire_post_store_batch_hooks(doc_ids, collection, contents, embeddings, metadatas)` from every CLI ingest path with the full batch and from MCP `store_put` with a 1-element batch. For enrichment that benefits from batched dependency calls (one ChromaDB query for N centroids, one batched T2 upsert). Current consumers: `chash_dual_write_batch_hook` (RDR-086), `taxonomy_assign_batch_hook` (RDR-070). Registration order is load-bearing: chash first, taxonomy second, preserving the invariant that chash rows exist before topic assignment runs.
- **Document-grain hook chain** (RDR-089). Register with `register_post_document_hook(fn)`; fired by `fire_post_document_hooks(source_path, collection, content)` from MCP `store_put` and from every CLI ingest path (8 fire sites in 6 modules — counted by AST drift guard; `doc_indexer.py` carries 3 sites for the three pdf/markdown/repo entry points). For document-grain enrichment that needs the source-document boundary as a stable identity rather than the chunk-level `doc_id`. Synchronous all the way down — zero asyncio in the dispatcher; audit F1 caught and pinned this contract via the `test_mcp_store_put_calls_document_hook_synchronously` AST assertion. Current consumer: `aspect_extraction_enqueue_hook` (defined in `aspect_worker.py`, registered in `mcp/core.py`) which enqueues to T2 `aspect_extraction_queue`; a daemon worker thread drains the queue and invokes the synchronous `extract_aspects` extractor — async dispatch was necessary because the P1.3 spike measured 26.5s median per document, blocking-inline would have been a non-starter on the ingest path.

**Content-sourcing contract** (document-grain chain): MCP `store_put` passes `content=<full document text>` literally — the text is in scope at the boundary. CLI ingest sites pass `content=""` as the contract signal that the hook may need to read `source_path` itself. `aspect_extraction_enqueue_hook` persists `content` to the queue row when non-empty so the worker has the text without re-reading from disk; CLI rows where content was not in scope rely on the worker's source-path-read fallback.

`taxonomy_assign_batch_hook` accepts `embeddings=None` from the MCP path and fetches them from T3 inline (with a local-MiniLM fallback). Batch-shape consumers therefore handle both the bulk path and the single-document path via one body.

All three chains capture per-hook exceptions, persist them to T2 `hook_failures`, and never propagate to the caller. The `chain` column (T2 4.14.2 migration, RDR-089) carries an enum value of `'single'`, `'batch'`, or `'document'`. Batch failures additionally store the JSON-encoded `doc_id` list in `batch_doc_ids` and dual-write `is_batch=1` for back-compat with pre-4.14.2 readers. Document failures store the `source_path` in the legacy `doc_id` column. Readers (`nx taxonomy status`) render batch rows with an "affecting M document(s)" parenthetical alongside the row count.

**Drift guard**: `tests/test_hook_drift_guard.py` uses `ast.walk` for two guarded sets. (1) `GUARDED_NAMES = {taxonomy_assign_batch_hook, chash_dual_write_batch_hook}` may only appear in `src/nexus/mcp_infra.py` (definitions) and `src/nexus/mcp/core.py` (registration). (2) `DOCUMENT_HOOK_GUARDED_NAMES = {aspect_extraction_enqueue_hook}` may only appear in `src/nexus/aspect_worker.py` (definition) and `src/nexus/mcp/core.py` (registration). New consumers register through `register_post_*_hook`. Direct calls fail CI. A separate runtime fire-once test (`test_index_pdf_fires_document_hook_exactly_once` in `tests/test_doc_indexer.py`) drives a sample PDF through `index_pdf` with a counting probe hook to assert the document chain fires exactly once per source document — the AST count guard alone cannot detect a regression that moves a fire site inside a per-chunk loop.

**Out of scope by design** (intentional, not deferrals):

- Three catalog-registration mechanisms (`_catalog_store_hook` in `commands/store.py`, `_catalog_pdf_hook` in `pipeline_stages.py`, `indexer.py:250` ad-hoc) capture different per-domain metadata (knowledge curator + doc_id; corpus curator + file_path + author + year + chunk_count; repo owner + rel_path + source_mtime + file_hash). Three legitimate per-domain registrations, not three copies of the same hook.
- `_catalog_auto_link` reads T1 scratch `link-context` entries that agents seed before MCP `store_put`. CLI bulk ingest has no per-file pre-declaration semantics; it uses entirely separate post-hoc linkers in `catalog/link_generator.py`. MCP-only auto-linking is intentional path-shape coupling.

## External Service Limits — CHECK BEFORE EVERY CALL

**ALWAYS** consult `src/nexus/db/chroma_quotas.py` (the `QUOTAS` dataclass and `QuotaValidator`) before writing any ChromaDB call. Violating these at runtime produces `ChromaError: Quota exceeded` — costly to debug, embarrassing in release.

**ChromaDB Cloud free-tier caps** (single source of truth: `chroma_quotas.py`):

| Operation | Limit | Notes |
|-----------|-------|-------|
| `coll.get(limit=N)` | N ≤ 300 | Same cap as queries. `_PAGE = 300` max for pagination |
| `coll.query(n_results=N)` | N ≤ 300 | `MAX_QUERY_RESULTS` |
| `coll.upsert/add(ids=[...])` | ≤ 300 records | `MAX_RECORDS_PER_WRITE` |
| Concurrent reads per coll | ≤ 10 | `MAX_CONCURRENT_READS` |
| Concurrent writes per coll | ≤ 10 | `MAX_CONCURRENT_WRITES` |
| Document size | ≤ 16384 bytes | `MAX_DOCUMENT_BYTES` (use `SAFE_CHUNK_BYTES = 12288`) |
| Query string | ≤ 256 chars | `MAX_QUERY_STRING_CHARS` |
| `where` predicates | ≤ 8 top-level | `MAX_WHERE_PREDICATES` |
| Embedding dims | ≤ 4096 | `MAX_EMBEDDING_DIMENSIONS` |

**Voyage AI**: `voyage-3` / `voyage-code-3` / `voyage-context-3` = 1024 dims, 32k tokens/request. Batch requests up to 128 inputs. Use `nexus.retry._voyage_with_retry` for transient failure handling.

**Rule of thumb**: paginating through a large ChromaDB collection requires `limit ≤ 300` per call. When fetching N documents, use `offset += 300` in a loop. `MAX_RECORDS_PER_WRITE = 300` means upsert batches must also be capped.

## Development Conventions

- **Python 3.12+**: use `match/case`, `tomllib`, `typing.Protocol`, walrus operator freely
- **Type hints everywhere**: all public functions, methods, and module-level variables
- **No ORM**: raw `sqlite3` for T2, WAL mode enabled on open
- **TDD**: write tests before implementation; use `pytest` + `pytest-asyncio`
- **Package manager**: `uv` (not pip directly); `pyproject.toml` for dependencies
- **Version pinning required**: `llama-index-core` + `tree-sitter-language-pack` have known breaking incompatibilities — do not bump without testing the full chunking pipeline
- **Logging**: `structlog` preferred; never `print()` in library code
- **Protocols over ABCs**: `typing.Protocol` for structural subtyping, no inheritance coupling
- **Constructor injection**: dependencies via constructor, no global singletons

## Adding a CLI Command

1. Create `src/nexus/commands/your_cmd.py` with a Click group or command
2. Register it in `src/nexus/cli.py` via `cli.add_command()`
3. Add tests in `tests/test_your_cmd.py`
4. Document in `docs/cli-reference.md`

## Task Tracking

Use beads (`/beads:*` skills) for task tracking and T2 memory (`nx memory`) for project context.

- Find ready work: `/beads:ready`
- Bead tracking: `/beads:list` / `/beads:show <id>`
- Store project context: `nx memory put ... --project {repo}`

## Git

Branch naming: `feature/<bead-id>-<short-description>`
Never push directly to `main` — all changes via PR.

## Sandbox Testing

Editable installs (`uv sync`) and wheel installs (`uv tool install`) resolve package data and version-gated migrations differently. Pytest runs the editable shape; users run the wheel shape. Bugs that pass tests and ship broken usually live in that gap.

`tests/e2e/release-sandbox.sh` mirrors the wheel shape locally + runs the canary surface. **Required before merging any PR that touches:**

- `pyproject.toml`, `uv.lock`
- `src/nexus/db/migrations.py` (T2 migrations are version-gated)
- `src/nexus/mcp/**`
- `nx/**` (plugin manifest, hooks, agents, skills)
- `.claude-plugin/**`
- `src/nexus/commands/{doctor,upgrade}.py`

```bash
./tests/e2e/release-sandbox.sh smoke    # ~2 min, all checks must pass
```

Modes: `smoke` (canary), `shell` (manual nx exercise), `tmux` (Claude Code against sandbox), `reset`. Manual: `tests/e2e/release-sandbox.md`.

If `smoke` fails: stop, fix on the same PR. Do not merge intending to fix on main.

## Release

See `docs/contributing.md` for the full release checklist. Files that change every release: `pyproject.toml`, `uv.lock` (must be committed), `CHANGELOG.md`, `nx/CHANGELOG.md`, `.claude-plugin/marketplace.json`.
