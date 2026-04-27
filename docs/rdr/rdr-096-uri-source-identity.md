---
title: "RDR-096: URI-Based Source Identity for Aspect Extraction"
id: RDR-096
type: Architecture
status: draft
priority: medium
author: Hal Hildebrand
reviewed-by: self
created: 2026-04-27
accepted_date:
related_issues:
  - "#331 — nx enrich aspects writes null-field rows when extractor fails to read source"
  - "#332 — Source identity should be a URI, not a filesystem path"
  - "#333 — scholarly-paper-v1 should fall back to chunk-text reassembly from chroma"
related_tests: [test_aspect_extractor.py, test_enrich_aspects_cmd.py, test_document_aspects.py]
related: [RDR-070, RDR-086, RDR-089, RDR-095]
---

# RDR-096: URI-Based Source Identity for Aspect Extraction

The aspect-extraction framework (RDR-089) treats `source_path` as a relative filesystem path universally. That assumption holds for one ingest pathway (filesystem-backed PDFs and markdown) and breaks for every other source shape we already support: Confluence fetches, web research, in-session synthesis, S3-backed corpora, RDR drafts whose names changed on disk. When the assumption breaks the framework writes a row with all-null aspect fields, leaves it in `document_aspects` with the current `model_version`, and silently corrupts every downstream operator SQL fast path that treats the row as data. This RDR replaces filesystem-path identity with URI identity, scheme-keyed reader dispatch, and a failure-mode contract that does not produce null rows.

## Problem Statement

### Enumerated gaps to close

#### Gap 1: Source identity is filesystem-shaped but real sources are not

`document_aspects.source_path` is treated as a relative path that `open()` consumes. In the live `nexus_rdr` collection 12 entries (~3% of catalog) point to renamed or deleted RDR file paths (research-1003 from RDR-090 spike: rdr-066 / rdr-067 / rdr-068 / rdr-069 renamed during drafting; cce-query-mismatch.md moved to post-mortem subdir; worktree paths from defunct agent worktrees). In `knowledge__knowledge` all 17 documents are slug-shaped strings (`bito-indexing-mechanics`, `gap-analysis-bito-vs-nexus`) that were never on the filesystem to begin with — they came from Confluence fetches, web research, in-session synthesis. The slug becomes the persisted `source_path` because that's the only column the schema offers, and `open(slug)` raises `FileNotFoundError` at extraction time.

#### Gap 2: Read failure produces silent null-field rows that pollute downstream SQL

When `extract_aspects` cannot read a source, the existing pipeline still upserts an `AspectRecord` to `document_aspects` with `problem_formulation=null`, `proposed_method=null`, `experimental_datasets=[]`, `experimental_baselines=[]`, `experimental_results=null`, `extras={}`, `confidence=null`, but with the current `model_version` and `extractor_name`. This sticks the row past three downstream behaviors:

1. `--re-extract` skips it (lexicographic-strict-less-than `model_version` comparison).
2. Operator SQL fast paths (`operator_filter`, `operator_groupby`, `operator_aggregate`) see the row as data and include it in matches, group counts, and aggregates.
3. The 3× retry inside the extractor wastes Claude API budget before the null row is recorded.

The fix is not a one-off "skip on failure" guard. The framework lacks a typed failure contract — every failure mode collapses into "row exists with null fields", indistinguishable at SQL level from a paper that genuinely has no problem formulation.

#### Gap 3: Knowledge collections are systematically un-extractable today

`knowledge__knowledge` (and any future knowledge collection backed by Confluence, web fetch, RFC archives, in-session synthesis) cannot be aspect-extracted today, even though the document text is fully present in chroma — `nx_answer` and `query` both return rich content from these documents. The chunks are in the embedding store; the slug-shaped `source_path` is the only thing standing between the extractor and the text.

## Context

### Background

RDR-089 (Structured Aspect Extraction at Ingest, accepted 2026-04-25, closed 2026-04-25) added the document-grain hook chain, the `aspect_extraction_enqueue_hook`, the async worker, and the synchronous Claude-CLI extractor with `scholarly-paper-v1` config keyed on `knowledge__*` prefix and `rdr-frontmatter-v1` on `rdr__*`. The 2026-04-27 spike for RDR-090 (research-1003) backfilled 358 catalog entries for `rdr__nexus-571b8edd` and surfaced 12 null-field rows from catalog stale paths. A separate instance subsequently filed #331/#332/#333 after hitting the same root cause via `knowledge__knowledge`. The two failure paths are the same shape under different surface labels.

### Technical Environment

- `src/nexus/aspect_extractor.py` — synchronous Claude-CLI extractor; scholarly-paper-v1 + rdr-frontmatter-v1 configs; null-byte defense; content-sourcing fallback.
- `src/nexus/aspect_worker.py` — async daemon-thread drain of T2 `aspect_extraction_queue`; document-grain hook registration.
- `src/nexus/db/t2/document_aspects.py` — `AspectRecord` dataclass + `DocumentAspects` store (upsert / list_by_collection / list_by_extractor_version).
- `src/nexus/commands/enrich.py` — `nx enrich aspects` command; iterates the catalog (one entry per source document); calls `extract_aspects` directly bypassing `fire_post_document_hooks`.
- `src/nexus/catalog/catalog.py` — catalog tables; `file_path` field; `register` / `update` / `resolve` semantics.
- `document_aspects` schema (T2) — `(collection, source_path)` composite key; `extras` JSON column.

### Empirical evidence

- Research-1003 (RDR-090 spike, 2026-04-27): 12 null-field rows across `rdr__nexus-571b8edd` from rename drift + worktree paths + docs reorganization.
- Issue #331 reproducer: 17 null-field rows across `knowledge__knowledge` from Confluence + web fetch slugs.
- T2 `nexus_rdr / 090-research-3` (id 1003) records the catalog-staleness path.

## Research Findings

### Investigation

The two failure populations (rename drift + slug-shaped sources) reach the same upsert path through different ingest histories. The catalog's `file_path` column carries whatever was passed at `nx catalog register` time. For filesystem-backed ingests that's an absolute or relative path. For knowledge-collection ingests that's a slug derived from the source title. The aspect extractor's read step (`open(catalog_entry.file_path)`) cannot distinguish between "path was always this slug" and "path used to exist but was renamed".

The chunks are present in chroma in both cases. RDR-086's `chash` (chunk content hash) plus the chunk's `chunk_index` metadata is sufficient to reassemble document text in order — the extractor's `scholarly-paper-v1` prompt (problem / method / datasets / baselines / results) is robust to chunking-overlap artifacts since it already handles multi-page papers.

### Dependency Source Verification

| Dependency | Source Searched? | Key Findings |
| --- | --- | --- |
| `chromadb.Collection.get(where=...)` | Yes (`src/nexus/db/t3.py`) | Stable API; `where={"source_path": value}` returns chunks for a document. Pagination cap is 300 (`chroma_quotas.MAX_QUERY_RESULTS`); knowledge documents are well under this per-doc. |
| Confluence URI shape | External | `https://<tenant>.atlassian.net/wiki/spaces/<space>/pages/<id>/<slug>` — already known to the ingest tooling that fetched the content. |
| `urllib.parse` URI scheme dispatch | stdlib | Stable since Python 2; `urlparse(uri).scheme` is canonical. |
| Existing `chash:` URL scheme | Yes (`src/nexus/doc/citations.py`) | RDR-086 already has chash-shaped citations in the codebase; URI dispatch is symmetric to it. |

### Key Discoveries

- **Verified** (research-1003): chunks for the 12 catalog-stale RDRs are still present in `rdr__nexus-571b8edd` (the basename-prefix grep shows current versions indexed under new names). The stale-path failures are about identity, not data loss.
- **Verified** (issue #333 reproducer): `knowledge__knowledge` documents are reassembled correctly by `nx_answer` and `query` (rich content returned) — the data is there; only the read path is broken.
- **Verified** (RDR-089 close-mortem): scholarly-paper-v1's prompt already absorbs chunking-overlap artifacts on multi-page papers; chunk-reassembled text from chroma is in the same shape category.
- **Assumed**: scheme-dispatched read latency does not regress the dominant `file://` path (the existing case). Verify via spike: 100-doc filesystem-backed extraction wall-clock before/after URI dispatch.
- **Assumed**: a single `source_uri` column with scheme prefix is sufficient — no per-scheme metadata column needed (Confluence page IDs, S3 bucket names, etc. fit inside the URI string). Verify by drafting URIs for all six origin shapes from the table below before committing the schema.

### Critical Assumptions

- [ ] Chroma chunk reassembly produces extractable text for `scholarly-paper-v1` on `knowledge__*` collections — **Status**: Unverified — **Method**: Spike on 5 documents from `knowledge__knowledge`, run `extract_aspects` on the reassembled text, compare extracted aspects against operator-verify on the original chunks.
- [ ] Backfill of `source_uri` from existing `source_path` is unambiguous for filesystem-backed collections (1:1 mapping `file://" + os.path.abspath(source_path)`) — **Status**: Unverified — **Method**: Spike across all current `rdr__*` and `docs__*` collections; report any `source_path` that doesn't round-trip cleanly to a `file://` URI and back.
- [ ] No-null-row contract does not regress aspect coverage for partial-extraction successes (e.g., extractor returns problem_formulation but not proposed_method) — **Status**: Unverified — **Method**: Audit RDR-089's existing extractor outcomes for the partial-success case; current behavior may already write rows with mixed null/non-null fields by design. The null-row footgun is read-failure-only, not partial-extraction.

## Proposed Solution

### Approach

Replace `source_path` (filesystem-shaped string) with `source_uri` (scheme-prefixed URI string) as the persisted source identity. Introduce a scheme-keyed reader dispatch that routes URI reads by scheme. Replace the implicit "write null row on failure" path with an explicit Result-shaped reader contract: success returns text, failure returns a typed error sentinel that the upsert-guard recognises and skips.

### Technical Design

**URI shape table** (six initial schemes; pluggable):

| Origin | URI shape | Reader |
|---|---|---|
| Filesystem markdown / PDF | `file:///abs/path/to/file.pdf` | `_read_file_uri` — existing `open()` path, wrapped |
| Confluence page | `https://<tenant>.atlassian.net/wiki/spaces/<space>/pages/<id>/<slug>` | `_read_chroma_uri` (preferred) or `_read_https_uri` for live fetch |
| Web research | `https://docs.bito.ai/...` | `_read_chroma_uri` (preferred) or `_read_https_uri` |
| In-session synthesis | `nx-scratch://session/<session-id>/<entry-id>` | `_read_scratch_uri` |
| Embedding-only | `chroma://<collection>/<source-identifier>` | `_read_chroma_uri` — reassemble chunks from chroma |
| RDR / docs (FS-shadow) | `file:///abs/repo/docs/rdr/rdr-090-...md` | `_read_file_uri` |

**Reader contract**:

```python
# Illustrative; final shape defined in src/nexus/aspect_readers.py
@dataclass(frozen=True)
class ReadOk:
    text: str
    metadata: dict  # scheme, content_type, ingested_at, etc.

@dataclass(frozen=True)
class ReadFail:
    reason: Literal["unreachable", "unauthorized", "scheme_unknown", "empty"]
    detail: str  # operator-readable

ReadResult = ReadOk | ReadFail

def read_source(uri: str, *, t3=None, scratch=None) -> ReadResult: ...
```

**Upsert-guard**:

```python
def extract_aspects(uri: str, ...) -> AspectRecord | ExtractFail:
    result = read_source(uri, ...)
    if isinstance(result, ReadFail):
        return ExtractFail(uri=uri, reason=result.reason, detail=result.detail)
    aspects = _claude_extract(result.text, ...)
    return AspectRecord(...)

# In nx enrich aspects:
for entry in catalog_entries:
    record_or_fail = extract_aspects(entry.source_uri, ...)
    if isinstance(record_or_fail, ExtractFail):
        log.warning("aspect_extract_skip", uri=entry.source_uri, reason=record_or_fail.reason)
        continue  # NO row written
    document_aspects.upsert(record_or_fail)
```

**Schema migration**:

Add `source_uri TEXT` to `document_aspects` and to the catalog tables. Backfill in two steps:

1. Read-time migration: for any row missing `source_uri`, derive it from `source_path` at SELECT time (`COALESCE(source_uri, 'file://' || source_path)`).
2. Background backfill: a one-shot migration writes the derived URI back. For knowledge collections without filesystem backing, the URI is `chroma://<collection>/<source_path>` — chunk-reassembly path becomes the canonical read.

`source_path` is retained for two releases as a deprecated alias column (read for back-compat, not written by new code paths).

**`chroma://` reader implementation**:

```python
def _read_chroma_uri(uri: str, t3) -> ReadResult:
    # uri = "chroma://<collection>/<source-identifier>"
    parsed = urlparse(uri)
    collection = parsed.netloc
    source_id = parsed.path.lstrip("/")
    try:
        coll = t3._client.get_collection(collection)
        # Pagination — chroma_quotas.MAX_QUERY_RESULTS = 300
        chunks = []
        offset = 0
        while True:
            page = coll.get(
                where={"source_path": source_id},
                limit=300, offset=offset,
                include=["documents", "metadatas"],
            )
            docs = page.get("documents") or []
            if not docs:
                break
            chunks.extend(zip(page["metadatas"], docs))
            offset += 300
            if len(docs) < 300:
                break
        if not chunks:
            return ReadFail(reason="empty", detail=f"no chunks for {source_id} in {collection}")
        chunks.sort(key=lambda pair: pair[0].get("chunk_index", 0))
        return ReadOk(
            text="\n\n".join(doc for _, doc in chunks),
            metadata={"scheme": "chroma", "chunk_count": len(chunks)},
        )
    except Exception as e:
        return ReadFail(reason="unreachable", detail=f"{type(e).__name__}: {e}")
```

### Existing Infrastructure Audit

| Proposed Component | Existing Module | Decision |
| --- | --- | --- |
| `src/nexus/aspect_readers.py` | none | **New**: scheme-dispatched reader registry |
| `source_uri` column | `document_aspects` table | **Schema migration** alongside back-compat read of `source_path` |
| `chroma://` URI scheme | RDR-086 `chash:` scheme precedent | **Add scheme**: symmetric with chash citations |
| Upsert-guard refactor | `commands/enrich.py:extract loop` | **Refactor**: typed Result return value replaces null-row write |
| Catalog `source_uri` field | `catalog/catalog.py` | **Add field**: backfilled on first migration; ingest paths populate going forward |

### Decision Rationale

URI identity is symmetric with how content actually enters the store. `nx ingest` already deals with web URLs, filesystem paths, and Confluence/web-fetch tooling at ingest time — the bug is that source identity collapses to a slug or relative path at the persistence boundary, losing the scheme. URI scheme dispatch restores the scheme that was always present at ingest.

The Result-shaped reader contract eliminates the null-row footgun by structural guarantee, not by guard-clause bandaid. A reader that returns `ReadFail` cannot accidentally produce a row; the upsert path takes a `ReadOk`-derived `AspectRecord` only.

The `chroma://` scheme has zero runtime fetch cost (chunks already in store) and unblocks knowledge collections immediately. It's also the long-term canonical read for any source where re-fetching upstream is cost-prohibitive or non-deterministic (deleted Confluence pages, paywalled research, regenerated session synthesis).

The two-release deprecation window for `source_path` is conservative — the dual-read path (`COALESCE(source_uri, 'file://' || source_path)`) lets every existing reader continue to work while writers transition.

## Alternatives Considered

### Alternative 1: Per-scheme columns on `document_aspects` (no URI string)

**Description**: Add `source_kind`, `source_collection`, `source_url`, `source_filesystem_path`, etc. as separate columns; the existing extractor branches on `source_kind`.

**Pros**: SQL-native — no URL parsing in the read path.

**Cons**: Schema bloat scales with scheme count. Adding `s3://`, `git://`, `arxiv://` later requires a migration each time. Plugin-shaped reader registry is harder. URI is the recognized identity-string convention for this exact problem.

**Reason for rejection**: URI strings are extensible without schema migrations; new schemes register a reader, not a column.

### Alternative 2: Just fix #331 (don't write null rows) and defer #332/#333

**Description**: Add a guard-clause in `extract_aspects` that returns early on read failure without upserting; leave `source_path` as filesystem-shaped.

**Pros**: Minimal, ships in a day.

**Cons**: Closes the symptom, leaves `knowledge__*` collections systematically un-extractable. The data is in chroma; we just can't read it because `source_path` isn't a path. A guard-clause without a URI scheme means we never re-attempt those documents, which is silent failure of a different shape (database has no row, but no reason recorded that another extractor pass should retry).

**Reason for rejection**: The architectural fix and the symptom fix collapse into roughly the same change set when both are done coherently. Doing only the guard-clause leaves the larger gap open and creates a follow-up RDR debt.

### Alternative 3: Chroma fallback as a flag on `scholarly-paper-v1` only (issue #333 alone)

**Description**: Implement chunk-reassembly as a fallback inside the `scholarly-paper-v1` extractor when `open(source_path)` fails. No URI scheme, no schema change.

**Pros**: Smaller diff; closes #333's concrete case.

**Cons**: Couples reader logic to extractor implementation. Future extractors (rdr-frontmatter-v1 or the eventual knowledge-doc-v1) need their own fallback. The reader/extractor separation that URI dispatch provides is the architectural unit.

**Reason for rejection**: Reader pluralism is the right boundary — extractors operate on text, readers retrieve text from a source. The two should not be coupled per-extractor.

### Briefly Rejected

- **Drop `document_aspects` entirely; treat aspects as derived data on every read.** Rejected — operator SQL fast paths require persistent rows; aspects are the unit of cross-document analysis.
- **Move all sources behind a uniform `chroma://` URI even for filesystem-backed collections.** Rejected — keeps the option but pre-canonicalizing all sources to chroma loses the round-trip path back to the original file for re-indexing or content updates. URI scheme should reflect actual provenance.

## Trade-offs

### Consequences

- Schema migration for `document_aspects` and the catalog tables (one ALTER TABLE; back-compat dual-read).
- New module `src/nexus/aspect_readers.py` with scheme registry and per-scheme reader.
- `nx enrich list` and `nx enrich info` surfaces gain a `scheme:` column.
- Extractor configs become `(scheme, collection_prefix)` keyed instead of `collection_prefix`-only — opens the door to scheme-specific extractors (Confluence-aware, arxiv-aware) without further architectural change.
- The 12 null-field rows in `nexus_rdr` and the 17 in `knowledge__knowledge` get cleaned up by a one-shot migration that drops them (they violate the new no-null-row invariant).

### Risks and Mitigations

- **Risk**: URI parsing introduces a new failure surface (malformed URIs at write time produce ingest errors that didn't exist before).
  **Mitigation**: Validation at the catalog `register` boundary; a malformed URI is a hard error there, not a silent persistence.

- **Risk**: `chroma://` reader's reassembled text differs subtly from filesystem-read text (chunking overlap, missing prefatory metadata) in ways scholarly-paper-v1 hasn't been tested against.
  **Mitigation**: Spike (Critical Assumption #1 above) compares extracted aspects on chroma-reassembled vs filesystem-read text for 5 known-FS-backed papers; gate on `operator_verify` agreement.

- **Risk**: Two-release deprecation window is too short — external consumers of `document_aspects` reads via raw SQL might break.
  **Mitigation**: SQL fast paths in `operator_*` use `COALESCE(source_uri, 'file://' || source_path)` for the entire window. The deprecation is on the writer side, not the reader side.

- **Risk**: Scheme dispatch latency regresses the dominant `file://` case.
  **Mitigation**: Spike measures wall-clock on a 100-doc FS-backed batch before vs after; reader registry uses a dict lookup, not regex, so the overhead is sub-microsecond per call.

### Failure Modes

- **Visible**: A new scheme is registered without a reader → `read_source` returns `ReadFail(reason="scheme_unknown")` → upsert-guard skips → `nx enrich list` shows the document as not-yet-extracted with the scheme value visible. Log emits `aspect_reader_scheme_unknown` with the scheme.
- **Visible**: Backfill leaves a row with `source_uri=NULL` and `source_path=NULL` (corrupt prior state) → migration warns and dumps the row's catalog ID for manual triage.
- **Silent**: `chroma://` reader returns `ReadOk` with empty text (collection has zero chunks for the source identifier). Mitigated by `ReadFail(reason="empty", ...)` short-circuit when `len(chunks) == 0`.

## Implementation Plan

### Prerequisites

- [ ] Spike: 5-doc chroma-reassembly extraction on `knowledge__knowledge` with `operator_verify` cross-check against the extracted aspects on filesystem-read versions of the same papers (where filesystem versions exist). Measure wall-clock; verify scholarly-paper-v1 prompt is robust to chunk-overlap artifacts.
- [ ] Spike: backfill dry-run across all current `rdr__*` and `docs__*` collections; report any `source_path` value that does not cleanly round-trip to a `file://` URI.
- [ ] T2 schema review: `source_uri TEXT` column on `document_aspects` and `catalog_documents`; backfill SQL; deprecation policy on `source_path`.

### Minimum Viable Validation

A single `nx enrich aspects knowledge__knowledge --dry-run` invocation completes without `aspect_extractor_source_path_unreadable` warnings, lists 17 documents to extract via `chroma://` reader, and reports the per-document chunk count.

### Phase 1: Reader registry + `chroma://` scheme (closes #333)

#### Step 1: `src/nexus/aspect_readers.py`

Reader registry, `ReadOk` / `ReadFail` Result types, dispatch helper, and the `_read_file_uri` + `_read_chroma_uri` initial implementations.

#### Step 2: Wire reader into `aspect_extractor.extract_aspects`

Replace the `open(source_path)` call site with `read_source(uri, t3=t3, ...)`. Read failures propagate to a typed `ExtractFail` instead of a null-field record.

#### Step 3: Wire upsert-guard into `commands/enrich.py`

Iteration over catalog entries calls `extract_aspects`; on `ExtractFail` log and skip; on `AspectRecord` upsert. **No null rows.** (closes #331)

### Phase 2: Schema migration + backfill

#### Step 1: Add `source_uri TEXT` column

To `document_aspects` and `catalog_documents`. Migration backfills `source_uri = 'file://' || abs(source_path)` for filesystem-backed collections; `source_uri = 'chroma://' || collection || '/' || source_path` for knowledge collections without FS backing.

#### Step 2: Drop null-field rows from existing tables

One-shot delete: rows where `problem_formulation IS NULL AND proposed_method IS NULL AND extras = '{}'`. Audit count first; emit summary.

#### Step 3: Dual-read in operator SQL

`COALESCE(source_uri, 'file://' || source_path)` in every `operator_*` SELECT for the deprecation window.

### Phase 3: Catalog `source_uri` surface

#### Step 1: `nx catalog show`, `nx catalog register` accept and display URIs

Backward compat: bare paths normalize to `file://` URIs at the catalog boundary.

#### Step 2: `nx enrich list` and `nx enrich info` surface `scheme:` column

Operator pre-filtering by scheme (e.g. "list all chroma-only documents to manually review").

### Phase 4: Additional schemes (post-MVP)

#### Step 1: `nx-scratch://` reader

For in-session synthesis documents that get persisted to T3 but originated in scratch.

#### Step 2: `https://` reader with cache

Re-fetch only when explicitly requested; default is "use chroma" if available. Useful for paywalled / dynamic upstreams.

### Phase 5: Deprecation

#### Step 1: Stop writing `source_path` from new ingest paths

All writes use `source_uri`. `source_path` is read-only.

#### Step 2: After two releases, drop `source_path` column

Migration removes the column; operator SQL switches to direct `source_uri` reads.

### Day 2 Operations

| Resource | List | Info | Delete | Verify | Backup |
| --- | --- | --- | --- | --- | --- |
| `document_aspects` rows | `nx enrich list COLLECTION` | `nx enrich info COLLECTION URI` | `nx enrich delete COLLECTION URI` | `nx enrich aspects COLLECTION --dry-run` | T2 backup |
| Reader registry | (in-process) | log line on dispatch | N/A | unit tests | git |
| URI backfill state | migration version table | `nx doctor --check-schema` | N/A | row count assertions | T2 backup |

### New Dependencies

None. `urllib.parse` is stdlib; chroma client is already a dependency.

## Test Plan

- **Scenario**: `nx enrich aspects knowledge__knowledge` with 17 unreadable-on-FS documents — **Verify**: All 17 produce non-null `AspectRecord`s via `chroma://` reader; zero `aspect_extractor_source_path_unreadable` warnings.
- **Scenario**: `nx enrich aspects rdr__nexus-571b8edd` after RDR rename drift — **Verify**: 12 catalog-stale entries are either (a) reachable via chroma reassembly and produce non-null rows or (b) cleanly skipped with `ExtractFail(reason="empty")` and zero null rows written.
- **Scenario**: Backfill migration on a snapshot of current `document_aspects` — **Verify**: Every row gets a populated `source_uri`; null-field rows are dropped; `COALESCE` dual-read returns the same data as direct `source_uri` read.
- **Scenario**: Operator SQL fast paths post-migration — **Verify**: `operator_filter("collection=knowledge__knowledge AND problem_formulation NOT NULL")` returns the actual extracted set, not phantom null rows.
- **Scenario**: New scheme registered without reader — **Verify**: `nx enrich aspects` skips the document, logs `aspect_reader_scheme_unknown`, no row written.
- **Scenario**: Scheme-dispatch latency regression test — **Verify**: 100-doc FS-backed extraction wall-clock ≤ 110% of pre-migration baseline.

## Validation

### Testing Strategy

Unit tests for `read_source` per scheme (file, chroma, scratch — https/s3 deferred). Integration test: full `nx enrich aspects` cycle on a fixture corpus containing one filesystem-backed paper, one Confluence slug, one in-session synthesis doc; verify all three produce extractable aspects via their respective readers. Migration test on a captured snapshot of `document_aspects` from a real installation. Cross-validate against operator SQL fast paths to ensure the dual-read window is transparent to consumers.

### Performance Expectations

- File-scheme dispatch overhead: <1 microsecond per call (dict lookup).
- Chroma-reader reassembly: ~50–200 ms per 10-page document (one paginated `coll.get`); knowledge documents are typically 1–3 pages so well under 100 ms.
- Backfill migration on 50K aspect rows: ~5 seconds (single SQL UPDATE).

## Finalization Gate

_To be completed during /nx:rdr-gate._

## References

- Issue #331 — `nx enrich aspects` writes null-field rows when extractor fails to read source
- Issue #332 — Source identity should be a URI, not a filesystem path
- Issue #333 — `scholarly-paper-v1` should fall back to chunk-text reassembly from chroma
- RDR-070 — Taxonomy at ingest (post-store hook framework precedent)
- RDR-086 — Content-Hash Chunk Index (`chash:` URL scheme precedent)
- RDR-089 — Structured Aspect Extraction at Ingest (introduces the framework this RDR repairs)
- RDR-090 — Realistic AgenticScholar Benchmark (research-1003 documented the catalog-staleness symptom on `rdr__nexus-571b8edd`)
- RDR-095 — Post-Store Hook Framework: Batch Contract (parallel framework-shape work)
