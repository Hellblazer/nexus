---
title: "Math-Aware PDF Extraction"
id: RDR-044
type: Bug
status: draft
priority: high
author: Hal Hildebrand
reviewed-by: ""
created: 2026-03-31
related_issues:
  - "RDR-021 - Docling PDF Extraction (closed)"
  - "RDR-042 - AgenticScholar-Inspired Enhancements (accepted)"
---

# RDR-044: Math-Aware PDF Extraction

> Revise during planning; lock at implementation.
> If wrong, abandon code and iterate RDR.

## Problem Statement

The PDF extraction pipeline silently replaces all mathematical equations with `<!-- formula-not-decoded -->` placeholder markers. This is a total extraction failure for mathematical content — not a partial degradation but a complete loss of semantic information for every equation in every indexed paper.

For a collection of ~300 math-heavy academic papers (cryptography, distributed systems, formal methods), this means:
- Every theorem statement has holes where the formulas should be
- Proof steps that reference equations are semantically disconnected
- Algorithm complexity bounds (O-notation, recurrence relations) are lost
- Protocol security parameters and probability bounds are missing
- Semantic search for mathematical concepts returns text *around* equations but never the equations themselves

The failure is **silent** — no warning during indexing, no metadata flag on affected chunks, no way for the user or downstream agents to know content is missing.

## Context

### Background

RDR-021 established Docling as the primary PDF extraction backend (replacing a 3-tier stack). Docling excels at multi-column layout, reading order, and table detection. But its equation handling depends on an optional pipeline stage that is either not enabled or not effective for the papers in this corpus.

Docling's `export_to_markdown()` produces `<!-- formula-not-decoded -->` for any mathematical notation it cannot convert to text or LaTeX. This includes inline math, display equations, and equation environments.

### Scale of Impact

- ~300 papers indexed in `knowledge__*` collections
- Papers span: distributed systems, cryptography, formal verification, consensus protocols
- Estimated: 20-100+ equations per paper → 6,000-30,000 lost formula instances
- Every chunk containing a formula has degraded embedding quality — the embedding model sees "formula not decoded" instead of the actual mathematical content

### Current Pipeline

```
PDF → Docling (primary) → markdown with <!-- formula-not-decoded --> → chunks → embeddings
    → PyMuPDF (fallback on Docling failure) → raw text (Unicode math symbols, imperfect)
```

## Proposed Solution

Evaluate and integrate a math-aware extraction backend. Three candidates:

### Option A: Nougat (Meta AI)

Neural OCR model specifically trained on academic papers. Outputs LaTeX for equations.
- Pros: Open source, specifically designed for academic PDFs, handles complex equations
- Cons: GPU-intensive, slow (~1 page/sec on CPU), large model download (~2GB)
- Integration: Replace Docling for math-heavy PDFs, or use as post-processing for formula regions

### Option B: Marker

Open-source PDF-to-markdown converter with equation support via Texify model.
- Pros: Fast, produces clean markdown with LaTeX math blocks, active development
- Cons: Requires surya + texify model downloads, less battle-tested than Docling for layout
- Integration: Could replace entire Docling pipeline or serve as equation-only supplement

### Option C: Docling equation pipeline

Docling may have configurable equation handling that we're not enabling.
- Pros: No new dependency, stays within current architecture
- Cons: May not exist or may be insufficient — needs investigation
- Integration: Configuration change in `_get_converter()` if available

### Option D: Hybrid approach

Use Docling for layout/tables + Nougat or Marker for equations only.
- Pros: Best of both worlds — Docling's layout intelligence + dedicated math extraction
- Cons: Complex pipeline, two models to manage, alignment between outputs

### Immediate mitigation (regardless of which option)

1. **Detection**: Count `formula-not-decoded` placeholders during `nx index pdf` and warn the user
2. **Metadata flag**: Add `has_formula_gaps: true` to chunk metadata when placeholders detected
3. **PyMuPDF math fallback**: When Docling produces formula placeholders, try PyMuPDF for those pages — it extracts Unicode math symbols which, while imperfect, are better than nothing

## Research Needed

- [ ] What does Docling's current equation pipeline offer? Check `PdfPipelineOptions` for math-related flags
- [ ] Nougat: install, run on sample papers, measure quality + latency
- [ ] Marker: install, run on same samples, compare
- [ ] Can Nougat/Marker be scoped to formula regions only (not full-page re-extraction)?
- [ ] What LaTeX representation works best for embedding models? Raw LaTeX vs Unicode vs description?
- [ ] How does PyMuPDF handle the same equations? Is it a viable interim fallback?

## Success Criteria

- [ ] Mathematical equations extracted as LaTeX or Unicode (not placeholder markers)
- [ ] `nx index pdf` warns when formula placeholders are detected
- [ ] Chunk metadata includes `has_formula_gaps` flag for affected content
- [ ] Re-indexed math papers show equation content in search results
- [ ] Existing non-math PDFs are unaffected (no regression)
- [ ] Solution works on CPU (GPU optional for speed)
