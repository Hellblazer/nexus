---
title: "Section-Type Metadata and Quality Scoring for Knowledge Collections"
id: RDR-055
type: Feature
status: closed
accepted_date: 2026-04-07
closed_date: 2026-04-07
close_reason: implemented
reviewed-by: self
priority: high
author: Hal Hildebrand
created: 2026-04-07
related_issues: [RDR-052, RDR-054]
---

# RDR-055: Section-Type Metadata and Quality Scoring for Knowledge Collections

> Revise during planning; lock at implementation.
> If wrong, abandon code and iterate RDR.

## Problem Statement

Nexus indexes all chunks equally regardless of their source section. A chunk from a paper's References section has the same retrieval weight as one from Results. This adds noise — reference lists and acknowledgements are low-information-density content that competes with substantive findings in retrieval results.

Additionally, enriched documents carry bibliographic metadata (`bib_citation_count`, `bib_year`) that is never used as a retrieval signal.

### Baseline Evidence Needed

Before implementing, run a retrieval quality spot-check: 10-20 representative queries against current `knowledge__` collections. Record which results are noise (reference chunks, acknowledgements, boilerplate) and whether section filtering or quality weighting would have changed the ranking. This establishes whether the problem is real at current scale.

## Research Findings

### RF-1: EvidenceNet Section-Aware Design

**Source**: arxiv 2603.28325 (indexed: `knowledge__biomedical_kg`)

EvidenceNet's first stage carries section labels on every chunk from PDF extraction. Non-informational sections are excluded before downstream processing. Section headers from MinerU/Docling structured output serve as the classification signal — no separate ML model. Credited with "reducing unsupported inference and improving extraction fidelity."

### RF-2: Section Classification — Regex Works

**Source**: GROBID project, AgenticScholar

GROBID provides 55+ section labels via CRF but is a heavy Java dependency. A simpler regex approach, validated by AgenticScholar for well-structured PDFs, is sufficient for nexus:

```python
SECTION_PATTERNS = {
    "abstract": r"^abstract$",
    "introduction": r"^(1\.?\s*)?introduction",
    "methods": r"^(methods?|materials?\s*(and|&)\s*methods?|methodology)",
    "results": r"^(results?(\s*and\s*discussion)?)",
    "discussion": r"^discussion",
    "conclusion": r"^conclusions?",
    "references": r"^references?$",
    "acknowledgements": r"^acknowledg",
    "appendix": r"^appendi",
}
# Compile: {k: re.compile(v, re.IGNORECASE) for k, v in SECTION_PATTERNS.items()}
```

Must use `re.IGNORECASE` — PDF sections frequently use ALL CAPS or Title Case.

### RF-3: Quality Scoring Signals

**Source**: SurveyGen (arxiv 2508.17647, EMNLP 2025)

Signals available via Semantic Scholar API and already stored by `nx enrich`:
- `bib_citation_count` — already in ChromaDB metadata
- `bib_year` — already in ChromaDB metadata
- `influential_citation_count` — better signal than raw count (not yet stored)

Recency weighting formula: `score = α × cos(q, d) + (1-α) × 0.5^(age_days / h)`. α = 0.8 recommended. Half-life h: ML ~365 days, medical ~1,460 days.

**Caveat**: `bib_citation_count` is only populated after `nx enrich` runs. Unenriched collections get no quality signal. E3 is only meaningful for enriched collections.

### RF-4: GraphRAG Is Not Universally Better

**Source**: arxiv 2502.11371

- Single-hop: flat RAG wins by 3%
- Multi-hop: GraphRAG wins by 5.4%
- Time-sensitive: GraphRAG 13.4% worse

Graph densification (semantic link discovery, extended link types) is deferred to future work pending evidence that multi-hop queries are a significant fraction of actual nexus usage.

### RF-5: Codebase Surface Area

**E1 (Section metadata)**: `_make_chunk()` in md_chunker.py:338 already has `header_path`/`level`. `section_title` flows end-to-end to ChromaDB. Add regex classifier → `section_type` metadata. ~25 LOC for markdown. PDF gap: `pdf_chunker.py` operates on plain text from `PDFExtractor.extract()` with no access to Docling's structured heading output. `DocItemLabel` does not appear in the nexus codebase. The PDF path requires a `PDFExtractor` interface change to surface heading labels — this is a separate, larger effort than the markdown path.

**E3 (Quality score)**: `bib_citation_count` already in ChromaDB metadata via `_pdf_chunks()` and `nx enrich`. `min_max_normalize()` in scoring.py:34 is the reusable pattern. ~50 LOC.

## Proposed Enhancements

### E1: Section-Type Metadata Tagging

**Priority**: P1
**Effort**: Low (~25 LOC markdown path; PDF path deferred)
**LLM Required**: No

Classify each chunk's section header during chunking and store as ChromaDB metadata field `section_type`. Values: `abstract`, `introduction`, `methods`, `results`, `discussion`, `conclusion`, `references`, `acknowledgements`, `appendix`, `other`.

**Implementation (markdown path — this RDR)**:
- `md_chunker.py`: Add regex classifier (RF-2 patterns) over `header_path` text in `_make_chunk()`. The heading text is already available at line 338.
- `doc_indexer.py`: Read `section_type` from chunk metadata alongside existing `section_title`.
- Downstream: `query` MCP tool's `where` parameter can filter on `section_type` immediately (e.g., `where={"section_type": {"$ne": "references"}}`).

**PDF path (deferred — separate effort)**: `pdf_chunker.py` operates on plain text from `PDFExtractor.extract()` and has no access to Docling's structured heading output. `DocItemLabel` does not exist in the nexus codebase. Surfacing section types for PDFs requires a `PDFExtractor` interface change to return heading labels alongside text — this is a separate design task beyond E1's scope. PDF chunks will have `section_type=""` until this is addressed.

**Migration**: Existing documents need re-indexing (`--force`) to gain `section_type`. Same pattern as RDR-054's chunk overlap change.

**Validation**: Index a known markdown-heavy repo, verify `section_type` metadata populated. Run before/after retrieval comparison on 10 queries. Record results in T2 (`nexus_rdr/055-baseline`).

### E2: Quality-Weighted Reranking

**Priority**: P2 (conditional — only for enriched collections)
**Effort**: Low (~50 LOC)
**LLM Required**: No

Compute `quality_score` as a **reranking-time** signal (not index-time) from two inputs already in ChromaDB metadata:
```
quality_score = α × log(bib_citation_count + 1) / log(C + 1)
              + (1-α) × 0.5^(age_days / half_life)
```
Where α = 0.5 initially, half_life = 730 days, C = 10,000 (domain-appropriate constant — most papers have <10K citations; this avoids a collection-scan at index time). Skip quality signal entirely when `bib_citation_count == 0` (unenriched) to avoid bias.

Computed at reranking time in `scoring.py` alongside existing `min_max_normalize()` — not stored as metadata. This avoids the ordering problem of computing max at index time and keeps the signal stateless.

RF-3 notes `influential_citation_count` is a better signal but is not yet stored by `nx enrich`. E2 uses raw `bib_citation_count` as a first approximation; a future enhancement can switch to `influential_citation_count` once the enricher stores it.

**Gate**: Only implement after confirming >30% of chunks in target `knowledge__` collections have `bib_citation_count > 0`. Measurement: run `nx search <collection> --limit 0 --where 'bib_citation_count>0'` and compare count against total chunks from `nx collection info <collection>`. If enrichment coverage is below 30%, run `nx enrich <collection>` first.

## Future Work

The following are deferred pending evidence that they solve real problems at current scale. The research findings are preserved here as input for future RDRs.

### Semantic Link Discovery (was E4)

Batch discovery of `relates` links between documents via embedding similarity (distance < 0.2 per RF-7). ~150 LOC. **Gate**: Only pursue if `knowledge__` collections exceed 100 documents AND multi-hop queries are a measured use pattern. See RF-4 — graph structure only helps multi-hop by +5.4%.

When this is implemented, extend the link vocabulary with `supports`, `contradicts`, `refines`, `extends` as sub-task (link_type is already free-form per RDR-052).

### Chunk Pre-Screening (was E5)

Filter low-value chunks (references, acknowledgements) before embedding. Depends on E1 shipping and collections being re-indexed. ~30 LOC. **Gate**: Measure actual embedding cost savings — if references are <5% of chunks, the savings are negligible.

Note: RDR-054's chunk overlap may produce short overlap-only chunks. Filter on `section_type` rather than raw length to avoid discarding intentional overlap artifacts.

### Cross-Document Deduplication (was E6)

Detect near-duplicate chunks (distance < 0.1) across documents using `chunk_text_hash` (RDR-053) for exact dedup, ChromaDB for semantic dedup. Create `near-duplicate` catalog links (distinct from `relates`). ~100 LOC. **Gate**: Run a duplicate analysis on current collections first — is redundancy actually measurable?

### Structured Claim Extraction

LLM-based extraction of typed claims from chunks. Requires its own RDR covering schema design (see RF-8 in research archive), cost model (~$6-8/1K papers), and two-pass filter architecture. Prerequisite: parameterize `content_type` in `_catalog_store_hook()` (store.py:143, 2-line fix) — tracked separately, not part of this RDR.

### LightRAG Evaluation

If nexus needs graph retrieval beyond catalog `follow_links`/`depth`, LightRAG (EMNLP 2025) is the better fit over Microsoft GraphRAG — 48-80% win rate, dramatically cheaper queries. Worth its own evaluation RDR if multi-hop query demand materializes.

## Research Archive

The full EvidenceNet analysis (RF-1 through RF-10 of the original research) is preserved in T2 memory (`nexus_rdr/055-research-*`) and T3 knowledge store (`knowledge__knowledge`). Key items for future RDRs:

- **EvidenceNet 4-stage pipeline**: Section-aware preprocessing → two-pass LLM extraction (PICO schema) → normalization + quality scoring → graph construction with 6 directed edge types (SUPPORTS, CONTRADICTS, REFINES, EXTENDS, REPLICATES, CAUSAL_CHAIN)
- **Quality formula**: `S(e) = (w1·S_design + w2·S_impact + w3·S_stat + w4·S_sample)·(1−λ) + λ·C_LLM`. Weights not published — must calibrate independently.
- **Similarity thresholds (RF-7)**: cosine > 0.9 for near-duplicate, > 0.8 for `relates`, two-stage pre-filter at > 0.7 for typed relations
- **Claim schema (RF-8)**: `{claim, evidence_type, subject, predicate, object, metric, value, confidence, source_section, conditions}`
- **GraphRAG benchmarks (RF-4)**: Only +5.4% for multi-hop; flat RAG wins single-hop by 3%

## Sources

- arxiv 2603.28325 — "Building evidence-based knowledge graphs from full-text literature" (indexed: `knowledge__biomedical_kg`)
- arxiv 2502.11371 — GraphRAG systematic evaluation
- SurveyGen (arxiv 2508.17647, EMNLP 2025) — quality-aware RAG framework
- GROBID — https://github.com/kermitt2/grobid
- Nexus codebase: `md_chunker.py`, `pdf_chunker.py`, `doc_indexer.py`, `scoring.py`, `pdf_extractor.py`

## Post-Mortem

**Closed**: 2026-04-07 — E1 implemented, E2 deferred.

### What Was Built (E1)

- `classify_section_type(header_path)` — regex classifier with 9 patterns + optional numeric prefix (`_NUM`), in `md_chunker.py`
- `SECTION_PATTERNS` dict — compiled `re.IGNORECASE` patterns for: abstract, introduction, methods, results, discussion, conclusion, references, acknowledgements, appendix
- `section_type` metadata wired through all 5 indexing paths: `doc_indexer._markdown_chunks`, `doc_indexer._pdf_chunks` (empty), `prose_indexer` markdown branch, `prose_indexer` non-markdown branch (empty), `pipeline_stages` PDF default (empty)
- 36 new tests across `test_md_chunker.py`, `test_doc_indexer.py`, `test_indexer.py`

### What Was Deferred (E2)

E2 (quality_score reranking from `bib_citation_count`) deferred — enrichment gate returned 0% coverage across all `knowledge__` collections. The `bib_citation_count` field is entirely absent, not zero. Beads nexus-rg6x and nexus-3idt deferred to 2026-04-14 pending `nx enrich` run.

### Baseline Results

10 queries across 5 `knowledge__` collections, 100 results classified:
- **21% noise** overall (16 reference chunks, 5 boilerplate)
- **Q1 pathology**: "consensus protocol" returned 100% reference noise — reference sections cite papers about the exact concepts queried
- With `section_type != references` filter: noise drops to **5%** (76% reduction)

### Review Finding (fixed in-band)

Code review found numbered section prefixes (e.g., "3. Methods") only handled for `introduction`. Extended `_NUM` prefix to all patterns except `abstract` and `references` (which are typically unnumbered). 4 additional tests added.

### Beads

Epic: nexus-x36c (closed). Children:
- nexus-5u6j: baseline spot-check (closed)
- nexus-zg3p: E1 classifier (closed)
- nexus-xbco: E1 pipeline wiring (closed)
- nexus-2jef: E1 code review (closed)
- nexus-fsyo: E2 enrichment gate (closed — NO-GO)
- nexus-1xrn: integration validation (closed)
- nexus-rg6x: E2 implementation (deferred)
- nexus-3idt: E2 review (deferred)

## Open Questions

1. **Baseline measurement**: How many of the current retrieval results are actually noise from reference/acknowledgement chunks? Run the 10-query spot-check before implementing. Record results in T2 (`nexus_rdr/055-baseline`).
2. **PDF section extraction (follow-on)**: `pdf_chunker.py` operates on plain text; `DocItemLabel` doesn't exist in the codebase. Surfacing section types for PDFs requires a `PDFExtractor` interface change. Separate design task — not blocking E1 (markdown path).
3. **Section regex accuracy**: Validated for well-structured PDFs. Unknown for heterogeneous formats. GROBID is the fallback if regex proves insufficient.
4. **C constant calibration**: E2 uses C=10,000 for citation normalization. May need adjustment per domain — biomedical papers can exceed this. Monitor and adjust.
