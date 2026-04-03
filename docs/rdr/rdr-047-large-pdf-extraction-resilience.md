---
title: "Large PDF Extraction Resilience"
id: RDR-047
type: Architecture
status: draft
priority: high
author: Hal Hildebrand
reviewed-by: self
created: 2026-04-03
epic: nexus-u0q5
related_issues:
  - "RDR-046 - MinerU Server-Backed PDF Extraction (closed)"
  - "nexus-u0q5 - Epic: Large PDF Extraction Resilience"
  - "nexus-jr1p - Incremental upsert"
  - "nexus-15p0 - Embed/upsert progress"
  - "nexus-cmcp - Parallel embedding"
  - "nexus-u1um - Shared PDFExtractor"
  - "nexus-uggc - Graceful page failure"
  - "nexus-vu11 - Vision pass formula routing"
  - "nexus-ezbf - Config get nested keys"
  - "nexus-luor - Lock file cleanup"
---

# RDR-047: Large PDF Extraction Resilience

> Revise during planning; lock at implementation.
> If wrong, abandon code and iterate RDR.

## Problem Statement

RDR-046 delivered MinerU server-backed extraction that works well for collections of 20-60 page papers. Live testing on a 771-page book (Grossberg's *Conscious Mind, Resonant Brain*) exposed fundamental resilience failures:

1. **All-or-nothing extraction**: 771 pages accumulated in memory. A blank page at page 766 raised `RuntimeError`, losing 30 minutes of work. Fixed with band-aid (empty page returns ""), but any failure during the subsequent chunk/embed/upsert phase still loses everything.

2. **No checkpoints**: No way to resume a failed extraction. The entire 771-page pipeline must restart from scratch.

3. **Silent embed/upsert phase**: After "771/771 done", 5-10 minutes of silence while chunks are embedded and upserted. No progress output, no way to know if it's working or hung.

4. **Sequential API calls**: CCE embedding batches sent one at a time. 2000+ chunks = 30+ sequential Voyage API round-trips. Could be parallelized.

5. **Lost pages on double failure**: When both MinerU server AND subprocess fail on a page (OOM), the page is silently lost from the document.

6. **Per-document PDFExtractor**: Each file in a batch creates a new `PDFExtractor()`, resetting the server health cache and restart budget.

### Root Cause

The extraction pipeline was designed for individual papers (20-60 pages), where accumulation in memory is fine. A 771-page book is 10-40x larger, making every accumulation point a failure risk.

### Impact

- 30-minute extraction lost at 99% completion (page 766/771)
- No resume capability — full restart required
- User cannot distinguish "working" from "hung" during embed/upsert
- Formula-heavy books trigger server OOM, subprocess fallback is slow

## Proposed Solution

### Phase 1: Incremental Upsert + Checkpoints (nexus-jr1p)

**Goal**: Never lose more than one page-batch of work.

Extract → chunk → embed → upsert in page-batch increments (e.g., 50 pages at a time). Write checkpoint after each batch.

```
~/.config/nexus/checkpoints/<content_hash>.json
{
  "pdf": "/path/to/book.pdf",
  "collection": "knowledge__art",
  "content_hash": "abc123...",
  "pages_completed": 750,
  "chunks_upserted": 1842,
  "embedding_model": "voyage-context-3",
  "timestamp": "2026-04-03T10:30:00Z"
}
```

On resume: read checkpoint, skip completed pages, continue from `pages_completed + 1`.

### Phase 2: Progress + Parallel Embedding (nexus-15p0, nexus-cmcp)

- Print `Embedding: batch 5/31...` and `Upserting: 500/2000 chunks...` during the silent phase
- Use `ThreadPoolExecutor(max_workers=4)` for concurrent Voyage API calls
- Respect Voyage rate limits (currently 300 RPM for CCE)

### Phase 3: Graceful Page Failure (nexus-uggc)

When both server and subprocess fail on a page:
1. Fall back to non-enriched Docling text for that page
2. Insert `[FORMULA: extraction failed — page N]` placeholders for detected formula regions
3. Log the failure, continue extraction
4. Never lose a page entirely

### Phase 4: Shared Extractor + Misc (nexus-u1um, nexus-ezbf, nexus-luor)

- Share `PDFExtractor` instance across batch indexing (one health check, one restart budget)
- Fix `nx config get` for nested dotted keys
- Investigate and fix lock file cleanup

### Deferred: Vision Pass Formula Routing (nexus-vu11)

Per-page formula detection via vision/layout model to skip non-formula pages in MinerU. Blocked by MinerU's bundling of formula YOLO + MFR behind `formula_enable`. Requires either upstream API change or lightweight external formula detector.

## Acceptance Criteria

- [ ] 771-page book indexes successfully with at most 50 pages of work lost on any single failure
- [ ] Crashed extraction resumes from checkpoint in under 30 seconds
- [ ] Progress output visible during embed and upsert phases
- [ ] Embedding phase completes in under 2 minutes for 2000 chunks (parallel API calls)
- [ ] Page failure (OOM) produces Docling fallback text, not a lost page
- [ ] `nx index repo` with 6 PDFs shares one PDFExtractor instance

## Research Findings

### RF-1: All-or-nothing extraction pipeline (2026-04-03)
**Classification**: Verified — empirical (CMRB, 771 pages) | **Confidence**: HIGH

`_extract_with_mineru` accumulates all pages in memory before returning. Any failure loses all work. Observed: 30 min lost at page 766/771 (blank page). Blank-page trigger fixed but accumulation architecture remains.

### RF-2: Sequential Voyage CCE embedding bottleneck (2026-04-03)
**Classification**: Verified — empirical + code inspection | **Confidence**: HIGH

CCE batches sent sequentially. 5,724 chunks = ~89 sequential API calls. 5-10 minute silent phase after extraction. Voyage API supports 300 RPM — ThreadPoolExecutor(4) would cut time ~4x.

### RF-3: MinerU server OOM pattern (2026-04-03)
**Classification**: Verified — empirical across 4 corpora | **Confidence**: HIGH

OOM triggered by MFR batching all formula-like regions per page as single inference. Complex diagrams misidentified as formula regions. Auto-restart (2x budget) + subprocess fallback handled all observed cases.

### RF-4: Non-enriched Docling is 100x faster (2026-04-03)
**Classification**: Verified — empirical | **Confidence**: HIGH

Enriched Docling: 20+ min/PDF. Non-enriched: 12s. Unicode math scan: 0.05s. Replaced enriched screening with quick scan + non-enriched extraction. Limitation: only works for digital PDFs, not scanned.

### RF-5: MinerU has no layout-only mode (2026-04-03)
**Classification**: Verified — source + Context7 docs | **Confidence**: HIGH

`formula_enable=false` disables both detection AND recognition. No way to cheaply identify formula pages without running MFR. Per-page formula routing requires external detector or upstream API change.

### RF-6: Observability gaps are the real user pain (2026-04-03)
**Classification**: Verified — empirical (user feedback) | **Confidence**: HIGH

Technical failures were recoverable. UX failures were not: (1) Docling enrichment produced zero output for 20 min — user killed and restarted. (2) Post-extraction embed/upsert phase silent for 5-10 min — appeared hung. (3) Stale server config (wrong port) caused silent fallback with no indication. (4) tqdm progress bar fights made progress messages invisible.

Key principle: **every operation >5s needs visible progress. Every fallback needs a visible message. Every skip needs a reason.**

### RF-7: Accumulation is the architectural trap (2026-04-03)
**Classification**: Verified — empirical + architectural analysis | **Confidence**: HIGH

Pipeline accumulates at every level: pages in extractor, chunks in indexer, embeddings in embed function, records in write batch. For 20-page papers, each holds seconds of work. For 771 pages, each holds minutes. Failure cost scales linearly with document size.

Fix is architectural: **process and persist in bounded increments**. Extract N pages → chunk → embed → upsert → checkpoint → repeat. Same pattern as database WAL, TCP sliding windows, git per-object storage. Cap failure cost at one batch, not one document.

### RF-8: Checkpoint design for PDF extraction resume (2026-04-03)
**Classification**: Design proposal | **Confidence**: MEDIUM

Checkpoint file at `~/.config/nexus/checkpoints/<content_hash>-<collection>.json`. Written atomically after each batch upsert. Contains pages_completed, chunks_upserted, chunk_ids. Resume skips to `pages_completed + 1`. Content hash mismatch = stale checkpoint, delete and restart. Maximum work at risk: 50 pages (~2 min). Key constraint: chunk IDs must be deterministic (`content_hash_chunkindex`) — already true.

### RF-9: Incremental upsert — minimal refactoring surface (2026-04-03)
**Classification**: Design proposal | **Confidence**: MEDIUM

Internal components already work in bounded chunks. Only the top-level orchestration needs restructuring: new `_index_document_incremental` that loops over 50-page batches, calling existing extract/chunk/embed/upsert per batch. Cross-page chunking concern: last chunk of batch N may need to merge with first chunk of batch N+1. Options: overlap extraction, accept boundary artifacts, or post-process boundaries.

### RF-10: Resilience hierarchy — defense in depth (2026-04-03)
**Classification**: Design proposal validated by empirical session | **Confidence**: HIGH

Five layers: (1) Server auto-restart — IMPLEMENTED, handled CMRB page 99. (2) Subprocess fallback — IMPLEMENTED, handled all OOM pages. (3) Graceful page degradation — NOT YET, Docling fallback + placeholders. (4) Incremental upsert + checkpoint — NOT YET, the big one. (5) Embed/upsert error recovery — PARTIAL (CCE retry, metadata stripping done; progress, parallelism not done). Goal: any single failure costs at most 2 minutes of work.
