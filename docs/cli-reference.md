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
| `--where KEY{op}VALUE` | Metadata filter (repeatable; multiple flags are ANDed). Operators: `=`, `>=`, `<=`, `>`, `<`, `!=`. Known numeric fields (`bib_year`, `bib_citation_count`, `page_count`, `chunk_count`) are auto-coerced to int. Example: `--where bib_year>=2024 --where section_type!=references` |
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
| `--no-taxonomy` | Skip automatic topic discovery after indexing |

**`pdf` and `md` flags:**

| Flag | Description |
|------|-------------|
| `--corpus NAME` | Corpus name for the `docs__` collection (default: `default`) |

**`pdf`-only flags:**

| Flag | Description |
|------|-------------|
| `--dir DIR` | Index all PDFs in a directory (mutually exclusive with `PATH`) |
| `--collection NAME` | Fully-qualified T3 collection name (e.g. `knowledge__delos`). Overrides `--corpus` when set |
| `--enrich` | Query Semantic Scholar for bibliographic metadata (year, venue, authors, citations). Off by default. Use `nx enrich <collection>` for bulk backfill |
| `--extractor [auto\|docling\|mineru]` | PDF extraction backend (default: `auto`). See [PDF Extraction Backends](#pdf-extraction-backends) below |
| `--dry-run` | Extract and embed locally using ONNX (no API keys, no cloud writes). Prints a chunk preview |
| `--streaming [auto\|always\|never]` | Pipeline mode (default: `auto`). `auto` uses the streaming pipeline for all PDFs (crash-resilient); `never` forces the legacy batch+checkpoint path |

### PDF Extraction Backends

Most PDFs work fine with the default (`auto`). You only need to think about this if you're indexing **math-heavy academic papers** with equations.

**How `auto` works:**

1. Docling extracts the PDF and counts formula regions
2. If **no formulas found** → done (uses Docling output as-is, zero overhead)
3. If **formulas found** → tries MinerU for better LaTeX extraction
4. If MinerU isn't installed → returns the Docling result anyway

**What you get without MinerU installed:**
- All PDFs extract normally via Docling
- Math-heavy PDFs get a `has_formulas: true` flag on their chunks (useful for filtering)
- Formula regions are detected but not re-extracted with MinerU

**What MinerU adds (optional):**
- Superior LaTeX extraction for display and inline equations
- ~2.9x faster than Docling's formula enrichment mode on equation-heavy papers
- Large PDFs are automatically split into 5-page batches, each processed in
  an isolated subprocess to prevent OOM on formula-dense documents

**Installing MinerU:**

```bash
uv pip install 'conexus[mineru]'
```

First run downloads the unimernet model (~2-3 GB). After that, `auto` mode automatically routes math-heavy PDFs through MinerU.

**Setting a default backend (sticky config):**

```bash
nx config set pdf.extractor=mineru    # global, applies to all repos
```

Or add to `.nexus.yml` (per-repo) or `~/.config/nexus/config.yml` (global) directly:

```yaml
pdf:
  extractor: mineru   # auto | docling | mineru
```

The `--extractor` flag overrides the config when passed explicitly.

**Forcing a specific backend (one-off):**

```bash
nx index pdf paper.pdf --extractor docling   # Always Docling (no MinerU attempt)
nx index pdf paper.pdf --extractor mineru    # Always MinerU (fails if not installed)
```

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

## nx catalog

Document catalog — track indexed documents and the relationships between them.

### nx catalog setup

```
nx catalog setup [--remote URL]
```

One-command onboarding: creates the catalog, populates from existing T3 collections and repos, generates links. Run once after installing or upgrading. Warns if no git remote is configured (cloud users should add one for durability).

On a new machine with an existing catalog remote: `nx catalog setup --remote <url>` clones from the remote instead of creating an empty catalog.

### nx catalog search

```
nx catalog search QUERY [--limit N] [--offset N] [--json]
```

Find documents by title, author, corpus, or file path. Returns tumbler, content type, and title.

### nx catalog show

```
nx catalog show TUMBLER_OR_TITLE [--json]
```

Full document metadata, physical collection, and all links in and out. Accepts tumblers or titles.

### nx catalog links

```
nx catalog links [TUMBLER] [--from TEXT] [--to TEXT] [--type TEXT] [--created-by TEXT] [--direction in|out|both] [--depth N] [--limit N] [--offset N] [--json]
```

With a positional tumbler/title: BFS graph traversal. Without: flat filter query across all links.

### nx catalog link

```
nx catalog link FROM TO --type TYPE [--from-span SPAN] [--to-span SPAN]
```

Create a typed link. Both endpoints accept tumblers or titles. Types: `cites`, `implements`, `implements-heuristic`, `supersedes`, `quotes`, `relates`, `comments`, `formalizes`.

Span formats: `line-line` (positional), `chunk:char-char` (positional), `chash:<sha256hex>` (whole chunk, content-addressed), or `chash:<sha256hex>:<start>-<end>` (character range within a chunk). Content-hash spans survive re-indexing; positional spans may become stale.

### nx catalog unlink

```
nx catalog unlink FROM TO [--type TYPE]
```

Remove link(s). Omit `--type` to remove all link types between the pair.

### nx catalog sync / pull

```
nx catalog sync [-m MESSAGE]     # commit JSONL changes + push to remote (if configured)
nx catalog pull                  # pull from remote + rebuild SQLite
```

`sync` is called automatically at session close (via the Stop hook) when JSONL files have changed. Manual use is rarely needed.

### nx catalog orphans

```
nx catalog orphans --no-links
```

Find catalog entries with zero incoming and outgoing links. Useful for identifying documents that need linking or cleanup.

### nx catalog coverage

```
nx catalog coverage [--owner OWNER_PREFIX]
```

Per content-type report showing what percentage of catalog entries have at least one link. Use `--owner 1.1` to scope to a specific owner prefix.

### nx catalog suggest-links

```
nx catalog suggest-links [--limit N]
```

Find unlinked code-RDR pairs by module name overlap. Read-only — shows potential links without creating them.

### nx catalog links-for-file

```
nx catalog links-for-file FILE_PATH
```

Show all linked documents for a specific file (by relative path). Displays link type and direction.

### nx catalog session-summary

```
nx catalog session-summary [--since HOURS]
```

Show linked RDRs for recently git-modified files. Default: last 24 hours. Useful for understanding design context of files you're working on.

### nx catalog link-generate

```
nx catalog link-generate [--dry-run]
```

Run the RDR filepath link generator over the full catalog. Use for initial setup or after bulk imports. Normal index runs are incremental. For citation links too, use `nx catalog generate-links`.

### nx catalog generate-links

```
nx catalog generate-links [--citations/--no-citations] [--filepath/--no-filepath] [--dry-run]
```

Auto-generate typed links from metadata cross-matching. `--citations` generates citation links from bibliographic metadata. `--filepath` generates RDR-to-code links by file path matching. Both default to enabled.

### nx catalog update

```
nx catalog update [TUMBLER] [--title TEXT] [--author TEXT] [--year N] [--corpus TEXT] [--meta JSON]
nx catalog update --owner PREFIX --corpus TEXT    # batch update all entries under an owner
nx catalog update --search QUERY --corpus TEXT    # batch update all entries matching search
```

Update catalog entry metadata. `TUMBLER` accepts a tumbler or title. Batch mode uses `--owner` or `--search` to update multiple entries at once.

### nx catalog gc

```
nx catalog gc [--dry-run]
```

Remove orphan catalog entries (entries with `miss_count >= 2` — missed in 2 consecutive index runs). Use `--dry-run` to preview.

### nx catalog list / stats / owners / delete

Standard catalog management. Run `nx catalog COMMAND --help` for details.

---

## nx taxonomy

Topic taxonomy — HDBSCAN clustering of T3 collection embeddings into topics for navigation, search grouping, and relevance boosting.

Topics are auto-discovered after `nx index repo` and auto-labeled with Claude haiku when available. Search results are grouped by topic and boosted when results share a topic cluster.

```
nx taxonomy status                              # health: collections, coverage, review state
nx taxonomy discover --all                      # discover topics for all T3 collections
nx taxonomy discover -c docs__nexus             # discover for a single collection
nx taxonomy discover -c docs__nexus --force     # re-discover (preserves operator labels)
nx taxonomy list                                # topic tree
nx taxonomy list -c docs__nexus                 # topic tree for one collection
nx taxonomy show 5                              # docs assigned to topic 5
nx taxonomy review                              # interactive: accept/rename/merge/delete/skip
nx taxonomy label                               # batch-relabel with Claude haiku
nx taxonomy assign doc-id "topic label"         # manually assign a doc
nx taxonomy rename "old label" "new label"      # rename a topic
nx taxonomy merge "source" "target"             # merge topics
nx taxonomy split "label" --k 3                 # split into sub-topics
nx taxonomy links                               # show inter-topic relationships
nx taxonomy rebuild -c docs__nexus              # full rebuild
```

| Subcommand | Description |
|------------|-------------|
| `status` | Collections, topic count, coverage, review state |
| `discover` | Discover topics via HDBSCAN. `--all` for all collections, `-c NAME` for one, `--force` to re-cluster |
| `list` | Topic tree with doc counts. `-c NAME` filters by collection, `-d N` sets tree depth (default: 2) |
| `show ID` | Documents assigned to a topic. `-n N` limits results (default: 20) |
| `review` | Interactive review: accept, rename, merge, delete, skip. `-c NAME` to filter, `-n N` topics per session (default: 15) |
| `label` | Batch-relabel topics with Claude haiku. `--all` relabels accepted topics too |
| `assign DOC LABEL` | Manually assign a doc to a topic by label. `-c NAME` scopes label lookup |
| `rename OLD NEW` | Rename a topic. `-c NAME` scopes label lookup |
| `merge SOURCE TARGET` | Merge source into target. `-c NAME` scopes label lookup |
| `split LABEL --k N` | Split into N sub-topics via KMeans. `-c NAME` scopes label lookup |
| `links` | Inter-topic link counts from catalog graph. `-c NAME` filters by collection |
| `rebuild` | Full re-cluster (alias for `discover --force`). `-c NAME` required |

**Configuration** (in `.nexus.yml`):

```yaml
taxonomy:
  auto_label: true                    # label with Claude haiku after discover (default: true)
  local_exclude_collections: []       # default: ["code__*"] — MiniLM clusters poorly on code
```

**Upgrade path**: Run `nx taxonomy discover --all` once after upgrading to populate topics for existing collections.

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
| `--offset N` | Skip this many entries (for pagination) |
| `--docs` | Show unique documents instead of individual chunks |

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
| `promote ID --project NAME --title NAME` | Promote to T2, report `action=new` or `overlap_detected` |
| `clear` | Delete all scratch notes |

**`put` flags:** `--tags` (comma-separated), `--persist` (auto-flush to T2), `-p` / `--project` / `-t` / `--title` (explicit T2 destination)

**`flag` flags:** `-p` / `--project` / `-t` / `--title` (explicit T2 destination)

**`search` flags:** `--n N` (max results, default: 10)

**`promote` output and semantics:** `nx scratch promote` echoes the
promotion result as `Promoted <id> -> <project>/<title> (action=<ACTION>)`.
Two actions are possible today:

- `action=new` — no similar entry found under the target project. Clean write.
- `action=overlap_detected` — an FTS5 keyword scan found a similar entry in the
  target project under a different title. The new row is **still** written to
  T2 as a separate entry — the report is an advisory, not a rejection.
  Agents should decide whether to manually merge via `memory_consolidate(action="merge", ...)`.

The underlying `T1.promote()` method returns a full `PromotionReport` dataclass
with `action`, `existing_title`, and `merged` fields. The CLI surfaces only the
`action` field; the full report is available to agents through `scratch_manage`
and Python API callers. See [Storage Tiers § Progressive Formalization](storage-tiers.md#progressive-formalization-rdr-057).

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
| `backfill-hash [NAME]` | Add `chunk_text_hash` metadata to chunks missing it (no re-embedding) |
| `delete NAME` | Delete collection (irreversible) |

**`verify` flags:**

| Flag | Description |
|------|-------------|
| `--deep` | Multi-probe health check: embeds up to 5 documents already in the collection, queries each back, and reports the probe hit rate. Status: `healthy` (100%), `degraded` (partial hits), `broken` (0%). Shows distance of last successful probe and the metric used |

**`reindex` flags:**

| Flag | Description |
|------|-------------|
| `--force` | Skip the pre-delete safety check (which verifies the source documents are still present before wiping the collection) |

The `reindex` command performs a pre-delete safety check before wiping the collection: it confirms the original source documents are still accessible. If the check fails, the command aborts unless `--force` is given. After re-indexing, a `verify --deep` probe runs automatically to confirm retrieval health. The command dispatches per collection type (`code__`, `docs__`, `rdr__`, `knowledge__`) to the appropriate indexer.

**`backfill-hash` flags:**

| Flag | Description |
|------|-------------|
| `--all` | Backfill all collections instead of a single named one |

Reads each chunk's stored text from ChromaDB and computes `sha256(text.encode()).hexdigest()`, updating metadata in-place. Embeddings and documents are untouched — no API keys or re-embedding needed. Idempotent: chunks that already have `chunk_text_hash` are skipped. Also runs automatically during `nx catalog setup`.

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

Checks: ChromaDB API key, ChromaDB tenant, T3 database (`CHROMA_DATABASE`), Voyage AI key, ripgrep binary, git binary, git hooks status for registered repos, index log last-write time, orphaned PDF checkpoints, orphaned pipeline buffer entries, T2 integrity.

```
nx doctor --clean-checkpoints   # Delete orphaned PDF checkpoint files
nx doctor --clean-pipelines     # Delete orphaned pipeline buffer entries
nx doctor --fix                 # Apply HNSW search_ef=256 to local collections
nx doctor --fix-paths           # Migrate absolute file_path entries to relative (catalog + T3)
nx doctor --fix-paths --dry-run # Preview migration without applying
```

The `--fix` flag retroactively applies HNSW `search_ef` tuning to all existing local-mode collections. New collections get this automatically. In cloud mode (SPANN), prints a skip message — SPANN defaults are adequate.

---

## nx console

Embedded web UI for monitoring agentic Nexus activity.

```
nx console [--port PORT] [--host HOST]
```

Starts a foreground FastAPI/uvicorn server. The PID file is written to `~/.config/nexus/console.<project>.pid` and removed on exit.

| Flag | Description |
|------|-------------|
| `--port PORT` | Port for the console server (default: 8765) |
| `--host HOST` | Host to bind to (default: `127.0.0.1`) |

---

## nx context

Project context cache for agent cold-start acceleration. Generates a compact topic map (~200 tokens) from taxonomy data and caches it for injection at session start.

```
nx context refresh
nx context show
```

| Subcommand | Description |
|------------|-------------|
| `refresh` | Regenerate the L1 context cache from current taxonomy topics |
| `show` | Display the current cached context for the current repo. Prints guidance if no cache exists |

**`refresh` flags:**

| Flag | Description |
|------|-------------|
| `--global` | Generate a single global cache (all collections) instead of per-repo |

The per-repo cache is stored at `~/.config/nexus/context/<repo>-<hash>.txt`. The global cache (via `--global`) is at `~/.config/nexus/context_l1.txt`. Both `show` and the SessionStart/SubagentStart hooks resolve the per-repo path first, falling back to global. The cache is automatically regenerated after `nx taxonomy discover` and `nx index repo`.

---

## nx mineru

MinerU server lifecycle management for PDF extraction. Requires `conexus[mineru]` extra.

### nx mineru start

```
nx mineru start [--port PORT]
```

Start a persistent `mineru-api` FastAPI process for PDF extraction. Stores PID file at `~/.config/nexus/mineru.pid`.

| Flag | Description |
|------|-------------|
| `--port PORT` | Port for mineru-api (default: 0 = auto-assign) |

### nx mineru stop

```
nx mineru stop
```

Stop the running MinerU server. Sends SIGTERM, waits up to 10s.

### nx mineru status

```
nx mineru status
```

Show server status: running/stopped, PID, port, active tasks, and completed tasks. Removes stale PID file if the server process is no longer running.
