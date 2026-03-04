# Storage Tiers

Nexus organizes data across three tiers with increasing durability. Data flows upward (T1 → T2 → T3).

| Tier | Storage | Network | Durability | Use |
|------|---------|---------|------------|-----|
| T1 -- scratch | ChromaDB HTTP server (per-session) | Localhost only | Session only | Working notes, hypotheses |
| T2 -- memory | SQLite + FTS5 (WAL) | None | Survives restarts | Per-project notes, session context |
| T3 -- knowledge | ChromaDB cloud + Voyage AI | Required | Permanent | Semantic search, indexed code/docs |

## T1 -- Session Scratch

Backed by a per-session `chromadb.HttpClient` connecting to a ChromaDB server process started by the `SessionStart` hook. Uses `DefaultEmbeddingFunction` (MiniLM-L6-v2, local ONNX). No API keys required.

When a parent Claude Code session starts, the `SessionStart` hook allocates a free localhost port, launches a ChromaDB server, and writes the server address and session ID to `~/.config/nexus/sessions/{ppid}.session`. Child agents spawned via the Agent tool walk the OS PPID chain to find the nearest ancestor session file and connect to the same server — they share scratch space and see each other's entries. Concurrent independent Claude Code windows stay isolated because they have disjoint OS process trees.

Falls back to a local `EphemeralClient` (with a warning) when no server record is found — T1 functions locally for that process but subagents get isolated sessions. This activates in restricted container environments where the server process cannot start, or when `ps` is unavailable.

Everything is wiped at session end: the `SessionEnd` hook stops the ChromaDB server and deletes the backing tmpdir. Use `nx scratch flag` to mark items for auto-promotion to T2 when the session closes.

**Use for**: working hypotheses, temporary notes, in-flight analysis shared across spawned agents.

## T2 -- Memory Bank

A local SQLite database. Every entry has a project, a title, and content — like a flat filesystem where project is the directory and title is the filename. Entries can have tags and an optional TTL. FTS5 provides keyword search with no API call. Stored at `~/.config/nexus/memory.db`.

T2 is the persistent local layer that bridges sessions. Notes, project state, and agent relay context survive restarts here. Different usage patterns share the same simple model:

- **Developer notes** — hypotheses, findings, decisions-in-progress via `nx memory put`
- **Project memory** — design notes, working state, active decisions. Store with `nx memory put`, retrieve with `nx memory get`. See [Memory and Tasks](memory-and-tasks.md).
- **RDR metadata** — status, type, priority, dates for each RDR document. See [RDR: Nexus Integration](rdr-nexus-integration.md).
- **Agent relay** — context passed between agent invocations
- **Promoted scratch** — T1 entries flagged during a session are auto-flushed to T2 at session end

Data is organized by project via the `--project` flag. TTL values: `30d`, `4w`, or permanent. Default is `30d` for developer notes.

## T3 -- Permanent Knowledge

Backed by four `chromadb.CloudClient` instances with `VoyageAIEmbeddingFunction`. Requires environment variables: `CHROMA_API_KEY`, `CHROMA_TENANT`, `CHROMA_DATABASE`, `VOYAGE_API_KEY`.

Each content type routes to a dedicated ChromaDB Cloud database derived from the `CHROMA_DATABASE` base name:

| Database | Collections | Purpose |
|----------|-------------|---------|
| `{base}_code` | `code__*` | Indexed source code |
| `{base}_docs` | `docs__*` | Indexed prose, PDFs, markdown |
| `{base}_rdr` | `rdr__*` | Indexed RDR documents |
| `{base}_knowledge` | `knowledge__*` | Agent outputs, notes, stored knowledge |

All four databases are provisioned automatically by `nx config init`. Run `nx doctor` to verify connectivity.

Collections are namespaced by corpus type using `__` (double underscore) as separator:

| Pattern | Contents | Index model | Query model |
|---------|----------|-------------|-------------|
| `code__<repo>-<hash>` | Indexed source code | voyage-code-3 | voyage-4 |
| `docs__<repo>-<hash>` | Indexed prose files | voyage-context-3 (CCE) | voyage-4 |
| `rdr__<repo>-<hash>` | Indexed RDR documents | voyage-context-3 (CCE) | voyage-4 |
| `docs__<corpus>` | Indexed PDFs and markdown | voyage-context-3 (CCE) | voyage-4 |
| `knowledge__<topic>` | Stored agent outputs and notes | voyage-context-3 | voyage-4 |

**TTL and expiry**: `nx store expire` removes expired entries from `knowledge__*` collections only. Code, docs, and RDR collections are never expired — they are refreshed via re-indexing.

**Use for**: semantic search across sessions, institutional knowledge.

## Data Flow

```
T1 (scratch)
  | scratch promote / flag-flush
  v
T2 (memory)
  | memory promote
  v
T3 (knowledge)
```

### Promotion methods

- **T1 -> T2**: `nx scratch promote ID --project NAME --title NAME`, or auto-flush of flagged items at session end.
- **T2 -> T3**: `nx memory promote TITLE --collection NAME`.

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
