# AGENTS.md

Project guidance for AI coding agents working in this repository. `CLAUDE.md` is a symlink to this file.

Nexus is a Python 3.12+ CLI + persistent server for semantic search and knowledge management. Published on PyPI as `conexus`; the CLI entry point is `nx` (`src/nexus/` is the package).

## Quick start

```bash
uv sync                                  # install deps
scripts/reinstall-tool.sh                # install nx CLI locally (preserves extras)
uv run pytest                            # full unit suite (no API keys needed)
uv run pytest -m integration             # E2E (requires .env from .env.example)
uv sync && scripts/reinstall-tool.sh && nx --version    # after edits
```

Unit tests use `chromadb.EphemeralClient` + bundled ONNX MiniLM — no API keys or network.

## Architecture at a glance

Three storage tiers, by lifetime:

- **T1** — `chromadb` Ephemeral or per-session HTTP server. Session scratch (`nx scratch`).
- **T2** — SQLite + FTS5, seven domain stores behind a `T2Database` facade. Persistent notes, plans, taxonomy, telemetry, chash, aspects, aspect queue.
- **T3** — `chromadb.PersistentClient` + local ONNX (local mode) **or** `chromadb.CloudClient` + Voyage (cloud mode). Permanent knowledge (`nx store`, `nx search`).

Collection prefixes coexist in one T3 database. Always `__` (double underscore) as separator — colons are invalid in ChromaDB collection names:

| Prefix | Embedder | Identity field |
|---|---|---|
| `code__*` | `voyage-code-3` | `source_path` |
| `docs__*`, `rdr__*` | `voyage-context-3` (CCE) | `source_path` |
| `knowledge__*` | `voyage-context-3` | `source_path` then `title` (fallback) |

For the full module map, post-store hook contracts, T2 schema, and design heritage see [`docs/architecture.md`](docs/architecture.md). For module-local guidance see the `AGENTS.md` files inside `src/nexus/catalog/`, `src/nexus/db/`, and `src/nexus/mcp/`.

## Critical conventions

- **Python 3.12+** — use `match/case`, `tomllib`, `typing.Protocol`, walrus freely.
- **Type hints on every public API.** Module-level constants too.
- **No ORM.** Raw `sqlite3` for T2; WAL mode enabled on open.
- **Composition over inheritance.** Protocols, not deep hierarchies. Constructor injection — no global singletons, no service locators.
- **TDD.** Test file before implementation. Deterministic: seeded randomness, fixed clocks, `port=0` for dynamic allocation.
- **Integration over mocks.** Hit a real `chromadb.EphemeralClient` and a real tmp-path SQLite — mocks hide boundary bugs.
- **Structured logging only.** `structlog.get_logger(__name__)`. Never `print()` in library code; CLI commands use `click.echo()`.
- **`uv` as package manager.** `pyproject.toml` for deps. Don't bump `llama-index-core` or `tree-sitter-language-pack` without exercising the chunking pipeline — they have known breaking incompatibilities.

## External service limits — check before every call

The single source of truth is `src/nexus/db/chroma_quotas.py` (the `QUOTAS` dataclass and `QuotaValidator`). Violating any of these at runtime produces `ChromaError: Quota exceeded`.

| Operation | Limit | Constant |
|---|---|---|
| `coll.get(limit=N)` | N ≤ 300 | `_PAGE` |
| `coll.query(n_results=N)` | N ≤ 300 | `MAX_QUERY_RESULTS` |
| `coll.upsert/add(ids=[...])` | ≤ 300 records | `MAX_RECORDS_PER_WRITE` |
| Concurrent reads / writes per coll | ≤ 10 each | `MAX_CONCURRENT_READS/WRITES` |
| Document size | ≤ 16384 bytes | `MAX_DOCUMENT_BYTES` (use `SAFE_CHUNK_BYTES = 12288`) |
| Query string | ≤ 256 chars | `MAX_QUERY_STRING_CHARS` |
| `where` predicates | ≤ 8 top-level | `MAX_WHERE_PREDICATES` |
| Embedding dims | ≤ 4096 | `MAX_EMBEDDING_DIMENSIONS` |

Voyage AI: `voyage-3` / `voyage-code-3` / `voyage-context-3` = 1024 dims, 32k tokens, 128 inputs/batch. Use `nexus.retry._voyage_with_retry` for transient failures.

Pagination over a large collection: `limit ≤ 300` per call, `offset += 300` in a loop.

## Hot rules (don'ts paired with dos)

- **Never `print()` in library code.** Use `structlog.get_logger(__name__).info(event=..., **fields)`.
- **Never push directly to `main`.** Open a PR. The only exception is the version-bump commit during a release (`docs/contributing.md` § Release Process).
- **Never `git add -A` or `git add .`.** Stage by explicit path so untracked drafts don't sneak in.
- **Never include AI attribution in commits.** No "Generated with Claude", no `Co-Authored-By: Claude`. Bead references and `Closes #N` only.
- **Never delete RDR files.** Closing an RDR is a frontmatter `status: closed` flip — the file stays. See [`docs/rdr/AGENTS.md`](docs/rdr/AGENTS.md).
- **T3 expire-guard SQL must be three-clause.** `ttl_days > 0 AND expires_at != "" AND expires_at < now`. The `!= ""` clause is mandatory: permanent rows have `expires_at=""` which sorts before ISO timestamps and would be wrongly deleted by a two-clause guard.
- **Always use full MCP tool names.** `mcp__plugin_<plugin>_<server>__<tool>`. Short names fail at runtime.

## Workflows

### Adding a CLI command

1. Create `src/nexus/commands/your_cmd.py` with a Click group/command.
2. Register it in `src/nexus/cli.py` via `cli.add_command()`.
3. Add tests in `tests/test_your_cmd.py`.
4. Document the new flags/subcommands in `docs/cli-reference.md`.

### Cutting a release (version bump + tag-push to PyPI)

1. **Run unit + integration suite.** `uv run pytest` and `uv run pytest -m integration`. Both must pass — integration is excluded from CI and is your last line of defense.
2. **Audit docs against changes since last tag.** `git log --oneline v<prev>..HEAD` then check `docs/cli-reference.md`, `docs/architecture.md`, `README.md` for user-visible drift.
3. **Bump version in all four manifests** (CI enforces parity):
   - `pyproject.toml` — `version = "X.Y.Z"`
   - `.claude-plugin/marketplace.json` — both `version` fields
   - `nx/.claude-plugin/plugin.json` — `version`
   - `sn/.claude-plugin/plugin.json` — `version`
4. **Update changelogs.** Add a new section to `CHANGELOG.md` and `nx/CHANGELOG.md` with the date and the changes since last release.
5. **Refresh `uv.lock`.** Run `uv sync` — the lock file MUST be committed.
6. **Run sandbox smoke.** `./tests/e2e/release-sandbox.sh smoke` (~2 min). Required for any change touching `pyproject.toml`, `uv.lock`, `src/nexus/db/migrations.py`, `src/nexus/mcp/**`, `nx/**`, `.claude-plugin/**`, `src/nexus/commands/{doctor,upgrade}.py`.
7. **Commit and push to main.** `chore(release): conexus X.Y.Z` is the only direct-to-main commit allowed.
8. **Tag and push the tag.** `git tag -a vX.Y.Z -m "conexus X.Y.Z" && git push origin vX.Y.Z`. The tag-push triggers the Release workflow → PyPI auto-publish via OIDC.
9. **Reinstall locally.** `scripts/reinstall-tool.sh && nx --version` — `pyproject.toml` is bumped but the local `nx` shim still points at the old wheel until reinstall.

Full checklist with rollback / one-time setup steps lives in [`docs/contributing.md` § Release Process](docs/contributing.md#release-process).

## Task tracking

Use **beads** (`bd`) for issue tracking. Find work with `bd ready`; claim with `bd update <id> --claim`; close with `bd close <id>`. Use `nx memory put` for project-context notes that persist across sessions. See `docs/contributing.md` § Git Workflow for branch naming (`feature/<bead-id>-<description>`).

## Settings

User-global permission settings live in `~/.claude/settings.json`. Never write to `settings.local.json` — it must remain `{}`.
