---
title: "Chunk Boundary Equation Splitting — Information Loss at Chunk Boundaries"
id: RDR-054
type: Bug
status: accepted
priority: high
author: Hal Hildebrand
reviewed-by: self
created: 2026-04-07
accepted_date: 2026-04-07
related_issues: []
---

# RDR-054: Chunk Boundary Equation Splitting — Information Loss at Chunk Boundaries

> Revise during planning; lock at implementation.
> If wrong, abandon code and iterate RDR.

## Problem Statement

When indexing PDF papers with mathematical equations, the chunker splits equation definitions across chunk boundaries such that:
- Chunk N ends with the prose leading to the definition (e.g., "The sigmoid excitatory feedback signal: f_u(u_ij) =")
- Chunk N+1 starts with the formula body, WITHOUT the variable name from Chunk N

This makes the formula **unsearchable by its variable name**. Searching for "f_u" finds Chunk N (truncated before the formula). Searching for the formula content finds Chunk N+1 but without context about what function it defines.

### Reproduction

- Paper: GnaGroSovereign2007.pdf (109 pages, 537 chunks, MinerU extractor)
- Section: Appendix A4.5, Eq.56 (page 88)
- Search: "sigmoid excitatory feedback signal f_u" → returns chunk ending before the formula
- Re-indexing with `--extractor mineru --force` produces identical truncation

The actual equation (from direct PDF read):
```
f_u(u_ij) = α^N · ([u_ij]⁺)^N / (α^N + θ · ([u_ij]⁺)^N)
where α=1.7, N=5, θ=9.8
```

### Impact

- HIGH for mathematical/scientific papers where equation definitions are primary content
- The f_u function was a Hill sigmoid, NOT the ([u]⁺)² inferred from analogy. The inference was wrong — 3 independent lines of analogical evidence all pointed to the wrong answer because the actual definition was trapped in a chunk boundary.
- Multiple parameter values (D₁=18 vs assumed 1.0, S_j={1/6,1/3,1/2} vs assumed {1.0,0.75,0.5}) were also in this truncated region.
- 17 parallel agents across 4 rounds of deep research (851+ chunk searches) all failed to find this equation via T3 search. Only resolved by reading the PDF directly with the Read tool at the page number.

## Proposed Mitigations

### M1: Chunk overlap (recommended, general-purpose)

**Priority 1 (CRITICAL):** Wire SemanticMarkdownChunker's dead `overlap_chars` (165 chars) into `_split_large_section` which currently has zero overlap. Affects: `md_chunker.py` lines 288–309. Cost: +5-8% corpus-level (only split sections pay).

**Priority 2 (HIGH):** Increase PDFChunker overlap from 225→300 chars (15%→20%). Affects: `pdf_chunker.py` line 12 (`_DEFAULT_OVERLAP`). Cost: +6.3% chunks (<$0.001/paper embedding).

**Migration cost:** Existing indexed documents retain old chunk boundaries until re-indexed. Full corpus re-indexing (`nx index pdf --force` per paper) required to benefit from the fix. For large corpora, estimate total embedding cost as documents × avg_chunks × embedding_price_per_token before running.

300 chars covers all tested equation types including worst case (Long Boltzmann at 202 chars) for the overlap window. However, overlap only ensures the *leading context* is repeated — it does not prevent mid-equation splits for equations longer than the overlap window.

### M2: Equation-aware chunking

Detect LaTeX equation environments (`\begin{equation}...\end{equation}`, `$$...$$`, `\tag{N}`) and ensure they are never split across chunks.

Affects: `pdf_chunker.py` primarily (LaTeX in extracted text).

Trade-off: requires equation boundary detection in extracted text, which varies by extractor. **Not yet verified:** whether MinerU output for GnaGroSovereign2007.pdf actually contains `$$`, `\begin{equation}`, or `\tag{N}` markers. If MinerU renders equations as plain text/Unicode, M2's LaTeX boundary detection has zero coverage. Needs an RF verifying the actual extraction format before implementing.

### M3: Paragraph-boundary chunking

Split only at paragraph boundaries (double newline), never mid-paragraph. The f_u definition was mid-paragraph — M3 addresses this failure mode by construction.

Affects: `pdf_chunker.py` (sentence snapper could prefer `\n\n` over `. `), `md_chunker.py`.

Trade-off: may produce very large chunks for papers with long prose paragraphs. Needs a max-size fallback. However, scientific appendix equations are typically in short display-math paragraphs — paragraph length distribution in the reproduction document (GnaGroSovereign2007.pdf) should be measured to validate this concern. If equation-bearing paragraphs are short, M3 + max-size fallback may be simpler and more correct than overlap alone.

### M4: Post-extraction equation linking

After chunking, scan for equation references (e.g., "Eq.56", "\tag{56}") and create cross-references between chunks that reference the same equation. Could use catalog links with `relates` type.

Affects: new module or extension to `link_generator.py`.

Trade-off: doesn't fix searchability — the equation is still split. But makes it discoverable via link traversal.

## Recommendation

Start with M1 (chunk overlap) — md_chunker first (CRITICAL: currently zero overlap), then PDFChunker bump to 20%. M1 reduces the probability of context loss at boundaries but does not prevent mid-equation splits for equations longer than ~300 chars. M2 (equation-aware snapper) remains necessary for the elimination case.

M3 (paragraph boundaries) deserves evaluation alongside M1 for PDFChunker — it directly addresses the observed failure mode (mid-paragraph split). M4 is independent and complementary.

## Key Discoveries

### RF-1: Chunker boundary overlap audit (verified, source search)

Three distinct overlap gaps identified:

1. **PDFChunker** (`pdf_chunker.py`): Has 225-char overlap (15% of 1500), but boundary snapper at lines 59–63 only looks for `. ` (period-space) — no awareness of LaTeX `$$...$$` or equation environments. Will split mid-equation if the equation doesn't end with a sentence period.

2. **SemanticMarkdownChunker** (`md_chunker.py`): `_split_large_section()` at lines 261–323 has **zero overlap** between sub-chunks. The `chunk_overlap` / `overlap_chars` fields are defined but never used by the semantic path — dead code. Only the naive fallback path has overlap (165 chars). This is the most significant gap.

3. **`_enforce_byte_cap`** (`chunker.py:163–211`): Sub-splits of oversized AST nodes also have zero overlap between sub-chunks. Lower priority since code chunking uses AST boundaries.

**Insertion points for fix:**
- `md_chunker.py:288–309`: Before `chunks.append(...)` at line 288, capture `emitted_text = "\n\n".join(current_parts)`. After the flush, prepend `emitted_text[-overlap_chars:]` to the new `current_parts` (after the header, so section context is preserved). The naive fallback path at lines 354–376 is a working reference implementation — same overlap structure, already tested.
- `pdf_chunker.py:59–63`: Extend snapper to check for unclosed `$$` in candidate window

### RF-2: Prior art — RAG chunking overlap and equation-aware splitting (verified, source search)

No mainstream chunking library has equation/math-aware splitting (verified across LlamaIndex, LangChain, Unstructured, Chonkie, semchunk). Default overlaps: LlamaIndex SentenceSplitter ~19.5%, LangChain 200 chars (5%), Unstructured 0. Literature consensus: 10-20% optimal (NVIDIA FinanceBench). Nexus PDFChunker at 15% is already in range — the bug is the sentence snapper and the dead md_chunker overlap code. Equation-boundary snapping would be novel.

### RF-3: Storage cost analysis — overlap overhead measured empirically (verified, spike)

PDFChunker 225→300 chars: +6.3% chunks, <$0.001/paper. 450 chars (doubled): +21.7% — excessive. md_chunker wiring 165ch overlap: +5-8% corpus-level. 200 chars is NOT enough for multi-line equations (Long Boltzmann at 202ch). 300 chars covers all tested types with margin. md_chunker overlap is CRITICAL priority (currently zero).

## Finalization Gate

- [x] Overlap size determined: 300 chars PDFChunker, 165 chars md_chunker (RF-3)
- [x] Storage overhead measured: +6.3% PDF, +5-8% md corpus-level (RF-3)
- [ ] Impact validated against real-world corpus (post-implementation)
- [ ] No regression in existing chunker tests (post-implementation)
- [ ] Search recall improvement measured for boundary-split cases (post-implementation)
- [ ] MinerU extraction format verified for equation markers (RF needed for M2)

## Revision History

- 2026-04-07: Gate PASSED. 2 criticals resolved in-gate (insertion point corrected, overlap limitation acknowledged), 3 significants addressed (migration cost, M3 evaluation, M2 feasibility).
- 2026-04-07: RF-1 (code audit), RF-2 (prior art), RF-3 (cost analysis) — all verified.
- 2026-04-07: Created from T2 issue report (nexus_issues/chunker-splits-equations-at-boundary). Source: RDR-068 research session where 17 parallel agents failed to find f_u equation definition.
