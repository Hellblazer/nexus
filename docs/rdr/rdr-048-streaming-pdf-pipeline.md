---
title: "Streaming PDF Pipeline"
id: RDR-048
type: Architecture
status: closed
accepted_date: 2026-04-03
priority: medium
author: Hal Hildebrand
reviewed-by: self
created: 2026-04-03
epic: "nexus-qwxz"
related_issues:
  - "RDR-047 - Large PDF Extraction Resilience (accepted)"
  - "nexus-u0q5 - Epic: Large PDF Extraction Resilience"
---

# RDR-048: Streaming PDF Pipeline

> Revise during planning; lock at implementation.
> If wrong, abandon code and iterate RDR.

## Problem Statement

RDR-047 fixed the costliest failure mode — losing 30 minutes of embedding work — by checkpointing the embed/upsert phase. But two of the three accumulation points identified in RF-7 remain unfixed:

1. **Extraction accumulates all pages in memory.** `_extract_with_mineru()` joins all page batches into `md_parts` before returning. For a 771-page book, this is ~2-5 MB of markdown text held in RAM. A crash during extraction loses all extracted pages.

2. **Chunking accumulates all chunks in memory.** `PDFChunker.chunk()` takes the full document text and returns all chunks at once. For 2000+ chunks, this is another multi-MB allocation. A crash after chunking but before the first embed/upsert checkpoint loses all chunk computation.

3. **Sequential pipeline.** Extraction must finish completely before chunking starts. Chunking must finish completely before embedding starts. For a 771-page book: ~5 min extraction + ~2s chunking + ~5 min embedding = ~10 min total, all sequential. The CPU sits idle during API calls; the API sits idle during extraction.

### Root Cause

The pipeline was designed as a function composition: `extract(pdf) → chunk(text) → embed(chunks) → upsert(records)`. Each stage consumes the full output of the previous stage. This is simple and correct for 20-page papers but creates unbounded memory and failure-cost scaling for large documents.

### Impact

- A crash during extraction of a 771-page book loses all extracted pages (up to 5 min of work)
- Memory usage scales linearly with document size — no backpressure
- Pipeline latency is the sum of all stages — no overlap possible
- Cannot leverage local ONNX embedding while waiting for API responses

## Proposed Solution

### Architecture: Three-Stage Producer-Consumer Pipeline

Replace the sequential function composition with a streaming pipeline backed by a persistent buffer (SQLite table). Three stages run as concurrent threads, connected by the buffer:

```
 ┌───────────┐     ┌─────────────────┐     ┌───────────┐     ┌─────────────────┐     ┌──────────┐
 │ Extractor │────>│  Buffer (pages)  │────>│  Chunker  │────>│ Buffer (chunks)  │────>│ Uploader │
 │  Thread   │     │  SQLite table    │     │  Thread   │     │  SQLite table    │     │  Thread  │
 └───────────┘     └─────────────────┘     └───────────┘     └─────────────────┘     └──────────┘
      │                                          │                                        │
      │ Writes pages as                          │ Reads N chars,                         │ Reads embedded
      │ they're extracted                        │ chunks, embeds,                        │ chunks, upserts
      │                                          │ writes to chunk                        │ to T3
      │                                          │ buffer                                 │
      ▼                                          ▼                                        ▼
  MinerU/Docling                          PDFChunker +                              ChromaDB T3
  (page batches)                          Voyage/ONNX embed                         (upsert batches)
```

### Buffer Schema (SQLite — reuse T2 or dedicated file)

**Text join contract (C1)**: The extractor writes one row per page to `pdf_pages`. The chunker reconstructs the full text as `'\n'.join(row.page_text for row in pages ORDER BY page_index)` — identical to the current `"\n".join(md_parts)` in `_extract_with_mineru()` and `"\n".join(page_texts)` in `_extract_with_docling()`. This contract ensures chunk boundaries are identical between streaming and batch paths.

**Chunker resume is idempotent**: On crash and restart, the chunker re-reads all pages from the buffer and re-chunks from the start. Chunks that already exist in `pdf_chunks` are skipped via `INSERT OR IGNORE` (same primary key = same chunk). Already-embedded chunks retain their embeddings. No cursor column needed — deterministic chunking + idempotent writes = free resume.

```sql
-- Page buffer: extractor writes, chunker reads
CREATE TABLE pdf_pages (
    content_hash TEXT NOT NULL,
    page_index   INTEGER NOT NULL,
    page_text    TEXT NOT NULL,
    metadata_json TEXT DEFAULT '{}',
    created_at   TEXT NOT NULL,
    PRIMARY KEY (content_hash, page_index)
);

-- Chunk buffer: chunker writes with embedding, uploader reads
CREATE TABLE pdf_chunks (
    content_hash  TEXT NOT NULL,
    chunk_index   INTEGER NOT NULL,
    chunk_text    TEXT NOT NULL,
    chunk_id      TEXT NOT NULL,
    metadata_json TEXT DEFAULT '{}',
    embedding     BLOB DEFAULT NULL,  -- NULL until embedded
    uploaded      INTEGER DEFAULT 0,  -- 0=pending, 1=uploaded
    created_at    TEXT NOT NULL,
    PRIMARY KEY (content_hash, chunk_index)
);

-- Pipeline state: tracks overall progress (RF-16 state machine)
CREATE TABLE pdf_pipeline (
    content_hash     TEXT PRIMARY KEY,
    pdf_path         TEXT NOT NULL,
    collection       TEXT NOT NULL,
    total_pages      INTEGER,
    pages_extracted  INTEGER DEFAULT 0,
    chunks_created   INTEGER,               -- NULL until chunker sets explicitly
    chunks_embedded  INTEGER,               -- NULL until chunker sets explicitly
    chunks_uploaded  INTEGER DEFAULT 0,
    status           TEXT DEFAULT 'running',  -- running|completed|failed|resuming
    error            TEXT DEFAULT '',          -- error details when status='failed'
    started_at       TEXT NOT NULL,
    updated_at       TEXT NOT NULL
);
```

### Stage 1: Extractor Thread

Writes pages to `pdf_pages` as they arrive from MinerU/Docling page batches. Each batch write is a single transaction. On crash, only the in-flight batch is lost (default 1 page; configurable via `mineru_page_batch`). On resume, reads `pages_extracted` from `pdf_pipeline` and skips completed pages.

MinerU already works in page batches internally (`get_mineru_page_batch()`). The change is: instead of appending to `md_parts` list, insert into `pdf_pages`.

**Concurrent pipeline guard (S3)**: Before starting, check `SELECT status FROM pdf_pipeline WHERE content_hash = ?`. If a `running` row exists, the pipeline is already active — log a warning and skip (same semantics as the current `fcntl.flock` contention guard). If a `failed` or `resuming` row exists, take ownership by setting `status = 'resuming'` and resume from the buffer state. If no row exists, `INSERT` a new `running` row.

### Stage 2: Chunker Thread

Polls `pdf_pages` for accumulated text. When `N` characters are available (where `N` >= chunk target size, e.g., 1500 chars + overlap margin), reads them and runs the chunker. The chunker produces perfect chunks from the available text stream — it doesn't need to wait for the full document.

Key insight: **the chunker operates on a character stream, not a page stream.** Pages are an extraction artifact. The chunker just needs enough text to produce a chunk. It reads pages from the buffer in order, concatenating their text, and chunks the running text as a sliding window.

After chunking, embeds each chunk (Voyage CCE or local ONNX) and writes the chunk + embedding to `pdf_chunks`. The embedding column being populated is the signal to the uploader that the chunk is ready.

Cross-page chunk boundaries are handled naturally: the chunker maintains a text cursor position. When it needs more text, it reads the next available page(s) from the buffer. A chunk that spans a page boundary is just a chunk — the page boundary is invisible to the chunker.

### Stage 3: Uploader Thread

Polls `pdf_chunks` for rows where `embedding IS NOT NULL AND uploaded = 0`. Batches them into 300-record upserts (matching ChromaDB Cloud limits). Marks `uploaded = 1` after each successful upsert.

On resume, only re-uploads chunks where `uploaded = 0` — no re-embedding needed.

**table_regions post-pass (RF-14)**: The `chunk_type` metadata tag (`"table_page"` vs `"text"`) requires `table_regions`, which is only available after extraction completes. The uploader proceeds without waiting — chunks are uploaded with `chunk_type = "text"` by default. After extraction signals completion, a single UPDATE pass applies the correct `chunk_type` to affected chunks. Chunks already uploaded with the wrong tag are corrected in-place in T3 via `update_chunks()`. The tag is cosmetic (display filtering only) so temporary incorrectness has no retrieval impact.

### Concurrency Model

```python
cancel = threading.Event()           # fast in-process cancellation (RF-7)
extraction_done = threading.Event()  # extractor → chunker signal
chunking_done = threading.Event()    # chunker → uploader signal
first_exc: BaseException | None = None

with ThreadPoolExecutor(max_workers=3) as pool:
    extract_future = pool.submit(extractor_loop, ..., extraction_done)
    chunk_future = pool.submit(chunker_loop, ..., extraction_done, chunking_done)
    upload_future = pool.submit(uploader_loop, ..., chunking_done)

    all_futures = {extract_future, chunk_future, upload_future}

    # F2 fix: wait for first exception, cancel, then join all remaining.
    done, not_done = wait(all_futures, return_when=FIRST_EXCEPTION)
    for f in done:
        if (exc := f.exception()) is not None:
            first_exc = exc
            cancel.set()
            break
    if not_done:
        wait(not_done, return_when=ALL_COMPLETED)

# All threads stopped. Post-passes: metadata enrichment, table_regions, stale pruning.
if first_exc is not None:
    db.mark_failed(content_hash, error=str(first_exc))
    raise first_exc
```

Each stage loop checks `cancel.is_set()` at the top of each iteration and exits cleanly if set. On crash (no clean shutdown), `pdf_pipeline.status` remains `running`. The concurrent guard distinguishes live from crashed pipelines using `updated_at`: if `status = 'running'` AND `updated_at` is older than 5 minutes, the pipeline is stale (crashed) — set `status = 'resuming'` and take ownership. If `updated_at` is recent, another process is actively running — skip. Each stage updates `updated_at` on every batch write, acting as a heartbeat.

Backpressure: if the uploader falls behind, the chunk buffer grows in SQLite (disk, not RAM). If the chunker falls behind, the page buffer grows in SQLite. Memory stays bounded at the working set of each thread (~1 batch per thread).

### Resume Semantics

On crash and restart:
1. Read `pdf_pipeline` for the content hash
2. Extractor: skip pages already in `pdf_pages` (resume from `pages_extracted + 1`)
3. Chunker: re-read all pages from `pdf_pages`, re-chunk from the start; `INSERT OR IGNORE` skips already-created chunks (idempotent — deterministic chunking produces identical rows)
4. Uploader: skip chunks where `uploaded = 1` (resume from first `uploaded = 0`)

On successful completion:
1. Delete all rows from `pdf_pages`, `pdf_chunks`, `pdf_pipeline` for this content hash
2. (Or: keep for debugging with a TTL, auto-clean in `nx doctor`)

### Local ONNX Embedding Option

The chunk buffer's `embedding` column enables a hybrid strategy: embed with local ONNX for immediate upload (low latency), then optionally re-embed with Voyage CCE for higher quality and update in place. Or: use ONNX as the default for the streaming pipeline and Voyage only for the final quality pass.

This is a future enhancement, not a requirement for the initial implementation.

## Acceptance Criteria

- [ ] 771-page book extraction streams pages to buffer as they arrive (not accumulated in memory)
- [ ] Chunker starts producing chunks before extraction finishes
- [ ] Uploader starts uploading before chunking finishes
- [ ] Crash during extraction loses at most one page batch (default 1 page; configurable via `mineru_page_batch`)
- [ ] Crash during chunking loses at most one chunk
- [ ] Crash during upload loses zero work (chunks + embeddings persist in buffer)
- [ ] Resume from any crash point completes in under 60s (excluding re-extraction of in-flight batch)
- [ ] Memory usage bounded: no stage holds more than one batch in RAM
- [ ] For 200+ page MinerU documents, wall-clock time ≤ extraction time + 60s (vs extraction + embedding time sequentially)
- [ ] Chunk boundaries identical to batch pipeline for the same document text (verified by chunk ID comparison)

## Relationship to RDR-047

RDR-047 Phase 1 (incremental upsert with checkpoints) is the **stepping stone** to this architecture. The checkpoint module (`src/nexus/checkpoint.py`) coexists with the `pdf_pipeline` table — checkpoint.py handles the batch path for small documents, while `pdf_pipeline` handles the streaming path for large ones. The routing decision is based on page count and extractor type:

- **Batch path** (checkpoint.py): documents < `_STREAMING_THRESHOLD` pages (~100 for MinerU, ~500 for Docling). Simple, low overhead, proven by RDR-047.
- **Streaming path** (pdf_pipeline): documents >= threshold. Three concurrent stages, SQLite buffer, O(1) memory.

Both paths produce identical chunk IDs and chunk boundaries for the same document text. The naming convention aligns: `chunks_uploaded` (pipeline) corresponds to `chunks_upserted` (checkpoint).

## Research Findings

### RF-1: PDFChunker is fully streamable — no full-text dependency (2026-04-03)
**Classification**: Verified — code inspection | **Confidence**: HIGH

`PDFChunker.chunk()` (`pdf_chunker.py:35-93`) uses a simple `while start < len(text)` loop with a sliding window. It advances a character cursor, finds sentence boundaries in the last 20% of the window, and emits chunks with overlap. The only dependency on global state is `page_boundaries` for assigning page numbers to chunks — and this is a simple `start_char` lookup (`_page_for`), not a structural dependency.

**Streaming adaptation**: The chunker can operate on a growing text buffer. As pages arrive, concatenate their text. When the buffer has >= `chunk_chars` (1500 chars default) available past the current cursor, produce the next chunk. The `page_boundaries` list grows incrementally as pages arrive — `_page_for` does a linear scan so it works with partial boundaries. The overlap window (`_DEFAULT_OVERLAP = 0.15` → ~225 chars) means the chunker needs at most `chunk_chars + overlap_chars` ≈ 1725 chars of lookahead past the cursor.

**Chunk ID stability**: Chunk IDs are `{content_hash[:16]}_{chunk_index}`. Since `content_hash` is computed from the full PDF file (not the text), IDs are stable regardless of whether chunking is streaming or batch. Chunk boundaries will be identical because the algorithm is deterministic on the same text sequence.

**No code change needed to `PDFChunker` itself** — the streaming wrapper just feeds it text incrementally and collects chunks.

### RF-2: SQLite WAL mode handles three concurrent writers correctly (2026-04-03)
**Classification**: Verified — SQLite documentation + T2 precedent | **Confidence**: HIGH

SQLite in WAL mode allows concurrent readers and one writer at a time. Multiple writers serialize on the WAL write lock — one gets it, others block briefly. T2 already uses WAL mode (`t2.py:47`: `PRAGMA journal_mode=WAL`) and works fine under concurrent access from multiple CLI invocations.

**Contention analysis for the pipeline**:
- Extractor writes: ~1 page/second (MinerU) → ~1 write/sec
- Chunker writes: ~1 chunk/second (1500 chars at reading speed) → ~1 write/sec
- Uploader writes: updates `uploaded=1` → ~1 write every few seconds (batched)

Total: ~2-3 writes/second. SQLite handles thousands of writes/second in WAL mode. Contention is negligible.

**Recommendation**: Use a single SQLite database (`~/.config/nexus/pipeline.db`). Each thread gets its own `sqlite3.Connection` (SQLite connections are not thread-safe, but separate connections to the same database are fine). No separate databases needed.

### RF-3: Three stages (not four) is the right design — embed in chunker (2026-04-03)
**Classification**: Design analysis | **Confidence**: MEDIUM

**Option A — embed in chunker (3 stages)**: Chunker reads pages, chunks text, embeds each chunk (Voyage CCE or local ONNX), writes chunk+embedding to buffer. Uploader reads embedded chunks and upserts to T3.

**Option B — separate embedder (4 stages)**: Chunker writes raw chunks. Embedder reads raw chunks, embeds, updates embedding column. Uploader reads embedded chunks.

**Analysis**: The embedding call is the bottleneck (~200-500ms per CCE batch via Voyage API). In option A, the chunker thread blocks on API calls, so it can't produce chunks while waiting. But chunking is fast (~1ms per chunk), so the chunker is never the bottleneck anyway — it's always waiting for the extractor to produce more pages. The embedding call fills the chunker's idle time perfectly.

In option B, the additional stage adds: (1) another polling loop, (2) another thread's SQLite writes, (3) more complex pipeline state tracking, (4) a "chunks created but not yet embedded" state that complicates resume. The only benefit is if you want to run multiple embedder threads — but the `_TokenBucket` rate limiter from RDR-047 Phase 2 already parallelizes embedding within a single thread via `ThreadPoolExecutor(4)`.

**Recommendation**: Three stages. The chunker embeds inline. If local ONNX embedding is used (no API call), embedding is ~10ms/chunk — the chunker never blocks. If Voyage CCE is used, the existing `_TokenBucket` + `ThreadPoolExecutor(4)` parallelism in `_embed_with_fallback` already maximizes API throughput within the chunker thread.

### RF-4: Buffer cleanup — delete on success, scan in doctor (2026-04-03)
**Classification**: Design proposal | **Confidence**: HIGH

Follow the pattern established by RDR-047 checkpoints:
- **On success**: `DELETE FROM pdf_pages WHERE content_hash = ?`, same for `pdf_chunks` and `pdf_pipeline`. Single transaction. Immediate cleanup.
- **On crash**: Rows persist. Next run detects `pdf_pipeline.status != 'done'` for the same content hash → resume.
- **Orphaned buffers**: If the PDF is deleted or content changes (new hash), old rows become orphans. Scan in `nx doctor` — same pattern as `scan_orphaned_checkpoints()` in `checkpoint.py`. Check if `pdf_path` still exists and `content_hash` still matches.
- **TTL not needed**: The buffer is working storage, not archival. Delete on success. Clean orphans in doctor. No TTL complexity.

### RF-5: Streaming threshold — 100+ pages based on empirical timing (2026-04-03)
**Classification**: Empirical estimate | **Confidence**: MEDIUM

**Timing breakdown from RDR-047 RF-1 and RF-4**:
- MinerU extraction: ~5 min for 771 pages → ~0.4s/page
- Docling extraction: ~12s for 771 pages → ~0.016s/page
- Chunking: ~2s for 5000+ chunks → negligible
- CCE embedding: ~5-10 min for 2000 chunks sequentially, ~1.5-3 min with 4x parallelism
- Upsert: ~1-2 min for 2000 chunks

**Sequential total (MinerU)**: 5 + 0 + 5 + 1 = ~11 min
**Streaming estimate**: Extraction and embedding overlap. Extraction finishes at 5 min. Embedding follows ~30s behind (buffering + chunking latency). Upload follows ~10s behind embedding. Total: ~5.5-6 min for a 771-page book. ~45% improvement.

**For Docling**: Extraction is only 12s. Streaming overhead (thread management, SQLite writes, polling) exceeds the benefit for < 100 pages. The crossover point where streaming saves more time than it costs is ~100 pages with MinerU, ~500 pages with Docling.

**Recommendation**: `_STREAMING_THRESHOLD = 100` pages (MinerU) or `200` chunks. Below this, the RDR-047 batch+checkpoint approach is sufficient. The threshold could be extractor-dependent: always stream for MinerU (slow extraction), batch for Docling (fast extraction).

### RF-6: Extractors already emit page batches — zero refactoring to stream (2026-04-03)
**Classification**: Verified — code inspection | **Confidence**: HIGH

All three extraction backends already iterate pages individually:

- **Docling** (`pdf_extractor.py:241-255`): `for p in range(1, page_count + 1): page_md = doc.export_to_markdown(page_no=p)` — literal per-page loop, accumulates into `page_texts` list and `page_boundaries` list.
- **MinerU** (`pdf_extractor.py:369-404`): `for batch_idx, (start, end) in enumerate(batches)` — per-batch loop controlled by `get_mineru_page_batch()` (default 1 page, configurable). Accumulates into `md_parts`, `all_content_list`, `all_pdf_info`.
- **PyMuPDF** (`pdf_extractor.py:778-798`): `for page_num, page in enumerate(doc)` — per-page loop, accumulates `text_parts` and `page_boundaries`.

In each case, the loop body produces one page/batch of text and appends it to a list. Converting to streaming means replacing `list.append(text)` with `buffer.write(page_index, text)`. The loop structure, error handling, and page_boundary computation stay identical.

The `page_boundaries` accumulation is itself streamable — each entry only depends on `current_pos` (a running counter) and the current page's text length.

**Implication**: The extractor refactoring surface is minimal. Each backend's loop body becomes a callback or buffer write instead of a list append.

### RF-7: Error propagation design — poison pill pattern (2026-04-03)
**Classification**: Design analysis | **Confidence**: HIGH

Three concurrent threads need coordinated shutdown when any stage fails. Standard patterns:

1. **Poison pill**: Failing stage writes a sentinel row (e.g., `page_index = -1, page_text = "ERROR: ..."`) to the buffer. Downstream stages detect the sentinel and shut down gracefully. Simple, database-native, survives crashes (the sentinel persists).

2. **threading.Event**: Shared `cancel_event = threading.Event()`. Each stage checks `cancel_event.is_set()` at the top of its loop. Failing stage sets the event. Simple, but doesn't survive crashes (event is in-memory only).

3. **Future exception propagation**: The `ThreadPoolExecutor` approach from the proposed design: `future.result()` raises the first exception. But this only fires when the main thread joins — downstream stages keep running until then.

**Recommendation**: Combine (1) and (2). Use `threading.Event` for fast in-process cancellation. Write the error to `pdf_pipeline.status = 'failed'` for crash persistence. On resume, check `status = 'failed'` and either retry or skip.

**Partial recovery**: If extraction fails at page 500 of 771, pages 1-499 are already in the buffer. The chunker can chunk them. The uploader can upload them. The document is partially indexed — better than nothing. The `pdf_pipeline` table records `pages_extracted = 499, total_pages = 771`. A future retry can attempt the remaining pages. This is a significant resilience improvement over the current all-or-nothing model.

### RF-8: Memory profile — current vs streaming (2026-04-03)
**Classification**: Estimated from code analysis | **Confidence**: MEDIUM

**Current pipeline memory for 771-page CMRB book**:
- Extraction: `md_parts` list — 771 page strings, ~2-5 MB total
- `"\n".join(md_parts)` — creates a second copy, ~2-5 MB
- `PDFChunker.chunk()` — returns ~2000 `TextChunk` objects, each holding a copy of its text. Total: ~3-5 MB (overlapping chunks mean ~20% extra text)
- `_embed_with_fallback` — embedding vectors: 2000 × 1024 floats × 4 bytes = ~8 MB
- `_index_document` / `_index_pdf_incremental` — IDs, documents, metadatas lists: ~5-10 MB
- **Peak concurrent**: ~20-30 MB for the 771-page book (extraction + chunks + embeddings all in memory simultaneously before the first upsert)

**Streaming pipeline memory**:
- Extractor: one page batch in memory (default 1 page, ~4 KB), rest in SQLite
- Chunker: text cursor reads ~2 KB at a time from SQLite, one chunk + one embedding in memory (~5 KB)
- Uploader: one upsert batch (~300 records × ~2 KB each = ~600 KB)
- **Peak concurrent**: ~1-2 MB regardless of document size

**Savings**: ~15-25x memory reduction for the 771-page book. More importantly, memory is **O(1)** not **O(pages)** — a 5000-page book uses the same memory as a 50-page paper.

### RF-9: `batch_index_pdfs` — streaming enables per-file parallelism (2026-04-03)
**Classification**: Design analysis | **Confidence**: MEDIUM

Currently `batch_index_pdfs()` (`doc_indexer.py:763`) processes PDFs sequentially. Each PDF goes through the full extract → chunk → embed → upsert pipeline before the next starts.

With the streaming pipeline backed by SQLite, multiple PDFs could have concurrent pipelines — each with their own `content_hash` in the buffer tables. The SQLite buffer naturally isolates them. This enables:

1. **Extract PDF-A** while **embedding PDF-B's chunks** while **uploading PDF-C's embedded chunks** — full utilization of CPU (extraction), API (embedding), and network (upload) simultaneously.
2. **Shared `PDFExtractor` instance** (nexus-u1um) becomes more valuable — the server stays warm across files.
3. The `_TokenBucket` rate limiter is already global — it correctly throttles across all concurrent embedding threads.

This is a natural extension of the three-stage pipeline, not an additional design. The buffer tables already partition by `content_hash`.

**Caution**: Per-file parallelism multiplies MinerU server memory pressure. If 3 PDFs extract concurrently via MinerU, the server handles 3× the page batches. The `_restart_budget` (2 restarts) may need increasing, or extraction should remain sequential with only embed/upload parallelized across files.

### RF-10: The buffer IS the checkpoint — simplifies the checkpoint module (2026-04-03)
**Classification**: Design analysis | **Confidence**: HIGH

RDR-047's `checkpoint.py` exists because the pipeline has no durable intermediate state. The streaming pipeline's SQLite buffer IS durable intermediate state. Every page, chunk, and embedding is persisted the moment it's produced.

On resume:
- `pdf_pipeline.pages_extracted` = how far extraction got → skip those pages
- `pdf_chunks WHERE embedding IS NOT NULL AND uploaded = 0` = chunks ready to upload → upload them
- `pdf_chunks WHERE embedding IS NULL` = chunks needing embedding → embed them
- `pdf_pages WHERE page_index > last_chunked_page` = pages needing chunking → chunk them

No separate checkpoint file needed. The buffer tables are self-describing. `checkpoint.py` can be deprecated for the streaming path (retained for the batch path used by small documents).

This also eliminates the S1 write-ordering race from RDR-047's critique — there's no "upsert succeeded but checkpoint write failed" window because the upload status is written in the same database as the chunk data.

### RF-11: Thread coordination — SQLite polling vs queue (2026-04-03)
**Classification**: Design analysis | **Confidence**: MEDIUM

Two options for inter-stage signaling:

**Option A — SQLite polling**: Each stage polls the buffer table on an interval (e.g., 500ms). Simple, crash-durable (buffer persists), no shared state. Downside: 500ms latency between stages; wasted queries when nothing is ready.

**Option B — `queue.Queue` + SQLite**: Use an in-memory `queue.Queue(maxsize=N)` for real-time signaling between threads. Each stage writes to SQLite AND puts a notification on the queue. The downstream stage blocks on `queue.get()` instead of polling. Crash recovery reads from SQLite (queue is lost on crash). Downside: dual writes (queue + SQLite), more complex.

**Recommendation**: Start with Option A (polling). 500ms latency is negligible when extraction takes 400ms/page and embedding takes 200-500ms/batch. Optimize to Option B only if profiling shows polling overhead is significant. The simplicity of "just read from SQLite" makes the code much easier to reason about and debug.

**Implementation note**: The shipped implementation uses `threading.Event` signals (`extraction_done`, `chunking_done`) for zero-latency inter-stage notification in the orchestrated path, with SQLite polling as a fallback for the standalone resume path. This hybrid combines Option A's crash durability with Option B's responsiveness.

### RF-12: CCE embedding quality is preserved — no regression from streaming (2026-04-03)
**Classification**: Verified — code inspection + API semantics | **Confidence**: HIGH

The concern: Voyage CCE `contextualized_embed(inputs=[[c1, c2, ..., cN]])` provides cross-chunk context within the inner list. If the streaming chunker embeds chunks one at a time (`inputs=[[c1]]`), cross-chunk context is lost.

**Resolution**: The current pipeline already loses cross-batch context. `_batch_chunks_for_cce()` (`doc_indexer.py:98`) splits a 2000-chunk document into ~62 batches of ~32 chunks. Chunks at the boundary of batch 31 and batch 32 share no context. This is an accepted quality tradeoff — CCE context is most valuable between nearby chunks within a batch.

The streaming chunker would use the **identical batching**: accumulate ~32 chunks (governed by `_CCE_TOKEN_LIMIT = 24_000` chars), then embed the batch as `inputs=[[c1..c32]]`. The batch size, context window, and quality are exactly the same. The only difference is that batches arrive incrementally instead of all at once — the CCE API doesn't know or care.

**No quality regression from streaming.** The cross-batch context boundary loss is identical.

### RF-13: Docling extraction is NOT truly streamable at the backend level (2026-04-03)
**Classification**: Verified — code inspection | **Confidence**: HIGH

Docling's `converter.convert(str(pdf_path))` (`pdf_extractor.py:233`) processes the entire PDF into an internal document representation before returning. The per-page iteration (`for p in range(1, page_count + 1): doc.export_to_markdown(page_no=p)` at line 241) happens after the full conversion completes.

This means for Docling:
- **Extraction phase**: The `convert()` call loads the whole PDF. ~12s for 771 pages (non-enriched). Memory is in Docling's internal model, not our text strings.
- **Page iteration**: Per-page markdown export is fast (<1ms/page). Writing pages to the buffer is streaming.
- **Net effect**: The *text accumulation* is eliminated (pages go to SQLite instead of `page_texts` list), but Docling's internal memory is unavoidable.

For MinerU, extraction IS truly streaming — each page batch (`_mineru_run_isolated`) is an independent API call or subprocess. Pages arrive incrementally over minutes.

For PyMuPDF, extraction is also per-page (`for page_num, page in enumerate(doc)` at line 778), and PyMuPDF pages are lightweight — truly streaming.

**Implication**: Docling's fast extraction (12s) means the streaming pipeline's benefit for Docling is crash durability (pages persist in buffer), not parallelism. MinerU's slow extraction (5+ min) is where streaming parallelism pays off. This reinforces RF-5's extractor-dependent threshold.

### RF-14: `table_regions` metadata requires post-pass — not available during streaming (2026-04-03)
**Classification**: Verified — code inspection | **Confidence**: HIGH

`PDFChunker.chunk()` uses `table_regions` (`pdf_chunker.py:49-50`) to tag chunks as `"table_page"` or `"text"`. `table_regions` is populated by iterating Docling's `doc.iterate_items()` (`pdf_extractor.py:261-296`) **after** the full extraction completes — it requires the complete document model.

For MinerU, `table_regions` comes from `content_list` entries (`_mineru_build_result`), which are accumulated across all page batches.

**Impact on streaming**: The chunker can produce chunk text and boundaries without `table_regions`. The `chunk_type` tag is metadata-only — it doesn't affect chunk text or boundaries. Two options:

1. **Defer tagging**: Chunk with `table_regions=[]`. After extraction completes, update chunk metadata in the buffer with the correct `chunk_type` tag. One SQL UPDATE per chunk that needs retagging.
2. **Incremental tagging**: MinerU's `content_list` arrives per-batch. Extract table pages from each batch's content_list and pass to the chunker incrementally. This works for MinerU. For Docling, `iterate_items()` needs the full document.

**Recommendation**: Option 1 (defer). The `chunk_type` tag is cosmetic — it's used for display filtering, not retrieval quality. A post-pass UPDATE is cheap and doesn't block the pipeline.

### RF-15: Concurrent pipeline hazards are handled by SQLite transaction isolation (2026-04-03)
**Classification**: Design analysis | **Confidence**: HIGH

Potential race conditions in the three-thread model:

1. **Chunker reads page while extractor writes it**: Impossible. Extractor commits a transaction per page batch. Chunker only sees committed rows (SQLite read isolation in WAL mode).

2. **Uploader reads chunk before embedding is written**: Impossible. Chunker writes chunk text + embedding in one transaction. Uploader queries `WHERE embedding IS NOT NULL` — only sees fully committed rows.

3. **Two pipelines for the same PDF**: `pdf_pipeline` PRIMARY KEY is `content_hash`. `INSERT OR IGNORE` or `INSERT ... ON CONFLICT` prevents duplicates. The first pipeline owns the content hash; a concurrent attempt detects the existing row and either waits or skips.

4. **Resume races (crash during concurrent pipeline)**: On restart, the pipeline state table shows the last committed state. Each stage's resume query only returns uncommitted work. No double-processing possible because row updates (e.g., `uploaded = 1`) are idempotent.

**No additional locking needed beyond SQLite's built-in transaction isolation.** This is a significant simplification over the file-based checkpoint approach (which had the S1 write-ordering race).

### RF-16: Pipeline state machine — "running" replaces per-stage status (2026-04-03)
**Classification**: Design proposal — adopted in schema | **Confidence**: MEDIUM

The original `pdf_pipeline.status` had values: `extracting|chunking|embedding|uploading|done|failed`. But with three concurrent stages, the pipeline is simultaneously extracting AND chunking AND uploading. Per-stage status is misleading. **Schema updated to reflect this finding.**

**Revised state machine**:
- `running` — at least one stage is active
- `completed` — all stages completed successfully, buffer cleaned
- `failed` — at least one stage failed (error details in a separate `error` column)
- `resuming` — resume in progress after a previous crash

Per-stage progress tracked by counters: `pages_extracted`, `chunks_created`, `chunks_embedded`, `chunks_uploaded`. These are monotonically increasing and sufficient to determine which stage needs more work on resume.

The uploader knows it's done when `chunks_uploaded = chunks_created` AND the extractor has signaled completion (a flag or `pages_extracted = total_pages`). The chunker knows it's done when all pages are chunked and the extractor has signaled completion.
