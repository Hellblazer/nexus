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
uv tool install .                 # install nx CLI locally

# Tests
uv run pytest                     # full unit suite (no API keys needed)
uv run pytest tests/test_indexer.py   # single file
uv run pytest -k "test_frecency"      # by name pattern
uv run pytest --cov=nexus             # with coverage
uv run pytest -m integration          # E2E (requires real API keys: copy .env.example → .env)

# After changes, reinstall CLI
uv sync && uv tool install --reinstall .
nx --version
```

Unit tests use `chromadb.EphemeralClient` + bundled ONNX MiniLM — no API keys or network.

## Project Overview

Nexus is a Python 3.12+ CLI + persistent server for semantic search and knowledge management. Published on PyPI as **`conexus`**; the CLI entry point is **`nx`** (`src/nexus/` is the package).

**Three storage tiers:**
- T1: `chromadb.EphemeralClient` (or HTTP server via SessionStart hook) — session scratch (`nx scratch`)
- T2: SQLite + FTS5 — persistent memory (`nx memory`)
- T3: `chromadb.CloudClient` + `VoyageAIEmbeddingFunction` — permanent knowledge (`nx store`, `nx search`)

**T3 ChromaDB database**: a single `chromadb.CloudClient` database (`CHROMA_DATABASE` value, e.g. `nexus`). All collection prefixes coexist in one database:
- `code__*` collections — `voyage-code-3` for index, `voyage-4` for query
- `docs__*` collections — `voyage-context-3` (CCE) index + query
- `rdr__*` collections — `voyage-context-3`
- `knowledge__*` collections — `voyage-context-3`

**Collection naming**: always `__` as separator — `code__myrepo`, `docs__corpus`, `knowledge__topic` (colons are invalid in ChromaDB collection names).

**Session propagation (T1)**: The `SessionStart` hook starts a per-session ChromaDB HTTP server, writes its address to `~/.config/nexus/sessions/{ppid}.session`. Child agents walk the OS PPID chain to find the nearest ancestor session file and share T1 scratch across the agent tree. Falls back to `EphemeralClient` when the server cannot start.

**T3 expire guard**: always filter `ttl_days > 0 AND expires_at != "" AND expires_at < now` — the `expires_at != ""` guard is mandatory: permanent entries use `expires_at=""` which sorts before ISO timestamps and would be incorrectly deleted by a 2-condition guard.

## Source Layout

```
src/nexus/           # Core package
  cli.py             # Click entry point; registers all command groups
  commands/          # One file per CLI command group (index, search, memory, scratch, store, collection, config, hooks, doctor)
  db/                # t1.py, t2.py, t3.py — tier implementations
  indexer.py         # Repo indexing pipeline (classify → chunk → embed → store)
  classifier.py      # File classification: CODE / PROSE / PDF / SKIP
  chunker.py         # Tree-sitter AST chunking (31 languages)
  md_chunker.py      # Semantic markdown splitter for prose
  pdf_extractor.py   # Docling-based PDF extraction
  pdf_chunker.py     # PDF → chunks
  doc_indexer.py     # Incremental doc indexer with hash-based dedup
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

Use beads (`bd`) for task tracking and T2 memory (`nx memory`) for project context.

- Find ready work: `bd ready`
- Bead tracking: `bd list` / `bd show <id>`
- Store project context: `nx memory put ... --project {repo}`

## Git

Branch naming: `feature/<bead-id>-<short-description>`
Never push directly to `main` — all changes via PR.

## Release

See `docs/contributing.md` for the full release checklist. Files that change every release: `pyproject.toml`, `uv.lock` (must be committed), `CHANGELOG.md`, `nx/CHANGELOG.md`, `.claude-plugin/marketplace.json`.
