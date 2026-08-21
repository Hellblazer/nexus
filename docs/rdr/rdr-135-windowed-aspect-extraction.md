---
title: "Windowed Aspect Extraction with Cross-Window Merge: Stop Whole-Paper Single-Shot Extraction from Degrading on Long Inputs"
id: RDR-135
type: Architecture
status: draft
priority: medium
author: Hal Hildebrand
reviewed-by: self
created: 2026-05-27
accepted_date:
related_issues: [nexus-u4qxk]
related_rdrs: [RDR-089]
related_tests: []
implementation_notes: ""
---

# RDR-135: Windowed Aspect Extraction with Cross-Window Merge

> Revise during planning; lock at implementation.
> If wrong, abandon code and iterate RDR.

> **STUB** captured 2026-05-27. Reshapes bead `nexus-u4qxk` (MemForest
> synthesis idea #4, "aspect extraction chunk-size knob"), which is not
> bead-sized: grounding showed the cheap version (a head char-cap) is actively
> harmful, and the correct version needs a cross-window merge strategy.
> Problem Statement and Approach are sketched; deeper gate sections await
> `/conexus:rdr-research`.

## Problem Statement

`aspect_extractor.extract_aspects` interpolates the **entire** document into a
single prompt (`config.prompt_template.format(content=content)`) with no length
bound (verified 2026-05-27). For long papers the prompt balloons and the
extracted fields (`problem_formulation`, `proposed_method`,
`experimental_results`, ...) can be truncated or blended across sections.
MemForest (Appendix C; catalog tumbler `1.14.4`) shows extraction chunk size is
a real accuracy lever. The original bead proposed a simple
`extraction_chunk_size` cap, but a head-truncation cap would **drop the results
section** that `scholarly-paper-v1` is specifically meant to extract, so the
cheap knob is the wrong fix.

### Enumerated gaps to close

#### Gap 1: whole-paper single-shot extraction has no length discipline

One prompt holds the whole document. Long inputs degrade extraction quality and
inflate cost, with no windowing and no per-section guarantee.

#### Gap 2: the correct fix needs a cross-window merge strategy

Windowing the document and extracting per window means reconciling N partial
aspect records into one. Scalar fields (`problem_formulation`,
`proposed_method`) need a "best/most-confident wins" rule; list fields
(`experimental_datasets`, `experimental_baselines`) likely need a union. That
merge strategy is the load-bearing design decision and the reason this is not a
one-line knob.

## Context

### Background

Surfaced while indexing the MemForest paper into `knowledge__dt-papers` and
reviewing the MemForest × nexus synthesis (idea #4, T3
`research-memforest-nexus-leverage-2026-05-27`). The synthesis optimistically
framed this as a ~2h knob; grounding against `aspect_extractor.py` showed the
single-shot path and the results-loss hazard of a naive cap.

### Technical Environment

- `src/nexus/aspect_extractor.py`: `extract_aspects`, `extract_aspects_batch`,
  `_build_batch_prompt`, `_build_record`/`_build_record_from_entry`,
  `ExtractorConfig` (frozen dataclass), `_SCHOLARLY_PAPER_CONFIG`,
  `_SCHOLARLY_BATCH_PROMPT_HEADER`.
- Aspect schema fields (T2 `document_aspects`): `problem_formulation`,
  `proposed_method`, `experimental_datasets`, `experimental_baselines`,
  `experimental_results`, `extras`, `salient_sentences`, `confidence`.

## Research Findings

### Investigation

[To be completed during `/conexus:rdr-research`: characterize the input-length
distribution that triggers degradation; prototype window sizes; design and
validate the per-field merge rule against multi-section papers.]

### Key Discoveries

- **Documented**: `extract_aspects` sends full content, no cap (read 2026-05-27).
- **Documented**: MemForest Appendix C names chunk size as an accuracy lever
  (tumbler `1.14.4`).
- **Assumed**: windowed extraction + per-field merge beats single-shot on long
  papers without harming short ones. Needs a spike.

### Critical Assumptions

- [ ] Windowed extraction improves field completeness on long papers vs
  single-shot — **Status**: Unverified — **Method**: Spike
- [ ] A per-field merge rule (scalar best-wins, list union) reconciles windows
  without contradiction — **Status**: Unverified — **Method**: Spike
- [ ] Short papers are unaffected (single window = current behavior) —
  **Status**: Unverified — **Method**: Spike

## Proposed Solution

### Approach

Add an optional windowing config to `ExtractorConfig` (e.g.
`extraction_window_chars` / `extraction_window_overlap`, default unset =
current single-shot behavior). When set and content exceeds the window,
extract per window and merge: scalar fields by highest-confidence / longest
non-empty; list fields (`experimental_datasets`, `experimental_baselines`) by
deduped union; `salient_sentences` by top-K across windows. Section-aware
windowing (split on headings) is preferable to blind char windows so the
results section lands intact in some window.

### Existing Infrastructure Audit

| Proposed Component | Existing Module | Decision |
| --- | --- | --- |
| windowing + per-window extract | `extract_aspects` | Extend |
| cross-window merge | new helper in `aspect_extractor.py` | New |
| window config | `ExtractorConfig` | Extend (backward-compatible default) |

[Decision rationale, alternatives (incl. the rejected head-cap), trade-offs,
test plan, finalization gate: to be completed during research.]

## References

- MemForest paper, Appendix C, catalog tumbler `1.14.4`
- T3 synthesis: `research-memforest-nexus-leverage-2026-05-27` (idea #4)
- RDR-089 (Structured Aspect Extraction at Ingest) — the extractor this extends
- Bead `nexus-u4qxk` (superseded by this RDR)
