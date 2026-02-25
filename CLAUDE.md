# Nexus — Claude Code Directives

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

## Project Overview

Nexus is a Python 3.12+ CLI + persistent server for semantic search and knowledge management.
See `docs/` for full documentation; `docs/architecture.md` for the module map.

**Three storage tiers:**
- T1: `chromadb.EphemeralClient` + `DefaultEmbeddingFunction` — session scratch (`nx scratch`)
- T2: SQLite + FTS5 — memory bank replacement (`nx memory`)
- T3: `chromadb.CloudClient` + `VoyageAIEmbeddingFunction` — permanent knowledge (`nx store`, `nx search`)

**Collection naming**: always `__` as separator — `code__myrepo`, `docs__corpus`, `knowledge__topic` (colons are invalid in ChromaDB collection names).

**Session ID**: generated via `os.getsid(0)` (session group leader PID), written to `~/.config/nexus/sessions/{getsid}.session`. `CLAUDE_SESSION_ID` does not exist in Claude Code. The PID-scoped path is intentional — multiple concurrent Claude Code windows each get an isolated session file (the flat `current_session` design was rejected as race-prone).

**T3 expire guard**: always filter `ttl_days > 0 AND expires_at != "" AND expires_at < now` — the `expires_at != ""` guard is mandatory: permanent entries use `expires_at=""` which sorts before ISO timestamps and would be incorrectly deleted by a 2-condition guard.

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

PM infrastructure lives in T2 under the bare `{repo}` namespace (tagged with `pm`). Use `nx pm` commands for all PM operations.

- Session resumption: `nx pm resume`
- Current phase/status: `nx pm status`
- Bead tracking: `bd list` / `bd show <id>`
- Standard docs (4): METHODOLOGY.md, BLOCKERS.md, CONTEXT_PROTOCOL.md, phases/phase-1/context.md

## Git

Branch naming: `feature/<bead-id>-<short-description>`
Never push directly to `main` — all changes via PR.
