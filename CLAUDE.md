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
- T2: SQLite + FTS5 — persistent memory (`nx memory`) + plan library (`plan_save(ttl=30)`/`plan_search` MCP tools, 5 builtin templates seeded at `nx catalog setup`)
- T3: `chromadb.PersistentClient` + local ONNX embeddings (local mode, zero-config) OR `chromadb.CloudClient` + `VoyageAIEmbeddingFunction` (cloud mode) — permanent knowledge (`nx store`, `nx search`)

**T3 ChromaDB database**: a single `chromadb.CloudClient` database (`CHROMA_DATABASE` value, e.g. `nexus`). All collection prefixes coexist in one database:
- `code__*` collections — `voyage-code-3` for index, `voyage-4` for query
- `docs__*` collections — `voyage-context-3` (CCE) index + query
- `rdr__*` collections — `voyage-context-3`
- `knowledge__*` collections — `voyage-context-3`

**Collection naming**: always `__` as separator — `code__myrepo`, `docs__corpus`, `knowledge__topic` (colons are invalid in ChromaDB collection names).

**Single-chunk CCE**: Documents with only 1 chunk in CCE collections (`docs__*`, `knowledge__*`, `rdr__*`) are embedded via `contextualized_embed()` with `inputs=[[chunk]]`. The previous `voyage-4` fallback for single-chunk documents was removed — it caused a model mismatch between index and query vectors (see post-mortem: cce-query-model-mismatch).

**Session propagation (T1)**: The `SessionStart` hook starts a per-session ChromaDB HTTP server, writes its address to `~/.config/nexus/sessions/{ppid}.session`. Child agents walk the OS PPID chain to find the nearest ancestor session file and share T1 scratch across the agent tree. Falls back to `EphemeralClient` when the server cannot start.

**Catalog (T3 metadata layer)**: Git-backed document registry that tracks *what* is indexed and *how documents relate*. JSONL files are the source of truth; SQLite + FTS5 is the query cache (rebuilt automatically on mtime change). Tumblers (hierarchical addresses like `1.2.5`) identify documents. Every indexing pathway (`index repo`, `index pdf`, `index rdr`, MCP `store_put`) auto-registers entries. `nx catalog setup` creates and populates the catalog in one step.

**Link graph**: Typed edges between documents (`cites`, `implements`, `implements-heuristic`, `supersedes`, `relates`, or custom). `created_by` tracks provenance. Three creation paths:

1. **Post-hoc** (batch, after indexing): `generate_citation_links()`, `generate_code_rdr_links()`, `generate_rdr_filepath_links()` in `link_generator.py`
2. **Auto-linker** (`auto_linker.py`): fires on every `store_put` MCP call, reads `link-context` from T1 scratch (tag: `link-context`), creates links to seeded targets. Skills seed before dispatch; agents self-seed from their task prompt when no context exists.
3. **Agent-direct**: agents call `catalog_link` MCP tool during work for precise typed links

**Two graph views**: `catalog_links` returns live-document links only. `catalog_link_query` returns all including orphans. The `query` MCP tool has catalog-aware routing (`author`, `content_type`, `subtree`, `follow_links`, `depth`) for scoped search.

**Pagination**: All list-returning tools include footers when truncated — `offset=N` for next page.

**T3 expire guard**: always filter `ttl_days > 0 AND expires_at != "" AND expires_at < now` — the `expires_at != ""` guard is mandatory: permanent entries use `expires_at=""` which sorts before ISO timestamps and would be incorrectly deleted by a 2-condition guard.

## Source Layout

```
src/nexus/           # Core package
  cli.py             # Click entry point; registers all command groups
  commands/          # One file per CLI command group (index, search, memory, scratch, store, collection, config, hooks, doctor, enrich, catalog)
  catalog/           # Xanadu-inspired document catalog (JSONL truth + SQLite cache)
    catalog.py       # Core: link(), link_query(), graph(), delete_document(), link_audit(), descendants(), resolve_chunk()
    catalog_db.py    # SQLite schema + FTS5 + UNIQUE link constraint + descendants() SQL helper
    tumbler.py       # Hierarchical addresses (depth, ancestors, lca) + JSONL readers with resilience
    auto_linker.py    # Storage-boundary auto-linking from T1 scratch link-context
    link_generator.py # Post-hoc batch linkers: citation, code-RDR heuristic, RDR file-path
  db/                # t1.py, t2.py, t3.py — tier implementations; local_ef.py — local ONNX embeddings
  indexer.py         # Repo indexing pipeline (classify → chunk → embed → store)
  classifier.py      # File classification: CODE / PROSE / PDF / SKIP
  chunker.py         # Tree-sitter AST chunking (31 languages)
  md_chunker.py      # Semantic markdown splitter for prose
  pdf_extractor.py   # Docling-based PDF extraction
  pdf_chunker.py     # PDF → chunks
  bib_enricher.py    # Semantic Scholar bibliographic metadata lookup
  doc_indexer.py     # Incremental doc indexer with hash-based dedup
  pipeline_buffer.py # SQLite WAL buffer for streaming PDF pipeline (RDR-048)
  pipeline_stages.py # Concurrent extractor/chunker/uploader stages + orchestrator
  checkpoint.py      # Batch-path crash recovery (RDR-047)
  search_engine.py   # Semantic + hybrid search
  frecency.py        # Git frecency scoring
  scoring.py         # Reranking
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
