# Nexus CLI Reference

Every `nx` command and flag. This is the **command reference** — exhaustive but skim-friendly. For **which retrieval interface to reach for** (`nx search` vs the MCP `search()` / `query()` / `nx_answer` tools), see [Querying Guide](querying-guide.md). For the MCP tool catalog, see [MCP Servers](mcp-servers.md).

Global flags: `--help`, `--version`, `-v`/`--verbose` (enable debug logging).

---

## nx search

Semantic search across T3 knowledge collections. For how `nx search` relates to the MCP search interfaces and the search-quality mechanics (topic boost, distance thresholds, contradiction flags), see [Querying Guide § Search quality features](querying-guide.md#search-quality-features).

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
| `--corpus [docs\|knowledge]` | Corpus routing for auto-classified prose/PDF files (default: `docs`). `docs` routes to `docs__` collections; `knowledge` routes to `knowledge__` collections instead |
| `--on-locked {skip,wait}` | Behavior under contention (default: `wait`). Per-repo advisory lock (two `nx index repo` on the same repo): `skip` exits immediately, `wait` blocks. Catalog-write fairness (RDR-146): when a foreground interactive catalog write is pending, `skip` defers this run's catalog writes to the next idempotent pass, `wait` proceeds after a bounded yield. `NX_WRITE_PRIORITY=interactive|batch` overrides the tty-based priority of a run's catalog writes. |

Per-file indexing runs with bounded concurrency (6.3.1, nexus-cfc72): 2 workers by default when both the vectors and catalog backends are the HTTP service, 1 otherwise. `NX_INDEX_CONCURRENCY=N` overrides (a warning is logged when it forces concurrency past the backend gate). Progress callbacks and post-store hook chains are serialized; `--debug-timing` gains a `hooks_s` bucket so hook-serialization wait is visible separately from upload time.
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
| `--on-formula-oom [fail\|docling]` | What to do when a single page reproducibly OOM-kills MinerU's formula model (default: `fail`). `fail` aborts the document (preserves the no-silent-fallback-for-formulas guarantee). `docling` degrades only that page to docling (formula-stripped) and continues |
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

**MinerU is included by default** since nexus-2fyb. Previously gated behind a `[mineru]` extra; the extras gate produced silent formula loss because fresh installs never picked it up. First use of `auto` or `mineru` modes downloads the unimernet model (~2-3 GB). If MinerU is missing at runtime, your install is corrupt — reinstall with `uv tool install --reinstall conexus`.

**Setting a default backend (sticky config):**

```bash
nx config set pdf.extractor=mineru    # global, applies to all repos
```

Or add to `.nexus.yml` (per-repo) or `~/.config/nexus/config.yml` (global) directly:

```yaml
pdf:
  extractor: mineru   # auto | docling | mineru
  mineru_page_batch: 1          # pages per MinerU subprocess (memory isolation)
  mineru_page_timeout_s: 180    # per-page wall-clock budget (× pages-in-range)
  mineru_memory_ceiling_mb: 0   # 0 = disabled; Linux-only RLIMIT_AS cap (see below)
```

The `--extractor` flag overrides the config when passed explicitly.

**MinerU OOM resilience (RDR-148).** Formula-dense pages can OOM-kill MinerU's
formula model. The recovery ladder: a failed multi-page batch bisects toward
single pages; a single page that still OOMs either aborts the document
(`--on-formula-oom fail`, the default) or degrades only that page to docling
(`--on-formula-oom docling`). On **Linux** you can additionally set
`mineru_memory_ceiling_mb` to cap the worker's address space (RLIMIT_AS) so a
runaway page fails fast and catchably instead of thrashing; macOS does not honour
RLIMIT_AS (the knob logs a warning and is ignored there). Note RLIMIT_AS caps
**virtual** address space, not physical RAM; PyTorch/MinerU mmap weights
aggressively, so set it generously (several GB).

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
| `--collection <name>` | T3 collection override. Wins over the extension-based default (e.g. `--collection knowledge__delos`) |
| `--corpus <name>` | Corpus name used to derive the default collection (default: `dt`). PDFs route to `knowledge__<corpus>-papers` (paper-shaped, aspect-eligible); markdown notes route to `docs__<corpus>` |
| `--dry-run` | Print records that would be indexed; make no T3 writes |
| `--extractor [auto\|docling\|mineru]` | PDF extraction backend for file-backed records (default `auto`). `mineru` is formula-aware but can OOM-fail on formula-dense pages; the recovery is `--extractor docling` (formula-stripped, always completes) |
| `--link-semantic` | After a record indexes, create `relates` edges to its DT similarity + explicit-link neighbours already indexed in nexus (RDR-139 Layer B). DT unavailable → zero edges. Opt-in |
| `--writeback` | After a record indexes, stamp the nexus identity back onto the DT record (RDR-139 Layer F): `nx-indexed` / `nx-tumbler:<t>` tags + a tumbler backlink annotation. nexus-owned namespace only; never edits user content. Opt-in |
| `--enrich` | After indexing, run a DT-CrossRef bibliographic gap-fill over each touched collection (RDR-139 Layer C): the `auto` primary backend, then DT's CrossRef resolver fills only still-empty `bib_*` fields (lowest precedence, never overwrites S2/OpenAlex). Opt-in |
| `--dt-content` | Index non-file-backed records (web archives, bookmarks, formatted notes) from DT's AI-extracted text instead of skipping them (RDR-139 Layer D). Every such chunk is stamped `extraction_source=dt_content`; file-backed records still index from their file. DT unavailable → records skipped as before. Opt-in |
| `--highlights` | After a record indexes, ingest its DT highlights + mentions as a markdown note attached to the record's tumbler in the `document_highlights` T2 table (RDR-139 Layer E). Read back with `nx dt highlights`. Opt-in |

**RDR-139 layered ingest.** The opt-in flags above compose: a single
`nx dt index --selection --link-semantic --writeback --enrich --highlights`
indexes the selection, links it into the graph, gap-fills bibliographic
metadata, stamps the nexus identity back onto each DT record, and ingests its
highlights. Each flag degrades cleanly when DEVONthink is absent (zero edges /
no write-back / primary-backend-only enrich / no highlight ingest); the index
itself always succeeds. See [`docs/rdr/rdr-139-devonthink-mcp-semantic-linking-sync.md`](rdr/rdr-139-devonthink-mcp-semantic-linking-sync.md).

**Default routing by extension** (nexus-cvaw): `nx dt index --uuid X` without `--collection` picks the home based on file type. PDFs land in `knowledge__<corpus>-papers` so `nx enrich aspects` can extract structured fields via `scholarly-paper-v1`. Markdown notes land in `docs__<corpus>` (no aspect extraction; `docs__` is reserved for non-paper prose per nexus-z70w). Pre-nexus-cvaw both extensions defaulted to `docs__default`, which stranded paper PDFs.

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

### nx dt capture

Capture a URL, DOI, or file into DEVONthink and index it end to end, in one
verb (RDR-139 Layer G). Provide exactly one source: a URL argument, `--doi`,
or `--file`. The captured record is then indexed (and optionally linked,
written-back, highlight-ingested, enriched).

This is the one DT-bound verb: unlike `nx dt index` (which degrades silently
when DEVONthink is absent), `nx dt capture` reports DT-required and exits
non-zero, because capture is impossible without DEVONthink.

```bash
# Capture a web page (default: web archive) and index it.
nx dt capture https://example.com/article

# Capture as a PDF and run the full incorporation chain.
nx dt capture https://example.com/paper --type pdf --link-semantic --writeback

# Download a DOI's open-access PDF (Unpaywall) and index it.
nx dt capture --doi 10.1038/nature12373 --contact-email you@example.com

# Import a loose file from disk.
nx dt capture --file ~/Downloads/notes.pdf
```

| Flag | Description |
| --- | --- |
| `<URL>` | Capture a web page via `capture_web_page` |
| `--doi <doi>` | Capture by DOI: download the open-access PDF (Unpaywall) |
| `--file <path>` | Import a loose file from this POSIX path |
| `--type [html\|webarchive\|markdown\|pdf]` | Web-capture format (default `webarchive`). `pdf` and `markdown` index from the on-disk file DT creates; `html` and `webarchive` are non-file-backed |
| `--contact-email <addr>` | Caller email for Unpaywall PDF discovery on `--doi` (else `$OPENALEX_MAILTO`) |
| `--collection` / `--corpus` | Index-step collection / corpus (as `nx dt index`) |
| `--link-semantic` / `--writeback` / `--highlights` / `--enrich` / `--extractor` | Forwarded to the index step |

Exit codes:

- `0`: captured and indexed (or the index step surfaced a per-record failure with exit 0).
- non-zero: DEVONthink not running (DT-required), no capture source / more than one, or capture produced no record.

### nx dt highlights

Show the DEVONthink highlights + mentions ingested for a record (RDR-139
Layer E). Accepts a tumbler or a DT UUID. This is a pure T2 read of the
`document_highlights` table populated by `nx dt index --highlights`;
DEVONthink need not be running.

```bash
nx dt highlights 1.14.4
nx dt highlights 886082AB-87B6-4AE6-AAF6-2E80891014B6
```

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
recipe in [`docs/integrations/devonthink-smart-rules.md`](integrations/devonthink-smart-rules.md).

### Cross-references

- In-DT scripts (toolbar / menu):
  [`docs/integrations/devonthink-scripts.md`](integrations/devonthink-scripts.md).
- Smart rule + folder action recipes:
  [`docs/integrations/devonthink-smart-rules.md`](integrations/devonthink-smart-rules.md).
- Manual smoke runbook + fixture creation:
  [`tests/e2e/devonthink-manual.md`](../tests/e2e/devonthink-manual.md).
- Design rationale + acceptance criteria:
  [RDR-099](rdr/rdr-099-devonthink-integration.md).

---

## nx enrich

Subcommand group. The previous single-shape `nx enrich <coll>` is now `nx enrich bib <coll>`; a new `nx enrich aspects <coll>` ships RDR-089's structured-aspect extraction.

### nx enrich bib

Backfill bibliographic metadata for an existing T3 collection. Two backends are supported: Semantic Scholar (default) and OpenAlex (`--source openalex`).

```
nx enrich bib knowledge__papers --delay 0.5 --limit 50
nx enrich bib knowledge__papers --source openalex --delay 0.5
```

For each unique `source_title` in the collection: extracts DOI / arXiv ID from chunk body text, tries the direct identifier lookup first, falls back to fuzzy title search on miss. Writes `bib_year`, `bib_venue`, `bib_authors`, `bib_citation_count`, plus the source-specific identifier (`bib_semantic_scholar_id` and / or `bib_openalex_id`) back to every chunk with that title. Already-enriched chunks (non-empty backend ID) are skipped, so the command is idempotent.

| Flag | Description |
|------|-------------|
| `COLLECTION` (positional) | Fully-qualified T3 collection name (e.g. `knowledge__papers`) |
| `--source {semantic-scholar\|openalex}` | Bibliographic backend (default: `semantic-scholar`) |
| `--delay SECONDS` | Delay between API calls (default: 0.5s). Increase to avoid rate limiting |
| `--limit N` | Maximum number of titles to enrich (default: 0 = unlimited) |

**Note**: Semantic Scholar's public API allows 100 requests per 5 minutes without an API key. OpenAlex is unauthenticated but encourages including a contact email via `pyalex.config.email`. For large collections, increase `--delay` or use `--limit` to process in batches. DOI extraction prefers labeled DOIs (`DOI: 10.x/y`) over bare DOI strings to avoid contamination from cited references.

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

### nx enrich aspects-show

Display the aspect record for a single document.

```
nx enrich aspects-show 1.653.83
nx enrich aspects-show "CacheRAG"
nx enrich aspects-show 1.653.83 --json
nx enrich aspects-show 1.653.83 --field experimental_datasets
```

Resolves the tumbler (or document title) via the catalog, looks up the aspect row by `(physical_collection, file_path)`, and renders all fields: `problem_formulation`, `proposed_method`, `experimental_datasets`, `experimental_baselines`, `experimental_results`, `extras`, `confidence`, plus extractor metadata (extractor name, model version, extracted-at timestamp). Pre-this verb, inspecting aspects required raw SQL against `~/.config/nexus/memory.db`.

| Flag | Description |
|------|-------------|
| `TUMBLER_OR_TITLE` (positional) | Catalog tumbler (`1.653.83`) or document title (case-insensitive substring match) |
| `--json` | Emit JSON instead of human-readable form |
| `--field NAME` | Project a single aspect field (`problem_formulation`, `proposed_method`, `experimental_datasets`, `experimental_baselines`, `experimental_results`, `extras`, `confidence`). Output is the raw value |

### nx enrich aspects-list

List aspect records for a collection, or the gaps with `--missing`.

```
nx enrich aspects-list --collection knowledge__delos
nx enrich aspects-list --collection knowledge__delos --missing
nx enrich aspects-list --collection knowledge__delos --json --limit 0
```

Companion to `aspects-show` at the collection level (preview / audit shape) instead of single-record detail. With `--missing` the verb inverts to gap detection: catalog rows in the collection that do not have a matching aspect row.

| Flag | Description |
|------|-------------|
| `--collection NAME` (required) | T3 collection to inspect (e.g. `knowledge__delos`) |
| `--limit N` | Maximum rows to display (default: 20; use 0 for unlimited) |
| `--missing` | Flip output: list catalog rows with NO aspect record (gap detection after partial enrichment) |
| `--json` | Emit JSON array instead of human-readable form |

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

## nx aspects

Aspect-extraction queue management (the async queue feeding the aspect-extraction worker). The group also provides `drain` (drain before a PK migration), `gc`, and `gc-fixtures`.

### nx aspects requeue-failed

Bulk re-enqueue terminal-`failed` aspect-queue rows. A row reaches `failed` after exhausting the backoff-retry ladder (RDR-163) or on a non-retryable error. Once the root cause is fixed (e.g. restored API quota, repaired source identity), this verb re-enqueues every failed row at its `(collection, source_path)` key — resetting it to `pending` with `retry_count=0` and clearing any stale backoff — so the worker picks it up again. The write is daemon-routed; the failed-backlog visibility counterpart is `nx doctor --check-aspect-queue`.

| Flag | Description |
|------|-------------|
| `--collection NAME` | Only re-enqueue failed rows in this collection. Default: all collections |
| `--limit N` | Re-enqueue at most N rows (oldest-`enqueued_at` first). Paces recovery of a large backlog so a burst of newly-pending rows doesn't immediately re-hammer a just-restored API quota |
| `--dry-run` | Report the rows that would be re-enqueued without writing anything |

Rows are processed oldest-`enqueued_at`-first (enqueue order, not most-recently-failed); re-enqueue resets `retry_count` to 0. This is a single-operator recovery verb — safe to re-run (it only touches the terminal `failed` state), but do not run two instances concurrently.

```bash
nx aspects requeue-failed                        # re-enqueue all failed rows
nx aspects requeue-failed --collection knowledge__x
nx aspects requeue-failed --limit 100            # pace a large backlog
nx aspects requeue-failed --dry-run              # report only, no writes
```

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

### nx catalog reconcile

```
nx catalog reconcile [--dry-run]
```

Repairs `document_chunks` manifest gaps left by a persistently-failed manifest-write hook (e.g. the catalog engine-service was briefly unreachable during indexing). A gap is a document with `chunk_count > 0` but fewer manifest rows than that (including zero) — such a document silently drops out of catalog-aware retrieval even though T3 still has its chunks.

For each gapped document, rebuilds its manifest from the T3 chunks in its `physical_collection`, matched by the whole-file `content_hash` recorded on both the document and every one of its chunks, ordered by character/line span. Documents with no `content_hash` recorded, or no matching T3 chunks, are reported as unmatched rather than silently skipped. `--dry-run` reports the same counts without writing.

Also see the end-of-run summary on `nx index repo`: a persistent manifest-write failure during indexing is now surfaced there (`WARNING: catalog manifest write failed for N document(s)`) with a pointer to this command.

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
nx catalog audit-membership --all-collections
nx catalog audit-membership --all-collections --json
```

Detect cross-project source_uri contamination in a single physical_collection. Catalog entries are grouped by their `source_uri` "home" (the first four path segments for `file://` URIs, `<scheme>://<netloc>` otherwise); per-home counts surface multi-root collections that look correct in `nx catalog list` but break aspect extraction (the chunks live under one project's identity, every other-project entry skips with `reason=empty`).

`--all-collections` runs the audit across every physical_collection in the catalog and emits one sorted summary (contaminated first). Use it as a daily or post-release health check to confirm the register-time guard (see [Catalog](catalog.md#cross-project-source_uri-guard-nexus-3e4s)) is preventing new contamination. The sweep is read-only. `--purge-non-canonical` and `--canonical-home` are per-collection contexts and raise a usage error when combined with `--all-collections`.

The sweep is owner-aware: when a collection is owned by exactly one `repo` owner with a known `repo_root`, the dominant source_uri home is cross-checked against that root. A single-home collection whose home does not match the owner's tree is flagged as 100% contaminated with a `[wrong-home]` tag (text mode) and `wrong_home: true` field (JSON mode). Without the owner check, single-home wrong-home collections appear "clean" by majority vote, which was the failure mode that masked ~4,200 wrong-home rows in `code__ART-...` pre-fix.

| Flag | Description |
|------|-------------|
| `COLLECTION` (positional) | Physical collection to audit (e.g. `rdr__ART-8c2e74c0`). Required unless `--all-collections` is set |
| `--all-collections` | Sweep every physical_collection in the catalog and emit a summary report. Read-only |
| `--purge-non-canonical` | Delete entries whose home does not match the canonical one. Use with `--dry-run` first. Per-collection only |
| `--canonical-home SUBSTR` | Override the dominant-home heuristic. Required when the contaminating entries outnumber the legitimate ones (e.g. `--canonical-home '/git/ART'`). Per-collection only |
| `--dry-run` | With `--purge-non-canonical`, preview without writing |
| `--yes` / `-y` | Skip the purge confirmation prompt |
| `--json` | Emit per-home counts as JSON. Works in both single-collection and `--all-collections` modes |

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

### nx catalog backfill-collections

```
nx catalog backfill-collections [--dry-run]
```

Populate the RDR-101 Phase 6 collections projection from existing state. Walks both the live T3 vector store and the catalog `documents.physical_collection` column, unions the two sets, and registers each name not already in the projection. The projector's `is_conformant_collection_name` regex decides each row's `legacy_grandfathered` flag automatically.

Idempotent. Conventional first invocation is `--dry-run` for operator review, then `--no-dry-run` to apply.

### nx catalog rename-collection

```
nx catalog rename-collection OLD NEW [--dry-run/--no-dry-run] [--yes] [--allow-legacy]
```

Combined verb that does both the data-plane rename (T3 native `modify(name=)` + T2 cascade + catalog documents re-point) and the RDR-101 Phase 6 control-plane work (collections-projection update + `CollectionSuperseded` event emission). `nx collection rename` (data plane only) remains available for operators who want it without the Phase 6 layer.

Validation gates fire BEFORE any side effect:
- new name must be conformant (`<content_type>__<owner_id>__<embedding_model>__v<n>`) or pass `--allow-legacy`
- old name must be in the collections projection
- old name must not already be superseded
- new name must not already exist in T3

Default is report-only; both `--no-dry-run` AND `--yes` are required to actually rename.

### nx catalog migrate-fallback

```
nx catalog migrate-fallback SOURCE [--target-model M] [--target-version v1] [--dry-run/--no-dry-run] [--yes]
```

Walk a fallback collection (`docs__default`, `knowledge__knowledge`, etc.) and propose a per-document target conformant collection. With `--yes`, re-points each document's `physical_collection` in the catalog and auto-registers the target rows in the projection. Fallback collections are deprecated when the migration empties them (single-target case auto-emits `CollectionSuperseded`); never silently nuked, per RDR-101 §"Phase 6".

Target form: `<content_type>__<owner>__<model>__<version>` where content_type comes from the source's prefix, owner comes from each tumbler (`1.5.42` → `1-5`; tumbler dots become hyphens for ChromaDB's name regex), model defaults to the source's Voyage family.

T3 chunks are NOT moved by this verb. Operators repopulate the target via `nx index` on the source files; old chunks become orphans (catalog now points elsewhere) and get swept by `nx t3 gc` on the next cycle.

### nx catalog doctor

```
nx catalog doctor [--replay-equality] [--t3-doc-id-coverage] [--collections-drift] [--json]
```

RDR-101 catalog doctor surface; pass at least one check flag.
- `--replay-equality`: synthesizer + projector round-trip against live SQLite (Phase 1).
- `--t3-doc-id-coverage`: every non-orphan T3 chunk carries a `doc_id` matching the event log (Phase 2).
- `--collections-drift`: every T3 collection and every distinct `documents.physical_collection` has a row in the collections projection (Phase 6 release gate).

Returns non-zero on any check failure. `--json` emits the per-check result for CI consumption.

---

## nx t3

T3 vector-store maintenance commands. As of 6.0 the live T3 store is Postgres 17 + pgvector behind the native nexus-service; these commands operate on that store through the vector client. (`nx t3 reidentify` was the RDR-108 ChromaDB natural-ID migration and is retained for legacy collections.) Distinct from `nx catalog gc`: `nx t3` operates on T3 chunks, the catalog command operates on catalog rows.

### nx t3 prune-stale

```
nx t3 prune-stale [-c COLLECTION] [--no-dry-run --confirm]
```

Sweep T3 chunks whose `source_path` is missing from disk. Default is report-only; both `--no-dry-run` AND `--confirm` are required to delete.

### nx t3 gc

```
nx t3 gc -c COLLECTION [--orphan-window 30d] [--no-dry-run --yes]
```

Garbage-collect orphaned T3 chunks (RDR-101 Phase 6 / nexus-r5eo). A chunk is an orphan when its `doc_id` metadata is no longer in the catalog projection's alive set for the collection AND its `indexed_at` predates the orphan window (default 30 days).

Per RF-101-3, `nx t3 gc` is the SOLE post-Phase-3 emitter of `ChunkOrphaned` events and the SOLE path that physically deletes T3 chunks. The strict per-candidate order is: append `ChunkOrphaned(chunk_id, reason)` to the event log, THEN call `T3Database.delete_by_chunk_ids`. A crash between the two leaves the log consistent with T3 (event present, delete pending), and the next run idempotently retries the delete.

Default is report-only; both `--no-dry-run` AND `--yes` are required to actually delete. Chunks without a `doc_id` (legacy pre-Phase-2 backfill) are undecidable here and skipped; use a maintenance backfill verb to address them, not GC.

`--orphan-window` accepts `s`, `m`, `h`, `d`, `w` suffixes (e.g. `30d`, `12h`, `2w`); a bare integer is rejected so a typo cannot silently mean 30 seconds.

### nx t3 reidentify

```
nx t3 reidentify (-c COLLECTION | --all-collections) [--no-dry-run]
```

Re-upsert T3 chunks under content-derived natural IDs `chunk_text_hash[:32]` (RDR-108 D1 / nexus-jc63). Per collection the verb paginates T3 chunks (300/op), computes the new natural ID for each chunk, re-upserts under the new ID using the existing embedding (no Voyage call), and batch-deletes the old chunk IDs after the get-loop completes. Document-level metadata fields (`doc_id`, `chunk_index`, `chunk_count`) are stripped at re-upsert; the `document_chunks` manifest table is now authoritative for those.

The verb is idempotent: re-running on a fully-migrated collection performs zero writes. It is also crash-resumable: re-invoking after an interrupted run safely sweeps the un-deleted old IDs.

Default is `--dry-run` (report-only). Use `--no-dry-run` to perform the migration.

Carve-outs:
- `taxonomy__*` collections are skipped (centroids use `centroid_hash` from the `topics` table, not `chunk_text_hash`).
- Pre-RDR-053 chunks lacking `chunk_text_hash` raise a structured error; re-index that collection from source before running.

---

## nx taxonomy

Topic taxonomy — HDBSCAN clustering of T3 collection embeddings into topics for navigation, search grouping, and relevance boosting.

Topics are auto-discovered after `nx index repo`, gated on the run actually having indexed files (an all-unchanged re-index skips the discover/kmeans/label/project/L1 pass entirely — a self-heal guard still runs discovery if a target collection has zero topics). Labeling with Claude haiku runs in a DETACHED background process spawned at the end of indexing (nexus-qqc1v) — the CLI exits immediately and labels land minutes later (progress: `~/.config/nexus/logs/deferred_labeling.log`; run `nx taxonomy label` manually if the spawn failed or you don't want to wait). Search results are grouped by topic and boosted when results share a topic cluster.

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
nx taxonomy backfill-source-collection                        # dry-run: backfill legacy source_collection rows
nx taxonomy backfill-source-collection --apply                # commit the backfill (irreversible)
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
| `project SOURCE` | Cross-collection projection: match chunks against other collections' centroids. `--against TARGETS` for explicit targets (default: sibling collections). `--threshold N` (optional; when omitted uses per-corpus defaults: `code__*` 0.70, `knowledge__*` 0.50, `docs__*`/`rdr__*` 0.55 — see [taxonomy-projection-tuning.md](exploration/taxonomy-projection-tuning.md)). `--use-icf` suppresses hub topics via Inverse Collection Frequency weighting (RDR-077). `--persist` to write assignments. `--backfill` to project all collections against each other |
| `hubs` | List generic-pattern hub topics (RDR-077 Phase 5). `--min-collections N` (default 2), `--max-icf F` filter, `--warn-stale` flags hubs whose latest assignment post-dates the newest `last_discover_at` across contributing source collections, `--explain` shows DF / ICF / matched stopword tokens per row. |
| `audit --collection NAME` | Per-collection projection-quality report (RDR-077 Phase 6): total assignments, p10/p50/p90 of raw cosine, count below threshold (re-projection candidates), top receiving topics with ICF, pattern-pollution flags. `--threshold F` overrides the per-corpus default; `--top-n N` caps the receiving-topic list. |
| `backfill-source-collection` | Backfill `topic_assignments.source_collection` for legacy hdbscan/centroid rows (RDR-087 Phase 4.1). Dry-run by default; `--apply` commits the writes (irreversible — review the dry-run output first) |

**Configuration** (in `.nexus.yml`):

```yaml
taxonomy:
  auto_label: true                    # label with Claude haiku after discover (default: true)
  local_exclude_collections: []       # default: ["code__*"] — local embeddings cluster poorly on code
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
| `get DOC_ID` | Retrieve entry by 32-char hex ID (from `nx store list`) |
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
| `--id ID` | Exact 32-char document ID from `nx store list` |
| `--title TITLE` | Exact title metadata match (deletes all matching chunks) |
| `-y` / `--yes` | Skip confirmation prompt |

Note: IDs shown by `nx store list` are 32 hex chars (`sha256(text)[:32]`). `--title` delete is paginated and safe for multi-chunk documents. To delete an entire collection use `nx collection delete`.

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
| `--assume-model MODEL` | Override the export header's declared embedding model. Pre-migration `.nxexp` files can carry a wrong label (GH #1370); use this to supply the true model instead of trusting the header |
| `--skip-existing` | Skip records whose id already exists in the target collection, instead of overwriting. Useful for resuming a partial import |

Non-conformant legacy chunk ids (16-char pre-migration ids that fail the
service backend's `chash` length constraint) are re-hashed to 32-char
content-derived ids automatically; the CLI reports how many were re-hashed.

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

**`put` flags:** `--tags`, `--ttl` (default: `30d`), `--merge` (canonical-fact merge: fold into an existing high-overlap entry instead of creating a duplicate, non-destructive), `--merge-threshold FLOAT` (word-set Jaccard threshold for `--merge`, default: `0.5`)

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
| `rename OLD NEW` | In-place metadata-only rename in the T3 vector store + T2 + catalog cascade (4.8.0, nexus-1ccq). Never re-embeds; same-prefix renames whose embedding-model segment differs are rejected (6.3.1, nexus-tcvpn) |
| `re-embed NAME --to MODEL` | In-place re-embed for non-CCE Voyage models (nexus-bw65). Service mode: same-model only — the computed vectors ride the verbatim passthrough; a cross-model `--to` fails loud (server-side embedding routes by the collection NAME's model segment; cross-model moves are the migration pipeline's job). `--no-dry-run --yes` to apply (6.3.1, nexus-c9xr2/u37lw) |
| `rewrite-metadata [NAME]` | Rewrite/repair chunk metadata in place; `--all` for every collection, `--source-path` to scope to one source, `--dry-run` to report counts only |
| `audit NAME` | Deep-dive per-collection report: distance histogram, top-5 cross-projections, orphan chunks, hub topics, chash coverage (RDR-087 Phase 4) |
| `health` | Composite per-collection health table — chunk counts (T3-sourced), staleness, hub score, chash coverage (RDR-087 Phase 3.4) |
| `merge-candidates` | Pair-wise cross-collection overlap ranking — surfaces collection pairs with high shared-topic similarity as merge/bridge candidates (RDR-087 Phase 4.3) |
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

Reads each chunk's stored text from the T3 store and computes `sha256(text.encode()).hexdigest()`, updating metadata in-place. Embeddings and documents are untouched — no API keys or re-embedding needed. Idempotent: chunks that already have `chunk_text_hash` are skipped. Also runs automatically during `nx catalog setup`.

**RDR-086 Phase 1.3 — T2 `chash_index` reconciliation.** The same per-chunk
pass also populates the T2 `chash_index` table so `nx doc cite` and
`Catalog.resolve_chash` can answer "which collection + doc_id holds this
chunk hash?" in ~50 µs instead of scanning ChromaDB. Reconciles gaps left
by Phase 1.2 dual-write failures and pre-Phase-1 collections indexed before
the dual-write existed. A tqdm progress bar renders in an interactive
terminal (auto-disabled on non-TTY CI logs).

Scale reference: a full `--all` on a 278k-chunk / 136-collection corpus
takes ~25–70 minutes on ChromaDB Cloud. Maintenance-window operation.

**`re-embed` flags:**

| Flag | Description |
|------|-------------|
| `--to MODEL` | Target embedding model (required). CCE models like `voyage-context-3` are not supported (nexus-bw65) |
| `--dry-run` / `--no-dry-run` | Default `--dry-run`; pass `--no-dry-run` to actually write |
| `--yes` | Skip the destructive-action confirmation prompt |

**`rewrite-metadata` flags:**

| Flag | Description |
|------|-------------|
| `--all` | Rewrite metadata in every T3 collection |
| `--source-path PATH` | Only rewrite chunks whose `source_path` equals this value |
| `--dry-run` | Report counts without issuing any writes |

**`merge-candidates` flags:**

| Flag | Description |
|------|-------------|
| `--min-shared N` | Minimum distinct shared topics between two collections to qualify as a candidate (default: 3) |
| `--min-similarity F` | Minimum mean similarity across shared topics (default: 0.5) |
| `--exclude-hubs` | Drop top-N cross-collection hub topics before thresholding (reduces false positives) |
| `--hub-top-n N` | Hub depth used by `--exclude-hubs` (default: 10) |
| `--limit N` | Max number of candidate pairs returned (default: 50) |
| `--format {table,json}` | Output format (default: `table`) |
| `--create-link` | (deferred) Reports a deferred-workflow advisory instead of writing catalog links — use `nx catalog link` manually |

**`rename` flags:**

| Flag | Description |
|------|-------------|
| `--force-prefix-change` | Allow a cross-prefix rename (e.g. `code__foo` → `docs__foo`) OR a same-prefix rename whose embedding-model segment differs (6.3.1, nexus-tcvpn). Rename never re-embeds, so either change leaves the vectors in the OLD model space under a name claiming the new one — use only when you know the vectors already match the target name (cross-model moves belong to `nx migrate` / guided-upgrade, the RDR-162 vector ETL) |

Renames the collection in the T3 vector store via `t3.rename_collection` (a metadata-only update on the pgvector service path — no embedding re-upload, no Voyage cost, no vector egress), and cascades the new name through T2 taxonomy, `chash_index`, and catalog (JSONL + SQLite). Ordering (SIG-8 / nexus-nhyh): the T2 cascade runs FIRST, then the T3 rename, so a partial failure is recoverable: if the T3 rename fails the T2/catalog rows can be re-pointed or the rename re-run; if T2 fails no T3 rename was attempted.

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
| `update-all` | Refresh the nexus stanza in already-managed hooks across **all** catalog-registered repos (brings every repo to the current stanza after an upgrade; unmanaged/uninstalled hooks are left untouched). Also run automatically by `nx upgrade`. |

Hooks run `nx index repo` in the background after each qualifying git operation, appending output to `~/.config/nexus/index.log`. If a hook file already exists, the nexus stanza is appended (sentinel-bounded) without overwriting existing content.

**Hook status values:** `not installed` · `owned` (nexus-created) · `appended` (added to existing hook) · `unmanaged` (no nexus sentinel)

### nx hook routing-stats

```
nx hook routing-stats [--log-path PATH] [--json]
```

Aggregates the per-rule JSONL log written by the RDR-121 routing-hook
framework (`conexus/hooks/scripts/routing/_lib.log_routing_event`). Reports
fire counts, deny / allow / escape outcomes, block-rate, and
escape-rate per rule.

| Flag | Description |
|------|-------------|
| `--log-path PATH` | Read from this path instead of the default |
| `--json` | Emit aggregated stats as JSON instead of a table |

Default log path resolves to `$NX_ROUTING_LOG_PATH`, falling back to
`~/.config/nexus/routing_log.jsonl`. Used at the 30-day soak review
(RDR-121 §Phase 4) to spot false positives (high escape rate), inert
matchers (zero fires), or overly broad blocks (high block rate).

---

## nx init

Guided first-run setup for the local embedder (RDR-144). Distinct from
`nx config init` (the cloud-credentials wizard): `nx init` chooses and
provisions the on-device embedding model for local mode.

```
nx init                       # local: provision + interactively offer autostart (default yes)
nx init --yes                 # accept service-autostart registration, no prompt
nx init --no-autostart        # provision + start a session supervisor only; register no unit
nx init --embedder minilm-384 # pick a specific embedder, no prompt
nx init --service             # DEPRECATED — plain `nx init` now does this by default
```

| Flag | Description |
|------|-------------|
| `--embedder [bge-768\|minilm-384]` | Select the embedder non-interactively (skips the prompt) |
| `--yes` / `-y` | Accept the service-autostart registration non-interactively (local mode). The autostart unit is installed as the **sole** starter; `nx init` waits for it to come up rather than also starting a session supervisor. |
| `--no-autostart` | Do not register the autostart unit; start a session supervisor only (local mode). Takes precedence over `--yes`. |
| `--service` | **DEPRECATED** (RDR-174 P3.1) — plain `nx init` now provisions the local service backend by default; the flag still works (and prints a deprecation notice) but will be removed in a future release. Provisions the local Postgres + pgvector cluster the RDR-152 service backend uses, locks the embedder to bge-768, acquires + verifies the native service binary, fetches the bge-768 ONNX, and starts the service. Idempotent. Acquire the binary + PG bundle first with `nx daemon service install-binary <engine-service-vX.Y.Z>`. |

**Service autostart (RDR-174 P2.4, decide-first):** in local mode `nx init`
decides autostart *before* starting any supervisor. Interactive runs prompt
(default yes); `--yes` accepts, `--no-autostart` declines. A non-interactive run
with neither flag declines — a system unit is **never** written without explicit
consent. On yes the OS unit becomes the single process watchdog; on no (or a
headless host where the unit can't activate) a session supervisor starts instead.

**Local mode** presents the two on-device embedders and records the choice in
`~/.config/nexus/config.yml` under `local.embed_model`:

| Choice | Model | Dim | Notes |
|--------|-------|-----|-------|
| `bge-768` | BAAI/bge-base-en-v1.5 | 768 | Recommended. Materially better local search. One-time ~140 MB model download. |
| `minilm-384` | all-MiniLM-L6-v2 | 384 | Bundled, instant, lower quality. |

When `bge-768` is chosen, `nx init` also:

1. **Adds the `[local]` extra** if missing. For a `uv tool` install it runs an
   extras-preserving reinstall; in a dev/editable checkout it prints the manual
   command instead of touching the tree.
2. **Pre-fetches the model** into the stable cache (`local.fastembed_cache_path`,
   default `~/.local/share/nexus/fastembed_cache`). Offline failures print an
   actionable message and retry on the next local search.
3. **Offers safe migration** of any pre-existing 384-dim collections that would
   otherwise become silently unsearchable under bge-768 (preview →
   double-confirm → reindex-first → delete-after-verify; `code__` and manual-note
   collections are reported, never auto-deleted; mixed file+note collections
   require an explicit note-loss confirmation and are never migrated under `--yes`).

**Cloud mode** is a no-op: embeddings run server-side via Voyage. `nx init`
points you at `nx config init` for credentials.

`nx doctor` reminds you if you are on the default 384-dim embedder, and flags
the degraded case where `local.embed_model` is `BAAI/bge-base-en-v1.5` but the `[local]`
extra is missing (so search silently runs at 384-dim).

---

## nx config

Configuration management.

```
nx config init
```

| Subcommand | Description |
|------------|-------------|
| `init` | Interactive managed-service (cloud) credential wizard — collects `service_url` + `service_token`. Local mode uses `nx init` instead. |
| `list` | Show all config values |
| `get KEY` | Get single value (masked by default) |
| `set KEY VALUE` | Set single value; also accepts `KEY=VALUE` form |

**`get` flags:**

| Flag | Description |
|------|-------------|
| `--show` | Reveal the full value instead of masking |

**Managed-service credentials** (RDR-166 greenfield onboarding):

| Key | Env var | Purpose |
|-----|---------|---------|
| `service_url` | `NX_SERVICE_URL` | Managed endpoint base URL (e.g. `https://api.conexus-nexus.com`) |
| `service_token` | `NX_SERVICE_TOKEN` | Per-tenant bearer token (operator-provisioned) |

Resolution is env first, then `config.yml`, for both. See
[managed-onboarding.md](managed-onboarding.md) for the full greenfield journey.

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

Checks (live T3 first): the nexus-service vector reachability probe (RDR-155: probed unconditionally — a pgvector install with the service down does NOT doctor all-green), the T3 collection census via the pgvector service, the service bge-768 model in local-service mode, and the legacy on-disk Chroma store (reported as awaiting the migration ETL, not as the live backend). Then: Voyage AI key, ripgrep binary, git binary, git hooks status for registered repos, index log last-write time, orphaned PDF checkpoints, orphaned pipeline buffer entries, T2 integrity, T2 daemon singleton (RDR-129: hard error if more than one T2 daemon serves the same `memory.db`), T2 best-effort writes (RDR-129: soft warning with the count of chash dual-writes dropped under WAL contention). The T2 integrity check reports a transient FTS5 write-lock during active indexing as a soft warning, not a hard failure (RDR-129 B4). The ChromaDB Cloud credential lines (`CHROMA_API_KEY` / `CHROMA_TENANT` / `CHROMA_DATABASE`) are still surfaced, but as of 6.0 they describe migration-source / pre-6.0 cloud config — the live T3 health surface is the vector-service probe above and `nx daemon service status`. A fresh local install with no Chroma keys is healthy.

Migration-health checks (RDR-178): the newest `<config>/migration-reports/migration-*.json` is read and doctor fails loud (fatal) when `summary.total_failed > 0` or the recorded verification verdict is `mismatch`/`indeterminate` — with the report path, per-store failure counts, and a re-run suggestion (`nx storage migrate all --verify-fill`). A legacy report written by pre-6.2 tooling (no verification key at all) with zero failures is a non-fatal WARN, not a failure. Once the newest report records a cloud `target.service_url`, a write-divergence check warns (non-fatal) when `memory.db`'s freshest local write postdates the report's `completed_at` — local writes have landed after the cloud cutover and are not in the cloud tier.

```
nx doctor --clean-checkpoints   # Delete orphaned PDF checkpoint files
nx doctor --clean-pipelines     # Delete orphaned pipeline buffer entries
nx doctor --fix                 # Apply HNSW search_ef=256 to local collections
nx doctor --fix-paths           # Migrate absolute file_path entries to relative (catalog + T3)
nx doctor --fix-paths --dry-run # Preview migration without applying
```

**Other check flags:**

| Flag | Description |
|------|-------------|
| `--check-search` | Run probe 3a — the name-resolution canary from `tests/fixtures/name_canaries.py`. Exits 2 when any surface raises an unexpected exception (RDR-087 Phase 3.2) |
| `--check-resources` | Probe POSIX semaphore headroom and report orphan multiprocessing-tracker pressure. Exits 2 with `Errno 28` when the namespace is exhausted (MinerU workers / orphan chroma children / trackers re-parented to init after ungraceful MCP shutdowns) |
| `--check-taxonomy` | Verify the `topic_links` ≡ projection-assignment invariant (GH #252). Exits 1 on drift |
| `--check-tier-discipline` | Audit tier-write activity for the current session: prints the tier-write summary and warns when a substantive session has no write-back (Phase 1B nexus-a52i) |
| `--check-tmpdirs` | List orphan `nx_t1_*` tmpdirs that no session record points at and are older than 24h (RDR-094 Phase 3). Read-only; pair with `--reap-tmpdirs` to actually delete them |
| `--reap-tmpdirs` | With `--check-tmpdirs`, run `sweep_orphan_tmpdirs` and report the count reaped |
| `--check-mcp-logs` | Scan Claude Code's per-server MCP cache for nx-mcp silent-death signatures (`STDIO connection dropped`, `stdio transport error`). macOS only; skips cleanly elsewhere (RDR-094 Phase H, nexus-50u5) |
| `--mcp-log-hours N` | Lookback window in hours for `--check-mcp-logs` (default: 24) |
| `--check-storage-boundary` | RDR-120 P0.A AST-scan for direct `sqlite3.connect` / `chromadb.{PersistentClient,CloudClient,EphemeralClient}` calls outside `src/nexus/db/` (daemon-internal); also allowlists `src/nexus/catalog/`. Per-line override via `# epsilon-allow: <reason>` (reason ≥8 chars) |
| `--fail-on-violation` | With `--check-storage-boundary`, exit 1 if any violation is found (otherwise the lint is informational) |
| `--phase ID` | With `--check-storage-boundary`, the RDR-120 phase identifier used to record the `120-phase-<phase>-catalog-allowlist-count` T2 metric |
| `--check-t1` | Diagnose T1 session-id lease presence + reachability (RDR-149 P4). Exits 1 when a session-id resolves but the lease is missing or unreachable |
| `--check-mineru` | Verify MinerU is importable — surfaces a corrupt install at doctor-time instead of waiting for the first math-heavy PDF index to fail |
| `--json` | Emit machine-parseable JSON (used with `--check-search`, `--check-quotas`) |

The `--fix` flag retroactively applies HNSW `search_ef` tuning to all existing local-mode collections. New collections get this automatically. In cloud mode (SPANN), prints a skip message — SPANN defaults are adequate.

```
nx doctor --check-schema          # Validate T2 database schema and report pending migrations
```

```
nx doctor --check-plan-library    # Report plan-library dimensional health (RDR-092 Phase 0c)
```

```
nx doctor --check-t3-legacy-metadata                        # Survey T3 for legacy doc_id/source_path chunk metadata
nx doctor --check-t3-legacy-metadata --strict-legacy-metadata  # Exit non-zero if any collection still carries it
```

The `--check-t3-legacy-metadata` flag (nexus-1714) surveys local (Chroma)
T3 collections and reports, per collection, whether any chunk still
carries `doc_id` or `source_path` metadata — both retired by RDR-108
Phase 3 in favour of the catalog `document_chunks` manifest. It gates
removal of the legacy tolerance branches in `mcp/core.py`,
`indexer_utils.py`, and `search_engine.py`: while any collection reports
`LEGACY`, those branches must stay. Detection is a single cheap
`get(where=…, limit=1)` presence probe per field per collection. Default
behaviour is warn (exit 0); add `--strict-legacy-metadata` to exit
non-zero when legacy metadata is found (for CI gating). The check is a
local-Chroma concern and reports *not applicable* in service/cloud mode,
where chunks use the RDR-155 pgvector schema.

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

The `--check-quotas` flag (introduced 4.9.0, nexus-c590) emits a three-section pre-flight report: (1) the per-request limits drawn from `nexus.db.chroma_quotas.QUOTAS` (`MAX_QUERY_RESULTS`, `MAX_RECORDS_PER_WRITE`, `MAX_CONCURRENT_*`, document size caps) which the managed-cloud path still honours, plus a reachability probe that fires only when a ChromaDB Cloud migration source is configured; (2) Voyage AI per-model token and dimension caps (`voyage-3`, `voyage-code-3`, `voyage-context-3`) with `VOYAGE_API_KEY` presence check; (3) the cumulative retry accumulator from `nexus.retry.get_retry_stats()` so any transient-error backoffs observed in the current process surface alongside the static limits.

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

## nx daemon

T2 and T3 daemon lifecycle (RDR-120). Since conexus 4.34.0, all
user-facing CLI commands that touch persistent state (`nx memory`,
`nx index`, `nx store`, `nx catalog`, `nx_answer` and the MCP
tools) route through the T2 daemon process so multi-process
consumers — host CLI + Cowork sessions + dev containers + the
nx-mcp server — share one arbitrated SQLite writer instead of
each opening their own connection.

For a brand-new install the recommended setup is the collapsed flow
(RDR-174 — one provisioning command, no separate T2-daemon step):

```
uv tool install conexus                                    # the nx CLI
nx daemon service install-binary <engine-service-vX.Y.Z>   # acquire the signed native service binary + PG bundle
nx init                                                     # provision Postgres+pgvector, fetch bge-768, start the service, offer autostart
```

`nx init` provisions and starts the service backend and offers to register the
OS autostart unit (prompt, default yes; `--yes` accepts, `--no-autostart`
declines — see [nx init](#nx-init)). In the default all-SERVICE config T2
(notes/plans) is served by the same service, so there is **no** separate
`nx daemon t2 install` step. The deprecated `nx init --service` flag still works
but plain `nx init` is the path now. (`nx daemon t2 install --autostart` remains
available as an explicit opt-in for a SQLite T2 backend, e.g.
`NX_STORAGE_BACKEND=sqlite`.)

Upgrade later with `uv tool upgrade conexus` (preserves extras like `[local]`); avoid `uv tool install --force`, which resets the environment and drops them.

T3 (the permanent vector store) serves through the native nexus-service over
Postgres + pgvector in **both** local and cloud mode (`nx daemon service`); the
legacy `nx daemon t3` ChromaDB daemon is a retired serving path.

On the opt-in SQLite T2 backend (`NX_STORAGE_BACKEND=sqlite`), the conexus
plugin's SessionStart hook auto-spawns the SQLite T2 daemon via
`nx daemon t2 ensure-running` on every Claude Code session start (a silent
no-op in the default service config, where T2 is served by the nexus-service).
For a SQLite T2 daemon that survives reboots independent of Claude Code, use
`nx daemon t2 install --autostart`. In the default service config the service's
own autostart unit (`nx daemon service install --autostart`, or accepting the
`nx init` prompt) covers reboot-persistence for every tier.

### nx daemon t2 start

Start the T2 daemon in the foreground. The daemon IS this Python
process (asyncio event loop until SIGTERM/SIGINT). Run under
launchd / systemd via `nx daemon t2 install --autostart` for
production use; direct foreground invocation is for debugging or
explicit one-shot starts.

| Flag | Description |
|------|-------------|
| `--config-dir PATH` | Config directory override (default: `~/.config/nexus/`) |
| `--db-path PATH` | Override the memory.db path (default: `nexus.config.default_db_path()`) |

Fails fast with `T2DaemonError` if another T2 daemon already holds
the spawn lock on the same config_dir.

### nx daemon t2 stop

Send SIGTERM to the running T2 daemon. Reads the PID from the
discovery file at `~/.config/nexus/t2_addr.<uid>` and signals it.
Idempotent — exits cleanly if no daemon is running.

| Flag | Description |
|------|-------------|
| `--config-dir PATH` | Config directory override |

### nx daemon t2 status

Print the T2 daemon discovery JSON: PID, UDS path, TCP host/port,
daemon version, start time.

| Flag | Description |
|------|-------------|
| `--config-dir PATH` | Config directory override |
| `--json` | Output raw JSON (default: pretty-print) |

The recorded PID is probed for liveness (`os.kill(pid, 0)`). A discovery
file whose PID is no longer running is reported as `STALE` (with `--json`,
an `"alive": false` field) and exits 1, so a daemon that died leaving a
stale discovery file is not reported as running. Exit code 1 also when no
discovery file exists.

The output also includes `restarts_in_window` (RDR-140 P4): the number
of cold respawns the crash-loop guard has recorded in the current window
(see `ensure-running` below). A rising count across successive `status`
calls is the crash-loop signal. `0` once the daemon converges.

### nx daemon t2 ensure-running

Idempotent: silent no-op if the T2 daemon is already running on the
named config_dir, otherwise spawns it in the background (detached
subprocess) and polls the discovery file until reachable (or the
timeout expires).

Stale discovery files left behind by crashed daemons trigger a
fresh spawn rather than a false-positive — the probe is
`os.kill(pid, 0)` against the discovery-file PID.

Version-aware (nexus-5ldk1): a live daemon whose `daemon_version`
differs from the installed `conexus` is treated as stale. The command
gracefully cycles it (SIGTERM drains in-flight RPC, then respawns) so
the running daemon matches the installed code. This is why `nx upgrade`,
`scripts/reinstall-tool.sh`, and the plugin / `.mcpb` session-start
hooks all call `ensure-running` after an install: the daemon comes up on
the new version without a manual restart. A daemon already matching the
installed version is left untouched.

Crash-loop guard (RDR-140 P4). Each cold respawn driven by
`ensure-running` is recorded in a sentinel file beside the discovery
file. After `_CRASHLOOP_MAX_RESTARTS` (default 5) respawns within
`_CRASHLOOP_WINDOW_S` (default 300s) without the daemon converging,
`ensure-running` stops respawning, logs once at `error`
(`t2_daemon_crash_loop_suppressed`), and exits 1, instead of an endless
crash-loop with a traceback per attempt. The counter clears when a
spawned daemon becomes reachable, and re-arms for a fresh window once the
prior restarts age out.

Scope: this guard bounds the `ensure-running`-driven respawn path (the
dominant source of churn, since the plugin / `.mcpb` session-start hooks
call `ensure-running` on every MCP-server boot). It does NOT bound the
launchd / systemd path: `install --autostart` runs `nx daemon t2 start`
directly, which never consults the sentinel, so a daemon failing under
`KeepAlive` is rate-limited only by the supervisor's own throttle
(launchd `ThrottleInterval`, systemd `RestartSec`). Known limitation: a
daemon that becomes briefly reachable then dies repeatedly (flapping)
resets the counter each cycle, so the guard fires only for a daemon that
never reaches the discovery-written state within the spawn timeout.
Inspect the daemon log, then re-run `ensure-running` after fixing the
root cause.

| Flag | Description |
|------|-------------|
| `--config-dir PATH` | Config directory override |
| `--timeout SECONDS` | Wait up to N seconds for spawn (default: 5.0) |
| `--quiet` | Suppress "already running" / "spawned" messages; only print errors |

Exit codes:
- `0`: reachable (already running OR successfully spawned)
- `1`: spawned but did not become reachable within `--timeout`

Used by the conexus plugin's SessionStart hook; safe to invoke from any
post-install script that needs the daemon up before running other
commands.

### nx daemon t2 install --autostart

Write a launchd plist (macOS) or systemd user unit (Linux) so the
T2 daemon starts at login / boot and respawns on crash.

| Flag | Description |
|------|-------------|
| `--autostart` | Required. Confirms intent to write an OS autostart entry. |
| `--force` | Overwrite an existing plist / unit file when its content differs from the freshly rendered template. |

The plist / unit file lands in the per-user autostart directory:

- macOS: `~/Library/LaunchAgents/com.nexus.t2.plist` (`KeepAlive=true`, `RunAtLoad=true`)
- Linux: `~/.config/systemd/user/nexus-t2.service` (`Restart=on-failure`, `WantedBy=default.target`)

After write, the command activates the unit:

- macOS: `launchctl bootstrap gui/<uid> ~/Library/LaunchAgents/com.nexus.t2.plist`
- Linux: `systemctl --user enable --now nexus-t2.service`

Logs:
- macOS: `~/Library/Logs/nexus-t2.log` / `nexus-t2.err`
- Linux: `journalctl --user -u nexus-t2.service`

Idempotent — running twice doesn't duplicate the plist or the
service unit. Re-render and re-activate on `conexus` upgrades by
running `nx daemon t2 install --autostart` again (the rendered
template's `__NX_BIN__` resolves to the current `nx` binary path).

### nx daemon t2 uninstall --autostart

Reverse of `install --autostart`: deactivate via
`launchctl bootout` (macOS) / `systemctl --user disable --now`
(Linux) then unlink the plist / unit file.

| Flag | Description |
|------|-------------|
| `--autostart` | Required. |

### nx daemon t3 start / stop / status / install / uninstall

> **Legacy / retired serving path (6.0).** T3 no longer serves from ChromaDB.
> The permanent vector store now serves through the native nexus-service over
> Postgres + pgvector — see `nx daemon service` below and `nx init`.
> `nx daemon t3` (the managed `chroma run` subprocess) remains only for reading a
> pre-6.0 ChromaDB store as the **migration source** (`nx guided-upgrade`).

Same shape as the `t2` subcommands, applies to the legacy T3 ChromaDB daemon.
It wraps the upstream `chroma run` server lifecycle under launchd / systemd
supervision (templates `com.nexus.t3.plist` / `nexus-t3.service`).

### nx daemon service start / stop / status

The storage-service supervisor (RDR-152 P5.1): the managed native
nexus-service binary + nx-managed Postgres. `start` ensures PG is running,
spawns the native binary (resolving `NX_VOYAGE_API_KEY` through the credential
chain), waits for `/health`, and publishes the endpoint lease that clients
auto-discover. The native binary is the sole launch artifact (RDR-161: the
`java -jar` path is expunged); acquire it with `install-binary` (below) or
`nx init`, which places and verifies it.

`status` is the single is-the-stack-healthy surface: the lease (host, port,
service pid, generation), supervisor pid, addr-file path, live `/health` probe,
the PG cluster (port, data dir, up/down, installed pgvector version,
pg_credentials path), the log-file paths (below), and the running service's
`/version` handshake (`app_version`, `embedding_mode` voyage|onnx-local with
the dispatchable models, `schema_latest_id`, `schema_changeset_count`). It
warns when the running binary differs from the installed one.

**Observability.** Every component of the stack writes a persistent log
(none of them is ever DEVNULL'd); when the stack dies, the evidence lives
in (all under `~/.config/nexus/` unless noted):

| File | Contents |
|------|----------|
| `logs/storage_service.log` | Supervisor lifecycle (rotating): start/exit breadcrumbs, service exit codes, restart attempts, PG recoveries, crash backstop. |
| `logs/storage_service_native.log` | The native service's stdout/stderr (banners, fatal errors). Size-rotated at respawn. |
| `logs/storage_service.crash.log` | Pre-startup failures of the detached supervisor (import errors, bad argv) and interpreter-fatal tracebacks. Quiet in healthy operation. |
| `<pg_data>/pg.log` | The nx-managed Postgres cluster log (`pg_ctl`). |

A supervisor death without a `storage_service_supervisor_exit` breadcrumb
in `storage_service.log` means it was killed, not that it chose to exit —
check the service log tail and `pg.log` next.

`stop` stops the supervisor + service but **leaves Postgres running by
design** (it is independently managed and may serve other clients) — the
command says so; pass `--with-pg` to stop the cluster too (`pg_ctl -m
fast`).

| Flag | Description |
|------|-------------|
| `--foreground` | Block until SIGTERM (for launchd/systemd supervision). |
| `--config-dir` | Config directory override. |
| `--json` | (`status`) Raw JSON output. |
| `--with-pg` | (`stop`) Also stop the nx-managed Postgres cluster. |

**Memory-constrained hosts.** Set `NX_SERVICE_MAX_HEAP` (e.g. `NX_SERVICE_MAX_HEAP=1g`)
to cap the native service's JVM heap. On low-RAM laptops and containers the
combined peak (service binary + bge-768 ONNX + Postgres + supervisor) can trip
the OS OOM-killer at first start; capping the heap reduces that risk. Default is
unset (no cap).

**Container reachability (`NX_SERVICE_BIND`, since engine-service v0.1.11).** The
service binds `127.0.0.1` (loopback) by default. Set `NX_SERVICE_BIND=0.0.0.0`
to bind all interfaces so a dev/CI container can reach a host-run service across
its network namespace. **Security:** the service has **no TLS** — a non-loopback
bind exposes a token-authed *plaintext* service (and unauthenticated `/health` /
`/version`) on the LAN; use it only on trusted/host-private container networks,
and never on an untrusted network. **Necessary but not sufficient:** the bind
makes the service *reachable*, but a container still cannot *discover* it — the
published lease host stays loopback by design (it is the host-side connect
address), and the service port is OS-allocated. A container must therefore set
`NX_SERVICE_HOST` / `NX_SERVICE_PORT` / `NX_SERVICE_TOKEN` explicitly (it cannot
read the host's lease file). A fixed/known-port mechanism for the full container
flow is tracked in `nexus-ddvjy`. (This does **not** apply to Claude Cowork,
which uses SDK transport to a host-resident MCP server per RDR-126.)

### nx daemon service install --autostart

```
nx daemon service install --autostart
nx daemon service install --autostart --force
```

Register the storage service to start at login/boot — writes a launchd
LaunchAgent (macOS, `~/Library/LaunchAgents/com.nexus.service.plist`) or a
systemd user unit (Linux, `~/.config/systemd/user/nexus-service.service`) that
execs `nx daemon service start --foreground`. The OS init system is the single
process watchdog (RDR-175), and the in-process respawn layer is retired. The
systemd unit restarts on a non-zero exit (`Restart=on-failure` +
`SuccessExitStatus=143` excludes a graceful SIGTERM stop; `StartLimitIntervalSec=0`
removes the give-up threshold). The launchd plist uses `KeepAlive=true`, which
restarts on any exit (including a clean `nx daemon service stop`) — stop it via
`nx daemon service uninstall --autostart` (or `launchctl bootout`) when you want
it to stay down. `nx init` runs this for you when you
accept the autostart prompt (decide-first — the unit is the sole starter, no
session supervisor underneath it). `--force` overwrites an existing unit whose
content differs. Remove with `nx daemon service uninstall --autostart`.

| Flag | Description |
|------|-------------|
| `--autostart` | Required. Install the OS autostart unit. |
| `--force` | Overwrite an existing unit file even when its content differs. |

### nx daemon service install-binary

```
nx daemon service install-binary <engine-service-vX.Y.Z>
nx daemon service install-binary <engine-service-vX.Y.Z> --no-pg-bundle
```

Download, verify, and install the signed native nexus-service binary (and,
by default, the relocatable PostgreSQL bundle) from a GitHub release to the
well-known location (`~/.config/nexus/service/`) with a provenance sidecar
(version, tag, sha256, install metadata). Supervisor discovery and
`nx init` use this location.

TAG is an EXPLICIT `engine-service-v*` release tag (e.g.
`engine-service-v0.1.3`); there is no "latest" resolution. Each per-platform
asset, its `.sha256`, and its `.sigstore.json` bundle are fetched and verified
(sha256 + keyless Sigstore signature, pinned to the engine-service release
workflow identity), then placed atomically. Verification **fails closed**:
nothing is installed unless BOTH gates pass. One verified seam covers the
binary and the PG bundle (RDR-161). `--no-pg-bundle` installs only the
service binary (e.g. a cloud habitat with a managed Postgres).

### nx daemon aspect-worker start

```
nx daemon aspect-worker start [--config-dir DIR] [--tenant TENANT] [--stale-timeout-seconds N]
```

Start the aspect-worker daemon in the foreground (runs until SIGTERM/SIGINT):
a leased, per-tenant host for the aspect-extraction loop (claim → `claude -p`
→ upsert `document_aspects` → mark done) and the `reclaim_stale` loop — one
more leased tier on the RDR-149 service-registry substrate (RDR-173). Rides
a per-tenant lease, so a second `start` for the same tenant fences the
predecessor (one owner survives).

| Flag | Description |
|------|-------------|
| `--config-dir` | Config directory override (default: `~/.config/nexus/`) |
| `--tenant` | Tenant scope for the lease (default `default`). Per-tenant only — per-host would need `BYPASSRLS`, forbidden by RDR-152 |
| `--stale-timeout-seconds` | Reclaim staleness threshold (default `300`). MUST exceed the `claude -p` extraction budget (180s) or an in-flight row could be false-reclaimed |

**Credential model (RDR-173):** this command MUST be spawned as a CHILD of a
process that already has `claude -p` credentials, so it inherits the
`claude` binary on `PATH`, `~/.claude`, and the Anthropic credential
context — a credential-bare invocation fails extraction. In normal
operation you never run this manually: the `store_put` enqueue hook
spawns it automatically (spawn-if-absent, single-flight) from the storing
process precisely so that inheritance happens.

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

## nx uninstall

First-class agent teardown (RDR-165). See
[docs/operations/agent-lifecycle.md](operations/agent-lifecycle.md) for the full
install → upgrade → uninstall lifecycle map. Cleanly removes nexus, auto-detecting
and handling BOTH install shapes — each branch is a no-op when its target is absent:

- **Local service**: stops the engine-service + Postgres stack
  (`nx daemon service stop --with-pg`), stops the T2 daemon, removes the OS
  autostart unit, and clears the first-run marker.
- **Managed-only client**: clears the managed endpoint config
  (`service_url` + `service_token`) from `config.yml`. Skips service-stop (no
  local service) and never touches the remote tenant's data.

```
nx uninstall                  # DRY RUN (default): preview what would be removed
nx uninstall --yes            # Perform the teardown
nx uninstall --yes --remove-data   # ALSO wipe the local data dir (notes + index)
```

| Flag | Description |
|------|-------------|
| `--yes` | Perform the teardown. Without it, `nx uninstall` only previews (dry-run default). |
| `--remove-data` | Also wipe the local nexus data dir (notes + search index). Irreversible; only acts with `--yes`. **Does NOT touch a managed/remote tenant's data.** |

**Managed env override:** if `NX_SERVICE_URL` / `NX_SERVICE_TOKEN` are exported in
your shell (not just `config.yml`), `nx uninstall` clears `config.yml` and warns
you to `unset` the shell export — it cannot unset the parent shell itself.

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

MinerU server lifecycle management for PDF extraction. MinerU is a default dependency since nexus-2fyb.

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

## nx tenant

Tenant provisioning for the RDR-152 storage service (bead nexus-gmiaf.32.3). Requires `NX_SERVICE_PORT` and `NX_SERVICE_TOKEN` (the bootstrap credential the storage-service supervisor publishes). All SQL runs in the Java service; the CLI is a thin client.

### nx tenant create

```
nx tenant create NAME
```

Create tenant `NAME` and mint its first bound service token. The token is printed **once** (store it immediately); only its hash is kept server-side. The name `*` is reserved for the bootstrap token and is rejected.

## nx service

Storage-service administration.

### nx service probe

```
nx service probe [--url URL]
```

Probe a managed nexus service for reachability and version compatibility. `--url` defaults to `NX_SERVICE_URL` (or the `service_url` credential). Reports the endpoint, `release_version`, `app_version`, and embedding mode; exits non-zero when the service is unreachable.

### nx service record-deploy

```
nx service record-deploy TAG [--commit SHA] [--gate RESULT] [--url URL]
```

Record `TAG` (`engine-service-vX.Y.Z`, `vX.Y.Z`, or `X.Y.Z`) as the cloud-deployed engine in the `deployed-engine-version` T2 tracker — **guarded by a live `/version` read**. GETs the service handshake, asserts `release_version` equals `TAG`'s version, and only then writes the tracker; the recorded version is machine-sourced from the live read, never hand-typed. Fails loud (and writes nothing) if the deploy has not landed or the version disagrees, so a *wrong* version can never be recorded (nexus-dz6b1 / RDR-179). This replaces the old hand-typed `nx memory put` in the engine-release skill's record step. Note the scope boundary: it guards the recorded value, not that the step is run — forcing the write (cloud-gate writes the tracker on pass) is a tracked follow-up; `--commit`/`--gate` are recorded verbatim, not verified.

### nx service token issue

```
nx service token issue --tenant TENANT [--label LABEL] [--ttl SECONDS]
```

Issue a new bearer token bound to `TENANT`. Printed once; only the hash is stored. `--ttl` sets an optional lifetime in seconds (default: no expiry). A token bound to a tenant ignores the client `X-Nexus-Tenant` header; the tenant comes from the token.

### nx service token rotate

```
nx service token rotate --tenant TENANT [--grace SECONDS]
```

Rotate `TENANT`'s tokens with zero downtime: issue a new token and set the previous live tokens to expire after the grace window (`--grace`, service default 300s), so both are valid during the overlap. Running clients pick up the new token by rediscovering the lease the storage-service supervisor publishes; no restart and no 401s during the window.

### nx service token revoke

```
nx service token revoke SELECTOR
```

Revoke a token by full hash or a unique hash prefix. Revocation is immediate on the storage service that handles the request (its auth cache is invalidated in-process). For any other reader, revocation propagates within the AuthFilter token-cache TTL bound (default 30s). Exits non-zero if no unique token matches.

### nx service token list

```
nx service token list [--tenant TENANT]
```

List service tokens: 12-char id prefix, tenant, status (`active`/`expired`/`revoked`), label, expiry, and revocation time. Never prints the raw token. Use the id prefix with `nx service token revoke`.

## nx guided-upgrade

```
nx guided-upgrade [--local-path PATH] [--db PATH] [--catalog-db PATH] [--service-url URL] [--timeout SECS] [--yes] [--force]
```

The **one-command upgrade from a pre-6.0 (ChromaDB) install to the service
stack** (RDR-002). It is the recommended migration entry point — it stands up
the service and then drives `nx migrate-to-service`, so you never hand-sequence
provisioning + migration.

Sequence: **pre-flight detect** (if there is no ChromaDB footprint to migrate it
no-ops without provisioning) → **provision + serve** the local service (the full
`nx init` path: Postgres + bge-768 ONNX + the native binary) — or, with
`--service-url`, gate an already-running service → **version-pin** (`/version`
`/version` must report a `release_version` — present from engine-service
v0.1.6+, code floor v0.1.8; older/below-floor binaries fail closed) → **bounded
health-gate** → **voyage-
capability pre-flight** (if the footprint has voyage collections, the target
service must be able to serve them — fail loud before migrating) → drive
**`nx migrate-to-service`** (detect → ETL → validate → unlock) → advisory
`nx doctor`.

- `--service-url URL` migrates into an already-running service instead of
  provisioning a local one; requires `NX_SERVICE_TOKEN` to be set.
- `--timeout SECS` bounds the wait for the service to become healthy (default 120).
- `--yes` / `-y` skips the confirmation prompt.
- `--force` (RDR-178 Gap 7) skips already-migrated detection and re-migrates
  every T2 store unconditionally.

A not-ready or wrong-version service **hard-fails before any migration**.
Idempotent and safe to re-run. The **T2 (SQLite) side is a true no-op on
re-run** (RDR-178 Gap 7): before migrating, the command consults the newest
`<config>/migration-reports/` artifacts plus a local-SQLite freshness probe
and skips any T2 store already covered by a clean report with no newer local
writes — printing an `already migrated <date>, no newer local writes` line
per skipped store. `--force` bypasses this and re-migrates every T2 store
unconditionally. The **T3 (ChromaDB → pgvector) side is NOT yet a no-op** —
copy-not-move leaves the ChromaDB source intact, so it is always re-detected
and re-verified at full cost (already-migrated detection for the T3 legs is
tracked separately, RDR-178 Wave 2). On a validation block it leaves the
`migrated-failed` sentinel and offers a rollback command (copy-not-move;
never auto-reverts). Operational narrative:
[`docs/migration-runbook.md`](migration-runbook.md).

## nx migrate-to-service

```
nx migrate-to-service [--dry-run] [--local-path PATH] [--db PATH] [--catalog-db PATH] [--service-url URL]
```

The lower-level Chroma-to-service migration primitive (RDR-159) that
`nx guided-upgrade` wraps. Use `nx guided-upgrade` for the full one-command
experience; use this directly when the service is **already provisioned and
running** and you only need the migration step. It sequences the proven
`nx storage migrate` primitives into one survivable command so a user never
hand-sequences the ~8-step gauntlet.

- `--dry-run` ships the read-only front half: it classifies the Chroma
  footprint per collection (source leg × embedding model, resolved against the
  deployment's wired embedders) and previews what would migrate — per-leg /
  per-model counts, a coarse token + time estimate, and unsupported collections
  flagged for re-index. It touches no data, and exits non-zero when any
  unsupported collection is present (a real run would block on it).
- The bare invocation runs the full flow: **detect** → **sequence** (set the
  cross-process migration sentinel, quiesce background indexing, per-collection
  model pre-gate, T2 `migrate all` requiring `total_failed == 0`, then T3
  vectors for every detected leg, refusing partial-leg success) → **validate**
  (taxonomy floor + source==target counts + manifest-orphans, no short-circuit)
  → **unlock** on a clean verdict (clear the sentinel; serve from pgvector).

On any validation block the migrated copy is left in place, reads stay
degraded-LOUD (the `migrated-failed` sentinel stands in for a bare empty index),
and rollback is **offered, never auto-invoked** — the Chroma source is untouched
(copy-not-move), so `nx storage migrate vectors --rollback [--cloud]` returns
the user to a fully-working pre-upgrade state. A fresh user with no Chroma data
is a clean no-op. Requires `NX_SERVICE_TOKEN` and a reachable nexus-service (the
T2 catalog ETL + manifest validation call the service). The operational
narrative lives in [`docs/migration-runbook.md`](migration-runbook.md).

## nx migration

```
nx migration [--clear-state] [--force]
```

Inspect or recover the cross-process migration sentinel (RDR-159). Bare `nx
migration` prints the current phase read-only (`not-migrating`, `migrating`,
`migrated`, or `migrated-failed`) with progress and any failure message.

- `--clear-state` removes a **stranded** sentinel — the named escape hatch for a
  CLI crash between a clean T3 copy and the UNLOCK clear, which would otherwise
  leave every read surface banner-wrapped (`migrating`/`migrated-failed`)
  forever. Clearing is **safe**: a resumed `nx migrate-to-service` recomputes
  done-vs-total from live source-vs-target counts (the ETL is idempotent on
  `(tenant, collection, chash)`), so it never trusts the stale marker. A no-op
  when no sentinel is present.
- `--force` is required to clear a `migrating` sentinel, which may belong to a
  live migration in another process (clearing it drops the read-surface banner
  mid-migration). A `migrated-failed` sentinel clears without `--force` — its
  writer is already dead.

This is distinct from re-running the migration itself: `nx migrate-to-service`
transitions a `migrated-failed` sentinel back to `migrating` (resume); `nx
migration --clear-state` drops it straight to `not-migrating` (abandon /
recover).

## nx storage

> Running a real migration window? The operational narrative (quiescence,
> mid-run failure playbook, cutover validation, rollback) lives in
> [`docs/migration-runbook.md`](migration-runbook.md); this section is the
> flag reference.

### nx storage migrate all

```
nx storage migrate all [--report PATH] [--db PATH] [--catalog-db PATH] [--service-url URL] [--verify-fill]
```

Run ALL eight T2 store migrations in the RDR-152 ladder order (memory →
plans → telemetry → taxonomy → aspects → chash → catalog →
aspects_queue — the last two trail so FK targets exist) with one
shared issue collector, and emit ONE RDR-153 migration report (default:
`~/.config/nexus/migration-reports/migration-<id>.json` — a run always
produces an artifact). Prints a per-store progress line as each store
completes (`<store>: <written> written / <read> read`, RDR-176 Gap 5) so a
long migration doesn't go silent between stores. Exits non-zero when
`summary.total_failed > 0` ("migration is NOT clean") or when post-run
count verification finds Postgres counts below the report's written
totals. When psql/credentials cannot be resolved the verification is
reported as **VERIFICATION INDETERMINATE** — a loud warning, never a
silent skip (the RDR-152 prod-copy.sh harness bug). The verdict is
recorded in the report artifact (`"verification"`). Verification queries
the LOCAL nx-managed Postgres (from `pg_credentials`) — when migrating
against a remote service it reports on the local cluster only.
`--service-url` overrides the nexus-service base URL (config-first chain:
`--service-url` > `NX_SERVICE_URL` env > `config.yml`; the auth token
resolves the same chain, RDR-176 Gap 3). Every per-store command also
accepts `--report PATH` for a single-store report (a default-path
artifact is written even when the flag is omitted, including on a
mid-run crash — partial data beats no data). Note: `aspects` has no
standalone command; it runs only via `migrate all`.

`--verify-fill` (RDR-178 wave-2): a re-run to patch a small hole no
longer re-sends the whole run. Per store:

- `chash` / `catalog` — the outer count-diff decides parity (zero writes)
  vs. divergent (send ONLY the rows genuinely missing from the target,
  per `physical_collection` for chash; owners/collections/document_chunks
  independently for catalog). Catalog's `documents`/`links` tables have
  no delta-fill surface yet — if either diverges, the whole catalog store
  falls back to the full ETL (never a partial/incoherent write).
- `memory` / `plans` / `telemetry` / `taxonomy` — outer-verify only (no
  delta-fill surface yet): a store already at parity is **skipped
  entirely** (folded into `report["skipped_stores"]`, same signal as an
  already-migrated pre-flight); a non-parity store falls back to the
  unchanged full ETL.
- `aspects` / `aspects_queue` — always the full ETL; unaffected by the flag.

The report gains an additive `"verify_fill"` key (`{"outer": {...},
"results": {...}, "total_filled": N}`) — absent on a normal run. A dedup
relation (`nexus.plans`) that parities below its source count due to
`ON CONFLICT DO UPDATE` convergence is surfaced as a
`verification_convergence_notes` entry, never mistaken for a hole.

### nx storage migration-report show

```
nx storage migration-report show <path>
```

Summarize an RDR-153 migration-report artifact: migration id and window,
the recorded verification verdict (`(not recorded)` for artifacts that
predate it), `max_severity` first, the by-action rollup
(severity-descending), per-issue triage lines (severity-descending, with
class/action/count/sample), and the gate verdict — **GATE: PASS** when
`summary.total_failed == 0`, otherwise **GATE: FAIL** with a non-zero
exit (scriptable; this is the RDR-152 Phase-4 SQLite-deletion gate
predicate). The reader lives in `nexus.migration` and survives the
`src/nexus/db/t2` deletion. When the artifact carries the additive
`"verify_fill"` key (a `--verify-fill` run), also prints a
`verify-fill: total_filled=N` line, one `<store>.<table>: filled=N
status=...` line per table that was actually diffed, any convergence
notes, and the list of stores skipped entirely (already at parity).

Storage migration ETLs (RDR-152 T2 stores; RDR-155 vectors). Every ETL is copy-not-move (the source is never modified) and idempotent (server-side upsert; re-runs produce no duplicates). All require `NX_SERVICE_TOKEN`.

### nx storage migrate

```
nx storage migrate memory|plans|telemetry|taxonomy|chash|catalog [--db PATH] [--service-url URL] [--dry-run] [--verify-fill]
```

Migrate a T2 SQLite store into the Postgres service tier through the validated HTTP seam. `--verify-fill` (RDR-178 wave-2) runs the delta path instead of the unconditional full re-send — see `nx storage migrate all` above for the per-store semantics (a single-store invocation applies the same store's rule in isolation). Every per-store command's report always carries a populated `"verification"` verdict now (not just `migrate all`'s), and `target.service_url` records the RESOLVED endpoint, never a `"(lease)"` placeholder.

### nx storage migrate vectors

```
nx storage migrate vectors [--local-path PATH | --cloud] [--collections A,B] [--service-url URL] [--dry-run | --rollback]
```

Migrate Chroma vector collections into pgvector (RDR-155 Phase 5). Two legs, run separately: the default local leg reads the on-disk store the retired T3 daemon served (`--local-path`, default `~/.config/nexus/chroma`); `--cloud` reads via the ChromaCloud REST/auth API using the configured `chroma_*` credentials. Chunk text, chash, and metadata transfer verbatim and the service re-embeds server-side; collection names are preserved verbatim so `topic_assignments.source_collection` references stay valid. `--rollback` deletes from pgvector exactly the chashes present in the source collections, leaving the source untouched. Exits non-zero when any collection failed or was skipped (non-conformant name with data present). Non-conformant collections with **zero** chunks receive status `skipped-empty` and do not redden the run — nothing can be lost by definition, so empty legacy collections (e.g. `tuples__*`) no longer force `--collections` hand-pinning.

**`--cloud` server-side delegation (RDR-176 P4 / RDR-178 Gap 5, nexus-ekk4o).** Before falling back to the client-mediated copy (every chunk round-trips ChromaCloud → your machine → the engine, ~57 chunks/s over your uplink), `--cloud` probes whether the target engine-service supports the async `ingest-cloud` job contract (`GET /version`, `release_version >= 0.1.18`) and, when it does, triggers ONE batched server-side job that pulls ChromaCloud directly into pgvector at datacenter bandwidth (minutes instead of tens of minutes for a large corpus) — your uplink never sees a vector. Delegation applies only to same-name, dim-dispatchable collections (no `--collections` cross-model remap target); anything else, and any collection the delegated job could not complete, transparently falls back to the unchanged client-mediated path — no data is ever dropped, and no new flag is needed. Progress lines and the final summary table mark delegated collections `[delegated]`; a probe or trigger failure is logged and never aborts the run.

Run the ETL with indexing paused (the post-write count verification assumes a quiescent window). Cutover validation sequence after both legs complete: run `manifest_backfill_sql()` then `manifest_orphan_sql(dim)` for each of 384/768/1024 (from `nexus.migration.vector_etl`) via psql as a superuser/admin role — zero orphan rows per dim is the pass condition. See the module docstring for the rationale (direct SQL, never the repository read API).

---

## nx tier-status

```
nx tier-status [--session SESSION_ID] [--last N] [--since ISO8601] [--json]
```

Audit tier-write activity (T1 scratch, T2 memory/plans, T3 store) for a session. Defaults to the current session (`NX_SESSION_ID`); `--last N` aggregates the most recent N sessions, `--since` bounds by timestamp, `--json` emits structured output instead of the human table. Phase 1B (nexus-a52i).

---

## nx command-context

Generates the agent-relay preamble context that the conexus skills consume (RDR-130 P2). Each subcommand mirrors a skill (`analyze-code`, `architecture`, `create-plan`, `implement`, `debug`, `deep-analysis`, `enrich-plan`, `knowledge-tidy`, `pdf-process`, `plan-audit`, and more) and prints the working-directory, project-type, git-branch, and ready-bead context blocks the agent needs. Run `nx command-context --help` for the full subcommand list. Primarily invoked by tooling, not by hand.

---

## nx rdr

RDR (Research-Design-Review) authoring helpers.

| Subcommand | Description |
|------------|-------------|
| `lint` | Lint RDR frontmatter/structure; reports findings per file |
| `set-status STATUS` | Flip an RDR's `status:` frontmatter field |
| `preamble` | Subgroup backing the RDR lifecycle skills (`rdr-list`, `rdr-create`, `rdr-show`, `rdr-gate`, `rdr-accept`, `rdr-close`, `rdr-research`) |

Run `nx rdr --help` / `nx rdr preamble --help` for the full subcommand list. The `preamble` subcommands are primarily invoked by the conexus RDR-lifecycle skills.
