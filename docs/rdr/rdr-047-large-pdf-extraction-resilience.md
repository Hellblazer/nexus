---
title: "Large PDF Extraction Resilience"
id: RDR-047
type: Architecture
status: draft
priority: high
author: Hal Hildebrand
reviewed-by: self
created: 2026-04-03
related_issues:
  - "RDR-046 - MinerU Server-Backed PDF Extraction (closed)"
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
