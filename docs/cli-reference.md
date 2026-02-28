# Nexus CLI Reference

All commands use the `nx` binary. Global flags: `--help`, `--version`, `-v`/`--verbose` (enable debug logging).

---

## nx search

Semantic search across T3 knowledge collections.

```
nx search "authentication middleware" --corpus code --hybrid --n 20
```

| Flag | Description |
|------|-------------|
| `QUERY` (positional) | Search query text |
| `PATH` (positional, optional) | Scope search to files under that directory |
| `--corpus NAME` | Collection prefix or full name (repeatable; default: `knowledge`, `code`, `docs`) |
| `-a` / `--answer` | Synthesize cited answer via Haiku after retrieval |
| `--agentic` | Multi-step Haiku query refinement before returning results |
| `--hybrid` | Merge semantic + ripgrep results for code (0.7*vector + 0.3*frecency) |
| `--no-rerank` | Disable cross-corpus reranking (use round-robin instead) |
| `--mxbai` | Fan out to Mixedbread-indexed collections (read-only) |
| `--where KEY=VALUE` | Metadata filter (repeatable; multiple flags are ANDed) |
| `-m` / `--n` / `--max-results NUM` | Max results (default 10) |
| `-A N` | Show N lines of context after each result chunk |
| `-C N` | Show N lines of context after each result chunk (alias for `-A`) |
| `-c` / `--content` | Show matched text inline under each result (truncated at 200 chars) |
| `-r` / `--reverse` | Reverse result order (highest-scoring last) |
| `--vimgrep` | Output as `path:line:col:content` |
| `--json` | JSON array output |
| `--files` | Unique file paths only |
| `--no-color` | Disable colored output |

---

## nx index

Index content into T3 four-store collections.

```
nx index repo ./my-project
```

| Subcommand | Description |
|------------|-------------|
| `repo PATH` | Index code repository (smart classification: code to `code__`, prose to `docs__`, RDRs to `rdr__`) |
| `rdr [PATH]` | Index RDR documents in `docs/rdr/` into `rdr__` collection (default: current dir) |
| `pdf PATH` | Index a PDF document into T3 `docs__CORPUS` |
| `md PATH` | Index a Markdown file into T3 `docs__CORPUS` |

**`repo` flags:**

| Flag | Description |
|------|-------------|
| `--frecency-only` | Update frecency scores only; skip re-embedding (faster, for re-ranking refresh) |

**`pdf` and `md` flags:**

| Flag | Description |
|------|-------------|
| `--corpus NAME` | Corpus name for the `docs__` collection (default: `default`) |

---

## nx store

Manage T3 cloud knowledge entries.

```
echo "# Cache Strategy" | nx store put - --collection knowledge --title "decision-cache" --tags "decision,arch"
```

| Subcommand | Description |
|------------|-------------|
| `put FILE_OR_DASH` | Store document (use `-` for stdin) |
| `list` | List stored entries |
| `expire` | Remove expired entries |

**`put` flags:**

| Flag | Description |
|------|-------------|
| `-c` / `--collection NAME` | Collection name or prefix (default: `knowledge`) |
| `-t` / `--title TITLE` | Entry title (required when SOURCE is `-`) |
| `--tags TAG,TAG` | Comma-separated tags |
| `--category LABEL` | Category label |
| `--ttl TTL` | Time to live (`30d`, `4w`, `permanent`; default: `permanent`) |

**`list` flags:**

| Flag | Description |
|------|-------------|
| `-c` / `--collection NAME` | Collection name or prefix (default: `knowledge`) |
| `-n` / `--limit NUM` | Maximum entries to show (default: 200) |

---

## nx memory

T2 persistent memory (SQLite + FTS5). See [Storage Tiers](storage-tiers.md) for what T2 holds and how it bridges sessions.

```
nx memory put "auth uses JWT" --project nexus_active --title findings.md --ttl 30d
```

| Subcommand | Description |
|------------|-------------|
| `put CONTENT --project NAME --title NAME` | Write a memory entry |
| `get [ID]` | Read entry by numeric ID |
| `get --project NAME --title NAME` | Read entry by project + title |
| `search QUERY` | FTS5 keyword search |
| `list` | List entries |
| `expire` | Remove expired entries |
| `promote ID --collection NAME` | Promote entry to T3 by ID |

**`put` flags:** `--tags`, `--ttl` (default: `30d`)

**`list` flags:** `--project NAME` (filter by project), `-a` / `--agent NAME` (filter by agent name)

**`promote` flags:** `--collection` (required), `--tags`, `--remove`

**`search` flags:** `--project NAME`

---

## nx scratch

T1 ephemeral session notes (in-memory ChromaDB).

```
nx scratch put "hypothesis: cache invalidation is stale"
```

| Subcommand | Description |
|------------|-------------|
| `put CONTENT` | Store ephemeral note |
| `get ID` | Retrieve by ID |
| `search QUERY` | Search scratch notes |
| `list` | List all notes |
| `flag ID` | Mark for auto-flush to T2 at session end |
| `unflag ID` | Remove flush mark |
| `promote ID --project NAME --title NAME` | Promote to T2 |
| `clear` | Delete all scratch notes |

**`put` flags:** `--tags` (comma-separated), `--persist` (auto-flush to T2), `-p` / `--project` / `-t` / `--title` (explicit T2 destination)

**`flag` flags:** `-p` / `--project` / `-t` / `--title` (explicit T2 destination)

---

## nx pm

Project management (T2 + T3). See [Project Management](project-management.md) for the lifecycle and integration details.

```
nx pm resume
```

| Subcommand | Description |
|------------|-------------|
| `init` | Initialize PM for current git repo |
| `resume` | Print computed PM continuation (phase, blockers, recent activity) |
| `status` | Show current phase, last-updated agent, and open blockers |
| `phase next` | Snapshot current context and advance to the next phase |
| `block BLOCKER` | Record a blocker |
| `unblock LINE` | Remove a blocker by 1-based line number |
| `search QUERY` | FTS5 search across PM docs |
| `promote TITLE` | Promote PM doc to T3 |
| `archive` | Synthesize to T3, start 90-day T2 decay |
| `close` | Archive + mark project completed |
| `restore PROJECT` | Restore within 90-day decay window |
| `reference [QUERY]` | Search archived syntheses in T3 |
| `expire` | Remove TTL-expired PM entries from T2 |

**`init`, `resume`, `status`, `block`, `unblock`, `close` flags:** `--project NAME` (defaults to current git repo name)

**`archive` flags:** `--project NAME`, `--status [completed|paused|cancelled]` (default: `completed`)

**`promote` flags:** `--project NAME`, `--collection NAME` (default: `knowledge__pm__{project}`), `--ttl DAYS` (default: `0` = permanent)

---

## nx collection

Manage T3 cloud collections.

```
nx collection list
```

| Subcommand | Description |
|------------|-------------|
| `list` | All cloud collections with document counts |
| `info NAME` | Details for one collection |
| `verify NAME` | Existence check + document count |
| `delete NAME` | Delete collection (irreversible) |

**`verify` flags:**

| Flag | Description |
|------|-------------|
| `--deep` | Probe embeddings in addition to count |

**`delete` flags:**

| Flag | Description |
|------|-------------|
| `-y` / `--yes` / `--confirm` | Skip interactive confirmation prompt |

---

## nx serve

Background daemon for indexing and search.

```
nx serve start
```

| Subcommand | Description |
|------------|-------------|
| `start` | Start background daemon |
| `stop` | Stop daemon |
| `status` | Uptime and per-repo indexing state |
| `logs` | Recent server log output (last 20 lines by default) |

**`logs` flags:**

| Flag | Description |
|------|-------------|
| `-n` / `--lines NUM` | Number of log lines to show (default: 20) |

---

## nx config

Configuration management.

```
nx config init
```

| Subcommand | Description |
|------------|-------------|
| `init` | Interactive credential wizard |
| `list` | Show all config values |
| `get KEY` | Get single value (masked by default) |
| `set KEY VALUE` | Set single value; also accepts `KEY=VALUE` form |

**`get` flags:**

| Flag | Description |
|------|-------------|
| `--show` | Reveal the full value instead of masking |

---

## nx migrate

One-time migration utilities for upgrading from the legacy single T3 store to the four-store layout.

```
nx migrate t3
```

| Subcommand | Description |
|------------|-------------|
| `t3` | Migrate all T3 data from the legacy single store to the four-store layout |

**`t3` behaviour:**

- **Source**: opens the legacy `chromadb.path` (PersistentClient) if set; otherwise opens via CloudClient using `chroma_api_key`, `chroma_tenant`, and `chroma_database`.
- **Destinations**: opens all four stores using `DefaultEmbeddingFunction` — `voyage_api_key` is **not required** during migration because embeddings are copied verbatim (no re-embedding).
- **Routing**: `code__*` → code store, `docs__*` → docs store, `rdr__*` → rdr store, `knowledge__*` and all others → knowledge store.
- **Idempotent**: if the destination collection already has the same document count as the source, the collection is skipped.
- **Non-destructive**: the source store is never deleted; verify the migration and remove it manually.

**Deployment ordering for CloudClient users:**

Keep `chroma_api_key`, `chroma_tenant`, and `chroma_database` in your config until `nx migrate t3` completes — those credentials are needed to open the CloudClient source store. After migration succeeds you can remove them with `nx config set`.

---

## nx doctor

Health check for all dependencies.

```
nx doctor
```

Checks: ChromaDB API key, ChromaDB tenant, ChromaDB database, Voyage AI key, Anthropic key, ripgrep binary, git binary, Nexus server, Mixedbread key (optional).
