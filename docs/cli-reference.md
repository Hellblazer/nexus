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
| `--hybrid` | Augment semantic results with frecency-weighted ranking and ripgrep keyword matches (0.7*vector + 0.3*frecency). Requires ripgrep |
| `--no-rerank` | Disable cross-corpus reranking (use round-robin instead) |
| `--where KEY{op}VALUE` | Metadata filter (repeatable; multiple flags are ANDed). Operators: `=`, `>=`, `<=`, `>`, `<`, `!=`. Known numeric fields (`bib_year`, `bib_citation_count`, `page_count`, `chunk_count`) are auto-coerced to int. Example: `--where bib_year>=2024 --where chunk_type=table_page` |
| `--max-file-chunks N` | Exclude chunks from files larger than N chunks (code corpora only; ANDs with `--where`) |
| `-m` / `--n` / `--max-results NUM` | Max results (default 10) |
| `-A N` | Show N lines of context after each matching line (within chunk) |
| `-B N` | Show N lines of context before each matching line (within chunk) |
| `-C N` | Show N lines before and after each match (equivalent to `-B N -A N`) |
| `-c` / `--content` | Show matched text inline under each result (truncated at 200 chars) |
| `-r` / `--reverse` | Reverse result order (highest-scoring last) |
| `--vimgrep` | Output as `path:line:col:content` (query-aware: reports best-matching line) |
| `--json` | JSON array output |
| `--files` | Unique file paths only |
| `--compact` | One line per result: `path:line:text` (grep-compatible) |
| `--bat` | Syntax highlight with `bat` (ignored with `--json`/`--vimgrep`/`--files`) |
| `--no-color` | Disable colored output (also skips `--bat`) |

---

## nx index

Index content into T3 collections.

```
nx index repo ./my-project
```

| Subcommand | Description |
|------------|-------------|
| `repo PATH` | Index code repository (smart classification: code to `code__`, prose to `docs__`, RDRs to `rdr__`) |
| `rdr [PATH]` | Index RDR documents in `docs/rdr/` into `rdr__` collection (default: current dir) |
| `pdf PATH` | Index a PDF document into T3 `docs__CORPUS` |
| `md PATH` | Index a Markdown file into T3 `docs__CORPUS` |

**Common flags (all subcommands):**

| Flag | Description |
|------|-------------|
| `--force` | Force re-indexing, bypassing staleness check (re-chunks and re-embeds in-place) |
| `--monitor` | Print per-file progress lines. For `pdf` and `md`, also shows a per-chunk tqdm progress bar during embedding. Auto-enabled when stdout is not a TTY (piped, backgrounded, CI) |

**`repo`-only flags:**

| Flag | Description |
|------|-------------|
| `--frecency-only` | Update frecency scores only; skip re-embedding (faster, for re-ranking refresh). Mutually exclusive with `--force` |
| `--force-stale` | Re-index only if collection pipeline version is outdated (smart force — skips current collections) |
| `--on-locked {skip,wait}` | Behavior when another process holds the repo lock: `skip` exits immediately, `wait` blocks (default: `wait`) |

**`pdf` and `md` flags:**

| Flag | Description |
|------|-------------|
| `--corpus NAME` | Corpus name for the `docs__` collection (default: `default`) |

**`pdf`-only flags:**

| Flag | Description |
|------|-------------|
| `--collection NAME` | Fully-qualified T3 collection name (e.g. `knowledge__delos`). Overrides `--corpus` when set |
| `--no-enrich` | Skip Semantic Scholar bibliographic metadata lookup (useful for offline/bulk indexing) |
| `--dry-run` | Extract and embed locally using ONNX (no API keys, no cloud writes). Prints a chunk preview |

---

## nx enrich

Backfill bibliographic metadata from Semantic Scholar for an existing T3 collection.

```
nx enrich knowledge__papers --delay 0.5 --limit 50
```

Queries Semantic Scholar for each unique `source_title` in the collection and writes `bib_year`, `bib_venue`, `bib_authors`, `bib_citation_count`, and `bib_semantic_scholar_id` back to every chunk with that title. Already-enriched chunks (non-empty `bib_semantic_scholar_id`) are skipped — the command is idempotent.

| Flag | Description |
|------|-------------|
| `COLLECTION` (positional) | Fully-qualified T3 collection name (e.g. `knowledge__papers`) |
| `--delay SECONDS` | Delay between API calls (default: 0.5s). Increase to avoid rate limiting |
| `--limit N` | Maximum number of titles to enrich (default: 0 = unlimited) |

**Note**: Semantic Scholar's public API allows 100 requests per 5 minutes without an API key. For large collections, increase `--delay` or use `--limit` to process in batches.

---

## nx store

Manage T3 knowledge entries.

```
echo "# Cache Strategy" | nx store put - --collection knowledge --title "decision-cache" --tags "decision,arch"
```

| Subcommand | Description |
|------------|-------------|
| `put FILE_OR_DASH` | Store document (use `-` for stdin) |
| `get DOC_ID` | Retrieve entry by 16-char hex ID (from `nx store list`) |
| `list` | List stored entries |
| `delete` | Delete a single entry by ID or title |
| `export [COLLECTION]` | Export a collection to portable `.nxexp` backup |
| `import FILE` | Import a `.nxexp` file into T3 |
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

**`delete` flags:**

| Flag | Description |
|------|-------------|
| `-c` / `--collection NAME` | Collection name (required) |
| `--id ID` | Exact 16-char document ID from `nx store list` |
| `--title TITLE` | Exact title metadata match (deletes all matching chunks) |
| `-y` / `--yes` | Skip confirmation prompt |

Note: IDs shown by `nx store list` are 16 hex chars. `--title` delete is paginated and safe for multi-chunk documents. To delete an entire collection use `nx collection delete`.

**`get` flags:**

| Flag | Description |
|------|-------------|
| `-c` / `--collection NAME` | Collection name or prefix (default: `knowledge`) |
| `--json` | Output as JSON |

**`export` flags:**

| Flag | Description |
|------|-------------|
| `-o` / `--output PATH` | Output file path (`.nxexp`) or directory (when `--all`) |
| `--include GLOB` | Glob pattern matched against `source_path` (repeatable; OR logic) |
| `--exclude GLOB` | Glob pattern matched against `source_path` (repeatable; OR logic) |
| `--all` | Export every collection to separate `.nxexp` files |

**`import` flags:**

| Flag | Description |
|------|-------------|
| `-c` / `--collection NAME` | Override target collection name (default: from export header) |
| `--remap OLD:NEW` | Path substitution for `source_path` metadata (repeatable) |

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
| `delete` | Delete one or more entries |
| `expire` | Remove expired entries |
| `promote ID --collection NAME` | Promote entry to T3 by ID |

**`put` flags:** `--tags`, `--ttl` (default: `30d`)

**`list` flags:** `--project NAME` (filter by project), `-a` / `--agent NAME` (filter by agent name)

**`promote` flags:** `--collection` (required), `--tags`, `--remove`

**`search` flags:** `--project NAME`

**`delete` flags:**

| Flag | Description |
|------|-------------|
| `-p` / `--project NAME` | Project namespace |
| `-t` / `--title NAME` | Entry title |
| `--id ID` | Numeric row ID |
| `--all` | Delete all entries in `--project` (requires `--project`) |
| `-y` / `--yes` | Skip confirmation prompt |

`--id` is mutually exclusive with `--project`, `--title`, and `--all`. Confirmation prompt shows `project/title` and content preview before deleting.

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
| `delete ID` | Delete one entry by ID prefix (no prompt) |
| `flag ID` | Mark for auto-flush to T2 at session end |
| `unflag ID` | Remove flush mark |
| `promote ID --project NAME --title NAME` | Promote to T2 |
| `clear` | Delete all scratch notes |

**`put` flags:** `--tags` (comma-separated), `--persist` (auto-flush to T2), `-p` / `--project` / `-t` / `--title` (explicit T2 destination)

**`flag` flags:** `-p` / `--project` / `-t` / `--title` (explicit T2 destination)

---

## nx collection

Manage T3 collections (local or cloud).

```
nx collection list
```

| Subcommand | Description |
|------------|-------------|
| `list` | All T3 collections with document counts |
| `info NAME` | Details for one collection |
| `verify NAME` | Existence check + document count |
| `reindex NAME` | Delete and re-index a collection from its source documents |
| `delete NAME` | Delete collection (irreversible) |

**`verify` flags:**

| Flag | Description |
|------|-------------|
| `--deep` | Known-document probe: embeds a document already in the collection, queries it back, and reports the retrieval distance. Distance near 0 is healthy; high distance indicates model mismatch or index corruption |

**`reindex` flags:**

| Flag | Description |
|------|-------------|
| `--force` | Skip the pre-delete safety check (which verifies the source documents are still present before wiping the collection) |

The `reindex` command performs a pre-delete safety check before wiping the collection: it confirms the original source documents are still accessible. If the check fails, the command aborts unless `--force` is given. After re-indexing, a `verify --deep` probe runs automatically to confirm retrieval health. The command dispatches per collection type (`code__`, `docs__`, `rdr__`, `knowledge__`) to the appropriate indexer.

**`delete` flags:**

| Flag | Description |
|------|-------------|
| `-y` / `--yes` / `--confirm` | Skip interactive confirmation prompt |

---

## nx hooks

Git hook management for automatic repo indexing.

```
nx hooks install [PATH]
```

| Subcommand | Description |
|------------|-------------|
| `install [PATH]` | Install `post-commit`, `post-merge`, `post-rewrite` hooks (default: `.`) |
| `uninstall [PATH]` | Remove nexus hook stanza; leaves other hook content intact |
| `status [PATH]` | Show hook status for each hook file |

Hooks run `nx index repo` in the background after each qualifying git operation, appending output to `~/.config/nexus/index.log`. If a hook file already exists, the nexus stanza is appended (sentinel-bounded) without overwriting existing content.

**Hook status values:** `not installed` · `owned` (nexus-created) · `appended` (added to existing hook) · `unmanaged` (no nexus sentinel)

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

## nx doctor

Health check for all dependencies.

```
nx doctor
```

Checks: ChromaDB API key, ChromaDB tenant, T3 database (`CHROMA_DATABASE`), Voyage AI key, ripgrep binary, git binary, git hooks status for registered repos, index log last-write time.
