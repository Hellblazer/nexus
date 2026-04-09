---
rdr: "048"
title: "Streaming PDF Pipeline"
status: closed
closed_date: 2026-04-09
reason: implemented
---

# RDR-048 Post-Mortem

## Outcome

Fully implemented. The three-stage streaming PDF pipeline ships as a concurrent extractor/chunker/uploader architecture backed by a SQLite WAL-mode persistent buffer. Core deliverables:

- `src/nexus/pipeline_buffer.py` — SQLite-backed persistent buffer with `pdf_pages`, `pdf_chunks`, and `pdf_pipeline` tables
- `src/nexus/pipeline_stages.py` — Three-stage concurrent orchestration (extractor, chunker, uploader threads) with `threading.Event` coordination and cancel propagation
- `src/nexus/checkpoint.py` — Crash-resilient checkpointing for the batch path (small documents)
- Test coverage: 778+ tests in `test_pipeline_stages.py`, 374+ in `test_pipeline_buffer.py`

## What Worked

- **Three-stage design (RF-3)**: Embedding inline in the chunker thread proved correct — chunking is never the bottleneck, so the embedding call fills idle time. Four stages would have added complexity for no throughput gain.
- **SQLite WAL as the buffer (RF-2, RF-15)**: Transaction isolation eliminated all inter-thread race conditions without explicit locking. Three writers at ~2-3 writes/second is well within WAL mode's capacity.
- **Buffer IS the checkpoint (RF-10)**: The persistent buffer made RDR-047's separate checkpoint files unnecessary for the streaming path. Every page, chunk, and embedding is durable the moment it's produced — no write-ordering race.
- **Existing extractors already page-iterable (RF-6)**: All three backends (MinerU, Docling, PyMuPDF) already had per-page loops. Converting `list.append()` to buffer writes was minimal refactoring.
- **Chunker streamability (RF-1)**: `PDFChunker` required zero changes — it operates on a character stream, not a page stream, so incremental feeding worked out of the box.
- **CCE quality preserved (RF-12)**: Batch boundaries for Voyage CCE embedding are identical between streaming and batch paths — no quality regression.

## What Didn't Work

- **Docling is not truly streaming at extraction level (RF-13)**: `converter.convert()` processes the entire PDF before returning. Streaming benefit for Docling is crash durability (pages persist in buffer), not extraction parallelism. MinerU is where the real pipeline overlap payoff lands.
- **`table_regions` requires post-pass (RF-14)**: Table/text chunk type metadata is only available after extraction completes. Required a deferred UPDATE pass, adding complexity to the uploader's completion logic.

## Deviations Accepted

- Streaming threshold is extractor-dependent (~100 pages for MinerU, higher for Docling) rather than a single fixed value, per RF-5 analysis.
- Local ONNX hybrid embedding strategy (mentioned in the RDR as a future enhancement) was not implemented — deferred to a future RDR if needed.

## Metrics

- Research findings: 16 (RF-1 through RF-16, all verified or adopted)
- Implementation files: 3 core (`pipeline_buffer.py`, `pipeline_stages.py`, `checkpoint.py`)
- Tests: 1150+ across buffer and stages test files
- Memory improvement: O(1) vs O(pages) — ~15-25x reduction for large documents
- Throughput improvement: ~45% wall-clock reduction for 771-page MinerU extractions (extraction and embedding overlap)
