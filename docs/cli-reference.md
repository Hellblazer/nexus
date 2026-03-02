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
| `--hybrid` | Merge semantic + ripgrep results for code (0.7*vector + 0.3*frecency) |
| `--no-rerank` | Disable cross-corpus reranking (use round-robin instead) |
| `--mxbai` | Fan out to Mixedbread-indexed collections (read-only) |
| `--where KEY=VALUE` | Metadata filter (repeatable; multiple flags are ANDed) |
| `--max-file-chunks N` | Exclude chunks from files larger than N chunks (code corpora only; ANDs with `--where`) |
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

Index content into T3 cloud collections.

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
| `--chunk-size N` | Lines per code chunk (default: 150). Smaller values improve search precision for large files at the cost of more chunks. Rejected if N < 1. |
| `--no-chunk-warning` | Suppress the large-file warning that is printed before indexing when large code files are detected. |

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

T1 ephemeral session notes (ChromaDB session server, shared across agents).

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

Migrate T3 collections between store layouts.

```
nx migrate t3
```

| Subcommand | Description |
|------------|-------------|
| `t3` | Copy all collections from the old single-database ChromaDB store to the new four-store layout (`{base}_code`, `{base}_docs`, `{base}_rdr`, `{base}_knowledge`) |

**`t3` flags:**

| Flag | Description |
|------|-------------|
| `-v` / `--verbose` | Print per-collection progress |

**Behaviour:**

- **Non-destructive** — the source database is never modified.
- **Idempotent** — collections already present in the destination with the same document count are silently skipped.
- **Verbatim** — embeddings are copied as-is; no re-embedding is performed.
- **Auto-create** — attempts to create the four destination databases automatically. On Chroma Cloud free-tier plans that restrict `AdminClient`, a warning is printed and migration continues; create the databases manually in the ChromaDB Cloud dashboard first if that happens.

**Prerequisites:**

1. The original single database (e.g. `nexus`) must still exist as the source.
2. Run `nx config init` (or `nx config set`) so `chroma_database` holds the **base name** used for the original database.
3. Optionally create the four destination databases in the ChromaDB Cloud dashboard before running.

---

## nx doctor

Health check for all dependencies.

```
nx doctor
```

Checks: ChromaDB API key, ChromaDB tenant, all four T3 databases (`{base}_code`, `{base}_docs`, `{base}_rdr`, `{base}_knowledge`), Voyage AI key, Anthropic key, ripgrep binary, git binary, Nexus server, Mixedbread key (optional).
