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
| `--no-rerank` | Disable cross-corpus reranking (use round-robin instead). Reranking runs SERVER-side (RDR-188): the engine scores candidates (Voyage rerank-2.5, or the local ms-marco cross-encoder without a Voyage key); a degraded rerank is reported on stderr and results fall back to distance order |
| `--where KEY{op}VALUE` | Metadata filter (repeatable; multiple flags are ANDed). Operators: `=`, `>=`, `<=`, `>`, `<`, `!=`. Range operators (`>=`, `<=`, `>`, `<`) auto-coerce an unambiguous numeric literal to a number for ANY field (numeric compare; JSON-string metadata values will not match a numeric operand); quote the value (`--where "created>='2026-01-01'"`) to force an ordered-STRING compare (correct for ISO dates; beware `'9' > '10'` lexically). Equality/`!=` coerce only the known numeric fields (`bib_year`, `bib_citation_count`, `page_count`, `chunk_count`). Example: `--where bib_year>=2024 --where section_type!=references`. Repeating the same key with `=` for two DIFFERENT values (`--where k=a --where k=b`) is a caller error, not an OR — no record can equal both — and raises loudly instead of silently keeping the last value; scope "any of several exact values" as two queries unioned client-side (single-key `$in` tracked as nexus-4gzc8; see [PDF Extraction Backends](#pdf-extraction-backends) for a worked `extraction_method` example) |
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

**Unchunkable sources (nexus-rqsh1, Hal directive 2026-08-15):** the indexer never registers a catalog document for a file it will not chunk. `repo` discovery silently skips zero-byte and binary-content files (counted in a `skipped_unchunkable` summary line — expected noise in an unbounded walk); the single-file forms (`md`, `pdf`, `rdr`) instead FAIL LOUD with a clean error naming the file (the operator named that exact file, so plain success with nothing registered would mislead), before any catalog write. In `rdr`'s batch walk an unchunkable file fails that file only, counted, never aborting the batch. `repo` staleness also now treats a doc whose catalog `index_state` is `indexing`/`failed` as stale regardless of content-hash match (nexus-cp46b) — a doc stranded by a failed upload drains on the next normal run, no `--force` needed.

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
| `--since-head` | Index only the git delta since the last indexed commit (`owners.head_hash`): changed files re-index, deleted files' docs prune, full-tree passes (staleness pulls, housekeeping, misclassified/orphan prunes, rg cache rebuild) are skipped. Worktree-inclusive. Falls back to a full index when no usable base exists; ignored with `--force`/`--force-stale`. The per-commit hook's fast path |
| `--corpus [docs\|knowledge]` | Corpus routing for auto-classified prose/PDF files (default: `docs`). `docs` routes to `docs__` collections; `knowledge` routes to `knowledge__` collections instead |
| `--on-locked {skip,wait}` | Behavior under contention (default: `wait`). Per-repo advisory lock (two `nx index repo` on the same repo): `skip` exits immediately, `wait` blocks. Catalog-write fairness (RDR-146): when a foreground interactive catalog write is pending, `skip` defers this run's catalog writes to the next idempotent pass, `wait` proceeds after a bounded yield. `NX_WRITE_PRIORITY=interactive|batch` overrides the tty-based priority of a run's catalog writes. |

Per-file indexing runs with bounded concurrency (6.3.1, nexus-cfc72): 2 workers by default when both the vectors and catalog backends are the HTTP service, 1 otherwise. `NX_INDEX_CONCURRENCY=N` overrides (a warning is logged when it forces concurrency past the backend gate). Progress callbacks and post-store hook chains are serialized; `--debug-timing` gains a `hooks_s` bucket so hook-serialization wait is visible separately from upload time.

Every `nx index repo` run also writes a per-repo log file at `~/.config/nexus/logs/index-<repo>-<hash8>.log` (6.3.3, nexus-mjc9l) — `tail -f` it to watch progress independent of terminal buffering, or check it after the fact if the terminal session is lost. Each run starts with a fresh file; the previous run's content survives as `.log.1`.
| `--no-taxonomy` | Skip automatic topic discovery after indexing |
| `--debug-timing` | Emit an end-of-run per-stage breakdown to stderr (chunking / embed / upload / retry seconds per file, aggregated with percentages). Instruments code, prose, and PDF per-file paths — silent without the flag. Use when investigating "why did indexing take N minutes?" (introduced 4.9.0, nexus-7niu) |

**Observability output** (stderr, all emitted automatically during `repo` runs):

- **Per-file line** — `  [N/total] path — K chunks  (T.Ts)` printed as each file completes (or when `--monitor` / no-TTY).
- **`[eta]` line** — every 60 s: `[eta] N/total files · C chunks · Xs/file avg · ~M min remaining`. Fires regardless of TTY so CI / `nohup` / `tail -f` see pace even when tqdm suppresses its bar (introduced 4.8.0, nexus-vatx Gap 3).
- **`[post]` phase markers** — after the per-file loop, the pipeline keeps running for RDR discovery, pruning, pipeline-version stamping, and catalog registration. Each phase emits `[post] <phase>…` / `[post] <phase> done (Xs)`, bookended by `[post] Post-processing complete (Xs)` (introduced 4.8.0, nexus-vatx Gap 2). Catalog linking is three of those phases since 7.22.0 (nexus-jg3x5): `[post] Catalog linking: rdr…` / `… rdr done (8.1s)`, then `prose`, then `pdf` — each pair only for a generator that has a new document of its source type to link (no phantom `pdf` pair on a batch with no PDF), the duration being the one recorded in the `catalog_hook_stage_timing` log event; a generator that fails closes its phase with `… <kind> failed (Ns)`.
- **Transient-error backoff summary** — on exit, if any Voyage / ChromaDB retry fired: `Transient-error backoff: Xs total (voyage ..., chroma ...)`. Silent on clean runs. Visible on exception paths (introduced 4.8.0, nexus-vatx Gap 4a).
- **Rate-limit brake summary** — on exit, if the shared rate-limit brake paused any writer this run: `Rate-limit brake: N pauses, Ss`. Silent when the brake was never tripped. Emitted by `nx index repo`, `nx index pdf` (single-file and `--dir`), and `nx index md`. See "Voyage per-project rate limit" below (nexus-cy9u7).

**Voyage per-project rate limit (nexus-cy9u7):** the engine embeds server-side on write, so a bulk `nx index` run's real "embed pressure" is its T3 vector-write and catalog manifest-write request rate. Voyage's RPM budget (4000 RPM for `voyage-context-3`) is per PROJECT, not per process or per worker — every concurrent worker thread AND every concurrent `nx index` session sharing that Voyage project draws from the SAME budget. Every write path now routes through a shared process-wide "rate brake" — `HttpVectorClient.upsert_chunks` (the one choke point every T3 write call site funnels through: the ChunkBatcher's combined-write flush, the per-file prose/code fallback, and PDF indexing, which never uses the batcher), the catalog manifest write, and the migration-ETL leg. The first worker to see ANY retryable transient failure — a 429, 502, 503, or 504, or a retryable transport error (connect refused, read timeout, ...), not only a narrow 429/503-with-`Retry-After` signal — pauses EVERY writer in this process until the same shared deadline, instead of each worker backing off independently and re-firing the limit the moment its own backoff elapses (the 2026-08-15 incident, conexus-ddh0/nexus-99r7y: the engine was retrying Voyage internally and the edge's own timeout surfaced to the client as a 502/504 with no `Retry-After` at all — a signal the narrower pre-fix scope would have missed entirely). The pause is floored at the server's `Retry-After` when one is supplied, otherwise an escalating default (2s, doubling per consecutive process-wide trip, capped at 60s); it resumes at the base delay once a write succeeds. `nexus-99r7y` (engine fail-fast with an explicit 429 + `Retry-After` instead of a bare edge timeout) sharpens this signal but is **not required** for the brake to engage — the escalating-default path covers every retryable failure shape either way.

**Deliberate coupling, disclosed (nexus-gvuv0):** the catalog manifest write also trips this SAME brake on a plain connectivity error (a dropped connection, a timeout) with no HTTP status involved at all — not only on a rate-limit signal. This is intentional: the manifest write and the T3 vector write land on the same local engine process, so a connectivity blip there is evidence the ENGINE is struggling, not evidence of a Voyage-specific problem — pausing every writer briefly is cheap and self-corrects on the next successful write. A locally-restarting engine can therefore pause bulk vector writes too, briefly, even though the vector-write path itself saw nothing wrong.

This brake is a per-PROCESS coordination mechanism — it cannot see or pace OTHER processes/sessions sharing the same Voyage project, and it is attempt-bounded, not time-unbounded: each write-retry wrapper keeps its own attempt budget (only widened for a narrow 429/503+`Retry-After` signal), so a genuinely dead upstream still fails in bounded time — worst case per call ranges from ~2-5 minutes (a generic transient failure, process already mid-incident — this includes `http_vector_client._request`'s own inner gateway retry, ~17s, stacking with the outer wrapper's per-attempt sleep) to ~35 minutes (a sustained rate-limit window with the server consistently reporting a large `Retry-After`); see `nexus.retry`'s wrapper docstrings for the exact per-path numbers. Operators running several bulk `nx index` invocations concurrently (multiple terminals, multiple CI jobs, multiple agents) against the same account should size that concurrency with the shared 4000 RPM budget in mind; the brake reduces self-inflicted pile-ups within one process but is not a substitute for sizing the job.

**Concurrent / interrupted runs (nexus-lcmbp):** a pipeline row stranded in `running` state is checked by heartbeat freshness before a retry proceeds. A `running` row younger than the stale threshold (5 minutes) now fails LOUD — HTTP 409 `conflict_running`, surfaced client-side as a non-zero exit with a remedy string — instead of the old silent `rc=0` skip that wrote zero chunks and reported success. A `running` row older than the threshold, or one marked `failed`, resumes normally; a `completed` row is skipped as up to date. Batch `--dir` mode places a fresh-heartbeat conflict in the run's failures bucket rather than aborting the whole batch — every file is still attempted (nexus-uqq9z: the batch as a whole now exits non-zero once any file lands in that bucket; see the Exit code note below).

Two related output lines are new since the RUNFENCE arc (nexus-5xn3k): `skipped: index fresh (use --force)` on `repo`/`pdf`/`md`/`rdr`/`nx dt index` paths where a staleness no-op previously printed nothing or `Indexed 0 chunk(s)`; and an end-of-run `WARNING: N of the M indexed above had completion refused by the engine's fail-closed verify (fence left at 'indexing') — NOT fully indexed. Re-index or --force to retry.` The refusal warning means the manifest write succeeded but the engine's completion-verify step declined to stamp the document complete — `index_state` stays `'indexing'`, the chunks are real (already counted among the files indexed this run) but the document is not fully indexed until a subsequent run — usually `--force` — completes the fence. `nx catalog show TUMBLER_OR_TITLE` (`index_state`) and `nx doctor`'s `stale index-run fences` check (see [nx doctor](#nx-doctor)) both name documents left in this state (`nx catalog manifest-verify`, formerly the other pointer here, is [retired](#nx-catalog-manifest-verify--retired) as of RDR-191 Phase 6). Since 7.22.0 (nexus-l6tr7) a `nx dt index` batch that mixes a flush-grain refusal (counted in `indexed`) with a propagating one (bucketed into `failed`) prints the split instead — `N completion refusal(s) …: K of the M indexed above and J listed under failed` — and a bucket count the run-wide collector cannot account for is reported as recording drift (`complete_refused_count_mismatch`), never clamped; either way the run exits non-zero.

**Superseded-chunk sweep summary (nexus-39upx):** when a re-index changes a document's extracted text, the new chunks land under new content hashes and the old ones fall out of the manifest — searchable T3 rows referenced by nothing until swept. `nx index repo` / `nx dt index` / `nx index pdf` / `nx index md` now report this at end-of-run: `swept N superseded T3 chunk(s) left behind by a changed re-index (nexus-39upx)` is informational (a successful cleanup, not a problem — it does not affect the exit code). `WARNING: superseded-chunk sweep skipped for N document(s) (REASON, ...) — old/superseded T3 rows may still be searchable. Re-index, or run 'nx t3 gc -c COLLECTION' once the underlying issue clears.` means the sweep could not verify orphanhood or note-safety for one or more documents this run (reasons: `before_read_failed`, `note_lookup_failed`, `delete_failed`) — capability-honest, never silent, and counted toward the non-zero exit described next.

**Exit code (nexus-tp8yk):** `nx index repo` and `nx dt index` now EXIT NON-ZERO when the run ends with any completion refusal, catalog manifest-write failure, manifest-identity drop, or superseded-chunk sweep skip — the WARNING classes described above. Previously these were WARNING-only (`rc=0`); a script or CI job that gated on the exit code alone could not tell a damaged run from a clean one. The failure message names the remedy (re-index with `--force`; `nx catalog manifest-verify <tumbler>`, formerly also named here, is [retired](#nx-catalog-manifest-verify--retired) as of RDR-191 Phase 6 — use `nx catalog show <tumbler>` instead). This is a NEW exit-code condition on an existing command — a "clean" run that used to exit 0 with WARNING lines now exits non-zero; any automation keying on `nx index repo` / `nx dt index`'s rc should account for this. An UNCONFIRMED completion stamp (a pre-fence engine's `None` sentinel — no verify was possible at all) is a separate case and stays WARNING-only at `rc=0`; only a POSITIVE engine verdict (a refusal) or a write/identity failure triggers the non-zero exit.

**Unextractable-file exit code (nexus-deyd5): a single skip stays clean, a systemic skip does not.** A file that raises `UnextractableContentError` during `nx index repo` is skipped and counted, but no longer fails the run by itself (`rc=0`) — nothing was ever written for that file, so skipping it alone costs no data; a `Note:` line on stderr names the count and points at the WARNING/ERROR log line(s) for the affected path(s). Separately, after the run's drain and post-processing have all already completed and committed, the AGGREGATE skip population across the whole run is checked against a systemic-skip floor: 100% of attempted files skipped, or at least 20 files attempted with 50% or more skipped. Crossing that floor raises a `ClickException` (non-zero exit) naming the skipped/attempted counts and percentage, even though the run itself finished and its successful work was already committed — nothing is discarded by this check, it only decides the exit code. The message suggests the most likely legitimate cause: scanned/image-only PDFs, since the default extractor does not run OCR (retry with `--extractor mineru` or set `pdf.extractor: mineru` in `.nexus.yml`).

**`pdf --dir` batch exit code (nexus-uqq9z):** `nx index pdf --dir` now EXITS NON-ZERO when one or more files land in the run's failures list — real per-file exceptions (extraction errors, pipeline conflicts, completion refusals) AND manifest-identity drops alike, i.e. any entry the printed `N failure(s):` list carries. Previously the batch always exited 0 regardless of how many files failed, the same "clean rc, damaged run" gap `nx index repo` / `nx dt index` closed above, just left open one layer down for the `--dir` batch path. Per-file isolation is UNCHANGED — every PDF in the directory is still attempted and the failures list still prints exactly as before; only the batch's own exit code is new. The failure message is `N of M file(s) failed — see list above`. Single-file `nx index pdf PATH` (no `--dir`) already exited non-zero on its own failures (nexus-7f5qj) and is unaffected.

**`pdf` and `md` flags:**

| Flag | Description |
|------|-------------|
| `--corpus NAME` | Corpus name for the `docs__` collection (default: `default`) |
| `--collection NAME` | T3 collection name, overriding `--corpus` when set. Bare names are normalized through `t3_collection_name()` (e.g. `delos` becomes the conformant `docs__delos__<model>__vN` shape); a fully-qualified name (e.g. `knowledge__delos`) is honoured as given |
| `--source-uri URI` | Resolve catalog identity by URI (e.g. `x-devonthink-item://<UUID>`) INSTEAD of by file path (nexus-y8qtj). Use when re-indexing a document that was originally registered under an out-of-band identity — a plain path-based re-index misses it and forks a second catalog Document, leaving the original's chunks live and un-swept. **Fail-loud, no silent fallback:** a URI that resolves to no live document is an error (never registers a new document); a URI that resolves to a document in a *different* `--collection` than this run targets is also an error (a move, not a re-index — use `nx catalog update` to move it deliberately). `pdf --source-uri` is mutually exclusive with `--dir` |

At the end of an index run, if the freshly-indexed document shares more than
a quarter of its chunks with another *live* catalog document, a
`index_possible_document_fork` WARNING is logged and a summary line is
printed (`N possible document fork(s) detected`) — this is a heads-up, not a
refusal: legitimate near-duplicates (a preprint vs. its camera-ready
revision) can share a meaningful minority of chunks without being the same
catalog identity.

**`pdf`-only flags:**

| Flag | Description |
|------|-------------|
| `--dir DIR` | Index all PDFs in a directory (mutually exclusive with `PATH`) |
| `--enrich` | Query Semantic Scholar for bibliographic metadata (year, venue, authors, citations). Off by default. Use `nx enrich bib <collection>` for bulk backfill |
| `--extractor [auto\|docling\|mineru]` | PDF extraction backend (default: `auto`). See [PDF Extraction Backends](#pdf-extraction-backends) below |
| `--on-formula-oom [fail\|docling]` | What to do when a single page reproducibly OOM-kills MinerU's formula model (default: `fail`). `fail` aborts the document (preserves the no-silent-fallback-for-formulas guarantee). `docling` degrades only that page to docling (formula-stripped) and continues |
| `--dry-run` | Preview extraction and chunking only — nothing is embedded, stored, or written (no API keys needed). Prints a chunk preview. Also registers no catalog document (nexus-uxg4u) — a preview never mints a Document row a subsequent refusal would leave behind. A real (non-dry) run that fails after registering a brand-new document rolls that registration back automatically; re-indexing a pre-existing document is left exactly as the completion fence marked it |
| `--streaming [auto\|always\|never]` | Pipeline mode (default: `auto`). `auto` uses the streaming pipeline for all PDFs (crash-resilient); `never` forces the legacy batch+checkpoint path |
| `--allow-degraded-extraction` | Accept extracted text that fails the post-extraction quality gate (nexus-wi1uv, see [Post-Extraction Quality Gate](#post-extraction-quality-gate) below) instead of failing the run |

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

**MinerU is included by default** since nexus-2fyb. Previously gated behind a `[mineru]` extra; the extras gate produced silent formula loss because fresh installs never picked it up. First use of `auto` or `mineru` modes downloads the unimernet model (~2-3 GB). If MinerU is missing at runtime, your install is corrupt — reinstall with [`nx self install`](#nx-self-install) (from a dev checkout, `scripts/reinstall-tool.sh`). The runtime error prints the command for the layout it finds, so a box still on the legacy uv tree is told the uv form instead.

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

#### Post-extraction quality gate

`--extractor docling` is the documented recovery when MinerU OOM-fails on a
formula-dense page — but on some pages Docling (and, less often, MinerU)
can complete "successfully" while producing **space-stripped, unsearchable
text**: words run together (`istheasetofthe`), with heavy raw-LaTeX noise.
Nothing about the run looked wrong — it reported success, the chunks were
indexed and embedded — the document just could not be found by search
(nexus-wi1uv).

Every extraction (MinerU, Docling, and the PyMuPDF fallback alike) now
passes through one post-extraction sanity gate before chunking: three cheap
signals — whitespace ratio, mean token length, and the fraction of
abnormally long tokens — computed on the raw extracted text and compared
against thresholds calibrated against real extracted text (clean prose,
legitimately dense math/formula notation, and code-identifier-dense prose,
so the gate does not reject real papers for being equation- or
algorithm-heavy). A document whose signals cross the thresholds **fails the
run** naming the failing signal(s), the measured value, and the remedy:

```
PDF paper.pdf failed the post-extraction quality gate (extraction_method=docling):
whitespace_ratio=0.0114 < floor 0.05. This is the space-stripped-garbage failure
mode (nexus-wi1uv) — the extracted text is likely unsearchable if indexed.
Remedy: retry with `--extractor mineru` (formula-aware, often avoids the
corruption), or if this document is legitimately dense/unusual and you have
reviewed the extracted text, rerun with `--allow-degraded-extraction` to
index it anyway.
```

**Blast radius of a failure differs by command.** `nx index pdf` (single
file or `--dir`) fails just that invocation/file — `--dir` continues the
rest of the batch and exits non-zero listing every failure. `nx dt index`
contains a gate failure to the offending record (per-record batch
isolation, same as any other recoverable indexing error) and continues the
rest of the run. `nx index repo` contains it to the offending PDF too —
the rest of the repo (code, prose, other PDFs, RDR docs) still indexes —
but a non-zero count of gated PDFs still fails the overall `nx index repo`
run's exit code (after post-processing completes), naming the count and
the per-file remedy below.

**Non-spaced scripts (CJK).** `str.split()` cannot segment Han, Hiragana,
Katakana, or Hangul text — a real Chinese/Japanese/Korean document has no
inter-word ASCII spaces and would otherwise look identical to the
space-stripped-garbage signature on every signal. The gate detects
non-spaced-script-dominant text and **skips** evaluation for it (logged at
INFO as `extraction_quality_gate_skipped`) rather than force-fitting a
threshold that cannot discriminate for that input — a mixed document
(e.g. English section headers in an otherwise-CJK paper) is unaffected as
long as CJK characters are not the dominant script.

Pass `--allow-degraded-extraction` only after reviewing the extraction (or
for a document you've confirmed genuinely trips the heuristic) — the run
logs a WARNING so the override is visible immediately, AND every chunk of
the document is stamped `quality_gate_overridden: true` in its persisted
T3 metadata, so a later shakedown/audit has a durable handle on which
documents were accepted with known-degraded text:

```bash
nx search "" --corpus docs --where quality_gate_overridden=true --files
```

**Querying by extractor identity (`extraction_method`, nexus-1oguj).** Every
PDF chunk's metadata carries which backend actually produced its text:
`docling` | `mineru` | `pymupdf_normalized`, or the honest mixed aggregate
`mineru+docling-degraded` when `--on-formula-oom docling` degraded at least
one page of an otherwise-MinerU document. This is what makes an extractor
regression scopeable after the fact (e.g. "which documents did MinerU
extract, so I can re-index just those").

```bash
nx search "" --corpus knowledge --where extraction_method=mineru --files
```

- **Scoping "any mineru-touched document" is two queries today, not one.**
  `--where` equality has no single-key OR/`$in` yet (repeating the same key
  with different values is a caller error and now raises loudly — see
  `--where` above) — `mineru` and `mineru+docling-degraded` are two distinct
  exact values. Run one query per value and union the results client-side:
  `--where extraction_method=mineru` and
  `--where extraction_method=mineru+docling-degraded` separately. Single-key
  `$in` support is tracked as nexus-4gzc8.
- **Absence of the key is not evidence of "not mineru."** `extraction_method`
  is a NEW-WRITES-ONLY field — chunks indexed before this fix carry no such
  key at all, so `--where extraction_method!=mineru` (or any filter that
  relies on the key's absence meaning "some other extractor") silently
  conflates "known non-mineru" with "indexed before this field existed."
  There is no backfill for pre-fix chunks (re-extraction would be required
  to recover the value honestly); treat a missing key as *unknown*, never as
  a negative result. Tracked as nexus-0qc4b.

---

## nx dt

DEVONthink integration verbs (macOS only). Wraps DT so selections, smart
groups, tags, and groups flow into Nexus indexing without manual
UUID/path copying, and Nexus search results round-trip back to DT via
`nx dt open`. Design rationale and acceptance criteria live in
[RDR-099](rdr/rdr-099-devonthink-integration.md); the smart-rule recipe
is in [`devonthink-smart-rules.md`](integrations/devonthink-smart-rules.md).

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

### nx dt incorporate

```
nx dt incorporate UUID
```

Incorporate an already-indexed DEVONthink record into the nexus graph (macOS-only; relocated from the retired `nx-mcp-devonthink` proxy's `dt_incorporate` tool, nexus-goypg). Resolves the record's tumbler (the record must already be indexed — run `nx dt index` or `nx dt capture` first), generates DT-derived `relates` edges to its DEVONthink similarity and explicit-link neighbours that are also indexed in nexus (Layer B), and stamps the nexus identity back onto the DT record (Layer F: `nx-indexed`/`nx-tumbler` tags plus a tumbler backlink annotation). Prints the tumbler, link counts, and writeback summary.

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
| `--backfill-catalog` | Re-drive the catalog write from ALREADY-enriched chunk metadata — no external API calls. Populates the catalog Document rows' `bib_*` fields (surfaced by `catalog_search` / `catalog_list` / `nx catalog show`) for collections enriched before those columns had a writer. Idempotent; titles whose chunks carry bib metadata but have no matching catalog row are reported separately as skipped |

Enrichment also writes the same bib fields to the matching **catalog**
Document rows (year, authors, venue, citation count, backend id), so
`catalog_search`, `catalog_list`, and `nx catalog show` surface them.

**Note**: Semantic Scholar's public API allows 100 requests per 5 minutes without an API key. OpenAlex is unauthenticated but encourages including a contact email via `pyalex.config.email`. For large collections, increase `--delay` or use `--limit` to process in batches. DOI extraction prefers labeled DOIs (`DOI: 10.x/y`) over bare DOI strings to avoid contamination from cited references.

### nx enrich aspects

Batch-extract structured aspects (problem formulation, proposed method, datasets, baselines, results, extras) for documents in a `knowledge__*` collection. Iterates the catalog (one entry per source document, NOT per chunk) and calls the synchronous extractor directly, bypassing the post-document hook chain to avoid double-firing on documents already triggered at ingest. Aspects land in T2 `document_aspects`.

**GAP-FILL IS THE DEFAULT (7.18.0, `nexus-ym9ey`).** A bare invocation processes
only documents with NO aspect row. Before 7.18.0 it re-extracted the entire
collection every time: three consecutive runs over the same `rdr__` collection
each reported "294 extracted" while filling zero gaps. That was free there
because `rdr__` routes to the deterministic parser, but on a Claude-CLI-backed
collection it re-spends on the whole corpus — filling one missing document in a
421-document collection re-dispatched all 421, and re-rolled 420
non-deterministic extractions that were already correct. Pass `--all` for the
old behaviour.

```
nx enrich aspects knowledge__delos                     # gap-fill: only uncovered documents
nx enrich aspects knowledge__delos --all               # re-extract everything (pre-7.18.0 default)
nx enrich aspects knowledge__delos --dry-run
nx enrich aspects knowledge__delos --validate-sample 10
nx enrich aspects knowledge__delos --re-extract --extractor-version claude-haiku-4-5-20251001
```

| Flag | Description |
|------|-------------|
| `COLLECTION` (positional) | Must be a `knowledge__*` collection (Phase 1 scope). Other prefixes return a "no extractor config" error |
| `--all` | Re-extract EVERY document, including ones that already have an aspect row. The pre-7.18.0 default; now opt-in because it re-spends on the whole corpus. Without it, only documents with NO aspect row are processed |
| `--dry-run` | Report document count + cost estimate (Haiku-class). No API calls, no T2 writes. The read-side skip prediction samples the first 25 entries (one vector-service round-trip each) and projects the skip rate onto the rest (7.21.0, nexus-bocft: one round-trip per entry ran a 407-entry cloud dry-run past five minutes with nothing printed); the exact per-entry verdicts come from the real run |
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

**`--missing` uses the gap-fill's own key (7.21.0, nexus-bocft).** A catalog entry is matched to `document_aspects.source_path` by `file_path or title` — the identity the store hook mints and the identity `nx enrich aspects` bills by. Before 7.21.0 the audit keyed on `file_path` alone and so silently dropped every title-only note: on a 416-entry knowledge collection with 10 file-path entries it reported 1 gap where the gap-fill would dispatch 407. The audit also reports **orphaned aspect rows** — rows in T2 that no current catalog entry claims (identities recorded under an earlier registration rule); a large count there is why "437 rows but 407 gaps" can both be true, and those rows never cover a gap. `--json` emits `{collection, entries, aspect_rows, gaps: [{tumbler, title, file_path, identity}], orphaned_aspect_rows: [...]}`.

| Flag | Description |
|------|-------------|
| `--collection NAME` (required) | T3 collection to inspect (e.g. `knowledge__delos`) |
| `--limit N` | Maximum rows to display (default: 20; use 0 for unlimited) |
| `--missing` | Flip output: list catalog rows with NO aspect record, keyed by `file_path or title` (the gap-fill's key), plus any orphaned aspect rows no current entry claims |
| `--json` | Emit JSON array instead of human-readable form |

### nx enrich list

```
nx enrich list COLLECTION [--limit N] [--scheme SCHEME]
```

Day 2 Ops: list extracted aspect rows for a collection. One row per source document (not per chunk), in `source_path ASC` order. Returns source path, extractor name, model version, extracted-at timestamp, and a confidence indicator. `--limit` caps the printed rows (default `0` = unlimited). `--scheme` (RDR-096 P3.2) filters to rows whose `source_uri` scheme matches (`file`, `chroma`, `https`, `nx-scratch`, ...); rows with empty/NULL `source_uri` are excluded once `--scheme` is set. No `--json` output. Useful for triaging "what got extracted?" before running `--re-extract` against a model upgrade.

### nx enrich info

```
nx enrich info COLLECTION SOURCE_PATH
```

Day 2 Ops: show the full aspect record for a single document. Takes no options — output is always JSON. Includes the five fixed columns (problem_formulation, proposed_method, experimental_datasets, experimental_baselines, experimental_results), the `extras` JSON object, confidence, extracted_at, model_version, and extractor_name.

### nx enrich delete

```
nx enrich delete COLLECTION SOURCE_PATH [--yes]
```

Day 2 Ops: remove a single aspect row. Use when re-indexing a document with a content change that should drop the prior aspects rather than overwrite. Requires `--yes` for confirmation. Safe: the underlying chunks in T3 are untouched.

### nx enrich aspects-promote-field

```
nx enrich aspects-promote-field [--history]
```

Show the `extras` -> fixed-column promotion history (RDR-089 Phase E).

**The runtime promotion verb is retired** (nexus-70x7y, 2026-07-25). It ran an `ALTER TABLE` + backfill directly against SQLite; under the ALL-DDL-through-Liquibase directive there is no substrate where that is legal, so it was deleted rather than reimplemented service-side. Invoking the command with a field name prints the replacement recipe and exits 2.

Promoting `extras.<name>` is now ONE Liquibase changeset that does all three phases together:

1. `ALTER TABLE nexus.document_aspects ADD COLUMN <name> <TYPE>;`
2. `UPDATE nexus.document_aspects SET <name> = extras->>'<name>' WHERE <name> IS NULL AND extras ? '<name>';`
3. `INSERT` the promotion event into `nexus.aspect_promotion_log`, so `--history` still reports it.

The optional extras prune (`SET extras = extras - '<name>'`) is a **separate, later** changeset — run it only once every reader consumes the typed column, preserving the dual-read cutover. Record it with `pruned = 1`. The full changeset template lives in `src/nexus/aspect_promotion.py`.

| Flag | Description |
|------|-------------|
| `--history` | List prior promotions from `aspect_promotion_log`, oldest first. Reads whichever `document_aspects` backend is configured: the local table on SQLite, `GET /v1/aspects/promotion/list` on the service backend |
| `FIELD_NAME` (positional, optional) | Accepted for compatibility with the retired form. Supplying it prints the changeset recipe and exits 2 |

---

## nx aspects

Aspect-extraction queue management (the async queue feeding the aspect-extraction worker). The group also provides `drain`, `gc`, and `gc-fixtures`.

### nx aspects drain

```
nx aspects drain [--timeout SECONDS] [--poll-interval SECONDS]
```

Stops the singleton `AspectExtractionWorker` (if running in this process), then waits until all pending and in-progress rows are processed or `--timeout` (default 30s) elapses; `--poll-interval` (default 0.1s) sets the queue-empty poll cadence. Use before `nx upgrade` when a `MigrationError` reports the aspect_extraction_queue is not drained. Exit 0 once drained (or already empty); exit 1 on timeout, naming the stuck-row count.

### nx aspects gc / gc-fixtures — RETIRED

```
nx aspects gc [--apply]              # refuses unconditionally
nx aspects gc-fixtures [--yes]       # refuses unconditionally
```

Both verbs are RETIRED and always raise a `click.UsageError` regardless of the flag — they are guided refusals, not report-only fallbacks. `gc` ATTACHed the local `.catalog.db` SQLite cache to the local T2 to sweep `source_uri`-keyed orphan aspect rows; `gc-fixtures` issued raw SQL `DELETE`s against the local SQLite `document_aspects` / `aspect_extraction_queue` tables (RDR-120 §A8). Both local SQLite substrates were deleted in the RDR-158 P4 retirement, so neither has a service-backend equivalent. On the service backend, aspect rows orphaned by a document delete cannot accumulate — `document_aspects.doc_id` is FK-bound to `catalog_documents` `ON DELETE CASCADE` (`fk-001-catalog-cross-store`) — but that FK does not cover the `source_uri`-keyed class `gc` used to sweep, since `source_uri` is a path string, not a tumbler reference. Tracked: nexus-ingey (`gc`), nexus-gmiaf.37 (`gc-fixtures`).

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

### nx aspects backfill-source-uri (retired)

```
nx aspects backfill-source-uri [--apply]   # refuses with guidance
```

**RETIRED.** This verb backfilled NULL/empty `source_uri` rows in the LOCAL
SQLite `document_aspects` (RDR-120 §A8 repair verb) ahead of the 4.31.0
migration that dropped the legacy `source_path` column. That migration
chain was deleted in the RDR-158 P4 retirement, and on this version the
local `.db` is a FROZEN migration source (RDR-176 Gap 2) that must never be
written — the unguarded raw UPDATE this verb carried was the last write path
into it. The command now raises `click.UsageError` unconditionally, with or
without `--apply`. If a pre-migration install still needs the repair, run it
on the last migration-capable 6.x release, which still ships both the verb
and the migration it serves.

### nx aspects gc-pre-rdr096 (retired)

```
nx aspects gc-pre-rdr096 [--apply]   # refuses with guidance
```

**RETIRED.** This verb deleted pre-RDR-096 read-failure rows from the LOCAL
SQLite `document_aspects` (RDR-120 §A8 repair verb), using the seven-clause
discriminator from RDR-096 research-3: every aspect field empty AND
`confidence IS NULL`, reachable only from a pre-RDR-096 read failure. On
this version the local `.db` is a FROZEN migration source (RDR-176 Gap 2)
that must never be written — the raw DELETE this verb carried was an
unguarded write path into it — and the engine's live table cannot hold the
pre-RDR-096 fingerprint (the going-forward writer contract predates the
migration), so there is nothing to sweep service-side either. The command
now raises `click.UsageError` unconditionally, with or without `--apply`.
If a pre-migration install still needs the sweep, run it on the last
migration-capable 6.x release.

---

## nx catalog

Document catalog — track indexed documents and the relationships between them.

### nx catalog setup / init (retired)

```
nx catalog setup      # refuses with guidance
nx catalog init       # refuses with guidance
```

**Retired in 7.0.0 (nexus-i711w).** The nexus service owns the catalog in
every mode and documents are registered automatically at index/store time —
there is nothing to initialize or set up. Both verbs remain as guided
refusals so old scripts fail with an explanation instead of a usage error.
The local SQLite catalog (and the `NX_STORAGE_BACKEND_CATALOG=sqlite`
opt-out these verbs served) no longer exists.

### nx catalog search

```
nx catalog search QUERY [--limit N] [--offset N] [--json]
```

Find documents by title, author, corpus, or file path. Returns tumbler, content type, and title.

### nx catalog export / import

```
nx catalog export FILE     # write the recovery bundle (run BEFORE a reinstall)
nx catalog import FILE     # restore it (after reinstall + re-index)
```

One paired recovery verb (GH #1419.9): a human-inspectable JSONL bundle
carrying the catalog **link graph** and **store_put-origin knowledge
content** — the two things a reinstall cannot regenerate. Identity is
`source_uri` (tumblers are not stable across reindex); no embeddings are
carried (import re-embeds through the real store_put chain, so the
bundle survives an embedding-mode change); import is idempotent and
reports every unresolvable link or failed doc without aborting the rest.
See `docs/catalog.md` § Recovery bundle for the format contract. For an
embedding-preserving per-collection backup use `nx export COLLECTION`
(`.nxexp`) instead.

### nx catalog show

```
nx catalog show TUMBLER_OR_TITLE [--json]
```

Full document metadata, physical collection, and all links in and out. Accepts tumblers or titles.

**Owner prefixes (nexus-v3w9n, catalog-034):** the tumbler grammar is enforced at the engine's API boundary (HTTP 400, rule `tumbler-grammar`) — a schema CHECK follows once the engine test fixtures conform (nexus-ia69x). On the ENGINE side an owner prefix is exactly 2 dot-separated segments (e.g. `1.7` or `bt.1`; segment content need not be numeric), a document tumbler is 3 or more. Passing a depth-2 NUMERIC tumbler to `nx catalog show` renders an owner card (`"kind": "owner"` in `--json`; name, type, repo hash, next sequence number, document count in text) instead of a document; depth 3+ is unchanged. An unknown depth-2 prefix still reports `Not found: <value>`. This CLI/MCP feature is numeric-only — `Tumbler.parse` (the Python client) is int-segmented, so a mnemonic owner prefix like `bt.1` falls through to title-fallback resolution instead of an owner card; production owner prefixes are always numeric (`1.N`) in practice, so this does not affect real usage.

### nx catalog manifest-verify — RETIRED

**RETIRED, RDR-191 Phase 6 (bead nexus-o8dil.33), 2026-08-15.** Both modes
(`TUMBLER_OR_TITLE` single-document check and `--list` corpus-wide
enumeration) are removed — the manifest-chunk FK
(`nexus.catalog_document_chunks` → `nexus.chunks`, added and VALIDATEd in
RDR-191 Phase 5) now REJECTS a dangling manifest row at write time (a
manifest INSERT referencing a nonexistent chunk, or a DELETE of a
still-referenced chunk, both fail loudly at the database), making the
diagnostic this command existed to run unreachable by construction: there
is no dangling-manifest state left for it to ever find. The underlying
engine functions it called (`nexus.manifest_orphans(dim)`,
`nexus.manifest_verify_all()`) are dropped outright
(`catalog-030-retire-manifest-verify.xml`); `nexus.manifest_verify(text)`
itself is kept, but only for `CatalogRepository.completeIndexRun`'s
internal write-path use — there is no client route or CLI surface onto it
any more.

To confirm a specific document is fully indexed, inspect
`nx catalog show TUMBLER_OR_TITLE` for `index_state` and re-index with
`nx index <path> --force` if it reads anything other than `complete` — the
write-path fail-closed verify-then-stamp (`CatalogRepository
.completeIndexRun`) already refuses to mark a document `complete` unless
its manifest is verified whole, so `index_state == 'complete'` is itself
the ground truth this retired command used to re-derive after the fact.

Not to be confused with [`nx catalog verify`](#nx-catalog-verify) below
(nexus-whh61.4, a pre-existing and unrelated command, unaffected by this
retirement) — that one reconciles the whole catalog against T3 on the
chash identity (vanished/lost finding classes in full mode;
vanished/damaged/lost in `--collection`-scoped mode, which computes damage
independently of the retired functions above).

### nx catalog links

```
nx catalog links [TUMBLER] [--from TEXT] [--to TEXT] [--type TEXT] [--created-by TEXT] [--direction in|out|both] [--depth N] [--limit N] [--offset N] [--json] [--resolve] [--unique-targets]
```

With a positional tumbler/title: BFS graph traversal. Without: flat filter query across all links. `--resolve` renders each endpoint as `<title-or-path> (<tumbler>)` instead of a bare tumbler; `--unique-targets` collapses rows that point at the same `file_path` via different owner tumblers (e.g. after re-indexing), keeping the first edge seen per target.

### nx catalog link

```
nx catalog link FROM TO --type TYPE [--from-span SPAN] [--to-span SPAN]
```

Create a typed link. Both endpoints accept tumblers or titles. Types: `cites`, `implements`, `implements-heuristic`, `supersedes`, `quotes`, `relates`, `comments`, `formalizes`.

Span formats: `line-line` (positional), `chunk:char-char` (positional), `chash:<sha256hex>` (whole chunk, content-addressed), or `chash:<sha256hex>:<start>-<end>` (character range within a chunk). Content-hash spans survive re-indexing; positional spans may become stale.

**Dangling endpoints are refused** (engine >= v0.1.61, nexus-9ssih): if either `FROM` or `TO` does not resolve to a live catalog document, the engine returns `400 {"code": "dangling_endpoint", ...}` and the CLI surfaces a clean `ValueError`-derived message naming both endpoints — no edge is written. Older engines that don't emit this code accept the link silently, same as before. `allow_dangling=True` bypasses the refusal but is **Python-API-only** (`HttpCatalogClient.link(..., allow_dangling=True)`) — the CLI `nx catalog link` command has no `--allow-dangling` flag.

### nx catalog unlink

```
nx catalog unlink FROM TO [--type TYPE]
```

Remove link(s). Omit `--type` to remove all link types between the pair.

### nx catalog sync / pull (retired)

```
nx catalog sync [-m MESSAGE]     # refuses with guidance
nx catalog pull                  # refuses with guidance
```

**Retired in 7.0.0 (catalog-git-DECISION Option C).** The nexus service's
Postgres is the sole catalog authority in every mode — every write already
lands there, so there is nothing to commit, push, or pull. Both verbs remain
as guided refusals (`click.ClickException`, no traceback) so an interactive
caller or old script sees an explanation instead of an uncaught
`NotImplementedError`. (Callers that suppress output, like the session-close
Stop hook's best-effort `|| true` invocation, still exit non-zero silently —
same as before, minus the buried traceback.) The git-backed JSONL durability
layer these verbs served no longer exists.

### nx catalog reconcile

```
nx catalog reconcile [--dry-run]
```

Repairs `document_chunks` manifest gaps left by a persistently-failed manifest-write hook (e.g. the catalog engine-service was briefly unreachable during indexing). A gap is a document with `chunk_count > 0` but fewer manifest rows than that (including zero) — such a document silently drops out of catalog-aware retrieval even though T3 still has its chunks.

For each gapped document, rebuilds its manifest from the T3 chunks in its `physical_collection`, matched by the whole-file `content_hash` recorded on both the document and every one of its chunks, ordered by character/line span. T3 fetches are batched per collection (a paged `$in` over content hashes), and the pass emits a scan header plus per-collection progress lines — on a 21k-document service-mode catalog a full pass takes minutes, not hours. `--dry-run` reports the same counts without writing.

The summary splits unmatched documents into two classes so real regressions are never buried in expected noise: `N with chunks LOST (real gap)` (the document recorded chunks but T3 no longer has them — investigate) versus `M never-chunked (expected: nothing to rebuild)` (e.g. empty `__init__.py` files that legitimately produced no chunks).

`nx index repo` also runs this heal automatically (owner-scoped) after per-file indexing and before orphan GC, so manifest gaps self-repair on the next index without re-embedding. The manual verb remains useful for catalog-wide passes.

Also see the end-of-run summary on `nx index repo`: a persistent manifest-write failure during indexing is now surfaced there (`WARNING: catalog manifest write failed for N document(s)`) with a pointer to this command.

#### Orphan-GC quarantine (soft delete)

The `nx index repo` orphan GC never hard-deletes directly: orphan chunks MOVE to a sibling collection (`quarantine__<owner>__<model>__v<n>`, excluded from every search surface by its prefix), embeddings intact. If a later heal or re-index references a quarantined chunk again, it is restored automatically. Quarantined chunks older than `NX_GC_QUARANTINE_DAYS` (default `14`) are hard-deleted on a later pass — and only THAT expiry step is guarded by the safety floor: a hard-delete condemning more than `NX_GC_FLOOR_FRACTION` (default `0.25`) of the quarantine at 100+ chunks means a manifest defect persisted for the whole grace window (the misclassification class that once deleted 6 live documents) and is refused with the verification path logged (`nx catalog doctor --t3-vs-catalog`, `nx catalog reconcile`); `NX_GC_FORCE=1` overrides after confirming. Ordinary mass updates (a large `git pull` superseding much of a repo) quarantine silently and expire quietly — no warnings, no action needed. Every move logs a per-document identity sample.

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

`--all-collections` runs the audit across every physical_collection in the catalog and emits one sorted summary (contaminated first). Use it as a daily or post-release health check to confirm the register-time guard (see [Catalog](catalog.md#cross-project-source_uri-guard-nexus-3e4s-nexus-e7cys)) is preventing new contamination. The sweep is read-only. `--purge-non-canonical` and `--canonical-home` are per-collection contexts and raise a usage error when combined with `--all-collections`.

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

**Deprecated alias** for `nx catalog generate-links` (nexus-2297) — prefer
the canonical command below. It delegates in full, so `--dry-run` here gets
the same real preview described there. Use for initial setup or after bulk
imports; normal index runs are incremental.

### nx catalog generate-links

```
nx catalog generate-links [--citations/--no-citations] [--filepath/--no-filepath]
                          [--prose/--no-prose] [--pdf/--no-pdf] [--dry-run]
```

Auto-generate typed links from metadata cross-matching. Four generators, all
enabled by default: `--citations` (citation links from bibliographic
metadata), `--filepath` (RDR-to-code links by file path), `--prose`
(prose/markdown filepath links) and `--pdf` (PDF corpus links).

`--prose` and `--pdf` were reachable at index time but not from this command
until 7.16.4 (nexus-glivh); before that a full pass here understated itself by
103 of its 133 links. Pass `--no-prose` / `--no-pdf` to restore the older,
narrower scope.

`--dry-run` reports what a pass WOULD create — per generator, by link type, by
creator, and source fan-out — and writes nothing. The preview runs the SAME
generators through a recording writer rather than a separate preview path, so
it cannot drift from the write path. Counts mean "would create", never "would
attempt": pre-existing links are deduped against and repeated proposals within
a run collapse. A generator that fails to preview reports UNKNOWN, never zero.

Measure before writing. This generator's failure mode is VOLUME — the
implements-heuristic flood had to be disabled engine-side after exactly one
unmeasured pass.

### nx catalog update

```
nx catalog update [TUMBLER] [--title TEXT] [--author TEXT] [--year N] [--corpus TEXT] [--meta JSON] [--source-uri URI] [--file-path PATH]
nx catalog update --owner PREFIX --corpus TEXT    # batch update all entries under an owner
nx catalog update --search QUERY --corpus TEXT    # batch update all entries matching search
```

Update catalog entry metadata. `TUMBLER` accepts a tumbler or title. Batch mode uses `--owner` or `--search` to update multiple entries at once.

`--source-uri` sets or replaces the catalog identity URI — recovery path for
entries whose DT-URI stamp failed during `nx dt index` (the entry carries
`source_uri=file://…` instead of `x-devonthink-item://<UUID>`), or for
manual reassignment of catalog identity. Validated against the same scheme
allowlist as register-time.

`--file-path` sets or replaces the `file_path` column (nexus-y8qtj) —
repoints an entry whose recorded path is dead (moved/renamed on disk)
*without* touching its `source_uri` identity; the two are separate columns
updated independently.

### nx catalog gc

```
nx catalog gc                          # report-only (default is --dry-run)
nx catalog gc --no-dry-run --confirm   # actually delete
```

Remove orphan catalog entries (entries with `miss_count >= 2`, i.e. missed in 2 consecutive index runs). Double-gated like `nx t3 gc`: report-only by default; both `--no-dry-run` AND `--confirm` are required to actually delete — `--no-dry-run` alone silently makes no changes (nexus-tnz3: 4.29.1 inverted the default so a forgotten flag no longer silently destroys entries). **Not reversible in-product**: the pre-delete JSONL backup snapshots died with the local catalog in 7.0.0 (nexus-i711w).

### nx catalog list-backups / vacuum-backups (removed)

Removed in 7.0.0 (nexus-i711w) together with the RDR-106 pre-delete JSONL
snapshot machinery: snapshots were written by the LOCAL catalog, which no
longer exists. Historical snapshots under
`$NEXUS_CONFIG_DIR/catalog/.deleted-backups/` remain plain JSONL you can
inspect by hand. Treat the destructive catalog verbs (`delete`, `gc`,
`prune-stale`, `link-bulk-delete`) as **not reversible in-product**.

### nx catalog remediate-paths

```
nx catalog remediate-paths SOURCE_DIR [--dry-run] [--collection NAME] [--owner PREFIX]
                           [--prefer-deepest] [--mark-missing] [--extensions LIST] [--rdr-prefix-mode]
```

Repair catalog entries whose `file_path` is a bare basename or points at a file that no longer exists on disk. Walks `SOURCE_DIR`, indexes files by basename (default extensions `.pdf,.md,.markdown`; `--extensions '*'` matches everything), and updates each remediable entry to the absolute path of its unique basename match. Use after moving PDFs from `~/Downloads` into a papers archive, or any time the original ingest paths went away.

- Entries whose `meta` carries a `devonthink_uri` are resolved via DEVONthink first (macOS only); DT's answer is authoritative when the reported path exists.
- Multiple basename matches are ambiguous and skipped by default; `--prefer-deepest` breaks ties by longest path.
- `--rdr-prefix-mode` adds a fallback for RDRs renamed end-to-end: when basename match fails, match by the durable `rdr-NNN-` prefix instead.
- `--mark-missing` sets `meta.status='missing'` on entries with no candidate, so `nx catalog gc` can sweep them.

Idempotent: re-running on the same `SOURCE_DIR` is a no-op once entries are resolved. Empty-`file_path` entries (MCP-stored knowledge with no source file) are never touched.

### nx catalog prune-stale

```
nx catalog prune-stale [--collection NAME] [--owner PREFIX] [--source-dir DIR] [--no-dry-run --confirm]
```

Drop catalog entries whose `file_path` is missing on disk. This is the supported stale-content sweep: `nx t3 prune-stale` was retired in 7.0.0 (nexus-bm8dd), and this verb followed by `nx t3 gc` replaces it. Pairs naturally with `remediate-paths`: run the remediator first to repair what's recoverable, then prune the rest. Default is report-only; both `--no-dry-run` AND `--confirm` are required to delete. **Not reversible in-product** (the pre-delete snapshots died with the local catalog, 7.0.0/nexus-i711w).

Never deleted: entries with empty `file_path` (MCP-stored), basename-only paths (remediable, not stale), paths that exist, relative-path entries whose owner has no `repo_root` (presence cannot be verified; repair the owner first), and, when `--source-dir` is set with `--rdr-prefix-skip` (the default), RDR entries whose `rdr-NNN-` prefix matches a file under the source dir (a plausible rename; prefer remediation over destructive prune). Relative paths are resolved against the owner's `repo_root`, not the cwd (nexus-6ims).

### nx catalog link-density

```
nx catalog link-density [--by-collection/--no-by-collection] [--sample N] [--depth N] [--threshold N]
```

Per-collection report of outgoing-link counts at the depth-N BFS frontier (default depth 2). For each `physical_collection`, samples up to `--sample` seed tumblers (default 50, capped to keep latency bounded) and runs a depth-`--depth` BFS from each. Output: one row per collection with `frontier_p50`, `frontier_p90`, and the set of `link_types` that fired during traversal. Introduced 4.18.0 (RDR-097, `nexus-8el5`) as observability for the hybrid retrieval plan: a collection with median frontier below `--threshold` (default 3) is flagged as a poor candidate for `hybrid-factual-lookup` — graph traversal adds latency with little recall gain there, and vector-only retrieval is the better choice. `--by-collection` is the only implemented mode today; `--no-by-collection` prints a "not yet implemented" message (the flag exists as a stable placeholder for a future global rollup). The CLI is observability only; it does not auto-rewrite plans. No `--json` output.

### nx catalog list / stats / owners / delete

```
nx catalog list [--owner PREFIX_OR_NAME] [--type TEXT] [-n/--limit N] [--offset N] [--json]
```

| Flag | Description |
|------|-------------|
| `--owner` | Filter to one owner. Accepts a dotted tumbler (`1.2`) or an owner name (resolved via catalog lookup); ambiguous names across multiple owners raise a clean error naming the candidates |
| `--type` | Filter to a `content_type` (e.g. `code`, `rdr`, `knowledge`) |
| `-n`, `--limit` | Page size. Default `50`. **Server-side cap (nexus-xoimv)** — the underlying query is limited at the source, not truncated client-side after a full fetch |
| `--offset` | Skip this many entries (pagination) |
| `--json` | Emit entries as a JSON array instead of text rows |

The `Next page: --offset N` hint printed when more rows exist means "more entries exist beyond this page," not a total count — `--limit` bounds what the server returns, so the total catalog size is not implied by a single page.

`stats`, `owners`, and `delete` remain standard catalog management. Run `nx catalog COMMAND --help` for details.

### nx catalog owners --census

```
nx catalog owners [--include-deactivated] [--json]
nx catalog owners --census [--include-deactivated] [--json]
nx catalog owners --census --execute deactivate [--no-dry-run] [--confirm]
nx catalog owners --execute reactivate --owner <tumbler_prefix> [--no-dry-run] [--confirm]
```

Read-only classification of every registered repo owner's on-disk `repo_root` (nexus-7kl32): `healthy` (root exists, has content), `path_vanished` (root does not exist at all — the bench-index-sandbox / throwaway-probe-checkout / stale-worktree debris population), `path_exists_empty` (root is still there but has been emptied out), or `unreadable` (existence/contents could not be confirmed, e.g. a permission error — deliberately never folded into `healthy`, same honesty principle as `nx doctor`'s git-hooks fix below). Owners with no `repo_root` set are reported separately and excluded from all four buckets. Since round 3 (T2 21467), classification only ever runs against the CURRENTLY ACTIVE owner set — deactivated owners are excluded from these buckets on every pass (they were already handled) and, if `--include-deactivated` is not passed, only a hidden-count hint is printed. The classification call itself now always fetches the full (active + deactivated) set in one round trip, both to support `--include-deactivated` and to compute the engine-capability signal below without a second call.

`--execute deactivate` (nexus-cw262) is the mutation arm: it soft-deletes (`catalog_owners.deactivated_at`) the `path_vanished` owners that also have **zero live documents attached** (corroborated via a `by_owner` lookup per candidate — a transiently-unmounted path with real, still-queryable documents is excluded with a named reason, never silently deactivated; this corroboration adds one `by_owner` call per `path_vanished` candidate to the census, so a run with a large dead-owner population is proportionally slower than the pre-cw262 census-only pass — a deliberate cost for the correctness it buys). `path_exists_empty` and `unreadable` owners are NEVER mutation-eligible, regardless of flags. Requires `--census`; double-gated exactly like `nx catalog reconcile-stale` — a bare `--execute deactivate` (or `--no-dry-run` alone) reports only, and only `--no-dry-run --confirm` together actually deactivate. `--json` cannot be combined with `--execute`.

Immediately before EACH write, the execute loop RE-VERIFIES that specific owner's eligibility (path re-classified as `path_vanished`, `by_owner` re-counted as zero) — the same "classification invariant re-check" discipline `nx catalog reconcile-stale`'s tombstone arm uses. An owner whose state changed between the census pass and its turn in the write loop (a long candidate list, a path that remounted, a document that got registered in the interim) is skipped with a named reason, never deactivated on stale evidence.

**THE RESIDUAL.** `catalog_owners` carries no `created_at`/`updated_at` column, so a genuinely healthy but transiently-unmounted owner (a network volume, an external drive, a container mount that is simply not attached right now) with zero currently-registered documents is **wire-indistinguishable** from actual debris — both read `path_vanished` + `doc_count=0`. This cannot be closed by more corroboration signal (there is no timestamp to age-gate against); it is bounded and made honest instead: every eligible candidate row states the residual explicitly in both the human report and `--json`'s `mutation_eligible[].residual_note`; deactivation is reversible (see `--execute reactivate` below, plus the automatic reactivate-on-reregister); and `--include-deactivated` keeps deactivated owners auditable rather than silently gone.

`--execute reactivate --owner <tumbler_prefix>` (nexus-cw262 round 3) is the undo affordance: it clears `deactivated_at` for ONE named owner. No `--census` needed — pass the exact target and the same `--no-dry-run --confirm` double-gate. This is the manual-recovery path alongside the automatic one: any live re-registration of the same owner (e.g. `nx index repo` on a remounted/re-cloned path) clears the flag automatically too, with no separate command needed — `--execute reactivate` exists for the case where re-registration isn't the fastest fix (the path is still down, or Hal just wants to undo a batch deactivate immediately).

`--include-deactivated` (nexus-cw262 round 3) surfaces owners that were deactivated — a past `--execute deactivate` is not a silent permanent exclusion; it is auditable state. Works on both `nx catalog owners` (plain listing) and `--census`.

The mutation arm's availability depends on which engine build the tenant is connected to. `nx catalog owners --census`'s `--json` output reports `mutation_status` as `"available"`, `"unavailable"` (the connected engine confirmed NOT to carry the nexus-cw262 owner-deactivate route — e.g. the live cloud engine before its next tag deploys), or `"unknown"` (no owners to read the signal from either way). The human report and `nx doctor`'s git-hooks fix_suggestion word themselves accordingly — neither ever claims `--execute deactivate` works against an engine confirmed not to support it.

`nx doctor`'s git-hooks check (a `path_vanished`/`path_exists_empty` owner used to render there as a signal-free `ok=True` "could not check") points a dead CATALOG owner at this verb's `--execute deactivate` arm, capability-qualified as above — but a dead owner registered only in the legacy `repos.json` file is invisible to this census (catalog owners only); doctor's suggestion for that case is to edit `repos.json` directly instead.

### nx catalog backfill-owner-id (removed)

Removed in 7.0.0 (nexus-i711w). The one-time RDR-137 P1.5a migration wrote
through a raw SQLite handle on the local catalog, which no longer exists;
it already refused in service mode, now the only mode.

### nx catalog backfill-source-uri

```
nx catalog backfill-source-uri [--apply] [--json]
```

Re-derives `chroma://` catalog `source_uri` values for filesystem-backed collections (nexus-poigc). `chroma://` is ALSO the internal store_put-origin marker for `knowledge__` documents minted via `nx store put` / MCP `store_put` (see `_STORE_PUT_URI_PREFIX` in `commands/catalog_cmds/reconcile_stale.py`) — unrelated to the retired ChromaDB dependency (RDR-155 P4b) and left alone by this verb. It ONLY rewrites rows whose physical collection routes to `file://` in `nexus.aspect_readers.uri_for` (the `rdr__`/`docs__`/`code__` prefixes): those rows carry a `chroma://` identity solely because an older `uri_for` build minted it before those prefixes routed to `file://` — migration residue, not a legitimate origin marker.

For each candidate, parses the path component out of the stored `chroma://<collection>/<path>` URI and re-derives through the CURRENT `uri_for(collection, path)`. Refuses (reports, never writes) a row whose parsed path is not absolute — there is no `repo_root` to anchor a relative one at backfill time, and this verb never guesses at one. Always prints a per-collection census of every `chroma://` row first — candidates (file-routed) vs. store_put-origin markers (left alone) — regardless of `--apply`.

| Flag | Description |
|------|-------------|
| `--apply` | Perform the rewrite (default: dry-run report only) |
| `--json` | Emit JSON instead of the human-readable report |

### nx catalog backfill-collections

```
nx catalog backfill-collections [--dry-run]
```

Populate the RDR-101 Phase 6 collections projection from existing state. Walks both the live T3 vector store and the catalog `documents.physical_collection` column, unions the two sets, and registers each name not already in the projection. The projector's `is_conformant_collection_name` regex decides each row's `legacy_grandfathered` flag automatically.

Idempotent. Conventional first invocation is `--dry-run` for operator review, then `--no-dry-run` to apply.

### nx catalog collection-name

```
nx catalog collection-name --content-type code|docs|rdr|knowledge [--repo DIR]
```

Resolve and print the canonical conformant T3 collection name for a content type in a repo (default: cwd). Output is a single line, suitable for shell substitution:

```
nx store put --collection "$(nx catalog collection-name --content-type knowledge)" ...
```

Requires an initialized catalog AND a registered owner for the repo (the indexer's `_catalog_hook` registers it on first `nx index repo`); errors with that remediation otherwise. Plugin-layer call sites (rdr-close post-mortem archival, rdr_hook status reporting) use this instead of constructing the legacy 2-segment shape by hand.

### nx catalog collection-gc

```
nx catalog collection-gc [--apply]
```

Sweep zombie T3 collections, the junkyard pattern flagged by `nx catalog doctor --collections-drift`: collections pre-created by an interrupted index or deleted worktree (`get_or_create_collection`) that never received a chunk. Conservative: a collection is deleted only when it has 0 chunks AND is not in the collections projection AND is not referenced by any `documents.physical_collection` row AND is not bypass-schema (`taxonomy__*`). Dry-run by default; `--apply` deletes. Stale projection rows (row exists, T3 collection gone) are NOT handled here (the event log is append-only), so those need an explicit `supersede_collection`; use the recipe printed by `nx catalog doctor --collections-drift`.

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

The old name survives the rename as a **superseded tombstone**: its projection row
stays, carrying `superseded_by` = the new name and a `superseded_at` timestamp, so a
rename remains auditable after the fact. It holds no chunks or documents (everything
re-homes onto the new name) and `superseded_by` keeps it out of collection-for-tuple
resolution, so it can never be picked as a write target. Renaming *back* onto a
tombstoned name revives it. Before 7.0.0 the row was deleted instead, which silently
discarded the rename's history (nexus-cecqy).

### nx catalog migrate-fallback

```
nx catalog migrate-fallback SOURCE [--target-model M] [--target-version v1] [--dry-run/--no-dry-run] [--yes]
```

Walk a fallback collection (`docs__default`, `knowledge__knowledge`, etc.) and propose a per-document target conformant collection. With `--yes`, re-points each document's `physical_collection` in the catalog and auto-registers the target rows in the projection. Fallback collections are deprecated when the migration empties them (single-target case auto-emits `CollectionSuperseded`); never silently nuked, per RDR-101 §"Phase 6".

Target form: `<content_type>__<owner>__<model>__<version>` where content_type comes from the source's prefix, owner comes from each tumbler (`1.5.42` → `1-5`; tumbler dots become hyphens for ChromaDB's name regex), model defaults to the source's Voyage family.

T3 chunks are NOT moved by this verb. Operators repopulate the target via `nx index` on the source files; old chunks become orphans (catalog now points elsewhere) and get swept by `nx t3 gc` on the next cycle.

### nx catalog doctor

```
nx catalog doctor [--collections-drift] [--chunk-size-distribution] [--chunk-text-dedup]
                  [--t3-vs-catalog] [--name-vs-embed-dim] [--store-put-integrity]
                  [--link-coverage] [--json]
```

RDR-101 catalog doctor surface; pass at least one check flag.

> `--replay-equality`, `--t3-doc-id-coverage` and `--strict-not-in-t3` were removed in 7.0.0. All three read their expectations out of the local `events.jsonl`, and replay-equality diffed a projection rebuilt from it against the local `.catalog.db` — artifacts that do not exist in service mode, where the catalog is owned by the nexus service. They already refused there; they are now gone along with the local catalog.

- `--collections-drift`: every T3 collection and every distinct `documents.physical_collection` has a row in the collections projection (Phase 6 release gate).
- `--chunk-size-distribution`: per-collection chunk-size stats (p50/p95/p99/max); FAIL on any chunk over `MAX_DOCUMENT_BYTES`, WARN when >5% of chunks are micro-chunks (<100 bytes).
- `--chunk-text-dedup`: chunk-text-hash duplication ratios — within-collection dupes >5% signal a chunker bug; >100 cross-collection dupes flag a cross-ingest investigation lead.
- `--t3-vs-catalog`: projection-vs-T3 triage — T3 collections with no LIVE catalog documents (orphan or tombstoned-only), projected collections with 0 chunks (zombie), and catalog documents whose `physical_collection` is gone from T3. The orphan half is a SHARED classification (nexus-8tnz2, `classify_t3_orphan_collections`) — `nx catalog verify`'s `orphan_collections` field and `nx catalog reconcile-stale --execute drop-orphan-collections` consume the exact same function, so all three agree by construction. It splits by class: `t3_orphans` (zero live AND zero tombstoned docs — genuine debris, counts toward FAIL) vs `t3_tombstoned_only` (zero live docs, but 1+ tombstoned/restorable docs, RDR-156 D6 — reported separately, never FAILs, never a `drop-orphan-collections` delete target).
- `--name-vs-embed-dim`: samples one chunk per conformant collection and compares the actual embedding dimension to the one implied by the collection name's `__<model>__` segment; FAIL suggests `nx collection rename` (cosmetic, no re-embed).
- `--store-put-integrity`: store_put-origin integrity (nexus-b6enc, GH #1419 Issue 8) — for `content_type='knowledge'` documents with no `file_path`: FAIL on `chunk_count` != manifest-row count (drift) and on ghosts (catalog row with zero manifest rows AND zero T3 chunks), reporting title + tumbler so the content can be re-created while it is still remembered.
- `--link-coverage` (7.16.4, nexus-glivh): per-content-type link coverage. FAILs only on a content type holding at least 10 documents with ZERO links — which means no generator has ever run for that type, not that its documents are unrelated. Percentages are reported and never flagged: a measured full generator pass over this corpus yields 133 links, so a low percentage is the natural ceiling of filepath extraction and no available remedy moves it. Index-time generation only links NEWLY registered tumblers, so a corpus can sit permanently unlinked with every other check green. Remediate with `nx catalog generate-links --dry-run` first.

Returns non-zero on any check failure. `--json` emits the per-check result for CI consumption.

### nx catalog verify

```
nx catalog verify [--collection NAME] [--heal] [--json]
```

Reconcile the catalog against T3 on the RDR-108/180 chash identity (rebuilt by nexus-sj4a3; the pre-rebuild version keyed on the retired pre-RDR-108 `meta.doc_id` and had collapsed to near-zero coverage). Every check walks `tumbler -> document_chunks.chash -> T3 chunk id`.

**RDR-191 Phase 6 (nexus-o8dil.33), 2026-08-15: full mode's damaged-manifest finding class (formerly "Class B", one collection-granular engine round trip via `manifest_verify_all()`) is RETIRED.** The manifest-chunk FK (`catalog_document_chunks -> nexus.chunks`, VALIDATEd) now REJECTS a dangling manifest row at write time, so the state that class detected is unreachable by construction — full mode's `damaged`/`damaged_collections` in the `--json` payload are always `[]`/`0`, permanently. Full mode still detects the two classes below; damaged-manifest detection survives ONLY in `--collection` scoped mode, computed independently and client-side (never via the retired `manifest_verify_all()`):

- **vanished collections** — a `physical_collection` with catalog docs that T3 no longer knows about at all (deleted, renamed). FINDING, both modes.
- **lost documents** — `chunk_count > 0` but the manifest has fewer rows than that (including none). FINDING, both modes.
- **damaged manifests** (`--collection` scoped mode ONLY) — a document's manifest references chashes T3 does not have, reported per DOCUMENT via a direct `get_manifests` + T3 `existing_ids` read (no engine-side anti-join function involved). Full mode never reports this — see above.
- **never-chunked** — `chunk_count == 0` and no manifest, split into `rdr145_exempt` (`knowledge__*` store_put notes with no file_path/source_uri whose chunks ARE in T3 — a manifest-only gap, legitimate by design and backfillable via `nx t3 backfill-manifest --dry-run --only-gapped`), `rdr145_ghost` (7.21.0, nexus-1uekf: the same shape with NO chunk in T3 under the note's chash or title — nothing to backfill, the title is the only surviving record; `tumblers` listed, and `nx catalog doctor --store-put-integrity` reports the same population by the same lookup), `rdr145_unverified` (a count of exempt-shaped notes whose T3 probe failed; counted as exempt, never as ghosts), `zero_content_by_design`, and `unclassified` (candidate data loss, see nexus-cdypx and `nx catalog reconcile-stale`). Before 7.21.0 the exempt class was a pure shape test that rendered "legitimate by design" over 226 rows whose content was gone. In full-catalog mode the split is decided by a live T3 lookup per exempt-shaped row; in `--collection` scoped mode, or when T3 is unreachable, the shape class stands alone. Report-only; never affects the exit code. Both modes.
- **ghosts** (full mode ONLY, nexus-xeux8) — a read-only CENSUS of documents with a blank/NULL `physical_collection`. This population is dropped outright by BOTH `verify`'s own health classification above (no owning-collection identity for a chunk_count-vs-manifest comparison to mean anything) AND by `nx catalog reconcile-stale`'s candidate filter — so before this section, nothing sized it at all. Computed from the SAME full-catalog document sweep `verify` already does for the classes above (no extra engine round trip, so it stays cheap even when the surrounding sweep is already minutes-scale). Reports `count`, a `by_owner` breakdown (tumbler 2-segment owner address), `by_tenant` (`{"available": false, "reason": ...}` — this client's reads are already single-tenant scoped via RLS and `CatalogEntry` carries no per-row tenant id to break out by even if it were), and a capped `sample_tumblers` list (with `sample_truncated` when the population exceeds the cap). A ghost is UNREPAIRABLE without a manual `physical_collection` assignment — this section never changes what `verify`/`reconcile-stale` repair, and it NEVER affects the exit code or the `docs`/`never_chunked_docs`/etc. counts above. Absent entirely in `--collection` scoped mode (a ghost has no collection to scope into by definition).
- **orphan collections** (full mode ONLY, nexus-8tnz2) — the reverse-direction sibling of ghosts: T3 collections with chunks but ZERO LIVE catalog documents referencing them. Same shared classification `nx catalog doctor --t3-vs-catalog` reports as `t3_orphans`/`t3_tombstoned_only` (`classify_t3_orphan_collections`) — one definition, three consumers. Each row carries a `class`: `"orphan"` (zero live AND zero tombstoned docs — genuine benchmark/gate debris, `{name, chunk_count, class}`), `"tombstoned-only"` (zero live docs but 1+ tombstoned/restorable docs, RDR-156 D6 — `{name, chunk_count, class, tombstoned_count}`, NEVER a `drop-orphan-collections` delete target), or `{name, error}` when the T3 read failed. Count-only in the summary, like ghosts — NEVER affects the exit code; act on `class=="orphan"` rows via `nx catalog reconcile-stale --execute drop-orphan-collections`.

Exit code: 0 when clean (never-chunked, ghosts, and orphan collections alone still exit 0); 1 on any vanished/lost finding (plus damaged, in `--collection` mode). A check or collection that could not be read at all (degraded T3, pre-fence engine, un-backfilled manifest rows, or a failed orphan-collection classification) is INCOMPLETE, not clean — that raises a distinct, louder error regardless of findings.

`--json` is the CI contract: `{"summary": {...}, "vanished_collections": [...], "damaged": [...], "lost": [...], "never_chunked": {...}, "unreadable": [...]}` (plus `"unverifiable_rows"` when present — full mode's `unverifiable_rows` is also retired alongside the damaged-manifest class it cross-checked, so this key is now permanently absent/empty for full mode; `"ghosts": {...}` plus `summary.ghost_docs`, full mode only; and `"orphan_collections": [...]` plus `summary.orphan_collections`, full mode only — see above). Full-catalog mode is cheap and meant to gate CI; `--collection` mode trades that cheapness for per-document detail, including the ONLY surviving damaged-manifest detection. `--heal` (requires `--collection`, incompatible with `--json`) prompts per damaged document: drop the tumbler, or print the `nx store put` invocation that would repopulate it.

```
nx catalog verify                                  # full sweep (CI)
nx catalog verify --json                           # CI-friendly output
nx catalog verify --collection knowledge__foo      # per-doc detail
nx catalog verify --collection knowledge__foo --heal   # interactive fix
```

### nx catalog reconcile-stale

```
nx catalog reconcile-stale [--execute recount|tombstone-vanished|tombstone-orphaned|tombstone-zero-content|tombstone-ghost-notes|drop-orphan-collections] [--dry-run/--no-dry-run] [--confirm] [--json]
```

Classify — and optionally repair — catalog documents with unreliable `chunk_count`/manifest state (nexus-cdypx: 61.2% of production catalog docs carried `chunk_count == 0`, so catalog-aware routing ranked over a corpus where most docs had no retrievable content). The default invocation is a pure read-only census: it constructs NO catalog writer. Exit 0 means the report was produced; a nonzero exit (the INCOMPLETE guard shared with `nx catalog verify`) means part of the classification could not be trusted and none of the findings should be acted on. This command is not itself a correctness gate over the findings — `nx catalog verify` is that gate.

**Substrate anchor (7.22.0, nexus-cwhci).** The first line of every census is the non-vacuity check the shakedown playbook's §S4 calls for: `Substrate anchor: OK — the engine counts N live catalog document(s) and this walk saw N (…)`. `N` on the engine side is `catalog_stats.doc_count` — one server-side SQL `count(*)` over `catalog_documents WHERE deleted_at IS NULL`, served by `GET /v1/catalog/stats` — reachable on a cloud-managed box with the store's own credentials; no `psql`. The walk side is every row `all_documents` paged through (aliases and rows without a collection are counted here and then excluded from the "examined" total). The walk is bracketed by a count taken before it and one after it, so writes that land during the walk on a busy box are corroborated (`moved_during_walk`), not mis-read as a probe failure. `MISMATCH` (the walk fell outside both counts) and `UNAVAILABLE` (the reader cannot report a count) both print the census and then exit non-zero as INCOMPLETE — a census the substrate does not corroborate is a probe failure, unverifiable is never a pass, and every `--execute` arm refuses on the same guard. **What it proves and what it cannot:** the aggregate and the walk are independent mechanisms, so agreement proves the walk saw every row the engine serves this caller (paging, truncation, swallowed reads); they share the caller's tenant scope and RLS, so a document hidden by scope is invisible to both — that class (the playbook's S4b) still needs a server-operator count. `--json` carries `substrate_anchor` (`status`, `substrate_doc_count`, `substrate_doc_count_before`/`_after`, `walked_docs`, `delta`, `moved_during_walk`, `reason`) plus `walked_docs`, `alias_docs`, `no_collection_docs`.

**Write-time guard census (7.22.0, nexus-41zr9; updated nexus-8tnz2).** Each mutation arm opens with `Write-time guard (playbook §5.4, as of <date>): <status> — <guard> [<where>]` naming the guard that prevents the population it is about to clean from recurring: `shipped`, `shipped-with-residuals` (with the residual beads listed), or `UNGUARDED`. `recount` is UNGUARDED (the chunk_count desync writer it repairs after, nexus-wu8s1, is still unfound). `tombstone-vanished` and `drop-orphan-collections` are now `shipped-with-residuals`, and the guard text is explicit that the scope is PARTIAL: `tests/test_host_harness_scratch_scope_lint.py` (nexus-8tnz2) prevents THIS repo's own tracked host-run harnesses from indexing/storing debris into a service — an exact, reviewed allowlist naming why each existing site is safe (read-only, container-isolated, `NX_LOCAL` under a sandboxed HOME, or the throughput bench's marker-scoped-owner + before/after-snapshot + EXIT-teardown shape) — plus the cascaded API delete/rename path. It does NOT address the observed live population's actual root cause: the design-of-record found no producer of the real debris names (`code__test-repo-<hex>`, `docs__hotfix_smoke`, `docs__local_smoketest_336`, `knowledge__val530`, `docs__1-2188`) anywhere in this repo — that producer is external and remains completely unguarded. `drop-orphan-collections` (below) is the sweep that cleans the SYMPTOM regardless of source — already-landed debris, or whatever the still-unidentified external producer adds next. The table in `reconcile_stale.py` is the record (the playbook points at it rather than restating it); the printed `as of` date says how old its claims are, and the shakedown's instrument-freshness census is what catches a row that has rotted. The census `--json` carries the table as `write_time_guards` plus `write_time_guards_as_of`.

Six mutation arms, each printing the classification report first, then its own target list, then acting only with `--no-dry-run --confirm` (the same double gate as `purge-trash`):

- **recount** — resync `chunk_count` for zero-count docs whose manifest is actually non-empty. Restores the COUNT, not verified content; re-run `nx catalog verify` afterward.
- **tombstone-vanished** — delete zero-manifest docs in vanished collections. Non-empty-manifest vanished docs are NEVER touched by this arm (nexus-3ck2g).
- **tombstone-orphaned** — delete zero-count docs whose confirmed on-disk location is gone (file missing, or the owner's repo_root/worktree itself deleted). Docs whose absence could not be CONFIRMED (no repo_root, malformed tumbler, a non-file source_uri, or no provenance at all) are never in this arm's target set — see `unresolvable_provenance` in the report. `store_put_origin` docs (see below) are also never in this arm's target set.
- **tombstone-zero-content** (nexus-rqsh1) — delete docs classified `zero_content_by_design`: the source file verifiably CAN never chunk (zero bytes by `stat`, or binary content by the same `looks_like_binary_content` sniff the indexer's registration guard uses). These docs never drain via re-indexing (the producer no longer registers such files at all), so without this arm they reappear in every census forever. A source that is merely unreadable (permission error) or missing is NEVER classified into this bucket — absence of proof is not proof of zero content. The bucket also appears as its own count + sample listing in `nx catalog verify` and this census, and stays counted until actually tombstoned (an honest bucket, not a suppression).
- **tombstone-ghost-notes** (7.21.0, nexus-1uekf) — delete store_put-origin notes whose content is gone from T3. Candidates are every zero-count row that is *note-shaped* (no `file_path`, a recorded chash — the same predicate `nx catalog doctor --store-put-integrity` scans by), from any `zero_count_*` bucket and any collection; which collection a note was put in says nothing about whether its content survives. Each candidate is then re-proved **per row at execution time**: the manifest is still empty (the invariant re-check every tombstone arm runs) AND T3 has no chunk under the note's chash OR its title (`note_chunks_present`, the lookup `verify` and `--store-put-integrity` report by). A note whose chunks are present under either key is a manifest-only gap (a `nx t3 backfill-manifest` candidate) and is never tombstoned; a row whose T3 probe fails is skipped and named, never tombstoned. Reversible until `purge-trash`. First production run 2026-08-28: 226 + 2 tombstoned, one live note correctly skipped; `--store-put-integrity` went from 228 ghosts to 0.
- **drop-orphan-collections** (7.22.0, nexus-8tnz2) — delete whole T3 collections that have chunks but ZERO catalog documents — live OR tombstoned — referencing them (benchmark/gate debris — the reverse direction of `tombstone-vanished`). Consumes the SAME classification `nx catalog doctor --t3-vs-catalog` reports as `t3_orphans` and `nx catalog verify` reports as `orphan_collections` (`classify_t3_orphan_collections` — one definition, three consumers). Only `class == "orphan"` (zero live AND zero tombstoned docs) is ever a delete target: a `class == "tombstoned-only"` collection (every referencing catalog document is soft-deleted, still restorable until `purge-trash`, RDR-156 D6) is listed distinctly with its `tombstoned_count` and NEVER dropped, and a collection whose chunk count OR tombstone count could not be read from T3 is never a delete target either — an unavailable tombstone-disambiguation read refuses the whole `--execute` as INCOMPLETE rather than guess. Drops go through the cascaded API delete path (`HttpCatalogClient.delete_collection`), never a raw vector-store delete, never `psql`. The tombstone/all-rows count requires an engine serving `GET /v1/catalog/docs/collection-counts-all` (nexus-8tnz2 fix-round-2 — a brand-new route, not a query param on the pre-existing `/docs/collection-counts`, so an older engine 404s cleanly instead of silently returning live-only data); on an older engine the arm reports INCOMPLETE and refuses `--execute`.

The `dishonest` bucket (`chunk_count > 0` but the manifest is empty; diagnosis only, never auto-swept per nexus-wq1e4) carries an `origin` field per document (nexus-0y0gk), checked in this order:

- **`store_put_origin`** (critique fix-round, nexus-0y0gk) — the nexus-sdp0u store_put signature: `reason: "chroma_uri"` when `source_uri` is the synthesized `chroma://<collection>/<title>` identity, or `reason: "knowledge_single_chunk_no_path"` when `chunk_count == 1`, the collection is `knowledge__*`, and BOTH `file_path`/`source_uri` are empty (store_put docs are single-chunk by construction). This is a RECOGNIZABLE, likely FK-safe `nx t3 backfill-manifest --only-gapped` candidate — NOT "cannot confirm, leave it" — so it must not collapse into `unresolvable_provenance`. A real `file_path` always wins as `reindex_candidate` instead (never silently reclassified).
- **`reindex_candidate`** — a concrete on-disk location resolves and exists.
- **`orphaned_path`** — confirmed absent (plus `reason`/`resolved_path`/`file_path`).
- **`unresolvable_provenance`** — absence could never be confirmed (plus `reason`).

This makes the triage between file-backed re-index, store_put-origin backfill, and genuinely-unknown provenance mechanical instead of a hand-run SQL query. `--json` also carries `dishonest_by_origin`, a count-by-origin summary alongside the existing `dishonest` list.

The zero_count triage gets the SAME `store_put_origin` treatment: a `zero_count_store_put_origin` bucket (parallel to `zero_count_reindex_candidate`/`zero_count_orphaned_path`/`zero_count_unresolvable_provenance`) holds zero-chunk-count `knowledge__` store_put docs carrying the synthesized `chroma://` `source_uri` (`reason` is always `"chroma_uri"` here — the single-chunk sub-signature requires `chunk_count == 1`, unreachable at `chunk_count == 0`). Formerly folded into `zero_count_unresolvable_provenance`/`source_uri_only`. `--json`'s `zero_count_live` block carries `store_put_origin` (the row list) and `store_put_origin_by_reason` alongside the existing `orphaned_path_by_reason`/`unresolvable_provenance_by_reason` counts, and `summary.zero_count_store_put_origin` is the count.

`--json` emits the full structured classification on stdout (diagnostics on stderr) and refuses to combine with `--execute`.

```
nx catalog reconcile-stale                          # census
nx catalog reconcile-stale --json                   # CI-friendly
nx catalog reconcile-stale --execute recount --no-dry-run --confirm
nx catalog reconcile-stale --execute tombstone-vanished --no-dry-run --confirm
nx catalog reconcile-stale --execute tombstone-ghost-notes             # dry-run plan
nx catalog reconcile-stale --execute drop-orphan-collections           # dry-run plan
```

### nx catalog gc-audit list

```
nx catalog gc-audit list [--collection NAME] [--operation OP] [--limit 50] [--offset 0] [--json]
```

Read the destructive-T3-op audit trail, `nexus.gc_audit` (nexus-jqvzk), newest first. Rows are written by the engine's background reaps (`actor="engine"`: `purge_trash`, `gc_quarantine_orphans`, `gc_expire_quarantine`, the chunk sweep) and, since 7.22.0 (nexus-fduai), by `nx t3 gc` reporting its own client-side delete (`operation=t3_gc`, `actor="nx t3 gc"`). Until this verb the only reader was `nx doctor`'s pass/fail non-empty check. Text mode prints one line per row (id, created_at, operation, actor, dry_run, chash_count, collection); `--json` emits every field including the engine-capped `chashes` list and the producer's `details`. An engine without the route (pre-v0.1.62) is reported as such, never as an empty trail.


```
nx catalog purge-trash [--older-than-days N] [--dry-run/--no-dry-run] [--confirm] [--json]
```

Physically reclaim tombstoned catalog rows and their manifest-orphaned T3 chunks (nexus-3ck2g). `nx catalog delete` soft-tombstones: it stamps `deleted_at` on the catalog row and deliberately leaves the `document_chunks` manifest and the T3 chunk rows in place, so a manual restore stays possible and the engine's own `nexus.purge_trash` orphan predicate (a manifest row exists but no live parent document does) still has something to sweep. This verb is the caller for that engine-side sweep, which previously had none.

Default is a read-only dry-run: a per-dim stranded-chunk count preview plus an aged-tombstone document count (`--older-than-days`, default 30, must be >= 1), computed engine-side and printed. Nothing is deleted in this mode, and `--json` emits the same counts as machine-parseable JSON.

**Age semantics are symmetric since catalog-026 (nexus-5da44, RDR-191 GATE-2; this paragraph described the earlier asymmetric behaviour for two weeks after the engine retired it — nexus-kcm6c):** both the `documents_purged` row delete AND the `chunks_<dim>_stranded` sweep honor `--older-than-days`. A tombstoned document inside the grace window keeps its catalog row, manifest rows, and chunks TOGETHER — the chunk sweep protects any chunk whose manifest row belongs to a live or still-in-window document, the exact complement of the row delete's predicate — and loses all three together once the window passes. "Manual restore stays possible" therefore genuinely holds for the whole window, even across mutating `purge-trash` runs. Consequence for reading the counts: a near-zero stranded count at the default 30 days next to a large one at `--older-than-days 1` means recent tombstones are being protected, by design — the 2026-08-27 shakedown read exactly that pair and concluded the counter was lying when the stale prose was (nexus-kcm6c).

Mutation is gated behind BOTH `--no-dry-run` AND `--confirm` (same gate as `nx catalog reconcile-stale`): `--no-dry-run` alone still reports only, and `--json` cannot be combined with `--no-dry-run` (the mutation path prints a plain-text report, not JSON).

**A partial purge is an error, not a footnote (nexus-ff85q).** The execute report carries `documents_eligible` — the age-gated population the engine measured in the same transaction as the purge — alongside `documents_purged`. Under identical state the two are equal. If fewer documents were purged than were eligible, the command prints the full report (the chunk sweep may have completed) and then exits non-zero naming the shortfall, rather than reporting a bare success. `purge-trash` is idempotent, so re-running is the correct first response. This exists because the first production execute purged 2 of the 63 documents its own dry-run reported and exited 0: the threshold the execute path applied was a calendar month rather than the requested 30 days, and every tombstone in the gap was silently skipped. Both halves are fixed — the interval is now exact days and the preview evaluates its threshold with the same expression and the same server clock as the purge — but the discrepancy check stays as the standing guard.

```
nx catalog purge-trash                                    # dry-run count preview
nx catalog purge-trash --json                              # CI-friendly preview
nx catalog purge-trash --older-than-days 90 --no-dry-run --confirm   # reclaim
```

Unlike `reconcile-stale`, the catalog writer is constructed even for the default dry-run — the count preview is itself computed engine-side via `nexus.purge_trash(dry_run=true)`, not a classification derived from client-side reads. On an engine older than nexus-3ck2g (no `/v1/catalog/purge-trash` route yet), the command raises a clear error naming the required engine release rather than silently no-op'ing.

Note: this verb reclaims storage; it is not the search-visibility fix for a deleted document. On engines carrying the nexus-3ck2g read-side tombstone filter, content stops appearing in search results as soon as `nx catalog delete` tombstones it — independent of when `purge-trash` later reclaims the underlying rows.

**Population (nexus-heizf):** the stranded-chunk count here is EXISTING chunk rows of TOMBSTONED documents with no live parent (direction chunk → parent). RDR-191 Phase 6 (nexus-o8dil.33) retired the instrument this note used to warn against cross-reading (`nx doctor`'s "dangling manifest chashes" warn / `nx catalog manifest-verify --list`, both gone — see [nx catalog manifest-verify — retired](#nx-catalog-manifest-verify--retired)) — the manifest-chunk FK makes that opposite-direction population (LIVE documents' manifest rows with no backing chunk) unreachable, so there is no other instrument left to conflate this one with.

### nx catalog orphan-backfill

```
nx catalog orphan-backfill dt-link COLLECTION [--min-score 0.75] [--owner PREFIX] [--no-dry-run]
nx catalog orphan-backfill synthetic COLLECTION [--owner PREFIX] [--no-dry-run]
nx catalog orphan-backfill dump-csv COLLECTION [--out-dir DIR] [--min-score 0.75]
nx catalog orphan-backfill apply-csv COLLECTION CSV_PATH [--owner PREFIX]
nx catalog orphan-backfill link-existing COLLECTION [--by title|content_hash] [--also-synthetic] [--no-dry-run]
```

Subgroup that backfills catalog Documents for T3 chunks that have no catalog entry. Complementary to `backfill-collections` (which syncs the collections projection) and to the manifest backfill (which writes manifest rows when Documents already exist). All destructive subcommands default to dry-run.

- `dt-link`: walks T3 chunks for the collection, groups by title, fuzzy-matches against DEVONthink via osascript, and registers a Document with `source_uri=x-devonthink-item://<UUID>` per high-precision match (score >= 0.75 by default). Requires DEVONthink running; macOS only.
- `synthetic`: registers Documents with `nx-orphan-backfill://` URIs for chunks `dt-link` cannot claim, populating the manifest without claiming false provenance. Untitled chunks fall back to per-chash singleton Documents.
- `dump-csv`: writes matched / low-confidence / unmatched title CSVs (default under `$NEXUS_CONFIG_DIR/backfill-queue/`) for operator triage; edit `operator_decision` / `operator_dt_uuid` columns.
- `apply-csv`: reads the operator-curated CSV back and registers Documents with the verified UUIDs.
- `link-existing`: links T3 chunks to EXISTING catalog Documents: `--by title` (MCP-style title metadata, e.g. `knowledge__knowledge`) or `--by content_hash` (PDF-shaped chunks matched to `documents.head_hash`). Writes `document_chunks` manifest rows only; `--also-synthetic` falls through to synthetic registration for what remains unlinked.

Owner resolution: `--owner` overrides; otherwise the owner is looked up from the built-in per-collection default map and unknown collections error with instructions.

---

## nx t3

T3 vector-store maintenance commands. As of 6.0 the live T3 store is Postgres 17 + pgvector behind the native nexus-service; these commands operate on that store through the vector client. (`nx t3 reidentify` was the RDR-108 ChromaDB natural-ID migration and is retained for legacy collections.) Distinct from `nx catalog gc`: `nx t3` operates on T3 chunks, the catalog command operates on catalog rows.

### nx t3 prune-stale — RETIRED in 7.0.0

Exits with an error explaining what to run instead. It swept chunks by their `source_path` metadata; RDR-102 D2 removed that key from the chunk schema, so the sweep matched nothing and reported a clean "0 stale" on every collection regardless of how many indexed files had been deleted from disk (nexus-bm8dd). It also resolved paths through the local catalog, which has not existed since 7.0.0/nexus-i711w.

Use the catalog-native pipeline:

```
nx catalog prune-stale [--collection COLLECTION] --no-dry-run --confirm   # drop stale documents
nx t3 gc -c COLLECTION --no-dry-run --yes                                 # collect their chunks
```

Note the flags differ: `nx catalog prune-stale` takes `--collection` (no short form); `nx t3 gc` takes `-c`/`--collection` and **requires** it.

Prune first, GC second. Deleting chunks while their document still references them leaves the dangling manifest `nx doctor` flags (nexus-5xn3k).

`nx t3 gc` **requires** `-c` (the orphan diff is per-collection), so the retired verb's sweep-every-collection mode has no direct replacement — `nx catalog prune-stale` does take all collections, but the GC half must be looped per collection:

```
nx collection list | awk '{print $1}' | xargs -I{} nx t3 gc -c {} --no-dry-run --yes
```

(`nx collection list` is the collection enumerator; `nx catalog list` lists *documents* and emits no collection name.) Tracked as `nexus-iitif`.

### nx t3 gc

```
nx t3 gc -c COLLECTION [--orphan-window 30d] [--no-dry-run --yes]
         [--allow-empty-manifest-set] [--allow-incomplete-index-state]
```

Garbage-collect orphaned T3 chunks via the catalog manifest (RDR-108 Phase 4). A chunk is an orphan when its full `meta.chunk_text_hash` is NOT referenced by any manifest row in the catalog `document_chunks` table for `--collection`, AND its `indexed_at` predates the orphan window (default 30 days). This is the same manifest-vs-T3 comparison the indexer's own `_prune_deleted_files` performs at the end of `nx index`; this CLI is the operator-driven form with explicit dry-run + `--yes` confirmation and `ChunkOrphaned` event emission for the audit trail. `nx t3 gc` is the SOLE emitter of `ChunkOrphaned` events and the SOLE path that physically deletes T3 chunks: the strict per-candidate order is append `ChunkOrphaned(chunk_id, reason)` to the event log, THEN call `T3Database.delete_by_chunk_ids`. A crash between the two leaves the log consistent with T3 (event present, delete pending), and the next run idempotently retries the delete.

Default is report-only; both `--no-dry-run` AND `--yes` are required to actually delete. Chunks missing `chunk_text_hash` (pre-RDR-053 relics — post-Phase-3 chunks have no `doc_id` at all, so that is no longer the skip criterion) are undecidable here and skipped with a warning; re-index the source or run `nx t3 reidentify` to populate the field.

**Audit record (7.22.0, nexus-fduai).** The engine's background reaps (`sweepChunks`, `purge_trash`, `gc_quarantine_orphans`) write their own `nexus.gc_audit` rows server-side; the delete this verb performs is client-side, so the verb reports it through the engine's client-facing producer (`POST /v1/catalog/gc_audit/record`). A successful `--no-dry-run --yes` run records one row — `operation=t3_gc`, `actor="nx t3 gc"`, the full `chashes` list (the engine caps it and keeps `chash_count` exact), and `details` with `deleted`, `requested`, `chunk_ids_sample` (first 50) and `chunk_ids_truncated` — readable with `nx catalog gc-audit list --operation t3_gc`, and mirrors it as a structured `t3_gc_chunks_deleted` log event carrying the same fields plus `gc_audit_id`. If the audit write fails after the delete succeeded the run prints a WARNING and exits 1 (the delete stands; the event carries `gc_audit_error`). Dry runs record nothing.

**Manifest-less notes are protected (nexus-39upx, RDR-145).** A `store_put` / `nx store put` note's chunk never gets a `document_chunks` manifest row — RDR-145 defers manifest-backed identity for notes, and `catalog-003-soft-delete.xml`'s `live_chunks` view treats a manifest-less chunk as live by design. Without a second check that would be indistinguishable from a chash that fell out of a live document's manifest via re-index (both simply read "not referenced"). Before computing orphan candidates, `nx t3 gc` fetches every catalog document registered under `--collection` (one server-scoped `list_by_collection` call) and excludes the chashes of any that are note-shaped (no `file_path`, `meta["doc_id"]` set — the same identity `nx catalog doctor --store-put-integrity` reads). A collection holding protected notes prints `protecting N manifest-less note chunk(s) from orphan classification (RDR-145)`. This lookup is fail-loud, not fail-open — unlike the orphan scan itself, an unverifiable note-set REFUSES the run (exit 1) rather than risk deleting live notes, since this is the operator-driven `--yes` path.

**RUNFENCE index-state precondition (nexus-g6k6b).** A T3 chunk carries no `doc_id` (post-RDR-108), so an orphan candidate cannot be attributed to the specific document that most recently owned it. Since a document mid-reindex (`index_state='indexing'`) or one whose reindex fenced a failure (`'failed'`) may leave a manifest that is a partial, in-progress artifact — and since which candidate belongs to which document cannot be determined — `nx t3 gc` refuses the WHOLE collection's `--no-dry-run --yes` run when it contains ANY document that is not `index_state='complete'` (`'indexing'`, `'failed'`, or explicitly-reported `NULL`; manifest-less notes are excluded from this check — they never carry `index_state` by design and are already, separately, always protected). A pre-RUNFENCE engine that does not report `index_state` at all is floor-tolerated: the check is skipped entirely, matching the RUNFENCE arc's behavior everywhere else. A report line names the count and states when it applies, in both dry-run and a real run.

`--orphan-window` accepts `s`, `m`, `h`, `d`, `w` suffixes (e.g. `30d`, `12h`, `2w`); a bare integer is rejected so a typo cannot silently mean 30 seconds.

`--allow-empty-manifest-set` (nexus-jqrtp) overrides a refusal: when the catalog manifest for `--collection` references ZERO chashes but the collection still holds live chunks, every one of them would read as an orphan candidate — indistinguishable from a fresh/mis-scoped tenant or an unbackfilled manifest. `nx t3 gc` REFUSES (exit 1) in that state unless this flag is passed, pointing at `nx t3 backfill-manifest -c COLLECTION` or `nx catalog reconcile` to investigate first. Only pass it once you've confirmed the collection really is fully orphaned (e.g. a deliberately catalog-less collection).

`--allow-incomplete-index-state` (nexus-g6k6b) overrides the RUNFENCE refusal above. DANGEROUS: only pass it once you've confirmed no reindex is concurrently running against the collection (finish or abandon any in-flight/failed runs first, or accept the risk).

### nx t3 reidentify

```
nx t3 reidentify (-c COLLECTION | --all-collections) [--no-dry-run] [--max-workers N]
```

Re-upsert T3 chunks under content-derived natural IDs, the full `chunk_text_hash` (RDR-108 D1 / nexus-jc63; full-width per RDR-180). Per collection the verb paginates T3 chunks (300/op), computes the new natural ID for each chunk, re-upserts under the new ID using the existing embedding (no Voyage call), and batch-deletes the old chunk IDs after the get-loop completes. Document-level metadata fields (`doc_id`, `chunk_index`, `chunk_count`) are stripped at re-upsert; the `document_chunks` manifest table is now authoritative for those.

The verb is idempotent: re-running on a fully-migrated collection performs zero writes. It is also crash-resumable: re-invoking after an interrupted run safely sweeps the un-deleted old IDs.

Default is `--dry-run` (report-only). Use `--no-dry-run` to perform the migration.

`--max-workers N` (default 4) sets how many collections process in parallel under `--all-collections` via a thread pool. Each collection has an independent ID namespace so concurrency is correctness-preserving; the practical ceiling is backend rate limits, not local CPU. Completion order is non-deterministic above 1 worker; pass `--max-workers 1` for serial, operator-readable output. Single-collection runs are inherently serial.

Carve-outs:
- `taxonomy__*` collections are skipped (centroids use `centroid_hash` from the `topics` table, not `chunk_text_hash`).
- Pre-RDR-053 chunks lacking `chunk_text_hash` raise a structured error; re-index that collection from source before running.

### nx t3 backfill-manifest

```
nx t3 backfill-manifest [-c COLLECTION] [--no-dry-run] [-n N] [--resume] [--only-gapped]
```

Backfill the `document_chunks` manifest from T3 chunk metadata (RDR-108 D2). Reads each catalog document's T3 chunk metadata (`doc_id`, `chunk_index`, `chunk_text_hash`, span coordinates) and writes one manifest row per chunk, so the catalog can answer "what chunks compose this Document, in what order?" without consulting T3. Omitting `-c` processes every collection registered in the catalog; `-n` caps documents per collection.

Idempotent: re-running overwrites the manifest with the same content (DELETE + INSERT in one transaction per document). Progress goes to stderr; SIGINT flushes a state file (`$NEXUS_BACKFILL_STATE_FILE`) so `--resume` skips collections already marked done. Same carve-outs as `reidentify`: `taxonomy__*` skipped, pre-RDR-053 chunks without `chunk_text_hash` error out.

`--only-gapped` (nexus-3n7pr) restricts the run to documents that currently have ZERO manifest rows — a batched pre-pass over the target collection's doc_ids determines the gapped set before any T3 read or write, so a document that already has a manifest is never rewritten. Use this for a targeted repair pass over a large, mostly-healthy collection (the default, unset behavior processes and rewrites every document `-c`/the full catalog selects, which is correct for a first-time backfill but not for repairing a small damaged subset). Honored under `--dry-run` too — the dry run is the sizing instrument, so it reports the same skip/process partition a real run would touch. Skipped docs are counted separately from the other skip classes (`no T3 collection`, `zero chunk matches`, `phase3 no chunk_index`, `chash id/metadata divergent`, `FK conflict`) in both the per-collection and summary output. Under `--only-gapped`, `-n N` bounds the GAPPED set (at most N zero-manifest documents are SELECTED for processing; healthy documents are still counted as skipped), so a canary such as `-n 25 --only-gapped` always exercises the write path when any gapped document exists — but selection is not the same as a written outcome: a selected doc that then hits `zero chunk matches`, `chash id/metadata divergent`, or `FK conflict` still counts against the N budget without producing a written manifest row, so `-n 25` is not a guarantee of 25 written rows. Without `--only-gapped`, `-n N` bounds the raw tumbler-ordered document list as before. `--resume` and `--only-gapped` combine cleanly: `--resume` skips whole COLLECTIONS already marked done in the state file, before `--only-gapped`'s per-DOCUMENT filter ever runs on the remaining ones.

Two skip classes guard the write path itself (nexus-dmf7r / nexus-r7g3i, from the nexus-3n7pr 910-doc remediation): a T3 chunk's own id is the row `fk_catalog_chunks_chunk` actually validates against (the id IS the chash by construction, RDR-108/RDR-180); if that id ever disagrees with its `chunk_text_hash` metadata copy, the whole document is skipped and counted as `chash id/metadata divergent` rather than writing a manifest row keyed on the unverifiable copy. Separately, a manifest write can still 409 against `fk_catalog_chunks_chunk` when no `nexus.chunks` row exists at all for the written chash (no manifest-only route exists for that document — it needs re-indexing or re-putting first); that is caught PER DOCUMENT and counted as `FK conflict`, and the rest of the collection keeps processing rather than aborting the remainder unmarked. Any other write error still propagates and aborts the collection, same as before.

Because those two classes are caught per-document and never raise, a collection containing one or more of them still "completes" this run without error — so `--resume`'s state file (nexus-69c94 critique) does NOT mark such a collection `__done__`. `zero chunk matches` joins the same residual (critique C2): it is the DOMINANT live gap class, and a future remediation pass (re-index / re-put) can make exactly those docs recoverable, so a collection whose only gaps are `zero chunk matches` must also stay revisitable rather than being marked permanently done. Any collection with a nonzero `zero chunk matches` / `chash id/metadata divergent` / `FK conflict` residual is recorded `__partial__` with the residual counts instead, and a future `--resume` run reprocesses the WHOLE collection (per-doc idempotency makes the full re-pass safe and cheap for the docs that already healed). The CLI prints which collections were left partial and why; `--resume`'s startup line also reports how many partial collections from a prior run are about to be retried.

---

## nx taxonomy

Topic taxonomy — HDBSCAN clustering of T3 collection embeddings into topics for navigation, search grouping, and relevance boosting.

Topics are auto-discovered after `nx index repo`, gated on the run actually having indexed files (an all-unchanged re-index skips the discover/kmeans/label/project/L1 pass entirely — a self-heal guard still runs discovery if a target collection has zero topics). The gate is per-collection: only collections whose own content kind (code / docs / rdr) wrote files this run enter the discover loop. Non-force discovery on a collection that already has topics returns instantly with `topics already exist … use `nx taxonomy rebuild` to re-discover` — previously the same no-op was decided only after fetching and clustering the full collection; incremental flush-grain assignment is what keeps existing topics current. Labeling with Claude haiku runs in a DETACHED background process spawned at the end of indexing (nexus-qqc1v) — the CLI exits immediately and labels land minutes later (progress: `~/.config/nexus/logs/deferred_labeling.log`; run `nx taxonomy label` manually if the spawn failed or you don't want to wait). Search results are grouped by topic and boosted when results share a topic cluster.

```
nx taxonomy status                              # health: collections, coverage, review state
nx taxonomy discover --all                      # discover topics for all T3 collections
nx taxonomy discover -c docs__nexus             # discover for a single collection
nx taxonomy discover -c docs__nexus --force     # re-discover (preserves operator labels)
nx taxonomy list                                # topic tree
nx taxonomy list -c docs__nexus                 # topic tree for one collection
nx taxonomy show 5                              # docs assigned to topic 5
nx taxonomy show 5 --assignments                # per-assignment quality: chunk/confidence/provenance
nx taxonomy review                              # interactive: accept/rename/merge/delete/skip
nx taxonomy review --auto                       # unattended: batched claude_dispatch verdicts
nx taxonomy review --auto --dry-run             # preview verdicts, apply nothing
nx taxonomy review --auto --yes                 # skip the destructive-action confirm prompt
nx taxonomy review --auto --batch-size 20       # topics per claude_dispatch call (default 40)
nx taxonomy label                               # batch-relabel with Claude haiku
nx taxonomy assign doc-id "topic label"         # manually assign a doc (see below)
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
nx taxonomy backfill-source-collection                        # RETIRED: refuses with guidance
nx taxonomy backfill-source-collection --apply                # RETIRED: refuses with guidance
```

### `nx taxonomy show --assignments`

`nx taxonomy show TOPIC_ID --assignments [-n/--limit N]` (nexus-92uh0) shows
per-assignment quality instead of a plain doc-id list: chunk (doc_id),
confidence (`similarity`, blank when the assign path recorded none),
`source_collection`, `assigned_by`, and `assigned_at` (UTC ISO-8601). This is
the read path for `topic_assignments.similarity` / `source_collection` /
`assigned_at` — written by every assign path (discover, `assign`,
`assign_single`, projection) but previously visible nowhere; without it an
operator could see a doc was assigned to a topic but not how confident the
assignment was, when it was made, or which collection it came from
(nexus-onjvy). `--limit` bounds how many of the topic's doc_ids are looked up
(same default as the plain `show`, 20); rows are additionally filtered to the
requested topic id defensively, in case a doc_id was reassigned between the
doc-id lookup and the detail fetch.

### `nx taxonomy assign`

Manually assign one document (chunk) to a topic by label: `nx taxonomy assign
DOC_ID "topic label" [-c/--collection SCOPE]`. `DOC_ID` must be a conformant
64-character lowercase hex chunk chash (RDR-194 D1) — `topic_assignments.doc_id`
is a chunk chash end to end, not a title or catalog tumbler; pass the chash
`nx search` / `nx query` reports.

`--collection` scopes the LABEL LOOKUP only (disambiguates same-named topics
in different collections) — it does not, by itself, decide what gets stored.
`source_collection` on the written assignment row is resolved from the
RESOLVED topic's own `collection` field, not from `--collection` (RDR-194
D1/P3b): `--collection` is an optional filter with no default, whereas the
topic's own collection is always on record once the topic exists, so it is
the authoritative value — the same identity `assign_from_chashes_<dim>`'s
centroid branch and the engine's own non-projection assignment path resolve
to. `topic_assignments.source_collection` is `NOT NULL`; a resolved topic
with no collection on record is a corrupt `topics` row, not user error, and
the command raises a `UsageError` naming the topic id rather than silently
guessing a value.

### `nx taxonomy review --auto`

Unattended alternative to the interactive judge: batches pending topics
(`--batch-size`, default 40) and asks `claude_dispatch` for a verdict per
topic — `accept` / `rename` / `delete` / `merge` — using the same rubric a
human reviewer would apply (specific-coherent label -> accept; coherent
cluster with a bad label -> rename; syntax pattern-pollution such as
pytest/monkeypatch scaffolding, Java test boilerplate, license/import
headers, CSS blobs, or home-directory path fragments -> delete;
near-duplicate of another topic -> merge into its real topic id).

```
nx taxonomy review --auto                       # default: up to 5000 pending topics
nx taxonomy review --auto -c docs__nexus         # scope to one collection
nx taxonomy review --auto --limit 200            # cap topics considered
nx taxonomy review --auto --batch-size 20        # topics per claude_dispatch call
nx taxonomy review --auto --dry-run              # print verdicts, apply nothing
nx taxonomy review --auto --yes                  # skip the destructive-action confirm prompt
```

`accept` and `rename` apply immediately (unless `--dry-run`, which suppresses
every mutation including those). `delete` and `merge` are held and printed as
a grouped destructive plan — topic id, label, doc count, and the model's
one-line reason for deletes; source label -> target label and doc counts for
merges — then applied only after an interactive `y/N` confirm or `--yes`.
Declining leaves those topics pending.

Fail-open throughout: a `claude_dispatch` exception, a malformed response, or
an invalid verdict entry leaves that topic pending rather than raising.
Verdicts are matched back to topics by their real id, not list position, so a
model response cannot be misapplied to the wrong topic. Merge verdicts are
additionally guard-validated once the whole batch is known — self-merge, a
target that is itself a merge source in the same run (the same-run
merge-chain guard: dropping this is what prevents a same-run `A->B, B->C`
chain from silently orphaning `A`'s doc assignments onto an already-deleted
`B`), a missing target, a target in a different collection, or a target that
was itself verdict-deleted this run all drop the merge (topic stays pending)
rather than applying it. Each destructive apply is independently
fault-tolerant — one delete or merge raising does not abort the rest of the
batch; it is counted as failed and logged, and remaining actions still apply.
A closing summary line reports exact counts: accepted, renamed, deleted,
merged, skipped (declined, fail-open, and guard-dropped), and failed
(apply-time errors).

`--limit` defaults to 15 for interactive review and 5000 for `--auto` —
pass `--limit` explicitly to override either mode. Dispatch is sequential,
one batch at a time; parallel dispatch is an explicit non-goal for this
version (nexus-6i01g).

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
| `status` | Collections, topic count, coverage, review state. `-c NAME` filters to one collection, `-n N` caps to the top N collections by doc count (default: all), `--summary` shows only the totals line, `--needs-review` shows only collections with pending topics |
| `discover` | Discover topics via HDBSCAN. `--all` for all collections, `-c NAME` for one, `--force` to re-cluster |
| `list` | Topic tree with doc counts. `-c NAME` filters by collection, `-d N` sets tree depth (default: 2) |
| `show ID` | Documents assigned to a topic. `-n N` limits results (default: 20) |
| `review` | Interactive review: accept, rename, merge, delete, skip. `-c NAME` to filter, `-n N` topics per session (default: 15). `--auto` swaps in batched `claude_dispatch` verdicts (default limit 5000); `--yes` skips the destructive-action confirm, `--dry-run` applies nothing, `--batch-size N` sets topics per dispatch (default: 40) |
| `label` | Batch-relabel topics with Claude haiku. `--all` relabels accepted topics too |
| `assign DOC LABEL` | Manually assign a doc to a topic by label. `-c NAME` scopes label lookup |
| `rename OLD NEW` | Rename a topic. `-c NAME` scopes label lookup |
| `merge SOURCE TARGET` | Merge source into target. `-c NAME` scopes label lookup |
| `split LABEL --k N` | Split into N sub-topics via KMeans. `-c NAME` scopes label lookup |
| `links` | Inter-topic link counts from catalog graph. `-c NAME` filters by collection |
| `rebuild` | Full re-cluster (alias for `discover --force`). `-c NAME` required |
| `project SOURCE` | Cross-collection projection: match chunks against other collections' centroids. `--against TARGETS` for explicit targets (default: sibling collections). `--threshold N` (optional; when omitted uses per-corpus defaults: `code__*` 0.70, `knowledge__*` 0.50, `docs__*`/`rdr__*` 0.55 — see [taxonomy-projection-tuning.md](exploration/taxonomy-projection-tuning.md)). `--top-k N` caps centroids considered per chunk (default: 3). `--use-icf` suppresses hub topics via Inverse Collection Frequency weighting (RDR-077). `--persist` to write assignments. `--backfill` to project all collections against each other |
| `hubs` | List generic-pattern hub topics (RDR-077 Phase 5). `--min-collections N` (default 2), `--max-icf F` filter, `--warn-stale` flags hubs whose latest assignment post-dates the newest `last_discover_at` across contributing source collections, `--explain` shows DF / ICF / matched stopword tokens per row. |
| `audit --collection NAME` | Per-collection projection-quality report (RDR-077 Phase 6): total assignments, p10/p50/p90 of raw cosine, count below threshold (re-projection candidates), top receiving topics with ICF, pattern-pollution flags. `--threshold F` overrides the per-corpus default; `--top-n N` caps the receiving-topic list. |
| `backfill-source-collection` | **RETIRED.** Backfilled `topic_assignments.source_collection` for legacy hdbscan/centroid rows (RDR-087 Phase 4.1) via raw SQL over the LOCAL SQLite store, which was deleted in the RDR-158 P4 retirement; the engine exposes no equivalent read-then-update API. Raises `click.UsageError` unconditionally, with or without `--apply`. If a pre-migration install still holds legacy NULL `source_collection` rows, run the backfill on the last migration-capable 6.x release before migrating |

**Configuration** (in `.nexus.yml`):

```yaml
taxonomy:
  auto_label: true                    # label with Claude haiku after discover (default: true)
  local_exclude_collections: []       # default: ["code__*"] — local embeddings cluster poorly on code
```

**Upgrade path**: Run `nx upgrade` — it converges every pending rung, including the cross-collection projection backfill. Run `nx taxonomy discover --all` to populate topics for new collections.

---

## nx store

Manage T3 knowledge entries.

```
echo "# Cache Strategy" | nx store put - --collection knowledge --title "decision-cache" --tags "decision,arch"
```

| Subcommand | Description |
|------------|-------------|
| `put FILE_OR_DASH` | Store document (use `-` for stdin) |
| `get DOC_ID` | Retrieve entry by 64-char hex ID (from `nx store list`) |
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
| `--id ID` | Exact 64-char document ID from `nx store list` |
| `--title TITLE` | Exact title metadata match (deletes all matching chunks) |
| `-y` / `--yes` | Skip confirmation prompt |

Note: IDs shown by `nx store list` are 64 hex chars (the full `sha256(text)` digest — RDR-180). Pre-cohort 32-hex IDs are no longer resolvable at all: the `chash_alias` legacy-reference route was retired at nexus-lgdel.l1 (its beneficiary population reached zero) — re-index the source to mint a canonical 64-hex chash. `--title` delete is paginated and safe for multi-chunk documents. To delete an entire collection use `nx collection delete`.

Deleting a `store_put`-origin document (`content_type == "knowledge"`, no `file_path`) also tombstones its catalog row via a chash-keyed, best-effort reap, so it drops out of `nx catalog list` immediately rather than waiting for the next `nx catalog gc` sweep. **The reap now runs BEFORE the T3 chunk delete, not after (RDR-191 F10c, nexus-o8dil.5).** The engine's delete is anti-join-scoped: it refuses to remove a chunk that any live catalog manifest row still references, including the note's own not-yet-tombstoned row, so reaping first is what lets the ordinary single-owner delete succeed at all.

For `--id`, the id is existence-checked against `--collection` before anything else runs: `nx store delete --id X -c Y` fails with `Entry 'X' not found in Y` when X is not actually in Y, catching a bogus id, or an id paired with the wrong collection, before the reap or the T3 delete can touch anything.

When the chash's catalog lookup is ambiguous (more than one candidate catalog document shares it), the reap deliberately declines to tombstone anything: nothing is guessed (nexus-5axey). That is no longer a benign no-op: with no live-manifest-row retracted, the engine's anti-join then refuses the chunk delete, and the command exits non-zero instead of silently leaving a ghost catalog row behind:

```
Error: Entry 'X' existed in Y moments ago but the delete did not remove it -- likely
anti-join-protected by another live reference, or a concurrent modification. Any
catalog cleanup for it has already run; check 'nx catalog list' / 'nx catalog reconcile'.
```

Run `nx catalog gc` / `nx catalog reconcile` to resolve the ambiguity, then retry the delete.

`--title` reports the SERVER's actual deleted count, not the number of matching chunks found (nexus-o8dil.45): `nx store delete --title 'X' -c Y` prints `Deleted K entries with title 'X' from Y.`, where K can be less than the N chunks matched, because the same anti-join can legitimately retain a chunk another live document's manifest still references. When `K < N`, a second line on stderr reports the shortfall:

```
(N-K of N requested were retained -- still referenced by another live document, not deleted.)
```

A script that asserts an exact `Deleted N entries` count against the number of chunks it expected to match can now legitimately see a smaller number without that being a bug; check stderr for the retained-count line before treating it as a failure.

`nx store expire` (removes all TTL-lapsed `knowledge__*` entries, no flags) has the same actual-count contract: it prints `Expired N entries.` where N is the count `batch_delete` actually removed, not the number of lapsed rows it found. The reap-before-delete ordering above applies here too: each expired chash's manifest row is retracted before the chunk delete is attempted, so a genuinely shared chash correctly stays retained and correctly stays out of the reported count, rather than being counted as expired while the chunk survives.

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

Non-conformant legacy chunk ids (16- or 32-char pre-migration ids that fail
the service backend's `chash` length constraint) are re-hashed to full 64-char
content-derived ids automatically (RDR-180); the CLI reports how many were
re-hashed.

**Restoring a pre-migration (Chroma-era) backup:**

```
nx store import old-backup.nxexp
# Error: header claims 'voyage-context-3' (1024-dim) but vectors are
# 768-dim for collection '...' -- the header label is wrong (a known
# defect of pre-migration exports, GH #1370); re-run with --assume-model.
nx store import old-backup.nxexp --assume-model bge-base-en-v15-768
```

```
nx store import partial-backup.nxexp
# Error: ... Hint: this looks like a chunk-id constraint conflict --
# a non-conformant legacy chunk id or a duplicate key. If you're
# re-running a partial import, retry with --skip-existing.
nx store import partial-backup.nxexp --skip-existing
```

---

## nx memory

T2 persistent memory (service-backed Postgres — see [nx daemon](#nx-daemon) below; the SQLite substrate is retired, RDR-158 P4). See [Storage Tiers](storage-tiers.md) for what T2 holds and how it bridges sessions.

```
nx memory put "auth uses JWT" --project nexus_active --title findings.md --ttl 30d
```

| Subcommand | Description |
|------------|-------------|
| `put CONTENT --project NAME --title NAME` | Write a memory entry |
| `get [ID]` | Read entry by numeric ID |
| `get --project NAME --title NAME` | Read entry by project + title |
| `search QUERY` | Keyword search (served by the engine's Postgres full-text index) |
| `list` | List entries |
| `delete` | Delete one or more entries |
| `expire` | Remove expired entries |
| `promote ID --collection NAME` | Promote entry to T3 by ID |

**`put` flags:** `--tags`, `--ttl` (default: `30d`), `--merge` (canonical-fact merge: fold into an existing high-overlap entry instead of creating a duplicate, non-destructive), `--merge-threshold FLOAT` (word-set Jaccard threshold for `--merge`, default: `0.5`)

**`list` flags:** `--project NAME` (filter by project), `-a` / `--agent NAME` (filter by agent name)

**`promote` flags:** `--collection` (required), `--tags`, `--remove`

**`search` flags:** `--project NAME`

**Degenerate queries now fail loud instead of returning a silent empty result** (nexus-senub, engine v0.1.69+): a query with no searchable terms — every word an English stopword (`nx memory search AND`) or punctuation-only — used to come back as an ordinary `No results found.`, indistinguishable from a real empty result. The engine now returns `400 {"code": "no_searchable_terms", ...}` for this case and `nx memory search`/`search_glob`/`search_by_tag` exit non-zero with a clean error message instead. This is corpus-dependent, not a blanket rejection: a stopword-only query can still resolve a real hit through the title/tag index (which doesn't strip stopwords) and return normally — only a search that finds nothing AND has no searchable content-side term hits the 400. Older engines fail open to the prior silent-empty-result behavior (no version gate).

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

T1 ephemeral session notes (PG-backed storage-service session, shared across agents; no in-process opt-out — nexus-4lkmz).

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
| `flag ID` | Mark for auto-flush to T2 at session end (accepts the 8-char ID prefix `list` shows) |
| `unflag ID` | Remove flush mark (accepts the 8-char ID prefix) |
| `promote ID --project NAME --title NAME` | Promote to T2, report `action=new` or `overlap_detected` |
| `clear` | Delete all scratch notes — prompts with an entry/flagged count unless `-y`/`--yes` (scripts must pass `-y`) |

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

**Wall-clock budget (`NX_T1_CLI_BUDGET_S`, default 60):** every bare-CLI
scratch operation runs under a total wall-clock budget covering the
session-token mint/borrow and any 401 self-heal retry legs. Past the budget no
further leg starts and the command fails with a "T1 service slow/unreachable"
remedy pointing at `nx doctor`; an operation slower than 5s emits a visible
slow-path warning. `0` (or negative) is the strictest setting — no retry legs
ever — not unlimited.

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
| `rename OLD NEW` | In-place metadata-only rename in the T3 vector store + T2 + catalog cascade (4.8.0, nexus-1ccq). Never re-embeds; same-prefix renames whose embedding-model segment differs are rejected (6.3.1, nexus-tcvpn) |
| `re-embed NAME --to MODEL` | In-place re-embed for non-CCE Voyage models (nexus-bw65). Service mode: same-model only — the computed vectors ride the verbatim passthrough; a cross-model `--to` fails loud (server-side embedding routes by the collection NAME's model segment; cross-model moves are the migration pipeline's job). `--no-dry-run --yes` to apply (6.3.1, nexus-c9xr2/u37lw) |
| `rewrite-metadata [NAME]` | Rewrite/repair chunk metadata in place; `--all` for every collection, `--source-path` to scope to one source, `--dry-run` to report counts only |
| `audit NAME` | Deep-dive per-collection report: distance histogram, top-5 cross-projections, orphan chunks, hub topics (RDR-087 Phase 4) |
| `health` | Composite per-collection health table — chunk counts (T3-sourced), staleness, hub score (RDR-087 Phase 3.4) |
| `merge-candidates` | Pair-wise cross-collection overlap ranking — surfaces collection pairs with high shared-topic similarity as merge/bridge candidates (RDR-087 Phase 4.3) |
| `delete NAME` | Delete collection (irreversible) |
| `prune` | List collections whose name-declared embedding dim mismatches the ACTIVE serving embedder — orphans from a prior embedder generation that every search silently skips (GH #1113, nexus-9tsdf). Fail-safe: no flags lists only; `--yes` deletes via the same cascade as `delete`; `--dry-run` always wins over `--yes`. An unresolved active-embedder probe lists nothing (never guesses). `nx doctor` names these orphans and points here |

**`verify` flags:**

| Flag | Description |
|------|-------------|
| `--deep` | Multi-probe health check: embeds up to 5 documents already in the collection, queries each back, and reports the probe hit rate. Status: `healthy` (100%), `degraded` (partial hits), `broken` (0%). Shows distance of last successful probe and the metric used |

**`reindex` flags:**

| Flag | Description |
|------|-------------|
| `--force` | Skip the pre-delete safety check (which verifies the source documents are still present before wiping the collection) |

The `reindex` command performs a pre-delete safety check before wiping the collection: it confirms the original source documents are still accessible. If the check fails, the command aborts unless `--force` is given. After re-indexing, a `verify --deep` probe runs automatically to confirm retrieval health. The command dispatches per collection type (`code__`, `docs__`, `rdr__`, `knowledge__`) to the appropriate indexer.


**Chash resolution (RDR-086 Phase 1.3, table retired at RDR-187).** The
per-chunk pass once populated a separate T2 `chash_index` table so `nx doc
cite` and `Catalog.resolve_chash` could answer "which collection + doc_id
holds this chunk hash?" without scanning the vector store. RDR-187 dropped
that table: the chunks tables ARE the chash-keyed store and the catalog
manifest (`document_chunks`) carries the doc-to-chash structure, so there
is no separate index to reconcile and nothing for this pass to backfill. A
tqdm progress bar renders in an interactive terminal (auto-disabled on
non-TTY CI logs).

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
| `--force-prefix-change` | Allow a cross-prefix rename (e.g. `code__foo` → `docs__foo`) OR a same-prefix rename whose embedding-model segment differs (6.3.1, nexus-tcvpn). Rename never re-embeds, so either change leaves the vectors in the OLD model space under a name claiming the new one — use only when you know the vectors already match the target name (cross-model moves belong to the ladder's substrate rung, the RDR-162 vector ETL — `nx upgrade`) |

Renames the collection in the T3 vector store via `t3.rename_collection` (a metadata-only update on the pgvector service path — no embedding re-upload, no Voyage cost, no vector egress), and cascades the new name through every collection-scoped engine table — chunks, taxonomy (assignments, topics, meta, centroids), aspects, highlights, telemetry, and the catalog documents/collection registration (service-backed Postgres; `chash_index` is retired, RDR-187). Ordering (SIG-8 / nexus-nhyh): the T2 cascade runs FIRST, then the T3 rename, so a partial failure is recoverable: if the T3 rename fails the T2/catalog rows can be re-pointed or the rename re-run; if T2 fails no T3 rename was attempted.

**`audit` flags:**

| Flag | Description |
|------|-------------|
| `--format {table,json}` | Output format (default: `table`) |
| `--live` | When the 30-day `search_telemetry` histogram is empty, sample live chunks from ChromaDB and derive the distance histogram from self-queries (4.8.0, nexus-fx2d). Budget ~10 s at default `--live-n` |
| `--live-n N` | Number of live-probe samples when `--live` fires (default: 25) |

Renders four sections: distance histogram, top-5 cross-projections, orphan chunks (>30d with no incoming links), and top-10 cross-collection hub topics this collection contributes to. (A fifth `chash_index` coverage section was removed at nexus-70vpz / RDR-187 — once RDR-187 dropped `chash_index`, the ratio it reported compared a chunk count to itself and could never read anything but 1.0.)

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

Delete cascade (engine-side, RDR-164 P2) covers the collection's chunks, taxonomy assignments + topics + centroids, aspects, highlights, aspect-queue rows, and catalog documents + manifest + collection registration; the streaming pipeline buffer is swept by its own engine endpoint (RDR-186). `chash_index` is retired (RDR-187).

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
| `update [PATH]` | Refresh THIS repo's nexus stanza to the current one (default: `.`). The remedy `nx doctor` names when it reports stanza drift. The managed-repo sweep is demoted, not gone — hidden `nx hooks update-all` (see [Internal upgrade primitives](#internal-upgrade-primitives)) still runs it; `nx upgrade` calls it for you, so most operators never need it directly |

Hooks run `nx index repo` in the background after each qualifying git operation, appending output to `~/.config/nexus/index.log`. If a hook file already exists, the nexus stanza is appended (sentinel-bounded) without overwriting existing content.

**Hook status values:** `not installed` · `owned` (nexus-created) · `appended` (added to existing hook) · `unmanaged` (no nexus sentinel)

### nx hook routing-stats

The `nx hook` group (hidden from `nx --help`) hosts Claude Code lifecycle plumbing: `session-start`, `session-end`, `session-end-flush`, and `session-end-detach` are invoked by the conexus plugin's SessionStart/SessionEnd hooks with a JSON payload on stdin and are not intended for manual use. `routing-stats` is the group's one operator-facing verb.

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
| `--escapes` | List escape events with their `# routing-allow:` reasons (the escape-audit surface); combines with `--json` |

JSON output shape (nexus-mzvwa.9): `{"rules": {<rule>: {...}}, "selftest_excluded": N,
"unregistered_rules": [...]}` — `unregistered_rules` present only when a
hooks.json registration surface was found. Fail-ladder self-test rows
(`selftest_*`, plus the historical `test_rule`/`unknown` suite pairs) are
excluded from the stats and counted in `selftest_excluded`; the table view
footnotes the same count. Rules present in the log but absent from the
plugin's hooks.json are marked `(unregistered)` — the log is append-only
history, so a stats row alone never proves a hook is currently live.

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
| `--service` | **DEPRECATED** (RDR-174 P3.1) — plain `nx init` now provisions the local service backend by default; the flag still works (and prints a deprecation notice) but will be removed in a future release. Provisions the local Postgres + pgvector cluster the RDR-152 service backend uses, locks the embedder to bge-768, acquires + verifies the native service binary, fetches the bge-768 ONNX, and starts the service. Idempotent. The binary + PG bundle are acquired automatically from the wheel's pinned engine tag (override: `NEXUS_SERVICE_TAG` env or a prior `nx daemon service install-binary`). |

**First-run ladder convergence (nexus-9xfx5):** once the backend is serving,
`nx init` converges the upgrade ladder as its final step, so a virgin box's
first `nx doctor` shows no pending rungs and the diagnostic views exist. A
convergence failure never fails init — it defers with a pointer at
`nx upgrade` (idempotent).

**Builtin plan-template seeding (nexus-e1ti4):** immediately after the
ladder converges, `nx init` also seeds/reconciles the builtin plan-template
library (the same reconcile `nx plan reseed` runs). Before this, a virgin
install left the global plan tier at zero rows — only the manual
`nx plan reseed` populated it — so `nx_answer`'s plan-match gate missed
100% of the time on a fresh box. Idempotent (dedups on the plan's
`(project, dimensions)` key) and best-effort: a seeding failure is echoed
to stderr and logged, never silently swallowed, and never fails an
otherwise-successful init. `nx doctor --check-plan-library` is the durable
signal if it ever fails; the fix is `nx plan reseed`.

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

1. **Does NOT add the `[local]` extra.** It once did, right here, via an
   extras-preserving `uv tool` reinstall — that step went with the RDR-144
   embedder picker at RDR-174 P1.3. Extras are fixed when the install is
   created and travel in the generation's install receipt, which every
   [`nx self install`](#nx-self-install) carries forward, so an upgrade never
   silently drops the 768-dim embedder. `nx doctor` flags the case where
   `local.embed_model` is bge-768 but the extra is absent (search silently
   falls back to 384-dim).
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

**Local mode with Voyage (nexus-35ok4 / GH #1461).** `nx init`'s guided
prompt only offers the two on-device embedders above, but a local install
CAN use Voyage instead: set `local.embed_model` to a voyage-shaped value
directly and configure a key —

```
nx config set local.embed_model voyage-code-3
nx config set voyage_api_key <key>
nx daemon service stop && nx daemon service start   # re-plumb the key into the engine
```

The engine only reads `NX_VOYAGE_API_KEY` at process spawn (the supervisor
resolves it from the credential chain and injects it into the native
binary's environment), so **a restart is required** after either command
above for the change to take effect — `nx config set` alone does not
reach an already-running engine. Once restarted, the engine boots into
Voyage-only mode for every collection (no local ONNX fallback), and
`nx index`/`nx store` mint `voyage-code-3`/`voyage-context-3` collection
names to match, exactly like cloud mode.

**What happens to a corpus you already indexed under bge/minilm** depends
on whether the key is configured yet, not just on `local.embed_model`:

| `local.embed_model` | `voyage_api_key` | Read (search / list / get) | Write (index / store put) |
|---|---|---|---|
| bge/minilm (default) | — | bge/minilm collection, as always | bge/minilm collection, as always |
| voyage-* | absent | finds the existing bge/minilm collection | **grandfathers onto the existing bge/minilm collection** (the engine has no key yet, so it's still serving bge — writing there still works) |
| voyage-* | present, no voyage collection yet | finds the existing bge/minilm collection | **targets the voyage collection — a NEW sibling**, never the old bge/minilm one |
| voyage-* | present, voyage collection already exists | **finds ONLY the voyage collection** — the bge/minilm one is no longer checked, even if it still holds data (see caveat below) | targets the (already-existing) voyage collection |

The "targets a new sibling" row is the one to plan around: once the key is
configured (whether or not the engine has been restarted yet), a write
does not extend your existing bge/minilm corpus — it starts a separate
`voyage-code-3`/`voyage-context-3` collection alongside it. Indexing
without a configured `voyage_api_key` in this state fails loud at write
time rather than silently falling back to bge. Pre-existing bge/minilm
collections are never deleted or migrated automatically; once the engine
is actually restarted and running voyage-only, they become unreadable
through it entirely (not just unwritable) until you reindex — so switch
before you have a corpus you care about, or budget time to reindex it
into the new voyage collection.

**Caveat — reads stop checking bge once a voyage sibling exists.** The
"finds the existing bge/minilm collection" read behavior above holds only
*until* the first keyed write creates the voyage collection. From that
point on, reads resolve directly to the voyage collection and never fall
back to check bge again — even though the bge collection is still there
and may still hold data nothing has migrated. `nx search`/`nx store list`
resolve to ONE physical collection per corpus, so this is not a
multi-collection merge; anything left in the old bge collection after a
keyed write is effectively invisible to reads until you reindex it into
the voyage collection. This is the same underlying gap tracked as a
follow-up in nexus-ddmfg (the engine's voyage-only-mode-flip after
restart) — that bead's scope now explicitly includes this stale/orphaned
bge-data-after-a-keyed-write case, not just the engine-restart case.

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
| `set KEY VALUE` | Set single value; also accepts `KEY=VALUE` form. |

**`get` flags:**

| Flag | Description |
|------|-------------|
| `--show` | Reveal the full value instead of masking |

**Managed-service credentials** (RDR-166 greenfield onboarding):

| Key | Env var | Purpose |
|-----|---------|---------|
| `service_url` | `NX_SERVICE_URL` | Managed endpoint base URL (e.g. `https://api.conexus-nexus.com`) |
| `service_token` | `NX_SERVICE_TOKEN` | Per-tenant bearer token (operator-provisioned) |
| `mint_token` | `NX_MINT_TOKEN` | Self-minting credential (RDR-005 2a, nexus-wrwb7): a `scope=mint` or `scope=mint-locked` bearer. When set, `nexus.db.data_token.DataTokenManager` self-mints short-TTL data tokens (`POST /v1/data-tokens/mint`) and presents those instead of the static `service_token` on every T1/T2/T3 engine call. Unconfigured (the default) is zero behavior change. Issue a mint-locked credential locally via `nx service token issue --scope mint-locked --tenant <t>` (see [`nx service`](#nx-service)). |
| `mint_tenant` | `NX_MINT_TENANT` | The tenant stamped in the mint request BODY (nexus-ssqk9), overriding the caller-passed tenant every `Http*Store` otherwise defaults to (`"default"`). A `scope=mint-locked` credential is bound server-side to whatever tenant it was ISSUED under (e.g. `"nexus"`) — `DataTokenHandler` 403s the mint the instant the body tenant differs from that bound tenant, so a real deployed credential routinely needs this set. **`mint_token` and `mint_tenant` travel as a PAIR**: set `mint_tenant` to the credential's actual bound tenant whenever that tenant is not literally `"default"`. Not a secret — displays UNMASKED from `nx config get`/`nx config list` (unlike every other credential in this table). `nx config set mint_tenant <tenant>` |

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
nx doc render docs/paper.md --project-root /path/to/repo  # resolver context (bead DB, rdr_paths); default: cwd
```

### nx doc validate

Parse-and-resolve without emission. Exits non-zero on any unresolved token.

```
nx doc validate docs/paper.md
nx doc validate docs/paper.md --project-root /path/to/repo  # resolver context (bead DB, rdr_paths); default: cwd
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
nx doc check-extensions docs/paper.md --primary-source docs__foo --format json
```

| Flag | Description |
| --- | --- |
| `--primary-source NAME` | Required. Collection whose projection defines "grounded" |
| `--threshold F` | Projection-similarity cutoff (default 0.70). Docs at-or-above are grounded; below are author-extension candidates |
| `--format table\|json` | Report format; default `table` |

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
- `2` — empty `chash_index` (run `nx upgrade` — the ladder heals the manifest), empty collection, or unknown collection

---

## nx doctor

Health check for all dependencies.

```
nx doctor
```

**Exit codes (7.21.0, nexus-be6x8).** The exit code says what the glyphs
say: `0` — every check is ✓ or ⚠ (soft warnings never move it, RDR-129 B4);
`1` — at least one hard ✗, something needs fixing; `2` — at least one fatal
✗, nexus cannot function (a broken generation layout, a missing base
interpreter). Before 7.21.0 only the two `fatal` checks could move the code,
so a sweep that printed genuine ✗ lines exited `0` and any script gating on
`$?` read a constant. Automation that wants "healthy or only warnings" tests
`== 0`; automation that only cares whether nexus will run at all tests `< 2`.

**Supplementary checks (new in 7.11.0).** After the default sweep prints its
own result, `nx doctor` additionally runs the cheap, read-only subset of the
`--check-*` diagnostics inline: `resources`, `plan-library`, `taxonomy`,
`aspect-queue`, and `t1`. Before 7.11.0 all fourteen `--check-*` modes were
opt-in only, so a real backlog was invisible unless an operator happened to
run its exact flag (the motivating case: an aspect-queue throwing hundreds of
claim failures while nothing in the default run watched it). These are
visibility-only and never change the sweep's exit code: their failure
semantics are not uniform, so gating on them would be an unreviewed policy
change. Each runs isolated, so one crashing cannot hide the rest, and the
section ends by naming the flags a default run still does NOT cover
(`--check-schema`, `--check-search`, `--check-quotas`, `--check-mcp-logs`,
`--check-tier-discipline`, `--check-storage-boundary`,
`--check-post-store-hooks`, `--check-mineru`, `--check-wal-retention`) so the
blind spots stay explicit. The section is printed on the human-readable path
only; `--json` output shape is unchanged.

Checks (live T3 first): the nexus-service vector reachability probe (RDR-155: probed unconditionally — a pgvector install with the service down does NOT doctor all-green), the T3 collection census via the pgvector service, the service bge-768 model in local-service mode, and (local-service mode only) the service cross-encoder reranker model.

Data-token self-minting (nexus-wrwb7, RDR-005 2a): a `mint_token` presence + reachability check that always runs (not behind a `--check-*` flag). Unconfigured (the default — most installs) reports a loud-but-passing skip line, since self-minting is optional and the static `service_token` path runs unchanged. When `mint_token` IS configured it routes through `DataTokenManager`'s own process-wide, TTL-cached singleton (never a throwaway manager — nexus-ssqk9 fixed a residue-discipline bug where the check used to mint a FRESH token on every single `nx doctor` invocation) and reports which of three things happened on success, plus the granted TTL: MINTED a fresh token, REUSED one already live in-process, or REUSED one borrowed from the cross-process lease file (nexus-9c7t9, below). A real `nx doctor` invocation is its own fresh subprocess with an empty in-process cache, so its own two outcomes are "minted a fresh" (no lease existed yet, or it had gone stale) or "reused the cached (lease file)" (a prior `nx` invocation's mint is still fresh); "reused the cached (in-process)" is observable only inside a long-lived process such as the MCP server. Degrades to a soft warning (never fatal, never a silent "ok") on an unresolvable endpoint or a rejected mint. If the credential is bound to a tenant other than `"default"` (see `mint_tenant` above), configure `mint_tenant` too — the doctor check mints against `mint_tenant` (or `"default"`) exactly like every other call site; an unconfigured `mint_tenant` against a mint-locked credential bound to a different tenant surfaces as the same 403 the `mint_tenant` table entry above documents. IMPORTANT caveat baked into the success line's wording: pre-cutover on the managed cloud path the edge still strips client `Authorization` and injects its own credential (RDR-005 2a staged cutover), so a successful round trip through the edge does not yet prove this credential's own authority — treat it as reachability, not proof, until the cutover.

**Cross-process data-token lease cache (nexus-9c7t9).** Every successful mint also (best-effort) persists the short-TTL data token — never the mint credential itself — to `~/.config/nexus/data_token_lease.<key>` (mode `0600`, atomic write; `<key>` is a filesystem-safe digest of the endpoint host:port and tenant, never a raw URL). The NEXT `nx` invocation for the same `(endpoint, tenant)` borrows this lease instead of minting, as long as it is still within the same 20%-of-TTL freshness window the in-process cache uses; a stale, corrupt, or foreign lease is silently ignored and the manager mints as before, so a lease-write failure never breaks a mint. This is what makes back-to-back scripted `nx` loops safe: before this cache, every invocation minted its own token and 5+ back-to-back invocations in one minute exhausted the engine's `MintRateLimiter` default burst (5 per credential+tenant per minute) and failed loud; now the engine sees roughly one mint per `(endpoint, tenant)` per TTL window rather than per invocation, and the rate-limiter ceiling only binds a genuine cold-start storm (many `nx` processes launched concurrently before any lease exists), not ordinary sequential CLI usage. `nx uninstall` removes every `data_token_lease.*` file unconditionally, alongside the managed credentials.

Before setting `mint_token` on a real install, `tests/e2e/data-token-cli-gate.sh` (nexus-rftfs) drives this whole journey — `nx service token issue --scope mint-locked`, `nx config set mint_token/mint_tenant`, a `store put`/`search` round trip that can only succeed via the self-minted token, this doctor check, and a wrong-`mint_tenant` negative arm — as REAL `nx` subprocess invocations in a scrubbed sandboxed HOME against a throwaway local engine (mirrors `tests/e2e/fresh-install-mvv.sh`'s isolation exactly). The in-process pytest E2E (`tests/db/test_data_token_manager_e2e.py`) proves the `DataTokenManager` resolution seam itself; this gate proves the CLI/config.yml/doctor wiring around it that only a real subprocess exercises.

Then, corpus-integrity checks (both modes, all read-only, all degrade to a skip rather than crash `nx doctor`): dimension-orphaned collections (nexus-9tsdf, remedy `nx collection prune`); **the dangling-manifest ROW census RETIRED (RDR-191 Phase 6, nexus-o8dil.33, 2026-08-15)** — `nx doctor` used to run a corpus-wide "populated manifest whose ROWS no longer resolve to a T3 chunk" sweep via the engine's `manifest_verify_all()`; that function is DROPPED, and the check is removed entirely, because the manifest-chunk FK (`catalog_document_chunks -> nexus.chunks`, VALIDATEd) now REJECTS the dangling state at write time — there is no longer anything for a corpus-wide sweep to find. The per-document form (formerly `nx catalog manifest-verify`) is [retired](#nx-catalog-manifest-verify--retired) the same way; use `nx catalog verify --collection NAME` for the one surviving damaged-manifest detection surface (client-side, per document, independent of the retired engine function — see [nx catalog verify](#nx-catalog-verify)). The pre-backfill NULL-collection census (`manifest pre-backfill rows (collection IS NULL)`) is UNAFFECTED and still runs — the FK does not cover NULL-collection rows (`MATCH SIMPLE` exempts them), so that census remains live. **The `--check-dangling-links` / `--strict-dangling-links` flags are RETIRED (RDR-194 Phase P1, bead nexus-tk070.p1, 2026-08-15)**, the identical shape as the manifest-verify retirement above: `nx doctor` used to report `catalog_links` rows whose `from_tumbler`/`to_tumbler` resolved to no document at all (nexus-ysrwi, GH #1419 issue 7) via the engine's `GET /v1/catalog/links/orphaned`; the flags are gone because `fk_catalog_links_from_document`/`fk_catalog_links_to_document` (`catalog-032-links-tumbler-fk.xml`, ON DELETE CASCADE) now REJECT a link naming a tumbler with no `catalog_documents` row at write time, so that state is unreachable by construction. The underlying `GET /v1/catalog/links/orphaned` route and the `HttpCatalogClient.orphaned_links()` method are NOT retired — they still report the one case the FK deliberately does not cover (a link whose endpoint document exists but is TOMBSTONED, since soft delete does not fire `ON DELETE CASCADE`); there is simply no `nx doctor` flag surfacing it any more. `nx catalog purge-trash`'s stranded-chunk count (the opposite direction: existing T3 chunks with no live manifest referrer) is unrelated to any of the above and still runs; stale index-run fences — documents stranded in `index_state='indexing'` beyond 6 hours (safe but wasteful: every intervening `nx index` re-chunks and re-embeds at full cost; remedy `nx index <path> --force`; nexus-5xn3k.6); chash width-conformance — width-non-conformant chash rows (the GH #1414 class, `octet_length(chash) <> 32`) via the engine's tenant-scoped `GET /v1/catalog/chash/conformance` route (nexus-du2dw, RDR-180): reports per-dim (`embedding_<dim>` column of the unified `nexus.chunks` table) total/non-conformant/sample counts, names collections it cannot route to a dim instead of silently skipping them, and degrades honestly (loud skip, never a false clean) on a pre-route engine; and tumbler-allocator ("next_seq") drift — owners whose sequence counter has fallen at or below their own highest child. Self-heals per-owner on that owner's next registration (`nx index <path>`, the only remedy this check names); the engine also exposes an all-owners `sweepNextSeqDrift` converge route for the same drift class, not yet wired to a `nx` client verb (nexus-0ehwe).

Then: Voyage AI key, ripgrep binary (as of 6.16.0 an optional-accelerator advisory — missing rg renders install hints, never a failed doctor; hybrid search is simply disabled), git binary, git hooks status for registered repos, the MinerU server (as of 6.16.0 probed only when actually provisioned — an explicit non-default `pdf.mineru_server_url` or a live `nx mineru start` pid; unprovisioned fresh boxes render the not-configured skip instead of a red ✗), index log last-write time, orphaned PDF checkpoints, orphaned pipeline buffer entries, T2 integrity, T2 best-effort writes (the meter's only producer — the RDR-129 chash dual-write hook — is retired by RDR-187, so a nonzero count is reported as a frozen HISTORICAL count, never a warning), and — in service mode — a stray T2 autostart unit left over from a pre-service-mode install (GH #1405: soft warning naming the unit path and the removal command). (The RDR-129 T2 daemon-singleton check is retired along with the T2 daemon it guarded — nexus-i711w Stage 2 sub-stage B; the single-writer invariant it enforced now belongs to Postgres, not to a pid count.) The T2 integrity check reports a transient FTS5 write-lock during active indexing as a soft warning, not a hard failure (RDR-129 B4). The Voyage credential line (`VOYAGE_API_KEY`) is informational only: it describes enrichment/engine-bootstrap config, never a serving requirement, and is never fatal — the live T3 health surface is the vector-service probe above and `nx daemon service status`. (The `CHROMA_*` credential rows retired with the migration machinery at RDR-155 P4b.)

Migration-report checks retired (RDR-155 P4b): the RDR-178 migration-report / write-divergence doctor rows died with the migration machinery; `<config>/migration-reports/*.json` files on disk remain as inert audit artifacts.

Orchestration-hook plugin floor (nexus-3xg21): a soft-warn row checks Claude Code's `installed_plugins.json` for the conexus plugin version — a plugin older than v6.14.0 carries no RDR-184 orchestration hooks (no subagent stop-guard, no expectations ledger) and doctor says so with `/plugin update conexus` as the fix. A box with no plugin install shows a not-applicable pass.

Stranded-install check (nexus-gynt2): a doctor row (`Stranded pre-PG install`) guards the post-Chroma-deletion era. On a release that no longer ships the migration tool (RDR-155 P4b — this one), a box still carrying unmigrated pre-PG data (`chroma.sqlite3`, `t2.db`, `memory.db`, or `catalog/.catalog.db` present with no verified migration report) fails doctor fatally with the two-hop instruction: install the pinned last migration-capable release, run `nx upgrade` there (the ladder converges the data migration), then upgrade back. The same detection also refuses `nx init`, banners every CLI invocation on such a box, and surfaces through both MCP servers' `instructions` channel at startup.

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
| `--check-taxonomy` | Verify the `topic_links` ≡ projection-assignment invariant (GH #252) against the ENGINE. Exits 1 on drift or an engine-side error, 2 when no engine answers or the deployed engine lacks the `/links/drift` route (unverifiable is not a pass; the frozen-SQLite fallback was deleted 2026-08-29 — there is no path back to that era) |
| `--check-tier-discipline` | Audit tier-write activity for the current session: prints the tier-write summary and warns when a substantive session has no write-back (Phase 1B nexus-a52i) |
| `--check-mcp-logs` | Summarize ERROR/CRITICAL events in nexus's OWN structured MCP log (`<NEXUS_CONFIG_DIR>/logs/mcp.log` plus rotations), counted by event name with the most-recent example per event. Until 7.11.0 this scanned ONLY Claude Code's per-server cache, so nexus's own error signatures had no automated consumer at all; that cache scan survives as a clearly-labeled secondary section, since it carries a distinct signal the nexus-side log cannot (client-side stdio transport death: `STDIO connection dropped`, `stdio transport error`, macOS only, skips cleanly elsewhere — RDR-094 Phase H, nexus-50u5) |
| `--mcp-log-hours N` | Lookback window in hours for `--check-mcp-logs` (default: 24) |
| `--check-storage-boundary` | RDR-120 P0.A AST-scan for direct `sqlite3.connect` / `voyageai.Client` calls and `T2Database`/`T3Database` constructions outside the named allowlists in `storage_boundary_lint.py`. The per-line `# epsilon-allow:` escape token is retired (RDR-186 P4): surviving sites are enumerated per file with exact counts; a new site is a hard violation |
| `--fail-on-violation` | With `--check-storage-boundary`, exit 1 if any violation is found (otherwise the lint is informational). With `--check-schema`, treat an honest N/A (fingerprint withheld by design) as a failure too — for release-gate callers that need an actual OK rather than an unprovable N/A that reads identically to a pass (nexus-b1v9z) |
| `--phase ID` | With `--check-storage-boundary`, the RDR-120 phase identifier used to record the `120-phase-<phase>-catalog-allowlist-count` T2 metric |
| `--check-t1` | Diagnose T1 session lease presence + freshness. Checks `~/.config/nexus/t1_session_lease.<session_id>`. Exits 1 only when a session-id resolves AND a lease file exists AND it is expired/corrupt; a resolved session with no lease file at all is informational (a bare CLI legitimately has none — the MCP lifespan mints its own) |
| `--check-mineru` | Verify MinerU is importable — surfaces a corrupt install at doctor-time instead of waiting for the first math-heavy PDF index to fail |
| `--check-wal-retention` | Sample retained WAL bytes (local service only) via `pg_ls_waldir()`, escalating a `nexus_svc` session to `pg_monitor` with `SET ROLE` first — unconditionally, since `nexus_svc` is `NOINHERIT` in every deployment posture, so `pg_monitor`'s privileges are never ambient without it. Purely informational (RDR-191 Phase 4 trough-window context, not a pass/fail gate): **always exits 0**. Reports UNMEASURED (never a false clean) when the sample can't be taken |
| `--json` | Emit machine-parseable JSON. On the MAIN sweep (no mode flag) this emits `{"checks": [{name, ok, status: ok\|warn\|fail, detail, fatal, fix_suggestions}], "summary": {total, ok, warn, fail}, "local_mode"}` (nexus-0vycz — previously the flag was silently ignored there). Also honored by `--check-search`, `--check-quotas`, `--check-mcp-logs`. Combining `--json` with any other mode flag that cannot honor it is a usage error, never a silent ignore. |

The `--fix` flag retroactively applies HNSW `search_ef` tuning to all existing local-mode collections. New collections get this automatically. In cloud mode (SPANN), prints a skip message — SPANN defaults are adequate.

```
nx doctor --check-schema          # Report where the T2 schema lives
```

The `--check-schema` flag (RDR-076; service-backed since RDR-152) validates
that the T2 schema is actually applied. The local-SQLite table/index/FTS5
census this check used to run died with the `=sqlite` opt-out (RDR-158 P3,
nexus-7bomn); nexus-p0clh replaced it with an unconditional N/A stub that
validated nothing. nexus-vl8lk PORTED it: the check now asks the engine's
`GET /version` for the applied Liquibase changelog fingerprint
(`schema_latest_id` / `schema_changeset_count` / `schema_error`) and renders
an honest verdict — OK with the changeset count, FAIL on `schema_error` or
zero applied changesets, exit 2 (state UNKNOWN) when the engine is
unreachable, or an explicit N/A when the endpoint withholds the fingerprint
by design (managed/cloud service).

The honest N/A is exit 0 by default (nexus-vl8lk: an operator asking "is my
schema okay?" interactively should not get a false failure). Combined with
`--fail-on-violation` (nexus-b1v9z), that same N/A exits 1 instead — for a
release-gate caller (e.g. `tests/e2e/release-sandbox.sh`) that cannot tell
an honest N/A apart from a real pass by exit code alone, and for which the
whole point of running the check is proving the substrate is actually
present and correct. A genuine `schema_error` or zero-changeset FAIL is
already non-zero regardless of this flag.

```
nx doctor --check-plan-library    # Report plan-library dimensional health
```

The `--check-plan-library` flag (introduced 4.9.13, nexus-4x9q) buckets
every row in the plan library into authored / backfilled / non-dimensional
and enforces the RDR-078 builtin floor. The local-SQLite census this check
used to run died with the `=sqlite` opt-out (RDR-158 P3, nexus-7bomn);
nexus-p0clh replaced it with an unconditional N/A stub that validated
nothing. nexus-vl8lk PORTED it: the check now reads the live plan library
via `HttpPlanLibrary.list_plans` (no new engine route) and renders the same
census against real service data — exit 1 when the global-tier builtin
count is below the floor (fix: `nx plan reseed`), exit 2 (counts UNKNOWN)
when the service is unreachable. No `nx plan repair` hint (that command
group no longer exists; see [`nx plan repair`](#nx-plan-repair-removed)).

**Disk-vs-live template parity (nexus-f1mbo).** The count floor above can
only fail against a library that is nearly empty — it cannot fail against
one that is the wrong shape. Beside the floor, the check now compares every
template shipped on disk against its stored row and fails with the same
exit 1 (fix: `nx plan reseed`, or `nx plan reseed --force` for a drifted
row — see [`nx plan reseed`](#nx-plan-reseed)) on:

- **missing** — a shipped template with no library row at all;
- **drifted** — a library row whose content no longer matches its
  template.

An **orphaned** row (a `builtin-template`-tagged row with no template
shipped on disk) is reported as a WARN, not a failure — remove it with
`nx plan delete <id>` if intended. When the live listing hits its page cap,
absence is unprovable — the check reports drift (still trustworthy for the
rows it did see) but notes that missing templates were not checked, rather
than silently under-reporting.

A WARN (orphaned rows, or non-dimensional legacy rows) never flips the exit
code on its own — only the FAIL-class conditions above do that. But the
verdict line now names any WARN emitted in the same run (`All checks
passed, with N warning(s).`) instead of printing an unqualified `All checks
passed.` next to a WARN two lines above, which read as the block
contradicting itself (nexus-eg5tw).

`--check-t3-legacy-metadata` / `--strict-legacy-metadata` (nexus-1714) were
DELETED at nexus-lgdel.l2: the check surveyed local Chroma T3 collections
for pre-RDR-108-Phase-3 `doc_id`/`source_path` chunk metadata, and reported
*not applicable* on every service-backed install. Since the RDR-155 P4b
Chroma deletion (7.0.0) the dependency itself is gone — there is no
Chroma-backed `T3Database` left to survey, in production or in test
fixtures — so the check had reported *not applicable* unconditionally for
every current install; structurally dead, not merely legacy-flavored.

```
nx doctor --trim-telemetry              # Delete aged search_telemetry + hook_failures rows (default 30 days)
nx doctor --trim-telemetry --days 7     # Aggressive retention (minimum 1 day)
nx doctor --trim-telemetry --dry-run    # Preview the row count WITHOUT deleting
```

`--trim-telemetry` trims both age-reaped, no-cascade audit tables: `search_telemetry` (RDR-087, one row per (query, collection) pair on every `nx search` / MCP search call when `telemetry.search_enabled` is true) and `hook_failures` (RDR-164 P0 audit-table TTL parity). Both trims go through the engine (`POST /v1/telemetry/{search,hook_failures}/trim` via `HttpTelemetryStore`) in every mode — there is no longer a local-SQLite arm (nexus-i711w Stage 2 sub-stage A collapsed the seam; nexus-ingey). Run periodically from cron or a CI job; the default 30-day window keeps an analytical signal long enough to detect slow-burn silent-threshold-drop patterns.

Combine with the global `--dry-run` flag to preview the count before deleting anything — the engine computes the preview from the exact same `WHERE` predicate the real delete uses (never a separate count query), so the previewed number is guaranteed to match what a follow-up non-dry-run call removes. `--dry-run` applies to BOTH tables together; there is no way to preview one while trimming the other in the same invocation.

```
nx doctor --check-quotas            # Vector-store limits + embedder caps + reranker + retry headroom
nx doctor --check-quotas --json     # Structured output for dashboards / CI gates
```

The `--check-quotas` flag (introduced 4.9.0, nexus-c590) emits a four-section pre-flight report: (1) `vector_store` — the per-request limits from `nexus.db.limits.QUOTAS` (`MAX_QUERY_RESULTS`, `MAX_RECORDS_PER_WRITE`, `MAX_CONCURRENT_*`, document size caps), which remain the authoritative chunking and paging caps, plus a reachability probe of the T3 vector store; (2) `voyage` — per-model token and dimension caps (`voyage-3`, `voyage-code-3`, `voyage-context-3`); (3) `cross_encoder` — the reranker's model info; (4) `retry` — the cumulative accumulator from `nexus.retry.get_retry_stats()`, so transient-error backoffs observed in the current process surface alongside the static limits.

Exit codes:
- `0` — the T3 vector store is reachable.
- `1` — unreachable; the report is not actionable without a working store. Suitable as a CI gate.

**Breaking change in 7.0.0**: the JSON section key `chromadb` was renamed to `vector_store`. It had carried the dependency's name for machine-consumer stability; RDR-155 P4b removed the dependency, and a MAJOR release is the point at which this documented payload's shape changes. Dashboards and CI gates parsing `--json` must read `vector_store`. The `voyage`, `cross_encoder` and `retry` sections are unchanged.

```
nx doctor --check-post-store-hooks   # Enumerate registered post-store hook chains
```

The `--check-post-store-hooks` flag (introduced 4.18.0, `nexus-b0ka`) prints every hook the MCP runtime has registered against the document-grain and batch-grain post-store chains, in fire order. Surfaces the side-effect surface that a `store_put` triggers (taxonomy assignment, aspect extraction queueing, link generation, etc.) without grepping `mcp_infra.py`. Use after a hook-registration change to confirm the chain wires up as intended.

```
nx doctor --check-aspect-queue       # Surface RDR-089 aspect-extraction worker depth
```

The `--check-aspect-queue` flag (introduced 4.18.0, `nexus-1pfq`) reports the `aspect_extraction_queue` row count plus per-status breakdown (`pending`, `processing`, `failed`, `completed`), the oldest non-completed `enqueued_at` as a lag indicator, and the top failed rows with their `last_error`. The same data surfaces in the `nx console` Aspect Queue card on `/health` for live monitoring. Pre-RDR-089 databases (no queue table) report cleanly as "table not present" rather than erroring. A transport failure (service unreachable) reports UNKNOWN and exits 0 — not reporting pass or fail; a reachable queue with one or more `failed` rows is a real backlog signal and exits 1 with a `✗ FAIL:` marker, matching the other promoted supplementary checks (nexus-fylxo).

---

## nx plan

Plan library maintenance commands (RDR-092 Phase 0d).

```
nx plan list                     # Tabulate plans in the library
nx plan show ID_OR_NAME          # Full record for one plan
nx plan delete PLAN_ID           # Delete a plan row (prompts unless -y)
nx plan disable PLAN_ID          # Soft-disable a plan without deleting it
nx plan enable PLAN_ID           # Re-enable a previously disabled plan
nx plan set-scope PLAN_ID TAGS   # Override a plan's scope_tags
nx plan reseed [--force] [--insert-only]  # Re-run the builtin seed loader
nx plan hygiene [--apply]        # Flag/disable bead-dumps, null-verb, always-failing plans
```

Service mode is the only mode: all verbs (`list` / `show` / `delete` /
`disable` / `enable` / `set-scope` / `reseed` / `hygiene`) route to the live
engine-served plan library over HTTP. The `NX_STORAGE_BACKEND=sqlite` escape
hatch is retired (RDR-158 P3 — setting it is a hard error), and the `repair`
group that used to require it was deleted along with the local SQLite plan
library (RDR-158 P4, nexus-i711w sub-stage A3); see below. `reseed`
reconciles by default (nexus-f1mbo) — see below.

### nx plan hygiene

```
nx plan hygiene            # report-only scan
nx plan hygiene --apply    # disable the flagged plans (reversible)
```

Scans the plan library for three pollution classes and, with `--apply`,
DISABLES them (never deletes; reverse with `nx plan enable ID`):

- plans whose `plan_json` is not an executable retrieval DAG (unparseable
  JSON, no/empty `steps`, steps without a `tool`) — the bead-dump class that
  can match a question and then crash the plan runner;
- null-verb rows (legacy pollution predating the save-time verb requirement;
  unmatchable by verb-filtered `nx_answer`);
- always-failing plans (zero recorded successes, 3+ failures) — the matcher
  already skips these live; hygiene retires them durably.

Unlike the retired `nx plan repair` group (see below), this verb works in
service mode: it routes through the storage facade and cleans the live
engine-served library on migrated installs. Partial apply failures are
reported per plan; a scan that hits the 10,000-row limit says so rather than
silently truncating.

### nx plan list

```
nx plan list [--scope S] [--origin builtin|grown|user] [--name SUBSTR] [-n N] [--json] [--include-disabled]
```

Tabulates plans: id, origin, verb, scope, use count, last used, name. Origin is
heuristic (`builtin` when tags include `builtin-template`; `grown` when
`project == 'personal'` with empty tags; `user` otherwise; nexus-7bwe tracks an
explicit origin column). `--include-disabled` also shows soft-disabled rows,
marked `[D]`. `--json` emits the same fields as a JSON array. Default limit 50.

### nx plan show

```
nx plan show ID_OR_NAME [--json]
```

Prints a plan's full record: metadata, run metrics (use / match / success /
failure counts), dimensions, and the pretty-printed `plan_json`. The argument
is a numeric id or a name substring (first match wins). `--json` dumps the raw
row.

### nx plan delete

```
nx plan delete PLAN_ID [-y]
```

Deletes the plan row. The numeric id is required (not a name) because deletion
is destructive and a name-substring lookup is fuzzy; find the id with
`nx plan list` or `nx plan show <name>` first. Prompts for confirmation unless
`-y`/`--yes`.

### nx plan disable / enable

```
nx plan disable PLAN_ID [--reason TEXT]    # Soft-disable a plan without deleting it
nx plan enable PLAN_ID                     # Re-enable a previously disabled plan
```

Introduced 4.18.0 (`nexus-mrzp`). `disable` sets `disabled_at` on the plan row so both matcher lanes (T1 cosine via `list_active_plans`, T2 Postgres full-text via `search_plans`) skip it, without losing its row id, telemetry counters, or T1 cache embedding. `--reason` appends a `disable-reason:<text>` tag as a historical record (preserved even after re-enable). `enable` clears `disabled_at`. Useful for triaging a plan whose match-text is misrouting traffic without committing to a delete + re-seed cycle.

### nx plan set-scope

```
nx plan set-scope PLAN_ID TAGS
nx plan set-scope PLAN_ID --from-project
```

Explicit admin override of a plan's `scope_tags` (comma-separated; can widen or
narrow scope). The matcher reads scope_tags fresh on every call, so changes take
effect immediately. Applies the same normalization as `plan_save`: hash suffixes
and glob tails stripped, scope-agnostic sentinels (`all`) dropped, stored
sorted-unique. Idempotent. `--from-project` (mutually exclusive with `TAGS`)
stamps scope_tags from the plan's own `project` column, the same recovery
source as the automatic #1069 fallback in `save_plan`.

### nx plan reseed

```
nx plan reseed [--force] [--insert-only]
```

Re-runs the four-tier plan-library seed loader. **Reconciles by default**
(nexus-f1mbo): each shipped template is compared against its stored row and
the row is rewritten when they differ, and any missing template is inserted.
A rewritten row is re-created, so its match/use counters reset — correct,
since those counters described the superseded text. Rows tagged grown/ad-hoc
are never rewritten, and library rows with no template on disk are left
alone; both cases are reported rather than acted on.

Reconcile used to be gated behind `--force`, which meant an install could
only be made correct by a user who already knew the library was wrong — an
install that never ran `--force` silently kept a stale snapshot with
templates missing and rows drifted. `--force` is now **accepted for
compatibility and is a no-op**: reconcile is the default, so the flag
changes nothing. `--insert-only` selects the pre-nexus-f1mbo behavior —
skip the update leg, insert missing templates only, and leave drifted rows
alone.

### nx plan repair (removed)

```
nx plan repair scope-tags          # Backfill empty scope_tags + rewash legacy 'all' sentinels
nx plan repair dimensions          # Backfill verb/name/dimensions on NULL-dimension rows
nx plan repair match-text          # Populate plans.match_text + refresh plans_fts
nx plan repair retire-legacy       # Delete pre-RDR-078 'operation'-shape rows
nx plan repair builtin-bindings    # Patch bindings into pre-4.10.1 builtin rows
nx plan repair all                 # Every pass, in dependency order
```

These were consumer-side content-repair verbs (RDR-120 §A8) that mutated row
content via raw SQL against the local T2 SQLite plan library. The `dimensions`
pass, for example, used a 20-rule verb-from-stem dictionary over the `query`
column with a wh-question fallback (`how` / `what` → research; `why` →
review), tagging confident matches `backfill` and wh-fallback rows
`backfill-low-conf`.

The whole group was **deleted in 7.0.0** along with the local SQLite plan
library (RDR-158 P4, nexus-i711w sub-stage A3); the `NX_STORAGE_BACKEND=sqlite`
escape hatch that could reach the local file is retired too (RDR-158 P3 —
setting it is now a hard error). `nx plan repair ...` is no longer a
registered command. The local file, where present, is a frozen pre-migration
snapshot.

---

## nx daemon

Storage daemon lifecycle (RDR-120; the `t3` sub-group retired at
RDR-155 P4b — T3 serves through `nx daemon service`). All user-facing CLI
commands that touch persistent state (`nx memory`, `nx index`, `nx store`,
`nx catalog`, `nx_answer` and the MCP tools) route through the nexus-service
in every mode, so multi-process consumers — host CLI + Cowork sessions + dev
containers + the nx-mcp server — share one Postgres-arbitrated writer instead
of each opening their own connection. (Prior to RDR-158 P4 this was a
separate T2 daemon process arbitrating a local SQLite writer; that daemon is
retired — see [`nx daemon t2`](#nx-daemon-t2--retired) below. Postgres is the
write arbiter now.)

For a brand-new install the recommended setup is the collapsed flow
(RDR-174 — one provisioning command, no separate T2-daemon step):

```
uv tool install conexus    # the nx CLI
nx init                    # acquire the pinned signed engine + PG bundle, provision Postgres+pgvector, fetch bge-768, start the service, offer autostart
```

No engine tag to choose: each conexus release is pinned to the exact
`engine-service` release it was tested against and `nx init` acquires it
automatically (cosign-verified). `nx daemon service install-binary` remains
available as an advanced pre-stage/override (below).

`nx init` provisions and starts the service backend and offers to register the
OS autostart unit (prompt, default yes; `--yes` accepts, `--no-autostart`
declines — see [nx init](#nx-init)). T2 (notes/plans) is served by the same
service, so there is no separate T2 install step — the `nx daemon t2` verb
group is retired (see below). The deprecated `nx init --service` flag still
works but plain `nx init` is the path now.

Upgrade later with [`nx self install`](#nx-self-install): it builds a new
generation beside the running one, carries the extras recorded in the install
receipt (`[local]` included), and never swaps a tree underneath a live session.
`uv tool upgrade conexus` and `uv tool install --reinstall conexus` do **not**
touch a generation install — the latter rebuilds the legacy uv tree and
re-symlinks over the nexus-owned shims.

T3 (the permanent vector store) serves through the native nexus-service over
Postgres + pgvector in **both** local and cloud mode (`nx daemon service`); the
legacy `nx daemon t3` ChromaDB daemon is a retired serving path.

The service's own autostart unit (`nx daemon service install --autostart`, or
accepting the `nx init` prompt) covers reboot-persistence for every tier. The
plugin's SessionStart hook no longer spawns anything for T2 — the
`nx daemon t2 ensure-running` call it used to make is retired with the rest of
that verb group (see below).

### nx daemon restart-stale

```
nx daemon restart-stale [--dry-run]
```

Finish an upgrade: after [`nx self install`](#nx-self-install) the new generation
is `current`, but every long-lived process (MCP hosts, the aspect-worker, MinerU)
keeps executing from the generation it resolved at spawn — by design, not by
fault. This verb reports every conexus process still bound to an older
generation, restarts the classes that are safe to cycle (aspect-worker —
respawns on demand; MinerU — cycled via its own lifecycle verbs), and names the
ones only you can close (MCP hosts belong to live Claude sessions).

A process that attributes to a generation is judged by IDENTITY — stale exactly
when its prefix differs from `readlink(current)`, no clock inference (this is
what catches a shim bypass: a process bound to an old generation but *started*
after the new one was installed, which the old start-time heuristic read as
fresh). Anything that attributes to no generation keeps the age heuristic
(started before the install's mtime), which is the only discriminator available
on a box that never migrated off the legacy uv tree — where in-place replacement
really does happen.

It also reports where this install came from, which explains why an upgrade did
or did not move. On a generation install that answer comes from the CURRENT
generation's own receipt (nexus-0za6e) and reads one of: `local checkout
(<path>)` — `nx self install` rebuilds from that checkout, so a new PyPI release
will not move it; `PyPI, built with --version X` — that pin was one-shot, `nx
self install` resolves the current release; or plain `PyPI` — `nx self install`
upgrades normally. Only when no generation receipt is readable does it fall back
to uv's receipt in uv's own vocabulary (`local checkout` / `PyPI, PINNED (==X)`,
where `uv tool upgrade` will never move past the pin / `PyPI, unpinned`) — the
answer a box that has not migrated actually needs.

**Engine convergence (nexus-cfgo9).** The same pass also converges the
installed engine-service binary to this release's engine — the exact
version the release was built and tested with. If the on-disk install
provenance shows a different engine, the release's pinned tag is
downloaded through the signed install path and the service is cycled.
A store blocked by the chash-poison install gate is never converged
silently: the pass prints the exact unblock steps instead. The same
pass repairs diag-view grant/ownership drift (GH #1402) and, in service
mode, unloads/removes a stale pre-service-mode T2 autostart unit
(com.nexus.t2 LaunchAgent on macOS, nexus-t2.service on Linux) left over
from a SQLite-era install (GH #1405) — such a unit otherwise respawns a
T2 daemon that exits instantly by design, forever. Each leg is
independent — a failure in one (even a missing `ps` binary on minimal
containers) never blocks the others.

This runs automatically on the first `nx` invocation after a version change
(a `last_seen_version` stamp in the config dir), printing one
`[upgrade-finish]` summary line; the verb is the manual form. `nx doctor`'s
"Process freshness" check surfaces the same skew, and its
"Engine convergence" check reports a pending engine convergence
(long-lived MCP-host-only boxes where no CLI runs won't auto-trigger —
`nx doctor` or this verb is the path there).

### nx daemon t2 — RETIRED

The entire `nx daemon t2` verb group (`start`, `stop`, `status`,
`ensure-running`, `install --autostart`, `uninstall --autostart`) has been
removed. It ran a SQLite-backed T2 daemon that arbitrated a single writer
across the host CLI, the MCP server, and dev-container clients. T2 is served
by the nexus-service over Postgres, which is the write arbiter, so there is
nothing left for it to arbitrate.

**If you have a leftover autostart unit** from a pre-retirement install
(`com.nexus.t2.plist` on macOS, `nexus-t2.service` on Linux), it will try to
run `nx daemon t2 start` on every boot and fail. `nx upgrade` removes it for
you on the next run — on every install, not just service-backed ones. To
check by hand: `launchctl list | grep com.nexus.t2` (macOS) or
`systemctl --user list-unit-files | grep nexus-t2` (Linux).

Reboot-persistence for every tier is now the service's own unit:
`nx daemon service install --autostart`, or accepting the `nx init` prompt.

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
| `--announce-stdout` | (`start`) Emit the discovery JSON on stdout at startup. |

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

> **Fixed (nexus-oyo2g):** `nx daemon service stop` used to decide what to
> signal purely from the discovery lease (15s TTL) — a supervisor whose
> heartbeat had stalled was alive and serving while invisible to that check,
> so `stop` could report "already stopped" having signalled nothing, and a
> following `start` would short-circuit once the heartbeat revived. `stop`
> now falls back to the OS process table (the same mechanism `nx upgrade`'s
> engine convergence already used) whenever the lease is absent, and always
> sweeps for a surviving engine child even after a lease-named supervisor is
> signalled — it never reports "already stopped" while a matching process is
> still running.

| Flag | Description |
|------|-------------|
| `--autostart` | Required. Install the OS autostart unit. |
| `--force` | Overwrite an existing unit file even when its content differs. |

### nx daemon service install-binary

```
nx daemon service install-binary <engine-service-vX.Y.Z>
nx daemon service install-binary <engine-service-vX.Y.Z> --no-pg-bundle
```

**Advanced / normally unnecessary** — a bare `nx init` acquires the wheel's
pinned engine automatically; use this command only to pre-stage a binary
(air-gapped installs) or to install a DIFFERENT engine tag than the pin
(engine testing). Downloads, verifies, and installs the signed native
nexus-service binary (and, by default, the relocatable PostgreSQL bundle)
from a GitHub release to the well-known location (`~/.config/nexus/service/`) with a provenance sidecar
(version, tag, sha256, install metadata). Supervisor discovery and
`nx init` use this location.

TAG is an EXPLICIT `engine-service-v*` release tag (e.g.
`engine-service-v0.1.3`); there is no "latest" resolution. Each per-platform
asset, its `.sha256`, and its `.sigstore.json` bundle are fetched and verified
(sha256 + keyless Sigstore signature, pinned to the engine-service release
workflow identity), then placed atomically. Verification **fails closed**:
nothing is installed unless BOTH gates pass. One verified seam covers the
binary and the PG bundle (RDR-161).

| Flag | Description |
|------|-------------|
| `--pg-bundle/--no-pg-bundle` | Also acquire + verify the relocatable PostgreSQL bundle from the same release (default on). `--no-pg-bundle` installs only the service binary (e.g. a cloud habitat with a managed Postgres). |
| `--config-dir` | Config directory override. |
| `--force` | Override the chash-poison pre-check (nexus-pnwu0 / GH #1414). The gate classifies the store first: width-non-conformant rows REFUSE the install (re-index the affected collections and confirm `nx doctor` clears before swapping engines — see [migration-runbook.md](migration-runbook.md)); an unverifiable store (service/PG not up) proceeds with a loud UNVERIFIED warning — install-binary is the designated recovery tool for the will-not-boot class. Use `--force` ONLY after remediating. |

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

## nx self install

```
nx self install [--keep N] [--version X.Y.Z] [--extras NAME] [--dry-run]
```

Upgrade this install of `nx`. Installs are **side-by-side generations**: the
command builds a NEW generation at `<tools>/gen-<stamp>` from the receipt of the
generation this process is running from, flips the `<tools>/current` symlink,
rewrites the shims in `<bin>`, then reaps old generations. Nothing is ever
swapped underneath a running process — a live holder keeps executing its own
generation byte-identically and converges at its next spawn, so no session has
to be closed and there is nothing to force.

**Adding an extra (nexus-pffc4):** `nx self install --extras local` builds the
new generation with that extra ADDED — the flag MERGES with the extras the
receipt already carries, never replaces them (repeatable, or comma-separated:
`--extras local,dt`). This is the supported way to get `[local]` (bge-768)
onto a box that installed without it; the legacy
`uv tool install --reinstall "conexus[local]"` answer rebuilds uv's tree over
the nexus-owned shims on a generation box and must not be used there.
`--extras` applies to generation installs only — on an unconverged legacy uv
box, converge first (`nx self install` with no flags), then re-run with the
flag.

**A uv takeover self-repairs (7.21.0).** Measured against uv 0.8: on a
generation box a plain `uv tool install conexus` rebuilds uv's tree (a
`[local]`-less copy) but refuses to overwrite the nexus shims; `--force`
takes them, and then every spawn resolves through uv's tree — wrong install,
maybe wrong version, wrong extras. `nx self install` run from that state (and
`nx upgrade`, which the SessionStart hook runs, so a box heals at its next
session with nothing to do) puts it back: shims rewritten to `current`, uv's
tree registered for reap, and — when uv's tree is the *newer* version, i.e.
you meant to upgrade — a generation built at that version from `current`'s
own receipt, so `[local]` survives. A pure uv-tool box (no generation layout)
is not a takeover; that is the convergence path above. Never run
`uv tool uninstall conexus` on a generation box: it deletes the nexus shims
at those paths. A reaped tree is what makes uv refuse to rebuild
(`uv tool upgrade conexus` → "not installed").

`<tools>` defaults to `~/.local/share/nexus/tools` (`NX_TOOLS_DIR`) and `<bin>`
to `~/.local/bin` (`NX_BIN_DIR`). A generation is a `gen-*` directory containing
a valid `nexus-install.json` receipt; the receipt is written last, so a
half-built tree is never mistaken for a working one.

| Flag | Description |
|------|-------------|
| `--keep N` | Generations to retain (default 3). The four never-delete rules still apply on top of it: the generation `current` points at, the previous one (free rollback), any generation with a live holder, and the generation hosting the running installer. |
| `--version X.Y.Z` | Install this version instead of whatever the source resolves to. **One-shot** — the pin is not sticky, and the next bare `nx self install` resolves the current release. Downgrades are safe by construction here: they build a new generation and flip, leaving the old tree for its holders. |
| `--dry-run` | Print the build command and stop. |

Extras travel in the generation's receipt and are threaded into the build
explicitly, so an upgrade never silently drops `[local]` (and with it the
768-dim embedder). There is no flag that *adds* an extra to an existing
generation — extras are fixed when the install is created.

It distinguishes THREE sites, not two (nexus-gu9zo). From a generation it
builds the next one, as above. From a **legacy `uv tool install conexus`
layout** it CONVERGES that install onto the generation layout — this is the
only supported route from a packaged install, and before 7.20.0 there was
none: the command refused everywhere that was not already a generation, so it
could upgrade a generation box and never create the first one. Measured on a
fresh `uv tool install conexus` of 7.19.0: uv-owned symlinks, no `gen-*`, no
`current`, no `<tools>` directory at all — i.e. no packaged install of any age
had the layout, and every generation box in existence was checkout-driven.

The converge builds the new generation side-by-side and never uninstalls: the
legacy tree is registered as a pseudo-generation and reaped by a LATER, separate
pass once nothing holds it, so live holders keep running from it and converge at
their next spawn. Extras bridge across from the legacy `uv-receipt.toml` — that
is the only path by which `[local]` survives the move.

Run from a dev checkout's `.venv` the command still refuses, naming
`scripts/reinstall-tool.sh` instead — a thin repo wrapper around the same
packaged scripts (`nexus/_install/*.sh`). One installer, not two. A packaged
install is no longer mistaken for a checkout: the packaged-vs-checkout question
is answered by `upgrade_finish.running_from_tool_install()`, and "where is uv's
tool dir" now has ONE resolver, `install_layout.uv_tool_root()`, honouring
`UV_TOOL_DIR` then `$XDG_DATA_HOME/uv/tools` then `~/.local/share/uv/tools`
(nexus-orhp5).

This upgrades the BINARY only. [`nx upgrade`](#nx-upgrade) walks the migration
ladder; they are two commands on purpose (RDR-143 CA-2) and `nx upgrade` never
invokes uv or pip.

**Relationship to uv.** `uv tool install conexus` is still how a box with no
`nx` at all gets its first tree. After that, `uv tool upgrade conexus` and
`uv tool install --reinstall conexus` do **not** touch a generation install; the
latter rebuilds the legacy uv tree and re-symlinks over the nexus-owned shims,
which `nx self install` (or `scripts/reinstall-tool.sh`) rewrites. `uv tool
uninstall conexus` remains the way to reap a legacy uv tree once nothing is
running from it.

---

## nx upgrade

The single trigger for the upgrade ladder ([RDR-185](rdr/rdr-185-single-ladder-convergent-upgrade.md)).

Upgrading nexus is: update the code ([`nx self install`](#nx-self-install)), then run `nx upgrade`. That single trigger
converges everything else — it brings the package, engine, and process
preconditions current, then walks one ordered ladder that auto-applies whichever
data migrations your install actually needs (the pre-RDR-108 chunk-identity
rekey is the standing rung; RDR-155 P4b retired the T2-schema and
Chroma→pgvector substrate rungs together with the migration machinery — a
pre-PG install is redirected to the pinned last migration-capable release by
the stranded-install detector), each rung detecting, converging, and verifying
before it records completion, resumable and idempotent. There is nothing to
sequence by hand and no era to know: `nx doctor` reports pending rungs
read-only, `nx upgrade` walks them, and an install that has been dormant for a
year converges the same way a current one no-ops. You are asked to decide only
what the product cannot derive for you — **billed re-embedding** (a cost
preview before anything charges; silent when nothing bills).

```
nx upgrade                        # Converge: preconditions, then every pending rung
nx upgrade --dry-run              # Report what is pending, read-only — changes nothing
nx upgrade --auto                 # Quiet mode for hook invocation (exit 0 always)
nx upgrade --yes                  # Unattended: pre-approve the billed re-embed (=NX_ASSUME_YES=1)
```

| Flag | Description |
|------|-------------|
| `--dry-run` | Report pending ladder rungs without executing — each rung's read-only `detect()`; the completion store is never opened. (`--force` was removed with the local T2 migration chain in RDR-158 P4 Stage 4 — there is no version gate left to reset) |
| `--auto` | Quiet mode for the SessionStart hook. The engine install is skipped (hook timeout budget); exit 0 always |
| `--skip-t3` | Skip T3 upgrade steps for a fast T2-only run. Also suppresses the precondition stage's engine install and process cycle (verdicts are still reported) |
| `--yes` | Assume yes to the **billed re-embed** consent prompt only (equivalent to `NX_ASSUME_YES=1`) — the unattended channel for a walk that would otherwise block on the cost preview. Not a blanket "say yes to everything": a vanished source still defers rather than guessing, and rollback is never automatic |

**Plan-library precondition runs on every invocation, never skipped.**
Beside package/engine/process/lockstep, `nx upgrade` also converges the
builtin plan-template library (reconciling it against the templates this
build ships, the same reconcile `nx plan reseed` runs) as its own
precondition axis. Unlike the others, this one is never suppressed by
`--skip-t3` or `--auto` — it is stateless and recurs on every release that
edits a template, so it is re-derived every time rather than gated behind a
flag. Failure is non-fatal and reported (a stale plan library degrades
retrieval; it does not block the upgrade or fail the invocation).

**Ladder position is derived, never stored.** How far an install is from current has exactly two answers, by class: DATA-rung state comes solely from the ladder position derived from per-rung completion records; PRECONDITION freshness (package, engine, processes) comes solely from a fresh comparison of on-disk installed state against required, and is deliberately stateless — re-derived at every invocation, never recorded. A rung is recorded complete only when its own verify passed ([RDR-142](rdr/rdr-142-migration-completeness-vs-version-row.md)), so the position never advances past deferred or failed work.

**Auto-upgrade**: `nx upgrade --auto` runs as the first SessionStart hook in the Claude Code plugin — pending ladder rungs and preconditions converge silently on every session start. Long rungs run only via explicit `nx upgrade`.

**Local T2 migrations are RETIRED** (RDR-158 P4 Stage 4, nexus-i711w): the client-side migration chain (`src/nexus/db/migrations.py`, `MIGRATIONS`, `T3UpgradeStep`) is deleted. Schema is engine-owned via Liquibase in every mode; the local `.db` files are a frozen migration source that `nx upgrade` never touches. New schema changes go through Liquibase changesets in the engine; a new DATA axis becomes a ladder rung in `src/nexus/upgrade_ladder/rungs/`, registered in `registry.py` — not a new verb.

### Internal upgrade primitives

RDR-155 P4b **deleted** the migration verbs outright (`nx guided-upgrade`,
`nx migrate-to-service`, `nx migration-audit`, the `nx storage` migrate
group, `nx daemon t3`): installs still carrying pre-PG data use the pinned
last migration-capable release (the stranded-install banner names it). The
remaining demoted-not-deleted primitives — still callable, out of `--help`:

| Demoted verb | Its only job was | Now done by |
|---|---|---|
| `nx migration` | migration-sentinel inspect/recover | crash-recovery plumbing behind the trigger |
| `nx collection backfill-hash` | upgrade-era `chunk_text_hash` repair | the ladder's manifest heal |
| `nx hooks update-all` | manual managed-hook sweep | `nx upgrade` refreshes managed hooks itself |

Verbs that appeared in the old upgrade graph but have a genuine **non-upgrade**
job keep their surface — `nx collection reindex` (refresh changed content),
`nx collection re-embed --to MODEL` (deliberately choose a different embedder),
`nx daemon restart-stale` (covers the aspect-worker/MinerU population the process
precondition does not touch), `nx init` (fresh install), `nx hooks install`
(new-repo setup).

---

## nx stranded

The cloud-mode consented de-strand escape (nexus-cmtpa). The stranded-install
detector's primary migrated signal is the engine-side upgrade-ladder
completion record, which is sound in local mode (one bundled PG per install)
but tenant-scoped rather than machine-scoped in managed/cloud mode — a shared
tenant across two machines could otherwise let one machine's migration falsely
clear the refusal for another machine's own, distinct, unmigrated pre-PG data.
`nx stranded ack` is the explicit, local, machine-scoped alternative for that
case only; it has no effect in local mode, where the engine-verified ladder
signal already governs.

```
nx stranded ack             # Attest this machine's pre-PG data is migrated (prompts for confirmation)
nx stranded ack --yes       # Same, unattended
```

`nx stranded ack` fingerprints the pre-PG artifact files found on this
machine (path, size, mtime — never content) and records a local marker
attesting the two-hop migration was completed for exactly that set. If those
files later change, the fingerprint no longer matches and the stranded-install
refusal returns. It never deletes anything — deletion of pre-PG artifacts
remains a separate, independently consented act.

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
  (`service_url` + `service_token` + `mint_token` + `mint_tenant`) from `config.yml`. Skips service-stop (no
  local service) and never touches the remote tenant's data.
- **Cross-process data-token leases** (nexus-9c7t9): removes every
  `data_token_lease.*` file under `nexus_config_dir()` unconditionally —
  mode-agnostic (a `mint_token` credential can be configured in either
  local or managed mode), not gated on `--remove-data`.

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

MinerU also **auto-starts on demand** (nexus-1qdb9): when the PDF pipeline
routes a document to MinerU and no server is running, it spawns one
automatically — race-free across concurrent indexing runs — and waits up to
two minutes for it to come healthy. A first-ever start may still be
downloading models (~2-3 GB); in that case the current document falls back
to the in-process extractor and the warmed server handles later ones — the
warm-up wait is a shared budget, so a slow first start never stalls each
document in a batch. If the spawned process dies immediately (for example
the configured port is already in use), the pipeline falls back right away
and points you at the `mineru_server` child log. Set
`pdf.mineru_autostart: false` in `config.yml` — or
`NX_MINERU_AUTOSTART=0` / `false` / `no` / `off` (any other non-empty
value force-enables, overriding config) — if you manage the server
out-of-band; the same switch also disables the automatic crash-restart
during extraction. An explicit non-local `pdf.mineru_server_url` is never
shadowed by a local spawn, and `nx mineru start` (the explicit verb) is
never gated.

**Upgrades do not start MinerU.** The post-upgrade process sweep only
*cycles* a MinerU server it finds running (so a stale binary is replaced);
a server that was not running at upgrade time stays absent — by design,
since the on-demand spawn above covers first use. The trade-off is that the
first post-upgrade PDF extraction pays the full cold start (model warm-up,
possibly a model download). Run `nx mineru start` after an upgrade if you
want the server warm before the first extraction touches it.

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
nx service record-deploy TAG [--commit SHA] [--gate-report-dir DIR | --gate-report FILE | --gate RESULT] [--url URL]
```

Record `TAG` (`engine-service-vX.Y.Z`, `vX.Y.Z`, or `X.Y.Z`) as the cloud-deployed engine in the `deployed-engine-version` T2 tracker — **guarded by a live `/version` read**. GETs the service handshake, asserts `release_version` equals `TAG`'s version, and only then writes the tracker; the recorded version is machine-sourced from the live read, never hand-typed. Fails loud (and writes nothing) if the deploy has not landed or the version disagrees, so a *wrong* version can never be recorded (nexus-dz6b1 / RDR-179). This replaces the old hand-typed `nx memory put` in the engine-release skill's record step.

**The `gate` field is derived from conexus's STEP-6 report, not typed** (7.23.0, nexus-nx3l5 shape c). `--gate-report-dir DIR` reads the gate reports in `DIR` (the conexus checkout's `deploy/`; gitignored there, so operator-local — a clone or CI cannot see them), selects the LATEST report by `run_timestamp` that gated the live `release_version` (`sections.preconditions.version_visibility.observed.release_version`, read from the live edge during the run — never `identity.jar_version`, the control-plane jar), requires `schema_version == 3` and `overall.pass`, prints its advisories (always read on green, never inferred empty), and records `gate PASSED <report basename> (advisories: N)`. Nothing is written — a named error — when the directory is missing, no report gated the live version, the latest one is red, or the schema moved; any-green and first-wins are both wrong on real files (`024127Z` red and `030235Z` green coexist for 0.1.88). `--gate-report FILE` names one report, which must have gated the live version. The tracker has one writer (`nexus.deploy_tracker.write_deployed_engine_tracker`), shared with the post-tag verify's `scripts/check_engine_release_floor.py --record-deploy-from-gate-report`, which is the normal path; this command is the manual fallback. `--gate RESULT` records a hand-typed value verbatim and is mutually exclusive with the report options — it is exactly what it says, and on 2026-08-28 it was typed 17 s before the gate it named came back red. `--commit` is recorded verbatim, not verified.

### nx service token issue

```
nx service token issue --tenant TENANT [--label LABEL] [--ttl SECONDS] [--scope tenant|mint|mint-locked]
```

Issue a new bearer token bound to `TENANT`. Printed once; only the hash is stored. `--ttl` sets an optional lifetime in seconds (default: no expiry). A token bound to a tenant ignores the client `X-Nexus-Tenant` header; the tenant comes from the token.

`--scope` (nexus-868dq / conexus RDR-005): default `tenant` (ordinary bearer). `mint` issues a **data-token mint credential** — it may ONLY call `POST /v1/data-tokens/mint` (minting short-TTL per-tenant data tokens, cross-tenant allowed, rate-limited) and is rejected on every admin and data route. `mint-locked` (nexus-xidcq / RDR-005 2a; requires engine-service ≥ 0.1.36) is the tenant-locked variant: identical surface confinement, but it may mint ONLY for its own bound tenant — a cross-tenant mint attempt gets a 403 (and does not consume rate-limit budget). Prefer `mint-locked` when a tenant self-custodies its mint credential; `mint` (cross-tenant) is the control-plane/edge shape. Issuing either mint scope requires the operator (root) bearer. `data` tokens are never issued here — only minted by the endpoint; `root` is never issuable. Revoking a mint credential (`nx service token revoke`) stops its mints immediately; outstanding data tokens drain on their own TTL (≤ 3600s ceiling, `NX_DATA_TOKEN_TTL_CEILING_SECONDS`).

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

## nx tier-status

```
nx tier-status [--session SESSION_ID] [--last N] [--since ISO8601] [--json]
```

Audit tier-write activity (T1 scratch, T2 memory/plans, T3 store) for a session. Defaults to the current session (`NX_SESSION_ID`); `--last N` aggregates the most recent N sessions, `--since` bounds by timestamp, `--json` emits structured output instead of the human table. Phase 1B (nexus-a52i).

In service mode the counts are read from the engine via `GET /v1/telemetry/tier_writes/query` (nexus-59wjj) — same filters, same row shape as the local SQLite path. Requires an engine that carries the route; against an older engine (or an unreachable service) the command degrades to an honest "service-backed; read unavailable" message rather than reporting a false zero. The doctor tier-discipline check reads through the same route.

---

## nx answer-runs

```
nx answer-runs [--since ISO8601] [--limit N] [--steps] [--include-failed] [--derive-budget] [--json]
```

Reads the `nx_answer_runs` telemetry table: every `nx_answer` MCP call
writes a row here via `POST /v1/telemetry/nx_answer_runs/record`, and until
`GET /v1/telemetry/nx_answer_runs/query` (nexus-eho3u) landed nothing ever
read one back — the ETL `import_nx_answer_run` path doesn't count, it only
writes in the other direction. Service-mode only; the table has no
session_id column, so unlike `nx tier-status` there is no per-session
default — `--since` bounds the window, `--limit` caps the listed page
(default 20, does not affect the whole-set aggregates below), `--json`
emits structured output.

Reports, computed by the ENGINE over the WHOLE `--since`-filtered set
independent of `--limit`, **including degenerate and failed rows**: total
run count, oldest run timestamp, plan-match hit count vs. inline-planner
fallback count, average `duration_ms`/`cost_usd`, and a fixed-edge latency
histogram (`<5s`, `5s-30s`, `30s-2min`, `2min-5min`, `>5min` — the same
buckets as the production distribution `nx_answer`'s own docstring cites,
and the shape the shakedown playbook's §4.5 telemetry baseline snapshot
captures every run). The human output labels this block explicitly
(`engine aggregate: ALL rows incl. degenerate + failed, whole
--since-filtered set`) — see § Three-way row split below for the
executed-ok-only counterpart, printed alongside it. Against an engine that
predates the route (or an unreachable service) the command degrades to an
honest "service-backed; read unavailable" message — a 404 is diagnosed as
version skew, any other HTTP status points at a live engine error, never a
silent "total: 0".

A "hit" is a row with a REAL matched plan (`plan_id` set and non-zero).
`plan_id = 0` is the synthetic ad-hoc `Match` sentinel every SUCCESSFUL
inline-planner run carries internally — not a matched plan — so it counts
toward `fallback`, and the per-row listing renders it `fallback` rather
than the misleading `plan=0`. (This per-row display convention is separate
from the `--steps` by-plan grouping key — see below.)

`created_at` is stamped by the ENGINE's clock, not this machine's. A run
recorded moments ago may not appear for a sub-second-precision `--since`
value if this machine's clock and the engine's have drifted by even a few
hundred milliseconds (observed non-hypothetically during development);
prefer cutoffs with real separation — minutes or more — from any write you
expect the read to see.

`--json` wraps the store's result in a query envelope —
`{since, limit, captured_at, ...}` — mirroring `nx tier-status --json`'s
`{scope, session_id, last_n, since, ...}` shape, so a caller diffing
successive §4.5 baseline snapshots can see the window and page size that
produced each one. `captured_at` is this process's own wall clock at
render time (display metadata only, never compared against a server
timestamp).

### Three-way row split: executed-ok / executed-failed / degenerate (RDR-196 .p1e)

Of the `--limit` rows actually listed (a PAGE-scoped diagnostic, DIFFERENT
from the whole-set engine block above — never mistake one for the other),
every row is split three ways:

- **executed-ok** (`step_count > 0` and the run succeeded) — the default
  population for every `--steps` aggregate below and for the page-scoped
  `executed_ok_*` latency fields.
- **executed-failed** (`step_count > 0` but the run FAILED: `final_text`
  starts with `"Error:"`, or — only detectable with `--steps` — the last
  recorded step's `ok` is `false`). A run that completed real, billable
  steps and then failed is a genuinely different population from a
  success; conflating the two reproduces the exact 45x-wrong-latency
  mistake this arc exists to end (a prior cut of this command did exactly
  that — see the code-review finding on nexus-nyry9.11).
- **degenerate** (`step_count == 0`) — further named by a read-time
  heuristic over `question`/`final_text`, never treated as a homogeneous
  "broken" bucket, since the nx-answer-degenerate-row-taxonomy census
  found 62% of a real degenerate population was the *benign* `redacted`
  class, not an error:
  - `redacted` — `trace=False` privacy opt-out (`final_text`/`question`
    == `"[redacted]"`).
  - `planner_error` — the inline-planner phase failed before any plan
    step ran.
  - `error` — a plan-execution or binding error before/without any
    completed step.
  - `other` — anything else (harness probes, unclassified rows).

`--json` carries `executed_ok_count`, `executed_failed_count`,
`degenerate_count`, `degenerate_breakdown: {class: count}`, plus a
page-scoped, executed-ok-only latency view: `executed_ok_avg_duration_ms`
and `executed_ok_latency_buckets` (same fixed edges as the whole-set
`latency_buckets` above, computed client-side from just the executed-ok
rows shown).

`--include-failed` folds executed-failed rows into the `--steps`
breakdown population alongside executed-ok rows (default: executed-ok
only) — the default answers "what does a working run cost", the flag
answers "what did failures cost too". Degenerate rows are never eligible
either way — they carry no steps to aggregate.

### Per-step breakdown (`--steps`)

Also fetches each listed row's `steps` (`GET
.../nx_answer_runs/query?include_steps=true`, nexus-lme1s / RDR-196
.p1c-b) and renders, from the executed-ok rows by default (executed-ok +
executed-failed with `--include-failed`; see above):

- **by operator** and **by source** (`llm` | `sql` | `bundle`), stable
  `--json` keys per entry (consumed directly by RDR-196 .p2a/.p2c):
  `count`, `known_cost_count`, `unknown_cost_count`, `total_cost_usd`,
  `median_cost_usd`, `total_elapsed_ms`, `median_elapsed_ms`.
- **by plan** (keyed by `plan_id` as a string, or `"fallback"` for a
  genuine planner-error miss with no `plan_id` at all — `plan_id=0`, the
  ad-hoc inline-planner sentinel, gets its OWN `"0"` key, distinct from
  `"fallback"`): `run_count`, `median_cost_usd`, `median_elapsed_ms`.
- **cost-consistency violations**: rows where a row's own `cost_usd`
  disagrees with `sum(steps.cost_usd)` beyond a relative epsilon (0.5%,
  sub-cent absolute floor — a literal to-the-cent tolerance is too loose
  for typical $0.0001-$0.05 per-call costs). Both unknown (`None`) agrees
  trivially; exactly one unknown never agrees.

A step's `cost_usd` of `None` ("unknown", not a measured cost — see
`StepRecord`'s own docstring, `plans/runner.py`) is excluded from every
sum/median but its count is always shown, never silently folded into a
zero.

Against an engine that predates the `nx_answer_steps` read route, `--steps`
degrades honestly: the human output states "does not support
include_steps"; `--json`'s `step_breakdown` is exactly
`{"steps_supported": false}` — never an empty breakdown indistinguishable
from "no steps were ever recorded".

### Predicted vs actual cost (RDR-196 Phase 3 Step 1)

When `plan_match` returns more than one above-floor candidate, `nx_answer`
picks among the CONTIGUOUS PREFIX of candidates (matcher order, starting
from the top match) within `PLAN_CHOICE_CONFIDENCE_BAND` (a named
constant, `nexus.plans.cost_estimate`) of the best raw confidence by the
**lowest PREDICTED cost** — an estimate from the candidate's step shape
(`nexus.plans.cost_estimate.estimate_plan_cost`), not a recorded per-plan
median (nexus-nyry9.3's `.r3` census found zero plans with a rankable
recorded-run population — see the RDR for detail). The prefix stops
permanently at the first candidate outside the band even if a later one
would individually qualify, so cost-ranking can never reach past a
relevance demotion the matcher's own scope-fit re-ranking (RDR-091)
already made; ties within the prefix break by earlier matcher position,
never by confidence. Outside the prefix, confidence wins regardless of
predicted cost. This decision runs on EVERY plan-match hit, not only when
more than one candidate is returned (candidate_count=1 is the common
case today per nexus-nyry9.3's census) — every candidate considered, its
predicted `usd`/`ms`/`basis`, and which one was chosen are written to
`structlog` as a `nx_answer_plan_choice` event on every hit (tail
`~/.config/nexus/logs/mcp.log`), and to the `structured=True` envelope's
`plan_choice` field (`{candidates, candidate_count, chosen_plan_id,
predicted_cost_usd, basis}`; `None` on any path that never reaches Step
1's hit branch — force_dynamic, a plan-miss, or an error before Step 1).

### `--derive-budget` (RDR-196 .p3a, nexus-nyry9.19)

Derives the default `budget_usd` from recorded **post-flip** history and
prints, instead of the report: rows scanned, executed-ok count, the three
exclusion counts (no step records = pre-7.14.0 client; pre-flip = a
flipped-operator step (`model_tiers.FLIPPED_OPERATORS`, read live)
recorded a non-cheap canonical model;
unknown cost), the qualifying run count `n`, and for p50/p75/p90/p95 the
per-run cost value plus the fraction of qualifying runs each would have
refused. Below the non-vacuity floor (`MIN_DERIVATION_RUNS`, 30) it names
NO value. `--since` bounds the scanned window; `--limit` is raised to at
least 300 (the telemetry page cap; the derivation needs every row it can
see, not a display page); `--steps` is implied and `--include-failed` is
ignored (failed runs never enter a budget derivation). The post-flip predicate is a per-step **model** filter (the
cheap tier's alias family against `StepRecord.model`), not a timestamp,
so it survives a later re-flip; `--steps`' `by_operator` aggregate keys on
operator only and pools both populations, and must never be used for this
derivation. `nx_answer(budget_usd=None)` now means "use
`nexus.plans.budget_default.DERIVED_BUDGET_USD`", which stays `None` (no
cap; enforcement OFF) until a sufficient derivation is recorded there with
its provenance.

**In this table**: `--steps`' `by_plan` entries carry `predicted_cost_usd`
and `predicted_basis` alongside the recorded `median_cost_usd` —
estimate vs actual, side by side. This is computed READ-TIME on every
`nx answer-runs --steps` call (fetches each plan's stored `plan_json`
from the plan library, prices it via `estimate_plan_cost` against a price
table built from the SAME telemetry query), never persisted — there is
still no `predicted_cost_usd` COLUMN on `nx_answer_runs`/`nx_answer_steps`
(the engine-side wire is unchanged; folding a persisted column in is
deferred to a later RDR-196 Phase 3 step). The `"fallback"` bucket (a
genuine planner-error miss with no `plan_id`, hence no stored plan JSON)
always reports `predicted_basis: "ad-hoc-no-plan-json"`; a plan library
lookup failure or a deleted plan degrades that row's predicted fields to
`None` with a named `predicted_basis` (`"unavailable"` /
`"plan-not-found"` / `"plan-json-missing"`), never a crash and never a
silent `0`.

### Per-operator model tiering (RDR-196 Phase 2 Step 3)

`nexus.operators.model_tiers.FLIPPED_OPERATORS` — `operator_filter`,
`operator_groupby`, `operator_extract`, `operator_rank`, plus
`operator_check` and `operator_verify` since 2026-08-21 (nexus-3mea3) —
dispatch at the cheap model tier **by default**, no opt-in required. The
original four cleared both pre-registered refutation criteria in the .p2c
A/B measurement (nexus-nyry9.16/.17: 14-20x cheaper, agreement at/above
the .p2a quality-proxy threshold on every measured pair); check/verify
were added on the pre-registered three-arm study (T2
`nexus_rdr/196-model-tier-study`: check agreed 1.000 on every pair across
all three models, verify fable-vs-haiku min 0.941 against a 0.70
threshold, cheap tier ~0.07-0.08x the cost — verify carries a recorded
caveat: it is UNDECIDABLE on the .p2a strong-vs-strong proxy, margin
+0.033). Every other operator (`aggregate`/`summarize`/`compare`/
`generate`) is unaffected by default — no quality proxy exists to
validate a cheap-tier switch, and the synthesis three-arm study
(`nexus_rdr/196-synthesis-tier-study`) REFUTED the cheap arms for
summarize/generate/compare outright. See
`docs/rdr/rdr-196-cost-aware-nx-answer.md`'s Phase 2 OUTCOME block for
the full per-operator decision table.

`NX_OPERATOR_MODEL_TIERING` is a 3-state override, consulted by the two
production call sites that route operator dispatches (`plans/runner.py`'s
isolated-step path, `mcp/core.py`'s inline planner):

| value | meaning |
|---|---|
| unset (default) | flipped operators route cheap; EVERY other known operator, every bundle, and the inline planner dispatch with the explicit `--model opus` pin (re-pointed from fable 2026-08-21 on the synthesis-study opus arm) (`model_tiers.STRONG_DEFAULT_ALIAS`, nexus-ek8tr). Direct MCP operator calls and `nx_tidy`/`nx_plan_audit`/`nx_enrich_beads` pin the same way at their own entry points (`_pin_default_model`), so no dispatch on this path inherits the box CLI default; only the `0` kill switch restores bare dispatch |
| `1` | measurement override: consult the WHOLE tier table (`nexus.operators.model_tiers.OPERATOR_MODEL_TIER`), including "strong" entries — for A/B re-verification, not production traffic |
| `0` | kill switch: no model injection anywhere (true pre-tiering bare dispatch — operators, bundles, and the planner inherit the box CLI default), without a code change — rollback lever |

A plan step (or MCP tool call) that already supplies its own `model=`
argument always wins over any of the above — the tiering machinery only
ever fills a gap the caller left unset. Bundled operator dispatches
(`nexus.plans.bundle.dispatch_bundle`) never consult per-operator tiers —
a bundle containing a flipped operator still dispatches strong — but
since nexus-ek8tr they carry the explicit strong pin (`STRONG_DEFAULT_ALIAS`) on every
path except the `0` kill switch, and the recorded canonical id is
checked against the requested family on every dispatch (a mismatch logs
a loud `model_family_drift` warning).

---

## nx telemetry baseline

```
nx telemetry baseline [--since ISO8601] [--json]
```

Captures the shakedown playbook's §4.5 fixed-shape telemetry baseline
snapshot (nexus-v0x32) — until this command existed, the same seven
figures were hand-assembled by composing several existing readers on
2026-08-04, -11, -19, and -27. Composes, never re-derives:

1. **nx_answer runs** — all-time total, `--since`-scoped count, plan-match
   hit vs. inline-planner fallback, the fixed-edge latency histogram
   (`answer-runs`'s own `_BUCKET_ORDER` — one definition), and the
   oldest/newest event timestamp — the instrument behind the 08-27
   capture's headline "zero rows since \<ts\>" finding.
2. **Tier writes** since `--since` (or all-time) — by tier, by tool, by
   agent, plus the null-agent share as a number.
3. **relevance_log** — row count, oldest/newest event timestamp (`GET
   /v1/telemetry/relevance/stats`, new at this bead; a server-side SQL
   `count`/`min`/`max` — this is the baseline's actual substrate-direct
   TELEMETRY figure), plus the cumulative-deletes retention marker for
   `nexus.relevance_log` (the existing `nexus-24p05` route). The two
   reads fail independently — a pre-v0x32 engine 404s on the new route
   but still answers the retention-marker one.
4. **search_telemetry** global row count, collections examined, and
   per-collection `zero_hit_rate` — looping the existing `search/stats`
   route over every T3 collection (the same enumeration `nx collection
   health` performs; `zero_hit_rate` costs no extra round trip — it is
   already on every response). Always rendered as a **LOWER BOUND**: a
   collection absent from the enumeration is invisible independent of
   any per-call error, so the caveat is unconditional, not gated on
   `errors > 0`. The text form prints the two worst (highest)
   `zero_hit_rate` readings, matching the 08-27 capture's own vocabulary
   (`zero_hit_rate 0.524 knowledge__dt-papers, 0.325 knowledge__knowledge`);
   `--json` carries the full per-collection map.
5. **Drop meter** — the RDR-129 B4 dropped-best-effort-write counter.
6. **Consent** — the literal row `RETIRED (nexus-lqqb2, 2026-08-28)`: the
   consent-audit telemetry writer family is dead wire, deleted in the
   same session this bead was designed. Never omitted, never rendered
   unavailable, never wrapped in a window — it is not a windowed read.
7. **Substrate check** — one engine-side SQL count,
   `catalog_stats.doc_count`, the same figure reconcile-stale's own
   "Substrate anchor" reads. Labeled explicitly as **context, not a
   telemetry anchor**: it is a catalog metric (`count(*)` over
   `catalog_documents`) unrelated to any telemetry aggregate in this
   block — it cross-checks neither search_telemetry's client-side sum
   nor tier_writes' by-tier sum. Kept because it still satisfies the
   playbook's §4.6 non-vacuity rule (one engine-side SQL count in the
   block); figure 3 (relevance_log's count) is this baseline's actual
   substrate-direct telemetry figure.

**Window scoping.** `--since` applies ONLY to figures 1 and 2
(nx_answer_runs, tier_writes); every other figure is always
whole-tenant/all-time. Every figure — except the consent literal, which
carries no window at all — reports its own window: `{"since": <iso>}` in
`--json` when scoped, the literal string `"all-time"` otherwise; the text
form prints the window on every line (`nx_answer runs: total N (+M since
<since>); hit H / fallback F; newest <ts>; oldest <ts>` when scoped,
`total N (all-time); ...` otherwise; `tier writes (since <since>): ...` /
`tier writes (all-time): ...`; `relevance_log (all-time): count N
(server-side SQL) ...`; `search_telemetry (all-time): ...`). No figure
may imply a window it does not honour — there is no single top-level
`since` key in `--json`, only `captured_at`.

A figure that cannot be read renders as the literal string
`"UNAVAILABLE: <reason>"` in place of its normal value — never omitted,
never a fabricated zero. `--json` keeps every key present regardless;
only the value's shape (number/dict vs. string) changes on failure. The
text form is one line per figure, diffable against a previous run's
output — "SAME QUERIES, SAME BUCKETS, EVERY TIME" (playbook §4.5).

---

## nx census

```
nx census capability [--session SESSION_ID] [--since ISO_DATE] [--project-dir PATH] [--json]
```

Counts tool calls per capability across Claude Code session transcripts, split **orchestrator vs subagent** (nexus-h33x8.1). Buckets are `skill`, `agent`, `serena`, `nx_answer`, `search_query`, `other_nx_mcp`, `baseline` (Bash/Read/Edit/Write), `other`.

Reads the transcript JSONL Claude Code already writes under `~/.claude/projects/<slug>/` — no hook, no daemon, no new log — so it is retroactive over every transcript on disk. `--project-dir` (or `NX_CENSUS_PROJECT_DIR`) overrides the directory; the default is derived from the current working directory.

**Roll-up rule, stated because leaving it unstated is what made the prior hand-derived baseline irreproducible:** a subagent's calls attribute to its **parent session**. `<sid>/subagents/**/agent-*.jsonl` rolls up to `<sid>`; orchestrator-vs-subagent is a *dimension* of a session, not a session boundary. Call totals are scope-independent, session counts are not — so counting sidechain files as their own sessions moves Serena from 13 sessions to 31 while leaving its 464 lifetime calls untouched. Every session count in the output names its scope: `sess` is either-scope, `orch sess` and `sub sess` split it.

The split is the point, not a detail: the same instruction delivered at SubagentStart draws far more use than at SessionStart (`plan_search`: 12 orchestrator calls against 282 subagent calls), and a census that summed the two would hide it.

**Two denominators, always.** `ALL MEASURABLE SESSIONS` and `SUBSTANTIAL SESSIONS` (at least 50 calls). Downstream work pre-registers predictions against the substantial subset, so emitting only one would leave those unfalsifiable against this command's own output.

**Exits non-zero when the run measured *nothing*.** An empty, unreadable, unparseable, or tool-call-free scope reports `UNMEASURABLE` with a reason rather than a clean zero; a zero row inside a measurable run is a real zero. Sessions that legitimately carry no tool call are the majority of transcripts — they are counted, listed by reason, and reported as a share, but they do not fail the run. The exit code answers "did this measure anything at all", **not** "is this corpus healthy"; a caller needing a health threshold must read `unmeasurable_share`, not `$?`.

**It reports counts and refuses a verdict.** Non-use of a capability may be a forgotten affordance or a correct rejection, and nothing in the transcript distinguishes them; `--json` carries a `verdict: null` field and per-tool counts so narrower slices stay derivable. The refusal governs what this command renders — it is not, and cannot be, an enforcement boundary against verdicts computed downstream from these numbers.

```
nx census dispatches [--session SESSION_ID] [--project-dir PATH] [--json]
```

Recognizes every `Agent` tool_use block in a transcript — `(subagent_type, ordinal, description)` — as `(subagent_type, ordinal)` (nexus-h33x8.2). This is the transcript-based recognizer nexus-nu7fo's RDR-184 Gap-1 ledger could never build for itself: the guard keyed dispatches on a `a<name>-<hash>` name-morphology, but the `Agent` tool has no `name` parameter, so `recognized` was structurally pinned at 0 across four consecutive sessions (0/6, 0/10, 0/7, 0/2). Every `Agent` block already carries `input.subagent_type` and `input.description` — this command reads exactly that, reusing `nx census capability`'s transcript walk and roll-up rule rather than re-deriving them.

Each row carries **two ordinals**: `session_ordinal` numbers every dispatch in the session in transcript order; `type_ordinal` numbers only the occurrences of that row's own `subagent_type` (so the second `conexus:code-review-expert` dispatch in a session is `type_ordinal=2`). **Neither ordinal is a ledger pairing key** — `tests/e2e/lib/expectations.sh` documents why an ordinal invented on the recognizer's side can never be paired against what the `SubagentStart` hook writes (no per-instance identifier survives that far). The ledger's own credit matching is N-of-type (N `EXPECT` rows of a type against N `START`s of that type); this command's `type_counts` is that same N, and the ordinals exist as a human "which one was #2" display aid, not a join key.

`subagent_type` in each row is the **sanitized, ledger-consumable form** — pass it straight to `expectations_expect`. For a plugin-namespaced type like `conexus:substantive-critic` this is the value UNCHANGED, colon included: per AGENTS.md's hot-rule convention ("keyed on the subagent type verbatim, colon included — never an invented name"), the ledger's charset already accepts the colon, and sanitizing it away would recreate the exact pairing failure nexus-nu7fo spent four sessions diagnosing. Sanitization only transforms a value the ledger's charset would otherwise reject outright (a leading non-alphanumeric character, an interior character outside `[A-Za-z0-9_:-]`, an empty string, or over 64 characters); a row whose value changed is marked with `subagent_type_sanitized_changed: true` (`*` in the text table). If two *distinct* raw values sanitize to the same key, that is reported under `sanitize_collisions` rather than silently merged — a downstream consumer must not double-credit two different agent types as one. An `Agent` block with no `input.subagent_type` at all is still enumerated (dropping it would break the recognized-count-equals-raw-count property), flagged `subagent_type_missing: true` (with `subagent_type_raw: null`), and keyed as `general-purpose` — **not** an invented placeholder: `conexus/hooks/scripts/agent-dispatch-expect.sh` (nexus-a795d) computes its own EXPECT-row name identically for the same omitted field (`str(ti.get("subagent_type") or "general-purpose")`), because the harness genuinely starts a `general-purpose` agent when the field is absent. Keying these rows any other way would make "pass `subagent_type` straight to `expectations_expect`" false for exactly the rows that most need it (28 of 1304 dispatches in the live corpus at last count).

A non-`Agent` tool_use block that nonetheless carries `input.subagent_type` is flagged as a **suspect block** (`suspect_blocks` per session, `suspect_blocks_total` at the top level) — the signature of an Agent-block-specific rename or restructure that would otherwise silently zero out recognition while every other tool_use in the same transcript keeps parsing normally, so nothing would go `UNMEASURABLE`. This is warn-only and never affects `exit_code`; the precedent is `agent-dispatch-expect.sh` itself still special-casing `"Task"` as "the pre-rename spelling of the same tool" alongside `"Agent"`.

A session that dispatched nothing is a **measured zero**, not unmeasurable — dispatch non-use is not the defect class this command exists to catch. The UNMEASURABLE-vs-zero contract is otherwise identical to `nx census capability`: an empty, unreadable, unparseable, or genuinely tool-call-free transcript reports `UNMEASURABLE` and a nonzero exit; a session with tool calls but zero `Agent` blocks reports zero dispatches and a zero exit. A session with unparseable trailing lines is still measurable but reported `PARTIAL` (text and JSON both) — the dispatch count above it may be an undercount, never a silently clean total.

**Scope fence (binding):** this command supplies the recognizer only. It does not read or write `tests/e2e/lib/expectations.sh`, does not compute `undeclared`/`BLINDSPOT`, and does not decide how (or whether) the ledger adopts its output — that is nexus-nu7fo's resolution, already closed via the dispatch-expect hook + the AGENTS.md convention above.

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
