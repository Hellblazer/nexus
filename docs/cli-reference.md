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
| `--threshold DISTANCE` | Override per-collection distance threshold (raw cosine distance, lower = stricter). Applies uniformly across selected collections. (RDR-087) |
| `--no-threshold` | Disable distance-threshold filtering entirely. Mutually exclusive with `--threshold`. Workaround for silent threshold-drop on dense-prose collections. (RDR-087) |
| `--quiet` | Suppress the RDR-087 silent-zero stderr note ("candidates dropped across N collections...") when every candidate is filtered by the distance threshold |

### Search telemetry (`.nexus.yml`)

RDR-087 observability surfaces are configurable via the `telemetry` section:

```yaml
telemetry:
  search_enabled: true        # Phase 2.2: persist per-call threshold-filter rows to T2 (search_telemetry table)
  stderr_silent_zero: true    # Phase 1.2: emit stderr note when a query returns zero results due to threshold filtering
```

Both default `true`. Set either to `false` to opt out project-wide. Query strings are sha256-hashed before persistence — raw queries are never stored.

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
| `--debug-timing` | Emit an end-of-run per-stage breakdown to stderr (chunking / embed / upload / retry seconds per file, aggregated with percentages). Instruments code, prose, and PDF per-file paths — silent without the flag. Use when investigating "why did indexing take N minutes?" (introduced 4.9.0, nexus-7niu) |

**Observability output** (stderr, all emitted automatically during `repo` runs):

- **Per-file line** — `  [N/total] path — K chunks  (T.Ts)` printed as each file completes (or when `--monitor` / no-TTY).
- **`[eta]` line** — every 60 s: `[eta] N/total files · C chunks · Xs/file avg · ~M min remaining`. Fires regardless of TTY so CI / `nohup` / `tail -f` see pace even when tqdm suppresses its bar (introduced 4.8.0, nexus-vatx Gap 3).
- **`[post]` phase markers** — after the per-file loop, the pipeline keeps running for RDR discovery, pruning, pipeline-version stamping, and catalog registration. Each phase emits `[post] <phase>…` / `[post] <phase> done (Xs)`, bookended by `[post] Post-processing complete (Xs)` (introduced 4.8.0, nexus-vatx Gap 2).
- **Transient-error backoff summary** — on exit, if any Voyage / ChromaDB retry fired: `Transient-error backoff: Xs total (voyage ..., chroma ...)`. Silent on clean runs. Visible on exception paths (introduced 4.8.0, nexus-vatx Gap 4a).

**`pdf` and `md` flags:**

| Flag | Description |
|------|-------------|
| `--corpus NAME` | Corpus name for the `docs__` collection (default: `default`) |

**`pdf`-only flags:**

| Flag | Description |
|------|-------------|
| `--dir DIR` | Index all PDFs in a directory (mutually exclusive with `PATH`) |
| `--collection NAME` | Fully-qualified T3 collection name (e.g. `knowledge__delos`). Overrides `--corpus` when set |
| `--enrich` | Query Semantic Scholar for bibliographic metadata (year, venue, authors, citations). Off by default. Use `nx enrich bib <collection>` for bulk backfill |
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

## nx dt

DEVONthink integration verbs (macOS only). Wraps DT so selections, smart
groups, tags, and groups flow into Nexus indexing without manual
UUID/path copying, and Nexus search results round-trip back to DT via
`nx dt open`. Design rationale and acceptance criteria live in
[RDR-099](rdr/rdr-099-devonthink-integration.md); the smart-rule recipe
is in [`devonthink-smart-rules.md`](devonthink-smart-rules.md).

The substrate (`x-devonthink-item://` URI scheme,
`meta.devonthink_uri` reverse-lookup) shipped in 4.17.0; `nx dt` is
the operator-facing surface.

### nx dt index

Index DT records into Nexus. Exactly one selector flag must be supplied:
`--selection`, `--tag`, `--group`, `--smart-group`, or one or more
`--uuid`. Per-record dispatch routes `.pdf` paths to `nx index pdf` and
`.md` paths to `nx index md`; other extensions are skipped with a WARN.

```bash
# Whatever is currently selected in DT's UI.
nx dt index --selection

# Every record carrying a tag, across all open libraries.
nx dt index --tag research

# Same, scoped to one library.
nx dt index --tag research --database NexusTest

# Recursive walk under a group path.
nx dt index --group "/AI/2025"

# Execute a smart group's saved query (honouring its search-group scope
# and exclude-subgroups flag).
nx dt index --smart-group "Recent PDFs"

# One or more known UUIDs.
nx dt index --uuid 8EDC855D-213F-40AD-A9CF-9543CC76476B
nx dt index --uuid UUID-A --uuid UUID-B --uuid UUID-C

# See what would be indexed without writing.
nx dt index --selection --dry-run
```

| Flag | Description |
| --- | --- |
| `--selection` | Index records currently selected in DT's UI |
| `--tag <name>` | Index every record carrying this tag |
| `--group <path>` | Index every record under this group path (recursive) |
| `--smart-group <name>` | Run the smart group's saved query and index its results |
| `--uuid <UUID>` | Index a single record; repeat for batch ingest |
| `--database <name>` | Limit selectors to one DT library (default: every open library) |
| `--collection <name>` | T3 collection override (e.g. `knowledge__papers`) |
| `--corpus <name>` | Corpus name for `docs__` collection (default: `default`) |
| `--dry-run` | Print records that would be indexed; make no T3 writes |

Multi-database default is the right behaviour for tags shared across
libraries (a `nexus-test` tag in both `Inbox` and a project library
returns records from both). Use `--database` when scope matters.

Smart groups honour their author-defined `search group` and
`exclude subgroups` properties. A smart group with `search group =
missing value` falls through to whole-library search.

Exit codes:

- `0`: indexed (or dry-ran) successfully, including the no-records case.
- `1`: DT not running, malformed selectors, or non-darwin platform.
- `2`: Click usage error (missing or mutually-exclusive flags).

### nx dt open

Open a record in DEVONthink by tumbler or UUID. UUIDs become
`x-devonthink-item://<UUID>` directly; tumblers are resolved via the
catalog, preferring `meta.devonthink_uri` and falling back to
`source_uri` when the entry was registered with a DT identity.

```bash
# UUID form: no catalog hit, no osascript spawn.
nx dt open 8EDC855D-213F-40AD-A9CF-9543CC76476B

# Tumbler form: catalog lookup yields the DT URI.
nx dt open 1.2.3
```

Exit codes:

- `0`: `open <uri>` invoked successfully.
- `1`: tumbler not found, no DT URI on the entry, malformed argument,
  or non-darwin platform.

### nx dt install-scripts

Install (or remove) DT-side AppleScripts that wrap `nx dt index` so
the actions are reachable from inside DEVONthink without a Claude
Code or terminal detour. Each script appears as a draggable Toolbar
button (`Toolbar/`) and/or in DT's own Scripts menu (`Menu/`, left
of Help).

```bash
# Default: install everything into both Toolbar/ and Menu/.
nx dt install-scripts

# Toolbar buttons only.
nx dt install-scripts --target toolbar

# Preview without writing.
nx dt install-scripts --dry-run

# Remove every installed script.
nx dt install-scripts --uninstall
```

| Flag | Description |
|------|-------------|
| `--target [toolbar\|menu\|all]` | Which DT script slot to install into. Default `all`. |
| `--uninstall` | Remove installed scripts instead of installing. Idempotent on missing files. |
| `--force` | Overwrite existing files without prompting. |
| `--dry-run` | Show what would happen without writing or deleting. |
| `--app-scripts-dir PATH` | Override the DT Application Scripts root. Used by tests; rarely needed. |

Default install root:
`~/Library/Application Scripts/com.devon-technologies.think/`. The
verb is macOS-only and exits non-zero on other platforms.

Shipped scripts (DT4):

| File | Subdirs | Behaviour |
|------|---------|-----------|
| `Index Selection in nx.applescript` | `Toolbar/`, `Menu/` | Calls `nx dt index --selection` for whatever is highlighted in the front viewer window. |
| `Index Selection in nx (Knowledge).applescript` | `Menu/` | Prompts for a collection name, then calls `nx dt index --selection --collection knowledge__<name>`. |
| `Index Current Group in nx.applescript` | `Toolbar/`, `Menu/` | Recursively walks the current group's records and calls `nx dt index --uuid <U> --uuid <V> ...` in a single subprocess. |

After install, restart DEVONthink so newly-installed Toolbar files
become draggable in `View > Customize Toolbar…`. Menu items are
picked up on the next menu open. Each script logs to
`~/Library/Logs/nexus-dt-scripts.log` and backgrounds the shell call
with a trailing `&` so DT's UI stays responsive.

For automatic indexing on import (no manual click), see the smart-rule
recipe in [`docs/devonthink-smart-rules.md`](devonthink-smart-rules.md).

### Cross-references

- In-DT scripts (toolbar / menu):
  [`docs/devonthink-scripts.md`](devonthink-scripts.md).
- Smart rule + folder action recipes:
  [`docs/devonthink-smart-rules.md`](devonthink-smart-rules.md).
- Manual smoke runbook + fixture creation:
  [`tests/e2e/devonthink-manual.md`](../tests/e2e/devonthink-manual.md).
- Design rationale + acceptance criteria:
  [RDR-099](rdr/rdr-099-devonthink-integration.md).

---

## nx enrich

Subcommand group. The previous single-shape `nx enrich <coll>` is now `nx enrich bib <coll>`; a new `nx enrich aspects <coll>` ships RDR-089's structured-aspect extraction.

### nx enrich bib

Backfill bibliographic metadata from Semantic Scholar for an existing T3 collection.

```
nx enrich bib knowledge__papers --delay 0.5 --limit 50
```

Queries Semantic Scholar for each unique `source_title` in the collection and writes `bib_year`, `bib_venue`, `bib_authors`, `bib_citation_count`, and `bib_semantic_scholar_id` back to every chunk with that title. Already-enriched chunks (non-empty `bib_semantic_scholar_id`) are skipped — the command is idempotent.

| Flag | Description |
|------|-------------|
| `COLLECTION` (positional) | Fully-qualified T3 collection name (e.g. `knowledge__papers`) |
| `--delay SECONDS` | Delay between API calls (default: 0.5s). Increase to avoid rate limiting |
| `--limit N` | Maximum number of titles to enrich (default: 0 = unlimited) |

**Note**: Semantic Scholar's public API allows 100 requests per 5 minutes without an API key. For large collections, increase `--delay` or use `--limit` to process in batches.

### nx enrich aspects

Batch-extract structured aspects (problem formulation, proposed method, datasets, baselines, results, extras) for documents in a `knowledge__*` collection. Iterates the catalog (one entry per source document, NOT per chunk) and calls the synchronous extractor directly, bypassing the post-document hook chain to avoid double-firing on documents already triggered at ingest. Aspects land in T2 `document_aspects`.

```
nx enrich aspects knowledge__delos
nx enrich aspects knowledge__delos --dry-run
nx enrich aspects knowledge__delos --validate-sample 10
nx enrich aspects knowledge__delos --re-extract --extractor-version claude-haiku-4-5-20251001
```

| Flag | Description |
|------|-------------|
| `COLLECTION` (positional) | Must be a `knowledge__*` collection (Phase 1 scope). Other prefixes return a "no extractor config" error |
| `--dry-run` | Report document count + cost estimate (Haiku-class). No API calls, no T2 writes |
| `--validate-sample N` | Validate N% of newly-extracted aspects via `operator_verify` against the document text. Disagreements append to `./validation_failures.jsonl`. Pass 0 to skip. Default 5 |
| `--re-extract` | Re-run only on rows whose `model_version` is strictly less than `--extractor-version` (and rows that are missing entirely) |
| `--extractor-version v` | Threshold for `--re-extract` (lexicographic STRICT-less-than) |

### nx enrich list

```
nx enrich list COLLECTION [--limit N] [--json]
```

Day 2 Ops: list extracted aspect rows for a collection. One row per source document (not per chunk). Returns source path, extractor name, model version, extracted-at timestamp, and a confidence indicator. Useful for triaging "what got extracted?" before running `--re-extract` against a model upgrade.

### nx enrich info

```
nx enrich info COLLECTION SOURCE_PATH [--json]
```

Day 2 Ops: show the full aspect record for a single document. Includes the five fixed columns (problem_formulation, proposed_method, experimental_datasets, experimental_baselines, experimental_results), the `extras` JSON object, confidence, extracted_at, model_version, and extractor_name.

### nx enrich delete

```
nx enrich delete COLLECTION SOURCE_PATH [--yes]
```

Day 2 Ops: remove a single aspect row. Use when re-indexing a document with a content change that should drop the prior aspects rather than overwrite. Requires `--yes` for confirmation. Safe: the underlying chunks in T3 are untouched.

### nx enrich aspects-promote-field

```
nx enrich aspects-promote-field NAME [--type {TEXT|INTEGER|REAL}] [--prune] [--history]
```

Promote a recurring `extras.<name>` key into a fixed column on `document_aspects` (RDR-089 Phase E). Three-phase mechanic:

1. `ALTER TABLE document_aspects ADD COLUMN <name> <type>` (idempotent — re-running on an already-promoted field is a no-op).
2. Backfill: copy the value of `extras.<name>` into the new column for every existing row via `json_extract`.
3. (Optional) `--prune` removes the `extras.<name>` key after backfill so the source of truth is the new column.

The promotion is logged to `aspect_promotion_log` (registry-managed) for audit. `--history` lists past promotions instead of running a new one. Reserved names (the 12 RDR-locked column names) and unsafe identifiers (digit prefix, hyphen, quote, semicolon, SQL-injection patterns, empty) are rejected before any DDL runs.

| Flag | Description |
|------|-------------|
| `NAME` (positional) | The `extras.<name>` key to promote into a fixed column. Validated against an alphanumeric+underscore identifier rule |
| `--type {TEXT|INTEGER|REAL}` | SQL column type. Default `TEXT`. `BLOB`, `JSON`, and other types are rejected |
| `--prune` | After backfill, remove the `extras.<name>` key from every row. Use when the new column should replace the extras key as the source of truth. **Destructive** — re-running with `--prune` after data has been written to the new column has no effect, but rolling back requires reverting the schema change manually |
| `--history` | List prior promotions from the audit log instead of running a new promotion |

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

### nx catalog audit-membership

```
nx catalog audit-membership <COLLECTION>
nx catalog audit-membership <COLLECTION> --json
nx catalog audit-membership <COLLECTION> --canonical-home '/git/ART' --purge-non-canonical --dry-run
nx catalog audit-membership <COLLECTION> --canonical-home '/git/ART' --purge-non-canonical --yes
```

Detect cross-project source_uri contamination in a single physical_collection. Catalog entries are grouped by their `source_uri` "home" (the first four path segments for `file://` URIs, `<scheme>://<netloc>` otherwise); per-home counts surface multi-root collections that look correct in `nx catalog list` but break aspect extraction (the chunks live under one project's identity, every other-project entry skips with `reason=empty`).

| Flag | Description |
|------|-------------|
| `COLLECTION` (positional) | Physical collection to audit (e.g. `rdr__ART-8c2e74c0`) |
| `--purge-non-canonical` | Delete entries whose home does not match the canonical one. Use with `--dry-run` first |
| `--canonical-home SUBSTR` | Override the dominant-home heuristic. Required when the contaminating entries outnumber the legitimate ones (e.g. `--canonical-home '/git/ART'`) |
| `--dry-run` | With `--purge-non-canonical`, preview without writing |
| `--yes` / `-y` | Skip the purge confirmation prompt |
| `--json` | Emit per-home counts as JSON |

The dominant home (numerical majority) is the default canonical. When dominance is wrong (e.g. ART-lhk1: 140 contaminating nexus URIs vs 105 legitimate ART URIs in `rdr__ART-...`), pass `--canonical-home` with a unique substring of the right home. Deletion is the standard `delete_document` path: tombstoned in JSONL, removed from SQLite, links preserved as orphans.

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

### nx catalog link-density

```
nx catalog link-density --by-collection [--depth N] [--purpose NAME] [--json]
```

Per-collection report of outgoing-link counts at the depth-N BFS frontier (default depth 2). Output: one row per collection with `frontier_p50`, `frontier_p90`, and the set of `link_types` present. Introduced 4.18.0 (RDR-097, `nexus-8el5`) as observability for the hybrid retrieval plan: collections with median frontier `< 3` are poor candidates for `hybrid-factual-lookup` and the operator should fall back to a vector-only plan. The CLI is observability only; it does not auto-rewrite plans.

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
nx taxonomy project code__nexus                 # project against sibling collections
nx taxonomy project code__nexus --against knowledge__art  # explicit targets
nx taxonomy project code__nexus --use-icf --persist  # suppress hub topics (RDR-077)
nx taxonomy project --backfill --persist        # project all collections
nx taxonomy hubs --min-collections 5 --max-icf 1.2 --explain  # hub detector (RDR-077)
nx taxonomy audit --collection code__nexus                    # projection quality audit (RDR-077)
nx taxonomy validate-refs docs/**/*.md                        # stale-reference validator (RDR-081)
```

### `nx taxonomy validate-refs`

Scan markdown docs for stale collection references and chunk-count claims
that have drifted from current T3 state. **Deterministic** — pure regex
plus `collection_list()` / `count()` lookups; no LLM.

```
nx taxonomy validate-refs docs/rdr/README.md docs/architecture.md
nx taxonomy validate-refs docs/**/*.md --strict                 # exit 1 on Missing too
nx taxonomy validate-refs docs/**/*.md --tolerance 0.20         # ±20% count window
nx taxonomy validate-refs docs/**/*.md --format json            # machine-readable
nx taxonomy validate-refs docs/**/*.md --prefixes docs,code     # override whitelist
```

Scans for `<prefix>__<name>` references (default prefixes `docs`, `code`,
`knowledge`, `rdr`) and proximate chunk-count claims like `"12,900 chunks"`,
`"~13k chunks"`. References inside fenced code blocks (``` ``` ``` or `~~~`)
are ignored so tutorial snippets don't false-positive.

Per-reference verdicts:
- `OK` — collection exists and (when a count is claimed) it matches within tolerance.
- `Drift` — collection exists but the claimed count differs by more than `--tolerance`.
- `Missing` — collection is not in the current T3 (renamed, split, or never indexed).

Exit codes: `0` = all OK (or only `Missing` without `--strict`); `1` = drift
(or `Missing` with `--strict`); `2` = scanner or T3 failure.

Prefix whitelist can be configured in `.nexus.yml`:

```yaml
taxonomy:
  collection_prefixes: [docs, code, knowledge, rdr, custom]
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
| `project SOURCE` | Cross-collection projection: match chunks against other collections' centroids. `--against TARGETS` for explicit targets (default: sibling collections). `--threshold N` (optional; when omitted uses per-corpus defaults: `code__*` 0.70, `knowledge__*` 0.50, `docs__*`/`rdr__*` 0.55 — see [taxonomy-projection-tuning.md](taxonomy-projection-tuning.md)). `--use-icf` suppresses hub topics via Inverse Collection Frequency weighting (RDR-077). `--persist` to write assignments. `--backfill` to project all collections against each other |
| `hubs` | List generic-pattern hub topics (RDR-077 Phase 5). `--min-collections N` (default 2), `--max-icf F` filter, `--warn-stale` flags hubs whose latest assignment post-dates the newest `last_discover_at` across contributing source collections, `--explain` shows DF / ICF / matched stopword tokens per row. |
| `audit --collection NAME` | Per-collection projection-quality report (RDR-077 Phase 6): total assignments, p10/p50/p90 of raw cosine, count below threshold (re-projection candidates), top receiving topics with ICF, pattern-pollution flags. `--threshold F` overrides the per-corpus default; `--top-n N` caps the receiving-topic list. |

**Configuration** (in `.nexus.yml`):

```yaml
taxonomy:
  auto_label: true                    # label with Claude haiku after discover (default: true)
  local_exclude_collections: []       # default: ["code__*"] — MiniLM clusters poorly on code
```

**Upgrade path**: Run `nx upgrade` after upgrading to apply pending migrations and T3 upgrade steps (including cross-collection projection backfill). Run `nx taxonomy discover --all` to populate topics for new collections.

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
| `rename OLD NEW` | In-place rename via ChromaDB `modify(name=)` + T2 + catalog cascade (4.8.0, nexus-1ccq) |
| `audit NAME` | Deep-dive per-collection report: distance histogram, top-5 cross-projections, orphan chunks, hub topics, chash coverage (RDR-087 Phase 4) |
| `health` | Composite per-collection health table — chunk counts (T3-sourced), staleness, hub score, chash coverage (RDR-087 Phase 3.4) |
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

**RDR-086 Phase 1.3 — T2 `chash_index` reconciliation.** The same per-chunk
pass also populates the T2 `chash_index` table so `nx doc cite` and
`Catalog.resolve_chash` can answer "which collection + doc_id holds this
chunk hash?" in ~50 µs instead of scanning ChromaDB. Reconciles gaps left
by Phase 1.2 dual-write failures and pre-Phase-1 collections indexed before
the dual-write existed. A tqdm progress bar renders in an interactive
terminal (auto-disabled on non-TTY CI logs).

Scale reference: a full `--all` on a 278k-chunk / 136-collection corpus
takes ~25–70 minutes on ChromaDB Cloud. Maintenance-window operation.

**`rename` flags:**

| Flag | Description |
|------|-------------|
| `--force-prefix-change` | Allow a cross-prefix rename (e.g. `code__foo` → `docs__foo`). Embedding-model spaces differ across prefixes, so the renamed collection is query-incompatible with its old clients — use only when you've deleted every downstream reader |

Uses ChromaDB's native `modify(name=)` for an O(1) metadata update — no embedding re-upload, no Voyage cost, no ChromaDB egress. Cascades the new name through T2 taxonomy, `chash_index`, and catalog (JSONL + SQLite). The cascade is fail-open by design: T3 renames first; a T2 or catalog failure prints a `warn: …` line on stderr but leaves T3 renamed so the operation is recoverable by retrying the cascade alone.

**`audit` flags:**

| Flag | Description |
|------|-------------|
| `--live` | When the 30-day `search_telemetry` histogram is empty, sample live chunks from ChromaDB and derive the distance histogram from self-queries (4.8.0, nexus-fx2d). Budget ~10 s at default `--live-n` |
| `--live-n N` | Number of live-probe samples when `--live` fires (default: 25) |

Renders five sections: distance histogram, top-5 cross-projections, orphan chunks (>30d with no incoming links), top-10 cross-collection hub topics this collection contributes to, and `chash_index` coverage ratio + sample unindexed chunk IDs.

**`health` flags:**

| Flag | Description |
|------|-------------|
| `--sort COLUMN` | Sort the table by a named column (`name`, `chunk_count`, `last_indexed`, `zero_hit_rate_30d`, `median_query_distance_30d`, `cross_projection_rank`, `orphan_catalog_rows`, `hub_domination_score`). Default: `name` |
| `--format {table,json}` | Output format (default: `table`). `--format=json` returns `{generated_at, collections: [...]}` for dashboards and CI gates |

Chunk counts come from T3's live `coll.count()` (same source as `nx collection list`) so the two surfaces cannot disagree — catalog-sourced counts were historically drifting to 0 on tenants that predated the catalog's `chunk_count` column (fixed 4.9.0, nexus-39zi).

**`delete` flags:**

| Flag | Description |
|------|-------------|
| `-y` / `--yes` / `--confirm` | Skip interactive confirmation prompt |

Delete cascade covers the T3 collection, T2 `chash_index` rows, T2 taxonomy assignments + topics, pipeline-buffer rows (4.8.0, nexus-8a8e), and catalog documents + links.

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

## nx doc

Author, validate, and cite documents backed by the Nexus content-addressed chunk surface
(RDR-082 / RDR-083 / RDR-086).

### nx doc render

Render markdown tokens (`{{bd:…}}`, `{{rdr:…}}`, `{{nx-anchor:…}}`) into a
`<stem>.rendered.md` sibling. With `--expand-citations` (RDR-086 Phase 4),
also resolves every `[display](chash:<hex>)` span and appends a `## Citations`
footnote block containing the chunk text (truncated at 500 chars). Unresolvable
chash values render as `[unresolved chash: <first8>…]` rather than crashing.

```
nx doc render docs/paper.md
nx doc render docs/paper.md --expand-citations
nx doc render docs/paper.md --allow-unresolved        # preserve unresolved tokens verbatim
nx doc render docs/paper.md --out-dir build/          # write to a specific directory
```

### nx doc validate

Parse-and-resolve without emission. Exits non-zero on any unresolved token.

```
nx doc validate docs/paper.md
```

### nx doc check-grounding

Report citation-coverage per markdown file — chash / prose / bracket counts
and the chash-coverage ratio. With `--fail-ungrounded` (RDR-086 Phase 4),
additionally exits 1 when any `chash:` span fails `Catalog.resolve_chash`
and prints `file:line: unresolved chash:<first8>…` to stderr.

```
nx doc check-grounding docs/paper.md
nx doc check-grounding docs/paper.md --fail-ungrounded
nx doc check-grounding docs/paper.md --fail-under 0.80   # coverage-ratio gate
nx doc check-grounding docs/paper.md --format json
```

| Flag | Description |
| --- | --- |
| `--fail-ungrounded` | Exit 1 when any `chash:` citation fails to resolve |
| `--fail-under N` | Exit 1 when chash-coverage ratio falls below `N` (0.0–1.0) |
| `--format table|json` | Report format; default `table` |

### nx doc check-extensions

Flag doc chunks that don't project into a primary source collection at the
given similarity threshold. RDR-086 Phase 4 caller-side fix: the chash spans
in your markdown are resolved to Chroma-scoped `doc_id`s *before* calling
the taxonomy's `chunk_grounded_in`, so you get real candidates instead of
the RDR-083 v1 "all inputs returned no_data" warning.

```
nx doc check-extensions docs/paper.md --primary-source docs__art-grossberg-papers
nx doc check-extensions docs/paper.md --primary-source docs__foo --threshold 0.85
```

### nx doc cite

One-shot authoring command: given a claim string, search the target collection,
resolve the top chunk's hash via `Catalog.resolve_chash`, and emit a paste-ready
`[excerpt](chash:<hex>)` markdown link. With `--json`, returns the full
`{candidates, query, threshold_met}` envelope.

```
nx doc cite "orange foxes navigate Voronoi fields" --against docs__art-grossberg-papers
nx doc cite "chromatic analysis" --against docs__art-grossberg-papers --json
nx doc cite "claim" --against knowledge__corpus --limit 10 --min-similarity 0.25
```

| Flag | Description |
| --- | --- |
| `--against <collection>` | Required. Collection to search for a grounding chunk |
| `--limit N` | Candidate fan-out (default 5); tied candidates within 0.01 surface in `--json` |
| `--min-similarity F` | Maximum acceptable distance (lower is stricter); default 0.30 |
| `--json` | Emit full candidate schema instead of a markdown link |

Exit codes:
- `0` — cite emitted; in JSON mode, `threshold_met=true`
- `1` — top distance above `--min-similarity`; stderr warning, stdout empty (markdown); JSON still returns candidates
- `2` — empty `chash_index` (run `nx collection backfill-hash --all`), empty collection, or unknown collection

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

```
nx doctor --check-schema          # Validate T2 database schema and report pending migrations
```

```
nx doctor --check-plan-library    # Report plan-library dimensional health (RDR-092 Phase 0c)
```

The `--check-plan-library` flag (introduced 4.9.13, nexus-4x9q) buckets
every row in the `plans` table into **authored** (dimensions populated,
not `backfill`-tagged), **backfilled** (dimensions populated, tagged
`backfill` or `backfill-low-conf` by the Phase 0d migration), and
**non-dimensional** (`dimensions IS NULL`, legacy pre-RDR-078 seeds).
Also reports the global-tier builtin count. Exits 1 when that count
falls below 9 (the RDR-078 builtin floor, which signals that
`nx catalog setup` was never run against the current plugin install).
Non-dimensional rows surface a `nx plan repair` hint pointing to the
day-2 command that drains them.

```
nx doctor --trim-telemetry              # Delete search_telemetry rows older than 30 days (RDR-087)
nx doctor --trim-telemetry --days 7     # Aggressive retention (minimum 1 day)
```

The `--trim-telemetry` flag caps `search_telemetry` disk use. The table accrues one row per (query, collection) pair on every `nx search` and MCP search call when `telemetry.search_enabled` is true. Run periodically from cron or a CI job; the default 30-day window keeps an analytical signal long enough to detect slow-burn silent-threshold-drop patterns.

```
nx doctor --check-quotas            # Report ChromaDB Cloud + Voyage AI free-tier caps + retry headroom
nx doctor --check-quotas --json     # Structured output for dashboards / CI gates
```

The `--check-quotas` flag (introduced 4.9.0, nexus-c590) emits a three-section pre-flight report: (1) ChromaDB Cloud limits drawn from `nexus.db.chroma_quotas.QUOTAS` (`MAX_QUERY_RESULTS`, `MAX_RECORDS_PER_WRITE`, `MAX_CONCURRENT_*`, document size caps) plus a live reachability probe of the configured tenant; (2) Voyage AI per-model token and dimension caps (`voyage-3`, `voyage-code-3`, `voyage-context-3`) with `VOYAGE_API_KEY` presence check; (3) the cumulative retry accumulator from `nexus.retry.get_retry_stats()` so any transient-error backoffs observed in the current process surface alongside the static limits.

Exit codes:
- `0` — reachable cloud tenant or local-mode (limits are reference-only).
- `1` — cloud tenant unreachable in cloud mode; the report is not actionable without a working client. Suitable as a CI gate.

```
nx doctor --check-post-store-hooks   # Enumerate registered post-store hook chains
```

The `--check-post-store-hooks` flag (introduced 4.18.0, `nexus-b0ka`) prints every hook the MCP runtime has registered against the document-grain and batch-grain post-store chains, in fire order. Surfaces the side-effect surface that a `store_put` triggers (taxonomy assignment, aspect extraction queueing, link generation, etc.) without grepping `mcp_infra.py`. Use after a hook-registration change to confirm the chain wires up as intended.

```
nx doctor --check-aspect-queue       # Surface RDR-089 aspect-extraction worker depth
```

The `--check-aspect-queue` flag (introduced 4.18.0, `nexus-1pfq`) reports the `aspect_extraction_queue` row count plus per-status breakdown (`pending`, `processing`, `failed`, `completed`), the oldest non-completed `enqueued_at` as a lag indicator, and the top failed rows with their `last_error`. The same data surfaces in the `nx console` Aspect Queue card on `/health` for live monitoring. Pre-RDR-089 databases (no queue table) report cleanly as "table not present" rather than erroring.

---

## nx plan

Plan library maintenance commands (RDR-092 Phase 0d).

```
nx plan repair                   # Backfill dimensions on legacy rows + list low-conf entries
```

The `repair` subcommand (introduced 4.9.12, nexus-1kvj) re-runs the
RDR-092 Phase 0d.1 plan-dimension backfill heuristic against the live
T2 DB. On every run it:

- backfills `verb` / `name` / `dimensions` on any row where
  `dimensions IS NULL`, using a 20-rule verb-from-stem dictionary
  over the `query` column;
- falls back to a wh-question heuristic (`how` / `what` → research;
  `why` → review) for rows that miss every stem rule;
- tags confident matches with `backfill` and low-confidence
  wh-fallback rows with `backfill-low-conf`;
- prints the backfill count, then lists each `backfill-low-conf`
  row with its id, inferred verb, and original query text so an
  operator can correct edge cases by hand (direct SQL, or a future
  editor command).

Idempotent: a second run reports `0 backfilled` and exits cleanly.
When the T2 DB is absent, exits 0 with "nothing to do" rather than a
traceback.

```
nx plan disable PLAN_ID    # Soft-disable a plan without deleting it
nx plan enable PLAN_ID     # Re-enable a previously disabled plan
```

Introduced 4.18.0 (`nexus-mrzp`). `disable` flips `outcome=disabled` on the plan row so it drops out of `plan_match` results without losing its row id, telemetry counters, or T1 cache embedding. `enable` flips it back to `outcome=success`. Useful for triaging a plan whose match-text is misrouting traffic without committing to a delete + re-seed cycle. The pair operates on plan ids returned from `nx plan repair` or `plan_inspect_default`.

---

## nx upgrade

Run pending database migrations and T3 upgrade steps.

```
nx upgrade                        # Apply all pending T2 + T3 migrations
nx upgrade --dry-run              # List pending migrations without executing
nx upgrade --force                # Reset version gate and re-run all migrations
nx upgrade --auto                 # Quiet mode for hook invocation (T2 only, exit 0 always)
```

| Flag | Description |
|------|-------------|
| `--dry-run` | List pending migrations without executing (creates base tables if absent) |
| `--force` | Reset version gate to 0.0.0 and re-run all migrations. Per-migration idempotency guards still apply |
| `--auto` | Quiet mode for SessionStart hook. T2 migrations only (T3 skipped — may exceed hook timeout). Exit 0 always |

**How it works**: The CLI version (`importlib.metadata.version("conexus")`) is compared against the last-seen version stored in T2 (`_nexus_version` table). Migrations tagged with versions between last-seen and current are executed. Each migration is idempotent via `PRAGMA table_info()` / `sqlite_master` guards.

**Auto-upgrade**: `nx upgrade --auto` runs as the first SessionStart hook in the Claude Code plugin. T2 migrations apply silently on every session start. T3 upgrade steps (e.g., cross-collection projection backfill) run only via explicit `nx upgrade`.

**Adding new migrations**: Append a `Migration("x.y.z", "description", fn)` entry to the `MIGRATIONS` list in `src/nexus/db/migrations.py`. For T3 operations, use `T3UpgradeStep`.

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
