<!-- Copyright (c) 2026 Hal Hildebrand. All rights reserved. -->

# Nexus — Claude Code Directives

## Copyright

All source files in this repository must include the following header as the first line:

- **Python**: `# Copyright (c) 2026 Hal Hildebrand. All rights reserved.`
- **Markdown / command / skill files**: `<!-- Copyright (c) 2026 Hal Hildebrand. All rights reserved. -->`
- **YAML / TOML / Shell**: `# Copyright (c) 2026 Hal Hildebrand. All rights reserved.`

## Project Overview

Nexus is a Python 3.12+ CLI + persistent server for semantic search and knowledge management.
See `spec.md` for the full specification.

**Three storage tiers:**
- T1: `chromadb.EphemeralClient` + `DefaultEmbeddingFunction` — session scratch (`nx scratch`)
- T2: SQLite + FTS5 — memory bank replacement (`nx memory`)
- T3: `chromadb.CloudClient` + `VoyageAIEmbeddingFunction` — permanent knowledge (`nx store`, `nx search`)

**Collection naming**: always `__` as separator — `code__myrepo`, `docs__corpus`, `knowledge__topic` (colons are invalid in ChromaDB collection names).

**Session ID**: generated as UUID4 by SessionStart hook, written to `~/.config/nexus/current_session`. `CLAUDE_SESSION_ID` does not exist in Claude Code.

**T3 expire guard**: always filter `ttl_days > 0 AND expires_at < now` — `expires_at=""` for permanent entries sorts before ISO timestamps.

## Development Conventions

- **Python 3.12+**: use `match/case`, `tomllib`, `typing.Protocol`, walrus operator freely
- **Type hints everywhere**: all public functions, methods, and module-level variables
- **No ORM**: raw `sqlite3` for T2, WAL mode enabled on open
- **TDD**: write tests before implementation; use `pytest` + `pytest-asyncio`
- **Package manager**: `uv` (not pip directly); `pyproject.toml` for dependencies
- **Version pinning required**: `llama-index-core` + `tree-sitter-language-pack` (known breaking incompatibilities)
- **No `synchronized`** — use `threading.Lock` or `asyncio.Lock` as appropriate
- **Logging**: `structlog` preferred; never `print()` in library code

## Project Management

PM infrastructure lives in `.pm/`. Use `nx pm` commands once Nexus is built; use `.pm/` files directly during bootstrap.

- Session resumption: `cat .pm/CONTINUATION.md`
- Current phase: see `.pm/phases/`
- Bead tracking: `bd list` / `bd show <id>`

## Git

Branch naming: `feature/<bead-id>-<short-description>`
Never push directly to `main` — all changes via PR.
