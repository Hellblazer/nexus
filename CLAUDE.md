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
- T2: SQLite + FTS5 — four domain stores: `MemoryStore` (persistent notes + FTS5), `PlanLibrary` (plan templates), `CatalogTaxonomy` (topic clustering + assignment), `Telemetry` (relevance log). Facade: `T2Database`. Plan tools: `plan_save(ttl=30)`/`plan_search` MCP tools, 5 builtin templates seeded at `nx catalog setup`.
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
    doctor.py        # nx doctor
    enrich.py        # nx enrich
    catalog.py       # nx catalog
    mineru.py        # nx mineru
    console.py       # nx console (embedded web UI server)
    taxonomy_cmd.py  # nx taxonomy (topic browsing and discovery)
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
  indexer_utils.py   # Shared indexing utilities: staleness checks, context-prefix building
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
  registry.py        # Collection → database routing
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
  mcp_infra.py       # MCP server infrastructure: singletons, caching, test injection, taxonomy_assign_hook (post-store topic assignment)
nx/                  # Claude Code plugin (skills, agents, hooks, slash commands)
tests/               # pytest suite (unit + integration + e2e/)
docs/                # Documentation (architecture.md is the module map)
```

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

## Release

See `docs/contributing.md` for the full release checklist. Files that change every release: `pyproject.toml`, `uv.lock` (must be committed), `CHANGELOG.md`, `nx/CHANGELOG.md`, `.claude-plugin/marketplace.json`.
