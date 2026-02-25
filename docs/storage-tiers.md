# Storage Tiers

Nexus organizes data across three tiers with increasing durability. Data flows upward only (T1 → T2 → T3), with no reverse flow except `nx pm restore`.

| Tier | Storage | Network | Durability | Use |
|------|---------|---------|------------|-----|
| T1 -- scratch | In-memory ChromaDB | None | Session only | Working notes, hypotheses |
| T2 -- memory | SQLite + FTS5 (WAL) | None | Survives restarts | Per-project notes, PM state |
| T3 -- knowledge | ChromaDB cloud + Voyage AI | Required | Permanent | Semantic search, indexed code/docs |

## T1 -- Session Scratch

Backed by `chromadb.EphemeralClient` with `DefaultEmbeddingFunction` (MiniLM-L6-v2, local ONNX). No API keys required.

Everything is wiped at session end. Use `nx scratch flag` to mark items for auto-promotion to T2 when the session closes. Each concurrent Claude Code window gets its own isolated session.

**Use for**: working hypotheses, temporary notes, in-flight analysis.

## T2 -- Memory Bank

SQLite database with an FTS5 virtual table, WAL mode enabled for concurrent access. Stored at `~/.config/nexus/memory.db`. No network dependency.

Data is organized by project via the `--project` flag. Supports TTL values: `30d`, `4w`, or permanent (default: `30d`). FTS5 enables keyword search with no API call.

**Use for**: session memory, PM docs, RDR metadata, agent relay state.

## T3 -- Permanent Knowledge

Backed by `chromadb.CloudClient` with `VoyageAIEmbeddingFunction`. Requires environment variables: `CHROMA_API_KEY`, `CHROMA_TENANT`, `CHROMA_DATABASE`, `VOYAGE_API_KEY`.

Collections are namespaced by corpus type using `__` (double underscore) as separator:

| Pattern | Contents | Index model | Query model |
|---------|----------|-------------|-------------|
| `code__<repo>-<hash>` | Indexed source code | voyage-code-3 | voyage-4 |
| `docs__<repo>-<hash>` | Indexed prose files | voyage-context-3 (CCE) | voyage-4 |
| `rdr__<repo>-<hash>` | Indexed RDR documents | voyage-context-3 (CCE) | voyage-4 |
| `docs__<corpus>` | Indexed PDFs and markdown | voyage-context-3 / voyage-4 | voyage-4 |
| `knowledge__<topic>` | Stored agent outputs and notes | voyage-context-3 | voyage-4 |

**TTL and expiry**: `nx store expire` removes expired entries from `knowledge__*` collections only. Code, docs, and RDR collections are never expired — they are refreshed via re-indexing.

**Use for**: semantic search across sessions, institutional knowledge, archived PM syntheses.

## Data Flow

```
T1 (scratch)
  | scratch promote / flag-flush
  v
T2 (memory)
  | memory promote / pm archive
  v
T3 (knowledge)
```

### Promotion methods

- **T1 -> T2**: `nx scratch promote ID --project NAME --title NAME`, or auto-flush of flagged items at session end.
- **T2 -> T3**: `nx memory promote TITLE --collection NAME`, or `nx pm archive` (Haiku synthesis).

### TTL translation on promote

| T2 TTL | T3 `ttl_days` | T3 `expires_at` |
|--------|---------------|-----------------|
| NULL (permanent) | 0 | `""` (empty string) |
| N days | N | Computed ISO 8601 timestamp |

## When to Use Each Tier

| Scenario | Tier | Why |
|----------|------|-----|
| Quick note during debugging | T1 | Ephemeral, no setup |
| Project decisions that survive restarts | T2 | Local, fast, searchable |
| Research findings for future sessions | T3 | Semantic search across time |
| Indexed code/docs | T3 | Vector similarity + reranking |

See [cli-reference.md](cli-reference.md) for command details.
